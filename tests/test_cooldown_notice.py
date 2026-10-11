"""Command-cooldown notices remain bounded and separate from command replies."""

from meshgarden.commands import CommandCooldownNotices, Reject
from meshgarden.database import MIGRATIONS, SCHEMA_VERSION, connect, migrate
from tests.fakes import make_packet


def _send(daemon, text: str, packet_id: int) -> None:
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=text, packet_id=packet_id)
    )
    daemon.drain()


def test_cooldown_notice_explains_rejection_without_restarting_window(daemon):
    _send(daemon, "INFO", 100)
    original_retry = ("!aabbccdd", 100)
    assert original_retry in daemon._sender._pending_retries
    command_started = daemon._limiter._command_last["!aabbccdd"]
    quota_before = daemon.conn.execute(
        "SELECT commands FROM node_daily_quota WHERE node_id = '!aabbccdd'"
    ).fetchone()[0]

    _send(daemon, "PING", 101)
    bodies = [sent["text"] for sent in daemon.iface.sent]
    assert bodies[-1].startswith("⏳ Please wait ")
    assert bodies[-1].endswith("s, then resend your command.")
    assert "pong" not in bodies[-1].lower()
    assert original_retry in daemon._sender._pending_retries
    assert ("!aabbccdd", 101) not in daemon._sender._pending_retries
    assert daemon._limiter._command_last["!aabbccdd"] == command_started
    quota_after = daemon.conn.execute(
        "SELECT commands FROM node_daily_quota WHERE node_id = '!aabbccdd'"
    ).fetchone()[0]
    assert quota_after == quota_before

    # The explanation itself must not restart the original command window.
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    _send(daemon, "PING", 102)
    assert "pong" in daemon.iface.sent[-1]["text"].lower()


def test_duplicate_and_additional_hits_inside_notice_window_are_silent(daemon):
    _send(daemon, "INFO", 200)
    _send(daemon, "PING", 201)
    assert len(daemon.iface.sent) == 2

    # Same RF packet is request-deduplicated before any notice decision.
    _send(daemon, "PING", 201)
    assert len(daemon.iface.sent) == 2

    # Start a fresh normal command cooldown, then hit it again. The 30-minute
    # persisted notice throttle suppresses a second explanation.
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    _send(daemon, "INFO", 202)
    _send(daemon, "PING", 203)
    assert len(daemon.iface.sent) == 3
    row = daemon.conn.execute(
        "SELECT state, reason FROM processed_requests WHERE packet_id = 203"
    ).fetchone()
    assert tuple(row) == ("dropped", "rate_limited")


def test_unknown_and_blocked_messages_never_get_cooldown_notices(daemon):
    _send(daemon, "INFO", 300)
    _send(daemon, "not-a-command", 301)
    assert len(daemon.iface.sent) == 1

    daemon.conn.execute(
        "INSERT INTO blocked_senders(node_id, blocked_at) VALUES ('!aabbccdd', 1)"
    )
    daemon.conn.commit()
    _send(daemon, "PING", 302)
    assert len(daemon.iface.sent) == 1


def test_notice_throttle_persists_and_prunes(tmp_path):
    path = str(tmp_path / "notice.db")
    conn = connect(path)
    migrate(conn)
    notices = CommandCooldownNotices(conn)
    assert notices.try_claim("!aabbccdd", 1_000)
    assert not notices.try_claim("!aabbccdd", 1_001)
    conn.close()

    conn = connect(path)
    notices = CommandCooldownNotices(conn)
    assert not notices.try_claim("!aabbccdd", 2_799)
    assert notices.try_claim("!11223344", 2_800)
    assert notices.prune(4_601) == 2
    assert conn.execute("SELECT COUNT(*) FROM command_cooldown_notices").fetchone()[0] == 0
    conn.close()


def test_queue_refusal_does_not_consume_notice_throttle(daemon, monkeypatch):
    _send(daemon, "INFO", 400)
    monkeypatch.setattr(daemon._queue, "reserve", lambda *args, **kwargs: Reject.QUEUE_FULL)
    _send(daemon, "PING", 401)
    assert daemon.conn.execute(
        "SELECT COUNT(*) FROM command_cooldown_notices"
    ).fetchone()[0] == 0


def test_schema_14_upgrade_creates_notice_throttle(tmp_path):
    conn = connect(str(tmp_path / "legacy.db"))
    for version, sql in MIGRATIONS:
        if version <= 14:
            conn.executescript(sql)
    conn.execute(
        "INSERT INTO nodes(node_id, observed_first_seen, observed_last_seen) "
        "VALUES ('!1234abcd', 1, 1)"
    )
    conn.commit()
    conn.execute("PRAGMA user_version = 14")
    assert migrate(conn) == SCHEMA_VERSION >= 15
    assert conn.execute("SELECT node_id FROM nodes").fetchone()[0] == "!1234abcd"
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(command_cooldown_notices)")
    }
    assert columns == {"node_id", "last_notice_at"}
    conn.close()


def test_meshcore_dm_gets_notice_but_channel_command_does_not(tmp_path):
    from tests.test_daemon_meshcore import (
        VISITOR_PUBKEY,
        _cfg,
        _start_meshcore_daemon,
    )

    daemon, transport = _start_meshcore_daemon(_cfg(tmp_path))
    try:
        transport.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
        transport.fake.emit_direct_message(VISITOR_PUBKEY[:12], "INFO")
        daemon.drain()
        transport.fake.emit_direct_message(VISITOR_PUBKEY[:12], "PING")
        daemon.drain()
        assert transport.fake.sent[-1]["msg"].startswith("⏳ Please wait ")
        sent_count = len(transport.fake.sent)

        transport.fake.emit_channel_message_from(0, "visitor", "PING")
        daemon.drain()
        assert len(transport.fake.sent) == sent_count
    finally:
        daemon.stop()
