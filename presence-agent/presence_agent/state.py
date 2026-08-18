from enum import Enum


class PresenceState(str, Enum):
    STARTING = "starting"
    PRESENT = "present"
    ABSENT = "absent"
    CAMERA_ERROR = "camera_error"


class PresenceTracker:
    def __init__(self, required_hits: int, absent_after_seconds: float) -> None:
        if required_hits <= 0:
            raise ValueError("required_hits must be greater than zero")
        if absent_after_seconds <= 0:
            raise ValueError("absent_after_seconds must be greater than zero")
        self.required_hits = required_hits
        self.absent_after_seconds = absent_after_seconds
        self.first_healthy_observation_time: float | None = None
        self.most_recent_positive_time: float | None = None
        self.positive_streak = 0
        self.state = PresenceState.STARTING

    def update(
        self,
        detected: bool,
        now_seconds: float,
        camera_ok: bool = True,
    ) -> PresenceState:
        if not camera_ok:
            self.first_healthy_observation_time = None
            self.most_recent_positive_time = None
            self.positive_streak = 0
            self.state = PresenceState.CAMERA_ERROR
            return self.state

        if self.first_healthy_observation_time is None:
            self.first_healthy_observation_time = now_seconds

        if detected:
            self.most_recent_positive_time = now_seconds
            self.positive_streak += 1
            if self.state is PresenceState.PRESENT:
                return self.state
            self.state = (
                PresenceState.PRESENT
                if self.positive_streak >= self.required_hits
                else PresenceState.STARTING
            )
            return self.state

        self.positive_streak = 0
        reference_time = (
            self.most_recent_positive_time
            if self.most_recent_positive_time is not None
            else self.first_healthy_observation_time
        )
        if now_seconds - reference_time >= self.absent_after_seconds:
            self.state = PresenceState.ABSENT
        elif self.state is not PresenceState.PRESENT:
            self.state = PresenceState.STARTING
        return self.state

    def metrics(self, now_seconds: float) -> dict[str, int | float]:
        reference_time = (
            self.most_recent_positive_time
            if self.most_recent_positive_time is not None
            else self.first_healthy_observation_time
        )
        seconds = 0.0 if reference_time is None else max(0.0, now_seconds - reference_time)
        return {
            "positive_streak": self.positive_streak,
            "seconds_since_last_positive": round(seconds, 3),
        }
