"""Provider-neutral interface for running a turn on a local coding agent."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

BUSY_REPLY = "Your coding agent is busy."
# A paused approval is not the same as a busy agent, and saying "busy" while the
# User is answering the question makes it look like their answer was lost. The
# whole request is then repeated rather than summarized, so "what was it asking
# again?" is answered by the repeat itself instead of by interpreting the
# question.
ANSWER_REQUIRED_PREFIX = "Sorry, I need a yes or a no."


class EndpointState(Enum):
    UNAVAILABLE = "unavailable"
    READY = "ready"
    BUSY = "busy"


class EndpointFailureKind(Enum):
    UNAVAILABLE = "unavailable"
    BUSY = "busy"
    CANCELLED = "cancelled"
    CRASHED = "crashed"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    REQUEST_FAILED = "request_failed"
    MALFORMED_OUTPUT = "malformed_output"


class AgentEndpointError(RuntimeError):
    """A provider-neutral failure safe for the Link host to handle."""

    def __init__(
        self,
        kind: EndpointFailureKind,
        provider: str,
        detail: str,
        *,
        permission_app: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.kind = kind
        self.provider = provider
        self.detail = detail
        self.permission_app = permission_app
        self.session_id = session_id
        super().__init__(detail)


class RuntimeProtocolError(RuntimeError):
    """The official provider runtime rejected or broke its local protocol."""


class RuntimePermissionBlocked(RuntimeError):
    """macOS stopped the provider runtime behind a permission boundary."""

    def __init__(self, app: str) -> None:
        self.app = app
        super().__init__(f"macOS permission blocked: {app}")


class RuntimeConsentRequired(RuntimeError):
    """macOS opened a secure consent dialog for the unattended runtime."""


_AUTH_MARKERS = (
    "auth",
    "credential",
    "login",
    "log in",
    "sign in",
    "unauthorized",
    "unauthenticated",
)

_CRASH_MARKERS = (
    "broken pipe",
    "connection reset",
    "process exited",
    "process is not running",
    "terminated unexpectedly",
)


@dataclass(frozen=True)
class EndpointReadiness:
    provider: str
    state: EndpointState
    detail: str | None = None


@dataclass(frozen=True)
class TurnRequest:
    prompt: str
    session_id: str | None = None
    reply_to: str = "device"
    origin: str = "device"


@dataclass(frozen=True)
class TurnResponse:
    text: str
    session_id: str | None
    needs_answer: bool = False
    provider_interaction_ttl_seconds: int | None = None
    interaction_id: str | None = None


class AgentEndpoint(Protocol):
    """The complete provider-neutral surface used by WalkieOne Link."""

    provider: str

    def readiness(self) -> EndpointReadiness: ...

    def run(self, request: TurnRequest) -> TurnResponse: ...

    def steer(self, prompt: str) -> str: ...

    def cancel(self) -> bool: ...


class _Runner(Protocol):
    def run(
        self, prompt: str, session_id: str | None, reply_to: str
    ) -> TurnResponse | tuple[str, str | None]: ...

    def cancel(self) -> bool: ...

    def steer(self, prompt: str) -> str: ...


class _ProcessAgentEndpoint:
    provider = ""

    def __init__(
        self,
        *,
        executable: Callable[[str], str | None],
        runner: Callable[[str], _Runner],
        scope: str = "device",
    ) -> None:
        self._executable = executable
        self._runner_factory = runner
        self.scope = scope
        self._active_runner: _Runner | None = None
        self._last_runner: _Runner | None = None
        self._cancel_requested = False
        self._accepting = True
        self._last_error = ""
        self._lock = threading.Lock()
        self._runner_started = threading.Event()

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancel_requested

    def readiness(self) -> EndpointReadiness:
        with self._lock:
            if self._active_runner is not None:
                return EndpointReadiness(self.provider, EndpointState.BUSY)
        if self._executable(self.provider):
            return EndpointReadiness(self.provider, EndpointState.READY)
        return EndpointReadiness(
            self.provider, EndpointState.UNAVAILABLE, "runtime is not installed"
        )

    def activity(self) -> dict | None:
        """Local observation of the active provider, without interpreting silence."""
        with self._lock:
            runner = self._active_runner
        return getattr(runner, "activity", lambda: None)()

    @property
    def steering_sent(self) -> bool:
        with self._lock:
            return bool(getattr(self._last_runner, "steering_sent", False))

    def steer(self, prompt: str) -> str:
        # A follow-up can arrive during host setup before run() publishes its
        # runner. This wait never starts another provider operation.
        if not self._runner_started.wait(6):
            return "rejected"
        with self._lock:
            runner = self._active_runner
        return runner.steer(prompt) if runner is not None else "inactive"

    def _response(self, result: object) -> TurnResponse:
        session_id = None
        if isinstance(result, TurnResponse):
            response = result
            session_id = result.session_id
        elif isinstance(result, tuple) and len(result) == 2:
            text = result[0]
            session_id = result[1] if isinstance(result[1], str) else None
            response = TurnResponse(
                text=text,
                session_id=result[1],
                needs_answer=isinstance(text, str) and text.rstrip().endswith("?"),
            )
        else:
            raise AgentEndpointError(
                EndpointFailureKind.MALFORMED_OUTPUT,
                self.provider,
                "provider runtime returned an invalid turn result",
                session_id=session_id,
            )
        if not isinstance(response.text, str) or not response.text.strip():
            raise AgentEndpointError(
                EndpointFailureKind.MALFORMED_OUTPUT,
                self.provider,
                "provider runtime returned no final spoken output",
                session_id=session_id,
            )
        if response.session_id is not None and not isinstance(response.session_id, str):
            raise AgentEndpointError(
                EndpointFailureKind.MALFORMED_OUTPUT,
                self.provider,
                "provider runtime returned an invalid session identifier",
                session_id=session_id,
            )
        if not isinstance(response.needs_answer, bool):
            raise AgentEndpointError(
                EndpointFailureKind.MALFORMED_OUTPUT,
                self.provider,
                "provider runtime returned an invalid answer-required signal",
                session_id=session_id,
            )
        return response

    def run(self, request: TurnRequest) -> TurnResponse:
        if not self._executable(self.provider):
            raise AgentEndpointError(
                EndpointFailureKind.UNAVAILABLE,
                self.provider,
                "provider runtime is not installed",
            )
        with self._lock:
            if self._active_runner is not None:
                raise AgentEndpointError(
                    EndpointFailureKind.BUSY,
                    self.provider,
                    "the Agent Endpoint is already running a turn",
                )
            if self._cancel_requested and self._last_runner is None:
                self._accepting = False
                raise AgentEndpointError(
                    EndpointFailureKind.CANCELLED,
                    self.provider,
                    "the Agent Endpoint turn was cancelled before it started",
                )
            self._cancel_requested = False
            runner = self._runner_factory(request.reply_to)
            if hasattr(runner, "origin"):
                runner.origin = request.origin
            if hasattr(runner, "delivery_origin"):
                runner.delivery_origin = request.origin
            self._active_runner = runner
            self._runner_started.set()
            self._last_runner = runner
            self._accepting = True
        try:
            result = runner.run(request.prompt, request.session_id, request.reply_to)
            with self._lock:
                cancelled = self._cancel_requested
            if cancelled:
                raise AgentEndpointError(
                    EndpointFailureKind.CANCELLED,
                    self.provider,
                    "the active Agent Endpoint turn was cancelled",
                )
            return self._response(result)
        except TimeoutError as error:
            detail = self._runtime_detail(error, runner)
            raise AgentEndpointError(
                EndpointFailureKind.REQUEST_FAILED, self.provider, detail
            ) from error
        except OSError as error:
            with self._lock:
                cancelled = self._cancel_requested
            kind = (
                EndpointFailureKind.CANCELLED
                if cancelled
                else EndpointFailureKind.CRASHED
            )
            detail = self._runtime_detail(error, runner)
            raise AgentEndpointError(kind, self.provider, detail) from error
        except RuntimeProtocolError as error:
            detail = self._runtime_detail(error, runner)
            with self._lock:
                cancelled = self._cancel_requested
            lowered = detail.lower()
            if cancelled:
                kind = EndpointFailureKind.CANCELLED
            elif any(marker in lowered for marker in _AUTH_MARKERS):
                kind = EndpointFailureKind.AUTHENTICATION
            elif any(marker in lowered for marker in _CRASH_MARKERS):
                kind = EndpointFailureKind.CRASHED
            else:
                kind = EndpointFailureKind.REQUEST_FAILED
            raise AgentEndpointError(kind, self.provider, detail) from error
        except RuntimePermissionBlocked as error:
            raise AgentEndpointError(
                EndpointFailureKind.PERMISSION,
                self.provider,
                str(error),
                permission_app=error.app,
            ) from error
        except RuntimeConsentRequired as error:
            raise AgentEndpointError(
                EndpointFailureKind.PERMISSION,
                self.provider,
                str(error) or "macOS consent is required",
            ) from error
        finally:
            with self._lock:
                if self._active_runner is runner:
                    self._active_runner = None
                self._accepting = False

    @staticmethod
    def _runner_error(runner: _Runner) -> str:
        error = getattr(runner, "error", None)
        if not callable(error):
            return ""
        try:
            return str(error() or "").strip()
        except OSError:
            return ""

    def _runtime_detail(self, error: BaseException, runner: _Runner) -> str:
        detail = str(error).strip()
        stderr = self._runner_error(runner)
        self._last_error = "\n".join(part for part in (detail, stderr) if part)
        return self._last_error or type(error).__name__

    def cancel(self) -> bool:
        with self._lock:
            runner = self._active_runner
            if runner is not None or self._accepting:
                self._cancel_requested = True
            accepting = self._accepting
        return runner.cancel() if runner else accepting

    def error(self) -> str:
        return self._last_error


class CodexAgentEndpoint(_ProcessAgentEndpoint):
    """Adapt the Codex runtime to the common Agent Endpoint contract."""

    provider = "codex"


class ClaudeAgentEndpoint(_ProcessAgentEndpoint):
    """Adapt the Claude runtime to the common Agent Endpoint contract."""

    provider = "claude"
