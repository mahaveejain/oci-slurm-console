#!/usr/bin/env bash
# Background SSH tunnel manager. No open terminal or LaunchAgent required.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.slurm-console-tunnel"
ACTION="${1:-start}"
DEFAULT_CONTROLLER="${SLURM_CONTROLLER:-ubuntu@192.0.2.10}"

usage() {
  echo "Usage: $0 {start|restart} [user@controller-ip-or-ssh-alias] [local-port]"
  echo "       $0 {stop|status} [local-port]"
}
fail() { echo "$*" >&2; exit 1; }
case "$ACTION" in
  start|restart) [[ $# -le 3 ]] || { usage; exit 2; }; REQUESTED_TARGET="${2:-}"; LOCAL_PORT="${3:-8080}" ;;
  stop|status) [[ $# -le 2 ]] || { usage; exit 2; }; LOCAL_PORT="${2:-8080}" ;;
  help|-h|--help) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac
[[ "$LOCAL_PORT" =~ ^[0-9]{4,5}$ ]] || fail "Local port must be between 1024 and 65535."
LOCAL_PORT=$((10#$LOCAL_PORT))
(( LOCAL_PORT >= 1024 && LOCAL_PORT <= 65535 )) || fail "Local port must be between 1024 and 65535."
[[ ! -L "$STATE_DIR" ]] || fail "Refusing symlink state directory: $STATE_DIR"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
SOCKET="${STATE_DIR}/${LOCAL_PORT}.sock"
TARGET_FILE="${STATE_DIR}/${LOCAL_PORT}.target"
CONSOLE_URL="http://127.0.0.1:${LOCAL_PORT}/"
# macOS Unix socket paths have a short length limit.
(( ${#SOCKET} < 100 )) || fail "Script directory path is too long for an SSH control socket."
SAVED_TARGET=""
[[ ! -f "$TARGET_FILE" ]] || SAVED_TARGET="$(cat "$TARGET_FILE")"
managed_running() {
  [[ -n "$SAVED_TARGET" && -S "$SOCKET" ]] &&
    ssh -S "$SOCKET" -O check "$SAVED_TARGET" >/dev/null 2>&1
}
port_in_use() {
  lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1
}
stop_managed() {
  if managed_running; then
    ssh -S "$SOCKET" -O exit "$SAVED_TARGET" >/dev/null 2>&1 || fail "Could not stop managed tunnel."
    # Wait for the master to release the local listener before a restart.
    for _ in {1..30}; do
      managed_running || break
      sleep 0.1
    done
    managed_running && fail "Tunnel is still stopping; retry shortly."
    rm -f "$SOCKET"
    echo "Tunnel stopped: $SAVED_TARGET (local port $LOCAL_PORT)."
  else
    echo "No managed tunnel running on local port $LOCAL_PORT."
  fi
}
if [[ "$ACTION" == status ]]; then
  if managed_running; then
    echo "Tunnel running: $SAVED_TARGET -> $CONSOLE_URL"
  elif port_in_use; then
    echo "Port $LOCAL_PORT is occupied by a process this script does not manage."
    exit 1
  else
    echo "Tunnel stopped (local port $LOCAL_PORT)."
  fi
  exit 0
fi
# Retire the previous launcher; it otherwise keeps retrying the stale controller.
if [[ "$LOCAL_PORT" == 8080 ]] && command -v launchctl >/dev/null 2>&1; then
  LEGACY_SERVICE="gui/$(id -u)/com.mahaveerjain.slurm-console-tunnel"
  if launchctl print "$LEGACY_SERVICE" >/dev/null 2>&1; then
    launchctl bootout "$LEGACY_SERVICE" || fail "Could not stop legacy tunnel service."
  fi
  LEGACY_PLIST="${HOME}/Library/LaunchAgents/com.mahaveerjain.slurm-console-tunnel.plist"
  if [[ -f "$LEGACY_PLIST" ]] && [[ -x /usr/libexec/PlistBuddy ]]; then
    LEGACY_PROGRAM="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$LEGACY_PLIST" 2>/dev/null || true)"
    if [[ "$LEGACY_PROGRAM" == "${SCRIPT_DIR}/slurm-console-tunnel.sh" ]]; then
      mv "$LEGACY_PLIST" "${STATE_DIR}/legacy-launchagent-$(date +%Y%m%d-%H%M%S)-$$.plist"
      echo "Retired old LaunchAgent; its backup is in $STATE_DIR."
    fi
  fi
fi
if [[ "$ACTION" == stop ]]; then
  stop_managed
  exit 0
fi
TARGET="${REQUESTED_TARGET:-${SAVED_TARGET:-$DEFAULT_CONTROLLER}}"
[[ "$TARGET" =~ ^([a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9_][a-zA-Z0-9_.:-]*$ ]] || fail "Use a controller IP, hostname, or SSH alias, optionally prefixed with user@."
# Default to the controller's ubuntu account, rather than a global SSH User setting.
if [[ "$TARGET" != *@* ]]; then
  TARGET="ubuntu@$TARGET"
fi
if managed_running && { [[ "$ACTION" == restart ]] || [[ "$SAVED_TARGET" != "$TARGET" ]]; }; then
  stop_managed
fi
if ! managed_running; then
  if port_in_use; then
    fail "Local port $LOCAL_PORT is already occupied. Close the earlier manual tunnel, or use: $0 start $TARGET 18080"
  fi
  rm -f "$SOCKET"
  echo "Connecting to $TARGET..."
  # -f detaches only after authentication and successful creation of the forward.
  # ControlPersist keeps the master alive; -O exit explicitly stops it.
  if ! ssh -f -N -T -M -S "$SOCKET" \
    -o ControlMaster=yes -o ControlPersist=yes \
    -o ExitOnForwardFailure=yes -o ConnectTimeout=15 \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:8080" "$TARGET"; then
    fail "SSH tunnel failed for $TARGET. See the SSH error above."
  fi
  printf '%s\n' "$TARGET" > "$TARGET_FILE"
  SAVED_TARGET="$TARGET"
  managed_running || fail "SSH started but the control socket is not responding."
fi
echo "Tunnel ready: $TARGET -> $CONSOLE_URL (safe to close this terminal)."
# A listener proves the SSH forward exists; this check also tests the remote app.
if command -v curl >/dev/null 2>&1; then
  if ! curl --silent --output /dev/null --connect-timeout 3 --max-time 8 "$CONSOLE_URL"; then
    echo "Tunnel is running, but the console is not responding. Check slurm-console.service and remote port 8080." >&2
    exit 1
  fi
fi
if [[ "${SLURM_TUNNEL_NO_OPEN:-0}" != 1 ]]; then
  if command -v open >/dev/null 2>&1; then
    open -a "Google Chrome" "$CONSOLE_URL" || open "$CONSOLE_URL"
  fi
fi
