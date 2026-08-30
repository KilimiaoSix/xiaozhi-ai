"""对话窗口门：只有用户主动发起过，才让语音进 LLM。

固件每条播报结束后必然自动开麦（tts.stop 之后进 Listening），麦克风会拾到
房间里与设备无关的人声。此前服务端对 ASR 文本无任何准入检查，于是
人声 -> ASR -> LLM -> 新的 TTS -> 播完又开麦，形成自激循环，设备长期占用，
主动推送的播报全被忙态吞掉。本模块就是那道准入门。
"""

from core.dialogue_gate import DialogueGate, window_open, _normalize, _effective_text


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


def make_config(exit_commands=None, **overrides):
    section = {"enabled": True, "window_seconds": 60}
    section.update(overrides)
    config = {
        "dialogue_gate": section,
        "wakeup_words": ["你好小智", "你好喵伴"],
        "wake_word": {"display": "你好喵伴"},
    }
    if exit_commands is not None:
        config["exit_commands"] = exit_commands
    return config


def make(exit_commands=None, **overrides):
    clock = Clock()
    config = make_config(exit_commands=exit_commands, **overrides)
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


def test_window_open_follows_gate_lifecycle():
    """window_open 是给休眠链路看的只读探针：开门为真，超时回假。"""
    clock = Clock()
    gate = DialogueGate({"dialogue_gate": {"enabled": True, "window_seconds": 60}}, clock=clock)
    conn = FakeConn({})

    assert window_open(conn, clock=clock) is False
    gate.open(conn, "唤醒词")
    assert window_open(conn, clock=clock) is True
    clock.advance(61)
    assert window_open(conn, clock=clock) is False


def test_window_open_ignores_conn_without_window_attr():
    assert window_open(object()) is False


# ---------------------------------------------------------------- 同音字归一（ASR 常见混淆）
# 真机联调实测：设备停在聆听态（唤醒模型关闭）时唤醒只能靠 ASR 文本匹配唤醒词，
# 而 ASR 把「你好小智」转成「你好，小治」、把「你好喵伴」转成「你好，苗办」——
# 变体表怎么加都追不上，必须做同音/易混字归一后再比较。


def test_homophone_confused_zhi_wake_word_opens_the_gate():
    """ASR 把「你好小智」听成「你好，小治」：同音归一后仍要能开门。"""
    gate, conn, clock = make()

    assert gate.allow(conn, "你好，小治。") is True
    clock.advance(3)
    assert gate.allow(conn, "帮我看下这个报错") is True


def test_homophone_confused_miaoban_wake_word_opens_the_gate():
    """ASR 把「你好喵伴」听成「你好，苗办」：同音归一后仍要能开门。"""
    gate, conn, clock = make()

    assert gate.allow(conn, "你好，苗办。") is True


def test_normalize_applies_the_confusable_char_mapping():
    """归一表至少覆盖唤醒词高频混淆：治/志/只/纸→智，苗/妙/秒/描→喵，
    办/拌/半/班→伴。"""
    assert _normalize("你好，小治。") == "你好小智"
    assert _normalize("你好志") == "你好智"
    assert _normalize("你好只") == "你好智"
    assert _normalize("你好纸") == "你好智"
    assert _normalize("你好苗办") == "你好喵伴"
    assert _normalize("你好妙伴") == "你好喵伴"
    assert _normalize("你好秒伴") == "你好喵伴"
    assert _normalize("你好描伴") == "你好喵伴"
    assert _normalize("你好苗拌") == "你好喵伴"
    assert _normalize("你好苗半") == "你好喵伴"
    assert _normalize("你好苗班") == "你好喵伴"


# ---------------------------------------------------------------- 退出命令逃生通道
# 对话门关闭时，此前连「退出」都会被 allow() 当成无关人声丢弃——丢弃日志在前、
# check_direct_exit 在后，语音层面完全没有逃生通道。


def test_exit_command_escapes_the_closed_gate():
    """门关闭时明确的退出词必须放行，让下游 check_direct_exit 正常关连接。"""
    gate, conn, clock = make(exit_commands=["退出", "没事了"])

    assert gate.allow(conn, "退出") is True


def test_exit_phrase_embedded_in_a_sentence_does_not_escape_the_closed_gate():
    """退出词必须是整句：「没事了退下吧」只是带着配置的退出词「没事了」，
    不是整句相等，不该被子串放行——真机上「挺好，再见。」就是这样因含
    「再见」被误放行进了 LLM，而 check_direct_exit 整句全等才关连接，
    子串放行的句子既不会真的退出，又会让机器人对着环境人声开口，比不
    放行还糟。"""
    gate, conn, clock = make(exit_commands=["退出", "没事了"])

    assert gate.allow(conn, "没事了退下吧") is False


def test_casual_chat_containing_an_exit_word_is_dropped_not_escaped():
    """真机 15:11:38 实锤：环境闲聊「挺好，再见。」因含退出词「再见」被
    子串放行误闯 LLM。归一后必须整句等于退出词才放行；整句「再见」照常
    放行，做对照。"""
    gate, conn, clock = make(exit_commands=["再见"])

    assert gate.allow(conn, "挺好，再见。") is False
    assert gate.allow(conn, "再见") is True


def test_exit_word_that_is_itself_a_full_sentence_still_escapes():
    """退出词配的就是一句完整口令时，整句相等照常放行——收紧到整句匹配
    不代表长退出词失效。"""
    gate, conn, clock = make(exit_commands=["没事了退下吧"])

    assert gate.allow(conn, "没事了退下吧") is True


def test_closed_gate_with_exit_commands_configured_still_rejects_unrelated_speech():
    """配置了退出词不代表放宽了门槛：普通无关语句照样被丢弃，不能误放。"""
    gate, conn, clock = make(exit_commands=["退出", "没事了"])

    assert gate.allow(conn, "没发你那边") is False
    assert gate.allow(conn, "什么这个wifi") is False


def test_exit_command_does_not_escape_an_open_gate_specially():
    """开门状态行为不变：窗口开着时退出词照常走「本轮用掉即关窗」的老路径，
    而不是被退出通道特殊处理绕过窗口状态变化。"""
    gate, conn, clock = make(exit_commands=["退出"])

    gate.open(conn, "唤醒词")
    assert gate.allow(conn, "退出") is True
    # 单次对话：本轮已被这句「退出」用掉，窗口应已关闭——
    # 后续无关语音应恢复被丢弃，证明退出词没有跳过正常的关窗逻辑。
    assert gate.allow(conn, "路人接着说的闲话") is False


def test_no_exit_commands_configured_behaves_like_before():
    """未配置 exit_commands 时不引入回归：关闭态照常丢弃一切文本。"""
    gate, conn, clock = make()

    assert gate.allow(conn, "退出") is False


# ---------------------------------------------------------------- 无副作用：归一不改写传给下游的原文


def test_normalize_does_not_mutate_its_input():
    original = "你好，小治。帮我看下这个治理任务"
    result = _normalize(original)

    assert result != original  # 归一确实生效（否则下面的相等断言没有意义）
    assert original == "你好，小治。帮我看下这个治理任务"


def test_effective_text_does_not_mutate_its_input():
    envelope = '{"content": "你好，小治。", "language": "zh"}'
    result = _effective_text(envelope)

    assert result == "你好，小治。"
    assert envelope == '{"content": "你好，小治。", "language": "zh"}'


def test_allow_only_returns_a_bool_and_leaves_the_caller_text_untouched():
    """allow() 只返回布尔；同音归一只用于门内部的唤醒词/退出词匹配比较，
    不能改写调用方持有的原始文本（放行给 LLM/check_direct_exit 的必须是原文）。"""
    gate, conn, clock = make(exit_commands=["退出"])
    original_text = "你好，小治，退出"

    returned = gate.allow(conn, original_text)

    assert returned is True
    assert isinstance(returned, bool)
    assert original_text == "你好，小治，退出"
