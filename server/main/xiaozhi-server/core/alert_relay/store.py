"""告警值班中继记录的落盘。

记录只放内存的代价是真实的：服务端一重启，飞书里那张卡片还挂着，人回一句
「帮我查」，回调按 alert_id 查不到记录就只能回 ALERT_NOT_FOUND，诊断结论
也再回不到那条话题下面；超时巡检的基线一并丢失，重启前已经等了 9 分钟的
告警，重启后要从零再等 10 分钟。

落盘沿用 away_ledger / incident_manager 的「先写 .tmp 再 rename」原子替换。
粒度是**整份重写一个 JSON**：记录数被 max_records 夹在 200 条以内，
一条也就几 KB，整份重写比一条一个文件更省事、也更难写错。

刻意不复用 RelayRecord.to_dict()：那是给 HTTP 看的视图，AlertEvent 的
`raw_text` 不在里面——而它正是诊断子进程的输入，丢了就等于记录白存。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Optional

from core.alert_relay.models import (
    AlertEvent,
    Diagnosis,
    DiagnosisFormatError,
    RelayRecord,
    RelayState,
)


# AlertEvent 是 frozen dataclass，字段名即落盘键名，两边共用这一份清单
_EVENT_FIELDS = (
    "raw_text",
    "level",
    "cluster",
    "namespace",
    "target",
    "workload",
    "keyword",
    "alert_time",
    "policy_url",
    "project_id",
    "cluster_id",
    "rule",
)


def event_payload(event: AlertEvent) -> dict[str, Any]:
    return {key: getattr(event, key, "") for key in _EVENT_FIELDS}


def event_from_payload(payload: Any) -> Optional[AlertEvent]:
    if not isinstance(payload, dict):
        return None
    raw_text = str(payload.get("raw_text") or "")
    if not raw_text:
        # 没有原文的记录复原出来也没法送去诊断，当脏数据丢掉
        return None
    kwargs = {key: str(payload.get(key) or "") for key in _EVENT_FIELDS}
    kwargs["raw_text"] = raw_text
    if not kwargs["level"]:
        kwargs.pop("level")
    return AlertEvent(**kwargs)


def record_payload(record: RelayRecord) -> dict[str, Any]:
    return {
        "alert_id": record.alert_id,
        "state": record.state.value,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_seen_at": record.last_seen_at,
        "repeat_count": record.repeat_count,
        "robot_delivered": record.robot_delivered,
        "robot_error": record.robot_error,
        "feishu_message_id": record.feishu_message_id,
        "feishu_chat_id": record.feishu_chat_id,
        "feishu_error": record.feishu_error,
        "claimed_by": record.claimed_by,
        "reply_text": record.reply_text,
        "error": record.error,
        "warnings": list(record.warnings),
        "history": list(record.history),
        "diagnosis": record.diagnosis.to_dict() if record.diagnosis else None,
        "event": event_payload(record.event),
    }


def record_from_payload(payload: Any) -> Optional[RelayRecord]:
    """把一条落盘记录还原成 RelayRecord；数据不可用时返回 None。

    单条坏记录只丢它自己，不该让整份存储读不出来（同 incident_manager
    对损坏时间线文件的处理）。
    """
    if not isinstance(payload, dict):
        return None
    alert_id = str(payload.get("alert_id") or "").strip()
    event = event_from_payload(payload.get("event"))
    if not alert_id or event is None:
        return None
    try:
        state = RelayState(str(payload.get("state") or RelayState.RECEIVED.value))
    except ValueError:
        return None

    diagnosis = None
    if payload.get("diagnosis"):
        try:
            diagnosis = Diagnosis.from_payload(payload.get("diagnosis"))
        except DiagnosisFormatError:
            diagnosis = None

    created_at = _float(payload.get("created_at"))
    record = RelayRecord(
        alert_id=alert_id,
        event=event,
        created_at=created_at,
        state=state,
        updated_at=_float(payload.get("updated_at"), created_at),
        repeat_count=int(_float(payload.get("repeat_count"))),
        last_seen_at=_float(payload.get("last_seen_at"), created_at),
        robot_delivered=bool(payload.get("robot_delivered")),
        robot_error=str(payload.get("robot_error") or ""),
        feishu_message_id=str(payload.get("feishu_message_id") or ""),
        feishu_chat_id=str(payload.get("feishu_chat_id") or ""),
        feishu_error=str(payload.get("feishu_error") or ""),
        claimed_by=str(payload.get("claimed_by") or ""),
        reply_text=str(payload.get("reply_text") or ""),
        diagnosis=diagnosis,
        error=str(payload.get("error") or ""),
        warnings=_string_list(payload.get("warnings")),
        history=[item for item in (payload.get("history") or []) if isinstance(item, dict)],
    )
    return record


def _float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


class RelayRecordStore:
    """一个 JSON 文件里的全部中继记录。

    写失败只影响重启后的恢复，绝不能把异常抛给告警入口——记录不下来
    也比因为磁盘满了丢掉一条 P0 告警强。
    """

    def __init__(self, path: str | Path, logger: Any = None) -> None:
        self._path = Path(path)
        self._logger = logger or logging.getLogger(__name__)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[RelayRecord]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            # 损坏的落盘文件当作没有：宁可丢历史，也不要崩在启动路径上。
            # 后续写入会把文件整体覆盖掉，坏文件不会一直卡着（同 away_ledger）。
            self._logger.warning(f"读取告警中继记录失败，按空存储处理: {self._path}")
            return []
        raw = (data or {}).get("records") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return []
        records = []
        for item in raw:
            record = record_from_payload(item)
            if record is not None:
                records.append(record)
        return records

    def save(self, records: Iterable[RelayRecord]) -> None:
        payload = {
            "version": 1,
            "records": [record_payload(record) for record in records],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
        except Exception as exc:
            self._logger.warning(f"告警中继记录落盘失败: {exc}")
