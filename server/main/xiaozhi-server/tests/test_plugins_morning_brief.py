"""晨间简报语音入口测试。

全部离线：通过替换模块级 _service_factory 注入假的 MorningBriefService，
不碰真实飞书、网络或磁盘台账。
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import plugins_func.functions.morning_brief as morning_brief
from core.morning_brief.models import AttentionItem
from plugins_func.register import Action


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 19, 9, 0, 0, tzinfo=timezone.utc)  # 2026-08-19 17:00 +08:00


class FakeLedger:
    def __init__(self, items=None, *, raise_error=False):
        self._items = items or []
        self._raise_error = raise_error

    def get_open_items(self):
        if self._raise_error:
            raise RuntimeError("ledger unavailable")
        return list(self._items)


class FakeService:
    def __init__(
        self,
        *,
        latest=None,
        preview_report=None,
        preview_error=None,
        ledger_items=None,
        ledger_raises=False,
        now=NOW,
        tz=TZ,
    ):
        self.timezone = tz
        self.now_provider = lambda: now
        self.ledger = FakeLedger(ledger_items, raise_error=ledger_raises)
        self._latest = latest
        self._preview_report = preview_report
        self._preview_error = preview_error
        self.preview_calls = 0

    def latest(self):
        return self._latest

    async def preview(self, report_date=None):
        self.preview_calls += 1
        if self._preview_error is not None:
            raise self._preview_error
        return self._preview_report


class FakeLogger:
    def bind(self, **kwargs):
        return self

    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


class FakeConn:
    def __init__(self, config):
        self.config = config
        self.logger = FakeLogger()


def _attention(
    message_id,
    *,
    sender_name="",
    chat_name="",
    topic_id=None,
):
    return AttentionItem(
        message_id=message_id,
        topic_id=topic_id or message_id,
        sender_id="ou_x",
        sender_name=sender_name,
        chat_id="oc_x",
        chat_name=chat_name,
        chat_type="group" if chat_name else "p2p",
        mention_kind="DIRECT",
        short_excerpt="占位摘要",
        source_timestamp=NOW,
        source_url="https://applink.feishu.cn/client/chat/open?openChatId=oc_x",
    )


@pytest.fixture(autouse=True)
def _restore_service_factory():
    original = morning_brief._service_factory
    yield
    morning_brief._service_factory = original


def _install_factory(service):
    calls = []

    def factory(config):
        calls.append(config)
        return service

    morning_brief._service_factory = factory
    return calls


@pytest.mark.asyncio
async def test_disabled_returns_fixed_reply_and_never_builds_service():
    def factory(config):
        raise AssertionError("未启用时不该创建 service")

    morning_brief._service_factory = factory
    conn = FakeConn({"morning_brief": {"enabled": False}})

    response = await morning_brief.get_morning_brief(conn)

    assert response.action == Action.RESPONSE
    assert response.response == morning_brief.DISABLED_REPLY
    assert response.result == "disabled"


@pytest.mark.asyncio
async def test_missing_morning_brief_section_is_treated_as_disabled():
    def factory(config):
        raise AssertionError("未配置晨报段时不该创建 service")

    morning_brief._service_factory = factory
    conn = FakeConn({})

    response = await morning_brief.get_morning_brief(conn)

    assert response.response == morning_brief.DISABLED_REPLY


@pytest.mark.asyncio
async def test_empty_top_three_returns_fixed_reply():
    report = {
        "report_date": "2026-08-19",
        "top_three": [],
        "calendar": [],
        "reauthorization_required": False,
        "permission_required": False,
    }
    service = FakeService(latest=report)
    _install_factory(service)
    conn = FakeConn({"morning_brief": {"enabled": True}})

    response = await morning_brief.get_morning_brief(conn)

    assert response.response == morning_brief.EMPTY_REPLY
    # 今天已有报告，不该再跑一次现采
    assert service.preview_calls == 0


@pytest.mark.asyncio
async def test_reauthorization_required_returns_fixed_reply():
    report = {
        "report_date": "2026-08-19",
        "top_three": [],
        "reauthorization_required": True,
        "permission_required": False,
    }
    service = FakeService(latest=report)
    _install_factory(service)
    conn = FakeConn({"morning_brief": {"enabled": True}})

    response = await morning_brief.get_morning_brief(conn)

    assert response.response == morning_brief.REAUTH_REPLY
    assert response.result == "reauthorization_required"


@pytest.mark.asyncio
async def test_permission_required_returns_fixed_reply():
    report = {
        "report_date": "2026-08-19",
        "top_three": [],
        "reauthorization_required": False,
        "permission_required": True,
        "missing_scopes": ["calendar:calendar:readonly"],
    }
    service = FakeService(latest=report)
    _install_factory(service)
    conn = FakeConn({"morning_brief": {"enabled": True}})

    response = await morning_brief.get_morning_brief(conn)

    assert response.response == morning_brief.PERMISSION_REPLY
    assert response.result == "permission_required"


@pytest.mark.asyncio
async def test_stale_latest_triggers_a_fresh_preview():
    stale = {"report_date": "2026-08-18", "top_three": []}
    fresh = {
        "report_date": "2026-08-19",
        "top_three": [],
        "reauthorization_required": False,
        "permission_required": False,
    }
    service = FakeService(latest=stale, preview_report=fresh)
    _install_factory(service)
    conn = FakeConn({"morning_brief": {"enabled": True}})

    response = await morning_brief.get_morning_brief(conn)

    assert service.preview_calls == 1
    assert response.response == morning_brief.EMPTY_REPLY


@pytest.mark.asyncio
async def test_fetch_failure_returns_graceful_fallback():
    service = FakeService(latest=None, preview_error=RuntimeError("network down"))
    _install_factory(service)
    conn = FakeConn({"morning_brief": {"enabled": True}})

    response = await morning_brief.get_morning_brief(conn)

    assert response.response == morning_brief.FETCH_ERROR_REPLY
    assert response.result == "error"


@pytest.mark.asyncio
async def test_top_three_reports_real_sources_and_recommendation():
    report = {
        "report_date": "2026-08-19",
        "top_three": [
            {
                "kind": "MESSAGE",
                "item_id": "om_1",
                "topic_id": "t1",
                "title": "请尽快确认这周发布窗口是不是没问题谢谢",
                "score": 155,
                "reasons": ["direct_mention", "p2p_request"],
                "source_url": "https://x",
                "status": "OPEN_NEW",
                "confidence": "HIGH",
            },
            {
                "kind": "MESSAGE",
                "item_id": "om_2",
                "topic_id": "t2",
                "title": "生产环境告警，请立即处理",
                "score": 120,
                "reasons": ["production_incident"],
                "source_url": "https://y",
                "status": "OPEN_NEW",
                "confidence": "HIGH",
            },
            {
                "kind": "CALENDAR",
                "item_id": "evt_1",
                "topic_id": "calendar:evt_1",
                "title": "季度评审会",
                "score": 30,
                "reasons": ["calendar_attention"],
                "source_url": "https://z",
                "status": "needs_action",
                "confidence": "LOW",
            },
        ],
        "calendar": [
            {
                "event_id": "evt_1",
                "summary": "季度评审会",
                "start": "2026-08-19T19:00:00+08:00",
                "end": "2026-08-19T20:00:00+08:00",
                "timezone": "Asia/Shanghai",
                "location": "",
                "rsvp_status": "needs_action",
                "organizer": "王芳",
                "source_url": "https://z",
                "conflicts": [],
            }
        ],
        "reauthorization_required": False,
        "permission_required": False,
    }
    ledger_items = [
        _attention("om_1", sender_name="李雷", chat_name="发布协调群", topic_id="t1"),
        _attention("om_2", sender_name="韩梅梅", chat_name="", topic_id="t2"),
    ]
    service = FakeService(latest=report, ledger_items=ledger_items)
    _install_factory(service)
    conn = FakeConn({"morning_brief": {"enabled": True}})

    response = await morning_brief.get_morning_brief(conn)
    text = response.response

    assert text.startswith("第一件：")
    assert "来自李雷在发布协调群。" in text
    assert "第二件：" in text
    assert "来自韩梅梅。" in text
    assert "第三件：" in text
    assert "来自日历·王芳。" in text
    assert "建议先处理第一件。" in text
    assert "最近的会议是19:00的季度评审会。" in text

    # 每条"第 N 件"控制在口播友好的长度以内
    for line in text.split("。"):
        if line.startswith("第"):
            assert len(line) + 1 <= morning_brief.MAX_ITEM_CHARS


@pytest.mark.asyncio
async def test_very_long_title_is_shortened_to_fit_the_voice_budget():
    report = {
        "report_date": "2026-08-19",
        "top_three": [
            {
                "kind": "MESSAGE",
                "item_id": "om_long",
                "topic_id": "t_long",
                "title": "这是一条特别特别特别特别特别特别特别特别特别特别长的消息摘要用来测试截断逻辑是否生效",
                "score": 100,
                "reasons": ["direct_mention"],
                "source_url": "https://x",
                "status": "OPEN_NEW",
                "confidence": "HIGH",
            }
        ],
        "calendar": [],
        "reauthorization_required": False,
        "permission_required": False,
    }
    ledger_items = [
        _attention("om_long", sender_name="李雷", chat_name="发布协调群", topic_id="t_long")
    ]
    service = FakeService(latest=report, ledger_items=ledger_items)
    _install_factory(service)
    conn = FakeConn({"morning_brief": {"enabled": True}})

    response = await morning_brief.get_morning_brief(conn)
    item_line = response.response.split("。")[0]

    assert len(item_line) + 1 <= morning_brief.MAX_ITEM_CHARS
    assert item_line.endswith("…，来自李雷在发布协调群") or "…" in item_line


@pytest.mark.asyncio
async def test_message_without_ledger_match_falls_back_to_generic_source():
    report = {
        "report_date": "2026-08-19",
        "top_three": [
            {
                "kind": "MESSAGE",
                "item_id": "om_missing",
                "topic_id": "t_missing",
                "title": "找不到台账记录的消息",
                "score": 100,
                "reasons": ["direct_mention"],
                "source_url": "https://x",
                "status": "OPEN_NEW",
                "confidence": "HIGH",
            }
        ],
        "calendar": [],
        "reauthorization_required": False,
        "permission_required": False,
    }
    service = FakeService(latest=report, ledger_items=[])
    _install_factory(service)
    conn = FakeConn({"morning_brief": {"enabled": True}})

    response = await morning_brief.get_morning_brief(conn)

    assert "来自飞书消息。" in response.response


@pytest.mark.asyncio
async def test_ledger_lookup_failure_falls_back_to_generic_source():
    report = {
        "report_date": "2026-08-19",
        "top_three": [
            {
                "kind": "MESSAGE",
                "item_id": "om_1",
                "topic_id": "t1",
                "title": "台账读取会抛异常",
                "score": 100,
                "reasons": ["direct_mention"],
                "source_url": "https://x",
                "status": "OPEN_NEW",
                "confidence": "HIGH",
            }
        ],
        "calendar": [],
        "reauthorization_required": False,
        "permission_required": False,
    }
    service = FakeService(latest=report, ledger_raises=True)
    _install_factory(service)
    conn = FakeConn({"morning_brief": {"enabled": True}})

    response = await morning_brief.get_morning_brief(conn)

    assert "来自飞书消息。" in response.response
