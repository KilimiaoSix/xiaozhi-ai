"""把一份晨报报告压成一句能念出口的待办播报。

预览报告本身是给桌面端看的：Top 3 带 240 字摘要、其他提及、完整日程、逐源覆盖说明。
机器人只有一块小屏和一条 TTS 通道，整份念完要一分多钟，人在早上不会听。所以这里
只保留「按优先级排好的前几条待办」，其余一律折成数量，长摘要按字数截断。

排序不在这里做：报告里的 top_three 已经是 ranking.py 按分数排好的顺序，本模块
只负责编排文案，保证屏幕上的第 1 条就是最该先处理的那条。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


DEFAULT_MAX_ITEMS = 3
DEFAULT_ITEM_CHARS = 16
DEFAULT_GREETING = "早上好"
ELLIPSIS = "…"

STATUS_BRIEF = "早报"
EMOTION_BRIEF = "happy"
STATUS_UNAVAILABLE = "早报不可用"
EMOTION_UNAVAILABLE = "confused"

TEXT_REAUTHORIZATION = "早报暂不可用：飞书授权未配置或已过期，修复后明早我再念。"
TEXT_PERMISSION = "早报暂不可用：飞书应用还缺读取权限。"
PARTIAL_SUFFIX = "数据可能不全。"


@dataclass(frozen=True)
class Announcement:
    text: str
    emotion: str
    status: str
    speak: bool


def _clip(text: str, limit: int) -> str:
    """摘要里常有换行和连续空格，先压成一行再按字数截断。"""
    flattened = " ".join(str(text or "").split())
    if len(flattened) <= limit:
        return flattened
    return flattened[:limit] + ELLIPSIS


def _start_times(report: dict[str, Any], display_timezone=None) -> dict[str, str]:
    """日程只在 calendar 段里带开始时间，ranked 项里没有，这里按 event_id 建索引。

    start 串带的是日程自身的时区偏移（海外组织者建的会不是 +08:00），钟点必须
    换算到播报时区再念，否则北京 17:00 的会能被念成 02:00。
    """
    times: dict[str, str] = {}
    for event in report.get("calendar") or []:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("event_id") or "")
        raw_start = event.get("start")
        if not event_id or not raw_start:
            continue
        try:
            start = datetime.fromisoformat(str(raw_start).replace("Z", "+00:00"))
        except ValueError:
            continue
        if display_timezone is not None and start.tzinfo is not None:
            start = start.astimezone(display_timezone)
        times[event_id] = start.strftime("%H:%M")
    return times


def _unavailable(text: str) -> Announcement:
    # 授权/权限这类问题要让人看见，但没必要打断早上的第一分钟去念一遍。
    # 这条是故障通知不是晨报，所以不套用配置的状态栏文案和表情——
    # 把「早报」四个字留在屏幕上会让人以为待办就是空的。
    return Announcement(
        text=text,
        emotion=EMOTION_UNAVAILABLE,
        status=STATUS_UNAVAILABLE,
        speak=False,
    )


def build_announcement(
    report: dict[str, Any],
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    item_chars: int = DEFAULT_ITEM_CHARS,
    greeting: str = DEFAULT_GREETING,
    status: str = STATUS_BRIEF,
    emotion: str = EMOTION_BRIEF,
    display_timezone=None,
) -> Announcement:
    """按优先级列出待办，返回一条可直接推给设备的播报。

    greeting / status / emotion 都可由配置覆盖：同一台机器人在不同人的工位上，
    称呼和状态栏想叫什么是使用者的事，不该写死在代码里。
    """
    report = report or {}
    if report.get("reauthorization_required"):
        return _unavailable(TEXT_REAUTHORIZATION)
    if report.get("permission_required"):
        return _unavailable(TEXT_PERMISSION)

    max_items = max(1, int(max_items))
    item_chars = max(1, int(item_chars))
    # 空串是有意义的取值：到岗迎接已经问过好时，晨报不该再说一遍「早上好」
    greeting = DEFAULT_GREETING if greeting is None else str(greeting)
    opening = f"{greeting}，" if greeting else ""
    status = str(status or STATUS_BRIEF)
    emotion = str(emotion or EMOTION_BRIEF)
    ranked = [
        item
        for item in (report.get("top_three") or [])
        if isinstance(item, dict)
    ]
    start_times = _start_times(report, display_timezone)

    lines: list[str] = []
    seen: set[str] = set()
    for item in ranked:
        title = _clip(item.get("title"), item_chars) or "待查看消息"
        prefix = ""
        if item.get("kind") == "CALENDAR":
            start = start_times.get(str(item.get("item_id") or ""))
            if start:
                prefix = f"{start} "
        line = f"{prefix}{title}"
        # 去重比的是「念出来的样子」：真实数据里出现过两条同文案的「授权操作通知」，
        # 截断后同形的两条听起来也是同一件事，念第二遍只会让人以为机器人坏了。
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
        if len(lines) == max_items:
            break

    if not lines:
        text = f"{opening}今天暂时没有待办。"
        if report.get("coverage_status") not in (None, "COMPLETE"):
            # 采集缺了一路时，空待办也不能说得斩钉截铁
            text += PARTIAL_SUFFIX
        return Announcement(
            text=text,
            emotion=emotion,
            status=status,
            speak=True,
        )

    entries = [f"{index} {line}" for index, line in enumerate(lines, start=1)]
    text = f"{opening}今天 {len(entries)} 件待办：" + "；".join(entries) + "。"

    others = len(report.get("other_unhandled_mentions") or [])
    if others:
        text += f"另有 {others} 条待回。"
    if report.get("coverage_status") not in (None, "COMPLETE"):
        # 采集缺了一路却照常念完，会让人以为待办就这些。
        text += PARTIAL_SUFFIX

    return Announcement(
        text=text,
        emotion=emotion,
        status=status,
        speak=True,
    )
