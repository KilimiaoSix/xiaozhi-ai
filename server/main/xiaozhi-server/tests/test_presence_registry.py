from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from core.presence_registry import (
    PresenceOutOfOrderError,
    PresenceRegistry,
    PresenceReport,
    PresenceValidationError,
)


NOW = datetime(2026, 8, 18, 1, 10, 30, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self.monotonic_value = 100.0
        self.utc_value = NOW

    def monotonic(self) -> float:
        return self.monotonic_value

    def utcnow(self) -> datetime:
        return self.utc_value

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.utc_value += timedelta(seconds=seconds)


@pytest.fixture
def payload():
    return {
        "schema_version": "1.0",
        "event_id": "6c618629-ffef-4c00-ab4f-17dc5ce2eb7a",
        "agent_instance_id": "45912c0c-144b-4ac7-970b-527add7b4dcc",
        "workstation_id": "desk-test",
        "source": "camera_pose",
        "state": "present",
        "previous_state": "starting",
        "changed": True,
        "reason": "pose_confirmed",
        "sequence": 1,
        "observed_at": "2026-08-18T01:10:30.000Z",
        "metrics": {
            "visible_core_landmarks": 5,
            "has_visible_shoulder": True,
            "positive_streak": 3,
            "seconds_since_last_positive": 0.0,
        },
    }


def parse(payload, now=NOW):
    return PresenceReport.from_payload(payload, now_utc=now)


def test_parses_valid_report(payload):
    report = parse(payload)

    assert report.workstation_id == "desk-test"
    assert report.state == "present"
    assert report.sequence == 1
    assert report.observed_at == NOW


def test_optional_identity_round_trips_and_legacy_payload_remains_valid(payload):
    assert parse(payload).identity is None
    payload["identity"] = {
        "state": "owner",
        "previous_state": "starting",
        "changed": True,
        "face_count": 1,
        "similarity": 0.712346,
    }
    clock = FakeClock()
    registry = PresenceRegistry(clock=clock)

    registry.accept(parse(payload, clock.utcnow()))
    state = registry.get("desk-test")

    assert state["identity"] == payload["identity"]
    payload["identity"]["state"] = "unknown"
    assert registry.get("desk-test")["identity"]["state"] == "owner"


def test_owner_horizontal_position_round_trips_without_face_geometry(payload):
    payload["identity"] = {
        "state": "owner",
        "previous_state": "starting",
        "changed": True,
        "face_count": 1,
        "similarity": 0.8,
        "horizontal_position": "left",
    }
    registry = PresenceRegistry()

    registry.accept(parse(payload))

    assert registry.get("desk-test")["identity"]["horizontal_position"] == "left"


@pytest.mark.parametrize("position", ["up", 1, True])
def test_rejects_invalid_owner_horizontal_position(payload, position):
    payload["identity"] = {
        "state": "owner",
        "previous_state": "starting",
        "changed": True,
        "face_count": 1,
        "similarity": 0.8,
        "horizontal_position": position,
    }

    with pytest.raises(PresenceValidationError, match="horizontal_position"):
        parse(payload)


def test_accepts_identity_only_transition_reason(payload):
    payload.update(
        state="present",
        previous_state="present",
        changed=False,
        reason="identity_changed",
        identity={
            "state": "unknown",
            "previous_state": "owner",
            "changed": True,
            "face_count": 1,
            "similarity": 0.2,
        },
    )

    assert parse(payload).reason == "identity_changed"


@pytest.mark.parametrize(
    "identity",
    [
        {"state": "visitor"},
        {
            "state": [],
            "previous_state": "starting",
            "changed": True,
            "face_count": 0,
        },
        {
            "state": "owner",
            "previous_state": "owner",
            "changed": True,
            "face_count": 1,
        },
        {
            "state": "owner",
            "previous_state": "starting",
            "changed": True,
            "face_count": 2,
            "similarity": 0.7,
        },
        {
            "state": "no_face",
            "previous_state": "owner",
            "changed": True,
            "face_count": 0,
            "similarity": 0.7,
        },
        {
            "state": "unknown",
            "previous_state": "owner",
            "changed": True,
            "face_count": 1,
            "embedding": [0.1, 0.2],
        },
    ],
)
def test_rejects_invalid_identity(payload, identity):
    payload["identity"] = identity

    with pytest.raises(PresenceValidationError, match="identity"):
        parse(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "2.0"),
        ("event_id", "not-a-uuid"),
        ("agent_instance_id", "not-a-uuid"),
        ("workstation_id", "desk test"),
        ("workstation_id", "x" * 65),
        ("source", "camera"),
        ("state", "stale"),
        ("previous_state", "stale"),
        ("changed", "true"),
        ("reason", "unknown"),
        ("sequence", True),
        ("sequence", 0),
        ("observed_at", "2026-08-18T01:10:30"),
    ],
)
def test_rejects_invalid_top_level_field(payload, field, value):
    payload[field] = value

    with pytest.raises(PresenceValidationError, match=field):
        parse(payload)


def test_rejects_missing_and_unexpected_top_level_fields(payload):
    del payload["state"]
    with pytest.raises(PresenceValidationError, match="state"):
        parse(payload)

    payload["state"] = "present"
    payload["frame"] = "base64"
    with pytest.raises(PresenceValidationError, match="unexpected fields"):
        parse(payload)


def test_rejects_changed_that_disagrees_with_states(payload):
    payload["changed"] = False

    with pytest.raises(PresenceValidationError, match="changed"):
        parse(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("visible_core_landmarks", 8),
        ("visible_core_landmarks", True),
        ("has_visible_shoulder", 1),
        ("positive_streak", -1),
        ("seconds_since_last_positive", -0.1),
        ("seconds_since_last_positive", float("inf")),
    ],
)
def test_rejects_invalid_metric(payload, field, value):
    payload["metrics"][field] = value

    with pytest.raises(PresenceValidationError, match=field):
        parse(payload)


def test_rejects_unexpected_metric(payload):
    payload["metrics"]["landmarks"] = []

    with pytest.raises(PresenceValidationError, match="metrics unexpected fields"):
        parse(payload)


def test_rejects_timestamp_more_than_five_minutes_in_future(payload):
    payload["observed_at"] = "2026-08-18T01:15:30.001Z"

    with pytest.raises(PresenceValidationError, match="observed_at"):
        parse(payload)


def test_accepts_first_report_and_returns_json_safe_state(payload):
    clock = FakeClock()
    registry = PresenceRegistry(clock=clock)

    result = registry.accept(parse(payload, clock.utcnow()))
    state = registry.get("desk-test")

    assert result == {
        "accepted": True,
        "duplicate": False,
        "workstation_id": "desk-test",
        "sequence": 1,
        "received_at": "2026-08-18T01:10:30.000Z",
    }
    assert state["effective_state"] == "present"
    assert state["reported_state"] == "present"
    assert state["age_seconds"] == 0.0
    assert state["metrics"] == payload["metrics"]


def test_duplicate_latest_event_is_idempotent(payload):
    clock = FakeClock()
    registry = PresenceRegistry(clock=clock)
    report = parse(payload, clock.utcnow())
    registry.accept(report)
    clock.advance(1.0)

    result = registry.accept(report)

    assert result["accepted"] is True
    assert result["duplicate"] is True
    assert registry.get("desk-test")["age_seconds"] == 1.0


@pytest.mark.parametrize("sequence", [1, 0])
def test_rejects_non_increasing_sequence_with_different_event(payload, sequence):
    clock = FakeClock()
    registry = PresenceRegistry(clock=clock)
    registry.accept(parse(payload, clock.utcnow()))
    payload["event_id"] = "b98af960-9166-45f3-bfb4-2f9fa6b9938f"
    payload["sequence"] = sequence

    if sequence == 0:
        with pytest.raises(PresenceValidationError):
            parse(payload, clock.utcnow())
    else:
        with pytest.raises(PresenceOutOfOrderError):
            registry.accept(parse(payload, clock.utcnow()))


def test_new_agent_instance_can_take_over_from_sequence_one(payload):
    clock = FakeClock()
    registry = PresenceRegistry(clock=clock)
    registry.accept(parse(payload, clock.utcnow()))
    clock.advance(1.0)
    payload.update(
        event_id="b98af960-9166-45f3-bfb4-2f9fa6b9938f",
        agent_instance_id="f2cc9abc-e8f2-4d6b-aa5d-34dbc560538b",
        observed_at="2026-08-18T01:10:31.000Z",
    )

    result = registry.accept(parse(payload, clock.utcnow()))

    assert result["accepted"] is True
    assert registry.get("desk-test")["agent_instance_id"] == payload["agent_instance_id"]


def test_rejects_old_agent_after_takeover(payload):
    clock = FakeClock()
    registry = PresenceRegistry(clock=clock)
    old = deepcopy(payload)
    registry.accept(parse(old, clock.utcnow()))
    clock.advance(1.0)
    payload.update(
        event_id="b98af960-9166-45f3-bfb4-2f9fa6b9938f",
        agent_instance_id="f2cc9abc-e8f2-4d6b-aa5d-34dbc560538b",
        observed_at="2026-08-18T01:10:31.000Z",
    )
    registry.accept(parse(payload, clock.utcnow()))
    old.update(
        event_id="7ebd1794-c471-4a73-8ec5-0f0307fd901a",
        sequence=2,
        observed_at="2026-08-18T01:10:32.000Z",
    )
    clock.advance(1.0)

    with pytest.raises(PresenceOutOfOrderError):
        registry.accept(parse(old, clock.utcnow()))


def test_rejects_new_instance_with_older_observation(payload):
    clock = FakeClock()
    registry = PresenceRegistry(clock=clock)
    registry.accept(parse(payload, clock.utcnow()))
    payload.update(
        event_id="b98af960-9166-45f3-bfb4-2f9fa6b9938f",
        agent_instance_id="f2cc9abc-e8f2-4d6b-aa5d-34dbc560538b",
        observed_at="2026-08-18T01:10:29.000Z",
    )

    with pytest.raises(PresenceOutOfOrderError):
        registry.accept(parse(payload, clock.utcnow()))


def test_unknown_workstation_returns_none():
    registry = PresenceRegistry(clock=FakeClock())

    assert registry.get("missing") is None


def test_state_becomes_stale_only_after_threshold(payload):
    clock = FakeClock()
    registry = PresenceRegistry(clock=clock, stale_after_seconds=30.0)
    registry.accept(parse(payload, clock.utcnow()))

    clock.advance(30.0)
    assert registry.get("desk-test")["effective_state"] == "present"

    clock.advance(0.001)
    state = registry.get("desk-test")
    assert state["effective_state"] == "stale"
    assert state["reported_state"] == "present"
    assert state["age_seconds"] == 30.001
