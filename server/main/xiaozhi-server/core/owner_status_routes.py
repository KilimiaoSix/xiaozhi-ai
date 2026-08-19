"""主人状态 HTTP 路由注册。

单独一层是为了能在不起 WebSocket 服务的情况下把路由挂进一个空 app 做测试
（同 presence_routes.py / pomodoro_routes.py 的理由）。
"""

from aiohttp import web


def add_owner_status_routes(app: web.Application, handler) -> None:
    app.add_routes(
        [
            web.get("/xiaozhi/status", handler.handle_get),
            web.post("/xiaozhi/status", handler.handle_post),
            web.options("/xiaozhi/status", handler.handle_options),
        ]
    )
