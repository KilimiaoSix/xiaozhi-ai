import asyncio
from datetime import datetime, timezone
import threading
from uuid import UUID

import pytest
from aiohttp import WSServerHandshakeError, web

from core.api.camera_stream_handler import (
    CameraEnrollmentRuntime,
    CameraStreamHandler,
    ModelUnavailableError,
)
from core.api.presence_handler import PresenceHandler
from core.camera_stream.protocol import MAX_FRAME_BYTES
from core.presence_registry import PresenceRegistry
from core.presence_routes import add_presence_routes


SESSION_ID = "6c618629-ffef-4c00-ab4f-17dc5ce2eb7a"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
JPEG = b"\xff\xd8camera-frame\xff\xd9"


def test_enrollment_runtime_preserves_input_sequence():
    decoded = object()

    class Collector:
        def accept(self, frame, now):
            assert frame is decoded
            assert now == 12.5
            return {
                "type": "enrollment_progress",
                "accepted": 1,
                "required": 20,
                "reason": "accepted",
            }

    runtime = CameraEnrollmentRuntime(lambda jpeg: decoded, Collector())

    result = runtime.process(JPEG, sequence=7, now=12.5)

    assert result["sequence"] == 7


class FakeRuntime:
    def __init__(self, results):
        self._results = iter(results)
        self.calls = []
        self.closed = False
        self.closed_event = asyncio.Event()

    def process(self, jpeg, sequence, now):
        self.calls.append((jpeg, sequence, now))
        result = dict(next(self._results))
        result.setdefault("sequence", sequence)
        return result

    def close(self):
        self.closed = True
        self.closed_event.set()


class RaisingRuntime(FakeRuntime):
    def process(self, jpeg, sequence, now):
        raise RuntimeError("model exploded")


def start_message(mode="monitoring", **extra):
    payload = {
        "type": "start",
        "schema_version": "1.0",
        "mode": mode,
        "session_id": SESSION_ID,
        "workstation_id": "desk-test",
    }
    payload.update(extra)
    return payload


async def make_client(
    aiohttp_client,
    *,
    monitoring_runtime=None,
    enrollment_runtime=None,
    auth=False,
    monitoring_factory=None,
):
    config = {
        "server": {
            "auth": {"enabled": auth},
            "auth_key": "test-secret" if auth else "",
        }
    }
    registry = PresenceRegistry()
    monitoring_runtime = monitoring_runtime or FakeRuntime([])
    enrollment_runtime = enrollment_runtime or FakeRuntime([])
    stream_handler = CameraStreamHandler(
        config,
        registry,
        monitoring_factory=monitoring_factory
        or (lambda options: monitoring_runtime),
        enrollment_factory=lambda options: enrollment_runtime,
        now_provider=lambda: NOW,
        monotonic=lambda: 100.0,
    )
    app = web.Application()
    add_presence_routes(
        app,
        PresenceHandler(config, registry, now_provider=lambda: NOW),
        stream_handler,
    )
    return await aiohttp_client(app), registry, monitoring_runtime, enrollment_runtime


@pytest.mark.asyncio
async def test_websocket_upgrade_uses_bearer_authentication(aiohttp_client):
    client, _, _, _ = await make_client(aiohttp_client, auth=True)

    with pytest.raises(WSServerHandshakeError) as rejected:
        await client.ws_connect("/xiaozhi/presence/stream")
    accepted = await client.ws_connect(
        "/xiaozhi/presence/stream",
        headers={"Authorization": "Bearer test-secret"},
    )

    assert rejected.value.status == 401
    await accepted.close()


@pytest.mark.asyncio
async def test_monitoring_stream_returns_result_and_updates_registry(aiohttp_client):
    runtime = FakeRuntime(
        [
            {
                "presence": {"state": "present", "changed": True},
                "identity": {
                    "state": "owner",
                    "previous_state": "starting",
                    "changed": True,
                    "face_count": 1,
                    "face_detected": True,
                    "similarity": 0.731245,
                    "horizontal_position": "right",
                    "threshold": 0.45,
                    "matched": True,
                },
                "metrics": {
                    "visible_core_landmarks": 5,
                    "has_visible_shoulder": True,
                    "positive_streak": 3,
                    "seconds_since_last_positive": 0.0,
                },
            }
        ]
    )
    client, registry, _, _ = await make_client(
        aiohttp_client, monitoring_runtime=runtime
    )
    ws = await client.ws_connect("/xiaozhi/presence/stream")

    await ws.send_json(start_message())
    ready = await ws.receive_json()
    await ws.send_bytes(JPEG)
    result = await ws.receive_json()

    assert ready == {"type": "ready", "session_id": SESSION_ID, "sequence": 0}
    assert result["type"] == "recognition_result"
    assert result["session_id"] == SESSION_ID
    assert result["sequence"] == 1
    assert result["processed_at"] == "2026-08-18T12:00:00.000Z"
    assert result["identity"]["face_detected"] is True
    assert result["identity"]["similarity"] == 0.731245
    assert result["identity"]["horizontal_position"] == "right"
    assert result["identity"]["matched"] is True
    assert result["metrics"]["processed_frames"] == 1
    assert result["metrics"]["server_dropped"] == 0
    assert runtime.calls == [(JPEG, 1, 100.0)]

    stored = registry.get("desk-test")
    assert stored["reported_state"] == "present"
    assert stored["identity"] == {
        "state": "owner",
        "previous_state": "starting",
        "changed": True,
        "face_count": 1,
        "similarity": 0.731245,
        "horizontal_position": "right",
    }
    UUID(stored["event_id"])
    await ws.close()


@pytest.mark.asyncio
async def test_enrollment_stream_returns_progress_and_completion(aiohttp_client):
    runtime = FakeRuntime(
        [
            {
                "type": "enrollment_progress",
                "accepted": 1,
                "required": 20,
                "reason": "accepted",
            },
            {
                "type": "enrollment_complete",
                "profile_id": "owner",
                "sample_id": "sample-id",
                "display_name": "主人",
                "stored_at": "2026-08-18T12:00:00Z",
                "sample_count": 18,
            },
        ]
    )
    client, _, _, _ = await make_client(
        aiohttp_client, enrollment_runtime=runtime
    )
    ws = await client.ws_connect("/xiaozhi/presence/stream")

    await ws.send_json(start_message("enrollment", display_name=" 主人 "))
    assert (await ws.receive_json())["type"] == "ready"
    await ws.send_bytes(JPEG)
    progress = await ws.receive_json()
    await ws.send_bytes(JPEG)
    complete = await ws.receive_json()

    assert progress == {
        "type": "enrollment_progress",
        "accepted": 1,
        "required": 20,
        "reason": "accepted",
        "session_id": SESSION_ID,
        "sequence": 1,
    }
    assert complete["type"] == "enrollment_complete"
    assert complete["session_id"] == SESSION_ID
    assert complete["sequence"] == 2
    assert complete["sample_count"] == 18
    await ws.close()


@pytest.mark.asyncio
async def test_first_message_must_be_start(aiohttp_client):
    client, _, _, _ = await make_client(aiohttp_client)
    ws = await client.ws_connect("/xiaozhi/presence/stream")

    await ws.send_bytes(JPEG)
    error = await ws.receive_json()

    assert error["type"] == "error"
    assert error["code"] == "PROTOCOL_ERROR"
    assert error["retryable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frame", "code"),
    [
        (b"not-a-jpeg", "INVALID_JPEG"),
        (b"\xff\xd8" + b"x" * MAX_FRAME_BYTES + b"\xff\xd9", "FRAME_TOO_LARGE"),
    ],
)
async def test_invalid_frames_return_stable_errors(aiohttp_client, frame, code):
    client, _, _, _ = await make_client(aiohttp_client)
    ws = await client.ws_connect("/xiaozhi/presence/stream")
    await ws.send_json(start_message())
    await ws.receive_json()

    await ws.send_bytes(frame)
    error = await ws.receive_json()

    assert error["type"] == "error"
    assert error["code"] == code
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_stop_returns_stopped_and_closes_runtime(aiohttp_client):
    runtime = FakeRuntime([])
    client, _, _, _ = await make_client(
        aiohttp_client, monitoring_runtime=runtime
    )
    ws = await client.ws_connect("/xiaozhi/presence/stream")
    await ws.send_json(start_message())
    await ws.receive_json()

    await ws.send_json({"type": "stop"})
    stopped = await ws.receive_json()

    assert stopped == {"type": "stopped", "session_id": SESSION_ID, "sequence": 0}
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_disconnect_closes_runtime(aiohttp_client):
    runtime = FakeRuntime([])
    client, _, _, _ = await make_client(
        aiohttp_client, monitoring_runtime=runtime
    )
    ws = await client.ws_connect("/xiaozhi/presence/stream")
    await ws.send_json(start_message())
    await ws.receive_json()

    await ws.close()
    await asyncio.wait_for(runtime.closed_event.wait(), timeout=0.1)

    assert runtime.closed is True


@pytest.mark.asyncio
async def test_missing_models_returns_retryable_model_unavailable(aiohttp_client):
    def unavailable(options):
        raise ModelUnavailableError("pose model is missing")

    client, _, _, _ = await make_client(
        aiohttp_client, monitoring_factory=unavailable
    )
    ws = await client.ws_connect("/xiaozhi/presence/stream")

    await ws.send_json(start_message())
    error = await ws.receive_json()

    assert error["code"] == "MODEL_UNAVAILABLE"
    assert error["retryable"] is True
    assert error["session_id"] == SESSION_ID


@pytest.mark.asyncio
async def test_runtime_factory_does_not_block_event_loop_thread(aiohttp_client):
    event_loop_thread = threading.get_ident()
    factory_threads = []
    runtime = FakeRuntime([])

    def factory(options):
        factory_threads.append(threading.get_ident())
        return runtime

    client, _, _, _ = await make_client(
        aiohttp_client, monitoring_factory=factory
    )
    ws = await client.ws_connect("/xiaozhi/presence/stream")

    await ws.send_json(start_message())
    assert (await ws.receive_json())["type"] == "ready"

    assert factory_threads and factory_threads[0] != event_loop_thread
    await ws.close()


@pytest.mark.asyncio
async def test_inference_failure_returns_retryable_error_without_details(
    aiohttp_client,
):
    client, _, _, _ = await make_client(
        aiohttp_client, monitoring_runtime=RaisingRuntime([])
    )
    ws = await client.ws_connect("/xiaozhi/presence/stream")
    await ws.send_json(start_message())
    await ws.receive_json()

    await ws.send_bytes(JPEG)
    error = await ws.receive_json()

    assert error["code"] == "INFERENCE_ERROR"
    assert error["retryable"] is True
    assert "exploded" not in error["message"]
