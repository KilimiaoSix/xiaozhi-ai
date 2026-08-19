"""主人状态 HTTP 接口。

来访者应答与桌面/固件面板都可能要读这份状态，因此单独开一个只读 + 写入的
薄接口层；真正的状态机、请假到期回落与 overdue 判定全部在 core/owner_status.py，
这一层只做鉴权、参数透传和响应封装。鉴权与响应格式照抄 core/api/event_handler.py，
保持仓库里 HTTP 层的手感一致。
"""
import json

from aiohttp import web

from core.api.base_handler import BaseHandler
from core.owner_status import OwnerStatusStore

TAG = __name__


class OwnerStatusHandler(BaseHandler):
    def __init__(self, config: dict, store: OwnerStatusStore):
        super().__init__(config)
        self.store = store
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

    async def handle_get(self, request):
        """当前有效状态：请假到期回落、overdue 判定都在 store.get() 里惰性算好。"""
        if not self._authorized(request):
            return self._json_response({"ok": False, "message": "unauthorized"}, 401)
        return self._json_response({"ok": True, **self.store.get()})

    async def handle_post(self, request):
        """设置声明状态。body 直传给 store.set_status，非法输入翻成 400。"""
        if not self._authorized(request):
            return self._json_response({"ok": False, "message": "unauthorized"}, 401)

        try:
            data = await request.json()
        except Exception:
            return self._json_response(
                {"ok": False, "message": "invalid json body"}, 400
            )

        if not isinstance(data, dict):
            return self._json_response(
                {"ok": False, "message": "body must be a json object"}, 400
            )

        state = data.get("state")
        if not state:
            return self._json_response(
                {"ok": False, "message": "state is required"}, 400
            )

        try:
            status = self.store.set_status(
                state=str(state),
                public_note=str(data.get("public_note") or ""),
                expected_return=data.get("expected_return"),
                leave_start=data.get("leave_start"),
                leave_end=data.get("leave_end"),
            )
        except ValueError as e:
            return self._json_response({"ok": False, "message": str(e)}, 400)

        return self._json_response({"ok": True, **status})
