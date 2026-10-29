import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
import agent
from spoken_history import MAX_AGE, SpokenHistory


def test_provider_protocol_and_stderr_are_not_written_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "WORKDIR", str(tmp_path))
    monkeypatch.setattr(agent, "TURN_DIR", str(tmp_path / "turns"))
    marker = "private conversation and tool output"
    event = {"type": "result", "result": marker}
    code = (
        "import sys; "
        f"print({json.dumps(json.dumps(event))}); "
        f"sys.stderr.write({marker!r})"
    )
    runner = agent._LiveTurn("codex", "device")
    try:
        runner._start([sys.executable, "-c", code], "device")
        assert runner._next() == event
    finally:
        runner._finish()
    # Stderr remains available in bounded memory for permission diagnosis.
    assert marker in runner.error()
    assert not list(tmp_path.rglob("*.jsonl"))


def test_idle_cleanup_removes_expired_future_and_malformed_speech(tmp_path):
    recent = tmp_path / "recent"
    now = 200_000
    recent.write_text(
        f"{now - MAX_AGE}\tdevice\theard\texpired secret\n"
        f"{now - 30}\tdevice\tsaid\trecent answer\n"
        f"{now + 60}\tdevice\theard\tfuture secret\n"
        "invalid\n"
    )
    history = SpokenHistory(tmp_path, recent, max_age=MAX_AGE * 10)
    assert history.read(now) == [f"{now - 30}\tdevice\tsaid\trecent answer"]
    assert recent.stat().st_mode & 0o777 == 0o600
    history.read(now + MAX_AGE)
    assert not recent.exists()


def test_history_opt_out_removes_old_speech_and_never_writes_new_speech(tmp_path):
    recent = tmp_path / "recent"
    history = SpokenHistory(tmp_path, recent)
    history.append("device", "heard", "private request")
    (tmp_path / "privacy.json").write_text('{"history_enabled":false}')
    history.append("device", "said", "private reply")
    assert history.read() == []
    assert not recent.exists()


def test_legacy_cleanup_keeps_user_files_and_provider_history(tmp_path):
    turns = tmp_path / "turns"
    turns.mkdir()
    (turns / "old.jsonl").write_text("private protocol output")
    artifact = turns / "user-output.txt"
    artifact.write_text("keep")
    history = SpokenHistory(tmp_path, tmp_path / "recent")
    history.clean_legacy_output(turns)
    assert not (turns / "old.jsonl").exists()
    assert artifact.read_text() == "keep"
