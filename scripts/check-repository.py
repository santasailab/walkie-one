"""Check the source boundary and local documentation links without networking."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    listing = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    )
    paths = {ROOT / p.decode() for p in listing.split(b"\0") if p}
    failures = []
    for path in sorted(paths):
        relative = path.relative_to(ROOT)
        if path.is_symlink():
            failures.append(f"Symlink is outside this source distribution: {relative}")
            continue
        if (
            path.name in {"server.json", "link.json", ".env", "command.json"}
            or path.suffix in {".pem", ".key", ".p12", ".pfx", ".db", ".log", ".jsonl"}
            or any(
                part in {".context", ".venv", "node_modules"} for part in relative.parts
            )
        ):
            failures.append(f"Private/generated artifact: {relative}")
        if path.suffix != ".md":
            continue
        for href in re.findall(r"\[[^\]\n]*\]\(([^)\n]+)\)", path.read_text()):
            href = href.split("#", 1)[0].strip("<>")
            if not href or "://" in href or href.startswith("mailto:"):
                continue
            target = (path.parent / href).resolve()
            if not target.is_relative_to(ROOT) or not target.exists():
                failures.append(f"Broken local link in {relative}: {href}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Source boundary and documentation links checked: {len(paths)} files.")


if __name__ == "__main__":
    main()
