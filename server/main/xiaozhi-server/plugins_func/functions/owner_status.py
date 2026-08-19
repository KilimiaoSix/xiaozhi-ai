"""主人状态语音控制。

薄封装：真正的状态机、请假到期回落与 overdue 判定都在 core/owner_status.py，
这里只把语音意图翻译成 store 调用，做参数归一化，并给用户一句简短确认语。
函数注册风格、ActionResponse 用法照抄 plugins_func/functions/pomodoro.py。

对外文案边界（同 core/owner_status.py 顶部注释）：这三个函数只经手 state /
expected_return / leave_start / leave_end / public_note，会议主题、请假原因
等一律不接收、不落盘——LLM 不该把这些传进来，函数签名里也就没有这些参数。
"""
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Optional, Tuple

from core.owner_status import (
    STATUS_AVAILABLE,
    STATUS_AWAY,
    STATUS_LEAVE,
    STATUS_MEETING,
    VALID_STATES,
    get_owner_status_store,
)
from plugins_func.register import register_function, ToolType, ActionResponse, Action

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

# "11:30" / "08:00" 这类只有时分的表达；HH 允许 0-23，两位或一位都行
_TIME_ONLY_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _now() -> datetime:
    """本地时区当前时间。单独抽出来是为了测试能 monkeypatch 固定时钟。"""
    return datetime.now()


set_owner_status_function_desc = {
    "type": "function",
    "function": {
        "name": "set_owner_status",
        "description": (
            "记录主人当前的声明状态，供来访者应答与到点提醒使用。"
            "用户说“我去开会，十一点半回来”时，state=meeting，expected_return=\"11:30\"；"
            "用户说“我出去一趟，一会回来”“我离开一下”时，state=away；"
            "用户说“我明天请假”时，state=leave，leave_start=\"tomorrow\"；"
            "用户说“今天请假”“我请假”时，state=leave，leave_start=\"today\"。"
            "expected_return 只在 meeting/away 场景下有意义，格式是“HH:MM”"
            "（按今天算，如果这个时间已经过了就按明天算）或完整 ISO 时间，"
            "用户没说具体时间就不传。leave_start/leave_end 只在 leave 场景下有意义，"
            "格式是 \"today\"/\"tomorrow\"/\"YYYY-MM-DD\"，用户没说到哪天结束就不传 leave_end。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": sorted(VALID_STATES),
                    "description": (
                        "声明状态：available 在岗 / meeting 会议中 / "
                        "away 稍后回来 / leave 请假"
                    ),
                },
                "expected_return": {
                    "type": "string",
                    "description": "预计返回时间，meeting/away 场景可选，例如 \"11:30\"",
                },
                "public_note": {
                    "type": "string",
                    "description": "对外文案，来访者会听到，可选，不传就用状态的默认文案",
                },
                "leave_start": {
                    "type": "string",
                    "description": (
                        "请假开始日期，leave 场景必填，"
                        "例如 \"today\"/\"tomorrow\"/\"2026-08-20\""
                    ),
                },
                "leave_end": {
                    "type": "string",
                    "description": "请假结束日期，leave 场景可选，缺省同 leave_start",
                },
            },
            "required": ["state"],
        },
    },
}

query_owner_status_function_desc = {
    "type": "function",
    "function": {
        "name": "query_owner_status",
        "description": (
            "查询主人当前的声明状态。用户问“他在吗”“主人在不在”“什么时候回来”"
            "“现在是什么状态”等场景时调用。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

clear_owner_status_function_desc = {
    "type": "function",
    "function": {
        "name": "clear_owner_status",
        "description": (
            "取消主人当前的声明状态，恢复默认在岗。用户说“我回来了”“取消请假”"
            "“不用挂会议中了”等场景时调用。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _reply(text: str, result: str = "ok") -> ActionResponse:
    return ActionResponse(action=Action.RESPONSE, result=result, response=text)


def _normalize_expected_return(value: Optional[str]) -> Optional[str]:
    """meeting/away 的预计返回时间：接受 "HH:MM"（今天，若已过则明天）或 ISO。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    match = _TIME_ONLY_RE.match(text)
    if match:
        now = _now()
        hour, minute = int(match.group(1)), int(match.group(2))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            # 说"11:30回来"这个点已经过了，只能是在说明天这个点
            candidate += timedelta(days=1)
        return candidate.isoformat(timespec="seconds")

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法识别的返回时间: {value}") from exc
    return parsed.isoformat(timespec="seconds")


def _normalize_leave_bound(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text == "today":
        return _now().date().isoformat()
    if text == "tomorrow":
        return (_now().date() + timedelta(days=1)).isoformat()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"无法识别的日期: {value}") from exc


def _normalize_leave_range(
    state: str, leave_start: Optional[str], leave_end: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    if state != STATUS_LEAVE:
        return None, None
    start = leave_start
    if start is None or not str(start).strip():
        # 用户只说"我请假"没说哪天，按今天处理最省心，也符合"今天请假"的默认语感
        start = "today"
    return _normalize_leave_bound(start), _normalize_leave_bound(leave_end)


def _describe_day(iso_date: str, today: date) -> str:
    d = date.fromisoformat(iso_date)
    if d == today:
        return "今天"
    if d == today + timedelta(days=1):
        return "明天"
    return f"{d.month}月{d.day}日"


def _format_clock(iso_value: str, today: date) -> str:
    dt = datetime.fromisoformat(iso_value)
    prefix = "" if dt.date() == today else f"{dt.month}月{dt.day}日"
    return f"{prefix}{dt.hour:02d}:{dt.minute:02d}"


def _confirm_text(status: dict) -> str:
    state = status["state"]
    if state in (STATUS_MEETING, STATUS_AWAY):
        return "知道了，我帮你看着工位"
    if state == STATUS_LEAVE:
        today = _now().date()
        start = _describe_day(status["leave_start"], today)
        end_raw = status.get("leave_end") or status["leave_start"]
        if end_raw == status["leave_start"]:
            return f"记住了，{start}的重要留言我替你收好"
        end = _describe_day(end_raw, today)
        return f"记住了，{start}到{end}请假期间的重要留言我替你收好"
    return "好，状态已恢复"


def _describe_status(status: dict) -> str:
    state = status["state"]
    if state == STATUS_AVAILABLE:
        return "他现在在工位，可以直接找他"

    if state in (STATUS_MEETING, STATUS_AWAY):
        label = "开会" if state == STATUS_MEETING else "暂时离开"
        text = f"他现在{label}"
        expected = status.get("expected_return")
        if expected:
            text += f"，预计{_format_clock(expected, _now().date())}回来"
        else:
            text += "，暂时没说什么时候回来"
        if status.get("overdue"):
            text += "，不过已经过了预计时间了"
        return text

    if state == STATUS_LEAVE:
        today = _now().date()
        start = _describe_day(status["leave_start"], today)
        end_raw = status.get("leave_end") or status["leave_start"]
        if end_raw == status["leave_start"]:
            return f"他请假中，{start}休息"
        end = _describe_day(end_raw, today)
        return f"他请假中，{start}到{end}"

    return status.get("public_note") or "状态未知"


@register_function(
    "set_owner_status", set_owner_status_function_desc, ToolType.SYSTEM_CTL
)
async def set_owner_status(
    conn: "ConnectionHandler",
    state: str,
    expected_return: str = None,
    public_note: str = None,
    leave_start: str = None,
    leave_end: str = None,
):
    try:
        normalized_expected = _normalize_expected_return(expected_return)
        normalized_start, normalized_end = _normalize_leave_range(
            state, leave_start, leave_end
        )
    except ValueError as e:
        return _reply(f"没太明白，能再说一次吗？（{e}）", "invalid_input")

    store = get_owner_status_store(conn.config)
    try:
        status = store.set_status(
            state=state,
            public_note=str(public_note or ""),
            expected_return=normalized_expected,
            leave_start=normalized_start,
            leave_end=normalized_end,
        )
    except ValueError as e:
        return _reply(f"没太明白，能再说一次吗？（{e}）", "invalid_input")

    return _reply(_confirm_text(status), "ok")


@register_function(
    "query_owner_status", query_owner_status_function_desc, ToolType.SYSTEM_CTL
)
async def query_owner_status(conn: "ConnectionHandler"):
    store = get_owner_status_store(conn.config)
    status = store.get()
    return _reply(_describe_status(status), "ok")


@register_function(
    "clear_owner_status", clear_owner_status_function_desc, ToolType.SYSTEM_CTL
)
async def clear_owner_status(conn: "ConnectionHandler"):
    store = get_owner_status_store(conn.config)
    store.clear()
    return _reply("好，状态已恢复", "ok")
