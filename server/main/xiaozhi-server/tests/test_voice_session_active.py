"""voice_session_active：设备语音对话活跃探针的判定口径。

presence 休眠链路用它回答「摄像头说没人，但语音说明人还在吗」。
只认语音对话自身的信号（正在播 / 正在听 / 最近说过话 / 对话窗口开着），
不认 last_activity_time——那个时间戳被设备 30 秒一条的 ping 心跳刷新，
安静挂机也永远“活跃”。
"""

from types import SimpleNamespace

from core.dialogue_gate import ROBOT_SPOKE_FIRST_REASON, DialogueGate
from core.handle.pushHandle import voice_session_active


def make_conn(**attrs):
    conn = SimpleNamespace()
    for key, value in attrs.items():
        setattr(conn, key, value)
    return conn


def test_tts_playing_counts_as_active():
    assert voice_session_active(make_conn(client_is_speaking=True), 60) is True


def test_incoming_voice_counts_as_active():
    assert voice_session_active(make_conn(client_have_voice=True), 60) is True


def test_recent_vad_voice_counts_as_active():
    conn = make_conn(vad_last_voice_time=1_000_000.0)  # 毫秒，VAD 的口径
    clock = lambda: 1_030.0  # 30 秒后（秒）
    assert voice_session_active(conn, 60, clock=clock) is True


def test_stale_vad_voice_is_not_active():
    conn = make_conn(vad_last_voice_time=1_000_000.0)
    clock = lambda: 1_100.0  # 100 秒后，超出 60 秒回看窗口
    assert voice_session_active(conn, 60, clock=clock) is False


def test_open_dialogue_window_counts_as_active():
    """光喊了唤醒词还没提问：VAD 没有人声，但窗口开着，对话正要开始。"""
    conn = make_conn()
    DialogueGate({"dialogue_gate": {"enabled": True}}).open(conn, "唤醒词")
    assert voice_session_active(conn, 60, clock=lambda: 0.0) is True


def test_robot_opened_window_is_not_presence_evidence():
    """机器人自己播报后开的窗不算「工位上有人」。

    每条 speak=true 的推送播完都会开 60 秒窗口（robot_spoke_first）。若把它
    当在场证据，告警风暴期间每条播报都把摄像头的离席判定往后推 60 秒：离席
    台账永远不开窗，那段真实离席期间的告警一条都进不了返岗汇总，设备也永远
    不进休眠。窗口本身照旧管 ASR 准入，只是不再冒充在场证据。
    """
    conn = make_conn()
    DialogueGate({"dialogue_gate": {"enabled": True}}).open(
        conn, ROBOT_SPOKE_FIRST_REASON
    )
    assert voice_session_active(conn, 2, clock=lambda: 0.0) is False


def test_user_reply_inside_a_robot_window_still_counts_as_active():
    """机器人开的窗里用户真的应答了：VAD 那两条判据自然命中，在场证据不丢。"""
    conn = make_conn(client_have_voice=True)
    DialogueGate({"dialogue_gate": {"enabled": True}}).open(
        conn, ROBOT_SPOKE_FIRST_REASON
    )
    assert voice_session_active(conn, 2, clock=lambda: 0.0) is True


def test_bare_conn_defaults_to_inactive():
    assert voice_session_active(object(), 60) is False
