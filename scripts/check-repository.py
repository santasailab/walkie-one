"""Check the source boundary and local documentation links without networking."""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def repository_paths(root):
    listing = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
    )
    return {root / p.decode() for p in listing.split(b"\0") if p}


def check_repository(root):
    paths = repository_paths(root)
    failures = []
    for path in sorted(paths):
        relative = path.relative_to(root)
        if path.is_symlink():
            failures.append(f"Symlink is outside this source distribution: {relative}")
            continue
        if not path.exists():
            continue  # A tracked deletion has no content in the working tree.
        if (
            path.name in {"server.json", "link.json", ".env", "command.json"}
            or path.name.startswith(".env.")
            and path.name != ".env.example"
            or ".jsonl" in path.name
            or path.suffix in {".pem", ".key", ".p12", ".pfx", ".db", ".log", ".jsonl"}
            or any(
                part in {".context", ".venv", "node_modules", ".ssh", ".aws", ".relay"}
                for part in relative.parts
            )
        ):
            failures.append(f"Private/generated artifact: {relative}")
        if path.suffix == ".json":
            try:
                value = json.loads(path.read_text())
            except (ValueError, UnicodeError):
                value = None
            if isinstance(value, dict) and (
                {"url", "credential"} <= value.keys()
                or {"devices", "link_credential", "speech"} <= value.keys()
            ):
                failures.append(f"Generated credential profile: {relative}")
        if path.suffix != ".md":
            continue
        for href in re.findall(r"\[[^\]\n]*\]\(([^)\n]+)\)", path.read_text()):
            href = href.split("#", 1)[0].strip("<>")
            if not href or "://" in href or href.startswith("mailto:"):
                continue
            target = (path.parent / href).resolve()
            if not target.is_relative_to(root) or not target.exists():
                failures.append(f"Broken local link in {relative}: {href}")
    return paths, failures


def main():
    paths, failures = check_repository(ROOT)
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Source boundary and documentation links checked: {len(paths)} files.")


if __name__ == "__main__":
    main()
