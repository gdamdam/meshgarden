import sqlite3

import pytest

from meshgarden.commands import mail_page, mail_pref_ack_text
from meshgarden.database import connect, migrate
from meshgarden.mail import REPLY_ROUTES_SQL, Mail


def _install_reply_routes(conn):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mail_reply_routes'"
    ).fetchone()
    if exists is None:
        conn.executescript(REPLY_ROUTES_SQL)


@pytest.fixture
def conn(tmp_path):
    db = connect(str(tmp_path / "mail-upgrade.db"))
    migrate(db)
    _install_reply_routes(db)
    yield db
    db.close()


def test_reply_route_schema_retains_no_message_body(conn):
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(mail_reply_routes)").fetchall()
    }
    assert columns == {"mail_id", "sender_node_id", "recipient_node_id", "expires_at"}


def test_mail_page_numbers_notes_and_shows_compact_route_expiry(conn):
    mail = Mail(conn)
    entry_id = mail.send("!11111111", "!22222222", "hello", 1_000)

    text, receipts = mail_page(
        mail.pending_for("!22222222"), now=1_000, max_bytes=120, expiry_days=7
    )

    assert len(text.encode("utf-8")) <= 120
    assert f"#{entry_id} from 1111" in text
    assert "reply 7d left" in text
    assert "hello" in text
    assert "REPLY #id <msg>" in text
    assert receipts == [(entry_id, "hello", len("hello"))]


def test_tight_mail_page_keeps_body_chunk_ahead_of_optional_reply_hint(conn):
    mail = Mail(conn)
    entry_id = mail.send("!11111111", "!22222222", "é" * 30, 2_000)

    text, receipts = mail_page(
        mail.pending_for("!22222222"), now=2_000, max_bytes=40, expiry_days=7
    )

    assert len(text.encode("utf-8")) <= 40
    assert text.startswith(f"#{entry_id} 1111")
    assert receipts and receipts[0][2] > 0
    assert receipts[0][1][: receipts[0][2]] in text
    assert "More: MAIL" in text


def test_mail_page_never_overstates_subday_reply_lifetime(conn):
    mail = Mail(conn)
    mail.send("!11111111", "!22222222", "soon", 3_000)
    text, _ = mail_page(
        mail.pending_for("!22222222"),
        now=3_000 + 7 * 86400 - 1_800,
        max_bytes=120,
        expiry_days=7,
    )
    assert "reply <1h left" in text

    text, _ = mail_page(
        mail.pending_for("!22222222"),
        now=3_000 + 7 * 86400 - 5_400,
        max_bytes=120,
        expiry_days=7,
    )
    assert "reply 1h left" in text


def test_mail_page_never_rounds_reply_days_up(conn):
    mail = Mail(conn)
    mail.send("!11111111", "!22222222", "soon", 3_000)
    text, _ = mail_page(
        mail.pending_for("!22222222"),
        now=3_000 + 1,
        max_bytes=120,
        expiry_days=7,
    )
    assert "reply 6d left" in text


def test_mail_opt_in_ack_teaches_reply_syntax_within_meshcore_budget():
    text = mail_pref_ack_text(True)
    assert "Mail is ON" in text
    assert "REPLY #id <msg>" in text
    assert len(text.encode("utf-8")) <= 120


def test_full_ack_atomically_replaces_body_with_authorized_route(conn):
    mail = Mail(conn)
    entry_id = mail.send("!11111111", "!22222222", "private body", 1_000)

    consumed = mail.consume_delivered(
        [(entry_id, "private body", len("private body"))], expiry_days=7
    )

    assert consumed == 1
    assert mail.pending_count("!22222222") == 0
    assert conn.execute("SELECT message FROM mail WHERE id = ?", (entry_id,)).fetchone() is None
    route = mail.lookup_reply_route(entry_id, "!22222222", 1_001)
    assert route is not None
    assert route.sender_node_id == "!11111111"
    assert route.recipient_node_id == "!22222222"
    assert route.expires_at == 1_000 + 7 * 86400
    assert mail.lookup_reply_route(entry_id, "!33333333", 1_001) is None
    assert (
        mail.consume_delivered(
            [(entry_id, "private body", len("private body"))], expiry_days=7
        )
        == 0
    )


def test_route_survives_reopen_after_original_body_is_deleted(tmp_path):
    path = tmp_path / "persistent.db"
    conn = connect(str(path))
    migrate(conn)
    _install_reply_routes(conn)
    entry_id = Mail(conn).send("!11111111", "!22222222", "gone after ACK", 2_000)
    Mail(conn).consume_delivered(
        [(entry_id, "gone after ACK", len("gone after ACK"))], expiry_days=1
    )
    conn.close()

    reopened = connect(str(path))
    try:
        route = Mail(reopened).lookup_reply_route(entry_id, "!22222222", 2_001)
        assert route is not None
        assert route.sender_node_id == "!11111111"
        assert reopened.execute("SELECT 1 FROM mail WHERE id = ?", (entry_id,)).fetchone() is None
    finally:
        reopened.close()


def test_missing_route_schema_rolls_back_body_consumption(tmp_path):
    conn = connect(str(tmp_path / "unmigrated-route.db"))
    migrate(conn)
    try:
        conn.execute("DROP TABLE IF EXISTS mail_reply_routes")
        mail = Mail(conn)
        entry_id = mail.send("!11111111", "!22222222", "keep on failure", 2_500)
        with pytest.raises(sqlite3.OperationalError, match="mail_reply_routes"):
            mail.consume_delivered(
                [(entry_id, "keep on failure", len("keep on failure"))], expiry_days=1
            )
        assert mail.pending_for("!22222222")[0].message == "keep on failure"
    finally:
        conn.close()


def test_stale_or_unauthorized_receipt_cannot_create_a_route(conn):
    mail = Mail(conn)
    entry_id = mail.send("!11111111", "!22222222", "current", 3_000)

    consumed = mail.consume_delivered([(entry_id, "stale snapshot", 5)], expiry_days=7)

    assert consumed == 0
    assert mail.pending_for("!22222222")[0].message == "current"
    assert conn.execute(
        "SELECT 1 FROM mail_reply_routes WHERE mail_id = ?", (entry_id,)
    ).fetchone() is None


def test_partial_ack_remembers_route_and_keeps_only_unread_suffix(conn):
    mail = Mail(conn)
    entry_id = mail.send("!11111111", "!22222222", "abcdef", 4_000)

    consumed = mail.consume_delivered([(entry_id, "abcdef", 3)], expiry_days=2)

    assert consumed == 0
    assert mail.pending_for("!22222222")[0].message == "def"
    assert mail.lookup_reply_route(entry_id, "!22222222", 4_001) is not None
    # Replaying the old receipt cannot consume the suffix or mutate the route.
    assert mail.consume_delivered([(entry_id, "abcdef", 3)], expiry_days=9) == 0
    assert mail.pending_for("!22222222")[0].message == "def"
    assert mail.lookup_reply_route(entry_id, "!22222222", 4_001).expires_at == 4_000 + 2 * 86400


def test_expiry_is_anchored_to_creation_and_pruning_is_batched(conn):
    mail = Mail(conn)
    for entry_id in range(1, 4):
        assert mail.remember_reply_route(
            entry_id, "!11111111", "!22222222", created_at=100, expiry_days=1
        )

    deadline = 100 + 86400
    assert mail.lookup_reply_route(1, "!22222222", deadline - 1) is not None
    assert mail.lookup_reply_route(1, "!22222222", deadline) is None
    assert mail.prune_reply_routes(deadline, batch=2) == 3
    assert conn.execute("SELECT COUNT(*) FROM mail_reply_routes").fetchone()[0] == 0


@pytest.mark.parametrize("method", ["consume", "remember"])
def test_reply_route_lifetime_must_be_positive(conn, method):
    mail = Mail(conn)
    if method == "consume":
        with pytest.raises(ValueError, match="expiry_days"):
            mail.consume_delivered([], expiry_days=0)
    else:
        with pytest.raises(ValueError, match="expiry_days"):
            mail.remember_reply_route(1, "!1", "!2", 0, 0)
