#!/usr/bin/env bash
# Registers a job for this Mac login session only; no login startup file is installed.
set -euo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL=com.mahaveerjain.slurm-grafana-browser-helper
DOMAIN="gui/$(id -u)"
case "${1:-status}" in
  start)
    mkdir -p "$ROOT/.slurm-console-tunnel"
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      echo 'Grafana helper already registered for this Mac session.'
    else
      launchctl bootstrap "$DOMAIN" "$ROOT/deploy/$LABEL.plist"
      echo 'Grafana helper started for this Mac session.'
    fi
    ;;
  stop) launchctl bootout "$DOMAIN/$LABEL"; echo 'Grafana helper stopped.' ;;
  status) launchctl print "$DOMAIN/$LABEL" ;;
  *) echo 'Usage: manage-grafana-browser-helper.sh {start|stop|status}' >&2; exit 2 ;;
esac
