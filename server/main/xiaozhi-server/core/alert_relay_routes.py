"""告警值班中继 aiohttp 路由。"""

from aiohttp import web


def add_alert_relay_routes(app: web.Application, handler) -> None:
    # 固定路径必须先于 /{alert_id} 注册，否则 health / recent 会被当成 alert_id。
    app.add_routes(
        [
            web.post("/xiaozhi/alert/ingest", handler.handle_ingest),
            web.options("/xiaozhi/alert/ingest", handler.handle_options),
            web.post("/xiaozhi/alert/feishu/callback", handler.handle_feishu_callback),
            web.options("/xiaozhi/alert/feishu/callback", handler.handle_options),
            web.get("/xiaozhi/alert/health", handler.handle_health),
            web.get("/xiaozhi/alert/recent", handler.handle_recent),
            web.get("/xiaozhi/alert/{alert_id}", handler.handle_detail),
            web.options("/xiaozhi/alert/{alert_id}", handler.handle_options),
        ]
    )
