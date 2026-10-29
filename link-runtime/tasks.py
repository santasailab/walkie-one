"""Background Tasks: Agent-written commands that WalkieOne starts on an interval.

Link runs inside WalkieOne, so macOS attributes a task's privacy requests -
controlling Chrome, Accessibility - to WalkieOne, and the User grants each
target app once instead of once per watcher program. WalkieOne only launches:
the task decides what is worth saying and calls walkieone-send itself. What
WalkieOne adds is the one thing a task cannot do for itself when macOS blocks
it: tell the User, by voice, what to click next time they are at the Mac.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from process_io import kill_process_group, reap_process_group

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MIN_EVERY = 60
MAX_EVERY = 7 * 24 * 60 * 60
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 60 * 60
MAX_TASKS = 20
MAX_RUNNING = 4
MAX_ARGS = 32
MAX_ARG_CHARS = 1_024
LOG_MAX = 64 * 1_024
LOG_RUN_MAX = 10 * 1_024 * 1_024
OUTPUT_TAIL = 2_000
# Timeouts and ordinary failures can be transient; a permission refusal is not.
ALERT_AFTER = 3

_APPLE_EVENTS = re.compile(
    r"Not authori[sz]ed to send Apple events to (.+?)\.?\s*\(-1743\)"
)
_ACCESSIBILITY = re.compile(r"assistive access|\(-25211\)|\(-1719\)")


def default_tasks_path() -> Path:
    # Outside the Agent workspace on purpose: a sandboxed Agent may write its
    # workspace freely, and a task later runs outside that sandbox.
    configured = os.environ.get("WALKIEONE_TASKS", "")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".relay" / "tasks"


def diagnose(output: str, timed_out: bool) -> tuple[str, str | None]:
    """Why a run failed, in the terms the User can act on."""
    # A script that bounds its own AppleScript call reports the same wait for
    # an unanswered macOS prompt as an error rather than being killed by us.
    if timed_out or "(-1712)" in output or re.search(r"osascript.*timed out", output):
        return "timeout", None
    match = _APPLE_EVENTS.search(output)
    if match:
        return "automation", match.group(1).strip().strip('“”"')
    if "(-1743)" in output:
        return "automation", None
    if _ACCESSIBILITY.search(output):
        return "accessibility", None
    return "failed", None


def alert_text(name: str, diagnosis: str, app: str | None) -> str:
    label = name.replace("-", " ")
    if diagnosis == "automation":
        target = app or "another app"
        return (
            f"Your {label} background task needs permission to control {target}. "
            "Next time you're at your Mac, open System Settings, then Privacy and "
            f"Security, then Automation, and turn on {target} under WalkieOne."
        )
    if diagnosis == "accessibility":
        return (
            f"Your {label} background task needs Accessibility permission. Next "
            "time you're at your Mac, open System Settings, then Privacy and "
            "Security, then Accessibility, and turn on WalkieOne."
        )
    if diagnosis == "timeout":
        return (
            f"Your {label} background task keeps timing out. There may be a "
            "permission box waiting on your Mac. Next time you're there, click "
            "Allow if you see one, or ask me to check the task."
        )
    return f"Your {label} background task keeps failing. Ask me to check it."


class TaskError(ValueError):
    """A request WalkieOne refuses, carrying the reason the caller sees."""

    def __init__(self, reason: str, status: int = 400) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _one(fields: dict[str, list[str]], key: str, default: str | None = None) -> str:
    values = fields.get(key)
    if not values:
        if default is None:
            raise TaskError(f"missing_{key}")
        return default
    if len(values) != 1:
        raise TaskError(f"invalid_{key}")
    return values[0].strip()


def _seconds(value: str, low: int, high: int, key: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise TaskError(f"invalid_{key}") from error
    if not low <= number <= high:
        raise TaskError(f"invalid_{key}")
    return number


class TaskRunner:
    """Owns task definitions in one directory and runs them while Link runs."""

    def __init__(
        self,
        path: str | Path,
        send: Callable[[object], Awaitable[object]],
        env: Callable[[str], dict[str, str]],
        workdir: str | Path,
        *,
        request: Callable[[str, str], object] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.send = send
        self.env = env
        self.workdir = Path(workdir)
        # Builds the proactive request; injected so this module does not import
        # the socket server it is served by.
        self.request = request
        self.clock = clock
        self.tasks: dict[str, dict] = {}
        self.state: dict[str, dict] = {}
        self._runs: dict[str, asyncio.Task] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    # --- storage -----------------------------------------------------------

    def _prepare(self) -> None:
        self.path.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError("task directory is not owned by this user")
        self.path.chmod(0o700)

    def _write(self, target: Path, value: dict) -> None:
        temporary = target.with_suffix(".partial")
        temporary.write_text(json.dumps(value, separators=(",", ":")) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)

    def load(self) -> None:
        self._prepare()
        for file in sorted(self.path.glob("*.json")):
            if file.name.endswith(".state.json"):
                continue
            try:
                task = json.loads(file.read_text())
                # Preserve older valid tasks at the new minimum frequency.
                if (
                    isinstance(task, dict)
                    and type(task.get("every")) is int
                    and 15 <= task["every"] < MIN_EVERY
                ):
                    task["every"] = MIN_EVERY
                self._validate(task)
            except (OSError, ValueError):
                continue
            self.tasks[task["name"]] = task
            self.state[task["name"]] = {"due": 0.0, "failures": 0, "alerted": False}

    @staticmethod
    def _validate(task: dict) -> None:
        if (
            not isinstance(task, dict)
            or task.get("v") != 1
            or not isinstance(task.get("name"), str)
            or not NAME.match(task["name"])
            or not isinstance(task.get("every"), int)
            or not MIN_EVERY <= task["every"] <= MAX_EVERY
            or not isinstance(task.get("timeout"), int)
            or not 1 <= task["timeout"] <= MAX_TIMEOUT
            or not isinstance(task.get("command"), list)
            or not 1 <= len(task["command"]) <= MAX_ARGS
            or not all(
                isinstance(arg, str)
                and 0 < len(arg) <= MAX_ARG_CHARS
                and "\0" not in arg
                for arg in task["command"]
            )
        ):
            raise ValueError("invalid task")

    # --- operations served over the protected socket -----------------------

    async def handle(self, op: str, fields: dict[str, list[str]]) -> tuple[int, dict]:
        try:
            if op == "add":
                return 200, self.add(fields)
            if op == "remove":
                return 200, self.remove(_one(fields, "name"))
            if op == "run":
                return 200, await self.run_now(_one(fields, "name"))
            if op == "list":
                return 200, {"v": 1, "status": "ok", "tasks": self.list()}
            raise TaskError("unsupported_operation")
        except TaskError as error:
            return error.status, {"v": 1, "status": "rejected", "reason": error.reason}

    def add(self, fields: dict[str, list[str]]) -> dict:
        name = _one(fields, "name")
        if not NAME.match(name):
            raise TaskError("invalid_name")
        task = {
            "v": 1,
            "name": name,
            "every": _seconds(_one(fields, "every"), MIN_EVERY, MAX_EVERY, "every"),
            "timeout": _seconds(
                _one(fields, "timeout", str(DEFAULT_TIMEOUT)), 1, MAX_TIMEOUT, "timeout"
            ),
            "command": fields.get("arg", []),
        }
        try:
            self._validate(task)
        except ValueError as error:
            raise TaskError("invalid_command") from error
        if name not in self.tasks and len(self.tasks) >= MAX_TASKS:
            raise TaskError("too_many_tasks")
        self._prepare()
        self._write(self.path / f"{name}.json", task)
        self.tasks[name] = task
        # A new or changed definition runs at once, so its first result - and
        # any macOS prompt it causes - arrives while the User is setting it up.
        self.state[name] = {"due": 0.0, "failures": 0, "alerted": False}
        return {"v": 1, "status": "saved", "name": name}

    def remove(self, name: str) -> dict:
        if name not in self.tasks:
            raise TaskError("not_found", 404)
        del self.tasks[name]
        self.state.pop(name, None)
        self._kill(name)
        for suffix in (".json", ".state.json", ".log"):
            (self.path / f"{name}{suffix}").unlink(missing_ok=True)
        return {"v": 1, "status": "removed", "name": name}

    def list(self) -> list[dict]:
        listed = []
        for name, task in sorted(self.tasks.items()):
            state = self.state.get(name, {})
            listed.append(
                {
                    "name": name,
                    "every": task["every"],
                    "command": task["command"],
                    "running": name in self._runs,
                    "last_run": state.get("last_run"),
                    "ok": state.get("ok"),
                    "failures": state.get("failures", 0),
                    "diagnosis": state.get("diagnosis"),
                    "app": state.get("app"),
                }
            )
        return listed

    async def run_now(self, name: str) -> dict:
        if name not in self.tasks:
            raise TaskError("not_found", 404)
        running = self._runs.get(name)
        if running is not None:
            await asyncio.shield(running)
        self._start(name, scheduled=False)
        return await asyncio.shield(self._runs[name])

    # --- scheduling --------------------------------------------------------

    def _start(self, name: str, *, scheduled: bool) -> None:
        run = asyncio.create_task(self._execute(name, scheduled))
        self._runs[name] = run
        run.add_done_callback(lambda _done, name=name: self._runs.pop(name, None))

    def tick(self) -> None:
        now = self.clock()
        for name, state in list(self.state.items()):
            if len(self._runs) >= MAX_RUNNING:
                return
            if name not in self._runs and state["due"] <= now:
                self._start(name, scheduled=True)

    async def run(self, interval: float = 1.0) -> None:
        try:
            while True:
                try:
                    self.tick()
                except Exception:  # noqa: BLE001, S110 - a task cannot stop Link
                    pass
                await asyncio.sleep(interval)
        finally:
            await self.close()

    async def close(self) -> None:
        runs = list(self._runs.values())
        for run in runs:
            run.cancel()
        if runs:
            await asyncio.gather(*runs, return_exceptions=True)

    def _kill(self, name: str) -> None:
        process = self._processes.get(name)
        if process is not None:
            kill_process_group(process)

    # --- one run -----------------------------------------------------------

    def _log(self, name: str):
        log = self.path / f"{name}.log"
        self._trim_log(log)
        handle = open(log, "ab")  # noqa: SIM115 - handed to the child process
        os.chmod(log, 0o600)
        return handle

    @staticmethod
    def _trim_log(log: Path) -> None:
        if log.exists() and log.stat().st_size > LOG_MAX:
            with log.open("r+b") as handle:
                handle.seek(-LOG_MAX, os.SEEK_END)
                tail = handle.read(LOG_MAX)
                handle.seek(0)
                handle.write(tail)
                handle.truncate()

    async def _execute(self, name: str, scheduled: bool) -> dict:
        task, state = self.tasks.get(name), self.state.get(name)
        if task is None or state is None:
            return {"v": 1, "status": "rejected", "reason": "not_found"}
        started = self.clock()
        state["due"] = started + task["every"]
        timed_out = False
        output_limited = False
        exit_code: int | None = None
        offset = 0
        log = self._log(name)
        try:
            log.write(
                f"--- {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(started))}"
                f" {'scheduled' if scheduled else 'manual'} run\n".encode()
            )
            log.flush()
            offset = log.tell()
            try:
                process = await asyncio.create_subprocess_exec(
                    *task["command"],
                    cwd=self.workdir,
                    env=self.env(name),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    # Its own group, so a timeout also stops what it spawned.
                    start_new_session=True,
                )
            except OSError as error:
                log.write(f"could not start: {error.strerror}\n".encode())
                log.flush()
            else:
                self._processes[name] = process

                async def capture():
                    nonlocal output_limited
                    while chunk := await process.stdout.read(64 * 1024):
                        remaining = max(0, LOG_RUN_MAX - log.tell())
                        log.write(chunk[:remaining])
                        log.flush()
                        if len(chunk) > remaining and not output_limited:
                            output_limited = True
                            self._kill(name)
                    return await process.wait()

                try:
                    exit_code = await asyncio.wait_for(capture(), task["timeout"])
                except (TimeoutError, asyncio.TimeoutError):
                    timed_out = True
                finally:
                    # Even an exited leader can leave child tools alive or
                    # holding stdout open. Cancellation follows this same path.
                    try:
                        await reap_process_group(process)
                    finally:
                        self._processes.pop(name, None)
        finally:
            log.close()
            output = self._tail(name, offset)
            self._trim_log(self.path / f"{name}.log")
        ok = exit_code == 0 and not timed_out and not output_limited
        diagnosis, app = (
            ("output_limit", None)
            if output_limited
            else (None, None)
            if ok
            else diagnose(output, timed_out)
        )
        if name in self.tasks:
            await self._record(name, scheduled, started, ok, diagnosis, app)
        return {
            "v": 1,
            "status": "ok" if ok else "failed",
            "name": name,
            "exit": exit_code,
            "diagnosis": diagnosis,
            "app": app,
            "output": output[-OUTPUT_TAIL:],
        }

    def _tail(self, name: str, offset: int) -> str:
        try:
            with open(self.path / f"{name}.log", "rb") as handle:
                handle.seek(max(offset, handle.seek(0, os.SEEK_END) - OUTPUT_TAIL * 2))
                return handle.read().decode("utf-8", "replace")
        except OSError:
            return ""

    async def _record(self, name, scheduled, started, ok, diagnosis, app) -> None:
        state = self.state[name]
        state.update(last_run=int(started), ok=ok, diagnosis=diagnosis, app=app)
        if ok:
            state.update(failures=0, alerted=False)
        else:
            if not state["failures"]:
                state["streak"] = int(started)
            state["failures"] += 1
            permanent = diagnosis in ("automation", "accessibility")
            # A manual run is the Agent watching; it reports the result itself.
            if (
                scheduled
                and not state["alerted"]
                and (permanent or state["failures"] >= ALERT_AFTER)
                and self.request is not None
            ):
                request = self.request(
                    alert_text(name, diagnosis, app),
                    f"task-{name}-{diagnosis}-{state['streak']}",
                )
                try:
                    receipt = await self.send(request)
                    state["alerted"] = getattr(receipt, "status", "") in (
                        "queued",
                        "scheduled",
                    )
                except Exception:  # noqa: BLE001, S110 - retried on the next failure
                    pass
        try:
            self._write(
                self.path / f"{name}.state.json",
                {k: v for k, v in state.items() if k != "due"},
            )
        except OSError:
            pass
