"""在岗状态驱动机器人迎接与休眠的编排测试。

全部离线：注入假时钟与假推送函数，不依赖真机、网络与 LLM。
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from core.handle import pushHandle
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
    """离开足够久(超过迎接冷却)再回来,才重新问候。"""
    orchestrator, clock, push_event, push_alert, _conn = env

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(90)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )
    # 冷却 180s 从上次问候起算:再多等一会儿,总离开时长越过冷却
    clock.advance(95)
    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    assert len(push_event.calls) == 2
    assert len(push_alert.calls) == 1


@pytest.mark.asyncio
async def test_short_cycle_return_stays_quiet(env):
    """姿态抖动型循环:宽限期虚过+快速回归,冷却期内不再出声。

    真机上坐得近会丢关键点,present/absent 秒级抖动曾造成几分钟内
    连续迎接十几次的复读机。冷却期内只静默重挂标记。
    """
    orchestrator, clock, push_event, push_alert, _conn = env

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(90)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )
    clock.advance(2)
    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    # 冷却期内只静默重挂标记:不再出声,但休眠标记要复位——
    # 下一轮确认离席仍然要能推休眠
    assert len(push_event.calls) == 1
    assert len(push_alert.calls) == 1

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(90)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )
    assert len(push_alert.calls) == 2


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
    # 冷却期内返岗:汇总已清空,不再有任何播报;越过冷却后才重新问候
    clock.advance(5)
    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    assert push_event.texts == [
        "早上好，今天也一起把事情搞定吧。你离开的三十五分钟里，有一条留言。",
    ]

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(90)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )
    clock.advance(95)
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


# ---------------------------------------------------------------- 基态注册
#
# wellbeing/审批/告警/晨报的提醒都带 restore_after,播完把画面恢复到设备基态。
# 基态不跟着到岗/离席走的话,任何一条提醒播完,屏幕都会回落默认「待机」——
# 人明明在工位,机器人却写着待机,直到下一个事件才纠正。


@pytest.fixture(autouse=True)
def _isolate_base_states():
    """构造器缺省时走真实 pushHandle 的模块级基态存储,测试间必须清空。"""
    pushHandle._base_states.clear()
    yield
    pushHandle._base_states.clear()
    for task in list(pushHandle._restore_tasks.values()):
        try:
            task.cancel()
        except RuntimeError:
            pass  # 事件循环已随用例关闭,残留任务不再需要取消
    pushHandle._restore_tasks.clear()


class BaseStateRecorder:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, device_id, status, message, emotion):
        self.calls.append(
            {
                "device_id": device_id,
                "status": status,
                "message": message,
                "emotion": emotion,
            }
        )

    @property
    def statuses(self):
        return [call["status"] for call in self.calls]


def _base_state_env(*, config=None, conn=..., visitor_flow=None, owner_status=None):
    if conn is ...:
        conn = object()
    clock = FakeClock()
    recorder = BaseStateRecorder()
    orchestrator = PresenceArrivalOrchestrator(
        config or make_config(),
        FakeRegistry(conn),
        push_event=Recorder(),
        push_alert=Recorder(),
        clock=clock,
        set_base_state=recorder,
        visitor_flow=visitor_flow,
        owner_status_store=owner_status,
    )
    return orchestrator, clock, recorder


@pytest.mark.asyncio
async def test_owner_arrival_registers_on_duty_base_state():
    orchestrator, _clock, base = _base_state_env()

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    # emotion 必须是 neutral:基态恢复是静默收画面,happy 会每次都触发 HeadUp 舵机动作
    assert base.calls == [
        {"device_id": DEVICE_ID, "status": "在岗", "message": "", "emotion": "neutral"}
    ]


@pytest.mark.asyncio
async def test_generic_greeting_also_registers_on_duty_base_state():
    """识别不清的人也被迎接(通用问候),画面同样常驻「在岗」。"""
    orchestrator, _clock, base = _base_state_env()

    await orchestrator.on_report(make_report(identity_state="unknown"), ACCEPTED)

    assert base.statuses == ["在岗"]


@pytest.mark.asyncio
async def test_unsettled_identity_does_not_register_base_state():
    orchestrator, _clock, base = _base_state_env()

    await orchestrator.on_report(make_report(identity_state="no_face"), ACCEPTED)

    assert base.calls == []


@pytest.mark.asyncio
async def test_visitor_flow_does_not_touch_base_state():
    """主人不在时来的是访客:基态描述的是主人的状态,不能被访客改成「在岗」。"""
    orchestrator, _clock, base = _base_state_env(
        visitor_flow=FakeVisitorFlow(), owner_status=FakeOwnerStatus("meeting")
    )

    await orchestrator.on_report(make_report(identity_state="unknown"), ACCEPTED)

    assert base.calls == []


@pytest.mark.asyncio
async def test_offline_device_still_registers_base_state():
    """基态按 device_id 存、与连接无关:设备离线也要注册,上线后第一次恢复就是对的。"""
    orchestrator, _clock, base = _base_state_env(conn=None)

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    assert base.statuses == ["在岗"]


@pytest.mark.asyncio
async def test_confirmed_absence_switches_base_state_to_sleep_screen():
    orchestrator, clock, base = _base_state_env()

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(90)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )

    # sleepy 让恢复时顺带低头回到睡姿;message 保留休眠文案,画面和刚睡着时一致
    assert base.calls[-1] == {
        "device_id": DEVICE_ID,
        "status": "休眠",
        "message": "工位没人，我先眯一会儿。",
        "emotion": "sleepy",
    }


@pytest.mark.asyncio
async def test_absence_within_grace_keeps_on_duty_base_state():
    """弯腰捡东西不算离开,基态不该跟着切到休眠。"""
    orchestrator, clock, base = _base_state_env()

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(30)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )

    assert base.statuses == ["在岗"]


@pytest.mark.asyncio
async def test_quiet_return_in_cooldown_still_restores_on_duty_base_state():
    """姿态抖动虚过宽限期后快速返岗:冷却期内不出声,但基态必须切回「在岗」。"""
    orchestrator, clock, base = _base_state_env()

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(90)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )
    assert base.calls[-1]["status"] == "休眠"

    clock.advance(2)
    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)

    assert base.calls[-1]["status"] == "在岗"


@pytest.mark.asyncio
async def test_absence_without_sleep_falls_back_to_default_base_state():
    """sleep_on_absent 关掉时,确认离席要回落默认待机基态(走真实默认接线验证)。"""
    conn = object()
    clock = FakeClock()
    # 不注入 set_base_state:验证生产默认实现真的写进 pushHandle
    orchestrator = PresenceArrivalOrchestrator(
        make_config(sleep_on_absent=False),
        FakeRegistry(conn),
        push_event=Recorder(),
        push_alert=Recorder(),
        clock=clock,
    )

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    assert pushHandle.get_base_state(DEVICE_ID)["status"] == "在岗"

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(90)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )

    assert pushHandle.get_base_state(DEVICE_ID) == pushHandle.DEFAULT_BASE_STATE


# ------------------------------------------- 基态与恢复的端到端时序


class _StubLogger:
    def bind(self, **kwargs):
        return self

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class _StubDialogue:
    def put(self, message):
        pass


class _FrameRecorder:
    def __init__(self) -> None:
        self.frames = []

    async def send(self, payload):
        self.frames.append(json.loads(payload))


class FakeDeviceConn:
    """够 push_work_event 全链路跑通的最小连接假体。"""

    def __init__(self, device_id=DEVICE_ID) -> None:
        self.device_id = device_id
        self.session_id = "sess-test"
        self.tts = None  # ensure_speakable 会因此判忙,播报自动降级为纯提示
        self.client_is_speaking = False
        self.client_have_voice = False
        # 播报等待缩到 10ms,否则 TTS 缺失要空等默认 3 秒
        self.config = {"push_speak": {"wait_seconds": 0.01, "poll_interval": 0.005}}
        self.logger = _StubLogger()
        self.dialogue = _StubDialogue()
        self.websocket = _FrameRecorder()

    @property
    def sent(self):
        return self.websocket.frames


@pytest.mark.asyncio
async def test_greeting_then_reminder_restore_returns_to_on_duty():
    """迎接 → wellbeing 式提醒(带 restore_after) → 恢复后回到「在岗」而非「待机」。

    走真实 pushHandle 全链路(不注入推送与基态函数)。wellbeing/审批/告警/晨报
    的提醒都是同一机制:push_work_event + restore_after,这里用缩短的延时代表它们。
    """
    conn = FakeDeviceConn()
    orchestrator = PresenceArrivalOrchestrator(
        make_config(), FakeRegistry(conn), clock=FakeClock()
    )

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    greeting = conn.sent[-1]
    assert greeting["status"] == "在岗"
    assert greeting["message"] == "早上好，今天也一起把事情搞定吧。"

    await pushHandle.push_work_event(
        conn, "起来活动一下吧", emotion="happy", status="健康提醒", restore_after=0.05
    )
    assert conn.sent[-1]["status"] == "健康提醒"

    await asyncio.sleep(0.2)
    restored = conn.sent[-1]
    assert restored["status"] == "在岗"  # 修复前这里是默认基态「待机」
    assert restored["silent"] is True  # 基态恢复是收画面,不该响提示音
    assert restored["message"] == ""


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


# ------------------------------------------- 语音对话活跃时推迟离席判定

class VoiceProbe:
    """可切换结果的语音活跃探针，记录每次调用参数。"""

    def __init__(self, active=False) -> None:
        self.active = active
        self.calls = []

    def __call__(self, conn, within_seconds):
        self.calls.append((conn, within_seconds))
        return self.active


def build_voice(*, probe, ledger=None, conn=None, registry=None, **config_overrides):
    conn = object() if conn is None else conn
    clock = FakeClock()
    push_event = Recorder()
    push_alert = Recorder()
    base_state = BaseStateRecorder()
    orchestrator = PresenceArrivalOrchestrator(
        make_config(absent_grace_seconds=45, **config_overrides),
        registry or FakeRegistry(conn),
        push_event=push_event,
        push_alert=push_alert,
        set_base_state=base_state,
        clock=clock,
        away_ledger=ledger,
        voice_active_probe=probe,
    )
    return orchestrator, clock, push_event, push_alert, base_state


@pytest.mark.asyncio
async def test_voice_active_defers_absent_confirmation():
    """真机 8-19 实录：低头凑近机器人说话会丢姿态，摄像头连报 absent，
    45 秒宽限期内喊了三次唤醒词照样被判「持续无人」——休眠画面顶掉聆听画面，
    离席台账还把人在场的时段记成「离席期间」。语音活跃必须推迟整套判定。"""
    conn = object()
    ledger = FakeAwayLedger()
    probe = VoiceProbe(active=True)
    orchestrator, clock, push_event, push_alert, base_state = build_voice(
        probe=probe, ledger=ledger, conn=conn, voice_active_seconds=25
    )

    await orchestrator.on_report(make_report(identity_state="owner"), ACCEPTED)
    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(45)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )

    assert push_alert.calls == []  # 没推休眠画面
    assert ledger.marked_away == 0  # 没开离席窗口
    assert "休眠" not in base_state.statuses  # 基态没切休眠
    assert probe.calls == [(conn, 25.0)]  # 探针拿到的是配置的回看窗口


@pytest.mark.asyncio
async def test_absence_confirmed_after_voice_quiets_down():
    """语音停了不立刻睡：从推迟点重新走满一个宽限期才确认离席。"""
    ledger = FakeAwayLedger()
    probe = VoiceProbe(active=True)
    orchestrator, clock, _push_event, push_alert, _base_state = build_voice(
        probe=probe, ledger=ledger
    )

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(45)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )
    assert push_alert.calls == []  # 语音活跃，推迟并重置计时

    probe.active = False
    clock.advance(30)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )
    assert push_alert.calls == []  # 重置后的宽限期还没走满

    clock.advance(15)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )
    assert len(push_alert.calls) == 1  # 走满宽限期，休眠照常
    assert ledger.marked_away == 1


@pytest.mark.asyncio
async def test_offline_device_skips_voice_probe():
    """设备不在线就没有语音证据可看，判定走原路（基态照记，推送自然跳过）。"""
    ledger = FakeAwayLedger()
    probe = VoiceProbe(active=True)
    orchestrator, clock, _push_event, push_alert, base_state = build_voice(
        probe=probe, ledger=ledger, registry=FakeRegistry(None)
    )

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(45)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )

    assert probe.calls == []  # 没有连接就不问语音
    assert ledger.marked_away == 1  # 离席判定照旧
    assert "休眠" in base_state.statuses  # 基态照样切休眠
    assert push_alert.calls == []  # 离线本来就推不了


@pytest.mark.asyncio
async def test_probe_failure_falls_back_to_camera_only():
    """探针坏了不能让设备永远睡不着：按无语音处理，判定走原路。"""
    def boom(conn, within_seconds):
        raise RuntimeError("conn attr missing")

    orchestrator, clock, _push_event, push_alert, _base_state = build_voice(probe=boom)

    await orchestrator.on_report(make_report("absent"), ACCEPTED)
    clock.advance(45)
    await orchestrator.on_report(
        make_report("absent", previous_state="absent", reason="heartbeat"), ACCEPTED
    )

    assert len(push_alert.calls) == 1
