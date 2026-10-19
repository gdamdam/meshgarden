import csv
import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
from dataclasses import replace

import pytest

from meshgarden import ops
from meshgarden.config import (
    BackupConfig,
    Config,
    DatabaseConfig,
    MetricsConfig,
    RadioConfig,
    RetentionConfig,
    ServiceConfig,
)
from meshgarden.database import SCHEMA_VERSION, connect, migrate

# =============================================================================
# helpers
# =============================================================================


def _cfg(tmp_path, *, device=None, retention=None, backup=None, metrics=None):
    return Config(
        radio=RadioConfig(
            device=device or str(tmp_path / "fake-device"), expected_node_id="!11223344"
        ),
        service=ServiceConfig(),
        database=DatabaseConfig(path=str(tmp_path / "o.db")),
        retention=retention or RetentionConfig(),
        backup=backup or BackupConfig(directory=str(tmp_path / "backups")),
        metrics=metrics or MetricsConfig(),
    )


def _mkdb(tmp_path, name="o.db"):
    conn = connect(str(tmp_path / name))
    migrate(conn)
    return conn


def _insert(
    conn,
    *,
    created_at,
    message,
    sender_node_id="!aabbccdd",
    display_name=None,
    visible=1,
    moderation_reason=None,
    msg_hash=None,
    topic=None,
):
    conn.execute(
        "INSERT INTO guestbook (created_at, sender_node_id, display_name, message, "
        "message_hash, visible, moderation_reason, topic) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            created_at,
            sender_node_id,
            display_name,
            message,
            msg_hash or f"hash-{created_at}",
            visible,
            moderation_reason,
            topic,
        ),
    )
    conn.commit()


def _make_backup_file(tmp_path, name="backup.db", rows=3):
    """Builds a small, fully migrated sqlite db (as `retention.backup` would
    produce) at tmp_path/name, with `rows` guestbook entries, and returns its
    path. Checkpoints WAL into the main file and closes before returning, so
    it behaves like an at-rest backup file."""
    path = str(tmp_path / name)
    conn = connect(path)
    migrate(conn)
    for i in range(rows):
        _insert(conn, created_at=1_000_000 + i, message=f"entry {i}")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return path


# =============================================================================
# export_guestbook
# =============================================================================


def test_export_json_shape_and_values(tmp_path):
    conn = _mkdb(tmp_path)
    _insert(conn, created_at=1_700_000_000, message="hello", display_name="Ali")
    out = ops.export_guestbook(conn, "json")
    data = json.loads(out)
    assert len(data) == 1
    row = data[0]
    assert set(row) == {
        "id",
        "created_at",
        "sender_node_id",
        "display_name",
        "visible",
        "moderation_reason",
        "message",
    }
    assert row["sender_node_id"] == "!aabbccdd"
    assert row["display_name"] == "Ali"
    assert row["message"] == "hello"
    assert row["visible"] is True
    assert row["moderation_reason"] is None
    # UTC ISO-8601 with an explicit offset.
    assert row["created_at"] == "2023-11-14T22:13:20+00:00"


def test_export_prefers_current_known_node_name_over_snapshot(tmp_path):
    conn = _mkdb(tmp_path)
    _insert(conn, created_at=1, message="hello", display_name="Old Name")
    with conn:
        conn.execute(
            "INSERT INTO nodes(node_id,observed_first_seen,observed_last_seen,short_name) "
            "VALUES ('!aabbccdd',1,1,'Current Name')"
        )

    row = json.loads(ops.export_guestbook(conn, "json"))[0]
    assert row["sender_node_id"] == "!aabbccdd"
    assert row["display_name"] == "Current Name"


def test_export_json_hidden_and_moderation_reason(tmp_path):
    conn = _mkdb(tmp_path)
    _insert(conn, created_at=1, message="visible one")
    _insert(conn, created_at=2, message="banned one", visible=0, moderation_reason="banned_word")

    visible_only = json.loads(ops.export_guestbook(conn, "json"))
    assert [r["message"] for r in visible_only] == ["visible one"]

    both = json.loads(ops.export_guestbook(conn, "json", include_hidden=True))
    assert [r["message"] for r in both] == ["visible one", "banned one"]
    hidden_row = both[1]
    assert hidden_row["visible"] is False
    assert hidden_row["moderation_reason"] == "banned_word"


def test_export_csv_quoting_commas_quotes_newlines_emoji(tmp_path):
    conn = _mkdb(tmp_path)
    tricky = 'Hello, "world"\nsecond line \U0001f331'  # comma, quotes, newline, emoji
    _insert(conn, created_at=42, message=tricky, display_name="A, B")

    out = ops.export_guestbook(conn, "csv")
    reader = csv.reader(io.StringIO(out))
    rows = list(reader)
    assert rows[0] == [
        "id",
        "created_at",
        "sender_node_id",
        "display_name",
        "visible",
        "moderation_reason",
        "message",
    ]
    assert len(rows) == 2
    data_row = rows[1]
    assert data_row[3] == "A, B"
    assert data_row[4] == "true"
    assert data_row[5] == ""
    assert data_row[6] == tricky


def test_export_csv_hidden_filter(tmp_path):
    conn = _mkdb(tmp_path)
    _insert(conn, created_at=1, message="shown")
    _insert(conn, created_at=2, message="hidden", visible=0, moderation_reason="x")

    visible_only = ops.export_guestbook(conn, "csv")
    assert "hidden" not in visible_only

    both = ops.export_guestbook(conn, "csv", include_hidden=True)
    assert "hidden" in both
    reader = csv.reader(io.StringIO(both))
    data_rows = list(reader)[1:]
    assert data_rows[1][4] == "false"  # visible column for the hidden entry


def test_export_default_excludes_board_posts(tmp_path):
    """Item 4 (ruling 9): default export is main-guestbook-only (topic IS
    NULL) -- a board post must not silently leak into it."""
    conn = _mkdb(tmp_path)
    _insert(conn, created_at=1, message="main guestbook entry")
    _insert(conn, created_at=2, message="bike board entry", topic="bike")

    data = json.loads(ops.export_guestbook(conn, "json"))
    assert [r["message"] for r in data] == ["main guestbook entry"]


def test_export_topic_selects_only_that_boards_entries(tmp_path):
    conn = _mkdb(tmp_path)
    _insert(conn, created_at=1, message="main guestbook entry")
    _insert(conn, created_at=2, message="bike board entry", topic="bike")
    _insert(conn, created_at=3, message="lost board entry", topic="lost")

    data = json.loads(ops.export_guestbook(conn, "json", topic="bike"))
    assert [r["message"] for r in data] == ["bike board entry"]


def test_export_invalid_format_raises(tmp_path):
    conn = _mkdb(tmp_path)
    with pytest.raises(ValueError):
        ops.export_guestbook(conn, "xml")


# =============================================================================
# restore_backup
# =============================================================================


def test_restore_happy_path(tmp_path):
    backup = _make_backup_file(tmp_path, rows=3)
    target = str(tmp_path / "restored" / "o.db")  # parent dir does not exist yet

    report = ops.restore_backup(backup, target)

    assert report.ok is True
    assert report.error is None
    assert report.backup_user_version == SCHEMA_VERSION
    assert report.guestbook_row_count == 3
    assert os.path.exists(target)

    conn = connect(target, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM guestbook").fetchone()[0] == 3
    finally:
        conn.close()

    # source untouched
    assert os.path.exists(backup)


def test_restore_never_deletes_or_modifies_source(tmp_path):
    backup = _make_backup_file(tmp_path, rows=2)
    with open(backup, "rb") as f:
        before = f.read()
    target = str(tmp_path / "o.db")

    ops.restore_backup(backup, target)

    with open(backup, "rb") as f:
        after = f.read()
    assert before == after


def test_restore_refuses_existing_target_without_overwrite(tmp_path):
    backup = _make_backup_file(tmp_path, rows=1)
    target = str(tmp_path / "existing.db")
    with open(target, "wb") as f:
        f.write(b"not a real db, but present")

    report = ops.restore_backup(backup, target)

    assert report.ok is False
    assert "already exists" in report.error
    with open(target, "rb") as f:
        assert f.read() == b"not a real db, but present"  # untouched


def test_restore_overwrite_replaces_existing_target(tmp_path):
    backup = _make_backup_file(tmp_path, rows=2)
    target = str(tmp_path / "existing.db")
    with open(target, "wb") as f:
        f.write(b"stale content")

    report = ops.restore_backup(backup, target, overwrite=True)

    assert report.ok is True
    assert report.guestbook_row_count == 2


def test_restore_refuses_target_with_wal_sidecar_unless_overwrite(tmp_path):
    """A `-wal` sidecar can exist even without the main db file present
    (e.g. right after a crash); the refusal must fire on the sidecar
    alone, not only when the main file also exists."""
    backup = _make_backup_file(tmp_path, rows=1)
    target = str(tmp_path / "live.db")
    with open(target + "-wal", "wb") as f:
        f.write(b"wal frames")

    refused = ops.restore_backup(backup, target)
    assert refused.ok is False
    assert "WAL/SHM sidecar" in refused.error

    allowed = ops.restore_backup(backup, target, overwrite=True)
    assert allowed.ok is True
    # The stale sidecar's bytes must be gone -- a restored (WAL-mode)
    # database legitimately gets its own fresh "-wal" the moment anything
    # opens it, so the file's mere presence afterwards proves nothing; its
    # content must no longer be the pre-restore garbage.
    if os.path.exists(target + "-wal"):
        with open(target + "-wal", "rb") as f:
            assert f.read() != b"wal frames"


def test_restore_refuses_unrelated_valid_sqlite_file_without_touching_target(tmp_path):
    """An unrelated-but-structurally-valid sqlite file (passes
    PRAGMA integrity_check, has no `guestbook`/other app tables) must be
    refused before ever copying -- never replace the target, and never let
    an exception escape while doing so. Regression test for the reported
    bug where such a file passed integrity_check, silently replaced the
    target (since both "guestbook" counts were 0, via the old
    except-OperationalError-means-0 fallback), and only then blew up with
    "no such table: guestbook" -- by which point the target's real data was
    already gone."""
    backup = str(tmp_path / "unrelated.db")
    conn = sqlite3.connect(backup)
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO widgets (name) VALUES ('a')")
    conn.commit()
    conn.close()

    target = str(tmp_path / "o.db")
    target_conn = connect(target)
    migrate(target_conn)
    _insert(target_conn, created_at=1, message="do not lose me")
    target_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    target_conn.close()
    with open(target, "rb") as f:
        original_bytes = f.read()

    report = ops.restore_backup(backup, target, overwrite=True)

    assert report.ok is False
    assert report.error is not None

    with open(target, "rb") as f:
        assert f.read() == original_bytes  # untouched

    conn = connect(target, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM guestbook").fetchone()[0] == 1
    finally:
        conn.close()


def test_restore_refuses_backup_with_user_version_zero(tmp_path):
    """A backup taken before migration 1 (user_version still 0) is a valid
    sqlite file but has none of the application's tables -- refused the
    same way, on the schema/version check, not silently treated as an
    empty guestbook."""
    backup = str(tmp_path / "pre_migration.db")
    conn = sqlite3.connect(backup)
    conn.execute("CREATE TABLE placeholder (x INTEGER)")
    conn.commit()
    conn.close()
    target = str(tmp_path / "o.db")

    report = ops.restore_backup(backup, target)

    assert report.ok is False
    assert "schema version" in report.error or "missing required table" in report.error
    assert not os.path.exists(target)


def test_restore_refuses_missing_backup(tmp_path):
    report = ops.restore_backup(str(tmp_path / "nope.db"), str(tmp_path / "o.db"))
    assert report.ok is False
    assert "not found" in report.error


def test_restore_refuses_corrupted_backup_file(tmp_path):
    backup = str(tmp_path / "corrupt.db")
    with open(backup, "wb") as f:
        f.write(b"this is not a sqlite database at all, just bytes" * 10)
    target = str(tmp_path / "o.db")

    report = ops.restore_backup(backup, target)

    assert report.ok is False
    assert "not a valid sqlite database" in report.error
    assert not os.path.exists(target)


def test_restore_setup_failure_returns_bounded_report_and_leaves_target_untouched(
    tmp_path, monkeypatch
):
    """The generic safety net must cover the `os.makedirs`/`tempfile.mkstemp`
    step too, not just the copy/swap steps after it -- an exception raised
    there must come back as a bounded ok=False report (naming the
    exception type) rather than propagating, per the docstring's "never
    raises" guarantee."""
    backup = _make_backup_file(tmp_path, rows=1)
    target = str(tmp_path / "o.db")

    def raise_mkstemp(*a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(tempfile, "mkstemp", raise_mkstemp)

    report = ops.restore_backup(backup, target)

    assert report.ok is False
    assert "OSError" in report.error
    assert not os.path.exists(target)


def test_restore_detects_guestbook_row_count_mismatch(tmp_path, monkeypatch):
    backup = _make_backup_file(tmp_path, rows=3)
    target = str(tmp_path / "o.db")

    def fake_copy(source, dest_path):
        # Simulates a copy that silently drops rows: a fresh, empty,
        # migrated db instead of an actual page-for-page copy.
        c = connect(dest_path)
        migrate(c)
        c.close()

    monkeypatch.setattr(ops, "_copy_backup_api", fake_copy)

    report = ops.restore_backup(backup, target)

    assert report.ok is False
    assert "row count mismatch" in report.error
    assert report.guestbook_row_count == 0


def test_restore_preserves_target_on_row_count_mismatch(tmp_path, monkeypatch):
    """When the copy silently drops rows, the original target must be
    byte-for-byte unchanged, and no .restore.tmp file should remain."""
    backup = _make_backup_file(tmp_path, rows=3)
    target = str(tmp_path / "existing.db")

    # Pre-create target with different content (5 rows)
    target_conn = connect(target)
    migrate(target_conn)
    for i in range(5):
        _insert(target_conn, created_at=2_000_000 + i, message=f"target entry {i}")
    target_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    target_conn.close()

    # Hash the original target before restore attempt
    with open(target, "rb") as f:
        original_hash = hashlib.sha256(f.read()).hexdigest()

    def fake_copy(source, dest_path):
        # Simulates a copy that silently drops rows
        c = connect(dest_path)
        migrate(c)
        c.close()

    monkeypatch.setattr(ops, "_copy_backup_api", fake_copy)

    report = ops.restore_backup(backup, target, overwrite=True)

    # Restore must fail
    assert report.ok is False
    assert "row count mismatch" in report.error

    # Original target must be byte-for-byte unchanged
    with open(target, "rb") as f:
        final_hash = hashlib.sha256(f.read()).hexdigest()
    assert final_hash == original_hash

    # No .restore.tmp file should remain
    assert not os.path.exists(target + ".restore.tmp")


def _crash_snapshot_target(tmp_path, live_name="live.db", target_name="target.db"):
    """Builds a target db that looks like it was left by a process that
    crashed without a clean shutdown: schema + baseline rows are
    checkpointed into the main file, then one more row is committed that
    lives ONLY in the WAL (never checkpointed). Returns `target` (the path
    restore_backup should act on) plus `holder`, a connection the caller
    must keep open until done asserting.

    Building this takes care to never let the *last* connection to the
    live db close before the snapshot is copied: SQLite auto-checkpoints
    on last-connection-close, which would fold the WAL row into the main
    file and defeat the whole point of the reproduction. So a `holder`
    connection is kept open throughout, and the crash state is captured by
    copying the db + `-wal` (+ `-shm`) files aside rather than by ever
    closing down to zero connections."""
    live = str(tmp_path / live_name)
    base = connect(live)
    migrate(base)
    base.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    base.close()

    conn = connect(live)
    _insert(conn, created_at=1, message="only in the wal")
    holder = connect(live)  # keeps the db "in use" so conn.close() can't checkpoint
    conn.close()
    assert os.path.exists(live + "-wal")

    target = str(tmp_path / target_name)
    shutil.copyfile(live, target)
    shutil.copyfile(live + "-wal", target + "-wal")
    if os.path.exists(live + "-shm"):
        shutil.copyfile(live + "-shm", target + "-shm")
    return target, holder


def _raw_count(path):
    """Counts guestbook rows via a bare sqlite3 connection (no WAL pragma
    churn of our own, so it can't accidentally checkpoint/repair anything
    on the way to reading)."""
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT COUNT(*) FROM guestbook").fetchone()[0]
    finally:
        c.close()


def test_restore_overwrite_failure_loses_uncheckpointed_wal_data(tmp_path, monkeypatch):
    """Regression test for the reported bug: restore_backup deleted the
    target's -wal/-shm sidecars before os.replace; if os.replace then
    failed, a guestbook row that lived only in the WAL (never
    checkpointed) was lost even though the restore reports ok=False.

    A blanket `os.replace` monkeypatch (failing every call) never
    exercises this: the FIRST `os.replace` restore_backup makes is the
    move-aside of the pre-existing target, so a blanket failure trips
    there -- no file is ever actually touched (the real os.replace never
    runs), and the row "surviving" proves nothing about the swap/move-back
    path at all. Instead, only the final tmp-copy -> target swap is made
    to fail (same pattern as
    test_restore_overwrite_failure_during_swap_preserves_target below),
    so the move-aside calls succeed for real and the move-back-on-failure
    logic is what has to save the row.
    """
    backup = _make_backup_file(tmp_path, rows=1)
    target, holder = _crash_snapshot_target(tmp_path)
    # NOTE: deliberately not peeking at target's row count here first --
    # opening+closing any connection to `target` while it is the only
    # connection to that file auto-checkpoints its WAL into the main file
    # (ordinary SQLite behavior), which would fold the row in "for free"
    # and defeat this reproduction before the bug even gets a chance to run.

    # Also hold a read transaction open on `target` itself (a second,
    # separate reader) so restore_backup's own best-effort
    # `PRAGMA wal_checkpoint(TRUNCATE)` -- run against the pre-existing
    # target just before it's moved aside -- cannot complete either.
    # Without this, that checkpoint alone folds the WAL row into the main
    # file before move-aside logic ever gets a chance to matter, so the
    # test would pass even without move-aside/restore-on-failure working
    # at all.
    reader = sqlite3.connect(target)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM guestbook").fetchone()

    real_replace = os.replace

    def flaky_replace(src, dst):
        # Fail only the final tmp-copy -> target swap itself, not any
        # move-aside (or move-back-on-failure) of the pre-existing target
        # -- a real disk/rename failure specific to that last rename
        # shouldn't be assumed to also break a plain rename back.
        if dst == target and "observatory-restore-" in os.path.basename(src):
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    report = ops.restore_backup(backup, target, overwrite=True)
    assert report.ok is False

    reader.close()
    holder.close()
    assert _raw_count(target) == 1, "committed WAL-only row must survive a failed restore"
    assert not os.path.exists(target + ".pre-restore")
    assert not os.path.exists(target + "-wal.pre-restore")
    assert not os.path.exists(target + "-shm.pre-restore")
    assert not [n for n in os.listdir(tmp_path) if n.startswith("observatory-restore-")], (
        "no restore temp file should remain"
    )


def test_restore_overwrite_failure_during_swap_preserves_target(tmp_path, monkeypatch):
    """Same failure mode, but confirms the target file itself (not just its
    WAL row) is still present and openable after os.replace raises."""
    backup = _make_backup_file(tmp_path, rows=1)
    target, holder = _crash_snapshot_target(tmp_path)

    real_replace = os.replace

    def flaky_replace(src, dst):
        # Fail only the final tmp-copy -> target swap itself, not any
        # move-aside (or move-back-on-failure) of the pre-existing target
        # -- a real disk/rename failure specific to that last rename
        # shouldn't be assumed to also break a plain rename back.
        if dst == target and "observatory-restore-" in os.path.basename(src):
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    report = ops.restore_backup(backup, target, overwrite=True)
    assert report.ok is False
    assert os.path.exists(target)

    holder.close()
    assert _raw_count(target) == 1


def test_restore_overwrite_success_still_replaces_target(tmp_path):
    """The move-aside-then-restore-on-failure sequence must not regress the
    ordinary successful-overwrite path: the backup's rows must land, and no
    `.pre-restore` leftovers should remain."""
    backup = _make_backup_file(tmp_path, rows=4)
    target, holder = _crash_snapshot_target(tmp_path)
    holder.close()

    report = ops.restore_backup(backup, target, overwrite=True)

    assert report.ok is True
    assert report.guestbook_row_count == 4
    conn = connect(target, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM guestbook").fetchone()[0] == 4
    finally:
        conn.close()
    assert not os.path.exists(target + ".pre-restore")
    assert not os.path.exists(target + "-wal.pre-restore")
    assert not os.path.exists(target + "-shm.pre-restore")


def test_restore_success_survives_a_post_swap_read_back_failure(tmp_path, monkeypatch):
    """The swap itself (backup -> target) has already fully succeeded by
    the time restore_backup opens the target again just to report a row
    count. A failure in that last, purely informational read-back (e.g. the
    connect() call itself raising) must not turn a successful restore into
    a reported failure -- it must come back as ok=True, with
    guestbook_row_count=None and a bounded note in `error` rather than
    letting the exception propagate."""
    backup = _make_backup_file(tmp_path, rows=4)
    target, holder = _crash_snapshot_target(tmp_path)
    holder.close()

    real_connect = sqlite3.connect

    def flaky_connect(database, *args, **kwargs):
        # Only the final bare `sqlite3.connect(target_db_path)` read-back
        # call -- no other positional/keyword args -- is made to fail;
        # every other connect() (the backup source, the checkpoint probe,
        # the tmp-copy row count check) is unaffected.
        if database == target and not args and not kwargs:
            raise sqlite3.OperationalError("simulated post-swap read-back failure")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", flaky_connect)

    report = ops.restore_backup(backup, target, overwrite=True)

    assert report.ok is True
    assert report.guestbook_row_count is None
    assert report.error is not None and "read-back" in report.error
    assert not os.path.exists(target + ".pre-restore")

    # The swap really did land: `real_connect` (not routed through
    # `flaky_connect`, which is still active until this test ends) reads
    # back the restored rows just fine.
    conn = real_connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM guestbook").fetchone()[0] == 4
    finally:
        conn.close()


def test_restore_overwrite_success_records_leftover_pre_restore_file(tmp_path, monkeypatch):
    """If cleaning up a ".pre-restore" aside file fails after an otherwise
    successful swap, the restore must still report ok=True (the data
    landed fine) but surface the leftover path rather than silently
    discarding the failure."""
    backup = _make_backup_file(tmp_path, rows=2)
    target = str(tmp_path / "existing.db")
    with open(target, "wb") as f:
        f.write(b"stale content")

    real_remove = os.remove

    def flaky_remove(path, *a, **kw):
        if str(path).endswith(".pre-restore"):
            raise PermissionError("cannot remove")
        real_remove(path, *a, **kw)

    monkeypatch.setattr(os, "remove", flaky_remove)

    report = ops.restore_backup(backup, target, overwrite=True)

    assert report.ok is True
    assert report.leftover_files == [target + ".pre-restore"]
    assert os.path.exists(target + ".pre-restore")


def test_restore_overwrite_failure_records_failed_move_back(tmp_path, monkeypatch):
    """If the final swap fails AND moving the aside file back also fails,
    the aside path must be named in the report's error (not just silently
    suppressed) so data isn't left parked under the wrong name without
    the operator being told."""
    backup = _make_backup_file(tmp_path, rows=1)
    target, holder = _crash_snapshot_target(tmp_path)
    holder.close()

    real_replace = os.replace

    def flaky_replace(src, dst):
        if dst == target and "observatory-restore-" in os.path.basename(src):
            raise OSError("disk full")  # the final tmp -> target swap
        if str(src).endswith(".pre-restore") and dst == target:
            raise OSError("move-back also fails")  # moving the main db back
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    report = ops.restore_backup(backup, target, overwrite=True)

    assert report.ok is False
    assert target + ".pre-restore" in report.error
    assert report.leftover_files == [target + ".pre-restore"]
    assert os.path.exists(target + ".pre-restore")


# =============================================================================
# doctor
# =============================================================================


def test_doctor_identity_check(tmp_path):
    good = _cfg(tmp_path)
    checks = {c.name: c for c in ops.doctor(good, now=1_700_000_000)}
    assert checks["identity"].ok is True

    bad = replace(good, radio=replace(good.radio, expected_node_id="not-a-node-id"))
    checks = {c.name: c for c in ops.doctor(bad, now=1_700_000_000)}
    assert checks["identity"].ok is False


def test_doctor_device_missing(tmp_path):
    cfg = _cfg(tmp_path, device=str(tmp_path / "no-such-device"))
    checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
    assert checks["radio_device"].ok is False
    assert "cannot stat" in checks["radio_device"].detail


def test_doctor_device_not_a_character_device(tmp_path):
    device = tmp_path / "regular-file-device"
    device.write_bytes(b"not a tty")
    cfg = _cfg(tmp_path, device=str(device))
    checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
    assert checks["radio_device"].ok is False
    assert checks["radio_device"].detail == f"{device}: not a character device"


def test_doctor_meshcore_not_configured_is_ok(tmp_path):
    cfg = _cfg(tmp_path)
    checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
    assert checks["meshcore"].ok is True
    assert "not configured" in checks["meshcore"].detail


def test_doctor_meshcore_configured_reports_package_and_state(tmp_path):
    cfg = _cfg(tmp_path)
    cfg = replace(
        cfg,
        radio=replace(
            cfg.radio,
            transport="meshcore",
            meshcore_channel="meshgarden",
            meshcore_autoadd_overwrite=True,
        ),
    )
    checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
    check = checks["meshcore"]
    # The dev venv this suite runs in has the optional `meshcore` package
    # installed (task 3B); this asserts the happy path, not the guard --
    # see test_meshcore_client.py for the absent-package guard itself.
    assert check.ok is True
    assert "package=installed" in check.detail
    assert "channel='meshgarden'" in check.detail
    assert "autoadd_overwrite=True" in check.detail


def test_doctor_meshcore_channel_disabled_reports_disabled():
    cfg = Config(
        radio=RadioConfig(
            device="/dev/fake",
            expected_node_id="!11223344",
            transport="meshcore",
            meshcore_channel="",
        ),
        service=ServiceConfig(),
        database=DatabaseConfig(path="/tmp/unused.db"),
        retention=RetentionConfig(),
        backup=BackupConfig(directory="/tmp/unused-backups"),
        metrics=MetricsConfig(),
    )
    check = ops._check_meshcore(cfg)
    assert "channel triggering disabled" in check.detail


def test_doctor_database_dir_missing(tmp_path):
    device = tmp_path / "dev"
    device.write_bytes(b"")
    cfg = _cfg(tmp_path, device=str(device))
    cfg = replace(cfg, database=replace(cfg.database, path=str(tmp_path / "nested" / "o.db")))

    checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
    assert checks["database_dir"].ok is False
    assert checks["database_integrity"].ok is True
    assert checks["schema_version"].ok is True


def test_doctor_database_dir_integrity_and_schema_version(tmp_path):
    device = tmp_path / "dev"
    device.write_bytes(b"")
    cfg = _cfg(tmp_path, device=str(device))

    # Create the real, migrated database.
    conn = _mkdb(tmp_path)
    conn.close()
    checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
    assert checks["database_dir"].ok is True
    assert checks["database_integrity"].ok is True
    assert checks["schema_version"].ok is True

    # Simulate a stale schema version marker.
    conn = connect(str(tmp_path / "o.db"))
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    conn.close()
    checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
    assert checks["schema_version"].ok is False
    assert f"expected={SCHEMA_VERSION}" in checks["schema_version"].detail


def test_check_schema_version_reports_failed_check_for_unreadable_db(tmp_path):
    # A corrupt/non-sqlite file must not traceback `doctor` -- it should
    # surface as a failed Check, same as _check_db_integrity does.
    db_path = str(tmp_path / "corrupt.db")
    with open(db_path, "wb") as f:
        f.write(b"this is not a sqlite database at all, just bytes" * 10)

    check = ops._check_schema_version(db_path)

    assert check.ok is False
    assert "unreadable" in check.detail


def test_doctor_disk_headroom(tmp_path):
    device = tmp_path / "dev"
    device.write_bytes(b"")
    os.makedirs(tmp_path / "backups", exist_ok=True)
    conn = _mkdb(tmp_path)
    conn.close()

    huge_retention = RetentionConfig(max_managed_storage_mib=10**9)
    huge_cap = _cfg(tmp_path, device=str(device), retention=huge_retention)
    checks = {c.name: c for c in ops.doctor(huge_cap, now=1_700_000_000)}
    assert checks["disk_headroom"].ok is False

    tiny_retention = RetentionConfig(max_managed_storage_mib=1)
    tiny_cap = _cfg(tmp_path, device=str(device), retention=tiny_retention)
    checks = {c.name: c for c in ops.doctor(tiny_cap, now=1_700_000_000)}
    assert checks["disk_headroom"].ok is True


def test_doctor_backup_dir_and_age(tmp_path):
    device = tmp_path / "dev"
    device.write_bytes(b"")
    conn = _mkdb(tmp_path)
    conn.close()
    cfg = _cfg(tmp_path, device=str(device))

    # backups dir doesn't exist yet
    checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
    assert checks["backup_dir"].ok is False
    assert checks["backup_age"].ok is False
    assert "no backups found" in checks["backup_age"].detail

    os.makedirs(tmp_path / "backups", exist_ok=True)
    backup_file = tmp_path / "backups" / "observatory-20230101T000000Z.db"
    backup_file.write_bytes(b"x")
    now = 1_700_000_000
    os.utime(backup_file, (now, now))
    checks = {c.name: c for c in ops.doctor(cfg, now=now)}
    assert checks["backup_dir"].ok is True
    assert checks["backup_age"].ok is True

    stale_time = now - 999_999
    os.utime(backup_file, (stale_time, stale_time))
    checks = {c.name: c for c in ops.doctor(cfg, now=now)}
    assert checks["backup_age"].ok is False


def test_doctor_log_dir(tmp_path):
    device = tmp_path / "dev"
    device.write_bytes(b"")
    conn = _mkdb(tmp_path)
    conn.close()
    cfg = _cfg(tmp_path, device=str(device))

    checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
    assert checks["log_dir"].ok is False

    log_dir = tmp_path / "logs"
    os.makedirs(log_dir, exist_ok=True)
    (log_dir / "meshgarden.log").write_bytes(b"1234567890")
    checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
    assert checks["log_dir"].ok is True
    assert "10 bytes" in checks["log_dir"].detail


def test_doctor_timezone(tmp_path):
    device = tmp_path / "dev"
    device.write_bytes(b"")
    cfg = _cfg(tmp_path, device=str(device))
    checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
    assert checks["timezone"].ok is True

    bad_cfg = replace(cfg, service=replace(cfg.service, timezone="Not/AZone"))
    checks = {c.name: c for c in ops.doctor(bad_cfg, now=1_700_000_000)}
    assert checks["timezone"].ok is False


def test_doctor_status_file(tmp_path):
    device = tmp_path / "dev"
    device.write_bytes(b"")
    cfg = _cfg(tmp_path, device=str(device))
    now = 1_700_000_000

    checks = {c.name: c for c in ops.doctor(cfg, now=now)}
    assert checks["status_file"].ok is False
    assert "missing" in checks["status_file"].detail

    status_path = tmp_path / "status.json"
    status_path.write_text("{}")
    os.utime(status_path, (now, now))
    checks = {c.name: c for c in ops.doctor(cfg, now=now)}
    assert checks["status_file"].ok is True

    os.utime(status_path, (now - 120, now - 120))
    checks = {c.name: c for c in ops.doctor(cfg, now=now)}
    assert checks["status_file"].ok is False


def test_doctor_worker_heartbeats_missing_status_file_is_ok(tmp_path):
    """No status file at all is `_check_status_file`'s failure to report,
    not this check's -- reporting the same root cause twice would only be
    confusing."""
    device = tmp_path / "dev"
    device.write_bytes(b"")
    cfg = _cfg(tmp_path, device=str(device))
    checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
    assert checks["worker_heartbeats"].ok is True
    assert "unavailable" in checks["worker_heartbeats"].detail


def test_doctor_worker_heartbeats_all_healthy(tmp_path):
    device = tmp_path / "dev"
    device.write_bytes(b"")
    cfg = _cfg(tmp_path, device=str(device))
    now = 1_700_000_000
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "workers": {
                    "db": {"age_seconds": 1.0, "alive": True, "stalled": False},
                    "tx": {"age_seconds": 1.0, "alive": True, "stalled": False},
                }
            }
        )
    )
    os.utime(status_path, (now, now))
    checks = {c.name: c for c in ops.doctor(cfg, now=now)}
    assert checks["worker_heartbeats"].ok is True
    assert "db" in checks["worker_heartbeats"].detail


def test_doctor_worker_heartbeats_reports_a_stalled_worker(tmp_path):
    device = tmp_path / "dev"
    device.write_bytes(b"")
    cfg = _cfg(tmp_path, device=str(device))
    now = 1_700_000_000
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "workers": {
                    "db": {"age_seconds": 1.0, "alive": True, "stalled": False},
                    "radio": {"age_seconds": 500.0, "alive": False, "stalled": True},
                }
            }
        )
    )
    os.utime(status_path, (now, now))
    checks = {c.name: c for c in ops.doctor(cfg, now=now)}
    assert checks["worker_heartbeats"].ok is False
    assert "radio" in checks["worker_heartbeats"].detail
    assert "db" not in checks["worker_heartbeats"].detail.split(":")[1]


def test_doctor_worker_heartbeats_no_workers_field_is_ok(tmp_path):
    """An older status file (predating this feature) with no `workers`
    field at all -- treated as "nothing to report", not a failure."""
    device = tmp_path / "dev"
    device.write_bytes(b"")
    cfg = _cfg(tmp_path, device=str(device))
    now = 1_700_000_000
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"version": "1.0.0"}))
    os.utime(status_path, (now, now))
    checks = {c.name: c for c in ops.doctor(cfg, now=now)}
    assert checks["worker_heartbeats"].ok is True


def test_doctor_metrics_port_bindable(tmp_path):
    device = tmp_path / "dev"
    device.write_bytes(b"")
    cfg = _cfg(tmp_path, device=str(device), metrics=MetricsConfig(listen="127.0.0.1", port=0))
    checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
    assert checks["metrics_port"].ok is True


def test_doctor_metrics_port_not_bindable_when_in_use(tmp_path):
    device = tmp_path / "dev"
    device.write_bytes(b"")

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    port = blocker.getsockname()[1]
    blocker.listen(1)
    try:
        metrics = MetricsConfig(listen="127.0.0.1", port=port)
        cfg = _cfg(tmp_path, device=str(device), metrics=metrics)
        checks = {c.name: c for c in ops.doctor(cfg, now=1_700_000_000)}
        assert checks["metrics_port"].ok is False
    finally:
        blocker.close()


def test_doctor_metrics_port_in_use_by_running_daemon_is_ok(tmp_path):
    """Port held + a fresh status.json (daemon alive) -- the port being
    taken is the daemon's own metrics server, not a conflict."""
    device = tmp_path / "dev"
    device.write_bytes(b"")
    now = 1_700_000_000
    status_path = tmp_path / "status.json"
    status_path.write_text("{}")
    os.utime(status_path, (now, now))

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    port = blocker.getsockname()[1]
    blocker.listen(1)
    try:
        metrics = MetricsConfig(listen="127.0.0.1", port=port)
        cfg = _cfg(tmp_path, device=str(device), metrics=metrics)
        checks = {c.name: c for c in ops.doctor(cfg, now=now)}
        assert checks["metrics_port"].ok is True
        assert "running daemon" in checks["metrics_port"].detail
    finally:
        blocker.close()


def test_doctor_metrics_port_in_use_with_stale_status_file_still_fails(tmp_path):
    """Port held but status.json is stale (daemon not actually alive) --
    keep reporting the conflict as a failure."""
    device = tmp_path / "dev"
    device.write_bytes(b"")
    now = 1_700_000_000
    status_path = tmp_path / "status.json"
    status_path.write_text("{}")
    os.utime(status_path, (now - 120, now - 120))

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    port = blocker.getsockname()[1]
    blocker.listen(1)
    try:
        metrics = MetricsConfig(listen="127.0.0.1", port=port)
        cfg = _cfg(tmp_path, device=str(device), metrics=metrics)
        checks = {c.name: c for c in ops.doctor(cfg, now=now)}
        assert checks["metrics_port"].ok is False
        assert "not bindable" in checks["metrics_port"].detail
    finally:
        blocker.close()


def test_doctor_checks_key_order_unchanged(tmp_path):
    """metrics_port stays last regardless of daemon_alive wiring."""
    device = tmp_path / "dev"
    device.write_bytes(b"")
    cfg = _cfg(tmp_path, device=str(device), metrics=MetricsConfig(listen="127.0.0.1", port=0))
    names = [c.name for c in ops.doctor(cfg, now=1_700_000_000)]
    assert names == [
        "identity",
        "radio_device",
        "meshcore",
        "database_dir",
        "database_integrity",
        "schema_version",
        "disk_headroom",
        "backup_dir",
        "backup_age",
        "log_dir",
        "timezone",
        "status_file",
        "worker_heartbeats",
        "metrics_port",
    ]


# =============================================================================
# exchange_trace_path
# =============================================================================


def test_exchange_trace_path(tmp_path):
    cfg = _cfg(tmp_path)
    assert ops.exchange_trace_path(cfg) == os.path.join(str(tmp_path), "logs", "exchange.log")


# =============================================================================
# follow_file
# =============================================================================


def _run_follow(path, *, from_end=True, poll_seconds=0.05, timeout=2.0):
    """Runs follow_file in a background thread, returning (lines, stop,
    thread) so the caller can append/rotate then stop() and join()."""
    stop = threading.Event()
    lines: list[str] = []

    def consume():
        gen = ops.follow_file(str(path), from_end=from_end, poll_seconds=poll_seconds, stop=stop)
        for line in gen:
            lines.append(line)

    t = threading.Thread(target=consume)
    t.start()
    return lines, stop, t


def test_follow_file_yields_appended_lines(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("preexisting\n")

    lines, stop, t = _run_follow(path)
    time.sleep(0.15)  # let it open and seek to end
    with open(path, "a") as f:
        f.write("new1\n")
        f.write("new2\n")
    time.sleep(0.3)
    stop.set()
    t.join(timeout=2)

    assert not t.is_alive()
    assert lines == ["new1", "new2"]


def test_follow_file_from_beginning_reads_existing_content(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("line1\nline2\n")

    lines, stop, t = _run_follow(path, from_end=False)
    time.sleep(0.3)
    stop.set()
    t.join(timeout=2)

    assert lines == ["line1", "line2"]


def test_follow_file_survives_in_place_truncation(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("aaaa\nbbbb\n")  # 10 bytes

    lines, stop, t = _run_follow(path)
    time.sleep(0.15)
    with open(path, "r+") as f:
        f.truncate(0)
        f.write("ok\n")  # shorter than the previous read position
    time.sleep(0.3)
    stop.set()
    t.join(timeout=2)

    assert lines == ["ok"]


def test_follow_file_survives_rename_rotation(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("old-content\n")

    lines, stop, t = _run_follow(path)
    time.sleep(0.15)
    os.rename(path, str(path) + ".1")
    with open(path, "w") as f:
        f.write("new-after-rotation\n")
    time.sleep(0.3)
    stop.set()
    t.join(timeout=2)

    assert lines == ["new-after-rotation"]


def test_follow_file_stops_within_one_poll(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("x\n")
    stop = threading.Event()
    stop.set()  # already stopped before iteration starts

    gen = ops.follow_file(str(path), poll_seconds=5.0, stop=stop)
    start = time.monotonic()
    with pytest.raises(StopIteration):
        next(gen)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # nowhere near the 5s poll interval


# --- shared bbs.db -----------------------------------------------------------


def _shared_cfg(tmp_path, peer=""):
    cfg = _cfg(tmp_path)
    return replace(
        cfg, database=replace(
            cfg.database, bbs_path=str(tmp_path / "bbs.db"), peer_path=peer))


def _shared_db(tmp_path):
    conn = connect(str(tmp_path / "o.db"), bbs_path=str(tmp_path / "bbs.db"),
                   transport="meshtastic")
    migrate(conn)
    return conn


def _at_rest_copy(src, dest):
    # Rollback-journal copy: restore opens the backup mode=ro, which a WAL
    # file without its -shm cannot do.
    shutil.copy(src, dest)
    conn = sqlite3.connect(dest)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()


def test_restore_bbs_backup(tmp_path):
    conn = _shared_db(tmp_path)
    _insert(conn, created_at=1, message="kept")
    conn.close()
    backup = str(tmp_path / "bbs-copy.db")
    _at_rest_copy(tmp_path / "bbs.db", backup)
    assert ops.backup_kind(backup) == "bbs"

    target = str(tmp_path / "restored-bbs.db")
    report = ops.restore_backup(backup, target)

    assert report.ok, report.error
    assert report.guestbook_row_count == 1


def test_restore_main_backup_of_split_install(tmp_path):
    conn = _shared_db(tmp_path)
    conn.close()
    backup = str(tmp_path / "observatory-copy.db")
    _at_rest_copy(tmp_path / "o.db", backup)
    report = ops.restore_backup(backup, str(tmp_path / "r.db"))
    assert report.ok, report.error
    assert report.guestbook_row_count is None


def test_restore_legacy_backup_refused_when_bbs_has_rows(tmp_path):
    legacy = _make_backup_file(tmp_path, "legacy.db", rows=1)
    conn = _shared_db(tmp_path)
    _insert(conn, created_at=1, message="already shared")
    conn.close()
    report = ops.restore_backup(
        legacy, str(tmp_path / "r.db"), bbs_path=str(tmp_path / "bbs.db"))
    assert report.ok is False
    assert "SplitConflict" in report.error
    assert not os.path.exists(tmp_path / "r.db")


def test_doctor_reports_bbs_checks(tmp_path):
    _shared_db(tmp_path).close()
    checks = {c.name: c for c in ops.doctor(_shared_cfg(tmp_path), now=1_700_000_000)}
    assert checks["bbs_integrity"].ok is True
    assert checks["bbs_schema_version"].ok is True
    # Same instance's own bbs file is not a peer, and no peer is configured.
    assert checks["peer_observatory"].detail == "not configured"


def test_doctor_peer_not_configured_ok(tmp_path):
    _shared_db(tmp_path).close()
    check = ops._check_peer_observatory(_shared_cfg(tmp_path))
    assert check.ok is True and check.detail == "not configured"


def test_doctor_peer_same_instance_warns(tmp_path):
    from meshgarden.bbs_store import _instance_id

    conn = _shared_db(tmp_path)
    _instance_id(conn)
    conn.close()
    check = ops._check_peer_observatory(_shared_cfg(tmp_path, peer=str(tmp_path / "o.db")))
    assert check.ok is False and "same instance_id" in check.detail


def test_doctor_single_file_has_no_bbs_checks(tmp_path):
    names = [c.name for c in ops.doctor(_cfg(tmp_path), now=1_700_000_000)]
    assert not any(n.startswith("bbs_") or n == "peer_observatory" for n in names)


def test_doctor_peer_missing_is_ok_corrupt_is_not(tmp_path):
    _shared_db(tmp_path).close()
    missing = ops._check_peer_observatory(_shared_cfg(tmp_path, peer=str(tmp_path / "peer.db")))
    assert missing.ok is True and "not created yet" in missing.detail
    (tmp_path / "peer.db").write_bytes(b"not a sqlite database" * 50)
    corrupt = ops._check_peer_observatory(_shared_cfg(tmp_path, peer=str(tmp_path / "peer.db")))
    assert corrupt.ok is False and "unreadable" in corrupt.detail
