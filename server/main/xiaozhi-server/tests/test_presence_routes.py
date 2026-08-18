from aiohttp import web

from core.api.presence_handler import PresenceHandler
from core.presence_registry import PresenceRegistry
from core.presence_routes import add_presence_routes
from presence_server import create_presence_app


CONFIG = {"server": {"auth": {"enabled": False}, "auth_key": ""}}


def route_signatures(app):
    return {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
    }


def test_route_registration_has_no_websocket_dependency():
    app = web.Application()
    add_presence_routes(app, PresenceHandler(CONFIG, PresenceRegistry()))

    assert route_signatures(app) >= {
        ("POST", "/xiaozhi/presence/report"),
        ("OPTIONS", "/xiaozhi/presence/report"),
        ("GET", "/xiaozhi/presence/{workstation_id}"),
        ("OPTIONS", "/xiaozhi/presence/{workstation_id}"),
    }


def test_lightweight_server_uses_shared_routes():
    app = create_presence_app(CONFIG)

    assert ("POST", "/xiaozhi/presence/report") in route_signatures(app)
    assert ("GET", "/xiaozhi/presence/{workstation_id}") in route_signatures(app)
    assert ("GET", "/xiaozhi/presence/stream") in route_signatures(app)
