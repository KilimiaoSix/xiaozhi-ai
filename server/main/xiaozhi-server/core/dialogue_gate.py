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

两个真机上摔过跤的细节：
  - ASR 到这里的文本可能是 {"content": ...} 说话人信封，唤醒词匹配必须用
    信封里的纯文本，否则纯唤醒词永远不等于唤醒词、被误判成「带问题」当场关窗；
  - 机器人上一句以问号收尾是在追问（「你是想问哪个？」），关窗后的
    window_seconds 内放这一条回答进来，不然对话被腰斩在机器人自己的问题上。

被拒的文本不会续窗，否则房间里一直有人说话就等于门没关。

默认不启用：升级服务端不该把别人正在用的语音对话闷掉。
"""

import json
import time
from typing import Callable, Optional

TAG = __name__

DEFAULT_WINDOW_SECONDS = 60.0
_WINDOW_ATTR = "_dialogue_window_until"
_FOLLOWUP_ATTR = "_dialogue_followup_until"


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
    """去标点去空白后比较，口径与 remove_punctuation_and_length 一致。

    否则配置里的「你好，喵伴」永远匹配不上 ASR 出的「你好喵伴」。
    """
    from core.utils.util import remove_punctuation_and_length

    try:
        _, stripped = remove_punctuation_and_length(text or "")
    except Exception:
        stripped = (text or "").strip()
    return stripped.replace(" ", "")


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

    def open(self, conn, reason: str = "") -> None:
        """用户确实主动发起了一轮：开窗。"""
        setattr(conn, _WINDOW_ATTR, self._clock() + self._window_seconds)
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
            # 机器人上一句在追问，这条是它等的回答；答完继续单次规则
            self._close(conn, "追问的回答，本轮已用掉")
            return True

        self._log(conn, f"对话窗口未打开，丢弃这段语音: {_effective_text(text)}")
        return False

    def _close(self, conn, reason: str = "") -> None:
        setattr(conn, _WINDOW_ATTR, None)
        # 关窗时间同时是追问续窗的起点：机器人若以问号收尾，
        # window_seconds 内允许用户把回答补进来
        setattr(conn, _FOLLOWUP_ATTR, self._clock() + self._window_seconds)
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
        content = (getattr(last, "content", None) or "").strip()
        return content.endswith(("？", "?"))

    def _contains_wake_word(self, normalized_text: str) -> bool:
        return any(word in normalized_text for word in self._wake_words)

    def _log(self, conn, message: str) -> None:
        logger = self._logger or getattr(conn, "logger", None)
        if logger is None:
            return
        try:
            logger.bind(tag=TAG).info(message)
        except Exception:
            pass
