"""Multi-frame owner enrollment over frames supplied by the desktop app."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


@dataclass(frozen=True)
class EnrollmentTemplate:
    embedding: Any
    model_sha256: str
    created_at: str
    sample_count: int
    schema_version: int = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class EnrollmentCollector:
    def __init__(
        self,
        *,
        engine: Any,
        display_name: str,
        template_path: Path,
        metadata_path: Path,
        recognizer_model_path: Path,
        quality_checker: Callable | None = None,
        centroid_builder: Callable | None = None,
        template_saver: Callable | None = None,
        metadata_saver: Callable | None = None,
        model_hash_provider: Callable[[Path], str] = _sha256,
        now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sample_id_factory: Callable[[], str] = lambda: str(uuid4()),
        required_samples: int = 20,
        sample_interval: float = 0.2,
    ) -> None:
        if required_samples <= 0:
            raise ValueError("required_samples must be positive")
        if sample_interval < 0:
            raise ValueError("sample_interval must be nonnegative")
        self._engine = engine
        self._display_name = display_name.strip()
        self._template_path = Path(template_path)
        self._metadata_path = Path(metadata_path)
        self._recognizer_model_path = Path(recognizer_model_path)
        self._quality_checker = quality_checker or self._default_quality_checker
        self._centroid_builder = centroid_builder or self._default_centroid_builder
        self._template_saver = template_saver or self._default_template_saver
        self._metadata_saver = metadata_saver or self._default_metadata_saver
        self._model_hash_provider = model_hash_provider
        self._now_utc = now_utc
        self._sample_id_factory = sample_id_factory
        self._required_samples = required_samples
        self._sample_interval = sample_interval
        self._samples = []
        self._last_accepted_at: float | None = None
        self._completed: dict[str, Any] | None = None

    @staticmethod
    def _default_quality_checker(aligned, detection):
        from presence_agent.face_verifier import sample_quality

        return sample_quality(aligned, detection)

    @staticmethod
    def _default_centroid_builder(samples, trim_count):
        from presence_agent.face_verifier import build_centroid

        return build_centroid(samples, trim_count)

    @staticmethod
    def _default_template_saver(path, template, overwrite):
        from presence_agent.face_template import save_template

        save_template(path, template, overwrite=overwrite)

    @staticmethod
    def _default_metadata_saver(path, value):
        from presence_agent.face_template import save_metadata

        save_metadata(path, value)

    def _progress(self, reason: str) -> dict[str, Any]:
        return {
            "type": "enrollment_progress",
            "accepted": len(self._samples),
            "required": self._required_samples,
            "reason": reason,
        }

    def accept(self, frame: Any, now: float) -> dict[str, Any]:
        if self._completed is not None:
            return dict(self._completed)
        if self._last_accepted_at is not None:
            elapsed = now - self._last_accepted_at
            if elapsed < self._sample_interval and not math.isclose(
                elapsed,
                self._sample_interval,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                return self._progress("sample_too_soon")

        detections = self._engine.detect(frame)
        if not detections:
            return self._progress("no_face")
        if len(detections) != 1:
            return self._progress("multiple_faces")

        aligned = self._engine.align(frame, detections[0])
        accepted, reason, _quality_score = self._quality_checker(
            aligned, detections[0]
        )
        if not accepted:
            return self._progress(reason)

        self._samples.append(self._engine.embedding_from_aligned(aligned))
        self._last_accepted_at = now
        if len(self._samples) < self._required_samples:
            return self._progress("accepted")

        trim_count = min(2, len(self._samples) - 1)
        centroid, retained = self._centroid_builder(self._samples, trim_count)
        stored_at = self._now_utc().astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        sample_id = self._sample_id_factory()
        template = EnrollmentTemplate(
            embedding=centroid,
            model_sha256=self._model_hash_provider(self._recognizer_model_path),
            created_at=stored_at,
            sample_count=retained,
        )
        metadata = {
            "profile_id": "owner",
            "sample_id": sample_id,
            "display_name": self._display_name,
            "stored_at": stored_at,
            "sample_count": retained,
        }
        self._template_saver(self._template_path, template, overwrite=True)
        self._metadata_saver(self._metadata_path, metadata)
        self._completed = {"type": "enrollment_complete", **metadata}
        return dict(self._completed)
