from datetime import datetime, timezone

import pytest
from aiohttp import web

from core.morning_brief.xfchat_client import (
    AuthenticationRequired,
    XfChatApiError,
    XfChatClient,
)


START = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc)


def message(message_id):
    return {
        "message_id": message_id,
        "chat_id": "oc_1",
        "create_time": "1787014800000",
        "content": '{"text":"请确认"}',
    }


async def make_xfchat_client(aiohttp_client, routes, **options):
    app = web.Application()
    app.add_routes(routes)
    test_client = await aiohttp_client(app)
    client = XfChatClient(
        base_url=str(test_client.make_url("")).rstrip("/"),
        user_access_token="u-secret-token",
        session=test_client.session,
        **options,
    )
    return client, test_client


@pytest.mark.asyncio
async def test_search_consumes_every_page_and_sends_user_token(aiohttp_client):
    requests = []

    async def search(request):
        body = await request.json()
        requests.append((body, request.headers.get("Authorization")))
        if body.get("page_token") == "page-2":
            return web.json_response(
                {"code": 0, "data": {"items": [message("om_2")], "has_more": False}}
            )
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "items": [message("om_1")],
                    "has_more": True,
                    "page_token": "page-2",
                },
            }
        )

    client, _ = await make_xfchat_client(
        aiohttp_client,
        [web.post("/open-apis/search/v2/message", search)],
    )

    result = await client.search_messages(
        START, END, at_chatter_ids=["ou_me"]
    )

    assert result.complete is True
    assert result.pages == 2
    assert [item["message_id"] for item in result.items] == ["om_1", "om_2"]
    assert requests == [
        (
            {
                "query": "",
                "start_time": "1786960800",
                "end_time": "1787014800",
                "page_size": 50,
                "at_chatter_ids": ["ou_me"],
            },
            "Bearer u-secret-token",
        ),
        (
            {
                "query": "",
                "start_time": "1786960800",
                "end_time": "1787014800",
                "page_size": 50,
                "at_chatter_ids": ["ou_me"],
                "page_token": "page-2",
            },
            "Bearer u-secret-token",
        ),
    ]


@pytest.mark.asyncio
async def test_search_marks_page_limit_as_incomplete(aiohttp_client):
    async def search(request):
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "items": [message("om_1")],
                    "has_more": True,
                    "page_token": "next-page",
                },
            }
        )

    client, _ = await make_xfchat_client(
        aiohttp_client,
        [web.post("/open-apis/search/v2/message", search)],
        max_pages=1,
    )

    result = await client.search_messages(START, END)

    assert result.complete is False
    assert result.pages == 1
    assert result.next_page_token == "next-page"
    assert result.error == "page limit reached before pagination completed"


@pytest.mark.asyncio
async def test_page_limited_results_are_still_enriched(aiohttp_client):
    """截断的结果同样要补详情，否则只有 message_id 的消息全部归一化失败。"""
    requested_ids = []

    async def search(request):
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "items": ["om_1"],
                    "has_more": True,
                    "page_token": "next-page",
                },
            }
        )

    async def mget(request):
        requested_ids.extend(request.query.getall("message_ids"))
        return web.json_response(
            {"code": 0, "data": {"messages": [message("om_1")]}}
        )

    client, _ = await make_xfchat_client(
        aiohttp_client,
        [
            web.post("/open-apis/search/v2/message", search),
            web.get("/open-apis/im/v1/messages/mget", mget),
        ],
        max_pages=1,
    )

    result = await client.search_messages(START, END)

    assert result.complete is False
    assert requested_ids == ["om_1"]
    assert result.items[0]["content"] == '{"text":"请确认"}'


@pytest.mark.asyncio
async def test_id_only_search_results_are_enriched_with_mget(aiohttp_client):
    requested_ids = []

    async def search(request):
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "message_id": "om_1",
                            "chat_name": "研发群",
                            "mentions": [{"id": "ou_me"}],
                        }
                    ],
                    "has_more": False,
                },
            }
        )

    async def mget(request):
        requested_ids.extend(request.query.getall("message_ids"))
        return web.json_response(
            {"code": 0, "data": {"messages": [message("om_1")]}}
        )

    client, _ = await make_xfchat_client(
        aiohttp_client,
        [
            web.post("/open-apis/search/v2/message", search),
            web.get("/open-apis/im/v1/messages/mget", mget),
        ],
    )

    result = await client.search_messages(START, END)

    assert result.complete is True
    assert requested_ids == ["om_1"]
    assert result.items[0]["content"] == '{"text":"请确认"}'
    assert result.items[0]["chat_name"] == "研发群"
    assert result.items[0]["mentions"] == [{"id": "ou_me"}]


@pytest.mark.asyncio
async def test_calendar_uses_primary_calendar_and_instance_view(aiohttp_client):
    calls = []

    async def primary(request):
        calls.append((request.method, request.path, dict(request.query)))
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "calendars": [{"calendar": {"calendar_id": "cal_a"}}]
                },
            }
        )

    async def instances(request):
        calls.append((request.method, request.path, dict(request.query)))
        return web.json_response(
            {"code": 0, "data": {"items": [{"event_id": "event_1"}]}}
        )

    client, _ = await make_xfchat_client(
        aiohttp_client,
        [
            web.post("/open-apis/calendar/v4/calendars/primary", primary),
            web.get(
                "/open-apis/calendar/v4/calendars/{calendar_id}/events/instance_view",
                instances,
            ),
        ],
    )

    result = await client.list_calendar_events(START, END)

    assert result.complete is True
    assert result.items == ({"event_id": "event_1"},)
    assert calls == [
        ("POST", "/open-apis/calendar/v4/calendars/primary", {}),
        (
            "GET",
            "/open-apis/calendar/v4/calendars/cal_a/events/instance_view",
            {"start_time": "1786960800", "end_time": "1787014800"},
        ),
    ]


@pytest.mark.asyncio
async def test_api_error_is_diagnostic_without_leaking_token(aiohttp_client):
    async def search(request):
        return web.json_response(
            {
                "code": 99991663,
                "msg": "permission denied for u-secret-token",
            },
            status=403,
        )

    client, _ = await make_xfchat_client(
        aiohttp_client,
        [web.post("/open-apis/search/v2/message", search)],
    )

    with pytest.raises(XfChatApiError) as captured:
        await client.search_messages(START, END)

    assert captured.value.code == 99991663
    assert captured.value.http_status == 403
    assert "permission denied" in str(captured.value)
    assert "u-secret-token" not in str(captured.value)


@pytest.mark.asyncio
async def test_http_401_is_classified_as_reauthorization_required(aiohttp_client):
    async def search(request):
        return web.json_response(
            {"code": 99991661, "msg": "user token expired"}, status=401
        )

    client, _ = await make_xfchat_client(
        aiohttp_client,
        [web.post("/open-apis/search/v2/message", search)],
    )

    with pytest.raises(AuthenticationRequired, match="expired or invalid"):
        await client.search_messages(START, END)


@pytest.mark.asyncio
async def test_missing_user_token_stops_before_network_access():
    client = XfChatClient(
        base_url="https://open.xfchat.iflytek.com",
        user_access_token="",
    )

    with pytest.raises(AuthenticationRequired, match="user access token"):
        await client.search_messages(START, END)

    assert client.capabilities()["user_token_configured"] is False


def test_capabilities_expose_private_domain_scopes():
    client = XfChatClient(
        base_url="https://open.xfchat.iflytek.com",
        user_access_token="u-token",
    )

    assert client.capabilities()["required_scopes"] == [
        "search:message",
        "im:message:get_as_user",
        "calendar:calendar:readonly",
    ]
