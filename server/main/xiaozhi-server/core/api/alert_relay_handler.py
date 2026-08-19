"""告警值班中继的 HTTP 适配器。

两类调用方，脾气完全不同：

- SAE / 运维脚本调 `ingest`：按本仓库既有习惯用 Bearer 鉴权、错误码回 4xx。
- 飞书调 `feishu/callback`：**非 200 会被重推**，所以除了鉴权失败和加密事件，
  一律回 200 温和吞掉，把判断留给 service。
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from typing import Any

from aiohttp import web


MAX_PAYLOAD_BYTES = 64 * 1024

# 文本消息里 @ 出来的是 `@_user_1` 这种占位符，不剔掉会污染意图识别。
_MENTION_PLACEHOLDER = re.compile(r"@_user_\d+\s*")


class AlertRelayHandler:
    def __init__(self, config: dict, service, logger=None) -> None:
        server_config = config.get("server", {})
        auth_config = server_config.get("auth", {})
        relay_config = config.get("alert_relay", {}) or {}
        self._auth_enabled = bool(auth_config.get("enabled", False))
        self._auth_key = str(server_config.get("auth_key", ""))
        self._enabled = bool(relay_config.get("enabled", False))
        self._verification_token = str(
            (relay_config.get("feishu", {}) or {}).get("verification_token", "") or ""
        )
        self._service = service
        self._logger = logger or logging.getLogger(__name__)

    # ------------------------------------------------------------ 基础设施

    def _authorized(self, request: web.Request) -> bool:
        if not self._auth_enabled:
            return True
        token = request.headers.get("Authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        return bool(self._auth_key) and hmac.compare_digest(token, self._auth_key)

    @staticmethod
    def _add_cors_headers(response: web.Response) -> None:
        response.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Origin"] = "*"

    def _json_response(
        self, code: str, message: str, data: Any, *, status: int = 200
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

    def _raw_response(self, payload: dict[str, Any], status: int = 200) -> web.Response:
        """飞书要的是裸 JSON（challenge / toast），不能套本仓库的信封。"""
        response = web.Response(
            text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            content_type="application/json",
            status=status,
        )
        self._add_cors_headers(response)
        return response

    async def _read_json(self, request: web.Request) -> Any:
        if request.content_length is not None and request.content_length > MAX_PAYLOAD_BYTES:
            raise ValueError("PAYLOAD_TOO_LARGE")
        raw = await request.read()
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValueError("PAYLOAD_TOO_LARGE")
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    async def handle_options(self, request: web.Request) -> web.Response:
        response = web.Response(body=b"", content_type="text/plain")
        self._add_cors_headers(response)
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    # ------------------------------------------------------------ 告警接入

    async def handle_ingest(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._json_response("UNAUTHORIZED", "unauthorized", None, status=401)
        if not self._enabled:
            return self._json_response(
                "ALERT_RELAY_DISABLED", "告警值班中继未启用", None, status=503
            )
        try:
            payload = await self._read_json(request)
        except ValueError as exc:
            if str(exc) == "PAYLOAD_TOO_LARGE":
                return self._json_response(
                    "PAYLOAD_TOO_LARGE", "请求体超过 64 KiB", None, status=413
                )
            return self._json_response("INVALID_JSON", "invalid json body", None, status=400)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json_response("INVALID_JSON", "invalid json body", None, status=400)
        if not isinstance(payload, dict):
            return self._json_response(
                "INVALID_REQUEST", "请求体必须是对象", None, status=400
            )

        try:
            result = await self._service.ingest(payload)
        except Exception:
            self._logger.exception("告警接入失败")
            return self._json_response(
                "ALERT_RELAY_ERROR", "告警接入失败", None, status=500
            )

        code = str(result.get("code", "OK"))
        if code == "OK":
            return self._json_response("OK", "success", result)
        status = 503 if code == "ALERT_RELAY_DISABLED" else 400
        return self._json_response(code, str(result.get("message", "")), None, status=status)

    # ------------------------------------------------------------ 飞书回调

    async def handle_feishu_callback(self, request: web.Request) -> web.Response:
        try:
            payload = await self._read_json(request)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return self._raw_response({"code": "INVALID_JSON", "message": "invalid json"}, 400)
        if not isinstance(payload, dict):
            return self._raw_response({"code": "INVALID_JSON", "message": "invalid json"}, 400)

        if payload.get("encrypt"):
            # 本中继不解密。回 400 让人一眼看到配错了，而不是让回调石沉大海。
            return self._raw_response(
                {
                    "code": "ENCRYPTED_EVENT_UNSUPPORTED",
                    "message": "事件已加密，请在开放平台关闭事件加密（Encrypt Key 留空）",
                },
                400,
            )

        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        token = str(payload.get("token") or header.get("token") or "")
        if self._verification_token and not hmac.compare_digest(
            token, self._verification_token
        ):
            return self._raw_response(
                {"code": "UNAUTHORIZED", "message": "verification token mismatch"}, 401
            )

        if payload.get("type") == "url_verification":
            return self._raw_response({"challenge": str(payload.get("challenge") or "")})

        if not self._enabled:
            return self._raw_response({"code": 0, "msg": "alert relay disabled"})

        event_type = str(header.get("event_type") or payload.get("type") or "")
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}

        try:
            if payload.get("action") or event_type == "card.action.trigger":
                return await self._handle_card_action(payload, event)
            if event_type == "im.message.receive_v1":
                return await self._handle_message(event)
        except Exception:
            # 回 200 避免飞书无限重推；失败细节进日志。
            self._logger.exception("飞书回调处理失败")
            return self._raw_response({"code": 0, "msg": "handled with error"})

        return self._raw_response({"code": 0, "msg": "ignored"})

    async def _handle_card_action(
        self, payload: dict[str, Any], event: dict[str, Any]
    ) -> web.Response:
        action = event.get("action") if isinstance(event.get("action"), dict) else None
        if action is None:
            action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        value = action.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        if not isinstance(value, dict):
            value = {}

        context = event.get("context") if isinstance(event.get("context"), dict) else {}
        operator = event.get("operator") if isinstance(event.get("operator"), dict) else {}
        user = str(
            operator.get("open_id")
            or operator.get("user_id")
            or payload.get("open_id")
            or payload.get("user_id")
            or ""
        )
        message_id = str(
            context.get("open_message_id") or payload.get("open_message_id") or ""
        )

        result = await self._service.handle_reply(
            alert_id=str(value.get("alert_id") or ""),
            intent=str(value.get("intent") or ""),
            user=user,
            message_id=message_id,
        )
        return self._raw_response({"toast": self._toast(result)})

    @staticmethod
    def _toast(result: dict[str, Any]) -> dict[str, str]:
        code = str(result.get("code", ""))
        if code == "OK":
            state = str(result.get("state", ""))
            content = "收到，我去查" if state == "CLAIMED" else "好，这条你自己看"
            return {"type": "success", "content": content}
        if code == "ALREADY_HANDLED":
            return {"type": "info", "content": "这条已经在处理了"}
        return {"type": "error", "content": str(result.get("message") or "没接上")}

    async def _handle_message(self, event: dict[str, Any]) -> web.Response:
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        if str(message.get("message_type") or "") != "text":
            return self._raw_response({"code": 0, "msg": "ignored non-text"})

        # 只认回在告警卡片话题下的消息，群里的闲聊不该被当成回复。
        root_id = str(message.get("root_id") or message.get("parent_id") or "")
        if not root_id:
            return self._raw_response({"code": 0, "msg": "ignored non-threaded"})

        text = ""
        content = message.get("content")
        if isinstance(content, str) and content:
            try:
                parsed = json.loads(content)
                text = str(parsed.get("text", "")) if isinstance(parsed, dict) else ""
            except json.JSONDecodeError:
                text = content
        text = _MENTION_PLACEHOLDER.sub("", text).strip()

        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        user = str(sender_id.get("open_id") or sender_id.get("user_id") or "")

        await self._service.handle_reply(
            root_message_id=root_id,
            text=text,
            user=user,
            message_id=str(message.get("message_id") or ""),
        )
        return self._raw_response({"code": 0, "msg": "ok"})

    # ------------------------------------------------------------ 状态查询

    async def handle_detail(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._json_response("UNAUTHORIZED", "unauthorized", None, status=401)
        alert_id = request.match_info.get("alert_id", "")
        record = self._service.get(alert_id)
        if record is None:
            return self._json_response(
                "ALERT_NOT_FOUND", f"没有告警 {alert_id}", None, status=404
            )
        return self._json_response("OK", "success", record)

    async def handle_recent(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._json_response("UNAUTHORIZED", "unauthorized", None, status=401)
        try:
            limit = int(request.query.get("limit", 20))
        except ValueError:
            limit = 20
        return self._json_response(
            "OK", "success", {"alerts": self._service.recent(limit)}
        )

    async def handle_health(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._json_response("UNAUTHORIZED", "unauthorized", None, status=401)
        data = dict(self._service.health())
        data["enabled"] = self._enabled
        if not self._enabled:
            data["status"] = "DISABLED"
        return self._json_response("OK", "success", data)
