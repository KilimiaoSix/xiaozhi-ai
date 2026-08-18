import asyncio
from datetime import datetime, timezone

import pytest
from aiohttp import ClientSession

from presence_agent.reporter import HttpPresenceTransport
from presence_server import create_presence_app


@pytest.mark.asyncio
async def test_real_transport_reports_to_shared_server_and_can_be_queried(aiohttp_server):
    config = {"server": {"auth": {"enabled": False}, "auth_key": ""}}
    server = await aiohttp_server(create_presence_app(config))
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    payload = {
        "schema_version": "1.0",
        "event_id": "6c618629-ffef-4c00-ab4f-17dc5ce2eb7a",
        "agent_instance_id": "45912c0c-144b-4ac7-970b-527add7b4dcc",
        "workstation_id": "desk-integration",
        "source": "camera_pose",
        "state": "present",
        "previous_state": "starting",
        "changed": True,
        "reason": "pose_confirmed",
        "sequence": 1,
        "observed_at": now,
        "metrics": {
            "visible_core_landmarks": 4,
            "has_visible_shoulder": True,
            "positive_streak": 3,
            "seconds_since_last_positive": 0.0,
        },
    }
    transport = HttpPresenceTransport(str(server.make_url("")))

    result = await asyncio.to_thread(transport.send, payload)
    async with ClientSession() as session:
        async with session.get(
            server.make_url("/xiaozhi/presence/desk-integration")
        ) as response:
            queried = await response.json()

    assert result["code"] == "OK"
    assert queried["data"]["effective_state"] == "present"
    assert "frame" not in payload
    assert "image" not in payload
    assert "landmarks" not in payload
    assert "landmarks" not in payload["metrics"]
