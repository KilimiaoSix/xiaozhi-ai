"""Run the real morning-brief HTTP chain against the live i讯飞 OpenAPI.

用真实配置构建 service/handler/routes，起一个临时 aiohttp 服务并依次调用
health、preview、latest 三个接口，打印真实返回，用于本机联调。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import aiohttp
from aiohttp import web

from config.config_loader import read_config, get_project_dir
from core.api.morning_brief_handler import MorningBriefHandler
from core.morning_brief.factory import create_morning_brief_service
from core.morning_brief_routes import add_morning_brief_routes


def load_config() -> dict:
    config = read_config(get_project_dir() + "config.yaml")
    private_path = get_project_dir() + "data/.config.yaml"
    if os.path.exists(private_path):
        private = read_config(private_path) or {}
        for key, value in private.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
    return config


def create_app(config: dict) -> web.Application:
    app = web.Application()
    service = create_morning_brief_service(config)
    add_morning_brief_routes(app, MorningBriefHandler(config, service))
    return app


async def main() -> int:
    config = load_config()
    app = create_app(config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18099)
    await site.start()

    base = "http://127.0.0.1:18099/xiaozhi/morning-brief"
    try:
        async with aiohttp.ClientSession() as session:
            for method, path, body in (
                ("GET", "/health", None),
                ("POST", "/preview", {}),
                ("GET", "/latest", None),
            ):
                async with session.request(
                    method, base + path, json=body
                ) as response:
                    payload = await response.json(content_type=None)
                print(f"=== {method} {path} -> HTTP {response.status} ===")
                print(json.dumps(payload, ensure_ascii=False, indent=1))
    finally:
        await runner.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
