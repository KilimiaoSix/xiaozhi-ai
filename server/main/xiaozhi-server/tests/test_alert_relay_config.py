import pytest

from core.alert_relay.factory import create_alert_relay_service


BASE = {
    "alert_relay": {
        "enabled": True,
        "receive_id": "ou_from_config",
        "reply_timeout_seconds": 900,
        "dedupe_window_seconds": 120,
        "auto_diagnose_on_timeout": True,
        "cluster_map": {"hf-lab": [902, 7]},
        "robot": {"enabled": True, "device_id": "dc:da:0c:26:9a:60"},
        "feishu": {
            "base_url": "https://open.xfchat.iflytek.com",
            "app_id": "cli_from_config",
            "app_secret": "secret_from_config",
        },
        "diagnosis": {
            "cli_command": ["claude"],
            "model": "opus",
            "source_dirs": ["/repos/iflyplot-server"],
            "timeout_seconds": 600,
        },
    }
}


def test_builds_a_service_from_plain_config():
    service = create_alert_relay_service(BASE, device_registry=object())
    health = service.health()
    assert health["enabled"] is True
    assert health["receive_id_configured"] is True
    assert health["reply_timeout_seconds"] == 900
    assert health["auto_diagnose_on_timeout"] is True
    assert health["feishu"]["app_id_configured"] is True
    assert health["diagnosis"]["model"] == "opus"
    assert health["diagnosis"]["source_dirs"] == ["/repos/iflyplot-server"]


def test_environment_variables_win_over_config_for_secrets(monkeypatch):
    """密钥不该进 config.yaml——环境变量必须能盖过去。"""
    monkeypatch.setenv("FEISHU_BOT_APP_ID", "cli_from_env")
    monkeypatch.setenv("FEISHU_BOT_APP_SECRET", "secret_from_env")
    monkeypatch.setenv("FEISHU_ALERT_RECEIVE_ID", "ou_from_env")

    service = create_alert_relay_service(BASE)
    assert service._bot.app_id == "cli_from_env"
    assert service._bot.app_secret == "secret_from_env"
    assert service._receive_id == "ou_from_env"


def test_missing_config_yields_a_disabled_but_usable_service():
    """没配也要能起：health 说清楚缺什么，而不是让进程起不来。"""
    service = create_alert_relay_service({})
    health = service.health()
    assert health["enabled"] is False
    assert health["receive_id_configured"] is False
    assert health["feishu"]["app_id_configured"] is False


@pytest.mark.asyncio
async def test_disabled_service_does_not_touch_anything():
    service = create_alert_relay_service({})
    result = await service.ingest({"raw_text": "告警集群：x"})
    assert result["code"] == "ALERT_RELAY_DISABLED"


def test_cluster_map_from_config_is_normalized_to_strings():
    """YAML 里写成数字的 projectId 必须转成字符串，否则拼 URL 时会炸。"""
    service = create_alert_relay_service(BASE)
    assert service._cluster_map["hf-lab"] == ("902", "7")


def test_robot_is_disabled_when_no_registry_is_available():
    """纯 HTTP 模式没有 device_registry，机器人这一路要老实说自己不可用。"""
    service = create_alert_relay_service(BASE)
    assert service._robot.available() is False
    assert create_alert_relay_service(BASE, device_registry=object())._robot.available() is True


def test_defaults_are_conservative():
    service = create_alert_relay_service({"alert_relay": {"enabled": True}})
    health = service.health()
    # 默认不自动开跑诊断：告警风暴下会同时拉起 N 个 Claude Code
    assert health["auto_diagnose_on_timeout"] is False
    assert health["diagnosis"]["cli_command"] == ["claude"]
    assert health["diagnosis"]["skill"] == "diagnose-sae-alert"


@pytest.mark.asyncio
async def test_http_server_wiring_signatures_stay_compatible():
    """把 http_server.py 里那三行接线原样跑一遍。

    那个模块本身要 requests / opuslib 等重依赖，测试环境装不全，所以这里不导入它，
    只钉住它调用的三个签名——改坏了这条用例会红，而不是等到进程起不来才发现。
    """
    from aiohttp import web

    from core.alert_relay_routes import add_alert_relay_routes
    from core.api.alert_relay_handler import AlertRelayHandler

    config = dict(BASE)
    config["server"] = {"auth": {"enabled": False}, "auth_key": ""}

    service = create_alert_relay_service(config, device_registry=None, logger=None)
    handler = AlertRelayHandler(config, service, logger=None)
    app = web.Application()
    add_alert_relay_routes(app, handler)

    paths = {str(route.resource.canonical) for route in app.router.routes()}
    assert "/xiaozhi/alert/ingest" in paths
    assert "/xiaozhi/alert/feishu/callback" in paths
    assert "/xiaozhi/alert/health" in paths

    # 巡检任务要能起也要能停，否则进程退出时会留下待处理任务
    await service.start(interval_seconds=0.01)
    await service.stop()


def test_string_cli_command_is_split_into_argv():
    service = create_alert_relay_service(
        {"alert_relay": {"enabled": True, "diagnosis": {"cli_command": "npx claude"}}}
    )
    assert service._runner.cli_command == ["npx", "claude"]
