import asyncio

import pytest

from core.alert_relay.diagnosis_runner import RunnerResult
from core.alert_relay.models import Diagnosis, RelayState
from core.alert_relay.service import INTENT_DECLINE, INTENT_DIAGNOSE, AlertRelayService

ALERT_TEXT = """【SAE告警通知】
告警等级：严重
告警集群：bj-jxq-autocar
命名空间：iflyplot
告警对象：iflyplot-ai-7d9f8b6c5d-x2k9p
告警规则：日志包含关键词 无痕改字处理超时 >5条
告警时间：2026-08-18 21:00:11
"""

DIAGNOSIS = Diagnosis.from_payload(
    {
        "title": "限流组打满导致改字超时",
        "severity": "严重",
        "root_cause": "限流组并发配置过低。",
        "suggestion": ["核查限流组并发配置"],
    }
)


class FakeRobot:
    def __init__(self, available=True):
        self.calls = []
        self.last_error = "" if available else "设备不在线"
        self._available = available

    def available(self):
        return self._available

    def health(self):
        return {"device_online": self._available}

    async def _record(self, name, *args, **kwargs):
        self.calls.append(name)
        return self._available

    async def alert(self, event):
        return await self._record("alert")

    async def escalate(self, event):
        return await self._record("escalate")

    async def claimed(self, event, by=""):
        return await self._record("claimed")

    async def diagnosing(self, event):
        return await self._record("diagnosing")

    async def diagnosed(self, diagnosis):
        return await self._record("diagnosed")

    async def failed(self, reason):
        return await self._record("failed")

    async def declined(self):
        return await self._record("declined")


class FakeBot:
    def __init__(self, configured=True, fail_send=False):
        self.cards = []
        self.replies = []
        self.texts = []
        self.reactions = []
        self._configured = configured
        self._fail_send = fail_send

    def configured(self):
        return self._configured

    def capabilities(self):
        return {"app_id_configured": self._configured}

    async def send_card(self, receive_id, card):
        if self._fail_send:
            raise RuntimeError("bot is not in the chat")
        self.cards.append((receive_id, card))
        return {"message_id": f"om_{len(self.cards)}", "chat_id": "oc_1"}

    async def reply_card(self, message_id, card):
        self.replies.append((message_id, card))
        return {"message_id": "om_reply"}

    async def reply_text(self, message_id, text):
        self.texts.append((message_id, text))
        return {"message_id": "om_text"}

    async def add_reaction(self, message_id, emoji_type="OK"):
        # 真发请求会让出事件循环，假对象也必须让——否则并发用例根本跑不出竞态。
        await asyncio.sleep(0)
        self.reactions.append(message_id)
        return True


class FakeRunner:
    def __init__(self, result=None):
        self.result = result or RunnerResult(True, diagnosis=DIAGNOSIS, command="claude -p")
        self.events = []

    def health(self):
        return {"cli_command": ["claude"]}

    def command_preview(self):
        return "claude -p --output-format json"

    async def run(self, event):
        self.events.append(event)
        return self.result


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_service(**overrides):
    robot = overrides.pop("robot", FakeRobot())
    bot = overrides.pop("bot", FakeBot())
    runner = overrides.pop("runner", FakeRunner())
    clock = overrides.pop("clock", Clock())
    options = {
        "receive_id": "ou_oncall",
        "enabled": True,
        "clock": clock,
    }
    options.update(overrides)
    service = AlertRelayService(robot=robot, feishu_bot=bot, runner=runner, **options)
    return service, robot, bot, runner, clock


@pytest.mark.asyncio
async def test_ingest_alerts_the_robot_and_the_human_then_waits():
    service, robot, bot, _, _ = make_service()
    result = await service.ingest({"raw_text": ALERT_TEXT})

    assert result["state"] == RelayState.AWAITING_REPLY.value
    assert robot.calls == ["alert"]
    assert bot.cards[0][0] == "ou_oncall"
    assert result["alert"]["workload"] == "iflyplot-ai"
    assert result["feishu_message_id"] == "om_1"


@pytest.mark.asyncio
async def test_ingest_never_starts_a_diagnosis_on_its_own():
    """没有人点头就开跑，是这套设计明确要避免的事。"""
    service, _, _, runner, _ = make_service()
    await service.ingest({"raw_text": ALERT_TEXT})
    await service.wait_for_idle()
    assert runner.events == []


@pytest.mark.asyncio
async def test_reply_asking_for_help_runs_the_local_claude_code_and_reports_back():
    service, robot, bot, runner, _ = make_service()
    ingested = await service.ingest({"raw_text": ALERT_TEXT})

    replied = await service.handle_reply(
        alert_id=ingested["alert_id"], intent=INTENT_DIAGNOSE, user="张三"
    )
    assert replied["state"] == RelayState.CLAIMED.value
    await service.wait_for_idle()

    record = service.get(ingested["alert_id"])
    assert record["state"] == RelayState.DIAGNOSED.value
    assert record["diagnosis"]["root_cause"] == "限流组并发配置过低。"
    assert record["claimed_by"] == "张三"
    assert len(runner.events) == 1
    # 结论回在告警卡片的同一话题下
    assert bot.replies[0][0] == "om_1"
    assert robot.calls == ["alert", "claimed", "diagnosing", "diagnosed"]


@pytest.mark.asyncio
async def test_reply_receipt_emoji_lands_before_the_slow_diagnosis():
    """skill 的回执约定：先回表情表示收到，再慢慢查。"""
    service, _, bot, _, _ = make_service()
    ingested = await service.ingest({"raw_text": ALERT_TEXT})
    await service.handle_reply(
        alert_id=ingested["alert_id"], intent=INTENT_DIAGNOSE, message_id="om_human"
    )
    assert bot.reactions == ["om_human"]


@pytest.mark.asyncio
async def test_free_text_replies_are_understood():
    for text in ("帮我查", "你查一下吧", "开始处理", "排查下原因"):
        service, _, _, runner, _ = make_service()
        ingested = await service.ingest({"raw_text": ALERT_TEXT})
        await service.handle_reply(alert_id=ingested["alert_id"], text=text)
        await service.wait_for_idle()
        assert service.get(ingested["alert_id"])["state"] == RelayState.DIAGNOSED.value, text


@pytest.mark.asyncio
async def test_declining_stops_the_flow_without_burning_tokens():
    service, robot, bot, runner, _ = make_service()
    ingested = await service.ingest({"raw_text": ALERT_TEXT})
    await service.handle_reply(alert_id=ingested["alert_id"], text="我自己看")
    await service.wait_for_idle()

    assert service.get(ingested["alert_id"])["state"] == RelayState.DECLINED.value
    assert runner.events == []
    assert robot.calls == ["alert", "declined"]


@pytest.mark.asyncio
async def test_declining_silences_the_same_alert_for_the_dedupe_window():
    """「我不打扰了」得当真：拒绝后监控原样重发，不能再叫一遍人。

    记录是按指纹归并的，拒绝只对这一条记录生效的话，60 秒后的同一条重发
    就是一条全新告警——机器人再响、飞书再发一张卡片，10 分钟后还会再升级。
    """
    service, robot, bot, _, clock = make_service(dedupe_window_seconds=300)
    ingested = await service.ingest({"raw_text": ALERT_TEXT})
    await service.handle_reply(alert_id=ingested["alert_id"], text="我自己看")

    clock.now += 60
    repeated = await service.ingest({"raw_text": ALERT_TEXT})

    assert repeated["deduped"] is True
    assert repeated["alert_id"] == ingested["alert_id"]
    assert repeated["state"] == RelayState.DECLINED.value
    assert robot.calls == ["alert", "declined"]
    assert len(bot.cards) == 1


@pytest.mark.asyncio
async def test_the_same_alert_long_after_a_decline_is_a_new_incident():
    """静默窗只有去重窗那么长：熄火之后再烧起来是新的一条，照样叫人。"""
    service, robot, _, _, clock = make_service(dedupe_window_seconds=300)
    ingested = await service.ingest({"raw_text": ALERT_TEXT})
    await service.handle_reply(alert_id=ingested["alert_id"], text="我自己看")

    clock.now += 301
    again = await service.ingest({"raw_text": ALERT_TEXT})

    assert again["deduped"] is False
    assert again["alert_id"] != ingested["alert_id"]
    assert again["state"] == RelayState.AWAITING_REPLY.value
    assert robot.calls == ["alert", "declined", "alert"]


@pytest.mark.asyncio
async def test_unrecognized_reply_asks_again_instead_of_guessing():
    """猜错方向要么白烧一次诊断，要么把该查的吞掉，不如问一句。"""
    service, _, bot, runner, _ = make_service()
    ingested = await service.ingest({"raw_text": ALERT_TEXT})
    result = await service.handle_reply(alert_id=ingested["alert_id"], text="嗯？")

    assert result["code"] == "UNKNOWN_INTENT"
    assert service.get(ingested["alert_id"])["state"] == RelayState.AWAITING_REPLY.value
    assert runner.events == []
    assert bot.texts, "应当回一句提示告诉人怎么答"


@pytest.mark.asyncio
async def test_reply_can_be_matched_by_the_message_it_replies_to():
    """人在飞书里直接回消息时不会带 alert_id，只能靠话题根消息认领。"""
    service, _, _, runner, _ = make_service()
    await service.ingest({"raw_text": ALERT_TEXT})
    result = await service.handle_reply(root_message_id="om_1", text="帮我查")
    await service.wait_for_idle()
    assert result["code"] == "OK"
    assert len(runner.events) == 1


@pytest.mark.asyncio
async def test_reply_for_an_unknown_alert_is_rejected_cleanly():
    service, _, _, _, _ = make_service()
    result = await service.handle_reply(alert_id="nope", text="帮我查")
    assert result["code"] == "ALERT_NOT_FOUND"


@pytest.mark.asyncio
async def test_second_claim_is_ignored_so_two_people_cannot_double_run():
    service, _, _, runner, _ = make_service()
    ingested = await service.ingest({"raw_text": ALERT_TEXT})
    await service.handle_reply(alert_id=ingested["alert_id"], text="帮我查")
    second = await service.handle_reply(alert_id=ingested["alert_id"], text="帮我查")
    await service.wait_for_idle()

    assert second["code"] == "ALREADY_HANDLED"
    assert len(runner.events) == 1


@pytest.mark.asyncio
async def test_simultaneous_claims_still_run_exactly_one_diagnosis():
    """两个人同时点「帮我查」：只要状态翻转前有 await 点，第二个就能溜进来跑第二个子进程。"""
    import asyncio

    service, _, _, runner, _ = make_service()
    ingested = await service.ingest({"raw_text": ALERT_TEXT})
    results = await asyncio.gather(
        *[
            service.handle_reply(
                alert_id=ingested["alert_id"], text="帮我查", message_id=f"om_{index}"
            )
            for index in range(4)
        ]
    )
    await service.wait_for_idle()

    assert [result["code"] for result in results].count("OK") == 1
    assert [result["code"] for result in results].count("ALREADY_HANDLED") == 3
    assert len(runner.events) == 1


@pytest.mark.asyncio
async def test_repeated_alerts_inside_the_window_do_not_re_poke_the_robot():
    """告警风暴时机器人被刷屏，用户会直接把它关掉——这比漏告警更糟。"""
    service, robot, bot, _, clock = make_service(dedupe_window_seconds=300)
    first = await service.ingest({"raw_text": ALERT_TEXT})
    clock.now += 60
    second = await service.ingest({"raw_text": ALERT_TEXT.replace("x2k9p", "q7w8e")})

    assert second["alert_id"] == first["alert_id"]
    assert second["deduped"] is True
    assert second["repeat_count"] == 1
    assert robot.calls == ["alert"]
    assert len(bot.cards) == 1


@pytest.mark.asyncio
async def test_the_same_alert_after_the_window_is_a_new_incident():
    service, robot, _, _, clock = make_service(dedupe_window_seconds=300)
    first = await service.ingest({"raw_text": ALERT_TEXT})
    clock.now += 301
    second = await service.ingest({"raw_text": ALERT_TEXT})
    assert second["alert_id"] != first["alert_id"]
    assert robot.calls == ["alert", "alert"]


@pytest.mark.asyncio
async def test_a_different_keyword_is_never_deduped_together():
    service, robot, _, _, _ = make_service()
    await service.ingest({"raw_text": ALERT_TEXT})
    await service.ingest({"raw_text": ALERT_TEXT.replace("无痕改字处理超时", "并发泄漏")})
    assert robot.calls == ["alert", "alert"]


@pytest.mark.asyncio
async def test_diagnosis_failure_is_reported_honestly():
    """宁可回一张「没查成」的卡片，也绝不编一个根因。"""
    runner = FakeRunner(
        RunnerResult(False, reason="诊断超时（900 秒）", detail="子进程未返回", command="claude -p")
    )
    service, robot, bot, _, _ = make_service(runner=runner)
    ingested = await service.ingest({"raw_text": ALERT_TEXT})
    await service.handle_reply(alert_id=ingested["alert_id"], text="帮我查")
    await service.wait_for_idle()

    record = service.get(ingested["alert_id"])
    assert record["state"] == RelayState.FAILED.value
    assert "超时" in record["error"]
    assert record["diagnosis"] is None
    assert robot.calls[-1] == "failed"
    failure_card = bot.replies[-1][1]
    assert failure_card["header"]["template"] == "red"


@pytest.mark.asyncio
async def test_alert_survives_a_dead_feishu_bot():
    """通知挂了不能把告警吞掉——机器人那一路还在，状态也要留痕。"""
    service, robot, _, _, _ = make_service(bot=FakeBot(fail_send=True))
    result = await service.ingest({"raw_text": ALERT_TEXT})
    assert result["state"] == RelayState.AWAITING_REPLY.value
    assert robot.calls == ["alert"]
    assert "bot is not in the chat" in result["feishu_error"]
    assert result["warnings"]


@pytest.mark.asyncio
async def test_alert_survives_an_offline_robot():
    service, _, bot, _, _ = make_service(robot=FakeRobot(available=False))
    result = await service.ingest({"raw_text": ALERT_TEXT})
    assert result["state"] == RelayState.AWAITING_REPLY.value
    assert result["robot_delivered"] is False
    assert len(bot.cards) == 1
    assert result["warnings"]


@pytest.mark.asyncio
async def test_timeout_escalates_but_does_not_auto_run_by_default():
    """告警风暴下自动开跑会同时拉起 N 个 Claude Code，默认必须关。"""
    service, robot, bot, runner, clock = make_service(reply_timeout_seconds=600)
    ingested = await service.ingest({"raw_text": ALERT_TEXT})
    clock.now += 601
    timed_out = await service.check_timeouts()
    await service.wait_for_idle()

    assert [item["alert_id"] for item in timed_out] == [ingested["alert_id"]]
    assert service.get(ingested["alert_id"])["state"] == RelayState.TIMEOUT.value
    assert robot.calls == ["alert", "escalate"]
    assert runner.events == []
    assert bot.texts, "催办消息应当发出去"


@pytest.mark.asyncio
async def test_timeout_can_auto_run_when_explicitly_enabled():
    service, _, _, runner, clock = make_service(
        reply_timeout_seconds=600, auto_diagnose_on_timeout=True
    )
    ingested = await service.ingest({"raw_text": ALERT_TEXT})
    clock.now += 601
    await service.check_timeouts()
    await service.wait_for_idle()

    record = service.get(ingested["alert_id"])
    assert record["state"] == RelayState.DIAGNOSED.value
    assert record["claimed_by"] == "自动（超时未响应）"
    assert len(runner.events) == 1


@pytest.mark.asyncio
async def test_a_late_reply_after_timeout_still_starts_the_diagnosis():
    service, _, _, runner, clock = make_service(reply_timeout_seconds=600)
    ingested = await service.ingest({"raw_text": ALERT_TEXT})
    clock.now += 601
    await service.check_timeouts()
    await service.handle_reply(alert_id=ingested["alert_id"], text="帮我查")
    await service.wait_for_idle()

    assert service.get(ingested["alert_id"])["state"] == RelayState.DIAGNOSED.value
    assert len(runner.events) == 1


@pytest.mark.asyncio
async def test_timeouts_only_fire_once_per_alert():
    """烧完就熄火的告警只升级一次：没人接也别没完没了地喊。"""
    service, robot, _, _, clock = make_service(reply_timeout_seconds=600)
    await service.ingest({"raw_text": ALERT_TEXT})
    clock.now += 601
    await service.check_timeouts()
    clock.now += 601
    assert await service.check_timeouts() == []
    assert robot.calls.count("escalate") == 1


@pytest.mark.asyncio
async def test_a_still_burning_timeout_is_escalated_again():
    """TIMEOUT 不是终点：告警还在烧、还没人接，就得按同样的节奏再喊一次。

    去重窗是滑动的（每次重复上报都把 last_seen_at 推到当下），监控每 60 秒
    重发一次的 P0 永远出不了窗口，指纹也就一直被这条 TIMEOUT 记录占着。
    没有再升级的话，服务端在两次通知之后就对这条告警彻底哑掉了。
    """
    service, robot, bot, _, clock = make_service(
        reply_timeout_seconds=600, dedupe_window_seconds=300
    )
    ingested = await service.ingest({"raw_text": ALERT_TEXT})

    escalated = []
    for _ in range(40):  # 监控每 60 秒重发一次，连烧 40 分钟没人接
        clock.now += 60
        assert (await service.ingest({"raw_text": ALERT_TEXT}))["deduped"] is True
        escalated.extend(await service.check_timeouts())

    assert service.get(ingested["alert_id"])["state"] == RelayState.TIMEOUT.value
    # 每过一个回复超时催一次，而不是第一次之后就再也不吭声
    assert robot.calls.count("escalate") == 4
    assert [item["alert_id"] for item in escalated] == [ingested["alert_id"]] * 4
    # 不新开卡片、不新建记录：还是那条告警，只是在原话题下再催一次
    assert len(bot.cards) == 1
    assert len(bot.texts) == 4
    assert len(service.recent()) == 1


@pytest.mark.asyncio
async def test_a_human_claim_during_the_escalation_beats_the_auto_claim():
    """升级提醒正是催人去点卡片的那一下，人在这个 await 里认领最自然。

    巡检回来必须复核状态：既不能把认领人覆盖成「自动」，也不能拿
    TIMEOUT→CLAIMED 去撞一条已经离开 TIMEOUT 的记录（非法流转会抛异常，
    把本轮剩下的超时告警全部带走）。
    """
    robot = FakeRobot()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hanging_escalate(event):
        entered.set()
        await release.wait()
        return await robot._record("escalate")

    robot.escalate = hanging_escalate
    service, _, _, runner, clock = make_service(
        robot=robot, reply_timeout_seconds=600, auto_diagnose_on_timeout=True
    )
    ingested = await service.ingest({"raw_text": ALERT_TEXT})
    clock.now += 601

    sweeping = asyncio.ensure_future(service.check_timeouts())
    await entered.wait()
    replied = await service.handle_reply(
        alert_id=ingested["alert_id"], text="帮我查", user="张三"
    )
    assert replied["code"] == "OK"
    release.set()
    await sweeping  # 人抢先认领不该让整轮巡检抛异常
    await service.wait_for_idle()

    record = service.get(ingested["alert_id"])
    assert record["claimed_by"] == "张三"
    assert record["state"] == RelayState.DIAGNOSED.value
    assert len(runner.events) == 1


@pytest.mark.asyncio
async def test_disabled_service_refuses_ingest_instead_of_half_working():
    service, robot, bot, _, _ = make_service(enabled=False)
    result = await service.ingest({"raw_text": ALERT_TEXT})
    assert result["code"] == "ALERT_RELAY_DISABLED"
    assert robot.calls == []
    assert bot.cards == []


@pytest.mark.asyncio
async def test_ingest_requires_raw_text():
    service, _, _, _, _ = make_service()
    result = await service.ingest({"level": "严重"})
    assert result["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_explicit_fields_override_the_parsed_text():
    service, _, _, runner, _ = make_service()
    ingested = await service.ingest(
        {"raw_text": ALERT_TEXT, "namespace": "iflyplot-pre", "keyword": "并发泄漏"}
    )
    assert ingested["alert"]["namespace"] == "iflyplot-pre"
    assert ingested["alert"]["keyword"] == "并发泄漏"


@pytest.mark.asyncio
async def test_health_reports_each_dependency_separately():
    service, _, _, _, _ = make_service()
    health = service.health()
    assert health["enabled"] is True
    assert health["receive_id_configured"] is True
    assert "robot" in health and "feishu" in health and "diagnosis" in health


@pytest.mark.asyncio
async def test_recent_keeps_newest_first_and_is_bounded():
    service, _, _, _, clock = make_service(max_records=3)
    for index in range(5):
        clock.now += 400
        await service.ingest({"raw_text": ALERT_TEXT.replace("超时", f"超时{index}")})
    recent = service.recent()
    assert len(recent) == 3
    assert recent[0]["created_at"] > recent[-1]["created_at"]
