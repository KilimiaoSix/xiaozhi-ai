"""在岗状态驱动机器人迎接与休眠的编排测试。

全部离线：注入假时钟与假推送函数，不依赖真机、网络与 LLM。
"""

from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from core.presence_arrival import (
    PresenceArrivalOrchestrator,
    create_presence_arrival_orchestrator,
)
from core.presence_registry import PresenceReport


NOW = datetime(2026, 8, 18, 1, 10, 30, tzinfo=timezone.utc)
WORKSTATION = "desk-test"
DEVICE_ID = "dc:da:0c:26:9a:60"

_FACE_COUNT = {
    "owner": 1,
    "unknown": 1,
    "multiple_faces": 2,
    "starting": 0,
    "not_enrolled": 0,
    "no_face": 0,
    "camera_error": 0,
}


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRegistry:
    """只实现编排器用到的 get()。"""

    def __init__(self, conn=None) -> None:
        self.conn = conn

    def get(self, device_id):
        return self.conn if device_id == DEVICE_ID else None


class Recorder:
    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, conn, text, **kwargs):
        self.calls.append({"conn": conn, "text": text, **kwargs})
        return True

    @property
    def texts(self):
        return [call["text"] for call in self.calls]


_event_ids = count(1)


def make_report(
    state="present",
    *,
    identity_state=None,
    previous_state=None,
    reason=None,
    sequence=1,
    observed_at=NOW,
):
    """构造一条通过 schema 1.0 校验的上报。"""
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
        "sequence": sequence,
        "observed_at": observed_at.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "metrics": {},
    }
    if identity_state is not None:
        identity = {
            "state": identity_state,
            "previous_state": identity_state,
            "changed": False,
            "face_count": _FACE_COUNT[identity_state],
        }
        if identity_state in {"owner", "unknown"}:
            identity["similarity"] = 0.82 if identity_state == "owner" else 0.11
        payload["identity"] = identity
    return PresenceReport.from_payload(payload, NOW)


ACCEPTED = {"accepted": True, "duplicate": False}
DUPLICATE = {"accepted": True, "duplicate": True}


def make_config(**overrides):
    presence_robot = {
        "enabled": True,
        "workstations": {WORKSTATION: DEVICE_ID},
        "identity_wait_seconds": 5,
        "absent_grace_seconds": 90,
        "sleep_on_absent": True,
        "greeting_owner": "早上好，今天也一起把事情搞定吧。",
        "greeting_generic": "你好，我在这儿。",
    }
    presence_robot.update(overrides)
    return {"presence_robot": presence_robot}


@pytest.fixture
def env():
    conn = object()
    clock = FakeClock()
    push_event = Recorder()
    push_alert = Recorder()
    orchestrator = PresenceArrivalOrchestrator(
        make_config(),
        FakeRegistry(conn),
        push_event=push_event,
        push_alert=push_alert,
        clock=clock,
    )
    return orchestrator, clock, push_event, push_alert, conn


# ---------------------------------------------------------------- 到岗

@pytest.mark.asyncio
async def test_owner_arrival_greets_once_with_owner_greeting(env):
    orchestrator, _clock, push_event, push_alert, conn = env

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    assert push_alert.calls == []
    assert len(push_event.calls) == 1
    call = push_event.calls[0]
    assert call["conn"] is conn
    assert call["text"] == "早上好，今天也一起把事情搞定吧。"
    # happy 表情在固件里会触发 HeadUp(15)，抬头与笑脸一条消息完成
    assert call["emotion"] == "happy"
    assert call["speak"] is True


@pytest.mark.asyncio
async def test_same_arrival_does_not_greet_twice(env):
    orchestrator, clock, push_event, _push_alert, _conn = env

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    clock.advance(10)
    await orchestrator.on_report(
        make_report(identity_state="owner", previous_state="present", reason="heartbeat"),
        ACCEPTED,
    )

    assert len(push_event.calls) == 1


@pytest.mark.asyncio
async def test_unsettled_identity_waits_before_greeting(env):
    orchestrator, _clock, push_event, _push_alert, _conn = env

    await orchestrator.on_report(make_report(identity_state="no_face"), ACCEPTED)

    assert push_event.calls == []


@pytest.mark.asyncio
async def test_identity_settling_to_owner_greets_with_owner_greeting(env):
    orchestrator, clock, push_event, _push_alert, _conn = env

    await orchestrator.on_report(make_report(identity_state="starting"), ACCEPTED)
    clock.advance(1)
    await orchestrator.on_report(
        make_report(identity_state="owner", previous_state="present", reason="identity_changed"),
        ACCEPTED,
    )

    assert push_event.texts == ["早上好，今天也一起把事情搞定吧。"]


@pytest.mark.asyncio
async def test_identity_never_settles_falls_back_to_generic_greeting(env):
    orchestrator, clock, push_event, _push_alert, _conn = env

    await orchestrator.on_report(make_report(identity_state="no_face"), ACCEPTED)
    clock.advance(5)
    await orchestrator.on_report(
        make_report(identity_state="no_face", previous_state="present", reason="heartbeat"),
        ACCEPTED,
    )

    assert push_event.texts == ["你好，我在这儿。"]


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_state", ["unknown", "not_enrolled", "multiple_faces"])
async def test_non_owner_gets_generic_greeting_without_name(env, identity_state):
    orchestrator, _clock, push_event, _push_alert, _conn = env

    await orchestrator.on_report(make_report(identity_state=identity_state), ACCEPTED)

    assert push_event.texts == ["你好，我在这儿。"]


@pytest.mark.asyncio
async def test_missing_identity_block_falls_back_to_generic_after_wait(env):
    """presence-agent 未启用人脸校验时不带 identity，不能因此永远不打招呼。"""
    orchestrator, clock, push_event, _push_alert, _conn = env

    await orchestrator.on_report(make_report(), ACCEPTED)
    assert push_event.calls == []

    clock.advance(5)
    await orchestrator.on_report(
        make_report(previous_state="present", reason="heartbeat"), ACCEPTED
    )
    assert push_event.texts == ["你好，我在这儿。"]


# ---------------------------------------------------------------- 离岗

@pytest.mark.asyncio
async def test_absent_within_grace_does_not_sleep(env):
    orchestrator, clock, _push_event, push_alert, _conn = env

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(30)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )

    assert push_alert.calls == []


@pytest.mark.asyncio
async def test_sustained_absence_sleeps_once(env):
    orchestrator, clock, _push_event, push_alert, _conn = env

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(90)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )
    clock.advance(15)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )

    assert len(push_alert.calls) == 1
    call = push_alert.calls[0]
    # sleepy 在固件里触发 HeadDown，呈现低头睡着；silent 避免每次都响提示音
    assert call["emotion"] == "sleepy"
    assert call["silent"] is True


@pytest.mark.asyncio
async def test_returning_after_sustained_absence_greets_again(env):
    orchestrator, clock, push_event, push_alert, _conn = env

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(90)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )
    clock.advance(5)
    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    assert len(push_event.calls) == 2
    assert len(push_alert.calls) == 1


@pytest.mark.asyncio
async def test_brief_absence_does_not_re_greet(env):
    """人只是弯腰捡个东西，回来不该再被问候一次。"""
    orchestrator, clock, push_event, push_alert, _conn = env

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(5)
    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    assert len(push_event.calls) == 1
    assert push_alert.calls == []


@pytest.mark.asyncio
async def test_sleep_can_be_disabled(env):
    orchestrator, clock, push_event, push_alert, conn = env
    orchestrator = PresenceArrivalOrchestrator(
        make_config(sleep_on_absent=False),
        FakeRegistry(conn),
        push_event=push_event,
        push_alert=push_alert,
        clock=clock,
    )

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(90)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )

    assert push_alert.calls == []


# ---------------------------------------------------------------- 忽略与降级

@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["starting", "camera_error"])
async def test_unreliable_states_produce_no_commands(env, state):
    orchestrator, _clock, push_event, push_alert, _conn = env

    await orchestrator.on_report(
        make_report(state, previous_state="absent", reason="initializing"), ACCEPTED
    )

    assert push_event.calls == []
    assert push_alert.calls == []


@pytest.mark.asyncio
async def test_duplicate_report_is_skipped(env):
    orchestrator, _clock, push_event, _push_alert, _conn = env

    await orchestrator.on_report(make_report(identity_state="owner"), DUPLICATE)

    assert push_event.calls == []


@pytest.mark.asyncio
async def test_unmapped_workstation_is_skipped(env):
    orchestrator, clock, push_event, push_alert, conn = env
    orchestrator = PresenceArrivalOrchestrator(
        make_config(workstations={"other-desk": DEVICE_ID}),
        FakeRegistry(conn),
        push_event=push_event,
        push_alert=push_alert,
        clock=clock,
    )

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    assert push_event.calls == []


@pytest.mark.asyncio
async def test_offline_device_greets_once_it_comes_online(env):
    """设备离线时不能把已问候标记置上，否则设备上线后永远收不到迎接。"""
    orchestrator, clock, push_event, push_alert, conn = env
    registry = FakeRegistry(None)
    orchestrator = PresenceArrivalOrchestrator(
        make_config(),
        registry,
        push_event=push_event,
        push_alert=push_alert,
        clock=clock,
    )

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    assert push_event.calls == []

    registry.conn = conn
    clock.advance(2)
    await orchestrator.on_report(
        make_report(identity_state="owner", previous_state="present", reason="heartbeat"),
        ACCEPTED,
    )

    assert len(push_event.calls) == 1


@pytest.mark.asyncio
async def test_push_failure_does_not_propagate(env):
    """编排失败绝不能把 /xiaozhi/presence/report 变成 500。"""
    orchestrator, clock, _push_event, push_alert, conn = env

    async def boom(*args, **kwargs):
        raise RuntimeError("websocket closed")

    orchestrator = PresenceArrivalOrchestrator(
        make_config(),
        FakeRegistry(conn),
        push_event=boom,
        push_alert=push_alert,
        clock=clock,
    )

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)


@pytest.mark.asyncio
async def test_multiple_workstations_track_state_independently(env):
    orchestrator, clock, push_event, push_alert, conn = env
    second = "desk-other"
    second_device = "aa:bb:cc:dd:ee:01"

    class TwoDeviceRegistry:
        def get(self, device_id):
            return conn if device_id in {DEVICE_ID, second_device} else None

    orchestrator = PresenceArrivalOrchestrator(
        make_config(workstations={WORKSTATION: DEVICE_ID, second: second_device}),
        TwoDeviceRegistry(),
        push_event=push_event,
        push_alert=push_alert,
        clock=clock,
    )

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    report = make_report(identity_state="owner")
    object.__setattr__(report, "workstation_id", second)
    await orchestrator.on_report(report, ACCEPTED)

    assert len(push_event.calls) == 2


# ------------------------------------------------- 离席台账 / 来访者 / 返岗汇总

class FakeAwayLedger:
    def __init__(self, *, speech=None, away=False) -> None:
        self.speech = speech
        self.away = away
        self.marked_away = 0
        self.marked_returned = 0
        self.marked_reported = 0

    def is_away(self):
        return self.away

    def mark_away(self, at=None):
        self.marked_away += 1
        self.away = True

    def mark_returned(self, at=None):
        self.marked_returned += 1
        self.away = False

    def compose_speech(self):
        return self.speech

    def mark_reported(self):
        self.marked_reported += 1
        self.speech = None


class FakeVisitorFlow:
    def __init__(self) -> None:
        self.calls = []

    async def on_visitor_detected(self, workstation_id):
        self.calls.append(workstation_id)
        return True


class FakeOwnerStatus:
    def __init__(self, state="available") -> None:
        self.state = state

    def get(self):
        return {"state": self.state, "public_note": "", "expected_return": None}


def build(*, ledger=None, visitor_flow=None, owner_status=None, conn=None,
          push_event=None, push_alert=None, clock=None):
    conn = object() if conn is None else conn
    clock = clock or FakeClock()
    push_event = push_event or Recorder()
    push_alert = push_alert or Recorder()
    orchestrator = PresenceArrivalOrchestrator(
        make_config(),
        FakeRegistry(conn),
        push_event=push_event,
        push_alert=push_alert,
        clock=clock,
        away_ledger=ledger,
        visitor_flow=visitor_flow,
        owner_status_store=owner_status,
    )
    return orchestrator, clock, push_event, push_alert


@pytest.mark.asyncio
async def test_confirmed_absence_marks_away_on_ledger():
    ledger = FakeAwayLedger()
    orchestrator, clock, _push_event, _push_alert = build(ledger=ledger)

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(90)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )

    assert ledger.marked_away == 1
    assert ledger.is_away() is True


@pytest.mark.asyncio
async def test_absence_within_grace_does_not_mark_away():
    """弯腰捡东西不算离席，不该开始攒返岗汇总。"""
    ledger = FakeAwayLedger()
    orchestrator, clock, _push_event, _push_alert = build(ledger=ledger)

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(30)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )

    assert ledger.marked_away == 0


@pytest.mark.asyncio
async def test_repeated_absent_heartbeats_mark_away_once():
    """离席心跳每 15 秒一条，起点不能被一路往后推。"""
    ledger = FakeAwayLedger()
    orchestrator, clock, _push_event, _push_alert = build(ledger=ledger)

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    for _ in range(3):
        clock.advance(90)
        await orchestrator.on_report(
            make_report("absent", previous_state="absent", reason="heartbeat"),
            ACCEPTED,
        )

    assert ledger.marked_away == 1


@pytest.mark.asyncio
async def test_owner_return_appends_summary_to_greeting():
    ledger = FakeAwayLedger(
        speech="你离开的三十五分钟里，Codex 完成了 2 个任务。", away=True
    )
    orchestrator, _clock, push_event, _push_alert = build(ledger=ledger)

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    assert push_event.texts == [
        "早上好，今天也一起把事情搞定吧。你离开的三十五分钟里，Codex 完成了 2 个任务。"
    ]


@pytest.mark.asyncio
async def test_owner_return_marks_reported_and_returned():
    ledger = FakeAwayLedger(speech="你离开的三十五分钟里，有一条留言。", away=True)
    orchestrator, _clock, _push_event, _push_alert = build(ledger=ledger)

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    assert ledger.marked_reported == 1
    assert ledger.marked_returned == 1
    assert ledger.is_away() is False


@pytest.mark.asyncio
async def test_owner_return_without_pending_still_closes_away_window():
    """没有待播报事项也要结束离席窗口，否则来访者流程会一直误触发。"""
    ledger = FakeAwayLedger(away=True)
    orchestrator, _clock, push_event, _push_alert = build(ledger=ledger)

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    assert push_event.texts == ["早上好，今天也一起把事情搞定吧。"]
    assert ledger.marked_returned == 1
    assert ledger.marked_reported == 0


@pytest.mark.asyncio
async def test_push_failure_keeps_summary_pending():
    """推送失败后台账不能被清空，否则同事的留言就永远消失了。"""
    ledger = FakeAwayLedger(speech="你离开的三十五分钟里，有一条留言。", away=True)

    async def boom(*args, **kwargs):
        raise RuntimeError("websocket closed")

    orchestrator, _clock, _push_event, _push_alert = build(
        ledger=ledger, push_event=boom
    )

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    assert ledger.marked_reported == 0
    assert ledger.compose_speech() is not None


@pytest.mark.asyncio
async def test_owner_return_is_summarised_only_once():
    ledger = FakeAwayLedger(speech="你离开的三十五分钟里，有一条留言。", away=True)
    orchestrator, clock, push_event, _push_alert = build(ledger=ledger)

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(90)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )
    clock.advance(5)
    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    assert push_event.texts == [
        "早上好，今天也一起把事情搞定吧。你离开的三十五分钟里，有一条留言。",
        "早上好，今天也一起把事情搞定吧。",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_state", ["unknown", "multiple_faces"])
async def test_visitor_triggers_visitor_flow_instead_of_greeting(identity_state):
    visitor_flow = FakeVisitorFlow()
    orchestrator, _clock, push_event, _push_alert = build(
        visitor_flow=visitor_flow, owner_status=FakeOwnerStatus("meeting")
    )

    await orchestrator.on_report(make_report(identity_state=identity_state), ACCEPTED)

    assert visitor_flow.calls == [WORKSTATION]
    assert push_event.calls == []


@pytest.mark.asyncio
async def test_visitor_flow_triggers_when_ledger_says_away():
    """主人没声明状态，但摄像头已经确认他离席了，来的人也是访客。"""
    visitor_flow = FakeVisitorFlow()
    orchestrator, _clock, push_event, _push_alert = build(
        visitor_flow=visitor_flow,
        ledger=FakeAwayLedger(away=True),
        owner_status=FakeOwnerStatus("available"),
    )

    await orchestrator.on_report(make_report(identity_state="unknown"), ACCEPTED)

    assert visitor_flow.calls == [WORKSTATION]
    assert push_event.calls == []


@pytest.mark.asyncio
async def test_stranger_gets_plain_greeting_when_owner_is_available():
    """主人在岗时旁边路过一个人，不该被当成来访者播报离席文案。"""
    visitor_flow = FakeVisitorFlow()
    orchestrator, _clock, push_event, _push_alert = build(
        visitor_flow=visitor_flow,
        ledger=FakeAwayLedger(away=False),
        owner_status=FakeOwnerStatus("available"),
    )

    await orchestrator.on_report(make_report(identity_state="unknown"), ACCEPTED)

    assert visitor_flow.calls == []
    assert push_event.texts == ["你好，我在这儿。"]


@pytest.mark.asyncio
async def test_visitor_flow_waits_for_identity_to_settle():
    visitor_flow = FakeVisitorFlow()
    orchestrator, _clock, push_event, _push_alert = build(
        visitor_flow=visitor_flow, owner_status=FakeOwnerStatus("meeting")
    )

    await orchestrator.on_report(make_report(identity_state="starting"), ACCEPTED)

    assert visitor_flow.calls == []
    assert push_event.calls == []


@pytest.mark.asyncio
async def test_unsettled_identity_timeout_does_not_trigger_visitor_flow():
    """人脸一直看不清时只能按"认不准"发通用问候，不能凭空断定是来访者。"""
    visitor_flow = FakeVisitorFlow()
    orchestrator, clock, push_event, _push_alert = build(
        visitor_flow=visitor_flow, owner_status=FakeOwnerStatus("meeting")
    )

    await orchestrator.on_report(make_report(identity_state="no_face"), ACCEPTED)
    clock.advance(5)
    await orchestrator.on_report(
        make_report(identity_state="no_face", previous_state="present",
                    reason="heartbeat"),
        ACCEPTED,
    )

    assert visitor_flow.calls == []
    assert push_event.texts == ["你好，我在这儿。"]


@pytest.mark.asyncio
async def test_visitor_flow_does_not_mark_arrival_greeted():
    """访客占着工位时主人回来，仍然要被迎接。"""
    visitor_flow = FakeVisitorFlow()
    ledger = FakeAwayLedger(away=True)
    orchestrator, clock, push_event, _push_alert = build(
        visitor_flow=visitor_flow, ledger=ledger,
        owner_status=FakeOwnerStatus("meeting"),
    )

    await orchestrator.on_report(make_report(identity_state="unknown"), ACCEPTED)
    clock.advance(10)
    await orchestrator.on_report(
        make_report(identity_state="owner", previous_state="present",
                    reason="identity_changed"),
        ACCEPTED,
    )

    assert push_event.texts == ["早上好，今天也一起把事情搞定吧。"]


@pytest.mark.asyncio
async def test_visitor_flow_failure_does_not_propagate():
    class BoomFlow:
        async def on_visitor_detected(self, workstation_id):
            raise RuntimeError("push failed")

    orchestrator, _clock, _push_event, _push_alert = build(
        visitor_flow=BoomFlow(), owner_status=FakeOwnerStatus("meeting")
    )

    await orchestrator.on_report(make_report(identity_state="unknown"), ACCEPTED)


@pytest.mark.asyncio
async def test_broken_owner_status_does_not_break_arrival():
    class BoomStatus:
        def get(self):
            raise RuntimeError("status file gone")

    visitor_flow = FakeVisitorFlow()
    orchestrator, _clock, push_event, _push_alert = build(
        visitor_flow=visitor_flow, owner_status=BoomStatus()
    )

    await orchestrator.on_report(make_report(identity_state="unknown"), ACCEPTED)

    assert visitor_flow.calls == []
    assert push_event.texts == ["你好，我在这儿。"]


@pytest.mark.asyncio
async def test_without_injections_behaviour_is_unchanged(env):
    """三个新依赖缺省时，迎接与休眠必须和接入前一模一样。"""
    orchestrator, clock, push_event, push_alert, _conn = env

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(90)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )

    assert push_event.texts == ["早上好，今天也一起把事情搞定吧。"]
    assert len(push_alert.calls) == 1


# ---------------------------------------------------------------- 工厂

def test_factory_returns_none_when_disabled():
    assert create_presence_arrival_orchestrator(
        make_config(enabled=False), FakeRegistry(object())
    ) is None


def test_factory_returns_none_without_device_registry():
    assert create_presence_arrival_orchestrator(make_config(), None) is None


def test_factory_returns_none_when_no_workstation_mapped():
    assert create_presence_arrival_orchestrator(
        make_config(workstations={}), FakeRegistry(object())
    ) is None


def test_factory_builds_orchestrator_when_configured():
    orchestrator = create_presence_arrival_orchestrator(
        make_config(), FakeRegistry(object())
    )
    assert isinstance(orchestrator, PresenceArrivalOrchestrator)


def test_factory_returns_none_when_section_absent():
    assert create_presence_arrival_orchestrator({}, FakeRegistry(object())) is None
