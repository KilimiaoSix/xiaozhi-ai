"""每日关注项的确定性评分和 Top 3 选择。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import re

from core.morning_brief.models import AttentionItem, CalendarItem


REQUEST_PATTERN = re.compile(r"[?？]|请|需要|麻烦|确认|审批|处理|安排|跟进|回复")
INCIDENT_PATTERN = re.compile(r"生产|线上|故障|告警|阻塞|失败|异常|客户影响|回滚")
DEADLINE_PATTERN = re.compile(r"今天|今日|当天|下班前|截止|立即|尽快")
INFORMATIONAL_PATTERN = re.compile(r"^(通知|公告|同步)[：:]?|仅供参考|无需处理|已经发布")


@dataclass(frozen=True)
class RankedItem:
    kind: str
    item_id: str
    topic_id: str
    title: str
    score: int
    reasons: tuple[str, ...]
    source_url: str
    status: str
    confidence: str


def _confidence(score: int) -> str:
    if score >= 100:
        return "HIGH"
    if score >= 35:
        return "MEDIUM"
    return "LOW"


def score_attention_item(item: AttentionItem) -> RankedItem:
    score = 0
    reasons: list[str] = []
    text = item.short_excerpt

    if item.mention_kind == "DIRECT":
        score += 100
        reasons.append("direct_mention")
    elif item.mention_kind == "ALL":
        score += 10
        reasons.append("at_all")

    is_request = bool(REQUEST_PATTERN.search(text))
    if item.chat_type == "p2p" and is_request:
        score += 55
        reasons.append("p2p_request")
    if is_request:
        score += 35
        reasons.append("request_or_question")

    if INCIDENT_PATTERN.search(text):
        score += 30
        reasons.append("production_incident")
    if DEADLINE_PATTERN.search(text):
        score += 25
        reasons.append("deadline_today")
    if item.status == "OPEN_CARRIED":
        score += 20
        reasons.append("carried")
    if item.user_replied:
        score -= 70
        reasons.append("user_replied")
    if INFORMATIONAL_PATTERN.search(text):
        score -= 40
        reasons.append("informational")

    return RankedItem(
        kind="MESSAGE",
        item_id=item.message_id,
        topic_id=item.topic_id,
        title=item.short_excerpt or "待查看消息",
        score=score,
        reasons=tuple(reasons),
        source_url=item.source_url,
        status=item.status,
        confidence=_confidence(score),
    )


def _score_calendar(item: CalendarItem) -> RankedItem:
    score = 10
    reasons = ["calendar_attention"]
    if item.rsvp_status == "needs_action":
        score += 20
        reasons.append("pending_rsvp")
    if item.conflicts:
        score += 15
        reasons.append("conflict")
    return RankedItem(
        kind="CALENDAR",
        item_id=item.event_id,
        topic_id=f"calendar:{item.event_id}",
        title=item.summary,
        score=score,
        reasons=tuple(reasons),
        source_url=item.source_url,
        status=item.rsvp_status,
        confidence=_confidence(score),
    )


def rank_candidates(
    items: list[AttentionItem],
    calendar_items: list[CalendarItem],
    limit: int = 3,
) -> list[RankedItem]:
    candidates = [score_attention_item(item) for item in items]
    candidates.extend(_score_calendar(item) for item in calendar_items)
    topic_counts = Counter(item.topic_id for item in items)
    candidates = [
        replace(
            candidate,
            score=candidate.score + 15,
            reasons=candidate.reasons + ("multiple_messages",),
            confidence=_confidence(candidate.score + 15),
        )
        if candidate.kind == "MESSAGE" and topic_counts[candidate.topic_id] > 1
        else candidate
        for candidate in candidates
    ]
    candidates.sort(key=lambda item: (-item.score, item.item_id))

    selected: list[RankedItem] = []
    seen_topics: set[str] = set()
    for candidate in candidates:
        if candidate.topic_id in seen_topics:
            continue
        selected.append(candidate)
        seen_topics.add(candidate.topic_id)
        if len(selected) == limit:
            return selected

    if len(selected) < limit:
        selected_ids = {item.item_id for item in selected}
        for candidate in candidates:
            if candidate.item_id in selected_ids:
                continue
            selected.append(candidate)
            if len(selected) == limit:
                break
    return selected
