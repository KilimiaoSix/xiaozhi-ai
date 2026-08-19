#!/usr/bin/env python3
"""下载分心检测用的 MediaPipe Object Detector 模型（efficientdet_lite0）。

流程六（番茄钟专注相位手机分心检测）用它识别画面里的 "cell phone"。模型体积
约 4~5MB，不适合放进仓库版本控制，改为运行时按需下载到 data/models/，与
presence-agent 的 pose_landmarker 模型同一套约定（见
.claude/worktrees/beautiful-panini-0a42fb/presence-agent/models/）。

幂等：目标文件已存在且大小在合理区间内就跳过，不重复打网络请求。
"""

import ssl
import sys
import urllib.request
from pathlib import Path

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/object_detector/"
    "efficientdet_lite0/float16/latest/efficientdet_lite0.tflite"
)

# 相对于本脚本所在仓库（xiaozhi-server）的模型落地路径，与
# core/camera_stream/distraction_observer.py 里的默认路径保持一致。
TARGET_PATH = Path(__file__).resolve().parent.parent / "data" / "models" / "efficientdet_lite0.tflite"

# 该模型官方体积约 4.6MB；用 1MB 做下限阈值，既能跳过已下载完整的文件，
# 又能识别出网络中断留下的半截文件（半截文件通常远小于 1MB 或干脆是 HTML 错误页）。
MIN_VALID_SIZE_BYTES = 1_000_000


def _is_valid_existing_file(path: Path) -> bool:
    if not path.is_file():
        return False
    return path.stat().st_size >= MIN_VALID_SIZE_BYTES


def _ssl_context() -> ssl.SSLContext:
    """优先用 certifi 的 CA 证书验证 HTTPS。

    本机 python.org 发行版没有系统信任库，走公司代理时 urllib 默认上下文常报
    CERTIFICATE_VERIFY_FAILED（本机现场复现过）。certifi 不可用时退回默认
    上下文，而不是关验证——下载模型这种一次性操作没有理由弱化证书校验。
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def download_model(target_path: Path = TARGET_PATH, url: str = MODEL_URL) -> Path:
    """下载模型到 target_path；已存在且大小合理则跳过。返回最终文件路径。"""
    if _is_valid_existing_file(target_path):
        size = target_path.stat().st_size
        print(f"模型已存在，跳过下载: {target_path} ({size} bytes)")
        return target_path

    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".part")
    print(f"下载模型: {url}")
    print(f"  -> {target_path}")
    try:
        with urllib.request.urlopen(url, timeout=60, context=_ssl_context()) as response:
            data = response.read()
        if len(data) < MIN_VALID_SIZE_BYTES:
            raise ValueError(
                f"下载内容只有 {len(data)} bytes，小于预期下限 "
                f"{MIN_VALID_SIZE_BYTES} bytes，疑似下载失败或被拦截"
            )
        tmp_path.write_bytes(data)
        tmp_path.replace(target_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    size = target_path.stat().st_size
    print(f"下载完成: {target_path} ({size} bytes)")
    return target_path


def main() -> int:
    try:
        download_model()
    except Exception as exc:  # noqa: BLE001 - 脚本顶层，打印后以非零退出码结束
        print(f"模型下载失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
