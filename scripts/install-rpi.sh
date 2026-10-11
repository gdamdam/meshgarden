#!/usr/bin/env bash
# Meshgarden Raspberry Pi installer.
#
# Run as: sudo ./scripts/install-rpi.sh
#
# Idempotent: safe to re-run after `git pull` (e.g. to pick up a new
# systemd unit or refresh the venv). Never overwrites an existing
# config.toml or any state under /var/lib/meshgarden (use --force-config
# to explicitly replace the config, which is first backed up).
#
# See `./scripts/install-rpi.sh --help` for all options.
set -euo pipefail

# -----------------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="meshgarden"
SERVICE_GROUP="meshgarden"
CONFIG_DIR="/etc/meshgarden"
CONFIG_PATH="/etc/meshgarden/config.toml"
STATE_DIR="/var/lib/meshgarden"
UNIT_NAME="meshgarden.service"
UNIT_SRC="$REPO_DIR/systemd/meshgarden.service"
UNIT_DEST="/etc/systemd/system/$UNIT_NAME"
REQUIRED_APT_PKGS=(python3 python3-venv git curl ca-certificates sqlite3)

REPO_USER="${SUDO_USER:-}"
DEVICE_PATH=""
INSTALL_DIR=""
WEB_LISTEN=""
TRANSPORT=""
NO_START=0
NO_ENABLE=0
SKIP_APT=0
SKIP_UV_INSTALL=0
FORCE_CONFIG=0
NO_CONFIG_SYNC=0
FIX_HOME_TRAVERSAL=0
RESET_DATA=0
ASSUME_YES=0
DEBUG=0
DRY_RUN=0
VERBOSE=0

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
log()  { printf '[install-rpi] %s\n' "$*"; }
warn() { printf '[install-rpi] warning: %s\n' "$*" >&2; }
err()  { printf '[install-rpi] error: %s\n' "$*" >&2; }

# Wraps every mutating command. Under --dry-run, prints the command
# (shell-quoted) instead of executing it, so a dry run makes no changes.
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
Usage: sudo ./scripts/install-rpi.sh [OPTIONS]

Installs Meshgarden to run in place from this checkout, as a systemd
service under a dedicated unprivileged "meshgarden" system user.
Safe to re-run (e.g. after `git pull`).

Options:
  --user USER          The repo-owning, non-root user that owns this checkout
                        and runs `uv sync` (default: $SUDO_USER, i.e. whoever
                        invoked sudo). This is NOT the service account -- the
                        daemon itself always runs as the dedicated system
                        user "meshgarden", created automatically if missing.
  --device PATH         Serial device path for [radio] device in the
                        generated config (e.g. /dev/serial/by-id/usb-...).
                        Required when more than one candidate device is
                        found; auto-selected when exactly one is found.
  --transport meshtastic|meshcore
                        Radio backend this install runs. Only affects
                        which optional Python extra is installed by `uv
                        sync` below (`--extra meshcore` when this is
                        "meshcore") -- it does NOT edit [radio] transport
                        in an existing config.toml; set that key yourself
                        (config.example.toml documents it). Defaults to
                        whatever an existing config.toml's [radio]
                        transport already says, or "meshtastic" if there
                        is no config yet -- so a re-run (e.g. after `git
                        pull`) keeps the meshcore extra installed for an
                        existing MeshCore deployment without repeating
                        this flag.
  --config PATH          Path to the config file to create/use
                        (default: /etc/meshgarden/config.toml).
  --web-listen ADDR      Enable the read-only web dashboard and bind it to
                        ADDR (IPv4/IPv6 literal or 0.0.0.0). When the
                        config is being (re)generated (first install or
                        --force-config), writes [web] section with enabled=true,
                        listen=ADDR, port=9109, and allow_lan=true iff ADDR is
                        not loopback. If config exists without --force-config,
                        prints the manual config keys instead.
  --install-dir PATH     Reserved for a future release. Meshgarden currently
                        always runs in place from this checkout; this flag is
                        accepted but has no effect (a notice is printed).
  --no-start             Do not start/restart the service after install.
  --no-enable            Do not `systemctl enable` the service.
  --skip-apt             Do not run apt-get; assume required packages are
                        already installed.
  --skip-uv-install      Do not install uv even if missing for the repo user;
                        fail instead if it cannot be found.
  --force-config         Replace an existing config.toml, after backing it up
                        to config.toml.bak.<timestamp>. Without this flag an
                        existing config is never touched.
  --no-config-sync       Without --force-config, an existing config.toml is
                        normally checked for keys added since it was written
                        (e.g. by a newer Meshgarden version) via
                        `mgarden config-sync`, which appends any that are
                        missing, with their defaults, and never changes a
                        value already in the file (a `.bak.<timestamp>` is
                        written first). --no-config-sync skips this check
                        entirely, leaving the existing config exactly as
                        found.
  --fix-home-traversal   When the service-user traversal pre-flight (see
                        below) fails, apply `chmod o+x` -- only the execute
                        bit for others, read/write bits are never touched --
                        to each directory from the checkout up to / that
                        lacks it, printing every change made. Without this
                        flag, the pre-flight only reports the problem.
  --reset-data           Wipe the database and every other file the daemon
                        writes under the state directory (/var/lib/meshgarden).
                        Config is never touched; backups/ is kept (use
                        uninstall-rpi.sh --purge to remove everything,
                        backups included). Stops the service first if it is
                        active. Requires typed confirmation ("yes") unless
                        --yes is given; skipped entirely under --dry-run,
                        which only prints the plan.
  --yes                  Skip the typed confirmation for --reset-data.
  --debug                Install the service running `mgarden run --debug`
                        instead of plain `run` (the DM/reply exchange trace;
                        see `mgarden run --help`). Per-install, not sticky:
                        re-running without --debug renders the unit with
                        plain `run` again. Prints a NOTICE about the
                        trade-off (main.py itself warns this is not meant
                        for an unattended/systemd deployment).
  --dry-run              Print every mutating command instead of running it;
                        makes no changes to the system.
  --verbose              Trace every command executed (set -x style).
  --help, -h             Show this help and exit.

Pre-flight: before installing the systemd unit, this script verifies (as
the "meshgarden" service user) that <checkout>/.venv/bin/mgarden is
executable and every directory above it is traversable. This commonly
fails for a checkout under a user's home directory (mode 0700 by
default), since the unit's ProtectHome=read-only does not bypass normal
POSIX permissions. Fix by running `chmod o+x /home/<user>` (and any other
0700 directory above the checkout), by moving the checkout to
/opt/meshgarden and re-running this installer there, or by re-running with
--fix-home-traversal to apply the chmod automatically.

Fresh install:
  git clone <repo-url> meshgarden && cd meshgarden
  sudo ./scripts/install-rpi.sh

Upgrade:
  cd meshgarden && git pull && sudo ./scripts/install-rpi.sh

Testing note: MESHGARDEN_INSTALL_FAKE_ROOT=1 bypasses the root check, for
automated --dry-run testing only. Never set it for a real install.
EOF
}

require_arg() {
    if [ -z "${2:-}" ]; then
        err "$1 requires an argument"
        exit 2
    fi
}

# -----------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --user) require_arg "$1" "${2:-}"; REPO_USER="$2"; shift 2 ;;
        --device) require_arg "$1" "${2:-}"; DEVICE_PATH="$2"; shift 2 ;;
        --transport) require_arg "$1" "${2:-}"; TRANSPORT="$2"; shift 2 ;;
        --config) require_arg "$1" "${2:-}"; CONFIG_PATH="$2"; shift 2 ;;
        --web-listen) require_arg "$1" "${2:-}"; WEB_LISTEN="$2"; shift 2 ;;
        --install-dir) require_arg "$1" "${2:-}"; INSTALL_DIR="$2"; shift 2 ;;
        --no-start) NO_START=1; shift ;;
        --no-enable) NO_ENABLE=1; shift ;;
        --skip-apt) SKIP_APT=1; shift ;;
        --skip-uv-install) SKIP_UV_INSTALL=1; shift ;;
        --force-config) FORCE_CONFIG=1; shift ;;
        --no-config-sync) NO_CONFIG_SYNC=1; shift ;;
        --fix-home-traversal) FIX_HOME_TRAVERSAL=1; shift ;;
        --reset-data) RESET_DATA=1; shift ;;
        --yes) ASSUME_YES=1; shift ;;
        --debug) DEBUG=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --verbose) VERBOSE=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) err "unknown option: $1"; usage; exit 2 ;;
    esac
done

if [ "$VERBOSE" -eq 1 ]; then
    set -x
fi

if [ -n "$INSTALL_DIR" ]; then
    warn "--install-dir is reserved for a future release; Meshgarden always runs in place from $REPO_DIR. Ignoring --install-dir=$INSTALL_DIR."
fi

if [ "$DEBUG" -eq 1 ]; then
    warn "NOTICE: --debug installs the service running \`mgarden run --debug\`: the DM/reply exchange trace writes every DM's raw text (guestbook/mail bodies, the LAB unlock word) to stderr, which this unit sends to the journal, where it persists, and it also lands in the rotated exchange file under the logs dir (<database dir>/logs/exchange.log). For troubleshooting only -- re-run this installer WITHOUT --debug to turn it off, and use \`journalctl --vacuum-time=<duration>\` to drop old trace lines."
fi

CONFIG_DIR="$(dirname "$CONFIG_PATH")"

# -----------------------------------------------------------------------
# Web listen validation
# -----------------------------------------------------------------------
validate_listen_addr() {
    local addr="$1"
    # Prefer real parsing via Python's ipaddress (the repo venv's python3 if
    # a prior sync already produced one, else whatever python3 check_python
    # requires) so obviously-bogus values like "foo:bar" (which merely
    # *contains* a colon) are rejected; only fall back to the looser regex
    # check below if no python3 is available at all.
    local py=""
    if [ -x "$REPO_DIR/.venv/bin/python3" ]; then
        py="$REPO_DIR/.venv/bin/python3"
    elif command -v python3 >/dev/null 2>&1; then
        py="python3"
    fi
    if [ -n "$py" ]; then
        "$py" -c '
import ipaddress, sys
try:
    ipaddress.ip_address(sys.argv[1])
except ValueError:
    sys.exit(1)
' "$addr" 2>/dev/null
        return $?
    fi
    # Fallback (no python3 found): accept 0.0.0.0, IPv4 addresses, or
    # hex-and-colons-only IPv6-shaped addresses; reject everything else.
    if [ "$addr" = "0.0.0.0" ]; then
        return 0
    fi
    if echo "$addr" | grep -qE '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'; then
        return 0
    fi
    if echo "$addr" | grep -qE '^[0-9A-Fa-f:]+$' && echo "$addr" | grep -q ':'; then
        return 0
    fi
    return 1
}

is_loopback_addr() {
    local addr="$1"
    case "$addr" in
        127.*|::1|localhost) return 0 ;;
        *) return 1 ;;
    esac
}

if [ -n "$WEB_LISTEN" ] && ! validate_listen_addr "$WEB_LISTEN"; then
    err "--web-listen ADDR must be an IPv4/IPv6 literal or 0.0.0.0, got: $WEB_LISTEN"
    exit 2
fi

if [ -n "$TRANSPORT" ] && [ "$TRANSPORT" != "meshtastic" ] && [ "$TRANSPORT" != "meshcore" ]; then
    err "--transport must be \"meshtastic\" or \"meshcore\", got: $TRANSPORT"
    exit 2
fi

# -----------------------------------------------------------------------
# Root check (bypassable only for automated --dry-run testing)
# -----------------------------------------------------------------------
if [ "$EUID" -ne 0 ] && [ "${MESHGARDEN_INSTALL_FAKE_ROOT:-0}" != "1" ]; then
    err "must run as root (sudo ./scripts/install-rpi.sh)"
    exit 1
fi

# Testing-only: when MESHGARDEN_INSTALL_FAKE_ROOT=1 is set, also honor
# MESHGARDEN_INSTALL_STATE_DIR to redirect STATE_DIR at a throwaway
# directory -- lets --reset-data's file-removal loop be exercised in tests
# without touching the real /var/lib/meshgarden. Never honored otherwise
# (a real root run always uses /var/lib/meshgarden), so this can't redirect
# a real install.
if [ "${MESHGARDEN_INSTALL_FAKE_ROOT:-0}" = "1" ] && [ -n "${MESHGARDEN_INSTALL_STATE_DIR:-}" ]; then
    STATE_DIR="$MESHGARDEN_INSTALL_STATE_DIR"
fi

# -----------------------------------------------------------------------
# Environment validation
# -----------------------------------------------------------------------
check_os() {
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        if [ "${ID:-}" != "debian" ] || [ "${VERSION_CODENAME:-}" != "bookworm" ]; then
            warn "detected ${PRETTY_NAME:-an unrecognized OS}; this installer targets Debian 12 (bookworm). Continuing anyway."
        fi
    else
        warn "cannot read /etc/os-release; assuming a Debian-compatible system."
    fi
}

check_arch() {
    local arch
    arch="$(uname -m)"
    case "$arch" in
        aarch64) log "architecture: $arch" ;;
        x86_64) warn "architecture x86_64 detected; Raspberry Pi hardware is normally aarch64. Continuing anyway." ;;
        *) warn "unrecognized architecture $arch; continuing anyway." ;;
    esac
}

check_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 not found on PATH"
        exit 1
    fi
    local major minor
    major="$(python3 -c 'import sys; print(sys.version_info[0])')"
    minor="$(python3 -c 'import sys; print(sys.version_info[1])')"
    if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 11 ]; }; then
        err "python3 >= 3.11 required, found $major.$minor"
        exit 1
    fi
    log "system python3: $major.$minor"
}

check_systemd() {
    if ! command -v systemctl >/dev/null 2>&1; then
        if [ "$DRY_RUN" -eq 1 ]; then
            warn "systemd (systemctl) not found; continuing anyway since --dry-run makes no changes"
        else
            err "systemd (systemctl) not found; this installer requires systemd"
            exit 1
        fi
    fi
}

check_os
check_arch
check_python
check_systemd

# -----------------------------------------------------------------------
# Repo user resolution
# -----------------------------------------------------------------------
if [ -z "$REPO_USER" ] || [ "$REPO_USER" = "root" ]; then
    err "cannot determine the repo-owning user; re-run via sudo (so \$SUDO_USER is set) or pass --user USER"
    exit 2
fi
if ! id -u "$REPO_USER" >/dev/null 2>&1; then
    err "--user $REPO_USER does not exist"
    exit 2
fi

# Portable home-directory lookup: `getent` isn't available on every
# platform this script gets exercised on (e.g. macOS during --dry-run
# testing); fall back to Python's pwd module, then to shell tilde
# expansion.
user_home_dir() {
    if command -v getent >/dev/null 2>&1; then
        getent passwd "$1" | cut -d: -f6
    else
        python3 -c "import pwd, sys; print(pwd.getpwnam(sys.argv[1]).pw_dir)" "$1" 2>/dev/null \
            || eval echo "~$1"
    fi
}

user_exists() {
    id -u "$1" >/dev/null 2>&1
}

REPO_HOME="$(user_home_dir "$REPO_USER")"
log "repo owner (runs uv sync): $REPO_USER (home: $REPO_HOME)"
log "checkout: $REPO_DIR"

# -----------------------------------------------------------------------
# Service user
# -----------------------------------------------------------------------
if user_exists "$SERVICE_USER"; then
    log "service user $SERVICE_USER already exists"
else
    log "creating system user $SERVICE_USER"
    run useradd --system --user-group --home "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
if id -nG "$SERVICE_USER" 2>/dev/null | tr ' ' '\n' | grep -qx dialout; then
    log "$SERVICE_USER already in dialout group"
else
    log "adding $SERVICE_USER to dialout group"
    run usermod -aG dialout "$SERVICE_USER"
fi

# -----------------------------------------------------------------------
# apt packages
# -----------------------------------------------------------------------
if [ "$SKIP_APT" -eq 1 ]; then
    log "skipping apt-get (--skip-apt)"
elif ! command -v dpkg >/dev/null 2>&1; then
    if [ "$DRY_RUN" -eq 1 ]; then
        warn "dpkg not found (not a Debian system?); skipping missing-package detection for this dry run"
    else
        err "dpkg not found; this installer requires a Debian-based system"
        exit 1
    fi
else
    missing=()
    for pkg in "${REQUIRED_APT_PKGS[@]}"; do
        if ! dpkg -s "$pkg" >/dev/null 2>&1; then
            missing+=("$pkg")
        fi
    done
    if [ "${#missing[@]}" -eq 0 ]; then
        log "all required apt packages already installed"
    else
        log "installing missing apt packages: ${missing[*]}"
        run apt-get update
        run apt-get install -y "${missing[@]}"
    fi
fi

# -----------------------------------------------------------------------
# uv (installed for the repo user, never touching system python)
# -----------------------------------------------------------------------
UV_BIN="$REPO_HOME/.local/bin/uv"
if [ -x "$UV_BIN" ]; then
    log "uv found at $UV_BIN"
elif command -v runuser >/dev/null 2>&1 && runuser -u "$REPO_USER" -- bash -lc 'command -v uv' >/dev/null 2>&1; then
    UV_BIN="$(runuser -u "$REPO_USER" -- bash -lc 'command -v uv')"
    log "uv found on $REPO_USER's PATH: $UV_BIN"
elif [ "$SKIP_UV_INSTALL" -eq 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        warn "uv not found for $REPO_USER; continuing dry run with --skip-uv-install (a real run would fail here)"
    else
        err "uv not found for $REPO_USER and --skip-uv-install was given"
        exit 1
    fi
else
    log "installing uv for $REPO_USER via the official installer"
    run runuser -u "$REPO_USER" -- bash -lc 'set -o pipefail; curl -LsSf https://astral.sh/uv/install.sh | sh'
    UV_BIN="$REPO_HOME/.local/bin/uv"
fi

# -----------------------------------------------------------------------
# uv sync + verification
# -----------------------------------------------------------------------
# Safely quoted for interpolation into the bash -lc string below (paths
# may contain characters that would break naive manual quoting).
REPO_DIR_Q="$(printf '%q' "$REPO_DIR")"
UV_BIN_Q="$(printf '%q' "$UV_BIN")"

# The `meshcore` package is an optional dependency group
# (pyproject.toml's own comment: "Only install this extra if you actually
# run that transport") -- a plain `uv sync --frozen --no-dev` never pulls
# it in, so a fresh MeshCore install used to come up with the library
# missing and the daemon exiting at startup with its own "install it with
# `uv sync --extra meshcore`" message. `--transport` (if given) decides
# this directly; otherwise, since config generation below runs AFTER this
# sync and a fresh install has no config yet to read, fall back to an
# EXISTING config.toml's own `[radio] transport` (from a previous run --
# e.g. `git pull && sudo ./scripts/install-rpi.sh`, the documented upgrade
# path), so a re-run keeps the extra installed for an already-deployed
# MeshCore radio without the operator having to repeat the flag. Neither
# source present: "meshtastic", same as today -- no behavior change for a
# Meshtastic-only install.
EFFECTIVE_TRANSPORT="$TRANSPORT"
if [ -z "$EFFECTIVE_TRANSPORT" ] && [ -e "$CONFIG_PATH" ] \
    && grep -Eq '^[[:space:]]*transport[[:space:]]*=[[:space:]]*"meshcore"' "$CONFIG_PATH"; then
    EFFECTIVE_TRANSPORT="meshcore"
    log "detected transport = \"meshcore\" in existing $CONFIG_PATH"
fi
UV_SYNC_EXTRA=""
if [ "$EFFECTIVE_TRANSPORT" = "meshcore" ]; then
    UV_SYNC_EXTRA=" --extra meshcore"
    log "MeshCore transport selected: uv sync will also install the optional meshcore extra"
fi

log "running uv sync --frozen --no-dev$UV_SYNC_EXTRA in $REPO_DIR as $REPO_USER"
run runuser -u "$REPO_USER" -- bash -lc "cd $REPO_DIR_Q && $UV_BIN_Q sync --frozen --no-dev$UV_SYNC_EXTRA"

if [ "$DRY_RUN" -eq 1 ]; then
    log "dry-run: skipping post-sync verification (uv run mgarden --help/--version)"
else
    log "verifying mgarden entry point"
    # --no-sync: the venv was just built --frozen --no-dev; a plain `uv run`
    # here would implicitly re-sync in the dev group and could rewrite uv.lock.
    runuser -u "$REPO_USER" -- bash -lc "cd $REPO_DIR_Q && $UV_BIN_Q run --no-sync mgarden --help" >/dev/null
    runuser -u "$REPO_USER" -- bash -lc "cd $REPO_DIR_Q && $UV_BIN_Q run --no-sync mgarden --version"
fi

# -----------------------------------------------------------------------
# --reset-data: wipe stored state, never the config, keeping backups/
# -----------------------------------------------------------------------
# Runs after the service is (optionally) stopped and before the state-dir
# creation/chown block below, so nothing is writing to $STATE_DIR while its
# contents are removed, and the directory is recreated/owned correctly
# afterwards. Never touches $CONFIG_DIR/$CONFIG_PATH -- only
# uninstall-rpi.sh --purge removes those.
if [ "$RESET_DATA" -eq 1 ]; then
    case "$CONFIG_PATH" in
        "$STATE_DIR"/*|"$STATE_DIR")
            err "--reset-data refused: --config ($CONFIG_PATH) resolves under STATE_DIR ($STATE_DIR); the removal loop would delete the config itself. Point --config outside $STATE_DIR and retry."
            exit 1
            ;;
    esac
    log "--reset-data: will wipe $STATE_DIR (database + daemon state), keeping backups/; config is never touched"
    if [ "$DRY_RUN" -eq 0 ] && [ "$ASSUME_YES" -ne 1 ]; then
        printf 'This will permanently delete the database and all other daemon state under %s (keeping %s/backups; %s is never touched). Type "yes" to continue: ' \
            "$STATE_DIR" "$STATE_DIR" "$CONFIG_PATH"
        read -r reply
        if [ "$reply" != "yes" ]; then
            log "reset-data cancelled"
            exit 0
        fi
    fi

    if [ "$DRY_RUN" -eq 0 ] && systemctl is-active --quiet "$UNIT_NAME"; then
        log "stopping $UNIT_NAME before resetting state"
        run systemctl stop "$UNIT_NAME"
    fi

    log "removing $STATE_DIR/observatory.db (and -wal/-shm)"
    run rm -f "$STATE_DIR/observatory.db" "$STATE_DIR/observatory.db-wal" "$STATE_DIR/observatory.db-shm"

    if [ -d "$STATE_DIR" ]; then
        # `find -mindepth 1 -maxdepth 1` (rather than a `"$STATE_DIR"/*`
        # shell glob) lists every top-level entry including dotfiles, since
        # a plain glob doesn't match those without `dotglob`.
        while IFS= read -r entry; do
            [ -n "$entry" ] || continue
            base="$(basename "$entry")"
            case "$base" in
                backups) continue ;;
                observatory.db|observatory.db-wal|observatory.db-shm) continue ;;
            esac
            log "removing $entry"
            run rm -rf "$entry"
        done < <(find "$STATE_DIR" -mindepth 1 -maxdepth 1)
    fi
    log "state reset complete; backups/ and $CONFIG_PATH were preserved"
fi

# -----------------------------------------------------------------------
# State directory
# -----------------------------------------------------------------------
STATE_DIR_IS_NEW=0
if [ -d "$STATE_DIR" ]; then
    log "state directory $STATE_DIR already exists; leaving contents untouched"
else
    log "creating state directory $STATE_DIR"
    run mkdir -p "$STATE_DIR"
    STATE_DIR_IS_NEW=1
fi
run mkdir -p "$STATE_DIR/backups"
if [ "$STATE_DIR_IS_NEW" -eq 1 ]; then
    # First creation: recurse (covers the freshly made backups/ subdir too).
    run chown -R "$SERVICE_USER:$SERVICE_GROUP" "$STATE_DIR"
else
    # Pre-existing state dir: never recurse into (possibly large) existing
    # contents on every re-run; just make sure the top-level dirs are right.
    run chown "$SERVICE_USER:$SERVICE_GROUP" "$STATE_DIR"
    run chown "$SERVICE_USER:$SERVICE_GROUP" "$STATE_DIR/backups"
fi
run chmod 0750 "$STATE_DIR"

# -----------------------------------------------------------------------
# Radio device discovery
# -----------------------------------------------------------------------
discover_devices() {
    local candidates=()
    if compgen -G "/dev/serial/by-id/*" >/dev/null 2>&1; then
        for f in /dev/serial/by-id/*; do candidates+=("$f"); done
    fi
    if [ "${#candidates[@]}" -eq 0 ]; then
        for pat in /dev/ttyACM* /dev/ttyUSB*; do
            if compgen -G "$pat" >/dev/null 2>&1; then
                for f in $pat; do candidates+=("$f"); done
            fi
        done
    fi
    # Guard against an old-bash (e.g. macOS's stock bash 3.2) "unbound
    # variable" quirk when expanding an empty array under `set -u`.
    if [ "${#candidates[@]}" -gt 0 ]; then
        printf '%s\n' "${candidates[@]}"
    fi
}

SELECTED_DEVICE=""
if [ -n "$DEVICE_PATH" ]; then
    SELECTED_DEVICE="$DEVICE_PATH"
else
    # Built with a read loop rather than `mapfile` for portability to
    # older bash (e.g. macOS's stock bash 3.2, used during --dry-run
    # testing on a dev machine).
    CANDIDATES=()
    while IFS= read -r line; do
        [ -n "$line" ] && CANDIDATES+=("$line")
    done < <(discover_devices)
    if [ "${#CANDIDATES[@]}" -eq 1 ]; then
        SELECTED_DEVICE="${CANDIDATES[0]}"
        log "auto-selected radio device: $SELECTED_DEVICE"
    elif [ "${#CANDIDATES[@]}" -eq 0 ]; then
        warn "no serial device candidates found under /dev/serial/by-id, /dev/ttyACM*, or /dev/ttyUSB*; config will keep the device placeholder. Pass --device PATH once the radio is attached, or edit config.toml directly."
    else
        warn "multiple serial device candidates found; pass --device PATH to select one:"
        printf '  %s\n' "${CANDIDATES[@]}" >&2
    fi
fi

# -----------------------------------------------------------------------
# Config generation (never overwrites an existing config unless --force-config)
# -----------------------------------------------------------------------
run mkdir -p "$CONFIG_DIR"

# Renders config.example.toml to $1 with the device/state-dir paths
# substituted the same way for both the fresh-generate path below and
# config-sync's template, so the two never drift apart. Deliberately
# excludes the --web-listen [web] rewrite: that's an explicit config
# *change* (handled separately, via the manual-edit notice below when the
# config isn't being regenerated), not a missing-key default config-sync
# should silently add.
render_config_base_template() {
    sed \
        -e "s|/var/lib/meshtastic-observatory/observatory.db|$STATE_DIR/observatory.db|" \
        -e "s|/var/lib/meshtastic-observatory/backups|$STATE_DIR/backups|" \
        "$REPO_DIR/config.example.toml" > "$1"
    if [ -n "$SELECTED_DEVICE" ]; then
        sed -i.bak "s|/dev/serial/by-id/REPLACE_WITH_YOUR_RADIO|$SELECTED_DEVICE|" "$1"
        rm -f "$1.bak"
    fi
}

CONFIG_IS_NEW=0
if [ -e "$CONFIG_PATH" ] && [ "$FORCE_CONFIG" -ne 1 ]; then
    log "config already exists at $CONFIG_PATH; leaving it untouched (use --force-config to replace)"
    if [ -n "$WEB_LISTEN" ]; then
        warn "to enable the dashboard without regenerating the config, manually add/update these lines in $CONFIG_PATH:"
        warn "  [web]"
        warn "  enabled = true"
        warn "  listen = \"$WEB_LISTEN\""
        warn "  port = 9109"
        if ! is_loopback_addr "$WEB_LISTEN"; then
            warn "  allow_lan = true"
        fi
    fi
    if [ "$NO_CONFIG_SYNC" -eq 1 ]; then
        log "skipping config-sync (--no-config-sync): not checking $CONFIG_PATH for keys added since it was written"
    else
        SYNC_TMP_CONFIG="$(mktemp "${TMPDIR:-/tmp}/meshgarden-config-sync.XXXXXX")"
        render_config_base_template "$SYNC_TMP_CONFIG"
        sync_cmd=(
            "$REPO_DIR/.venv/bin/mgarden" config-sync
            --template "$SYNC_TMP_CONFIG" --config "$CONFIG_PATH"
        )
        if [ "$DRY_RUN" -eq 1 ]; then
            sync_cmd+=(--dry-run)
            log "running config-sync against $CONFIG_PATH (dry-run: preview only, nothing written)"
        else
            log "running config-sync against $CONFIG_PATH"
        fi
        # Non-fatal: a failure here (e.g. the existing config doesn't even
        # parse) must not abort the rest of the install, so this is
        # deliberately an `if`, not a bare `set -e`-tripping assignment.
        if sync_output="$("${sync_cmd[@]}" 2>&1)"; then
            sync_rc=0
        else
            sync_rc=$?
        fi
        while IFS= read -r sync_line; do
            [ -n "$sync_line" ] && log "config-sync: $sync_line"
        done <<< "$sync_output"
        if [ "$sync_rc" -ne 0 ]; then
            warn "config-sync exited with status $sync_rc; $CONFIG_PATH was left as found"
        fi
        rm -f "$SYNC_TMP_CONFIG"
    fi
elif [ -e "$CONFIG_PATH" ] && [ "$FORCE_CONFIG" -eq 1 ]; then
    backup="$CONFIG_PATH.bak.$(date +%Y%m%d%H%M%S)"
    log "backing up existing config to $backup before replacing"
    run cp -a "$CONFIG_PATH" "$backup"
    CONFIG_IS_NEW=1
else
    CONFIG_IS_NEW=1
fi

if [ "$CONFIG_IS_NEW" -eq 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        log "would generate $CONFIG_PATH from config.example.toml (dry-run: preview only, nothing written)"
        TMP_CONFIG="${TMPDIR:-/tmp}/meshgarden-config.preview"
    else
        log "generating $CONFIG_PATH from config.example.toml"
        TMP_CONFIG="$(mktemp "${TMPDIR:-/tmp}/meshgarden-config.XXXXXX")"
        render_config_base_template "$TMP_CONFIG"
        if [ -n "$WEB_LISTEN" ]; then
            # Add or update the [web] section
            # Remove existing [web] section if present
            sed -i.bak '/^\[web\]$/,/^$/d' "$TMP_CONFIG"
            rm -f "$TMP_CONFIG.bak"
            # Append the new [web] section at the end
            {
                echo ""
                echo "[web]"
                echo "enabled = true"
                echo "listen = \"$WEB_LISTEN\""
                echo "port = 9109"
                if ! is_loopback_addr "$WEB_LISTEN"; then
                    echo "allow_lan = true"
                fi
            } >> "$TMP_CONFIG"
        fi
    fi
    run install -o root -g "$SERVICE_GROUP" -m 0640 "$TMP_CONFIG" "$CONFIG_PATH"
    [ "$DRY_RUN" -eq 1 ] || rm -f "$TMP_CONFIG"
fi

# -----------------------------------------------------------------------
# expected_node_id auto-detection
# -----------------------------------------------------------------------
# Only attempted when: the config still holds the placeholder node id, a
# device was selected, the service is not currently running (so nothing
# else holds the port open), and this is not a dry run. Reading
# myInfo.my_node_num over SerialInterface issues a config request over
# USB only -- it never transmits over LoRa.
if [ "$DRY_RUN" -eq 0 ] && [ -n "$SELECTED_DEVICE" ] && [ -e "$CONFIG_PATH" ] \
    && grep -q '!REPLACE_WITH_NODE_ID' "$CONFIG_PATH" 2>/dev/null; then
    if systemctl is-active --quiet "$UNIT_NAME" 2>/dev/null; then
        warn "$UNIT_NAME is running; not probing $SELECTED_DEVICE for its node id. Stop the service and re-run, or edit expected_node_id in $CONFIG_PATH by hand."
    else
        log "reading node id from $SELECTED_DEVICE (read-only USB config request, no LoRa transmission)"
        NODE_ID="$(timeout 20 "$REPO_DIR/.venv/bin/python" -c "
import sys
from meshtastic.serial_interface import SerialInterface
iface = SerialInterface(devPath=sys.argv[1])
try:
    print('!{:08x}'.format(iface.myInfo.my_node_num))
finally:
    iface.close()
" "$SELECTED_DEVICE" 2>/dev/null || true)"
        if [ -n "$NODE_ID" ]; then
            log "detected node id $NODE_ID; writing to $CONFIG_PATH"
            run sed -i "s|!REPLACE_WITH_NODE_ID|$NODE_ID|" "$CONFIG_PATH"
        else
            warn "could not read the node id from $SELECTED_DEVICE. Edit expected_node_id in $CONFIG_PATH by hand (mgarden doctor will flag it until then)."
        fi
    fi
elif [ "$DRY_RUN" -eq 0 ] && [ -e "$CONFIG_PATH" ] && grep -q '!REPLACE_WITH_NODE_ID' "$CONFIG_PATH" 2>/dev/null; then
    warn "expected_node_id still a placeholder in $CONFIG_PATH; edit it by hand once you know the radio's node id (mgarden doctor will flag it)."
fi

# -----------------------------------------------------------------------
# Service-user traversal pre-flight
# -----------------------------------------------------------------------
# The unit's ProtectHome=read-only keeps the checkout visible to the
# service, but that's not enough on its own: the "meshgarden" service user
# still needs ordinary POSIX search/execute permission on every directory
# down to .venv/bin/mgarden. A checkout under a user's home directory
# (commonly mode 0700) blocks that, which would otherwise only surface as
# a unit that fails to start. `test -x` on the full path fails if any
# ancestor can't be traversed, so this one check covers both "is it
# executable" and "can the service user even reach it".

# Lists (one per line, root-to-leaf order not required) every directory from
# $1 up to but not including / that lacks the "other" execute bit -- the
# directories --fix-home-traversal would chmod. `find -perm -o+x` is used
# (rather than parsing `stat` output) because its syntax behaves identically
# under GNU find (Linux targets) and BSD find (macOS, used for --dry-run
# testing on a dev machine), and needs no root or specific user: the "other"
# bit is what --fix-home-traversal grants, regardless of which user
# (service user or otherwise) ends up relying on it.
non_o_x_ancestors() {
    local dir="$1"
    local out=()
    while [ "$dir" != "/" ] && [ -n "$dir" ]; do
        # `find`'s own exit status only reflects whether traversal errored,
        # not whether -perm matched -- an empty dir it can't error on
        # (-maxdepth 0 on a directory that exists) always exits 0 even with
        # zero matches. Check its OUTPUT instead: empty means no match,
        # i.e. the "other" execute bit is missing.
        if [ -z "$(find "$dir" -maxdepth 0 -perm -o+x 2>/dev/null)" ]; then
            out+=("$dir")
        fi
        dir="$(dirname "$dir")"
    done
    if [ "${#out[@]}" -gt 0 ]; then
        printf '%s\n' "${out[@]}"
    fi
}

PREFLIGHT_BIN="$REPO_DIR/.venv/bin/mgarden"
if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would run: runuser -u %s -- test -x %s\n' "$SERVICE_USER" "$PREFLIGHT_BIN"
    log "dry-run: skipping service-user traversal pre-flight"
    if [ "$FIX_HOME_TRAVERSAL" -eq 1 ]; then
        while IFS= read -r dir; do
            [ -n "$dir" ] || continue
            printf '[dry-run] would run: chmod o+x %s\n' "$dir"
        done < <(non_o_x_ancestors "$REPO_DIR")
    fi
elif runuser -u "$SERVICE_USER" -- test -x "$PREFLIGHT_BIN"; then
    log "$SERVICE_USER can traverse to and execute $PREFLIGHT_BIN"
elif [ "$FIX_HOME_TRAVERSAL" -eq 1 ]; then
    while IFS= read -r dir; do
        [ -n "$dir" ] || continue
        log "chmod o+x $dir"
        run chmod o+x "$dir"
    done < <(non_o_x_ancestors "$REPO_DIR")
    if runuser -u "$SERVICE_USER" -- test -x "$PREFLIGHT_BIN"; then
        log "$SERVICE_USER can traverse to and execute $PREFLIGHT_BIN (fixed via --fix-home-traversal)"
    else
        err "$SERVICE_USER still cannot execute $PREFLIGHT_BIN after --fix-home-traversal; a non-standard permission issue (ACLs, a mount option, SELinux, etc.) is likely involved."
        exit 1
    fi
else
    err "$SERVICE_USER cannot execute $PREFLIGHT_BIN -- a parent directory (commonly a home directory, mode 0700 by default) is blocking traversal for the service user, or the venv isn't there yet."
    err "fix: chmod o+x $REPO_HOME (and any other 0700 directories above the checkout) so $SERVICE_USER can reach it, move the checkout to /opt/meshgarden and re-run this installer from there, or re-run with --fix-home-traversal to apply the chmod automatically."
    exit 1
fi

# -----------------------------------------------------------------------
# systemd unit
# -----------------------------------------------------------------------
log "installing systemd unit to $UNIT_DEST"
# The unit's ExecStart= line ends in plain `run`; --debug appends ` --debug`
# to it and nothing else in the unit changes. Computed here (rather than a
# placeholder in the checked-in template) so the template stays valid
# as-is for the repo's unit-file tests.
# Guarded by [ -r "$UNIT_SRC" ]: some test harnesses exercise install-rpi.sh
# from a copied scripts/-only checkout without systemd/meshgarden.service
# present, which is fine since this preview is dry-run-only informational
# output, not required for the actual install (the "run install" below).
UNIT_EXEC_LINE=""
if [ -r "$UNIT_SRC" ]; then
    UNIT_EXEC_LINE="$(grep '^ExecStart=' "$UNIT_SRC")"
    UNIT_EXEC_LINE="${UNIT_EXEC_LINE//__CHECKOUT__/$REPO_DIR}"
    if [ "$DEBUG" -eq 1 ]; then
        UNIT_EXEC_LINE="$UNIT_EXEC_LINE --debug"
    fi
fi
if [ "$DRY_RUN" -eq 1 ]; then
    log "would render $UNIT_SRC (dry-run: preview only, nothing written)"
    if [ -n "$UNIT_EXEC_LINE" ]; then
        log "would render unit: $UNIT_EXEC_LINE"
    fi
    TMP_UNIT="${TMPDIR:-/tmp}/meshgarden-unit.preview"
else
    TMP_UNIT="$(mktemp "${TMPDIR:-/tmp}/meshgarden-unit.XXXXXX")"
    sed "s|__CHECKOUT__|$REPO_DIR|g" "$UNIT_SRC" > "$TMP_UNIT"
    if [ "$DEBUG" -eq 1 ]; then
        sed -i.bak '/^ExecStart=/ s/ run$/ run --debug/' "$TMP_UNIT"
        rm -f "$TMP_UNIT.bak"
    fi
fi
run install -o root -g root -m 0644 "$TMP_UNIT" "$UNIT_DEST"
[ "$DRY_RUN" -eq 1 ] || rm -f "$TMP_UNIT"

run systemctl daemon-reload

if [ "$NO_ENABLE" -eq 1 ]; then
    log "skipping systemctl enable (--no-enable)"
else
    run systemctl enable "$UNIT_NAME"
fi

if [ "$NO_START" -eq 1 ]; then
    log "skipping start/restart (--no-start)"
else
    if [ "$DRY_RUN" -eq 0 ] && systemctl is-active --quiet "$UNIT_NAME"; then
        run systemctl restart "$UNIT_NAME"
    else
        run systemctl start "$UNIT_NAME"
    fi
fi

# -----------------------------------------------------------------------
# System-wide CLI wrappers (/usr/local/bin/mgarden, /usr/local/bin/meshtastic-observatory)
# -----------------------------------------------------------------------
# Thin wrappers so the CLI works from any shell (`mgarden status`, `sudo
# mgarden backup`) without an operator needing to know the checkout path.
# These are deployment artifacts, not user data -- a re-run always
# regenerates and overwrites them, same as the systemd unit above.
render_cli_wrapper() {
    local bin_name="$1"
    cat <<WRAPPER_EOF
#!/usr/bin/env bash
# Generated by install-rpi.sh -- safe to overwrite; re-run the installer to
# regenerate. Execs the real entry point in the checkout's venv, inserting
# --config for the default install config unless the caller already passed
# --config/--config=... or no default config exists yet.
set -euo pipefail
BIN="$REPO_DIR/.venv/bin/$bin_name"
has_config=0
for arg in "\$@"; do
    case "\$arg" in
        --config|--config=*) has_config=1 ;;
    esac
done
if [ "\$has_config" -eq 0 ] && [ -e /etc/meshgarden/config.toml ]; then
    exec "\$BIN" --config /etc/meshgarden/config.toml "\$@"
fi
exec "\$BIN" "\$@"
WRAPPER_EOF
}

for bin_name in mgarden meshtastic-observatory; do
    wrapper_dest="/usr/local/bin/$bin_name"
    log "installing CLI wrapper to $wrapper_dest"
    if [ "$DRY_RUN" -eq 1 ]; then
        log "would render $wrapper_dest wrapper (dry-run: preview only, nothing written)"
        TMP_WRAPPER="${TMPDIR:-/tmp}/meshgarden-wrapper-$bin_name.preview"
    else
        TMP_WRAPPER="$(mktemp "${TMPDIR:-/tmp}/meshgarden-wrapper.XXXXXX")"
        render_cli_wrapper "$bin_name" > "$TMP_WRAPPER"
    fi
    run install -o root -g root -m 0755 "$TMP_WRAPPER" "$wrapper_dest"
    [ "$DRY_RUN" -eq 1 ] || rm -f "$TMP_WRAPPER"
done

# Only ever prints a dashboard URL when [web] was actually written to
# CONFIG_PATH just now (CONFIG_IS_NEW): if an existing config was left
# untouched (no --force-config), the manual-edit notice printed earlier
# (in the config-generation section above) is the only dashboard guidance
# given -- printing a URL here too would imply the dashboard is live when
# it may not be.
print_web_summary() {
    if [ -n "$WEB_LISTEN" ] && [ "$CONFIG_IS_NEW" -eq 1 ]; then
        if [ "$WEB_LISTEN" = "0.0.0.0" ]; then
            log "  dashboard: http://<pi-ip>:9109/ (replace <pi-ip> with your Raspberry Pi's IP address)"
        else
            log "  dashboard: http://$WEB_LISTEN:9109/"
        fi
        warn "dashboard is read-only and unauthenticated; expose only on a trusted LAN or behind a firewall"
    fi
}

# -----------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
    log "dry-run complete; no changes were made"
    print_web_summary
    exit 0
fi

STATUS_OK=1

if [ "$NO_ENABLE" -eq 0 ]; then
    if systemctl is-enabled --quiet "$UNIT_NAME"; then
        log "$UNIT_NAME is enabled"
    else
        err "$UNIT_NAME is not enabled"
        STATUS_OK=0
    fi
fi

if [ "$NO_START" -eq 0 ]; then
    sleep 1
    if systemctl is-active --quiet "$UNIT_NAME"; then
        log "$UNIT_NAME is active"
    else
        err "$UNIT_NAME is not active"
        STATUS_OK=0
    fi
fi

log "running mgarden doctor as $SERVICE_USER"
if runuser -u "$SERVICE_USER" -- "$REPO_DIR/.venv/bin/mgarden" --config "$CONFIG_PATH" doctor; then
    log "doctor checks passed"
else
    warn "doctor reported one or more failing checks (see above); fix them, then re-run: mgarden doctor"
fi

if [ "$STATUS_OK" -ne 1 ]; then
    err "installation finished with errors; recent logs:"
    journalctl -u "$UNIT_NAME" -n 20 --no-pager || true
    exit 1
fi

log "install complete."
log "  config:  $CONFIG_PATH"
log "  state:   $STATE_DIR"
log "  unit:    $UNIT_DEST"
log "  status:  sudo systemctl status $UNIT_NAME"
log "  logs:    journalctl -u $UNIT_NAME -f"
log "  mgarden status / mgarden doctor now work from any shell (/usr/local/bin/mgarden)"

print_web_summary
