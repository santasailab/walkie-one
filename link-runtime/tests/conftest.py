"""Never let runtime tests prune or load the developer's real speech context."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import agent
import link
from proactive import SendRateLimit


@pytest.fixture(autouse=True)
def isolated_speech_storage(monkeypatch, tmp_path):
    root = tmp_path / "speech-storage"
    root.mkdir()
    monkeypatch.setattr(agent, "RELAY_HOME", str(root))
    monkeypatch.setattr(agent, "RECENT", str(root / "recent"))
    monkeypatch.setattr(link, "_proactive_rate_limit", SendRateLimit())
