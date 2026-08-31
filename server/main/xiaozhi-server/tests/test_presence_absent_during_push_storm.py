"""告警风暴期间，机器人自己的播报不能把摄像头的离席判定顶死。

跨状态机的活锁：presence 的离席确认把「语音对话活跃」当成人还在（低头凑近
机器人说话会丢姿态关键点，那种 absent 不可信），而语音活跃探针把「对话窗口
开着」也算一条证据——可这扇窗正是机器人播报完自己开的（robot_spoke_first，
60 秒）。于是空工位 + 告警风暴时每条播报都把离席判定往后推一整扇窗：
离席台账永不开窗（record 判定「不在离席窗口」，风暴期间的告警一条都进不了
pending，主人回来听到的返岗汇总是空的），设备也永远不进休眠基态。

本文件走真实的 voice_session_active 探针（不注入替身），只把 TTS 那一段替换掉，
锁的就是「机器人开的窗不算在场证据」这条。
"""

import json
from datetime import datetime, timezone
from itertools import count

import pytest

from core.handle import pushHandle
from core.presence_arrival import PresenceArrivalOrchestrator
from core.presence_registry import PresenceReport


NOW = datetime(2026, 8, 30, 1, 10, 30, tzinfo=timezone.utc)
WORKSTATION = "desk-storm"
DEVICE_ID = "dc:da:0c:26:9a:60"

_event_ids = count(1)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRegistry:
    def __init__(self, conn) -> None:
        self.conn = conn

    def get(self, device_id):
        return self.conn if device_id == DEVICE_ID else None


class FakeAwayLedger:
    def __init__(self) -> None:
        self.away = False
        self.marked_away = 0

    def is_away(self):
        return self.away

    def mark_away(self, at=None):
        self.marked_away += 1
        self.away = True

    def mark_returned(self, at=None):
        self.away = False

    def compose_speech(self):
        return None

    def mark_reported(self):
        pass


class BaseStateRecorder:
    def __init__(self) -> None:
        self.statuses = []

    def __call__(self, device_id, status, message, emotion):
        self.statuses.append(status)


class AlertRecorder:
    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, conn, text, **kwargs):
        self.calls.append(text)


class _StubLogger:
    def bind(self, **_kwargs):
        return self

    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass


class _StubDialogue:
    def __init__(self) -> None:
        self.dialogue = []

    def put(self, message):
        self.dialogue.append(message)


class _FrameRecorder:
    def __init__(self) -> None:
        self.frames = []

    async def send(self, payload):
        self.frames.append(json.loads(payload))


class FakeDeviceConn:
    """够 push_work_event 跑通的最小连接假体。"""

    def __init__(self) -> None:
        self.device_id = DEVICE_ID
        self.session_id = "sess-storm"
        self.tts = object()
        self.client_is_speaking = False
        self.client_have_voice = False
        self.config = {
            "dialogue_gate": {"enabled": True, "window_seconds": 60},
            "push_speak": {"wait_seconds": 0.01, "poll_interval": 0.005},
        }
        self.logger = _StubLogger()
        self.dialogue = _StubDialogue()
        self.websocket = _FrameRecorder()


def make_report(state, *, previous_state=None, reason=None):
    if previous_state is None:
        previous_state = "absent" if state == "present" else "present"
    if reason is None:
        reason = "pose_confirmed" if state != previous_state else "heartbeat"
    payload = {
        "schema_version": "1.0",
        "event_id": f"6c618629-ffef-4c00-ab4f-{next(_event_ids):012d}",
        "agent_instance_id": "45912c0c-144b-4ac7-970b-527add7b4dcc",
        "workstation_id": WORKSTATION,
        "source": "camera_pose",
        "state": state,
        "previous_state": previous_state,
        "changed": state != previous_state,
        "reason": reason,
        "sequence": 1,
        "observed_at": NOW.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "metrics": {},
    }
    return PresenceReport.from_payload(payload, NOW)


ACCEPTED = {"accepted": True, "duplicate": False}


@pytest.fixture
def spoken_push(monkeypatch):
    """让播报必然“说出口”，只考察它开的那扇窗对离席判定的影响。"""

    async def fake_ensure_speakable(conn):
        return None

    async def fake_speak(conn, text):
        return True

    monkeypatch.setattr(pushHandle, "ensure_speakable", fake_ensure_speakable)
    monkeypatch.setattr(pushHandle, "speak_on_device", fake_speak)


@pytest.mark.asyncio
async def test_alert_storm_still_confirms_absence_and_opens_the_away_window(
    spoken_push,
):
    """空工位 + 每 30 秒一条带声告警：离席判定照常落地。

    修复前：每条告警播完开 60 秒 robot_spoke_first 窗口 → 语音活跃探针判「人还在」
    → absent_since 被重置 → 台账不开窗、基态不切休眠，一直循环。
    """
    conn = FakeDeviceConn()
    ledger = FakeAwayLedger()
    clock = FakeClock()
    base_state = BaseStateRecorder()
    push_alert = AlertRecorder()
    orchestrator = PresenceArrivalOrchestrator(
        {
            "presence_robot": {
                "enabled": True,
                "workstations": {WORKSTATION: DEVICE_ID},
                "absent_grace_seconds": 45,
                "voice_active_seconds": 2,
                "sleep_on_absent": True,
            }
        },
        FakeRegistry(conn),
        push_alert=push_alert,
        set_base_state=base_state,
        clock=clock,
        away_ledger=ledger,
    )

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    for _ in range(3):
        await pushHandle.push_work_event(
            conn, "生产告警：错误率升高", status="告警", speak=True
        )
        clock.advance(30)
        await orchestrator.on_report(
            make_report("absent", previous_state="absent", reason="heartbeat"),
            ACCEPTED,
        )

    assert ledger.marked_away == 1  # 离席窗口开了，风暴期间的告警才进得了返岗汇总
    assert "休眠" in base_state.statuses
    assert len(push_alert.calls) == 1  # 休眠画面下发过一次


@pytest.mark.asyncio
async def test_user_answering_the_alert_still_defers_absence(spoken_push):
    """机器人开的窗里用户真的应答了：VAD 判据命中，离席判定照旧推迟。"""
    conn = FakeDeviceConn()
    ledger = FakeAwayLedger()
    clock = FakeClock()
    base_state = BaseStateRecorder()
    push_alert = AlertRecorder()
    orchestrator = PresenceArrivalOrchestrator(
        {
            "presence_robot": {
                "enabled": True,
                "workstations": {WORKSTATION: DEVICE_ID},
                "absent_grace_seconds": 45,
                "voice_active_seconds": 2,
                "sleep_on_absent": True,
            }
        },
        FakeRegistry(conn),
        push_alert=push_alert,
        set_base_state=base_state,
        clock=clock,
        away_ledger=ledger,
    )

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    await pushHandle.push_work_event(
        conn, "生产告警：错误率升高", status="告警", speak=True
    )
    conn.client_have_voice = True  # 主人就在工位上，正在回话
    clock.advance(46)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )

    assert ledger.marked_away == 0
    assert push_alert.calls == []
