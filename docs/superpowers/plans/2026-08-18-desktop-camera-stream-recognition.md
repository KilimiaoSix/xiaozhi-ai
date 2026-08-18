# Desktop Camera Stream Recognition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `bounded-plan-execution` in this repository to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Repository policy forbids subagent-driven execution, worktrees, commits, pushes, and PRs unless the user explicitly requests them.

**Goal:** Replace timed desktop snapshots with a persistent WebSocket camera stream that lets Server perform pose presence and owner face verification on the same frames, including multi-frame PC enrollment and live similarity display.

**Architecture:** A root-level React provider owns the camera and monitoring intent across navigation. Electron main owns an authenticated `ws` connection and bounded transport. aiohttp owns a capacity-one frame session and runs the existing presence and face algorithms in a serialized thread-backed inference runtime. Server and presence-agent share one Python 3.10, NumPy 1.26, and OpenCV 4.11 dependency set.

**Tech Stack:** Electron 43, React 19, TypeScript 7, `ws`, Vitest, aiohttp 3.13, Python 3.10, MediaPipe 0.10.35, OpenCV contrib 4.11, NumPy 1.26, YuNet/SFace, pytest.

**Design:** [`../specs/2026-08-18-desktop-camera-stream-recognition-design.md`](../specs/2026-08-18-desktop-camera-stream-recognition-design.md)

**Execution graph:** [`../graphs/2026-08-18-desktop-camera-stream-recognition.json`](../graphs/2026-08-18-desktop-camera-stream-recognition.json)

## Global Constraints

- Monitoring remains active across page navigation, minimization, camera recovery, and Server reconnects until the user turns the switch off or the app exits.
- Registration and monitoring are mutually exclusive; registration never silently stops monitoring.
- One accepted JPEG is decoded once and passed to both pose and face inference.
- Every producer/consumer boundary is bounded to one in-flight or one latest frame.
- Raw frames, landmarks, embeddings, template contents, and auth tokens are never logged.
- Existing report/query APIs, presence-agent CLI, model files, and owner template schema stay compatible.
- Implement each task RED -> GREEN -> REFACTOR. Do not commit, push, create a worktree, or open a PR.

---

### Task 1: Unified Camera Inference Domain

**Files:**
- Create: `server/main/xiaozhi-server/core/camera_stream/__init__.py`
- Create: `server/main/xiaozhi-server/core/camera_stream/protocol.py`
- Create: `server/main/xiaozhi-server/core/camera_stream/inference.py`
- Create: `server/main/xiaozhi-server/core/camera_stream/enrollment.py`
- Create: `server/main/xiaozhi-server/requirements-camera.txt`
- Create: `server/main/xiaozhi-server/tests/test_camera_stream_protocol.py`
- Create: `server/main/xiaozhi-server/tests/test_camera_stream_inference.py`
- Create: `server/main/xiaozhi-server/tests/test_camera_stream_enrollment.py`
- Create: `presence-agent/pyproject.toml`
- Modify: `presence-agent/requirements.txt`
- Modify: `presence-agent/presence_agent/face_template.py`
- Modify: `presence-agent/tests/test_packaging.py`
- Modify: `presence-agent/tests/test_face_template.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class StreamStart:
    mode: Literal["monitoring", "enrollment"]
    session_id: UUID
    workstation_id: str
    display_name: str | None

class CameraInferenceRuntime:
    def process(self, jpeg: bytes, sequence: int, now: float) -> dict: ...

class EnrollmentCollector:
    def accept(self, frame, now: float) -> dict: ...
```

- [x] **Step 1: Write protocol RED tests.** Cover a valid monitoring start, enrollment requiring a trimmed non-empty `display_name`, invalid JSON/type/schema/UUID/workstation, extra fields, non-JPEG magic bytes, empty frame, and the 1 MiB frame limit.
- [x] **Step 2: Run protocol RED.** From `server/main/xiaozhi-server`, run `python -m pytest tests/test_camera_stream_protocol.py -q`. Expected: collection fails because `core.camera_stream.protocol` is missing.
- [x] **Step 3: Implement strict protocol parsing.** `parse_start(text) -> StreamStart` must reject undeclared fields and use the existing workstation regex. `validate_jpeg(bytes)` must check size and `FF D8 ... FF D9` markers before decode.
- [x] **Step 4: Run protocol GREEN.** Run the same command. Expected: all protocol tests pass.
- [x] **Step 5: Write same-frame inference and enrollment RED tests.** Inject fake decoder, pose detector, face engine, clock, template loader/saver. Assert one decode per JPEG, the same frame object reaches both detectors, monitoring emits similarity/matched, and enrollment enforces one face, quality, 200 ms spacing, 20 samples, trim-to-18, and atomic metadata/template completion.
- [x] **Step 6: Run inference/enrollment RED.** Run `python -m pytest tests/test_camera_stream_inference.py tests/test_camera_stream_enrollment.py -q`. Expected: missing runtime modules or behavior failures.
- [x] **Step 7: Implement inference, enrollment, and metadata.** Reuse presence-agent algorithms in-process, keep model objects serialized, and extend `face_template` with atomic metadata JSON.
- [x] **Step 8: Unify dependencies.** Pin `presence-agent` to `numpy==1.26.4` and `opencv-contrib-python==4.11.0.86`, add installable package metadata, and make `requirements-camera.txt` layer the local package onto the existing Server Python 3.10 environment without re-resolving unrelated Server dependencies.
- [x] **Step 9: Run Task 1 GREEN.** Run protocol/inference/enrollment tests with Server `.venv`; run presence-agent template, verifier, state, packaging, and integration tests under the unified versions. Run `pip check` and real YuNet/SFace model initialization. Expected: all pass in Python 3.10.

### Task 2: aiohttp WebSocket Session and Presence Registry Integration

**Files:**
- Create: `server/main/xiaozhi-server/core/api/camera_stream_handler.py`
- Create: `server/main/xiaozhi-server/core/camera_stream/session.py`
- Create: `server/main/xiaozhi-server/tests/test_camera_stream_session.py`
- Create: `server/main/xiaozhi-server/tests/test_camera_stream_handler.py`
- Modify: `server/main/xiaozhi-server/core/presence_routes.py`
- Modify: `server/main/xiaozhi-server/core/http_server.py`
- Modify: `server/main/xiaozhi-server/presence_server.py`
- Modify: `server/main/xiaozhi-server/core/presence_registry.py`
- Modify: `server/main/xiaozhi-server/tests/test_presence_routes.py`
- Modify: `server/main/xiaozhi-server/tests/test_presence_registry.py`

**Interfaces:**

```python
class LatestFrameSlot:
    def replace(self, sequence: int, jpeg: bytes) -> int: ...  # returns dropped delta
    async def take(self) -> Frame | None: ...
    def close(self) -> None: ...

class CameraStreamHandler:
    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse: ...
```

- [x] **Step 1: Write session RED tests.** Prove capacity one replaces the older pending frame, processed frames are never duplicated, close wakes a waiting consumer, and cancellation leaves no task running.
- [x] **Step 2: Run session RED.** Run `python -m pytest tests/test_camera_stream_session.py -q`. Expected: missing session imports.
- [x] **Step 3: Implement latest-frame session.** Keep receive and processing loops separate; run synchronous decode/inference through `asyncio.to_thread`; count client/server dropped and processed frames; allow only one inference call in flight.
- [x] **Step 4: Write aiohttp WebSocket RED tests.** Use fake runtime/enrollment factories and assert: Bearer auth, start-first requirement, ready, binary monitoring result, enrollment progress/complete, 1 MiB rejection, malformed JPEG error, stop/stopped, disconnect cleanup, and registry query after a monitoring result.
- [x] **Step 5: Run handler RED.** Run `python -m pytest tests/test_camera_stream_handler.py tests/test_presence_routes.py -q`. Expected: missing handler and route failures.
- [x] **Step 6: Implement handler and route.** Register `GET /xiaozhi/presence/stream`; require existing auth during upgrade; return stable `PROTOCOL_ERROR`, `FRAME_TOO_LARGE`, `INVALID_JPEG`, `MODEL_UNAVAILABLE`, and `INFERENCE_ERROR` events with `retryable` flags; close with appropriate WebSocket codes.
- [x] **Step 7: Publish compatible registry records.** For each stable monitoring result, build a valid internal `PresenceReport` with generated event/agent UUIDs, increasing sequence, `source=camera_pose`, and the current optional identity payload. Do not call the HTTP report endpoint internally.
- [x] **Step 8: Make camera inference lazy at Server startup.** `SimpleHttpServer` and `create_presence_app` construct a lazy runtime factory. Missing ML dependencies/models only cause `MODEL_UNAVAILABLE` for the camera stream; inference failures become `INFERENCE_ERROR`; existing OTA, event, report, query, voice, and robot routes still start.
- [x] **Step 9: Run Task 2 GREEN.** Run `python -m pytest tests/test_camera_stream_session.py tests/test_camera_stream_handler.py tests/test_presence_registry.py tests/test_presence_handler.py tests/test_presence_routes.py -q`. Expected: all pass.

### Task 3: Electron Main WebSocket Transport and IPC Contract

**Files:**
- Create: `desktop/src/main/camera/cameraStreamClient.ts`
- Create: `desktop/src/main/camera/cameraStreamClient.test.ts`
- Create: `desktop/src/main/camera/cameraStreamIpc.ts`
- Create: `desktop/src/main/camera/cameraStreamIpc.test.ts`
- Modify: `desktop/src/main/camera/registerCameraIpc.ts`
- Modify: `desktop/src/main.ts`
- Modify: `desktop/src/preload.ts`
- Modify: `desktop/src/shared/contracts.ts`
- Modify: `desktop/src/modules/features/camera-capture/types.ts`
- Modify: `desktop/package.json`
- Modify: `desktop/package-lock.json`
- Delete: `desktop/src/main/camera/cameraHttpClient.ts`
- Delete: `desktop/src/main/camera/cameraHttpClient.test.ts`
- Delete: `desktop/src/modules/features/camera-capture/services/frameUploadScheduler.ts`
- Delete: `desktop/src/modules/features/camera-capture/services/frameUploadScheduler.test.ts`

**Interfaces:**

```ts
type StreamMode = 'monitoring' | 'enrollment';
type FrameSendResult = 'sent' | 'dropped' | 'not-ready';

interface CameraRecognitionDesktopApi {
  start(options: RecognitionStreamOptions): Promise<void>;
  sendFrame(jpeg: ArrayBuffer): Promise<FrameSendResult>;
  stop(): Promise<void>;
  onEvent(listener: (event: RecognitionEvent) => void): () => void;
}
```

- [x] **Step 1: Install and type the transport.** Add runtime dependency `ws` and development dependency `@types/ws` with `npm install ws && npm install -D @types/ws` from `desktop`; inspect lockfile diff and keep only those dependency changes.
- [x] **Step 2: Write transport RED tests.** Inject a fake WebSocket/timers/random UUID. Cover HTTP-to-WS URL conversion, Authorization header without logging, start control message, binary send, 1 MiB bufferedAmount drop, result forwarding, 1/2/4/8/16/30-second capped reconnect that continues indefinitely while enabled, and stop cancelling reconnect.
- [x] **Step 3: Run transport RED.** Run `npm test -- src/main/camera/cameraStreamClient.test.ts`. Expected: missing module failure.
- [x] **Step 4: Implement `CameraStreamClient`.** Read `XIAOFEI_SERVER_URL` and `XIAOFEI_SERVER_AUTH_TOKEN` only in main; keep the user monitoring intent separate from connection state; generate a fresh session ID on reconnect; never put auth data in renderer events.
- [x] **Step 5: Write IPC RED tests.** Prove one client per app, sender ownership, sanitized argument validation, frame ArrayBuffer transfer, event forwarding only to live windows, active-monitoring state, and cleanup on app quit.
- [x] **Step 6: Implement IPC and lifecycle.** Replace flat HTTP camera IPC with `camera:recognition-start/frame/stop`; expose the typed nested preload API; return a cleanup function from registration; make `before-quit` await/trigger stop.
- [x] **Step 7: Implement close-to-minimize behavior.** While monitoring is active and app quit has not begun, prevent the main window close and call `minimize()`; allow close after `before-quit`. Keep normal close behavior when monitoring is off.
- [x] **Step 8: Remove obsolete snapshot upload code.** Delete `CameraHttpClient` and `FrameUploadScheduler`; verify `rg -n "/api/vision/frames|setInterval\(|uploadMonitoringFrame" desktop/src` returns no monitoring transport usage.
- [x] **Step 9: Run Task 3 GREEN.** Run `npm test -- src/main/camera/cameraStreamClient.test.ts src/main/camera/cameraStreamIpc.test.ts` and `npm run typecheck`. Expected: all tests pass and TypeScript reports no errors.

### Task 4: App-Level Persistent Camera Provider and Recognition UI

**Files:**
- Create: `desktop/src/modules/features/camera-capture/context/CameraMonitoringProvider.tsx`
- Create: `desktop/src/modules/features/camera-capture/context/CameraMonitoringProvider.test.tsx`
- Create: `desktop/src/modules/features/camera-capture/services/videoFrameProducer.ts`
- Create: `desktop/src/modules/features/camera-capture/services/videoFrameProducer.test.ts`
- Modify: `desktop/src/renderer.tsx`
- Modify: `desktop/src/renderer/App.tsx`
- Modify: `desktop/src/modules/features/camera-capture/CameraPage.tsx`
- Modify: `desktop/src/modules/features/camera-capture/CameraPage.test.tsx`
- Modify: `desktop/src/modules/features/camera-capture/components/CameraPreview.tsx`
- Modify: `desktop/src/modules/features/camera-capture/components/OwnerEnrollment.tsx`
- Modify: `desktop/src/modules/features/camera-capture/components/OwnerEnrollment.test.tsx`
- Modify: `desktop/src/modules/features/camera-capture/components/PresenceMonitoring.tsx`
- Modify: `desktop/src/modules/features/camera-capture/hooks/useCameraStream.ts`
- Modify: `desktop/src/modules/features/camera-capture/state/cameraReducer.ts`
- Modify: `desktop/src/modules/features/camera-capture/state/cameraReducer.test.ts`
- Modify: `desktop/src/modules/features/camera-capture/camera.css`

**Interfaces:**

```ts
interface CameraMonitoringValue {
  enabled: boolean;
  stream: MediaStream | null;
  connection: 'idle' | 'connecting' | 'online' | 'reconnecting';
  presence: PresenceRecognitionState;
  identity: FaceRecognitionState;
  metrics: RecognitionMetrics;
  startMonitoring(): Promise<void>;
  stopMonitoring(): Promise<void>;
  startEnrollment(displayName: string): Promise<void>;
  cancelEnrollment(): Promise<void>;
}
```

- [x] **Step 1: Write frame producer RED tests.** Fake `requestVideoFrameCallback`, canvas, clock, and gateway. Assert 5 FPS gating, 640×360 aspect-preserving dimensions, JPEG quality 0.72, one encode/send in flight, skip/drop accounting, and cancellation preventing future callbacks.
- [x] **Step 2: Run producer RED.** Run `npm test -- src/modules/features/camera-capture/services/videoFrameProducer.test.ts`. Expected: missing module failure.
- [x] **Step 3: Implement frame producer.** Use one hidden video/canvas owned by the producer, never `setInterval`; keep callback scheduling alive while monitoring intent is true even when the gateway reports `not-ready`.
- [x] **Step 4: Write provider RED tests.** Assert monitoring survives camera page unmount and App navigation, minimized/background state does not stop, Server errors keep `enabled=true`, reconnect events update status, track-ended restarts the selected device, manual stop is the only runtime stop path, and unmount at application root performs final cleanup.
- [x] **Step 5: Implement root provider.** Mount it around `<App />` in `renderer.tsx`; move camera stream, monitoring intent, transport events, metrics, and recovery out of `CameraPage`; keep enrollment state app-level only for its active operation.
- [x] **Step 6: Write registration and monitoring UI RED tests.** Assert monitoring disables enrollment without stopping it; registration shows accepted/20 and quality reason; completion stops registration camera; result UI distinguishes person presence, face presence, owner/unknown/multiple/no-face/not-enrolled, shows similarity only when supplied, and renders matched from Server rather than recalculating.
- [x] **Step 7: Implement the UI and reducer/types.** Replace “每秒发送一张” and `1 FPS` copy with continuous-stream status. Keep compact metrics with fixed dimensions so changing labels/counts do not shift layout. Add an App-level compact monitoring indicator outside the camera page.
- [x] **Step 8: Run Task 4 GREEN.** Run `npm test -- src/modules/features/camera-capture` and `npm run typecheck`. Expected: all camera tests and TypeScript checks pass.

### Task 5: Integration, Documentation, and Completion Verification

**Files:**
- Create: `server/main/xiaozhi-server/tests/test_camera_stream_integration.py`
- Modify: `docs/api/camera-presence-api.md`
- Modify: `README.md`
- Modify: `presence-agent/README.md`
- Modify: `docs/superpowers/specs/2026-08-18-desktop-camera-stream-recognition-design.md`
- Modify: `docs/superpowers/plans/2026-08-18-desktop-camera-stream-recognition.md`

**Interfaces:**
- Documents and proves the PC registration -> persistent frame stream -> same-frame pose/face inference -> Registry/UI chain.

- [x] **Step 1: Write end-to-end RED integration test.** Start a real aiohttp app with fake deterministic decoder/models, connect using an aiohttp WebSocket client, enroll 20 accepted frames, assert template completion, reconnect in monitoring mode, send owner/unknown/no-face sequences, assert live results, then query `PresenceRegistry` and assert compatible presence plus identity state.
- [x] **Step 2: Run integration RED.** From `server/main/xiaozhi-server`, run `python -m pytest tests/test_camera_stream_integration.py -q`. Expected: failure until all route/runtime pieces are integrated.
- [x] **Step 3: Complete integration wiring and docs.** Update the API document with WebSocket start/event/error schemas, auth, frame limits, persistent monitoring lifecycle, registration flow, privacy limits, state tables, and smoke commands. Update README startup instructions and clarify that standalone presence-agent remains optional compatibility tooling.
- [x] **Step 4: Run Python verification.** Run `python -m pytest tests -q` from `server/main/xiaozhi-server`; run `python -m pytest tests -q` from `presence-agent`; run `python -m compileall core presence_server.py` from Server. Expected: all pass and compileall reports no syntax errors.
- [x] **Step 5: Run desktop verification.** From `desktop`, run `npm test`, `npm run typecheck`, and `npm run package`. Expected: all pass and Forge produces the packaged app.
- [x] **Step 6: Run static acceptance checks.** From repository root, run `rg -n "/api/vision/frames|setInterval\(|uploadMonitoringFrame" desktop/src` and expect no obsolete monitoring transport; run `git diff --check` and expect no output; inspect `git status --short` and preserve all user-owned changes.
- [x] **Step 7: Run manual hardware smoke.** Start Server and desktop, enroll the current user, enable monitoring, verify owner/unknown/no-face and similarity, navigate home, minimize for 30 seconds, restart Server and observe automatic recovery, then switch monitoring off and confirm the camera indicator turns off. Record any unavailable hardware-only evidence explicitly rather than replacing it with a unit-test claim.

## Completion Evidence

- Server: `89 passed`; integration flow: `1 passed`; `compileall` passed.
- presence-agent: `59 passed, 1 skipped`（macOS 无 PowerShell）；`pip check` 无冲突。
- Desktop（合入远程更新后）: `35` 个测试文件、`109 passed`；TypeScript 检查和 macOS arm64 Forge 打包通过。
- 真实模型 smoke：NumPy 1.26.4、OpenCV 4.11.0、MediaPipe 0.10.35 成功初始化并处理 640×360 JPEG。
- 静态检查：旧 HTTP 帧路径、旧上传 API、摄像头 `setInterval` 均不存在；`git diff --check` 通过。
- 物理摄像头下的真人/陌生人识别、最小化 30 秒及人工重启 Server 需要用户在本机 UI 中完成，未用自动化结果冒充硬件验收。
