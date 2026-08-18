from pathlib import Path

from aiohttp import web

from core.api.morning_brief_handler import MorningBriefHandler
from core.morning_brief_routes import add_morning_brief_routes


class FakeService:
    async def preview(self, report_date=None):
        return {}

    def latest(self):
        return None

    def health(self):
        return {}


def route_signatures(app):
    return {(route.method, route.resource.canonical) for route in app.router.routes()}


def test_shared_route_registration_contains_all_morning_brief_endpoints():
    app = web.Application()
    handler = MorningBriefHandler(
        {
            "server": {"auth": {"enabled": False}, "auth_key": ""},
            "morning_brief": {"enabled": True},
        },
        FakeService(),
    )

    add_morning_brief_routes(app, handler)

    assert route_signatures(app) >= {
        ("POST", "/xiaozhi/morning-brief/preview"),
        ("GET", "/xiaozhi/morning-brief/latest"),
        ("GET", "/xiaozhi/morning-brief/health"),
        ("OPTIONS", "/xiaozhi/morning-brief/preview"),
        ("OPTIONS", "/xiaozhi/morning-brief/latest"),
        ("OPTIONS", "/xiaozhi/morning-brief/health"),
    }


def test_simple_http_server_composes_shared_morning_brief_routes():
    source = (
        Path(__file__).parents[1] / "core" / "http_server.py"
    ).read_text(encoding="utf-8")

    assert "create_morning_brief_service(config)" in source
    assert "add_morning_brief_routes(app, self.morning_brief_handler)" in source
