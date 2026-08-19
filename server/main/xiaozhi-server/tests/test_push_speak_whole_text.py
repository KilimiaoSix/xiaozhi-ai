"""推送播报的整句合成。

真机现象（2026-08-19 21:xx 演示彩排）：迎接语「早上好，今天也一起把事情搞定吧。」
念完「早上好」之后要停好几秒才接下半句。

机理：TTS 首句为了压低首字延迟，会在 `first_sentence_punctuations`（含逗号）处提前
切一刀，于是这条本来完整的文案被拆成两次 Edge TTS 请求串行合成。LLM 流式回答时这
是对的（文本还没生成完，先出半句更快），但推送播报的文本在入队时就已经完整，拆开
只换来多一次网络往返——代理 + 热点下实测每次请求首字节 3.1~3.6s，全落在两句之间。
"""

import pytest

from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.providers.tts.edge import TTSProvider

GREETING = "早上好，今天也一起把事情搞定吧。"


def _provider():
    return TTSProvider(
        {"voice": "zh-CN-XiaoxiaoNeural", "output_dir": "tmp/"},
        delete_audio_file=True,
    )


def _segments(provider, text, *, whole_text):
    """把一句话按生产链路喂进去，收集实际会发起几次合成、每次合成什么。"""
    import re

    provider._begin_sentence(
        TTSMessageDTO(
            sentence_id="s1",
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
            whole_text=whole_text,
        )
    )
    produced = []
    for seg in re.split(r"([。！？!?；;\n])", text):
        provider.tts_text_buff.append(seg)
        got = provider._get_segment_text()
        if got:
            produced.append(got)
    provider.tts_stop_request = True
    got = provider._get_segment_text()
    if got:
        produced.append(got)
    return produced


def test_streaming_sentence_still_splits_at_comma():
    """LLM 流式回答的首句仍要在逗号处提前出声，别把上游的低延迟优化改坏了。"""
    assert _segments(_provider(), GREETING, whole_text=False) == [
        "早上好",
        "今天也一起把事情搞定吧",
    ]


def test_whole_text_sentence_synthesizes_once():
    assert _segments(_provider(), GREETING, whole_text=True) == [
        "早上好，今天也一起把事情搞定吧"
    ]


def test_dto_defaults_to_streaming_behaviour():
    dto = TTSMessageDTO(
        sentence_id="s1",
        sentence_type=SentenceType.FIRST,
        content_type=ContentType.ACTION,
    )
    assert dto.whole_text is False


@pytest.mark.asyncio
async def test_speak_on_device_marks_sentence_as_whole_text():
    from core.handle import pushHandle

    enqueued = []

    class FakeQueue:
        def put(self, message):
            enqueued.append(message)

    class FakeTTS:
        def __init__(self):
            self.tts_text_queue = FakeQueue()
            self.spoken = []

        def store_tts_text(self, sentence_id, text):
            pass

        def tts_one_sentence(self, conn, content_type, content_detail=None):
            self.spoken.append(content_detail)

    class FakeConn:
        def __init__(self):
            self.tts = FakeTTS()
            self.client_abort = True
            self.sentence_id = None
            self.client_is_speaking = False

    conn = FakeConn()

    async def fake_send_tts_message(_conn, _state):
        return None

    import core.handle.sendAudioHandle as sendAudioHandle

    original = sendAudioHandle.send_tts_message
    sendAudioHandle.send_tts_message = fake_send_tts_message
    try:
        assert await pushHandle.speak_on_device(conn, GREETING) is True
    finally:
        sendAudioHandle.send_tts_message = original

    first = [m for m in enqueued if m.sentence_type is SentenceType.FIRST]
    assert len(first) == 1
    assert first[0].whole_text is True
