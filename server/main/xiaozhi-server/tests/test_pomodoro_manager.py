"""番茄钟状态机与推送编排的离线测试。

全部离线：设备注册表、alert / 动作 / MCP 工具调用都注入假实现，相位时长压到
亚秒级，不依赖真机、网络与 LLM。
"""

import asyncio
import json
import time

import pytest

from core import pomodoro_manager as pomodoro_module
from core.pomodoro_manager import POMODORO_SHOW_TOOL, PomodoroManager


DEVICE_ID = "dc:da:0c:26:9a:60"
OTHER_DEVICE_ID = "dc:da:0c:26:9a:61"

# 0.01 分钟 = 0.6 秒。真机语义不变，只是把相位压短到测试跑得完。
TINY_MINUTES = 0.01
PHASE_SECONDS = 0.6
# 相位到点后留给编排（alert + 动作 + show）落地的余量
SETTLE_SECONDS = 0.3


async def wait_until(predicate, timeout=5.0, what="条件"):
    """轮询等到 predicate 成立。

    不按固定时长 sleep：多相位串起来时每次多等的余量会累积成漂移，
    等着等着就跨过了下一个相位边界。
    """
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert predicate(), f"等待超时：{what}"


async def wait_for_shows(tools, count, device_id=DEVICE_ID, timeout=5.0):
    """等到设备累计收到 count 次画面下发。

    命令路径的画面下发是射后不理的后台任务，命令返回时它往往还没跑，
    所以断言前一律先等，别假设推送已经落地。
    """
    await wait_until(
        lambda: len(tools.shows(device_id)) >= count,
        timeout,
        f"{device_id} 收到 {count} 次画面下发（只等到 {len(tools.shows(device_id))} 次）",
    )


async def wait_pushes_settled(manager, timeout=5.0):
    """等射后不理的推送任务全部跑完（含重试窗口）。

    盯管理器手里的任务集合，重试窗口多长都判得准；
    "推送没有再发生"这类断言必须等到这里，不然只是抢在任务被调度之前看了一眼。
    """
    await wait_until(lambda: not manager._push_tasks, timeout, "后台推送任务跑完")


async def wait_phase_end():
    """等到当前相位到点并且转相位编排跑完（只适用于单次转相位）。"""
    await asyncio.sleep(PHASE_SECONDS + SETTLE_SECONDS)


class FakeConn:
    """只实现推送路径用到的属性。"""

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


class AlertRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, conn, text, emotion=None, status=None, silent=False):
        self.calls.append(
            {
                "device_id": conn.device_id,
                "text": text,
                "emotion": emotion,
                "status": status,
                "silent": silent,
            }
        )

    @property
    def texts(self):
        return [call["text"] for call in self.calls]


class ActionRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, conn, action):
        self.calls.append({"device_id": conn.device_id, "action": action})
        return True

    @property
    def actions(self):
        return [call["action"] for call in self.calls]


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


def make_manager(
    *,
    focus=TINY_MINUTES,
    short_break=TINY_MINUTES,
    long_break=TINY_MINUTES,
    interval=4,
    registry=None,
    celebration_delay_s=0.0,
):
    config = {
        "pomodoro": {
            "focus_minutes": focus,
            "short_break_minutes": short_break,
            "long_break_minutes": long_break,
            "long_break_interval": interval,
        }
    }
    if registry is None:
        registry = FakeRegistry({DEVICE_ID: FakeConn()})
    alerts = AlertRecorder()
    actions = ActionRecorder()
    tools = ToolRecorder()
    manager = PomodoroManager(
        config,
        registry,
        push_alert=alerts,
        play_action=actions,
        call_tool=tools,
        celebration_delay_s=celebration_delay_s,
    )
    return manager, alerts, actions, tools


async def shutdown(manager):
    """收掉后台计时任务与推送任务，避免测试之间互相干扰。"""
    for device_id in list(manager.active_device_ids()):
        await manager.stop(device_id)
    await wait_pushes_settled(manager)


# ------------------------------------------------------------------ 相位序列


@pytest.mark.asyncio
async def test_focus_then_short_break_then_next_focus_round():
    manager, alerts, actions, tools = make_manager()

    await manager.start(DEVICE_ID)
    await wait_for_shows(tools, 1)
    assert tools.shows()[0] == {
        "phase": "focus",
        "paused": False,
        "remaining_s": 1,
        "total_s": 1,
        "round": 1,
        "total_rounds": 4,
    }

    await wait_for_shows(tools, 2)
    assert tools.phases() == ["focus", "short_break"]
    # 短休期间 round 停在刚完成的那一轮
    assert tools.shows()[1]["round"] == 1
    assert alerts.texts[0].startswith("专注结束，休息")
    assert alerts.calls[0]["silent"] is False
    assert actions.actions == ["roll"]

    await wait_for_shows(tools, 3)
    assert tools.phases() == ["focus", "short_break", "focus"]
    assert tools.shows()[2]["round"] == 2
    assert alerts.texts[1] == "休息结束，第2轮专注开始"
    assert actions.actions == ["roll", "nod"]

    await shutdown(manager)


@pytest.mark.asyncio
async def test_long_break_after_interval_then_back_to_first_round():
    manager, alerts, actions, tools = make_manager(interval=2)

    await manager.start(DEVICE_ID)
    await wait_for_shows(tools, 5)

    assert tools.phases() == ["focus", "short_break", "focus", "long_break", "focus"]
    long_break_args = tools.shows()[3]
    # 长休时 round 记成 total_rounds，进度条才是满的
    assert long_break_args["round"] == 2
    assert long_break_args["total_rounds"] == 2
    assert alerts.texts[2].startswith("完成2轮专注，长休")
    # 长休结束回到第 1 轮
    assert tools.shows()[4]["round"] == 1
    assert alerts.texts[3] == "休息结束，第1轮专注开始"

    await shutdown(manager)


# --------------------------------------------------------------- 暂停 / 继续


@pytest.mark.asyncio
async def test_pause_freezes_remaining_and_resume_continues():
    manager, _, _, tools = make_manager(focus=1.0)  # 60 秒，够慢到能观察冻结

    await manager.start(DEVICE_ID)
    await asyncio.sleep(0.2)
    paused = await manager.pause(DEVICE_ID)

    assert paused["outcome"] == "paused"
    assert paused["status"]["paused"] is True
    frozen = paused["status"]["remaining_s"]
    assert 0 < frozen <= 60

    await asyncio.sleep(0.4)
    still = await manager.status(DEVICE_ID)
    assert still["status"]["remaining_s"] == frozen
    await wait_for_shows(tools, 2)
    assert tools.shows()[-1]["paused"] is True

    resumed = await manager.resume(DEVICE_ID)
    assert resumed["outcome"] == "resumed"
    assert resumed["status"]["paused"] is False
    # 暂停的那 0.4 秒不该被扣掉
    assert resumed["status"]["remaining_s"] == frozen
    await wait_for_shows(tools, 3)
    assert tools.shows()[-1]["paused"] is False

    await shutdown(manager)


@pytest.mark.asyncio
async def test_pause_and_resume_on_missing_session_do_not_raise():
    manager, _, _, tools = make_manager()

    paused = await manager.pause(DEVICE_ID)
    resumed = await manager.resume(DEVICE_ID)
    skipped = await manager.skip(DEVICE_ID)

    assert paused["outcome"] == "not_running"
    assert resumed["outcome"] == "not_running"
    assert skipped["outcome"] == "not_running"
    assert paused["status"]["active"] is False
    assert paused["status"]["phase"] == "idle"
    # 没有会话就没有画面可推
    assert tools.shows() == []


@pytest.mark.asyncio
async def test_toggle_walks_start_pause_resume():
    manager, _, _, _ = make_manager(focus=1.0)

    first = await manager.toggle(DEVICE_ID)
    second = await manager.toggle(DEVICE_ID)
    third = await manager.toggle(DEVICE_ID)

    assert [first["outcome"], second["outcome"], third["outcome"]] == [
        "started",
        "paused",
        "resumed",
    ]
    assert third["status"]["active"] is True
    assert third["status"]["paused"] is False

    await shutdown(manager)


@pytest.mark.asyncio
async def test_start_twice_does_not_restart_the_session():
    manager, _, _, tools = make_manager(focus=1.0)

    await manager.start(DEVICE_ID)
    again = await manager.start(DEVICE_ID)

    assert again["outcome"] == "already_running"
    # 不重开就不该再推一次画面
    await wait_for_shows(tools, 1)
    await wait_pushes_settled(manager)
    assert len(tools.shows()) == 1

    await shutdown(manager)


# ------------------------------------------------------------------ skip/stop


@pytest.mark.asyncio
async def test_skip_transitions_immediately():
    manager, alerts, actions, tools = make_manager(focus=10.0, short_break=10.0)

    await manager.start(DEVICE_ID)
    result = await manager.skip(DEVICE_ID)
    assert result["outcome"] == "skipped"

    # 跳过走的是正常的相位到点编排，只是不等计时器
    await wait_for_shows(tools, 2)
    assert tools.phases() == ["focus", "short_break"]
    assert alerts.texts[0].startswith("专注结束，休息")
    assert actions.actions == ["roll"]

    await shutdown(manager)


@pytest.mark.asyncio
async def test_stop_clears_session_and_pushes_idle():
    manager, _, _, tools = make_manager(focus=10.0)

    await manager.start(DEVICE_ID)
    result = await manager.stop(DEVICE_ID)

    assert result["outcome"] == "stopped"
    assert result["status"]["active"] is False
    assert manager.active_device_ids() == []
    await wait_for_shows(tools, 2)
    assert tools.shows()[-1] == {
        "phase": "idle",
        "paused": False,
        "remaining_s": 0,
        "total_s": 0,
        "round": 0,
        "total_rounds": 4,
    }


@pytest.mark.asyncio
async def test_stop_without_session_still_pushes_idle():
    """服务端重启会丢会话，设备却还停在番茄钟画面，stop 得能把它收回去。"""
    manager, _, _, tools = make_manager()

    result = await manager.stop(DEVICE_ID)

    assert result["outcome"] == "not_running"
    await wait_for_shows(tools, 1)
    assert tools.phases() == ["idle"]


# ------------------------------------------------------------ 离线 / 多设备


@pytest.mark.asyncio
async def test_offline_device_keeps_ticking_without_raising():
    manager, alerts, actions, tools = make_manager(registry=FakeRegistry({}))

    started = await manager.start(DEVICE_ID)
    assert started["outcome"] == "started"
    assert started["status"]["connected"] is False
    assert started["status"]["active"] is True

    await wait_phase_end()

    # 一条推送都发不出去，但状态机照常走到了短休
    assert tools.calls == []
    assert alerts.calls == []
    assert actions.calls == []
    assert (await manager.status(DEVICE_ID))["status"]["phase"] == "short_break"

    await shutdown(manager)


@pytest.mark.asyncio
async def test_missing_device_tool_is_not_retried():
    """固件还没上 self.pomodoro.show 时，重试只会白卡 3 秒，不该发生。"""
    attempts = []

    async def missing_tool(conn, mcp_client, tool_name, args, timeout=30):
        attempts.append(tool_name)
        raise ValueError(f"工具 {tool_name} 不存在")

    manager = PomodoroManager(
        {"pomodoro": {"focus_minutes": 1.0}},
        FakeRegistry({DEVICE_ID: FakeConn()}),
        push_alert=AlertRecorder(),
        play_action=ActionRecorder(),
        call_tool=missing_tool,
        celebration_delay_s=0.0,
    )

    result = await manager.start(DEVICE_ID)

    assert result["outcome"] == "started"
    await wait_pushes_settled(manager)
    assert attempts == [POMODORO_SHOW_TOOL]

    await shutdown(manager)


@pytest.mark.asyncio
async def test_sessions_are_isolated_per_device():
    registry = FakeRegistry(
        {DEVICE_ID: FakeConn(), OTHER_DEVICE_ID: FakeConn(OTHER_DEVICE_ID)}
    )
    manager, _, _, tools = make_manager(focus=1.0, registry=registry)

    await manager.start(DEVICE_ID)
    await manager.start(OTHER_DEVICE_ID, focus_minutes=2)
    await manager.pause(DEVICE_ID)

    first = await manager.status(DEVICE_ID)
    second = await manager.status(OTHER_DEVICE_ID)

    assert first["status"]["paused"] is True
    assert second["status"]["paused"] is False
    assert second["status"]["total_s"] == 120
    assert sorted(manager.active_device_ids()) == sorted([DEVICE_ID, OTHER_DEVICE_ID])
    await wait_for_shows(tools, 1, OTHER_DEVICE_ID)
    assert tools.phases(OTHER_DEVICE_ID) == ["focus"]

    await shutdown(manager)


# ---------------------------------------------------------------------- 竞态


@pytest.mark.asyncio
async def test_stale_timer_does_not_fire_after_pause():
    """暂停必须让旧计时任务彻底失效，否则相位会在暂停期间自己往前走。"""
    manager, alerts, _, tools = make_manager()

    await manager.start(DEVICE_ID)
    await manager.pause(DEVICE_ID)
    await wait_for_shows(tools, 2)
    await wait_phase_end()

    assert alerts.calls == []
    assert tools.phases() == ["focus", "focus"]  # start 一次 + pause 回显一次

    await shutdown(manager)


@pytest.mark.asyncio
async def test_stale_timer_does_not_fire_after_stop():
    manager, alerts, _, tools = make_manager()

    await manager.start(DEVICE_ID)
    await manager.stop(DEVICE_ID)
    await wait_for_shows(tools, 2)
    await wait_phase_end()

    assert alerts.calls == []
    assert tools.phases() == ["focus", "idle"]


@pytest.mark.asyncio
async def test_retry_after_stop_does_not_repush_stale_show(monkeypatch):
    """重试窗口里 stop：陈旧画面不许盖掉 stop 已经推出去的 idle。

    首推失败（设备刚重连、MCP 还没握手完）会进重试窗口，用户在窗口里 stop，
    重试醒来若还拿着旧的 focus 参数往上推，设备就停在服务端已经不存在的番茄钟画面上，
    只能靠用户再 stop 一次收回。stop 自己已经推过 idle，弃推才是安全的。
    """
    # 真实的 3s 重试窗口语义不变，只是压短到测试跑得完
    monkeypatch.setattr(pomodoro_module, "SHOW_RETRY_DELAY", 0.05)

    class FlakyTool(ToolRecorder):
        """第一次 focus 下发必失败，之后恢复正常。"""

        def __init__(self):
            super().__init__()
            self.attempts = []
            self.failures_left = 1

        async def __call__(self, conn, mcp_client, tool_name, args, timeout=30):
            phase = json.loads(args)["phase"]
            self.attempts.append(phase)
            if phase == "focus" and self.failures_left > 0:
                self.failures_left -= 1
                raise RuntimeError("设备重连中，MCP 尚未就绪")
            return await super().__call__(conn, mcp_client, tool_name, args, timeout)

    tools = FlakyTool()
    manager = PomodoroManager(
        {"pomodoro": {"focus_minutes": 25}},
        FakeRegistry({DEVICE_ID: FakeConn()}),
        push_alert=AlertRecorder(),
        play_action=ActionRecorder(),
        call_tool=tools,
        celebration_delay_s=0.0,
    )

    await manager.start(DEVICE_ID)
    # 等首推真的失败了再 stop，否则可能抢在重试窗口之前
    await wait_until(lambda: tools.attempts == ["focus"], what="首次画面下发失败")
    await manager.stop(DEVICE_ID)

    await wait_pushes_settled(manager)
    # 设备最后看到的必须是 idle，重试不得再补一次 focus
    assert tools.phases() == ["idle"]
    assert tools.attempts == ["focus", "idle"]
    assert manager.active_device_ids() == []


@pytest.mark.asyncio
async def test_restart_after_stop_begins_from_first_round():
    manager, _, _, tools = make_manager(focus=1.0)

    await manager.start(DEVICE_ID)
    await manager.stop(DEVICE_ID)
    await manager.start(DEVICE_ID)

    await wait_for_shows(tools, 3)
    assert tools.shows()[-1]["phase"] == "focus"
    assert tools.shows()[-1]["round"] == 1

    await shutdown(manager)


# ------------------------------------------------------------------ 按键反馈


@pytest.mark.asyncio
async def test_button_path_adds_a_spoken_alert():
    manager, alerts, _, _ = make_manager(focus=1.0)

    await manager.start(DEVICE_ID, feedback=True)
    await manager.pause(DEVICE_ID, feedback=True)

    assert len(alerts.calls) == 2
    assert all(call["silent"] is False for call in alerts.calls)
    assert "暂停" in alerts.texts[1]

    await shutdown(manager)


@pytest.mark.asyncio
async def test_voice_and_http_paths_stay_silent():
    manager, alerts, _, _ = make_manager(focus=1.0)

    await manager.start(DEVICE_ID)
    await manager.pause(DEVICE_ID)

    assert alerts.calls == []

    await shutdown(manager)


# ------------------------------------------------------------------ 配置默认


@pytest.mark.asyncio
async def test_defaults_apply_when_config_section_is_absent():
    manager = PomodoroManager(
        {},
        FakeRegistry({DEVICE_ID: FakeConn()}),
        push_alert=AlertRecorder(),
        play_action=ActionRecorder(),
        call_tool=ToolRecorder(),
        celebration_delay_s=0.0,
    )

    result = await manager.start(DEVICE_ID)

    assert result["status"]["total_s"] == 25 * 60
    assert result["status"]["total_rounds"] == 4

    await shutdown(manager)


@pytest.mark.asyncio
async def test_start_accepts_focus_minutes_override():
    manager, _, _, _ = make_manager(focus=1.0)

    result = await manager.start(DEVICE_ID, focus_minutes=45)

    assert result["status"]["total_s"] == 45 * 60
    assert result["status"]["remaining_s"] == 45 * 60

    await shutdown(manager)
