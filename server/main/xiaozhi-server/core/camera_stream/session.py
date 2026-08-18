"""Bounded latest-frame processing for desktop camera streams."""

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass(frozen=True)
class Frame:
    sequence: int
    jpeg: bytes


class LatestFrameSlot:
    def __init__(self) -> None:
        self._latest: Frame | None = None
        self._closed = False
        self._available = asyncio.Event()

    def replace(self, sequence: int, jpeg: bytes) -> int:
        if self._closed:
            raise RuntimeError("frame slot is closed")
        dropped = int(self._latest is not None)
        self._latest = Frame(sequence, jpeg)
        self._available.set()
        return dropped

    async def take(self) -> Frame | None:
        while self._latest is None and not self._closed:
            await self._available.wait()
            self._available.clear()
        if self._latest is None:
            return None
        frame = self._latest
        self._latest = None
        return frame

    def close(self) -> None:
        self._closed = True
        self._available.set()


class CameraStreamSession:
    def __init__(
        self,
        process: Callable[[Frame], Awaitable[dict]],
        emit: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._slot = LatestFrameSlot()
        self._process = process
        self._emit = emit
        self._task: asyncio.Task | None = None

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("camera stream session already started")
        self._task = asyncio.create_task(self._run())

    def replace(self, sequence: int, jpeg: bytes) -> int:
        return self._slot.replace(sequence, jpeg)

    async def close(self) -> None:
        self._slot.close()
        task = self._task
        if task is not None:
            await task
            self._task = None

    async def cancel(self) -> None:
        self._slot.close()
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            frame = await self._slot.take()
            if frame is None:
                return
            result = await self._process(frame)
            await self._emit(result)
