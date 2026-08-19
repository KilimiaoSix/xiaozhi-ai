"""编码助手工具调用的审批状态机。

链路：Claude Code / Codex 的 PreToolUse 钩子 POST 一条审批请求过来 → 机器人
开口播报"谁要做什么" → 主人对着桌面摄像头竖大拇指 → 手势观察者把请求解决掉 →
钩子轮询到 approved 才放行。

两条硬性要求决定了这里的设计：

1. **默认不批准**。超时、设备离线、识别不确定，全都落到"不批准"。所以到期
   自动转 timeout，而 timeout 在钩子那边与 denied 同样处理。任何"拿不准"的
   状态都不会变成放行。
2. **幂等**。手势与人工兜底（HTTP decision 接口）可能同时到达，只有 pending
   能转成终态；重复 resolve 直接返回当前状态，不会二次推送、不会把已拒绝的
   请求翻成批准。

设备离线仍然创建请求：钩子端有自己的超时兜底，这里少建一条记录反而让
GET 状态查不到，钩子只能一直轮询到自己超时。创建成功但播报失败只记日志。

计时用 asyncio 任务，clock 与 sleep 都可注入，测试不必真等 25 秒。
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

TAG = __name__

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_TIMEOUT = "timeout"

# 终态集合：只有 pending 能往里转，进来了就不再变（幂等的实现基础）
TERMINAL_STATUSES = frozenset({STATUS_APPROVED, STATUS_DENIED, STATUS_TIMEOUT})
# 外部允许显式指定的决策；timeout 只能由计时器产生，接口传不进来
DECISIONS = frozenset({STATUS_APPROVED, STATUS_DENIED})

SOURCE_GESTURE = "gesture"
SOURCE_MANUAL = "manual"
SOURCE_TIMEOUT = "timeout"

DEFAULT_TIMEOUT_S = 25.0
# 审批上限：钩子端最长也就等半分钟量级，再长的请求没有意义，
# 且过长的 pending 会一直让手势观察者解码每一帧
MAX_TIMEOUT_S = 300.0
DEFAULT_RISK = "local_test"

# 终态推送几秒后把画面收回设备基态，别让"已允许"一直挂在屏幕上
DEFAULT_RESTORE_AFTER_S = 6.0

# 固件按字符串直接渲染，取值必须落在它支持的表情表里
EMOTION_ASK = "confused"
EMOTION_APPROVED = "happy"
EMOTION_DENIED = "neutral"
STATUS_TEXT_ASK = "等待审批"
STATUS_TEXT_APPROVED = "已批准"
STATUS_TEXT_DENIED = "已拒绝"
STATUS_TEXT_TIMEOUT = "已超时"
ACTION_ASK = "look_left"
ACTION_APPROVED = "nod"
ACTION_DENIED = "shake"

TEXT_APPROVED = "已允许，继续执行"
TEXT_DENIED = "没有确认，先不执行"
TEXT_TIMEOUT = "超时没有确认，先不执行"


async def _default_push_event(conn, text: str, **kwargs) -> bool:
    from core.handle.pushHandle import push_work_event

    return await push_work_event(conn, text=text, **kwargs)


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    if parsed <= 0:
        return fallback
    return min(parsed, MAX_TIMEOUT_S)


@dataclass
class _Request:
    approval_id: str
    summary: str
    risk: str
    status: str
    created_at: float
    expires_at: float
    decision_source: Optional[str] = None
    decided_at: Optional[float] = None
    task: Optional[asyncio.Task] = None


@dataclass(frozen=True)
class _Settings:
    timeout_s: float
    device_id: str
    restore_after_s: float
    speak: bool


def _read_settings(config: Optional[dict]) -> _Settings:
    """读 approval 配置段。全部带默认值，不写这一段也能跑。"""
    section = (config or {}).get("approval") or {}
    if not isinstance(section, dict):
        section = {}
    return _Settings(
        timeout_s=_positive_float(section.get("timeout_s"), DEFAULT_TIMEOUT_S),
        device_id=str(section.get("device_id") or "").strip(),
        restore_after_s=_positive_float(
            section.get("restore_after_s"), DEFAULT_RESTORE_AFTER_S
        ),
        speak=bool(section.get("speak", True)),
    )


class ApprovalManager:
    """审批请求集合。

    推送函数、设备解析、时钟与 sleep 都可注入，便于离线单测；
    生产用默认实现走 pushHandle 与设备注册表。
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        device_registry=None,
        *,
        push_event: Optional[Callable] = None,
        device_resolver: Optional[Callable[[], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable] = None,
        logger=None,
    ) -> None:
        self._config = config
        self._device_registry = device_registry
        self._push_event = push_event or _default_push_event
        self._device_resolver = device_resolver
        self._clock = clock or time.time
        self._sleep = sleep or asyncio.sleep
        self._logger = logger or logging.getLogger(__name__)
        self._logger_bound = logger is not None
        # 插入序即创建序，resolve_pending 取最老的那条靠的就是它
        self._requests: Dict[str, _Request] = {}

    # ------------------------------------------------------------ 装配

    def bind(
        self, config: Optional[dict] = None, device_registry=None, logger=None
    ) -> None:
        """补上生产环境的 config、设备注册表与 logger，只填空缺不覆盖已有的
        （同 PomodoroManager.bind）。"""
        if config is not None and self._config is None:
            self._config = config
        if device_registry is not None and self._device_registry is None:
            self._device_registry = device_registry
        if logger is not None and not self._logger_bound:
            self._logger = logger
            self._logger_bound = True

    @property
    def settings(self) -> _Settings:
        return _read_settings(self._config)

    # ------------------------------------------------------------ 创建

    async def create_request(
        self,
        summary: str,
        *,
        risk: str = DEFAULT_RISK,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """登记一条待审批请求，并让机器人开口问。

        返回 {approval_id, status, expires_at, summary, risk, timeout_s}。
        播报失败（设备离线 / TTS 没就绪）不影响请求成立——钩子端还有超时兜底，
        最坏情况是没人听见、到点默认拒绝。
        """
        settings = self.settings
        summary = str(summary or "").strip()
        if not summary:
            raise ValueError("summary 不能为空")

        window = _positive_float(timeout_s, settings.timeout_s)
        now = self._clock()
        request = _Request(
            approval_id=uuid.uuid4().hex,
            summary=summary,
            risk=str(risk or DEFAULT_RISK),
            status=STATUS_PENDING,
            created_at=now,
            expires_at=now + window,
        )
        self._requests[request.approval_id] = request
        request.task = asyncio.create_task(
            self._expire_after(request.approval_id, window)
        )
        self._logger.info(
            f"新建审批请求 {request.approval_id}: {summary}"
            f"（risk={request.risk}, 超时 {window:g}s）"
        )

        await self._announce(request, settings)
        return self._snapshot(request)

    async def _announce(self, request: _Request, settings: _Settings) -> None:
        """播报"谁要做什么"。这是整条链路里唯一让主人知情的环节，失败要记日志。"""
        conn = self._resolve_conn(settings)
        if conn is None:
            self._logger.warning(
                f"审批请求 {request.approval_id} 无可用设备，跳过播报"
                f"（仍会按超时默认拒绝）"
            )
            return
        text = f"Codex 想{request.summary}。竖个大拇指我就让它继续。"
        try:
            await self._push_event(
                conn,
                text=text,
                emotion=EMOTION_ASK,
                status=STATUS_TEXT_ASK,
                action=ACTION_ASK,
                speak=settings.speak,
            )
        except Exception as e:
            self._logger.warning(f"审批请求 {request.approval_id} 播报失败: {e}")

    # ------------------------------------------------------------ 查询

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        """按 id 取状态快照，不存在返回 None。"""
        request = self._requests.get(str(approval_id or ""))
        if request is None:
            return None
        return self._snapshot(request)

    def has_pending(self) -> bool:
        """当前是否有等待中的请求。手势观察者用它决定要不要看帧。"""
        return any(r.status == STATUS_PENDING for r in self._requests.values())

    def pending_ids(self) -> List[str]:
        """按创建先后列出待审批 id，最老的在前。"""
        return [
            r.approval_id
            for r in self._requests.values()
            if r.status == STATUS_PENDING
        ]

    # ------------------------------------------------------------ 决策

    async def resolve(
        self, approval_id: str, decision: str, source: str = SOURCE_MANUAL
    ) -> Optional[Dict[str, Any]]:
        """把一条请求转成终态。幂等：只有 pending 能转，重复调用返回当前状态。

        decision 只接受 approved / denied——timeout 是计时器的内部产物，
        不能从外部塞进来冒充"用户拒绝"。
        """
        decision = str(decision or "").strip()
        if decision not in DECISIONS:
            raise ValueError(f"未知决策: {decision}")

        request = self._requests.get(str(approval_id or ""))
        if request is None:
            return None
        return await self._settle(request, decision, str(source or SOURCE_MANUAL))

    async def resolve_pending(
        self, decision: str, source: str = SOURCE_GESTURE
    ) -> Optional[Dict[str, Any]]:
        """解决最老的那条 pending 请求。

        手势不知道 approval_id——摄像头只看得见一只大拇指。取最老的那条是因为
        钩子是串行阻塞的：新请求出现时，老请求要么已经解决、要么就是用户此刻
        正在回应的那一条。
        """
        decision = str(decision or "").strip()
        if decision not in DECISIONS:
            raise ValueError(f"未知决策: {decision}")

        for request in self._requests.values():
            if request.status == STATUS_PENDING:
                return await self._settle(
                    request, decision, str(source or SOURCE_GESTURE)
                )
        return None

    async def _settle(
        self, request: _Request, status: str, source: str
    ) -> Dict[str, Any]:
        """状态转移 + 终态播报。非 pending 直接返回现状，一次都不重复推送。"""
        if request.status != STATUS_PENDING:
            return self._snapshot(request)

        # 先置位再 await：手势与人工决策同时到达时，第二条会看到已是终态而退出，
        # 不会两条都推一次画面
        request.status = status
        request.decision_source = source
        request.decided_at = self._clock()
        self._cancel_timer(request)
        self._logger.info(
            f"审批请求 {request.approval_id} -> {status}（来源 {source}）: "
            f"{request.summary}"
        )

        await self._announce_outcome(request)
        return self._snapshot(request)

    async def _expire_after(self, approval_id: str, delay: float) -> None:
        """到点把还没决策的请求转成 timeout（视同拒绝）。"""
        try:
            await self._sleep(delay)
            request = self._requests.get(approval_id)
            if request is None or request.status != STATUS_PENDING:
                return
            request.status = STATUS_TIMEOUT
            request.decision_source = SOURCE_TIMEOUT
            request.decided_at = self._clock()
            request.task = None
            self._logger.info(
                f"审批请求 {approval_id} 超时未确认，按默认不批准处理: "
                f"{request.summary}"
            )
            await self._announce_outcome(request)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._logger.warning(f"审批请求 {approval_id} 计时异常: {e}")

    def _cancel_timer(self, request: _Request) -> None:
        task = request.task
        request.task = None
        if task is not None and not task.done():
            task.cancel()

    async def _announce_outcome(self, request: _Request) -> None:
        """终态播报。推送失败只记日志：状态已经落定，钩子照样查得到。"""
        settings = self.settings
        conn = self._resolve_conn(settings)
        if conn is None:
            return

        if request.status == STATUS_APPROVED:
            text, emotion, status_text, action = (
                TEXT_APPROVED,
                EMOTION_APPROVED,
                STATUS_TEXT_APPROVED,
                ACTION_APPROVED,
            )
        elif request.status == STATUS_TIMEOUT:
            # 超时与拒绝在语气上不同：一个是"没听见"，一个是"你说了不"，
            # 但落到行为上完全一致——都不放行
            text, emotion, status_text, action = (
                TEXT_TIMEOUT,
                EMOTION_DENIED,
                STATUS_TEXT_TIMEOUT,
                ACTION_DENIED,
            )
        else:
            text, emotion, status_text, action = (
                TEXT_DENIED,
                EMOTION_DENIED,
                STATUS_TEXT_DENIED,
                ACTION_DENIED,
            )

        try:
            await self._push_event(
                conn,
                text=text,
                emotion=emotion,
                status=status_text,
                action=action,
                speak=settings.speak,
                restore_after=settings.restore_after_s,
            )
        except Exception as e:
            self._logger.warning(f"审批请求 {request.approval_id} 终态播报失败: {e}")

    # ------------------------------------------------------------ 设备

    def _resolve_conn(self, settings: _Settings):
        """取要播报的那条设备连接。

        注入的 device_resolver 优先（测试与特殊编排用）；否则按配置的
        approval.device_id 查注册表，没配就在只有一台设备在线时用那一台
        ——演示场景就一个机器人，逼用户配一遍 MAC 没有意义；多台在线时
        不猜，宁可不播报也不要冲着错误的设备喊。
        """
        if self._device_resolver is not None:
            try:
                return self._device_resolver()
            except Exception as e:
                self._logger.warning(f"解析审批播报设备失败: {e}")
                return None

        registry = self._device_registry
        if registry is None:
            return None
        if settings.device_id:
            return registry.get(settings.device_id)
        try:
            online = list(registry.device_ids())
        except Exception:
            return None
        if len(online) == 1:
            return registry.get(online[0])
        if len(online) > 1:
            self._logger.warning(
                f"有 {len(online)} 台设备在线但未配置 approval.device_id，跳过审批播报"
            )
        return None

    # ------------------------------------------------------------ 快照

    def _snapshot(self, request: _Request) -> Dict[str, Any]:
        return {
            "approval_id": request.approval_id,
            "status": request.status,
            "decision_source": request.decision_source,
            "summary": request.summary,
            "risk": request.risk,
            "created_at": request.created_at,
            "expires_at": request.expires_at,
            "decided_at": request.decided_at,
        }


# ------------------------------------------------------------------ 单例

_manager: Optional[ApprovalManager] = None


def get_approval_manager(
    config: Optional[dict] = None, device_registry=None, **kwargs
) -> ApprovalManager:
    """取进程级单例。

    手势观察者、HTTP 接口、装配代码拿到的必须是同一个实例，否则手势解决的
    请求钩子那边查不到。首次调用建实例，之后的 config / registry 通过 bind
    补空缺（HTTP 接口通常比设备连接先起来）。
    """
    global _manager
    if _manager is None:
        _manager = ApprovalManager(config, device_registry, **kwargs)
    else:
        _manager.bind(
            config=config,
            device_registry=device_registry,
            logger=kwargs.get("logger"),
        )
    return _manager


def reset_approval_manager() -> None:
    """丢弃单例。测试之间隔离用，生产不调。"""
    global _manager
    _manager = None
