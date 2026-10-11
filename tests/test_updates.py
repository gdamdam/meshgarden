"""ACK/cursor contract tests for the request-driven NEW summary."""

import json

import pytest

from meshgarden.database import connect, migrate
from meshgarden.updates import remember, settle, summary


@pytest.fixture
def conn(tmp_path):
    db = connect(str(tmp_path / "updates.db"))
    migrate(db)
    yield db
    db.close()


def request(conn, node, packet, state, now=100):
    conn.execute(
        "INSERT INTO processed_requests(sender_node_id,packet_id,first_seen_at,"
        "expires_at,command,state,updated_at) VALUES(?,?,?,?,?,?,?)",
        (node, packet, now, now + 86400, "NEW", state, now),
    )
    conn.commit()


def news(conn, ident, created, visible=1, expires=None):
    conn.execute(
        "INSERT INTO news(id,created_at,text,visible,expires_at) VALUES(?,?,?,?,?)",
        (ident, created, f"n{ident}", visible, expires),
    )
    conn.commit()


def test_cursor_advances_only_after_ack(conn):
    node = "!aabbccdd"
    news(conn, 1, 100)
    _, snapshot = summary(conn, node, 100)
    remember(conn, node, 10, snapshot, 100)
    request(conn, node, 10, "submitted")
    settle(conn, 101, max_age=3600)
    assert conn.execute("SELECT 1 FROM update_cursors WHERE node_id=?", (node,)).fetchone() is None
    assert (
        conn.execute("SELECT 1 FROM update_pending WHERE node_id=?", (node,)).fetchone() is not None
    )

    conn.execute("UPDATE processed_requests SET state='acked' WHERE packet_id=10")
    conn.commit()
    settle(conn, 102, max_age=3600)
    row = conn.execute("SELECT snapshot FROM update_cursors WHERE node_id=?", (node,)).fetchone()
    assert json.loads(row[0]) == snapshot


@pytest.mark.parametrize("state", ["failed", "unknown", "expired"])
def test_failure_unknown_and_expiry_leave_cursor(conn, state):
    node = "!aabbccdd"
    news(conn, 1, 100)
    _, snapshot = summary(conn, node, 100)
    remember(conn, node, 11, snapshot, 100)
    request(conn, node, 11, state)
    settle(conn, 200, max_age=10)
    assert conn.execute("SELECT 1 FROM update_cursors WHERE node_id=?", (node,)).fetchone() is None


def test_pending_survives_restart_and_ack_is_durable(conn, tmp_path):
    node = "!aabbccdd"
    news(conn, 1, 100)
    _, snapshot = summary(conn, node, 100)
    remember(conn, node, 12, snapshot, 100)
    request(conn, node, 12, "queued")
    path = str(tmp_path / "updates.db")
    conn.close()
    reopened = connect(path)
    migrate(reopened)
    assert reopened.execute("SELECT 1 FROM update_pending WHERE node_id=?", (node,)).fetchone()
    reopened.execute("UPDATE processed_requests SET state='acked' WHERE packet_id=12")
    reopened.commit()
    settle(reopened, 101, max_age=3600)
    assert reopened.execute("SELECT 1 FROM update_cursors WHERE node_id=?", (node,)).fetchone()
    reopened.close()


def test_out_of_order_ack_never_regresses_cursor(conn):
    node = "!aabbccdd"
    news(conn, 1, 100)
    _, first = summary(conn, node, 100)
    remember(conn, node, 20, first, 100)
    request(conn, node, 20, "acked")
    news(conn, 2, 100)
    _, second = summary(conn, node, 100)
    remember(conn, node, 21, second, 100)
    request(conn, node, 21, "acked")
    # Newer snapshot is acknowledged first.
    request_state = "UPDATE processed_requests SET state='submitted' WHERE packet_id=20"
    conn.execute(request_state)
    settle(conn, 101, max_age=3600)
    conn.execute("UPDATE processed_requests SET state='acked' WHERE packet_id=20")
    conn.commit()
    # A late ack for the older packet is harmless and cannot lower key 2.
    settle(conn, 102, max_age=3600)
    row = conn.execute("SELECT snapshot FROM update_cursors WHERE node_id=?", (node,)).fetchone()
    assert json.loads(row[0])["news"] == 2


def test_same_second_post_after_snapshot_is_not_lost(conn):
    node = "!aabbccdd"
    news(conn, 1, 100)
    _, snapshot = summary(conn, node, 100)
    news(conn, 2, 100)
    remember(conn, node, 30, snapshot, 100)
    request(conn, node, 30, "acked")
    settle(conn, 100, max_age=3600)
    text, _ = summary(conn, node, 100)
    assert "1 NEWS" in text


def test_hidden_and_expired_news_are_excluded(conn):
    node = "!aabbccdd"
    news(conn, 1, 100, visible=0)
    news(conn, 2, 100, expires=100)
    news(conn, 3, 100)
    text, snapshot = summary(conn, node, 100)
    assert "1 NEWS" in text
    assert snapshot["news"] == 3


def test_lab_locked_summary_does_not_expose_or_advance_boards(conn):
    conn.execute(
        "INSERT INTO guestbook(entry_id,created_at,sender_node_id,display_name,"
        "message,message_hash,visible,topic) "
        "VALUES(1,100,'!x','X','secret','hash',1,'local')"
    )
    conn.commit()
    text, snapshot = summary(conn, "!locked", 100, boards_enabled=False)
    assert "BOARDS" not in text and "boards" not in snapshot


def test_summary_respects_120_utf8_byte_budget(conn):
    node = "!aabbccdd"
    for ident in range(1, 5):
        news(conn, ident, 100)
    text, _ = summary(conn, node, 100, boards_enabled=True, mail_enabled=True, max_bytes=120)
    assert len(text.encode("utf-8")) <= 120
