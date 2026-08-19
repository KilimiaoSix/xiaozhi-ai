"""离席事件台账（需求文档流程四/五）。

主人不在工位时发生的事——Agent 任务结果、同事留言、告警——攒在这里，
等他回来一次性汇总播报。核心约束有四条：

1. **只有离席窗口内的事件才进待播报队列。** 主人在工位时 Agent 完成任务，
   他当场就看到了；回来再念一遍是噪音。不在窗口内的调用只进当日日志。
2. **同一任务只报最终结果。** Agent 一个任务会推「开始/需要确认/完成」多条，
   按 task_key 覆盖，播报时只剩最后那条，否则汇总里全是同一件事的流水账。
3. **播报过的不再重复。** mark_reported() 之后队列清空，第二次回来不会再念。
4. **留言不能因重启丢失。** 那是同事托付给机器人的事，落 JSON。

播报顺序（需求：严重告警 > 等待用户操作 > 已完成任务 > 留言 > 普通消息）
由 pending_summary() 保证，桶内保持时间先后。

与 owner_status 的分工：owner_status 是主人自己声明的状态（会议/请假），
本模块记的是「离席期间发生了什么」，两者都要有才能拼出返岗汇总与来访应答。

配置段（config.yaml，本地覆盖走 data/.config.yaml）::

    away_ledger:
      persist_path: data/away_ledger.json
      log_retention_days: 3
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

TAG = __name__

KIND_AGENT_COMPLETED = "agent_completed"
KIND_AGENT_FAILED = "agent_failed"
KIND_AGENT_NEEDS_USER = "agent_needs_user"
KIND_VISITOR_MESSAGE = "visitor_message"
KIND_INCIDENT = "incident"
KIND_GENERIC = "generic"

VALID_KINDS = frozenset(
    {
        KIND_AGENT_COMPLETED,
        KIND_AGENT_FAILED,
        KIND_AGENT_NEEDS_USER,
        KIND_VISITOR_MESSAGE,
        KIND_INCIDENT,
        KIND_GENERIC,
    }
)

SEVERITY_CRITICAL = "critical"
SEVERITY_NORMAL = "normal"

# 按 task_key 折叠的事件类型：同一个 Agent 任务的状态流转只该留最后一条
_AGENT_KINDS = frozenset(
    {KIND_AGENT_COMPLETED, KIND_AGENT_FAILED, KIND_AGENT_NEEDS_USER}
)

# 留言入账时统一带的前缀（visitor_flow 写入），计数句里要把它剥掉再拼
VISITOR_PREFIX = "有同事留言："

DEFAULT_PERSIST_PATH = "data/away_ledger.json"
DEFAULT_LOG_RETENTION_DAYS = 3

_BUCKET_CRITICAL = 0
_BUCKET_NEEDS_USER = 1
_BUCKET_AGENT_RESULT = 2
_BUCKET_VISITOR = 3
_BUCKET_GENERIC = 4


@dataclass
class AwayEvent:
    """一条离席期间发生的事。"""

    kind: str
    text: str
    task_key: str = ""
    severity: str = SEVERITY_NORMAL
    source: str = ""
    at: str = ""
    count: int = 1

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def _bucket_of(event: AwayEvent) -> int:
    if event.kind == KIND_INCIDENT and event.severity == SEVERITY_CRITICAL:
        return _BUCKET_CRITICAL
    if event.kind == KIND_AGENT_NEEDS_USER:
        return _BUCKET_NEEDS_USER
    if event.kind in (KIND_AGENT_COMPLETED, KIND_AGENT_FAILED):
        return _BUCKET_AGENT_RESULT
    if event.kind == KIND_VISITOR_MESSAGE:
        return _BUCKET_VISITOR
    # 未知 kind 与非严重告警一并算「普通消息」，排在最后，绝不丢
    return _BUCKET_GENERIC


# ---------------------------------------------------------------- 中文数字

_CN_DIGITS = "零一二三四五六七八九"


def _cn_int(value: int) -> str:
    """0..99 的中文读法；超出范围直接回退阿拉伯数字，播报不至于变成乱码。"""
    value = int(value)
    if value < 0:
        value = 0
    if value < 10:
        return _CN_DIGITS[value]
    if value < 20:
        remainder = value % 10
        return "十" + (_CN_DIGITS[remainder] if remainder else "")
    if value < 100:
        tens, remainder = divmod(value, 10)
        return _CN_DIGITS[tens] + "十" + (_CN_DIGITS[remainder] if remainder else "")
    return str(value)


def _cn_quantity(value: int) -> str:
    """量词前的读法：单独的 2 念「两」而不是「二」（两小时 / 两分钟）。"""
    return "两" if int(value) == 2 else _cn_int(value)


def _cn_duration(minutes: int) -> Optional[str]:
    """把分钟数读成中文时长；不足一分钟返回 None，由调用方换个说法。"""
    minutes = int(minutes)
    if minutes < 1:
        return None
    if minutes < 60:
        return f"{_cn_quantity(minutes)}分钟"
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return f"{_cn_quantity(hours)}小时"
    return f"{_cn_quantity(hours)}小时{_cn_int(rest)}分"


def _strip_visitor_prefix(text: str) -> str:
    return text[len(VISITOR_PREFIX):] if text.startswith(VISITOR_PREFIX) else text


def _trim_sentence_end(text: str) -> str:
    """去掉句尾标点，方便和外层的「。」「；」拼接时不出现叠标点。"""
    return text.rstrip("。！？.!?　 ")


class AwayLedger:
    """离席事件台账。

    读写来自三条线程：HTTP 事件推送在事件循环里、语音函数在会话线程里、
    在岗编排在推理线程里，因此全程持锁；落盘为原子替换（先写 .tmp 再 rename）。
    clock 可注入（返回 datetime），便于测试离席时长与跨日日志。
    """

    def __init__(
        self,
        persist_path: str | Path = DEFAULT_PERSIST_PATH,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        log_retention_days: int = DEFAULT_LOG_RETENTION_DAYS,
        logger=None,
    ) -> None:
        self._path = Path(persist_path)
        self._clock = clock or datetime.now
        self._log_retention_days = max(1, int(log_retention_days))
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._away_started_at: Optional[datetime] = None
        self._last_away_minutes: int = 0
        self._pending: list[AwayEvent] = []
        self._log: list[AwayEvent] = []
        self._load()

    # ---------- 离席窗口 ----------

    def is_away(self) -> bool:
        with self._lock:
            return self._away_started_at is not None

    @property
    def away_started_at(self) -> Optional[datetime]:
        with self._lock:
            return self._away_started_at

    def mark_away(self, at: Optional[datetime] = None) -> None:
        """标记主人离席。

        幂等：离岗判定挂在每条心跳上（presence 心跳 15 秒一条），重复调用
        不能把离席起点一路往后推，否则返岗时报出来的时长永远是 0。
        """
        with self._lock:
            if self._away_started_at is not None:
                return
            self._away_started_at = at or self._clock()
            self._persist_locked()

    def mark_returned(self, at: Optional[datetime] = None) -> None:
        """标记主人返岗，结束离席窗口。

        窗口关掉之前先把时长存下来：桌面端在返岗之后拉 /away/summary
        还要显示「刚才离开了多久」。
        """
        with self._lock:
            if self._away_started_at is None:
                return
            self._last_away_minutes = self._elapsed_minutes_locked(at)
            self._away_started_at = None
            self._persist_locked()

    def away_minutes(self, at: Optional[datetime] = None) -> int:
        with self._lock:
            if self._away_started_at is None:
                return self._last_away_minutes
            return self._elapsed_minutes_locked(at)

    def _elapsed_minutes_locked(self, at: Optional[datetime] = None) -> int:
        started = self._away_started_at
        if started is None:
            return self._last_away_minutes
        now = at or self._clock()
        return max(0, int((now - started).total_seconds() // 60))

    # ---------- 记账 ----------

    def record(
        self,
        kind: str,
        text: str,
        *,
        task_key: str = "",
        severity: str = SEVERITY_NORMAL,
        source: str = "",
        at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """记一条事件。

        无论是否在离席窗口内都进当日日志（供日终总结）；只有离席窗口内的
        才进待播报队列——主人在工位时发生的事他当场就看见了。
        """
        event = AwayEvent(
            kind=str(kind or KIND_GENERIC),
            text=str(text or "").strip(),
            task_key=str(task_key or ""),
            severity=str(severity or SEVERITY_NORMAL),
            source=str(source or ""),
            at=(at or self._clock()).isoformat(timespec="seconds"),
        )
        with self._lock:
            self._log.append(event)
            self._prune_log_locked()
            if self._away_started_at is not None:
                self._merge_into_pending_locked(event)
            self._persist_locked()
            return event.to_payload()

    def _merge_into_pending_locked(self, event: AwayEvent) -> None:
        existing = self._find_mergeable_locked(event)
        if existing is None:
            self._pending.append(event)
            return
        # 原地覆盖而不是追加：保住这条事项在队列里的原始时间位置，
        # 否则一个反复刷新的任务会一路挤到汇总末尾。
        if event.kind == KIND_INCIDENT:
            event.count = existing.count + 1
        index = self._pending.index(existing)
        self._pending[index] = event

    def _find_mergeable_locked(self, event: AwayEvent) -> Optional[AwayEvent]:
        if event.kind in _AGENT_KINDS and event.task_key:
            # 同一任务的状态流转只留最终结果（开始 → 完成只报完成）
            for candidate in self._pending:
                if candidate.kind in _AGENT_KINDS and candidate.task_key == event.task_key:
                    return candidate
            return None
        if event.kind == KIND_INCIDENT and event.severity == SEVERITY_CRITICAL:
            # 同一条严重告警刷屏时合并计数，不然一次抖动就把汇总占满
            for candidate in self._pending:
                if (
                    candidate.kind == KIND_INCIDENT
                    and candidate.severity == SEVERITY_CRITICAL
                    and candidate.text == event.text
                ):
                    return candidate
        return None

    # ---------- 查询 ----------

    def pending_summary(self, at: Optional[datetime] = None) -> dict[str, Any]:
        """按播报优先级排好序的待播报事项。"""
        with self._lock:
            # 稳定排序：桶间按优先级，桶内保持记账时的时间先后
            items = sorted(self._pending, key=_bucket_of)
            started = self._away_started_at
            return {
                "away": started is not None,
                "away_since": started.isoformat(timespec="seconds") if started else None,
                "away_minutes": self._elapsed_minutes_locked(at),
                "count": len(items),
                "items": [item.to_payload() for item in items],
            }

    def events_today(self, at: Optional[datetime] = None) -> list[dict[str, Any]]:
        """当日全部日志（含在岗期间发生的），供日终总结使用。"""
        today = (at or self._clock()).date()
        with self._lock:
            return [
                event.to_payload()
                for event in self._log
                if _event_date(event) == today
            ]

    def compose_speech(self, at: Optional[datetime] = None) -> Optional[str]:
        """把待播报事项组装成不超过三句的中文播报；没事项返回 None。

        三句是给 TTS 的硬上限：返岗汇总一口气念十条，用户只会记住最后一条。
        """
        summary = self.pending_summary(at)
        items = [AwayEvent(**item) for item in summary["items"]]
        if not items:
            return None

        clauses = _build_clauses(items)
        if not clauses:
            return None

        duration = _cn_duration(summary["away_minutes"])
        prefix = f"你离开的{duration}里，" if duration else "你离开这会儿，"

        sentences = [prefix + clauses[0] + "。"]
        if len(clauses) > 1:
            sentences.append(clauses[1] + "。")
        if len(clauses) > 2:
            # 剩下的全部并进第三句，句数不随事项数增长
            sentences.append("；".join(clauses[2:]) + "。")
        return "".join(sentences)

    def mark_reported(self) -> None:
        """清空待播报队列。当日日志保留——播报过不等于没发生过。"""
        with self._lock:
            if not self._pending:
                return
            self._pending = []
            self._persist_locked()

    # ---------- 落盘 ----------

    def _prune_log_locked(self) -> None:
        """日志只留最近几天：这个文件每条记账都要整体重写，不能无限长。"""
        cutoff = (self._clock() - timedelta(days=self._log_retention_days)).date()
        self._log = [event for event in self._log if _event_date(event) >= cutoff]

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("台账根节点不是对象")
            self._away_started_at = _parse_iso(data.get("away_started_at"))
            self._last_away_minutes = int(data.get("last_away_minutes") or 0)
            self._pending = _load_events(data.get("pending"))
            self._log = _load_events(data.get("log"))
        except FileNotFoundError:
            return
        except Exception:
            # 损坏的落盘文件当作没有：宁可丢历史，也不要崩在启动路径上。
            # 后续写入会把文件整体覆盖掉，坏文件不会一直卡着。
            self._logger.warning(f"读取离席台账失败，按空台账处理: {self._path}")
            self._away_started_at = None
            self._last_away_minutes = 0
            self._pending = []
            self._log = []

    def _persist_locked(self) -> None:
        payload = {
            "version": 1,
            "away_started_at": (
                self._away_started_at.isoformat(timespec="seconds")
                if self._away_started_at
                else None
            ),
            "last_away_minutes": self._last_away_minutes,
            "pending": [event.to_payload() for event in self._pending],
            "log": [event.to_payload() for event in self._log],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
        except Exception:
            # 落盘失败只影响重启后的恢复，本次会话内的内存态照常可用，
            # 绝不能把异常抛给记账方（可能是 HTTP 推送或语音回调）。
            self._logger.warning(f"离席台账落盘失败: {self._path}")


def _event_date(event: AwayEvent) -> date:
    parsed = _parse_iso(event.at)
    return parsed.date() if parsed else date.min


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _load_events(raw: Any) -> list[AwayEvent]:
    events: list[AwayEvent] = []
    if not isinstance(raw, list):
        return events
    fields = AwayEvent.__dataclass_fields__
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            events.append(
                AwayEvent(**{key: item[key] for key in fields if key in item})
            )
        except TypeError:
            continue
    return events


# ---------------------------------------------------------------- 播报组句

def _build_clauses(items: list[AwayEvent]) -> list[str]:
    """按桶生成短句，顺序即播报优先级。"""
    buckets: dict[int, list[AwayEvent]] = {}
    for item in items:
        buckets.setdefault(_bucket_of(item), []).append(item)

    clauses: list[str] = []

    critical = buckets.get(_BUCKET_CRITICAL, [])
    if critical:
        # count 是同一条告警的重复次数，条数按不同告警数算
        latest = _trim_sentence_end(critical[-1].text)
        if len(critical) == 1:
            clauses.append(f"有严重告警：{latest}")
        else:
            clauses.append(f"有 {len(critical)} 条严重告警，最新一条：{latest}")

    needs_user = buckets.get(_BUCKET_NEEDS_USER, [])
    if needs_user:
        latest = _trim_sentence_end(needs_user[-1].text)
        if len(needs_user) == 1:
            clauses.append(f"有个任务在等你确认：{latest}")
        else:
            clauses.append(
                f"有 {len(needs_user)} 个任务在等你确认，比如：{latest}"
            )

    results = buckets.get(_BUCKET_AGENT_RESULT, [])
    completed = [item for item in results if item.kind == KIND_AGENT_COMPLETED]
    failed = [item for item in results if item.kind == KIND_AGENT_FAILED]
    if completed:
        sources = {item.source for item in completed if item.source}
        who = f"{sources.pop()} " if len(sources) == 1 else ""
        clauses.append(f"{who}完成了 {len(completed)} 个任务")
    if failed:
        latest = _trim_sentence_end(failed[-1].text)
        if len(failed) == 1:
            clauses.append(f"有个任务失败了：{latest}")
        else:
            clauses.append(f"有 {len(failed)} 个任务失败了，比如：{latest}")

    visitors = buckets.get(_BUCKET_VISITOR, [])
    if visitors:
        latest = _trim_sentence_end(visitors[-1].text)
        if len(visitors) == 1:
            clauses.append(latest)
        else:
            clauses.append(
                f"有 {len(visitors)} 条留言，最近一条是{_strip_visitor_prefix(latest)}"
            )

    generic = buckets.get(_BUCKET_GENERIC, [])
    if generic:
        latest = _trim_sentence_end(generic[-1].text)
        if len(generic) == 1:
            clauses.append(latest)
        else:
            clauses.append(f"还有 {len(generic)} 条消息，最近一条是{latest}")

    return [clause for clause in clauses if clause]


# ---------------------------------------------------------------- 单例

def _read_settings(config: Optional[dict]) -> tuple[str, int]:
    section = (config or {}).get("away_ledger") or {}
    if not isinstance(section, dict):
        section = {}
    path = str(section.get("persist_path") or DEFAULT_PERSIST_PATH)
    try:
        retention = int(section.get("log_retention_days") or DEFAULT_LOG_RETENTION_DAYS)
    except (TypeError, ValueError):
        retention = DEFAULT_LOG_RETENTION_DAYS
    return path, retention


_ledger: Optional[AwayLedger] = None
_ledger_lock = threading.Lock()


def get_away_ledger(config: Optional[dict] = None) -> AwayLedger:
    """进程级单例。事件推送接口、来访流程、在岗编排都从这里拿同一份台账。"""
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            path, retention = _read_settings(config)
            _ledger = AwayLedger(path, log_retention_days=retention)
        return _ledger


def reset_away_ledger() -> None:
    """仅测试用：清掉单例，避免用例间串台。"""
    global _ledger
    with _ledger_lock:
        _ledger = None
