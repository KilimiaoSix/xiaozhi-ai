#!/usr/bin/env python3
"""下载 MediaPipe 手势识别模型到 data/models/。

手势审批链路（core/camera_stream/gesture_observer.py）需要一个 .task 模型文件。
它约 8MB，不入库（同 SenseVoiceSmall 权重的处理方式），部署时跑一次本脚本即可。

已存在且大小合理就跳过，可重复执行。用法：

    .venv/bin/python scripts/download_gesture_model.py            # 下载并校验
    .venv/bin/python scripts/download_gesture_model.py --force    # 强制重下
    .venv/bin/python scripts/download_gesture_model.py --verify   # 顺带真实加载一次

注意 --verify 会 import mediapipe 并真的建一次 GestureRecognizer，
用来确认这份文件不只是"大小对"，而是真能被当前版本的 mediapipe 加载。
"""

import argparse
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
    "gesture_recognizer/float16/latest/gesture_recognizer.task"
)

# 官方 float16 包约 8MB。低于这个下限说明下到的是错误页或半截文件，
# 直接当成"没有模型"重下，别让一份坏文件一路带到运行时才炸。
MIN_BYTES = 1_000_000

DEFAULT_RELATIVE_PATH = os.path.join("data", "models", "gesture_recognizer.task")


def project_dir() -> str:
    """仓库根目录（scripts/ 的上一级），不依赖当前工作目录。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_model_path() -> str:
    return os.path.join(project_dir(), DEFAULT_RELATIVE_PATH)


def is_present(path: str) -> bool:
    """文件在且大小合理才算已下载。"""
    try:
        return os.path.getsize(path) >= MIN_BYTES
    except OSError:
        return False


def _fetch_urllib(url: str, tmp_path: str, timeout: float) -> None:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        with open(tmp_path, "wb") as handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)


def _fetch_curl(url: str, tmp_path: str, timeout: float) -> None:
    """用 curl 重下一次。

    存在的理由：公司网络走 MITM 代理时，代理的根证书装在系统钥匙串里，
    而 Python 只信 certifi 那份，urllib 会 CERTIFICATE_VERIFY_FAILED。
    curl 用系统信任库，照样是「验证过的」下载——这里不是关掉校验，
    只是换一个信任库。
    """
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("系统里没有 curl，无法回退下载")
    subprocess.run(
        [curl, "-fsSL", "--max-time", str(int(timeout)), "-o", tmp_path, url],
        check=True,
    )


def download(url: str, dest: str, timeout: float = 120.0) -> int:
    """下载到临时文件再原子替换，避免中断留下半截文件被当成"已存在"。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(dest), suffix=".partial"
    )
    os.close(tmp_fd)
    try:
        try:
            _fetch_urllib(url, tmp_path, timeout)
        except (ssl.SSLError, urllib.error.URLError) as exc:
            # URLError 把 SSL 错误包在 reason 里，两种形态都要认
            reason = getattr(exc, "reason", exc)
            if not isinstance(reason, ssl.SSLError) and not isinstance(exc, ssl.SSLError):
                raise
            print(f"urllib 证书校验失败（{reason}），改用 curl 走系统信任库重试")
            _fetch_curl(url, tmp_path, timeout)
        size = os.path.getsize(tmp_path)
        if size < MIN_BYTES:
            raise RuntimeError(
                f"下载内容只有 {size} 字节，不像是模型文件（可能被代理拦成了错误页）"
            )
        os.replace(tmp_path, dest)
        return size
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def verify(path: str) -> str:
    """真的加载一次模型并识别一帧，返回一行人类可读的确认信息。

    只有加载成功才说明这份文件与当前 mediapipe 版本兼容——大小校验拦不住
    "版本对不上"这类问题。这里复用 gesture_observer 里那个已经处理过
    macOS Metal 坑的适配器，顺带验证适配器本身在本机可用。
    """
    import numpy as np
    import mediapipe as mp

    sys.path.insert(0, project_dir())
    from core.camera_stream.gesture_observer import MediaPipeGestureRecognizer

    recognizer = MediaPipeGestureRecognizer(path)
    try:
        # 一张纯色图：确认整条图计算能跑通，无手时返回空列表
        blank = np.zeros((360, 640, 3), dtype=np.uint8)
        found = recognizer.classify(blank)
    finally:
        recognizer.close()
    return (
        f"mediapipe {mp.__version__} 加载成功"
        f"（delegate={'GPU' if recognizer.uses_gpu else 'CPU'}），"
        f"空白帧识别到 {len(found)} 个手势"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="下载 MediaPipe 手势识别模型")
    parser.add_argument("--dest", default=default_model_path(), help="目标文件路径")
    parser.add_argument("--url", default=MODEL_URL, help="模型下载地址")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    parser.add_argument(
        "--verify", action="store_true", help="下载后用 mediapipe 真实加载一次"
    )
    args = parser.parse_args(argv)

    dest = os.path.abspath(args.dest)

    if is_present(dest) and not args.force:
        print(f"模型已存在，跳过下载: {dest} ({os.path.getsize(dest)} 字节)")
    else:
        print(f"下载 {args.url}\n  -> {dest}")
        try:
            size = download(args.url, dest)
        except Exception as exc:
            print(f"下载失败: {exc}", file=sys.stderr)
            return 1
        print(f"下载完成: {size} 字节")

    if args.verify:
        try:
            print(verify(dest))
        except Exception as exc:
            print(f"模型加载校验失败: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
