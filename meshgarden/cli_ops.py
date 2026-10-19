"""Operator CLI subcommands built on top of `ops.py`'s pure helpers.

Kept out of main.py (beyond one registration call and one dispatch call) so
main.py's own daemon-dispatch surface can keep changing elsewhere without
conflicting with this module. Every subcommand here is local-only: none of
them ever construct a `RadioAdapter`, and all but `restore` are read-only.

`register_subcommands` wires up argparse; `dispatch` maps a parsed
`argparse.Namespace` to the matching `cmd_*` function, returning `None` when
`args.command` isn't one of this module's own subcommands (so main.py's
dispatch can fall through to its own fallthrough/assertion unchanged).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import textwrap
import threading
import time
import tomllib
from collections import deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from meshgarden import bbs_ids, digest, ops, positions
from meshgarden.fortune import local_iso
from meshgarden.guestbook import (
    clean_display_name,
    fallback_display_name,
    lookup_bbs_node_name,
    lookup_node_name,
)

if TYPE_CHECKING:
    from meshgarden.config import Config

_MIN_LIST_LIMIT = 1
_MAX_LIST_LIMIT = 1000


def bbs_node_argument(cfg: Config, text: str) -> str | None:
    """A node id argument for a shared BBS table, in its stored form: an
    `mc:`/`mt:` prefix names the mesh, a bare id means this config's own."""
    return bbs_ids.typed_node_id(
        text, cfg.radio.transport, shared=cfg.database.resolved_bbs_path is not None
    )


def _node_label(conn, node_id: str) -> str:
    """Human label retaining the stable node id for operator decisions."""
    name = lookup_node_name(conn, node_id)
    return f"{name} ({node_id})" if name else node_id


def _bbs_node_label(conn, stored: str) -> str:
    """`_node_label` for an id stored in a BBS table (mail, aliases, LAB)."""
    name = lookup_bbs_node_name(conn, stored)
    return f"{name} ({stored})" if name else stored


def _quota_node_label(conn, node_id: str) -> str:
    """Quota reports prefer the full known name while retaining the stable id."""
    row = conn.execute(
        "SELECT long_name, short_name FROM nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    name = clean_display_name(row["long_name"], row["short_name"]) if row else None
    return f"{name} ({node_id})" if name else node_id


def _resolve_quota_node(conn, identifier: str) -> tuple[str | None, tuple[str, ...]]:
    """Resolve a quota target by canonical id or exact known short/long name.

    Names are case-insensitive and must identify exactly one node. The second
    tuple contains every matching id when a name is ambiguous.
    """
    from meshgarden.config import parse_node_id_argument

    parsed = parse_node_id_argument(identifier)
    if parsed is not None:
        return parsed, ()
    text = identifier.strip()
    rows = conn.execute(
        "SELECT DISTINCT node_id FROM nodes "
        "WHERE (short_name IS NOT NULL AND lower(short_name)=lower(?)) "
        "OR (long_name IS NOT NULL AND lower(long_name)=lower(?)) "
        "ORDER BY node_id",
        (text, text),
    ).fetchall()
    matches = tuple(row["node_id"] for row in rows)
    return (matches[0], ()) if len(matches) == 1 else (None, matches)


def _report_quota_node_error(identifier: str, matches: tuple[str, ...]) -> int:
    if matches:
        print(
            f"ambiguous node name {identifier!r}; use one of: {', '.join(matches)}",
            file=sys.stderr,
        )
    else:
        print(f"unknown node id or exact name: {identifier!r}", file=sys.stderr)
    return 2

# Duplicated from main.py's own DEFAULT_CONFIG_PATH -- same reasoning as
# _connect_readonly_or_report above: this module never imports main.py
# (main.py is the one importing it), so a tiny constant is copied rather
# than shared.
_DEFAULT_CONFIG_PATH = "/etc/meshtastic-observatory/config.toml"


def _invalid_limit(limit: int) -> bool:
    return not (_MIN_LIST_LIMIT <= limit <= _MAX_LIST_LIMIT)


def _connect_readonly_or_report(cfg: Config):
    """Same convention as main.py's own helper of the same name: a writable
    `connect()` would silently create a fresh, empty, migrated database (and
    its parent directories) when the configured path is wrong. Duplicated
    (rather than imported from main) so this module never imports main.py --
    main.py is the one importing this module."""
    import sqlite3

    from meshgarden.database import connect

    try:
        return connect(cfg.database.path, read_only=True, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    except sqlite3.OperationalError:
        print(f"error: database not found: {cfg.database.path}", file=sys.stderr)
        return None


# =============================================================================
# tail
# =============================================================================


def cmd_tail(
    cfg: Config,
    *,
    from_start: bool = False,
    lines: int = 0,
    stop: threading.Event | None = None,
    poll_seconds: float = 0.5,
) -> int:
    """Follows `ops.exchange_trace_path(cfg)`. `lines` > 0 first prints the
    last `lines` existing lines (like `tail -n N -f`) before following new
    ones; `from_start` prints the whole file instead. `stop` and
    `poll_seconds` are TEST SUPPORT ONLY -- production always omits `stop`,
    relying on Ctrl-C (KeyboardInterrupt) instead."""
    path = ops.exchange_trace_path(cfg)
    if not os.path.exists(path):
        print(
            f"error: exchange trace not found: {path}\n"
            "(this file is written only while the daemon is running with "
            "`run --debug`; start it that way first)",
            file=sys.stderr,
        )
        return 1

    if lines > 0 and not from_start:
        # The trace is small (one line per DM) so a bounded deque over the
        # whole file is simpler than seeking backwards by block, and it
        # stays correct for multi-byte UTF-8 text.
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in deque((ln.rstrip("\n") for ln in f), maxlen=lines):
                print(line)

    if stop is None:
        stop = threading.Event()
    try:
        for line in ops.follow_file(
            path, from_end=not from_start, poll_seconds=poll_seconds, stop=stop
        ):
            print(line)
    except KeyboardInterrupt:
        stop.set()
    return 0


# =============================================================================
# export
# =============================================================================


def cmd_export(
    cfg: Config,
    *,
    fmt: str = "csv",
    include_hidden: bool = False,
    out_path: str | None = None,
    topic: str | None = None,
) -> int:
    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        data = ops.export_guestbook(conn, fmt, include_hidden=include_hidden, topic=topic)
    finally:
        conn.close()

    if not data.endswith("\n"):
        data += "\n"

    if out_path:
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            f.write(data)
    else:
        sys.stdout.write(data)
    return 0


# =============================================================================
# restore
# =============================================================================


def cmd_restore(
    cfg: Config, backup_path: str, *, overwrite: bool = False, yes: bool = False
) -> int:
    # A bbs-*.db backup goes to the shared file, anything else to main.
    target = cfg.database.path
    if ops.backup_kind(backup_path) == "bbs":
        if cfg.database.resolved_bbs_path is None:
            print(
                "error: this is a shared BBS backup but database.bbs_path is not configured",
                file=sys.stderr,
            )
            return 1
        target = cfg.database.resolved_bbs_path
    if os.path.exists(target) and not yes:
        print(
            f"error: target database already exists: {target} (use --yes to confirm restore)",
            file=sys.stderr,
        )
        return 1

    # Once the operator has confirmed overwriting the target (--yes), actually
    # let restore_backup overwrite it -- otherwise it refuses again below.
    report = ops.restore_backup(
        backup_path, target, overwrite=overwrite or yes, bbs_path=cfg.database.resolved_bbs_path
    )
    for key, value in asdict(report).items():
        print(f"{key}: {value}")
    return 0 if report.ok else 1


# =============================================================================
# doctor
# =============================================================================


def cmd_doctor(cfg: Config, *, as_json: bool = False) -> int:
    checks = ops.doctor(cfg, now=int(time.time()))
    all_ok = all(c.ok for c in checks)
    if as_json:
        print(json.dumps([asdict(c) for c in checks]))
    else:
        for c in checks:
            status = "OK" if c.ok else "FAIL"
            print(f"{status} {c.name}: {c.detail}")
    return 0 if all_ok else 1


# =============================================================================
# guestbook review
# =============================================================================


def cmd_guestbook_review(cfg: Config, *, limit: int = 10, topic: str | None = None) -> int:
    """Hidden entries that carry a moderation_reason (i.e. auto-moderated by
    the SIGN banned-word guard, not merely operator-hidden), newest first.
    `topic` filters to that topic; omitted (None) means the main guestbook
    only, never "all topics" (ruling 9). Read-only; never constructs a
    RadioAdapter."""
    if _invalid_limit(limit):
        print(
            f"error: --limit must be between {_MIN_LIST_LIMIT} and {_MAX_LIST_LIMIT} "
            f"(got {limit})",
            file=sys.stderr,
        )
        return 2

    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        rows = conn.execute(
            "SELECT entry_id, created_at, sender_node_id, display_name, message, "
            "moderation_reason, nodes.short_name AS current_short_name, "
            "nodes.long_name AS current_long_name FROM guestbook "
            "LEFT JOIN nodes ON nodes.node_id=bbs_local_id(guestbook.sender_node_id) "
            "WHERE visible=0 AND moderation_reason IS NOT NULL AND topic IS ? "
            "ORDER BY entry_id DESC LIMIT ?",
            (topic, limit),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        display_name = clean_display_name(
            row["current_short_name"], row["current_long_name"], row["display_name"]
        ) or fallback_display_name(row["sender_node_id"])
        when = local_iso(row["created_at"], cfg.service.timezone)
        print(
            f"#{row['entry_id']} {when}  {row['sender_node_id']}  "
            f"{display_name}: {row['message']}  [{row['moderation_reason']}]"
        )
    return 0


# =============================================================================
# digest
# =============================================================================


def cmd_digest(
    cfg: Config, *, date: str | None = None, as_json: bool = False, now: int | None = None
) -> int:
    """Aggregate-only digest of one local calendar day (default: yesterday
    in `[service] timezone`). Local-only, read-only -- never constructs a
    RadioAdapter. `now` is TEST SUPPORT ONLY; production always omits it,
    using the real wall clock."""
    if now is None:
        now = int(time.time())
    tz = cfg.service.timezone
    day = date if date is not None else digest.default_digest_day(now, tz)
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        print(f"error: --date must be YYYY-MM-DD (got {date!r})", file=sys.stderr)
        return 2

    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        d = digest.build_digest(conn, day=day, tz=tz, now=now)
    finally:
        conn.close()

    if as_json:
        print(json.dumps(d))
    else:
        d = dict(d)
        d["place"] = cfg.service.haiku_place
        print(digest.format_digest(d))
    return 0


# =============================================================================
# guestbook approve
# =============================================================================


def cmd_guestbook_approve(cfg: Config, entry_id: int, *, topic: str | None = None) -> int:
    """Approves a hidden entry auto-moderated by the SIGN banned-word guard
    (has a moderation_reason): visible=1, moderation_reason cleared. An
    entry that does not exist, is already visible, or was hidden directly
    by an operator (`guestbook hide`, no moderation_reason) is left exactly
    as it is and reported as such -- exit 3 in every one of those cases.

    `topic` is a guard (ruling 9): `entry_id` must belong to that topic (or,
    if omitted, must be a main-guestbook entry) or the command refuses with
    an error and does nothing, checked before the approve UPDATE."""
    from meshgarden.database import connect, migrate
    from meshgarden.guestbook import Guestbook

    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    try:
        migrate(conn, transport=cfg.radio.transport, salt=cfg.service.fortune_salt)
        row = conn.execute(
            "SELECT visible, moderation_reason, topic FROM guestbook WHERE entry_id=?", (entry_id,)
        ).fetchone()
        if row is None:
            print(f"error: guestbook entry not found: {entry_id}", file=sys.stderr)
            return 3
        if row["topic"] != topic:
            where = f"topic {topic}" if topic else "the main guestbook"
            print(f"error: entry {entry_id} is not in {where}", file=sys.stderr)
            return 3
        if Guestbook(conn, cfg.service).approve(entry_id):
            print(f"approved: {entry_id}")
            return 0
        if row["visible"]:
            print(f"error: guestbook entry {entry_id} is not hidden", file=sys.stderr)
        else:
            print(
                f"error: guestbook entry {entry_id} was hidden by an operator "
                "(not by moderation) -- left hidden as-is; use `guestbook hide` or "
                "`guestbook delete` to manage it directly",
                file=sys.stderr,
            )
        return 3
    finally:
        conn.close()


# =============================================================================
# mail
# =============================================================================


def cmd_mail(cfg: Config, *, reveal: bool = False, as_json: bool = False) -> int:
    """Counts-only by default (pending count overall, per-recipient
    breakdown, and a total count of hidden/moderated notes); --reveal
    additionally prints every note's sender/recipient/body -- hidden
    (auto-moderated) notes included and marked with their moderation
    reason, since those are exactly the ones an operator abuse review
    needs to see (operator abuse review only)."""
    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        rows = conn.execute(
            "SELECT recipient_node_id, COUNT(*) AS n FROM mail WHERE hidden=0 "
            "GROUP BY recipient_node_id ORDER BY n DESC"
        ).fetchall()
        total_pending = sum(r["n"] for r in rows)
        total_hidden = conn.execute("SELECT COUNT(*) FROM mail WHERE hidden=1").fetchone()[0]

        if as_json:
            data = {
                "pending": total_pending,
                "by_recipient": {r["recipient_node_id"]: r["n"] for r in rows},
                "hidden": total_hidden,
            }
            if reveal:
                notes = conn.execute(
                    "SELECT id, created_at, sender_node_id, recipient_node_id, message, "
                    "hidden, moderation_reason FROM mail ORDER BY id DESC"
                ).fetchall()
                data["notes"] = [
                    {
                        "id": n["id"],
                        "created_at": n["created_at"],
                        "from": n["sender_node_id"],
                        "to": n["recipient_node_id"],
                        "message": n["message"],
                        "hidden": bool(n["hidden"]),
                        "moderation_reason": n["moderation_reason"],
                    }
                    for n in notes
                ]
            print(json.dumps(data))
        else:
            print(f"pending: {total_pending}")
            for r in rows:
                print(f"  {_bbs_node_label(conn, r['recipient_node_id'])}: {r['n']}")
            print(f"hidden: {total_hidden}")
            if reveal:
                notes = conn.execute(
                    "SELECT id, created_at, sender_node_id, recipient_node_id, message, "
                    "hidden, moderation_reason FROM mail ORDER BY id DESC"
                ).fetchall()
                for n in notes:
                    when = local_iso(n["created_at"], cfg.service.timezone)
                    tag = f" [hidden: {n['moderation_reason']}]" if n["hidden"] else ""
                    print(
                        f"#{n['id']} {when}  {_bbs_node_label(conn, n['sender_node_id'])} -> "
                        f"{_bbs_node_label(conn, n['recipient_node_id'])}{tag}: {n['message']}"
                    )
        return 0
    finally:
        conn.close()


def cmd_mail_purge(cfg: Config, node_id: str) -> int:
    """Deletes all mail for `node_id`, hidden/moderated notes included --
    an explicit operator write, like `guestbook hide`/`delete` and
    `block`/`unblock`."""
    from meshgarden.database import connect, migrate

    parsed = bbs_node_argument(cfg, node_id)
    if parsed is None:
        print(f"invalid node id: {node_id!r}", file=sys.stderr)
        return 2
    node_id = parsed

    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    try:
        migrate(conn, transport=cfg.radio.transport, salt=cfg.service.fortune_salt)
        label = _bbs_node_label(conn, node_id)
        with conn:
            cur = conn.execute("DELETE FROM mail WHERE recipient_node_id=?", (node_id,))
        print(f"purged: {cur.rowcount} note(s) for {label}")
        return 0
    finally:
        conn.close()


def cmd_mail_alias_list(cfg: Config, *, as_json: bool = False) -> int:
    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        from meshgarden.mail import MailAliases

        rows = MailAliases(conn).list_active()
        if as_json:
            print(json.dumps([dict(row) for row in rows]))
        else:
            for row in rows:
                print(f"@{row['alias']} -> {_bbs_node_label(conn, row['node_id'])}")
            if not rows:
                print("no active mail aliases")
        return 0
    finally:
        conn.close()


def cmd_mail_alias_set(
    cfg: Config, node_id: str, alias: str, *, replace_existing: bool = False
) -> int:
    from meshgarden.database import connect, migrate
    from meshgarden.mail import MailAliases

    parsed = bbs_node_argument(cfg, node_id)
    if parsed is None:
        print(f"invalid node id: {node_id!r}", file=sys.stderr)
        return 2
    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    try:
        migrate(conn, transport=cfg.radio.transport, salt=cfg.service.fortune_salt)
        status = MailAliases(conn).operator_set(
            parsed, alias, int(time.time()), replace=replace_existing
        )
        if status == "invalid":
            print("invalid alias: use 3-16 letters/numbers/_/-", file=sys.stderr)
            return 2
        if status == "taken":
            print(
                "alias already belongs to another node; use --replace to transfer",
                file=sys.stderr,
            )
            return 3
        from meshgarden.mail import normalize_mail_alias

        normalized = normalize_mail_alias(alias, allow_at=False)
        assert normalized is not None
        print(f"alias set: @{normalized} -> {_bbs_node_label(conn, parsed)}")
        return 0
    finally:
        conn.close()


def cmd_mail_alias_release(cfg: Config, alias: str) -> int:
    from meshgarden.database import connect, migrate
    from meshgarden.mail import MailAliases, normalize_mail_alias

    normalized = normalize_mail_alias(alias)
    if normalized is None:
        print("invalid alias", file=sys.stderr)
        return 2
    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    try:
        migrate(conn, transport=cfg.radio.transport, salt=cfg.service.fortune_salt)
        if not MailAliases(conn).operator_release(normalized, int(time.time())):
            print(f"alias not found: @{normalized}", file=sys.stderr)
            return 3
        print(f"alias released: @{normalized}")
        return 0
    finally:
        conn.close()


# =============================================================================
# lab
# =============================================================================


def cmd_lab(cfg: Config, *, as_json: bool = False) -> int:
    """Lists nodes that have unlocked the LAB tier -- node id and unlock
    time only. The configured unlock word itself is never read from
    config here and never printed by any CLI output."""
    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        rows = conn.execute(
            "SELECT node_id, unlocked_at FROM lab_nodes ORDER BY unlocked_at"
        ).fetchall()
        labels = {r["node_id"]: _bbs_node_label(conn, r["node_id"]) for r in rows}
    finally:
        conn.close()

    if as_json:
        print(json.dumps({
            "unlocked": len(rows),
            "nodes": [
                {"node_id": r["node_id"], "unlocked_at": r["unlocked_at"]} for r in rows
            ],
        }))
    else:
        print(f"unlocked: {len(rows)}")
        for r in rows:
            when = local_iso(r["unlocked_at"], cfg.service.timezone)
            print(f"  {labels[r['node_id']]}  {when}")
    return 0


def cmd_positions(
    cfg: Config, *, node_id: str | None = None, days: int | None = None,
    limit: int = 50, as_json: bool = False,
) -> int:
    """Lists positions visitors shared by DM (dm_positions), newest first."""
    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        since = int(time.time()) - days * 86400 if days is not None else None
        rows = positions.list_dm_positions(conn, node_id=node_id, since=since, limit=limit)
        labels = {r["node_id"]: _node_label(conn, r["node_id"]) for r in rows}
    finally:
        conn.close()

    if as_json:
        print(json.dumps({
            "count": len(rows),
            "positions": [
                {
                    "id": r["id"], "node_id": r["node_id"], "server_time": r["server_time"],
                    "kind": r["kind"], "lat": r["lat"], "lon": r["lon"],
                }
                for r in rows
            ],
        }))
    else:
        print(f"positions: {len(rows)}")
        for r in rows:
            when = local_iso(r["server_time"], cfg.service.timezone)
            print(
                f"  #{r['id']}  {when}  {labels[r['node_id']]}  {r['kind']}  "
                f"{r['lat']:.6f}, {r['lon']:.6f}"
            )
    return 0


def _local_day_start(day: str, tz: str) -> int:
    """UTC epoch seconds of local midnight starting `day` (YYYY-MM-DD)."""
    from zoneinfo import ZoneInfo

    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ZoneInfo(tz)).timestamp())


def cmd_positions_delete(
    cfg: Config,
    *,
    ids: list[int] | None = None,
    id_range: str | None = None,
    node_id: str | None = None,
    from_day: str | None = None,
    to_day: str | None = None,
) -> int:
    """Deletes shared positions matching every given filter. Dates are local
    calendar days in [service] timezone, both ends inclusive. Exits 2 for
    bad arguments or no filter at all, 3 when nothing matched."""
    from datetime import timedelta

    from meshgarden.config import parse_node_id_argument
    from meshgarden.database import connect, migrate

    if not (ids or id_range or node_id or from_day or to_day):
        print(
            "positions delete needs at least one of: ID..., --range, --node, --from, --to",
            file=sys.stderr,
        )
        return 2
    if ids and any(i < 1 for i in ids):
        print("ids must be positive", file=sys.stderr)
        return 2
    parsed_range = None
    if id_range is not None:
        lo, sep, hi = id_range.partition("-")
        if not (sep and lo.isdigit() and hi.isdigit() and 1 <= int(lo) <= int(hi)):
            print(f"--range must be FIRST-LAST, e.g. 10-20 (got {id_range!r})", file=sys.stderr)
            return 2
        parsed_range = (int(lo), int(hi))
    if node_id is not None:
        parsed_node = parse_node_id_argument(node_id)
        if parsed_node is None:
            print(f"invalid node id: {node_id!r}", file=sys.stderr)
            return 2
        node_id = parsed_node
    tz = cfg.service.timezone
    try:
        since = _local_day_start(from_day, tz) if from_day else None
        until = None
        if to_day:
            next_day = datetime.strptime(to_day, "%Y-%m-%d") + timedelta(days=1)
            until = _local_day_start(next_day.strftime("%Y-%m-%d"), tz)
    except ValueError:
        print("--from/--to must be YYYY-MM-DD", file=sys.stderr)
        return 2
    if since is not None and until is not None and since >= until:
        print("--from must not be after --to", file=sys.stderr)
        return 2

    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    try:
        migrate(conn, transport=cfg.radio.transport, salt=cfg.service.fortune_salt)
        deleted = positions.delete_dm_positions(
            conn, ids=ids or None, id_range=parsed_range, node_id=node_id,
            since=since, until=until,
        )
    finally:
        conn.close()
    if deleted:
        print(f"deleted: {deleted}")
        return 0
    print("no matching positions", file=sys.stderr)
    return 3


def cmd_lab_revoke(cfg: Config, node_id: str) -> int:
    """Deletes one node's LAB-tier unlock -- an explicit operator write,
    like `mail purge`/`block`/`unblock`."""
    from meshgarden.database import connect, migrate

    parsed = bbs_node_argument(cfg, node_id)
    if parsed is None:
        print(f"invalid node id: {node_id!r}", file=sys.stderr)
        return 2
    node_id = parsed

    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    try:
        migrate(conn, transport=cfg.radio.transport, salt=cfg.service.fortune_salt)
        label = _bbs_node_label(conn, node_id)
        with conn:
            cur = conn.execute("DELETE FROM lab_nodes WHERE node_id=?", (node_id,))
        if cur.rowcount:
            print(f"revoked: {label}")
            return 0
        print(f"not unlocked: {label}", file=sys.stderr)
        return 3
    finally:
        conn.close()


# =============================================================================
# quota
# =============================================================================


def cmd_quota_reset(
    cfg: Config,
    node_id: str,
    *,
    day: str | None,
    commands_only: bool = False,
    signs_only: bool = False,
) -> int:
    """Resets one node's daily quota counters for `day` (default: today in
    `[service] timezone`, NOT the system timezone) -- an explicit operator
    write, like `mail purge`/`lab revoke`/`block`/`unblock`.

    Default (neither flag) deletes the whole `(node_id, day)` row from
    `node_daily_quota`; `--commands-only`/`--signs-only` instead zero just
    that one column, leaving the row (and the other counter) alone. A
    missing row is not an error: "nothing to reset" is still exit 0.

    Never touches the in-memory, per-process 1-hour SIGN cooldown
    (`RateLimiter`) -- that is separate from the daily `signs` counter
    reset here, and is only cleared by a daemon restart or the sender's
    own UNSIGN.
    """
    from meshgarden.database import connect, migrate
    from meshgarden.fortune import local_day

    day = day or local_day(int(time.time()), cfg.service.timezone)

    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    try:
        migrate(conn, transport=cfg.radio.transport, salt=cfg.service.fortune_salt)
        resolved, matches = _resolve_quota_node(conn, node_id)
        if resolved is None:
            return _report_quota_node_error(node_id, matches)
        node_id = resolved
        label = _node_label(conn, node_id)
        with conn:
            if commands_only:
                cur = conn.execute(
                    "UPDATE node_daily_quota SET commands=0 WHERE node_id=? AND day=?",
                    (node_id, day),
                )
                what = "commands counter"
            elif signs_only:
                cur = conn.execute(
                    "UPDATE node_daily_quota SET signs=0 WHERE node_id=? AND day=?",
                    (node_id, day),
                )
                what = "signs counter"
            else:
                cur = conn.execute(
                    "DELETE FROM node_daily_quota WHERE node_id=? AND day=?", (node_id, day)
                )
                what = "quota row"
        if cur.rowcount:
            print(
                f"reset {what}: {label} on {day} "
                "(in-memory SIGN cooldown unaffected -- a restart or UNSIGN clears it)"
            )
        else:
            print(f"nothing to reset: {label} on {day}")
        return 0
    finally:
        conn.close()


def cmd_quota_reset_all(cfg: Config) -> int:
    """Delete every stored daily quota row for every node and day.

    This write is reachable only through the explicit `quota --reset-all`
    operator flag. It does not affect the daemon's in-memory SIGN cooldown.
    """
    from meshgarden.database import connect, migrate

    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
        transport=cfg.radio.transport)
    try:
        migrate(conn, transport=cfg.radio.transport, salt=cfg.service.fortune_salt)
        with conn:
            cur = conn.execute("DELETE FROM node_daily_quota")
        print(
            f"reset all quota rows: {cur.rowcount} "
            "(all nodes, all days; in-memory SIGN cooldown unaffected)"
        )
        return 0
    finally:
        conn.close()


def cmd_quota_show(cfg: Config, node_id: str | None, *, day: str | None) -> int:
    """Read-only quota listing with all four persisted counters.

    With a node id, preserves the original behavior: default to today in
    `[service] timezone` and print that one row (or "no row"). Without a
    node id, list every stored node/day row, optionally filtered by `day`.
    """
    from meshgarden.fortune import local_day

    conn = _connect_readonly_or_report(cfg)
    if conn is None:
        return 1
    try:
        requested_label = None
        if node_id is not None:
            identifier = node_id
            resolved, matches = _resolve_quota_node(conn, identifier)
            if resolved is None:
                return _report_quota_node_error(identifier, matches)
            node_id = resolved
            day = day or local_day(int(time.time()), cfg.service.timezone)
            requested_label = _quota_node_label(conn, node_id)
        if node_id is not None:
            rows = conn.execute(
                "SELECT node_id, day, commands, signs, posts, mails_sent "
                "FROM node_daily_quota WHERE node_id=? AND day=?",
                (node_id, day),
            ).fetchall()
        elif day is not None:
            rows = conn.execute(
                "SELECT node_id, day, commands, signs, posts, mails_sent "
                "FROM node_daily_quota WHERE day=? ORDER BY node_id",
                (day,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT node_id, day, commands, signs, posts, mails_sent "
                "FROM node_daily_quota ORDER BY day DESC, node_id"
            ).fetchall()
        rendered = [
            (
                _quota_node_label(conn, row["node_id"]), row["day"], row["commands"],
                row["signs"], row["posts"], row["mails_sent"],
            )
            for row in rows
        ]
    finally:
        conn.close()

    if not rendered:
        if node_id is not None:
            print(f"{requested_label} {day}: no row")
        elif day is not None:
            print(f"no quota rows for {day}")
        else:
            print("no quota rows")
        return 0

    for label, row_day, commands, signs, posts, mails_sent in rendered:
        print(
            f"{label} {row_day}: commands={commands} signs={signs} "
            f"posts={posts} mails_sent={mails_sent}"
        )
    return 0


# =============================================================================
# config-sync
# =============================================================================


def cmd_config_sync(*, template_path: str, config_path: str, dry_run: bool) -> int:
    """Adds to `config_path` every key present in the rendered template at
    `template_path` but missing from it -- each with the template's
    default value and comment -- without changing, reordering, or
    reformatting any line already in `config_path` (see
    `config_sync.sync_missing_keys`). Prints one `section.key` line per
    addition, or "nothing to add" when the config is already complete.
    `--dry-run` prints the same thing but writes nothing. A real run
    backs the original file up to `<config_path>.bak.<UTC timestamp>`,
    preserving its mode (and, when running as a user allowed to, its
    owner) before writing the merged result, and -- belt and suspenders on
    top of `sync_missing_keys` itself only ever appending -- refuses to
    write anything (exit 1) if the merged text doesn't still parse as
    TOML and validate as a `Config`.
    """
    from meshgarden.config import ConfigError, parse_config
    from meshgarden.config_sync import sync_missing_keys

    try:
        existing_text = Path(config_path).read_text()
    except OSError as exc:
        print(f"error: cannot read --config {config_path}: {exc}", file=sys.stderr)
        return 1
    try:
        template_text = Path(template_path).read_text()
    except OSError as exc:
        print(f"error: cannot read --template {template_path}: {exc}", file=sys.stderr)
        return 1

    try:
        new_text, additions, skipped = sync_missing_keys(existing_text, template_text)
    except (tomllib.TOMLDecodeError, ConfigError) as exc:
        print(f"error: cannot parse --config/--template as TOML: {exc}", file=sys.stderr)
        return 1

    for path in skipped:
        print(f"note: skipped nested table {path}", file=sys.stderr)

    if not additions:
        print("nothing to add")
        return 0

    try:
        parse_config(tomllib.loads(new_text))
    except (tomllib.TOMLDecodeError, ConfigError) as exc:
        print(f"error: sync result failed validation, nothing written: {exc}", file=sys.stderr)
        return 1

    for key in additions:
        print(key)

    if dry_run:
        return 0

    backup_path = f"{config_path}.bak.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    st = os.stat(config_path)
    shutil.copy2(config_path, backup_path)  # copies bytes + mode bits (not owner)
    try:
        os.chown(backup_path, st.st_uid, st.st_gid)
    except OSError as exc:
        print(
            f"note: could not chown backup {backup_path} to "
            f"{st.st_uid}:{st.st_gid}: {exc}",
            file=sys.stderr,
        )
    Path(config_path).write_text(new_text)
    return 0


# =============================================================================
# wiring
# =============================================================================


def _add_command(subparsers, name, *, help, description, examples=()):
    """Adds a subparser with the shared help layout (wrapped description,
    verbatim Examples epilog); same shape as the helper in main.py."""
    epilog = "Examples\n" + "\n".join(f"  {e}" for e in examples) if examples else None
    return subparsers.add_parser(
        name,
        help=help,
        description=textwrap.fill(description, 78),
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def register_subcommands(subparsers, guestbook_subparsers) -> None:
    """Adds this module's subcommands to an existing argparse tree.

    `subparsers` is the top-level `add_subparsers()` object (same one `run`,
    `status`, etc. are added to); `guestbook_subparsers` is the `guestbook`
    subcommand's own `add_subparsers()` object (same one `hide`/`delete`/
    `list` are added to) -- `review` is added there instead of top-level so
    it reads naturally as `mgarden guestbook review`.
    """
    tail_parser = _add_command(
        subparsers,
        "tail",
        help="follow the DM/reply exchange trace (written only by `run --debug`)",
        description=(
            "Follows the trace of visitor messages and replies, like `tail -f`. The "
            "trace file exists only while the daemon runs with `mgarden run --debug`; "
            "exits 1 if it is not found. By default only new lines are shown: -n N "
            "prints the last N existing lines first, then follows, and --from-start "
            "prints the whole file first. Ctrl-C exits cleanly."
        ),
        examples=(
            "mgarden tail                 follow new lines only",
            "mgarden tail -n 200          print the last 200 lines, then follow",
            "mgarden tail --from-start    print the whole file, then follow",
        ),
    )
    tail_start = tail_parser.add_mutually_exclusive_group()
    tail_start.add_argument(
        "--from-start",
        action="store_true",
        help="print the whole file first, then follow (default: only new lines)",
    )
    tail_start.add_argument(
        "-n",
        "--lines",
        type=int,
        default=0,
        metavar="N",
        help="print the last N existing lines first, then follow (like tail -n N -f)",
    )

    export_parser = _add_command(
        subparsers,
        "export",
        help="export guestbook entries",
        description=(
            "Exports guestbook entries as CSV or JSON, to stdout unless --out is given. "
            "Visible entries only unless --hidden. Read-only; exits 1 if the database "
            "is not found."
        ),
        examples=(
            "mgarden export > guestbook.csv",
            "mgarden export --format json --hidden --out entries.json",
        ),
    )
    export_parser.add_argument(
        "--format",
        choices=("csv", "json"),
        default="csv",
        help="output format (default: csv)",
    )
    export_parser.add_argument("--hidden", action="store_true", help="include hidden entries")
    export_parser.add_argument(
        "--out", default=None, metavar="PATH", help="write to PATH instead of stdout"
    )
    export_parser.add_argument(
        "--topic", default=None, metavar="TOPIC", help="export this board only (default: main)"
    )

    restore_parser = _add_command(
        subparsers,
        "restore",
        help="restore a backup file into the configured database",
        description=(
            "Restores a backup file into the configured database. Stop the daemon first. "
            "Refuses to replace an existing database without --yes; exits 1 if refused "
            "or failed."
        ),
        examples=(
            "mgarden restore /var/backups/meshgarden/latest.db --yes",
            "mgarden restore backup.db --overwrite --yes",
        ),
    )
    restore_parser.add_argument(
        "backup_path", metavar="PATH", help="backup file to restore from"
    )
    restore_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace the existing database with the backup",
    )
    restore_parser.add_argument(
        "--yes", action="store_true", help="confirm restoring over an existing database"
    )

    doctor_parser = _add_command(
        subparsers,
        "doctor",
        help="run operator health checks",
        description=(
            "Checks identity, radio device, database, disk, backups, timezone, status "
            "file and metrics port, and says what is wrong. Read-only; exits 1 if any "
            "check failed."
        ),
        examples=("mgarden doctor", "mgarden doctor --json"),
    )
    doctor_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")

    digest_parser = _add_command(
        subparsers,
        "digest",
        help="aggregate-only daily operator digest (default: yesterday)",
        description=(
            "Summarizes one day: packets, nodes, busiest hour, guestbook signatures, "
            "reply outcomes and commands by type. Counts only, never node ids or message "
            "text. Read-only; exits 1 if the database is not found and 2 for a bad --date."
        ),
        examples=("mgarden digest", "mgarden digest --date 2026-10-09 --json"),
    )
    digest_parser.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="local calendar day (default: yesterday in [service] timezone)",
    )
    digest_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")

    review_parser = _add_command(
        guestbook_subparsers,
        "review",
        help="list hidden entries with a moderation reason, for operator review",
        description=(
            "Lists entries the SIGN banned-word guard hid automatically, with the reason, "
            "newest first. Read-only; exits 1 if the database is not found."
        ),
        examples=("mgarden guestbook review", "mgarden guestbook review --limit 50"),
    )
    review_parser.add_argument(
        "--limit", type=int, default=10, metavar="N", help="show at most N entries (default: 10)"
    )
    review_parser.add_argument(
        "--topic", default=None, metavar="TOPIC", help="review this board only (default: main)"
    )

    approve_parser = _add_command(
        guestbook_subparsers,
        "approve",
        help="approve a hidden entry auto-moderated by the SIGN banned-word guard",
        description=(
            "Reverses the automatic hide on one entry so visitors can see it. Exits 3 if "
            "the entry is not found or was not hidden by moderation."
        ),
        examples=("mgarden guestbook approve 12",),
    )
    approve_parser.add_argument("entry_id", type=int, metavar="entry-id", help="entry id")
    approve_parser.add_argument(
        "--topic", default=None, metavar="TOPIC", help="board the entry belongs to (default: main)"
    )

    mail_parser = _add_command(
        subparsers,
        "mail",
        help="mailbox operator view (counts only by default)",
        description=(
            "Shows pending mail counts overall and per recipient, plus how many notes "
            "moderation hid. Message bodies stay private unless --reveal is given. "
            "Read-only; exits 1 if the database is not found."
        ),
        examples=("mgarden mail", "mgarden mail --reveal", "mgarden mail purge !a1b2c3d4"),
    )
    mail_parser.add_argument(
        "--reveal",
        action="store_true",
        help="also print sender, recipient and body of every note (operator abuse review only)",
    )
    mail_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    mail_sub = mail_parser.add_subparsers(dest="mail_command", metavar="<action>")
    purge_parser = _add_command(
        mail_sub,
        "purge",
        help="delete all mail for a node id (hidden/moderated included)",
        description=(
            "Deletes every mail note for one node id, including hidden and moderated "
            "ones. This cannot be undone."
        ),
        examples=("mgarden mail purge !a1b2c3d4",),
    )
    purge_parser.add_argument("node_id", metavar="node-id", help="node id, e.g. !a1b2c3d4")
    alias_parser = _add_command(
        mail_sub,
        "alias",
        help="manage stable @aliases",
        description=(
            "Lists, sets or releases the @aliases visitors use to receive mail. Local "
            "changes only; nothing is transmitted."
        ),
        examples=("mgarden mail alias list", "mgarden mail alias set !a1b2c3d4 alice"),
    )
    alias_sub = alias_parser.add_subparsers(
        dest="mail_alias_command", required=True, metavar="<action>"
    )
    alias_list = _add_command(
        alias_sub,
        "list",
        help="list active aliases",
        description=(
            "Lists active aliases and the node ids they point to. Read-only; never "
            "transmits; exits 1 if the database is not found."
        ),
        examples=("mgarden mail alias list --json",),
    )
    alias_list.add_argument("--json", action="store_true", help="emit JSON instead of text")
    alias_set = _add_command(
        alias_sub,
        "set",
        help="set or transfer an alias",
        description=(
            "Points an alias at a node id. Exits 2 for an invalid node or alias, and 3 if "
            "another node owns the alias and --replace was not given."
        ),
        examples=(
            "mgarden mail alias set !a1b2c3d4 alice",
            "mgarden mail alias set !a1b2c3d4 alice --replace",
        ),
    )
    alias_set.add_argument("node_id", metavar="node-id", help="node id, e.g. !a1b2c3d4")
    alias_set.add_argument("alias", metavar="alias", help="alias name, without the @")
    alias_set.add_argument(
        "--replace", action="store_true", help="transfer an alias from its current owner"
    )
    alias_release = _add_command(
        alias_sub,
        "release",
        help="release an alias",
        description=(
            "Frees an alias locally; its former owner keeps the anti-churn wait before "
            "claiming another. Exits 2 for an invalid alias and 3 if it was not found."
        ),
        examples=("mgarden mail alias release alice",),
    )
    alias_release.add_argument("alias", metavar="alias", help="alias name, without the @")

    lab_parser = _add_command(
        subparsers,
        "lab",
        help="list nodes that have unlocked the LAB tier (node id + unlock time only)",
        description=(
            "Lists nodes that have unlocked the LAB tier, with node id and unlock time "
            "only, never the secret word. Read-only; exits 1 if the database is not found."
        ),
        examples=("mgarden lab", "mgarden lab revoke !a1b2c3d4"),
    )
    lab_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    lab_sub = lab_parser.add_subparsers(dest="lab_command", metavar="<action>")
    lab_revoke_parser = _add_command(
        lab_sub,
        "revoke",
        help="revoke one node's LAB-tier unlock",
        description="Takes away one node's LAB-tier unlock. Exits 3 if the node was not unlocked.",
        examples=("mgarden lab revoke !a1b2c3d4",),
    )
    lab_revoke_parser.add_argument("node_id", metavar="node-id", help="node id, e.g. !a1b2c3d4")

    positions_parser = _add_command(
        subparsers,
        "positions",
        help="list positions visitors shared by DM",
        description=(
            "Lists positions visitors deliberately shared by DM (\"Saved position: lat, lon\" "
            "or \"GPS position: lat, lon\"), newest first. Only recorded when "
            "[observatory] dm_position_capture is on. Read-only; exits 1 if the database "
            "is not found."
        ),
        examples=(
            "mgarden positions",
            "mgarden positions --node !a1b2c3d4 --days 7",
            "mgarden positions --limit 200 --json",
            "mgarden positions delete 12",
        ),
    )
    positions_parser.add_argument(
        "--node", metavar="node-id", default=None, help="only this node id, e.g. !a1b2c3d4"
    )
    positions_parser.add_argument(
        "--days", type=int, default=None, metavar="N", help="only the last N days"
    )
    positions_parser.add_argument(
        "--limit", type=int, default=50, metavar="N", help="show at most N rows (default: 50)"
    )
    positions_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    positions_sub = positions_parser.add_subparsers(dest="positions_command", metavar="<action>")
    positions_delete_parser = _add_command(
        positions_sub,
        "delete",
        help="delete shared positions by id, id range, node, or date range",
        description=(
            "Deletes shared positions matching every given filter (they combine with AND). "
            "At least one filter is required. Dates are local days in [service] timezone, "
            "both ends inclusive. The ids are the #N column of `mgarden positions`. Exits 2 "
            "for invalid arguments and 3 if nothing matched."
        ),
        examples=(
            "mgarden positions delete 12",
            "mgarden positions delete 12 15 19",
            "mgarden positions delete --range 10-20",
            "mgarden positions delete --from 2026-10-01 --to 2026-10-07",
            "mgarden positions delete --node !a1b2c3d4 --to 2026-09-30",
        ),
    )
    positions_delete_parser.add_argument(
        "ids", nargs="*", type=int, metavar="ID", help="position ids to delete"
    )
    positions_delete_parser.add_argument(
        "--range", dest="id_range", default=None, metavar="FIRST-LAST",
        help="an inclusive id range, e.g. 10-20",
    )
    positions_delete_parser.add_argument(
        "--node", dest="delete_node", default=None, metavar="node-id",
        help="only this node id",
    )
    positions_delete_parser.add_argument(
        "--from", dest="from_day", default=None, metavar="YYYY-MM-DD",
        help="first local day to delete (inclusive)",
    )
    positions_delete_parser.add_argument(
        "--to", dest="to_day", default=None, metavar="YYYY-MM-DD",
        help="last local day to delete (inclusive)",
    )

    quota_parser = _add_command(
        subparsers,
        "quota",
        help="inspect or reset stored daily quota counters",
        description=(
            "Shows or resets the per-node daily quota counters (commands, signs, posts, "
            "mails sent). Never affects the in-memory SIGN cooldown."
        ),
        examples=(
            "mgarden quota show !a1b2c3d4",
            "mgarden quota reset !a1b2c3d4 --signs-only",
            "mgarden quota --reset-all",
        ),
    )
    quota_parser.add_argument(
        "--reset-all",
        action="store_true",
        help="delete all quota rows for every node and every day",
    )
    quota_sub = quota_parser.add_subparsers(dest="quota_command", metavar="<action>")
    quota_reset_parser = _add_command(
        quota_sub,
        "reset",
        help=(
            "reset one node's daily quota (default day: today in [service] "
            "timezone; does not affect the in-memory SIGN cooldown)"
        ),
        description=(
            "Resets one node's daily quota: deletes the whole row, or zeroes one counter "
            "with --commands-only or --signs-only. Accepts a node id or a unique exact "
            "known name; exits 2 if the target is unknown or ambiguous. Having no row to "
            "reset is not an error."
        ),
        examples=(
            "mgarden quota reset !a1b2c3d4",
            "mgarden quota reset !a1b2c3d4 --day 2026-10-09 --commands-only",
        ),
    )
    quota_reset_parser.add_argument(
        "node_id", metavar="node-id-or-name", help="node id, or a unique exact known name"
    )
    quota_reset_parser.add_argument(
        "--day", default=None, metavar="YYYY-MM-DD", help="local calendar day (default: today)"
    )
    quota_reset_group = quota_reset_parser.add_mutually_exclusive_group()
    quota_reset_group.add_argument(
        "--commands-only", action="store_true", help="reset only the commands counter"
    )
    quota_reset_group.add_argument(
        "--signs-only", action="store_true", help="reset only the signs counter"
    )
    quota_show_parser = _add_command(
        quota_sub,
        "show",
        help="list quota rows and all counters; optionally select one node",
        description=(
            "Lists stored quota rows with all counters. Without a node it lists every day "
            "and node (--day filters); with a node the day defaults to today. Read-only; "
            "exits 1 if the database is not found and 2 if the target is unknown or "
            "ambiguous."
        ),
        examples=("mgarden quota show", "mgarden quota show !a1b2c3d4 --day 2026-10-09"),
    )
    quota_show_parser.add_argument(
        "node_id",
        nargs="?",
        metavar="node-id-or-name",
        help="node id, or a unique exact known name (default: all nodes)",
    )
    quota_show_parser.add_argument(
        "--day", default=None, metavar="YYYY-MM-DD", help="local calendar day (default: today)"
    )

    config_sync_parser = _add_command(
        subparsers,
        "config-sync",
        help=(
            "add config keys missing from an existing config, with their "
            "defaults and comments (never changes an existing value)"
        ),
        description=(
            "Adds keys that are in a template config but missing from your config, with "
            "the template's default and comment. Never changes a value already set; backs "
            "the file up first. Used by install-rpi.sh on re-run, not usually by hand."
        ),
        examples=(
            "mgarden config-sync --template config.toml.example --dry-run",
            "mgarden config-sync --template new.toml --config /etc/meshgarden/config.toml",
        ),
    )
    config_sync_parser.add_argument(
        "--template",
        required=True,
        metavar="PATH",
        help="rendered template config to read missing keys' defaults/comments from",
    )
    config_sync_parser.add_argument(
        "--config",
        # SUPPRESS (not None) so that, when this flag is omitted, argparse
        # leaves the namespace's shared `config` dest untouched instead of
        # stomping the global `--config` value with its own default -- see
        # "mgarden --config X config-sync ..." vs. "config-sync --config Y".
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="config file to add missing keys to (default: same as the global --config)",
    )
    config_sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be added; write nothing",
    )


def dispatch(args: argparse.Namespace, cfg: Config) -> int | None:
    """Returns None when `args.command` is not one of this module's own
    subcommands, so main.py's dispatch can fall through unchanged."""
    command = getattr(args, "command", None)

    if command == "tail":
        return cmd_tail(cfg, from_start=args.from_start, lines=args.lines)
    if command == "export":
        try:
            topic = ops._normalize_cli_topic(args.topic)
        except ValueError:
            print("invalid topic", file=sys.stderr)
            return 2
        return cmd_export(
            cfg, fmt=args.format, include_hidden=args.hidden, out_path=args.out, topic=topic
        )
    if command == "restore":
        return cmd_restore(cfg, args.backup_path, overwrite=args.overwrite, yes=args.yes)
    if command == "doctor":
        return cmd_doctor(cfg, as_json=args.json)
    if command == "digest":
        return cmd_digest(cfg, date=args.date, as_json=args.json)
    if command == "guestbook" and getattr(args, "guestbook_command", None) == "review":
        try:
            topic = ops._normalize_cli_topic(args.topic)
        except ValueError:
            print("invalid topic", file=sys.stderr)
            return 2
        return cmd_guestbook_review(cfg, limit=args.limit, topic=topic)
    if command == "guestbook" and getattr(args, "guestbook_command", None) == "approve":
        try:
            topic = ops._normalize_cli_topic(args.topic)
        except ValueError:
            print("invalid topic", file=sys.stderr)
            return 2
        return cmd_guestbook_approve(cfg, args.entry_id, topic=topic)
    if command == "mail" and getattr(args, "mail_command", None) == "purge":
        return cmd_mail_purge(cfg, args.node_id)
    if command == "mail" and getattr(args, "mail_command", None) == "alias":
        if args.mail_alias_command == "list":
            return cmd_mail_alias_list(cfg, as_json=args.json)
        if args.mail_alias_command == "set":
            return cmd_mail_alias_set(
                cfg, args.node_id, args.alias, replace_existing=args.replace
            )
        if args.mail_alias_command == "release":
            return cmd_mail_alias_release(cfg, args.alias)
        raise AssertionError(f"unhandled mail alias command {args.mail_alias_command!r}")
    if command == "mail":
        return cmd_mail(cfg, reveal=args.reveal, as_json=args.json)
    if command == "lab" and getattr(args, "lab_command", None) == "revoke":
        return cmd_lab_revoke(cfg, args.node_id)
    if command == "lab":
        return cmd_lab(cfg, as_json=args.json)
    if command == "positions" and getattr(args, "positions_command", None) == "delete":
        return cmd_positions_delete(
            cfg, ids=args.ids, id_range=args.id_range, node_id=args.delete_node,
            from_day=args.from_day, to_day=args.to_day,
        )
    if command == "positions":
        if args.limit < _MIN_LIST_LIMIT or args.limit > _MAX_LIST_LIMIT or (
            args.days is not None and args.days < 1
        ):
            print("--limit must be 1..1000 and --days >= 1", file=sys.stderr)
            return 2
        return cmd_positions(
            cfg, node_id=args.node, days=args.days, limit=args.limit, as_json=args.json
        )
    if command == "quota" and args.reset_all:
        if args.quota_command is not None:
            print("--reset-all cannot be combined with a quota subcommand", file=sys.stderr)
            return 2
        return cmd_quota_reset_all(cfg)
    if command == "quota" and args.quota_command == "reset":
        return cmd_quota_reset(
            cfg,
            args.node_id,
            day=args.day,
            commands_only=args.commands_only,
            signs_only=args.signs_only,
        )
    if command == "quota" and args.quota_command == "show":
        return cmd_quota_show(cfg, args.node_id, day=args.day)
    if command == "quota":
        print("quota requires show, reset, or --reset-all", file=sys.stderr)
        return 2
    if command == "config-sync":
        config_path = (
            getattr(args, "config", None)
            or os.environ.get("MESHTASTIC_OBSERVATORY_CONFIG")
            or _DEFAULT_CONFIG_PATH
        )
        return cmd_config_sync(
            template_path=args.template, config_path=config_path, dry_run=args.dry_run
        )

    return None
