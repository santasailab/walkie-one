"""Keep real configuration out even when it is renamed or force-added."""

import json
import runpy
import secrets
import subprocess
from pathlib import Path

import pytest

CHECK = runpy.run_path(str(Path(__file__).parents[1] / "check-repository.py"))


@pytest.fixture
def repository(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_renamed_profile_is_rejected_without_printing_its_credential(repository):
    secret = secrets.token_urlsafe(32)
    (repository / "my-handheld.json").write_text(
        json.dumps({"url": "wss://example.invalid/device", "credential": secret})
    )
    _, failures = CHECK["check_repository"](repository)
    assert failures == ["Generated credential profile: my-handheld.json"]
    assert secret not in "\n".join(failures)


@pytest.mark.parametrize(
    "name", [".env.production", "transcripts.jsonl.2", ".ssh/config"]
)
def test_force_added_private_artifact_is_rejected(repository, name):
    path = repository / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("private fixture")
    (repository / ".gitignore").write_text(name + "\n")
    assert CHECK["check_repository"](repository)[1] == []
    subprocess.run(["git", "add", "-f", name], cwd=repository, check=True)
    assert (
        f"Private/generated artifact: {name}"
        in CHECK["check_repository"](repository)[1]
    )


def test_source_schema_is_allowed_but_symlinks_cannot_import_external_files(repository):
    (repository / "events.json").write_text('{"type":"object"}')
    assert CHECK["check_repository"](repository)[1] == []
    (repository / "outside").symlink_to(repository.parent / "missing-private-file")
    assert CHECK["check_repository"](repository)[1] == [
        "Symlink is outside this source distribution: outside"
    ]
