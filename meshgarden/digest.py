"""Daily operator digest: an aggregate-only summary of one local calendar day.

Built entirely from existing tables (observations, nodes, guestbook,
processed_requests, book_stats, wordle_daily) -- no new schema, no daemon
wiring. `build_digest` computes the aggregate dict; `format_digest` renders
it as short human text; `write_digest_file`/`prune_digests` persist it to
`<database dir>/digest/<day>.json` and expire old files. The daemon-side
midnight scheduling that would call these automatically is a separate,
later step -- this module only exposes the functions.

Like every other operator-facing report in this package, a digest is
aggregate-only: it never contains a node id, a node name, or guestbook/DM
message text -- only bounded counts.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from meshgarden.config import Config

KEEP_DAYS = 90

# Fixed, bounded set of terminal-ish request states a digest reports on --
# same states retention.py/rollup_day already treat as meaningful outcomes.
_REPLY_OUTCOMES = ("submitted", "acked", "failed", "expired", "unknown")


def _local_day_window(day: str, tz: str) -> tuple[int, int]:
    """[start, end) unix epoch seconds spanning local calendar day `day`
    (YYYY-MM-DD) in IANA zone `tz`."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ZoneInfo(tz))
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def default_digest_day(now: int, tz: str) -> str:
    """"Yesterday" (YYYY-MM-DD) in local zone `tz` at unix timestamp `now`
    -- the CLI's default when `--date` is omitted."""
    today = datetime.fromtimestamp(now, tz=ZoneInfo(tz)).date()
    return (today - timedelta(days=1)).isoformat()


def _busiest_local_hour(
    conn: sqlite3.Connection, start_ts: int, end_ts: int, tz: str
) -> int | None:
    """The local hour (0-23) with the most packets in [start_ts, end_ts),
    or None if there are none. Ties break toward the earlier hour -- same
    convention as collector.Collector.busiest_hour_since, but bounded above
    too (busiest_hour_since has no upper bound, so it cannot be reused for
    a day that has already ended)."""
    rows = conn.execute(
        "SELECT server_time FROM observations WHERE server_time >= ? AND server_time < ?",
        (start_ts, end_ts),
    ).fetchall()
    if not rows:
        return None
    zone = ZoneInfo(tz)
    counts = Counter(datetime.fromtimestamp(r[0], tz=zone).hour for r in rows)
    best_hour, _ = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return best_hour


def _quota_cooldown_drops(conn: sqlite3.Connection, day: str) -> dict[str, int] | None:
    """Quota/cooldown-specific drop counts, ONLY if daily_stats happens to
    carry columns for them -- as of schema version 4 it does not:
    commands_rejected is a single total, never broken out by reason.
    Returns None (the digest simply omits the field) rather than guessing
    from processed_requests, whose rows for a past day are usually already
    pruned by the time a digest is built. Forward-compatible: a later
    migration adding `quota_drops`/`cooldown_drops` columns to daily_stats
    is picked up automatically, no digest.py change required.

    Note: daily_stats.day is a UTC calendar day while the digest's `day` is
    a LOCAL calendar day -- usually the same, but they can name different
    underlying 24h spans near a timezone boundary. This lookup is a
    best-effort cross-reference, not an exact per-local-day count.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(daily_stats)")}
    wanted = sorted({"quota_drops", "cooldown_drops"} & cols)
    if not wanted:
        return None
    row = conn.execute(
        f"SELECT {', '.join(wanted)} FROM daily_stats WHERE day = ?", (day,)
    ).fetchone()
    if row is None:
        return dict.fromkeys(wanted, 0)
    return {name: row[name] or 0 for name in wanted}


def build_digest(conn: sqlite3.Connection, *, day: str, tz: str, now: int) -> dict:
    """Aggregate-only digest of local calendar day `day` (YYYY-MM-DD, zone
    `tz`). `now` is stamped into the result as `generated_at`; it plays no
    part in which rows are counted (those are bounded entirely by `day`)."""
    start_ts, end_ts = _local_day_window(day, tz)

    obs_row = conn.execute(
        "SELECT COUNT(*) AS raw, "
        "SUM(CASE WHEN is_duplicate = 0 THEN 1 ELSE 0 END) AS unique_packets, "
        "COUNT(DISTINCT sender_node_id) AS distinct_senders "
        "FROM observations WHERE server_time >= ? AND server_time < ?",
        (start_ts, end_ts),
    ).fetchone()

    new_nodes = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE observed_first_seen >= ? AND observed_first_seen < ?",
        (start_ts, end_ts),
    ).fetchone()[0]

    gb_row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN visible = 0 AND moderation_reason IS NOT NULL THEN 1 ELSE 0 END) AS held "
        "FROM guestbook WHERE created_at >= ? AND created_at < ? AND topic IS NULL",
        (start_ts, end_ts),
    ).fetchone()

    reply_rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM processed_requests "
        "WHERE first_seen_at >= ? AND first_seen_at < ? AND state IN (?, ?, ?, ?, ?) "
        "GROUP BY state",
        (start_ts, end_ts, *_REPLY_OUTCOMES),
    ).fetchall()
    replies = dict.fromkeys(_REPLY_OUTCOMES, 0)
    for r in reply_rows:
        replies[r["state"]] = r["n"]

    command_rows = conn.execute(
        "SELECT command, COUNT(*) AS n FROM processed_requests "
        "WHERE first_seen_at >= ? AND first_seen_at < ? AND command IS NOT NULL "
        "GROUP BY command",
        (start_ts, end_ts),
    ).fetchall()
    commands = {r["command"]: r["n"] for r in command_rows}

    book_row = conn.execute(
        "SELECT COALESCE(SUM(solves), 0) AS solves FROM book_stats WHERE day = ?", (day,)
    ).fetchone()
    wordle_row = conn.execute(
        "SELECT solves FROM wordle_daily WHERE day = ?", (day,)
    ).fetchone()

    return {
        "day": day,
        "tz": tz,
        "generated_at": now,
        "packets": {
            "raw": obs_row["raw"] or 0,
            "unique": obs_row["unique_packets"] or 0,
        },
        "distinct_senders": obs_row["distinct_senders"] or 0,
        "new_nodes": new_nodes,
        "busiest_hour": _busiest_local_hour(conn, start_ts, end_ts, tz),
        "guestbook": {
            "signatures": gb_row["total"] or 0,
            "held": gb_row["held"] or 0,
        },
        "replies": replies,
        "commands": commands,
        "drops": _quota_cooldown_drops(conn, day),
        "book_solves": book_row["solves"] or 0,
        "wordle_solves": (wordle_row["solves"] or 0) if wordle_row is not None else 0,
    }


def format_digest(d: dict) -> str:
    """Short multi-line human text, e.g.:

        Meshgarden — 2026-09-14 (Potrero): 118 packets · 81 nodes (6 new) ·
        busiest 20:00 · 3 signatures (1 held) · replies 12 acked / 1 failed /
        0 expired / 0 unknown / 0 submitted
        Commands: SIGN 5 · GUESTBOOK 3
        Games: 2 book solves · 3 wordle solves

    `d["place"]` (optional; the CLI fills it in from `[service]
    haiku_place` before calling this) is shown in parens next to the day
    when non-empty; omitted entirely otherwise. Never includes a node id or
    message text -- only the aggregate fields `build_digest` produces.
    """
    place = d.get("place") or ""
    place_suffix = f" ({place})" if place else ""
    hour = d["busiest_hour"]
    hour_text = f"{hour:02d}:00" if hour is not None else "n/a"
    replies = d["replies"]

    lines = [
        f"Meshgarden — {d['day']}{place_suffix}: "
        f"{d['packets']['unique']} packets · {d['distinct_senders']} nodes "
        f"({d['new_nodes']} new) · busiest {hour_text} · "
        f"{d['guestbook']['signatures']} signatures ({d['guestbook']['held']} held) · "
        f"replies {replies['acked']} acked / {replies['failed']} failed / "
        f"{replies['expired']} expired / {replies['unknown']} unknown / "
        f"{replies['submitted']} submitted"
    ]

    if d["commands"]:
        commands_text = " · ".join(f"{cmd} {n}" for cmd, n in sorted(d["commands"].items()))
        lines.append(f"Commands: {commands_text}")

    drops = d.get("drops")
    if drops:
        drops_text = " · ".join(f"{name} {n}" for name, n in sorted(drops.items()))
        lines.append(f"Drops: {drops_text}")

    if d["book_solves"] or d["wordle_solves"]:
        lines.append(f"Games: {d['book_solves']} book solves · {d['wordle_solves']} wordle solves")

    return "\n".join(lines)


def _digest_dir(cfg: Config) -> str:
    db_dir = os.path.dirname(os.path.abspath(cfg.database.path))
    return os.path.join(db_dir, "digest")


def write_digest_file(cfg: Config, d: dict) -> str:
    """Writes `d` as JSON to `<database dir>/digest/<day>.json`, dir mode
    0o700, file mode 0o600 -- same owner-only convention as the database
    file and backups. Writes to a same-directory .tmp file and os.replace's
    it into place, so a reader never observes a partial file. Returns the
    final path."""
    digest_dir = _digest_dir(cfg)
    is_new_dir = not os.path.isdir(digest_dir)
    os.makedirs(digest_dir, exist_ok=True)
    if is_new_dir:
        os.chmod(digest_dir, 0o700)

    dest = os.path.join(digest_dir, f"{d['day']}.json")
    tmp_dest = dest + ".tmp"
    with open(tmp_dest, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.chmod(tmp_dest, 0o600)
    os.replace(tmp_dest, dest)
    return dest


def prune_digests(cfg: Config, *, today: str, keep_days: int = KEEP_DAYS) -> int:
    """Deletes digest/<day>.json files whose `day` is more than `keep_days`
    before `today` (both "YYYY-MM-DD"). Non-conforming filenames in the
    digest directory are left alone. Returns the number of files deleted."""
    digest_dir = _digest_dir(cfg)
    try:
        names = os.listdir(digest_dir)
    except FileNotFoundError:
        return 0

    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=keep_days)).strftime(
        "%Y-%m-%d"
    )
    deleted = 0
    for name in names:
        if not name.endswith(".json"):
            continue
        day = name[: -len(".json")]
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            continue
        if day < cutoff:
            os.remove(os.path.join(digest_dir, name))
            deleted += 1
    return deleted
