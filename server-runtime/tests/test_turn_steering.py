"""Voice follow-ups choose the current conversation after transcription."""

import asyncio
import json
import threading

import pytest
from test_turn_router import FALLBACK_AUDIO, FakeSpeech, _control
from turns import (
    AGENT_BUSY_REPLY,
    STEERING_ERROR_REPLY,
    STEERING_UNKNOWN_REPLY,
    CloudTurnRouter,
)


class Conversation:
    def __init__(self, *, enabled=True, status="accepted", hosted=False):
        self.speech = FakeSpeech()
        self.sent = []
        self.status = status
        self.enabled = enabled
        self.on_steer = None
        self.router = CloudTurnRouter(
            speech=self.speech,
            endpoint_state=lambda _: "ready",
            steering_enabled=lambda _: self.enabled,
            hosted_assistant=lambda _: hosted,
            send_link=self.send,
            send_device=lambda *_: asyncio.sleep(0, result=True),
            set_busy=lambda *_: asyncio.sleep(0),
            fallback_error_audio=FALLBACK_AUDIO,
        )

    async def send(self, channel, pair, payload):
        message = json.loads(payload)
        self.sent.append(message)
        if message["type"] == "link.turn.steer":
            if self.on_steer:
                await self.on_steer(message)
            if self.status is not None:
                await self.router.handle_link(
                    channel,
                    pair,
                    {
                        "v": 1,
                        "type": "link.turn.steered",
                        "id": message["id"],
                        "turn_id": message["turn_id"],
                        "status": self.status,
                    },
                )
        return True

    async def talk(self, pcm, pair="pair", *, wait=True):
        for frame in (_control("talk.start"), pcm, _control("talk.end")):
            await self.router.handle_device("channel", pair, frame)
        if wait:
            await asyncio.wait_for(self.router.wait_idle(), 2)

    async def finish(self, identifier):
        await self.router.handle_link(
            "channel",
            "pair",
            {
                "v": 1,
                "type": "link.turn.result",
                "id": identifier,
                "text": "Finished.",
                "needs_answer": False,
            },
        )


def test_second_press_steers_same_turn_and_stop_is_ordinary_text():
    async def exercise():
        conversation = Conversation()
        await conversation.talk(b"first mic")
        root = conversation.sent[0]["id"]
        await conversation.talk(b"stop mic")
        followup = conversation.sent[1]
        assert followup["type"] == "link.turn.steer"
        assert followup["turn_id"] == root
        assert followup["text"] == "Stop."
        assert conversation.router.busy("channel")
        assert conversation.speech.synthesized == []
        await conversation.finish(root)
        await conversation.router.wait_idle()
        assert conversation.speech.synthesized == ["Finished."]
        assert not conversation.router.busy("channel")
        assert not conversation.router._steering_waiters

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "status,spoken",
    [
        ("unknown", STEERING_UNKNOWN_REPLY),
        ("rejected", STEERING_ERROR_REPLY),
        (None, STEERING_UNKNOWN_REPLY),
    ],
)
def test_uncertain_or_rejected_input_never_replays_original(
    monkeypatch, status, spoken
):
    monkeypatch.setattr("turns.STEERING_RECEIPT_SECONDS", 0.02)

    async def exercise():
        conversation = Conversation(status=status)
        await conversation.talk(b"first mic")
        await conversation.talk(b"busy mic")
        assert [row["type"] for row in conversation.sent] == [
            "link.turn.request",
            "link.turn.steer",
        ]
        assert conversation.speech.synthesized == [spoken]
        assert conversation.router.busy("channel")
        assert not conversation.router._steering_waiters

    asyncio.run(exercise())


def test_completion_during_transcription_starts_followup_as_next_turn():
    async def exercise():
        conversation = Conversation()
        await conversation.talk(b"first mic")
        started, release = threading.Event(), threading.Event()

        def transcribe(_):
            started.set()
            assert release.wait(2)
            return "Follow-up after completion"

        conversation.speech.transcribe = transcribe
        await conversation.talk(b"busy mic", wait=False)
        assert await asyncio.to_thread(started.wait, 1)
        try:
            await conversation.finish(conversation.sent[0]["id"])
        finally:
            release.set()
        await asyncio.wait_for(conversation.router.wait_idle(), 2)
        requests = [
            row for row in conversation.sent if row["type"] == "link.turn.request"
        ]
        assert len(requests) == 2
        assert requests[1]["text"] == "Follow-up after completion"
        assert requests[0]["id"] != requests[1]["id"]
        assert not any(row["type"] == "link.turn.steer" for row in conversation.sent)

    asyncio.run(exercise())


def test_explicit_inactive_after_result_allows_one_new_turn():
    async def exercise():
        conversation = Conversation(status="inactive")
        await conversation.talk(b"first mic")
        conversation.on_steer = lambda row: conversation.finish(row["turn_id"])
        await conversation.talk(b"busy mic")
        requests = [
            row for row in conversation.sent if row["type"] == "link.turn.request"
        ]
        assert len(requests) == 2
        assert requests[1]["text"] == "Is it done?"

    asyncio.run(exercise())


def test_other_pair_cannot_steer_current_conversation():
    async def exercise():
        conversation = Conversation()
        await conversation.talk(b"first mic")
        await conversation.talk(b"stop mic", pair="other-pair")
        assert len(conversation.sent) == 1
        assert conversation.speech.synthesized == [AGENT_BUSY_REPLY]

    asyncio.run(exercise())


def test_followups_keep_spoken_order_while_receipt_is_pending():
    async def exercise():
        conversation = Conversation()
        await conversation.talk(b"first mic")
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_receipt(_):
            entered.set()
            await release.wait()

        conversation.on_steer = hold_receipt
        await conversation.talk(b"busy mic", wait=False)
        await asyncio.wait_for(entered.wait(), 1)
        await conversation.talk(b"stop mic", wait=False)
        await asyncio.sleep(0)
        assert len(conversation.sent) == 2
        release.set()
        await asyncio.wait_for(conversation.router.wait_idle(), 2)
        assert [row["text"] for row in conversation.sent] == [
            "Build the project.",
            "Is it done?",
            "Stop.",
        ]

    asyncio.run(exercise())


def test_legacy_link_keeps_busy_admission():
    async def exercise():
        conversation = Conversation(enabled=False)
        await conversation.talk(b"first mic")
        await conversation.talk(b"stop mic")
        assert len(conversation.sent) == 1
        assert conversation.speech.transcribed == [b"first mic"]
        assert conversation.speech.synthesized == [AGENT_BUSY_REPLY]

    asyncio.run(exercise())


def test_device_reconnect_can_steer_its_existing_work():
    async def exercise():
        conversation = Conversation()
        await conversation.talk(b"first mic")
        root = conversation.sent[0]["id"]
        await conversation.router.device_gone("pair")
        await conversation.talk(b"stop mic")
        assert conversation.sent[1]["type"] == "link.turn.steer"
        assert conversation.sent[1]["turn_id"] == root
        assert conversation.speech.synthesized == []

    asyncio.run(exercise())


def test_input_waiting_for_transcription_is_discarded_on_device_disconnect():
    async def exercise():
        conversation = Conversation()
        await conversation.talk(b"first mic")
        entered, release = threading.Event(), threading.Event()

        def transcribe(_):
            entered.set()
            assert release.wait(2)
            return "Stale follow-up"

        conversation.speech.transcribe = transcribe
        await conversation.talk(b"stop mic", wait=False)
        assert await asyncio.to_thread(entered.wait, 1)
        try:
            await conversation.router.device_gone("pair")
        finally:
            release.set()
        await asyncio.wait_for(conversation.router.wait_idle(), 2)
        assert len(conversation.sent) == 1
        assert not conversation.router._speech_inflight
        assert conversation.router.busy("channel")

    asyncio.run(exercise())


def test_wrong_pair_or_target_cannot_acknowledge_followup(monkeypatch):
    monkeypatch.setattr("turns.STEERING_RECEIPT_SECONDS", 0.02)

    async def exercise():
        conversation = Conversation(status=None)
        await conversation.talk(b"first mic")

        async def wrong_receipts(row):
            receipt = {
                "v": 1,
                "type": "link.turn.steered",
                "id": row["id"],
                "turn_id": row["turn_id"],
                "status": "accepted",
            }
            await conversation.router.handle_link("channel", "other-pair", receipt)
            receipt["turn_id"] = "f" * 32
            await conversation.router.handle_link("channel", "pair", receipt)

        conversation.on_steer = wrong_receipts
        await conversation.talk(b"stop mic")
        assert conversation.speech.synthesized == [STEERING_UNKNOWN_REPLY]

    asyncio.run(exercise())
