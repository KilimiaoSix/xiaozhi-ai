"""手势审批的 HTTP 接口。

给 Claude Code / Codex 的 PreToolUse 钩子三个入口：

    POST /xiaozhi/approval/request              登记请求，机器人开口问
    GET  /xiaozhi/approval/{approval_id}         轮询状态
    POST /xiaozhi/approval/{approval_id}/decision 人工兜底（演示导演 / 手势失灵时）

决策权威在 ApprovalManager，这一层只做鉴权、参数校验和快照序列化（同
PomodoroHandler）。鉴权照 event_handler：Bearer token，auth 未开启时放行。
"""
import json

from aiohttp import web

from core.api.base_handler import BaseHandler
from core.approval_manager import (
    DECISIONS,
    DEFAULT_RISK,
    SOURCE_MANUAL,
    get_approval_manager,
)

TAG = __name__


class ApprovalHandler(BaseHandler):
    def __init__(self, config: dict, device_registry=None, manager=None):
        super().__init__(config)
        self.device_registry = device_registry
        self.manager = manager or get_approval_manager(config, device_registry)
        # HTTP 侧通常最早被装配起来，顺手把 config 与注册表接上，
        # 手势观察者那条路径就不必等设备连上来才有得用（同 PomodoroHandler）
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

    async def _read_object(self, request):
        """读 JSON body，返回 (data, 错误响应)。空 body 当作空对象。"""
        raw = await request.read()
        if not raw:
            return {}, None
        try:
            data = json.loads(raw)
        except Exception:
            return None, self._json_response(
                {"success": False, "message": "invalid json body"}, 400
            )
        if not isinstance(data, dict):
            return None, self._json_response(
                {"success": False, "message": "body must be a json object"}, 400
            )
        return data, None

    async def handle_request(self, request):
        """Body: {"summary": 必填, "risk": 可选, "timeout_s": 可选}"""
        if not self._authorized(request):
            return self._unauthorized()

        data, error = await self._read_object(request)
        if error is not None:
            return error

        summary = str(data.get("summary") or "").strip()
        if not summary:
            return self._json_response(
                {"success": False, "message": "summary is required"}, 400
            )

        try:
            result = await self.manager.create_request(
                summary,
                risk=str(data.get("risk") or DEFAULT_RISK),
                timeout_s=data.get("timeout_s"),
            )
        except ValueError as e:
            return self._json_response({"success": False, "message": str(e)}, 400)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"创建审批请求失败: {e}")
            return self._json_response(
                {"success": False, "message": f"create failed: {e}"}, 502
            )

        self.logger.bind(tag=TAG).info(
            f"审批请求 {result['approval_id']} 已登记: {summary}"
        )
        return self._json_response({"success": True, **result})

    async def handle_get(self, request):
        if not self._authorized(request):
            return self._unauthorized()

        approval_id = request.match_info.get("approval_id", "")
        result = self.manager.get(approval_id)
        if result is None:
            return self._json_response(
                {"success": False, "message": f"unknown approval_id: {approval_id}"},
                404,
            )
        return self._json_response({"success": True, **result})

    async def handle_decision(self, request):
        """Body: {"decision": approved|denied}

        人工兜底通道：手势没识别出来、或演示导演要直接给结论时用。
        timeout 不接受——它只能由计时器产生，外部塞进来就成了"假装用户拒绝过"。
        """
        if not self._authorized(request):
            return self._unauthorized()

        approval_id = request.match_info.get("approval_id", "")
        data, error = await self._read_object(request)
        if error is not None:
            return error

        decision = str(data.get("decision") or "").strip()
        if decision not in DECISIONS:
            return self._json_response(
                {"success": False, "message": f"unknown decision: {decision}"}, 400
            )

        try:
            result = await self.manager.resolve(
                approval_id, decision, str(data.get("source") or SOURCE_MANUAL)
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(
                f"审批请求 {approval_id} 决策 {decision} 失败: {e}"
            )
            return self._json_response(
                {"success": False, "message": f"decision failed: {e}"}, 502
            )

        if result is None:
            return self._json_response(
                {"success": False, "message": f"unknown approval_id: {approval_id}"},
                404,
            )

        self.logger.bind(tag=TAG).info(
            f"审批请求 {approval_id} 人工决策 {decision} -> {result['status']}"
        )
        return self._json_response({"success": True, **result})
