"""Same-frame pose and owner-face inference adapter."""

from typing import Any, Callable


def _state_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


class CameraInferenceRuntime:
    def __init__(
        self,
        *,
        decoder: Callable[[bytes], Any],
        pose_detector: Any,
        presence_tracker: Any,
        face_verifier: Any,
        face_threshold: float = 0.45,
    ) -> None:
        self._decoder = decoder
        self._pose_detector = pose_detector
        self._presence_tracker = presence_tracker
        self._face_verifier = face_verifier
        self._face_threshold = face_threshold
        self._previous_timestamp_ms = -1

    def close(self) -> None:
        self._pose_detector.close()

    def process(self, jpeg: bytes, sequence: int, now: float) -> dict[str, Any]:
        frame = self._decoder(jpeg)
        timestamp_ms = max(int(now * 1000), self._previous_timestamp_ms + 1)
        self._previous_timestamp_ms = timestamp_ms

        pose = self._pose_detector.detect(frame, timestamp_ms)
        previous_presence = _state_value(self._presence_tracker.state)
        presence_state = _state_value(
            self._presence_tracker.update(pose.is_present(), now)
        )
        identity = dict(self._face_verifier.observe(frame, now).to_payload())
        face_count = identity.get("face_count", 0)
        identity["face_detected"] = isinstance(face_count, int) and face_count > 0
        identity["matched"] = identity.get("state") == "owner"
        if "similarity" in identity:
            identity["threshold"] = self._face_threshold
        else:
            identity.pop("threshold", None)

        metrics = dict(pose.diagnostic_metrics())
        metrics.update(self._presence_tracker.metrics(now))
        return {
            "sequence": sequence,
            "presence": {
                "state": presence_state,
                "changed": presence_state != previous_presence,
            },
            "identity": identity,
            "metrics": metrics,
        }
