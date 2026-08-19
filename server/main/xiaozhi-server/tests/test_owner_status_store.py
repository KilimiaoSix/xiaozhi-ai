"""主人状态存储测试。

纯离线：落盘目录用 tmp_path，时钟全部注入，不依赖系统当前时间与真实文件
系统之外的任何状态。覆盖：set/get 往返、请假到期自动回落、meeting/away
overdue 判定、非法输入、损坏落盘文件的容错、并发写入安全冒烟。
"""

import json
import threading
from datetime import datetime, timedelta

import pytest

from core.owner_status import (
    DEFAULT_PUBLIC_NOTES,
    OwnerStatusStore,
    STATUS_AVAILABLE,
    STATUS_AWAY,
    STATUS_LEAVE,
    STATUS_MEETING,
    get_owner_status_store,
    reset_owner_status_store,
)


class FakeClock:
    """可推进的假时钟，注入到 store 里驱动到期/overdue 判定。"""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_owner_status_store()
    yield
    reset_owner_status_store()


def make_store(tmp_path, clock=None):
    persist_path = tmp_path / "owner_status.json"
    return OwnerStatusStore(
        persist_path, clock=clock or FakeClock(datetime(2026, 8, 19, 9, 0, 0))
    )


# ---------------------------------------------------------------- set/get


def test_default_state_is_available(tmp_path):
    store = make_store(tmp_path)

    status = store.get()

    assert status["state"] == STATUS_AVAILABLE
    assert status["overdue"] is False
    assert status["public_note"] == DEFAULT_PUBLIC_NOTES[STATUS_AVAILABLE]


def test_set_meeting_persists_and_reload_sees_it(tmp_path):
    clock = FakeClock(datetime(2026, 8, 19, 9, 0, 0))
    persist_path = tmp_path / "owner_status.json"
    store = OwnerStatusStore(persist_path, clock=clock)

    result = store.set_status(
        STATUS_MEETING, expected_return="2026-08-19T11:30:00", public_note="开会"
    )

    assert result["state"] == STATUS_MEETING
    assert result["expected_return"] == "2026-08-19T11:30:00"
    assert result["public_note"] == "开会"
    assert result["set_at"] == "2026-08-19T09:00:00"

    # 新实例读同一份落盘文件，验证真的写下去了，不是只改了内存
    reloaded = OwnerStatusStore(persist_path, clock=clock)
    assert reloaded.get()["state"] == STATUS_MEETING
    assert reloaded.get()["expected_return"] == "2026-08-19T11:30:00"


def test_set_status_without_public_note_uses_default(tmp_path):
    store = make_store(tmp_path)

    result = store.set_status(STATUS_AWAY, expected_return="2026-08-19T10:00:00")

    assert result["public_note"] == DEFAULT_PUBLIC_NOTES[STATUS_AWAY]


def test_clear_restores_available(tmp_path):
    store = make_store(tmp_path)
    store.set_status(STATUS_AWAY, expected_return="2026-08-19T10:00:00")

    result = store.clear()

    assert result["state"] == STATUS_AVAILABLE
    assert result["expected_return"] is None


# ---------------------------------------------------------------- 请假到期回落


def test_leave_stays_active_within_range(tmp_path):
    clock = FakeClock(datetime(2026, 8, 19, 9, 0, 0))
    store = make_store(tmp_path, clock)
    store.set_status(STATUS_LEAVE, leave_start="2026-08-19", leave_end="2026-08-20")

    clock.advance(days=1)  # 仍在 leave_end 当天

    assert store.get()["state"] == STATUS_LEAVE


def test_leave_rolls_back_to_available_after_end_date(tmp_path):
    clock = FakeClock(datetime(2026, 8, 19, 9, 0, 0))
    store = make_store(tmp_path, clock)
    store.set_status(STATUS_LEAVE, leave_start="2026-08-19", leave_end="2026-08-19")

    clock.advance(days=1)  # 过了 leave_end 次日
    status = store.get()

    assert status["state"] == STATUS_AVAILABLE
    assert status["leave_start"] is None
    assert status["leave_end"] is None


def test_leave_end_defaults_to_start(tmp_path):
    store = make_store(tmp_path)

    result = store.set_status(STATUS_LEAVE, leave_start="2026-08-20")

    assert result["leave_start"] == "2026-08-20"
    assert result["leave_end"] == "2026-08-20"


# ---------------------------------------------------------------- overdue


def test_meeting_not_overdue_before_expected_return(tmp_path):
    clock = FakeClock(datetime(2026, 8, 19, 9, 0, 0))
    store = make_store(tmp_path, clock)
    store.set_status(STATUS_MEETING, expected_return="2026-08-19T11:30:00")

    clock.advance(hours=1)

    assert store.get()["overdue"] is False


def test_meeting_overdue_after_expected_return(tmp_path):
    clock = FakeClock(datetime(2026, 8, 19, 9, 0, 0))
    store = make_store(tmp_path, clock)
    store.set_status(STATUS_MEETING, expected_return="2026-08-19T11:30:00")

    clock.advance(hours=2, minutes=31)
    status = store.get()

    assert status["overdue"] is True
    # overdue 只是派生标记，不改变状态本身
    assert status["state"] == STATUS_MEETING


def test_away_without_expected_return_is_never_overdue(tmp_path):
    clock = FakeClock(datetime(2026, 8, 19, 9, 0, 0))
    store = make_store(tmp_path, clock)
    store.set_status(STATUS_AWAY)

    clock.advance(days=1)

    assert store.get()["overdue"] is False


def test_available_state_is_never_overdue(tmp_path):
    store = make_store(tmp_path)

    assert store.get()["overdue"] is False


def test_leave_state_is_never_overdue(tmp_path):
    clock = FakeClock(datetime(2026, 8, 19, 9, 0, 0))
    store = make_store(tmp_path, clock)
    store.set_status(STATUS_LEAVE, leave_start="2026-08-19")

    assert store.get()["overdue"] is False


# ---------------------------------------------------------------- 非法输入


def test_unknown_state_raises_value_error(tmp_path):
    store = make_store(tmp_path)

    with pytest.raises(ValueError):
        store.set_status("napping")


def test_invalid_expected_return_raises_value_error(tmp_path):
    store = make_store(tmp_path)

    with pytest.raises(ValueError):
        store.set_status(STATUS_MEETING, expected_return="not-a-time")


def test_leave_without_start_raises_value_error(tmp_path):
    store = make_store(tmp_path)

    with pytest.raises(ValueError):
        store.set_status(STATUS_LEAVE)


def test_leave_end_before_start_raises_value_error(tmp_path):
    store = make_store(tmp_path)

    with pytest.raises(ValueError):
        store.set_status(
            STATUS_LEAVE, leave_start="2026-08-20", leave_end="2026-08-19"
        )


# ---------------------------------------------------------------- 损坏落盘文件容错


def test_missing_persist_file_falls_back_to_available(tmp_path):
    persist_path = tmp_path / "does_not_exist" / "owner_status.json"

    store = OwnerStatusStore(persist_path)

    assert store.get()["state"] == STATUS_AVAILABLE


def test_corrupted_json_falls_back_to_available(tmp_path):
    persist_path = tmp_path / "owner_status.json"
    persist_path.write_text("{not valid json", encoding="utf-8")

    store = OwnerStatusStore(persist_path)

    assert store.get()["state"] == STATUS_AVAILABLE


def test_persist_file_with_invalid_state_falls_back_to_available(tmp_path):
    persist_path = tmp_path / "owner_status.json"
    persist_path.write_text(json.dumps({"state": "napping"}), encoding="utf-8")

    store = OwnerStatusStore(persist_path)

    assert store.get()["state"] == STATUS_AVAILABLE


def test_recovers_and_can_still_write_after_corrupted_load(tmp_path):
    persist_path = tmp_path / "owner_status.json"
    persist_path.write_text("garbage", encoding="utf-8")
    store = OwnerStatusStore(persist_path)

    result = store.set_status(STATUS_AWAY, expected_return="2026-08-19T10:00:00")

    assert result["state"] == STATUS_AWAY
    assert json.loads(persist_path.read_text(encoding="utf-8"))["state"] == STATUS_AWAY


# ---------------------------------------------------------------- 并发安全冒烟


def test_concurrent_writes_do_not_crash_or_corrupt(tmp_path):
    store = make_store(tmp_path)
    errors = []

    def worker(i):
        try:
            if i % 2 == 0:
                store.set_status(STATUS_MEETING, expected_return="2026-08-19T23:59:00")
            else:
                store.clear()
        except Exception as e:  # pragma: no cover - 出现即测试失败
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    final = store.get()
    assert final["state"] in (STATUS_MEETING, STATUS_AVAILABLE)
    # 落盘文件本身也得是合法 JSON，没有被交错写坏
    on_disk = json.loads((tmp_path / "owner_status.json").read_text(encoding="utf-8"))
    assert on_disk["state"] in (STATUS_MEETING, STATUS_AVAILABLE)


# ---------------------------------------------------------------- 进程级单例


def test_get_owner_status_store_returns_singleton(tmp_path):
    config = {"owner_status": {"persist_path": str(tmp_path / "owner_status.json")}}

    store1 = get_owner_status_store(config)
    store2 = get_owner_status_store(config)

    assert store1 is store2


def test_reset_owner_status_store_clears_singleton(tmp_path):
    config = {"owner_status": {"persist_path": str(tmp_path / "owner_status.json")}}
    store1 = get_owner_status_store(config)

    reset_owner_status_store()
    store2 = get_owner_status_store(config)

    assert store1 is not store2
