# Face Verification Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add local owner face verification to the existing camera presence stack and expose its stable result through the compatible presence API.

**Architecture:** The presence agent owns one camera loop and passes each frame to both pose and face inference. A revisioned snapshot carries independent presence and identity transitions through the existing idempotent reporter to a strictly validating in-memory Server registry.

**Tech Stack:** Python 3.13, OpenCV Contrib YuNet/SFace, NumPy, MediaPipe, aiohttp, pytest, PowerShell.

## Global Constraints

- Never upload frames, images, landmarks, face embeddings, or the owner template.
- Keep `schema_version=1.0` and accept reports without `identity`.
- Store the owner template only under ignored `presence-agent/.runtime/`.
- Use one camera capture loop during continuous monitoring.
- Face verification is advisory and must not authorize high-risk actions.

---

### Task 1: Face domain and local inference

**Files:**
- Create: `presence-agent/presence_agent/face_verifier.py`
- Create: `presence-agent/presence_agent/face_template.py`
- Test: `presence-agent/tests/test_face_verifier.py`
- Test: `presence-agent/tests/test_face_template.py`

**Interfaces:**
- Produces: `FaceState`, `FaceObservation`, `FaceStateTracker.update()`, `FaceVerifier.observe(frame)`, template load/save helpers.

- [ ] Write tests for 3-hit face states, 1-second no-face state, cosine threshold, multiple faces, and validated atomic template storage.
- [ ] Run the tests and confirm imports/behavior fail before implementation.
- [ ] Implement the smallest YuNet/SFace adapter, state tracker, embedding normalization, and template persistence that passes them.
- [ ] Run both test files and confirm they pass.

### Task 2: Revisioned Agent integration

**Files:**
- Modify: `presence-agent/presence_agent/snapshot.py`
- Modify: `presence-agent/presence_agent/reporter.py`
- Modify: `presence-agent/presence_agent/app.py`
- Modify: `presence-agent/presence_agent/render.py`
- Test: `presence-agent/tests/test_snapshot.py`
- Test: `presence-agent/tests/test_reporter.py`
- Test: `presence-agent/tests/test_app.py`

**Interfaces:**
- Consumes: `FaceVerifier.observe(frame) -> FaceObservation`.
- Produces: optional top-level `identity` report object.

- [ ] Add failing tests proving identity changes increment snapshot revision, serialize independently, and use the already-read frame.
- [ ] Run focused tests and confirm the expected failures.
- [ ] Add face arguments/factory, publish identity snapshots, and render the stable identity label.
- [ ] Run focused tests and confirm they pass without changing legacy payloads when identity is absent.

### Task 3: Server protocol compatibility

**Files:**
- Modify: `server/main/xiaozhi-server/core/presence_registry.py`
- Test: `server/main/xiaozhi-server/tests/test_presence_registry.py`
- Test: `server/main/xiaozhi-server/tests/test_presence_handler.py`

**Interfaces:**
- Consumes: optional request `identity` object.
- Produces: optional query response `data.identity` object.

- [ ] Add failing tests for valid identity, legacy payload compatibility, invalid states/counts/similarity, and query round trip.
- [ ] Run focused Server tests and confirm expected failures.
- [ ] Implement strict optional identity validation and immutable registry storage.
- [ ] Run focused Server tests and confirm they pass.

### Task 4: Enrollment and one-command operation

**Files:**
- Create: `presence-agent/presence_agent/face_enrollment.py`
- Create: `presence-agent/enroll-face.ps1`
- Modify: `presence-agent/run.ps1`
- Modify: `run-presence-stack.ps1`
- Modify: `presence-agent/requirements.txt`
- Modify: `.gitignore`
- Test: `presence-agent/tests/test_packaging.py`

**Interfaces:**
- Produces: `-EnrollOwner`, `-DeleteFaceTemplate`, face model/template/threshold parameters.

- [ ] Add failing packaging and command-line tests for bundled models, licenses, ignored templates and PowerShell parameters.
- [ ] Run focused tests and confirm failures.
- [ ] Add enrollment CLI/scripts and wire parameters through the root launcher.
- [ ] Run focused tests and PowerShell parse checks.

### Task 5: Documentation and verification

**Files:**
- Modify: `README.md`
- Modify: `presence-agent/README.md`
- Modify: `docs/api/camera-presence-api.md`

**Interfaces:**
- Documents: enrollment, launch, identity request/response, compatibility, privacy, and security limitations.

- [ ] Update documentation with exact commands and complete field/error behavior.
- [ ] Run all Python tests, desktop tests, TypeScript typecheck, compileall, and `git diff --check`.
- [ ] Commit with `feat: integrate local face verification`.
- [ ] Fetch/rebase latest `origin/master`, rerun key tests if the base changed, push normally, and verify the remote SHA.
