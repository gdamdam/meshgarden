import pytest

from meshgarden.database import connect, migrate
from meshgarden.mail import Mail, MailAliases, MailPrefs, normalize_mail_alias


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "o.db"))
    migrate(c)
    yield c
    c.close()


def test_prefs_default_off(conn):
    prefs = MailPrefs(conn)
    assert prefs.get("!aabbccdd") is False


def test_prefs_set_and_get(conn):
    prefs = MailPrefs(conn)
    prefs.set("!aabbccdd", True, 1000)
    assert prefs.get("!aabbccdd") is True
    prefs.set("!aabbccdd", False, 1001)
    assert prefs.get("!aabbccdd") is False


@pytest.mark.parametrize("value", ["ab", "a" * 17, "bad space", "@prefixed", "MAIL"])
def test_invalid_or_reserved_mail_aliases(value):
    assert normalize_mail_alias(value, allow_at=False) is None


def test_mail_alias_claim_resolve_and_casefold(conn):
    aliases = MailAliases(conn)
    assert aliases.claim("!11111111", "Garden_Fox", 1000) == "claimed"
    assert aliases.get("!11111111") == "garden_fox"
    assert aliases.resolve("@GARDEN_FOX") == "!11111111"
    assert aliases.claim("!11111111", "garden_fox", 1001) == "unchanged"


def test_mail_alias_is_unique_and_changes_are_rate_limited(conn):
    aliases = MailAliases(conn)
    assert aliases.claim("!11111111", "gardenfox", 1000) == "claimed"
    assert aliases.claim("!22222222", "GARDENFOX", 1001) == "taken"
    assert aliases.claim("!11111111", "otherfox", 1001) == "cooldown"
    assert aliases.claim("!11111111", "otherfox", 1000 + 86400) == "claimed"


def test_mail_alias_release_prevents_immediate_churn(conn):
    aliases = MailAliases(conn)
    assert aliases.claim("!11111111", "gardenfox", 1000) == "claimed"
    assert aliases.release("!11111111", 2000) is True
    assert aliases.get("!11111111") is None
    assert aliases.resolve("gardenfox") is None
    assert aliases.claim("!11111111", "newfox", 2001) == "cooldown"


def test_operator_can_transfer_alias(conn):
    aliases = MailAliases(conn)
    assert aliases.operator_set("!11111111", "gardenfox", 1000) == "claimed"
    assert aliases.operator_set("!22222222", "gardenfox", 1001) == "taken"
    assert aliases.operator_set("!22222222", "gardenfox", 1002, replace=True) == "claimed"
    assert aliases.resolve("gardenfox") == "!22222222"


def test_send_and_pending_for(conn):
    mail = Mail(conn)
    mail.send("!11111111", "!22222222", "hi there", 1000)
    pending = mail.pending_for("!22222222")
    assert len(pending) == 1
    assert pending[0].message == "hi there"
    assert pending[0].sender_node_id == "!11111111"


def test_pending_count(conn):
    mail = Mail(conn)
    mail.send("!11111111", "!22222222", "one", 1000)
    mail.send("!11111111", "!22222222", "two", 1001)
    assert mail.pending_count("!22222222") == 2


def test_mark_delivered_removes_the_notes(conn):
    mail = Mail(conn)
    id1 = mail.send("!11111111", "!22222222", "one", 1000)
    mail.mark_delivered([id1])
    assert mail.pending_count("!22222222") == 0


def test_prune_expired_only_removes_old_undelivered(conn):
    mail = Mail(conn)
    mail.send("!11111111", "!22222222", "old", 1000)
    mail.send("!11111111", "!22222222", "new", 1000 + 8 * 86400)
    removed = mail.prune_expired(1000 + 8 * 86400, expiry_days=7)
    assert removed == 1
    assert mail.pending_count("!22222222") == 1


def test_pending_for_is_newest_first(conn):
    mail = Mail(conn)
    mail.send("!11111111", "!22222222", "first", 1000)
    mail.send("!11111111", "!22222222", "second", 1001)
    pending = mail.pending_for("!22222222")
    assert [p.message for p in pending] == ["second", "first"]


# --- shared bbs.db: Meshtastic daemon side of transport-qualified ids --------


@pytest.fixture
def shared_daemon(tmp_path):
    from dataclasses import replace

    from meshgarden.main import Daemon
    from tests.conftest import _cfg, _unlock_standard_lab_senders
    from tests.fakes import FakeTransport

    cfg = _cfg(tmp_path)
    cfg = replace(cfg, database=replace(cfg.database, bbs_path=str(tmp_path / "bbs.db")))
    ft = FakeTransport(0x11223344, cfg.radio.expected_node_id, service_cfg=cfg.service)
    d = Daemon(cfg, transport=ft, start_metrics=False)
    d.iface = ft.iface
    d.start()
    _unlock_standard_lab_senders(d.conn)
    yield d
    d.stop()


def _dm(daemon, text, packet_id, from_num=0xAABBCCDD):
    from tests.fakes import make_packet

    daemon.iface.emit_packet(make_packet(
        to_num=daemon.my_node_num, from_num=from_num, text=text, packet_id=packet_id))
    daemon.drain()


def test_meshtastic_sign_stores_bare_sender(shared_daemon):
    _dm(shared_daemon, "SIGN hello from meshtastic", 9501)
    rows = [tuple(r) for r in shared_daemon.conn.execute("SELECT sender_node_id FROM guestbook")]
    assert rows == [("!aabbccdd",)]


def test_meshtastic_daemon_skips_mc_mail_delivery(shared_daemon):
    """Mail for the MeshCore node with the same bare id is that mesh's:
    MAIL from the Meshtastic node must neither show nor consume it."""
    d = shared_daemon
    MailPrefs(d.conn).set("!aabbccdd", True, 1000)
    Mail(d.conn).send("mc:!12345678", "mc:!aabbccdd", "secret for meshcore", 1000)
    _dm(d, "MAIL", 9502)
    assert "secret for meshcore" not in d.iface.sent[-1]["text"]
    assert Mail(d.conn).pending_count("mc:!aabbccdd") == 1


def test_cross_mesh_mail_send_to_mc_prefix(shared_daemon):
    d = shared_daemon
    MailPrefs(d.conn).set("mc:!ffeeddcc", True, 1000)
    _dm(d, "MAIL mc:!FFEEDDCC hello other mesh", 9503)
    rows = [tuple(r) for r in d.conn.execute(
        "SELECT sender_node_id, recipient_node_id, message FROM mail")]
    assert rows == [("!aabbccdd", "mc:!ffeeddcc", "hello other mesh")]
    # The bare id is this mesh's node, a different mailbox.
    assert Mail(d.conn).pending_count("!ffeeddcc") == 0


def test_meshtastic_guestbook_never_names_an_mc_entry_from_its_nodes(shared_daemon):
    d = shared_daemon
    d.conn.execute("INSERT INTO nodes(node_id, short_name, long_name, observed_first_seen, "
                   "observed_last_seen) VALUES ('!a1d3db88', 'Evil', 'Evil', 0, 0)")
    d.conn.execute(
        "INSERT INTO guestbook(created_at, sender_node_id, display_name, message, "
        "message_hash, visible) VALUES (0, 'mc:!a1d3db88', 'Alice', 'hi', 'h', 1)")
    d.conn.commit()
    _dm(d, "GUESTBOOK", 9504)
    reply = d.iface.sent[-1]["text"]
    assert "Alice" in reply and "Evil" not in reply
    from meshgarden.guestbook import lookup_bbs_node_name
    assert lookup_bbs_node_name(d.conn, "mc:!a1d3db88") is None
    assert lookup_bbs_node_name(d.conn, "!a1d3db88") == "Evil"


def test_single_file_mail_ignores_transport_prefix(daemon, monkeypatch):
    """Without a shared bbs file `mc:` is not a prefix: the argument is
    parsed as before and, being no node id, the MAIL is dropped."""
    from meshgarden import bbs_ids

    def boom(*a, **k):
        raise AssertionError("prefix branch taken in single-file mode")

    monkeypatch.setattr(bbs_ids, "typed_node_id", boom)
    _dm(daemon, "MAIL mc:!ffeeddcc hello", 9510)
    assert daemon.conn.execute("SELECT count(*) FROM mail").fetchone()[0] == 0


def test_shared_meshtastic_menu_says_shared_with_meshcore(shared_daemon):
    _dm(shared_daemon, "MENU", 9590)
    text = shared_daemon.iface.sent[-1]["text"]
    assert text.splitlines()[0] == "🌱 MeshGarden · shared with MeshCore"
    assert "MENU DAILY" in text and "quota" in text
