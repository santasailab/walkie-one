from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

LINK_RUNTIME = Path(__file__).parents[1]
sys.path.insert(0, str(LINK_RUNTIME))

from alerts import AlertWriter, build_prompt, parse
from proactive import LocalSendReceipt, LocalSendRequest, LocalSendServer

ITEMS = (
    '[{"from":"Mom","text":"call me about grandma"},{"from":"Promo","text":"50% off"}]'
)


def _fake_agent(tmp_path: Path, reply: str, *, delay: float = 0) -> Path:
    """A stand-in for claude/codex that records how it was called."""
    script = tmp_path / "fake-agent"
    record = tmp_path / "call.json"
    script.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, sys, time\n"
        f"time.sleep({delay})\n"
        f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), "
        f"'listing': os.listdir('.')}}, open({str(record)!r}, 'w'))\n"
        f"reply = {reply!r}\n"
        "if '-o' in sys.argv:\n"
        "    open(sys.argv[sys.argv.index('-o') + 1], 'w').write(reply)\n"
        "else:\n"
        "    print(reply)\n"
    )
    script.chmod(0o700)
    return script


def _writer(tmp_path, sent, writer, *, timeout=10):
    async def send(request):
        sent.append(request)
        return LocalSendReceipt.queued("m1")

    return AlertWriter(
        send,
        lambda text, key: LocalSendRequest(text=text, idempotency_key=key),
        lambda: writer,
        lambda: dict(os.environ),
        timeout=timeout,
    )


def _fields(**overrides):
    fields = {
        "source": "WhatsApp",
        "rule": "only important messages",
        "items": ITEMS,
        "fallback": "2 new WhatsApp messages from Mom and Promo.",
        "idempotency_key": "whatsapp-1",
    }
    fields.update(overrides)
    return {key: [value] for key, value in fields.items() if value is not None}


def test_the_model_writes_the_alert_with_no_tools_outside_the_workspace(tmp_path):
    reply = '{"send": true, "text": "WhatsApp: your mom wants a call about grandma."}'
    fake = _fake_agent(tmp_path, reply)
    sent = []
    status, payload = asyncio.run(
        _writer(tmp_path, sent, ("claude", str(fake))).handle(_fields())
    )
    assert (status, payload["status"], payload["composed"]) == (202, "queued", True)
    assert sent[0].text == "WhatsApp: your mom wants a call about grandma."
    assert sent[0].idempotency_key == "whatsapp-1"
    call = json.loads((tmp_path / "call.json").read_text())
    assert call["argv"][:4] == ["-p", "--tools", "", "--no-session-persistence"]
    prompt = call["argv"][-1]
    assert "only important messages" in prompt and "untrusted" in prompt
    assert "call me about grandma" in prompt
    # An empty scratch directory: the agent's workspace instructions never load.
    assert call["listing"] == [] and "walkieone-alert-" in call["cwd"]


def test_codex_writes_its_answer_to_a_file_in_a_read_only_sandbox(tmp_path):
    fake = _fake_agent(
        tmp_path, '{"send": true, "text": "WhatsApp: Raj asks about dinner."}'
    )
    sent = []
    asyncio.run(_writer(tmp_path, sent, ("codex", str(fake))).handle(_fields()))
    assert sent[0].text == "WhatsApp: Raj asks about dinner."
    argv = json.loads((tmp_path / "call.json").read_text())["argv"]
    assert argv[:3] == ["exec", "--sandbox", "read-only"] and "--ephemeral" in argv


def test_the_model_can_decide_nothing_is_worth_saying(tmp_path):
    fake = _fake_agent(tmp_path, '{"send": false}')
    sent = []
    status, payload = asyncio.run(
        _writer(tmp_path, sent, ("claude", str(fake))).handle(_fields())
    )
    assert (status, payload["status"]) == (200, "skipped")
    assert sent == []


def test_news_is_never_dropped_when_the_model_fails(tmp_path):
    cases = [
        ("claude", str(_fake_agent(tmp_path, "I think Mom is important"))),
        ("claude", "/nonexistent/claude"),
        None,
    ]
    for writer in cases:
        sent = []
        _status, payload = asyncio.run(
            _writer(tmp_path, sent, writer).handle(_fields())
        )
        assert payload["composed"] is False
        assert sent[0].text == "2 new WhatsApp messages from Mom and Promo."


def test_a_slow_model_falls_back_instead_of_hanging(tmp_path):
    fake = _fake_agent(tmp_path, '{"send": true, "text": "late"}', delay=5)
    sent = []
    asyncio.run(
        _writer(tmp_path, sent, ("claude", str(fake)), timeout=0.5).handle(_fields())
    )
    assert sent[0].text.startswith("2 new WhatsApp messages")


def test_invalid_alert_requests_are_refused(tmp_path):
    for overrides, reason in (
        ({"items": None}, "invalid_items"),
        ({"fallback": ""}, "invalid_fallback"),
        ({"source": "x" * 61}, "invalid_source"),
    ):
        status, payload = asyncio.run(
            _writer(tmp_path, [], None).handle(_fields(**overrides))
        )
        assert (status, payload["reason"]) == (400, reason)


def test_parse_accepts_only_a_clear_decision():
    assert parse('Sure! {"send": true, "text": "  WhatsApp:\\n Mom  called. "}') == (
        True,
        "WhatsApp: Mom called.",
    )
    assert parse('{"send": false}') == (False, "")
    assert parse('{"send": "yes", "text": "x"}') is None
    assert parse('{"send": true}') is None
    assert parse("no json") is None
    assert "Tell me about every new item." in build_prompt("Mail", "", "[]")


def test_watcher_script_speaks_through_the_cli_over_the_socket(tmp_path):
    fake = _fake_agent(
        tmp_path, '{"send": true, "text": "WhatsApp: Mom wants a call."}'
    )
    sent = []
    writer = _writer(tmp_path, sent, ("claude", str(fake)))

    async def exercise():
        with tempfile.TemporaryDirectory(prefix="w1-", dir="/tmp") as short:
            socket_path = Path(short) / "send.sock"
            routes = {b"POST /v1/alerts HTTP/1.1": writer.handle}
            async with LocalSendServer(socket_path, writer.send, routes=routes):
                return await asyncio.to_thread(
                    subprocess.run,
                    [
                        str(LINK_RUNTIME / "walkieone-alert"),
                        "--source",
                        "WhatsApp",
                        "--fallback",
                        "New WhatsApp messages.",
                        "--idempotency-key",
                        "whatsapp-2",
                        "--rule",
                        "only important messages",
                    ],
                    input=ITEMS,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=dict(os.environ, WALKIEONE_SEND_SOCKET=str(socket_path)),
                )

    result = asyncio.run(exercise())
    assert result.returncode == 0, result.stdout
    receipt = json.loads(result.stdout)
    assert (receipt["status"], receipt["composed"]) == ("queued", True)
    assert sent[0].text == "WhatsApp: Mom wants a call."


def test_the_selected_coding_agent_writes_alerts_when_it_can(monkeypatch):
    import agent
    import link

    installed = {"claude": "/bin/claude", "codex": "/bin/codex"}
    monkeypatch.setattr(agent, "executable", installed.get)
    monkeypatch.setattr(agent, "BACKEND", "codex")
    assert link.alert_writer() == ("codex", "/bin/codex")
    monkeypatch.setattr(agent, "BACKEND", "retired-provider")
    assert link.alert_writer() == ("claude", "/bin/claude")
    monkeypatch.setattr(agent, "executable", lambda name: None)
    assert link.alert_writer() is None


def test_alert_quota_is_checked_before_starting_the_model(monkeypatch, tmp_path):
    import alerts

    composed = []

    async def compose(*_args):
        composed.append(1)
        return False, ""

    monkeypatch.setattr(alerts, "compose", compose)
    writer = _writer(tmp_path, [], None)

    async def exercise():
        for _ in range(6):
            assert (await writer.handle(_fields()))[1]["status"] == "skipped"
        status, payload = await writer.handle(_fields())
        assert status == 503 and payload["reason"] == "rate_limited"
        assert len(composed) == 6

    asyncio.run(exercise())
