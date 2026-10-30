"""The daemon has to actually start.

Everything else here imports modules and calls functions, which cannot catch a
name that only resolves when `serve()` runs. A missing import in that path
shipped once: Link died the instant it launched and the Mac app could only say
"WalkieOne's engine stopped as soon as it started."
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

LINK_RUNTIME = Path(__file__).parents[1]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def link_daemon():
    port = _free_port()
    # A Unix socket path has a hard length limit, so this cannot live under the
    # usual long pytest temporary directory.
    directory = tempfile.TemporaryDirectory(prefix="w1-boot-", dir="/tmp")
    root = Path(directory.name)
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(root)}
    environment.update(
        LINK_HOST="127.0.0.1",
        LINK_PORT=str(port),
        LINK_AUTH="test-token",
        WALKIEONE_STATE_DIRECTORY=str(root / "relay"),
        WALKIEONE_WORKSPACE=str(root / "relay" / "agent"),
        WALKIEONE_SEND_SOCKET=str(root / "send.sock"),
        WALKIEONE_OUTBOX=str(root / "outbox"),
        WALKIEONE_TASKS=str(root / "tasks"),
    )
    environment.pop("RELAY_CLOUD_URL", None)
    environment.pop("RELAY_TOKEN", None)
    process = subprocess.Popen(
        [sys.executable, str(LINK_RUNTIME / "link.py")],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        yield process, port
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        directory.cleanup()


def test_link_starts_and_serves_status(link_daemon):
    process, port = link_daemon
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/status", headers={"Authorization": "test-token"}
    )

    status = None
    deadline = time.time() + 25
    while time.time() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                "Link exited during startup:\n" + (process.stdout.read() or "")[-2000:]
            )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                status = json.loads(response.read())
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.25)

    assert status is not None, "Link never answered /status"
    assert status["agent"]["backend"]
    assert "activity" in status

    # Still alive a moment later: a task that raises on its first tick would
    # take the whole process down just after it started answering.
    time.sleep(3)
    assert process.poll() is None, (
        "Link died shortly after startup:\n" + (process.stdout.read() or "")[-2000:]
    )
