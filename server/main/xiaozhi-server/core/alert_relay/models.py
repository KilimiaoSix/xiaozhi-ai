"""告警值班中继的数据模型与状态机。

诊断结果沿用 diagnose-sae-alert skill 已经敲定的输出契约，这里只做归一与校验，
**不做任何补全或猜测**：契约破了就报错，让上层如实回帖失败原因。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class RelayState(str, Enum):
    RECEIVED = "RECEIVED"
    NOTIFIED = "NOTIFIED"
    AWAITING_REPLY = "AWAITING_REPLY"
    CLAIMED = "CLAIMED"
    DIAGNOSING = "DIAGNOSING"
    DIAGNOSED = "DIAGNOSED"
    DECLINED = "DECLINED"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"


# 状态机把「人必须先认领才能开跑诊断」写死在类型层：
# AWAITING_REPLY / TIMEOUT 都到不了 DIAGNOSING，必须先经过 CLAIMED。
ALLOWED_TRANSITIONS: dict[RelayState, frozenset[RelayState]] = {
    RelayState.RECEIVED: frozenset({RelayState.NOTIFIED, RelayState.FAILED}),
    RelayState.NOTIFIED: frozenset({RelayState.AWAITING_REPLY, RelayState.FAILED}),
    RelayState.AWAITING_REPLY: frozenset(
        {RelayState.CLAIMED, RelayState.DECLINED, RelayState.TIMEOUT}
    ),
    RelayState.TIMEOUT: frozenset({RelayState.CLAIMED, RelayState.DECLINED}),
    RelayState.CLAIMED: frozenset({RelayState.DIAGNOSING, RelayState.FAILED}),
    RelayState.DIAGNOSING: frozenset({RelayState.DIAGNOSED, RelayState.FAILED}),
    RelayState.DIAGNOSED: frozenset(),
    RelayState.DECLINED: frozenset(),
    RelayState.FAILED: frozenset(),
}

TERMINAL_STATES = frozenset(
    {RelayState.DIAGNOSED, RelayState.DECLINED, RelayState.FAILED}
)

LEVEL_URGENT = "紧急"
LEVEL_CRITICAL = "严重"
LEVEL_WARNING = "警告"
DEFAULT_LEVEL = LEVEL_WARNING

# 告警对象是 pod 名，去掉 ReplicaSet hash 和实例后缀才是 workload。
# 拉日志时 fields_workload_name 这个 label 只认 workload，推导错了就一条日志都拉不到；
# 而且推导失败还会顺带毁掉去重指纹（每个 pod 算一条新告警，机器人被刷屏）。
# 随机后缀 k8s 现在固定 5 位，这里放宽到 4-6 位以容忍别的编排器。
_POD_SUFFIX = re.compile(r"-[0-9a-z]{6,10}-[0-9a-z]{4,6}$")
_POD_SUFFIX_STATEFUL = re.compile(r"-\d+$")


def can_transition(current: RelayState, target: RelayState) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def workload_of(target: str) -> str:
    """pod 名 → workload 名。给不出 pod 就原样返回。"""
    name = str(target or "").strip()
    if not name:
        return ""
    stripped = _POD_SUFFIX.sub("", name)
    if stripped != name:
        return stripped
    return _POD_SUFFIX_STATEFUL.sub("", name)


@dataclass(frozen=True)
class AlertEvent:
    raw_text: str
    level: str = DEFAULT_LEVEL
    cluster: str = ""
    namespace: str = ""
    target: str = ""
    workload: str = ""
    keyword: str = ""
    alert_time: str = ""
    policy_url: str = ""
    project_id: str = ""
    cluster_id: str = ""
    rule: str = ""

    def fingerprint(self) -> str:
        """去重指纹：只取「哪个集群的哪个 workload 触发了哪条规则」。

        pod 名和告警时间刻意排除在外——它们每次都变，算进去就永远去不了重。
        """
        parts = [
            self.cluster.strip(),
            self.namespace.strip(),
            (self.workload or workload_of(self.target)).strip(),
            self.keyword.strip(),
        ]
        digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
        return digest[:16]

    def resolved_workload(self) -> str:
        return (self.workload or workload_of(self.target)).strip()

    def summary(self) -> str:
        """一行摘要，给机器人屏幕和卡片标题用。

        OLED 是 128×64 单色屏，16px 中文一行约 8 字、最多 4 行，
        所以摘要在这里就先收敛到 32 字以内，不指望硬件替我们截。
        """
        workload = self.resolved_workload() or self.cluster or "线上服务"
        keyword = self.keyword or (self.rule or "异常告警")
        text = f"{workload} {keyword}"
        return text if len(text) <= 32 else text[:31] + "…"

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "cluster": self.cluster,
            "namespace": self.namespace,
            "target": self.target,
            "workload": self.resolved_workload(),
            "keyword": self.keyword,
            "alert_time": self.alert_time,
            "policy_url": self.policy_url,
            "project_id": self.project_id,
            "cluster_id": self.cluster_id,
            "rule": self.rule,
            "summary": self.summary(),
            "fingerprint": self.fingerprint(),
        }


class DiagnosisFormatError(ValueError):
    """诊断输出不符合 skill 的 JSON 契约。"""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_text(item) for item in value if _text(item)]


def _dict_list(value: Any, keys: Iterable[str]) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    rows: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        row = {key: _text(item.get(key)) for key in keys}
        if any(row.values()):
            rows.append(row)
    return rows


@dataclass(frozen=True)
class Diagnosis:
    title: str
    root_cause: str
    severity: str = ""
    time_window: str = ""
    affected_summary: str = ""
    affected: list[dict[str, str]] = field(default_factory=list)
    user_impact: str = ""
    timeline: list[str] = field(default_factory=list)
    why: list[dict[str, str]] = field(default_factory=list)
    ruled_out: list[str] = field(default_factory=list)
    suggestion: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: Any) -> "Diagnosis":
        if not isinstance(payload, dict):
            raise DiagnosisFormatError("诊断输出不是 JSON 对象")
        title = _text(payload.get("title"))
        root_cause = _text(payload.get("root_cause"))
        if not title:
            raise DiagnosisFormatError("诊断输出缺少 title")
        if not root_cause:
            raise DiagnosisFormatError("诊断输出缺少 root_cause")
        return cls(
            title=title,
            root_cause=root_cause,
            severity=_text(payload.get("severity")),
            time_window=_text(payload.get("time_window")),
            affected_summary=_text(payload.get("affected_summary")),
            # taskId 一律原样保留：skill 的输出标准写死了「绝不截断成前 8 位」。
            affected=_dict_list(
                payload.get("affected"), ("time", "uid", "taskId", "note")
            ),
            user_impact=_text(payload.get("user_impact")),
            timeline=_string_list(payload.get("timeline")),
            why=_dict_list(payload.get("why"), ("point", "code", "log")),
            ruled_out=_string_list(payload.get("ruled_out")),
            suggestion=_string_list(payload.get("suggestion")),
        )

    def speech_line(self) -> str:
        """机器人要播报的一句话。屏幕和喇叭都塞不下整份诊断，只取第一句根因。"""
        first_sentence = re.split(r"[。！!？?\n]", self.root_cause)[0].strip()
        body = first_sentence or self.title
        if len(body) > 40:
            body = body[:39] + "…"
        return f"查清了：{body}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "severity": self.severity,
            "time_window": self.time_window,
            "affected_summary": self.affected_summary,
            "affected": list(self.affected),
            "user_impact": self.user_impact,
            "timeline": list(self.timeline),
            "why": list(self.why),
            "ruled_out": list(self.ruled_out),
            "root_cause": self.root_cause,
            "suggestion": list(self.suggestion),
        }


@dataclass
class RelayRecord:
    alert_id: str
    event: AlertEvent
    created_at: float
    state: RelayState = RelayState.RECEIVED
    updated_at: float = 0.0
    repeat_count: int = 0
    last_seen_at: float = 0.0
    robot_delivered: bool = False
    robot_error: str = ""
    feishu_message_id: str = ""
    feishu_chat_id: str = ""
    feishu_error: str = ""
    claimed_by: str = ""
    reply_text: str = ""
    diagnosis: Diagnosis | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.updated_at:
            self.updated_at = self.created_at
        if not self.last_seen_at:
            self.last_seen_at = self.created_at

    def transition(self, target: RelayState, *, at: float, note: str = "") -> None:
        if not can_transition(self.state, target):
            raise ValueError(f"非法状态流转: {self.state.value} -> {target.value}")
        self.state = target
        self.updated_at = at
        self.history.append({"state": target.value, "at": at, "note": note})

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "repeat_count": self.repeat_count,
            "alert": self.event.to_dict(),
            "robot_delivered": self.robot_delivered,
            "robot_error": self.robot_error,
            "feishu_message_id": self.feishu_message_id,
            "feishu_chat_id": self.feishu_chat_id,
            "feishu_error": self.feishu_error,
            "claimed_by": self.claimed_by,
            "reply_text": self.reply_text,
            "diagnosis": self.diagnosis.to_dict() if self.diagnosis else None,
            "error": self.error,
            "warnings": list(self.warnings),
            "history": list(self.history),
        }
