"""每日关注晨报的采集、归一化、持久化和排序编排。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from core.morning_brief.ledger import AttentionLedger
from core.morning_brief.models import (
    AttentionItem,
    CalendarItem,
    ModelValidationError,
    normalize_calendar_event,
    normalize_message,
    resolve_timezone,
)
from core.morning_brief.ranking import RankedItem, rank_candidates
from core.morning_brief.feishu_client import (
    AuthenticationRequired,
    CollectionResult,
    FeishuClient,
    FeishuApiError,
)


class MorningBriefService:
    def __init__(
        self,
        client: FeishuClient,
        ledger: AttentionLedger,
        self_open_id: str,
        *,
        timezone_name: str = "Asia/Shanghai",
        overlap_minutes: int = 10,
        excerpt_chars: int = 240,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.ledger = ledger
        self.self_open_id = self_open_id
        self.timezone_name = timezone_name
        self.timezone = resolve_timezone(timezone_name)
        self.overlap = timedelta(minutes=max(0, int(overlap_minutes)))
        self.excerpt_chars = max(1, int(excerpt_chars))
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        # 日历是可选数据源：私有部署可能不发放日历读权限。
        self.calendar_enabled = bool(getattr(client, "calendar_enabled", True))

    @staticmethod
    def _previous_workday(value: date) -> date:
        current = value - timedelta(days=1)
        while current.weekday() >= 5:
            current -= timedelta(days=1)
        return current

    def _scan_start(self, report_date: date) -> datetime:
        watermark = self.ledger.get_watermark("messages")
        if watermark is not None:
            return watermark - self.overlap
        previous = self._previous_workday(report_date)
        return datetime.combine(previous, time(18, 0), tzinfo=self.timezone)

    @staticmethod
    def _coverage(source: str, result: Any) -> dict[str, Any]:
        if isinstance(result, BaseException):
            return {
                "source": source,
                "status": "FAILED",
                "pages": 0,
                "item_count": 0,
                "next_page_token_present": False,
                "error": str(result)[:500],
            }
        status = "COMPLETE" if result.complete else (
            "PARTIAL" if result.items else "FAILED"
        )
        return {
            "source": source,
            "status": status,
            "pages": result.pages,
            "item_count": len(result.items),
            "next_page_token_present": bool(result.next_page_token),
            "error": result.error or None,
        }

    @staticmethod
    def _disabled_coverage(source: str) -> dict[str, Any]:
        return {
            "source": source,
            "status": "DISABLED",
            "pages": 0,
            "item_count": 0,
            "next_page_token_present": False,
            "error": None,
        }

    @staticmethod
    def _overall_status(coverage: list[dict[str, Any]]) -> str:
        # 主动关闭的数据源不参与整体评价，否则永远停在 PARTIAL。
        statuses = [row["status"] for row in coverage if row["status"] != "DISABLED"]
        if statuses and all(status == "COMPLETE" for status in statuses):
            return "COMPLETE"
        if statuses and all(status == "FAILED" for status in statuses):
            return "FAILED"
        return "PARTIAL"

    @staticmethod
    def _ranked_dict(item: RankedItem) -> dict[str, Any]:
        result = asdict(item)
        result["reasons"] = list(item.reasons)
        return result

    @staticmethod
    def _attention_dict(item: AttentionItem) -> dict[str, Any]:
        result = asdict(item)
        result["source_timestamp"] = item.source_timestamp.isoformat()
        return result

    @staticmethod
    def _calendar_dict(item: CalendarItem) -> dict[str, Any]:
        result = asdict(item)
        result["start"] = item.start.isoformat()
        result["end"] = item.end.isoformat()
        result["conflicts"] = list(item.conflicts)
        return result

    @staticmethod
    def _mark_partial(
        coverage: list[dict[str, Any]], source: str, message: str
    ) -> None:
        for row in coverage:
            if row["source"] == source:
                row["status"] = "PARTIAL" if row["item_count"] else "FAILED"
                existing = row.get("error")
                row["error"] = f"{existing}; {message}" if existing else message
                return

    def _normalize_messages(
        self,
        result: Any,
        source: str,
        coverage: list[dict[str, Any]],
    ) -> list[AttentionItem]:
        if isinstance(result, BaseException):
            return []
        normalized: list[AttentionItem] = []
        failures = 0
        for raw in result.items:
            try:
                normalized.append(
                    normalize_message(raw, self.self_open_id, self.excerpt_chars)
                )
            except ModelValidationError:
                failures += 1
        if failures:
            self._mark_partial(
                coverage,
                source,
                f"{failures} message(s) could not be normalized",
            )
        return normalized

    def _normalize_calendar(
        self,
        result: Any,
        coverage: list[dict[str, Any]],
    ) -> list[CalendarItem]:
        if isinstance(result, BaseException):
            return []
        items: list[CalendarItem] = []
        failures = 0
        for raw in result.items:
            if str(raw.get("status", "")).lower() == "cancelled":
                continue
            if str(raw.get("self_rsvp_status", "")).lower() in {
                "decline",
                "removed",
            }:
                continue
            try:
                items.append(normalize_calendar_event(raw, self.timezone_name))
            except ModelValidationError:
                failures += 1
        if failures:
            self._mark_partial(
                coverage,
                "calendar",
                f"{failures} calendar event(s) could not be normalized",
            )
        return self._detect_conflicts(items)

    @staticmethod
    def _detect_conflicts(items: list[CalendarItem]) -> list[CalendarItem]:
        ordered = sorted(items, key=lambda item: (item.start, item.end, item.event_id))
        conflicts: dict[str, set[str]] = {item.event_id: set() for item in ordered}
        for index, current in enumerate(ordered):
            for other in ordered[index + 1 :]:
                if other.start >= current.end:
                    break
                if current.start < other.end and other.start < current.end:
                    conflicts[current.event_id].add(other.event_id)
                    conflicts[other.event_id].add(current.event_id)
        return [
            replace(item, conflicts=tuple(sorted(conflicts[item.event_id])))
            for item in ordered
        ]

    @staticmethod
    def _deduplicate(items: list[AttentionItem]) -> list[AttentionItem]:
        by_id: dict[str, AttentionItem] = {}
        for item in items:
            existing = by_id.get(item.message_id)
            if existing is None:
                by_id[item.message_id] = item
                continue
            if existing.mention_kind != "DIRECT" and item.mention_kind == "DIRECT":
                by_id[item.message_id] = item
            elif len(item.short_excerpt) > len(existing.short_excerpt):
                by_id[item.message_id] = item
        return sorted(
            by_id.values(),
            key=lambda item: (item.source_timestamp, item.message_id),
        )

    def _apply_replies(
        self,
        replies: list[AttentionItem],
    ) -> None:
        open_by_topic: dict[str, list[AttentionItem]] = {}
        for item in self.ledger.get_open_items():
            open_by_topic.setdefault(item.topic_id, []).append(item)
        for reply in sorted(replies, key=lambda item: item.source_timestamp):
            originals = open_by_topic.get(reply.topic_id, [])
            if any(
                reply.source_timestamp > original.source_timestamp
                for original in originals
            ):
                self.ledger.mark_replied(reply.topic_id, reply.source_timestamp)

    async def preview(self, report_date: date | None = None) -> dict[str, Any]:
        now = self.now_provider()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now_local = now.astimezone(self.timezone)
        current_date = report_date or now_local.date()
        scan_start = self._scan_start(current_date)
        scan_end = now_local
        day_start = datetime.combine(current_date, time.min, tzinfo=self.timezone)
        day_end = day_start + timedelta(days=1)

        collectors = [
            self.client.search_messages(scan_start, scan_end),
            self.client.search_messages(
                scan_start,
                scan_end,
                at_chatter_ids=[self.self_open_id],
            ),
        ]
        if self.calendar_enabled:
            collectors.append(self.client.list_calendar_events(day_start, day_end))

        results = await asyncio.gather(*collectors, return_exceptions=True)
        general, mentions = results[0], results[1]
        calendar_result = results[2] if self.calendar_enabled else None
        coverage = [
            self._coverage("messages", general),
            self._coverage("mentions", mentions),
            self._coverage("calendar", calendar_result)
            if self.calendar_enabled
            else self._disabled_coverage("calendar"),
        ]

        normalized_general = self._normalize_messages(
            general, "messages", coverage
        )
        normalized_mentions = self._normalize_messages(
            mentions, "mentions", coverage
        )
        all_messages = self._deduplicate(
            normalized_general + normalized_mentions
        )
        external_items = [
            item for item in all_messages if item.sender_id != self.self_open_id
        ]
        own_replies = [
            item for item in all_messages if item.sender_id == self.self_open_id
        ]
        self.ledger.upsert_items(external_items, now)
        self._apply_replies(own_replies)

        calendar = (
            self._normalize_calendar(calendar_result, coverage)
            if self.calendar_enabled
            else []
        )
        open_items = self.ledger.get_open_items()
        ranked = rank_candidates(open_items, calendar, limit=3)
        top_ids = {item.item_id for item in ranked if item.kind == "MESSAGE"}
        other_mentions = [
            item
            for item in open_items
            if item.mention_kind == "DIRECT" and item.message_id not in top_ids
        ]

        message_sources_complete = all(
            row["status"] == "COMPLETE"
            for row in coverage
            if row["source"] in {"messages", "mentions"}
        )
        failures = (general, mentions) + (
            (calendar_result,) if self.calendar_enabled else ()
        )
        reauthorization_required = bool(failures) and all(
            isinstance(result, AuthenticationRequired) for result in failures
        )
        permission_required = any(
            isinstance(result, FeishuApiError)
            and str(result.code) == "99991672"
            for result in failures
        )
        report = {
            "report_type": "OPEN_ATTENTION",
            "report_date": current_date.isoformat(),
            "generated_at": now.isoformat(),
            "scan_window": {
                "start": scan_start.astimezone(self.timezone).isoformat(),
                "end": scan_end.astimezone(self.timezone).isoformat(),
            },
            "coverage_status": self._overall_status(coverage),
            "coverage": coverage,
            "top_three": [self._ranked_dict(item) for item in ranked],
            "other_unhandled_mentions": [
                self._attention_dict(item) for item in other_mentions
            ],
            "calendar": [self._calendar_dict(item) for item in calendar],
            "reauthorization_required": reauthorization_required,
            "permission_required": permission_required,
            "missing_scopes": (
                list(self.client.required_scopes()) if permission_required else []
            ),
            "disclaimer": "待关注/未处理不等同于飞书客户端真实未读。",
        }
        self.ledger.save_brief(report)
        if message_sources_complete:
            self.ledger.set_watermark("messages", now)
        self.ledger.purge(now)
        return report

    def latest(self) -> dict[str, Any] | None:
        return self.ledger.latest_brief()

    def health(self) -> dict[str, Any]:
        capabilities = self.client.capabilities()
        configured = bool(self.self_open_id) and bool(
            capabilities.get("user_token_configured")
        )
        latest = self.latest()
        if not configured:
            status = "CONFIGURATION_REQUIRED"
        elif latest and latest.get("permission_required"):
            status = "PERMISSION_REQUIRED"
        else:
            status = "READY"
        return {
            "status": status,
            "self_open_id_configured": bool(self.self_open_id),
            "capabilities": capabilities,
            "latest_available": latest is not None,
            "disclaimer": "开放接口不提供客户端逐会话未读游标。",
        }
