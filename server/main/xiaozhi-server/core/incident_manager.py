"""线上告警接入、播报降噪与恢复观察（需求文档流程七）。

外部监控把告警 POST 到 /xiaozhi/incident/webhook，本模块负责决定
「这一条到底要不要打断主人」，以及故障从爆发到恢复的全过程记账：

- P0/P1 立即播报（打断），P2/P3 一律不出声，只经 on_low_severity 回调交给
  离席台账 / 日终摘要——需求里「低级别不直接播报」的落点就是这个回调。
- 同一 incident_id 在 dedup_cooldown_s 内重复上报只累计 repeat_count，不再播报。
  监控系统对同一故障通常每 30~60 秒重发一次，不降噪的话机器人会连续喊几十遍。
- 收到 resolved 不立刻宣布恢复：先进入 observe_seconds 的观察窗，窗内又 firing
  说明只是抖动（错误率在阈值上下摆动），取消恢复并重新告警；窗口安静走完才播报
  恢复并把故障时间线定稿。
- simulated=true 的事件，播报文案一律带「模拟」前缀：演示时必须让在场的人
  一耳朵听出这不是真故障。

诊断只读：start_diagnosis() 起的是 DiagnosisRunner（claude -p 无头模式，只给
Read/Grep/Glob 这类只读工具），结论只播报事实与建议，任何生产操作都由人来做。

状态放内存 + 时间线落盘（data/incidents/{date}-{incident_id}.json）：内存丢了
只影响正在观察中的窗口，已经发生过的事件序列在盘上；落盘用「先写 .tmp 再 rename」
的原子替换（同 core/owner_status.py），进程被 kill 也不会留半截 JSON。

配置段（config.yaml，本地覆盖走 data/.config.yaml）::

    incident:
      dedup_cooldown_s: 120     # 同一故障多久之内不重复播报
      observe_seconds: 300      # 收到 resolved 后再观察多久才宣布恢复
      storage_dir: data/incidents
      device_id: ""             # 播报到哪台设备，留空则取 presence_robot.workstations 第一条
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

TAG = __name__

# ---------------------------------------------------------------- 常量

SEVERITIES = ("P0", "P1", "P2", "P3")
# 播报优先级：数字越小越该被关注，active_incident() 按它排序
SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
# 只有这两级会打断主人。P2/P3 走 on_low_severity 进摘要
ANNOUNCE_SEVERITIES = frozenset({"P0", "P1"})

# webhook 入参的 status
INBOUND_FIRING = "firing"
INBOUND_RESOLVED = "resolved"

# 故障记录自身的状态机：燃烧中 → 恢复观察中 → 已恢复
STATUS_FIRING = "firing"
STATUS_OBSERVING = "observing"
STATUS_RECOVERED = "recovered"

DEFAULT_DEDUP_COOLDOWN_S = 120.0
DEFAULT_OBSERVE_SECONDS = 300.0
DEFAULT_STORAGE_DIR = "data/incidents"

SIMULATED_PREFIX = "【模拟】"

# 固件按字符串查表渲染表情，取值必须落在它支持的表里
# （firmware/main/boards/echoear/emote_display.cc）。
EMOTION_ALERT = "shocked"
EMOTION_RECOVERED = "relaxed"
EMOTION_DIAGNOSIS = "thinking"
EMOTION_DIAGNOSIS_FAILED = "confused"
ACTION_ALERT = "look_up"

STATUS_TEXT_RECOVERED = "告警恢复"
STATUS_TEXT_DIAGNOSIS = "故障诊断"

DIAGNOSIS_ACK = "正在检查日志、监控和最近发布"
DIAGNOSIS_BUSY = "这个故障的诊断还在跑，稍等一下"

# 时间线事件名。落盘后是给人看的，别改字面量
EVENT_RECEIVED = "received"
EVENT_ANNOUNCED = "announced"
EVENT_MERGED = "merged"
EVENT_DIAGNOSIS_STARTED = "diagnosis_started"
EVENT_DIAGNOSIS_RESULT = "diagnosis_result"
EVENT_RESOLVED_REPORTED = "resolved_reported"
EVENT_REIGNITED = "reignited"
EVENT_RECOVERED = "recovered"

# incident_id 会直接拼进文件名，必须先过滤：监控系统的 id 里带 "/" 或 ".."
# 就能把时间线写到 data/incidents 之外去。
_ID_SAFE = re.compile(r"[^A-Za-z0-9_.:-]")
_MAX_ID_LEN = 64


# ---------------------------------------------------------------- 默认实现


async def _default_push_event(conn, text: str, **kwargs) -> bool:
    from core.handle.pushHandle import push_work_event

    return await push_work_event(conn, text=text, **kwargs)


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


async def _maybe_await(result):
    """回调既可以是普通函数也可以是协程函数，集成方两种都写得出来。"""
    if inspect.isawaitable(result):
        return await result
    return result


def _stable_incident_id(service: str, title: str) -> str:
    """service+title 的稳定哈希。

    监控不给 incident_id 时用它归并：同一个服务的同一条告警规则，
    每次重发都会算出同一个 id，dedup 才有依据。
    """
    digest = hashlib.sha1(f"{service}|{title}".encode("utf-8")).hexdigest()
    return f"{_safe_id(service)}-{digest[:8]}"


def _safe_id(value: str) -> str:
    cleaned = _ID_SAFE.sub("_", str(value or "").strip())
    return cleaned[:_MAX_ID_LEN] or "unknown"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _seconds_between(later: Optional[str], earlier: Optional[str]) -> Optional[float]:
    a, b = _parse_iso(later), _parse_iso(earlier)
    if a is None or b is None:
        return None
    return (a - b).total_seconds()


def _duration_text(seconds: float) -> str:
    """观察窗时长的口播说法。

    默认 300 秒说成「5分钟」。演示时会把窗口压到几十秒，这时按秒说——
    压成 30 秒却播「连续1分钟没有新增异常」，是在台上说一句假话。
    """
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(round(seconds)) or 1}秒"
    return f"{int(round(seconds / 60.0))}分钟"


# ---------------------------------------------------------------- 配置


class _Settings:
    __slots__ = ("dedup_cooldown_s", "observe_seconds", "storage_dir", "device_id")

    def __init__(self, config: Optional[dict]) -> None:
        section = (config or {}).get("incident") or {}
        if not isinstance(section, dict):
            section = {}
        self.dedup_cooldown_s = _positive_float(
            section.get("dedup_cooldown_s"), DEFAULT_DEDUP_COOLDOWN_S
        )
        self.observe_seconds = _positive_float(
            section.get("observe_seconds"), DEFAULT_OBSERVE_SECONDS
        )
        self.storage_dir = str(section.get("storage_dir") or DEFAULT_STORAGE_DIR)
        self.device_id = str(section.get("device_id") or "").strip()


# ---------------------------------------------------------------- 主体


class IncidentManager:
    """告警状态机。

    推送函数、设备解析、低级别回调、时钟与 sleep 全部可注入：单测不碰真机，
    也不真的等 5 分钟观察窗。生产用默认实现走 pushHandle + device_registry。
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        device_registry=None,
        *,
        push_event: Optional[Callable] = None,
        device_resolver: Optional[Callable[[], Any]] = None,
        on_low_severity: Optional[Callable[[dict], Any]] = None,
        diagnosis_runner=None,
        clock: Optional[Callable[[], datetime]] = None,
        sleep: Optional[Callable[[float], Any]] = None,
        storage_dir: Optional[str | Path] = None,
        logger=None,
    ) -> None:
        self._config = config
        self._device_registry = device_registry
        self._push_event = push_event or _default_push_event
        self._device_resolver = device_resolver
        self._on_low_severity = on_low_severity
        self._diagnosis_runner = diagnosis_runner
        self._clock = clock or datetime.now
        self._sleep = sleep or asyncio.sleep
        self._storage_dir_override = Path(storage_dir) if storage_dir else None
        # 模块级单例建起来时还没有 loguru，先挂标准库 logger，bind 时换成服务端那个
        self._logger = logger or logging.getLogger(__name__)
        self._logger_bound = logger is not None

        self._incidents: Dict[str, dict] = {}
        self._observe_tasks: Dict[str, asyncio.Task] = {}
        self._diagnosis_tasks: Dict[str, asyncio.Task] = {}
        # 事件循环只对任务持弱引用，不留句柄会被 GC 提前回收（同 pomodoro_manager）
        self._tasks: set = set()

    # ------------------------------------------------------------ 装配

    def bind(
        self, config: Optional[dict] = None, device_registry=None, logger=None
    ) -> None:
        """补上生产环境的 config / 设备注册表 / logger，只填空缺不覆盖。"""
        if config is not None and self._config is None:
            self._config = config
        if device_registry is not None and self._device_registry is None:
            self._device_registry = device_registry
        if logger is not None and not self._logger_bound:
            self._logger = logger
            self._logger_bound = True

    def bind_from_conn(self, conn) -> None:
        """语音路径入口：从一条设备连接上取 config / 注册表 / logger。"""
        server = getattr(conn, "server", None)
        self.bind(
            config=getattr(conn, "config", None),
            device_registry=getattr(server, "device_registry", None),
            logger=getattr(conn, "logger", None),
        )

    def set_on_low_severity(self, callback: Optional[Callable[[dict], Any]]) -> None:
        """低级别告警的去向（离席台账 / 日终摘要），由集成方接。"""
        self._on_low_severity = callback

    def _settings(self) -> _Settings:
        return _Settings(self._config)

    @property
    def _storage_dir(self) -> Path:
        if self._storage_dir_override is not None:
            return self._storage_dir_override
        return Path(self._settings().storage_dir)

    # ------------------------------------------------------------ 入口

    async def handle_webhook(self, payload: dict) -> dict:
        """处理一条告警 webhook。

        入参非法抛 ValueError（HTTP 层翻成 400）。返回 outcome 说明这一条
        到底做了什么：announced / merged / low_severity / observing /
        already_observing / already_recovered / unknown。观察窗内的复燃按
        严重度重新分流（outcome 仍是 announced 或 low_severity），
        「取消恢复」这件事记在时间线的 reignited 事件里。
        """
        event = _normalize_payload(payload)
        self._prune_old_records()
        if event["status"] == INBOUND_RESOLVED:
            return await self._handle_resolved(event)
        return await self._handle_firing(event)

    # ------------------------------------------------------------ firing

    async def _handle_firing(self, event: dict) -> dict:
        settings = self._settings()
        now = self._now_iso()
        incident_id = event["incident_id"]
        record = self._incidents.get(incident_id)

        if record is None:
            record = self._new_record(event, now)
            self._incidents[incident_id] = record
            self._append_event(record, EVENT_RECEIVED, "首次收到告警")
            return await self._route_new_firing(record, settings)

        # 已知故障：先把事实更新上去，播不播报另说
        record["repeat_count"] = int(record.get("repeat_count", 0)) + 1
        record["last_seen_at"] = now
        self._refresh_facts(record, event)

        if record["state"] == STATUS_OBSERVING:
            # 观察窗里又烧起来了：这不是恢复，是抖动。取消恢复、重新告警。
            self._cancel_observation(incident_id)
            record["state"] = STATUS_FIRING
            record["resolved_at"] = None
            self._append_event(record, EVENT_REIGNITED, "恢复观察期内再次告警，取消恢复")
            return await self._route_new_firing(record, settings)

        if record["state"] == STATUS_RECOVERED:
            # 已经定稿的故障又复发：当成新一轮火灾重新开始计数
            record["state"] = STATUS_FIRING
            record["resolved_at"] = None
            record["recovered_at"] = None
            self._append_event(record, EVENT_REIGNITED, "已恢复的故障再次告警")
            return await self._route_new_firing(record, settings)

        # 仍在燃烧：冷却期内只累计次数，不再出声，也不再往摘要里塞一条
        # （监控对同一故障通常每 30~60 秒重发一次，不挡住的话摘要也会被刷屏）
        since = _seconds_between(now, record.get("last_notified_at"))
        if since is not None and since < settings.dedup_cooldown_s:
            self._persist(record)
            self._logger.info(
                f"告警 {incident_id} 在冷却期内重复上报（第 {record['repeat_count']} 次），不播报"
            )
            return self._result("merged", record, announced=False)

        return await self._route_new_firing(record, settings)

    async def _route_new_firing(self, record: dict, settings: _Settings) -> dict:
        """按严重度分流：P0/P1 打断播报，P2/P3 进摘要。

        两条路都会刷新 last_notified_at——冷却窗对播报与摘要一视同仁。
        """
        record["last_notified_at"] = self._now_iso()
        if record["severity"] in ANNOUNCE_SEVERITIES:
            announced = await self._announce_alert(record)
            self._persist(record)
            return self._result("announced", record, announced=announced)

        # 低级别：不出声，交给集成方（离席台账 / 日终摘要）
        snapshot = self.snapshot(record)
        self._append_event(record, EVENT_MERGED, "低级别告警，进摘要不播报")
        self._persist(record)
        await self._notify_low_severity(snapshot)
        return self._result("low_severity", record, announced=False)

    async def _announce_alert(self, record: dict) -> bool:
        text = self._alert_text(record)
        spoke = await self._push(
            text,
            emotion=EMOTION_ALERT,
            status=f"线上告警 {record['severity']}",
            action=ACTION_ALERT,
        )
        record["announced"] = True
        record["last_announced_at"] = self._now_iso()
        self._append_event(record, EVENT_ANNOUNCED, text)
        return spoke

    def _alert_text(self, record: dict) -> str:
        prefix = SIMULATED_PREFIX if record.get("simulated") else ""
        message = str(record.get("message") or "").strip()
        if message:
            return f"{prefix}线上告警：{record['title']}，{message}"
        return f"{prefix}线上告警：{record['title']}"

    async def _notify_low_severity(self, snapshot: dict) -> None:
        if self._on_low_severity is None:
            self._logger.info(
                f"低级别告警 {snapshot['incident_id']} 无摘要回调，仅记录时间线"
            )
            return
        try:
            await _maybe_await(self._on_low_severity(snapshot))
        except Exception as e:
            # 摘要侧出问题不该把 webhook 变成 500：告警本身已经记下来了
            self._logger.warning(f"低级别告警回调失败，已忽略: {e}")

    # ------------------------------------------------------------ resolved

    async def _handle_resolved(self, event: dict) -> dict:
        incident_id = event["incident_id"]
        record = self._incidents.get(incident_id)
        now = self._now_iso()

        if record is None:
            # 进程重启后只收到恢复信号：没有燃烧过程可收尾，记一笔就够，不播报
            self._logger.info(f"收到未知故障 {incident_id} 的恢复信号，忽略")
            return {
                "ok": True,
                "outcome": "unknown",
                "incident_id": incident_id,
                "announced": False,
                "incident": None,
            }

        record["last_seen_at"] = now
        if record["state"] == STATUS_RECOVERED:
            return self._result("already_recovered", record, announced=False)
        if record["state"] == STATUS_OBSERVING:
            # 监控重复发 resolved，观察窗不重开，否则永远等不到头
            self._persist(record)
            return self._result("already_observing", record, announced=False)

        settings = self._settings()
        record["state"] = STATUS_OBSERVING
        record["resolved_at"] = now
        record["observe_seconds"] = settings.observe_seconds
        self._append_event(
            record,
            EVENT_RESOLVED_REPORTED,
            f"指标恢复，进入 {int(settings.observe_seconds)} 秒观察窗",
        )
        self._persist(record)
        self._schedule_observation(record["incident_id"], settings.observe_seconds)
        return self._result("observing", record, announced=False)

    def _schedule_observation(self, incident_id: str, seconds: float) -> None:
        self._cancel_observation(incident_id)
        task = asyncio.create_task(self._observe(incident_id, seconds))
        self._observe_tasks[incident_id] = task
        self._track(task)

    def _cancel_observation(self, incident_id: str) -> None:
        task = self._observe_tasks.pop(incident_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _observe(self, incident_id: str, seconds: float) -> None:
        """观察窗：安静走完才宣布恢复。

        期间又收到 firing 时 _handle_firing 会 cancel 掉本任务；这里醒来后
        还要再确认一次状态，因为 cancel 可能恰好赶在任务已经被调度、正等着
        往下跑的时刻（同 pomodoro_manager 的代次检查）。
        """
        try:
            await self._sleep(seconds)
            record = self._incidents.get(incident_id)
            if record is None or record["state"] != STATUS_OBSERVING:
                return
            await self._finalize_recovery(record, seconds)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._logger.warning(f"故障 {incident_id} 的恢复观察异常，未播报恢复: {e}")
        finally:
            # 只摘掉自己那一条：复燃时 _cancel_observation 已经摘过，
            # 这里再无脑 pop 会把后来重排的那个任务的登记摘掉
            if self._observe_tasks.get(incident_id) is asyncio.current_task():
                self._observe_tasks.pop(incident_id, None)

    async def _finalize_recovery(self, record: dict, seconds: float) -> None:
        record["state"] = STATUS_RECOVERED
        record["recovered_at"] = self._now_iso()

        if record.get("announced"):
            text = self._recovery_text(record, seconds)
            await self._push(text, emotion=EMOTION_RECOVERED, status=STATUS_TEXT_RECOVERED)
            self._append_event(record, EVENT_RECOVERED, text)
        else:
            # 从没播报过的低级别故障，恢复也不该突然出声
            self._append_event(record, EVENT_RECOVERED, "指标恢复（未播报，低级别）")
        self._persist(record)
        self._logger.info(f"故障 {record['incident_id']} 已恢复，时间线已定稿")

    def _recovery_text(self, record: dict, seconds: float) -> str:
        prefix = SIMULATED_PREFIX if record.get("simulated") else ""
        return (
            f"{prefix}错误率已经恢复，连续{_duration_text(seconds)}没有新增异常。"
            "故障时间线我也记录好了。"
        )

    # ------------------------------------------------------------ 诊断

    async def start_diagnosis(self) -> Optional[str]:
        """对当前最该关注的故障起一次只读诊断。

        立刻返回一句给用户的确认语，真正的 claude -p 在后台跑（一次要几十秒，
        内联 await 会把语音回复卡到超时）。没有活跃故障时返回 None，
        由调用方决定怎么措辞。
        """
        record = self._active_record()
        if record is None:
            return None

        incident_id = record["incident_id"]
        running = self._diagnosis_tasks.get(incident_id)
        if running is not None and not running.done():
            return DIAGNOSIS_BUSY

        self._append_event(record, EVENT_DIAGNOSIS_STARTED, "启动只读诊断")
        self._persist(record)

        snapshot = self.snapshot(record)
        task = asyncio.create_task(self._run_diagnosis(snapshot))
        self._diagnosis_tasks[incident_id] = task
        self._track(task)
        return DIAGNOSIS_ACK

    async def _run_diagnosis(self, snapshot: dict) -> None:
        incident_id = snapshot["incident_id"]
        runner = self._runner()
        if runner is None:
            await self._on_diagnosis_result(
                incident_id, {"ok": False, "error": "没有可用的诊断执行器"}
            )
            return

        fired = False

        async def _callback(callback_incident_id: str, result: dict) -> None:
            nonlocal fired
            fired = True
            await self._on_diagnosis_result(callback_incident_id, result)

        try:
            result = await runner.run(snapshot, on_result=_callback)
        except Exception as e:
            # 子进程起不来（claude 不在 PATH、目录不存在）都会落在这里
            self._logger.warning(f"故障 {incident_id} 诊断执行失败: {e}")
            if not fired:
                await self._on_diagnosis_result(
                    incident_id, {"ok": False, "error": str(e)}
                )
            return

        # runner 已经回调过就不再重复处理，否则会播两遍
        if not fired:
            await self._on_diagnosis_result(
                incident_id, result or {"ok": False, "error": "诊断没有返回结果"}
            )

    async def _on_diagnosis_result(self, incident_id: str, result: dict) -> None:
        """诊断结束的统一落点：写时间线 + 播报结论。"""
        result = result or {}
        record = self._incidents.get(incident_id)
        ok = bool(result.get("ok"))
        summary = str(result.get("summary") or "").strip()
        error = str(result.get("error") or "").strip() or "未知原因"

        if ok and summary:
            text = f"诊断结果：{summary}"
            emotion = EMOTION_DIAGNOSIS
        else:
            text = f"诊断没有跑完：{error}"
            emotion = EMOTION_DIAGNOSIS_FAILED

        if record is not None:
            record["diagnosis"] = {
                "ok": ok and bool(summary),
                "summary": summary,
                "error": "" if (ok and summary) else error,
                "at": self._now_iso(),
            }
            self._append_event(record, EVENT_DIAGNOSIS_RESULT, text)
            self._persist(record)
        else:
            self._logger.warning(f"诊断结果找不到对应故障 {incident_id}，仅播报")

        await self._push(text, emotion=emotion, status=STATUS_TEXT_DIAGNOSIS)

    def _runner(self):
        if self._diagnosis_runner is None:
            from core.incident_diagnosis import DiagnosisRunner

            self._diagnosis_runner = DiagnosisRunner(
                self._config, logger=self._logger
            )
        return self._diagnosis_runner

    # ------------------------------------------------------------ 查询

    def _active_record(self) -> Optional[dict]:
        """当前最该关注的故障：未恢复的里面 P0>P1>P2>P3，同级取最近的。"""
        live = [
            record
            for record in self._incidents.values()
            if record["state"] != STATUS_RECOVERED
        ]
        if not live:
            return None
        # 先按严重度，同级再按「最近上报的排前面」（取负让 sort 保持升序写法）
        live.sort(
            key=lambda r: (
                SEVERITY_RANK.get(r["severity"], 9),
                -_epoch(r.get("last_seen_at")),
            )
        )
        return live[0]

    def active_incident(self) -> Optional[dict]:
        record = self._active_record()
        return self.snapshot(record) if record is not None else None

    def list_today(self) -> List[dict]:
        """今天的全部故障，供日终总结用。

        内存优先，再补上盘里今天但内存没有的（进程重启前发生的）。
        坏文件跳过：日终总结不该因为一个半截 JSON 就整个报不出来。
        """
        today = self._clock().strftime("%Y-%m-%d")
        merged: Dict[str, dict] = {}

        for path in sorted(self._storage_dir.glob(f"{today}-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self._logger.warning(f"故障时间线文件损坏，已跳过: {path}")
                continue
            incident_id = str((data or {}).get("incident_id") or "").strip()
            if incident_id:
                merged[incident_id] = data

        for incident_id, record in self._incidents.items():
            if str(record.get("first_seen_at", ""))[:10] == today:
                merged[incident_id] = self.snapshot(record)

        return sorted(merged.values(), key=lambda r: str(r.get("first_seen_at") or ""))

    def snapshot(self, record: dict) -> dict:
        """给 HTTP / 语音 / 落盘共用的一份可 JSON 序列化的拷贝。"""
        data = json.loads(json.dumps(record, ensure_ascii=False, default=str))
        data["observing"] = record["state"] == STATUS_OBSERVING
        data["recovered"] = record["state"] == STATUS_RECOVERED
        return data

    async def wait_idle(self) -> None:
        """等所有后台任务（观察窗、诊断）跑完。测试用，生产路径不调。"""
        while True:
            pending = [task for task in list(self._tasks) if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def shutdown(self) -> None:
        """取消所有后台任务（进程退出 / 测试收尾）。"""
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        self._observe_tasks.clear()
        self._diagnosis_tasks.clear()

    # ------------------------------------------------------------ 内部

    def _prune_old_records(self) -> None:
        """丢掉一天前就已恢复的记录。

        服务端是长跑进程，内存里留着上周的故障除了占地方没有用；
        时间线在盘上，list_today 也是按当天的文件读，丢了不影响复盘。
        """
        cutoff = _epoch(self._now_iso()) - 86400
        stale = [
            incident_id
            for incident_id, record in self._incidents.items()
            if record["state"] == STATUS_RECOVERED
            and _epoch(record.get("recovered_at")) < cutoff
        ]
        for incident_id in stale:
            self._incidents.pop(incident_id, None)
            self._observe_tasks.pop(incident_id, None)
            self._diagnosis_tasks.pop(incident_id, None)

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _now_iso(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _new_record(self, event: dict, now: str) -> dict:
        return {
            "incident_id": event["incident_id"],
            "service": event["service"],
            "severity": event["severity"],
            "title": event["title"],
            "message": event["message"],
            "metric": event["metric"],
            "value": event["value"],
            "source": event["source"],
            "simulated": event["simulated"],
            "started_at": event["started_at"] or now,
            "first_seen_at": now,
            "last_seen_at": now,
            "state": STATUS_FIRING,
            "repeat_count": 1,
            "announced": False,
            "last_announced_at": None,
            # 上一次「对外做了点什么」（播报或进摘要）的时刻，dedup 冷却按它算
            "last_notified_at": None,
            "resolved_at": None,
            "recovered_at": None,
            "observe_seconds": None,
            "diagnosis": None,
            "timeline": [],
        }

    def _refresh_facts(self, record: dict, event: dict) -> None:
        """重复上报时更新会变的事实：数值、文案、严重度升级。"""
        if event["value"] is not None:
            record["value"] = event["value"]
        if event["message"]:
            record["message"] = event["message"]
        if event["metric"]:
            record["metric"] = event["metric"]
        # 严重度只升不降：P2 升 P0 要能立刻打断，P0 降 P2 不该让后续告警变哑
        if SEVERITY_RANK.get(event["severity"], 9) < SEVERITY_RANK.get(
            record["severity"], 9
        ):
            record["severity"] = event["severity"]
        # 模拟标记只增不减：一旦某次上报说了是模拟，整条故障都按模拟播报
        record["simulated"] = bool(record.get("simulated")) or event["simulated"]

    def _append_event(self, record: dict, event: str, detail: str = "") -> None:
        record["timeline"].append(
            {"at": self._now_iso(), "event": event, "detail": str(detail or "")}
        )

    def _result(self, outcome: str, record: dict, announced: bool) -> dict:
        return {
            "ok": True,
            "outcome": outcome,
            "incident_id": record["incident_id"],
            "announced": bool(announced),
            "incident": self.snapshot(record),
        }

    # ---------- 推送 ----------

    def _resolve_conn(self):
        """找到该播报的那台设备。

        优先 incident.device_id，其次 presence_robot.workstations 的第一条映射；
        都没配就退回「唯一在线的那台」——单机器人的演示场景下这是对的，
        多台设备时宁可不猜（会打扰到别人的工位）。
        """
        if self._device_resolver is not None:
            return self._device_resolver()
        if self._device_registry is None:
            return None

        settings = self._settings()
        if settings.device_id:
            return self._device_registry.get(settings.device_id)

        workstations = ((self._config or {}).get("presence_robot") or {}).get(
            "workstations"
        ) or {}
        if isinstance(workstations, dict):
            for _, device_id in workstations.items():
                device_id = str(device_id or "").strip()
                if device_id:
                    return self._device_registry.get(device_id)

        try:
            online = list(self._device_registry.device_ids())
        except Exception:
            online = []
        if len(online) == 1:
            return self._device_registry.get(online[0])
        if online:
            self._logger.warning(
                "有多台设备在线但没配 incident.device_id / presence_robot.workstations，"
                "本次告警不播报"
            )
        return None

    async def _push(
        self,
        text: str,
        *,
        emotion: str,
        status: str,
        action: Optional[str] = None,
    ) -> bool:
        conn = self._resolve_conn()
        if conn is None:
            self._logger.info(f"没有可播报的设备，本次告警只记录: {text}")
            return False
        try:
            return bool(
                await self._push_event(
                    conn,
                    text=text,
                    emotion=emotion,
                    status=status,
                    speak=True,
                    action=action,
                )
            )
        except Exception as e:
            # 推送失败不能让状态机停摆：故障记录本身比播报更重要
            self._logger.warning(f"告警播报失败，已降级为仅记录: {e}")
            return False

    # ---------- 落盘 ----------

    def _record_path(self, record: dict) -> Path:
        day = str(record.get("first_seen_at") or "")[:10] or self._clock().strftime(
            "%Y-%m-%d"
        )
        return self._storage_dir / f"{day}-{record['incident_id']}.json"

    def _persist(self, record: dict) -> None:
        """原子写：先写 .tmp 再 rename，进程被 kill 也不会留半截 JSON。"""
        path = self._record_path(record)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.snapshot(record), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as e:
            # 落盘失败只影响事后复盘，不影响这一轮播报
            self._logger.warning(f"故障时间线落盘失败: {e}")


def _epoch(value: Optional[str]) -> float:
    """ISO 时间转秒。解析不了当作很久以前，排序时自然沉到后面。"""
    parsed = _parse_iso(value)
    if parsed is None:
        return 0.0
    return parsed.timestamp()


# ---------------------------------------------------------------- 校验


def _normalize_payload(payload: Any) -> dict:
    """把 webhook 入参规范化，非法输入抛 ValueError（HTTP 层翻 400）。

    严重度与 status 必须是枚举值：写错了会让「该打断的没打断」，
    这种错误必须当场报出来，不能默默按默认值放行。
    started_at 反过来宽容：格式怪的时间戳只影响时间线好不好看，
    为它拒收一条 P0 告警是本末倒置。
    """
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是 JSON 对象")

    service = str(payload.get("service") or "").strip()
    if not service:
        raise ValueError("service 不能为空")

    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("title 不能为空")

    severity = str(payload.get("severity") or "").strip().upper()
    if severity not in SEVERITIES:
        raise ValueError(f"severity 必须是 P0/P1/P2/P3，收到: {payload.get('severity')!r}")

    status = str(payload.get("status") or INBOUND_FIRING).strip().lower()
    if status not in (INBOUND_FIRING, INBOUND_RESOLVED):
        raise ValueError(f"status 必须是 firing 或 resolved，收到: {payload.get('status')!r}")

    raw_id = str(payload.get("incident_id") or "").strip()
    incident_id = _safe_id(raw_id) if raw_id else _stable_incident_id(service, title)

    started_at = payload.get("started_at")
    started_iso = None
    if started_at not in (None, ""):
        parsed = _parse_iso(str(started_at))
        if parsed is not None:
            started_iso = parsed.isoformat(timespec="seconds")

    return {
        "incident_id": incident_id,
        "service": service,
        "severity": severity,
        "title": title,
        "message": str(payload.get("message") or "").strip(),
        "metric": str(payload.get("metric") or "").strip(),
        "value": payload.get("value"),
        "source": str(payload.get("source") or "").strip(),
        "simulated": bool(payload.get("simulated", False)),
        "status": status,
        "started_at": started_iso,
    }


# ---------------------------------------------------------------- 单例

_manager: Optional[IncidentManager] = None


def get_incident_manager(config: Optional[dict] = None, **kwargs) -> IncidentManager:
    """进程级单例。

    HTTP webhook、语音函数、日终总结必须看到同一份故障状态，
    否则「启动诊断」会找不到刚刚播报过的那条告警。
    """
    global _manager
    if _manager is None:
        _manager = IncidentManager(config, **kwargs)
    elif config is not None:
        _manager.bind(config=config)
    return _manager


def reset_incident_manager() -> None:
    """仅测试用：清掉单例，避免用例间串台。"""
    global _manager
    if _manager is not None:
        _manager.shutdown()
    _manager = None
