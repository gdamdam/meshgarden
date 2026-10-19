"""`mgarden upgrade-check`: rehearse an upgrade on snapshots of the live files.

The live databases are opened read-only, only to read versions and to feed
SQLite's online backup API. Everything that migrates, repairs or checks runs
on copies inside a private (0700) temp dir that is always removed, sidecars
included. Nothing here writes to a live file or transmits anything.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass, field

from meshgarden import bbs_store, database, ops

NOTICE = (
    "Rehearsal only: live databases were not modified. "
    "This is not authorization to upgrade."
)

_SEQUENCE_TABLES = {"mail": "id", "guestbook": "entry_id", "news": "id"}


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


def _make_tempdir() -> str:
    # mkdtemp creates the directory with mode 0700.
    return tempfile.mkdtemp(prefix="mgarden-upgrade-")


def _open_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _read_state(path: str) -> tuple[int, set[str]]:
    conn = _open_ro(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        return version, tables
    finally:
        conn.close()


def _snapshot(src: str, dest: str) -> None:
    source = _open_ro(src)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    os.chmod(dest, 0o600)


def _schema_of(conn: sqlite3.Connection, table: str) -> str:
    if bbs_store.is_attached(conn) and bbs_store._has(conn, "bbs", "table", table):
        return "bbs"
    return "main"


def _seq(conn: sqlite3.Connection, schema: str, table: str) -> int:
    row = conn.execute(
        f"SELECT seq FROM {schema}.sqlite_sequence WHERE name = ?", (table,)
    ).fetchone()
    return row[0] if row else 0


def _cursor_floors(conn: sqlite3.Connection) -> dict[str, int]:
    floors = {"guestbook": 0, "news": 0}
    schema = _schema_of(conn, "update_cursors")
    for (snapshot,) in conn.execute(
        f"SELECT snapshot FROM {schema}.update_cursors "
        f"UNION ALL SELECT snapshot FROM {schema}.update_pending"
    ):
        try:
            marks = json.loads(snapshot)
        except (ValueError, TypeError):
            continue
        if not isinstance(marks, dict):
            continue
        for key, table in (("guestbook", "guestbook"), ("boards", "guestbook"), ("news", "news")):
            value = marks.get(key)
            if isinstance(value, int):
                floors[table] = max(floors[table], value)
    return floors


def check_invariants(conn: sqlite3.Connection, transport: str) -> tuple[list[str], list[str]]:
    """Application invariants on a migrated snapshot -> (problems, warnings)."""
    problems: list[str] = []
    warnings: list[str] = []
    cursor = _cursor_floors(conn)
    rschema = _schema_of(conn, "mail_reply_routes")
    for table, pk in _SEQUENCE_TABLES.items():
        schema = _schema_of(conn, table)
        floor = conn.execute(f"SELECT COALESCE(MAX({pk}), 0) FROM {schema}.{table}").fetchone()[0]
        if table == "mail":
            floor = max(
                floor,
                conn.execute(
                    f"SELECT COALESCE(MAX(mail_id), 0) FROM {rschema}.mail_reply_routes"
                ).fetchone()[0],
            )
        floor = max(floor, cursor.get(table, 0))
        seq = _seq(conn, schema, table)
        if seq < floor:
            problems.append(
                f"{table}: sequence {seq} is below referenced id {floor}; "
                "a new row would reuse an id that is still referenced"
            )
    mail_schema = _schema_of(conn, "mail")
    ambiguous = conn.execute(
        f"SELECT r.mail_id FROM {rschema}.mail_reply_routes AS r "
        f"JOIN {mail_schema}.mail AS m ON m.id = r.mail_id "
        "WHERE m.recipient_node_id <> r.recipient_node_id"
    ).fetchall()
    if ambiguous:
        ids = ", ".join(str(r[0]) for r in ambiguous[:10])
        warnings.append(
            f"{len(ambiguous)} reply route(s) name a mail id now held by a different "
            f"recipient (mail ids {ids}); reported only, never repaired or deleted"
        )
    if transport == "meshcore" and bbs_store.is_attached(conn):
        problems.extend(_bare_id_problems(conn))
    return problems, warnings


def _bare_id_problems(conn: sqlite3.Connection) -> list[str]:
    instance = conn.execute(
        "SELECT value FROM main.daemon_state WHERE key = 'instance_id'"
    ).fetchone()
    if instance is None or not bbs_store._has(conn, "main", "table", "nodes"):
        return []
    out = []
    for table, cols in bbs_store.NODE_ID_COLUMNS.items():
        if bbs_store._meta_get(conn, f"moved:{table}") != instance[0]:
            continue  # rows not brought by this instance: bare ids may be Meshtastic's
        for col in cols:
            n = conn.execute(
                f"SELECT count(*) FROM bbs.{table} WHERE {col} IN "
                "(SELECT node_id FROM main.nodes WHERE node_id LIKE '!%')"
            ).fetchone()[0]
            if n:
                out.append(
                    f"bbs.{table}.{col}: {n} bare '!' id(s) of nodes this MeshCore "
                    "instance has seen; they would collide with Meshtastic ids"
                )
    return out


def _check_backup(cfg, report: Report, tmp: str, now: float) -> None:
    names = ops._list_backup_files(cfg.backup.directory)
    if not names:
        report.warnings.append(f"no backup found in {cfg.backup.directory}")
        report.info["backup"] = None
        return
    path = os.path.join(cfg.backup.directory, names[-1])
    age = max(0, int(now - os.path.getmtime(path)))
    copy = os.path.join(tmp, "backup-verify.db")
    verified = False
    try:
        shutil.copyfile(path, copy)
        conn = sqlite3.connect(copy)
        try:
            verified = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        verified = False
    report.info["backup"] = {"path": path, "age_seconds": age, "verified": verified}
    if not verified:
        report.problems.append(f"newest backup {path} does not verify (integrity_check)")


def _inspect(cfg, report: Report) -> tuple[bool, bool]:
    """Read versions from the live files. Returns (main_exists, bbs_exists)."""
    main_path, bbs_path = cfg.database.path, cfg.database.resolved_bbs_path
    main_ok = os.path.isfile(main_path)
    bbs_ok = bool(bbs_path) and os.path.isfile(bbs_path)
    main_tables: set[str] = set()
    report.info["main"] = {"path": main_path, "exists": main_ok, "version": None}
    if main_ok:
        version, main_tables = _read_state(main_path)
        report.info["main"]["version"] = version
        pending = [v for v, _ in database.MIGRATIONS if v > version]
        report.info["main"]["pending"] = pending
    else:
        report.warnings.append(f"main database {main_path} not found (fresh install?)")
    if bbs_path:
        report.info["bbs"] = {"path": bbs_path, "exists": bbs_ok, "version": None}
        if bbs_ok:
            version, bbs_tables = _read_state(bbs_path)
            report.info["bbs"]["version"] = version
            report.info["bbs"]["pending"] = [
                v for v, _ in bbs_store.BBS_MIGRATIONS if v > version
            ]
            if version == 0 and main_ok and not set(bbs_store.BBS_TABLES) <= main_tables:
                report.problems.append(
                    f"{bbs_path} is uninitialized but {main_path} lacks BBS tables; "
                    "restore the shared BBS file"
                )
        else:
            report.warnings.append(f"shared BBS file {bbs_path} not found; it would be created")
            if main_ok and not set(bbs_store.BBS_TABLES) <= main_tables:
                report.problems.append(
                    f"{main_path} was split earlier but {bbs_path} is missing; restore it"
                )
    # main split but bbs_path unset is caught by the rehearsal (assert_not_split).
    peer = cfg.database.peer_path
    if peer:
        report.info["peer"] = {"path": peer, "exists": os.path.isfile(peer)}
        if not os.path.isfile(peer):
            report.warnings.append(f"peer database {peer} not found")
    return main_ok, bbs_ok


def _rehearse(cfg, report: Report, tmp: str, main_ok: bool, bbs_ok: bool) -> None:
    snap_main = os.path.join(tmp, "observatory.db")
    snap_bbs = os.path.join(tmp, "bbs.db") if cfg.database.resolved_bbs_path else None
    if main_ok:
        _snapshot(cfg.database.path, snap_main)
    if snap_bbs and bbs_ok:
        _snapshot(cfg.database.resolved_bbs_path, snap_bbs)
    transport = cfg.radio.transport
    conn = database.connect(snap_main, bbs_path=snap_bbs, transport=transport)
    try:
        try:
            database.migrate(conn, transport=transport, salt=cfg.service.fortune_salt)
        except Exception as exc:  # any failure is the finding
            report.problems.append(f"migration rehearsal failed: {type(exc).__name__}: {exc}")
            return
        report.info["rehearsal"] = {
            "main_version": database.get_schema_version(conn),
            "bbs_version": (
                conn.execute("PRAGMA bbs.user_version").fetchone()[0] if snap_bbs else None
            ),
        }
        schemas = ["main"] + (["bbs"] if snap_bbs else [])
        for schema in schemas:
            result = conn.execute(f"PRAGMA {schema}.integrity_check").fetchone()[0]
            if result != "ok":
                report.problems.append(f"{schema} integrity_check after rehearsal: {result}")
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            report.problems.append(f"foreign_key_check: {len(fk)} violation(s) after rehearsal")
        problems, warnings = check_invariants(conn, transport)
        report.problems.extend(problems)
        report.warnings.extend(warnings)
    finally:
        conn.close()


def preflight(cfg, now: float | None = None) -> Report:
    report = Report()
    now = time.time() if now is None else now
    tmp = _make_tempdir()
    try:
        try:
            main_ok, bbs_ok = _inspect(cfg, report)
            if not report.problems:
                _rehearse(cfg, report, tmp, main_ok, bbs_ok)
            _check_backup(cfg, report, tmp, now)
        except (OSError, sqlite3.Error) as exc:
            report.problems.append(f"preflight failed: {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return report


def render(report: Report) -> str:
    lines = []
    for key in ("main", "bbs", "peer"):
        item = report.info.get(key)
        if item is None:
            continue
        state = "present" if item["exists"] else "MISSING"
        version = item.get("version")
        extra = f" schema v{version}" if version is not None else ""
        pending = item.get("pending")
        if pending:
            extra += f", pending migrations: {pending}"
        lines.append(f"{key}: {item['path']} ({state}{extra})")
    reh = report.info.get("rehearsal")
    if reh:
        lines.append(f"rehearsal reached main v{reh['main_version']}, bbs v{reh['bbs_version']}")
    backup = report.info.get("backup")
    if backup:
        lines.append(
            f"newest backup: {backup['path']} ({backup['age_seconds']}s old, "
            f"{'verified' if backup['verified'] else 'NOT VERIFIED'})"
        )
    lines.extend(f"WARNING: {w}" for w in report.warnings)
    lines.extend(f"PROBLEM: {p}" for p in report.problems)
    lines.append("result: " + ("ok" if report.ok else "problems found"))
    lines.append(NOTICE)
    return "\n".join(lines)


def run(cfg, as_json: bool = False) -> int:
    report = preflight(cfg)
    if as_json:
        print(json.dumps({**asdict(report), "ok": report.ok, "notice": NOTICE}, indent=2))
    else:
        print(render(report))
    return 0 if report.ok else 1
