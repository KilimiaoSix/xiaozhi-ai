# Camera Presence Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将本地摄像头在岗检测以独立 sidecar 接入 launchcrush Server，提供稳定查询接口、轻量联调入口和 Windows 一键启动能力。

**Architecture:** `presence-agent` 在本机完成 MediaPipe 推理和防抖，只向 Server 上报版本化状态。Server 通过无外部依赖的 `PresenceRegistry` 保存每个工位最新值，由 aiohttp handler 提供上报和查询接口；完整 Server 与轻量 presence-only Server 复用同一领域层及路由。

**Tech Stack:** Python 3.10-3.13、aiohttp 3.13.2、MediaPipe 1.0.1、OpenCV 5.0.0.93、pytest 9.1.1、PowerShell。

## Global Constraints

- 摄像头帧、截图和完整人体 landmark 永不离开 presence-agent 进程。
- 主 Server 不新增 MediaPipe 或 OpenCV 依赖。
- agent 和 Server 的状态阈值固定为：3 个连续正样本确认 present、2 秒确认 absent、15 秒心跳、超过 30 秒派生 stale。
- Server 仅保存最新状态，不保存历史、不直接触发机器人动作。
- 新增接口使用 `{code, message, data}` 响应结构并沿用现有 Bearer Token 配置语义。
- 所有生产行为严格执行 RED-GREEN-REFACTOR；必须先观察测试因缺失行为而失败。
- 按用户要求在当前 `gitrepo` 的 `master` 工作区内实施；不创建 commit，不 push。

---

### Task 1: Server Presence Domain and Registry

**Files:**
- Create: `server/main/xiaozhi-server/core/presence_registry.py`
- Create: `server/main/xiaozhi-server/tests/test_presence_registry.py`

**Interfaces:**
- Produces: `PresenceReport.from_payload(payload, now_utc) -> PresenceReport`
- Produces: `PresenceRegistry(clock, stale_after_seconds=30.0).accept(report)`
- Produces: `PresenceRegistry.get(workstation_id) -> dict | None`
- Produces: `PresenceValidationError`, `PresenceOutOfOrderError`

- [ ] **Step 1: Write failing validation tests**

Test a complete valid report and parameterize invalid schema version, UUIDs, workstation ID, state, `changed`, reason, boolean sequence, naive/future timestamp, unexpected metric, and metric ranges. Assert `PresenceValidationError` contains a stable field-oriented message.

```python
def test_parses_valid_report(valid_payload, now_utc):
    report = PresenceReport.from_payload(valid_payload, now_utc)
    assert report.workstation_id == "desk-test"
    assert report.state == "present"
    assert report.sequence == 1

@pytest.mark.parametrize("field,value", [("schema_version", "2.0"), ("sequence", True)])
def test_rejects_invalid_fields(valid_payload, now_utc, field, value):
    valid_payload[field] = value
    with pytest.raises(PresenceValidationError):
        PresenceReport.from_payload(valid_payload, now_utc)
```

- [ ] **Step 2: Run validation tests and observe RED**

Run from `server/main/xiaozhi-server`:

```powershell
python -m pytest tests/test_presence_registry.py -q
```

Expected: collection fails because `core.presence_registry` does not exist.

- [ ] **Step 3: Implement immutable report parsing**

Use frozen dataclasses, `uuid.UUID`, timezone-aware `datetime.fromisoformat`, a compiled workstation regex, explicit key sets, finite-number checks, and exact enums from the spec. Normalize `observed_at` to UTC while retaining JSON-safe primitives for output.

- [ ] **Step 4: Run validation tests and observe GREEN**

Run the same command. Expected: all parsing tests pass.

- [ ] **Step 5: Write failing registry tests**

Cover first report, latest-event idempotency, same-instance lower/equal sequence rejection, new-instance sequence 1 takeover, old-instance delayed rejection, unknown workstation, and the exact stale boundary.

```python
def test_state_becomes_stale_only_after_threshold(registry, report, clock):
    registry.accept(report)
    clock.advance(30.0)
    assert registry.get("desk-test")["effective_state"] == "present"
    clock.advance(0.001)
    assert registry.get("desk-test")["effective_state"] == "stale"
```

- [ ] **Step 6: Observe RED, implement registry, then observe GREEN**

The registry stores a record per workstation containing the report, UTC receipt time, monotonic receipt time, active instance, latest sequence, and latest event ID. Duplicate latest events return an acceptance result with `duplicate=True`; ordering violations raise `PresenceOutOfOrderError`. `get()` returns a new JSON-safe dict so callers cannot mutate internal state.

Run:

```powershell
python -m pytest tests/test_presence_registry.py -q
```

Expected: all registry tests pass.

### Task 2: HTTP Handler, Routes, and Lightweight Server

**Files:**
- Create: `server/main/xiaozhi-server/core/api/presence_handler.py`
- Create: `server/main/xiaozhi-server/core/presence_routes.py`
- Create: `server/main/xiaozhi-server/presence_server.py`
- Create: `server/main/xiaozhi-server/tests/test_presence_handler.py`
- Create: `server/main/xiaozhi-server/tests/test_presence_routes.py`
- Modify: `server/main/xiaozhi-server/core/http_server.py`
- Modify: `server/main/xiaozhi-server/app.py`

**Interfaces:**
- Consumes: Task 1 registry and exceptions.
- Produces: `PresenceHandler(config, registry)` with `handle_report`, `handle_get`, `handle_options`.
- Produces: `add_presence_routes(app, handler) -> None`.
- Produces: `create_presence_app(config) -> aiohttp.web.Application`.

- [ ] **Step 1: Write failing aiohttp contract tests**

Build an aiohttp app with `PresenceHandler` and test success report/query, duplicate response, 400 invalid JSON/object/fields, 401 auth, 404 unknown, 409 ordering, 413 payload, CORS headers, and URL-decoded path validation.

```python
async def test_report_then_query(aiohttp_client, valid_payload, config):
    app = web.Application()
    registry = PresenceRegistry()
    add_presence_routes(app, PresenceHandler(config, registry))
    client = await aiohttp_client(app)
    response = await client.post("/xiaozhi/presence/report", json=valid_payload)
    assert response.status == 200
    assert (await response.json())["data"]["accepted"] is True
```

- [ ] **Step 2: Run handler tests and observe RED**

```powershell
python -m pytest tests/test_presence_handler.py tests/test_presence_routes.py -q
```

Expected: missing handler/routes imports.

- [ ] **Step 3: Implement handler and route registration**

Read at most 16 KiB, parse JSON with the standard parser, return the exact envelope/error codes in the spec, compare optional Bearer Token without logging it, and apply existing CORS header names. Register POST/GET/OPTIONS independently of `ws_server`.

- [ ] **Step 4: Implement lightweight app and production integration**

`presence_server.py` parses host/port/auth CLI arguments and calls `web.run_app(create_presence_app(config))`. `SimpleHttpServer` owns a registry/handler and invokes `add_presence_routes` unconditionally before its existing routes. Add startup log lines for report and query endpoints.

- [ ] **Step 5: Run focused and domain tests**

```powershell
python -m pytest tests/test_presence_registry.py tests/test_presence_handler.py tests/test_presence_routes.py -q
```

Expected: all Server presence tests pass.

### Task 3: Agent Detection State and Snapshot Model

**Files:**
- Create: `presence-agent/presence_agent/__init__.py`
- Create: `presence-agent/presence_agent/state.py`
- Create: `presence-agent/presence_agent/pose_detector.py`
- Create: `presence-agent/presence_agent/snapshot.py`
- Create: `presence-agent/tests/test_state.py`
- Create: `presence-agent/tests/test_pose_detector.py`
- Create: `presence-agent/tests/test_snapshot.py`

**Interfaces:**
- Produces: `PresenceState`, `PresenceTracker.update(detected, now_seconds, camera_ok=True)`.
- Produces: `PoseObservation.is_present(min_confidence=0.5)` and `diagnostic_metrics()`.
- Produces: `LatestSnapshot.publish(...)`, `LatestSnapshot.read() -> PresenceSnapshot`.

- [ ] **Step 1: Copy behavior as tests, not production code**

Recreate the validated demo assertions for 3-hit confirmation, 2-second absence, no-positive startup absence, camera error reset, 4-of-7 visible core points, and required shoulder. Add snapshot tests proving same-state metrics updates do not increment revision while state changes do.

- [ ] **Step 2: Observe RED**

```powershell
python -m pytest presence-agent/tests/test_state.py presence-agent/tests/test_pose_detector.py presence-agent/tests/test_snapshot.py -q
```

Expected: package modules are missing.

- [ ] **Step 3: Implement minimal state, pose, and snapshot modules**

Port only local state/detector behavior, add diagnostic metric extraction, and protect latest snapshot reads/writes with `threading.Lock`. A state change records previous state/reason and increments revision; a same-state observation only refreshes observation time/metrics.

- [ ] **Step 4: Observe GREEN**

Run the same command. Expected: all agent domain tests pass.

### Task 4: Versioned Reporter and HTTP Transport

**Files:**
- Create: `presence-agent/presence_agent/reporter.py`
- Create: `presence-agent/tests/test_reporter.py`

**Interfaces:**
- Consumes: `LatestSnapshot.read()`.
- Produces: `PresenceReporter.step(now_monotonic) -> float` and `run(stop_event)`.
- Produces: `HttpPresenceTransport.send(payload) -> dict`.

- [ ] **Step 1: Write failing reporter tests**

Use an in-memory transport and injected UUID/UTC/monotonic clocks. Assert initial report, immediate transition report, 15-second heartbeat, stable payload fields, same-event retry, replacement by a newer revision, backoff sequence `1,2,4,8,16,30,30`, and reset after success.

```python
def test_retry_keeps_event_identity(reporter, transport, snapshot):
    transport.failures_remaining = 1
    reporter.step(0.0)
    first = transport.attempts[-1]
    reporter.step(1.0)
    assert transport.attempts[-1]["event_id"] == first["event_id"]
    assert transport.attempts[-1]["sequence"] == first["sequence"]
```

- [ ] **Step 2: Observe RED**

```powershell
python -m pytest presence-agent/tests/test_reporter.py -q
```

Expected: reporter module missing.

- [ ] **Step 3: Implement reporter and transport**

Use standard-library `urllib.request` with a finite timeout. Never include image data or log auth headers. `step()` returns seconds until the next useful call, allowing deterministic tests; `run()` waits on `stop_event` for that duration. Replace a pending event only when snapshot revision changes.

- [ ] **Step 4: Observe GREEN and run all agent domain tests**

```powershell
python -m pytest presence-agent/tests -q
```

Expected: all current agent tests pass.

### Task 5: Camera Application and Process Integration

**Files:**
- Create: `presence-agent/presence_agent/render.py`
- Create: `presence-agent/presence_agent/app.py`
- Create: `presence-agent/tests/test_app.py`
- Create: `presence-agent/tests/test_integration.py`

**Interfaces:**
- Consumes: Tasks 3-4 modules.
- Produces: `parse_args(argv)`, `run(args) -> int`, `main(argv=None) -> int`.

- [ ] **Step 1: Write failing CLI and camera lifecycle tests**

Use fake camera, detector, clocks, and reporter thread. Cover defaults, invalid positive values, missing model exit 2, smoke-frame completion, camera open/read error publication, camera recovery to starting, preview-off behavior, release/close cleanup, and monotonic MediaPipe timestamps.

- [ ] **Step 2: Observe RED**

```powershell
python -m pytest presence-agent/tests/test_app.py -q
```

Expected: app module missing.

- [ ] **Step 3: Implement CLI and camera loop**

Default to headless operation. Run the reporter on a daemon thread with a stop event. Open the camera with DirectShow on Windows, retry camera failures without exiting, process `--smoke-frames` successful frames, and only import/render UI dependencies when `--preview` is enabled.

- [ ] **Step 4: Add real aiohttp integration test**

Start `create_presence_app` on an ephemeral port, use an in-memory/fake snapshot source with real `HttpPresenceTransport`, and query the registry through HTTP. Assert final `effective_state` and that serialized requests contain no forbidden image/landmark fields.

- [ ] **Step 5: Observe GREEN**

```powershell
python -m pytest presence-agent/tests server/main/xiaozhi-server/tests -q
```

Expected: agent and Server tests pass together.

### Task 6: Reproducible Packaging and One-Command Startup

**Files:**
- Create: `presence-agent/requirements.txt`
- Create: `presence-agent/requirements-test.txt`
- Create: `presence-agent/setup.ps1`
- Create: `presence-agent/run.ps1`
- Create: `presence-agent/README.md`
- Create binary: `presence-agent/models/pose_landmarker_lite.task`
- Create: `run-presence-stack.ps1`
- Modify: `.gitignore`
- Create: `presence-agent/tests/test_packaging.py`

**Interfaces:**
- Produces: `presence-agent/run.ps1` for local/remote Server usage.
- Produces: root `run-presence-stack.ps1` for compatible Server reuse, full Server opt-in, or presence-only fallback.

- [ ] **Step 1: Write failing packaging tests**

Assert required scripts/files exist, requirements are exact pins, model SHA-256 equals the spec, scripts parse under PowerShell, secrets are not literalized, `.venv/.runtime` are ignored, and README commands match CLI flags.

- [ ] **Step 2: Observe RED**

```powershell
python -m pytest presence-agent/tests/test_packaging.py -q
```

Expected: packaging files are missing.

- [ ] **Step 3: Add pinned dependencies and bundled model**

Runtime pins are MediaPipe `1.0.1`, OpenCV contrib `5.0.0.93`, and aiohttp `3.13.2`; test pin is pytest `9.1.1` plus pytest-aiohttp compatible with aiohttp 3.13. Copy the already validated 5,777,746-byte model and verify its SHA-256.

- [ ] **Step 4: Implement setup and launch scripts**

`setup.ps1` creates/updates the isolated environment idempotently. `run.ps1` bootstraps when needed and passes explicit CLI values without printing Token. Root script health-checks an existing Server, starts full Server only with `-ServerPython`, otherwise starts hidden presence-only Server with runtime logs, waits for readiness, runs agent in foreground, and cleans up only its own child.

- [ ] **Step 5: Observe GREEN and run script smoke checks**

```powershell
python -m pytest presence-agent/tests/test_packaging.py -q
powershell -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw ./presence-agent/run.ps1)); [void][scriptblock]::Create((Get-Content -Raw ./run-presence-stack.ps1))"
```

Expected: tests pass and PowerShell parser exits 0.

### Task 7: API Document, Full Verification, and Runtime Smoke

**Files:**
- Create: `docs/api/camera-presence-api.md`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-18-camera-presence-integration-design.md` only if implementation evidence requires a factual correction.

**Interfaces:**
- Documents: exact implemented report/query endpoints, fields, curl, JSON, errors, states, integration guidance, and compatibility.

- [ ] **Step 1: Write API integration document from implemented behavior**

Follow `writing-frontend-api-doc`: first-screen overview with interface nature/action, integration conclusion, one section per endpoint with path/method/input/output/curl/JSON, change summary, error codes, enums, state machine, polling guidance, and explicit handling of 404/camera_error/stale.

- [ ] **Step 2: Update root README deployment entry points**

Add concise commands for one-command local demo, sidecar against remote Server, direct query, prerequisites, privacy boundary, and links to spec/API document.

- [ ] **Step 3: Run fresh full automated verification**

```powershell
presence-agent\.venv\Scripts\python.exe -m pytest presence-agent/tests server/main/xiaozhi-server/tests -q
Set-Location desktop
npm test
npm run typecheck
```

Expected: all Python and desktop tests pass, TypeScript exits 0.

- [ ] **Step 4: Run process-level API smoke**

Start `presence_server.py` on an unused local port, POST a deterministic sample, GET it back, stop the process, and assert the response envelope and state.

- [ ] **Step 5: Run real-camera smoke**

```powershell
.\presence-agent\run.ps1 -ServerUrl http://127.0.0.1:<port> -WorkstationId desk-smoke -SmokeFrames 30
```

Expected: 30 frames process successfully, process exits 0, and GET returns the latest non-stale agent-reported state.

- [ ] **Step 6: Final hygiene and requirements audit**

Run `git diff --check`, scan tracked/untracked files for secrets and forbidden image payload fields, verify model hash, inspect `git status`, and compare every acceptance criterion in the spec against implementation/tests/docs. Leave all changes uncommitted for user review.
