import asyncio
import collections
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import agent
import link
import proactive
from proactive import LocalSendRequest


class _CloudSocket:
    def __init__(self):
        self.sent = []

    async def send(self, frame):
        self.sent.append(frame)


class _LocalSocket(_CloudSocket):
    def __init__(self, frame, path="/agent?auth=token"):
        super().__init__()
        self.frame = frame
        self.request = type("Request", (), {"path": path, "headers": {}})()
        self.closed = None

    async def recv(self):
        return self.frame

    async def close(self, code, reason):
        self.closed = (code, reason)


class _DuplexCloudSocket(_CloudSocket):
    def __init__(self):
        super().__init__()
        self.incoming = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.incoming.get()
        if frame is None:
            raise StopAsyncIteration
        return frame


def test_proactive_send_uses_the_current_primary_pair_and_waits_for_cloud_receipt(
    monkeypatch,
):
    async def exercise():
        raw = _DuplexCloudSocket()
        mux = link._LinkMultiplexer(raw)
        run = asyncio.create_task(mux.run())
        await raw.incoming.put(
            json.dumps({"v": 1, "type": "link.primary", "pair": "pair-a"})
        )
        while mux.primary_pair is None:
            await asyncio.sleep(0)

        send = asyncio.create_task(
            link.send_proactive(LocalSendRequest("The build finished.", "build-42"))
        )
        while len(raw.sent) < 2:
            await asyncio.sleep(0)
        route = json.loads(raw.sent[0])
        request = json.loads(raw.sent[1])
        await raw.incoming.put(
            json.dumps(
                {
                    "v": 1,
                    "type": "link.route",
                    "pair": "pair-a",
                    "primary": True,
                }
            )
        )
        await raw.incoming.put(
            json.dumps(
                {
                    "v": 1,
                    "type": "link.proactive.receipt",
                    "id": request["id"],
                    "status": "queued",
                    "message_id": "7d53976f-5f55-58bc-b491-c09679b0fb79",
                }
            )
        )
        receipt = await send
        await raw.incoming.put(None)
        await run
        return route, request, receipt

    monkeypatch.setattr(link, "_routed_multiplexers", set())
    route, request, receipt = asyncio.run(exercise())

    assert route == {"v": 1, "type": "link.output", "pair": "pair-a"}
    assert request["type"] == "link.proactive.send"
    assert request["text"] == "The build finished."
    assert request["idempotency_key"] == "build-42"
    assert receipt.status == "queued"
    assert receipt.message_id == "7d53976f-5f55-58bc-b491-c09679b0fb79"


def test_proactive_send_rejects_when_no_active_link_is_connected(monkeypatch):
    monkeypatch.setattr(link, "_routed_multiplexers", set())

    receipt = asyncio.run(
        link.send_proactive(LocalSendRequest("The build finished.", "build-42"))
    )

    assert receipt.status == "rejected"
    assert receipt.reason == "link_disconnected"


def test_proactive_send_deadline_includes_a_blocked_websocket_write(monkeypatch):
    class BlockedCloudSocket:
        def __init__(self):
            self.closed = []

        async def send(self, _frame):
            await asyncio.Event().wait()

        async def close(self, code, reason):
            self.closed.append((code, reason))

    async def exercise():
        raw = BlockedCloudSocket()
        mux = link._LinkMultiplexer(raw)
        mux.primary_pair = "pair-a"
        link._routed_multiplexers.add(mux)
        receipt = await link.send_proactive(
            LocalSendRequest("The build finished.", "build-42")
        )
        await asyncio.sleep(0)
        return raw, mux, receipt

    monkeypatch.setattr(link, "_routed_multiplexers", set())
    monkeypatch.setattr(link, "PROACTIVE_RECEIPT_TIMEOUT", 0.01)
    raw, mux, receipt = asyncio.run(exercise())

    assert receipt.reason == "cloud_unavailable"
    assert raw.closed == [(1011, "Link write interrupted")]
    assert mux._proactive_waiters == {}


def test_exact_proactive_playback_ack_updates_context_once(monkeypatch):
    async def exercise():
        raw = _DuplexCloudSocket()
        mux = link._LinkMultiplexer(raw)
        run = asyncio.create_task(mux.run())
        played = {
            "v": 1,
            "type": "link.proactive.played",
            "id": "7d53976f-5f55-58bc-b491-c09679b0fb79",
            "text": "The build finished.",
        }
        for _ in range(2):
            await raw.incoming.put(
                json.dumps(
                    {"v": 1, "type": "link.route", "pair": "pair-a", "primary": True}
                )
            )
            await raw.incoming.put(json.dumps(played))
        while len(raw.sent) < 4:
            await asyncio.sleep(0)
        await raw.incoming.put(None)
        await run
        return [json.loads(frame) for frame in raw.sent]

    remembered = []
    monkeypatch.setattr(link, "_routed_multiplexers", set())
    monkeypatch.setattr(link, "_cloud_proactive_acked", collections.OrderedDict())
    monkeypatch.setattr(agent, "said", lambda *args: remembered.append(args))
    link._activity.clear()

    frames = asyncio.run(exercise())

    assert remembered == [("said", "The build finished.", "device")]
    # The card is written when the message is sent, not when it is played, so a
    # reply can be read by someone who missed hearing it. A playback receipt -
    # including a repeated one - must not add a second copy.
    assert list(link._activity) == []
    assert (
        frames[1]
        == frames[3]
        == {
            "v": 1,
            "type": "link.proactive.acknowledged",
            "id": "7d53976f-5f55-58bc-b491-c09679b0fb79",
        }
    )


def test_link_presence_sends_only_the_normalized_unavailable_code(monkeypatch):
    class Multiplexer:
        def __init__(self):
            self.sent = []

        async def send_control(self, frame):
            self.sent.append(frame)

    mux = Multiplexer()
    monkeypatch.setattr(agent, "busy", lambda: False)
    monkeypatch.setattr(
        agent,
        "status",
        lambda: {
            "state": "unavailable",
            "failure": "authentication_expired",
            "repair": "Sign in to the provider.",
            "diagnostic": "token=must-not-cross-link",
        },
    )
    monkeypatch.setattr(link, "_routed_multiplexers", {mux})

    asyncio.run(link._publish_idle_link_presence(agent))

    assert json.loads(mux.sent[0]) == {
        "v": 1,
        "type": "link.presence",
        "agent": "unavailable",
        "failure": "authentication_expired",
        "steering": True,
    }
    assert "must-not-cross" not in mux.sent[0]


def test_cloud_text_turn_calls_the_agent_without_local_speech(monkeypatch):
    socket = _CloudSocket()
    asked = []
    monkeypatch.setattr(agent, "busy", lambda: False)
    monkeypatch.setattr(agent, "said", lambda *args: None)
    monkeypatch.setattr(
        agent,
        "ask_result",
        lambda heard, to: asked.append((heard, to)) or agent.AgentTurnOutcome("Done."),
    )
    monkeypatch.setattr(agent, "answer_required", lambda: True)
    monkeypatch.setattr(link, "_turn_gate", None)

    asyncio.run(link.run_cloud_turn("a" * 32, "Build the project.", socket, "device"))

    assert asked == [("Build the project.", "device")]
    assert not hasattr(link, "speak_to_pcm")
    assert json.loads(socket.sent[0]) == {
        "v": 1,
        "type": "link.turn.result",
        "id": "a" * 32,
        "text": "Done.",
        "needs_answer": True,
    }


def test_authenticated_local_turn_shares_device_scope_and_records_answer(monkeypatch):
    socket = _LocalSocket(json.dumps({"v": 1, "text": "Set up my briefing"}))
    asked = []
    said = []
    monkeypatch.setattr(link, "AUTH", "token")
    monkeypatch.setattr(link, "_turn_gate", None)
    monkeypatch.setattr(agent, "busy", lambda: False)
    monkeypatch.setattr(agent, "said", lambda *args: said.append(args))
    monkeypatch.setattr(agent, "status", lambda: {"state": "ready"})
    monkeypatch.setattr(
        agent,
        "ask_result",
        lambda heard, to, origin: (
            asked.append((heard, to, origin)) or agent.AgentTurnOutcome("Done.")
        ),
    )
    link._activity.clear()

    asyncio.run(link._local_agent_socket(socket))

    assert asked == [("Set up my briefing", "device", "mac")]
    assert said == [
        ("heard", "Set up my briefing", "device"),
        ("said", "Done.", "device"),
    ]
    assert json.loads(socket.sent[0]) == {"v": 1, "ok": True, "text": "Done."}
    assert [(event["kind"], event["text"]) for event in link._activity] == [
        ("heard", "Set up my briefing"),
        ("spoke", "Done."),
    ]


def test_cloud_result_enters_local_context_only_after_exact_playback_ack(monkeypatch):
    turn_id = "e" * 32
    socket = _CloudSocket()
    said = []
    monkeypatch.setattr(agent, "busy", lambda: False)
    monkeypatch.setattr(agent, "available", lambda: True)
    monkeypatch.setattr(
        agent,
        "ask_result",
        lambda *_args: agent.AgentTurnOutcome("Which folder?"),
    )
    monkeypatch.setattr(agent, "answer_required", lambda: True)
    monkeypatch.setattr(agent, "said", lambda *args: said.append(args))
    monkeypatch.setattr(link, "_turn_gate", None)
    link._activity.clear()
    link._cloud_turn_results.clear()
    link._cloud_turn_acked.clear()

    asyncio.run(link.run_cloud_turn(turn_id, "Build it.", socket, "device"))

    assert said == [("heard", "Build it.", "device")]
    assert turn_id in link._cloud_turn_results

    class AckSocket(_CloudSocket):
        def __aiter__(self):
            return self._frames()

        async def _frames(self):
            yield json.dumps({"v": 1, "type": "link.turn.ack", "id": turn_id})

    ack_socket = AckSocket()
    asyncio.run(link.handle(ack_socket, outbound=True))

    assert said == [
        ("heard", "Build it.", "device"),
        ("said", "Which folder?", "device"),
    ]
    assert [(event["kind"], event["text"]) for event in link._activity] == [
        ("heard", "Build it."),
        ("spoke", "Which folder?"),
    ]
    assert turn_id not in link._cloud_turn_results
    assert json.loads(ack_socket.sent[0]) == {
        "v": 1,
        "type": "link.turn.acknowledged",
        "id": turn_id,
    }


def test_provider_interaction_ttl_and_exact_ack_cross_the_cloud(monkeypatch):
    turn_id = "d" * 32
    interaction_id = "c" * 32
    socket = _CloudSocket()
    acknowledged = []
    monkeypatch.setattr(agent, "busy", lambda: False)
    monkeypatch.setattr(agent, "said", lambda *_args: None)
    monkeypatch.setattr(
        agent,
        "ask_result",
        lambda *_args: agent.AgentTurnOutcome(
            "Codex wants to edit the file. Say yes or no.",
            provider_interaction_ttl_seconds=540,
            interaction_id=interaction_id,
        ),
    )
    monkeypatch.setattr(agent, "answer_required", lambda: True)
    monkeypatch.setattr(
        agent,
        "acknowledge_interaction",
        lambda value: acknowledged.append(value) or True,
    )
    monkeypatch.setattr(link, "_turn_gate", None)
    link._cloud_turn_results.clear()
    link._cloud_turn_interactions.clear()
    link._cloud_turn_acked.clear()

    asyncio.run(link.run_cloud_turn(turn_id, "Change it.", socket, "device"))

    assert json.loads(socket.sent[0])["provider_interaction_ttl_seconds"] == 540
    assert link._cloud_turn_interactions[turn_id] == interaction_id

    class AckSocket(_CloudSocket):
        def __aiter__(self):
            return self._frames()

        async def _frames(self):
            yield json.dumps({"v": 1, "type": "link.turn.ack", "id": turn_id})

    asyncio.run(link.handle(AckSocket(), outbound=True))

    assert acknowledged == [interaction_id]
    assert turn_id not in link._cloud_turn_interactions


def test_mac_cannot_resolve_device_interaction_before_playback(monkeypatch):
    interaction_id = "b" * 32
    socket = _LocalSocket(
        json.dumps({"v": 1, "id": interaction_id, "decision": "allow"}),
        path="/interaction?auth=token",
    )
    pending = type(
        "Pending",
        (),
        {
            "id": interaction_id,
            "origin": "device",
            "acknowledged": False,
        },
    )()
    resolved = []
    monkeypatch.setattr(link, "AUTH", "token")
    monkeypatch.setattr(agent, "pending_interaction", lambda: pending)
    monkeypatch.setattr(
        agent, "resolve_interaction", lambda *args, **kwargs: resolved.append(args)
    )

    asyncio.run(link._local_agent_socket(socket))

    assert resolved == []
    assert json.loads(socket.sent[0]) == {
        "v": 1,
        "ok": False,
        "error": "awaiting_playback",
    }


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_mac_decision_delivers_the_continuation_or_repair_to_device(
    monkeypatch, decision
):
    interaction_id = "c" * 32
    socket = _LocalSocket(
        json.dumps({"v": 1, "id": interaction_id, "decision": decision}),
        path="/interaction?auth=token",
    )
    pending = type(
        "Pending",
        (),
        {"id": interaction_id, "origin": "device", "acknowledged": decision == "allow"},
    )()
    outcome = (
        agent.AgentTurnOutcome("Declined.")
        if decision == "deny"
        else (
            agent.AgentTurnOutcome(
                agent.turn_blockers.PERMISSION_REPLY,
                agent.AgentFailure(
                    "permission_blocked", "Mac access", "private diagnostic"
                ),
            )
        )
    )
    resolved, sent = [], []

    def resolve(*args, **kwargs):
        resolved.append((args, kwargs))
        return outcome

    async def deliver(request):
        sent.append(request)
        return proactive.LocalSendReceipt.queued("fixture-message")

    async def presence(_frame):
        pass

    monkeypatch.setattr(link, "AUTH", "token")
    monkeypatch.setattr(link, "_turn_gate", None)
    monkeypatch.setattr(
        link,
        "_routed_multiplexers",
        [
            SimpleNamespace(
                primary_pair="pair-a", send_proactive=deliver, send_control=presence
            )
        ],
    )
    # Background senders spent the whole budget; the approved reply still goes.
    spent = proactive.SendRateLimit(clock=lambda: 0.0)
    while spent.admit():
        pass
    monkeypatch.setattr(link, "_proactive_rate_limit", spent)
    monkeypatch.setattr(link, "_note", lambda *_: None)
    monkeypatch.setattr(agent, "pending_interaction", lambda: pending)
    monkeypatch.setattr(agent, "busy", lambda: False)
    monkeypatch.setattr(agent, "status", lambda: {"state": "ready"})
    monkeypatch.setattr(agent, "resolve_interaction", resolve)

    asyncio.run(link._local_agent_socket(socket))

    assert resolved == [
        (
            (interaction_id, decision),
            {
                "scope": link.DEVICE,
                "origin": "mac",
                "require_acknowledged": decision == "allow",
            },
        )
    ]
    assert [request.text for request in sent] == [outcome.text]
    assert json.loads(socket.sent[0]) == {
        "v": 1,
        "ok": True,
        "text": outcome.text,
        "queued_for_device": True,
    }
    assert "private diagnostic" not in socket.sent[0]


def test_cloud_playback_ack_is_idempotent_and_confirmed_to_cloud(monkeypatch):
    turn_id = "f" * 32
    said = []
    monkeypatch.setattr(agent, "said", lambda *args: said.append(args))
    link._cloud_turn_results.clear()
    link._cloud_turn_acked.clear()
    link._cloud_turn_results[turn_id] = ("Done.", "device")

    class AckSocket(_CloudSocket):
        def __aiter__(self):
            return self._frames()

        async def _frames(self):
            frame = json.dumps({"v": 1, "type": "link.turn.ack", "id": turn_id})
            yield frame
            yield frame

    socket = AckSocket()
    asyncio.run(link.handle(socket, outbound=True))

    assert said == [("said", "Done.", "device")]
    assert [json.loads(frame) for frame in socket.sent] == [
        {"v": 1, "type": "link.turn.acknowledged", "id": turn_id},
        {"v": 1, "type": "link.turn.acknowledged", "id": turn_id},
    ]


def test_private_agent_cancel_stops_the_shared_agent_regardless_of_ingress(
    monkeypatch,
):
    busy_id = "1" * 32
    request_id = "2" * 32
    cancelled = []
    monkeypatch.setattr(agent, "cancel", lambda *args: cancelled.append(args) or True)
    monkeypatch.setattr(agent, "busy", lambda: True)
    monkeypatch.setattr(link, "_agent_busy_id", busy_id)

    class CancelSocket(_CloudSocket):
        def __aiter__(self):
            return self._frames()

        async def _frames(self):
            yield json.dumps(
                {
                    "v": 1,
                    "type": "link.agent.cancel",
                    "id": request_id,
                    "busy_id": busy_id,
                }
            )

    async def exercise():
        gate = link.turn_gate()
        await gate.acquire()
        try:
            socket = CancelSocket()
            await link.handle(socket, outbound=True)
            return socket
        finally:
            gate.release()

    link._turn_gate = None
    socket = asyncio.run(exercise())

    assert cancelled == [(None,)]
    assert json.loads(socket.sent[0]) == {
        "v": 1,
        "type": "link.agent.cancelled",
        "id": request_id,
        "busy_id": busy_id,
        "cancelled": True,
    }


def test_private_agent_cancel_waits_through_prestart_claim_race(monkeypatch):
    busy_id = "3" * 32
    request_id = "4" * 32
    cancelled = []
    busy_states = iter((False, True))
    monkeypatch.setattr(agent, "busy", lambda: next(busy_states, True))
    monkeypatch.setattr(agent, "cancel", lambda *args: cancelled.append(args) or True)
    monkeypatch.setattr(link, "_agent_busy_id", busy_id)

    class CancelSocket(_CloudSocket):
        def __aiter__(self):
            return self._frames()

        async def _frames(self):
            yield json.dumps(
                {
                    "v": 1,
                    "type": "link.agent.cancel",
                    "id": request_id,
                    "busy_id": busy_id,
                }
            )

    async def exercise():
        gate = link.turn_gate()
        await gate.acquire()
        try:
            socket = CancelSocket()
            await link.handle(socket, outbound=True)
            return socket
        finally:
            gate.release()

    link._turn_gate = None
    socket = asyncio.run(exercise())

    assert cancelled == [(None,)]
    assert json.loads(socket.sent[0])["cancelled"] is True


def test_stale_private_agent_cancel_cannot_touch_newer_busy_generation(monkeypatch):
    old_busy_id = "5" * 32
    request_id = "6" * 32
    monkeypatch.setattr(link, "_agent_busy_id", "7" * 32)
    monkeypatch.setattr(
        agent,
        "cancel",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("stale generation must not cancel the current Agent")
        ),
    )

    class CancelSocket(_CloudSocket):
        def __aiter__(self):
            return self._frames()

        async def _frames(self):
            yield json.dumps(
                {
                    "v": 1,
                    "type": "link.agent.cancel",
                    "id": request_id,
                    "busy_id": old_busy_id,
                }
            )

    socket = CancelSocket()
    asyncio.run(link.handle(socket, outbound=True))

    assert json.loads(socket.sent[0])["cancelled"] is False


def test_cloud_text_turn_returns_a_safe_error_without_private_detail(monkeypatch):
    socket = _CloudSocket()
    monkeypatch.setattr(agent, "busy", lambda: False)
    monkeypatch.setattr(agent, "said", lambda *args: None)
    monkeypatch.setattr(
        agent,
        "ask_result",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("secret provider output")),
    )
    monkeypatch.setattr(link, "_turn_gate", None)

    asyncio.run(link.run_cloud_turn("b" * 32, "Do it.", socket, "device"))

    payload = json.loads(socket.sent[0])
    assert payload == {"v": 1, "type": "link.turn.error", "id": "b" * 32}
    assert "secret" not in socket.sent[0]


@pytest.mark.parametrize(
    "reason",
    [
        "unavailable",
        "authentication_expired",
        "permission_blocked",
        "process_failed",
        "request_failed",
        "update_required",
        "response_too_long",
    ],
)
def test_cloud_text_turn_reports_only_the_normalized_failure(reason, monkeypatch):
    socket = _CloudSocket()
    failure = agent.AgentFailure(
        code=reason,
        repair="Open WalkieOne.",
        diagnostic="local provider detail",
    )
    monkeypatch.setattr(agent, "busy", lambda: False)
    monkeypatch.setattr(agent, "said", lambda *args: None)
    monkeypatch.setattr(
        agent,
        "ask_result",
        lambda *_args: agent.AgentTurnOutcome("local fallback speech", failure),
    )
    monkeypatch.setattr(link, "_turn_gate", None)

    asyncio.run(link.run_cloud_turn("9" * 32, "Do it.", socket, "device"))

    assert json.loads(socket.sent[0]) == {
        "v": 1,
        "type": "link.turn.error",
        "id": "9" * 32,
        "reason": reason,
    }
    assert "provider" not in socket.sent[0]


def test_cloud_text_turn_rejects_a_locked_local_gate_as_busy(monkeypatch):
    socket = _CloudSocket()
    asked = []
    monkeypatch.setattr(agent, "busy", lambda: False)
    monkeypatch.setattr(agent, "available", lambda: True)
    monkeypatch.setattr(agent, "said", lambda *args: None)
    monkeypatch.setattr(agent, "ask", lambda *args: asked.append(args) or "Done.")
    monkeypatch.setattr(link, "_turn_gate", None)

    async def exercise():
        gate = link.turn_gate()
        await gate.acquire()
        try:
            await link.run_cloud_turn("c" * 32, "Do it.", socket, "device")
        finally:
            gate.release()

    asyncio.run(exercise())

    assert asked == []
    assert json.loads(socket.sent[0]) == {
        "v": 1,
        "type": "link.turn.error",
        "id": "c" * 32,
        "reason": "busy",
    }


@pytest.mark.parametrize("length", [60_000, 60_001])
def test_cloud_accepts_long_transcripts_and_rejects_overflow(monkeypatch, length):
    text = "é" * length
    completed = []

    class ScriptSocket(_CloudSocket):
        def __aiter__(self):
            return self._frames()

        async def _frames(self):
            yield json.dumps(
                {
                    "v": 1,
                    "type": "link.turn.request",
                    "id": "e" * 32,
                    "text": text,
                    "language": "en-US",
                }
            )
            await asyncio.sleep(0)

    async def run_turn(_id, heard, *_args):
        completed.append(heard)

    monkeypatch.setattr(link, "run_cloud_turn", run_turn)
    monkeypatch.setattr(link, "_cloud_turn_tasks", {})
    monkeypatch.setattr(link, "_cloud_turn_running", set())
    asyncio.run(link.handle(ScriptSocket(), outbound=True))
    assert completed == ([text] if length == 60_000 else [])


def test_cloud_cancel_before_agent_claim_prevents_the_turn_from_running(monkeypatch):
    turn_id = "d" * 32
    request = json.dumps(
        {
            "v": 1,
            "type": "link.turn.request",
            "id": turn_id,
            "text": "Do it.",
            "language": "en-US",
        }
    )
    cancel = json.dumps({"v": 1, "type": "link.turn.cancel", "id": turn_id})
    completed = []

    class ScriptSocket(_CloudSocket):
        def __aiter__(self):
            return self._frames()

        async def _frames(self):
            yield request
            yield cancel
            await asyncio.sleep(0.01)

    async def delayed_turn(*_args):
        await asyncio.sleep(1)
        completed.append(True)

    monkeypatch.setattr(link, "run_cloud_turn", delayed_turn)
    monkeypatch.setattr(
        agent,
        "cancel",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("pre-start cancel must not touch another Agent turn")
        ),
    )
    link._cloud_turn_tasks.clear()
    link._cloud_turn_running.clear()

    asyncio.run(link.handle(ScriptSocket(), outbound=True))

    assert completed == []
    assert not link._cloud_turn_tasks
    assert not link._cloud_turn_running


def test_local_http_surface_is_authenticated_status_only(monkeypatch):
    monkeypatch.setattr(link, "authorized", lambda _token: True)

    status = asyncio.run(link.http_route("/status", "token"))
    retired = [
        asyncio.run(link.http_route(path, "token"))[0]
        for path in (
            "/",
            "/walkie",
            "/ask?text=do+something",
            "/say?text=speak+locally",
        )
    ]

    assert status[0] == 200
    assert retired == [404, 404, 404, 404]


def test_a_reply_is_readable_before_it_is_ever_played(monkeypatch):
    """The whole point of the card is catching a message you missed, so it
    cannot wait for the playback receipt that a missed message never sends."""

    class _Multiplexer:
        primary_pair = "pair-a"

        def __init__(self):
            self.sent = []

        async def send_proactive(self, request):
            self.sent.append(request)
            return proactive.LocalSendReceipt.queued("7d53976f-5f55-58bc-b491-c096")

    multiplexer = _Multiplexer()
    monkeypatch.setattr(link, "_routed_multiplexers", {multiplexer})
    monkeypatch.setattr(agent, "said", lambda *_args: None)
    link._activity.clear()

    receipt = asyncio.run(
        link.send_proactive(
            proactive.LocalSendRequest(
                text="The build finished.", idempotency_key="b-1"
            )
        )
    )

    assert receipt.status == "queued"
    # Nothing has been played, and nothing acknowledged, yet it is readable.
    assert [(event["kind"], event["text"]) for event in link._activity] == [
        ("spoke", "The build finished."),
    ]


def test_a_reply_that_was_never_sent_is_not_claimed_as_spoken(monkeypatch):
    monkeypatch.setattr(link, "_routed_multiplexers", set())
    link._activity.clear()

    receipt = asyncio.run(
        link.send_proactive(
            proactive.LocalSendRequest(text="Nobody heard this.", idempotency_key="n-1")
        )
    )

    assert receipt.status == "rejected"
    assert list(link._activity) == []


def test_a_send_from_the_speaking_turn_is_refused_not_duplicated(monkeypatch):
    """The turn's own answer is already going to the Device, so a send carrying
    that same turn is the same message twice. The prompt asks the Agent not to;
    this makes it so regardless."""

    class _Multiplexer:
        primary_pair = "pair-a"

        def __init__(self):
            self.sent = []

        async def send_proactive(self, request):
            self.sent.append(request)
            return proactive.LocalSendReceipt.queued("11111111-0000-4000-8000-0000")

    multiplexer = _Multiplexer()
    monkeypatch.setattr(link, "_routed_multiplexers", {multiplexer})
    monkeypatch.setattr(agent, "active_turn_token", lambda: "turn-abc")
    monkeypatch.setattr(agent, "said", lambda *_args: None)
    link._activity.clear()

    refused = asyncio.run(
        link.send_proactive(
            proactive.LocalSendRequest(
                text="The weather is hot.", idempotency_key="w-1", turn="turn-abc"
            )
        )
    )

    assert refused.status == "rejected"
    assert refused.reason == "answer_instead"
    assert multiplexer.sent == []
    # Nothing was said, so nothing is claimed to have been said.
    assert list(link._activity) == []


def test_a_send_left_for_later_still_goes_out_after_its_turn_ends(monkeypatch):
    """Anything the turn scheduled carries the same token but fires when that
    turn is no longer speaking, so scheduling must be unaffected."""

    class _Multiplexer:
        primary_pair = "pair-a"

        def __init__(self):
            self.sent = []

        async def send_proactive(self, request):
            self.sent.append(request)
            return proactive.LocalSendReceipt.queued("22222222-0000-4000-8000-0000")

    multiplexer = _Multiplexer()
    monkeypatch.setattr(link, "_routed_multiplexers", {multiplexer})
    monkeypatch.setattr(agent, "said", lambda *_args: None)

    # A different turn is speaking now.
    monkeypatch.setattr(agent, "active_turn_token", lambda: "turn-later")
    link._activity.clear()
    during = asyncio.run(
        link.send_proactive(
            proactive.LocalSendRequest(
                text="Time to stretch.", idempotency_key="s-1", turn="turn-abc"
            )
        )
    )

    # And between turns, nothing is speaking at all.
    monkeypatch.setattr(agent, "active_turn_token", lambda: None)
    between = asyncio.run(
        link.send_proactive(
            proactive.LocalSendRequest(
                text="Time to stretch.", idempotency_key="s-2", turn="turn-abc"
            )
        )
    )

    assert during.status == "queued"
    assert between.status == "queued"
    assert [item.text for item in multiplexer.sent] == [
        "Time to stretch.",
        "Time to stretch.",
    ]


def test_completed_result_retries_on_reconnected_link_without_rerunning_agent(
    monkeypatch,
):
    class BrokenSocket:
        async def send(self, _):
            raise ConnectionError("radio dropped")

    asked = []
    monkeypatch.setattr(agent, "busy", lambda: False)
    monkeypatch.setattr(agent, "said", lambda *_: None)
    monkeypatch.setattr(
        agent,
        "ask_result",
        lambda *args: asked.append(args) or agent.AgentTurnOutcome("Recovered result."),
    )
    monkeypatch.setattr(agent, "answer_required", lambda: False)
    monkeypatch.setattr(link, "_turn_gate", None)
    monkeypatch.setattr(link, "_routed_multiplexers", set())
    monkeypatch.setattr(link, "_cloud_turn_results", collections.OrderedDict())
    monkeypatch.setattr(link, "_cloud_turn_interactions", {})

    async def exercise():
        old_mux = link._LinkMultiplexer(BrokenSocket())
        origin = link._RoutedDeviceSocket(old_mux, "original-pair")
        raw = _CloudSocket()
        new_mux = link._LinkMultiplexer(raw)
        new_mux.primary_pair = "another-pair"
        task = asyncio.create_task(
            link.run_cloud_turn("e" * 32, "Build.", origin, "device")
        )
        await asyncio.sleep(0.02)
        link._routed_multiplexers.add(new_mux)
        await asyncio.wait_for(task, 1)
        for socket in new_mux._devices.values():
            socket.finish()
        await asyncio.gather(*new_mux._handlers.values())
        messages = [json.loads(frame) for frame in raw.sent]
        result_index = next(
            i for i, frame in enumerate(messages) if frame["type"] == "link.turn.result"
        )
        assert messages[result_index - 1] == {
            "v": 1,
            "type": "link.output",
            "pair": "original-pair",
        }
        assert messages[result_index]["id"] == "e" * 32
        assert messages[result_index]["text"] == "Recovered result."
        assert link._cloud_turn_results["e" * 32] == ("Recovered result.", "device")

    asyncio.run(exercise())
    assert asked == [("Build.", "device")]
