from datetime import datetime, timezone

import pytest

from core.wellbeing.engine import WellbeingEvent
from core.wellbeing.service import WellbeingService


class FakePresenceRegistry:
    def get(self, workstation_id):
        return {"effective_state": "present", "identity": {"state": "owner"}}


class FakeDeviceRegistry:
    def __init__(self, devices):
        self.devices = devices

    def device_ids(self):
        return list(self.devices)

    def get(self, device_id):
        return self.devices.get(device_id)


class EventEngine:
    def evaluate(self, workstation_id, presence, now):
        return WellbeingEvent("warm", "加油", "loving", "给你加油", "nod")


@pytest.mark.asyncio
async def test_single_online_device_is_auto_bound(monkeypatch):
    conn = object()
    pushes = []

    async def fake_push(target, **kwargs):
        pushes.append((target, kwargs))

    monkeypatch.setattr("core.wellbeing.service.push_work_event", fake_push)
    service = WellbeingService(
        {"wellbeing": {"bindings": [{"workstation_id": "desk", "device_id": ""}]}},
        FakePresenceRegistry(),
        FakeDeviceRegistry({"robot": conn}),
        now_provider=lambda: datetime(2026, 8, 18, tzinfo=timezone.utc),
    )
    service._engine = EventEngine()

    await service.tick()

    assert pushes[0][0] is conn
    assert pushes[0][1]["emotion"] == "loving"


@pytest.mark.asyncio
async def test_ambiguous_devices_drop_event_without_guessing(monkeypatch):
    pushes = []

    async def fake_push(target, **kwargs):
        pushes.append((target, kwargs))

    monkeypatch.setattr("core.wellbeing.service.push_work_event", fake_push)
    service = WellbeingService(
        {"wellbeing": {"bindings": [{"workstation_id": "desk", "device_id": ""}]}},
        FakePresenceRegistry(),
        FakeDeviceRegistry({"one": object(), "two": object()}),
    )
    service._engine = EventEngine()

    await service.tick()

    assert pushes == []


@pytest.mark.asyncio
async def test_explicit_offline_device_is_not_redirected(monkeypatch):
    pushes = []

    async def fake_push(target, **kwargs):
        pushes.append((target, kwargs))

    monkeypatch.setattr("core.wellbeing.service.push_work_event", fake_push)
    service = WellbeingService(
        {
            "wellbeing": {
                "bindings": [
                    {"workstation_id": "desk", "device_id": "expected"}
                ]
            }
        },
        FakePresenceRegistry(),
        FakeDeviceRegistry({"other": object()}),
    )
    service._engine = EventEngine()

    await service.tick()

    assert pushes == []


@pytest.mark.asyncio
async def test_offline_daily_event_is_not_replayed_after_reconnect(monkeypatch):
    pushes = []
    devices = FakeDeviceRegistry({})
    current_time = datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc)

    async def fake_push(target, **kwargs):
        pushes.append((target, kwargs))

    monkeypatch.setattr("core.wellbeing.service.push_work_event", fake_push)
    service = WellbeingService(
        {
            "wellbeing": {
                "bindings": [
                    {"workstation_id": "desk", "device_id": "robot"}
                ]
            }
        },
        FakePresenceRegistry(),
        devices,
        now_provider=lambda: current_time,
    )

    await service.tick()
    devices.devices["robot"] = object()
    current_time = datetime(2026, 8, 18, 13, 1, tzinfo=timezone.utc)
    await service.tick()

    assert pushes == []


@pytest.mark.asyncio
async def test_background_loop_starts_and_stops_cleanly():
    service = WellbeingService(
        {"wellbeing": {"enabled": True}},
        FakePresenceRegistry(),
        FakeDeviceRegistry({}),
    )

    await service.start()
    task = service._task
    await service.start()
    assert service._task is task

    await service.stop()

    assert service._task is None
    assert task.cancelled()
