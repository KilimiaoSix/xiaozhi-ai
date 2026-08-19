"""对话窗口门：只有用户主动发起过，才让语音进 LLM。

固件每条播报结束后必然自动开麦（tts.stop 之后进 Listening），麦克风会拾到
房间里与设备无关的人声。此前服务端对 ASR 文本无任何准入检查，于是
人声 -> ASR -> LLM -> 新的 TTS -> 播完又开麦，形成自激循环，设备长期占用，
主动推送的播报全被忙态吞掉。本模块就是那道准入门。
"""

from core.dialogue_gate import DialogueGate


class FakeMessage:
    def __init__(self, role, content):
        self.role = role
        self.content = content


class FakeDialogue:
    def __init__(self):
        self.dialogue = []


class FakeConn:
    """只带门需要的字段。"""

    def __init__(self, config):
        self.config = config
        self.device_id = "dc:da:0c:26:9a:60"
        self.dialogue = FakeDialogue()

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


def test_each_allowed_turn_slides_the_window_in_continuous_mode():
    """连续对话（single_turn: false）不该在第 60 秒被硬切断。"""
    gate, conn, clock = make(window_seconds=60, single_turn=False)

    gate.open(conn, "唤醒词")
    for _ in range(5):
        clock.advance(50)
        assert gate.allow(conn, "继续聊") is True

    clock.advance(61)
    assert gate.allow(conn, "这次该关了") is False


# ---------------------------------------------------------------- 单次对话（默认）

def test_single_turn_is_the_default_one_round_per_open():
    """默认单次对话：一次开窗只放行一轮，答完必须重新唤醒。"""
    gate, conn, clock = make()

    gate.open(conn, "唤醒词")
    clock.advance(5)
    assert gate.allow(conn, "帮我看下这个报错") is True
    clock.advance(5)
    assert gate.allow(conn, "房间里接着说的话") is False


def test_single_turn_bare_wake_word_keeps_window_for_the_question():
    """光喊唤醒词还没提问：窗口要留给真正的问题那一轮，不能被招呼语用掉。"""
    gate, conn, clock = make()

    assert gate.allow(conn, "你好喵伴") is True
    clock.advance(3)
    assert gate.allow(conn, "今天天气怎么样") is True
    clock.advance(3)
    assert gate.allow(conn, "路人接着说的闲话") is False


def test_single_turn_wake_word_with_question_is_that_one_round():
    """唤醒词直接带着问题说：这句自己就是本轮，说完门就关。"""
    gate, conn, clock = make()

    assert gate.allow(conn, "你好喵伴，帮我跑一下测试") is True
    clock.advance(3)
    assert gate.allow(conn, "路人接着说的闲话") is False


def test_single_turn_unused_window_still_expires():
    """开了窗一直没人提问，窗口照旧超时关闭。"""
    gate, conn, clock = make(window_seconds=60)

    gate.open(conn, "唤醒词")
    clock.advance(61)
    assert gate.allow(conn, "过期之后才说的话") is False


def test_single_turn_can_rewake_after_consumed():
    """本轮用掉后再次唤醒，新的一轮照常放行。"""
    gate, conn, clock = make()

    gate.open(conn, "唤醒词")
    assert gate.allow(conn, "第一轮的问题") is True
    assert gate.allow(conn, "没重新唤醒的话") is False
    assert gate.allow(conn, "你好喵伴，再问一句") is True
    assert gate.allow(conn, "还想蹭第三句") is False


# ---------------------------------------------------------------- ASR 信封

def test_enveloped_bare_wake_word_keeps_window_for_the_question():
    """ASR 给的是 {"content": ...} 信封：纯唤醒词不能因为信封包裹被当成
    「唤醒词带问题」而当场关窗——真机上就是这样把提问闷掉的。"""
    gate, conn, clock = make()

    envelope = '{"content": "你好，小智。", "language": "zh", "emotion": "😶"}'
    assert gate.allow(conn, envelope) is True
    clock.advance(3)
    assert gate.allow(conn, "帮我看下这个报错") is True
    clock.advance(3)
    assert gate.allow(conn, "路人接着说的闲话") is False


def test_enveloped_wake_word_with_question_is_that_one_round():
    gate, conn, clock = make()

    envelope = '{"content": "你好小智，帮我跑一下测试", "language": "zh"}'
    assert gate.allow(conn, envelope) is True
    clock.advance(3)
    assert gate.allow(conn, "路人接着说的闲话") is False


# ---------------------------------------------------------------- 追问续窗

def test_single_turn_reopens_for_the_answer_when_assistant_asked():
    """机器人上一句以问号收尾是在追问，用户的回答必须能进来——
    真机上「你是想让我讲个故事，还是刚才那个任务的事？」之后的回答全被丢弃过。"""
    gate, conn, clock = make()

    gate.open(conn, "唤醒词")
    assert gate.allow(conn, "这个这个人") is True  # 本轮已用掉
    conn.dialogue.dialogue.append(
        FakeMessage("assistant", "你是想让我讲个故事，还是刚才那个任务的事？")
    )
    clock.advance(10)
    assert gate.allow(conn, "刚才那个任务的事") is True  # 追问的回答
    conn.dialogue.dialogue.append(FakeMessage("assistant", "好，任务的事搞定了。"))
    clock.advance(5)
    assert gate.allow(conn, "随口再说的一句") is False  # 没有追问就照常关门


def test_followup_answer_must_arrive_within_the_window():
    """追问续窗不能永久有效，否则一条以问号结尾的旧回复等于门常开。"""
    gate, conn, clock = make(window_seconds=60)

    gate.open(conn, "唤醒词")
    assert gate.allow(conn, "问一句") is True
    conn.dialogue.dialogue.append(FakeMessage("assistant", "要不要继续？"))
    clock.advance(61)
    assert gate.allow(conn, "太晚才给的回答") is False


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
