import pytest
from aiohttp import web

from core.alert_relay.feishu_bot import FeishuBot, FeishuBotError


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


async def make_bot(aiohttp_client, routes, *, clock=None, **options):
    app = web.Application()
    app.add_routes(routes)
    test_client = await aiohttp_client(app)
    bot = FeishuBot(
        base_url=str(test_client.make_url("")).rstrip("/"),
        app_id="cli_test",
        app_secret="secret",
        session=test_client.session,
        clock=clock or Clock(),
        **options,
    )
    return bot, test_client


def token_route(calls, *, expire=7200):
    async def handler(request):
        body = await request.json()
        calls.append(body)
        return web.json_response(
            {"code": 0, "msg": "ok", "tenant_access_token": f"t-{len(calls)}", "expire": expire}
        )

    return web.post("/open-apis/auth/v3/tenant_access_token/internal", handler)


@pytest.mark.asyncio
async def test_send_card_posts_the_card_with_a_bot_token(aiohttp_client):
    token_calls, send_calls = [], []

    async def send(request):
        send_calls.append(
            (await request.json(), request.headers.get("Authorization"), dict(request.query))
        )
        return web.json_response(
            {"code": 0, "data": {"message_id": "om_1", "chat_id": "oc_1"}}
        )

    bot, _ = await make_bot(
        aiohttp_client,
        [token_route(token_calls), web.post("/open-apis/im/v1/messages", send)],
    )
    result = await bot.send_card("ou_receiver", {"header": {}, "elements": []})

    assert result["message_id"] == "om_1"
    assert result["chat_id"] == "oc_1"
    body, authorization, query = send_calls[0]
    assert authorization == "Bearer t-1"
    assert query["receive_id_type"] == "open_id"
    assert body["receive_id"] == "ou_receiver"
    assert body["msg_type"] == "interactive"
    # content 必须是 JSON 字符串，直接塞 dict 会被接口拒掉
    assert isinstance(body["content"], str)


@pytest.mark.asyncio
async def test_receive_id_type_follows_the_id_shape(aiohttp_client):
    """群 id 以 oc_ 开头，用 open_id 去发会报 receive_id 无效。"""
    token_calls, queries = [], []

    async def send(request):
        queries.append(dict(request.query))
        return web.json_response({"code": 0, "data": {"message_id": "om_1"}})

    bot, _ = await make_bot(
        aiohttp_client,
        [token_route(token_calls), web.post("/open-apis/im/v1/messages", send)],
    )
    await bot.send_card("oc_group", {})
    await bot.send_card("ou_person", {})
    await bot.send_card("user@iflytek.com", {})
    assert [q["receive_id_type"] for q in queries] == ["chat_id", "open_id", "email"]


@pytest.mark.asyncio
async def test_token_is_reused_until_it_nears_expiry(aiohttp_client):
    token_calls = []
    clock = Clock(1000.0)

    async def send(request):
        return web.json_response({"code": 0, "data": {"message_id": "om_1"}})

    bot, _ = await make_bot(
        aiohttp_client,
        [token_route(token_calls, expire=7200), web.post("/open-apis/im/v1/messages", send)],
        clock=clock,
    )
    await bot.send_card("ou_a", {})
    clock.now += 3600
    await bot.send_card("ou_a", {})
    assert len(token_calls) == 1

    # 提前量之内就该重新申请，别等真过期后拿 401
    clock.now += 3600
    await bot.send_card("ou_a", {})
    assert len(token_calls) == 2


@pytest.mark.asyncio
async def test_reply_threads_under_the_original_message(aiohttp_client):
    token_calls, paths = [], []

    async def reply(request):
        paths.append(request.path)
        return web.json_response({"code": 0, "data": {"message_id": "om_reply"}})

    bot, _ = await make_bot(
        aiohttp_client,
        [
            token_route(token_calls),
            web.post("/open-apis/im/v1/messages/{message_id}/reply", reply),
        ],
    )
    result = await bot.reply_card("om_root", {"elements": []})
    assert result["message_id"] == "om_reply"
    assert paths == ["/open-apis/im/v1/messages/om_root/reply"]


@pytest.mark.asyncio
async def test_reaction_is_the_receipt_for_a_human_reply(aiohttp_client):
    """skill 的回执约定：被叫到先立刻回一个表情，再慢慢查。"""
    token_calls, bodies = [], []

    async def react(request):
        bodies.append(await request.json())
        return web.json_response({"code": 0, "data": {}})

    bot, _ = await make_bot(
        aiohttp_client,
        [
            token_route(token_calls),
            web.post("/open-apis/im/v1/messages/{message_id}/reactions", react),
        ],
    )
    assert await bot.add_reaction("om_1") is True
    assert bodies[0]["reaction_type"]["emoji_type"] == "OK"


@pytest.mark.asyncio
async def test_reaction_failure_never_breaks_the_flow(aiohttp_client):
    """回执只是礼貌，挂了也不能挡住真正的诊断。"""
    token_calls = []

    async def react(request):
        return web.json_response({"code": 232002, "msg": "reaction exists"})

    bot, _ = await make_bot(
        aiohttp_client,
        [
            token_route(token_calls),
            web.post("/open-apis/im/v1/messages/{message_id}/reactions", react),
        ],
    )
    assert await bot.add_reaction("om_1") is False


@pytest.mark.asyncio
async def test_api_errors_surface_with_endpoint_and_code(aiohttp_client):
    token_calls = []

    async def send(request):
        return web.json_response({"code": 230002, "msg": "bot is not in the chat"})

    bot, _ = await make_bot(
        aiohttp_client,
        [token_route(token_calls), web.post("/open-apis/im/v1/messages", send)],
    )
    with pytest.raises(FeishuBotError) as excinfo:
        await bot.send_card("oc_group", {})
    assert "230002" in str(excinfo.value)
    assert "bot is not in the chat" in str(excinfo.value)


@pytest.mark.asyncio
async def test_secrets_never_leak_into_error_messages(aiohttp_client):
    async def token(request):
        return web.json_response({"code": 10003, "msg": "invalid app_secret: secret"})

    bot, _ = await make_bot(
        aiohttp_client,
        [web.post("/open-apis/auth/v3/tenant_access_token/internal", token)],
    )
    with pytest.raises(FeishuBotError) as excinfo:
        await bot.send_card("ou_a", {})
    assert "secret" not in str(excinfo.value).replace("app_secret", "")


def test_configured_flag_reports_missing_credentials():
    assert FeishuBot("https://open.feishu.cn", "", "", ).configured() is False
    assert FeishuBot("https://open.feishu.cn", "cli_a", "s").configured() is True
