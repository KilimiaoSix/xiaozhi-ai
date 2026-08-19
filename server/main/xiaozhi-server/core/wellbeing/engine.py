"""Pure rule engine for owner-presence wellbeing events."""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import random
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _timezone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name=name)
        return timezone.utc


def _minutes(value: Any, fallback: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= minimum else fallback


def _clock_time(value: Any, fallback: str) -> time:
    text = str(value or fallback)
    try:
        hour, minute = (int(part) for part in text.split(":", 1))
        return time(hour, minute)
    except (TypeError, ValueError):
        hour, minute = (int(part) for part in fallback.split(":", 1))
        return time(hour, minute)


@dataclass(frozen=True)
class WellbeingPolicy:
    enabled: bool
    timezone_name: str
    bindings: dict[str, str]
    workdays: frozenset[int]
    workday_end: time
    commute_advance_minutes: int
    long_work_minutes: int
    break_reset_minutes: int
    long_work_cooldown_minutes: int
    overtime_start: time
    overtime_window_minutes: int
    frantic_start: time
    frantic_end: time
    frantic_interval_minutes: int
    warm_start: time
    warm_end: time
    warm_interval_min_minutes: int
    warm_interval_max_minutes: int
    warm_quiet_minutes: int
    poll_seconds: int

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "WellbeingPolicy":
        raw = config.get("wellbeing", {})
        raw = raw if isinstance(raw, Mapping) else {}
        bindings: dict[str, str] = {}
        raw_bindings = raw.get("bindings", [])
        if not isinstance(raw_bindings, (list, tuple)):
            raw_bindings = []
        for item in raw_bindings:
            if not isinstance(item, Mapping):
                continue
            workstation = str(item.get("workstation_id", "")).strip()
            if workstation:
                bindings[workstation] = str(item.get("device_id", "")).strip()
        if not bindings:
            bindings["desktop-local"] = ""

        raw_workdays = raw.get("workdays", [1, 2, 3, 4, 5])
        if not isinstance(raw_workdays, (list, tuple, set, frozenset)):
            raw_workdays = [1, 2, 3, 4, 5]
        workdays = {
            day
            for day in raw_workdays
            if isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 7
        }
        interval_min = _minutes(raw.get("warm_interval_min_minutes"), 45)
        interval_max = _minutes(raw.get("warm_interval_max_minutes"), 90)
        interval_max = max(interval_min, interval_max)
        return cls(
            enabled=bool(raw.get("enabled", True)),
            timezone_name=str(raw.get("timezone", "Asia/Shanghai")),
            bindings=bindings,
            workdays=frozenset(workdays),
            workday_end=_clock_time(raw.get("workday_end"), "18:00"),
            commute_advance_minutes=_minutes(raw.get("commute_advance_minutes"), 10),
            long_work_minutes=_minutes(raw.get("long_work_minutes"), 50),
            break_reset_minutes=_minutes(raw.get("break_reset_minutes"), 5),
            long_work_cooldown_minutes=_minutes(
                raw.get("long_work_cooldown_minutes"), 50
            ),
            overtime_start=_clock_time(raw.get("overtime_start"), "21:00"),
            overtime_window_minutes=_minutes(raw.get("overtime_window_minutes"), 30),
            frantic_start=_clock_time(raw.get("frantic_start"), "23:00"),
            frantic_end=_clock_time(raw.get("frantic_end"), "06:00"),
            frantic_interval_minutes=_minutes(
                raw.get("frantic_interval_minutes"), 10
            ),
            warm_start=_clock_time(raw.get("warm_start"), "09:30"),
            warm_end=_clock_time(raw.get("warm_end"), "20:30"),
            warm_interval_min_minutes=interval_min,
            warm_interval_max_minutes=interval_max,
            warm_quiet_minutes=_minutes(raw.get("warm_quiet_minutes"), 20),
            poll_seconds=_minutes(raw.get("poll_seconds"), 5),
        )

    @property
    def timezone(self):
        return _timezone(self.timezone_name)


@dataclass(frozen=True)
class WellbeingEvent:
    kind: str
    text: str
    emotion: str
    status: str
    action: str
    speak: bool = False
    silent: bool = False
    restore_after: float = 8.0


@dataclass
class _WorkstationState:
    local_date: date | None = None
    daily_sent: set[str] = field(default_factory=set)
    present_since: datetime | None = None
    absent_since: datetime | None = None
    last_long_work: datetime | None = None
    last_high_priority: datetime | None = None
    last_frantic: datetime | None = None
    next_warm: datetime | None = None
    long_work_count: int = 0
    frantic_count: int = 0


class WellbeingEngine:
    def __init__(
        self,
        policy: WellbeingPolicy,
        *,
        random_source: Callable[[int, int], int] | None = None,
    ) -> None:
        self.policy = policy
        self._randint = random_source or random.randint
        self._states: dict[str, _WorkstationState] = {}

    def evaluate(
        self,
        workstation_id: str,
        presence: Mapping[str, Any] | None,
        now: datetime,
    ) -> WellbeingEvent | None:
        local_now = self._local(now)
        state = self._states.setdefault(workstation_id, _WorkstationState())
        if state.local_date != local_now.date():
            state.local_date = local_now.date()
            state.daily_sent.clear()

        if not self._is_owner_present(presence):
            self._observe_absence(state, local_now)
            return None

        self._observe_presence(state, local_now)
        position = str(
            (presence or {})
            .get("identity", {})
            .get("horizontal_position", "center")
        )

        if self._frantic_due(state, local_now):
            state.last_frantic = local_now
            state.last_high_priority = local_now
            state.frantic_count += 1
            action = "shake" if state.frantic_count % 2 else "roll"
            return WellbeingEvent(
                "frantic_overtime",
                "已经很晚了，工作先停在这里。现在就收拾回家，明天再继续。",
                "shocked",
                "该下班了",
                action,
                speak=True,
                restore_after=12.0,
            )

        if self._daily_window_due(
            state,
            local_now,
            "overtime",
            self.policy.overtime_start,
            self.policy.overtime_window_minutes,
        ):
            state.last_high_priority = local_now
            return WellbeingEvent(
                "overtime",
                "已经九点了，今天辛苦了。把手头这点收个尾，早点下班吧。",
                "sleepy",
                "早点下班",
                "look_down",
                speak=True,
                restore_after=10.0,
            )

        if self._commute_due(state, local_now):
            state.last_high_priority = local_now
            return WellbeingEvent(
                "commute_safety",
                "快下班啦，回去路上慢一点，注意安全。",
                "winking",
                "下班平安",
                "nod",
                restore_after=8.0,
                speak=True,
            )

        if self._long_work_due(state, local_now):
            state.last_long_work = local_now
            state.last_high_priority = local_now
            state.long_work_count += 1
            stand = state.long_work_count % 2 == 1
            return WellbeingEvent(
                "long_work",
                (
                    "坐得有点久了，站起来伸伸腰，走两分钟再继续吧。"
                    if stand
                    else "专注很久啦，喝几口水，看看远处，让眼睛也休息一下。"
                ),
                "relaxed",
                "休息一下",
                "look_up" if stand else "nod",
                speak=True,
                restore_after=10.0,
            )

        if self._warm_due(state, local_now):
            state.next_warm = self._next_warm(local_now)
            variants = (
                ("认真工作的你很棒，给你一颗小爱心。", "loving"),
                ("稳稳向前就很好，我在这里陪你。", "winking"),
                ("给现在的你加个油，慢慢来，也会做得很好。", "confident"),
            )
            text, emotion = variants[self._randint(0, len(variants) - 1)]
            return WellbeingEvent(
                "warm_encouragement",
                text,
                emotion,
                "给你加油",
                {"left": "look_left", "right": "look_right"}.get(position, "nod"),
                silent=True,
                restore_after=7.0,
            )
        return None

    def _local(self, now: datetime) -> datetime:
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(self.policy.timezone)

    @staticmethod
    def _is_owner_present(presence: Mapping[str, Any] | None) -> bool:
        if not presence or presence.get("effective_state") != "present":
            return False
        identity = presence.get("identity")
        return isinstance(identity, Mapping) and identity.get("state") == "owner"

    def _observe_absence(self, state: _WorkstationState, now: datetime) -> None:
        if state.absent_since is None:
            state.absent_since = now
        if now - state.absent_since >= timedelta(minutes=self.policy.break_reset_minutes):
            state.present_since = None
            state.last_long_work = None
            state.next_warm = None

    def _observe_presence(self, state: _WorkstationState, now: datetime) -> None:
        if state.absent_since is not None:
            if now - state.absent_since >= timedelta(
                minutes=self.policy.break_reset_minutes
            ):
                state.present_since = now
                state.last_long_work = None
                state.next_warm = self._next_warm(now)
            state.absent_since = None
        if state.present_since is None:
            state.present_since = now
        if state.next_warm is None:
            state.next_warm = self._next_warm(now)

    def _next_warm(self, now: datetime) -> datetime:
        delay = self._randint(
            self.policy.warm_interval_min_minutes,
            self.policy.warm_interval_max_minutes,
        )
        return now + timedelta(minutes=delay)

    def _frantic_due(self, state: _WorkstationState, now: datetime) -> bool:
        current = now.time().replace(tzinfo=None)
        overnight = self.policy.frantic_start > self.policy.frantic_end
        in_window = (
            current >= self.policy.frantic_start or current < self.policy.frantic_end
            if overnight
            else self.policy.frantic_start <= current < self.policy.frantic_end
        )
        if not in_window:
            return False
        return state.last_frantic is None or now - state.last_frantic >= timedelta(
            minutes=self.policy.frantic_interval_minutes
        )

    def _daily_window_due(
        self,
        state: _WorkstationState,
        now: datetime,
        key: str,
        start: time,
        duration_minutes: int,
    ) -> bool:
        if key in state.daily_sent:
            return False
        window_start = datetime.combine(now.date(), start, tzinfo=now.tzinfo)
        if not window_start <= now < window_start + timedelta(minutes=duration_minutes):
            return False
        state.daily_sent.add(key)
        return True

    def _commute_due(self, state: _WorkstationState, now: datetime) -> bool:
        if now.isoweekday() not in self.policy.workdays:
            return False
        start = datetime.combine(now.date(), self.policy.workday_end, tzinfo=now.tzinfo)
        start -= timedelta(minutes=self.policy.commute_advance_minutes)
        if "commute" in state.daily_sent or not start <= now < start + timedelta(
            minutes=self.policy.commute_advance_minutes
        ):
            return False
        state.daily_sent.add("commute")
        return True

    def _long_work_due(self, state: _WorkstationState, now: datetime) -> bool:
        if state.present_since is None or now - state.present_since < timedelta(
            minutes=self.policy.long_work_minutes
        ):
            return False
        reference = state.last_long_work or state.present_since
        cooldown = (
            self.policy.long_work_minutes
            if state.last_long_work is None
            else self.policy.long_work_cooldown_minutes
        )
        return now - reference >= timedelta(minutes=cooldown)

    def _warm_due(self, state: _WorkstationState, now: datetime) -> bool:
        current = now.time().replace(tzinfo=None)
        if not self.policy.warm_start <= current < self.policy.warm_end:
            if state.next_warm is not None and now >= state.next_warm:
                state.next_warm = self._next_warm(now)
            return False
        if state.next_warm is None or now < state.next_warm:
            return False
        return state.last_high_priority is None or (
            now - state.last_high_priority
            >= timedelta(minutes=self.policy.warm_quiet_minutes)
        )
