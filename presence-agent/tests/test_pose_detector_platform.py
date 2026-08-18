from types import SimpleNamespace

import numpy as np

from presence_agent.pose_detector import PoseDetector


class FakeDelegate:
    CPU = 0
    GPU = 1


class FakeBaseOptions:
    Delegate = FakeDelegate

    def __init__(self, model_asset_path, delegate=None):
        self.model_asset_path = model_asset_path
        self.delegate = delegate


class FakeLandmarker:
    def __init__(self, records):
        self._records = records

    def detect_for_video(self, image, timestamp_ms):
        self._records["image"] = image
        self._records["timestamp_ms"] = timestamp_ms
        return SimpleNamespace(pose_landmarks=[])

    def close(self):
        self._records["closed"] = True


class FakeImage:
    def __init__(self, image_format, data):
        self.image_format = image_format
        self.data = data


def fake_mediapipe():
    records = {}

    class FakePoseLandmarker:
        @staticmethod
        def create_from_options(options):
            records["options"] = options
            return FakeLandmarker(records)

    vision = SimpleNamespace(
        PoseLandmarkerOptions=lambda **kwargs: SimpleNamespace(**kwargs),
        RunningMode=SimpleNamespace(VIDEO="video"),
        PoseLandmarker=FakePoseLandmarker,
    )
    module = SimpleNamespace(
        tasks=SimpleNamespace(BaseOptions=FakeBaseOptions, vision=vision),
        Image=FakeImage,
        ImageFormat=SimpleNamespace(SRGB="srgb", SRGBA="srgba"),
    )
    return module, records


def bgr_frame():
    return np.zeros((16, 24, 3), dtype=np.uint8)


def test_macos_uses_the_metal_delegate_and_rgba_frames():
    module, records = fake_mediapipe()

    detector = PoseDetector(
        "pose.task", mediapipe_module=module, platform="darwin"
    )
    detector.detect(bgr_frame(), 42)

    assert records["options"].base_options.delegate == FakeDelegate.GPU
    assert records["image"].image_format == "srgba"
    assert records["image"].data.shape == (16, 24, 4)
    assert records["timestamp_ms"] == 42


def test_other_platforms_keep_the_default_delegate_and_rgb_frames():
    module, records = fake_mediapipe()

    detector = PoseDetector(
        "pose.task", mediapipe_module=module, platform="win32"
    )
    detector.detect(bgr_frame(), 7)

    assert records["options"].base_options.delegate is None
    assert records["image"].image_format == "srgb"
    assert records["image"].data.shape == (16, 24, 3)


def test_close_releases_the_landmarker():
    module, records = fake_mediapipe()

    with PoseDetector("pose.task", mediapipe_module=module, platform="linux"):
        pass

    assert records["closed"] is True
