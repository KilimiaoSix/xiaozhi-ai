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
        face_implies_present: bool = False,
    ) -> None:
        self._decoder = decoder
        self._pose_detector = pose_detector
        self._presence_tracker = presence_tracker
        self._face_verifier = face_verifier
        self._face_threshold = face_threshold
        # 姿态判在岗要求 7 个核心关键点里可见 >=4 且含肩膀。坐得离摄像头近时画面
        # 只有头和肩顶,关键点常年停在 3,姿态恒判 absent——人在工位却收不到 present,
        # 到岗迎接/返岗汇总全哑。打开后姿态与人脸取或:看到脸也算在岗。
        # 代价:画面里的人像照片/屏幕里的人脸同样会被算成在岗,默认关闭。
        self._face_implies_present = bool(face_implies_present)
        self._previous_timestamp_ms = -1

    def close(self) -> None:
        self._pose_detector.close()

    def process(self, jpeg: bytes, sequence: int, now: float) -> dict[str, Any]:
        frame = self._decoder(jpeg)
        timestamp_ms = max(int(now * 1000), self._previous_timestamp_ms + 1)
        self._previous_timestamp_ms = timestamp_ms

        pose = self._pose_detector.detect(frame, timestamp_ms)
        # 人脸推理提到在岗判定之前:开了 face_implies_present 时要拿它的结果参与判定。
        # 同一帧、同一个 now,顺序调整不改变任何单项结果。
        identity = dict(self._face_verifier.observe(frame, now).to_payload())
        face_count = identity.get("face_count", 0)
        face_detected = isinstance(face_count, int) and face_count > 0
        identity["face_detected"] = face_detected

        detected = pose.is_present()
        if self._face_implies_present:
            detected = detected or face_detected

        previous_presence = _state_value(self._presence_tracker.state)
        presence_state = _state_value(self._presence_tracker.update(detected, now))
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
