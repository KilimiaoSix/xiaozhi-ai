"""每日关注项的本地 SQLite 台账。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from core.morning_brief.models import AttentionItem


OPEN_STATUSES = ("OPEN_NEW", "OPEN_CARRIED")


class AttentionLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        excerpt_chars: int = 240,
        excerpt_retention_days: int = 7,
        resolved_retention_days: int = 30,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.excerpt_chars = max(1, int(excerpt_chars))
        self.excerpt_retention_days = max(1, int(excerpt_retention_days))
        self.resolved_retention_days = max(1, int(resolved_retention_days))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS attention_items (
                    source_message_id TEXT PRIMARY KEY,
                    topic_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    chat_name TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    mention_kind TEXT NOT NULL,
                    source_timestamp TEXT NOT NULL,
                    status TEXT NOT NULL,
                    score INTEGER NOT NULL DEFAULT 0,
                    short_excerpt TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    user_replied INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_attention_topic
                    ON attention_items(topic_id, source_timestamp);
                CREATE INDEX IF NOT EXISTS idx_attention_status
                    ON attention_items(status, last_seen_at);

                CREATE TABLE IF NOT EXISTS watermarks (
                    source TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS briefs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_date TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS announcements (
                    workstation_id TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    announced_at TEXT NOT NULL,
                    PRIMARY KEY (workstation_id, report_date)
                );
                """
            )

    @staticmethod
    def _date(value: str) -> object:
        return datetime.fromisoformat(value).date()

    @staticmethod
    def _to_item(row: sqlite3.Row) -> AttentionItem:
        return AttentionItem(
            message_id=row["source_message_id"],
            topic_id=row["topic_id"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            chat_id=row["chat_id"],
            chat_name=row["chat_name"],
            chat_type=row["chat_type"],
            mention_kind=row["mention_kind"],
            short_excerpt=row["short_excerpt"],
            source_timestamp=datetime.fromisoformat(row["source_timestamp"]),
            source_url=row["source_url"],
            status=row["status"],
            user_replied=bool(row["user_replied"]),
        )

    def upsert_items(
        self, items: list[AttentionItem], seen_at: datetime
    ) -> list[AttentionItem]:
        seen_text = seen_at.isoformat()
        stored: list[AttentionItem] = []
        with self._connect() as connection:
            for item in items:
                existing = connection.execute(
                    "SELECT * FROM attention_items WHERE source_message_id = ?",
                    (item.message_id,),
                ).fetchone()
                excerpt = item.short_excerpt[: self.excerpt_chars]
                if existing is None:
                    status = "OPEN_NEW"
                    user_replied = False
                    connection.execute(
                        """
                        INSERT INTO attention_items (
                            source_message_id, topic_id, sender_id, sender_name,
                            chat_id, chat_name, chat_type, mention_kind,
                            source_timestamp, status, short_excerpt, source_url,
                            first_seen_at, last_seen_at, user_replied
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item.message_id,
                            item.topic_id,
                            item.sender_id,
                            item.sender_name,
                            item.chat_id,
                            item.chat_name,
                            item.chat_type,
                            item.mention_kind,
                            item.source_timestamp.isoformat(),
                            status,
                            excerpt,
                            item.source_url,
                            seen_text,
                            seen_text,
                            0,
                        ),
                    )
                else:
                    status = existing["status"]
                    if status in OPEN_STATUSES and self._date(
                        existing["first_seen_at"]
                    ) < seen_at.date():
                        status = "OPEN_CARRIED"
                    user_replied = bool(existing["user_replied"])
                    connection.execute(
                        """
                        UPDATE attention_items
                        SET topic_id = ?, sender_id = ?, sender_name = ?,
                            chat_id = ?, chat_name = ?, chat_type = ?,
                            mention_kind = ?, source_timestamp = ?, status = ?,
                            short_excerpt = ?, source_url = ?, last_seen_at = ?
                        WHERE source_message_id = ?
                        """,
                        (
                            item.topic_id,
                            item.sender_id,
                            item.sender_name,
                            item.chat_id,
                            item.chat_name,
                            item.chat_type,
                            item.mention_kind,
                            item.source_timestamp.isoformat(),
                            status,
                            excerpt,
                            item.source_url,
                            seen_text,
                            item.message_id,
                        ),
                    )
                stored.append(
                    replace(
                        item,
                        short_excerpt=excerpt,
                        status=status,
                        user_replied=user_replied,
                    )
                )
        return stored

    def mark_replied(self, topic_id: str, replied_at: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE attention_items
                SET status = 'DONE', user_replied = 1, last_seen_at = ?
                WHERE topic_id = ? AND status IN ('OPEN_NEW', 'OPEN_CARRIED')
                """,
                (replied_at.isoformat(), topic_id),
            )
            return cursor.rowcount

    def get_open_items(self) -> list[AttentionItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM attention_items
                WHERE status IN ('OPEN_NEW', 'OPEN_CARRIED')
                ORDER BY source_timestamp DESC, source_message_id
                """
            ).fetchall()
        return [self._to_item(row) for row in rows]

    def count_items(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM attention_items"
            ).fetchone()
        return int(row["count"])

    def set_watermark(self, source: str, value: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO watermarks(source, value) VALUES (?, ?)
                ON CONFLICT(source) DO UPDATE SET value = excluded.value
                """,
                (source, value.isoformat()),
            )

    def get_watermark(self, source: str) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM watermarks WHERE source = ?", (source,)
            ).fetchone()
        return datetime.fromisoformat(row["value"]) if row else None

    def save_brief(self, report: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO briefs(report_date, generated_at, payload_json)
                VALUES (?, ?, ?)
                """,
                (
                    str(report["report_date"]),
                    str(report["generated_at"]),
                    json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def latest_brief(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM briefs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def mark_announced(self, workstation_id: str, report_date) -> None:
        """记下「这个工位今天已经播过晨报」，进程重启后仍然算数。"""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO announcements(
                    workstation_id, report_date, announced_at
                ) VALUES (?, ?, ?)
                """,
                (
                    str(workstation_id),
                    str(report_date),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def was_announced(self, workstation_id: str, report_date) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM announcements"
                " WHERE workstation_id = ? AND report_date = ?",
                (str(workstation_id), str(report_date)),
            ).fetchone()
        return row is not None

    def purge(self, now: datetime) -> dict[str, int]:
        resolved_before = (now - timedelta(days=self.resolved_retention_days)).isoformat()
        excerpt_before = (now - timedelta(days=self.excerpt_retention_days)).isoformat()
        with self._connect() as connection:
            briefs_deleted = connection.execute(
                "DELETE FROM briefs WHERE generated_at < ?",
                (excerpt_before,),
            ).rowcount
            deleted = connection.execute(
                """
                DELETE FROM attention_items
                WHERE status IN ('DONE', 'DISMISSED') AND last_seen_at < ?
                """,
                (resolved_before,),
            ).rowcount
            cleared = connection.execute(
                """
                UPDATE attention_items SET short_excerpt = ''
                WHERE last_seen_at < ? AND short_excerpt != ''
                """,
                (excerpt_before,),
            ).rowcount
        return {
            "excerpts_cleared": cleared,
            "resolved_deleted": deleted,
            "briefs_deleted": briefs_deleted,
        }
