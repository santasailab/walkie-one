"""Optional delivery-interface value types; no storage or network client."""

from dataclasses import dataclass


class MailboxError(Exception):
    def __init__(self, message: str, *, code: str | None = None):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class MailboxEntry:
    message_id: str
    pair: str
    channel: str
    idempotency_key: str
    text: str
    needs_answer: bool
    expires_at: str | None = None


@dataclass(frozen=True)
class MailboxBatch:
    batch_id: str
    pair: str
    channel: str
    messages: tuple[MailboxEntry, ...]
    unread_count: int
    remaining_count: int
    needs_answer: bool
    delivery_state: str
    oldest_unread_at: str
    oldest_unread_age_seconds: int


@dataclass(frozen=True)
class MailboxReceipt:
    batch_id: str
    pair: str
    channel: str
    message_ids: tuple[str, ...]
    delivery_state: str
    unread_count: int
    oldest_unread_at: str | None
    oldest_unread_age_seconds: int | None
