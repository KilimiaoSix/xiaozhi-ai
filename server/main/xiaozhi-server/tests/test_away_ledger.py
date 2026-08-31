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


def test_compose_speech_stays_within_length_target_when_not_folded(env):
    """混合六类事件但都没触发折叠时，整体长度贴着 200 字目标，不再受"至多三句"硬顶约束。

    旧断言 speech.count("。") <= 3 与新规则冲突：新规则里严重告警/留言逐条播、
    等待操作最多播 3 条，都可能各自独立成句，句数不再是长度控制的手段。
    """
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
    assert len(speech) <= 200
    assert "其余我记在桌面端返岗页了" not in speech


def test_compose_speech_hours_and_minutes(env):
    ledger, clock, _path = env
    ledger.mark_away()
    ledger.record("generic", "周会挪到四点")
    clock.advance(hours=2)

    assert ledger.compose_speech().startswith("你离开的两小时里，")


def test_compose_speech_visitor_messages_are_never_folded(env):
    """留言不能丢内容：多条留言要逐条播完，不能只报最新一条 + 计数。"""
    ledger, clock, _path = env
    ledger.mark_away()

    ledger.record("visitor_message", "有同事留言：日志方案已发飞书")
    ledger.record("visitor_message", "有同事留言：下午两点对一下接口")
    clock.advance(minutes=5)

    speech = ledger.compose_speech()

    assert "有 2 条留言" in speech
    assert "日志方案已发飞书" in speech
    assert "下午两点对一下接口" in speech
    assert "其余我记在桌面端返岗页了" not in speech


def test_compose_speech_completed_and_failed_only_report_counts(env):
    """已完成桶（含失败）只报计数，不念内容——细节留给桌面端返岗页。"""
    ledger, clock, _path = env
    ledger.mark_away()

    ledger.record("agent_completed", "跑完了很长很长的一段集成测试", task_key="t1", source="Codex")
    ledger.record("agent_failed", "构建失败，报错信息巨长巨长巨长", task_key="t2")
    clock.advance(minutes=5)

    speech = ledger.compose_speech()

    assert "Codex 完成了 1 个任务" in speech
    assert "有 1 个任务失败了" in speech
    assert "构建失败" not in speech
    assert "报错信息" not in speech


def test_compose_speech_generic_only_reports_count(env):
    """只有普通消息时，只报条数，不念任何一条的原文。"""
    ledger, clock, _path = env
    ledger.mark_away()

    ledger.record("generic", "周会挪到四点")
    ledger.record("generic", "打印机没纸了")
    ledger.record("generic", "快递到前台了")
    clock.advance(minutes=5)

    speech = ledger.compose_speech()

    assert speech == "你离开的五分钟里，有 3 条普通消息。"
    assert "其余我记在桌面端返岗页了" not in speech


def test_compose_speech_needs_user_exactly_three_speaks_all_no_fold(env):
    """等待操作恰好 3 条：边界内逐条播完，不折叠、不加导流尾句。"""
    ledger, clock, _path = env
    ledger.mark_away()

    ledger.record("agent_needs_user", "确认部署方案 A", task_key="t1")
    ledger.record("agent_needs_user", "确认部署方案 B", task_key="t2")
    ledger.record("agent_needs_user", "确认部署方案 C", task_key="t3")
    clock.advance(minutes=5)

    speech = ledger.compose_speech()

    assert "有 3 个任务在等你确认" in speech
    assert "确认部署方案 A" in speech
    assert "确认部署方案 B" in speech
    assert "确认部署方案 C" in speech
    assert "其余我记在桌面端返岗页了" not in speech


def test_compose_speech_needs_user_four_items_folds_with_headline_and_count(env):
    """等待操作超过 3 条：只播头条 + 报总数，并在结尾加导流尾句。"""
    ledger, clock, _path = env
    ledger.mark_away()

    ledger.record("agent_needs_user", "确认部署方案 A", task_key="t1")
    ledger.record("agent_needs_user", "确认部署方案 B", task_key="t2")
    ledger.record("agent_needs_user", "确认部署方案 C", task_key="t3")
    ledger.record("agent_needs_user", "确认部署方案 D", task_key="t4")
    clock.advance(minutes=5)

    speech = ledger.compose_speech()

    assert "有 4 个任务在等你确认" in speech
    # 只播头条（最新一条），不逐条列出被折叠掉的其余几条
    assert "确认部署方案 D" in speech
    assert "确认部署方案 A" not in speech
    assert "确认部署方案 B" not in speech
    assert "确认部署方案 C" not in speech
    assert speech.endswith("其余我记在桌面端返岗页了。")


def test_compose_speech_never_folds_critical_and_visitor_even_when_others_fold(env):
    """严重告警和留言永不折叠：即便等待操作因超量触发折叠，这两类内容也不能被截断或丢弃。"""
    ledger, clock, _path = env
    ledger.mark_away()

    ledger.record("incident", "服务 A 挂了", severity="critical")
    ledger.record("incident", "服务 B 挂了", severity="critical")
    ledger.record("incident", "服务 C 挂了", severity="critical")
    ledger.record("visitor_message", "有同事留言：合同要今天签")
    ledger.record("visitor_message", "有同事留言：快递到前台了")
    ledger.record("visitor_message", "有同事留言：下午三点开会")
    ledger.record("agent_needs_user", "确认部署方案 A", task_key="t1")
    ledger.record("agent_needs_user", "确认部署方案 B", task_key="t2")
    ledger.record("agent_needs_user", "确认部署方案 C", task_key="t3")
    ledger.record("agent_needs_user", "确认部署方案 D", task_key="t4")
    clock.advance(minutes=5)

    speech = ledger.compose_speech()

    # 折叠确实发生了（等待操作超过 3 条），尾句该在
    assert speech.endswith("其余我记在桌面端返岗页了。")
    # 但严重告警三条全在，一条都不能少
    assert "服务 A 挂了" in speech
    assert "服务 B 挂了" in speech
    assert "服务 C 挂了" in speech
    # 留言三条也全在
    assert "合同要今天签" in speech
    assert "快递到前台了" in speech
    assert "下午三点开会" in speech


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
