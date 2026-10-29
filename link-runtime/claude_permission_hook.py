#!/usr/bin/env python3
"""Session-local Claude PreToolUse decision used by WalkieOne.

Claude owns invocation and passes the tool payload over stdin. The only state
comes from the WalkieOne-owned child environment; no global Claude settings are
read or changed here.
"""

import json
import os
import sys


def main():
    try:
        request = json.load(sys.stdin)
    except (OSError, ValueError):
        return 2
    decision = os.environ.get("WALKIEONE_CLAUDE_TOOL_DECISION", "defer")
    expected = os.environ.get("WALKIEONE_CLAUDE_TOOL_ID", "")
    if decision not in {"allow", "deny", "defer"}:
        decision = "defer"
    if expected and request.get("tool_use_id") != expected:
        decision = "defer"
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": (
                "The user approved this action through WalkieOne."
                if decision == "allow"
                else "The user declined this action through WalkieOne."
                if decision == "deny"
                else ""
            ),
        }
    }
    print(json.dumps(output, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
