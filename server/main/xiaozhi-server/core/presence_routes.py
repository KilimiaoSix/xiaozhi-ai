"""Shared aiohttp route registration for presence APIs."""

from aiohttp import web


def add_presence_routes(app: web.Application, handler, stream_handler=None) -> None:
    routes = []
    if stream_handler is not None:
        routes.append(
            web.get(
                "/xiaozhi/presence/stream",
                stream_handler.handle_websocket,
            )
        )
    routes.extend(
        [
            web.post("/xiaozhi/presence/report", handler.handle_report),
            web.options("/xiaozhi/presence/report", handler.handle_options),
            web.get("/xiaozhi/presence/{workstation_id}", handler.handle_get),
            web.options(
                "/xiaozhi/presence/{workstation_id}", handler.handle_options
            ),
        ]
    )
    app.add_routes(routes)
