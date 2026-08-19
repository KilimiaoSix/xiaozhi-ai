"""离席汇总查询接口。

桌面端「推到电脑」的数据源：机器人只用三句话把返岗汇总念完（TTS 记不住更多），
详细清单要有个地方能看全。GET /xiaozhi/away/summary 就是那份全量。

只读，不清账——清账由在岗编排在真的播报出去之后做（core/presence_arrival.py），
不然桌面端刷新一下页面就把主人还没听到的留言标成已播报了。
"""

import hmac
import json
import logging
from typing import Any

from aiohttp import web

TAG = __name__


class AwaySummaryHandler:
    def __init__(self, config: dict, away_ledger, logger=None) -> None:
        server_config = (config or {}).get("server", {}) or {}
        auth_config = server_config.get("auth", {}) or {}
        self._auth_enabled = bool(auth_config.get("enabled", False))
        self._auth_key = str(server_config.get("auth_key", ""))
        self._ledger = away_ledger
        self._logger = logger or logging.getLogger(__name__)

    def _authorized(self, request: web.Request) -> bool:
        if not self._auth_enabled:
            return True
        token = request.headers.get("Authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        # compare_digest 而不是 ==：避免用响应时间把 auth_key 逐字节试出来
        return bool(self._auth_key) and hmac.compare_digest(token, self._auth_key)

    @staticmethod
    def _add_cors_headers(response: web.Response) -> None:
        response.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Origin"] = "*"

    def _json_response(self, payload: dict[str, Any], status: int = 200) -> web.Response:
        response = web.Response(
            text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            content_type="application/json",
            status=status,
        )
        self._add_cors_headers(response)
        return response

    async def handle_options(self, request: web.Request) -> web.Response:
        response = web.Response(body=b"", content_type="text/plain")
        self._add_cors_headers(response)
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        return response

    async def handle_summary(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._json_response({"ok": False, "message": "unauthorized"}, 401)

        try:
            summary = dict(self._ledger.pending_summary())
            speech = self._ledger.compose_speech()
        except Exception as e:
            # 桌面端在轮询这个接口，台账炸了也不该把它变成 500
            self._logger.warning(f"读取离席汇总失败: {e}")
            return self._json_response(
                {"ok": False, "message": f"away summary unavailable: {e}"}, 502
            )

        summary["ok"] = True
        summary["away"] = bool(self._ledger.is_away())
        summary["speech"] = speech
        return self._json_response(summary)
