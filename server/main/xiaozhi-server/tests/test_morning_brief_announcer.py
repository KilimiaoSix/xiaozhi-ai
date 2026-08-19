"""早上第一次认出主人时播报晨报的编排。"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core.morning_brief.announcer import (
    MorningBriefAnnouncer,
    create_morning_brief_announcer,
)


CST = timezone(timedelta(hours=8))


def local(hour, minute=0, *, day=19):
    """2026-08-19 是周三，19 号之外的日期由调用方指定。"""
    return datetime(2026, 8, day, hour, minute, tzinfo=CST)


def make_config(**announce):
    settings = {
        "enabled": True,
        "bindings": [{"workstation_id": "desk", "device_id": "robot"}],
    }
    settings.update(announce)
    return {"morning_brief": {"enabled": True, "announce": settings}}


OWNER_PRESENT = {"effective_state": "present", "identity": {"state": "owner"}}


class FakePresenceRegistry:
    def __init__(self, snapshot=None):
        self.snapshot = OWNER_PRESENT if snapshot is None else snapshot

    def get(self, workstation_id):
        return self.snapshot


class WorkstationPresenceRegistry:
    """按工位区分的注册表：未上报的工位返回 None，与真实 PresenceRegistry 一致。"""

    def __init__(self, snapshots):
        self.snapshots = snapshots

    def get(self, workstation_id):
        return self.snapshots.get(workstation_id)


class FakeDeviceRegistry:
    def __init__(self, devices):
        self.devices = devices

    def device_ids(self):
        return list(self.devices)

    def get(self, device_id):
        return self.devices.get(device_id)


class FakeService:
    def __init__(self, report=None, error=None):
        self.report = report or {
            "coverage_status": "COMPLETE",
            "top_three": [
                {
                    "kind": "MESSAGE",
                    "item_id": "m1",
                    "topic_id": "t1",
                    "title": "回滚线上发布",
                    "score": 100,
                    "reasons": ["direct_mention"],
                    "source_url": "https://example.invalid",
                    "status": "OPEN_NEW",
                    "confidence": "HIGH",
                }
            ],
            "other_unhandled_mentions": [],
            "calendar": [],
            "reauthorization_required": False,
            "permission_required": False,
        }
        self.error = error
        self.calls = 0

    async def preview(self, report_date=None, limit=3):
        self.calls += 1
        self.last_limit = limit
        if self.error is not None:
            raise self.error
        return self.report


class MarkedFakeService(FakeService):
    """带播报标记的服务桩，marker 字典可在多个实例间共享以模拟重启。"""

    def __init__(self, marker, report=None, error=None):
        super().__init__(report=report, error=error)
        self.marker = marker

    def was_announced(self, workstation_id, report_date):
        return (workstation_id, str(report_date)) in self.marker

    def mark_announced(self, workstation_id, report_date):
        self.marker[(workstation_id, str(report_date))] = True


class RecordingLogger:
    def __init__(self):
        self.lines = []

    def _record(self, message, *args, **kwargs):
        self.lines.append(str(message))

    info = warning = error = exception = _record


def build(config, presence, devices, service, now, busy_reason=None):
    """now 用可变容器传入，测试内推进时间。"""
    pushes = []

    async def push(conn, **kwargs):
        pushes.append((conn, kwargs))
        return True

    announcer = MorningBriefAnnouncer(
        config,
        presence,
        devices,
        service,
        push=push,
        now_provider=lambda: now[0],
        busy_reason=busy_reason or (lambda conn: None),
    )
    return announcer, pushes


@pytest.mark.asyncio
async def test_first_owner_sighting_in_window_announces_once():
    conn = object()
    now = [local(8, 5)]
    service = FakeService()
    announcer, pushes = build(
        make_config(),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": conn}),
        service,
        now,
    )

    await announcer.tick()
    now[0] = local(8, 6)
    await announcer.tick()

    assert service.calls == 1
    assert len(pushes) == 1
    assert pushes[0][0] is conn
    assert "回滚线上发布" in pushes[0][1]["text"]
    assert pushes[0][1]["speak"] is True


@pytest.mark.asyncio
async def test_owner_present_before_the_window_is_announced_once_it_opens():
    conn = object()
    now = [local(7, 50)]
    service = FakeService()
    announcer, pushes = build(
        make_config(),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": conn}),
        service,
        now,
    )

    await announcer.tick()
    assert pushes == []

    now[0] = local(8, 0)
    await announcer.tick()

    assert len(pushes) == 1


@pytest.mark.asyncio
async def test_after_the_window_closes_nothing_is_announced():
    now = [local(9, 31)]
    service = FakeService()
    announcer, pushes = build(
        make_config(),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
    )

    await announcer.tick()

    assert service.calls == 0
    assert pushes == []


@pytest.mark.asyncio
async def test_someone_who_is_not_the_owner_gets_no_brief():
    now = [local(8, 5)]
    service = FakeService()
    announcer, pushes = build(
        make_config(),
        FakePresenceRegistry(
            {"effective_state": "present", "identity": {"state": "unknown"}}
        ),
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
    )

    await announcer.tick()

    assert service.calls == 0
    assert pushes == []


@pytest.mark.asyncio
async def test_stale_presence_is_not_a_sighting():
    now = [local(8, 5)]
    service = FakeService()
    announcer, pushes = build(
        make_config(),
        FakePresenceRegistry(
            {"effective_state": "stale", "identity": {"state": "owner"}}
        ),
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
    )

    await announcer.tick()

    assert service.calls == 0
    assert pushes == []


@pytest.mark.asyncio
async def test_weekend_is_skipped():
    # 2026-08-22 是周六
    now = [local(8, 5, day=22)]
    service = FakeService()
    announcer, pushes = build(
        make_config(),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
    )

    await announcer.tick()

    assert service.calls == 0
    assert pushes == []


@pytest.mark.asyncio
async def test_offline_robot_does_not_burn_a_feishu_scan_and_retries_later():
    devices = FakeDeviceRegistry({})
    now = [local(8, 5)]
    service = FakeService()
    announcer, pushes = build(
        make_config(retry_seconds=60),
        FakePresenceRegistry(),
        devices,
        service,
        now,
    )

    await announcer.tick()
    assert service.calls == 0

    devices.devices["robot"] = object()
    now[0] = local(8, 5) + timedelta(seconds=30)
    await announcer.tick()
    assert service.calls == 0, "重试间隔内不该重复尝试"

    now[0] = local(8, 5) + timedelta(seconds=61)
    await announcer.tick()

    assert service.calls == 1
    assert len(pushes) == 1


@pytest.mark.asyncio
async def test_scan_failures_retry_then_give_up_for_the_day():
    now = [local(8, 0)]
    service = FakeService(error=RuntimeError("feishu down"))
    announcer, pushes = build(
        make_config(retry_seconds=60, max_attempts=3),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
    )

    for minute in range(0, 30):
        now[0] = local(8, 0) + timedelta(minutes=minute)
        await announcer.tick()

    assert service.calls == 3
    assert pushes == []


@pytest.mark.asyncio
async def test_push_failure_is_retried_on_the_next_attempt():
    now = [local(8, 5)]
    service = FakeService()
    attempts = []

    async def push(conn, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError("websocket closed")
        return True

    announcer = MorningBriefAnnouncer(
        make_config(retry_seconds=60),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        push=push,
        busy_reason=lambda conn: None,
        now_provider=lambda: now[0],
    )

    await announcer.tick()
    now[0] = local(8, 5) + timedelta(seconds=61)
    await announcer.tick()

    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_configured_wording_reaches_the_device():
    now = [local(8, 5)]
    announcer, pushes = build(
        make_config(greeting="早", status="今日待办", emotion="laughing"),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        FakeService(),
        now,
    )

    await announcer.tick()

    assert pushes[0][1]["status"] == "今日待办"
    assert pushes[0][1]["emotion"] == "laughing"
    assert pushes[0][1]["text"].startswith("早，")


@pytest.mark.asyncio
async def test_brief_waits_while_the_device_is_speaking():
    # 到岗迎接是事件驱动的、先到一步，它的 TTS 还在放时插播会被降级成只上屏
    busy = ["设备正在播放语音"]
    now = [local(8, 5)]
    service = FakeService()
    announcer, pushes = build(
        make_config(),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
        busy_reason=lambda conn: busy[0],
    )

    await announcer.tick()
    assert service.calls == 0, "设备忙时不该白扫一次飞书"
    assert pushes == []

    busy[0] = None
    now[0] = local(8, 5) + timedelta(seconds=5)
    await announcer.tick()

    assert len(pushes) == 1
    assert pushes[0][1]["speak"] is True


@pytest.mark.asyncio
async def test_a_device_that_never_frees_up_still_gets_the_brief_on_screen():
    now = [local(8, 5)]
    announcer, pushes = build(
        make_config(busy_grace_seconds=120),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        FakeService(),
        now,
        busy_reason=lambda conn: "设备正在播放语音",
    )

    await announcer.tick()
    now[0] = local(8, 5) + timedelta(seconds=119)
    await announcer.tick()
    assert pushes == []

    now[0] = local(8, 5) + timedelta(seconds=121)
    await announcer.tick()

    assert len(pushes) == 1


@pytest.mark.asyncio
async def test_greeting_can_be_emptied_so_the_arrival_greeting_is_not_repeated():
    now = [local(8, 5)]
    announcer, pushes = build(
        make_config(greeting=""),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        FakeService(),
        now,
    )

    await announcer.tick()

    assert pushes[0][1]["text"].startswith("今天 ")


@pytest.mark.asyncio
async def test_configured_window_replaces_the_default_one():
    now = [local(6, 59)]
    service = FakeService()
    announcer, pushes = build(
        make_config(window_start="07:00", window_end="07:30"),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
    )

    await announcer.tick()
    assert pushes == []

    now[0] = local(7, 10)
    await announcer.tick()
    assert len(pushes) == 1

    # 默认窗口内不再补播，说明窗口确实被替换而不是叠加
    now[0] = local(8, 5)
    await announcer.tick()
    assert len(pushes) == 1


@pytest.mark.asyncio
async def test_configured_workdays_replace_the_default_ones():
    # 2026-08-22 是周六，配成七天全播时也该播
    now = [local(8, 5, day=22)]
    announcer, pushes = build(
        make_config(workdays=[1, 2, 3, 4, 5, 6, 7]),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        FakeService(),
        now,
    )

    await announcer.tick()

    assert len(pushes) == 1


@pytest.mark.asyncio
async def test_a_new_day_gets_a_new_brief():
    now = [local(8, 5)]
    service = FakeService()
    announcer, pushes = build(
        make_config(),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
    )

    await announcer.tick()
    now[0] = local(8, 5, day=20)
    await announcer.tick()

    assert service.calls == 2
    assert len(pushes) == 2


@pytest.mark.asyncio
async def test_device_id_is_auto_bound_only_when_exactly_one_robot_is_online():
    now = [local(8, 5)]
    service = FakeService()
    announcer, pushes = build(
        {
            "morning_brief": {
                "enabled": True,
                "announce": {
                    "enabled": True,
                    "bindings": [
                        {"workstation_id": "desk", "device_id": ""}
                    ],
                },
            }
        },
        FakePresenceRegistry(),
        FakeDeviceRegistry({"one": object(), "two": object()}),
        service,
        now,
    )

    await announcer.tick()

    assert service.calls == 0
    assert pushes == []


def test_factory_returns_none_when_the_feature_is_off():
    presence = FakePresenceRegistry()
    devices = FakeDeviceRegistry({})
    service = FakeService()

    assert (
        create_morning_brief_announcer(
            {"morning_brief": {"enabled": False, "announce": {"enabled": True}}},
            presence,
            devices,
            service,
        )
        is None
    )
    assert (
        create_morning_brief_announcer(
            {"morning_brief": {"enabled": True, "announce": {"enabled": False}}},
            presence,
            devices,
            service,
        )
        is None
    )
    assert (
        create_morning_brief_announcer(make_config(), presence, None, service) is None
    )
    assert isinstance(
        create_morning_brief_announcer(make_config(), presence, devices, service),
        MorningBriefAnnouncer,
    )


@pytest.mark.asyncio
async def test_push_degraded_by_late_busy_is_retried_with_silent_speech():
    """扫描后才转忙时 TTS 被降级（push 返回 False），宽限内应改期重试而不是记成功。"""
    now = [local(8, 5)]
    service = FakeService()
    results = [False, True]
    pushes = []

    async def push(conn, **kwargs):
        pushes.append(kwargs)
        return results[len(pushes) - 1]

    announcer = MorningBriefAnnouncer(
        make_config(retry_seconds=60, busy_grace_seconds=300),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        push=push,
        busy_reason=lambda conn: None,
        now_provider=lambda: now[0],
    )

    await announcer.tick()
    assert len(pushes) == 1
    assert not pushes[0].get("silent")

    # 宽限内按 retry_seconds 改期，不立即重推
    now[0] = local(8, 5) + timedelta(seconds=5)
    await announcer.tick()
    assert len(pushes) == 1

    now[0] = local(8, 5) + timedelta(seconds=61)
    await announcer.tick()

    assert len(pushes) == 2
    assert pushes[1]["silent"] is True, "重推不该再响一次提示音"
    assert pushes[1]["speak"] is True
    assert service.calls == 1, "重推不该再扫一次飞书"

    now[0] = local(8, 5) + timedelta(seconds=130)
    await announcer.tick()
    assert len(pushes) == 2, "播报成功后不该再推"


@pytest.mark.asyncio
async def test_push_that_keeps_degrading_settles_for_screen_after_grace():
    now = [local(8, 5)]
    service = FakeService()
    pushes = []

    async def push(conn, **kwargs):
        pushes.append(kwargs)
        return False

    announcer = MorningBriefAnnouncer(
        make_config(retry_seconds=60, busy_grace_seconds=120),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        push=push,
        busy_reason=lambda conn: None,
        now_provider=lambda: now[0],
    )

    for seconds in (0, 61, 122, 190, 260):
        now[0] = local(8, 5) + timedelta(seconds=seconds)
        await announcer.tick()

    assert service.calls == 1
    assert 2 <= len(pushes) <= 3, "宽限用尽后接受只上屏，不该无限重推"
    assert len({id(p) for p in pushes}) == len(pushes)
    last_two = pushes[-2:]
    assert all(p["silent"] for p in last_two[1:]) or len(pushes) == 2


@pytest.mark.asyncio
async def test_conn_is_refetched_after_scan():
    """飞书扫描期间固件断线重连，推送必须落在新连接上。"""
    old_conn, new_conn = object(), object()
    devices = FakeDeviceRegistry({"robot": old_conn})
    now = [local(8, 5)]

    class SwappingService(FakeService):
        async def preview(self, report_date=None, limit=3):
            devices.devices["robot"] = new_conn
            return await super().preview(report_date, limit=limit)

    service = SwappingService()
    announcer, pushes = build(
        make_config(),
        FakePresenceRegistry(),
        devices,
        service,
        now,
    )

    await announcer.tick()

    assert len(pushes) == 1
    assert pushes[0][0] is new_conn


@pytest.mark.asyncio
async def test_conn_lost_during_scan_defers_and_reuses_the_report():
    devices = FakeDeviceRegistry({"robot": object()})
    now = [local(8, 5)]

    class DroppingService(FakeService):
        async def preview(self, report_date=None, limit=3):
            devices.devices.clear()
            return await super().preview(report_date, limit=limit)

    service = DroppingService()
    announcer, pushes = build(
        make_config(retry_seconds=60),
        FakePresenceRegistry(),
        devices,
        service,
        now,
    )

    await announcer.tick()
    assert pushes == []

    devices.devices["robot"] = object()
    now[0] = local(8, 5) + timedelta(seconds=61)
    await announcer.tick()

    assert len(pushes) == 1
    assert service.calls == 1, "设备回来后该复用已扫好的报告，不再扫一次"


@pytest.mark.asyncio
async def test_null_device_id_binding_still_auto_binds():
    """YAML 裸空 device_id 解析为 None，不该变成字符串 \"None\"。"""
    conn = object()
    now = [local(8, 5)]
    service = FakeService()
    announcer, pushes = build(
        make_config(bindings=[{"workstation_id": "desk", "device_id": None}]),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": conn}),
        service,
        now,
    )

    await announcer.tick()

    assert len(pushes) == 1
    assert pushes[0][0] is conn


def test_null_timezone_falls_back_to_default():
    config = {
        "morning_brief": {
            "enabled": True,
            "timezone": None,
            "announce": {"enabled": True},
        }
    }
    from core.morning_brief.announcer import AnnouncePolicy

    policy = AnnouncePolicy.from_config(config)
    assert policy.timezone_name == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_multi_device_log_points_at_binding_not_offline():
    now = [local(8, 5)]
    logger = RecordingLogger()
    service = FakeService()
    announcer = MorningBriefAnnouncer(
        make_config(bindings=[{"workstation_id": "desk", "device_id": ""}]),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"one": object(), "two": object()}),
        service,
        push=None,
        busy_reason=lambda conn: None,
        now_provider=lambda: now[0],
        logger=logger,
    )

    await announcer.tick()

    assert service.calls == 0
    joined = "".join(logger.lines)
    assert "不在线" not in joined
    assert "自动绑定" in joined or "device_id" in joined


@pytest.mark.asyncio
async def test_busy_anchor_resets_when_owner_leaves():
    """离席会打断「连续忙」的观察，返岗后必须重新计宽限，而不是拿旧锚点硬推。"""
    presence = FakePresenceRegistry()
    busy = ["设备正在播放语音"]
    now = [local(8, 0)]
    service = FakeService()
    announcer, pushes = build(
        make_config(busy_grace_seconds=120),
        presence,
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
        busy_reason=lambda conn: busy[0],
    )

    await announcer.tick()  # busy_since = 08:00
    assert pushes == []

    presence.snapshot = {"effective_state": "absent", "identity": {"state": "owner"}}
    now[0] = local(8, 1)
    await announcer.tick()

    presence.snapshot = OWNER_PRESENT
    now[0] = local(8, 4)  # 离席 3 分钟后回来，设备恰在播第二次迎接语
    await announcer.tick()
    assert pushes == [], "旧 busy_since 已超宽限，但新忙态才 0 秒，不该硬推"

    busy[0] = None
    now[0] = local(8, 4) + timedelta(seconds=5)
    await announcer.tick()
    assert len(pushes) == 1
    assert pushes[0][1]["speak"] is True


@pytest.mark.asyncio
async def test_all_failed_scan_is_not_announced_as_no_todos():
    """三路采集全失败时不能念「今天暂时没有待办」，该按扫描失败重试。"""
    now = [local(8, 0)]
    failed_report = {
        "coverage_status": "FAILED",
        "top_three": [],
        "other_unhandled_mentions": [],
        "calendar": [],
        "reauthorization_required": False,
        "permission_required": False,
    }
    service = FakeService(report=failed_report)
    announcer, pushes = build(
        make_config(retry_seconds=60, max_attempts=3),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
    )

    for minute in range(0, 10):
        now[0] = local(8, 0) + timedelta(minutes=minute)
        await announcer.tick()

    assert pushes == []
    assert service.calls == 3, "按 max_attempts 重试后放弃，而不是播假话或无限扫"


@pytest.mark.asyncio
async def test_unavailable_notice_is_sticky():
    """授权故障通知只上屏，不该被 restore_after 在 20 秒后收走。"""
    now = [local(8, 5)]
    service = FakeService(
        report={
            "coverage_status": "FAILED",
            "top_three": [],
            "other_unhandled_mentions": [],
            "calendar": [],
            "reauthorization_required": True,
            "permission_required": False,
        }
    )
    announcer, pushes = build(
        make_config(),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
    )

    await announcer.tick()

    assert len(pushes) == 1
    kwargs = pushes[0][1]
    assert kwargs["speak"] is False
    assert kwargs.get("restore_after") is None

    now[0] = local(8, 6)
    await announcer.tick()
    assert len(pushes) == 1, "故障通知每天最多一条"


@pytest.mark.asyncio
async def test_build_failures_hit_the_circuit_breaker():
    """报告形状异常导致编排抛错时，必须走熔断而不是每 5 秒扫一次飞书。"""

    class EvilReport(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("malformed report")

    now = [local(8, 0)]
    service = FakeService(report=EvilReport({"seed": 1}))
    announcer, pushes = build(
        make_config(retry_seconds=60, max_attempts=3),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
    )

    for minute in range(0, 10):
        now[0] = local(8, 0) + timedelta(minutes=minute)
        await announcer.tick()

    assert pushes == []
    assert service.calls == 3


@pytest.mark.asyncio
async def test_announced_marker_survives_restart():
    marker = {}
    now = [local(8, 5)]
    first = MarkedFakeService(marker)
    announcer, pushes = build(
        make_config(),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        first,
        now,
    )
    await announcer.tick()
    assert len(pushes) == 1

    # 模拟进程重启：全新 announcer 实例，共享持久化标记
    second = MarkedFakeService(marker)
    announcer2, pushes2 = build(
        make_config(),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        second,
        now,
    )
    now[0] = local(8, 20)
    await announcer2.tick()

    assert second.calls == 0, "重启后同一天不该再扫"
    assert pushes2 == []

    # 次日照常
    now[0] = local(8, 5, day=20)
    await announcer2.tick()
    assert second.calls == 1
    assert len(pushes2) == 1


@pytest.mark.asyncio
async def test_max_items_config_reaches_the_scan_limit():
    now = [local(8, 5)]
    service = FakeService()
    announcer, pushes = build(
        make_config(max_items=5),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
    )

    await announcer.tick()

    assert service.last_limit == 5


@pytest.mark.asyncio
async def test_unreported_workstation_is_skipped_without_error():
    now = [local(8, 5)]
    service = FakeService()
    announcer = MorningBriefAnnouncer(
        make_config(
            bindings=[
                {"workstation_id": "desk", "device_id": "robot"},
                {"workstation_id": "ghost", "device_id": "robot"},
            ]
        ),
        WorkstationPresenceRegistry({"desk": OWNER_PRESENT}),
        FakeDeviceRegistry({"robot": object()}),
        service,
        push=None,
        busy_reason=lambda conn: None,
        now_provider=lambda: now[0],
    )
    pushes = []

    async def push(conn, **kwargs):
        pushes.append(kwargs)
        return True

    announcer._push = push

    await announcer.tick()

    assert len(pushes) == 1, "有上报的工位照播，未上报的工位安静跳过"


@pytest.mark.asyncio
async def test_background_loop_actually_polls():
    """让出一次事件循环，证明 _run 真的跑了 tick 与 sleep。"""
    ticks = []
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0)

    service = FakeService()
    announcer = MorningBriefAnnouncer(
        make_config(),
        FakePresenceRegistry(),
        FakeDeviceRegistry({}),
        service,
        push=None,
        busy_reason=lambda conn: None,
        now_provider=lambda: local(8, 5),
        sleep=fake_sleep,
    )
    original_tick = announcer.tick

    async def counting_tick():
        ticks.append(1)
        await original_tick()

    announcer.tick = counting_tick

    await announcer.start()
    for _ in range(6):
        await asyncio.sleep(0)
    await announcer.stop()

    assert len(ticks) >= 1, "_run 循环体必须真的执行过"
    assert len(sleeps) >= 1
    assert sleeps[0] == announcer.policy.poll_seconds


def test_announcer_kwargs_match_real_push_work_event_signature():
    import inspect

    from core.handle.pushHandle import push_work_event

    sig = inspect.signature(push_work_event)
    sig.bind(
        object(),
        text="t",
        emotion="happy",
        status="早报",
        speak=True,
        restore_after=20.0,
        silent=False,
    )


def test_misconfigured_values_fall_back_with_warnings(caplog):
    import logging as std_logging

    from core.morning_brief.announcer import AnnouncePolicy

    config = {
        "morning_brief": {
            "enabled": True,
            "announce": {
                "enabled": True,
                "window_start": "08:00",
                "window_end": 600,  # 无引号 10:00 被 YAML 1.1 解析成的六十进制整数
                "workdays": ["1", "2", "3"],
                "max_attempts": True,
                "restore_after_seconds": 0,
            },
        }
    }
    with caplog.at_level(
        std_logging.WARNING, logger="core.morning_brief.announcer"
    ):
        policy = AnnouncePolicy.from_config(config)

    assert policy.window_end.strftime("%H:%M") == "09:30"
    assert policy.workdays == frozenset({1, 2, 3, 4, 5}), "全被过滤时回落默认而不是恒不播"
    assert policy.max_attempts == 3, "bool 不是合法数字"
    assert policy.restore_after_seconds == 0.0, "0 是合法取值：播完不收屏"
    assert len(caplog.records) >= 3, "每处静默回退都该有告警日志"


def test_enabled_flag_rejects_quoted_no(caplog):
    from core.morning_brief.announcer import AnnouncePolicy

    policy = AnnouncePolicy.from_config(
        {"morning_brief": {"enabled": True, "announce": {"enabled": "no"}}}
    )
    assert policy.enabled is False, '带引号的 "no" 也该按关闭理解'


def test_reversed_window_falls_back_with_warning(caplog):
    import logging as std_logging

    from core.morning_brief.announcer import AnnouncePolicy

    with caplog.at_level(
        std_logging.WARNING, logger="core.morning_brief.announcer"
    ):
        policy = AnnouncePolicy.from_config(
            {
                "morning_brief": {
                    "enabled": True,
                    "announce": {
                        "enabled": True,
                        "window_start": "09:30",
                        "window_end": "08:00",
                    },
                }
            }
        )

    assert policy.window_start.strftime("%H:%M") == "08:00"
    assert policy.window_end.strftime("%H:%M") == "09:30"
    assert caplog.records


@pytest.mark.asyncio
async def test_zero_restore_after_keeps_the_screen():
    now = [local(8, 5)]
    service = FakeService()
    announcer, pushes = build(
        make_config(restore_after_seconds=0),
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": object()}),
        service,
        now,
    )

    await announcer.tick()

    assert pushes[0][1].get("restore_after") is None


@pytest.mark.asyncio
async def test_background_loop_starts_and_stops_cleanly():
    announcer = MorningBriefAnnouncer(
        make_config(),
        FakePresenceRegistry(),
        FakeDeviceRegistry({}),
        FakeService(),
    )

    await announcer.start()
    task = announcer._task
    await announcer.start()
    assert announcer._task is task

    await announcer.stop()

    assert announcer._task is None
    assert task.cancelled()
