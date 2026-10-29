"""Replaceable cloud speech boundary for the WalkieOne transport.

The managed deployment defaults to Azure managed identity; owners may select
recognition and synthesis independently, including their own HTTP service. Tests provide a deterministic object with the same two methods, so
normal development never needs Azure access, credentials, or downloaded speech
models.
"""

from __future__ import annotations

import array
import http.client
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from collections.abc import Callable, MutableMapping
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock
from typing import Protocol
from xml.sax.saxutils import escape

# Device microphone audio is raw PCM16 mono 24 kHz; replies are MP3 (MPEG-2
# Layer III, mono, 24 kHz, 48 kbit/s), which the Device decodes itself.
DEVICE_RATE = 24_000
AZURE_STT_RATE = 16_000
SAMPLE_WIDTH = 2
MAX_AUDIO_SECONDS = 15 * 60
MAX_AUDIO_BYTES = DEVICE_RATE * SAMPLE_WIDTH * MAX_AUDIO_SECONDS
SHORT_AUDIO_BYTES = DEVICE_RATE * SAMPLE_WIDTH * 60  # Azure's short-audio API cap
MAX_SYNTHESIZED_BYTES = 1024 * 1024  # the Device's MP3 playback cap
TTS_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
DEFAULT_VOICE = "en-US-AvaMultilingualNeural"
# The voice name lands inside an SSML attribute and saxutils.escape leaves
# quotes alone, so only these exact Azure name shapes are allowed through.
# Standard Multilingual voices are current. The HD shape stays valid so a
# control plane still sending HD names during a rolling deploy keeps working.
VOICE_PATTERN = re.compile(
    r"[a-z]{2}-[A-Z]{2}-(?:[A-Za-z]+MultilingualNeural|[A-Za-z]+:DragonHDLatestNeural)"
)
FALLBACK_ERROR_ASSET = Path(__file__).with_name("fallback-error.mp3")
TOKEN_RESOURCE = "https://cognitiveservices.azure.com"
METADATA_TOKEN_URL = (
    "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01"
)
MAX_ATTEMPTS = 3
RETRY_WINDOW = 3.0  # added time after the first transient failure
LATENCY_BUCKETS = ((0.25, "250ms"), (1, "1s"), (3, "3s"), (15, "15s"))
SPEECH_COUNTER_KEYS = tuple(
    f"speech_{operation}_{suffix}"
    for operation in ("stt", "tts")
    for suffix in (
        "requests",
        "retries",
        "429",
        "5xx",
        *(f"latency_le_{label}" for _, label in LATENCY_BUCKETS),
        "latency_count",
    )
)


class SpeechError(RuntimeError):
    """A safe cloud speech failure without response bodies or credentials."""


class SpeechAdapter(Protocol):
    def transcribe(self, pcm: bytes) -> str: ...

    def synthesize(self, text: str, voice: str | None = None) -> bytes: ...


class RecognitionAdapter(Protocol):
    def transcribe(self, pcm: bytes) -> str: ...


class SynthesisAdapter(Protocol):
    def synthesize(self, text: str, voice: str | None = None) -> bytes: ...


class ConfiguredSpeech:
    """Independent speech choices behind the existing turn-router interface."""

    def __init__(self, recognition: RecognitionAdapter, synthesis: SynthesisAdapter):
        self.recognition = recognition
        self.synthesis = synthesis

    def transcribe(self, pcm: bytes) -> str:
        return self.recognition.transcribe(pcm)

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        return self.synthesis.synthesize(text, voice)


class _NoSpeechRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SpeechError("Speech endpoint redirects are refused")


class DeepgramSpeechAdapter:
    """Owner-key REST speech; no Azure or Walkie One account is involved."""

    def __init__(
        self,
        api_key: str,
        *,
        recognition_model: str = "nova-3",
        synthesis_model: str = "aura-2-thalia-en",
        opener: Callable | None = None,
    ):
        if not api_key or any(c in api_key for c in "\r\n"):
            raise SpeechError("Deepgram API key is missing or invalid")
        for model in (recognition_model, synthesis_model):
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", model):
                raise SpeechError("Deepgram model is invalid")
        self._key = api_key
        self.recognition_model = recognition_model
        self.synthesis_model = synthesis_model
        self._open = opener or urllib.request.build_opener(_NoSpeechRedirect()).open

    @classmethod
    def from_environment(cls):
        return cls(
            os.environ.get("DEEPGRAM_API_KEY", ""),
            recognition_model=os.environ.get("DEEPGRAM_STT_MODEL", "nova-3"),
            synthesis_model=os.environ.get("DEEPGRAM_TTS_MODEL", "aura-2-thalia-en"),
        )

    def _request(
        self, operation: str, query: dict, body: bytes, content_type: str, limit: int
    ):
        request = urllib.request.Request(
            "https://api.deepgram.com/v1/"
            + operation
            + "?"
            + urllib.parse.urlencode(query),
            data=body,
            headers={
                "Authorization": "Token " + self._key,
                "Content-Type": content_type,
            },
            method="POST",
        )
        try:
            with self._open(request, timeout=15) as response:
                result = response.read(limit + 1)
        except (OSError, urllib.error.URLError) as error:
            # Provider bodies/URLs may contain private text or credentials.
            raise SpeechError("Deepgram request failed") from error
        if len(result) > limit:
            raise SpeechError("Deepgram response exceeded its bound")
        return result

    def transcribe(self, pcm: bytes) -> str:
        if len(pcm) % SAMPLE_WIDTH:
            raise SpeechError("Device audio must be complete PCM16 samples")
        if len(pcm) > MAX_AUDIO_BYTES:
            raise SpeechError("Device audio cannot exceed 60 seconds")
        if not pcm:
            return ""
        raw = self._request(
            "listen",
            {
                "model": self.recognition_model,
                "encoding": "linear16",
                "sample_rate": DEVICE_RATE,
                "channels": 1,
                "language": "en",
                "smart_format": "true",
                "mip_opt_out": "true",
            },
            pcm,
            "application/octet-stream",
            1_000_000,
        )
        try:
            result = json.loads(raw)["results"]["channels"][0]["alternatives"][0][
                "transcript"
            ]
        except (ValueError, TypeError, KeyError, IndexError) as error:
            raise SpeechError("Deepgram returned invalid transcription data") from error
        if not isinstance(result, str) or len(result) > 4_000:
            raise SpeechError("Deepgram transcript is invalid or too long")
        return result.strip()

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        text = text.strip()
        if not text or len(text) > 4_000:
            raise SpeechError("Speech synthesis text is empty or too long")
        # A legacy Azure voice preference cannot select a different provider.
        model = (
            voice
            if voice and re.fullmatch(r"aura-[a-z0-9-]{1,72}", voice)
            else self.synthesis_model
        )
        audio = self._request(
            "speak",
            {
                "model": model,
                "encoding": "mp3",
                "bit_rate": 48000,
                "mip_opt_out": "true",
            },
            json.dumps({"text": text}).encode(),
            "application/json",
            MAX_SYNTHESIZED_BYTES,
        )
        if not looks_like_mp3(audio):
            raise SpeechError("Deepgram returned invalid MP3 audio")
        return audio


class HTTPSpeechAdapter:
    """Small owner-hosted HTTP contract; providers stay behind this boundary."""

    def __init__(self, endpoint: str, token: str, *, opener: Callable | None = None):
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in endpoint
            or any(ord(c) < 33 or ord(c) > 126 for c in endpoint)
        ):
            raise SpeechError(
                "Custom speech requires an HTTPS endpoint without credentials"
            )
        if not token or any(c in token for c in "\r\n"):
            raise SpeechError("Custom speech token is missing or invalid")
        self.endpoint = endpoint.rstrip("/")
        self._token = token
        self._open = opener or urllib.request.build_opener(_NoSpeechRedirect()).open

    @classmethod
    def from_environment(cls):
        return cls(
            os.environ.get("WALKIEONE_SPEECH_URL", ""),
            os.environ.get("WALKIEONE_SPEECH_TOKEN", ""),
        )

    def _request(self, path: str, body: bytes, content_type: str, limit: int) -> bytes:
        request = urllib.request.Request(
            self.endpoint + path,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": content_type,
            },
        )
        try:
            with self._open(request, timeout=15) as response:
                result = response.read(limit + 1)
        except (OSError, urllib.error.URLError) as error:
            raise SpeechError("Custom speech request failed") from error
        if len(result) > limit:
            raise SpeechError("Custom speech response exceeded its bound")
        return result

    def transcribe(self, pcm: bytes) -> str:
        if len(pcm) % SAMPLE_WIDTH or len(pcm) > MAX_AUDIO_BYTES:
            raise SpeechError(
                "Device audio must be PCM16 mono 24 kHz and at most 60 seconds"
            )
        if not pcm:
            return ""
        raw = self._request("/transcribe", pcm, "application/octet-stream", 64_000)
        try:
            result = json.loads(raw)["text"]
        except (ValueError, KeyError, TypeError) as error:
            raise SpeechError(
                "Custom speech returned invalid transcription data"
            ) from error
        if not isinstance(result, str) or len(result) > 4_000:
            raise SpeechError("Custom speech transcript is invalid or too long")
        return result.strip()

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        text = text.strip()
        if not text or len(text) > 4_000:
            raise SpeechError("Speech synthesis text is empty or too long")
        # Voice names are opaque to the owner endpoint, but remain bounded.
        if voice is not None and (not isinstance(voice, str) or len(voice) > 128):
            raise SpeechError("Custom speech voice is too long")
        audio = self._request(
            "/synthesize",
            json.dumps({"text": text, "voice": voice}).encode(),
            "application/json",
            MAX_SYNTHESIZED_BYTES,
        )
        if not looks_like_mp3(audio):
            raise SpeechError("Custom speech returned invalid MP3 audio")
        return audio


def configured_speech() -> ConfiguredSpeech:
    """Resolve only selected providers; absent settings preserve managed Azure."""
    names = [
        os.environ.get("WALKIEONE_STT_PROVIDER", "azure"),
        os.environ.get("WALKIEONE_TTS_PROVIDER", "azure"),
    ]
    constructors = {
        "azure": AzureSpeechAdapter.from_environment,
        "deepgram": DeepgramSpeechAdapter.from_environment,
        "http": HTTPSpeechAdapter.from_environment,
    }
    if any(name not in constructors for name in names):
        raise SpeechError("Unknown speech provider; choose azure, deepgram or http")
    instances = {name: constructors[name]() for name in set(names)}
    return ConfiguredSpeech(instances[names[0]], instances[names[1]])


def looks_like_mp3(audio: bytes) -> bool:
    """Cheap shape check; the Device validates every frame before playback."""
    return audio[:3] == b"ID3" or (
        len(audio) > 1 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0
    )


def load_fallback_error_audio(path: Path = FALLBACK_ERROR_ASSET) -> bytes:
    """Load the cloud-owned error speech that does not depend on Azure."""
    try:
        audio = path.read_bytes()
    except OSError as error:
        raise SpeechError("pre-rendered cloud error speech is unavailable") from error
    if not looks_like_mp3(audio) or len(audio) > MAX_SYNTHESIZED_BYTES:
        raise SpeechError("pre-rendered cloud error speech is invalid")
    return audio


def _resample_24k_to_16k(pcm: bytes) -> bytes:
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    output = array.array("h")
    # 16/24 = 2/3. Output sample n falls at input position n * 3/2;
    # interpolate the half-sample positions instead of simply discarding every
    # third sample, which keeps spoken consonants intelligible.
    for index in range(len(samples) * 2 // 3):
        source_twice = index * 3
        left = source_twice // 2
        if source_twice % 2 and left + 1 < len(samples):
            output.append((samples[left] + samples[left + 1]) // 2)
        else:
            output.append(samples[left])
    if sys.byteorder != "little":
        output.byteswap()
    return output.tobytes()


def _wav_16khz(pcm_24khz: bytes) -> bytes:
    return _wav(_resample_24k_to_16k(pcm_24khz), AZURE_STT_RATE)


def _wav(pcm: bytes, rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return output.getvalue()


class AzureSpeechAdapter:
    """Azure Speech REST adapter authenticated by the relay managed identity."""

    def __init__(
        self,
        *,
        endpoint: str,
        resource_id: str,
        managed_identity_client_id: str,
        voice: str = DEFAULT_VOICE,
        opener: Callable = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        counters: MutableMapping[str, int] | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.resource_id = resource_id
        self.managed_identity_client_id = managed_identity_client_id
        self.voice = voice
        self._open = opener
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._jitter = jitter
        self._counters = counters if counters is not None else {}
        self._counter_lock = Lock()
        self._token = ""
        self._token_expiry = 0.0
        self._warned_voice = False
        if not self.endpoint.startswith("https://"):
            raise SpeechError("Azure Speech endpoint must use HTTPS")
        if not self.resource_id.startswith("/") or not managed_identity_client_id:
            raise SpeechError(
                "Azure Speech managed identity configuration is incomplete"
            )

    @classmethod
    def from_environment(
        cls, *, counters: MutableMapping[str, int] | None = None
    ) -> AzureSpeechAdapter:
        missing = [
            name
            for name in (
                "AZURE_SPEECH_ENDPOINT",
                "AZURE_SPEECH_RESOURCE_ID",
                "AZURE_MANAGED_IDENTITY_CLIENT_ID",
            )
            if not os.environ.get(name)
        ]
        if missing:
            raise SpeechError("missing " + ", ".join(missing))
        return cls(
            endpoint=os.environ["AZURE_SPEECH_ENDPOINT"],
            resource_id=os.environ["AZURE_SPEECH_RESOURCE_ID"],
            managed_identity_client_id=os.environ["AZURE_MANAGED_IDENTITY_CLIENT_ID"],
            voice=os.environ.get("AZURE_SPEECH_VOICE", DEFAULT_VOICE),
            counters=counters,
        )

    def _count(self, operation: str, suffix: str) -> None:
        key = f"speech_{operation}_{suffix}"
        # Speech calls run concurrently in the relay's worker threads.
        with self._counter_lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    def _retry_after(self, value: str | None) -> float:
        if not value:
            return 0.0
        try:
            if value.strip().isdigit():
                return float(value)
            return max(0.0, parsedate_to_datetime(value).timestamp() - self._clock())
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def _read_body(self, response, limit: int, idle: float) -> bytes:
        if not isinstance(response, http.client.HTTPResponse):
            return response.read(limit + 1)
        # urllib's socket timeout is idle (per read), set to whatever header
        # budget remained. Once the body streams, give each read the full idle
        # timeout: a long reply that keeps arriving must not hit the deadline.
        try:
            response.fp.raw._sock.settimeout(idle)
        except (AttributeError, OSError):
            pass  # private attribute; keep urllib's shorter idle timeout
        body = response.read(limit + 1)
        # read(n) returns a short body on early EOF instead of raising.
        if len(body) <= limit and response.length:
            raise http.client.IncompleteRead(b"")
        return body

    def _read(
        self,
        request: urllib.request.Request,
        limit: int,
        timeout: int,
        *,
        operation: str | None = None,
    ) -> bytes:
        started = self._monotonic()
        deadline = started + timeout
        attempts = MAX_ATTEMPTS if operation else 1
        try:
            for attempt in range(attempts):
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise SpeechError("Azure Speech request deadline exceeded")
                if operation:
                    self._count(operation, "requests")
                    if attempt:
                        self._count(operation, "retries")
                retry_after = 0.0
                try:
                    with self._open(request, timeout=remaining) as response:
                        # The deadline bounds connect and headers only.
                        if self._monotonic() >= deadline:
                            raise SpeechError("Azure Speech request deadline exceeded")
                        body = self._read_body(response, limit, timeout)
                    if len(body) > limit:
                        raise SpeechError("Azure Speech response exceeded its bound")
                    return body
                except SpeechError:
                    raise
                except urllib.error.HTTPError as error:
                    status = error.code
                    retryable = status == 429 or 500 <= status < 600
                    if operation and retryable:
                        self._count(operation, "429" if status == 429 else "5xx")
                    retry_after = self._retry_after(error.headers.get("Retry-After"))
                    error.close()  # never read/log the error body
                    failure = f"Azure Speech request failed: HTTP {status}"
                except (
                    OSError,
                    urllib.error.URLError,
                    http.client.HTTPException,
                ) as error:
                    reason = (
                        error.reason
                        if isinstance(error, urllib.error.URLError)
                        else error
                    )
                    retryable = isinstance(
                        reason, (ConnectionError, http.client.IncompleteRead)
                    )
                    failure = f"Azure Speech request failed: {type(error).__name__}"
                except (AttributeError, TypeError, ValueError) as error:
                    # Callers handle only SpeechError; an unexpected client or
                    # response shape must not crash the turn.
                    raise SpeechError(
                        f"Azure Speech request failed: {type(error).__name__}"
                    ) from None
                if not retryable or attempt + 1 == attempts:
                    raise SpeechError(failure) from None
                # The original timeout is shared by every attempt and sleep.
                # Even an early 429 cannot add more than three seconds.
                if attempt == 0:
                    deadline = min(deadline, self._monotonic() + RETRY_WINDOW)
                delay = max(retry_after, self._jitter(0.0, 0.25 * 2**attempt))
                if delay >= deadline - self._monotonic():
                    # Never shorten Azure's Retry-After to squeeze in a retry.
                    raise SpeechError(failure) from None
                self._sleep(delay)
        finally:
            if operation:
                elapsed = self._monotonic() - started
                for bound, label in LATENCY_BUCKETS:
                    if elapsed <= bound:
                        self._count(operation, f"latency_le_{label}")
                self._count(operation, "latency_count")

    def _access_token(self) -> str:
        if self._token and self._token_expiry - self._clock() > 60:
            return self._token
        query = urllib.parse.urlencode(
            {
                "resource": TOKEN_RESOURCE,
                "client_id": self.managed_identity_client_id,
            }
        )
        request = urllib.request.Request(
            f"{METADATA_TOKEN_URL}&{query}", headers={"Metadata": "true"}
        )
        try:
            payload = json.loads(self._read(request, 64_000, 5))
            token = payload["access_token"]
            expiry = float(payload["expires_on"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise SpeechError(
                "managed identity returned an invalid Speech token"
            ) from error
        if not isinstance(token, str) or not token:
            raise SpeechError("managed identity returned an empty Speech token")
        self._token = token
        self._token_expiry = expiry
        return token

    def _authorization(self) -> str:
        # Azure Speech's Entra form binds the access token to this one resource.
        return f"Bearer aad#{self.resource_id}#{self._access_token()}"

    def transcribe(self, pcm: bytes) -> str:
        if len(pcm) % SAMPLE_WIDTH:
            raise SpeechError("Device audio must be complete PCM16 samples")
        if len(pcm) > MAX_AUDIO_BYTES:
            raise SpeechError("Device audio cannot exceed 15 minutes")
        if not pcm:
            return ""
        if len(pcm) > SHORT_AUDIO_BYTES:
            return self._transcribe_long(pcm)
        url = (
            f"{self.endpoint}/stt/speech/recognition/conversation/"
            "cognitiveservices/v1?language=en-US&format=detailed"
        )
        request = urllib.request.Request(
            url,
            data=_wav_16khz(pcm),
            method="POST",
            headers={
                "Authorization": self._authorization(),
                "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
                "Accept": "application/json",
            },
        )
        try:
            payload = json.loads(self._read(request, 1_000_000, 15, operation="stt"))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise SpeechError(
                "Azure Speech returned invalid transcription data"
            ) from error
        if not isinstance(payload, dict):
            raise SpeechError("Azure Speech returned invalid transcription data")
        status = payload.get("RecognitionStatus")
        if status in {"NoMatch", "InitialSilenceTimeout", "BabbleTimeout"}:
            return ""
        if status != "Success":
            raise SpeechError("Azure Speech could not transcribe the Device audio")
        choices = payload.get("NBest")
        best = choices[0] if isinstance(choices, list) and choices else None
        text = best.get("Display") if isinstance(best, dict) else None
        if not isinstance(text, str):
            text = payload.get("DisplayText")
        if not isinstance(text, str):
            raise SpeechError("Azure Speech transcription had no text")
        return text.strip()

    def _transcribe_long(self, pcm: bytes) -> str:
        # Fast transcription accepts a complete WAV at the Device's native rate.
        # Keep it in memory; no temporary audio file or persistent batch job.
        boundary = uuid.uuid4().hex
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="definition"\r\n'
                'Content-Type: application/json\r\n\r\n{"locales":["en-US"]}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="recording.wav"\r\n'
                "Content-Type: audio/wav\r\n\r\n"
            ).encode()
            + _wav(pcm, DEVICE_RATE)
            + f"\r\n--{boundary}--\r\n".encode()
        )
        request = urllib.request.Request(
            f"{self.endpoint}/speechtotext/transcriptions:transcribe?api-version=2025-10-15",
            data=body,
            method="POST",
            headers={
                # This REST surface uses the Entra token directly, unlike /stt/.
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
        )
        try:
            payload = json.loads(self._read(request, 4_000_000, 180, operation="stt"))
        except (TypeError, ValueError) as error:
            raise SpeechError(
                "Azure Speech returned invalid transcription data"
            ) from error
        phrases = payload.get("combinedPhrases") if isinstance(payload, dict) else None
        if not isinstance(phrases, list) or any(
            not isinstance(phrase, dict) or not isinstance(phrase.get("text"), str)
            for phrase in phrases
        ):
            raise SpeechError("Azure Speech returned invalid transcription data")
        return " ".join(phrase["text"].strip() for phrase in phrases).strip()

    def _voice(self, voice: str | None) -> str:
        if voice is None or VOICE_PATTERN.fullmatch(voice):
            return voice or self.voice
        if not self._warned_voice:
            self._warned_voice = True
            print("  speech: invalid voice name; using the default voice")
        return self.voice

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        voice = self._voice(voice)
        text = text.strip()
        if not text:
            raise SpeechError("Speech synthesis text is empty")
        if len(text) > 4_000:
            raise SpeechError("Speech synthesis text exceeded its bound")
        ssml = (
            '<speak version="1.0" xml:lang="en-US">'
            f'<voice xml:lang="en-US" name="{escape(voice)}">'
            f"{escape(text)}</voice></speak>"
        ).encode()
        request = urllib.request.Request(
            f"{self.endpoint}/tts/cognitiveservices/v1",
            data=ssml,
            method="POST",
            headers={
                "Authorization": self._authorization(),
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": TTS_FORMAT,
                "User-Agent": "WalkieOne-Relay",
            },
        )
        audio = self._read(request, MAX_SYNTHESIZED_BYTES, 15, operation="tts")
        if not looks_like_mp3(audio):
            raise SpeechError("Azure Speech returned invalid MP3 audio")
        return audio
