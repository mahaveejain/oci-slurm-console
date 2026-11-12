#!/usr/bin/env bash
# Run on the Mac that opens the console, never on the controller.
set -euo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "$(uname -s)" == Darwin ]] || { echo 'Run this helper on your Mac.' >&2; exit 1; }
PYTHON="${SLURM_HELPER_PYTHON:-${ROOT}/.venv/bin/python}"
[[ -x "$PYTHON" ]] || { echo 'Create the Mac .venv and install requirements-grafana-helper.txt first.' >&2; exit 1; }
exec "$PYTHON" "$ROOT/scripts/grafana_browser_helper.py" "$@"
