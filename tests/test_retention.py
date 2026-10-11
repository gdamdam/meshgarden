import os, shutil, sqlite3, time
from datetime import UTC, datetime
import pytest
from meshgarden.config import RetentionConfig, BackupConfig
from meshgarden.database import connect, migrate
from meshgarden.retention import Retention, StorageReport

def mk(tmp_path, **over):
    conn = connect(str(tmp_path / "o.db")); migrate(conn)
    r = RetentionConfig(**{"observations_days": 7, "aggregates_days": 90,
                           "guestbook_expiry_days": 0,
                           "max_managed_storage_mib": 1024,
                           "node_daily_days": 365, "links_days": 30, **over})
    b = BackupConfig(directory=str(tmp_path / "backups"), interval_hours=24, keep=7)
    return conn, Retention(conn, r, b, log_dir=str(tmp_path / "logs"))

def ins_obs(conn, t, sender="!aabbccdd", pid=1, dup=0):
    conn.execute("INSERT INTO observations(server_time,sender_node_id,dest_node_id,"
                 "channel,portnum,packet_id,is_duplicate) VALUES (?,?,'^all',0,"
                 "'TEXT_MESSAGE_APP',?,?)", (t, sender, pid, dup)); conn.commit()

def test_observations_older_than_seven_days_are_pruned(tmp_path):
    conn, r = mk(tmp_path)
    now = 10_000_000
    ins_obs(conn, now - 8 * 86400, pid=1)
    ins_obs(conn, now - 6 * 86400, pid=2)
    assert r.prune_observations(now) == 1
    assert conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 1

def test_pruning_runs_in_bounded_batches(tmp_path):
    conn, r = mk(tmp_path)
    now = 10_000_000
    for i in range(1200):
        ins_obs(conn, now - 9 * 86400, pid=i)
    assert r.prune_observations(now) == 1200
    assert conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 0

def test_guestbook_is_never_pruned(tmp_path):
    conn, r = mk(tmp_path)
    conn.execute("INSERT INTO guestbook(created_at,sender_node_id,message,message_hash)"
                 " VALUES (1, '!aabbccdd', 'hello', 'h')"); conn.commit()
    r.prune_observations(10_000_000); r.prune_nodes(10_000_000)
    assert conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == 1

def test_node_with_guestbook_entry_is_not_pruned(tmp_path):
    conn, r = mk(tmp_path)
    now = 10_000_000
    conn.execute("INSERT INTO nodes(node_id,observed_first_seen,observed_last_seen)"
                 " VALUES ('!aabbccdd',1,1)")
    conn.execute("INSERT INTO guestbook(created_at,sender_node_id,message,message_hash)"
                 " VALUES (1,'!aabbccdd','hi','h')")
    conn.execute("INSERT INTO nodes(node_id,observed_first_seen,observed_last_seen)"
                 " VALUES ('!00000002',1,1)"); conn.commit()
    assert r.prune_nodes(now) == 1
    assert conn.execute("SELECT count(*) FROM nodes").fetchone()[0] == 1

def ins_req(conn, sender, pid, state, updated_at, expires_at=10_000_000_000):
    conn.execute(
        "INSERT INTO processed_requests"
        "(sender_node_id,packet_id,first_seen_at,expires_at,state,updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (sender, pid, updated_at, expires_at, state, updated_at),
    )
    conn.commit()

def test_stale_submitted_rows_age_to_unknown(tmp_path):
    conn, r = mk(tmp_path)
    now = 10_000_000
    ins_req(conn, "!aabbccdd", 1, "submitted", now - 400)
    assert r.age_stale_submitted(now, ack_timeout_seconds=300) == 1
    row = conn.execute("SELECT state FROM processed_requests WHERE packet_id=1").fetchone()
    assert row["state"] == "unknown"
    conn.close()

def test_fresh_submitted_row_is_not_aged(tmp_path):
    conn, r = mk(tmp_path)
    now = 10_000_000
    ins_req(conn, "!aabbccdd", 2, "submitted", now - 100)
    assert r.age_stale_submitted(now, ack_timeout_seconds=300) == 0
    row = conn.execute("SELECT state FROM processed_requests WHERE packet_id=2").fetchone()
    assert row["state"] == "submitted"
    conn.close()

def test_aged_submitted_rows_are_pruned_after_their_own_expiry_window(tmp_path):
    conn, r = mk(tmp_path)
    now = 10_000_000
    ins_req(conn, "!aabbccdd", 3, "submitted", now - 400, expires_at=now - 1)
    assert r.age_stale_submitted(now, ack_timeout_seconds=300) == 1
    assert r.prune_processed_requests(now) == 1
    remaining = conn.execute(
        "SELECT count(*) FROM processed_requests WHERE packet_id=3"
    ).fetchone()[0]
    assert remaining == 0
    conn.close()

def ins_pos(conn, t, node_id="!aabbccdd"):
    conn.execute(
        "INSERT INTO node_positions(node_id, server_time, lat, lon) VALUES (?, ?, 1.0, 1.0)",
        (node_id, t),
    )
    conn.commit()


def ins_sample(conn, t):
    conn.execute(
        "INSERT INTO local_radio_samples(server_time, battery) VALUES (?, 90)", (t,)
    )
    conn.commit()


def ins_node_daily(conn, node_id, day, packets=1):
    conn.execute(
        "INSERT INTO node_daily(node_id, day, packets) VALUES (?, ?, ?)",
        (node_id, day, packets),
    )
    conn.commit()


def ins_link(conn, a, b, last_seen):
    conn.execute(
        "INSERT INTO links(a_node_id, b_node_id, first_seen, last_seen, count, kind) "
        "VALUES (?, ?, ?, ?, 1, 'inferred_direct')",
        (a, b, last_seen, last_seen),
    )
    conn.commit()


def test_positions_older_than_observations_days_are_pruned(tmp_path):
    conn, r = mk(tmp_path)
    now = 10_000_000
    ins_pos(conn, now - 8 * 86400)
    ins_pos(conn, now - 6 * 86400)
    assert r.prune_positions(now) == 1
    assert conn.execute("SELECT count(*) FROM node_positions").fetchone()[0] == 1


def test_local_radio_samples_older_than_observations_days_are_pruned(tmp_path):
    conn, r = mk(tmp_path)
    now = 10_000_000
    ins_sample(conn, now - 8 * 86400)
    ins_sample(conn, now - 6 * 86400)
    assert r.prune_local_radio_samples(now) == 1
    assert conn.execute("SELECT count(*) FROM local_radio_samples").fetchone()[0] == 1


def test_node_daily_older_than_configured_days_is_pruned(tmp_path):
    conn, r = mk(tmp_path, node_daily_days=30)
    ins_node_daily(conn, "!aabbccdd", "2020-01-01")
    ins_node_daily(conn, "!aabbccdd", "2026-09-01")
    now = int(datetime(2026, 9, 14, tzinfo=UTC).timestamp())
    assert r.prune_node_daily(now) == 1
    remaining = {row["day"] for row in conn.execute("SELECT day FROM node_daily")}
    assert remaining == {"2026-09-01"}


def test_links_older_than_configured_days_since_last_seen_are_pruned(tmp_path):
    conn, r = mk(tmp_path, links_days=30)
    now = 10_000_000
    ins_link(conn, "!aabbccdd", "!11112222", now - 40 * 86400)
    ins_link(conn, "!11112222", "!33334444", now - 10 * 86400)
    assert r.prune_links(now) == 1
    remaining = conn.execute("SELECT count(*) FROM links").fetchone()[0]
    assert remaining == 1


def test_pruning_positions_local_samples_and_links_run_in_bounded_batches(tmp_path):
    conn, r = mk(tmp_path)
    now = 10_000_000
    for i in range(600):
        ins_pos(conn, now - 9 * 86400, node_id=f"!{i:08x}")
    assert r.prune_positions(now) == 600
    assert conn.execute("SELECT count(*) FROM node_positions").fetchone()[0] == 0


def test_rollup_contains_no_node_ids(tmp_path):
    conn, r = mk(tmp_path)
    t = 1700000000  # 2023-11-14 UTC
    ins_obs(conn, t, sender="!aabbccdd", pid=1)
    ins_obs(conn, t, sender="!11112222", pid=2)
    ins_obs(conn, t, sender="!11112222", pid=2, dup=1)
    r.rollup_day("2023-11-14")
    row = conn.execute("SELECT * FROM daily_stats WHERE day='2023-11-14'").fetchone()
    assert row["raw_events"] == 3 and row["unique_packets"] == 2
    assert row["distinct_senders"] == 2
    assert "aabbccdd" not in str(tuple(row))

def test_rollup_populates_the_request_derived_columns(tmp_path):
    conn, r = mk(tmp_path)
    t = 1700000000  # 2023-11-14 UTC
    # accepted: progressed past 'received'/'dropped'
    ins_req(conn, "!aabbccdd", 1, "acked", t)
    ins_req(conn, "!aabbccdd", 2, "submitted", t)
    ins_req(conn, "!aabbccdd", 3, "failed", t)
    ins_req(conn, "!aabbccdd", 4, "queued", t)
    # rejected
    ins_req(conn, "!11112222", 5, "dropped", t)
    ins_req(conn, "!11112222", 6, "dropped", t)
    conn.execute(
        "UPDATE processed_requests SET entry_id = 42 WHERE packet_id = 1"
    )
    conn.commit()

    r.rollup_day("2023-11-14")
    row = conn.execute("SELECT * FROM daily_stats WHERE day='2023-11-14'").fetchone()
    assert row["commands_accepted"] == 4  # acked, submitted, failed, queued
    assert row["commands_rejected"] == 2
    assert row["replies_submitted"] == 1
    assert row["replies_acked"] == 1
    assert row["replies_failed"] == 1
    assert row["entries_created"] == 1
    assert "aabbccdd" not in str(tuple(row))
    conn.close()


def test_rollup_marks_a_past_day_finalized_but_not_today(tmp_path):
    conn, r = mk(tmp_path)
    r.rollup_day("2023-11-14")  # far in the past relative to real "now"
    past = conn.execute("SELECT finalized FROM daily_stats WHERE day='2023-11-14'").fetchone()
    assert past["finalized"] == 1

    today = time.strftime("%Y-%m-%d", time.gmtime())
    r.rollup_day(today)
    current = conn.execute(
        "SELECT finalized FROM daily_stats WHERE day=?", (today,)
    ).fetchone()
    assert current["finalized"] == 0
    conn.close()


def test_backup_uses_online_api_and_is_readable(tmp_path):
    conn, r = mk(tmp_path)
    conn.execute("INSERT INTO blocked_senders(node_id,blocked_at) VALUES ('!1',1)")
    conn.commit()
    path = r.backup(1700000000)
    assert os.path.exists(path)
    b = sqlite3.connect(path)
    assert b.execute("SELECT count(*) FROM blocked_senders").fetchone()[0] == 1

def test_backup_rotation_keeps_only_n(tmp_path):
    conn, r = mk(tmp_path)
    for i in range(10):
        r.backup(1700000000 + i * 3600)
    files = os.listdir(str(tmp_path / "backups"))
    assert len(files) == 7

def test_storage_report_counts_db_backups_and_logs(tmp_path):
    # Allow the migrated schema plus WAL, backup and test log to coexist.
    conn, r = mk(tmp_path, max_managed_storage_mib=2)
    os.makedirs(str(tmp_path / "logs"), exist_ok=True)
    with open(tmp_path / "logs" / "app.log", "wb") as f:
        f.write(b"x" * 200_000)
    r.backup(1700000000)
    rep = r.storage_report()
    assert rep.log_bytes >= 200_000
    assert rep.backup_bytes > 0
    assert rep.db_bytes > 0
    assert rep.total_bytes == rep.db_bytes + rep.backup_bytes + rep.log_bytes
    assert rep.cap_bytes == 2 * 1024 * 1024

def test_pressure_thresholds_and_publication_pause(tmp_path):
    conn, r = mk(tmp_path, max_managed_storage_mib=1)
    os.makedirs(str(tmp_path / "logs"), exist_ok=True)
    with open(tmp_path / "logs" / "app.log", "wb") as f:
        f.write(b"x" * 1_040_000)     # > 95% of 1 MiB
    rep = r.storage_report()
    assert rep.pressure == "critical"
    assert rep.publication_paused is True

def test_due_for_backup_respects_interval(tmp_path):
    conn, r = mk(tmp_path)
    assert r.due_for_backup(1700000000) is True
    r.backup(1700000000)
    assert r.due_for_backup(1700000000 + 3600) is False
    assert r.due_for_backup(1700000000 + 24 * 3600 + 1) is True


# --- fix round 1: sqlite3.Error handling, atomic writes, storage pre-flight ---

# sqlite3.Connection is an immutable C type: its "backup" method can't be
# monkeypatched on the class or on a plain instance. A subclass can override
# it, so we reopen the (already migrated) db file through one to force the
# primary real-world failure mode (sqlite3.OperationalError, e.g. disk I/O
# error) out of Connection.backup().
class _BoomConnection(sqlite3.Connection):
    def backup(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")


def mk_boom(tmp_path):
    conn = connect(str(tmp_path / "o.db")); migrate(conn); conn.close()
    boom_conn = sqlite3.connect(str(tmp_path / "o.db"), factory=_BoomConnection)
    boom_conn.row_factory = sqlite3.Row
    rcfg = RetentionConfig()
    bcfg = BackupConfig(directory=str(tmp_path / "backups"), interval_hours=24, keep=7)
    return Retention(boom_conn, rcfg, bcfg, log_dir=str(tmp_path / "logs"))

def test_backup_operational_error_is_caught_not_raised(tmp_path):
    r = mk_boom(tmp_path)
    result = r.backup(1700000000)
    assert result is None
    assert r.last_backup_error == "OperationalError"
    assert os.listdir(str(tmp_path / "backups")) == []

def test_backup_write_is_atomic_on_failure(tmp_path):
    r = mk_boom(tmp_path)
    r.backup(1700000000)
    files = os.listdir(str(tmp_path / "backups"))
    assert not any(f.endswith(".tmp") for f in files)
    assert not any(f.endswith(".db") for f in files)

def test_backup_write_is_atomic_on_success(tmp_path):
    conn, r = mk(tmp_path)
    path = r.backup(1700000000)
    files = os.listdir(str(tmp_path / "backups"))
    assert files == [os.path.basename(path)]
    assert not any(f.endswith(".tmp") for f in files)

def test_rotation_ignores_leftover_tmp_files(tmp_path):
    conn, r = mk(tmp_path)
    os.makedirs(str(tmp_path / "backups"), exist_ok=True)
    with open(tmp_path / "backups" / "observatory-stale.db.tmp", "wb") as f:
        f.write(b"partial")
    for i in range(10):
        r.backup(1700000000 + i * 3600)
    files = os.listdir(str(tmp_path / "backups"))
    assert "observatory-stale.db.tmp" in files  # rotation never counts or touches .tmp files
    assert sum(1 for f in files if f.endswith(".db")) == 7

def test_backup_refuses_when_projected_size_exceeds_cap(tmp_path):
    conn = connect(str(tmp_path / "o.db")); migrate(conn)
    rcfg = RetentionConfig()
    bcfg = BackupConfig(directory=str(tmp_path / "backups"), interval_hours=24, keep=7)
    r = Retention(conn, rcfg, bcfg, log_dir=str(tmp_path / "logs"), cap_bytes=1)
    result = r.backup(1700000000)
    assert result is None
    assert r.last_backup_error == "storage_cap"
    assert os.listdir(str(tmp_path / "backups")) == []

def test_backup_refuses_when_disk_free_space_is_low(tmp_path, monkeypatch):
    conn, r = mk(tmp_path)
    tiny = shutil.disk_usage(str(tmp_path))._replace(free=1)
    monkeypatch.setattr(
        "meshgarden.retention.shutil.disk_usage", lambda path: tiny
    )
    result = r.backup(1700000000)
    assert result is None
    assert r.last_backup_error == "disk_space"
    assert os.listdir(str(tmp_path / "backups")) == []

def test_last_backup_accessors_are_read_only_and_track_state(tmp_path):
    conn, r = mk(tmp_path)
    assert r.last_backup_at is None
    assert r.last_backup_error is None
    r.backup(1700000000)
    assert r.last_backup_at == 1700000000
    assert r.last_backup_error is None
    with pytest.raises(AttributeError):
        r.last_backup_at = 5
