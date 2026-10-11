"""MeshCore observation and persisted statistics regressions (no hardware)."""

import time
from datetime import UTC, datetime

import pytest
from prometheus_client import generate_latest

from meshgarden.collector import Collector
from meshgarden.commands import Command, RequestLog
from meshgarden.config import BackupConfig, RetentionConfig
from meshgarden.database import connect, migrate
from meshgarden.meshcore_transport import Event, EventType, _normalize_hops
from meshgarden.models import MeshCoreObservation
from meshgarden.retention import Retention, week_summary
from meshgarden.web import _record_window
from tests.test_daemon_meshcore import VISITOR_PUBKEY, _cfg, _start_meshcore_daemon


@pytest.fixture
def daemon(tmp_path):
    d, ft = _start_meshcore_daemon(_cfg(tmp_path))
    yield d, ft
    d.stop()


def receive(ft, text="PING", *, path_len=255, snr=6.5):
    ft.fake._dispatch(
        Event(
            EventType.CONTACT_MSG_RECV,
            {
                "pubkey_prefix": VISITOR_PUBKEY[:12],
                "text": text,
                "sender_timestamp": int(time.time()),
                "path_len": path_len,
                "SNR": snr,
            },
        )
    )


def test_live_signal_persists_even_when_command_is_unknown_and_survives_restart(tmp_path):
    cfg = _cfg(tmp_path)
    d, ft = _start_meshcore_daemon(cfg)
    try:
        receive(ft, "not a command", path_len=3)
        receive(ft, "not a command", path_len=3)
        d.drain()
        rows = d.conn.execute("SELECT * FROM observations ORDER BY id").fetchall()
        assert len(rows) == 2
        assert [r["is_duplicate"] for r in rows] == [0, 1]
        assert rows[0]["rx_snr"] == 6.5
        assert rows[0]["hops_away"] == 3
        assert rows[0]["hop_start"] is None
        assert rows[0]["routing_mode"] == "flood"
        assert rows[0]["transport"] == "meshcore"
        daily = d.conn.execute("SELECT * FROM node_daily").fetchone()
        assert daily["packets"] == 1
        assert daily["snr_count"] == 1
        assert daily["best_hops"] == 3
        metrics = generate_latest(d._metrics.registry).decode()
        assert "meshgarden_rx_snr_db_count 2.0" in metrics
        assert 'portnum="MESHCORE_DM"} 2.0' in metrics
    finally:
        d.stop()
    conn = connect(cfg.database.path)
    col = Collector(conn)
    assert col.distinct_senders_since(0) == 1
    assert col.unique_packets_since(0) == 1
    assert col.best_snr_since(0) == 6.5


def test_direct_routing_is_not_zero_hops(daemon):
    d, ft = daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    receive(ft)
    d.drain()
    assert "hops n/a" in ft.fake.sent[-1]["msg"]
    assert "PKI n/a" in ft.fake.sent[-1]["msg"]
    row = d.conn.execute("SELECT * FROM observations WHERE portnum='MESHCORE_DM'").fetchone()
    assert row["hops_away"] is None
    assert row["routing_mode"] == "direct_route"
    assert Collector(d.conn).distinct_direct_senders_since(0) == 0
    assert _normalize_hops(255) == (None, None)
    assert _normalize_hops(0) == (0, True)
    assert _normalize_hops(3) == (3, False)


def test_anonymous_channel_counts_traffic_without_fabricating_nodes(daemon):
    d, ft = daemon
    ft.fake.emit_channel_message_from(0, "unknown person", "hello")
    d.drain()
    col = Collector(d.conn)
    assert col.raw_events_since(0) == 1
    assert col.distinct_senders_since(0) == 0
    assert d.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
    assert d.conn.execute("SELECT COUNT(*) FROM node_daily").fetchone()[0] == 0
    ft.fake._dispatch(
        Event(
            EventType.CHANNEL_MSG_RECV,
            {
                "text": "not even a named sender",
                "channel_idx": 0,
                "SNR": 9,
                "path_len": 0,
                "sender_timestamp": 123,
            },
        )
    )
    d.drain()
    assert col.best_snr_with_sender_since(0) == (9, None)
    assert _record_window(d.conn, d.cfg, 0)["best_snr"] == (9, None)


def test_imported_contact_is_not_a_reception_but_live_adverts_refresh_activity(daemon):
    d, ft = daemon
    payload = {
        "public_key": VISITOR_PUBKEY,
        "adv_name": "visitor",
        "adv_lat": 37.0,
        "adv_lon": -122.0,
        "_imported": True,
    }
    ft.fake._dispatch(Event(EventType.NEW_CONTACT, payload))
    d.drain()
    col = Collector(d.conn)
    assert col.raw_events_since(0) == 0
    assert col.first_seen_date("!aabbccdd", "UTC") is None
    assert d.conn.execute("SELECT COUNT(*) FROM node_positions").fetchone()[0] == 1
    ft.fake.emit_advertisement(VISITOR_PUBKEY)
    d.drain()
    first = d.conn.execute("SELECT observed_last_seen FROM nodes").fetchone()[0]
    d.advance_seconds(20)
    # Transport timestamps use its own injectable clock.
    ft._clock = lambda: first + 20
    ft.fake.emit_advertisement(VISITOR_PUBKEY)
    d.drain()
    row = d.conn.execute("SELECT * FROM nodes").fetchone()
    assert row["observed_last_seen"] == first + 20
    assert row["observation_count"] == 2
    assert col.distinct_senders_since(first) == 1
    # A subsequent observed flood message makes the advertised location usable.
    receive(ft, "hello", path_len=0)
    d.drain()
    assert (
        col.furthest_direct_km_since(
            0,
            observatory_lat=37.01,
            observatory_lon=-122,
            max_direct_km=100,
            max_km_per_hop=100,
            max_observed_km=1000,
        )
        is not None
    )


def test_week_counts_submissions_after_ack_failure_and_restart(tmp_path):
    path = str(tmp_path / "stats.db")
    conn = connect(path)
    migrate(conn)
    now = int(time.time())
    today = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    log = RequestLog(conn)
    for packet_id, terminal in enumerate(("acked", "failed", "unknown"), 1):
        log.claim("!aabbccdd", packet_id, now, Command.PING)
        log.set_state("!aabbccdd", packet_id, "submitted", now)
        log.set_state("!aabbccdd", packet_id, terminal, now + 1)
    log.claim("!aabbccdd", 4, now, Command.PING)
    log.set_state("!aabbccdd", 4, "failed", now)  # never submitted
    conn.close()
    conn = connect(path)
    retention = Retention(conn, RetentionConfig(), BackupConfig(directory=str(tmp_path)))
    retention.rollup_day(today)
    assert week_summary(conn, today).replies == 3
    conn.execute(
        "INSERT INTO daily_stats(day,unique_packets,replies_sent) VALUES('2000-01-01',999,999)"
    )
    conn.commit()
    assert week_summary(conn, today).total_packets == 0
    assert week_summary(conn, today).replies == 3
    assert week_summary(conn, today).days_counted == 1


def test_meshcore_position_capture_toggle_and_anonymous_daily_totals(tmp_path):
    conn = connect(str(tmp_path / "stats.db"))
    migrate(conn)
    col = Collector(conn, enabled_position_capture=False)
    now = int(time.time())
    col.record_contact_identity(
        "!aabbccdd", display_name="visitor", lat=37, lon=-122, observed_at=now
    )
    assert conn.execute("SELECT COUNT(*) FROM node_positions").fetchone()[0] == 0
    col.record_meshcore(MeshCoreObservation(now, None, "^all", 123, "MESHCORE_CHANNEL"))
    today = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    Retention(conn, RetentionConfig(), BackupConfig(directory=str(tmp_path))).rollup_day(today)
    row = conn.execute("SELECT * FROM daily_stats").fetchone()
    assert row["distinct_senders"] == 0
    assert row["unique_packets"] == 1
    assert row["text_packets"] == 1


def test_upgrade_preserves_old_daily_reply_counts_and_clears_false_heard_times(tmp_path):
    from meshgarden.database import MIGRATIONS

    conn = connect(str(tmp_path / "upgrade.db"))
    for version, sql in MIGRATIONS:
        if version >= 10:
            break
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version={version}")
    conn.execute(
        "INSERT INTO nodes(node_id, observed_first_seen, observed_last_seen) "
        "VALUES('!aabbccdd',100,100)"
    )
    conn.execute(
        "INSERT INTO daily_stats(day, replies_submitted, replies_acked, finalized) "
        "VALUES('2020-01-01',2,3,1)"
    )
    conn.commit()
    migrate(conn)
    row = conn.execute("SELECT * FROM nodes").fetchone()
    assert row["observed_first_seen"] == row["observed_last_seen"] == 0
    assert week_summary(conn, "2020-01-01").replies == 5
    migrate(conn)  # idempotent, no double-counting
    assert week_summary(conn, "2020-01-01").replies == 5


def test_record_reply_uses_persisted_signal_and_hops(daemon):
    d, ft = daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    receive(ft, "RECORD", path_len=3, snr=6.5)
    d.drain()
    reply = ft.fake.sent[-1]["msg"]
    assert "Best RX SNR +6.5 dB" in reply
    assert "Most RX hops 3" in reply
    assert "2 events" in reply
