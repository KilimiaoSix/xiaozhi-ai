"""Interactive local owner enrollment for the integrated face verifier."""

import argparse
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import sys
import time

import cv2

from presence_agent.camera_backend import open_camera, permission_hint
from presence_agent.face_template import OwnerTemplate, save_template
from presence_agent.face_verifier import FaceEngine, build_centroid, sample_quality


AGENT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DETECTOR = AGENT_ROOT / "models" / "face_detection_yunet_2026may.onnx"
DEFAULT_RECOGNIZER = AGENT_ROOT / "models" / "face_recognition_sface_2021dec.onnx"
DEFAULT_TEMPLATE = AGENT_ROOT / ".runtime" / "owner_template.npz"
MAX_READ_FAILURES = 30


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Enroll the local workstation owner")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--width", type=_positive_int, default=640)
    parser.add_argument("--height", type=_positive_int, default=480)
    parser.add_argument("--samples", type=_positive_int, default=20)
    parser.add_argument("--detector-model", type=Path, default=DEFAULT_DETECTOR)
    parser.add_argument("--recognizer-model", type=Path, default=DEFAULT_RECOGNIZER)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(args) -> int:
    if args.samples <= 2:
        print("Enrollment samples must be greater than 2", file=sys.stderr)
        return 2
    for path in (args.detector_model, args.recognizer_model):
        if not path.is_file():
            print(f"Face model file not found: {path}", file=sys.stderr)
            return 2
    if args.template.exists() and not args.force:
        print(f"Template already exists: {args.template}. Use -Force to replace it.", file=sys.stderr)
        return 2

    engine = FaceEngine(args.detector_model, args.recognizer_model)
    camera = open_camera(cv2, args.camera)
    if not camera.isOpened():
        print(f"Cannot open camera index {args.camera}", file=sys.stderr)
        hint = permission_hint()
        if hint:
            print(hint, file=sys.stderr)
        camera.release()
        return 1
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    samples = []
    last_accepted = -math.inf
    failures = 0
    try:
        while len(samples) < args.samples:
            ok, frame = camera.read()
            if not ok or frame is None:
                failures += 1
                if failures >= MAX_READ_FAILURES:
                    print("Camera failed to return 30 consecutive frames", file=sys.stderr)
                    return 1
                continue
            failures = 0
            frame = cv2.flip(frame, 1)
            detections = engine.detect(frame)
            message = "Keep exactly one face in view"
            if len(detections) == 1:
                aligned = engine.align(frame, detections[0])
                accepted, reason, score = sample_quality(aligned, detections[0])
                now = time.monotonic()
                if accepted and now - last_accepted >= 0.2:
                    samples.append(engine.embedding_from_aligned(aligned))
                    last_accepted = now
                    message = f"Accepted {len(samples)}/{args.samples}"
                elif not accepted:
                    message = f"Quality: {reason} ({score:.1f})"
            elif len(detections) > 1:
                message = "Multiple faces: no sample accepted"
            for face in detections:
                cv2.rectangle(
                    frame,
                    (max(0, int(face.x)), max(0, int(face.y))),
                    (max(0, int(face.x + face.width)), max(0, int(face.y + face.height))),
                    (0, 220, 255),
                    2,
                )
            cv2.putText(frame, message, (16, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(frame, "Q / Esc: cancel", (16, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
            cv2.imshow("Launchcrush owner enrollment", frame)
            if (cv2.waitKey(1) & 0xFF) in (ord("q"), ord("Q"), 27):
                return 130

        centroid, retained = build_centroid(samples, trim_count=2)
        save_template(
            args.template,
            OwnerTemplate(
                embedding=centroid,
                model_sha256=_sha256(args.recognizer_model),
                created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                sample_count=retained,
            ),
            overwrite=args.force,
        )
        print(f"Owner template saved: {args.template}")
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        camera.release()
        cv2.destroyAllWindows()


def main(argv=None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
