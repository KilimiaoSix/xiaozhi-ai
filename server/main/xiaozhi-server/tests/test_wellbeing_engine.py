from datetime import datetime, timedelta, timezone

from core.wellbeing.engine import WellbeingEngine, WellbeingPolicy


UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8))


def local(hour: int, minute: int = 0, *, day: int = 18) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=SHANGHAI).astimezone(UTC)


def owner(position: str = "center") -> dict:
    return {
        "effective_state": "present",
        "identity": {"state": "owner", "horizontal_position": position},
    }


def absent() -> dict:
    return {
        "effective_state": "absent",
        "identity": {"state": "no_face"},
    }


def policy(**overrides) -> WellbeingPolicy:
    raw = {
        "wellbeing": {
            "enabled": True,
            "bindings": [{"workstation_id": "desk", "device_id": "robot"}],
            "warm_interval_min_minutes": 60,
            "warm_interval_max_minutes": 60,
        }
    }
    raw["wellbeing"].update(overrides)
    return WellbeingPolicy.from_config(raw)


def test_only_verified_owner_is_eligible():
    engine = WellbeingEngine(policy(long_work_minutes=1), random_source=lambda a, b: a)
    unknown = {
        "effective_state": "present",
        "identity": {"state": "unknown", "horizontal_position": "left"},
    }

    assert engine.evaluate("desk", unknown, local(10, 0)) is None
    assert engine.evaluate("desk", unknown, local(10, 2)) is None


def test_long_work_alternates_stand_and_water_with_cooldown():
    engine = WellbeingEngine(
        policy(
            long_work_minutes=50,
            long_work_cooldown_minutes=50,
            warm_interval_min_minutes=999,
            warm_interval_max_minutes=999,
        ),
        random_source=lambda a, b: a,
    )

    assert engine.evaluate("desk", owner(), local(9, 0)) is None
    first = engine.evaluate("desk", owner(), local(9, 50))
    assert first.kind == "long_work"
    assert "站起来" in first.text
    assert engine.evaluate("desk", owner(), local(10, 39)) is None
    second = engine.evaluate("desk", owner(), local(10, 40))
    assert second.kind == "long_work"
    assert "喝几口水" in second.text


def test_short_absence_does_not_reset_but_five_minute_break_does():
    engine = WellbeingEngine(
        policy(
            long_work_minutes=50,
            break_reset_minutes=5,
            warm_interval_min_minutes=999,
            warm_interval_max_minutes=999,
        ),
        random_source=lambda a, b: a,
    )
    engine.evaluate("desk", owner(), local(9, 0))
    engine.evaluate("desk", absent(), local(9, 30))
    engine.evaluate("desk", owner(), local(9, 34))

    assert engine.evaluate("desk", owner(), local(9, 50)).kind == "long_work"

    engine.evaluate("desk", absent(), local(10, 0))
    engine.evaluate("desk", absent(), local(10, 5))
    engine.evaluate("desk", owner(), local(10, 6))

    assert engine.evaluate("desk", owner(), local(10, 55)) is None
    assert engine.evaluate("desk", owner(), local(10, 56)).kind == "long_work"


def test_commute_reminder_is_workday_only_and_once_per_day():
    engine = WellbeingEngine(policy(), random_source=lambda a, b: a)

    first = engine.evaluate("desk", owner(), local(17, 50))
    duplicate = engine.evaluate("desk", owner(), local(17, 55))
    saturday = engine.evaluate("weekend", owner(), local(17, 50, day=22))

    assert first.kind == "commute_safety"
    assert duplicate is None
    assert saturday is None


def test_nine_pm_window_does_not_backfill_later():
    engine = WellbeingEngine(policy(), random_source=lambda a, b: a)

    assert engine.evaluate("desk", owner(), local(21, 0)).kind == "overtime"
    assert engine.evaluate("desk", owner(), local(21, 20)) is None
    assert engine.evaluate("late-start", owner(), local(22, 0)) is None


def test_frantic_reminder_repeats_every_ten_minutes_across_midnight():
    engine = WellbeingEngine(policy(), random_source=lambda a, b: a)

    first = engine.evaluate("desk", owner(), local(23, 0))
    assert first.kind == "frantic_overtime"
    assert first.action == "shake"
    assert first.speak is True
    assert engine.evaluate("desk", owner(), local(23, 9)) is None
    second = engine.evaluate("desk", owner(), local(23, 10))
    assert second.action == "roll"
    assert engine.evaluate("desk", owner(), local(0, 5, day=19)) is not None


def test_warm_event_faces_owner_then_reschedules():
    engine = WellbeingEngine(
        policy(
            warm_interval_min_minutes=45,
            warm_interval_max_minutes=45,
            long_work_minutes=999,
        ),
        random_source=lambda a, b: a,
    )

    engine.evaluate("desk", owner("left"), local(10, 0))
    event = engine.evaluate("desk", owner("left"), local(10, 45))

    assert event.kind == "warm_encouragement"
    assert event.emotion == "loving"
    assert event.action == "look_left"
    assert event.silent is True
    assert engine.evaluate("desk", owner("right"), local(10, 46)) is None


def test_high_priority_event_delays_overdue_warm_event():
    engine = WellbeingEngine(
        policy(
            warm_interval_min_minutes=45,
            warm_interval_max_minutes=45,
            warm_quiet_minutes=20,
            long_work_minutes=999,
        ),
        random_source=lambda a, b: a,
    )
    engine.evaluate("desk", owner(), local(17, 0))

    assert engine.evaluate("desk", owner(), local(17, 50)).kind == "commute_safety"
    assert engine.evaluate("desk", owner(), local(18, 0)) is None
    assert engine.evaluate("desk", owner(), local(18, 10)).kind == "warm_encouragement"
