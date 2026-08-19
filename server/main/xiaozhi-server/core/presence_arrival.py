"""在岗状态到机器人表演的编排。

presence-agent 把工位在岗与本人识别结果 POST 给 Server，此前 Server 收下后只存进
内存字典，没有任何消费方（AGENTS.md「当前尚未接通的地方」第 3 条）。本模块就是那个
消费方：主人到岗时让机器人抬头、露出笑脸并开口问好，工位持续无人时让它低头休眠。

上报只在状态变化时发送，另有每 15 秒一条 reason=heartbeat 的心跳，所以这里收到的是
变化事件流而不是帧流，去重只需按「到岗周期」而不必按帧节流。

设计见 docs/superpowers/specs/2026-08-18-presence-arrival-orchestration-design.md
"""

from dataclasses import dataclass, field
import logging
import time
from typing import Any, Callable, Optional

from core.presence_registry import PresenceReport


TAG = __name__

# identity 收敛到这几个状态才算「认得清楚了」，可以据此决定说哪句问候。
# starting 是人脸校验尚未出结果，no_face 是这一帧没看到脸（人可能只是低头），
# 两者都还会变，过早下结论会把主人当成陌生人。
SETTLED_IDENTITY_STATES = frozenset(
    {"owner", "unknown", "not_enrolled", "multiple_faces"}
)

# starting 是 agent 还没判定，camera_error 说明摄像头本身有问题，
# 这两种状态下的在岗结论都不可信，一律不驱动机器人。
IGNORED_PRESENCE_STATES = frozenset({"starting", "camera_error"})

# 识别结果确定是「不是主人」的那两种。not_enrolled（底片没录过）与识别超时
# 都只说明认不准，不足以断定来了个访客，因此不进来访者流程。
VISITOR_IDENTITY_STATES = frozenset({"unknown", "multiple_faces"})

DEFAULT_IDENTITY_WAIT_SECONDS = 5.0
DEFAULT_ABSENT_GRACE_SECONDS = 90.0
# 语音对话活跃的回看窗口:宽限期满时,设备上正在播报/正在拾音/最近这么多秒内
# 说过话/对话窗口开着,都算「人还在」,推迟离席确认并重置计时。低头凑近机器人
# 说话时姿态关键点会丢(is_present 要求肩膀可见),摄像头的 absent 在这种场景
# 不可信;真机 8-19 实录:45 秒宽限期内喊了三次唤醒词照样被判「持续无人」,
# 休眠画面把聆听画面顶掉,用户误以为麦克风被关了。
DEFAULT_VOICE_ACTIVE_SECONDS = 60.0
# 迎接冷却:两次问候之间的最小间隔。姿态检测在用户坐得近/角度偏时会丢关键点,
# present/absent 秒级抖动 + 宽限期一过就复位迎接标记,真机上出现过几分钟内
# 连续迎接十几次的"复读机"。冷却期内被复位也不再出声;返岗汇总不受冷却影响,
# 有待播报事项时仍然送达(只是不再重复"早上好")。
DEFAULT_GREETING_COOLDOWN_SECONDS = 180.0
DEFAULT_GREETING_OWNER = "早上好，今天也一起把事情搞定吧。"
DEFAULT_GREETING_GENERIC = "你好，我在这儿。"
DEFAULT_GREETING_EMOTION = "happy"
DEFAULT_GREETING_STATUS = "在岗"
DEFAULT_SLEEP_TEXT = "工位没人，我先眯一会儿。"
DEFAULT_SLEEP_EMOTION = "sleepy"
DEFAULT_SLEEP_STATUS = "休眠"


async def _default_push_event(conn, text: str, **kwargs) -> bool:
    from core.handle.pushHandle import push_work_event

    return await push_work_event(conn, text=text, **kwargs)


async def _default_push_alert(conn, text: str, **kwargs) -> None:
    from core.handle.pushHandle import push_alert_to_device

    return await push_alert_to_device(conn, text=text, **kwargs)


def _default_set_base_state(device_id: str, status: str, message: str, emotion: str) -> None:
    from core.handle.pushHandle import set_base_state

    set_base_state(device_id, status, message, emotion)


def _default_voice_active_probe(conn, within_seconds: float) -> bool:
    from core.handle.pushHandle import voice_session_active

    return voice_session_active(conn, within_seconds)


def _join_sentences(first: str, second: str) -> str:
    """迎接语 + 返岗汇总拼成一条播报，缺句号就补一个，避免两句黏成一坨。"""
    first = first.rstrip()
    if first and first[-1] not in "。！？!?.…":
        first += "。"
    return first + second


@dataclass
class _WorkstationState:
    """单个工位的到岗周期状态。"""

    greeted: bool = False
    asleep: bool = False
    present_since: Optional[float] = None
    absent_since: Optional[float] = None
    # 最近一次成功问候的时刻(clock 轴);迎接冷却的锚点
    last_greeted_at: Optional[float] = None


@dataclass(frozen=True)
class _Settings:
    workstations: dict
    identity_wait_seconds: float
    absent_grace_seconds: float
    voice_active_seconds: float
    greeting_cooldown_seconds: float
    sleep_on_absent: bool
    greeting_owner: str
    greeting_generic: str
    greeting_emotion: str
    greeting_status: str
    sleep_text: str
    sleep_emotion: str
    sleep_status: str


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _read_settings(config: dict) -> Optional[_Settings]:
    """读配置段；未启用或没有可用工位映射时返回 None。"""
    section = (config or {}).get("presence_robot") or {}
    if not isinstance(section, dict) or not section.get("enabled", False):
        return None

    raw = section.get("workstations") or {}
    if not isinstance(raw, dict):
        return None
    workstations = {
        str(key): str(value)
        for key, value in raw.items()
        if str(key).strip() and str(value).strip()
    }
    if not workstations:
        return None

    return _Settings(
        workstations=workstations,
        identity_wait_seconds=_positive_float(
            section.get("identity_wait_seconds"), DEFAULT_IDENTITY_WAIT_SECONDS
        ),
        absent_grace_seconds=_positive_float(
            section.get("absent_grace_seconds"), DEFAULT_ABSENT_GRACE_SECONDS
        ),
        voice_active_seconds=_positive_float(
            section.get("voice_active_seconds"), DEFAULT_VOICE_ACTIVE_SECONDS
        ),
        greeting_cooldown_seconds=_positive_float(
            section.get("greeting_cooldown_seconds"),
            DEFAULT_GREETING_COOLDOWN_SECONDS,
        ),
        sleep_on_absent=bool(section.get("sleep_on_absent", True)),
        greeting_owner=str(section.get("greeting_owner") or DEFAULT_GREETING_OWNER),
        greeting_generic=str(
            section.get("greeting_generic") or DEFAULT_GREETING_GENERIC
        ),
        greeting_emotion=str(
            section.get("greeting_emotion") or DEFAULT_GREETING_EMOTION
        ),
        greeting_status=str(section.get("greeting_status") or DEFAULT_GREETING_STATUS),
        sleep_text=str(section.get("sleep_text") or DEFAULT_SLEEP_TEXT),
        sleep_emotion=str(section.get("sleep_emotion") or DEFAULT_SLEEP_EMOTION),
        sleep_status=str(section.get("sleep_status") or DEFAULT_SLEEP_STATUS),
    )


class PresenceArrivalOrchestrator:
    """把在岗状态变化翻译成机器人指令。

    推送/基态函数与时钟都可注入，便于离线单测；生产用默认实现走 pushHandle。

    away_ledger / visitor_flow / owner_status_store 是流程四/五接进来的可选依赖，
    三个都缺省 None 时行为与接入前完全一致（只做迎接与休眠）。
    """

    def __init__(
        self,
        config: dict,
        device_registry,
        *,
        push_event: Optional[Callable] = None,
        push_alert: Optional[Callable] = None,
        set_base_state: Optional[Callable] = None,
        voice_active_probe: Optional[Callable] = None,
        clock: Optional[Callable[[], float]] = None,
        logger=None,
        settings: Optional[_Settings] = None,
        owner_status_store=None,
        away_ledger=None,
        visitor_flow=None,
    ) -> None:
        self._settings = settings or _read_settings(config)
        if self._settings is None:
            raise ValueError("presence_robot 未启用或没有配置任何工位映射")
        self._device_registry = device_registry
        self._push_event = push_event or _default_push_event
        self._push_alert = push_alert or _default_push_alert
        self._set_base_state = set_base_state or _default_set_base_state
        self._voice_active_probe = voice_active_probe or _default_voice_active_probe
        self._clock = clock or time.monotonic
        self._logger = logger or logging.getLogger(__name__)
        self._states: dict[str, _WorkstationState] = {}
        self._owner_status_store = owner_status_store
        self._away_ledger = away_ledger
        self._visitor_flow = visitor_flow

    def _state_for(self, workstation_id: str) -> _WorkstationState:
        state = self._states.get(workstation_id)
        if state is None:
            state = _WorkstationState()
            self._states[workstation_id] = state
        return state

    async def on_report(self, report: PresenceReport, acceptance: dict) -> None:
        """每条被 registry 接受的上报调用一次。

        本方法自行吞掉所有异常：它挂在 /xiaozhi/presence/report 的处理路径上，
        编排失败不该把上报接口变成 500。
        """
        try:
            await self._dispatch(report, acceptance)
        except Exception:
            self._logger.exception("在岗编排处理失败，已忽略本次上报")

    async def _dispatch(self, report: PresenceReport, acceptance: dict) -> None:
        # 同一 event_id 重发只是网络重试，不该再问好一次
        if (acceptance or {}).get("duplicate"):
            return

        device_id = self._settings.workstations.get(report.workstation_id)
        if not device_id:
            # 一台 Server 可能接多个工位，其中只有部分配了机器人
            return

        if report.state in IGNORED_PRESENCE_STATES:
            return

        state = self._state_for(report.workstation_id)
        now = self._clock()

        if report.state == "present":
            await self._on_present(report, state, device_id, now)
        elif report.state == "absent":
            await self._on_absent(state, device_id, now)

    async def _on_present(
        self,
        report: PresenceReport,
        state: _WorkstationState,
        device_id: str,
        now: float,
    ) -> None:
        state.absent_since = None
        if state.present_since is None:
            state.present_since = now
        if state.greeted:
            return

        greeting = self._choose_greeting(report, state, now)
        if greeting is None:
            # identity 还没收敛：既不问好也不当访客，等下一条上报
            return

        identity_state = (report.identity or {}).get("state")
        if (
            self._visitor_flow is not None
            and identity_state in VISITOR_IDENTITY_STATES
            and self._owner_is_out()
        ):
            # 流程四：主人不在时来的是访客，走应答+留言，不走迎接。
            # 刻意不置 greeted——访客还站在工位前时主人回来，仍然要被迎接。
            await self._handle_visitor(report.workstation_id)
            return

        # 认清来人后立刻注册「在岗」基态。wellbeing/审批/告警/晨报都带 restore_after,
        # 基态不注册的话,任何一条提醒播完画面都回落默认「待机」——人在工位,
        # 屏幕却写着待机(设计文档 §4.4:迎接后设备应保持醒着的状态)。
        # 基态按 device_id 存、与连接无关,设备离线也要注册,上线后第一次恢复就是对的;
        # 也必须先于冷却分支——静默返岗不出声,但基态照样要切回来。
        # emotion 用 neutral:基态恢复是静默收画面,不该每次都触发表情舵机动作。
        self._set_base_state(device_id, self._settings.greeting_status, "", "neutral")

        is_owner = identity_state == "owner"
        # 流程五：返岗汇总接在迎接语后面，一条推送说完，不拆成两次播报
        summary = self._compose_away_summary() if is_owner else None

        # 迎接冷却:姿态抖动会让宽限期虚过、迎接标记被复位,没有冷却就会
        # 循环打招呼。冷却期内:无汇总 → 静默重挂标记;有汇总 → 只播汇总。
        in_cooldown = (
            state.last_greeted_at is not None
            and (now - state.last_greeted_at)
            < self._settings.greeting_cooldown_seconds
        )
        if in_cooldown:
            if summary is None:
                state.greeted = True
                state.asleep = False
                return
            greeting = summary
        elif summary:
            greeting = _join_sentences(greeting, summary)

        conn = self._resolve_conn(device_id)
        if conn is None:
            # 设备还没连上来。不置已问候标记，等它上线后的下一条上报再补这次迎接。
            self._logger.info(
                f"工位 {report.workstation_id} 有人到岗，"
                f"但设备 {device_id} 不在线，跳过迎接"
            )
            return

        # 先置位再 await：两条上报交错时不会都判定为「该问好」
        state.greeted = True
        state.asleep = False
        try:
            await self._push_event(
                conn,
                text=greeting,
                emotion=self._settings.greeting_emotion,
                status=self._settings.greeting_status,
                speak=True,
            )
        except Exception:
            # 回滚标记，让下一条上报还能再试一次，否则一次抖动就永久丢掉这次迎接
            state.greeted = False
            raise
        state.last_greeted_at = now
        if is_owner:
            # 只有推送成功之后才清账：先清后推的话，一次断线就把同事的留言吞了
            self._settle_return(reported=bool(summary))
        self._logger.info(
            f"工位 {report.workstation_id} 到岗，已让设备 {device_id} 迎接：{greeting}"
        )

    # ---------- 流程四/五：离席台账与来访者 ----------

    def _owner_is_out(self) -> bool:
        """主人是不是「不在」：声明状态非在岗，或摄像头已确认离席。

        两个来源都要看——主人可能只是走开没声明（台账说了算），
        也可能人还在楼里但已声明会议中（声明状态说了算）。
        """
        ledger = self._away_ledger
        if ledger is not None:
            try:
                if ledger.is_away():
                    return True
            except Exception as e:
                self._logger.warning(f"读取离席台账失败，按主人在岗处理: {e}")
        store = self._owner_status_store
        if store is not None:
            try:
                return str(store.get().get("state") or "") != "available"
            except Exception as e:
                self._logger.warning(f"读取主人状态失败，按主人在岗处理: {e}")
        return False

    async def _handle_visitor(self, workstation_id: str) -> None:
        try:
            await self._visitor_flow.on_visitor_detected(workstation_id)
        except Exception as e:
            self._logger.warning(f"来访者应答失败，已忽略本次上报: {e}")

    def _compose_away_summary(self) -> Optional[str]:
        ledger = self._away_ledger
        if ledger is None:
            return None
        try:
            return ledger.compose_speech()
        except Exception as e:
            self._logger.warning(f"组装返岗汇总失败，本次只做迎接: {e}")
            return None

    def _settle_return(self, *, reported: bool) -> None:
        """主人已被迎接：念过的清账，离席窗口无论有没有汇总都要关。

        没有待播报事项也要 mark_returned，否则 is_away() 一直为真，
        来访者流程会把主人自己当成访客。
        """
        ledger = self._away_ledger
        if ledger is None:
            return
        try:
            if reported:
                ledger.mark_reported()
            ledger.mark_returned()
        except Exception as e:
            self._logger.warning(f"返岗台账收尾失败: {e}")

    def _begin_away_window(self) -> None:
        """确认离席后开始攒返岗汇总。

        先问 is_away() 再标记：离席心跳 15 秒一条，无条件调用会让台账
        每条心跳都重写一次落盘文件。
        """
        ledger = self._away_ledger
        if ledger is None:
            return
        try:
            if not ledger.is_away():
                ledger.mark_away()
        except Exception as e:
            self._logger.warning(f"标记离席失败: {e}")

    def _choose_greeting(
        self, report: PresenceReport, state: _WorkstationState, now: float
    ) -> Optional[str]:
        """返回该说的问候语；identity 还没收敛且未超时则返回 None 表示继续等。"""
        identity_state = (report.identity or {}).get("state")
        if identity_state in SETTLED_IDENTITY_STATES:
            if identity_state == "owner":
                return self._settings.greeting_owner
            # 非本人一律通用问候：不喊姓名、不带任何个人信息
            return self._settings.greeting_generic

        waited = now - (state.present_since or now)
        if waited < self._settings.identity_wait_seconds:
            return None
        # 等不到确定结果就按不确定处理，宁可说通用问候也不要喊错人
        return self._settings.greeting_generic

    async def _on_absent(
        self, state: _WorkstationState, device_id: str, now: float
    ) -> None:
        state.present_since = None
        if state.absent_since is None:
            state.absent_since = now
        if now - state.absent_since < self._settings.absent_grace_seconds:
            # 宽限期内：可能只是弯腰捡东西，别急着睡，也别复位到岗标记
            return

        # 摄像头说没人，但设备上的语音对话还活跃：低头凑近机器人说话时姿态
        # 关键点会丢，这种 absent 不可信。把语音活动当在场证据、重置离席计时，
        # 否则休眠画面会顶掉聆听画面（用户误以为麦克风被关了），台账还会把
        # 人在场的时段记成「离席期间」、回归时再错误地重新迎接一遍。
        conn = self._resolve_conn(device_id)
        if conn is not None and self._voice_is_active(conn):
            state.absent_since = now
            self._logger.info(
                f"设备 {device_id} 语音对话仍活跃，摄像头离席判定已推迟"
            )
            return

        # 确认是真的离开了，复位到岗周期，下次回来重新迎接
        state.greeted = False
        # 流程五：从这一刻起的事件才算「离席期间发生的」，回来要汇总
        self._begin_away_window()

        # 基态跟着离席走:睡则常驻休眠画面(sleepy 让离席期间的告警播完后
        # 顺带低头回到睡姿),不睡则传空值回落 pushHandle 的默认待机。
        # 不用 clear_base_state——它会顺带取消进行中的恢复任务,离席期间的
        # 告警画面会被永久钉在屏上;set 是幂等写,心跳每 15 秒重走这里也无害。
        if self._settings.sleep_on_absent:
            self._set_base_state(
                device_id,
                self._settings.sleep_status,
                self._settings.sleep_text,
                self._settings.sleep_emotion,
            )
        else:
            self._set_base_state(device_id, "", "", "")

        if not self._settings.sleep_on_absent or state.asleep:
            return

        conn = self._resolve_conn(device_id)
        if conn is None:
            return

        state.asleep = True
        try:
            await self._push_alert(
                conn,
                text=self._settings.sleep_text,
                emotion=self._settings.sleep_emotion,
                status=self._settings.sleep_status,
                silent=True,
            )
        except Exception:
            state.asleep = False
            raise
        self._logger.info(f"工位持续无人，设备 {device_id} 已进入休眠")

    def _voice_is_active(self, conn) -> bool:
        try:
            return bool(
                self._voice_active_probe(conn, self._settings.voice_active_seconds)
            )
        except Exception as e:
            # 探针坏了不能让设备永远睡不着，按无语音处理
            self._logger.warning(f"语音活跃探测失败，按无语音处理: {e}")
            return False

    def _resolve_conn(self, device_id: str):
        if self._device_registry is None:
            return None
        return self._device_registry.get(device_id)


def create_presence_arrival_orchestrator(
    config: dict, device_registry, **kwargs
) -> Optional[PresenceArrivalOrchestrator]:
    """按配置装配编排器；未启用、无工位映射或拿不到设备注册表时返回 None。

    device_registry 来自 WebSocket 服务，presence_server.py 那个轻量入口没有它，
    此时不装配，presence 接口行为保持不变。
    """
    if device_registry is None:
        return None
    settings = _read_settings(config)
    if settings is None:
        return None
    return PresenceArrivalOrchestrator(
        config, device_registry, settings=settings, **kwargs
    )
