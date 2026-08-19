import pytest

from core.alert_relay.models import AlertEvent, Diagnosis
from core.alert_relay.robot import RobotNotifier


class FakeRegistry:
    def __init__(self, conn=None):
        self.conn = conn
        self.asked = []

    def get(self, device_id):
        self.asked.append(device_id)
        return self.conn

    def device_ids(self):
        return ["dc:da:0c:26:9a:60"] if self.conn else []


class FakeConn:
    device_id = "dc:da:0c:26:9a:60"


def make_event(level="严重"):
    return AlertEvent(
        raw_text="x",
        level=level,
        cluster="bj-jxq-autocar",
        namespace="iflyplot",
        target="iflyplot-ai-7d9f8b6c5d-x2k9p",
        workload="iflyplot-ai",
        keyword="无痕改字处理超时",
    )


def make_notifier(*, conn=FakeConn(), **options):
    pushes = []

    async def fake_push(conn_arg, **kwargs):
        pushes.append((conn_arg, kwargs))
        return bool(kwargs.get("speak"))

    notifier = RobotNotifier(
        FakeRegistry(conn),
        "dc:da:0c:26:9a:60",
        push=fake_push,
        **options,
    )
    return notifier, pushes


@pytest.mark.asyncio
async def test_alert_makes_the_robot_look_up_and_speak():
    notifier, pushes = make_notifier()
    assert await notifier.alert(make_event("严重")) is True
    _, kwargs = pushes[0]
    assert kwargs["emotion"] == "shocked"
    assert kwargs["action"] == "look_up"
    assert kwargs["speak"] is True
    assert "iflyplot-ai" in kwargs["text"]
    assert kwargs["status"] == "线上告警"


@pytest.mark.asyncio
async def test_warning_level_does_not_speak_out_loud():
    """警告级别别打断人：抬头看一眼就够了，喇叭留给严重和紧急。"""
    notifier, pushes = make_notifier()
    await notifier.alert(make_event("警告"))
    _, kwargs = pushes[0]
    assert kwargs["emotion"] == "confused"
    assert kwargs["speak"] is False


@pytest.mark.asyncio
async def test_alert_screen_is_not_auto_restored():
    """没人处理的告警必须一直挂在屏幕上，自动收回等于把告警吞了。"""
    notifier, pushes = make_notifier()
    await notifier.alert(make_event())
    assert pushes[0][1].get("restore_after") is None


@pytest.mark.asyncio
async def test_claimed_nods_and_confirms_out_loud():
    notifier, pushes = make_notifier()
    assert await notifier.claimed(make_event(), by="张三") is True
    _, kwargs = pushes[0]
    assert kwargs["emotion"] == "happy"
    assert kwargs["action"] == "nod"
    assert kwargs["speak"] is True
    assert "查" in kwargs["text"]


@pytest.mark.asyncio
async def test_diagnosing_switches_to_thinking_without_extra_noise():
    notifier, pushes = make_notifier()
    await notifier.diagnosing(make_event())
    _, kwargs = pushes[0]
    assert kwargs["emotion"] == "thinking"
    assert kwargs["status"] == "排查中"
    assert kwargs["speak"] is False
    assert kwargs.get("action") is None


@pytest.mark.asyncio
async def test_diagnosed_speaks_one_short_line_not_the_whole_report():
    """屏幕一行约 8 字、最多 4 行，整份诊断塞不进去，只播一句根因。"""
    notifier, pushes = make_notifier()
    diagnosis = Diagnosis.from_payload(
        {"title": "限流组打满", "root_cause": "限流组并发配置过低，任务排队到超时。还有第二句。"}
    )
    await notifier.diagnosed(diagnosis)
    _, kwargs = pushes[0]
    assert kwargs["emotion"] == "confident"
    assert kwargs["action"] == "nod"
    assert kwargs["speak"] is True
    assert "还有第二句" not in kwargs["text"]
    assert len(kwargs["text"]) <= 48


@pytest.mark.asyncio
async def test_failed_shakes_its_head():
    notifier, pushes = make_notifier()
    await notifier.failed("Claude Code 超时")
    _, kwargs = pushes[0]
    assert kwargs["emotion"] == "sad"
    assert kwargs["action"] == "shake"
    assert kwargs["status"] == "排查失败"


@pytest.mark.asyncio
async def test_declined_returns_to_standby_quietly():
    notifier, pushes = make_notifier()
    await notifier.declined()
    _, kwargs = pushes[0]
    assert kwargs["speak"] is False
    assert kwargs["restore_after"]


@pytest.mark.asyncio
async def test_offline_device_reports_a_reason_and_never_raises():
    """机器人离线不能让整条告警链路崩掉——飞书那一路还得照发。"""
    notifier, pushes = make_notifier(conn=None)
    assert await notifier.alert(make_event()) is False
    assert "不在线" in notifier.last_error
    assert pushes == []


@pytest.mark.asyncio
async def test_push_failure_is_swallowed_and_recorded():
    async def exploding_push(conn, **kwargs):
        raise RuntimeError("websocket 已关闭")

    notifier = RobotNotifier(FakeRegistry(FakeConn()), "dev", push=exploding_push)
    assert await notifier.alert(make_event()) is False
    assert "websocket 已关闭" in notifier.last_error


@pytest.mark.asyncio
async def test_disabled_notifier_short_circuits():
    notifier, pushes = make_notifier(enabled=False)
    assert await notifier.alert(make_event()) is False
    assert pushes == []
    assert "未启用" in notifier.last_error


@pytest.mark.asyncio
async def test_missing_device_id_is_reported_as_configuration_error():
    notifier = RobotNotifier(FakeRegistry(FakeConn()), "", push=None)
    assert await notifier.alert(make_event()) is False
    assert "device_id" in notifier.last_error


@pytest.mark.asyncio
async def test_missing_registry_does_not_crash_the_relay():
    """server 以纯 HTTP 方式起时没有 device_registry，中继仍要能跑。"""
    notifier = RobotNotifier(None, "dev", push=None)
    assert await notifier.alert(make_event()) is False
    assert notifier.last_error
