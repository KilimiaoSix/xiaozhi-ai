#!/usr/bin/env python3
"""只读拉取 i讯飞 SAE 生产日志（网关 https://one.iflytek.com/sae/apis）。

**为什么仓库里要有这个脚本**：告警值班中继把诊断外包给本机 Claude Code，
而原始的 sae-log-manager skill 只装在个人的 `~/.claude/skills/` 下，且入口是
Windows 专用的 `sae.ps1`。别人克隆本仓库时那两样都没有，诊断必然跑不起来。
这个脚本把「读凭证 + 绕代理 + 发 GET」三件事收进一个纯标准库文件，
macOS / Linux / Windows 通用。

红线：**只发 GET**，不改 SAE 里的任何东西。

凭证（按优先级，都不落盘到仓库）：
  1. 环境变量 SAE_AUTHORIZATION（形如 `Bearer <jwt>`，长期 token，首选）
  2. ~/.sae/sae-token.env 里的 SAE_AUTHORIZATION=
  3. 环境变量 SAE_COOKIE / ~/.sae/auth.env 里的 SAE_COOKIE=（浏览器 cookie，约 1 小时过期）

生产网关的参数 schema 与预发不同，容易踩：
  logKeywords = [{"logViewOp":"|=","keyword":"..."}]   对象数组，不是 ["|= x"]
  labels      = {"fields_namespace":"iflyplot"}        字符串值，不是数组
传成预发那种形状会收到 HTTP 400 "cannot unmarshal array into string"。

用法：
  python sae_logs.py --project-id 117 --cluster-id 3 \
      --start "2026-08-18 21:00:00" --end "2026-08-18 21:06:00" \
      --keyword "无痕改字处理超时" \
      --label fields_namespace=iflyplot --label fields_workload_name=iflyplot-ai
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://one.iflytek.com/sae/apis"
SHANGHAI = dt.timezone(dt.timedelta(hours=8))
# 接口会用 <span class="hight-color"> 把命中的关键词高亮起来，读之前先剥掉。
SPAN_RE = re.compile(r"</?span[^>]*>")

TOKEN_FILE = Path.home() / ".sae" / "sae-token.env"
COOKIE_FILE = Path.home() / ".sae" / "auth.env"
CREDENTIAL_KEYS = ("SAE_AUTHORIZATION", "SAE_COOKIE", "SAE_BASE_URL")


def load_env_file(path: Path) -> dict[str, str]:
    """读 KEY=VALUE 文件；值里可以有 =，注释和空行跳过。"""
    values: dict[str, str] = {}
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in CREDENTIAL_KEYS:
            values[key] = value.strip()
    return values


def resolve_credentials() -> dict[str, str]:
    """环境变量优先，其次 ~/.sae 下的私有文件。"""
    resolved = {
        key: os.environ[key] for key in CREDENTIAL_KEYS if os.environ.get(key)
    }
    if not resolved.get("SAE_AUTHORIZATION"):
        resolved.update(
            {k: v for k, v in load_env_file(TOKEN_FILE).items() if v and k not in resolved}
        )
    if not resolved.get("SAE_AUTHORIZATION") and not resolved.get("SAE_COOKIE"):
        resolved.update(
            {k: v for k, v in load_env_file(COOKIE_FILE).items() if v and k not in resolved}
        )
    # 长期 token 在就别再带 cookie：过期 cookie 会把请求带歪，反而拿 403。
    if resolved.get("SAE_AUTHORIZATION"):
        resolved.pop("SAE_COOKIE", None)
    return resolved


def parse_time(value: str | None) -> int | None:
    """ISO / 'YYYY-MM-DD HH:MM:SS' / 秒 / 毫秒 / 纳秒 → SAE 的纳秒时间戳。"""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        number = int(value)
        if len(value) >= 18:
            return number
        if len(value) >= 13:
            return number * 1_000_000
        return number * 1_000_000_000
    normalized = value.replace("Z", "+00:00").replace("/", "-")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        # 告警时间一律是东八区，别让运行机器的本地时区把窗口挪走。
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return int(parsed.timestamp() * 1000) * 1_000_000


def build_opener() -> urllib.request.OpenerDirector:
    """内网网关（one.iflytek.com → 10.x）走公司代理必超时，这里直接禁用代理。

    只清环境变量不够：urllib 在别处也可能拿到代理配置（macOS 会读系统设置），
    显式传一个空的 ProxyHandler 才是确定的。
    """
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "one.iflytek.com,iflytek.com,10.0.0.0/8,localhost,127.0.0.1"
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request_text(url: str, credentials: dict[str, str], timeout: float) -> str:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "launchcrush-alert-relay/1.0",
    }
    if credentials.get("SAE_AUTHORIZATION"):
        headers["Authorization"] = credentials["SAE_AUTHORIZATION"]
    if credentials.get("SAE_COOKIE"):
        headers["Cookie"] = credentials["SAE_COOKIE"]

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with build_opener().open(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code in (401, 403):
            raise SystemExit(
                f"HTTP {exc.code}: SAE 凭证无效或已过期。"
                f"请更新 SAE_AUTHORIZATION（或 {TOKEN_FILE}）。详情: {detail[:300]}"
            ) from exc
        raise SystemExit(f"HTTP {exc.code}: {detail[:800]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"请求失败: {exc}。内网网关只能直连，确认没有被代理拦截、且当前网络能到 "
            f"one.iflytek.com。"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="只读拉取 i讯飞 SAE 生产日志")
    parser.add_argument("--base-url", default=os.environ.get("SAE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument("--start", help="ISO / 'YYYY-MM-DD HH:MM:SS' / epoch")
    parser.add_argument("--end", help="同 --start；不给就用当前时间")
    parser.add_argument("--minutes", type=int, default=10, help="相对窗口长度，配合 --end 使用")
    parser.add_argument("--keyword", action="append", default=[], help="服务端过滤关键词，可重复")
    parser.add_argument("--op", default="|=", choices=["|=", "!=", "|~", "!~"],
                        help="|= 包含 / != 不含 / |~ 正则 / !~ 正则取反")
    parser.add_argument("--label", action="append", default=[],
                        help="标签过滤 key=value，可重复，如 fields_workload_name=iflyplot-ai")
    parser.add_argument("--mode", choices=["query", "download"], default="query",
                        help="query 服务端过滤、最多 1000 行；download 整段原文、无行数上限")
    parser.add_argument("--out", help="download 模式写入的文件，不给就打到 stdout")
    parser.add_argument("--reverse-order", default="true", choices=["true", "false"])
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--keep-highlight", action="store_true", help="保留命中词的 <span> 高亮标签")
    parser.add_argument("--check-credentials", action="store_true",
                        help="只检查凭证是否就绪，不发任何请求")
    args = parser.parse_args()

    credentials = resolve_credentials()
    if not credentials.get("SAE_AUTHORIZATION") and not credentials.get("SAE_COOKIE"):
        print(
            "找不到 SAE 凭证。请任选其一：\n"
            "  1) 设环境变量 SAE_AUTHORIZATION='Bearer <jwt>'（长期 token，推荐）\n"
            f"  2) 写入 {TOKEN_FILE}，内容一行：SAE_AUTHORIZATION=Bearer <jwt>\n"
            f"  3) 写入 {COOKIE_FILE}，内容一行：SAE_COOKIE=<浏览器 cookie>（约 1 小时过期）",
            file=sys.stderr,
        )
        return 2
    if args.check_credentials:
        source = "SAE_AUTHORIZATION" if credentials.get("SAE_AUTHORIZATION") else "SAE_COOKIE"
        print(f"SAE 凭证就绪（{source}），网关 {args.base_url}")
        return 0

    end_ns = parse_time(args.end) or int(time.time() * 1000) * 1_000_000
    start_ns = parse_time(args.start) or end_ns - args.minutes * 60 * 1000 * 1_000_000

    labels: dict[str, str] = {}
    for item in args.label:
        if "=" not in item:
            raise SystemExit(f"--label 必须是 key=value，收到: {item}")
        key, _, value = item.partition("=")
        labels[key] = value

    base = args.base_url.rstrip("/")
    prefix = (
        f"{base}/logging.sae.iflytek.com/v1/projects/{args.project_id}"
        f"/clusters/{args.cluster_id}"
    )
    path = "persistentlogs/download" if args.mode == "download" else "persistentlogs"
    params = {
        "projectId": args.project_id,
        "clusterId": args.cluster_id,
        "startTimestamp": str(start_ns),
        "endTimestamp": str(end_ns),
        "reverseOrder": args.reverse_order,
        # 生产网关：对象数组 + 字符串值，传错形状会 400
        "logKeywords": json.dumps(
            [{"logViewOp": args.op, "keyword": word} for word in args.keyword],
            ensure_ascii=False,
        ),
        "labels": json.dumps(labels, ensure_ascii=False),
    }
    url = f"{prefix}/{path}?{urllib.parse.urlencode(params)}"
    body = request_text(url, credentials, args.timeout)

    if args.mode == "download":
        if not args.keep_highlight:
            body = SPAN_RE.sub("", body)
        if args.out:
            Path(args.out).write_text(body, encoding="utf-8")
            lines = body.count("\n") + (1 if body and not body.endswith("\n") else 0)
            print(f"saved {len(body)} bytes, ~{lines} lines -> {args.out}")
        else:
            sys.stdout.write(body)
        return 0

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        sys.stdout.write(body)
        return 0
    data = payload.get("data") if isinstance(payload, dict) else None
    if data is None:
        # 没命中时接口回的是 {"code":200,"data":null}。把这坨 JSON 原样吐出去，
        # 读的人会当成"拿到日志了"，实际是空——必须翻译成人话。
        # 最常见的原因是 label 值写错（如 fields_namespace 填了个不存在的值），
        # 而不是真的没日志。
        print(
            "（没有命中任何日志行。先只用 --label fields_workload_name=<workload> 试一次："
            "label 值写错时接口一样返回成功，只是 data 为 null。）",
            file=sys.stderr,
        )
        return 0
    if not isinstance(data, list):
        sys.stdout.write(body)
        return 0

    lines = []
    for entry in data:
        line = entry.get("body") if isinstance(entry, dict) else None
        if not line:
            continue
        lines.append(line if args.keep_highlight else SPAN_RE.sub("", line))
    if not lines:
        print("（该时间窗内没有命中任何日志行）", file=sys.stderr)
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
