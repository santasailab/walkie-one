import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import claude_permission_hook


@pytest.mark.parametrize(
    ("decision", "expected", "tool_id"),
    [
        ("allow", "allow", "tool-7"),
        ("deny", "deny", "tool-7"),
        ("allow", "defer", "different-tool"),
    ],
)
def test_hook_applies_decision_only_to_exact_deferred_tool(
    monkeypatch, decision, expected, tool_id
):
    monkeypatch.setenv("WALKIEONE_CLAUDE_TOOL_DECISION", decision)
    monkeypatch.setenv("WALKIEONE_CLAUDE_TOOL_ID", "tool-7")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_use_id": tool_id})))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)

    assert claude_permission_hook.main() == 0

    payload = json.loads(output.getvalue())
    assert payload["hookSpecificOutput"]["permissionDecision"] == expected
