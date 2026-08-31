"""对话窗口门：只有用户主动发起过，语音才进 LLM。

**为什么需要它**

固件在每条 TTS 播报结束后必然自动开麦：本板 aec_mode_ 恒为 kAecOff，
listening_mode_ 的默认值又正好是 kListeningModeAutoStop（application.h:79），
于是 tts.stop 分支只要不是 ManualStop 就 SetDeviceState(Listening) 并
SendStartListening(mode=auto)（application.cc:461-470、738）。

此前服务端对 ASR 文本没有任何准入检查——asr/base.py 里出了非空文本就无条件
调 startToChat。结果房间里其他人说话 -> ASR -> LLM -> 新的 TTS -> 播完又开麦，
形成自激循环。真机实测拾到过「没发你那边」「什么这个wifi」这类无关人声，
设备因此长期处于忙态，主动推送（工作事件、到岗迎接）的语音播报被
device_busy_reason 全部吞掉。

**这道门怎么判**

窗口在「用户确实主动发起」时打开：
  - detect 通道（唤醒词 / 手势 / 按键）由调用方显式 open()；
  - 会话中途说唤醒语只会以 ASR 文本到达——Listening 态下固件的 AFE 唤醒检测是关的
    （application.cc:741-744）——所以这里额外做一次文本级唤醒词匹配。
窗口外的文本直接丢弃，不进 LLM，循环断掉。

**单次对话是默认（single_turn: true）**：一次开窗只放行一轮。光喊唤醒词时
窗口留给接下来的问题；唤醒词直接带问题时那句自己就是本轮。放行即关窗，
再说话必须重新唤醒。设 single_turn: false 回到连续对话：窗口内的后续轮次
照常放行并把窗口往后滑，直到 window_seconds 内不再有新轮次。

三个真机上摔过跤的细节：
  - ASR 到这里的文本可能是 {"content": ...} 说话人信封，唤醒词匹配必须用
    信封里的纯文本，否则纯唤醒词永远不等于唤醒词、被误判成「带问题」当场关窗；
  - 机器人上一句以问号收尾是在追问（「你是想问哪个？」），关窗后的
    window_seconds 内放这一条回答进来，不然对话被腰斩在机器人自己的问题上；
  - 设备停在聆听态时（唤醒模型关闭）唤醒只能靠这里的文本匹配，而 ASR 把
    「你好小智」转成「你好，小治」、「你好喵伴」转成「你好，苗办」——变体表
    怎么加都追不上，所以 _normalize 里额外做一次同音/易混字归一
    （_CONFUSABLE_CHARS）再比较。

对话门关闭时，语音层面必须留一条逃生通道：不然设备卡在忙态又没人在旁边按键，
连「退出」都会被这道门当无关人声丢弃，用户没有任何办法让它闭嘴。allow() 在
判定丢弃之前会额外检查一次归一后的整句文本是否等于配置的某个 exit_commands
词条（归一口径与唤醒词一致，含同音映射），整句相等就放行，交给下游
check_direct_exit 走正常的关连接流程；窗口已经开着时退出词仍然走原来的
「本轮用掉即关窗」路径，不受这条通道影响。

这里必须是整句相等，不能是子串包含：真机 15:11:38 实锤过反例——环境闲聊
「挺好，再见。」因为子串包含配置的退出词「再见」被这道逃生通道放行进了
LLM，而下游 check_direct_exit 本来就是整句全等才关连接，子串放行的句子既
不会让设备真的退出，又会让机器人对着环境人声开口，比不放行还糟。所以判定
口径收紧成与 check_direct_exit 一致的整句相等。

被拒的文本不会续窗，否则房间里一直有人说话就等于门没关。

默认不启用：升级服务端不该把别人正在用的语音对话闷掉。
"""

import json
import time
from typing import Callable, Optional

TAG = __name__

DEFAULT_WINDOW_SECONDS = 60.0
_WINDOW_ATTR = "_dialogue_window_until"
_WINDOW_REASON_ATTR = "_dialogue_window_reason"
_FOLLOWUP_ATTR = "_dialogue_followup_until"
# 推送写进对话历史的 assistant 文案上打的记号，见 mark_push_authored。
_PUSH_AUTHORED_ATTR = "_dialogue_push_authored"

# 机器人自己播报完开的那扇窗（pushHandle 播报后调 open 的理由）。门要认它
# ——用户顺口的应答得进得来；但它不能冒充「工位上有人」，休眠链路必须分得清，
# 见 user_window_open。
ROBOT_SPOKE_FIRST_REASON = "robot_spoke_first"

# ASR 对唤醒词高频出现的同音/易混字混淆——真机实测「你好小智」被转写成
# 「你好，小治」、「你好喵伴」被转写成「你好，苗办」。只作用于本模块内部
# 的唤醒词/退出词归一比较，不改写传给下游 LLM 或 check_direct_exit 的原文。
_CONFUSABLE_CHARS = {
    "治": "智",
    "志": "智",
    "只": "智",
    "纸": "智",
    "苗": "喵",
    "妙": "喵",
    "秒": "喵",
    "描": "喵",
    "办": "伴",
    "拌": "伴",
    "半": "伴",
    "班": "伴",
    "伴": "伴",
}


def window_open(conn, clock: Optional[Callable[[], float]] = None) -> bool:
    """对话窗口当前是否开着（唤醒后等提问 / 连续对话滑动期内）。

    给休眠链路等旁观者用的只读探针：窗口属性的名字与时间轴（monotonic）
    是本模块的内部约定，外部别直接摸属性。
    """
    deadline = getattr(conn, _WINDOW_ATTR, None)
    if deadline is None:
        return False
    return (clock or time.monotonic)() < deadline


def user_window_open(conn, clock: Optional[Callable[[], float]] = None) -> bool:
    """窗口开着，且开它的是用户（唤醒词 / 手势 / 按键 / 打断），不是机器人自己。

    休眠链路把「窗口开着」当成人还在工位的证据，可这扇窗也可能是机器人播报完
    自己开的（ROBOT_SPOKE_FIRST_REASON）：告警风暴期间每条播报都续 60 秒，
    摄像头的离席判定会被一路推迟——离席台账永不开窗，那段真实离席期间的告警
    一条都进不了返岗汇总，设备也永远不进休眠。机器人开的窗里用户若真的应答了，
    正在拾音 / 最近说过话这两条判据自然会命中，在场证据并不会因此丢掉。
    """
    if not window_open(conn, clock):
        return False
    return getattr(conn, _WINDOW_REASON_ATTR, "") != ROBOT_SPOKE_FIRST_REASON


def mark_push_authored(message) -> None:
    """标记这条 assistant 消息是主动推送写进历史的，不是对话里的一轮回答。

    追问通道认的是「机器人上一句在追问」，而推送的文案同样以 assistant 身份
    进历史（pushHandle 要给「刚才那个告警怎么回事」留上下文）。不加区分的话，
    一条以问号收尾的通知型推送就能借追问通道把环境人声放进 LLM，绕过它自己
    的 open_dialogue=False。
    """
    setattr(message, _PUSH_AUTHORED_ATTR, True)


def _effective_text(text: str) -> str:
    """ASR 可能给 {"content": "...", "speaker": ...} 信封，匹配要用纯文本。

    口径与 receiveAudioHandle 里的信封解析一致；解析不了就按原文处理。
    """
    raw = (text or "").strip()
    if raw.startswith("{") and raw.endswith("}"):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
        if isinstance(data, dict) and isinstance(data.get("content"), str):
            return data["content"]
    return raw


def _normalize(text: str) -> str:
    """去标点去空白、同音字归一后比较，口径与 remove_punctuation_and_length 一致。

    否则配置里的「你好，喵伴」永远匹配不上 ASR 出的「你好喵伴」；同音归一
    进一步兜底 ASR 把「小智」听成「小治」这类高频混淆（_CONFUSABLE_CHARS）。
    只用于本模块内部的唤醒词/退出词匹配比较，纯函数、不修改入参。
    """
    from core.utils.util import remove_punctuation_and_length

    try:
        _, stripped = remove_punctuation_and_length(text or "")
    except Exception:
        stripped = (text or "").strip()
    stripped = stripped.replace(" ", "")
    return "".join(_CONFUSABLE_CHARS.get(ch, ch) for ch in stripped)


class DialogueGate:
    def __init__(
        self,
        config: dict,
        clock: Optional[Callable[[], float]] = None,
        logger=None,
    ) -> None:
        section = (config or {}).get("dialogue_gate") or {}
        if not isinstance(section, dict):
            section = {}
        self._enabled = bool(section.get("enabled", False))
        self._single_turn = bool(section.get("single_turn", True))
        self._window_seconds = self._positive(
            section.get("window_seconds"), DEFAULT_WINDOW_SECONDS
        )
        self._clock = clock or time.monotonic
        self._logger = logger
        self._wake_words = self._collect_wake_words(config or {})
        self._exit_words = self._collect_exit_words(config or {})

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def _positive(value, fallback: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if parsed > 0 else fallback

    @staticmethod
    def _collect_wake_words(config: dict) -> set:
        words = set()
        for word in config.get("wakeup_words") or []:
            normalized = _normalize(str(word))
            if normalized:
                words.add(normalized)
        section = config.get("wake_word") or {}
        if isinstance(section, dict):
            normalized = _normalize(str(section.get("display") or ""))
            if normalized:
                words.add(normalized)
        return words

    @staticmethod
    def _collect_exit_words(config: dict) -> set:
        """退出命令逃生通道用的词表，归一口径与唤醒词一致（同音映射同样兜底）。"""
        words = set()
        for word in config.get("exit_commands") or []:
            normalized = _normalize(str(word))
            if normalized:
                words.add(normalized)
        return words

    def open(self, conn, reason: str = "") -> None:
        """用户确实主动发起了一轮：开窗。

        开窗理由一并记在 conn 上：机器人自己播报后开的窗（见
        ROBOT_SPOKE_FIRST_REASON）在门这里与用户开的等价，但对休眠链路不是
        在场证据，user_window_open 靠这条记录区分。
        """
        setattr(conn, _WINDOW_ATTR, self._clock() + self._window_seconds)
        setattr(conn, _WINDOW_REASON_ATTR, reason)
        self._log(conn, f"对话窗口已打开（{reason or '主动发起'}）")

    def allow(self, conn, text: str) -> bool:
        """这段文本该不该进 LLM。"""
        if not self._enabled:
            return True

        stripped = _normalize(_effective_text(text))
        if not stripped:
            return False

        if self._contains_wake_word(stripped):
            if self._single_turn and stripped not in self._wake_words:
                # 唤醒词后面直接带了问题，这句自己就是本轮，说完即关窗
                self._close(conn, "唤醒词带问题，本轮已用掉")
                return True
            # 光喊唤醒词：窗口留给接下来真正的问题
            self.open(conn, "识别到唤醒词")
            return True

        now = self._clock()
        deadline = getattr(conn, _WINDOW_ATTR, None)
        if deadline is not None and now < deadline:
            if self._single_turn:
                # 单次对话：一次开窗只放行这一轮，再说话必须重新唤醒
                self._close(conn, "单次对话，本轮已用掉")
            else:
                # 连续对话：后续轮次照常放行，并把窗口往后滑，
                # 不该在第 N 秒被硬切断
                setattr(conn, _WINDOW_ATTR, now + self._window_seconds)
            return True

        if self._single_turn and self._followup_pending(conn, now):
            # 机器人上一句在追问，这条是它等的回答；答完追问通道就此关死，
            # 不再续期（arm_followup=False，理由见 _close）
            self._close(conn, "追问的回答，本轮已用掉", arm_followup=False)
            return True

        if self._is_exit_word(stripped):
            # 逃生通道：窗口已经打开时不会走到这里（上面的分支早已 return），
            # 所以不影响「开门状态」的既有行为；只在原本要被丢弃时放行，
            # 交给下游 check_direct_exit 正常关连接。整句相等，不是子串包含
            # ——子串包含会把「挺好，再见。」这类闲聊误放行（见模块顶部说明）。
            self._log(conn, "退出命令放行（对话门关闭中）")
            return True

        self._log(conn, f"对话窗口未打开，丢弃这段语音: {_effective_text(text)}")
        return False

    def _close(self, conn, reason: str = "", arm_followup: bool = True) -> None:
        setattr(conn, _WINDOW_ATTR, None)
        # 关窗时间同时是追问续窗的起点：机器人若以问号收尾，
        # window_seconds 内允许用户把回答补进来。
        #
        # arm_followup=False 只给「这一条本来就是追问的回答」用：追问通道放行
        # 一条即终结。否则消费追问的那次 _close 反手又续满 window_seconds，而
        # LLM 结尾反问是常态，等于「每放行一条再续 60 秒」——房间里一直有人
        # 说话，门就再也关不上，自激循环原样复活。
        deadline = self._clock() + self._window_seconds if arm_followup else None
        setattr(conn, _FOLLOWUP_ATTR, deadline)
        setattr(conn, _WINDOW_REASON_ATTR, "")
        self._log(conn, f"对话窗口已关闭（{reason}）")

    def _followup_pending(self, conn, now: float) -> bool:
        deadline = getattr(conn, _FOLLOWUP_ATTR, None)
        if deadline is None or now >= deadline:
            return False
        return self._last_assistant_reply_asks(conn)

    @staticmethod
    def _last_assistant_reply_asks(conn) -> bool:
        messages = getattr(getattr(conn, "dialogue", None), "dialogue", None)
        if not messages:
            return False
        last = messages[-1]
        if getattr(last, "role", None) != "assistant":
            return False
        if getattr(last, _PUSH_AUTHORED_ATTR, False):
            # 主动推送的文案不是对话里的一轮追问（见 mark_push_authored）
            return False
        content = (getattr(last, "content", None) or "").strip()
        return content.endswith(("？", "?"))

    def _contains_wake_word(self, normalized_text: str) -> bool:
        return any(word in normalized_text for word in self._wake_words)

    def _is_exit_word(self, normalized_text: str) -> bool:
        """整句相等才算命中，口径与 check_direct_exit 的 `text == cmd` 一致。"""
        return normalized_text in self._exit_words

    def _log(self, conn, message: str) -> None:
        logger = self._logger or getattr(conn, "logger", None)
        if logger is None:
            return
        try:
            logger.bind(tag=TAG).info(message)
        except Exception:
            pass
