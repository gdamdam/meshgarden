"""Name labels resolve at read time while identity and payload bounds stay intact."""

from dataclasses import replace

import pytest

from meshgarden.commands import mail_list_shown_count, mail_list_text
from meshgarden.config import ServiceConfig
from meshgarden.database import connect, migrate
from meshgarden.guestbook import Guestbook, format_guestbook, lookup_node_name
from meshgarden.mail import MailEntry
from meshgarden.meshcore_transport import Event, EventType
from meshgarden.web import _short_name_for_record
from tests.test_daemon_meshcore import VISITOR_NODE_ID, VISITOR_PUBKEY, _cfg, _start_meshcore_daemon


@pytest.fixture
def storage(tmp_path):
    conn = connect(str(tmp_path / "names.db"))
    migrate(conn)
    yield conn, Guestbook(conn, ServiceConfig())
    conn.close()


def set_name(conn, short=None, long=None):
    with conn:
        conn.execute(
            "INSERT INTO nodes(node_id, observed_first_seen, observed_last_seen, "
            "short_name, long_name) VALUES (?, 1, 1, ?, ?) "
            "ON CONFLICT(node_id) DO UPDATE SET short_name=excluded.short_name, "
            "long_name=excluded.long_name",
            (VISITOR_NODE_ID, short, long),
        )


@pytest.mark.parametrize("topic", [None, "radio"])
def test_old_entries_use_newly_learned_name_on_every_read(storage, topic):
    conn, gb = storage
    with conn:
        entry_id = gb.insert(VISITOR_NODE_ID, None, "hello", 1, topic=topic)
    set_name(conn, long="Oak Grove")
    for entry in [
        gb.latest_visible(3, topic)[0],
        gb.page_visible(1, 3, topic)[0],
        gb.get_visible(entry_id, topic),
        gb.list_entries(3, topic=topic)[0],
    ]:
        assert entry.display_name == "Oak Grove"
    assert "Oak Grove: hello" in format_guestbook(gb.latest_visible(3, topic), 120)
    assert conn.execute("SELECT display_name FROM guestbook").fetchone()[0] is None
    set_name(conn, short="New Oak")
    assert gb.get_visible(entry_id, topic).display_name == "New Oak"


def test_name_fallback_sanitization_and_snapshot_are_preserved(storage):
    conn, gb = storage
    with conn:
        entry_id = gb.insert(VISITOR_NODE_ID, "Old Name", "hello", 1)
    set_name(conn, short="\x00\n", long="Oak\u202e Grove")
    assert lookup_node_name(conn, VISITOR_NODE_ID) == "Oak Grove"
    assert gb.get_visible(entry_id).display_name == "Oak Grove"
    set_name(conn, short=" ", long=None)
    assert gb.get_visible(entry_id).display_name == "Old Name"
    with conn:
        conn.execute("UPDATE guestbook SET display_name=NULL")
    assert "ccdd: hello" in format_guestbook(gb.latest_visible(3), 120)


def test_long_multibyte_labels_fit_meshcore(storage):
    conn, gb = storage
    with conn:
        gb.insert(VISITOR_NODE_ID, None, "message " * 12, 1)
    set_name(conn, short="🌳" * 30)
    text = format_guestbook(gb.latest_visible(3), 120)
    assert "🌳🌳: " in text
    assert len(text.encode()) <= 120
    assert _short_name_for_record(conn, VISITOR_NODE_ID) == "🌳" * 30


@pytest.mark.parametrize("command", ["SEEN !aabbccdd", "RECORD", "ME", "MAIL"])
def test_meshcore_reports_include_known_name_with_id(tmp_path, command):
    d, ft = _start_meshcore_daemon(_cfg(tmp_path))
    try:
        ft.fake.emit_new_contact(VISITOR_PUBKEY, "OakGrove")
        ft.fake._dispatch(
            Event(
                EventType.CONTACT_MSG_RECV,
                {
                    "pubkey_prefix": VISITOR_PUBKEY[:12],
                    "text": "not a command",
                    "path_len": 0,
                    "SNR": 6.5,
                    "sender_timestamp": 0,
                },
            )
        )
        d.drain()
        with d.conn:
            d.conn.execute("INSERT INTO lab_nodes VALUES (?,1)", (VISITOR_NODE_ID,))
            d.conn.execute(
                "INSERT INTO mail(created_at,sender_node_id,recipient_node_id, "
                "message,message_hash) VALUES (1,?,?,?,?)",
                (VISITOR_NODE_ID, VISITOR_NODE_ID, "hi", "hash"),
            )
        ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], command)
        d.drain()
        text = ft.fake.sent[-1]["msg"]
        assert "OakGrove" in text
        assert "ccdd" in text
        assert len(text.encode()) <= 120
    finally:
        d.stop()


def test_mail_delivery_count_uses_the_same_name_labels_as_the_reply():
    first = MailEntry(1, 100, VISITOR_NODE_ID, "!11223344", "one" * 8, False, None)
    entries = [first, replace(first, id=2, message="two" * 8)]
    labels = {VISITOR_NODE_ID: "OakGrove · ccdd"}
    text = mail_list_text(entries, now=110, max_bytes=100, labels=labels)
    assert "OakGrove · ccdd" in text
    assert len(text.encode()) <= 100
    assert mail_list_shown_count(entries, now=110, max_bytes=100, labels=labels) == 1
    assert "two" not in text


def test_partial_mail_keeps_known_sender_name_and_id():
    entry = MailEntry(1, 100, VISITOR_NODE_ID, "!11223344", "hello " * 100, False, None)
    text = mail_list_text(
        [entry], now=110, max_bytes=120, labels={VISITOR_NODE_ID: "OakGrove · ccdd"}
    )
    assert "OakGrove · ccdd" in text
    assert "More: MAIL" in text
    assert len(text.encode()) <= 120
