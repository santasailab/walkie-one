"""Deterministic Agent Endpoint used by host and transport tests."""

from collections import deque

from agent_endpoint import EndpointReadiness, EndpointState, TurnRequest, TurnResponse


class DeterministicAgentEndpoint:
    provider = "fake"

    def __init__(
        self,
        outcomes: list[TurnResponse | BaseException],
        *,
        scope: str = "device",
    ) -> None:
        self.scope = scope
        self.requests: list[TurnRequest] = []
        self._outcomes = deque(outcomes)
        self.accepting = True
        self.cancelled = False

    def readiness(self) -> EndpointReadiness:
        state = (
            EndpointState.BUSY
            if self.requests and self.accepting
            else EndpointState.READY
        )
        return EndpointReadiness(self.provider, state)

    def run(self, request: TurnRequest) -> TurnResponse:
        self.requests.append(request)
        try:
            outcome = self._outcomes.popleft()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        finally:
            self.accepting = False

    def cancel(self) -> bool:
        if not self.accepting:
            return False
        self.cancelled = True
        self.accepting = False
        return True
