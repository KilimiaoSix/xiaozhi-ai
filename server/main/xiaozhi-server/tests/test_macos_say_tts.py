"""本地 TTS 兜底 provider（macOS say）。

存在意义见 core/providers/tts/macos_say.py 的模块注释：EdgeTTS 依赖外网，
DNS 或墙一出问题机器人就一句话说不出来（2026-09-02 热点 DNS 故障实证）。
"""
import asyncio
import os
import sys
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.skipif(
    not os.path.exists("/usr/bin/say"), reason="仅 macOS 提供 say"
)


def _provider(tmp_path, **overrides):
    from core.providers.tts.macos_say import TTSProvider

    config = {"voice": "Tingting", "output_dir": str(tmp_path), "sample_rate": 16000}
    config.update(overrides)
    return TTSProvider(config, delete_audio_file=True)


def test_synthesizes_a_playable_wav_without_network(tmp_path):
    provider = _provider(tmp_path)
    target = str(tmp_path / "out.wav")

    asyncio.run(provider.text_to_speak("你好，我是小智", target))

    assert os.path.getsize(target) > 0
    with wave.open(target, "rb") as wav:
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        # 老式拼接合成也该有半秒以上，太短说明音色是"只登记未下载"的空壳
        assert wav.getnframes() / wav.getframerate() > 0.5


def test_returns_audio_bytes_when_no_output_file(tmp_path):
    provider = _provider(tmp_path)

    data = asyncio.run(provider.text_to_speak("测试", None))

    assert isinstance(data, bytes) and data.startswith(b"RIFF")


def test_speech_rate_maps_into_words_per_minute(tmp_path):
    assert _provider(tmp_path, rate="0").wpm == 180
    assert _provider(tmp_path, rate="50").wpm == 270
    # 夹紧上下限，避免配置写飞了让语音快到听不清
    assert _provider(tmp_path, rate="500").wpm == 360
    assert _provider(tmp_path, rate="-500").wpm == 90


def test_missing_say_binary_fails_loudly(tmp_path, monkeypatch):
    from core.providers.tts import macos_say

    monkeypatch.setattr(macos_say.os.path, "exists", lambda p: False)
    with pytest.raises(RuntimeError, match="本地语音合成不可用"):
        _provider(tmp_path)


def test_undownloaded_voice_is_rejected_instead_of_pushing_silence(tmp_path):
    # say 对未下载的音色与不存在的音色名都退出码 0 地产出约 0.14s 近静音，
    # provider 必须识破，否则机器人"张嘴没声"。
    provider = _provider(tmp_path, voice="NoSuchVoice__")
    with pytest.raises(Exception, match="近静音"):
        asyncio.run(provider.text_to_speak("你好世界", str(tmp_path / "x.wav")))
