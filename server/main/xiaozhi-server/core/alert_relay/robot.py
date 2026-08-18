"""硬件桥：把告警中继的每个节点翻译成机器人的一个动作。

表情只在固件已有的 21 种里选，动作只在既有预设里选（见 AGENTS.md「硬件边界」）。
机器人是物理设备，任何一次失败都不许把整条告警链路带崩——所有失败都吞掉并记 last_error，
让飞书那一路照常送达。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.alert_relay.models import LEVEL_CRITICAL, LEVEL_URGENT, AlertEvent, Diagnosis


@dataclass(frozen=True)
class RobotCue:
    status: str
    emotion: str
    action: Optional[str] = None
    speak: bool = False
    restore_after: Optional[float] = None


# 告警级别 → 到达时的表情。警告级别刻意不出声：被打断的代价比漏看一条警告大。
ALERT_CUES = {
    LEVEL_URGENT: RobotCue("线上告警", "shocked", "look_up", speak=True),
    LEVEL_CRITICAL: RobotCue("线上告警", "shocked", "look_up", speak=True),
}
DEFAULT_ALERT_CUE = RobotCue("线上告警", "confused", "look_up", speak=False)

CLAIMED_CUE = RobotCue("排查中", "happy", "nod", speak=True, restore_after=None)
DIAGNOSING_CUE = RobotCue("排查中", "thinking", None, speak=False)
DIAGNOSED_CUE = RobotCue("排查完成", "confident", "nod", speak=True, restore_after=60)
FAILED_CUE = RobotCue("排查失败", "sad", "shake", speak=True, restore_after=60)
DECLINED_CUE = RobotCue("待机", "neutral", "nod", speak=False, restore_after=6)
ESCALATE_CUE = RobotCue("还没人接", "shocked", "look_up", speak=True)


class RobotNotifier:
    def __init__(
        self,
        device_registry: Any,
        device_id: str,
        *,
        enabled: bool = True,
        push: Callable[..., Any] | None = None,
        logger: Any = None,
    ) -> None:
        self._registry = device_registry
        self._device_id = str(device_id or "").strip()
        self._enabled = bool(enabled)
        self._push = push
        self._logger = logger or logging.getLogger(__name__)
        self.last_error = ""

    def available(self) -> bool:
        return bool(self._enabled and self._device_id and self._registry is not None)

    def health(self) -> dict[str, Any]:
        online = []
        if self._registry is not None:
            try:
                online = list(self._registry.device_ids())
            except Exception:
                online = []
        return {
            "enabled": self._enabled,
            "device_id": self._device_id,
            "device_online": self._device_id in online,
            "online_devices": online,
            "last_error": self.last_error,
        }

    def _resolve_push(self) -> Callable[..., Any]:
        if self._push is not None:
            return self._push
        # 延迟导入：pushHandle 会牵进 TTS / MCP 那一坨，纯 HTTP 场景不该为它付启动代价。
        from core.handle.pushHandle import push_work_event

        return push_work_event

    async def _send(self, cue: RobotCue, text: str) -> bool:
        self.last_error = ""
        if not self._enabled:
            self.last_error = "机器人联动未启用"
            return False
        if not self._device_id:
            self.last_error = "未配置 device_id，无法定位机器人"
            return False
        if self._registry is None:
            self.last_error = "服务未开放设备注册表（WebSocket 服务未启动）"
            return False

        conn = self._registry.get(self._device_id)
        if conn is None:
            self.last_error = f"设备 {self._device_id} 不在线"
            return False

        try:
            await self._resolve_push()(
                conn,
                text=text,
                emotion=cue.emotion,
                status=cue.status,
                speak=cue.speak,
                action=cue.action,
                restore_after=cue.restore_after,
            )
            return True
        except Exception as exc:
            self.last_error = f"推送失败: {exc}"
            try:
                self._logger.warning(f"机器人告警推送失败: {exc}")
            except Exception:
                pass
            return False

    async def alert(self, event: AlertEvent) -> bool:
        cue = ALERT_CUES.get(event.level, DEFAULT_ALERT_CUE)
        return await self._send(cue, f"线上告警：{event.summary()}")

    async def escalate(self, event: AlertEvent) -> bool:
        return await self._send(ESCALATE_CUE, f"还没人接：{event.summary()}")

    async def claimed(self, event: AlertEvent, *, by: str = "") -> bool:
        who = f"{by}接手了，" if by else ""
        return await self._send(CLAIMED_CUE, f"{who}我去查这条告警")

    async def diagnosing(self, event: AlertEvent) -> bool:
        return await self._send(DIAGNOSING_CUE, f"排查中：{event.summary()}")

    async def diagnosed(self, diagnosis: Diagnosis) -> bool:
        return await self._send(DIAGNOSED_CUE, diagnosis.speech_line())

    async def failed(self, reason: str) -> bool:
        return await self._send(FAILED_CUE, f"没查成：{str(reason)[:24]}")

    async def declined(self) -> bool:
        return await self._send(DECLINED_CUE, "好，这条你自己看")
