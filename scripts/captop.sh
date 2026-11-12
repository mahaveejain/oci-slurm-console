#!/usr/bin/env bash
# Read-only capacity topology report; no nodenurse, OCI CLI, jq, or Slurm.
set -euo pipefail
python_bin="${CAPTOP_PYTHON:-python3}"
if ! command -v "$python_bin" >/dev/null 2>&1; then
    printf 'captop: Python interpreter not found: %s\n' "$python_bin" >&2
    exit 1
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$python_bin" -u "$script_dir/captop.py" "$@"
