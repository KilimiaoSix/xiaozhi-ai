from dataclasses import replace
from datetime import datetime, timedelta, timezone

from core.morning_brief.models import AttentionItem, CalendarItem
from core.morning_brief.ranking import rank_candidates, score_attention_item


NOW = datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc)


def message(
    message_id,
    topic_id,
    text,
    *,
    mention_kind="NONE",
    chat_type="group",
    status="OPEN_NEW",
    user_replied=False,
):
    return AttentionItem(
        message_id=message_id,
        topic_id=topic_id,
        sender_id="ou_sender",
        sender_name="张三",
        chat_id="oc_1",
        chat_name="研发群",
        chat_type=chat_type,
        mention_kind=mention_kind,
        short_excerpt=text,
        source_timestamp=NOW,
        source_url=f"https://applink.feishu.cn/client/chat/open?openChatId=oc_{message_id}",
        status=status,
        user_replied=user_replied,
    )


def test_score_uses_approved_weights_and_explains_reasons():
    item = message(
        "om_1",
        "incident",
        "线上故障阻塞，请今天处理？",
        mention_kind="DIRECT",
    )

    scored = score_attention_item(item)

    assert scored.score == 190
    assert set(scored.reasons) >= {
        "direct_mention",
        "request_or_question",
        "production_incident",
        "deadline_today",
    }


def test_at_all_does_not_receive_direct_mention_priority():
    direct = score_attention_item(
        message("om_1", "one", "请确认", mention_kind="DIRECT")
    )
    broadcast = score_attention_item(
        message("om_2", "two", "请确认", mention_kind="ALL")
    )

    assert direct.score - broadcast.score == 90


def test_p2p_request_and_general_request_weights_are_additive():
    scored = score_attention_item(
        message(
            "om_1",
            "one",
            "请确认？",
            mention_kind="DIRECT",
            chat_type="p2p",
        )
    )

    assert scored.score == 190
    assert scored.reasons == (
        "direct_mention",
        "p2p_request",
        "request_or_question",
    )
    assert scored.confidence == "HIGH"


def test_replied_and_informational_items_are_demoted():
    replied = score_attention_item(
        message(
            "om_1",
            "one",
            "通知：版本已经发布",
            mention_kind="DIRECT",
            user_replied=True,
        )
    )

    assert replied.score == -10
    assert replied.reasons[-2:] == ("user_replied", "informational")


def test_top_three_prefers_direct_mentions_and_distinct_topics():
    items = [
        message("om_1", "incident", "线上故障阻塞，请处理？", mention_kind="DIRECT"),
        message("om_2", "incident", "生产异常，请处理？", mention_kind="DIRECT"),
        message("om_3", "approval", "请审批变更", mention_kind="DIRECT"),
        message("om_4", "release", "请确认发布窗口"),
    ]

    result = rank_candidates(items, calendar_items=[], limit=3)

    assert [item.topic_id for item in result] == ["incident", "approval", "release"]
    assert result[0].score >= result[1].score >= result[2].score


def test_multiple_messages_on_one_topic_receive_topic_signal():
    result = rank_candidates(
        [
            message("om_1", "same", "请确认"),
            message("om_2", "same", "补充：请审批"),
        ],
        calendar_items=[],
        limit=2,
    )

    assert all(item.score >= 50 for item in result)
    assert all("multiple_messages" in item.reasons for item in result)


def test_calendar_conflict_and_pending_rsvp_can_enter_top_three():
    calendar = CalendarItem(
        event_id="event_1",
        summary="架构评审",
        start=NOW + timedelta(hours=1),
        end=NOW + timedelta(hours=2),
        timezone="Asia/Shanghai",
        location="A301",
        rsvp_status="needs_action",
        organizer="李四",
        source_url="https://calendar.example/event_1",
        conflicts=("event_2",),
    )

    result = rank_candidates([], [calendar], limit=3)

    assert len(result) == 1
    assert result[0].kind == "CALENDAR"
    assert result[0].score == 45
    assert result[0].reasons == ("calendar_attention", "pending_rsvp", "conflict")
    assert result[0].confidence == "MEDIUM"
