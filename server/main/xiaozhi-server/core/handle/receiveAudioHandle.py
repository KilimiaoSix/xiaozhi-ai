import time
import json
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.util import audio_to_data
from core.handle.abortHandle import handleAbortMessage
from core.handle.intentHandler import handle_user_intent
from core.utils.output_counter import check_device_output_limit
from core.handle.sendAudioHandle import send_stt_message, SentenceType

TAG = __name__


async def handleAudioMessage(conn: "ConnectionHandler", pcm_frame):
    # 当前片段是否有人说话
    have_voice = conn.vad.is_vad(conn, pcm_frame)
    # 如果设备刚刚被唤醒，短暂忽略VAD检测
    if hasattr(conn, "just_woken_up") and conn.just_woken_up:
        have_voice = False
        # 设置一个短暂延迟后恢复VAD检测
        if not hasattr(conn, "vad_resume_task") or conn.vad_resume_task.done():
            conn.vad_resume_task = asyncio.create_task(resume_vad_detection(conn))
        return
    # 服务端AEC功能需要实时触发打断
    if conn.client_aec and have_voice:
        if conn.client_is_speaking and conn.client_listen_mode != "manual":
            await handleAbortMessage(conn)
    # 设备长时间空闲检测，用于say goodbye
    await no_voice_close_connect(conn, have_voice)
    # 接收音频
    await conn.asr.receive_audio(conn, pcm_frame, have_voice)


def wakeup_resume_vad_seconds(config) -> float:
    """唤醒应答后 VAD 静默期的时长；缺配或非法值回退上游原值 2 秒。

    本板（无 AEC）播报期间麦克风不采音，这段静默只需盖住应答的混响尾音。
    上游的 2 秒会把「请讲」之后用户立刻说的第一句整段吞掉——单次对话下
    表现为「唤醒后提问没反应，像是麦克风关了」。
    """
    raw = (config or {}).get("wakeup_resume_vad_seconds", 2.0)
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return 2.0
    return parsed if parsed >= 0 else 2.0


async def resume_vad_detection(conn: "ConnectionHandler"):
    # 静默期过后恢复VAD检测
    await asyncio.sleep(wakeup_resume_vad_seconds(conn.config))
    conn.just_woken_up = False


async def startToChat(conn: "ConnectionHandler", text, source: str = "asr"):
    """把一段文本送进对话链路。

    source 区分这段文本从哪来，决定它要不要过对话窗口门：
      - "detect"：唤醒词 / 手势 / 按键，走 listen detect 通道，是明确的用户主动发起；
      - "system"：结束语这类内部提示，不是用户语音；
      - "asr"（默认）：麦克风拾到的，受门管——固件播完必然自动开麦，
        这条路径会把房间里与设备无关的人声也送进来。
    """
    # 剧本模式：拍摄时把语音输入与 LLM 断开。
    # 每条 speak=true 播完后固件都会自动开麦（tts.stop 之后进 Listening），
    # 演员这时说的任何话都会被 ASR 拾到并触发真 LLM 应答录进素材。
    # 开启后语音只做"设备在听"的画面，实际内容由分镜脚本经 /xiaozhi/event/push 下发。
    if conn.config.get("script_mode", False):
        conn.logger.bind(tag=TAG).info(f"剧本模式已开启，不送 LLM: {text}")
        return

    # 流程四：来访者留言窗口。必须放在对话窗口门之前——访客没唤醒过设备，
    # gate.allow() 会把这句直接丢掉，留言就永远记不上。窗口没开时返回 None，
    # 这句照常往下走。
    from core.visitor_flow import visitor_flow_handle_asr

    # ASR 可能给的是 {"content": "...", "language": ...} 信封（说话人格式的
    # 解析在本函数更靠后的位置才做），留言必须取信封里的纯文本——
    # 真机上出现过把整段 JSON 当留言存进台账、返岗时照本宣科念出来的情况。
    visitor_text = text
    try:
        if text.strip().startswith("{") and text.strip().endswith("}"):
            envelope = json.loads(text)
            if isinstance(envelope, dict) and isinstance(
                envelope.get("content"), str
            ):
                visitor_text = envelope["content"]
    except (json.JSONDecodeError, TypeError):
        pass

    visitor_reply = visitor_flow_handle_asr(conn.device_id, visitor_text)
    if visitor_reply is not None:
        from core.handle.pushHandle import push_work_event

        await push_work_event(
            conn, text=visitor_reply, emotion="happy", status="留言", speak=True
        )
        return

    # 对话窗口门：只有用户主动发起过，麦克风拾到的语音才进 LLM。
    # 见 core/dialogue_gate.py 的说明——固件每条播报后必然自动开麦，
    # 没有这道门，房间人声会把设备拖进自激循环。
    from core.dialogue_gate import DialogueGate

    gate = DialogueGate(conn.config)
    if gate.enabled and source != "system":
        if source == "detect":
            gate.open(conn, "用户主动发起")
        elif not gate.allow(conn, text):
            return

    # 检查输入是否是JSON格式（包含说话人信息）
    speaker_name = None
    actual_text = text

    try:
        # 尝试解析JSON格式的输入
        if text.strip().startswith("{") and text.strip().endswith("}"):
            data = json.loads(text)
            if "speaker" in data and "content" in data:
                speaker_name = data["speaker"]
                actual_content = data["content"]
                conn.logger.bind(tag=TAG).info(f"解析到说话人信息: {speaker_name}")

                # 仅在该说话人首次出现时保留 {"speaker":...} JSON，让模型自然称呼一次；
                # 后续轮降为纯文本，避免每轮重复出现名字诱导模型反复称呼
                if speaker_name not in conn.introduced_speakers:
                    conn.introduced_speakers.add(speaker_name)
                    actual_text = text
                else:
                    actual_text = actual_content
    except (json.JSONDecodeError, KeyError):
        # 如果解析失败，继续使用原始文本
        pass

    # 保存说话人信息到连接对象
    if speaker_name:
        conn.current_speaker = speaker_name
    else:
        conn.current_speaker = None

    if conn.need_bind:
        await check_bind_device(conn)
        return

    # 如果当日的输出字数大于限定的字数
    if conn.max_output_size > 0:
        if check_device_output_limit(
            conn.headers.get("device-id"), conn.max_output_size
        ):
            await max_out_size(conn)
            return

    # manual 模式下不打断正在播放的内容
    if conn.client_is_speaking and conn.client_listen_mode != "manual":
        await handleAbortMessage(conn)

    # 首先进行意图分析，使用实际文本内容
    intent_handled = await handle_user_intent(conn, actual_text)

    if intent_handled:
        # 如果意图已被处理，不再进行聊天
        return

    # 意图未被处理，继续常规聊天流程，使用实际文本内容
    await send_stt_message(conn, actual_text)

    # 准备开始新会话
    conn.client_abort = False

    conn.executor.submit(conn.chat, actual_text)


async def no_voice_close_connect(conn: "ConnectionHandler", have_voice):
    if have_voice:
        conn.last_activity_time = time.time() * 1000
        return
    # 只有在已经初始化过时间戳的情况下才进行超时检查
    if conn.last_activity_time > 0.0:
        no_voice_time = time.time() * 1000 - conn.last_activity_time
        close_connection_no_voice_time = int(
            conn.config.get("close_connection_no_voice_time", 120)
        )
        if (
            not conn.close_after_chat
            and no_voice_time > 1000 * close_connection_no_voice_time
        ):
            conn.close_after_chat = True
            conn.client_abort = False
            end_prompt = conn.config.get("end_prompt", {})
            if end_prompt and end_prompt.get("enable", True) is False:
                conn.logger.bind(tag=TAG).info("结束对话，无需发送结束提示语")
                await conn.close()
                return
            prompt = end_prompt.get("prompt")
            if not prompt:
                prompt = "请你以```时间过得真快```未来头，用富有感情、依依不舍的话来结束这场对话吧。！"
            await startToChat(conn, prompt, source="system")


async def max_out_size(conn: "ConnectionHandler"):
    # 播放超出最大输出字数的提示
    conn.client_abort = False
    text = "不好意思，我现在有点事情要忙，明天这个时候我们再聊，约好了哦！明天不见不散，拜拜！"
    await send_stt_message(conn, text)
    file_path = "config/assets/max_output_size.wav"
    opus_packets = await audio_to_data(file_path)
    conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
    conn.close_after_chat = True


async def check_bind_device(conn: "ConnectionHandler"):
    if conn.bind_code:
        # 确保bind_code是6位数字
        if len(conn.bind_code) != 6:
            conn.logger.bind(tag=TAG).error(f"无效的绑定码格式: {conn.bind_code}")
            text = "绑定码格式错误，请检查配置。"
            await send_stt_message(conn, text)
            return

        text = f"请登录控制面板，输入{conn.bind_code}，绑定设备。"
        await send_stt_message(conn, text)

        # 播放提示音
        music_path = "config/assets/bind_code.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.FIRST, opus_packets, text))

        # 逐个播放数字
        for i in range(6):  # 确保只播放6位数字
            try:
                digit = conn.bind_code[i]
                num_path = f"config/assets/bind_code/{digit}.wav"
                num_packets = await audio_to_data(num_path)
                conn.tts.tts_audio_queue.put((SentenceType.MIDDLE, num_packets, None))
            except Exception as e:
                conn.logger.bind(tag=TAG).error(f"播放数字音频失败: {e}")
                continue
        conn.tts.tts_audio_queue.put((SentenceType.LAST, [], None))
    else:
        # 播放未绑定提示
        conn.client_abort = False
        text = f"没有找到该设备的版本信息，请正确配置 OTA地址，然后重新编译固件。"
        await send_stt_message(conn, text)
        music_path = "config/assets/bind_not_found.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
