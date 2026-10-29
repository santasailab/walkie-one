"""Exercise SIGTERM and timeouts against isolated, real process trees."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import alerts
import pytest

RUNTIME = Path(__file__).parents[1]


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("isolated child did not reach the expected state")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def fake_tree(root):
    script = root / "fake-agent"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, subprocess, sys, time\n"
        "kind = sys.argv[1] if sys.argv[1] in ('provider', 'task') else 'alert'\n"
        "child = subprocess.Popen(['/bin/sleep', '60'])\n"
        f"open({str(root)!r} + '/' + kind + '.pid', 'w').write(str(os.getpid()) + ' ' + str(child.pid))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    script.chmod(0o700)
    return script


def cleanup_trees(root):
    for path in root.glob("*.pid"):
        for pid in map(int, path.read_text().split()):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("cancel", [False, True])
def test_alert_timeout_or_cancellation_kills_descendants(tmp_path, cancel):
    fake = fake_tree(tmp_path)

    async def exercise():
        task = asyncio.create_task(
            alerts.compose(
                "Mail",
                "Every item",
                "new mail",
                ("claude", str(fake)),
                dict(os.environ),
                timeout=0.5 if not cancel else 30,
            )
        )
        if cancel:
            await asyncio.to_thread(wait_for, lambda: (tmp_path / "alert.pid").exists())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is None

    try:
        asyncio.run(asyncio.wait_for(exercise(), 5))
        pids = list(map(int, (tmp_path / "alert.pid").read_text().split()))
        wait_for(lambda: all(not alive(pid) for pid in pids))
    finally:
        cleanup_trees(tmp_path)


def test_sigterm_unwinds_real_serve_and_kills_provider_task_and_alert_groups():
    with tempfile.TemporaryDirectory(prefix="w1-stop-", dir="/tmp") as directory:
        root = Path(directory)
        fake = fake_tree(root)
        task_dir = root / "tasks"
        task_dir.mkdir()
        (task_dir / "watch.json").write_text(
            json.dumps(
                {
                    "v": 1,
                    "name": "watch",
                    "every": 60,
                    "timeout": 60,
                    "command": [str(fake), "task"],
                }
            )
        )
        launcher = root / "launch.py"
        launcher.write_text(
            "import asyncio, sys\n"
            f"sys.path.insert(0, {str(RUNTIME)!r})\n"
            "import agent, link, runtime\n"
            "agent.setup = lambda: None\n"
            f"agent.executable = lambda name: {str(fake)!r}\n"
            "provider = agent._LiveTurn('claude', 'device')\n"
            f"provider._start([{str(fake)!r}, 'provider'], 'device')\n"
            "agent._active = provider\n"
            "asyncio.run(runtime.run_service(link.serve))\n"
        )
        env = dict(
            os.environ,
            RELAY_HOME=str(root / "relay"),
            WALKIEONE_TASKS=str(task_dir),
            WALKIEONE_OUTBOX=str(root / "outbox"),
            WALKIEONE_SEND_SOCKET=str(root / "send.sock"),
            LINK_HOST="127.0.0.1",
            LINK_PORT="0",
        )
        env.pop("RELAY_CLOUD_URL", None)
        env.pop("RELAY_TOKEN", None)
        (root / "relay" / "agent").mkdir(parents=True)
        process = subprocess.Popen(
            [sys.executable, str(launcher)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            wait_for(
                lambda: (root / "send.sock").exists() or process.poll() is not None
            )
            assert process.poll() is None, process.communicate()[0].decode()
            client.connect(str(root / "send.sock"))
            body = urllib.parse.urlencode(
                {
                    "source": "Mail",
                    "items": "news",
                    "rule": "all",
                    "fallback": "News",
                    "idempotency_key": "stop-test",
                }
            ).encode()
            client.sendall(
                b"POST /v1/alerts HTTP/1.1\r\n"
                b"Content-Type: application/x-www-form-urlencoded\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            wait_for(
                lambda: all(
                    (root / (kind + ".pid")).exists()
                    for kind in ("provider", "task", "alert")
                )
            )
            process.terminate()
            try:
                output, _ = process.communicate(timeout=10)
            except subprocess.TimeoutExpired as error:
                raise AssertionError(
                    (error.output or b"no shutdown output").decode()
                ) from error
            assert process.returncode == 0, output.decode()
            pids = [
                int(pid)
                for path in root.glob("*.pid")
                for pid in path.read_text().split()
            ]
            assert len(pids) == 6
            wait_for(lambda: all(not alive(pid) for pid in pids))
            assert not (root / "send.sock").exists()
        finally:
            client.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            cleanup_trees(root)


def detached_holder(root):
    """Exits at once, leaving a new-session grandchild holding stdout open."""
    script = root / "detach"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        f"    open({str(root)!r} + '/detached.pid', 'w').write(str(os.getpid()))\n"
        "    time.sleep(60)\n"
    )
    script.chmod(0o700)
    return script


def test_a_detached_grandchild_holding_the_pipe_does_not_hang_cleanup(
    tmp_path, monkeypatch
):
    import process_io
    import tasks

    monkeypatch.setattr(process_io, "REAP_TIMEOUT_SECONDS", 0.3)
    holder = detached_holder(tmp_path)

    async def send(_request):
        return None

    runner = tasks.TaskRunner(
        tmp_path / "tasks",
        send,
        lambda name: {"PATH": "/usr/bin:/bin"},
        tmp_path,
        request=lambda text, key: None,
    )
    runner.add(
        {"name": ["detach"], "every": ["60"], "timeout": ["1"], "arg": [str(holder)]}
    )

    async def exercise():
        alert = await alerts.compose(
            "Mail",
            "all",
            "news",
            ("claude", str(holder)),
            dict(os.environ),
            timeout=0.3,
        )
        assert alert is None
        result = await runner.run_now("detach")
        assert result["status"] == "failed"

    try:
        asyncio.run(asyncio.wait_for(exercise(), 5))
    finally:
        cleanup_trees(tmp_path)


def test_a_hung_cleanup_exits_at_the_sigterm_deadline(tmp_path):
    launcher = tmp_path / "hang.py"
    launcher.write_text(
        "import asyncio, sys\n"
        f"sys.path.insert(0, {str(RUNTIME)!r})\n"
        "import runtime\n"
        "runtime.STOP_DEADLINE_SECONDS = 0.5\n"
        "async def serve():\n"
        "    print('ready', flush=True)\n"
        "    while True:\n"
        "        try:\n"
        "            await asyncio.sleep(60)\n"
        "        except asyncio.CancelledError:\n"
        "            pass  # a cleanup that never finishes\n"
        "asyncio.run(runtime.run_service(serve))\n"
    )
    process = subprocess.Popen([sys.executable, str(launcher)], stdout=subprocess.PIPE)
    try:
        assert process.stdout.readline() == b"ready\n"
        process.terminate()
        process.terminate()
        assert process.wait(timeout=5) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
