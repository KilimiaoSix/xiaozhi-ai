"""飞书机器人（应用身份）客户端。

与晨报的 `morning_brief/feishu_client.py` 刻意分开：
晨报读的是「我的消息」必须用用户令牌，这里是「机器人主动发消息」用应用令牌，
两套权限体系不同，混用会在私有化部署上撞上截然不同的报错。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import aiohttp


DEFAULT_BASE_URL = "https://open.feishu.cn"

# 机器人发消息与加表情所需权限（在开放平台的「权限管理」里开通）：
REQUIRED_SCOPES = (
    "im:message:send_as_bot",
    "im:message.reaction:write",
)

# 令牌快到期就提前换，别等真过期后拿 401 再补救——那会丢掉一次告警通知。
TOKEN_REFRESH_MARGIN_SECONDS = 300


class FeishuBotError(RuntimeError):
    def __init__(self, endpoint: str, code: Any, message: str):
        self.endpoint = endpoint
        self.code = code
        self.api_message = message
        super().__init__(f"飞书 OpenAPI {endpoint} 失败: code={code}, message={message}")


def _receive_id_type(receive_id: str) -> str:
    value = str(receive_id or "")
    if value.startswith("oc_"):
        return "chat_id"
    if value.startswith("ou_"):
        return "open_id"
    if value.startswith("on_"):
        return "union_id"
    if "@" in value:
        return "email"
    return "user_id"


class FeishuBot:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        app_id: str = "",
        app_secret: str = "",
        *,
        timeout_seconds: float = 10.0,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self._session = session
        self._clock = clock
        self._token = ""
        self._token_expires_at = 0.0

    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def capabilities(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "app_id_configured": bool(self.app_id),
            "app_secret_configured": bool(self.app_secret),
            "required_scopes": list(REQUIRED_SCOPES),
        }

    def _redact(self, text: str) -> str:
        value = str(text or "")
        for secret in (self.app_secret, self._token):
            if secret and len(secret) > 3:
                value = value.replace(secret, "[REDACTED]")
        return value

    async def _post(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
        params: dict[str, str] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)

        async def execute(session: aiohttp.ClientSession) -> dict[str, Any]:
            async with session.post(
                url, json=json_body, params=params, headers=headers, timeout=timeout
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    raise FeishuBotError(path, "INVALID_RESPONSE", "响应不是合法 JSON") from exc
                if not isinstance(payload, dict):
                    raise FeishuBotError(path, "INVALID_RESPONSE", "响应不是 JSON 对象")
                code = payload.get("code", 0)
                if response.status >= 400 or code not in (0, "0", None):
                    message = str(payload.get("msg") or payload.get("message") or "未知错误")
                    raise FeishuBotError(path, code, self._redact(message))
                return payload

        if self._session is not None:
            return await execute(self._session)
        async with aiohttp.ClientSession() as session:
            return await execute(session)

    async def _tenant_token(self) -> str:
        if not self.configured():
            raise FeishuBotError(
                "/open-apis/auth/v3/tenant_access_token/internal",
                "NOT_CONFIGURED",
                "飞书机器人 app_id / app_secret 未配置",
            )
        now = self._clock()
        if self._token and now < self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
            return self._token

        payload = await self._post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json_body={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        token = str(payload.get("tenant_access_token") or "")
        if not token:
            raise FeishuBotError(
                "/open-apis/auth/v3/tenant_access_token/internal",
                "INVALID_RESPONSE",
                "响应里没有 tenant_access_token",
            )
        self._token = token
        self._token_expires_at = now + float(payload.get("expire") or 7200)
        return token

    async def send_card(self, receive_id: str, card: dict[str, Any]) -> dict[str, Any]:
        token = await self._tenant_token()
        payload = await self._post(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": _receive_id_type(receive_id)},
            json_body={
                "receive_id": receive_id,
                "msg_type": "interactive",
                # content 必须是 JSON 字符串，接口不接受嵌套对象。
                "content": json.dumps(card, ensure_ascii=False),
            },
            token=token,
        )
        data = payload.get("data") or {}
        return {
            "message_id": str(data.get("message_id") or ""),
            "chat_id": str(data.get("chat_id") or ""),
        }

    async def reply_card(self, message_id: str, card: dict[str, Any]) -> dict[str, Any]:
        """回在原消息的话题下，让告警和结论在飞书里连成一串。"""
        token = await self._tenant_token()
        payload = await self._post(
            f"/open-apis/im/v1/messages/{message_id}/reply",
            json_body={
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            token=token,
        )
        data = payload.get("data") or {}
        return {"message_id": str(data.get("message_id") or "")}

    async def reply_text(self, message_id: str, text: str) -> dict[str, Any]:
        token = await self._tenant_token()
        payload = await self._post(
            f"/open-apis/im/v1/messages/{message_id}/reply",
            json_body={
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            token=token,
        )
        data = payload.get("data") or {}
        return {"message_id": str(data.get("message_id") or "")}

    async def add_reaction(self, message_id: str, emoji_type: str = "OK") -> bool:
        """回执表情。失败只记 False——礼貌动作不该挡住真正的诊断。"""
        try:
            token = await self._tenant_token()
            await self._post(
                f"/open-apis/im/v1/messages/{message_id}/reactions",
                json_body={"reaction_type": {"emoji_type": emoji_type}},
                token=token,
            )
            return True
        except FeishuBotError:
            return False
        except Exception:
            return False
