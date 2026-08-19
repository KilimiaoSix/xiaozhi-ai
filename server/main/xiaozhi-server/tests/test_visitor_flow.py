"""来访者应答与留言流程测试。

全部离线：owner_status / away_ledger / push / 设备解析全部注入假件。
"""

from datetime import datetime, timedelta

import pytest

from core.visitor_flow import (
    VisitorFlow,
    get_visitor_flow,
    make_registry_device_resolver,
    reset_visitor_flow,
    visitor_flow_handle_asr,
)


START = datetime(2026, 8, 19, 14, 0, 0)
WORKSTATION = "desk-test"
DEVICE_ID = "dc:da:0c:26:9a:60"


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class FakeOwnerStatus:
    def __init__(self, **payload) -> None:
        self.payload = {
            "state": "away",
            "public_note": "",
            "expected_return": None,
            "overdue": False,
        }
        self.payload.update(payload)

    def get(self):
        return dict(self.payload)


class FakeLedger:
    def __init__(self) -> None:
        self.records = []

    def record(self, kind, text, **kwargs):
        entry = {"kind": kind, "text": text, **kwargs}
        self.records.append(entry)
        return entry


class Recorder:
    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, conn, text, **kwargs):
        self.calls.append({"conn": conn, "text": text, **kwargs})
        return True

    @property
    def texts(self):
        return [call["text"] for call in self.calls]


def build(owner_status=None, *, conn=object(), resolve=True):
    clock = FakeClock()
    push = Recorder()
    ledger = FakeLedger()

    def resolver(workstation_id):
        if not resolve or workstation_id != WORKSTATION:
            return None
        return (DEVICE_ID, conn)

    flow = VisitorFlow(
        owner_status_store=owner_status or FakeOwnerStatus(),
        away_ledger=ledger,
        push_event=push,
        device_resolver=resolver,
        clock=clock,
    )
    return flow, clock, push, ledger, conn


# ---------------------------------------------------------------- 应答文案

@pytest.mark.asyncio
async def test_meeting_with_expected_return_announces_time():
    owner = FakeOwnerStatus(
        state="meeting", expected_return="2026-08-19T15:30:00"
    )
    flow, _clock, push, _ledger, conn = build(owner)

    await flow.on_visitor_detected(WORKSTATION)

    assert push.texts == ["他正在开会，预计15:30回来。需要帮你留句话吗？"]
    call = push.calls[0]
    assert call["conn"] is conn
    assert call["emotion"] == "neutral"
    assert call["speak"] is True
    assert call["action"] == "center"


@pytest.mark.asyncio
async def test_meeting_without_expected_return_omits_time():
    flow, _clock, push, _ledger, _conn = build(FakeOwnerStatus(state="meeting"))

    await flow.on_visitor_detected(WORKSTATION)

    assert push.texts == ["他正在开会，暂时不在工位。需要帮你留句话吗？"]


@pytest.mark.asyncio
async def test_leave_uses_leave_wording():
    """无 leave_end 时退回不带日期的请假文案（绝不编造返回时间）。"""
    flow, _clock, push, _ledger, _conn = build(FakeOwnerStatus(state="leave"))

    await flow.on_visitor_detected(WORKSTATION)

    assert push.texts == ["他今天请假，不在工位。有重要的事我可以帮你记下来。"]


@pytest.mark.asyncio
async def test_leave_with_end_date_tells_public_return_date():
    """需求：请假期间说明"不在岗"和可公开的返回时间——返回日=假期结束次日。"""
    flow, _clock, push, _ledger, _conn = build(
        FakeOwnerStatus(state="leave", leave_start="2026-08-20", leave_end="2026-08-20")
    )

    await flow.on_visitor_detected(WORKSTATION)

    assert push.texts == ["他请假了，预计8月21日回来。有重要的事我可以帮你记下来。"]


@pytest.mark.asyncio
async def test_away_uses_generic_wording():
    flow, _clock, push, _ledger, _conn = build(FakeOwnerStatus(state="away"))

    await flow.on_visitor_detected(WORKSTATION)

    assert push.texts == ["他暂时不在工位，需要帮你留句话吗？"]


@pytest.mark.asyncio
async def test_available_but_absent_uses_generic_wording():
    """摄像头看到工位没人，但主人没声明状态——只能说公开的那句。"""
    flow, _clock, push, _ledger, _conn = build(FakeOwnerStatus(state="available"))

    await flow.on_visitor_detected(WORKSTATION)

    assert push.texts == ["他暂时不在工位，需要帮你留句话吗？"]


@pytest.mark.asyncio
async def test_meeting_topic_never_leaks():
    """会议主题/请假原因属于私密信息，对外文案里不能出现。"""
    owner = FakeOwnerStatus(
        state="meeting",
        public_note="他正在开会",
        expected_return="2026-08-19T15:30:00",
    )
    flow, _clock, push, _ledger, _conn = build(owner)

    await flow.on_visitor_detected(WORKSTATION)

    assert "主题" not in push.texts[0]


@pytest.mark.asyncio
async def test_broken_expected_return_falls_back():
    owner = FakeOwnerStatus(state="meeting", expected_return="下午三点半")
    flow, _clock, push, _ledger, _conn = build(owner)

    await flow.on_visitor_detected(WORKSTATION)

    assert push.texts == ["他正在开会，暂时不在工位。需要帮你留句话吗？"]


# ---------------------------------------------------------------- 防抖

@pytest.mark.asyncio
async def test_same_visit_does_not_repeat_greeting():
    """来访者站着不动时上报会一直来，不能每条都念一遍。"""
    flow, clock, push, _ledger, _conn = build()

    await flow.on_visitor_detected(WORKSTATION)
    clock.advance(seconds=30)
    await flow.on_visitor_detected(WORKSTATION)

    assert len(push.calls) == 1


@pytest.mark.asyncio
async def test_new_visit_after_cooldown_greets_again():
    flow, clock, push, _ledger, _conn = build()

    await flow.on_visitor_detected(WORKSTATION)
    clock.advance(seconds=181)
    await flow.on_visitor_detected(WORKSTATION)

    assert len(push.calls) == 2


@pytest.mark.asyncio
async def test_offline_device_does_not_consume_cooldown():
    """设备不在线时这次应答根本没发生，不能因此把冷却期用掉。"""
    conn = object()
    flow, _clock, push, _ledger, _conn = build(conn=conn, resolve=False)

    await flow.on_visitor_detected(WORKSTATION)
    assert push.calls == []

    flow._device_resolver = lambda workstation_id: (DEVICE_ID, conn)
    await flow.on_visitor_detected(WORKSTATION)
    assert len(push.calls) == 1


@pytest.mark.asyncio
async def test_push_failure_is_swallowed_and_retried():
    clock = FakeClock()
    calls = []

    async def boom(conn, text, **kwargs):
        calls.append(text)
        raise RuntimeError("websocket closed")

    flow = VisitorFlow(
        owner_status_store=FakeOwnerStatus(),
        away_ledger=FakeLedger(),
        push_event=boom,
        device_resolver=lambda w: (DEVICE_ID, object()),
        clock=clock,
    )

    assert await flow.on_visitor_detected(WORKSTATION) is False
    # 推送失败没开成留言窗口，下一条上报还得再试
    assert flow.handle_asr_text(DEVICE_ID, "日志方案已发飞书") is None
    await flow.on_visitor_detected(WORKSTATION)
    assert len(calls) == 2


# ---------------------------------------------------------------- 留言窗口

@pytest.mark.asyncio
async def test_message_within_window_is_recorded():
    flow, _clock, _push, ledger, _conn = build()
    await flow.on_visitor_detected(WORKSTATION)

    reply = flow.handle_asr_text(DEVICE_ID, "日志方案已发飞书")

    assert reply == "记下了，他回来我就提醒。"
    assert len(ledger.records) == 1
    record = ledger.records[0]
    assert record["kind"] == "visitor_message"
    assert record["text"] == "有同事留言：日志方案已发飞书"


@pytest.mark.asyncio
async def test_window_closes_after_one_message():
    flow, _clock, _push, ledger, _conn = build()
    await flow.on_visitor_detected(WORKSTATION)

    flow.handle_asr_text(DEVICE_ID, "日志方案已发飞书")
    assert flow.handle_asr_text(DEVICE_ID, "还有一句") is None
    assert len(ledger.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["不用", "不需要", "没事", "不了", "没有", "不用了，谢谢", "没事了"],
)
async def test_declining_does_not_record(text):
    flow, _clock, _push, ledger, _conn = build()
    await flow.on_visitor_detected(WORKSTATION)

    reply = flow.handle_asr_text(DEVICE_ID, text)

    assert reply == "好的，他回来我就说你来过。"
    assert ledger.records == []


@pytest.mark.asyncio
async def test_long_sentence_containing_negative_word_is_still_a_message():
    """否定词出现在长句里通常是内容而不是拒绝，不能因此吞掉留言。"""
    flow, _clock, _push, ledger, _conn = build()
    await flow.on_visitor_detected(WORKSTATION)

    reply = flow.handle_asr_text(
        DEVICE_ID, "没有别的事，就是日志方案已经发飞书了让他看一下"
    )

    assert reply == "记下了，他回来我就提醒。"
    assert len(ledger.records) == 1


@pytest.mark.asyncio
async def test_message_after_window_expires_is_ignored():
    flow, clock, _push, ledger, _conn = build()
    await flow.on_visitor_detected(WORKSTATION)
    clock.advance(seconds=91)

    assert flow.handle_asr_text(DEVICE_ID, "日志方案已发飞书") is None
    assert ledger.records == []


def test_asr_without_open_window_returns_none():
    flow, _clock, _push, ledger, _conn = build()

    assert flow.handle_asr_text(DEVICE_ID, "今天天气不错") is None
    assert ledger.records == []


@pytest.mark.asyncio
async def test_blank_text_keeps_window_open():
    flow, _clock, _push, ledger, _conn = build()
    await flow.on_visitor_detected(WORKSTATION)

    assert flow.handle_asr_text(DEVICE_ID, "   ") is None
    assert flow.handle_asr_text(DEVICE_ID, "日志方案已发飞书") is not None


@pytest.mark.asyncio
async def test_other_device_does_not_consume_window():
    flow, _clock, _push, ledger, _conn = build()
    await flow.on_visitor_detected(WORKSTATION)

    assert flow.handle_asr_text("aa:bb:cc:dd:ee:ff", "日志方案已发飞书") is None
    assert ledger.records == []


@pytest.mark.asyncio
async def test_custom_window_and_cooldown_are_honoured():
    clock = FakeClock()
    push = Recorder()
    flow = VisitorFlow(
        {"visitor_flow": {"message_window_s": 10, "regreet_cooldown_s": 20}},
        owner_status_store=FakeOwnerStatus(),
        away_ledger=FakeLedger(),
        push_event=push,
        device_resolver=lambda w: (DEVICE_ID, object()),
        clock=clock,
    )

    await flow.on_visitor_detected(WORKSTATION)
    clock.advance(seconds=11)
    assert flow.handle_asr_text(DEVICE_ID, "日志方案已发飞书") is None
    clock.advance(seconds=10)
    await flow.on_visitor_detected(WORKSTATION)
    assert len(push.calls) == 2


# ---------------------------------------------------------------- 集成入口

def test_module_helper_returns_none_without_singleton():
    reset_visitor_flow()
    assert visitor_flow_handle_asr(DEVICE_ID, "日志方案已发飞书") is None


@pytest.mark.asyncio
async def test_module_helper_uses_singleton():
    reset_visitor_flow()
    try:
        push = Recorder()
        ledger = FakeLedger()
        flow = get_visitor_flow(
            owner_status_store=FakeOwnerStatus(),
            away_ledger=ledger,
            push_event=push,
            device_resolver=lambda w: (DEVICE_ID, object()),
            clock=FakeClock(),
        )
        assert get_visitor_flow() is flow

        await flow.on_visitor_detected(WORKSTATION)
        assert (
            visitor_flow_handle_asr(DEVICE_ID, "日志方案已发飞书")
            == "记下了，他回来我就提醒。"
        )
        assert len(ledger.records) == 1
    finally:
        reset_visitor_flow()


def test_registry_device_resolver_maps_workstation_to_online_conn():
    conn = object()

    class FakeRegistry:
        def get(self, device_id):
            return conn if device_id == DEVICE_ID else None

    config = {"presence_robot": {"workstations": {WORKSTATION: DEVICE_ID}}}
    resolver = make_registry_device_resolver(config, FakeRegistry())

    assert resolver(WORKSTATION) == (DEVICE_ID, conn)
    assert resolver("desk-other") is None


def test_registry_device_resolver_returns_none_when_offline():
    class EmptyRegistry:
        def get(self, device_id):
            return None

    config = {"presence_robot": {"workstations": {WORKSTATION: DEVICE_ID}}}
    resolver = make_registry_device_resolver(config, EmptyRegistry())

    assert resolver(WORKSTATION) is None
