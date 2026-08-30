"""主人状态 HTTP 接口测试。

走 aiohttp 测试客户端，store 用真实实现但落盘到 tmp_path，只验证接口契约
（字段、状态码、鉴权、CORS），不碰真机、不碰语音链路。
"""

from datetime import datetime, timedelta

import pytest
from aiohttp import web

from core.api.owner_status_handler import OwnerStatusHandler
from core.owner_status import OwnerStatusStore, STATUS_AVAILABLE, STATUS_LEAVE, STATUS_MEETING
from core.owner_status_routes import add_owner_status_routes

# 冻结时钟：store 的请假到期惰性回落用它判定，避免用例里写死的日期
# 随真实系统时间推移变成"日期炸弹"（到了 leave_end 次日，get() 会把
# leave 状态冲回 available）。
FIXED_NOW = datetime(2026, 8, 19, 9, 0, 0)


def build_app(tmp_path, *, auth=False):
    config = {
        "server": {
            "auth": {"enabled": auth},
            "auth_key": "test-secret" if auth else "",
        }
    }
    store = OwnerStatusStore(tmp_path / "owner_status.json", clock=lambda: FIXED_NOW)
    app = web.Application()
    handler = OwnerStatusHandler(config, store)
    add_owner_status_routes(app, handler)
    app["owner_status_store"] = store
    return app


async def make_client(aiohttp_client, tmp_path, **kwargs):
    return await aiohttp_client(build_app(tmp_path, **kwargs))


@pytest.mark.asyncio
async def test_get_returns_default_available(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.get("/xiaozhi/status")

    assert response.status == 200
    body = await response.json()
    assert body["ok"] is True
    assert body["state"] == STATUS_AVAILABLE
    assert body["overdue"] is False


@pytest.mark.asyncio
async def test_post_sets_meeting_state_and_get_reflects_it(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.post(
        "/xiaozhi/status",
        json={"state": STATUS_MEETING, "expected_return": "2026-08-19T11:30:00"},
    )

    assert response.status == 200
    body = await response.json()
    assert body["ok"] is True
    assert body["state"] == STATUS_MEETING
    assert body["expected_return"] == "2026-08-19T11:30:00"

    follow_up = await client.get("/xiaozhi/status")
    assert (await follow_up.json())["state"] == STATUS_MEETING


@pytest.mark.asyncio
async def test_post_sets_leave_range(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)
    # 相对冻结时钟推导，而非写死绝对日期：这样请假区间始终在 FIXED_NOW
    # 之后，不会被 store 的惰性过期逻辑在断言前就冲回 available。
    leave_start = (FIXED_NOW.date() + timedelta(days=1)).isoformat()
    leave_end = (FIXED_NOW.date() + timedelta(days=2)).isoformat()

    response = await client.post(
        "/xiaozhi/status",
        json={
            "state": STATUS_LEAVE,
            "leave_start": leave_start,
            "leave_end": leave_end,
        },
    )

    body = await response.json()
    assert body["state"] == STATUS_LEAVE
    assert body["leave_start"] == leave_start
    assert body["leave_end"] == leave_end


@pytest.mark.asyncio
async def test_post_missing_state_returns_400(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.post("/xiaozhi/status", json={})

    assert response.status == 400
    assert (await response.json())["ok"] is False


@pytest.mark.asyncio
async def test_post_invalid_state_returns_400(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.post("/xiaozhi/status", json={"state": "napping"})

    assert response.status == 400
    body = await response.json()
    assert body["ok"] is False
    assert "napping" in body["message"]


@pytest.mark.asyncio
async def test_post_leave_without_leave_start_returns_400(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.post("/xiaozhi/status", json={"state": STATUS_LEAVE})

    assert response.status == 400


@pytest.mark.asyncio
async def test_post_invalid_json_body_returns_400(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.post(
        "/xiaozhi/status", data=b"{", headers={"Content-Type": "application/json"}
    )

    assert response.status == 400
    assert (await response.json())["ok"] is False


@pytest.mark.asyncio
async def test_post_non_object_body_returns_400(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.post(
        "/xiaozhi/status", data=b"[1,2,3]", headers={"Content-Type": "application/json"}
    )

    assert response.status == 400


@pytest.mark.asyncio
async def test_auth_rejects_missing_token_and_accepts_bearer(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path, auth=True)

    missing_get = await client.get("/xiaozhi/status")
    missing_post = await client.post("/xiaozhi/status", json={"state": STATUS_MEETING})
    accepted = await client.get(
        "/xiaozhi/status", headers={"Authorization": "Bearer test-secret"}
    )

    assert missing_get.status == 401
    assert missing_post.status == 401
    assert (await missing_get.json())["ok"] is False
    assert accepted.status == 200


@pytest.mark.asyncio
async def test_options_carries_cors(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.options("/xiaozhi/status")

    assert response.status == 200
    assert response.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"


@pytest.mark.asyncio
async def test_get_response_carries_cors(aiohttp_client, tmp_path):
    client = await make_client(aiohttp_client, tmp_path)

    response = await client.get("/xiaozhi/status")

    assert response.headers["Access-Control-Allow-Origin"] == "*"


def test_routes_are_registered_without_websocket_dependency(tmp_path):
    app = build_app(tmp_path)
    signatures = {
        (route.method, route.resource.canonical) for route in app.router.routes()
    }

    assert signatures >= {
        ("GET", "/xiaozhi/status"),
        ("POST", "/xiaozhi/status"),
        ("OPTIONS", "/xiaozhi/status"),
    }
