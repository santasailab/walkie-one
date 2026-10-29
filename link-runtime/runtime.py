#!/usr/bin/env python3
"""Frozen entry point for the helper shipped inside WalkieOne.app.

The source checkout still runs link.py directly. The app builds this module
with PyInstaller so customers receive Python, websockets, and the Relay engine
as one signed helper directory and never install developer tools.
"""

import asyncio
import json
import os
import signal
import sys
import threading

# The app sends SIGTERM once and never SIGKILL. A cleanup that hangs past this
# would leave Reconnect waiting and port 8765 held by an orphaned helper.
STOP_DEADLINE_SECONDS = 10


async def run_service(serve):
    """Process.terminate() must unwind the helper just like a normal exit."""
    loop = asyncio.get_running_loop()
    service = asyncio.current_task()
    stopping = False

    def stop():
        nonlocal stopping
        # A second termination request must not interrupt process cleanup;
        # the deadline bounds it instead.
        if not stopping:
            stopping = True
            deadline = threading.Timer(STOP_DEADLINE_SECONDS, os._exit, (1,))
            deadline.daemon = True
            deadline.start()
            service.cancel()

    loop.add_signal_handler(signal.SIGTERM, stop)
    try:
        await serve()
    except asyncio.CancelledError:
        pass
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def _bootstrap():
    """Read secrets once from an anonymous pipe owned by the parent app.

    Environment variables are visible to other same-user processes on macOS.
    WalkieOne therefore sends its local control token and pairing credential
    over stdin, closes the pipe, and only then starts the server.
    """
    if os.environ.pop("WALKIEONE_BOOTSTRAP_STDIN", "") != "1":
        return
    raw = sys.stdin.readline()
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        raise SystemExit("WalkieOne supplied invalid runtime credentials")
    allowed = {"LINK_AUTH", "RELAY_CLOUD_URL", "RELAY_TOKEN"}
    for key in allowed:
        os.environ.pop(key, None)
    for key, value in values.items():
        if key in allowed and isinstance(value, str) and value:
            os.environ[key] = value


def main():
    # Keep direct developer invocations responsive. The native app discards
    # stdout/stderr; ordinary runtime output is not a persistent speech log.
    sys.stdout.reconfigure(line_buffering=True)
    if "--claude-permission-hook" in sys.argv:
        import claude_permission_hook

        raise SystemExit(claude_permission_hook.main())
    _bootstrap()
    if "--preflight-json" in sys.argv:
        import preflight
        print(preflight.as_json())
        return
    if "--preflight-demo" in sys.argv:
        import preflight
        preflight.demo()
        return
    if "--agent-blocker-demo" in sys.argv:
        import agent
        agent.turn_blocker_demo()
        return

    import link
    if "--demo" in sys.argv:
        link.demo()
    else:
        try:
            asyncio.run(run_service(link.serve))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
