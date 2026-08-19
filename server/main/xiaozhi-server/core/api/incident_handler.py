"""线上告警的 HTTP 入口（需求文档流程七）。

两个接口：

- POST /xiaozhi/incident/webhook  监控系统 / 演示脚本把告警打进来
- GET  /xiaozhi/incident/latest   当前最该关注的故障 + 今天的故障列表

这一层只做鉴权、参数校验与序列化；要不要播报、怎么降噪、什么时候宣布恢复
全在 IncidentManager 里。校验失败（severity 写错、缺 service）一律 400 并把
原因带回去——监控接进来的当天，这条错误信息就是唯一的排障线索。
"""

import json

from aiohttp import web

from core.api.base_handler import BaseHandler
from core.incident_manager import get_incident_manager

TAG = __name__


class IncidentHandler(BaseHandler):
    def __init__(self, config: dict, device_registry=None, manager=None):
        super().__init__(config)
        self.device_registry = device_registry
        self.manager = manager or get_incident_manager(config)
        # HTTP 侧通常最早装配起来，顺手把 config 与注册表接上，
        # 语音路径就不必等设备连上来才有得用（同 PomodoroHandler）。
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

    async def handle_webhook(self, request):
        """Body: {"service","severity":"P0|P1|P2|P3","title", ...}，详见 IncidentManager。"""
        if not self._authorized(request):
            return self._unauthorized()

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

        try:
            result = await self.manager.handle_webhook(data)
        except ValueError as e:
            # 字段非法：监控侧的问题，把原因原样告诉它
            return self._json_response({"success": False, "message": str(e)}, 400)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"处理告警 webhook 失败: {e}")
            return self._json_response(
                {"success": False, "message": f"webhook failed: {e}"}, 502
            )

        self.logger.bind(tag=TAG).info(
            f"告警 {result['incident_id']} 处理结果: {result['outcome']}"
            f"（播报={result['announced']}）"
        )
        return self._json_response({"success": True, **result})

    async def handle_latest(self, request):
        """当前活跃故障 + 今日故障列表（日终总结与桌面端用）。"""
        if not self._authorized(request):
            return self._unauthorized()

        try:
            active = self.manager.active_incident()
            today = self.manager.list_today()
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"读取故障列表失败: {e}")
            return self._json_response(
                {"success": False, "message": f"read failed: {e}"}, 502
            )

        return self._json_response(
            {"success": True, "active": active, "count": len(today), "today": today}
        )
