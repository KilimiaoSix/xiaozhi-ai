"""主人状态到期提醒测试。

全部离线：store 用真实实现但注入假时钟，推送/设备解析/presence 查询全部
注入假实现（同时覆盖同步与协程两种写法），不依赖真实时间流逝、真机或
presence 服务。
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from core.owner_status import OwnerStatusStore, STATUS_MEETING
from core.owner_status_reminder import OwnerStatusReminder, REMINDER_TEXT

DEVICE_ID = "dc:da:0c:26:9a:60"
CONN = object()


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class Recorder:
    """假 push_event：记录每次调用的参数，不真的碰网络。"""

    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, conn, text, **kwargs):
        self.calls.append({"conn": conn, "text": text, **kwargs})
        return True


def make_store(tmp_path, clock):
    return OwnerStatusStore(tmp_path / "owner_status.json", clock=clock)


def async_resolver(result=(DEVICE_ID, CONN)):
    async def resolver():
        return result

    return resolver


def make_overdue_store(tmp_path, minutes_overdue=31):
    """构造一个已经过了 expected_return 的 meeting 状态。"""
    clock = FakeClock(datetime(2026, 8, 19, 9, 0, 0))
    store = make_store(tmp_path, clock)
    store.set_status(STATUS_MEETING, expected_return="2026-08-19T09:30:00")
    clock.advance(minutes=minutes_overdue)
    return store, clock


@pytest.mark.asyncio
async def test_check_once_reminds_when_overdue(tmp_path):
    store, _ = make_overdue_store(tmp_path)
    push = Recorder()
    reminder = OwnerStatusReminder(store, async_resolver(), push_event=push)

    fired = await reminder.check_once()

    assert fired is True
    assert len(push.calls) == 1
    assert push.calls[0]["text"] == REMINDER_TEXT
    assert push.calls[0]["speak"] is True
    assert push.calls[0]["conn"] is CONN


@pytest.mark.asyncio
async def test_check_once_does_not_remind_before_expected_return(tmp_path):
    clock = FakeClock(datetime(2026, 8, 19, 9, 0, 0))
    store = make_store(tmp_path, clock)
    store.set_status(STATUS_MEETING, expected_return="2026-08-19T09:30:00")
    push = Recorder()
    reminder = OwnerStatusReminder(store, async_resolver(), push_event=push)

    fired = await reminder.check_once()

    assert fired is False
    assert push.calls == []


@pytest.mark.asyncio
async def test_check_once_does_not_repeat_for_same_set_at(tmp_path):
    store, _ = make_overdue_store(tmp_path)
    push = Recorder()
    reminder = OwnerStatusReminder(store, async_resolver(), push_event=push)

    first = await reminder.check_once()
    second = await reminder.check_once()
    third = await reminder.check_once()

    assert (first, second, third) == (True, False, False)
    assert len(push.calls) == 1


@pytest.mark.asyncio
async def test_presence_absent_suppresses_reminder(tmp_path):
    store, _ = make_overdue_store(tmp_path)
    push = Recorder()
    reminder = OwnerStatusReminder(
        store, async_resolver(), push_event=push, presence_lookup=lambda: "absent"
    )

    fired = await reminder.check_once()

    assert fired is False
    assert push.calls == []


@pytest.mark.asyncio
async def test_presence_present_allows_reminder(tmp_path):
    store, _ = make_overdue_store(tmp_path)
    push = Recorder()

    async def presence_lookup():
        return "present"

    reminder = OwnerStatusReminder(
        store, async_resolver(), push_event=push, presence_lookup=presence_lookup
    )

    fired = await reminder.check_once()

    assert fired is True


@pytest.mark.asyncio
async def test_presence_lookup_returning_none_allows_reminder(tmp_path):
    store, _ = make_overdue_store(tmp_path)
    push = Recorder()
    reminder = OwnerStatusReminder(
        store, async_resolver(), push_event=push, presence_lookup=lambda: None
    )

    fired = await reminder.check_once()

    assert fired is True


@pytest.mark.asyncio
async def test_missing_presence_lookup_allows_reminder(tmp_path):
    store, _ = make_overdue_store(tmp_path)
    push = Recorder()
    reminder = OwnerStatusReminder(store, async_resolver(), push_event=push)

    fired = await reminder.check_once()

    assert fired is True


@pytest.mark.asyncio
async def test_supports_sync_device_resolver(tmp_path):
    store, _ = make_overdue_store(tmp_path)
    push = Recorder()
    reminder = OwnerStatusReminder(
        store, lambda: (DEVICE_ID, CONN), push_event=push
    )

    fired = await reminder.check_once()

    assert fired is True


@pytest.mark.asyncio
async def test_no_device_resolved_skips_reminder(tmp_path):
    store, _ = make_overdue_store(tmp_path)
    push = Recorder()
    reminder = OwnerStatusReminder(store, async_resolver(None), push_event=push)

    fired = await reminder.check_once()

    assert fired is False
    assert push.calls == []


@pytest.mark.asyncio
async def test_status_change_rearms_reminder(tmp_path):
    clock = FakeClock(datetime(2026, 8, 19, 9, 0, 0))
    store = make_store(tmp_path, clock)
    store.set_status(STATUS_MEETING, expected_return="2026-08-19T09:30:00")
    clock.advance(minutes=31)

    push = Recorder()
    reminder = OwnerStatusReminder(store, async_resolver(), push_event=push)

    first = await reminder.check_once()
    assert first is True

    # 主人回话说"还在开会，晚点回来"，改了新的预计返回时间——set_at 变了，
    # 之前"已经提醒过"的记录不该再拦住新一轮提醒。
    clock.advance(minutes=-20)  # 9:31 -> 9:11
    store.set_status(STATUS_MEETING, expected_return="2026-08-19T09:40:00")
    clock.advance(minutes=30)  # 9:11 -> 9:41，过了新的 expected_return

    second = await reminder.check_once()

    assert second is True
    assert len(push.calls) == 2


@pytest.mark.asyncio
async def test_push_failure_does_not_arm_dedup_and_can_retry(tmp_path):
    store, _ = make_overdue_store(tmp_path)

    async def failing_push(conn, text, **kwargs):
        raise RuntimeError("设备离线")

    reminder = OwnerStatusReminder(store, async_resolver(), push_event=failing_push)

    first = await reminder.check_once()
    assert first is False

    push = Recorder()
    reminder._push_event = push
    second = await reminder.check_once()

    assert second is True
    assert len(push.calls) == 1


@pytest.mark.asyncio
async def test_start_stop_runs_check_and_cancels_cleanly(tmp_path):
    store, _ = make_overdue_store(tmp_path)
    push = Recorder()
    release = asyncio.Event()

    async def fake_sleep(seconds):
        release.set()
        # 挂起直到测试主动 stop()，模拟"周期检查之间一直在等"
        await asyncio.sleep(3600)

    reminder = OwnerStatusReminder(
        store,
        async_resolver(),
        push_event=push,
        sleep=fake_sleep,
        check_interval_s=5,
    )

    reminder.start()
    await asyncio.wait_for(release.wait(), timeout=1.0)

    # 循环里 check_once() 排在 sleep() 之前，release 被 set 时说明第一次检查已跑完
    assert len(push.calls) == 1

    await reminder.stop()  # 不应抛异常，且能在超时前完成


@pytest.mark.asyncio
async def test_start_is_idempotent_while_running(tmp_path):
    store, _ = make_overdue_store(tmp_path)
    push = Recorder()

    async def fake_sleep(seconds):
        await asyncio.sleep(3600)

    reminder = OwnerStatusReminder(
        store, async_resolver(), push_event=push, sleep=fake_sleep
    )

    reminder.start()
    task_after_first_start = reminder._task
    reminder.start()  # 已经在跑，不该起第二条循环

    assert reminder._task is task_after_first_start

    await reminder.stop()
