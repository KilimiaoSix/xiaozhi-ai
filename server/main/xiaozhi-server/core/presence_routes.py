"""Shared aiohttp route registration for presence APIs."""

from aiohttp import web


def add_presence_routes(app: web.Application, handler) -> None:
    app.add_routes(
        [
            web.post("/xiaozhi/presence/report", handler.handle_report),
            web.options("/xiaozhi/presence/report", handler.handle_options),
            web.get("/xiaozhi/presence/{workstation_id}", handler.handle_get),
            web.options(
                "/xiaozhi/presence/{workstation_id}", handler.handle_options
            ),
        ]
    )
