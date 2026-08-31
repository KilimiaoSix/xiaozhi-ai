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


# ------------------------------------------- 抢播不能顺带取消「本轮说完就收会话」


class _WrapupConn:
    """正在播确认语、且这轮说完就该收会话的连接假体。

    比 FakeConn 多的字段都是真实 handleAbortMessage 会碰的：本用例刻意不注入
    abort 替身，走真打断，否则测不出「抢播清零收场位」这条接缝。
    """

    class _WS:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)

    class _Log:
        def bind(self, **_kw):
            return self

        def info(self, *_a, **_k):
            pass

        def warning(self, *_a, **_k):
            pass

    def __init__(self):
        self.config = {"push_speak": {"wait_seconds": 1, "preempt_speaking": True}}
        self.logger = self._Log()
        self.websocket = self._WS()
        self.session_id = "sess-wrapup"
        self.tts = object()
        self.client_is_speaking = True  # 确认语正在播
        self.client_have_voice = False
        self.client_abort = False
        # 用户刚说「我去开会了」：确认语的 LAST 句播完就该关这轮会话
        self.close_after_chat = True

    def clear_queues(self):
        pass

    def clearSpeakStatus(self):
        self.client_is_speaking = False


@pytest.mark.asyncio
async def test_preempting_its_own_playback_keeps_a_pending_session_wrapup():
    """服务端为插播而发的打断，不该把「声明离开 → 收会话」一起吃掉。

    handleAbortMessage 无条件清零 close_after_chat（那是给用户主动打断用的
    语义：打断告别就等于不走了），而抢播调的是同一个函数、不带 reason。真机
    时序：确认语「知道了，我帮你看着工位」播报期间来了条告警推送 → 抢播 →
    收场位被清零，确认语的 LAST 永远到不了 sendAudioHandle 的关闭判断，
    owner_status 已经落盘成 meeting，会话却继续挂着。
    """
    conn = _WrapupConn()
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    clock = {"t": 0.0}

    def now():
        clock["t"] += 0.3
        return clock["t"]

    busy = await ensure_speakable(conn, sleep=sleep, clock=now)

    assert busy is None  # 抢播成功，本次推送可以播
    assert conn.client_abort is True  # 打断确实发生了
    assert conn.close_after_chat is True  # 收场位留着，交给本次推送的 LAST 去执行


@pytest.mark.asyncio
async def test_preempt_does_not_invent_a_wrapup_that_was_not_pending():
    """本来就没有待办收场的连接，抢播后照旧不收会话。"""
    conn = _WrapupConn()
    conn.close_after_chat = False

    async def sleep(_seconds):
        pass

    clock = {"t": 0.0}

    def now():
        clock["t"] += 0.3
        return clock["t"]

    assert await ensure_speakable(conn, sleep=sleep, clock=now) is None
    assert conn.close_after_chat is False


class _GateConn:
    """push_work_event 走完 alert+speak 所需的最小连接桩。"""

    class _WS:
        async def send(self, _msg):
            pass

    class _Dlg:
        def put(self, _msg):
            pass

    class _Log:
        def bind(self, **_kw):
            return self

        def info(self, *_a, **_k):
            pass

        def warning(self, *_a, **_k):
            pass

    def __init__(self):
        self.config = {}
        self.websocket = self._WS()
        self.dialogue = self._Dlg()
        self.logger = self._Log()
        self.session_id = "s"
        self.device_id = "dc:da:0c:26:9a:60"
        self.tts = object()          # ensure_speakable 只查非 None
        self.client_is_speaking = False
        self.client_have_voice = False


def make_conn():
    return _GateConn()


@pytest.mark.asyncio
async def test_successful_speak_opens_dialogue_window(monkeypatch):
    """机器人主动开口后要打开对话窗口——告警后的"启动诊断"这类顺口应答
    曾被对话窗口门整句丢弃,用户只能先念唤醒词才能对话,不像人话。"""
    conn = make_conn()
    conn.config["dialogue_gate"] = {"enabled": True, "window_seconds": 60}

    async def fake_speak(c, text):
        return True

    monkeypatch.setattr("core.handle.pushHandle.speak_on_device", fake_speak)
    from core.handle.pushHandle import push_work_event

    spoke = await push_work_event(conn, text="线上告警:错误率升高", speak=True)

    assert spoke is True
    assert getattr(conn, "_dialogue_window_until", 0) > 0


@pytest.mark.asyncio
async def test_silent_push_does_not_open_dialogue_window(monkeypatch):
    conn = make_conn()
    conn.config["dialogue_gate"] = {"enabled": True, "window_seconds": 60}
    from core.handle.pushHandle import push_work_event

    await push_work_event(conn, text="静默提示", speak=False, silent=True)

    assert getattr(conn, "_dialogue_window_until", None) is None
