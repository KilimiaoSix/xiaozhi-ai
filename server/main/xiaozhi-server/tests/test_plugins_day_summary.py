"""日终总结语音入口测试。

数据源全部走 register_summary_provider 注入假数据，不依赖 away_ledger /
incident_manager / owner_status 等并行任务的真实实现。
"""

import pytest

import plugins_func.functions.day_summary as day_summary
from plugins_func.register import Action


class FakeConn:
    pass


@pytest.fixture(autouse=True)
def _reset_providers():
    day_summary.reset_summary_providers()
    yield
    day_summary.reset_summary_providers()


def test_register_and_reset_summary_providers():
    day_summary.register_summary_provider("agent_events", lambda: [1, 2, 3])
    assert "agent_events" in day_summary.summary_providers

    day_summary.reset_summary_providers()

    assert day_summary.summary_providers == {}


def test_register_same_name_overwrites_previous_provider():
    day_summary.register_summary_provider("agent_events", lambda: [1])
    day_summary.register_summary_provider("agent_events", lambda: [1, 2])

    assert day_summary.summary_providers["agent_events"]() == [1, 2]


def test_all_empty_returns_fallback_reply():
    assert day_summary._compose_summary() == day_summary.FALLBACK_REPLY


def test_provider_exception_is_skipped_and_others_still_used():
    def boom():
        raise RuntimeError("away_ledger 挂了")

    day_summary.register_summary_provider("agent_events", boom)
    day_summary.register_summary_provider(
        "tomorrow_status", lambda: {"state": "leave"}
    )

    text = day_summary._compose_summary()

    assert "编码任务" not in text
    assert text == "明天你请假，重要留言我会替你收好。"


def test_unregistered_provider_is_treated_as_no_data():
    day_summary.register_summary_provider("tomorrow_status", lambda: {"state": "leave"})
    # agent_events / incidents 均未注册

    text = day_summary._compose_summary()

    assert text == "明天你请假，重要留言我会替你收好。"


@pytest.mark.parametrize(
    "value,expected",
    [
        ([1, 2, 3], 3),
        ({"count": 5}, 5),
        (7, 7),
        (True, 0),  # bool 不当整数计数，避免 True 被当成 1
        ("3", 0),  # 字符串不是约定形状，不猜测
        (None, 0),
    ],
)
def test_extract_count_supported_shapes(value, expected):
    assert day_summary._extract_count(value) == expected


def test_compose_summary_with_tasks_and_single_recovered_incident():
    day_summary.register_summary_provider("agent_events", lambda: [{}, {}, {}])
    day_summary.register_summary_provider(
        "incidents", lambda: [{"severity": "P1", "recovered": True}]
    )

    text = day_summary._compose_summary()

    assert text == "今天完成了3个编码任务，处理了一次线上告警（已恢复）。"


def test_compose_summary_with_unrecovered_incident():
    day_summary.register_summary_provider("agent_events", lambda: [{}])
    day_summary.register_summary_provider(
        "incidents", lambda: [{"severity": "P0", "recovered": False}]
    )

    text = day_summary._compose_summary()

    assert text == "今天完成了1个编码任务，处理了一次线上告警（处理中）。"


def test_compose_summary_with_multiple_incidents_reports_recovered_count():
    day_summary.register_summary_provider(
        "incidents",
        lambda: [
            {"severity": "P1", "recovered": True},
            {"severity": "P2", "recovered": False},
            {"severity": "P3", "recovered": True},
        ],
    )

    text = day_summary._compose_summary()

    assert text == "处理了3次线上告警，2次已恢复。"


def test_compose_summary_includes_leave_tomorrow_sentence():
    day_summary.register_summary_provider("agent_events", lambda: [{}, {}])
    day_summary.register_summary_provider(
        "tomorrow_status", lambda: {"state": "leave", "leave_start": "2026-08-20"}
    )

    text = day_summary._compose_summary()

    assert text == "今天完成了2个编码任务。明天你请假，重要留言我会替你收好。"


def test_tomorrow_status_available_produces_no_extra_sentence():
    day_summary.register_summary_provider("agent_events", lambda: [{}])
    day_summary.register_summary_provider(
        "tomorrow_status", lambda: {"state": "available"}
    )

    text = day_summary._compose_summary()

    assert text == "今天完成了1个编码任务。"


def test_zero_task_count_is_not_mentioned():
    day_summary.register_summary_provider("agent_events", lambda: [])
    day_summary.register_summary_provider(
        "incidents", lambda: [{"severity": "P1", "recovered": True}]
    )

    text = day_summary._compose_summary()

    assert "编码任务" not in text
    assert text == "处理了一次线上告警（已恢复）。"


@pytest.mark.asyncio
async def test_end_of_day_summary_returns_action_response():
    day_summary.register_summary_provider("agent_events", lambda: [{}, {}])

    response = await day_summary.end_of_day_summary(FakeConn())

    assert response.action == Action.RESPONSE
    assert response.response == "今天完成了2个编码任务。"
