"""离席汇总查询接口测试。

走 aiohttp 测试客户端，台账用真实实现但落盘到 tmp_path，
只验证接口契约（字段、状态码、鉴权、CORS）。
"""

from datetime import datetime, timedelta

import pytest
from aiohttp import web

from core.api.away_summary_handler import AwaySummaryHandler
from core.away_ledger import AwayLedger
from core.away_routes import add_away_routes


START = datetime(2026, 8, 19, 14, 0, 0)


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def build_app(tmp_path, *, auth=False, ledger=None):
    config = {
        "server": {
            "auth": {"enabled": auth},
            "auth_key": "test-secret" if auth else "",
        }
    }
    if ledger is None:
        ledger = AwayLedger(tmp_path / "away_ledger.json", clock=FakeClock())
    app = web.Application()
    add_away_routes(app, AwaySummaryHandler(config, ledger))
    app["away_ledger"] = ledger
    return app


@pytest.mark.asyncio
async def test_summary_reports_idle_state_when_owner_present(aiohttp_client, tmp_path):
    client = await aiohttp_client(build_app(tmp_path))

    response = await client.get("/xiaozhi/away/summary")

    assert response.status == 200
    body = await response.json()
    assert body["ok"] is True
    assert body["away"] is False
    assert body["items"] == []
    assert body["count"] == 0
    assert body["away_minutes"] == 0


@pytest.mark.asyncio
async def test_summary_returns_sorted_pending_items(aiohttp_client, tmp_path):
    clock = FakeClock()
    ledger = AwayLedger(tmp_path / "away_ledger.json", clock=clock)
    ledger.mark_away()
    ledger.record("visitor_message", "有同事留言：日志方案已发飞书")
    ledger.record("incident", "生产环境 5xx 飙升", severity="critical")
    clock.advance(minutes=20)
    client = await aiohttp_client(build_app(tmp_path, ledger=ledger))

    body = await (await client.get("/xiaozhi/away/summary")).json()

    assert body["away"] is True
    assert body["away_minutes"] == 20
    assert body["count"] == 2
    assert [item["kind"] for item in body["items"]] == [
        "incident",
        "visitor_message",
    ]
    assert body["items"][0]["severity"] == "critical"
    assert body["items"][1]["text"] == "有同事留言：日志方案已发飞书"
    assert body["speech"].startswith("你离开的二十分钟里，")


@pytest.mark.asyncio
async def test_summary_speech_is_null_without_pending(aiohttp_client, tmp_path):
    client = await aiohttp_client(build_app(tmp_path))

    body = await (await client.get("/xiaozhi/away/summary")).json()

    assert body["speech"] is None


@pytest.mark.asyncio
async def test_summary_requires_bearer_token_when_auth_enabled(
    aiohttp_client, tmp_path
):
    client = await aiohttp_client(build_app(tmp_path, auth=True))

    response = await client.get("/xiaozhi/away/summary")

    assert response.status == 401
    assert (await response.json())["ok"] is False


@pytest.mark.asyncio
async def test_summary_accepts_valid_bearer_token(aiohttp_client, tmp_path):
    client = await aiohttp_client(build_app(tmp_path, auth=True))

    response = await client.get(
        "/xiaozhi/away/summary",
        headers={"Authorization": "Bearer test-secret"},
    )

    assert response.status == 200


@pytest.mark.asyncio
async def test_summary_rejects_wrong_bearer_token(aiohttp_client, tmp_path):
    client = await aiohttp_client(build_app(tmp_path, auth=True))

    response = await client.get(
        "/xiaozhi/away/summary",
        headers={"Authorization": "Bearer nope"},
    )

    assert response.status == 401


@pytest.mark.asyncio
async def test_options_returns_cors_headers(aiohttp_client, tmp_path):
    client = await aiohttp_client(build_app(tmp_path))

    response = await client.options("/xiaozhi/away/summary")

    assert response.status == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "GET" in response.headers["Access-Control-Allow-Methods"]


@pytest.mark.asyncio
async def test_ledger_failure_returns_502_not_500(aiohttp_client, tmp_path):
    """桌面端轮询这个接口，台账炸了也不该把 HTTP 服务拖成 500。"""

    class BrokenLedger:
        def pending_summary(self):
            raise RuntimeError("disk on fire")

        def is_away(self):
            return False

        def compose_speech(self):
            return None

    client = await aiohttp_client(build_app(tmp_path, ledger=BrokenLedger()))

    response = await client.get("/xiaozhi/away/summary")

    assert response.status == 502
    assert (await response.json())["ok"] is False
