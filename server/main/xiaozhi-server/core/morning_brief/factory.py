"""从服务配置创建每日关注晨报依赖。"""

from __future__ import annotations

import os
from pathlib import Path
import re

from core.morning_brief.ledger import AttentionLedger
from core.morning_brief.service import MorningBriefService
from core.morning_brief.feishu_client import DEFAULT_BASE_URL, FeishuClient


SERVER_ROOT = Path(__file__).resolve().parents[2]
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _read_local_env(path: Path) -> dict[str, str]:
    """读取简单 KEY=VALUE 文件，不执行 shell，也不覆盖进程环境变量。"""
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not ENV_KEY_PATTERN.fullmatch(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def create_morning_brief_service(config: dict) -> MorningBriefService:
    brief_config = config.get("morning_brief", {})
    local_env = _read_local_env(SERVER_ROOT / "data/.env")
    token = os.environ.get("FEISHU_USER_ACCESS_TOKEN") or local_env.get(
        "FEISHU_USER_ACCESS_TOKEN"
    ) or str(
        brief_config.get("user_access_token") or ""
    )
    self_open_id = os.environ.get("FEISHU_SELF_OPEN_ID") or local_env.get(
        "FEISHU_SELF_OPEN_ID"
    ) or str(
        brief_config.get("self_open_id") or ""
    )
    ledger_path = Path(
        str(brief_config.get("ledger_path", "data/morning_brief.sqlite3"))
    )
    if not ledger_path.is_absolute():
        ledger_path = SERVER_ROOT / ledger_path

    client = FeishuClient(
        base_url=str(brief_config.get("base_url", DEFAULT_BASE_URL)),
        user_access_token=token,
        page_size=int(brief_config.get("page_size", 50)),
        max_pages=int(brief_config.get("max_pages", 40)),
        timeout_seconds=float(brief_config.get("timeout_seconds", 15)),
        calendar_enabled=bool(brief_config.get("calendar_enabled", True)),
    )
    ledger = AttentionLedger(
        ledger_path,
        excerpt_chars=int(brief_config.get("excerpt_chars", 240)),
        excerpt_retention_days=int(
            brief_config.get("excerpt_retention_days", 7)
        ),
        resolved_retention_days=int(
            brief_config.get("resolved_retention_days", 30)
        ),
    )
    return MorningBriefService(
        client,
        ledger,
        self_open_id,
        timezone_name=str(brief_config.get("timezone", "Asia/Shanghai")),
        overlap_minutes=int(brief_config.get("overlap_minutes", 10)),
        excerpt_chars=int(brief_config.get("excerpt_chars", 240)),
    )
