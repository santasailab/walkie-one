"""Protected per-user IPC for proactive WalkieOne speech.

The socket path is discoverable, not secret. Filesystem permissions and the
kernel-reported peer user are the authority; no Device, Link, or cloud bearer
credential crosses into an Agent process.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
import socket
import stat
import struct
import sys
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing_extensions import Self

MAX_TEXT_CHARS = 1_200
MAX_IDEMPOTENCY_CHARS = 128
MAX_REQUEST_BYTES = 8_192
MAX_HEADER_BYTES = 4_096
READ_TIMEOUT = 5
MAX_INFLIGHT_REQUESTS = 8
# The longest a message may sit waiting for its moment. Beyond this it belongs
# in a real scheduler, not in an outbox WalkieOne holds open.
MAX_SCHEDULED_SECONDS = 24 * 60 * 60


class SendRateLimit:
    """Two token buckets, shared for a helper's lifetime, not per connection.

    A burst can spend six tokens; replenishment is six/minute and sixty/hour.
    Admission runs on the event loop without awaiting, so concurrent callers
    cannot spend the same token. Rejected attempts consume neither bucket.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.updated = clock()
        self.minute = 6.0
        self.hour = 60.0

    def admit(self) -> bool:
        now = self.clock()
        elapsed = max(0, now - self.updated)
        self.updated = now
        self.minute = min(6, self.minute + elapsed / 10)
        self.hour = min(60, self.hour + elapsed / 60)
        if min(self.minute, self.hour) < 1:
            return False
        self.minute -= 1
        self.hour -= 1
        return True


Route = Callable[[dict[str, list[str]]], Awaitable[tuple[int, dict]]]


def task_routes(
    handle: Callable[[str, dict[str, list[str]]], Awaitable[tuple[int, dict]]],
) -> dict[bytes, Route]:
    """Background Task operations, served beside sends on the same socket."""
    return {
        f"POST /v1/tasks/{op} HTTP/1.1".encode(): (
            lambda fields, op=op: handle(op, fields)
        )
        for op in ("add", "remove", "run", "list")
    }


@dataclass(frozen=True)
class LocalSendRequest:
    text: str
    idempotency_key: str
    # Which Agent turn issued this, when one did. A turn's own answer is already
    # spoken, so a send carrying the turn that is speaking right now is the same
    # message arriving twice. Anything detached inherits the value and fires
    # later, when that turn is no longer the one speaking.
    turn: str = ""


@dataclass(frozen=True)
class LocalSendReceipt:
    status: str
    message_id: str | None = None
    reason: str | None = None
    at: float | None = None

    @classmethod
    def queued(cls, message_id: str) -> LocalSendReceipt:
        return cls("queued", message_id=message_id)

    @classmethod
    def rejected(cls, reason: str) -> LocalSendReceipt:
        return cls("rejected", reason=reason)

    @classmethod
    def scheduled(cls, at: float) -> LocalSendReceipt:
        """Accepted, and waiting for its moment inside WalkieOne."""
        return cls("scheduled", at=at)

    def payload(self) -> dict:
        payload = {"v": 1, "status": self.status}
        if self.status == "queued" and self.message_id is not None:
            payload["message_id"] = self.message_id
        elif self.status == "rejected" and self.reason is not None:
            payload["reason"] = self.reason
        elif self.status == "scheduled" and self.at is not None:
            payload["at"] = int(self.at)
        else:
            raise ValueError("invalid local send receipt")
        return payload


def default_socket_path() -> Path:
    configured = os.environ.get("WALKIEONE_SEND_SOCKET", "")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".relay" / "ipc" / "send.sock"


def _peer_uid(peer_socket) -> int:
    """Return the peer's effective uid on macOS or Linux Unix sockets."""
    if hasattr(socket, "SO_PEERCRED"):
        size = struct.calcsize("3i")
        raw = peer_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid

    if sys.platform == "darwin":
        effective_uid = ctypes.c_uint()
        effective_gid = ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        getpeereid = libc.getpeereid
        getpeereid.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        getpeereid.restype = ctypes.c_int
        if (
            getpeereid(
                peer_socket.fileno(),
                ctypes.byref(effective_uid),
                ctypes.byref(effective_gid),
            )
            != 0
        ):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return effective_uid.value

    raise OSError("peer credentials are unavailable on this platform")


class LocalSendServer:
    """One-operation HTTP endpoint bound only to a protected Unix socket."""

    def __init__(
        self,
        path: str | Path,
        send: Callable[[LocalSendRequest], Awaitable[LocalSendReceipt]],
        *,
        routes: dict[bytes, Route] | None = None,
        peer_uid: Callable[[object], int] = _peer_uid,
    ) -> None:
        self.path = Path(path)
        self.send = send
        # Task and alert operations share this socket's same-user authority.
        self.routes = routes or {}
        self.peer_uid = peer_uid
        self._server: asyncio.Server | None = None
        self._inflight = 0
        self._clients: set[asyncio.Task] = set()
        self._closing = False

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def start(self) -> None:
        if self._server is not None:
            return
        self._closing = False
        parent = self.path.parent
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        parent_stat = parent.lstat()
        if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.geteuid():
            raise PermissionError("local send directory is not owned by this user")
        parent.chmod(0o700)

        if os.path.lexists(self.path):
            existing = self.path.lstat()
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid():
                raise PermissionError("refusing to replace an unsafe local send path")
            self.path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle,
            path=str(self.path),
            limit=MAX_REQUEST_BYTES + MAX_HEADER_BYTES,
        )
        self.path.chmod(0o600)

    async def close(self) -> None:
        self._closing = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
        clients = list(self._clients)
        for client in clients:
            client.cancel()
        if clients:
            await asyncio.gather(*clients, return_exceptions=True)
        if server is not None:
            await server.wait_closed()
        if os.path.lexists(self.path):
            existing = self.path.lstat()
            if stat.S_ISSOCK(existing.st_mode) and existing.st_uid == os.geteuid():
                self.path.unlink()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client = asyncio.current_task()
        self._clients.add(client)
        admitted = False
        try:
            if self._closing:
                return
            peer_socket = writer.get_extra_info("socket")
            try:
                authorized = (
                    peer_socket is not None
                    and self.peer_uid(peer_socket) == os.geteuid()
                )
            except OSError:
                authorized = False
            if not authorized:
                await self._respond(
                    writer, 403, LocalSendReceipt.rejected("unauthorized")
                )
                return
            if self._inflight >= MAX_INFLIGHT_REQUESTS:
                await self._respond(
                    writer, 503, LocalSendReceipt.rejected("mailbox_full")
                )
                return
            self._inflight += 1
            admitted = True
            try:
                line, fields = await self._read_form(reader)
                route = self.routes.get(line)
                request = None if route else self._message(line, fields)
            except (
                TimeoutError,
                asyncio.TimeoutError,
                ValueError,
                UnicodeError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ):
                await self._respond(
                    writer, 400, LocalSendReceipt.rejected("invalid_request")
                )
                return
            if route:
                try:
                    status, payload = await route(fields)
                except Exception:  # noqa: BLE001 - same trust boundary as sends
                    status, payload = (
                        503,
                        LocalSendReceipt.rejected("unavailable").payload(),
                    )
                await self._respond(writer, status, payload)
                return
            try:
                receipt = await self.send(request)
            # This is a trust boundary: a local caller gets one stable
            # rejection, never an engine exception or provider diagnostic.
            except Exception:  # noqa: BLE001
                receipt = LocalSendReceipt.rejected("link_disconnected")
            status = 202 if receipt.status == "queued" else 503
            await self._respond(writer, status, receipt)
        except (ConnectionError, OSError):
            pass
        finally:
            self._clients.discard(client)
            if admitted:
                self._inflight -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _read_form(
        self, reader: asyncio.StreamReader
    ) -> tuple[bytes, dict[str, list[str]]]:
        raw_headers = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT
        )
        if len(raw_headers) > MAX_HEADER_BYTES:
            raise ValueError("request headers are too large")
        lines = raw_headers[:-4].split(b"\r\n")
        if not lines or not lines[0].startswith(b"POST /v1/"):
            raise ValueError("unsupported local send request")
        headers: dict[str, str] = {}
        for raw_line in lines[1:]:
            name, separator, value = raw_line.partition(b":")
            if not separator:
                raise ValueError("malformed request header")
            key = name.decode("ascii").strip().lower()
            if key in headers:
                raise ValueError("duplicate request header")
            headers[key] = value.decode("ascii").strip()
        if headers.get("content-type", "").split(";", 1)[0].strip() != (
            "application/x-www-form-urlencoded"
        ):
            raise ValueError("unsupported request content type")
        try:
            content_length = int(headers["content-length"])
        except (KeyError, ValueError) as error:
            raise ValueError("missing request length") from error
        if content_length < 1 or content_length > MAX_REQUEST_BYTES:
            raise ValueError("request body is too large")
        body = await asyncio.wait_for(reader.readexactly(content_length), READ_TIMEOUT)
        fields = urllib.parse.parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
        )
        return lines[0], fields

    @staticmethod
    def _message(line: bytes, fields: dict[str, list[str]]) -> LocalSendRequest:
        if line != b"POST /v1/messages HTTP/1.1":
            raise ValueError("unsupported local send request")
        if (
            not {"text", "idempotency_key"} <= set(fields)
            or not set(fields)
            <= {
                "text",
                "idempotency_key",
                "turn",
            }
            or any(len(values) != 1 for values in fields.values())
        ):
            raise ValueError("invalid local send fields")
        text = fields["text"][0].strip()
        key = fields["idempotency_key"][0].strip()
        turn = fields.get("turn", [""])[0].strip()
        if (
            not text
            or len(text) > MAX_TEXT_CHARS
            or not key
            or len(key) > MAX_IDEMPOTENCY_CHARS
            or len(turn) > MAX_IDEMPOTENCY_CHARS
            or any(ord(character) < 32 or ord(character) == 127 for character in key)
            or any(ord(character) < 32 or ord(character) == 127 for character in turn)
        ):
            raise ValueError("invalid local send values")
        return LocalSendRequest(text=text, idempotency_key=key, turn=turn)

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        status: int,
        receipt: LocalSendReceipt | dict,
    ) -> None:
        payload = receipt if isinstance(receipt, dict) else receipt.payload()
        body = json.dumps(payload, separators=(",", ":")).encode()
        reason = {
            200: "OK",
            202: "Accepted",
            400: "Bad Request",
            403: "Forbidden",
            404: "Not Found",
            503: "Service Unavailable",
        }[status]
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()


def default_outbox_path() -> Path:
    """Where a sandboxed Agent drops a send it cannot deliver itself.

    Inside the Agent's own workspace on purpose: a coding agent normally runs
    under a sandbox that permits writing its workspace and denies the network,
    and a Unix socket connect is a network operation. Handing it a file to write
    keeps the one thing it must do inside what it is always allowed to do.
    """
    configured = os.environ.get("WALKIEONE_OUTBOX", "")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".relay" / "agent" / "outbox"


class LocalSendOutbox:
    """Drains sends the Agent left as files, and answers each with a receipt.

    The Agent presses a button; this side does the network. Requests survive a
    Link restart because they are on disk until they are delivered, which the
    socket could never offer.
    """

    REQUEST_SUFFIX = ".request.json"
    RECEIPT_SUFFIX = ".receipt.json"
    # A runaway loop in a detached job must not fill the disk or the mailbox.
    MAX_PENDING = 32
    MAX_RECEIPTS = 64
    RETRY_SECONDS = 15 * 60
    RETRY_REASONS = frozenset(
        {"link_disconnected", "cloud_unavailable", "send_failed", "rate_limited"}
    )

    def __init__(
        self,
        path: str | Path,
        send: Callable[[LocalSendRequest], Awaitable[LocalSendReceipt]],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.send = send
        self.clock = clock

    def _prepare(self) -> None:
        self.path.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError("outbox directory is not owned by this user")

    def _write_receipt(self, name: str, receipt: LocalSendReceipt) -> None:
        target = self.path / (name + self.RECEIPT_SUFFIX)
        temporary = target.with_suffix(".partial")
        temporary.write_text(
            json.dumps(receipt.payload(), separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        receipts = sorted(
            self.path.glob("*" + self.RECEIPT_SUFFIX), key=lambda p: p.stat().st_mtime
        )
        for stale in receipts[: -self.MAX_RECEIPTS]:
            stale.unlink(missing_ok=True)

    @staticmethod
    def _read(path: Path, now: float | None = None) -> LocalSendRequest | None:
        try:
            if path.stat().st_size > MAX_REQUEST_BYTES:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict) or value.get("v") != 1:
            return None
        moment = value.get("not_before")
        now = time.time() if now is None else now
        if moment is not None and (
            isinstance(moment, bool)
            or not isinstance(moment, (int, float))
            or moment < 0
            or moment > now + MAX_SCHEDULED_SECONDS
            or not math.isfinite(moment)
        ):
            return None
        text = value.get("text")
        key = value.get("idempotency_key")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_TEXT_CHARS
            or not isinstance(key, str)
            or not key.strip()
            or len(key) > MAX_IDEMPOTENCY_CHARS
        ):
            return None
        turn = value.get("turn")
        turn = turn.strip() if isinstance(turn, str) else ""
        if value.get("not_before") is not None:
            # Deliberately deferred, so it cannot be duplicating the answer of
            # the turn that wrote it - and that turn may still be running when
            # its own reminder comes due.
            turn = ""
        return LocalSendRequest(
            text=text.strip(),
            idempotency_key=key.strip(),
            turn=turn[:MAX_IDEMPOTENCY_CHARS],
        )

    @staticmethod
    def _due_at(path: Path) -> float:
        """When this request may be delivered, as a unix timestamp."""
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0.0
        moment = value.get("not_before") if isinstance(value, dict) else None
        if moment is None:
            return path.stat().st_mtime
        return float(moment)

    async def drain_once(self) -> int:
        """Deliver every request currently waiting. Returns how many were read."""
        self._prepare()
        pending = sorted(
            self.path.glob("*" + self.REQUEST_SUFFIX), key=lambda p: p.stat().st_mtime
        )
        handled = 0
        now = self.clock()
        valid = []
        # Validate before counting capacity, so far-future or malformed files
        # cannot exclude a legitimate due reminder.
        for request_path in pending:
            name = request_path.name[: -len(self.REQUEST_SUFFIX)]
            request = self._read(request_path, now)
            if request is None:
                self._write_receipt(name, LocalSendReceipt.rejected("invalid_request"))
                request_path.unlink(missing_ok=True)
                handled += 1
                continue
            due = self._due_at(request_path)
            if now >= due + self.RETRY_SECONDS:
                self._write_receipt(name, LocalSendReceipt.rejected("expired"))
                request_path.unlink(missing_ok=True)
                handled += 1
                continue
            valid.append((request_path, request, due))
        for extra, _request, _due in valid[self.MAX_PENDING :]:
            name = extra.name[: -len(self.REQUEST_SUFFIX)]
            self._write_receipt(name, LocalSendReceipt.rejected("outbox_full"))
            extra.unlink(missing_ok=True)
        for request_path, request, due in valid[: self.MAX_PENDING]:
            name = request_path.name[: -len(self.REQUEST_SUFFIX)]
            # A send with a time on it waits here rather than in a process the
            # Agent leaves behind: a turn's children do not survive the turn.
            if due > now:
                receipt_path = self.path / (name + self.RECEIPT_SUFFIX)
                if not receipt_path.exists():
                    self._write_receipt(name, LocalSendReceipt.scheduled(due))
                continue
            handled += 1
            try:
                receipt = await self.send(request)
            except Exception:  # noqa: BLE001 - one bad send cannot stop the drain
                receipt = LocalSendReceipt.rejected("send_failed")
            if receipt.status == "rejected" and receipt.reason in self.RETRY_REASONS:
                # Keep both the request and its original due/creation time.
                # Restarting Link cannot extend the retry window. Cloud uses
                # the same idempotency key if an earlier send was accepted.
                continue
            self._write_receipt(name, receipt)
            request_path.unlink(missing_ok=True)
        return handled

    async def run(self, interval: float = 1.0) -> None:
        while True:
            try:
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001, S110 - draining must not stop Link
                pass
            await asyncio.sleep(interval)
