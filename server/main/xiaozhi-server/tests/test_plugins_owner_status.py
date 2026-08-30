"""主人状态语音函数测试。

fake conn 带 config/device_id（同 tests/test_pomodoro_handler.py 的 FakeConn 风格），
覆盖参数归一化（"HH:MM" → 今天/明天、"today"/"tomorrow" → 具体日期）与三个
语音函数的回复文案分支。全部离线：store 落盘到 tmp_path，_now() 用 monkeypatch
钉死，不依赖真实系统时间、真实 LLM 或设备。
"""

from datetime import datetime, timedelta

import pytest

import core.owner_status as core_owner_status
import plugins_func.functions.owner_status as owner_status_module
from core.owner_status import (
    STATUS_AVAILABLE,
    STATUS_AWAY,
    STATUS_LEAVE,
    STATUS_MEETING,
    reset_owner_status_store,
)
from plugins_func.functions.owner_status import (
    clear_owner_status,
    query_owner_status,
    set_owner_status,
)

FIXED_NOW = datetime(2026, 8, 19, 9, 0, 0)  # 周三 09:00


class FakeConn:
    """只带语音函数用得到的两个属性：config（落盘路径）与 device_id。"""

    def __init__(self, persist_path):
        self.device_id = "dc:da:0c:26:9a:60"
        self.config = {"owner_status": {"persist_path": str(persist_path)}}


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_owner_status_store()
    yield
    reset_owner_status_store()


@pytest.fixture
def fixed_now(monkeypatch):
    """冻结插件模块的 _now，并让 get_owner_status_store 拿到同一个冻结时钟。

    单独 patch _now 只覆盖了参数归一化（"today"/"tomorrow"/"HH:MM"）；
    store 自身请假到期的惰性回落判定用的是它自己的 clock（真实
    datetime.now），两个时钟不一致时，写死在过去的请假日期一到真实
    系统时间就会被 get() 冲回 available（日期炸弹）。这里让
    get_owner_status_store 在创建单例时也注入同一个冻结 clock，
    使二者读到的"现在"永远一致。
    """
    monkeypatch.setattr(owner_status_module, "_now", lambda: FIXED_NOW)
    real_get_store = core_owner_status.get_owner_status_store
    monkeypatch.setattr(
        owner_status_module,
        "get_owner_status_store",
        lambda config=None: real_get_store(config, clock=lambda: FIXED_NOW),
    )
    return FIXED_NOW


def make_conn(tmp_path):
    return FakeConn(tmp_path / "owner_status.json")


def read_status(conn):
    return owner_status_module.get_owner_status_store(conn.config).get()


# ---------------------------------------------------------------- expected_return 归一化


@pytest.mark.asyncio
async def test_meeting_hhmm_still_ahead_today_stays_today(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    response = await set_owner_status(conn, STATUS_MEETING, expected_return="11:30")

    assert response.result == "ok"
    assert read_status(conn)["expected_return"] == "2026-08-19T11:30:00"


@pytest.mark.asyncio
async def test_meeting_hhmm_already_past_rolls_to_tomorrow(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    await set_owner_status(conn, STATUS_MEETING, expected_return="08:00")

    assert read_status(conn)["expected_return"] == "2026-08-20T08:00:00"


@pytest.mark.asyncio
async def test_meeting_hhmm_equal_to_now_rolls_to_tomorrow(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    await set_owner_status(conn, STATUS_MEETING, expected_return="09:00")

    assert read_status(conn)["expected_return"] == "2026-08-20T09:00:00"


@pytest.mark.asyncio
async def test_away_accepts_full_iso_expected_return(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    await set_owner_status(conn, STATUS_AWAY, expected_return="2026-08-19T09:15:00")

    status = read_status(conn)
    assert status["state"] == STATUS_AWAY
    assert status["expected_return"] == "2026-08-19T09:15:00"


@pytest.mark.asyncio
async def test_away_without_expected_return_is_allowed(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    response = await set_owner_status(conn, STATUS_AWAY)

    assert response.result == "ok"
    assert read_status(conn)["expected_return"] is None


@pytest.mark.asyncio
async def test_invalid_expected_return_returns_friendly_error_and_no_write(
    tmp_path, fixed_now
):
    conn = make_conn(tmp_path)

    response = await set_owner_status(conn, STATUS_MEETING, expected_return="午饭后")

    assert response.result == "invalid_input"
    assert read_status(conn)["state"] == STATUS_AVAILABLE


# ---------------------------------------------------------------- leave_start/leave_end 归一化


@pytest.mark.asyncio
async def test_leave_tomorrow_resolves_to_tomorrows_date(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    response = await set_owner_status(conn, STATUS_LEAVE, leave_start="tomorrow")

    status = read_status(conn)
    assert status["leave_start"] == "2026-08-20"
    assert status["leave_end"] == "2026-08-20"
    assert "明天" in response.response


@pytest.mark.asyncio
async def test_leave_today_resolves_to_todays_date(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    response = await set_owner_status(conn, STATUS_LEAVE, leave_start="today")

    status = read_status(conn)
    assert status["leave_start"] == "2026-08-19"
    assert "今天" in response.response


@pytest.mark.asyncio
async def test_leave_without_leave_start_defaults_to_today(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    response = await set_owner_status(conn, STATUS_LEAVE)

    assert response.result == "ok"
    assert read_status(conn)["leave_start"] == "2026-08-19"


@pytest.mark.asyncio
async def test_leave_accepts_explicit_date_range(tmp_path, fixed_now):
    conn = make_conn(tmp_path)
    # 相对冻结时钟推导，而非写死绝对日期，避免用例随真实系统时间推移过期。
    leave_start = (fixed_now.date() + timedelta(days=2)).isoformat()
    leave_end = (fixed_now.date() + timedelta(days=3)).isoformat()

    response = await set_owner_status(
        conn, STATUS_LEAVE, leave_start=leave_start, leave_end=leave_end
    )

    status = read_status(conn)
    assert status["leave_start"] == leave_start
    assert status["leave_end"] == leave_end
    assert response.result == "ok"


@pytest.mark.asyncio
async def test_invalid_leave_date_returns_friendly_error_and_no_write(
    tmp_path, fixed_now
):
    conn = make_conn(tmp_path)

    response = await set_owner_status(conn, STATUS_LEAVE, leave_start="下周")

    assert response.result == "invalid_input"
    assert read_status(conn)["state"] == STATUS_AVAILABLE


# ---------------------------------------------------------------- 回复文案分支


@pytest.mark.asyncio
async def test_meeting_confirmation_text(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    response = await set_owner_status(conn, STATUS_MEETING, expected_return="11:30")

    assert response.response == "知道了，我帮你看着工位"


@pytest.mark.asyncio
async def test_away_confirmation_text(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    response = await set_owner_status(conn, STATUS_AWAY)

    assert response.response == "知道了，我帮你看着工位"


@pytest.mark.asyncio
async def test_leave_single_day_confirmation_text(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    response = await set_owner_status(conn, STATUS_LEAVE, leave_start="tomorrow")

    assert response.response == "记住了，明天的重要留言我替你收好"


@pytest.mark.asyncio
async def test_leave_range_confirmation_text(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    response = await set_owner_status(
        conn, STATUS_LEAVE, leave_start="today", leave_end="tomorrow"
    )

    assert response.response == "记住了，今天到明天请假期间的重要留言我替你收好"


# ---------------------------------------------------------------- 查询


@pytest.mark.asyncio
async def test_query_reports_default_available(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    response = await query_owner_status(conn)

    assert "在工位" in response.response


@pytest.mark.asyncio
async def test_query_reports_meeting_with_return_time(tmp_path, fixed_now):
    conn = make_conn(tmp_path)
    await set_owner_status(conn, STATUS_MEETING, expected_return="11:30")

    response = await query_owner_status(conn)

    assert "开会" in response.response
    assert "11:30" in response.response


@pytest.mark.asyncio
async def test_query_reports_overdue_meeting(tmp_path, fixed_now):
    conn = make_conn(tmp_path)
    # store 的 overdue 判定现在也走 fixed_now 注入的冻结 clock（见 fixture），
    # 这里仍用一个绝对早于 FIXED_NOW 的 ISO 时间，保证必然过期、测试确定性。
    await set_owner_status(conn, STATUS_MEETING, expected_return="2020-01-01T00:00:00")

    response = await query_owner_status(conn)

    assert "过了预计时间" in response.response


@pytest.mark.asyncio
async def test_query_reports_leave_range(tmp_path, fixed_now):
    conn = make_conn(tmp_path)
    # 相对冻结时钟推导，而非写死绝对日期，避免用例随真实系统时间推移过期。
    leave_start = (fixed_now.date() + timedelta(days=2)).isoformat()
    leave_end = (fixed_now.date() + timedelta(days=3)).isoformat()
    await set_owner_status(
        conn, STATUS_LEAVE, leave_start=leave_start, leave_end=leave_end
    )

    response = await query_owner_status(conn)

    assert "请假" in response.response


# ---------------------------------------------------------------- 取消/回来


@pytest.mark.asyncio
async def test_clear_restores_available_state_and_reply(tmp_path, fixed_now):
    conn = make_conn(tmp_path)
    await set_owner_status(conn, STATUS_MEETING, expected_return="11:30")

    response = await clear_owner_status(conn)

    assert response.response == "好，状态已恢复"
    assert read_status(conn)["state"] == STATUS_AVAILABLE


@pytest.mark.asyncio
async def test_clear_on_already_available_is_a_noop_reply(tmp_path, fixed_now):
    conn = make_conn(tmp_path)

    response = await clear_owner_status(conn)

    assert response.response == "好，状态已恢复"
    assert read_status(conn)["state"] == STATUS_AVAILABLE
