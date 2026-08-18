"""设备唤醒词下发。

唤醒词是 esp-sr 预训练的 wakenet 神经网络权重，不是可配置的字符串，
所以只能在 esp-sr 自带的模型里选。模型打包成 assets 镜像放在 data/bin/ 下，
由 /xiaozhi/ota/download/ 提供下载，服务端通过设备的 MCP 工具
self.assets.set_download_url 把地址写进设备 NVS，设备下次启动时下载并生效。

打包脚本见 scripts/pack_wake_word_assets.py。
"""

import json
import os

from config.config_loader import get_project_dir
from core.utils.util import get_local_ip

TAG = __name__

ASSETS_TOOL = "self.assets.set_download_url"
_STATE_FILE = "data/.wake_word_state.json"


def get_wake_word_config(config: dict):
    """返回 (显示名, assets 包文件名)；未配置时返回 (None, None)。"""
    section = config.get("wake_word") or {}
    if not isinstance(section, dict):
        return None, None
    display = str(section.get("display") or "").strip()
    assets_file = str(section.get("assets_file") or "").strip()
    if not assets_file:
        return display or None, None
    # 只允许 data/bin 下的裸文件名，避免拼出越界路径
    if assets_file != os.path.basename(assets_file):
        return display or None, None
    return display or None, assets_file


def build_assets_url(config: dict, assets_file: str) -> str:
    """用服务端自身地址拼下载 URL，避免把 IP 写死在配置里（换网就失效）。"""
    server = config.get("server", {}) or {}
    port = int(server.get("http_port", 8003))
    return f"http://{get_local_ip()}:{port}/xiaozhi/ota/download/{assets_file}"


def _state_path() -> str:
    return get_project_dir() + _STATE_FILE


def _load_state() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def needs_push(device_id: str, url: str) -> bool:
    """设备已经拿过同一个包就不再下发，否则每次启动都会重复下载几百 KB。"""
    return _load_state().get(device_id) != url


def mark_pushed(device_id: str, url: str) -> None:
    state = _load_state()
    state[device_id] = url
    _save_state(state)


async def push_wake_word_assets(conn) -> bool:
    """设备 MCP 就绪后调用。已是最新则跳过，返回是否真的下发了。"""
    config = conn.config
    display, assets_file = get_wake_word_config(config)
    if not assets_file:
        return False

    local_path = os.path.join(get_project_dir(), "data", "bin", assets_file)
    if not os.path.isfile(local_path):
        conn.logger.bind(tag=TAG).error(
            f"唤醒词 assets 包不存在: {local_path}，跳过下发"
        )
        return False

    device_id = getattr(conn, "device_id", "") or ""
    url = build_assets_url(config, assets_file)
    if not needs_push(device_id, url):
        conn.logger.bind(tag=TAG).debug(f"设备 {device_id} 已是最新唤醒词包，跳过")
        return False

    # self.assets.set_download_url 是设备的 user-only 工具，不在 tools/list 里
    # （刻意如此，免得大模型能调重启/刷固件），所以绕过 call_mcp_tool 的存在性检查，
    # 直接发 tools/call —— 设备的 DoToolCall 不区分 user-only。
    from core.providers.tools.device_mcp.mcp_handler import send_mcp_message

    payload = {
        "jsonrpc": "2.0",
        "id": await conn.mcp_client.get_next_id(),
        "method": "tools/call",
        "params": {"name": ASSETS_TOOL, "arguments": {"url": url}},
    }
    await send_mcp_message(conn, payload)
    mark_pushed(device_id, url)
    conn.logger.bind(tag=TAG).info(
        f"已向设备 {device_id} 下发唤醒词 {display or assets_file} 的 assets 包: {url}"
    )
    return True
