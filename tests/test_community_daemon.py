"""Radio-facing integration coverage for community/update commands."""

from dataclasses import replace

import pytest

from meshgarden.mail import Mail, MailPrefs
from tests.fakes import make_packet


def dm(daemon, text, packet_id, *, from_num=0xAABBCCDD, to_num=None):
    daemon.iface.emit_packet(
        make_packet(
            from_num=from_num,
            to_num=daemon.my_node_num if to_num is None else to_num,
            text=text,
            packet_id=packet_id,
        )
    )
    daemon.drain()


def cooldown(daemon):
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)


def test_new_cursor_commits_only_after_real_routing_ack(daemon):
    dm(daemon, "NEW", 700)
    assert daemon.iface.sent
    assert daemon.conn.execute("SELECT 1 FROM update_cursors").fetchone() is None
    tx = daemon.conn.execute(
        "SELECT tx_packet_id FROM processed_requests WHERE packet_id=700"
    ).fetchone()[0]
    daemon.iface.emit_packet(
        make_packet(
            from_num=0xAABBCCDD,
            to_num=daemon.my_node_num,
            portnum="ROUTING_APP",
            text=None,
            packet_id=9700,
            request_id=tx,
            routing_error="NONE",
        )
    )
    daemon.drain()
    # Settlement runs on the daemon DB worker; a subsequent NEW drives the
    # same helper without crossing SQLite thread ownership.
    cooldown(daemon)
    dm(daemon, "NEW", 701)
    assert daemon.conn.execute("SELECT 1 FROM update_cursors").fetchone() is not None


@pytest.mark.parametrize("command", ["NEW", "NEWS", "LOCAL", "GUIDE", "CHECK", "INBOX"])
def test_community_dm_commands_are_one_bounded_reply(daemon, command):
    dm(daemon, command, 710 + len(command))
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["destinationId"] == "!aabbccdd"
    assert len(daemon.iface.sent[0]["text"].encode()) <= daemon._transport.max_text_bytes


@pytest.mark.parametrize("command", ["NEW", "NEWS", "LOCAL", "GUIDE", "CHECK", "INBOX"])
def test_community_commands_stay_silent_on_broadcast(daemon, command):
    dm(daemon, command, 730 + len(command), to_num=0xFFFFFFFF)
    assert daemon.iface.sent == []


def test_typo_gets_one_hint_but_arbitrary_text_stays_silent(daemon):
    dm(daemon, "GUESTBOK", 750)
    assert len(daemon.iface.sent) == 1
    assert "GUESTBOOK" in daemon.iface.sent[0]["text"]
    # The second near-match is blocked by the ordinary per-node cooldown.
    dm(daemon, "GUESTBOK", 751)
    assert len(daemon.iface.sent) == 1
    cooldown(daemon)
    dm(daemon, "just some arbitrary text", 752)
    assert len(daemon.iface.sent) == 1


def test_public_mail_toggle_controls_locked_visitor_fallback(daemon):
    # The fixture unlocks only the standard sender; this node is LAB-locked.
    dm(daemon, "MAIL", 760, from_num=0xDEADBEEF)
    assert len(daemon.iface.sent) == 1
    daemon.iface.sent.clear()
    daemon.cfg = replace(daemon.cfg, service=replace(daemon.cfg.service, mail_public=False))
    cooldown(daemon)
    dm(daemon, "MAIL", 761, from_num=0xDEADBEEF)
    assert daemon.iface.sent == []


def test_reply_route_is_owner_only_and_expires(daemon):
    now = int(daemon._clock.time())
    owner = "!aabbccdd"
    sender = "!deadbeef"
    MailPrefs(daemon.conn).set(sender, True, now)
    mail_id = 901
    Mail(daemon.conn).remember_reply_route(mail_id, sender, owner, now, expiry_days=7)

    dm(daemon, f"REPLY #{mail_id} hello", 770, from_num=0xAABBCCDD)
    assert daemon.conn.execute(
        "SELECT 1 FROM mail WHERE sender_node_id=? AND recipient_node_id=? AND message=?",
        (owner, sender, "hello"),
    ).fetchone() is not None
    daemon.iface.sent.clear()
    cooldown(daemon)
    dm(daemon, f"REPLY #{mail_id} spoof", 771, from_num=0xCAFEBABE)
    assert "REPLY unavailable" in daemon.iface.sent[-1]["text"]

    daemon.iface.sent.clear()
    cooldown(daemon)
    Mail(daemon.conn).remember_reply_route(902, sender, owner, now - 8 * 86400, 7)
    dm(daemon, "REPLY #902 late", 772, from_num=0xAABBCCDD)
    assert "REPLY unavailable" in daemon.iface.sent[-1]["text"]
