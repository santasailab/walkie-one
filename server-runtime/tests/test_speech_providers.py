"""Provider boundaries: independent selection, format, bounds and safe failures."""

import json
import sys
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from speech import (
    MAX_AUDIO_BYTES,
    MAX_SYNTHESIZED_BYTES,
    AzureSpeechAdapter,
    ConfiguredSpeech,
    DeepgramSpeechAdapter,
    SpeechError,
    _NoSpeechRedirect,
    configured_speech,
)


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.body[:limit]


def test_independent_selection_does_not_construct_unselected_provider(monkeypatch):
    adapter = object()
    monkeypatch.setenv("WALKIEONE_STT_PROVIDER", "deepgram")
    monkeypatch.setenv("WALKIEONE_TTS_PROVIDER", "deepgram")
    monkeypatch.setattr(DeepgramSpeechAdapter, "from_environment", lambda: adapter)
    monkeypatch.setattr(
        AzureSpeechAdapter, "from_environment", lambda: pytest.fail("Azure accessed")
    )
    speech = configured_speech()
    assert speech.recognition is speech.synthesis is adapter


def test_recognition_and_synthesis_can_use_different_providers(monkeypatch):
    first, second = object(), object()
    monkeypatch.setenv("WALKIEONE_STT_PROVIDER", "azure")
    monkeypatch.setenv("WALKIEONE_TTS_PROVIDER", "deepgram")
    monkeypatch.setattr(AzureSpeechAdapter, "from_environment", lambda: first)
    monkeypatch.setattr(DeepgramSpeechAdapter, "from_environment", lambda: second)
    speech = configured_speech()
    assert speech.recognition is first
    assert speech.synthesis is second


def test_unknown_or_missing_configuration_never_falls_back(monkeypatch):
    monkeypatch.setenv("WALKIEONE_STT_PROVIDER", "not-a-provider")
    with pytest.raises(SpeechError, match="Unknown"):
        configured_speech()
    monkeypatch.setenv("WALKIEONE_STT_PROVIDER", "deepgram")
    monkeypatch.setenv("WALKIEONE_TTS_PROVIDER", "deepgram")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(SpeechError, match="API key"):
        configured_speech()


def test_deepgram_wire_contract_and_legacy_voice():
    calls = []
    mp3 = b"\xff\xf3\x64\xc4" + bytes(140)

    def opened(request, timeout):
        calls.append(request)
        assert timeout == 15
        assert request.headers["Authorization"] == "Token example-not-a-real-key"
        query = parse_qs(urlsplit(request.full_url).query)
        assert query["mip_opt_out"] == ["true"]
        if "/listen?" in request.full_url:
            assert query["sample_rate"] == ["24000"]
            assert query["channels"] == ["1"]
            assert query["encoding"] == ["linear16"]
            assert request.data == b"\0\0" * 100
            return Response(
                json.dumps(
                    {
                        "results": {
                            "channels": [{"alternatives": [{"transcript": " Hello. "}]}]
                        }
                    }
                ).encode()
            )
        assert query["encoding"] == ["mp3"]
        assert query["bit_rate"] == ["48000"]
        assert query["model"] == ["aura-2-thalia-en"]
        assert json.loads(request.data) == {"text": "Hello."}
        return Response(mp3)

    adapter = DeepgramSpeechAdapter("example-not-a-real-key", opener=opened)
    speech = ConfiguredSpeech(adapter, adapter)
    assert speech.transcribe(b"\0\0" * 100) == "Hello."
    assert speech.synthesize("Hello.", "en-US-Ava:DragonHDLatestNeural") == mp3
    assert len(calls) == 2


@pytest.mark.parametrize("body", [b"{}", b"not json", b'{"results":null}'])
def test_malformed_transcript_is_safe(body):
    adapter = DeepgramSpeechAdapter("example", opener=lambda *a, **kw: Response(body))
    with pytest.raises(SpeechError, match="invalid transcription"):
        adapter.transcribe(b"\0\0")


def test_bounds_before_and_after_network():
    adapter = DeepgramSpeechAdapter(
        "example", opener=lambda *a, **kw: pytest.fail("network called")
    )
    for audio in (b"odd", bytes(MAX_AUDIO_BYTES + 2)):
        with pytest.raises(SpeechError):
            adapter.transcribe(audio)
    assert adapter.transcribe(b"") == ""
    for text in (" ", "a" * 4001):
        with pytest.raises(SpeechError):
            adapter.synthesize(text)
    adapter = DeepgramSpeechAdapter(
        "example", opener=lambda *a, **kw: Response(bytes(MAX_SYNTHESIZED_BYTES + 1))
    )
    with pytest.raises(SpeechError, match="bound"):
        adapter.synthesize("Hello")


def test_provider_failure_does_not_include_response_or_key():
    def failed(*a, **kw):
        raise urllib.error.URLError("secret provider body and API key")

    adapter = DeepgramSpeechAdapter("example", opener=failed)
    with pytest.raises(SpeechError) as caught:
        adapter.transcribe(b"\0\0")
    assert str(caught.value) == "Deepgram request failed"
    with pytest.raises(SpeechError, match="redirects"):
        _NoSpeechRedirect().redirect_request(
            None, None, 302, "", {}, "https://other.invalid"
        )


def test_owner_hosted_speech_contract():
    from speech import HTTPSpeechAdapter

    requests = []

    def opened(request, timeout):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer owner-example"
        assert timeout == 15
        if request.full_url.endswith("/transcribe"):
            assert request.data == b"\0\0"
            return Response(b'{"text":"Try my own assistant."}')
        assert json.loads(request.data) == {"text": "Done.", "voice": None}
        return Response(b"\xff\xf3\x64\xc4" + bytes(140))

    adapter = HTTPSpeechAdapter(
        "https://speech.example.org/v1", "owner-example", opener=opened
    )
    assert adapter.transcribe(b"\0\0") == "Try my own assistant."
    assert adapter.synthesize("Done.")[:2] == b"\xff\xf3"
    assert len(requests) == 2
    for url in (
        "http://speech.example.org",
        "https://user:pw@speech.example.org",
        "https://speech.example.org?key=secret",
    ):
        with pytest.raises(SpeechError):
            HTTPSpeechAdapter(url, "owner-example")
