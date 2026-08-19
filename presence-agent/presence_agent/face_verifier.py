"""Local YuNet/SFace owner verification over frames owned by presence-agent."""

from dataclasses import dataclass
from enum import Enum
import math
import os

import cv2
import numpy as np


class FaceState(str, Enum):
    STARTING = "starting"
    NOT_ENROLLED = "not_enrolled"
    NO_FACE = "no_face"
    OWNER = "owner"
    UNKNOWN = "unknown"
    MULTIPLE_FACES = "multiple_faces"
    CAMERA_ERROR = "camera_error"


@dataclass(frozen=True)
class FaceDetection:
    row: np.ndarray
    x: float
    y: float
    width: float
    height: float
    score: float


@dataclass(frozen=True)
class FaceObservation:
    state: FaceState
    face_count: int
    similarity: float | None = None
    horizontal_position: str | None = None


@dataclass(frozen=True)
class FaceIdentity:
    state: FaceState
    previous_state: FaceState
    changed: bool
    face_count: int
    similarity: float | None = None
    camera: int | None = None
    horizontal_position: str | None = None

    def to_payload(self) -> dict:
        payload = {
            "state": self.state.value,
            "previous_state": self.previous_state.value,
            "changed": self.changed,
            "face_count": self.face_count,
        }
        if self.similarity is not None:
            payload["similarity"] = round(float(self.similarity), 6)
        if self.camera is not None:
            payload["camera"] = self.camera
        if self.horizontal_position is not None:
            payload["horizontal_position"] = self.horizontal_position
        return payload


def normalize(vector) -> np.ndarray:
    result = np.asarray(vector, dtype=np.float32)
    if result.ndim != 1 or result.size == 0 or not np.all(np.isfinite(result)):
        raise ValueError("embedding must be a finite one-dimensional vector")
    norm = float(np.linalg.norm(result))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("embedding must be nonzero")
    return np.asarray(result / norm, dtype=np.float32)


def cosine_similarity(left, right) -> float:
    left_normalized = normalize(left)
    right_normalized = normalize(right)
    if left_normalized.shape != right_normalized.shape:
        raise ValueError("embeddings must have the same shape")
    return float(np.dot(left_normalized, right_normalized))


def build_centroid(samples, trim_count: int = 2) -> tuple[np.ndarray, int]:
    normalized = [normalize(sample) for sample in samples]
    if not normalized or trim_count < 0 or trim_count >= len(normalized):
        raise ValueError("trim_count must leave at least one sample")
    shape = normalized[0].shape
    if any(sample.shape != shape for sample in normalized):
        raise ValueError("all samples must have the same shape")
    provisional = normalize(np.mean(normalized, axis=0))
    similarities = np.asarray([np.dot(sample, provisional) for sample in normalized])
    retained = np.argsort(similarities, kind="stable")[trim_count:]
    return normalize(np.mean([normalized[index] for index in retained], axis=0)), len(retained)


def sample_quality(
    aligned_face,
    detection: FaceDetection,
    min_face_size: int = 120,
    min_blur_score: float = 80.0,
) -> tuple[bool, str, float]:
    if detection.width < min_face_size or detection.height < min_face_size:
        return False, "face_too_small", 0.0
    grayscale = cv2.cvtColor(aligned_face, cv2.COLOR_BGR2GRAY)
    blur_score = float(cv2.Laplacian(grayscale, cv2.CV_64F).var())
    if not math.isfinite(blur_score) or blur_score < min_blur_score:
        return False, "too_blurry", blur_score
    return True, "accepted", blur_score


class FaceEngine:
    def __init__(
        self,
        detector_model,
        recognizer_model,
        *,
        cv2_module=cv2,
        detector_score_threshold: float = 0.8,
    ) -> None:
        self._cv2 = cv2_module
        self._detector = cv2_module.FaceDetectorYN.create(
            os.fspath(detector_model), "", (320, 320), detector_score_threshold, 0.3, 5000
        )
        self._recognizer = cv2_module.FaceRecognizerSF.create(
            os.fspath(recognizer_model), ""
        )

    def detect(self, frame) -> tuple[FaceDetection, ...]:
        if frame.ndim < 2 or frame.shape[0] <= 0 or frame.shape[1] <= 0:
            raise ValueError("frame must have nonzero width and height")
        height, width = frame.shape[:2]
        self._detector.setInputSize((int(width), int(height)))
        _, rows = self._detector.detect(frame)
        if rows is None:
            return ()
        detections = []
        for raw_row in rows:
            row = np.asarray(raw_row, dtype=np.float32).reshape(-1)
            if row.size < 15:
                raise ValueError("YuNet detection row must contain at least 15 values")
            detections.append(
                FaceDetection(
                    row=row.copy(),
                    x=float(row[0]),
                    y=float(row[1]),
                    width=float(row[2]),
                    height=float(row[3]),
                    score=float(row[14]),
                )
            )
        return tuple(detections)

    def align(self, frame, detection: FaceDetection):
        return self._recognizer.alignCrop(frame, detection.row)

    def embedding_from_aligned(self, aligned_face) -> np.ndarray:
        return normalize(np.asarray(self._recognizer.feature(aligned_face)).reshape(-1))

    def embedding(self, frame, detection: FaceDetection) -> np.ndarray:
        return self.embedding_from_aligned(self.align(frame, detection))


class FaceVerifier:
    def __init__(
        self,
        engine,
        owner_embedding,
        *,
        threshold: float = 0.45,
        required_hits: int = 3,
        no_face_delay: float = 1.0,
    ) -> None:
        if required_hits <= 0:
            raise ValueError("required_hits must be positive")
        if not math.isfinite(no_face_delay) or no_face_delay < 0:
            raise ValueError("no_face_delay must be finite and nonnegative")
        if not math.isfinite(threshold) or not -1 <= threshold <= 1:
            raise ValueError("threshold must be between -1 and 1")
        self._engine = engine
        self._owner_embedding = normalize(owner_embedding) if owner_embedding is not None else None
        self._threshold = threshold
        self._required_hits = required_hits
        self._no_face_delay = no_face_delay
        initial = FaceState.NOT_ENROLLED if engine is None else FaceState.STARTING
        self._identity = FaceIdentity(initial, initial, False, 0)
        self._candidate = None
        self._candidate_streak = 0
        self._no_face_started_at = None

    @classmethod
    def not_enrolled(cls) -> "FaceVerifier":
        return cls(None, None)

    @property
    def identity(self) -> FaceIdentity:
        return self._identity

    def observe(self, frame, now_seconds: float) -> FaceIdentity:
        if self._engine is None:
            return self._identity
        detections = self._engine.detect(frame)
        if not detections:
            observation = FaceObservation(FaceState.NO_FACE, 0)
        elif len(detections) > 1:
            observation = FaceObservation(FaceState.MULTIPLE_FACES, len(detections))
        else:
            query = self._engine.embedding(frame, detections[0])
            similarity = cosine_similarity(query, self._owner_embedding)
            state = FaceState.OWNER if similarity >= self._threshold else FaceState.UNKNOWN
            observation = FaceObservation(
                state,
                1,
                similarity,
                self._horizontal_position(frame, detections[0]),
            )
        self._update(observation, now_seconds)
        return self._identity

    def camera_error(self, camera: int) -> FaceIdentity:
        self._reset_candidate()
        return self._transition(FaceObservation(FaceState.CAMERA_ERROR, 0), camera=camera)

    def camera_recovered(self) -> FaceIdentity:
        self._reset_candidate()
        state = FaceState.NOT_ENROLLED if self._engine is None else FaceState.STARTING
        return self._transition(FaceObservation(state, 0))

    def _update(self, observation: FaceObservation, now_seconds: float) -> None:
        if observation.state is FaceState.NO_FACE:
            if self._candidate is not FaceState.NO_FACE:
                self._candidate = FaceState.NO_FACE
                self._candidate_streak = 0
                self._no_face_started_at = now_seconds
            elapsed = now_seconds - self._no_face_started_at
            if elapsed >= self._no_face_delay or math.isclose(
                elapsed, self._no_face_delay, rel_tol=0.0, abs_tol=1e-9
            ):
                self._transition(observation)
            return

        self._no_face_started_at = None
        if observation.state is self._candidate:
            self._candidate_streak += 1
        else:
            self._candidate = observation.state
            self._candidate_streak = 1
        if self._candidate_streak >= self._required_hits:
            self._transition(observation)

    def _transition(self, observation: FaceObservation, camera=None) -> FaceIdentity:
        if observation.state is self._identity.state:
            self._identity = FaceIdentity(
                state=self._identity.state,
                previous_state=self._identity.previous_state,
                changed=self._identity.changed,
                face_count=observation.face_count,
                similarity=observation.similarity,
                camera=camera if camera is not None else self._identity.camera,
                horizontal_position=observation.horizontal_position,
            )
            return self._identity
        self._identity = FaceIdentity(
            state=observation.state,
            previous_state=self._identity.state,
            changed=True,
            face_count=observation.face_count,
            similarity=observation.similarity,
            camera=camera,
            horizontal_position=observation.horizontal_position,
        )
        return self._identity

    @staticmethod
    def _horizontal_position(frame, detection) -> str | None:
        shape = getattr(frame, "shape", None)
        width = shape[1] if shape is not None and len(shape) >= 2 else 0
        if not width or not hasattr(detection, "x") or not hasattr(detection, "width"):
            return None
        center = (float(detection.x) + float(detection.width) / 2.0) / float(width)
        if center < 0.4:
            return "left"
        if center > 0.6:
            return "right"
        return "center"

    def _reset_candidate(self) -> None:
        self._candidate = None
        self._candidate_streak = 0
        self._no_face_started_at = None
