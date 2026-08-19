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

窗口在「用户确实主动发起」时打开，之后一段时间内的后续轮次照常放行：
  - detect 通道（唤醒词 / 手势 / 按键）由调用方显式 open()；
  - 会话中途说唤醒语只会以 ASR 文本到达——Listening 态下固件的 AFE 唤醒检测是关的
    （application.cc:741-744）——所以这里额外做一次文本级唤醒词匹配。
窗口外的文本直接丢弃，不进 LLM，循环断掉。

被拒的文本不会续窗，否则房间里一直有人说话就等于门没关。

默认不启用：升级服务端不该把别人正在用的语音对话闷掉。
"""

import time
from typing import Callable, Optional

TAG = __name__

DEFAULT_WINDOW_SECONDS = 60.0
_WINDOW_ATTR = "_dialogue_window_until"


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

        stripped = _normalize(text)
        if not stripped:
            return False

        if self._contains_wake_word(stripped):
            self.open(conn, "识别到唤醒词")
            return True

        deadline = getattr(conn, _WINDOW_ATTR, None)
        if deadline is not None and self._clock() < deadline:
            # 窗口内的后续轮次照常放行，并把窗口往后滑，
            # 连续对话不该在第 N 秒被硬切断
            setattr(conn, _WINDOW_ATTR, self._clock() + self._window_seconds)
            return True

        self._log(conn, f"对话窗口未打开，丢弃这段语音: {text}")
        return False

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
