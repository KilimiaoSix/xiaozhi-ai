"""分心检测（演示流程六）：番茄钟专注相位里，摄像头画面持续出现手机就温和提醒一次。

硬约束（来自需求文档流程六，全部体现为下面的实现细节，不是可选项）：
- 只在用户主动开启专注模式时检测：wants_frame 完全由注入的 active_lookup 网关，
  active_lookup 返回 False 时零开销跳过，不做默认监控。
- 短暂查看不提醒：手机要连续/累计出现到 trigger_seconds 才算，检测间隔内的
  短暂消失（< release_seconds）不清零累计，只有消失够久才清零重来。
- 首次只做一次温和提醒：同一次激活期内 fired 后不再触发；active_lookup 从
  False 变回 True 才算新激活期，重新武装。
- 本地检测、不落盘：只在内存里解码 JPEG 做一次推理，日志只记布尔与时长，
  从不写入任何帧内容。

时间源：本模块所有状态机判断都用注入的 clock()，不用 FrameObserver 协议传入的
now 参数——这样测试可以直接操纵 clock 做确定性时间推进，不必真实 sleep，也不
用费心让传给 on_frame 的 now 参数和状态机时间轴保持一致。

现场验证过的 mediapipe==1.0.1（macOS, arm64, opencv==5.0.0）事实，直接决定了
下面 create_default_detector 的写法，不是随手选的实现细节：

1. BaseOptions.Delegate.CPU 在 ObjectDetector.create_from_options() 阶段就会
   因为 TensorsToDetectionsCalculator::Open() 里 DrishtiMetalHelper 拿不到 GPU
   共享服务而 ABSL_CHECK 失败，把整个解释器进程直接 SIGABRT 掉——这是 C++
   层的硬中止，Python 的 try/except 完全拦不住。生产 detector 必须用
   Delegate.GPU，实测创建成功且能正常推理。
2. GPU 路径下如果给 ObjectDetectorOptions 传 category_allowlist，会在真正跑
   detect() 时触发另一个 ABSL_CHECK（GPU 侧的 class-index 过滤只认「全部
   类别」或从 0/1 开始的连续区间，单独一个 "cell phone" 类不满足这个条件，
   同样是直接 SIGABRT）。所以 create_default_detector 不设 allowlist，类别
   与分数过滤全部挪到本模块的 Python 侧做（见 _extract_target_score）。
3. GPU 路径的图像转换只认 4 通道 SRGBA；cv2.imdecode 解出来的是 3 通道 BGR，
   必须先 cvtColor 成 RGBA 再包 mp.Image，否则 detect() 阶段会因为 GpuBuffer
   转换不认 3 通道格式而崩（同样是 ABSL_CHECK，不是 Python 异常）。
"""

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional


TAG = __name__

# 相对仓库根（xiaozhi-server 进程 cwd）的默认模型路径，与
# scripts/download_detection_model.py 的下载落地路径保持一致。
DEFAULT_MODEL_PATH = "data/models/efficientdet_lite0.tflite"

# COCO 类目里手机对应的类别名（mediapipe ObjectDetector 返回的 category_name），
# 现场用真实照片验证过取值正是这个字符串（见开发时的探测脚本，非猜测）。
TARGET_CATEGORY = "cell phone"

DEFAULT_MIN_INTERVAL_S = 0.5
DEFAULT_TRIGGER_SECONDS = 8.0
DEFAULT_RELEASE_SECONDS = 3.0
DEFAULT_MIN_SCORE = 0.4


def create_default_detector(model_path: str = DEFAULT_MODEL_PATH) -> Any:
    """生产用 mediapipe ObjectDetector 工厂。

    Delegate 必须是 GPU、不能设 category_allowlist——这两点是本机现场验证过的
    约束，见模块 docstring 第 1、2 条，不是可以随手改动的实现细节。
    模型缺失或加载失败时直接抛异常，由调用方（DistractionObserver.__init__）
    捕获并降级为 disabled，这里不吞异常。
    """
    import mediapipe as mp

    vision = mp.tasks.vision
    base_options = mp.tasks.BaseOptions(
        model_asset_path=model_path,
        delegate=mp.tasks.BaseOptions.Delegate.GPU,
    )
    options = vision.ObjectDetectorOptions(
        base_options=base_options,
        running_mode=vision.RunningMode.IMAGE,
        max_results=5,
        # 低阈值只用来削掉明显噪声、减轻后处理量；真正的判定阈值是
        # DistractionObserver 自己的 min_score（见约束 2：GPU 路径不能用
        # category_allowlist，类别+分数过滤都统一挪到 Python 侧一起做，
        # 这里的阈值就不能设得比 min_score 还高，否则会在这一层提前把
        # 用户想要的检测结果过滤掉）。
        score_threshold=0.1,
    )
    return vision.ObjectDetector.create_from_options(options)


def _decode_bgr(jpeg: bytes) -> Optional[Any]:
    """把 JPEG 字节解码成 BGR ndarray；解不出来返回 None（跳过本帧，不报错）。"""
    import cv2
    import numpy as np

    array = np.frombuffer(jpeg, dtype=np.uint8)
    if array.size == 0:
        return None
    return cv2.imdecode(array, cv2.IMREAD_COLOR)


def _bgr_to_mp_image(frame_bgr: Any) -> Any:
    """BGR -> RGBA -> mp.Image。GPU delegate 只认 4 通道，见模块 docstring 第 3 条。"""
    import cv2
    import mediapipe as mp

    frame_rgba = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGBA)
    return mp.Image(image_format=mp.ImageFormat.SRGBA, data=frame_rgba)


@dataclass
class _WorkstationState:
    """单个工位的分心检测状态，工位之间完全独立。"""

    # 上一次检查时 active_lookup 是否为真。False -> True 的跳变才算「新激活期」，
    # 借此识别番茄钟专注相位重新开始（或从暂停恢复），从而重新武装。
    was_active: bool = False
    # 本次激活期内是否已经提醒过；同一激活期内只提醒一次。
    fired: bool = False
    # 手机在画面中的累计时长（秒），按实际跑检测的间隔累加，不是墙钟流逝时间。
    accumulated_s: float = 0.0
    # 最近一次检测到手机在画面里的时刻（clock 轴）；用来判断消失了多久。
    last_seen_present_at: Optional[float] = None
    # 最近一次真正跑检测的时刻（clock 轴）；既是节流锚点，也是算累加 dt 的基准。
    last_tick_at: Optional[float] = None


class DistractionObserver:
    """FrameObserver 实现：番茄钟专注相位里持续看到手机就温和提醒一次。

    构造注入 active_lookup / on_distraction / detector 工厂 / clock，全部可替换，
    便于离线单测（不联网、不建真模型、不用真实 sleep）。
    """

    def __init__(
        self,
        *,
        active_lookup: Callable[[str], bool],
        on_distraction: Callable[[str], None],
        detector_factory: Callable[[], Any] = create_default_detector,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        trigger_seconds: float = DEFAULT_TRIGGER_SECONDS,
        release_seconds: float = DEFAULT_RELEASE_SECONDS,
        min_score: float = DEFAULT_MIN_SCORE,
        clock: Optional[Callable[[], float]] = None,
        logger=None,
        debug: bool = False,
    ) -> None:
        self._active_lookup = active_lookup
        self._on_distraction = on_distraction
        self._min_interval_s = max(0.0, float(min_interval_s))
        self._trigger_seconds = max(0.0, float(trigger_seconds))
        self._release_seconds = max(0.0, float(release_seconds))
        self._min_score = float(min_score)
        self._clock = clock or time.monotonic
        self._logger = logger or logging.getLogger(__name__)
        # 诊断开关：真机上「手机举了半天没反应」有两种成因——检测器压根没看到，
        # 或者看到了但分数够不上 min_score。日志里分不开就只能靠猜。默认关闭，
        # 因为开着是 2 帧/秒一条，会把 server.log 淹掉。
        self._debug = bool(debug)

        self._states: dict[str, _WorkstationState] = {}
        self._locks: dict[str, threading.Lock] = {}
        # 保护 _locks 本身的创建（不同工位的锁对象只能被创建一次，否则两个线程
        # 各拿到一把不同的锁，起不到互斥效果）；不用来保护 _states 的读写，
        # CPython 里单条 dict 语句的原子性够用，真正的状态互斥靠每工位的锁。
        self._registry_lock = threading.Lock()

        # 检测器模型缺失/加载失败：构造不炸，降级为 disabled 并记一条 warning。
        # 之后 wants_frame 恒为 False，观察者变成零开销的空操作。
        self._detector: Optional[Any] = None
        self._disabled = False
        try:
            self._detector = detector_factory()
        except Exception:
            self._disabled = True
            self._logger.warning(
                "分心检测模型加载失败，本次运行禁用分心检测（不影响其它功能）",
                exc_info=True,
            )

    # ------------------------------------------------------------ FrameObserver

    def wants_frame(self, workstation_id: str, now: float) -> bool:
        if self._disabled:
            return False

        active = bool(self._active_lookup(workstation_id))
        lock = self._lock_for(workstation_id)
        with lock:
            state = self._state_for(workstation_id)
            if active and not state.was_active:
                # False -> True 跳变：专注相位刚开始（或从暂停/上次结束后重开），
                # 视为新激活期，重新武装。
                state.fired = False
                state.accumulated_s = 0.0
                state.last_seen_present_at = None
                state.last_tick_at = None
            state.was_active = active
        return active

    def on_frame(self, workstation_id: str, jpeg: bytes, now: float) -> None:
        if self._disabled:
            return

        lock = self._lock_for(workstation_id)
        with lock:
            state = self._state_for(workstation_id)
            current = self._clock()

            # 节流：检测间隔内不重复跑检测，直接返回——不解码、不推理，零额外开销。
            if (
                state.last_tick_at is not None
                and (current - state.last_tick_at) < self._min_interval_s
            ):
                return

            dt = (
                current - state.last_tick_at
                if state.last_tick_at is not None
                else 0.0
            )
            state.last_tick_at = current

            phone_present = self._run_detection(jpeg)
            self._advance_state(workstation_id, state, phone_present, dt, current)

    # ------------------------------------------------------------ 检测 + 状态机

    def _run_detection(self, jpeg: bytes) -> bool:
        """解码 + 推理 + 按类别/分数过滤，返回这一帧里是不是看到了手机。

        解码失败、推理异常都按「没看到手机」处理并记日志，不向上抛——
        观察者异常不该影响识别主链路（同 FrameObserverHub.offer 的兜底约定）。
        """
        frame_bgr = _decode_bgr(jpeg)
        if frame_bgr is None:
            self._logger.warning("分心检测：JPEG 解码失败，跳过本帧")
            return False

        try:
            mp_image = _bgr_to_mp_image(frame_bgr)
            result = self._detector.detect(mp_image)
        except Exception:
            self._logger.warning("分心检测：推理失败，跳过本帧", exc_info=True)
            return False

        best_target = 0.0
        seen: list[tuple[str, float]] = []
        for detection in getattr(result, "detections", []):
            for category in getattr(detection, "categories", []):
                name = getattr(category, "category_name", None)
                score = float(getattr(category, "score", 0.0) or 0.0)
                seen.append((str(name), score))
                if name == TARGET_CATEGORY and score > best_target:
                    best_target = score

        hit = best_target >= self._min_score
        if self._debug:
            self._log_detection(hit, best_target, seen)
        return hit

    def _log_detection(
        self, hit: bool, best_target: float, seen: list
    ) -> None:
        """debug 开关下的每帧诊断。只记类别名与分数，从不记录任何帧内容。"""
        if best_target > 0.0:
            self._logger.info(
                f"分心检测[debug]：看到手机 score={best_target:.2f}，"
                f"阈值 {self._min_score:.2f}，"
                f"{'计入累计' if hit else '未达阈值，本帧不计入'}"
            )
            return
        top = ", ".join(
            f"{name}={score:.2f}"
            for name, score in sorted(seen, key=lambda item: -item[1])[:3]
        )
        self._logger.info(
            f"分心检测[debug]：本帧没看到手机，画面里最强的目标：{top or '无'}"
        )

    def _advance_state(
        self,
        workstation_id: str,
        state: _WorkstationState,
        phone_present: bool,
        dt: float,
        current: float,
    ) -> None:
        if phone_present:
            state.accumulated_s += dt
            state.last_seen_present_at = current
            if self._debug:
                self._logger.info(
                    f"分心检测[debug]：工位 {workstation_id} 累计 "
                    f"{state.accumulated_s:.1f}s / 阈值 {self._trigger_seconds:.1f}s"
                    f"（本次 +{dt:.1f}s，已提醒过={state.fired}）"
                )
            if not state.fired and state.accumulated_s >= self._trigger_seconds:
                state.fired = True
                self._logger.info(
                    f"工位 {workstation_id} 手机分心累计 "
                    f"{state.accumulated_s:.1f}s 达到阈值 {self._trigger_seconds:.1f}s，"
                    "已触发一次温和提醒"
                )
                self._on_distraction(workstation_id)
            return

        # 没看到手机：消失够久（>= release_seconds）才清零累计，短暂消失不清零。
        if (
            state.last_seen_present_at is not None
            and (current - state.last_seen_present_at) >= self._release_seconds
        ):
            state.accumulated_s = 0.0
            state.last_seen_present_at = None

    # ------------------------------------------------------------ 内部注册表

    def _lock_for(self, workstation_id: str) -> threading.Lock:
        with self._registry_lock:
            lock = self._locks.get(workstation_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[workstation_id] = lock
            return lock

    def _state_for(self, workstation_id: str) -> _WorkstationState:
        """调用方必须已持有该工位的锁。"""
        state = self._states.get(workstation_id)
        if state is None:
            state = _WorkstationState()
            self._states[workstation_id] = state
        return state


def build_reminder_text(remaining_s: Optional[int]) -> str:
    """分心提醒的播报文案。

    remaining_s 为 None（拿不到番茄钟剩余时间时的兜底）就不报具体分钟数；
    有值就报分钟数（向上取整，25:30 说"26分钟"而不是"25分钟"，宁可少估计
    剩余时间也不要让用户觉得"怎么还没到就说完成了"）；不足 1 分钟时用
    "最后一分钟"而不是机械地说"还剩1分钟"（虽然两者数值上算出来是同一个数，
    但"最后一分钟"更贴近用户实际感受到的"马上就好了"）。
    """
    if remaining_s is None:
        return "专注时间还没结束，我们坚持一下？"

    if remaining_s < 60:
        remaining_desc = "最后一分钟了"
    else:
        minutes = math.ceil(remaining_s / 60)
        remaining_desc = f"还剩{minutes}分钟"

    return f"{remaining_desc}，这个番茄钟就完成了。我们坚持一下？"
