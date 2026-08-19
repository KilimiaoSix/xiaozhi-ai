"""把 SAE 告警原文解析成结构化事件。

解析只服务于「通知」和「拉日志的 label」，**原文一定原样带着走**——
诊断子进程吃的是原文，解析漏掉的字段不能让诊断也跟着瞎。
"""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from core.alert_relay.models import (
    DEFAULT_LEVEL,
    LEVEL_CRITICAL,
    LEVEL_URGENT,
    LEVEL_WARNING,
    AlertEvent,
    workload_of,
)


# 集群名 → (projectId, clusterId)。与 diagnose-sae-alert skill 的映射表保持一致，
# 两边不一致会让拉日志的接口打到错误的集群上。
DEFAULT_CLUSTER_MAP: dict[str, tuple[str, str]] = {
    "bj-jxq-autocar": ("117", "3"),
}

_FIELD_PATTERNS = {
    "level": r"告警等级",
    "cluster": r"告警集群",
    "namespace": r"命名空间",
    "target": r"告警对象",
    "rule": r"告警规则",
    "alert_time": r"告警时间",
    "policy_url": r"告警策略链接|策略链接|告警链接",
}

_LEVEL_ALIASES = {
    "紧急": LEVEL_URGENT,
    "urgent": LEVEL_URGENT,
    "critical": LEVEL_CRITICAL,
    "p0": LEVEL_URGENT,
    "p1": LEVEL_CRITICAL,
    "严重": LEVEL_CRITICAL,
    "error": LEVEL_CRITICAL,
    "警告": LEVEL_WARNING,
    "warning": LEVEL_WARNING,
    "warn": LEVEL_WARNING,
    "p2": LEVEL_WARNING,
    "info": LEVEL_WARNING,
}

# 「包含关键词 X >N条」有带引号、不带引号、中英文引号几种写法，都要认。
_KEYWORD_PATTERNS = (
    r"包含关键词\s*[“\"']([^”\"']+)[”\"']",
    r"包含关键词\s*(.+?)\s*(?:>|＞|超过|大于)",
    r"关键词\s*[:：]\s*(\S+)",
)


def _field(text: str, label_pattern: str) -> str:
    # 标签本身可能是多选一（如「告警策略链接|策略链接」），必须括进非捕获组，
    # 否则 | 会把整条正则劈开，(.*) 只挂在最后一个分支上。
    pattern = rf"(?:{label_pattern})\s*[:：]\s*(.*)"
    match = re.search(pattern, text)
    if not match:
        return ""
    return match.group(1).strip()


def _normalize_level(raw: str) -> str:
    value = raw.strip()
    if not value:
        return DEFAULT_LEVEL
    return _LEVEL_ALIASES.get(value.lower(), value)


def _keyword_from_rule(rule: str) -> str:
    for pattern in _KEYWORD_PATTERNS:
        match = re.search(pattern, rule)
        if match:
            return match.group(1).strip().strip("“”\"'")
    return ""


def _ids_from_url(url: str) -> tuple[str, str]:
    if not url:
        return "", ""
    parsed = urlparse(url)
    # SAE 控制台是 hash 路由，projectId 常常挂在 fragment 上而不是 query 上。
    query = parse_qs(parsed.query)
    if parsed.fragment:
        fragment_query = parsed.fragment.split("?", 1)
        if len(fragment_query) == 2:
            for key, value in parse_qs(fragment_query[1]).items():
                query.setdefault(key, value)
    project_id = (query.get("projectId") or [""])[0]
    cluster_id = (query.get("clusterId") or [""])[0]
    return str(project_id or ""), str(cluster_id or "")


def parse_alert(
    raw_text: str,
    *,
    overrides: Mapping[str, Any] | None = None,
    cluster_map: Mapping[str, tuple[str, str]] | None = None,
) -> AlertEvent:
    """解析告警原文；`overrides` 里的非空值优先于解析结果。"""
    text = str(raw_text or "")
    parsed = {name: _field(text, pattern) for name, pattern in _FIELD_PATTERNS.items()}

    merged: dict[str, Any] = dict(parsed)
    for key, value in (overrides or {}).items():
        # 空串/None 是「没给」，不能把解析出来的值抹掉。
        if value is None or str(value).strip() == "":
            continue
        merged[key] = str(value).strip()

    level = _normalize_level(str(merged.get("level", "")))
    rule = str(merged.get("rule", ""))
    keyword = str(merged.get("keyword", "")) or _keyword_from_rule(rule)
    target = str(merged.get("target", ""))
    workload = str(merged.get("workload", "")) or workload_of(target)
    cluster = str(merged.get("cluster", ""))
    policy_url = str(merged.get("policy_url", ""))

    known = dict(DEFAULT_CLUSTER_MAP)
    known.update(cluster_map or {})
    project_id, cluster_id = known.get(cluster, ("", ""))
    if not project_id or not cluster_id:
        # 未知集群回退到告警策略链接里的 id——skill 的第 2 步就是这么规定的。
        project_id, cluster_id = _ids_from_url(policy_url)
    project_id = str(merged.get("project_id", "") or project_id)
    cluster_id = str(merged.get("cluster_id", "") or cluster_id)

    return AlertEvent(
        raw_text=text,
        level=level,
        cluster=cluster,
        namespace=str(merged.get("namespace", "")),
        target=target,
        workload=workload,
        keyword=keyword,
        alert_time=str(merged.get("alert_time", "")),
        policy_url=policy_url,
        project_id=project_id,
        cluster_id=cluster_id,
        rule=rule,
    )
