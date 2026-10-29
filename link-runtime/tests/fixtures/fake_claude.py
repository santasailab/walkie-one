#!/usr/bin/env python3
"""Stand-in for `claude -p --output-format stream-json`.

Like Claude Code 2.1.247 with its stdin held open, it forwards one MCP
elicitation as a `control_request` on stdout and waits for the matching
`control_response` on stdin before finishing the tool and the turn."""

import json
import os
import sys


def send(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


sys.stdin.readline()  # the single user message
send({"type": "system", "subtype": "init", "session_id": "claude-session"})
request = {
    "subtype": os.environ.get("FAKE_CLAUDE_CONTROL_SUBTYPE", "elicitation"),
    "mcp_server_name": "computer-use",
    "mode": "form",
    "message": 'Allow Computer Use to use "Mail"?',
    "requested_schema": {"type": "object", "properties": {}, "required": []},
}
override = os.environ.get("FAKE_CLAUDE_ELICITATION")
if override and os.path.exists(override):
    with open(override, encoding="utf-8") as handle:
        request.update(json.load(handle))
send({"type": "control_request", "request_id": "elicit-1", "request": request})
response = json.loads(sys.stdin.readline())
record = os.environ.get("FAKE_CLAUDE_RECORD")
if record:
    with open(record, "w", encoding="utf-8") as handle:
        json.dump(response, handle)
answer = response.get("response") or {}
action = (answer.get("response") or {}).get("action", "cancel")
send(
    {
        "type": "result",
        "session_id": "claude-session",
        "is_error": False,
        "result": (
            "Finished with Claude."
            if action == "accept"
            else "Claude did not make that change."
        ),
    }
)
