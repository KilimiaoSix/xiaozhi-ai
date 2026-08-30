"""设备基态的落盘与重启恢复。

基态原本只活在 pushHandle 的模块级 dict 里：进程一重启，
`get_base_state` 就回落默认「待机」——人明明在工位、屏幕却写着待机，
下一条带 restore_after 的提醒播完还会把这个错的画面钉上去，
只能等摄像头链路下一次纠偏才改回来。

本文件锁的是「新进程从同一 data 目录装载」这一条：
落盘沿用 away_ledger / incident_manager 的 .tmp + rename 原子替换，
坏文件按空存储处理，绝不崩在启动路径上。
"""

import json
from pathlib import Path

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
        self.session_id = "fake-session"
        self.logger = _StubLogger()
        self.websocket = FakeWebsocket()


@pytest.fixture
def store_path(tmp_path):
    """每个用例一份独立的基态存储，用完把模块恢复到默认路径。"""
    path = tmp_path / "base_states.json"
    pushHandle.set_base_state_store(path)
    yield path
    pushHandle.set_base_state_store(pushHandle.DEFAULT_BASE_STATE_PATH)
    pushHandle._base_states.clear()


def restart(path: Path) -> None:
    """模拟进程重启：内存态全丢，只剩盘上那份文件。"""
    pushHandle._base_states.clear()
    pushHandle.set_base_state_store(path)


def test_base_state_survives_a_restart(store_path):
    pushHandle.set_base_state(DEVICE_ID, "在岗", "", "neutral")

    restart(store_path)

    assert pushHandle.get_base_state(DEVICE_ID) == {
        "status": "在岗",
        "message": "",
        "emotion": "neutral",
    }


def test_sleep_base_state_survives_a_restart(store_path):
    """离席休眠基态同样要活过重启，否则重启后离席工位会亮成待机。"""
    pushHandle.set_base_state(DEVICE_ID, "休眠", "我先眯一会儿", "sleepy")

    restart(store_path)

    assert pushHandle.get_base_state(DEVICE_ID)["emotion"] == "sleepy"


def test_cleared_base_state_does_not_come_back_after_restart(store_path):
    pushHandle.set_base_state(DEVICE_ID, "在岗", "", "neutral")
    pushHandle.clear_base_state(DEVICE_ID)

    restart(store_path)

    assert pushHandle.get_base_state(DEVICE_ID) == pushHandle.DEFAULT_BASE_STATE


def test_unknown_device_still_falls_back_to_the_default(store_path):
    pushHandle.set_base_state(DEVICE_ID, "在岗", "", "neutral")

    restart(store_path)

    assert pushHandle.get_base_state("other-device") == pushHandle.DEFAULT_BASE_STATE


def test_corrupt_store_is_treated_as_empty(store_path):
    """半截 JSON 不能把服务卡在启动路径上，按空存储处理即可。"""
    store_path.write_text('{"version": 1, "devices": {"dc', encoding="utf-8")

    restart(store_path)

    assert pushHandle.get_base_state(DEVICE_ID) == pushHandle.DEFAULT_BASE_STATE
    # 后续写入要能把坏文件整体覆盖掉，不能一直卡着
    pushHandle.set_base_state(DEVICE_ID, "在岗", "", "neutral")
    restart(store_path)
    assert pushHandle.get_base_state(DEVICE_ID)["status"] == "在岗"


def test_persist_is_atomic_and_leaves_no_tmp_file(store_path):
    pushHandle.set_base_state(DEVICE_ID, "在岗", "", "neutral")

    data = json.loads(store_path.read_text(encoding="utf-8"))

    assert data["devices"][DEVICE_ID]["status"] == "在岗"
    assert list(store_path.parent.glob("*.tmp")) == []


def test_repeated_identical_writes_do_not_touch_the_disk(store_path, monkeypatch):
    """离席心跳每 15 秒重写一次同样的基态，没必要每次都落盘。"""
    writes = []
    original = pushHandle._write_base_state_store

    def counting_write():
        writes.append(1)
        original()

    monkeypatch.setattr(pushHandle, "_write_base_state_store", counting_write)

    pushHandle.set_base_state(DEVICE_ID, "休眠", "我先眯一会儿", "sleepy")
    pushHandle.set_base_state(DEVICE_ID, "休眠", "我先眯一会儿", "sleepy")
    pushHandle.set_base_state(DEVICE_ID, "休眠", "我先眯一会儿", "sleepy")

    assert len(writes) == 1


@pytest.mark.asyncio
async def test_restore_after_a_restart_uses_the_persisted_base_state(store_path):
    """端到端：重启后带 restore_after 的推送必须收回到「在岗」而不是「待机」。"""
    pushHandle.set_base_state(DEVICE_ID, "在岗", "", "neutral")
    restart(store_path)

    conn = FakeConn()
    await pushHandle.push_work_event(
        conn, "任务完成", emotion="happy", status="任务完成", restore_after=0.01
    )
    task = pushHandle._restore_tasks.get(DEVICE_ID)
    assert task is not None
    await task

    assert conn.websocket.sent[-1]["status"] == "在岗"
    assert conn.websocket.sent[-1]["silent"] is True
