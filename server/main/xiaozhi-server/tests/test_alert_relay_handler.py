import json

import pytest
from aiohttp import web

from core.alert_relay_routes import add_alert_relay_routes
from core.api.alert_relay_handler import AlertRelayHandler


class FakeService:
    def __init__(self):
        self.ingested = []
        self.replies = []
        self.ingest_result = {
            "code": "OK",
            "alert_id": "a-1",
            "state": "AWAITING_REPLY",
            "deduped": False,
        }
        self.reply_result = {"code": "OK", "alert_id": "a-1", "state": "CLAIMED"}
        self.records = {"a-1": {"alert_id": "a-1", "state": "AWAITING_REPLY"}}

    async def ingest(self, payload):
        self.ingested.append(payload)
        return self.ingest_result

    async def handle_reply(self, **kwargs):
        self.replies.append(kwargs)
        return self.reply_result

    def get(self, alert_id):
        return self.records.get(alert_id)

    def recent(self, limit=20):
        return list(self.records.values())

    def health(self):
        return {"enabled": True, "receive_id_configured": True}


async def make_client(aiohttp_client, *, auth=False, enabled=True, token="", service=None):
    config = {
        "server": {
            "auth": {"enabled": auth},
            "auth_key": "test-secret" if auth else "",
        },
        "alert_relay": {"enabled": enabled, "feishu": {"verification_token": token}},
    }
    app = web.Application()
    current = service or FakeService()
    add_alert_relay_routes(app, AlertRelayHandler(config, current))
    return await aiohttp_client(app), current


@pytest.mark.asyncio
async def test_ingest_accepts_an_alert_and_returns_its_state(aiohttp_client):
    client, service = await make_client(aiohttp_client)
    response = await client.post("/xiaozhi/alert/ingest", json={"raw_text": "告警集群：x"})
    assert response.status == 200
    body = await response.json()
    assert body["code"] == "OK"
    assert body["data"]["alert_id"] == "a-1"
    assert service.ingested[0]["raw_text"] == "告警集群：x"


@pytest.mark.asyncio
async def test_ingest_rejects_a_bad_body(aiohttp_client):
    client, _ = await make_client(aiohttp_client)
    response = await client.post(
        "/xiaozhi/alert/ingest", data="not json", headers={"Content-Type": "application/json"}
    )
    assert response.status == 400
    assert (await response.json())["code"] == "INVALID_JSON"


@pytest.mark.asyncio
async def test_ingest_maps_service_rejections_to_4xx(aiohttp_client):
    service = FakeService()
    service.ingest_result = {"code": "INVALID_REQUEST", "message": "raw_text 必填"}
    client, _ = await make_client(aiohttp_client, service=service)
    response = await client.post("/xiaozhi/alert/ingest", json={})
    assert response.status == 400


@pytest.mark.asyncio
async def test_disabled_relay_answers_503(aiohttp_client):
    client, _ = await make_client(aiohttp_client, enabled=False)
    response = await client.post("/xiaozhi/alert/ingest", json={"raw_text": "x"})
    assert response.status == 503
    assert (await response.json())["code"] == "ALERT_RELAY_DISABLED"


@pytest.mark.asyncio
async def test_ingest_requires_the_bearer_token_when_auth_is_on(aiohttp_client):
    client, _ = await make_client(aiohttp_client, auth=True)
    assert (await client.post("/xiaozhi/alert/ingest", json={"raw_text": "x"})).status == 401
    ok = await client.post(
        "/xiaozhi/alert/ingest",
        json={"raw_text": "x"},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert ok.status == 200


@pytest.mark.asyncio
async def test_url_verification_challenge_is_echoed(aiohttp_client):
    """飞书配置事件订阅时先来这一发，答不上来订阅就配不上。"""
    client, _ = await make_client(aiohttp_client)
    response = await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={"type": "url_verification", "challenge": "abc123", "token": ""},
    )
    assert response.status == 200
    assert (await response.json())["challenge"] == "abc123"


@pytest.mark.asyncio
async def test_callback_verifies_the_token_when_configured(aiohttp_client):
    client, _ = await make_client(aiohttp_client, token="v-token")
    bad = await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={"type": "url_verification", "challenge": "abc", "token": "wrong"},
    )
    assert bad.status == 401
    good = await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={"type": "url_verification", "challenge": "abc", "token": "v-token"},
    )
    assert good.status == 200


@pytest.mark.asyncio
async def test_card_button_callback_reaches_the_service_with_intent(aiohttp_client):
    client, service = await make_client(aiohttp_client)
    response = await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={
            "open_id": "ou_zhangsan",
            "open_message_id": "om_1",
            "action": {"tag": "button", "value": {"alert_id": "a-1", "intent": "diagnose"}},
        },
    )
    assert response.status == 200
    assert service.replies[0]["alert_id"] == "a-1"
    assert service.replies[0]["intent"] == "diagnose"
    # 卡片回调要给用户一个即时反馈，否则按钮像是没反应
    assert "toast" in await response.json()


@pytest.mark.asyncio
async def test_card_action_trigger_v2_shape_is_supported(aiohttp_client):
    client, service = await make_client(aiohttp_client)
    await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={
            "schema": "2.0",
            "header": {"event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_lisi"},
                "action": {"value": {"alert_id": "a-1", "intent": "decline"}},
                "context": {"open_message_id": "om_1"},
            },
        },
    )
    assert service.replies[0]["intent"] == "decline"
    assert service.replies[0]["user"] == "ou_lisi"


@pytest.mark.asyncio
async def test_plain_text_reply_event_is_routed_by_root_message(aiohttp_client):
    client, service = await make_client(aiohttp_client)
    await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_zhangsan"}},
                "message": {
                    "message_id": "om_reply",
                    "root_id": "om_1",
                    "chat_id": "oc_1",
                    "message_type": "text",
                    "content": json.dumps({"text": "帮我查"}, ensure_ascii=False),
                },
            },
        },
    )
    call = service.replies[0]
    assert call["root_message_id"] == "om_1"
    assert call["text"] == "帮我查"
    assert call["message_id"] == "om_reply"


@pytest.mark.asyncio
async def test_at_mention_placeholders_are_stripped_from_the_reply_text(aiohttp_client):
    client, service = await make_client(aiohttp_client)
    await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_a"}},
                "message": {
                    "message_id": "om_reply",
                    "parent_id": "om_1",
                    "message_type": "text",
                    "content": json.dumps({"text": "@_user_1 帮我查"}, ensure_ascii=False),
                },
            },
        },
    )
    assert service.replies[0]["text"] == "帮我查"
    # root_id 缺失时回落到 parent_id，否则线程内的直接回复认不出来
    assert service.replies[0]["root_message_id"] == "om_1"


@pytest.mark.asyncio
async def test_messages_without_a_thread_are_ignored_quietly(aiohttp_client):
    """群里的闲聊不该被当成对告警的回复。"""
    client, service = await make_client(aiohttp_client)
    response = await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_a"}},
                "message": {"message_id": "om_x", "message_type": "text",
                            "content": json.dumps({"text": "今天午饭吃啥"})},
            },
        },
    )
    assert response.status == 200
    assert service.replies == []


@pytest.mark.asyncio
async def test_non_text_messages_are_ignored(aiohttp_client):
    client, service = await make_client(aiohttp_client)
    await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_a"}},
                "message": {"message_id": "om_x", "root_id": "om_1",
                            "message_type": "image", "content": "{}"},
            },
        },
    )
    assert service.replies == []


@pytest.mark.asyncio
async def test_encrypted_events_fail_loudly_instead_of_silently_dropping(aiohttp_client):
    """本中继不解密；配错了要一眼看出来，而不是回调石沉大海。"""
    client, _ = await make_client(aiohttp_client)
    response = await client.post(
        "/xiaozhi/alert/feishu/callback", json={"encrypt": "AbCdEf=="}
    )
    assert response.status == 400
    body = await response.json()
    assert body["code"] == "ENCRYPTED_EVENT_UNSUPPORTED"
    assert "加密" in body["message"]


@pytest.mark.asyncio
async def test_callback_always_answers_200_for_unknown_events(aiohttp_client):
    """飞书对非 200 会重推，未知事件必须温和吞掉。"""
    client, _ = await make_client(aiohttp_client)
    response = await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={"schema": "2.0", "header": {"event_type": "im.chat.updated_v1"}, "event": {}},
    )
    assert response.status == 200


@pytest.mark.asyncio
async def test_status_and_health_endpoints(aiohttp_client):
    client, _ = await make_client(aiohttp_client)
    detail = await client.get("/xiaozhi/alert/a-1")
    assert (await detail.json())["data"]["state"] == "AWAITING_REPLY"

    missing = await client.get("/xiaozhi/alert/nope")
    assert missing.status == 404

    health = await client.get("/xiaozhi/alert/health")
    assert health.status == 200
    assert (await health.json())["data"]["enabled"] is True


@pytest.mark.asyncio
async def test_health_reports_disabled_without_pretending_to_be_ready(aiohttp_client):
    client, _ = await make_client(aiohttp_client, enabled=False)
    health = await client.get("/xiaozhi/alert/health")
    assert health.status == 200
    body = await health.json()
    assert body["data"]["enabled"] is False
    assert body["data"]["status"] == "DISABLED"


@pytest.mark.asyncio
async def test_recent_listing_is_available_for_debugging(aiohttp_client):
    client, _ = await make_client(aiohttp_client)
    response = await client.get("/xiaozhi/alert/recent")
    assert response.status == 200
    assert isinstance((await response.json())["data"]["alerts"], list)


@pytest.mark.asyncio
async def test_health_and_recent_are_not_shadowed_by_the_id_route(aiohttp_client):
    """/xiaozhi/alert/health 必须命中健康检查，而不是被当成 alert_id=health。"""
    client, service = await make_client(aiohttp_client)
    service.records["health"] = {"alert_id": "health", "state": "X"}
    body = await (await client.get("/xiaozhi/alert/health")).json()
    assert "enabled" in body["data"]
