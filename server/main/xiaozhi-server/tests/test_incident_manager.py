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
    _safe_id,
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


class GatedPush(PushRecorder):
    """假推送：第一条命中 match 的播报卡在闸门上，之后的照常立即返回。

    真机上一次播报是秒级的（ensure_speakable 最长等 3 秒 + TTS 合成播放），
    这段「播报在途」的真空期正是竞态窗口；立即返回的假推送测不到它。
    只卡第一条是为了让窗口期内进来的第二条能跑完，用例才有断言可做。
    """

    def __init__(self, match: str) -> None:
        super().__init__()
        self.match = match
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def __call__(self, conn, text, **kwargs):
        self.calls.append({"conn": conn, "text": text, **kwargs})
        if self.match in text and not self.entered.is_set():
            self.entered.set()
            await self.gate.wait()
        return True

    async def wait_in_flight(self) -> None:
        await asyncio.wait_for(self.entered.wait(), 1.0)

    def release(self) -> None:
        self.gate.set()


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


def build_manager(tmp_path, *, on_low_severity=None, runner=None, config=None, push=None):
    clock = FakeClock()
    sleep = GatedSleep()
    push = push or PushRecorder()
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
    """P2 在冷却窗**之内**升级 P0 必须立即播报。

    冷却门只比时间的话，「低级别先到、随后升级」这个真实告警里最常见的
    形态会被当成普通重复静默吞掉——advance 超过冷却期的写法测不到这条。
    """
    seen = []
    manager, clock, _, push = build_manager(tmp_path, on_low_severity=seen.append)
    await manager.handle_webhook(firing(severity="P2"))

    clock.advance(10)  # 远小于 dedup_cooldown_s=120，仍在冷却窗内
    result = await manager.handle_webhook(firing(severity="P0"))

    assert result["outcome"] == "announced"
    assert push.calls[0]["status"] == "线上告警 P0"
    assert result["incident"]["severity"] == "P0"


async def test_announced_incident_repeat_upgrade_still_merges_in_cooldown(tmp_path):
    """已播报过的 P1 在冷却窗内又升 P0：只该合并，不该二次出声。

    升级突破冷却的特例只给「从未播报过」的故障；已经打断过一次的故障
    在冷却窗内反复出声就是需求里明说要防的连续播报。
    """
    manager, clock, _, push = build_manager(tmp_path)
    await manager.handle_webhook(firing(severity="P1"))
    assert len(push.calls) == 1

    clock.advance(10)
    result = await manager.handle_webhook(firing(severity="P0"))

    assert result["outcome"] == "merged"
    assert len(push.calls) == 1
    assert result["incident"]["severity"] == "P0"


async def test_repeat_while_first_announcement_in_flight_is_merged(tmp_path):
    """首播还卡在设备侧时进来的重复上报，必须走冷却合并，不能再播一遍。

    冷却窗的破窗条件看的是「这条故障还从未播报过」；announced 若等播报
    的 await 返回才置位，首播在途的那几秒里每一条重复都会被当成
    「刚升级、还没播过」放行——告警风暴/webhook 重试有几条就播几条。
    """
    push = GatedPush("线上告警")
    manager, clock, _, _ = build_manager(tmp_path, push=push)

    first = asyncio.create_task(manager.handle_webhook(firing(severity="P0")))
    await push.wait_in_flight()

    clock.advance(30)  # 远在 dedup_cooldown_s=120 之内
    second = await manager.handle_webhook(firing(severity="P0"))

    push.release()
    await first

    assert second["outcome"] == "merged"
    assert len(push.calls) == 1
    assert second["incident"]["repeat_count"] == 2


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


async def test_reignition_during_recovery_push_does_not_finalize(tmp_path):
    """恢复播报还在途中就复燃：这条记录不能以 recovered 收尾。

    定稿先把 state 置 recovered 再 await 播报，这个 await 在真机上是秒级的；
    期间进来的 firing 把状态改回 firing 并重新告警，播报返回后定稿若还是
    无脑往下写，就会给一条正在燃烧的故障追加 recovered 事件——桌面端和
    日终总结读到的是一条自相矛盾的记录（state=firing，时间线以 recovered 结尾）。
    """
    push = GatedPush("已经恢复")
    manager, _, sleep, _ = build_manager(tmp_path, push=push)
    result = await manager.handle_webhook(firing(severity="P0"))
    incident_id = result["incident_id"]
    await manager.handle_webhook(resolved(severity="P0"))
    await asyncio.sleep(0)

    sleep.release()  # 观察窗走完，定稿卡在恢复播报上
    await push.wait_in_flight()

    reignite = await manager.handle_webhook(firing(severity="P0"))
    push.release()
    await manager.wait_idle()

    assert reignite["outcome"] == "announced"
    data = json.loads(
        (tmp_path / f"2026-08-19-{incident_id}.json").read_text(encoding="utf-8")
    )
    assert data["state"] == STATUS_FIRING
    assert data["recovered"] is False
    assert data["recovered_at"] is None
    assert "recovered" not in [event["event"] for event in data["timeline"]]


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


async def test_long_service_incident_id_survives_the_id_filter(tmp_path):
    """服务名很长时自动生成的 id 不能超长。

    写入侧用的是完整 id（内存键与文件名），ack / 诊断侧一律先过 _safe_id
    再查表；id 一旦被截断两侧就对不上，这些故障的「标记已处理」「启动诊断」
    恒 404，桌面端点了没反应还查不出原因。
    """
    long_service = "iflyplot-ai-inference-gateway-canary-shanghai-prod-cluster-a"
    manager, _, _, _ = build_manager(tmp_path, runner=FakeRunner())

    result = await manager.handle_webhook(firing(service=long_service))
    incident_id = result["incident_id"]

    assert _safe_id(incident_id) == incident_id
    assert manager.ack(incident_id)["outcome"] == "acked"
    assert (await manager.request_diagnosis(incident_id))["outcome"] == "accepted"
    await manager.wait_idle()


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


# ---------------------------------------------------------------- 跨进程重启


def _timeline(tmp_path, incident_id):
    path = tmp_path / f"2026-08-19-{incident_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


async def test_refiring_after_restart_keeps_the_previous_timeline(tmp_path):
    """重启后同一故障当天再次告警，不能把重启前的时间线整份覆盖掉。

    文件名是 {当天}-{incident_id}.json，新记录的 first_seen_at 又是当下，
    命中的正是那份旧文件——_persist 整份 snapshot 写下去，announced 与
    resolved_reported 就此消失。时间线是审计记录，不该被一次重启抹掉。
    """
    manager, _, _, _ = build_manager(tmp_path)
    result = await manager.handle_webhook(firing())
    incident_id = result["incident_id"]
    await manager.handle_webhook(resolved())
    manager.shutdown()  # 进程退出

    revived, _, _, _ = build_manager(tmp_path)  # 新进程，同一个 storage_dir
    again = await revived.handle_webhook(firing())

    data = _timeline(tmp_path, incident_id)
    events = [event["event"] for event in data["timeline"]]
    assert events[:3] == ["received", "announced", "resolved_reported"]
    assert "reignited" in events
    assert data["state"] == STATUS_FIRING
    assert again["incident"]["repeat_count"] == 2


async def test_restore_finishes_the_observation_window_of_a_dead_process(tmp_path):
    """重启前正在观察窗里的故障，装载后要接着把窗口走完并定稿。

    定稿只有内存里的观察任务能触发，不装回来的话盘上永远停在 observing，
    桌面端列表会一直挂着一条「恢复观察中」。
    """
    manager, _, _, _ = build_manager(tmp_path)
    result = await manager.handle_webhook(firing())
    incident_id = result["incident_id"]
    await manager.handle_webhook(resolved())
    manager.shutdown()

    revived, clock, sleep, push = build_manager(tmp_path)
    clock.advance(60)  # 停机 60 秒，300 秒的观察窗还剩 240 秒
    await revived.restore()
    await asyncio.sleep(0)  # 让重排的观察任务真的跑到闸门前

    assert sleep.calls == [240.0]
    sleep.release()
    await revived.wait_idle()

    # 播的是整个观察窗，不是剩下的那 240 秒——「连续4分钟」是句假话
    assert push.texts[-1] == (
        "错误率已经恢复，连续5分钟没有新增异常。故障时间线我也记录好了。"
    )
    data = _timeline(tmp_path, incident_id)
    assert data["state"] == STATUS_RECOVERED
    assert [event["event"] for event in data["timeline"]][-1] == "recovered"


async def test_restore_finalizes_an_expired_window_without_speaking(tmp_path):
    """停机期间观察窗就走完了：时间线照样定稿，但不播过期的恢复。"""
    manager, _, _, _ = build_manager(tmp_path)
    result = await manager.handle_webhook(firing())
    incident_id = result["incident_id"]
    await manager.handle_webhook(resolved())
    manager.shutdown()

    revived, clock, sleep, push = build_manager(tmp_path)
    clock.advance(3600)  # 停机一小时
    await revived.restore()
    sleep.release()
    await revived.wait_idle()

    assert push.calls == []
    assert _timeline(tmp_path, incident_id)["state"] == STATUS_RECOVERED


async def test_restore_keeps_firing_incidents_and_skips_recovered_ones(tmp_path):
    manager, _, sleep, _ = build_manager(tmp_path)
    still_firing = await manager.handle_webhook(firing(title="还在烧"))
    await manager.handle_webhook(firing(title="已经好了"))
    await manager.handle_webhook(resolved(title="已经好了"))
    sleep.release()
    await manager.wait_idle()
    manager.shutdown()

    revived, _, revived_sleep, _ = build_manager(tmp_path)
    restored = await revived.restore()

    assert restored["restored"] == [still_firing["incident_id"]]
    assert revived_sleep.calls == []  # 已定稿的不重排观察窗
    assert revived.active_incident()["title"] == "还在烧"


async def test_resolved_after_restart_finalizes_the_disk_record(tmp_path):
    """重启后监控补发的 resolved 要能接上盘上那条，而不是当成未知故障丢掉。"""
    manager, _, _, _ = build_manager(tmp_path)
    result = await manager.handle_webhook(firing())
    incident_id = result["incident_id"]
    manager.shutdown()

    revived, _, sleep, _ = build_manager(tmp_path)
    outcome = await revived.handle_webhook(resolved())
    sleep.release()
    await revived.wait_idle()

    assert outcome["outcome"] == "observing"
    data = _timeline(tmp_path, incident_id)
    assert [event["event"] for event in data["timeline"]] == [
        "received",
        "announced",
        "resolved_reported",
        "recovered",
    ]
    assert data["state"] == STATUS_RECOVERED


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
