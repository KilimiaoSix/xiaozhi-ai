"""竖大拇指审批的帧观察者。

机器人本体没有摄像头也没有手势传感器（AGENTS.md「硬件边界」），画面来自桌面端
经 WS 推上来的 JPEG 流。本模块挂在 frame_observer_hub 上，只在有待审批请求时
才看帧，识别到连续数帧的 Thumb_Up 就把请求批准掉。

三条约束来自 observers.py 的契约：

1. on_frame 跑在推理工作线程（asyncio.to_thread 的线程池），必须自己节流。
   MediaPipe 一次推理几十毫秒，5fps 全跑会跟人脸识别抢 CPU，所以默认
   min_interval_s=0.35（约 3fps）。
2. 自己解码 JPEG——识别链路的解码帧不外传，保持"原始帧不出推理进程"的边界。
3. 决策要回到事件循环才能 await：构造时注入 loop，用
   asyncio.run_coroutine_threadsafe 投递，不等结果（等结果会把推理线程卡住）。

**连续 required_hits 帧**而不是单帧就批准，是因为默认不批准是硬性要求：
单帧误识别的代价是放行一条没人同意的命令，而多等 0.7 秒的代价只是慢一点。
中间只要断一帧（换了别的手势、手移出画面）计数就清零。

模型文件缺失时构造不抛异常，只置 disabled 并告警：审批链路照样能走
超时默认拒绝与 HTTP 人工决策，不该因为少一个模型文件就让服务起不来。
"""

import asyncio
import inspect
import logging
import os
import sys
import threading
from typing import Any, Callable, List, Optional, Sequence, Tuple

TAG = __name__

# MediaPipe 内置手势分类器的类别名（canned gestures）。实测 category.index 恒为 -1，
# 只有 category_name 可用，别按下标取。
GESTURE_THUMB_UP = "Thumb_Up"
GESTURE_THUMB_DOWN = "Thumb_Down"

DECISION_APPROVED = "approved"
DECISION_DENIED = "denied"
SOURCE_GESTURE = "gesture"

DEFAULT_MODEL_RELATIVE_PATH = os.path.join("data", "models", "gesture_recognizer.task")
DEFAULT_MIN_INTERVAL_S = 0.35
DEFAULT_REQUIRED_HITS = 3
DEFAULT_MIN_SCORE = 0.5


def _project_dir() -> str:
    package_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(package_dir))


def default_model_path() -> str:
    """默认模型路径：<仓库根>/data/models/gesture_recognizer.task。"""
    return os.path.join(_project_dir(), DEFAULT_MODEL_RELATIVE_PATH)


def resolve_model_path(model_path: Optional[str]) -> str:
    """把配置里的模型路径解析成绝对路径。

    config.yaml 里写的通常是 data/models/... 这样的相对路径，而服务可以从任意
    目录启动（systemd / launchd 的 cwd 常常是 /）。相对路径一律按仓库根解析，
    不按当前工作目录——否则换个启动方式就静默降级成"模型不存在"。
    """
    if not model_path:
        return default_model_path()
    path = str(model_path)
    if os.path.isabs(path):
        return path
    return os.path.join(_project_dir(), path)


class MediaPipeGestureRecognizer:
    """MediaPipe GestureRecognizer 的适配器，IMAGE 模式。

    对外只暴露 classify(frame_bgr) -> [(类别名, 置信度), ...]，把 mp.Image 的构造
    与平台差异关在里面：观察者本身不 import mediapipe，单测注入假实现即可。

    **macOS 的坑**（与 presence-agent 的 PoseDetector 同源）：本机 mediapipe 轮子
    把 TensorsToDetections 编译成了 Metal 路径，用默认 CPU delegate 建图会直接
    abort（graph_service.h:139 Check failed: service_，进程级 abort 而不是异常，
    try/except 拦不住）；改 GPU delegate 后输入必须是 SRGBA，喂 SRGB 会在推理时
    报 unsupported ImageFrame format。Linux/Windows 保持 CPU delegate + SRGB。
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        *,
        num_hands: int = 1,
        platform: str = sys.platform,
        mediapipe_module=None,
        cv2_module=None,
    ) -> None:
        # 重依赖只在真要跑识别时才加载：审批链路不开启时不该拖慢服务启动
        from importlib import import_module

        mp = mediapipe_module or import_module("mediapipe")
        self._mp = mp
        self._cv2 = cv2_module or import_module("cv2")
        self._uses_gpu = platform == "darwin"

        base_options = mp.tasks.BaseOptions(
            model_asset_path=str(model_path or default_model_path()),
            **(
                {"delegate": mp.tasks.BaseOptions.Delegate.GPU}
                if self._uses_gpu
                else {}
            ),
        )
        vision = mp.tasks.vision
        options = vision.GestureRecognizerOptions(
            base_options=base_options,
            running_mode=vision.RunningMode.IMAGE,
            num_hands=num_hands,
        )
        self._recognizer = vision.GestureRecognizer.create_from_options(options)
        # 同一实例的 recognize 不保证并发安全，而推理线程池可能同时递两帧进来
        self._lock = threading.Lock()

    @property
    def uses_gpu(self) -> bool:
        return self._uses_gpu

    def classify(self, frame_bgr) -> List[Tuple[str, float]]:
        """识别一帧 BGR 图，返回 [(类别名, 置信度), ...]，没手就是空列表。"""
        cv2 = self._cv2
        mp = self._mp
        if self._uses_gpu:
            image = mp.Image(
                image_format=mp.ImageFormat.SRGBA,
                data=cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGBA),
            )
        else:
            image = mp.Image(
                image_format=mp.ImageFormat.SRGB,
                data=cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB),
            )
        with self._lock:
            result = self._recognizer.recognize(image)

        found: List[Tuple[str, float]] = []
        for hand in getattr(result, "gestures", None) or []:
            for category in hand or []:
                name = getattr(category, "category_name", None)
                if name:
                    found.append((str(name), float(getattr(category, "score", 0.0))))
        return found

    def close(self) -> None:
        try:
            self._recognizer.close()
        except Exception:
            pass


def _default_decoder(jpeg: bytes):
    """JPEG bytes -> BGR ndarray。解码失败返回 None（半截帧不该炸掉观察者）。"""
    from importlib import import_module

    cv2 = import_module("cv2")
    np = import_module("numpy")

    buffer = np.frombuffer(jpeg, dtype=np.uint8)
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


class GestureObserver:
    """实现 FrameObserver：待审批期间盯着画面找大拇指。

    approval_manager 与 has_pending/resolve 回调二选一注入；生产传 manager，
    单测传同步回调就不必起事件循环。
    """

    def __init__(
        self,
        approval_manager=None,
        *,
        has_pending: Optional[Callable[[], bool]] = None,
        resolve: Optional[Callable[[str, str], Any]] = None,
        recognizer_factory: Optional[Callable[[], Any]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        model_path: Optional[str] = None,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        required_hits: int = DEFAULT_REQUIRED_HITS,
        min_score: float = DEFAULT_MIN_SCORE,
        decoder: Optional[Callable[[bytes], Any]] = None,
        logger=None,
    ) -> None:
        if approval_manager is None and (has_pending is None or resolve is None):
            raise ValueError(
                "需要 approval_manager，或同时提供 has_pending 与 resolve 回调"
            )

        self._has_pending = has_pending or approval_manager.has_pending
        self._resolve = resolve or approval_manager.resolve_pending
        self._loop = loop
        self._logger = logger or logging.getLogger(__name__)
        self._min_interval_s = max(0.0, float(min_interval_s))
        self._required_hits = max(1, int(required_hits))
        self._min_score = float(min_score)
        self._decode = decoder or _default_decoder

        self._model_path = resolve_model_path(model_path)
        self._recognizer_factory = recognizer_factory
        self._recognizer = None
        self._disabled = False
        self._last_frame_at: Optional[float] = None
        self._streak_gesture: Optional[str] = None
        self._streak_count = 0

        if recognizer_factory is None and not os.path.exists(self._model_path):
            # 只告警不抛：没有模型时审批仍能靠超时默认拒绝与 HTTP 人工决策走完
            self._disabled = True
            self._logger.warning(
                f"手势模型不存在，手势审批已降级为「仅超时/人工决策」: "
                f"{self._model_path}（跑 scripts/download_gesture_model.py 下载）"
            )

    # ------------------------------------------------------------ 契约

    @property
    def disabled(self) -> bool:
        return self._disabled

    def wants_frame(self, workstation_id: str, now: float) -> bool:
        """只有真有 pending 且自己没被降级时才要帧。

        降级时返回 False 而不是 True：拿到帧也识别不了，白白解码每一帧。
        """
        if self._disabled:
            return False
        try:
            return bool(self._has_pending())
        except Exception:
            self._logger.exception("查询待审批状态失败，本帧跳过")
            return False

    def on_frame(self, workstation_id: str, jpeg: bytes, now: float) -> None:
        """处理一帧。跑在推理工作线程，必须快进快出。"""
        if self._disabled:
            return
        if not self._should_process(now):
            return

        recognizer = self._ensure_recognizer()
        if recognizer is None:
            return

        frame = self._decode(jpeg)
        if frame is None:
            self._logger.warning("手势审批：JPEG 解码失败，本帧跳过")
            return

        try:
            found = recognizer.classify(frame)
        except Exception:
            self._logger.exception("手势识别失败，本帧跳过")
            return

        self._consume(self._pick_gesture(found))

    # ------------------------------------------------------------ 内部

    def _should_process(self, now: float) -> bool:
        """按 min_interval_s 节流。now 是 handler 传下来的单调时钟。"""
        last = self._last_frame_at
        if last is not None and (now - last) < self._min_interval_s:
            return False
        self._last_frame_at = now
        return True

    def _ensure_recognizer(self):
        """懒加载识别器。建图要一秒量级，不该在服务启动时就付这个代价。"""
        if self._recognizer is not None:
            return self._recognizer
        factory = self._recognizer_factory or (
            lambda: MediaPipeGestureRecognizer(self._model_path)
        )
        try:
            self._recognizer = factory()
        except Exception:
            # 建图失败就永久降级，否则每帧都重试一次，反复吃一秒
            self._disabled = True
            self._logger.exception("手势识别器初始化失败，手势审批已降级")
            return None
        return self._recognizer

    def _pick_gesture(self, found: Sequence[Tuple[str, float]]) -> Optional[str]:
        """挑出这一帧的有效手势名，够不上 min_score 的一律当没看见。"""
        best_name: Optional[str] = None
        best_score = self._min_score
        for name, score in found or []:
            if score >= best_score:
                best_name, best_score = name, score
        if best_name in (GESTURE_THUMB_UP, GESTURE_THUMB_DOWN):
            return best_name
        return None

    def _consume(self, gesture: Optional[str]) -> None:
        """累计连续帧。换手势或没手势都从头数起。"""
        if gesture is None:
            self._reset_streak()
            return
        if gesture != self._streak_gesture:
            self._streak_gesture = gesture
            self._streak_count = 1
        else:
            self._streak_count += 1

        if self._streak_count < self._required_hits:
            return

        decision = (
            DECISION_APPROVED if gesture == GESTURE_THUMB_UP else DECISION_DENIED
        )
        # 先清零再投递：决策落地是异步的，不清零的话紧接着的几帧会再投一次，
        # 把下一条审批请求也一起解决掉
        self._reset_streak()
        self._logger.info(f"手势 {gesture} 已连续确认，提交审批决策: {decision}")
        self._submit(decision)

    def _reset_streak(self) -> None:
        self._streak_gesture = None
        self._streak_count = 0

    def _submit(self, decision: str) -> None:
        """把决策交回事件循环。工作线程不能 await，也不能等结果。"""
        if self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._resolve_async(decision), self._loop
                )
            except Exception:
                self._logger.exception("提交审批决策失败")
            return

        # 没注入 loop：只能是同步 resolve（单测形态）
        try:
            outcome = self._resolve(decision, SOURCE_GESTURE)
        except Exception:
            self._logger.exception("提交审批决策失败")
            return
        if inspect.isawaitable(outcome):
            # 关掉它，否则留下一个 "coroutine was never awaited" 的警告，
            # 而且决策实际上没落地——这是装配漏了 loop，必须吵出来
            outcome.close()
            self._logger.error(
                "resolve 是协程但没有注入 loop，审批决策未落地"
            )

    async def _resolve_async(self, decision: str):
        outcome = self._resolve(decision, SOURCE_GESTURE)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return outcome

    def close(self) -> None:
        recognizer = self._recognizer
        self._recognizer = None
        if recognizer is not None and hasattr(recognizer, "close"):
            try:
                recognizer.close()
            except Exception:
                pass


def create_gesture_observer(
    config: Optional[dict],
    approval_manager,
    *,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    logger=None,
    **kwargs,
) -> Optional[GestureObserver]:
    """按 config 的 approval.gesture 段装配观察者；未启用返回 None。"""
    section = ((config or {}).get("approval") or {}).get("gesture") or {}
    if not isinstance(section, dict):
        section = {}
    if not section.get("enabled", True):
        return None

    def _number(key: str, fallback: float) -> float:
        try:
            value = float(section.get(key))
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 else fallback

    return GestureObserver(
        approval_manager,
        loop=loop,
        logger=logger,
        model_path=(str(section.get("model_path")) if section.get("model_path") else None),
        min_interval_s=_number("min_interval_s", DEFAULT_MIN_INTERVAL_S),
        required_hits=int(_number("required_hits", DEFAULT_REQUIRED_HITS)),
        min_score=_number("min_score", DEFAULT_MIN_SCORE),
        **kwargs,
    )
