"""Operator helpers: guestbook export, backup restore, health checks, and log
following.

Every function here is pure and testable in isolation -- no argparse, no
`print`, no `sys.exit`. A later step wires these into `mgarden` subcommands
(and picks the CLI's usual exit-code convention for them); this module only
owns the logic.

Nothing here opens the radio device (see `_check_device` below: it only
`os.stat`/`os.access`s the configured path, never `open`s it) and nothing
here deletes a backup source file.
"""

from __future__ import annotations

import contextlib
import csv
import errno
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from meshgarden.bbs_store import BBS_SCHEMA_VERSION, BBS_TABLES
from meshgarden.config import NODE_ID_RE
from meshgarden.database import SCHEMA_VERSION
from meshgarden.guestbook import clean_display_name

if TYPE_CHECKING:
    from collections.abc import Iterator
    from threading import Event

    from meshgarden.config import BackupConfig, Config, MetricsConfig, RetentionConfig

# =============================================================================
# CLI --topic normalization
# =============================================================================

# Same topic shape as guestbook.py's own `_TOPIC_RE` (minus the optional
# leading '#', which is stripped before this is checked).
_CLI_TOPIC_RE = re.compile(r"^[a-z0-9]{2,12}$")


def _normalize_cli_topic(raw: str | None) -> str | None:
    """None (omitted --topic) passes through unchanged -- main guestbook
    default (ruling 9). A given value is normalized
    strip().removeprefix('#').lower() and validated against the topic
    shape ^[a-z0-9]{2,12}$; raises ValueError if it doesn't match."""
    if raw is None:
        return None
    normalized = raw.strip().removeprefix("#").lower()
    if not _CLI_TOPIC_RE.match(normalized):
        raise ValueError(f"invalid topic: {raw!r}")
    return normalized


# =============================================================================
# export_guestbook
# =============================================================================

_EXPORT_FIELDS = (
    "id",
    "created_at",
    "sender_node_id",
    "display_name",
    "visible",
    "moderation_reason",
    "message",
)


def _export_rows(
    conn: sqlite3.Connection, *, include_hidden: bool, topic: str | None = None
) -> list[dict]:
    clauses = [] if include_hidden else ["visible=1"]
    clauses.append("topic IS ?")
    where = "WHERE " + " AND ".join(clauses) + " "
    rows = conn.execute(
        "SELECT entry_id, created_at, sender_node_id, display_name, message, "
        "visible, moderation_reason, nodes.short_name, nodes.long_name FROM guestbook "
        f"LEFT JOIN nodes ON nodes.node_id=bbs_local_id(guestbook.sender_node_id) {where}"
        "ORDER BY entry_id ASC",
        (topic,),
    ).fetchall()
    out = []
    for (
        entry_id,
        created_at,
        sender_node_id,
        display_name,
        message,
        visible,
        moderation_reason,
        current_short_name,
        current_long_name,
    ) in rows:
        out.append(
            {
                "id": entry_id,
                "created_at": datetime.fromtimestamp(created_at, tz=UTC).isoformat(),
                "sender_node_id": sender_node_id,
                "display_name": clean_display_name(
                    current_short_name, current_long_name, display_name
                ),
                "visible": bool(visible),
                "moderation_reason": moderation_reason,
                "message": message,
            }
        )
    return out


def export_guestbook(
    conn: sqlite3.Connection, fmt: str, *, include_hidden: bool = False, topic: str | None = None
) -> str:
    """Exports guestbook entries as `"csv"` or `"json"`.

    Columns/keys (in order): id, created_at (UTC ISO-8601, e.g.
    "2024-01-01T12:00:00+00:00"), sender_node_id, display_name, visible,
    moderation_reason, message. Visible entries only unless `include_hidden`
    -- same convention as `Guestbook.list_entries`. Newest-last (entry_id
    ASC) so an export is stable/appendable across runs.

    `topic` filters to that board's entries (matched with SQLite's
    null-safe `IS`, same convention as `Guestbook.list_entries`); omitted
    (None, the default) means the main guestbook only (`topic IS NULL`),
    never "all topics" (ruling 9) -- a board post never silently leaks
    into the default export.

    CSV is built with the `csv` module (proper quoting of commas, quotes,
    and embedded newlines); JSON is a plain list of objects, UTF-8 with
    Unicode characters left unescaped.
    """
    if fmt not in ("csv", "json"):
        raise ValueError(f"unsupported export format: {fmt!r} (expected 'csv' or 'json')")

    rows = _export_rows(conn, include_hidden=include_hidden, topic=topic)

    if fmt == "json":
        return json.dumps(rows, ensure_ascii=False)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_EXPORT_FIELDS)
    for r in rows:
        writer.writerow(
            [
                r["id"],
                r["created_at"],
                r["sender_node_id"],
                r["display_name"] or "",
                "true" if r["visible"] else "false",
                r["moderation_reason"] or "",
                r["message"],
            ]
        )
    return buf.getvalue()


# =============================================================================
# restore_backup
# =============================================================================


@dataclass(frozen=True)
class RestoreReport:
    backup_path: str
    target_db_path: str
    ok: bool
    error: str | None
    backup_user_version: int | None
    guestbook_row_count: int | None
    # Paths that could not be cleaned up / put back and are left on disk
    # under a ".pre-restore" name -- always bounded (at most the target db
    # plus its two sidecars), never a reason by itself to treat `ok` as
    # meaning "and nothing needs operator attention".
    leftover_files: list[str] = field(default_factory=list)


def _copy_backup_api(source: sqlite3.Connection, dest_path: str) -> None:
    """Copies `source` into a fresh sqlite3 file at `dest_path` using the
    sqlite3 online backup API (same approach as `retention.Retention.backup`)
    -- consistent even if `source` were being written elsewhere. A small,
    separately-named seam so tests can monkeypatch it to simulate a copy
    that silently loses rows, without needing an actually-broken backup
    run."""
    dest = sqlite3.connect(dest_path)
    try:
        source.backup(dest)
    finally:
        dest.close()


def _sidecar_paths(db_path: str) -> tuple[str, str]:
    return (db_path + "-wal", db_path + "-shm")


def _fail(
    backup_path: str,
    target_db_path: str,
    error: str,
    *,
    leftover_files: list[str] | tuple[str, ...] = (),
) -> RestoreReport:
    return RestoreReport(
        backup_path=backup_path,
        target_db_path=target_db_path,
        ok=False,
        error=error,
        backup_user_version=None,
        guestbook_row_count=None,
        leftover_files=list(leftover_files),
    )


# Tables created by migration 1 that stay in the per-instance file -- present
# in every schema version this app has ever shipped (SCHEMA_VERSION >= 1). An
# unrelated-but-valid sqlite file (some other app's database, or a backup
# taken before migration 1) passes `PRAGMA integrity_check` without ever
# containing these; checking for them up front is what tells the two apart
# before anything is copied. A legacy single-file backup also carries the BBS
# tables, a split instance's main does not, so those are not required here.
_REQUIRED_MAIN_TABLES = (
    "nodes",
    "observations",
    "processed_requests",
    "daily_stats",
)
_REQUIRED_BBS_TABLES = (*BBS_TABLES, "bbs_meta")


def _backup_kind(tables: set[str]) -> str | None:
    """"main" (observatory file, legacy single-file ones included), "bbs"
    (shared BBS file) or None. `observations` decides: a legacy backup holds
    both `observations` and `guestbook`, and restores as main."""
    if "observations" in tables:
        return "main"
    if "guestbook" in tables:
        return "bbs"
    return None


def backup_kind(path: str) -> str | None:
    """Kind of the backup file at `path` (see `_backup_kind`), None when it
    cannot be read or is neither."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return _backup_kind(_table_names(conn))
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _validate_source_schema(source: sqlite3.Connection) -> str | None:
    """Returns a bounded reason string if `source` is not a Meshgarden
    application database this version knows how to restore, else None.

    Checked BEFORE any copy: `PRAGMA integrity_check` alone only proves
    `source` is *a* valid sqlite file, not that it is *our* schema, so an
    unrelated-but-valid sqlite file must be caught here rather than
    discovered only after it has already replaced the target.
    """
    existing = _table_names(source)
    kind = _backup_kind(existing)
    # An unrecognized file is reported against the main tables, the common case.
    required, newest = (
        (_REQUIRED_BBS_TABLES, BBS_SCHEMA_VERSION)
        if kind == "bbs"
        else (_REQUIRED_MAIN_TABLES, SCHEMA_VERSION)
    )
    user_version = source.execute("PRAGMA user_version").fetchone()[0]
    if not (1 <= user_version <= newest):
        return (
            f"unsupported schema version: user_version={user_version} "
            f"(expected 1..{newest})"
        )
    missing = sorted(set(required) - existing)
    if missing:
        return f"backup is missing required table(s): {missing}"
    return None


def _bbs_rows_in(path: str) -> list[str]:
    """BBS tables of the shared file at `path` that hold rows ([] when the
    file is missing or empty)."""
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        present = _table_names(conn)
        return [
            t
            for t in BBS_TABLES
            if t in present and conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
        ]
    finally:
        conn.close()


def restore_backup(
    backup_path: str,
    target_db_path: str,
    *,
    overwrite: bool = False,
    bbs_path: str | None = None,
) -> RestoreReport:
    """Restores `backup_path` (an on-disk sqlite file, e.g. one produced by
    `retention.Retention.backup`) to `target_db_path`.

    Restores whichever kind the file is (see `backup_kind`): the caller picks
    `target_db_path` accordingly (main file or the shared bbs file). A legacy
    single-file backup restores as main even into a split install; the next
    daemon start re-runs `bbs_store.ensure_split` to move its BBS tables out.
    With `bbs_path` (the configured shared file) such a restore is refused
    when that file already holds BBS rows, since the split would conflict.

    Refuses (returning a not-`ok` report, never raising for an expected
    refusal) when:
      - `target_db_path` already exists and `overwrite` is False.
      - a `-wal`/`-shm` sidecar of `target_db_path` exists and `overwrite`
        is False (a daemon is likely running against that database).
      - `backup_path` cannot be opened, fails `PRAGMA integrity_check`, or
        is not a valid sqlite database.
      - `backup_path` is a structurally valid sqlite file but not a
        Meshgarden application database: its `PRAGMA user_version` is not
        in `1..SCHEMA_VERSION`, or it is missing one of the tables created
        by migration 1 (see `_REQUIRED_SOURCE_TABLES`). Checked BEFORE any
        copy, so an unrelated-but-valid sqlite file can never replace the
        target -- `PRAGMA integrity_check` passing only proves the file is
        *a* valid sqlite database, not *our* schema.

    Any other unexpected exception encountered after the backup has been
    validated (this includes `os.makedirs`/`tempfile.mkstemp` failures,
    not just the copy/swap steps that follow them) is also caught and
    turned into a bounded `ok=False` report (`error` names the exception
    type) rather than propagating -- the target is never modified before
    the final `os.replace`, so it is guaranteed to be left untouched by
    any failure up to that point.

    Never deletes or modifies `backup_path` -- the copy is one-directional,
    backup -> target, via the sqlite3 online backup API. When `overwrite`
    is True, the existing target (and its `-wal`/`-shm` sidecars, if any)
    are moved aside -- never deleted outright -- before the (atomic,
    same-directory-tmp-then-`os.replace`) copy lands: if anything fails
    between that move and the final `os.replace`, the moved-aside files
    are put back so a leftover WAL from the file being replaced is never
    lost, only ever replaced once the swap actually succeeds. (When
    `overwrite` is True, a best-effort `PRAGMA wal_checkpoint(TRUNCATE)`
    is first run against the existing target to fold its own WAL into its
    main file before the move, so restoring over a database whose daemon
    is NOT running leaves nothing behind to move aside at all -- the
    daemon must not be running against `target_db_path` during an
    overwrite restore, since a live WAL writer defeats that checkpoint.)

    After the copy, `guestbook` row counts in the backup and the restored
    copy are compared; a mismatch is reported (`ok=False`) rather than
    silently accepted. On mismatch, the temporary copy is discarded and
    the original target (if it exists) is left untouched.

    `RestoreReport.leftover_files` names any ".pre-restore" aside file
    that could not be cleaned up (on success) or moved back to its
    original name (on failure); on a failed move-back, the same path is
    also folded into `error` so the operator isn't left checking a
    separate field to learn that data was parked under the wrong name.

    `ok=True` guarantees only that the swap itself landed: `target_db_path`
    now holds the copied backup, byte-for-byte (modulo SQLite's own fresh
    "-wal"/"-shm" pair), and any moved-aside original has been removed or
    is named in `leftover_files`. It does NOT guarantee that
    `RestoreReport.guestbook_row_count` is populated: the row count is read
    back from the target purely for reporting, after the swap has already
    succeeded, and a failure in that last read-back (e.g. a permission
    error) is caught and folded into a bounded `error` note rather than
    turning an already-successful restore into `ok=False` -- in that case
    `guestbook_row_count` is `None`.
    """
    if not os.path.exists(backup_path):
        return _fail(backup_path, target_db_path, f"backup not found: {backup_path}")

    if os.path.exists(target_db_path) and not overwrite:
        return _fail(
            backup_path,
            target_db_path,
            f"target already exists: {target_db_path} (pass --yes to overwrite)",
        )

    existing_sidecars = [p for p in _sidecar_paths(target_db_path) if os.path.exists(p)]
    if existing_sidecars and not overwrite:
        return _fail(
            backup_path,
            target_db_path,
            f"target has a WAL/SHM sidecar (daemon likely running?): {existing_sidecars[0]} "
            "(pass --yes to overwrite)",
        )

    try:
        source = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        return _fail(backup_path, target_db_path, f"cannot open backup: {exc}")

    try:
        try:
            integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            return _fail(backup_path, target_db_path, f"not a valid sqlite database: {exc}")
        if integrity != "ok":
            return _fail(backup_path, target_db_path, f"integrity check failed: {integrity}")

        schema_error = _validate_source_schema(source)
        if schema_error is not None:
            return _fail(backup_path, target_db_path, schema_error)

        backup_user_version = source.execute("PRAGMA user_version").fetchone()[0]
        # A split instance's main backup has no guestbook: nothing to compare.
        has_guestbook = "guestbook" in _table_names(source)
        source_guestbook_count = (
            source.execute("SELECT COUNT(*) FROM guestbook").fetchone()[0]
            if has_guestbook
            else None
        )
        if has_guestbook and bbs_path and backup_kind(backup_path) == "main":
            conflicts = _bbs_rows_in(bbs_path)
            if conflicts:
                return _fail(
                    backup_path,
                    target_db_path,
                    f"legacy single-file backup would bring {', '.join(conflicts)} into "
                    f"shared BBS file {bbs_path}, which already holds rows (SplitConflict); "
                    "restore a bbs-*.db backup instead or empty the shared file",
                )

        # Guarded like the copy step below: a failure here (e.g. the
        # target's directory can't be created, or the filesystem refuses
        # a temp file) must also come back as a bounded ok=False report
        # instead of propagating -- nothing has touched `target_db_path`
        # yet at this point, so there is nothing to undo.
        try:
            dirname = os.path.dirname(os.path.abspath(target_db_path))
            if dirname:
                os.makedirs(dirname, exist_ok=True)

            # Use a unique temp name via tempfile.mkstemp; close the fd
            # immediately since we only need the name.
            tmp_fd, tmp_dest = tempfile.mkstemp(
                dir=dirname or ".", prefix="observatory-restore-", suffix=".tmp"
            )
            os.close(tmp_fd)
        except Exception as exc:
            return _fail(
                backup_path, target_db_path, f"restore setup failed: {type(exc).__name__}: {exc}"
            )

        try:
            _copy_backup_api(source, tmp_dest)
        except (OSError, sqlite3.Error) as exc:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_dest)
            return _fail(backup_path, target_db_path, f"copy failed: {exc}")
    finally:
        source.close()

    # Verify row counts in the copied file BEFORE any destructive operations.
    # A plain connection (not mode=ro) is needed because the copied file may be
    # in WAL mode and read-only opens don't properly handle WAL state.
    #
    # Everything from here through `os.replace` below is wrapped so that ANY
    # unexpected exception (not just the ones explicitly checked for) is
    # turned into a bounded ok=False report rather than escaping and
    # potentially leaving a partially-processed tmp file around or -- worse
    # -- an inconsistent target: the target is never modified before
    # `os.replace`, so any exception raised before that line is guaranteed
    # to leave it untouched.
    # (aside, original) pairs for anything moved out of the way below --
    # restored on any failure so a partial/failed swap never loses data
    # that was only ever committed to the pre-existing target's WAL.
    preserved: list[tuple[str, str]] = []
    try:
        tmp_target = sqlite3.connect(tmp_dest)
        try:
            tmp_guestbook_count = (
                tmp_target.execute("SELECT COUNT(*) FROM guestbook").fetchone()[0]
                if has_guestbook
                else None
            )
        finally:
            tmp_target.close()

        # If row counts don't match, discard the bad copy and leave the target untouched.
        if tmp_guestbook_count != source_guestbook_count:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_dest)
            return RestoreReport(
                backup_path=backup_path,
                target_db_path=target_db_path,
                ok=False,
                error=(
                    f"row count mismatch: backup guestbook={source_guestbook_count} "
                    f"restored copy guestbook={tmp_guestbook_count}"
                ),
                backup_user_version=backup_user_version,
                guestbook_row_count=tmp_guestbook_count,
            )

        if overwrite:
            # Best-effort: fold the *existing* target's own WAL into its
            # main file before it gets moved aside, so there is as little
            # as possible left to move at all. If a daemon is (against
            # the documented requirement) still holding the database open,
            # this checkpoint simply won't apply cleanly and is ignored --
            # the move-aside-then-restore-on-failure sequence below still
            # protects whatever ends up on disk either way.
            if os.path.exists(target_db_path):
                with contextlib.suppress(sqlite3.Error, OSError):
                    ckpt_conn = sqlite3.connect(target_db_path, timeout=1)
                    try:
                        ckpt_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    finally:
                        ckpt_conn.close()

            # Move the existing target and its sidecars aside instead of
            # deleting them outright: if the swap below fails partway,
            # they are moved back so committed data -- including anything
            # left only in a WAL -- is never lost even though the restore
            # itself is reported as a failure.
            for p in (target_db_path, *_sidecar_paths(target_db_path)):
                if os.path.exists(p):
                    aside = p + ".pre-restore"
                    os.replace(p, aside)
                    preserved.append((aside, p))

        os.chmod(tmp_dest, 0o600)
        os.replace(tmp_dest, target_db_path)  # atomic: only a complete copy gets the final name
    except Exception as exc:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_dest)
        # Move each aside file back independently -- one failing must not
        # stop the others from being restored -- but a failure here is no
        # longer swallowed: the aside path is surfaced in both `error`
        # (so it's visible without inspecting a separate field) and
        # `leftover_files`, since it means that original data is now
        # parked under its ".pre-restore" name instead of its real one.
        move_back_failed: list[str] = []
        for aside, original in preserved:
            try:
                os.replace(aside, original)
            except OSError:
                move_back_failed.append(aside)
        error = f"restore failed: {exc}"
        if move_back_failed:
            error += (
                f"; failed to move back {move_back_failed} -- "
                "original data is parked at the listed path(s) under a "
                "'.pre-restore' name, not lost"
            )
        return _fail(backup_path, target_db_path, error, leftover_files=move_back_failed)

    # Swap succeeded: the pre-restore originals are no longer needed. A
    # failure to remove one is no longer silently swallowed beyond
    # FileNotFoundError -- it's recorded as a leftover so the operator
    # knows a ".pre-restore" file is still sitting next to the restored
    # target (harmless, but not nothing).
    leftover_files: list[str] = []
    for aside, _original in preserved:
        try:
            os.remove(aside)
        except FileNotFoundError:
            pass
        except OSError:
            leftover_files.append(aside)

    # NOT opened via mode=ro: a WAL-mode database (journal_mode is part of
    # the restored file's own header, copied verbatim from the backup) can
    # fail to open read-only when it has no "-wal" file yet -- SQLite still
    # wants to create the "-shm" index and a read-only connection can't.
    # A plain connection legitimately leaves the just-restored target with
    # its own fresh "-wal"/"-shm" pair; that's normal SQLite WAL behavior,
    # not leftover state from the restore itself.
    #
    # By this point the swap has already fully succeeded -- target_db_path
    # is the just-restored file, and every ".pre-restore" aside has already
    # been cleaned up or recorded in `leftover_files` above. This read-back
    # is purely informational (populating `guestbook_row_count`), so a
    # failure here (a stray permission error, the filesystem hiccuping)
    # must not turn an already-successful restore into a reported failure:
    # it is caught and folded into a bounded note in `error` instead, with
    # `guestbook_row_count` left `None`.
    target_guestbook_count: int | None = None
    read_back_error: str | None = None
    try:
        target = sqlite3.connect(target_db_path)
        try:
            if has_guestbook:
                target_guestbook_count = target.execute(
                    "SELECT COUNT(*) FROM guestbook"
                ).fetchone()[0]
        finally:
            target.close()
    except Exception as exc:
        read_back_error = (
            "restore succeeded (target already replaced) but the post-restore "
            f"read-back count failed: {type(exc).__name__}: {exc}"
        )

    return RestoreReport(
        backup_path=backup_path,
        target_db_path=target_db_path,
        ok=True,
        error=read_back_error,
        backup_user_version=backup_user_version,
        guestbook_row_count=target_guestbook_count,
        leftover_files=leftover_files,
    )


# =============================================================================
# doctor
# =============================================================================


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str  # bounded, human-readable; never a secret (no salts/tokens)


# Duplicated from main.py's own `_STATUS_FILE_NAME` / `_STATUS_FILE_STALE_SECONDS`
# rather than imported: main.py is expected to import THIS module once a later
# step wires `mgarden` subcommands, so ops.py must not import main.py back.
_STATUS_FILE_NAME = "status.json"
_STATUS_FILE_STALE_SECONDS = 60


def _database_dir(cfg: Config) -> str:
    return os.path.dirname(os.path.abspath(cfg.database.path))


def _check_identity(cfg: Config) -> Check:
    node_id = cfg.radio.expected_node_id
    if not NODE_ID_RE.match(node_id):
        return Check(
            "identity", False, f"expected_node_id {node_id!r} does not match {NODE_ID_RE.pattern!r}"
        )
    return Check("identity", True, f"expected_node_id={node_id}")


def _check_meshcore(cfg: Config) -> Check:
    """Reports the MeshCore transport's configuration state. Never opens
    the device, never connects, never imports `meshcore` itself (only
    checks whether it COULD be imported via `importlib.util.find_spec`,
    which does not execute the package) -- consistent with `doctor`'s
    read-only contract. `ok=True` and a short "not configured" detail when
    `[radio] transport` is still `"meshtastic"`, so this check is silent
    for every existing (Meshtastic-only) installation.
    """
    if cfg.radio.transport != "meshcore":
        return Check("meshcore", True, "transport=meshtastic; meshcore not configured")
    installed = importlib.util.find_spec("meshcore") is not None
    channel = cfg.radio.meshcore_channel
    channel_detail = f"channel={channel!r}" if channel else "channel triggering disabled"
    detail = (
        f"package={'installed' if installed else 'MISSING'}; {channel_detail}; "
        f"autoadd_overwrite={cfg.radio.meshcore_autoadd_overwrite}"
    )
    if not installed:
        detail += (
            " (install with `uv sync --extra meshcore` or "
            "`pip install 'meshgarden[meshcore]'`)"
        )
    return Check("meshcore", installed, detail)


def _check_device(device: str) -> Check:
    """Never opens the device -- only stats/checks permission bits, so
    `doctor` can run safely while the daemon holds the serial port open."""
    try:
        st = os.stat(device)
    except OSError as exc:
        return Check("radio_device", False, f"{device}: cannot stat ({exc.strerror or exc})")
    if not stat.S_ISCHR(st.st_mode):
        return Check("radio_device", False, f"{device}: not a character device")
    if not os.access(device, os.R_OK | os.W_OK):
        return Check("radio_device", False, f"{device}: not readable/writable by current user")
    return Check("radio_device", True, f"{device}: character device, readable and writable")


def _check_db_dir(db_dir: str) -> Check:
    if not os.path.isdir(db_dir):
        return Check("database_dir", False, f"{db_dir}: does not exist")
    if not os.access(db_dir, os.W_OK):
        return Check("database_dir", False, f"{db_dir}: not writable")
    return Check("database_dir", True, f"{db_dir}: exists and writable")


def _check_db_integrity(db_path: str, name: str = "database_integrity") -> Check:
    if not os.path.exists(db_path):
        return Check(name, True, "database not yet created")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        return Check(name, False, f"cannot open database: {exc}")
    try:
        try:
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            return Check(name, False, f"quick_check failed: {exc}")
    finally:
        conn.close()
    if result != "ok":
        return Check(name, False, f"quick_check: {result}")
    return Check(name, True, "quick_check: ok")


def _check_schema_version(
    db_path: str, name: str = "schema_version", expected: int = SCHEMA_VERSION
) -> Check:
    if not os.path.exists(db_path):
        return Check(name, True, "database not yet created")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return Check(name, False, f"unreadable: {exc}")
    try:
        try:
            actual = conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error as exc:
            return Check(name, False, f"unreadable: {exc}")
    finally:
        conn.close()
    if actual != expected:
        return Check(name, False, f"user_version={actual} expected={expected}")
    return Check(name, True, f"user_version={actual}")


def _check_peer_observatory(cfg: Config) -> Check:
    """Readable peer observatory, or "not configured". A peer whose
    `instance_id` equals ours is this very file: the peer view would then
    show our own mesh as the other one."""
    peer = cfg.database.peer_path
    if not peer:
        return Check("peer_observatory", True, "not configured")
    if not os.path.exists(peer):
        # The other daemon may simply not have started yet.
        return Check(
            "peer_observatory", True, f"{peer}: peer not created yet (other daemon not started)"
        )

    def instance_id(path: str) -> str | None:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM daemon_state WHERE key = 'instance_id'"
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    try:
        theirs = instance_id(peer)
        ours = instance_id(cfg.database.path) if os.path.exists(cfg.database.path) else None
    except sqlite3.Error as exc:
        return Check("peer_observatory", False, f"{peer}: unreadable: {exc}")
    if theirs is not None and theirs == ours:
        return Check(
            "peer_observatory",
            False,
            f"{peer} is this instance's own database (same instance_id): "
            "database.peer_path must point at the other mesh's file",
        )
    return Check("peer_observatory", True, f"{peer}: readable")


def _check_disk_headroom(db_dir: str, retention: RetentionConfig) -> Check:
    cap_bytes = retention.max_managed_storage_mib * 1024 * 1024
    probe_dir = db_dir if os.path.isdir(db_dir) else (os.path.dirname(db_dir) or ".")
    try:
        free = shutil.disk_usage(probe_dir).free
    except OSError as exc:
        return Check("disk_headroom", False, f"cannot stat filesystem for {probe_dir}: {exc}")
    if free < cap_bytes:
        return Check(
            "disk_headroom", False, f"free={free} bytes < managed cap={cap_bytes} bytes"
        )
    return Check("disk_headroom", True, f"free={free} bytes >= managed cap={cap_bytes} bytes")


def _check_backup_dir(backup: BackupConfig) -> Check:
    if not os.path.isdir(backup.directory):
        return Check("backup_dir", False, f"{backup.directory}: does not exist")
    if not os.access(backup.directory, os.W_OK):
        return Check("backup_dir", False, f"{backup.directory}: not writable")
    return Check("backup_dir", True, f"{backup.directory}: exists and writable")


def _list_backup_files(directory: str) -> list[str]:
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(n for n in names if n.startswith("observatory-") and not n.endswith(".tmp"))


def _check_backup_age(backup: BackupConfig, now: int) -> Check:
    names = _list_backup_files(backup.directory)
    if not names:
        return Check("backup_age", False, "no backups found")
    latest = names[-1]
    try:
        mtime = os.path.getmtime(os.path.join(backup.directory, latest))
    except OSError as exc:
        return Check("backup_age", False, f"cannot stat {latest}: {exc}")
    age_seconds = max(0, int(now - mtime))
    limit_seconds = backup.interval_hours * 3600
    ok = age_seconds <= limit_seconds
    return Check(
        "backup_age",
        ok,
        f"latest={latest} age_seconds={age_seconds} interval_seconds={limit_seconds}",
    )


def _check_log_dir(db_dir: str) -> Check:
    log_dir = os.path.join(db_dir, "logs")
    if not os.path.isdir(log_dir):
        return Check("log_dir", False, f"{log_dir}: does not exist")
    try:
        names = os.listdir(log_dir)
    except OSError as exc:
        return Check("log_dir", False, f"{log_dir}: cannot list ({exc})")
    total_bytes = 0
    for name in names:
        with contextlib.suppress(OSError):
            total_bytes += os.path.getsize(os.path.join(log_dir, name))
    return Check("log_dir", True, f"{log_dir}: {len(names)} file(s), {total_bytes} bytes")


def _check_timezone(tz: str) -> Check:
    try:
        ZoneInfo(tz)
    except Exception as exc:
        return Check("timezone", False, f"{tz!r} is not a known IANA zone: {exc}")
    return Check("timezone", True, f"{tz}: valid")


def _check_status_file(db_dir: str, now: int) -> Check:
    path = os.path.join(db_dir, _STATUS_FILE_NAME)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return Check("status_file", False, f"{path}: missing (daemon not running?)")
    age_seconds = max(0, int(now - mtime))
    ok = age_seconds <= _STATUS_FILE_STALE_SECONDS
    return Check(
        "status_file", ok, f"age_seconds={age_seconds} stale_after={_STATUS_FILE_STALE_SECONDS}"
    )


def _check_worker_heartbeats(db_dir: str) -> Check:
    """Reports any worker (db/tx/radio/maintenance) whose heartbeat has
    gone stale, per status.json's own `workers` field -- the same
    `stalled` flag `Daemon.status_snapshot()`/`Heartbeats.snapshot()`
    already compute. Never recomputes staleness itself (the age/threshold
    logic lives with the daemon, next to where each heartbeat is produced)
    -- this only reads and reports what the daemon already decided.

    A missing/unreadable status file, or one with no `workers` field yet
    (an older daemon build, or one that hasn't written a status file yet),
    is reported `ok=True` here: that condition is `_check_status_file`'s
    job, not this check's -- reporting it twice as two failures for the
    same root cause would only be confusing.
    """
    path = os.path.join(db_dir, _STATUS_FILE_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return Check("worker_heartbeats", True, "status file unavailable; nothing to report")
    workers = payload.get("workers")
    if not workers:
        return Check("worker_heartbeats", True, "no worker heartbeat data in status file")
    stalled = sorted(name for name, info in workers.items() if info.get("stalled"))
    if stalled:
        return Check("worker_heartbeats", False, f"stalled: {', '.join(stalled)}")
    return Check("worker_heartbeats", True, f"all workers healthy: {', '.join(sorted(workers))}")


def _check_metrics_port(metrics: MetricsConfig, *, daemon_alive: bool) -> Check:
    """Binds (and immediately closes) the configured metrics listen/port --
    never leaves anything listening. If the port is taken and the daemon
    this config belongs to is already running (per `_check_status_file`'s
    liveness, passed in as `daemon_alive`), that's its own metrics server
    holding the port, not a conflict -- reported OK. Any other bind
    failure, or the same EADDRINUSE while the daemon is not alive, is
    still a failure."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((metrics.listen, metrics.port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE and daemon_alive:
            return Check(
                "metrics_port",
                True,
                f"{metrics.listen}:{metrics.port}: in use by the running daemon"
                " (status file fresh)",
            )
        return Check(
            "metrics_port",
            False,
            f"{metrics.listen}:{metrics.port} not bindable ({exc.strerror or exc})",
        )
    finally:
        sock.close()
    return Check("metrics_port", True, f"{metrics.listen}:{metrics.port}: bindable")


def doctor(cfg: Config, *, now: int) -> list[Check]:
    """Runs every operator health check and returns them in a fixed order.
    Read-only throughout: never opens the radio device, never writes to
    the database, never binds a socket it doesn't immediately close."""
    db_dir = _database_dir(cfg)
    status_file_check = _check_status_file(db_dir, now)
    # Shared-BBS checks exist only with a bbs file, so single-file installs
    # keep today's exact check list.
    bbs_checks = (
        [
            _check_db_integrity(bbs, "bbs_integrity"),
            _check_schema_version(bbs, "bbs_schema_version", BBS_SCHEMA_VERSION),
            _check_peer_observatory(cfg),
        ]
        if (bbs := cfg.database.resolved_bbs_path)
        else []
    )
    return [
        _check_identity(cfg),
        _check_device(cfg.radio.device),
        _check_meshcore(cfg),
        _check_db_dir(db_dir),
        _check_db_integrity(cfg.database.path),
        _check_schema_version(cfg.database.path),
        *bbs_checks,
        _check_disk_headroom(db_dir, cfg.retention),
        _check_backup_dir(cfg.backup),
        _check_backup_age(cfg.backup, now),
        _check_log_dir(db_dir),
        _check_timezone(cfg.service.timezone),
        status_file_check,
        _check_worker_heartbeats(db_dir),
        _check_metrics_port(cfg.metrics, daemon_alive=status_file_check.ok),
    ]


# =============================================================================
# follow_file / exchange_trace_path
# =============================================================================


def exchange_trace_path(cfg: Config) -> str:
    """`<database dir>/logs/exchange.log` -- the path a future `mgarden tail`
    will `follow_file` for `run --debug`'s DM/reply exchange trace.

    NOTE: as of this writing, `exchange_log.configure(True)` (see
    `Daemon.__init__`) only ever attaches a stderr `StreamHandler`; nothing
    yet writes this file to disk. Making the daemon side of `--debug` also
    (or instead) write here is a separate, later step -- this helper only
    fixes the path convention so `follow_file`/`mgarden tail` have a
    stable target to point at once that lands.
    """
    return os.path.join(_database_dir(cfg), "logs", "exchange.log")


def follow_file(
    path: str,
    *,
    from_end: bool = True,
    poll_seconds: float = 0.5,
    stop: Event,
) -> Iterator[str]:
    """A `tail -f` generator, tolerant of rotation.

    Yields each newline-terminated line (newline stripped) as it is
    appended to `path`. Starts at end-of-file when `from_end` is True
    (the common "watch new lines from now on" case), or at the beginning
    otherwise. Reopens `path` when its inode changes (rename-based
    rotation, e.g. logrotate's `create`) or when the same inode shrinks
    (truncate-based rotation) -- either way, no line written after the
    rotation is skipped, and a reopened file is always read from its
    start (only the very first open honors `from_end`). Stops (the
    generator returns) within one `poll_seconds` of `stop.set()`.

    Intended for the future `mgarden tail` of `exchange_trace_path(cfg)`.
    """
    f = None
    inode: int | None = None
    seek_end_next_open = from_end

    while True:
        if f is None:
            try:
                # Deliberately not a `with` block: the handle must outlive
                # this iteration, carried across yields until rotation or
                # `stop` closes it explicitly below.
                f = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
            except OSError:
                if stop.wait(poll_seconds):
                    return
                continue
            inode = os.fstat(f.fileno()).st_ino
            if seek_end_next_open:
                f.seek(0, os.SEEK_END)
            seek_end_next_open = False  # a reopen after rotation always starts at 0

        pos_before = f.tell()
        line = f.readline()

        if line.endswith("\n"):
            yield line[:-1]
            continue

        if line:
            # Partial line: the writer hasn't flushed its trailing newline
            # yet. Rewind so the next poll re-reads it whole.
            f.seek(pos_before)

        try:
            st = os.stat(path)
        except OSError:
            st = None

        if st is None or st.st_ino != inode:
            f.close()
            f = None
            if stop.wait(poll_seconds):
                return
            continue

        if st.st_size < f.tell():
            f.seek(0)
            continue

        if stop.wait(poll_seconds):
            f.close()
            return
