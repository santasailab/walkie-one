#!/usr/bin/env python3
"""Bounded health checks for the detachable WalkieOne Link runtime.

WalkieOne owns transport and coding-Agent connectivity. It does not prepare
applications, request broad macOS permissions, install speech models, or
change power settings. Provider- and task-specific repair stays with the Agent
and is surfaced here only when the Agent has already reported a problem.
"""

import json
import os
import signal
import subprocess
import sys
import threading

SH_OUTPUT_MAX = 64 * 1024


def _sh(args, timeout=15):
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as e:
        return 1, "", str(e)

    captured = {"stdout": [], "stderr": []}

    def drain(name, pipe):
        kept = 0
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    break
                if kept < SH_OUTPUT_MAX:
                    piece = chunk[:SH_OUTPUT_MAX - kept]
                    captured[name].append(piece)
                    kept += len(piece)
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    readers = [
        threading.Thread(
            target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(
            target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    for reader in readers:
        reader.join(timeout=0.1)
    if any(reader.is_alive() for reader in readers):
        # A CLI may launch a helper that inherits its pipe and then exit first.
        # End that private process group so the drain cannot outlive this
        # bounded check or retain file descriptors indefinitely.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        for reader in readers:
            reader.join(timeout=2)
    stdout = "".join(captured["stdout"]).strip()
    stderr = "".join(captured["stderr"]).strip()
    if timed_out:
        message = f"timed out after {timeout} seconds"
        stderr = (stderr + "  " + message).strip()[:SH_OUTPUT_MAX]
    return process.returncode if process.returncode is not None else 1, stdout, stderr


def check_agent():
    """The brain. Relay has none of its own any more - if this can't run, the
    device is a speaker with nothing to say."""
    import agent

    if agent.BACKEND not in agent.BACKENDS:
        return False, f"RELAY_AGENT={agent.BACKEND} is not a backend I know", (
            "unset RELAY_AGENT, or set it to one of: "
            + ", ".join(agent.BACKENDS))
    path = agent.executable(agent.BACKEND)
    if not path:
        # agent.py picks whichever backend is actually on PATH, so reaching here
        # without RELAY_AGENT set means no supported runtime is installed. The
        # native app owns the friendly install choice; the terminal path names
        # all supported commands directly.
        if os.environ.get("WALKIEONE_RESOURCES"):
            fix = ("Open WalkieOne Settings, choose an Agent provider, then "
                   "use its Download button.")
        elif os.environ.get("RELAY_AGENT"):
            fix = (f"RELAY_AGENT={agent.BACKEND} is set but not installed. "
                   "Install it, or unset RELAY_AGENT to use whichever is here")
        else:
            fix = "Install codex or claude, then run this check again"
        return False, f"{agent.BACKEND} is not installed", fix
    # Installed is not the same as working: codex on this machine resolves on
    # PATH but its vendored binary was never fetched, so it exits ENOENT. A
    # check that only looked at PATH would have called that ready.
    code, out, err = _sh([path, "--version"], timeout=30)
    if code != 0:
        return False, f"{agent.BACKEND} is installed but won't run", (
            f"{(err or out).strip()[:160]}  ->  try reinstalling it")
    if agent.BACKEND == "codex":
        signed, _, login_err = _sh([path, "login", "status"], timeout=30)
        if signed != 0:
            return False, "OpenAI account is not connected", (
                "Open WalkieOne Settings and choose Sign in."
                + (" " + login_err.strip()[:100] if login_err else ""))
    elif agent.BACKEND == "claude":
        signed, _, login_err = _sh([path, "auth", "status"], timeout=30)
        if signed != 0:
            return False, "Anthropic account is not connected", (
                "Open WalkieOne Settings and choose Sign in."
                + (" " + login_err.strip()[:100] if login_err else ""))
    version = out.splitlines()[0][:40]
    label = version if version.lower().startswith(agent.BACKEND) else (
        f"{agent.BACKEND} {version}")
    return True, label, None


def check_permissions_pending():
    """Report task-specific permission trouble already observed by the Agent."""
    pending = sorted(blocked_apps())
    if not pending:
        return True, "nothing has been blocked", None
    return None, "earlier access issues: " + ", ".join(pending), (
        "Open WalkieOne Settings > Mac access to check current permissions, "
        "then retry the affected task. These reports are not live grant state.")


def check_websockets():
    """The outbound Link transport dependency."""
    try:
        import websockets
        return True, f"websockets {websockets.__version__}", None
    except ImportError:
        return False, "websockets not installed", (
            "pip3 install -r requirements.txt")


CHECKS = [
    ("websockets", check_websockets, True),
    ("Agent", check_agent, True),
    ("Agent permissions", check_permissions_pending, False),
]


BLOCKED = os.path.expanduser("~/.relay/needs-permission")
KNOWN_PERMISSION_APPS = {
    "Accessibility", "Calendar", "Contacts", "Google Chrome", "Mail",
    "Messages", "Music", "Notes", "Photos", "Reminders", "Safari",
    "Slack", "System Events",
    "Screen Recording", "Full Disk Access", "Files and Folders", "Mac access",
}


def blocked_apps():
    try:
        with open(BLOCKED, encoding="utf-8") as handle:
            names = {line.strip() for line in handle.read(4096).splitlines() if line.strip()}
    except OSError:
        return set()
    known = {name.lower(): name for name in KNOWN_PERMISSION_APPS}
    return {known[n.lower()] for n in names if n.lower() in known}


def run():
    results, blocking = [], []
    for label, fn, required in CHECKS:
        try:
            ok, detail, fix = fn()
        except Exception as e:
            ok, detail, fix = None, f"check failed: {e}", None
        results.append((label, ok, detail, fix, required))
        if ok is False and required:
            blocking.append(label)
    return results, blocking


def as_json():
    """The same checks, for a UI instead of a terminal.

    WalkieOne.app reads this rather than parsing the printout above, because the
    printout is prose for a human and gets reworded. `ok` stays three-valued -
    true, false, and null for "could not tell" - since an app that renders
    unknown as failure sends people to fix a setting that was never broken.

    Runs without the daemon so the native app can diagnose a failed Link.
    """
    results, blocking = run()
    return json.dumps({
        "checks": [{"label": l, "ok": ok, "detail": d, "fix": f,
                    "required": req} for l, ok, d, f, req in results],
        "blocking": blocking,
        # The apps the agent got stuck on, so the app can ask for exactly those.
        "needs_permission": sorted(blocked_apps()),
    }, indent=2)


def demo():
    # Every check must return a 3-tuple and never raise, whatever the machine
    # looks like - a preflight that crashes tells the user nothing.
    for label, fn, _ in CHECKS:
        out = fn()
        assert isinstance(out, tuple) and len(out) == 3, (label, out)
        ok, detail, fix = out
        assert ok in (True, False, None), (label, ok)
        assert isinstance(detail, str) and detail, label
        # Anything reported as broken must say how to fix it.
        if ok is False:
            assert fix, f"{label} failed with no remedy"

    # The app's contract. Every check must survive a round trip, and a failure
    # must carry its remedy across - a UI that shows a red row with no next step
    # is the support ticket this file exists to prevent.
    got = json.loads(as_json())
    assert len(got["checks"]) == len(CHECKS)
    for c in got["checks"]:
        assert c["ok"] in (True, False, None), c
        assert c["detail"], c
        if c["ok"] is False:
            assert c["fix"], c
    assert all(b in [c["label"] for c in got["checks"]] for b in got["blocking"])
    print("ok")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
        sys.exit(0)
    if "--json" in sys.argv:
        print(as_json())
        sys.exit(0)
    results, blocking = run()
    mark = {True: "OK  ", False: "FAIL", None: "??  "}
    for label, ok, detail, fix, required in results:
        print(f"{mark[ok]} {label:<20} {detail}"
              + ("" if required else "  (optional)"))
        if ok is False and ("--fix" in sys.argv or required):
            print(f"     -> {fix}")

    print()
    if blocking:
        print("NOT READY: " + ", ".join(blocking))
        sys.exit(1)
    degraded = [l for l, ok, _, _, req in results if ok is False and not req]
    print("Ready." + (f" Degraded: {', '.join(degraded)}." if degraded else ""))
