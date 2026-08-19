"""手势审批 HTTP 路由注册。

单独一层是为了能在不起 WebSocket 服务的情况下把路由挂进一个空 app 做测试
（同 presence_routes.py / pomodoro_routes.py）。
"""

from aiohttp import web


def add_approval_routes(app: web.Application, handler) -> None:
    # request 必须排在 {approval_id} 前面，否则会被动态段吃掉
    app.add_routes(
        [
            web.post("/xiaozhi/approval/request", handler.handle_request),
            web.options("/xiaozhi/approval/request", handler.handle_options),
            web.post(
                "/xiaozhi/approval/{approval_id}/decision", handler.handle_decision
            ),
            web.options(
                "/xiaozhi/approval/{approval_id}/decision", handler.handle_options
            ),
            web.get("/xiaozhi/approval/{approval_id}", handler.handle_get),
            web.options("/xiaozhi/approval/{approval_id}", handler.handle_options),
        ]
    )
