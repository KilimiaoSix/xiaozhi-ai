"""离席事件台账测试。

全部离线：注入假时钟、落盘到 tmp_path，不碰真机与真实 data/ 目录。
"""

from datetime import datetime, timedelta

import pytest

from core.away_ledger import (
    AwayLedger,
    get_away_ledger,
    reset_away_ledger,
)


START = datetime(2026, 8, 19, 10, 0, 0)


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def env(tmp_path):
    clock = FakeClock()
    ledger = AwayLedger(tmp_path / "away_ledger.json", clock=clock)
    return ledger, clock, tmp_path / "away_ledger.json"


# ---------------------------------------------------------------- 离席窗口

def test_record_outside_away_window_only_logs(env):
    """主人在工位时发生的事件不该攒着回来再念一遍——他当场就看到了。"""
    ledger, _clock, _path = env

    ledger.record("agent_completed", "Codex 跑完了测试", task_key="t1")

    assert ledger.is_away() is False
    assert ledger.pending_summary()["items"] == []
    assert len(ledger.events_today()) == 1


def test_record_inside_away_window_enters_pending(env):
    ledger, _clock, _path = env

    ledger.mark_away()
    ledger.record("agent_completed", "Codex 跑完了测试", task_key="t1")

    assert ledger.is_away() is True
    items = ledger.pending_summary()["items"]
    assert [item["text"] for item in items] == ["Codex 跑完了测试"]


def test_mark_away_is_idempotent_and_keeps_first_start(env):
    """离岗判定挂在每条心跳上，重复调用不能把离席起点一路往后推。"""
    ledger, clock, _path = env

    ledger.mark_away()
    first = ledger.away_started_at
    clock.advance(minutes=10)
    ledger.mark_away()

    assert ledger.away_started_at == first
    assert ledger.pending_summary()["away_minutes"] == 10


def test_mark_returned_closes_window(env):
    ledger, clock, _path = env

    ledger.mark_away()
    clock.advance(minutes=35)
    ledger.mark_returned()

    assert ledger.is_away() is False
    assert ledger.away_started_at is None
    # 回来之后桌面端还要能看到"刚才离开了多久"
    assert ledger.pending_summary()["away_minutes"] == 35


# ---------------------------------------------------------------- 去重与合并

def test_same_task_key_keeps_only_latest_agent_event(env):
    """同一任务开始→完成只报最终结果，否则汇总里全是同一件事的流水。"""
    ledger, clock, _path = env
    ledger.mark_away()

    ledger.record("agent_needs_user", "Codex 需要确认改动", task_key="task-7")
    clock.advance(minutes=2)
    ledger.record("agent_completed", "Codex 完成了改动", task_key="task-7")

    items = ledger.pending_summary()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "agent_completed"
    assert items[0]["text"] == "Codex 完成了改动"


def test_different_task_keys_are_kept_separately(env):
    ledger, _clock, _path = env
    ledger.mark_away()

    ledger.record("agent_completed", "任务 A 完成", task_key="a")
    ledger.record("agent_completed", "任务 B 完成", task_key="b")

    assert len(ledger.pending_summary()["items"]) == 2


def test_agent_event_without_task_key_is_not_deduplicated(env):
    ledger, _clock, _path = env
    ledger.mark_away()

    ledger.record("agent_completed", "任务完成")
    ledger.record("agent_completed", "任务完成")

    assert len(ledger.pending_summary()["items"]) == 2


def test_repeated_critical_incident_merges_with_count(env):
    """同一条严重告警刷屏时只报一条，附带次数。"""
    ledger, _clock, _path = env
    ledger.mark_away()

    ledger.record("incident", "生产环境 5xx 飙升", severity="critical")
    ledger.record("incident", "生产环境 5xx 飙升", severity="critical")
    ledger.record("incident", "生产环境 5xx 飙升", severity="critical")

    items = ledger.pending_summary()["items"]
    assert len(items) == 1
    assert items[0]["count"] == 3


def test_non_critical_incident_is_not_merged(env):
    ledger, _clock, _path = env
    ledger.mark_away()

    ledger.record("incident", "磁盘用量 70%")
    ledger.record("incident", "磁盘用量 70%")

    assert len(ledger.pending_summary()["items"]) == 2


# ---------------------------------------------------------------- 排序

def test_pending_summary_orders_by_severity_then_kind(env):
    ledger, _clock, _path = env
    ledger.mark_away()

    ledger.record("generic", "普通消息")
    ledger.record("visitor_message", "有同事留言：日志方案已发飞书")
    ledger.record("agent_completed", "任务完成", task_key="t1")
    ledger.record("agent_needs_user", "等你确认", task_key="t2")
    ledger.record("incident", "生产环境 5xx 飙升", severity="critical")

    kinds = [item["kind"] for item in ledger.pending_summary()["items"]]
    assert kinds == [
        "incident",
        "agent_needs_user",
        "agent_completed",
        "visitor_message",
        "generic",
    ]


def test_same_bucket_keeps_chronological_order(env):
    ledger, clock, _path = env
    ledger.mark_away()

    ledger.record("visitor_message", "有同事留言：先来的")
    clock.advance(minutes=1)
    ledger.record("visitor_message", "有同事留言：后来的")

    texts = [item["text"] for item in ledger.pending_summary()["items"]]
    assert texts == ["有同事留言：先来的", "有同事留言：后来的"]


# ---------------------------------------------------------------- 播报文案

def test_compose_speech_without_pending_returns_none(env):
    ledger, _clock, _path = env
    ledger.mark_away()

    assert ledger.compose_speech() is None


def test_compose_speech_reads_duration_and_grouped_items(env):
    ledger, clock, _path = env
    ledger.mark_away()

    ledger.record("agent_completed", "跑完了测试", task_key="t1", source="Codex")
    ledger.record("agent_completed", "改完了文案", task_key="t2", source="Codex")
    ledger.record("visitor_message", "有同事留言：日志方案已发飞书")
    clock.advance(minutes=35)

    speech = ledger.compose_speech()

    assert speech == (
        "你离开的三十五分钟里，Codex 完成了 2 个任务。"
        "有同事留言：日志方案已发飞书。"
    )


def test_compose_speech_is_at_most_three_sentences(env):
    ledger, clock, _path = env
    ledger.mark_away()

    ledger.record("incident", "生产环境 5xx 飙升", severity="critical")
    ledger.record("agent_needs_user", "等你确认部署", task_key="t1")
    ledger.record("agent_completed", "跑完了测试", task_key="t2", source="Codex")
    ledger.record("agent_failed", "构建失败", task_key="t3")
    ledger.record("visitor_message", "有同事留言：日志方案已发飞书")
    ledger.record("generic", "周会挪到四点")
    clock.advance(minutes=90)

    speech = ledger.compose_speech()

    assert speech.startswith("你离开的一小时三十分里，")
    assert speech.count("。") <= 3


def test_compose_speech_hours_and_minutes(env):
    ledger, clock, _path = env
    ledger.mark_away()
    ledger.record("generic", "周会挪到四点")
    clock.advance(hours=2)

    assert ledger.compose_speech().startswith("你离开的两小时里，")


def test_compose_speech_counts_multiple_visitor_messages(env):
    ledger, clock, _path = env
    ledger.mark_away()

    ledger.record("visitor_message", "有同事留言：日志方案已发飞书")
    ledger.record("visitor_message", "有同事留言：下午两点对一下接口")
    clock.advance(minutes=5)

    speech = ledger.compose_speech()

    assert "有 2 条留言" in speech
    # 计数句里不该再套一层"有同事留言："
    assert "最近一条是下午两点对一下接口" in speech


def test_compose_speech_reports_critical_incident_first(env):
    ledger, clock, _path = env
    ledger.mark_away()

    ledger.record("generic", "周会挪到四点")
    ledger.record("incident", "生产环境 5xx 飙升", severity="critical")
    clock.advance(minutes=5)

    speech = ledger.compose_speech()

    assert speech.startswith("你离开的五分钟里，有严重告警：生产环境 5xx 飙升。")


def test_compose_speech_short_absence_uses_fallback_prefix(env):
    ledger, _clock, _path = env
    ledger.mark_away()
    ledger.record("generic", "周会挪到四点")

    assert ledger.compose_speech().startswith("你离开这会儿，")


# ---------------------------------------------------------------- 已播报

def test_mark_reported_clears_pending(env):
    ledger, _clock, _path = env
    ledger.mark_away()
    ledger.record("visitor_message", "有同事留言：日志方案已发飞书")

    ledger.mark_reported()

    assert ledger.pending_summary()["items"] == []
    assert ledger.compose_speech() is None
    # 播报过不等于没发生过：当日日志仍要留着给日终总结
    assert len(ledger.events_today()) == 1


def test_mark_reported_does_not_replay_after_second_return(env):
    ledger, clock, _path = env
    ledger.mark_away()
    ledger.record("visitor_message", "有同事留言：日志方案已发飞书")
    ledger.mark_reported()
    ledger.mark_returned()

    clock.advance(minutes=10)
    ledger.mark_away()
    clock.advance(minutes=10)

    assert ledger.compose_speech() is None


# ---------------------------------------------------------------- 当日日志

def test_events_today_only_returns_today(env):
    ledger, clock, _path = env

    ledger.record("generic", "昨天的事")
    clock.advance(days=1)
    ledger.record("generic", "今天的事")

    texts = [item["text"] for item in ledger.events_today()]
    assert texts == ["今天的事"]


def test_events_today_includes_events_from_both_windows(env):
    ledger, _clock, _path = env

    ledger.record("generic", "在岗时发生的")
    ledger.mark_away()
    ledger.record("generic", "离席时发生的")

    assert len(ledger.events_today()) == 2


# ---------------------------------------------------------------- 持久化

def test_pending_survives_restart(env):
    """留言不能因为重启就丢——那是同事托付给机器人的事。"""
    ledger, clock, path = env
    ledger.mark_away()
    ledger.record("visitor_message", "有同事留言：日志方案已发飞书")

    reloaded = AwayLedger(path, clock=clock)

    assert reloaded.is_away() is True
    assert reloaded.away_started_at == START
    assert [item["text"] for item in reloaded.pending_summary()["items"]] == [
        "有同事留言：日志方案已发飞书"
    ]
    assert len(reloaded.events_today()) == 1


def test_corrupted_file_falls_back_to_empty(env, tmp_path):
    path = tmp_path / "away_ledger.json"
    path.write_text("{ this is not json", encoding="utf-8")

    ledger = AwayLedger(path, clock=FakeClock())

    assert ledger.is_away() is False
    assert ledger.pending_summary()["items"] == []
    # 坏文件不能让后续写入也失败
    ledger.mark_away()
    ledger.record("generic", "还能继续记")
    assert len(ledger.pending_summary()["items"]) == 1


def test_persist_failure_does_not_break_recording(tmp_path):
    """落盘失败只该影响重启后的恢复，不该让本次记录抛出去。"""
    unwritable = tmp_path / "nope.json"
    ledger = AwayLedger(unwritable, clock=FakeClock())
    ledger._path = tmp_path  # 指向一个目录，写入必然失败

    ledger.mark_away()
    ledger.record("generic", "内存里还得有")

    assert len(ledger.pending_summary()["items"]) == 1


# ---------------------------------------------------------------- 单例

def test_singleton_returns_same_instance(tmp_path):
    reset_away_ledger()
    try:
        config = {"away_ledger": {"persist_path": str(tmp_path / "l.json")}}
        assert get_away_ledger(config) is get_away_ledger(config)
    finally:
        reset_away_ledger()


def test_reset_singleton_drops_instance(tmp_path):
    reset_away_ledger()
    try:
        config = {"away_ledger": {"persist_path": str(tmp_path / "l.json")}}
        first = get_away_ledger(config)
        reset_away_ledger()
        assert get_away_ledger(config) is not first
    finally:
        reset_away_ledger()
