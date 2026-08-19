"""飞书开放平台的只读 OpenAPI 客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import aiohttp


DEFAULT_BASE_URL = "https://open.feishu.cn"

# 搜索接口只返回 message_id，补详情走 im/v1/messages/mget。
# 用户令牌读消息详情除基础权限外还要按会话类型补 get_as_user，
# 单聊和群聊是两个独立权限，缺任意一个都会让对应会话的消息读不到。
MESSAGE_SCOPES = (
    "search:message",
    "im:message:readonly",
    "im:message.p2p_msg:get_as_user",
    "im:message.group_msg:get_as_user",
)
CALENDAR_SCOPES = ("calendar:calendar:readonly",)
TASK_SCOPES = ("task:task:read",)
TASKLIST_SCOPES = ("task:tasklist:read",)
REQUIRED_SCOPES = MESSAGE_SCOPES + CALENDAR_SCOPES


class AuthenticationRequired(RuntimeError):
    """没有可用于读取个人资源的用户令牌。"""


class FeishuApiError(RuntimeError):
    def __init__(self, endpoint: str, http_status: int, code: Any, message: str):
        self.endpoint = endpoint
        self.http_status = http_status
        self.code = code
        self.api_message = message
        super().__init__(
            f"飞书 OpenAPI {endpoint} failed: HTTP {http_status}, "
            f"code={code}, message={message}"
        )


@dataclass(frozen=True)
class CollectionResult:
    items: tuple[dict[str, Any], ...]
    pages: int
    complete: bool
    next_page_token: str = ""
    error: str = ""


class FeishuClient:
    def __init__(
        self,
        base_url: str,
        user_access_token: str,
        *,
        page_size: int = 50,
        max_pages: int = 40,
        timeout_seconds: float = 15,
        calendar_enabled: bool = True,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_access_token = user_access_token.strip()
        self.calendar_enabled = bool(calendar_enabled)
        self.page_size = max(1, min(int(page_size), 50))
        self.max_pages = max(1, int(max_pages))
        self.timeout_seconds = float(timeout_seconds)
        self._session = session

    def required_scopes(self) -> tuple[str, ...]:
        """只声明已启用数据源真正需要的权限。"""
        if self.calendar_enabled:
            return MESSAGE_SCOPES + CALENDAR_SCOPES
        return MESSAGE_SCOPES

    def capabilities(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "user_token_configured": bool(self.user_access_token),
            "required_scopes": list(self.required_scopes()),
            "calendar_enabled": self.calendar_enabled,
            "message_search": "/open-apis/search/v2/message",
            "calendar_instance_view": (
                "/open-apis/calendar/v4/calendars/{calendar_id}/events/instance_view"
                if self.calendar_enabled
                else None
            ),
            "client_unread_cursor_supported": False,
        }

    def _headers(self) -> dict[str, str]:
        if not self.user_access_token:
            raise AuthenticationRequired("飞书 user access token is not configured")
        return {
            "Authorization": f"Bearer {self.user_access_token}",
            "Accept": "application/json",
        }

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = self._headers()
        url = f"{self.base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)

        async def execute(session: aiohttp.ClientSession):
            async with session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout,
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    raise FeishuApiError(
                        path,
                        response.status,
                        "INVALID_RESPONSE",
                        "response was not valid JSON",
                    ) from exc

                code = payload.get("code", 0) if isinstance(payload, dict) else "INVALID_RESPONSE"
                if response.status >= 400 or code not in (0, "0", None):
                    if response.status == 401 or code in (99991661, "99991661"):
                        raise AuthenticationRequired(
                            f"飞书 user access token is expired or invalid for {path}"
                        )
                    message = "unknown OpenAPI error"
                    if isinstance(payload, dict):
                        message = str(payload.get("msg") or payload.get("message") or message)
                    if self.user_access_token:
                        message = message.replace(
                            self.user_access_token, "[REDACTED]"
                        )
                    raise FeishuApiError(path, response.status, code, message)
                if not isinstance(payload, dict):
                    raise FeishuApiError(
                        path, response.status, "INVALID_RESPONSE", "response was not an object"
                    )
                data = payload.get("data", {})
                return data if isinstance(data, dict) else {}

        if self._session is not None:
            return await execute(self._session)
        async with aiohttp.ClientSession() as session:
            return await execute(session)

    @staticmethod
    def _page_items(data: dict[str, Any]) -> list[dict[str, Any]]:
        raw_items = data.get("items")
        if raw_items is None:
            raw_items = data.get("messages")
        if raw_items is None:
            raw_items = data.get("message_ids", [])
        items: list[dict[str, Any]] = []
        for raw in raw_items if isinstance(raw_items, list) else []:
            if isinstance(raw, dict):
                items.append(raw)
            elif raw:
                items.append({"message_id": str(raw)})
        return items

    @staticmethod
    def _needs_detail(item: dict[str, Any]) -> bool:
        return not any(
            item.get(key) is not None
            for key in ("content", "body", "create_time", "timestamp")
        )

    async def _enrich_messages(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        missing_ids = [
            str(item.get("message_id", ""))
            for item in items
            if self._needs_detail(item) and item.get("message_id")
        ]
        if not missing_ids:
            return items

        details: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(missing_ids), 50):
            batch = missing_ids[offset : offset + 50]
            params = [("message_ids", message_id) for message_id in batch]
            data = await self._request_json(
                "GET", "/open-apis/im/v1/messages/mget", params=params
            )
            for detail in self._page_items(data):
                message_id = str(detail.get("message_id", ""))
                if message_id:
                    details[message_id] = detail
        enriched: list[dict[str, Any]] = []
        for item in items:
            detail = details.get(str(item.get("message_id", "")))
            enriched.append({**item, **detail} if detail else item)
        return enriched

    async def search_messages(
        self,
        start: datetime,
        end: datetime,
        *,
        at_chatter_ids: list[str] | None = None,
    ) -> CollectionResult:
        page_token = ""
        pages = 0
        items: list[dict[str, Any]] = []
        has_more = False

        while pages < self.max_pages:
            # 搜索接口的时间窗字段是 start_time/end_time，单位为秒。
            # 传错字段名不会报错，接口会静默忽略时间窗并返回全部历史消息。
            body: dict[str, Any] = {
                "query": "",
                "start_time": str(int(start.timestamp())),
                "end_time": str(int(end.timestamp())),
                "page_size": self.page_size,
            }
            if at_chatter_ids:
                body["at_chatter_ids"] = list(at_chatter_ids)
            if page_token:
                body["page_token"] = page_token

            data = await self._request_json(
                "POST", "/open-apis/search/v2/message", json_body=body
            )
            pages += 1
            items.extend(self._page_items(data))
            has_more = bool(data.get("has_more"))
            page_token = str(data.get("page_token") or "")
            if not has_more:
                enriched = await self._enrich_messages(items)
                return CollectionResult(tuple(enriched), pages, True)
            if not page_token:
                return CollectionResult(
                    tuple(items),
                    pages,
                    False,
                    error="pagination reported more data without a page token",
                )

        # 截断的结果同样要补详情：搜索接口只返回消息 ID，
        # 不补详情的话这批消息缺少内容和时间，后续会全部归一化失败。
        enriched = await self._enrich_messages(items)
        return CollectionResult(
            tuple(enriched),
            pages,
            False,
            next_page_token=page_token if has_more else "",
            error="page limit reached before pagination completed",
        )

    @staticmethod
    def _calendar_id(data: dict[str, Any]) -> str:
        direct = data.get("calendar_id")
        if direct:
            return str(direct)
        calendar = data.get("calendar")
        if isinstance(calendar, dict) and calendar.get("calendar_id"):
            return str(calendar["calendar_id"])
        calendars = data.get("calendars")
        if isinstance(calendars, list):
            for entry in calendars:
                current = entry.get("calendar", entry) if isinstance(entry, dict) else {}
                if isinstance(current, dict) and current.get("calendar_id"):
                    return str(current["calendar_id"])
        return ""

    async def list_calendar_events(
        self, start: datetime, end: datetime
    ) -> CollectionResult:
        primary = await self._request_json(
            "POST", "/open-apis/calendar/v4/calendars/primary"
        )
        calendar_id = self._calendar_id(primary)
        if not calendar_id:
            raise FeishuApiError(
                "/open-apis/calendar/v4/calendars/primary",
                200,
                "INVALID_RESPONSE",
                "primary calendar ID was missing",
            )
        encoded_id = quote(calendar_id, safe="")
        path = (
            f"/open-apis/calendar/v4/calendars/{encoded_id}/events/instance_view"
        )
        data = await self._request_json(
            "GET",
            path,
            params={
                "start_time": str(int(start.timestamp())),
                "end_time": str(int(end.timestamp())),
            },
        )
        return CollectionResult(tuple(self._page_items(data)), 1, True)

    async def list_tasks(self, *, completed: bool = False) -> CollectionResult:
        """列取当前用户负责的任务，按 OpenAPI page_token 拉完。"""
        page_token = ""
        pages = 0
        items: list[dict[str, Any]] = []
        has_more = False

        while pages < self.max_pages:
            params = {
                "page_size": 100,
                "completed": str(bool(completed)).lower(),
                "type": "my_tasks",
                "user_id_type": "open_id",
            }
            if page_token:
                params["page_token"] = page_token
            data = await self._request_json(
                "GET", "/open-apis/task/v2/tasks", params=params
            )
            pages += 1
            items.extend(self._page_items(data))
            has_more = bool(data.get("has_more"))
            page_token = str(data.get("page_token") or "")
            if not has_more:
                return CollectionResult(tuple(items), pages, True)
            if not page_token:
                return CollectionResult(
                    tuple(items),
                    pages,
                    False,
                    error="pagination reported more data without a page token",
                )

        return CollectionResult(
            tuple(items),
            pages,
            False,
            next_page_token=page_token if has_more else "",
            error="page limit reached before pagination completed",
        )

    async def get_tasklist(self, tasklist_guid: str) -> dict[str, Any]:
        encoded_guid = quote(str(tasklist_guid), safe="")
        data = await self._request_json(
            "GET",
            f"/open-apis/task/v2/tasklists/{encoded_guid}",
            params={"user_id_type": "open_id"},
        )
        tasklist = data.get("tasklist")
        return tasklist if isinstance(tasklist, dict) else {}
