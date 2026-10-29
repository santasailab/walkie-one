"""Follow-ups redirect the actual provider operation without replaying work."""

import asyncio
import collections
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import agent
import link
from agent_endpoint import TurnRequest

FIXTURE = Path(__file__).parent / "fixtures" / "fake_steering.py"


@pytest.fixture
def runtime_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "WORKDIR", str(tmp_path))
    monkeypatch.setattr(agent, "CONSENT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(agent, "STEERING_ACK_SECONDS", 1)
    monkeypatch.setattr(
        agent, "_codex", lambda: [sys.executable, str(FIXTURE), "codex"]
    )
    monkeypatch.setattr(
        agent, "_claude", lambda _: [sys.executable, str(FIXTURE), "claude"]
    )
    monkeypatch.setattr(agent, "executable", lambda _: sys.executable)
    monkeypatch.setattr(agent, "child_env", lambda *_: dict(os.environ))
    record = tmp_path / "protocol.jsonl"
    monkeypatch.setenv("STEERING_FIXTURE_RECORD", str(record))
    return record


@pytest.mark.parametrize(
    "provider,mode",
    [
        ("codex", "normal"),
        ("codex", "terminal_first"),
        ("claude", "normal"),
        ("claude", "result_only"),
    ],
)
def test_followup_uses_same_operation_and_returns_only_redirected_reply(
    runtime_setup, monkeypatch, provider, mode
):
    monkeypatch.setenv("STEERING_FIXTURE_MODE", mode)
    endpoint = agent._agent_endpoint(provider, "device")
    with ThreadPoolExecutor(2) as executor:
        result = executor.submit(endpoint.run, TurnRequest("Original task"))
        try:
            assert endpoint.steer("Stop and do this instead.") == "accepted"
            assert result.result(3).text == "Stop and do this instead."
        finally:
            endpoint.cancel()
    messages = [json.loads(line) for line in runtime_setup.read_text().splitlines()]
    if provider == "codex":
        assert sum(row.get("method") == "turn/start" for row in messages) == 1
        assert sum(row.get("method") == "turn/steer" for row in messages) == 1
    else:
        assert len({row["uuid"] for row in messages if row["type"] == "user"}) == 2
    assert endpoint.steering_sent
    assert endpoint.steer("Too late") == "inactive"


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_multiple_followups_keep_order(runtime_setup, monkeypatch, provider):
    monkeypatch.setenv("STEERING_FIXTURE_INPUTS", "2")
    endpoint = agent._agent_endpoint(provider, "device")
    with ThreadPoolExecutor(2) as executor:
        result = executor.submit(endpoint.run, TurnRequest("Original task"))
        try:
            assert endpoint.steer("Use the first file") == "accepted"
            assert not result.done()
            assert endpoint.steer("Actually use the second file") == "accepted"
            assert result.result(3).text == "Actually use the second file"
        finally:
            endpoint.cancel()


def test_sent_input_without_receipt_is_unknown_and_never_restarted(
    runtime_setup, monkeypatch
):
    monkeypatch.setenv("STEERING_FIXTURE_MODE", "no_ack")
    monkeypatch.setattr(agent, "STEERING_ACK_SECONDS", 0.1)
    runtime = agent._CodexRuntimeAdapter("device")
    with ThreadPoolExecutor(2) as executor:
        result = executor.submit(runtime.run, "Original", None, "device")
        try:
            assert runtime.steer("Follow-up") == "unknown"
            with pytest.raises(agent._ProtocolError, match="acknowledgment"):
                result.result(3)
        finally:
            runtime._finish()
    assert runtime.steering_sent


def test_unsent_followup_can_be_withdrawn_without_later_delivery(monkeypatch):
    monkeypatch.setattr(agent, "STEERING_ACK_SECONDS", 0.01)
    runtime = agent._CodexRuntimeAdapter("device")
    assert runtime.steer("No provider yet") == "rejected"
    assert not runtime.steering_sent
    assert runtime._steering == []


def test_crash_after_steering_does_not_replay_original(runtime_setup, monkeypatch):
    monkeypatch.setenv("STEERING_FIXTURE_MODE", "crash")
    monkeypatch.setattr(agent, "BACKEND", "codex")
    monkeypatch.setattr(agent, "_active", None)
    monkeypatch.setattr(agent, "_process_turn_lock", lambda: None)
    monkeypatch.setattr(agent, "_release_process_turn", lambda _: None)
    monkeypatch.setattr(agent, "setup", lambda *_: None)
    monkeypatch.setattr(agent, "_session", lambda _: None)
    monkeypatch.setattr(agent, "recent", lambda _: "")
    monkeypatch.setattr(agent, "_with_clock", lambda text: text)
    endpoint = agent._agent_endpoint("codex", "device")
    monkeypatch.setattr(agent, "_agent_endpoint", lambda *_: endpoint)
    with ThreadPoolExecutor(2) as executor:
        result = executor.submit(agent.ask_result, "Original")
        assert endpoint.steer("New instructions") == "unknown"
        assert result.result(3).failure.code == "request_failed"
    messages = [json.loads(line) for line in runtime_setup.read_text().splitlines()]
    assert sum(row.get("method") == "turn/start" for row in messages) == 1


def test_paused_approval_remains_cancellable(runtime_setup):
    runtime = agent._CodexRuntimeAdapter("device")
    runtime._start([sys.executable, "-c", "import time; time.sleep(60)"], "device")
    try:
        runtime._pause_steering()
        assert runtime.steer("Skip the approval") == "inactive"
        assert runtime.cancel()
        assert runtime.process.poll() is not None
    finally:
        runtime._finish()


def test_link_steering_is_idempotent_and_pair_bound(monkeypatch):
    class Socket:
        route_pair = "pair-a"

        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    received = []
    monkeypatch.setattr(agent, "busy", lambda: True)
    monkeypatch.setattr(
        agent, "steer", lambda text, scope: received.append(text) or "accepted"
    )
    monkeypatch.setattr(agent, "said", lambda *_: None)
    monkeypatch.setattr(link, "_note", lambda *_: None)
    monkeypatch.setattr(link, "_cloud_steer_receipts", collections.OrderedDict())
    monkeypatch.setattr(link, "_cloud_turn_pairs", {"a" * 32: "pair-a"})
    monkeypatch.setattr(link, "_agent_busy_id", "a" * 32)

    async def exercise():
        socket = Socket()
        task = asyncio.create_task(asyncio.Event().wait())
        monkeypatch.setattr(link, "_cloud_turn_tasks", {"a" * 32: task})
        try:
            await asyncio.gather(
                *(
                    link.run_cloud_steer("b" * 32, "a" * 32, "Change it", socket)
                    for _ in range(2)
                )
            )
            await link.run_cloud_steer("b" * 32, "a" * 32, "Conflicting input", socket)
            socket.route_pair = "pair-b"
            await link.run_cloud_steer("c" * 32, "a" * 32, "Wrong Device", socket)
            assert [row["status"] for row in socket.sent] == [
                "accepted",
                "accepted",
                "rejected",
                "rejected",
            ]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert received == ["Change it"]
