"""Daemon wiring: threads, queues, and the DM command dispatch loop.

This module owns the process lifecycle. Three threads run:

- the meshtastic pubsub/library thread (owned by RadioAdapter), whose sink
  callback here does nothing but a bounded, nonblocking enqueue;
- one DB worker thread that drains the ingest queue and performs every
  daemon SQLite write (collector.record, request log, sender.enqueue,
  transport.note_routing_packet -> sender.on_delivery), plus the periodic
  retention/backup/metrics maintenance tick;
- one TX worker thread that loops Sender.pump().

Every sqlite3.Connection is owned by exactly one thread (Python's
check_same_thread default is never relaxed): the main/constructing thread
gets its own connection (`self.conn`, used for startup migration/cleanup and
exposed for external reads such as the CLI and tests); the DB worker thread
opens its own connection at the top of its run function and uses it for
everything that thread touches; the TX worker thread does the same for
Sender.pump()'s state transitions. WAL mode + busy_timeout, not a relaxed
same-thread guard, is what makes this safe across connections to one file.

On Meshtastic, the daemon never sends anything unsolicited: no startup
announcement, no periodic advertisement, no heartbeat. On MeshCore only, it
sends a periodic flood self-advertisement (once per connection, then every
`meshcore_advert_interval_seconds`) so other nodes can resolve a path back
to it -- presence beaconing, not a reply. Otherwise, the only replies that
ever reach Sender.enqueue are direct responses to an eligible, parsed DM
command, plus (documented exception) the weather-alert channel broadcasts.

The CLI (argparse entry point) arrives in a later task; this module exposes
only the Daemon class the CLI will drive.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import random
import re
import signal
import socket
import sqlite3
import sys
import textwrap
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from meshgarden import (
    __version__,
    advert_control,
    bbs_ids,
    bbs_store,
    booktrivia,
    cli_ops,
    community,
    digest,
    exchange_log,
    moderation,
    ops,
    positions,
    updates,
    usefulness,
    watchdog,
    web,
)
from meshgarden.booktrivia import (
    book_prompt_text,
    book_score_text,
    book_stats_summary,
    book_wrong_text,
    guess_matches,
)
from meshgarden.booktrivia import (
    puzzle_for as book_puzzle_for,
)
from meshgarden.collector import Collector
from meshgarden.commands import (
    LAB_COMMANDS,
    LAB_HELP_WORDS,
    LAB_MENU_PAGES,
    ChannelKnockCooldown,
    Command,
    CommandCooldownNotices,
    Outcome,
    ParsedCommand,
    Reject,
    RequestLog,
    _relative_ago,
    boards_text,
    channel_menu_text,
    decide_inbound,
    garden_text,
    help_text,
    info_text,
    is_blocked,
    knock_text,
    lab_text,
    mail_alias_text,
    mail_page,
    mail_pref_ack_text,
    mail_sent_ack_text,
    me_text,
    menu_reply_for,
    menu_text,
    not_emergency_text,
    parse_command,
    parse_position_text,
    ping_text,
    position_ack_text,
    record_text,
    repeater_text,
    seen_ambiguous_text,
    seen_not_heard_text,
    seen_text,
    suggest_command,
    version_text,
    week_text,
    who_text,
    with_quota_hint,
)
from meshgarden.config import (
    Config,
    ConfigError,
    load_config,
    node_id_from_num,
    parse_node_id_argument,
)
from meshgarden.contacts import select_contacts_to_prune
from meshgarden.database import connect, get_daemon_state, migrate, set_daemon_state
from meshgarden.diagnostics import route_reply
from meshgarden.fortune import (
    haiku_text,
    iching_text,
    local_day,
    local_iso,
    local_midnight,
)
from meshgarden.guestbook import (
    Guestbook,
    SignError,
    fallback_display_name,
    format_board,
    format_board_no_more_entries,
    format_guestbook,
    format_guestbook_entry,
    format_guestbook_no_more_entries,
    format_guestbook_not_found,
    format_sign_cooldown,
    format_sign_duplicate,
    format_sign_empty,
    format_sign_quota,
    format_sign_too_long,
    format_signed_ack,
    format_stats,
    format_storage_full,
    format_unsign_hidden,
    format_unsign_nothing,
    lookup_bbs_node_name,
    lookup_node_name,
    validate_topic,
)
from meshgarden.identity import bind_meshcore_identity
from meshgarden.lab import Lab
from meshgarden.mail import Mail, MailAliases, MailPrefs, normalize_mail_alias
from meshgarden.meshcore_client import build_meshcore_transport
from meshgarden.meshcore_transport import MeshCoreTransport
from meshgarden.meshtastic_transport import MeshtasticTransport
from meshgarden.metrics import (
    Metrics,
    bounded_channel,
    bounded_portnum,
    start_metrics_server,
)
from meshgarden.models import IncomingPacket, MeshCoreObservation, MeshCoreRoute, ReplyJob
from meshgarden.moon import moon_text
from meshgarden.quota import DailyQuota
from meshgarden.radio import RadioAdapter, as_inbound
from meshgarden.retention import Retention, week_summary
from meshgarden.sender import RateLimiter, ReplyQueue, Sender, truncate_utf8
from meshgarden.transport import ChannelScope, ChannelTriggerPolicy, Delivery, Inbound, Transport
from meshgarden.weather import (
    WeatherBatch,
    WeatherLedger,
    WeatherPoller,
    effective_aliases,
    wx_status_text,
)
from meshgarden.wordle import play as wordle_play
from meshgarden.wordle import prune_wordle_state, wordle_stats_summary

log = logging.getLogger(__name__)

_MESH_LABELS = {"meshcore": "MeshCore", "meshtastic": "Meshtastic"}

_CHANNEL_PUBLIC_COMMANDS = frozenset(
    {
        Command.MENU,
        Command.NEWS,
        Command.GUIDE,
        Command.NOT_EMERGENCY,
        Command.INFO,
        Command.GUESTBOOK,
        Command.STATS,
        Command.REPEATER,
        Command.ICHING,
        Command.HAIKU,
        Command.GARDEN,
        Command.RECORD,
        Command.WEEK,
        Command.PING,
        Command.WX,
        Command.MOON,
        Command.SUN,
        Command.READ,
        Command.BOARDS,
        Command.CMD_HELP,
        Command.VERSION,
    }
)

_INGEST_QUEUE_MAXSIZE = 1000
_MAINTENANCE_INTERVAL_SECONDS = 60
_STATUS_FILE_INTERVAL_SECONDS = 10
_STATUS_FILE_STALE_SECONDS = 60  # CLI treats an older/missing file as "daemon not running"
_STATUS_FILE_NAME = "status.json"
_DB_LOOP_POLL_SECONDS = 0.2  # ingest.get(timeout=...) lets the maintenance tick fire w/o traffic
_TX_LOOP_POLL_SECONDS = 0.1
_THREAD_READY_TIMEOUT_SECONDS = 10.0

# SQLite INTEGER columns are 64-bit signed: an entry_id past this can never
# be a real guestbook row (AUTOINCREMENT never produces one), and binding
# it into a query raises OverflowError rather than just finding no match.
_SQLITE_MAX_INT = 2**63 - 1
_STOP_SENTINEL = object()

# check_eligibility's ineligible reasons all fold into the bounded "UNKNOWN"
# command bucket; only NOT_REPLY_READY and STALE map to their own outcome
# bucket, since metrics.py preallocates "not_ready" specifically for "we
# can't reply right now" (identity mismatch / disconnected / stale replay),
# distinct from a plain reject.
_ELIGIBILITY_OUTCOME: dict[Reject, str] = {
    Reject.NOT_REPLY_READY: "not_ready",
    Reject.STALE: "not_ready",
}

# `Guestbook.validate` (shared by SIGN and POST) only ever raises SignError
# with SIGN_EMPTY/SIGN_TOO_LONG -- `_handle_post` remaps those to its own
# POST_EMPTY/POST_TOO_LONG reason codes so a POST failure is audited under a
# POST code, never a SIGN one. Any other SignError reason (there are none
# today) passes through unchanged.
_POST_SIGN_ERROR_REASON: dict[Reject, Reject] = {
    Reject.SIGN_EMPTY: Reject.POST_EMPTY,
    Reject.SIGN_TOO_LONG: Reject.POST_TOO_LONG,
}

# Same remap, for `_handle_mail_send`'s call into the same shared
# `Guestbook.validate` -- a MAIL failure must be audited under a MAIL code,
# never a SIGN one, mirroring `_POST_SIGN_ERROR_REASON` exactly (fix round
# 1, review finding).
_MAIL_SIGN_ERROR_REASON: dict[Reject, Reject] = {
    Reject.SIGN_EMPTY: Reject.MAIL_EMPTY,
    Reject.SIGN_TOO_LONG: Reject.MAIL_TOO_LONG,
}

_STORAGE_PRESSURE_LEVEL = {"ok": 0, "warn": 1, "critical": 2}

# Worker heartbeat stall thresholds (Heartbeats, below): the four fixed
# worker names ("db", "tx", "radio", "maintenance") plus "weather" when
# `[weather] enabled` are also the only
# `worker` label values metrics.py's meshgarden_worker_heartbeat_age_seconds
# gauge ever takes.
_WORKER_DB_TX_STALL_SECONDS = 30.0
_WORKER_RADIO_MIN_STALL_SECONDS = 60.0
_WORKER_MAINTENANCE_STALL_MULTIPLIER = 3  # x _MAINTENANCE_INTERVAL_SECONDS

# How long the radio may stay disconnected before the daemon escalates to
# ERROR (see Daemon._report_radio_health). Deliberately longer than a normal
# reconnect -- the radio reboots, and so re-enumerates, on any config change
# from the phone app -- but far shorter than the 85 minutes an operator once
# spent unaware the node had gone deaf.
_RADIO_DISCONNECTED_ALERT_SECONDS = 300.0

_logging_configured = False


class Heartbeats:
    """Thread-safe last-beat registry for the daemon's four bounded worker
    names: "db" (DB worker loop), "tx" (TX worker loop), "radio" (the
    RadioAdapter reconnect thread), "maintenance" (the periodic maintenance
    tick, which runs inside the DB worker loop rather than its own thread).

    Each worker calls `beat(name)` on every pass through its own loop (the
    maintenance tick calls it once per completed tick, not every DB-worker
    iteration). `snapshot()` -- consulted by `status_snapshot()`, the
    stall-transition warning log in `write_status_file()`, the
    `meshgarden_worker_heartbeat_age_seconds` gauge, and the systemd
    watchdog ping in `run_forever` -- reports each worker's heartbeat age
    against its own fixed threshold.

    Uses the caller-supplied clock (the daemon's own monotonic clock, in
    production and in tests) rather than `time.monotonic` directly, so a
    test can simulate a stalled worker by simply not calling `beat()` again
    and advancing that clock via `Daemon.advance_seconds` -- no real sleep.
    """

    def __init__(self, thresholds: dict[str, float], *, clock: Callable[[], float]) -> None:
        self._thresholds = dict(thresholds)
        self._clock = clock
        self._lock = threading.Lock()
        now = clock()
        self._last: dict[str, float] = dict.fromkeys(thresholds, now)

    def beat(self, name: str) -> None:
        with self._lock:
            self._last[name] = self._clock()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = self._clock()
        with self._lock:
            last = dict(self._last)
        result: dict[str, dict[str, Any]] = {}
        for name, threshold in self._thresholds.items():
            age = max(0.0, now - last[name])
            stalled = age > threshold
            result[name] = {
                "age_seconds": round(age, 3),
                "alive": not stalled,
                "stalled": stalled,
            }
        return result

    def any_stalled(self) -> bool:
        return any(info["stalled"] for info in self.snapshot().values())


class _Clock:
    """Wall + monotonic clock pair, real by default.

    `advance()` (see `Daemon.advance_seconds`, TEST SUPPORT ONLY) moves both
    forward together without sleeping, so RateLimiter windows, ReplyJob TTL
    deadlines, and DB timestamps (created_at / first_seen_at, re-stamped onto
    every observation in `Daemon._ingest_sink`) all stay consistent with each
    other. In production the offset never changes, so this is exactly
    time.time()/time.monotonic().
    """

    def __init__(self) -> None:
        self._offset = 0.0

    def time(self) -> float:
        return time.time() + self._offset

    def monotonic(self) -> float:
        return time.monotonic() + self._offset

    def advance(self, seconds: float) -> None:
        self._offset += seconds


def _sd_notify(state: str) -> None:
    """Sends one systemd `sd_notify` datagram to `$NOTIFY_SOCKET`, or does
    nothing if that env var is unset (the common case: not running under
    systemd, or a unit without `Type=notify`).

    Implements the (tiny) wire protocol directly -- one `AF_UNIX
    SOCK_DGRAM` datagram, the message ASCII-encoded -- rather than adding
    the optional `sdnotify` package as a new dependency for what is, per
    systemd's own `sd_notify(3)` docs, about 20 lines. `@`-prefixed
    addresses name the Linux abstract namespace (a leading NUL byte instead
    of the `@`), same as systemd's own convention. Never raises: a failed
    notify (missing socket, closed at the other end, ...) must never crash
    the daemon over what is purely a liveness signal to systemd.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.connect(addr)
        sock.sendall(state.encode("ascii"))
    except OSError:
        log.warning("sd_notify failed to send %r", state)
    finally:
        sock.close()


def _watchdog_interval_seconds() -> float | None:
    """`WATCHDOG_USEC / 2`, systemd's own recommended margin -- `None` if
    `NOTIFY_SOCKET` or `WATCHDOG_USEC` is unset, empty, or not a positive
    integer, in which case `run_forever` never pings the watchdog (and
    plain `Type=simple`/no watchdog units are completely unaffected)."""
    if not os.environ.get("NOTIFY_SOCKET"):
        return None
    raw = os.environ.get("WATCHDOG_USEC")
    if not raw:
        return None
    try:
        usec = int(raw)
    except ValueError:
        return None
    if usec <= 0:
        return None
    return usec / 1_000_000 / 2


def _database_dir(cfg: Config) -> str:
    return os.path.dirname(os.path.abspath(cfg.database.path))


def _status_file_path(cfg: Config) -> str:
    return os.path.join(_database_dir(cfg), _STATUS_FILE_NAME)


def _ensure_dir_owner_only(path: str) -> None:
    """Creates `path` (and parents) at mode 0700 if it doesn't already
    exist. A directory that already exists (e.g. systemd's own
    StateDirectory, which manages its own mode) is left untouched -- this
    only ever tightens a directory THIS process creates itself. Belt and
    braces alongside the unit's UMask=0077: `os.makedirs`'s `mode` argument
    alone is still subject to the ambient umask, so the explicit `chmod`
    is what actually guarantees 0700 regardless of umask.
    """
    if os.path.isdir(path):
        return
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _create_owner_only_file(path: str) -> None:
    """Pre-creates `path` at mode 0600 (a no-op if it already exists --
    O_CREAT | O_APPEND never truncates) before any `logging.FileHandler`
    ever opens it, so the file is never briefly readable at the ambient
    umask's default mode. `os.open`'s own `mode` argument is still subject
    to the ambient umask (same caveat as `_ensure_dir_owner_only`'s
    `os.makedirs`), so the explicit `os.chmod` afterwards is what actually
    guarantees 0600 regardless of umask."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)


class _SecureRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A RotatingFileHandler that re-chmods 0600 every file it opens.

    The stock handler is only ever chmod'ed once, by its caller, right
    after construction -- the FRESH active file `doRollover` opens after
    rotating is never re-chmod'ed, so it would be left at the ambient
    umask's default mode. Overriding `_open` (the one method both the
    initial open and every rollover's reopen funnel through) closes that
    gap in a single place. The rotated `.1`/`.2` backups need no separate
    handling: `doRollover` produces them with `os.rename`, which preserves
    the source file's mode, and the source was always opened through this
    same override.
    """

    def _open(self):
        stream = super()._open()
        os.fchmod(stream.fileno(), 0o600)
        return stream


def _configure_logging(log_dir: str) -> None:
    """Rotating file handler (10 MiB x 5) under log_dir, plus stderr.

    Idempotent at process scope: a process only ever gets one daemon, so
    this only needs to run once even though tests construct many Daemons.
    """
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True
    _ensure_dir_owner_only(log_dir)
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "meshgarden.log"), maxBytes=10 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def _seen_label(conn: sqlite3.Connection, node_id: str, transport: Transport) -> str:
    """"<name> · <tail>" (or the bare tail) from `conn`'s own `nodes` table."""
    name = lookup_node_name(conn, node_id)
    tail = transport.short_label(node_id)
    return f"{name} · {tail}" if name else tail


def _node_label_for(raw_argument: str, transport: Transport) -> str:
    """SEEN's not-found/not-heard label for an UNRESOLVED argument: the
    tail when `raw_argument` itself already looks like a full id (in the
    running transport's own shape, via `transport.parse_node_argument`) or
    a bare 4-hex tail, else a bland "that node" placeholder -- never echoes
    an arbitrary short-name guess back to the sender (no-echo convention,
    ruling 1).

    Deliberately derived from the ARGUMENT's shape, not from a resolved
    node id (see the call site's own comment, main.py ~2440, and
    `test_seen_stale_short_name_matches_the_never_seen_reply_byte_for_byte`):
    a short name that resolves but hasn't been heard recently must produce
    the exact same reply as an unrecognized short name. `transport.
    parse_node_argument` is used only as the shape TEST here, never for its
    canonicalized (lowercased) return value -- `transport.short_label` is
    then applied to the ORIGINAL `text`, so a mixed-case argument keeps
    rendering in the case the visitor typed, exactly as before this was
    routed through the transport (task 3D)."""
    text = raw_argument.strip()
    is_tail = re.match(r"^[0-9a-fA-F]{4}$", text) is not None
    if transport.parse_node_argument(text) is not None or is_tail:
        return transport.short_label(text)
    return "that node"


@dataclass(frozen=True)
class _MeshCoreContact:
    """One `NEW_CONTACT` event's identity payload, queued through
    `Daemon._ingest` exactly like an `IncomingPacket`/`Inbound`/`Delivery`
    (task 3B-2 item F): arrives on the transport's pump thread via
    `Daemon._meshcore_contact_sink` (bounded, nonblocking enqueue only) and
    is applied to the database on the DB worker thread by
    `Daemon._process_meshcore_contact` -- the single owner of that
    connection is never crossed.
    """

    node_id: str
    display_name: str | None
    lat: float | None
    lon: float | None
    observed_at: int


@dataclass(frozen=True)
class _WeatherDropped:
    """A weather ReplyJob that settled to a DEFINITE pre-transmission
    failure. Built on the TX worker thread (Sender's release callback),
    processed on the DB worker thread, exactly like _KnockCooldownRelease."""

    product_key: str


@dataclass(frozen=True)
class _KnockCooldownRelease:
    """A channel knock's cooldown-claim release, marshaled through
    `Daemon._ingest` onto the DB worker thread -- exactly like
    `Delivery`/`_MeshCoreContact` already are.

    Fix round 2: the first cut of the knock's compensating-action release
    (task 3B-3, fix round 1, item 2) called `ChannelKnockCooldown.release`
    directly from inside `Sender._finish_expired`/`_mark_dropped`'s
    callback -- but those run on the TX WORKER thread (see `Sender`'s own
    module docstring), while `ChannelKnockCooldown` is built on
    `self._db_conn`, owned by the DB WORKER thread. That raised every
    time (`check_same_thread` is never relaxed), and the escaping
    exception could skip an unrelated ready job's `ReplyQueue.release()`
    entirely, wedging that OTHER node's replies for the life of the
    process (see `Sender.pump`'s hardened expired-jobs loop, added in the
    same fix round, as the general-purpose backstop for that hazard).
    `Daemon._enqueue_knock_release` is the only thing allowed to
    construct this; `Daemon._process_knock_cooldown_release` is the only
    thing allowed to consume it, on the DB worker thread, where `self.
    _knock_cooldown` actually lives.
    """

    node_id: str
    claimed_at: int


@dataclass(frozen=True)
class _RadioLikeStatus:
    """The connectivity fields `status_snapshot`/`write_status_file`/
    `_report_radio_health`/`_radio_disconnected_seconds` need, normalized
    from EITHER `RadioAdapter.status()` (Meshtastic, unchanged) or the
    generic `Transport.status()` plus a little daemon-side bookkeeping
    (MeshCore, which has no `RadioStatus`-shaped object at all -- task
    3B-2 item C). See `Daemon._current_status`."""

    connected: bool
    reply_ready: bool
    actual_node_id: str | None
    expected_node_id: str
    mismatch_reason: str | None
    last_packet_at: int | None
    disconnected_since_monotonic: float | None


class Daemon:
    def __init__(
        self,
        cfg: Config,
        *,
        interface_factory: Callable[[str], Any] | None = None,
        transport: Transport | None = None,
        start_metrics: bool = True,
        haiku_rng: random.Random | random.SystemRandom | None = None,
        debug: bool = False,
        weather_poller: bool = True,
    ) -> None:
        self.cfg = cfg
        self._clock = _Clock()
        # Operator opt-in only (`run --debug`): prints a DM/reply exchange
        # trace to stderr via the dedicated exchange_log logger, and (once
        # the log dir exists -- see below) also appends it to
        # ops.exchange_trace_path(cfg), a small rotated file under the logs
        # dir, so `mgarden tail` has a source to follow. Never reaches the
        # rotating daemon log, the database, metrics, or the status file --
        # see exchange_log.py and _process_obs/sender.py's own call sites.
        self._debug = debug
        self._exchange_handlers: list[logging.Handler] = []
        stream_handler = exchange_log.configure(debug)
        if stream_handler is not None:
            self._exchange_handlers.append(stream_handler)
        if debug:
            log.warning(
                "DEBUG MODE ENABLED: this run prints DM and reply contents to "
                "stderr; never use --debug for an unattended/systemd deployment"
            )
        # TEST SUPPORT: HAIKU's random template selection uses this if
        # given (a seeded random.Random), else a fresh SystemRandom() at
        # call time. ICHING is deliberately NOT randomized -- it is a pure
        # function of (sender, local day, salt); see fortune.py.
        self._haiku_rng = haiku_rng
        self.started_at = int(time.time())
        self._stopped = False
        self._stop_event = threading.Event()

        # Loaded once at startup (main/constructing thread; a frozenset
        # needs no DB connection): SIGN's banned-word guard. Empty when no
        # banned_words_file is configured, so the guard is a no-op.
        self._banned_terms: frozenset[str] = (
            moderation.load_banned_terms(cfg.moderation.banned_words_file)
            if cfg.moderation.banned_words_file
            else frozenset()
        )

        # In-memory ring of recent rate-limit-style drop timestamps
        # (cooldown, quota, sign_quota, queue_full), for status_snapshot's
        # rate_limited_drops_1h summary. Bounded: _note_drop/
        # _rate_limited_drops_1h both prune anything older than 1h.
        self._drop_events: deque[int] = deque()

        # Create the state directory itself, owner-only, before anything
        # else (logging's own makedirs, or database.connect()'s) can
        # create it first under the ambient umask: whichever of the two
        # ran first would otherwise "win" and leave the other's own
        # is-it-new-and-therefore-mine-to-chmod check permanently False.
        db_dir = _database_dir(cfg)
        _ensure_dir_owner_only(db_dir)

        self._log_dir = os.path.join(db_dir, "logs")
        _configure_logging(self._log_dir)

        if debug:
            trace_path = ops.exchange_trace_path(cfg)
            # _configure_logging (above) only actually creates self._log_dir
            # the FIRST time any Daemon runs in this process (see its own
            # _logging_configured guard) -- never rely on that side effect
            # for a directory a later Daemon in the same process needs.
            _ensure_dir_owner_only(os.path.dirname(trace_path))
            # Pre-create the file at 0600 ourselves, THEN hand it to the
            # handler with delay=True: constructing a plain
            # RotatingFileHandler creates the file itself (at the ambient
            # umask's default mode) and is only chmod'ed afterwards here,
            # leaving a brief window where it's readable beyond its owner.
            # delay=True stops the handler from opening (and therefore
            # re-creating) the file at construction time at all -- it opens
            # our already-0600 file, in append mode, on first emit.
            _create_owner_only_file(trace_path)
            file_handler = _SecureRotatingFileHandler(
                trace_path, maxBytes=2 * 1024 * 1024, backupCount=2, delay=True
            )
            file_handler.setFormatter(logging.Formatter("%(message)s"))
            logging.getLogger(exchange_log.NAME).addHandler(file_handler)
            self._exchange_handlers.append(file_handler)

        # The main/constructing thread's own connection: startup migration
        # and the one-time uncertain-state resolution, plus the handle
        # exposed publicly (tests, and later the CLI) for direct reads.
        # Never touched by either worker thread.
        self.conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
        migrate(self.conn, transport=cfg.radio.transport)
        # Maintenance can run in operator/tests without starting worker
        # threads. The DB worker replaces this with its thread-owned helper
        # on start, matching the other runtime database helpers.
        self._command_cooldown_notices = CommandCooldownNotices(self.conn)

        now = int(time.time())
        unknown_count, received_settled_count = RequestLog(
            self.conn, dedup_hours=cfg.service.request_dedup_hours
        ).resolve_uncertain_on_startup(now, reply_ttl_seconds=cfg.service.reply_ttl_seconds)
        if unknown_count:
            log.warning("resolved %d uncertain in-flight request(s) at startup", unknown_count)
        if received_settled_count:
            log.warning(
                "settled %d 'received' request(s) at startup "
                "(no in-memory work survives a restart)",
                received_settled_count,
            )

        self._metrics = Metrics()
        if unknown_count:
            # All of these rows just became 'unknown' -- a legitimate reply
            # outcome, not merely bookkeeping -- so it is counted like any
            # other outcome in note_routing_packet.
            self._metrics.replies_total.labels("unknown").inc(unknown_count)
        if received_settled_count:
            # Also settled to 'unknown', not 'failed' -- see
            # RequestLog.resolve_uncertain_on_startup.
            self._metrics.replies_total.labels("unknown").inc(received_settled_count)

        self._metrics_httpd: Any | None = None
        self._metrics_thread: threading.Thread | None = None
        if start_metrics:
            self._metrics_httpd, self._metrics_thread = start_metrics_server(
                cfg.metrics, self._metrics.registry
            )

        # Local read-only dashboard (batch 3a): a no-op (returns (None, None))
        # unless the operator has set [web] enabled = true. See web.py's own
        # module docstring for every safety property this relies on (GET
        # only, one short-lived read-only connection per request, status-
        # file-derived health fields) -- this daemon-side hook is
        # intentionally tiny: start here, stop in Daemon.stop().
        #
        # A misconfigured [web] section must never take the whole daemon
        # down with it -- only the dashboard is unavailable. web_status
        # ("disabled"/"running"/"refused"/"failed") is surfaced via
        # status_snapshot(), the status file, and `mgarden status`.
        self._web_httpd: Any | None = None
        self._web_thread: threading.Thread | None = None
        self._web_status = "disabled"
        try:
            self._web_httpd, self._web_thread = web.start_web_server(cfg)
            if self._web_httpd is not None:
                self._web_status = "running"
        except web.WebRefused as exc:
            log.error("local dashboard disabled: %s", exc)
            self._web_status = "refused"
        except OSError as exc:
            log.error("local dashboard failed to start: %s", exc)
            self._web_status = "failed"

        self._limiter = RateLimiter(cfg.service, clock=self._clock.monotonic)
        # Task 3C finding 2: max_pending_knocks bounds the knock's own
        # share of this shared queue, separate from max_pending_replies.
        # Read from [radio] (like meshcore_knock_cooldown_seconds) even
        # though ReplyQueue's main config type stays ServiceConfig -- the
        # cap is passed in explicitly, the same shape as a transport
        # property, rather than folded into ServiceConfig itself.
        self._queue = ReplyQueue(
            cfg.service, max_pending_knocks=cfg.radio.meshcore_max_pending_knocks
        )

        # Mixed-type queue (task 3B-2 items B/F): an IncomingPacket for the
        # Meshtastic pubsub path, or an Inbound/Delivery/_MeshCoreContact for
        # MeshCore's pump-thread callbacks (which have no ingest thread of
        # their own to lean on) -- see _meshcore_message_sink/
        # _meshcore_delivery_sink/_meshcore_contact_sink and
        # _db_worker_loop's dispatch. Every item still funnels through this
        # one queue/one DB-worker-thread discipline, whichever transport is
        # running.
        self._ingest: queue.Queue[
            IncomingPacket
            | MeshCoreObservation
            | MeshCoreRoute
            | Inbound
            | Delivery
            | _MeshCoreContact
            | _KnockCooldownRelease
            | WeatherBatch
            | _WeatherDropped
        ] = queue.Queue(maxsize=_INGEST_QUEUE_MAXSIZE)

        # `_owns_radio_lifecycle` governs START only: it is False only when
        # a pre-built `transport` was injected (test support -- see
        # `transport` param), and `start()` below consults it to skip
        # calling RadioAdapter.start()/wait_ready() a second time on a
        # radio someone else already started (which would double-subscribe
        # it to pubsub and race two reconnect threads against the same
        # state). It does NOT extend to stop(): `stop()` always calls
        # `self._radio.stop()` unconditionally, regardless of this flag,
        # and that is deliberate, not an oversight -- `RadioAdapter.stop()`
        # is idempotent, and every current caller (a fixture whose
        # FakeTransport-owned radio is running, or a Daemon that owns its
        # radio outright) wants that radio torn down when the Daemon stops.
        self._owns_radio_lifecycle = transport is None
        # Meshtastic-only handle (task 3B-2 item C): `RadioAdapter` has no
        # MeshCore counterpart, so this stays None for a MeshCore-backed
        # daemon. Every direct `self._radio.*` call elsewhere is guarded on
        # this being not-None.
        self._radio: RadioAdapter | None = None
        # MeshCore-only handle: the underlying `MeshCoreClient` (real or
        # fake), reached via `MeshCoreTransport.client` -- needed for
        # daemon-level supervision (`_check_meshcore_wedge`) and shutdown,
        # neither of which is part of the `Transport` Protocol.
        self._meshcore_client: Any | None = None
        # MeshCore-only bookkeeping the generic `Transport.status()` does
        # not carry (task 3B-2 item C's `_RadioLikeStatus` normalization;
        # see `_current_status`).
        self._meshcore_last_packet_at: int | None = None
        self._meshcore_disconnected_since_monotonic: float | None = None
        # Fact #1 from task-3B-1-report.md (fix rounds 3/4): the pinned
        # library's disconnect() can hang forever inside a run's own outer
        # reconnect loop, which then never retries again -- honestly
        # reporting status().connected == False permanently, with no
        # self-heal possible. Tracks how long the transport has been
        # continuously disconnected so _check_meshcore_wedge can tell "a
        # normal backoff wait" from "wedged forever" and force a fresh
        # reconnect. See that method.
        self._meshcore_wedge_since_monotonic: float | None = None
        # Task 3B-4 fix 2 (hardware finding: the garden was unreachable
        # until manually flood-advertised): tracks when the maintenance
        # tick last repeated the self-advertisement `RealMeshCoreClient`
        # already sends once per connection -- see `_check_meshcore_
        # advert_interval`. `None` until the first tick after this daemon
        # (or a reconnect) starts observing, exactly like `_meshcore_wedge_
        # since_monotonic` above, so the periodic repeat never fires in
        # the same tick as the connection's own advert.
        self._meshcore_last_advert_monotonic: float | None = None
        # Last contact-table listing/prune, for status_snapshot (written on the DB worker).
        self._contacts_status: dict[str, int | None] = {
            "count": None, "max": None, "last_prune_at": None, "last_pruned": None,
        }
        # scope-decisions.md sections 2/6/7: MeshCore-only, and only when a
        # channel is configured -- Meshtastic passes channel_policy=None to
        # decide_inbound unconditionally (see _process_obs), so this is
        # never even read for it.
        self._meshcore_channel_policy: ChannelTriggerPolicy | None = (
            ChannelTriggerPolicy(cooldown_seconds=cfg.radio.meshcore_knock_cooldown_seconds)
            if cfg.radio.meshcore_channel
            else None
        )
        # Weather alerts (spec 2026-10-10-weather-alerts-spec.md). The ledger
        # is built on the DB worker thread in _db_worker_run (it needs that
        # thread's connection); the poller thread starts in start().
        self._weather_enabled = cfg.weather.enabled and cfg.radio.transport == "meshcore"
        self._weather_ledger: WeatherLedger | None = None
        self._weather_poller: WeatherPoller | None = None
        self._weather_thread: threading.Thread | None = None
        self._start_weather_poller = weather_poller
        self._weather_unresolved_logged = False
        # Mixed into each weather packet id so two retries of one identical
        # line in the same second never collide on processed_requests.
        self._weather_seq = 0
        if transport is None:
            if cfg.radio.transport == "meshcore":
                # Constructed (and connected) in start(), not here:
                # build_meshcore_transport() connects as one atomic call
                # (meshcore_client.py's documented factory -- 3B-2 forwards
                # config to it, it does not decompose it), and this daemon
                # must not open the radio before start(), exactly like
                # RadioAdapter below, which is likewise inert until then.
                self._transport = None
            else:
                self._radio = RadioAdapter(
                    cfg.radio,
                    sink=self._ingest_sink,
                    interface_factory=interface_factory,
                    clock=self._clock.monotonic,
                    on_reconnect=self._metrics.reconnects_total.inc,
                    on_malformed=self._metrics.malformed_packets_total.inc,
                    on_heartbeat=lambda: self._heartbeats.beat("radio"),
                )
                # Wraps the RadioAdapter just constructed above; never
                # replaces it -- the radio's own lifecycle (start/stop/
                # reconnect, the ingest sink feeding the collector) is
                # unchanged by this transport's existence. `main.py` still
                # reaches `self._radio` directly for that lifecycle and for
                # the observation path (2A's boundary keeps that
                # Meshtastic-shaped in this epic); everything on the
                # trigger/reply/send path goes through `self._transport`
                # instead.
                self._transport = MeshtasticTransport(
                    self._radio,
                    service_cfg=cfg.service,
                    expected_node_id=cfg.radio.expected_node_id,
                )
        else:
            # TEST SUPPORT: a pre-built transport (e.g. tests.fakes.
            # FakeTransport or tests.fake_meshcore.FakeMeshCoreTransport),
            # constructed -- and, for either fake, already started+connected
            # -- before this Daemon exists. A Meshtastic FakeTransport's
            # RadioAdapter sink/metrics/heartbeat hooks are bound below, via
            # rebind_hooks, once self._heartbeats exists; a MeshCore(-shaped)
            # transport has no such rebind step (see start()'s on_message/
            # on_contact/on_delivery wiring instead).
            self._transport = transport
            self._radio = getattr(transport, "radio", None)
            self._meshcore_client = getattr(transport, "client", None)

        # Built after self._radio so its own (fixed) backoff_max is
        # available for the radio worker's stall threshold, without
        # duplicating that constant here. Referenced by the radio
        # on_heartbeat lambda (above, or in rebind_hooks below) via
        # `self._heartbeats`, looked up at call time -- never before this
        # line runs, in either path. MeshCore has no RadioAdapter to read a
        # backoff_max from; its own configured reconnect ceiling stands in.
        self._worker_stall_state: dict[str, bool] = {}
        # Latches the stale-DM warning to once per episode (see
        # _warn_if_stale); a skewed radio clock rejects EVERY DM.
        self._stale_warned = False
        # Latches _report_radio_health's ERROR to once per outage.
        self._radio_disconnected_alerted = False
        self._heartbeats = Heartbeats(
            {
                "db": _WORKER_DB_TX_STALL_SECONDS,
                "tx": _WORKER_DB_TX_STALL_SECONDS,
                "radio": max(
                    self._radio.backoff_max
                    if self._radio is not None
                    else cfg.radio.meshcore_backoff_max_seconds,
                    _WORKER_RADIO_MIN_STALL_SECONDS,
                ),
                "maintenance": _WORKER_MAINTENANCE_STALL_MULTIPLIER
                * _MAINTENANCE_INTERVAL_SECONDS,
                **(
                    {"weather": 3.0 * cfg.weather.poll_interval_seconds + 900.0}
                    if self._weather_enabled
                    else {}
                ),
            },
            clock=self._clock.monotonic,
        )

        if transport is not None and self._radio is not None:
            # Only now, with self._ingest_sink/self._metrics/self._heartbeats
            # all in place, is it safe to rebind the injected transport's
            # RadioAdapter onto this daemon -- see RadioAdapter.rebind_hooks
            # and the `_owns_radio_lifecycle` note above. Skipped entirely
            # for an injected MeshCore(-shaped) transport (self._radio is
            # None): it has no RadioAdapter-style hooks to rebind.
            self._radio.rebind_hooks(
                sink=self._ingest_sink,
                on_reconnect=self._metrics.reconnects_total.inc,
                on_malformed=self._metrics.malformed_packets_total.inc,
                on_heartbeat=lambda: self._heartbeats.beat("radio"),
            )

        # Seam for a "local midnight" (day-change) hook -- see
        # _on_local_midnight/_run_maintenance. Each callable takes the
        # finalized previous day's YYYY-MM-DD string. Starts empty: the
        # daily operator digest writer (_digest_midnight_task) used to be
        # registered here, but this seam fires on the UTC day changing,
        # not the configured timezone's day -- wrong for a digest whose
        # own queries are all in local-day terms (see _run_maintenance's
        # separate `_last_digest_local_day` tracking, which now drives it
        # directly instead).
        self._midnight_tasks: list[Callable[[str], None]] = []

        # Populated by the worker threads themselves at startup (each on its
        # own connection, created inside its own run function -- see
        # _db_worker_run / _tx_worker_run) and by start() once both are
        # ready.
        self._db_conn: Any | None = None
        self._tx_conn: Any | None = None
        self._db_conn_owner_ident: int | None = None
        self._tx_conn_owner_ident: int | None = None
        self._collector: Collector | None = None
        self._guestbook: Guestbook | None = None
        self._mail_prefs: MailPrefs | None = None
        self._mail_aliases: MailAliases | None = None
        self._mail: Mail | None = None
        self._lab: Lab | None = None
        self._quota: DailyQuota | None = None
        self._db_request_log: RequestLog | None = None
        self._tx_request_log: RequestLog | None = None
        self._retention: Retention | None = None
        self._sender: Sender | None = None
        # DB-worker-thread-owned: the UTC day (YYYY-MM-DD) last rolled up
        # as "today" by _run_maintenance, so it can tell when the UTC day
        # has changed (or this is the first tick after startup) and
        # finalize yesterday's row one more time. See _run_maintenance.
        self._last_rollup_day: str | None = None
        # DB-worker-thread-owned: the configured timezone's calendar day
        # (YYYY-MM-DD) last used to fire the digest midnight task, tracked
        # separately from `_last_rollup_day` (UTC) -- see _run_maintenance.
        self._last_digest_local_day: str | None = None
        # DB-worker-thread-owned: (sender_node_id, packet_id) of a bare
        # MAIL request -> (queued_at, [mail entry ids it listed]), awaiting
        # that reply's own delivery outcome -- see _handle_mail_list and
        # _resolve_pending_mail_deliveries.
        self._mail_delivery_pending: dict[
            tuple[str, int], tuple[int, list[tuple[int, str, int]]]
        ] = {}

        self._db_thread: threading.Thread | None = None
        self._tx_thread: threading.Thread | None = None
        self._advert_control: advert_control.AdvertControl | None = None

    # -- public surface --------------------------------------------------

    @property
    def my_node_num(self) -> int | None:
        # MeshCore has no Meshtastic-style numeric node number at all (its
        # id is derived from a public key, never a firmware-assigned num) --
        # None here for a MeshCore-backed daemon, exactly as if the radio
        # had never completed identity verification.
        return self._radio.my_node_num if self._radio is not None else None

    def start(self) -> None:
        if self._owns_radio_lifecycle:
            if self.cfg.radio.transport == "meshcore" and self._transport is None:
                # The one call site (task 3B-2 item A): build_meshcore_
                # transport() constructs the client, connects, resolves the
                # configured channel name to an index, sets the radio's
                # clock, and performs the opt-in autoadd write -- see
                # meshcore_client.py's own docstring for why this is one
                # atomic call rather than decomposed here.
                self._transport = build_meshcore_transport(
                    device=self.cfg.radio.device,
                    # The identity interlock, same input `RadioAdapter`
                    # gets for Meshtastic: the transport reports
                    # `reply_ready=False` when the radio's actual id is not
                    # this one, so a misidentified radio is rejected at
                    # ELIGIBILITY rather than only at send time.
                    expected_node_id=self.cfg.radio.expected_node_id,
                    channel_name=self.cfg.radio.meshcore_channel,
                    alert_channel_name=self.cfg.weather.channel if self._weather_enabled else "",
                    autoadd_overwrite=self.cfg.radio.meshcore_autoadd_overwrite,
                    max_text_bytes=self.cfg.radio.meshcore_max_text_bytes,
                    backoff_base=self.cfg.radio.meshcore_backoff_base_seconds,
                    backoff_max=self.cfg.radio.meshcore_backoff_max_seconds,
                    max_reconnect_attempts=self.cfg.radio.meshcore_max_reconnect_attempts,
                    min_stable_session_seconds=self.cfg.radio.meshcore_min_stable_session_seconds,
                    repeater_public_key=self.cfg.radio.meshcore_repeater_public_key,
                    repeater_guest_password=self.cfg.radio.meshcore_repeater_guest_password,
                    repeater_cache_seconds=self.cfg.radio.meshcore_repeater_cache_seconds,
                    repeater_query_timeout=self.cfg.radio.meshcore_repeater_query_timeout_seconds,
                    on_message=self._meshcore_message_sink,
                    on_contact=self._meshcore_contact_sink,
                    on_route=self._meshcore_route_sink,
                    on_observation=self._meshcore_observation_sink,
                    identity_guard=lambda node, key: bind_meshcore_identity(
                        self.cfg.database.path, node, key
                    ),
                )
                self._meshcore_client = self._transport.client
            elif self._radio is not None:
                self._radio.start()
                self._radio.wait_ready(timeout=_THREAD_READY_TIMEOUT_SECONDS)
        elif isinstance(self._transport, MeshCoreTransport):
            # TEST SUPPORT: an injected MeshCore(-shaped) transport (e.g.
            # FakeMeshCoreTransport) is already connected by the fixture --
            # still needs on_message/on_contact registered here, exactly
            # like the owned path above, since the Daemon (not the fixture)
            # owns command dispatch.
            self._transport.on_message(self._meshcore_message_sink)
            self._transport.on_contact(self._meshcore_contact_sink)
            self._transport.on_route(self._meshcore_route_sink)
            self._transport.on_observation(self._meshcore_observation_sink)

        db_ready = threading.Event()
        tx_ready = threading.Event()
        sender_ready = threading.Event()

        self._db_thread = threading.Thread(
            target=self._db_worker_run, args=(db_ready, sender_ready), name="db-worker", daemon=True
        )
        self._tx_thread = threading.Thread(
            target=self._tx_worker_run, args=(tx_ready, sender_ready), name="tx-worker", daemon=True
        )
        self._db_thread.start()
        self._tx_thread.start()

        if not db_ready.wait(timeout=_THREAD_READY_TIMEOUT_SECONDS) or not tx_ready.wait(
            timeout=_THREAD_READY_TIMEOUT_SECONDS
        ):
            raise RuntimeError("daemon worker threads failed to initialize in time")

        if self._weather_enabled and self._start_weather_poller:
            self._weather_poller = WeatherPoller(
                self.cfg.weather,
                emit=self._ingest.put_nowait,
                heartbeat=lambda: self._heartbeats.beat("weather"),
                stop_event=self._stop_event,
            )
            self._weather_thread = self._weather_poller.start()

        # Sender is constructed here (not in __init__) because it needs both
        # worker connections' RequestLogs, which only exist once each worker
        # thread has created its own connection.
        self._sender = Sender(
            transport=self._transport,
            cfg=self.cfg.service,
            limiter=self._limiter,
            queue=self._queue,
            request_log=self._db_request_log,
            tx_request_log=self._tx_request_log,
            metrics=self._metrics,
            expected_node_id=self.cfg.radio.expected_node_id,
            is_blocked=lambda node_id: is_blocked(self._tx_conn, self._bbs_id(node_id)),
            clock=self._clock.monotonic,
        )
        # The transport reports a delivery outcome (ack/fail) via this
        # registered callback. Meshtastic: MeshtasticTransport.
        # note_routing_packet calls it synchronously from _process_obs's
        # ROUTING_APP branch, which already runs on the DB worker thread, so
        # calling straight into Sender.on_delivery (itself DB-worker-owned)
        # is safe. MeshCore: on_delivery fires on the transport's own pump
        # thread instead (task 3B-2 item B) -- calling Sender.on_delivery
        # directly from there would touch the DB-worker-owned request_log
        # from a foreign thread, so it goes through the same bounded,
        # nonblocking ingest queue every other MeshCore event does.
        if isinstance(self._transport, MeshCoreTransport):
            self._transport.on_delivery(self._meshcore_delivery_sink)
        else:
            self._transport.on_delivery(self._sender.on_delivery)
        sender_ready.set()
        if self.cfg.radio.transport == "meshcore":
            try:
                self._advert_control = advert_control.AdvertControl(
                    advert_control.socket_path(self.cfg.database.path), self._operator_advert
                )
            except OSError as exc:
                log.warning("operator advert control unavailable: %s", exc)
        log.info("daemon started")

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        try:
            self._ingest.put(_STOP_SENTINEL, timeout=1.0)
        except queue.Full:
            # The DB worker may already have exited (e.g. an unexpected
            # exception escaping its loop) with the queue full, in which
            # case a plain blocking put() would never return. _stop_event
            # (checked by the loop on every ingest.get() timeout) is the
            # fallback path out.
            log.warning("ingest queue full at shutdown; relying on stop event")
        if self._weather_thread is not None:
            self._weather_thread.join(timeout=5)
        if self._db_thread is not None:
            self._db_thread.join(timeout=10)
        if self._tx_thread is not None:
            self._tx_thread.join(timeout=10)
        if self._advert_control is not None:
            self._advert_control.close()
            self._advert_control = None
        # Unconditional, unlike start()'s _owns_radio_lifecycle guard: stop()
        # is idempotent and this radio -- ours or an injected transport's --
        # must be torn down here either way.
        if self._radio is not None:
            self._radio.stop()
        if self._meshcore_client is not None:
            # RealMeshCoreClient.stop() (meshcore_client.py, fix rounds
            # 1-4): detaches the current run, sets its stop_event, joins
            # both threads (timed), and returns -- never called again until
            # this has returned, per that module's central rule. A fake
            # client injected for tests may not implement stop() (e.g.
            # tests.fake_meshcore.FakeMeshCore models connect()/disconnect(),
            # not this lifecycle) -- tolerated silently, since there is
            # nothing more this daemon can do about a test double's shape.
            stop = getattr(self._meshcore_client, "stop", None)
            if stop is not None:
                stop()
        if self._metrics_httpd is not None:
            self._metrics_httpd.shutdown()
            if self._metrics_thread is not None:
                self._metrics_thread.join(timeout=5)
            self._metrics_httpd.server_close()
        web.stop_web_server(self._web_httpd, self._web_thread)
        self.conn.close()
        for handler in self._exchange_handlers:
            exchange_log.deconfigure(handler)
        self._exchange_handlers = []
        log.info("daemon stopped")

    def run_forever(self) -> None:
        """Installs SIGTERM/SIGINT handlers, blocks until signaled, shuts down.

        Also this process's systemd watchdog main loop, entirely optional
        and dependency-free (see `_sd_notify`/`_watchdog_interval_seconds`):
        under a plain unit (no `NOTIFY_SOCKET`) this is unchanged, a single
        blocking `wait()`. Under a `Type=notify` unit, sends `READY=1` once
        here (systemd otherwise treats the service as still starting), then
        wakes every `WATCHDOG_USEC/2` to send `WATCHDOG=1` -- but ONLY while
        no worker is stalled (`Heartbeats.any_stalled`): once one is, this
        loop simply stops pinging, so systemd's own `WatchdogSec` timeout
        restarts the unit, exactly as if the process had hung outright.
        """

        def _handle_signal(signum: int, frame: Any) -> None:
            log.info("received signal %d; shutting down", signum)
            self._stop_event.set()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        _sd_notify("READY=1")
        watchdog_interval = _watchdog_interval_seconds()
        if watchdog_interval is not None:
            self._watchdog_loop(watchdog_interval)
        else:
            self._stop_event.wait()
        self.stop()

    def _watchdog_loop(self, interval: float) -> None:
        """The actual `WATCHDOG=1` ping loop, factored out of `run_forever`
        so it can be driven directly by a test (a short `interval`, a
        controllable `_stop_event`/`_heartbeats`) without installing real
        signal handlers -- `signal.signal` only works on the main thread,
        which a test exercising this loop from a background thread cannot
        rely on. Sends one ping per wake, but only while no worker is
        stalled; stops entirely (silently, no ping at all) once one is,
        until it recovers."""
        while not self._stop_event.wait(timeout=interval):
            if not self._heartbeats.any_stalled():
                _sd_notify("WATCHDOG=1")

    def drain(self, timeout: float = 5.0) -> None:
        """TEST SUPPORT ONLY -- never called in production.

        Blocks until every packet already in the ingest queue has been
        processed by the DB worker, then waits for the real TX worker
        thread to fully resolve every reply (sent, failed, or dropped -- not
        merely popped off the queue; see ReplyQueue.in_flight) or until
        `timeout` elapses, resetting the global transmit cooldown on every
        poll so a test is never held back by global_tx_cooldown_seconds.
        This never calls Sender.pump() itself: pump() must only ever run on
        the TX worker thread, since it uses that thread's own private
        connection.
        """
        self._ingest.join()
        self._queue.waive_holds()  # channel-reply delay: see ReplyQueue.waive_holds
        deadline = time.monotonic() + timeout
        while self._queue.in_flight() > 0 and time.monotonic() < deadline:
            self._limiter._global_tx_last = None
            time.sleep(0.01)

    def advance_seconds(self, seconds: float) -> None:
        """TEST SUPPORT ONLY -- never called in production.

        Advances the daemon's injected clock, moving RateLimiter cooldown
        windows, ReplyJob TTL deadlines, and DB timestamps (via
        `_ingest_sink`, which re-stamps every observation's wall-clock
        fields from this same clock) forward together without sleeping.
        """
        self._clock.advance(seconds)

    def _current_status(self) -> _RadioLikeStatus:
        """Normalizes either `RadioAdapter.status()` (Meshtastic, unchanged)
        or the generic `Transport.status()` (MeshCore, which has no
        `RadioStatus`-shaped object at all) into the one small shape
        `status_snapshot`/`write_status_file`/`_report_radio_health`/
        `_radio_disconnected_seconds` need -- task 3B-2 item C. Two fields
        `TransportStatus` does not carry are tracked here instead:
        `last_packet_at` (stamped by `_meshcore_message_sink`/
        `_meshcore_delivery_sink`/`_meshcore_contact_sink`) and
        `disconnected_since_monotonic` (this method's own transition
        tracking, mirroring what `RadioAdapter` does internally for
        Meshtastic).
        """
        if self._radio is not None:
            s = self._radio.status()
            return _RadioLikeStatus(
                connected=s.connected,
                reply_ready=s.reply_ready,
                actual_node_id=s.actual_node_id,
                expected_node_id=s.expected_node_id,
                mismatch_reason=s.mismatch_reason,
                last_packet_at=s.last_packet_at,
                disconnected_since_monotonic=s.disconnected_since_monotonic,
            )
        ts = self._transport.status()
        if ts.connected:
            self._meshcore_disconnected_since_monotonic = None
        elif self._meshcore_disconnected_since_monotonic is None:
            self._meshcore_disconnected_since_monotonic = self._clock.monotonic()
        mismatch_reason = None
        if ts.actual_node_id is not None and ts.actual_node_id != self.cfg.radio.expected_node_id:
            mismatch_reason = "node_id_mismatch"
        return _RadioLikeStatus(
            connected=ts.connected,
            reply_ready=ts.reply_ready,
            actual_node_id=ts.actual_node_id,
            expected_node_id=self.cfg.radio.expected_node_id,
            mismatch_reason=mismatch_reason,
            last_packet_at=self._meshcore_last_packet_at,
            disconnected_since_monotonic=self._meshcore_disconnected_since_monotonic,
        )

    def status_snapshot(self) -> dict:
        """Consumed by the CLI (Task 11)."""
        status = self._current_status()
        report = self._retention.storage_report()
        now = int(time.time())
        return {
            "version": __version__,
            "started_at": self.started_at,
            "uptime_seconds": max(0, now - self.started_at),
            "radio_connected": status.connected,
            "reply_ready": status.reply_ready,
            "actual_node_id": status.actual_node_id,
            "expected_node_id": status.expected_node_id,
            "mismatch_reason": status.mismatch_reason,
            "last_packet_at": status.last_packet_at,
            "queue_depth": self._queue.depth(),
            "storage": {
                "total_bytes": report.total_bytes,
                "cap_bytes": report.cap_bytes,
                "pressure": report.pressure,
            },
            "last_backup_at": self._retention.last_backup_at,
            "last_backup_error": self._retention.last_backup_error,
            "rate_limited_drops_1h": self._rate_limited_drops_1h(now),
            "radio_disconnected_seconds": self._radio_disconnected_seconds(status),
            "workers": self._heartbeats.snapshot(),
            "weather": self._weather_status(),
            "contacts": dict(self._contacts_status),
            "web_status": self._web_status,
        }

    def _note_drop(self, now: int) -> None:
        """Records one rate-limit-style drop (cooldown, quota, sign_quota,
        or queue_full) for the rolling 1h summary in status_snapshot().
        Bounded: entries older than 1h are pruned on every call, so this
        never grows past roughly one hour's worth of drops."""
        self._drop_events.append(now)
        self._prune_drop_events(now)

    def _prune_drop_events(self, now: int) -> None:
        cutoff = now - 3600
        while self._drop_events and self._drop_events[0] < cutoff:
            self._drop_events.popleft()

    def _rate_limited_drops_1h(self, now: int) -> int:
        self._prune_drop_events(now)
        return len(self._drop_events)

    def write_status_file(self) -> None:
        """Atomically rewrites `<database dir>/status.json` (temp file in the
        same directory, then `os.replace`) with `status_snapshot()`'s
        contents: connectivity/identity/counters only -- no node history, no
        message text, no secrets. Owner-only permissions (0o600). Called
        periodically by the DB worker loop, and safe to call directly (e.g.
        from tests) since it does not touch any sqlite3.Connection.
        """
        payload = self.status_snapshot()
        self._report_worker_health(payload["workers"])
        self._report_radio_health(self._current_status())
        # Sub-second-resolution write marker: lets a caller (or a test) prove
        # the periodic loop produced a NEW file, not just that a file exists
        # -- second-resolution wall-clock fields and directory mtimes are too
        # coarse on some filesystems to tell two rapid rewrites apart.
        payload["generated_at"] = time.time()
        db_dir = _database_dir(self.cfg)
        os.makedirs(db_dir, exist_ok=True)
        path = _status_file_path(self.cfg)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)

    def _report_worker_health(self, workers: dict[str, dict[str, Any]]) -> None:
        """Updates the `meshgarden_worker_heartbeat_age_seconds` gauge for
        every worker, and logs a bounded WARNING (bounded: only the four
        fixed worker names ever appear) the moment a worker's `stalled`
        flag flips False -> True, and a matching INFO once it recovers.
        Never logs on every tick a worker stays stalled or stays healthy --
        only on the transition -- so a long-stalled worker never floods the
        log.
        """
        for name, info in workers.items():
            self._metrics.worker_heartbeat_age_seconds.labels(name).set(info["age_seconds"])
            was_stalled = self._worker_stall_state.get(name, False)
            stalled = info["stalled"]
            if stalled and not was_stalled:
                log.warning(
                    "worker %s stalled: heartbeat age %.1fs", name, info["age_seconds"]
                )
            elif was_stalled and not stalled:
                log.info("worker %s recovered", name)
            self._worker_stall_state[name] = stalled

    def _radio_disconnected_seconds(self, status: _RadioLikeStatus) -> float | None:
        """How long the radio has been disconnected, or None while it is
        connected (or while the adapter carries no stamp)."""
        if status.connected or status.disconnected_since_monotonic is None:
            return None
        return round(max(0.0, self._clock.monotonic() - status.disconnected_since_monotonic), 3)

    def _warn_if_stale(
        self, obs: IncomingPacket | Inbound, reason: Reject, reply_ttl_seconds: int
    ) -> None:
        """Logs one bounded WARNING per episode of stale-rejected DMs.

        `obs` is duck-typed on purpose: `IncomingPacket` (Meshtastic) and
        `Inbound` (MeshCore) both carry `rx_time`/`received_at` under those
        exact names, and this method reads nothing else from `IncomingPacket`
        -- see `_process_meshcore_inbound`, which shares this with
        `_process_obs`. `Inbound.sender_ts` (MeshCore only) is read via
        `getattr` for the same reason `IncomingPacket` has no such field.

        A radio whose clock has drifted past `reply_ttl_seconds` fails the
        freshness gate on EVERY DM. Each one is still recorded as an
        observation, so from the outside the node looks healthy and the
        sender's radio even acks -- but nothing is ever answered, no
        `processed_requests` row appears, and the only trace was a DEBUG
        line invisible at the default level. That combination cost an hour
        of diagnosis; naming the skew makes it immediate.

        Latched to once per episode (re-armed by the first DM that does get
        through) because a wrong clock rejects every DM, and one line per
        packet would flood the log.
        """
        if reason is not Reject.STALE or self._stale_warned:
            return
        self._stale_warned = True
        sender_ts = getattr(obs, "sender_ts", None)
        if obs.rx_time is not None:
            log.warning(
                "stale DM rejected: the radio's clock is %ds behind ours, over the "
                "%ds reply_ttl_seconds freshness window -- every DM will be dropped "
                "until the radio's clock is set",
                obs.received_at - obs.rx_time,
                reply_ttl_seconds,
            )
        elif sender_ts is not None:
            log.warning(
                "stale DM rejected: the packet's sender_timestamp is %ds older than "
                "our clock, over the %ds reply_ttl_seconds freshness window",
                obs.received_at - sender_ts,
                reply_ttl_seconds,
            )
        else:
            log.warning(
                "stale DM rejected: the packet carried no radio receive time and the "
                "radio has not been connected for %ds yet",
                reply_ttl_seconds,
            )

    def _report_radio_health(self, status: _RadioLikeStatus) -> None:
        """Escalates a sustained radio outage to ERROR exactly once per
        outage, and logs one INFO when it clears.

        This is the one fault the worker heartbeats structurally cannot see.
        A dead radio leaves the reconnect thread perfectly healthy -- it
        loops, beats, and backs off correctly with nothing to talk to -- so
        `workers` reports all-clear and the systemd watchdog has nothing to
        trip on. Restarting the process would not help either, since the
        radio itself is what needs power-cycling. So the only useful thing
        the daemon can do is say so loudly: without this it serves nobody
        while still reporting `daemon_status: ok`, which is exactly how an
        85-minute outage went unnoticed until a visitor's DM went
        unanswered.
        """
        if status.connected:
            if self._radio_disconnected_alerted:
                log.info("radio reconnected")
                self._radio_disconnected_alerted = False
            return
        age = self._radio_disconnected_seconds(status)
        if age is None or age <= _RADIO_DISCONNECTED_ALERT_SECONDS:
            return
        if not self._radio_disconnected_alerted:
            log.error(
                "radio disconnected for %.0fs: nothing can be received and no reply "
                "can be sent; the radio itself may need a power cycle",
                age,
            )
            self._radio_disconnected_alerted = True

    # -- pubsub sink: nonblocking, never touches the database -------------

    def _ingest_sink(self, obs: IncomingPacket) -> None:
        """Called on the meshtastic library thread: must only enqueue.

        Re-stamps `received_at` from the daemon's own injected clock, so DB
        timestamps stay consistent with the monotonic clock RateLimiter/TTL
        deadlines use -- this is what lets a test advance both together via
        advance_seconds() without sleeping. A no-op in production, where
        the injected clock is real time and this simply re-reads it a few
        microseconds later than RadioAdapter did.

        `rx_time` (the radio's own receive timestamp) is shifted by the
        same offset rather than overwritten: `delta` is 0 in production
        (the injected clock IS real time), and equals however many seconds
        a test has advanced the clock by, so a fresh packet still reads as
        fresh after advance_seconds(). Overwriting rx_time outright (the
        previous behavior) made every packet look freshly received no
        matter how old its actual rx_time was, which let
        `check_eligibility`'s freshness gate (`received_at - rx_time >
        reply_ttl_seconds`) never fire -- a replayed/stale DM would still
        get answered.

        `received_monotonic` is likewise re-stamped from
        `self._clock.monotonic()`, overwritten outright (there is no prior
        reading to preserve an offset from, unlike `rx_time`): it feeds
        `ReplyJob.deadline_monotonic` directly, so it must land on the same
        clock `RateLimiter`/`ReplyQueue`/`Sender` compare it against.
        Historically this fell out for free because `RadioAdapter` was
        always constructed with `clock=self._clock.monotonic`; a transport
        injected via `Daemon(transport=...)` (task 2B fix round 1's
        `tests.fakes.FakeTransport`) may wrap a `RadioAdapter` on a
        different clock (real `time.monotonic`, unaffected by
        `advance_seconds()`), which silently expired every reply job in a
        test that advanced the clock between sends until this line existed
        to normalize it here instead of relying on the source.
        """
        now = int(self._clock.time())
        delta = now - int(time.time())
        obs = replace(
            obs,
            received_at=now,
            received_monotonic=self._clock.monotonic(),
            rx_time=(obs.rx_time + delta if obs.rx_time is not None else None),
        )
        try:
            self._ingest.put_nowait(obs)
        except queue.Full:
            self._metrics.ingest_dropped_total.inc()
            log.warning("ingest queue full; packet dropped")

    # -- MeshCore pump-thread sinks: nonblocking, never touch the database --
    #
    # MeshCore has no separate pubsub ingest thread to lean on the way
    # RadioAdapter's does (task 3B-2 item B): `MeshCoreTransport.
    # on_message`/`on_delivery`/`on_contact` all fire on the transport's own
    # pump thread. Each of these three sinks does exactly what
    # `_ingest_sink` does above -- re-stamp a wall-clock reading from the
    # daemon's own injected clock, then a bounded, nonblocking enqueue onto
    # the SAME `_ingest` queue the DB worker thread already owns -- so
    # `decide_inbound`/the command dispatcher/the collector are only ever
    # reached from that one thread, never from the pump thread directly.

    def _meshcore_message_sink(self, inbound: Inbound) -> None:
        """Registered via `MeshCoreTransport.on_message`. Drives
        `decide_inbound` indirectly: this only enqueues `inbound` for
        `_process_meshcore_inbound` (DB worker thread) to decide and
        dispatch."""
        now = int(self._clock.time())
        self._meshcore_last_packet_at = now
        inbound = replace(inbound, received_at=now)
        try:
            self._ingest.put_nowait(inbound)
        except queue.Full:
            self._metrics.ingest_dropped_total.inc()
            log.warning("ingest queue full; meshcore inbound dropped")

    def _meshcore_delivery_sink(self, delivery: Delivery) -> None:
        """Registered via `MeshCoreTransport.on_delivery` ONLY for a
        MeshCore transport (see `start()`) -- Meshtastic's `on_delivery`
        fires from `_process_obs`'s ROUTING_APP branch, already on this
        thread, and is wired straight to `Sender.on_delivery` instead."""
        self._meshcore_last_packet_at = int(self._clock.time())
        try:
            self._ingest.put_nowait(delivery)
        except queue.Full:
            self._metrics.ingest_dropped_total.inc()
            log.warning("ingest queue full; meshcore delivery dropped")

    def _meshcore_route_sink(self, route: MeshCoreRoute) -> None:
        try:
            self._ingest.put_nowait(route)
        except queue.Full:
            self._metrics.ingest_dropped_total.inc()

    def _meshcore_observation_sink(self, observation: MeshCoreObservation) -> None:
        self._meshcore_last_packet_at = observation.received_at
        try:
            self._ingest.put_nowait(observation)
        except queue.Full:
            self._metrics.ingest_dropped_total.inc()

    def _process_meshcore_observation(self, observation: MeshCoreObservation) -> None:
        identity = parse_node_id_argument(self._transport.identity())
        unique = self._collector.record_meshcore(
            observation, my_node_num=int(identity[1:], 16) if identity else None
        )
        kind = bounded_portnum(observation.kind)
        channel = bounded_channel(observation.channel)
        self._metrics.raw_receive_events.labels(kind, channel).inc()
        if unique:
            self._metrics.unique_packets.labels(kind, channel).inc()
        if observation.snr is not None:
            self._metrics.rx_snr.observe(observation.snr)
        if observation.rssi is not None:
            self._metrics.rx_rssi.observe(observation.rssi)

    def _meshcore_contact_sink(
        self, node_id: str, display_name: str | None, lat: float | None, lon: float | None
    ) -> None:
        """Registered via `MeshCoreTransport.on_contact` (task 3B-2 item
        F). Never calls `self._collector` itself -- that connection is
        owned by the DB worker thread, and this fires on the pump thread."""
        now = int(self._clock.time())
        contact = _MeshCoreContact(
            node_id=node_id, display_name=display_name, lat=lat, lon=lon, observed_at=now
        )
        try:
            self._ingest.put_nowait(contact)
        except queue.Full:
            self._metrics.ingest_dropped_total.inc()
            log.warning("ingest queue full; meshcore contact dropped")

    def _enqueue_knock_release(self, node_id: str, claimed_at: int) -> None:
        """Fix round 2 (task 3B-3): the channel knock's `ReplyJob.
        release_on_pretransmission_failure` callback. UNLIKE the sinks
        above, this is called from the TX WORKER thread (via `Sender.
        _finish_expired`/`_mark_dropped`) as well as, synchronously, from
        the DB worker thread (via `_reserve_and_send`'s own immediate
        queue refusal, inside `_handle_knock`) -- it must be safe from
        EITHER. It therefore never touches `self._knock_cooldown` (built
        on `self._db_conn`, owned by the DB worker thread) directly; it
        only ever enqueues, exactly like the pump-thread sinks above, so
        the actual release always happens on `_process_knock_cooldown_
        release`, on the one thread that owns that connection. `queue.
        Queue.put_nowait` is safe to call from any thread."""
        try:
            self._ingest.put_nowait(_KnockCooldownRelease(node_id=node_id, claimed_at=claimed_at))
        except queue.Full:
            self._metrics.ingest_dropped_total.inc()
            log.warning("ingest queue full; knock cooldown release dropped")

    # -- DB worker thread ---------------------------------------------------

    def _db_worker_run(self, ready: threading.Event, sender_ready: threading.Event) -> None:
        """Owns its own connection end to end: opened here, closed here.
        Never touched by any other thread."""
        self._db_conn = connect(
            self.cfg.database.path, bbs_path=self.cfg.database.resolved_bbs_path,
            transport=self.cfg.radio.transport,
        )
        self._db_conn_owner_ident = threading.get_ident()
        try:
            self._collector = Collector(
                self._db_conn,
                enabled_position_capture=self.cfg.observatory.enabled_position_capture,
            )
            self._guestbook = Guestbook(self._db_conn, self.cfg.service)
            self._mail_prefs = MailPrefs(self._db_conn)
            self._mail_aliases = MailAliases(self._db_conn)
            self._mail = Mail(self._db_conn)
            self._lab = Lab(self._db_conn)
            self._quota = DailyQuota(
                self._db_conn,
                command_limit=self.cfg.service.daily_command_quota,
                sign_limit=self.cfg.service.daily_sign_quota,
                post_limit=self.cfg.service.daily_post_quota,
                mail_sent_limit=self.cfg.service.daily_mail_quota,
            )
            self._db_request_log = RequestLog(
                self._db_conn, dedup_hours=self.cfg.service.request_dedup_hours
            )
            if self._weather_enabled:
                self._weather_ledger = WeatherLedger(
                    self._db_conn,
                    max_sends_per_hour=self.cfg.weather.max_sends_per_hour,
                    aliases=effective_aliases(self.cfg.weather.zone_aliases),
                    timezone=self.cfg.service.timezone,
                )
            # Task 3B-3: the channel-knock cooldown's persistent state
            # (database.py migration 9). Constructed unconditionally, like
            # every other DB-worker-owned helper above -- it is simply
            # never consulted on a Meshtastic-transport daemon, since
            # `_process_meshcore_inbound` (the only caller of
            # `_handle_knock`) never runs for one.
            self._knock_cooldown = ChannelKnockCooldown(self._db_conn)
            self._command_cooldown_notices = CommandCooldownNotices(self._db_conn)
            self._retention = Retention(
                self._db_conn, self.cfg.retention, self.cfg.backup, log_dir=self._log_dir
            )
            self._write_startup_catchup_digest()
            self._refresh_live_gauges(int(time.time()))
            ready.set()
            sender_ready.wait()
            self._db_worker_loop()
        finally:
            self._db_conn.close()

    def _db_worker_loop(self) -> None:
        last_maintenance = time.monotonic()
        last_status_write = 0.0  # write once immediately, then every 10s
        while True:
            self._heartbeats.beat("db")
            try:
                obs = self._ingest.get(timeout=_DB_LOOP_POLL_SECONDS)
            except queue.Empty:
                obs = None
                # Fallback shutdown path: stop() couldn't enqueue the stop
                # sentinel (queue full) so it set _stop_event instead. Never
                # hang here waiting for a sentinel that was never sent.
                if self._stop_event.is_set():
                    break

            if obs is not None:
                if obs is _STOP_SENTINEL:
                    self._ingest.task_done()
                    break
                try:
                    if isinstance(obs, IncomingPacket):
                        self._process_obs(obs)
                    elif isinstance(obs, Inbound):
                        self._process_meshcore_inbound(obs)
                    elif isinstance(obs, MeshCoreRoute):
                        self._collector.record_meshcore_route(obs)
                    elif isinstance(obs, MeshCoreObservation):
                        self._process_meshcore_observation(obs)
                    elif isinstance(obs, Delivery):
                        self._sender.on_delivery(obs)
                    elif isinstance(obs, _MeshCoreContact):
                        self._process_meshcore_contact(obs)
                    elif isinstance(obs, _KnockCooldownRelease):
                        self._process_knock_cooldown_release(obs)
                    elif isinstance(obs, WeatherBatch):
                        self._process_weather_batch(obs)
                    elif isinstance(obs, _WeatherDropped) and self._weather_ledger is not None:
                        self._weather_ledger.mark(obs.product_key, "dropped", int(time.time()))
                except Exception:
                    log.exception("db worker: unexpected error processing an observation")
                    self._metrics.database_errors_total.inc()
                    if isinstance(obs, IncomingPacket):
                        self._settle_failed_request(node_id_from_num(obs.from_num), obs.packet_id)
                    elif isinstance(obs, Inbound):
                        self._settle_failed_request(obs.sender, obs.raw_id)
                finally:
                    self._ingest.task_done()

            try:
                self._sender.drain_deliveries()
            except Exception:
                log.exception("db worker: failed to settle buffered deliveries")

            if self._advert_control is not None:
                try:
                    self._advert_control.poll()
                except Exception:
                    log.exception("db worker: operator advert control failed")

            now_mono = time.monotonic()
            if now_mono - last_maintenance >= _MAINTENANCE_INTERVAL_SECONDS:
                last_maintenance = now_mono
                try:
                    self._run_maintenance()
                except Exception:
                    log.exception("db worker: unexpected error during maintenance")

            if now_mono - last_status_write >= _STATUS_FILE_INTERVAL_SECONDS:
                last_status_write = now_mono
                try:
                    self._refresh_live_gauges(int(time.time()))
                    self.write_status_file()
                except Exception:
                    log.exception("db worker: unexpected error writing status file")

    def _settle_failed_request(self, sender_node_id: str, packet_id: int | None) -> None:
        """Best-effort cleanup after `_process_obs`/`_process_meshcore_
        inbound` raised: a request may already have been `claim()`-ed
        (inserted as 'received') before the exception hit, and normally
        moves out of 'received' within the same tick -- if it never does, it
        would sit there forever, invisible to pruning (terminal states
        only) and to startup recovery (queued/sending/submitted only).
        Settles it to 'failed'/'internal_error' instead. A no-op if no row
        was ever claimed (the exception happened earlier, e.g. before
        dispatch), `packet_id` is None (the same "never claimed" case,
        since claim() always needs one), or it already reached some other
        state. Never raises: this runs from inside an `except Exception:`
        handler already, and must not replace that error with one of its
        own.

        Takes the already-resolved `(sender_node_id, packet_id)` rather
        than an `IncomingPacket`/`Inbound`, since the two transports derive
        them differently (`node_id_from_num(obs.from_num)` vs.
        `inbound.sender`/`inbound.raw_id` directly) but need identical
        settlement logic once resolved.
        """
        if packet_id is None:
            return
        try:
            if self._db_request_log.get_state(sender_node_id, packet_id) == "received":
                self._db_request_log.set_state(
                    sender_node_id,
                    packet_id,
                    "failed",
                    int(time.time()),
                    reason="internal_error",
                )
        except Exception:
            log.exception("db worker: failed to settle a request after an unexpected error")

    def _process_obs(self, obs: IncomingPacket) -> None:
        sender_node_id = node_id_from_num(obs.from_num)

        is_unique = self._collector.record(obs, my_node_num=self._radio.my_node_num)

        portnum = bounded_portnum(obs.portnum)
        channel = bounded_channel(obs.channel)
        self._metrics.raw_receive_events.labels(portnum, channel).inc()
        if is_unique:
            self._metrics.unique_packets.labels(portnum, channel).inc()
        # Missing readings are never observed as zero.
        if obs.rx_snr is not None:
            self._metrics.rx_snr.observe(obs.rx_snr)
        if obs.rx_rssi is not None:
            self._metrics.rx_rssi.observe(obs.rx_rssi)

        # Display names only, from a packet we observed ourselves; never an
        # identity, and never logged.
        if obs.short_name is not None or obs.long_name is not None:
            self._collector.update_node_names(
                sender_node_id,
                short_name=obs.short_name,
                long_name=obs.long_name,
                observed_at=obs.received_at,
            )

        if obs.portnum == "ROUTING_APP":
            self._transport.note_routing_packet(obs)
            # A routing packet is the common way a MAIL-list reply's
            # delivery outcome (acked/failed) actually resolves -- settle
            # it right away rather than waiting for the next maintenance
            # tick (guarded on non-empty: the dict is normally empty, and
            # this runs on every ROUTING_APP packet).
            if self._mail_delivery_pending:
                self._resolve_pending_mail_deliveries(int(self._clock.time()))

        tstatus = self._transport.status()
        connected_seconds = (
            tstatus.connected_seconds if tstatus.connected_seconds is not None else float("inf")
        )
        decision = decide_inbound(
            as_inbound(obs),
            me=self._transport.identity(),
            reply_ready=tstatus.reply_ready,
            reply_ttl_seconds=self.cfg.service.reply_ttl_seconds,
            connected_seconds=connected_seconds,
            # Meshtastic never triggers on a channel: no policy, ever. See
            # meshtastic_transport.py's module docstring.
            channel_policy=None,
        )
        if decision.outcome is Outcome.KNOCK:
            # Channel-trigger handling (the rate-limited invite DM) is out
            # of scope for this transport -- MeshtasticTransport never
            # supplies a channel_policy, so decide_inbound cannot actually
            # return KNOCK from the call above today (channel_policy=None
            # is a hardcoded literal, not a config read). Handled as its
            # own branch anyway, rather than falling into the REJECTED
            # branch below: KNOCK carries no `reason` (InboundDecision's
            # own invariant -- reason is set if and only if the outcome is
            # REJECTED), so `reason.value` there would raise the moment a
            # future channel_policy makes this branch reachable.
            self._metrics.commands_total.labels("UNKNOWN", "rejected").inc()
            return
        if decision.outcome is not Outcome.COMMAND:
            reason = decision.reason
            outcome = _ELIGIBILITY_OUTCOME.get(reason, "rejected")
            log.debug("packet ineligible reason=%s", reason.value)
            if obs.text is not None or obs.portnum == "TEXT_MESSAGE_APP":
                exchange_log.log_rejection(
                    sender_node_id, reason.value, now=obs.received_at, packet_id=obs.packet_id,
                    text=obs.text,
                )
            self._warn_if_stale(obs, reason, self.cfg.service.reply_ttl_seconds)
            self._metrics.commands_total.labels("UNKNOWN", outcome).inc()
            return
        # A DM got through, so any stale episode is over: re-arm the
        # warning so a later one (the radio reboots again) is reported.
        self._stale_warned = False

        # Text is decoded only transiently: parse now, never persist it.
        # Fed from decision.command_text, never obs.text directly -- the
        # structural half of the knock guarantee (InboundDecision.
        # command_text is non-None iff outcome is COMMAND); a no-op for
        # Meshtastic today since decision.command_text == obs.text
        # whenever we reach here, but it is the boundary-correct call.
        parsed = self._parse_command_for_dispatch(decision.command_text, sender_node_id)
        if not self._trace_and_check_known(parsed, sender_node_id, obs.received_at, obs.text):
            return

        now = obs.received_at
        if not self._claim_and_gate(sender_node_id, obs, now, parsed):
            return

        self._dispatch_command(parsed, sender_node_id, obs, now)

    def _parse_command_for_dispatch(self, command_text: str, sender_node_id: str) -> ParsedCommand:
        """`parse_command` plus the SEEN/LAB_COMMANDS gating rewrites, shared
        VERBATIM between `_process_obs` (Meshtastic) and
        `_process_meshcore_inbound` (MeshCore) so the two receive paths can
        never quietly diverge on which commands a locked/disabled tier
        rewrites to UNKNOWN."""
        parsed = parse_command(
            command_text,
            lab_unlock_word=self.cfg.service.lab_unlock_word,
            # Without a peer observatory the MC/MT suffix stays ordinary text.
            mesh_suffix=bool(self.cfg.database.peer_path),
        )
        if parsed.command is Command.POSITION and not self.cfg.observatory.dm_position_capture:
            # Off by default: a location share is ordinary silent text
            # unless the operator opted in, and must not claim a dedup or
            # quota slot (same reasoning as the SEEN rewrite below).
            parsed = ParsedCommand(Command.UNKNOWN, "", rejection_reason="position_disabled")
        if parsed.command is Command.SEEN and not self.cfg.service.seen_enabled:
            # ruling 15: seen_enabled=False means SEEN "does not exist" --
            # exactly like an unrecognized command, not merely a silent
            # handler-level drop. Rewriting here (before ANY of the
            # eligibility/rate-limit/dedup/quota checks below) is what
            # makes that literal: a disabled SEEN must never claim a
            # dedup slot or consume a quota slot, unlike a normal silent
            # rejection.
            parsed = ParsedCommand(Command.UNKNOWN, "", rejection_reason="seen_disabled")
        # LAB tier (Task 13): POST/READ/BOARDS/MAIL are
        # experimental, gated per-node behind the operator-configured
        # unlock word. A locked node (the tier is off entirely, or this
        # sender hasn't unlocked it) gets exactly the UNKNOWN behavior --
        # no send, no processed_requests row, no quota -- for every
        # `LAB_COMMANDS` member, their HELP entries (including any alias
        # word, hence `LAB_HELP_WORDS`), and the MENU LAB/MENU
        # BOARDS sub-pages, same rewrite-before-any-check pattern as
        # SEEN's gate above. Command.LAB_UNLOCK itself is never gated
        # here -- sending the correct word is exactly how a locked node
        # unlocks the tier.
        lab_locked = (
            self.cfg.service.lab_unlock_word == ""
            or not self._lab.is_unlocked(self._bbs_id(sender_node_id))
        )
        lab_gated_help = parsed.command is Command.CMD_HELP and parsed.argument in LAB_HELP_WORDS
        lab_gated_menu = (
            parsed.command is Command.MENU
            and parsed.argument.strip().upper() in LAB_MENU_PAGES
        )
        public_mail = self.cfg.service.mail_public and (
            parsed.command in (Command.MAIL, Command.REPLY)
            or (parsed.command is Command.CMD_HELP
                and parsed.argument in ("MAIL", "INBOX", "REPLY"))
        )
        if lab_locked and not public_mail and (
            parsed.command in LAB_COMMANDS or lab_gated_help or lab_gated_menu
        ):
            parsed = ParsedCommand(
                Command.UNKNOWN,
                "",
                rejection_reason=(
                    "lab_locked" if self.cfg.service.lab_unlock_word else "lab_disabled"
                ),
            )
        if parsed.command is Command.UNKNOWN and parsed.rejection_reason is None:
            hidden = set(LAB_HELP_WORDS) if lab_locked else set()
            if self.cfg.service.mail_public:
                hidden -= {"MAIL", "INBOX", "REPLY"}
            if not self.cfg.service.seen_enabled:
                hidden.add("SEEN")
            hint = suggest_command(command_text, hidden_words=frozenset(hidden))
            if hint:
                parsed = ParsedCommand(Command.SUGGEST, hint)
        return parsed

    def _trace_and_check_known(
        self, parsed: ParsedCommand, sender_node_id: str, received_at: int, text: str | None,
        *, channel: bool = False,
    ) -> bool:
        """The --debug exchange trace line, plus the UNKNOWN early-return --
        shared verbatim between the two receive paths. Returns True if
        dispatch should proceed."""
        # --debug only (see exchange_log.py): a no-op unless the operator
        # started `run --debug`. Every text DM addressed to the service --
        # known-command or not -- gets exactly one trace line here, before
        # any eligibility/rate-limit outcome is decided.
        cmd_label = "unknown" if parsed.command == Command.UNKNOWN else parsed.command.value
        ts = local_iso(received_at, self.cfg.service.timezone)
        # LAB_UNLOCK's whole message IS the operator-configured unlock
        # word (see parse_command) -- never let it reach this trace, even
        # with --debug on.
        logged_text = "<lab unlock redacted>" if parsed.command == Command.LAB_UNLOCK else text
        exchange_log.remember_node_name(
            sender_node_id, self._report_display_name(sender_node_id)
        )
        route = "channel" if channel else "dm"
        exchange_log.log_line(
            f"{ts} {route} from={exchange_log.node_label(sender_node_id)} "
            f"cmd={cmd_label} text={logged_text!r}"
        )
        if parsed.command == Command.UNKNOWN:
            # Unknown text receives no response.
            if parsed.rejection_reason is None and not is_blocked(
                self._db_conn, self._bbs_id(sender_node_id)
            ):
                usefulness.counter(self._db_conn, "UNKNOWN_REQUESTS", 1, received_at)
            exchange_log.log_rejection(
                sender_node_id, parsed.rejection_reason or "unknown_command_or_invalid_arguments",
                now=received_at, text=text,
            )
            self._metrics.commands_total.labels(parsed.command.value, "rejected").inc()
            return False
        return True

    def _claim_and_gate(
        self, sender_node_id: str, obs: IncomingPacket, now: int, parsed: ParsedCommand
    ) -> bool:
        """Blocked-sender / rate-limit / dedup-claim / quota gates, shared
        verbatim between the two receive paths. Returns True if dispatch
        should proceed; False if this method already recorded the
        rejection (metrics, drop bookkeeping, and, for a quota refusal, the
        terminal DB state)."""
        packet_id = obs.packet_id
        if packet_id is None:
            return False
        if is_blocked(self._db_conn, self._bbs_id(sender_node_id)):
            exchange_log.log_rejection(
                sender_node_id, "blocked", command=parsed.command.value,
                now=now, packet_id=packet_id,
            )
            self._metrics.commands_total.labels(parsed.command.value, "blocked").inc()
            return False

        if self._limiter.check_command(sender_node_id) is not None:
            exchange_log.log_rejection(
                sender_node_id, "command_cooldown", command=parsed.command.value,
                now=now, packet_id=packet_id,
                cooldown_seconds=self.cfg.service.per_node_cooldown_seconds,
            )
            self._metrics.commands_total.labels(parsed.command.value, "rate_limited").inc()
            self._note_drop(now)
            if parsed.command is not Command.SUGGEST:
                self._maybe_send_command_cooldown_notice(
                    sender_node_id, obs, now, parsed
                )
            return False

        # Dedup BEFORE quota: claim() must run first so a retransmitted/
        # duplicate packet id can never consume a quota slot. Counting
        # quota ahead of dedup let a duplicate (which never produces a
        # second reply) silently burn a slot that a later, genuinely new
        # request then needed -- e.g. quota=2 with packet ids 1, 1, 2
        # yielded only one reply instead of two.
        if not self._db_request_log.claim(sender_node_id, packet_id, now, parsed.command):
            exchange_log.log_rejection(
                sender_node_id, "duplicate_request", command=parsed.command.value,
                now=now, packet_id=packet_id,
            )
            self._metrics.commands_total.labels(parsed.command.value, "deduplicated").inc()
            return False

        quota_day = local_day(now, self.cfg.service.timezone)
        if not self._quota.check_and_count_command(sender_node_id, quota_day, now):
            self._metrics.commands_total.labels(parsed.command.value, "quota").inc()
            self._note_drop(now)
            notice_queued = False
            if parsed.command is not Command.SUGGEST:
                notice_queued = self._maybe_send_quota_notice(
                    sender_node_id, obs, now, quota_day, "command"
                )
            if notice_queued:
                exchange_log.log_rejection(
                    sender_node_id, "quota", command=parsed.command.value,
                    now=now, packet_id=packet_id,
                )
            else:
                # The row already exists (claim() above), so settle a silent
                # refusal rather than leaving it 'received' forever.
                self._db_request_log.set_state(
                    sender_node_id, packet_id, "dropped", now, reason="quota"
                )
            return False
        return True

    def _maybe_send_quota_notice(
        self,
        sender_node_id: str,
        obs: IncomingPacket,
        now: int,
        quota_day: str,
        quota_kind: str,
    ) -> bool:
        """Queue one throttled explanation for a daily quota refusal.

        The quota gate has already claimed this request. A kind- and day-scoped
        throttle key keeps repeated refusals
        quiet without letting one quota class suppress another, or a notice
        near midnight suppress the next day's first notice. Like a cooldown
        notice, this informational DM consumes no quota, does not advance
        command cooldown, and is never retried.
        """
        refusal = self._queue.reserve(sender_node_id)
        if refusal is not None:
            return False

        throttle_key = f"quota:{quota_kind}:{quota_day}:{sender_node_id}"
        if not self._command_cooldown_notices.try_claim(throttle_key, now):
            return False

        labels = {"command": "command", "post": "POST", "mail": "MAIL"}
        body = truncate_utf8(
            f"📊 Daily {labels[quota_kind]} quota reached. "
            f"Resets at 00:00 {self.cfg.service.timezone}.",
            self._transport.max_text_bytes,
        )
        return self._reserve_and_send(
            sender_node_id,
            obs,
            now,
            body,
            commit_rate_limit=False,
            is_cooldown_notice=True,
        )

    def _maybe_send_command_cooldown_notice(
        self, sender_node_id: str, obs: IncomingPacket, now: int, parsed: ParsedCommand
    ) -> None:
        """Queue one throttled explanation for a newly rejected command.

        This runs only after eligibility, known-command, and block-list gates.
        Claiming before the notice throttle makes retransmission of this same
        packet id silent. It does not consume daily quota or advance the
        original monotonic command cooldown.
        """
        packet_id = obs.packet_id
        if packet_id is None:
            return
        if self._db_request_log.get_state(sender_node_id, packet_id) is not None:
            return
        if not self._db_request_log.claim(sender_node_id, packet_id, now, parsed.command):
            return

        # Do not consume the persistent notice allowance when no queue slot
        # exists. The normal sender-key slot also prevents the notice from
        # displacing an earlier legitimate reply still in the queue.
        refusal = self._queue.reserve(sender_node_id)
        if refusal is not None:
            self._db_request_log.set_state(
                sender_node_id, packet_id, "dropped", now, reason=refusal.value
            )
            return
        # A private explanation must not suppress feedback in the public
        # channel. Keep the legacy DM key for persisted throttles.
        notice_key = (
            sender_node_id if obs.reply_channel_idx is None
            else f"channel:{obs.reply_channel_idx}:{sender_node_id}"
        )
        if not self._command_cooldown_notices.try_claim(notice_key, now):
            self._db_request_log.set_state(
                sender_node_id, packet_id, "dropped", now, reason=Reject.RATE_LIMITED.value
            )
            return

        remaining = self._limiter.command_cooldown_remaining(sender_node_id)
        body = truncate_utf8(
            f"⏳ Please wait {remaining}s, then resend your command.",
            self._transport.max_text_bytes,
        )
        if obs.reply_channel_idx is not None:
            prefix = "⏳ "
            suffix = f": wait {remaining}s, then resend your command."
            name_budget = max(
                0, self._transport.max_text_bytes - len((prefix + suffix).encode("utf-8"))
            )
            name = truncate_utf8(
                self._lookup_display_name(sender_node_id) or sender_node_id, name_budget
            )
            body = truncate_utf8(prefix + name + suffix, self._transport.max_text_bytes)
        self._reserve_and_send(
            sender_node_id,
            obs,
            now,
            body,
            commit_rate_limit=False,
            is_cooldown_notice=True,
        )

    def _dispatch_command(
        self, parsed: ParsedCommand, sender_node_id: str, obs: IncomingPacket, now: int
    ) -> None:
        """The command dispatch chain, shared verbatim between the two
        receive paths. `obs` is a real `IncomingPacket` for Meshtastic, or
        a synthetic one built by `_synthetic_incoming_packet` for MeshCore
        (see that method for exactly which fields are honest and which are
        inert placeholders)."""
        usefulness.record(self._db_conn, sender_node_id, parsed.command.value, now)
        if parsed.command == Command.NEW:
            self._resolve_pending_mail_deliveries(now)
            bbs_sender = self._bbs_id(sender_node_id)
            lab = bool(self.cfg.service.lab_unlock_word and self._lab.is_unlocked(bbs_sender))
            body, snapshot = updates.summary(
                self._db_conn, bbs_sender, now, boards_enabled=lab,
                mail_enabled=(self.cfg.service.mail_public or lab),
                max_bytes=self._transport.max_text_bytes,
            )
            sent = self._reserve_and_send(sender_node_id, obs, now, body)
            if sent:
                updates.remember(self._db_conn, bbs_sender, obs.packet_id, snapshot, now)
            self._metrics.commands_total.labels("NEW", "accepted" if sent else "queue_full").inc()
            return
        if parsed.command == Command.CHECK:
            body = usefulness.check_text(self._db_conn, sender_node_id, obs, now,
                                         self._transport.max_text_bytes)
            sent = self._reserve_and_send(sender_node_id, obs, now, body)
            self._metrics.commands_total.labels("CHECK", "accepted" if sent else "queue_full").inc()
            return
        if parsed.command == Command.REPLY:
            parts = parsed.argument.split(None, 1)
            ident = parts[0].removeprefix("#") if parts else ""
            route = self._mail.lookup_reply_route(
                int(ident), self._bbs_id(sender_node_id), now
            ) if (
                len(parts) == 2 and ident.isascii() and ident.isdigit() and len(ident) <= 18
            ) else None
            if route is None:
                sent = self._reserve_and_send(
                    sender_node_id, obs, now,
                    "REPLY unavailable. Use REPLY #id <msg> before note expiry.")
                self._metrics.commands_total.labels(
                    "REPLY", "rejected" if sent else "queue_full").inc()
            else:
                # The stored sender may belong to the other mesh; its
                # prefixed form keeps the reply addressed to that mesh.
                self._handle_mail_send(
                    sender_node_id, f"{self._bbs_argument(route.sender_node_id)} {parts[1]}",
                    obs, now)
            return
        if parsed.command == Command.SIGN:
            self._handle_sign(sender_node_id, parsed.argument, obs, now)
            return

        if parsed.command == Command.BOOK:
            self._handle_book(sender_node_id, parsed.argument, obs, now)
            return

        if parsed.command == Command.GUESTBOOK:
            self._handle_guestbook(sender_node_id, parsed.argument, obs, now)
            return

        if parsed.command == Command.WORDLE:
            self._handle_wordle(sender_node_id, parsed.argument, obs, now)
            return

        if parsed.command == Command.UNSIGN:
            self._handle_unsign(sender_node_id, obs, now)
            return

        if parsed.command == Command.SEEN:
            self._handle_seen(
                sender_node_id, parsed.argument, obs, now, mesh=self._peer_mesh(parsed.mesh)
            )
            return

        if parsed.command == Command.ME:
            self._handle_me(sender_node_id, obs, now)
            return

        if parsed.command == Command.PATH:
            body = route_reply(
                self._db_conn, sender_node_id, obs,
                meshcore=isinstance(self._transport, MeshCoreTransport),
                now=now, max_bytes=self._transport.max_text_bytes,
            )
            sent = self._reserve_and_send(sender_node_id, obs, now, body)
            outcome = "accepted" if sent else "queue_full"
            self._metrics.commands_total.labels(Command.PATH.value, outcome).inc()
            return

        if parsed.command == Command.PING:
            self._handle_ping(sender_node_id, obs, now)
            return

        if parsed.command == Command.POSITION:
            self._handle_position(parsed, sender_node_id, obs, now)
            return

        if parsed.command == Command.WX:
            self._handle_wx(sender_node_id, obs, now)
            return

        if parsed.command == Command.REPEATER:
            self._handle_repeater(sender_node_id, obs, now)
            return

        if parsed.command == Command.POST:
            self._handle_post(sender_node_id, parsed.argument, obs, now)
            return

        if parsed.command == Command.READ:
            self._handle_read(sender_node_id, parsed.argument, obs, now)
            return

        if parsed.command == Command.MAIL:
            self._handle_mail(sender_node_id, parsed.argument, obs, now)
            return

        if parsed.command == Command.LAB_UNLOCK:
            self._handle_lab_unlock(sender_node_id, obs, now)
            return

        self._handle_simple_command(
            parsed.command, sender_node_id, obs, now, parsed.argument,
            mesh=self._peer_mesh(parsed.mesh),
        )

    # -- MeshCore receive path (task 3B-2 item B) -------------------------
    #
    # No pubsub ingest thread to reuse (see _meshcore_message_sink above):
    # this is driven from the DB worker thread by an Inbound dequeued from
    # _ingest, exactly like _process_obs is driven by an IncomingPacket.

    def _process_meshcore_inbound(self, inbound: Inbound) -> None:
        """Handle commands; companion observations arrive independently of eligibility."""
        if not self._transport.identity_is_unambiguous(inbound.sender):
            return
        tstatus = self._transport.status()
        connected_seconds = (
            tstatus.connected_seconds if tstatus.connected_seconds is not None else float("inf")
        )
        decision = decide_inbound(
            inbound,
            me=self._transport.identity(),
            reply_ready=tstatus.reply_ready,
            reply_ttl_seconds=self.cfg.service.reply_ttl_seconds,
            connected_seconds=connected_seconds,
            channel_policy=self._meshcore_channel_policy,
            channel_commands=self.cfg.service.mode == "channel",
        )
        if decision.outcome is Outcome.KNOCK:
            self._handle_knock(inbound)
            return
        if decision.outcome is not Outcome.COMMAND:
            reason = decision.reason
            outcome = _ELIGIBILITY_OUTCOME.get(reason, "rejected")
            log.debug("meshcore packet ineligible reason=%s", reason.value)
            # Ordinary chatter on a channel that is not the garden's own
            # (Public, mostly) is rejected as BROADCAST many times an hour.
            # It is not traffic meant for the garden, so it stays out of the
            # exchange trace -- `mgarden tail` is for DMs and the watched
            # channel -- while the metric and the debug log still count it.
            if not (reason is Reject.BROADCAST and inbound.channel is ChannelScope.OTHER):
                exchange_log.log_rejection(
                    inbound.sender, reason.value, now=inbound.received_at,
                    packet_id=inbound.raw_id, text=inbound.text,
                )
            if ChannelTriggerPolicy().matches(inbound.text):
                log.info("meshcore knock ignored reason=%s", reason.value)
            self._warn_if_stale(inbound, reason, self.cfg.service.reply_ttl_seconds)
            self._metrics.commands_total.labels("UNKNOWN", outcome).inc()
            return
        # A DM or accepted watched-channel input got through, so any
        # stale episode is over -- same re-arm _process_obs does.
        self._stale_warned = False

        sender_node_id = inbound.sender
        parsed = self._parse_command_for_dispatch(decision.command_text, sender_node_id)
        from_channel = inbound.channel is ChannelScope.WATCHED
        mail_only = (
            parsed.command in (Command.MAIL, Command.REPLY)
            or (
                parsed.command is Command.CMD_HELP
                and parsed.argument in ("MAIL", "INBOX", "REPLY")
            )
        )
        public_channel_command = parsed.command in _CHANNEL_PUBLIC_COMMANDS and not (
            parsed.command is Command.MENU and bool(parsed.argument)
        )
        # A channel display name is not authenticated. Mail content and mail
        # controls therefore remain strictly DM-only, and typo suggestions do
        # not turn ordinary channel chatter into bot traffic.
        if from_channel and (mail_only or not public_channel_command):
            exchange_log.log_rejection(
                sender_node_id,
                "channel_dm_only",
                command=parsed.command.value,
                now=inbound.received_at,
                packet_id=inbound.raw_id,
                text=inbound.text,
            )
            self._metrics.commands_total.labels(parsed.command.value, "rejected").inc()
            return
        if not self._trace_and_check_known(
            parsed, sender_node_id, inbound.received_at, inbound.text,
            channel=from_channel,
        ):
            return

        now = inbound.received_at
        obs = self._synthetic_incoming_packet(inbound)
        if not self._claim_and_gate(sender_node_id, obs, now, parsed):
            return

        self._dispatch_command(parsed, sender_node_id, obs, now)

    def _handle_knock(self, inbound: Inbound) -> None:
        """The ENTIRE knock outcome (task 3B-3; scope-decisions.md section
        7): at most one rate-limited direct message to the resolved
        contact, carrying invite text. A knock remains distinct from the
        optional public command path and never replies on the channel.

        THE COOLDOWN IS CHECKED BEFORE ANY SEND, STRUCTURALLY: `_knock_
        cooldown.try_claim` is one atomic database statement that both
        checks and records the cooldown (see `ChannelKnockCooldown`).
        There is no separate "check" step to call late or skip -- the only
        way execution reaches `_reserve_and_send` below is through this
        method's own early `return` NOT firing, which happens only when
        `try_claim` has ALREADY recorded `now` as this node's last knock.
        A send can therefore never precede, or happen without, the record.

        Fix round 1: two things a review found wrong with the first cut.
        (1) The knock must never commit the generic per-node
        `RateLimiter` (item 1) -- see `_reserve_and_send`'s own docstring
        for why: a forged knock committing that shared limiter silently
        denied the IMPERSONATED node's own next genuine command. (2) A
        knock whose DM definitely never transmitted (queue-full, TTL
        expiry, radio not ready) releases its cooldown claim rather than
        silencing a legitimate knocker for the whole window over a failure
        that was never their fault (item 2) -- never for a blocked sender,
        and never for an attempted-or-uncertain outcome; see
        `ChannelKnockCooldown.release` and `ReplyJob.
        release_on_pretransmission_failure` for exactly which cases this
        covers and why.

        Fix round 2 (a Critical in fix round 1's item 2): the release
        callback below goes through `_enqueue_knock_release`, NEVER
        `self._knock_cooldown.release` directly -- `_finish_expired`/
        `_mark_dropped` call it from the TX worker thread, and `self.
        _knock_cooldown` is built on the DB worker thread's own
        connection. See `_KnockCooldownRelease`'s docstring for the full
        story of what calling `release` directly there actually did.

        Task 3C findings 1 and 2: the knock's reply is enqueued with a
        namespaced `reply_queue_key` (never the plain `node_id`) and
        `is_knock=True` -- see `_reserve_and_send`'s and `ReplyQueue`'s own
        docstrings. Without this, a forged knock occupied the IMPERSONATED
        node's own single `ReplyQueue` slot, so that node's own next
        genuine command was refused with no reply at all (after already
        burning a dedup row and a quota slot at `_claim_and_gate`) -- and a
        burst of forged knocks, each naming a different attacker-minted
        contact, could still fill the shared queue and starve everyone
        else even after that first fix, since each knock's slot key would
        otherwise be unique. Both fixed here, together, since one is not
        useful without the other.
        """
        # Fix round 1, item 4: decide_inbound only ever returns
        # Outcome.KNOCK when channel_policy is not None (row 1b's own
        # guard) -- true today, but a bare `self._meshcore_channel_policy.
        # cooldown_seconds` dereference would fail only at the attribute
        # access if that invariant were ever broken by a future change to
        # that function. Make the dependency explicit instead, so a
        # violation fails loudly and legibly right here.
        channel_policy = self._meshcore_channel_policy
        if channel_policy is None:
            raise RuntimeError(
                "_handle_knock reached with no channel policy configured -- "
                "decide_inbound must only return Outcome.KNOCK when "
                "channel_policy is not None"
            )

        node_id = inbound.sender
        now = inbound.received_at
        cooldown_seconds = channel_policy.cooldown_seconds
        if not self._knock_cooldown.try_claim(node_id, now, cooldown_seconds):
            exchange_log.log_rejection(
                node_id, "knock_cooldown", command=Command.KNOCK.value, now=now,
                packet_id=inbound.raw_id, cooldown_seconds=cooldown_seconds,
            )
            log.info("meshcore knock ignored reason=cooldown recipient=%s", node_id)
            # Rate-limited: silent drop. No DM and no channel reply.
            # Reported as Reject.RATE_LIMITED's own metric label,
            # exactly as decide_inbound's docstring anticipates.
            self._metrics.commands_total.labels(Command.KNOCK.value, "rate_limited").inc()
            return

        # A knock remains an invitation DM even when channel-command mode is
        # enabled; it is not itself a command reply.
        obs = replace(self._synthetic_incoming_packet(inbound), reply_channel_idx=None)
        # Command.KNOCK is a bookkeeping label ONLY (see its own docstring
        # in commands.py) -- claim() gives this reply a processed_requests
        # row so it can reuse _reserve_and_send/Sender exactly like every
        # real command's reply does (the same send-time `get_state(...) ==
        # "queued"` gate, the same queueing/truncation/ack correlation). It
        # is never passed to _dispatch_command; nothing constructs a
        # ParsedCommand naming it anywhere in the tree.
        if not self._db_request_log.claim(node_id, obs.packet_id, now, Command.KNOCK):
            exchange_log.log_rejection(
                node_id, "duplicate_request", command=Command.KNOCK.value,
                now=now, packet_id=obs.packet_id,
            )
            # Re-delivery of the identical physical channel message. The
            # cooldown claim above already recorded this attempt regardless
            # -- a STRICTER, never a looser, bound on this recipient's rate.
            # Fix round 1, item 3: this path previously recorded no metric
            # at all, unlike every other rejection here -- "deduplicated"
            # matches _claim_and_gate's own naming for the identical
            # concept on the DM path.
            self._metrics.commands_total.labels(Command.KNOCK.value, "deduplicated").inc()
            return

        body = truncate_utf8(knock_text(), self._transport.max_text_bytes)
        sent = self._reserve_and_send(
            node_id,
            obs,
            now,
            body,
            # Item 1: never let a forged knock rate-limit the impersonated
            # node's own next genuine command via the shared limiter.
            commit_rate_limit=False,
            # Item 2: a definite pre-transmission failure (never a blocked
            # sender, never an attempted-or-uncertain outcome -- see
            # ChannelKnockCooldown.release's own docstring) releases this
            # specific claim, identified by the exact timestamp it recorded.
            # Fix round 2: marshaled via _enqueue_knock_release -- this
            # closure is safe to call from ANY thread (it only ever puts
            # onto the thread-safe ingest queue), whether that is the DB
            # worker thread (_reserve_and_send's own immediate refusal,
            # called synchronously from here) or the TX worker thread
            # (Sender's asynchronous _finish_expired/_mark_dropped).
            on_pretransmission_failure=lambda reason: self._enqueue_knock_release(node_id, now),
            # Findings 1/2: namespaced so this reply's slot can never
            # collide with node_id's OWN plain slot, and flagged so a
            # burst of knocks (each to a different attacker-minted
            # contact, each with a different key) is still bounded as a
            # group by ReplyQueue's separate max_pending_knocks.
            reply_queue_key=f"knock:{node_id}",
            is_knock=True,
        )
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.KNOCK.value, outcome).inc()

    def _synthetic_incoming_packet(self, inbound: Inbound) -> IncomingPacket:
        """Builds a MINIMAL `IncomingPacket`-shaped record so MeshCore's
        trigger/reply path can reuse `_dispatch_command`/`_handle_*`/
        `_reserve_and_send` without a second copy of the command-handling
        machinery. This is NEVER an observation -- it is never passed to
        `self._collector.record()`, never persisted, and every field only
        the observation path cares about (from_num/to_num/channel/portnum
        and every NODEINFO/POSITION/TELEMETRY field) is an inert
        placeholder. `transport-boundary-spec.md` section 1 deliberately
        does not merge `Inbound` and `IncomingPacket`; this method exists
        only to let the handful of fields `_handle_*`/`_reserve_and_send`
        actually read (packet_id, received_at, received_monotonic, rx_snr,
        rx_rssi, hop_start, hop_limit, pki_encrypted, payload_len) flow
        through the shared dispatch path honestly.
        """
        hop_start: int | None = None
        hop_limit: int | None = None
        if inbound.path_len is not None:
            # Inbound.path_len is the known flood hop count; direct-route
            # sentinel hops remain unknown -- see
            # meshcore_transport._normalize_hops); hop_limit=0 reproduces
            # the same "hops away" difference _process_obs's own hops_away
            # math (and ping_text/me_text) compute from hop_start-hop_limit.
            hop_start, hop_limit = inbound.path_len, 0
        text = inbound.text or ""
        return IncomingPacket(
            received_at=inbound.received_at,
            # Stamped at PROCESS time (DB worker thread), not at ingest
            # time the way _ingest_sink stamps it for Meshtastic -- Inbound
            # carries no monotonic field to carry one through on. Bounded
            # imprecision: at most one _DB_LOOP_POLL_SECONDS-ish queueing
            # delay against the TTL deadline this feeds via
            # _reserve_and_send, negligible next to reply_ttl_seconds.
            received_monotonic=self._clock.monotonic(),
            from_num=0,
            to_num=0,
            packet_id=inbound.raw_id,
            channel=inbound.channel_index or 0,
            portnum="TEXT_MESSAGE_APP",
            rx_time=None,
            rx_snr=inbound.signal.snr,
            rx_rssi=inbound.signal.rssi,
            hop_limit=hop_limit,
            hop_start=hop_start,
            via_mqtt=None,
            # MeshCore encrypts all contact traffic by construction but
            # surfaces no per-message PKI flag (Inbound.encrypted_to_us is
            # always None -- transport-boundary-spec.md section 1);
            # "encrypted by default" is not the same claim as "this packet
            # was PKI-encrypted to us", so this is never hardcoded True.
            pki_encrypted=inbound.encrypted_to_us,
            payload_len=len(text.encode("utf-8")),
            text=inbound.text,
            request_id=None,
            routing_error=None,
            reply_channel_idx=(
                inbound.channel_index if inbound.channel is ChannelScope.WATCHED else None
            ),
        )

    def _process_meshcore_contact(self, contact: _MeshCoreContact) -> None:
        """Store names/positions; separate live observation events maintain activity."""
        exchange_log.remember_node_name(contact.node_id, contact.display_name)
        self._collector.record_contact_identity(
            contact.node_id,
            display_name=contact.display_name,
            lat=contact.lat,
            lon=contact.lon,
            observed_at=contact.observed_at,
        )

    def _process_knock_cooldown_release(self, release: _KnockCooldownRelease) -> None:
        """Fix round 2 (task 3B-3): the ONLY place `ChannelKnockCooldown.
        release` is ever called -- runs on the DB worker thread (arrived
        via `_ingest`, like every other DB-owning event), the single
        owner of `self._knock_cooldown`'s connection. `_enqueue_knock_
        release` is the only caller that may construct a
        `_KnockCooldownRelease`; nothing else in the tree calls `self.
        _knock_cooldown.release` directly."""
        self._knock_cooldown.release(release.node_id, release.claimed_at)

    def _alert_channel_idx(self) -> int | None:
        return getattr(self._transport, "alert_channel_idx", None)

    def _process_weather_batch(self, batch: WeatherBatch) -> None:
        """DB worker thread. Turns the ledger's plans into ordinary
        ReplyJobs: the garden's own node id as request_sender (there is no
        requester), a stable hash as packet id, Command.WEATHER on the
        processed_requests row so Sender's 'queued' guard holds, and the
        alert channel as the reply surface. Nothing here touches the
        transport."""
        self._metrics.weather_polls_total.labels(batch.result).inc()
        if batch.result == "error" or self._weather_ledger is None:
            return
        self._metrics.weather_last_success_timestamp_seconds.set(batch.fetched_at)
        now = int(time.time())
        for plan in self._weather_ledger.apply(batch.products, now):
            self._enqueue_weather_line(
                key=plan.product_key, kind=plan.kind, line=plan.line, now=now
            )

    def _enqueue_weather_line(
        self, *, key: str, kind: str, line: str, now: int, retry_on_drop: bool = True
    ) -> bool:
        """DB worker thread. True when Sender accepted the job. With
        `retry_on_drop` (alerts) a refusal or a pre-transmission failure marks
        the ledger row `dropped`; the heartbeat has no row, so it neither
        marks nor is told about later failures."""
        assert self._weather_ledger is not None
        idx = self._alert_channel_idx()
        if idx is None:
            if not self._weather_unresolved_logged:
                log.warning(
                    "weather: alert channel %r is not resolved on the radio; "
                    "lines are dropped until it is",
                    self.cfg.weather.channel,
                )
                self._weather_unresolved_logged = True
            if retry_on_drop:
                self._weather_ledger.mark(key, "dropped", now)
            return False
        self._weather_unresolved_logged = False
        sender = self.cfg.radio.expected_node_id
        # `now` is in the hash so a retry of an identical line gets a fresh
        # packet id instead of colliding with its earlier terminal row.
        self._weather_seq += 1
        digest = hashlib.sha256(f"{key}|{kind}|{line}|{now}|{self._weather_seq}".encode()).digest()
        packet_id = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
        if not self._db_request_log.claim(sender, packet_id, now, Command.WEATHER):
            if retry_on_drop:
                self._weather_ledger.mark(key, "suppressed", now)
            return False
        self._db_request_log.set_state(sender, packet_id, "queued", now)
        job = ReplyJob(
            request_sender=sender,
            request_packet_id=packet_id,
            request_received_at=now,
            deadline_monotonic=self._clock.monotonic() + self.cfg.service.reply_ttl_seconds,
            body=line,
            release_on_pretransmission_failure=(
                (lambda _reason, k=key: self._enqueue_weather_drop(k)) if retry_on_drop else None
            ),
            reply_queue_key=f"weather:{key}",
            reply_channel_idx=idx,
        )
        reason = self._sender.enqueue(job)
        if reason is not None:
            self._db_request_log.set_state(sender, packet_id, "dropped", now, reason=reason.value)
            if retry_on_drop:
                self._weather_ledger.mark(key, "dropped", now)
            return False
        self._metrics.weather_lines_total.labels(kind).inc()
        exchange_log.log_line(
            f"{local_iso(now, self.cfg.service.timezone)} weather {kind} idx={idx} text={line!r}"
        )
        return True

    def _maybe_send_weather_heartbeat(self, now: int) -> None:
        """Once per local day, at or after the configured hour -- "at or
        after" so a daemon that was down at that hour still sends the day's
        line when it comes back, and a DST-skipped hour cannot lose a day.
        The day is recorded only when the enqueue was accepted, so a refused
        enqueue retries next tick; a line dropped after acceptance is not
        retried until tomorrow."""
        hour = self.cfg.weather.heartbeat_hour
        if hour < 0 or not self._weather_enabled or self._weather_ledger is None:
            return
        local = datetime.fromtimestamp(now, tz=ZoneInfo(self.cfg.service.timezone))
        today = local.strftime("%Y-%m-%d")
        if local.hour < hour or self._weather_ledger.get_state("heartbeat_day") == today:
            return
        line = self._weather_status_line(now, lead="wx-bay daily")
        if self._enqueue_weather_line(
            key=f"heartbeat:{today}", kind="heartbeat", line=line, now=now, retry_on_drop=False
        ):
            self._weather_ledger.set_state("heartbeat_day", today, now)

    def _enqueue_weather_drop(self, product_key: str) -> None:
        """Safe from ANY thread: only enqueues (see _enqueue_knock_release)."""
        try:
            self._ingest.put_nowait(_WeatherDropped(product_key=product_key))
        except queue.Full:
            log.warning("weather: ingest queue full; drop for %s not recorded", product_key)

    def _weather_status(self) -> dict[str, Any]:
        poller = self._weather_poller
        now = int(time.time())
        # The ledger's connection belongs to the DB worker; status_snapshot is
        # also called from other threads (tests, CLI), so only count there.
        return {
            "enabled": self._weather_enabled,
            "channel_resolved": self._alert_channel_idx() is not None,
            "last_poll_ok_at": poller.last_success_at if poller is not None else None,
            "backoff_seconds": poller.backoff_seconds if poller is not None else 0.0,
            "sent_last_hour": (
                self._weather_ledger.sent_in_last_hour(now)
                if self._weather_ledger is not None
                and threading.current_thread() is self._db_thread
                else None
            ),
        }

    def _handle_simple_command(
        self,
        command: Command,
        sender_node_id: str,
        obs: IncomingPacket,
        now: int,
        argument: str = "",
        mesh: str | None = None,
    ) -> None:
        """MENU / INFO / STATS / ICHING / HAIKU / NOT_EMERGENCY / GARDEN /
        RECORD / WEEK / CMD_HELP / WHO / BOARDS / VERSION: a single fixed (aggregate-
        only) response body, with no extra rate limiting or storage beyond
        the shared checks _process_obs already ran. GUESTBOOK and WORDLE have
        their own handlers (`_handle_guestbook`, `_handle_wordle`) -- the
        former needs its parsed argument (page / #id) to pick a read
        helper, the latter needs the queue-availability-before-consuming-a-
        guess ordering neither this generic path nor `_build_response_body`
        provides. UNSIGN, SEEN, ME, and PING also have their own handlers
        (`_handle_unsign`, `_handle_seen`, `_handle_me`, `_handle_ping`):
        PING needs the request's own IncomingPacket fields, UNSIGN mutates
        guestbook state with the same reserve-before-mutate ordering as
        WORDLE, SEEN needs its parsed argument, and ME needs the request's
        own IncomingPacket fields plus several lookups `_build_response_body`
        has no access to. WHOAMI/SIGNAL are aliases of ME (see commands.py's
        `_BARE_COMMANDS`), never reaching this generic path themselves.

        `argument` is only meaningful for MENU (selects the top-level menu
        vs. the MENU STATS/MENU BOARDS/MENU GAMES/MENU DAILY sub-page, see
        `menu_reply_for`) and CMD_HELP (the command word to look up, see
        `help_text`); every other command reached here takes none and gets
        the default ""."""
        body = truncate_utf8(
            (
                channel_menu_text()
                if command is Command.MENU and obs.reply_channel_idx is not None
                else self._build_response_body(command, sender_node_id, now, argument, mesh)
            ),
            self._transport.max_text_bytes,
        )
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(command.value, outcome).inc()

    def _handle_repeater(
        self, sender_node_id: str, obs: IncomingPacket, now: int
    ) -> None:
        client = self._meshcore_client
        query = getattr(client, "repeater_status", None)
        state = "unavailable"
        if not callable(query):
            body = "📡 Repeater stats\nUnavailable on this radio."
        else:
            state, stats, age, error = query(start_refresh=False)
            if stats is None:
                if state in ("idle", "pending"):
                    body = "⏳ Querying repeater…\nSend REPEATER again shortly."
                elif state == "disabled":
                    body = "📡 Repeater stats\nNot configured."
                else:
                    body = "📡 No repeater reply.\nTry REPEATER again later."
            else:
                key = self.cfg.radio.meshcore_repeater_public_key
                label = self._node_report_label("!" + key[:8].lower()) if key else None
                body = repeater_text(
                    stats, label=label, age=age, stale=state == "stale",
                    max_bytes=self._transport.max_text_bytes,
                )
        body = truncate_utf8(body, self._transport.max_text_bytes)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        # RF work starts only after this command has successfully reserved a
        # DM reply.  A full reply queue can therefore never trigger an
        # unactionable repeater login/status transmission.
        if sent and callable(query) and state in ("idle", "stale"):
            query()
        self._metrics.commands_total.labels(Command.REPEATER.value,
                                            "accepted" if sent else "queue_full").inc()

    def _build_response_body(
        self, command: Command, sender_node_id: str, now: int, argument: str = "",
        mesh: str | None = None,
    ) -> str:
        if mesh is not None:
            # Only WHO/GARDEN/RECORD parse a mesh suffix; `mesh` is already
            # the peer's (own-mesh suffixes were normalised away).
            return self._peer_response_body(command, mesh, now)
        if command == Command.MENU:
            # Same "tier on AND this sender unlocked it" test as
            # _process_obs's `lab_locked` gate -- an unlocked node's own
            # top-level MENU/? gains the "MENU LAB" line
            # (see menu_text's lab_unlocked param); a locked node's menu is
            # unaffected.
            lab_unlocked = (
                self.cfg.service.lab_unlock_word != ""
                and self._lab.is_unlocked(self._bbs_id(sender_node_id))
            )
            body = menu_reply_for(argument, lab_unlocked=lab_unlocked,
                                  mail_public=self.cfg.service.mail_public)
            # Only the top-level MENU/? reply carries the quota hint --
            # never MENU STATS/MENU BOARDS/MENU GAMES/MENU DAILY/MENU LAB,
            # and never an echo of `argument` itself (menu_reply_for
            # already guarantees that part).
            if argument.strip().upper() not in (
                "STATS", "GAMES", "BOARDS", "LAB", "DAILY", "SOCIAL"
            ):
                day = local_day(now, self.cfg.service.timezone)
                used, _ = self._quota.usage(sender_node_id, day)
                body = with_quota_hint(
                    body,
                    used,
                    self.cfg.service.daily_command_quota,
                    max_bytes=self._transport.max_text_bytes,
                )
            return body
        if command == Command.NEWS:
            if argument and not (argument.isascii() and argument.isdigit() and len(argument) <= 4
                                 and 1 <= int(argument) <= 9999):
                return "📰 News\nUse NEWS [page], e.g. NEWS 2"
            return community.news_text(self._db_conn, int(argument or 1), now,
                                       self._transport.max_text_bytes)
        if command == Command.GUIDE:
            return community.guide_text(argument, self._transport.max_text_bytes)
        if command == Command.SUGGEST:
            wait = self.cfg.service.per_node_cooldown_seconds
            return f"❓ Unknown command\nTry {argument} or MENU.\nWait {wait}s between commands."
        if command == Command.NOT_EMERGENCY:
            return not_emergency_text(max_bytes=self._transport.max_text_bytes)
        if command == Command.INFO:
            shared_with = None
            if self.cfg.database.resolved_bbs_path is not None:
                shared_with = _MESH_LABELS[self._peer_transport_name()]
            return info_text(self.cfg.service.name, shared_with, self._transport.max_text_bytes)
        if command == Command.STATS:
            return format_stats(
                senders_24h=self._collector.distinct_senders_since(now - 86400),
                unique_packets_24h=self._collector.unique_packets_since(now - 86400),
                guestbook_visible=self._guestbook.visible_count(),
                uptime_seconds=now - self.started_at,
                max_bytes=self._transport.max_text_bytes,
                companion_events=isinstance(self._transport, MeshCoreTransport),
            )
        if command == Command.SUN:
            lat, lon = self.cfg.observatory.latitude, self.cfg.observatory.longitude
            if (lat, lon) == (0.0, 0.0):
                lat = lon = None
            try:
                from meshgarden.sun import sun_text
            except ImportError:
                log.warning(
                    "SUN unavailable: synchronize dependencies using scripts/install-rpi.sh"
                )
                return "☀️ SUN unavailable\nAstronomy data support needs updating."
            return sun_text(now, latitude=lat, longitude=lon, timezone="America/Los_Angeles")
        if command == Command.MOON:
            return moon_text(now)
        if command == Command.ICHING:
            day = local_day(now, self.cfg.service.timezone)
            return iching_text(
                sender_node_id, day, self.cfg.service.fortune_salt,
                max_bytes=self._transport.max_text_bytes,
            )
        if command == Command.HAIKU:
            return haiku_text(
                senders_24h=self._collector.distinct_senders_since(now - 86400),
                guestbook_visible=self._guestbook.visible_count(),
                place=self.cfg.service.haiku_place,
                rng=self._haiku_rng,
                max_bytes=self._transport.max_text_bytes,
            )
        if command == Command.GARDEN:
            return self._garden_body(self._collector, now, self._transport.max_text_bytes)
        if command == Command.RECORD:
            return self._record_body(
                self._collector, self._db_conn, now, self._transport.max_text_bytes,
                companion_events=isinstance(self._transport, MeshCoreTransport),
            )
        if command == Command.WHO:
            return who_text(
                heard_1h=self._collector.distinct_senders_since(now - 3600),
                direct_1h=self._collector.distinct_direct_senders_since(now - 3600),
                heard_24h=self._collector.distinct_senders_since(now - 86400),
            )
        if command == Command.WEEK:
            today = datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d")
            summary = week_summary(self._db_conn, today)
            return week_text(
                total_packets=summary.total_packets,
                senders_min=summary.senders_min,
                senders_max=summary.senders_max,
                guestbook_entries=summary.guestbook_entries,
                replies=summary.replies,
                companion_events=isinstance(self._transport, MeshCoreTransport),
            )
        if command == Command.CMD_HELP:
            return help_text(argument) or menu_text()
        if command == Command.BOARDS:
            return boards_text(self._guestbook.topic_counts())
        if command == Command.VERSION:
            return version_text(__version__)
        raise AssertionError(f"unsupported command reached body builder: {command!r}")

    def _handle_book(
        self, sender_node_id: str, raw_argument: str, obs: IncomingPacket, now: int
    ) -> None:
        """BOOK / BOOK <guess>: same single-reply dispatch as
        `_handle_simple_command` (no extra rate limiting or storage beyond
        the shared checks `_process_obs` already ran), but the reply body
        depends on `raw_argument`, the visitor's guess. The day's puzzle
        selection itself carries no per-sender state (see
        `booktrivia.puzzle_for`), so every visitor sees the same puzzle on
        the same local day; `book_state` separately tracks, per hashed
        player_key, whether THIS sender already solved it today (see
        `booktrivia.has_solved`/`record_solved`) -- never a raw node id,
        never the guess text, only a solved flag. `raw_argument` is never
        stored or logged -- only the fixed prompt/correct/wrong/already-
        solved reply text and the bounded accepted/queue_full outcome are.

        Every prompt shown and every guess made updates `book_stats` (no
        node ids -- see booktrivia.record_shown/record_book_guess). A
        correct guess picks one of three reply tiers: the book's first-ever
        solve, a "rare catch" (low all-time solve rate with enough
        all-time guesses), or the ordinary "you're the Nth today" reply --
        unless this sender already solved today, in which case none of that
        runs and the fixed already-solved reply is sent instead.

        `BOOK SCORE`/`SCORES` is checked first, before any of the above: it
        returns mesh-wide today/7-day aggregates from `book_stats` and never
        touches `book_state`, never counts as a guess or a shown.
        """
        day = local_day(now, self.cfg.service.timezone)
        puzzle = book_puzzle_for(day, self.cfg.service.fortune_salt)
        key = booktrivia.book_key(puzzle)
        keyword = " ".join(raw_argument.strip().upper().split())
        if keyword in ("SCORE", "SCORES"):
            # Checked before the title-guess branch below, and never
            # touches book_state/book_stats -- see booktrivia.book_score_text.
            body = book_score_text(self._db_conn, day)
        elif raw_argument:
            key_player = booktrivia.player_key(
                self._bbs_id(sender_node_id), day, self.cfg.service.fortune_salt
            )
            if booktrivia.has_solved(self._db_conn, key_player, day):
                body = booktrivia.book_already_solved_text()
            else:
                prior_guesses, prior_solves = booktrivia.alltime_totals(self._db_conn, key)
                correct = guess_matches(raw_argument, puzzle)
                booktrivia.record_book_guess(self._db_conn, key, day, correct)
                if not correct:
                    body = book_wrong_text(puzzle)
                elif prior_solves == 0:
                    body = booktrivia.book_first_solve_text(puzzle)
                elif booktrivia.is_rare_solve(prior_guesses, prior_solves):
                    body = booktrivia.book_rare_solve_text(puzzle)
                else:
                    nth_today = booktrivia.today_solves(self._db_conn, key, day)
                    body = booktrivia.book_nth_solve_text(puzzle, nth_today)
                if correct:
                    booktrivia.record_solved(self._db_conn, key_player, day, now)
        else:
            booktrivia.record_shown(self._db_conn, key, day)
            body = book_prompt_text(puzzle)
        body = truncate_utf8(body, self._transport.max_text_bytes)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.BOOK.value, outcome).inc()

    def _handle_guestbook(
        self, sender_node_id: str, argument: str, obs: IncomingPacket, now: int
    ) -> None:
        """GUESTBOOK / GUESTBOOK <page> / GUESTBOOK #<id>: same single-reply
        dispatch as `_handle_simple_command` (no extra rate limiting or
        storage beyond the shared checks `_process_obs` already ran).
        `argument`'s shape is already validated by `parse_command` (empty,
        a page int 1-999, or `#<id>`) -- this only picks the read helper
        and formatter: empty is the unchanged latest-page behavior,
        `#<id>` is a single visible entry (or a bounded not-found reply),
        and a bare digit is always a page number, never an entry id (see
        commands.py's `_parse_guestbook_argument`)."""
        per_page = self.cfg.service.guestbook_entries_returned
        max_bytes = self._transport.max_text_bytes
        if argument.startswith("#"):
            entry_id = int(argument[1:])
            # `_GUESTBOOK_ENTRY_ARG_RE` (commands.py) matches "#" followed by
            # ANY number of digits, so this can be arbitrarily large -- past
            # SQLite's 64-bit INTEGER range, no real entry_id can match, and
            # attempting the query would raise OverflowError. Treated the
            # same as any other nonexistent id instead.
            entry = self._guestbook.get_visible(entry_id) if entry_id <= _SQLITE_MAX_INT else None
            body = (
                format_guestbook_entry(entry, max_bytes, self.cfg.service.timezone)
                if entry is not None
                else format_guestbook_not_found(entry_id)
            )
        elif argument:
            page = int(argument)
            entries = self._guestbook.page_visible(page, per_page)
            if entries:
                total_visible = self._guestbook.visible_count()
                body = format_guestbook(
                    entries,
                    max_bytes,
                    page=page,
                    total_visible=total_visible,
                    has_more=total_visible > page * per_page,
                )
            else:
                body = format_guestbook_no_more_entries(self._guestbook.visible_count())
        else:
            total_visible = self._guestbook.visible_count()
            body = format_guestbook(
                self._guestbook.latest_visible(per_page),
                max_bytes,
                page=1,
                total_visible=total_visible,
                has_more=total_visible > per_page,
            )
        body = truncate_utf8(body, max_bytes)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.GUESTBOOK.value, outcome).inc()

    def _handle_wordle(
        self, sender_node_id: str, raw_argument: str, obs: IncomingPacket, now: int
    ) -> None:
        """WORDLE / WORDLE <guess>: same single-reply shape as BOOK, but
        `wordle.play` also writes per-player guess state (guesses used,
        solved) on this DB worker connection. A play whose reply cannot be
        queued (rate limited / queue full) must never consume a guess, so
        queue availability is checked BEFORE calling `wordle.play` --
        exactly the way `_handle_sign` checks `self._queue.reserve()`
        before `self._guestbook.insert()`. `raw_argument` (the guess) is
        never stored or logged: `wordle.play` only ever persists a bounded
        guess count/solved flag under a one-way hashed player_key.
        """
        refusal = self._queue.reserve(sender_node_id)
        if refusal is not None:
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "dropped", now, reason=refusal.value
            )
            self._metrics.commands_total.labels(Command.WORDLE.value, "queue_full").inc()
            self._note_drop(now)
            return

        day = local_day(now, self.cfg.service.timezone)
        body = wordle_play(
            self._db_conn,
            sender_node_id=self._bbs_id(sender_node_id),
            text_argument=raw_argument,
            day=day,
            salt=self.cfg.service.fortune_salt,
            now=now,
        )
        body = truncate_utf8(body, self._transport.max_text_bytes)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.WORDLE.value, outcome).inc()

    def _handle_seen(
        self, sender_node_id: str, raw_argument: str, obs: IncomingPacket, now: int,
        mesh: str | None = None,
    ) -> None:
        """SEEN <node>: resolves the argument (full id / 4-hex tail / exact
        short name -- `Collector.resolve_node`) and reports when/how we
        last heard it in the last 7 days. `[service] seen_enabled = false`
        (ruling 15) is handled earlier, in `_process_obs` (rewritten to
        `Command.UNKNOWN` before any eligibility/rate-limit/dedup/quota
        check runs, so a disabled SEEN leaves no more of a footprint than
        an unrecognized command) -- this handler only ever runs when SEEN
        is enabled. A bare `SEEN` with no argument has nothing to resolve
        and is silent. Never shows distance or position (see the Global
        Constraints/Privacy & consent rulings) -- only relative time, hop
        count, SNR when direct, first-seen date, and a 7-day seen count,
        all already voluntarily broadcast by the target node itself."""
        if not raw_argument:
            exchange_log.log_rejection(
                sender_node_id, "missing_node_argument", command=Command.SEEN.value,
                now=now, packet_id=obs.packet_id,
            )
            self._metrics.commands_total.labels(Command.SEEN.value, "rejected").inc()
            return
        if mesh is None:
            body = self._seen_body(self._collector, self._db_conn, raw_argument, now)
        else:
            label = _MESH_LABELS[mesh]
            suffix = f" ({label})"
            try:
                with self._peer_observatory() as (collector, conn):
                    body = self._seen_body(
                        collector, conn, raw_argument, now, suffix=suffix
                    )
            except sqlite3.Error:
                body = f"{label} data unavailable right now"
        body = truncate_utf8(body, self._transport.max_text_bytes)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.SEEN.value, outcome).inc()

    def _seen_body(
        self, collector: Collector, conn: sqlite3.Connection, raw_argument: str, now: int,
        suffix: str = "",
    ) -> str:
        """SEEN's reply text from `collector`'s observatory; `conn` is the
        same database, so a node's name comes from the mesh it was heard on.
        `suffix` (the peer mesh label) is appended after the formatters ran
        within a budget that leaves it room."""
        max_bytes = self._transport.max_text_bytes - len(suffix.encode("utf-8"))
        earliest = collector.first_packet_at()
        listening_seconds = now - earliest if earliest is not None else None
        resolution = collector.resolve_node(
            raw_argument, parse_node_argument=self._transport.parse_node_argument
        )
        if resolution.ambiguous:
            body = seen_ambiguous_text()
        elif resolution.node_id is None:
            body = seen_not_heard_text(
                _node_label_for(raw_argument, self._transport), listening_seconds=listening_seconds
            )
        else:
            node_id = resolution.node_id
            today = datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d")
            days_seen = collector.days_seen_last_n(node_id, today, 7)
            last_obs = collector.last_observation(node_id)
            if days_seen == 0 or last_obs is None:
                # Resolved but not observed in the last 7 days: the exact
                # same reply as "never observed" -- deliberately
                # indistinguishable (ruling 1/privacy). The label is
                # derived from the ARGUMENT's shape (_node_label_for), not
                # from the resolved node id: a short-name lookup must say
                # "that node" here exactly as it does when unresolved,
                # while an id/tail argument still shows its own tail (the
                # two cases produce the same label either way, since an
                # id/tail argument's tail is the resolved node's own tail).
                # last_obs can be None even when days_seen > 0 if
                # retention.observations_days is shorter than this 7-day
                # window -- the observation row was pruned but node_daily
                # rows survive longer -- so this falls back to the same
                # not-heard reply rather than crashing on last_obs[...].
                body = seen_not_heard_text(
                    _node_label_for(raw_argument, self._transport),
                    listening_seconds=listening_seconds,
                )
            else:
                delta = now - last_obs["server_time"]
                hops_away = last_obs["hops_away"]
                if hops_away is None:
                    how, snr = "unknown", None
                elif hops_away == 0:
                    how, snr = "direct", last_obs["rx_snr"]
                else:
                    how, snr = f"via {hops_away} hops", None
                first_seen = collector.first_seen_date(node_id, self.cfg.service.timezone)
                body = seen_text(
                    _seen_label(conn, node_id, self._transport),
                    relative_ago=_relative_ago(delta),
                    how=how,
                    snr=snr,
                    first_seen_date=first_seen or "unknown",
                    days_seen=days_seen,
                    heard_24h=collector.sender_observations_since(node_id, now - 86400),
                    max_bytes=max_bytes,
                )
        return body + suffix

    def _handle_ping(self, sender_node_id: str, obs: IncomingPacket, now: int) -> None:
        """PING: same single-reply dispatch as `_handle_simple_command`,
        but the reply body depends on the REQUEST'S OWN packet fields
        (snr/hops/pki), same reason ME has its own handler."""
        body = ping_text(
            snr=obs.rx_snr,
            hop_start=obs.hop_start,
            hop_limit=obs.hop_limit,
            pki_encrypted=obs.pki_encrypted,
        )
        body = truncate_utf8(body, self._transport.max_text_bytes)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.PING.value, outcome).inc()

    def _handle_position(
        self, parsed: ParsedCommand, sender_node_id: str, obs: IncomingPacket, now: int
    ) -> None:
        """A MeshCore app location share sent by DM: record the parsed
        coordinates (never the text) and acknowledge once. Only reachable
        when `[observatory] dm_position_capture` is on (see
        `_parse_command_for_dispatch`) and only from the DM path (POSITION
        is not in `_CHANNEL_PUBLIC_COMMANDS`, so a channel sender's
        forgeable name can never record a position)."""
        parsed_pos = parse_position_text(parsed.argument)
        if parsed_pos is None:  # parse_command only yields POSITION for a valid share
            return
        kind, lat, lon = parsed_pos
        positions.record_dm_position(self._db_conn, sender_node_id, kind, lat, lon, now)
        body = truncate_utf8(position_ack_text(kind, lat, lon), self._transport.max_text_bytes)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.POSITION.value, outcome).inc()

    def _weather_status_line(self, now: int, lead: str = "wx-bay") -> str:
        return wx_status_text(
            enabled=self._weather_enabled,
            counties=len(self.cfg.weather.counties),
            last_poll_ok_at=self._weather_poller.last_success_at if self._weather_poller else None,
            active=self._weather_ledger.active_count(now) if self._weather_ledger else 0,
            last_sent_at=self._weather_ledger.last_sent_at() if self._weather_ledger else None,
            now=now,
            timezone=self.cfg.service.timezone,
            lead=lead,
            max_bytes=self._transport.max_text_bytes,
        )

    def _handle_wx(self, sender_node_id: str, obs: IncomingPacket, now: int) -> None:
        """WX: status of the weather feed. Same single-reply shape as PING."""
        body = self._weather_status_line(now)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.WX.value, outcome).inc()

    def _handle_me(self, sender_node_id: str, obs: IncomingPacket, now: int) -> None:
        """ME: the requester's own directory/status summary -- WHOAMI/SIGNAL
        are aliases of ME (operator decision), so this is also their reply.
        Every field is either derived from the request's own IncomingPacket
        or scoped to the requester's own node id -- never another node's
        data."""
        display_name = self._report_display_name(sender_node_id)
        today = datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d")
        days_seen = self._collector.days_seen_last_n(sender_node_id, today, 7)
        first_seen = self._collector.first_seen_date(sender_node_id, self.cfg.service.timezone)
        heard_24h = self._collector.sender_observations_since(sender_node_id, now - 86400)
        day = local_day(now, self.cfg.service.timezone)
        used, _ = self._quota.usage(sender_node_id, day)
        signed_count = self._db_conn.execute(
            "SELECT COUNT(*) FROM guestbook WHERE sender_node_id=? AND visible=1 "
            "AND topic IS NULL",
            (self._bbs_id(sender_node_id),),
        ).fetchone()[0]
        mail_enabled = self._mail_prefs.get(self._bbs_id(sender_node_id))
        mail_pending = self._mail.pending_count(self._bbs_id(sender_node_id))
        body = me_text(
            self._transport.display_label(sender_node_id),
            display_name=display_name,
            first_seen_date=first_seen or "unknown",
            days_seen=days_seen,
            heard_24h=heard_24h,
            snr=obs.rx_snr,
            rssi=obs.rx_rssi,
            hop_start=obs.hop_start,
            hop_limit=obs.hop_limit,
            pki_encrypted=obs.pki_encrypted,
            signed_count=signed_count,
            quota_used=used,
            quota_limit=self.cfg.service.daily_command_quota,
            mail_enabled=mail_enabled,
            mail_pending=mail_pending,
            max_bytes=self._transport.max_text_bytes,
        )
        body = truncate_utf8(body, self._transport.max_text_bytes)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.ME.value, outcome).inc()

    def _handle_unsign(self, sender_node_id: str, obs: IncomingPacket, now: int) -> None:
        """UNSIGN: hides the sender's own newest visible guestbook entry
        (`Guestbook.hide_own_latest`) -- entries stay stored, only their
        visibility flips, exactly like the `guestbook hide` CLI action.
        Same queue-reserve-before-mutate ordering as WORDLE: a reply that
        cannot be queued must not hide an entry with no confirmation ever
        delivered.

        Also resets the sender's SIGN cooldown (`RateLimiter.reset_sign`):
        UNSIGN frees the slot it just hid, so a visitor who takes their
        entry down can immediately SIGN again instead of waiting out a
        cooldown started by the entry they just removed."""
        refusal = self._queue.reserve(sender_node_id)
        if refusal is not None:
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "dropped", now, reason=refusal.value
            )
            self._metrics.commands_total.labels(Command.UNSIGN.value, "queue_full").inc()
            self._note_drop(now)
            return

        entry_id = self._guestbook.hide_own_latest(self._bbs_id(sender_node_id), now)
        if entry_id is not None:
            self._limiter.reset_sign(sender_node_id)
        body = (
            format_unsign_hidden(entry_id) if entry_id is not None else format_unsign_nothing()
        )
        body = truncate_utf8(body, self._transport.max_text_bytes)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.UNSIGN.value, outcome).inc()

    def _reserve_and_send(
        self,
        sender_node_id: str,
        obs: IncomingPacket,
        now: int,
        body: str,
        *,
        commit_rate_limit: bool = True,
        on_pretransmission_failure: Callable[[str], None] | None = None,
        reply_queue_key: str | None = None,
        is_knock: bool = False,
        is_cooldown_notice: bool = False,
    ) -> bool:
        """Reserves a reply-queue slot and enqueues `body`, or marks the
        request dropped if no slot is available. Returns whether a reply
        was enqueued. Callers own their own outcome metrics.

        `commit_rate_limit=False` (fix round 1, item 1): skips committing
        the generic per-node command `RateLimiter` cooldown. Every ordinary
        command reply leaves this `True` (unchanged behavior); the channel
        knock passes `False`, because committing the shared limiter for an
        unauthenticated channel trigger would let a forged knock silently
        deny the IMPERSONATED node's own next genuine command at
        `_claim_and_gate`'s `check_command` gate -- a service-denial side
        effect on an innocent third party the knock's design must not have.
        The knock's own persistent, per-recipient cooldown already rate-
        limits it; this shared limiter was never the mechanism doing that.

        `on_pretransmission_failure` (fix round 1, item 2): forwarded to
        `ReplyJob.release_on_pretransmission_failure` (see its own
        docstring), and ALSO invoked directly, right here, for this
        method's own immediate queue refusal (`QUEUE_FULL`, or
        `RATE_LIMITED` meaning "this sender already has one reply
        pending") -- the one failure mode that happens synchronously,
        before any `ReplyJob` exists for `Sender` to later settle.

        `reply_queue_key`/`is_knock` (task 3C, findings 1 and 2): forwarded
        to `ReplyQueue`/`ReplyJob` verbatim (see their own docstrings).
        Every ordinary command reply leaves both at their defaults (`None`,
        `False`), so `ReplyQueue.reserve`/`offer` key on `sender_node_id`
        itself, unchanged. The channel knock passes a namespaced key that
        can never collide with a real node id, so its pending reply and a
        real command's pending reply to the SAME node are never the same
        slot, and marks `is_knock=True` so a burst of knocks (each to a
        DIFFERENT attacker-minted contact, and so each with a different
        key) is still bounded as a group.
        """
        queue_key = reply_queue_key if reply_queue_key is not None else sender_node_id
        refusal = self._queue.reserve(queue_key, is_knock=is_knock)
        if refusal is not None:
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "dropped", now, reason=refusal.value
            )
            self._note_drop(now)
            if on_pretransmission_failure is not None:
                on_pretransmission_failure(refusal.value)
            return False

        self._db_request_log.set_state(sender_node_id, obs.packet_id, "queued", now)
        job = ReplyJob(
            request_sender=sender_node_id,
            request_packet_id=obs.packet_id,
            request_received_at=obs.received_at,
            deadline_monotonic=obs.received_monotonic + self.cfg.service.reply_ttl_seconds,
            body=body,
            release_on_pretransmission_failure=on_pretransmission_failure,
            reply_queue_key=reply_queue_key,
            is_knock=is_knock,
            is_cooldown_notice=is_cooldown_notice,
            reply_channel_idx=obs.reply_channel_idx,
        )
        self._sender.enqueue(job)
        if commit_rate_limit:
            self._limiter.commit_command(sender_node_id)
        return True

    def _peer_mesh(self, suffix_mesh: str | None) -> str | None:
        """The peer mesh a command suffix names, or None for this daemon's
        own mesh (no suffix, or a suffix naming the mesh it already serves)."""
        if suffix_mesh is None or suffix_mesh == self.cfg.radio.transport:
            return None
        return suffix_mesh

    @contextlib.contextmanager
    def _peer_observatory(self):
        """Yields (Collector, connection) over the peer's observatory DB,
        opened read-only and closed on exit. Opened per request on the
        calling (DB worker) thread, never kept: the peer's file may appear
        or disappear while this daemon runs. Raises sqlite3.Error when no
        peer is configured or its file cannot be opened/read."""
        path = self.cfg.database.peer_path
        if not path:
            raise sqlite3.OperationalError("no peer observatory configured")
        # No bbs_path: the peer contributes observatory data only; BBS
        # tables stay on this daemon's own shared file.
        conn = connect(path, read_only=True)
        try:
            yield Collector(conn), conn
        finally:
            conn.close()

    def _peer_response_body(self, command: Command, mesh: str, now: int) -> str:
        """WHO/GARDEN/RECORD for the peer mesh's observatory, labelled with
        the mesh so it cannot be mistaken for this mesh's own numbers."""
        label = _MESH_LABELS[mesh]
        max_bytes = self._transport.max_text_bytes
        suffix = f" ({label})"
        # Reserve the label's room up front so the formatters' own
        # truncation never cuts it off.
        budget = max_bytes - len(suffix.encode("utf-8"))
        try:
            with self._peer_observatory() as (collector, conn):
                if command == Command.WHO:
                    body = who_text(
                        heard_1h=collector.distinct_senders_since(now - 3600),
                        direct_1h=collector.distinct_direct_senders_since(now - 3600),
                        heard_24h=collector.distinct_senders_since(now - 86400),
                    )
                elif command == Command.GARDEN:
                    body = self._garden_body(collector, now, budget)
                elif command == Command.RECORD:
                    body = self._record_body(
                        collector, conn, now, budget, companion_events=mesh == "meshcore"
                    )
                else:
                    raise AssertionError(f"command has no peer form: {command!r}")
        except sqlite3.Error:
            return truncate_utf8(f"{label} data unavailable right now", max_bytes)
        return truncate_utf8(body, budget) + suffix

    def _distance_limits(self) -> dict:
        obs_cfg = self.cfg.observatory
        return {
            "observatory_lat": obs_cfg.latitude,
            "observatory_lon": obs_cfg.longitude,
            "max_direct_km": obs_cfg.max_direct_km,
            "max_km_per_hop": obs_cfg.max_km_per_hop,
            "max_observed_km": obs_cfg.max_observed_km,
        }

    def _garden_body(self, collector: Collector, now: int, max_bytes: int) -> str:
        """GARDEN text from `collector`'s observatory (own or peer)."""
        midnight = local_midnight(now, self.cfg.service.timezone)
        return garden_text(
            self.cfg.service.name,
            nodes_today=collector.distinct_senders_since(midnight),
            heard_direct=collector.distinct_direct_senders_since(midnight),
            best_snr=collector.best_snr_since(now - 86400),
            guestbook_entries=self._guestbook.visible_count(),
            furthest_direct_km=collector.furthest_direct_km_since(
                midnight, **self._distance_limits()
            ),
            max_bytes=max_bytes,
        )

    def _record_body(
        self, collector: Collector, conn: sqlite3.Connection, now: int, max_bytes: int,
        *, companion_events: bool,
    ) -> str:
        """RECORD text from `collector`'s observatory. Names come from
        `conn`, the same database: a peer's bare ids must not be named from
        this daemon's own `nodes`."""
        midnight = local_midnight(now, self.cfg.service.timezone)
        best = collector.best_snr_with_sender_since(midnight)
        best_snr, best_snr_node_id = best if best is not None else (None, None)
        hops = collector.max_hops_with_sender_since(midnight)
        max_hops, max_hops_node_id = hops if hops is not None else (None, None)
        far = collector.furthest_observed_km_with_node_since(
            midnight, **self._distance_limits()
        )
        far_km, far_node_id = far if far is not None else (None, None)
        return record_text(
            best_snr=best_snr,
            best_snr_node_id=best_snr_node_id,
            best_snr_node_name=lookup_node_name(conn, best_snr_node_id),
            max_hops=max_hops,
            max_hops_node_id=max_hops_node_id,
            max_hops_node_name=lookup_node_name(conn, max_hops_node_id),
            busiest_hour=collector.busiest_hour_since(midnight, self.cfg.service.timezone),
            total_packets=collector.raw_events_since(midnight),
            furthest_observed_km=far_km,
            furthest_observed_node_id=far_node_id,
            furthest_observed_node_name=lookup_node_name(conn, far_node_id),
            companion_events=companion_events,
            max_bytes=max_bytes,
        )

    def _peer_transport_name(self) -> str:
        return "meshtastic" if self.cfg.radio.transport == "meshcore" else "meshcore"

    def _bbs_id(self, node_id: str) -> str:
        """The form of a sender/recipient id stored in the BBS tables.
        Only a shared bbs.db holds both meshes' rows, so a single-file
        install keeps bare ids (and needs no migration of its rows)."""
        if self.cfg.database.resolved_bbs_path is None:
            return node_id
        return bbs_ids.qualify(node_id, self.cfg.radio.transport)

    def _bbs_local(self, stored: str) -> str | None:
        """This mesh's bare id for a stored BBS id, or None when it belongs
        to the other mesh (whose daemon delivers to, protects and names it)."""
        if self.cfg.database.resolved_bbs_path is None:
            return stored
        return bbs_ids.local_or_none(stored, self.cfg.radio.transport)

    def _bbs_argument(self, stored: str) -> str:
        """A stored BBS id as a MAIL recipient argument naming its mesh."""
        if self.cfg.database.resolved_bbs_path is None:
            return stored
        return bbs_ids.argument_form(stored)

    def _bbs_node_label(self, stored: str) -> str:
        """Reply label for a stored BBS id. A peer-mesh id gets no name from
        this mesh's node table: the same bare id there is a different node,
        so its name would misattribute the message."""
        local = self._bbs_local(stored)
        if local is not None:
            return self._node_report_label(local)
        transport, bare = bbs_ids.split(stored)
        prefix = bbs_ids.MESHCORE_PREFIX if transport == "meshcore" else bbs_ids.MESHTASTIC_PREFIX
        return prefix + self._transport.short_label(bare)

    def _lookup_display_name(self, sender_node_id: str) -> str | None:
        """Known node name, sanitized for display; never an identity lookup."""
        return lookup_node_name(self._db_conn, sender_node_id)

    def _report_display_name(self, node_id: str | None) -> str | None:
        """Full known name; each formatter budgets its own complete reply."""
        return lookup_node_name(self._db_conn, node_id)

    def _node_report_label(self, node_id: str) -> str:
        name = self._report_display_name(node_id)
        tail = self._transport.short_label(node_id)
        return f"{name} · {tail}" if name else tail

    def _handle_sign(
        self, sender_node_id: str, raw_argument: str, obs: IncomingPacket, now: int
    ) -> None:
        """Implements the SIGN dispatch order: storage pressure, validation,
        the per-sender sign cooldown, identical-text suppression, then a
        single transaction that inserts the guestbook entry and moves the
        request to 'queued' together. Every rejection (storage-full,
        SIGN_EMPTY/SIGN_TOO_LONG, cooldown, duplicate, daily quota) gets a
        short reason reply within the same traffic limits -- only queue_full
        stays silent, since no reply can be sent at all in that case. The
        metrics outcome label still reflects the reason (rejected/
        rate_limited/sign_quota), unaffected by whether the reply itself was
        queued or later dropped for being queue_full. Never logs the
        argument or the normalized text."""
        if self._retention.storage_report().publication_paused:
            self._metrics.commands_total.labels(Command.SIGN.value, "storage_full").inc()
            exchange_log.log_rejection(
                sender_node_id, "storage_full", command=Command.SIGN.value, now=now
            )
            self._reserve_and_send(sender_node_id, obs, now, format_storage_full())
            return

        max_bytes = self._transport.max_text_bytes
        try:
            normalized = self._guestbook.validate(raw_argument, inbound_payload_len=obs.payload_len)
        except SignError as exc:
            self._metrics.commands_total.labels(Command.SIGN.value, "rejected").inc()
            exchange_log.log_rejection(
                sender_node_id, exc.reason.value, command=Command.SIGN.value, now=now
            )
            max_chars = self.cfg.service.max_guestbook_chars
            body = (
                format_sign_empty(max_chars)
                if exc.reason is Reject.SIGN_EMPTY
                else format_sign_too_long(max_chars)
            )
            self._reserve_and_send(sender_node_id, obs, now, truncate_utf8(body, max_bytes))
            return

        if self._limiter.check_sign(sender_node_id) is not None:
            exchange_log.log_rejection(
                sender_node_id, "sign_cooldown", command=Command.SIGN.value, now=now,
                cooldown_seconds=self.cfg.service.sign_cooldown_seconds,
            )
            self._metrics.commands_total.labels(Command.SIGN.value, "rate_limited").inc()
            self._note_drop(now)
            remaining = self._limiter.sign_cooldown_remaining(sender_node_id)
            body = format_sign_cooldown(remaining)
            self._reserve_and_send(sender_node_id, obs, now, truncate_utf8(body, max_bytes))
            return

        if self._guestbook.is_duplicate_sign(
            self._bbs_id(sender_node_id),
            normalized,
            now,
            window_hours=self.cfg.service.identical_sign_dedup_hours,
        ):
            self._metrics.commands_total.labels(Command.SIGN.value, "rejected").inc()
            body = format_sign_duplicate(self.cfg.service.identical_sign_dedup_hours)
            exchange_log.log_rejection(
                sender_node_id, "sign_duplicate", command=Command.SIGN.value, now=now
            )
            self._reserve_and_send(sender_node_id, obs, now, truncate_utf8(body, max_bytes))
            return

        refusal = self._queue.reserve(sender_node_id)
        if refusal is not None:
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "dropped", now, reason=refusal.value
            )
            self._metrics.commands_total.labels(Command.SIGN.value, "queue_full").inc()
            self._note_drop(now)
            return

        sign_quota_day = local_day(now, self.cfg.service.timezone)
        if not self._quota.check_and_count_sign(sender_node_id, sign_quota_day, now):
            exchange_log.log_rejection(
                sender_node_id, "sign_quota", command=Command.SIGN.value, now=now
            )
            self._metrics.commands_total.labels(Command.SIGN.value, "sign_quota").inc()
            self._note_drop(now)
            body = format_sign_quota()
            self._reserve_and_send(sender_node_id, obs, now, truncate_utf8(body, max_bytes))
            return

        # Banned-word guard: a match is still stored (never silently
        # discarded) but inserted hidden, with a bounded moderation_reason
        # -- the visitor still gets the normal ack (see below), so
        # moderation is silent to them; only the operator (mgarden
        # guestbook list --hidden) and the bounded moderated-entries metric
        # ever see it.
        moderation_reason: str | None = None
        if self._banned_terms:
            match = moderation.find_banned_term(
                moderation.normalize_for_match(normalized), self._banned_terms
            )
            if match is not None:
                moderation_reason = moderation.BANNED_WORD_REASON

        display_name = self._lookup_display_name(sender_node_id)
        with self._db_conn:
            entry_id = self._guestbook.insert(
                self._bbs_id(sender_node_id),
                display_name,
                normalized,
                now,
                visible=(moderation_reason is None),
                moderation_reason=moderation_reason,
            )
            # commit=False: this outer `with self._db_conn:` is the SOLE
            # transaction owner. set_state's own `with self._conn:` would
            # otherwise commit (or roll back) the insert above early, on
            # this statement alone, making the outer block's exit a no-op.
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "queued", now, entry_id=entry_id, commit=False
            )

        self._limiter.commit_sign(sender_node_id)
        self._metrics.guestbook_entries_created.inc()
        if moderation_reason is not None:
            self._metrics.guestbook_entries_moderated.inc()

        job = ReplyJob(
            request_sender=sender_node_id,
            request_packet_id=obs.packet_id,
            request_received_at=obs.received_at,
            deadline_monotonic=obs.received_monotonic + self.cfg.service.reply_ttl_seconds,
            body=format_signed_ack(entry_id),
            reply_channel_idx=obs.reply_channel_idx,
        )
        self._sender.enqueue(job)
        self._limiter.commit_command(sender_node_id)
        self._metrics.commands_total.labels(Command.SIGN.value, "accepted").inc()

    def _handle_post(
        self, sender_node_id: str, raw_argument: str, obs: IncomingPacket, now: int
    ) -> None:
        """Mirrors _handle_sign's dispatch order: storage pressure, validate
        topic+message, duplicate suppression scoped to the topic, queue
        reserve, per-node daily_post_quota (its own counter, separate from
        daily_sign_quota), moderation, then insert. No cooldown beyond the
        shared per-command cooldown already enforced in _process_obs (POST
        has no SIGN-style extra per-command cooldown -- the spec only asked
        for a daily quota). Boards share the guestbook table with SIGN
        (ruling 5), so the same storage-pressure guard applies here too."""
        if self._retention.storage_report().publication_paused:
            self._metrics.commands_total.labels(Command.POST.value, "storage_full").inc()
            exchange_log.log_rejection(
                sender_node_id, "storage_full", command=Command.POST.value, now=now
            )
            self._reserve_and_send(sender_node_id, obs, now, format_storage_full())
            return

        parts = raw_argument.split(None, 1)
        if len(parts) < 2:
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "dropped", now, reason=Reject.POST_EMPTY.value
            )
            self._metrics.commands_total.labels(Command.POST.value, "rejected").inc()
            return
        topic_raw, message_raw = parts

        existing_topics = self._guestbook.distinct_topics_in_use()
        topic_or_reject = validate_topic(
            topic_raw,
            max_topics_in_use=len(existing_topics),
            max_topics=self.cfg.service.max_topics,
            existing_topics=existing_topics,
        )
        if isinstance(topic_or_reject, Reject):
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "dropped", now, reason=topic_or_reject.value
            )
            self._metrics.commands_total.labels(Command.POST.value, "rejected").inc()
            return
        topic = topic_or_reject

        try:
            normalized = self._guestbook.validate(message_raw, inbound_payload_len=obs.payload_len)
        except SignError as exc:
            reason = _POST_SIGN_ERROR_REASON.get(exc.reason, exc.reason)
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "dropped", now, reason=reason.value
            )
            self._metrics.commands_total.labels(Command.POST.value, "rejected").inc()
            return

        if self._guestbook.is_duplicate_sign(
            self._bbs_id(sender_node_id),
            normalized,
            now,
            window_hours=self.cfg.service.identical_sign_dedup_hours,
            topic=topic,
        ):
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "dropped", now, reason=Reject.POST_DUPLICATE.value
            )
            self._metrics.commands_total.labels(Command.POST.value, "rejected").inc()
            return

        refusal = self._queue.reserve(sender_node_id)
        if refusal is not None:
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "dropped", now, reason=refusal.value
            )
            self._metrics.commands_total.labels(Command.POST.value, "queue_full").inc()
            self._note_drop(now)
            return

        day = local_day(now, self.cfg.service.timezone)
        if not self._quota.check_and_count_post(sender_node_id, day, now):
            self._metrics.commands_total.labels(Command.POST.value, "post_quota").inc()
            self._note_drop(now)
            if not self._maybe_send_quota_notice(sender_node_id, obs, now, day, "post"):
                self._db_request_log.set_state(
                    sender_node_id, obs.packet_id, "dropped", now,
                    reason=Reject.POST_QUOTA.value,
                )
            return

        moderation_reason: str | None = None
        if self._banned_terms:
            match = moderation.find_banned_term(
                moderation.normalize_for_match(normalized), self._banned_terms
            )
            if match is not None:
                moderation_reason = moderation.BANNED_WORD_REASON

        display_name = self._lookup_display_name(sender_node_id)
        with self._db_conn:
            entry_id = self._guestbook.insert(
                self._bbs_id(sender_node_id),
                display_name,
                normalized,
                now,
                visible=(moderation_reason is None),
                moderation_reason=moderation_reason,
                topic=topic,
            )
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "queued", now, entry_id=entry_id, commit=False
            )
        self._metrics.guestbook_entries_created.inc()
        if moderation_reason is not None:
            self._metrics.guestbook_entries_moderated.inc()

        job = ReplyJob(
            request_sender=sender_node_id,
            request_packet_id=obs.packet_id,
            request_received_at=obs.received_at,
            deadline_monotonic=obs.received_monotonic + self.cfg.service.reply_ttl_seconds,
            body=f"📌 #{topic} · posted",
            reply_channel_idx=obs.reply_channel_idx,
        )
        self._sender.enqueue(job)
        self._limiter.commit_command(sender_node_id)
        self._metrics.commands_total.labels(Command.POST.value, "accepted").inc()

    def _handle_read(
        self, sender_node_id: str, raw_argument: str, obs: IncomingPacket, now: int
    ) -> None:
        """READ <topic> [page]: same single-reply dispatch as
        `_handle_simple_command`, but the topic must be resolved/validated
        first (ruling 3) -- a shape-invalid topic, or a topic nobody has
        ever posted to, gets no reply at all (same silent-refusal
        philosophy as an unrecognized command)."""
        parts = raw_argument.split(None, 1)
        if not parts:
            # READ with no topic has nothing to answer -- silent, no reply.
            exchange_log.log_rejection(
                sender_node_id, "missing_topic", command=Command.READ.value,
                now=now, packet_id=obs.packet_id,
            )
            self._metrics.commands_total.labels(Command.READ.value, "rejected").inc()
            return
        topic_raw = parts[0]
        page_raw = parts[1].strip() if len(parts) > 1 else ""
        existing_topics = self._guestbook.distinct_topics_in_use()
        topic_or_reject = validate_topic(
            topic_raw,
            max_topics_in_use=len(existing_topics),
            max_topics=self.cfg.service.max_topics,
            existing_topics=existing_topics,
        )
        if isinstance(topic_or_reject, Reject) or topic_or_reject not in existing_topics:
            exchange_log.log_rejection(
                sender_node_id, "invalid_or_unknown_topic", command=Command.READ.value,
                now=now, packet_id=obs.packet_id,
            )
            # An invalid shape, or a topic nobody has ever posted to: no
            # reply -- indistinguishable from READ of an unrecognized word,
            # same silent-refusal philosophy as GUESTBOOK's own argument
            # grammar.
            self._metrics.commands_total.labels(Command.READ.value, "rejected").inc()
            return
        topic = topic_or_reject
        per_page = self.cfg.service.guestbook_entries_returned
        max_bytes = self._transport.max_text_bytes
        # isascii() guard: some non-ASCII digits (e.g. "²") satisfy
        # isdigit() but raise ValueError from int().
        page = (
            int(page_raw)
            if page_raw.isascii() and page_raw.isdigit() and 1 <= int(page_raw) <= 999
            else 1
        )
        total_visible = self._guestbook.visible_count(topic=topic)
        entries = self._guestbook.page_visible(page, per_page, topic=topic)
        if entries:
            body = format_board(
                entries,
                max_bytes,
                topic=topic,
                page=page,
                total_visible=total_visible,
                has_more=total_visible > page * per_page,
            )
        elif page <= 1 and total_visible == 0:
            # A topic can exist (something was posted, per
            # distinct_topics_in_use above) yet have nothing visible right
            # now (every post hidden/moderated) -- page 1 gets the honest
            # "is empty" reply via format_board, not "No more entries",
            # which would wrongly imply there was ever something to page
            # through.
            body = format_board([], max_bytes, topic=topic, page=page, total_visible=total_visible)
        else:
            body = format_board_no_more_entries(topic, total_visible)
        body = truncate_utf8(body, max_bytes)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.READ.value, outcome).inc()

    def _handle_mail(
        self, sender_node_id: str, raw_argument: str, obs: IncomingPacket, now: int
    ) -> None:
        """MAIL / ON|OFF / ALIAS / send: sub-forms sharing
        one dispatch -- this is the one place that tells them apart, since
        none has a fixed shape `parse_command` could validate eagerly (same
        reason POST/READ split their own argument in main.py, not
        commands.py). ON/OFF is matched case-insensitively; anything else
        non-empty is a send."""
        arg = raw_argument.strip()
        if not arg:
            self._handle_mail_list(sender_node_id, obs, now)
            return
        if arg.upper() in ("ON", "OFF"):
            self._handle_mail_pref(sender_node_id, arg.upper() == "ON", obs, now)
            return
        alias_parts = arg.split(None, 1)
        if alias_parts[0].upper() == "ALIAS":
            self._handle_mail_alias(
                sender_node_id,
                alias_parts[1] if len(alias_parts) == 2 else "",
                obs,
                now,
            )
            return
        self._handle_mail_send(sender_node_id, arg, obs, now)

    def _handle_mail_list(self, sender_node_id: str, obs: IncomingPacket, now: int) -> None:
        """Read pending notes, consuming only the text covered by an eventual ACK.

        Long notes are delivered over successive explicit MAIL requests. Each
        receipt includes the original body, so overlapping reads are idempotent.
        """
        pending = self._mail.pending_for(self._bbs_id(sender_node_id))
        labels = {
            node_id: self._bbs_node_label(node_id)
            for node_id in {e.sender_node_id for e in pending}
        }
        max_bytes = self._transport.max_text_bytes
        body, receipts = mail_page(pending, now=now, max_bytes=max_bytes, labels=labels,
                                   expiry_days=self.cfg.retention.mail_expiry_days)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        if sent and receipts:
            self._mail_delivery_pending[(sender_node_id, obs.packet_id)] = (now, receipts)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.MAIL.value, outcome).inc()

    def _resolve_pending_mail_deliveries(self, now: int) -> None:
        """Settles every MAIL-list reply still awaiting its own delivery
        outcome (see `_handle_mail_list`): a genuine ack means the
        requester actually received the list, so those entry ids are now
        safe to hard-delete; any other terminal outcome (failed/dropped/
        expired/unknown) means it never arrived, so the mapping is simply
        dropped -- the notes were never deleted and stay pending for the
        next MAIL read. An entry still in-flight (queued/sending/submitted)
        is left alone.

        The state is consulted BEFORE any age bound: an ack may legitimately
        land any time inside ack_timeout_seconds (300 s by default), which
        is far longer than reply_ttl_seconds (60 s) -- the TTL only bounds
        how long a request may wait in the queue before it is first sent.
        Dropping the mapping on the shorter TTL first (an earlier version of
        this method) lost every ack that arrived after it, leaving delivered
        notes pending and re-deliverable. The age safety net is therefore
        the full ack window plus the reply TTL (the latest an ack could
        possibly still be attributed), so the dict stays bounded even if a
        state transition is somehow missed."""
        updates.settle(self._db_conn, now,
                       self.cfg.service.ack_timeout_seconds + self.cfg.service.reply_ttl_seconds,
                       local_id=self._bbs_local)
        if not self._mail_delivery_pending:
            return
        max_age = self.cfg.service.ack_timeout_seconds + self.cfg.service.reply_ttl_seconds
        for key, (queued_at, entry_ids) in list(self._mail_delivery_pending.items()):
            sender_node_id, packet_id = key
            state = self._db_request_log.get_state(sender_node_id, packet_id)
            if state == "acked":
                collected = self._mail.consume_delivered(
                    entry_ids, expiry_days=self.cfg.retention.mail_expiry_days)
                usefulness.counter(self._db_conn, "MAIL_COLLECTED", collected, now)
                del self._mail_delivery_pending[key]
            elif state in ("failed", "dropped", "expired", "unknown") or now - queued_at > max_age:
                del self._mail_delivery_pending[key]

    def _handle_mail_pref(
        self, sender_node_id: str, enabled: bool, obs: IncomingPacket, now: int
    ) -> None:
        """MAIL ON/OFF: per-node opt-in toggle, default off (ruling 10)."""
        self._mail_prefs.set(self._bbs_id(sender_node_id), enabled, now)
        body = truncate_utf8(mail_pref_ack_text(enabled), self._transport.max_text_bytes)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.MAIL.value, outcome).inc()

    def _handle_mail_alias(
        self, sender_node_id: str, argument: str, obs: IncomingPacket, now: int
    ) -> None:
        """MAIL ALIAS [<name>|OFF]: authenticated, local and non-enumerable."""
        value = argument.strip()
        alias: str | None = None
        if not value:
            alias = self._mail_aliases.get(self._bbs_id(sender_node_id))
            status = "unchanged" if alias is not None else "none"
        elif value.upper() == "OFF":
            status = (
                "removed" if self._mail_aliases.release(self._bbs_id(sender_node_id), now)
                else "none"
            )
        else:
            status = self._mail_aliases.claim(self._bbs_id(sender_node_id), value, now)
            alias = normalize_mail_alias(value, allow_at=False)
        body = truncate_utf8(
            mail_alias_text(status, alias), self._transport.max_text_bytes
        )
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        success = status in ("claimed", "unchanged", "removed", "none")
        outcome = "accepted" if sent and success else "rejected" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.MAIL.value, outcome).inc()

    def _resolve_on_peer(self, node_raw: str) -> str | None:
        """Peer-mesh bare id for a MAIL recipient this mesh does not know;
        None when unknown, ambiguous there, or the peer is unavailable."""
        try:
            with self._peer_observatory() as (collector, _conn):
                resolution = collector.resolve_node(
                    node_raw, parse_node_argument=self._transport.parse_node_argument
                )
        except sqlite3.Error:
            return None
        return resolution.node_id

    def _handle_mail_send(
        self, sender_node_id: str, arg: str, obs: IncomingPacket, now: int
    ) -> None:
        """MAIL <node> <msg>: mirrors _handle_post's shared dispatch order
        (validate -> queue reserve -> quota -> moderation) -- including
        `_MAIL_SIGN_ERROR_REASON`'s remap of `Guestbook.validate`'s
        SignError (SIGN_EMPTY/SIGN_TOO_LONG) to MAIL_EMPTY/MAIL_TOO_LONG,
        same reason `_POST_SIGN_ERROR_REASON` exists: a MAIL failure must
        be audited under a MAIL code, never a SIGN one. The recipient's
        opt-in/pending-cap decision (ruling 10) is NEVER visible in the
        reply: every well-formed send gets the exact same neutral
        `mail_sent_ack_text()`, whether or not the note was actually stored.
        Stored-vs-refused only ever shows up in the two bounded
        `mail_notes_stored`/`mail_notes_refused` counters."""
        parts = arg.split(None, 1)
        if len(parts) < 2:
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "dropped", now, reason=Reject.MAIL_EMPTY.value
            )
            self._metrics.commands_total.labels(Command.MAIL.value, "rejected").inc()
            return
        node_raw, message_raw = parts
        recipient: str | None
        if node_raw.startswith("@"):
            alias = normalize_mail_alias(node_raw)
            if alias is None:
                self._db_request_log.set_state(
                    sender_node_id,
                    obs.packet_id,
                    "dropped",
                    now,
                    reason=Reject.MAIL_NODE_INVALID.value,
                )
                self._metrics.commands_total.labels(Command.MAIL.value, "rejected").inc()
                return
            # A syntactically valid but unknown alias follows the same neutral
            # acknowledgement path as an opted-out/full mailbox. This prevents
            # MAIL from becoming an alias-enumeration oracle.
            recipient = self._mail_aliases.resolve(alias)
        elif (
            self.cfg.database.resolved_bbs_path is not None
            and bbs_ids.has_transport_prefix(node_raw)
        ):
            # An explicit mesh prefix names the recipient exactly: no lookup,
            # since this mesh's node table cannot vouch for the other mesh.
            # Only with a shared bbs file: single-file installs parse MAIL
            # recipients exactly as before the prefixes existed.
            recipient = bbs_ids.typed_node_id(
                node_raw, self.cfg.radio.transport, shared=True,
            )
            if recipient is None:
                self._db_request_log.set_state(
                    sender_node_id,
                    obs.packet_id,
                    "dropped",
                    now,
                    reason=Reject.MAIL_NODE_INVALID.value,
                )
                self._metrics.commands_total.labels(Command.MAIL.value, "rejected").inc()
                return
        else:
            resolution = self._collector.resolve_node(
                node_raw, parse_node_argument=self._transport.parse_node_argument
            )
            peer_node_id = None
            if (
                resolution.node_id is None
                and not resolution.ambiguous
                and self.cfg.database.resolved_bbs_path is not None
            ):
                peer_node_id = self._resolve_on_peer(node_raw)
            if resolution.node_id is None and peer_node_id is None:
                self._db_request_log.set_state(
                    sender_node_id,
                    obs.packet_id,
                    "dropped",
                    now,
                    reason=Reject.MAIL_NODE_INVALID.value,
                )
                self._metrics.commands_total.labels(Command.MAIL.value, "rejected").inc()
                return
            if resolution.node_id is not None:
                recipient = self._bbs_id(resolution.node_id)
            else:
                # Qualified with the PEER's mesh: the bare id was found in
                # the peer's node table, not this one's.
                recipient = bbs_ids.qualify(peer_node_id, self._peer_transport_name())

        try:
            normalized = self._guestbook.validate(message_raw, inbound_payload_len=obs.payload_len)
        except SignError as exc:
            reason = _MAIL_SIGN_ERROR_REASON.get(exc.reason, exc.reason)
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "dropped", now, reason=reason.value
            )
            self._metrics.commands_total.labels(Command.MAIL.value, "rejected").inc()
            return

        refusal = self._queue.reserve(sender_node_id)
        if refusal is not None:
            self._db_request_log.set_state(
                sender_node_id, obs.packet_id, "dropped", now, reason=refusal.value
            )
            self._metrics.commands_total.labels(Command.MAIL.value, "queue_full").inc()
            self._note_drop(now)
            return

        day = local_day(now, self.cfg.service.timezone)
        if not self._quota.check_and_count_mail_sent(sender_node_id, day, now):
            self._metrics.commands_total.labels(Command.MAIL.value, "mail_quota").inc()
            self._note_drop(now)
            if not self._maybe_send_quota_notice(sender_node_id, obs, now, day, "mail"):
                self._db_request_log.set_state(
                    sender_node_id, obs.packet_id, "dropped", now,
                    reason=Reject.MAIL_QUOTA.value,
                )
            return

        # Opt-in/pending-cap decision: NEVER visible in the reply (ruling
        # 10) -- only in the two bounded internal counters below.
        recipient_opted_in = recipient is not None and self._mail_prefs.get(recipient)
        # pending_count deliberately excludes hidden (moderated) notes:
        # a moderated note doesn't consume the recipient's pending-limit
        # slot -- the sender's own daily_mail_quota (checked above) is
        # what bounds how many notes (moderated or not) one sender can
        # create per day.
        recipient_has_room = recipient is not None and (
            self._mail.pending_count(recipient) < self.cfg.service.mail_pending_limit
        )
        if recipient is not None and recipient_opted_in and recipient_has_room:
            moderation_reason: str | None = None
            if self._banned_terms:
                match = moderation.find_banned_term(
                    moderation.normalize_for_match(normalized), self._banned_terms
                )
                if match is not None:
                    moderation_reason = moderation.BANNED_WORD_REASON
            self._mail.send(
                self._bbs_id(sender_node_id), recipient, normalized, now,
                moderation_reason=moderation_reason,
            )
            self._metrics.mail_notes_stored.inc()
            usefulness.counter(self._db_conn, "MAIL_STORED", 1, now)
        else:
            self._metrics.mail_notes_refused.inc()

        self._db_request_log.set_state(sender_node_id, obs.packet_id, "queued", now)
        job = ReplyJob(
            request_sender=sender_node_id,
            request_packet_id=obs.packet_id,
            request_received_at=obs.received_at,
            deadline_monotonic=obs.received_monotonic + self.cfg.service.reply_ttl_seconds,
            body=mail_sent_ack_text(),
        )
        self._sender.enqueue(job)
        self._limiter.commit_command(sender_node_id)
        self._metrics.commands_total.labels(Command.MAIL.value, "accepted").inc()

    def _handle_lab_unlock(self, sender_node_id: str, obs: IncomingPacket, now: int) -> None:
        """LAB_UNLOCK: commands.py's `parse_command` already matched the
        operator-configured word as the sender's whole message (only
        possible when `service.lab_unlock_word` is non-empty -- see
        `_process_obs`'s gate, which never rewrites this command away).
        `Lab.unlock` is idempotent -- unlocking an already-unlocked node
        keeps its original `unlocked_at` -- but this handler still sends
        the ordinary `lab_text()` reply every time, same fixed-reply-
        regardless-of-effect convention as MAIL's opt-in ack. The word
        itself is never included in the reply, a log line, or a metric
        label -- only the bounded `Command.LAB_UNLOCK` value is."""
        self._lab.unlock(self._bbs_id(sender_node_id), now)
        body = truncate_utf8(lab_text(), self._transport.max_text_bytes)
        sent = self._reserve_and_send(sender_node_id, obs, now, body)
        outcome = "accepted" if sent else "queue_full"
        self._metrics.commands_total.labels(Command.LAB_UNLOCK.value, outcome).inc()

    def _check_meshcore_wedge(self) -> None:
        """Detects and recovers from a wedged MeshCore run (task 3B-2's
        "three lifecycle facts", #1; see task-3B-1-report.md's fix rounds
        3/4 and `meshcore_client.py`'s module docstring).

        The pinned library's `MeshCore.disconnect()` can hang forever
        (`dispatcher.stop()` awaits `queue.join()` on a consumer that
        already exited with items left) -- and that await sits inside the
        run's own outer reconnect loop's session teardown, so when it
        hangs, that loop never reaches its next `create_serial()` attempt
        again. The transport keeps honestly reporting `status().connected
        == False` forever: not a lie, but not something that will ever
        self-heal either. Only external supervision can tell "an ordinary
        backoff wait" from "wedged forever" (still disconnected long after
        any ordinary wait would have retried), so this runs on the DB
        worker thread's maintenance tick. The threshold is a flat 120s
        (not scaled off `meshcore_backoff_max_seconds`, which defaults to
        300s and made this take up to 600s / 10 minutes off the air): 120s
        off the air is already worth forcing a fresh reconnect over.

        Recovery: `self._meshcore_client.stop()` (bounded, timed joins --
        see that method) then `.connect()` -- a brand-new run, independent
        of whatever the old one's stranded thread is still doing. Safe by
        the fix-round-3/4 generation-token design: a stranded run's belated
        thread, whenever it finally wakes, disconnects and forwards
        nothing, never becoming a second live source. Never calls
        `connect()` again until `stop()` has returned, per that module's
        central rule -- this method does exactly that, in order, and
        nothing else touches `self._meshcore_client` concurrently (this
        tick is the only caller).
        """
        status = self._transport.status()
        if status.connected:
            self._meshcore_wedge_since_monotonic = None
            return
        now_mono = self._clock.monotonic()
        if self._meshcore_wedge_since_monotonic is None:
            self._meshcore_wedge_since_monotonic = now_mono
            return
        threshold = 120.0
        if now_mono - self._meshcore_wedge_since_monotonic < threshold:
            return
        log.error(
            "meshcore transport disconnected for over %.0fs with no self-heal; "
            "forcing a fresh reconnect (see task-3B-1-report.md's fix rounds 3/4 "
            "on the pinned library's disconnect() hanging forever)",
            threshold,
        )
        self._meshcore_client.stop()
        self._meshcore_client.connect()
        self._meshcore_wedge_since_monotonic = None

    def _operator_advert(self) -> tuple[bool, str]:
        if self._stop_event.is_set() or not self._transport.status().reply_ready:
            return False, "radio not ready; check connection and configured node identity"
        send_advert = getattr(self._meshcore_client, "send_advert", None)
        if send_advert is None:
            return False, "MeshCore advertisement is unavailable"
        if not send_advert():
            return False, (
                "advertisement not confirmed (60-second cooldown or radio failure); see daemon log"
            )
        return True, "flood advertisement accepted by the radio"

    def _check_meshcore_advert_interval(self) -> None:
        """Task 3B-4 fix 2 (hardware finding: the radio only became
        reachable after a manual flood advertisement -- without one, a node
        that has never heard this garden cannot resolve a path to it, so
        even a correctly-ACKed reply DM can fail to route). `RealMeshCore
        Client._post_connect_setup` already sends one self-advertisement on
        every successful connection; this repeats it on `[radio] meshcore_
        advert_interval_seconds` (default 14400 = 4h) so a garden that
        stays connected for hours keeps re-announcing itself between
        reconnects too. `0` disables this repeat only -- the per-connection
        advert is unconditional and lives entirely in `meshcore_client.py`.

        This is presence beaconing, not message traffic: a deliberate,
        documented exception to the "transmits nothing but a DM reply"
        invariant (see `docs/architecture.md`/`docs/security-and-
        privacy.md`), analogous to what Meshtastic firmware already does on
        its own. Reuses the DB worker's existing maintenance tick (the same
        home `_check_meshcore_wedge` uses) rather than a new thread.
        """
        interval = self.cfg.radio.meshcore_advert_interval_seconds
        if interval <= 0:
            return
        now_mono = self._clock.monotonic()
        if self._meshcore_last_advert_monotonic is None:
            self._meshcore_last_advert_monotonic = now_mono
            return
        if now_mono - self._meshcore_last_advert_monotonic < interval:
            return
        self._meshcore_last_advert_monotonic = now_mono
        send_advert = getattr(self._meshcore_client, "send_advert", None)
        if send_advert is not None:
            send_advert()

    def _check_meshcore_contact_prune(self, now: int) -> None:
        """Opt-in daily housekeeping (`[radio] meshcore_contact_prune_enabled`):
        a radio at its contact cap stops auto-adding newcomers, so once per
        local day, at or after the configured hour ("at or after" so a daemon
        that was down at that hour still runs the day's pass), remove the
        least-recently-heard contacts down to `meshcore_contact_target`.
        Selection is pure (`contacts.select_contacts_to_prune`); this does the
        DB reads and radio I/O. Runs on the DB worker's maintenance tick."""
        radio = self.cfg.radio
        client = self._meshcore_client
        if (
            not radio.meshcore_contact_prune_enabled
            or client is None
            or self._db_conn is None
            or not hasattr(client, "list_contacts")
        ):
            return
        local = datetime.fromtimestamp(now, tz=ZoneInfo(self.cfg.service.timezone))
        today = local.strftime("%Y-%m-%d")
        if local.hour < radio.meshcore_contact_prune_hour:
            return
        conn = self._db_conn
        if get_daemon_state(conn, "contact_prune_day") == today:
            return
        contacts = client.list_contacts()
        if contacts is None:
            log.warning("contact prune: could not list radio contacts; will retry next tick")
            return
        mx = getattr(client, "max_contacts", None)
        self._contacts_status.update(count=len(contacts), max=mx)
        self._metrics.radio_contacts.set(len(contacts))
        if mx is not None:
            self._metrics.radio_contacts_max.set(mx)
        target = radio.meshcore_contact_target
        if mx is not None and target >= mx:
            log.warning(
                "contact prune: meshcore_contact_target (%d) must be below the radio's "
                "maximum (%d); skipping today", target, mx,
            )
            set_daemon_state(conn, "contact_prune_day", today, now)
            return
        last_seen = dict(conn.execute("SELECT node_id, observed_last_seen FROM nodes"))
        protected: set[str] = set(self.cfg.service.mobile_dm_recovery_destinations)
        protected.update(
            row[0] for row in conn.execute("SELECT DISTINCT sender_node_id FROM processed_requests")
        )
        # BBS tables are shared with the other mesh: only this mesh's ids
        # name contacts on this radio.
        for sql in ("SELECT node_id FROM lab_nodes", "SELECT node_id FROM mail_prefs"):
            protected.update(
                local for row in conn.execute(sql)
                if (local := self._bbs_local(row[0])) is not None
            )
        decision = select_contacts_to_prune(
            contacts, target=target, max_remove=radio.meshcore_contact_prune_max_per_day,
            now=now, last_seen_by_node=last_seen, protected_nodes=protected,
        )
        removed = 0
        for contact in decision.remove:
            if not client.remove_contact(contact.public_key):
                log.warning("contact prune: radio refused a removal; stopping this run")
                break
            self._transport.forget_contact(contact.public_key)
            log.debug("contact prune: removed %s", contact.node_id)
            removed += 1
        self._metrics.contacts_pruned_total.inc(removed)
        self._metrics.radio_contacts.set(len(contacts) - removed)
        self._contacts_status.update(
            count=len(contacts) - removed, last_prune_at=now, last_pruned=removed
        )
        log.info(
            "contact prune: %d contacts before, %d removed, %d protected, target %d",
            len(contacts), removed, decision.kept_protected, target,
        )
        set_daemon_state(conn, "contact_prune_day", today, now)

    def _run_maintenance(self, now: int | None = None) -> None:
        """`now` is TEST SUPPORT ONLY: production always omits it, so this
        computes real wall-clock time itself -- retention/rollup work in
        real UTC calendar days, independent of the daemon's injected test
        clock used elsewhere for cooldowns/TTLs.
        """
        if now is None:
            now = int(time.time())
        if self._meshcore_client is not None:
            # Heartbeat for MeshCore (task 3B-2 item C): RadioAdapter's own
            # background loop beats "radio" on its own schedule regardless
            # of traffic; MeshCore has no equivalent hook exposed from
            # RealMeshCoreClient, so this ties "radio" liveness to the
            # maintenance tick itself instead -- coarser (60s resolution)
            # but still catches the failure category heartbeats exist for
            # (the whole DB worker loop, and therefore this tick, stopping).
            self._heartbeats.beat("radio")
            self._check_meshcore_wedge()
            self._check_meshcore_advert_interval()
            self._check_meshcore_contact_prune(now)
        # getattr: tests drive _run_maintenance on a bare Daemon built with
        # __new__ (see tests/test_daemon.py::_bare_daemon_for_maintenance)
        # that never ran __init__, so weather attributes may be absent.
        if getattr(self, "_weather_ledger", None) is not None:
            pruned = self._weather_ledger.prune(now)
            if pruned:
                log.info("weather: pruned %d expired alert row(s)", pruned)
            self._maybe_send_weather_heartbeat(now)
        aged = self._retention.age_stale_submitted(now, self.cfg.service.ack_timeout_seconds)
        if aged:
            self._metrics.replies_total.labels("unknown").inc(aged)

        # Safety net for a 'received' row stuck in a daemon that never
        # restarts (resolve_uncertain_on_startup only runs once, at
        # construction) -- see RequestLog.age_stale_received.
        aged_received = self._db_request_log.age_stale_received(
            now, self.cfg.service.reply_ttl_seconds
        )
        if aged_received:
            self._metrics.replies_total.labels("unknown").inc(aged_received)

        # Settle any MAIL-list reply whose delivery outcome (acked vs.
        # failed/dropped/expired) has landed since the last tick -- see
        # _handle_mail_list / _resolve_pending_mail_deliveries.
        self._resolve_pending_mail_deliveries(now)

        # Rollup BEFORE prune: prune_processed_requests (below) deletes
        # expired terminal-state rows, and rollup_day recomputes a day's
        # daily_stats from whatever processed_requests rows still exist --
        # rolling up first means a day is always finalized from its
        # complete set of requests, never from a set already thinned by
        # this same tick's prune. (rollup_day also refuses on its own to
        # recompute a day already marked `finalized`, so this ordering is
        # belt-and-suspenders, not the only guard against a restart's
        # `_last_rollup_day`-reset re-triggering yesterday's rollup.)
        today = datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d")
        if today != self._last_rollup_day:
            # The UTC day changed since the last tick, or this is the
            # first tick after startup (_last_rollup_day is still None --
            # e.g. a restart shortly after midnight): finalize yesterday's
            # row one more time, since the tick that would otherwise have
            # done so may never have run. Bounded to at most one extra
            # rollup_day call per tick.
            yesterday = (datetime.fromtimestamp(now, tz=UTC) - timedelta(days=1)).strftime(
                "%Y-%m-%d"
            )
            self._retention.rollup_day(yesterday)
            if self._last_rollup_day is not None:
                # A genuine day change (not the first tick after startup,
                # which has no real "previous day" to report): fire the
                # once-per-day-change hook, now that `yesterday` is
                # finalized. See _on_local_midnight.
                self._on_local_midnight(yesterday)
        # Pending requests may settle after the midnight tick, or across downtime.
        unfinished = self._db_conn.execute(
            "SELECT day FROM daily_stats WHERE finalized = 0 AND day < ? ORDER BY day LIMIT 90",
            (today,),
        ).fetchall()
        for row in unfinished:
            self._retention.rollup_day(row["day"])
        self._retention.rollup_day(today)
        self._last_rollup_day = today

        # Daily digest: scheduled on the CONFIGURED TIMEZONE's calendar day
        # changing, tracked independently of `_last_rollup_day` above.
        # digest.py's own queries (build_digest/_local_day_window) are all
        # in local-day terms, so driving the write from the UTC day (as
        # this used to, via _on_local_midnight) could fire hours before
        # local midnight for that day has actually arrived -- e.g. a UTC
        # rollover at 17:00 local finalizes a digest labeled with today's
        # local date while 7 more hours of that local day's packets are
        # still to come. Computed and written BEFORE the prune_* calls
        # below, same as the UTC rollup, so the data it reads is never
        # already thinned by this same tick.
        today_local = local_day(now, self.cfg.service.timezone)
        if today_local != self._last_digest_local_day:
            if self._last_digest_local_day is not None:
                self._digest_midnight_task(self._last_digest_local_day)
            self._last_digest_local_day = today_local

        self._retention.prune_observations(now)
        self._retention.prune_nodes(now)
        usefulness.sync(self._db_conn, now)
        self._retention.prune_processed_requests(now)
        self._retention.prune_aggregates(now)
        self._retention.prune_positions(now)
        self._retention.prune_local_radio_samples(now)
        self._retention.prune_node_daily(now)
        self._retention.prune_links(now)
        # Same bounded-batch shape again, for undelivered mail past
        # [retention] mail_expiry_days (ruling 12) -- delivered notes are
        # already gone as soon as their MAIL-list reply is actually acked
        # (see _resolve_pending_mail_deliveries), not merely queued.
        self._mail.prune_expired(now, self.cfg.retention.mail_expiry_days)
        self._mail.prune_reply_routes(now)
        usefulness.prune(self._db_conn, now)
        # Bounded batches, same as every other prune_* above; WORDLE's own
        # per-player state (guesses/solved), never a guessed word or a raw
        # node id -- see wordle.py's own docstring.
        prune_wordle_state(self._db_conn, now)
        # Same bounded-batch shape for BOOK's per-player solved flag.
        booktrivia.prune_book_state(self._db_conn, now)
        # Same bounded-batch shape for node_daily_quota rows.
        self._quota.prune(now)
        # Same bounded-batch shape for channel_knocks rows (task 3B-3) --
        # only meaningful when channel triggering is on at all (Meshtastic,
        # and a channel-less MeshCore install, never write this table).
        if self._meshcore_channel_policy is not None:
            self._knock_cooldown.prune(now, self._meshcore_channel_policy.cooldown_seconds)
        notices = getattr(self, "_command_cooldown_notices", None)
        if notices is None:
            # Some maintenance-only callers intentionally construct the
            # daemon without starting workers; use that caller's DB handle.
            notices = CommandCooldownNotices(self._db_conn)
        notices.prune(now)
        # Same bounded-batch shape again, this time for old digest/*.json
        # files -- `today` here (a LOCAL calendar day, unlike this method's
        # own UTC `today` above) matches the convention build_digest/
        # write_digest_file already use for a digest's `day`.
        digest.prune_digests(self.cfg, today=local_day(now, self.cfg.service.timezone))

        if self._retention.due_for_backup(now):
            self._retention.backup(now)
        self._refresh_gauges(now)
        self._heartbeats.beat("maintenance")

    def _on_local_midnight(self, prev_day: str) -> None:
        """Fires once per day change (see `_run_maintenance`'s existing
        `_last_rollup_day` tracking, immediately after `prev_day` was just
        finalized by the existing yesterday-rollup call above) -- never on
        the first tick after startup, which has no real "previous day" of
        its own to report. `prev_day` is a YYYY-MM-DD string in the same
        UTC calendar-day convention `daily_stats`/`rollup_day`/WEEK already
        use everywhere else.

        Prepare-only seam: runs `self._midnight_tasks` (currently empty --
        the daily digest used to be wired in here, but a digest is a
        local-day concept and this fires on the UTC day changing, so it's
        now driven directly by `_run_maintenance`'s own
        `_last_digest_local_day` tracking instead; see
        `_digest_midnight_task`) so a future UTC-day-scoped per-day task
        can be wired in with a one-line `self._midnight_tasks.append(...)`,
        without this method itself needing to change. A task's own
        exception is caught and logged so one broken task can never block
        another, or the maintenance tick that called this.
        """
        for task in self._midnight_tasks:
            try:
                task(prev_day)
            except Exception:
                log.exception("midnight task failed for day %s", prev_day)

    def _digest_midnight_task(self, prev_day: str) -> None:
        """Builds and writes the aggregate-only operator digest for
        `prev_day` -- a calendar day in `[service] timezone`, just
        finalized by `_run_maintenance`'s `_last_digest_local_day`
        tracking (NOT `_on_local_midnight`'s UTC-day tracking: a digest's
        own queries, build_digest/_local_day_window, are entirely in
        local-day terms, so scheduling from the UTC day could fire before
        -- or after -- that local day has actually ended). Runs on the DB
        worker thread's own connection, using `[service]
        timezone`/`haiku_place` from config -- the same fields the
        `digest` CLI command already uses (see cli_ops.cmd_digest).

        Bounded to one digest built and written per call. Any failure (a
        full disk, a permissions problem, a bad query, ...) is caught and
        logged here, never raised, so the log line names the digest
        specifically.
        """
        try:
            d = digest.build_digest(
                self._db_conn, day=prev_day, tz=self.cfg.service.timezone, now=int(time.time())
            )
            d = dict(d)
            d["place"] = self.cfg.service.haiku_place
            digest.write_digest_file(self.cfg, d)
        except Exception:
            log.exception("failed to build/write digest for day %s", prev_day)

    def _write_startup_catchup_digest(self) -> None:
        """Startup-only catch-up: if the daemon was not running through
        the last local midnight (so `_digest_midnight_task` never got a
        chance to fire for yesterday -- e.g. a restart, or the very first
        run of the day), write yesterday's digest now. Only when no file
        for it exists yet (never overwrites one already written, by an
        earlier run or an earlier catch-up) and only when yesterday
        actually has packet activity (a legitimately idle day -- most
        commonly the very first startup ever, before any traffic -- gets
        no placeholder digest full of zeroes).

        Called once from `_db_worker_run`, before the main loop starts.
        Bounded to at most one digest built and written; any failure is
        caught and logged, never fatal to startup.
        """
        try:
            tz = self.cfg.service.timezone
            now = int(time.time())
            day = digest.default_digest_day(now, tz)
            dest = os.path.join(_database_dir(self.cfg), "digest", f"{day}.json")
            if os.path.exists(dest):
                return
            d = digest.build_digest(self._db_conn, day=day, tz=tz, now=now)
            if not d["packets"]["raw"]:
                return
            d = dict(d)
            d["place"] = self.cfg.service.haiku_place
            digest.write_digest_file(self.cfg, d)
        except Exception:
            log.exception("failed to write startup catch-up digest")

    def _refresh_live_gauges(self, now: int) -> None:
        """Publish live health at startup and every status tick, without retention work."""
        if self._radio is not None:
            status = self._radio.status()
            connected = status.connected
            reply_ready = status.reply_ready
            connected_seconds = 0
            if status.connected and status.connected_since is not None:
                connected_seconds = max(0, now - status.connected_since)
        else:
            # MeshCore: no RadioStatus/connected_since (an absolute epoch
            # stamp); Transport.status() already reports connected_seconds
            # directly (monotonic-based), which is exactly this gauge's
            # figure -- no daemon-side bookkeeping needed here, unlike
            # `_current_status`'s disconnected_since_monotonic tracking.
            tstatus = self._transport.status()
            connected = tstatus.connected
            reply_ready = tstatus.reply_ready
            connected_seconds = (
                max(0, int(tstatus.connected_seconds)) if tstatus.connected_seconds else 0
            )
        self._metrics.radio_connected.set(1 if connected else 0)
        self._metrics.reply_ready.set(1 if reply_ready else 0)
        self._metrics.radio_connected_seconds.set(connected_seconds)

        self._metrics.distinct_senders_1h.set(self._collector.distinct_senders_since(now - 3600))
        self._metrics.distinct_senders_24h.set(self._collector.distinct_senders_since(now - 86400))
        self._metrics.guestbook_visible.set(self._guestbook.visible_count())
        self._metrics.service_uptime_seconds.set(max(0, now - self.started_at))
        self._metrics.queue_depth.set(self._queue.depth())

    def _refresh_gauges(self, now: int) -> None:
        self._refresh_live_gauges(now)
        report = self._retention.storage_report()
        self._metrics.storage_used_bytes.set(report.total_bytes)
        self._metrics.storage_cap_bytes.set(report.cap_bytes)
        self._metrics.storage_pressure.set(_STORAGE_PRESSURE_LEVEL[report.pressure])
        self._metrics.backup_last_success_ts.set(self._retention.last_backup_at or 0)
        self._metrics.backup_healthy.set(0 if self._retention.last_backup_error else 1)

        self._metrics.rate_limited_drops_1h.set(self._rate_limited_drops_1h(now))

    # -- TX worker thread -----------------------------------------------------

    def _tx_worker_run(self, ready: threading.Event, sender_ready: threading.Event) -> None:
        """Owns its own connection end to end: opened here, closed here.
        Never touched by any other thread."""
        self._tx_conn = connect(
            self.cfg.database.path, bbs_path=self.cfg.database.resolved_bbs_path,
            transport=self.cfg.radio.transport,
        )
        self._tx_conn_owner_ident = threading.get_ident()
        try:
            self._tx_request_log = RequestLog(
                self._tx_conn, dedup_hours=self.cfg.service.request_dedup_hours
            )
            ready.set()
            sender_ready.wait()
            self._tx_worker_loop()
        finally:
            self._tx_conn.close()

    def _tx_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            self._heartbeats.beat("tx")
            try:
                self._sender.pump()
                self._limiter.purge()
            except Exception:
                log.exception("tx worker: unexpected error in pump/purge")
            self._stop_event.wait(_TX_LOOP_POLL_SECONDS)


# =============================================================================
# CLI -- operator commands. `run` is the only subcommand that ever constructs
# a Daemon (and therefore the only one that ever opens the radio).
# `advert` asks that running daemon to transmit via its local control socket;
# database subcommands open their own short-lived database
# connection (WAL + busy_timeout, from database.connect) and closes it in a
# finally block. Moderation, blocking, and configuration changes are local
# operator actions only -- never available over the radio.
# =============================================================================

DEFAULT_CONFIG_PATH = "/etc/meshtastic-observatory/config.toml"


def _log_dir_for(cfg: Config) -> str:
    return os.path.join(_database_dir(cfg), "logs")


def _read_status_file(cfg: Config) -> dict | None:
    """Returns the daemon's last status snapshot, or None if the file is
    missing or older than _STATUS_FILE_STALE_SECONDS (daemon likely not
    running)."""
    path = _status_file_path(cfg)
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return None
    if age > _STATUS_FILE_STALE_SECONDS:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _connect_readonly_or_report(cfg: Config) -> sqlite3.Connection | None:
    """Read-only connection for status/db-stats: a writable `connect()`
    would silently create a fresh, empty, migrated database (and its parent
    directories) when the configured path is wrong, making a mistyped path
    look like a daemon that simply hasn't observed anything yet. Returns
    None (after printing the bounded error to stderr) instead."""
    try:
        return connect(cfg.database.path, read_only=True, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    except sqlite3.OperationalError:
        print(f"error: database not found: {cfg.database.path}", file=sys.stderr)
        return None


def cmd_status(cfg: Config, *, as_json: bool) -> int:
    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        collector = Collector(conn)
        guestbook = Guestbook(conn, cfg.service)
        retention = Retention(conn, cfg.retention, cfg.backup, log_dir=_log_dir_for(cfg))
        now = int(time.time())

        data: dict[str, Any] = {
            "version": __version__,
            "connected": None,
            "reply_ready": None,
            "radio_disconnected_seconds": None,
            "expected_node_id": cfg.radio.expected_node_id,
            "actual_node_id": None,
            "mismatch_reason": None,
            "uptime_seconds": None,
            "last_packet_at": None,
            "queue_depth": None,
            "backup_last_success": None,
            "backup_last_error": None,
            "rate_limited_drops_1h": None,
            "daemon_status": "stale (daemon not running?)",
            "workers": None,
            "web_status": None,
        }
        snapshot = _read_status_file(cfg)
        if snapshot is not None:
            data.update(
                {
                    "connected": snapshot.get("radio_connected"),
                    "reply_ready": snapshot.get("reply_ready"),
                    "radio_disconnected_seconds": snapshot.get(
                        "radio_disconnected_seconds"
                    ),
                    "actual_node_id": snapshot.get("actual_node_id"),
                    "mismatch_reason": snapshot.get("mismatch_reason"),
                    "uptime_seconds": snapshot.get("uptime_seconds"),
                    "last_packet_at": snapshot.get("last_packet_at"),
                    "queue_depth": snapshot.get("queue_depth"),
                    "backup_last_success": snapshot.get("last_backup_at"),
                    "backup_last_error": snapshot.get("last_backup_error"),
                    "rate_limited_drops_1h": snapshot.get("rate_limited_drops_1h"),
                    "daemon_status": "ok",
                    "workers": snapshot.get("workers"),
                    "web_status": snapshot.get("web_status"),
                }
            )

        report = retention.storage_report()
        data["raw_receive_events"] = collector.raw_events_since(0)
        data["unique_packets"] = collector.unique_packets_since(0)
        data["distinct_senders_1h"] = collector.distinct_senders_since(now - 3600)
        data["distinct_senders_24h"] = collector.distinct_senders_since(now - 86400)
        data["guestbook_visible"] = guestbook.visible_count()
        data["storage_used_bytes"] = report.total_bytes
        data["storage_pressure"] = report.pressure

        if as_json:
            print(json.dumps(data))
        else:
            workers = data.pop("workers")
            for key, value in data.items():
                print(f"{key}: {value}")
            if workers:
                for name, info in workers.items():
                    print(
                        f"worker {name}: age={info['age_seconds']}s "
                        f"alive={info['alive']} stalled={info['stalled']}"
                    )
        return 0
    finally:
        conn.close()


def cmd_db_stats(cfg: Config) -> int:
    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        shared = cfg.database.resolved_bbs_path is not None
        # With a shared file the BBS tables are not in main: list them
        # separately under their own heading, as the same names resolve
        # to bbs once main's copies are gone.
        tables = ["observations", "nodes", "guestbook", "processed_requests", "daily_stats",
                  "blocked_senders"]
        if shared:
            tables = [t for t in tables if t not in bbs_store.BBS_TABLES]
        for table in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table}: {count}")
        if shared:
            print("bbs:")
            for table in bbs_store.BBS_TABLES:
                count = conn.execute(f"SELECT COUNT(*) FROM bbs.{table}").fetchone()[0]
                print(f"  {table}: {count}")
        return 0
    finally:
        conn.close()


_MIN_LIST_LIMIT = 1
_MAX_LIST_LIMIT = 1000


def _invalid_limit(limit: int) -> bool:
    return not (_MIN_LIST_LIMIT <= limit <= _MAX_LIST_LIMIT)


def _cli_bbs_node_label(conn: sqlite3.Connection, stored: str) -> str:
    name = lookup_bbs_node_name(conn, stored)
    return f"{name} ({stored})" if name else stored


def _cli_node_label(conn: sqlite3.Connection, node_id: str) -> str:
    name = lookup_node_name(conn, node_id)
    return f"{name} ({node_id})" if name else node_id


def cmd_requests(cfg: Config, *, limit: int, as_json: bool) -> int:
    """Local-only, read-only: most recent `processed_requests` rows, newest
    first. Never includes message/argument text -- the table carries none.
    Never constructs a RadioAdapter."""
    if _invalid_limit(limit):
        print(
            f"error: --limit must be between {_MIN_LIST_LIMIT} and {_MAX_LIST_LIMIT} (got {limit})",
            file=sys.stderr,
        )
        return 2

    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        request_log = RequestLog(conn, dedup_hours=cfg.service.request_dedup_hours)
        rows = request_log.recent(limit)
        data = [
            {
                "time": local_iso(row["first_seen_at"], cfg.service.timezone),
                "sender_node_id": row["sender_node_id"],
                "command": row["command"],
                "state": row["state"],
                "reason": row["reason"],
                "tx_packet_id": row["tx_packet_id"],
            }
            for row in rows
        ]
        if as_json:
            print(json.dumps(data))
        else:
            for d in data:
                print(
                    f"{d['time']}  {_cli_node_label(conn, d['sender_node_id'])}  "
                    f"{(d['command'] or '-'):<12} {d['state']:<10} "
                    f"{(d['reason'] or '-'):<16} "
                    f"{d['tx_packet_id'] if d['tx_packet_id'] is not None else '-'}"
                )
        return 0
    finally:
        conn.close()


def cmd_guestbook_list(
    cfg: Config, *, limit: int, hidden: bool, as_json: bool, topic: str | None = None
) -> int:
    """Local-only, read-only: guestbook entries, newest first. Visible only
    unless `hidden`, in which case hidden rows are included too (marked
    'hidden'). `topic` filters to that topic; omitted (None) means the main
    guestbook only, never "all topics" (ruling 9) -- unchanged output for
    existing scripts that don't pass --topic. Never constructs a
    RadioAdapter."""
    if _invalid_limit(limit):
        print(
            f"error: --limit must be between {_MIN_LIST_LIMIT} and {_MAX_LIST_LIMIT} (got {limit})",
            file=sys.stderr,
        )
        return 2

    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        guestbook = Guestbook(conn, cfg.service)
        entries = guestbook.list_entries(limit, include_hidden=hidden, topic=topic)
        data = [
            {
                "entry_id": e.entry_id,
                "time": local_iso(e.created_at, cfg.service.timezone),
                "sender_node_id": e.sender_node_id,
                "display_name": e.display_name or fallback_display_name(e.sender_node_id),
                "visibility": "visible" if e.visible else "hidden",
                "message": e.message,
            }
            for e in entries
        ]
        if as_json:
            print(json.dumps(data))
        else:
            for d in data:
                tag = "" if d["visibility"] == "visible" else " [hidden]"
                print(
                    f"#{d['entry_id']} {d['time']}  {d['sender_node_id']}  "
                    f"{d['display_name']}: {d['message']}{tag}"
                )
        return 0
    finally:
        conn.close()


def _guestbook_topic_mismatch(entry_id: int, topic: str | None) -> str:
    where = f"topic {topic}" if topic else "the main guestbook"
    return f"error: entry {entry_id} is not in {where}"


def cmd_guestbook_hide(cfg: Config, entry_id: int, *, topic: str | None = None) -> int:
    """`topic` is a guard (ruling 9): `entry_id` must belong to that topic
    (or, if omitted, must be a main-guestbook entry) or the command refuses
    with an error and does nothing."""
    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    try:
        migrate(conn, transport=cfg.radio.transport)
        row = conn.execute("SELECT topic FROM guestbook WHERE entry_id=?", (entry_id,)).fetchone()
        if row is None:
            print(f"guestbook entry not found: {entry_id}", file=sys.stderr)
            return 3
        if row["topic"] != topic:
            print(_guestbook_topic_mismatch(entry_id, topic), file=sys.stderr)
            return 3
        if not Guestbook(conn, cfg.service).hide(entry_id):
            print(f"guestbook entry not found: {entry_id}", file=sys.stderr)
            return 3
        print(f"hidden: {entry_id}")
        return 0
    finally:
        conn.close()


def cmd_guestbook_delete(cfg: Config, entry_id: int, *, topic: str | None = None) -> int:
    """Same topic guard as `cmd_guestbook_hide` (ruling 9)."""
    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    try:
        migrate(conn, transport=cfg.radio.transport)
        row = conn.execute("SELECT topic FROM guestbook WHERE entry_id=?", (entry_id,)).fetchone()
        if row is None:
            print(f"guestbook entry not found: {entry_id}", file=sys.stderr)
            return 3
        if row["topic"] != topic:
            print(_guestbook_topic_mismatch(entry_id, topic), file=sys.stderr)
            return 3
        if not Guestbook(conn, cfg.service).delete(entry_id):
            print(f"guestbook entry not found: {entry_id}", file=sys.stderr)
            return 3
        print(f"deleted: {entry_id}")
        return 0
    finally:
        conn.close()


def cmd_block(cfg: Config, node_id: str) -> int:
    parsed = cli_ops.bbs_node_argument(cfg, node_id)
    if parsed is None:
        print(f"invalid node id: {node_id!r}", file=sys.stderr)
        return 2
    node_id = parsed
    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    try:
        migrate(conn, transport=cfg.radio.transport)
        now = int(time.time())
        with conn:
            conn.execute(
                "INSERT INTO blocked_senders(node_id, blocked_at) VALUES (?, ?) "
                "ON CONFLICT(node_id) DO NOTHING",
                (node_id, now),
            )
        print(f"blocked: {_cli_bbs_node_label(conn, node_id)}")
        return 0
    finally:
        conn.close()


def cmd_unblock(cfg: Config, node_id: str) -> int:
    parsed = cli_ops.bbs_node_argument(cfg, node_id)
    if parsed is None:
        print(f"invalid node id: {node_id!r}", file=sys.stderr)
        return 2
    node_id = parsed
    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    try:
        migrate(conn, transport=cfg.radio.transport)
        with conn:
            cur = conn.execute("DELETE FROM blocked_senders WHERE node_id=?", (node_id,))
            deleted = cur.rowcount
        if deleted == 0:
            print(f"not blocked: {_cli_bbs_node_label(conn, node_id)}", file=sys.stderr)
            return 3
        print(f"unblocked: {_cli_bbs_node_label(conn, node_id)}")
        return 0
    finally:
        conn.close()


def cmd_books(cfg: Config, *, as_json: bool) -> int:
    """Local-only, read-only: all-time book_stats aggregates, one row per
    book title. Never constructs a RadioAdapter."""
    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        rows = book_stats_summary(conn)
        data = [
            {"book": r.book_key, "shown": r.shown, "guesses": r.guesses, "solves": r.solves}
            for r in rows
        ]
        if as_json:
            print(json.dumps(data))
        else:
            for d in data:
                print(
                    f"{d['book']}: shown={d['shown']} guesses={d['guesses']} "
                    f"solves={d['solves']}"
                )
        return 0
    finally:
        conn.close()


def cmd_wordle(cfg: Config, *, days: int, as_json: bool) -> int:
    """Local-only, read-only: newest-first wordle_daily aggregates, up to
    `days` of them. Never constructs a RadioAdapter."""
    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        rows = wordle_stats_summary(conn, days)
        data = [
            {
                "day": r.day,
                "players": r.players,
                "solves": r.solves,
                "avg_guesses": round(r.avg_guesses, 2),
            }
            for r in rows
        ]
        if as_json:
            print(json.dumps(data))
        else:
            for d in data:
                print(
                    f"{d['day']}: players={d['players']} solves={d['solves']} "
                    f"avg_guesses={d['avg_guesses']}"
                )
        return 0
    finally:
        conn.close()


def cmd_notify_test(cfg: Config) -> int:
    """Sends one test notification through the exact path the watchdog uses.

    Shares `watchdog.notify`, so a pass here proves the real alert path
    works rather than merely that some HTTP request can be made. Never
    echoes the URL: the ntfy topic is a shared secret, and printing it
    would leak it into terminal scrollback.
    """
    env = watchdog.effective_env()
    url = env.get(watchdog.ENV_NTFY_URL, "")
    if not url:
        print(
            f"{watchdog.ENV_NTFY_URL} is not set (looked in the environment and "
            f"{env.get(watchdog.ENV_FILE, watchdog.DEFAULT_ENV_FILE)}). "
            "Set it to your ntfy topic URL; try sudo if the env file is root-only.",
            file=sys.stderr,
        )
        return 2
    delivered = watchdog.notify(
        url,
        "Meshgarden test notification",
        "This is a test alert from mgarden notify-test. No action needed.",
    )
    if not delivered:
        print(
            "notification failed to send (see the log line above for the reason)",
            file=sys.stderr,
        )
        return 1
    print("test notification sent")
    return 0


def cmd_advert(cfg: Config) -> int:
    if cfg.radio.transport != "meshcore":
        print("advert requires radio.transport = meshcore", file=sys.stderr)
        return 2
    ok, message = advert_control.request_advert(cfg.database.path)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def cmd_radio_watchdog(cfg: Config) -> int:
    """One watchdog poll, for the systemd timer.

    Exits 0 whenever the poll itself completed, even when the radio is
    down: the alert is the notification, and a unit that fails every
    minute during an outage would be its own kind of noise (and would bury
    a genuinely broken watchdog in `systemctl --failed`).
    """
    env = watchdog.effective_env()
    state_path = env.get(
        watchdog.ENV_STATE_PATH, os.path.join(_database_dir(cfg), "watchdog-state.json")
    )
    try:
        status = watchdog.run_once(
            metrics=cfg.metrics, state_path=state_path, env=env
        )
    except Exception as exc:
        print(f"watchdog poll failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"radio watchdog: {status}")
    return 0


def cmd_backup(cfg: Config) -> int:
    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    try:
        migrate(conn, transport=cfg.radio.transport)
        retention = Retention(conn, cfg.retention, cfg.backup, log_dir=_log_dir_for(cfg))
        path = retention.backup(int(time.time()))
        if path is None:
            print(f"backup failed: {retention.last_backup_error}", file=sys.stderr)
            return 1
        print(f"backup created: {path}")
        return 0
    finally:
        conn.close()


def cmd_run(cfg: Config, *, debug: bool = False) -> int:
    try:
        daemon = Daemon(cfg, debug=debug)
        daemon.start()
        daemon.run_forever()
        return 0
    except bbs_store.SplitConflict as exc:
        # An operator decision (which instance brings its BBS data), not a
        # bug: report the message without a traceback.
        log.error("daemon failed to start: %s", exc)
        print(f"daemon failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        log.exception("daemon failed to run")
        print(f"daemon failed: {exc}", file=sys.stderr)
        return 1


_TOP_DESCRIPTION = (
    "mgarden is the operator command line for meshgarden, the mesh radio daemon and "
    "its database.\n"
    "Every command reads the same config.toml the daemon uses, chosen with --config, "
    "$MESHTASTIC_OBSERVATORY_CONFIG, or /etc/meshgarden/config.toml.\n"
    "Most commands only read the database and never transmit over the radio."
)

_TOP_EPILOG = """\
Command groups
  Run & health    run, status, doctor, radio-watchdog, notify-test, advert
  Watch traffic   tail, requests, digest, usefulness, db-stats
  Visitors        guestbook, block, unblock, quota, lab, mail, news
  Games           books, wordle
  Data            backup, restore, export, config-sync

Examples
  mgarden status                  is the daemon up, is the radio connected
  mgarden tail -n 200             last 200 trace lines, then follow (needs run --debug)
  mgarden requests --limit 50     what visitors asked recently
  mgarden guestbook list --hidden
  mgarden quota show !a1b2c3d4
  mgarden news add "Garden moved to #kallisti" --days 14

Run `mgarden <command> -h` (or `mgarden help <command>`) for that command's options."""


def _add_command(subparsers, name, *, help, description, examples=()):
    """Adds a subparser with the shared help layout (wrapped description,
    verbatim Examples epilog) so every command reads the same under -h."""
    epilog = "Examples\n" + "\n".join(f"  {e}" for e in examples) if examples else None
    return subparsers.add_parser(
        name,
        help=help,
        description=textwrap.fill(description, 78),
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_parser() -> argparse.ArgumentParser:
    # Fixed "mgarden" prog so usage/error text names the current CLI even
    # when invoked through the deprecated meshtastic-observatory alias.
    parser = argparse.ArgumentParser(
        prog="mgarden",
        usage="mgarden [--config PATH] <command> [options]",
        description=_TOP_DESCRIPTION,
        epilog=_TOP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        # Fixed "mgarden" prefix regardless of which script alias
        # (mgarden / meshtastic-observatory) invoked this: both are the
        # same entry point (see pyproject.toml's [project.scripts]).
        version=f"mgarden {__version__}",
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help=(
            "path to config.toml (default: $MESHTASTIC_OBSERVATORY_CONFIG, "
            "else /etc/meshgarden/config.toml)"
        ),
    )
    # required=False so a bare `mgarden` prints the full help (see main())
    # instead of argparse's one-line "arguments are required" error.
    sub = parser.add_subparsers(dest="command", required=False, metavar="<command>")

    run_parser = _add_command(
        sub,
        "run",
        help="connect to the radio and run the daemon",
        description=(
            "Connects to the radio and runs the daemon until stopped. This is the only "
            "command that transmits; it needs the radio attached and configured."
        ),
        examples=("mgarden run", "mgarden --config ./config.toml run --debug"),
    )
    run_parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "print a DM/reply exchange trace to stderr (message content); "
            "operator opt-in only, never for an unattended/systemd deployment"
        ),
    )

    status_parser = _add_command(
        sub,
        "status",
        help="report daemon/observatory status",
        description=(
            "Shows whether the daemon is up and the radio is connected, plus identity, "
            "queue depth, storage and backup health. Read-only; exits 1 if the database "
            "is not found."
        ),
        examples=("mgarden status", "mgarden status --json"),
    )
    status_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")

    _add_command(
        sub,
        "db-stats",
        help="report per-table row counts",
        description=(
            "Prints how many rows each database table holds. Read-only; exits 1 if the "
            "database is not found."
        ),
    )
    _add_command(
        sub,
        "usefulness",
        help="local JSON report of visitor return and command use",
        description=(
            "Prints a JSON report of how often visitors return and which commands they "
            "use. Aggregate and read-only; never transmits."
        ),
    )
    news_parser = _add_command(
        sub,
        "news",
        help="manage local notices (no radio transmissions)",
        description=(
            "Adds, lists, hides or deletes local notices shown to visitors on request. "
            "Changes the database only; nothing is transmitted."
        ),
        examples=(
            'mgarden news add "Garden moved to #kallisti" --days 14',
            "mgarden news list",
            "mgarden news hide 3",
        ),
    )
    news_sub = news_parser.add_subparsers(dest="news_command", required=True, metavar="<action>")
    news_add = _add_command(
        news_sub,
        "add",
        help="publish a short notice",
        description="Publishes a short notice that visitors can read until it expires.",
        examples=(
            'mgarden news add "Garden moved to #kallisti" --days 14',
            'mgarden news add "Maintenance tonight"',
        ),
    )
    news_add.add_argument("text", metavar="TEXT", help="notice text (quote it)")
    news_add.add_argument(
        "--days", type=int, default=7, metavar="N", help="expires after N days, 1-90 (default: 7)"
    )
    _add_command(
        news_sub,
        "list",
        help="list unexpired notices",
        description="Prints the notices that have not expired yet, as JSON. Read-only.",
    )
    news_hide = _add_command(
        news_sub,
        "hide",
        help="hide a notice; retained in the database",
        description="Stops showing a notice to visitors but keeps it in the database.",
        examples=("mgarden news hide 3",),
    )
    news_hide.add_argument("id", type=int, metavar="entry-id", help="notice id from `news list`")
    news_delete = _add_command(
        news_sub,
        "delete",
        help="permanently remove a notice; not recoverable (prefer hide)",
        description=(
            "Permanently removes a notice from the database. This cannot be undone; "
            "prefer `news hide`."
        ),
        examples=("mgarden news delete 3",),
    )
    news_delete.add_argument(
        "id", type=int, metavar="entry-id", help="notice id from `news list`"
    )

    requests_parser = _add_command(
        sub,
        "requests",
        help="show recent processed requests",
        description=(
            "Shows what visitors asked recently, newest first. Read-only; exits 1 if the "
            "database is not found and 2 for a bad --limit."
        ),
        examples=("mgarden requests", "mgarden requests --limit 50 --json"),
    )
    requests_parser.add_argument(
        "--limit", type=int, default=20, metavar="N", help="show at most N requests (default: 20)"
    )
    requests_parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of text"
    )

    guestbook_parser = _add_command(
        sub,
        "guestbook",
        help="guestbook moderation",
        description=(
            "Lists, hides, deletes, reviews or approves guestbook entries. Changes the "
            "database only; nothing is transmitted."
        ),
        examples=("mgarden guestbook list --hidden", "mgarden guestbook hide 12"),
    )
    guestbook_sub = guestbook_parser.add_subparsers(
        dest="guestbook_command", required=True, metavar="<action>"
    )
    hide_parser = _add_command(
        guestbook_sub,
        "hide",
        help="hide a guestbook entry",
        description=(
            "Hides an entry from visitors but keeps it in the database. Exits 3 if the "
            "entry is not found."
        ),
        examples=("mgarden guestbook hide 12",),
    )
    hide_parser.add_argument("entry_id", type=int, metavar="entry-id", help="entry id")
    hide_parser.add_argument(
        "--topic", default=None, metavar="TOPIC", help="board the entry belongs to (default: main)"
    )
    delete_parser = _add_command(
        guestbook_sub,
        "delete",
        help="permanently delete a guestbook entry",
        description=(
            "Permanently deletes an entry; this cannot be undone. Exits 3 if the entry "
            "is not found."
        ),
        examples=("mgarden guestbook delete 12",),
    )
    delete_parser.add_argument("entry_id", type=int, metavar="entry-id", help="entry id")
    delete_parser.add_argument(
        "--topic", default=None, metavar="TOPIC", help="board the entry belongs to (default: main)"
    )
    list_parser = _add_command(
        guestbook_sub,
        "list",
        help="list guestbook entries",
        description=(
            "Lists guestbook entries, newest first; visible ones only unless --hidden. "
            "Read-only; exits 1 if the database is not found and 2 for a bad --limit."
        ),
        examples=("mgarden guestbook list", "mgarden guestbook list --hidden --limit 50"),
    )
    list_parser.add_argument(
        "--limit", type=int, default=10, metavar="N", help="show at most N entries (default: 10)"
    )
    list_parser.add_argument("--hidden", action="store_true", help="include hidden entries")
    list_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    list_parser.add_argument(
        "--topic", default=None, metavar="TOPIC", help="list this board only (default: main)"
    )

    block_parser = _add_command(
        sub,
        "block",
        help="block a sender node id",
        description=(
            "Adds a sender to the block list so the daemon ignores it. Exits 2 for an "
            "invalid node id."
        ),
        examples=("mgarden block !a1b2c3d4",),
    )
    block_parser.add_argument("node_id", metavar="node-id", help="node id, e.g. !a1b2c3d4")
    unblock_parser = _add_command(
        sub,
        "unblock",
        help="unblock a sender node id",
        description=(
            "Removes a sender from the block list. Exits 2 for an invalid node id and 3 "
            "if the node was not blocked."
        ),
        examples=("mgarden unblock !a1b2c3d4",),
    )
    unblock_parser.add_argument("node_id", metavar="node-id", help="node id, e.g. !a1b2c3d4")

    _add_command(
        sub,
        "backup",
        help="trigger an immediate database backup",
        description=(
            "Writes a database backup now, instead of waiting for the schedule. Exits 1 "
            "if the backup failed or was refused."
        ),
    )
    _add_command(
        sub,
        "advert",
        help="ask the running MeshCore daemon to send a flood advertisement",
        description=(
            "Asks the running MeshCore daemon to broadcast a flood advertisement, so "
            "this node announces itself. Transmits; run as the service user or with sudo."
        ),
    )

    _add_command(
        sub,
        "notify-test",
        help="send one test alert through the watchdog's ntfy path",
        description=(
            "Sends one test alert through the same ntfy path the watchdog uses, to prove "
            "alerts arrive. Needs MESHGARDEN_NTFY_URL set (exit 2 if not); exits 1 if "
            "delivery failed."
        ),
    )
    _add_command(
        sub,
        "radio-watchdog",
        help="one watchdog poll: alert (and optionally power-cycle) if the radio is down",
        description=(
            "Runs one watchdog poll: alerts, and optionally power-cycles the radio, if "
            "the node has gone deaf. Normally driven by meshgarden-watchdog.timer."
        ),
    )

    books_parser = _add_command(
        sub,
        "books",
        help="book-trivia leaderboard aggregates (all-time)",
        description=(
            "Shows all-time book-trivia totals per book title (shown, guesses, solves). "
            "Read-only; exits 1 if the database is not found."
        ),
        examples=("mgarden books", "mgarden books --json"),
    )
    books_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")

    wordle_parser = _add_command(
        sub,
        "wordle",
        help="wordle leaderboard aggregates (daily)",
        description=(
            "Shows daily Wordle totals, newest first: players, solves and average "
            "guesses. Read-only; exits 1 if the database is not found."
        ),
        examples=("mgarden wordle", "mgarden wordle --days 30"),
    )
    wordle_parser.add_argument(
        "--days", type=int, default=7, metavar="N", help="show the last N days (default: 7)"
    )
    wordle_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")

    help_parser = _add_command(
        sub,
        "help",
        help="show full help, or one command's help",
        description="Prints the full help, or the help of one command.",
        examples=("mgarden help", "mgarden help tail"),
    )
    help_parser.add_argument(
        "topic_command",
        nargs="?",
        metavar="command",
        help="command to describe (default: the top-level help)",
    )

    cli_ops.register_subcommands(sub, guestbook_sub)

    return parser


def _run_help(parser: argparse.ArgumentParser, name: str | None) -> int:
    sub_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    if name is None:
        parser.print_help()
        return 0
    target = sub_action.choices.get(name)
    if target is None:
        valid = ", ".join(sub_action.choices)
        parser.exit(2, f"mgarden: unknown command '{name}'; valid commands: {valid}\n")
    target.print_help()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Exit codes: 0 success, 1 runtime failure, 2 usage/config error,
    3 target not found (e.g. unknown entry id)."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        parser.exit(2)
    if args.command == "help":
        return _run_help(parser, args.topic_command)

    config_path = (
        args.config or os.environ.get("MESHTASTIC_OBSERVATORY_CONFIG") or DEFAULT_CONFIG_PATH
    )
    try:
        cfg = load_config(config_path)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "run":
            return cmd_run(cfg, debug=args.debug)
        if args.command == "status":
            return cmd_status(cfg, as_json=args.json)
        if args.command == "db-stats":
            return cmd_db_stats(cfg)
        if args.command in ("news", "usefulness"):
            if not os.path.isfile(cfg.database.path):
                print("database not found; start the daemon first", file=sys.stderr)
                return 1
            readonly = args.command == "usefulness" or args.news_command == "list"
            conn = connect(
                cfg.database.path,
                read_only=readonly,
                bbs_path=cfg.database.resolved_bbs_path,
                transport=cfg.radio.transport,
            )
            try:
                now = int(time.time())
                if args.command == "usefulness":
                    print(json.dumps(usefulness.report(conn, now), indent=2))
                elif args.news_command == "list":
                    print(json.dumps([dict(r) for r in community.list_news(conn, now)], indent=2))
                elif args.news_command == "add":
                    if not 1 <= args.days <= 90:
                        print("--days must be 1-90", file=sys.stderr)
                        return 2
                    with conn:
                        try:
                            ident = community.add_news(conn, args.text, now, now+args.days*86400)
                        except ValueError as exc:
                            print(str(exc), file=sys.stderr)
                            return 2
                    print(f"NEWS #{ident} published")
                elif args.news_command == "delete":
                    with conn:
                        removed = community.delete_news(conn, args.id)
                    print("notice deleted" if removed else "notice not found")
                    return 0 if removed else 3
                else:
                    with conn:
                        changed = community.hide_news(conn, args.id)
                    print("notice hidden (retained)" if changed else "notice not found")
                    return 0 if changed else 3
                return 0
            finally:
                conn.close()
        if args.command == "requests":
            return cmd_requests(cfg, limit=args.limit, as_json=args.json)
        # --topic (hide/delete/list) is normalized once at the dispatch
        # boundary (item 5): strip/removeprefix('#')/lower, refusing a
        # shape-invalid value here rather than letting it silently match
        # no entry/topic -- same normalizer cli_ops.dispatch uses for
        # review/approve/export. Any other guestbook subcommand (e.g.
        # `review`) falls through to cli_ops.dispatch below unchanged.
        if args.command == "guestbook" and args.guestbook_command in ("hide", "delete", "list"):
            try:
                topic = ops._normalize_cli_topic(args.topic)
            except ValueError:
                print("invalid topic", file=sys.stderr)
                return 2
            if args.guestbook_command == "hide":
                return cmd_guestbook_hide(cfg, args.entry_id, topic=topic)
            if args.guestbook_command == "delete":
                return cmd_guestbook_delete(cfg, args.entry_id, topic=topic)
            return cmd_guestbook_list(
                cfg, limit=args.limit, hidden=args.hidden, as_json=args.json, topic=topic
            )
        if args.command == "block":
            return cmd_block(cfg, args.node_id)
        if args.command == "unblock":
            return cmd_unblock(cfg, args.node_id)
        if args.command == "backup":
            return cmd_backup(cfg)
        if args.command == "advert":
            return cmd_advert(cfg)
        if args.command == "notify-test":
            return cmd_notify_test(cfg)
        if args.command == "radio-watchdog":
            return cmd_radio_watchdog(cfg)
        if args.command == "books":
            return cmd_books(cfg, as_json=args.json)
        if args.command == "wordle":
            return cmd_wordle(cfg, days=args.days, as_json=args.json)
        rc = cli_ops.dispatch(args, cfg)
        if rc is not None:
            return rc
        raise AssertionError(f"unhandled command {args.command!r}")
    except Exception as exc:
        log.exception("CLI command failed: %s", args.command)
        print(f"error: {exc}", file=sys.stderr)
        return 1
