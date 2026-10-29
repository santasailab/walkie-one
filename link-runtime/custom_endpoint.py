"""Explicit owner-installed command adapter; no shell or remote plugin loading."""

import json
import os
import signal
import stat
import subprocess
import threading
from pathlib import Path

from agent_endpoint import RuntimeProtocolError, TurnResponse, _ProcessAgentEndpoint


def load_command(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Command configuration must be private")
        data = file.read(4097)
    if len(data) > 4096:
        raise ValueError("Command configuration is too large")
    argv = json.loads(data)
    if (
        not isinstance(argv, list)
        or not 1 <= len(argv) <= 16
        or any(
            not isinstance(arg, str) or not arg or len(arg) > 1024 or "\x00" in arg
            for arg in argv
        )
        or not Path(argv[0]).is_absolute()
        or not os.access(argv[0], os.X_OK)
    ):
        raise ValueError("Choose an executable absolute path and bounded arguments")
    return argv


def custom_executable():
    try:
        return load_command(os.environ.get("WALKIEONE_CUSTOM_COMMAND", ""))[0]
    except (OSError, ValueError, TypeError):
        return None


class CustomAgentEndpoint(_ProcessAgentEndpoint):
    provider = "custom"


class CommandRunner:
    timeout = 180

    def __init__(self, scope):
        self._lock = threading.Lock()
        self._process = None
        self._cancelled = False

    def cancel(self):
        with self._lock:
            self._cancelled = True
            process = self._process
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # macOS may report EPERM for a group whose leader has
                    # already exited. A live process must still be stopped.
                    if process.poll() is None:
                        process.kill()
        return True

    def run(self, prompt, session_id, reply_to):
        import agent

        payload = (
            json.dumps(
                {
                    "version": 1,
                    "prompt": prompt,
                    "session_id": session_id,
                    "scope": reply_to,
                }
            ).encode()
            + b"\n"
        )
        if len(payload) > 128 * 1024:
            raise RuntimeProtocolError("Custom assistant input exceeds its bound")
        argv = load_command(os.environ.get("WALKIEONE_CUSTOM_COMMAND", ""))
        captured = bytearray()
        overflow = threading.Event()
        with self._lock:
            if self._cancelled:
                raise RuntimeProtocolError("Custom assistant was cancelled")
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=agent.WORKDIR,
                env=agent.child_env(),
                start_new_session=True,
            )
            process = self._process

        def drain():
            while chunk := process.stdout.read(4096):
                if len(captured) + len(chunk) > 16 * 1024:
                    overflow.set()
                    self.cancel()
                    break
                captured.extend(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()

        def write_input():
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except (OSError, ValueError):
                pass

        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()
        try:
            # communicate's stdout buffer is unbounded; drain separately instead.
            process.wait(timeout=self.timeout)
            reader.join(timeout=2)
            writer.join(timeout=2)
            if reader.is_alive() or overflow.is_set() or process.returncode != 0:
                raise RuntimeProtocolError(
                    "Custom assistant did not return a bounded result; inspect it locally"
                )
            result = json.loads(captured)
            if (
                not isinstance(result, dict)
                or set(result) - {"text", "session_id", "needs_answer"}
                or not isinstance(result.get("text"), str)
                or not 1 <= len(result["text"].strip()) <= 4000
                or result.get("session_id") is not None
                and (
                    not isinstance(result["session_id"], str)
                    or len(result["session_id"]) > 256
                )
                or not isinstance(result.get("needs_answer", False), bool)
            ):
                raise ValueError("invalid result")
            return TurnResponse(
                result["text"],
                result.get("session_id"),
                result.get("needs_answer", False),
            )
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeProtocolError(
                "Custom assistant did not complete; inspect it locally"
            ) from error
        finally:
            self.cancel()
            process.wait(timeout=5)
            reader.join(timeout=2)
            writer.join(timeout=2)
            process.stdout.close()
            with self._lock:
                self._process = None
