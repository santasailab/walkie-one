#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
.venv/bin/python scripts/check-repository.py
.venv/bin/ruff check personal-server server-runtime link-runtime scripts
.venv/bin/ruff format --check personal-server link-runtime/independent.py link-runtime/custom_endpoint.py server-runtime/tests/tls_fixture.py scripts
.venv/bin/python -m pytest server-runtime/tests link-runtime/tests
.venv/bin/python schema/test_events.py
.venv/bin/python personal-server/rehearse.py
echo "All local software checks passed. Hardware and live providers are separate."
