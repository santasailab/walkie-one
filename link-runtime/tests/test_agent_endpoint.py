import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from agent_endpoint import (
    AgentEndpointError,
    ClaudeAgentEndpoint,
    CodexAgentEndpoint,
    EndpointFailureKind,
    EndpointState,
    RuntimePermissionBlocked,
    RuntimeProtocolError,
    TurnRequest,
    TurnResponse,
)


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, prompt, session_id, reply_to):
        self.calls.append((prompt, session_id, reply_to))
        return self.result

    def cancel(self):
        return True


class RaisingRunner(FakeRunner):
    def run(self, prompt, session_id, reply_to):
        self.calls.append((prompt, session_id, reply_to))
        raise self.result


class BlockingRunner(FakeRunner):
    def __init__(self):
        super().__init__(("done", "session"))
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, prompt, session_id, reply_to):
        self.calls.append((prompt, session_id, reply_to))
        self.started.set()
        assert self.release.wait(1)
        return self.result

    def cancel(self):
        self.release.set()
        return True


ENDPOINT_TYPES = [
    CodexAgentEndpoint,
    ClaudeAgentEndpoint,
]


def test_codex_endpoint_starts_a_new_session_through_the_common_contract():
    runner = FakeRunner(("Ready to speak.", "codex-session"))
    endpoint = CodexAgentEndpoint(
        executable=lambda _provider: "/official/codex",
        runner=lambda _scope: runner,
    )

    response = endpoint.run(TurnRequest("hello", reply_to="device"))

    assert response == TurnResponse(
        text="Ready to speak.",
        session_id="codex-session",
        needs_answer=False,
    )
    assert runner.calls == [("hello", None, "device")]


def test_readiness_detects_the_provider_owned_runtime():
    available = CodexAgentEndpoint(
        executable=lambda _provider: "/official/codex",
        runner=lambda _scope: FakeRunner(("ok", "session")),
    )
    missing = CodexAgentEndpoint(
        executable=lambda _provider: None,
        runner=lambda _scope: FakeRunner(("ok", "session")),
    )

    assert available.readiness().provider == "codex"
    assert available.readiness().state is EndpointState.READY
    assert missing.readiness().provider == "codex"
    assert missing.readiness().state is EndpointState.UNAVAILABLE

    with pytest.raises(AgentEndpointError) as raised:
        missing.run(TurnRequest("hello"))
    assert raised.value.kind is EndpointFailureKind.UNAVAILABLE


@pytest.mark.parametrize(
    ("endpoint_type", "provider"),
    [
        (CodexAgentEndpoint, "codex"),
        (ClaudeAgentEndpoint, "claude"),
    ],
)
def test_provider_adapters_resume_through_the_same_contract(endpoint_type, provider):
    runner = FakeRunner(("Still with you.", f"{provider}-session"))
    endpoint = endpoint_type(
        executable=lambda name: f"/official/{name}",
        runner=lambda _scope: runner,
    )

    response = endpoint.run(
        TurnRequest("continue", session_id="existing-session", reply_to="device")
    )

    assert response.text == "Still with you."
    assert response.session_id == f"{provider}-session"
    assert runner.calls == [("continue", "existing-session", "device")]


@pytest.mark.parametrize("endpoint_type", ENDPOINT_TYPES)
def test_provider_adapters_preserve_the_minimal_answer_required_signal(endpoint_type):
    endpoint = endpoint_type(
        executable=lambda name: f"/official/{name}",
        runner=lambda _scope: FakeRunner(("Which folder?", "session")),
    )

    assert endpoint.run(TurnRequest("move it")) == TurnResponse(
        "Which folder?", "session", needs_answer=True
    )


@pytest.mark.parametrize("endpoint_type", ENDPOINT_TYPES)
def test_provider_adapters_report_busy_without_starting_a_second_turn(endpoint_type):
    runner = BlockingRunner()
    endpoint = endpoint_type(
        executable=lambda name: f"/official/{name}",
        runner=lambda _scope: runner,
    )
    worker = threading.Thread(target=lambda: endpoint.run(TurnRequest("first")))
    worker.start()
    assert runner.started.wait(1)

    assert endpoint.readiness().state is EndpointState.BUSY
    with pytest.raises(AgentEndpointError) as raised:
        endpoint.run(TurnRequest("second"))
    assert raised.value.kind is EndpointFailureKind.BUSY
    assert runner.calls == [("first", None, "device")]

    runner.release.set()
    worker.join(1)
    assert not worker.is_alive()


@pytest.mark.parametrize("endpoint_type", ENDPOINT_TYPES)
def test_provider_adapters_report_cancellation_explicitly(endpoint_type):
    runner = BlockingRunner()
    endpoint = endpoint_type(
        executable=lambda name: f"/official/{name}",
        runner=lambda _scope: runner,
    )
    failures = []

    def run():
        try:
            endpoint.run(TurnRequest("keep working"))
        except AgentEndpointError as error:
            failures.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    assert runner.started.wait(1)

    assert endpoint.cancel() is True
    worker.join(1)

    assert [failure.kind for failure in failures] == [EndpointFailureKind.CANCELLED]
    assert endpoint.readiness().state is EndpointState.READY
    assert endpoint.run(TurnRequest("try again")) == TurnResponse(
        "done", "session", needs_answer=False
    )


@pytest.mark.parametrize("endpoint_type", ENDPOINT_TYPES)
def test_provider_adapters_map_a_runtime_crash_to_an_explicit_failure(endpoint_type):
    endpoint = endpoint_type(
        executable=lambda name: f"/official/{name}",
        runner=lambda _scope: RaisingRunner(OSError("process exited")),
    )

    with pytest.raises(AgentEndpointError) as raised:
        endpoint.run(TurnRequest("hello"))

    assert raised.value.kind is EndpointFailureKind.CRASHED
    assert "process exited" in raised.value.detail


@pytest.mark.parametrize("endpoint_type", ENDPOINT_TYPES)
def test_provider_adapters_map_login_failures_without_exposing_credentials(
    endpoint_type,
):
    endpoint = endpoint_type(
        executable=lambda name: f"/official/{name}",
        runner=lambda _scope: RaisingRunner(
            RuntimeProtocolError("authentication required; run the provider login")
        ),
    )

    with pytest.raises(AgentEndpointError) as raised:
        endpoint.run(TurnRequest("hello"))

    assert raised.value.kind is EndpointFailureKind.AUTHENTICATION
    assert not hasattr(TurnRequest("hello"), "credentials")


@pytest.mark.parametrize("endpoint_type", ENDPOINT_TYPES)
def test_provider_adapters_separate_request_failures_from_process_crashes(
    endpoint_type,
):
    endpoint = endpoint_type(
        executable=lambda name: f"/official/{name}",
        runner=lambda _scope: RaisingRunner(
            RuntimeProtocolError("provider rejected this request")
        ),
    )

    with pytest.raises(AgentEndpointError) as raised:
        endpoint.run(TurnRequest("hello"))

    assert raised.value.kind is EndpointFailureKind.REQUEST_FAILED


@pytest.mark.parametrize("endpoint_type", ENDPOINT_TYPES)
def test_provider_adapters_recognize_protocol_level_process_crashes(endpoint_type):
    endpoint = endpoint_type(
        executable=lambda name: f"/official/{name}",
        runner=lambda _scope: RaisingRunner(
            RuntimeProtocolError("agent process exited before completing")
        ),
    )

    with pytest.raises(AgentEndpointError) as raised:
        endpoint.run(TurnRequest("hello"))

    assert raised.value.kind is EndpointFailureKind.CRASHED


@pytest.mark.parametrize("endpoint_type", ENDPOINT_TYPES)
def test_provider_adapters_map_permission_failures_with_the_blocking_app(endpoint_type):
    endpoint = endpoint_type(
        executable=lambda name: f"/official/{name}",
        runner=lambda _scope: RaisingRunner(RuntimePermissionBlocked("Calendar")),
    )

    with pytest.raises(AgentEndpointError) as raised:
        endpoint.run(TurnRequest("what is next?"))

    assert raised.value.kind is EndpointFailureKind.PERMISSION
    assert raised.value.permission_app == "Calendar"


@pytest.mark.parametrize("endpoint_type", ENDPOINT_TYPES)
@pytest.mark.parametrize(
    "result",
    [None, ("", "session"), ("answer", 42), ("too", "many", "values")],
)
def test_provider_adapters_reject_malformed_spoken_output(endpoint_type, result):
    endpoint = endpoint_type(
        executable=lambda name: f"/official/{name}",
        runner=lambda _scope: FakeRunner(result),
    )

    with pytest.raises(AgentEndpointError) as raised:
        endpoint.run(TurnRequest("hello"))

    assert raised.value.kind is EndpointFailureKind.MALFORMED_OUTPUT
    if isinstance(result, tuple) and len(result) == 2 and result[1] == "session":
        assert raised.value.session_id == "session"
