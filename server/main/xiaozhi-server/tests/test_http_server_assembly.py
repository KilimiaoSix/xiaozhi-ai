"""SimpleHttpServer 装配层回归测试。

各模块自身的单测都绿,不代表拼起来的那几根线还在:对抗核验发现
presence 编排回调、观察者注册、日终总结数据源、告警台账胶水这些
装配代码此前没有任何测试守着——重构误删一行赋值不会有测试变红,
只能等真机验收才暴露。本文件专门锁装配,不测各模块内部逻辑。
"""

import asyncio

import pytest

from core.away_ledger import (
    SEVERITY_CRITICAL,
    SEVERITY_NORMAL,
    get_away_ledger,
    reset_away_ledger,
)
from core.camera_stream.observers import frame_observer_hub
from core.http_server import SimpleHttpServer
from core.incident_manager import get_incident_manager, reset_incident_manager
from core.owner_status import reset_owner_status_store
from core.visitor_flow import reset_visitor_flow
from plugins_func.functions.day_summary import (
    reset_summary_providers,
    summary_providers,
)


class FakeRegistry:
    def get(self, device_id):
        return None

    def device_ids(self):
        return []


class FakeWsServer:
    def __init__(self):
        self.device_registry = FakeRegistry()


@pytest.fixture
def assembled(tmp_path):
    """带全部演示功能开关的真实装配。单例逐个复位,避免用例间串台。"""
    reset_away_ledger()
    reset_owner_status_store()
    reset_visitor_flow()
    reset_incident_manager()
    reset_summary_providers()
    for name in list(frame_observer_hub.names()):
        frame_observer_hub.unregister(name)

    config = {
        "server": {"auth": {"enabled": False}, "auth_key": "", "http_port": 8003},
        "presence_robot": {
            "enabled": True,
            "workstations": {"desk-test": "aa:bb:cc:dd:ee:ff"},
        },
        "owner_status": {"persist_path": str(tmp_path / "owner.json")},
        "away_ledger": {"persist_path": str(tmp_path / "ledger.json")},
        "incident": {"storage_dir": str(tmp_path / "incidents")},
        "distraction": {
            "enabled": True,
            # 指向不存在的模型:观察者应降级注册而不是装配失败
            "model_path": str(tmp_path / "no-model.tflite"),
        },
        "timed_prompts": {
            "enabled": True,
            "prompts": [{"time": "10:30", "text": "喝水"}],
        },
        "approval": {"gesture": {"model_path": str(tmp_path / "no-gesture.task")}},
        "wellbeing": {"enabled": False},
        "morning_brief": {"enabled": False},
    }

    async def build():
        return SimpleHttpServer(dict(config), FakeWsServer())

    server = asyncio.new_event_loop().run_until_complete(build())
    yield server, config
    reset_away_ledger()
    reset_owner_status_store()
    reset_visitor_flow()
    reset_incident_manager()
    reset_summary_providers()
    for name in list(frame_observer_hub.names()):
        frame_observer_hub.unregister(name)


def test_presence_orchestrator_wired_to_both_ingest_paths(assembled):
    """迎接/休眠编排必须同时接 HTTP 上报与摄像头流两条链路。"""
    server, _ = assembled
    assert server.presence_orchestrator is not None
    assert (
        server.presence_handler._on_accepted
        == server.presence_orchestrator.on_report
    )
    assert (
        server.camera_stream_handler._on_accepted
        == server.presence_orchestrator.on_report
    )


def test_presence_orchestrator_shares_ledger_and_visitor_flow(assembled):
    """编排、来访应答、台账必须是同一批实例,否则不在一本账上。"""
    server, config = assembled
    assert server.presence_orchestrator._away_ledger is server.away_ledger
    assert server.presence_orchestrator._visitor_flow is server.visitor_flow
    assert server.away_ledger is get_away_ledger(config)


def test_camera_observers_registered(assembled):
    """手势审批与分心检测观察者都要挂进帧集线器(模型缺失也要降级注册)。"""
    server, _ = assembled
    names = frame_observer_hub.names()
    assert "gesture_approval" in names
    assert "distraction" in names


def test_day_summary_providers_registered(assembled):
    """三个数据源槽位不接上,「总结一下今天」永远只会说兜底话术。"""
    _, _ = assembled
    assert set(summary_providers) == {"agent_events", "incidents", "tomorrow_status"}
    # provider 真的能跑,而不只是挂了个名字
    assert summary_providers["agent_events"]() == []
    assert summary_providers["incidents"]() == []
    assert summary_providers["tomorrow_status"]()["state"] == "available"


@pytest.mark.asyncio
async def test_incident_callbacks_feed_away_ledger(assembled):
    """P2/P3 走 normal 桶、P0/P1 播报后走 critical 桶,都要真的落进台账。"""
    server, config = assembled
    manager = get_incident_manager(config)
    ledger = server.away_ledger
    ledger.mark_away()

    await manager.handle_webhook(
        {"service": "demo", "severity": "P3", "title": "慢查询", "simulated": True}
    )
    await manager.handle_webhook(
        {"service": "demo", "severity": "P0", "title": "接口挂了", "simulated": True}
    )

    pending = ledger.pending_summary()["items"]
    severities = {item["text"]: item["severity"] for item in pending}
    assert severities["demo 慢查询"] == SEVERITY_NORMAL
    assert severities["demo 接口挂了"] == SEVERITY_CRITICAL


def test_all_new_routes_mounted(assembled):
    """八流程的 HTTP 面必须全部挂载。"""
    server, _ = assembled
    app = server.create_app()
    paths = {r.resource.canonical for r in app.router.routes() if r.resource}
    for path in [
        "/xiaozhi/status",
        "/xiaozhi/away/summary",
        "/xiaozhi/approval/request",
        "/xiaozhi/approval/{approval_id}",
        "/xiaozhi/incident/webhook",
        "/xiaozhi/incident/latest",
        "/xiaozhi/event/push",
        "/xiaozhi/pomodoro/{device_id}",
        "/xiaozhi/presence/stream",
    ]:
        assert path in paths, f"路由未挂载: {path}"


def test_reminder_and_timed_prompts_constructed(assembled):
    """状态过期提醒与定时提示要随 HTTP 服务装配(未 start 也必须存在)。"""
    server, _ = assembled
    assert server.owner_status_reminder is not None
    assert server.timed_prompt_scheduler is not None
