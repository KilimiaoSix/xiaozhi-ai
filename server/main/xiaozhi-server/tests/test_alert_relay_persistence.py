"""告警值班中继记录的落盘与重启恢复。

记录原本全在内存里。真实后果：服务端一重启，飞书里那张卡片还挂着，
人回一句「帮我查」，回调按 alert_id 查不到记录 → ALERT_NOT_FOUND，
诊断结论也就永远回不到那条话题下面。超时巡检的基线一并丢失，
重启前已经等了 9 分钟的告警，重启后要从零再等 10 分钟。

本文件锁的是「新实例从同一 data 目录装载」：
- 非终态记录连同 alert_id / feishu_message_id / 事件原文一起回来
- 超时巡检基线用落盘的时间戳，不是进程启动时刻
- 终态记录不再占着去重指纹，同一条规则再来要能重新叫人
- 坏文件按空存储处理，不崩在启动路径上
"""

import json

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
    def __init__(self):
        self.calls = []
        self.last_error = ""

    def available(self):
        return True

    def health(self):
        return {"device_online": True}

    async def _record(self, name, *args, **kwargs):
        self.calls.append(name)
        return True

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
    def __init__(self):
        self.cards = []
        self.replies = []
        self.texts = []
        self.reactions = []

    def configured(self):
        return True

    def capabilities(self):
        return {"app_id_configured": True}

    async def send_card(self, receive_id, card):
        self.cards.append((receive_id, card))
        return {"message_id": f"om_{len(self.cards)}", "chat_id": "oc_1"}

    async def reply_card(self, message_id, card):
        self.replies.append((message_id, card))
        return {"message_id": "om_reply"}

    async def reply_text(self, message_id, text):
        self.texts.append((message_id, text))
        return {"message_id": "om_text"}

    async def add_reaction(self, message_id, emoji_type="OK"):
        self.reactions.append(message_id)
        return True


class FakeRunner:
    def __init__(self, result=None):
        self.result = result or RunnerResult(
            True, diagnosis=DIAGNOSIS, command="claude -p"
        )
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

    def advance(self, seconds):
        self.now += float(seconds)


def make_service(store, *, clock=None, **overrides):
    """一个「进程」。同一个 store 建第二个实例即模拟重启。"""
    robot = overrides.pop("robot", FakeRobot())
    bot = overrides.pop("bot", FakeBot())
    runner = overrides.pop("runner", FakeRunner())
    options = {
        "receive_id": "ou_oncall",
        "enabled": True,
        "clock": clock or Clock(),
        "persist_path": str(store),
    }
    options.update(overrides)
    service = AlertRelayService(robot=robot, feishu_bot=bot, runner=runner, **options)
    return service, robot, bot, runner


@pytest.fixture
def store(tmp_path):
    return tmp_path / "alert_relay_records.json"


# ------------------------------------------------------------------ 落盘

@pytest.mark.asyncio
async def test_ingest_persists_the_record_atomically(store):
    service, _, _, _ = make_service(store)

    ingested = await service.ingest({"raw_text": ALERT_TEXT})

    data = json.loads(store.read_text(encoding="utf-8"))
    saved = data["records"][0]
    assert saved["alert_id"] == ingested["alert_id"]
    assert saved["state"] == RelayState.AWAITING_REPLY.value
    assert saved["feishu_message_id"] == "om_1"
    # 诊断的输入是告警原文，to_dict() 里没有它，落盘必须自己带上
    assert saved["event"]["raw_text"].startswith("【SAE告警通知】")
    assert list(store.parent.glob("*.tmp")) == []


# --------------------------------------------------------------- 重启后回复

@pytest.mark.asyncio
async def test_reply_after_restart_finds_the_record_by_alert_id(store):
    """重启前发出去的卡片，人回「帮我查」不该再撞 ALERT_NOT_FOUND。"""
    first, _, _, _ = make_service(store)
    ingested = await first.ingest({"raw_text": ALERT_TEXT})

    service, robot, bot, runner = make_service(store)
    replied = await service.handle_reply(
        alert_id=ingested["alert_id"], intent=INTENT_DIAGNOSE, user="张三"
    )
    assert replied["code"] == "OK"
    assert replied["state"] == RelayState.CLAIMED.value
    await service.wait_for_idle()

    record = service.get(ingested["alert_id"])
    assert record["state"] == RelayState.DIAGNOSED.value
    assert record["claimed_by"] == "张三"
    # 诊断真的拿到了重启前那条告警的原文
    assert runner.events[0].raw_text.startswith("【SAE告警通知】")
    # 结论回在原卡片的话题下，而不是另起一条
    assert bot.replies[0][0] == "om_1"


@pytest.mark.asyncio
async def test_reply_after_restart_finds_the_record_by_root_message_id(store):
    """飞书回调常常只给 root_message_id，这条索引也得活过重启。"""
    first, _, _, _ = make_service(store)
    await first.ingest({"raw_text": ALERT_TEXT})

    service, _, _, _ = make_service(store)
    replied = await service.handle_reply(
        root_message_id="om_1", intent=INTENT_DECLINE, user="李四"
    )

    assert replied["code"] == "OK"
    assert replied["state"] == RelayState.DECLINED.value


@pytest.mark.asyncio
async def test_diagnosis_result_survives_a_restart(store):
    first, _, _, _ = make_service(store)
    ingested = await first.ingest({"raw_text": ALERT_TEXT})
    await first.handle_reply(alert_id=ingested["alert_id"], intent=INTENT_DIAGNOSE)
    await first.wait_for_idle()

    service, _, _, _ = make_service(store)
    record = service.get(ingested["alert_id"])

    assert record["state"] == RelayState.DIAGNOSED.value
    assert record["diagnosis"]["root_cause"] == "限流组并发配置过低。"
    assert record["history"][-1]["state"] == RelayState.DIAGNOSED.value


# ------------------------------------------------------------------ 超时基线

@pytest.mark.asyncio
async def test_timeout_baseline_comes_from_the_persisted_timestamp(store):
    """重启不该让「已经等了 9 分钟」的告警从零重新计时。"""
    clock = Clock()
    first, _, _, _ = make_service(store, clock=clock)
    await first.ingest({"raw_text": ALERT_TEXT})

    clock.advance(700)  # 超过默认的 600 秒回复超时
    service, robot, _, _ = make_service(store, clock=clock)
    timed_out = await service.check_timeouts()

    assert [item["state"] for item in timed_out] == [RelayState.TIMEOUT.value]
    assert "escalate" in robot.calls


@pytest.mark.asyncio
async def test_timeout_does_not_fire_early_after_a_restart(store):
    clock = Clock()
    first, _, _, _ = make_service(store, clock=clock)
    await first.ingest({"raw_text": ALERT_TEXT})

    clock.advance(60)
    service, _, _, _ = make_service(store, clock=clock)

    assert await service.check_timeouts() == []


# ------------------------------------------------------------------ 去重指纹

@pytest.mark.asyncio
async def test_active_fingerprint_still_dedupes_after_a_restart(store):
    clock = Clock()
    first, _, _, _ = make_service(store, clock=clock)
    await first.ingest({"raw_text": ALERT_TEXT})

    clock.advance(10)
    service, robot, _, _ = make_service(store, clock=clock)
    repeated = await service.ingest({"raw_text": ALERT_TEXT})

    assert repeated["deduped"] is True
    assert repeated["repeat_count"] == 1
    assert robot.calls == []  # 重启不该让同一条告警又叫一次人


@pytest.mark.asyncio
async def test_terminal_record_does_not_block_a_new_alert_after_restart(store):
    """已经处理完的告警不该占着指纹，同一条规则再烧起来必须重新叫人。"""
    clock = Clock()
    first, _, _, _ = make_service(store, clock=clock)
    ingested = await first.ingest({"raw_text": ALERT_TEXT})
    await first.handle_reply(alert_id=ingested["alert_id"], intent=INTENT_DECLINE)

    clock.advance(10)
    service, robot, _, _ = make_service(store, clock=clock)
    again = await service.ingest({"raw_text": ALERT_TEXT})

    assert again["deduped"] is False
    assert again["alert_id"] != ingested["alert_id"]
    assert robot.calls == ["alert"]


@pytest.mark.asyncio
async def test_interrupted_diagnosis_is_reported_as_failed(store):
    """诊断跑到一半进程没了：恢复出来的记录不能永远停在 DIAGNOSING。

    子进程随进程一起死了，没人会再回调它。挂着不动的话，这条记录既拿不到
    结论，又一直占着非终态——「查不出来就说查不出来」在这里同样适用。
    """
    first, _, _, _ = make_service(store)
    ingested = await first.ingest({"raw_text": ALERT_TEXT})
    # 手工把盘上的状态改成「崩在诊断里」，模拟进程被 kill
    data = json.loads(store.read_text(encoding="utf-8"))
    data["records"][0]["state"] = RelayState.DIAGNOSING.value
    data["records"][0]["history"].append(
        {"state": RelayState.DIAGNOSING.value, "at": 1000.0, "note": ""}
    )
    store.write_text(json.dumps(data), encoding="utf-8")

    service, _, _, _ = make_service(store)
    record = service.get(ingested["alert_id"])

    assert record["state"] == RelayState.FAILED.value
    assert "重启" in record["error"]
    # 终态了就不该再占着去重指纹
    again = await service.ingest({"raw_text": ALERT_TEXT})
    assert again["deduped"] is False


# ------------------------------------------------------------ 坏文件 / 缺省

@pytest.mark.asyncio
async def test_corrupt_store_is_treated_as_empty(store):
    store.write_text('{"version": 1, "records": [{"alert_id', encoding="utf-8")

    service, _, _, _ = make_service(store)

    assert service.recent() == []
    # 坏文件不能一直卡着：下一次写入要能整体覆盖
    ingested = await service.ingest({"raw_text": ALERT_TEXT})
    assert (
        json.loads(store.read_text(encoding="utf-8"))["records"][0]["alert_id"]
        == ingested["alert_id"]
    )


@pytest.mark.asyncio
async def test_without_a_persist_path_nothing_touches_the_disk(tmp_path):
    """离线单测构造的服务不落盘：注入路径才开启持久化（生产由 factory 注入）。"""
    service = AlertRelayService(
        robot=FakeRobot(),
        feishu_bot=FakeBot(),
        runner=FakeRunner(),
        receive_id="ou_oncall",
        enabled=True,
        clock=Clock(),
    )

    await service.ingest({"raw_text": ALERT_TEXT})

    assert list(tmp_path.iterdir()) == []
    assert len(service.recent()) == 1


def test_factory_wires_the_default_store_path(tmp_path):
    """生产装配必须自己带上落盘路径，否则这一整套等于没做。"""
    from core.alert_relay.factory import create_alert_relay_service

    service = create_alert_relay_service(
        {"alert_relay": {"enabled": True, "persist_path": str(tmp_path / "relay.json")}}
    )

    assert service._store_path == tmp_path / "relay.json"

    default_service = create_alert_relay_service({"alert_relay": {"enabled": True}})
    assert default_service._store_path is not None
