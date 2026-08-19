# Hardware Control API Documentation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce and publish an implementation-accurate API document explaining how LaunchCrush Server controls the ESP32-S3 robot.

**Architecture:** Document the public HTTP control plane separately from the internal WebSocket transport. Treat `master` as the released contract and isolate the unmerged `origin/feat/robot-push-and-actions` fields in a clearly labeled candidate-extension section.

**Tech Stack:** Markdown, aiohttp HTTP API, Python WebSocket/MCP client, ESP-IDF firmware, Feishu Docx v2.

## Global Constraints

- Write the repository document to `server/docs/hardware-control-api.md`.
- Document only behavior verified in source; do not present unmerged fields as available.
- Each HTTP endpoint must include path, method, inputs, outputs, curl, and complete JSON examples.
- Publish the completed Markdown to public Feishu, grant `tfzhang11@iflytek.com` full access, verify Chinese content, and deliver it by bot card.

---

### Task 1: Write and verify the repository API document

**Files:**
- Create: `server/docs/hardware-control-api.md`

**Interfaces:**
- Consumes: `GET /xiaozhi/event/devices`, `POST /xiaozhi/event/push`, WebSocket `alert`, and MCP `tools/call` behavior from the current source tree.
- Produces: A standalone Markdown integration document usable by desktop and Server developers.

- [x] **Step 1: Write the API overview and current/pending status boundary**

Describe HTTP port `8003`, WebSocket port `8000`, device registration, authentication, and the distinction between current `master` and `origin/feat/robot-push-and-actions`.

- [x] **Step 2: Document every public HTTP endpoint**

For both endpoints, include all six mandatory sections and exact success/error response shapes.

- [x] **Step 3: Document internal hardware protocols**

Describe the `alert` frame, MCP JSON-RPC `tools/call`, firmware dispatch, action queue, and servo execution path.

- [x] **Step 4: Run document self-checks**

Run:

```powershell
rg -n "TBD|TODO|待补充" server/docs/hardware-control-api.md
git diff --check -- server/docs/hardware-control-api.md
```

Expected: no placeholder matches and `git diff --check` exits `0`.

### Task 2: Publish and verify the Feishu document

**Files:**
- Consume: `server/docs/hardware-control-api.md`

**Interfaces:**
- Consumes: The verified repository Markdown.
- Produces: A Feishu Docx document, full-access permission for the current user, and a bot direct-message card.

- [x] **Step 1: Create the Feishu document as bot**

Run `lark-cli docs +create --api-version v2 --as bot --doc-format markdown` with UTF-8 stdin from the repository Markdown.

- [x] **Step 2: Verify creation output and document outline**

Require `ok=true`, no warnings, and a fetched outline containing the Chinese title and endpoint headings without `????` corruption.

- [x] **Step 3: Grant full access**

If automatic grant is not `granted`, call the Drive permission member API with the current user's `open_id` and `perm=full_access`.

- [x] **Step 4: Deliver by bot card**

Send an interactive direct-message card to the current user's `open_id`, with the document title, delivery summary, and an `打开文档` button.
