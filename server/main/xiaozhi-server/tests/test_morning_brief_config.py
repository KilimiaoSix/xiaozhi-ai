import sys
from types import ModuleType

import pytest

try:
    import yaml  # noqa: F401
except ImportError:
    yaml_stub = ModuleType("yaml")
    yaml_stub.safe_load = lambda stream: {}
    sys.modules["yaml"] = yaml_stub

manage_api_stub = ModuleType("config.manage_api_client")
manage_api_stub.init_service = lambda config: None
manage_api_stub.get_server_config = None
manage_api_stub.get_agent_models = None
manage_api_stub.get_correct_words = None
manage_api_stub.DeviceNotFoundException = type(
    "DeviceNotFoundException", (Exception,), {}
)
manage_api_stub.DeviceBindException = type("DeviceBindException", (Exception,), {})
sys.modules["config.manage_api_client"] = manage_api_stub

from config import config_loader
from core.morning_brief import factory


@pytest.fixture(autouse=True)
def isolate_local_env(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "SERVER_ROOT", tmp_path)


def test_factory_treats_null_identity_and_token_as_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_USER_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("FEISHU_SELF_OPEN_ID", raising=False)

    service = factory.create_morning_brief_service(
        {
            "morning_brief": {
                "user_access_token": None,
                "self_open_id": None,
                "ledger_path": str(tmp_path / "brief.db"),
            }
        }
    )

    assert service.health()["status"] == "CONFIGURATION_REQUIRED"
    assert service.health()["self_open_id_configured"] is False
    assert service.client.capabilities()["user_token_configured"] is False


def test_factory_prefers_environment_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_USER_ACCESS_TOKEN", "u-env-token")
    monkeypatch.setenv("FEISHU_SELF_OPEN_ID", "ou_env")

    service = factory.create_morning_brief_service(
        {
            "morning_brief": {
                "user_access_token": "u-config-token",
                "self_open_id": "ou_config",
                "ledger_path": str(tmp_path / "brief.db"),
            }
        }
    )

    assert service.client.user_access_token == "u-env-token"
    assert service.self_open_id == "ou_env"
    assert service.health()["status"] == "READY"


def test_factory_loads_credentials_from_local_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_USER_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("FEISHU_SELF_OPEN_ID", raising=False)
    monkeypatch.setattr(factory, "SERVER_ROOT", tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".env").write_text(
        "FEISHU_USER_ACCESS_TOKEN='u-file-token'\n"
        'FEISHU_SELF_OPEN_ID="ou_file"\n',
        encoding="utf-8",
    )

    service = factory.create_morning_brief_service(
        {"morning_brief": {"ledger_path": "data/brief.db"}}
    )

    assert service.client.user_access_token == "u-file-token"
    assert service.self_open_id == "ou_file"
    assert service.health()["status"] == "READY"


def test_local_env_file_does_not_override_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_USER_ACCESS_TOKEN", "u-process-token")
    monkeypatch.setenv("FEISHU_SELF_OPEN_ID", "ou_process")
    monkeypatch.setattr(factory, "SERVER_ROOT", tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".env").write_text(
        "FEISHU_USER_ACCESS_TOKEN=u-file-token\n"
        "FEISHU_SELF_OPEN_ID=ou_file\n",
        encoding="utf-8",
    )

    service = factory.create_morning_brief_service(
        {"morning_brief": {"ledger_path": "data/brief.db"}}
    )

    assert service.client.user_access_token == "u-process-token"
    assert service.self_open_id == "ou_process"


def test_factory_defaults_calendar_to_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_USER_ACCESS_TOKEN", raising=False)

    service = factory.create_morning_brief_service(
        {"morning_brief": {"ledger_path": str(tmp_path / "brief.db")}}
    )

    assert service.client.calendar_enabled is True
    assert "calendar:calendar:readonly" in (
        service.client.capabilities()["required_scopes"]
    )


def test_factory_can_disable_calendar_source(tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_USER_ACCESS_TOKEN", raising=False)

    service = factory.create_morning_brief_service(
        {
            "morning_brief": {
                "calendar_enabled": False,
                "ledger_path": str(tmp_path / "brief.db"),
            }
        }
    )

    assert service.client.calendar_enabled is False
    capabilities = service.client.capabilities()
    assert capabilities["calendar_enabled"] is False
    assert not [
        scope
        for scope in capabilities["required_scopes"]
        if scope.startswith("calendar:")
    ]


@pytest.mark.asyncio
async def test_manager_api_mode_preserves_local_morning_brief_config(monkeypatch):
    async def fake_server_config():
        return {"server": {}, "prompt_template": ""}

    monkeypatch.setattr(config_loader, "init_service", lambda config: None)
    monkeypatch.setattr(config_loader, "get_server_config", fake_server_config)

    result = await config_loader.get_config_from_api_async(
        {
            "server": {
                "ip": "127.0.0.1",
                "port": 8000,
                "http_port": 8003,
                "vision_explain": "",
                "auth": {"enabled": False},
                "auth_key": "",
            },
            "manager-api": {"url": "https://manager.example", "secret": "x"},
            "morning_brief": {"enabled": True, "self_open_id": "ou_me"},
            "wellbeing": {
                "enabled": True,
                "bindings": [
                    {"workstation_id": "desktop-local", "device_id": "robot"}
                ],
            },
            "prompt_template": "prompt",
        }
    )

    assert result["morning_brief"] == {
        "enabled": True,
        "self_open_id": "ou_me",
    }
    assert result["wellbeing"]["bindings"][0]["device_id"] == "robot"
