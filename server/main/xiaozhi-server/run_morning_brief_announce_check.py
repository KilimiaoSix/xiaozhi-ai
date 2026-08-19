"""真机联调：模拟「主人第一次坐下」，看机器人会不会把今日晨报念出来。

对着**已经在跑的** Server 打真实的在岗上报，走的就是 presence-agent / 桌面端
平时走的那条路径，因此机器人真会开口。脚本自己不起服务、不改配置、不碰飞书凭据。

    python run_morning_brief_announce_check.py
    python run_morning_brief_announce_check.py --host 192.168.1.20 --device dc:da:0c:26:9a:60

依次检查：

    0. 配置：晨报总开关、播报开关、当前时间在不在播报窗口、工位与设备绑定
    1. 设备：/xiaozhi/event/devices 里有没有在线的机器人
    2. 触发：POST 一条 present + owner 的在岗上报（首次到岗）
    3. 结果：轮询 /xiaozhi/morning-brief/latest，等这次扫描产出的新报告
    4. 复核：把机器人应该念的那句话打印出来，和真机听到的对一遍

第 0 步不通过时直接告诉你改哪一项，不会让你对着不会响的机器人干等。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
import sys
import uuid

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.config_loader import get_project_dir, read_config
from core.morning_brief.announcement import build_announcement
from core.morning_brief.announcer import AnnouncePolicy


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8003
POLL_SECONDS = 2
POLL_ATTEMPTS = 30


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


def preflight(config: dict, policy: AnnouncePolicy, workstation: str) -> list[str]:
    """返回阻塞项列表；空列表表示这一步没问题。"""
    problems: list[str] = []
    brief = config.get("morning_brief") or {}
    if not brief.get("enabled"):
        problems.append(
            "morning_brief.enabled 是 false —— 在 data/.config.yaml 里打开"
        )
    if not policy.enabled:
        problems.append(
            "morning_brief.announce.enabled 是 false —— 打开它才会主动播报"
        )
    token = os.environ.get("FEISHU_USER_ACCESS_TOKEN") or brief.get(
        "user_access_token"
    )
    if not token:
        problems.append(
            "没有飞书用户令牌 —— 写进 data/.env 的 FEISHU_USER_ACCESS_TOKEN，"
            "否则扫描会直接失败"
        )
    if not (
        os.environ.get("FEISHU_SELF_OPEN_ID") or brief.get("self_open_id")
    ):
        problems.append("没有 FEISHU_SELF_OPEN_ID —— 同上，写进 data/.env")

    now = datetime.now(timezone.utc).astimezone(policy.timezone)
    if now.isoweekday() not in policy.workdays:
        problems.append(
            f"今天是星期{now.isoweekday()}，不在 announce.workdays={sorted(policy.workdays)} 里"
            " —— 临时把今天加进去再重启"
        )
    current = now.time().replace(tzinfo=None)
    if not policy.window_start <= current < policy.window_end:
        problems.append(
            f"现在 {current.strftime('%H:%M')} 不在播报窗口 "
            f"{policy.window_start.strftime('%H:%M')}-{policy.window_end.strftime('%H:%M')}"
            " —— 把 announce.window_start/window_end 临时改成覆盖当前时间再重启服务"
        )
    if workstation not in policy.bindings:
        problems.append(
            f"工位 {workstation} 不在 announce.bindings={list(policy.bindings)} 里"
        )
    return problems


def presence_payload(workstation: str, agent_instance_id: str, sequence: int) -> dict:
    return {
        "schema_version": "1.0",
        "event_id": str(uuid.uuid4()),
        "agent_instance_id": agent_instance_id,
        "workstation_id": workstation,
        "source": "camera_pose",
        "state": "present",
        "previous_state": "starting" if sequence == 1 else "present",
        "changed": sequence == 1,
        "reason": "pose_confirmed" if sequence == 1 else "heartbeat",
        "sequence": sequence,
        "observed_at": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "metrics": {"visible_core_landmarks": 5, "has_visible_shoulder": True},
        "identity": {
            "state": "owner",
            "previous_state": "starting" if sequence == 1 else "owner",
            "changed": sequence == 1,
            "face_count": 1,
            "similarity": 0.72,
            "horizontal_position": "center",
        },
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workstation", default="desktop-local")
    parser.add_argument(
        "--device",
        default="",
        help="期望播报的机器人 device_id；留空则只检查有设备在线",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="配置检查只警告不退出",
    )
    args = parser.parse_args()

    config = load_config()
    policy = AnnouncePolicy.from_config(config)
    base = f"http://{args.host}:{args.port}/xiaozhi"
    auth = str((config.get("server") or {}).get("auth_key") or "")
    headers = {"Authorization": f"Bearer {auth}"} if auth else {}

    print("[0/4] 配置检查")
    print(
        f"      窗口 {policy.window_start.strftime('%H:%M')}-"
        f"{policy.window_end.strftime('%H:%M')}"
        f" 星期 {sorted(policy.workdays)} 绑定 {policy.bindings}"
    )
    problems = preflight(config, policy, args.workstation)
    for problem in problems:
        print(f"      ✗ {problem}")
    if problems and not args.skip_preflight:
        print("      —— 上面这些不解决，机器人不会响。改完重启服务再跑。")
        return 1
    if not problems:
        print("      ✓ 配置允许现在播报")

    async with aiohttp.ClientSession(headers=headers) as session:
        print("[1/4] 查在线设备")
        try:
            async with session.get(f"{base}/event/devices") as response:
                devices = await response.json(content_type=None)
        except aiohttp.ClientError as error:
            print(f"      ✗ 连不上 Server：{error}")
            print(f"      Server 起了吗？./server.command status")
            return 1
        online = (devices or {}).get("devices")
        if online is None:
            online = (devices or {}).get("data") or []
        print(f"      在线设备：{json.dumps(online, ensure_ascii=False)}")
        if not online:
            print("      ✗ 没有机器人在线，播报会被跳过（设计如此，不会白扫飞书）")
            return 1
        if args.device and args.device not in json.dumps(online):
            print(f"      ✗ 期望的设备 {args.device} 不在线")
            return 1

        print("[2/4] 读取当前最近一次晨报，用于识别新报告")
        before = None
        async with session.get(f"{base}/morning-brief/latest") as response:
            if response.status == 200:
                payload = await response.json(content_type=None)
                before = ((payload or {}).get("data") or {}).get("generated_at")
        print(f"      当前最新报告 generated_at={before}")

        print("[3/4] 打一条 present + owner 的在岗上报（模拟主人坐下）")
        agent_instance_id = str(uuid.uuid4())
        async with session.post(
            f"{base}/presence/report",
            json=presence_payload(args.workstation, agent_instance_id, 1),
        ) as response:
            body = await response.json(content_type=None)
            print(f"      HTTP {response.status} {json.dumps(body, ensure_ascii=False)}")
            if response.status != 200:
                return 1

        print("[4/4] 等这次扫描产出的新报告（最长 %d 秒）" % (POLL_SECONDS * POLL_ATTEMPTS))
        report = None
        for _ in range(POLL_ATTEMPTS):
            await asyncio.sleep(POLL_SECONDS)
            async with session.get(f"{base}/morning-brief/latest") as response:
                if response.status != 200:
                    continue
                payload = await response.json(content_type=None)
                candidate = (payload or {}).get("data") or {}
                if candidate.get("generated_at") and candidate["generated_at"] != before:
                    report = candidate
                    break
        if report is None:
            print("      ✗ 一直没有新报告。看服务端日志里的这几行：")
            print("        「晨报稍后再播」= 设备正忙，等迎接语说完")
            print("        「机器人不在线」= device_id 没绑上")
            print("        「晨报扫描失败」= 飞书那一路的问题，日志里有栈")
            return 1

        announcement = build_announcement(
            report,
            max_items=policy.max_items,
            item_chars=policy.item_chars,
            greeting=policy.greeting,
            status=policy.status,
            emotion=policy.emotion,
        )
        print(f"      ✓ 新报告 generated_at={report['generated_at']}"
              f" 覆盖={report.get('coverage_status')}")
        print("      机器人应该念出来的是：")
        print(f"      「{announcement.text}」")
        print(f"      屏幕状态栏「{announcement.status}」表情 {announcement.emotion}")
        print("      —— 真机听到的和这句一致就算通过。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
