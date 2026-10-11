"""The transport boundary's own tests.

`Transport` is a Protocol with no implementation in this task, so what is
tested here is what actually carries behavior: the `Inbound` model, the
channel-trigger policy, the re-derived eligibility table, and the two
structural guarantees (no channel-send member; a knock cannot reach the
command dispatcher). The wiring itself is 2B's and is not faked here.
"""

import dataclasses

import pytest

from meshgarden.commands import (
    BROADCAST_NUM,
    InboundDecision,
    Outcome,
    Reject,
    check_eligibility,
    decide_inbound,
)
from meshgarden.config import NODE_ID_RE, node_id_from_num
from meshgarden.models import IncomingPacket
from meshgarden.radio import as_inbound, extract
from meshgarden.transport import (
    BROADCAST,
    ChannelScope,
    ChannelTriggerPolicy,
    Inbound,
    Signal,
    Transport,
)
from tests.fakes import make_packet

ME_NUM = 0x11223344
ME = node_id_from_num(ME_NUM)
THEM = node_id_from_num(0xAABBCCDD)


def packet(**kw) -> IncomingPacket:
    return extract(make_packet(**kw), now=1000, now_monotonic=1.0)


def dm(**kw) -> Inbound:
    """An eligible DM to us, overridden field by field."""
    base = {
        "received_at": 1000,
        "sender": THEM,
        "recipient": ME,
        "is_text": True,
        "text": "HELP",
        "raw_id": 4242,
        "rx_time": 1000,
    }
    return Inbound(**{**base, **kw})


def decide(inb: Inbound, **kw) -> InboundDecision:
    return decide_inbound(inb, me=ME, reply_ready=kw.pop("reply_ready", True), **kw)


# --- the model ----------------------------------------------------------


def test_broadcast_sentinel_can_never_be_mistaken_for_a_node_id():
    # A transport whose ids are derived from a hash could in principle render
    # "!ffffffff"; it can never render "^all".
    assert NODE_ID_RE.match(BROADCAST) is None


def test_inbound_defaults_to_a_direct_envelope_with_no_signal():
    inb = dm()
    assert inb.channel is ChannelScope.DIRECT
    assert inb.signal == Signal(None, None)
    assert (inb.path_len, inb.direct, inb.encrypted_to_us, inb.via_gateway) == (
        None,
        None,
        None,
        None,
    )


# --- the structural guarantees ------------------------------------------

# The whole agreed surface. Channel sends are used only by a configured
# MeshCore channel reply; Meshtastic's implementation refuses them.
TRANSPORT_SURFACE = {
    "connect",
    "identity",
    "is_broadcast",
    "on_message",
    "send_dm",
    "reset_dm_path",
    "send_channel",
    "on_delivery",
    "max_text_bytes",
    "display_label",
    "short_label",
    "parse_node_argument",
    "status",
}


def test_transport_protocol_exposes_exactly_the_agreed_surface():
    assert {name for name in vars(Transport) if not name.startswith("_")} == TRANSPORT_SURFACE


def test_only_explicit_dm_and_channel_members_transmit():
    assert {name for name in TRANSPORT_SURFACE if name.startswith("send")} == {
        "send_dm",
        "send_channel",
    }


def test_a_decision_cannot_carry_command_text_unless_it_is_a_command():
    with pytest.raises(ValueError):
        InboundDecision(Outcome.KNOCK, command_text="HELP")
    with pytest.raises(ValueError):
        InboundDecision(Outcome.COMMAND)


def test_a_rejection_must_carry_a_reason_and_nothing_else_may():
    with pytest.raises(ValueError):
        InboundDecision(Outcome.REJECTED)
    with pytest.raises(ValueError):
        InboundDecision(Outcome.KNOCK, Reject.BROADCAST)


# --- the channel-trigger policy -----------------------------------------


@pytest.mark.parametrize(
    "text", ["!garden", "!GARDEN", "  !Garden  ", "!garden\n", "!dm", "!DM", "  !Dm\n"]
)
def test_the_knock_token_is_matched_whole_and_case_insensitively(text):
    assert ChannelTriggerPolicy().matches(text) is True


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "garden",  # the bare word is ordinary chatter
        "anyone been to the !garden lately",  # not a prefix/substring match
        "!garden HELP",  # a knock carries no payload, ever
        "!gardens",
        "x!garden",
        "dm",
        "!dm HELP",
        "!dms",
        "please !dm",
        "HELP",
    ],
)
def test_ordinary_chatter_never_knocks(text):
    assert ChannelTriggerPolicy().matches(text) is False


def test_the_token_and_cooldown_are_configurable():
    policy = ChannelTriggerPolicy(token="!mg", cooldown_seconds=3600)
    assert policy.matches("!mg") is True
    assert policy.matches("!garden") is False
    assert policy.cooldown_seconds == 3600


def test_the_default_cooldown_is_a_minute():
    assert ChannelTriggerPolicy().cooldown_seconds == 60


# --- the DM table, re-derived -------------------------------------------

# Every case `tests/test_eligibility.py` pins, expressed twice: once as a
# Meshtastic packet through the current gate, once as the `Inbound` a
# transport would build from it. The two verdicts must agree, reason for
# reason -- that agreement IS the "preserves every current rejection reason"
# claim, checked rather than asserted in prose.
PARITY_CASES = [
    ("eligible dm", {"to_num": ME_NUM, "text": "HELP"}, None),
    ("broadcast", {"to_num": BROADCAST_NUM, "text": "HELP"}, Reject.BROADCAST),
    ("third party", {"to_num": 0x99999999, "text": "HELP"}, Reject.NOT_FOR_US),
    ("self echo", {"from_num": ME_NUM, "to_num": ME_NUM, "text": "HELP"}, Reject.SELF_ORIGIN),
    (
        "non text",
        {"to_num": ME_NUM, "portnum": "POSITION_APP", "text": None},
        Reject.NOT_TEXT,
    ),
    ("undecodable", {"to_num": ME_NUM, "text": None}, Reject.NO_TEXT),
    ("no packet id", {"to_num": ME_NUM, "packet_id": None, "text": "HELP"}, Reject.NO_PACKET_ID),
    ("stale rx_time", {"to_num": ME_NUM, "rx_time": 500, "text": "HELP"}, Reject.STALE),
    ("pki dm", {"to_num": ME_NUM, "pki_encrypted": True, "text": "HELP"}, None),
]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [(kw, reason) for _, kw, reason in PARITY_CASES],
    ids=[name for name, _, _ in PARITY_CASES],
)
def test_the_new_table_returns_the_same_reason_as_the_old_one(kwargs, reason):
    obs = packet(**kwargs)
    old = check_eligibility(obs, my_node_num=ME_NUM, reply_ready=True)
    new = decide(as_inbound(obs))
    assert old.reason is reason
    assert new.reason is reason
    assert new.outcome is (Outcome.COMMAND if reason is None else Outcome.REJECTED)
    assert old.eligible is (new.outcome is Outcome.COMMAND)


def test_not_reply_ready_matches_the_old_gate():
    obs = packet(to_num=ME_NUM, text="HELP")
    old = check_eligibility(obs, my_node_num=ME_NUM, reply_ready=False)
    new = decide_inbound(as_inbound(obs), me=ME, reply_ready=False)
    assert old.reason is Reject.NOT_REPLY_READY
    assert new.reason is Reject.NOT_REPLY_READY


def test_a_zero_packet_id_is_no_packet_id_on_both_sides():
    # `packet_id == 0` is the second half of the NO_PACKET_ID row and is not
    # reachable through `make_packet`'s omission rules, so it is built by hand.
    obs = dataclasses.replace(packet(to_num=ME_NUM, text="HELP"), packet_id=0)
    assert check_eligibility(obs, my_node_num=ME_NUM, reply_ready=True).reason is (
        Reject.NO_PACKET_ID
    )
    assert decide(as_inbound(obs)).reason is Reject.NO_PACKET_ID


def test_an_absent_rx_time_falls_back_to_the_connected_grace_window():
    obs = packet(to_num=ME_NUM, rx_time=None, text="HELP")
    for connected in (0.0, 59.0):
        old = check_eligibility(
            obs, my_node_num=ME_NUM, reply_ready=True, connected_seconds=connected
        )
        new = decide(as_inbound(obs), connected_seconds=connected)
        assert old.reason is Reject.STALE
        assert new.reason is Reject.STALE
    assert decide(as_inbound(obs), connected_seconds=60.0).outcome is Outcome.COMMAND


def test_an_eligible_dm_hands_its_text_to_the_dispatcher():
    assert decide(dm(text="SIGN hello")).command_text == "SIGN hello"


# --- the channel-knock branch -------------------------------------------

POLICY = ChannelTriggerPolicy()


def chan(**kw) -> Inbound:
    base = {"recipient": BROADCAST, "channel": ChannelScope.WATCHED, "text": "!garden"}
    return dm(**{**base, **kw})


def test_a_knock_on_the_watched_channel_is_a_knock():
    d = decide(chan(), channel_policy=POLICY)
    assert d.outcome is Outcome.KNOCK
    assert d.command_text is None


def test_without_a_policy_the_same_message_is_just_a_broadcast():
    # This is the Meshtastic case: no channel policy, so the knock branch is
    # unreachable and the DM table decides, exactly as it does today.
    assert decide(chan()).reason is Reject.BROADCAST


def test_a_message_on_some_other_channel_is_never_a_knock():
    inb = chan(channel=ChannelScope.OTHER)
    assert decide(inb, channel_policy=POLICY).reason is Reject.BROADCAST


def test_chatter_on_the_watched_channel_is_rejected_silently():
    assert decide(chan(text="morning all"), channel_policy=POLICY).reason is Reject.BROADCAST


@pytest.mark.parametrize(
    "text",
    ["HELP", "?", "MENU", "SIGN hello", "SEEN !abcd", "MAIL !abcd hi", "!garden HELP", "WORDLE"],
)
def test_no_watched_channel_message_can_ever_become_a_command(text):
    d = decide(chan(text=text), channel_policy=POLICY)
    assert d.outcome is not Outcome.COMMAND
    assert d.command_text is None


def test_a_knock_is_held_to_the_same_soundness_gates_as_a_dm():
    assert decide(chan(is_text=False), channel_policy=POLICY).reason is Reject.NOT_TEXT
    assert decide(chan(text=None), channel_policy=POLICY).reason is Reject.NO_TEXT
    assert decide(chan(raw_id=None), channel_policy=POLICY).reason is Reject.NO_PACKET_ID
    assert decide(chan(raw_id=0), channel_policy=POLICY).reason is Reject.NO_PACKET_ID
    assert (
        decide_inbound(chan(), me=ME, reply_ready=False, channel_policy=POLICY).reason
        is Reject.NOT_REPLY_READY
    )
    assert decide(chan(rx_time=500), channel_policy=POLICY).reason is Reject.STALE


def test_our_own_message_echoed_back_from_the_channel_is_not_a_knock():
    assert decide(chan(sender=ME), channel_policy=POLICY).reason is Reject.SELF_ORIGIN


def test_a_direct_message_is_still_a_command_even_when_a_channel_policy_is_configured():
    """2A's review flagged this forward: every existing test passes either
    no `channel_policy` at all or a WATCHED-channel message, so the actual
    production MeshCore shape -- a policy configured AND a real DM arriving
    on `ChannelScope.DIRECT` -- was never pinned. Row 1b is unreachable
    outside `WATCHED`, so a DIRECT DM must fall through to the ordinary DM
    table and become a COMMAND exactly as it would with no policy at all."""
    d = decide(dm(text="SIGN hello"), channel_policy=POLICY)
    assert d.outcome is Outcome.COMMAND
    assert d.command_text == "SIGN hello"
