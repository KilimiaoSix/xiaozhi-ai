"""来访者应答与留言流程（需求文档流程四）。

主人不在工位时有人靠近：机器人先说主人的**公开**状态与预计返回时间，再问
「需要帮你留句话吗？」；对方说了内容就记进离席台账，说「不用」就不留痕。

三条会真的绊倒人的约束：

1. **只说公开信息。** 会议主题、请假原因不出 owner_status，这里也只用
   state / expected_return 拼固定模板，绝不把 owner_status 的其它字段念出去。
2. **防抖。** presence 上报在人站着不动时也会持续来（心跳 15 秒一条），
   没有冷却期的话来访者会被同一句话反复轰炸。冷却只在推送成功后才计，
   设备不在线的那一次根本没发生，不能把冷却用掉。
3. **留言窗口是有界的。** 播报成功后开一个窗口（默认 90 秒），只有窗口内
   这台设备的下一句 ASR 才当留言；否则主人自己回来说的话会被记成留言。
4. **候选留言要先过噪声过滤，入账要带确认复述。** 真机实锤过：设备在
   Listening 态下唤醒模型是关的，一句被 ASR 误转写的唤醒词（「你好么办」
   之类）会被当场原样记成留言、无任何确认。所以候选文本先归一（复用
   dialogue_gate._normalize，同音混淆表也一并生效），命中唤醒词表或归一后
   不足 4 个字就判定为噪声：不入账，改口再问一次；第二次还是噪声就礼貌
   收场，不问第三次。真正入账后的应答也不再是固定的「记下了」，而是带上
   留言内容复述一遍，播报超长时截断（台账仍存全文），让主人和来访者都能
   当场确认记的是不是那句话。

handle_asr_text 返回要说的回复语，**不负责推送**——集成方（startToChat）
拿到字符串后自己走 push_work_event，避免本模块和会话生命周期耦合。
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

TAG = __name__

DEFAULT_MESSAGE_WINDOW_S = 90.0
DEFAULT_REGREET_COOLDOWN_S = 180.0
DEFAULT_STATUS = "有人来访"
DEFAULT_EMOTION = "neutral"
DEFAULT_ACTION = "center"

# 来访应答文案。全部是固定模板，唯一的变量是预计返回时刻。
TEXT_MEETING_WITH_TIME = "他正在开会，预计{time}回来。需要帮你留句话吗？"
TEXT_MEETING = "他正在开会，暂时不在工位。需要帮你留句话吗？"
# 请假文案带的是「可公开的返回日期」（假期结束次日），不是请假原因——
# 需求原话：请假期间只说明"不在岗"和可公开的返回时间，不播报请假原因
TEXT_LEAVE_WITH_DATE = "他请假了，预计{date}回来。有重要的事我可以帮你记下来。"
TEXT_LEAVE = "他今天请假，不在工位。有重要的事我可以帮你记下来。"
TEXT_AWAY_WITH_TIME = "他暂时不在工位，预计{time}回来。需要帮你留句话吗？"
TEXT_GENERIC = "他暂时不在工位，需要帮你留句话吗？"

REPLY_DECLINED = "好的，他回来我就说你来过。"
# 入账成功的应答带确认复述，不再是固定的「记下了」——让来访者当场确认
# 记的是不是那句话；brief 超长时会被截断（见 BROADCAST_TRUNCATE_CHARS）。
REPLY_RECORDED_TEMPLATE = "记下了：{brief}。他回来我就转达"
# 疑似噪声（唤醒词误转写/超短句）时的再问文案，风格与其它访客话术一致
REPLY_ASK_AGAIN = "不好意思没听清，你要带的话是？"

# 来访者身份未知，一律记成「同事」——不能凭空给留言安一个名字
VISITOR_MESSAGE_PREFIX = "有同事留言："

# 候选留言归一（同 dialogue_gate._normalize 口径）后不足这个字数，
# 大概率是「嗯」「啊」这类无意义音节或误转写残片，不值得当场记下来。
NOISE_MIN_CHARS = 4
# 确认复述播报的长度上限：台账永远存全文，只有念给来访者/主人听的这句会截断。
BROADCAST_TRUNCATE_CHARS = 40

# 否定词只在短句里生效：「没有别的事，就是日志方案已经发飞书了」里的「没有」
# 是内容不是拒绝，按包含匹配会把整条留言吞掉。
NEGATIVE_PREFIXES = ("不用", "不需要", "没事", "不了", "没有")
NEGATIVE_MAX_CHARS = 8
_PUNCTUATION = "。，！？、,.!?~… \t\r\n　"


async def _default_push_event(conn, text: str, **kwargs) -> bool:
    from core.handle.pushHandle import push_work_event

    return await push_work_event(conn, text=text, **kwargs)


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _format_expected_return(value: Any) -> Optional[str]:
    """把 expected_return 读成 HH:MM；解析不了就返回 None，退回不带时间的说法。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).strftime("%H:%M")
    except (TypeError, ValueError):
        return None


def _format_leave_return_date(leave_end: Any) -> Optional[str]:
    """把 leave_end（含当天）读成假期结束次日的「M月D日」；解析不了返回 None。"""
    if not leave_end:
        return None
    try:
        back = date.fromisoformat(str(leave_end)) + timedelta(days=1)
    except (TypeError, ValueError):
        return None
    return f"{back.month}月{back.day}日"


def _is_decline(text: str) -> bool:
    stripped = text.strip().strip(_PUNCTUATION)
    normalized = "".join(ch for ch in stripped if ch not in _PUNCTUATION)
    if not normalized:
        return False
    if len(normalized) > NEGATIVE_MAX_CHARS:
        return False
    return normalized.startswith(NEGATIVE_PREFIXES)


def make_registry_device_resolver(config: dict, device_registry) -> Callable:
    """按 presence_robot.workstations 把工位映射到在线设备。

    返回 (device_id, conn)；工位没配机器人或设备不在线时返回 None——
    后者必须区分出来，否则来访应答会被记成「已播报」而实际没人听见。
    """
    workstations = ((config or {}).get("presence_robot") or {}).get("workstations") or {}
    mapping = {str(key): str(value) for key, value in workstations.items()}

    def resolve(workstation_id: str):
        device_id = mapping.get(str(workstation_id))
        if not device_id or device_registry is None:
            return None
        conn = device_registry.get(device_id)
        if conn is None:
            return None
        return device_id, conn

    return resolve


class VisitorFlow:
    """来访者应答 + 留言窗口。

    owner_status / away_ledger / push / 设备解析 / 时钟全部可注入，便于离线单测；
    生产用默认实现走 pushHandle 与各自的单例。
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        *,
        owner_status_store=None,
        away_ledger=None,
        push_event: Optional[Callable] = None,
        device_resolver: Optional[Callable] = None,
        clock: Optional[Callable[[], datetime]] = None,
        logger=None,
        message_window_s: Optional[float] = None,
        regreet_cooldown_s: Optional[float] = None,
        status: Optional[str] = None,
    ) -> None:
        section = (config or {}).get("visitor_flow") or {}
        if not isinstance(section, dict):
            section = {}

        if owner_status_store is None:
            from core.owner_status import get_owner_status_store

            owner_status_store = get_owner_status_store(config)
        if away_ledger is None:
            from core.away_ledger import get_away_ledger

            away_ledger = get_away_ledger(config)

        # 噪声过滤复用 dialogue_gate 的归一与唤醒词口径，直接 import 用，不复制实现。
        from core.dialogue_gate import DialogueGate, _normalize

        self._owner_status = owner_status_store
        self._ledger = away_ledger
        self._normalize = _normalize
        self._wake_words = DialogueGate._collect_wake_words(config or {})
        self._push_event = push_event or _default_push_event
        self._device_resolver = device_resolver
        self._clock = clock or datetime.now
        self._logger = logger or logging.getLogger(__name__)
        self._status = str(status or section.get("status") or DEFAULT_STATUS)
        self._message_window_s = _positive_float(
            message_window_s
            if message_window_s is not None
            else section.get("message_window_s"),
            DEFAULT_MESSAGE_WINDOW_S,
        )
        self._regreet_cooldown_s = _positive_float(
            regreet_cooldown_s
            if regreet_cooldown_s is not None
            else section.get("regreet_cooldown_s"),
            DEFAULT_REGREET_COOLDOWN_S,
        )
        self._lock = threading.RLock()
        # workstation_id -> 上次成功播报时刻，用于同一来访片段的防抖
        self._last_greeted: dict[str, datetime] = {}
        # device_id -> 留言窗口关闭时刻
        self._windows: dict[str, datetime] = {}
        # device_id -> 已经用过一次「疑似噪声，再问一次」的机会；
        # 命中过一次的设备下次再判定为噪声就直接收场，不再问第三次。
        self._noise_retry_used: set[str] = set()

    # ---------- 流程四·应答 ----------

    async def on_visitor_detected(self, workstation_id: str) -> bool:
        """有非本人靠近工位时调用；返回是否真的播报了。

        由 presence 编排调用，异常自行吞掉：它挂在上报处理路径上，
        应答失败不该把 /xiaozhi/presence/report 变成 500。
        """
        try:
            return await self._announce(workstation_id)
        except Exception:
            self._logger.exception("来访者应答失败，已忽略本次上报")
            return False

    async def _announce(self, workstation_id: str) -> bool:
        now = self._clock()
        with self._lock:
            last = self._last_greeted.get(workstation_id)
            if last is not None and (now - last).total_seconds() < self._regreet_cooldown_s:
                # 同一个来访片段：人还站在那儿，上报还在来，但话已经说过了
                return False

        if self._device_resolver is None:
            return False
        resolved = self._device_resolver(workstation_id)
        if not resolved:
            # 设备不在线：这次应答根本没发生，不记冷却也不开留言窗口，
            # 否则设备上线后这位来访者永远等不到回应。
            self._logger.info(f"工位 {workstation_id} 有人来访，但设备不在线，跳过应答")
            return False
        device_id, conn = resolved

        text = self._compose_announcement()
        try:
            await self._push_event(
                conn,
                text=text,
                emotion=DEFAULT_EMOTION,
                status=self._status,
                speak=True,
                action=DEFAULT_ACTION,
            )
        except Exception as e:
            # 没说出口就不能记冷却，下一条上报还要再试一次
            self._logger.warning(f"来访应答推送失败，将在下次上报重试: {e}")
            return False

        with self._lock:
            self._last_greeted[workstation_id] = now
            # 只有话真说出口了才开留言窗口
            self._windows[device_id] = now + timedelta(seconds=self._message_window_s)
            # 新一轮来访播报，清掉上一轮可能残留的噪声重试标记
            self._noise_retry_used.discard(device_id)
        self._logger.info(f"工位 {workstation_id} 有人来访，已应答：{text}")
        return True

    def _compose_announcement(self) -> str:
        """只用 state 与 expected_return 拼固定模板，绝不外泄私密信息。"""
        try:
            status = self._owner_status.get() if self._owner_status else {}
        except Exception:
            self._logger.warning("读取主人状态失败，来访应答退回通用文案")
            status = {}

        state = str(status.get("state") or "")
        expected = _format_expected_return(status.get("expected_return"))

        if state == "meeting":
            if expected:
                return TEXT_MEETING_WITH_TIME.format(time=expected)
            return TEXT_MEETING
        if state == "leave":
            back = _format_leave_return_date(status.get("leave_end"))
            if back:
                return TEXT_LEAVE_WITH_DATE.format(date=back)
            return TEXT_LEAVE
        if expected:
            return TEXT_AWAY_WITH_TIME.format(time=expected)
        return TEXT_GENERIC

    # ---------- 流程四·留言 ----------

    def handle_asr_text(self, device_id: str, text: str) -> Optional[str]:
        """窗口开着时消费这句 ASR，返回该说的回复语；窗口没开返回 None。

        返回 None 表示「这句话不归我管」，集成方应把它照常送进对话链路。
        """
        if not device_id:
            return None
        content = str(text or "").strip()

        now = self._clock()
        with self._lock:
            expires = self._windows.get(device_id)
            if expires is None:
                return None
            if now >= expires:
                # 窗口过期：来访者转身走了，之后这台设备听到的是别人的话
                self._windows.pop(device_id, None)
                self._noise_retry_used.discard(device_id)
                return None
            if not content:
                # 空识别结果不算一次回答，窗口继续开着等真正那句
                return None
            self._windows.pop(device_id, None)
            already_retried = device_id in self._noise_retry_used

        if _is_decline(content):
            with self._lock:
                self._noise_retry_used.discard(device_id)
            self._logger.info("来访者谢绝留言，不入账")
            return REPLY_DECLINED

        if self._is_noise(content):
            if already_retried:
                # 第二次仍是噪声：大概率环境音或唤醒词又一次被误转写，
                # 礼貌收场，不再问第三次，免得反复打扰来访者。
                with self._lock:
                    self._noise_retry_used.discard(device_id)
                self._logger.info(f"来访留言连续两次疑似噪声，礼貌收场，不入账：{content}")
                return REPLY_DECLINED
            # 第一次疑似噪声（唤醒词误转写/超短句）：不入账，重开窗口再问一次
            with self._lock:
                self._noise_retry_used.add(device_id)
                self._windows[device_id] = now + timedelta(
                    seconds=self._message_window_s
                )
            self._logger.info(f"来访留言疑似噪声，不入账，再问一次：{content}")
            return REPLY_ASK_AGAIN

        with self._lock:
            self._noise_retry_used.discard(device_id)

        # 来访时间由台账按记账时刻自动打上；身份未知就记「同事」
        self._ledger.record(
            "visitor_message",
            f"{VISITOR_MESSAGE_PREFIX}{content}",
            source="visitor",
        )
        self._logger.info(f"已记录来访留言：{content}")
        return self._compose_recorded_reply(content)

    def _is_noise(self, content: str) -> bool:
        """归一后不足 4 个字，或命中唤醒词表，判定为噪声候选。"""
        normalized = self._normalize(content)
        if len(normalized) < NOISE_MIN_CHARS:
            return True
        return any(word in normalized for word in self._wake_words)

    @staticmethod
    def _compose_recorded_reply(content: str) -> str:
        """确认复述：台账存全文，这里只截断念给人听的这句。"""
        brief = content
        if len(brief) > BROADCAST_TRUNCATE_CHARS:
            brief = brief[:BROADCAST_TRUNCATE_CHARS] + "…"
        return REPLY_RECORDED_TEMPLATE.format(brief=brief)

    def has_open_window(self, device_id: str) -> bool:
        with self._lock:
            expires = self._windows.get(device_id)
            return expires is not None and self._clock() < expires


# ---------------------------------------------------------------- 单例

_flow: Optional[VisitorFlow] = None
_flow_lock = threading.Lock()


def get_visitor_flow(config: Optional[dict] = None, **kwargs) -> VisitorFlow:
    """进程级单例。首次调用带依赖装配，之后各入口拿到的是同一个实例。"""
    global _flow
    with _flow_lock:
        if _flow is None:
            _flow = VisitorFlow(config, **kwargs)
        return _flow


def reset_visitor_flow() -> None:
    """仅测试用：清掉单例，避免用例间串台。"""
    global _flow
    with _flow_lock:
        _flow = None


def visitor_flow_handle_asr(device_id: str, text: str) -> Optional[str]:
    """给 ASR 入口用的便捷函数：没装配过来访流程时返回 None，行为不变。"""
    with _flow_lock:
        flow = _flow
    if flow is None:
        return None
    try:
        return flow.handle_asr_text(device_id, text)
    except Exception:
        logging.getLogger(__name__).exception("来访留言处理失败，本句按普通对话继续")
        return None
