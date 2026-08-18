import json

from core.alert_relay.cards import (
    INTENT_DECLINE,
    INTENT_DIAGNOSE,
    build_alert_card,
    build_diagnosis_card,
    build_failure_card,
)
from core.alert_relay.models import AlertEvent, Diagnosis, RelayRecord


def make_record(**overrides):
    event = AlertEvent(
        raw_text="【告警】",
        level=overrides.pop("level", "严重"),
        cluster="bj-jxq-autocar",
        namespace="iflyplot",
        target="iflyplot-ai-7d9f8b6c5d-x2k9p",
        workload="iflyplot-ai",
        keyword="无痕改字处理超时",
        alert_time="2026-08-18 21:00:11",
        policy_url="https://one.iflytek.com/sae/#/alarm?projectId=117&clusterId=3",
    )
    record = RelayRecord(alert_id="alert-1", event=event, created_at=100.0)
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


def texts_of(card):
    """把卡片里所有文本抠出来，便于断言内容出现过。"""
    dumped = json.dumps(card, ensure_ascii=False)
    return dumped


def buttons_of(card):
    found = []
    for element in card["elements"]:
        if element.get("tag") == "action":
            found.extend(element.get("actions", []))
    return found


def test_alert_card_buttons_carry_alert_id_and_intent():
    """回调只带按钮的 value，认不出是哪条告警就没法接回状态机。"""
    card = build_alert_card(make_record())
    values = [json.loads(b["value"]) if isinstance(b["value"], str) else b["value"]
              for b in buttons_of(card) if "value" in b]
    intents = {v["intent"]: v["alert_id"] for v in values}
    assert intents[INTENT_DIAGNOSE] == "alert-1"
    assert intents[INTENT_DECLINE] == "alert-1"


def test_alert_card_header_color_follows_level():
    assert build_alert_card(make_record(level="紧急"))["header"]["template"] == "red"
    assert build_alert_card(make_record(level="严重"))["header"]["template"] == "orange"
    assert build_alert_card(make_record(level="警告"))["header"]["template"] == "yellow"


def test_alert_card_shows_the_facts_needed_to_judge_without_leaving_feishu():
    dumped = texts_of(build_alert_card(make_record()))
    for fact in ("bj-jxq-autocar", "iflyplot", "iflyplot-ai", "无痕改字处理超时", "2026-08-18 21:00:11"):
        assert fact in dumped


def test_alert_card_links_to_the_policy_url_when_present():
    dumped = texts_of(build_alert_card(make_record()))
    assert "https://one.iflytek.com/sae/" in dumped


def test_alert_card_says_when_the_robot_could_not_be_reached():
    """机器人离线是要让人知道的：否则人会以为「机器人没动=没告警」。"""
    online = texts_of(build_alert_card(make_record(robot_delivered=True)))
    offline = texts_of(build_alert_card(make_record(robot_delivered=False, robot_error="设备不在线")))
    assert "机器人" in offline and "离线" in offline
    assert "离线" not in online


def test_alert_card_shows_repeat_count_only_when_repeated():
    once = texts_of(build_alert_card(make_record(repeat_count=0)))
    many = texts_of(build_alert_card(make_record(repeat_count=7)))
    assert "重复" not in once
    assert "7" in many and "重复" in many


def full_diagnosis():
    return Diagnosis.from_payload(
        {
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
            "suggestion": ["核查限流组并发配置", "评估临时扩容"],
        }
    )


def test_diagnosis_card_renders_every_contract_section():
    dumped = texts_of(build_diagnosis_card(make_record(), full_diagnosis()))
    for fragment in (
        "限流组打满导致改字超时",
        "21:00:11 到 21:03:42",
        "RateLimiter.java:88",
        "当前并发 2/2",
        "同窗口慢查询 0 条",
        "限流组并发配置过低",
        "核查限流组并发配置",
        "u_1001",
    ):
        assert fragment in dumped, fragment


def test_diagnosis_card_never_truncates_task_ids():
    dumped = texts_of(build_diagnosis_card(make_record(), full_diagnosis()))
    assert "3f2a1b0c-4d5e-6f70-8192-a3b4c5d6e7f8" in dumped


def test_diagnosis_card_omits_empty_sections_instead_of_rendering_blank_blocks():
    minimal = Diagnosis.from_payload({"title": "t", "root_cause": "r"})
    card = build_diagnosis_card(make_record(), minimal)
    dumped = texts_of(card)
    for heading in ("**时间轴**", "**已排除**", "**建议**", "**受影响的请求**"):
        assert heading not in dumped
    assert "**根因**" in dumped


def test_diagnosis_card_states_it_is_read_only():
    """来源声明是 skill 输出标准的第 8 条，卡片上必须看得见。"""
    dumped = texts_of(build_diagnosis_card(make_record(), full_diagnosis()))
    assert "只读" in dumped


def test_failure_card_tells_the_operator_how_to_retry_by_hand():
    card = build_failure_card(make_record(), "诊断超时", "子进程 600 秒未返回", retry_hint="claude -p ...")
    dumped = texts_of(card)
    assert card["header"]["template"] == "red"
    assert "诊断超时" in dumped
    assert "子进程 600 秒未返回" in dumped
    assert "claude -p ..." in dumped


def test_failure_card_clips_pasted_raw_output():
    """输出不合契约时要附原文，但不能把几万字日志整段糊进卡片。"""
    card = build_failure_card(make_record(), "输出不合契约", "x" * 5000)
    dumped = texts_of(card)
    assert len(dumped) < 3000
    assert "…" in dumped
