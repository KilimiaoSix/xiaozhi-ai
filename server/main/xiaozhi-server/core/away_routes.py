"""离席汇总 HTTP 路由注册。

单独一层是为了能在不起 WebSocket 服务的情况下把路由挂进一个空 app 做测试
（同 presence_routes.py / pomodoro_routes.py）。
"""

from aiohttp import web


def add_away_routes(app: web.Application, handler) -> None:
    app.add_routes(
        [
            web.get("/xiaozhi/away/summary", handler.handle_summary),
            web.options("/xiaozhi/away/summary", handler.handle_options),
        ]
    )
