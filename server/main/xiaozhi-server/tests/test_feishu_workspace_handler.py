from datetime import date

import pytest
from aiohttp import web

from core.api.feishu_workspace_handler import FeishuWorkspaceHandler
from core.feishu_workspace.service import FeishuWorkspaceUnavailable
from core.feishu_workspace_routes import add_feishu_workspace_routes


STATUS = {
    "state": "ready",
    "message": "Server 已配置飞书用户凭据。",
    "open_id": "ou_me",
    "source": "server_openapi",
    "required_scopes": ["calendar:calendar:readonly", "task:task:read"],
    "missing_scopes": [],
}


class FakeService:
    def __init__(self):
        self.briefing_dates = []

    def status(self):
        return STATUS

    async def get_briefing(self, report_date=None):
        self.briefing_dates.append(report_date)
        return {
            "status": STATUS,
            "date": (report_date or date(2026, 8, 19)).isoformat(),
            "meetings": [],
            "tasks": [],
            "warnings": [],
            "fetched_at": "2026-08-19T08:00:00+08:00",
        }


class BriefingFailingService(FakeService):
    async def get_briefing(self, report_date=None):
        raise FeishuWorkspaceUnavailable("两个飞书数据源都不可用")


class ConfigurationRequiredService(FakeService):
    def __init__(self):
        super().__init__()
        self.called = False

    def status(self):
        return {
            **STATUS,
            "state": "configuration_required",
            "message": "Server 尚未配置 FEISHU_USER_ACCESS_TOKEN。",
        }

    async def get_briefing(self, report_date=None):
        self.called = True
        return await super().get_briefing(report_date)


async def make_client(aiohttp_client, *, auth=False, service=None):
    config = {
        "server": {
            "auth": {"enabled": auth},
            "auth_key": "test-secret" if auth else "",
        }
    }
    app = web.Application()
    current_service = service or FakeService()
    add_feishu_workspace_routes(
        app,
        FeishuWorkspaceHandler(config, current_service),
    )
    return await aiohttp_client(app), current_service


@pytest.mark.asyncio
async def test_status_exposes_server_openapi_configuration_without_token(
    aiohttp_client,
):
    client, _ = await make_client(aiohttp_client)

    response = await client.get("/xiaozhi/feishu/status")

    assert response.status == 200
    assert await response.json() == {
        "code": "OK",
        "message": "success",
        "data": STATUS,
    }
    assert "access_token" not in (await response.text()).lower()


@pytest.mark.asyncio
async def test_briefing_accepts_optional_iso_date(aiohttp_client):
    client, service = await make_client(aiohttp_client)

    response = await client.get(
        "/xiaozhi/feishu/briefing", params={"date": "2026-08-19"}
    )

    assert response.status == 200
    payload = await response.json()
    assert payload["code"] == "OK"
    assert payload["data"]["date"] == "2026-08-19"
    assert service.briefing_dates == [date(2026, 8, 19)]


@pytest.mark.asyncio
async def test_briefing_returns_502_when_both_upstreams_fail(aiohttp_client):
    client, _ = await make_client(
        aiohttp_client,
        service=BriefingFailingService(),
    )

    response = await client.get("/xiaozhi/feishu/briefing")

    assert response.status == 502
    assert await response.json() == {
        "code": "FEISHU_WORKSPACE_UPSTREAM_ERROR",
        "message": "两个飞书数据源都不可用",
        "data": None,
    }


@pytest.mark.asyncio
async def test_briefing_returns_503_before_io_when_server_token_is_missing(
    aiohttp_client,
):
    service = ConfigurationRequiredService()
    client, _ = await make_client(aiohttp_client, service=service)

    response = await client.get("/xiaozhi/feishu/briefing")

    assert response.status == 503
    assert (await response.json())["code"] == "FEISHU_WORKSPACE_NOT_CONFIGURED"
    assert service.called is False


@pytest.mark.asyncio
async def test_bearer_authentication_protects_status_and_briefing(aiohttp_client):
    client, _ = await make_client(aiohttp_client, auth=True)

    status_missing = await client.get("/xiaozhi/feishu/status")
    briefing_missing = await client.get("/xiaozhi/feishu/briefing")
    accepted = await client.get(
        "/xiaozhi/feishu/status",
        headers={"Authorization": "Bearer test-secret"},
    )

    assert status_missing.status == briefing_missing.status == 401
    assert accepted.status == 200


@pytest.mark.asyncio
async def test_briefing_rejects_non_iso_date(aiohttp_client):
    client, _ = await make_client(aiohttp_client)

    response = await client.get(
        "/xiaozhi/feishu/briefing", params={"date": "19/08/2026"}
    )

    assert response.status == 400
    assert (await response.json())["code"] == "FEISHU_WORKSPACE_INVALID_REQUEST"
