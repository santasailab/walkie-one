"""Bound writes to a turn-owned, isolated provider process group."""

import asyncio
import os
import signal
import threading

WRITE_TIMEOUT_SECONDS = 2
REAP_TIMEOUT_SECONDS = 5


def kill_process_group(process):
    """Kill an isolated subprocess and descendants, even after its leader exits."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin can report EPERM for a group whose last members have exited
        # but have not yet been reaped. Still kill a live leader if possible.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass


async def reap_process_group(process):
    """Kill the group, then drain its pipes for a bounded time.

    A descendant that started its own session (`ssh -f`, a daemon) survives
    killpg and can hold stdout open forever, so communicate() alone never
    returns. After the deadline, close our pipe ends and move on.
    """
    kill_process_group(process)
    try:
        await asyncio.wait_for(process.communicate(), REAP_TIMEOUT_SECONDS)
    except (TimeoutError, asyncio.TimeoutError):
        # ponytail: asyncio exposes no public close for a Process's pipes.
        process._transport.close()


def write_with_deadline(process, lock, data):
    """A stopped reader must not hold the turn or its cancellation indefinitely."""
    if not lock.acquire(timeout=WRITE_TIMEOUT_SECONDS):
        raise TimeoutError("The coding agent stopped reading requests")
    expired = threading.Event()

    def abort_write():
        expired.set()
        # Callers create a new session for each provider. Killing its group
        # closes every inherited read end, including a tool behind a dialog.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            if process.poll() is None:
                process.kill()

    timer = threading.Timer(WRITE_TIMEOUT_SECONDS, abort_write)
    timer.daemon = True
    timer.start()
    try:
        process.stdin.write(data)
        process.stdin.flush()
    except OSError:
        if not expired.is_set():
            raise
    finally:
        timer.cancel()
        lock.release()
    if expired.is_set():
        raise TimeoutError("The coding agent stopped reading requests")
