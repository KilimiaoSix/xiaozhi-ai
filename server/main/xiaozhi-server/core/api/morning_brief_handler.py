"""每日关注晨报 HTTP 适配器。"""

from datetime import date
import hmac
import json
import logging
from typing import Any

from aiohttp import web


MAX_PAYLOAD_BYTES = 4 * 1024


class MorningBriefHandler:
    def __init__(self, config: dict, service, logger=None) -> None:
        server_config = config.get("server", {})
        auth_config = server_config.get("auth", {})
        self._auth_enabled = bool(auth_config.get("enabled", False))
        self._auth_key = str(server_config.get("auth_key", ""))
        self._enabled = bool(config.get("morning_brief", {}).get("enabled", False))
        self._service = service
        self._logger = logger or logging.getLogger(__name__)

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
        return self._json_response(
            "UNAUTHORIZED", "unauthorized", None, status=401
        )

    async def handle_options(self, request: web.Request) -> web.Response:
        response = web.Response(body=b"", content_type="text/plain")
        self._add_cors_headers(response)
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    async def handle_preview(self, request: web.Request) -> web.Response:
        authentication_error = self._authentication_error(request)
        if authentication_error is not None:
            return authentication_error
        if not self._enabled:
            return self._json_response(
                "MORNING_BRIEF_DISABLED",
                "morning brief is disabled",
                None,
                status=503,
            )
        if request.content_length is not None and request.content_length > MAX_PAYLOAD_BYTES:
            return self._json_response(
                "PAYLOAD_TOO_LARGE",
                "request body exceeds 4 KiB",
                None,
                status=413,
            )

        raw_body = await request.read()
        if len(raw_body) > MAX_PAYLOAD_BYTES:
            return self._json_response(
                "PAYLOAD_TOO_LARGE",
                "request body exceeds 4 KiB",
                None,
                status=413,
            )
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json_response(
                "INVALID_JSON", "invalid json body", None, status=400
            )
        if not isinstance(payload, dict):
            return self._json_response(
                "MORNING_BRIEF_INVALID_REQUEST",
                "request body must be an object",
                None,
                status=400,
            )
        unknown = set(payload) - {"report_date"}
        if unknown:
            return self._json_response(
                "MORNING_BRIEF_INVALID_REQUEST",
                f"unsupported field(s): {', '.join(sorted(unknown))}",
                None,
                status=400,
            )
        report_date = None
        if payload.get("report_date") is not None:
            try:
                report_date = date.fromisoformat(str(payload["report_date"]))
            except ValueError:
                return self._json_response(
                    "MORNING_BRIEF_INVALID_REQUEST",
                    "report_date must use YYYY-MM-DD",
                    None,
                    status=400,
                )

        try:
            report = await self._service.preview(report_date)
        except Exception:
            self._logger.exception("Morning brief preview failed")
            return self._json_response(
                "MORNING_BRIEF_UPSTREAM_ERROR",
                "morning brief preview failed",
                None,
                status=502,
            )
        return self._json_response("OK", "success", report)

    async def handle_latest(self, request: web.Request) -> web.Response:
        authentication_error = self._authentication_error(request)
        if authentication_error is not None:
            return authentication_error
        report = self._service.latest()
        if report is None:
            return self._json_response(
                "MORNING_BRIEF_NOT_FOUND",
                "no morning brief preview is available",
                None,
                status=404,
            )
        return self._json_response("OK", "success", report)

    async def handle_health(self, request: web.Request) -> web.Response:
        authentication_error = self._authentication_error(request)
        if authentication_error is not None:
            return authentication_error
        data = dict(self._service.health())
        data["enabled"] = self._enabled
        if not self._enabled:
            data["status"] = "DISABLED"
        return self._json_response("OK", "success", data)

