from datetime import date

import pytest
from aiohttp import web

from core.api.morning_brief_handler import MorningBriefHandler
from core.morning_brief_routes import add_morning_brief_routes


REPORT = {
    "report_type": "OPEN_ATTENTION",
    "report_date": "2026-08-18",
    "coverage_status": "COMPLETE",
    "top_three": [],
}


class FakeService:
    def __init__(self, latest=REPORT):
        self.preview_dates = []
        self.latest_value = latest
        self.preview_error = None

    async def preview(self, report_date=None):
        self.preview_dates.append(report_date)
        if self.preview_error:
            raise self.preview_error
        return REPORT

    def latest(self):
        return self.latest_value

    def health(self):
        return {
            "status": "READY",
            "capabilities": {
                "user_token_configured": True,
                "client_unread_cursor_supported": False,
            },
        }


async def make_client(
    aiohttp_client,
    *,
    auth=False,
    enabled=True,
    service=None,
):
    config = {
        "server": {
            "auth": {"enabled": auth},
            "auth_key": "test-secret" if auth else "",
        },
        "morning_brief": {"enabled": enabled},
    }
    app = web.Application()
    current_service = service or FakeService()
    add_morning_brief_routes(
        app,
        MorningBriefHandler(config, current_service),
    )
    return await aiohttp_client(app), current_service


@pytest.mark.asyncio
async def test_preview_accepts_optional_report_date(aiohttp_client):
    client, service = await make_client(aiohttp_client)

    response = await client.post(
        "/xiaozhi/morning-brief/preview",
        json={"report_date": "2026-08-18"},
    )

    assert response.status == 200
    assert await response.json() == {
        "code": "OK",
        "message": "success",
        "data": REPORT,
    }
    assert service.preview_dates == [date(2026, 8, 18)]


@pytest.mark.asyncio
async def test_invalid_preview_body_returns_400(aiohttp_client):
    client, _ = await make_client(aiohttp_client)

    malformed = await client.post(
        "/xiaozhi/morning-brief/preview",
        data=b"{",
        headers={"Content-Type": "application/json"},
    )
    bad_date = await client.post(
        "/xiaozhi/morning-brief/preview",
        json={"report_date": "18/08/2026"},
    )
    extra = await client.post(
        "/xiaozhi/morning-brief/preview",
        json={"unknown": True},
    )

    assert malformed.status == bad_date.status == extra.status == 400
    assert (await malformed.json())["code"] == "INVALID_JSON"
    assert (await bad_date.json())["code"] == "MORNING_BRIEF_INVALID_REQUEST"
    assert (await extra.json())["code"] == "MORNING_BRIEF_INVALID_REQUEST"


@pytest.mark.asyncio
async def test_disabled_preview_does_not_call_external_service(aiohttp_client):
    client, service = await make_client(aiohttp_client, enabled=False)

    response = await client.post("/xiaozhi/morning-brief/preview", json={})

    assert response.status == 503
    assert (await response.json())["code"] == "MORNING_BRIEF_DISABLED"
    assert service.preview_dates == []


@pytest.mark.asyncio
async def test_bearer_authentication_protects_all_endpoints(aiohttp_client):
    client, _ = await make_client(aiohttp_client, auth=True)

    missing = await client.get("/xiaozhi/morning-brief/health")
    accepted = await client.get(
        "/xiaozhi/morning-brief/health",
        headers={"Authorization": "Bearer test-secret"},
    )

    assert missing.status == 401
    assert (await missing.json())["code"] == "UNAUTHORIZED"
    assert accepted.status == 200


@pytest.mark.asyncio
async def test_latest_returns_404_before_first_preview(aiohttp_client):
    client, _ = await make_client(
        aiohttp_client,
        service=FakeService(latest=None),
    )

    response = await client.get("/xiaozhi/morning-brief/latest")

    assert response.status == 404
    assert (await response.json())["code"] == "MORNING_BRIEF_NOT_FOUND"


@pytest.mark.asyncio
async def test_health_reports_enabled_without_exposing_credentials(aiohttp_client):
    client, _ = await make_client(aiohttp_client)

    response = await client.get("/xiaozhi/morning-brief/health")
    payload = await response.json()
    response_text = await response.text()

    assert payload["data"]["enabled"] is True
    assert payload["data"]["capabilities"]["user_token_configured"] is True
    assert "token" not in response_text.lower().replace("user_token_configured", "")


@pytest.mark.asyncio
async def test_preview_failure_returns_502_and_cors(aiohttp_client):
    service = FakeService()
    service.preview_error = RuntimeError("collector crashed")
    client, _ = await make_client(aiohttp_client, service=service)

    response = await client.post("/xiaozhi/morning-brief/preview", json={})
    options = await client.options("/xiaozhi/morning-brief/preview")

    assert response.status == 502
    assert (await response.json())["code"] == "MORNING_BRIEF_UPSTREAM_ERROR"
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert options.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
