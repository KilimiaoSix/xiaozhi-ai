from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from presence_agent.app import (
    _default_face_verifier_factory,
    _strict_timestamp_ms,
    parse_args,
    run,
)
from presence_agent.face_verifier import FaceIdentity, FaceState
from presence_agent.pose_detector import Landmark, PoseObservation
from presence_agent.state import PresenceState


def visible_pose():
    landmarks = [Landmark(0, 0, 0, 0.0, 0.0) for _ in range(33)]
    for index in (0, 11, 13, 23):
        landmarks[index] = Landmark(0, 0, 0, 0.9, 0.9)
    return PoseObservation(tuple(landmarks))


class FakeCamera:
    def __init__(self, opened, reads):
        self.opened = opened
        self.reads = deque(reads)
        self.released = False
        self.settings = []

    def isOpened(self):
        return self.opened

    def read(self):
        return self.reads.popleft()

    def set(self, name, value):
        self.settings.append((name, value))

    def release(self):
        self.released = True


class FakeCv2:
    CAP_DSHOW = 700
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4

    def __init__(self, cameras):
        self.cameras = deque(cameras)
        self.created = []
        self.imshow_calls = 0
        self.destroyed = False

    def VideoCapture(self, index, backend):
        camera = self.cameras.popleft()
        self.created.append((index, backend, camera))
        return camera

    @staticmethod
    def flip(frame, direction):
        return frame

    def imshow(self, name, frame):
        self.imshow_calls += 1

    @staticmethod
    def waitKey(delay):
        return -1

    def destroyAllWindows(self):
        self.destroyed = True


class FakeDetector:
    def __init__(self, observations):
        self.observations = deque(observations)
        self.timestamps = []
        self.frames = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def detect(self, frame, timestamp_ms):
        self.frames.append(frame)
        self.timestamps.append(timestamp_ms)
        return self.observations.popleft()


class FakeReporter:
    def __init__(self):
        self.step_calls = 0

    def run(self, stop_event):
        stop_event.wait()

    def step(self, now_monotonic):
        self.step_calls += 1
        return 0.1


class FakeFaceVerifier:
    def __init__(self):
        self.frames = []
        self.identity = FaceIdentity(
            FaceState.STARTING, FaceState.STARTING, False, 0
        )

    def observe(self, frame, now_seconds):
        self.frames.append(frame)
        if len(self.frames) == 3:
            self.identity = FaceIdentity(
                FaceState.OWNER, FaceState.STARTING, True, 1, 0.72
            )
        return self.identity

    def camera_error(self, camera):
        self.identity = FaceIdentity(
            FaceState.CAMERA_ERROR,
            self.identity.state,
            True,
            0,
            camera=camera,
        )
        return self.identity

    def camera_recovered(self):
        self.identity = FaceIdentity(
            FaceState.STARTING, self.identity.state, True, 0
        )
        return self.identity


class FakeThread:
    def __init__(self, target, **kwargs):
        self.target = target
        self.started = False
        self.joined = False

    def start(self):
        self.started = True

    def join(self, timeout=None):
        self.joined = True


def make_args(model, **overrides):
    values = {
        "server_url": "http://127.0.0.1:8003",
        "workstation_id": "desk-test",
        "auth_token": "",
        "camera": 0,
        "width": 640,
        "height": 480,
        "absent_after": 2.0,
        "heartbeat_seconds": 15.0,
        "model": model,
        "preview": False,
        "smoke_frames": 3,
        "camera_retry_seconds": 0.01,
        "face_verification": True,
        "face_detector_model": Path("face-detector.onnx"),
        "face_recognizer_model": Path("face-recognizer.onnx"),
        "face_template": Path("owner-template.npz"),
        "face_threshold": 0.45,
        "face_hits": 3,
        "no_face_delay": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def dependencies(cv2, detector, capture):
    face_verifier = FakeFaceVerifier()
    capture["face_verifier"] = face_verifier

    def reporter_factory(latest, args):
        capture["latest"] = latest
        capture["reporter"] = FakeReporter()
        return capture["reporter"]

    return {
        "cv2_module": cv2,
        "detector_factory": lambda model: detector,
        "reporter_factory": reporter_factory,
        "face_verifier_factory": lambda args: face_verifier,
        "thread_factory": FakeThread,
        "sleep": lambda seconds: None,
        "monotonic": iter([1.0, 1.1, 1.2, 1.3, 1.4, 1.5]).__next__,
    }


def test_parse_args_has_headless_service_defaults():
    args = parse_args([])

    assert args.server_url == "http://127.0.0.1:8003"
    assert args.camera == 0
    assert args.preview is False
    assert args.smoke_frames == 0
    assert args.heartbeat_seconds == 15.0
    assert args.workstation_id
    assert args.face_verification is True
    assert args.face_threshold == 0.45
    assert args.face_hits == 3
    assert args.no_face_delay == 1.0


@pytest.mark.parametrize(
    "argv",
    [
        ["--width", "0"],
        ["--height", "-1"],
        ["--absent-after", "0"],
        ["--heartbeat-seconds", "nan"],
        ["--smoke-frames", "-1"],
        ["--workstation-id", "desk test"],
    ],
)
def test_parse_args_rejects_invalid_values(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)


def test_strict_timestamp_always_increases():
    assert _strict_timestamp_ms(1.0, 1000) == 1001
    assert _strict_timestamp_ms(1.234, -1) == 1234


def test_missing_model_returns_configuration_error(tmp_path):
    args = make_args(tmp_path / "missing.task")

    assert run(args) == 2


def test_missing_face_models_fail_even_when_template_is_not_enrolled(tmp_path):
    args = make_args(
        tmp_path / "pose.task",
        face_detector_model=tmp_path / "missing-detector.onnx",
        face_recognizer_model=tmp_path / "missing-recognizer.onnx",
        face_template=tmp_path / "missing-template.npz",
    )

    with pytest.raises(FileNotFoundError, match="Face model"):
        _default_face_verifier_factory(args)


def test_smoke_frames_publish_present_and_clean_up(tmp_path):
    model = tmp_path / "model.task"
    model.write_bytes(b"model")
    frames = [object() for _ in range(3)]
    camera = FakeCamera(True, [(True, frame) for frame in frames])
    cv2 = FakeCv2([camera])
    detector = FakeDetector([visible_pose(), visible_pose(), visible_pose()])
    capture = {}

    result = run(make_args(model), **dependencies(cv2, detector, capture))

    assert result == 0
    assert capture["latest"].read().state is PresenceState.PRESENT
    assert capture["latest"].read().metrics["visible_core_landmarks"] == 4
    assert capture["latest"].read().identity["state"] == "owner"
    assert capture["latest"].read().identity["similarity"] == 0.72
    assert detector.frames == frames
    assert capture["face_verifier"].frames == frames
    assert detector.timestamps == sorted(set(detector.timestamps))
    assert camera.released is True
    assert detector.closed is True
    assert cv2.imshow_calls == 0
    assert cv2.destroyed is True


def test_shutdown_flushes_latest_snapshot(tmp_path):
    model = tmp_path / "model.task"
    model.write_bytes(b"model")
    camera = FakeCamera(True, [(True, object())])
    cv2 = FakeCv2([camera])
    detector = FakeDetector([visible_pose()])
    capture = {}

    run(make_args(model, smoke_frames=1), **dependencies(cv2, detector, capture))

    assert capture["reporter"].step_calls == 1


def test_camera_open_failure_reports_error_then_recovers(tmp_path):
    model = tmp_path / "model.task"
    model.write_bytes(b"model")
    failed = FakeCamera(False, [])
    recovered = FakeCamera(True, [(True, object())])
    cv2 = FakeCv2([failed, recovered])
    detector = FakeDetector([PoseObservation(())])
    capture = {}

    result = run(
        make_args(model, smoke_frames=1),
        **dependencies(cv2, detector, capture),
    )

    snapshot = capture["latest"].read()
    assert result == 0
    assert snapshot.state is PresenceState.STARTING
    assert snapshot.previous_state is PresenceState.CAMERA_ERROR
    assert snapshot.reason == "camera_recovered"
    assert failed.released is True
    assert recovered.released is True


def test_camera_read_failure_reopens_camera(tmp_path):
    model = tmp_path / "model.task"
    model.write_bytes(b"model")
    failed = FakeCamera(True, [(False, None)])
    recovered = FakeCamera(True, [(True, object())])
    cv2 = FakeCv2([failed, recovered])
    detector = FakeDetector([PoseObservation(())])
    capture = {}

    result = run(
        make_args(model, smoke_frames=1),
        **dependencies(cv2, detector, capture),
    )

    assert result == 0
    assert len(cv2.created) == 2
    assert failed.released is True
