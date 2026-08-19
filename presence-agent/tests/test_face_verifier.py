import numpy as np

from presence_agent.face_verifier import FaceDetection, FaceState, FaceVerifier


class FakeEngine:
    def __init__(self, detections, embeddings=None):
        self._detections = iter(detections)
        self._embeddings = embeddings or {}
        self.frames = []

    def detect(self, frame):
        self.frames.append(frame)
        return next(self._detections)

    def embedding(self, frame, detection):
        return self._embeddings[detection]


def test_three_matching_frames_confirm_owner_and_keep_transition():
    face = object()
    frame = object()
    engine = FakeEngine(
        [(face,), (face,), (face,), (face,)],
        {face: np.array([1.0, 0.0], dtype=np.float32)},
    )
    verifier = FaceVerifier(engine, np.array([1.0, 0.0], dtype=np.float32))

    assert verifier.observe(frame, 0.0).state is FaceState.STARTING
    assert verifier.observe(frame, 0.1).state is FaceState.STARTING
    confirmed = verifier.observe(frame, 0.2)
    unchanged = verifier.observe(frame, 0.3)

    assert confirmed.to_payload() == {
        "state": "owner",
        "previous_state": "starting",
        "changed": True,
        "face_count": 1,
        "similarity": 1.0,
    }
    assert unchanged == confirmed
    assert engine.frames == [frame, frame, frame, frame]


def test_same_stable_identity_refreshes_similarity_without_new_transition():
    face = object()
    engine = FakeEngine(
        [(face,), (face,)],
        {
            face: np.array([1.0, 0.0], dtype=np.float32),
        },
    )
    verifier = FaceVerifier(
        engine,
        np.array([1.0, 0.0], dtype=np.float32),
        required_hits=1,
    )
    first = verifier.observe(object(), 0.0)
    engine._embeddings[face] = np.array([0.8, 0.6], dtype=np.float32)

    refreshed = verifier.observe(object(), 0.1)

    assert first.similarity == 1.0
    assert np.isclose(refreshed.similarity, 0.8)
    assert refreshed.state is FaceState.OWNER
    assert refreshed.previous_state is FaceState.STARTING
    assert refreshed.changed is True


def test_unknown_multiple_faces_and_no_face_are_stabilized():
    face = object()
    other = object()
    engine = FakeEngine(
        [(face,)] * 3 + [(face, other)] * 3 + [(), (), ()],
        {face: np.array([-1.0, 0.0], dtype=np.float32)},
    )
    verifier = FaceVerifier(
        engine,
        np.array([1.0, 0.0], dtype=np.float32),
        no_face_delay=1.0,
    )

    for now in (0.0, 0.1, 0.2):
        identity = verifier.observe(object(), now)
    assert identity.state is FaceState.UNKNOWN

    for now in (0.3, 0.4, 0.5):
        identity = verifier.observe(object(), now)
    assert identity.state is FaceState.MULTIPLE_FACES
    assert identity.face_count == 2

    assert verifier.observe(object(), 1.0).state is FaceState.MULTIPLE_FACES
    assert verifier.observe(object(), 1.999).state is FaceState.MULTIPLE_FACES
    assert verifier.observe(object(), 2.0).state is FaceState.NO_FACE


def test_not_enrolled_and_camera_transitions_are_explicit():
    not_enrolled = FaceVerifier.not_enrolled()
    assert not_enrolled.observe(object(), 0.0).to_payload() == {
        "state": "not_enrolled",
        "previous_state": "not_enrolled",
        "changed": False,
        "face_count": 0,
    }

    face = object()
    verifier = FaceVerifier(
        FakeEngine([(face,)], {face: np.array([1.0, 0.0], dtype=np.float32)}),
        np.array([1.0, 0.0], dtype=np.float32),
        required_hits=1,
    )
    assert verifier.camera_error(2).to_payload() == {
        "state": "camera_error",
        "previous_state": "starting",
        "changed": True,
        "face_count": 0,
        "camera": 2,
    }
    assert verifier.camera_recovered().to_payload() == {
        "state": "starting",
        "previous_state": "camera_error",
        "changed": True,
        "face_count": 0,
    }


def test_owner_position_is_reduced_to_three_safe_horizontal_buckets():
    frame = np.zeros((360, 600, 3), dtype=np.uint8)
    rows = [
        FaceDetection(np.zeros(15), 30, 20, 60, 60, 0.9),
        FaceDetection(np.zeros(15), 270, 20, 60, 60, 0.9),
        FaceDetection(np.zeros(15), 510, 20, 60, 60, 0.9),
    ]

    class PositionEngine(FakeEngine):
        def embedding(self, frame, detection):
            return np.array([1.0, 0.0], dtype=np.float32)

    engine = PositionEngine([(face,) for face in rows])
    verifier = FaceVerifier(
        engine,
        np.array([1.0, 0.0], dtype=np.float32),
        required_hits=1,
    )

    positions = [
        verifier.observe(frame, index / 10).horizontal_position
        for index in range(3)
    ]

    assert positions == ["left", "center", "right"]
    assert verifier.identity.to_payload()["horizontal_position"] == "right"
