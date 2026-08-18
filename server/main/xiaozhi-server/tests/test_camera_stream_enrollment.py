from datetime import datetime, timezone

import numpy as np

from core.camera_stream.enrollment import EnrollmentCollector


class FakeDetection:
    pass


class FakeEngine:
    def __init__(self, face_count=1):
        self.face_count = face_count
        self.embeddings = 0

    def detect(self, frame):
        return tuple(FakeDetection() for _ in range(self.face_count))

    def align(self, frame, detection):
        return frame

    def embedding_from_aligned(self, aligned):
        self.embeddings += 1
        return np.array([1.0, float(self.embeddings)], dtype=np.float32)


def test_collects_twenty_spaced_samples_and_atomically_completes(tmp_path):
    saved = []
    metadata = []
    collector = EnrollmentCollector(
        engine=FakeEngine(),
        display_name="主人",
        template_path=tmp_path / "owner_template.npz",
        metadata_path=tmp_path / "owner.json",
        recognizer_model_path=tmp_path / "sface.onnx",
        quality_checker=lambda aligned, detection: (True, "accepted", 120.0),
        centroid_builder=lambda samples, trim_count: (
            np.array([0.6, 0.8], dtype=np.float32),
            len(samples) - trim_count,
        ),
        template_saver=lambda path, template, overwrite: saved.append(
            (path, template, overwrite)
        ),
        metadata_saver=lambda path, value: metadata.append((path, value)),
        model_hash_provider=lambda path: "a" * 64,
        now_utc=lambda: datetime(2026, 8, 18, tzinfo=timezone.utc),
        sample_id_factory=lambda: "sample-id",
    )

    for index in range(19):
        progress = collector.accept(object(), now=index * 0.2)
        assert progress["type"] == "enrollment_progress"
        assert progress["accepted"] == index + 1

    completed = collector.accept(object(), now=19 * 0.2)

    assert completed == {
        "type": "enrollment_complete",
        "profile_id": "owner",
        "sample_id": "sample-id",
        "stored_at": "2026-08-18T00:00:00Z",
        "sample_count": 18,
        "display_name": "主人",
    }
    assert len(saved) == 1
    assert saved[0][2] is True
    assert saved[0][1].sample_count == 18
    assert metadata[0][1]["sample_id"] == "sample-id"


def test_rejects_quality_and_rate_limited_samples_without_progress(tmp_path):
    quality = iter([(False, "too_blurry", 20.0), (True, "accepted", 120.0)])
    collector = EnrollmentCollector(
        engine=FakeEngine(),
        display_name="主人",
        template_path=tmp_path / "owner_template.npz",
        metadata_path=tmp_path / "owner.json",
        recognizer_model_path=tmp_path / "sface.onnx",
        quality_checker=lambda aligned, detection: next(quality),
        required_samples=2,
        centroid_builder=lambda samples, trim_count: (samples[0], 1),
        template_saver=lambda *args, **kwargs: None,
        metadata_saver=lambda *args, **kwargs: None,
        model_hash_provider=lambda path: "a" * 64,
    )

    blurry = collector.accept(object(), now=0.0)
    accepted = collector.accept(object(), now=0.2)
    too_soon = collector.accept(object(), now=0.3)

    assert blurry["reason"] == "too_blurry"
    assert blurry["accepted"] == 0
    assert accepted["accepted"] == 1
    assert too_soon == {
        "type": "enrollment_progress",
        "accepted": 1,
        "required": 2,
        "reason": "sample_too_soon",
    }


def test_requires_exactly_one_face(tmp_path):
    def collector(face_count):
        return EnrollmentCollector(
            engine=FakeEngine(face_count),
            display_name="主人",
            template_path=tmp_path / "owner_template.npz",
            metadata_path=tmp_path / "owner.json",
            recognizer_model_path=tmp_path / "sface.onnx",
            quality_checker=lambda *args: (True, "accepted", 120.0),
            template_saver=lambda *args, **kwargs: None,
            metadata_saver=lambda *args, **kwargs: None,
            model_hash_provider=lambda path: "a" * 64,
        )

    assert collector(0).accept(object(), 0.0)["reason"] == "no_face"
    assert collector(2).accept(object(), 0.0)["reason"] == "multiple_faces"
