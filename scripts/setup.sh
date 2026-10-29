#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
"${WALKIEONE_PYTHON:-python3.12}" -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
echo "Ready. Run ./scripts/check.sh or follow personal-server/README.md."
