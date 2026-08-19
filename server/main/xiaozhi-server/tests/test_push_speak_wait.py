"""主动推送要出声时，先尽力等设备空闲，必要时抢占它自己的播报。

此前 push_work_event 撞上忙态是直接放弃（不排队、不重试、不打断），
真机联调里工作事件的播报因此经常被吞掉。
"""

import pytest

from core.handle.pushHandle import ensure_speakable


class FakeConn:
    def __init__(self, busy_sequence, config=None):
        # 每次查询忙态弹出一个值，None 表示空闲
        self._sequence = list(busy_sequence)
        self._last = None
        self.config = config or {}
        self.tts = object()
        self.client_is_speaking = False
        self.client_have_voice = False
        self.aborted = 0

        class _Logger:
            def bind(self, **_k):
                return self

            def info(self, *_a, **_k):
                pass

            def warning(self, *_a, **_k):
                pass

        self.logger = _Logger()

    def next_busy(self):
        if self._sequence:
            self._last = self._sequence.pop(0)
        return self._last


def make(busy_sequence, **gate):
    config = {"push_speak": {**gate}} if gate else {}
    conn = FakeConn(busy_sequence, config)
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    aborted = []

    async def abort(c):
        aborted.append(c)
        c.aborted += 1
        # 打断之后设备不再播报
        conn._sequence = [None] * 5

    clock = {"t": 0.0}

    def now():
        clock["t"] += 0.3
        return clock["t"]

    return conn, slept, aborted, sleep, abort, now


async def run(conn, slept, aborted, sleep, abort, now, busy_probe=None):
    return await ensure_speakable(
        conn,
        busy_probe=busy_probe or (lambda c: c.next_busy()),
        sleep=sleep,
        clock=now,
        abort=abort,
    )


@pytest.mark.asyncio
async def test_idle_device_returns_immediately_without_waiting():
    args = make([None])

    assert await run(*args) is None
    assert args[1] == []  # 没有 sleep 过


@pytest.mark.asyncio
async def test_waits_out_a_short_busy_blip():
    """房间人声让 VAD 抖一下就好，等一会儿就能播。"""
    args = make(["用户正在说话", "用户正在说话", None])

    assert await run(*args) is None
    assert len(args[1]) == 2


@pytest.mark.asyncio
async def test_gives_up_after_the_configured_wait():
    args = make(["用户正在说话"] * 50, wait_seconds=1, poll_interval=0.3)

    assert await run(*args) == "用户正在说话"


@pytest.mark.asyncio
async def test_never_preempts_while_the_user_is_talking():
    """插播会污染这一轮的 ASR 与 client_abort 状态，用户说话时只等不抢。"""
    args = make(["用户正在说话"] * 50, wait_seconds=1, preempt_speaking=True)

    assert await run(*args) == "用户正在说话"
    assert args[2] == []  # 没有调用过 abort


@pytest.mark.asyncio
async def test_preempts_its_own_playback_when_enabled():
    args = make(["设备正在播放语音"] * 50, wait_seconds=1, preempt_speaking=True)

    assert await run(*args) is None
    assert len(args[2]) == 1


@pytest.mark.asyncio
async def test_preemption_is_off_by_default():
    args = make(["设备正在播放语音"] * 50, wait_seconds=1)

    assert await run(*args) == "设备正在播放语音"
    assert args[2] == []


@pytest.mark.asyncio
async def test_default_wait_stays_under_the_client_http_timeout():
    """桌面端推送客户端的 HTTP 超时是 5 秒，服务端等待必须明显短于它，
    否则调用方先超时，等待就白做了。"""
    from core.handle.pushHandle import DEFAULT_PUSH_WAIT_SECONDS

    assert DEFAULT_PUSH_WAIT_SECONDS <= 3.0
