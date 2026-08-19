"""定时提示调度器测试。

全部离线：注入假时钟/假 sleep/假 push_event/假 device_resolver/假
presence_lookup，不依赖真实设备连接、飞书或系统时钟。
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from core.timed_prompts import TimedPromptScheduler, create_timed_prompt_scheduler


WORKSTATION = "desk-1"
DEVICE_ID = "dc:da:0c:26:9a:60"
CONN = object()


def make_config(*, enabled=True, prompts=None, workstations=None):
    return {
        "timed_prompts": {
            "enabled": enabled,
            "prompts": (
                prompts
                if prompts is not None
                else [{"time": "10:30", "text": "该喝水啦"}]
            ),
        },
        "presence_robot": {
            "workstations": (
                workstations if workstations is not None else {WORKSTATION: DEVICE_ID}
            )
        },
    }


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, conn, text, **kwargs):
        self.calls.append({"conn": conn, "text": text, **kwargs})
        return True

    @property
    def texts(self):
        return [call["text"] for call in self.calls]


def make_scheduler(config, *, clock, presence="present", device=DEVICE_ID, **kwargs):
    push = Recorder()
    presence_map = {WORKSTATION: presence} if presence is not None else {}

    def presence_lookup(workstation_id):
        return presence_map.get(workstation_id)

    def device_resolver(device_id):
        return CONN if device_id == device else None

    scheduler = TimedPromptScheduler(
        config,
        push_event=push,
        device_resolver=device_resolver,
        presence_lookup=presence_lookup,
        clock=clock,
        **kwargs,
    )
    return scheduler, push


@pytest.mark.asyncio
async def test_tick_fires_when_present_at_scheduled_time():
    clock = FakeClock(datetime(2026, 8, 19, 10, 30))
    scheduler, push = make_scheduler(make_config(), clock=clock, presence="present")

    await scheduler.tick()

    assert push.texts == ["该喝水啦"]
    assert push.calls[0]["conn"] is CONN
    assert push.calls[0]["emotion"] == "happy"
    assert push.calls[0]["speak"] is True


@pytest.mark.asyncio
async def test_tick_does_not_fire_outside_the_scheduled_minute():
    clock = FakeClock(datetime(2026, 8, 19, 10, 29))
    scheduler, push = make_scheduler(make_config(), clock=clock, presence="present")

    await scheduler.tick()

    assert push.calls == []


@pytest.mark.asyncio
async def test_absent_workstation_is_skipped():
    clock = FakeClock(datetime(2026, 8, 19, 10, 30))
    scheduler, push = make_scheduler(make_config(), clock=clock, presence="absent")

    await scheduler.tick()

    assert push.calls == []


@pytest.mark.asyncio
async def test_no_presence_report_is_treated_as_not_present():
    clock = FakeClock(datetime(2026, 8, 19, 10, 30))
    scheduler, push = make_scheduler(make_config(), clock=clock, presence=None)

    await scheduler.tick()

    assert push.calls == []


@pytest.mark.asyncio
async def test_offline_device_is_skipped_even_when_present():
    clock = FakeClock(datetime(2026, 8, 19, 10, 30))
    scheduler, push = make_scheduler(
        make_config(), clock=clock, presence="present", device="some-other-device"
    )

    await scheduler.tick()

    assert push.calls == []


@pytest.mark.asyncio
async def test_same_prompt_does_not_repeat_within_the_same_day():
    clock = FakeClock(datetime(2026, 8, 19, 10, 30))
    scheduler, push = make_scheduler(make_config(), clock=clock, presence="present")

    await scheduler.tick()
    await scheduler.tick()  # 同一分钟内再对一次表

    assert len(push.calls) == 1


@pytest.mark.asyncio
async def test_prompt_rearms_on_the_next_day():
    clock = FakeClock(datetime(2026, 8, 19, 10, 30))
    scheduler, push = make_scheduler(make_config(), clock=clock, presence="present")

    await scheduler.tick()
    clock.now = datetime(2026, 8, 20, 10, 30)
    await scheduler.tick()

    assert len(push.calls) == 2


@pytest.mark.asyncio
async def test_days_filter_skips_non_matching_weekday_and_fires_on_matching_one():
    # 2026-08-19 是周三(isoweekday=3)，2026-08-22 是周六(isoweekday=6)
    prompts = [{"time": "10:30", "text": "该站起来走走了", "days": [6, 7]}]
    clock = FakeClock(datetime(2026, 8, 19, 10, 30))
    scheduler, push = make_scheduler(
        make_config(prompts=prompts), clock=clock, presence="present"
    )

    await scheduler.tick()
    assert push.calls == []

    clock.now = datetime(2026, 8, 22, 10, 30)
    await scheduler.tick()
    assert push.texts == ["该站起来走走了"]


@pytest.mark.asyncio
async def test_days_filter_accepts_weekday_name_aliases():
    prompts = [{"time": "10:30", "text": "喝水", "days": ["sat", "sun"]}]
    clock = FakeClock(datetime(2026, 8, 22, 10, 30))  # 周六
    scheduler, push = make_scheduler(
        make_config(prompts=prompts), clock=clock, presence="present"
    )

    await scheduler.tick()

    assert push.texts == ["喝水"]


def test_disabled_scheduler_does_not_start():
    clock = FakeClock(datetime(2026, 8, 19, 10, 30))
    scheduler, push = make_scheduler(
        make_config(enabled=False), clock=clock, presence="present"
    )

    scheduler.start()

    assert scheduler.enabled is False
    assert scheduler._task is None
    scheduler.stop()  # 不应该抛异常


def test_scheduler_without_workstation_mapping_is_disabled():
    clock = FakeClock(datetime(2026, 8, 19, 10, 30))
    scheduler, push = make_scheduler(
        make_config(workstations={}), clock=clock, presence="present"
    )

    assert scheduler.enabled is False


def test_scheduler_without_valid_prompts_is_disabled():
    clock = FakeClock(datetime(2026, 8, 19, 10, 30))
    scheduler, push = make_scheduler(
        make_config(prompts=[{"text": "缺 time 字段"}]), clock=clock, presence="present"
    )

    assert scheduler.enabled is False


def test_create_timed_prompt_scheduler_returns_none_when_disabled():
    assert create_timed_prompt_scheduler(make_config(enabled=False)) is None


def test_create_timed_prompt_scheduler_builds_instance_when_enabled():
    scheduler = create_timed_prompt_scheduler(make_config())

    assert isinstance(scheduler, TimedPromptScheduler)
    assert scheduler.enabled is True


@pytest.mark.asyncio
async def test_start_runs_ticks_in_background_and_stop_cancels_cleanly():
    clock = FakeClock(datetime(2026, 8, 19, 10, 29))
    push = Recorder()
    presence_map = {WORKSTATION: "present"}
    fired = asyncio.Event()

    async def fake_sleep(seconds):
        clock.now = clock.now + timedelta(seconds=seconds)
        if push.calls:
            fired.set()
        await asyncio.sleep(0)

    scheduler = TimedPromptScheduler(
        make_config(),
        push_event=push,
        device_resolver=lambda device_id: CONN if device_id == DEVICE_ID else None,
        presence_lookup=lambda workstation_id: presence_map.get(workstation_id),
        clock=clock,
        sleep=fake_sleep,
        tick_seconds=60.0,
    )

    scheduler.start()
    task = scheduler._task
    assert task is not None

    await asyncio.wait_for(fired.wait(), timeout=2)
    scheduler.stop()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert push.texts == ["该喝水啦"]
