#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v gitleaks >/dev/null || {
  echo "Install Gitleaks 8.30.1, then run this command again." >&2
  exit 1
}
.venv/bin/python scripts/check-repository.py
.venv/bin/pip-audit --strict --progress-spinner off -r requirements-dev.txt
gitleaks git --config .gitleaks.toml --redact --no-banner --log-opts='--all' .

# Scan uncommitted source too, without copying ignored private files or the venv.
scan_tree=$(mktemp -d)
trap 'rm -rf "$scan_tree"' EXIT
.venv/bin/python - "$scan_tree" <<'PY'
import runpy
import shutil
import sys
from pathlib import Path
root, target = Path.cwd(), Path(sys.argv[1])
checks = runpy.run_path('scripts/check-repository.py')
for path in checks['repository_paths'](root):
    if path.is_file():
        destination = target / path.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
PY
gitleaks dir "$scan_tree" --config "$PWD/.gitleaks.toml" --redact --no-banner

# Verify our custom rule actually detects both generated token formats. Test
# values exist only in a private temporary directory and never enter Git/logs.
.venv/bin/python - <<'PY'
import json
import secrets
import subprocess
import tempfile
from pathlib import Path
config = str(Path('.gitleaks.toml').resolve())
with tempfile.TemporaryDirectory(prefix='walkie-secret-test-') as directory:
    root = Path(directory)
    for name, prefix in [('device', 'v1.test.device.'), ('link', 'v2.test.test.link.')]:
        suffix = secrets.token_urlsafe(32)
        if name == 'link':
            suffix = suffix[:-1] + '-'  # Accepted URL-safe character, not a word boundary.
        (root / (name + '.txt')).write_text(prefix + suffix)
    report = root / 'report.json'
    result = subprocess.run(
        ['gitleaks', 'dir', directory, '--config', config, '--redact', '--no-banner',
         '--report-format', 'json', '--report-path', str(report)], capture_output=True,
    )
    if result.returncode != 1:
        raise SystemExit('Secret scanner regression: expected fixture detection.')
    matches = json.loads(report.read_text())
    detected = {Path(item['File']).name for item in matches
                if item['RuleID'] == 'walkieone-client-credential'}
    if detected != {'device.txt', 'link.txt'}:
        raise SystemExit('Secret scanner regression: a credential format was missed.')
print('Security checks passed, including generated-credential detection.')
PY
