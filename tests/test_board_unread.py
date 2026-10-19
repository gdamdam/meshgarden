"""Per-board unread cursors: ack-committed, per reader, per board."""

import pytest

from meshgarden import board_unread as bu
from meshgarden.database import connect, migrate
from tests.fakes import make_packet

NOW = 1_000_000
MAX = 120


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "o.db"))
    migrate(c)
    yield c
    c.close()


def post(conn, topic, msg="hello", *, at=NOW, visible=1, sender="!aa000001"):
    cur = conn.execute(
        "INSERT INTO guestbook(created_at, sender_node_id, message, message_hash, visible, topic)"
        " VALUES (?,?,?,?,?,?)",
        (at, sender, msg, f"h{msg}{at}{topic}", visible, topic),
    )
    conn.commit()
    return cur.lastrowid


def request(conn, node, packet_id, state, now=NOW):
    conn.execute(
        "INSERT OR REPLACE INTO processed_requests(sender_node_id, packet_id, first_seen_at,"
        " expires_at, state, updated_at) VALUES (?,?,?,?,?,?)",
        (node, packet_id, now, now + 3600, state, now),
    )
    conn.commit()


def read(conn, node, board, packet_id, now=NOW, max_bytes=MAX):
    body, high = bu.read_new(conn, node, board, now, max_bytes)
    if high is not None:
        bu.remember(conn, node, packet_id, board, high, now)
    return body, high


def cursor(conn, node, board):
    row = conn.execute(
        "SELECT last_read_id FROM board_cursors WHERE node_id=? AND board=?", (node, board)
    ).fetchone()
    return row[0] if row else None


def settle(conn, now=NOW + 10):
    bu.settle(conn, now, 360)


def test_counts_are_per_board_and_first_visit_is_last_24h(conn):
    post(conn, "wx", at=NOW - 90000)  # older than a day: not counted on first visit
    post(conn, "wx")
    post(conn, "ham")
    post(conn, "ham")
    assert bu.unread_counts(conn, "!n1", NOW) == [("ham", 2), ("wx", 1)]
    assert bu.boards_new_text(bu.unread_counts(conn, "!n1", NOW), MAX) == "Boards new\nham:2\nwx:1"
    assert cursor(conn, "!n1", "ham") is None  # reading counts advances nothing


def test_advance_only_on_ack_and_only_that_board(conn):
    post(conn, "wx")
    last = post(conn, "ham")
    _, high = read(conn, "!n1", "ham", 1)
    assert high == last
    request(conn, "!n1", 1, "submitted")
    settle(conn)
    assert cursor(conn, "!n1", "ham") is None
    request(conn, "!n1", 1, "acked")
    settle(conn)
    assert cursor(conn, "!n1", "ham") == last
    assert cursor(conn, "!n1", "wx") is None
    assert bu.unread_counts(conn, "!n1", NOW) == [("wx", 1)]
    assert conn.execute("SELECT COUNT(*) FROM board_cursor_pending").fetchone()[0] == 0


@pytest.mark.parametrize("state", ["failed", "expired", "unknown", None])
def test_lost_failed_or_unknown_never_advances(conn, state):
    post(conn, "wx")
    read(conn, "!n1", "wx", 1)
    if state:
        request(conn, "!n1", 1, state)
    settle(conn)
    assert cursor(conn, "!n1", "wx") is None
    assert conn.execute("SELECT COUNT(*) FROM board_cursor_pending").fetchone()[0] == 0
    # The retry sees the same posts again.
    assert bu.read_new(conn, "!n1", "wx", NOW, MAX)[1] is not None


def test_partial_page_then_continue(conn):
    ids = [post(conn, "wx", "x" * 40) for _ in range(4)]
    body, high = read(conn, "!n1", "wx", 1)
    assert "more: READ wx NEW" in body
    assert len(body.encode()) <= MAX
    assert high < ids[-1]
    request(conn, "!n1", 1, "acked")
    settle(conn)
    assert cursor(conn, "!n1", "wx") == high
    body2, high2 = read(conn, "!n1", "wx", 2)
    assert high2 > high
    shown = body.count("x" * 40) + body2.count("x" * 40)
    assert shown <= 4
    # Walk to the end: every post is shown exactly once.
    seen, n = shown, 3
    request(conn, "!n1", 2, "acked")
    settle(conn)
    while "more:" in body2:
        body2, high2 = read(conn, "!n1", "wx", n)
        seen += body2.count("x" * 40)
        request(conn, "!n1", n, "acked")
        settle(conn)
        n += 1
    assert seen == 4
    assert cursor(conn, "!n1", "wx") == ids[-1]


def test_overlapping_requests_settle_independently_with_max(conn):
    ids = [post(conn, "wx", "y" * 40) for _ in range(4)]
    _, h1 = read(conn, "!n1", "wx", 1)  # before the first is acked
    _, h2 = read(conn, "!n1", "wx", 2)  # retry: same posts
    assert h1 == h2
    post(conn, "wx", "late")
    request(conn, "!n1", 2, "acked")
    settle(conn)
    assert cursor(conn, "!n1", "wx") == h2
    request(conn, "!n1", 1, "acked")
    settle(conn)
    assert cursor(conn, "!n1", "wx") == h1
    # Never skips: ids above the shown high stay unread.
    assert bu.unread_counts(conn, "!n1", NOW) == [("wx", 5 - ids.index(h1) - 1)]


def test_cursor_never_moves_backwards(conn):
    post(conn, "wx", "z" * 40)
    post(conn, "wx", "z" * 40)
    _, early = read(conn, "!n1", "wx", 1)
    request(conn, "!n1", 1, "acked")
    settle(conn)
    conn.execute("UPDATE board_cursors SET last_read_id=last_read_id+50")
    bu.remember(conn, "!n1", 9, "wx", early, NOW)
    request(conn, "!n1", 9, "acked")
    settle(conn)
    assert cursor(conn, "!n1", "wx") == early + 50


def test_restart_persists(tmp_path):
    path = str(tmp_path / "o.db")
    c = connect(path)
    migrate(c)
    last = post(c, "wx")
    read(c, "!n1", "wx", 1)
    c.close()
    c = connect(path)  # new connection: pending is durable
    request(c, "!n1", 1, "acked")
    settle(c)
    c.close()
    c = connect(path)
    assert cursor(c, "!n1", "wx") == last
    assert bu.unread_counts(c, "!n1", NOW) == []
    c.close()


def test_hidden_posts_are_skipped(conn):
    post(conn, "wx", "gone", visible=0)
    keep = post(conn, "wx", "kept")
    hidden_tail = post(conn, "wx", "later gone", visible=0)
    assert bu.unread_counts(conn, "!n1", NOW) == [("wx", 1)]
    body, high = read(conn, "!n1", "wx", 1)
    assert "kept" in body and "gone" not in body
    assert high == keep
    request(conn, "!n1", 1, "acked")
    settle(conn)
    assert hidden_tail > cursor(conn, "!n1", "wx")
    assert bu.unread_counts(conn, "!n1", NOW) == []
    assert bu.read_new(conn, "!n1", "wx", NOW, MAX) == ("#wx: no new posts", None)


def test_oversized_single_post_is_truncated_not_skipped(conn):
    post(conn, "wx", "w" * 300)
    body, high = read(conn, "!n1", "wx", 1)
    assert high is not None and len(body.encode()) <= MAX


def test_boards_new_text_fits_budget(conn):
    counts = [(f"board{i:02d}", 100 + i) for i in range(30)]
    text = bu.boards_new_text(counts, MAX)
    assert len(text.encode()) <= MAX and text.startswith("Boards new\nboard00:100")


def test_stale_cursors_and_pending_pruned(conn):
    conn.execute("INSERT INTO board_cursors VALUES ('!old','wx',1,?)", (NOW - 91 * 86400,))
    conn.execute("INSERT INTO board_cursors VALUES ('!new','wx',1,?)", (NOW - 86400,))
    conn.execute("INSERT INTO board_cursor_pending VALUES ('!n1',5,'wx',3,?)", (NOW - 9999,))
    conn.commit()
    request(conn, "!n1", 5, "queued", now=NOW - 9999)
    settle(conn)
    assert cursor(conn, "!old", "wx") is None and cursor(conn, "!new", "wx") == 1
    assert conn.execute("SELECT COUNT(*) FROM board_cursor_pending").fetchone()[0] == 0


@pytest.mark.parametrize("first,second", [("meshtastic", "meshcore"), ("meshcore", "meshtastic")])
def test_shared_bbs_isolates_mesh_ids(tmp_path, first, second):
    from meshgarden import bbs_ids

    bbs = str(tmp_path / "shared" / "bbs.db")
    conns = []
    for name, transport in (("a", first), ("b", second)):
        c = connect(str(tmp_path / name / "o.db"), bbs_path=bbs, transport=transport)
        migrate(c, transport=transport)
        conns.append((c, transport))
    (ca, ta), (cb, tb) = conns
    last = post(ca, "wx")
    reader_a = bbs_ids.qualify("!aabbccdd", ta)
    reader_b = bbs_ids.qualify("!aabbccdd", tb)
    assert reader_a != reader_b
    read(ca, reader_a, "wx", 1)
    request(ca, "!aabbccdd", 1, "acked")
    bu.settle(ca, NOW + 10, 360, local_id=lambda s: bbs_ids.local_or_none(s, ta))
    assert cursor(cb, reader_a, "wx") == last  # shared file
    assert bu.unread_counts(cb, reader_b, NOW) == [("wx", 1)]  # other mesh unaffected
    # Other daemon's settle must leave this mesh's pending row alone.
    post(ca, "wx", "more")
    read(ca, reader_a, "wx", 2)
    bu.settle(cb, NOW + 10, 360, local_id=lambda s: bbs_ids.local_or_none(s, tb))
    assert cb.execute("SELECT COUNT(*) FROM board_cursor_pending").fetchone()[0] == 1
    for c, _ in conns:
        c.close()


def dm(daemon, text, packet_id):
    daemon.iface.emit_packet(
        make_packet(from_num=0xAABBCCDD, to_num=daemon.my_node_num, text=text, packet_id=packet_id)
    )
    daemon.drain()


def ack(daemon, request_packet):
    tx = daemon.conn.execute(
        "SELECT tx_packet_id FROM processed_requests WHERE packet_id=?", (request_packet,)
    ).fetchone()[0]
    daemon.iface.emit_packet(
        make_packet(
            from_num=0xAABBCCDD, to_num=daemon.my_node_num, portnum="ROUTING_APP", text=None,
            packet_id=request_packet + 9000, request_id=tx, routing_error="NONE",
        )
    )
    daemon.drain()


def test_daemon_boards_new_and_read_new_flow(daemon):
    now = int(daemon._clock.time())
    post(daemon.conn, "wx", "rain", at=now)
    post(daemon.conn, "ham", "73", at=now)
    dm(daemon, "BOARDS NEW", 800)
    text = daemon.iface.sent[-1]["text"]
    assert "wx:1" in text and "ham:1" in text
    assert len(text.encode()) <= daemon._transport.max_text_bytes
    assert daemon.conn.execute("SELECT COUNT(*) FROM board_cursor_pending").fetchone()[0] == 0
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    dm(daemon, "READ wx NEW", 801)
    assert "rain" in daemon.iface.sent[-1]["text"]
    assert daemon.conn.execute("SELECT COUNT(*) FROM board_cursors").fetchone()[0] == 0
    ack(daemon, 801)
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    dm(daemon, "READ wx NEW", 802)  # drives settle before answering
    assert "no new posts" in daemon.iface.sent[-1]["text"]
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    dm(daemon, "BOARDS NEW", 803)
    text = daemon.iface.sent[-1]["text"]
    assert "ham:1" in text and "wx" not in text


def test_boards_bare_with_other_argument_still_unknown():
    from meshgarden.commands import Command, parse_command

    assert parse_command("BOARDS NEW").command == Command.BOARDS
    assert parse_command("BOARDS NEW").argument == "NEW"
    assert parse_command("BOARDS now").command == Command.UNKNOWN
    assert parse_command("BOARDS").argument == ""
