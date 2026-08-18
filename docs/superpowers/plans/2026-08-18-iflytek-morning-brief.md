# i讯飞每日关注晨报 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 launchcrush 的 Python 服务端实现只读的 i讯飞消息、`@我`、日程聚合，以及可解释的每日 Top 3 关注项预览 API。

**Architecture:** 通过用户访问令牌调用私有 i讯飞 OpenAPI，采集器负责分页和响应校验，服务层负责归一化、去重、回复识别、确定性排序和覆盖率汇总。SQLite 仅保存消息标识、短摘要和处理状态，HTTP 层复用现有 Bearer 鉴权与 JSON envelope；缺少权限或单源失败时输出可诊断的 `PARTIAL/FAILED`，不宣称客户端“真实未读”。

**Tech Stack:** Python 3.13、aiohttp、标准库 sqlite3/dataclasses/zoneinfo、pytest、pytest-aiohttp。

## Global Constraints

- 仅实现 Phase 0/1：能力验证、只读采集、本地台账、Top 3、预览/最近结果/健康检查。
- 不实现定时外发、卡片回调或客户端未读游标模拟。
- 用户文案统一使用“待关注/未处理”，不得标成“真实未读”。
- 外部请求必须使用用户访问令牌；令牌只能来自本地覆盖配置或 `IFLYTEK_USER_ACCESS_TOKEN` 环境变量。
- SQLite 不保存完整消息正文，短摘要默认不超过 240 字符。
- 所有来源必须报告页数、条数、状态和错误；分页未完成时不得推进消息水位。

---

### Task 1: 领域模型、归一化与确定性排序

**Files:**
- Create: `server/main/xiaozhi-server/core/morning_brief/__init__.py`
- Create: `server/main/xiaozhi-server/core/morning_brief/models.py`
- Create: `server/main/xiaozhi-server/core/morning_brief/ranking.py`
- Test: `server/main/xiaozhi-server/tests/test_morning_brief_models.py`
- Test: `server/main/xiaozhi-server/tests/test_morning_brief_ranking.py`

**Interfaces:**
- Produces: `normalize_message(raw, self_open_id, excerpt_chars) -> AttentionItem`
- Produces: `normalize_calendar_event(raw, timezone_name) -> CalendarItem`
- Produces: `rank_candidates(items, calendar_items, limit=3) -> list[RankedItem]`

- [ ] **Step 1: Write failing normalization tests**

```python
def test_direct_mention_and_at_all_are_distinguished():
    direct = normalize_message(raw_message(mentions=[{"id": "ou_me"}]), "ou_me", 240)
    broadcast = normalize_message(raw_message(mentions=[{"id": "all"}]), "ou_me", 240)
    assert direct.mention_kind == "DIRECT"
    assert broadcast.mention_kind == "ALL"
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `../../../.venv/Scripts/python.exe -m pytest tests/test_morning_brief_models.py -q`

Expected: collection fails because `core.morning_brief.models` does not exist.

- [ ] **Step 3: Implement immutable normalized models**

```python
@dataclass(frozen=True)
class AttentionItem:
    message_id: str
    topic_id: str
    sender_id: str
    chat_id: str
    mention_kind: str
    short_excerpt: str
    source_timestamp: datetime
```

The parser accepts both search-result and message-detail envelopes, parses text/post JSON without persisting full content, and maps mention `id == self_open_id` to `DIRECT`, `id in {"all", "@all"}` to `ALL`.

- [ ] **Step 4: Run model tests and confirm GREEN**

Run: `../../../.venv/Scripts/python.exe -m pytest tests/test_morning_brief_models.py -q`

Expected: all model tests pass.

- [ ] **Step 5: Write failing ranking tests**

```python
def test_top_three_prefers_direct_mentions_and_distinct_topics():
    result = rank_candidates(items, calendar_items=[], limit=3)
    assert [item.topic_id for item in result] == ["incident", "approval", "release"]
    assert result[0].score >= result[1].score >= result[2].score
```

- [ ] **Step 6: Implement documented scoring and topic diversity**

Implement the approved weights (`+100` direct `@me`, `+55` P2P request, `+35` request/question, `+30` incident, `+25` deadline, `+20` carried, `+10` calendar/`@all`, `-70` replied, `-40` informational). Select distinct topics first, except production-critical items with score at least 130.

- [ ] **Step 7: Run ranking tests and confirm GREEN**

Run: `../../../.venv/Scripts/python.exe -m pytest tests/test_morning_brief_ranking.py -q`

Expected: all ranking tests pass.

### Task 2: i讯飞只读 OpenAPI 客户端

**Files:**
- Create: `server/main/xiaozhi-server/core/morning_brief/xfchat_client.py`
- Test: `server/main/xiaozhi-server/tests/test_morning_brief_xfchat_client.py`

**Interfaces:**
- Produces: `XfChatClient.search_messages(start, end, at_chatter_ids=None) -> CollectionResult`
- Produces: `XfChatClient.list_calendar_events(day_start, day_end) -> CollectionResult`
- Produces: `XfChatClient.capabilities() -> dict`

- [ ] **Step 1: Write failing fake-server tests for pagination and errors**

```python
async def test_search_consumes_every_page(xfchat_server):
    result = await client.search_messages(START, END, at_chatter_ids=["ou_me"])
    assert result.complete is True
    assert result.pages == 2
    assert [item["message_id"] for item in result.items] == ["om_1", "om_2"]
```

Also assert the request uses `POST /open-apis/search/v2/message`, sends seconds as strings, passes `page_token`, bounds pages, and redacts tokens from raised errors.

- [ ] **Step 2: Run client tests and confirm RED**

Run: `../../../.venv/Scripts/python.exe -m pytest tests/test_morning_brief_xfchat_client.py -q`

Expected: import fails because the client is absent.

- [ ] **Step 3: Implement aiohttp client and capability errors**

```python
payload = {
    "query": "",
    "from_time": str(int(start.timestamp())),
    "to_time": str(int(end.timestamp())),
    "page_size": self.page_size,
}
if at_chatter_ids:
    payload["at_chatter_ids"] = at_chatter_ids
```

Use `Authorization: Bearer <user_access_token>`, validate `code == 0`, fetch all pages up to `max_pages`, optionally enrich ID-only results through `GET /open-apis/im/v1/messages/mget`, and use `POST /open-apis/calendar/v4/calendars/primary` followed by `GET /open-apis/calendar/v4/calendars/{calendar_id}/events/instance_view`.

- [ ] **Step 4: Run client tests and confirm GREEN**

Run: `../../../.venv/Scripts/python.exe -m pytest tests/test_morning_brief_xfchat_client.py -q`

Expected: all client tests pass.

### Task 3: SQLite attention ledger

**Files:**
- Create: `server/main/xiaozhi-server/core/morning_brief/ledger.py`
- Test: `server/main/xiaozhi-server/tests/test_morning_brief_ledger.py`

**Interfaces:**
- Produces: `AttentionLedger.upsert_items(items, seen_at) -> list[AttentionItem]`
- Produces: `AttentionLedger.mark_replied(topic_id, replied_at) -> int`
- Produces: `AttentionLedger.save_brief(report)`, `latest_brief()`, `get/set_watermark()` and `purge()`

- [ ] **Step 1: Write failing persistence tests**

```python
def test_repeated_message_is_deduplicated_and_carried(tmp_path):
    ledger = AttentionLedger(tmp_path / "brief.db")
    first = ledger.upsert_items([item], NOW)
    second = ledger.upsert_items([item], TOMORROW)
    assert first[0].status == "OPEN_NEW"
    assert second[0].status == "OPEN_CARRIED"
    assert ledger.count_items() == 1
```

Assert persisted excerpts are truncated and raw content is absent from the schema and stored payload.

- [ ] **Step 2: Run ledger tests and confirm RED**

Run: `../../../.venv/Scripts/python.exe -m pytest tests/test_morning_brief_ledger.py -q`

Expected: import fails because the ledger is absent.

- [ ] **Step 3: Implement SQLite schema and transactional writes**

Use a unique key on `source_message_id`; store `OPEN_NEW`, `OPEN_CARRIED`, `SNOOZED`, `DONE`, `DISMISSED`; keep report JSON and source watermarks in separate tables. Run `purge()` after successful previews with 7-day excerpt clearing and 30-day resolved-record deletion.

- [ ] **Step 4: Run ledger tests and confirm GREEN**

Run: `../../../.venv/Scripts/python.exe -m pytest tests/test_morning_brief_ledger.py -q`

Expected: all ledger tests pass.

### Task 4: Aggregation service and coverage semantics

**Files:**
- Create: `server/main/xiaozhi-server/core/morning_brief/service.py`
- Test: `server/main/xiaozhi-server/tests/test_morning_brief_service.py`

**Interfaces:**
- Consumes: `XfChatClient`, `AttentionLedger`, normalization and ranking APIs.
- Produces: `MorningBriefService.preview(report_date=None) -> dict`
- Produces: `MorningBriefService.latest() -> dict | None`
- Produces: `MorningBriefService.health() -> dict`

- [ ] **Step 1: Write failing service tests**

Cover: two independent message scans, ten-minute overlap, initial previous-workday 18:00 window, deduplication, later self reply, calendar conflict detection, deterministic Top 3, `COMPLETE/PARTIAL/FAILED`, and no watermark advancement after interrupted pagination.

- [ ] **Step 2: Run service tests and confirm RED**

Run: `../../../.venv/Scripts/python.exe -m pytest tests/test_morning_brief_service.py -q`

Expected: import fails because the service is absent.

- [ ] **Step 3: Implement orchestration**

```python
general, mentions, calendar = await asyncio.gather(
    self.client.search_messages(start, end),
    self.client.search_messages(start, end, at_chatter_ids=[self.self_open_id]),
    self.client.list_calendar_events(day_start, day_end),
    return_exceptions=True,
)
```

Convert each result into a coverage row. Advance `messages` watermark only when both message scans are complete and persisted. Never describe results as client unread.

- [ ] **Step 4: Run service tests and confirm GREEN**

Run: `../../../.venv/Scripts/python.exe -m pytest tests/test_morning_brief_service.py -q`

Expected: all service tests pass.

### Task 5: HTTP API、配置与文档

**Files:**
- Create: `server/main/xiaozhi-server/core/api/morning_brief_handler.py`
- Create: `server/main/xiaozhi-server/core/morning_brief_routes.py`
- Modify: `server/main/xiaozhi-server/core/http_server.py`
- Modify: `server/main/xiaozhi-server/config.yaml`
- Create: `server/main/xiaozhi-server/tests/test_morning_brief_handler.py`
- Create: `server/main/xiaozhi-server/tests/test_morning_brief_routes.py`
- Create: `docs/api/i讯飞每日关注晨报接口.md`

**Interfaces:**
- Produces: `POST /xiaozhi/morning-brief/preview`
- Produces: `GET /xiaozhi/morning-brief/latest`
- Produces: `GET /xiaozhi/morning-brief/health`

- [ ] **Step 1: Write failing route and handler tests**

Assert normalized `{code,message,data}` envelopes, Bearer authentication, `404 MORNING_BRIEF_NOT_FOUND`, request validation, CORS, and route registration inside `SimpleHttpServer` without external network access.

- [ ] **Step 2: Run HTTP tests and confirm RED**

Run: `../../../.venv/Scripts/python.exe -m pytest tests/test_morning_brief_handler.py tests/test_morning_brief_routes.py -q`

Expected: imports fail because the HTTP adapter is absent.

- [ ] **Step 3: Implement HTTP adapter and default-disabled configuration**

Add `morning_brief.enabled: false`, private base URL, local SQLite path, timeout/page/retention settings, empty self ID and token fields. Read secrets from environment first and never return them from health or error payloads.

- [ ] **Step 4: Write Chinese API and dry-run documentation**

Document endpoint requests/responses, `COMPLETE/PARTIAL/FAILED`, minimum scopes, environment variables, local override example, and the fact that “待关注” is not the client unread badge.

- [ ] **Step 5: Run focused and full verification**

Run:

```powershell
../../../.venv/Scripts/python.exe -m pytest tests/test_morning_brief_*.py -q
../../../.venv/Scripts/python.exe -m pytest tests -q
../../../.venv/Scripts/python.exe -m compileall core/morning_brief core/api/morning_brief_handler.py core/morning_brief_routes.py
git diff --check
```

Expected: all tests pass, compilation exits 0, and `git diff --check` prints nothing.

