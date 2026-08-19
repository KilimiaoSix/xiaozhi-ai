"""线上告警的语音入口（需求文档流程七）。

薄封装：状态机、降噪、恢复观察都在 core/incident_manager.py，诊断子进程在
core/incident_diagnosis.py，这里只把「启动诊断 / 故障怎么样了」两句话翻译成
命令，并给一句立刻能说出口的回复。

诊断要跑几十秒，所以 incident_diagnose 只返回一句确认语，真正的结论由
manager 在后台跑完后主动播报——语音函数里 await 会把这一轮对话卡到超时。
"""

from typing import TYPE_CHECKING

from core.incident_manager import (
    SIMULATED_PREFIX,
    STATUS_OBSERVING,
    STATUS_RECOVERED,
    get_incident_manager,
)
from plugins_func.register import register_function, ToolType, ActionResponse, Action

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

NO_INCIDENT_REPLY = "现在没有待诊断的告警"
NO_ACTIVE_REPLY = "现在没有需要盯着的线上告警"


incident_diagnose_function_desc = {
    "type": "function",
    "function": {
        "name": "incident_diagnose",
        "description": (
            "对当前线上告警启动一次只读诊断。用户说“启动诊断/帮我诊断/"
            "查一下这个故障/看看是什么原因/分析一下这个告警”等想让机器人去查"
            "日志和最近变更的场景时调用。诊断只读，不会改动任何线上环境。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

incident_status_function_desc = {
    "type": "function",
    "function": {
        "name": "incident_status",
        "description": (
            "查询当前线上告警的状态。用户说“故障怎么样了/恢复了吗/现在还在报警吗/"
            "那个告警什么情况”等场景时调用。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _manager(conn: "ConnectionHandler"):
    """语音路径可能先于 HTTP 接口用到状态机，就地把 config 与设备注册表接上。"""
    manager = get_incident_manager()
    manager.bind_from_conn(conn)
    return manager


def _reply(text: str, result: str = "ok") -> ActionResponse:
    return ActionResponse(action=Action.RESPONSE, result=result, response=text)


def _describe(incident: dict) -> str:
    """把故障现状说成一句话。"""
    prefix = SIMULATED_PREFIX if incident.get("simulated") else ""
    title = str(incident.get("title") or "线上告警")
    service = str(incident.get("service") or "")
    head = f"{prefix}{service}的{title}" if service else f"{prefix}{title}"
    repeat = int(incident.get("repeat_count") or 1)

    if incident.get("state") == STATUS_OBSERVING:
        return (
            f"{head}已经收到恢复信号，我还在观察有没有反复，"
            f"这轮一共上报了{repeat}次，确认之后再跟你说。"
        )
    if incident.get("state") == STATUS_RECOVERED:
        return f"{head}已经恢复了，这轮一共上报了{repeat}次。"

    severity = str(incident.get("severity") or "")
    sentence = f"{head}还在报，级别{severity}，一共上报了{repeat}次。"

    diagnosis = incident.get("diagnosis") or {}
    if diagnosis.get("ok") and diagnosis.get("summary"):
        sentence += f"诊断结论是：{diagnosis['summary']}"
    return sentence


@register_function(
    "incident_diagnose", incident_diagnose_function_desc, ToolType.SYSTEM_CTL
)
async def incident_diagnose(conn: "ConnectionHandler"):
    reply = await _manager(conn).start_diagnosis()
    if reply is None:
        return _reply(NO_INCIDENT_REPLY, "no_incident")
    return _reply(reply, "started")


@register_function(
    "incident_status", incident_status_function_desc, ToolType.SYSTEM_CTL
)
async def incident_status(conn: "ConnectionHandler"):
    incident = _manager(conn).active_incident()
    if incident is None:
        return _reply(NO_ACTIVE_REPLY, "idle")
    return _reply(_describe(incident), "active")
