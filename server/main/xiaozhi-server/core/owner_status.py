"""主人声明状态存储。

presence（摄像头）回答"人是否物理在场"；本模块回答"主人自己宣称的状态"：
在岗 / 会议中 / 稍后回来 / 请假。两者共同决定来访者应答文案、返岗汇总
口径与打断策略。

与 presence 的关键区别：声明状态跨越连接与进程生命周期（"明天请假"必须
活过服务端重启），因此落盘到 JSON；presence 是易失观测态，重启即重建。

对外文案边界（需求文档流程四/八）：来访者只能听到 public_note 与预计返回
时间；会议主题、请假原因等一律不出本模块——set 的时候就不该传进来。

配置段（config.yaml，本地覆盖走 data/.config.yaml）::

    owner_status:
      persist_path: data/owner_status.json

状态语义：
- available     默认在岗。
- meeting       会议中，expected_return 为预计返回的 HH:MM 或 ISO 时间。
- away          稍后回来（短暂离开），expected_return 可选。
- leave         请假：leave_start / leave_end（YYYY-MM-DD，含当天）。
                到 leave_end 次日自动回落 available（get() 时惰性求值）。
- meeting/away 到了 expected_return 不自动清除，get() 标 overdue=True，
  由提醒编排负责"到点问主人一句"，避免状态无限保留（流程四·状态过期）。
"""

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any, Callable, Optional

TAG = __name__

STATUS_AVAILABLE = "available"
STATUS_MEETING = "meeting"
STATUS_AWAY = "away"
STATUS_LEAVE = "leave"

VALID_STATES = frozenset(
    {STATUS_AVAILABLE, STATUS_MEETING, STATUS_AWAY, STATUS_LEAVE}
)

# 来访者听到的默认对外文案；set_status 未提供 public_note 时按状态取
DEFAULT_PUBLIC_NOTES = {
    STATUS_AVAILABLE: "他在工位附近",
    STATUS_MEETING: "他正在开会",
    STATUS_AWAY: "他暂时离开，稍后回来",
    STATUS_LEAVE: "他今天请假，不在工位",
}


@dataclass
class OwnerStatusRecord:
    state: str = STATUS_AVAILABLE
    public_note: str = ""
    # 预计返回时刻，ISO 格式（本地时区 naive），meeting/away 可选
    expected_return: Optional[str] = None
    # 请假起止日期，YYYY-MM-DD，含当天；leave 状态必填 start，end 缺省同 start
    leave_start: Optional[str] = None
    leave_end: Optional[str] = None
    set_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


class OwnerStatusStore:
    """单主人状态的线程安全存储。

    语音函数跑在会话线程、HTTP 跑在事件循环、观察者跑在推理线程，
    读写都可能并发，全部走锁；落盘为原子替换（先写 .tmp 再 rename）。
    clock 可注入，便于测试请假过期与 overdue 判定。
    """

    def __init__(
        self,
        persist_path: str | Path = "data/owner_status.json",
        *,
        clock: Optional[Callable[[], datetime]] = None,
        logger=None,
    ) -> None:
        self._path = Path(persist_path)
        self._clock = clock or datetime.now
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._record = self._load()

    # ---------- 读 ----------

    def get(self) -> dict[str, Any]:
        """返回当前有效状态。

        惰性处理两件事：请假到期自动回落 available（并落盘），
        meeting/away 超过 expected_return 标 overdue=True（不改状态本身）。
        """
        with self._lock:
            record = self._effective_locked()
            payload = record.to_payload()
        payload["overdue"] = self._is_overdue(record)
        payload["public_note"] = record.public_note or DEFAULT_PUBLIC_NOTES.get(
            record.state, ""
        )
        return payload

    def is_available(self) -> bool:
        return self.get()["state"] == STATUS_AVAILABLE

    # ---------- 写 ----------

    def set_status(
        self,
        state: str,
        *,
        public_note: str = "",
        expected_return: Optional[str] = None,
        leave_start: Optional[str] = None,
        leave_end: Optional[str] = None,
    ) -> dict[str, Any]:
        """设置声明状态；非法输入抛 ValueError，调用方翻译成用户话术。"""
        if state not in VALID_STATES:
            raise ValueError(f"未知状态: {state}")
        if state == STATUS_LEAVE:
            start = _parse_date(leave_start)
            if start is None:
                raise ValueError("请假必须提供 leave_start (YYYY-MM-DD)")
            end = _parse_date(leave_end) or start
            if end < start:
                raise ValueError("leave_end 不能早于 leave_start")
            leave_start, leave_end = start.isoformat(), end.isoformat()
        else:
            leave_start = leave_end = None
            if expected_return is not None and _parse_iso(expected_return) is None:
                raise ValueError("expected_return 必须是 ISO 时间")

        record = OwnerStatusRecord(
            state=state,
            public_note=str(public_note or ""),
            expected_return=expected_return,
            leave_start=leave_start,
            leave_end=leave_end,
            set_at=self._clock().isoformat(timespec="seconds"),
        )
        with self._lock:
            self._record = record
            self._persist_locked()
        return self.get()

    def clear(self) -> dict[str, Any]:
        """回到默认在岗态。"""
        return self.set_status(STATUS_AVAILABLE)

    # ---------- 内部 ----------

    def _effective_locked(self) -> OwnerStatusRecord:
        record = self._record
        if record.state == STATUS_LEAVE:
            end = _parse_date(record.leave_end)
            if end is not None and self._clock().date() > end:
                # 假期结束自动恢复：流程八「到期后自动恢复正常状态」
                record = OwnerStatusRecord(
                    set_at=self._clock().isoformat(timespec="seconds")
                )
                self._record = record
                self._persist_locked()
        return record

    def _is_overdue(self, record: OwnerStatusRecord) -> bool:
        if record.state not in (STATUS_MEETING, STATUS_AWAY):
            return False
        expected = _parse_iso(record.expected_return)
        return expected is not None and self._clock() > expected

    def _load(self) -> OwnerStatusRecord:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            record = OwnerStatusRecord(**{
                key: data.get(key)
                for key in OwnerStatusRecord.__dataclass_fields__
                if key in data
            })
            if record.state not in VALID_STATES:
                raise ValueError(f"落盘状态非法: {record.state}")
            return record
        except FileNotFoundError:
            return OwnerStatusRecord()
        except Exception:
            # 损坏的落盘文件当作没有：宁可回到默认在岗，也不要崩在启动路径
            self._logger.warning(f"读取主人状态文件失败，按默认在岗处理: {self._path}")
            return OwnerStatusRecord()

    def _persist_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._record.to_payload(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception:
            # 落盘失败只影响重启后的恢复，不影响本次会话内的行为
            self._logger.exception("主人状态落盘失败")


def _read_persist_path(config: dict) -> str:
    section = (config or {}).get("owner_status") or {}
    if not isinstance(section, dict):
        section = {}
    return str(section.get("persist_path") or "data/owner_status.json")


_store: Optional[OwnerStatusStore] = None
_store_lock = threading.Lock()


def get_owner_status_store(config: Optional[dict] = None) -> OwnerStatusStore:
    """进程级单例。语音函数与 HTTP 各自入口都从这里拿，保证看到同一份状态。"""
    global _store
    with _store_lock:
        if _store is None:
            _store = OwnerStatusStore(_read_persist_path(config or {}))
        return _store


def reset_owner_status_store() -> None:
    """仅测试用：清掉单例，避免用例间串台。"""
    global _store
    with _store_lock:
        _store = None
