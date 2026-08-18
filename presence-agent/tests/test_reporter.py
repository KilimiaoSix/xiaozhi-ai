from datetime import datetime, timedelta, timezone

import pytest

from presence_agent.reporter import PresenceReporter, ReportTransportError
from presence_agent.snapshot import LatestSnapshot
from presence_agent.state import PresenceState


NOW = datetime(2026, 8, 18, 1, 10, 30, tzinfo=timezone.utc)
INSTANCE_ID = "45912c0c-144b-4ac7-970b-527add7b4dcc"
EVENT_IDS = [
    "6c618629-ffef-4c00-ab4f-17dc5ce2eb7a",
    "b98af960-9166-45f3-bfb4-2f9fa6b9938f",
    "7ebd1794-c471-4a73-8ec5-0f0307fd901a",
    "c001289c-449b-4932-a33f-6bc5e6c8775e",
    "0c931a73-9999-44de-b25e-a274389e72e5",
    "25843368-7956-4e96-a13f-352b4fdb6080",
    "c12b5a79-2e18-4a75-a82a-1e601e3c1e96",
    "1881d741-769c-48aa-b445-f9223de2296a",
    "0f876b3b-fac1-48cb-9dce-7021966619f7",
]


class FakeTransport:
    def __init__(self):
        self.attempts = []
        self.failures_remaining = 0

    def send(self, payload):
        self.attempts.append(payload)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise ReportTransportError("offline")
        return {"code": "OK", "message": "success", "data": {"accepted": True}}


@pytest.fixture
def setup_reporter():
    latest = LatestSnapshot(NOW)
    transport = FakeTransport()
    event_ids = iter(EVENT_IDS)
    reporter = PresenceReporter(
        snapshot_provider=latest.read,
        transport=transport,
        workstation_id="desk-test",
        heartbeat_seconds=15.0,
        poll_seconds=0.1,
        agent_instance_id=INSTANCE_ID,
        event_id_factory=lambda: next(event_ids),
    )
    return latest, transport, reporter


def test_initial_snapshot_is_reported_with_versioned_payload(setup_reporter):
    _, transport, reporter = setup_reporter

    delay = reporter.step(0.0)

    assert delay == 0.1
    assert transport.attempts == [
        {
            "schema_version": "1.0",
            "event_id": EVENT_IDS[0],
            "agent_instance_id": INSTANCE_ID,
            "workstation_id": "desk-test",
            "source": "camera_pose",
            "state": "starting",
            "previous_state": "starting",
            "changed": False,
            "reason": "initializing",
            "sequence": 1,
            "observed_at": "2026-08-18T01:10:30.000Z",
            "metrics": {},
        }
    ]


def test_transition_is_sent_immediately(setup_reporter):
    latest, transport, reporter = setup_reporter
    reporter.step(0.0)
    latest.publish(
        PresenceState.PRESENT,
        "pose_confirmed",
        NOW + timedelta(seconds=1),
        {"positive_streak": 3},
    )

    reporter.step(0.1)

    assert transport.attempts[-1]["sequence"] == 2
    assert transport.attempts[-1]["state"] == "present"
    assert transport.attempts[-1]["previous_state"] == "starting"
    assert transport.attempts[-1]["changed"] is True


def test_heartbeat_is_sent_at_15_seconds(setup_reporter):
    _, transport, reporter = setup_reporter
    reporter.step(0.0)

    reporter.step(14.999)
    assert len(transport.attempts) == 1
    reporter.step(15.0)

    heartbeat = transport.attempts[-1]
    assert len(transport.attempts) == 2
    assert heartbeat["sequence"] == 2
    assert heartbeat["state"] == "starting"
    assert heartbeat["previous_state"] == "starting"
    assert heartbeat["changed"] is False
    assert heartbeat["reason"] == "heartbeat"


def test_retry_keeps_event_identity(setup_reporter):
    _, transport, reporter = setup_reporter
    transport.failures_remaining = 1

    assert reporter.step(0.0) == 1.0
    first = transport.attempts[-1]
    reporter.step(1.0)

    assert transport.attempts[-1]["event_id"] == first["event_id"]
    assert transport.attempts[-1]["sequence"] == first["sequence"]


def test_new_revision_replaces_pending_report(setup_reporter):
    latest, transport, reporter = setup_reporter
    transport.failures_remaining = 2
    reporter.step(0.0)
    latest.publish(
        PresenceState.CAMERA_ERROR,
        "camera_read_failed",
        NOW + timedelta(seconds=0.5),
        {},
    )

    reporter.step(0.1)

    assert transport.attempts[-1]["event_id"] != transport.attempts[0]["event_id"]
    assert transport.attempts[-1]["sequence"] == 2
    assert transport.attempts[-1]["state"] == "camera_error"


def test_exponential_backoff_caps_and_resets(setup_reporter):
    _, transport, reporter = setup_reporter
    transport.failures_remaining = 7
    now = 0.0
    delays = []
    for _ in range(7):
        delay = reporter.step(now)
        delays.append(delay)
        now += delay

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]

    reporter.step(now)
    transport.failures_remaining = 1
    assert reporter.step(now + 15.0) == 1.0


def test_serialized_report_never_contains_camera_data(setup_reporter):
    latest, transport, reporter = setup_reporter
    latest.publish(
        PresenceState.PRESENT,
        "pose_confirmed",
        NOW,
        {
            "visible_core_landmarks": 5,
            "has_visible_shoulder": True,
            "positive_streak": 3,
            "seconds_since_last_positive": 0.0,
        },
    )

    reporter.step(0.0)
    payload = transport.attempts[-1]

    assert set(payload["metrics"]) == {
        "visible_core_landmarks",
        "has_visible_shoulder",
        "positive_streak",
        "seconds_since_last_positive",
    }
    assert "frame" not in payload
    assert "image" not in payload
    assert "landmarks" not in payload


def test_identity_is_serialized_as_separate_optional_dimension(setup_reporter):
    latest, transport, reporter = setup_reporter
    latest.publish(
        PresenceState.PRESENT,
        "pose_confirmed",
        NOW,
        {"positive_streak": 3},
        identity={
            "state": "owner",
            "previous_state": "starting",
            "changed": True,
            "face_count": 1,
            "similarity": 0.712346,
        },
    )

    reporter.step(0.0)

    assert transport.attempts[-1]["identity"] == {
        "state": "owner",
        "previous_state": "starting",
        "changed": True,
        "face_count": 1,
        "similarity": 0.712346,
    }
    assert "embedding" not in str(transport.attempts[-1]).lower()


def test_identity_only_transition_and_heartbeat_do_not_repeat_other_transitions(
    setup_reporter,
):
    latest, transport, reporter = setup_reporter
    reporter.step(0.0)
    latest.publish(
        PresenceState.PRESENT,
        "pose_confirmed",
        NOW,
        {},
        identity={
            "state": "owner",
            "previous_state": "starting",
            "changed": True,
            "face_count": 1,
            "similarity": 0.7,
        },
    )
    reporter.step(0.1)
    latest.publish(
        PresenceState.PRESENT,
        "pose_confirmed",
        NOW + timedelta(seconds=1),
        {},
        identity={
            "state": "unknown",
            "previous_state": "owner",
            "changed": True,
            "face_count": 1,
            "similarity": 0.2,
        },
    )

    reporter.step(0.2)
    identity_transition = transport.attempts[-1]
    assert identity_transition["state"] == "present"
    assert identity_transition["previous_state"] == "present"
    assert identity_transition["changed"] is False
    assert identity_transition["reason"] == "identity_changed"
    assert identity_transition["identity"]["changed"] is True

    reporter.step(15.2)
    heartbeat = transport.attempts[-1]
    assert heartbeat["reason"] == "heartbeat"
    assert heartbeat["identity"]["previous_state"] == "unknown"
    assert heartbeat["identity"]["changed"] is False


def test_presence_transition_normalizes_unchanged_identity_transition(setup_reporter):
    latest, transport, reporter = setup_reporter
    reporter.step(0.0)
    identity = {
        "state": "owner",
        "previous_state": "starting",
        "changed": True,
        "face_count": 1,
        "similarity": 0.7,
    }
    latest.publish(PresenceState.STARTING, "initializing", NOW, {}, identity=identity)
    reporter.step(0.1)

    latest.publish(
        PresenceState.PRESENT,
        "pose_confirmed",
        NOW + timedelta(seconds=1),
        {},
        identity=identity,
    )
    reporter.step(0.2)

    event = transport.attempts[-1]
    assert event["changed"] is True
    assert event["identity"]["state"] == "owner"
    assert event["identity"]["previous_state"] == "owner"
    assert event["identity"]["changed"] is False
