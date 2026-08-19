from datetime import datetime, timedelta, timezone
import sqlite3

from core.morning_brief.ledger import AttentionLedger
from core.morning_brief.models import AttentionItem


NOW = datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc)


def item(message_id="om_1", topic_id="omt_1", text="请确认发布窗口"):
    return AttentionItem(
        message_id=message_id,
        topic_id=topic_id,
        sender_id="ou_sender",
        sender_name="张三",
        chat_id="oc_1",
        chat_name="发布群",
        chat_type="group",
        mention_kind="DIRECT",
        short_excerpt=text,
        source_timestamp=NOW,
        source_url=f"https://applink.feishu.cn/client/chat/open?openChatId=oc_{message_id}",
    )


def test_repeated_message_is_deduplicated_and_carried(tmp_path):
    ledger = AttentionLedger(tmp_path / "brief.db")

    first = ledger.upsert_items([item()], NOW)
    repeated = ledger.upsert_items([item()], NOW + timedelta(hours=1))
    carried = ledger.upsert_items([item()], NOW + timedelta(days=1))

    assert first[0].status == "OPEN_NEW"
    assert repeated[0].status == "OPEN_NEW"
    assert carried[0].status == "OPEN_CARRIED"
    assert ledger.count_items() == 1
    assert ledger.get_open_items()[0].status == "OPEN_CARRIED"


def test_non_open_status_is_not_reopened_by_a_rescan(tmp_path):
    ledger = AttentionLedger(tmp_path / "brief.db")
    ledger.upsert_items([item()], NOW)
    assert ledger.mark_replied("omt_1", NOW + timedelta(minutes=10)) == 1

    rescanned = ledger.upsert_items([item()], NOW + timedelta(days=1))

    assert rescanned[0].status == "DONE"
    assert rescanned[0].user_replied is True
    assert ledger.get_open_items() == []


def test_ledger_schema_and_storage_never_contain_full_message_body(tmp_path):
    path = tmp_path / "brief.db"
    ledger = AttentionLedger(path, excerpt_chars=12)
    secret_tail = "不应落库的完整消息"

    stored = ledger.upsert_items(
        [item(text="A" * 20 + secret_tail)],
        NOW,
    )[0]

    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(attention_items)")
        }
        row = connection.execute(
            "SELECT short_excerpt FROM attention_items WHERE source_message_id = ?",
            ("om_1",),
        ).fetchone()

    assert stored.short_excerpt == "A" * 12
    assert row[0] == "A" * 12
    assert columns.isdisjoint({"body", "content", "raw_content", "full_message"})
    assert secret_tail.encode("utf-8") not in path.read_bytes()


def test_watermark_and_latest_brief_round_trip(tmp_path):
    ledger = AttentionLedger(tmp_path / "brief.db")
    report = {
        "report_date": "2026-08-18",
        "generated_at": NOW.isoformat(),
        "coverage_status": "COMPLETE",
        "top_three": [],
    }

    assert ledger.get_watermark("messages") is None
    ledger.set_watermark("messages", NOW)
    ledger.save_brief(report)

    assert ledger.get_watermark("messages") == NOW
    assert ledger.latest_brief() == report


def test_purge_clears_old_excerpts_and_removes_old_resolved_items(tmp_path):
    ledger = AttentionLedger(
        tmp_path / "brief.db",
        excerpt_retention_days=7,
        resolved_retention_days=30,
    )
    ledger.upsert_items([item("om_open", "topic_open")], NOW)
    ledger.upsert_items([item("om_done", "topic_done")], NOW)
    ledger.mark_replied("topic_done", NOW + timedelta(minutes=1))
    ledger.save_brief(
        {
            "report_date": "2026-08-18",
            "generated_at": NOW.isoformat(),
            "top_three": [{"title": "敏感短摘要"}],
        }
    )

    result = ledger.purge(NOW + timedelta(days=31))

    assert result == {
        "excerpts_cleared": 1,
        "resolved_deleted": 1,
        "briefs_deleted": 1,
    }
    assert ledger.count_items() == 1
    assert ledger.get_open_items()[0].short_excerpt == ""
    assert ledger.latest_brief() is None


def test_announce_marker_round_trip(tmp_path):
    """播报标记要落库：进程重启后同一天不再重播。"""
    from datetime import date

    ledger = AttentionLedger(tmp_path / "ledger.sqlite3")
    day = date(2026, 8, 19)

    assert ledger.was_announced("desk", day) is False

    ledger.mark_announced("desk", day)
    assert ledger.was_announced("desk", day) is True
    # 幂等：重复标记不炸
    ledger.mark_announced("desk", day)

    assert ledger.was_announced("desk", date(2026, 8, 20)) is False
    assert ledger.was_announced("other", day) is False

    # 模拟重启：同一路径新实例仍读得到
    reopened = AttentionLedger(tmp_path / "ledger.sqlite3")
    assert reopened.was_announced("desk", day) is True
