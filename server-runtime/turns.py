"""Cloud-owned speech turn lifecycle, independent from relay authentication.

The relay supplies three narrow callbacks for already-authorized writes. This
module owns transient capture, correlated Agent turns per Channel, and one
in-memory playback span per Device until its exact acknowledgment arrives.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import uuid
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial

from delivery_types import MailboxError
from speech import MAX_AUDIO_BYTES, MAX_SYNTHESIZED_BYTES, SpeechAdapter, SpeechError

AGENT_BUSY_REPLY = "Your coding agent is busy."
HOSTED_BUSY_REPLY = "Your agent is busy."
NOTHING_WAITING_REPLY = "Nothing waiting."
QUEUE_FULL_REPLY = "Too many answers are waiting. Listen before asking again."
NO_SPEECH_REPLY = "Sorry, I didn't catch that."
SPEECH_ERROR_REPLY = "I couldn't understand that. Please try again."
LINK_ERROR_REPLY = (
    "Your coding agent couldn't finish that. Check WalkieOne on your computer."
)
HOSTED_OFFLINE_REPLY = "Your assistant is not connected. Reconnect it and try again."
HOSTED_ERROR_REPLY = (
    "I lost the connection to your assistant. It may still be working. "
    "Check your assistant before asking again."
)
AGENT_ERROR_REPLIES = {
    "unavailable": (
        "Your coding agent is not connected. Open WalkieOne on your computer."
    ),
    "authentication_expired": (
        "Your coding agent needs you to sign in. Open WalkieOne on your computer."
    ),
    "update_required": (
        "Your coding agent needs an update. Open WalkieOne on your computer to update it."
    ),
    "permission_blocked": (
        "This needs approval on your computer. Open WalkieOne there to review "
        "permission settings or the system prompt, then ask again."
    ),
    "process_failed": (
        "Your coding agent stopped unexpectedly. Open WalkieOne on your computer."
    ),
    "request_failed": (
        "Your coding agent couldn't finish. Check your computer for a prompt "
        "or error in WalkieOne, then ask again."
    ),
    "response_too_long": (
        "Your coding agent couldn't produce a short spoken response. Open WalkieOne "
        "on your computer."
    ),
}
NORMALIZED_AGENT_FAILURES = frozenset(AGENT_ERROR_REPLIES)
STANDARD_REPLIES = frozenset(
    {
        AGENT_BUSY_REPLY,
        HOSTED_BUSY_REPLY,
        NOTHING_WAITING_REPLY,
        QUEUE_FULL_REPLY,
        NO_SPEECH_REPLY,
        SPEECH_ERROR_REPLY,
        LINK_ERROR_REPLY,
        HOSTED_OFFLINE_REPLY,
        HOSTED_ERROR_REPLY,
        *AGENT_ERROR_REPLIES.values(),
    }
)
MAX_TRANSCRIPT_CHARS = 60_000  # 15-minute input; keep Link/hosted bounds in sync
MAX_RESULT_CHARS = 4_000
MAX_PROACTIVE_CHARS = 1_200
PLAYBACK_CHUNK = 4_096
MAX_PLAYBACKS_PER_PAIR = 4
# Reserve peak rendering space, then charge retained audio plus bounded metadata.
MAX_PLAYBACK_BYTES = 256 * 1024 * 1024
MAX_OVERFLOW_PLAYBACK_BYTES = 4 * 1024 * 1024
PLAYBACK_METADATA_BYTES = 32 * 1024
PLAYBACK_RESERVATION_BYTES = MAX_SYNTHESIZED_BYTES + PLAYBACK_METADATA_BYTES
LINK_RECONNECT_GRACE = 120
LINK_ACK_TTL = 12 * 60 * 60
MAX_PENDING_LINK_ACKS = 50
MAX_PENDING_INPUTS = 4
STEERING_RECEIPT_SECONDS = 30
STEERING_ERROR_REPLY = "I couldn't pass that follow-up to your agent. Please try again."
STEERING_UNKNOWN_REPLY = (
    "I couldn't confirm that your agent received that follow-up. "
    "It may still be working."
)
MAX_PROACTIVE_INFLIGHT = 4
MAX_DIGEST_MESSAGES = 4
MAX_DIGEST_TEXT_CHARS = 1_200
PROACTIVE_MESSAGE_NAMESPACE = uuid.UUID("681720a5-300e-4ff8-8e8e-f56f332e05fc")


@dataclass
class _CapacityReservation:
    id: str
    pair: str
    overflow: bool
    size: int = PLAYBACK_RESERVATION_BYTES


@dataclass
class _Turn:
    id: str
    pair: str
    generation: int
    reservation: _CapacityReservation | None
    waiting_for_user: bool = False


@dataclass
class _LinkGrace:
    link_id: str
    connected: bool = False
    tasks: set = field(default_factory=set)


@dataclass
class _Capture:
    turn_id: str | None
    mode: str
    audio: bytearray
    generation: int
    reservation: _CapacityReservation | None = None
    overflow: bool = False


@dataclass
class _Playback:
    channel: str
    id: str
    audio: bytes
    turn_id: str | None = None
    needs_answer: bool = False
    reservation: _CapacityReservation | None = None
    emergency_token: str | None = None
    emergency_text: str | None = None
    mailbox_batch_id: str | None = None
    mailbox_entries: tuple = ()
    mailbox_unread_count: int | None = None
    expires_at: datetime | None = None

    @property
    def counted(self) -> bool:
        return self.reservation is not None and not self.reservation.overflow

    @property
    def overflow(self) -> bool:
        return self.reservation is not None and self.reservation.overflow


class CloudTurnRouter:
    """Deep boundary for Device audio -> Agent text -> Device audio."""

    def __init__(
        self,
        *,
        speech: SpeechAdapter,
        endpoint_state: Callable[[str], str],
        send_link: Callable[[str, str, str], Awaitable[bool]],
        send_device: Callable[
            [str, str, Iterable[str | bytes | memoryview]], Awaitable[bool]
        ],
        set_busy: Callable[[str, bool], Awaitable[None]],
        fallback_error_audio: bytes,
        mailbox=None,
        hosted_assistant: Callable[[str], bool] | None = None,
        voice_for: Callable[[str], str | None] | None = None,
        concurrent_turns: Callable[[str], bool] | None = None,
        steering_enabled: Callable[[str], bool] | None = None,
        steering_target: Callable[[str], str | None] | None = None,
        speech_executor: Executor | None = None,
    ) -> None:
        self.speech = speech
        self.speech_executor = speech_executor
        # A Pair's chosen voice; None keeps the adapter default.
        self.voice_for = voice_for or (lambda _pair: None)
        self.endpoint_state = endpoint_state
        self.send_link = send_link
        self.send_device = send_device
        self.set_busy = set_busy
        self._published_working: dict[str, bool] = {}
        if not fallback_error_audio:
            raise ValueError("fallback error audio must be non-empty")
        self.fallback_error_audio = fallback_error_audio
        self.mailbox = mailbox
        self.hosted_assistant = hosted_assistant or (lambda _channel: False)
        self.concurrent_turns = concurrent_turns or (lambda _channel: False)
        self.steering_enabled = steering_enabled or (lambda _channel: False)
        self.steering_target = steering_target or (lambda _channel: None)
        self._input_locks: dict[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._steering_waiters: dict[str, tuple[str, str, str, asyncio.Future]] = {}
        self._active: dict[tuple[str, str], _Turn] = {}
        self._captures: dict[tuple[str, str], _Capture] = {}
        self._playback: dict[str, _Playback] = {}
        self._playback_waiting: dict[str, deque[_Playback]] = {}
        self._playback_expiry_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._lock = asyncio.Lock()
        # Holders and waiters keep a strong reference. Weak registries prune
        # idle entries without replacing a lock that another task still owns.
        self._output_locks: dict[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._pending_link_acks: dict[str, deque[tuple[str, str]]] = {}
        self._pending_proactive_acks: dict[str, deque[tuple[str, str, str]]] = {}
        self._proactive_inflight: dict[str, int] = {}
        self._link_ack_locks: dict[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._playback_counts: dict[str, int] = {}
        self._playback_total = 0
        self._playback_bytes = 0
        self._overflow_bytes = 0
        self._playback_capacity_lock = asyncio.Lock()
        self._overflow_pairs: dict[str, str] = {}
        self._overflow_total = 0
        self._capacity_reservations: dict[str, _CapacityReservation] = {}
        self._emergency_pairs: dict[tuple[str, str], str] = {}
        self._standard_audio: dict[tuple[str | None, str], bytes] = {}
        self._standard_audio_locks: dict[tuple[str | None, str], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._device_generations: dict[str, int] = {}
        self._generation_sequence = 0
        self._link_grace: dict[str, _LinkGrace] = {}
        self._ack_expiries = {}
        self._connected_devices: set[str] = set()
        self._device_channels: dict[str, str] = {}
        self._mailbox_locks: dict[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._mailbox_wakes: set[str] = set()
        self._uncommitted_acks: dict[str, tuple[str, str]] = {}
        self._mailbox_inflight: dict[tuple[str, str], int] = {}
        self._departed_mailbox_routes: dict[tuple[str, str], str | None] = {}
        self._speech_inflight: dict[tuple[str, str], int] = {}
        self._speech_reservations: dict[
            tuple[str, str], dict[str, _CapacityReservation]
        ] = {}
        self._speech_emergencies: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
        self._departed_speech_routes: dict[tuple[str, str], str | None] = {}
        self._tasks: set[asyncio.Task] = set()
        self.mailbox_rerenders = 0  # Each one is a paid TTS call.

    def busy(self, channel: str) -> bool:
        return any(key[0] == channel for key in self._active)

    def working(self, channel: str) -> bool:
        return (
            any(
                key[0] == channel and not turn.waiting_for_user
                for key, turn in self._active.items()
            )
            or any(key[0] == channel for key in self._speech_inflight)
            or any(key[0] == channel for key in self._mailbox_inflight)
        )

    async def _publish_working(self, channel: str) -> None:
        working = self.working(channel)
        if working == self._published_working.get(channel, False):
            return
        if working:
            self._published_working[channel] = True
        else:
            self._published_working.pop(channel, None)
        await self.set_busy(channel, working)

    async def resume_hosted(self, channel: str, pair: str, turn_id: str) -> None:
        async with self._lock:
            self._active.setdefault(
                (channel, turn_id),
                _Turn(turn_id, pair, self._device_generation(pair), None),
            )
        await self._publish_working(channel)

    async def hosted_delivered(self, channel: str, pair: str, turn_id: str) -> None:
        # The API already committed the reply to the Mailbox. Rendering is the
        # only remaining work, including after a relay process restart.
        route_key = self._track_mailbox(channel, pair)
        try:
            reservation = await self._finish_active(channel, turn_id)
            await self._stage_mailbox_message(channel, pair, reservation=reservation)
        finally:
            self._finish_mailbox(route_key)
            await self._publish_working(channel)

    async def end_hosted_wait(self, channel: str, pair: str, turn_id: str) -> None:
        """Release live waiting silently; the API can still Mailbox a late reply."""
        async with self._lock:
            active = self._active.get((channel, turn_id))
            if active is None or active.pair != pair:
                return
            del self._active[(channel, turn_id)]
        await self._release_reservation(active.reservation)
        await self._publish_working(channel)

    def capturing(self, channel: str, pair: str) -> bool:
        return (channel, pair) in self._captures

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _track_speech(
        self,
        channel: str,
        pair: str,
        reservation: _CapacityReservation | None = None,
    ) -> tuple[str, str]:
        route_key = (channel, pair)
        self._speech_inflight[route_key] = self._speech_inflight.get(route_key, 0) + 1
        if reservation is not None:
            self._speech_reservations.setdefault(route_key, {})[reservation.id] = (
                reservation
            )
        return route_key

    def _device_generation(self, pair: str) -> int:
        if pair not in self._device_generations:
            self._generation_sequence += 1
            self._device_generations[pair] = self._generation_sequence
        return self._device_generations[pair]

    def _mailbox_lock(self, pair: str) -> asyncio.Lock:
        lock = self._mailbox_locks.get(pair)
        if lock is None:
            lock = asyncio.Lock()
            self._mailbox_locks[pair] = lock
        return lock

    def _track_mailbox(self, channel: str, pair: str) -> tuple[str, str]:
        route_key = (channel, pair)
        self._mailbox_inflight[route_key] = self._mailbox_inflight.get(route_key, 0) + 1
        return route_key

    def _finish_mailbox(self, route_key: tuple[str, str]) -> None:
        remaining = self._mailbox_inflight.get(route_key, 1) - 1
        if remaining:
            self._mailbox_inflight[route_key] = remaining
        else:
            self._mailbox_inflight.pop(route_key, None)
            self._departed_mailbox_routes.pop(route_key, None)

    def _same_device_generation(self, pair: str, generation: int) -> bool:
        return self._device_generations.get(pair) == generation

    def _spawn_speech(
        self,
        channel: str,
        pair: str,
        text: str,
        *,
        turn_id: str | None = None,
        needs_answer: bool = False,
        generation: int | None = None,
        reservation: _CapacityReservation | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        # Register before create_task: route replacement can run before the new
        # task receives its first event-loop slice.
        self._track_speech(channel, pair, reservation)
        self._spawn(
            self._queue_speech(
                channel,
                pair,
                text,
                turn_id=turn_id,
                needs_answer=needs_answer,
                _reservation=reservation,
                _generation=(
                    self._device_generation(pair) if generation is None else generation
                ),
                _tracked=True,
                _expires_at=expires_at,
            )
        )

    async def close(self) -> None:
        """Stop transient work when an embedding server shuts down."""
        tasks = set(self._tasks) | set(self._playback_expiry_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_idle(self) -> None:
        """Deterministic test seam; production never waits on Agent work."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    def has_mailbox_playback(self, channel: str, pair: str) -> bool:
        return any(
            item is not None and item.channel == channel and item.mailbox_batch_id
            for item in (
                self._playback.get(pair),
                *self._playback_waiting.get(pair, ()),
            )
        )

    def wake_mailbox(self, channel: str, pair: str) -> None:
        """Coalesce cloud notifications without blocking route/revocation refresh."""
        if pair in self._mailbox_wakes or (
            pair not in self._uncommitted_acks
            and self.has_mailbox_playback(channel, pair)
        ):
            return
        self._mailbox_wakes.add(pair)

        async def stage():
            try:
                await self._stage_mailbox_message(channel, pair)
            finally:
                self._mailbox_wakes.discard(pair)

        self._spawn(stage())

    async def device_ready(self, channel: str, pair: str) -> None:
        """Stage the oldest durable reply for this authenticated Device."""
        self._connected_devices.add(pair)
        self._device_channels[pair] = channel
        await self._stage_mailbox_message(channel, pair)

    @staticmethod
    def _turn_from_idempotency(idempotency_key: str) -> str | None:
        if not idempotency_key.startswith("turn:"):
            return None
        turn_id = idempotency_key.removeprefix("turn:")
        if len(turn_id) != 32:
            return None
        try:
            int(turn_id, 16)
        except ValueError:
            return None
        return turn_id

    @staticmethod
    def _mailbox_batch_from_playback(briefing_id: str) -> str | None:
        if not briefing_id.startswith("c_") or len(briefing_id) != 34:
            return None
        try:
            return str(uuid.UUID(hex=briefing_id[2:]))
        except ValueError:
            return None

    @staticmethod
    def _mailbox_speech(batch) -> str:
        count_words = {
            1: "One",
            2: "Two",
            3: "Three",
            4: "Four",
            5: "Five",
            6: "Six",
            7: "Seven",
            8: "Eight",
            9: "Nine",
            10: "Ten",
            11: "Eleven",
            12: "Twelve",
            13: "Thirteen",
            14: "Fourteen",
            15: "Fifteen",
            16: "Sixteen",
            17: "Seventeen",
            18: "Eighteen",
            19: "Nineteen",
            20: "Twenty",
        }
        item_words = ("One", "Two", "Three", "Four")
        count = batch.unread_count
        if count == 1 and len(batch.messages) == 1:
            return batch.messages[0].text
        count_label = count_words.get(count, str(count))
        parts = [f"{count_label} new messages."]
        if batch.remaining_count:
            batch_label = count_words.get(len(batch.messages), str(len(batch.messages)))
            parts.append(
                "Here is the first one."
                if len(batch.messages) == 1
                else f"Here are the first {batch_label.lower()}."
            )
        parts.extend(
            f"{item_words[index]}. {entry.text}"
            for index, entry in enumerate(batch.messages)
        )
        return " ".join(parts)

    async def _store_mailbox_result(
        self,
        channel: str,
        pair: str,
        turn_id: str,
        text: str,
        needs_answer: bool,
        reservation: _CapacityReservation | None,
        provider_interaction_ttl_seconds: int | None = None,
        *,
        _tracked: bool = False,
        hosted_connection_id: str | None = None,
    ) -> None:
        route_key = (channel, pair)
        if not _tracked:
            self._track_mailbox(channel, pair)
        try:
            async with self._mailbox_lock(pair):
                message_id = str(uuid.UUID(hex=turn_id))
                try:
                    authority = (
                        {"hosted_connection_id": hosted_connection_id}
                        if hosted_connection_id is not None
                        else {}
                    )
                    await self.mailbox.store(
                        channel,
                        pair,
                        message_id,
                        "turn:" + turn_id,
                        text,
                        needs_answer,
                        provider_interaction_ttl_seconds=(
                            provider_interaction_ttl_seconds
                        ),
                        **authority,
                    )
                except MailboxError:
                    if (
                        route_key not in self._departed_mailbox_routes
                        and pair in self._connected_devices
                        and self._device_channels.get(pair) == channel
                    ):
                        await self._queue_speech(
                            channel,
                            pair,
                            LINK_ERROR_REPLY,
                            _generation=self._device_generation(pair),
                            _reservation=reservation,
                        )
                    else:
                        await self._release_reservation(reservation)
                    return
                if (
                    route_key in self._departed_mailbox_routes
                    or pair not in self._connected_devices
                    or self._device_channels.get(pair) != channel
                ):
                    await self._release_reservation(reservation)
                    return
                await self._stage_mailbox_message_locked(
                    channel,
                    pair,
                    reservation=reservation,
                )
        finally:
            self._finish_mailbox(route_key)
            await self._publish_working(channel)

    async def _stage_mailbox_message(
        self,
        channel: str,
        pair: str,
        *,
        reservation: _CapacityReservation | None = None,
    ) -> bool:
        pending = self._uncommitted_acks.get(pair)
        if pending is not None and pending[0] == channel:
            await self._release_reservation(reservation)
            return await self._ack(channel, pair, pending[1])
        route_key = self._track_mailbox(channel, pair)
        try:
            async with self._mailbox_lock(pair):
                return await self._stage_mailbox_message_locked(
                    channel, pair, reservation=reservation
                )
        finally:
            self._finish_mailbox(route_key)
            await self._publish_working(channel)

    async def _stage_mailbox_message_locked(
        self,
        channel: str,
        pair: str,
        *,
        reservation: _CapacityReservation | None = None,
    ) -> bool:
        route_key = (channel, pair)
        if self.mailbox is None or pair not in self._connected_devices:
            await self._release_reservation(reservation)
            return False
        if (
            self._device_channels.get(pair) != channel
            or route_key in self._departed_mailbox_routes
        ):
            await self._release_reservation(reservation)
            return False
        if self.has_mailbox_playback(channel, pair):
            await self._release_reservation(reservation)
            return True
        if not self._reservation_is_active(reservation):
            reservation = await self._reserve_playback(pair)
        if reservation is None and any(
            key[0] == channel and turn.pair == pair and turn.waiting_for_user
            for key, turn in self._active.items()
        ):
            # Pending approvals must remain audible even when all ordinary
            # slots are reserved by their blocked requests. Reuse the bounded
            # overflow byte budget (one refusal per Pair).
            reservation = await self._reserve_overflow(pair)
        if reservation is None:
            # Do not ask the control plane to mark the durable row staged until
            # volatile playback has a real slot. Any exact playback ack below
            # retries this lane after releasing capacity.
            return False
        try:
            batch = await self.mailbox.next_batch(
                channel,
                pair,
                max_messages=MAX_DIGEST_MESSAGES,
                max_text_chars=MAX_DIGEST_TEXT_CHARS,
            )
        except MailboxError:
            if route_key not in self._departed_mailbox_routes:
                await self._queue_speech(
                    channel, pair, LINK_ERROR_REPLY, _reservation=reservation
                )
            else:
                await self._release_reservation(reservation)
            return False
        if (
            pair not in self._connected_devices
            or self._device_channels.get(pair) != channel
            or route_key in self._departed_mailbox_routes
        ):
            await self._release_reservation(reservation)
            return False
        if batch is None:
            await self._release_reservation(reservation)
            return False
        async with self._output_lock(pair):
            existing = [
                candidate
                for candidate in (
                    self._playback.get(pair),
                    *self._playback_waiting.get(pair, ()),
                )
                if candidate is not None
            ]
            if any(
                candidate.mailbox_batch_id == batch.batch_id for candidate in existing
            ):
                await self._release_reservation(reservation)
                return True
        expiries = [
            datetime.fromisoformat(entry.expires_at.replace("Z", "+00:00"))
            for entry in batch.messages
            if entry.expires_at is not None
        ]
        earliest_expiry = min(expiries) if expiries else None
        if earliest_expiry is not None and earliest_expiry <= datetime.now(UTC):
            # Never restage consent speech after its absolute deadline even if
            # a rolling or skewed mailbox implementation returns it once more.
            await self._release_reservation(reservation)
            return False
        digest_text = self._mailbox_speech(batch)
        return await self._queue_speech(
            channel,
            pair,
            digest_text,
            needs_answer=batch.needs_answer,
            _reservation=reservation,
            _playback_id="c_" + uuid.UUID(batch.batch_id).hex,
            _mailbox_batch_id=batch.batch_id,
            _mailbox_entries=batch.messages,
            _mailbox_unread_count=batch.unread_count,
            _expires_at=earliest_expiry,
        )

    async def handle_device(self, channel: str, pair: str, frame: str | bytes) -> bool:
        """Consume cloud-speech frames; return False for Link-owned controls."""
        self._connected_devices.add(pair)
        self._device_channels[pair] = channel
        if isinstance(frame, (bytes, bytearray)):
            async with self._lock:
                capture = self._captures.get((channel, pair))
                if capture is None:
                    # Binary Device frames have no meaning outside a talk span.
                    # Once cloud speech owns this route, consume a late/stray
                    # chunk instead of leaking microphone audio to Link.
                    return True
                if capture.mode not in {"active", "conversation"}:
                    return True
                remaining = MAX_AUDIO_BYTES - len(capture.audio)
                capture.audio.extend(frame[: max(0, remaining)])
                if len(frame) > remaining:
                    capture.overflow = True
            return True

        try:
            message = json.loads(frame)
        except (TypeError, ValueError):
            return False
        if not isinstance(message, dict) or message.get("v") != 1:
            return False
        kind = message.get("type")

        if kind == "talk.start":
            became_busy = False
            generation = self._device_generation(pair)
            async with self._lock:
                # Reliable transports can still deliver a duplicate control at
                # a reconnect boundary. Keep the bytes already captured for
                # this physical press instead of replacing its active Turn
                # with a synthetic busy capture.
                if (channel, pair) in self._captures:
                    return True
                previous = tuple(key[1] for key in self._active if key[0] == channel)
                if (
                    self.steering_enabled(channel)
                    and not self.hosted_assistant(channel)
                    and self.endpoint_state(channel) in {"ready", "busy"}
                ):
                    # Admit speech while work continues. Serialize transcription
                    # and choose its live target only once the words are known.
                    mode = (
                        "conversation"
                        if self._speech_inflight.get((channel, pair), 0)
                        < MAX_PENDING_INPUTS
                        else "capacity"
                    )
                    capture = _Capture(None, mode, bytearray(), generation)
                elif not previous or self.concurrent_turns(channel):
                    endpoint = self.endpoint_state(channel)
                    if endpoint == "ready":
                        hosted = self.hosted_assistant(channel)
                        reservation = (
                            None if hosted else await self._reserve_playback(pair)
                        )
                        pair_full = (
                            self._playback_counts.get(pair, 0)
                            + sum(
                                turn.pair == pair and turn.reservation is None
                                for turn in self._active.values()
                            )
                            >= MAX_PLAYBACKS_PER_PAIR
                        )
                        if (not hosted and reservation is None) or (
                            hosted and pair_full
                        ):
                            reservation = await self._reserve_overflow(pair)
                            capture = _Capture(
                                None,
                                "capacity",
                                bytearray(),
                                generation,
                                reservation,
                            )
                        else:
                            turn = _Turn(
                                secrets.token_hex(16),
                                pair,
                                generation,
                                reservation,
                            )
                            self._active[(channel, turn.id)] = turn
                            capture = _Capture(
                                turn.id,
                                "active",
                                bytearray(),
                                generation,
                                reservation,
                            )
                            became_busy = True
                    elif endpoint == "busy":
                        capture = _Capture(
                            None,
                            "link_busy",
                            bytearray(),
                            generation,
                        )
                    else:
                        capture = _Capture(
                            None,
                            f"agent:{endpoint}"
                            if endpoint in NORMALIZED_AGENT_FAILURES
                            else "unavailable",
                            bytearray(),
                            generation,
                        )
                else:
                    capture = _Capture(
                        previous[0],
                        "busy",
                        bytearray(),
                        generation,
                    )
                self._captures[(channel, pair)] = capture
            if became_busy:
                await self._publish_working(channel)
            return True

        if kind == "talk.end":
            async with self._lock:
                capture = self._captures.pop((channel, pair), None)
            if capture is None:
                return True
            # Register before create_task for the same reason as synthesized
            # output: authority can move before the task's first event-loop
            # slice, and the replacement still owes one explicit response.
            self._track_speech(channel, pair, capture.reservation)
            self._spawn(self._finish_capture(channel, pair, capture, _tracked=True))
            return True

        if kind == "cancel":
            # Retired Device control. Consume legacy frames without forwarding
            # them to Link, changing pending work, or discarding future replies.
            return True

        if kind == "listen":
            # Firmware plays an already-preloaded span locally. It sends this
            # control only when no span is staged, so the cloud owns the small
            # spoken status response that the retired Mac backlog used to make.
            pending = self._uncommitted_acks.get(pair)
            if pending is not None and pending[0] == channel:
                await self._ack(channel, pair, pending[1])
                return True
            async with self._output_lock(pair):
                if pair in self._playback or self._playback_waiting.get(pair):
                    return True
            async with self._lock:
                active = self.busy(channel)
            endpoint = self.endpoint_state(channel)
            if active or endpoint == "busy":
                # Presence shows work on the Device. Do not enqueue a status
                # clip that would hide the real answer behind a busy message.
                return True
            elif endpoint in NORMALIZED_AGENT_FAILURES:
                reply = AGENT_ERROR_REPLIES[endpoint]
            elif endpoint != "ready":
                reply = LINK_ERROR_REPLY
            else:
                reply = NOTHING_WAITING_REPLY
            self._spawn_speech(channel, pair, reply)
            return True

        if kind in {"ack", "audio.retry"}:
            briefing_id = message.get("id")
            if not isinstance(briefing_id, str) or not briefing_id.startswith("c_"):
                return False
            if kind == "ack":
                await self._ack(channel, pair, briefing_id)
            else:
                await self._retry(channel, pair, briefing_id)
            # The c_ namespace is cloud-owned. A duplicate/stale receipt is an
            # idempotent no-op, never a legacy Link payload.
            return True

        if kind == "dismiss":
            briefing_id = message.get("id")
            if not isinstance(briefing_id, str) or not briefing_id.startswith("c_"):
                return False
            await self._dismiss(channel, pair, briefing_id)
            return True

        return False

    async def _finish_capture(
        self,
        channel: str,
        pair: str,
        capture: _Capture,
        *,
        _tracked: bool = False,
    ) -> None:
        if capture.mode == "conversation":
            lock = self._input_locks.setdefault(channel, asyncio.Lock())
            async with lock:
                await self._finish_capture_ordered(
                    channel, pair, capture, _tracked=_tracked
                )
        else:
            await self._finish_capture_ordered(
                channel, pair, capture, _tracked=_tracked
            )

    async def _finish_capture_ordered(
        self,
        channel: str,
        pair: str,
        capture: _Capture,
        *,
        _tracked: bool = False,
    ) -> None:
        route_key = (channel, pair)
        if not _tracked:
            self._track_speech(channel, pair, capture.reservation)

        async def speak(
            text: str,
            reservation: _CapacityReservation | None = None,
        ) -> bool:
            return await self._queue_speech(
                channel,
                pair,
                text,
                _generation=capture.generation,
                _reservation=reservation,
            )

        try:
            if (
                route_key in self._departed_speech_routes
                or not self._same_device_generation(pair, capture.generation)
            ):
                if capture.mode == "active":
                    reservation = await self._finish_active(channel, capture.turn_id)
                    await self._release_reservation(reservation)
                else:
                    await self._release_reservation(capture.reservation)
                return
            if capture.mode == "capacity":
                await speak(QUEUE_FULL_REPLY, capture.reservation)
                return
            if capture.mode == "unavailable":
                await speak(
                    LINK_ERROR_REPLY
                    if not self.hosted_assistant(channel)
                    else HOSTED_OFFLINE_REPLY,
                    capture.reservation,
                )
                return
            if capture.mode.startswith("agent:"):
                failure = capture.mode.removeprefix("agent:")
                await speak(AGENT_ERROR_REPLIES[failure], capture.reservation)
                return
            if capture.mode in {"busy", "link_busy"}:
                await speak(self._busy_reply(channel))
                return
            if capture.overflow:
                if capture.mode == "active":
                    reservation = await self._finish_active(channel, capture.turn_id)
                else:
                    reservation = capture.reservation
                await speak(SPEECH_ERROR_REPLY, reservation)
                return
            try:
                heard = await self._speech_call(
                    self.speech.transcribe, bytes(capture.audio)
                )
            except SpeechError:
                heard = None

            if (
                route_key in self._departed_speech_routes
                or not self._same_device_generation(pair, capture.generation)
            ):
                if capture.mode == "active":
                    reservation = await self._finish_active(channel, capture.turn_id)
                    await self._release_reservation(reservation)
                else:
                    await self._release_reservation(capture.reservation)
                return

            if heard is None:
                reservation = await self._finish_active(channel, capture.turn_id)
                await speak(SPEECH_ERROR_REPLY, reservation)
                return
            heard = heard.strip()
            if not heard:
                reservation = await self._finish_active(channel, capture.turn_id)
                await speak(NO_SPEECH_REPLY, reservation)
                return
            if len(heard) > MAX_TRANSCRIPT_CHARS:
                reservation = await self._finish_active(channel, capture.turn_id)
                await speak(SPEECH_ERROR_REPLY, reservation)
                return
            if capture.mode == "conversation":
                await self._conversation_input(channel, pair, capture, heard)
                return
            async with self._lock:
                active = self._active.get((channel, capture.turn_id))
                if (
                    active is None
                    or active.id != capture.turn_id
                    or active.pair != pair
                    or active.generation != capture.generation
                ):
                    return
            payload = json.dumps(
                {
                    "v": 1,
                    "type": "link.turn.request",
                    "id": capture.turn_id,
                    "text": heard,
                    "language": "en-US",
                },
                separators=(",", ":"),
            )
            if not await self.send_link(channel, pair, payload):
                reservation = await self._finish_active(channel, capture.turn_id)
                await speak(LINK_ERROR_REPLY, reservation)
        finally:
            if capture.reservation is not None:
                tracked_reservations = self._speech_reservations.get(route_key)
                if tracked_reservations is not None:
                    tracked_reservations.pop(capture.reservation.id, None)
                    if not tracked_reservations:
                        self._speech_reservations.pop(route_key, None)
            remaining = self._speech_inflight.get(route_key, 1) - 1
            if remaining:
                self._speech_inflight[route_key] = remaining
            else:
                self._speech_inflight.pop(route_key, None)
                self._departed_speech_routes.pop(route_key, None)
                self._speech_reservations.pop(route_key, None)
                self._speech_emergencies.pop(route_key, None)
            await self._publish_working(channel)

    async def _conversation_input(self, channel, pair, capture, heard):
        async def speak(text, reservation=None):
            return await self._queue_speech(
                channel,
                pair,
                text,
                _generation=capture.generation,
                _reservation=reservation,
            )

        if not self.steering_enabled(channel):
            await speak(STEERING_ERROR_REPLY)
            return
        async with self._lock:
            if not self._same_device_generation(pair, capture.generation):
                return
            active = next(
                (turn for (owner, _), turn in self._active.items() if owner == channel),
                None,
            )
        if active is not None and active.pair != pair:
            await speak(AGENT_BUSY_REPLY)
            return
        # A Mac approval can resume the same provider without a new Device
        # request. Link advertises that busy generation and checks its Pair.
        target = active.id if active is not None else self.steering_target(channel)
        if target is not None:
            identifier = secrets.token_hex(16)
            future = asyncio.get_running_loop().create_future()
            self._steering_waiters[identifier] = (channel, pair, target, future)
            try:
                payload = json.dumps(
                    {
                        "v": 1,
                        "type": "link.turn.steer",
                        "id": identifier,
                        "turn_id": target,
                        "text": heard,
                        "language": "en-US",
                    },
                    separators=(",", ":"),
                )
                try:
                    async with asyncio.timeout(STEERING_RECEIPT_SECONDS):
                        if not await self.send_link(channel, pair, payload):
                            status = "unknown"
                        else:
                            status = await future
                except (TimeoutError, ConnectionError, OSError):
                    status = "unknown"
            finally:
                self._steering_waiters.pop(identifier, None)
            if status == "accepted":
                return
            if status != "inactive":
                await speak(
                    STEERING_UNKNOWN_REPLY
                    if status == "unknown"
                    else STEERING_ERROR_REPLY
                )
                return
            # Only an explicit non-acceptance permits a new turn. Never retry
            # an uncertain send; the provider may already be acting on it.
        if not self._same_device_generation(
            pair, capture.generation
        ) or not self.steering_enabled(channel):
            return
        async with self._lock:
            if self.busy(channel):
                reservation = None
                busy = True
            else:
                busy = False
                reservation = await self._reserve_playback(pair)
                if reservation is not None:
                    turn = _Turn(
                        secrets.token_hex(16), pair, capture.generation, reservation
                    )
                    self._active[(channel, turn.id)] = turn
        if busy:
            await speak(AGENT_BUSY_REPLY)
            return
        if reservation is None:
            await speak(QUEUE_FULL_REPLY)
            return
        await self._publish_working(channel)
        if not self._same_device_generation(
            pair, capture.generation
        ) or not self.steering_enabled(channel):
            reservation = await self._finish_active(channel, turn.id)
            await self._release_reservation(reservation)
            return
        payload = json.dumps(
            {
                "v": 1,
                "type": "link.turn.request",
                "id": turn.id,
                "text": heard,
                "language": "en-US",
            },
            separators=(",", ":"),
        )
        if not await self.send_link(channel, pair, payload):
            reservation = await self._finish_active(channel, turn.id)
            await speak(LINK_ERROR_REPLY, reservation)

    async def _finish_active(
        self, channel: str, turn_id: str | None
    ) -> _CapacityReservation | None:
        async with self._lock:
            active = self._active.get((channel, turn_id))
            if active is None or active.id != turn_id:
                return None
            del self._active[(channel, turn_id)]
        await self._publish_working(channel)
        return active.reservation

    def _busy_reply(self, channel: str) -> str:
        return (
            AGENT_BUSY_REPLY
            if not self.hosted_assistant(channel)
            else HOSTED_BUSY_REPLY
        )

    async def handle_link(
        self,
        channel: str,
        pair: str,
        message: dict,
        *,
        hosted_connection_id: str | None = None,
        proactive_allowed: bool = True,
    ) -> bool:
        kind = message.get("type")
        if kind not in {
            "link.turn.steered",
            "link.turn.status",
            "link.turn.result",
            "link.turn.error",
            "link.turn.acknowledged",
            "link.agent.cancelled",
            "link.proactive.send",
            "link.proactive.acknowledged",
        }:
            return False
        if kind == "link.turn.steered":
            identifier, target, status = (
                message.get("id"),
                message.get("turn_id"),
                message.get("status"),
            )
            if (
                message.get("v") != 1
                or set(message) != {"v", "type", "id", "turn_id", "status"}
                or not all(
                    isinstance(value, str)
                    and len(value) == 32
                    and set(value) <= set("0123456789abcdef")
                    for value in (identifier, target)
                )
                or not isinstance(status, str)
                or status not in {"accepted", "inactive", "rejected", "unknown"}
            ):
                raise ValueError("malformed steering receipt")
            waiter = self._steering_waiters.get(identifier)
            if (
                waiter is not None
                and waiter[:3] == (channel, pair, target)
                and not waiter[3].done()
            ):
                waiter[3].set_result(status)
            return True
        if kind == "link.proactive.acknowledged":
            try:
                message_id = str(uuid.UUID(message.get("id", "")))
            except (AttributeError, ValueError) as error:
                raise ValueError("malformed proactive acknowledgment") from error
            if set(message) != {"v", "type", "id"} or message.get("v") != 1:
                raise ValueError("malformed proactive acknowledgment")
            async with self._lock:
                pending = self._pending_proactive_acks.get(channel)
                if pending is not None:
                    retained = deque(
                        item for item in pending if item[:2] != (pair, message_id)
                    )
                    if retained:
                        self._pending_proactive_acks[channel] = retained
                    else:
                        self._pending_proactive_acks.pop(channel, None)
                if not self._pending_link_acks.get(
                    channel
                ) and not self._pending_proactive_acks.get(channel):
                    self._forget_link_acks(channel)
            return True
        if kind == "link.proactive.send":
            request_id = message.get("id")
            idempotency_key = message.get("idempotency_key")
            text = message.get("text")
            if (
                set(message) != {"v", "type", "id", "idempotency_key", "text"}
                or message.get("v") != 1
                or not isinstance(request_id, str)
                or len(request_id) != 32
                or any(character not in "0123456789abcdef" for character in request_id)
                or not isinstance(idempotency_key, str)
                or not idempotency_key.strip()
                or len(idempotency_key) > 128
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in idempotency_key
                )
                or not isinstance(text, str)
                or not text.strip()
                or len(text) > MAX_PROACTIVE_CHARS
            ):
                raise ValueError("malformed proactive Link send")
            if not proactive_allowed:
                self._spawn(
                    self.send_link(
                        channel,
                        pair,
                        json.dumps(
                            {
                                "v": 1,
                                "type": "link.proactive.receipt",
                                "id": request_id,
                                "status": "rejected",
                                "reason": "route_changed",
                            },
                            separators=(",", ":"),
                        ),
                    )
                )
                return True
            if self._proactive_inflight.get(pair, 0) >= MAX_PROACTIVE_INFLIGHT:
                self._spawn(
                    self.send_link(
                        channel,
                        pair,
                        json.dumps(
                            {
                                "v": 1,
                                "type": "link.proactive.receipt",
                                "id": request_id,
                                "status": "rejected",
                                "reason": "mailbox_full",
                            },
                            separators=(",", ":"),
                        ),
                    )
                )
                return True
            self._proactive_inflight[pair] = self._proactive_inflight.get(pair, 0) + 1
            self._spawn(
                self._store_proactive_send(
                    channel,
                    pair,
                    request_id,
                    idempotency_key.strip(),
                    text.strip(),
                )
            )
            return True
        if kind == "link.agent.cancelled":
            # A delayed acknowledgment from an older relay has no active waiter.
            return True
        turn_id = message.get("id")
        if message.get("v") != 1 or not isinstance(turn_id, str):
            raise ValueError("malformed Link turn result")
        if kind == "link.turn.status":
            if (
                hosted_connection_id is None
                or set(message) != {"v", "type", "id", "waiting_for_user"}
                or not isinstance(message.get("waiting_for_user"), bool)
            ):
                raise ValueError("malformed hosted turn status")
            async with self._lock:
                active = self._active.get((channel, turn_id))
                if active is None or active.pair != pair:
                    return True
                active.waiting_for_user = message["waiting_for_user"]
            await self._publish_working(channel)
            return True
        if kind == "link.turn.acknowledged":
            async with self._lock:
                pending = self._pending_link_acks.get(channel)
                if pending is not None:
                    retained = deque(
                        item for item in pending if item != (pair, turn_id)
                    )
                    if retained:
                        self._pending_link_acks[channel] = retained
                    else:
                        self._pending_link_acks.pop(channel, None)
                if not self._pending_link_acks.get(
                    channel
                ) and not self._pending_proactive_acks.get(channel):
                    self._forget_link_acks(channel)
            return True
        text = message.get("text")
        if kind == "link.turn.result" and (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_RESULT_CHARS
        ):
            raise ValueError("malformed Link turn result")
        needs_answer = message.get("needs_answer", False)
        if kind == "link.turn.result" and not isinstance(needs_answer, bool):
            raise ValueError("malformed Link turn result")
        provider_interaction_ttl_seconds = message.get(
            "provider_interaction_ttl_seconds"
        )
        if kind == "link.turn.result" and (
            provider_interaction_ttl_seconds is not None
            and (
                not isinstance(provider_interaction_ttl_seconds, int)
                or isinstance(provider_interaction_ttl_seconds, bool)
                or not 1 <= provider_interaction_ttl_seconds <= 540
                or not needs_answer
            )
        ):
            raise ValueError("malformed Link turn result")
        reason = message.get("reason")
        if (
            kind == "link.turn.error"
            and reason not in {None, "busy", *NORMALIZED_AGENT_FAILURES}
            and not (hosted_connection_id is not None and reason == "assistant_busy")
        ):
            raise ValueError("malformed Link turn error")
        async with self._lock:
            active = self._active.get((channel, turn_id))
            matches = (
                active is not None and active.id == turn_id and active.pair == pair
            )
            generation = active.generation if matches and active is not None else None
            reservation = active.reservation if matches and active is not None else None
            if matches:
                del self._active[(channel, turn_id)]
        if not matches:
            return True
        if kind == "link.turn.error":
            reply = (
                AGENT_BUSY_REPLY
                if reason == "busy"
                else AGENT_ERROR_REPLIES.get(reason, LINK_ERROR_REPLY)
            )
            if hosted_connection_id is not None:
                reply = (
                    HOSTED_BUSY_REPLY
                    if reason == "assistant_busy"
                    else QUEUE_FULL_REPLY
                    if reason == "busy"
                    else HOSTED_OFFLINE_REPLY
                    if reason == "unavailable"
                    else HOSTED_ERROR_REPLY
                )
            self._spawn_speech(
                channel,
                pair,
                reply,
                generation=generation,
                reservation=reservation,
            )
            return True
        if self.mailbox is None:
            self._spawn_speech(
                channel,
                pair,
                text.strip(),
                turn_id=turn_id,
                needs_answer=needs_answer,
                generation=generation,
                reservation=reservation,
                expires_at=(
                    datetime.now(UTC)
                    + timedelta(seconds=provider_interaction_ttl_seconds)
                    if provider_interaction_ttl_seconds is not None
                    else None
                ),
            )
        else:
            self._track_mailbox(channel, pair)
            self._spawn(
                self._store_mailbox_result(
                    channel,
                    pair,
                    turn_id,
                    text.strip(),
                    needs_answer,
                    reservation,
                    provider_interaction_ttl_seconds,
                    _tracked=True,
                    hosted_connection_id=hosted_connection_id,
                )
            )
        await self._publish_working(channel)
        return True

    async def _store_proactive_send(
        self,
        channel: str,
        pair: str,
        request_id: str,
        idempotency_key: str,
        text: str,
    ) -> None:
        receipt = {
            "v": 1,
            "type": "link.proactive.receipt",
            "id": request_id,
            "status": "rejected",
            "reason": "cloud_unavailable",
        }
        if self.mailbox is None:
            await self.send_link(
                channel, pair, json.dumps(receipt, separators=(",", ":"))
            )
            remaining = self._proactive_inflight.get(pair, 1) - 1
            if remaining:
                self._proactive_inflight[pair] = remaining
            else:
                self._proactive_inflight.pop(pair, None)
            return

        route_key = self._track_mailbox(channel, pair)
        digest = hashlib.sha256(f"{channel}\0{idempotency_key}".encode()).hexdigest()
        stored_key = "proactive:" + digest
        message_id = str(
            uuid.uuid5(
                PROACTIVE_MESSAGE_NAMESPACE,
                f"{channel}:{pair}:{idempotency_key}",
            )
        )
        stage = False
        try:
            async with self._mailbox_lock(pair):
                try:
                    await self.mailbox.store(
                        channel,
                        pair,
                        message_id,
                        stored_key,
                        text,
                        False,
                        primary_only=True,
                    )
                except MailboxError as error:
                    receipt["reason"] = {
                        "mailbox_full": "mailbox_full",
                        "mailbox_idempotency_conflict": "idempotency_conflict",
                        "mailbox_primary_changed": "route_changed",
                    }.get(error.code, "cloud_unavailable")
                else:
                    if route_key in self._departed_mailbox_routes:
                        receipt["reason"] = "route_changed"
                    else:
                        receipt = {
                            "v": 1,
                            "type": "link.proactive.receipt",
                            "id": request_id,
                            "status": "queued",
                            "message_id": message_id,
                        }
                        stage = (
                            pair in self._connected_devices
                            and self._device_channels.get(pair) == channel
                        )
            await self.send_link(
                channel, pair, json.dumps(receipt, separators=(",", ":"))
            )
            if receipt["status"] == "queued" and stage:
                self._spawn(self._stage_mailbox_message(channel, pair))
        finally:
            remaining = self._proactive_inflight.get(pair, 1) - 1
            if remaining:
                self._proactive_inflight[pair] = remaining
            else:
                self._proactive_inflight.pop(pair, None)
            self._finish_mailbox(route_key)
            await self._publish_working(channel)

    async def link_gone(self, channel: str, link_id: str | None = None) -> None:
        if link_id is None:
            await self._drop_link_turns(channel)
            return
        grace = self._link_grace.get(channel)
        if grace is None or grace.link_id != link_id:
            grace = self._link_grace[channel] = _LinkGrace(link_id)
        grace.connected = False
        # Each disconnect owns a deadline for exactly the Turns it interrupted.
        # A reconnect does not cancel it: a Mac that never resends the result
        # must not leave the Channel busy forever.
        held = [(key, turn) for key, turn in self._active.items() if key[0] == channel]
        task = asyncio.create_task(self._expire_link_grace(channel, grace, held))
        grace.tasks.add(task)

    async def _expire_link_grace(self, channel, grace, held) -> None:
        try:
            await asyncio.sleep(LINK_RECONNECT_GRACE)
            await self._drop_link_turns(channel, held)
            if not grace.connected:
                self._forget_link_acks(channel)
        finally:
            grace.tasks.discard(asyncio.current_task())
            if not grace.tasks and self._link_grace.get(channel) is grace:
                del self._link_grace[channel]

    async def _drop_link_turns(self, channel: str, held=None) -> None:
        async with self._lock:
            if held is None:
                active_turns = [
                    self._active.pop(key)
                    for key in list(self._active)
                    if key[0] == channel
                ]
            else:
                active_turns = [
                    self._active.pop(key)
                    for key, turn in held
                    if self._active.get(key) is turn
                ]
            dropped_ids = {turn.id for turn in active_turns}
            capture_ids = set()
            for key, capture in self._captures.items():
                if key[0] != channel:
                    continue
                if held is not None and capture.turn_id not in dropped_ids:
                    continue
                # The same physical press must retain one owner. Keep a tiny
                # tombstone until talk.end so the remaining microphone frames
                # keep one owner and cannot become an orphaned partial span.
                capture_ids.add(capture.turn_id)
                capture.turn_id = None
                capture.mode = "unavailable"
                capture.audio.clear()
                capture.overflow = False
        if active_turns:
            await self._publish_working(channel)
        for active in active_turns:
            if active.id not in capture_ids:
                self._spawn_speech(
                    channel,
                    active.pair,
                    LINK_ERROR_REPLY,
                    generation=active.generation,
                    reservation=active.reservation,
                )

    async def device_gone(self, pair: str) -> None:
        """Discard unfinished recordings owned by a disconnected Device.

        Once ``talk.end`` has handed a Turn to the Agent, a Device disconnect
        must not cancel that work. Only captures still waiting for their end
        marker are abandoned here.
        """
        self._connected_devices.discard(pair)
        self._device_channels.pop(pair, None)
        self._device_generations.pop(pair, None)
        released_channels = set()
        released_reservations: dict[str, _CapacityReservation] = {}
        released_emergencies: dict[str, tuple[str, str]] = {}
        async with self._lock:
            for key in [key for key in self._captures if key[1] == pair]:
                capture = self._captures.pop(key)
                channel = key[0]
                if capture.reservation is not None:
                    released_reservations[capture.reservation.id] = capture.reservation
                active = self._active.get((channel, capture.turn_id))
                if (
                    capture.mode == "active"
                    and active is not None
                    and active.id == capture.turn_id
                    and active.pair == pair
                ):
                    del self._active[(channel, capture.turn_id)]
                    released_channels.add(channel)
                    if active.reservation is not None:
                        released_reservations[active.reservation.id] = (
                            active.reservation
                        )
            for route_key, reservations in self._speech_reservations.items():
                if route_key[1] != pair:
                    continue
                released_reservations.update(reservations)
            for route_key, emergencies in self._speech_emergencies.items():
                if route_key[1] != pair:
                    continue
                released_emergencies.update(emergencies)
        for reservation in released_reservations.values():
            await self._release_reservation(reservation)
        for token, (emergency_pair, emergency_text) in released_emergencies.items():
            self._release_emergency(emergency_pair, emergency_text, token)
        for channel in released_channels:
            await self._publish_working(channel)
        # Generated audio stays live-only. Durable Mailbox text, when present,
        # remains staged and is rendered again for the replacement session.
        async with self._output_lock(pair):
            items = list(self._playback_waiting.get(pair, ()))
            if pair in self._playback:
                items.append(self._playback[pair])
            self._playback.pop(pair, None)
            self._playback_waiting.pop(pair, None)
            for item in items:
                self._cancel_playback_expiry(pair, item)
                await self._release_reservation(item.reservation)
                self._release_emergency(pair, item.emergency_text, item.emergency_token)

    async def route_gone(
        self,
        channel: str,
        pair: str,
        replacement_channel: str | None = None,
        *,
        link_only: bool = False,
    ) -> None:
        """End old authority without splitting or silently losing a turn.

        ``link_only`` means only the active Link changed on the same Channel:
        its unanswered Turns end, but rendered and in-flight replies (including
        durable Mailbox playback) still belong to this Device.
        """
        released = False
        migrated_capture = False
        self._uncommitted_acks.pop(pair, None)
        route_key = (channel, pair)
        released_reservations: dict[str, _CapacityReservation] = {}
        released_emergencies: dict[str, tuple[str, str]] = {}
        async with self._lock:
            if pair in self._connected_devices:
                if replacement_channel is None:
                    self._device_channels.pop(pair, None)
                else:
                    self._device_channels[pair] = replacement_channel
            capture = self._captures.pop(route_key, None)
            if capture is not None and replacement_channel is not None:
                replacement_key = (replacement_channel, pair)
                if replacement_key not in self._captures:
                    capture.turn_id = None
                    capture.mode = "unavailable"
                    capture.audio.clear()
                    capture.overflow = False
                    self._captures[replacement_key] = capture
                    migrated_capture = True
            if capture is not None and capture.reservation is not None:
                released_reservations[capture.reservation.id] = capture.reservation
                capture.reservation = None
            for key, active in list(self._active.items()):
                if key[0] == channel and active.pair == pair:
                    del self._active[key]
                    released = True
                    if active.reservation is not None:
                        released_reservations[active.reservation.id] = (
                            active.reservation
                        )
            pending_acks = self._pending_link_acks.get(channel)
            if pending_acks is not None:
                retained_acks = deque(item for item in pending_acks if item[0] != pair)
                if retained_acks:
                    self._pending_link_acks[channel] = retained_acks
                else:
                    self._pending_link_acks.pop(channel, None)
            pending_proactive = self._pending_proactive_acks.get(channel)
            if pending_proactive is not None:
                retained_proactive = deque(
                    item for item in pending_proactive if item[0] != pair
                )
                if retained_proactive:
                    self._pending_proactive_acks[channel] = retained_proactive
                else:
                    self._pending_proactive_acks.pop(channel, None)
            had_inflight_speech = (
                not link_only and self._speech_inflight.get(route_key, 0) > 0
            )
            if had_inflight_speech:
                self._departed_speech_routes[route_key] = replacement_channel
                released_reservations.update(
                    self._speech_reservations.get(route_key, {})
                )
                released_emergencies.update(self._speech_emergencies.get(route_key, {}))
            had_inflight_mailbox = (
                not link_only and self._mailbox_inflight.get(route_key, 0) > 0
            )
            if had_inflight_mailbox:
                self._departed_mailbox_routes[route_key] = replacement_channel
        for reservation in released_reservations.values():
            await self._release_reservation(reservation)
        for token, (emergency_pair, emergency_text) in released_emergencies.items():
            self._release_emergency(emergency_pair, emergency_text, token)
        if released:
            await self._publish_working(channel)
        removed_current = removed_waiting = False
        if not link_only:
            async with self._output_lock(pair):
                current = self._playback.get(pair)
                removed_current = current is not None and current.channel == channel
                removed_items = (
                    [current] if removed_current and current is not None else []
                )
                if removed_current:
                    self._playback.pop(pair, None)
                waiting = self._playback_waiting.get(pair)
                if waiting is not None:
                    retained = deque(
                        item for item in waiting if item.channel != channel
                    )
                    removed_waiting = len(retained) != len(waiting)
                    removed_items.extend(
                        item for item in waiting if item.channel == channel
                    )
                else:
                    retained = deque()
                    removed_waiting = False
                if removed_current and retained:
                    self._playback[pair] = retained.popleft()
                if retained:
                    self._playback_waiting[pair] = retained
                else:
                    self._playback_waiting.pop(pair, None)
                for item in removed_items:
                    self._cancel_playback_expiry(pair, item)
                    await self._release_reservation(item.reservation)
                    self._release_emergency(
                        pair, item.emergency_text, item.emergency_token
                    )
                if removed_current and pair in self._playback:
                    await self._send_current(pair)
        if (
            replacement_channel is not None
            and not migrated_capture
            and (
                released
                or removed_current
                or removed_waiting
                or had_inflight_speech
                or had_inflight_mailbox
            )
        ):
            await self._queue_speech(replacement_channel, pair, LINK_ERROR_REPLY)
        # If this Pair was the failed head of an old-Channel receipt replay,
        # continue the bounded queue for the other Pairs immediately. Waiting
        # for an unrelated Link reconnect would strand their truthful log ack.
        await self._flush_link_acks(channel)
        if not self._pending_link_acks.get(
            channel
        ) and not self._pending_proactive_acks.get(channel):
            self._forget_link_acks(channel)
        if replacement_channel is None:
            self._device_generations.pop(pair, None)
        if not any(key[0] == channel for key in self._active):
            grace = self._link_grace.pop(channel, None)
            if grace is not None:
                for task in grace.tasks:
                    task.cancel()

    def _output_lock(self, pair: str) -> asyncio.Lock:
        lock = self._output_locks.get(pair)
        if lock is None:
            lock = asyncio.Lock()
            self._output_locks[pair] = lock
        return lock

    def _link_ack_lock(self, channel: str) -> asyncio.Lock:
        lock = self._link_ack_locks.get(channel)
        if lock is None:
            lock = asyncio.Lock()
            self._link_ack_locks[channel] = lock
        return lock

    async def _retain_link_ack(self, channel: str, pair: str, turn_id: str) -> None:
        async with self._lock:
            pending = self._pending_link_acks.setdefault(channel, deque())
            item = (pair, turn_id)
            if item not in pending:
                pending.append(item)
                while len(pending) > MAX_PENDING_LINK_ACKS:
                    pending.popleft()
        self._expire_link_acks_later(channel)
        await self._flush_link_acks(channel)

    async def _flush_link_acks(self, channel: str) -> None:
        async with self._link_ack_lock(channel):
            async with self._lock:
                pending = list(self._pending_link_acks.get(channel, ()))
            for pair, turn_id in pending:
                if not await self.send_link(
                    channel,
                    pair,
                    json.dumps(
                        {"v": 1, "type": "link.turn.ack", "id": turn_id},
                        separators=(",", ":"),
                    ),
                ):
                    return

            async with self._lock:
                proactive = list(self._pending_proactive_acks.get(channel, ()))
            for pair, message_id, text in proactive:
                if not await self.send_link(
                    channel,
                    pair,
                    json.dumps(
                        {
                            "v": 1,
                            "type": "link.proactive.played",
                            "id": message_id,
                            "text": text,
                        },
                        separators=(",", ":"),
                    ),
                ):
                    return

    async def _retain_proactive_ack(
        self, channel: str, pair: str, message_id: str, text: str
    ) -> None:
        async with self._lock:
            pending = self._pending_proactive_acks.setdefault(channel, deque())
            item = (pair, message_id, text)
            if item not in pending:
                pending.append(item)
                while len(pending) > MAX_PENDING_LINK_ACKS:
                    pending.popleft()
        self._expire_link_acks_later(channel)
        await self._flush_link_acks(channel)

    async def link_ready(self, channel: str, link_id: str | None = None) -> None:
        """Resume only the same authorized Link inside its disconnect grace."""
        grace = self._link_grace.get(channel)
        if grace is not None and grace.link_id == link_id:
            grace.connected = True
        elif grace is not None:
            del self._link_grace[channel]
            for task in grace.tasks:
                task.cancel()
            await self.link_gone(channel)
            self._forget_link_acks(channel)
        await self._flush_link_acks(channel)

    def _forget_link_acks(self, channel):
        self._pending_link_acks.pop(channel, None)
        self._pending_proactive_acks.pop(channel, None)
        handle = self._ack_expiries.pop(channel, None)
        if handle is not None:
            handle.cancel()

    def _expire_link_acks_later(self, channel):
        # A silent/uninstalled Link cannot retain receipts indefinitely. Do not
        # extend the oldest receipt's lifetime with a stream of newer receipts.
        if channel not in self._ack_expiries:
            self._ack_expiries[channel] = asyncio.get_running_loop().call_later(
                LINK_ACK_TTL, self._forget_link_acks, channel
            )

    async def _reserve_playback(self, pair: str) -> _CapacityReservation | None:
        async with self._playback_capacity_lock:
            pair_count = self._playback_counts.get(pair, 0)
            if (
                pair_count >= MAX_PLAYBACKS_PER_PAIR
                or self._playback_bytes + PLAYBACK_RESERVATION_BYTES
                > MAX_PLAYBACK_BYTES
            ):
                return None
            reservation = _CapacityReservation(
                secrets.token_hex(16), pair, False, PLAYBACK_RESERVATION_BYTES
            )
            self._playback_counts[pair] = pair_count + 1
            self._playback_total += 1
            self._playback_bytes += reservation.size
            self._capacity_reservations[reservation.id] = reservation
            return reservation

    async def _reserve_overflow(self, pair: str) -> _CapacityReservation | None:
        async with self._playback_capacity_lock:
            if (
                pair in self._overflow_pairs
                or self._overflow_bytes + PLAYBACK_RESERVATION_BYTES
                > MAX_OVERFLOW_PLAYBACK_BYTES
            ):
                return None
            reservation = _CapacityReservation(
                secrets.token_hex(16), pair, True, PLAYBACK_RESERVATION_BYTES
            )
            self._overflow_pairs[pair] = reservation.id
            self._overflow_total += 1
            self._overflow_bytes += reservation.size
            self._capacity_reservations[reservation.id] = reservation
            return reservation

    async def _release_reservation(
        self, reservation: _CapacityReservation | None
    ) -> None:
        if reservation is None:
            return
        async with self._playback_capacity_lock:
            if self._capacity_reservations.pop(reservation.id, None) is None:
                return
            if reservation.overflow:
                if self._overflow_pairs.get(reservation.pair) == reservation.id:
                    del self._overflow_pairs[reservation.pair]
                self._overflow_total -= 1
                self._overflow_bytes -= reservation.size
                return
            pair_count = self._playback_counts.get(reservation.pair, 0)
            if pair_count <= 1:
                self._playback_counts.pop(reservation.pair, None)
            else:
                self._playback_counts[reservation.pair] = pair_count - 1
            self._playback_total -= 1
            self._playback_bytes -= reservation.size

    def _resize_reservation(self, reservation, size):
        # No await: accounting and the audio reference change in one loop slice.
        if not self._reservation_is_active(reservation):
            return False
        attribute = "_overflow_bytes" if reservation.overflow else "_playback_bytes"
        budget = (
            MAX_OVERFLOW_PLAYBACK_BYTES if reservation.overflow else MAX_PLAYBACK_BYTES
        )
        total = getattr(self, attribute) + size - reservation.size
        if total > budget:
            return False
        setattr(self, attribute, total)
        reservation.size = size
        return True

    async def _speech_call(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(
            self.speech_executor, partial(function, *args)
        )

    def _reservation_is_active(self, reservation: _CapacityReservation | None) -> bool:
        return (
            reservation is not None
            and self._capacity_reservations.get(reservation.id) == reservation
        )

    def _reserve_emergency(self, pair: str, text: str) -> str | None:
        key = (pair, text)
        if key in self._emergency_pairs:
            return None
        token = secrets.token_hex(16)
        self._emergency_pairs[key] = token
        return token

    def _release_emergency(
        self, pair: str, text: str | None, token: str | None
    ) -> None:
        key = (pair, text) if text is not None else None
        if (
            key is not None
            and token is not None
            and self._emergency_pairs.get(key) == token
        ):
            del self._emergency_pairs[key]

    def _synthesize(self, text: str, voice: str | None) -> bytes:
        # Older adapters and test fakes take only the text.
        if voice is None:
            return self.speech.synthesize(text)
        return self.speech.synthesize(text, voice)

    async def shared_standard_audio(self, text: str, voice: str | None = None) -> bytes:
        """One immutable clip per (voice, fixed phrase); fallback MP3 on failure.

        Only a real synthesis is cached. A Speech blip returns the fallback for
        this request alone, so it cannot replace the phrase until a restart.
        """
        key = (voice, text)
        if key in self._standard_audio:
            return self._standard_audio[key]
        lock = self._standard_audio_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key not in self._standard_audio:
                try:
                    self._standard_audio[key] = await self._speech_call(
                        self._synthesize, text, voice
                    )
                except SpeechError:
                    return self.fallback_error_audio
            return self._standard_audio[key]

    async def _queue_speech(
        self,
        channel: str,
        pair: str,
        text: str,
        *,
        turn_id: str | None = None,
        needs_answer: bool = False,
        _generation: int | None = None,
        _reservation: _CapacityReservation | None = None,
        _tracked: bool = False,
        _playback_id: str | None = None,
        _mailbox_batch_id: str | None = None,
        _mailbox_entries: tuple = (),
        _mailbox_unread_count: int | None = None,
        _expires_at: datetime | None = None,
    ) -> bool:
        route_key = (channel, pair)
        generation = (
            self._device_generation(pair) if _generation is None else _generation
        )
        if not _tracked:
            self._track_speech(channel, pair, _reservation)
        reservation = (
            _reservation if self._reservation_is_active(_reservation) else None
        )
        inserted = False
        try:
            if not self._same_device_generation(pair, generation):
                return False
            if reservation is None:
                reservation = await self._reserve_playback(pair)
            if reservation is None:
                print(f"  cloud playback queue full for {pair}; queued refusal")
                return await self._queue_standard_error(
                    channel,
                    pair,
                    route_key,
                    generation,
                    text if text in STANDARD_REPLIES else QUEUE_FULL_REPLY,
                )
            self._speech_reservations.setdefault(route_key, {})[reservation.id] = (
                reservation
            )
            if (
                route_key in self._departed_speech_routes
                or not self._same_device_generation(pair, generation)
            ):
                return False
            if text in STANDARD_REPLIES:
                audio = await self.shared_standard_audio(text, self.voice_for(pair))
            else:
                try:
                    audio = await self._speech_call(
                        self._synthesize, text, self.voice_for(pair)
                    )
                except SpeechError:
                    # Error speech cannot depend on the adapter whose failure it is
                    # explaining. This pre-rendered cloud asset is deliberately the
                    # last dependency before a total cloud outage becomes indicator-only.
                    audio = self.fallback_error_audio
                    # Hearing a failure tone is not hearing the assistant's
                    # answer or permission question, including in live-only mode.
                    turn_id = None
                    needs_answer = False
                    if _mailbox_batch_id is not None:
                        # The User heard the fixed failure, not the numbered
                        # snapshot. Give the failure its own volatile span so
                        # its ack cannot produce a false durable played receipt.
                        _playback_id = None
                        _mailbox_batch_id = None
                        _mailbox_entries = ()
                        _mailbox_unread_count = None
            if not self._resize_reservation(
                reservation, len(audio) + PLAYBACK_METADATA_BYTES
            ):
                return False
            playback = _Playback(
                channel,
                _playback_id or "c_" + secrets.token_hex(12),
                audio,
                turn_id=turn_id,
                needs_answer=needs_answer,
                reservation=reservation,
                mailbox_batch_id=_mailbox_batch_id,
                mailbox_entries=_mailbox_entries,
                mailbox_unread_count=_mailbox_unread_count,
                expires_at=_expires_at,
            )
            async with self._output_lock(pair):
                if (
                    route_key in self._departed_speech_routes
                    or not self._same_device_generation(pair, generation)
                ):
                    return False
                if pair in self._playback:
                    self._playback_waiting.setdefault(pair, deque()).append(playback)
                    self._schedule_playback_expiry(pair, playback)
                    inserted = True
                    return True
                self._playback[pair] = playback
                self._schedule_playback_expiry(pair, playback)
                inserted = True
                return await self._send_current(pair)
        finally:
            if reservation is not None:
                tracked_reservations = self._speech_reservations.get(route_key)
                if tracked_reservations is not None:
                    tracked_reservations.pop(reservation.id, None)
                    if not tracked_reservations:
                        self._speech_reservations.pop(route_key, None)
                if not inserted:
                    await self._release_reservation(reservation)
            remaining = self._speech_inflight.get(route_key, 1) - 1
            if remaining:
                self._speech_inflight[route_key] = remaining
            else:
                self._speech_inflight.pop(route_key, None)
                self._departed_speech_routes.pop(route_key, None)
                self._speech_reservations.pop(route_key, None)
                self._speech_emergencies.pop(route_key, None)
            await self._publish_working(channel)

    async def _queue_standard_error(
        self,
        channel: str,
        pair: str,
        route_key: tuple[str, str],
        generation: int,
        text: str,
    ) -> bool:
        # Fixed lifecycle outcomes share one immutable clip per phrase. The
        # capacity reply gets a small large-audio budget first; all fixed replies
        # still have a one-record-per-Pair path after that budget is full.
        reservation = (
            await self._reserve_overflow(pair) if text == QUEUE_FULL_REPLY else None
        )
        emergency_token = None
        if reservation is None:
            if text == QUEUE_FULL_REPLY and pair in self._overflow_pairs:
                return False
            emergency_token = self._reserve_emergency(pair, text)
            if emergency_token is None:
                return False
            self._speech_emergencies.setdefault(route_key, {})[emergency_token] = (
                pair,
                text,
            )
        else:
            self._speech_reservations.setdefault(route_key, {})[reservation.id] = (
                reservation
            )
        inserted = False
        try:
            if not self._same_device_generation(pair, generation):
                return False
            audio = await self.shared_standard_audio(text, self.voice_for(pair))
            playback = _Playback(
                channel,
                "c_" + secrets.token_hex(12),
                audio,
                reservation=reservation,
                emergency_token=emergency_token,
                emergency_text=text if emergency_token is not None else None,
            )
            async with self._output_lock(pair):
                if (
                    route_key in self._departed_speech_routes
                    or not self._same_device_generation(pair, generation)
                ):
                    return False
                if pair in self._playback:
                    self._playback_waiting.setdefault(pair, deque()).append(playback)
                    inserted = True
                    return False
                self._playback[pair] = playback
                inserted = True
                await self._send_current(pair)
                return False
        finally:
            if reservation is not None:
                tracked_reservations = self._speech_reservations.get(route_key)
                if tracked_reservations is not None:
                    tracked_reservations.pop(reservation.id, None)
                    if not tracked_reservations:
                        self._speech_reservations.pop(route_key, None)
            if emergency_token is not None:
                tracked_emergencies = self._speech_emergencies.get(route_key)
                if tracked_emergencies is not None:
                    tracked_emergencies.pop(emergency_token, None)
                    if not tracked_emergencies:
                        self._speech_emergencies.pop(route_key, None)
            if not inserted:
                await self._release_reservation(reservation)
                self._release_emergency(pair, text, emergency_token)

    @staticmethod
    def digest(
        missed: int, needs_answer: bool = False, dismissible: bool = False
    ) -> str:
        return json.dumps(
            {
                "v": 1,
                "type": "digest",
                "missed": missed,
                "needs_answer": needs_answer,
                "dismissible": dismissible,
                "summary": "",
                "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
            separators=(",", ":"),
        )

    def _schedule_playback_expiry(self, pair: str, playback: _Playback) -> None:
        if playback.expires_at is None:
            return
        key = (pair, playback.id)
        previous = self._playback_expiry_tasks.pop(key, None)
        if previous is not None:
            previous.cancel()
        task = asyncio.create_task(
            self._expire_playback(pair, playback.id, playback.expires_at)
        )
        self._playback_expiry_tasks[key] = task

        def finished(completed: asyncio.Task) -> None:
            if self._playback_expiry_tasks.get(key) is completed:
                self._playback_expiry_tasks.pop(key, None)

        task.add_done_callback(finished)

    def _cancel_playback_expiry(self, pair: str, playback: _Playback) -> None:
        task = self._playback_expiry_tasks.pop((pair, playback.id), None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _expire_playback(
        self, pair: str, playback_id: str, expires_at: datetime
    ) -> None:
        delay = max(0.0, (expires_at - datetime.now(UTC)).total_seconds())
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        removed = None
        expired_current = False
        promoted = False
        channel = None
        async with self._output_lock(pair):
            current = self._playback.get(pair)
            if current is not None and current.id == playback_id:
                removed = current
                expired_current = True
                channel = current.channel
                waiting = self._playback_waiting.get(pair)
                if waiting:
                    self._playback[pair] = waiting.popleft()
                    promoted = True
                    if not waiting:
                        self._playback_waiting.pop(pair, None)
                else:
                    self._playback.pop(pair, None)
                retract_start = json.dumps(
                    {"v": 1, "type": "audio.start", "id": playback_id},
                    separators=(",", ":"),
                )
                retract_end = json.dumps(
                    {
                        "v": 1,
                        "type": "audio.end",
                        "id": playback_id,
                        "complete": False,
                    },
                    separators=(",", ":"),
                )
                await self.send_device(
                    current.channel, pair, [retract_start, retract_end]
                )
                if promoted:
                    await self._send_current(pair)
            else:
                waiting = self._playback_waiting.get(pair)
                if waiting is not None:
                    retained = deque(item for item in waiting if item.id != playback_id)
                    removed = next(
                        (item for item in waiting if item.id == playback_id), None
                    )
                    if retained:
                        self._playback_waiting[pair] = retained
                    else:
                        self._playback_waiting.pop(pair, None)
        if removed is not None:
            await self._release_reservation(removed.reservation)
            self._release_emergency(
                pair, removed.emergency_text, removed.emergency_token
            )
        if expired_current and not promoted and channel is not None:
            staged = await self._stage_mailbox_message(channel, pair)
            if not staged:
                await self.send_device(channel, pair, [self.digest(0)])

    async def _send_current(self, pair: str) -> bool:
        current = self._playback.get(pair)
        if current is None:
            return True
        rendered = True
        if not current.audio and current.mailbox_batch_id is not None:
            # A failed re-render releases the empty playback below, like a
            # failed send; the durable Mailbox entry is staged again later.
            rendered = self._resize_reservation(
                current.reservation, PLAYBACK_RESERVATION_BYTES
            )
            if rendered:
                try:
                    batch = await self.mailbox.next_batch(current.channel, pair)
                    rendered = (
                        batch is not None and batch.batch_id == current.mailbox_batch_id
                    )
                    if rendered:
                        self.mailbox_rerenders += 1
                        current.audio = await self._speech_call(
                            self._synthesize,
                            self._mailbox_speech(batch),
                            self.voice_for(pair),
                        )
                except (MailboxError, SpeechError):
                    rendered = False
                finally:
                    self._resize_reservation(
                        current.reservation,
                        len(current.audio) + PLAYBACK_METADATA_BYTES,
                    )
        waiting = self._playback_waiting.get(pair, ())
        digest = self.digest(
            current.mailbox_unread_count or 1 + len(waiting),
            current.needs_answer or any(item.needs_answer for item in waiting),
            False,  # Older firmware must never offer reply deletion.
        )
        audio_start = json.dumps(
            {"v": 1, "type": "audio.start", "id": current.id},
            separators=(",", ":"),
        )
        audio_end = json.dumps(
            {
                "v": 1,
                "type": "audio.end",
                "id": current.id,
                "complete": True,
            },
            separators=(",", ":"),
        )

        def frames() -> Iterable[str | bytes | memoryview]:
            yield audio_start
            audio = memoryview(current.audio)
            for index in range(0, len(audio), PLAYBACK_CHUNK):
                yield audio[index : index + PLAYBACK_CHUNK]
            yield audio_end
            yield digest

        sent = rendered and await self.send_device(current.channel, pair, frames())
        if sent and current.mailbox_batch_id is not None and self.mailbox is not None:
            current.audio = b""
            self._resize_reservation(current.reservation, PLAYBACK_METADATA_BYTES)
            try:
                await self.mailbox.notified(
                    current.channel, pair, current.mailbox_batch_id
                )
            except MailboxError:
                # The complete span is already on the authenticated Device. A
                # retry or its exact played receipt repairs the observable state.
                pass
        if not sent:
            dropped_items = [current, *self._playback_waiting.get(pair, ())]
            self._playback.pop(pair, None)
            self._playback_waiting.pop(pair, None)
            for item in dropped_items:
                self._cancel_playback_expiry(pair, item)
                await self._release_reservation(item.reservation)
                self._release_emergency(pair, item.emergency_text, item.emergency_token)
        return sent

    async def _ack(self, channel: str, pair: str, briefing_id: str) -> bool:
        turn_id = None
        mailbox_batch_id = None
        mailbox_entries = ()
        stage_next = False
        send_empty_digest = False
        async with self._output_lock(pair):
            current = self._playback.get(pair)
            if (
                current is None
                or current.channel != channel
                or current.id != briefing_id
            ):
                duplicate_batch_id = self._mailbox_batch_from_playback(briefing_id)
                if self.mailbox is None or duplicate_batch_id is None:
                    return False
                try:
                    await self.mailbox.complete_batch(channel, pair, duplicate_batch_id)
                except MailboxError:
                    return False
                if self._uncommitted_acks.get(pair) == (channel, briefing_id):
                    self._uncommitted_acks.pop(pair, None)
                await self._confirm_ack(channel, pair, briefing_id)
                return True
            turn_id = current.turn_id
            mailbox_batch_id = current.mailbox_batch_id
            mailbox_entries = current.mailbox_entries
            if mailbox_batch_id is not None:
                if self.mailbox is None:
                    return False
                try:
                    await self.mailbox.complete_batch(channel, pair, mailbox_batch_id)
                except MailboxError:
                    # Legacy firmware does not retry. Periodic Mailbox wake or
                    # Listen retries this exact receipt before staging more.
                    self._uncommitted_acks[pair] = (channel, briefing_id)
                    return False
                self._uncommitted_acks.pop(pair, None)
                await self._confirm_ack(channel, pair, briefing_id)
            self._cancel_playback_expiry(pair, current)
            waiting = self._playback_waiting.get(pair)
            if waiting:
                self._playback[pair] = waiting.popleft()
                if not waiting:
                    self._playback_waiting.pop(pair, None)
            else:
                self._playback.pop(pair, None)
            await self._release_reservation(current.reservation)
            self._release_emergency(
                pair, current.emergency_text, current.emergency_token
            )
            if pair in self._playback:
                await self._send_current(pair)
            elif mailbox_batch_id is None:
                await self.send_device(channel, pair, [self.digest(0)])
            stage_next = self.mailbox is not None
            send_empty_digest = mailbox_batch_id is not None
        if turn_id is not None:
            await self._retain_link_ack(channel, pair, turn_id)
        for entry in mailbox_entries:
            entry_turn = self._turn_from_idempotency(entry.idempotency_key)
            if entry_turn is not None:
                await self._retain_link_ack(channel, pair, entry_turn)
            elif entry.idempotency_key.startswith("proactive:"):
                await self._retain_proactive_ack(
                    channel, pair, entry.message_id, entry.text
                )
        if stage_next:
            staged = await self._stage_mailbox_message(channel, pair)
            if send_empty_digest and not staged:
                await self.send_device(channel, pair, [self.digest(0)])
        return True

    async def _confirm_ack(self, channel: str, pair: str, briefing_id: str) -> None:
        await self.send_device(
            channel,
            pair,
            [
                json.dumps(
                    {
                        "v": 1,
                        "type": "ack.confirmed",
                        "id": briefing_id,
                    },
                    separators=(",", ":"),
                )
            ],
        )

    async def _retry(self, channel: str, pair: str, briefing_id: str) -> bool:
        async with self._output_lock(pair):
            current = self._playback.get(pair)
            if (
                current is None
                or current.channel != channel
                or current.id != briefing_id
            ):
                return False
            await self._send_current(pair)
        return True

    async def _dismiss(self, channel: str, pair: str, briefing_id: str) -> bool:
        # Older firmware clears its local audio before sending this event.
        # Restore that exact reply; user gestures never delete unread speech.
        return await self._retry(channel, pair, briefing_id)
