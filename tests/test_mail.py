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
