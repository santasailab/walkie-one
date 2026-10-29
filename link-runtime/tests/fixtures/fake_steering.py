"""Controlled provider process exercising the production JSONL adapters."""

import json
import os
import sys


def send(value):
    print(json.dumps(value), flush=True)


provider = sys.argv[1]
mode = os.environ.get("STEERING_FIXTURE_MODE", "normal")
count = int(os.environ.get("STEERING_FIXTURE_INPUTS", "1"))
inputs = []
approval = os.environ.get("STEERING_FIXTURE_APPROVAL") == "1"
if provider == "claude" and os.environ.get("WALKIEONE_CLAUDE_TOOL_DECISION") == "allow":
    inputs = ["resumed-input"]
for line in sys.stdin:
    event = json.loads(line)
    record = os.environ.get("STEERING_FIXTURE_RECORD")
    if record:
        with open(record, "a") as handle:
            handle.write(json.dumps(event) + "\n")
    if provider == "codex":
        method = event.get("method")
        if method == "initialize":
            send({"id": event["id"], "result": {}})
        elif method in {"thread/start", "thread/resume"}:
            send({"id": event["id"], "result": {"thread": {"id": "thread-1"}}})
        elif method == "turn/start":
            send({"id": event["id"], "result": {"turn": {"id": "turn-1"}}})
            if approval:
                send(
                    {
                        "id": 700,
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "itemId": "command-1",
                            "command": "sleep 4",
                            "reason": "Run the controlled task",
                        },
                    }
                )
        elif method == "turn/steer":
            assert event["params"]["threadId"] == "thread-1"
            assert event["params"]["expectedTurnId"] == "turn-1"
            inputs.append(event["params"]["input"][0]["text"])
            completed = {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [{"type": "agentMessage", "text": inputs[-1]}],
                    }
                },
            }
            if mode == "terminal_first":
                send(completed)
            if mode == "crash":
                raise SystemExit(1)
            if mode != "no_ack":
                send({"id": event["id"], "result": {"turnId": "turn-1"}})
            if len(inputs) == count and mode != "terminal_first":
                send(completed)
    elif event.get("type") == "user":
        if not inputs:
            original = event["uuid"]
            inputs.append(original)
            if approval:
                send(
                    {
                        "type": "result",
                        "session_id": "session-1",
                        "stop_reason": "tool_deferred",
                        "deferred_tool_use": {
                            "id": "tool-1",
                            "name": "Bash",
                            "input": {"command": "sleep 4"},
                        },
                    }
                )
            continue
        assert event["priority"] == "now"
        inputs.append(event["uuid"])
        # The old turn can end successfully after a tool was interrupted.
        send(
            {
                "type": "result",
                "session_id": "session-1",
                "is_error": False,
                "terminal_reason": "aborted_tools",
                "user_message_uuid": inputs[-2],
                "user_message_uuids": [inputs[-2]],
                "result": "OLD RESULT",
            }
        )
        if mode != "result_only":
            send(event)
        if len(inputs) == count + 1:
            send(
                {
                    "type": "result",
                    "session_id": "session-1",
                    "user_message_uuid": event["uuid"],
                    "user_message_uuids": inputs[1:] if mode != "missing_uuid" else [],
                    "result": event["message"]["content"][0]["text"],
                }
            )
