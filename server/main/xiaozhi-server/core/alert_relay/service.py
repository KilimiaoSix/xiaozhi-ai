"""告警值班中继的编排层。

一条告警的一生都在这里：接收 → 叫人（机器人 + 飞书）→ 等回复 → 调起本机
Claude Code 诊断 → 回帖结论。三条铁律：

1. **人不点头就不开跑。** 状态机不给 AWAITING_REPLY → DIAGNOSING 的边。
2. **一路通知挂了不能吞掉告警。** 机器人和飞书互不阻塞，失败只记 warning。
3. **查不出来就说查不出来。** 诊断失败回失败卡片，绝不编根因。

记录落盘（persist_path，生产由 factory 从 alert_relay.persist_path 注入）：
每次状态变迁写一次，启动时把上个进程的记录全部装回来，非终态的连同
去重指纹与超时基线一起重建——否则重启后飞书里那张卡片就成了孤儿，
人回「帮我查」只会撞 ALERT_NOT_FOUND。不注入路径就是纯内存（离线单测用）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from core.alert_relay.cards import (
    INTENT_DECLINE,
    INTENT_DIAGNOSE,
    build_alert_card,
    build_diagnosis_card,
    build_failure_card,
)
from core.alert_relay.models import AlertEvent, RelayRecord, RelayState
from core.alert_relay.parser import parse_alert
from core.alert_relay.store import RelayRecordStore


DEFAULT_DEDUPE_WINDOW_SECONDS = 300.0
DEFAULT_REPLY_TIMEOUT_SECONDS = 600.0
DEFAULT_MAX_RECORDS = 200

# 自由文本的意图识别。人在飞书里回的是人话，不是按钮。
DIAGNOSE_KEYWORDS = (
    "帮我查", "帮忙查", "你查", "查一下", "查下", "查查", "排查", "开始处理",
    "开始查", "去查", "看下原因", "看看原因", "找原因", "定位", "诊断",
    "diagnose", "go", "start",
)
DECLINE_KEYWORDS = (
    "我自己看", "自己看", "不用了", "不用查", "先不", "稍后", "等会", "忽略",
    "我来", "我处理", "已知", "误报", "no", "skip", "ignore",
)

REPLY_HINT = "回「帮我查」我就调起本机 Claude Code 做只读排查；回「我自己看」我就不打扰了。"

# 认领后、结论前被重启打断的诊断。子进程随进程一起死了，没人会再回调它
DIAGNOSIS_INTERRUPTED = "服务重启，诊断已中断"


def _match_intent(text: str) -> str:
    """自由文本 → 意图。认不出就返回空串，由上层去问，绝不替人猜。"""
    value = str(text or "").strip().lower()
    if not value:
        return ""
    # 先判拒绝：「我自己看一下」里同时含「自己看」和「看下」，拒绝的语义更强。
    for keyword in DECLINE_KEYWORDS:
        if keyword in value:
            return INTENT_DECLINE
    for keyword in DIAGNOSE_KEYWORDS:
        if keyword in value:
            return INTENT_DIAGNOSE
    return ""


class AlertRelayService:
    def __init__(
        self,
        *,
        robot: Any,
        feishu_bot: Any,
        runner: Any,
        receive_id: str = "",
        enabled: bool = True,
        dedupe_window_seconds: float = DEFAULT_DEDUPE_WINDOW_SECONDS,
        reply_timeout_seconds: float = DEFAULT_REPLY_TIMEOUT_SECONDS,
        auto_diagnose_on_timeout: bool = False,
        cluster_map: Mapping[str, tuple[str, str]] | None = None,
        max_records: int = DEFAULT_MAX_RECORDS,
        clock: Callable[[], float] = time.time,
        persist_path: str | Path | None = None,
        logger: Any = None,
    ) -> None:
        self._robot = robot
        self._bot = feishu_bot
        self._runner = runner
        self._receive_id = str(receive_id or "").strip()
        self._enabled = bool(enabled)
        self._dedupe_window = float(dedupe_window_seconds)
        self._reply_timeout = float(reply_timeout_seconds)
        self._auto_diagnose_on_timeout = bool(auto_diagnose_on_timeout)
        self._cluster_map = dict(cluster_map or {})
        self._max_records = max(1, int(max_records))
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)

        self._records: dict[str, RelayRecord] = {}
        self._active_by_fingerprint: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._sweeper: Optional[asyncio.Task] = None
        self._store = (
            RelayRecordStore(persist_path, logger=self._logger)
            if persist_path
            else None
        )
        self._load()

    # ---------------------------------------------------------------- 持久化

    @property
    def _store_path(self) -> Optional[Path]:
        return self._store.path if self._store else None

    def _load(self) -> None:
        """把上个进程的记录装回来。

        只有非终态记录重建去重指纹：已经 DIAGNOSED / DECLINED / FAILED 的
        还占着指纹的话，同一条规则再烧起来会被当成重复上报，机器人一声不吭。
        超时巡检不需要额外的基线——updated_at 存的就是同一个 clock 的时间戳，
        check_timeouts 直接拿它跟当下比，重启前已经等掉的那几分钟照样算数。

        认领后、结论前被打断的记录（CLAIMED / DIAGNOSING）直接判失败：
        跑诊断的子进程随上个进程一起死了，不会再有回调，挂着不动的话
        这条记录既等不到结论、又一直占着非终态。「查不出来就说查不出来」。
        """
        if self._store is None:
            return
        interrupted = 0
        for record in self._store.load():
            if record.state in (RelayState.CLAIMED, RelayState.DIAGNOSING):
                record.error = DIAGNOSIS_INTERRUPTED
                record.transition(
                    RelayState.FAILED, at=self._clock(), note=DIAGNOSIS_INTERRUPTED
                )
                interrupted += 1
            self._records[record.alert_id] = record
            if not record.is_terminal():
                self._active_by_fingerprint[record.event.fingerprint()] = record.alert_id
        if self._records:
            self._logger.info(
                f"告警中继记录已恢复 {len(self._records)} 条"
                + (f"，其中 {interrupted} 条诊断被重启打断" if interrupted else "")
            )
        if interrupted:
            self._persist()

    def _persist(self) -> None:
        if self._store is None:
            return
        self._store.save(self._records.values())

    # ---------------------------------------------------------------- 查询

    def get(self, alert_id: str) -> Optional[dict[str, Any]]:
        record = self._records.get(str(alert_id or ""))
        return record.to_dict() if record else None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        records = sorted(
            self._records.values(), key=lambda item: item.created_at, reverse=True
        )
        return [record.to_dict() for record in records[: max(1, int(limit))]]

    def health(self) -> dict[str, Any]:
        states: dict[str, int] = {}
        for record in self._records.values():
            states[record.state.value] = states.get(record.state.value, 0) + 1
        return {
            "enabled": self._enabled,
            "receive_id_configured": bool(self._receive_id),
            "reply_timeout_seconds": self._reply_timeout,
            "dedupe_window_seconds": self._dedupe_window,
            "auto_diagnose_on_timeout": self._auto_diagnose_on_timeout,
            "robot": self._robot.health() if self._robot else {},
            "feishu": self._bot.capabilities() if self._bot else {},
            "diagnosis": self._runner.health() if self._runner else {},
            "tracked_alerts": len(self._records),
            "states": states,
        }

    # ---------------------------------------------------------------- 接入

    async def ingest(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self._enabled:
            return {"code": "ALERT_RELAY_DISABLED", "message": "告警值班中继未启用"}
        if not isinstance(payload, Mapping):
            return {"code": "INVALID_REQUEST", "message": "请求体必须是对象"}

        raw_text = str(payload.get("raw_text") or "").strip()
        if not raw_text:
            return {"code": "INVALID_REQUEST", "message": "raw_text 必填"}

        overrides = {
            key: payload.get(key)
            for key in (
                "level", "cluster", "namespace", "target", "workload", "keyword",
                "alert_time", "policy_url", "project_id", "cluster_id", "rule",
            )
        }
        event = parse_alert(raw_text, overrides=overrides, cluster_map=self._cluster_map)
        now = self._clock()

        merged = self._merge_repeat(event, now)
        if merged is not None:
            self._persist()
            return {"code": "OK", "deduped": True, **merged.to_dict()}

        record = self._create_record(event, now)
        await self._notify(record)
        self._persist()
        return {"code": "OK", "deduped": False, **record.to_dict()}

    def _merge_repeat(self, event: AlertEvent, now: float) -> Optional[RelayRecord]:
        """窗口内的同一条规则只累加计数，不再打扰机器人。"""
        alert_id = self._active_by_fingerprint.get(event.fingerprint())
        if not alert_id:
            return None
        record = self._records.get(alert_id)
        if record is None or record.is_terminal():
            self._active_by_fingerprint.pop(event.fingerprint(), None)
            return None
        if now - record.last_seen_at > self._dedupe_window:
            self._active_by_fingerprint.pop(event.fingerprint(), None)
            return None
        record.repeat_count += 1
        record.last_seen_at = now
        return record

    def _create_record(self, event: AlertEvent, now: float) -> RelayRecord:
        alert_id = f"{event.fingerprint()}-{int(now)}"
        record = RelayRecord(alert_id=alert_id, event=event, created_at=now)
        self._records[alert_id] = record
        self._active_by_fingerprint[event.fingerprint()] = alert_id
        self._prune()
        return record

    def _prune(self) -> None:
        if len(self._records) <= self._max_records:
            return
        ordered = sorted(self._records.values(), key=lambda item: item.created_at)
        for record in ordered[: len(self._records) - self._max_records]:
            self._records.pop(record.alert_id, None)
            if self._active_by_fingerprint.get(record.event.fingerprint()) == record.alert_id:
                self._active_by_fingerprint.pop(record.event.fingerprint(), None)

    async def _notify(self, record: RelayRecord) -> None:
        """两路通知互不阻塞：任何一路挂了，另一路照送，状态照走。"""
        record.robot_delivered = await self._safe_robot(self._robot.alert, record.event)
        if not record.robot_delivered:
            reason = getattr(self._robot, "last_error", "") or "机器人未送达"
            record.robot_error = reason
            record.warnings.append(f"机器人未提醒：{reason}")

        await self._send_alert_card(record)

        record.transition(RelayState.NOTIFIED, at=self._clock())
        record.transition(RelayState.AWAITING_REPLY, at=self._clock())

    async def _send_alert_card(self, record: RelayRecord) -> None:
        if not self._receive_id:
            record.feishu_error = "未配置通知对象 receive_id"
            record.warnings.append("飞书未通知：未配置 receive_id")
            return
        try:
            sent = await self._bot.send_card(self._receive_id, build_alert_card(record))
            record.feishu_message_id = sent.get("message_id", "")
            record.feishu_chat_id = sent.get("chat_id", "")
        except Exception as exc:
            record.feishu_error = str(exc)
            record.warnings.append(f"飞书未通知：{exc}")
            self._logger.warning(f"告警卡片发送失败: {exc}")

    # ---------------------------------------------------------------- 回复

    async def handle_reply(
        self,
        *,
        alert_id: str = "",
        root_message_id: str = "",
        intent: str = "",
        text: str = "",
        user: str = "",
        message_id: str = "",
    ) -> dict[str, Any]:
        record = self._locate(alert_id, root_message_id)
        if record is None:
            return {"code": "ALERT_NOT_FOUND", "message": "找不到对应的告警"}

        resolved = str(intent or "").strip() or _match_intent(text)
        if not resolved:
            await self._safe_reply_text(record, REPLY_HINT)
            return {
                "code": "UNKNOWN_INTENT",
                "message": "没听懂这条回复",
                "alert_id": record.alert_id,
            }

        if record.state not in (RelayState.AWAITING_REPLY, RelayState.TIMEOUT):
            return {
                "code": "ALREADY_HANDLED",
                "message": f"该告警已处于 {record.state.value}",
                "alert_id": record.alert_id,
                "state": record.state.value,
            }

        # 状态必须在任何 await 之前就翻掉：两个人几乎同时点「帮我查」时，
        # 只要中间有一个 await 点，第二个回调就能溜过上面的状态检查，跑出第二个
        # Claude Code 子进程（双倍 token、双份卡片）。翻状态是同步的，因此是原子的。
        record.reply_text = str(text or "")
        record.claimed_by = user or record.claimed_by
        target = RelayState.DECLINED if resolved == INTENT_DECLINE else RelayState.CLAIMED
        record.transition(target, at=self._clock(), note=user)
        # 认领同样在 await 之前落盘：诊断要跑几分钟，这期间进程被重启的话，
        # 盘上必须已经是 CLAIMED，否则恢复出来的记录还停在等回复。
        self._persist()

        # 回执先于诊断：诊断要跑几分钟，人得先知道「收到了」。
        if message_id:
            await self._safe_reaction(message_id)

        if target is RelayState.DECLINED:
            self._release_fingerprint(record)
            self._persist()
            await self._safe_robot(self._robot.declined)
            await self._safe_reply_text(record, "好，这条交给你，我不打扰了。")
            return {"code": "OK", "alert_id": record.alert_id, "state": record.state.value}

        await self._safe_robot(self._robot.claimed, record.event, by=user)
        self._spawn(self._diagnose(record))
        return {"code": "OK", "alert_id": record.alert_id, "state": record.state.value}

    def _locate(self, alert_id: str, root_message_id: str) -> Optional[RelayRecord]:
        if alert_id:
            return self._records.get(str(alert_id))
        if root_message_id:
            for record in sorted(
                self._records.values(), key=lambda item: item.created_at, reverse=True
            ):
                if record.feishu_message_id and record.feishu_message_id == root_message_id:
                    return record
        return None

    # ---------------------------------------------------------------- 诊断

    async def _diagnose(self, record: RelayRecord) -> None:
        record.transition(RelayState.DIAGNOSING, at=self._clock())
        self._persist()
        await self._safe_robot(self._robot.diagnosing, record.event)

        try:
            result = await self._runner.run(record.event)
        except Exception as exc:  # 子进程层已经兜过一轮，这里只防意外
            self._logger.exception("诊断执行异常")
            await self._finish_failure(record, "诊断执行异常", str(exc))
            return

        if not result.ok or result.diagnosis is None:
            await self._finish_failure(
                record,
                result.reason or "诊断失败",
                result.detail,
                retry_hint=getattr(result, "command", ""),
            )
            return

        record.diagnosis = result.diagnosis
        record.transition(RelayState.DIAGNOSED, at=self._clock())
        self._release_fingerprint(record)
        self._persist()
        await self._safe_reply_card(record, build_diagnosis_card(record, result.diagnosis))
        await self._safe_robot(self._robot.diagnosed, result.diagnosis)

    async def _finish_failure(
        self, record: RelayRecord, reason: str, detail: str, *, retry_hint: str = ""
    ) -> None:
        record.error = reason
        record.transition(RelayState.FAILED, at=self._clock(), note=reason)
        self._release_fingerprint(record)
        self._persist()
        await self._safe_reply_card(
            record, build_failure_card(record, reason, detail, retry_hint=retry_hint)
        )
        await self._safe_robot(self._robot.failed, reason)

    # ---------------------------------------------------------------- 超时

    async def check_timeouts(self) -> list[dict[str, Any]]:
        now = self._clock()
        timed_out: list[dict[str, Any]] = []
        for record in list(self._records.values()):
            if record.state is not RelayState.AWAITING_REPLY:
                continue
            if now - record.updated_at < self._reply_timeout:
                continue
            record.transition(RelayState.TIMEOUT, at=now, note="无人认领")
            await self._safe_robot(self._robot.escalate, record.event)
            await self._safe_reply_text(
                record, f"这条告警 {int(self._reply_timeout // 60)} 分钟没人接。{REPLY_HINT}"
            )
            if self._auto_diagnose_on_timeout:
                record.claimed_by = "自动（超时未响应）"
                record.transition(RelayState.CLAIMED, at=now, note="超时自动认领")
                self._spawn(self._diagnose(record))
            self._persist()
            timed_out.append(record.to_dict())
        return timed_out

    async def start(self, interval_seconds: float = 60.0) -> None:
        """起一个后台巡检，把超时未认领的告警升级提醒。"""
        if self._sweeper is not None or not self._enabled:
            return

        async def loop():
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    await self.check_timeouts()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._logger.warning(f"告警超时巡检失败: {exc}")

        self._sweeper = asyncio.create_task(loop())

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    # ---------------------------------------------------------------- 工具

    def _release_fingerprint(self, record: RelayRecord) -> None:
        fingerprint = record.event.fingerprint()
        if self._active_by_fingerprint.get(fingerprint) == record.alert_id:
            self._active_by_fingerprint.pop(fingerprint, None)

    def _spawn(self, coroutine) -> None:
        """诊断要跑几分钟，飞书回调必须秒回，所以丢后台。"""
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_for_idle(self) -> None:
        """等所有后台诊断跑完——给测试和优雅停机用。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _safe_robot(self, method, *args, **kwargs) -> bool:
        if self._robot is None:
            return False
        try:
            return bool(await method(*args, **kwargs))
        except Exception as exc:
            self._logger.warning(f"机器人联动失败: {exc}")
            return False

    async def _safe_reply_card(self, record: RelayRecord, card: dict[str, Any]) -> None:
        if not record.feishu_message_id:
            # 告警卡片当初就没发出去，只能另起一条，别把结论丢了。
            if not self._receive_id:
                return
            try:
                await self._bot.send_card(self._receive_id, card)
            except Exception as exc:
                self._logger.warning(f"诊断结论发送失败: {exc}")
            return
        try:
            await self._bot.reply_card(record.feishu_message_id, card)
        except Exception as exc:
            record.warnings.append(f"结论回帖失败：{exc}")
            self._logger.warning(f"诊断结论回帖失败: {exc}")

    async def _safe_reply_text(self, record: RelayRecord, text: str) -> None:
        try:
            if record.feishu_message_id:
                await self._bot.reply_text(record.feishu_message_id, text)
            elif self._receive_id:
                await self._bot.send_card(
                    self._receive_id,
                    {"elements": [{"tag": "div",
                                   "text": {"tag": "lark_md", "content": text}}]},
                )
        except Exception as exc:
            self._logger.warning(f"飞书文本回复失败: {exc}")

    async def _safe_reaction(self, message_id: str) -> None:
        try:
            await self._bot.add_reaction(message_id)
        except Exception:
            pass
