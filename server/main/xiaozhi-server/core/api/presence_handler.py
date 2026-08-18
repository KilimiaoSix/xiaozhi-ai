"""HTTP adapter for camera presence reports and latest-state queries."""

from datetime import datetime, timezone
import hmac
import json
import logging
from typing import Any, Callable

from aiohttp import web

from core.presence_registry import (
    PresenceOutOfOrderError,
    PresenceRegistry,
    PresenceReport,
    PresenceValidationError,
    WORKSTATION_PATTERN,
)


MAX_PAYLOAD_BYTES = 16 * 1024


class PresenceHandler:
    def __init__(
        self,
        config: dict,
        registry: PresenceRegistry,
        now_provider: Callable[[], datetime] | None = None,
        logger=None,
    ) -> None:
        server_config = config.get("server", {})
        auth_config = server_config.get("auth", {})
        self._auth_enabled = bool(auth_config.get("enabled", False))
        self._auth_key = str(server_config.get("auth_key", ""))
        self._registry = registry
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._logger = logger or logging.getLogger(__name__)

    def _authorized(self, request: web.Request) -> bool:
        if not self._auth_enabled:
            return True
        token = request.headers.get("Authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        return bool(self._auth_key) and hmac.compare_digest(token, self._auth_key)

    @staticmethod
    def _add_cors_headers(response: web.StreamResponse) -> None:
        response.headers["Access-Control-Allow-Headers"] = (
            "client-id, content-type, device-id, authorization"
        )
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Origin"] = "*"

    def _json_response(
        self,
        code: str,
        message: str,
        data: Any,
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

    async def handle_options(self, request: web.Request) -> web.Response:
        response = web.Response(body=b"", content_type="text/plain")
        self._add_cors_headers(response)
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    async def handle_report(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._json_response(
                "UNAUTHORIZED", "unauthorized", None, status=401
            )

        if request.content_length is not None and request.content_length > MAX_PAYLOAD_BYTES:
            return self._json_response(
                "PAYLOAD_TOO_LARGE",
                "request body exceeds 16 KiB",
                None,
                status=413,
            )

        raw_body = await request.read()
        if len(raw_body) > MAX_PAYLOAD_BYTES:
            return self._json_response(
                "PAYLOAD_TOO_LARGE",
                "request body exceeds 16 KiB",
                None,
                status=413,
            )

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json_response(
                "INVALID_JSON", "invalid json body", None, status=400
            )

        try:
            report = PresenceReport.from_payload(payload, self._now_provider())
            result = self._registry.accept(report)
        except PresenceValidationError as exc:
            return self._json_response(
                "PRESENCE_INVALID_REQUEST", str(exc), None, status=400
            )
        except PresenceOutOfOrderError as exc:
            return self._json_response(
                "PRESENCE_OUT_OF_ORDER", str(exc), None, status=409
            )
        except Exception:
            self._logger.exception("Unexpected error while accepting presence report")
            return self._json_response(
                "INTERNAL_ERROR", "internal server error", None, status=500
            )

        return self._json_response("OK", "success", result)

    async def handle_get(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._json_response(
                "UNAUTHORIZED", "unauthorized", None, status=401
            )

        workstation_id = request.match_info.get("workstation_id", "")
        if not WORKSTATION_PATTERN.fullmatch(workstation_id):
            return self._json_response(
                "PRESENCE_INVALID_REQUEST",
                "workstation_id must match [A-Za-z0-9._-]{1,64}",
                None,
                status=400,
            )

        state = self._registry.get(workstation_id)
        if state is None:
            return self._json_response(
                "PRESENCE_NOT_FOUND",
                f"no presence report for workstation {workstation_id}",
                None,
                status=404,
            )
        return self._json_response("OK", "success", state)
