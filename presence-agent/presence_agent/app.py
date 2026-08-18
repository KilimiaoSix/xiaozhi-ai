"""Command-line camera presence sidecar."""

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import import_module
import math
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time

from presence_agent.camera_backend import open_camera, permission_hint
from presence_agent.pose_detector import PoseDetector, PoseObservation
from presence_agent.face_template import load_template
from presence_agent.face_verifier import FaceEngine, FaceVerifier
from presence_agent.render import render_frame
from presence_agent.reporter import HttpPresenceTransport, PresenceReporter
from presence_agent.snapshot import LatestSnapshot
from presence_agent.state import PresenceState, PresenceTracker


AGENT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_PATH = AGENT_ROOT / "models" / "pose_landmarker_lite.task"
DEFAULT_FACE_DETECTOR_PATH = AGENT_ROOT / "models" / "face_detection_yunet_2026may.onnx"
DEFAULT_FACE_RECOGNIZER_PATH = AGENT_ROOT / "models" / "face_recognition_sface_2021dec.onnx"
DEFAULT_FACE_TEMPLATE_PATH = AGENT_ROOT / ".runtime" / "owner_template.npz"
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


def _nonnegative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return number


def _similarity_threshold(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not -1 <= number <= 1:
        raise argparse.ArgumentTypeError("must be a finite number between -1 and 1")
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
    parser.set_defaults(face_verification=True)
    parser.add_argument(
        "--disable-face-verification",
        action="store_false",
        dest="face_verification",
    )
    parser.add_argument(
        "--face-detector-model",
        type=Path,
        default=DEFAULT_FACE_DETECTOR_PATH,
        metavar="PATH",
    )
    parser.add_argument(
        "--face-recognizer-model",
        type=Path,
        default=DEFAULT_FACE_RECOGNIZER_PATH,
        metavar="PATH",
    )
    parser.add_argument(
        "--face-template",
        type=Path,
        default=DEFAULT_FACE_TEMPLATE_PATH,
        metavar="PATH",
    )
    parser.add_argument(
        "--face-threshold",
        type=_similarity_threshold,
        default=0.45,
    )
    parser.add_argument("--face-hits", type=_positive_int, default=3)
    parser.add_argument(
        "--no-face-delay",
        type=_nonnegative_float,
        default=1.0,
        metavar="SECONDS",
    )
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_face_verifier_factory(args):
    if not args.face_verification:
        return None
    template_path = Path(args.face_template)
    detector_path = Path(args.face_detector_model)
    recognizer_path = Path(args.face_recognizer_model)
    for path in (detector_path, recognizer_path):
        if not path.is_file():
            raise FileNotFoundError(f"Face model file not found: {path}")
    if not template_path.is_file():
        return FaceVerifier.not_enrolled()
    owner = load_template(template_path, _sha256(recognizer_path))
    return FaceVerifier(
        FaceEngine(detector_path, recognizer_path),
        owner.embedding,
        threshold=args.face_threshold,
        required_hits=args.face_hits,
        no_face_delay=args.no_face_delay,
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
    face_verifier_factory=_default_face_verifier_factory,
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

    try:
        face_verifier = face_verifier_factory(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Cannot start face verification: {exc}", file=sys.stderr)
        return 2

    cv2 = cv2_module or import_module("cv2")
    latest = LatestSnapshot(utcnow())
    if face_verifier is not None:
        latest.publish(
            PresenceState.STARTING,
            "initializing",
            utcnow(),
            {},
            identity=face_verifier.identity.to_payload(),
        )
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
    open_failure_hinted = False

    try:
        with detector_factory(model_path) as detector:
            while True:
                camera = open_camera(cv2, args.camera)
                if not camera.isOpened():
                    hint = permission_hint()
                    if hint and not open_failure_hinted:
                        print(hint, file=sys.stderr)
                        open_failure_hinted = True
                    now = monotonic()
                    tracker.update(False, now, camera_ok=False)
                    identity = (
                        face_verifier.camera_error(args.camera).to_payload()
                        if face_verifier is not None
                        else None
                    )
                    latest.publish(
                        PresenceState.CAMERA_ERROR,
                        "camera_open_failed",
                        utcnow(),
                        {},
                        identity=identity,
                    )
                    camera.release()
                    camera = None
                    sleep(args.camera_retry_seconds)
                    continue

                camera.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
                camera.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
                if (
                    face_verifier is not None
                    and face_verifier.identity.state.value == "camera_error"
                ):
                    face_verifier.camera_recovered()

                while True:
                    ok, frame = camera.read()
                    now = monotonic()
                    if not ok:
                        tracker.update(False, now, camera_ok=False)
                        identity = (
                            face_verifier.camera_error(args.camera).to_payload()
                            if face_verifier is not None
                            else None
                        )
                        latest.publish(
                            PresenceState.CAMERA_ERROR,
                            "camera_read_failed",
                            utcnow(),
                            {},
                            identity=identity,
                        )
                        camera.release()
                        camera = None
                        sleep(args.camera_retry_seconds)
                        break

                    frame = cv2.flip(frame, 1)
                    timestamp_ms = _strict_timestamp_ms(now, previous_timestamp_ms)
                    previous_timestamp_ms = timestamp_ms
                    observation = detector.detect(frame, timestamp_ms)
                    identity = (
                        face_verifier.observe(frame, now).to_payload()
                        if face_verifier is not None
                        else None
                    )
                    previous_state = tracker.state
                    state = tracker.update(observation.is_present(), now)
                    metrics = observation.diagnostic_metrics()
                    metrics.update(tracker.metrics(now))
                    latest.publish(
                        state,
                        _transition_reason(previous_state, state),
                        utcnow(),
                        metrics,
                        identity=identity,
                    )

                    if args.preview:
                        fps = (
                            0.0
                            if previous_frame_time is None
                            else 1.0 / max(now - previous_frame_time, 1e-9)
                        )
                        preview = render_frame(
                            frame, observation, state, fps, identity=identity
                        )
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
