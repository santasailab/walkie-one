#!/usr/bin/env python3
"""Replay a rejected Codex resume without running tools or calling a provider."""

import json
import os
import sys


def send(value):
    print(json.dumps(value), flush=True)


assert sys.argv[1:] == ["app-server", "--stdio"]
for line in sys.stdin:
    request = json.loads(line)
    with open(os.environ["FAKE_CODEX_REQUESTS"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(request) + "\n")
    method = request.get("method")
    request_id = request.get("id")
    rejected = method in os.environ["FAKE_CODEX_REJECT_METHODS"].split(",")
    if method == "thread/resume":
        rejected = rejected and request["params"]["threadId"] == "locked-thread"
    if rejected:
        send(
            {
                "id": request_id,
                "error": {
                    "code": -32600,
                    "message": os.environ["FAKE_CODEX_REJECTION"],
                },
            }
        )
    elif method == "initialize":
        send({"id": request_id, "result": {}})
    elif method in {"thread/start", "thread/resume"}:
        thread = {"id": "fresh-thread"}
        if method == "thread/start" and os.environ.get("FAKE_CODEX_EMPTY_THREAD"):
            thread = {}
        send({"id": request_id, "result": {"thread": thread}})
    elif method == "turn/start":
        send({"id": request_id, "result": {"turn": {"id": "codex-turn"}}})
        if "turn/completed" in os.environ["FAKE_CODEX_REJECT_METHODS"].split(","):
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "turn": {
                            "id": "codex-turn",
                            "status": "failed",
                            "error": {"message": os.environ["FAKE_CODEX_REJECTION"]},
                        }
                    },
                }
            )
            continue
        send(
            {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "id": "codex-turn",
                        "status": "completed",
                        "items": [
                            {
                                "type": "agentMessage",
                                "text": "It is 20 degrees Celsius.",
                                "phase": "final_answer",
                            }
                        ],
                    }
                },
            }
        )
