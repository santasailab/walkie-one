#!/usr/bin/env python3
"""Provider-neutral Mac Link.

The Device and this process independently connect to the cloud. Cloud owns
microphone speech recognition, playback synthesis, the unread mailbox, and the
public ready/busy/cancel lifecycle. This module keeps only the computer-side
boundary:

* an authenticated, outbound ``/link`` WebSocket;
* one selected local Agent Endpoint;
* exact played receipts for the bounded twelve-hour local speech context;
* owner-only proactive IPC; and
* a loopback ``/status`` response for the native application.

There is deliberately no local Device socket, browser stand-in, microphone
decoder, speech model, text-to-speech renderer, Messages watcher, or response
backlog here.
"""

import asyncio
import collections
import contextvars
import hashlib
import http
import json
import os
import random
import re
import secrets
import time
import urllib.parse
import uuid

from proactive import (
    LocalSendOutbox,
    LocalSendReceipt,
    LocalSendRequest,
    LocalSendServer,
    SendRateLimit,
    default_outbox_path,
    default_socket_path,
    task_routes,
)
from alerts import AlertWriter
from tasks import TaskRunner, default_tasks_path


HOST = os.environ.get("LINK_HOST", "127.0.0.1")
PORT = int(os.environ.get("LINK_PORT", "8765"))
AUTH = os.environ.get("LINK_AUTH", "")
CLOUD_URL = os.environ.get("RELAY_CLOUD_URL", "")
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "")
DEVICE = "device"

MAX_RECEIPTS = 50
PROACTIVE_RECEIPT_TIMEOUT = 12
MAX_PROACTIVE_INFLIGHT = 4
_proactive_rate_limit = SendRateLimit()

_started = time.time()
_cloud = {"state": "off", "error": None, "since": _started}
_empty_device = {"battery_pct": None, "rssi": None, "fw": None, "at": None}
_activity = collections.deque(maxlen=64)

_tasks = set()
_turn_gate = None
_cloud_turn_tasks = {}
_cloud_turn_running = set()
_cloud_turn_results = collections.OrderedDict()
_cloud_turn_interactions = {}
_cloud_turn_acked = collections.OrderedDict()
_cloud_proactive_acked = collections.OrderedDict()
_agent_busy_id = None
_cloud_turn_pairs = {}
_cloud_steer_receipts = collections.OrderedDict()
_interaction_pairs = collections.OrderedDict()
_native_turn_tasks = {}

# The native window holds one /events socket and is told when status changes,
# rather than asking. Changes can come from Agent threads, so wake-ups hop onto
# the serving loop.
_status_loop = None
_status_changed = None
# Device presence the relay pushes as link.device, per relay connection.
# None while disconnected: the Device is then unreachable through this Link.
_pair_online = None

_route_pair = contextvars.ContextVar("walkieone_route_pair", default=None)
_routed_end = object()
_pair_devices = {}
_routed_multiplexers = set()
_last_primary_pair = None


def _notify():
    """Tell /events subscribers that status may have changed."""
    def wake():
        global _status_changed
        if _status_changed is not None:
            _status_changed.set()
            _status_changed = asyncio.Event()
    if _status_loop is not None:
        _status_loop.call_soon_threadsafe(wake)


def _cloud_state(state, error=None):
    _cloud.update(state=state, error=error, since=time.time())
    _notify()


# The activity card is a record of the conversation: what the User said and what
# was said back. Provider tool lines ("run ...", "edit ...") were landing here
# too, rendered as red warnings, so ordinary work looked like a wall of errors.
# Failures still reach the User through the repair banner and Troubleshooting.
_SPOKEN_ACTIVITY_KINDS = frozenset({"heard", "spoke"})


def _note(kind, text):
    """Retain a bounded native-UI activity feed, never a cloud history."""
    if kind not in _SPOKEN_ACTIVITY_KINDS:
        return
    _activity.append(
        {
            "at": time.time(),
            "kind": kind,
            "text": " ".join(str(text).split())[:160],
        }
    )
    _notify()


def _seed_activity(lines):
    """Refill the feed from the on-disk speech record after a restart.

    The feed lives in memory, so a restart emptied it while All transcripts
    still showed the same conversation and the dashboard read as broken.
    """
    for line in lines:
        when, _, rest = line.partition("\t")
        _, _, rest = rest.partition("\t")
        who, _, text = rest.partition("\t")
        try:
            at = float(when)
        except ValueError:
            continue
        if text:
            _activity.append(
                {
                    "at": at,
                    "kind": "spoke" if who == "said" else "heard",
                    "text": " ".join(text.split())[:160],
                }
            )


def _spawn(coro):
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def turn_gate():
    global _turn_gate
    if _turn_gate is None:
        _turn_gate = asyncio.Lock()
    return _turn_gate


def authorized(token):
    """A missing configured token must never authorize local assistant access."""
    return bool(AUTH) and (
        isinstance(token, str) and token.isascii() and secrets.compare_digest(token, AUTH)
    )


def _token(path, headers):
    header = headers.get("Authorization") if headers else None
    if header:
        return header
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    return query.get("auth", [None])[0]


def _response(status, body, content_type):
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    body = body.encode() if isinstance(body, str) else body
    return Response(
        status.value,
        status.phrase,
        Headers(
            {
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "Connection": "close",
            }
        ),
        body,
    )


def _active_device():
    pair = _route_pair.get()
    if pair is None:
        primaries = {
            mux.primary_pair
            for mux in _routed_multiplexers
            if mux.primary_pair is not None
        }
        if len(primaries) == 1:
            pair = next(iter(primaries))
        elif not primaries:
            pair = _last_primary_pair
    return dict(_pair_devices.get(pair, _empty_device))


def _blocked_apps():
    try:
        import preflight

        return preflight.blocked_apps()
    except Exception:  # noqa: BLE001 - diagnostics cannot break status
        return set()


async def http_route(path, token):
    """The native app's only local HTTP API."""
    if not authorized(token):
        return (
            http.HTTPStatus.UNAUTHORIZED,
            "WalkieOne local authentication is required.",
            "text/plain; charset=utf-8",
        )
    route = urllib.parse.urlsplit(path).path
    if route != "/status":
        return (
            http.HTTPStatus.NOT_FOUND,
            "not found",
            "text/plain; charset=utf-8",
        )

    return (
        http.HTTPStatus.OK,
        json.dumps(await _status_snapshot(), separators=(",", ":")),
        "application/json",
    )


def _primary_online():
    if not _pair_online:
        return False
    pair = next(
        (mux.primary_pair for mux in _routed_multiplexers if mux.primary_pair),
        _last_primary_pair,
    )
    return bool(_pair_online.get(pair)) if pair else any(_pair_online.values())


async def _status_snapshot(include_uptime=True):
    import agent

    snapshot = {
        "cloud": dict(_cloud, url=bool(CLOUD_URL)),
        "hardware": _primary_online(),
        "device": _active_device(),
        "agent": agent.status(),
        "activity": list(_activity),
        "needs_permission": sorted(_blocked_apps()),
    }
    if include_uptime:
        snapshot["uptime"] = round(time.time() - _started, 1)
    return snapshot


async def _status_events(ws):
    """Push status to the native window when it changes; nothing to poll.

    A slow in-process re-check catches state no event names (for example an
    approval request reaching its time limit). It never makes a network call.
    """
    global _status_changed
    if _status_changed is None:
        _status_changed = asyncio.Event()
    last = None
    while True:
        changed = _status_changed
        current = await _status_snapshot(include_uptime=False)
        if current != last:
            current["uptime"] = round(time.time() - _started, 1)
            await ws.send(json.dumps(current, separators=(",", ":")))
            current.pop("uptime")
            last = current
        try:
            await asyncio.wait_for(changed.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


async def process_request(_connection, request):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    try:
        status, body, content_type = await http_route(
            request.path, _token(request.path, request.headers)
        )
    except Exception as error:  # noqa: BLE001 - status failure stays local
        print(f"  (status: {type(error).__name__}: {str(error)[:160]})")
        status, body, content_type = (
            http.HTTPStatus.INTERNAL_SERVER_ERROR,
            "WalkieOne status is unavailable.",
            "text/plain; charset=utf-8",
        )
    return _response(status, body, content_type)


async def send_proactive(
    request: LocalSendRequest, *, rate_limited: bool = True
) -> LocalSendReceipt:
    """Send through exactly one connected Link without exposing its secret.

    Local senders (socket, outbox, tasks, alerts) share the rate limit. The
    answer to a turn the User approved on the Mac passes rate_limited=False so
    background work cannot starve a reply someone is waiting for.
    """
    candidates = [
        mux for mux in list(_routed_multiplexers) if mux.primary_pair is not None
    ]
    if len(candidates) != 1:
        return LocalSendReceipt.rejected("link_disconnected")
    # The turn's own answer is already on its way to the Device, so a send from
    # that same turn is the same thing arriving twice. Refused with a reason the
    # Agent can act on inside the turn it is still in. Anything it detached
    # carries the same token but fires later, when this is no longer the turn
    # that is speaking, so scheduled delivery is unaffected.
    if request.turn:
        import agent

        if request.turn == agent.active_turn_token():
            return LocalSendReceipt.rejected("answer_instead")
    if rate_limited and not _proactive_rate_limit.admit():
        return LocalSendReceipt.rejected("rate_limited")
    receipt = await candidates[0].send_proactive(request)
    if receipt.status == "queued":
        # A message sent while nobody is listening is the one most likely to be
        # missed, so it is readable here without waiting for playback.
        _note("spoke", request.text)
    return receipt


class _RoutedDeviceSocket:
    """One Pair's private Link frames on a shared Channel socket."""

    def __init__(self, multiplexer, pair):
        self.multiplexer = multiplexer
        self.route_pair = pair
        self._incoming = asyncio.Queue()
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self._incoming.get()
        if frame is _routed_end:
            raise StopAsyncIteration
        return frame

    async def send(self, frame):
        if self._closed:
            raise ConnectionError("logical Device is closed")
        await self.multiplexer.send_to(self.route_pair, frame)

    def feed(self, frame):
        if not self._closed:
            self._incoming.put_nowait(frame)

    def finish(self):
        if not self._closed:
            self._closed = True
            self._incoming.put_nowait(_routed_end)


class _LinkMultiplexer:
    """Demultiplex Pair-tagged private controls over one Channel Link."""

    def __init__(self, raw):
        self.raw = raw
        self._send_lock = asyncio.Lock()
        self._devices = {}
        self._handlers = {}
        self._proactive_waiters = {}
        self.primary_pair = None
        self._ignored_extensions = set()

    def _ignore_extension(self, category):
        # Fixed categories keep logs and memory bounded even for many new types.
        # Never print unknown fields, which may contain private relay data.
        if category not in self._ignored_extensions:
            self._ignored_extensions.add(category)
            print(f"relay: ignoring {category}")

    async def send_control(self, frame):
        async with self._send_lock:
            await self.raw.send(frame)

    async def send_to(self, pair, frame):
        async with self._send_lock:
            try:
                await self.raw.send(
                    json.dumps(
                        {"v": 1, "type": "link.output", "pair": pair},
                        separators=(",", ":"),
                    )
                )
                await self.raw.send(frame)
            except asyncio.CancelledError:
                # An interrupted write may leave cloud waiting for the second
                # half of the envelope. Only this framing fault closes WSS;
                # waiting too long for a receipt after both writes never does.
                _spawn(self.raw.close(1011, "Link write interrupted"))
                raise

    async def send_proactive(self, request):
        pair = self.primary_pair
        if pair is None:
            return LocalSendReceipt.rejected("link_disconnected")
        if len(self._proactive_waiters) >= MAX_PROACTIVE_INFLIGHT:
            return LocalSendReceipt.rejected("mailbox_full")
        request_id = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self._proactive_waiters[request_id] = (pair, future)

        async def exchange():
            await self.send_to(
                pair,
                json.dumps(
                    {
                        "v": 1,
                        "type": "link.proactive.send",
                        "id": request_id,
                        "idempotency_key": request.idempotency_key,
                        "text": request.text,
                    },
                    separators=(",", ":"),
                ),
            )
            return await future

        try:
            try:
                return await asyncio.wait_for(
                    exchange(), PROACTIVE_RECEIPT_TIMEOUT
                )
            except (TimeoutError, asyncio.TimeoutError):
                # Mailbox latency says nothing about socket liveness. A late
                # receipt is harmless; retries retain the idempotency key.
                return LocalSendReceipt.rejected("cloud_unavailable")
        except Exception:  # noqa: BLE001 - stable local receipt contract
            return LocalSendReceipt.rejected("link_disconnected")
        finally:
            current = self._proactive_waiters.get(request_id)
            if current is not None and current[1] is future:
                self._proactive_waiters.pop(request_id, None)

    def _proactive_receipt(self, pair, message):
        request_id = message.get("id")
        status = message.get("status")
        if (
            message.get("v") != 1
            or not isinstance(request_id, str)
            or re.fullmatch(r"[a-f0-9]{32}", request_id) is None
            or status not in {"queued", "rejected"}
        ):
            raise ValueError("cloud sent an invalid proactive receipt")
        if status == "queued":
            try:
                message_id = str(uuid.UUID(message.get("message_id", "")))
            except (AttributeError, ValueError) as error:
                raise ValueError(
                    "cloud sent an invalid proactive receipt"
                ) from error
            if set(message) - {"v", "type", "id", "status", "message_id"}:
                self._ignore_extension("extra Link fields")
            receipt = LocalSendReceipt.queued(message_id)
        else:
            reason = message.get("reason")
            if reason not in {
                "cloud_unavailable",
                "idempotency_conflict",
                "mailbox_full",
                "route_changed",
            }:
                # A newer relay may add reasons; the request still failed and
                # a retry with the same key is safe.
                self._ignore_extension("unknown receipt reason")
                reason = "cloud_unavailable"
            if set(message) - {"v", "type", "id", "status", "reason"}:
                self._ignore_extension("extra Link fields")
            receipt = LocalSendReceipt.rejected(reason)
        waiter = self._proactive_waiters.get(request_id)
        if waiter is not None and waiter[0] == pair and not waiter[1].done():
            waiter[1].set_result(receipt)

    async def _proactive_played(self, pair, message):
        message_id = message.get("id")
        text = message.get("text")
        try:
            message_id = str(uuid.UUID(message_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError(
                "cloud sent an invalid proactive played receipt"
            ) from error
        if (
            message.get("v") != 1
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 1200
        ):
            raise ValueError("cloud sent an invalid proactive played receipt")
        if set(message) - {"v", "type", "id", "text"}:
            self._ignore_extension("extra Link fields")
        if message_id not in _cloud_proactive_acked:
            import agent

            spoken = text.strip()
            # Only what was played becomes conversation the Agent may refer
            # back to; the card showed it when it was sent.
            agent.said("said", spoken, DEVICE)
            _cloud_proactive_acked[message_id] = None
            while len(_cloud_proactive_acked) > MAX_RECEIPTS:
                _cloud_proactive_acked.popitem(last=False)
        await self.send_to(
            pair,
            json.dumps(
                {
                    "v": 1,
                    "type": "link.proactive.acknowledged",
                    "id": message_id,
                },
                separators=(",", ":"),
            ),
        )

    def _device(self, pair):
        device = self._devices.get(pair)
        if device is None:
            device = _RoutedDeviceSocket(self, pair)
            self._devices[pair] = device
            self._handlers[pair] = asyncio.create_task(self._serve(device))
        return device

    async def _serve(self, device):
        token = _route_pair.set(device.route_pair)
        try:
            await handle(device, outbound=True)
        finally:
            _route_pair.reset(token)
            if self._devices.get(device.route_pair) is device:
                del self._devices[device.route_pair]
            self._handlers.pop(device.route_pair, None)

    async def run(self):
        global _last_primary_pair
        pending = None
        _routed_multiplexers.add(self)
        try:
            async for frame in self.raw:
                if pending is None:
                    try:
                        route = json.loads(frame) if isinstance(frame, str) else None
                    except ValueError:
                        route = None
                    if (isinstance(route, dict)
                            and isinstance(route.get("type"), str)
                            and route["type"] not in {
                                "link.route", "link.primary", "link.device"
                            }):
                        # Only standalone controls are safe to skip; see README
                        # before adding one that precedes a paired payload.
                        self._ignore_extension("unknown Link control")
                        continue
                    pair = route.get("pair") if isinstance(route, dict) else None
                    valid_pair = (
                        isinstance(pair, str)
                        and re.fullmatch(r"[a-z0-9-]{1,32}", pair) is not None
                    )
                    if (
                        isinstance(route, dict)
                        and route.get("v") == 1
                        and route.get("type") == "link.primary"
                        and valid_pair
                    ):
                        self.primary_pair = pair
                        _last_primary_pair = pair
                        _notify()
                        continue
                    if (
                        isinstance(route, dict)
                        and route.get("v") == 1
                        and route.get("type") == "link.device"
                        and valid_pair
                        and isinstance(route.get("online"), bool)
                    ):
                        _set_pair_online(pair, route["online"])
                        continue
                    if (
                        not isinstance(route, dict)
                        or route.get("v") != 1
                        or route.get("type") != "link.route"
                        or not valid_pair
                        or not isinstance(route.get("primary"), bool)
                    ):
                        raise ValueError(
                            "cloud sent an invalid Link route envelope"
                        )
                    if route["primary"]:
                        self.primary_pair = pair
                        _last_primary_pair = pair
                    pending = pair
                    continue
                try:
                    message = json.loads(frame) if isinstance(frame, str) else None
                except ValueError:
                    message = None
                if isinstance(message, dict) and message.get("type") in (
                    "link.route", "link.primary", "link.device"
                ):
                    raise ValueError("cloud sent a Link route without its payload")
                if isinstance(message, dict) and message.get("type") == (
                    "link.proactive.receipt"
                ):
                    self._proactive_receipt(pending, message)
                elif isinstance(message, dict) and message.get("type") == (
                    "link.proactive.played"
                ):
                    await self._proactive_played(pending, message)
                else:
                    self._device(pending).feed(frame)
                pending = None
            if pending is not None:
                raise ValueError("cloud ended a Link route without its payload")
        finally:
            _routed_multiplexers.discard(self)
            _clear_pair_online()
            for _pair, future in self._proactive_waiters.values():
                if not future.done():
                    future.set_result(
                        LocalSendReceipt.rejected("link_disconnected")
                    )
            self._proactive_waiters.clear()
            handlers = list(self._handlers.values())
            for device in list(self._devices.values()):
                device.finish()
            if handlers:
                await asyncio.gather(*handlers, return_exceptions=True)


def _set_pair_online(pair, online):
    global _pair_online
    if _pair_online is None:
        _pair_online = {}
    _pair_online[pair] = online
    _notify()


def _clear_pair_online():
    """Presence is per relay connection; the next one pushes it again."""
    global _pair_online
    _pair_online = None
    _notify()


async def _publish_link_presence(state, busy_id=None, failure=None):
    payload = {"v": 1, "type": "link.presence", "agent": state, "steering": True}
    if state == "busy" and busy_id is not None:
        payload["busy_id"] = busy_id
        if busy_id in _native_turn_tasks:
            payload["steering_continuation"] = True
    if state == "unavailable" and failure is not None:
        payload["failure"] = failure
    frame = json.dumps(payload, separators=(",", ":"))
    _notify()
    await asyncio.gather(
        *(
            mux.send_control(frame)
            for mux in list(_routed_multiplexers)
        ),
        return_exceptions=True,
    )


def _idle_agent_presence(agent):
    if agent.busy():
        return "busy", None
    current = agent.status()
    failure = (
        current["failure"] if current["state"] == "unavailable" else None
    )
    # "waiting" is native UI state, not a cloud presence value. The provider
    # is paused and ready for the next spoken answer. Sending it on the wire
    # closes the relay connection (4400), losing that answer before approval.
    state = "ready" if current["state"] == "waiting" else current["state"]
    return state, failure


async def _publish_idle_link_presence(agent):
    state, failure = _idle_agent_presence(agent)
    await _publish_link_presence(state, failure=failure)


async def _send_cloud_turn_error(ws, turn_id, reason=None):
    payload = {"v": 1, "type": "link.turn.error", "id": turn_id}
    if reason is not None:
        payload["reason"] = reason
    await ws.send(json.dumps(payload, separators=(",", ":")))


async def run_cloud_turn(turn_id, heard, ws, to=DEVICE):
    """Run cloud-transcribed English and return only final speakable text."""
    global _agent_busy_id
    import agent

    gate = turn_gate()
    claimed = False
    try:
        if gate.locked() or agent.busy():
            await _send_cloud_turn_error(ws, turn_id, "busy")
            return
        async with gate:
            if agent.busy():
                await _send_cloud_turn_error(ws, turn_id, "busy")
                return
            claimed = True
            _cloud_turn_running.add(turn_id)
            _cloud_turn_pairs[turn_id] = getattr(ws, "route_pair", None)
            _agent_busy_id = turn_id
            print("  cloud speech request received")
            _note("heard", heard)
            agent.said("heard", heard, to)
            await _publish_link_presence("busy", turn_id)
            outcome = await asyncio.to_thread(agent.ask_result, heard, to)
            if outcome.failure is not None:
                _note("failed", outcome.failure.code)
                await _send_cloud_turn_error(
                    ws, turn_id, outcome.failure.code
                )
                return
            result = (outcome.text or "").strip()
            if not result:
                raise RuntimeError("Agent Endpoint returned no spoken output")
            # Recorded now rather than on the playback receipt: the card is how
            # someone reads a reply they did not catch, which is exactly the
            # case where the receipt never arrives.
            _note("spoke", result)
            _cloud_turn_results[turn_id] = (result, to)
            if outcome.interaction_id is not None:
                _cloud_turn_interactions[turn_id] = outcome.interaction_id
                _remember_interaction_pair(outcome.interaction_id, getattr(ws, "route_pair", None))
            while len(_cloud_turn_results) > MAX_RECEIPTS:
                forgotten, _ = _cloud_turn_results.popitem(last=False)
                _cloud_turn_interactions.pop(forgotten, None)
            try:
                payload = {
                    "v": 1,
                    "type": "link.turn.result",
                    "id": turn_id,
                    "text": result,
                    "needs_answer": agent.answer_required(),
                }
                if outcome.provider_interaction_ttl_seconds is not None:
                    payload["provider_interaction_ttl_seconds"] = (
                        outcome.provider_interaction_ttl_seconds
                    )
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 120
                interaction_deadline = (
                    loop.time() + outcome.provider_interaction_ttl_seconds
                    if outcome.provider_interaction_ttl_seconds is not None else None
                )
                destination = ws
                failed_multiplexers = set()
                while True:
                    if loop.time() >= deadline:
                        raise ConnectionError("result reconnect grace expired")
                    if interaction_deadline is not None:
                        remaining = interaction_deadline - loop.time()
                        if remaining <= 0:
                            raise ConnectionError("provider interaction expired")
                        from math import ceil
                        payload["provider_interaction_ttl_seconds"] = ceil(remaining)
                    if destination is not None:
                        encoded = json.dumps(payload, separators=(",", ":"))
                        try:
                            await asyncio.wait_for(destination.send(encoded), 5)
                            break
                        except (ConnectionError, OSError, TimeoutError):
                            pass
                        except Exception as send_error:
                            from websockets.exceptions import ConnectionClosed
                            if not isinstance(send_error, ConnectionClosed):
                                raise
                        failed = getattr(destination, "multiplexer", None)
                        if failed is not None:
                            failed_multiplexers.add(failed)
                            close = getattr(failed.raw, "close", None)
                            if close is not None:
                                _spawn(close(1011, "result delivery interrupted"))
                        destination = None
                    # Retry only the completed result, with its original turn
                    # and Pair. Never run the provider action a second time.
                    pair = getattr(ws, "route_pair", None)
                    if pair is None:
                        raise ConnectionError("result has no reconnect route")
                    await asyncio.sleep(0.1)
                    current = next(
                        (mux for mux in _routed_multiplexers
                         if mux not in failed_multiplexers
                         and mux.primary_pair is not None), None
                    )
                    if current is not None:
                        destination = current._device(pair)
            except BaseException:
                _cloud_turn_results.pop(turn_id, None)
                interaction_id = _cloud_turn_interactions.pop(turn_id, None)
                if interaction_id is not None:
                    agent.cancel(to)
                raise
    except asyncio.CancelledError:
        raise
    except Exception as error:  # provider detail never crosses Link
        print(
            f"  (cloud turn failed: {type(error).__name__})"
        )
        _note("failed", type(error).__name__)
        try:
            await _send_cloud_turn_error(ws, turn_id)
        except Exception:  # noqa: BLE001 - the socket is already failing
            pass
    finally:
        _cloud_turn_running.discard(turn_id)
        _cloud_turn_pairs.pop(turn_id, None)
        if claimed:
            if _agent_busy_id == turn_id:
                _agent_busy_id = None
            await _publish_idle_link_presence(agent)


def _remember_interaction_pair(interaction_id, pair):
    _interaction_pairs[interaction_id] = pair
    while len(_interaction_pairs) > MAX_RECEIPTS:
        _interaction_pairs.popitem(last=False)


async def run_cloud_steer(identifier, target, heard, ws):
    """One bounded, idempotent follow-up to this Pair's exact active turn."""
    import agent
    pair = getattr(ws, "route_pair", None)
    key = (pair, identifier)
    digest = hashlib.sha256((target + "\0" + heard).encode()).hexdigest()
    retained = _cloud_steer_receipts.get(key)
    if retained is not None:
        status = (await asyncio.shield(retained[1])
                  if retained[0] == digest else "rejected")
    elif sum(not row[1].done() for row in _cloud_steer_receipts.values()) >= 4:
        status = "rejected"
    else:
        receipt = asyncio.get_running_loop().create_future()
        _cloud_steer_receipts[key] = (digest, receipt)
        status = "unknown"
        try:
            task = _cloud_turn_tasks.get(target) or _native_turn_tasks.get(target)
            if target in _cloud_turn_pairs and _cloud_turn_pairs[target] != pair:
                status = "rejected"
            elif task is None or task.done():
                status = "inactive"
            else:
                # The root is published before its executor thread starts.
                deadline = asyncio.get_running_loop().time() + 6
                while (not (agent.busy() and _agent_busy_id == target)
                       and not task.done() and target not in _cloud_turn_results):
                    if asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(0.01)
                status = (await asyncio.to_thread(agent.steer, heard, DEVICE)
                          if agent.busy() and _agent_busy_id == target else "inactive")
                if status == "inactive" and not task.done():
                    # Ensure the old result precedes the inactive receipt, so
                    # cloud can safely admit this input as the next turn.
                    try:
                        await asyncio.wait_for(asyncio.shield(task), 3)
                    except (TimeoutError, asyncio.TimeoutError):
                        status = "rejected"
                if status == "accepted":
                    _note("heard", heard)
                    agent.said("heard", heard, DEVICE)
        except asyncio.CancelledError:
            raise
        except Exception:
            status = "unknown"
        finally:
            receipt.set_result(status)
            for old_key, (_, old_receipt) in list(_cloud_steer_receipts.items()):
                if len(_cloud_steer_receipts) <= MAX_RECEIPTS:
                    break
                if old_receipt.done():
                    del _cloud_steer_receipts[old_key]
    try:
        async with asyncio.timeout(5):
            await ws.send(json.dumps({"v": 1, "type": "link.turn.steered", "id": identifier,
                                      "turn_id": target, "status": status}, separators=(",", ":")))
    except Exception:
        pass  # A lost receipt never repeats the provider input.


async def _cancel_active_or_claimed(agent, scope, gate, busy_id=None):
    """Cancel the exact shared Agent generation, including its claim race."""
    while gate.locked() and (busy_id is None or _agent_busy_id == busy_id):
        if agent.busy():
            return agent.cancel(scope)
        await asyncio.sleep(0.01)
    return False


async def handle(ws, outbound=False):
    """Consume one Pair's private cloud-to-Link control stream."""
    if not outbound:
        await ws.close(code=4404, reason="local Device sockets are retired")
        return
    import agent

    try:
        async for raw in ws:
            if not isinstance(raw, str):
                print("  dropped binary private Link frame")
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                print("  dropped invalid private Link JSON")
                continue
            if not isinstance(message, dict) or message.get("v") != 1:
                print("  dropped invalid private Link message")
                continue
            kind = message.get("type")
            if kind == "link.turn.steer":
                identifier, target, heard = message.get("id"), message.get("turn_id"), message.get("text")
                if (not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value)
                            for value in (identifier, target))
                        or not isinstance(heard, str) or not heard.strip() or len(heard) > 60_000
                        or message.get("language") != "en-US"):
                    print("  dropped malformed cloud steering input")
                    continue
                _spawn(run_cloud_steer(identifier, target, heard.strip(), ws))
            elif kind == "link.turn.request":
                turn_id = message.get("id")
                heard = message.get("text")
                if (
                    not isinstance(turn_id, str)
                    or re.fullmatch(r"[a-f0-9]{32}", turn_id) is None
                    or not isinstance(heard, str)
                    or not heard.strip()
                    or len(heard) > 60_000
                    or message.get("language") != "en-US"
                ):
                    print("  dropped malformed cloud turn request")
                    continue
                if (turn_id in _cloud_turn_tasks or turn_id in _cloud_turn_results
                        or turn_id in _cloud_turn_acked):
                    print("  dropped duplicate cloud turn request")
                    continue
                task = _spawn(
                    run_cloud_turn(turn_id, heard.strip(), ws, DEVICE)
                )
                _cloud_turn_tasks[turn_id] = task
                _cloud_turn_pairs[turn_id] = getattr(ws, "route_pair", None)

                def forget_cloud_turn(done, turn_id=turn_id):
                    if _cloud_turn_tasks.get(turn_id) is done:
                        _cloud_turn_tasks.pop(turn_id, None)
                        _cloud_turn_pairs.pop(turn_id, None)
                    _cloud_turn_running.discard(turn_id)

                task.add_done_callback(forget_cloud_turn)
            elif kind == "link.turn.ack":
                turn_id = message.get("id")
                if (
                    not isinstance(turn_id, str)
                    or re.fullmatch(r"[a-f0-9]{32}", turn_id) is None
                ):
                    print("  dropped malformed cloud turn acknowledgment")
                    continue
                result = _cloud_turn_results.pop(turn_id, None)
                if result is not None:
                    text, scope = result
                    interaction_id = _cloud_turn_interactions.pop(turn_id, None)
                    if interaction_id is not None:
                        agent.acknowledge_interaction(interaction_id)
                    # Only what was actually played becomes conversation the
                    # Agent may refer back to; the card already showed it.
                    agent.said("said", text, scope)
                    _cloud_turn_acked[turn_id] = None
                    while len(_cloud_turn_acked) > MAX_RECEIPTS:
                        _cloud_turn_acked.popitem(last=False)
                if result is not None or turn_id in _cloud_turn_acked:
                    await ws.send(
                        json.dumps(
                            {
                                "v": 1,
                                "type": "link.turn.acknowledged",
                                "id": turn_id,
                            },
                            separators=(",", ":"),
                        )
                    )
            elif kind == "link.agent.cancel":
                request_id = message.get("id")
                busy_id = message.get("busy_id")
                if (
                    set(message) != {"v", "type", "id", "busy_id"}
                    or not isinstance(request_id, str)
                    or re.fullmatch(r"[a-f0-9]{32}", request_id) is None
                    or not isinstance(busy_id, str)
                    or re.fullmatch(r"[a-f0-9]{32}", busy_id) is None
                ):
                    print("  dropped malformed cloud Agent cancellation")
                    continue
                cancelled = await _cancel_active_or_claimed(
                    agent, None, turn_gate(), busy_id
                )
                if cancelled:
                    print("  cloud cancelled shared Agent turn")
                await ws.send(
                    json.dumps(
                        {
                            "v": 1,
                            "type": "link.agent.cancelled",
                            "id": request_id,
                            "busy_id": busy_id,
                            "cancelled": cancelled,
                        },
                        separators=(",", ":"),
                    )
                )
            elif kind == "link.turn.cancel":
                turn_id = message.get("id")
                if (
                    not isinstance(turn_id, str)
                    or re.fullmatch(r"[a-f0-9]{32}", turn_id) is None
                ):
                    print("  dropped malformed cloud turn cancellation")
                    continue
                task = _cloud_turn_tasks.get(turn_id)
                if task is None or task.done():
                    continue
                if turn_id not in _cloud_turn_running:
                    task.cancel()
                    print("  cloud cancelled Agent turn before it started")
                elif await _cancel_active_or_claimed(
                    agent, DEVICE, turn_gate(), turn_id
                ):
                    print("  cloud cancelled Agent turn")
            elif kind == "status":
                pair = _route_pair.get()
                if pair is not None:
                    _pair_devices[pair] = {
                        "battery_pct": message.get("battery_pct"),
                        "rssi": message.get("rssi"),
                        "fw": message.get("fw"),
                        "at": time.time(),
                    }
                    _notify()
            # hello/listen and Device playback controls are cloud-owned. They
            # may be forwarded during a rolling relay update and are ignored.
    except Exception as error:
        if error.__class__.__name__ != "ConnectionClosed":
            raise


async def relay_client():
    """Maintain the authenticated outbound Link with full-jitter backoff."""
    import websockets

    split = urllib.parse.urlsplit(CLOUD_URL)
    if split.scheme not in {"ws", "wss"} or split.path.rstrip("/") != "/link":
        _cloud_state("refused", "WalkieOne requires an active Link route")
        print("relay: configured URL is not a /link WebSocket")
        return

    backoff_min, backoff_max, backoff_pending, backoff_auth = 1, 60, 300, 900
    stable = 30
    ceiling = backoff_min
    while True:
        opened = None
        code = None
        try:
            if ceiling < backoff_auth:
                _cloud_state("connecting")
            async with websockets.connect(
                CLOUD_URL,
                additional_headers={"Authorization": RELAY_TOKEN},
                max_size=512 * 1024,  # 60k transcript chars, including JSON escapes
                ping_interval=30,
                ping_timeout=30,
            ) as ws:
                print(f"relay: connected to {CLOUD_URL}")
                _cloud_state("connected")
                opened = time.monotonic()
                multiplexer = _LinkMultiplexer(ws)
                import agent

                busy = agent.busy() or turn_gate().locked()
                idle_state, idle_failure = _idle_agent_presence(agent)
                state = (
                    "busy"
                    if busy and _agent_busy_id is not None
                    else idle_state
                )
                presence = {
                    "v": 1,
                    "type": "link.presence",
                    "agent": state,
                    # This Link understands pushed link.device presence.
                    "device_presence": True,
                    "steering": True,
                }
                if state == "busy":
                    presence["busy_id"] = _agent_busy_id
                    if _agent_busy_id in _native_turn_tasks:
                        presence["steering_continuation"] = True
                elif state == "unavailable" and idle_failure is not None:
                    presence["failure"] = idle_failure
                await multiplexer.send_control(
                    json.dumps(presence, separators=(",", ":"))
                )
                await multiplexer.run()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - reconnect owns transport faults
            closed = getattr(error, "rcvd", None)
            code = getattr(closed, "code", None)
            if code == 4409 and getattr(closed, "reason", "") == "Link is no longer active":
                # One connection per Walkie One: it moved to another assistant
                # or computer. Reconnecting cannot get it back; the native app
                # returns to the Claude/Codex chooser instead.
                print("relay: Device moved to another connection")
                _cloud_state("moved", "Device moved to another connection")
                return
            if code == 4401:
                print("relay: Link credential refused")
                _cloud_state("refused", "Link credential refused")
                ceiling = backoff_auth
            elif code == 4410:
                print("relay: Link route pending - retrying with backoff")
                _cloud_state("retrying", "Link route pending")
            else:
                detail = f"{type(error).__name__}: {str(error)[:120]}"
                print(f"relay: {detail} - retrying within {ceiling}s")
                _cloud_state("retrying", detail)
        maximum = (backoff_auth if code == 4401 else
                   backoff_pending if code == 4410 else backoff_max)
        if code != 4401 and opened is not None and time.monotonic() - opened > stable:
            # A deploy drops a fleet of otherwise healthy sockets together.
            ceiling = 15
        ceiling = min(ceiling, maximum)
        await asyncio.sleep(random.uniform(0, ceiling))
        ceiling = min(ceiling * 2, maximum)


async def _local_agent_socket(ws):
    """Run one typed, authenticated request through the same Device scope.

    This is intentionally a one-request socket. It gives setup and repair UI a
    real Agent turn without growing a second chat product or a second session
    namespace beside the walkie-talkie.
    """
    global _agent_busy_id
    import agent

    path = ws.request.path
    route = urllib.parse.urlsplit(path).path
    if route not in {"/agent", "/interaction", "/events"}:
        await ws.close(4404, "not found")
        return
    if not authorized(_token(path, ws.request.headers)):
        await ws.close(4401, "local authentication required")
        return
    if route == "/events":
        # A quiet status stream may have nothing more to send. Observe socket
        # closure independently so quitting never waits for a future update.
        stream = asyncio.create_task(_status_events(ws))
        closed = asyncio.create_task(ws.wait_closed())
        try:
            done, _ = await asyncio.wait((stream, closed), return_when=asyncio.FIRST_COMPLETED)
            if stream in done:
                await stream
        finally:
            stream.cancel()
            closed.cancel()
            await asyncio.gather(stream, closed, return_exceptions=True)
        return
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        request = json.loads(raw)
        if route == "/interaction":
            interaction_id = request.get("id")
            decision = request.get("decision")
            if (
                request.get("v") != 1
                or not isinstance(interaction_id, str)
                or re.fullmatch(r"[a-f0-9]{32}", interaction_id) is None
                or decision not in {"allow", "deny"}
            ):
                await ws.send(json.dumps({"v": 1, "ok": False,
                                          "error": "invalid_request"}))
                return
            pending = agent.pending_interaction()
            if pending is None or pending.id != interaction_id:
                await ws.send(json.dumps({"v": 1, "ok": False,
                                          "error": "already_resolved"}))
                return
            # A visible Mac button may race the Device playback receipt. The
            # exact spoken request must land before Allow, otherwise the User
            # can approve one action while hearing an older question. Decline
            # needs no playback receipt because it grants no authority.
            require_acknowledged = pending.origin == "device" and decision == "allow"
            if require_acknowledged and not pending.acknowledged:
                await ws.send(json.dumps({"v": 1, "ok": False,
                                          "error": "awaiting_playback"}))
                return
            gate = turn_gate()
            if gate.locked() or agent.busy():
                await ws.send(json.dumps({"v": 1, "ok": False,
                                          "error": "busy"}))
                return
            request_id = uuid.uuid4().hex
            origin_pair = _interaction_pairs.get(interaction_id)
            try:
                async with gate:
                    if agent.busy():
                        await ws.send(json.dumps({"v": 1, "ok": False,
                                                  "error": "busy"}))
                        return
                    _agent_busy_id = request_id
                    _interaction_pairs.pop(interaction_id, None)
                    _native_turn_tasks[request_id] = asyncio.current_task()
                    _cloud_turn_pairs[request_id] = origin_pair
                    await _publish_link_presence("busy", request_id)
                    outcome = await asyncio.to_thread(
                        agent.resolve_interaction,
                        interaction_id,
                        decision,
                        scope=DEVICE,
                        origin="mac",
                        require_acknowledged=require_acknowledged,
                    )
                    if outcome.text == agent.BUSY_REPLY:
                        await ws.send(json.dumps({"v": 1, "ok": False,
                                                  "error": "already_resolved"}))
                        return
                    # Failure text is a host-owned repair message. Deliver it
                    # to the Device too when an approval resumes into an OS
                    # denial; provider diagnostics remain local.
                    if outcome.failure is not None:
                        _note("failed", outcome.failure.code)
                    answer = (outcome.text or "").strip()
                    if not answer:
                        raise RuntimeError("Agent Endpoint returned no output")
                    if outcome.interaction_id is not None:
                        _remember_interaction_pair(outcome.interaction_id, origin_pair)
                        _note("note", answer)
                    delivered = None
                    if (getattr(pending, "delivery_origin", pending.origin)
                            == "device"
                            and outcome.interaction_id is None):
                        delivered = await send_proactive(LocalSendRequest(
                            text=answer,
                            idempotency_key=(
                                "provider-interaction-" + interaction_id),
                        ), rate_limited=False)
                        if delivered.status != "queued":
                            _note("failed", "Device delivery failed")
                    else:
                        _note("spoke", answer)
                        agent.said("said", answer, DEVICE)
                    await ws.send(json.dumps({
                        "v": 1,
                        "ok": True,
                        "text": answer,
                        "queued_for_device": delivered is not None
                            and delivered.status == "queued",
                    }, separators=(",", ":")))
            finally:
                _native_turn_tasks.pop(request_id, None)
                _cloud_turn_pairs.pop(request_id, None)
                if _agent_busy_id == request_id:
                    _agent_busy_id = None
                await _publish_idle_link_presence(agent)
            return
        text = " ".join(str(request.get("text", "")).split())
        if request.get("v") != 1 or not text or len(text) > 2000:
            await ws.send(json.dumps({"v": 1, "ok": False,
                                      "error": "invalid_request"}))
            return
    except (asyncio.TimeoutError, AttributeError, TypeError, ValueError):
        await ws.send(json.dumps({"v": 1, "ok": False,
                                  "error": "invalid_request"}))
        return

    gate = turn_gate()
    if gate.locked() or agent.busy():
        await ws.send(json.dumps({"v": 1, "ok": False, "error": "busy"}))
        return

    request_id = uuid.uuid4().hex
    try:
        async with gate:
            if agent.busy():
                await ws.send(json.dumps({"v": 1, "ok": False,
                                          "error": "busy"}))
                return
            _agent_busy_id = request_id
            _note("heard", text)
            agent.said("heard", text, DEVICE)
            await _publish_link_presence("busy", request_id)
            outcome = await asyncio.to_thread(
                agent.ask_result, text, DEVICE, "mac"
            )
            if outcome.failure is not None:
                _note("failed", outcome.failure.code)
                await ws.send(json.dumps({"v": 1, "ok": False,
                                          "error": outcome.failure.code}))
                return
            answer = (outcome.text or "").strip()
            if not answer:
                raise RuntimeError("Agent Endpoint returned no output")
            _note("spoke", answer)
            agent.said("said", answer, DEVICE)
            await ws.send(json.dumps({"v": 1, "ok": True, "text": answer},
                                     separators=(",", ":")))
    except Exception as error:  # noqa: BLE001 - provider detail stays local
        print(f"  (local agent turn failed: {type(error).__name__}: "
              f"{agent.redact_diagnostic(error)})")
        _note("failed", type(error).__name__)
        await ws.send(json.dumps({"v": 1, "ok": False,
                                  "error": "request_failed"}))
    finally:
        if _agent_busy_id == request_id:
            _agent_busy_id = None
        await _publish_idle_link_presence(agent)


def task_env(name):
    """The provider boundary, minus the turn token.

    A task's send is never the answer of whatever turn happens to be speaking
    when it runs, so it must not be refused as `answer_instead`.
    """
    import agent

    env = agent.child_env()
    env.pop("WALKIEONE_TURN", None)
    env["WALKIEONE_TASK_NAME"] = name
    return env


def alert_writer():
    """The coding agent that writes watcher alerts: the selected one if it can,
    otherwise whichever of Claude Code or Codex is installed."""
    import agent

    for name in dict.fromkeys((agent.BACKEND, "claude", "codex")):
        if name in ("claude", "codex"):
            path = agent.executable(name)
            if path:
                return name, path
    return None


async def maintain_history():
    import agent

    while True:
        try:
            await asyncio.to_thread(agent.prune_history)
        except OSError:
            print("local history cleanup failed; will retry")
        await asyncio.sleep(60)


async def serve():
    import agent
    import websockets

    global _status_loop
    _status_loop = asyncio.get_running_loop()
    agent.set_event_sink(_note)
    agent.setup()
    try:
        _seed_activity(agent._history().read())
    except OSError:
        pass
    tasks = TaskRunner(
        default_tasks_path(),
        send_proactive,
        task_env,
        agent.WORKDIR,
        request=lambda text, key: LocalSendRequest(text=text, idempotency_key=key),
    )
    tasks.load()
    alerts = AlertWriter(
        send_proactive,
        lambda text, key: LocalSendRequest(text=text, idempotency_key=key),
        alert_writer,
        lambda: task_env("alert"),
    )
    routes = task_routes(tasks.handle)
    routes[b"POST /v1/alerts HTTP/1.1"] = alerts.handle
    async with websockets.serve(
        _local_agent_socket,
        HOST,
        PORT,
        max_size=4096,
        ping_interval=240,
        ping_timeout=60,
        process_request=process_request,
    ), LocalSendServer(default_socket_path(), send_proactive, routes=routes) as send_server:
        try:
            print(f"Link status listening on http://{HOST}:{PORT}/status")
            print(f"proactive send listening on {default_socket_path()}")
            # A sandboxed Agent cannot open the socket - that is a network
            # operation - so it leaves sends as files in its own workspace and
            # this drains them.
            outbox = default_outbox_path()
            print(f"proactive outbox draining {outbox}")
            _spawn(LocalSendOutbox(outbox, send_proactive).run())
            print(f"background tasks running from {tasks.path} ({len(tasks.tasks)})")
            _spawn(tasks.run())
            _spawn(maintain_history())
            if CLOUD_URL and RELAY_TOKEN:
                _spawn(relay_client())
            else:
                _cloud_state("off", "Link is not claimed on this computer")
                print("relay: no active Link credential")
            await asyncio.Future()
        finally:
            # Stop admissions before cancelling work. Unix-socket handlers own
            # alert subprocesses; the scheduler owns background task groups.
            await send_server.close()
            workers = list(_tasks)
            for worker in workers:
                worker.cancel()
            await asyncio.to_thread(agent.cancel)
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            _status_loop = None


def demo():
    assert urllib.parse.urlsplit("wss://example.test/link").path == "/link"
    assert not hasattr(__import__(__name__), "speak_to_pcm")
    assert not hasattr(__import__(__name__), "transcribe")
    print("link demo ok")


if __name__ == "__main__":
    import sys

    if "--demo" in sys.argv:
        demo()
    else:
        try:
            from runtime import run_service

            asyncio.run(run_service(serve))
        except KeyboardInterrupt:
            pass
