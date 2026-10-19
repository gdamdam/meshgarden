"""Shared BBS file: the tables both daemons (Meshtastic and MeshCore) serve.

When `database.bbs_path` is set, `database.connect` attaches that file as
schema `bbs` and `database.migrate` calls `ensure_split`, which moves the
BBS tables out of the instance's own `main` file exactly once. Every query
in the codebase stays unqualified: once `main.<table>` is dropped, SQLite's
name resolution (temp, main, then attached schemas) finds `bbs.<table>`.

Observatory tables (nodes, observations, ...) stay per-instance in `main`.
"""

import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

BBS_SCHEMA_VERSION = 1

BBS_TABLES: tuple[str, ...] = (
    "guestbook",
    "news",
    "mail",
    "mail_prefs",
    "mail_aliases",
    "mail_reply_routes",
    "update_cursors",
    "update_pending",
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
    conn.execute(f"PRAGMA bbs.user_version = {BBS_SCHEMA_VERSION}")


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
    # Ids are copied explicitly, but a deleted top id would otherwise be
    # handed out again (a REPLY route keyed on an old mail id must not
    # resolve to a new note).
    conn.execute(
        "UPDATE bbs.sqlite_sequence "
        "SET seq = (SELECT m.seq FROM main.sqlite_sequence AS m WHERE m.name = ?1) "
        "WHERE name = ?1 AND seq < "
        "coalesce((SELECT m.seq FROM main.sqlite_sequence AS m WHERE m.name = ?1), 0)",
        (table,),
    )


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


def ensure_split(conn: sqlite3.Connection, transport: str) -> None:
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
                pass  # nothing to bring
            elif _count(conn, "bbs", table) > 0:
                # Re-checked under this lock: another daemon may have copied
                # in since the pre-pass.
                raise _conflict_error(conn, [table])
            else:
                _copy_table(conn, table, transport)
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
