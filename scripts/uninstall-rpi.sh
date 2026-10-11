#!/usr/bin/env bash
# Meshgarden Raspberry Pi uninstaller.
#
# Run as: sudo ./scripts/uninstall-rpi.sh
#
# By default, stops/disables/removes the systemd unit only; config
# (/etc/meshgarden) and state (/var/lib/meshgarden) are preserved. Pass
# --purge to remove those too (with a typed confirmation unless --yes).
set -euo pipefail

SERVICE_USER="meshgarden"
UNIT_NAME="meshgarden.service"
UNIT_DEST="/etc/systemd/system/$UNIT_NAME"
WATCHDOG_TIMER_NAME="meshgarden-watchdog.timer"
WATCHDOG_SERVICE_NAME="meshgarden-watchdog.service"
WATCHDOG_TIMER_DEST="/etc/systemd/system/$WATCHDOG_TIMER_NAME"
WATCHDOG_SERVICE_DEST="/etc/systemd/system/$WATCHDOG_SERVICE_NAME"
CONFIG_DIR="/etc/meshgarden"
STATE_DIR="/var/lib/meshgarden"

PURGE=0
REMOVE_USER=0
ASSUME_YES=0
DRY_RUN=0
VERBOSE=0

log()  { printf '[uninstall-rpi] %s\n' "$*"; }
warn() { printf '[uninstall-rpi] warning: %s\n' "$*" >&2; }
err()  { printf '[uninstall-rpi] error: %s\n' "$*" >&2; }

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[dry-run] would run:'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

usage() {
    cat <<'EOF'
Usage: sudo ./scripts/uninstall-rpi.sh [OPTIONS]

Stops, disables, and removes the meshgarden.service systemd unit.
Config (/etc/meshgarden) and state (/var/lib/meshgarden) are preserved
by default.

Options:
  --purge         Also remove /etc/meshgarden and /var/lib/meshgarden.
                  Requires typed confirmation ("yes") unless --yes is given.
  --remove-user   Also remove the "meshgarden" system user (and its group).
  --yes           Skip the typed confirmation for --purge.
  --dry-run       Print every mutating command instead of running it; makes
                  no changes to the system.
  --verbose       Trace every command executed (set -x style).
  --help, -h      Show this help and exit.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1; shift ;;
        --remove-user) REMOVE_USER=1; shift ;;
        --yes) ASSUME_YES=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --verbose) VERBOSE=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) err "unknown option: $1"; usage; exit 2 ;;
    esac
done

if [ "$VERBOSE" -eq 1 ]; then
    set -x
fi

if [ "$EUID" -ne 0 ] && [ "${MESHGARDEN_INSTALL_FAKE_ROOT:-0}" != "1" ]; then
    err "must run as root (sudo ./scripts/uninstall-rpi.sh)"
    exit 1
fi

if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet "$UNIT_NAME" 2>/dev/null; then
        log "stopping $UNIT_NAME"
        run systemctl stop "$UNIT_NAME"
    fi
    if systemctl is-enabled --quiet "$UNIT_NAME" 2>/dev/null; then
        log "disabling $UNIT_NAME"
        run systemctl disable "$UNIT_NAME"
    fi
fi

if [ -e "$UNIT_DEST" ]; then
    log "removing $UNIT_DEST"
    run rm -f "$UNIT_DEST"
    run systemctl daemon-reload
else
    log "$UNIT_DEST not present; nothing to remove"
fi

# The watchdog timer/service (docs/operations.md) are installed alongside
# the main unit but are a separate opt-in step -- if present, they must go
# too, or the timer keeps firing every minute after mgarden is removed.
if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet "$WATCHDOG_TIMER_NAME" 2>/dev/null; then
        log "stopping $WATCHDOG_TIMER_NAME"
        run systemctl stop "$WATCHDOG_TIMER_NAME"
    fi
    if systemctl is-enabled --quiet "$WATCHDOG_TIMER_NAME" 2>/dev/null; then
        log "disabling $WATCHDOG_TIMER_NAME"
        run systemctl disable "$WATCHDOG_TIMER_NAME"
    fi
    if systemctl is-active --quiet "$WATCHDOG_SERVICE_NAME" 2>/dev/null; then
        log "stopping $WATCHDOG_SERVICE_NAME"
        run systemctl stop "$WATCHDOG_SERVICE_NAME"
    fi
fi

for watchdog_unit in "$WATCHDOG_TIMER_DEST" "$WATCHDOG_SERVICE_DEST"; do
    if [ -e "$watchdog_unit" ]; then
        log "removing $watchdog_unit"
        run rm -f "$watchdog_unit"
        run systemctl daemon-reload
    else
        log "$watchdog_unit not present; nothing to remove"
    fi
done

# CLI wrappers are deployment artifacts installed by install-rpi.sh, not
# user data -- always removed, regardless of --purge.
for bin_name in mgarden meshtastic-observatory; do
    wrapper_dest="/usr/local/bin/$bin_name"
    if [ -e "$wrapper_dest" ]; then
        log "removing $wrapper_dest"
        run rm -f "$wrapper_dest"
    else
        log "$wrapper_dest not present; nothing to remove"
    fi
done

if [ "$PURGE" -eq 1 ]; then
    if [ "$ASSUME_YES" -ne 1 ]; then
        printf 'This will permanently delete %s and %s. Type "yes" to continue: ' "$CONFIG_DIR" "$STATE_DIR"
        read -r reply
        if [ "$reply" != "yes" ]; then
            log "purge cancelled"
            exit 0
        fi
    fi
    log "purging $CONFIG_DIR"
    run rm -rf "$CONFIG_DIR"
    log "purging $STATE_DIR"
    run rm -rf "$STATE_DIR"
else
    log "preserving $CONFIG_DIR and $STATE_DIR (pass --purge to remove them)"
fi

if [ "$REMOVE_USER" -eq 1 ]; then
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        log "removing user $SERVICE_USER"
        run userdel "$SERVICE_USER"
    else
        log "user $SERVICE_USER does not exist; nothing to remove"
    fi
fi

log "uninstall complete."
