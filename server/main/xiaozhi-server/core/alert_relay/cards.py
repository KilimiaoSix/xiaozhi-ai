"""飞书交互卡片模板。

诊断卡片严格按 diagnose-sae-alert skill 的输出契约逐段渲染：
字段短、可扫读、空的段落直接不出现——渲染成空白块比不渲染更难读。
"""

from __future__ import annotations

import json
from typing import Any

from core.alert_relay.models import (
    LEVEL_CRITICAL,
    LEVEL_URGENT,
    AlertEvent,
    Diagnosis,
    RelayRecord,
)


INTENT_DIAGNOSE = "diagnose"
INTENT_DECLINE = "decline"

_LEVEL_TEMPLATE = {
    LEVEL_URGENT: "red",
    LEVEL_CRITICAL: "orange",
}
DEFAULT_TEMPLATE = "yellow"

MAX_DETAIL_CHARS = 800


def _md(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _note(content: str) -> dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "lark_md", "content": content}]}


def _divider() -> dict[str, Any]:
    return {"tag": "hr"}


def _safe(text: str) -> str:
    """lark_md 会把尖括号当标签吃掉，日志片段里带 <xxx> 很常见。"""
    return str(text or "").replace("<", "＜").replace(">", "＞")


def _clip(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "…"


def _button(text: str, intent: str, alert_id: str, *, style: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": style,
        # 回调只回传 value，不带 alert_id 就认不出是哪条告警。
        "value": {"alert_id": alert_id, "intent": intent},
    }


def _header(title: str, template: str) -> dict[str, Any]:
    return {
        "template": template,
        "title": {"tag": "plain_text", "content": _clip(title, 100)},
    }


def _alert_fact_lines(event: AlertEvent) -> list[str]:
    rows = [
        ("集群", event.cluster),
        ("命名空间", event.namespace),
        ("workload", event.resolved_workload()),
        ("告警对象", event.target),
        ("关键词", event.keyword),
        ("告警时间", event.alert_time),
    ]
    return [f"**{name}**：{_safe(value)}" for name, value in rows if value]


def build_alert_card(record: RelayRecord) -> dict[str, Any]:
    """告警到达时发给值班人的卡片。两个按钮就是状态机的人工决策点。"""
    event = record.event
    template = _LEVEL_TEMPLATE.get(event.level, DEFAULT_TEMPLATE)
    elements: list[dict[str, Any]] = [
        _md("\n".join(_alert_fact_lines(event)) or "（告警字段解析为空，见原文）")
    ]

    if record.repeat_count:
        elements.append(
            _note(f"⚠️ 同一规则在窗口内重复触发 **{record.repeat_count}** 次，已合并通知")
        )

    if record.robot_delivered:
        elements.append(_note("🤖 桌面机器人已抬头提醒"))
    else:
        reason = record.robot_error or "未知原因"
        elements.append(_note(f"🤖 机器人离线，未能当面提醒（{_safe(reason)}）"))

    actions: list[dict[str, Any]] = [
        _button("帮我查", INTENT_DIAGNOSE, record.alert_id, style="primary"),
        _button("我自己看", INTENT_DECLINE, record.alert_id, style="default"),
    ]
    if event.policy_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开告警策略"},
                "type": "default",
                "url": event.policy_url,
            }
        )
    elements.append({"tag": "action", "actions": actions})
    elements.append(
        _note("点「帮我查」我会调起本机 Claude Code 做只读根因诊断，不改任何线上对象。")
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(f"[{event.level}] {event.summary()}", template),
        "elements": elements,
    }


def _affected_lines(diagnosis: Diagnosis) -> list[str]:
    lines = []
    for row in diagnosis.affected:
        # taskId 完整保留，这是 skill 输出标准里点名的硬要求。
        parts = [row.get("time", ""), row.get("uid", ""), row.get("taskId", "")]
        text = " ｜ ".join(part for part in parts if part)
        note = row.get("note", "")
        lines.append(f"- {_safe(text)}" + (f"（{_safe(note)}）" if note else ""))
    return lines


def _why_lines(diagnosis: Diagnosis) -> list[str]:
    lines = []
    for row in diagnosis.why:
        point = _safe(row.get("point", ""))
        code = _safe(row.get("code", ""))
        log = _safe(row.get("log", ""))
        line = f"- **{point}**"
        if code:
            line += f" — `{code}`"
        if log:
            line += f"\n  日志：{log}"
        lines.append(line)
    return lines


def build_diagnosis_card(record: RelayRecord, diagnosis: Diagnosis) -> dict[str, Any]:
    """诊断结论卡片，回在告警卡片的同一话题下。"""
    template = _LEVEL_TEMPLATE.get(diagnosis.severity or record.event.level, DEFAULT_TEMPLATE)
    elements: list[dict[str, Any]] = [_md(f"**根因**：{_safe(diagnosis.root_cause)}")]

    if diagnosis.time_window:
        elements.append(_md(f"**失败时间窗**：{_safe(diagnosis.time_window)}"))
    if diagnosis.user_impact:
        elements.append(_md(f"**用户影响**：{_safe(diagnosis.user_impact)}"))
    if diagnosis.affected_summary:
        elements.append(_note(_safe(diagnosis.affected_summary)))

    if diagnosis.affected:
        elements.append(_divider())
        elements.append(_md("**受影响的请求**\n" + "\n".join(_affected_lines(diagnosis))))
    if diagnosis.timeline:
        elements.append(_divider())
        elements.append(
            _md("**时间轴**\n" + "\n".join(f"{i + 1}. {_safe(step)}"
                                          for i, step in enumerate(diagnosis.timeline)))
        )
    if diagnosis.why:
        elements.append(_divider())
        elements.append(_md("**为什么**\n" + "\n".join(_why_lines(diagnosis))))
    if diagnosis.ruled_out:
        elements.append(_divider())
        elements.append(
            _md("**已排除**\n" + "\n".join(f"- {_safe(item)}" for item in diagnosis.ruled_out))
        )
    if diagnosis.suggestion:
        elements.append(_divider())
        elements.append(
            _md("**建议**\n" + "\n".join(f"- {_safe(item)}" for item in diagnosis.suggestion))
        )

    elements.append(_divider())
    elements.append(
        _note("来源：grep 代码 + 只读拉取 SAE 日志，未改任何线上对象；建议需人工执行。")
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(f"诊断结论 · {diagnosis.title}", template),
        "elements": elements,
    }


def build_failure_card(
    record: RelayRecord,
    reason: str,
    detail: str = "",
    *,
    retry_hint: str = "",
) -> dict[str, Any]:
    """诊断没跑成的卡片。宁可说清楚失败，也绝不编一个根因。"""
    elements: list[dict[str, Any]] = [_md(f"**失败原因**：{_safe(reason)}")]
    if detail:
        elements.append(_md(f"```\n{_clip(_safe(detail), 600)}\n```"))
    elements.append(_note(f"告警：{_safe(record.event.summary())}"))
    if retry_hint:
        elements.append(_md(f"**手工重跑**\n```\n{_clip(_safe(retry_hint), 400)}\n```"))
    elements.append(_note("诊断未产出结论，请人工介入；本次未改任何线上对象。"))
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("诊断失败", "red"),
        "elements": elements,
    }
