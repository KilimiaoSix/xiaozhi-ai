"""外部工作事件推送接口。

给 Codex / Claude Code / 监控告警等外部来源一个 HTTP 入口，把事件推给指定的
常连设备。设备侧需要是常连固件（WebsocketProtocol 开机即连并 ping 保活）。
"""
import json

from aiohttp import web

from core.api.base_handler import BaseHandler
from core.handle.pushHandle import DEFAULT_EMOTION, DEFAULT_STATUS, push_work_event

TAG = __name__


def _positive_float(value):
    """把 restore_after 解析成正浮点数，非法或非正一律当作不恢复。"""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class EventHandler(BaseHandler):
    def __init__(self, config: dict, device_registry):
        super().__init__(config)
        self.device_registry = device_registry
        auth_config = config["server"].get("auth", {})
        self.auth_enable = auth_config.get("enabled", False)
        self.auth_key = config["server"].get("auth_key", "")

    def _authorized(self, request) -> bool:
        if not self.auth_enable:
            return True
        token = request.headers.get("authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        return bool(self.auth_key) and token == self.auth_key

    def _json_response(self, payload: dict, status: int = 200):
        response = web.Response(
            text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            content_type="application/json",
            status=status,
        )
        self._add_cors_headers(response)
        return response

    async def handle_devices(self, request):
        """列出当前在线设备，联调时用来确认设备是否真的常连上来了。"""
        if not self._authorized(request):
            return self._json_response({"ok": False, "message": "unauthorized"}, 401)
        devices = self.device_registry.device_ids()
        return self._json_response({"ok": True, "count": len(devices), "devices": devices})

    async def handle_push(self, request):
        """推送一条工作事件到指定设备。

        Body: {"device_id": 必填, "text": 必填, "emotion": 可选, "status": 可选,
               "speak": 可选, "restore_after": 可选（秒，到点把画面恢复到设备基态）}
        """
        if not self._authorized(request):
            return self._json_response({"ok": False, "message": "unauthorized"}, 401)

        try:
            data = await request.json()
        except Exception:
            return self._json_response({"ok": False, "message": "invalid json body"}, 400)

        if not isinstance(data, dict):
            return self._json_response({"ok": False, "message": "body must be a json object"}, 400)

        device_id = data.get("device_id") or request.headers.get("device-id", "")
        if not device_id:
            return self._json_response({"ok": False, "message": "device_id is required"}, 400)

        text = data.get("text")
        if not text or not str(text).strip():
            return self._json_response({"ok": False, "message": "text is required"}, 400)

        conn = self.device_registry.get(device_id)
        if conn is None:
            return self._json_response(
                {
                    "ok": False,
                    "message": f"device {device_id} is not online",
                    "online_devices": self.device_registry.device_ids(),
                },
                404,
            )

        try:
            spoke = await push_work_event(
                conn,
                text=str(text),
                emotion=str(data.get("emotion") or DEFAULT_EMOTION),
                status=str(data.get("status") or DEFAULT_STATUS),
                speak=bool(data.get("speak", False)),
                restore_after=_positive_float(data.get("restore_after")),
                action=(str(data["action"]).strip() if data.get("action") else None),
                idle_animation=(bool(data["idle_animation"])
                                if "idle_animation" in data else None),
                silent=bool(data.get("silent", False)),
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"推送事件到设备 {device_id} 失败: {e}")
            return self._json_response(
                {"ok": False, "message": f"push failed: {e}", "delivered": False}, 502
            )

        # 流程五：主人离席时经过的事件攒进台账，等他回来一次性汇总。
        # 是否入待播报队列由 AwayLedger 按离席窗口自行判断，这里无条件调用。
        try:
            from core.away_ledger import get_away_ledger

            get_away_ledger(self.config).record(
                str(data.get("kind") or "generic"),
                str(text),
                task_key=str(data.get("task_key") or ""),
                severity=str(data.get("severity") or "normal"),
                source=str(data.get("source") or ""),
            )
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"离席台账记账失败: {e}")

        self.logger.bind(tag=TAG).info(f"已推送工作事件到设备 {device_id}: {text}")
        return self._json_response(
            {"ok": True, "device_id": device_id, "delivered": True, "spoke": spoke}
        )
