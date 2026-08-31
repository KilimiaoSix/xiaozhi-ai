"""推送播报后的对话窗口开关（open_dialogue）。

每条 speak=true 的推送播完都会 gate.open(robot_spoke_first)——告警后的
「启动诊断」、迎接后的顺口吩咐靠它进门。但通用 HTTP 推送口的自动化事件
（coding agent 任务完成等）真机上每 10~30 秒一条，每条都开 60 秒窗口，
等于把门顶成常开：环境人声随便进 LLM，单次对话名存实亡。

开窗因此收进 open_dialogue 开关：内部对话型调用方默认开（不动老行为），
通用 HTTP 推送口默认关、payload 显式传 open_dialogue: true 才开。
"""

import pytest

from core.dialogue_gate import DialogueGate
from core.handle import pushHandle


class _StubLogger:
    def bind(self, **_kwargs):
        return self

    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass


class _StubDialogue:
    def __init__(self):
        self.dialogue = []

    def put(self, message):
        self.dialogue.append(message)


class FakeConn:
    def __init__(self):
        self.device_id = "dc:da:0c:26:9a:60"
        self.client_is_speaking = False
        self.config = {
            "dialogue_gate": {"enabled": True, "window_seconds": 60},
            "push_speak": {"wait_seconds": 0.01, "poll_interval": 0.005},
        }
        self.logger = _StubLogger()
        self.dialogue = _StubDialogue()


@pytest.fixture
def spoken_push(monkeypatch):
    """让播报链路必然“说出口”，只考察开窗逻辑本身。"""

    async def fake_alert(conn, *_a, **_k):
        return None

    async def fake_ensure_speakable(conn):
        return None

    async def fake_speak(conn, text):
        return True

    monkeypatch.setattr(pushHandle, "push_alert_to_device", fake_alert)
    monkeypatch.setattr(pushHandle, "ensure_speakable", fake_ensure_speakable)
    monkeypatch.setattr(pushHandle, "speak_on_device", fake_speak)


def window_is_open(conn) -> bool:
    return DialogueGate(conn.config).allow(conn, "好的，启动诊断")


@pytest.mark.asyncio
async def test_spoken_push_opens_reply_window_by_default(spoken_push):
    """内部调用方不传开关：播报后照旧开窗，告警应答不回退。"""
    conn = FakeConn()

    spoke = await pushHandle.push_work_event(conn, "生产告警来了", speak=True)

    assert spoke is True
    assert window_is_open(conn) is True


@pytest.mark.asyncio
async def test_open_dialogue_false_keeps_the_door_shut(spoken_push):
    """通知类推送显式关窗：播完不留对话窗口，环境人声进不来。"""
    conn = FakeConn()

    spoke = await pushHandle.push_work_event(
        conn, "claude-code 任务 完成了", speak=True, open_dialogue=False
    )

    assert spoke is True
    assert window_is_open(conn) is False


@pytest.mark.asyncio
async def test_disabled_gate_leaves_no_ghost_window(spoken_push):
    """门没启用就别在 conn 上留窗口属性。

    open() 是无条件 setattr，而 window_open 只读属性、不看 enabled：
    dialogue_gate.enabled=false 时每条带声推送都会留下一个 60 秒的窗口
    属性，没有任何写者认领，却会被 voice_session_active 这类旁观者读成
    「人还在」。另外两处调用点（abortHandle / receiveAudioHandle）都是先
    判 gate.enabled 再开窗，这里必须对齐。
    """
    conn = FakeConn()
    conn.config["dialogue_gate"] = {"enabled": False, "window_seconds": 60}

    await pushHandle.push_work_event(conn, "生产告警来了", speak=True)

    assert getattr(conn, "_dialogue_window_until", None) is None


@pytest.mark.asyncio
async def test_notification_push_text_does_not_open_the_followup_channel(spoken_push):
    """推送文案以问号收尾时，不能借「机器人在追问」那条通道放行环境人声。

    推送内容会以 assistant 身份写进对话历史（用户追问「刚才那个告警怎么回事」
    时 LLM 得有上下文），而对话门的追问通道只看末条 assistant 是否以问号收尾。
    于是一条 open_dialogue=False 的通知型推送只要问一句「要不要继续？」，就能
    从追问通道把房间人声放进 LLM——等于绕过它自己关掉的那扇窗。
    """
    conn = FakeConn()
    gate = DialogueGate(conn.config)
    gate.open(conn, "唤醒词")
    assert gate.allow(conn, "帮我跑一下测试") is True  # 用户这轮用掉窗口，追问窗开始计时

    await pushHandle.push_work_event(
        conn, "claude-code 要不要继续跑下一步？", speak=True, open_dialogue=False
    )

    assert gate.allow(conn, "那我们下午再对一下") is False


@pytest.mark.asyncio
async def test_unspoken_push_never_opens_a_window(spoken_push):
    """没播出口的推送（speak=false）从来不开窗，这是既有行为的回归钉。"""
    conn = FakeConn()

    spoke = await pushHandle.push_work_event(conn, "只上屏不出声", speak=False)

    assert spoke is False
    assert window_is_open(conn) is False
