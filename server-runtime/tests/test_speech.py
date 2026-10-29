from __future__ import annotations

import http.client
import io
import json
import socket
import sys
import threading
import urllib.error
import wave
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import formatdate
from pathlib import Path

import pytest

CLOUD = Path(__file__).parents[1]
sys.path.insert(0, str(CLOUD))

from speech import (
    SPEECH_COUNTER_KEYS,
    TTS_FORMAT,
    AzureSpeechAdapter,
    SpeechError,
    load_fallback_error_audio,
)

MP3_REPLY = b"\xff\xf3\x64\xc4" + b"\0" * 140


class Response:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        assert len(self.body) <= limit
        return self.body


def test_azure_adapter_uses_managed_identity_pcm_in_and_mp3_out():
    requests = []

    def open_request(request, timeout):
        requests.append((request, timeout))
        if "metadata/identity/oauth2/token" in request.full_url:
            return Response(
                json.dumps(
                    {"access_token": "entra-token", "expires_on": "2000000000"}
                ).encode()
            )
        if "/stt/" in request.full_url:
            with wave.open(io.BytesIO(request.data), "rb") as wav:
                assert wav.getnchannels() == 1
                assert wav.getsampwidth() == 2
                assert wav.getframerate() == 16000
            assert "language=en-US" in request.full_url
            return Response(
                json.dumps(
                    {
                        "RecognitionStatus": "Success",
                        "NBest": [{"Display": "Open the project."}],
                    }
                ).encode()
            )
        assert request.full_url.endswith("/tts/cognitiveservices/v1")
        assert request.headers["X-microsoft-outputformat"] == TTS_FORMAT
        assert b'xml:lang="en-US"' in request.data
        assert b'name="en-US-AvaMultilingualNeural"' in request.data
        assert b"Open &amp; explain &lt;this&gt;." in request.data
        return Response(MP3_REPLY)

    adapter = AzureSpeechAdapter(
        endpoint="https://walkie-speech.cognitiveservices.azure.com",
        resource_id=(
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.CognitiveServices/accounts/walkie-speech"
        ),
        managed_identity_client_id="identity-client",
        opener=open_request,
        clock=lambda: 1_000_000_000,
    )

    pcm_24khz = b"\x00\x00\x01\x00\x02\x00" * 800
    assert adapter.transcribe(pcm_24khz) == "Open the project."
    assert adapter.synthesize("Open & explain <this>.") == MP3_REPLY

    assert len([r for r, _ in requests if "metadata/" in r.full_url]) == 1
    for request, timeout in requests[1:]:
        assert 0 < timeout <= 15
        assert request.headers["Authorization"] == (
            "Bearer aad#/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.CognitiveServices/accounts/walkie-speech#entra-token"
        )
        assert "Ocp-apim-subscription-key" not in request.headers


def test_azure_adapter_rejects_audio_outside_the_device_contract():
    adapter = AzureSpeechAdapter(
        endpoint="https://speech.invalid",
        resource_id="/speech/id",
        managed_identity_client_id="identity",
        opener=lambda *_args: pytest.fail("invalid input must not reach Azure"),
    )

    with pytest.raises(SpeechError, match="PCM16"):
        adapter.transcribe(b"odd")
    with pytest.raises(SpeechError, match="15 minutes"):
        adapter.transcribe(b"\0\0" * (24_000 * 15 * 60 + 1))


@pytest.mark.parametrize("seconds", [60, 61, 900])
def test_complete_recording_reaches_the_appropriate_transcription_api(seconds):
    pcm = b"\x01\0" * (24_000 * seconds)
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        if "metadata/" in request.full_url:
            return Response(b'{"access_token":"token","expires_on":"2000000000"}')
        if seconds == 60:
            assert "/stt/" in request.full_url
            assert 0 < timeout <= 15
            return Response(b'{"RecognitionStatus":"Success","DisplayText":"Short."}')
        assert request.full_url.endswith(
            "/speechtotext/transcriptions:transcribe?api-version=2025-10-15"
        )
        assert request.headers["Authorization"] == "Bearer token"
        assert 15 < timeout <= 180
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {request.headers['Content-type']}\r\n\r\n".encode()
            + request.data
        )
        parts = {
            part.get_param("name", header="content-disposition"): part.get_payload(
                decode=True
            )
            for part in message.iter_parts()
        }
        assert json.loads(parts["definition"]) == {"locales": ["en-US"]}
        with wave.open(io.BytesIO(parts["audio"]), "rb") as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
                24_000,
                1,
                2,
            )
            assert wav.getnframes() == seconds * 24_000
            assert wav.readframes(wav.getnframes()) == pcm
        return Response(
            json.dumps({"combinedPhrases": [{"text": " Full recording. "}]}).encode()
        )

    adapter = AzureSpeechAdapter(
        endpoint="https://speech.invalid",
        resource_id="/speech/id",
        managed_identity_client_id="identity",
        opener=open_request,
        clock=lambda: 1_000_000_000,
    )
    assert adapter.transcribe(pcm) == ("Short." if seconds == 60 else "Full recording.")
    assert len(requests) == 2


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"combinedPhrases": None},
        {"combinedPhrases": [None]},
        {"combinedPhrases": [{"text": 42}]},
        {"combinedPhrases": [{"text": "partial"}, {}]},
    ],
)
def test_long_transcription_rejects_malformed_results(payload):
    adapter = AzureSpeechAdapter(
        endpoint="https://speech.invalid",
        resource_id="/speech/id",
        managed_identity_client_id="identity",
        opener=lambda request, timeout: Response(
            b'{"access_token":"token","expires_on":"2000000000"}'
            if "metadata/" in request.full_url
            else json.dumps(payload).encode()
        ),
    )
    with pytest.raises(SpeechError, match="transcription"):
        adapter.transcribe(b"\0\0" * (24_000 * 61))


def test_long_transcription_handles_silence():
    adapter = AzureSpeechAdapter(
        endpoint="https://speech.invalid",
        resource_id="/speech/id",
        managed_identity_client_id="identity",
        opener=lambda request, timeout: Response(
            b'{"access_token":"token","expires_on":"2000000000"}'
            if "metadata/" in request.full_url
            else b'{"combinedPhrases":[]}'
        ),
    )
    assert adapter.transcribe(b"\0\0" * (24_000 * 61)) == ""


def test_azure_reply_must_be_mp3():
    def open_request(request, timeout):
        if "metadata/" in request.full_url:
            return Response(b'{"access_token": "t", "expires_on": "2000000000"}')
        return Response(b"\x01\x00\x02\x00")

    adapter = AzureSpeechAdapter(
        endpoint="https://speech.invalid",
        resource_id="/speech/id",
        managed_identity_client_id="identity",
        opener=open_request,
    )
    with pytest.raises(SpeechError, match="invalid MP3"):
        adapter.synthesize("Hello.")


def test_pre_rendered_error_audio_is_device_mp3():
    audio = load_fallback_error_audio()

    # MPEG-2 Layer III, mono, 24 kHz, 48 kbit/s: 144-byte frames, no ID3/Xing.
    assert audio[:4] == b"\xff\xf3\x64\xc4"
    assert len(audio) % 144 == 0
    assert 10 * 144 < len(audio) < 1024 * 1024


def test_synthesis_uses_an_allowlisted_voice_or_the_default(capsys):
    voices = []

    def open_request(request, timeout):
        if "metadata/" in request.full_url:
            return Response(b'{"access_token": "t", "expires_on": "2000000000"}')
        voices.append(request.data.split(b'name="', 1)[1].split(b'"', 1)[0])
        return Response(MP3_REPLY)

    adapter = AzureSpeechAdapter(
        endpoint="https://speech.invalid",
        resource_id="/speech/id",
        managed_identity_client_id="identity",
        opener=open_request,
    )
    adapter.synthesize("Hello.", "en-US-EmmaMultilingualNeural")
    # A control plane still sending HD names during a rolling deploy works.
    adapter.synthesize("Hello.", "en-GB-Ryan:DragonHDLatestNeural")
    adapter.synthesize("Hello.")
    adapter.synthesize("Hello.", 'en-US-Ava" onload="xMultilingualNeural')
    adapter.synthesize("Hello.", "en-US-Ava:OtherNeural")
    assert voices == [
        b"en-US-EmmaMultilingualNeural",
        b"en-GB-Ryan:DragonHDLatestNeural",
        b"en-US-AvaMultilingualNeural",
        b"en-US-AvaMultilingualNeural",
        b"en-US-AvaMultilingualNeural",
    ]
    assert capsys.readouterr().out.count("invalid voice") == 1


class FakeTime:
    now = 0.0

    def __init__(self):
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def http_error(status, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://speech.invalid/private",
        status,
        "private error",
        headers,
        io.BytesIO(b"secret response body"),
    )


@pytest.fixture(params=["stt", "tts"])
def speech_call(request):
    operation = request.param
    timer = FakeTime()
    attempts = []
    outcomes = []
    counters = dict.fromkeys(SPEECH_COUNTER_KEYS, 0)

    def open_request(request, timeout):
        if "metadata/" in request.full_url:
            return Response(
                b'{"access_token":"secret-token","expires_on":"2000000000"}'
            )
        attempts.append((request, timeout))
        outcome = outcomes.pop(0) if outcomes else None
        if callable(outcome):
            outcome = outcome(timeout)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or Response(
            b'{"RecognitionStatus":"Success","DisplayText":"Hello."}'
            if operation == "stt"
            else MP3_REPLY
        )

    adapter = AzureSpeechAdapter(
        endpoint="https://speech.invalid",
        resource_id="/speech/id",
        managed_identity_client_id="identity",
        opener=open_request,
        clock=lambda: 1_000_000_000,
        monotonic=lambda: timer.now,
        sleep=lambda seconds: timer.sleep(seconds),
        jitter=lambda low, high: high,
        counters=counters,
    )
    call = (
        (lambda: adapter.transcribe(b"\0\0" * 24))
        if operation == "stt"
        else (lambda: adapter.synthesize("Hello."))
    )
    return operation, call, timer, attempts, outcomes, counters


def test_throttle_then_success_reuses_request_and_counts_latency(speech_call):
    operation, call, timer, attempts, outcomes, counters = speech_call
    error = http_error(429)
    outcomes.append(error)
    assert call()
    assert error.fp.closed
    assert timer.sleeps == [0.25]
    assert [timeout for _, timeout in attempts] == [15, 2.75]
    assert attempts[0][0] is attempts[1][0]
    assert counters[f"speech_{operation}_requests"] == 2
    assert counters[f"speech_{operation}_retries"] == 1
    assert counters[f"speech_{operation}_429"] == 1
    assert counters[f"speech_{operation}_latency_count"] == 1
    assert counters[f"speech_{operation}_latency_le_250ms"] == 1
    assert counters[f"speech_{operation}_latency_le_1s"] == 1


@pytest.mark.parametrize(
    "header,delay",
    [
        ("2", 2),
        (formatdate(1_000_000_002, usegmt=True), 2),
        ("invalid", 0.25),
        ("-1", 0.25),
        ("0", 0.25),
        (formatdate(999_999_990, usegmt=True), 0.25),
    ],
)
def test_retry_after(speech_call, header, delay):
    _, call, timer, attempts, outcomes, _ = speech_call
    outcomes.append(http_error(429, header))
    assert call()
    assert timer.sleeps == [delay]
    assert attempts[1][1] == 3 - delay


@pytest.mark.parametrize("header", ["3", "600", "9" * 400])
def test_retry_after_outside_budget_fails_without_waiting(speech_call, header):
    _, call, timer, attempts, outcomes, _ = speech_call
    outcomes.append(http_error(429, header))
    with pytest.raises(SpeechError, match="HTTP 429"):
        call()
    assert len(attempts) == 1
    assert timer.sleeps == []


@pytest.mark.parametrize("status", [500, 503, 599])
def test_server_errors_exhaust_three_attempts(speech_call, status):
    operation, call, timer, attempts, outcomes, counters = speech_call
    outcomes.extend(http_error(status) for _ in range(3))
    with pytest.raises(SpeechError) as failure:
        call()
    assert str(failure.value) == f"Azure Speech request failed: HTTP {status}"
    assert len(attempts) == 3
    assert timer.sleeps == [0.25, 0.5]
    assert counters[f"speech_{operation}_5xx"] == 3
    assert counters[f"speech_{operation}_retries"] == 2
    assert counters[f"speech_{operation}_latency_le_250ms"] == 0
    assert counters[f"speech_{operation}_latency_le_1s"] == 1
    assert counters[f"speech_{operation}_latency_count"] == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 422])
def test_client_errors_are_not_retried(speech_call, status):
    _, call, timer, attempts, outcomes, _ = speech_call
    outcomes.append(http_error(status))
    with pytest.raises(SpeechError, match=f"HTTP {status}"):
        call()
    assert len(attempts) == 1
    assert timer.sleeps == []


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("secret"),
        urllib.error.URLError(ConnectionResetError("secret")),
        http.client.RemoteDisconnected("secret"),
        http.client.IncompleteRead(b"secret"),
    ],
)
def test_connection_failures_retry(speech_call, error):
    _, call, timer, attempts, outcomes, _ = speech_call
    outcomes.append(error)
    assert call()
    assert len(attempts) == 2
    assert timer.sleeps == [0.25]


def test_connection_reset_while_reading_retries(speech_call):
    _, call, _, attempts, outcomes, _ = speech_call

    class ResetResponse(Response):
        def read(self, limit):
            raise ConnectionResetError("secret")

    outcomes.append(ResetResponse(b""))
    assert call()
    assert len(attempts) == 2


@pytest.mark.parametrize("first_duration", [0, 14])
def test_retries_share_deadline_and_add_at_most_three_seconds(
    speech_call, first_duration
):
    _, call, timer, attempts, outcomes, _ = speech_call

    def first_attempt(timeout):
        timer.now += first_duration
        return http_error(503)

    def stalled_retry(timeout):
        timer.now += timeout
        return TimeoutError("secret")

    outcomes.extend([first_attempt, stalled_retry])
    with pytest.raises(SpeechError, match="TimeoutError"):
        call()
    assert len(attempts) == 2
    assert timer.now == min(15, first_duration + 3)


def test_late_failure_does_not_retry_or_sleep(speech_call):
    _, call, timer, attempts, outcomes, _ = speech_call

    def late_failure(timeout):
        timer.now += timeout
        return http_error(503)

    outcomes.append(late_failure)
    with pytest.raises(SpeechError, match="HTTP 503"):
        call()
    assert len(attempts) == 1
    assert timer.sleeps == []


def test_deadline_is_rechecked_after_sleep(speech_call):
    _, call, timer, attempts, outcomes, _ = speech_call
    outcomes.append(http_error(503))
    sleep = timer.sleep

    # Simulate a descheduled thread waking after its retry deadline.
    def oversleep(seconds):
        sleep(seconds + 4)

    timer.sleep = oversleep
    with pytest.raises(SpeechError, match="deadline"):
        call()
    assert len(attempts) == 1


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("secret"),
        urllib.error.URLError("secret"),
        ValueError("secret"),
        AttributeError("secret"),
    ],
)
def test_non_retryable_transport_errors_are_safe(speech_call, error):
    _, call, timer, attempts, outcomes, _ = speech_call
    outcomes.append(error)
    with pytest.raises(SpeechError) as failure:
        call()
    assert str(failure.value) == f"Azure Speech request failed: {type(error).__name__}"
    assert len(attempts) == 1
    assert timer.sleeps == []


def _slow_http_adapter(timer, response, header_seconds):
    def opener(request, timeout):
        timer.now += header_seconds  # connection and headers use the budget
        return response

    return AzureSpeechAdapter(
        endpoint="https://speech.invalid",
        resource_id="/speech/id",
        managed_identity_client_id="identity",
        opener=opener,
        monotonic=lambda: timer.now,
    )


def test_long_reply_streams_past_the_deadline_with_an_idle_timeout():
    timer = FakeTime()
    audio = MP3_REPLY * 7_000  # ~1 MB, like a 4,000-character reply
    client, server = socket.socketpair()
    client.settimeout(1)  # urllib's leftover header budget
    head = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(audio)
    with client, server:
        # More than a socket buffer, so the server writes from a thread.
        threading.Thread(target=server.sendall, args=(head + audio,)).start()
        response = http.client.HTTPResponse(client)
        response.begin()
        read = response.read
        timeouts = []

        def slow_read(limit):
            timeouts.append(client.gettimeout())
            timer.now += 40  # the whole body takes far longer than 15 s
            return read(limit)

        response.read = slow_read
        adapter = _slow_http_adapter(timer, response, header_seconds=14)
        request = urllib.request.Request("https://speech.invalid")
        assert adapter._read(request, 1024 * 1024, 15, operation="tts") == audio
        assert timeouts == [15]


def test_headers_after_the_deadline_fail():
    timer = FakeTime()
    client, server = socket.socketpair()
    with client, server:
        server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        response = http.client.HTTPResponse(client)
        response.begin()
        adapter = _slow_http_adapter(timer, response, header_seconds=15)
        with pytest.raises(SpeechError, match="deadline"):
            adapter._read(urllib.request.Request("https://speech.invalid"), 10, 15)


def test_missing_private_socket_falls_back_to_urllib_timeout():
    client, server = socket.socketpair()
    with client, server:
        server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\n")
        response = http.client.HTTPResponse(client)
        response.begin()
        response.fp = io.BufferedReader(io.BytesIO(b"abcdef"))  # no ._sock
        adapter = _slow_http_adapter(FakeTime(), response, header_seconds=0)
        request = urllib.request.Request("https://speech.invalid")
        assert adapter._read(request, 10, 15) == b"abcdef"


def test_truncated_http_body_retries_instead_of_accepting_partial_mp3(speech_call):
    _, call, _, attempts, outcomes, _ = speech_call
    client, server = socket.socketpair()
    with client, server:
        server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n\xff\xf3")
        server.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(client)
        response.begin()
        outcomes.append(response)
        assert call()
        assert len(attempts) == 2


@pytest.mark.parametrize(
    "payload", [b"[]", b"1", b'{"RecognitionStatus":"Success","NBest":["x"]}']
)
def test_malformed_transcription_is_a_speech_error(payload):
    def open_request(request, timeout):
        if "metadata/" in request.full_url:
            return Response(b'{"access_token": "t", "expires_on": "2000000000"}')
        return Response(payload)

    adapter = AzureSpeechAdapter(
        endpoint="https://speech.invalid",
        resource_id="/speech/id",
        managed_identity_client_id="identity",
        opener=open_request,
    )
    with pytest.raises(SpeechError):
        adapter.transcribe(b"\0\0" * 24)
