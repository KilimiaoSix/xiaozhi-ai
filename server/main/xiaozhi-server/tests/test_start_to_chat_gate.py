"""startToChat 的准入：麦克风拾到的语音受对话窗口门管，主动发起与系统提示不受管。"""

import sys

# tests/test_morning_brief_config.py 在导入期就把 config.manage_api_client 换成了
# 不完整的桩，且是全局 sys.modules 级别；全量跑时它先于本文件生效，
# 会让下面的 receiveAudioHandle 导入失败。这里只补齐缺的成员，不去改那份桩。
_stub = sys.modules.get("config.manage_api_client")
if _stub is not None and not hasattr(_stub, "report"):
    _stub.report = lambda *args, **kwargs: None

import pytest

from core.handle.receiveAudioHandle import startToChat


class Recorder:
    def __init__(self):
        self.messages = []

    def bind(self, **_kwargs):
        return self

    def info(self, message="", *_a, **_k):
        self.messages.append(str(message))

    def debug(self, *_a, **_k):
        pass


class FakeConn:
    """只带门判定阶段会用到的字段；一旦放行会往下走 need_bind 分支并立即返回，
    因此不需要搭起完整的 LLM/TTS 环境。"""

    def __init__(self, config):
        self.config = config
        self.device_id = "dc:da:0c:26:9a:60"
        self.logger = Recorder()
        self.introduced_speakers = set()
        self.current_speaker = None
        self.need_bind = True          # 放行后走这条分支即刻返回
        self.bind_checked = False

    async def _noop(self, *_a, **_k):
        pass


async def _fake_check_bind(conn):
    conn.bind_checked = True


@pytest.fixture(autouse=True)
def stub_bind(monkeypatch):
    monkeypatch.setattr(
        "core.handle.receiveAudioHandle.check_bind_device", _fake_check_bind
    )


def gated_config(**overrides):
    section = {"enabled": True, "window_seconds": 60}
    section.update(overrides)
    return {
        "dialogue_gate": section,
        "wakeup_words": ["你好喵伴"],
        "script_mode": False,
    }


@pytest.mark.asyncio
async def test_microphone_speech_is_dropped_while_the_window_is_closed():
    conn = FakeConn(gated_config())

    await startToChat(conn, "没发你那边")

    assert conn.bind_checked is False
    assert any("对话窗口未打开" in m for m in conn.logger.messages)


@pytest.mark.asyncio
async def test_detect_channel_opens_the_window_and_passes_through():
    """唤醒词/手势/按键都走 detect 通道，是明确的用户主动发起。"""
    conn = FakeConn(gated_config())

    await startToChat(conn, "嘿，你好呀", source="detect")

    assert conn.bind_checked is True


@pytest.mark.asyncio
async def test_window_opened_by_detect_lets_following_microphone_turns_through():
    conn = FakeConn(gated_config())

    await startToChat(conn, "嘿，你好呀", source="detect")
    conn.bind_checked = False
    await startToChat(conn, "接着刚才那个问题")

    assert conn.bind_checked is True


@pytest.mark.asyncio
async def test_system_prompt_is_never_gated():
    """结束语这类内部提示不是用户语音，不该被门拦住。"""
    conn = FakeConn(gated_config())

    await startToChat(conn, "用依依不舍的话结束这场对话", source="system")

    assert conn.bind_checked is True


@pytest.mark.asyncio
async def test_wake_word_spoken_mid_session_opens_the_window():
    conn = FakeConn(gated_config())

    await startToChat(conn, "你好喵伴")

    assert conn.bind_checked is True


@pytest.mark.asyncio
async def test_gate_disabled_keeps_previous_behaviour():
    conn = FakeConn(gated_config(enabled=False))

    await startToChat(conn, "没发你那边")

    assert conn.bind_checked is True


@pytest.mark.asyncio
async def test_script_mode_still_wins_over_the_gate():
    config = gated_config()
    config["script_mode"] = True
    conn = FakeConn(config)

    await startToChat(conn, "嘿，你好呀", source="detect")

    assert conn.bind_checked is False
