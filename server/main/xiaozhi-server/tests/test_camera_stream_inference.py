from dataclasses import dataclass

from core.camera_stream.inference import CameraInferenceRuntime


FRAME = object()


class FakeDecoder:
    def __init__(self):
        self.calls = []

    def __call__(self, jpeg):
        self.calls.append(jpeg)
        return FRAME


class FakePoseObservation:
    def is_present(self):
        return True

    def diagnostic_metrics(self):
        return {"visible_core_landmarks": 5, "has_visible_shoulder": True}


class FakePoseDetector:
    def __init__(self):
        self.calls = []
        self.closed = False

    def detect(self, frame, timestamp_ms):
        self.calls.append((frame, timestamp_ms))
        return FakePoseObservation()

    def close(self):
        self.closed = True


@dataclass
class FakeState:
    value: str


class FakePresenceTracker:
    def __init__(self):
        self.state = FakeState("starting")
        self.calls = []

    def update(self, detected, now):
        self.calls.append((detected, now))
        self.state = FakeState("present")
        return self.state

    def metrics(self, now):
        return {"positive_streak": 3, "seconds_since_last_positive": 0.0}


class FakeIdentity:
    def to_payload(self):
        return {
            "state": "owner",
            "previous_state": "starting",
            "changed": True,
            "face_count": 1,
            "similarity": 0.731245,
        }


class FakeFaceVerifier:
    def __init__(self):
        self.frames = []

    def observe(self, frame, now):
        self.frames.append((frame, now))
        return FakeIdentity()


def test_decodes_once_and_runs_pose_and_face_on_the_same_frame():
    decoder = FakeDecoder()
    pose = FakePoseDetector()
    tracker = FakePresenceTracker()
    face = FakeFaceVerifier()
    runtime = CameraInferenceRuntime(
        decoder=decoder,
        pose_detector=pose,
        presence_tracker=tracker,
        face_verifier=face,
        face_threshold=0.45,
    )

    result = runtime.process(b"jpeg", sequence=7, now=12.5)

    assert decoder.calls == [b"jpeg"]
    assert pose.calls[0][0] is FRAME
    assert face.frames == [(FRAME, 12.5)]
    assert result["sequence"] == 7
    assert result["presence"] == {"state": "present", "changed": True}
    assert result["identity"] == {
        "state": "owner",
        "previous_state": "starting",
        "changed": True,
        "face_count": 1,
        "face_detected": True,
        "similarity": 0.731245,
        "threshold": 0.45,
        "matched": True,
    }
    assert result["metrics"]["visible_core_landmarks"] == 5
    assert result["metrics"]["positive_streak"] == 3


class NoFaceIdentity:
    def to_payload(self):
        return {
            "state": "no_face",
            "previous_state": "owner",
            "changed": True,
            "face_count": 0,
        }


class NoFaceVerifier:
    def observe(self, frame, now):
        return NoFaceIdentity()


def test_non_single_face_result_does_not_reuse_similarity():
    runtime = CameraInferenceRuntime(
        decoder=lambda _: FRAME,
        pose_detector=FakePoseDetector(),
        presence_tracker=FakePresenceTracker(),
        face_verifier=NoFaceVerifier(),
    )

    identity = runtime.process(b"jpeg", sequence=1, now=1.0)["identity"]

    assert identity["face_detected"] is False
    assert identity["matched"] is False
    assert "similarity" not in identity
    assert "threshold" not in identity


def test_close_releases_pose_detector():
    pose = FakePoseDetector()
    runtime = CameraInferenceRuntime(
        decoder=lambda _: FRAME,
        pose_detector=pose,
        presence_tracker=FakePresenceTracker(),
        face_verifier=NoFaceVerifier(),
    )

    runtime.close()

    assert pose.closed is True
