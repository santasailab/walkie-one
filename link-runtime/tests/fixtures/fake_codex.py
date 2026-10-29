#!/usr/bin/env python3
"""Deterministic Codex App Server with one reverse approval request."""

import json
import os
import sys


def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


if sys.argv[1:] != ["app-server", "--stdio"]:
    raise SystemExit(2)

approval_id = 700
elicitation = os.environ.get("FAKE_CODEX_ELICITATION")
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        send({"id": request_id, "result": {}})
    elif method in {"thread/start", "thread/resume"}:
        send({"id": request_id, "result": {"thread": {"id": "codex-thread"}}})
    elif method == "turn/start":
        send({"id": request_id, "result": {"turn": {"id": "codex-turn"}}})
        if elicitation:
            with open(elicitation, encoding="utf-8") as handle:
                approval = json.load(handle)
            approval_id = approval["id"]
            send(approval)
            continue
        send(
            {
                "id": 700,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "codex-thread",
                    "turnId": "codex-turn",
                    "itemId": "command-one",
                    "command": "touch requested-file",
                    "reason": "Create the file the user requested",
                },
            }
        )
    elif request_id == approval_id and method is None:
        record = os.environ.get("FAKE_CODEX_RECORD")
        if record:
            with open(record, "w", encoding="utf-8") as handle:
                json.dump(request, handle)
        result = request.get("result") or {}
        allowed = result.get("action" if elicitation else "decision") == "accept"
        if allowed and os.environ.get("FAKE_CODEX_MAC_DENIAL"):
            send(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "mcpToolCall",
                            "status": "failed",
                            "error": {"message": "not trusted for accessibility"},
                        }
                    },
                }
            )
        text = "Finished with Codex." if allowed else "Codex did not make that change."
        send(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "text": text,
                        "phase": "final_answer",
                    }
                },
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "turn": {"id": "codex-turn", "status": "completed", "items": []}
                },
            }
        )
