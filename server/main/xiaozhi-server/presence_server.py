"""Lightweight presence-only HTTP server for local demos and integration."""

import argparse
import os

from aiohttp import web

from core.api.camera_stream_handler import CameraStreamHandler
from core.api.presence_handler import PresenceHandler
from core.presence_registry import PresenceRegistry
from core.presence_routes import add_presence_routes


def create_presence_app(
    config: dict,
    registry: PresenceRegistry | None = None,
) -> web.Application:
    app = web.Application()
    current_registry = registry or PresenceRegistry()
    app["presence_registry"] = current_registry
    stream_handler = CameraStreamHandler(config, current_registry)
    app["camera_stream_handler"] = stream_handler
    add_presence_routes(
        app,
        PresenceHandler(config, current_registry),
        stream_handler,
    )
    return app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run the presence-only API server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("PRESENCE_AUTH_TOKEN", ""),
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    config = {
        "server": {
            "auth": {"enabled": bool(args.auth_token)},
            "auth_key": args.auth_token,
        }
    }
    web.run_app(create_presence_app(config), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
