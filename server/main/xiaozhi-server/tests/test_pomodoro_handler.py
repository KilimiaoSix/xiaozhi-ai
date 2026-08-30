"""番茄钟 HTTP 接口测试。

走 aiohttp 测试客户端，manager 用真实实现但注入假推送函数，
只验证接口契约（字段、状态码、鉴权、CORS），不碰真机。
"""

import pytest
from aiohttp import web

import config.settings
from config.config_loader import get_project_dir, read_config
from config.logger import setup_logging
from core.utils.cache.manager import CacheType, cache_manager
from core.api.pomodoro_handler import PomodoroHandler
from core import pomodoro_manager as pomodoro_module
from core.pomodoro_manager import PomodoroManager
from core.pomodoro_routes import add_pomodoro_routes


# BaseHandler 构造时会 setup_logging()，配置缓存是冷的就会走 asyncio.run(load_config())，
# 而 handler 是在测试的事件循环里构造的。这里趁导入阶段还没有 loop 先把缓存捂热。
# 只用仓库自带的 config.yaml：data/.config.yaml 是 gitignore 的本地密钥配置，
# 全新检出没有它，测试不能把它当前提（否则整个测试套件在收集阶段就死）。
_repo_config = read_config(get_project_dir() + "config.yaml")
cache_manager.set(CacheType.CONFIG, "main_config", _repo_config)
config.settings.config_file_valid = True  # 跳过 data/.config.yaml 存在性检查，测试只需要 log 段
setup_logging(_repo_config)


DEVICE_ID = "dc:da:0c:26:9a:60"
OFFLINE_DEVICE_ID = "dc:da:0c:26:9a:61"


@pytest.fixture(autouse=True)
def _isolate_session_store(tmp_path, monkeypatch):
    """会话每次相位变迁都落盘，接口测试不该往仓库 data/ 里写文件。"""
    monkeypatch.setattr(
        pomodoro_module, "DEFAULT_PERSIST_PATH", str(tmp_path / "pomodoro_sessions.json")
    )


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


async def _noop_alert(conn, text, emotion=None, status=None, silent=False):
    return None


async def _noop_action(conn, action):
    return True


async def _noop_tool(conn, mcp_client, tool_name, args, timeout=30):
    return "true"


def build_app(*, auth=False, registry=None):
    config = {
        "server": {
            "auth": {"enabled": auth},
            "auth_key": "test-secret" if auth else "",
        },
        # 相位压到 10 分钟只是为了让快照数字好断言，测试不等它到点
        "pomodoro": {
            "focus_minutes": 10,
            "short_break_minutes": 5,
            "long_break_minutes": 15,
            "long_break_interval": 4,
        },
    }
    if registry is None:
        registry = FakeRegistry({DEVICE_ID: FakeConn()})
    manager = PomodoroManager(
        config,
        registry,
        push_alert=_noop_alert,
        play_action=_noop_action,
        call_tool=_noop_tool,
        celebration_delay_s=0.0,
    )
    app = web.Application()
    add_pomodoro_routes(app, PomodoroHandler(config, registry, manager=manager))
    app["pomodoro_manager"] = manager
    return app


async def make_client(aiohttp_client, **kwargs):
    return await aiohttp_client(build_app(**kwargs))


async def shutdown(client):
    manager = client.app["pomodoro_manager"]
    for device_id in list(manager.active_device_ids()):
        await manager.stop(device_id)


@pytest.mark.asyncio
async def test_devices_lists_online_and_active_devices(aiohttp_client):
    registry = FakeRegistry({DEVICE_ID: FakeConn()})
    client = await make_client(aiohttp_client, registry=registry)
    # 离线设备只要有活跃会话也要出现在列表里
    await client.app["pomodoro_manager"].start(OFFLINE_DEVICE_ID)

    response = await client.get("/xiaozhi/pomodoro/devices")

    assert response.status == 200
    body = await response.json()
    assert body["success"] is True
    by_id = {item["device_id"]: item for item in body["devices"]}
    assert by_id[DEVICE_ID] == {
        "device_id": DEVICE_ID,
        "connected": True,
        "active": False,
    }
    assert by_id[OFFLINE_DEVICE_ID] == {
        "device_id": OFFLINE_DEVICE_ID,
        "connected": False,
        "active": True,
    }

    await shutdown(client)


@pytest.mark.asyncio
async def test_status_without_session_is_idle(aiohttp_client):
    client = await make_client(aiohttp_client)

    response = await client.get(f"/xiaozhi/pomodoro/{DEVICE_ID}")

    assert response.status == 200
    assert await response.json() == {
        "success": True,
        "device_id": DEVICE_ID,
        "connected": True,
        "active": False,
        "phase": "idle",
        "paused": False,
        "remaining_s": 0,
        "total_s": 0,
        "round": 0,
        "total_rounds": 4,
    }


@pytest.mark.asyncio
async def test_start_command_returns_running_snapshot(aiohttp_client):
    client = await make_client(aiohttp_client)

    response = await client.post(
        f"/xiaozhi/pomodoro/{DEVICE_ID}/command", json={"command": "start"}
    )

    assert response.status == 200
    body = await response.json()
    assert body["success"] is True
    assert body["active"] is True
    assert body["phase"] == "focus"
    assert body["round"] == 1
    assert body["total_s"] == 600
    assert 0 < body["remaining_s"] <= 600

    await shutdown(client)


@pytest.mark.asyncio
async def test_start_honours_focus_minutes(aiohttp_client):
    client = await make_client(aiohttp_client)

    response = await client.post(
        f"/xiaozhi/pomodoro/{DEVICE_ID}/command",
        json={"command": "start", "focus_minutes": 30},
    )

    assert (await response.json())["total_s"] == 1800

    await shutdown(client)


@pytest.mark.asyncio
async def test_pause_resume_and_stop_round_trip(aiohttp_client):
    client = await make_client(aiohttp_client)
    await client.post(f"/xiaozhi/pomodoro/{DEVICE_ID}/command", json={"command": "start"})

    paused = await client.post(
        f"/xiaozhi/pomodoro/{DEVICE_ID}/command", json={"command": "pause"}
    )
    resumed = await client.post(
        f"/xiaozhi/pomodoro/{DEVICE_ID}/command", json={"command": "resume"}
    )
    stopped = await client.post(
        f"/xiaozhi/pomodoro/{DEVICE_ID}/command", json={"command": "stop"}
    )

    assert (await paused.json())["paused"] is True
    assert (await resumed.json())["paused"] is False
    stopped_body = await stopped.json()
    assert stopped_body["active"] is False
    assert stopped_body["phase"] == "idle"


@pytest.mark.asyncio
async def test_toggle_and_skip_are_accepted(aiohttp_client):
    client = await make_client(aiohttp_client)

    toggled = await client.post(
        f"/xiaozhi/pomodoro/{DEVICE_ID}/command", json={"command": "toggle"}
    )
    skipped = await client.post(
        f"/xiaozhi/pomodoro/{DEVICE_ID}/command", json={"command": "skip"}
    )

    assert (await toggled.json())["active"] is True
    assert skipped.status == 200
    assert (await skipped.json())["success"] is True

    await shutdown(client)


@pytest.mark.asyncio
async def test_unknown_command_returns_400(aiohttp_client):
    client = await make_client(aiohttp_client)

    response = await client.post(
        f"/xiaozhi/pomodoro/{DEVICE_ID}/command", json={"command": "explode"}
    )

    assert response.status == 400
    body = await response.json()
    assert body["success"] is False
    assert "explode" in body["message"]


@pytest.mark.asyncio
async def test_invalid_json_body_returns_400(aiohttp_client):
    client = await make_client(aiohttp_client)

    response = await client.post(
        f"/xiaozhi/pomodoro/{DEVICE_ID}/command",
        data=b"{",
        headers={"Content-Type": "application/json"},
    )

    assert response.status == 400
    assert (await response.json())["success"] is False


@pytest.mark.asyncio
async def test_auth_rejects_missing_token_and_accepts_bearer(aiohttp_client):
    client = await make_client(aiohttp_client, auth=True)

    missing = await client.get(f"/xiaozhi/pomodoro/{DEVICE_ID}")
    missing_devices = await client.get("/xiaozhi/pomodoro/devices")
    missing_command = await client.post(
        f"/xiaozhi/pomodoro/{DEVICE_ID}/command", json={"command": "start"}
    )
    accepted = await client.get(
        f"/xiaozhi/pomodoro/{DEVICE_ID}",
        headers={"Authorization": "Bearer test-secret"},
    )

    assert missing.status == 401
    assert missing_devices.status == 401
    assert missing_command.status == 401
    assert (await missing.json())["success"] is False
    assert accepted.status == 200


@pytest.mark.asyncio
async def test_options_and_responses_carry_cors(aiohttp_client):
    client = await make_client(aiohttp_client)

    options = await client.options(f"/xiaozhi/pomodoro/{DEVICE_ID}/command")
    status = await client.get(f"/xiaozhi/pomodoro/{DEVICE_ID}")

    assert options.status == 200
    assert options.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
    assert status.headers["Access-Control-Allow-Origin"] == "*"


def test_routes_are_registered_without_websocket_dependency():
    app = build_app()
    signatures = {
        (route.method, route.resource.canonical) for route in app.router.routes()
    }

    assert signatures >= {
        ("GET", "/xiaozhi/pomodoro/devices"),
        ("GET", "/xiaozhi/pomodoro/{device_id}"),
        ("POST", "/xiaozhi/pomodoro/{device_id}/command"),
        ("OPTIONS", "/xiaozhi/pomodoro/{device_id}"),
        ("OPTIONS", "/xiaozhi/pomodoro/{device_id}/command"),
    }
