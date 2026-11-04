"""Host-level outcomes for turns that cannot finish unattended.

The coding agent can explain a macOS permission failure, but it cannot be the
only thing responsible for noticing one: the process may be sitting behind the
very dialog it needs to explain.  This module is the small seam between either
provider protocol and the spoken product response.
"""

import json
import os
import re
import time


BLOCKED = os.path.expanduser("~/.relay/needs-permission")

def permission_name(value):
    """Keep a bounded display name, never raw error text, paths or commands."""
    if not isinstance(value, str):
        return "Mac access"
    if not re.fullmatch(r"[A-Z][A-Za-z0-9 .()_-]{0,47}", value):
        return "Mac access"
    return value.strip(" ._-") or "Mac access"


def permission_reply(app):
    app = permission_name(app)
    reason = {
        "Accessibility": "I need Accessibility permission to click and type on your Mac.",
        "Screen Recording": "I need Screen Recording permission to see your Mac's screen.",
        "Full Disk Access": "I need Full Disk Access to read the protected files for this task.",
        "Files and Folders": "I need permission to access the folder for this task.",
        "Contacts": "I need permission to access your contacts.",
        "Mac access": "This task needs permission on your Mac, but I couldn't identify which permission.",
    }.get(app, f"I need permission to use {app} on your Mac.")
    return (reason + " Open System Settings on your Mac, then Privacy & Security, "
            "to allow access for the app or tool making the request. Then ask me again, or ask me to do something else.")


def permission_repair(app):
    app = permission_name(app)
    if app == "Mac access":
        return ("The task couldn't identify which permission it needs. "
                "Ask again while you're at your Mac.")
    if app in {"Accessibility", "Screen Recording", "Full Disk Access",
               "Files and Folders", "Contacts"}:
        return f"Allow {app}, then ask again."
    return f"Allow access to {app}, then ask again."


PERMISSION_REPLY = permission_reply("Mac access")

# Deliberately specific. A bare "operation not permitted" can be an ordinary
# Unix file error and must not turn into a false macOS-consent diagnosis.
_PERMISSION = re.compile(
    r"(?:not authorized to send apple events|not allowed assistive access|"
    r"not trusted for accessibility|appleevent.*(?:denied|not permitted)|"
    r"screen(?: and system audio)? recording.*(?:denied|permission)|"
    r"cgrequestscreencaptureaccess|tcc(?:access)?request|(?:error\s*)?-1743)",
    re.IGNORECASE,
)
_PROTECTED_DATA = re.compile(
    r"(?:operation not permitted|permission denied).{0,160}"
    r"(?:Library/(?:Messages|Mail|Calendars|Reminders|AddressBook)|chat\.db)|"
    r"(?:Library/(?:Messages|Mail|Calendars|Reminders|AddressBook)|chat\.db)"
    r".{0,160}(?:operation not permitted|permission denied)",
    re.IGNORECASE,
)
_APP = re.compile(
    r"(?:application|app|control|events\s+to)\s+[\"']?"
    r"([A-Z][A-Za-z0-9 ._-]{1,40}?)[\"']?(?:[.:(]|$)",
)


def flatten(value):
    """Render a provider event for classification without knowing its schema."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def permission_app(value):
    """Return the blocked app name, "Mac access", or None when not TCC-related."""
    text = flatten(value)
    if not (_PERMISSION.search(text) or _PROTECTED_DATA.search(text)):
        return None
    if re.search(r"assistive access|accessibility", text, re.IGNORECASE):
        return "Accessibility"
    if re.search(r"screen.*recording|cgrequestscreencaptureaccess", text,
                 re.IGNORECASE):
        return "Screen Recording"
    if _PROTECTED_DATA.search(text):
        return "Full Disk Access"
    match = _APP.search(text)
    if not match:
        return "Mac access"
    name = " ".join(match.group(1).split()).strip(" ._-")
    return permission_name(name)


def event_permission(event):
    """Inspect tool outcomes/errors, never model text or approval requests."""
    if not isinstance(event, dict):
        return None
    if "id" in event and event.get("method"):
        return None
    if event.get("error"):
        return permission_app(event["error"])
    method = event.get("method")
    params = event.get("params") or {}
    if not isinstance(params, dict):
        return None
    if method == "item/completed":
        item = params.get("item") or {}
        if isinstance(item, dict):
            kind = item.get("type")
            failed = item.get("status") == "failed" or bool(item.get("error"))
            if kind == "commandExecution":
                failed |= item.get("exitCode") not in (None, 0)
            if kind == "dynamicToolCall":
                failed |= item.get("success") is False
            result = item.get("result")
            if kind == "mcpToolCall" and isinstance(result, dict):
                failed |= result.get("isError") is True
            if failed and kind in {"commandExecution", "mcpToolCall", "dynamicToolCall"}:
                return permission_app({key: item.get(key) for key in (
                    "aggregatedOutput", "error", "result", "contentItems")})
    if method == "session/update":
        update = params.get("update") or {}
        if (isinstance(update, dict)
                and update.get("sessionUpdate") == "tool_call_update"
                and update.get("status") == "failed"):
            return permission_app(update.get("content"))
    if event.get("type") == "user":
        message = event.get("message") or {}
        content = message.get("content", []) if isinstance(message, dict) else []
        if isinstance(content, list):
            for item in content:
                if (isinstance(item, dict) and item.get("type") == "tool_result"
                        and item.get("is_error")):
                    if app := permission_app(item.get("content")):
                        return app
    if event.get("type") == "result" and event.get("is_error"):
        return permission_app(event.get("result"))
    return None


def record(app, path=BLOCKED):
    """Persist one blocker for the app UI; never let bookkeeping break a turn."""
    if not app:
        return
    app = permission_name(app)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = set()
        try:
            existing = {line.strip() for line in open(path, encoding="utf-8")
                        if line.strip()}
        except OSError:
            pass
        existing.add(app)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(sorted(existing)) + "\n")
    except OSError:
        pass


def demo():
    assert permission_app({
        "output": 'execution error: Not authorized to send Apple events to Notes. (-1743)'
    }) == "Notes"
    assert permission_app("System Events got an error: not allowed assistive access") \
        == "Accessibility"
    assert permission_app("rm: private.txt: Operation not permitted") is None
    assert permission_app(
        "open ~/Library/Messages/chat.db: Operation not permitted") == "Full Disk Access"

    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "blocked")
    record("Notes", path)
    record("Calendar", path)
    record("Notes", path)
    assert open(path, encoding="utf-8").read().splitlines() == ["Calendar", "Notes"]
    print("ok")


if __name__ == "__main__" and "--demo" in os.sys.argv:
    started = time.monotonic()
    demo()
    assert time.monotonic() - started < 1
