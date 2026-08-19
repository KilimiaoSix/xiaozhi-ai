"""审批 HTTP 接口测试。

走 aiohttp 测试客户端，manager 用真实实现但注入假推送函数与假设备，
只验证接口契约（字段、状态码、鉴权、CORS），不碰真机与摄像头。
"""

import asyncio

import pytest
from aiohttp import web

import config.settings
from config.config_loader import get_project_dir, read_config
from config.logger import setup_logging
from core.utils.cache.manager import CacheType, cache_manager
from core.api.approval_handler import ApprovalHandler
from core.approval_manager import (
    ApprovalManager,
    STATUS_APPROVED,
    STATUS_DENIED,
    STATUS_PENDING,
    STATUS_TIMEOUT,
    reset_approval_manager,
)
from core.approval_routes import add_approval_routes


# BaseHandler 构造时会 setup_logging()，配置缓存是冷的就会走 asyncio.run(load_config())，
# 而 handler 是在测试的事件循环里构造的。这里趁导入阶段还没有 loop 先把缓存捂热
# （同 test_pomodoro_handler.py）。
_repo_config = read_config(get_project_dir() + "config.yaml")
cache_manager.set(CacheType.CONFIG, "main_config", _repo_config)
config.settings.config_file_valid = True
setup_logging(_repo_config)


class FakeConn:
    """占位连接。推送函数是假的，不会读它的字段。"""


class Recorder:
    def __init__(self):
        self.calls = []

    async def push(self, conn, text, **kwargs):
        self.calls.append({"text": text, **kwargs})
        return True


@pytest.fixture(autouse=True)
def _isolate_singleton():
    """每个用例前后都清掉单例，避免请求在用例之间串味。"""
    reset_approval_manager()
    yield
    reset_approval_manager()


def build_app(*, auth=False, timeout_s=5, conn=FakeConn(), recorder=None):
    recorder = recorder or Recorder()
    config = {
        "server": {
            "auth": {"enabled": auth},
            "auth_key": "test-secret" if auth else "",
        },
        "approval": {"timeout_s": timeout_s},
    }
    manager = ApprovalManager(
        config, push_event=recorder.push, device_resolver=lambda: conn
    )
    handler = ApprovalHandler(config, None, manager=manager)
    app = web.Application()
    add_approval_routes(app, handler)
    return app, manager, recorder


AUTH = {"Authorization": "Bearer test-secret"}


# ---------------------------------------------------------------- POST /request


@pytest.mark.asyncio
async def test_request_creates_pending_approval(aiohttp_client):
    app, manager, recorder = build_app()
    client = await aiohttp_client(app)

    resp = await client.post(
        "/xiaozhi/approval/request",
        json={"summary": "运行项目测试，只在本地执行"},
    )
    body = await resp.json()

    assert resp.status == 200
    assert body["success"] is True
    assert body["status"] == STATUS_PENDING
    assert body["approval_id"]
    assert body["expires_at"] > body["created_at"]
    assert manager.has_pending() is True
    assert "运行项目测试，只在本地执行" in recorder.calls[0]["text"]


@pytest.mark.asyncio
async def test_request_accepts_risk_and_timeout(aiohttp_client):
    app, _, _ = build_app(timeout_s=25)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/xiaozhi/approval/request",
        json={"summary": "跑一次构建", "risk": "build", "timeout_s": 3},
    )
    body = await resp.json()

    assert body["risk"] == "build"
    assert body["expires_at"] - body["created_at"] == pytest.approx(3.0, abs=0.05)


@pytest.mark.asyncio
async def test_request_without_summary_is_400(aiohttp_client):
    app, _, _ = build_app()
    client = await aiohttp_client(app)

    resp = await client.post("/xiaozhi/approval/request", json={"summary": "  "})

    assert resp.status == 400
    assert (await resp.json())["success"] is False


@pytest.mark.asyncio
async def test_request_with_invalid_json_is_400(aiohttp_client):
    app, _, _ = build_app()
    client = await aiohttp_client(app)

    resp = await client.post(
        "/xiaozhi/approval/request",
        data="not json",
        headers={"Content-Type": "application/json"},
    )

    assert resp.status == 400


@pytest.mark.asyncio
async def test_request_with_non_object_body_is_400(aiohttp_client):
    app, _, _ = build_app()
    client = await aiohttp_client(app)

    resp = await client.post("/xiaozhi/approval/request", json=["nope"])

    assert resp.status == 400


# ---------------------------------------------------------------- GET /{id}


@pytest.mark.asyncio
async def test_get_returns_current_status(aiohttp_client):
    app, manager, _ = build_app()
    client = await aiohttp_client(app)
    created = await manager.create_request("运行项目测试")

    resp = await client.get(f"/xiaozhi/approval/{created['approval_id']}")
    body = await resp.json()

    assert resp.status == 200
    assert body["status"] == STATUS_PENDING
    assert body["decision_source"] is None


@pytest.mark.asyncio
async def test_get_reflects_gesture_decision(aiohttp_client):
    """手势那条路径解决掉的请求，钩子必须在这里查得到。"""
    app, manager, _ = build_app()
    client = await aiohttp_client(app)
    created = await manager.create_request("运行项目测试")

    await manager.resolve_pending(STATUS_APPROVED, "gesture")

    body = await (
        await client.get(f"/xiaozhi/approval/{created['approval_id']}")
    ).json()
    assert body["status"] == STATUS_APPROVED
    assert body["decision_source"] == "gesture"


@pytest.mark.asyncio
async def test_get_reflects_timeout(aiohttp_client):
    """超时必须能被钩子看到，且不是 approved。"""
    app, manager, _ = build_app()
    client = await aiohttp_client(app)
    created = await manager.create_request("运行项目测试", timeout_s=0.05)

    for _ in range(100):
        await asyncio.sleep(0.01)
        if manager.get(created["approval_id"])["status"] != STATUS_PENDING:
            break

    body = await (
        await client.get(f"/xiaozhi/approval/{created['approval_id']}")
    ).json()
    assert body["status"] == STATUS_TIMEOUT


@pytest.mark.asyncio
async def test_get_unknown_id_is_404(aiohttp_client):
    app, _, _ = build_app()
    client = await aiohttp_client(app)

    resp = await client.get("/xiaozhi/approval/does-not-exist")

    assert resp.status == 404
    assert (await resp.json())["success"] is False


# ---------------------------------------------------------------- POST /decision


@pytest.mark.asyncio
async def test_decision_approves(aiohttp_client):
    app, manager, recorder = build_app()
    client = await aiohttp_client(app)
    created = await manager.create_request("运行项目测试")

    resp = await client.post(
        f"/xiaozhi/approval/{created['approval_id']}/decision",
        json={"decision": "approved"},
    )
    body = await resp.json()

    assert resp.status == 200
    assert body["status"] == STATUS_APPROVED
    assert body["decision_source"] == "manual"
    assert recorder.calls[-1]["text"] == "已允许，继续执行"


@pytest.mark.asyncio
async def test_decision_denies(aiohttp_client):
    app, manager, _ = build_app()
    client = await aiohttp_client(app)
    created = await manager.create_request("运行项目测试")

    body = await (
        await client.post(
            f"/xiaozhi/approval/{created['approval_id']}/decision",
            json={"decision": "denied"},
        )
    ).json()

    assert body["status"] == STATUS_DENIED


@pytest.mark.asyncio
async def test_decision_is_idempotent(aiohttp_client):
    """人工兜底和手势可能撞车，第二次不得翻案。"""
    app, manager, _ = build_app()
    client = await aiohttp_client(app)
    created = await manager.create_request("运行项目测试")
    path = f"/xiaozhi/approval/{created['approval_id']}/decision"

    await client.post(path, json={"decision": "denied"})
    body = await (await client.post(path, json={"decision": "approved"})).json()

    assert body["status"] == STATUS_DENIED


@pytest.mark.asyncio
async def test_decision_rejects_timeout_value(aiohttp_client):
    """timeout 只能由计时器产生，接口不接受。"""
    app, manager, _ = build_app()
    client = await aiohttp_client(app)
    created = await manager.create_request("运行项目测试")

    resp = await client.post(
        f"/xiaozhi/approval/{created['approval_id']}/decision",
        json={"decision": "timeout"},
    )

    assert resp.status == 400


@pytest.mark.asyncio
async def test_decision_rejects_unknown_value(aiohttp_client):
    app, manager, _ = build_app()
    client = await aiohttp_client(app)
    created = await manager.create_request("运行项目测试")

    resp = await client.post(
        f"/xiaozhi/approval/{created['approval_id']}/decision",
        json={"decision": "maybe"},
    )

    assert resp.status == 400


@pytest.mark.asyncio
async def test_decision_unknown_id_is_404(aiohttp_client):
    app, _, _ = build_app()
    client = await aiohttp_client(app)

    resp = await client.post(
        "/xiaozhi/approval/nope/decision", json={"decision": "approved"}
    )

    assert resp.status == 404


# ---------------------------------------------------------------- 鉴权与 CORS


@pytest.mark.asyncio
async def test_all_endpoints_require_bearer_when_auth_enabled(aiohttp_client):
    app, manager, _ = build_app(auth=True)
    client = await aiohttp_client(app)
    created = await manager.create_request("运行项目测试")
    approval_id = created["approval_id"]

    unauthorized = [
        await client.post("/xiaozhi/approval/request", json={"summary": "x"}),
        await client.get(f"/xiaozhi/approval/{approval_id}"),
        await client.post(
            f"/xiaozhi/approval/{approval_id}/decision",
            json={"decision": "approved"},
        ),
    ]
    assert [r.status for r in unauthorized] == [401, 401, 401]
    assert manager.get(approval_id)["status"] == STATUS_PENDING, "未授权不得改状态"


@pytest.mark.asyncio
async def test_bearer_token_is_accepted(aiohttp_client):
    app, manager, _ = build_app(auth=True)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/xiaozhi/approval/request", json={"summary": "运行项目测试"}, headers=AUTH
    )
    body = await resp.json()
    assert resp.status == 200

    resp = await client.get(f"/xiaozhi/approval/{body['approval_id']}", headers=AUTH)
    assert resp.status == 200

    resp = await client.post(
        f"/xiaozhi/approval/{body['approval_id']}/decision",
        json={"decision": "approved"},
        headers=AUTH,
    )
    assert resp.status == 200
    assert (await resp.json())["status"] == STATUS_APPROVED


@pytest.mark.asyncio
async def test_wrong_token_is_rejected(aiohttp_client):
    app, _, _ = build_app(auth=True)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/xiaozhi/approval/request",
        json={"summary": "x"},
        headers={"Authorization": "Bearer wrong"},
    )

    assert resp.status == 401


@pytest.mark.asyncio
async def test_options_returns_cors_headers(aiohttp_client):
    app, _, _ = build_app()
    client = await aiohttp_client(app)

    for path in (
        "/xiaozhi/approval/request",
        "/xiaozhi/approval/abc",
        "/xiaozhi/approval/abc/decision",
    ):
        resp = await client.options(path)
        assert resp.status == 200, path
        assert resp.headers["Access-Control-Allow-Origin"] == "*"
        assert "authorization" in resp.headers["Access-Control-Allow-Headers"]


@pytest.mark.asyncio
async def test_request_route_is_not_shadowed_by_id_route(aiohttp_client):
    """/request 必须先于 /{approval_id} 匹配，否则登记接口会被动态段吃掉。"""
    app, _, _ = build_app()
    client = await aiohttp_client(app)

    resp = await client.post(
        "/xiaozhi/approval/request", json={"summary": "运行项目测试"}
    )

    assert resp.status == 200
    assert (await resp.json())["status"] == STATUS_PENDING
