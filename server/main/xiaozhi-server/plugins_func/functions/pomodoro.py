"""番茄钟语音控制。

薄封装：真正的状态机、计时与设备推送都在 core/pomodoro_manager.py，
这里只把语音意图翻译成命令，并给用户一句简短确认语。
"""
from typing import TYPE_CHECKING

from core.pomodoro_manager import (
    PHASE_FOCUS,
    PHASE_LONG_BREAK,
    PHASE_SHORT_BREAK,
    pomodoro_manager,
)
from plugins_func.register import register_function, ToolType, ActionResponse, Action

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

PHASE_NAMES = {
    PHASE_FOCUS: "专注",
    PHASE_SHORT_BREAK: "短休息",
    PHASE_LONG_BREAK: "长休息",
}

NO_SESSION_REPLY = "当前没有进行中的番茄钟"


pomodoro_start_function_desc = {
    "type": "function",
    "function": {
        "name": "pomodoro_start",
        "description": (
            "开始一个番茄钟。用户说“开始番茄钟/开始专注/我要专注/帮我计时专注/"
            "专注25分钟”等想要进入专注计时的场景时调用。"
            "用户明确说了专注多少分钟才传 focus_minutes，没说就不传，用默认时长。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "focus_minutes": {
                    "type": "integer",
                    "description": "本次专注时长，单位分钟，例如：25、45",
                }
            },
            "required": [],
        },
    },
}

pomodoro_pause_function_desc = {
    "type": "function",
    "function": {
        "name": "pomodoro_pause",
        "description": (
            "暂停正在进行的番茄钟。用户说“暂停番茄钟/先暂停一下/停一下计时”等"
            "想要临时中断但不结束本次专注的场景时调用。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

pomodoro_resume_function_desc = {
    "type": "function",
    "function": {
        "name": "pomodoro_resume",
        "description": (
            "继续已暂停的番茄钟。用户说“继续番茄钟/接着计时/继续专注”等"
            "想要从暂停处恢复计时的场景时调用。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

pomodoro_skip_function_desc = {
    "type": "function",
    "function": {
        "name": "pomodoro_skip",
        "description": (
            "跳过当前这一段，直接进入下一相位。用户说“跳过这段休息/不休息了直接下一轮/"
            "这轮专注提前结束”等场景时调用。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

pomodoro_stop_function_desc = {
    "type": "function",
    "function": {
        "name": "pomodoro_stop",
        "description": (
            "结束番茄钟并退出番茄钟画面。用户说“结束番茄钟/不专注了/关掉计时/"
            "停止番茄钟”等想要彻底结束本次番茄钟的场景时调用。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

pomodoro_status_function_desc = {
    "type": "function",
    "function": {
        "name": "pomodoro_status",
        "description": (
            "查询番茄钟当前进度。用户说“还剩多久/现在第几轮/番茄钟怎么样了/"
            "专注还有多长时间”等场景时调用。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _manager(conn: "ConnectionHandler"):
    """语音路径可能先于 HTTP 接口用到状态机，就地把 config 与设备注册表接上。"""
    pomodoro_manager.bind_from_conn(conn)
    return pomodoro_manager


def _reply(text: str, result: str = "ok") -> ActionResponse:
    return ActionResponse(action=Action.RESPONSE, result=result, response=text)


def _humanize(seconds: int) -> str:
    minutes, rest = divmod(max(0, int(seconds)), 60)
    if minutes and rest:
        return f"{minutes}分{rest}秒"
    if minutes:
        return f"{minutes}分钟"
    return f"{rest}秒"


def _describe(status: dict) -> str:
    """把状态快照说成一句话。"""
    phase = PHASE_NAMES.get(status["phase"], status["phase"])
    remaining = _humanize(status["remaining_s"])
    if status["phase"] == PHASE_FOCUS:
        head = f"第{status['round']}轮{phase}"
    else:
        head = phase
    if status["paused"]:
        return f"{head}已暂停，还剩{remaining}"
    return f"{head}中，还剩{remaining}"


@register_function("pomodoro_start", pomodoro_start_function_desc, ToolType.SYSTEM_CTL)
async def pomodoro_start(conn: "ConnectionHandler", focus_minutes: int = None):
    result = await _manager(conn).start(conn.device_id, focus_minutes)
    if result["outcome"] == "already_running":
        return _reply("番茄钟已在进行中", "already_running")
    minutes = result["status"]["total_s"] // 60
    return _reply(f"好，开始专注{minutes}分钟", "started")


@register_function("pomodoro_pause", pomodoro_pause_function_desc, ToolType.SYSTEM_CTL)
async def pomodoro_pause(conn: "ConnectionHandler"):
    result = await _manager(conn).pause(conn.device_id)
    if result["outcome"] == "not_running":
        return _reply(NO_SESSION_REPLY, "not_running")
    if result["outcome"] == "already_paused":
        return _reply("番茄钟已经是暂停状态", "already_paused")
    return _reply("番茄钟已暂停", "paused")


@register_function(
    "pomodoro_resume", pomodoro_resume_function_desc, ToolType.SYSTEM_CTL
)
async def pomodoro_resume(conn: "ConnectionHandler"):
    result = await _manager(conn).resume(conn.device_id)
    if result["outcome"] == "not_running":
        return _reply(NO_SESSION_REPLY, "not_running")
    if result["outcome"] == "already_running":
        return _reply("番茄钟正在进行中", "already_running")
    return _reply("好，继续专注", "resumed")


@register_function("pomodoro_skip", pomodoro_skip_function_desc, ToolType.SYSTEM_CTL)
async def pomodoro_skip(conn: "ConnectionHandler"):
    result = await _manager(conn).skip(conn.device_id)
    if result["outcome"] == "not_running":
        return _reply(NO_SESSION_REPLY, "not_running")
    return _reply("好，跳到下一段", "skipped")


@register_function("pomodoro_stop", pomodoro_stop_function_desc, ToolType.SYSTEM_CTL)
async def pomodoro_stop(conn: "ConnectionHandler"):
    result = await _manager(conn).stop(conn.device_id)
    if result["outcome"] == "not_running":
        return _reply(NO_SESSION_REPLY, "not_running")
    return _reply("番茄钟已结束", "stopped")


@register_function(
    "pomodoro_status", pomodoro_status_function_desc, ToolType.SYSTEM_CTL
)
async def pomodoro_status(conn: "ConnectionHandler"):
    result = await _manager(conn).status(conn.device_id)
    if not result["status"]["active"]:
        return _reply(NO_SESSION_REPLY, "not_running")
    return _reply(_describe(result["status"]), "running")
