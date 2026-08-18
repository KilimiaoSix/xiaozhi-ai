"""把外部工作事件主动推送给常连设备。

固件对下行 alert 的处理见 application.cc: Alert(status, message, emotion, OGG_VIBRATION)，
一条消息即可同时驱动 状态栏 / 屏显文字 / 表情(emoji 板会联动舵机动作) / 提示音，
且设备停留在 Idle，不会打开麦克风。

speak=True 时额外走一次 TTS 播报。注意固件收到 tts.stop 后，若当前不是
kListeningModeManualStop 会切到 Listening（麦克风打开等待回话），所以默认不播报。
"""
import json
import uuid

TAG = __name__

DEFAULT_EMOTION = "neutral"
DEFAULT_STATUS = "通知"


async def push_alert_to_device(conn, text: str, emotion: str = DEFAULT_EMOTION,
                               status: str = DEFAULT_STATUS) -> None:
    """下发一条 alert。三个字段必须都是字符串，否则固件会直接丢弃。"""
    message = {
        "type": "alert",
        "status": str(status or DEFAULT_STATUS),
        "message": str(text),
        "emotion": str(emotion or DEFAULT_EMOTION),
        "session_id": getattr(conn, "session_id", ""),
    }
    await conn.websocket.send(json.dumps(message))


async def speak_on_device(conn, text: str) -> bool:
    """让设备把文字念出来，复用会话内既有的 TTS 队列。

    TTS 尚未初始化时返回 False，由调用方决定是否降级。
    """
    tts = getattr(conn, "tts", None)
    if tts is None:
        return False

    # 重依赖（opus 等）只在真正播报时才加载，保证 alert 路径足够轻
    from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO

    conn.client_abort = False
    conn.sentence_id = uuid.uuid4().hex
    tts.store_tts_text(conn.sentence_id, text)
    tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)
    tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )
    return True


async def push_work_event(conn, text: str, emotion: str = DEFAULT_EMOTION,
                          status: str = DEFAULT_STATUS, speak: bool = False) -> bool:
    """推送一条工作事件，返回是否完成了语音播报。"""
    await push_alert_to_device(conn, text, emotion, status)
    if not speak:
        return False
    try:
        return await speak_on_device(conn, text)
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"推送事件语音播报失败，已降级为仅提示: {e}")
        return False
