from presence_agent.state import PresenceState, PresenceTracker


def test_requires_three_consecutive_positive_frames():
    tracker = PresenceTracker(required_hits=3, absent_after_seconds=2.0)

    assert tracker.update(True, 0.0) is PresenceState.STARTING
    assert tracker.update(True, 0.1) is PresenceState.STARTING
    assert tracker.update(True, 0.2) is PresenceState.PRESENT


def test_negative_frame_resets_positive_streak():
    tracker = PresenceTracker(required_hits=3, absent_after_seconds=2.0)

    tracker.update(True, 0.0)
    tracker.update(True, 0.1)
    tracker.update(False, 0.2)

    assert tracker.update(True, 0.3) is PresenceState.STARTING
    assert tracker.positive_streak == 1


def test_present_is_held_until_two_seconds_after_last_positive():
    tracker = PresenceTracker(required_hits=3, absent_after_seconds=2.0)
    tracker.update(True, 0.0)
    tracker.update(True, 0.1)
    tracker.update(True, 0.2)

    assert tracker.update(False, 2.199) is PresenceState.PRESENT
    assert tracker.update(False, 2.2) is PresenceState.ABSENT


def test_absent_timer_starts_with_first_healthy_frame_when_never_present():
    tracker = PresenceTracker(required_hits=3, absent_after_seconds=2.0)

    assert tracker.update(False, 10.0) is PresenceState.STARTING
    assert tracker.update(False, 11.999) is PresenceState.STARTING
    assert tracker.update(False, 12.0) is PresenceState.ABSENT


def test_camera_error_resets_tracking_and_recovery_starts_fresh():
    tracker = PresenceTracker(required_hits=3, absent_after_seconds=2.0)
    tracker.update(True, 0.0)
    tracker.update(True, 0.1)

    assert tracker.update(False, 0.2, camera_ok=False) is PresenceState.CAMERA_ERROR
    assert tracker.positive_streak == 0
    assert tracker.update(True, 1.0) is PresenceState.STARTING


def test_tracker_exposes_minimal_metrics():
    tracker = PresenceTracker(required_hits=3, absent_after_seconds=2.0)
    tracker.update(True, 5.0)

    assert tracker.metrics(5.5) == {
        "positive_streak": 1,
        "seconds_since_last_positive": 0.5,
    }
