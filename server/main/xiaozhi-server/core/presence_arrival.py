"""在岗状态到机器人表演的编排。

presence-agent 把工位在岗与本人识别结果 POST 给 Server，此前 Server 收下后只存进
内存字典，没有任何消费方（AGENTS.md「当前尚未接通的地方」第 3 条）。本模块就是那个
消费方：主人到岗时让机器人抬头、露出笑脸并开口问好，工位持续无人时让它低头休眠。

上报只在状态变化时发送，另有每 15 秒一条 reason=heartbeat 的心跳，所以这里收到的是
变化事件流而不是帧流，去重只需按「到岗周期」而不必按帧节流。

设计见 docs/superpowers/specs/2026-08-18-presence-arrival-orchestration-design.md
"""

from dataclasses import dataclass, field
import logging
import time
from typing import Any, Callable, Optional

from core.presence_registry import PresenceReport


TAG = __name__

# identity 收敛到这几个状态才算「认得清楚了」，可以据此决定说哪句问候。
# starting 是人脸校验尚未出结果，no_face 是这一帧没看到脸（人可能只是低头），
# 两者都还会变，过早下结论会把主人当成陌生人。
SETTLED_IDENTITY_STATES = frozenset(
    {"owner", "unknown", "not_enrolled", "multiple_faces"}
)

# starting 是 agent 还没判定，camera_error 说明摄像头本身有问题，
# 这两种状态下的在岗结论都不可信，一律不驱动机器人。
IGNORED_PRESENCE_STATES = frozenset({"starting", "camera_error"})

DEFAULT_IDENTITY_WAIT_SECONDS = 5.0
DEFAULT_ABSENT_GRACE_SECONDS = 90.0
DEFAULT_GREETING_OWNER = "早上好，今天也一起把事情搞定吧。"
DEFAULT_GREETING_GENERIC = "你好，我在这儿。"
DEFAULT_GREETING_EMOTION = "happy"
DEFAULT_GREETING_STATUS = "在岗"
DEFAULT_SLEEP_TEXT = "工位没人，我先眯一会儿。"
DEFAULT_SLEEP_EMOTION = "sleepy"
DEFAULT_SLEEP_STATUS = "休眠"


async def _default_push_event(conn, text: str, **kwargs) -> bool:
    from core.handle.pushHandle import push_work_event

    return await push_work_event(conn, text=text, **kwargs)


async def _default_push_alert(conn, text: str, **kwargs) -> None:
    from core.handle.pushHandle import push_alert_to_device

    return await push_alert_to_device(conn, text=text, **kwargs)


@dataclass
class _WorkstationState:
    """单个工位的到岗周期状态。"""

    greeted: bool = False
    asleep: bool = False
    present_since: Optional[float] = None
    absent_since: Optional[float] = None


@dataclass(frozen=True)
class _Settings:
    workstations: dict
    identity_wait_seconds: float
    absent_grace_seconds: float
    sleep_on_absent: bool
    greeting_owner: str
    greeting_generic: str
    greeting_emotion: str
    greeting_status: str
    sleep_text: str
    sleep_emotion: str
    sleep_status: str


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _read_settings(config: dict) -> Optional[_Settings]:
    """读配置段；未启用或没有可用工位映射时返回 None。"""
    section = (config or {}).get("presence_robot") or {}
    if not isinstance(section, dict) or not section.get("enabled", False):
        return None

    raw = section.get("workstations") or {}
    if not isinstance(raw, dict):
        return None
    workstations = {
        str(key): str(value)
        for key, value in raw.items()
        if str(key).strip() and str(value).strip()
    }
    if not workstations:
        return None

    return _Settings(
        workstations=workstations,
        identity_wait_seconds=_positive_float(
            section.get("identity_wait_seconds"), DEFAULT_IDENTITY_WAIT_SECONDS
        ),
        absent_grace_seconds=_positive_float(
            section.get("absent_grace_seconds"), DEFAULT_ABSENT_GRACE_SECONDS
        ),
        sleep_on_absent=bool(section.get("sleep_on_absent", True)),
        greeting_owner=str(section.get("greeting_owner") or DEFAULT_GREETING_OWNER),
        greeting_generic=str(
            section.get("greeting_generic") or DEFAULT_GREETING_GENERIC
        ),
        greeting_emotion=str(
            section.get("greeting_emotion") or DEFAULT_GREETING_EMOTION
        ),
        greeting_status=str(section.get("greeting_status") or DEFAULT_GREETING_STATUS),
        sleep_text=str(section.get("sleep_text") or DEFAULT_SLEEP_TEXT),
        sleep_emotion=str(section.get("sleep_emotion") or DEFAULT_SLEEP_EMOTION),
        sleep_status=str(section.get("sleep_status") or DEFAULT_SLEEP_STATUS),
    )


class PresenceArrivalOrchestrator:
    """把在岗状态变化翻译成机器人指令。

    推送函数与时钟都可注入，便于离线单测；生产用默认实现走 pushHandle。
    """

    def __init__(
        self,
        config: dict,
        device_registry,
        *,
        push_event: Optional[Callable] = None,
        push_alert: Optional[Callable] = None,
        clock: Optional[Callable[[], float]] = None,
        logger=None,
        settings: Optional[_Settings] = None,
    ) -> None:
        self._settings = settings or _read_settings(config)
        if self._settings is None:
            raise ValueError("presence_robot 未启用或没有配置任何工位映射")
        self._device_registry = device_registry
        self._push_event = push_event or _default_push_event
        self._push_alert = push_alert or _default_push_alert
        self._clock = clock or time.monotonic
        self._logger = logger or logging.getLogger(__name__)
        self._states: dict[str, _WorkstationState] = {}

    def _state_for(self, workstation_id: str) -> _WorkstationState:
        state = self._states.get(workstation_id)
        if state is None:
            state = _WorkstationState()
            self._states[workstation_id] = state
        return state

    async def on_report(self, report: PresenceReport, acceptance: dict) -> None:
        """每条被 registry 接受的上报调用一次。

        本方法自行吞掉所有异常：它挂在 /xiaozhi/presence/report 的处理路径上，
        编排失败不该把上报接口变成 500。
        """
        try:
            await self._dispatch(report, acceptance)
        except Exception:
            self._logger.exception("在岗编排处理失败，已忽略本次上报")

    async def _dispatch(self, report: PresenceReport, acceptance: dict) -> None:
        # 同一 event_id 重发只是网络重试，不该再问好一次
        if (acceptance or {}).get("duplicate"):
            return

        device_id = self._settings.workstations.get(report.workstation_id)
        if not device_id:
            # 一台 Server 可能接多个工位，其中只有部分配了机器人
            return

        if report.state in IGNORED_PRESENCE_STATES:
            return

        state = self._state_for(report.workstation_id)
        now = self._clock()

        if report.state == "present":
            await self._on_present(report, state, device_id, now)
        elif report.state == "absent":
            await self._on_absent(state, device_id, now)

    async def _on_present(
        self,
        report: PresenceReport,
        state: _WorkstationState,
        device_id: str,
        now: float,
    ) -> None:
        state.absent_since = None
        if state.present_since is None:
            state.present_since = now
        if state.greeted:
            return

        greeting = self._choose_greeting(report, state, now)
        if greeting is None:
            return

        conn = self._resolve_conn(device_id)
        if conn is None:
            # 设备还没连上来。不置已问候标记，等它上线后的下一条上报再补这次迎接。
            self._logger.info(
                f"工位 {report.workstation_id} 有人到岗，"
                f"但设备 {device_id} 不在线，跳过迎接"
            )
            return

        # 先置位再 await：两条上报交错时不会都判定为「该问好」
        state.greeted = True
        state.asleep = False
        try:
            await self._push_event(
                conn,
                text=greeting,
                emotion=self._settings.greeting_emotion,
                status=self._settings.greeting_status,
                speak=True,
            )
        except Exception:
            # 回滚标记，让下一条上报还能再试一次，否则一次抖动就永久丢掉这次迎接
            state.greeted = False
            raise
        self._logger.info(
            f"工位 {report.workstation_id} 到岗，已让设备 {device_id} 迎接：{greeting}"
        )

    def _choose_greeting(
        self, report: PresenceReport, state: _WorkstationState, now: float
    ) -> Optional[str]:
        """返回该说的问候语；identity 还没收敛且未超时则返回 None 表示继续等。"""
        identity_state = (report.identity or {}).get("state")
        if identity_state in SETTLED_IDENTITY_STATES:
            if identity_state == "owner":
                return self._settings.greeting_owner
            # 非本人一律通用问候：不喊姓名、不带任何个人信息
            return self._settings.greeting_generic

        waited = now - (state.present_since or now)
        if waited < self._settings.identity_wait_seconds:
            return None
        # 等不到确定结果就按不确定处理，宁可说通用问候也不要喊错人
        return self._settings.greeting_generic

    async def _on_absent(
        self, state: _WorkstationState, device_id: str, now: float
    ) -> None:
        state.present_since = None
        if state.absent_since is None:
            state.absent_since = now
        if now - state.absent_since < self._settings.absent_grace_seconds:
            # 宽限期内：可能只是弯腰捡东西，别急着睡，也别复位到岗标记
            return

        # 确认是真的离开了，复位到岗周期，下次回来重新迎接
        state.greeted = False

        if not self._settings.sleep_on_absent or state.asleep:
            return

        conn = self._resolve_conn(device_id)
        if conn is None:
            return

        state.asleep = True
        try:
            await self._push_alert(
                conn,
                text=self._settings.sleep_text,
                emotion=self._settings.sleep_emotion,
                status=self._settings.sleep_status,
                silent=True,
            )
        except Exception:
            state.asleep = False
            raise
        self._logger.info(f"工位持续无人，设备 {device_id} 已进入休眠")

    def _resolve_conn(self, device_id: str):
        if self._device_registry is None:
            return None
        return self._device_registry.get(device_id)


def create_presence_arrival_orchestrator(
    config: dict, device_registry, **kwargs
) -> Optional[PresenceArrivalOrchestrator]:
    """按配置装配编排器；未启用、无工位映射或拿不到设备注册表时返回 None。

    device_registry 来自 WebSocket 服务，presence_server.py 那个轻量入口没有它，
    此时不装配，presence 接口行为保持不变。
    """
    if device_registry is None:
        return None
    settings = _read_settings(config)
    if settings is None:
        return None
    return PresenceArrivalOrchestrator(
        config, device_registry, settings=settings, **kwargs
    )
