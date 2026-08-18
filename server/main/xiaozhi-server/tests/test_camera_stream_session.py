import asyncio

import pytest

from core.camera_stream.session import CameraStreamSession, LatestFrameSlot


@pytest.mark.asyncio
async def test_latest_frame_slot_replaces_pending_frame_and_counts_drop():
    slot = LatestFrameSlot()

    assert slot.replace(1, b"first") == 0
    assert slot.replace(2, b"latest") == 1

    frame = await slot.take()
    assert (frame.sequence, frame.jpeg) == (2, b"latest")

    slot.close()
    assert await slot.take() is None


@pytest.mark.asyncio
async def test_close_wakes_waiting_consumer():
    slot = LatestFrameSlot()
    waiting = asyncio.create_task(slot.take())
    await asyncio.sleep(0)

    slot.close()

    assert await asyncio.wait_for(waiting, timeout=0.1) is None


@pytest.mark.asyncio
async def test_session_processes_in_flight_and_latest_pending_frame_once():
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    processed = []
    emitted = []

    async def process(frame):
        processed.append(frame.sequence)
        if frame.sequence == 1:
            first_started.set()
            await release_first.wait()
        return {"sequence": frame.sequence}

    async def emit(result):
        emitted.append(result["sequence"])

    session = CameraStreamSession(process, emit)
    session.start()
    assert session.replace(1, b"one") == 0
    await asyncio.wait_for(first_started.wait(), timeout=0.1)

    assert session.replace(2, b"two") == 0
    assert session.replace(3, b"three") == 1
    release_first.set()

    for _ in range(20):
        if emitted == [1, 3]:
            break
        await asyncio.sleep(0)
    await session.close()

    assert processed == [1, 3]
    assert emitted == [1, 3]
    assert session.running is False


@pytest.mark.asyncio
async def test_cancel_stops_blocked_processing_task():
    started = asyncio.Event()
    never = asyncio.Event()

    async def process(frame):
        started.set()
        await never.wait()

    async def emit(result):
        raise AssertionError("cancelled processing must not emit")

    session = CameraStreamSession(process, emit)
    session.start()
    session.replace(1, b"jpeg")
    await asyncio.wait_for(started.wait(), timeout=0.1)

    await session.cancel()

    assert session.running is False
    assert session.task is None
