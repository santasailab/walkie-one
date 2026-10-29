"""Fleet resilience using the real Link boundary with deterministic relay/time."""

import asyncio
import collections
import json
from types import SimpleNamespace

import agent
import link
import proactive
import pytest
import tasks
import websockets
from proactive import LocalSendOutbox, LocalSendReceipt, LocalSendRequest
from websockets.frames import Close


class Relay:
    def __init__(self, frames=()):
        self.frames = list(frames)
        self.sent = []
        self.closed = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return json.dumps(self.frames.pop(0))

    async def send(self, frame):
        self.sent.append(json.loads(frame))

    async def close(self, *args):
        self.closed.append(args)


def route(**extra):
    return dict(v=1, type="link.route", pair="pair-a", primary=True, **extra)


def test_unknown_controls_and_extra_fields_preserve_route_pairing(monkeypatch, capsys):
    received = []

    async def handle(ws, **_kwargs):
        async for frame in ws:
            received.append((ws.route_pair, json.loads(frame)))

    monkeypatch.setattr(link, "handle", handle)
    raw = Relay(
        [
            {"v": 1, "type": "link.future", "secret": "do not log"},
            {"v": 1, "type": "link.other"},
            route(future=True),
            {"v": 1, "type": "status", "battery_pct": 90},
        ]
    )
    asyncio.run(link._LinkMultiplexer(raw).run())
    assert received == [("pair-a", {"v": 1, "type": "status", "battery_pct": 90})]
    output = capsys.readouterr().out
    assert output.count("unknown Link control") == 1
    assert "do not log" not in output


@pytest.mark.parametrize(
    "frames",
    [
        [{"v": 1, "type": "link.route", "pair": "bad/pair", "primary": True}],
        [{"v": 1, "type": "link.route", "pair": "pair-a"}],
        [route()],
        [route(), route()],
    ],
)
def test_malformed_route_or_missing_payload_still_fails(frames):
    with pytest.raises(ValueError):
        asyncio.run(link._LinkMultiplexer(Relay(frames)).run())


def test_proactive_receipts_allow_extensions_and_keep_exact_correlation(monkeypatch):
    remembered = []
    monkeypatch.setattr(agent, "said", lambda *args: remembered.append(args))
    monkeypatch.setattr(link, "_cloud_proactive_acked", collections.OrderedDict())

    async def exercise():
        raw = Relay()
        mux = link._LinkMultiplexer(raw)
        future = asyncio.get_running_loop().create_future()
        mux._proactive_waiters["a" * 32] = ("pair-a", future)
        receipt = {
            "v": 1,
            "type": "link.proactive.receipt",
            "id": "a" * 32,
            "status": "queued",
            "message_id": "11111111-0000-4000-8000-000000000000",
            "future": "ignored",
        }
        mux._proactive_receipt("pair-b", receipt)
        assert not future.done()
        mux._proactive_receipt("pair-a", receipt)
        assert future.result().status == "queued"
        await mux._proactive_played(
            "pair-a",
            {
                "v": 1,
                "type": "link.proactive.played",
                "id": receipt["message_id"],
                "text": "Finished.",
                "future": True,
            },
        )
        assert raw.sent[0]["pair"] == "pair-a"
        assert remembered == [("said", "Finished.", "device")]
        future = asyncio.get_running_loop().create_future()
        mux._proactive_waiters["a" * 32] = ("pair-a", future)
        mux._proactive_receipt(
            "pair-a",
            {
                "v": 1,
                "type": "link.proactive.receipt",
                "id": "a" * 32,
                "status": "rejected",
                "reason": "mailbox_full",
                "future": True,
            },
        )
        assert future.result().reason == "mailbox_full"

    asyncio.run(exercise())


def test_an_unknown_rejection_reason_is_cloud_unavailable_not_a_disconnect(capsys):
    async def exercise():
        mux = link._LinkMultiplexer(Relay())
        for request_id in ("b" * 32, "c" * 32):
            future = asyncio.get_running_loop().create_future()
            mux._proactive_waiters[request_id] = ("pair-a", future)
            mux._proactive_receipt(
                "pair-a",
                {
                    "v": 1,
                    "type": "link.proactive.receipt",
                    "id": request_id,
                    "status": "rejected",
                    "reason": "future_reason",
                },
            )
            assert future.result().reason == "cloud_unavailable"

    asyncio.run(exercise())
    assert capsys.readouterr().out.count("unknown receipt reason") == 1


def test_receipt_timeout_does_not_disconnect_a_healthy_socket(monkeypatch):
    monkeypatch.setattr(link, "PROACTIVE_RECEIPT_TIMEOUT", 0.01)

    async def exercise():
        raw = Relay()
        mux = link._LinkMultiplexer(raw)
        mux.primary_pair = "pair-a"
        receipt = await mux.send_proactive(LocalSendRequest("Finished.", "one"))
        await asyncio.sleep(0)
        assert receipt.reason == "cloud_unavailable"
        assert mux._proactive_waiters == {}
        assert raw.closed == []
        await mux.send_control('{"v":1,"type":"link.presence","agent":"ready"}')
        assert raw.sent[-1]["agent"] == "ready"

    asyncio.run(exercise())


def test_proactive_burst_is_rate_limited_across_reconnects(monkeypatch):
    sent = []

    async def send(request):
        sent.append(request)
        return LocalSendReceipt.queued("one")

    mux = SimpleNamespace(primary_pair="pair-a", send_proactive=send)
    monkeypatch.setattr(link, "_routed_multiplexers", [mux])

    async def exercise():
        for index in range(6):
            assert (
                await link.send_proactive(LocalSendRequest("News", str(index)))
            ).status == "queued"
        # A fresh multiplexer must not reset the process-wide send budget.
        monkeypatch.setattr(
            link,
            "_routed_multiplexers",
            [SimpleNamespace(primary_pair="pair-a", send_proactive=send)],
        )
        assert (
            await link.send_proactive(LocalSendRequest("More", "last"))
        ).reason == "rate_limited"
        assert len(sent) == 6

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "stable, code, expected",
    [
        (True, None, [15, 30, 60]),
        (False, 4410, [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300]),
        (True, 4401, [900, 900, 900]),
    ],
)
def test_reconnect_jitter_and_pending_backoff(monkeypatch, stable, code, expected):
    clock = [0.0]
    ceilings = []
    connects = []

    class Connection(Relay):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    async def run(_self):
        if stable and not ceilings:
            clock[0] += 31
        if code:
            raise websockets.exceptions.ConnectionClosedError(Close(code, "test"), None)

    def connect(*_args, **kwargs):
        connects.append(kwargs)
        return Connection()

    async def sleep(_delay):
        if len(ceilings) == len(expected):
            raise asyncio.CancelledError

    monkeypatch.setattr(
        link, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0])
    )
    monkeypatch.setattr(
        link,
        "asyncio",
        SimpleNamespace(CancelledError=asyncio.CancelledError, sleep=sleep),
    )
    monkeypatch.setattr(
        link.random, "uniform", lambda low, high: ceilings.append(high) or high / 2
    )
    monkeypatch.setattr(link, "CLOUD_URL", "wss://example.test/link")
    monkeypatch.setattr(link, "_turn_gate", asyncio.Lock())
    monkeypatch.setattr(link._LinkMultiplexer, "run", run)
    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setattr(agent, "busy", lambda: False)
    monkeypatch.setattr(agent, "status", lambda: {"state": "ready"})
    # The multiplexer creates asyncio primitives; retain these real constructors.
    link.asyncio.Lock = asyncio.Lock
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(link.relay_client())
    assert ceilings == expected
    assert all(call["ping_interval"] <= 30 for call in connects)


def write_request(path, **extra):
    path.write_text(
        json.dumps(dict(v=1, text="Reminder", idempotency_key="same-key", **extra))
    )


def test_due_outbox_message_survives_disconnect_and_retries_same_key(tmp_path):
    sent = []
    path = tmp_path / "due.request.json"
    write_request(path)

    async def send(request):
        assert path.exists(), "the request must survive an interrupted send"
        sent.append(request)
        return (
            LocalSendReceipt.rejected("link_disconnected")
            if len(sent) == 1
            else LocalSendReceipt.queued("one")
        )

    async def exercise():
        outbox = LocalSendOutbox(tmp_path, send)
        await outbox.drain_once()
        assert path.exists()
        await outbox.drain_once()
        assert not path.exists()
        assert [item.idempotency_key for item in sent] == ["same-key", "same-key"]

    asyncio.run(exercise())


def test_far_future_outbox_requests_cannot_exhaust_capacity(tmp_path):
    for index in range(LocalSendOutbox.MAX_PENDING):
        write_request(tmp_path / f"future-{index}.request.json", not_before=10**15)
    path = tmp_path / "due.request.json"
    write_request(path)
    sent = []

    async def send(request):
        sent.append(request)
        return LocalSendReceipt.queued("one")

    asyncio.run(LocalSendOutbox(tmp_path, send).drain_once())
    assert len(sent) == 1
    assert not list(tmp_path.glob("*.request.json"))
    assert (
        json.loads((tmp_path / "future-0.receipt.json").read_text())["reason"]
        == "invalid_request"
    )


def test_background_task_interval_has_a_one_minute_floor(tmp_path):
    runner = tasks.TaskRunner(tmp_path, None, None, tmp_path)
    with pytest.raises(tasks.TaskError, match="invalid_every"):
        runner.add({"name": ["fast"], "every": ["15"], "arg": ["/usr/bin/true"]})


def test_send_buckets_refill_and_enforce_the_hour_budget():
    clock = [0.0]
    limiter = proactive.SendRateLimit(clock=lambda: clock[0])
    assert all(limiter.admit() for _ in range(6))
    assert not limiter.admit()
    clock[0] = 10
    assert limiter.admit()
    assert not limiter.admit()
    # Sustained requests eventually exhaust the hourly bucket despite staying
    # within the short-term refill rate.
    refused = False
    for moment in range(20, 900, 10):
        clock[0] = moment
        refused |= not limiter.admit()
    assert refused
    clock[0] += 3600
    assert all(limiter.admit() for _ in range(6))
    assert not limiter.admit()


@pytest.mark.parametrize(
    "reason", ["link_disconnected", "cloud_unavailable", "rate_limited", "send_failed"]
)
def test_outbox_retry_window_survives_restart_and_expires(tmp_path, reason):
    now = 1_000_000.0
    path = tmp_path / "due.request.json"
    write_request(path, not_before=now)
    sent = []

    async def send(request):
        sent.append(request)
        return LocalSendReceipt.rejected(reason)

    async def exercise():
        await LocalSendOutbox(tmp_path, send, clock=lambda: now).drain_once()
        assert path.exists()
        await LocalSendOutbox(tmp_path, send, clock=lambda: now + 899).drain_once()
        assert path.exists()
        await LocalSendOutbox(tmp_path, send, clock=lambda: now + 900).drain_once()
        assert not path.exists()
        assert len(sent) == 2
        assert (
            json.loads((tmp_path / "due.receipt.json").read_text())["reason"]
            == "expired"
        )

    asyncio.run(exercise())


def test_cancelling_an_outbox_send_keeps_the_request(tmp_path):
    path = tmp_path / "due.request.json"
    write_request(path)

    async def send(_request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(LocalSendOutbox(tmp_path, send).drain_once())
    assert path.exists()


@pytest.mark.parametrize("moment", [True, "tomorrow", float("inf"), float("nan"), -1])
def test_outbox_rejects_invalid_scheduled_times(tmp_path, moment):
    path = tmp_path / "invalid.request.json"
    write_request(path, not_before=moment)
    assert LocalSendOutbox._read(path) is None
