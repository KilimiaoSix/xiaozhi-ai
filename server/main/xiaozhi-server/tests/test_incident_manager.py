"""线上告警状态机测试（需求文档流程七）。

全部离线：注入假时钟、假 sleep、假推送与假诊断执行器，不碰真机、不跑 claude、
不真的等 5 分钟观察窗。恢复观察用「闸门式」假 sleep 精确控制窗口开合，
避免依赖事件循环的调度顺序。
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

import config.settings
from config.config_loader import get_project_dir, read_config
from config.logger import setup_logging
from core.utils.cache.manager import CacheType, cache_manager

# 语音函数经 plugins_func.register 导入时会 setup_logging()，配置缓存是冷的就会
# 走 asyncio.run(load_config())，而用例跑在事件循环里。趁导入阶段先把缓存捂热
# （同 tests/test_pomodoro_handler.py 的理由）。
_repo_config = read_config(get_project_dir() + "config.yaml")
cache_manager.set(CacheType.CONFIG, "main_config", _repo_config)
config.settings.config_file_valid = True
setup_logging(_repo_config)

from core.incident_manager import (  # noqa: E402
    DIAGNOSIS_ACK,
    DIAGNOSIS_BUSY,
    IncidentManager,
    STATUS_FIRING,
    STATUS_OBSERVING,
    STATUS_RECOVERED,
    get_incident_manager,
    reset_incident_manager,
)
from plugins_func.functions.incident import (  # noqa: E402
    NO_ACTIVE_REPLY,
    NO_INCIDENT_REPLY,
    incident_diagnose,
    incident_status,
)


# 仓库没有 pytest 配置文件，pytest-asyncio 走 strict 模式，异步用例必须显式打标
pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 19, 10, 0, 0)
CONN = object()


class FakeClock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class GatedSleep:
    """假 sleep：记录被要求睡多久，直到测试放行才返回。

    观察窗要能「停在窗口中间」才测得了复燃取消恢复，立即返回的假 sleep
    做不到这一点（任务一被调度就跑完了）。
    """

    def __init__(self) -> None:
        self.calls = []
        self.gate = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await self.gate.wait()

    def release(self) -> None:
        self.gate.set()


class PushRecorder:
    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, conn, text, **kwargs):
        self.calls.append({"conn": conn, "text": text, **kwargs})
        return True

    @property
    def texts(self):
        return [call["text"] for call in self.calls]


class FakeRunner:
    """假诊断执行器：不起子进程，直接回调 manager 给的结果。"""

    def __init__(self, result=None, delay_event=None) -> None:
        self.result = result or {"ok": True, "summary": "最可能是上游超时", "error": ""}
        self.delay_event = delay_event
        self.incidents = []

    async def run(self, incident, on_result=None):
        self.incidents.append(incident)
        if self.delay_event is not None:
            await self.delay_event.wait()
        if on_result is not None:
            await on_result(incident.get("incident_id"), self.result)
        return self.result


def build_manager(tmp_path, *, on_low_severity=None, runner=None, config=None):
    clock = FakeClock()
    sleep = GatedSleep()
    push = PushRecorder()
    manager = IncidentManager(
        config or {"incident": {"dedup_cooldown_s": 120, "observe_seconds": 300}},
        push_event=push,
        device_resolver=lambda: CONN,
        on_low_severity=on_low_severity,
        diagnosis_runner=runner,
        clock=clock,
        sleep=sleep,
        storage_dir=tmp_path,
    )
    return manager, clock, sleep, push


def firing(**overrides):
    payload = {
        "service": "demo-api",
        "severity": "P1",
        "title": "接口错误率升高",
        "message": "支付回调错误率 12%",
        "metric": "error_rate",
        "value": "12%",
        "simulated": False,
    }
    payload.update(overrides)
    return payload


def resolved(**overrides):
    return firing(status="resolved", **overrides)


# ---------------------------------------------------------------- 播报与降噪


@pytest.mark.parametrize("severity", ["P0", "P1"])
async def test_high_severity_announces_immediately(tmp_path, severity):
    manager, _, _, push = build_manager(tmp_path)

    result = await manager.handle_webhook(firing(severity=severity))

    assert result["outcome"] == "announced"
    assert result["announced"] is True
    assert len(push.calls) == 1
    call = push.calls[0]
    assert call["text"] == "线上告警：接口错误率升高，支付回调错误率 12%"
    assert call["emotion"] == "shocked"
    assert call["status"] == f"线上告警 {severity}"
    assert call["speak"] is True
    assert call["action"] == "look_up"


async def test_simulated_alert_is_prefixed(tmp_path):
    manager, _, _, push = build_manager(tmp_path)

    await manager.handle_webhook(firing(simulated=True))

    assert push.texts == ["【模拟】线上告警：接口错误率升高，支付回调错误率 12%"]


async def test_alert_without_message_still_reads_naturally(tmp_path):
    manager, _, _, push = build_manager(tmp_path)

    await manager.handle_webhook(firing(message=""))

    assert push.texts == ["线上告警：接口错误率升高"]


@pytest.mark.parametrize("severity", ["P2", "P3"])
async def test_low_severity_goes_to_summary_without_speaking(tmp_path, severity):
    seen = []
    manager, _, _, push = build_manager(tmp_path, on_low_severity=seen.append)

    result = await manager.handle_webhook(firing(severity=severity))

    assert result["outcome"] == "low_severity"
    assert result["announced"] is False
    assert push.calls == []
    assert len(seen) == 1
    assert seen[0]["severity"] == severity
    assert seen[0]["title"] == "接口错误率升高"


async def test_low_severity_callback_failure_does_not_break_webhook(tmp_path):
    def boom(_incident):
        raise RuntimeError("台账挂了")

    manager, _, _, _ = build_manager(tmp_path, on_low_severity=boom)

    result = await manager.handle_webhook(firing(severity="P3"))

    assert result["ok"] is True


async def test_repeat_within_cooldown_merges_without_speaking(tmp_path):
    manager, clock, _, push = build_manager(tmp_path)
    await manager.handle_webhook(firing())

    clock.advance(30)
    second = await manager.handle_webhook(firing())
    clock.advance(30)
    third = await manager.handle_webhook(firing())

    assert second["outcome"] == "merged"
    assert third["outcome"] == "merged"
    assert len(push.calls) == 1
    assert third["incident"]["repeat_count"] == 3


async def test_repeat_after_cooldown_speaks_again(tmp_path):
    manager, clock, _, push = build_manager(tmp_path)
    await manager.handle_webhook(firing())

    clock.advance(121)
    result = await manager.handle_webhook(firing())

    assert result["outcome"] == "announced"
    assert len(push.calls) == 2


async def test_low_severity_repeat_within_cooldown_does_not_spam_summary(tmp_path):
    seen = []
    manager, clock, _, _ = build_manager(tmp_path, on_low_severity=seen.append)
    await manager.handle_webhook(firing(severity="P2"))

    clock.advance(10)
    result = await manager.handle_webhook(firing(severity="P2"))

    assert result["outcome"] == "merged"
    assert len(seen) == 1


async def test_severity_upgrade_breaks_through_cooldown_state(tmp_path):
    seen = []
    manager, clock, _, push = build_manager(tmp_path, on_low_severity=seen.append)
    await manager.handle_webhook(firing(severity="P2"))

    clock.advance(200)
    result = await manager.handle_webhook(firing(severity="P0"))

    assert result["outcome"] == "announced"
    assert push.calls[0]["status"] == "线上告警 P0"
    assert result["incident"]["severity"] == "P0"


async def test_different_titles_are_separate_incidents(tmp_path):
    manager, _, _, push = build_manager(tmp_path)

    first = await manager.handle_webhook(firing())
    second = await manager.handle_webhook(firing(title="队列积压"))

    assert first["incident_id"] != second["incident_id"]
    assert len(push.calls) == 2


# ---------------------------------------------------------------- 恢复观察


async def test_resolved_enters_observation_then_announces_recovery(tmp_path):
    manager, _, sleep, push = build_manager(tmp_path)
    await manager.handle_webhook(firing())

    result = await manager.handle_webhook(resolved())
    assert result["outcome"] == "observing"
    assert result["incident"]["state"] == STATUS_OBSERVING
    # 恢复播报要等观察窗走完，这时还不该出声
    await asyncio.sleep(0)
    assert len(push.calls) == 1

    sleep.release()
    await manager.wait_idle()

    assert sleep.calls == [300.0]
    assert push.texts[-1] == (
        "错误率已经恢复，连续5分钟没有新增异常。故障时间线我也记录好了。"
    )
    assert push.calls[-1]["emotion"] == "relaxed"
    assert manager.active_incident() is None


async def test_simulated_recovery_is_prefixed(tmp_path):
    manager, _, sleep, push = build_manager(tmp_path)
    await manager.handle_webhook(firing(simulated=True))
    await manager.handle_webhook(resolved(simulated=True))

    sleep.release()
    await manager.wait_idle()

    assert push.texts[-1].startswith("【模拟】错误率已经恢复，连续5分钟")


async def test_reignition_during_observation_cancels_recovery(tmp_path):
    manager, _, sleep, push = build_manager(tmp_path)
    await manager.handle_webhook(firing())
    await manager.handle_webhook(resolved())
    # 让观察任务真的跑到闸门前，再复燃，确保测的是「取消」而不是「还没开始」
    await asyncio.sleep(0)
    assert sleep.calls == [300.0]

    result = await manager.handle_webhook(firing())
    sleep.release()
    await manager.wait_idle()

    assert result["outcome"] == "announced"
    assert result["incident"]["state"] == STATUS_FIRING
    assert len(push.calls) == 2  # 首次告警 + 复燃告警，没有恢复播报
    assert all("已经恢复" not in text for text in push.texts)


async def test_second_resolved_does_not_restart_observation(tmp_path):
    manager, _, sleep, _ = build_manager(tmp_path)
    await manager.handle_webhook(firing())
    await manager.handle_webhook(resolved())
    await asyncio.sleep(0)

    result = await manager.handle_webhook(resolved())

    assert result["outcome"] == "already_observing"
    assert sleep.calls == [300.0]
    manager.shutdown()


async def test_resolved_for_unknown_incident_is_ignored(tmp_path):
    manager, _, _, push = build_manager(tmp_path)

    result = await manager.handle_webhook(resolved())

    assert result["outcome"] == "unknown"
    assert push.calls == []


async def test_never_announced_incident_recovers_silently(tmp_path):
    manager, _, sleep, push = build_manager(tmp_path, on_low_severity=lambda _i: None)
    await manager.handle_webhook(firing(severity="P3"))
    await manager.handle_webhook(resolved(severity="P3"))

    sleep.release()
    await manager.wait_idle()

    assert push.calls == []
    assert manager.list_today()[0]["state"] == STATUS_RECOVERED


async def test_observe_seconds_config_is_honoured(tmp_path):
    manager, _, sleep, push = build_manager(
        tmp_path, config={"incident": {"observe_seconds": 60}}
    )
    await manager.handle_webhook(firing())
    await manager.handle_webhook(resolved())

    sleep.release()
    await manager.wait_idle()

    assert sleep.calls == [60.0]
    assert "连续1分钟" in push.texts[-1]


async def test_short_observation_window_is_announced_in_seconds(tmp_path):
    """演示常把窗口压到几十秒，这时不能还播「连续1分钟」——那是假话。"""
    manager, _, sleep, push = build_manager(
        tmp_path, config={"incident": {"observe_seconds": 30}}
    )
    await manager.handle_webhook(firing())
    await manager.handle_webhook(resolved())

    sleep.release()
    await manager.wait_idle()

    assert "连续30秒没有新增异常" in push.texts[-1]


# ---------------------------------------------------------------- 时间线


async def test_timeline_file_records_full_sequence(tmp_path):
    manager, _, sleep, _ = build_manager(tmp_path, runner=FakeRunner())
    result = await manager.handle_webhook(firing(simulated=True))
    incident_id = result["incident_id"]
    await manager.start_diagnosis()
    await manager.wait_idle()
    await manager.handle_webhook(resolved())
    sleep.release()
    await manager.wait_idle()

    path = tmp_path / f"2026-08-19-{incident_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))

    assert [event["event"] for event in data["timeline"]] == [
        "received",
        "announced",
        "diagnosis_started",
        "diagnosis_result",
        "resolved_reported",
        "recovered",
    ]
    assert data["simulated"] is True
    assert data["state"] == STATUS_RECOVERED
    assert data["diagnosis"]["summary"] == "最可能是上游超时"
    assert all(event["at"].startswith("2026-08-19T10:") for event in data["timeline"])


async def test_incident_id_from_payload_is_sanitised_for_filename(tmp_path):
    manager, _, _, _ = build_manager(tmp_path)

    result = await manager.handle_webhook(firing(incident_id="../../etc/passwd"))

    assert "/" not in result["incident_id"]
    assert (tmp_path / f"2026-08-19-{result['incident_id']}.json").exists()
    assert list(tmp_path.glob("*.json"))


async def test_recovered_records_are_pruned_the_next_day(tmp_path):
    manager, clock, sleep, _ = build_manager(tmp_path)
    await manager.handle_webhook(firing())
    await manager.handle_webhook(resolved())
    sleep.release()
    await manager.wait_idle()

    clock.advance(86400 + 3600)  # 第二天
    await manager.handle_webhook(firing(title="新一天的故障"))

    assert [item["title"] for item in manager.list_today()] == ["新一天的故障"]


async def test_list_today_skips_corrupt_files(tmp_path):
    manager, _, _, _ = build_manager(tmp_path)
    await manager.handle_webhook(firing())
    (tmp_path / "2026-08-19-broken.json").write_text("{ not json", encoding="utf-8")

    today = manager.list_today()

    assert [item["title"] for item in today] == ["接口错误率升高"]


async def test_list_today_merges_files_from_previous_process(tmp_path):
    manager, _, _, _ = build_manager(tmp_path)
    (tmp_path / "2026-08-19-old-one.json").write_text(
        json.dumps(
            {"incident_id": "old-one", "title": "上一进程的故障", "first_seen_at": "2026-08-19T09:00:00"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    await manager.handle_webhook(firing())

    titles = [item["title"] for item in manager.list_today()]

    assert titles == ["上一进程的故障", "接口错误率升高"]


# ---------------------------------------------------------------- 校验


@pytest.mark.parametrize(
    "payload",
    [
        {"severity": "P1", "title": "x"},  # 缺 service
        {"service": "a", "severity": "P1"},  # 缺 title
        {"service": "a", "severity": "P9", "title": "x"},  # 严重度非法
        {"service": "a", "severity": "P1", "title": "x", "status": "flapping"},
        "not a dict",
    ],
)
async def test_invalid_payload_raises_value_error(tmp_path, payload):
    manager, _, _, _ = build_manager(tmp_path)

    with pytest.raises(ValueError):
        await manager.handle_webhook(payload)


async def test_unparseable_started_at_falls_back_to_now(tmp_path):
    manager, _, _, _ = build_manager(tmp_path)

    result = await manager.handle_webhook(firing(started_at="昨天下午"))

    assert result["incident"]["started_at"] == "2026-08-19T10:00:00"


# ---------------------------------------------------------------- 活跃故障


async def test_active_incident_prefers_highest_severity(tmp_path):
    manager, clock, _, _ = build_manager(tmp_path, on_low_severity=lambda _i: None)
    await manager.handle_webhook(firing(severity="P2", title="磁盘偏高"))
    clock.advance(5)
    await manager.handle_webhook(firing(severity="P0", title="支付全挂"))
    clock.advance(5)
    await manager.handle_webhook(firing(severity="P1", title="队列积压"))

    active = manager.active_incident()

    assert active["title"] == "支付全挂"


async def test_active_incident_prefers_latest_within_same_severity(tmp_path):
    manager, clock, _, _ = build_manager(tmp_path)
    await manager.handle_webhook(firing(title="旧的"))
    clock.advance(600)
    await manager.handle_webhook(firing(title="新的"))

    assert manager.active_incident()["title"] == "新的"


# ---------------------------------------------------------------- 诊断编排


async def test_start_diagnosis_returns_ack_and_pushes_result(tmp_path):
    runner = FakeRunner()
    manager, _, _, push = build_manager(tmp_path, runner=runner)
    await manager.handle_webhook(firing())

    ack = await manager.start_diagnosis()
    await manager.wait_idle()

    assert ack == DIAGNOSIS_ACK
    assert runner.incidents[0]["title"] == "接口错误率升高"
    assert push.texts[-1] == "诊断结果：最可能是上游超时"
    assert push.calls[-1]["emotion"] == "thinking"
    assert push.calls[-1]["speak"] is True


async def test_failed_diagnosis_is_reported_not_swallowed(tmp_path):
    runner = FakeRunner({"ok": False, "summary": "", "error": "诊断超时，已停在 300 秒"})
    manager, _, _, push = build_manager(tmp_path, runner=runner)
    await manager.handle_webhook(firing())

    await manager.start_diagnosis()
    await manager.wait_idle()

    assert push.texts[-1] == "诊断没有跑完：诊断超时，已停在 300 秒"
    assert push.calls[-1]["emotion"] == "confused"


async def test_runner_exception_is_reported(tmp_path):
    class ExplodingRunner:
        async def run(self, incident, on_result=None):
            raise RuntimeError("claude 不在 PATH")

    manager, _, _, push = build_manager(tmp_path, runner=ExplodingRunner())
    await manager.handle_webhook(firing())

    await manager.start_diagnosis()
    await manager.wait_idle()

    assert push.texts[-1] == "诊断没有跑完：claude 不在 PATH"


async def test_runner_without_callback_still_reports(tmp_path):
    class SilentRunner:
        async def run(self, incident, on_result=None):
            return {"ok": True, "summary": "磁盘满了", "error": ""}

    manager, _, _, push = build_manager(tmp_path, runner=SilentRunner())
    await manager.handle_webhook(firing())

    await manager.start_diagnosis()
    await manager.wait_idle()

    assert push.texts[-1] == "诊断结果：磁盘满了"


async def test_second_diagnosis_while_running_is_rejected(tmp_path):
    gate = asyncio.Event()
    runner = FakeRunner(delay_event=gate)
    manager, _, _, _ = build_manager(tmp_path, runner=runner)
    await manager.handle_webhook(firing())

    first = await manager.start_diagnosis()
    await asyncio.sleep(0)
    second = await manager.start_diagnosis()

    assert first == DIAGNOSIS_ACK
    assert second == DIAGNOSIS_BUSY

    gate.set()
    await manager.wait_idle()
    assert len(runner.incidents) == 1


async def test_start_diagnosis_without_incident_returns_none(tmp_path):
    manager, _, _, _ = build_manager(tmp_path, runner=FakeRunner())

    assert await manager.start_diagnosis() is None


# ---------------------------------------------------------------- 播报降级


async def test_offline_device_does_not_break_state_machine(tmp_path):
    clock, sleep, push = FakeClock(), GatedSleep(), PushRecorder()
    manager = IncidentManager(
        {},
        push_event=push,
        device_resolver=lambda: None,  # 设备不在线
        clock=clock,
        sleep=sleep,
        storage_dir=tmp_path,
    )

    result = await manager.handle_webhook(firing())

    assert result["announced"] is False
    assert result["incident"]["state"] == STATUS_FIRING
    assert push.calls == []


async def test_push_failure_is_downgraded_to_logging(tmp_path):
    async def exploding_push(conn, text, **kwargs):
        raise RuntimeError("websocket 已断开")

    manager = IncidentManager(
        {},
        push_event=exploding_push,
        device_resolver=lambda: CONN,
        clock=FakeClock(),
        sleep=GatedSleep(),
        storage_dir=tmp_path,
    )

    result = await manager.handle_webhook(firing())

    assert result["ok"] is True
    assert result["announced"] is False


# ---------------------------------------------------------------- 设备解析


async def test_device_resolver_defaults_to_first_workstation(tmp_path):
    conn = object()

    class FakeRegistry:
        def __init__(self):
            self.asked = []

        def get(self, device_id):
            self.asked.append(device_id)
            return conn if device_id == "dc:da:0c:26:9a:60" else None

        def device_ids(self):
            return ["dc:da:0c:26:9a:60"]

    registry = FakeRegistry()
    push = PushRecorder()
    manager = IncidentManager(
        {"presence_robot": {"workstations": {"desk-1": "dc:da:0c:26:9a:60"}}},
        registry,
        push_event=push,
        clock=FakeClock(),
        sleep=GatedSleep(),
        storage_dir=tmp_path,
    )

    await manager.handle_webhook(firing())

    assert registry.asked == ["dc:da:0c:26:9a:60"]
    assert push.calls[0]["conn"] is conn


async def test_incident_device_id_overrides_workstations(tmp_path):
    seen = []

    class FakeRegistry:
        def get(self, device_id):
            seen.append(device_id)
            return object()

        def device_ids(self):
            return []

    manager = IncidentManager(
        {
            "incident": {"device_id": "aa:bb:cc:dd:ee:ff"},
            "presence_robot": {"workstations": {"desk-1": "dc:da:0c:26:9a:60"}},
        },
        FakeRegistry(),
        push_event=PushRecorder(),
        clock=FakeClock(),
        sleep=GatedSleep(),
        storage_dir=tmp_path,
    )

    await manager.handle_webhook(firing())

    assert seen == ["aa:bb:cc:dd:ee:ff"]


# ---------------------------------------------------------------- 单例


async def test_singleton_is_shared_and_resettable():
    reset_incident_manager()
    try:
        first = get_incident_manager({"incident": {}})
        second = get_incident_manager()
        assert first is second
    finally:
        reset_incident_manager()

    assert get_incident_manager({"incident": {}}) is not first
    reset_incident_manager()


# ---------------------------------------------------------------- 语音函数


class FakeConn:
    def __init__(self, config):
        self.config = config
        self.server = None
        self.logger = None


async def test_voice_diagnose_starts_diagnosis(tmp_path):
    reset_incident_manager()
    try:
        manager = get_incident_manager(
            {"incident": {}},
            push_event=PushRecorder(),
            device_resolver=lambda: CONN,
            diagnosis_runner=FakeRunner(),
            clock=FakeClock(),
            sleep=GatedSleep(),
            storage_dir=tmp_path,
        )
        await manager.handle_webhook(firing())

        response = await incident_diagnose(FakeConn({"incident": {}}))
        await manager.wait_idle()

        assert response.response == DIAGNOSIS_ACK
        assert response.result == "started"
    finally:
        reset_incident_manager()


async def test_voice_diagnose_without_incident(tmp_path):
    reset_incident_manager()
    try:
        get_incident_manager(
            {"incident": {}},
            device_resolver=lambda: CONN,
            clock=FakeClock(),
            sleep=GatedSleep(),
            storage_dir=tmp_path,
        )

        response = await incident_diagnose(FakeConn({"incident": {}}))

        assert response.response == NO_INCIDENT_REPLY
        assert response.result == "no_incident"
    finally:
        reset_incident_manager()


async def test_voice_status_describes_firing_and_observing(tmp_path):
    reset_incident_manager()
    try:
        manager = get_incident_manager(
            {"incident": {}},
            push_event=PushRecorder(),
            device_resolver=lambda: CONN,
            clock=FakeClock(),
            sleep=GatedSleep(),
            storage_dir=tmp_path,
        )
        conn = FakeConn({"incident": {}})

        idle = await incident_status(conn)
        await manager.handle_webhook(firing(simulated=True))
        await manager.handle_webhook(firing(simulated=True))
        firing_reply = await incident_status(conn)
        await manager.handle_webhook(resolved(simulated=True))
        observing_reply = await incident_status(conn)

        assert idle.response == NO_ACTIVE_REPLY
        assert "【模拟】" in firing_reply.response
        assert "级别P1" in firing_reply.response
        assert "上报了2次" in firing_reply.response
        assert "还在观察" in observing_reply.response
    finally:
        reset_incident_manager()
