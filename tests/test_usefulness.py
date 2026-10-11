import sqlite3
from types import SimpleNamespace

import pytest

from meshgarden import usefulness
from meshgarden.database import connect, migrate


def metrics_db(tmp_path):
    conn = connect(str(tmp_path / "usefulness.db"))
    migrate(conn)
    return conn


def test_record_is_text_free_pseudonymous_and_idempotent(tmp_path):
    conn = metrics_db(tmp_path)
    now = 2_000_000_000
    conn.execute(
        "INSERT INTO processed_requests"
        "(sender_node_id,packet_id,first_seen_at,expires_at,command,state,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("!aabbccdd", 7, now, now + 3600, "READ", "queued", now),
    )
    conn.commit()

    usefulness.record(conn, "!aabbccdd", "READ", now)
    usefulness.record(conn, "!aabbccdd", "READ", now)

    row = conn.execute(
        "SELECT event_key,user_key,command,outcome FROM usefulness_events"
    ).fetchone()
    assert conn.execute("SELECT COUNT(*) FROM usefulness_events").fetchone()[0] == 1
    assert row["command"] == "READ"
    assert row["outcome"] == "accepted"
    assert len(row["event_key"]) == len(row["user_key"]) == 32
    assert "aabbccdd" not in row["event_key"] + row["user_key"]
    columns = {r[1] for r in conn.execute("PRAGMA table_info(usefulness_events)")}
    assert not ({"node_id", "text", "argument", "topic", "recipient"} & columns)


def test_reused_packet_id_after_dedup_expiry_is_a_new_event(tmp_path):
    conn = metrics_db(tmp_path)
    now = 2_000_000_000
    values = ("!aabbccdd", 7, now, now + 3600, "READ", "queued", now)
    conn.execute(
        "INSERT INTO processed_requests"
        "(sender_node_id,packet_id,first_seen_at,expires_at,command,state,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        values,
    )
    conn.commit()
    usefulness.record(conn, "!aabbccdd", "READ", now)

    conn.execute("DELETE FROM processed_requests")
    later = now + 2 * usefulness.DAY_SECONDS
    conn.execute(
        "INSERT INTO processed_requests"
        "(sender_node_id,packet_id,first_seen_at,expires_at,command,state,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("!aabbccdd", 7, later, later + 3600, "READ", "queued", later),
    )
    conn.commit()
    usefulness.record(conn, "!aabbccdd", "READ", later)

    assert conn.execute("SELECT COUNT(*) FROM usefulness_events").fetchone()[0] == 2


def test_report_funnels_returning_users_and_bounded_command_names(tmp_path):
    conn = metrics_db(tmp_path)
    now = 2_000_000_000
    day = usefulness.DAY_SECONDS

    # A returns on another UTC day and completes both funnels.
    usefulness.record(conn, "!aaaaaaaa", "NEW", now - 3 * day)
    usefulness.record(conn, "!aaaaaaaa", "MENU", now - 2 * day)
    usefulness.record(conn, "!aaaaaaaa", "READ", now - day)
    # B opens the menu but performs no useful action.  Its visitor-supplied
    # unknown word is collapsed to UNKNOWN instead of being retained.
    usefulness.record(conn, "!bbbbbbbb", "MENU", now - 100)
    usefulness.record(conn, "!bbbbbbbb", "THIS IS VISITOR TEXT", now - 90)
    usefulness.record(conn, "!cccccccc", "POST", now - 80)
    usefulness.record(conn, "!cccccccc", "BOARDS", now - 70)
    usefulness.record(conn, "!cccccccc", "MAIL", now - 60)
    usefulness.record(conn, "!dddddddd", "ERROR", now - 50)

    result = usefulness.report(conn, now)

    assert result["returning_users_7d"] == 1
    assert result["users_7d"] == 4
    assert result["menu_to_useful"] == {"started": 2, "converted": 1, "rate": 0.5}
    assert result["new_to_read"] == {"started": 1, "converted": 1, "rate": 1.0}
    assert result["boards"] == {
        "reads": 1,
        "posts": 1,
        "lists": 1,
        "mail": 1,
        "mail_stored": 0,
        "mail_collected": 0,
    }
    assert result["unknown"] == 1
    assert result["errors"] == 1
    assert "THIS IS VISITOR TEXT" not in str(result)


def test_report_overlays_current_processed_request_outcome(tmp_path):
    conn = metrics_db(tmp_path)
    now = 2_000_000_000
    conn.execute(
        "INSERT INTO processed_requests"
        "(sender_node_id,packet_id,first_seen_at,expires_at,command,state,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("!aabbccdd", 9, now, now + 3600, "MAIL", "queued", now),
    )
    conn.commit()
    usefulness.record(conn, "!aabbccdd", "MAIL", now)
    conn.execute(
        "UPDATE processed_requests SET state='failed', updated_at=? WHERE packet_id=9",
        (now + 1,),
    )
    conn.commit()

    result = usefulness.report(conn, now + 2)
    assert result["events"] == 1
    assert result["outcomes"] == {"error": 1}
    assert result["command_outcomes"] == {"MAIL": {"error": 1}}


def test_sync_preserves_terminal_outcome_after_request_log_is_pruned(tmp_path):
    conn = metrics_db(tmp_path)
    now = 2_000_000_000
    conn.execute(
        "INSERT INTO processed_requests"
        "(sender_node_id,packet_id,first_seen_at,expires_at,command,state,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("!aabbccdd", 10, now - 100, now - 10, "SUGGEST", "failed", now - 50),
    )
    conn.commit()

    assert usefulness.sync(conn, now) == 1
    conn.execute("DELETE FROM processed_requests")
    conn.commit()

    result = usefulness.report(conn, now)
    assert result["commands"] == {"SUGGEST": 1}
    assert result["outcomes"] == {"error": 1}


def test_funnels_require_successful_outcomes(tmp_path):
    conn = metrics_db(tmp_path)
    now = 2_000_000_000
    for packet_id, command, state in (
        (1, "MENU", "queued"),
        (2, "POST", "failed"),
        (3, "NEW", "queued"),
        (4, "NEWS", "failed"),
    ):
        conn.execute(
            "INSERT INTO processed_requests"
            "(sender_node_id,packet_id,first_seen_at,expires_at,command,state,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("!aabbccdd", packet_id, now + packet_id, now + 1000, command, state, now),
        )
    conn.commit()

    usefulness.sync(conn, now + 10)
    result = usefulness.report(conn, now + 10)
    assert result["menu_to_useful"]["converted"] == 0
    assert result["new_to_read"]["converted"] == 0


def test_daily_mail_counters_are_identity_free_and_aggregate(tmp_path):
    conn = metrics_db(tmp_path)
    now = 2_000_000_000
    usefulness.counter(conn, "MAIL_STORED", 1, now)
    usefulness.counter(conn, "mail_stored", 2, now)
    usefulness.counter(conn, "MAIL_COLLECTED", 4, now)
    usefulness.counter(conn, "UNKNOWN_REQUESTS", 2, now)

    result = usefulness.report(conn, now)
    assert result["counters"] == {
        "MAIL_COLLECTED": 4,
        "MAIL_STORED": 3,
        "UNKNOWN_REQUESTS": 2,
    }
    assert result["boards"]["mail_stored"] == 3
    assert result["boards"]["mail_collected"] == 4
    assert result["unknown"] == result["unknown_requests"] == 2
    columns = {r[1] for r in conn.execute("PRAGMA table_info(usefulness_daily_counters)")}
    assert "user_key" not in columns and "node_id" not in columns
    with pytest.raises(ValueError):
        usefulness.counter(conn, "MAIL_BODY", 1, now)


def test_suggest_does_not_count_as_menu_useful_conversion(tmp_path):
    conn = metrics_db(tmp_path)
    now = 2_000_000_000
    usefulness.record(conn, "!aabbccdd", "MENU", now - 2)
    usefulness.record(conn, "!aabbccdd", "SUGGEST", now - 1)

    assert usefulness.report(conn, now)["menu_to_useful"] == {
        "started": 1,
        "converted": 0,
        "rate": 0.0,
    }


def test_prune_keeps_exact_ninety_day_boundary(tmp_path):
    conn = metrics_db(tmp_path)
    now = 2_000_000_000
    usefulness.record(conn, "!oldold01", "MENU", now - usefulness.RETENTION_SECONDS - 1)
    usefulness.record(conn, "!boundary", "MENU", now - usefulness.RETENTION_SECONDS)
    usefulness.record(conn, "!recent01", "READ", now)
    usefulness.counter(conn, "MAIL_STORED", 2, now - 91 * usefulness.DAY_SECONDS)

    assert usefulness.prune(conn, now) == 2
    assert conn.execute("SELECT COUNT(*) FROM usefulness_events").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM usefulness_daily_counters").fetchone()[0] == 0


def _observation(
    conn: sqlite3.Connection,
    *,
    at: int,
    node: str = "!aabbccdd",
    snr: float | None,
    hops: int | None,
    duplicate: int = 0,
    via_mqtt: int | None = 0,
    transport: str = "meshtastic",
):
    conn.execute(
        "INSERT INTO observations"
        "(server_time,sender_node_id,dest_node_id,channel,portnum,is_duplicate,"
        "rx_snr,hops_away,via_mqtt,pki_encrypted,transport) "
        "VALUES (?,?,?,0,'TEXT_MESSAGE_APP',?,?,?,?,0,?)",
        (at, node, "!11223344", duplicate, snr, hops, via_mqtt, transport),
    )


def test_check_text_compares_unique_rf_samples_and_last_direct(tmp_path):
    conn = metrics_db(tmp_path)
    now = 2_000_000_000
    _observation(conn, at=now - 7200, snr=-2.0, hops=2)
    _observation(conn, at=now - 3600, snr=4.0, hops=0)
    _observation(conn, at=now - 1800, snr=20.0, hops=0, duplicate=1)
    _observation(conn, at=now - 1200, snr=30.0, hops=0, via_mqtt=1)
    _observation(conn, at=now - 600, node="!ffffffff", snr=40.0, hops=0)
    conn.commit()
    obs = SimpleNamespace(rx_snr=7.5, hop_start=3, hop_limit=1)

    text = usefulness.check_text(conn, "!aabbccdd", obs, now, 180)

    assert "📡 SNR +7.5 dB · 2 hops" in text
    assert "24h med +1.0 · best +4.0 (2 rx)" in text
    assert "Direct: +4.0 dB 1h ago" in text
    assert "last RF link" in text
    assert len(text.encode()) <= 180


def test_check_text_handles_no_history_and_utf8_byte_limit(tmp_path):
    conn = metrics_db(tmp_path)
    obs = SimpleNamespace(snr=None, hops=None)

    full = usefulness.check_text(conn, "!aabbccdd", obs, 2_000_000_000, 160)
    assert "📡 SNR n/a dB · hops ?" in full
    assert "24h SNR n/a · 0 rx" in full
    assert "Direct: none in 24h" in full
    assert full.endswith("SNR=last RF link")

    clipped = usefulness.check_text(conn, "!aabbccdd", obs, 2_000_000_000, 37)
    assert len(clipped.encode("utf-8")) <= 37
    clipped.encode("utf-8")  # valid code-point boundary

    meshcore_sized = usefulness.check_text(
        conn,
        "!aabbccdd",
        SimpleNamespace(snr=5.0, path_len=255),
        2_000_000_000,
        120,
    )
    assert "hops ?" in meshcore_sized
    assert "24h SNR n/a · 0 rx" in meshcore_sized
    assert "Direct: none in 24h" in meshcore_sized
    assert meshcore_sized.endswith("SNR=last RF link")
    assert len(meshcore_sized.encode()) <= 120


def test_new_funnel_counts_public_reads_but_not_ambiguous_mail(tmp_path):
    conn = metrics_db(tmp_path)
    now = 2_000_000_000
    usefulness.record(conn, "!mailonly", "NEW", now - 4)
    usefulness.record(conn, "!mailonly", "MAIL", now - 3)
    usefulness.record(conn, "!newsread", "NEW", now - 2)
    usefulness.record(conn, "!newsread", "NEWS", now - 1)

    result = usefulness.report(conn, now)
    assert result["new_to_read"] == {"started": 2, "converted": 1, "rate": 0.5}
    assert result["read_types"] == {
        "guestbook": 0,
        "news": 1,
        "board": 0,
        "mail_collected": 0,
    }


def test_schema_rejects_unbounded_commands(tmp_path):
    conn = metrics_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO usefulness_events VALUES ('e','u',1,?,'accepted')",
            ("X" * 33,),
        )
