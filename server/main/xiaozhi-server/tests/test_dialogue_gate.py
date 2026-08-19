"""对话窗口门：只有用户主动发起过，才让语音进 LLM。

固件每条播报结束后必然自动开麦（tts.stop 之后进 Listening），麦克风会拾到
房间里与设备无关的人声。此前服务端对 ASR 文本无任何准入检查，于是
人声 -> ASR -> LLM -> 新的 TTS -> 播完又开麦，形成自激循环，设备长期占用，
主动推送的播报全被忙态吞掉。本模块就是那道准入门。
"""

from core.dialogue_gate import DialogueGate


class FakeConn:
    """只带门需要的字段。"""

    def __init__(self, config):
        self.config = config
        self.device_id = "dc:da:0c:26:9a:60"

        class _Logger:
            def bind(self, **_kwargs):
                return self

            def info(self, *_a, **_k):
                pass

            def debug(self, *_a, **_k):
                pass

        self.logger = _Logger()


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_config(**overrides):
    section = {"enabled": True, "window_seconds": 60}
    section.update(overrides)
    return {
        "dialogue_gate": section,
        "wakeup_words": ["你好小智", "你好喵伴"],
        "wake_word": {"display": "你好喵伴"},
    }


def make(**overrides):
    clock = Clock()
    config = make_config(**overrides)
    return DialogueGate(config, clock=clock), FakeConn(config), clock


# ---------------------------------------------------------------- 默认与开关

def test_disabled_gate_allows_everything():
    gate, conn, _ = make(enabled=False)

    assert gate.allow(conn, "随便一句无关的话") is True


def test_absent_section_defaults_to_disabled():
    """没配就是不启用：不能因为升级了服务端就把别人的语音对话闷掉。"""
    gate = DialogueGate({}, clock=Clock())
    conn = FakeConn({})

    assert gate.allow(conn, "随便一句无关的话") is True


# ---------------------------------------------------------------- 关门态

def test_closed_window_rejects_unrelated_speech():
    gate, conn, _ = make()

    assert gate.allow(conn, "没发你那边") is False


def test_wake_word_in_asr_text_opens_the_window():
    """Listening 态下固件的唤醒词检测是关的，唤醒语只会以 ASR 文本到达。"""
    gate, conn, _ = make()

    assert gate.allow(conn, "你好喵伴") is True
    assert gate.allow(conn, "帮我看下这个报错") is True


def test_wake_word_match_ignores_punctuation():
    gate, conn, _ = make()

    assert gate.allow(conn, "你好，喵伴。") is True


def test_wake_word_embedded_in_a_sentence_opens_the_window():
    gate, conn, _ = make()

    assert gate.allow(conn, "你好喵伴，帮我跑一下测试") is True


# ---------------------------------------------------------------- 用户主动发起

def test_open_marks_window_and_lets_following_turns_through():
    gate, conn, clock = make()

    gate.open(conn, "唤醒词")
    clock.advance(5)

    assert gate.allow(conn, "接着刚才那个问题") is True


def test_window_expires_after_configured_seconds():
    gate, conn, clock = make(window_seconds=60)

    gate.open(conn, "唤醒词")
    clock.advance(61)

    assert gate.allow(conn, "过期之后的无关人声") is False


def test_each_allowed_turn_slides_the_window():
    """连续对话不该在第 60 秒被硬切断。"""
    gate, conn, clock = make(window_seconds=60)

    gate.open(conn, "唤醒词")
    for _ in range(5):
        clock.advance(50)
        assert gate.allow(conn, "继续聊") is True

    clock.advance(61)
    assert gate.allow(conn, "这次该关了") is False


def test_rejected_text_does_not_slide_the_window():
    """被拒的无关人声不能把窗口续命，否则房间一直有人说话就等于门没关。"""
    gate, conn, clock = make(window_seconds=60)

    clock.advance(10)
    assert gate.allow(conn, "无关人声") is False
    gate.open(conn, "唤醒词")
    clock.advance(30)
    assert gate.allow(conn, "无关人声但窗口还开着") is True


# ---------------------------------------------------------------- 隔离与边界

def test_two_connections_keep_independent_windows():
    gate, conn_a, _ = make()
    conn_b = FakeConn(conn_a.config)

    gate.open(conn_a, "唤醒词")

    assert gate.allow(conn_a, "甲的后续") is True
    assert gate.allow(conn_b, "乙工位的无关人声") is False


def test_empty_text_is_rejected_without_opening():
    gate, conn, _ = make()

    assert gate.allow(conn, "") is False
    assert gate.allow(conn, "   ") is False


def test_wake_word_list_falls_back_to_display_only():
    gate = DialogueGate(
        {"dialogue_gate": {"enabled": True}, "wake_word": {"display": "你好喵伴"}},
        clock=Clock(),
    )
    conn = FakeConn({})

    assert gate.allow(conn, "你好喵伴") is True
    assert gate.allow(conn, "别的话") is True  # 上一句已开门


def test_no_wake_words_configured_still_gates_on_explicit_open():
    """一个唤醒词都没配时，只认 detect 路径的显式开门，不因此放行一切。"""
    gate = DialogueGate({"dialogue_gate": {"enabled": True}}, clock=Clock())
    conn = FakeConn({})

    assert gate.allow(conn, "无关人声") is False
    gate.open(conn, "按键")
    assert gate.allow(conn, "按键之后说的话") is True
