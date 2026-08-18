"""Strict control-message and JPEG validation for camera streams."""

from dataclasses import dataclass
import json
import re
from typing import Literal
from uuid import UUID


MAX_FRAME_BYTES = 1024 * 1024
WORKSTATION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class StreamProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class StreamStart:
    mode: Literal["monitoring", "enrollment"]
    session_id: UUID
    workstation_id: str
    display_name: str | None = None


def parse_start(text: str) -> StreamStart:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StreamProtocolError("start message must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise StreamProtocolError("start message must be a JSON object")

    mode = payload.get("mode")
    if mode not in ("monitoring", "enrollment"):
        raise StreamProtocolError("mode must be monitoring or enrollment")
    allowed = {
        "type",
        "schema_version",
        "mode",
        "session_id",
        "workstation_id",
    }
    if mode == "enrollment":
        allowed.add("display_name")
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        raise StreamProtocolError(f"unexpected fields: {', '.join(unexpected)}")
    if payload.get("type") != "start":
        raise StreamProtocolError("type must be start")
    if payload.get("schema_version") != "1.0":
        raise StreamProtocolError("schema_version must be 1.0")

    try:
        session_id = UUID(payload.get("session_id", ""))
    except (AttributeError, TypeError, ValueError) as exc:
        raise StreamProtocolError("session_id must be a UUID") from exc
    workstation_id = payload.get("workstation_id")
    if not isinstance(workstation_id, str) or not WORKSTATION_PATTERN.fullmatch(
        workstation_id
    ):
        raise StreamProtocolError(
            "workstation_id must match [A-Za-z0-9._-]{1,64}"
        )

    display_name = None
    if mode == "enrollment":
        raw_name = payload.get("display_name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise StreamProtocolError("display_name is required for enrollment")
        display_name = raw_name.strip()
        if len(display_name) > 64:
            raise StreamProtocolError("display_name must not exceed 64 characters")

    return StreamStart(mode, session_id, workstation_id, display_name)


def validate_jpeg(frame: bytes) -> bytes:
    if not isinstance(frame, bytes) or not frame.startswith(b"\xff\xd8") or not frame.endswith(
        b"\xff\xd9"
    ):
        raise StreamProtocolError("frame must be a complete JPEG image")
    if len(frame) > MAX_FRAME_BYTES:
        raise StreamProtocolError("JPEG frame exceeds 1 MiB")
    return frame
