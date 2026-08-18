"""Platform-aware OpenCV capture backend selection."""

import sys


WINDOWS_BACKEND = "CAP_DSHOW"
MACOS_BACKEND = "CAP_AVFOUNDATION"
FALLBACK_BACKEND = "CAP_ANY"

MACOS_PERMISSION_HINT = (
    "macOS 未授权摄像头时 OpenCV 也会返回打开失败："
    "请在 系统设置 > 隐私与安全性 > 摄像头 中允许当前终端或 Python，然后重新运行。"
)


def backend_name(platform: str = sys.platform) -> str:
    if platform.startswith("win") or platform == "cygwin":
        return WINDOWS_BACKEND
    if platform == "darwin":
        return MACOS_BACKEND
    return FALLBACK_BACKEND


def capture_backend(cv2_module, platform: str = sys.platform) -> int:
    backend = getattr(cv2_module, backend_name(platform), None)
    if backend is None:
        backend = getattr(cv2_module, FALLBACK_BACKEND, 0)
    return int(backend)


def open_camera(cv2_module, index: int, platform: str = sys.platform):
    return cv2_module.VideoCapture(index, capture_backend(cv2_module, platform))


def permission_hint(platform: str = sys.platform) -> str | None:
    return MACOS_PERMISSION_HINT if platform == "darwin" else None
