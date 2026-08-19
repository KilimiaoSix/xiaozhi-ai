"""飞书任务与会议工作台路由。"""

from aiohttp import web


def add_feishu_workspace_routes(app: web.Application, handler) -> None:
    app.add_routes(
        [
            web.get("/xiaozhi/feishu/status", handler.handle_status),
            web.get("/xiaozhi/feishu/briefing", handler.handle_briefing),
        ]
    )
