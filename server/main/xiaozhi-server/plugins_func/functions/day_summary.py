"""日终总结语音入口：≤3 句说完今天完成的任务、线上事件与明日安排。

数据源解耦：本模块不直接依赖 away_ledger / incident_manager / owner_status
等并行任务的实现（不许改那些文件，此时它们也未必都已就绪），只约定三个
已知语义槽位——agent_events（离席台账当日事件）、incidents（今日故障列表）、
tomorrow_status（主人明日状态）——由各集成方在启动时调用
register_summary_provider 接上自己的真实数据源。播报只使用 provider 返回的
真实数据，没有就不说，全空则给一句兜底安慰语，不编造任何数字或事件。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, Union

from plugins_func.register import register_function, ToolType, ActionResponse, Action

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

AGENT_EVENTS_KEY = "agent_events"
INCIDENTS_KEY = "incidents"
TOMORROW_STATUS_KEY = "tomorrow_status"

FALLBACK_REPLY = "今天的记录不多，注意休息。"

# name -> 返回数据的 provider。集成方在启动装配时调用 register_summary_provider
# 注册；测试用 reset_summary_providers 清场，避免用例间串台。
summary_providers: Dict[str, Callable[[], Union[dict, list, None]]] = {}


def register_summary_provider(
    name: str, fn: Callable[[], Union[dict, list, None]]
) -> None:
    """登记一个数据源。同名重复注册直接覆盖，方便替换/测试。"""
    summary_providers[str(name)] = fn


def reset_summary_providers() -> None:
    """仅测试用：清空已注册的 providers。"""
    summary_providers.clear()


end_of_day_summary_function_desc = {
    "type": "function",
    "function": {
        "name": "end_of_day_summary",
        "description": (
            "生成今天的日终总结。用户说“总结一下今天/下班总结/今天干了什么/"
            "帮我总结一下今天/下班了”等想快速回顾当天的场景时调用。"
            "只汇报已接入数据源的真实统计（完成的任务数、线上故障、明日安排），"
            "没有数据的部分不会编造，总长控制在三句以内。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _reply(text: str, result: str = "ok") -> ActionResponse:
    return ActionResponse(action=Action.RESPONSE, result=result, response=text)


def _call_provider(name: str) -> Any:
    """跑一个 provider；不存在、返回异常都当作没有数据，不让单个数据源拖垮总结。"""
    provider = summary_providers.get(name)
    if provider is None:
        return None
    try:
        return provider()
    except Exception:
        return None


def _extract_count(value: Any) -> int:
    """把 provider 的返回值统一成一个非负整数计数。

    支持 list/tuple（数长度）、dict（取 count 字段）、int 三种形状，
    其余一律当没有数据，不猜测语义。
    """
    if isinstance(value, (list, tuple)):
        return len(value)
    if isinstance(value, dict):
        try:
            return max(0, int(value.get("count", 0)))
        except (TypeError, ValueError):
            return 0
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    return 0


def _incident_clause(value: Any) -> str:
    """今日故障列表拼成一个短句；没有故障就返回空串，交给上层跳过这一段。"""
    if not isinstance(value, (list, tuple)) or not value:
        return ""
    total = len(value)
    recovered = sum(
        1 for item in value if isinstance(item, dict) and item.get("recovered")
    )
    if total == 1:
        status = "已恢复" if recovered == 1 else "处理中"
        return f"处理了一次线上告警（{status}）"
    return f"处理了{total}次线上告警，{recovered}次已恢复"


def _tomorrow_status_sentence(value: Any) -> str:
    """明日安排；目前只认识"请假"这一种主人声明状态，其余状态不编造文案。"""
    if not isinstance(value, dict):
        return ""
    if str(value.get("state") or "") == "leave":
        return "明天你请假，重要留言我会替你收好。"
    return ""


def _compose_summary() -> str:
    task_count = _extract_count(_call_provider(AGENT_EVENTS_KEY))
    incident_clause = _incident_clause(_call_provider(INCIDENTS_KEY))
    tomorrow_sentence = _tomorrow_status_sentence(_call_provider(TOMORROW_STATUS_KEY))

    clauses = []
    if task_count > 0:
        clauses.append(f"今天完成了{task_count}个编码任务")
    if incident_clause:
        clauses.append(incident_clause)

    sentences = []
    if clauses:
        sentences.append("，".join(clauses) + "。")
    if tomorrow_sentence:
        sentences.append(tomorrow_sentence)

    if not sentences:
        return FALLBACK_REPLY
    return "".join(sentences)


@register_function(
    "end_of_day_summary", end_of_day_summary_function_desc, ToolType.SYSTEM_CTL
)
async def end_of_day_summary(conn: "ConnectionHandler"):
    return _reply(_compose_summary())
