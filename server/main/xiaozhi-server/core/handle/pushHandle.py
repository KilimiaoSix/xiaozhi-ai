"""把外部工作事件主动推送给常连设备。

固件对下行 alert 的处理见 application.cc: Alert(status, message, emotion, OGG_VIBRATION)，
一条消息即可同时驱动 状态栏 / 屏显文字 / 表情(emoji 板会联动舵机动作) / 提示音，
且设备停留在 Idle，不会打开麦克风。

speak=True 时额外走一次 TTS 播报。注意固件收到 tts.stop 后，若当前不是
kListeningModeManualStop 会切到 Listening（麦克风打开等待回话），所以默认不播报。
"""
import asyncio
import json
import uuid
from typing import Any, Dict, Optional

TAG = __name__

DEFAULT_EMOTION = "neutral"
DEFAULT_STATUS = "通知"

# 设备的"基态"——事件播完之后该回到的常驻画面。
# 离席看家这类场景需要持续维持一个画面，而 alert 是覆盖式的，
# 任何路过的瞬时事件都会把它永久冲掉，所以事件播完要能自动恢复。
DEFAULT_BASE_STATE = {"status": "待机", "message": "", "emotion": "neutral"}

# 按 device_id 存，不挂在 conn 上：固件断线 10s 就重连、conn 会被换掉，
# 挂 conn 上的状态在 WiFi 抖动时会静默丢失。
_base_states: Dict[str, Dict[str, str]] = {}
_restore_tasks: Dict[str, Any] = {}


def set_base_state(device_id: str, status: str, message: str, emotion: str) -> None:
    """设定设备的常驻基态。"""
    if not device_id:
        return
    _base_states[device_id] = {
        "status": str(status or DEFAULT_BASE_STATE["status"]),
        "message": str(message or ""),
        "emotion": str(emotion or DEFAULT_BASE_STATE["emotion"]),
    }


def get_base_state(device_id: str) -> Dict[str, str]:
    """读取基态，没设过就是默认待机态。"""
    return dict(_base_states.get(device_id) or DEFAULT_BASE_STATE)


def clear_base_state(device_id: str) -> None:
    """清除自定义基态，回落到默认待机态。"""
    if not device_id:
        return
    _base_states.pop(device_id, None)
    _cancel_pending_restore(device_id)


def _cancel_pending_restore(device_id: str) -> None:
    task = _restore_tasks.pop(device_id, None)
    if task is not None and not task.done():
        task.cancel()


def _resolve_conn(fallback_conn, device_id: str):
    """按 device_id 取当前活跃连接，取不到就退回传入的那个。

    连接注册表挂在 WebSocketServer 上，这里经 conn.server 反查，避免把
    pushHandle 和 websocket_server 绑死。
    """
    server = getattr(fallback_conn, "server", None)
    registry = getattr(server, "device_registry", None)
    if registry is None:
        return fallback_conn
    return registry.get(device_id)


def _schedule_base_state_restore(conn, device_id: str, delay: float) -> None:
    """安排 delay 秒后把设备画面恢复到基态。

    新事件到来时取消上一个待恢复任务，否则旧的恢复会把新事件的画面冲掉。
    """
    _cancel_pending_restore(device_id)

    async def _restore():
        try:
            await asyncio.sleep(delay)
            # 不能用捕获的 conn：固件断线 10s 就重连，会议时长级的 restore_after
            # 期间只要 WiFi 抖一次，这个 conn 就已经是死连接了（见本文件顶部注释）。
            # 恢复时刻按 device_id 重新取当前活跃连接。
            target = _resolve_conn(conn, device_id)
            if target is None:
                conn.logger.bind(tag=TAG).warning(
                    f"设备 {device_id} 已离线，跳过基态恢复"
                )
                return
            base = get_base_state(device_id)
            # 基态恢复一律静音：它是把画面收回去，不是新事件
            await push_alert_to_device(
                target, base["message"], base["emotion"], base["status"], silent=True
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(f"恢复设备基态失败: {e}")
        finally:
            if _restore_tasks.get(device_id) is not None:
                _restore_tasks.pop(device_id, None)

    _restore_tasks[device_id] = asyncio.create_task(_restore())


def device_busy_reason(conn) -> Optional[str]:
    """设备是否忙到不能接受主动播报，忙则返回原因，空闲返回 None。

    推送随时可能到来，而设备的音频通道是独占的：正在播语音时再塞一段会音频交错，
    用户正在说话时插播会污染这一轮的 ASR 与 client_abort 状态。
    """
    if getattr(conn, "tts", None) is None:
        return "TTS 尚未就绪"
    if getattr(conn, "client_is_speaking", False):
        return "设备正在播放语音"
    if getattr(conn, "client_have_voice", False):
        return "用户正在说话"
    return None


def voice_session_active(conn, within_seconds: float, clock=None) -> bool:
    """设备上的语音对话是否活跃：正在播报、正在拾到人声、最近 within_seconds
    秒内说过话，或对话窗口开着（唤醒词刚喊过、还在等问题）。

    presence 休眠链路用它判断「摄像头说没人，但语音说明人还在」。刻意不看
    last_activity_time——那个时间戳被设备 30 秒一条的 ping 心跳刷新，
    安静挂机也永远“活跃”。clock 返回秒（time.time 口径），与
    vad_last_voice_time 的毫秒对齐后比较。
    """
    import time as _time

    if getattr(conn, "client_is_speaking", False):
        return True
    if getattr(conn, "client_have_voice", False):
        return True

    last_voice_ms = getattr(conn, "vad_last_voice_time", 0.0) or 0.0
    if last_voice_ms > 0:
        now_ms = (clock or _time.time)() * 1000
        if now_ms - last_voice_ms < within_seconds * 1000:
            return True

    from core.dialogue_gate import window_open

    return window_open(conn)


# 推送要出声时的等待策略。默认 3 秒：桌面端推送客户端的 HTTP 超时是 5 秒，
# 服务端等待必须明显短于它，否则调用方先超时，等待就白做了。
# 想等更久必须同步放宽调用方超时。
DEFAULT_PUSH_WAIT_SECONDS = 3.0
DEFAULT_PUSH_POLL_INTERVAL = 0.3
BUSY_SPEAKING = "设备正在播放语音"


def _push_speak_settings(config) -> tuple:
    section = (config or {}).get("push_speak") or {}
    if not isinstance(section, dict):
        section = {}

    def positive(value, fallback):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if parsed > 0 else fallback

    return (
        positive(section.get("wait_seconds"), DEFAULT_PUSH_WAIT_SECONDS),
        positive(section.get("poll_interval"), DEFAULT_PUSH_POLL_INTERVAL),
        bool(section.get("preempt_speaking", False)),
    )


async def ensure_speakable(conn, busy_probe=None, sleep=None, clock=None, abort=None):
    """尽力让设备进入可播报状态；返回 None 表示可以播，否则返回未解决的忙因。

    设备的音频通道是独占的，撞上忙态直接放弃会让工作事件的播报经常被吞掉。
    这里改成先等一小会儿——房间人声引起的 VAD 抖动通常一两秒就过去。

    仍然区别对待两种忙因：
      - "用户正在说话"：只等不抢。插播会污染这一轮的 ASR 与 client_abort 状态。
      - "设备正在播放语音"：开启 preempt_speaking 后可以打断它自己的播报，
        等价于用户打断它说话，代价可控。
    """
    import asyncio
    import time as _time

    probe = busy_probe or device_busy_reason
    sleep = sleep or asyncio.sleep
    clock = clock or _time.monotonic
    if abort is None:
        from core.handle.abortHandle import handleAbortMessage as abort

    wait_seconds, poll_interval, preempt = _push_speak_settings(
        getattr(conn, "config", None)
    )

    busy = probe(conn)
    if not busy:
        return None

    deadline = clock() + wait_seconds
    while busy:
        if preempt and busy == BUSY_SPEAKING:
            conn.logger.bind(tag=TAG).info("打断设备当前播报，让位给本次推送")
            await abort(conn)
            busy = probe(conn)
            if not busy:
                return None
        if clock() >= deadline:
            break
        await sleep(poll_interval)
        busy = probe(conn)

    return busy


async def push_alert_to_device(conn, text: str, emotion: str = DEFAULT_EMOTION,
                               status: str = DEFAULT_STATUS,
                               silent: bool = False) -> None:
    """下发一条 alert。三个字段必须都是字符串，否则固件会直接丢弃。

    silent=True 时固件不播提示音——基态恢复这类"收画面"的推送不该每次都响一声。
    """
    message = {
        "type": "alert",
        "status": str(status or DEFAULT_STATUS),
        "message": str(text),
        "emotion": str(emotion or DEFAULT_EMOTION),
        "silent": bool(silent),
        "session_id": getattr(conn, "session_id", ""),
    }
    await conn.websocket.send(json.dumps(message))


# emotion 会连带驱动舵机，不只是画表情。链路是 Application::Alert ->
# EmojiDisplay::SetEmotion -> emotion_controller_->TriggerEmotion ->
# PlayAnimation(HAPPY) -> ExecuteHappyAnimation()，而该动画体内调了
# servo_controller_->HeadUp(15)（emoji_controller.cc:971）；sleepy 同理调 HeadDown。
# 早先这里写的「emotion 不碰舵机」是把追溯停在 PlayAnimation 就下的结论，并不成立。
# 因此想要「抬头 + 笑脸」一条 emotion 就够，不必再叠一个 action——叠了反而让动作队列
# 与动画队列同时抢 ServoController 的互斥锁。需要的是与表情无关的动作时才用下面的工具。
# 注意工具名用下划线版：MCPClient 按 sanitize_tool_name 后的键存查，带点的名字查不到。
ROBOT_ACTION_TOOL = "self_robot_play_action"
IDLE_ANIMATION_TOOL = "self_robot_set_idle_animation"
VALID_ACTIONS = {"nod", "shake", "roll",
                 # 瞥一眼后回中
                 "look_left", "look_right", "look_up", "look_down",
                 # 转过去保持不动（供"转向同事并保持注视"这类镜头）
                 "hold_left", "hold_right", "hold_up", "hold_down",
                 "center"}
ACTION_MAX_ATTEMPTS = 2
ACTION_RETRY_DELAY = 3.0


async def play_action_on_device(conn, action: str) -> bool:
    """让设备做一个头部动作，失败不影响本次推送的其它部分。"""
    if action not in VALID_ACTIONS:
        conn.logger.bind(tag=TAG).warning(f"未知动作，已忽略: {action}")
        return False

    mcp_client = getattr(conn, "mcp_client", None)
    if mcp_client is None:
        conn.logger.bind(tag=TAG).warning("设备未初始化 MCP，无法执行动作")
        return False

    from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool

    # 设备重连后 MCP 客户端要重新初始化，这几秒内调用必失败。
    # 固件断线 10s 就重连，所以重试一次通常就能落地。
    args = json.dumps({"action": action})
    last_error = None
    for attempt in range(ACTION_MAX_ATTEMPTS):
        try:
            await call_mcp_tool(conn, mcp_client, ROBOT_ACTION_TOOL, args, timeout=10)
            conn.logger.bind(tag=TAG).info(
                f"已下发动作: {action}" + (f"（第 {attempt + 1} 次尝试）" if attempt else "")
            )
            return True
        except Exception as e:
            last_error = e
            if attempt < ACTION_MAX_ATTEMPTS - 1:
                conn.logger.bind(tag=TAG).info(f"下发动作 {action} 失败，{ACTION_RETRY_DELAY}s 后重试: {e}")
                await asyncio.sleep(ACTION_RETRY_DELAY)
                mcp_client = getattr(conn, "mcp_client", None)
                if mcp_client is None:
                    break

    conn.logger.bind(tag=TAG).warning(f"下发动作 {action} 最终失败: {last_error}")
    return False


async def set_idle_animation_on_device(conn, enabled: bool) -> bool:
    """开关设备的随机空闲动画（每约 10 秒一次的自发转头）。

    默认关闭，让机器人的每一次动作都是对事件的刻意回应；
    需要"待机显得有生气"的镜头时再打开。
    """
    mcp_client = getattr(conn, "mcp_client", None)
    if mcp_client is None:
        conn.logger.bind(tag=TAG).warning("设备未初始化 MCP，无法开关空闲动画")
        return False

    from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool

    try:
        await call_mcp_tool(conn, mcp_client, IDLE_ANIMATION_TOOL,
                            json.dumps({"enabled": bool(enabled)}), timeout=10)
        conn.logger.bind(tag=TAG).info(f"空闲动画已{'开启' if enabled else '关闭'}")
        return True
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"开关空闲动画失败: {e}")
        return False


async def speak_on_device(conn, text: str) -> bool:
    """让设备把文字念出来，复用会话内既有的 TTS 队列。

    TTS 尚未初始化时返回 False，由调用方决定是否降级。
    """
    tts = getattr(conn, "tts", None)
    if tts is None:
        return False

    # 重依赖（opus 等）只在真正播报时才加载，保证 alert 路径足够轻
    from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
    from core.handle.sendAudioHandle import send_tts_message

    conn.client_abort = False
    conn.sentence_id = uuid.uuid4().hex

    # 关键：设备只有收到 tts.start 才会从 Idle 切到 Speaking，
    # 而 OnIncomingAudio 只在 Speaking 态收包（application.cc:426-429）。
    # 少了这一条，推送的音频会被固件静默丢弃，真机上听不到任何声音。
    conn.client_is_speaking = True
    await send_tts_message(conn, "start")

    tts.store_tts_text(conn.sentence_id, text)
    tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
            # 推送的文案是一次性写好的，不要按 LLM 流式那样在逗号处抢跑切段：
            # 多出来的那次 TTS 往返会在两个半句之间留出好几秒空白
            whole_text=True,
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
                          status: str = DEFAULT_STATUS, speak: bool = False,
                          restore_after: Optional[float] = None,
                          action: Optional[str] = None,
                          idle_animation: Optional[bool] = None,
                          silent: bool = False,
                          open_dialogue: bool = True) -> bool:
    """推送一条工作事件，返回是否完成了语音播报。

    restore_after 传正数时，该秒数之后把画面恢复到设备基态；不传则保持覆盖式行为。
    open_dialogue 决定播报后要不要开对话窗口（等用户顺口应答）：告警、迎接这类
    对话型推送要开；通知型推送（coding agent 事件流水）必须关，否则高频推送
    会把 60 秒窗口顶成常开，环境人声随便进 LLM，单次对话名存实亡。
    """
    await push_alert_to_device(conn, text, emotion, status, silent=silent)

    if idle_animation is not None:
        await set_idle_animation_on_device(conn, idle_animation)

    # 把推送内容写进对话历史，否则用户追问"刚才那个告警怎么回事"时 LLM 毫无上下文。
    # 历史已有截断（dialogue.py DEFAULT_MAX_HISTORY_MESSAGES），不怕撑爆。
    try:
        from core.utils.dialogue import Message

        conn.dialogue.put(Message(role="assistant", content=text))
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"推送内容写入对话历史失败: {e}")

    if action:
        await play_action_on_device(conn, action)

    if restore_after is not None and restore_after > 0:
        device_id = getattr(conn, "device_id", None)
        if device_id:
            _schedule_base_state_restore(conn, device_id, float(restore_after))

    if not speak:
        return False

    busy = await ensure_speakable(conn)
    if busy:
        conn.logger.bind(tag=TAG).info(f"{busy}，本次推送降级为仅提示: {text}")
        return False

    try:
        spoke = await speak_on_device(conn, text)
    except Exception as e:
        conn.client_is_speaking = False
        conn.logger.bind(tag=TAG).warning(f"推送事件语音播报失败，已降级为仅提示: {e}")
        return False

    if spoke and open_dialogue:
        # 机器人主动开口后设备会自动开麦,用户顺口的应答(告警后的"启动诊断"、
        # 迎接后的吩咐)必须能进对话窗口门——门默认只认用户主动唤醒,
        # 这里补上"机器人发起对话"的另一半,真机上告警应答曾被门整句丢弃。
        try:
            from core.dialogue_gate import DialogueGate

            DialogueGate(conn.config).open(conn, reason="robot_spoke_first")
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(f"播报后开对话窗口失败: {e}")
    return spoke
