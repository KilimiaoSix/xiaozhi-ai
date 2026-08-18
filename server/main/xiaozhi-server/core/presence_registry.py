"""In-memory latest-value registry for workstation presence reports."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re
import threading
import time
from typing import Any, Mapping
from uuid import UUID


SCHEMA_VERSION = "1.0"
SOURCE = "camera_pose"
AGENT_STATES = frozenset({"starting", "present", "absent", "camera_error"})
REASONS = frozenset(
    {
        "initializing",
        "pose_confirmed",
        "absence_timeout",
        "camera_open_failed",
        "camera_read_failed",
        "camera_recovered",
        "heartbeat",
    }
)
WORKSTATION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_CLOCK_SKEW = timedelta(minutes=5)

REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "agent_instance_id",
        "workstation_id",
        "source",
        "state",
        "previous_state",
        "changed",
        "reason",
        "sequence",
        "observed_at",
        "metrics",
    }
)
METRIC_FIELDS = frozenset(
    {
        "visible_core_landmarks",
        "has_visible_shoulder",
        "positive_streak",
        "seconds_since_last_positive",
    }
)


class PresenceValidationError(ValueError):
    """The submitted presence payload does not match schema 1.0."""


class PresenceOutOfOrderError(ValueError):
    """A report would roll back the active agent instance or sequence."""


def _isoformat_millis(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _require_uuid(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise PresenceValidationError(f"{field} must be a UUID string")
    try:
        UUID(value)
    except (ValueError, AttributeError):
        raise PresenceValidationError(f"{field} must be a UUID string") from None
    return value


def _require_state(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if value not in AGENT_STATES:
        raise PresenceValidationError(f"{field} must be an agent presence state")
    return value


def _parse_observed_at(value: Any, now_utc: datetime) -> datetime:
    if not isinstance(value, str):
        raise PresenceValidationError("observed_at must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PresenceValidationError(
            "observed_at must be an RFC 3339 timestamp"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PresenceValidationError("observed_at must include a timezone")
    parsed = parsed.astimezone(timezone.utc)
    if parsed > now_utc.astimezone(timezone.utc) + MAX_CLOCK_SKEW:
        raise PresenceValidationError("observed_at is more than 5 minutes in the future")
    return parsed


def _validate_metrics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PresenceValidationError("metrics must be a json object")
    unexpected = set(value) - METRIC_FIELDS
    if unexpected:
        raise PresenceValidationError(
            f"metrics unexpected fields: {', '.join(sorted(unexpected))}"
        )

    metrics = deepcopy(value)
    if "visible_core_landmarks" in metrics:
        visible = metrics["visible_core_landmarks"]
        if isinstance(visible, bool) or not isinstance(visible, int) or not 0 <= visible <= 7:
            raise PresenceValidationError(
                "visible_core_landmarks must be an integer from 0 to 7"
            )
    if "has_visible_shoulder" in metrics and not isinstance(
        metrics["has_visible_shoulder"], bool
    ):
        raise PresenceValidationError("has_visible_shoulder must be a boolean")
    if "positive_streak" in metrics:
        streak = metrics["positive_streak"]
        if isinstance(streak, bool) or not isinstance(streak, int) or streak < 0:
            raise PresenceValidationError(
                "positive_streak must be a nonnegative integer"
            )
    if "seconds_since_last_positive" in metrics:
        seconds = metrics["seconds_since_last_positive"]
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            raise PresenceValidationError(
                "seconds_since_last_positive must be a finite nonnegative number"
            )
    return metrics


@dataclass(frozen=True)
class PresenceReport:
    schema_version: str
    event_id: str
    agent_instance_id: str
    workstation_id: str
    source: str
    state: str
    previous_state: str
    changed: bool
    reason: str
    sequence: int
    observed_at: datetime
    metrics: dict[str, Any]

    @classmethod
    def from_payload(
        cls, payload: Any, now_utc: datetime | None = None
    ) -> "PresenceReport":
        if not isinstance(payload, dict):
            raise PresenceValidationError("body must be a json object")

        missing = REPORT_FIELDS - set(payload)
        if missing:
            raise PresenceValidationError(
                f"missing required fields: {', '.join(sorted(missing))}"
            )
        unexpected = set(payload) - REPORT_FIELDS
        if unexpected:
            raise PresenceValidationError(
                f"unexpected fields: {', '.join(sorted(unexpected))}"
            )

        if payload["schema_version"] != SCHEMA_VERSION:
            raise PresenceValidationError("schema_version must be 1.0")
        event_id = _require_uuid(payload, "event_id")
        agent_instance_id = _require_uuid(payload, "agent_instance_id")

        workstation_id = payload["workstation_id"]
        if not isinstance(workstation_id, str) or not WORKSTATION_PATTERN.fullmatch(
            workstation_id
        ):
            raise PresenceValidationError(
                "workstation_id must match [A-Za-z0-9._-]{1,64}"
            )
        if payload["source"] != SOURCE:
            raise PresenceValidationError("source must be camera_pose")

        state = _require_state(payload, "state")
        previous_state = _require_state(payload, "previous_state")
        changed = payload["changed"]
        if not isinstance(changed, bool) or changed != (state != previous_state):
            raise PresenceValidationError(
                "changed must be a boolean equal to state != previous_state"
            )

        reason = payload["reason"]
        if reason not in REASONS:
            raise PresenceValidationError("reason is not a supported value")
        sequence = payload["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise PresenceValidationError("sequence must be an integer greater than zero")

        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        if now_utc.tzinfo is None or now_utc.utcoffset() is None:
            raise ValueError("now_utc must be timezone-aware")

        return cls(
            schema_version=SCHEMA_VERSION,
            event_id=event_id,
            agent_instance_id=agent_instance_id,
            workstation_id=workstation_id,
            source=SOURCE,
            state=state,
            previous_state=previous_state,
            changed=changed,
            reason=reason,
            sequence=sequence,
            observed_at=_parse_observed_at(payload["observed_at"], now_utc),
            metrics=_validate_metrics(payload["metrics"]),
        )


class _SystemClock:
    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _RegistryRecord:
    report: PresenceReport
    received_at: datetime
    received_monotonic: float


class PresenceRegistry:
    def __init__(self, clock=None, stale_after_seconds: float = 30.0) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be greater than zero")
        self._clock = clock or _SystemClock()
        self._stale_after_seconds = float(stale_after_seconds)
        self._records: dict[str, _RegistryRecord] = {}
        self._lock = threading.RLock()

    def accept(self, report: PresenceReport) -> dict[str, Any]:
        with self._lock:
            current = self._records.get(report.workstation_id)
            if current is not None:
                current_report = current.report
                if report.event_id == current_report.event_id:
                    return self._acceptance(current, duplicate=True)

                if report.agent_instance_id == current_report.agent_instance_id:
                    if report.sequence <= current_report.sequence:
                        raise PresenceOutOfOrderError(
                            "sequence must increase within an agent instance"
                        )
                else:
                    if report.sequence != 1:
                        raise PresenceOutOfOrderError(
                            "a new agent instance must start at sequence 1"
                        )
                    if report.observed_at < current_report.observed_at:
                        raise PresenceOutOfOrderError(
                            "a new agent instance cannot replace a newer observation"
                        )

                if report.observed_at < current_report.observed_at - MAX_CLOCK_SKEW:
                    raise PresenceValidationError(
                        "observed_at is more than 5 minutes older than the current report"
                    )

            record = _RegistryRecord(
                report=report,
                received_at=self._clock.utcnow().astimezone(timezone.utc),
                received_monotonic=self._clock.monotonic(),
            )
            self._records[report.workstation_id] = record
            return self._acceptance(record, duplicate=False)

    def _acceptance(
        self, record: _RegistryRecord, duplicate: bool
    ) -> dict[str, Any]:
        return {
            "accepted": True,
            "duplicate": duplicate,
            "workstation_id": record.report.workstation_id,
            "sequence": record.report.sequence,
            "received_at": _isoformat_millis(record.received_at),
        }

    def get(self, workstation_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(workstation_id)
            if record is None:
                return None
            age_seconds = max(0.0, self._clock.monotonic() - record.received_monotonic)
            age_seconds = round(age_seconds, 3)
            report = record.report
            return {
                "workstation_id": report.workstation_id,
                "source": report.source,
                "effective_state": (
                    "stale"
                    if age_seconds > self._stale_after_seconds
                    else report.state
                ),
                "reported_state": report.state,
                "reason": report.reason,
                "changed": report.changed,
                "sequence": report.sequence,
                "event_id": report.event_id,
                "agent_instance_id": report.agent_instance_id,
                "observed_at": _isoformat_millis(report.observed_at),
                "received_at": _isoformat_millis(record.received_at),
                "age_seconds": age_seconds,
                "stale_after_seconds": self._stale_after_seconds,
                "metrics": deepcopy(report.metrics),
            }
