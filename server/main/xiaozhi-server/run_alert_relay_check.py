"""告警值班中继的本机联调脚本。

两种模式：

- `--simulate`（默认）：起一个假飞书 OpenAPI 和一个假机器人，**但用真的 Claude Code**
  跑诊断，把「告警 → 叫人 → 人回复 → 排查 → 回帖」整条链路走一遍并打印每一步。
  不需要真飞书应用、不需要真机器人，适合改完代码先自检。
- `--live`：用 config.yaml + data/.config.yaml 的真实配置装配，只打 health 和一次
  ingest，用来确认线上环境（飞书机器人、device_id、CLI 路径）是不是真的通。

两种模式都只读：不改 SAE、不改任何线上对象。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from aiohttp import web

from config.config_loader import read_config, get_project_dir
from core.alert_relay.diagnosis_runner import ClaudeCodeRunner
from core.alert_relay.factory import create_alert_relay_service
from core.alert_relay.feishu_bot import FeishuBot
from core.alert_relay.robot import RobotNotifier
from core.alert_relay.service import AlertRelayService
from core.alert_relay_routes import add_alert_relay_routes
from core.api.alert_relay_handler import AlertRelayHandler


SAMPLE_ALERT = """【SAE告警通知】
告警等级：严重
告警集群：bj-jxq-autocar
命名空间：iflyplot
告警对象：iflyplot-ai-7d9f8b6c5d-x2k9p
告警规则：日志包含关键词 无痕改字处理超时 >5条
告警时间：2026-08-18 21:00:11
告警策略链接：https://one.iflytek.com/sae/#/alarm?projectId=117&clusterId=3
"""

CANNED_DIAGNOSIS = {
    "title": "限流组并发打满导致改字超时",
    "severity": "严重",
    "time_window": "2026-08-18 21:00:11 到 21:03:42",
    "affected_summary": "3 名用户的无痕改字任务在该窗口内全部超时。",
    "affected": [
        {"time": "21:00:11", "uid": "u_1001",
         "taskId": "3f2a1b0c-4d5e-6f70-8192-a3b4c5d6e7f8", "note": "提交后无回调"}
    ],
    "user_impact": "用户点了无痕改字，一直转圈到超时也没结果。",
    "timeline": ["21:00 提交任务", "21:01 令牌耗尽", "21:03 超时清扫"],
    "why": [{"point": "限流组并发为 2", "code": "RateLimitConfig.java:42",
             "log": "当前并发 2/2，进入等待"}],
    "ruled_out": ["现网 MySQL 同窗口慢查询 0 条 → 排除 DB 侧"],
    "root_cause": "限流组并发上限过低，高峰期任务排队直到等待超时。",
    "suggestion": ["核查限流组并发配置", "评估高峰期临时扩容"],
}


def load_config() -> dict:
    config = read_config(get_project_dir() + "config.yaml")
    private_path = get_project_dir() + "data/.config.yaml"
    if os.path.exists(private_path):
        private = read_config(private_path) or {}
        for key, value in private.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
    return config


def step(index: int, title: str) -> None:
    print(f"\n=== {index}. {title} " + "=" * max(0, 46 - len(title)))


# ----------------------------------------------------------------- 假飞书

def fake_feishu_app(state: dict) -> web.Application:
    async def token(request):
        return web.json_response({"code": 0, "tenant_access_token": "t-fake", "expire": 7200})

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

    app = web.Application()
    app.add_routes(
        [
            web.post("/open-apis/auth/v3/tenant_access_token/internal", token),
            web.post("/open-apis/im/v1/messages", send),
            web.post("/open-apis/im/v1/messages/{message_id}/reply", reply),
            web.post("/open-apis/im/v1/messages/{message_id}/reactions", react),
        ]
    )
    return app


class FakeRegistry:
    def get(self, device_id):
        return object()

    def device_ids(self):
        return ["dc:da:0c:26:9a:60"]


def canned_cli(tmp_dir: str) -> list[str]:
    """一个不烧 token 的假 CLI，用于只想验管道的时候。"""
    path = os.path.join(tmp_dir, "canned_claude.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "import sys, json\n"
            "sys.stdin.buffer.read()\n"
            f"result = json.dumps({CANNED_DIAGNOSIS!r}, ensure_ascii=False)\n"
            "sys.stdout.buffer.write(json.dumps("
            "{'type':'result','is_error':False,'result':result}, ensure_ascii=False)"
            ".encode('utf-8'))\n"
        )
    return [sys.executable, path]


async def simulate(args) -> int:
    state = {"sent": [], "replies": [], "reactions": [], "pushes": []}

    feishu_runner = web.AppRunner(fake_feishu_app(state))
    await feishu_runner.setup()
    feishu_site = web.TCPSite(feishu_runner, "127.0.0.1", 0)
    await feishu_site.start()
    feishu_port = feishu_site._server.sockets[0].getsockname()[1]

    async def record_push(conn, **kwargs):
        state["pushes"].append(kwargs)
        print(
            f"    🤖 机器人 | {kwargs['status']} | {kwargs['emotion']}"
            f" | 动作={kwargs.get('action')} | 播报={kwargs['speak']} | {kwargs['text']}"
        )
        return bool(kwargs.get("speak"))

    cli_command = ["claude"] if args.real_cli else canned_cli(args.tmp_dir)
    print(f"诊断 CLI: {' '.join(cli_command)}" + ("（真 Claude Code）" if args.real_cli else "（假 CLI）"))

    service = AlertRelayService(
        robot=RobotNotifier(FakeRegistry(), "dc:da:0c:26:9a:60", push=record_push),
        feishu_bot=FeishuBot(
            base_url=f"http://127.0.0.1:{feishu_port}",
            app_id="cli_fake",
            app_secret="fake",
        ),
        runner=ClaudeCodeRunner(
            cli_command=cli_command,
            source_dirs=[path for path in args.source_dir or []],
            timeout_seconds=float(args.timeout),
        ),
        receive_id="ou_oncall",
        enabled=True,
    )
    config = {
        "server": {"auth": {"enabled": False}, "auth_key": ""},
        "alert_relay": {"enabled": True, "feishu": {"verification_token": ""}},
    }
    app = web.Application()
    add_alert_relay_routes(app, AlertRelayHandler(config, service))
    relay_runner = web.AppRunner(app)
    await relay_runner.setup()
    relay_site = web.TCPSite(relay_runner, "127.0.0.1", 0)
    await relay_site.start()
    port = relay_site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    import aiohttp

    alert_text = SAMPLE_ALERT
    if args.alert_file:
        with open(args.alert_file, encoding="utf-8") as handle:
            alert_text = handle.read()

    exit_code = 0
    async with aiohttp.ClientSession() as session:
        step(1, "SAE 推送告警 → POST /xiaozhi/alert/ingest")
        async with session.post(f"{base}/xiaozhi/alert/ingest",
                                json={"raw_text": alert_text}) as response:
            payload = await response.json()
        record = payload["data"]
        alert_id = record["alert_id"]
        print(f"    HTTP {response.status} | alert_id={alert_id} | 状态={record['state']}")
        print(f"    解析：workload={record['alert']['workload']} "
              f"关键词={record['alert']['keyword']} "
              f"projectId={record['alert']['project_id']}")

        step(2, "飞书卡片已送达值班人")
        card = json.loads(state["sent"][0]["content"])
        print(f"    标题：{card['header']['title']['content']}")
        buttons = [action["text"]["content"]
                   for element in card["elements"]
                   for action in element.get("actions", [])]
        print(f"    按钮：{buttons}")

        step(3, "人点「帮我查」→ POST /xiaozhi/alert/feishu/callback")
        async with session.post(
            f"{base}/xiaozhi/alert/feishu/callback",
            json={
                "open_id": "ou_zhangsan",
                "open_message_id": "om_alert_1",
                "action": {"tag": "button",
                           "value": {"alert_id": alert_id, "intent": "diagnose"}},
            },
        ) as response:
            print(f"    HTTP {response.status} | toast={(await response.json())['toast']}")

        step(4, f"调起本机 Claude Code 排查（最长 {args.timeout}s，只读）")
        await service.wait_for_idle()
        detail = service.get(alert_id)
        print(f"    状态={detail['state']}")
        if detail["state"] != "DIAGNOSED":
            print(f"    失败原因：{detail['error']}")
            exit_code = 1

        step(5, "结论回帖到告警卡片的同一话题")
        if state["replies"]:
            target, body = state["replies"][-1]
            replied = json.loads(body["content"]) if body["msg_type"] == "interactive" else body
            print(f"    回在 {target}")
            print(json.dumps(replied, ensure_ascii=False, indent=2)[:1800])
        else:
            print("    （没有回帖）")
            exit_code = 1

        step(6, "机器人动作序列")
        print(f"    {[push['emotion'] for push in state['pushes']]}")
        print(f"    表情回执：{state['reactions']}")

    await relay_runner.cleanup()
    await feishu_runner.cleanup()
    print("\n结果：" + ("全链路打通 ✅" if exit_code == 0 else "链路未跑通 ❌"))
    return exit_code


async def live(args) -> int:
    config = load_config()
    service = create_alert_relay_service(config)
    step(1, "health（真实配置）")
    print(json.dumps(service.health(), ensure_ascii=False, indent=2))
    if not config.get("alert_relay", {}).get("enabled"):
        print("\nalert_relay.enabled 为 false，未发起真实告警。")
        return 0
    step(2, "ingest（会真的发飞书卡片、真的推机器人）")
    result = await service.ingest({"raw_text": SAMPLE_ALERT})
    print(json.dumps(result, ensure_ascii=False, indent=2)[:2000])
    return 0 if result.get("code") == "OK" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="告警值班中继联调")
    parser.add_argument("--live", action="store_true", help="用真实配置跑，而不是模拟")
    parser.add_argument("--real-cli", action="store_true",
                        help="模拟模式下用真的 Claude Code（会烧 token 并访问 SAE，只读）")
    parser.add_argument("--alert-file", help="从文件读告警原文")
    parser.add_argument("--source-dir", action="append",
                        help="被诊断服务的源码目录，可多次指定")
    parser.add_argument("--timeout", type=float, default=600, help="诊断超时秒数")
    parser.add_argument("--tmp-dir", default="tmp", help="假 CLI 脚本落盘目录")
    args = parser.parse_args()
    os.makedirs(args.tmp_dir, exist_ok=True)
    return asyncio.run(live(args) if args.live else simulate(args))


if __name__ == "__main__":
    raise SystemExit(main())
