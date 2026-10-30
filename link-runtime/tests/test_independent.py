import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "independent_link", Path(__file__).parents[1] / "independent.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_link_only_needs_scoped_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("WALKIEONE_SEND_SOCKET", "/another-link/send.sock")
    path = tmp_path / "link.json"
    profile = {
        "url": "wss://relay.example.org/link",
        "credential": "v2.channel.linkid.link." + "A" * 43,
    }
    path.write_text(json.dumps(profile))
    path.chmod(0o600)
    environment = module.link_environment(path, "codex")
    assert environment["RELAY_CLOUD_URL"] == profile["url"]
    assert environment["RELAY_TOKEN"] == profile["credential"]
    assert environment["LINK_HOST"] == "127.0.0.1"
    assert environment["LINK_PORT"] == "0"
    assert environment["WALKIEONE_SEND_SOCKET"] != "/another-link/send.sock"
    assert environment["WALKIEONE_SEND_SOCKET"].endswith(".sock")
    assert len(environment["LINK_AUTH"]) >= 43
    assert environment["LINK_AUTH"] != profile["credential"]
    assert (
        module.link_environment(path, "codex")["LINK_AUTH"] != environment["LINK_AUTH"]
    )
    assert "/.relay/servers/" in environment["WALKIEONE_STATE_DIRECTORY"]
    assert environment["WALKIEONE_TASKS"].startswith(
        environment["WALKIEONE_STATE_DIRECTORY"]
    )
    profile["url"] = "wss://other.example.org/link"
    path.write_text(json.dumps(profile))
    other = module.link_environment(path, "codex")
    assert (
        other["WALKIEONE_STATE_DIRECTORY"] != environment["WALKIEONE_STATE_DIRECTORY"]
    )
    assert other["WALKIEONE_SEND_SOCKET"] != environment["WALKIEONE_SEND_SOCKET"]
    path.chmod(0o644)
    with pytest.raises(ValueError):
        module.link_environment(path, "codex")


@pytest.mark.parametrize(
    "change",
    [
        {"url": "ws://relay.example.org/link"},
        {"url": "wss://user:pw@relay.example.org/link"},
        {"url": "wss://relay.example.org/link?token=secret"},
        {"credential": "v1.pair.device." + "A" * 43},
        {"relay_signing_key": "must not travel to Link"},
    ],
)
def test_link_rejects_wrong_authority_and_unsafe_urls(tmp_path, change):
    path = tmp_path / "link.json"
    path.write_text(
        json.dumps(
            {
                "url": "wss://relay.example.org/link",
                "credential": "v2.channel.linkid.link." + "A" * 43,
            }
            | change
        )
    )
    path.chmod(0o600)
    with pytest.raises(ValueError):
        module.link_environment(path, "codex", development=True)
