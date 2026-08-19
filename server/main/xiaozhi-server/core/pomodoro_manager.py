"""番茄钟状态机与设备推送编排。

经典循环：专注 → 短休 → …… → 第 long_break_interval 轮专注结束后长休 → 回到第 1 轮。
计时权威在服务端，设备只负责渲染：每次相位变化下发一次 self.pomodoro.show，
固件拿到 remaining_s 后本地 1Hz 自减，走到 00:00 就停住等服务端推下一相位。
这样 WiFi 抖动只会让画面短暂不同步，不会让两端的轮次各走各的。

会话按 device_id 存在本模块，不挂在 conn 上：固件断线 10s 就重连、conn 会被换掉，
挂 conn 上的状态在 WiFi 抖动时会静默丢失（同 pushHandle.py 顶部的理由）。
推送时刻一律按 device_id 重新取活跃连接，不用闭包里那个可能已经死掉的 conn。

服务端不做持久化：进程重启会丢掉所有会话，只记日志。
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
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
        logger=None,
    ) -> None:
        self._config = config
        self._device_registry = device_registry
        self._push_alert = push_alert or _default_push_alert
        self._play_action = play_action or _default_play_action
        self._call_tool = call_tool or _default_call_tool
        self._celebration_delay_s = max(0.0, float(celebration_delay_s))
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
        self._logger.info(f"设备 {device_id} 跳过当前番茄钟相位: {session.phase}")
        return self._result("skipped", device_id)

    async def stop(self, device_id: str, feedback: bool = False) -> Dict[str, Any]:
        session = self._sessions.pop(device_id, None)
        outcome = "stopped" if session is not None else "not_running"
        if session is not None:
            self._cancel_timer(session)
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

        conn = self._resolve_conn(session.device_id)
        if conn is not None:
            await self._safe_alert(conn, text, emotion, silent=False)
            await self._safe_action(conn, action)

        if self._celebration_delay_s > 0:
            await asyncio.sleep(self._celebration_delay_s)

        # 倒计时从设备看见画面那一刻算起，别把庆祝的这几秒算进专注时间
        session.deadline = time.monotonic() + session.total_s
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
