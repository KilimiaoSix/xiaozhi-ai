"""线上告警 HTTP 路由注册。

单独一层是为了能在不起 WebSocket 服务的情况下把路由挂进一个空 app 做测试
（同 presence_routes.py / pomodoro_routes.py）。
"""

from aiohttp import web


def add_incident_routes(app: web.Application, handler) -> None:
    app.add_routes(
        [
            web.post("/xiaozhi/incident/webhook", handler.handle_webhook),
            web.options("/xiaozhi/incident/webhook", handler.handle_options),
            web.get("/xiaozhi/incident/latest", handler.handle_latest),
            web.options("/xiaozhi/incident/latest", handler.handle_options),
        ]
    )
