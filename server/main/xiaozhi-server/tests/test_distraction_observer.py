"""DistractionObserver 单测：全部注入 fake detector，不建真模型、不连网、不真实 sleep。

时间轴由注入的 ManualClock 手动推进；JPEG 解码用的是真实 cv2（很快、无外部依赖），
只有 mediapipe 推理这一层被替身替换，这样既测到了真实的解码路径，又不必等模型加载。
"""

import cv2
import numpy as np
import pytest

from core.camera_stream.distraction_observer import (
    DistractionObserver,
    build_reminder_text,
)


# 4x4 全黑图编码出来的真实 JPEG 字节，所有用例共用；内容无所谓，
# 测的是分心检测的状态机，不是解码结果本身。
_ok, _encoded = cv2.imencode(".jpg", np.zeros((4, 4, 3), dtype=np.uint8))
assert _ok
FAKE_JPEG = _encoded.tobytes()

WORKSTATION = "desk-1"


class ManualClock:
    """测试专用的可手动推进的单调时钟，替代注入的 clock 参数。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, dt: float) -> None:
        self._now += dt


class _FakeCategory:
    def __init__(self, category_name: str, score: float) -> None:
        self.category_name = category_name
        self.score = score


class _FakeDetection:
    def __init__(self, categories) -> None:
        self.categories = categories


class _FakeResult:
    def __init__(self, detections) -> None:
        self.detections = detections


class FakeDetector:
    """行为可控的假 ObjectDetector：present 开关决定 detect() 是否"看到手机"。"""

    def __init__(self) -> None:
        self.present = False
        self.score = 0.9
        self.calls = 0

    def detect(self, mp_image):
        self.calls += 1
        if self.present:
            return _FakeResult(
                [_FakeDetection([_FakeCategory("cell phone", self.score)])]
            )
        return _FakeResult([])


class ActiveLookup:
    """测试专用的 workstation_id -> bool 查表，模拟番茄钟专注相位是否进行中。"""

    def __init__(self) -> None:
        self._active: dict[str, bool] = {}

    def set(self, workstation_id: str, active: bool) -> None:
        self._active[workstation_id] = active

    def __call__(self, workstation_id: str) -> bool:
        return self._active.get(workstation_id, False)


def _make_observer(
    *,
    detector: FakeDetector,
    active_lookup: ActiveLookup,
    on_distraction,
    clock: ManualClock,
    min_interval_s: float = 1.0,
    trigger_seconds: float = 5.0,
    release_seconds: float = 3.0,
    min_score: float = 0.4,
) -> DistractionObserver:
    return DistractionObserver(
        active_lookup=active_lookup,
        on_distraction=on_distraction,
        detector_factory=lambda: detector,
        min_interval_s=min_interval_s,
        trigger_seconds=trigger_seconds,
        release_seconds=release_seconds,
        min_score=min_score,
        clock=clock,
    )


# ------------------------------------------------------------ wants_frame 网关


def test_inactive_workstation_does_not_want_frame():
    """未开启专注模式（active_lookup 为 False）：wants_frame 恒 False，不做默认监控。"""
    active_lookup = ActiveLookup()
    active_lookup.set(WORKSTATION, False)
    clock = ManualClock()
    observer = _make_observer(
        detector=FakeDetector(),
        active_lookup=active_lookup,
        on_distraction=lambda ws: None,
        clock=clock,
    )
    assert observer.wants_frame(WORKSTATION, clock()) is False


def test_active_workstation_wants_frame():
    active_lookup = ActiveLookup()
    active_lookup.set(WORKSTATION, True)
    clock = ManualClock()
    observer = _make_observer(
        detector=FakeDetector(),
        active_lookup=active_lookup,
        on_distraction=lambda ws: None,
        clock=clock,
    )
    assert observer.wants_frame(WORKSTATION, clock()) is True


# ------------------------------------------------------------ 触发/不触发核心状态机


def test_brief_appearance_does_not_trigger():
    """手机只是短暂出现（累计远小于 trigger_seconds）就消失：不该提醒。"""
    active_lookup = ActiveLookup()
    active_lookup.set(WORKSTATION, True)
    detector = FakeDetector()
    fired = []
    clock = ManualClock()
    observer = _make_observer(
        detector=detector,
        active_lookup=active_lookup,
        on_distraction=fired.append,
        clock=clock,
        min_interval_s=1.0,
        trigger_seconds=5.0,
        release_seconds=3.0,
    )

    observer.wants_frame(WORKSTATION, clock())
    detector.present = True
    observer.on_frame(WORKSTATION, FAKE_JPEG, clock())  # 第一次跑检测，dt=0，累计仍是 0
    clock.advance(1.0)
    observer.on_frame(WORKSTATION, FAKE_JPEG, clock())  # 累计 1s，远小于 5s

    detector.present = False
    clock.advance(1.0)
    observer.on_frame(WORKSTATION, FAKE_JPEG, clock())

    assert fired == []


def test_sustained_presence_triggers_once():
    """手机持续在画面里，累计时长达到 trigger_seconds 就触发一次提醒。"""
    active_lookup = ActiveLookup()
    active_lookup.set(WORKSTATION, True)
    detector = FakeDetector()
    detector.present = True
    fired = []
    clock = ManualClock()
    observer = _make_observer(
        detector=detector,
        active_lookup=active_lookup,
        on_distraction=fired.append,
        clock=clock,
        min_interval_s=1.0,
        trigger_seconds=5.0,
        release_seconds=3.0,
    )

    observer.wants_frame(WORKSTATION, clock())
    # 第一次跑检测 dt=0 不计累计；之后每隔 min_interval_s 跑一次，每次累计 +1s。
    for _ in range(6):
        observer.on_frame(WORKSTATION, FAKE_JPEG, clock())
        clock.advance(1.0)

    assert fired == [WORKSTATION]


def test_fired_does_not_retrigger_within_same_activation():
    """fired 之后同一激活期内手机继续出现，不应该再提醒第二次。"""
    active_lookup = ActiveLookup()
    active_lookup.set(WORKSTATION, True)
    detector = FakeDetector()
    detector.present = True
    fired = []
    clock = ManualClock()
    observer = _make_observer(
        detector=detector,
        active_lookup=active_lookup,
        on_distraction=fired.append,
        clock=clock,
        min_interval_s=1.0,
        trigger_seconds=5.0,
        release_seconds=3.0,
    )

    observer.wants_frame(WORKSTATION, clock())
    for _ in range(10):
        observer.on_frame(WORKSTATION, FAKE_JPEG, clock())
        clock.advance(1.0)

    assert fired == [WORKSTATION]  # 跑了 10 轮，早就过了阈值，但只提醒一次


def test_reactivation_rearms_and_can_trigger_again():
    """专注相位结束（active 变 False）再重新开始（变 True）：视为新激活期，重新武装。"""
    active_lookup = ActiveLookup()
    active_lookup.set(WORKSTATION, True)
    detector = FakeDetector()
    detector.present = True
    fired = []
    clock = ManualClock()
    observer = _make_observer(
        detector=detector,
        active_lookup=active_lookup,
        on_distraction=fired.append,
        clock=clock,
        min_interval_s=1.0,
        trigger_seconds=5.0,
        release_seconds=3.0,
    )

    observer.wants_frame(WORKSTATION, clock())
    for _ in range(6):
        observer.on_frame(WORKSTATION, FAKE_JPEG, clock())
        clock.advance(1.0)
    assert fired == [WORKSTATION]

    # 相位结束：active 变 False，观察者不再要帧
    active_lookup.set(WORKSTATION, False)
    assert observer.wants_frame(WORKSTATION, clock()) is False

    # 新一轮专注相位开始：active 变回 True，应重新武装（fired/累计清零）
    active_lookup.set(WORKSTATION, True)
    assert observer.wants_frame(WORKSTATION, clock()) is True

    for _ in range(6):
        observer.on_frame(WORKSTATION, FAKE_JPEG, clock())
        clock.advance(1.0)

    assert fired == [WORKSTATION, WORKSTATION]  # 新激活期里又提醒了一次


def test_disappearance_shorter_than_release_does_not_reset_accumulation():
    """短暂消失（< release_seconds）不清零累计：中间瞄一眼手机又拿开不该被免罪重来。

    时间轴（min_interval=1.0, trigger=5.0, release=3.0），每步之间隔 1s 跑一次检测：
      t=0 present  dt=0  accumulated=0            （首帧不计 dt）
      t=1 present  dt=1  accumulated=1  last_seen=1
      t=2 absent   dt=1  gap=2-1=1 < 3   -> 不清零，accumulated 仍是 1
      t=3 absent   dt=1  gap=3-1=2 < 3   -> 不清零，accumulated 仍是 1
      t=4 present  dt=1  accumulated=2   last_seen=4
      t=5 present  dt=1  accumulated=3
      t=6 present  dt=1  accumulated=4
      t=7 present  dt=1  accumulated=5 >= trigger -> 触发
    如果消失期把累计清零了，t=7 时累计只会是 4（4 次 present tick 里只有 t=4..7 共 4 次
    dt=1），永远到不了 5，用这个差异断言"没被清零"。
    """
    active_lookup = ActiveLookup()
    active_lookup.set(WORKSTATION, True)
    detector = FakeDetector()
    fired = []
    clock = ManualClock()
    observer = _make_observer(
        detector=detector,
        active_lookup=active_lookup,
        on_distraction=fired.append,
        clock=clock,
        min_interval_s=1.0,
        trigger_seconds=5.0,
        release_seconds=3.0,
    )

    observer.wants_frame(WORKSTATION, clock())

    presence_by_tick = [True, True, False, False, True, True, True, True]
    for i, present in enumerate(presence_by_tick):
        detector.present = present
        observer.on_frame(WORKSTATION, FAKE_JPEG, clock())
        if i < len(presence_by_tick) - 1:
            assert fired == [], f"不该在第 {i} 步就触发"
            clock.advance(1.0)

    assert fired == [WORKSTATION]


def test_disappearance_longer_than_release_resets_accumulation():
    """消失超过 release_seconds：累计清零，之后要从头攒够 trigger_seconds 才触发。"""
    active_lookup = ActiveLookup()
    active_lookup.set(WORKSTATION, True)
    detector = FakeDetector()
    fired = []
    clock = ManualClock()
    observer = _make_observer(
        detector=detector,
        active_lookup=active_lookup,
        on_distraction=fired.append,
        clock=clock,
        min_interval_s=1.0,
        trigger_seconds=5.0,
        release_seconds=3.0,
    )

    observer.wants_frame(WORKSTATION, clock())
    detector.present = True
    for _ in range(4):
        observer.on_frame(WORKSTATION, FAKE_JPEG, clock())  # 累计到 3s
        clock.advance(1.0)

    # 消失 4s（> release_seconds=3s）：清零
    detector.present = False
    for _ in range(4):
        observer.on_frame(WORKSTATION, FAKE_JPEG, clock())
        clock.advance(1.0)

    # 重新出现，只攒了 3s（< 5s），不该触发
    detector.present = True
    for _ in range(3):
        observer.on_frame(WORKSTATION, FAKE_JPEG, clock())
        clock.advance(1.0)

    assert fired == []  # 清零生效：3s 不够，之前那 3s 白攒的没被继承


# ------------------------------------------------------------ 节流


def test_throttle_skips_detection_within_min_interval():
    """min_interval_s 内的重复帧不重新跑检测：既不解码推理，也不推进状态机。"""
    active_lookup = ActiveLookup()
    active_lookup.set(WORKSTATION, True)
    detector = FakeDetector()
    detector.present = True
    clock = ManualClock()
    observer = _make_observer(
        detector=detector,
        active_lookup=active_lookup,
        on_distraction=lambda ws: None,
        clock=clock,
        min_interval_s=1.0,
        trigger_seconds=5.0,
        release_seconds=3.0,
    )

    observer.wants_frame(WORKSTATION, clock())
    observer.on_frame(WORKSTATION, FAKE_JPEG, clock())  # 第一次，跑一次检测
    assert detector.calls == 1

    clock.advance(0.2)  # 远小于 min_interval_s=1.0
    observer.on_frame(WORKSTATION, FAKE_JPEG, clock())
    observer.on_frame(WORKSTATION, FAKE_JPEG, clock())
    assert detector.calls == 1  # 被节流，没有新的检测调用

    clock.advance(1.0)  # 凑够 1.2s，超过节流间隔
    observer.on_frame(WORKSTATION, FAKE_JPEG, clock())
    assert detector.calls == 2


# ------------------------------------------------------------ 降级


def test_disabled_when_detector_factory_fails():
    """检测器工厂抛异常（如模型文件缺失）：构造不炸，观察者降级为恒不要帧。"""
    active_lookup = ActiveLookup()
    active_lookup.set(WORKSTATION, True)

    def _broken_factory():
        raise FileNotFoundError("model file missing")

    observer = DistractionObserver(
        active_lookup=active_lookup,
        on_distraction=lambda ws: None,
        detector_factory=_broken_factory,
    )

    assert observer.wants_frame(WORKSTATION, 0.0) is False
    # on_frame 在 disabled 状态下也必须是安全的空操作，不抛异常
    observer.on_frame(WORKSTATION, FAKE_JPEG, 0.0)


# ------------------------------------------------------------ min_score 过滤


def test_low_score_detection_does_not_count_as_present():
    """分数低于 min_score 的检测结果不算「看到手机」，不该累计。"""
    active_lookup = ActiveLookup()
    active_lookup.set(WORKSTATION, True)
    detector = FakeDetector()
    detector.present = True
    detector.score = 0.1  # 低于 min_score=0.4
    fired = []
    clock = ManualClock()
    observer = _make_observer(
        detector=detector,
        active_lookup=active_lookup,
        on_distraction=fired.append,
        clock=clock,
        min_interval_s=1.0,
        trigger_seconds=2.0,
        release_seconds=3.0,
        min_score=0.4,
    )

    observer.wants_frame(WORKSTATION, clock())
    for _ in range(5):
        observer.on_frame(WORKSTATION, FAKE_JPEG, clock())
        clock.advance(1.0)

    assert fired == []


# ------------------------------------------------------------ build_reminder_text


def test_build_reminder_text_without_remaining():
    assert build_reminder_text(None) == "专注时间还没结束，我们坚持一下？"


def test_build_reminder_text_with_remaining_minutes():
    # 90s 向上取整为 2 分钟
    assert build_reminder_text(90) == "还剩2分钟，这个番茄钟就完成了。我们坚持一下？"


def test_build_reminder_text_exact_minute_boundary():
    # 恰好 60s：算 1 分钟，走正常分支而不是"最后一分钟"
    assert build_reminder_text(60) == "还剩1分钟，这个番茄钟就完成了。我们坚持一下？"


def test_build_reminder_text_under_one_minute():
    assert build_reminder_text(30) == "最后一分钟了，这个番茄钟就完成了。我们坚持一下？"


def test_build_reminder_text_zero_remaining():
    assert build_reminder_text(0) == "最后一分钟了，这个番茄钟就完成了。我们坚持一下？"
