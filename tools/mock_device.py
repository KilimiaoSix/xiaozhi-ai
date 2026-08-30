#!/usr/bin/env python3
"""模拟设备：没有真机时顶替 ESP32-S3 挂到 Server 上，用来验证三端链路。

**这是模拟设备，不是真机。** 所有日志都带「模拟设备」前缀，默认 device-id 是
一个一眼能看出来的假 MAC（mo:ck:…），不会顶替真机在 DeviceRegistry 里的位置。

行为按 firmware/main/protocols/websocket_protocol.cc 与 firmware/main/mcp_server.cc
对齐真机：

1. 连 ``ws://host:8000/xiaozhi/v1/``，带 ``Device-Id`` / ``Client-Id`` /
   ``Protocol-Version`` 请求头（server/core/websocket_server.py 只认 device-id，
   auth 关闭时不需要 token）。
2. 连上先发 ``hello``（features.mcp=true），等服务端回欢迎 hello 拿 session_id。
3. 每 30 秒发一次 ``{"type":"ping"}`` 保活——Server 的
   ``close_connection_no_voice_time`` 靠它续命，不发会被当哑设备踢掉。
4. 收到服务端的 MCP 请求时按固件行为应答：``initialize`` 报 serverInfo，
   ``tools/list`` 交出 4 个工具（参数 schema 抄的 emoji_board.cc），
   ``tools/call`` 回 ``{"content":[…],"isError":false}`` 并把调用详情打出来。
5. ``tts`` / ``stt`` / ``llm`` / ``alert`` / ``system`` 下行结构化打印，二进制
   Opus 帧只计数不解码。

用法::

    tools/mock_device.py                         # 连本机 8000，一直挂着
    tools/mock_device.py --duration 30           # 挂 30 秒后干净断开
    tools/mock_device.py --server 192.168.1.5:8000 --device-id mo:ck:00:00:00:02

或经统一启动器：``./gongban mock-device --duration 30``
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import sys
import time
import uuid
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import websockets

LOG_PREFIX = "[模拟设备]"
DEFAULT_SERVER = "ws://127.0.0.1:8000/xiaozhi/v1/"
DEFAULT_WS_PORT = 8000
# 一眼假的 MAC：mo/ck 不是十六进制，扫一眼设备列表就知道这条不是真机
DEFAULT_DEVICE_ID = "mo:ck:00:00:00:01"
PING_INTERVAL_SECONDS = 30.0
PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "mock-deskbot", "version": "0.1.0-mock"}

# 与 firmware/main/boards/esp32-s3n16r8-emoji/emoji_board.cc InitializeTools() 一致。
# 固件的 Property 有默认值时不进 required，所以只有 phase/action/emotion/enabled 是必填。
_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "self.robot.play_action",
        "description": (
            "Play a physical head action on the desktop robot, for embodied feedback. "
            "Available actions: nod, shake, roll, look_left/right/up/down, "
            "hold_left/right/up/down, center."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        },
    },
    {
        "name": "self.robot.set_emotion",
        "description": (
            "Set the facial expression shown on the robot's screen. "
            "It also drives the matching head movement. 21 emotions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"emotion": {"type": "string"}},
            "required": ["emotion"],
        },
    },
    {
        "name": "self.robot.set_idle_animation",
        "description": (
            "Enable or disable the robot's idle fidget animation. Disabled by default."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
    },
    {
        "name": "self.pomodoro.show",
        "description": (
            "Render the pomodoro countdown screen on the OLED. phase is one of "
            "focus, short_break, long_break, idle; idle dismisses the screen."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "phase": {"type": "string"},
                "paused": {"type": "boolean", "default": False},
                "remaining_s": {"type": "integer", "default": 0, "minimum": 0, "maximum": 86400},
                "total_s": {"type": "integer", "default": 0, "minimum": 0, "maximum": 86400},
                "round": {"type": "integer", "default": 0, "minimum": 0, "maximum": 99},
                "total_rounds": {"type": "integer", "default": 4, "minimum": 1, "maximum": 99},
            },
            "required": ["phase"],
        },
    },
]

POMODORO_PHASES = ("focus", "short_break", "long_break", "idle")


def build_tools_list() -> List[Dict[str, Any]]:
    """固件 tools/list 的应答内容（深拷贝，调用方改不坏模板）。"""
    return copy.deepcopy(_TOOLS)


def normalize_server_url(value: Optional[str]) -> str:
    """把 ``127.0.0.1:8000`` / ``http://host`` / 完整 ws URL 统一成设备连接地址。"""
    text = (value or "").strip()
    if not text:
        return DEFAULT_SERVER
    if re.match(r"^https?://", text):
        text = "ws" + text[4:]
    elif not re.match(r"^wss?://", text):
        text = "ws://" + text
    parsed = urlsplit(text)
    netloc = parsed.netloc
    if netloc and ":" not in netloc:
        netloc = f"{netloc}:{DEFAULT_WS_PORT}"
    path = parsed.path
    if path in ("", "/"):
        path = "/xiaozhi/v1/"
    return urlunsplit((parsed.scheme, netloc, path, parsed.query, ""))


def _default_log(line: str) -> None:
    print(line, flush=True)


def _short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class MockDevice:
    """一台按固件契约说话的假机器人。"""

    def __init__(
        self,
        server: str = DEFAULT_SERVER,
        device_id: str = DEFAULT_DEVICE_ID,
        duration: Optional[float] = None,
        ping_interval: float = PING_INTERVAL_SECONDS,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.server = normalize_server_url(server)
        self.device_id = device_id
        self.duration = duration if duration and duration > 0 else None
        self.ping_interval = ping_interval
        self.client_id = f"mock-{uuid.uuid4()}"
        self.session_id: Optional[str] = None
        self.audio_frames = 0
        self.tool_calls: List[Dict[str, Any]] = []
        self._log_sink = log or _default_log

    # ── 日志 ────────────────────────────────────────────────

    def _log(self, line: str) -> None:
        self._log_sink(f"{time.strftime('%H:%M:%S')} {LOG_PREFIX} {line}")

    # ── 报文构造 ────────────────────────────────────────────

    def headers(self) -> Dict[str, str]:
        return {
            "Device-Id": self.device_id,
            "Client-Id": self.client_id,
            "Protocol-Version": "1",
        }

    def hello_message(self) -> Dict[str, Any]:
        """照抄 WebsocketProtocol::GetHelloMessage()，少一个字段服务端就走别的分支。"""
        return {
            "type": "hello",
            "version": 1,
            "features": {"mcp": True},
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }

    # ── MCP ────────────────────────────────────────────────

    @staticmethod
    def _result(msg_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"message": message}}

    def handle_mcp_payload(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """处理服务端下发的 MCP 请求，返回要回给服务端的 payload。

        只有 ``method`` 报文需要应答；服务端极少反过来发 result/error，收到就只记一笔。
        """
        if not isinstance(payload, dict):
            return None
        method = payload.get("method")
        if not method:
            self._log(f"MCP 非请求报文 | {_short(payload)}")
            return None

        msg_id = payload.get("id", 0)

        if method == "initialize":
            self._log("MCP initialize | 报上 serverInfo，等服务端要工具列表")
            return self._result(
                msg_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": dict(SERVER_INFO),
                },
            )

        if method == "tools/list":
            tools = build_tools_list()
            self._log(
                "MCP tools/list | 注册 "
                + ", ".join(tool["name"] for tool in tools)
            )
            # 一次回全，固件的分页 nextCursor 在只有 4 个工具时也用不上
            return self._result(msg_id, {"tools": tools, "nextCursor": ""})

        if method == "tools/call":
            params = payload.get("params")
            if not isinstance(params, dict):
                return self._error(msg_id, "Missing params")
            name = params.get("name")
            if not isinstance(name, str):
                return self._error(msg_id, "Missing name")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return self._error(msg_id, "Invalid arguments")
            return self._call_tool(msg_id, name, arguments)

        self._log(f"MCP 未实现的方法 | {method}")
        return self._error(msg_id, f"Method not implemented: {method}")

    def _call_tool(
        self, msg_id: Any, name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        tool = next((t for t in _TOOLS if t["name"] == name), None)
        if tool is None:
            self._log(f"MCP tools/call | 未知工具 {name}")
            return self._error(msg_id, f"Unknown tool: {name}")

        for required in tool["inputSchema"]["required"]:
            if required not in arguments:
                self._log(f"MCP tools/call | {name} 缺参数 {required}")
                return self._error(msg_id, f"Missing valid argument: {required}")

        if name == "self.pomodoro.show":
            phase = arguments.get("phase")
            if phase not in POMODORO_PHASES:
                self._log(f"MCP tools/call | {name} 相位非法 {phase}")
                return self._error(msg_id, f"Unknown phase: {phase}")

        self.tool_calls.append({"name": name, "arguments": arguments})
        self._log(f"MCP tools/call | {name} {_short(arguments)} → 已表演")
        self._log(f"  设备表现：{self._describe_performance(name, arguments)}")
        # 固件 McpTool::Call() 对 bool 返回值统一包成 text "true"
        return self._result(
            msg_id, {"content": [{"type": "text", "text": "true"}], "isError": False}
        )

    @staticmethod
    def _describe_performance(name: str, arguments: Dict[str, Any]) -> str:
        """把工具调用翻译成"真机上会看到什么"，方便对着日志判断链路对不对。"""
        if name == "self.robot.play_action":
            return f"云台执行动作 {arguments.get('action')}"
        if name == "self.robot.set_emotion":
            return f"屏幕切到表情 {arguments.get('emotion')}（会联动舵机）"
        if name == "self.robot.set_idle_animation":
            return "随机空闲动画 " + ("开" if arguments.get("enabled") else "关")
        if name == "self.pomodoro.show":
            phase = arguments.get("phase")
            if phase == "idle":
                return "退出番茄钟画面，回到脸"
            return (
                f"番茄钟画面 {phase} 剩 {arguments.get('remaining_s', 0)}s / "
                f"{arguments.get('total_s', 0)}s，第 {arguments.get('round', 0)}/"
                f"{arguments.get('total_rounds', 0)} 轮"
                + ("（已暂停）" if arguments.get("paused") else "")
            )
        return "未知工具"

    # ── 下行 ────────────────────────────────────────────────

    def describe_downlink(self, message: Dict[str, Any]) -> str:
        kind = message.get("type")
        if kind == "tts":
            text = message.get("text")
            line = f"下行 tts | state={message.get('state')}"
            if text:
                line += f" text={_short(text)}"
            return line
        if kind == "stt":
            return f"下行 stt | text={_short(message.get('text', ''))}"
        if kind == "llm":
            return (
                f"下行 llm | emotion={message.get('emotion')} "
                f"text={_short(message.get('text', ''))}"
            )
        if kind == "alert":
            return (
                f"下行 alert | status={message.get('status')} "
                f"emotion={message.get('emotion')} "
                f"silent={bool(message.get('silent'))} "
                f"message={_short(message.get('message', ''))}"
            )
        if kind == "system":
            return f"下行 system | command={message.get('command')}"
        return f"下行 {kind} | {_short(message)}"

    async def _handle_text(self, websocket, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            # 服务端对没带 device-id 的连接会回一句纯文本提示
            self._log(f"下行 非 JSON 文本 | {_short(raw)}")
            return
        if not isinstance(message, dict):
            self._log(f"下行 非对象报文 | {_short(message)}")
            return

        kind = message.get("type")
        if kind == "hello":
            self.session_id = message.get("session_id")
            sample_rate = (message.get("audio_params") or {}).get("sample_rate")
            self._log(
                f"握手完成 | session_id={self.session_id} 服务端采样率={sample_rate}"
            )
            return
        if kind == "pong":
            self._log("下行 pong | 保活正常")
            return
        if kind == "mcp":
            reply = self.handle_mcp_payload(message.get("payload") or {})
            if reply is not None:
                await websocket.send(json.dumps({"type": "mcp", "payload": reply}))
            return
        self._log(self.describe_downlink(message))

    async def _read_loop(self, websocket) -> None:
        async for raw in websocket:
            if isinstance(raw, (bytes, bytearray)):
                self.audio_frames += 1
                if self.audio_frames == 1 or self.audio_frames % 50 == 0:
                    self._log(f"下行 audio | 累计 {self.audio_frames} 个 Opus 帧")
                continue
            await self._handle_text(websocket, raw)

    async def _ping_loop(self, websocket) -> None:
        while True:
            await asyncio.sleep(self.ping_interval)
            await websocket.send(json.dumps({"type": "ping"}))

    async def run(self) -> None:
        self._log(f"连接 {self.server}")
        self._log(f"device-id={self.device_id} client-id={self.client_id}")
        # 协议级 ping 关掉：真机靠应用层 {"type":"ping"} 保活，这里保持一致
        async with websockets.connect(
            self.server,
            additional_headers=self.headers(),
            ping_interval=None,
            open_timeout=10,
            close_timeout=5,
        ) as websocket:
            await websocket.send(json.dumps(self.hello_message()))
            self._log("已发出 hello，等待服务端欢迎消息")

            reader = asyncio.create_task(self._read_loop(websocket))
            pinger = asyncio.create_task(self._ping_loop(websocket))
            try:
                if self.duration is None:
                    await reader
                else:
                    done, _ = await asyncio.wait({reader}, timeout=self.duration)
                    if not done:
                        self._log(f"已运行 {self.duration:g} 秒，主动断开")
                    else:
                        # reader 没等到 duration 就先结束了（多半是连接被服务端断开）：
                        # 取一次 result() 让里面的异常上抛，不能假装正常退出
                        reader.result()
            finally:
                for task in (reader, pinger):
                    task.cancel()
                await asyncio.gather(reader, pinger, return_exceptions=True)
        self._log(
            f"连接已关闭 | 收到工具调用 {len(self.tool_calls)} 次，"
            f"音频帧 {self.audio_frames} 个"
        )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="模拟 ESP32-S3 设备连到 Server，用于无真机联调（不是真机）",
    )
    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=f"Server WebSocket 地址，可只写 host:port，默认 {DEFAULT_SERVER}",
    )
    parser.add_argument(
        "--device-id",
        default=DEFAULT_DEVICE_ID,
        help=(
            f"上报的 device-id，默认 {DEFAULT_DEVICE_ID}。"
            "刻意不读 DESKPET_DEVICE_ID：拿真机号连上会把真机从设备表里挤掉"
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="挂多少秒后自动干净断开，不传则一直挂到 Ctrl-C",
    )
    parser.add_argument(
        "--ping-interval",
        type=float,
        default=PING_INTERVAL_SECONDS,
        help=f"应用层 ping 间隔秒数，默认 {PING_INTERVAL_SECONDS:g}（与固件一致）",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    device = MockDevice(
        server=args.server,
        device_id=args.device_id,
        duration=args.duration,
        ping_interval=args.ping_interval,
    )
    try:
        asyncio.run(device.run())
    except KeyboardInterrupt:
        print(f"{LOG_PREFIX} 收到中断，已断开", flush=True)
    except (OSError, asyncio.TimeoutError) as exc:
        print(f"{LOG_PREFIX} 连不上 {device.server}：{exc}", file=sys.stderr, flush=True)
        print(
            f"{LOG_PREFIX} 先确认 Server 在跑：./gongban status",
            file=sys.stderr,
            flush=True,
        )
        return 1
    except websockets.exceptions.WebSocketException as exc:
        print(f"{LOG_PREFIX} 握手失败：{exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
