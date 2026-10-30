"""Small, privacy-conscious product measurements and the ``CHECK`` reply.

The product event table deliberately contains no node ids, command arguments,
packet text, topics, mail recipients, or message bodies.  A random per-database
key turns a node id into a stable pseudonym so seven-day return and funnel
counts are possible without retaining the id itself.  Keeping that key in the
same database is pseudonymisation, not anonymity: it protects an exported
event table on its own, but an operator with the whole database can still test
candidate node ids.  Rows are therefore also time-bounded to 90 days.

``processed_requests`` remains the source of truth for the request outcome
when it is present.  Its roughly 24-hour lifetime is too short for product
trends, so :func:`record` copies only its bounded command/state classification,
never any visitor-supplied text.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import statistics
from collections import Counter, defaultdict
from typing import Any

from meshgarden import bbs_ids

DAY_SECONDS = 86_400
RETENTION_SECONDS = 90 * DAY_SECONDS
RETURN_WINDOW_SECONDS = 7 * DAY_SECONDS
VISITORS_RESET_KEY = "usefulness_visitors_reset_at"

# Kept standalone so database.py can install it as one root migration.
SCHEMA_SQL = """
CREATE TABLE usefulness_meta (
  key   TEXT PRIMARY KEY,
  value BLOB NOT NULL
);
INSERT OR IGNORE INTO usefulness_meta(key, value)
VALUES ('identity_hmac_key', randomblob(32));

CREATE TABLE usefulness_events (
  event_key   TEXT PRIMARY KEY,       -- HMAC of request identity; no raw packet/node id
  user_key    TEXT NOT NULL,          -- HMAC of node id, stable only inside this database
  occurred_at INTEGER NOT NULL,       -- UTC epoch seconds
  command     TEXT NOT NULL CHECK(length(command) BETWEEN 1 AND 32),
  outcome     TEXT NOT NULL CHECK(outcome IN ('accepted', 'error', 'unknown'))
);
CREATE INDEX idx_usefulness_events_time ON usefulness_events(occurred_at);
CREATE INDEX idx_usefulness_events_user_time
  ON usefulness_events(user_key, occurred_at);

CREATE TABLE usefulness_daily_counters (
  day   INTEGER NOT NULL,             -- start of UTC day, epoch seconds
  name  TEXT NOT NULL CHECK(name IN (
    'MAIL_STORED', 'MAIL_COLLECTED', 'UNKNOWN_REQUESTS'
  )),
  count INTEGER NOT NULL CHECK(count >= 0),
  PRIMARY KEY(day, name)
);
CREATE INDEX idx_usefulness_daily_counters_day
  ON usefulness_daily_counters(day);
"""


# Explicitly enumerated so an unrecognised visitor word can never become a
# durable string in this table.  NEW and CHECK are included for the onboarding
# and comparative-radio additions that use this module.
_COMMANDS = frozenset(
    {
        "MENU", "NOT_EMERGENCY", "INFO", "SIGN", "GUESTBOOK", "STATS",
        "ICHING", "HAIKU", "BOOK", "WORDLE", "GARDEN", "RECORD",
        "WEEK", "UNSIGN", "SEEN", "WHO", "ME", "PING", "PATH", "MOON",
        "SUN", "POST", "READ", "BOARDS", "MAIL",
        "LAB_UNLOCK", "CMD_HELP", "VERSION", "KNOCK", "NEW", "NEWS",
        "GUIDE", "REPLY", "SUGGEST", "CHECK", "UNKNOWN", "ERROR",
    }
)
_NON_USEFUL = frozenset(
    {
        "MENU", "NEW", "UNKNOWN", "ERROR", "CMD_HELP", "NOT_EMERGENCY",
        "KNOCK", "SUGGEST",
    }
)
_ERROR_STATES = frozenset({"dropped", "failed", "expired"})
_ACCEPTED_STATES = frozenset({"received", "queued", "sending", "submitted", "acked"})
_COUNTERS = frozenset({"MAIL_STORED", "MAIL_COLLECTED", "UNKNOWN_REQUESTS"})


def _bounded_command(command: object) -> str:
    value = getattr(command, "value", command)
    if not isinstance(value, str):
        return "UNKNOWN"
    value = value.upper()
    return value if value in _COMMANDS else "UNKNOWN"


def _outcome(state: str | None, command: str) -> str:
    if command == "ERROR" or state in _ERROR_STATES:
        return "error"
    if command == "UNKNOWN" or state == "unknown":
        return "unknown"
    # UNKNOWN and ERROR are explicit instrumentation events.  Any other
    # command recorded without a processed_requests row reached the product
    # recorder and is an accepted use; this also keeps the module useful in
    # small standalone deployments that do not install the request log.
    if state is None:
        return "accepted"
    if state in _ACCEPTED_STATES:
        return "accepted"
    return "unknown"


def _key(conn: sqlite3.Connection) -> bytes:
    row = conn.execute(
        "SELECT value FROM usefulness_meta WHERE key='identity_hmac_key'"
    ).fetchone()
    if row is None:
        # Defensive for databases whose migration was interrupted between
        # table creation and seed insertion.  SQLite supplies cryptographic
        # random bytes without introducing a process-global secret.
        conn.execute(
            "INSERT INTO usefulness_meta(key, value) "
            "VALUES ('identity_hmac_key', randomblob(32))"
        )
        row = conn.execute(
            "SELECT value FROM usefulness_meta WHERE key='identity_hmac_key'"
        ).fetchone()
    return bytes(row[0])


def _digest(key: bytes, purpose: str, value: str) -> str:
    data = f"{purpose}\0{value}".encode("utf-8", "strict")
    return hmac.new(key, data, hashlib.sha256).hexdigest()[:32]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _recent_request(
    conn: sqlite3.Connection, node_id: str, command: str, now: int
) -> tuple[int, int, str] | None:
    if not _has_table(conn, "processed_requests"):
        return None
    row = conn.execute(
        "SELECT packet_id, first_seen_at, state FROM processed_requests "
        "WHERE sender_node_id=? AND command=? AND first_seen_at<=? "
        "ORDER BY first_seen_at DESC, rowid DESC LIMIT 1",
        (node_id, command, now),
    ).fetchone()
    return None if row is None else (int(row[0]), int(row[1]), str(row[2]))


def record(
    conn: sqlite3.Connection, node_id: str, command: object, now: int
) -> None:
    """Record one command use without retaining its node id or arguments.

    Calling this twice for the same ``processed_requests`` row is idempotent.
    If no request row exists (notably UNKNOWN/ERROR instrumentation), a
    same-node, same-command, same-second call is treated as the same event.
    """

    command_name = _bounded_command(command)
    timestamp = int(now)
    with conn:
        secret = _key(conn)
        user_key = _digest(secret, "user", str(node_id))
        request = _recent_request(conn, str(node_id), command_name, timestamp)
        if request is None:
            event_value = f"{node_id}\0{command_name}\0{timestamp}"
            event_key = _digest(secret, "event", event_value)
            state = None
        else:
            packet_id, first_seen_at, state = request
            event_key = _digest(
                secret, "request", f"{node_id}\0{packet_id}\0{first_seen_at}"
            )
        conn.execute(
            "INSERT INTO usefulness_events"
            "(event_key,user_key,occurred_at,command,outcome) VALUES (?,?,?,?,?) "
            "ON CONFLICT(event_key) DO UPDATE SET "
            "command=excluded.command, outcome=excluded.outcome "
            "WHERE usefulness_events.command IS NOT excluded.command "
            "OR usefulness_events.outcome IS NOT excluded.outcome",
            (event_key, user_key, timestamp, command_name, _outcome(state, command_name)),
        )


def _processed_overlay(
    conn: sqlite3.Connection, now: int, secret: bytes
) -> dict[str, tuple[str, int, str, str]]:
    """Return recent request-log events keyed like usefulness_events.

    This makes reports reflect a state that advanced after ``record`` (for
    example queued -> failed) while the request row still exists.  Rows absent
    from the durable table are also usable for the current report; regular
    wiring still calls ``record`` because processed_requests expires quickly.
    """

    if not _has_table(conn, "processed_requests"):
        return {}
    rows = conn.execute(
        "SELECT sender_node_id,packet_id,first_seen_at,command,state "
        "FROM processed_requests WHERE first_seen_at>=? AND first_seen_at<=?",
        (now - RETENTION_SECONDS, now),
    ).fetchall()
    result: dict[str, tuple[str, int, str, str]] = {}
    for node_id, packet_id, occurred_at, raw_command, state in rows:
        command = _bounded_command(raw_command)
        # KNOCK is request-log bookkeeping for an unauthenticated channel
        # trigger, not a command sent by the resolved recipient.  Treating it
        # as that person's product use would invent a user and a return visit.
        if command == "KNOCK":
            continue
        user_key = _digest(secret, "user", str(node_id))
        event_key = _digest(
            secret, "request", f"{node_id}\0{packet_id}\0{occurred_at}"
        )
        result[event_key] = (
            user_key, int(occurred_at), command, _outcome(str(state), command)
        )
    return result


def sync(conn: sqlite3.Connection, now: int) -> int:
    """Persist current request-log outcomes before its 24-hour pruning.

    Returns the number of recent request rows considered.  The event key makes
    repeated maintenance passes idempotent while allowing a later terminal
    state to replace an earlier queued snapshot.
    """

    timestamp = int(now)
    with conn:
        secret = _key(conn)
        overlay = _processed_overlay(conn, timestamp, secret)
        for event_key, (user_key, occurred_at, command, outcome) in overlay.items():
            conn.execute(
                "INSERT INTO usefulness_events"
                "(event_key,user_key,occurred_at,command,outcome) VALUES (?,?,?,?,?) "
                "ON CONFLICT(event_key) DO UPDATE SET "
                "command=excluded.command, outcome=excluded.outcome "
                "WHERE usefulness_events.command IS NOT excluded.command "
                "OR usefulness_events.outcome IS NOT excluded.outcome",
                (event_key, user_key, occurred_at, command, outcome),
            )
        return len(overlay)


def counter(
    conn: sqlite3.Connection, name: str, count: int, now: int
) -> None:
    """Increment one bounded, identity-free daily product counter."""

    metric = str(name).upper()
    if metric not in _COUNTERS:
        raise ValueError(f"unknown usefulness counter: {name!r}")
    if type(count) is not int or count < 0:
        raise ValueError("counter increment must be a non-negative integer")
    if count == 0:
        return
    day = (int(now) // DAY_SECONDS) * DAY_SECONDS
    with conn:
        conn.execute(
            "INSERT INTO usefulness_daily_counters(day,name,count) VALUES (?,?,?) "
            "ON CONFLICT(day,name) DO UPDATE SET count=count+excluded.count",
            (day, metric, count),
        )


def _funnel(
    events: list[tuple[str, int, str, str]], start: str, targets: frozenset[str] | None
) -> dict[str, int | float | None]:
    starts: dict[str, int] = {}
    target_times: dict[str, list[int]] = defaultdict(list)
    for user_key, at, command, outcome in events:
        if outcome != "accepted":
            continue
        if command == start:
            starts[user_key] = min(starts.get(user_key, at), at)
        if targets is None:
            if command not in _NON_USEFUL:
                target_times[user_key].append(at)
        elif command in targets:
            target_times[user_key].append(at)
    converted = sum(
        1
        for user_key, start_at in starts.items()
        if any(target_at >= start_at for target_at in target_times.get(user_key, ()))
    )
    started = len(starts)
    return {
        "started": started,
        "converted": converted,
        "rate": (converted / started) if started else None,
    }


def report(conn: sqlite3.Connection, now: int) -> dict[str, Any]:
    """Return aggregate 90-day usefulness measurements.

    The result contains counts only.  User pseudonyms never leave this
    function, and no per-node Prometheus-style labels are produced.
    """

    timestamp = int(now)
    cutoff = timestamp - RETENTION_SECONDS
    counter_cutoff_day = (cutoff // DAY_SECONDS) * DAY_SECONDS
    secret = _key(conn)
    rows = conn.execute(
        "SELECT event_key,user_key,occurred_at,command,outcome "
        "FROM usefulness_events WHERE occurred_at>=? AND occurred_at<=?",
        (cutoff, timestamp),
    ).fetchall()
    merged: dict[str, tuple[str, int, str, str]] = {
        str(event_key): (str(user_key), int(at), str(command), str(outcome))
        for event_key, user_key, at, command, outcome in rows
    }
    merged.update(_processed_overlay(conn, timestamp, secret))
    events = list(merged.values())

    commands = Counter(event[2] for event in events)
    outcomes = Counter(event[3] for event in events)
    command_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    for _, _, command, outcome in events:
        command_outcomes[command][outcome] += 1

    recent_cutoff = timestamp - RETURN_WINDOW_SECONDS
    active_days: dict[str, set[int]] = defaultdict(set)
    for user_key, occurred_at, _, _ in events:
        if occurred_at >= recent_cutoff:
            active_days[user_key].add(occurred_at // DAY_SECONDS)
    users_7d = len(active_days)
    returning = sum(len(days) >= 2 for days in active_days.values())
    counters = Counter(
        {
            str(name): int(count)
            for name, count in conn.execute(
                "SELECT name,SUM(count) FROM usefulness_daily_counters "
                "WHERE day>=? AND day<=? GROUP BY name",
                (counter_cutoff_day, timestamp),
            ).fetchall()
        }
    )

    return {
        "window_days": 90,
        "events": len(events),
        "unique_users": len({event[0] for event in events}),
        "commands": dict(sorted(commands.items())),
        "outcomes": dict(sorted(outcomes.items())),
        "command_outcomes": {
            command: dict(sorted(counts.items()))
            for command, counts in sorted(command_outcomes.items())
        },
        "users_7d": users_7d,
        "returning_users_7d": returning,
        "returning_rate_7d": (returning / users_7d) if users_7d else None,
        "menu_to_useful": _funnel(events, "MENU", None),
        # These are unambiguous public/board reads.  MAIL ON, sends, and
        # inbox reads share one command classification, so generic MAIL use
        # would inflate this funnel.  Successful inbox collection has the
        # separate MAIL_COLLECTED counter.
        "new_to_read": _funnel(
            events, "NEW", frozenset({"GUESTBOOK", "NEWS", "READ"})
        ),
        "read_types": {
            "guestbook": commands["GUESTBOOK"],
            "news": commands["NEWS"],
            "board": commands["READ"],
            "mail_collected": counters["MAIL_COLLECTED"],
        },
        "boards": {
            "reads": commands["READ"],
            "posts": commands["POST"],
            "lists": commands["BOARDS"],
            "mail": commands["MAIL"],
            "mail_stored": counters["MAIL_STORED"],
            "mail_collected": counters["MAIL_COLLECTED"],
        },
        "counters": dict(sorted(counters.items())),
        "unknown": commands["UNKNOWN"] + counters["UNKNOWN_REQUESTS"],
        "unknown_requests": counters["UNKNOWN_REQUESTS"],
        "unknown_events": commands["UNKNOWN"],
        "errors": outcomes["error"],
    }


def _operator_keys(conn: sqlite3.Connection, secret: bytes) -> tuple[set[str], int]:
    """Pseudonyms of LAB-unlocked nodes, i.e. the operator's own radios.

    ``lab_nodes`` may live in the attached shared BBS file, where MeshCore ids
    carry an ``mc:`` prefix that :func:`record` never sees, so both the stored
    and the bare form are digested.
    """

    try:
        rows = conn.execute("SELECT node_id FROM lab_nodes").fetchall()
    except sqlite3.OperationalError:
        return set(), 0
    keys: set[str] = set()
    for (stored,) in rows:
        for node_id in {str(stored), bbs_ids.split(str(stored))[1]}:
            keys.add(_digest(secret, "user", node_id))
    return keys, len(rows)


def _reset_at(conn: sqlite3.Connection) -> int | None:
    if not _has_table(conn, "daemon_state"):
        return None
    row = conn.execute(
        "SELECT value FROM daemon_state WHERE key=?", (VISITORS_RESET_KEY,)
    ).fetchone()
    return int(row[0]) if row is not None else None


def visitors(conn: sqlite3.Connection, now: int) -> dict[str, Any]:
    """Messages from everyone except the operator, since the last reset.

    Read-only, so the web page's read-only connection can call it.  The
    window never reaches past the 90-day event retention.
    """

    timestamp = int(now)
    reset_at = _reset_at(conn)
    since = max(timestamp - RETENTION_SECONDS, reset_at or 0)
    row = conn.execute(
        "SELECT value FROM usefulness_meta WHERE key='identity_hmac_key'"
    ).fetchone()
    events: list[tuple[str, int, str]] = []
    own_nodes = 0
    if row is not None:
        own, own_nodes = _operator_keys(conn, bytes(row[0]))
        events = [
            (str(user_key), int(at), str(command))
            for user_key, at, command in conn.execute(
                "SELECT user_key,occurred_at,command FROM usefulness_events "
                "WHERE occurred_at>=? AND occurred_at<=?",
                (since, timestamp),
            )
            if str(user_key) not in own
        ]
    recent_cutoff = timestamp - RETURN_WINDOW_SECONDS
    return {
        "since": since,
        "reset_at": reset_at,
        "messages": len(events),
        "senders": len({event[0] for event in events}),
        "senders_7d": len({event[0] for event in events if event[1] >= recent_cutoff}),
        "commands": sum(event[2] not in ("UNKNOWN", "ERROR") for event in events),
        "guestbook_signs": sum(event[2] == "SIGN" for event in events),
        "last_at": max((event[1] for event in events), default=None),
        "own_nodes_excluded": own_nodes,
    }


def reset_visitors(conn: sqlite3.Connection, now: int) -> None:
    """Start the visitor count again from ``now``; events are kept."""

    timestamp = int(now)
    with conn:
        conn.execute(
            "INSERT INTO daemon_state(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (VISITORS_RESET_KEY, str(timestamp), timestamp),
        )


def prune(conn: sqlite3.Connection, now: int) -> int:
    """Delete product events older than 90 days and return the row count."""

    with conn:
        cur_events = conn.execute(
            "DELETE FROM usefulness_events WHERE occurred_at < ?",
            (int(now) - RETENTION_SECONDS,),
        )
        cutoff_day = ((int(now) - RETENTION_SECONDS) // DAY_SECONDS) * DAY_SECONDS
        cur_counters = conn.execute(
            "DELETE FROM usefulness_daily_counters WHERE day < ?",
            (cutoff_day,),
        )
        return cur_events.rowcount + cur_counters.rowcount


def _obs_value(obs: object, *names: str) -> object | None:
    for name in names:
        if hasattr(obs, name):
            value = getattr(obs, name)
            if value is not None:
                return value
    return None


def _obs_hops(obs: object) -> int | None:
    value = _obs_value(obs, "hops_away", "hops", "path_len")
    if type(value) is int and 0 <= value < 255:
        return value
    start = _obs_value(obs, "hop_start")
    limit = _obs_value(obs, "hop_limit")
    if type(start) is int and type(limit) is int and start >= limit:
        return start - limit
    return None


def _snr(value: object | None) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{float(value):+.1f}"


def _age(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _truncate_utf8(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")[: max(0, int(max_bytes))]
    while data:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            data = data[:-1]
    return ""


def _fit_lines(lines: list[str], footer: str, max_bytes: int) -> str:
    """Keep the footer and include only complete detail lines that fit."""

    budget = max(0, int(max_bytes))
    if len(footer.encode("utf-8")) > budget:
        return _truncate_utf8(footer, budget)
    kept: list[str] = []
    used = len(footer.encode("utf-8"))
    for line in lines:
        line_bytes = len(line.encode("utf-8"))
        if used + line_bytes + 1 <= budget:
            kept.append(line)
            used += line_bytes + 1
    return "\n".join([*kept, footer])


def check_text(
    conn: sqlite3.Connection,
    node_id: str,
    obs: object,
    now: int,
    max_bytes: int,
) -> str:
    """Build a comparative receive-side radio check for one node.

    Signal samples are unique, locally received RF observations from the last
    24 hours.  A direct reception requires recorded ``hops_away == 0``; an
    unknown hop count is never promoted to direct.  The wording explicitly
    limits SNR to the final RF link and makes no end-to-end claim.
    """

    cutoff = int(now) - DAY_SECONDS
    # MeshCore has no via_mqtt flag, while Meshtastic proves a local RF
    # reception with via_mqtt=0.  NULL from old Meshtastic rows is unknown and
    # excluded rather than silently treated as RF.
    rf_clause = "(transport='meshcore' OR (transport='meshtastic' AND via_mqtt=0))"
    samples = [
        float(row[0])
        for row in conn.execute(
            "SELECT rx_snr FROM observations WHERE sender_node_id=? "
            "AND server_time>=? AND server_time<=? AND is_duplicate=0 "
            "AND rx_snr IS NOT NULL AND " + rf_clause + " ORDER BY server_time",
            (str(node_id), cutoff, int(now)),
        ).fetchall()
    ]
    direct = conn.execute(
        "SELECT server_time,rx_snr FROM observations WHERE sender_node_id=? "
        "AND server_time>=? AND server_time<=? AND is_duplicate=0 "
        "AND hops_away=0 AND " + rf_clause + " ORDER BY server_time DESC LIMIT 1",
        (str(node_id), cutoff, int(now)),
    ).fetchone()

    current_snr = _obs_value(obs, "rx_snr", "snr")
    hops = _obs_hops(obs)
    route = "hops ?" if hops is None else ("direct" if hops == 0 else f"{hops} hops")
    lines = [f"📡 SNR {_snr(current_snr)} dB · {route}"]
    if samples:
        lines.append(
            f"24h med {_snr(statistics.median(samples))} · best {_snr(max(samples))} "
            f"({len(samples)} rx)"
        )
    else:
        lines.append("24h SNR n/a · 0 rx")
    if direct is None:
        lines.append("Direct: none in 24h")
    else:
        direct_snr = "" if direct[1] is None else f" {_snr(direct[1])} dB"
        lines.append(f"Direct:{direct_snr} {_age(int(now) - int(direct[0]))} ago")
    return _fit_lines(lines, "SNR=last RF link", max_bytes)
