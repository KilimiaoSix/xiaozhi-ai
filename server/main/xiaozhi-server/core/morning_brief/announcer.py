"""早上第一次认出主人时，把当天待办播成一条晨报。

晨报此前只有 HTTP 预览入口：人得自己去桌面端点一下才看得到，而最需要它的时刻恰好是
刚坐下、还没打开电脑的那一分钟。这里把触发权交给摄像头——在配置的时间窗内，
PresenceRegistry 第一次出现 `present + owner` 就扫一次飞书并让机器人念出来，每天一次。

几条刻意的取舍：

- 时间窗内的「第一次」而不是「当天第一次」：7:50 就到工位的人在 8:00 一样该听到晨报，
  否则来得早反而没有。
- 设备不在线时不扫飞书：扫描是有额度和延迟的外部调用，念不出来的扫描没有意义，
  隔一个重试间隔再看设备回来没有。
- 扫描连续失败到上限就放弃当天：早高峰每分钟重试一次飞书，既吵又没用。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import Any, Awaitable, Callable, Mapping

from core.handle.pushHandle import device_busy_reason, push_work_event
from core.morning_brief.announcement import (
    DEFAULT_GREETING,
    DEFAULT_ITEM_CHARS,
    DEFAULT_MAX_ITEMS,
    EMOTION_BRIEF,
    STATUS_BRIEF,
    build_announcement,
)
from core.morning_brief.models import resolve_timezone


DEFAULT_WINDOW_START = "08:00"
DEFAULT_WINDOW_END = "09:30"
DEFAULT_POLL_SECONDS = 5
DEFAULT_RETRY_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BUSY_GRACE_SECONDS = 120
DEFAULT_RESTORE_AFTER_SECONDS = 20.0
DEFAULT_WORKSTATION = "desktop-local"


def _positive_int(value: Any, fallback: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= minimum else fallback


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _clock_time(value: Any, fallback: str) -> time:
    text = str(value or fallback)
    try:
        hour, minute = (int(part) for part in text.split(":", 1))
        return time(hour, minute)
    except (TypeError, ValueError):
        hour, minute = (int(part) for part in fallback.split(":", 1))
        return time(hour, minute)


@dataclass(frozen=True)
class AnnouncePolicy:
    enabled: bool
    timezone_name: str
    bindings: dict[str, str]
    workdays: frozenset[int]
    window_start: time
    window_end: time
    greeting: str
    status: str
    emotion: str
    max_items: int
    item_chars: int
    poll_seconds: int
    retry_seconds: int
    max_attempts: int
    busy_grace_seconds: int
    restore_after_seconds: float

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "AnnouncePolicy":
        brief = config.get("morning_brief", {}) if isinstance(config, Mapping) else {}
        brief = brief if isinstance(brief, Mapping) else {}
        raw = brief.get("announce", {})
        raw = raw if isinstance(raw, Mapping) else {}

        bindings: dict[str, str] = {}
        raw_bindings = raw.get("bindings", [])
        if not isinstance(raw_bindings, (list, tuple)):
            raw_bindings = []
        for item in raw_bindings:
            if not isinstance(item, Mapping):
                continue
            workstation = str(item.get("workstation_id", "")).strip()
            if workstation:
                bindings[workstation] = str(item.get("device_id", "")).strip()
        if not bindings:
            bindings[DEFAULT_WORKSTATION] = ""

        raw_workdays = raw.get("workdays", [1, 2, 3, 4, 5])
        if not isinstance(raw_workdays, (list, tuple, set, frozenset)):
            raw_workdays = [1, 2, 3, 4, 5]
        workdays = {
            day
            for day in raw_workdays
            if isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 7
        }

        return cls(
            # 晨报总开关关着时，这条链路连扫描都不该发生。
            enabled=bool(brief.get("enabled", False)) and bool(raw.get("enabled", True)),
            timezone_name=str(brief.get("timezone", "Asia/Shanghai")),
            bindings=bindings,
            workdays=frozenset(workdays),
            window_start=_clock_time(raw.get("window_start"), DEFAULT_WINDOW_START),
            window_end=_clock_time(raw.get("window_end"), DEFAULT_WINDOW_END),
            # 空串是有意义的取值（不再问好），所以不能用 or 兜底
            greeting=(
                DEFAULT_GREETING
                if raw.get("greeting") is None
                else str(raw.get("greeting"))
            ),
            status=str(raw.get("status") or STATUS_BRIEF),
            emotion=str(raw.get("emotion") or EMOTION_BRIEF),
            max_items=_positive_int(raw.get("max_items"), DEFAULT_MAX_ITEMS),
            item_chars=_positive_int(raw.get("item_chars"), DEFAULT_ITEM_CHARS),
            poll_seconds=_positive_int(raw.get("poll_seconds"), DEFAULT_POLL_SECONDS),
            retry_seconds=_positive_int(
                raw.get("retry_seconds"), DEFAULT_RETRY_SECONDS
            ),
            max_attempts=_positive_int(
                raw.get("max_attempts"), DEFAULT_MAX_ATTEMPTS
            ),
            busy_grace_seconds=_positive_int(
                raw.get("busy_grace_seconds"), DEFAULT_BUSY_GRACE_SECONDS
            ),
            restore_after_seconds=_positive_float(
                raw.get("restore_after_seconds"), DEFAULT_RESTORE_AFTER_SECONDS
            ),
        )

    @property
    def timezone(self):
        return resolve_timezone(self.timezone_name)


@dataclass
class _WorkstationState:
    """一个工位当天的晨报状态。跨日直接整个换掉。"""

    local_date: date | None = None
    # 今天不用再管了：要么播过了，要么连续失败到上限主动放弃了。
    done: bool = False
    attempts: int = 0
    next_attempt_at: datetime | None = None
    busy_since: datetime | None = None


class MorningBriefAnnouncer:
    """轮询在岗状态，在时间窗内首次认出主人时播报晨报。"""

    def __init__(
        self,
        config: Mapping[str, Any],
        presence_registry,
        device_registry,
        service,
        *,
        push: Callable[..., Awaitable[Any]] | None = None,
        busy_reason: Callable[[Any], str | None] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        logger=None,
    ) -> None:
        self.policy = AnnouncePolicy.from_config(config)
        self._presence_registry = presence_registry
        self._device_registry = device_registry
        self._service = service
        self._push = push or push_work_event
        self._busy_reason = busy_reason or device_busy_reason
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or asyncio.sleep
        self._logger = logger or logging.getLogger(__name__)
        self._states: dict[str, _WorkstationState] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.policy.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="morning-brief-announcer")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def tick(self) -> None:
        if not self.policy.enabled:
            return
        now = self._local_now()
        for workstation_id, configured_device in self.policy.bindings.items():
            await self._tick_workstation(workstation_id, configured_device, now)

    async def _tick_workstation(
        self, workstation_id: str, configured_device: str, now: datetime
    ) -> None:
        state = self._state_for(workstation_id, now)
        if state.done:
            return
        if now.isoweekday() not in self.policy.workdays:
            return
        if not self._in_window(now):
            return
        if not self._is_owner_present(self._presence_registry.get(workstation_id)):
            return
        if state.next_attempt_at is not None and now < state.next_attempt_at:
            return

        device_id = self._resolve_device(configured_device)
        conn = self._device_registry.get(device_id) if device_id else None
        if conn is None:
            # 不计入失败次数：机器人没上线不是飞书的问题，窗口内一直等着它回来。
            state.next_attempt_at = now + timedelta(seconds=self.policy.retry_seconds)
            self._logger.info(
                f"工位 {workstation_id} 已认出主人，但机器人不在线，暂不生成晨报"
            )
            return

        # 到岗迎接是事件驱动的、比这里先到一步，它的 TTS 还在放时插播会被
        # push_work_event 降级成只上屏——晨报就白播了。等它说完再扫。
        busy = self._busy_reason(conn)
        if busy:
            if state.busy_since is None:
                state.busy_since = now
                self._logger.info(f"{busy}，晨报稍后再播")
            if now - state.busy_since < timedelta(
                seconds=self.policy.busy_grace_seconds
            ):
                return
            # 等太久了：只上屏也比今天不播强
        else:
            state.busy_since = None

        state.attempts += 1
        try:
            report = await self._service.preview()
        except Exception:
            self._logger.exception("晨报扫描失败")
            self._defer_or_give_up(state, now, "扫描")
            return

        announcement = build_announcement(
            report,
            max_items=self.policy.max_items,
            item_chars=self.policy.item_chars,
            greeting=self.policy.greeting,
            status=self.policy.status,
            emotion=self.policy.emotion,
        )
        # 先置位再 await：推送期间又来一次 tick 不会重复播报。
        state.done = True
        try:
            await self._push(
                conn,
                text=announcement.text,
                emotion=announcement.emotion,
                status=announcement.status,
                speak=announcement.speak,
                restore_after=self.policy.restore_after_seconds,
            )
        except Exception:
            state.done = False
            self._logger.exception("晨报推送失败")
            self._defer_or_give_up(state, now, "推送")
            return
        self._logger.info(
            f"工位 {workstation_id} 晨报已播报给设备 {device_id}：{announcement.text}"
        )

    def _defer_or_give_up(
        self, state: _WorkstationState, now: datetime, stage: str
    ) -> None:
        if state.attempts >= self.policy.max_attempts:
            state.done = True
            self._logger.warning(
                f"晨报{stage}连续失败 {state.attempts} 次，今天不再重试"
            )
            return
        state.next_attempt_at = now + timedelta(seconds=self.policy.retry_seconds)

    def _state_for(self, workstation_id: str, now: datetime) -> _WorkstationState:
        state = self._states.get(workstation_id)
        if state is None or state.local_date != now.date():
            state = _WorkstationState(local_date=now.date())
            self._states[workstation_id] = state
        return state

    def _in_window(self, now: datetime) -> bool:
        current = now.time().replace(tzinfo=None)
        return self.policy.window_start <= current < self.policy.window_end

    @staticmethod
    def _is_owner_present(presence: Mapping[str, Any] | None) -> bool:
        # 与关怀编排同一判据：只有本人核验通过的在岗才算「发现主人」。
        # effective_state 为 stale 时上报已断流，不能当成人还在。
        if not presence or presence.get("effective_state") != "present":
            return False
        identity = presence.get("identity")
        return isinstance(identity, Mapping) and identity.get("state") == "owner"

    def _resolve_device(self, configured_device: str) -> str | None:
        if configured_device:
            return configured_device
        devices = self._device_registry.device_ids()
        return devices[0] if len(devices) == 1 else None

    def _local_now(self) -> datetime:
        now = self._now_provider()
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(self.policy.timezone)

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("晨报播报轮询异常")
            await self._sleep(self.policy.poll_seconds)


def create_morning_brief_announcer(
    config: Mapping[str, Any],
    presence_registry,
    device_registry,
    service,
    **kwargs,
) -> MorningBriefAnnouncer | None:
    """按配置装配播报器；未启用或缺少设备注册表时返回 None。

    device_registry 来自 WebSocket 服务，presence_server.py 那个轻量入口没有它，
    此时不装配，晨报保持只有 HTTP 预览入口的行为。
    """
    if presence_registry is None or device_registry is None or service is None:
        return None
    policy = AnnouncePolicy.from_config(config)
    if not policy.enabled:
        return None
    return MorningBriefAnnouncer(
        config, presence_registry, device_registry, service, **kwargs
    )
