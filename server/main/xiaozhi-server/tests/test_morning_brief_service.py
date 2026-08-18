import json
from datetime import datetime, timedelta, timezone

import pytest

from core.morning_brief.ledger import AttentionLedger
from core.morning_brief.models import AttentionItem
from core.morning_brief.service import MorningBriefService
from core.morning_brief.feishu_client import (
    AuthenticationRequired,
    CALENDAR_SCOPES,
    CollectionResult,
    MESSAGE_SCOPES,
    REQUIRED_SCOPES,
    FeishuApiError,
)


NOW = datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc)


def raw_message(
    message_id,
    topic_id,
    text,
    *,
    sender_id="ou_sender",
    mentions=None,
    timestamp=None,
):
    return {
        "message_id": message_id,
        "thread_id": topic_id,
        "chat_id": "oc_1",
        "chat_name": "研发协同群",
        "chat_type": "group",
        "sender": {"id": sender_id, "name": "张三"},
        "create_time": str(
            int((timestamp or (NOW - timedelta(minutes=30))).timestamp() * 1000)
        ),
        "content": json.dumps({"text": text}, ensure_ascii=False),
        "mentions": mentions or [],
    }


def raw_event(event_id, start, end, *, rsvp="accept", status="confirmed"):
    return {
        "event_id": event_id,
        "summary": f"日程 {event_id}",
        "start_time": {"timestamp": str(int(start.timestamp())), "timezone": "Asia/Shanghai"},
        "end_time": {"timestamp": str(int(end.timestamp())), "timezone": "Asia/Shanghai"},
        "self_rsvp_status": rsvp,
        "status": status,
    }


class FakeClient:
    def __init__(self, general=(), mentions=(), calendar=(), calendar_enabled=True):
        self.general = general
        self.mentions = mentions
        self.calendar = calendar
        self.calendar_enabled = calendar_enabled
        self.search_calls = []
        self.calendar_calls = []
        self.general_result = None
        self.mentions_result = None
        self.calendar_result = None
        self.general_error = None
        self.mentions_error = None
        self.calendar_error = None

    async def search_messages(self, start, end, *, at_chatter_ids=None):
        self.search_calls.append((start, end, at_chatter_ids))
        if at_chatter_ids:
            if self.mentions_error:
                raise self.mentions_error
            return self.mentions_result or CollectionResult(
                tuple(self.mentions), 1, True
            )
        if self.general_error:
            raise self.general_error
        return self.general_result or CollectionResult(tuple(self.general), 1, True)

    async def list_calendar_events(self, start, end):
        self.calendar_calls.append((start, end))
        if self.calendar_error:
            raise self.calendar_error
        return self.calendar_result or CollectionResult(tuple(self.calendar), 1, True)

    def required_scopes(self):
        if self.calendar_enabled:
            return MESSAGE_SCOPES + CALENDAR_SCOPES
        return MESSAGE_SCOPES

    def capabilities(self):
        return {
            "user_token_configured": True,
            "client_unread_cursor_supported": False,
            "calendar_enabled": self.calendar_enabled,
            "required_scopes": list(self.required_scopes()),
        }


def make_service(tmp_path, client, now=NOW):
    return MorningBriefService(
        client=client,
        ledger=AttentionLedger(tmp_path / "brief.db"),
        self_open_id="ou_me",
        now_provider=lambda: now,
    )


@pytest.mark.asyncio
async def test_complete_preview_uses_two_message_scans_and_deduplicates(tmp_path):
    direct = raw_message(
        "om_1",
        "topic_1",
        "线上故障，请今天处理？",
        mentions=[{"id": "ou_me"}],
    )
    request = raw_message("om_2", "topic_2", "请确认发布窗口")
    client = FakeClient(general=[direct, request], mentions=[direct])
    service = make_service(tmp_path, client)

    report = await service.preview()

    assert report["report_date"] == "2026-08-18"
    assert report["coverage_status"] == "COMPLETE"
    assert report["scan_window"] == {
        "start": "2026-08-17T18:00:00+08:00",
        "end": "2026-08-18T09:00:00+08:00",
    }
    assert len(client.search_calls) == 2
    assert client.search_calls[0][2] is None
    assert client.search_calls[1][2] == ["ou_me"]
    assert service.ledger.count_items() == 2
    assert service.ledger.get_watermark("messages") == NOW
    assert [item["item_id"] for item in report["top_three"]] == ["om_1", "om_2"]
    assert report["other_unhandled_mentions"] == []
    assert report["disclaimer"] == "待关注/未处理不等同于飞书客户端真实未读。"


@pytest.mark.asyncio
async def test_existing_watermark_uses_ten_minute_overlap(tmp_path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    watermark = NOW - timedelta(minutes=20)
    service.ledger.set_watermark("messages", watermark)

    await service.preview()

    assert client.search_calls[0][0] == watermark - timedelta(minutes=10)


@pytest.mark.asyncio
async def test_monday_initial_scan_starts_on_friday_evening(tmp_path):
    monday = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
    client = FakeClient()
    service = make_service(tmp_path, client, now=monday)

    report = await service.preview()

    assert report["scan_window"]["start"] == "2026-08-14T18:00:00+08:00"


@pytest.mark.asyncio
async def test_incomplete_pagination_marks_partial_and_does_not_advance_watermark(tmp_path):
    client = FakeClient()
    client.general_result = CollectionResult(
        (raw_message("om_1", "topic_1", "请确认"),),
        pages=2,
        complete=False,
        next_page_token="next",
        error="page limit reached before pagination completed",
    )
    service = make_service(tmp_path, client)

    report = await service.preview()

    assert report["coverage_status"] == "PARTIAL"
    assert report["coverage"][0]["status"] == "PARTIAL"
    assert report["coverage"][0]["next_page_token_present"] is True
    assert service.ledger.get_watermark("messages") is None


@pytest.mark.asyncio
async def test_report_persistence_failure_does_not_advance_watermark(tmp_path):
    client = FakeClient()
    service = make_service(tmp_path, client)

    def fail_save(report):
        raise RuntimeError("disk full")

    service.ledger.save_brief = fail_save

    with pytest.raises(RuntimeError, match="disk full"):
        await service.preview()

    assert service.ledger.get_watermark("messages") is None


@pytest.mark.asyncio
async def test_one_source_failure_is_visible_and_other_sources_survive(tmp_path):
    client = FakeClient(general=[raw_message("om_1", "topic_1", "请确认")])
    client.mentions_error = RuntimeError("mention endpoint unavailable")
    service = make_service(tmp_path, client)

    report = await service.preview()

    assert report["coverage_status"] == "PARTIAL"
    assert [source["status"] for source in report["coverage"]] == [
        "COMPLETE",
        "FAILED",
        "COMPLETE",
    ]
    assert report["coverage"][1]["error"] == "mention endpoint unavailable"
    assert report["top_three"][0]["item_id"] == "om_1"


@pytest.mark.asyncio
async def test_user_reply_closes_an_older_item_in_the_same_topic(tmp_path):
    client = FakeClient(
        general=[
            raw_message(
                "om_reply",
                "topic_1",
                "已处理",
                sender_id="ou_me",
                timestamp=NOW - timedelta(minutes=5),
            )
        ]
    )
    service = make_service(tmp_path, client)
    service.ledger.upsert_items(
        [
            AttentionItem(
                message_id="om_original",
                topic_id="topic_1",
                sender_id="ou_sender",
                sender_name="张三",
                chat_id="oc_1",
                chat_name="研发群",
                chat_type="group",
                mention_kind="DIRECT",
                short_excerpt="请确认",
                source_timestamp=NOW - timedelta(hours=1),
                source_url="https://applink.feishu.cn/client/chat/open?openChatId=oc_om_original",
            )
        ],
        NOW - timedelta(hours=1),
    )

    report = await service.preview()

    assert service.ledger.get_open_items() == []
    assert service.ledger.count_items() == 1
    assert report["top_three"] == []


@pytest.mark.asyncio
async def test_calendar_conflicts_are_symmetric_and_declined_events_are_ignored(tmp_path):
    client = FakeClient(
        calendar=[
            raw_event("event_1", NOW + timedelta(hours=1), NOW + timedelta(hours=2)),
            raw_event(
                "event_2",
                NOW + timedelta(hours=1, minutes=30),
                NOW + timedelta(hours=3),
                rsvp="tentative",
            ),
            raw_event(
                "event_declined",
                NOW + timedelta(hours=1),
                NOW + timedelta(hours=2),
                rsvp="decline",
            ),
        ]
    )
    service = make_service(tmp_path, client)

    report = await service.preview()

    assert [event["event_id"] for event in report["calendar"]] == [
        "event_1",
        "event_2",
    ]
    assert report["calendar"][0]["conflicts"] == ["event_2"]
    assert report["calendar"][1]["conflicts"] == ["event_1"]


@pytest.mark.asyncio
async def test_all_sources_auth_failure_returns_failed_diagnostic(tmp_path):
    client = FakeClient()
    error = AuthenticationRequired("user access token expired")
    client.general_error = error
    client.mentions_error = error
    client.calendar_error = error
    service = make_service(tmp_path, client)

    report = await service.preview()

    assert report["coverage_status"] == "FAILED"
    assert report["reauthorization_required"] is True
    assert report["top_three"] == []
    assert service.latest() == report


@pytest.mark.asyncio
async def test_permission_failure_is_visible_in_report_and_health(tmp_path):
    client = FakeClient()
    error = FeishuApiError(
        "/open-apis/search/v2/message",
        400,
        99991672,
        "Access denied. One of the following scopes is required: [search:message]",
    )
    client.general_error = error
    client.mentions_error = error
    client.calendar_error = error
    service = make_service(tmp_path, client)

    report = await service.preview()

    assert report["coverage_status"] == "FAILED"
    assert report["permission_required"] is True
    assert report["missing_scopes"] == list(REQUIRED_SCOPES)
    assert report["reauthorization_required"] is False
    assert service.health()["status"] == "PERMISSION_REQUIRED"


@pytest.mark.asyncio
async def test_disabled_calendar_is_skipped_without_degrading_coverage(tmp_path):
    client = FakeClient(
        general=[raw_message("om_1", "topic_1", "请确认发布窗口")],
        calendar=[
            raw_event("event_1", NOW + timedelta(hours=1), NOW + timedelta(hours=2))
        ],
        calendar_enabled=False,
    )
    service = make_service(tmp_path, client)

    report = await service.preview()

    assert client.calendar_calls == []
    calendar_coverage = next(
        row for row in report["coverage"] if row["source"] == "calendar"
    )
    assert calendar_coverage["status"] == "DISABLED"
    assert calendar_coverage["error"] is None
    assert report["calendar"] == []
    # 主动关闭的数据源不应把整体状态拖成 PARTIAL。
    assert report["coverage_status"] == "COMPLETE"
    assert [item["item_id"] for item in report["top_three"]] == ["om_1"]


@pytest.mark.asyncio
async def test_disabled_calendar_drops_calendar_scopes_from_permission_hint(tmp_path):
    client = FakeClient(calendar_enabled=False)
    error = FeishuApiError(
        "/open-apis/search/v2/message",
        400,
        99991672,
        "Access denied. One of the following scopes is required: [search:message]",
    )
    client.general_error = error
    client.mentions_error = error
    service = make_service(tmp_path, client)

    report = await service.preview()

    assert report["permission_required"] is True
    assert report["missing_scopes"] == list(MESSAGE_SCOPES)
    assert not set(report["missing_scopes"]) & set(CALENDAR_SCOPES)
    assert service.health()["capabilities"]["calendar_enabled"] is False
