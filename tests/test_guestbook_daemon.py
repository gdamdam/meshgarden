from meshgarden.commands import RequestLog
from meshgarden.guestbook import format_signed_ack
from tests.fakes import make_packet

_ENTRY_ID_OVERFLOWS_SQLITE_I64 = 9223372036854775808  # 2**63, one past SQLite's INTEGER max


def sign(daemon, text, pid):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text=text, packet_id=pid))
    daemon.drain()


def test_sign_publishes_and_acknowledges_after_commit(daemon):
    sign(daemon, "SIGN Hello Mesh", 1)
    row = daemon.conn.execute("SELECT * FROM guestbook").fetchone()
    assert row["message"] == "Hello Mesh"
    assert row["sender_node_id"] == "!aabbccdd"
    assert row["visible"] == 1
    assert daemon.iface.sent[-1]["text"] == format_signed_ack(row["entry_id"])


def test_ordinary_dm_is_never_published(daemon):
    sign(daemon, "just saying hi", 1)
    assert daemon.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == 0
    assert daemon.iface.sent == []


def test_duplicate_packet_does_not_insert_twice(daemon):
    p = make_packet(to_num=daemon.my_node_num, text="SIGN Hello", packet_id=7)
    daemon.iface.emit_packet(p)
    daemon.drain()
    daemon.iface.emit_packet(p)
    daemon.drain()
    assert daemon.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == 1
    assert len(daemon.iface.sent) == 1


def test_second_sign_within_an_hour_is_not_stored_and_gets_a_cooldown_reply(daemon):
    sign(daemon, "SIGN First message", 1)
    daemon.advance_seconds(31)  # clear the 30s command cooldown only
    sign(daemon, "SIGN Second message", 2)
    assert daemon.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == 1
    # The rejected SIGN is no longer silent: the visitor is told why and
    # how long to wait (see main._handle_sign's cooldown branch).
    assert len(daemon.iface.sent) == 2
    assert daemon.iface.sent[1]["text"].startswith("🌱 Too soon to sign again\n")


def test_identical_sign_text_within_24h_does_not_insert_again(daemon):
    sign(daemon, "SIGN Hello Mesh", 1)
    daemon.advance_seconds(3601)  # clears both the 30s and the 3600s windows
    sign(daemon, "SIGN Hello Mesh", 2)
    assert daemon.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == 1


def test_broadcast_sign_is_never_published(daemon):
    for ch in range(8):
        daemon.iface.emit_packet(
            make_packet(to_num=0xFFFFFFFF, text="SIGN hello", channel=ch, packet_id=100 + ch)
        )
    daemon.drain()
    assert daemon.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == 0
    assert daemon.iface.sent == []


def test_guestbook_command_returns_at_most_three_entries(daemon):
    for i in range(5):
        daemon.conn.execute(
            "INSERT INTO guestbook(created_at,sender_node_id,display_name,"
            "message,message_hash) VALUES (?,?,?,?,?)",
            (i, "!11112222", "Nd", f"msg{i}", f"h{i}"),
        )
    daemon.conn.commit()
    sign(daemon, "GUESTBOOK", 1)
    out = daemon.iface.sent[-1]["text"]
    assert len(out.encode("utf-8")) <= 180
    assert "msg4" in out and "msg0" not in out


def test_hidden_entries_are_not_returned_over_radio(daemon):
    daemon.conn.execute(
        "INSERT INTO guestbook(created_at,sender_node_id,display_name,message,"
        "message_hash,visible) VALUES (1,'!11112222','Nd','hiddenmsg','h',0)"
    )
    daemon.conn.commit()
    sign(daemon, "GUESTBOOK", 1)
    assert "hiddenmsg" not in daemon.iface.sent[-1]["text"]


def test_stats_counts_only_visible_entries(daemon):
    daemon.conn.execute(
        "INSERT INTO guestbook(created_at,sender_node_id,message,message_hash,"
        "visible) VALUES (1,'!11112222','a','h1',1)"
    )
    daemon.conn.execute(
        "INSERT INTO guestbook(created_at,sender_node_id,message,message_hash,"
        "visible) VALUES (1,'!11112222','b','h2',0)"
    )
    daemon.conn.commit()
    sign(daemon, "STATS", 1)
    assert "Guestbook 1" in daemon.iface.sent[-1]["text"]


def test_guestbook_entry_id_overflowing_sqlite_i64_gets_a_bounded_reply(daemon):
    """Regression test: `GUESTBOOK #<id>` matches `_GUESTBOOK_ENTRY_ARG_RE`
    (`^#\\d+$`) for ANY number of digits, so an id past SQLite's 64-bit
    signed integer range (2**63-1) parses fine as a Python int but then
    raises OverflowError the moment it is bound into a query
    (`guestbook.get_visible`). That exception used to escape
    `_handle_guestbook` into `_db_worker_loop`'s generic `except Exception`,
    which only logs it -- so the request's processed_requests row, already
    claimed as 'received' before dispatch, was never settled to a terminal
    state and sat as 'received' forever (never pruned, never resolved by
    startup recovery either, since that only ever touches
    queued/sending/submitted)."""
    sign(daemon, f"GUESTBOOK #{_ENTRY_ID_OVERFLOWS_SQLITE_I64}", 1)

    # A bounded reply, same as any other not-found entry id -- never a
    # crash and never silence.
    assert len(daemon.iface.sent) == 1

    row = daemon.conn.execute(
        "SELECT state, reason FROM processed_requests WHERE packet_id=1"
    ).fetchone()
    assert row["state"] not in ("received",), "request must be settled, not stuck forever"


def test_startup_recovery_settles_stale_received_rows_past_the_reply_ttl(tmp_path):
    """A 'received' row (claimed, but never dispatched to a terminal state
    -- e.g. the daemon crashed between claim() and settling it, or hit the
    overflow above before its fix) must not sit forever: startup recovery
    only ever touched queued/sending/submitted, so 'received' rows were
    invisible to it and to pruning (which only deletes terminal states)
    alike."""
    from datetime import UTC, datetime

    from meshgarden.config import (
        BackupConfig,
        Config,
        DatabaseConfig,
        MetricsConfig,
        RadioConfig,
        RetentionConfig,
        ServiceConfig,
    )
    from meshgarden.database import connect, migrate
    from meshgarden.main import Daemon
    from tests.fakes import FakeInterface

    cfg = Config(
        radio=RadioConfig(device="/dev/fake", expected_node_id="!11223344"),
        service=ServiceConfig(reply_ttl_seconds=60),
        database=DatabaseConfig(path=str(tmp_path / "o.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "backups")),
        metrics=MetricsConfig(),
    )
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    now = int(datetime.now(UTC).timestamp())
    stale_first_seen = now - 3600  # well past reply_ttl_seconds=60
    conn.execute(
        "INSERT INTO processed_requests (sender_node_id, packet_id, first_seen_at, "
        "expires_at, command, state, reason, entry_id, tx_packet_id, updated_at) "
        "VALUES ('!aaaaaaaa', 42, ?, ?, 'GUESTBOOK', 'received', NULL, NULL, NULL, ?)",
        (stale_first_seen, stale_first_seen + 86400, stale_first_seen),
    )
    conn.commit()
    conn.close()

    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    try:
        row = d.conn.execute(
            "SELECT state, reason FROM processed_requests WHERE packet_id=42"
        ).fetchone()
        assert row["state"] != "received", "stale 'received' rows must be settled at startup"
    finally:
        d.stop()


def test_storage_cap_refuses_new_entries_without_deleting_any(daemon_full_storage):
    d = daemon_full_storage
    before = d.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0]
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="SIGN please store me", packet_id=1))
    d.drain()
    assert d.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == before
    assert len(d.iface.sent) == 1
    assert "full" in d.iface.sent[-1]["text"].lower()


def test_sign_text_never_appears_in_the_log(daemon, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    sign(daemon, "SIGN SuperSecretPhrase", 1)
    assert "SuperSecretPhrase" not in caplog.text


def test_sign_argument_never_stored_in_processed_requests(daemon):
    sign(daemon, "SIGN TopSecretArgument", 1)
    rows = daemon.conn.execute(
        "SELECT sender_node_id, packet_id, command, state, reason, entry_id FROM processed_requests"
    ).fetchall()
    dump = str([dict(r) for r in rows])
    assert "TopSecretArgument" not in dump


# --- round 1 fix: insert + queued transition must be one real transaction ---


def test_sign_insert_and_queued_transition_are_atomic_on_failure(daemon, monkeypatch):
    """If the request-log transition raises after guestbook.insert() has
    already run inside the shared transaction, the whole thing must roll
    back together: no orphaned guestbook row, no stray 'queued' state, no
    reply sent, and the sign cooldown must not have been committed either
    (so a subsequent valid SIGN from the same sender still succeeds)."""
    original_set_state = RequestLog.set_state
    calls = {"n": 0}

    def boom(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated failure after insert()")
        return original_set_state(self, *args, **kwargs)

    monkeypatch.setattr(RequestLog, "set_state", boom)

    sign(daemon, "SIGN First attempt", 1)

    assert daemon.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == 0
    row = daemon.conn.execute("SELECT state FROM processed_requests WHERE packet_id=1").fetchone()
    assert row["state"] != "queued"
    assert daemon.iface.sent == []

    monkeypatch.setattr(RequestLog, "set_state", original_set_state)
    sign(daemon, "SIGN Second attempt", 2)
    assert daemon.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == 1
    assert daemon.iface.sent[-1]["text"] == "🌱 Signed!\nThanks! Others read it with GUESTBOOK"


def test_sign_happy_path_still_yields_one_row_and_one_ack(daemon):
    sign(daemon, "SIGN Just fine", 1)
    assert daemon.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == 1
    assert len(daemon.iface.sent) == 1
    row = daemon.conn.execute("SELECT entry_id FROM guestbook").fetchone()
    assert daemon.iface.sent[0]["text"] == format_signed_ack(row["entry_id"])


# --- round 1 ruling: display names are sanitized before storage/display ----


def test_display_name_is_sanitized_before_storage_and_display(daemon):
    daemon.iface.emit_packet(
        make_packet(
            from_num=0xAABBCCDD,
            to_num=0xFFFFFFFF,
            portnum="NODEINFO_APP",
            text=None,
            packet_id=200,
            user={"shortName": "Al\x01i​", "longName": "Alice"},
        )
    )
    daemon.drain()

    daemon.iface.emit_packet(
        make_packet(
            from_num=0xAABBCCDD, to_num=daemon.my_node_num, text="SIGN Hello", packet_id=201
        )
    )
    daemon.drain()

    row = daemon.conn.execute("SELECT display_name FROM guestbook").fetchone()
    assert row["display_name"] == "Ali"
    assert "\x01" not in row["display_name"]

    daemon.iface.emit_packet(
        make_packet(from_num=0x99999999, to_num=daemon.my_node_num, text="GUESTBOOK", packet_id=1)
    )
    daemon.drain()
    out = daemon.iface.sent[-1]["text"]
    assert "Ali" in out
    assert "\x01" not in out
    assert "​" not in out
