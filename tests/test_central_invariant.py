import pathlib
import re

import pytest

from tests.fakes import FakeInterface, make_packet  # noqa: F401

CHANNELS = [0, 1, 2, 3, 4, 5, 6, 7]
BROADCAST_TEXTS = [
    "MENU",
    "menu",
    "MENU STATS",
    "MENU BOARDS",
    "MENU GAMES",
    "MENU LAB",
    "?",
    "? GAMES",
    "? boards",
    "HELP",
    "help",
    "HELP sign",
    "help sign",
    "? sign",
    "? xyz",
    "SOS",
    "EMERGENCY",
    "INFO",
    "STATS",
    "GUESTBOOK",
    "ICHING",
    "iching",
    "HAIKU",
    "haiku",
    "BOOK",
    "book",
    "BOOK 1984",
    "WORDLE",
    "wordle",
    "WORDLE crane",
    "WORDLE clue",
    "WORDLE giveup",
    "wordle i give up",
    "WHOAMI",
    "whoami",
    "SIGNAL",
    "GARDEN",
    "garden",
    "RECORD",
    "record",
    "WEEK",
    "week",
    "UNSIGN",
    "unsign",
    "SEEN !aabbccdd",
    "seen ccdd",
    "SEEN nope",
    "WHO",
    "who",
    "ME",
    "me",
    "PING",
    "ping",
    "SIGN hello everyone",
    "sign hello",
    "POST bike lost my dog test",
    "post bike test",
    "READ bike",
    "read bike 2",
    "BOARDS",
    "boards",
    "MAIL",
    "mail",
    "MAIL ON",
    "MAIL OFF",
    "MAIL !aabbccdd hello test",
    "SUPERGARDENME",
    "supergardenme",
    "random chatter",
    "",
    # Task 3C (ADDED to the parametrize list; no existing assertion changed).
    # `ChannelTriggerPolicy.token` -- the MeshCore channel-knock word -- on
    # a Meshtastic broadcast. `RadioConfig.meshcore_channel` defaults to a
    # non-empty string, so a Meshtastic-backed `Daemon` really does build a
    # `_meshcore_channel_policy`; the ONLY thing keeping Meshtastic strictly
    # DM-triggered is that `_process_obs` passes `channel_policy=None` as a
    # hardcoded literal. If a future edit ever "wires up" that field, these
    # two rows are what notice.
    "!garden",
    "meshgarden: !garden",
]


@pytest.mark.parametrize("channel", CHANNELS)
@pytest.mark.parametrize("text", BROADCAST_TEXTS)
def test_no_broadcast_on_any_channel_ever_causes_a_send(daemon, channel, text):
    """THE central invariant, asserted at the send boundary."""
    daemon.iface.emit_packet(
        make_packet(to_num=0xFFFFFFFF, channel=channel, text=text, packet_id=1000 + channel)
    )
    daemon.drain()
    assert daemon.iface.sent == []


@pytest.mark.parametrize("channel", CHANNELS)
def test_dm_on_any_channel_does_cause_exactly_one_send(daemon, channel):
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, channel=channel, text="HELP", packet_id=2000 + channel
        )
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1


def test_dm_addressed_to_a_third_party_causes_no_send(daemon):
    daemon.iface.emit_packet(make_packet(to_num=0x99999999, text="HELP"))
    daemon.drain()
    assert daemon.iface.sent == []


def test_self_echo_causes_no_send(daemon):
    daemon.iface.emit_packet(
        make_packet(from_num=daemon.my_node_num, to_num=daemon.my_node_num, text="HELP")
    )
    daemon.drain()
    assert daemon.iface.sent == []


def test_every_send_is_a_unicast_dm_with_ack_and_no_response_flag(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="HELP"))
    daemon.drain()
    call = daemon.iface.sent[0]
    assert call["destinationId"] == "!aabbccdd"
    assert call["destinationId"] not in ("^all", "^local")
    assert call["wantAck"] is True
    assert call["wantResponse"] is False


def test_every_send_traces_to_one_eligible_request(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="HELP", packet_id=4242))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    row = daemon.conn.execute(
        "SELECT * FROM processed_requests WHERE tx_packet_id IS NOT NULL"
    ).fetchone()
    assert row["sender_node_id"] == "!aabbccdd" and row["packet_id"] == 4242


def test_identity_mismatch_produces_zero_sends(daemon_mismatched):
    daemon_mismatched.iface.emit_packet(make_packet(to_num=0xDEADBEEF, text="HELP"))
    daemon_mismatched.drain()
    assert daemon_mismatched.iface.sent == []


def test_only_sender_module_calls_send_text():
    """Static guard: grep the package for send_text call sites, AND for
    send_dm call sites, so the guard's reach actually matches the two-layer
    transmit path introduced in task 2B.

    `send_text`/`sendText` (the raw radio primitive) may be called only from
    `radio.py` (its owner) and `meshtastic_transport.py` (the transport
    wrapper `sender.py` now calls through instead of calling it directly).
    `send_dm` (the `Transport` Protocol's one transmitting member) may be
    called only from `sender.py` -- that second grep is what actually
    enforces "sender.py remains the only caller of the abstraction"; without
    it, nothing stopped a future module from calling `transport.send_dm(...)`
    directly and bypassing `Sender`'s guards entirely, and the first grep
    alone would never have noticed."""
    pkg = pathlib.Path("meshgarden")
    send_text_offenders = []
    send_dm_offenders = []
    for p in pkg.glob("*.py"):
        text = p.read_text()
        if p.name not in ("radio.py", "sender.py", "meshtastic_transport.py") and re.search(
            r"\.send_text\s*\(|\.sendText\s*\(", text
        ):
            send_text_offenders.append(p.name)
        if p.name != "sender.py" and re.search(r"\.send_dm\s*\(", text):
            send_dm_offenders.append(p.name)
    assert send_text_offenders == []
    assert send_dm_offenders == []
