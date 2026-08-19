"""早上第一次认出主人时播报晨报的编排。"""

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


class FakePresenceRegistry:
    def __init__(self, snapshot=None):
        self.snapshot = (
            {"effective_state": "present", "identity": {"state": "owner"}}
            if snapshot is None
            else snapshot
        )

    def get(self, workstation_id):
        return self.snapshot


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

    async def preview(self, report_date=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.report


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
