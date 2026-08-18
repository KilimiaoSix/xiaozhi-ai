"""番茄钟的 HTTP 控制接口。

给桌面端一个不经过语音也不经过按键的控制面板入口：轮询状态、开始/暂停/跳过/停止。
计时权威仍然在 PomodoroManager，这一层只做鉴权、参数校验和快照序列化。
"""
import json

from aiohttp import web

from core.api.base_handler import BaseHandler
from core.pomodoro_manager import COMMANDS, pomodoro_manager

TAG = __name__


class PomodoroHandler(BaseHandler):
    def __init__(self, config: dict, device_registry, manager=None):
        super().__init__(config)
        self.device_registry = device_registry
        self.manager = manager or pomodoro_manager
        # HTTP 侧通常是最早被装配起来的一条路径，顺手把 config 与注册表接上，
        # 语音 / 按键路径就不必等设备连上来才有得用。
        self.manager.bind(
            config=config, device_registry=device_registry, logger=self.logger
        )
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

    def _unauthorized(self):
        return self._json_response({"success": False, "message": "unauthorized"}, 401)

    async def handle_devices(self, request):
        """在线设备 ∪ 有活跃会话的设备。

        设备掉线时会话还在服务端跑着，桌面端得能看见它，否则一次 WiFi 抖动
        就会让面板上的番茄钟凭空消失。
        """
        if not self._authorized(request):
            return self._unauthorized()

        online = list(self.device_registry.device_ids()) if self.device_registry else []
        active = self.manager.active_device_ids()
        device_ids = list(dict.fromkeys(online + active))
        devices = [
            {
                "device_id": device_id,
                "connected": device_id in online,
                "active": device_id in active,
            }
            for device_id in device_ids
        ]
        return self._json_response({"success": True, "devices": devices})

    async def handle_get(self, request):
        if not self._authorized(request):
            return self._unauthorized()

        device_id = request.match_info.get("device_id", "")
        result = await self.manager.status(device_id)
        return self._json_response({"success": True, **result["status"]})

    async def handle_command(self, request):
        """Body: {"command": start|pause|resume|toggle|skip|stop, "focus_minutes": 可选}"""
        if not self._authorized(request):
            return self._unauthorized()

        device_id = request.match_info.get("device_id", "")
        try:
            data = await request.json()
        except Exception:
            return self._json_response(
                {"success": False, "message": "invalid json body"}, 400
            )
        if not isinstance(data, dict):
            return self._json_response(
                {"success": False, "message": "body must be a json object"}, 400
            )

        command = str(data.get("command", "")).strip()
        if command not in COMMANDS:
            return self._json_response(
                {"success": False, "message": f"unknown command: {command}"}, 400
            )

        try:
            result = await self.manager.execute(
                device_id, command, focus_minutes=data.get("focus_minutes")
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(
                f"番茄钟命令 {command} 在设备 {device_id} 上执行失败: {e}"
            )
            return self._json_response(
                {"success": False, "message": f"command failed: {e}"}, 502
            )

        self.logger.bind(tag=TAG).info(
            f"番茄钟命令 {command} -> 设备 {device_id}: {result['outcome']}"
        )
        return self._json_response({"success": True, **result["status"]})
