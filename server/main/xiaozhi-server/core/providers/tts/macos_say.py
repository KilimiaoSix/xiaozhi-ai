import asyncio
import os
import shutil
import uuid
from datetime import datetime

from core.providers.tts.base import TTSProviderBase

# macOS 自带的语音合成，完全本地：不联网、无模型下载、无额外进程。
# 存在的理由是 EdgeTTS 依赖 speech.platform.bing.com，换网络（热点、公司网）
# 就整条哑掉——本地合成让"能不能说话"不再取决于外网可达性。
SAY_BIN = "/usr/bin/say"

# say 的 --data-format：LEI16 = 小端 16 位整型 PCM，与设备侧采样率对齐即可直接解码。
_DATA_FORMAT = "LEI16@{rate}"

# WAV 头固定 44 字节；低于这个时长即判定为空音频（实测空壳音色恒为 0.14s）
_WAV_HEADER_BYTES = 44
_MIN_SPEECH_SECONDS = 0.25


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsRate", "speech_rate", -100, 100, 0, int),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.voice = config.get("private_voice") or config.get("voice") or "Flo"
        self.audio_file_type = "wav"

        # say 的语速单位是词/分钟，默认 180；这里沿用其他 provider 的 -100~100 百分比语义再换算。
        speech_rate = config.get("rate", "0")
        self.speech_rate = int(speech_rate) if speech_rate else 0
        self._apply_percentage_params(config)
        base_wpm = int(config.get("base_wpm", 180) or 180)
        self.wpm = max(90, min(360, int(base_wpm * (1 + self.speech_rate / 100))))

        self.sample_rate = int(config.get("sample_rate", 16000) or 16000)

        if not os.path.exists(SAY_BIN):
            raise RuntimeError(f"本地语音合成不可用：找不到 {SAY_BIN}（仅 macOS 提供）")

    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )

    async def text_to_speak(self, text, output_file):
        target = output_file or self.generate_filename()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        args = [
            SAY_BIN,
            "-v", self.voice,
            "-r", str(self.wpm),
            "-o", target,
            "--data-format", _DATA_FORMAT.format(rate=self.sample_rate),
            text,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            # 合成是本地的，正常在百毫秒级；超时说明 say 卡死，宁可报错也不能挂住 TTS 队列。
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise Exception("本地语音合成超时（say 未在 20 秒内返回）")

        if proc.returncode != 0:
            detail = (stderr or b"").decode(errors="replace").strip()
            raise Exception(f"本地语音合成失败（say 退出码 {proc.returncode}）: {detail}")
        if not os.path.exists(target) or os.path.getsize(target) == 0:
            raise Exception("本地语音合成失败：未产出音频")

        # say 对"系统里登记了、但语音数据没下载"的音色（Flo/Eddy 等新式音色）
        # 以及不存在的音色名，都会退出码 0 地产出约 0.14 秒的近静音——
        # 不拦住就等于给机器人推一段哑音频，现场表现为"它张嘴了却没声"。
        duration = (os.path.getsize(target) - _WAV_HEADER_BYTES) / (self.sample_rate * 2)
        if len(text.strip()) >= 2 and duration < _MIN_SPEECH_SECONDS:
            raise Exception(
                f"本地语音合成失败：音色 {self.voice} 只产出 {duration:.2f}s 近静音"
                "（该音色多半尚未下载，换 Tingting 或在系统设置里下载它）"
            )

        if output_file:
            return None
        # 基类的无文件分支要求返回音频二进制
        with open(target, "rb") as f:
            data = f.read()
        if self.delete_audio_file:
            try:
                os.remove(target)
            except OSError:
                pass
        return data
