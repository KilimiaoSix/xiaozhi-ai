from datetime import date, datetime, timezone

import pytest

from core.feishu_workspace.service import (
    FeishuWorkspaceService,
    FeishuWorkspaceUnavailable,
)
from core.morning_brief.feishu_client import CollectionResult, FeishuApiError


class FakeFeishuClient:
    def capabilities(self):
        return {"user_token_configured": True}

    async def list_tasks(self, *, completed=False):
        assert completed is False
        return CollectionResult(
            (
                {
                    "guid": "task-1",
                    "summary": "整理演示材料",
                    "due": {
                        "timestamp": "1787068800000",
                        "is_all_day": False,
                    },
                    "url": "https://applink.feishu.cn/task-1",
                    "tasklists": [{"tasklist_guid": "list-1"}],
                },
            ),
            1,
            True,
        )


    async def get_tasklist(self, tasklist_guid):
        assert tasklist_guid == "list-1"
        return {"guid": tasklist_guid, "name": "桌面精灵"}

    async def list_calendar_events(self, start, end):
        assert start.isoformat() == "2026-08-19T00:00:00+08:00"
        assert end.isoformat() == "2026-08-20T00:00:00+08:00"
        return CollectionResult(
            (
                {
                    "event_id": "event-1",
                    "summary": "产品周会",
                    "start_time": {
                        "timestamp": "1787097600",
                        "timezone": "Asia/Shanghai",
                    },
                    "end_time": {
                        "timestamp": "1787101200",
                        "timezone": "Asia/Shanghai",
                    },
                    "app_link": "https://applink.feishu.cn/event-1",
                },
            ),
            1,
            True,
        )


class CalendarPermissionDeniedClient(FakeFeishuClient):
    async def list_calendar_events(self, start, end):
        raise FeishuApiError(
            "/open-apis/calendar/v4/calendars/primary",
            403,
            99991672,
            "permission denied",
        )


class TasklistPermissionDeniedClient(FakeFeishuClient):
    async def get_tasklist(self, tasklist_guid):
        raise FeishuApiError(
            f"/open-apis/task/v2/tasklists/{tasklist_guid}",
            403,
            99991672,
            "permission denied",
        )


class AllSourcesFailingClient(FakeFeishuClient):
    async def list_tasks(self, *, completed=False):
        raise RuntimeError("task upstream unavailable")

    async def list_calendar_events(self, start, end):
        raise RuntimeError("calendar upstream unavailable")


@pytest.mark.asyncio
async def test_briefing_returns_today_meetings_and_open_tasks_with_tasklist_name():
    service = FeishuWorkspaceService(
        FakeFeishuClient(),
        self_open_id="ou_me",
        timezone_name="Asia/Shanghai",
        now_provider=lambda: datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc),
    )

    snapshot = await service.get_briefing(date(2026, 8, 19))

    assert snapshot["status"] == {
        "state": "ready",
        "message": "Server 已配置飞书用户凭据。",
        "open_id": "ou_me",
        "source": "server_openapi",
        "required_scopes": [
            "calendar:calendar:readonly",
            "task:task:read",
            "task:tasklist:read",
        ],
        "missing_scopes": [],
    }
    assert snapshot["date"] == "2026-08-19"
    assert snapshot["meetings"] == [
        {
            "id": "event-1",
            "title": "产品周会",
            "start": "2026-08-19T00:00:00+00:00",
            "end": "2026-08-19T01:00:00+00:00",
            "all_day": False,
            "url": "https://applink.feishu.cn/event-1",
        }
    ]
    assert snapshot["tasks"] == [
        {
            "id": "task-1",
            "title": "整理演示材料",
            "due": "2026-08-18T16:00:00+00:00",
            "all_day": False,
            "url": "https://applink.feishu.cn/task-1",
            "project_name": "桌面精灵",
        }
    ]
    assert snapshot["warnings"] == []


@pytest.mark.asyncio
async def test_calendar_permission_failure_keeps_tasks_and_reports_scope():
    service = FeishuWorkspaceService(
        CalendarPermissionDeniedClient(),
        self_open_id="ou_me",
        timezone_name="Asia/Shanghai",
    )

    snapshot = await service.get_briefing(date(2026, 8, 19))

    assert snapshot["meetings"] == []
    assert [task["id"] for task in snapshot["tasks"]] == ["task-1"]
    assert snapshot["warnings"] == [
        "今日日程缺少飞书权限：calendar:calendar:readonly"
    ]
    assert snapshot["status"]["missing_scopes"] == [
        "calendar:calendar:readonly"
    ]


@pytest.mark.asyncio
async def test_tasklist_permission_failure_keeps_tasks_without_project_name():
    service = FeishuWorkspaceService(
        TasklistPermissionDeniedClient(),
        self_open_id="ou_me",
        timezone_name="Asia/Shanghai",
    )

    snapshot = await service.get_briefing(date(2026, 8, 19))

    assert snapshot["tasks"][0]["project_name"] is None
    assert snapshot["warnings"] == [
        "任务清单名称缺少飞书权限：task:tasklist:read"
    ]
    assert snapshot["status"]["missing_scopes"] == ["task:tasklist:read"]


@pytest.mark.asyncio
async def test_briefing_fails_when_both_primary_sources_are_unavailable():
    service = FeishuWorkspaceService(
        AllSourcesFailingClient(),
        self_open_id="ou_me",
        timezone_name="Asia/Shanghai",
    )

    with pytest.raises(
        FeishuWorkspaceUnavailable,
        match="未完成任务读取失败.*今日日程读取失败",
    ):
        await service.get_briefing(date(2026, 8, 19))


class MalformedCalendarClient(FakeFeishuClient):
    """真实飞书里出现过的畸形条目：缺 event_id、start_time 不完整。"""

    async def list_calendar_events(self, start, end):
        return CollectionResult(
            (
                {"summary": "缺 event_id 的残留同步"},
                {
                    "event_id": "event-ok",
                    "summary": "产品周会",
                    "start_time": {
                        "timestamp": "1787097600",
                        "timezone": "Asia/Shanghai",
                    },
                    "end_time": {
                        "timestamp": "1787101200",
                        "timezone": "Asia/Shanghai",
                    },
                },
                {"event_id": "event-no-time", "summary": "没有时间的日程"},
            ),
            1,
            True,
        )


class NullTasklistsClient(FakeFeishuClient):
    async def list_tasks(self, *, completed=False):
        return CollectionResult(
            ({"guid": "task-1", "summary": "字段在但值是 null", "tasklists": None},),
            1,
            True,
        )


class TruncatedClient(FakeFeishuClient):
    async def list_tasks(self, *, completed=False):
        return CollectionResult(
            ({"guid": "task-1", "summary": "只拉到第一页"},),
            40,
            False,
            "next-page-token",
        )


class HugeTimestampClient(FakeFeishuClient):
    async def list_tasks(self, *, completed=False):
        return CollectionResult(
            (
                {
                    "guid": "task-1",
                    "summary": "截止时间是脏数据",
                    "due": {"timestamp": "99999999999999999999"},
                },
            ),
            1,
            True,
        )


@pytest.mark.asyncio
async def test_one_malformed_calendar_entry_does_not_sink_the_whole_briefing():
    service = FeishuWorkspaceService(
        MalformedCalendarClient(),
        self_open_id="ou_me",
        timezone_name="Asia/Shanghai",
        now_provider=lambda: datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc),
    )

    snapshot = await service.get_briefing(date(2026, 8, 19))

    assert [item["id"] for item in snapshot["meetings"]] == ["event-ok"]
    assert snapshot["tasks"], "坏日程不该连带丢掉已经拉到的任务"
    assert any("2 条日程" in warning for warning in snapshot["warnings"])


@pytest.mark.asyncio
async def test_tasklists_null_is_treated_as_no_tasklist():
    service = FeishuWorkspaceService(
        NullTasklistsClient(),
        self_open_id="ou_me",
        timezone_name="Asia/Shanghai",
        now_provider=lambda: datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc),
    )

    snapshot = await service.get_briefing(date(2026, 8, 19))

    assert snapshot["tasks"][0]["project_name"] is None


@pytest.mark.asyncio
async def test_truncated_collection_is_reported_as_a_warning():
    service = FeishuWorkspaceService(
        TruncatedClient(),
        self_open_id="ou_me",
        timezone_name="Asia/Shanghai",
        now_provider=lambda: datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc),
    )

    snapshot = await service.get_briefing(date(2026, 8, 19))

    assert any(
        "未完成任务" in w and "未取完" in w and "40 页" in w
        for w in snapshot["warnings"]
    )


@pytest.mark.asyncio
async def test_out_of_range_due_timestamp_degrades_instead_of_raising():
    service = FeishuWorkspaceService(
        HugeTimestampClient(),
        self_open_id="ou_me",
        timezone_name="Asia/Shanghai",
        now_provider=lambda: datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc),
    )

    snapshot = await service.get_briefing(date(2026, 8, 19))

    assert snapshot["tasks"][0]["due"] is None
