"""Computer Use app approvals through the real Link host and a stdio provider."""

import asyncio
import collections
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import agent
from provider_interactions import InteractionBroker

CODEX_FIXTURE = Path(__file__).parent / "fixtures" / "fake_codex.py"


def app_request(app="Mail"):
    # Same shape as Codex 0.155.1's captured Mail/Chrome requests. Identifiers
    # and metadata are synthetic; no customer transcripts enter the fixture.
    return {
        "id": 0,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "codex-thread",
            "turnId": "codex-turn",
            "serverName": "cua_repl",
            "mode": "form",
            "message": f'Allow Computer Use to use "{app}"?',
            "requestedSchema": {"type": "object", "properties": {}},
            "_meta": {
                "codex_approval_kind": "mcp_tool_call",
                "connector_id": "computer-use",
                "connector_name": "Computer Use",
                "persist": ["session", "always"],
                "tool_name": "get_app_state",
                "tool_params": {
                    "app": "com.apple.mail" if app == "Mail" else "com.google.Chrome"
                },
                "riskLevel": "high" if app == "Google Chrome" else "low",
                "subtitle": "Review the app access before allowing it.",
            },
        },
    }


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
    monkeypatch.setattr(agent, "BACKEND", "codex")
    monkeypatch.setattr(agent, "WORKDIR", str(tmp_path))
    monkeypatch.setattr(agent, "TURN_DIR", str(tmp_path / "turns"))
    monkeypatch.setattr(agent, "TURN_LOCK", str(tmp_path / "agent.lock"))
    monkeypatch.setattr(agent, "setup", lambda *_: None)
    monkeypatch.setattr(agent, "_session", lambda *_: None)
    monkeypatch.setattr(agent, "_remember", lambda *_: None)
    monkeypatch.setattr(agent, "_with_clock", lambda question: question)
    monkeypatch.setattr(agent.turn_blockers, "record", lambda *_: None)
    monkeypatch.setattr(agent, "executable", lambda _: str(CODEX_FIXTURE))
    record = tmp_path / "response.json"
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(record))
    request_path = tmp_path / "request.json"
    monkeypatch.setenv("FAKE_CODEX_ELICITATION", str(request_path))
    endpoint = agent._agent_endpoint("codex", "device")
    monkeypatch.setattr(agent, "_agent_endpoint", lambda *_: endpoint)

    def start(request=None, origin="device"):
        request_path.write_text(json.dumps(request or app_request()))
        return agent.ask_result("Check my inbox.", origin=origin)

    yield start, record, endpoint
    broker.cancel()
    endpoint.cancel()
    agent._release_retained_process_turn()


@pytest.mark.parametrize("app", ["Mail", "Google Chrome"])
@pytest.mark.parametrize("decision", ["allow", "deny"])
@pytest.mark.parametrize("origin", ["device", "mac"])
def test_app_approval_reaches_user_and_returns_exact_mcp_response(
    host, app, decision, origin
):
    start, record, _ = host
    waiting = start(app_request(app), origin)
    assert waiting.interaction_id
    assert waiting.provider_interaction_ttl_seconds == 540
    assert app in waiting.text
    assert "once" in waiting.text
    pending = agent.pending_interaction()
    assert pending.kind == "permission"
    assert app in pending.title
    assert "Review the app access" in pending.detail
    assert agent.status()["state"] == "waiting"
    assert not record.exists()

    if origin == "device":
        assert agent.ask_result("yes").text == agent.BUSY_REPLY
        assert not agent.acknowledge_interaction("unrelated-playback")
        assert not record.exists()
        assert agent.acknowledge_interaction(waiting.interaction_id)
        completed = agent.ask_result("yes" if decision == "allow" else "no")
    else:
        completed = agent.resolve_interaction(
            waiting.interaction_id, decision, origin="mac"
        )

    assert completed.failure is None
    assert completed.text == (
        "Finished with Codex."
        if decision == "allow"
        else "Codex did not make that change."
    )
    assert json.loads(record.read_text()) == {
        "id": 0,
        "result": {
            "action": "accept" if decision == "allow" else "decline",
            "content": {} if decision == "allow" else None,
            "_meta": None,
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
    assert not agent.acknowledge_interaction(waiting.interaction_id)
    assert (
        agent.resolve_interaction(waiting.interaction_id, "allow").text
        == agent.EXPIRED_APPROVAL_REPLY
    )
    assert not record.exists()
    assert not agent.busy()


def test_app_approval_does_not_mask_subsequent_mac_permission_denial(host, monkeypatch):
    start, record, _ = host
    monkeypatch.setenv("FAKE_CODEX_MAC_DENIAL", "1")
    waiting = start(origin="mac")
    assert waiting.interaction_id
    completed = agent.resolve_interaction(waiting.interaction_id, "allow", origin="mac")
    assert json.loads(record.read_text())["result"]["action"] == "accept"
    assert completed.failure.code == "permission_blocked"
    assert "Accessibility" in completed.failure.repair
    assert agent.pending_interaction() is None
    assert not agent.busy()


@pytest.mark.parametrize(
    "replacement",
    [
        {
            "mode": "url",
            "url": "https://example.invalid/login",
            "elicitationId": "login",
        },
        {"mode": "openai/form"},
        {
            "requestedSchema": {
                "type": "object",
                "properties": {"password": {"type": "string"}},
            }
        },
        {
            "requestedSchema": {
                "type": "object",
                "properties": {},
                "required": ["missing"],
            }
        },
        {"requestedSchema": None},
        {"_meta": None},
        {"_meta": {"codex_approval_kind": "other"}},
        {"message": ""},
        {"threadId": "another-thread"},
        {"turnId": "a-stale-turn"},
    ],
)
def test_non_confirmation_elicitation_is_declined_without_fake_user_input(
    host, replacement
):
    start, record, _ = host
    request = copy.deepcopy(app_request())
    request["params"].update(replacement)
    completed = start(request)
    assert completed.interaction_id is None
    assert agent.pending_interaction() is None
    assert json.loads(record.read_text())["result"] == {
        "action": "decline",
        "content": None,
        "_meta": None,
    }


def test_approval_text_is_redacted_before_voice_or_mac_display(host):
    start, _, _ = host
    request = app_request()
    request["params"]["message"] += " Authorization: Bearer fixture-secret-value"
    request["params"]["_meta"]["subtitle"] += " token=fixture-secret-value"
    waiting = start(request)
    assert waiting.interaction_id
    pending = agent.pending_interaction()
    assert "fixture-secret-value" not in waiting.text + pending.title + pending.detail
    assert "[redacted]" in pending.detail


@pytest.mark.parametrize("mode", ["ask", "auto", "full"])
def test_provider_app_consent_is_still_required_in_each_execution_mode(
    host, monkeypatch, mode
):
    start, record, _ = host
    monkeypatch.setenv("WALKIEONE_APPROVAL_MODE", mode)
    request = app_request()
    # App Server can deliver an MCP request without a correlated turn ID.
    request["params"]["turnId"] = None
    waiting = start(request, origin="mac")
    assert waiting.interaction_id
    assert not record.exists()
    agent.resolve_interaction(waiting.interaction_id, "deny", origin="mac")
    assert json.loads(record.read_text())["result"]["action"] == "decline"
