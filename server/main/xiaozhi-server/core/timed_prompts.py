"""按时间设置好的语音提示调度（喝水/站立等），到点且本人在工位才播。

配置段 config["timed_prompts"]::

    timed_prompts:
      enabled: false
      prompts:
        - time: "10:30"        # HH:MM，本地时区
          text: "该喝水啦"
          emotion: happy       # 可选，默认 happy
          speak: true          # 可选，默认 true
          days: [1, 2, 3, 4, 5]  # 可选，1=周一...7=周日，也接受 mon/tue/.../sun；不填=每天

播报目标是 config["presence_robot"]["workstations"]（工位 -> 设备 MAC）里配置的
每个工位——与到岗迎接（core/presence_arrival.py）共用同一份工位映射，这里只读
不改。每分钟对一次表，命中时间的提示逐个检查对应工位是否在岗：presence_lookup
返回 "present" 才播，其余一律跳过并只记 debug（不追补、不重试）。同一天同一条
提示只播一次，跨天自动重新武装。

clock/sleep/push_event/device_resolver/presence_lookup 均可注入，便于离线单测；
生产装配见 create_timed_prompt_scheduler。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional


TAG = __name__

DEFAULT_EMOTION = "happy"
DEFAULT_SPEAK = True
TICK_SECONDS = 60.0

_DAY_ALIASES = {
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
    "sun": 7,
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
    "sunday": 7,
}


@dataclass(frozen=True)
class _Prompt:
    index: int  # 在原始配置数组里的位置，用作当天去重的稳定 key
    time: str  # 归一化后的 "HH:MM"
    text: str
    emotion: str
    speak: bool
    days: Optional[frozenset]  # None = 每天


@dataclass(frozen=True)
class _Settings:
    prompts: tuple
    workstations: dict  # workstation_id -> device_id


def _parse_days(raw: Any) -> Optional[frozenset]:
    """解析 days 字段；格式非法或为空数组时按"不限制"处理（None）。"""
    if not isinstance(raw, list) or not raw:
        return None
    days: set = set()
    for item in raw:
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and 1 <= item <= 7:
            days.add(item)
            continue
        if isinstance(item, str) and item.strip().lower() in _DAY_ALIASES:
            days.add(_DAY_ALIASES[item.strip().lower()])
    return frozenset(days) if days else None


def _parse_time(raw: Any) -> Optional[str]:
    """把 time 字段归一化成 "HH:MM"；格式非法返回 None，调用方据此丢弃该条。"""
    text = str(raw or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _parse_prompt(index: int, raw: Any) -> Optional[_Prompt]:
    """单条提示配置非法（缺 text / time 格式错）就整条丢弃，不拖累其余提示。"""
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()
    time_text = _parse_time(raw.get("time"))
    if not text or time_text is None:
        return None
    return _Prompt(
        index=index,
        time=time_text,
        text=text,
        emotion=str(raw.get("emotion") or DEFAULT_EMOTION),
        speak=bool(raw.get("speak", DEFAULT_SPEAK)),
        days=_parse_days(raw.get("days")),
    )


def _read_settings(config: dict) -> Optional[_Settings]:
    """读配置段；未启用、没有合法提示或没有工位映射时返回 None（调度器不必启动）。"""
    section = (config or {}).get("timed_prompts") or {}
    if not isinstance(section, dict) or not section.get("enabled", False):
        return None

    raw_prompts = section.get("prompts") or []
    if not isinstance(raw_prompts, list):
        return None
    prompts = tuple(
        parsed
        for index, raw in enumerate(raw_prompts)
        for parsed in [_parse_prompt(index, raw)]
        if parsed is not None
    )
    if not prompts:
        return None

    presence_section = (config or {}).get("presence_robot") or {}
    raw_workstations = (
        presence_section.get("workstations")
        if isinstance(presence_section, dict)
        else {}
    ) or {}
    workstations = {
        str(key): str(value)
        for key, value in raw_workstations.items()
        if str(key).strip() and str(value).strip()
    }
    if not workstations:
        return None

    return _Settings(prompts=prompts, workstations=workstations)


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _default_push_event(conn, text: str, **kwargs) -> bool:
    from core.handle.pushHandle import push_work_event

    return await push_work_event(conn, text=text, **kwargs)


class TimedPromptScheduler:
    """每分钟对表，命中时间且对应工位在岗才把配置好的提示播出去。"""

    def __init__(
        self,
        config: dict,
        *,
        push_event: Optional[Callable[..., Awaitable[Any]]] = None,
        device_resolver: Optional[Callable[[str], Any]] = None,
        presence_lookup: Optional[Callable[[str], Optional[str]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        tick_seconds: float = TICK_SECONDS,
        logger=None,
        settings: Optional[_Settings] = None,
    ) -> None:
        self._settings = settings if settings is not None else _read_settings(config)
        self._push_event = push_event or _default_push_event
        # device_resolver: device_id -> conn（同 DeviceRegistry.get 的签名，可直接传它）
        self._device_resolver = device_resolver
        # presence_lookup: workstation_id -> "present"/"absent"/None（同
        # PresenceRegistry.get(...)["effective_state"] 的语义）
        self._presence_lookup = presence_lookup
        self._clock = clock or datetime.now
        self._sleep = sleep or _default_sleep
        self._tick_seconds = max(1.0, float(tick_seconds))
        self._logger = logger or logging.getLogger(__name__)
        self._task: Optional[asyncio.Task] = None
        self._fired_date = None
        self._fired_today: set = set()

    @property
    def enabled(self) -> bool:
        return self._settings is not None

    def start(self) -> None:
        """启动每分钟对表任务；未启用（无配置/无合法提示/无工位映射）时不启动。"""
        if self._settings is None:
            self._logger.debug("timed_prompts 未启用或缺少可用配置，调度器不启动")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        """取消调度任务。可重复调用，不会抛异常。"""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        try:
            while True:
                await self.tick()
                await self._sleep(self._tick_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception("timed_prompts 调度任务异常退出")
            raise

    async def tick(self) -> None:
        """跑一次对表：检查是否有提示命中当前时间，命中就尝试播报。

        暴露为公开方法，便于单测直接驱动一次对表而不必绕过 asyncio 循环。
        """
        if self._settings is None:
            return

        now = self._clock()
        today = now.date()
        if self._fired_date != today:
            self._fired_date = today
            self._fired_today.clear()

        current_time = f"{now.hour:02d}:{now.minute:02d}"
        weekday = now.isoweekday()  # 1=周一...7=周日

        for prompt in self._settings.prompts:
            if prompt.index in self._fired_today:
                continue
            if prompt.time != current_time:
                continue
            if prompt.days is not None and weekday not in prompt.days:
                continue
            # 先占位再推送：同一天这一条只评估一次，不管推送是否真的送达任何工位，
            # 否则没人在的日子里，调度会在这一分钟之外的每一次 tick 都白跑一遍。
            self._fired_today.add(prompt.index)
            await self._fire(prompt)

    async def _fire(self, prompt: _Prompt) -> None:
        for workstation_id, device_id in self._settings.workstations.items():
            state = self._presence_state(workstation_id)
            if state != "present":
                self._logger.debug(
                    f"提示 {prompt.text!r} 到点，工位 {workstation_id} 当前"
                    f"{state or '无上报'}，跳过"
                )
                continue
            conn = self._resolve_device(device_id)
            if conn is None:
                self._logger.debug(
                    f"提示 {prompt.text!r} 到点，设备 {device_id} 不在线，跳过"
                )
                continue
            try:
                await self._push_event(
                    conn,
                    text=prompt.text,
                    emotion=prompt.emotion,
                    speak=prompt.speak,
                )
            except Exception:
                self._logger.exception(f"提示 {prompt.text!r} 推送失败")

    def _presence_state(self, workstation_id: str) -> Optional[str]:
        if self._presence_lookup is None:
            return None
        try:
            return self._presence_lookup(workstation_id)
        except Exception:
            self._logger.exception(f"查询工位 {workstation_id} 在岗状态失败")
            return None

    def _resolve_device(self, device_id: str):
        if self._device_resolver is None:
            return None
        try:
            return self._device_resolver(device_id)
        except Exception:
            self._logger.exception(f"解析设备 {device_id} 连接失败")
            return None


def create_timed_prompt_scheduler(
    config: dict, **kwargs
) -> Optional[TimedPromptScheduler]:
    """按配置装配调度器；未启用、无合法提示或缺少工位映射时返回 None。"""
    settings = _read_settings(config)
    if settings is None:
        return None
    return TimedPromptScheduler(config, settings=settings, **kwargs)
