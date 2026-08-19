"""手势观察者的离线测试。

识别器一律用假实现（注入 recognizer_factory），不加载真模型：真模型要一秒量级
建图、要 Metal 上下文，单测里不该碰。真模型的可用性由
scripts/download_gesture_model.py --verify 单独验证。

帧本身用 cv2 真编真解，让节流与解码失败这两条路径走的是生产代码。
"""

import asyncio
import warnings

import cv2
import numpy as np
import pytest

from core.camera_stream.gesture_observer import (
    DECISION_APPROVED,
    DECISION_DENIED,
    GESTURE_THUMB_DOWN,
    GESTURE_THUMB_UP,
    SOURCE_GESTURE,
    GestureObserver,
    create_gesture_observer,
)


WORKSTATION = "desk-1"


def make_jpeg(width=64, height=48) -> bytes:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    ok, buffer = cv2.imencode(".jpg", frame)
    assert ok
    return buffer.tobytes()


JPEG = make_jpeg()


class FakeRecognizer:
    """按预设脚本逐帧吐结果，跑完脚本后一直返回最后一项。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.closed = False

    def classify(self, frame_bgr):
        assert frame_bgr is not None
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[index]

    def close(self):
        self.closed = True


class Decisions:
    """同步 resolve 回调，记录收到的决策。"""

    def __init__(self):
        self.calls = []

    def resolve(self, decision, source):
        self.calls.append((decision, source))
        return {"status": decision}


def build_observer(script, *, pending=True, required_hits=3, min_score=0.5,
                   min_interval_s=0.0, decisions=None):
    decisions = decisions or Decisions()
    recognizer = FakeRecognizer(script)
    observer = GestureObserver(
        has_pending=lambda: pending() if callable(pending) else pending,
        resolve=decisions.resolve,
        recognizer_factory=lambda: recognizer,
        required_hits=required_hits,
        min_score=min_score,
        min_interval_s=min_interval_s,
    )
    return observer, decisions, recognizer


def feed(observer, count, start=0.0, step=1.0):
    """喂 count 帧，时间戳按 step 递增（默认远大于节流窗口）。"""
    for i in range(count):
        observer.on_frame(WORKSTATION, JPEG, start + i * step)


# ---------------------------------------------------------------- wants_frame 门控


def test_wants_frame_only_when_pending():
    observer, _, _ = build_observer([[]], pending=False)
    assert observer.wants_frame(WORKSTATION, 0.0) is False

    observer, _, _ = build_observer([[]], pending=True)
    assert observer.wants_frame(WORKSTATION, 0.0) is True


def test_wants_frame_false_when_disabled():
    """降级后拿到帧也识别不了，返回 False 才不会白白解码每一帧。"""
    observer = GestureObserver(
        has_pending=lambda: True,
        resolve=lambda decision, source: None,
        model_path="/nonexistent/gesture_recognizer.task",
    )
    assert observer.disabled is True
    assert observer.wants_frame(WORKSTATION, 0.0) is False


def test_wants_frame_survives_failing_predicate():
    def boom():
        raise RuntimeError("registry 挂了")

    observer = GestureObserver(
        has_pending=boom,
        resolve=lambda decision, source: None,
        recognizer_factory=lambda: FakeRecognizer([[]]),
    )
    assert observer.wants_frame(WORKSTATION, 0.0) is False


# ---------------------------------------------------------------- 连续帧判定


def test_consecutive_thumb_up_approves():
    observer, decisions, _ = build_observer(
        [[(GESTURE_THUMB_UP, 0.9)]], required_hits=3
    )

    feed(observer, 2)
    assert decisions.calls == [], "不足 required_hits 不该批准"

    feed(observer, 1, start=2.0)
    assert decisions.calls == [(DECISION_APPROVED, SOURCE_GESTURE)]


def test_consecutive_thumb_down_denies():
    observer, decisions, _ = build_observer(
        [[(GESTURE_THUMB_DOWN, 0.9)]], required_hits=3
    )

    feed(observer, 3)
    assert decisions.calls == [(DECISION_DENIED, SOURCE_GESTURE)]


def test_interrupted_streak_resets():
    """中间断一帧就得从头数：单帧误识别不该攒成一次放行。"""
    script = [
        [(GESTURE_THUMB_UP, 0.9)],
        [(GESTURE_THUMB_UP, 0.9)],
        [],                                # 手移出画面
        [(GESTURE_THUMB_UP, 0.9)],
        [(GESTURE_THUMB_UP, 0.9)],
    ]
    observer, decisions, _ = build_observer(script, required_hits=3)

    feed(observer, 5)
    assert decisions.calls == [], "断过一帧后只攒到 2 帧，不该批准"

    feed(observer, 1, start=5.0)  # 脚本跑完后继续返回最后一项（Thumb_Up）
    assert decisions.calls == [(DECISION_APPROVED, SOURCE_GESTURE)]


def test_switching_gesture_restarts_count():
    """从大拇指朝上换成朝下，不该拿之前的计数凑数。"""
    script = [
        [(GESTURE_THUMB_UP, 0.9)],
        [(GESTURE_THUMB_UP, 0.9)],
        [(GESTURE_THUMB_DOWN, 0.9)],
    ]
    observer, decisions, _ = build_observer(script, required_hits=3)

    feed(observer, 3)
    assert decisions.calls == []


def test_low_score_frames_are_ignored():
    """够不上 min_score 一律当没看见——不确定就不批准。"""
    observer, decisions, _ = build_observer(
        [[(GESTURE_THUMB_UP, 0.3)]], required_hits=2, min_score=0.5
    )

    feed(observer, 4)
    assert decisions.calls == []


def test_unrelated_gestures_do_not_decide():
    observer, decisions, _ = build_observer(
        [[("Victory", 0.95)], [("Open_Palm", 0.95)], [("Closed_Fist", 0.95)]],
        required_hits=2,
    )

    feed(observer, 6)
    assert decisions.calls == []


def test_streak_resets_after_decision():
    """决策投出去后要清零，否则紧接着的几帧会把下一条请求也解决掉。"""
    observer, decisions, _ = build_observer(
        [[(GESTURE_THUMB_UP, 0.9)]], required_hits=3
    )

    feed(observer, 3)
    assert decisions.calls == [(DECISION_APPROVED, SOURCE_GESTURE)]

    feed(observer, 2, start=3.0)
    assert len(decisions.calls) == 1, "清零后要再攒满 3 帧才能再决策"

    feed(observer, 1, start=5.0)
    assert len(decisions.calls) == 2


# ---------------------------------------------------------------- 节流


def test_throttle_skips_frames_inside_window():
    observer, decisions, recognizer = build_observer(
        [[]], min_interval_s=0.35, required_hits=1
    )

    observer.on_frame(WORKSTATION, JPEG, 100.0)
    observer.on_frame(WORKSTATION, JPEG, 100.1)
    observer.on_frame(WORKSTATION, JPEG, 100.2)
    assert recognizer.calls == 1, "窗口内的帧应直接丢弃，不进识别"

    observer.on_frame(WORKSTATION, JPEG, 100.4)
    assert recognizer.calls == 2


def test_throttled_frames_do_not_count_toward_streak():
    """节流掉的帧既不识别也不计数，否则 5fps 会让 required_hits 形同虚设。"""
    observer, decisions, _ = build_observer(
        [[(GESTURE_THUMB_UP, 0.9)]], min_interval_s=0.35, required_hits=3
    )

    for i in range(9):
        observer.on_frame(WORKSTATION, JPEG, 200.0 + i * 0.1)

    assert decisions.calls == [(DECISION_APPROVED, SOURCE_GESTURE)]


# ---------------------------------------------------------------- 降级


def test_missing_model_disables_without_raising():
    """缺模型不能炸构造：审批还能走超时默认拒绝与 HTTP 人工决策。"""
    observer = GestureObserver(
        has_pending=lambda: True,
        resolve=lambda decision, source: None,
        model_path="/nonexistent/gesture_recognizer.task",
    )

    assert observer.disabled is True
    observer.on_frame(WORKSTATION, JPEG, 0.0)  # 不该抛


def test_recognizer_build_failure_disables_permanently():
    """建图失败就永久降级，别每帧重试一次白吃一秒。"""
    attempts = []

    def boom():
        attempts.append(1)
        raise RuntimeError("Metal 上下文建不起来")

    observer = GestureObserver(
        has_pending=lambda: True,
        resolve=lambda decision, source: None,
        recognizer_factory=boom,
        min_interval_s=0.0,
    )

    feed(observer, 3)
    assert observer.disabled is True
    assert len(attempts) == 1


def test_decode_failure_is_survivable():
    observer, decisions, recognizer = build_observer([[]], required_hits=1)

    observer.on_frame(WORKSTATION, b"not a jpeg at all", 0.0)

    assert recognizer.calls == 0
    assert decisions.calls == []


def test_classify_failure_is_survivable():
    class Exploding:
        def classify(self, frame):
            raise RuntimeError("推理炸了")

    observer = GestureObserver(
        has_pending=lambda: True,
        resolve=lambda decision, source: None,
        recognizer_factory=Exploding,
        min_interval_s=0.0,
    )

    observer.on_frame(WORKSTATION, JPEG, 0.0)  # 不该抛
    assert observer.disabled is False, "单帧识别失败不该整体降级"


def test_recognizer_is_lazily_built():
    """建图要一秒量级，没有待审批时不该付这个代价。"""
    built = []

    def factory():
        built.append(1)
        return FakeRecognizer([[]])

    observer = GestureObserver(
        has_pending=lambda: True,
        resolve=lambda decision, source: None,
        recognizer_factory=factory,
        min_interval_s=0.0,
    )
    assert built == []

    observer.on_frame(WORKSTATION, JPEG, 0.0)
    observer.on_frame(WORKSTATION, JPEG, 1.0)
    assert built == [1], "只建一次，之后复用"


# ---------------------------------------------------------------- 回到事件循环


@pytest.mark.asyncio
async def test_async_resolve_is_submitted_to_injected_loop():
    """on_frame 在工作线程跑，协程 resolve 必须经 loop 投递才能落地。"""
    seen = []

    async def resolve(decision, source):
        seen.append((decision, source))
        return {"status": decision}

    loop = asyncio.get_running_loop()
    observer = GestureObserver(
        has_pending=lambda: True,
        resolve=resolve,
        recognizer_factory=lambda: FakeRecognizer([[(GESTURE_THUMB_UP, 0.9)]]),
        loop=loop,
        required_hits=2,
        min_interval_s=0.0,
    )

    # 真在工作线程里喂帧，复刻 asyncio.to_thread 的调用形态
    await asyncio.to_thread(feed, observer, 2)
    await asyncio.sleep(0.05)

    assert seen == [(DECISION_APPROVED, SOURCE_GESTURE)]


def test_async_resolve_without_loop_is_reported_not_silently_dropped():
    """漏注入 loop 是装配错误：决策落不了地，必须记 error 且不留悬空协程。"""
    logged = []

    class CaptureLogger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, message, *a, **k):
            logged.append(message)

        def exception(self, *a, **k):
            pass

    async def resolve(decision, source):
        return None

    observer = GestureObserver(
        has_pending=lambda: True,
        resolve=resolve,
        recognizer_factory=lambda: FakeRecognizer([[(GESTURE_THUMB_UP, 0.9)]]),
        required_hits=1,
        min_interval_s=0.0,
        logger=CaptureLogger(),
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        observer.on_frame(WORKSTATION, JPEG, 0.0)

    assert logged, "没有 loop 时必须记一条 error"
    assert not [w for w in caught if "never awaited" in str(w.message)]


# ---------------------------------------------------------------- 装配


def test_manager_shorthand_uses_manager_callbacks():
    class FakeManager:
        def __init__(self):
            self.pending = True
            self.calls = []

        def has_pending(self):
            return self.pending

        def resolve_pending(self, decision, source):
            self.calls.append((decision, source))
            return None

    manager = FakeManager()
    observer = GestureObserver(
        manager,
        recognizer_factory=lambda: FakeRecognizer([[(GESTURE_THUMB_UP, 0.9)]]),
        required_hits=1,
        min_interval_s=0.0,
    )

    assert observer.wants_frame(WORKSTATION, 0.0) is True
    observer.on_frame(WORKSTATION, JPEG, 0.0)
    assert manager.calls == [(DECISION_APPROVED, SOURCE_GESTURE)]


def test_requires_manager_or_callbacks():
    with pytest.raises(ValueError):
        GestureObserver()


def test_create_gesture_observer_respects_enabled_flag():
    class FakeManager:
        def has_pending(self):
            return False

        def resolve_pending(self, decision, source):
            return None

    assert (
        create_gesture_observer(
            {"approval": {"gesture": {"enabled": False}}}, FakeManager()
        )
        is None
    )


def test_create_gesture_observer_reads_tuning():
    class FakeManager:
        def has_pending(self):
            return False

        def resolve_pending(self, decision, source):
            return None

    observer = create_gesture_observer(
        {
            "approval": {
                "gesture": {
                    "required_hits": 5,
                    "min_score": 0.7,
                    "min_interval_s": 0.5,
                    "model_path": "/nonexistent/x.task",
                }
            }
        },
        FakeManager(),
    )

    assert observer is not None
    assert observer._required_hits == 5
    assert observer._min_score == 0.7
    assert observer._min_interval_s == 0.5
    # 指定的模型不存在 -> 降级而不是抛
    assert observer.disabled is True


def test_close_releases_recognizer():
    observer, _, recognizer = build_observer([[]], required_hits=1)
    observer.on_frame(WORKSTATION, JPEG, 0.0)

    observer.close()
    assert recognizer.closed is True


def test_relative_model_path_resolves_against_project_dir():
    """服务可能从任意 cwd 启动，配置里的相对路径必须按仓库根解析。"""
    import os

    from core.camera_stream.gesture_observer import (
        default_model_path,
        resolve_model_path,
    )

    resolved = resolve_model_path("data/models/gesture_recognizer.task")
    assert os.path.isabs(resolved)
    assert resolved == default_model_path()
    assert resolve_model_path(None) == default_model_path()
    assert resolve_model_path("/abs/x.task") == "/abs/x.task"
