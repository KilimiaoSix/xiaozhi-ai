"""Versioned latest-state reporting with heartbeat and bounded backoff."""

from copy import deepcopy
from datetime import timezone
import json
import logging
import time
from typing import Callable
from urllib import error, request
from uuid import uuid4

from presence_agent.snapshot import PresenceSnapshot


BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)


class ReportTransportError(RuntimeError):
    pass


class HttpPresenceTransport:
    def __init__(
        self,
        server_url: str,
        auth_token: str = "",
        timeout_seconds: float = 5.0,
    ) -> None:
        self._url = server_url.rstrip("/") + "/xiaozhi/presence/report"
        self._auth_token = auth_token
        self._timeout_seconds = timeout_seconds

    def send(self, payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        http_request = request.Request(
            self._url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=self._timeout_seconds) as response:
                response_body = response.read()
        except error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
                code = detail.get("code", "HTTP_ERROR")
            except Exception:
                code = "HTTP_ERROR"
            raise ReportTransportError(f"server returned HTTP {exc.code} ({code})") from None
        except (error.URLError, OSError, TimeoutError) as exc:
            raise ReportTransportError(f"presence server unavailable: {exc}") from None

        try:
            result = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ReportTransportError("presence server returned invalid json") from None
        if not isinstance(result, dict) or result.get("code") != "OK":
            raise ReportTransportError("presence server rejected the report")
        return result


class PresenceReporter:
    def __init__(
        self,
        snapshot_provider: Callable[[], PresenceSnapshot],
        transport,
        workstation_id: str,
        heartbeat_seconds: float = 15.0,
        poll_seconds: float = 0.1,
        agent_instance_id: str | None = None,
        event_id_factory: Callable[[], str] | None = None,
        logger=None,
    ) -> None:
        if heartbeat_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("reporter intervals must be greater than zero")
        self._snapshot_provider = snapshot_provider
        self._transport = transport
        self._workstation_id = workstation_id
        self._heartbeat_seconds = heartbeat_seconds
        self._poll_seconds = poll_seconds
        self._agent_instance_id = agent_instance_id or str(uuid4())
        self._event_id_factory = event_id_factory or (lambda: str(uuid4()))
        self._logger = logger or logging.getLogger(__name__)

        self._sequence = 0
        self._last_acked_revision: int | None = None
        self._last_success_monotonic: float | None = None
        self._pending: dict | None = None
        self._pending_revision: int | None = None
        self._next_attempt_monotonic = 0.0
        self._backoff_index = 0

    @staticmethod
    def _format_timestamp(snapshot: PresenceSnapshot) -> str:
        return snapshot.observed_at.astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")

    def _build_event(self, snapshot: PresenceSnapshot, heartbeat: bool) -> dict:
        self._sequence += 1
        event = {
            "schema_version": "1.0",
            "event_id": self._event_id_factory(),
            "agent_instance_id": self._agent_instance_id,
            "workstation_id": self._workstation_id,
            "source": "camera_pose",
            "state": snapshot.state.value,
            "previous_state": (
                snapshot.state.value if heartbeat else snapshot.previous_state.value
            ),
            "changed": False if heartbeat else snapshot.changed,
            "reason": "heartbeat" if heartbeat else snapshot.reason,
            "sequence": self._sequence,
            "observed_at": self._format_timestamp(snapshot),
            "metrics": deepcopy(snapshot.metrics),
        }
        if snapshot.identity is not None:
            event["identity"] = deepcopy(snapshot.identity)
            if heartbeat:
                event["identity"]["previous_state"] = event["identity"]["state"]
                event["identity"]["changed"] = False
        return event

    def _replace_pending(self, snapshot: PresenceSnapshot, now_monotonic: float) -> None:
        self._pending = self._build_event(snapshot, heartbeat=False)
        self._pending_revision = snapshot.revision
        self._next_attempt_monotonic = now_monotonic

    def step(self, now_monotonic: float) -> float:
        snapshot = self._snapshot_provider()

        if self._pending is not None and snapshot.revision != self._pending_revision:
            self._replace_pending(snapshot, now_monotonic)

        if self._pending is None:
            if self._last_acked_revision != snapshot.revision:
                self._replace_pending(snapshot, now_monotonic)
            elif (
                self._last_success_monotonic is not None
                and now_monotonic - self._last_success_monotonic
                >= self._heartbeat_seconds
            ):
                self._pending = self._build_event(snapshot, heartbeat=True)
                self._pending_revision = snapshot.revision
                self._next_attempt_monotonic = now_monotonic
            else:
                return self._poll_seconds

        if now_monotonic < self._next_attempt_monotonic:
            return min(
                self._poll_seconds,
                self._next_attempt_monotonic - now_monotonic,
            )

        try:
            self._transport.send(deepcopy(self._pending))
        except Exception as exc:
            delay = BACKOFF_SECONDS[min(self._backoff_index, len(BACKOFF_SECONDS) - 1)]
            self._backoff_index += 1
            self._next_attempt_monotonic = now_monotonic + delay
            self._logger.warning(
                "Presence report failed; retrying in %.1fs: %s", delay, exc
            )
            return delay

        self._last_acked_revision = self._pending_revision
        self._last_success_monotonic = now_monotonic
        self._pending = None
        self._pending_revision = None
        self._backoff_index = 0
        self._next_attempt_monotonic = now_monotonic
        return self._poll_seconds

    def run(self, stop_event) -> None:
        while not stop_event.is_set():
            delay = max(0.001, self.step(time.monotonic()))
            stop_event.wait(delay)
