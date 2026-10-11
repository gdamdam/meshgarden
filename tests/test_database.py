import os
import sqlite3

import pytest

from meshgarden import database
from meshgarden.database import SCHEMA_VERSION, connect, get_schema_version, migrate

TABLES = {
    "nodes",
    "observations",
    "guestbook",
    "processed_requests",
    "daily_stats",
    "blocked_senders",
    "node_positions",
    "node_daily",
    "links",
    "local_radio_samples",
    "channel_knocks",
    "mail_aliases",
}


def test_migrate_creates_all_tables(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names


def test_migrate_adds_new_node_snapshot_columns(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert {
        "hw_model",
        "role",
        "last_position_lat",
        "last_position_lon",
        "last_position_at",
        "last_battery",
    } <= cols


def test_links_kind_check_constraint(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO links(a_node_id,b_node_id,first_seen,last_seen,count,kind)"
            " VALUES ('!1','!2',1,1,1,'bogus')"
        )


def test_node_daily_primary_key_blocks_duplicates(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    conn.execute(
        "INSERT INTO node_daily(node_id, day, packets) VALUES ('!aabbccdd','2026-01-01',1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO node_daily(node_id, day, packets) VALUES ('!aabbccdd','2026-01-01',2)"
        )


def test_wal_is_enabled(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_migrate_is_idempotent(tmp_path):
    p = str(tmp_path / "o.db")
    conn = connect(p)
    migrate(conn)
    assert migrate(conn) == SCHEMA_VERSION
    assert get_schema_version(conn) == SCHEMA_VERSION


def test_second_connection_can_read_while_first_writes(tmp_path):
    p = str(tmp_path / "o.db")
    a = connect(p)
    migrate(a)
    a.execute("BEGIN")
    a.execute("INSERT INTO blocked_senders(node_id, blocked_at) VALUES ('!1', 1)")
    b = connect(p)
    assert b.execute("SELECT count(*) FROM blocked_senders").fetchone()[0] == 0
    a.commit()
    assert b.execute("SELECT count(*) FROM blocked_senders").fetchone()[0] == 1


def test_processed_requests_primary_key_blocks_duplicates(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    conn.execute(
        "INSERT INTO processed_requests"
        "(sender_node_id,packet_id,first_seen_at,expires_at,state,updated_at)"
        " VALUES ('!aabbccdd',7,1,2,'received',1)"
    )
    try:
        conn.execute(
            "INSERT INTO processed_requests"
            "(sender_node_id,packet_id,first_seen_at,expires_at,state,updated_at)"
            " VALUES ('!aabbccdd',7,9,9,'received',9)"
        )
        raise AssertionError("expected IntegrityError")
    except sqlite3.IntegrityError:
        pass


def test_busy_timeout_is_set(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_connect_creates_a_new_directory_owner_only(tmp_path):
    """L91: a state directory this process creates itself must not rely on
    the ambient umask alone to stay owner-only (mode 0700), regardless of
    what the caller's umask happens to be."""
    nested = tmp_path / "not_yet_created"
    assert not nested.exists()
    conn = connect(str(nested / "o.db"))
    assert oct(os.stat(nested).st_mode & 0o777) == oct(0o700)
    conn.close()


def test_connect_does_not_touch_permissions_of_an_existing_directory(tmp_path):
    """A directory that already existed (e.g. systemd's own StateDirectory,
    which manages its own mode) must be left alone."""
    existing = tmp_path / "already_here"
    os.makedirs(existing)
    os.chmod(existing, 0o750)
    conn = connect(str(existing / "o.db"))
    assert oct(os.stat(existing).st_mode & 0o777) == oct(0o750)
    conn.close()


def test_connect_accepts_bare_relative_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    conn = connect("meshgarden.db")
    migrate(conn)
    assert os.path.exists(tmp_path / "meshgarden.db")


def test_migrate_failure_is_atomic_and_schema_version_unchanged(tmp_path, monkeypatch):
    p = str(tmp_path / "o.db")
    conn = connect(p)
    migrate(conn)

    original_migrations = list(database.MIGRATIONS)
    bad_migration = (SCHEMA_VERSION + 1, "CREATE TABLE x(a); CREATE TABLE x(a);")
    monkeypatch.setattr(database, "MIGRATIONS", original_migrations + [bad_migration])

    with pytest.raises(sqlite3.OperationalError):
        migrate(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "x" not in names

    monkeypatch.setattr(database, "MIGRATIONS", original_migrations)
    assert migrate(conn) == SCHEMA_VERSION


def test_migration_6_adds_boards_and_mail_schema(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(guestbook)")}
    assert "topic" in cols
    q_cols = {row["name"] for row in conn.execute("PRAGMA table_info(node_daily_quota)")}
    assert {"posts", "mails_sent"} <= q_cols
    for table in ("mail_prefs", "mail"):
        conn.execute(f"SELECT * FROM {table} LIMIT 0")  # raises if missing
    conn.close()


def test_migration_7_adds_book_state(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(book_state)")}
    assert cols == {"player_key", "day", "solved", "updated_at"}
    conn.close()


def test_migration_8_adds_lab_nodes(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    assert get_schema_version(conn) >= 8
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(lab_nodes)")}
    assert cols == {"node_id", "unlocked_at"}
    conn.close()


def test_migration_9_adds_channel_knocks(tmp_path):
    """Task 3B-3: the MeshCore channel-knock cooldown's persistent state
    (scope-decisions.md section 7; transport-boundary-spec.md section
    3.2)."""
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(channel_knocks)")}
    assert cols == {"node_id", "last_knock_at"}
    conn.close()


def test_migration_16_adds_weather_alerts(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    assert migrate(conn) >= 16
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(weather_alerts)")}
    assert cols == {
        "product_key", "last_alert_id", "event", "areas",
        "expires_at", "message_type", "outcome", "updated_at",
    }
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(weather_alerts)")}
    assert {"idx_weather_alerts_last_id", "idx_weather_alerts_expires"} <= idx


def test_migration_17_adds_weather_state(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    assert migrate(conn) >= 17
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(weather_state)")}
    assert cols == {"key", "value", "updated_at"}


def test_migration_18_adds_daemon_state_and_helpers_round_trip(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    assert migrate(conn) == SCHEMA_VERSION >= 18
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(daemon_state)")}
    assert cols == {"key", "value", "updated_at"}
    assert database.get_daemon_state(conn, "k") is None
    database.set_daemon_state(conn, "k", "v1", 10)
    database.set_daemon_state(conn, "k", "v2", 20)
    assert database.get_daemon_state(conn, "k") == "v2"
