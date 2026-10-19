"""Shared BBS file: the tables both daemons (Meshtastic and MeshCore) serve.

When `database.bbs_path` is set, `database.connect` attaches that file as
schema `bbs` and `database.migrate` calls `ensure_split`, which moves the
BBS tables out of the instance's own `main` file exactly once. Every query
in the codebase stays unqualified: once `main.<table>` is dropped, SQLite's
name resolution (temp, main, then attached schemas) finds `bbs.<table>`.

Observatory tables (nodes, observations, ...) stay per-instance in `main`.
"""

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

from meshgarden.booktrivia import book_key, puzzle_for
from meshgarden.wordle import player_key

# bbs.user_version: 1 = 1.11.0 split layout, 2 = sequence/reply-route repair
# applied (1.11.2), 3 = board unread cursors, 4 = linked identities. A fresh
# split is created at the base version and walks
# BBS_MIGRATIONS in the same call, so it lands at BBS_SCHEMA_VERSION.
BBS_BASE_VERSION = 1
BBS_SCHEMA_VERSION = 4

BBS_TABLES: tuple[str, ...] = (
    "guestbook",
    "news",
    "mail",
    "mail_prefs",
    "mail_aliases",
    "mail_reply_routes",
    "update_cursors",
    "update_pending",
    "board_cursors",
    "board_cursor_pending",
    "identity_links",
    "identity_challenges",
    "identity_audit",
    "wordle_state",
    "wordle_daily",
    "book_state",
    "book_stats",
    "lab_nodes",
    "blocked_senders",
)

BBS_VIEWS: tuple[str, ...] = ("book_stats_alltime",)

# Columns holding a node id. A legacy MeshCore instance stored bare `!xxxxxxxx`
# ids, which would collide with Meshtastic ids once both meshes share one
# table, so its rows are qualified with `mc:` while being moved.
NODE_ID_COLUMNS: dict[str, tuple[str, ...]] = {
    "guestbook": ("sender_node_id",),
    "mail": ("sender_node_id", "recipient_node_id"),
    "mail_prefs": ("node_id",),
    "mail_aliases": ("node_id",),
    "mail_reply_routes": ("sender_node_id", "recipient_node_id"),
    "update_cursors": ("node_id",),
    "update_pending": ("node_id",),
    "board_cursors": ("node_id",),
    "board_cursor_pending": ("node_id",),
    "identity_links": ("node_id", "account_id"),
    "identity_challenges": ("node_id",),
    "identity_audit": ("node_id", "account_id"),
    "lab_nodes": ("node_id",),
    "blocked_senders": ("node_id",),
}

_INSTANCE_KEY = "instance_id"

# sqlite_master.sql is normalized (no IF NOT EXISTS/TEMP, single spaces after
# the leading keywords), so only an optionally quoted object name follows.
_CREATE_RE = re.compile(r'^(CREATE\s+(?:TABLE|VIEW|(?:UNIQUE\s+)?INDEX)\s+)("?)(\w+)\2', re.I)


class SplitConflict(RuntimeError):
    """The shared BBS file cannot take this instance's legacy BBS data."""


def is_attached(conn: sqlite3.Connection) -> bool:
    return any(row[1] == "bbs" for row in conn.execute("PRAGMA database_list"))


def _db_file(conn: sqlite3.Connection, schema: str) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == schema:
            return row[2]
    return schema


@contextmanager
def _immediate(conn: sqlite3.Connection):
    # BEGIN IMMEDIATE takes the write lock on main and every attached file,
    # so a second daemon starting at the same moment waits (busy_timeout)
    # instead of interleaving its checks with our copy.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        # COMMIT itself can fail (e.g. busy); never leave the lock held.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _has(conn: sqlite3.Connection, schema: str, type_: str, name: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type = ? AND name = ?", (type_, name)
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, schema: str, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {schema}.{table}").fetchone()[0]


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create the BBS objects in `bbs` from this instance's own DDL, so the
    shared file matches the schema the legacy migrations produce."""
    objects = BBS_TABLES + BBS_VIEWS
    marks = ",".join("?" * len(objects))
    rows = conn.execute(
        f"SELECT type, name, sql FROM main.sqlite_master WHERE tbl_name IN ({marks}) "
        "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_autoindex_%'",
        objects,
    ).fetchall()
    present = {r[1] for r in rows if r[0] in ("table", "view")}
    missing = [name for name in objects if name not in present]
    if missing:
        # e.g. this instance was split earlier and the shared file was
        # replaced by an empty one: re-creating it from nothing would hide
        # the loss behind a working but empty garden.
        raise SplitConflict(
            f"shared BBS file {_db_file(conn, 'bbs')} is uninitialized but "
            f"{_db_file(conn, 'main')} has no {', '.join(missing)} to create it from; "
            "restore the shared BBS file"
        )
    order = {"table": 0, "index": 1, "view": 2}
    for type_, name, sql in sorted(rows, key=lambda r: order.get(r[0], 3)):
        if type_ not in order:
            # A trigger (none exist today) would otherwise vanish from the
            # shared schema without anyone noticing.
            raise RuntimeError(f"BBS split cannot carry {type_} {name}")
        rewritten, n = _CREATE_RE.subn(r"\1bbs.\2\3\2", sql, count=1)
        if n != 1:
            raise RuntimeError(f"cannot rewrite DDL for {type_} {name}: {sql[:60]!r}")
        conn.execute(rewritten)
    conn.execute("CREATE TABLE bbs.bbs_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(f"PRAGMA bbs.user_version = {BBS_BASE_VERSION}")


def _instance_id(conn: sqlite3.Connection) -> str:
    """Stable per-instance id recorded on each table this instance moved, so a
    restart after a crash between copy and drop recognizes its own copy."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO daemon_state(key, value, updated_at) VALUES (?, ?, ?)",
            (_INSTANCE_KEY, uuid.uuid4().hex, int(time.time())),
        )
    return conn.execute(
        "SELECT value FROM daemon_state WHERE key = ?", (_INSTANCE_KEY,)
    ).fetchone()[0]


def _copy_table(conn: sqlite3.Connection, table: str, transport: str) -> None:
    cols = [row[1] for row in conn.execute(f"PRAGMA main.table_info({table})")]
    qualify = transport == "meshcore"
    node_cols = set(NODE_ID_COLUMNS.get(table, ())) if qualify else set()
    exprs = [
        f"CASE WHEN {c} LIKE '!%' THEN 'mc:' || {c} ELSE {c} END" if c in node_cols else c
        for c in cols
    ]
    conn.execute(
        f"INSERT INTO bbs.{table} ({', '.join(cols)}) "
        f"SELECT {', '.join(exprs)} FROM main.{table}"
    )
    copied, source = _count(conn, "bbs", table), _count(conn, "main", table)
    if copied != source:
        raise RuntimeError(f"BBS split of {table}: copied {copied} of {source} rows")


def _carry_sequence(conn: sqlite3.Connection, table: str) -> None:
    """Raise bbs's AUTOINCREMENT high-water mark to at least main's.

    Runs for every moved table, empty ones included: a fully collected
    mailbox has no rows to copy, yet its reply routes still name old mail
    ids, so a restarted sequence would let REPLY #1 resolve to a stale sender.
    """
    main_seq = conn.execute(
        "SELECT seq FROM main.sqlite_sequence WHERE name = ?", (table,)
    ).fetchone()
    if main_seq is not None:
        _raise_sequence(conn, table, main_seq[0])


def _raise_sequence(conn: sqlite3.Connection, table: str, floor: int) -> None:
    """Make bbs's next AUTOINCREMENT id for `table` exceed `floor`."""
    if floor <= 0:
        return
    bbs_seq = conn.execute(
        "SELECT seq FROM bbs.sqlite_sequence WHERE name = ?", (table,)
    ).fetchone()
    if bbs_seq is None:
        # sqlite_sequence has no unique key, so insert only when absent.
        conn.execute("INSERT INTO bbs.sqlite_sequence(name, seq) VALUES (?, ?)", (table, floor))
    elif bbs_seq[0] < floor:
        conn.execute("UPDATE bbs.sqlite_sequence SET seq = ? WHERE name = ?", (floor, table))


def _conflicts(conn: sqlite3.Connection, table: str, instance_id: str) -> bool:
    """main and bbs both hold rows for `table` and bbs's are not our own copy."""
    moved = conn.execute(
        "SELECT value FROM bbs.bbs_meta WHERE key = ?", (f"moved:{table}",)
    ).fetchone()
    if moved is not None and moved[0] == instance_id:
        return False
    return _count(conn, "main", table) > 0 and _count(conn, "bbs", table) > 0


def _conflict_error(conn: sqlite3.Connection, tables: list[str]) -> SplitConflict:
    return SplitConflict(
        f"cannot move {', '.join(tables)} from {_db_file(conn, 'main')} into shared BBS "
        f"file {_db_file(conn, 'bbs')}: both hold rows; only one instance "
        "may bring existing BBS data"
    )


def assert_not_split(conn: sqlite3.Connection) -> None:
    """Refuse to run single-file on a main whose BBS tables were moved out.

    Without this, dropping `database.bbs_path` after a split would leave
    every BBS command failing on a missing table (legacy migrations are
    already at their final version, so nothing recreates them).
    """
    missing = [t for t in BBS_TABLES if not _has(conn, "main", "table", t)]
    if missing:
        raise SplitConflict(
            f"{_db_file(conn, 'main')} was split into a shared BBS file (no "
            f"{', '.join(missing)}); set database.bbs_path to that bbs.db"
        )


# Game-state tables keyed by `wordle.player_key(sender, day, salt)` rather than
# a raw node id, so NODE_ID_COLUMNS cannot qualify them.
PLAYER_KEY_TABLES: tuple[str, ...] = ("wordle_state", "book_state")


def _rekey_players(conn: sqlite3.Connection, table: str, salt: str) -> None:
    """Re-key a MeshCore instance's copied game rows from the hash of the bare
    id to the hash of the `mc:` id the daemon now plays under.

    The hash is one-way, so candidates come from the ids this instance has
    seen (its contacts and recent senders). A row nobody matches keeps its old
    key and is orphaned, as before. Done here, before any other instance
    shares the file, because a lookup-time fallback to the bare-id key would
    let a forged MeshCore id claim a Meshtastic player's same-day state.

    When both keys already hold a row (a 1.11.0 split, where the player kept
    playing under the new key), the two are merged: the solved row wins, else
    the one with more guesses, and the dropped row's share of that day's
    aggregates is taken back out so the player counts once.
    """
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT node_id FROM main.nodes WHERE node_id LIKE '!%' "
            "UNION SELECT sender_node_id FROM main.processed_requests "
            "WHERE sender_node_id LIKE '!%'"
        )
    ]
    days = [row[0] for row in conn.execute(f"SELECT DISTINCT day FROM bbs.{table}")]
    for day in days:
        for node_id in ids:
            old = player_key(node_id, day, salt)
            new = player_key("mc:" + node_id, day, salt)
            o, n = (
                conn.execute(
                    f"SELECT * FROM bbs.{table} WHERE player_key = ? AND day = ?", (k, day)
                ).fetchone()
                for k in (old, new)
            )
            if o is None:
                continue
            if n is None:
                conn.execute(
                    f"UPDATE bbs.{table} SET player_key = ? WHERE player_key = ? AND day = ?",
                    (new, old, day),
                )
                continue
            _merge_player_rows(conn, table, day, salt, o, n)
            conn.execute(
                f"DELETE FROM bbs.{table} WHERE player_key = ? AND day = ?", (old, day)
            )


def _merge_player_rows(
    conn: sqlite3.Connection, table: str, day: str, salt: str, o: sqlite3.Row, n: sqlite3.Row
) -> None:
    """Fold old-key row `o` into new-key row `n` (same player, same day) and
    undo the double count the duplicate put into the day's aggregates."""
    o_guesses = o["guesses"] if table == "wordle_state" else 0
    n_guesses = n["guesses"] if table == "wordle_state" else 0
    keep_old = (o["solved"], o_guesses) > (n["solved"], n_guesses)
    keep, drop = (o, n) if keep_old else (n, o)
    if table == "wordle_state":
        conn.execute(
            "UPDATE bbs.wordle_state SET guesses = ?, solved = ?, updated_at = ? "
            "WHERE player_key = ? AND day = ?",
            (keep["guesses"], keep["solved"], max(o["updated_at"], n["updated_at"]),
             n["player_key"], day),
        )
        # Each row was counted as a player; a solved one also as a solve.
        conn.execute(
            "UPDATE bbs.wordle_daily SET players = MAX(players - 1, 0), "
            "solves = MAX(solves - ?, 0), "
            "total_guesses_on_solve = MAX(total_guesses_on_solve - ?, 0) WHERE day = ?",
            (drop["solved"], drop["guesses"] if drop["solved"] else 0, day),
        )
    else:
        conn.execute(
            "UPDATE bbs.book_state SET solved = ?, updated_at = ? "
            "WHERE player_key = ? AND day = ?",
            (keep["solved"], max(o["updated_at"], n["updated_at"]), n["player_key"], day),
        )
        if drop["solved"]:
            # Both solved, so the day's book was credited twice.
            conn.execute(
                "UPDATE bbs.book_stats SET solves = MAX(solves - 1, 0) "
                "WHERE book_key = ? AND day = ?",
                (book_key(puzzle_for(day, salt)), day),
            )


def ensure_split(conn: sqlite3.Connection, transport: str, salt: str = "") -> None:
    """Create the shared BBS schema if needed and move this instance's legacy
    BBS tables into it. Idempotent and resumable after a crash at any point.

    The copy and the drop are separate transactions on purpose: in WAL mode a
    transaction spanning two files is not atomic across them, so dropping in
    the same commit could lose the drop's partner copy. A crash between the
    two leaves `moved:<table>` = our instance id, and the next start drops.
    """
    with _immediate(conn):
        # Re-read inside the lock: another daemon may have just created it.
        if conn.execute("PRAGMA bbs.user_version").fetchone()[0] == 0:
            _create_schema(conn)

    instance_id = _instance_id(conn)
    pending = [t for t in BBS_TABLES if _has(conn, "main", "table", t)]
    # Check every table before moving any: a conflict found on a later table
    # after earlier ones were moved and dropped would strand this instance
    # half-merged, with the operator's "who brings the data" choice gone.
    with _immediate(conn):
        conflicts = [t for t in pending if _conflicts(conn, t, instance_id)]
        if conflicts:
            raise _conflict_error(conn, conflicts)
    for table in pending:
        with _immediate(conn):
            moved = conn.execute(
                "SELECT value FROM bbs.bbs_meta WHERE key = ?", (f"moved:{table}",)
            ).fetchone()
            if moved is not None and moved[0] == instance_id:
                pass  # our copy committed before a crash; only the drop remains
            elif _count(conn, "main", table) == 0:
                _carry_sequence(conn, table)  # no rows, but ids already issued
            elif _count(conn, "bbs", table) > 0:
                # Re-checked under this lock: another daemon may have copied
                # in since the pre-pass.
                raise _conflict_error(conn, [table])
            else:
                _copy_table(conn, table, transport)
                _carry_sequence(conn, table)
                if transport == "meshcore" and table in PLAYER_KEY_TABLES:
                    _rekey_players(conn, table, salt)
                conn.execute(
                    "INSERT OR REPLACE INTO bbs.bbs_meta(key, value) VALUES (?, ?)",
                    (f"moved:{table}", instance_id),
                )
        with _immediate(conn):
            if table == "book_stats":
                # The view is defined over book_stats; drop it first so main
                # never holds a view whose table resolves elsewhere.
                for view in BBS_VIEWS:
                    conn.execute(f"DROP VIEW IF EXISTS main.{view}")
            conn.execute(f"DROP TABLE main.{table}")
    with _immediate(conn):
        _run_bbs_migrations(conn, transport, salt, instance_id, moved_now=set(pending))


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM bbs.bbs_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO bbs.bbs_meta(key, value) VALUES (?, ?)", (key, value))


def _migrate_v2_sequences(conn: sqlite3.Connection, instance_id: str) -> None:
    """v2: lift sequences above referenced ids and repoint hijacked routes."""
    # A file already repaired by 1.11.2 carries the marker but still has
    # user_version 1: recognise it and only bump, never run the repair twice.
    if _meta_get(conn, "repair:sequences") is None:
        _repair_sequences(conn)
        _meta_set(conn, "repair:sequences", instance_id)


# (target bbs.user_version, step). Each step is idempotent and runs inside
# the caller's lock together with its version bump.
def _migrate_v3_board_cursors(conn: sqlite3.Connection, instance_id: str) -> None:
    """v3: unread-board cursors for files split before they existed. A fresh
    split already copied them from main's DDL, hence IF NOT EXISTS."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bbs.board_cursors (node_id TEXT NOT NULL, "
        "board TEXT NOT NULL, last_read_id INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
        "PRIMARY KEY(node_id, board))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bbs.board_cursor_pending (node_id TEXT NOT NULL, "
        "packet_id INTEGER NOT NULL, board TEXT NOT NULL, high_id INTEGER NOT NULL, "
        "created_at INTEGER NOT NULL, PRIMARY KEY(node_id, packet_id, board))"
    )


def _migrate_v4_identities(conn: sqlite3.Connection, instance_id: str) -> None:
    """v4: linked-identity tables (accounts.SCHEMA_SQL) for files split
    before they existed; a fresh split already copied them from main."""
    from meshgarden.accounts import SCHEMA_SQL

    # Comments first: they may contain ";".
    for statement in re.sub(r"--[^\n]*", "", SCHEMA_SQL).split(";"):
        body = statement.strip()
        if not body:
            continue
        body = re.sub(r"^CREATE (TABLE|INDEX) ", r"CREATE \1 IF NOT EXISTS bbs.", body)
        conn.execute(body)


BBS_MIGRATIONS = [
    (2, _migrate_v2_sequences),
    (3, _migrate_v3_board_cursors),
    (4, _migrate_v4_identities),
]


def _run_bbs_migrations(
    conn: sqlite3.Connection, transport: str, salt: str, instance_id: str, moved_now: set[str]
) -> None:
    """Versioned repairs for the shared file (1.11.0 split fixes), plus the
    per-instance rekey, which depends on which instance moved the rows and so
    keeps its own marker rather than a file-wide version."""
    current = conn.execute("PRAGMA bbs.user_version").fetchone()[0]
    for version, step in BBS_MIGRATIONS:
        if version > current:
            step(conn, instance_id)
            conn.execute(f"PRAGMA bbs.user_version = {version}")
            current = version
    rekey_mark = f"repair:rekey:{instance_id}"
    if transport != "meshcore" or _meta_get(conn, rekey_mark) is not None:
        return
    for table in PLAYER_KEY_TABLES:
        # Only game rows this instance brought itself carry its bare-id keys;
        # any other bare-id row belongs to a Meshtastic player and must not be
        # handed to a MeshCore id that happens to match. Tables moved in this
        # call were re-keyed during the copy.
        if table not in moved_now and _meta_get(conn, f"moved:{table}") == instance_id:
            _rekey_players(conn, table, salt)
    _meta_set(conn, rekey_mark, "done")


def _repair_sequences(conn: sqlite3.Connection) -> None:
    """Lift sequences above every id still referenced, and repoint reply
    routes that a reused mail id hijacked."""
    # A reused id with the same recipient makes that reader's REPLY #id
    # resolve to the old sender: repoint it at the note they now hold. A
    # reused id to a different recipient leaves the old route alone -- it is
    # still that reader's genuine conversation (lookups match the recipient).
    conn.execute(
        "UPDATE bbs.mail_reply_routes SET sender_node_id = "
        "(SELECT m.sender_node_id FROM bbs.mail AS m WHERE m.id = mail_reply_routes.mail_id) "
        "WHERE EXISTS (SELECT 1 FROM bbs.mail AS m WHERE m.id = mail_reply_routes.mail_id "
        "AND m.recipient_node_id = mail_reply_routes.recipient_node_id "
        "AND m.sender_node_id <> mail_reply_routes.sender_node_id)"
    )
    floors = {
        "mail": conn.execute(
            "SELECT COALESCE(MAX(mail_id), 0) FROM bbs.mail_reply_routes"
        ).fetchone()[0],
        "guestbook": 0,
        "news": 0,
    }
    # NEW cursors hold per-reader high-water ids; ids at or below them would
    # never be announced as new.
    for (snapshot,) in conn.execute(
        "SELECT snapshot FROM bbs.update_cursors UNION ALL SELECT snapshot FROM bbs.update_pending"
    ):
        try:
            marks = json.loads(snapshot)
        except ValueError:
            continue
        if not isinstance(marks, dict):
            continue
        for key, table in (("guestbook", "guestbook"), ("boards", "guestbook"), ("news", "news")):
            value = marks.get(key)
            if isinstance(value, int):
                floors[table] = max(floors[table], value)
    for table, floor in floors.items():
        _raise_sequence(conn, table, floor)
