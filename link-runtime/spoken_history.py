"""The optional local speech context, with a hard age limit and private files.

The native app uses the same lock and settings when Link is stopped. Provider
histories and files created by the Agent are deliberately outside this store.
"""

import contextlib
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path

MAX_AGE = 12 * 3600


class SpokenHistory:
    def __init__(self, home, recent, keep=400, max_age=MAX_AGE):
        self.home = Path(home)
        self.path = Path(recent)
        self.keep = min(400, max(1, keep))
        self.max_age = min(MAX_AGE, max(0, max_age))

    @contextlib.contextmanager
    def locked(self):
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.home.chmod(0o700)
        descriptor = os.open(self.home / "history.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def settings(self):
        try:
            settings = json.loads((self.home / "privacy.json").read_text())
            return (
                settings if isinstance(settings, dict) else {"history_enabled": False}
            )
        except FileNotFoundError:
            return {"history_enabled": True}
        except (OSError, ValueError):
            return {"history_enabled": False}

    def enabled(self):
        return self.settings().get("history_enabled") is True

    def _lines(self, now):
        if not self.enabled():
            return []
        try:
            raw = self.path.read_text().splitlines()
        except FileNotFoundError:
            return []
        lines = []
        for line in raw:
            fields = line.split("\t", 3)
            try:
                age = now - int(fields[0])
            except ValueError:
                continue
            if len(fields) == 4 and fields[3] and 0 <= age < self.max_age:
                lines.append(line)
        return lines[-self.keep :]

    def _write(self, lines):
        if not lines:
            self.path.unlink(missing_ok=True)
            return
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write("\n".join(lines) + "\n")
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def read(self, now=None):
        now = time.time() if now is None else now
        with self.locked():
            lines = self._lines(now)
            self._write(lines)
            return lines

    def append(self, scope, who, text):
        with self.locked():
            lines = self._lines(time.time())
            if self.enabled():
                lines.append(f"{int(time.time())}\t{scope}\t{who}\t{text}")
            self._write(lines[-self.keep :])

    def clean_legacy_output(self, turns):
        # Only files created by our old protocol logger; never recurse into
        # Agent work or provider-owned storage.
        for path in Path(turns).glob("*.jsonl"):
            path.unlink(missing_ok=True)

    def clear(self):
        with self.locked():
            self.path.unlink(missing_ok=True)
            for path in self.home.glob("session-*"):
                if path.is_file():
                    path.unlink(missing_ok=True)
