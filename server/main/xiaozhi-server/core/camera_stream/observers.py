"""摄像头帧观察者集线器。

摄像头流的主职责是同帧在岗/人脸识别（inference.py），但手势审批与分心检测
也需要看同一路画面。为了不动 CameraInferenceRuntime 的纯函数契约，这里提供
一个旁路：CameraStreamHandler 处理完每帧识别后，把原始 JPEG 再喂给当前
「感兴趣」的观察者。

设计约束：
- 观察者只在自己的活跃窗口内收帧（审批等待中 / 番茄钟专注相位），平时
  wants_frame() 返回 False，一次字典遍历就过去了，不产生任何解码开销。
- on_frame 在推理线程（asyncio.to_thread 的工作线程）里同步执行，观察者
  自己负责限频与快速返回；重活（如 MediaPipe 推理）应自行按 min_interval
  节流，不要每帧都跑。
- 观察者按需自行解码 JPEG——识别链路的解码帧不外传，保持「原始帧不出
  推理进程」的隐私边界不变。
- 观察者异常只记日志，不得影响识别主链路。
"""

import logging
import threading
from typing import Protocol


class FrameObserver(Protocol):
    """帧观察者契约。两个方法都必须是快速、无阻塞网络 I/O 的。"""

    def wants_frame(self, workstation_id: str, now: float) -> bool:
        """当前是否需要看帧。False 时集线器直接跳过，零开销。"""
        ...

    def on_frame(self, workstation_id: str, jpeg: bytes, now: float) -> None:
        """处理一帧。在推理工作线程同步执行，自行节流与解码。"""
        ...


class FrameObserverHub:
    """线程安全的观察者注册表。

    register/unregister 可能来自事件循环线程（审批窗口开启/关闭），
    offer 来自推理工作线程，故用锁保护注册表快照。
    """

    def __init__(self, logger=None) -> None:
        self._observers: dict[str, FrameObserver] = {}
        self._lock = threading.Lock()
        self._logger = logger or logging.getLogger(__name__)

    def register(self, name: str, observer: FrameObserver) -> None:
        with self._lock:
            self._observers[name] = observer

    def unregister(self, name: str) -> None:
        with self._lock:
            self._observers.pop(name, None)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._observers)

    def offer(self, workstation_id: str, jpeg: bytes, now: float) -> None:
        """把一帧提供给所有感兴趣的观察者。观察者异常不外抛。"""
        with self._lock:
            observers = list(self._observers.items())
        for name, observer in observers:
            try:
                if observer.wants_frame(workstation_id, now):
                    observer.on_frame(workstation_id, jpeg, now)
            except Exception:
                self._logger.exception(f"帧观察者 {name} 处理失败，已忽略")


# 进程级单例：camera_stream_handler 在推理线程调用 offer，
# approval/distraction 模块在装配时 register。
frame_observer_hub = FrameObserverHub()
