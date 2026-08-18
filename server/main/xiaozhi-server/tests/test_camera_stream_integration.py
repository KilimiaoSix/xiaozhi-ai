from datetime import datetime, timezone

import pytest
from aiohttp import web

from core.api.camera_stream_handler import CameraStreamHandler
from core.api.presence_handler import PresenceHandler
from core.presence_registry import PresenceRegistry
from core.presence_routes import add_presence_routes


JPEG = b"\xff\xd8integration-frame\xff\xd9"
ENROLLMENT_SESSION = "6c618629-ffef-4c00-ab4f-17dc5ce2eb7a"
MONITORING_SESSION = "c0cfab2b-7a8f-43ee-b9d8-6985f03ee1ba"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def start_message(mode, session_id, **extra):
    message = {
        "type": "start",
        "schema_version": "1.0",
        "mode": mode,
        "session_id": session_id,
        "workstation_id": "desk-integration",
    }
    message.update(extra)
    return message


class EnrollmentPipeline:
    def __init__(self, state):
        self.state = state
        self.accepted = 0

    def process(self, jpeg, sequence, now):
        assert jpeg == JPEG
        self.accepted += 1
        if self.accepted < 20:
            return {
                "type": "enrollment_progress",
                "sequence": sequence,
                "accepted": self.accepted,
                "required": 20,
                "reason": "accepted",
            }
        self.state["template_enrolled"] = True
        return {
            "type": "enrollment_complete",
            "sequence": sequence,
            "profile_id": "owner",
            "sample_id": "sample-integration",
            "display_name": "主人",
            "stored_at": "2026-08-18T12:00:00Z",
            "sample_count": 18,
        }


class MonitoringPipeline:
    def __init__(self, state):
        assert state.get("template_enrolled") is True
        self.results = iter(
            [
                ("owner", "starting", 1, 0.82, True),
                ("unknown", "owner", 1, 0.21, False),
                ("no_face", "unknown", 0, None, False),
            ]
        )

    def process(self, jpeg, sequence, now):
        state, previous, face_count, similarity, matched = next(self.results)
        identity = {
            "state": state,
            "previous_state": previous,
            "changed": state != previous,
            "face_count": face_count,
            "face_detected": face_count > 0,
            "matched": matched,
        }
        if similarity is not None:
            identity.update(similarity=similarity, threshold=0.45)
        return {
            "sequence": sequence,
            "presence": {"state": "present", "changed": sequence == 1},
            "identity": identity,
            "metrics": {
                "visible_core_landmarks": 5,
                "has_visible_shoulder": True,
                "positive_streak": sequence + 2,
                "seconds_since_last_positive": 0.0,
            },
        }


@pytest.mark.asyncio
async def test_enrollment_reconnect_monitoring_and_registry_query(aiohttp_client):
    config = {"server": {"auth": {"enabled": False}, "auth_key": ""}}
    registry = PresenceRegistry()
    pipeline_state = {}
    stream_handler = CameraStreamHandler(
        config,
        registry,
        enrollment_factory=lambda options: EnrollmentPipeline(pipeline_state),
        monitoring_factory=lambda options: MonitoringPipeline(pipeline_state),
        now_provider=lambda: NOW,
        monotonic=lambda: 100.0,
    )
    app = web.Application()
    add_presence_routes(
        app,
        PresenceHandler(config, registry, now_provider=lambda: NOW),
        stream_handler,
    )
    client = await aiohttp_client(app)

    enrollment = await client.ws_connect("/xiaozhi/presence/stream")
    await enrollment.send_json(
        start_message(
            "enrollment", ENROLLMENT_SESSION, display_name="主人"
        )
    )
    assert (await enrollment.receive_json())["type"] == "ready"
    for accepted in range(1, 21):
        await enrollment.send_bytes(JPEG)
        event = await enrollment.receive_json()
        if accepted < 20:
            assert event["type"] == "enrollment_progress"
            assert event["accepted"] == accepted
        else:
            assert event["type"] == "enrollment_complete"
            assert event["sample_count"] == 18
    assert pipeline_state == {"template_enrolled": True}
    await enrollment.close()

    monitoring = await client.ws_connect("/xiaozhi/presence/stream")
    await monitoring.send_json(
        start_message("monitoring", MONITORING_SESSION)
    )
    assert (await monitoring.receive_json())["type"] == "ready"

    expected = [
        ("owner", True, 0.82),
        ("unknown", False, 0.21),
        ("no_face", False, None),
    ]
    for identity_state, matched, similarity in expected:
        await monitoring.send_bytes(JPEG)
        result = await monitoring.receive_json()
        assert result["type"] == "recognition_result"
        assert result["presence"]["state"] == "present"
        assert result["identity"]["state"] == identity_state
        assert result["identity"]["matched"] is matched
        assert result["identity"].get("similarity") == similarity

        query = await client.get(
            "/xiaozhi/presence/desk-integration"
        )
        payload = await query.json()
        assert payload["data"]["reported_state"] == "present"
        assert payload["data"]["identity"]["state"] == identity_state

    await monitoring.close()
