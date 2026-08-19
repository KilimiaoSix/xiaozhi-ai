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
- 「每天一次」以台账里的播报标记为准，不是内存标记：改配置要重启进程是本仓库的
  常规操作，内存状态活不过重启，同一天重播一遍比想象中容易发生。
- 扫描结果先挂在工位状态上再送达：扫描和送达之间隔着秒级的飞书往返，设备可能
  恰好转忙或断线重连，改期补送时不该再烧一次扫描额度。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import Any, Awaitable, Callable, Mapping

from core.handle.pushHandle import device_busy_reason, push_work_event
from core.morning_brief.announcement import (
    Announcement,
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
DEFAULT_WORKDAYS = frozenset({1, 2, 3, 4, 5})

# 配置误写的表象一律是「改了没生效」：解析期必须出声，静默回退等于没人能排查。
_config_logger = logging.getLogger(__name__)


class _ScanFailed(RuntimeError):
    """扫描名义上成功、实际三路采集全军覆没，按扫描失败对待。"""


def _warn_config(key: str, value: Any, fallback: Any) -> None:
    _config_logger.warning(
        f"morning_brief.announce.{key} 的配置值 {value!r} 无法使用，回退为 {fallback!r}"
    )


def _text(value: Any) -> str:
    """None 安全的字符串归一。YAML 裸空键解析为 None，直接 str() 会得到 \"None\"。"""
    return "" if value is None else str(value).strip()


def _positive_int(value: Any, fallback: int, minimum: int = 1, *, key: str = "") -> int:
    # bool 是 int 的子类：yes/on 会悄悄变成 1，必须显式拒绝。
    if isinstance(value, bool):
        _warn_config(key, value, fallback)
        return fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        if value is not None:
            _warn_config(key, value, fallback)
        return fallback
    if parsed < minimum:
        _warn_config(key, value, fallback)
        return fallback
    return parsed


def _non_negative_float(value: Any, fallback: float, *, key: str = "") -> float:
    if isinstance(value, bool):
        _warn_config(key, value, fallback)
        return fallback
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        if value is not None:
            _warn_config(key, value, fallback)
        return fallback
    if parsed < 0:
        _warn_config(key, value, fallback)
        return fallback
    return parsed


def _flag(value: Any, default: bool, *, key: str = "") -> bool:
    """布尔开关：带引号的 "no"/"false" 要按关闭理解，而不是按非空字符串恒真。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0", ""}:
            return False
        # 开关看不懂就当关：错开一个早晨的骚扰比错关一个功能更难收场
        _warn_config(key, value, False)
        return False
    return bool(value)


def _parse_clock(text: str) -> time:
    hour, minute = (int(part) for part in text.split(":", 1))
    return time(hour, minute)


def _clock_time(value: Any, fallback: str, *, key: str = "") -> time:
    if value is None:
        return _parse_clock(fallback)
    try:
        return _parse_clock(str(value))
    except (TypeError, ValueError):
        # 不带引号的 8:00/10:00 会被 YAML 1.1 当六十进制整数读进来，到这里已无从恢复本意
        _config_logger.warning(
            f'morning_brief.announce.{key} 的配置值 {value!r} 不是 "HH:MM"'
            f'（时间要带引号写，如 "08:00"），回退为 {fallback}'
        )
        return _parse_clock(fallback)


def _workdays(raw: Any) -> frozenset[int]:
    if raw is None:
        return DEFAULT_WORKDAYS
    if not isinstance(raw, (list, tuple, set, frozenset)):
        _warn_config("workdays", raw, sorted(DEFAULT_WORKDAYS))
        return DEFAULT_WORKDAYS
    valid = {
        day
        for day in raw
        if isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 7
    }
    dropped = [day for day in raw if day not in valid]
    if dropped:
        _config_logger.warning(
            f"morning_brief.announce.workdays 忽略非法项 {dropped!r}（要用 1-7 的整数）"
        )
    if not valid and dropped:
        # 写了内容但全不合法：按写错处理回落默认，而不是静默变成「永不播」
        _warn_config("workdays", list(raw), sorted(DEFAULT_WORKDAYS))
        return DEFAULT_WORKDAYS
    if not valid:
        _config_logger.warning(
            "morning_brief.announce.workdays 为空列表，晨报不会在任何一天触发"
        )
    return frozenset(valid)


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
            workstation = _text(item.get("workstation_id"))
            if workstation:
                bindings[workstation] = _text(item.get("device_id"))
        if not bindings:
            bindings[DEFAULT_WORKSTATION] = ""

        window_start = _clock_time(
            raw.get("window_start"), DEFAULT_WINDOW_START, key="window_start"
        )
        window_end = _clock_time(
            raw.get("window_end"), DEFAULT_WINDOW_END, key="window_end"
        )
        if window_start >= window_end:
            _config_logger.warning(
                f"morning_brief.announce 时间窗写反了"
                f"（window_start={window_start:%H:%M} >= window_end={window_end:%H:%M}），"
                f"回退为 {DEFAULT_WINDOW_START}-{DEFAULT_WINDOW_END}"
            )
            window_start = _parse_clock(DEFAULT_WINDOW_START)
            window_end = _parse_clock(DEFAULT_WINDOW_END)

        return cls(
            # 晨报总开关关着时，这条链路连扫描都不该发生。
            enabled=(
                _flag(brief.get("enabled"), False, key="morning_brief.enabled")
                and _flag(raw.get("enabled"), True, key="enabled")
            ),
            timezone_name=_text(brief.get("timezone")) or "Asia/Shanghai",
            bindings=bindings,
            workdays=_workdays(raw.get("workdays")),
            window_start=window_start,
            window_end=window_end,
            # 空串是有意义的取值（不再问好），所以不能用 or 兜底
            greeting=(
                DEFAULT_GREETING
                if raw.get("greeting") is None
                else str(raw.get("greeting"))
            ),
            status=str(raw.get("status") or STATUS_BRIEF),
            emotion=str(raw.get("emotion") or EMOTION_BRIEF),
            max_items=_positive_int(
                raw.get("max_items"), DEFAULT_MAX_ITEMS, key="max_items"
            ),
            item_chars=_positive_int(
                raw.get("item_chars"), DEFAULT_ITEM_CHARS, key="item_chars"
            ),
            poll_seconds=_positive_int(
                raw.get("poll_seconds"), DEFAULT_POLL_SECONDS, key="poll_seconds"
            ),
            retry_seconds=_positive_int(
                raw.get("retry_seconds"), DEFAULT_RETRY_SECONDS, key="retry_seconds"
            ),
            max_attempts=_positive_int(
                raw.get("max_attempts"), DEFAULT_MAX_ATTEMPTS, key="max_attempts"
            ),
            # 0 是合法取值：不等宽限、立即照发
            busy_grace_seconds=_positive_int(
                raw.get("busy_grace_seconds"),
                DEFAULT_BUSY_GRACE_SECONDS,
                minimum=0,
                key="busy_grace_seconds",
            ),
            # 0 是合法取值：播完不收屏
            restore_after_seconds=_non_negative_float(
                raw.get("restore_after_seconds"),
                DEFAULT_RESTORE_AFTER_SECONDS,
                key="restore_after_seconds",
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
    # 报告已扫好但语音还没送达：改期补送时复用，不再烧一次飞书扫描。
    pending: Announcement | None = None
    # 画面是否已经上屏：补送 TTS 时带 silent，不再响第二声提示音。
    screen_shown: bool = False
    # 语音被设备忙态挤掉的首次时刻：宽限用完就接受只上屏。
    speak_retry_since: datetime | None = None


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
            # 离席打断了「连续忙」的观察：旧锚点留着会把返岗时 0 秒龄的新忙态
            # 记成已经忙了好几分钟，直接绕过宽限硬推，语音又被挤掉。
            state.busy_since = None
            state.speak_retry_since = None
            return
        if state.next_attempt_at is not None and now < state.next_attempt_at:
            return

        device_id = self._resolve_device(configured_device)
        conn = self._device_registry.get(device_id) if device_id else None
        if conn is None:
            # 不计入失败次数：机器人没上线不是飞书的问题，窗口内一直等着它回来。
            state.next_attempt_at = now + timedelta(seconds=self.policy.retry_seconds)
            self._log_no_device(workstation_id, configured_device)
            return

        # 到岗迎接是事件驱动的、比这里先到一步，它的 TTS 还在放时插播会被
        # push_work_event 降级成只上屏——晨报就白播了。等它说完再扫。
        busy = self._busy_reason(conn)
        if busy:
            if state.busy_since is None:
                state.busy_since = now
                self._logger.info(f"{busy}，晨报稍后再播")
            if not self._grace_spent(state, now):
                return
            # 等太久了：只上屏也比今天不播强
        else:
            state.busy_since = None

        if state.pending is None:
            state.attempts += 1
            try:
                report = await self._service.preview(limit=self.policy.max_items)
                if self._scan_came_back_empty_handed(report):
                    # 三路采集全军覆没时念「今天暂时没有待办」是假话，按扫描失败改期。
                    raise _ScanFailed("飞书三路采集全部失败")
                state.pending = build_announcement(
                    report,
                    max_items=self.policy.max_items,
                    item_chars=self.policy.item_chars,
                    greeting=self.policy.greeting,
                    status=self.policy.status,
                    emotion=self.policy.emotion,
                    display_timezone=self.policy.timezone,
                )
            except Exception:
                self._logger.exception("晨报扫描失败")
                self._defer_or_give_up(state, now, "扫描")
                return

            # 扫描要等飞书返回好几秒：期间固件断线重连会把 conn 换掉（固件 10 秒
            # 就重连一次），设备也可能开始播迎接语。送达前按当前状态重新核一遍。
            conn = self._device_registry.get(device_id)
            if conn is None:
                state.next_attempt_at = now + timedelta(
                    seconds=self.policy.retry_seconds
                )
                self._logger.info(
                    f"工位 {workstation_id} 扫描期间机器人离线，晨报改期补送"
                )
                return
            busy = self._busy_reason(conn)
            if busy:
                if state.busy_since is None:
                    state.busy_since = now
                if not self._grace_spent(state, now):
                    self._logger.info(f"{busy}，晨报改期补送（报告已备好）")
                    return

        announcement = state.pending
        # 故障通知（speak=False）是给人看的，不套自动收屏：20 秒后被基态恢复
        # 抹掉的话，没盯着屏幕的人全天不知道晨报坏了。
        restore_after = (
            self.policy.restore_after_seconds
            if announcement.speak and self.policy.restore_after_seconds > 0
            else None
        )
        try:
            spoke = await self._push(
                conn,
                text=announcement.text,
                emotion=announcement.emotion,
                status=announcement.status,
                speak=announcement.speak,
                restore_after=restore_after,
                # 补送只为把语音送出去，画面早已上屏，别再响一声提示音
                silent=state.screen_shown,
            )
        except Exception:
            self._logger.exception("晨报推送失败")
            self._defer_or_give_up(state, now, "推送")
            return
        state.screen_shown = True

        if announcement.speak and spoke is False and not self._grace_spent(state, now):
            # push_work_event 内部只等 3 秒就降级只上屏（那是给桌面端 HTTP 超时
            # 设计的预算）；这里有整个宽限期可用，改期补一次语音。
            if state.speak_retry_since is None:
                state.speak_retry_since = now
            state.next_attempt_at = now + timedelta(seconds=self.policy.retry_seconds)
            self._logger.info(
                f"工位 {workstation_id} 晨报语音被设备忙态挤掉，画面已上屏，宽限内改期补播"
            )
            return

        state.done = True
        state.pending = None
        self._mark_announced(workstation_id, now.date())
        delivery = "已播报" if (not announcement.speak or spoke) else "仅上屏"
        self._logger.info(
            f"工位 {workstation_id} 晨报{delivery}（设备 {device_id}）：{announcement.text}"
        )

    def _grace_spent(self, state: _WorkstationState, now: datetime) -> bool:
        grace = timedelta(seconds=self.policy.busy_grace_seconds)
        if state.busy_since is not None and now - state.busy_since >= grace:
            return True
        return (
            state.speak_retry_since is not None
            and now - state.speak_retry_since >= grace
        )

    def _log_no_device(self, workstation_id: str, configured_device: str) -> None:
        online = len(self._device_registry.device_ids())
        if not configured_device and online > 1:
            # 多台在线不猜、宁可不播是设计；但日志得指对方向，别把人引去查连接
            self._logger.info(
                f"工位 {workstation_id} 已认出主人，但有 {online} 台设备在线且未配置 "
                f"device_id，无法自动绑定，晨报暂不生成"
            )
            return
        self._logger.info(
            f"工位 {workstation_id} 已认出主人，但机器人不在线，暂不生成晨报"
        )

    @staticmethod
    def _scan_came_back_empty_handed(report: Any) -> bool:
        if not isinstance(report, Mapping):
            return False
        if report.get("reauthorization_required") or report.get("permission_required"):
            # 授权类故障有专门的屏显通知，不算扫描失败
            return False
        return report.get("coverage_status") == "FAILED" and not report.get("top_three")

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
            # 「每天一次」的账在台账里：重启后内存清零，靠这条不重播。
            state.done = self._was_announced(workstation_id, now.date())
            self._states[workstation_id] = state
        return state

    def _was_announced(self, workstation_id: str, local_date: date) -> bool:
        checker = getattr(self._service, "was_announced", None)
        if checker is None:
            return False
        try:
            return bool(checker(workstation_id, local_date))
        except Exception:
            self._logger.exception("读取晨报播报标记失败")
            return False

    def _mark_announced(self, workstation_id: str, local_date: date) -> None:
        marker = getattr(self._service, "mark_announced", None)
        if marker is None:
            return
        try:
            marker(workstation_id, local_date)
        except Exception:
            self._logger.exception("写入晨报播报标记失败")

    def _in_window(self, now: datetime) -> bool:
        return self.policy.window_start <= now.time() < self.policy.window_end

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
    announcer = MorningBriefAnnouncer(
        config, presence_registry, device_registry, service, **kwargs
    )
    if not announcer.policy.enabled:
        return None
    return announcer
