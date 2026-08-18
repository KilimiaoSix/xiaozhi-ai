"""外部消息和日程的最小化领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ModelValidationError(ValueError):
    """外部数据缺少生成可靠关注项所需的字段。"""


@dataclass(frozen=True)
class AttentionItem:
    message_id: str
    topic_id: str
    sender_id: str
    sender_name: str
    chat_id: str
    chat_name: str
    chat_type: str
    mention_kind: str
    short_excerpt: str
    source_timestamp: datetime
    source_url: str
    status: str = "OPEN_NEW"
    user_replied: bool = False


@dataclass(frozen=True)
class CalendarItem:
    event_id: str
    summary: str
    start: datetime
    end: datetime
    timezone: str
    location: str
    rsvp_status: str
    organizer: str
    source_url: str
    conflicts: tuple[str, ...] = ()


def _first_text(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _extract_id(value: Any) -> str:
    if isinstance(value, dict):
        return _first_text(
            value.get("open_id"),
            value.get("user_id"),
            value.get("union_id"),
            value.get("id"),
        )
    return _first_text(value)


def _parse_timestamp(value: Any) -> datetime:
    text = _first_text(value)
    if not text:
        raise ModelValidationError("timestamp is required")
    if re.fullmatch(r"\d+", text):
        seconds = int(text)
        if seconds > 10_000_000_000:
            seconds /= 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelValidationError("timestamp is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def resolve_timezone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        return timezone.utc


def _flatten_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return " ".join(value.split())
        return _flatten_content(decoded)
    if isinstance(value, list):
        parts = [_flatten_content(item) for item in value]
        return " ".join(part for part in parts if part)
    if not isinstance(value, dict):
        return _first_text(value)

    if isinstance(value.get("text"), str):
        return " ".join(value["text"].split())

    parts: list[str] = []
    for key in ("zh_cn", "en_us", "ja_jp", "title", "content"):
        if key in value:
            text = _flatten_content(value[key])
            if text:
                parts.append(text)
    return " ".join(parts)


def _mention_kind(mentions: Any, self_open_id: str) -> str:
    direct = False
    at_all = False
    for mention in mentions if isinstance(mentions, list) else []:
        mention_id = _extract_id(mention.get("id") if isinstance(mention, dict) else mention)
        if mention_id == self_open_id and self_open_id:
            direct = True
        if mention_id.lower() in {"all", "@all"}:
            at_all = True
    if direct:
        return "DIRECT"
    if at_all:
        return "ALL"
    return "NONE"


def normalize_message(
    raw: dict[str, Any], self_open_id: str, excerpt_chars: int
) -> AttentionItem:
    if not isinstance(raw, dict):
        raise ModelValidationError("message must be an object")
    message_id = _first_text(raw.get("message_id"), raw.get("id"))
    if not message_id:
        raise ModelValidationError("message_id is required")

    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    sender_id = _extract_id(
        sender.get("sender_id", sender.get("id", raw.get("sender_id")))
    )
    body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
    content = raw.get("content") if raw.get("content") is not None else body.get("content")
    excerpt = _flatten_content(content)
    excerpt = excerpt[: max(1, int(excerpt_chars))]
    chat_id = _first_text(raw.get("chat_id"))
    topic_id = _first_text(
        raw.get("thread_id"), raw.get("root_id"), message_id
    )
    source_url = _first_text(raw.get("source_url"), raw.get("message_url"))
    if not source_url:
        source_url = f"xfchat://chat/{chat_id}/message/{message_id}"

    return AttentionItem(
        message_id=message_id,
        topic_id=topic_id,
        sender_id=sender_id,
        sender_name=_first_text(sender.get("name"), raw.get("sender_name")),
        chat_id=chat_id,
        chat_name=_first_text(raw.get("chat_name")),
        chat_type=_first_text(raw.get("chat_type"), "unknown").lower(),
        mention_kind=_mention_kind(raw.get("mentions"), self_open_id),
        short_excerpt=excerpt,
        source_timestamp=_parse_timestamp(
            raw.get("create_time", raw.get("timestamp"))
        ),
        source_url=source_url,
    )


def _calendar_time(value: Any, fallback_timezone: str) -> datetime:
    if not isinstance(value, dict):
        return _parse_timestamp(value)
    if value.get("timestamp"):
        result = _parse_timestamp(value["timestamp"])
        timezone_name = _first_text(value.get("timezone"), fallback_timezone)
        return result.astimezone(resolve_timezone(timezone_name))
    if value.get("date"):
        parsed_date = date.fromisoformat(str(value["date"]))
        return datetime.combine(
            parsed_date,
            time.min,
            tzinfo=resolve_timezone(fallback_timezone),
        )
    raise ModelValidationError("calendar time is required")


def normalize_calendar_event(
    raw: dict[str, Any], timezone_name: str
) -> CalendarItem:
    if not isinstance(raw, dict):
        raise ModelValidationError("calendar event must be an object")
    event_id = _first_text(raw.get("event_id"), raw.get("id"))
    if not event_id:
        raise ModelValidationError("event_id is required")
    start = _calendar_time(raw.get("start_time"), timezone_name)
    end = _calendar_time(raw.get("end_time"), timezone_name)
    location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    organizer = (
        raw.get("event_organizer")
        if isinstance(raw.get("event_organizer"), dict)
        else {}
    )
    vchat = raw.get("vchat") if isinstance(raw.get("vchat"), dict) else {}

    return CalendarItem(
        event_id=event_id,
        summary=_first_text(raw.get("summary"), "未命名日程"),
        start=start,
        end=end,
        timezone=_first_text(
            (raw.get("start_time") or {}).get("timezone")
            if isinstance(raw.get("start_time"), dict)
            else "",
            timezone_name,
        ),
        location=" ".join(
            item
            for item in (
                _first_text(location.get("name")),
                _first_text(location.get("address")),
            )
            if item
        ),
        rsvp_status=_first_text(raw.get("self_rsvp_status"), "unknown"),
        organizer=_first_text(organizer.get("display_name")),
        source_url=_first_text(
            raw.get("source_url"),
            raw.get("event_url"),
            vchat.get("meeting_url"),
            f"xfchat://calendar/event/{event_id}",
        ),
    )
