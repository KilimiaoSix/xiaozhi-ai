"""唤醒词打断（abort reason=wake_word_detected）之后必须重开对话窗口门。

背景：设备在说话态检测到唤醒词会上行 {"type":"abort",
"reason":"wake_word_detected"}（固件 application.cc:678-679 -> protocol.cc:42-49）。
服务端 handleAbortMessage 此前只处理打断本身（置 client_abort、清 TTS
队列、下行 tts stop），完全不碰对话门。dialogue_gate.enabled=true 时，
用户唤醒词打断成功后接着说的话会被 dialogue_gate.py 的 allow() 静默丢弃
——打断成功也白打断。

本文件验证：reason 为 wake_word_detected 时重开窗口；其他 reason（含缺省）
不触碰门；conn 拿不到门实例时静默跳过、不抛错。
"""

import pytest

from core.dialogue_gate import DialogueGate
from core.handle.abortHandle import handleAbortMessage
from core.handle.textHandler.abortMessageHandler import AbortTextMessageHandler


class _StubLogger:
    def bind(self, **_kwargs):
        return self

    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass


class _StubWebsocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


class FakeConn:
    """只带 handleAbortMessage 需要的字段，config 与 test_dialogue_gate.py 同口径。"""

    def __init__(self, config=None):
        self.config = (
            config
            if config is not None
            else {"dialogue_gate": {"enabled": True, "window_seconds": 60}}
        )
        self.logger = _StubLogger()
        self.websocket = _StubWebsocket()
        self.session_id = "sess-1"
        self.close_after_chat = True
        self.client_abort = False
        self.cleared_queues = False

    def clear_queues(self):
        self.cleared_queues = True

    def clearSpeakStatus(self):
        pass


class BareConn:
    """没有 .config：模拟 conn 上拿不到门实例所需信息的场景。"""

    def __init__(self):
        self.logger = _StubLogger()
        self.websocket = _StubWebsocket()
        self.session_id = "sess-2"
        self.close_after_chat = True
        self.client_abort = False

    def clear_queues(self):
        pass

    def clearSpeakStatus(self):
        pass


class RecordingGate:
    """gate 替身：只记录构造参数与 open() 调用，不跑真实窗口逻辑。"""

    instances = []

    def __init__(self, config):
        self.config = config
        self.enabled = True
        self.open_calls = []
        RecordingGate.instances.append(self)

    def open(self, conn, reason=""):
        self.open_calls.append((conn, reason))


@pytest.fixture(autouse=True)
def _reset_recording_gate():
    RecordingGate.instances = []
    yield
    RecordingGate.instances = []


@pytest.fixture
def fake_gate(monkeypatch):
    monkeypatch.setattr("core.dialogue_gate.DialogueGate", RecordingGate)
    return RecordingGate


def _all_open_calls():
    calls = []
    for inst in RecordingGate.instances:
        calls.extend(inst.open_calls)
    return calls


# ---------------------------------------------------------------- handleAbortMessage 直接调用


@pytest.mark.asyncio
async def test_wake_word_abort_reopens_dialogue_gate(fake_gate):
    conn = FakeConn()

    await handleAbortMessage(conn, reason="wake_word_detected")

    calls = _all_open_calls()
    assert len(calls) == 1
    called_conn, called_reason = calls[0]
    assert called_conn is conn
    assert isinstance(called_reason, str) and called_reason != ""
    # gate 必须用 conn.config 构造，口径与 receiveAudioHandle.py 的 detect 通道一致
    assert RecordingGate.instances[0].config is conn.config


@pytest.mark.asyncio
async def test_missing_reason_does_not_reopen_gate(fake_gate):
    conn = FakeConn()

    await handleAbortMessage(conn)

    assert _all_open_calls() == []


@pytest.mark.asyncio
async def test_other_reason_does_not_reopen_gate(fake_gate):
    conn = FakeConn()

    await handleAbortMessage(conn, reason="manual_stop")

    assert _all_open_calls() == []


@pytest.mark.asyncio
async def test_abort_still_performs_core_interrupt_regardless_of_reason(fake_gate):
    """开门是附加动作，不能改变打断本身的既有行为。"""
    conn = FakeConn()

    await handleAbortMessage(conn, reason="wake_word_detected")

    assert conn.client_abort is True
    assert conn.close_after_chat is False
    assert conn.cleared_queues is True
    assert len(conn.websocket.sent) == 1
    assert '"state": "stop"' in conn.websocket.sent[0]


@pytest.mark.asyncio
async def test_conn_without_config_does_not_raise(fake_gate):
    """conn 没有 .config，拿不到门实例：静默跳过，不抛错。"""
    conn = BareConn()

    await handleAbortMessage(conn, reason="wake_word_detected")  # 不应抛异常

    assert conn.client_abort is True
    assert _all_open_calls() == []


@pytest.mark.asyncio
async def test_disabled_gate_is_not_opened(fake_gate):
    """gate 未启用时静默跳过，不调用 open()。"""
    RecordingGate_disabled = RecordingGate

    class DisabledGate(RecordingGate_disabled):
        def __init__(self, config):
            super().__init__(config)
            self.enabled = False

    import core.dialogue_gate as dialogue_gate_module

    conn = FakeConn(config={"dialogue_gate": {"enabled": False}})

    # 替换成一个 enabled=False 的替身
    import core.handle.abortHandle as abort_handle_module  # noqa: F401

    orig = dialogue_gate_module.DialogueGate
    dialogue_gate_module.DialogueGate = DisabledGate
    try:
        await handleAbortMessage(conn, reason="wake_word_detected")
    finally:
        dialogue_gate_module.DialogueGate = orig

    assert _all_open_calls() == []


# ---------------------------------------------------------------- AbortTextMessageHandler 透传 reason


@pytest.mark.asyncio
async def test_handler_forwards_wake_word_reason_to_gate(fake_gate):
    handler = AbortTextMessageHandler()
    conn = FakeConn()

    await handler.handle(conn, {"type": "abort", "reason": "wake_word_detected"})

    assert len(_all_open_calls()) == 1


@pytest.mark.asyncio
async def test_handler_without_reason_key_does_not_reopen_gate(fake_gate):
    handler = AbortTextMessageHandler()
    conn = FakeConn()

    await handler.handle(conn, {"type": "abort"})

    assert _all_open_calls() == []


# ---------------------------------------------------------------- 端到端：真实 DialogueGate 下窗口确实打开


@pytest.mark.asyncio
async def test_wake_word_abort_actually_opens_the_real_gate_window():
    """不用替身，跑真实 DialogueGate：打断后窗口内的下一句真的能进来。"""
    conn = FakeConn(
        config={"dialogue_gate": {"enabled": True, "window_seconds": 60}}
    )

    await handleAbortMessage(conn, reason="wake_word_detected")

    assert DialogueGate(conn.config).allow(conn, "接着刚才那句") is True


@pytest.mark.asyncio
async def test_non_wake_word_abort_leaves_real_gate_closed():
    conn = FakeConn(
        config={"dialogue_gate": {"enabled": True, "window_seconds": 60}}
    )

    await handleAbortMessage(conn, reason="manual_stop")

    assert DialogueGate(conn.config).allow(conn, "无关的房间人声") is False
