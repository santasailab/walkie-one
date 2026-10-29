"""MCP app approvals through the real Link host and a stdio stand-in for
headless Claude's SDK control channel."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import agent
from provider_interactions import InteractionBroker

CLAUDE_FIXTURE = Path(__file__).parent / "fixtures" / "fake_claude.py"


@pytest.fixture
def host(monkeypatch, tmp_path):
    broker = InteractionBroker()
    monkeypatch.setattr(agent, "_interactions", broker)
    for name in (
        "_active",
        "_last_failure",
        "_retained_turn_lock",
        "_retained_turn_interaction",
    ):
        monkeypatch.setattr(agent, name, None)
    monkeypatch.setattr(agent, "_active_cancelled", False)
    monkeypatch.setattr(agent, "BACKEND", "claude")
    monkeypatch.setattr(agent, "WORKDIR", str(tmp_path))
    monkeypatch.setattr(agent, "TURN_DIR", str(tmp_path / "turns"))
    monkeypatch.setattr(agent, "TURN_LOCK", str(tmp_path / "agent.lock"))
    monkeypatch.setattr(agent, "setup", lambda *_: None)
    monkeypatch.setattr(agent, "_session", lambda *_: None)
    monkeypatch.setattr(agent, "_remember", lambda *_: None)
    monkeypatch.setattr(agent, "_with_clock", lambda question: question)
    monkeypatch.setattr(agent.turn_blockers, "record", lambda *_: None)
    monkeypatch.setattr(agent, "executable", lambda _: str(CLAUDE_FIXTURE))
    record = tmp_path / "control-response.json"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    override = tmp_path / "elicitation.json"
    monkeypatch.setenv("FAKE_CLAUDE_ELICITATION", str(override))
    endpoint = agent._agent_endpoint("claude", "device")
    monkeypatch.setattr(agent, "_agent_endpoint", lambda *_: endpoint)

    def start(fields=None, origin="device"):
        override.write_text(json.dumps(fields or {}))
        return agent.ask_result("Check my inbox.", origin=origin)

    yield start, record, endpoint
    broker.cancel()
    endpoint.cancel()
    agent._release_retained_process_turn()


@pytest.mark.parametrize("mode", ["ask", "auto", "full"])
@pytest.mark.parametrize("decision", ["allow", "deny"])
@pytest.mark.parametrize("origin", ["device", "mac"])
def test_app_approval_reaches_user_and_answers_the_control_request(
    host, monkeypatch, mode, decision, origin
):
    monkeypatch.setenv("WALKIEONE_APPROVAL_MODE", mode)
    start, record, endpoint = host
    waiting = start(origin=origin)
    assert waiting.interaction_id
    assert waiting.provider_interaction_ttl_seconds == 540
    assert "Mail" in waiting.text and "once" in waiting.text
    pending = agent.pending_interaction()
    assert pending.kind == "permission"
    assert "Mail" in pending.title
    assert "computer-use" in pending.detail
    assert agent.status()["state"] == "waiting"
    assert not record.exists()
    assert endpoint._last_runner.process.poll() is None, "child stays alive"

    if origin == "device":
        assert agent.ask_result("yes").text == agent.BUSY_REPLY
        assert agent.acknowledge_interaction(waiting.interaction_id)
        completed = agent.ask_result("yes" if decision == "allow" else "no")
    else:
        completed = agent.resolve_interaction(
            waiting.interaction_id, decision, origin="mac"
        )

    assert completed.failure is None
    assert completed.text == (
        "Finished with Claude."
        if decision == "allow"
        else "Claude did not make that change."
    )
    assert json.loads(record.read_text()) == {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": "elicit-1",
            "response": (
                {"action": "accept", "content": {}}
                if decision == "allow"
                else {"action": "decline"}
            ),
        },
    }
    assert agent.pending_interaction() is None
    assert not agent.busy()


@pytest.mark.parametrize("end", ["stop", "expire"])
def test_stopped_or_expired_app_approval_cannot_be_granted_later(host, end):
    start, record, endpoint = host
    waiting = start()
    assert waiting.interaction_id
    process = endpoint._last_runner.process
    if end == "stop":
        assert agent.cancel("device")
    else:
        agent._interactions._expire(waiting.interaction_id)
    assert process.poll() is not None
    assert (
        agent.resolve_interaction(waiting.interaction_id, "allow").text
        == agent.EXPIRED_APPROVAL_REPLY
    )
    assert not record.exists()
    assert not agent.busy()


@pytest.mark.parametrize(
    "fields",
    [
        {"mode": "url", "url": "https://example.invalid/login", "elicitation_id": "x"},
        {
            "requested_schema": {
                "type": "object",
                "properties": {"password": {"type": "string"}},
            }
        },
        {
            "requested_schema": {
                "type": "object",
                "properties": {},
                "required": ["missing"],
            }
        },
        {"requested_schema": None},
        {"message": ""},
    ],
)
def test_non_confirmation_elicitation_is_declined_without_fake_user_input(host, fields):
    start, record, _ = host
    completed = start(fields)
    assert completed.interaction_id is None
    assert agent.pending_interaction() is None
    answer = json.loads(record.read_text())["response"]
    assert answer["subtype"] == "success"
    assert answer["response"] == {"action": "decline"}
    assert completed.text == "Claude did not make that change."


def test_unknown_control_request_is_refused_not_left_hanging(host, monkeypatch):
    start, record, _ = host
    monkeypatch.setenv("FAKE_CLAUDE_CONTROL_SUBTYPE", "can_use_tool")
    completed = start()
    assert completed.interaction_id is None
    answer = json.loads(record.read_text())["response"]
    assert answer["subtype"] == "error"
    assert answer["request_id"] == "elicit-1"
    assert "can_use_tool" in answer["error"]
    assert completed.text == "Claude did not make that change."


def test_approval_text_is_redacted_before_voice_or_mac_display(host):
    start, _, _ = host
    waiting = start(
        {"message": "Allow Mail? Authorization: Bearer fixture-secret-value"}
    )
    assert waiting.interaction_id
    pending = agent.pending_interaction()
    assert "fixture-secret-value" not in waiting.text + pending.title + pending.detail
    assert "[redacted]" in pending.detail
