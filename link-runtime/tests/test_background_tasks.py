from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

LINK_RUNTIME = Path(__file__).parents[1]
sys.path.insert(0, str(LINK_RUNTIME))

import tasks as background
from proactive import LocalSendReceipt, LocalSendRequest, LocalSendServer, task_routes
from tasks import TaskRunner, diagnose


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def _runner(path: Path, sent: list, clock=time.time, status="queued"):
    async def send(request):
        sent.append(request)
        return LocalSendReceipt(
            status,
            message_id="m" if status == "queued" else None,
            reason=None if status == "queued" else "link_disconnected",
        )

    return TaskRunner(
        path / "tasks",
        send,
        lambda name: {"PATH": "/usr/bin:/bin", "WALKIEONE_TASK_NAME": name},
        path,
        request=lambda text, key: LocalSendRequest(text=text, idempotency_key=key),
        clock=clock,
    )


def _fields(**values):
    fields = {key: [str(value)] for key, value in values.items() if key != "arg"}
    fields["arg"] = list(values.get("arg", []))
    return fields


async def _settle(runner):
    while runner._runs:
        await asyncio.gather(*runner._runs.values(), return_exceptions=True)


def _cli(socket_path: Path, *args: str):
    environment = dict(os.environ, WALKIEONE_SEND_SOCKET=str(socket_path))
    result = subprocess.run(
        [str(LINK_RUNTIME / "walkieone-task"), *args],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )
    return result.returncode, json.loads(result.stdout)


def test_agent_registers_runs_lists_and_removes_a_task_through_the_socket(tmp_path):
    async def exercise():
        with tempfile.TemporaryDirectory(prefix="w1-", dir="/tmp") as short:
            socket_path = Path(short) / "send.sock"
            runner = _runner(tmp_path, [])
            runner.load()
            async with LocalSendServer(
                socket_path, runner.handle, routes=task_routes(runner.handle)
            ):
                code, saved = await asyncio.to_thread(
                    _cli,
                    socket_path,
                    "add",
                    "inbox-watch",
                    "--every",
                    "60",
                    "--",
                    "/bin/sh",
                    "-c",
                    'printf "%s saw two words" "$WALKIEONE_TASK_NAME"',
                )
                assert (code, saved["status"]) == (0, "saved")
                definition = json.loads((runner.path / "inbox-watch.json").read_text())
                # Words containing spaces arrive as one argument each.
                assert definition["command"][-1] == (
                    'printf "%s saw two words" "$WALKIEONE_TASK_NAME"'
                )
                assert oct(runner.path.stat().st_mode & 0o777) == "0o700"

                code, result = await asyncio.to_thread(
                    _cli, socket_path, "run", "inbox-watch"
                )
                assert code == 0
                assert result["status"] == "ok"
                assert result["output"].strip().endswith("inbox-watch saw two words")

                code, listed = await asyncio.to_thread(_cli, socket_path, "list")
                assert [task["name"] for task in listed["tasks"]] == ["inbox-watch"]
                assert listed["tasks"][0]["ok"] is True

                code, removed = await asyncio.to_thread(
                    _cli, socket_path, "remove", "inbox-watch"
                )
                assert removed["status"] == "removed"
                assert not list(runner.path.glob("inbox-watch*"))
                code, missing = await asyncio.to_thread(
                    _cli, socket_path, "remove", "inbox-watch"
                )
                assert code != 0 and missing["reason"] == "not_found"

    asyncio.run(exercise())


def test_cli_reports_when_walkieone_is_not_running(tmp_path):
    code, result = _cli(tmp_path / "missing.sock", "list")
    assert code == 3
    assert result == {"v": 1, "status": "rejected", "reason": "walkieone_not_running"}


@pytest.mark.parametrize(
    "values, reason",
    [
        ({"name": "Bad Name", "every": 60, "arg": ["/usr/bin/true"]}, "invalid_name"),
        ({"name": "fast", "every": 5, "arg": ["/usr/bin/true"]}, "invalid_every"),
        (
            {"name": "slow", "every": 60, "timeout": 4000, "arg": ["/usr/bin/true"]},
            "invalid_timeout",
        ),
        ({"name": "empty", "every": 60}, "invalid_command"),
    ],
)
def test_invalid_task_definitions_are_refused(tmp_path, values, reason):
    runner = _runner(tmp_path, [])
    status, payload = asyncio.run(runner.handle("add", _fields(**values)))
    assert (status, payload["reason"]) == (400, reason)
    assert not runner.tasks


def test_task_count_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(background, "MAX_TASKS", 2)
    runner = _runner(tmp_path, [])
    for name in ("one", "two"):
        assert (
            runner.add(_fields(name=name, every=60, arg=["/usr/bin/true"]))["status"]
            == "saved"
        )
    _status, payload = asyncio.run(
        runner.handle("add", _fields(name="three", every=60, arg=["/usr/bin/true"]))
    )
    assert payload["reason"] == "too_many_tasks"
    # Replacing an existing task is not a new one.
    assert (
        runner.add(_fields(name="two", every=120, arg=["/usr/bin/true"]))["status"]
        == "saved"
    )


def test_definitions_survive_a_link_restart(tmp_path):
    first = _runner(tmp_path, [])
    first.add(_fields(name="briefing", every=3600, arg=["/usr/bin/true"]))
    second = _runner(tmp_path, [])
    second.load()
    assert second.tasks["briefing"]["every"] == 3600


def test_tasks_run_when_added_then_on_their_interval(tmp_path):
    async def exercise():
        clock = Clock()
        runner = _runner(tmp_path, [], clock=clock)
        runner.add(_fields(name="tick", every=60, arg=["/bin/sh", "-c", "echo run"]))
        runner.tick()
        await _settle(runner)
        runner.tick()
        assert not runner._runs  # not due again yet
        clock.now += 60
        runner.tick()
        await _settle(runner)
        log = (runner.path / "tick.log").read_text()
        assert log.count("scheduled run") == 2

    asyncio.run(exercise())


def test_a_refused_macos_permission_is_spoken_once_with_the_fix(tmp_path):
    async def exercise():
        clock, sent = Clock(), []
        runner = _runner(tmp_path, sent, clock=clock)
        refusal = (
            "echo 'execution error: Not authorized to send Apple events to "
            "Google Chrome. (-1743)' >&2; exit 1"
        )
        runner.add(
            _fields(name="whatsapp-alerts", every=60, arg=["/bin/sh", "-c", refusal])
        )
        for _ in range(2):
            runner.tick()
            await _settle(runner)
            clock.now += 60
        assert len(sent) == 1
        assert "permission to control Google Chrome" in sent[0].text
        assert "Automation" in sent[0].text and "WalkieOne" in sent[0].text
        assert runner.list()[0]["diagnosis"] == "automation"
        assert runner.list()[0]["app"] == "Google Chrome"

        # Fixing it resets the streak, so a later break is announced again.
        runner.add(_fields(name="whatsapp-alerts", every=60, arg=["/usr/bin/true"]))
        runner.tick()
        await _settle(runner)
        assert runner.state["whatsapp-alerts"]["failures"] == 0

    asyncio.run(exercise())


def test_a_manual_run_reports_to_the_agent_without_speaking(tmp_path):
    async def exercise():
        sent = []
        runner = _runner(tmp_path, sent)
        runner.add(
            _fields(
                name="needs-access",
                every=60,
                arg=[
                    "/bin/sh",
                    "-c",
                    "echo 'osascript is not allowed assistive access' >&2; exit 1",
                ],
            )
        )
        result = await runner.run_now("needs-access")
        assert (result["status"], result["diagnosis"]) == ("failed", "accessibility")
        assert sent == []

    asyncio.run(exercise())


def test_a_hung_task_is_killed_with_its_children_and_eventually_reported(tmp_path):
    async def exercise():
        clock, sent = Clock(), []
        runner = _runner(tmp_path, sent, clock=clock)
        marker = tmp_path / "child.pid"
        hang = f"sleep 30 & echo $! > {marker}; wait"
        runner.add(
            _fields(name="hangs", every=60, timeout=1, arg=["/bin/sh", "-c", hang])
        )
        for attempt in range(background.ALERT_AFTER):
            runner.tick()
            await _settle(runner)
            clock.now += 60
            assert len(sent) == (1 if attempt == background.ALERT_AFTER - 1 else 0)
        child = int(marker.read_text())
        await asyncio.sleep(0.1)
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
        assert "keeps timing out" in sent[0].text
        assert "permission box" in sent[0].text

    asyncio.run(exercise())


def test_an_undelivered_alert_is_retried_on_the_next_failure(tmp_path):
    async def exercise():
        clock, sent = Clock(), []
        runner = _runner(tmp_path, sent, clock=clock, status="rejected")
        refusal = (
            "echo 'Not authorized to send Apple events to Mail. (-1743)' >&2; exit 1"
        )
        runner.add(_fields(name="mail", every=60, arg=["/bin/sh", "-c", refusal]))
        for _ in range(2):
            runner.tick()
            await _settle(runner)
            clock.now += 60
        assert len(sent) == 2
        # Same failure streak, same message: the Mailbox deduplicates retries.
        assert sent[0].idempotency_key == sent[1].idempotency_key

    asyncio.run(exercise())


def test_a_missing_program_fails_without_stopping_the_runner(tmp_path):
    runner = _runner(tmp_path, [])
    runner.add(_fields(name="gone", every=60, arg=["/nonexistent/watcher"]))
    result = asyncio.run(runner.run_now("gone"))
    assert result["status"] == "failed"
    assert "could not start" in result["output"]


def test_diagnosis_names_the_app_macos_refused():
    assert diagnose(
        "execution error: Not authorized to send Apple events to “Safari”. (-1743)",
        False,
    ) == ("automation", "Safari")
    assert diagnose("anything", True) == ("timeout", None)
    # A watcher that times out its own AppleScript call is waiting on macOS too.
    assert diagnose(
        "Command '['/usr/bin/osascript', '-e', 'tell application']' timed out after 25 seconds",
        False,
    ) == ("timeout", None)
    assert diagnose("execution error: AppleEvent timed out. (-1712)", False) == (
        "timeout",
        None,
    )
    assert diagnose("Traceback: KeyError", False) == ("failed", None)


def test_task_environment_has_the_send_tools_but_no_turn_or_credentials(monkeypatch):
    import agent
    import link

    monkeypatch.setenv("RELAY_TOKEN", "secret")
    monkeypatch.setattr(agent, "_turn_token", "turn-in-progress")
    env = link.task_env("whatsapp-alerts")
    assert "RELAY_TOKEN" not in env
    assert "WALKIEONE_TURN" not in env
    assert env["WALKIEONE_TASK_NAME"] == "whatsapp-alerts"
    assert env["WALKIEONE_SEND"].endswith("/walkieone-send")
    assert env["WALKIEONE_TASK"].endswith("/walkieone-task")
    assert os.access(env["WALKIEONE_TASK"], os.X_OK)


def test_a_noisy_task_is_stopped_and_its_log_is_trimmed(tmp_path, monkeypatch):
    monkeypatch.setattr(background, "LOG_RUN_MAX", 256 * 1024)
    runner = _runner(tmp_path, [])
    marker = tmp_path / "noisy-child.pid"
    code = (
        "import subprocess, time, os; "
        "child = subprocess.Popen(['/bin/sleep', '60']); "
        f"open({str(marker)!r}, 'w').write(str(child.pid)); "
        "os.write(1, b'x' * (512 * 1024)); time.sleep(60)"
    )
    runner.add(_fields(name="noisy", every=60, arg=[sys.executable, "-c", code]))
    result = asyncio.run(asyncio.wait_for(runner.run_now("noisy"), 5))
    assert result["diagnosis"] == "output_limit"
    assert (runner.path / "noisy.log").stat().st_size <= background.LOG_MAX
    assert runner._processes == {}
    time.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_log_trimming_reads_only_a_bounded_tail(tmp_path, monkeypatch):
    runner = _runner(tmp_path, [])
    runner.load()
    path = runner.path / "large.log"
    with path.open("wb") as handle:
        handle.seek(128 * 1024 * 1024)
        handle.write(b"the useful tail\n")

    def forbid_read_bytes(_self):
        raise AssertionError("must never read an entire legacy task log")

    monkeypatch.setattr(Path, "read_bytes", forbid_read_bytes)
    with runner._log("large"):
        pass
    assert path.stat().st_size == background.LOG_MAX
    assert path.read_text().endswith("the useful tail\n")


def test_old_fifteen_second_tasks_resume_at_the_new_minimum(tmp_path):
    runner = _runner(tmp_path, [])
    runner.load()
    (runner.path / "old.json").write_text(
        json.dumps(
            {
                "v": 1,
                "name": "old",
                "every": 15,
                "timeout": 30,
                "command": ["/usr/bin/true"],
            }
        )
    )
    runner.load()
    assert runner.list()[0]["every"] == 60
