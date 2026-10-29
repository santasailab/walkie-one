import concurrent.futures
import sys
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import provider_interactions
from provider_interactions import InteractionBroker, InteractionUnavailable


class _ManualTimer:
    instances: ClassVar[list] = []

    def __init__(self, _seconds, callback, args=()):
        self.callback = callback
        self.args = args
        self.cancelled = False
        self.daemon = False
        self.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback(*self.args)


def _open(broker, resumed, cancelled, *, origin="device"):
    return broker.open(
        provider="codex",
        scope="device",
        origin=origin,
        kind="permission",
        title="Allow command?",
        detail="touch marker",
        spoken="Codex wants to run a command. Say yes or no.",
        resume=lambda decision, answer_origin: (
            resumed.append((decision, answer_origin)) or "done"
        ),
        cancel=lambda: cancelled.append(True),
        ttl_seconds=540,
    )


def test_exact_ack_and_first_answer_wins_atomically(monkeypatch):
    _ManualTimer.instances = []
    monkeypatch.setattr(provider_interactions.threading, "Timer", _ManualTimer)
    broker = InteractionBroker()
    resumed = []
    cancelled = []
    interaction = _open(broker, resumed, cancelled)

    with pytest.raises(InteractionUnavailable):
        broker.resolve("0" * 32, "allow", scope="device", require_acknowledged=True)
    with pytest.raises(InteractionUnavailable):
        broker.resolve(
            interaction.id, "allow", scope="device", require_acknowledged=True
        )
    assert broker.acknowledge(interaction.id)

    def answer(decision):
        try:
            return broker.resolve(
                interaction.id,
                decision,
                scope="device",
                origin="mac",
                require_acknowledged=True,
            )
        except InteractionUnavailable:
            return "stale"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(answer, ["allow", "deny"]))

    assert sorted(outcomes) == ["done", "stale"]
    assert len(resumed) == 1
    assert resumed[0][1] == "mac"
    assert interaction.delivery_origin == "device"
    assert cancelled == []
    assert broker.snapshot() is None


def test_expiry_cancels_without_resuming(monkeypatch):
    _ManualTimer.instances = []
    monkeypatch.setattr(provider_interactions.threading, "Timer", _ManualTimer)
    broker = InteractionBroker()
    resumed = []
    cancelled = []
    interaction = _open(broker, resumed, cancelled)

    _ManualTimer.instances[0].fire()

    assert broker.snapshot() is None
    assert resumed == []
    assert cancelled == [True]
    assert broker.acknowledge(interaction.id) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yes", "allow"),
        ("Decline!", "deny"),
        ("yes, but only once", None),
        # How people actually answer out loud. Requiring the exact word "yes"
        # meant a real approval was heard as an unrelated request and answered
        # with "your coding agent is busy".
        ("Yes please", "allow"),
        ("yeah", "allow"),
        ("Sure.", "allow"),
        ("okay", "allow"),
        ("go ahead", "allow"),
        ("Do it, please", "allow"),
        ("nope", "deny"),
        ("No thanks", "deny"),
        ("don't", "deny"),
        ("never mind", "deny"),
        # Still closed: anything conditional or contradictory re-asks rather
        # than guessing, because guessing runs a real command.
        ("no, go ahead", None),
        ("yes if it is safe", None),
        ("maybe", None),
        ("run it twice", None),
        ("", None),
    ],
)
def test_spoken_decision_accepts_only_unambiguous_short_answers(text, expected):
    assert provider_interactions.spoken_decision(text) == expected
