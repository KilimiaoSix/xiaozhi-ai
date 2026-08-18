"""番茄钟 HTTP 路由注册。

单独一层是为了能在不起 WebSocket 服务的情况下把路由挂进一个空 app 做测试
（同 presence_routes.py）。
"""

from aiohttp import web


def add_pomodoro_routes(app: web.Application, handler) -> None:
    # devices 必须排在 {device_id} 前面，否则会被动态段吃掉
    app.add_routes(
        [
            web.get("/xiaozhi/pomodoro/devices", handler.handle_devices),
            web.options("/xiaozhi/pomodoro/devices", handler.handle_options),
            web.get("/xiaozhi/pomodoro/{device_id}", handler.handle_get),
            web.options("/xiaozhi/pomodoro/{device_id}", handler.handle_options),
            web.post(
                "/xiaozhi/pomodoro/{device_id}/command", handler.handle_command
            ),
            web.options(
                "/xiaozhi/pomodoro/{device_id}/command", handler.handle_options
            ),
        ]
    )
