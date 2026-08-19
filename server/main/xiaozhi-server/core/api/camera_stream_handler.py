"""Authenticated aiohttp WebSocket adapter for desktop camera frames."""

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
from importlib import import_module
import json
import logging
from pathlib import Path
import time
from typing import Any, Callable
from uuid import uuid4

from aiohttp import WSMsgType, WSCloseCode, web

from core.camera_stream.enrollment import EnrollmentCollector
from core.camera_stream.inference import CameraInferenceRuntime
from core.camera_stream.protocol import (
    MAX_FRAME_BYTES,
    StreamProtocolError,
    StreamStart,
    parse_start,
    validate_jpeg,
)
from core.camera_stream.observers import FrameObserverHub, frame_observer_hub
from core.camera_stream.session import CameraStreamSession, Frame
from core.presence_registry import PresenceRegistry, PresenceReport


_REGISTRY_METRICS = {
    "visible_core_landmarks",
    "has_visible_shoulder",
    "positive_streak",
    "seconds_since_last_positive",
}
_REGISTRY_IDENTITY = {
    "state",
    "previous_state",
    "changed",
    "face_count",
    "similarity",
    "camera",
}


class ModelUnavailableError(RuntimeError):
    """Camera inference dependencies or model assets are unavailable."""


class CameraEnrollmentRuntime:
    def __init__(self, decoder, collector: EnrollmentCollector) -> None:
        self._decoder = decoder
        self._collector = collector

    def process(self, jpeg: bytes, sequence: int, now: float) -> dict[str, Any]:
        result = dict(self._collector.accept(self._decoder(jpeg), now))
        result["sequence"] = sequence
        return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decoder():
    cv2 = import_module("cv2")
    np = import_module("numpy")

    def decode(jpeg: bytes):
        frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("JPEG could not be decoded")
        return frame

    return decode


def _require_models(*paths: Path) -> None:
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise ModelUnavailableError(
            f"camera model files are missing: {', '.join(missing)}"
        )


def _default_monitoring_factory(options: StreamStart):
    try:
        from presence_agent.app import (
            DEFAULT_FACE_DETECTOR_PATH,
            DEFAULT_FACE_RECOGNIZER_PATH,
            DEFAULT_FACE_TEMPLATE_PATH,
            DEFAULT_MODEL_PATH,
        )
        from presence_agent.face_template import load_template
        from presence_agent.face_verifier import FaceEngine, FaceVerifier
        from presence_agent.pose_detector import PoseDetector
        from presence_agent.state import PresenceTracker

        _require_models(
            DEFAULT_MODEL_PATH,
            DEFAULT_FACE_DETECTOR_PATH,
            DEFAULT_FACE_RECOGNIZER_PATH,
        )
        pose_detector = PoseDetector(DEFAULT_MODEL_PATH)
        if DEFAULT_FACE_TEMPLATE_PATH.is_file():
            owner = load_template(
                DEFAULT_FACE_TEMPLATE_PATH,
                _sha256(DEFAULT_FACE_RECOGNIZER_PATH),
            )
            face_verifier = FaceVerifier(
                FaceEngine(
                    DEFAULT_FACE_DETECTOR_PATH,
                    DEFAULT_FACE_RECOGNIZER_PATH,
                ),
                owner.embedding,
                threshold=0.45,
                required_hits=3,
                no_face_delay=1.0,
            )
        else:
            face_verifier = FaceVerifier.not_enrolled()
        return CameraInferenceRuntime(
            decoder=_decoder(),
            pose_detector=pose_detector,
            presence_tracker=PresenceTracker(3, 2.0),
            face_verifier=face_verifier,
            face_threshold=0.45,
        )
    except ModelUnavailableError:
        raise
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise ModelUnavailableError("camera inference runtime is unavailable") from exc


def _default_enrollment_factory(options: StreamStart):
    try:
        from presence_agent.app import (
            DEFAULT_FACE_DETECTOR_PATH,
            DEFAULT_FACE_RECOGNIZER_PATH,
            DEFAULT_FACE_TEMPLATE_PATH,
        )
        from presence_agent.face_verifier import FaceEngine

        _require_models(DEFAULT_FACE_DETECTOR_PATH, DEFAULT_FACE_RECOGNIZER_PATH)
        metadata_path = DEFAULT_FACE_TEMPLATE_PATH.with_name("owner_profile.json")
        collector = EnrollmentCollector(
            engine=FaceEngine(
                DEFAULT_FACE_DETECTOR_PATH,
                DEFAULT_FACE_RECOGNIZER_PATH,
            ),
            display_name=options.display_name or "",
            template_path=DEFAULT_FACE_TEMPLATE_PATH,
            metadata_path=metadata_path,
            recognizer_model_path=DEFAULT_FACE_RECOGNIZER_PATH,
        )
        return CameraEnrollmentRuntime(_decoder(), collector)
    except ModelUnavailableError:
        raise
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise ModelUnavailableError("camera enrollment runtime is unavailable") from exc


def _isoformat_millis(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class CameraStreamHandler:
    def __init__(
        self,
        config: dict,
        registry: PresenceRegistry,
        *,
        monitoring_factory: Callable[[StreamStart], Any] | None = None,
        enrollment_factory: Callable[[StreamStart], Any] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        on_accepted: Callable[..., Any] | None = None,
        observer_hub: FrameObserverHub | None = None,
        logger=None,
    ) -> None:
        server_config = config.get("server", {})
        auth_config = server_config.get("auth", {})
        self._auth_enabled = bool(auth_config.get("enabled", False))
        self._auth_key = str(server_config.get("auth_key", ""))
        self._registry = registry
        self._monitoring_factory = (
            monitoring_factory or _default_monitoring_factory
        )
        self._enrollment_factory = enrollment_factory or _default_enrollment_factory
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        # 与 PresenceHandler 的 on_accepted 同一契约：每条被 registry 接受的
        # 上报回调一次 (report, acceptance)。桌面摄像头流是产品默认链路，
        # 不接这个回调的话，迎接/休眠编排只对 presence-agent 的 HTTP 上报生效。
        self._on_accepted = on_accepted
        # 手势审批 / 分心检测的旁路看帧入口。默认用进程级单例；
        # 测试可注入独立实例避免用例间串台。
        self._observer_hub = observer_hub or frame_observer_hub
        self._logger = logger or logging.getLogger(__name__)

    def _authorized(self, request: web.Request) -> bool:
        if not self._auth_enabled:
            return True
        token = request.headers.get("Authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        return bool(self._auth_key) and hmac.compare_digest(token, self._auth_key)

    @staticmethod
    async def _error(
        ws: web.WebSocketResponse,
        *,
        session_id: str | None,
        sequence: int,
        code: str,
        message: str,
        retryable: bool,
        close_code: int | None = None,
    ) -> None:
        if not ws.closed:
            await ws.send_json(
                {
                    "type": "error",
                    "session_id": session_id,
                    "sequence": sequence,
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                }
            )
        if close_code is not None:
            await ws.close(code=close_code)

    async def handle_websocket(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return web.json_response(
                {"code": "UNAUTHORIZED", "message": "unauthorized", "data": None},
                status=401,
            )

        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES + 1024)
        await ws.prepare(request)
        runtime = None
        session = None
        session_id = None
        last_sequence = 0
        cleaned = False

        async def cleanup(cancel: bool = True) -> None:
            nonlocal cleaned
            if cleaned:
                return
            cleaned = True
            if session is not None:
                if cancel:
                    await session.cancel()
                else:
                    await session.close()
            close = getattr(runtime, "close", None)
            if close is not None:
                close()

        first = await ws.receive()
        if first.type is not WSMsgType.TEXT:
            await self._error(
                ws,
                session_id=None,
                sequence=0,
                code="PROTOCOL_ERROR",
                message="first message must be start",
                retryable=False,
                close_code=WSCloseCode.PROTOCOL_ERROR,
            )
            return ws

        try:
            options = parse_start(first.data)
        except StreamProtocolError as exc:
            await self._error(
                ws,
                session_id=None,
                sequence=0,
                code="PROTOCOL_ERROR",
                message=str(exc),
                retryable=False,
                close_code=WSCloseCode.PROTOCOL_ERROR,
            )
            return ws

        session_id = str(options.session_id)
        try:
            factory = (
                self._monitoring_factory
                if options.mode == "monitoring"
                else self._enrollment_factory
            )
            runtime = await asyncio.to_thread(factory, options)
        except ModelUnavailableError as exc:
            await self._error(
                ws,
                session_id=session_id,
                sequence=0,
                code="MODEL_UNAVAILABLE",
                message=str(exc),
                retryable=True,
                close_code=WSCloseCode.INTERNAL_ERROR,
            )
            return ws

        counters = {"server_dropped": 0, "processed_frames": 0}
        previous_presence = "starting"
        registry_sequence = 0

        observer_hub = self._observer_hub
        workstation_id = options.workstation_id
        monitoring = options.mode == "monitoring"

        def _process_sync(jpeg: bytes, sequence: int, now: float) -> dict[str, Any]:
            result = runtime.process(jpeg, sequence, now)
            # 识别成功后才把帧提供给旁路观察者（审批手势/分心检测）：
            # 观察者在同一工作线程按需解码，原始帧仍不离开推理进程
            if monitoring:
                observer_hub.offer(workstation_id, jpeg, now)
            return result

        async def process(frame: Frame) -> dict[str, Any]:
            try:
                return await asyncio.to_thread(
                    _process_sync,
                    frame.jpeg,
                    frame.sequence,
                    self._monotonic(),
                )
            except Exception:
                self._logger.exception("Camera inference failed")
                return {"_inference_error": True, "sequence": frame.sequence}

        async def emit(result: dict[str, Any]) -> None:
            nonlocal previous_presence, registry_sequence
            sequence = int(result.get("sequence", 0))
            if result.pop("_inference_error", False):
                await self._error(
                    ws,
                    session_id=session_id,
                    sequence=sequence,
                    code="INFERENCE_ERROR",
                    message="camera inference failed",
                    retryable=True,
                    close_code=WSCloseCode.INTERNAL_ERROR,
                )
                return

            if options.mode == "enrollment":
                event = dict(result)
                event.update(session_id=session_id, sequence=sequence)
                await ws.send_json(event)
                return

            counters["processed_frames"] += 1
            presence = dict(result["presence"])
            current_presence = presence["state"]
            presence["changed"] = current_presence != previous_presence
            identity = dict(result["identity"])
            metrics = dict(result.get("metrics", {}))
            metrics.update(counters)
            now = self._now_provider()
            event = {
                "type": "recognition_result",
                "session_id": session_id,
                "sequence": sequence,
                "processed_at": _isoformat_millis(now),
                "presence": presence,
                "identity": identity,
                "metrics": metrics,
            }

            registry_sequence += 1
            registry_identity = {
                key: value for key, value in identity.items() if key in _REGISTRY_IDENTITY
            }
            reason = self._reason(
                previous_presence,
                current_presence,
                presence["changed"],
                bool(registry_identity.get("changed")),
            )
            report = PresenceReport.from_payload(
                {
                    "schema_version": "1.0",
                    "event_id": str(uuid4()),
                    "agent_instance_id": session_id,
                    "workstation_id": options.workstation_id,
                    "source": "camera_pose",
                    "state": current_presence,
                    "previous_state": previous_presence,
                    "changed": presence["changed"],
                    "reason": reason,
                    "sequence": registry_sequence,
                    "observed_at": _isoformat_millis(now),
                    "metrics": {
                        key: value
                        for key, value in result.get("metrics", {}).items()
                        if key in _REGISTRY_METRICS
                    },
                    "identity": registry_identity,
                },
                now_utc=now,
            )
            acceptance = self._registry.accept(report)
            if self._on_accepted is not None:
                try:
                    await self._on_accepted(report, acceptance)
                except Exception:
                    # 编排失败不能拖垮识别流：桌面端还等着 recognition_result 刷新 UI
                    self._logger.exception("presence on_accepted 回调失败，已忽略")
            previous_presence = current_presence
            await ws.send_json(event)

        session = CameraStreamSession(process, emit)
        session.start()
        await ws.send_json(
            {"type": "ready", "session_id": session_id, "sequence": 0}
        )

        try:
            async for message in ws:
                if message.type is WSMsgType.BINARY:
                    try:
                        validate_jpeg(message.data)
                    except StreamProtocolError as exc:
                        code = (
                            "FRAME_TOO_LARGE"
                            if len(message.data) > MAX_FRAME_BYTES
                            else "INVALID_JPEG"
                        )
                        close_code = (
                            WSCloseCode.MESSAGE_TOO_BIG
                            if code == "FRAME_TOO_LARGE"
                            else WSCloseCode.UNSUPPORTED_DATA
                        )
                        await self._error(
                            ws,
                            session_id=session_id,
                            sequence=last_sequence,
                            code=code,
                            message=str(exc),
                            retryable=False,
                            close_code=close_code,
                        )
                        break
                    last_sequence += 1
                    counters["server_dropped"] += session.replace(
                        last_sequence, message.data
                    )
                    continue

                if message.type is WSMsgType.TEXT and self._is_stop(message.data):
                    await cleanup()
                    await ws.send_json(
                        {
                            "type": "stopped",
                            "session_id": session_id,
                            "sequence": last_sequence,
                        }
                    )
                    await ws.close()
                    return ws

                await self._error(
                    ws,
                    session_id=session_id,
                    sequence=last_sequence,
                    code="PROTOCOL_ERROR",
                    message="expected a JPEG frame or stop message",
                    retryable=False,
                    close_code=WSCloseCode.PROTOCOL_ERROR,
                )
                break
        finally:
            cleanup_task = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise
        return ws

    @staticmethod
    def _is_stop(text: str) -> bool:
        try:
            return json.loads(text) == {"type": "stop"}
        except (TypeError, json.JSONDecodeError):
            return False

    @staticmethod
    def _reason(
        previous: str,
        current: str,
        presence_changed: bool,
        identity_changed: bool,
    ) -> str:
        if presence_changed:
            if current == "present":
                return "pose_confirmed"
            if current == "absent":
                return "absence_timeout"
            if current == "camera_error":
                return "camera_read_failed"
            if previous == "camera_error" and current == "starting":
                return "camera_recovered"
            return "initializing"
        if identity_changed:
            return "identity_changed"
        return "heartbeat"
