"""Pose observations and the MediaPipe video landmarker adapter."""

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path


CORE_LANDMARK_INDICES = (0, 11, 12, 13, 14, 23, 24)
SHOULDER_INDICES = (11, 12)


@dataclass(frozen=True)
class Landmark:
    x: float
    y: float
    z: float
    visibility: float
    presence: float


@dataclass(frozen=True)
class PoseObservation:
    landmarks: tuple[Landmark, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "landmarks", tuple(self.landmarks))

    def _visible_core_indices(self, min_confidence: float) -> set[int]:
        return {
            index
            for index in CORE_LANDMARK_INDICES
            if index < len(self.landmarks)
            and self.landmarks[index].visibility >= min_confidence
        }

    def is_present(self, min_confidence: float = 0.5) -> bool:
        visible = self._visible_core_indices(min_confidence)
        return len(visible) >= 4 and bool(visible.intersection(SHOULDER_INDICES))

    def diagnostic_metrics(self, min_confidence: float = 0.5) -> dict[str, int | bool]:
        visible = self._visible_core_indices(min_confidence)
        return {
            "visible_core_landmarks": len(visible),
            "has_visible_shoulder": bool(visible.intersection(SHOULDER_INDICES)),
        }


class PoseDetector:
    def __init__(self, model_path: str | Path) -> None:
        mp = import_module("mediapipe")
        vision = mp.tasks.vision
        options = vision.PoseLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=0.5,
            min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._mp = mp
        self._landmarker = vision.PoseLandmarker.create_from_options(options)

    def __enter__(self) -> "PoseDetector":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._landmarker.close()

    def detect(self, frame_bgr, timestamp_ms: int) -> PoseObservation:
        cv2 = import_module("cv2")
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = self._mp.Image(
            image_format=self._mp.ImageFormat.SRGB,
            data=frame_rgb,
        )
        result = self._landmarker.detect_for_video(image, timestamp_ms)
        if not result.pose_landmarks:
            return PoseObservation(())
        return PoseObservation(
            tuple(
                Landmark(
                    x=landmark.x,
                    y=landmark.y,
                    z=landmark.z,
                    visibility=landmark.visibility,
                    presence=landmark.presence,
                )
                for landmark in result.pose_landmarks[0]
            )
        )
