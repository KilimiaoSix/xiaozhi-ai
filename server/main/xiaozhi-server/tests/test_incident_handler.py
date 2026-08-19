"""告警 HTTP 接口测试。

走 aiohttp 测试客户端，manager 用真实实现但注入假推送与临时落盘目录，
只验证接口契约（字段、状态码、鉴权、CORS），不碰真机、不跑 claude。
"""

import pytest
from aiohttp import web

import config.settings
from config.config_loader import get_project_dir, read_config
from config.logger import setup_logging
from core.utils.cache.manager import CacheType, cache_manager

# BaseHandler 构造时会 setup_logging()，配置缓存是冷的就会走 asyncio.run(load_config())，
# 而 handler 是在测试的事件循环里构造的。趁导入阶段先把缓存捂热
# （同 tests/test_pomodoro_handler.py）。
_repo_config = read_config(get_project_dir() + "config.yaml")
cache_manager.set(CacheType.CONFIG, "main_config", _repo_config)
config.settings.config_file_valid = True
setup_logging(_repo_config)

from core.api.incident_handler import IncidentHandler  # noqa: E402
from core.incident_manager import IncidentManager  # noqa: E402
from core.incident_routes import add_incident_routes  # noqa: E402


CONN = object()


class PushRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, conn, text, **kwargs):
        self.calls.append({"text": text, **kwargs})
        return True


def build_app(tmp_path, *, auth=False, manager=None):
    config = {
        "server": {
            "auth": {"enabled": auth},
            "auth_key": "test-secret" if auth else "",
        },
        "incident": {"dedup_cooldown_s": 120, "observe_seconds": 300},
    }
    push = PushRecorder()
    if manager is None:
        manager = IncidentManager(
            config,
            push_event=push,
            device_resolver=lambda: CONN,
            storage_dir=tmp_path,
        )
    app = web.Application()
    add_incident_routes(app, IncidentHandler(config, manager=manager))
    app["incident_manager"] = manager
    app["push"] = push
    return app


async def make_client(aiohttp_client, tmp_path, **kwargs):
    return await aiohttp_client(build_app(tmp_path, **kwargs))


def firing(**overrides):
    payload = {
        "service": "demo-api",
        "severity": "P1",
        "title": "接口错误率升高",
        "message": "支付回调错误率 12%",
        "simulated": True,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_webhook_accepts_alert_and_reports_announcement(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.post("/xiaozhi/incident/webhook", json=firing())

    assert response.status == 200
    body = await response.json()
    assert body["success"] is True
    assert body["outcome"] == "announced"
    assert body["announced"] is True
    assert body["incident"]["severity"] == "P1"
    assert body["incident"]["simulated"] is True
    assert client.app["push"].calls[0]["text"].startswith("【模拟】线上告警：")


@pytest.mark.asyncio
async def test_webhook_merges_duplicate_alert(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)
    await client.post("/xiaozhi/incident/webhook", json=firing())

    response = await client.post("/xiaozhi/incident/webhook", json=firing())

    assert (await response.json())["outcome"] == "merged"
    assert len(client.app["push"].calls) == 1


@pytest.mark.asyncio
async def test_webhook_low_severity_is_not_announced(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.post("/xiaozhi/incident/webhook", json=firing(severity="P3"))

    body = await response.json()
    assert body["outcome"] == "low_severity"
    assert body["announced"] is False
    assert client.app["push"].calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"severity": "P1", "title": "x"},
        {"service": "a", "title": "x"},
        {"service": "a", "severity": "P1"},
        {"service": "a", "severity": "P9", "title": "x"},
        {"service": "a", "severity": "P1", "title": "x", "status": "weird"},
    ],
)
async def test_webhook_rejects_invalid_payload_with_reason(
    aiohttp_client, tmp_path, payload
):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.post("/xiaozhi/incident/webhook", json=payload)

    assert response.status == 400
    body = await response.json()
    assert body["success"] is False
    assert body["message"]


@pytest.mark.asyncio
async def test_webhook_rejects_non_object_body(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.post("/xiaozhi/incident/webhook", json=["nope"])

    assert response.status == 400
    assert (await response.json())["message"] == "body must be a json object"


@pytest.mark.asyncio
async def test_webhook_rejects_broken_json(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.post(
        "/xiaozhi/incident/webhook",
        data=b"{",
        headers={"Content-Type": "application/json"},
    )

    assert response.status == 400
    assert (await response.json())["success"] is False


@pytest.mark.asyncio
async def test_manager_failure_returns_502(aiohttp_client, tmp_path):
    class ExplodingManager:
        def bind(self, **kwargs):
            return None

        async def handle_webhook(self, payload):
            raise RuntimeError("状态机炸了")

    client = await make_client(aiohttp_client, tmp_path, manager=ExplodingManager())

    response = await client.post("/xiaozhi/incident/webhook", json=firing())

    assert response.status == 502
    assert (await response.json())["success"] is False


@pytest.mark.asyncio
async def test_latest_returns_active_incident_and_today_list(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)
    await client.post("/xiaozhi/incident/webhook", json=firing(severity="P0"))
    await client.post(
        "/xiaozhi/incident/webhook", json=firing(severity="P1", title="队列积压")
    )

    response = await client.get("/xiaozhi/incident/latest")

    assert response.status == 200
    body = await response.json()
    assert body["success"] is True
    assert body["active"]["severity"] == "P0"
    assert body["count"] == 2
    assert {item["title"] for item in body["today"]} == {"接口错误率升高", "队列积压"}


@pytest.mark.asyncio
async def test_latest_without_incidents(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    body = await (await client.get("/xiaozhi/incident/latest")).json()

    assert body["active"] is None
    assert body["today"] == []


@pytest.mark.asyncio
async def test_auth_rejects_missing_token_and_accepts_bearer(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path, auth=True)

    missing_post = await client.post("/xiaozhi/incident/webhook", json=firing())
    missing_get = await client.get("/xiaozhi/incident/latest")
    accepted = await client.post(
        "/xiaozhi/incident/webhook",
        json=firing(),
        headers={"Authorization": "Bearer test-secret"},
    )
    wrong = await client.get(
        "/xiaozhi/incident/latest", headers={"Authorization": "Bearer nope"}
    )

    assert missing_post.status == 401
    assert missing_get.status == 401
    assert wrong.status == 401
    assert accepted.status == 200
    assert (await missing_post.json())["success"] is False


@pytest.mark.asyncio
async def test_options_and_responses_carry_cors(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    options = await client.options("/xiaozhi/incident/webhook")
    latest = await client.get("/xiaozhi/incident/latest")

    assert options.status == 200
    assert options.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
    assert latest.headers["Access-Control-Allow-Origin"] == "*"


def test_routes_are_registered_without_websocket_dependency(tmp_path):
    app = build_app(tmp_path)

    signatures = {
        (route.method, route.resource.canonical) for route in app.router.routes()
    }

    # aiohttp 会给每条 GET 顺带注册 HEAD，这里只断言我们自己挂的四条
    assert signatures >= {
        ("POST", "/xiaozhi/incident/webhook"),
        ("OPTIONS", "/xiaozhi/incident/webhook"),
        ("GET", "/xiaozhi/incident/latest"),
        ("OPTIONS", "/xiaozhi/incident/latest"),
    }
