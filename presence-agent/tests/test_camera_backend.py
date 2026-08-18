from presence_agent.camera_backend import (
    backend_name,
    capture_backend,
    open_camera,
    permission_hint,
)


class FakeCv2:
    CAP_ANY = 0
    CAP_DSHOW = 700
    CAP_AVFOUNDATION = 1200

    def __init__(self):
        self.created = []

    def VideoCapture(self, index, backend):
        self.created.append((index, backend))
        return f"camera-{index}"


class LegacyCv2:
    CAP_ANY = 0

    def VideoCapture(self, index, backend):
        return (index, backend)


def test_backend_name_follows_the_host_platform():
    assert backend_name("win32") == "CAP_DSHOW"
    assert backend_name("cygwin") == "CAP_DSHOW"
    assert backend_name("darwin") == "CAP_AVFOUNDATION"
    assert backend_name("linux") == "CAP_ANY"


def test_capture_backend_resolves_platform_constants():
    cv2 = FakeCv2()

    assert capture_backend(cv2, "win32") == 700
    assert capture_backend(cv2, "darwin") == 1200
    assert capture_backend(cv2, "linux") == 0


def test_capture_backend_falls_back_when_constant_is_missing():
    assert capture_backend(LegacyCv2(), "darwin") == 0


def test_open_camera_uses_the_platform_backend():
    cv2 = FakeCv2()

    assert open_camera(cv2, 1, "darwin") == "camera-1"
    assert cv2.created == [(1, 1200)]


def test_permission_hint_only_targets_macos():
    hint = permission_hint("darwin")

    assert hint is not None
    assert "隐私与安全性" in hint
    assert permission_hint("win32") is None
    assert permission_hint("linux") is None
