import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__

WAKE_WORD_ABORT_REASON = "wake_word_detected"


async def handleAbortMessage(conn: "ConnectionHandler", reason: str = ""):
    conn.logger.bind(tag=TAG).info("Abort message received")
    # 设置成打断状态，会自动打断llm、tts任务
    conn.close_after_chat = False
    conn.client_abort = True
    conn.clear_queues()
    # 打断客户端说话状态
    await conn.websocket.send(
        json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id})
    )
    conn.clearSpeakStatus()
    if reason == WAKE_WORD_ABORT_REASON:
        _reopen_dialogue_gate(conn)
    conn.logger.bind(tag=TAG).info("Abort message received-end")


def _reopen_dialogue_gate(conn) -> None:
    """说话态被唤醒词打断后重开对话窗口门。

    固件在说话态检测到唤醒词会上行 abort（application.cc:678-679 ->
    protocol.cc:42-49），此前服务端只处理打断本身，不碰对话门——
    dialogue_gate.enabled=true 时用户打断成功后接着说的话会被
    dialogue_gate.py 的 allow() 静默丢弃，打断等于白打断。

    gate 的取用方式与开窗时长口径与 receiveAudioHandle.py 的 detect
    通道（唤醒词路径）一致：都用 DialogueGate(conn.config) 现取现建，
    时长由配置的 window_seconds 决定，这里不重复定义。conn 拿不到可用
    的门配置或门未启用时静默跳过，风格与 pushHandle.py 的
    「播报后开窗」失败处理一致。
    """
    try:
        from core.dialogue_gate import DialogueGate

        gate = DialogueGate(conn.config)
        if gate.enabled:
            gate.open(conn, "唤醒词打断")
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"打断后重开对话窗口失败: {e}")
