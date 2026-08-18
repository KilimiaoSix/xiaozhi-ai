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
