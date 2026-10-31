from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LINK_RUNTIME = Path(__file__).parents[1]
sys.path.insert(0, str(LINK_RUNTIME))

import proactive
from proactive import LocalSendReceipt, LocalSendServer


def _run_client(
    socket_path: Path, text: str, key: str, outbox: Path | None = None, wait: str = "1"
):
    environment = dict(os.environ)
    environment["WALKIEONE_SEND_SOCKET"] = str(socket_path)
    # Never let a test reach the real workspace outbox, and do not wait the
    # production ceiling for a receipt nobody is going to write.
    environment["WALKIEONE_OUTBOX"] = str(
        outbox if outbox is not None else socket_path.parent / "unused-outbox"
    )
    environment["WALKIEONE_SEND_WAIT"] = wait
    return subprocess.run(
        [
            str(LINK_RUNTIME / "walkieone-send"),
            "--text",
            text,
            "--idempotency-key",
            key,
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        # The CLI permits 15 seconds for curl, then up to 10 seconds for the
        # receipt in these tests. Also allow process startup on hosted macOS.
        timeout=30,
    )


def _short_socket_directory():
    return tempfile.TemporaryDirectory(prefix="w1-", dir="/tmp")


async def _raw_request(socket_path: Path, key: str):
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    body = f"text=Finished&idempotency_key={key}".encode()
    writer.write(
        b"POST /v1/messages HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/x-www-form-urlencoded\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    await writer.drain()
    raw_headers = await reader.readuntil(b"\r\n\r\n")
    content_length = next(
        int(line.split(b":", 1)[1])
        for line in raw_headers.split(b"\r\n")
        if line.lower().startswith(b"content-length:")
    )
    raw = await reader.readexactly(content_length)
    writer.close()
    await writer.wait_closed()
    return json.loads(raw)


def test_same_user_detached_process_receives_an_idempotent_queued_receipt():
    async def exercise():
        received = []

        async def send(request):
            received.append(request)
            return LocalSendReceipt.queued("7d53976f-5f55-58bc-b491-c09679b0fb79")

        with _short_socket_directory() as directory:
            socket_path = Path(directory) / "private" / "send.sock"
            async with LocalSendServer(socket_path, send):
                first = await asyncio.to_thread(
                    _run_client, socket_path, "The build finished.", "build-42"
                )
                duplicate = await asyncio.to_thread(
                    _run_client, socket_path, "The build finished.", "build-42"
                )
                socket_mode = socket_path.stat().st_mode & 0o777
                directory_mode = socket_path.parent.stat().st_mode & 0o777

        return received, first, duplicate, socket_mode, directory_mode

    received, first, duplicate, socket_mode, directory_mode = asyncio.run(exercise())

    assert [(item.text, item.idempotency_key) for item in received] == [
        ("The build finished.", "build-42"),
        ("The build finished.", "build-42"),
    ]
    assert first.returncode == duplicate.returncode == 0
    assert (
        json.loads(first.stdout)
        == json.loads(duplicate.stdout)
        == {
            "v": 1,
            "status": "queued",
            "message_id": "7d53976f-5f55-58bc-b491-c09679b0fb79",
        }
    )
    assert socket_mode == 0o600
    assert directory_mode == 0o700


def test_other_os_user_cannot_invoke_the_local_capability():
    async def exercise():
        called = []

        async def send(request):
            called.append(request)
            return LocalSendReceipt.queued("7d53976f-5f55-58bc-b491-c09679b0fb79")

        with _short_socket_directory() as directory:
            socket_path = Path(directory) / "private" / "send.sock"
            server = LocalSendServer(
                socket_path,
                send,
                peer_uid=lambda _socket: os.geteuid() + 1,
            )
            async with server:
                result = await asyncio.to_thread(
                    _run_client, socket_path, "Private alert.", "private-1"
                )
        return called, result

    called, result = asyncio.run(exercise())

    assert called == []
    assert result.returncode != 0
    assert json.loads(result.stdout) == {
        "v": 1,
        "status": "rejected",
        "reason": "unauthorized",
    }


def test_disconnected_link_returns_one_bounded_rejection():
    async def exercise():
        async def send(_request):
            return LocalSendReceipt.rejected("link_disconnected")

        with _short_socket_directory() as directory:
            socket_path = Path(directory) / "private" / "send.sock"
            async with LocalSendServer(socket_path, send):
                return await asyncio.to_thread(
                    _run_client, socket_path, "Still waiting.", "watch-9"
                )

    result = asyncio.run(exercise())

    # WalkieOne answered, so its refusal is the outcome. Only an unreachable
    # WalkieOne falls back to the outbox.
    assert result.returncode != 0
    assert len(result.stdout) < 256
    assert json.loads(result.stdout) == {
        "v": 1,
        "status": "rejected",
        "reason": "link_disconnected",
    }


def test_missing_local_link_returns_the_same_bounded_rejection():
    with _short_socket_directory() as directory:
        result = _run_client(
            Path(directory) / "missing" / "send.sock",
            "Still waiting.",
            "watch-10",
        )

    assert result.returncode != 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "v": 1,
        "status": "pending",
        "reason": "walkieone_not_running",
    }


def test_local_send_fanout_is_bounded_before_callbacks_accumulate(monkeypatch):
    async def exercise():
        active = 0
        peak = 0
        started = asyncio.Event()
        release = asyncio.Event()

        async def send(_request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            started.set()
            await release.wait()
            active -= 1
            return LocalSendReceipt.queued("7d53976f-5f55-58bc-b491-c09679b0fb79")

        with _short_socket_directory() as directory:
            socket_path = Path(directory) / "private" / "send.sock"
            async with LocalSendServer(socket_path, send):
                accepted = asyncio.create_task(
                    _raw_request(socket_path, "build-accepted")
                )
                await started.wait()
                rejected = await _raw_request(socket_path, "one-too-many")
                release.set()
                receipt = await accepted
        return peak, rejected, receipt

    monkeypatch.setattr(proactive, "MAX_INFLIGHT_REQUESTS", 1)
    peak, rejected, receipt = asyncio.run(exercise())

    assert peak == 1
    assert rejected == {"v": 1, "status": "rejected", "reason": "mailbox_full"}
    assert receipt["status"] == "queued"


def test_local_capability_rejects_oversized_or_malformed_speech():
    async def exercise():
        called = []

        async def send(request):
            called.append(request)
            return LocalSendReceipt.queued("7d53976f-5f55-58bc-b491-c09679b0fb79")

        with _short_socket_directory() as directory:
            socket_path = Path(directory) / "private" / "send.sock"
            async with LocalSendServer(socket_path, send):
                oversized = await asyncio.to_thread(
                    _run_client, socket_path, "x" * 1201, "too-long"
                )
        return called, oversized

    called, oversized = asyncio.run(exercise())

    assert called == []
    assert oversized.returncode != 0
    assert json.loads(oversized.stdout)["status"] == "rejected"


def test_sandboxed_agent_send_reaches_the_device_through_the_outbox():
    """A coding agent normally runs under a sandbox that denies the network, so
    it cannot open the send socket at all. It leaves the send in its workspace
    and WalkieOne does the network part."""

    async def exercise():
        delivered = []

        async def send(request):
            delivered.append(request)
            return LocalSendReceipt.queued("9f1c7a1e-0000-4000-8000-00000000abcd")

        with _short_socket_directory() as directory:
            outbox = Path(directory) / "outbox"
            unreachable = Path(directory) / "no-link" / "send.sock"
            drain = proactive.LocalSendOutbox(outbox, send)

            async def keep_draining():
                while True:
                    await drain.drain_once()
                    await asyncio.sleep(0.05)

            drainer = asyncio.ensure_future(keep_draining())
            try:
                result = await asyncio.to_thread(
                    _run_client,
                    unreachable,
                    "The build finished.",
                    "build-42",
                    outbox,
                    "20",
                )
            finally:
                drainer.cancel()
            return delivered, result, outbox

    delivered, result, outbox = asyncio.run(exercise())

    assert [(item.text, item.idempotency_key) for item in delivered] == [
        ("The build finished.", "build-42")
    ]
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "queued"
    # The request is consumed rather than left to be delivered twice.
    assert list(outbox.glob("*.request.json")) == []


def test_outbox_refuses_malformed_and_runaway_requests(tmp_path):
    async def exercise():
        delivered = []

        async def send(request):
            delivered.append(request)
            return LocalSendReceipt.queued("00000000-0000-4000-8000-000000000000")

        outbox = tmp_path / "outbox"
        outbox.mkdir(mode=0o700)
        (outbox / "bad.request.json").write_text("{not json", encoding="utf-8")
        (outbox / "empty.request.json").write_text(
            json.dumps({"v": 1, "text": "   ", "idempotency_key": "k"}),
            encoding="utf-8",
        )
        (outbox / "wrong-version.request.json").write_text(
            json.dumps({"v": 2, "text": "hi", "idempotency_key": "k"}),
            encoding="utf-8",
        )
        drain = proactive.LocalSendOutbox(outbox, send)
        await drain.drain_once()
        return delivered, outbox

    delivered, outbox = asyncio.run(exercise())

    assert delivered == []
    # Every request is answered and consumed, so a bad file cannot wedge the
    # drain or be retried forever.
    assert list(outbox.glob("*.request.json")) == []
    reasons = {
        json.loads(path.read_text())["reason"] for path in outbox.glob("*.receipt.json")
    }
    assert reasons == {"invalid_request"}


def test_outbox_is_bounded_so_a_runaway_job_cannot_flood_the_device(tmp_path):
    async def exercise():
        delivered = []

        async def send(request):
            delivered.append(request)
            return LocalSendReceipt.queued("00000000-0000-4000-8000-000000000000")

        outbox = tmp_path / "outbox"
        outbox.mkdir(mode=0o700)
        for index in range(proactive.LocalSendOutbox.MAX_PENDING + 5):
            (outbox / f"n{index:03d}.request.json").write_text(
                json.dumps(
                    {"v": 1, "text": f"m{index}", "idempotency_key": f"k{index}"}
                ),
                encoding="utf-8",
            )
        drain = proactive.LocalSendOutbox(outbox, send)
        await drain.drain_once()
        return delivered, outbox

    delivered, outbox = asyncio.run(exercise())

    assert len(delivered) == proactive.LocalSendOutbox.MAX_PENDING
    assert list(outbox.glob("*.request.json")) == []
    rejected = [
        path
        for path in outbox.glob("*.receipt.json")
        if json.loads(path.read_text()).get("reason") == "outbox_full"
    ]
    assert len(rejected) == 5


def test_a_send_with_a_time_waits_in_walkieone_not_in_a_process(tmp_path):
    """A turn's children do not survive the turn, so a later message cannot be
    a backgrounded `sleep`. WalkieOne holds it and delivers when it is due."""

    async def exercise():
        delivered = []

        async def send(request):
            delivered.append(request)
            return LocalSendReceipt.queued("11111111-0000-4000-8000-000000000000")

        outbox = tmp_path / "outbox"
        outbox.mkdir(mode=0o700)
        due = time.time() + 60
        (outbox / "later.request.json").write_text(
            json.dumps(
                {
                    "v": 1,
                    "text": "The build finished.",
                    "idempotency_key": "later-1",
                    "not_before": due,
                }
            ),
            encoding="utf-8",
        )
        drain = proactive.LocalSendOutbox(outbox, send)

        await drain.drain_once()
        early = list(delivered)
        # The message is still waiting, and the caller has been told so.
        receipt = json.loads((outbox / "later.receipt.json").read_text())
        still_queued = (outbox / "later.request.json").exists()

        # Now it is due.
        (outbox / "later.request.json").write_text(
            json.dumps(
                {
                    "v": 1,
                    "text": "The build finished.",
                    "idempotency_key": "later-1",
                    "not_before": time.time() - 1,
                }
            ),
            encoding="utf-8",
        )
        await drain.drain_once()
        return early, receipt, still_queued, delivered, outbox

    early, receipt, still_queued, delivered, outbox = asyncio.run(exercise())

    assert early == []
    assert receipt["status"] == "scheduled"
    assert still_queued
    assert [item.text for item in delivered] == ["The build finished."]
    assert list(outbox.glob("*.request.json")) == []


def test_a_scheduled_send_cannot_be_parked_indefinitely(tmp_path):
    async def exercise():
        async def send(_request):
            return LocalSendReceipt.queued("22222222-0000-4000-8000-000000000000")

        outbox = tmp_path / "outbox"
        outbox.mkdir(mode=0o700)
        (outbox / "far.request.json").write_text(
            json.dumps(
                {
                    "v": 1,
                    "text": "Next year.",
                    "idempotency_key": "far-1",
                    "not_before": time.time() + 400 * 24 * 3600,
                }
            ),
            encoding="utf-8",
        )
        drain = proactive.LocalSendOutbox(outbox, send)
        await drain.drain_once()
        return json.loads((outbox / "far.receipt.json").read_text())

    receipt = asyncio.run(exercise())

    assert receipt == {"v": 1, "status": "rejected", "reason": "invalid_request"}


def test_a_scheduled_send_keeps_its_turn_out_of_the_duplicate_check(tmp_path):
    """A reminder can come due while the turn that scheduled it is still
    running, and it is not a duplicate of that turn's answer - it was
    deliberately deferred."""

    outbox = tmp_path / "outbox"
    outbox.mkdir(mode=0o700)
    (outbox / "now.request.json").write_text(
        json.dumps({"v": 1, "text": "Now.", "idempotency_key": "n", "turn": "t-1"}),
        encoding="utf-8",
    )
    (outbox / "later.request.json").write_text(
        json.dumps(
            {
                "v": 1,
                "text": "Later.",
                "idempotency_key": "l",
                "turn": "t-1",
                "not_before": time.time() - 1,
            }
        ),
        encoding="utf-8",
    )

    immediate = proactive.LocalSendOutbox._read(outbox / "now.request.json")
    deferred = proactive.LocalSendOutbox._read(outbox / "later.request.json")

    assert immediate.turn == "t-1"
    assert deferred.turn == "", "a deferred send must not be refused as a duplicate"
