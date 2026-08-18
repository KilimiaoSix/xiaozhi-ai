import json

import pytest

from core.camera_stream.protocol import (
    MAX_FRAME_BYTES,
    StreamProtocolError,
    parse_start,
    validate_jpeg,
)


def start_payload(**changes):
    payload = {
        "type": "start",
        "schema_version": "1.0",
        "mode": "monitoring",
        "session_id": "6c618629-ffef-4c00-ab4f-17dc5ce2eb7a",
        "workstation_id": "desktop-local",
    }
    payload.update(changes)
    return payload


def test_parses_monitoring_and_trimmed_enrollment_start():
    monitoring = parse_start(json.dumps(start_payload()))
    enrollment = parse_start(
        json.dumps(start_payload(mode="enrollment", display_name="  主人  "))
    )

    assert monitoring.mode == "monitoring"
    assert monitoring.display_name is None
    assert str(monitoring.session_id) == start_payload()["session_id"]
    assert enrollment.mode == "enrollment"
    assert enrollment.display_name == "主人"


@pytest.mark.parametrize(
    "value,match",
    [
        ("not-json", "JSON"),
        (json.dumps([]), "object"),
        (json.dumps(start_payload(type="frame")), "type"),
        (json.dumps(start_payload(schema_version="2.0")), "schema_version"),
        (json.dumps(start_payload(mode="snapshot")), "mode"),
        (json.dumps(start_payload(session_id="not-a-uuid")), "session_id"),
        (json.dumps(start_payload(workstation_id="desk space")), "workstation_id"),
        (json.dumps(start_payload(extra=True)), "unexpected"),
        (json.dumps(start_payload(mode="enrollment")), "display_name"),
        (
            json.dumps(start_payload(mode="monitoring", display_name="owner")),
            "unexpected",
        ),
    ],
)
def test_rejects_invalid_start_messages(value, match):
    with pytest.raises(StreamProtocolError, match=match):
        parse_start(value)


def test_validates_jpeg_markers_and_size():
    jpeg = b"\xff\xd8payload\xff\xd9"

    assert validate_jpeg(jpeg) is jpeg

    for invalid in (b"", b"not-jpeg", b"\xff\xd8missing-end"):
        with pytest.raises(StreamProtocolError, match="JPEG"):
            validate_jpeg(invalid)

    with pytest.raises(StreamProtocolError, match="1 MiB"):
        validate_jpeg(b"\xff\xd8" + b"x" * MAX_FRAME_BYTES + b"\xff\xd9")
