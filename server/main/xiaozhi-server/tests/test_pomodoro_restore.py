"""番茄钟会话的落盘与重启恢复。

原本模块头就写着「服务端不做持久化：进程重启会丢掉所有会话」。
代价是真实的：重启后服务端认为没有会话，设备却还停在自己 1Hz 自减的
倒计时画面上，两端各走各的，只能靠用户手动 stop 才收得回来。

本文件锁的是「新实例从同一 data 目录装载」：
- 仍在相位内 → 按墙钟重算 monotonic 截止、恢复计时任务、设备回连后刷新画面
- 已过期     → 丢会话，设备回连后推一次 idle 收屏
- 等待中被停 → 设备回连后照样要补一次 idle，否则没有任何一方给设备收屏
- 暂停中     → 恢复为暂停态，冻结的剩余秒数不因重启被扣掉
- 坏文件     → 按空存储处理，不崩在启动路径上

墙钟截止时刻必须存 ISO 时间：monotonic 的原点每次进程启动都不同，
存进文件的 monotonic 值重启后没有任何意义。
"""

import asyncio
import json
import time
from datetime import datetime, timedelta

import pytest

from core import pomodoro_manager as pomodoro_module
from core.pomodoro_manager import POMODORO_SHOW_TOOL, PomodoroManager


DEVICE_ID = "dc:da:0c:26:9a:60"

# 0.01 分钟 = 0.6 秒（同 test_pomodoro_manager.py）
TINY_MINUTES = 0.01
PHASE_SECONDS = 0.6
SETTLE_SECONDS = 0.3


async def wait_until(predicate, timeout=5.0, what="条件"):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert predicate(), f"等待超时：{what}"


class FakeConn:
    def __init__(self, device_id=DEVICE_ID):
        self.device_id = device_id
        self.session_id = "fake-session"
        self.mcp_client = object()


class FakeRegistry:
    def __init__(self, conns=None):
        self._conns = dict(conns or {})

    def get(self, device_id):
        return self._conns.get(device_id)

    def device_ids(self):
        return list(self._conns.keys())

    def plug_in(self, device_id=DEVICE_ID):
        """模拟设备回连。"""
        self._conns[device_id] = FakeConn(device_id)


class ToolRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, conn, mcp_client, tool_name, args, timeout=30):
        self.calls.append(
            {
                "device_id": conn.device_id,
                "tool": tool_name,
                "args": json.loads(args),
            }
        )
        return "true"

    def shows(self, device_id=DEVICE_ID):
        return [
            call["args"]
            for call in self.calls
            if call["tool"] == POMODORO_SHOW_TOOL and call["device_id"] == device_id
        ]

    def phases(self, device_id=DEVICE_ID):
        return [args["phase"] for args in self.shows(device_id)]


class AlertRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, conn, text, emotion=None, status=None, silent=False):
        self.calls.append({"text": text, "emotion": emotion})

    @property
    def texts(self):
        return [call["text"] for call in self.calls]


class ActionRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, conn, action):
        self.calls.append(action)
        return True


class FakeWallClock:
    """墙钟。重启恢复靠它算「这段时间过去了多久」，必须可注入。"""

    def __init__(self, start=None):
        self.now = start or datetime(2026, 8, 28, 9, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=float(seconds))


def make_manager(
    store,
    *,
    focus=25.0,
    short_break=5.0,
    long_break=15.0,
    interval=4,
    registry=None,
    wall_clock=None,
):
    config = {
        "pomodoro": {
            "focus_minutes": focus,
            "short_break_minutes": short_break,
            "long_break_minutes": long_break,
            "long_break_interval": interval,
            "persist_path": str(store),
        }
    }
    if registry is None:
        registry = FakeRegistry({DEVICE_ID: FakeConn()})
    tools = ToolRecorder()
    manager = PomodoroManager(
        config,
        registry,
        push_alert=AlertRecorder(),
        play_action=ActionRecorder(),
        call_tool=tools,
        celebration_delay_s=0.0,
        wall_clock=wall_clock or FakeWallClock(),
    )
    return manager, tools


async def settle(manager, timeout=5.0):
    await wait_until(lambda: not manager._push_tasks, timeout, "后台推送任务跑完")


async def shutdown(manager):
    for device_id in list(manager.active_device_ids()):
        await manager.stop(device_id)
    # 设备离线时补帧任务会一直等回连（生产上正是要这样），测试收尾直接取消掉
    for task in list(manager._push_tasks):
        task.cancel()
    await settle(manager)


async def crash(manager):
    """模拟进程被 kill：计时任务原地消失，盘上的快照保持不动。

    不能用 stop()——那是用户主动停止，会把会话从盘上抹掉，
    正好抹掉本文件要验的东西。
    """
    await settle(manager)
    for session in list(manager._sessions.values()):
        if session.task is not None and not session.task.done():
            session.task.cancel()
    manager._sessions.clear()


@pytest.fixture
def store(tmp_path):
    return tmp_path / "pomodoro_sessions.json"


@pytest.fixture
def fast_reconnect_poll(monkeypatch):
    """真实的 1s 回连轮询语义不变，只是压短到测试跑得完（同 SHOW_RETRY_DELAY 的做法）。"""
    monkeypatch.setattr(pomodoro_module, "RESYNC_POLL_INTERVAL", 0.02)


# ------------------------------------------------------------------ 落盘

@pytest.mark.asyncio
async def test_start_persists_the_session(store):
    manager, _ = make_manager(store)

    await manager.start(DEVICE_ID)
    await settle(manager)

    data = json.loads(store.read_text(encoding="utf-8"))
    saved = data["sessions"][0]
    assert saved["device_id"] == DEVICE_ID
    assert saved["phase"] == "focus"
    assert saved["paused"] is False
    # 墙钟截止时刻必须是 ISO 时间：monotonic 值重启后毫无意义。
    # 注入的墙钟不走，但 monotonic 真的在走，落差是那几十微秒
    saved_deadline = datetime.fromisoformat(saved["deadline_at"])
    assert abs((saved_deadline - datetime(2026, 8, 28, 9, 25, 0)).total_seconds()) < 1
    assert list(store.parent.glob("*.tmp")) == []

    await shutdown(manager)


@pytest.mark.asyncio
async def test_stop_removes_the_session_from_disk(store):
    manager, _ = make_manager(store)

    await manager.start(DEVICE_ID)
    await manager.stop(DEVICE_ID)
    await settle(manager)

    assert json.loads(store.read_text(encoding="utf-8"))["sessions"] == []


# --------------------------------------------------------------- 相位内恢复

@pytest.mark.asyncio
async def test_session_inside_the_phase_is_restored_by_wall_clock(store):
    """重启后仍在相位内：按墙钟重算剩余时间，而不是从头开始。"""
    clock = FakeWallClock()
    manager, _ = make_manager(store, wall_clock=clock)
    await manager.start(DEVICE_ID)
    await crash(manager)

    clock.advance(600)  # 重启这 10 分钟照样要从专注里扣掉
    fresh, tools = make_manager(store, wall_clock=clock)
    await fresh.restore()

    snapshot = (await fresh.status(DEVICE_ID))["status"]
    assert snapshot["active"] is True
    assert snapshot["phase"] == "focus"
    assert snapshot["round"] == 1
    assert 25 * 60 - 600 - 2 <= snapshot["remaining_s"] <= 25 * 60 - 600
    # 设备在线就顺手把画面刷新一次，别让它接着显示自己那份陈旧倒计时
    await wait_until(lambda: tools.phases() == ["focus"], what="恢复后刷新画面")

    await shutdown(fresh)


@pytest.mark.asyncio
async def test_restored_session_keeps_ticking_into_the_next_phase(store):
    """恢复的不只是快照，还有计时任务：到点必须照常转相位。"""
    clock = FakeWallClock()
    manager, _ = make_manager(
        store, focus=TINY_MINUTES, short_break=TINY_MINUTES, wall_clock=clock
    )
    await manager.start(DEVICE_ID)
    await crash(manager)

    fresh, tools = make_manager(
        store, focus=TINY_MINUTES, short_break=TINY_MINUTES, wall_clock=clock
    )
    await fresh.restore()

    await wait_until(
        lambda: tools.phases()[-1:] == ["short_break"], what="恢复后的计时器把相位推进"
    )

    await shutdown(fresh)


@pytest.mark.asyncio
async def test_restored_session_refreshes_the_screen_after_the_device_reconnects(
    store, fast_reconnect_poll
):
    """设备总比服务端晚一步回来，画面要等它回连再推。"""
    clock = FakeWallClock()
    manager, _ = make_manager(store, wall_clock=clock)
    await manager.start(DEVICE_ID)
    await crash(manager)

    registry = FakeRegistry({})  # 服务端起来时设备还没连上
    fresh, tools = make_manager(store, registry=registry, wall_clock=clock)
    await fresh.restore()
    await asyncio.sleep(0.05)
    assert tools.shows() == []

    registry.plug_in()
    await wait_until(lambda: tools.phases() == ["focus"], what="设备回连后补推画面")

    await shutdown(fresh)


# ------------------------------------------------------------------ 过期丢弃

@pytest.mark.asyncio
async def test_expired_session_is_dropped_and_idle_collapses_the_screen(store):
    """重启时相位早就过完了：会话作废，设备那张倒计时画面得收回去。"""
    clock = FakeWallClock()
    manager, _ = make_manager(store, wall_clock=clock)
    await manager.start(DEVICE_ID)
    await crash(manager)

    clock.advance(25 * 60 + 60)  # 睡了一觉才重启
    fresh, tools = make_manager(store, wall_clock=clock)
    await fresh.restore()

    assert fresh.active_device_ids() == []
    await wait_until(lambda: tools.phases() == ["idle"], what="过期会话推 idle 收屏")
    # 作废的会话不该留在盘上等下次重启再来一遍
    assert json.loads(store.read_text(encoding="utf-8"))["sessions"] == []


@pytest.mark.asyncio
async def test_expired_session_waits_for_the_device_before_pushing_idle(
    store, fast_reconnect_poll
):
    clock = FakeWallClock()
    manager, _ = make_manager(store, wall_clock=clock)
    await manager.start(DEVICE_ID)
    await crash(manager)

    clock.advance(25 * 60 + 60)
    registry = FakeRegistry({})
    fresh, tools = make_manager(store, registry=registry, wall_clock=clock)
    await fresh.restore()
    await asyncio.sleep(0.05)
    assert tools.shows() == []

    registry.plug_in()
    await wait_until(lambda: tools.phases() == ["idle"], what="设备回连后补推 idle")


@pytest.mark.asyncio
async def test_new_session_started_while_waiting_for_device_wins_over_stale_idle_task(
    store, fast_reconnect_poll
):
    """过期会话的收屏任务不能拍飞回连前另起的新会话。

    restore() 给过期会话挂的补帧任务（_resync_when_online）会等设备回连。
    这段等待窗口里，用户完全可能经 HTTP / 语音 / 按键对同一设备重新 start 了一个
    新会话——新会话 start() 里的画面下发因为设备当时还离线发不出去，只会被并到
    同一个补帧任务上（每设备去重）。设备回连时，如果补帧无条件推 idle 而不重新
    查一遍 self._sessions，就会把刚起的新会话画面覆盖成 idle：服务端 /status 报
    "进行中"，设备却被钉在 idle，直到相位自然到点才恢复。
    """
    clock = FakeWallClock()
    manager, _ = make_manager(store, wall_clock=clock)
    await manager.start(DEVICE_ID)
    await crash(manager)

    clock.advance(25 * 60 + 60)  # 重启时旧会话早已过期
    registry = FakeRegistry({})  # 服务端起来时设备还没连上
    fresh, tools = make_manager(store, registry=registry, wall_clock=clock)
    await fresh.restore()
    assert fresh.active_device_ids() == []  # 过期会话确实被丢弃了

    # 回连前，用户对同一设备重新开始了一个新会话
    # 注意：不能用 settle() —— 补帧任务还在等设备回连，
    # push_tasks 要到 registry.plug_in() 之后才会清空
    started = await fresh.start(DEVICE_ID)
    assert started["outcome"] == "started"
    await asyncio.sleep(0.05)
    # 佐证：设备当时还离线，start() 的画面下发确实静默失败了，不会自己重试
    assert tools.shows() == []

    registry.plug_in()
    await wait_until(lambda: tools.shows() != [], what="设备回连后至少收到一次画面")

    # 设备收到的最后一次画面必须是新会话的相位，而不是过期任务补推的 idle
    assert tools.phases()[-1] == "focus"
    assert (await fresh.status(DEVICE_ID))["status"]["active"] is True

    await shutdown(fresh)


@pytest.mark.asyncio
async def test_stop_while_waiting_for_the_device_collapses_the_screen_on_reconnect(
    store, fast_reconnect_poll
):
    """恢复的等待窗口内被 stop：设备回连后仍要收一次 idle 收屏。

    restore 给恢复出来的会话挂了个等设备回连的补帧任务。等待期间用户从桌面端
    stop（设备离线不影响 HTTP 通路），stop 那侧的 idle 因为设备不在线被当场丢弃。
    补帧任务醒来若只是"会话没了就算了"，两边一让就没人负责收屏：设备停在自己
    1Hz 自减出来的倒计时上，用户必须在回连后再 stop 一次——正是持久化要消除的代价。
    """
    clock = FakeWallClock()
    manager, _ = make_manager(store, wall_clock=clock)
    await manager.start(DEVICE_ID)
    await crash(manager)

    registry = FakeRegistry({})  # 服务端起来时设备还没连上
    fresh, tools = make_manager(store, registry=registry, wall_clock=clock)
    await fresh.restore()
    assert fresh.active_device_ids() == [DEVICE_ID]

    stopped = await fresh.stop(DEVICE_ID)
    assert stopped["outcome"] == "stopped"
    await asyncio.sleep(0.05)
    # 佐证：设备离线时 stop 的 idle 确实发不出去
    assert tools.shows() == []

    registry.plug_in()
    await wait_until(
        lambda: tools.phases() == ["idle"], what="设备回连后补推 idle 收屏"
    )


# ------------------------------------------------------------------ 暂停恢复

@pytest.mark.asyncio
async def test_paused_session_restores_as_paused_with_frozen_remaining(store):
    clock = FakeWallClock()
    manager, _ = make_manager(store, wall_clock=clock)
    await manager.start(DEVICE_ID)
    clock.advance(300)
    paused = await manager.pause(DEVICE_ID)
    frozen = paused["status"]["remaining_s"]
    await crash(manager)

    clock.advance(3600)  # 暂停期间过了多久都不该被扣掉
    fresh, tools = make_manager(store, wall_clock=clock)
    await fresh.restore()

    snapshot = (await fresh.status(DEVICE_ID))["status"]
    assert snapshot["active"] is True
    assert snapshot["paused"] is True
    assert snapshot["remaining_s"] == frozen
    await wait_until(
        lambda: tools.shows() and tools.shows()[-1]["paused"] is True,
        what="恢复后刷新暂停画面",
    )

    # 暂停态恢复后照样能继续
    resumed = await fresh.resume(DEVICE_ID)
    assert resumed["outcome"] == "resumed"
    assert resumed["status"]["remaining_s"] == frozen

    await shutdown(fresh)


@pytest.mark.asyncio
async def test_running_session_restore_clamps_remaining_to_the_phase_length(store):
    """墙钟被回拨后恢复：剩余时间不许超过相位总长。

    盘上存的是墙钟截止时刻，重启时（开机 NTP 步进最容易发生）系统时间往回拨，
    (deadline_at - now) 就比整段相位还长：该相位实际多跑这段时间，下发给固件的
    remaining_s 也越界，进度条 lv_bar_set_value(total_s - remaining) 直接为负。
    暂停分支本来就双向夹紧了，运行态分支漏了上界这一次。
    """
    clock = FakeWallClock()
    manager, _ = make_manager(store, wall_clock=clock)
    await manager.start(DEVICE_ID)
    await crash(manager)

    clock.advance(-20 * 60)  # 开机 NTP 把墙钟往回拨了 20 分钟
    fresh, _ = make_manager(store, wall_clock=clock)
    await fresh.restore()

    snapshot = (await fresh.status(DEVICE_ID))["status"]
    assert snapshot["active"] is True
    assert snapshot["total_s"] == 25 * 60
    # 最坏影响限制为"这一相位从头开始"，而不是多跑 20 分钟
    assert snapshot["remaining_s"] == 25 * 60

    await shutdown(fresh)


# ------------------------------------------------------------ 代次 / 坏文件

@pytest.mark.asyncio
async def test_pausing_a_restored_session_cancels_its_timer(store):
    """恢复出来的计时任务同样受 generation 管：暂停之后不许再自己往前走。"""
    clock = FakeWallClock()
    manager, _ = make_manager(
        store, focus=TINY_MINUTES, short_break=TINY_MINUTES, wall_clock=clock
    )
    await manager.start(DEVICE_ID)
    await crash(manager)

    fresh, tools = make_manager(
        store, focus=TINY_MINUTES, short_break=TINY_MINUTES, wall_clock=clock
    )
    await fresh.restore()
    result = await fresh.pause(DEVICE_ID)
    assert result["outcome"] == "paused"
    await wait_until(
        lambda: tools.shows() and tools.shows()[-1]["paused"] is True,
        what="pause 的画面回显落地",
    )
    baseline = len(tools.shows())

    await asyncio.sleep(PHASE_SECONDS + SETTLE_SECONDS)
    await settle(fresh)

    assert tools.shows()[baseline:] == []
    assert (await fresh.status(DEVICE_ID))["status"]["phase"] == "focus"

    await shutdown(fresh)


@pytest.mark.asyncio
async def test_corrupt_store_is_treated_as_empty(store):
    store.write_text('{"version": 1, "sessions": [{"device', encoding="utf-8")

    fresh, tools = make_manager(store)
    await fresh.restore()

    assert fresh.active_device_ids() == []
    await settle(fresh)
    assert tools.shows() == []
    # 坏文件不能一直卡着：下一次写入要能整体覆盖
    await fresh.start(DEVICE_ID)
    await settle(fresh)
    assert json.loads(store.read_text(encoding="utf-8"))["sessions"][0][
        "device_id"
    ] == DEVICE_ID

    await shutdown(fresh)


@pytest.mark.asyncio
async def test_missing_store_file_restores_nothing(store):
    fresh, _ = make_manager(store)

    await fresh.restore()

    assert fresh.active_device_ids() == []


@pytest.mark.asyncio
async def test_restore_does_not_clobber_a_live_session(store):
    """restore 只补空缺：已经跑起来的会话不该被盘上的旧快照覆盖。"""
    clock = FakeWallClock()
    manager, _ = make_manager(store, wall_clock=clock)
    await manager.start(DEVICE_ID)
    await crash(manager)
    stale = store.read_text(encoding="utf-8")

    fresh, _ = make_manager(store, wall_clock=clock)
    await fresh.start(DEVICE_ID, focus_minutes=45)
    # 新会话已经把文件覆盖掉了，把旧快照放回去，restore 才有机会犯这个错
    store.write_text(stale, encoding="utf-8")
    await fresh.restore()

    assert (await fresh.status(DEVICE_ID))["status"]["total_s"] == 45 * 60

    await shutdown(fresh)
