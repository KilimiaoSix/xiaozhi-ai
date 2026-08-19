"""飞书任务与会议工作台 HTTP 适配器。"""

from __future__ import annotations

import hmac
import json
from datetime import date
from typing import Any

from aiohttp import web

from core.feishu_workspace.service import FeishuWorkspaceUnavailable


class FeishuWorkspaceHandler:
    def __init__(self, config: dict, service, logger=None) -> None:
        server_config = config.get("server", {})
        auth_config = server_config.get("auth", {})
        self._auth_enabled = bool(auth_config.get("enabled", False))
        self._auth_key = str(server_config.get("auth_key", ""))
        self._service = service
        self._logger = logger

    def _authorized(self, request: web.Request) -> bool:
        if not self._auth_enabled:
            return True
        token = request.headers.get("Authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        return bool(self._auth_key) and hmac.compare_digest(token, self._auth_key)

    @staticmethod
    def _add_cors_headers(response: web.Response) -> None:
        response.headers["Access-Control-Allow-Headers"] = (
            "content-type, authorization"
        )
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Origin"] = "*"

    def _json_response(
        self,
        code: str,
        message: str,
        data: Any,
        *,
        status: int = 200,
    ) -> web.Response:
        response = web.Response(
            text=json.dumps(
                {"code": code, "message": message, "data": data},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            content_type="application/json",
            status=status,
        )
        self._add_cors_headers(response)
        return response

    def _authentication_error(self, request: web.Request) -> web.Response | None:
        if self._authorized(request):
            return None
        return self._json_response("UNAUTHORIZED", "unauthorized", None, status=401)

    async def handle_status(self, request: web.Request) -> web.Response:
        authentication_error = self._authentication_error(request)
        if authentication_error is not None:
            return authentication_error
        return self._json_response("OK", "success", self._service.status())

    async def handle_briefing(self, request: web.Request) -> web.Response:
        authentication_error = self._authentication_error(request)
        if authentication_error is not None:
            return authentication_error
        current_status = self._service.status()
        if current_status.get("state") != "ready":
            return self._json_response(
                "FEISHU_WORKSPACE_NOT_CONFIGURED",
                str(current_status.get("message") or "feishu is not configured"),
                current_status,
                status=503,
            )
        report_date = None
        if request.query.get("date"):
            try:
                report_date = date.fromisoformat(request.query["date"])
            except ValueError:
                return self._json_response(
                    "FEISHU_WORKSPACE_INVALID_REQUEST",
                    "date must use YYYY-MM-DD",
                    None,
                    status=400,
                )
        try:
            briefing = await self._service.get_briefing(report_date)
        except FeishuWorkspaceUnavailable as error:
            return self._json_response(
                "FEISHU_WORKSPACE_UPSTREAM_ERROR",
                str(error),
                None,
                status=502,
            )
        return self._json_response("OK", "success", briefing)
