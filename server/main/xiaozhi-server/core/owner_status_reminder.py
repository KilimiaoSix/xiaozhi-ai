"""主人状态到期提醒编排。

meeting/away 到了 expected_return 后，store.get() 只把 overdue 标成 True，
不会主动做任何事——按 core/owner_status.py 顶部的注释，"到点问主人一句"是
编排层的职责（流程四·状态过期）。本模块周期性检查 store，overdue 时把提醒
推给主人所在设备，且同一次状态设置（同一个 set_at）只提醒一次，避免每个
检查周期都重复打扰；状态被重新设置（新的 set_at）后自动重新武装。

device_resolver / push_event / presence_lookup 都可注入，且都允许同步或
协程两种实现：生产环境里它们通常要去查设备注册表、presence 状态这些跑在
事件循环里的资源，测试则直接传同步的假实现。

任务管理照抄 core/pomodoro_manager.py 的做法：start()/stop() 管一个可取消的
asyncio.Task，不用裸 while True 挂着不管生命周期。
"""

import asyncio
import inspect
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from core.owner_status import OwnerStatusStore

TAG = __name__

DEFAULT_CHECK_INTERVAL_S = 60.0

REMINDER_TEXT = "已经到你说的返回时间了，还在会议中吗？需要我继续保持会议中状态吗？"


async def _default_push_event(conn, text: str, **kwargs) -> bool:
    from core.handle.pushHandle import push_work_event

    return await push_work_event(conn, text=text, **kwargs)


async def _maybe_await(value: Any) -> Any:
    """device_resolver / presence_lookup 允许同步或异步实现，这里统一摊平。"""
    if inspect.isawaitable(value):
        return await value
    return value


class OwnerStatusReminder:
    """周期检查主人状态是否过期，过期时提醒一次。"""

    def __init__(
        self,
        store: OwnerStatusStore,
        device_resolver: Callable[[], Any],
        *,
        push_event: Optional[Callable[..., Awaitable[bool]]] = None,
        presence_lookup: Optional[Callable[[], Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        check_interval_s: float = DEFAULT_CHECK_INTERVAL_S,
        logger=None,
    ) -> None:
        self._store = store
        self._device_resolver = device_resolver
        self._push_event = push_event or _default_push_event
        self._presence_lookup = presence_lookup
        self._clock = clock or datetime.now
        self._sleep = sleep or asyncio.sleep
        self._check_interval_s = max(1.0, float(check_interval_s))
        self._logger = logger or logging.getLogger(__name__)
        self._task: Optional[asyncio.Task] = None
        # 按 set_at 去重：同一次状态设置只提醒一次；状态被重设后 set_at 变了，
        # 下一次检查就会发现是个"没提醒过的 set_at"，自动重新武装。
        self._reminded_set_at: Optional[str] = None

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        """起一个周期检查任务；已在跑就什么也不做，避免重复起两条循环。"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """取消循环并等它真正退出，供进程关闭时优雅收尾。"""
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        try:
            while True:
                await self.check_once()
                await self._sleep(self._check_interval_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 循环本身的意外崩溃不该悄无声息地让提醒永久停摆而没有任何痕迹
            self._logger.exception("主人状态提醒循环异常退出")

    # -------------------------------------------------------------------- 检查

    async def check_once(self) -> bool:
        """检查一次并在需要时提醒；返回是否实际发出了提醒（供测试与日志用）。"""
        try:
            status = self._store.get()
        except Exception:
            self._logger.exception("读取主人状态失败，跳过本次提醒检查")
            return False

        if not status.get("overdue"):
            return False

        set_at = status.get("set_at")
        if set_at and set_at == self._reminded_set_at:
            return False

        if self._presence_lookup is not None:
            presence_state = await _maybe_await(self._presence_lookup())
            # 只有明确不是"在场"时才不提醒；presence_lookup 缺省或答不上来
            # （返回 None）都按可以提醒处理，宁可问一句也不要漏提醒。
            if presence_state is not None and presence_state != "present":
                return False

        resolved = await _maybe_await(self._device_resolver())
        if not resolved:
            return False
        device_id, conn = resolved
        if conn is None:
            return False

        try:
            await self._push_event(conn, text=REMINDER_TEXT, speak=True)
        except Exception:
            self._logger.exception(f"设备 {device_id} 主人状态提醒推送失败")
            return False

        self._reminded_set_at = set_at
        self._logger.info(f"设备 {device_id} 已收到主人状态过期提醒")
        return True
