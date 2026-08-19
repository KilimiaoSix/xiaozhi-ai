"""审批状态机的离线测试。

全部离线：推送函数、设备解析、时钟与 sleep 都注入假实现，超时窗口压到
亚秒级，不依赖真机、摄像头与 mediapipe。
"""

import asyncio

import pytest

from core.approval_manager import (
    ApprovalManager,
    SOURCE_GESTURE,
    SOURCE_MANUAL,
    SOURCE_TIMEOUT,
    STATUS_APPROVED,
    STATUS_DENIED,
    STATUS_PENDING,
    STATUS_TIMEOUT,
    TEXT_APPROVED,
    TEXT_DENIED,
    TEXT_TIMEOUT,
    get_approval_manager,
    reset_approval_manager,
)


class FakeConn:
    """占位连接。管理器只把它原样传给推送函数，不读它的字段。"""


class Recorder:
    """记录每次推送的参数，断言播报内容用。"""

    def __init__(self):
        self.calls = []

    async def push(self, conn, text, **kwargs):
        self.calls.append({"conn": conn, "text": text, **kwargs})
        return True

    @property
    def texts(self):
        return [call["text"] for call in self.calls]


def build_manager(*, conn=None, config=None, recorder=None, clock=None):
    """默认给一台在线设备与 0.2 秒超时窗口，测试跑得完又不至于抢跑。"""
    recorder = recorder or Recorder()
    conn = FakeConn() if conn is None else conn
    manager = ApprovalManager(
        config if config is not None else {"approval": {"timeout_s": 0.2}},
        push_event=recorder.push,
        device_resolver=lambda: conn,
        clock=clock,
    )
    return manager, recorder


async def wait_until(predicate, timeout=3.0, what="条件"):
    """轮询等到 predicate 成立。

    不按固定时长 sleep：超时与推送是两个独立的任务，固定 sleep 要么等不够
    要么白等（同 test_pomodoro_manager.wait_until）。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate() and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert predicate(), f"等待超时：{what}"


# ---------------------------------------------------------------- 创建与播报


@pytest.mark.asyncio
async def test_create_request_returns_pending_snapshot():
    manager, _ = build_manager()

    result = await manager.create_request("运行项目测试")

    assert result["status"] == STATUS_PENDING
    assert result["approval_id"]
    assert result["risk"] == "local_test"
    assert result["expires_at"] > result["created_at"]
    assert manager.has_pending() is True


@pytest.mark.asyncio
async def test_create_request_announces_summary_to_robot():
    """播报必须说清"谁要做什么"并给出手势提示——这是主人唯一的知情渠道。"""
    manager, recorder = build_manager()

    await manager.create_request("运行项目测试，只在本地执行")

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["text"] == (
        "Codex 想运行项目测试，只在本地执行。竖个大拇指我就让它继续。"
    )
    assert call["emotion"] == "confused"
    assert call["status"] == "等待审批"
    assert call["action"] == "look_left"
    assert call["speak"] is True


@pytest.mark.asyncio
async def test_create_request_survives_offline_device():
    """设备离线仍要建请求：钩子端靠 GET 状态和自身超时兜底。"""
    manager, recorder = build_manager(conn=None)
    manager._device_resolver = lambda: None

    result = await manager.create_request("运行项目测试")

    assert result["status"] == STATUS_PENDING
    assert recorder.calls == []
    assert manager.get(result["approval_id"])["status"] == STATUS_PENDING


@pytest.mark.asyncio
async def test_create_request_survives_push_failure():
    """播报炸了不该让请求创建失败。"""

    async def boom(conn, text, **kwargs):
        raise RuntimeError("TTS 没就绪")

    manager = ApprovalManager(
        {"approval": {"timeout_s": 0.2}},
        push_event=boom,
        device_resolver=FakeConn,
    )

    result = await manager.create_request("运行项目测试")
    assert result["status"] == STATUS_PENDING


@pytest.mark.asyncio
async def test_create_request_rejects_empty_summary():
    manager, _ = build_manager()
    with pytest.raises(ValueError):
        await manager.create_request("   ")


@pytest.mark.asyncio
async def test_explicit_timeout_overrides_config():
    manager, _ = build_manager(config={"approval": {"timeout_s": 25}})

    result = await manager.create_request("运行项目测试", timeout_s=3)

    assert result["expires_at"] - result["created_at"] == pytest.approx(3.0, abs=0.05)


# ---------------------------------------------------------------- 批准与拒绝


@pytest.mark.asyncio
async def test_approve_moves_to_approved_and_announces():
    manager, recorder = build_manager()
    created = await manager.create_request("运行项目测试")

    result = await manager.resolve(
        created["approval_id"], STATUS_APPROVED, SOURCE_GESTURE
    )

    assert result["status"] == STATUS_APPROVED
    assert result["decision_source"] == SOURCE_GESTURE
    assert manager.has_pending() is False

    outcome = recorder.calls[-1]
    assert outcome["text"] == TEXT_APPROVED
    assert outcome["emotion"] == "happy"
    assert outcome["action"] == "nod"


@pytest.mark.asyncio
async def test_deny_moves_to_denied_and_announces():
    manager, recorder = build_manager()
    created = await manager.create_request("运行项目测试")

    result = await manager.resolve(
        created["approval_id"], STATUS_DENIED, SOURCE_GESTURE
    )

    assert result["status"] == STATUS_DENIED
    outcome = recorder.calls[-1]
    assert outcome["text"] == TEXT_DENIED
    assert outcome["emotion"] == "neutral"
    assert outcome["action"] == "shake"


@pytest.mark.asyncio
async def test_resolve_is_idempotent_and_keeps_first_decision():
    """手势与人工兜底可能同时到达，第二次不得翻案、不得二次播报。"""
    manager, recorder = build_manager()
    created = await manager.create_request("运行项目测试")
    approval_id = created["approval_id"]

    await manager.resolve(approval_id, STATUS_DENIED, SOURCE_GESTURE)
    pushes_after_first = len(recorder.calls)

    second = await manager.resolve(approval_id, STATUS_APPROVED, SOURCE_MANUAL)

    assert second["status"] == STATUS_DENIED
    assert second["decision_source"] == SOURCE_GESTURE
    assert len(recorder.calls) == pushes_after_first


@pytest.mark.asyncio
async def test_resolve_rejects_timeout_as_decision():
    """timeout 只能由计时器产生，外部不能塞进来冒充用户拒绝。"""
    manager, _ = build_manager()
    created = await manager.create_request("运行项目测试")

    with pytest.raises(ValueError):
        await manager.resolve(created["approval_id"], STATUS_TIMEOUT, SOURCE_MANUAL)


@pytest.mark.asyncio
async def test_resolve_unknown_id_returns_none():
    manager, _ = build_manager()
    assert await manager.resolve("nope", STATUS_APPROVED) is None
    assert manager.get("nope") is None


# ---------------------------------------------------------------- 超时默认拒绝


@pytest.mark.asyncio
async def test_timeout_defaults_to_not_approved():
    """需求硬性要求：超时一律不批准。"""
    manager, recorder = build_manager()
    created = await manager.create_request("运行项目测试", timeout_s=0.05)

    await wait_until(
        lambda: manager.get(created["approval_id"])["status"] != STATUS_PENDING,
        what="请求超时",
    )

    snapshot = manager.get(created["approval_id"])
    assert snapshot["status"] == STATUS_TIMEOUT
    assert snapshot["decision_source"] == SOURCE_TIMEOUT
    assert manager.has_pending() is False
    await wait_until(
        lambda: recorder.texts[-1] == TEXT_TIMEOUT, what="超时播报"
    )


@pytest.mark.asyncio
async def test_decision_before_deadline_cancels_timeout():
    """在超时窗口内做决策，之后不该被计时器改写。"""
    manager, _ = build_manager()
    created = await manager.create_request("运行项目测试", timeout_s=0.08)

    await manager.resolve(created["approval_id"], STATUS_APPROVED, SOURCE_GESTURE)
    await asyncio.sleep(0.15)

    assert manager.get(created["approval_id"])["status"] == STATUS_APPROVED


@pytest.mark.asyncio
async def test_sleep_is_injectable():
    """计时用注入的 sleep，测试不必真等 25 秒。"""
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    manager = ApprovalManager(
        {"approval": {"timeout_s": 25}},
        push_event=Recorder().push,
        device_resolver=FakeConn,
        sleep=fake_sleep,
    )
    created = await manager.create_request("运行项目测试")

    await wait_until(
        lambda: manager.get(created["approval_id"])["status"] == STATUS_TIMEOUT,
        what="注入 sleep 后立即超时",
    )
    assert slept == [25.0]


# ---------------------------------------------------------------- resolve_pending


@pytest.mark.asyncio
async def test_resolve_pending_takes_oldest():
    """手势不知道 approval_id，按创建先后取最老的那条。"""
    manager, _ = build_manager()
    first = await manager.create_request("运行项目测试", timeout_s=5)
    second = await manager.create_request("跑一次构建", timeout_s=5)

    result = await manager.resolve_pending(STATUS_APPROVED, SOURCE_GESTURE)

    assert result["approval_id"] == first["approval_id"]
    assert manager.get(first["approval_id"])["status"] == STATUS_APPROVED
    assert manager.get(second["approval_id"])["status"] == STATUS_PENDING


@pytest.mark.asyncio
async def test_resolve_pending_skips_settled_requests():
    manager, _ = build_manager()
    first = await manager.create_request("运行项目测试", timeout_s=5)
    second = await manager.create_request("跑一次构建", timeout_s=5)
    await manager.resolve(first["approval_id"], STATUS_DENIED, SOURCE_MANUAL)

    result = await manager.resolve_pending(STATUS_APPROVED, SOURCE_GESTURE)

    assert result["approval_id"] == second["approval_id"]


@pytest.mark.asyncio
async def test_resolve_pending_without_pending_returns_none():
    manager, _ = build_manager()
    assert await manager.resolve_pending(STATUS_APPROVED, SOURCE_GESTURE) is None


@pytest.mark.asyncio
async def test_has_pending_gates_gesture_observer():
    """has_pending 是手势观察者的门控，必须随状态精确翻转。"""
    manager, _ = build_manager()
    assert manager.has_pending() is False

    created = await manager.create_request("运行项目测试", timeout_s=5)
    assert manager.has_pending() is True

    await manager.resolve(created["approval_id"], STATUS_APPROVED, SOURCE_GESTURE)
    assert manager.has_pending() is False


# ---------------------------------------------------------------- 设备解析与单例


@pytest.mark.asyncio
async def test_configured_device_id_wins_over_registry_guess():
    class Registry:
        def __init__(self):
            self.asked = []

        def get(self, device_id):
            self.asked.append(device_id)
            return FakeConn()

        def device_ids(self):
            return ["a", "b"]

    registry = Registry()
    recorder = Recorder()
    manager = ApprovalManager(
        {"approval": {"timeout_s": 5, "device_id": "b"}},
        registry,
        push_event=recorder.push,
    )

    await manager.create_request("运行项目测试")

    assert registry.asked == ["b"]
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_multiple_online_devices_without_config_skips_announce():
    """多台在线又没配 device_id 时不猜，宁可不播报也不要冲错设备喊。"""

    class Registry:
        def get(self, device_id):
            return FakeConn()

        def device_ids(self):
            return ["a", "b"]

    recorder = Recorder()
    manager = ApprovalManager(
        {"approval": {"timeout_s": 5}}, Registry(), push_event=recorder.push
    )

    result = await manager.create_request("运行项目测试")

    assert result["status"] == STATUS_PENDING
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_single_online_device_is_used_without_config():
    class Registry:
        def get(self, device_id):
            return FakeConn()

        def device_ids(self):
            return ["only-one"]

    recorder = Recorder()
    manager = ApprovalManager(
        {"approval": {"timeout_s": 5}}, Registry(), push_event=recorder.push
    )

    await manager.create_request("运行项目测试")
    assert len(recorder.calls) == 1


def test_singleton_is_shared_and_resettable():
    """手势观察者与 HTTP 接口必须拿到同一个实例，否则决策落不到同一批请求上。"""
    reset_approval_manager()
    try:
        first = get_approval_manager({"approval": {"timeout_s": 5}})
        second = get_approval_manager()
        assert first is second

        reset_approval_manager()
        assert get_approval_manager() is not first
    finally:
        reset_approval_manager()


def test_bind_fills_gaps_without_overwriting():
    reset_approval_manager()
    try:
        manager = get_approval_manager()
        manager.bind(config={"approval": {"timeout_s": 7}})
        assert manager.settings.timeout_s == 7.0

        # 已有配置不该被后来的装配覆盖
        manager.bind(config={"approval": {"timeout_s": 99}})
        assert manager.settings.timeout_s == 7.0
    finally:
        reset_approval_manager()
