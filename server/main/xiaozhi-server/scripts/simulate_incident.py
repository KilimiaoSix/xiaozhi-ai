#!/usr/bin/env python3
"""演示用的告警模拟脚本（需求文档流程七，分镜 7-1 / 7-5）。

往服务端的 webhook 打一条**标记为模拟**的告警，或者打一条恢复信号。
simulated 恒为 true：机器人播报时会带「模拟」前缀，现场不会有人把它当成真故障。

    # 7-1 触发告警（P1，立即播报）
    python scripts/simulate_incident.py --severity P1 --title "接口错误率升高" \
        --message "支付回调 5 分钟内错误率 12%"

    # 7-5 指标恢复（进入观察窗，安静走完才播报恢复）
    python scripts/simulate_incident.py --resolve --title "接口错误率升高"

不传 --incident-id 时，服务端按 service+title 的稳定哈希归并，所以恢复那条
必须用**同样的 --service 与 --title**，否则会被当成另一个故障而找不到。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_SERVER = "http://127.0.0.1:8003"
DEFAULT_SERVICE = "demo-api"
DEFAULT_TITLE = "接口错误率升高"
DEFAULT_MESSAGE = "支付回调接口 5 分钟内错误率 12%，超过 5% 阈值"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="给小智桌伴机器人发一条模拟线上告警（或恢复信号）"
    )
    parser.add_argument("--server", default=DEFAULT_SERVER, help="服务端地址")
    parser.add_argument(
        "--severity",
        default="P1",
        choices=["P0", "P1", "P2", "P3"],
        help="严重度，P0/P1 会立即打断播报，P2/P3 只进摘要",
    )
    parser.add_argument("--title", default=DEFAULT_TITLE, help="告警标题")
    parser.add_argument("--message", default=DEFAULT_MESSAGE, help="告警详情")
    parser.add_argument("--service", default=DEFAULT_SERVICE, help="出问题的服务名")
    parser.add_argument("--metric", default="error_rate", help="指标名")
    parser.add_argument("--value", default="12%", help="指标当前值")
    parser.add_argument(
        "--incident-id",
        default="",
        help="故障 ID，不传则由服务端按 service+title 归并",
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="发恢复信号（status=resolved）而不是新告警",
    )
    parser.add_argument("--auth-key", default="", help="服务端开了鉴权时的 Bearer token")
    return parser


def build_payload(args: argparse.Namespace) -> dict:
    payload = {
        "service": args.service,
        "severity": args.severity,
        "title": args.title,
        "message": args.message,
        "metric": args.metric,
        "value": args.value,
        "source": "simulate_incident.py",
        # 演示脚本发出的一切都必须能被一耳朵听出是模拟
        "simulated": True,
        "status": "resolved" if args.resolve else "firing",
    }
    if args.incident_id:
        payload["incident_id"] = args.incident_id
    return payload


def post(url: str, payload: dict, auth_key: str = "") -> tuple:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    if auth_key:
        request.add_header("Authorization", f"Bearer {auth_key}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # 400 的 body 里有校验失败的原因，比状态码本身有用得多
        return e.code, e.read().decode("utf-8", errors="replace")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    url = args.server.rstrip("/") + "/xiaozhi/incident/webhook"
    payload = build_payload(args)

    print(f"POST {url}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    try:
        status, text = post(url, payload, args.auth_key)
    except Exception as e:
        print(f"请求失败：{e}", file=sys.stderr)
        return 1

    print(f"HTTP {status}")
    try:
        print(json.dumps(json.loads(text), ensure_ascii=False, indent=2))
    except ValueError:
        print(text)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
