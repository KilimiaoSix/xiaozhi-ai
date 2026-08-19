import pytest

from core.alert_relay.models import (
    AlertEvent,
    Diagnosis,
    DiagnosisFormatError,
    RelayRecord,
    RelayState,
    can_transition,
)


def make_event(**overrides):
    payload = {
        "raw_text": "【告警】",
        "level": "严重",
        "cluster": "bj-jxq-autocar",
        "namespace": "iflyplot",
        "target": "iflyplot-ai-7d9f8b6c5d-x2k9p",
        "workload": "iflyplot-ai",
        "keyword": "无痕改字处理超时",
        "alert_time": "2026-08-18 21:00:00",
    }
    payload.update(overrides)
    return AlertEvent(**payload)


def test_fingerprint_ignores_pod_suffix_and_alert_time():
    """同一个 workload 的同一条规则连续告警必须落到同一个指纹。

    告警对象每次重启都换 pod 名、告警时间每次都不同，指纹若把它们算进去
    就永远去不了重，机器人会被告警风暴刷屏。
    """
    first = make_event(target="iflyplot-ai-7d9f8b6c5d-x2k9p", alert_time="21:00:00")
    second = make_event(target="iflyplot-ai-9x0y1z2a3b-q7w8e", alert_time="21:04:00")
    assert first.fingerprint() == second.fingerprint()


def test_fingerprint_separates_different_keywords():
    assert make_event().fingerprint() != make_event(keyword="并发泄漏").fingerprint()


def test_summary_is_short_enough_for_the_oled_screen():
    """屏幕 16px 中文一行约 8 字、最多 4 行，摘要必须先于硬件截断。"""
    summary = make_event().summary()
    assert "iflyplot-ai" in summary
    assert "无痕改字处理超时" in summary
    assert len(summary) <= 32


def test_state_machine_allows_the_happy_path():
    path = [
        RelayState.RECEIVED,
        RelayState.NOTIFIED,
        RelayState.AWAITING_REPLY,
        RelayState.CLAIMED,
        RelayState.DIAGNOSING,
        RelayState.DIAGNOSED,
    ]
    for current, nxt in zip(path, path[1:]):
        assert can_transition(current, nxt)


def test_state_machine_rejects_skipping_human_confirmation():
    """没有人认领就直接开跑诊断是本设计明确禁止的。"""
    assert not can_transition(RelayState.AWAITING_REPLY, RelayState.DIAGNOSING)
    assert not can_transition(RelayState.NOTIFIED, RelayState.CLAIMED)


def test_timeout_can_still_be_claimed_later():
    assert can_transition(RelayState.AWAITING_REPLY, RelayState.TIMEOUT)
    assert can_transition(RelayState.TIMEOUT, RelayState.CLAIMED)


def test_terminal_states_have_no_out_edges():
    for terminal in (RelayState.DIAGNOSED, RelayState.DECLINED, RelayState.FAILED):
        assert not can_transition(terminal, RelayState.CLAIMED)


def test_record_transition_records_history_and_rejects_illegal_moves():
    record = RelayRecord(alert_id="a-1", event=make_event(), created_at=100.0)
    record.transition(RelayState.NOTIFIED, at=101.0)
    assert record.state is RelayState.NOTIFIED
    assert record.updated_at == 101.0
    assert [entry["state"] for entry in record.history] == ["NOTIFIED"]

    with pytest.raises(ValueError):
        record.transition(RelayState.DIAGNOSED, at=102.0)
    assert record.state is RelayState.NOTIFIED


def test_diagnosis_normalizes_the_skill_output_contract():
    payload = {
        "title": "限流组打满导致改字超时",
        "severity": "严重",
        "time_window": "21:00:11 到 21:03:42",
        "affected_summary": "3 名用户的改字任务超时。",
        "affected": [
            {
                "time": "21:00:11",
                "uid": "u_1001",
                "taskId": "3f2a1b0c-4d5e-6f70-8192-a3b4c5d6e7f8",
                "note": "提交后无回调",
            }
        ],
        "user_impact": "用户点了改字，等到超时也没结果。",
        "timeline": ["21:00 提交任务", "21:03 超时清扫"],
        "why": [
            {
                "point": "限流组并发为 2",
                "code": "RateLimiter.java:88",
                "log": "当前并发 2/2，进入等待",
            }
        ],
        "ruled_out": ["同窗口慢查询 0 条"],
        "root_cause": "限流组并发配置过低。",
        "suggestion": ["核查限流组并发配置"],
        "unexpected_field": "should be dropped",
    }
    diagnosis = Diagnosis.from_payload(payload)
    assert diagnosis.title == "限流组打满导致改字超时"
    assert diagnosis.severity == "严重"
    assert diagnosis.affected[0]["taskId"].endswith("a3b4c5d6e7f8")
    assert diagnosis.why[0]["code"] == "RateLimiter.java:88"
    assert "unexpected_field" not in diagnosis.to_dict()


def test_diagnosis_keeps_full_task_id():
    """skill 的输出标准写死了 taskId 绝不截断成前 8 位，模型层不能替它截。"""
    task_id = "3f2a1b0c-4d5e-6f70-8192-a3b4c5d6e7f8"
    diagnosis = Diagnosis.from_payload(
        {
            "title": "t",
            "root_cause": "r",
            "affected": [{"taskId": task_id}],
        }
    )
    assert diagnosis.affected[0]["taskId"] == task_id


def test_diagnosis_tolerates_missing_optional_arrays():
    diagnosis = Diagnosis.from_payload({"title": "t", "root_cause": "r"})
    assert diagnosis.timeline == []
    assert diagnosis.why == []
    assert diagnosis.suggestion == []


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"title": "只有标题"},
        {"root_cause": "只有根因"},
        {"title": "  ", "root_cause": "r"},
    ],
)
def test_diagnosis_rejects_payloads_that_break_the_contract(payload):
    """契约破了就必须失败，绝不猜——猜出来的根因比没有更危险。"""
    with pytest.raises(DiagnosisFormatError):
        Diagnosis.from_payload(payload)


def test_diagnosis_speech_line_is_one_short_sentence():
    diagnosis = Diagnosis.from_payload(
        {
            "title": "限流组打满导致改字超时",
            "root_cause": "限流组并发配置过低，任务排队到超时。这是第二句。",
        }
    )
    line = diagnosis.speech_line()
    assert line.startswith("查清了")
    assert "。这是第二句" not in line
