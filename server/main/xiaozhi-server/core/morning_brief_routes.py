"""每日关注晨报 aiohttp 路由。"""

from aiohttp import web


def add_morning_brief_routes(app: web.Application, handler) -> None:
    app.add_routes(
        [
            web.post(
                "/xiaozhi/morning-brief/preview", handler.handle_preview
            ),
            web.get("/xiaozhi/morning-brief/latest", handler.handle_latest),
            web.get("/xiaozhi/morning-brief/health", handler.handle_health),
            web.options(
                "/xiaozhi/morning-brief/preview", handler.handle_options
            ),
            web.options(
                "/xiaozhi/morning-brief/latest", handler.handle_options
            ),
            web.options(
                "/xiaozhi/morning-brief/health", handler.handle_options
            ),
        ]
    )

