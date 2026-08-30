"""从服务配置装配告警值班中继。

密钥一律「环境变量 > data/.config.yaml > config.yaml」：
`config.yaml` 是入库文件，app_secret 写进去就等于泄露。
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any, Mapping

from core.alert_relay.diagnosis_runner import (
    DEFAULT_ALLOWED_TOOLS,
    DEFAULT_CLI_COMMAND,
    DEFAULT_SKILL,
    ClaudeCodeRunner,
)
from core.alert_relay.feishu_bot import DEFAULT_BASE_URL, FeishuBot
from core.alert_relay.robot import RobotNotifier
from core.alert_relay.service import AlertRelayService


# server/main/xiaozhi-server/core/alert_relay/factory.py → 仓库根
REPO_ROOT = Path(__file__).resolve().parents[5]

# 中继记录的默认落盘位置（同 data/ 下的 away_ledger.json / owner_status.json）。
# 生产必须带上它：不落盘的话，重启后飞书里的卡片就成了孤儿。
DEFAULT_PERSIST_PATH = "data/alert_relay_records.json"


def default_diagnosis_cwd() -> str:
    """诊断子进程的默认工作目录：仓库根。

    随仓库分发的 skill 在 `<仓库根>/.claude/skills/` 下，Claude Code 只会在工作目录
    这一侧找项目级 skill。默认跑在 `server/main/xiaozhi-server` 里的话，别人克隆下来
    照样找不到 skill——这正是「换台电脑就跑不起来」的成因之一。
    """
    if (REPO_ROOT / ".claude" / "skills").is_dir():
        return str(REPO_ROOT)
    return ""


def _section(config: Mapping[str, Any], *path: str) -> dict[str, Any]:
    current: Any = config
    for key in path:
        current = (current or {}).get(key, {}) if isinstance(current, Mapping) else {}
    return dict(current) if isinstance(current, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # 允许在 YAML 里写成一行命令，省得逼人手写 argv 数组。
        return shlex.split(value)
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _cluster_map(raw: Any) -> dict[str, tuple[str, str]]:
    """YAML 里 projectId 常被写成数字，拼 URL 前必须统一成字符串。"""
    mapping: dict[str, tuple[str, str]] = {}
    if not isinstance(raw, Mapping):
        return mapping
    for cluster, ids in raw.items():
        if isinstance(ids, Mapping):
            project_id = ids.get("project_id") or ids.get("projectId")
            cluster_id = ids.get("cluster_id") or ids.get("clusterId")
        elif isinstance(ids, (list, tuple)) and len(ids) >= 2:
            project_id, cluster_id = ids[0], ids[1]
        else:
            continue
        mapping[str(cluster)] = (str(project_id), str(cluster_id))
    return mapping


def create_alert_relay_service(
    config: Mapping[str, Any],
    device_registry: Any = None,
    logger: Any = None,
) -> AlertRelayService:
    relay = _section(config, "alert_relay")
    feishu = _section(config, "alert_relay", "feishu")
    robot_config = _section(config, "alert_relay", "robot")
    diagnosis = _section(config, "alert_relay", "diagnosis")
    fast_mode = bool(diagnosis.get("fast_mode", True))
    timeout_value = diagnosis.get("timeout_seconds")

    bot = FeishuBot(
        base_url=str(feishu.get("base_url") or DEFAULT_BASE_URL),
        app_id=os.environ.get("FEISHU_BOT_APP_ID") or str(feishu.get("app_id") or ""),
        app_secret=os.environ.get("FEISHU_BOT_APP_SECRET")
        or str(feishu.get("app_secret") or ""),
        timeout_seconds=float(feishu.get("timeout_seconds", 10)),
    )

    robot = RobotNotifier(
        device_registry,
        os.environ.get("ALERT_RELAY_DEVICE_ID") or str(robot_config.get("device_id") or ""),
        enabled=bool(robot_config.get("enabled", True)),
        logger=logger,
    )

    runner = ClaudeCodeRunner(
        cli_command=_string_list(diagnosis.get("cli_command")) or list(DEFAULT_CLI_COMMAND),
        skill=str(diagnosis.get("skill") or DEFAULT_SKILL),
        model=str(diagnosis.get("model") or ""),
        source_dirs=_string_list(diagnosis.get("source_dirs")),
        cwd=str(diagnosis.get("cwd") or default_diagnosis_cwd()),
        permission_mode=str(diagnosis.get("permission_mode") or "dontAsk"),
        allowed_tools=_string_list(diagnosis.get("allowed_tools"))
        or list(DEFAULT_ALLOWED_TOOLS),
        extra_args=_string_list(diagnosis.get("extra_args")),
        timeout_seconds=float(timeout_value) if timeout_value is not None else None,
        fast_mode=fast_mode,
        logger=logger,
    )

    return AlertRelayService(
        robot=robot,
        feishu_bot=bot,
        runner=runner,
        receive_id=os.environ.get("FEISHU_ALERT_RECEIVE_ID")
        or str(relay.get("receive_id") or ""),
        enabled=bool(relay.get("enabled", False)),
        dedupe_window_seconds=float(relay.get("dedupe_window_seconds", 300)),
        reply_timeout_seconds=float(relay.get("reply_timeout_seconds", 600)),
        auto_diagnose_on_timeout=bool(relay.get("auto_diagnose_on_timeout", False)),
        cluster_map=_cluster_map(relay.get("cluster_map")),
        max_records=int(relay.get("max_records", 200)),
        persist_path=str(relay.get("persist_path") or DEFAULT_PERSIST_PATH),
        logger=logger,
    )
