"""晨间简报语音入口：只汇报飞书晨报排出来的前三件事，能说出每件事的来源。

真正的采集、去重、排序都在 core/morning_brief/ 下（不许改），这里只做三件事：
按配置决定要不要跑、把 service 返回的报告翻译成一段口播文案、以及在权限/授权
出问题时给出明确提示。晨报这类"用户当场就能追问细节"的播报，编造字段立刻会
被拆穿，所以宁可说"没有"也不瞎编——source 找不到就用兜底的通用说法。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from core.morning_brief.factory import create_morning_brief_service
from plugins_func.register import register_function, ToolType, ActionResponse, Action

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

# 测试通过替换本模块变量注入假的 service（不改真实 factory）。
_service_factory = create_morning_brief_service

DISABLED_REPLY = "晨报功能还没启用，需要在配置里打开并完成飞书授权"
REAUTH_REPLY = "飞书授权过期了，重新授权后我再帮你汇总"
PERMISSION_REPLY = "晨报缺少必要的飞书权限，需要管理员开通后才能用"
EMPTY_REPLY = "暂时没有需要特别关注的事"
FETCH_ERROR_REPLY = "晨报服务暂时取不到，稍后再试试吧"

# 口播友好长度：每条"第 N 件"控制在这么多字以内（含来源）。
MAX_ITEM_CHARS = 40
NUMERALS = ("一", "二", "三")


get_morning_brief_function_desc = {
    "type": "function",
    "function": {
        "name": "get_morning_brief",
        "description": (
            "汇报今天最重要/最急的事及推荐处理顺序。用户说“今天最重要的事是什么/"
            "今天最急的事是什么/晨间简报/今天有什么安排/帮我过一下今天”等想了解"
            "当天待关注事项的场景时调用。只汇报飞书晨报排出来的前三件事，"
            "每件事都能说出真实来源，不会编造内容。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _reply(text: str, result: str = "ok") -> ActionResponse:
    return ActionResponse(action=Action.RESPONSE, result=result, response=text)


def _enabled(config: dict) -> bool:
    section = (config or {}).get("morning_brief") or {}
    return bool(isinstance(section, dict) and section.get("enabled", False))


def _now_local(service: Any) -> datetime:
    """本地时区当前时间，口径与 service 自己算 report_date 用的完全一致。

    MorningBriefService.preview() 内部用 self.now_provider() 决定"现在"
    （core/morning_brief/service.py），这里复用同一个 provider 而不是另开
    一个 datetime.now()，否则"今天已有报告"的判断可能跟 service 自己的
    report_date 对不上（例如两者取到了午夜前后不同的日期）。
    """
    now_provider = getattr(service, "now_provider", None)
    now = now_provider() if callable(now_provider) else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    tz = getattr(service, "timezone", None)
    return now.astimezone(tz) if tz is not None else now


async def _load_report(service: Any) -> dict:
    """今天已经跑过就用缓存的那份，否则现采一次。

    preview() 内部用 aiohttp 做非阻塞 IO（core/morning_brief/feishu_client.py），
    不是同步阻塞调用，语音函数这里直接 await 即可，不需要 asyncio.to_thread。
    """
    latest = service.latest()
    today = _now_local(service).date().isoformat()
    if latest is not None and latest.get("report_date") == today:
        return latest
    return await service.preview()


def _shorten(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return text[:1]
    return text[: limit - 1] + "…"


def _calendar_by_id(report: dict) -> dict[str, dict]:
    return {
        str(item.get("event_id")): item
        for item in report.get("calendar") or []
        if isinstance(item, dict) and item.get("event_id")
    }


def _open_items_by_message_id(service: Any) -> dict[str, Any]:
    """从台账里把 top_three 消息项的发送者/群名找回来。

    report["top_three"] 只有排序结果（见 core/morning_brief/ranking.py 的
    RankedItem），不带 sender_name/chat_name；这两个字段只存在于台账里的
    AttentionItem（core/morning_brief/models.py），所以要单独查一次。
    查不到就返回空字典，调用方按"没有的字段不编造"处理。
    """
    ledger = getattr(service, "ledger", None)
    if ledger is None:
        return {}
    try:
        items = ledger.get_open_items()
    except Exception:
        return {}
    return {str(item.message_id): item for item in items}


def _source_description(
    ranked: dict, calendar_by_id: dict, open_items_by_message_id: dict
) -> str:
    """来源=消息发送者/群名/日历，按 models.py 实际字段组装，没有就用通用说法。"""
    if ranked.get("kind") == "CALENDAR":
        calendar_item = calendar_by_id.get(str(ranked.get("item_id")))
        organizer = str((calendar_item or {}).get("organizer") or "").strip()
        return f"日历·{organizer}" if organizer else "日历"

    attention = open_items_by_message_id.get(str(ranked.get("item_id")))
    if attention is None:
        return "飞书消息"
    sender = str(getattr(attention, "sender_name", "") or "").strip()
    chat = str(getattr(attention, "chat_name", "") or "").strip()
    if sender and chat:
        return f"{sender}在{chat}"
    return sender or chat or "飞书消息"


def _nearest_meeting_sentence(report: dict, now_local: datetime) -> str:
    """日历项有的话补一句最近的会议时间；没有日历数据就不说。"""
    parsed: list[tuple[datetime, dict]] = []
    for item in report.get("calendar") or []:
        if not isinstance(item, dict):
            continue
        try:
            start_dt = datetime.fromisoformat(str(item.get("start") or ""))
        except ValueError:
            continue
        parsed.append((start_dt, item))
    if not parsed:
        return ""

    upcoming = [entry for entry in parsed if entry[0] >= now_local]
    start_dt, chosen = min(upcoming or parsed, key=lambda entry: entry[0])
    summary = str(chosen.get("summary") or "").strip()
    time_text = start_dt.strftime("%H:%M")
    if summary:
        return f"最近的会议是{time_text}的{summary}。"
    return f"最近的会议在{time_text}。"


def _compose_reply(report: dict, service: Any) -> str:
    top_three = report.get("top_three") or []
    if not top_three:
        return EMPTY_REPLY

    calendar_by_id = _calendar_by_id(report)
    open_items_by_message_id = _open_items_by_message_id(service)

    lines: list[str] = []
    for index, ranked in enumerate(top_three):
        label = NUMERALS[index] if index < len(NUMERALS) else str(index + 1)
        source = _source_description(ranked, calendar_by_id, open_items_by_message_id)
        prefix = f"第{label}件："
        suffix = f"，来自{source}。"
        budget = max(4, MAX_ITEM_CHARS - len(prefix) - len(suffix))
        summary = _shorten(str(ranked.get("title") or "待确认事项"), budget)
        lines.append(f"{prefix}{summary}{suffix}")

    lines.append("建议先处理第一件。")
    meeting_sentence = _nearest_meeting_sentence(report, _now_local(service))
    if meeting_sentence:
        lines.append(meeting_sentence)
    return "".join(lines)


@register_function("get_morning_brief", get_morning_brief_function_desc, ToolType.SYSTEM_CTL)
async def get_morning_brief(conn: "ConnectionHandler"):
    config = getattr(conn, "config", None) or {}
    if not _enabled(config):
        return _reply(DISABLED_REPLY, "disabled")

    service = _service_factory(config)
    try:
        report = await _load_report(service)
    except Exception as exc:
        conn.logger.bind(tag=TAG).warning(f"晨报采集失败: {exc}")
        return _reply(FETCH_ERROR_REPLY, "error")

    if report.get("reauthorization_required"):
        return _reply(REAUTH_REPLY, "reauthorization_required")
    if report.get("permission_required"):
        return _reply(PERMISSION_REPLY, "permission_required")

    return _reply(_compose_reply(report, service), "ok")
