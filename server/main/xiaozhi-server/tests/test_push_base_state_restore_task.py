"""基态恢复任务的登记表：被替换掉的旧任务不能把新任务摘成孤儿。

`_restore_tasks` 是「这台设备还欠一次收画面」的唯一登记处，也是取消它的唯一
入口（新推送来了要取消上一个，否则旧的恢复会把新事件的画面冲掉）。旧任务
收尾时若只看「有没有条目」就 pop，摘掉的会是别人刚登记的新任务：新任务照常
睡着，却再也没人取消得了它，到点用基态覆盖此刻屏上的画面——番茄钟相位、
刚到的告警、聆听画面，全被静默顶掉。

时序是确定性的，不需要多线程：取消旧任务只是投递 CancelledError，它的 finally
要等事件循环下一次恢复它才跑，而「取消旧的 → 登记新的」中间没有 await。
"""

import asyncio
import json

import pytest

from core.handle import pushHandle


DEVICE_ID = "dc:da:0c:26:9a:60"


class _StubLogger:
    def bind(self, **_kwargs):
        return self

    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass


class FakeWebsocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


class FakeConn:
    def __init__(self):
        self.device_id = DEVICE_ID
        self.session_id = "sess-restore"
        self.logger = _StubLogger()
        self.websocket = FakeWebsocket()

    @property
    def sent(self):
        return self.websocket.sent


@pytest.fixture
def store_path(tmp_path):
    """独立的基态存储 + 用完清干净登记表，别把待恢复任务漏给下一个用例。"""
    path = tmp_path / "base_states.json"
    pushHandle.set_base_state_store(path)
    yield path
    for task in list(pushHandle._restore_tasks.values()):
        task.cancel()
    pushHandle._restore_tasks.clear()
    pushHandle.set_base_state_store(pushHandle.DEFAULT_BASE_STATE_PATH)
    pushHandle._base_states.clear()


@pytest.mark.asyncio
async def test_replaced_restore_task_does_not_deregister_its_successor(store_path):
    conn = FakeConn()

    pushHandle._schedule_base_state_restore(conn, DEVICE_ID, 5)
    old = pushHandle._restore_tasks[DEVICE_ID]
    await asyncio.sleep(0.01)  # 旧任务睡进 sleep

    pushHandle._schedule_base_state_restore(conn, DEVICE_ID, 5)  # 取消旧的，登记新的
    new = pushHandle._restore_tasks[DEVICE_ID]
    await asyncio.sleep(0.01)  # 旧任务的 CancelledError 与 finally 在这里跑

    assert old.cancelled()
    assert pushHandle._restore_tasks.get(DEVICE_ID) is new


@pytest.mark.asyncio
async def test_orphaned_restore_task_cannot_overwrite_the_live_screen(store_path):
    """摘成孤儿的任务连 clear_base_state 都取消不了，到点照样覆盖当前画面。"""
    conn = FakeConn()
    pushHandle.set_base_state(DEVICE_ID, "待机", "", "neutral")

    pushHandle._schedule_base_state_restore(conn, DEVICE_ID, 5)  # 会议时长级的旧任务
    await asyncio.sleep(0.01)
    pushHandle._schedule_base_state_restore(conn, DEVICE_ID, 0.05)  # 新推送替换掉它
    await asyncio.sleep(0.01)

    pushHandle.clear_base_state(DEVICE_ID)  # 新会话开始，待恢复必须取消得掉
    await asyncio.sleep(0.15)

    assert conn.sent == []


@pytest.mark.asyncio
async def test_completed_restore_deregisters_itself(store_path):
    """正常跑完的任务要把自己从登记表里摘掉，别让条目越堆越多。"""
    conn = FakeConn()
    pushHandle.set_base_state(DEVICE_ID, "在岗", "", "neutral")

    pushHandle._schedule_base_state_restore(conn, DEVICE_ID, 0.01)
    task = pushHandle._restore_tasks[DEVICE_ID]
    await task

    assert pushHandle._restore_tasks.get(DEVICE_ID) is None
    assert conn.sent[-1]["status"] == "在岗"
    assert conn.sent[-1]["silent"] is True
