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
    workspace = root / "agent"
    workspace.mkdir()
    for name, path in {
        "RELAY_HOME": root,
        "WORKDIR": workspace,
        "TURN_DIR": root / "turns",
        "TURN_LOCK": root / "agent.lock",
        "RECENT": root / "recent",
        "RECIPES": workspace / "RECIPES.md",
        "CAPABILITIES": workspace / "CAPABILITIES.md",
        "RECIPE_META": root / "recipes.meta.json",
    }.items():
        monkeypatch.setattr(agent, name, str(path))
    monkeypatch.setattr(link, "_proactive_rate_limit", SendRateLimit())
