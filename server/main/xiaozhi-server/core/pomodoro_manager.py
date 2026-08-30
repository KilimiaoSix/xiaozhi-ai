"""番茄钟状态机与设备推送编排。

经典循环：专注 → 短休 → …… → 第 long_break_interval 轮专注结束后长休 → 回到第 1 轮。
计时权威在服务端，设备只负责渲染：每次相位变化下发一次 self.pomodoro.show，
固件拿到 remaining_s 后本地 1Hz 自减，走到 00:00 就停住等服务端推下一相位。
这样 WiFi 抖动只会让画面短暂不同步，不会让两端的轮次各走各的。

会话按 device_id 存在本模块，不挂在 conn 上：固件断线 10s 就重连、conn 会被换掉，
挂 conn 上的状态在 WiFi 抖动时会静默丢失（同 pushHandle.py 顶部的理由）。
推送时刻一律按 device_id 重新取活跃连接，不用闭包里那个可能已经死掉的 conn。

会话落盘（`pomodoro.persist_path`，默认 data/pomodoro_sessions.json）：每次相位
变迁原子写一次（先写 .tmp 再 rename，同 away_ledger / incident_manager）。
不落盘的代价是真实的——服务端重启后认为没有会话，设备却还停在自己 1Hz 自减的
倒计时画面上，两端各走各的，只能靠用户手动 stop 才收得回来。

截止时刻存的是**墙钟 ISO 时间**，不是 time.monotonic() 的值：monotonic 的原点
每次进程启动都不同，存进文件的那个数字重启后没有任何意义。restore() 把它
换算回本进程的 monotonic 轴：仍在相位内就恢复计时并在设备回连后刷新画面，
已经过期就丢掉会话、等设备回连推一次 idle 收屏。
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


TAG = __name__

# 工具名用下划线版：MCPClient 按 sanitize_tool_name 后的键存查，带点的原名查不到
# （同 pushHandle.ROBOT_ACTION_TOOL）。
POMODORO_SHOW_TOOL = "self_pomodoro_show"

PHASE_FOCUS = "focus"
PHASE_SHORT_BREAK = "short_break"
PHASE_LONG_BREAK = "long_break"
PHASE_IDLE = "idle"

# HTTP / 按键 / 语音三条路径共用的一套命令名
COMMANDS = ("start", "pause", "resume", "toggle", "skip", "stop")

DEFAULT_FOCUS_MINUTES = 25.0
DEFAULT_SHORT_BREAK_MINUTES = 5.0
DEFAULT_LONG_BREAK_MINUTES = 15.0
DEFAULT_LONG_BREAK_INTERVAL = 4

# 相位到点后先庆祝（表情 + 动作 + 提示音），几秒后再把新相位的画面推上去。
# 一并推会让用户还没反应过来就看见下一段倒计时在跑。
DEFAULT_CELEBRATION_DELAY_S = 3.0

# 固件把这三个字段直接当字符串渲染，取值必须落在它支持的表情表里
# （firmware emote_display.cc / emotion_response_controller.cc）。
STATUS_TEXT = "番茄钟"
EMOTION_BREAK = "happy"
EMOTION_FOCUS = "confident"
ACTION_BREAK = "roll"
ACTION_FOCUS = "nod"

# 设备重连后 MCP 客户端要重新握手，这几秒内调用必失败，重试一次通常就能落地
# （与 pushHandle.play_action_on_device 同款）。
SHOW_MAX_ATTEMPTS = 2
SHOW_RETRY_DELAY = 3.0
SHOW_TIMEOUT = 10

# 契约里 remaining_s / total_s 的上限
MAX_PHASE_SECONDS = 86400
# 契约里 round / total_rounds 的上限
MAX_ROUNDS = 99

# 会话落盘位置（config 的 pomodoro.persist_path 可覆盖）
DEFAULT_PERSIST_PATH = "data/pomodoro_sessions.json"

# 恢复出来的画面要等设备回连才推得出去：服务端起来时固件通常还在重连路上
# （断线 10s 重连一次）。等不到就放弃，不能让这条任务永远挂着。
RESTORE_WAIT_SECONDS = 180.0
RESTORE_POLL_INTERVAL = 1.0

# 可恢复的相位。idle 不是会话状态，落盘里出现就是脏数据
RESTORABLE_PHASES = (PHASE_FOCUS, PHASE_SHORT_BREAK, PHASE_LONG_BREAK)


async def _default_push_alert(conn, text: str, **kwargs) -> None:
    from core.handle.pushHandle import push_alert_to_device

    return await push_alert_to_device(conn, text, **kwargs)


async def _default_play_action(conn, action: str) -> bool:
    from core.handle.pushHandle import play_action_on_device

    return await play_action_on_device(conn, action)


async def _default_call_tool(conn, mcp_client, tool_name: str, args: str, timeout: int):
    from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool

    return await call_mcp_tool(conn, mcp_client, tool_name, args, timeout=timeout)


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _clamped_int(value: Any, fallback: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    if parsed < low or parsed > high:
        return fallback
    return parsed


def _phase_seconds(minutes: float) -> float:
    """分钟转秒，并夹到契约允许的区间内。"""
    return max(0.0, min(float(minutes) * 60.0, float(MAX_PHASE_SECONDS)))


def _display_minutes(minutes: float) -> str:
    """播报里的分钟数：25 说成"25"而不是"25.0"，测试用的小数原样保留。"""
    return f"{float(minutes):g}"


@dataclass(frozen=True)
class _Settings:
    focus_minutes: float
    short_break_minutes: float
    long_break_minutes: float
    long_break_interval: int


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _read_settings(config: Optional[dict]) -> _Settings:
    """读 pomodoro 配置段。全部带默认值，用户不写这一段也能跑。"""
    section = (config or {}).get("pomodoro") or {}
    if not isinstance(section, dict):
        section = {}
    return _Settings(
        focus_minutes=_positive_float(
            section.get("focus_minutes"), DEFAULT_FOCUS_MINUTES
        ),
        short_break_minutes=_positive_float(
            section.get("short_break_minutes"), DEFAULT_SHORT_BREAK_MINUTES
        ),
        long_break_minutes=_positive_float(
            section.get("long_break_minutes"), DEFAULT_LONG_BREAK_MINUTES
        ),
        long_break_interval=_clamped_int(
            section.get("long_break_interval"),
            DEFAULT_LONG_BREAK_INTERVAL,
            1,
            MAX_ROUNDS,
        ),
    )


@dataclass
class _Session:
    device_id: str
    settings: _Settings
    focus_minutes: float
    phase: str
    round: int
    total_s: float
    # 暂停时冻结的剩余秒数；运行中只作为 deadline 缺失时的兜底
    remaining_s: float
    # 运行中的到点时刻（time.monotonic 轴）；暂停或转相位窗口内为 None
    deadline: Optional[float]
    paused: bool
    # 计时任务与它的代次。任何状态变更都会 +1，让还没被调度到的旧任务自行退出
    task: Optional[asyncio.Task] = None
    generation: int = 0


class PomodoroManager:
    """番茄钟会话集合。

    推送函数与设备注册表都可注入，便于离线单测；生产用默认实现走 pushHandle 与
    设备 MCP。celebration_delay_s 注入为 0 可以让测试不必真等庆祝窗口。
    wall_clock 返回 datetime，重启恢复按它换算已经过去了多久。
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        device_registry=None,
        *,
        push_alert: Optional[Callable] = None,
        play_action: Optional[Callable] = None,
        call_tool: Optional[Callable] = None,
        celebration_delay_s: float = DEFAULT_CELEBRATION_DELAY_S,
        persist_path: Optional[Any] = None,
        wall_clock: Optional[Callable[[], datetime]] = None,
        logger=None,
    ) -> None:
        self._config = config
        self._device_registry = device_registry
        self._push_alert = push_alert or _default_push_alert
        self._play_action = play_action or _default_play_action
        self._call_tool = call_tool or _default_call_tool
        self._celebration_delay_s = max(0.0, float(celebration_delay_s))
        self._persist_path_override = Path(persist_path) if persist_path else None
        self._wall_clock = wall_clock or datetime.now
        # 模块级默认实例建起来时还没有 loguru 可用，先挂标准库 logger，
        # 等 bind 时换成服务端那个（否则相位切换的日志进不了 tmp/server.log）。
        self._logger = logger or logging.getLogger(__name__)
        self._logger_bound = logger is not None
        self._sessions: Dict[str, _Session] = {}
        # 射后不理的推送任务。事件循环只对任务持弱引用，不留句柄会被 GC 提前回收
        # （同 pushHandle._restore_tasks 的理由），跑完由回调自行摘掉。
        self._push_tasks: set = set()

    # ------------------------------------------------------------ 装配

    def bind(
        self, config: Optional[dict] = None, device_registry=None, logger=None
    ) -> None:
        """补上生产环境的 config、设备注册表与 logger，只填空缺不覆盖已有的。

        模块级默认实例在被谁先用到时才知道这几样东西：HTTP 接口在 SimpleHttpServer
        里接线，语音与按键路径则是第一次收到设备消息时才有 conn 可查。
        """
        if config is not None and self._config is None:
            self._config = config
        if device_registry is not None and self._device_registry is None:
            self._device_registry = device_registry
        if logger is not None and not self._logger_bound:
            self._logger = logger
            self._logger_bound = True

    def bind_from_conn(self, conn) -> None:
        """从一条设备连接上取 config、设备注册表与 logger（语音 / 按键路径用）。"""
        server = getattr(conn, "server", None)
        self.bind(
            config=getattr(conn, "config", None),
            device_registry=getattr(server, "device_registry", None),
            logger=getattr(conn, "logger", None),
        )

    def active_device_ids(self) -> List[str]:
        return list(self._sessions.keys())

    # ------------------------------------------------------------ 落盘

    @property
    def _store_path(self) -> Path:
        if self._persist_path_override is not None:
            return self._persist_path_override
        section = (self._config or {}).get("pomodoro") or {}
        if not isinstance(section, dict):
            section = {}
        return Path(str(section.get("persist_path") or DEFAULT_PERSIST_PATH))

    def _deadline_iso(self, session: _Session) -> Optional[str]:
        """把 monotonic 截止时刻换算成墙钟 ISO 时间。

        暂停中（deadline 为 None）与转相位的庆祝窗口内都返回 None，
        恢复侧按 paused / total_s 走对应分支。
        微秒精度保留：演示时会把相位压到亚秒级，截到秒会让恢复出来的会话直接过期。
        """
        if session.deadline is None:
            return None
        remaining = max(0.0, session.deadline - time.monotonic())
        return (self._wall_clock() + timedelta(seconds=remaining)).isoformat()

    def _session_payload(self, session: _Session) -> Dict[str, Any]:
        return {
            "device_id": session.device_id,
            "phase": session.phase,
            "round": session.round,
            "total_s": session.total_s,
            "remaining_s": self._remaining_seconds(session),
            "paused": bool(session.paused),
            "deadline_at": self._deadline_iso(session),
            "focus_minutes": session.focus_minutes,
            "settings": {
                "focus_minutes": session.settings.focus_minutes,
                "short_break_minutes": session.settings.short_break_minutes,
                "long_break_minutes": session.settings.long_break_minutes,
                "long_break_interval": session.settings.long_break_interval,
            },
            "saved_at": self._wall_clock().isoformat(),
        }

    def _persist(self) -> None:
        """原子写：先写 .tmp 再 rename，进程被 kill 也不会留半截 JSON。

        会话最多几台设备各一条，整份重写比增量便宜也更难写错（同 away_ledger）。
        落盘失败只影响重启后的恢复，绝不能把异常抛给命令路径。
        """
        payload = {
            "version": 1,
            "sessions": [
                self._session_payload(session) for session in self._sessions.values()
            ],
        }
        path = self._store_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(path)
        except Exception as e:
            self._logger.warning(f"番茄钟会话落盘失败: {e}")

    def _load(self) -> List[Dict[str, Any]]:
        path = self._store_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            # 损坏的落盘文件当作没有：宁可丢会话，也不要崩在启动路径上。
            # 后续写入会把文件整体覆盖掉，坏文件不会一直卡着（同 away_ledger）。
            self._logger.warning(f"读取番茄钟会话失败，按空存储处理: {path}")
            return []
        sessions = (data or {}).get("sessions") if isinstance(data, dict) else None
        if not isinstance(sessions, list):
            return []
        return [item for item in sessions if isinstance(item, dict)]

    async def restore(self) -> Dict[str, List[str]]:
        """装载上个进程留下的会话（HTTP 服务启动时调一次）。

        - 仍在相位内：按墙钟重算 monotonic 截止，恢复计时任务，设备回连后刷新画面。
        - 已过期：丢掉会话，设备回连后推一次 idle——设备那张停在 00:00 的
          倒计时画面只有服务端能收回去。
        - 暂停中：恢复为暂停态，冻结的剩余秒数不因重启被扣掉。

        只补空缺：已经跑起来的会话不会被盘上的旧快照覆盖。
        """
        restored: List[str] = []
        expired: List[str] = []
        now = self._wall_clock()

        for payload in self._load():
            device_id = str(payload.get("device_id") or "").strip()
            if not device_id or device_id in self._sessions:
                continue
            session = self._session_from_payload(device_id, payload, now)
            if session is None:
                expired.append(device_id)
                continue
            self._sessions[device_id] = session
            if not session.paused:
                self._start_timer(session)
            restored.append(device_id)
            self._spawn_push(self._show_when_online(device_id))

        for device_id in expired:
            self._spawn_push(self._idle_when_online(device_id))

        if restored or expired:
            # 过期的那几条别留在盘上，否则下次重启还要再走一遍
            self._persist()
            self._logger.info(
                f"番茄钟会话恢复：{len(restored)} 个继续计时，{len(expired)} 个已过期"
            )
        return {"restored": restored, "expired": expired}

    def _session_from_payload(
        self, device_id: str, payload: Dict[str, Any], now: datetime
    ) -> Optional[_Session]:
        """把一条落盘快照还原成会话；已经过期或数据不可用时返回 None。"""
        phase = str(payload.get("phase") or "")
        if phase not in RESTORABLE_PHASES:
            return None

        raw_settings = payload.get("settings")
        if not isinstance(raw_settings, dict):
            raw_settings = {}
        fallback = _read_settings(self._config)
        settings = _Settings(
            focus_minutes=_positive_float(
                raw_settings.get("focus_minutes"), fallback.focus_minutes
            ),
            short_break_minutes=_positive_float(
                raw_settings.get("short_break_minutes"), fallback.short_break_minutes
            ),
            long_break_minutes=_positive_float(
                raw_settings.get("long_break_minutes"), fallback.long_break_minutes
            ),
            long_break_interval=_clamped_int(
                raw_settings.get("long_break_interval"),
                fallback.long_break_interval,
                1,
                MAX_ROUNDS,
            ),
        )

        total_s = max(0.0, min(_positive_float(payload.get("total_s"), 0.0),
                               float(MAX_PHASE_SECONDS)))
        if total_s <= 0:
            return None
        paused = bool(payload.get("paused"))

        if paused:
            remaining = max(
                0.0, min(_positive_float(payload.get("remaining_s"), 0.0), total_s)
            )
            deadline = None
        else:
            deadline_at = _parse_iso(payload.get("deadline_at"))
            if deadline_at is None:
                # 崩在转相位的庆祝窗口里：相位已经切了、截止时刻还没定，
                # 按新相位的整段时长重新开始，比直接丢掉会话保守。
                remaining = total_s
            else:
                remaining = (deadline_at - now).total_seconds()
            if remaining <= 0:
                return None
            deadline = time.monotonic() + remaining

        return _Session(
            device_id=device_id,
            settings=settings,
            focus_minutes=_positive_float(
                payload.get("focus_minutes"), settings.focus_minutes
            ),
            phase=phase,
            round=_clamped_int(payload.get("round"), 1, 1, MAX_ROUNDS),
            total_s=total_s,
            remaining_s=remaining,
            deadline=deadline,
            paused=paused,
        )

    async def _await_device(self, device_id: str):
        """等设备回连。服务端总比设备先起来（固件断线 10s 才重连一次）。"""
        deadline = time.monotonic() + RESTORE_WAIT_SECONDS
        while True:
            conn = self._resolve_conn(device_id)
            if conn is not None:
                return conn
            if time.monotonic() >= deadline:
                self._logger.info(
                    f"设备 {device_id} 在恢复窗口内没有回连，放弃补推番茄钟画面"
                )
                return None
            await asyncio.sleep(RESTORE_POLL_INTERVAL)

    async def _show_when_online(self, device_id: str) -> bool:
        if await self._await_device(device_id) is None:
            return False
        session = self._sessions.get(device_id)
        if session is None:
            # 等待期间用户已经 stop 了，别把陈旧画面补上去（同 _call_show 的理由）
            return False
        return await self._call_show(device_id, self._show_args(session))

    async def _idle_when_online(self, device_id: str) -> bool:
        if await self._await_device(device_id) is None:
            return False
        session = self._sessions.get(device_id)
        if session is not None:
            # 等待期间用户已经对同一设备重新 start 了会话：这条 idle 是给旧的、
            # 已过期的会话收屏的，不该拍飞回连前刚起的新会话。新会话 start() 时的
            # 画面下发因为设备当时离线已经静默失败且不会重试（同 _call_show 的理由），
            # 这里就是回连后唯一能把新会话画面补上去的机会（同 _show_when_online）。
            return await self._call_show(device_id, self._show_args(session))
        await self._push_idle(device_id)
        return True

    def is_focus_active(self, device_id: str) -> bool:
        """该设备是否正处于进行中的专注相位（未暂停）。

        给分心检测的 wants_frame 用：它在推理工作线程里同步调用，不能
        await status()。这里只做字典读与属性读（GIL 下原子），够用且无锁。
        """
        session = self._sessions.get(device_id)
        return (
            session is not None
            and session.phase == PHASE_FOCUS
            and not session.paused
        )

    # ------------------------------------------------------------ 命令

    async def execute(
        self,
        device_id: str,
        command: str,
        focus_minutes: Optional[float] = None,
        feedback: bool = False,
    ) -> Dict[str, Any]:
        """按命令名分发，命令名非法时抛 ValueError（由调用方翻成 400）。"""
        if command == "start":
            return await self.start(device_id, focus_minutes, feedback=feedback)
        if command not in COMMANDS:
            raise ValueError(f"未知命令: {command}")
        return await getattr(self, command)(device_id, feedback=feedback)

    async def start(
        self,
        device_id: str,
        focus_minutes: Optional[float] = None,
        feedback: bool = False,
    ) -> Dict[str, Any]:
        session = self._sessions.get(device_id)
        if session is not None:
            # 已在进行中就别重开，否则用户一句"开始专注"会把跑了 20 分钟的会话清零
            return self._result("already_running", device_id)

        settings = _read_settings(self._config)
        focus = _positive_float(focus_minutes, settings.focus_minutes)
        session = _Session(
            device_id=device_id,
            settings=settings,
            focus_minutes=focus,
            phase=PHASE_FOCUS,
            round=1,
            total_s=_phase_seconds(focus),
            remaining_s=_phase_seconds(focus),
            deadline=None,
            paused=False,
        )
        self._sessions[device_id] = session
        session.deadline = time.monotonic() + session.total_s
        self._start_timer(session)
        self._persist()
        self._logger.info(
            f"设备 {device_id} 开始番茄钟：专注 {_display_minutes(focus)} 分钟，"
            f"共 {settings.long_break_interval} 轮"
        )

        if feedback:
            await self._push_feedback(
                session, f"开始专注{_display_minutes(focus)}分钟", EMOTION_FOCUS
            )
        self._spawn_show(session)
        return self._result("started", device_id)

    async def pause(self, device_id: str, feedback: bool = False) -> Dict[str, Any]:
        session = self._sessions.get(device_id)
        if session is None:
            return self._result("not_running", device_id)

        outcome = "already_paused" if session.paused else "paused"
        if not session.paused:
            session.remaining_s = self._remaining_seconds(session)
            session.deadline = None
            session.paused = True
            self._cancel_timer(session)
            self._persist()
            self._logger.info(f"设备 {device_id} 番茄钟已暂停")

        if feedback:
            await self._push_feedback(session, "番茄钟已暂停", EMOTION_FOCUS)
        self._spawn_show(session)
        return self._result(outcome, device_id)

    async def resume(self, device_id: str, feedback: bool = False) -> Dict[str, Any]:
        session = self._sessions.get(device_id)
        if session is None:
            return self._result("not_running", device_id)

        outcome = "resumed" if session.paused else "already_running"
        if session.paused:
            session.deadline = time.monotonic() + session.remaining_s
            session.paused = False
            self._start_timer(session)
            self._persist()
            self._logger.info(f"设备 {device_id} 番茄钟已继续")

        if feedback:
            await self._push_feedback(session, "番茄钟继续", EMOTION_FOCUS)
        self._spawn_show(session)
        return self._result(outcome, device_id)

    async def toggle(self, device_id: str, feedback: bool = False) -> Dict[str, Any]:
        """按键的三态语义：没会话就开，跑着就暂停，暂停了就继续。"""
        session = self._sessions.get(device_id)
        if session is None:
            return await self.start(device_id, feedback=feedback)
        if session.paused:
            return await self.resume(device_id, feedback=feedback)
        return await self.pause(device_id, feedback=feedback)

    async def skip(self, device_id: str, feedback: bool = False) -> Dict[str, Any]:
        """跳过当前相位。走的是正常的相位到点编排，只是不等计时器。

        转相位本身要庆祝几秒再推画面，直接在这里 await 会把 HTTP 请求拖到超时，
        所以只是把到点时刻提前到现在，剩下的交给计时任务。
        """
        session = self._sessions.get(device_id)
        if session is None:
            return self._result("not_running", device_id)

        session.paused = False
        session.remaining_s = 0.0
        session.deadline = time.monotonic()
        self._start_timer(session)
        self._persist()
        self._logger.info(f"设备 {device_id} 跳过当前番茄钟相位: {session.phase}")
        return self._result("skipped", device_id)

    async def stop(self, device_id: str, feedback: bool = False) -> Dict[str, Any]:
        session = self._sessions.pop(device_id, None)
        outcome = "stopped" if session is not None else "not_running"
        if session is not None:
            self._cancel_timer(session)
            self._persist()
            self._logger.info(f"设备 {device_id} 番茄钟已停止")

        if feedback and session is not None:
            await self._push_feedback_to_device(
                device_id, "番茄钟已停止", EMOTION_FOCUS
            )
        # 没会话也推一次 idle：服务端重启会丢会话，设备却还停在番茄钟画面上，
        # 这时的 stop 就是唯一能把它收回去的手段。
        self._spawn_push(self._push_idle(device_id))
        return self._result(outcome, device_id)

    async def status(self, device_id: str) -> Dict[str, Any]:
        session = self._sessions.get(device_id)
        return self._result("running" if session is not None else "idle", device_id)

    # ------------------------------------------------------------ 计时

    def _start_timer(self, session: _Session) -> None:
        self._cancel_timer(session)
        session.task = asyncio.create_task(
            self._run_phase(session.device_id, session.generation)
        )

    def _cancel_timer(self, session: _Session) -> None:
        """停掉旧计时任务并让它作废。

        只 cancel 不够：任务可能已经从 sleep 醒来、正排队等着往下跑，
        代次自增才能让它在真正动手前发现自己已经过期。
        """
        session.generation += 1
        task = session.task
        session.task = None
        if task is not None and not task.done():
            task.cancel()

    async def _run_phase(self, device_id: str, generation: int) -> None:
        try:
            while True:
                session = self._sessions.get(device_id)
                if session is None or session.generation != generation:
                    return
                if session.deadline is None:
                    return
                delay = session.deadline - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                session = self._sessions.get(device_id)
                if session is None or session.generation != generation:
                    return
                await self._advance(session)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._logger.warning(f"设备 {device_id} 番茄钟计时异常，已停止推进: {e}")

    async def _advance(self, session: _Session) -> None:
        """当前相位到点，庆祝一下再切到下一相位。"""
        phase, round_no, minutes = self._next_phase(session)
        text, emotion, action = self._transition_cue(session, phase, round_no, minutes)

        # 先把会话切到新相位（暂不设到点时刻）：庆祝窗口内被暂停时状态才是自洽的
        session.phase = phase
        session.round = round_no
        session.total_s = _phase_seconds(minutes)
        session.remaining_s = session.total_s
        session.deadline = None
        session.paused = False
        # 庆祝窗口内被 kill 时盘上是「相位已切、截止时刻未定」，
        # restore 会按新相位整段重新开始（比丢掉整个会话保守）
        self._persist()

        conn = self._resolve_conn(session.device_id)
        if conn is not None:
            await self._safe_alert(conn, text, emotion, silent=False)
            await self._safe_action(conn, action)

        if self._celebration_delay_s > 0:
            await asyncio.sleep(self._celebration_delay_s)

        # 倒计时从设备看见画面那一刻算起，别把庆祝的这几秒算进专注时间
        session.deadline = time.monotonic() + session.total_s
        self._persist()
        await self._push_show(session)

    def _next_phase(self, session: _Session):
        """返回下一相位的 (phase, round, minutes)。"""
        settings = session.settings
        if session.phase == PHASE_FOCUS:
            if session.round >= settings.long_break_interval:
                # 长休时 round 记成 total_rounds，设备上的进度点才是满的
                return (
                    PHASE_LONG_BREAK,
                    settings.long_break_interval,
                    settings.long_break_minutes,
                )
            # 短休时 round 停在刚完成的那一轮
            return PHASE_SHORT_BREAK, session.round, settings.short_break_minutes
        if session.phase == PHASE_LONG_BREAK:
            return PHASE_FOCUS, 1, session.focus_minutes
        return PHASE_FOCUS, session.round + 1, session.focus_minutes

    def _transition_cue(
        self, session: _Session, phase: str, round_no: int, minutes: float
    ):
        """转相位时的播报文案 / 表情 / 动作。"""
        if phase == PHASE_SHORT_BREAK:
            return (
                f"专注结束，休息{_display_minutes(minutes)}分钟",
                EMOTION_BREAK,
                ACTION_BREAK,
            )
        if phase == PHASE_LONG_BREAK:
            return (
                f"完成{session.settings.long_break_interval}轮专注，"
                f"长休{_display_minutes(minutes)}分钟",
                EMOTION_BREAK,
                ACTION_BREAK,
            )
        return f"休息结束，第{round_no}轮专注开始", EMOTION_FOCUS, ACTION_FOCUS

    # ------------------------------------------------------------ 推送

    def _resolve_conn(self, device_id: str):
        """按 device_id 取当前活跃连接。取不到说明设备离线，本次推送整体跳过。"""
        if self._device_registry is None:
            return None
        return self._device_registry.get(device_id)

    async def _safe_alert(self, conn, text: str, emotion: str, silent: bool) -> None:
        """推送失败只记日志：设备掉线不该让状态机停摆。"""
        try:
            await self._push_alert(
                conn, text, emotion=emotion, status=STATUS_TEXT, silent=silent
            )
        except Exception as e:
            self._logger.warning(f"番茄钟提示下发失败: {e}")

    async def _safe_action(self, conn, action: str) -> None:
        try:
            await self._play_action(conn, action)
        except Exception as e:
            self._logger.warning(f"番茄钟动作下发失败: {e}")

    async def _push_feedback(self, session: _Session, text: str, emotion: str) -> None:
        await self._push_feedback_to_device(session.device_id, text, emotion)

    async def _push_feedback_to_device(
        self, device_id: str, text: str, emotion: str
    ) -> None:
        """按键路径的有声确认。

        语音路径有 TTS 回复、桌面路径有自己的 UI，都不需要这一条；
        按键则完全没有别的反馈渠道，必须响一声让用户知道按到了。
        """
        conn = self._resolve_conn(device_id)
        if conn is None:
            return
        await self._safe_alert(conn, text, emotion, silent=False)

    async def _push_idle(self, device_id: str) -> None:
        """让设备退出番茄钟画面，回到它原本的模式。"""
        args = {
            "phase": PHASE_IDLE,
            "paused": False,
            "remaining_s": 0,
            "total_s": 0,
            "round": 0,
            "total_rounds": _read_settings(self._config).long_break_interval,
        }
        await self._call_show(device_id, args)

    async def _push_show(self, session: _Session) -> None:
        await self._call_show(session.device_id, self._show_args(session))

    def _spawn_push(self, coro) -> None:
        """把一次推送挂到后台跑，命令路径不等它的结果。

        设备半死时一次 show 最坏要卡 ~23s（两次 10s 超时 + 中间 3s 重试间隔），
        内联 await 会把发起命令的 HTTP 请求或语音回复一起拖住：桌面端 5s 就超时，
        用户以为没按到又按一次，反而把 toggle 按成来回切。
        命令返回的快照只读服务端状态，本来就不依赖推送结果。
        （skip 一直是这个形态，这里让 start/pause/resume/stop 跟它对齐。）
        """
        task = asyncio.create_task(coro)
        self._push_tasks.add(task)
        task.add_done_callback(self._push_tasks.discard)

    def _spawn_show(self, session: _Session) -> None:
        """后台下发当前画面。

        参数在命令时刻就算好，不拖到任务被调度才算：那时会话可能已经被 stop 掉，
        再去读会话就成了给一个已经不存在的番茄钟算画面。
        """
        self._spawn_push(self._call_show(session.device_id, self._show_args(session)))

    async def _call_show(self, device_id: str, args: Dict[str, Any]) -> bool:
        conn = self._resolve_conn(device_id)
        if conn is None:
            self._logger.info(f"设备 {device_id} 不在线，跳过番茄钟画面下发")
            return False

        mcp_client = getattr(conn, "mcp_client", None)
        if mcp_client is None:
            self._logger.warning(f"设备 {device_id} 未初始化 MCP，无法下发番茄钟画面")
            return False

        last_error = None
        for attempt in range(SHOW_MAX_ATTEMPTS):
            try:
                await self._call_tool(
                    conn,
                    mcp_client,
                    POMODORO_SHOW_TOOL,
                    json.dumps(args),
                    SHOW_TIMEOUT,
                )
                return True
            except ValueError as e:
                # call_mcp_tool 用 ValueError 表示"工具不存在 / 参数非法"这类不会自愈的
                # 情况（固件还没上这个工具时就是它）。重试只会白白卡住 3 秒。
                self._logger.warning(f"设备 {device_id} 不支持番茄钟画面工具: {e}")
                return False
            except Exception as e:
                last_error = e
                if attempt >= SHOW_MAX_ATTEMPTS - 1:
                    break
                self._logger.info(
                    f"下发番茄钟画面失败，{SHOW_RETRY_DELAY}s 后重试: {e}"
                )
                await asyncio.sleep(SHOW_RETRY_DELAY)
                # 重连后 conn 会被换掉，重试前重新取一次
                conn = self._resolve_conn(device_id)
                mcp_client = getattr(conn, "mcp_client", None) if conn else None
                if mcp_client is None:
                    break
                if args.get("phase") != PHASE_IDLE:
                    session = self._sessions.get(device_id)
                    if session is None:
                        # 睡这几秒里用户 stop 了。stop 自己已经把 idle 推过去，
                        # 这时再把陈旧的番茄钟画面补上，设备就停在服务端已经不存在的
                        # 会话画面上，只能靠用户再 stop 一次才收得回来。弃推才是安全的。
                        self._logger.info(
                            f"设备 {device_id} 番茄钟已停止，放弃重推陈旧画面"
                        )
                        return False
                    # 这几秒里倒计时还在走，参数得按重试时刻重算
                    args = self._show_args(session)

        self._logger.warning(f"下发番茄钟画面最终失败: {last_error}")
        return False

    # ------------------------------------------------------------ 快照

    def _remaining_seconds(self, session: _Session) -> float:
        if session.deadline is None:
            return max(0.0, session.remaining_s)
        return max(0.0, session.deadline - time.monotonic())

    def _show_args(self, session: _Session) -> Dict[str, Any]:
        return {
            "phase": session.phase,
            "paused": bool(session.paused),
            "remaining_s": _ceil_seconds(self._remaining_seconds(session)),
            "total_s": _ceil_seconds(session.total_s),
            "round": min(session.round, MAX_ROUNDS),
            "total_rounds": session.settings.long_break_interval,
        }

    def _snapshot(self, device_id: str) -> Dict[str, Any]:
        """给 HTTP / 语音看的状态快照。字段顺序对齐三端契约，方便肉眼比对。"""
        session = self._sessions.get(device_id)
        head = {
            "device_id": device_id,
            "connected": self._resolve_conn(device_id) is not None,
            "active": session is not None,
        }
        if session is None:
            head.update(
                {
                    "phase": PHASE_IDLE,
                    "paused": False,
                    "remaining_s": 0,
                    "total_s": 0,
                    "round": 0,
                    "total_rounds": _read_settings(self._config).long_break_interval,
                }
            )
            return head
        head.update(self._show_args(session))
        return head

    def _result(self, outcome: str, device_id: str) -> Dict[str, Any]:
        return {"outcome": outcome, "status": self._snapshot(device_id)}


def _ceil_seconds(seconds: float) -> int:
    """秒数向上取整：刚下发时显示 25:00 而不是 24:59。"""
    return max(0, min(int(math.ceil(seconds - 1e-6)), MAX_PHASE_SECONDS))


# 模块级默认实例。HTTP 接口在 SimpleHttpServer 里接线，语音与按键路径
# 用 bind_from_conn 就地补上 config 与设备注册表。
pomodoro_manager = PomodoroManager()
