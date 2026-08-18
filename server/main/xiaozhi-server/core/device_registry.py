"""在线设备注册表。

设备侧改为常连 WebSocket 后，服务端需要按 device-id 找到活跃连接，才能把外部
工作事件（Codex / Claude Code 任务状态等）主动推给设备。

刻意不引入任何重依赖，保证可独立测试。
"""
from typing import Any, Dict, List, Optional


class DeviceRegistry:
    def __init__(self):
        self._connections: Dict[str, Any] = {}

    def register(self, device_id: Optional[str], conn: Any) -> None:
        if not device_id:
            return
        self._connections[device_id] = conn

    def unregister(self, device_id: Optional[str], conn: Any) -> None:
        """只注销自己那条连接。

        设备重连时新连接可能先于旧连接的 finally 建立，直接 pop 会误删新连接。
        """
        if not device_id:
            return
        if self._connections.get(device_id) is conn:
            self._connections.pop(device_id, None)

    def get(self, device_id: Optional[str]) -> Optional[Any]:
        if not device_id:
            return None
        return self._connections.get(device_id)

    def device_ids(self) -> List[str]:
        return list(self._connections.keys())

    def count(self) -> int:
        return len(self._connections)
