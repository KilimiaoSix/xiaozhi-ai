"""晨报播报文案：短、按优先级、只念待办。"""

from core.morning_brief.announcement import build_announcement


def make_report(**overrides):
    report = {
        "report_type": "OPEN_ATTENTION",
        "report_date": "2026-08-19",
        "coverage_status": "COMPLETE",
        "top_three": [],
        "other_unhandled_mentions": [],
        "calendar": [],
        "reauthorization_required": False,
        "permission_required": False,
    }
    report.update(overrides)
    return report


def ranked(item_id, title, *, kind="MESSAGE"):
    return {
        "kind": kind,
        "item_id": item_id,
        "topic_id": f"topic:{item_id}",
        "title": title,
        "score": 100,
        "reasons": ["direct_mention"],
        "source_url": "https://example.invalid",
        "status": "OPEN_NEW",
        "confidence": "HIGH",
    }


def test_todos_are_numbered_in_report_priority_order():
    report = make_report(
        top_three=[
            ranked("m1", "回滚线上发布"),
            ranked("m2", "确认排期"),
            ranked("m3", "补测试报告"),
        ]
    )

    announcement = build_announcement(report)

    assert announcement.text.index("1 回滚线上发布") < announcement.text.index(
        "2 确认排期"
    )
    assert announcement.text.index("2 确认排期") < announcement.text.index(
        "3 补测试报告"
    )
    assert announcement.speak is True
    assert len(announcement.text) <= 100


def test_long_excerpt_is_clipped_so_the_brief_stays_short():
    report = make_report(top_three=[ranked("m1", "线" * 200)])

    announcement = build_announcement(report, item_chars=8)

    assert "线" * 8 + "…" in announcement.text
    assert "线" * 9 not in announcement.text


def test_only_max_items_are_announced():
    report = make_report(
        top_three=[
            ranked("m1", "第一件"),
            ranked("m2", "第二件"),
            ranked("m3", "第三件"),
        ]
    )

    announcement = build_announcement(report, max_items=2)

    assert "第三件" not in announcement.text
    assert "2 件待办" in announcement.text


def test_calendar_item_is_announced_with_its_start_time():
    report = make_report(
        top_three=[ranked("e1", "迭代评审", kind="CALENDAR")],
        calendar=[
            {
                "event_id": "e1",
                "summary": "迭代评审",
                "start": "2026-08-19T10:00:00+08:00",
                "end": "2026-08-19T11:00:00+08:00",
                "timezone": "Asia/Shanghai",
                "location": "",
                "rsvp_status": "accept",
                "organizer": "",
                "source_url": "https://example.invalid",
                "conflicts": [],
            }
        ],
    )

    announcement = build_announcement(report)

    assert "1 10:00 迭代评审" in announcement.text


def test_other_mentions_collapse_into_a_count():
    report = make_report(
        top_three=[ranked("m1", "回滚线上发布")],
        other_unhandled_mentions=[{"message_id": "m9"}, {"message_id": "m8"}],
    )

    announcement = build_announcement(report)

    assert "另有 2 条待回" in announcement.text
    assert "m9" not in announcement.text


def test_no_open_item_says_so_in_one_sentence():
    announcement = build_announcement(make_report())

    assert "待办" in announcement.text
    assert len(announcement.text) <= 20
    assert announcement.speak is True


def test_default_wording_is_the_morning_brief_one():
    announcement = build_announcement(
        make_report(top_three=[ranked("m1", "回滚线上发布")])
    )

    assert announcement.text.startswith("早上好，")
    assert announcement.status == "早报"
    assert announcement.emotion == "happy"


def test_greeting_status_and_emotion_are_configurable():
    report = make_report(top_three=[ranked("m1", "回滚线上发布")])

    announcement = build_announcement(
        report, greeting="早", status="今日待办", emotion="laughing"
    )

    assert announcement.text.startswith("早，")
    assert announcement.status == "今日待办"
    assert announcement.emotion == "laughing"


def test_the_no_todo_sentence_uses_the_same_configured_wording():
    announcement = build_announcement(
        make_report(), greeting="早", status="今日待办", emotion="laughing"
    )

    assert announcement.text.startswith("早，")
    assert announcement.status == "今日待办"
    assert announcement.emotion == "laughing"


def test_empty_greeting_drops_the_prefix():
    # 到岗迎接已经说过「早上好」时，晨报不必再问一次好
    report = make_report(top_three=[ranked("m1", "回滚线上发布")])

    announcement = build_announcement(report, greeting="")

    assert announcement.text.startswith("今天 1 件待办：")


def test_empty_greeting_also_applies_to_the_no_todo_sentence():
    announcement = build_announcement(make_report(), greeting="")

    assert announcement.text == "今天暂时没有待办。"


def test_unavailable_notice_keeps_its_own_wording():
    announcement = build_announcement(
        make_report(reauthorization_required=True),
        status="今日待办",
        emotion="laughing",
    )

    assert announcement.status != "今日待办"
    assert announcement.emotion != "laughing"


def test_partial_coverage_is_disclosed():
    report = make_report(
        coverage_status="PARTIAL", top_three=[ranked("m1", "回滚线上发布")]
    )

    announcement = build_announcement(report)

    assert "不全" in announcement.text


def test_complete_coverage_does_not_add_a_disclaimer():
    report = make_report(top_three=[ranked("m1", "回滚线上发布")])

    assert "不全" not in build_announcement(report).text


def test_reauthorization_required_is_shown_but_not_spoken():
    report = make_report(reauthorization_required=True, top_three=[])

    announcement = build_announcement(report)

    assert "授权" in announcement.text
    assert announcement.speak is False
    assert announcement.status != "早报"


def test_permission_required_is_shown_but_not_spoken():
    report = make_report(
        permission_required=True, top_three=[ranked("m1", "回滚线上发布")]
    )

    announcement = build_announcement(report)

    assert "权限" in announcement.text
    assert announcement.speak is False
