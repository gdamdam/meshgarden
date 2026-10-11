"""External radio watchdog: pages the operator when the node goes deaf.

This exists because a dead radio is invisible from inside the daemon. The
reconnect thread stays perfectly healthy -- looping, beating and backing off
correctly -- with nothing to talk to, so worker heartbeats report all-clear
and systemd's `WatchdogSec` has nothing to trip on. The daemon now logs an
ERROR on a sustained outage (see `Daemon._report_radio_health`), but a log
line pages nobody.

So this runs *outside* the daemon, on a systemd timer, and polls the metrics
endpoint. That placement is the point: it catches a daemon that has died or
hung as well as one that is merely deaf, neither of which the daemon can
report on its own behalf.

Everything here is stdlib-only, and `decide()` is a pure function so every
anti-flap and power-cycle guard is testable without sleeping or touching
hardware.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from meshgarden.config import MetricsConfig

log = logging.getLogger(__name__)

# The gauge the daemon already exports; 1 while the serial interface is
# connected, 0 otherwise.
_GAUGE = "meshgarden_radio_connected"

_DEFAULT_GRACE_SECONDS = 300.0
_DEFAULT_MAX_CYCLES_PER_DAY = 3
_DEFAULT_CYCLE_COOLDOWN_SECONDS = 900.0

# Environment (set by the systemd unit's EnvironmentFile, which is where the
# ntfy URL belongs -- it is a shared secret and must never be committed, nor
# written into config.toml, which the service user can read).
ENV_NTFY_URL = "MESHGARDEN_NTFY_URL"
ENV_GRACE = "MESHGARDEN_WATCHDOG_GRACE_SECONDS"
ENV_MAX_CYCLES = "MESHGARDEN_WATCHDOG_MAX_CYCLES_PER_DAY"
ENV_COOLDOWN = "MESHGARDEN_WATCHDOG_CYCLE_COOLDOWN_SECONDS"
ENV_UHUBCTL_LOCATION = "MESHGARDEN_WATCHDOG_HUB"
ENV_UHUBCTL_PORT = "MESHGARDEN_WATCHDOG_HUB_PORT"
ENV_STATE_PATH = "MESHGARDEN_WATCHDOG_STATE"
ENV_FILE = "MESHGARDEN_WATCHDOG_ENV_FILE"

# Root-only file holding the ntfy URL, read by the systemd unit via
# EnvironmentFile= and, for `mgarden notify-test`, directly (which is why
# that command wants sudo).
DEFAULT_ENV_FILE = "/etc/meshgarden/watchdog.env"


@dataclass(frozen=True)
class Thresholds:
    grace_seconds: float = _DEFAULT_GRACE_SECONDS
    max_cycles_per_day: int = _DEFAULT_MAX_CYCLES_PER_DAY
    cycle_cooldown_seconds: float = _DEFAULT_CYCLE_COOLDOWN_SECONDS


@dataclass
class Decision:
    """What one poll concluded. `notifications` are (title, body) pairs."""

    state: dict[str, Any]
    notifications: list[tuple[str, str]] = field(default_factory=list)
    cycle: bool = False


def new_state() -> dict[str, Any]:
    """The empty state: nothing wrong, nobody paged, no cycles used."""
    return {
        "bad_since": None,
        "notified": False,
        "cycles": {},
        "last_cycle_at": None,
        # Latches the "hit the daily cap" page separately from `notified`,
        # which is already spent on the outage itself by the time the cap
        # is reached.
        "cap_notified": False,
        # Kernel boot id seen on the last poll, so the first poll after a
        # reboot (clean, power loss, or panic alike) can page about it.
        "boot_id": None,
        # Wall-clock of the last poll: the reboot page reports how long the
        # host was gone as "last seen ... back at ...".
        "last_seen_at": None,
    }


# -- metrics ------------------------------------------------------------


def metrics_url(cfg: MetricsConfig) -> str:
    """The URL to scrape. `0.0.0.0` (and `::`) are *bind* addresses, not
    destinations: the watchdog always runs on the same host, so those
    become loopback rather than being used verbatim."""
    host = cfg.listen
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"http://{host}:{cfg.port}/metrics"


def parse_radio_connected(text: str) -> bool | None:
    """Reads the `meshgarden_radio_connected` sample, or None if absent.

    None means "cannot tell" and is never treated as healthy -- a metrics
    body that does not carry the gauge indicates something other than a
    working radio. The name is matched exactly: `..._connected_seconds` is a
    different gauge that is nonzero *while connected*, so a prefix match
    would invert the reading.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        if name != _GAUGE:
            continue
        try:
            return float(value) != 0.0
        except ValueError:
            return None
    return None


def poll_status(url: str, *, timeout: float = 5.0, _open=None) -> str:
    """Returns "ok", "radio_down", or "unreachable".

    "unreachable" covers a daemon that is stopped, crashed, or hung hard
    enough not to serve metrics -- deliberately distinct from
    "radio_down", because only the latter is a power-cycle candidate.
    """
    opener = _open or urllib.request.urlopen
    try:
        with opener(url, timeout=timeout) as resp:  # noqa: S310 (fixed loopback URL)
            body = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return "unreachable"
    connected = parse_radio_connected(body)
    if connected is None:
        return "unreachable"
    return "ok" if connected else "radio_down"


# -- state file ---------------------------------------------------------


def load_state(path: str | Path) -> dict[str, Any]:
    """Reads the state file, falling back to a fresh state on anything
    unexpected. A corrupt or hand-edited file must never wedge the
    watchdog into silence -- losing the outage clock costs at most one
    extra grace period, while crashing costs every future alert.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return new_state()
    if not isinstance(raw, dict):
        return new_state()
    state = new_state()
    bad_since = raw.get("bad_since")
    last_cycle_at = raw.get("last_cycle_at")
    cycles = raw.get("cycles")
    if bad_since is not None and not isinstance(bad_since, (int, float)):
        return new_state()
    if last_cycle_at is not None and not isinstance(last_cycle_at, (int, float)):
        return new_state()
    if not isinstance(cycles, dict) or not all(
        isinstance(k, str) and isinstance(v, int) for k, v in cycles.items()
    ):
        return new_state()
    state["bad_since"] = float(bad_since) if bad_since is not None else None
    state["last_cycle_at"] = float(last_cycle_at) if last_cycle_at is not None else None
    state["notified"] = bool(raw.get("notified", False))
    state["cap_notified"] = bool(raw.get("cap_notified", False))
    state["cycles"] = dict(cycles)
    boot_id = raw.get("boot_id")
    last_seen_at = raw.get("last_seen_at")
    state["boot_id"] = boot_id if isinstance(boot_id, str) and boot_id else None
    state["last_seen_at"] = float(last_seen_at) if isinstance(last_seen_at, (int, float)) else None
    return state


def save_state(path: str | Path, state: Mapping[str, Any]) -> None:
    """Writes the state file atomically, owner-only (0600).

    Only the known keys are persisted, so nothing from the environment
    (least of all the ntfy URL) can end up on disk here.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: state.get(k) for k in new_state()}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


# -- configuration ------------------------------------------------------


def load_env_file(path: str | Path) -> dict[str, str]:
    """Parses a systemd-style `KEY=VALUE` env file. Missing or unreadable
    is `{}`, never an error: the watchdog must still run (and still
    power-cycle) when it cannot page.

    Deliberately minimal -- `export` prefixes, surrounding quotes, blank
    lines and `#` comments only. No variable expansion, no line
    continuations: this reads one operator-written file holding a URL, and
    a fuller shell parser would only add ways to misread it.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def effective_env(
    env: Mapping[str, str] | None = None, *, env_file: str | Path | None = None
) -> dict[str, str]:
    """The process environment layered over the env file.

    The process wins: under systemd the unit's `EnvironmentFile=` has
    already populated it, and an operator exporting a value by hand for a
    one-off `notify-test` should override the file.
    """
    env = os.environ if env is None else env
    path = env_file if env_file is not None else env.get(ENV_FILE, DEFAULT_ENV_FILE)
    merged = load_env_file(path)
    merged.update(env)
    return merged


def thresholds_from_env(env: Mapping[str, str]) -> Thresholds:
    """Reads the tunables, silently keeping the default for anything
    unparseable or negative: a typo in the env file must not stop the
    watchdog from watching."""

    def num(key: str, default: float) -> float:
        try:
            value = float(str(env[key]).strip())
        except (KeyError, TypeError, ValueError):
            return default
        return default if value < 0 else value

    return Thresholds(
        grace_seconds=num(ENV_GRACE, _DEFAULT_GRACE_SECONDS),
        max_cycles_per_day=int(num(ENV_MAX_CYCLES, _DEFAULT_MAX_CYCLES_PER_DAY)),
        cycle_cooldown_seconds=num(ENV_COOLDOWN, _DEFAULT_CYCLE_COOLDOWN_SECONDS),
    )


# -- the decision -------------------------------------------------------


def decide(
    state: Mapping[str, Any],
    *,
    status: str,
    now: float,
    today: str,
    cfg: Thresholds,
) -> Decision:
    """Pure: given the last state and this poll's status, what to do now.

    Anti-flap: nothing happens until the fault has persisted for
    `grace_seconds`, because the radio reboots (and so re-enumerates) on
    any config change from the phone app, and a down/up pair of pages for
    that is noise. A recovery inside the grace period is therefore silent
    too -- there was never an alert to retract.

    Escalation is latched: one page per outage, not one per poll.
    """
    new: dict[str, Any] = dict(new_state())
    new.update(state)
    new["cycles"] = dict(new.get("cycles") or {})
    decision = Decision(state=new)

    if status == "ok":
        if new["bad_since"] is not None and new["notified"]:
            decision.notifications.append(
                ("Meshgarden radio recovered", "The radio is connected again.")
            )
        new["bad_since"] = None
        new["notified"] = False
        new["cap_notified"] = False
        return decision

    # Still faulty. Keep the ORIGINAL start time: a daemon that comes back
    # while the radio stays dead is the same outage continuing, and
    # restarting the clock would delay recovery by another grace period.
    if new["bad_since"] is None:
        new["bad_since"] = now
        return decision

    if now - float(new["bad_since"]) < cfg.grace_seconds:
        return decision

    minutes = int((now - float(new["bad_since"])) // 60)
    if not new["notified"]:
        if status == "unreachable":
            decision.notifications.append(
                (
                    "Meshgarden daemon unreachable",
                    f"The metrics endpoint has been unreachable for {minutes} min. "
                    "The daemon may be stopped, crashed, or hung.",
                )
            )
        else:
            decision.notifications.append(
                (
                    "Meshgarden radio down",
                    f"The radio has been disconnected for {minutes} min. "
                    "Nothing can be received and no reply can be sent.",
                )
            )
        new["notified"] = True

    # Only a radio that is down while the daemon is up and reporting is a
    # power-cycle candidate. Cutting power cannot fix a stopped daemon --
    # which may well have been stopped deliberately for maintenance.
    if status != "radio_down" or cfg.max_cycles_per_day <= 0:
        return decision

    # Prune to today: the counter exists to cap cycles per day, so keeping
    # older days would grow the file without bound for no benefit.
    used = int(new["cycles"].get(today, 0))
    new["cycles"] = {today: used}

    last = new["last_cycle_at"]
    if last is not None and now - float(last) < cfg.cycle_cooldown_seconds:
        return decision

    if used >= cfg.max_cycles_per_day:
        if not new.get("cap_notified"):
            decision.notifications.append(
                (
                    "Meshgarden radio still down",
                    f"Reached the cap of {cfg.max_cycles_per_day} power cycles for "
                    "today and the radio is still down. Manual intervention needed.",
                )
            )
            new["cap_notified"] = True
        return decision

    new["cycles"][today] = used + 1
    new["last_cycle_at"] = now
    new["cap_notified"] = False
    decision.cycle = True
    decision.notifications.append(
        (
            "Meshgarden power-cycling the radio",
            f"Attempt {used + 1} of {cfg.max_cycles_per_day} today.",
        )
    )
    return decision


# -- effects ------------------------------------------------------------


# -- reboot detection ----------------------------------------------------

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

# What the previous boot's journal is grepped for, restricted to PID 1,
# logind and the kernel (see `_CLUE_SOURCES`): systemd logs the target it
# reached and logind who asked for it; the kernel logs undervoltage,
# throttling, OOM kills and panics. The pattern is mixed-case on purpose --
# journalctl -g is case-sensitive then -- and names exact messages, because
# a loose "shutdown|thermal" matched boot-time noise ("Record System
# Boot/Shutdown in UTMP", "Registered thermal governor") and the daemon's
# own "shutting down" lines from ordinary restarts.
_SHUTDOWN_CLUES = (
    r"Reached target (reboot|poweroff|halt|shutdown)\.target|"
    r"System is (rebooting|powering down|halting)|will (reboot|power off|halt) now|"
    r"requested by|Under-?voltage|throttl|Kernel panic|oom-kill|Out of memory"
)
# journalctl match groups separated by "+" are OR-ed.
_CLUE_SOURCES = ["_PID=1", "+", "_TRANSPORT=kernel", "+", "_COMM=systemd-logind"]
_MAX_CLUE_LINES = 12
_MAX_CLUE_CHARS = 160


def read_boot_id(path: str | Path = BOOT_ID_PATH) -> str | None:
    """The kernel's random per-boot id, or None where it cannot be read."""
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def note_boot(
    state: Mapping[str, Any], *, boot_id: str | None, now: float
) -> tuple[dict[str, Any], bool]:
    """Pure: records this boot and reports whether it is a NEW one.

    The very first poll ever (no boot id on record) only records, so
    installing the watchdog never pages about a reboot that did not
    happen. An unreadable boot id is treated as "unknown" and never
    pages either -- a page on every poll would be worse than none.
    """
    new = dict(state)
    previous = state.get("boot_id")
    new["last_seen_at"] = now
    if boot_id is None:
        return new, False
    new["boot_id"] = boot_id
    return new, previous is not None and previous != boot_id


def previous_boot_clues(
    *, _run: Callable[..., Any] | None = None, timeout: float = 15.0
) -> list[str]:
    """Lines from the PREVIOUS boot's journal that hint at why it ended.

    Never raises: a volatile journal (no `-b -1`), a corrupted journal file
    after a power loss, or a missing journalctl all simply yield no clues.
    """
    cmd = [
        "journalctl",
        "-b",
        "-1",
        "-q",
        "--no-pager",
        "-o",
        "short-iso",
        "-n",
        str(_MAX_CLUE_LINES),
        "-g",
        _SHUTDOWN_CLUES,
        *_CLUE_SOURCES,
    ]
    try:
        result = (_run or subprocess.run)(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("journalctl failed: %s: %s", type(exc).__name__, exc)
        return []
    if result.returncode != 0:
        log.warning("journalctl exited %s: %s", result.returncode, (result.stderr or "").strip())
        return []
    lines = [ln.strip()[:_MAX_CLUE_CHARS] for ln in (result.stdout or "").splitlines()]
    # short-iso lines sort chronologically; a journal left inconsistent by
    # a power cut has been seen to come back newest-first.
    return sorted(ln for ln in lines if ln)[-_MAX_CLUE_LINES:]


def reboot_notification(
    *, now: float, last_seen_at: float | None, clues: list[str]
) -> tuple[str, str]:
    """(title, body) for the "host rebooted" page."""
    fmt = "%Y-%m-%d %H:%M"
    back = datetime.fromtimestamp(now).strftime(fmt)
    if last_seen_at is None:
        gone = "Last alive: unknown."
    else:
        minutes = max(0, int((now - last_seen_at) // 60))
        seen = datetime.fromtimestamp(last_seen_at).strftime(fmt)
        gone = f"Last alive {seen}, gone about {minutes} min."
    if clues:
        why = "Previous boot's journal, lines mentioning shutdown/power:\n" + "\n".join(clues)
    else:
        why = (
            "No shutdown, reboot or power messages in the previous boot's "
            "journal (or it is unavailable): likely a power cut or a hard hang."
        )
    return (
        "Meshgarden host rebooted",
        f"The host came back at {back}. {gone}\n{why}",
    )


def notify(
    url: str,
    title: str,
    body: str,
    *,
    priority: str = "default",
    timeout: float = 10.0,
    _post: Callable[..., Any] | None = None,
) -> bool:
    """POSTs one notification. Returns whether it was delivered.

    Never raises: a dead notification channel must not stop the watchdog,
    or a transient ntfy outage would also disable the power-cycle
    recovery. An unset URL is a no-op, so an unconfigured watchdog still
    recovers the radio -- it just cannot page.
    """
    if not url:
        return False
    headers = {"Title": title, "Priority": priority, "Tags": "seedling"}
    post = _post or _http_post
    try:
        post(url, data=body.encode("utf-8"), headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        log.warning("watchdog notification failed: %s: %s", type(exc).__name__, exc)
        return False
    return True


def _http_post(url: str, data: bytes, headers: dict[str, str], timeout: float) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout):  # noqa: S310 (operator-set URL)
        pass


def cycle_radio(
    *,
    hub: str,
    port: str,
    delay_seconds: int = 3,
    _run: Callable[..., Any] | None = None,
) -> bool:
    """Power-cycles the radio's USB port via uhubctl. Returns success.

    Never raises, for the same reason as `notify`: this runs unattended on
    a timer and must always live to poll again.
    """
    cmd = [
        "uhubctl",
        "-l",
        hub,
        "-p",
        port,
        "-a",
        "cycle",
        "-d",
        str(delay_seconds),
    ]
    runner = _run or subprocess.run
    try:
        result = runner(cmd, capture_output=True, text=True, timeout=60, check=False)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        log.error("uhubctl failed to run: %s: %s", type(exc).__name__, exc)
        return False
    if result.returncode != 0:
        log.error("uhubctl exited %s: %s", result.returncode, (result.stderr or "").strip())
        return False
    log.warning("power-cycled the radio on hub %s port %s", hub, port)
    return True


def run_once(
    *,
    metrics: MetricsConfig,
    state_path: str | Path,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
    _poll: Callable[..., str] | None = None,
    _notify: Callable[..., bool] | None = None,
    _cycle: Callable[..., bool] | None = None,
    _boot_id: Callable[[], str | None] | None = None,
    _clues: Callable[[], list[str]] | None = None,
) -> str:
    """One poll: read status, decide, act, persist. Returns the status."""
    env = os.environ if env is None else env
    cfg = thresholds_from_env(env)
    now = time.time() if now is None else now
    url = metrics_url(metrics)
    status = (_poll or poll_status)(url)

    state = load_state(state_path)
    last_seen_at = state.get("last_seen_at")
    state, rebooted = note_boot(state, boot_id=(_boot_id or read_boot_id)(), now=now)
    decision = decide(
        state,
        status=status,
        now=now,
        today=datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
        cfg=cfg,
    )

    # Persist BEFORE acting: a crash mid-power-cycle must not re-run the
    # cycle on the next tick with the counter unincremented.
    save_state(state_path, decision.state)

    ntfy_url = env.get(ENV_NTFY_URL, "")
    if rebooted:
        # First, so the cause (a reboot) precedes any outage it explains.
        decision.notifications.insert(
            0,
            reboot_notification(
                now=now, last_seen_at=last_seen_at, clues=(_clues or previous_boot_clues)()
            ),
        )
    for title, body in decision.notifications:
        (_notify or notify)(
            ntfy_url, title, body, priority="high" if "recovered" not in title else "default"
        )
    if decision.cycle:
        (_cycle or cycle_radio)(
            hub=env.get(ENV_UHUBCTL_LOCATION, "1-1"),
            port=env.get(ENV_UHUBCTL_PORT, "4"),
        )
    return status
