from datetime import datetime, timedelta, timezone

from presence_agent.snapshot import LatestSnapshot
from presence_agent.state import PresenceState


NOW = datetime(2026, 8, 18, 1, 10, 30, tzinfo=timezone.utc)


def test_initial_snapshot_is_starting():
    latest = LatestSnapshot(NOW)

    snapshot = latest.read()

    assert snapshot.state is PresenceState.STARTING
    assert snapshot.previous_state is PresenceState.STARTING
    assert snapshot.changed is False
    assert snapshot.reason == "initializing"
    assert snapshot.revision == 0


def test_state_change_increments_revision_and_records_transition():
    latest = LatestSnapshot(NOW)

    latest.publish(
        PresenceState.PRESENT,
        "pose_confirmed",
        NOW + timedelta(seconds=1),
        {"positive_streak": 3},
    )
    snapshot = latest.read()

    assert snapshot.state is PresenceState.PRESENT
    assert snapshot.previous_state is PresenceState.STARTING
    assert snapshot.changed is True
    assert snapshot.reason == "pose_confirmed"
    assert snapshot.revision == 1


def test_same_state_refreshes_observation_without_hiding_transition():
    latest = LatestSnapshot(NOW)
    latest.publish(
        PresenceState.PRESENT,
        "pose_confirmed",
        NOW + timedelta(seconds=1),
        {"positive_streak": 3},
    )

    latest.publish(
        PresenceState.PRESENT,
        "pose_confirmed",
        NOW + timedelta(seconds=2),
        {"positive_streak": 4},
    )
    snapshot = latest.read()

    assert snapshot.revision == 1
    assert snapshot.previous_state is PresenceState.STARTING
    assert snapshot.changed is True
    assert snapshot.observed_at == NOW + timedelta(seconds=2)
    assert snapshot.metrics == {"positive_streak": 4}


def test_read_returns_metrics_copy():
    latest = LatestSnapshot(NOW)
    first = latest.read()
    first.metrics["frame"] = "forbidden"

    assert latest.read().metrics == {}
