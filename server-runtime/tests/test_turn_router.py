from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

CLOUD = Path(__file__).parents[1]
sys.path.insert(0, str(CLOUD))

from delivery_types import MailboxBatch, MailboxEntry, MailboxError, MailboxReceipt
from speech import SpeechError
from turns import (
    AGENT_BUSY_REPLY,
    AGENT_ERROR_REPLIES,
    LINK_ERROR_REPLY,
    MAX_PENDING_LINK_ACKS,
    MAX_PLAYBACKS_PER_PAIR,
    MAX_PROACTIVE_INFLIGHT,
    NOTHING_WAITING_REPLY,
    QUEUE_FULL_REPLY,
    CloudTurnRouter,
)

FALLBACK_AUDIO = b"\x01\x00"


class FakeSpeech:
    def __init__(self):
        self.heard = {
            b"first mic": "Build the project.",
            b"busy mic": "Is it done?",
            b"stop mic": "Stop.",
        }
        self.transcribed = []
        self.synthesized = []

    def transcribe(self, pcm):
        self.transcribed.append(pcm)
        return self.heard[pcm]

    def synthesize(self, text):
        self.synthesized.append(text)
        return ("pcm:" + text).encode()


class FakeMailbox:
    def __init__(self):
        self.messages = []
        self.store_calls = []
        self.complete_calls = []

    async def store(
        self,
        channel,
        pair,
        message_id,
        idempotency_key,
        text,
        needs_answer,
        *,
        primary_only=False,
        provider_interaction_ttl_seconds=None,
    ):
        call = {
            "channel": channel,
            "pair": pair,
            "message_id": message_id,
            "idempotency_key": idempotency_key,
            "text": text,
            "needs_answer": needs_answer,
            "primary_only": primary_only,
        }
        if provider_interaction_ttl_seconds is not None:
            call["provider_interaction_ttl_seconds"] = provider_interaction_ttl_seconds
        self.store_calls.append(call)
        existing = next(
            (
                item
                for item in self.messages
                if item["pair"] == pair and item["idempotency_key"] == idempotency_key
            ),
            None,
        )
        if existing is not None:
            return True
        item = {
            "channel": channel,
            "pair": pair,
            "message_id": message_id,
            "idempotency_key": idempotency_key,
            "text": text,
            "needs_answer": needs_answer,
            "provider_interaction_ttl_seconds": provider_interaction_ttl_seconds,
            "state": "unread",
        }
        if provider_interaction_ttl_seconds is not None:
            item["expires_at"] = (
                (
                    datetime.now(UTC)
                    + timedelta(seconds=provider_interaction_ttl_seconds)
                )
                .isoformat()
                .replace("+00:00", "Z")
            )
        self.messages.append(item)
        return True

    async def next_batch(self, channel, pair, *, max_messages=4, max_text_chars=1_200):
        now = datetime.now(UTC)
        for item in self.messages:
            expires_at = item.get("expires_at")
            if (
                expires_at is not None
                and datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= now
            ):
                item["state"] = "expired"
        candidates = [
            item
            for item in self.messages
            if item["channel"] == channel
            and item["pair"] == pair
            and item["state"] in {"unread", "staged"}
        ]
        if not candidates:
            return None
        staged = [item for item in candidates if item["state"] == "staged"]
        if staged:
            selected = staged
            batch_id = staged[0]["batch_id"]
        else:
            selected = []
            total_chars = 0
            for item in candidates:
                if selected and (
                    len(selected) >= max_messages
                    or total_chars + len(item["text"]) > max_text_chars
                ):
                    break
                selected.append(item)
                total_chars += len(item["text"])
            batch_id = str(uuid.uuid4())
            for item in selected:
                item["state"] = "staged"
                item["batch_id"] = batch_id
        return MailboxBatch(
            batch_id=batch_id,
            pair=pair,
            channel=channel,
            messages=tuple(
                MailboxEntry(
                    message_id=item["message_id"],
                    pair=pair,
                    channel=channel,
                    idempotency_key=item["idempotency_key"],
                    text=item["text"],
                    needs_answer=item["needs_answer"],
                    expires_at=item.get("expires_at"),
                )
                for item in selected
            ),
            unread_count=len(candidates),
            remaining_count=len(candidates) - len(selected),
            needs_answer=any(item["needs_answer"] for item in selected),
            delivery_state=(
                "notified"
                if all(item.get("notified") for item in selected)
                else "queued"
            ),
            oldest_unread_at="2026-08-27T00:00:00Z",
            oldest_unread_age_seconds=0,
        )

    def _batch_receipt(self, channel, pair, batch_id, state):
        selected = [
            item
            for item in self.messages
            if item["channel"] == channel
            and item["pair"] == pair
            and item.get("batch_id") == batch_id
        ]
        if not selected:
            raise MailboxError("batch not found")
        return MailboxReceipt(
            batch_id=batch_id,
            pair=pair,
            channel=channel,
            message_ids=tuple(item["message_id"] for item in selected),
            delivery_state=state,
            unread_count=len(
                [
                    item
                    for item in self.messages
                    if item["channel"] == channel
                    and item["pair"] == pair
                    and item["state"] in {"unread", "staged"}
                ]
            ),
            oldest_unread_at=(
                "2026-08-27T00:00:00Z"
                if any(item["state"] in {"unread", "staged"} for item in self.messages)
                else None
            ),
            oldest_unread_age_seconds=(
                0
                if any(item["state"] in {"unread", "staged"} for item in self.messages)
                else None
            ),
        )

    async def notified(self, channel, pair, batch_id):
        selected = [
            item
            for item in self.messages
            if item.get("batch_id") == batch_id
            and item["channel"] == channel
            and item["pair"] == pair
        ]
        if not selected:
            raise MailboxError("batch not found")
        for item in selected:
            item["notified"] = True
        return self._batch_receipt(channel, pair, batch_id, "notified")

    async def complete_batch(self, channel, pair, batch_id):
        selected = [
            item
            for item in self.messages
            if item.get("batch_id") == batch_id
            and item["channel"] == channel
            and item["pair"] == pair
        ]
        if not selected:
            raise MailboxError("batch not found")
        for item in selected:
            item["state"] = "played"
            item["text"] = None
        self.complete_calls.append((channel, pair, batch_id))
        return self._batch_receipt(channel, pair, batch_id, "played")

    async def dismiss_batch(self, channel, pair, batch_id):
        selected = [
            item
            for item in self.messages
            if item.get("batch_id") == batch_id
            and item["channel"] == channel
            and item["pair"] == pair
        ]
        if not selected:
            raise MailboxError("batch not found")
        for item in selected:
            item["state"] = "dismissed"
            item["text"] = None
        return self._batch_receipt(channel, pair, batch_id, "dismissed")


class BlockingStoreMailbox(FakeMailbox):
    def __init__(self):
        super().__init__()
        self.store_started = asyncio.Event()
        self.release_store = asyncio.Event()

    async def store(self, *args, **kwargs):
        self.store_started.set()
        await self.release_store.wait()
        return await super().store(*args, **kwargs)


def _control(kind, **fields):
    return json.dumps({"v": 1, "type": kind, **fields})


def test_fifteen_minute_recording_and_long_transcript_are_bounded_without_truncation():
    async def exercise(extra_sample=False, extra_character=False):
        pcm = b"\x01\0" * (24_000 * 15 * 60)
        transcript = "x" * (60_000 + int(extra_character))
        speech = FakeSpeech()
        speech.heard[pcm] = transcript
        sent = []

        async def send_link(_channel, _pair, payload):
            sent.append(json.loads(payload))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _: "ready",
            send_link=send_link,
            send_device=lambda *_: asyncio.sleep(0, result=True),
            set_busy=lambda *_: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )
        await router.handle_device("channel", "pair", _control("talk.start"))
        for offset in range(0, len(pcm), 960):
            await router.handle_device("channel", "pair", pcm[offset : offset + 960])
        if extra_sample:
            await router.handle_device("channel", "pair", b"\x01\0")
        capture = router._captures[("channel", "pair")]
        assert len(capture.audio) == len(pcm)
        assert capture.overflow is extra_sample
        await router.handle_device("channel", "pair", _control("talk.end"))
        await router.wait_idle()
        if extra_sample or extra_character:
            assert sent == []
            assert speech.synthesized
        else:
            assert len(sent) == 1
            assert sent[0]["type"] == "link.turn.request"
            assert sent[0]["text"] == transcript
        assert speech.transcribed == ([] if extra_sample else [pcm])

    asyncio.run(exercise())
    asyncio.run(exercise(extra_sample=True))
    asyncio.run(exercise(extra_character=True))


def test_listen_without_staged_speech_gets_one_cloud_owned_status_reply():
    async def exercise(endpoint):
        speech = FakeSpeech()
        sent = []

        async def send_device(_channel, _pair, frames):
            sent.append(list(frames))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: endpoint,
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        assert await router.handle_device("channel", "pair-a", _control("listen"))
        await router.wait_idle()
        return speech.synthesized, sent

    synthesized, sent = asyncio.run(exercise("ready"))
    assert synthesized == [NOTHING_WAITING_REPLY]
    assert sent

    synthesized, sent = asyncio.run(exercise("busy"))
    assert synthesized == []
    assert sent == []


def test_each_pair_speaks_in_its_voice_and_fixed_clips_are_cached_per_voice():
    class VoiceSpeech(FakeSpeech):
        def synthesize(self, text, voice=None):
            self.synthesized.append((voice, text))
            return f"{voice}:{text}".encode()

    voices = {
        "pair-a": "en-GB-Ryan:DragonHDLatestNeural",
        "pair-b": "en-IN-Neerja:DragonHDLatestNeural",
        "pair-d": "en-GB-Ryan:DragonHDLatestNeural",
    }

    async def exercise():
        speech = VoiceSpeech()
        sent = {}

        async def send_device(_channel, pair, frames):
            sent[pair] = b"".join(
                bytes(frame) for frame in frames if not isinstance(frame, str)
            )
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            voice_for=voices.get,
        )
        for pair in ("pair-a", "pair-b", "pair-c", "pair-d"):
            await router.handle_device("channel-" + pair, pair, _control("listen"))
        await router.wait_idle()
        assert await router._queue_speech("channel-pair-b", "pair-b", "Done.")
        return speech.synthesized, sent

    synthesized, sent = asyncio.run(exercise())
    assert synthesized == [
        (voices["pair-a"], NOTHING_WAITING_REPLY),
        (voices["pair-b"], NOTHING_WAITING_REPLY),
        (None, NOTHING_WAITING_REPLY),
        (voices["pair-b"], "Done."),
    ]
    assert sent["pair-d"] == sent["pair-a"]
    assert sent["pair-c"] == f"None:{NOTHING_WAITING_REPLY}".encode()


def test_mailbox_speech_omits_numbering_for_one_and_numbers_multiple():
    def entry(message_id, text):
        return MailboxEntry(
            message_id=message_id,
            pair="pair-a",
            channel="channel",
            idempotency_key="proactive:" + message_id,
            text=text,
            needs_answer=False,
        )

    one = MailboxBatch(
        batch_id=str(uuid.uuid4()),
        pair="pair-a",
        channel="channel",
        messages=(entry("one", "The build is complete."),),
        unread_count=1,
        remaining_count=0,
        needs_answer=False,
        delivery_state="staged",
        oldest_unread_at="2026-08-27T20:00:00Z",
        oldest_unread_age_seconds=0,
    )
    two = MailboxBatch(
        batch_id=str(uuid.uuid4()),
        pair="pair-a",
        channel="channel",
        messages=(
            entry("one", "The build is complete."),
            entry("two", "The test passed."),
        ),
        unread_count=2,
        remaining_count=0,
        needs_answer=False,
        delivery_state="staged",
        oldest_unread_at="2026-08-27T20:00:00Z",
        oldest_unread_age_seconds=0,
    )

    assert CloudTurnRouter._mailbox_speech(one) == "The build is complete."
    assert CloudTurnRouter._mailbox_speech(two) == (
        "Two new messages. One. The build is complete. Two. The test passed."
    )


def test_mailbox_preloads_one_numbered_snapshot_and_acks_only_that_batch():
    async def exercise():
        speech = FakeSpeech()
        mailbox = FakeMailbox()
        devices = []

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=mailbox,
        )
        for number in range(1, 7):
            await mailbox.store(
                "channel",
                "pair-a",
                str(uuid.uuid4()),
                f"proactive:{number}",
                f"Message {number} finished.",
                number == 2,
            )

        await router.device_ready("channel", "pair-a")
        first = router._playback["pair-a"]
        first_batch = first.mailbox_batch_id
        first_frames = devices[-1][1]
        controls = [
            json.loads(frame) for frame in first_frames if isinstance(frame, str)
        ]
        assert [control["type"] for control in controls] == [
            "audio.start",
            "audio.end",
            "digest",
        ]
        assert controls[-1]["missed"] == 6
        assert controls[-1]["needs_answer"] is True
        assert controls[-1]["dismissible"] is False
        assert speech.synthesized == [
            (
                "Six new messages. Here are the first four. "
                "One. Message 1 finished. Two. Message 2 finished. "
                "Three. Message 3 finished. Four. Message 4 finished."
            )
        ]
        assert [item["state"] for item in mailbox.messages] == [
            "staged",
            "staged",
            "staged",
            "staged",
            "unread",
            "unread",
        ]
        assert all(item.get("notified") for item in mailbox.messages[:4])

        arrival = str(uuid.uuid4())
        await mailbox.store(
            "channel",
            "pair-a",
            arrival,
            "proactive:arrival",
            "Arrived during playback.",
            False,
        )
        await router.handle_device(
            "channel", "pair-a", _control("audio.retry", id=first.id)
        )
        assert router._playback["pair-a"].mailbox_batch_id == first_batch
        assert len(devices) == 2

        await router.handle_device("channel", "pair-a", _control("ack", id=first.id))
        second = router._playback["pair-a"]
        assert second.mailbox_batch_id != first_batch
        assert [item["state"] for item in mailbox.messages[:4]] == ["played"] * 4
        assert [entry.message_id for entry in second.mailbox_entries] == [
            mailbox.messages[4]["message_id"],
            mailbox.messages[5]["message_id"],
            arrival,
        ]
        assert speech.synthesized[-1] == (
            "Three new messages. One. Message 5 finished. "
            "Two. Message 6 finished. Three. Arrived during playback."
        )

    asyncio.run(exercise())


def test_legacy_dismiss_restores_reply_without_deleting_it():
    async def exercise():
        mailbox = FakeMailbox()
        devices = []
        router = CloudTurnRouter(
            speech=FakeSpeech(),
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=lambda _channel, pair, frames: asyncio.sleep(
                0, result=not devices.append((pair, list(frames)))
            ),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=mailbox,
        )
        await mailbox.store(
            "channel",
            "pair-a",
            str(uuid.uuid4()),
            "proactive:dismiss",
            "No longer useful.",
            False,
        )
        await router.device_ready("channel", "pair-a")
        playback_id = router._playback["pair-a"].id

        assert await router.handle_device(
            "channel", "pair-a", _control("dismiss", id=playback_id)
        )
        assert mailbox.messages[0]["state"] == "staged"
        assert router._playback["pair-a"].id == playback_id
        assert json.loads(devices[-1][1][0])["type"] == "audio.start"

    asyncio.run(exercise())


def test_mailbox_tts_failure_never_marks_the_unheard_snapshot_played():
    class FailingDigestSpeech(FakeSpeech):
        def synthesize(self, text):
            self.synthesized.append(text)
            raise SpeechError("speech unavailable")

    async def exercise():
        mailbox = FakeMailbox()
        devices = []
        router = CloudTurnRouter(
            speech=FailingDigestSpeech(),
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=lambda _channel, pair, frames: asyncio.sleep(
                0, result=not devices.append((pair, list(frames)))
            ),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=mailbox,
        )
        await mailbox.store(
            "channel",
            "pair-a",
            str(uuid.uuid4()),
            "proactive:tts-failure",
            "Keep this unread.",
            False,
        )
        await router.device_ready("channel", "pair-a")
        failure = router._playback["pair-a"]
        assert failure.mailbox_batch_id is None
        assert (
            b"".join(frame for frame in devices[-1][1] if isinstance(frame, memoryview))
            == FALLBACK_AUDIO
        )

        await router.handle_device("channel", "pair-a", _control("ack", id=failure.id))
        assert mailbox.messages[0]["state"] == "staged"
        assert mailbox.complete_calls == []

    asyncio.run(exercise())


def test_proactive_link_send_queues_once_in_the_same_durable_mailbox():
    async def exercise():
        mailbox = FakeMailbox()
        link = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        router = CloudTurnRouter(
            speech=FakeSpeech(),
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=mailbox,
        )
        request = {
            "v": 1,
            "type": "link.proactive.send",
            "id": "a" * 32,
            "idempotency_key": "build-42",
            "text": "The build finished.",
        }
        assert await router.handle_link("channel", "pair-a", request)
        request["id"] = "b" * 32
        assert await router.handle_link("channel", "pair-a", request)
        await router.wait_idle()
        return mailbox, link

    mailbox, link = asyncio.run(exercise())

    assert len(mailbox.messages) == 1
    assert mailbox.messages[0]["text"] == "The build finished."
    assert mailbox.messages[0]["idempotency_key"].startswith("proactive:")
    assert mailbox.store_calls[0]["primary_only"] is True
    assert [message[1]["status"] for message in link] == ["queued", "queued"]
    assert link[0][1]["message_id"] == link[1][1]["message_id"]


def test_proactive_link_send_returns_a_bounded_mailbox_rejection():
    class FullMailbox(FakeMailbox):
        async def store(self, *args, **kwargs):
            raise MailboxError("mailbox is full", code="mailbox_full")

    async def exercise():
        link = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        router = CloudTurnRouter(
            speech=FakeSpeech(),
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=FullMailbox(),
        )
        await router.handle_link(
            "channel",
            "pair-a",
            {
                "v": 1,
                "type": "link.proactive.send",
                "id": "c" * 32,
                "idempotency_key": "build-43",
                "text": "Another build finished.",
            },
        )
        await router.wait_idle()
        return link

    link = asyncio.run(exercise())

    assert link == [
        (
            "pair-a",
            {
                "v": 1,
                "type": "link.proactive.receipt",
                "id": "c" * 32,
                "status": "rejected",
                "reason": "mailbox_full",
            },
        )
    ]


def test_proactive_send_to_a_stale_primary_is_rejected_without_mailbox_work():
    async def exercise():
        mailbox = FakeMailbox()
        link = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        router = CloudTurnRouter(
            speech=FakeSpeech(),
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=mailbox,
        )
        await router.handle_link(
            "channel",
            "old-primary",
            {
                "v": 1,
                "type": "link.proactive.send",
                "id": "d" * 32,
                "idempotency_key": "primary-crossing",
                "text": "The build finished.",
            },
            proactive_allowed=False,
        )
        await router.wait_idle()
        return mailbox, link

    mailbox, link = asyncio.run(exercise())

    assert mailbox.store_calls == []
    assert link[0][1] == {
        "v": 1,
        "type": "link.proactive.receipt",
        "id": "d" * 32,
        "status": "rejected",
        "reason": "route_changed",
    }


def test_proactive_fanout_is_bounded_before_mailbox_tasks_accumulate():
    class BlockingMailbox(FakeMailbox):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def store(self, *args, **kwargs):
            self.started.set()
            await self.release.wait()
            return await super().store(*args, **kwargs)

    async def exercise():
        mailbox = BlockingMailbox()
        link = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        router = CloudTurnRouter(
            speech=FakeSpeech(),
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=mailbox,
        )
        for index in range(MAX_PROACTIVE_INFLIGHT + 1):
            await router.handle_link(
                "channel",
                "pair-a",
                {
                    "v": 1,
                    "type": "link.proactive.send",
                    "id": f"{index:032x}",
                    "idempotency_key": f"build-{index}",
                    "text": f"Build {index} finished.",
                },
            )
        await mailbox.started.wait()
        await asyncio.sleep(0)
        assert router._proactive_inflight == {"pair-a": MAX_PROACTIVE_INFLIGHT}
        mailbox.release.set()
        await router.wait_idle()
        return mailbox, link, router

    mailbox, link, router = asyncio.run(exercise())

    assert len(mailbox.store_calls) == MAX_PROACTIVE_INFLIGHT
    assert [message[1]["status"] for message in link].count("rejected") == 1
    assert router._proactive_inflight == {}


def test_proactive_mailbox_waits_for_playback_capacity_then_retries_after_ack():
    async def exercise():
        mailbox = FakeMailbox()
        link = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        router = CloudTurnRouter(
            speech=FakeSpeech(),
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=mailbox,
        )
        await router.device_ready("channel", "pair-a")
        for index in range(MAX_PLAYBACKS_PER_PAIR):
            assert await router._queue_speech(
                "channel", "pair-a", f"Existing answer {index}."
            )
        await router.handle_link(
            "channel",
            "pair-a",
            {
                "v": 1,
                "type": "link.proactive.send",
                "id": "e" * 32,
                "idempotency_key": "capacity-retry",
                "text": "The detached build finished.",
            },
        )
        await router.wait_idle()
        stored = mailbox.messages[0]
        assert stored["state"] == "unread"
        assert not any(
            any(
                entry.message_id == stored["message_id"]
                for entry in item.mailbox_entries
            )
            for item in (
                router._playback["pair-a"],
                *router._playback_waiting["pair-a"],
            )
        )

        await router.handle_device(
            "channel",
            "pair-a",
            _control("ack", id=router._playback["pair-a"].id),
        )
        assert stored["state"] == "staged"
        assert any(
            any(
                entry.message_id == stored["message_id"]
                for entry in item.mailbox_entries
            )
            for item in (
                router._playback["pair-a"],
                *router._playback_waiting["pair-a"],
            )
        )
        return link

    link = asyncio.run(exercise())

    assert any(message[1]["status"] == "queued" for message in link)


def test_agent_result_survives_device_disconnect_until_exact_mailbox_ack():
    async def exercise():
        speech = FakeSpeech()
        mailbox = FakeMailbox()
        link = []
        devices = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=mailbox,
        )

        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"first mic")
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()
        turn_id = link[0][1]["id"]

        await router.device_gone("pair-a")
        await router.handle_link(
            "channel",
            "pair-a",
            {
                "v": 1,
                "type": "link.turn.result",
                "id": turn_id,
                "text": "The build is complete.",
                "needs_answer": True,
            },
        )
        await router.wait_idle()

        assert devices == []
        assert len(mailbox.messages) == 1
        stored = mailbox.messages[0]
        assert stored["state"] == "unread"
        assert mailbox.store_calls == [
            {
                "channel": "channel",
                "pair": "pair-a",
                "message_id": str(uuid.UUID(hex=turn_id)),
                "idempotency_key": "turn:" + turn_id,
                "text": "The build is complete.",
                "needs_answer": True,
                "primary_only": False,
            }
        ]

        await router.device_ready("channel", "pair-a")
        await router.wait_idle()
        first_frames = devices[-1][1]
        first_start = next(
            json.loads(frame)
            for frame in first_frames
            if isinstance(frame, str) and json.loads(frame)["type"] == "audio.start"
        )
        assert first_start["id"] == "c_" + uuid.UUID(stored["batch_id"]).hex
        assert stored["state"] == "staged"
        assert (
            b"".join(
                frame
                for frame in first_frames
                if isinstance(frame, (bytes, memoryview))
            )
            == b"pcm:The build is complete."
        )

        await router.device_gone("pair-a")
        await router.device_ready("channel", "pair-a")
        await router.wait_idle()
        repeated_start = next(
            json.loads(frame)
            for frame in devices[-1][1]
            if isinstance(frame, str) and json.loads(frame)["type"] == "audio.start"
        )
        assert repeated_start["id"] == first_start["id"]
        assert stored["state"] == "staged"

        await router.handle_device(
            "other-channel", "pair-b", _control("ack", id=first_start["id"])
        )
        assert stored["state"] == "staged"
        await router.handle_device(
            "channel", "pair-a", _control("ack", id=first_start["id"])
        )
        assert stored["state"] == "played"
        assert stored["text"] is None

        await router.handle_device(
            "channel", "pair-a", _control("ack", id=first_start["id"])
        )
        assert stored["state"] == "played"
        assert mailbox.complete_calls[-2:] == [
            ("channel", "pair-a", stored["batch_id"]),
            ("channel", "pair-a", stored["batch_id"]),
        ]

    asyncio.run(exercise())


def test_provider_interaction_uses_a_distinct_short_lived_mailbox_contract():
    async def exercise():
        speech = FakeSpeech()
        mailbox = FakeMailbox()
        link = []
        devices = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=mailbox,
        )
        await router.device_ready("channel", "pair-a")
        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"first mic")
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()
        turn_id = next(
            payload["id"]
            for _pair, payload in link
            if payload.get("type") == "link.turn.request"
        )

        await router.handle_link(
            "channel",
            "pair-a",
            {
                "v": 1,
                "type": "link.turn.result",
                "id": turn_id,
                "text": "Codex wants permission. Say yes or no.",
                "needs_answer": True,
                "provider_interaction_ttl_seconds": 540,
            },
        )
        await router.wait_idle()

        assert mailbox.store_calls == [
            {
                "channel": "channel",
                "pair": "pair-a",
                "message_id": str(uuid.UUID(hex=turn_id)),
                "idempotency_key": "turn:" + turn_id,
                "text": "Codex wants permission. Say yes or no.",
                "needs_answer": True,
                "primary_only": False,
                "provider_interaction_ttl_seconds": 540,
            }
        ]
        assert speech.synthesized == ["Codex wants permission. Say yes or no."]
        assert devices
        controls = [
            json.loads(frame) for frame in devices[-1][1] if isinstance(frame, str)
        ]
        assert controls[-1]["needs_answer"] is True

    asyncio.run(exercise())


def test_expired_provider_interaction_is_retracted_from_device_playback():
    async def exercise():
        speech = FakeSpeech()
        mailbox = FakeMailbox()
        message_id = str(uuid.uuid4())
        mailbox.messages.append(
            {
                "channel": "channel",
                "pair": "pair-a",
                "message_id": message_id,
                "idempotency_key": "turn:" + uuid.UUID(message_id).hex,
                "text": "Codex wants permission. Say yes or no.",
                "needs_answer": True,
                "state": "unread",
                "expires_at": (datetime.now(UTC) + timedelta(milliseconds=50))
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        devices = []

        async def send_device(_channel, _pair, frames):
            devices.append(list(frames))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=mailbox,
        )

        await router.device_ready("channel", "pair-a")
        span_id = router._playback["pair-a"].id
        await asyncio.sleep(0.08)

        assert "pair-a" not in router._playback
        controls = [
            json.loads(frame)
            for delivery in devices
            for frame in delivery
            if isinstance(frame, str)
        ]
        retraction = [
            control
            for control in controls
            if control.get("id") == span_id
            and control.get("type") in {"audio.start", "audio.end"}
        ][-2:]
        assert retraction == [
            {"v": 1, "type": "audio.start", "id": span_id},
            {"v": 1, "type": "audio.end", "id": span_id, "complete": False},
        ]
        assert controls[-1]["type"] == "digest"
        assert controls[-1]["missed"] == 0

    asyncio.run(exercise())


def test_concurrent_device_ready_stages_one_stable_mailbox_playback():
    async def exercise():
        speech = FakeSpeech()
        mailbox = FakeMailbox()
        message_id = str(uuid.uuid4())
        await mailbox.store(
            "channel",
            "pair-a",
            message_id,
            "turn:" + uuid.UUID(message_id).hex,
            "One durable answer.",
            False,
        )
        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=mailbox,
        )

        await asyncio.gather(
            router.device_ready("channel", "pair-a"),
            router.device_ready("channel", "pair-a"),
        )
        await router.wait_idle()

        batch_id = mailbox.messages[0]["batch_id"]
        assert router._playback["pair-a"].id == "c_" + uuid.UUID(batch_id).hex
        assert not router._playback_waiting.get("pair-a")
        assert speech.synthesized == ["One durable answer."]

    asyncio.run(exercise())


def test_reconnect_during_mailbox_store_stages_result_without_another_reconnect():
    async def exercise():
        mailbox = BlockingStoreMailbox()
        link = []
        devices = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def send_device(channel, pair, frames):
            devices.append((channel, pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=FakeSpeech(),
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=mailbox,
        )
        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"first mic")
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()
        turn_id = link[0][1]["id"]
        await router.handle_link(
            "channel",
            "pair-a",
            {
                "v": 1,
                "type": "link.turn.result",
                "id": turn_id,
                "text": "The build is complete.",
            },
        )
        await mailbox.store_started.wait()
        await router.device_gone("pair-a")
        reconnect = asyncio.create_task(router.device_ready("channel", "pair-a"))
        await asyncio.sleep(0)
        mailbox.release_store.set()
        await reconnect
        await router.wait_idle()

        assert len(devices) == 1
        assert devices[0][:2] == ("channel", "pair-a")
        assert [
            entry.message_id for entry in router._playback["pair-a"].mailbox_entries
        ] == [str(uuid.UUID(hex=turn_id))]

    asyncio.run(exercise())


def test_route_move_during_mailbox_store_returns_one_failure_on_new_route():
    async def exercise():
        mailbox = BlockingStoreMailbox()
        link = []
        devices = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def send_device(channel, pair, frames):
            devices.append((channel, pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=FakeSpeech(),
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
            mailbox=mailbox,
        )
        await router.handle_device("old-channel", "pair-a", _control("talk.start"))
        await router.handle_device("old-channel", "pair-a", b"first mic")
        await router.handle_device("old-channel", "pair-a", _control("talk.end"))
        await router.wait_idle()
        turn_id = link[0][1]["id"]
        await router.handle_link(
            "old-channel",
            "pair-a",
            {
                "v": 1,
                "type": "link.turn.result",
                "id": turn_id,
                "text": "Do not strand this on the old route.",
            },
        )
        await mailbox.store_started.wait()
        await router.route_gone("old-channel", "pair-a", "new-channel")
        mailbox.release_store.set()
        await router.wait_idle()

        assert len(devices) == 1
        assert devices[0][:2] == ("new-channel", "pair-a")
        assert (
            b"".join(
                frame
                for frame in devices[0][2]
                if isinstance(frame, (bytes, memoryview))
            )
            == b"pcm:" + LINK_ERROR_REPLY.encode()
        )

    asyncio.run(exercise())


def test_one_cloud_speech_turn_returns_audio_only_to_its_origin():
    async def exercise():
        speech = FakeSpeech()
        link = []
        devices = []
        presence = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        async def set_busy(channel, busy):
            presence.append((channel, busy))

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=send_device,
            set_busy=set_busy,
            fallback_error_audio=FALLBACK_AUDIO,
        )

        assert await router.handle_device("channel", "pair-a", _control("talk.start"))
        assert await router.handle_device("channel", "pair-a", b"first mic")
        assert await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()

        request = link.pop()
        assert request[0] == "pair-a"
        assert request[1]["type"] == "link.turn.request"
        assert request[1]["text"] == "Build the project."
        turn_id = request[1]["id"]
        assert presence == [("channel", True)]

        assert await router.handle_link(
            "channel",
            "pair-a",
            {
                "v": 1,
                "type": "link.turn.result",
                "id": turn_id,
                "text": "Which folder?",
                "needs_answer": True,
            },
        )
        await router.wait_idle()

        assert presence[-1] == ("channel", False)
        assert speech.transcribed == [b"first mic"]
        assert speech.synthesized == ["Which folder?"]
        assert len(devices) == 1
        pair, frames = devices[0]
        assert pair == "pair-a"
        controls = [json.loads(f) for f in frames if isinstance(f, str)]
        assert controls[-1]["type"] == "digest" and controls[-1]["missed"] == 1
        assert controls[-1]["needs_answer"] is True
        assert controls[-1]["dismissible"] is False
        assert controls[0]["type"] == "audio.start"
        assert (
            b"".join(f for f in frames if isinstance(f, (bytes, memoryview)))
            == b"pcm:Which folder?"
        )
        assert controls[1] == {
            "v": 1,
            "type": "audio.end",
            "id": controls[0]["id"],
            "complete": True,
        }
        assert not any(candidate == "pair-b" for candidate, _ in devices)

        assert await router.handle_device(
            "channel", "pair-a", _control("ack", id=controls[0]["id"])
        )
        assert link == [
            (
                "pair-a",
                {"v": 1, "type": "link.turn.ack", "id": turn_id},
            )
        ]
        assert await router.handle_link(
            "channel",
            "pair-a",
            {"v": 1, "type": "link.turn.acknowledged", "id": turn_id},
        )
        assert not router._pending_link_acks

    asyncio.run(exercise())


def test_each_normalized_link_failure_has_fixed_cloud_owned_speech():
    assert AGENT_ERROR_REPLIES["update_required"] == (
        "Your coding agent needs an update. Open WalkieOne on your computer to update it."
    )

    async def exercise():
        speech = FakeSpeech()
        devices = []

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        for number, (reason, reply) in enumerate(AGENT_ERROR_REPLIES.items()):
            pair = f"pair-{number}"
            await router.handle_device("channel", pair, _control("talk.start"))
            await router.handle_device("channel", pair, b"first mic")
            await router.handle_device("channel", pair, _control("talk.end"))
            await router.wait_idle()
            turn_id = next(
                turn.id for turn in router._active.values() if turn.pair == pair
            )
            assert await router.handle_link(
                "channel",
                pair,
                {
                    "v": 1,
                    "type": "link.turn.error",
                    "id": turn_id,
                    "reason": reason,
                },
            )
            await router.wait_idle()
            assert reply in speech.synthesized

            audio_id = next(
                json.loads(frame)["id"]
                for delivered_pair, frames in devices
                if delivered_pair == pair
                for frame in frames
                if isinstance(frame, str) and json.loads(frame)["type"] == "audio.start"
            )
            await router.handle_device("channel", pair, _control("ack", id=audio_id))

    asyncio.run(exercise())


def test_busy_gate_treats_stop_as_an_ordinary_utterance():
    async def exercise():
        speech = FakeSpeech()
        link = []
        devices = []
        presence = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=send_device,
            set_busy=lambda channel, busy: _append(presence, (channel, busy)),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        for pair, pcm in (
            ("pair-a", b"first mic"),
            ("pair-b", b"busy mic"),
            ("pair-c", b"stop mic"),
        ):
            await router.handle_device("channel", pair, _control("talk.start"))
            await router.handle_device("channel", pair, pcm)
            await router.handle_device("channel", pair, _control("talk.end"))
            await router.wait_idle()

        requests = [
            payload for _, payload in link if payload["type"] == "link.turn.request"
        ]
        cancels = [
            payload for _, payload in link if payload["type"] == "link.turn.cancel"
        ]
        assert len(requests) == 1
        assert not cancels
        assert router.busy("channel")
        assert speech.synthesized == [
            AGENT_BUSY_REPLY
        ]  # cached speech serves both Devices
        assert [pair for pair, _ in devices] == ["pair-b", "pair-c"]
        assert presence == [("channel", True)]

    async def _append(items, item):
        items.append(item)

    asyncio.run(exercise())


def test_stop_cannot_cancel_agent_work_reported_busy_by_link():
    async def exercise():
        speech, link = FakeSpeech(), []
        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _: "busy",
            send_link=lambda _, pair, payload: _record_concurrent(link, payload, True),
            send_device=lambda *_: asyncio.sleep(0, result=True),
            set_busy=lambda *_: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )
        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"stop mic")
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.handle_device("channel", "pair-a", _control("cancel"))
        await router.wait_idle()
        assert not link
        assert speech.synthesized == [AGENT_BUSY_REPLY]

    asyncio.run(exercise())


def test_idle_stop_and_cancel_are_sent_as_ordinary_text():
    async def exercise(word):
        speech, link = FakeSpeech(), []
        speech.heard[b"words"] = word
        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _: "ready",
            send_link=lambda _, pair, payload: _record_concurrent(
                link, json.loads(payload), True
            ),
            send_device=lambda *_: asyncio.sleep(0, result=True),
            set_busy=lambda *_: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )
        await router.handle_device("channel", "pair", _control("talk.start"))
        await router.handle_device("channel", "pair", b"words")
        await router.handle_device("channel", "pair", _control("talk.end"))
        await router.wait_idle()
        assert len(link) == 1
        assert link[0]["type"] == "link.turn.request" and link[0]["text"] == word
        assert router.busy("channel") and not speech.synthesized

    for word in ("Stop.", "Cancel!"):
        asyncio.run(exercise(word))


def test_playback_ack_replays_after_link_reconnect_until_confirmed():
    async def exercise():
        speech = FakeSpeech()
        link = []
        devices = []
        ack_available = False

        async def send_link(_channel, pair, payload):
            message = json.loads(payload)
            link.append((pair, message))
            return message["type"] != "link.turn.ack" or ack_available

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"first mic")
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()
        turn_id = link[0][1]["id"]
        await router.handle_link(
            "channel",
            "pair-a",
            {"v": 1, "type": "link.turn.result", "id": turn_id, "text": "Done."},
        )
        await router.wait_idle()
        audio_id = next(
            json.loads(frame)["id"]
            for _pair, frames in devices
            for frame in frames
            if isinstance(frame, str) and json.loads(frame)["type"] == "audio.start"
        )

        await router.handle_device("channel", "pair-a", _control("ack", id=audio_id))
        assert list(router._pending_link_acks["channel"]) == [("pair-a", turn_id)]

        ack_available = True
        await router.link_ready("channel")
        assert [payload["type"] for _pair, payload in link].count("link.turn.ack") == 2
        assert list(router._pending_link_acks["channel"]) == [("pair-a", turn_id)]

        await router.handle_link(
            "channel",
            "pair-a",
            {"v": 1, "type": "link.turn.acknowledged", "id": turn_id},
        )
        assert not router._pending_link_acks

    asyncio.run(exercise())


def test_pending_link_ack_queue_is_bounded_to_the_mac_result_window():
    async def exercise():
        router = CloudTurnRouter(
            speech=FakeSpeech(),
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=False),
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        for number in range(MAX_PENDING_LINK_ACKS + 1):
            await router._retain_link_ack("channel", "pair-a", f"{number:032x}")

        pending = list(router._pending_link_acks["channel"])
        assert len(pending) == MAX_PENDING_LINK_ACKS
        assert ("pair-a", f"{0:032x}") not in pending
        assert pending[-1] == ("pair-a", f"{MAX_PENDING_LINK_ACKS:032x}")

    asyncio.run(exercise())


def test_live_playback_queue_speaks_one_explicit_refusal_at_its_hard_cap():
    async def exercise():
        speech = FakeSpeech()
        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        accepted = []
        for number in range(MAX_PLAYBACKS_PER_PAIR + 1):
            accepted.append(
                await router._queue_speech("channel", "pair-a", f"message {number}")
            )

        assert accepted == [True] * MAX_PLAYBACKS_PER_PAIR + [False]
        assert speech.synthesized == [
            *(f"message {number}" for number in range(MAX_PLAYBACKS_PER_PAIR)),
            QUEUE_FULL_REPLY,
        ]
        queued = [
            router._playback["pair-a"],
            *router._playback_waiting["pair-a"],
        ]
        assert len(queued) == MAX_PLAYBACKS_PER_PAIR + 1
        assert sum(item.counted for item in queued) == MAX_PLAYBACKS_PER_PAIR
        assert sum(item.overflow for item in queued) == 1
        assert router._overflow_total == 1

        # Further rejected results share the already queued explanation rather
        # than growing a second unbounded overflow queue.
        assert not await router._queue_speech("channel", "pair-a", "another")
        assert speech.synthesized.count(QUEUE_FULL_REPLY) == 1

    asyncio.run(exercise())


def test_process_capacity_rejects_before_agent_and_bounds_audible_refusals(monkeypatch):
    monkeypatch.setattr("turns.MAX_PLAYBACK_BYTES", 16 * (32768 + 1))
    monkeypatch.setattr("turns.MAX_OVERFLOW_PLAYBACK_BYTES", 4 * (32768 + 1))
    monkeypatch.setattr("turns.PLAYBACK_RESERVATION_BYTES", 32768 + 1)
    # Keep each retained reply at the charged size so this fills the budget.
    monkeypatch.setattr(
        FakeSpeech,
        "synthesize",
        lambda self, text: (self.synthesized.append(text), b"x")[1],
    )

    async def exercise():
        speech = FakeSpeech()
        link = []
        devices = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        for number in range(16):
            assert await router._queue_speech(
                "channel", f"normal-{number}", f"normal message {number}"
            )

        synthesized_before_overflow = len(speech.synthesized)
        for number in range(4):
            pair = f"overflow-{number}"
            channel = f"overflow-channel-{number}"
            assert await router.handle_device(channel, pair, _control("talk.start"))
            assert await router.handle_device(channel, pair, b"never transcribed")
            assert await router.handle_device(channel, pair, _control("talk.end"))
        await router.wait_idle()

        assert router._playback_total == 16
        assert router._overflow_total == 4
        assert len(router._playback) == (16 + 4)
        assert len(speech.synthesized) == synthesized_before_overflow + 1
        assert speech.synthesized.count(QUEUE_FULL_REPLY) == 1

        assert speech.synthesized[-1] == QUEUE_FULL_REPLY
        assert speech.transcribed == []
        assert link == []

        assert await router.handle_device(
            "rejected-channel", "overflow-rejected", _control("talk.start")
        )
        assert await router.handle_device(
            "rejected-channel", "overflow-rejected", b"never accepted"
        )
        assert await router.handle_device(
            "rejected-channel", "overflow-rejected", _control("talk.end")
        )
        await router.wait_idle()
        assert router._overflow_total == 4
        assert len(router._playback) == (16 + 4 + 1)
        assert len(speech.synthesized) == synthesized_before_overflow + 1
        assert speech.transcribed == []
        assert link == []
        assert devices[-1][0] == "overflow-rejected"
        emergency = router._playback["overflow-rejected"]
        assert emergency.emergency_token is not None
        assert emergency.audio is router._standard_audio[(None, QUEUE_FULL_REPLY)]
        emergency_frames = next(
            frames for pair, frames in devices if pair == "overflow-rejected"
        )
        emergency_chunks = [
            frame for frame in emergency_frames if isinstance(frame, memoryview)
        ]
        assert emergency_chunks
        assert all(frame.obj is emergency.audio for frame in emergency_chunks)

        # The last-resort lane is one metadata item per connected Pair, but all
        # of them reference the same immutable PCM instead of multiplying a
        # six-megabyte Speech response per User.
        for number in range(20):
            pair = f"emergency-{number}"
            channel = f"emergency-channel-{number}"
            await router.handle_device(channel, pair, _control("talk.start"))
            await router.handle_device(channel, pair, _control("talk.end"))
        await router.wait_idle()
        emergency_items = [
            item for item in router._playback.values() if item.emergency_token
        ]
        assert len(emergency_items) == 21
        assert all(
            item.audio is router._standard_audio[(None, QUEUE_FULL_REPLY)]
            for item in emergency_items
        )
        assert len(speech.synthesized) == synthesized_before_overflow + 1

    asyncio.run(exercise())


def test_route_move_discards_only_that_pairs_stale_link_receipts():
    async def exercise():
        sent = []

        async def send_link(channel, pair, payload):
            sent.append((channel, pair, json.loads(payload)))
            return True

        router = CloudTurnRouter(
            speech=FakeSpeech(),
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )
        router._pending_link_acks["old-channel"] = deque(
            [("pair-a", "a" * 32), ("pair-b", "b" * 32)]
        )

        await router.route_gone("old-channel", "pair-a", "new-channel")

        assert list(router._pending_link_acks["old-channel"]) == [("pair-b", "b" * 32)]
        assert sent == [
            (
                "old-channel",
                "pair-b",
                {"v": 1, "type": "link.turn.ack", "id": "b" * 32},
            )
        ]

    asyncio.run(exercise())


def test_route_removal_releases_an_unfinished_capacity_capture():
    async def exercise():
        router = CloudTurnRouter(
            speech=FakeSpeech(),
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )
        for number in range(MAX_PLAYBACKS_PER_PAIR):
            assert await router._queue_speech(
                "retained-channel", "pair-a", f"retained {number}"
            )

        assert await router.handle_device(
            "removed-channel", "pair-a", _control("talk.start")
        )
        assert router._overflow_total == 1
        assert router.capturing("removed-channel", "pair-a")

        await router.route_gone("removed-channel", "pair-a")

        assert not router.capturing("removed-channel", "pair-a")
        assert router._overflow_total == 0
        assert router._playback_total == MAX_PLAYBACKS_PER_PAIR
        assert len(router._capacity_reservations) == MAX_PLAYBACKS_PER_PAIR

    asyncio.run(exercise())


def test_legacy_cancel_during_recording_does_not_change_the_request():
    async def exercise():
        speech = FakeSpeech()
        link = []
        devices = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )
        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"first mic")
        await router.handle_device("channel", "pair-b", _control("cancel"))
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()

        assert [payload["type"] for _, payload in link] == ["link.turn.request"]
        assert router.busy("channel")
        assert not speech.synthesized and not devices

    asyncio.run(exercise())


def test_link_loss_ends_the_turn_with_cloud_hosted_speech():
    async def exercise():
        speech = FakeSpeech()
        link = []
        devices = []
        presence = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        async def set_busy(channel, busy):
            presence.append((channel, busy))

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=send_device,
            set_busy=set_busy,
            fallback_error_audio=FALLBACK_AUDIO,
        )
        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"first mic")
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()
        await router.link_gone("channel")
        await router.wait_idle()

        assert [payload["type"] for _, payload in link] == ["link.turn.request"]
        assert speech.synthesized == [
            "Your coding agent couldn't finish that. Check WalkieOne on your computer."
        ]
        assert [pair for pair, _ in devices] == ["pair-a"]
        assert presence == [("channel", True), ("channel", False)]

    asyncio.run(exercise())


def test_device_disconnect_during_capture_releases_channel_busy_state():
    async def exercise():
        speech = FakeSpeech()
        link = []
        presence = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=lambda channel, busy: _append(presence, (channel, busy)),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"first mic")
        await router.device_gone("pair-a")
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()

        assert link == []
        assert speech.transcribed == []
        assert not router.busy("channel")
        assert presence == [("channel", True), ("channel", False)]

    async def _append(items, item):
        items.append(item)

    asyncio.run(exercise())


def test_duplicate_talk_start_is_idempotent_for_the_active_capture():
    async def exercise():
        speech = FakeSpeech()
        link = []
        presence = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=lambda channel, busy: _append(presence, (channel, busy)),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"first ")
        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"mic")
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()

        assert speech.transcribed == [b"first mic"]
        assert len(link) == 1
        assert link[0][1]["type"] == "link.turn.request"
        assert presence == [("channel", True)]

    async def _append(items, item):
        items.append(item)

    asyncio.run(exercise())


def test_link_reported_busy_uses_the_same_fixed_cloud_reply():
    async def exercise():
        speech = FakeSpeech()
        link = []
        devices = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "busy",
            send_link=send_link,
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"busy mic")
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()

        assert link == []
        assert speech.synthesized == ["Your coding agent is busy."]
        assert [pair for pair, _ in devices] == ["pair-a"]
        assert not router.busy("channel")

    asyncio.run(exercise())


def test_device_disconnect_discards_unacknowledged_generated_audio():
    async def exercise():
        speech = FakeSpeech()
        link = []
        devices = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"first mic")
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()
        turn_id = link[0][1]["id"]
        await router.handle_link(
            "channel",
            "pair-a",
            {"v": 1, "type": "link.turn.result", "id": turn_id, "text": "Done."},
        )
        await router.wait_idle()
        audio_start = next(
            json.loads(frame)
            for _pair, frames in devices
            for frame in frames
            if isinstance(frame, str) and json.loads(frame)["type"] == "audio.start"
        )

        await router.device_gone("pair-a")

        assert await router.handle_device(
            "channel", "pair-a", _control("ack", id=audio_start["id"])
        )
        assert len(devices) == 1

    asyncio.run(exercise())


def test_delayed_busy_stop_cannot_cancel_a_newer_turn():
    class DelayedSpeech(FakeSpeech):
        def __init__(self):
            super().__init__()
            self.heard[b"new mic"] = "Start the newer task."
            self.entered = threading.Event()
            self.release = threading.Event()

        def synthesize(self, text):
            if text == AGENT_BUSY_REPLY:
                self.entered.set()
                assert self.release.wait(2)
            return super().synthesize(text)

    async def exercise():
        speech = DelayedSpeech()
        link = []
        devices = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"first mic")
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        while len(link) < 1:
            await asyncio.sleep(0)
        first_id = link[0][1]["id"]

        await router.handle_device("channel", "pair-b", _control("talk.start"))
        await router.handle_device("channel", "pair-b", b"stop mic")
        await router.handle_device("channel", "pair-b", _control("talk.end"))
        assert await asyncio.to_thread(speech.entered.wait, 1)

        await router.handle_link(
            "channel",
            "pair-a",
            {"v": 1, "type": "link.turn.result", "id": first_id, "text": "Done."},
        )
        await router.handle_device("channel", "pair-c", _control("talk.start"))
        await router.handle_device("channel", "pair-c", b"new mic")
        await router.handle_device("channel", "pair-c", _control("talk.end"))
        while (
            len([item for item in link if item[1]["type"] == "link.turn.request"]) < 2
        ):
            await asyncio.sleep(0)

        new_id = link[-1][1]["id"]
        speech.release.set()
        await router.wait_idle()

        assert router.busy("channel")
        assert not any(item[1]["type"] == "link.turn.cancel" for item in link)
        assert link[-1][1]["id"] == new_id
        assert AGENT_BUSY_REPLY in speech.synthesized
        assert any(pair == "pair-b" for pair, _frames in devices)

    asyncio.run(exercise())


def test_speech_failure_uses_independent_pre_rendered_cloud_audio():
    class FailedSpeech:
        def transcribe(self, _pcm):
            raise SpeechError("Azure authentication failed")

        def synthesize(self, _text):
            raise SpeechError("Azure authentication failed")

    async def exercise():
        devices = []

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=FailedSpeech(),
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"broken speech")
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()

        assert [pair for pair, _frames in devices] == ["pair-a"]
        assert (
            b"".join(
                frame
                for _pair, frames in devices
                for frame in frames
                if isinstance(frame, (bytes, memoryview))
            )
            == FALLBACK_AUDIO
        )

    asyncio.run(exercise())


def test_pair_moving_channel_keeps_capture_cloud_owned_and_speaks_failure():
    async def exercise():
        speech = FakeSpeech()
        presence = []
        link = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def set_busy(channel, busy):
            presence.append((channel, busy))

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=set_busy,
            fallback_error_audio=FALLBACK_AUDIO,
        )

        await router.handle_device("old-channel", "pair-a", _control("talk.start"))
        await router.handle_device("old-channel", "pair-a", b"first mic")
        await router.route_gone("old-channel", "pair-a", "new-channel")

        assert not router.busy("old-channel")
        assert not router.capturing("old-channel", "pair-a")
        assert router.capturing("new-channel", "pair-a")
        assert await router.handle_device("new-channel", "pair-a", b"late pcm")
        assert await router.handle_device("new-channel", "pair-a", _control("talk.end"))
        await router.wait_idle()

        assert link == []
        assert presence == [
            ("old-channel", True),
            ("old-channel", False),
            ("new-channel", True),
            ("new-channel", False),
        ]
        assert speech.transcribed == []
        assert speech.synthesized == [
            "Your coding agent couldn't finish that. Check WalkieOne on your computer."
        ]

    asyncio.run(exercise())


def test_route_move_immediately_after_talk_end_moves_the_fixed_reply():
    async def exercise():
        speech = FakeSpeech()
        devices = []

        async def send_device(channel, pair, frames):
            devices.append((channel, pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "unavailable",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        await router.handle_device("old-channel", "pair-a", _control("talk.start"))
        await router.handle_device("old-channel", "pair-a", _control("talk.end"))
        # No yield or task wait between talk.end and the authority cutover.
        await router.route_gone("old-channel", "pair-a", "new-channel")
        await router.wait_idle()

        assert [channel for channel, _pair, _frames in devices] == ["new-channel"]
        assert speech.synthesized == [LINK_ERROR_REPLY]
        assert router._playback["pair-a"].channel == "new-channel"

    asyncio.run(exercise())


def test_route_move_invalidates_tts_already_running_for_the_old_route():
    class DelayedSpeech(FakeSpeech):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def synthesize(self, text):
            if text == "Done.":
                self.entered.set()
                assert self.release.wait(2)
            return super().synthesize(text)

    async def exercise():
        speech = DelayedSpeech()
        link = []
        devices = []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        async def send_device(channel, pair, frames):
            devices.append((channel, pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=send_link,
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        await router.handle_device("old-channel", "pair-a", _control("talk.start"))
        await router.handle_device("old-channel", "pair-a", b"first mic")
        await router.handle_device("old-channel", "pair-a", _control("talk.end"))
        await router.wait_idle()
        turn_id = link[0][1]["id"]

        await router.handle_link(
            "old-channel",
            "pair-a",
            {"v": 1, "type": "link.turn.result", "id": turn_id, "text": "Done."},
        )
        assert await asyncio.to_thread(speech.entered.wait, 1)

        await router.route_gone("old-channel", "pair-a", "new-channel")
        speech.release.set()
        await router.wait_idle()

        assert [channel for channel, _pair, _frames in devices] == ["new-channel"]
        assert LINK_ERROR_REPLY in speech.synthesized
        assert "Done." in speech.synthesized
        assert router._playback_total == 1
        assert router._playback["pair-a"].channel == "new-channel"

    asyncio.run(exercise())


def test_route_move_replaces_only_its_exact_inflight_capacity_ownership():
    class DelayedSpeech(FakeSpeech):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def synthesize(self, text):
            if text == QUEUE_FULL_REPLY and not self.entered.is_set():
                self.entered.set()
                assert self.release.wait(2)
            return super().synthesize(text)

    async def exercise():
        speech = DelayedSpeech()
        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=lambda *_args: asyncio.sleep(0, result=True),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )
        for number in range(MAX_PLAYBACKS_PER_PAIR):
            assert await router._queue_speech(
                "retained-channel", "pair-a", f"retained {number}"
            )

        old = asyncio.create_task(
            router._queue_speech("old-channel", "pair-a", "old rejected result")
        )
        assert await asyncio.to_thread(speech.entered.wait, 1)

        await router.route_gone("old-channel", "pair-a", "new-channel")
        speech.release.set()
        assert not await old
        await router.wait_idle()

        items = [
            router._playback["pair-a"],
            *router._playback_waiting["pair-a"],
        ]
        emergency = [item for item in items if item.emergency_token]
        assert len(emergency) == 1
        assert emergency[0].channel == "new-channel"
        assert router._overflow_total == 0
        assert "pair-a" not in router._overflow_pairs
        assert (
            router._emergency_pairs[("pair-a", LINK_ERROR_REPLY)]
            == emergency[0].emergency_token
        )
        assert router._playback_total == MAX_PLAYBACKS_PER_PAIR

    asyncio.run(exercise())


def test_device_disconnect_invalidates_tts_from_the_old_socket_generation():
    class DelayedSpeech(FakeSpeech):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def synthesize(self, text):
            if text == "old response":
                self.entered.set()
                assert self.release.wait(2)
            return super().synthesize(text)

    async def exercise():
        speech = DelayedSpeech()
        devices = []

        async def send_device(channel, pair, frames):
            devices.append((channel, pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        old = asyncio.create_task(
            router._queue_speech("channel", "pair-a", "old response")
        )
        assert await asyncio.to_thread(speech.entered.wait, 1)
        await router.device_gone("pair-a")
        speech.release.set()

        assert not await old
        assert devices == []
        assert router._playback_total == 0
        assert "pair-a" not in router._playback

        assert await router._queue_speech("channel", "pair-a", "new-session response")
        assert [pair for _channel, pair, _frames in devices] == ["pair-a"]
        assert router._playback_total == 1

    asyncio.run(exercise())


def test_link_loss_mid_capture_keeps_the_span_cloud_owned_until_talk_end():
    async def exercise():
        speech = FakeSpeech()
        devices = []

        async def send_device(channel, pair, frames):
            devices.append((channel, pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"first mic")
        await router.link_gone("channel")

        assert router.capturing("channel", "pair-a")
        assert await router.handle_device("channel", "pair-a", b"late pcm")
        assert await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()

        assert speech.transcribed == []
        assert speech.synthesized == [
            "Your coding agent couldn't finish that. Check WalkieOne on your computer."
        ]
        assert [pair for _channel, pair, _frames in devices] == ["pair-a"]
        audio_id = next(
            json.loads(frame)["id"]
            for _channel, _pair, frames in devices
            for frame in frames
            if isinstance(frame, str) and json.loads(frame)["type"] == "audio.start"
        )
        assert router._playback_total == 1
        assert len(router._capacity_reservations) == 1

        assert await router.handle_device(
            "channel", "pair-a", _control("ack", id=audio_id)
        )
        assert router._playback_total == 0
        assert router._capacity_reservations == {}

    asyncio.run(exercise())


def test_released_capture_reservation_is_revalidated_before_playback():
    async def exercise():
        speech = FakeSpeech()
        devices = []

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        await router.handle_device("channel", "pair-a", _control("talk.start"))
        stale = router._captures[("channel", "pair-a")].reservation
        assert stale is not None

        await router.end_hosted_wait(
            "channel", "pair-a", router._captures[("channel", "pair-a")].turn_id
        )
        assert stale.id not in router._capacity_reservations
        assert router._playback_total == 0

        await router.link_gone("channel")
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()

        playback = router._playback["pair-a"]
        assert playback.reservation is not None
        assert playback.reservation.id != stale.id
        assert (
            router._capacity_reservations[playback.reservation.id]
            == playback.reservation
        )
        assert router._playback_total == 1

    asyncio.run(exercise())


def test_saturated_busy_and_unavailable_keep_their_exact_spoken_outcomes():
    async def exercise():
        speech = FakeSpeech()
        devices = []
        endpoint = "busy"

        async def send_device(_channel, pair, frames):
            devices.append((pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: endpoint,
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=send_device,
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )
        for number in range(16):
            assert await router._queue_speech(
                "retained-channel", f"retained-{number}", f"retained {number}"
            )

        await router.handle_device("busy-channel", "busy-pair", _control("talk.start"))
        await router.handle_device("busy-channel", "busy-pair", b"busy mic")
        await router.handle_device("busy-channel", "busy-pair", _control("talk.end"))
        await router.wait_idle()

        endpoint = "unavailable"
        await router.handle_device(
            "unavailable-channel", "unavailable-pair", _control("talk.start")
        )
        await router.handle_device(
            "unavailable-channel", "unavailable-pair", _control("talk.end")
        )
        await router.wait_idle()

        assert AGENT_BUSY_REPLY in speech.synthesized
        assert AGENT_ERROR_REPLIES["unavailable"] in speech.synthesized
        pcm_by_pair = {
            pair: b"".join(
                frame for frame in frames if isinstance(frame, (bytes, memoryview))
            )
            for pair, frames in devices
        }
        assert pcm_by_pair["busy-pair"] == ("pcm:" + AGENT_BUSY_REPLY).encode()
        assert (
            pcm_by_pair["unavailable-pair"]
            == ("pcm:" + AGENT_ERROR_REPLIES["unavailable"]).encode()
        )

    asyncio.run(exercise())


def test_concurrent_turns_keep_origin_ids_presence_and_playback_order():
    async def exercise():
        speech, link, devices, presence = FakeSpeech(), [], [], []

        async def send_link(channel, pair, payload):
            link.append((channel, pair, json.loads(payload)))
            return True

        async def send_device(channel, pair, frames):
            devices.append((channel, pair, list(frames)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _: "ready",
            concurrent_turns=lambda _: True,
            send_link=send_link,
            send_device=send_device,
            set_busy=lambda channel, busy: _record_concurrent(
                presence, (channel, busy)
            ),
            fallback_error_audio=FALLBACK_AUDIO,
        )
        for pair in ("pair-a", "pair-a", "pair-b"):
            await router.handle_device("channel", pair, _control("talk.start"))
            await router.handle_device("channel", pair, b"first mic")
            await router.handle_device("channel", pair, _control("talk.end"))
            await router.wait_idle()
        requests = list(link)
        assert len(requests) == 3
        assert len({message["id"] for _, _, message in requests}) == 3
        await router.handle_device("channel", "pair-a", _control("listen"))
        await router.wait_idle()
        assert not speech.synthesized  # the working light is the status

        async def reply(index, pair=None):
            channel, origin, request = requests[index]
            await router.handle_link(
                channel,
                pair or origin,
                {
                    "v": 1,
                    "type": "link.turn.result",
                    "id": request["id"],
                    "text": f"Answer {index}",
                    "needs_answer": False,
                },
            )
            await router.wait_idle()

        await reply(0, "pair-b")  # a foreign Pair cannot complete this request
        assert len(router._active) == 3 and not devices
        await reply(1)
        first = router._playback["pair-a"]
        assert first.audio == b"pcm:Answer 1"
        assert router.busy("channel") and presence[-1][1]
        await reply(0)
        assert router._playback["pair-a"] is first  # no overwrite while paused
        assert len(router._playback_waiting["pair-a"]) == 1
        await reply(2)
        assert router._playback["pair-b"].audio == b"pcm:Answer 2"
        assert not router.busy("channel") and not presence[-1][1]
        await router.handle_device("channel", "pair-a", _control("ack", id=first.id))
        assert router._playback["pair-a"].audio == b"pcm:Answer 0"
        acks = [entry for entry in link if entry[2]["type"] == "link.turn.ack"]
        assert [entry[2]["id"] for entry in acks] == [requests[1][2]["id"]]
        await reply(1)  # duplicate completion never enqueues a second copy
        assert not router._playback_waiting.get("pair-a")

    asyncio.run(exercise())


def test_concurrent_stop_is_another_request_and_preserves_existing_work():
    async def exercise():
        speech, link = FakeSpeech(), []

        async def send_link(_channel, pair, payload):
            link.append((pair, json.loads(payload)))
            return True

        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _: "ready",
            concurrent_turns=lambda _: True,
            send_link=send_link,
            send_device=lambda *_: asyncio.sleep(0, result=True),
            set_busy=lambda *_: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

        async def talk(pair, audio=b"first mic"):
            await router.handle_device("channel", pair, _control("talk.start"))
            await router.handle_device("channel", pair, audio)
            await router.handle_device("channel", pair, _control("talk.end"))
            await router.wait_idle()

        await talk("pair-a")
        await talk("pair-a")
        old_ids = {message["id"] for _, message in link}
        await router.handle_device("channel", "pair-a", _control("talk.start"))
        await router.handle_device("channel", "pair-a", b"stop mic")
        await talk("pair-b")  # started after the stop recording began
        newest = link[-1][1]["id"]
        await router.handle_device("channel", "pair-a", _control("talk.end"))
        await router.wait_idle()
        assert all(message["type"] == "link.turn.request" for _, message in link)
        assert link[-1][1]["text"] == "Stop."
        assert {turn.id for turn in router._active.values()} == old_ids | {
            newest,
            link[-1][1]["id"],
        }
        assert not speech.synthesized

    asyncio.run(exercise())


def test_stop_at_capacity_does_not_discard_pending_work():
    async def exercise():
        speech, link = FakeSpeech(), []
        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _: "ready",
            concurrent_turns=lambda _: True,
            send_link=lambda _, pair, payload: _record_concurrent(
                link, (pair, json.loads(payload)), True
            ),
            send_device=lambda *_: asyncio.sleep(0, result=True),
            set_busy=lambda *_: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )
        for index in range(MAX_PLAYBACKS_PER_PAIR + 1):
            await router.handle_device("channel", "pair-a", _control("talk.start"))
            await router.handle_device(
                "channel",
                "pair-a",
                b"stop mic" if index == MAX_PLAYBACKS_PER_PAIR else b"first mic",
            )
            await router.handle_device("channel", "pair-a", _control("talk.end"))
            await router.wait_idle()
        assert (
            len(
                [
                    message
                    for _, message in link
                    if message["type"] == "link.turn.request"
                ]
            )
            == MAX_PLAYBACKS_PER_PAIR
        )
        assert router.busy("channel")
        assert speech.synthesized == [QUEUE_FULL_REPLY]
        assert router._playback_total == MAX_PLAYBACKS_PER_PAIR
        assert all(message["type"] == "link.turn.request" for _, message in link)

    asyncio.run(exercise())


async def _record_concurrent(items, item, result=None):
    items.append(item)
    return result


def test_approval_notice_can_play_when_waiting_requests_reserve_every_slot():
    async def exercise():
        mailbox, link = FakeMailbox(), []
        router = CloudTurnRouter(
            speech=FakeSpeech(),
            endpoint_state=lambda _: "ready",
            concurrent_turns=lambda _: True,
            mailbox=mailbox,
            send_link=lambda _, pair, payload: _record_concurrent(
                link, (pair, json.loads(payload)), True
            ),
            send_device=lambda *_: asyncio.sleep(0, result=True),
            set_busy=lambda *_: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )
        for _ in range(MAX_PLAYBACKS_PER_PAIR):
            await router.handle_device("channel", "pair", _control("talk.start"))
            await router.handle_device("channel", "pair", b"first mic")
            await router.handle_device("channel", "pair", _control("talk.end"))
            await router.wait_idle()
        for _, request in link:
            await router.handle_link(
                "channel",
                "pair",
                {
                    "v": 1,
                    "type": "link.turn.status",
                    "id": request["id"],
                    "waiting_for_user": True,
                },
                hosted_connection_id="hosted",
            )
        await mailbox.store(
            "channel",
            "pair",
            str(uuid.uuid4()),
            "muse:approval",
            "Muse needs your approval.",
            False,
        )
        await router.device_ready("channel", "pair")
        await router.wait_idle()
        assert router._playback["pair"].audio == b""
        assert router.speech.synthesized == ["Muse needs your approval."]
        assert router.busy("channel") and not router.working("channel")
        assert router._playback_total == MAX_PLAYBACKS_PER_PAIR
        assert router._overflow_total == 1
        await router.handle_device(
            "channel", "pair", _control("ack", id=router._playback["pair"].id)
        )
        assert router._overflow_total == 0
        assert len(router._active) == MAX_PLAYBACKS_PER_PAIR

    asyncio.run(exercise())


def test_concurrent_disconnect_and_route_retirement_keep_other_work():
    async def exercise():
        router = CloudTurnRouter(
            speech=FakeSpeech(),
            endpoint_state=lambda _: "ready",
            concurrent_turns=lambda _: True,
            send_link=lambda *_: asyncio.sleep(0, result=True),
            send_device=lambda *_: asyncio.sleep(0, result=True),
            set_busy=lambda *_: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )
        for pair in ("pair-a", "pair-a", "pair-b"):
            await router.handle_device("channel", pair, _control("talk.start"))
            await router.handle_device("channel", pair, b"first mic")
            await router.handle_device("channel", pair, _control("talk.end"))
            await router.wait_idle()
        await router.handle_device("channel", "pair-a", _control("talk.start"))
        assert len(router._active) == 4
        await router.device_gone("pair-a")
        assert len(router._active) == 3  # only the unfinished recording is gone
        assert router._playback_total == 3
        await router.route_gone("channel", "pair-a")
        assert [turn.pair for turn in router._active.values()] == ["pair-b"]
        assert router._playback_total == 1
        assert router.busy("channel")
        await router.link_gone("channel")
        await router.wait_idle()
        assert not router.busy("channel")
        assert router._playback["pair-b"].audio == ("pcm:" + LINK_ERROR_REPLY).encode()

    asyncio.run(exercise())


def test_a_speech_blip_does_not_replace_a_fixed_phrase_until_restart():
    class FlakySpeech(FakeSpeech):
        def __init__(self):
            super().__init__()
            self.fail = True

        def synthesize(self, text):
            if self.fail:
                raise SpeechError("token expired")
            return super().synthesize(text)

    async def exercise():
        speech = FlakySpeech()
        router = CloudTurnRouter(
            speech=speech,
            endpoint_state=lambda _channel: "ready",
            send_link=lambda *_args: asyncio.sleep(0, result=True),
            send_device=lambda *_args: asyncio.sleep(0),
            set_busy=lambda *_args: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )
        assert await router.shared_standard_audio(AGENT_BUSY_REPLY) == FALLBACK_AUDIO
        speech.fail = False
        assert (
            await router.shared_standard_audio(AGENT_BUSY_REPLY)
            == ("pcm:" + AGENT_BUSY_REPLY).encode()
        )

    asyncio.run(exercise())
