#!/usr/bin/env bash
# Adapted from the user's hrun.sh GPU diagnostic script. The console version
# accepts one literal node only, uses fixed checks, and bounds SSH/check runtime.
# It does not expand hostlists, accept custom commands, or change SSH trust.
set -u

if [[ $# -ne 1 || ! "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ || "$1" =~ ^[Aa][Ll][Ll]$ ]]; then
  echo "Usage: hrun.sh NODE (one literal hostname, maximum 128 characters)" >&2
  exit 2
fi

if ! command -v timeout >/dev/null 2>&1; then
  echo "ERROR: GNU timeout is required on the controller." >&2
  exit 127
fi

node="$1"
# Every check runs even if a previous check fails; a nonzero result is retained.
# GNU timeout's five-second kill grace is included in the 90-second node limit.
remote_command='timeout --signal=TERM --kill-after=5s 85s /bin/bash -c '\''status=0; hostname || status=$?; nvidia-smi || status=$?; /usr/bin/sudo -n /usr/bin/python3 /opt/oci-hpc/healthchecks/check_gpu_setup.py || status=$?; exit "$status"'\'''

printf '====== %s ======\n' "$node"
# The local five-second kill grace is included in the 110-second SSH limit.
timeout --signal=TERM --kill-after=5s 105s ssh \
  -o BatchMode=yes \
  -o ConnectTimeout=10 \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=2 \
  "$node" "$remote_command"
status=$?
printf '%s\n' '=============================================================' \
  '============================================================='
exit "$status"
