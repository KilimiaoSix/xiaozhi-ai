"""Command-line camera presence sidecar."""

import argparse
from datetime import datetime, timezone
from importlib import import_module
import math
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time

from presence_agent.pose_detector import PoseDetector, PoseObservation
from presence_agent.render import render_frame
from presence_agent.reporter import HttpPresenceTransport, PresenceReporter
from presence_agent.snapshot import LatestSnapshot
from presence_agent.state import PresenceState, PresenceTracker


AGENT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_PATH = AGENT_ROOT / "models" / "pose_landmarker_lite.task"
WINDOW_NAME = "Launchcrush Camera Presence"
REQUIRED_HITS = 3
WORKSTATION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def _workstation_id(value: str) -> str:
    if not WORKSTATION_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("must match [A-Za-z0-9._-]{1,64}")
    return value


def _default_workstation_id() -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", socket.gethostname()).strip("-.")
    return (normalized or "workstation")[:64]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Detect local workstation presence and report state to launchcrush."
    )
    parser.add_argument("--server-url", default="http://127.0.0.1:8003")
    parser.add_argument(
        "--workstation-id",
        type=_workstation_id,
        default=_default_workstation_id(),
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("PRESENCE_AUTH_TOKEN", ""),
    )
    parser.add_argument("--camera", type=_nonnegative_int, default=0, metavar="INDEX")
    parser.add_argument("--width", type=_positive_int, default=640, metavar="PIXELS")
    parser.add_argument("--height", type=_positive_int, default=480, metavar="PIXELS")
    parser.add_argument(
        "--absent-after", type=_positive_float, default=2.0, metavar="SECONDS"
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=_positive_float,
        default=15.0,
        metavar="SECONDS",
    )
    parser.add_argument(
        "--camera-retry-seconds",
        type=_positive_float,
        default=5.0,
        metavar="SECONDS",
    )
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH, metavar="PATH")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument(
        "--smoke-frames", type=_nonnegative_int, default=0, metavar="COUNT"
    )
    return parser.parse_args(argv)


def _strict_timestamp_ms(now_seconds: float, previous_timestamp_ms: int) -> int:
    return max(int(now_seconds * 1000), previous_timestamp_ms + 1)


def _default_reporter_factory(latest: LatestSnapshot, args) -> PresenceReporter:
    return PresenceReporter(
        snapshot_provider=latest.read,
        transport=HttpPresenceTransport(args.server_url, args.auth_token),
        workstation_id=args.workstation_id,
        heartbeat_seconds=args.heartbeat_seconds,
    )


def _transition_reason(previous: PresenceState, current: PresenceState) -> str:
    if current is PresenceState.PRESENT:
        return "pose_confirmed"
    if current is PresenceState.ABSENT:
        return "absence_timeout"
    if current is PresenceState.CAMERA_ERROR:
        return "camera_read_failed"
    if previous is PresenceState.CAMERA_ERROR and current is PresenceState.STARTING:
        return "camera_recovered"
    return "initializing"


def run(
    args,
    *,
    cv2_module=None,
    detector_factory=PoseDetector,
    reporter_factory=_default_reporter_factory,
    thread_factory=threading.Thread,
    sleep=time.sleep,
    monotonic=time.monotonic,
    utcnow=lambda: datetime.now(timezone.utc),
) -> int:
    model_path = Path(args.model)
    if not model_path.is_file():
        print(f"Model file not found: {model_path}", file=sys.stderr)
        return 2

    cv2 = cv2_module or import_module("cv2")
    latest = LatestSnapshot(utcnow())
    reporter = reporter_factory(latest, args)
    stop_event = threading.Event()
    reporter_thread = thread_factory(
        target=lambda: reporter.run(stop_event),
        name="presence-reporter",
        daemon=True,
    )
    reporter_thread.start()

    tracker = PresenceTracker(REQUIRED_HITS, args.absent_after)
    camera = None
    processed_frames = 0
    previous_timestamp_ms = -1
    previous_frame_time = None

    try:
        with detector_factory(model_path) as detector:
            while True:
                camera = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
                if not camera.isOpened():
                    now = monotonic()
                    tracker.update(False, now, camera_ok=False)
                    latest.publish(
                        PresenceState.CAMERA_ERROR,
                        "camera_open_failed",
                        utcnow(),
                        {},
                    )
                    camera.release()
                    camera = None
                    sleep(args.camera_retry_seconds)
                    continue

                camera.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
                camera.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)

                while True:
                    ok, frame = camera.read()
                    now = monotonic()
                    if not ok:
                        tracker.update(False, now, camera_ok=False)
                        latest.publish(
                            PresenceState.CAMERA_ERROR,
                            "camera_read_failed",
                            utcnow(),
                            {},
                        )
                        camera.release()
                        camera = None
                        sleep(args.camera_retry_seconds)
                        break

                    frame = cv2.flip(frame, 1)
                    timestamp_ms = _strict_timestamp_ms(now, previous_timestamp_ms)
                    previous_timestamp_ms = timestamp_ms
                    observation = detector.detect(frame, timestamp_ms)
                    previous_state = tracker.state
                    state = tracker.update(observation.is_present(), now)
                    metrics = observation.diagnostic_metrics()
                    metrics.update(tracker.metrics(now))
                    latest.publish(
                        state,
                        _transition_reason(previous_state, state),
                        utcnow(),
                        metrics,
                    )

                    if args.preview:
                        fps = (
                            0.0
                            if previous_frame_time is None
                            else 1.0 / max(now - previous_frame_time, 1e-9)
                        )
                        preview = render_frame(frame, observation, state, fps)
                        cv2.imshow(WINDOW_NAME, preview)
                        if (cv2.waitKey(1) & 0xFF) in (ord("q"), 27):
                            return 0
                    previous_frame_time = now

                    if args.smoke_frames > 0:
                        processed_frames += 1
                        if processed_frames >= args.smoke_frames:
                            return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if camera is not None:
            camera.release()
        cv2.destroyAllWindows()
        stop_event.set()
        reporter_thread.join(timeout=6.0)
        reporter.step(monotonic())


def main(argv=None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
