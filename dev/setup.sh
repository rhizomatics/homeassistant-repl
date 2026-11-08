#!/usr/bin/env bash
# Install Home Assistant (own venv) and the ha-repl CLI (project venv). Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."
HA_VENV="${HA_VENV:-$PWD/.venv-ha}"

command -v uv >/dev/null || pip install --user uv

[ -x "$HA_VENV/bin/python" ] || uv venv --python 3.14 "$HA_VENV"
VIRTUAL_ENV="$HA_VENV" uv pip install "homeassistant${HA_VERSION:+==$HA_VERSION}"
uv sync --group dev

# pre-commit bakes this container's venv path into .git/hooks/pre-commit, so a
# fresh container (or one rebuilt with a different venv path) needs this rerun
# - it's not something committing the hook file itself could fix.
uv run prek install
