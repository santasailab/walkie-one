import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

LINK_RUNTIME = Path(__file__).parents[1]
sys.path.insert(0, str(LINK_RUNTIME))
sys.path.insert(0, str(Path(__file__).parent))

import agent
import preflight
from agent_endpoint import AgentEndpointError, EndpointFailureKind, TurnResponse
from fake_agent_endpoint import DeterministicAgentEndpoint

CODEX_FIXTURE = Path(__file__).parent / "fixtures" / "fake_codex.py"


def test_agent_demo_preserves_existing_user_state(tmp_path):
    state = tmp_path / "existing-state"
    workspace = state / "agent"
    workspace.mkdir(parents=True)
    personal = {
        state / "recent": b"existing private conversation\n",
        state / "recipes.meta.json": b'{"existing": true}\n',
        workspace / "AGENTS.md": b"Existing workspace instructions.\n",
        workspace / "CLAUDE.md": b"Existing assistant instructions.\n",
    }
    for path, body in personal.items():
        path.write_bytes(body)
    environment = dict(os.environ)
    for name in ("WALKIEONE_MANAGED_AGENT", "WALKIEONE_AGENT_BIN", "RELAY_AGENT"):
        environment.pop(name, None)
    environment.update(
        WALKIEONE_STATE_DIRECTORY=str(state),
        WALKIEONE_WORKSPACE=str(workspace),
    )
    result = subprocess.run(
        [sys.executable, str(LINK_RUNTIME / "agent.py"), "--demo"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert {p: p.read_bytes() for p in state.rglob("*") if p.is_file()} == personal
    assert result.returncode == 0, result.stderr


def test_managed_runtime_never_falls_back_to_personal_install(monkeypatch, tmp_path):
    personal = tmp_path / "personal" / "codex"
    personal.parent.mkdir()
    personal.write_text("#!/bin/sh\nexit 0\n")
    personal.chmod(0o755)
    monkeypatch.setenv("PATH", str(personal.parent))
    monkeypatch.setenv("RELAY_AGENT", "codex")
    monkeypatch.setenv("WALKIEONE_MANAGED_AGENT", "1")
    monkeypatch.setenv("WALKIEONE_AGENT_BIN", str(tmp_path / "missing"))

    assert agent.executable("codex") is None
    with pytest.raises(FileNotFoundError):
        agent._codex()
    monkeypatch.setenv("WALKIEONE_AGENT_BIN", "")
    assert agent.executable("codex") is None

    managed = tmp_path / "managed" / "codex"
    managed.parent.mkdir()
    managed.write_text("#!/bin/sh\nexit 0\n")
    managed.chmod(0o755)
    monkeypatch.setenv("WALKIEONE_AGENT_BIN", str(managed))
    assert agent._codex()[0] == str(managed)
    assert agent.executable("claude") is None
    managed.unlink()
    assert agent.executable("codex") is None


def test_managed_state_migration_does_not_resume_personal_threads(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(agent, "RELAY_HOME", str(tmp_path))
    monkeypatch.setattr(agent, "BACKEND", "codex")
    monkeypatch.delenv("WALKIEONE_MANAGED_AGENT", raising=False)
    personal = agent.session_file("channel:one")
    monkeypatch.setenv("WALKIEONE_MANAGED_AGENT", "1")
    managed = agent.session_file("channel:one")
    assert personal != managed
    # Package upgrades preserve the managed conversation namespace.
    monkeypatch.setenv("WALKIEONE_AGENT_BIN", "/agents/codex/next/bin/codex")
    assert agent.session_file("channel:one") == managed
    assert agent.session_file("channel:two") != managed
    monkeypatch.setenv("CODEX_HOME", "/walkieone/agent-state/codex")
    monkeypatch.setenv("DISABLE_UPDATES", "1")
    child = agent.child_env("channel:one")
    assert child["CODEX_HOME"] == "/walkieone/agent-state/codex"
    assert child["DISABLE_UPDATES"] == "1"


def test_preflight_subprocess_output_is_bounded():
    code, output, error = preflight._sh(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.stdout.write('x' * 200000); "
                "sys.stderr.write('y' * 200000)"
            ),
        ]
    )

    assert code == 0
    assert len(output) == preflight.SH_OUTPUT_MAX
    assert len(error) == preflight.SH_OUTPUT_MAX


def _isolate_host(monkeypatch, endpoint):
    agent._interactions.cancel()
    remembered = []
    monkeypatch.setattr(agent, "BACKEND", "codex")
    monkeypatch.setattr(agent, "executable", lambda _backend: "/official/codex")
    monkeypatch.setattr(agent, "_agent_endpoint", lambda _backend, _scope: endpoint)
    monkeypatch.setattr(agent, "_process_turn_lock", lambda: None)
    monkeypatch.setattr(agent, "_release_process_turn", lambda _lock: None)
    monkeypatch.setattr(agent, "setup", lambda _scope, _question: None)
    monkeypatch.setattr(agent, "_session", lambda _scope: "warm-session")
    monkeypatch.setattr(
        agent,
        "_remember",
        lambda session_id, scope: remembered.append((session_id, scope)),
    )
    monkeypatch.setattr(agent, "_with_clock", lambda question: question)
    monkeypatch.setattr(agent, "_active", None)
    monkeypatch.setattr(agent, "_last_failure", None)
    return remembered


def _codex_endpoint(monkeypatch, tmp_path):
    workdir = tmp_path / "agent"
    workdir.mkdir(exist_ok=True)
    monkeypatch.setattr(agent, "WORKDIR", str(workdir))
    monkeypatch.setattr(agent, "TURN_DIR", str(tmp_path / "turns"))
    monkeypatch.setattr(
        agent,
        "executable",
        lambda provider: str(CODEX_FIXTURE) if provider == "codex" else None,
    )
    return agent._agent_endpoint("codex", "device")


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("ask", "default"), ("auto", "acceptEdits"), ("full", "bypassPermissions")],
)
def test_execution_modes_translate_only_at_provider_launch(monkeypatch, mode, expected):
    monkeypatch.setenv("WALKIEONE_APPROVAL_MODE", mode)
    monkeypatch.setattr(agent, "executable", lambda name: f"/official/{name}")

    argv = agent._claude(None)

    assert argv[argv.index("--permission-mode") + 1] == expected
    assert ("--dangerously-skip-permissions" in argv) == (mode == "full")
    assert ("--settings" in argv) == (mode != "full")
    if mode != "full":
        settings = json.loads(argv[argv.index("--settings") + 1])
        matcher = settings["hooks"]["PreToolUse"][0]["matcher"]
        assert ("Edit" in matcher) == (mode == "ask")


def test_frozen_claude_uses_the_bundled_runtime_for_approval_hook(monkeypatch):
    monkeypatch.setenv("WALKIEONE_APPROVAL_MODE", "ask")
    monkeypatch.setattr(agent, "executable", lambda _name: "/official/claude")
    monkeypatch.setattr(agent.sys, "frozen", True, raising=False)
    monkeypatch.setattr(agent.sys, "executable", "/signed/relay-runtime")

    argv = agent._claude(None)
    settings = json.loads(argv[argv.index("--settings") + 1])
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    assert command == "/signed/relay-runtime --claude-permission-hook"
    assert "/usr/bin/python3" not in command


def test_claude_user_denial_is_a_completed_turn_not_a_permission_failure(
    monkeypatch,
):
    runtime = agent._ClaudeRuntimeAdapter("device")
    monkeypatch.setattr(runtime, "_start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_send", lambda _message: None)
    runtime._messages.put(
        json.dumps(
            {
                "type": "result",
                "session_id": "claude-session",
                "is_error": False,
                "permission_denials": [{"tool_use_id": "tool-7"}],
                "result": "I did not run the command.",
            }
        )
    )

    text, session = runtime._run_claude("", None, "device", decision="deny")

    assert text == "I did not run the command."
    assert session == "claude-session"


def test_mac_followup_approval_stays_in_app_but_final_delivery_stays_device(
    monkeypatch,
):
    agent._interactions.cancel()
    runtime = agent._CodexRuntimeAdapter("device")
    runtime.origin = "device"
    runtime.delivery_origin = "device"
    first_spec = agent._ProviderInteractionSpec(
        "permission",
        "First approval",
        "first command",
        "Say yes or no.",
        {"id": 1, "method": "item/commandExecution/requestApproval"},
    )
    second_spec = agent._ProviderInteractionSpec(
        "permission",
        "Second approval",
        "second command",
        "Say yes or no.",
        {"id": 2, "method": "item/commandExecution/requestApproval"},
    )
    monkeypatch.setattr(
        runtime, "_continue", lambda _spec, _decision: runtime._defer(second_spec)
    )

    first = runtime._defer(first_spec)
    agent._interactions.resolve(first.interaction_id, "allow", origin="mac")
    second = agent.pending_interaction()

    assert second.origin == "mac"
    assert second.delivery_origin == "device"
    agent._interactions.cancel()


def test_paused_provider_keeps_cross_process_agent_lock_until_cancelled(
    monkeypatch, tmp_path
):
    agent._interactions.cancel()
    agent._release_retained_process_turn()
    monkeypatch.setattr(agent, "TURN_LOCK", str(tmp_path / "agent.lock"))
    runtime = agent._CodexRuntimeAdapter("device")
    spec = agent._ProviderInteractionSpec(
        "permission",
        "Approval",
        "command",
        "Say yes or no.",
        {"id": 1, "method": "item/commandExecution/requestApproval"},
    )
    response = runtime._defer(spec)
    agent._retain_process_turn(agent._process_turn_lock(), response.interaction_id)
    probe = (
        "import fcntl,os,sys; "
        "f=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600); "
        "fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
    )

    blocked = subprocess.run(
        [sys.executable, "-c", probe, agent.TURN_LOCK],
        capture_output=True,
        check=False,
    )
    assert blocked.returncode != 0

    assert agent._interactions.cancel(interaction_id=response.interaction_id)
    released = subprocess.run(
        [sys.executable, "-c", probe, agent.TURN_LOCK],
        capture_output=True,
        check=False,
    )
    assert released.returncode == 0


@pytest.mark.parametrize(
    ("mode", "approval", "sandbox"),
    [
        ("ask", "on-request", "workspace-write"),
        ("auto", "never", "workspace-write"),
        ("full", "never", "danger-full-access"),
    ],
)
def test_codex_execution_mode_controls_approval_and_sandbox(
    monkeypatch, mode, approval, sandbox
):
    monkeypatch.setenv("WALKIEONE_APPROVAL_MODE", mode)

    assert agent._codex_thread_options()["approvalPolicy"] == approval
    assert agent._codex_thread_options()["sandbox"] == sandbox


def test_codex_voice_approval_waits_for_exact_playback_then_resumes(
    monkeypatch, tmp_path
):
    record = tmp_path / "codex-response.json"
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(record))
    endpoint = _codex_endpoint(monkeypatch, tmp_path)
    _isolate_host(monkeypatch, endpoint)
    monkeypatch.setattr(
        agent,
        "executable",
        lambda provider: str(CODEX_FIXTURE) if provider == "codex" else None,
    )

    pending = agent.ask_result("create it")

    assert pending.provider_interaction_ttl_seconds == 540
    assert pending.interaction_id
    assert "Say yes" in pending.text
    assert agent.status()["state"] == "waiting"
    assert agent.ask_result("yes").text == agent.BUSY_REPLY
    assert not record.exists()
    assert agent.acknowledge_interaction(pending.interaction_id) is True
    # An answer we cannot read as yes or no repeats the whole request, so
    # "what was it asking again?" is answered without interpreting it. Saying
    # the agent was busy made a real approval look like it had been ignored.
    unclear = agent.ask_result("what is it asking for again?")
    assert unclear.text.startswith(agent.ANSWER_REQUIRED_PREFIX)
    assert pending.text in unclear.text
    assert "Say yes" in unclear.text
    assert agent.pending_interaction() is not None
    assert not record.exists()
    # It keeps asking rather than giving up or timing out into a decision.
    again = agent.ask_result("hold on")
    assert again.text == unclear.text
    assert agent.pending_interaction() is not None
    # Ordinary spoken phrasing has to resolve it, not just the bare word.
    completed = agent.ask_result("yes please")
    assert completed.text == "Finished with Codex."
    assert json.loads(record.read_text())["result"] == {"decision": "accept"}
    assert agent.pending_interaction() is None


def test_agent_gets_the_local_send_capability_without_walkieone_credentials(
    monkeypatch,
):
    monkeypatch.setenv("LINK_AUTH", "local-control-secret")
    monkeypatch.setenv("RELAY_TOKEN", "cloud-link-secret")
    monkeypatch.setenv("RELAY_CLOUD_URL", "wss://relay.example/link")
    monkeypatch.setenv("RELAY_DEVICE_TOKEN", "legacy-device-secret")
    monkeypatch.setenv("RELAY_MAC_TOKEN", "legacy-mac-secret")
    monkeypatch.setenv("RELAY_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("RELAY_REPLY_TO", "device")
    monkeypatch.setenv("GROK_DEPLOYMENT_KEY", "grok-deployment-secret")
    monkeypatch.setenv("XAI_API_KEY", "xai-api-secret")

    environment = agent.child_env()

    assert environment["WALKIEONE_SEND"].endswith("/walkieone-send")
    assert os.access(environment["WALKIEONE_SEND"], os.X_OK)
    for secret in (
        "LINK_AUTH",
        "RELAY_TOKEN",
        "RELAY_CLOUD_URL",
        "RELAY_DEVICE_TOKEN",
        "RELAY_MAC_TOKEN",
        "RELAY_URL",
        "RELAY_REPLY_TO",
        "GROK_DEPLOYMENT_KEY",
        "XAI_API_KEY",
    ):
        assert secret not in environment


def test_device_host_runs_through_a_deterministic_agent_endpoint(monkeypatch):
    endpoint = DeterministicAgentEndpoint(
        [TurnResponse("Finished it.", "next-session")], scope="device"
    )
    remembered = _isolate_host(monkeypatch, endpoint)

    assert agent.ask("do the thing") == "Finished it."
    assert endpoint.requests[0].prompt == "do the thing"
    assert endpoint.requests[0].session_id == "warm-session"
    assert remembered == [("next-session", "device")]
    assert agent.answer_required() is False


def test_device_host_exposes_answer_required_to_the_cloud_link(monkeypatch):
    endpoint = DeterministicAgentEndpoint(
        [TurnResponse("Which folder?", "next-session", needs_answer=True)],
        scope="device",
    )
    _isolate_host(monkeypatch, endpoint)

    assert agent.ask("move it") == "Which folder?"
    assert agent.answer_required() is True


def test_device_host_speaks_a_provider_login_failure(monkeypatch):
    endpoint = DeterministicAgentEndpoint(
        [
            AgentEndpointError(
                EndpointFailureKind.AUTHENTICATION,
                "codex",
                "provider login required",
            )
        ],
        scope="device",
    )
    _isolate_host(monkeypatch, endpoint)

    assert agent.ask("hello") == "Your coding agent needs you to sign in on this Mac."


def test_device_host_speaks_a_provider_request_failure(monkeypatch):
    endpoint = DeterministicAgentEndpoint(
        [
            AgentEndpointError(
                EndpointFailureKind.REQUEST_FAILED,
                "codex",
                "provider rejected the request",
            )
        ],
        scope="device",
    )
    _isolate_host(monkeypatch, endpoint)

    assert agent.ask("hello") == agent.REQUEST_ERROR_REPLY


def test_outdated_codex_explains_the_update_in_speech_and_app(monkeypatch):
    endpoint = DeterministicAgentEndpoint(
        [
            AgentEndpointError(
                EndpointFailureKind.REQUEST_FAILED,
                "codex",
                "The 'gpt-6-astra' model requires a newer version of Codex. "
                "Please upgrade to the latest app or CLI and try again.",
            )
        ],
        scope="device",
    )
    _isolate_host(monkeypatch, endpoint)

    outcome = agent.ask_result("Can you hear me?")

    assert outcome.failure.code == "update_required"
    assert "update" in outcome.text.lower()
    assert outcome.failure.repair == "Update Codex in WalkieOne, then ask again."
    assert agent.status()["failure"] == "update_required"
    assert len(endpoint.requests) == 1  # Retrying the same binary cannot help.


def test_codex_update_rejection_survives_real_protocol_and_recovers(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("FAKE_CODEX_MODE", "update_required")
    endpoint = _codex_endpoint(monkeypatch, tmp_path)
    _isolate_host(monkeypatch, endpoint)
    fixture = Path(__file__).parent / "fixtures" / "fake_codex_update.py"
    monkeypatch.setattr(agent, "executable", lambda _provider: str(fixture))
    outcome = agent.ask_result("Can you hear me?")
    assert outcome.failure.code == "update_required"
    assert outcome.text == agent.UPDATE_ERROR_REPLY
    assert not agent.busy()
    assert agent.pending_interaction() is None

    monkeypatch.delenv("FAKE_CODEX_MODE")
    assert agent.ask_result("try again").text == "Finished with Codex."
    assert agent.status()["failure"] is None


def _codex_session_fixture(monkeypatch, tmp_path, rejected_methods, detail):
    session_reader, session_writer = agent._session, agent._remember
    endpoint = _codex_endpoint(monkeypatch, tmp_path)
    _isolate_host(monkeypatch, endpoint)
    monkeypatch.setattr(agent, "BACKEND", "codex")
    monkeypatch.setattr(agent, "RELAY_HOME", str(tmp_path))
    monkeypatch.setattr(agent, "_session", session_reader)
    monkeypatch.setattr(agent, "_remember", session_writer)
    fixture = Path(__file__).parent / "fixtures" / "fake_codex_session.py"
    monkeypatch.setattr(agent, "executable", lambda _provider: str(fixture))
    requests = tmp_path / "requests.jsonl"
    monkeypatch.setenv("FAKE_CODEX_REQUESTS", str(requests))
    monkeypatch.setenv("FAKE_CODEX_REJECT_METHODS", rejected_methods)
    monkeypatch.setenv("FAKE_CODEX_REJECTION", detail)
    agent._remember("locked-thread", "device")
    return requests


@pytest.mark.parametrize(
    "detail",
    [
        "thread locked-thread already has an active writer",
        (
            "failed to initialize thread persistence: thread-store conflict: "
            "thread locked-thread already has an active writer"
        ),
        "failed to initialize thread persistence: thread-store conflict",
    ],
)
def test_codex_writer_conflict_recovers_before_submitting_the_turn(
    monkeypatch, tmp_path, detail
):
    requests = _codex_session_fixture(monkeypatch, tmp_path, "thread/resume", detail)

    outcome = agent.ask_result("Convert that to Celsius.")

    assert outcome.failure is None
    assert outcome.text == "It is 20 degrees Celsius."
    assert agent._session("device") == "fresh-thread"
    events = [json.loads(line) for line in requests.read_text().splitlines()]
    assert [e["method"] for e in events] == [
        "initialize",
        "initialized",
        "thread/resume",
        "thread/start",
        "turn/start",
    ]
    assert events[-1]["params"] == {
        "threadId": "fresh-thread",
        "input": [{"type": "text", "text": "Convert that to Celsius."}],
    }
    assert events[2]["params"]["threadId"] == "locked-thread"
    assert events[3]["params"] == {
        k: v for k, v in events[2]["params"].items() if k != "threadId"
    }
    assert agent.ask_result("And tomorrow?").failure is None
    later = [json.loads(line) for line in requests.read_text().splitlines()][
        len(events) :
    ]
    assert [e["method"] for e in later] == [
        "initialize",
        "initialized",
        "thread/resume",
        "turn/start",
    ]
    assert later[2]["params"]["threadId"] == "fresh-thread"


@pytest.mark.parametrize(
    ("methods", "detail", "expected_methods"),
    [
        ("thread/resume", "The selected model does not exist.", ["thread/resume"]),
        (
            "turn/start",
            "thread locked-thread already has an active writer",
            ["thread/resume", "turn/start"],
        ),
        (
            "turn/completed",
            "thread-store conflict: thread locked-thread already has an active writer",
            ["thread/resume", "turn/start"],
        ),
        (
            "thread/resume,thread/start",
            "thread-store conflict",
            ["thread/resume", "thread/start"],
        ),
    ],
)
def test_codex_writer_recovery_is_bounded_and_only_for_a_rejected_resume(
    monkeypatch, tmp_path, methods, detail, expected_methods
):
    requests = _codex_session_fixture(monkeypatch, tmp_path, methods, detail)

    outcome = agent.ask_result("Convert that to Celsius.")

    assert outcome.failure.code == "request_failed"
    events = [json.loads(line) for line in requests.read_text().splitlines()]
    assert [e["method"] for e in events] == [
        "initialize",
        "initialized",
        *expected_methods,
    ]
    assert agent._session("device") == "locked-thread"


def test_codex_writer_recovery_requires_a_new_thread_id(monkeypatch, tmp_path):
    requests = _codex_session_fixture(
        monkeypatch, tmp_path, "thread/resume", "thread-store conflict"
    )
    monkeypatch.setenv("FAKE_CODEX_EMPTY_THREAD", "1")

    outcome = agent.ask_result("Convert that to Celsius.")

    assert outcome.failure.code == "request_failed"
    assert "did not return a thread id" in outcome.failure.diagnostic
    assert agent._session("device") == "locked-thread"
    events = [json.loads(line) for line in requests.read_text().splitlines()]
    assert [e["method"] for e in events] == [
        "initialize",
        "initialized",
        "thread/resume",
        "thread/start",
    ]


@pytest.mark.parametrize(
    "detail",
    [
        "Network request failed. Please try again.",
        "Your dependency requires a newer version of Python. Please upgrade.",
        "The selected model does not exist.",
    ],
)
def test_unrelated_request_failures_do_not_offer_a_runtime_update(monkeypatch, detail):
    endpoint = DeterministicAgentEndpoint(
        [AgentEndpointError(EndpointFailureKind.REQUEST_FAILED, "codex", detail)],
        scope="device",
    )
    _isolate_host(monkeypatch, endpoint)
    assert agent.ask_result("hello").failure.code == "request_failed"


def test_model_prose_about_updating_codex_is_not_a_failure(monkeypatch):
    text = (
        "This requires a newer version of Codex. "
        "Please upgrade to the latest app or CLI."
    )
    endpoint = DeterministicAgentEndpoint(
        [TurnResponse(text, "session")], scope="device"
    )
    _isolate_host(monkeypatch, endpoint)
    assert agent.ask_result("explain the message") == agent.AgentTurnOutcome(text)


@pytest.mark.parametrize(
    ("endpoint_results", "expected_code", "expected_reply"),
    [
        (
            [
                AgentEndpointError(
                    EndpointFailureKind.UNAVAILABLE,
                    "codex",
                    "runtime is not installed",
                )
            ],
            "unavailable",
            "Your coding agent is not available on this computer.",
        ),
        (
            [
                AgentEndpointError(
                    EndpointFailureKind.AUTHENTICATION,
                    "codex",
                    "token=provider-secret login required",
                )
            ],
            "authentication_expired",
            "Your coding agent needs you to sign in on this Mac.",
        ),
        (
            [
                AgentEndpointError(
                    EndpointFailureKind.PERMISSION,
                    "codex",
                    "Authorization: Bearer provider-secret",
                    permission_app="Calendar",
                )
            ],
            "permission_blocked",
            agent.turn_blockers.PERMISSION_REPLY,
        ),
        (
            [
                AgentEndpointError(
                    EndpointFailureKind.CRASHED,
                    "codex",
                    "process exited token=provider-secret",
                ),
                AgentEndpointError(
                    EndpointFailureKind.CRASHED,
                    "codex",
                    "process exited again token=provider-secret",
                ),
            ],
            "process_failed",
            agent.PROCESS_ERROR_REPLY,
        ),
        (
            [
                AgentEndpointError(
                    EndpointFailureKind.REQUEST_FAILED,
                    "codex",
                    "provider rejected request api_key=provider-secret",
                )
            ],
            "request_failed",
            agent.REQUEST_ERROR_REPLY,
        ),
        (
            [
                AgentEndpointError(
                    EndpointFailureKind.MALFORMED_OUTPUT,
                    "codex",
                    "private provider output token=provider-secret",
                    session_id="session-one",
                ),
                AgentEndpointError(
                    EndpointFailureKind.MALFORMED_OUTPUT,
                    "codex",
                    "private provider output again token=provider-secret",
                    session_id="session-one",
                ),
            ],
            "response_too_long",
            agent.OUTPUT_ERROR_REPLY,
        ),
    ],
)
def test_device_host_normalizes_every_agent_failure_without_exposing_secrets(
    monkeypatch, endpoint_results, expected_code, expected_reply
):
    endpoint = DeterministicAgentEndpoint(endpoint_results, scope="device")
    _isolate_host(monkeypatch, endpoint)

    outcome = agent.ask_result("hello")

    assert outcome.text == expected_reply
    assert outcome.failure is not None
    assert outcome.failure.code == expected_code
    assert outcome.failure.repair
    assert "provider-secret" not in outcome.failure.diagnostic
    status = agent.status()
    assert status["failure"] == expected_code
    assert status["repair"] == outcome.failure.repair
    assert "provider-secret" not in status["diagnostic"]


@pytest.mark.parametrize(
    "detail, secret",
    [
        ('{"access_token":"eyJ.provider.secret"}', "eyJ.provider.secret"),
        ("{'refresh_token': 'provider-secret'}", "provider-secret"),
        ('{"client_secret":"provider-secret"}', "provider-secret"),
        ('password="two words"', "two words"),
        ("Authorization: Bearer eyJ.provider.secret", "eyJ.provider.secret"),
        ('{"auth":"provider-secret"}', "provider-secret"),
        (
            "https://127.0.0.1/status?auth=rotated-local-secret&v=1",
            "rotated-local-secret",
        ),
    ],
)
def test_agent_diagnostic_redaction_covers_structured_and_quoted_secrets(
    detail, secret
):
    redacted = agent.redact_diagnostic(detail)

    assert secret not in redacted
    assert "[redacted]" in redacted


def test_successful_agent_turn_clears_the_previous_failure(monkeypatch):
    endpoint = DeterministicAgentEndpoint(
        [
            AgentEndpointError(
                EndpointFailureKind.REQUEST_FAILED,
                "codex",
                "provider rejected request",
            ),
            TurnResponse("Recovered.", "session-two"),
        ],
        scope="device",
    )
    _isolate_host(monkeypatch, endpoint)

    assert agent.ask_result("first").failure is not None
    assert agent.status()["failure"] == "request_failed"
    assert agent.ask_result("second").text == "Recovered."
    assert agent.status()["failure"] is None


def test_walkieone_does_not_own_agent_inactivity_deadlines():
    assert not hasattr(agent, "TURN_TIMEOUT")
    assert not hasattr(agent, "SILENCE_TIMEOUT")


def test_agent_availability_requires_the_selected_runtime(monkeypatch):
    monkeypatch.setattr(agent, "BACKEND", "codex")
    monkeypatch.setattr(
        agent, "executable", lambda name: "/bin/codex" if name == "codex" else None
    )
    assert agent.available() is True
    monkeypatch.setattr(agent, "executable", lambda _name: None)
    assert agent.available() is False


def test_codex_thread_uses_the_app_server_sandbox_enum():
    options = agent._codex_thread_options()

    assert options["sandbox"] == "workspace-write"
    assert options["approvalPolicy"] == "on-request"


def test_installing_a_missing_runtime_recovers_link_readiness(monkeypatch):
    installed = False
    monkeypatch.setattr(agent, "BACKEND", "codex")
    monkeypatch.setattr(agent, "_active", None)
    monkeypatch.setattr(agent, "_last_failure", None)
    monkeypatch.setattr(
        agent, "executable", lambda _name: "/official/codex" if installed else None
    )

    assert agent.status()["state"] == "unavailable"
    assert agent.status()["failure"] == "unavailable"
    installed = True
    assert agent.status()["state"] == "ready"
    assert agent.status()["failure"] is None


def test_an_existing_agent_process_gets_the_same_busy_response(monkeypatch):
    endpoint = DeterministicAgentEndpoint(
        [TurnResponse("must not run", "session")], scope="device"
    )
    _isolate_host(monkeypatch, endpoint)
    monkeypatch.setattr(
        agent,
        "_process_turn_lock",
        lambda: (_ for _ in ()).throw(BlockingIOError()),
    )

    assert agent.ask("second request") == agent.BUSY_REPLY
    assert endpoint.requests == []
    assert agent.busy() is False


def test_oversized_output_gets_one_agent_correction_without_truncation(
    monkeypatch,
):
    endpoint = DeterministicAgentEndpoint(
        [
            TurnResponse("This response is much too long to speak.", "session-one"),
            TurnResponse("Short answer.", "session-one"),
        ],
        scope="device",
    )
    _isolate_host(monkeypatch, endpoint)
    monkeypatch.setattr(agent, "SPOKEN_MAX_CHARS", 20)

    assert agent.ask("tell me") == "Short answer."
    assert len(endpoint.requests) == 2
    assert endpoint.requests[1].session_id == "session-one"
    assert "20 characters" in endpoint.requests[1].prompt


def test_repeated_oversized_output_returns_the_standard_error(monkeypatch):
    endpoint = DeterministicAgentEndpoint(
        [
            TurnResponse("This response is much too long to speak.", "session-one"),
            TurnResponse("This one is still far too long to speak.", "session-one"),
        ],
        scope="device",
    )
    _isolate_host(monkeypatch, endpoint)
    monkeypatch.setattr(agent, "SPOKEN_MAX_CHARS", 20)

    assert agent.ask("tell me") == (
        "Your coding agent couldn't produce a short spoken response."
    )
    assert len(endpoint.requests) == 2


def test_malformed_output_gets_one_correction_in_the_provider_session(monkeypatch):
    endpoint = DeterministicAgentEndpoint(
        [
            AgentEndpointError(
                EndpointFailureKind.MALFORMED_OUTPUT,
                "codex",
                "no final output",
                session_id="session-one",
            ),
            TurnResponse("Recovered answer.", "session-one"),
        ],
        scope="device",
    )
    remembered = _isolate_host(monkeypatch, endpoint)

    assert agent.ask("tell me") == "Recovered answer."
    assert endpoint.requests[1].session_id == "session-one"
    assert remembered == [("session-one", "device"), ("session-one", "device")]


def test_agent_process_crash_is_restarted_exactly_once(monkeypatch):
    endpoint = DeterministicAgentEndpoint(
        [
            AgentEndpointError(EndpointFailureKind.CRASHED, "codex", "process exited"),
            TurnResponse("Recovered.", "session-one"),
        ],
        scope="device",
    )
    _isolate_host(monkeypatch, endpoint)

    assert agent.ask("do it") == "Recovered."
    assert len(endpoint.requests) == 2


def test_agent_process_does_not_enter_a_restart_loop(monkeypatch):
    endpoint = DeterministicAgentEndpoint(
        [
            AgentEndpointError(EndpointFailureKind.CRASHED, "codex", "process exited"),
            AgentEndpointError(
                EndpointFailureKind.CRASHED, "codex", "process exited again"
            ),
        ],
        scope="device",
    )
    _isolate_host(monkeypatch, endpoint)

    assert agent.ask("do it") == agent.PROCESS_ERROR_REPLY
    assert len(endpoint.requests) == 2


def test_cancel_wins_the_output_correction_endpoint_replacement_race(monkeypatch):
    first = DeterministicAgentEndpoint(
        [TurnResponse("This answer is too long.", "session-one")], scope="device"
    )
    replacement = DeterministicAgentEndpoint(
        [TurnResponse("Must not run.", "session-one")], scope="device"
    )
    _isolate_host(monkeypatch, first)
    monkeypatch.setattr(agent, "SPOKEN_MAX_CHARS", 10)
    calls = []

    def endpoint_factory(_backend, _scope):
        calls.append(True)
        if len(calls) == 1:
            return first
        assert agent.cancel("device") is True
        return replacement

    monkeypatch.setattr(agent, "_agent_endpoint", endpoint_factory)

    assert agent.ask("tell me") == ""
    assert replacement.requests == []
    assert agent.busy() is False


def test_codex_cancel_interrupts_then_terminates_an_unresponsive_app_server():
    class Process:
        def __init__(self):
            self.waited = []
            self.killed = False

        def poll(self):
            return None

        def wait(self, timeout):
            self.waited.append(timeout)
            raise subprocess.TimeoutExpired("codex", timeout)

        def kill(self):
            self.killed = True

    process = Process()
    turn = agent._LiveTurn("codex", "device")
    turn.process = process
    turn.thread_id = "thread"
    turn.turn_id = "turn"
    sent = []
    turn._send = sent.append

    assert turn.cancel() is True
    assert sent[0]["method"] == "turn/interrupt"
    assert process.waited and all(timeout == 1 for timeout in process.waited)
    assert process.killed is True


def test_cancel_before_process_start_prevents_provider_spawn(monkeypatch):
    turn = agent._LiveTurn("codex", "device")
    spawned = []
    monkeypatch.setattr(
        agent.subprocess, "Popen", lambda *_args, **_kwargs: spawned.append(True)
    )

    assert turn.cancel() is True
    with pytest.raises(agent._ProtocolError, match="cancelled before process start"):
        turn._start(["codex"], "device")
    assert spawned == []


def test_device_host_preserves_permission_detection_from_runtime_stderr(
    monkeypatch,
):
    endpoint = DeterministicAgentEndpoint(
        [
            AgentEndpointError(
                EndpointFailureKind.CRASHED,
                "codex",
                "Not authorized to send Apple events to Calendar. (-1743)",
            )
        ],
        scope="device",
    )
    _isolate_host(monkeypatch, endpoint)
    recorded = []
    monkeypatch.setattr(agent.turn_blockers, "record", recorded.append)

    assert agent.ask("what is next?") == agent.turn_blockers.PERMISSION_REPLY
    assert recorded == ["Calendar"]


def test_endpoint_seam_preserves_shared_prompt_recipes_and_speech_context(
    monkeypatch, tmp_path
):
    workdir = tmp_path / "agent"
    monkeypatch.setattr(agent, "RELAY_HOME", str(tmp_path))
    monkeypatch.setattr(agent, "WORKDIR", str(workdir))
    monkeypatch.setattr(agent, "RECIPES", str(workdir / "RECIPES.md"))
    monkeypatch.setattr(agent, "CAPABILITIES", str(workdir / "CAPABILITIES.md"))
    monkeypatch.setattr(agent, "RECIPE_META", str(tmp_path / "recipes.meta.json"))
    monkeypatch.setattr(agent, "RECENT", str(tmp_path / "recent"))

    agent.said("said", "The build finished", scope="device")
    agent.setup("device", "what happened?")

    for name in ("AGENTS.md", "CLAUDE.md"):
        prompt = (workdir / name).read_text()
        assert "# You are being heard" in prompt
        assert "The build finished" not in prompt
        assert "# Machine-local recipes" in prompt
        assert "# Managed capabilities" in prompt
        assert "inspect the real scheduler" in prompt
        assert "Answer in English" in prompt
        assert f"{agent.SPOKEN_MAX_CHARS} characters" in prompt
        assert "steered into this very turn" not in prompt
        assert "asks for status while you are still working" not in prompt
    assert "The build finished" in agent.recent("device")
    assert agent.SESSION_TTL == 60 * 60
    assert agent.RECENT_MAX_AGE == 12 * 60 * 60


def test_managed_capabilities_survive_sessions_but_never_claim_current_state(
    tmp_path,
):
    path = tmp_path / "CAPABILITIES.md"
    path.write_text(
        agent.CAPABILITIES_HEADER
        + "## Weekday briefing\n"
        + "- Does: sends a morning message summary.\n"
        + "- Verify: inspect launchd label com.example.briefing.\n"
        + "## Weekday briefing\n"
        + "- Does: sends a weekday message summary.\n"
        + "- Verify: inspect launchd label com.example.briefing.v2.\n"
    )

    rendered = agent.managed_capabilities(str(path))

    assert rendered.count("## Weekday briefing") == 1
    assert "com.example.briefing.v2" in rendered
    assert "com.example.briefing." not in rendered.replace(
        "com.example.briefing.v2", ""
    )
    assert "not current state" in rendered
    assert path.read_text() == rendered


def test_endpoint_seam_preserves_one_hour_provider_session_continuation(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(agent, "RELAY_HOME", str(tmp_path))
    monkeypatch.setattr(agent, "BACKEND", "codex")

    agent._remember("session-one", "device")
    assert agent._session("device") == "session-one"

    session_path = Path(agent.session_file("device"))
    expired = time.time() - agent.SESSION_TTL - 1
    os.utime(session_path, (expired, expired))
    assert agent._session("device") is None


@pytest.mark.parametrize("operation", ["stat", "unlink"])
def test_session_pruning_tolerates_another_cleaner(monkeypatch, tmp_path, operation):
    monkeypatch.setattr(agent, "RELAY_HOME", str(tmp_path))
    monkeypatch.setattr(agent, "RECENT", str(tmp_path / "recent"))
    path = tmp_path / "session-fixture"
    path.write_text("provider-session-pointer")
    expired = time.time() - agent.SESSION_TTL - 1
    os.utime(path, (expired, expired))
    original = getattr(os, operation)
    unlink = os.unlink

    def race(target, *args, **kwargs):
        if str(target) == str(path):
            unlink(path)
        return original(target, *args, **kwargs)

    monkeypatch.setattr(os, operation, race)
    agent.prune_history()
    assert not path.exists()


def test_switching_to_claude_starts_a_fresh_provider_session(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "RELAY_HOME", str(tmp_path))
    monkeypatch.setattr(agent, "BACKEND", "codex")
    agent._remember("codex-session", "device")

    monkeypatch.setattr(agent, "BACKEND", "claude")
    assert agent._session("device") is None
    agent._remember("claude-session", "device")

    monkeypatch.setattr(agent, "BACKEND", "codex")
    assert agent._session("device") == "codex-session"
    monkeypatch.setattr(agent, "BACKEND", "claude")
    assert agent._session("device") == "claude-session"
