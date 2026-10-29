"""One provider-neutral gate for paused coding-agent interactions.

The provider adapters own protocol details. This module owns the state that is
shared by voice and the native app: one bounded pending interaction, exact-ID
resolution, first-answer-wins, acknowledgement, expiry, and cancellation.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace


class InteractionUnavailable(RuntimeError):
    """The request is absent, stale, unacknowledged, or already resolved."""


@dataclass(frozen=True)
class Interaction:
    id: str
    provider: str
    scope: str
    origin: str
    delivery_origin: str
    kind: str
    title: str
    detail: str
    spoken: str
    created_at: float
    expires_at: float
    acknowledged: bool = False

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "scope": self.scope,
            "origin": self.origin,
            "delivery_origin": self.delivery_origin,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "spoken": self.spoken,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "acknowledged": self.acknowledged,
        }


@dataclass
class _Pending:
    interaction: Interaction
    resume: Callable[[str, str], object]
    cancel: Callable[[], object]
    timer: threading.Timer
    activity: Callable[[], dict] | None = None
    steer: Callable[[str], str] | None = None
    cancelled: bool = False


class InteractionBroker:
    """Atomic ownership of the single paused provider process on this Link."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: _Pending | None = None
        self._resolving: _Pending | None = None
        self._cancelled_scopes: dict[str, float] = {}

    def open(
        self,
        *,
        provider: str,
        scope: str,
        origin: str,
        delivery_origin: str | None = None,
        kind: str,
        title: str,
        detail: str,
        spoken: str,
        resume: Callable[[str, str], object],
        cancel: Callable[[], object],
        ttl_seconds: int,
        activity: Callable[[], dict] | None = None,
        steer: Callable[[str], str] | None = None,
    ) -> Interaction:
        now = self._clock()
        interaction = Interaction(
            id=uuid.uuid4().hex,
            provider=provider,
            scope=scope,
            origin=origin,
            delivery_origin=delivery_origin or origin,
            kind=kind,
            title=" ".join(title.split())[:120],
            detail=" ".join(detail.split())[:800],
            spoken=" ".join(spoken.split())[:500],
            created_at=now,
            expires_at=now + ttl_seconds,
        )
        timer = threading.Timer(ttl_seconds, self._expire, args=(interaction.id,))
        timer.daemon = True
        pending = _Pending(interaction, resume, cancel, timer, activity, steer)
        with self._lock:
            if self._pending is not None:
                raise InteractionUnavailable("another provider interaction is open")
            if self._resolving is not None and self._resolving.cancelled:
                raise InteractionUnavailable("the provider turn was cancelled")
            self._pending = pending
        timer.start()
        return interaction

    def snapshot(self) -> Interaction | None:
        with self._lock:
            return self._pending.interaction if self._pending else None

    def activity(self) -> dict | None:
        with self._lock:
            pending = self._resolving
            observe = pending.activity if pending is not None else None
        return observe() if observe is not None else None

    def steer(self, prompt: str) -> str:
        with self._lock:
            pending = self._resolving
            send = pending.steer if pending and not pending.cancelled else None
        return send(prompt) if send is not None else "inactive"

    def recently_cancelled(self, scope: str) -> bool:
        with self._lock:
            at = self._cancelled_scopes.get(scope)
            return at is not None and self._clock() - at < 540

    def forget_cancellation(self, scope: str) -> None:
        with self._lock:
            self._cancelled_scopes.pop(scope, None)

    def acknowledge(self, interaction_id: str) -> bool:
        with self._lock:
            pending = self._pending
            if pending is None or pending.interaction.id != interaction_id:
                return False
            pending.interaction = replace(pending.interaction, acknowledged=True)
            return True

    def resolve(
        self,
        interaction_id: str,
        decision: str,
        *,
        scope: str | None = None,
        origin: str | None = None,
        require_acknowledged: bool = False,
    ) -> object:
        if decision not in {"allow", "deny"}:
            raise ValueError("decision must be allow or deny")
        with self._lock:
            pending = self._pending
            if (
                pending is None
                or pending.interaction.id != interaction_id
                or (scope is not None and pending.interaction.scope != scope)
                or (require_acknowledged and not pending.interaction.acknowledged)
                or self._resolving is not None
            ):
                raise InteractionUnavailable("provider interaction is not answerable")
            expired = self._clock() >= pending.interaction.expires_at
            self._pending = None
            self._resolving = pending
            pending.timer.cancel()
        try:
            if expired:
                raise InteractionUnavailable("provider interaction has expired")
            result = pending.resume(decision, origin or pending.interaction.origin)
            if pending.cancelled:
                raise InteractionUnavailable("provider turn was cancelled")
            return result
        except Exception:  # noqa: BLE001 - provider cleanup must follow any failure
            try:
                self.cancel(interaction_id=interaction_id)
            finally:
                raise
        finally:
            with self._lock:
                if self._resolving is pending:
                    self._resolving = None

    def cancel(
        self, *, scope: str | None = None, interaction_id: str | None = None
    ) -> bool:
        with self._lock:
            selected = [pending for pending in (self._pending, self._resolving)
                        if pending is not None and not pending.cancelled
                        and (scope is None or pending.interaction.scope == scope)
                        and (interaction_id is None
                             or pending.interaction.id == interaction_id)]
            for pending in selected:
                pending.cancelled = True
                self._cancelled_scopes[pending.interaction.scope] = self._clock()
                while len(self._cancelled_scopes) > 64:
                    self._cancelled_scopes.pop(next(iter(self._cancelled_scopes)))
                pending.timer.cancel()
                if self._pending is pending:
                    self._pending = None
        for pending in selected:
            pending.cancel()
        return bool(selected)

    def _expire(self, interaction_id: str) -> None:
        self.cancel(interaction_id=interaction_id)


# Spoken approvals stay a closed vocabulary: an answer is never handed to a
# model to interpret, because a generous reading of an ambiguous phrase would
# authorize a real command. The sets are wide enough for how people actually
# answer out loud and no wider - anything outside them re-asks.
_ALLOW_PHRASES = frozenset({
    "yes", "yeah", "yep", "yup", "ya", "yup yup",
    "sure", "ok", "okay", "okey", "alright", "all right",
    "allow", "approve", "approved", "proceed", "confirm", "confirmed",
    "do it", "go ahead", "go for it", "affirmative", "sounds good",
})
_DENY_PHRASES = frozenset({
    "no", "nope", "nah", "negative",
    "deny", "decline", "declined", "cancel", "stop", "abort",
    "do not", "don't", "dont", "do not do it", "don't do it",
    "never mind", "nevermind", "forget it",
})
# Politeness that carries no decision. "no thanks" must still deny, so these are
# only ever stripped from the ends of an otherwise complete answer.
_COURTESY = ("please", "thanks", "thank you", "buddy", "man", "dude")


def spoken_decision(text: str) -> str | None:
    normalized = " ".join(str(text).lower().split()).strip(" .!?,;\n\t")
    for _ in range(len(_COURTESY)):
        stripped = normalized
        for word in _COURTESY:
            if stripped.startswith(word + " "):
                stripped = stripped[len(word) + 1:]
            if stripped.endswith(" " + word):
                stripped = stripped[: -len(word) - 1]
        stripped = stripped.strip(" .!?,;")
        if stripped == normalized:
            break
        normalized = stripped
    if normalized in _ALLOW_PHRASES:
        return "allow"
    if normalized in _DENY_PHRASES:
        return "deny"
    return None
