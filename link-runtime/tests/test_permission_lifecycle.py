"""Permissions across a real host approval/resume/cancel lifecycle."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import agent
from provider_interactions import InteractionBroker


@pytest.fixture
def host(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "_interactions", InteractionBroker())
    monkeypatch.setattr(agent, "_active", None)
    monkeypatch.setattr(agent, "_last_failure", None)
    monkeypatch.setattr(agent, "_retained_turn_lock", None)
    monkeypatch.setattr(agent, "_retained_turn_interaction", None)
    monkeypatch.setattr(agent, "RELAY_HOME", str(tmp_path))
    monkeypatch.setattr(agent, "TURN_LOCK", str(tmp_path / "agent.lock"))
    monkeypatch.setattr(agent, "_remember", lambda *_args: None)
    monkeypatch.setattr(agent.turn_blockers, "record", lambda *_args: None)
    yield
    agent._interactions.cancel()
    agent._release_retained_process_turn()


def pending(resume, cancel=lambda: None):
    return agent._interactions.open(
        provider="codex",
        scope="device",
        origin="mac",
        kind="permission",
        title="Fixture action",
        detail="Fixture command",
        spoken="Allow once?",
        resume=resume,
        cancel=cancel,
        ttl_seconds=540,
    )


@pytest.mark.parametrize(
    "error",
    [
        agent._PermissionBlocked("Calendar"),
        agent._ConsentDialog(),
    ],
)
def test_permission_after_approval_returns_mac_repair(host, error):
    def resume(_decision, _origin):
        raise error

    interaction = pending(resume)
    outcome = agent.resolve_interaction(interaction.id, "allow")
    assert outcome.failure.code == "permission_blocked"
    assert "Mac access" in outcome.failure.repair
    assert not agent.busy()
    assert agent.pending_interaction() is None


def test_stop_after_approval_reaches_provider_and_discards_completion(host):
    entered, release, stopped = (threading.Event() for _ in range(3))
    outcomes = []

    def resume(_decision, _origin):
        entered.set()
        assert release.wait(3)
        return agent.TurnResponse("Must not deliver after Stop.", None)

    interaction = pending(resume, stopped.set)
    worker = threading.Thread(
        target=lambda: outcomes.append(
            agent.resolve_interaction(interaction.id, "allow")
        )
    )
    worker.start()
    try:
        assert entered.wait(2)
        assert agent.cancel("device")
        assert stopped.is_set()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert outcomes[0].text == ""
    assert outcomes[0].failure is None
    assert not agent.busy()


def test_permission_words_in_agent_message_do_not_abort_the_turn():
    runtime = agent._LiveTurn("codex", "device", record_blocker=lambda _: None)
    event = {
        "method": "item/completed",
        "params": {
            "item": {
                "type": "agentMessage",
                "text": "This document explains: not authorized to send Apple events.",
            }
        },
    }
    runtime._messages.put(json.dumps(event))
    assert runtime._next() == event


@pytest.mark.parametrize("exit_code", [0, 1])
def test_permission_text_in_command_output_requires_a_failed_command(exit_code):
    runtime = agent._LiveTurn("codex", "device", record_blocker=lambda _: None)
    event = {
        "method": "item/completed",
        "params": {
            "item": {
                "type": "commandExecution",
                "exitCode": exit_code,
                "aggregatedOutput": "Not authorized to send Apple events to Notes. (-1743)",
            }
        },
    }
    runtime._messages.put(json.dumps(event))
    if exit_code:
        with pytest.raises(agent._PermissionBlocked):
            runtime._next()
    else:
        assert runtime._next() == event


def test_silent_work_survives_old_limits_then_receives_output(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "WORKDIR", str(tmp_path))
    monkeypatch.setattr(agent, "CONSENT_POLL_SECONDS", 0.02)
    runtime = agent._LiveTurn("codex", "device", record_blocker=lambda _: None)
    code = "import sys,time; print(sys.stdin.readline(), flush=True); time.sleep(60)"
    runtime._start([sys.executable, "-c", code], "device")
    runtime._last_output -= 24 * 60 * 60
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(runtime._next()))
    worker.start()
    try:
        time.sleep(0.1)
        assert worker.is_alive() and runtime.process.poll() is None
        assert runtime.activity() == {"progress": "unknown"}
        event = {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": "Done"}},
        }
        runtime._send(event)
        worker.join(2)
        assert outcomes == [event]
        assert runtime.activity() == {"progress": "receiving_updates"}
    finally:
        runtime.cancel()
        runtime._finish()
        worker.join(2)
    assert not worker.is_alive()


def test_live_permission_stderr_still_interrupts_silent_work(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "WORKDIR", str(tmp_path))
    monkeypatch.setattr(agent, "CONSENT_POLL_SECONDS", 0.02)
    runtime = agent._LiveTurn("codex", "device", record_blocker=lambda _: None)
    code = "import sys,time; sys.stderr.write('not allowed assistive access\\n'); sys.stderr.flush(); time.sleep(60)"
    runtime._start([sys.executable, "-c", code], "device")
    try:
        with pytest.raises(agent._PermissionBlocked):
            runtime._next()
    finally:
        runtime.cancel()
        runtime._finish()
    assert runtime.process.poll() is not None


def test_dead_provider_with_child_holding_stdout_releases_wait(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "WORKDIR", str(tmp_path))
    monkeypatch.setattr(agent, "CONSENT_POLL_SECONDS", 0.02)
    runtime = agent._LiveTurn("codex", "device", record_blocker=lambda _: None)
    code = "import subprocess,sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])"
    runtime._start([sys.executable, "-c", code], "device")
    try:
        with pytest.raises(agent._ProtocolError, match="process exited"):
            runtime._next()
    finally:
        runtime.cancel()
        runtime._finish()
    assert runtime.process.poll() is not None


def test_approval_continuation_starts_with_fresh_observation(monkeypatch):
    runtime = agent._CodexRuntimeAdapter("device")
    runtime._last_output -= 24 * 60 * 60
    monkeypatch.setattr(runtime, "_continue", lambda *_: runtime.activity())
    assert runtime._continue_from("device", None, "allow") == {
        "progress": "receiving_updates"
    }


@pytest.mark.parametrize("text_mode", [True, False])
def test_unread_provider_input_has_a_write_deadline(monkeypatch, text_mode):
    import process_io

    monkeypatch.setattr(process_io, "WRITE_TIMEOUT_SECONDS", 0.05)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=text_mode,
    )
    lock = threading.Lock()
    payload = "x" * (4 * 1024 * 1024)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="stopped reading"):
            process_io.write_with_deadline(
                process, lock, payload if text_mode else payload.encode()
            )
        process.wait(timeout=2)
        assert not lock.locked()
        assert time.monotonic() - started < 3
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        process.stdin.close()


def test_stop_cancels_a_followup_opened_by_the_continuation(host):
    entered, release = threading.Event(), threading.Event()
    cancelled, outcomes = [], []

    def resume(_decision, _origin):
        followup = pending(lambda *_: None, lambda: cancelled.append("followup"))
        entered.set()
        assert release.wait(3)
        return agent.TurnResponse("Next question?", None, interaction_id=followup.id)

    interaction = pending(resume, lambda: cancelled.append("original"))
    worker = threading.Thread(
        target=lambda: outcomes.append(
            agent.resolve_interaction(interaction.id, "allow")
        )
    )
    worker.start()
    try:
        assert entered.wait(2)
        assert agent.cancel("device")
    finally:
        release.set()
        worker.join(3)
    assert sorted(cancelled) == ["followup", "original"]
    assert outcomes[0].text == ""
    assert agent.pending_interaction() is None


def test_claude_stop_between_defer_and_spawn_prevents_the_tool(host, monkeypatch):
    runtime = agent._ClaudeRuntimeAdapter("device")
    runtime.accepting = False  # the initial process ended at tool_deferred
    monkeypatch.setattr(agent, "forget", lambda _: None)
    runtime._retire_deferred()
    assert runtime.cancelled
    with pytest.raises(agent._ProtocolError, match="cancelled before process start"):
        runtime._start(["must-not-spawn"], "device")
    runtime._finish()


def test_deadline_is_enforced_even_if_expiry_timer_has_not_run():
    clock = [0]
    broker = InteractionBroker(clock=lambda: clock[0])
    resumed, cancelled = [], []
    interaction = broker.open(
        provider="codex",
        scope="device",
        origin="mac",
        kind="permission",
        title="Fixture",
        detail="Fixture",
        spoken="Allow once?",
        resume=lambda *_: resumed.append(True),
        cancel=lambda: cancelled.append(True),
        ttl_seconds=540,
    )
    clock[0] = 541
    with pytest.raises(agent.InteractionUnavailable):
        broker.resolve(interaction.id, "allow")
    assert not resumed
    assert cancelled == [True]


def test_permission_categories_are_visible_as_history(monkeypatch, tmp_path):
    import preflight
    import turn_blockers

    history = tmp_path / "needs-permission"
    for name in ["Mac access", "Screen Recording", "Full Disk Access", "Notes"]:
        turn_blockers.record(name, str(history))
    monkeypatch.setattr(preflight, "BLOCKED", str(history))
    assert preflight.blocked_apps() == {
        "Mac access",
        "Screen Recording",
        "Full Disk Access",
        "Notes",
    }
    assert preflight.check_permissions_pending()[0] is None


def test_late_yes_after_expiry_does_not_start_a_new_provider_turn(host, monkeypatch):
    interaction = pending(lambda *_: None)
    agent._interactions._expire(interaction.id)
    monkeypatch.setattr(
        agent, "_agent_endpoint", lambda *_: pytest.fail("started a new turn")
    )
    assert agent.ask_result("yes").text == agent.EXPIRED_APPROVAL_REPLY


def test_claude_continuation_does_not_consume_previous_process_eof(
    host, monkeypatch, tmp_path
):
    fixture = tmp_path / "claude"
    fixture.write_text(
        "#!" + sys.executable + "\n"
        "import json,os,select,sys\n"
        "if os.environ.get('WALKIEONE_CLAUDE_TOOL_DECISION') == 'allow':\n"
        # A resumed Claude replays the tool with no new message, and only an
        # open stdin lets it forward an MCP server's own question later.
        " ready, _, _ = select.select([sys.stdin], [], [], 0.5)\n"
        " text = 'stdin was closed' if ready and not sys.stdin.readline() else 'Completed.'\n"
        " print(json.dumps({'type':'result','session_id':'fixture','result':text}))\n"
        "else:\n"
        " sys.stdin.readline()\n"
        " print(json.dumps({'type':'result','session_id':'fixture','stop_reason':'tool_deferred',"
        "'deferred_tool_use':{'id':'tool-1','name':'Bash','input':{'command':'fixture'}}}))\n"
    )
    fixture.chmod(0o700)
    monkeypatch.setattr(agent, "BACKEND", "claude")
    monkeypatch.setattr(agent, "WORKDIR", str(tmp_path))
    monkeypatch.setattr(agent, "TURN_DIR", str(tmp_path / "turns"))
    monkeypatch.setattr(agent, "executable", lambda _: str(fixture))
    monkeypatch.setattr(agent, "setup", lambda *_: None)
    monkeypatch.setattr(agent, "_session", lambda _: None)
    question = agent.ask_result("Run it", origin="mac")
    assert question.interaction_id is not None
    result = agent.resolve_interaction(question.interaction_id, "allow")
    assert result.failure is None
    assert result.text == "Completed."
