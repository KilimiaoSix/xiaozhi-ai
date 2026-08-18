from copy import deepcopy
from datetime import datetime, timezone
import json
from unittest.mock import ANY

import pytest
from aiohttp import web

from core.api.presence_handler import MAX_PAYLOAD_BYTES, PresenceHandler
from core.presence_registry import PresenceRegistry
from core.presence_routes import add_presence_routes


NOW = datetime(2026, 8, 18, 1, 10, 30, tzinfo=timezone.utc)


@pytest.fixture
def payload():
    return {
        "schema_version": "1.0",
        "event_id": "6c618629-ffef-4c00-ab4f-17dc5ce2eb7a",
        "agent_instance_id": "45912c0c-144b-4ac7-970b-527add7b4dcc",
        "workstation_id": "desk-test",
        "source": "camera_pose",
        "state": "present",
        "previous_state": "starting",
        "changed": True,
        "reason": "pose_confirmed",
        "sequence": 1,
        "observed_at": "2026-08-18T01:10:30.000Z",
        "metrics": {},
    }


async def make_client(aiohttp_client, *, auth=False):
    config = {
        "server": {
            "auth": {"enabled": auth},
            "auth_key": "test-secret" if auth else "",
        }
    }
    app = web.Application()
    handler = PresenceHandler(
        config,
        PresenceRegistry(),
        now_provider=lambda: NOW,
    )
    add_presence_routes(app, handler)
    return await aiohttp_client(app)


@pytest.mark.asyncio
async def test_report_then_query_returns_normalized_envelope(aiohttp_client, payload):
    client = await make_client(aiohttp_client)

    report_response = await client.post("/xiaozhi/presence/report", json=payload)
    query_response = await client.get("/xiaozhi/presence/desk-test")

    assert report_response.status == 200
    assert await report_response.json() == {
        "code": "OK",
        "message": "success",
        "data": {
            "accepted": True,
            "duplicate": False,
            "workstation_id": "desk-test",
            "sequence": 1,
            "received_at": ANY,
        },
    }
    assert query_response.status == 200
    body = await query_response.json()
    assert body["code"] == "OK"
    assert body["message"] == "success"
    assert body["data"]["effective_state"] == "present"
    assert body["data"]["reported_state"] == "present"


@pytest.mark.asyncio
async def test_latest_event_retry_is_idempotent(aiohttp_client, payload):
    client = await make_client(aiohttp_client)
    await client.post("/xiaozhi/presence/report", json=payload)

    response = await client.post("/xiaozhi/presence/report", json=payload)

    assert response.status == 200
    assert (await response.json())["data"]["duplicate"] is True


@pytest.mark.asyncio
async def test_invalid_json_and_non_object_have_distinct_validation_errors(
    aiohttp_client,
):
    client = await make_client(aiohttp_client)

    malformed = await client.post(
        "/xiaozhi/presence/report",
        data=b"{",
        headers={"Content-Type": "application/json"},
    )
    non_object = await client.post("/xiaozhi/presence/report", json=[])

    assert malformed.status == 400
    assert (await malformed.json())["code"] == "INVALID_JSON"
    assert non_object.status == 400
    assert (await non_object.json())["code"] == "PRESENCE_INVALID_REQUEST"


@pytest.mark.asyncio
async def test_invalid_field_returns_400(aiohttp_client, payload):
    client = await make_client(aiohttp_client)
    payload["state"] = "stale"

    response = await client.post("/xiaozhi/presence/report", json=payload)

    assert response.status == 400
    body = await response.json()
    assert body["code"] == "PRESENCE_INVALID_REQUEST"
    assert "state" in body["message"]


@pytest.mark.asyncio
async def test_authentication_uses_bearer_token(aiohttp_client, payload):
    client = await make_client(aiohttp_client, auth=True)

    missing = await client.post("/xiaozhi/presence/report", json=payload)
    accepted = await client.post(
        "/xiaozhi/presence/report",
        json=payload,
        headers={"Authorization": "Bearer test-secret"},
    )

    assert missing.status == 401
    assert (await missing.json())["code"] == "UNAUTHORIZED"
    assert accepted.status == 200


@pytest.mark.asyncio
async def test_payload_larger_than_16_kib_is_rejected(aiohttp_client):
    client = await make_client(aiohttp_client)
    body = json.dumps({"padding": "x" * MAX_PAYLOAD_BYTES}).encode()

    response = await client.post(
        "/xiaozhi/presence/report",
        data=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status == 413
    assert (await response.json())["code"] == "PAYLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_out_of_order_report_returns_409(aiohttp_client, payload):
    client = await make_client(aiohttp_client)
    await client.post("/xiaozhi/presence/report", json=payload)
    payload["event_id"] = "b98af960-9166-45f3-bfb4-2f9fa6b9938f"

    response = await client.post("/xiaozhi/presence/report", json=payload)

    assert response.status == 409
    assert (await response.json())["code"] == "PRESENCE_OUT_OF_ORDER"


@pytest.mark.asyncio
async def test_unknown_and_invalid_workstation_are_not_absent(aiohttp_client):
    client = await make_client(aiohttp_client)

    unknown = await client.get("/xiaozhi/presence/unknown")
    invalid = await client.get("/xiaozhi/presence/desk%20test")

    assert unknown.status == 404
    assert (await unknown.json())["code"] == "PRESENCE_NOT_FOUND"
    assert invalid.status == 400
    assert (await invalid.json())["code"] == "PRESENCE_INVALID_REQUEST"


@pytest.mark.asyncio
async def test_options_and_json_responses_include_cors(aiohttp_client, payload):
    client = await make_client(aiohttp_client)

    options = await client.options("/xiaozhi/presence/report")
    report = await client.post("/xiaozhi/presence/report", json=payload)

    assert options.status == 200
    assert options.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
    assert report.headers["Access-Control-Allow-Origin"] == "*"
