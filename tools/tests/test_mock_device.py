"""模拟设备 tools/mock_device.py 的握手与下行处理测试。

替身只有一处：Server 侧换成一个按真实契约发消息的假 Server（照抄
core/websocket_server.py 的连接约定与 device_mcp/mcp_handler.py 的 MCP 报文），
被测的模拟设备本身是真的——真 websocket 连接、真 JSON 报文、真 ping。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
import websockets
import websockets.exceptions  # noqa: F401  websockets 不会自动挂出这个子模块属性，显式导入避免孤立跑本文件时 AttributeError

TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import mock_device  # noqa: E402

EXPECTED_TOOLS = [
    "self.robot.play_action",
    "self.robot.set_emotion",
    "self.robot.set_idle_animation",
    "self.pomodoro.show",
]


# ── 纯函数层 ────────────────────────────────────────────────


def test_registers_exactly_the_four_firmware_tools():
    assert [tool["name"] for tool in mock_device.build_tools_list()] == EXPECTED_TOOLS


def test_pomodoro_schema_matches_agents_contract():
    tool = next(
        t for t in mock_device.build_tools_list() if t["name"] == "self.pomodoro.show"
    )
    schema = tool["inputSchema"]
    assert set(schema["properties"]) == {
        "phase",
        "paused",
        "remaining_s",
        "total_s",
        "round",
        "total_rounds",
    }
    # 固件里只有 phase 没有默认值，所以 required 只有它一项
    assert schema["required"] == ["phase"]
    assert schema["properties"]["paused"]["type"] == "boolean"
    assert schema["properties"]["remaining_s"]["type"] == "integer"
    assert schema["properties"]["phase"]["type"] == "string"


def test_robot_tool_schemas_match_agents_contract():
    tools = {t["name"]: t for t in mock_device.build_tools_list()}
    assert tools["self.robot.play_action"]["inputSchema"]["required"] == ["action"]
    assert tools["self.robot.set_emotion"]["inputSchema"]["required"] == ["emotion"]
    idle = tools["self.robot.set_idle_animation"]["inputSchema"]
    assert idle["required"] == ["enabled"]
    assert idle["properties"]["enabled"]["type"] == "boolean"


def test_default_device_id_is_obviously_a_mock():
    assert mock_device.DEFAULT_DEVICE_ID.count(":") == 5
    assert "mo:ck" in mock_device.DEFAULT_DEVICE_ID


def test_hello_message_matches_firmware_handshake():
    device = mock_device.MockDevice(server="ws://127.0.0.1:8000/xiaozhi/v1/")
    hello = device.hello_message()
    assert hello["type"] == "hello"
    assert hello["transport"] == "websocket"
    assert hello["features"]["mcp"] is True
    assert hello["audio_params"]["format"] == "opus"
    assert hello["audio_params"]["sample_rate"] == 16000
    assert hello["audio_params"]["channels"] == 1


# ── 假 Server ───────────────────────────────────────────────


class FakeServer:
    """按真实 Server 的报文顺序驱动一次完整握手，并记录设备的应答。"""

    def __init__(self):
        self.request_path = None
        self.headers = {}
        self.hello = None
        self.replies = {}
        self.pings = 0
        self.got_hello = asyncio.Event()
        self.script_done = asyncio.Event()

    async def _wait_reply(self, msg_id, timeout=5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while msg_id not in self.replies:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"设备没有回复 MCP id={msg_id}")
            await asyncio.sleep(0.01)
        return self.replies[msg_id]

    async def _send_mcp(self, connection, payload):
        await connection.send(json.dumps({"type": "mcp", "payload": payload}))

    async def _script(self, connection):
        await asyncio.wait_for(self.got_hello.wait(), timeout=5)
        await connection.send(
            json.dumps(
                {
                    "type": "hello",
                    "transport": "websocket",
                    "session_id": "fake-session",
                    "audio_params": {"sample_rate": 24000},
                }
            )
        )

        await self._send_mcp(
            connection,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"vision": {"url": "http://x", "token": "t"}},
                },
            },
        )
        await self._wait_reply(1)

        await self._send_mcp(
            connection, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        await self._wait_reply(2)

        await self._send_mcp(
            connection,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "self.robot.play_action",
                    "arguments": {"action": "nod"},
                },
            },
        )
        await self._wait_reply(3)

        await self._send_mcp(
            connection,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "self.robot.no_such_tool", "arguments": {}},
            },
        )
        await self._wait_reply(4)

        await self._send_mcp(
            connection, {"jsonrpc": "2.0", "id": 5, "method": "resources/list"}
        )
        await self._wait_reply(5)

        for message in (
            {"type": "tts", "state": "start", "session_id": "fake-session"},
            {
                "type": "tts",
                "state": "sentence_start",
                "text": "任务完成",
                "session_id": "fake-session",
            },
            {"type": "stt", "text": "现在几点", "session_id": "fake-session"},
            {"type": "llm", "text": "🙂", "emotion": "happy"},
            {
                "type": "alert",
                "status": "任务完成",
                "message": "Codex 任务已完成",
                "emotion": "happy",
                "silent": False,
            },
            {"type": "system", "command": "reboot"},
        ):
            await connection.send(json.dumps(message))

        while self.pings == 0:
            await asyncio.sleep(0.01)
        self.script_done.set()

    async def handler(self, connection):
        self.request_path = connection.request.path
        self.headers = {k.lower(): v for k, v in connection.request.headers.items()}
        script = asyncio.create_task(self._script(connection))
        try:
            async for raw in connection:
                if isinstance(raw, bytes):
                    continue
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "hello":
                    self.hello = message
                    self.got_hello.set()
                elif kind == "ping":
                    self.pings += 1
                    await connection.send(json.dumps({"type": "pong"}))
                elif kind == "mcp":
                    payload = message.get("payload") or {}
                    self.replies[int(payload.get("id", 0))] = payload
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            script.cancel()


async def _run_session():
    fake = FakeServer()
    logs = []
    async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        device = mock_device.MockDevice(
            server=f"ws://127.0.0.1:{port}/xiaozhi/v1/",
            device_id="mo:ck:00:00:00:99",
            duration=2.0,
            ping_interval=0.1,
            log=logs.append,
        )
        await asyncio.wait_for(device.run(), timeout=15)
        await asyncio.wait_for(fake.script_done.wait(), timeout=5)
    return fake, logs


def test_full_handshake_against_fake_server():
    fake, logs = asyncio.run(_run_session())

    # 连接契约
    assert fake.request_path.startswith("/xiaozhi/v1/")
    assert fake.headers.get("device-id") == "mo:ck:00:00:00:99"
    assert fake.headers.get("client-id")

    # hello 握手
    assert fake.hello["type"] == "hello"
    assert fake.hello["features"]["mcp"] is True

    # MCP initialize
    init = fake.replies[1]["result"]
    assert init["protocolVersion"] == "2024-11-05"
    assert init["serverInfo"]["name"]

    # tools/list
    tools = fake.replies[2]["result"]["tools"]
    assert [t["name"] for t in tools] == EXPECTED_TOOLS

    # tools/call 成功
    call = fake.replies[3]["result"]
    assert call["isError"] is False
    assert call["content"][0]["type"] == "text"

    # tools/call 未知工具 → 按固件行为回 error
    assert "error" in fake.replies[4]
    assert "no_such_tool" in fake.replies[4]["error"]["message"]

    # 未实现的方法 → error
    assert "error" in fake.replies[5]

    # 30s ping 保活（测试里压到 0.1s）
    assert fake.pings >= 1

    # 下行结构化打印，且全部带「模拟设备」标注
    text = "\n".join(logs)
    assert all("模拟设备" in line for line in logs), logs
    for kind in ("tts", "stt", "llm", "alert", "system"):
        assert kind in text, f"没有打印 {kind} 下行：{text}"
    assert "Codex 任务已完成" in text
    assert "self.robot.play_action" in text


def test_duration_makes_the_device_exit_by_itself():
    async def _timed():
        loop = asyncio.get_running_loop()
        started = loop.time()
        await _run_session()
        return loop.time() - started

    elapsed = asyncio.run(_timed())
    assert elapsed < 10, f"--duration 到点没有自己退出，用了 {elapsed:.1f}s"


# ── --duration 分支：reader 提前结束不能被吞 ───────────────────


class AbruptCloseServer:
    """握手完就带错误码断连，模拟"连接被服务端断开"而不是"跑满 duration"。"""

    async def handler(self, connection):
        async for raw in connection:
            if isinstance(raw, bytes):
                continue
            message = json.loads(raw)
            if message.get("type") == "hello":
                await connection.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "transport": "websocket",
                            "session_id": "fake-session",
                            "audio_params": {"sample_rate": 24000},
                        }
                    )
                )
                # 1011 = internal error，不是正常关闭码，客户端 recv 会报 ConnectionClosedError
                await connection.close(code=1011, reason="server boom")
                return


async def _run_abrupt_close():
    fake = AbruptCloseServer()
    async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        device = mock_device.MockDevice(
            server=f"ws://127.0.0.1:{port}/xiaozhi/v1/",
            device_id="mo:ck:00:00:00:98",
            # duration 特意给得比服务端断连所需的时间长得多：
            # 走到超时分支就说明异常被 asyncio.wait 的 timeout 吞掉了，是回归
            duration=5.0,
            ping_interval=0.1,
        )
        await device.run()


def test_duration_branch_reraises_when_reader_ends_early_from_disconnect():
    """reader 任务在 duration 到点前就因连接被断开而结束时，异常不能被吞成退出码 0。"""
    with pytest.raises(websockets.exceptions.ConnectionClosedError):
        asyncio.run(asyncio.wait_for(_run_abrupt_close(), timeout=10))


# ── main() 的握手超时兜底 ───────────────────────────────────────
#
# 真实握手超时要等满 open_timeout=10 秒才触发，直接造太重；这里改成给
# MockDevice.run 打个桩直接抛 asyncio.TimeoutError，专门验证 main() 的
# except 子句真的接得住它——Python 3.10 下 asyncio.TimeoutError 不是
# OSError 的子类（3.11+ 才是 TimeoutError 的别名），只 except OSError 接不住。


def test_main_handles_handshake_timeout_without_unhandled_exception(monkeypatch, capsys):
    async def _raise_timeout(self):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(mock_device.MockDevice, "run", _raise_timeout)

    exit_code = mock_device.main(["--server", "ws://127.0.0.1:1/xiaozhi/v1/"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "连不上" in captured.err
    assert "模拟设备" in captured.err
