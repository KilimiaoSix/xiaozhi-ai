import json
from datetime import datetime, timezone

import pytest

from core.morning_brief.models import (
    ModelValidationError,
    normalize_calendar_event,
    normalize_message,
)


def raw_message(**overrides):
    payload = {
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "group",
        "chat_name": "发布协同群",
        "root_id": "om_root",
        "thread_id": "omt_topic",
        "create_time": "1787014800000",
        "sender": {
            "id": "ou_sender",
            "name": "张三",
            "sender_type": "user",
        },
        "body": {"content": json.dumps({"text": "请今天确认发布窗口"})},
        "mentions": [],
    }
    payload.update(overrides)
    return payload


def test_direct_mention_and_at_all_are_distinguished():
    direct = normalize_message(
        raw_message(mentions=[{"id": "ou_me", "name": "我"}]),
        "ou_me",
        240,
    )
    broadcast = normalize_message(
        raw_message(mentions=[{"id": "all", "name": "所有人"}]),
        "ou_me",
        240,
    )

    assert direct.mention_kind == "DIRECT"
    assert broadcast.mention_kind == "ALL"


def test_message_normalization_accepts_nested_ids_and_truncates_excerpt():
    item = normalize_message(
        raw_message(
            thread_id="",
            root_id="",
            sender={"sender_id": {"open_id": "ou_nested"}, "name": "李四"},
            mentions=[{"id": {"open_id": "ou_me"}, "key": "@_user_1"}],
            content=json.dumps({"text": "A" * 100}),
            body=None,
        ),
        "ou_me",
        12,
    )

    assert item.sender_id == "ou_nested"
    assert item.topic_id == "om_1"
    assert item.short_excerpt == "A" * 12
    assert item.source_timestamp == datetime(
        2026, 8, 18, 1, 0, tzinfo=timezone.utc
    )


def test_post_content_is_flattened_without_storing_raw_json():
    post = {
        "zh_cn": {
            "title": "变更评审",
            "content": [[{"tag": "text", "text": "请审批"}, {"tag": "a", "text": "详情"}]],
        }
    }

    item = normalize_message(
        raw_message(body={"content": json.dumps(post, ensure_ascii=False)}),
        "ou_me",
        240,
    )

    assert item.short_excerpt == "变更评审 请审批 详情"
    assert "zh_cn" not in item.short_excerpt


def test_message_without_identifier_is_rejected():
    with pytest.raises(ModelValidationError, match="message_id"):
        normalize_message(raw_message(message_id=""), "ou_me", 240)


def test_calendar_instance_normalizes_time_location_and_rsvp():
    event = normalize_calendar_event(
        {
            "event_id": "event_1_1787014800",
            "summary": "版本发布评审",
            "start_time": {
                "timestamp": "1787014800",
                "timezone": "Asia/Shanghai",
            },
            "end_time": {
                "timestamp": "1787018400",
                "timezone": "Asia/Shanghai",
            },
            "location": {"name": "A301", "address": "三楼"},
            "self_rsvp_status": "tentative",
            "event_organizer": {"display_name": "王五"},
            "vchat": {"meeting_url": "https://meeting.example/event_1"},
        },
        "Asia/Shanghai",
    )

    assert event.event_id == "event_1_1787014800"
    assert event.summary == "版本发布评审"
    assert event.start.isoformat() == "2026-08-18T09:00:00+08:00"
    assert event.end.isoformat() == "2026-08-18T10:00:00+08:00"
    assert event.location == "A301 三楼"
    assert event.rsvp_status == "tentative"
    assert event.organizer == "王五"
    assert event.source_url == "https://meeting.example/event_1"

