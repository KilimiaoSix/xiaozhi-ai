"""EdgeTTS 代理解析。

edge_tts 自建 aiohttp session（trust_env=False），系统代理环境变量对它无效。
直连 speech.platform.bing.com 被重置的网络下，不显式传 proxy 就整条 TTS 链路失败。
"""

import pytest

from core.providers.tts.edge import TTSProvider


def _make(config=None, env=None, monkeypatch=None):
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    base = {"voice": "zh-CN-XiaoxiaoNeural", "output_dir": "tmp/"}
    base.update(config or {})
    return TTSProvider(base, delete_audio_file=True)


def test_proxy_defaults_to_none_without_env(monkeypatch):
    assert _make(monkeypatch=monkeypatch).proxy is None


@pytest.mark.parametrize(
    "key", ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"]
)
def test_proxy_read_from_env(key, monkeypatch):
    provider = _make(env={key: "http://127.0.0.1:10910"}, monkeypatch=monkeypatch)
    assert provider.proxy == "http://127.0.0.1:10910"


def test_https_proxy_wins_over_http_proxy(monkeypatch):
    provider = _make(
        env={"HTTP_PROXY": "http://plain:1", "HTTPS_PROXY": "http://tls:2"},
        monkeypatch=monkeypatch,
    )
    assert provider.proxy == "http://tls:2"


def test_config_overrides_env(monkeypatch):
    provider = _make(
        config={"proxy": "http://from-config:3"},
        env={"HTTPS_PROXY": "http://from-env:4"},
        monkeypatch=monkeypatch,
    )
    assert provider.proxy == "http://from-config:3"
