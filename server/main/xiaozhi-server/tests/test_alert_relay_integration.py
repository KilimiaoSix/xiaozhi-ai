"""端到端串联：SAE 告警 → 机器人 + 飞书 → 人回复 → 真子进程诊断 → 结论回帖。

这条用例刻意不 mock 中间层：真路由、真 handler、真 service、真 FeishuBot（打到
本地假飞书 OpenAPI）、真 ClaudeCodeRunner（起真子进程）。只有两处是替身——
飞书服务端和 claude CLI 本身，因为它们在测试机上不可用。
"""

import json
import sys

import pytest
from aiohttp import web

from core.alert_relay.diagnosis_runner import ClaudeCodeRunner
from core.alert_relay.feishu_bot import FeishuBot
from core.alert_relay.robot import RobotNotifier
from core.alert_relay.service import AlertRelayService
from core.alert_relay_routes import add_alert_relay_routes
from core.api.alert_relay_handler import AlertRelayHandler


SAE_ALERT = """【SAE告警通知】
告警等级：严重
告警集群：bj-jxq-autocar
命名空间：iflyplot
告警对象：iflyplot-ai-7d9f8b6c5d-x2k9p
告警规则：日志包含关键词 无痕改字处理超时 >5条
告警时间：2026-08-18 21:00:11
告警策略链接：https://one.iflytek.com/sae/#/alarm?projectId=117&clusterId=3
"""

# 完全按 diagnose-sae-alert skill 的输出契约构造，字段一个不少。
SKILL_OUTPUT = {
    "title": "限流组并发打满导致改字超时",
    "severity": "严重",
    "time_window": "2026-08-18 21:00:11 到 21:03:42",
    "affected_summary": "3 名用户的无痕改字任务在该窗口内全部超时。",
    "affected": [
        {
            "time": "21:00:11",
            "uid": "u_1001",
            "taskId": "3f2a1b0c-4d5e-6f70-8192-a3b4c5d6e7f8",
            "note": "提交后无回调",
        },
        {
            "time": "21:01:47",
            "uid": "u_1042",
            "taskId": "9a8b7c6d-5e4f-3021-8796-b5a4c3d2e1f0",
            "note": "等待令牌超时",
        },
    ],
    "user_impact": "用户点了无痕改字，一直转圈到超时也没拿到结果。",
    "timeline": ["21:00 用户提交改字任务", "21:01 限流组令牌耗尽", "21:03 超时清扫任务回收"],
    "why": [
        {
            "point": "限流组并发配置为 2",
            "code": "RateLimitConfig.java:42",
            "log": "当前并发 2/2，任务进入等待队列",
        },
        {
            "point": "等待超时 180 秒",
            "code": "InkFreeTaskService.java:188",
            "log": "无痕改字处理超时, taskId=3f2a1b0c",
        },
    ],
    "ruled_out": ["现网 MySQL 同窗口慢查询 0 条 → 排除 DB 侧", "引擎回调正常，非下游故障"],
    "root_cause": "限流组并发上限过低，高峰期任务排队直到等待超时。",
    "suggestion": ["核查限流组并发配置是否与当前流量匹配", "评估高峰期临时扩容"],
}


def write_fake_claude(tmp_path, *, output=None, exit_code=0):
    """一个假 claude CLI：校验提示词，再按契约吐 JSON。"""
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import sys, json\n"
        "prompt = sys.stdin.buffer.read().decode('utf-8')\n"
        "argv = sys.argv[1:]\n"
        # 三条硬约束：点名 skill、带上告警原文、不许绕过权限
        "assert 'diagnose-sae-alert' in prompt\n"
        "assert '无痕改字处理超时' in prompt\n"
        "assert '--dangerously-skip-permissions' not in argv\n"
        "assert '--output-format' in argv and 'json' in argv\n"
        f"exit_code = {exit_code}\n"
        "if exit_code:\n"
        "    sys.stderr.buffer.write('Claude Code 鉴权失败'.encode('utf-8'))\n"
        "    sys.exit(exit_code)\n"
        f"result = json.dumps({output!r}, ensure_ascii=False) if {output!r} is not None else None\n"
        "envelope = {'type': 'result', 'is_error': False, 'result': result}\n"
        "sys.stdout.buffer.write(json.dumps(envelope, ensure_ascii=False).encode('utf-8'))\n",
        encoding="utf-8",
    )
    return script


def fake_feishu_routes(state):
    async def token(request):
        return web.json_response(
            {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
        )

    async def send(request):
        body = await request.json()
        state["sent"].append(body)
        return web.json_response(
            {"code": 0, "data": {"message_id": "om_alert_1", "chat_id": "oc_ops"}}
        )

    async def reply(request):
        body = await request.json()
        state["replies"].append((request.match_info["message_id"], body))
        return web.json_response({"code": 0, "data": {"message_id": "om_reply_1"}})

    async def react(request):
        state["reactions"].append(request.match_info["message_id"])
        return web.json_response({"code": 0, "data": {}})

    return [
        web.post("/open-apis/auth/v3/tenant_access_token/internal", token),
        web.post("/open-apis/im/v1/messages", send),
        web.post("/open-apis/im/v1/messages/{message_id}/reply", reply),
        web.post("/open-apis/im/v1/messages/{message_id}/reactions", react),
    ]


class FakeRegistry:
    def __init__(self, conn):
        self._conn = conn

    def get(self, device_id):
        return self._conn

    def device_ids(self):
        return ["dc:da:0c:26:9a:60"]


async def build_stack(aiohttp_client, tmp_path, *, script=None):
    """装出一整条真链路，只把飞书服务端和 claude CLI 换成替身。"""
    state = {"sent": [], "replies": [], "reactions": [], "pushes": []}

    feishu_app = web.Application()
    feishu_app.add_routes(fake_feishu_routes(state))
    feishu_client = await aiohttp_client(feishu_app)

    bot = FeishuBot(
        base_url=str(feishu_client.make_url("")).rstrip("/"),
        app_id="cli_fake",
        app_secret="secret",
        session=feishu_client.session,
    )

    async def record_push(conn, **kwargs):
        state["pushes"].append(kwargs)
        return bool(kwargs.get("speak"))

    robot = RobotNotifier(
        FakeRegistry(object()), "dc:da:0c:26:9a:60", push=record_push
    )
    runner = ClaudeCodeRunner(
        cli_command=[sys.executable, str(script or write_fake_claude(tmp_path, output=SKILL_OUTPUT))],
        source_dirs=[str(tmp_path)],
        timeout_seconds=60,
        fast_mode=False,
        enforce_preflight=False,
    )
    service = AlertRelayService(
        robot=robot,
        feishu_bot=bot,
        runner=runner,
        receive_id="ou_oncall",
        enabled=True,
    )
    config = {
        "server": {"auth": {"enabled": False}, "auth_key": ""},
        "alert_relay": {"enabled": True, "feishu": {"verification_token": "v-token"}},
    }
    app = web.Application()
    add_alert_relay_routes(app, AlertRelayHandler(config, service))
    return await aiohttp_client(app), service, state


def card_of(body):
    return json.loads(body["content"])


def button_value(card, intent):
    for element in card["elements"]:
        for action in element.get("actions", []):
            value = action.get("value")
            if isinstance(value, dict) and value.get("intent") == intent:
                return value
    raise AssertionError(f"卡片上没有 {intent} 按钮")


@pytest.mark.asyncio
async def test_full_chain_from_sae_alert_to_diagnosis_card(aiohttp_client, tmp_path):
    client, service, state = await build_stack(aiohttp_client, tmp_path)

    # 1) SAE 把告警推进来
    ingest = await client.post("/xiaozhi/alert/ingest", json={"raw_text": SAE_ALERT})
    assert ingest.status == 200
    ingested = (await ingest.json())["data"]
    alert_id = ingested["alert_id"]
    assert ingested["state"] == "AWAITING_REPLY"
    assert ingested["alert"]["workload"] == "iflyplot-ai"
    assert ingested["alert"]["project_id"] == "117"

    # 2) 机器人抬头，飞书卡片到人
    assert state["pushes"][0]["status"] == "线上告警"
    assert state["pushes"][0]["action"] == "look_up"
    alert_card = card_of(state["sent"][0])
    assert "iflyplot-ai" in json.dumps(alert_card, ensure_ascii=False)
    value = button_value(alert_card, "diagnose")
    assert value["alert_id"] == alert_id

    # 3) 人点「帮我查」——按钮回调带的就是卡片里那份 value
    callback = await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={
            "token": "v-token",
            "open_id": "ou_zhangsan",
            "open_message_id": "om_alert_1",
            "action": {"tag": "button", "value": value},
        },
    )
    assert callback.status == 200
    assert (await callback.json())["toast"]["content"] == "收到，我去查"

    # 4) 真子进程跑完诊断
    await service.wait_for_idle()

    detail = await client.get(f"/xiaozhi/alert/{alert_id}")
    record = (await detail.json())["data"]
    assert record["state"] == "DIAGNOSED"
    assert record["claimed_by"] == "ou_zhangsan"
    assert record["diagnosis"]["root_cause"] == SKILL_OUTPUT["root_cause"]

    # 5) 结论回在告警卡片的话题下，完整 taskId 没被截断
    reply_target, reply_body = state["replies"][0]
    assert reply_target == "om_alert_1"
    reply_text = json.dumps(card_of(reply_body), ensure_ascii=False)
    assert "3f2a1b0c-4d5e-6f70-8192-a3b4c5d6e7f8" in reply_text
    assert "RateLimitConfig.java:42" in reply_text
    assert "现网 MySQL 同窗口慢查询 0 条" in reply_text
    assert "只读" in reply_text

    # 6) 机器人走完 抬头 → 点头认领 → 思考 → 点头播报
    assert [push["emotion"] for push in state["pushes"]] == [
        "shocked", "happy", "thinking", "confident",
    ]
    assert state["pushes"][-1]["text"].startswith("查清了")
    assert state["reactions"] == ["om_alert_1"]


@pytest.mark.asyncio
async def test_full_chain_when_the_human_says_they_will_look_themselves(
    aiohttp_client, tmp_path
):
    client, service, state = await build_stack(aiohttp_client, tmp_path)
    ingest = await client.post("/xiaozhi/alert/ingest", json={"raw_text": SAE_ALERT})
    alert_id = (await ingest.json())["data"]["alert_id"]

    await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={
            "token": "v-token",
            "schema": "2.0",
            "header": {"event_type": "card.action.trigger", "token": "v-token"},
            "event": {
                "operator": {"open_id": "ou_zhangsan"},
                "action": {"value": {"alert_id": alert_id, "intent": "decline"}},
                "context": {"open_message_id": "om_alert_1"},
            },
        },
    )
    await service.wait_for_idle()

    record = (await (await client.get(f"/xiaozhi/alert/{alert_id}")).json())["data"]
    assert record["state"] == "DECLINED"
    # 没开子进程，也就没有结论卡片
    assert state["replies"][-1][1]["msg_type"] == "text"
    assert [push["emotion"] for push in state["pushes"]] == ["shocked", "neutral"]


@pytest.mark.asyncio
async def test_full_chain_reply_as_plain_text_in_the_thread(aiohttp_client, tmp_path):
    """人更可能直接在话题里回一句话，而不是点按钮。"""
    client, service, state = await build_stack(aiohttp_client, tmp_path)
    ingest = await client.post("/xiaozhi/alert/ingest", json={"raw_text": SAE_ALERT})
    alert_id = (await ingest.json())["data"]["alert_id"]

    await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1", "token": "v-token"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_zhangsan"}},
                "message": {
                    "message_id": "om_human",
                    "root_id": "om_alert_1",
                    "chat_id": "oc_ops",
                    "message_type": "text",
                    "content": json.dumps({"text": "@_user_1 帮我查一下"}, ensure_ascii=False),
                },
            },
        },
    )
    await service.wait_for_idle()

    record = (await (await client.get(f"/xiaozhi/alert/{alert_id}")).json())["data"]
    assert record["state"] == "DIAGNOSED"
    assert record["reply_text"] == "帮我查一下"
    assert state["reactions"] == ["om_human"]


@pytest.mark.asyncio
async def test_full_chain_reports_a_failed_diagnosis_without_inventing_one(
    aiohttp_client, tmp_path
):
    script = write_fake_claude(tmp_path, exit_code=1)
    client, service, state = await build_stack(aiohttp_client, tmp_path, script=script)
    ingest = await client.post("/xiaozhi/alert/ingest", json={"raw_text": SAE_ALERT})
    alert_id = (await ingest.json())["data"]["alert_id"]

    await client.post(
        "/xiaozhi/alert/feishu/callback",
        json={
            "token": "v-token",
            "open_message_id": "om_alert_1",
            "open_id": "ou_zhangsan",
            "action": {"value": {"alert_id": alert_id, "intent": "diagnose"}},
        },
    )
    await service.wait_for_idle()

    record = (await (await client.get(f"/xiaozhi/alert/{alert_id}")).json())["data"]
    assert record["state"] == "FAILED"
    assert record["diagnosis"] is None
    failure_card = card_of(state["replies"][-1][1])
    dumped = json.dumps(failure_card, ensure_ascii=False)
    assert failure_card["header"]["template"] == "red"
    assert "退出码 1" in dumped
    assert "鉴权失败" in dumped
    assert state["pushes"][-1]["emotion"] == "sad"


@pytest.mark.asyncio
async def test_alert_storm_only_pokes_the_robot_once(aiohttp_client, tmp_path):
    client, _, state = await build_stack(aiohttp_client, tmp_path)
    # 每次重启换一个真实形状的 pod 后缀（k8s 是 5 位随机串），workload 不变
    for suffix in ("q7w8e", "z1x2c", "m3n4b", "k5l6j", "h7g8f"):
        await client.post(
            "/xiaozhi/alert/ingest",
            json={"raw_text": SAE_ALERT.replace("x2k9p", suffix)},
        )
    assert len(state["pushes"]) == 1
    assert len(state["sent"]) == 1


@pytest.mark.asyncio
async def test_health_endpoint_reflects_the_assembled_stack(aiohttp_client, tmp_path):
    client, _, _ = await build_stack(aiohttp_client, tmp_path)
    health = (await (await client.get("/xiaozhi/alert/health")).json())["data"]
    assert health["enabled"] is True
    assert health["receive_id_configured"] is True
    assert health["robot"]["device_online"] is True
    assert health["feishu"]["app_id_configured"] is True
    assert health["diagnosis"]["skill"] == "diagnose-sae-alert"
