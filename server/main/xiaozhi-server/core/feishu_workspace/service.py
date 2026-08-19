"""聚合飞书 OpenAPI 的今日日程与未完成任务。"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from core.morning_brief.models import normalize_calendar_event, resolve_timezone
from core.morning_brief.feishu_client import (
    CALENDAR_SCOPES,
    TASK_SCOPES,
    TASKLIST_SCOPES,
    FeishuApiError,
)


WORKSPACE_SCOPES = (
    *CALENDAR_SCOPES,
    *TASK_SCOPES,
    *TASKLIST_SCOPES,
)


class FeishuWorkspaceUnavailable(RuntimeError):
    """任务与日历两个主数据源均不可用。"""


def _text(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _timestamp(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    try:
        numeric = int(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    seconds = numeric / 1000 if numeric > 10_000_000_000 else numeric
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


class FeishuWorkspaceService:
    def __init__(
        self,
        client,
        self_open_id: str,
        *,
        timezone_name: str = "Asia/Shanghai",
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.self_open_id = str(self_open_id or "")
        self.timezone_name = timezone_name
        self.timezone = resolve_timezone(timezone_name)
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def status(self, missing_scopes: list[str] | None = None) -> dict[str, Any]:
        configured = bool(
            self.client.capabilities().get("user_token_configured")
        )
        return {
            "state": "ready" if configured else "configuration_required",
            "message": (
                "Server 已配置飞书用户凭据。"
                if configured
                else "Server 尚未配置 FEISHU_USER_ACCESS_TOKEN。"
            ),
            "open_id": self.self_open_id or None,
            "source": "server_openapi",
            "required_scopes": list(WORKSPACE_SCOPES),
            "missing_scopes": list(missing_scopes or []),
        }

    @staticmethod
    def _warning(label: str, error: BaseException, scopes: tuple[str, ...]) -> str:
        if isinstance(error, FeishuApiError) and str(error.code) in {
            "99991663",
            "99991672",
        }:
            return f"{label}缺少飞书权限：{'、'.join(scopes)}"
        return f"{label}读取失败：{error}"

    async def get_briefing(
        self, report_date: date | None = None
    ) -> dict[str, Any]:
        current_date = report_date or self.now_provider().astimezone(
            self.timezone
        ).date()
        day_start = datetime.combine(current_date, time.min, tzinfo=self.timezone)
        day_end = day_start + timedelta(days=1)
        task_result, calendar_result = await asyncio.gather(
            self.client.list_tasks(completed=False),
            self.client.list_calendar_events(day_start, day_end),
            return_exceptions=True,
        )

        warnings: list[str] = []
        missing_scopes: list[str] = []
        if isinstance(task_result, BaseException):
            warnings.append(
                self._warning("未完成任务", task_result, TASK_SCOPES)
            )
            if isinstance(task_result, FeishuApiError) and str(task_result.code) in {
                "99991663",
                "99991672",
            }:
                missing_scopes.extend(TASK_SCOPES)
        if isinstance(calendar_result, BaseException):
            warnings.append(
                self._warning(
                    "今日日程",
                    calendar_result,
                    CALENDAR_SCOPES,
                )
            )
            if isinstance(calendar_result, FeishuApiError) and str(
                calendar_result.code
            ) in {"99991663", "99991672"}:
                missing_scopes.extend(CALENDAR_SCOPES)
        if isinstance(task_result, BaseException) and isinstance(
            calendar_result, BaseException
        ):
            raise FeishuWorkspaceUnavailable("；".join(warnings))

        tasklist_guids = []
        task_items = () if isinstance(task_result, BaseException) else task_result.items
        calendar_items = (
            () if isinstance(calendar_result, BaseException) else calendar_result.items
        )
        for task in task_items:
            for entry in task.get("tasklists", []):
                if isinstance(entry, dict) and entry.get("tasklist_guid"):
                    guid = str(entry["tasklist_guid"])
                    if guid not in tasklist_guids:
                        tasklist_guids.append(guid)
        tasklist_limit = asyncio.Semaphore(10)

        async def load_tasklist(guid: str):
            async with tasklist_limit:
                return await self.client.get_tasklist(guid)

        tasklists = await asyncio.gather(
            *(load_tasklist(guid) for guid in tasklist_guids),
            return_exceptions=True,
        )
        tasklist_names = {
            guid: _text(value.get("name"))
            for guid, value in zip(tasklist_guids, tasklists)
            if isinstance(value, dict)
        }
        tasklist_errors = [
            value for value in tasklists if isinstance(value, BaseException)
        ]
        if tasklist_errors:
            warnings.append(
                self._warning(
                    "任务清单名称",
                    tasklist_errors[0],
                    TASKLIST_SCOPES,
                )
            )
            if any(
                isinstance(error, FeishuApiError)
                and str(error.code) in {"99991663", "99991672"}
                for error in tasklist_errors
            ):
                missing_scopes.extend(TASKLIST_SCOPES)

        tasks = []
        for index, task in enumerate(task_items):
            due = task.get("due") if isinstance(task.get("due"), dict) else {}
            project_name = ""
            for entry in task.get("tasklists", []):
                if not isinstance(entry, dict):
                    continue
                project_name = tasklist_names.get(
                    str(entry.get("tasklist_guid") or ""), ""
                )
                if project_name:
                    break
            tasks.append(
                {
                    "id": _text(task.get("guid"), task.get("task_id"))
                    or f"task-{index}",
                    "title": _text(task.get("summary")) or "未命名任务",
                    "due": _timestamp(due.get("timestamp")),
                    "all_day": due.get("is_all_day") is True,
                    "url": _text(task.get("url")) or None,
                    "project_name": project_name or None,
                }
            )

        meetings = []
        for raw in calendar_items:
            item = normalize_calendar_event(raw, self.timezone_name)
            meetings.append(
                {
                    "id": item.event_id,
                    "title": item.summary,
                    "start": item.start.astimezone(timezone.utc).isoformat(),
                    "end": item.end.astimezone(timezone.utc).isoformat(),
                    "all_day": bool(
                        isinstance(raw.get("start_time"), dict)
                        and raw["start_time"].get("date")
                    ),
                    "url": item.source_url or None,
                }
            )
        meetings.sort(key=lambda item: item["start"])

        return {
            "status": self.status(missing_scopes),
            "date": current_date.isoformat(),
            "meetings": meetings,
            "tasks": tasks,
            "warnings": warnings,
            "fetched_at": self.now_provider().isoformat(),
        }
