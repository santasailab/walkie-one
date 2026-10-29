#!/usr/bin/env python3
"""Replay the provider's version rejection, then a successful updated turn."""

import json
import os
import sys


def send(value):
    print(json.dumps(value), flush=True)


assert sys.argv[1:] == ["app-server", "--stdio"]
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
        if os.environ.get("FAKE_CODEX_MODE") == "update_required":
            turn = {
                "id": "codex-turn",
                "status": "failed",
                "items": [],
                "error": {
                    "message": json.dumps(
                        {
                            "type": "error",
                            "status": 400,
                            "error": {
                                "type": "invalid_request_error",
                                "message": "The 'gpt-6-astra' model requires a newer version of Codex. "
                                "Please upgrade to the latest app or CLI and try again.",
                            },
                        }
                    )
                },
            }
        else:
            turn = {
                "id": "codex-turn",
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "text": "Finished with Codex.",
                        "phase": "final_answer",
                    }
                ],
            }
        send({"method": "turn/completed", "params": {"turn": turn}})
