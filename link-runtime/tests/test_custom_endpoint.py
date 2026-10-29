import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import agent
from agent_endpoint import AgentEndpointError, RuntimeProtocolError, TurnRequest
from custom_endpoint import CommandRunner, load_command


def configure(tmp_path, monkeypatch, program):
    script = tmp_path / "assistant.py"
    script.write_text(program)
    config = tmp_path / "command.json"
    config.write_text(json.dumps([sys.executable, str(script)]))
    config.chmod(0o600)
    monkeypatch.setenv("WALKIEONE_CUSTOM_COMMAND", str(config))
    monkeypatch.setattr(agent, "WORKDIR", str(tmp_path))
    return config


def test_custom_assistant_uses_real_endpoint_and_sanitized_environment(
    tmp_path, monkeypatch
):
    configure(
        tmp_path,
        monkeypatch,
        """import json,sys,os
request=json.loads(sys.stdin.readline())
assert request['version']==1 and request['scope']=='owner-channel'
assert 'RELAY_TOKEN' not in os.environ and 'DEEPGRAM_API_KEY' not in os.environ
print(json.dumps({'text':'My own assistant answered.', 'session_id':'owner-session'}))
""",
    )
    monkeypatch.setenv("RELAY_TOKEN", "sensitive-fixture")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "sensitive-fixture")
    endpoint = agent._agent_endpoint("custom", "owner-channel")
    result = endpoint.run(TurnRequest("Hello", reply_to="owner-channel"))
    assert result.text == "My own assistant answered."
    assert result.session_id == "owner-session"


@pytest.mark.parametrize(
    "program",
    [
        "print('x'*20000)",
        "print('not json')",
        "import time; time.sleep(20)",
    ],
)
def test_custom_failures_are_bounded_without_private_output(
    tmp_path, monkeypatch, program
):
    configure(tmp_path, monkeypatch, program)
    monkeypatch.setattr(CommandRunner, "timeout", 0.2)
    endpoint = agent._agent_endpoint("custom", "owner-channel")
    with pytest.raises(AgentEndpointError) as error:
        endpoint.run(TurnRequest("private prompt"))
    assert "private prompt" not in str(error.value)


def test_custom_cancel_and_private_configuration(tmp_path, monkeypatch):
    config = configure(tmp_path, monkeypatch, "import time; time.sleep(20)")
    runner = CommandRunner("owner-channel")
    finished = threading.Event()

    def run():
        try:
            runner.run("hello", None, "owner-channel")
        except RuntimeProtocolError:
            finished.set()
        finally:
            finished.set()

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 2
    while runner._process is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.cancel()
    assert finished.wait(3)
    thread.join()
    config.chmod(0o644)
    with pytest.raises(ValueError):
        load_command(config)
