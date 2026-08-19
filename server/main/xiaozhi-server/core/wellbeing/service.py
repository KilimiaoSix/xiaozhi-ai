"""Async adapter from presence snapshots to connected robot pushes."""

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Awaitable, Callable

from core.handle.pushHandle import push_work_event
from core.wellbeing.engine import WellbeingEngine, WellbeingPolicy


class WellbeingService:
    def __init__(
        self,
        config: dict,
        presence_registry,
        device_registry,
        *,
        now_provider: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        logger=None,
    ) -> None:
        self.policy = WellbeingPolicy.from_config(config)
        self._engine = WellbeingEngine(self.policy)
        self._presence_registry = presence_registry
        self._device_registry = device_registry
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or asyncio.sleep
        self._logger = logger or logging.getLogger(__name__)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.policy.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="wellbeing-reminders")

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
        now = self._now_provider()
        for workstation_id, configured_device in self.policy.bindings.items():
            presence = self._presence_registry.get(workstation_id)
            event = self._engine.evaluate(workstation_id, presence, now)
            if event is None:
                continue
            device_id = self._resolve_device(configured_device)
            conn = self._device_registry.get(device_id) if device_id else None
            if conn is None:
                self._logger.info(
                    f"Dropping wellbeing event {event.kind} for {workstation_id} "
                    "because no target device is online"
                )
                continue
            try:
                await push_work_event(
                    conn,
                    text=event.text,
                    emotion=event.emotion,
                    status=event.status,
                    speak=event.speak,
                    restore_after=event.restore_after,
                    action=event.action,
                    silent=event.silent,
                )
            except Exception:
                self._logger.exception(
                    f"Failed to deliver wellbeing event {event.kind} to device {device_id}"
                )

    def _resolve_device(self, configured_device: str) -> str | None:
        if configured_device:
            return configured_device
        devices = self._device_registry.device_ids()
        return devices[0] if len(devices) == 1 else None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("Unexpected wellbeing reminder loop failure")
            await self._sleep(self.policy.poll_seconds)
