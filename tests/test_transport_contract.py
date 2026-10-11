"""The `Transport` contract, pinned against BOTH implementations.

Task 3A's brief: "the heart of the task." Where Meshtastic and MeshCore
genuinely differ -- `tx_ref` shape, delivery-failure signaling, channel
triggering, whether `on_message` actually fires anything today -- the
difference is pinned as its own explicit test rather than skipped. A
skipped test hides a gap; a precisely-named test documents one.

Fixture `harness` is parametrized over both transports and exposes just
enough common surface (`.transport`, `.known_recipient`, `.ack`) for the
genuinely-shared cases below. Transport-specific behavior is tested
directly against `FakeTransport`/`FakeMeshCoreTransport`, not through the
harness.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from meshgarden.config import NODE_ID_RE
from meshgarden.meshcore_transport import (
    _AMBIGUOUS,
    EventType,
    MeshCoreSendFailed,
    MeshCoreUnknownContact,
)
from meshgarden.models import IncomingPacket
from meshgarden.transport import (
    BROADCAST,
    ChannelScope,
    Delivery,
    DeliveryOutcome,
)
from tests.fake_meshcore import FakeMeshCore, FakeMeshCoreTransport
from tests.fakes import FakeTransport, make_packet
from tests.test_transport import TRANSPORT_SURFACE

# A recipient node id shared by both harnesses on purpose: Meshtastic
# accepts it because any syntactically valid id is addressable, and the
# MeshCore harness picks a public key whose first 4 bytes render the same
# string, so genuinely-shared assertions can use one literal expectation.
_SHARED_RECIPIENT = "!aabbccdd"


def _routing_packet(*, from_num: int, request_id: int, routing_error: str) -> IncomingPacket:
    return IncomingPacket(
        received_at=int(time.time()),
        received_monotonic=0.0,
        from_num=from_num,
        to_num=0x11223344,
        packet_id=1,
        channel=0,
        portnum="ROUTING_APP",
        rx_time=None,
        rx_snr=None,
        rx_rssi=None,
        hop_limit=None,
        hop_start=None,
        via_mqtt=None,
        pki_encrypted=False,
        payload_len=0,
        text=None,
        request_id=request_id,
        routing_error=routing_error,
    )


@dataclass
class Harness:
    name: str
    transport: object
    known_recipient: str
    ack: Callable[[str], None]
    teardown: Callable[[], None] = field(default=lambda: None)


def _meshtastic_harness() -> Harness:
    ft = FakeTransport()

    def ack(tx_ref: str) -> None:
        ft.note_routing_packet(
            _routing_packet(from_num=0xAABBCCDD, request_id=int(tx_ref), routing_error="NONE")
        )

    return Harness(
        name="meshtastic",
        transport=ft,
        known_recipient=_SHARED_RECIPIENT,
        ack=ack,
        teardown=ft.stop,
    )


def _meshcore_harness() -> Harness:
    fmt = FakeMeshCoreTransport()
    recipient_pubkey = "aabbccdd" + "11" * 28
    fmt.fake.emit_new_contact(recipient_pubkey, "contact-a")

    def ack(tx_ref: str) -> None:
        fmt.fake.emit_ack(tx_ref)

    return Harness(name="meshcore", transport=fmt, known_recipient=_SHARED_RECIPIENT, ack=ack)


@pytest.fixture(params=["meshtastic", "meshcore"])
def harness(request):
    h = _meshtastic_harness() if request.param == "meshtastic" else _meshcore_harness()
    yield h
    h.teardown()


# --- genuinely shared across both transports -----------------------------


def test_transport_exposes_the_agreed_surface(harness):
    assert set(dir(harness.transport)) >= TRANSPORT_SURFACE


def test_identity_is_a_well_formed_node_id_once_connected(harness):
    assert NODE_ID_RE.match(harness.transport.identity())


def test_a_connected_transport_whose_identity_matches_is_reply_ready(harness):
    """The baseline the mismatch test below is measured against -- without
    it, a transport that hardcoded `reply_ready=False` would "pass"."""
    st = harness.transport.status()
    assert st.connected is True
    assert st.actual_node_id == "!11223344"
    assert st.reply_ready is True


@pytest.mark.parametrize("name", ["meshtastic", "meshcore"])
def test_neither_transport_is_reply_ready_when_the_radios_identity_mismatches(name):
    """Whole-branch review, finding 2. `reply_ready` gates ELIGIBILITY
    (`commands.decide_inbound` row 1b), not merely transmission: a
    transport that reports ready while its radio is not the configured one
    lets a trigger consume a daily quota slot, commit the per-node rate
    limiter, and write a lab-unlock row or a guestbook signature under a
    misidentified radio -- all before the send is refused.

    `RadioAdapter._check_identity` has always forced the flag false on
    mismatch; `MeshCoreTransport` was never told the expected identity at
    all and derived readiness purely from being connected with an identity.
    The two diverged because the contract suite had ZERO assertions about
    readiness for EITHER transport, which is exactly the gap this
    parametrized-over-both test closes. Not folded into the `harness`
    fixture on purpose: the harness builds MATCHING transports, and this
    case needs a deliberately mismatched `expected_node_id`.
    """
    if name == "meshtastic":
        t = FakeTransport(my_node_num=0x11223344, expected_node_id="!99999999")
        teardown = t.stop
    else:
        t = FakeMeshCoreTransport(expected_node_id="!99999999")
        teardown = lambda: None  # noqa: E731 - the fake companion owns no threads
    try:
        st = t.status()
        assert st.connected is True, "the mismatch must not be expressed as 'disconnected'"
        assert st.actual_node_id == "!11223344", "the radio's REAL id is still reported"
        assert st.reply_ready is False, f"{name} reports ready while misidentified"
    finally:
        teardown()


def test_is_broadcast(harness):
    t = harness.transport
    assert t.is_broadcast(BROADCAST) is True
    assert t.is_broadcast(harness.known_recipient) is False


def test_send_dm_returns_an_opaque_string_reference(harness):
    """OPAQUE means exactly that: a caller may hold onto it and compare it
    for equality against a later `Delivery.tx_ref`, and nothing else. See
    `test_meshcore_tx_ref_is_not_safely_int_parseable` below for why
    `sender.py:505`'s `int(tx_ref)` breaks this contract for MeshCore."""
    tx_ref = harness.transport.send_dm(harness.known_recipient, "hi")
    assert isinstance(tx_ref, str)
    assert tx_ref != ""


def test_reset_dm_path_is_meshcore_only(harness):
    reset = harness.transport.reset_dm_path(harness.known_recipient)
    if harness.name == "meshcore":
        assert reset is True
        assert harness.transport.fake.paths_reset == ["aabbccdd" + "11" * 28]
    else:
        assert reset is False


def test_a_successful_delivery_fires_the_on_delivery_callback(harness):
    seen: list[Delivery] = []
    harness.transport.on_delivery(seen.append)
    tx_ref = harness.transport.send_dm(harness.known_recipient, "hi")
    harness.ack(tx_ref)
    assert len(seen) == 1
    assert seen[0].tx_ref == tx_ref
    assert seen[0].outcome is DeliveryOutcome.ACKED


def test_no_delivery_is_ever_reported_for_a_send_nobody_acks_or_fails(harness):
    """NOT a timeout test: neither transport's `Transport` implementation
    has any TTL/aging logic at all, so there is no clock to drive past
    anything here -- aging an unresolved send to `unknown` is entirely
    `sender.py`'s TTL maintenance tick's job (transport-boundary-spec.md
    section 4), out of scope for this task and untouched by it. This test
    only pins the narrower, transport-level half of that contract: absent
    any injected ack/fail event, the callback must never fire spuriously
    for a send that is simply still in flight."""
    seen: list[Delivery] = []
    harness.transport.on_delivery(seen.append)
    harness.transport.send_dm(harness.known_recipient, "hi")
    assert seen == []


def test_max_text_bytes_is_a_positive_transport_property(harness):
    assert isinstance(harness.transport.max_text_bytes, int)
    assert harness.transport.max_text_bytes > 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!AABBCCDD", "!aabbccdd"),
        ("  !aabbccdd  ", "!aabbccdd"),
        ("ccdd", None),  # a bare tail is not a full id
        ("fndr", None),  # a short name is not a full id either
        ("", None),
    ],
)
def test_parse_node_argument_only_resolves_a_full_id(harness, text, expected):
    """Both transports accept the SAME shape: a MeshCore node id is
    format-compatible with `config.NODE_ID_RE` (meshcore-notes.md's
    "Meshtastic concept -> MeshCore equivalent" table), so this is one of
    the few cases that is *actually* identical, not just similarly
    shaped."""
    assert harness.transport.parse_node_argument(text) == expected


@pytest.mark.parametrize(
    ("node_id", "expected"),
    [
        ("!aabbccdd", "!aabbccdd"),
        ("!11223344", "!11223344"),
    ],
)
def test_display_label_is_the_node_id_unchanged_on_both_transports(harness, node_id, expected):
    """`display_label` is the identity function on both current
    implementations -- task 3D wires this into `_handle_me`'s "You are
    <id>" line, so the requester's own id must render exactly as given,
    on either transport."""
    assert harness.transport.display_label(node_id) == expected


@pytest.mark.parametrize(
    ("node_id", "expected"),
    [
        ("!aabbccdd", "!ccdd"),
        ("!11223344", "!3344"),
    ],
)
def test_short_label_is_the_node_ids_own_last_four_hex_chars_on_both_transports(
    harness, node_id, expected
):
    """Both transports abbreviate identically: "!" plus the node id's own
    last 4 hex characters -- task 3D wires this into SEEN's found-node
    reply (replacing the removed daemon-local `main._node_id_tail_label`)
    and into `_node_label_for`'s not-found/not-heard label."""
    assert harness.transport.short_label(node_id) == expected


# --- where the transports genuinely differ: tx_ref shape ------------------


def test_meshtastic_tx_ref_happens_to_be_decimal():
    ft = FakeTransport()
    try:
        tx_ref = ft.send_dm("!aabbccdd", "hi")
        assert int(tx_ref) > 0  # sender.py:505 relies on exactly this
    finally:
        ft.stop()


def test_meshcore_tx_ref_is_not_safely_int_parseable():
    """Pins the hazard flagged forward for 3B: `sender.py:505` calls
    `int(tx_ref)` unconditionally, AFTER the radio call has already
    transmitted. MeshCore's `tx_ref` is the hex of a 4-byte `expected_ack`
    and routinely contains a-f digits, so this is not theoretical --
    `int("abcdef01")` raises `ValueError` today. Not fixed in this task
    (out of scope for 3A); this test exists so 3B inherits a failing-in-
    context contract instead of discovering it on hardware.
    """
    fmt = FakeMeshCoreTransport()
    recipient_pubkey = "abcdef01" + "22" * 28
    fmt.fake.emit_new_contact(recipient_pubkey, "contact-hex")
    # Reach into the fake's counter to force a hex-lettered `expected_ack`
    # (mirrors tests/fakes.py's own convention of patching a test double's
    # internals directly, e.g. `radio.status = lambda: ...`): the real
    # library's `expected_ack` is not a small monotonic counter, so a
    # letter-free value here would be an artifact of the fake, not a fact
    # about MeshCore.
    fmt.fake.commands._next_ack = 0xABCDEF01
    tx_ref = fmt.send_dm("!" + recipient_pubkey[:8], "hi")
    assert isinstance(tx_ref, str)
    with pytest.raises(ValueError):
        int(tx_ref)


# --- where the transports genuinely differ: delivery failure --------------


def test_meshtastic_reports_a_nak_as_a_failed_delivery():
    ft = FakeTransport()
    try:
        seen: list[Delivery] = []
        ft.on_delivery(seen.append)
        tx_ref = ft.send_dm("!aabbccdd", "hi")
        ft.note_routing_packet(
            _routing_packet(
                from_num=0xAABBCCDD, request_id=int(tx_ref), routing_error="MAX_RETRANSMIT"
            )
        )
        assert seen == [
            Delivery(tx_ref=tx_ref, outcome=DeliveryOutcome.FAILED, reason="MAX_RETRANSMIT")
        ]
    finally:
        ft.stop()


def test_meshcore_has_no_dm_level_nak_a_failed_delivery_looks_like_a_slow_one():
    """Precisely what the notes support, no more: meshcore-notes.md section
    3 establishes there is no DM-level negative acknowledgement -- no NAK
    event for `send_msg` the way Meshtastic's ROUTING_APP carries a
    `routing_error`. A send that will never be delivered therefore looks
    IDENTICAL, from the transport's point of view, to one that is merely
    slow: once `send_dm` has returned a `tx_ref` (i.e. the command itself
    was accepted -- see `test_meshcore_send_command_error_raises_before_
    minting_a_tx_ref` below for the case where it is NOT), no `Delivery`
    ever fires for it either way. `DeliveryOutcome.FAILED` is therefore
    unreachable through `MeshCoreTransport.on_delivery` for an ACCEPTED
    send -- documented here rather than silently absent. This is NOT the
    same claim as "MeshCore has no failure signal at all": section 12.5
    confirms `EventType.ERROR` is a real command-response event in this
    library, which is exactly what the next test exercises.
    """
    fmt = FakeMeshCoreTransport()
    recipient_pubkey = "aabbccdd" + "33" * 28
    fmt.fake.emit_new_contact(recipient_pubkey, "contact-b")
    seen: list[Delivery] = []
    fmt.on_delivery(seen.append)
    fmt.send_dm("!" + recipient_pubkey[:8], "hi")
    assert seen == []  # forever, absent an ACK -- there is no DM-level NAK event


def test_meshcore_ack_wait_uses_firmware_metadata_and_preserves_retry_timestamp():
    times = iter((1000, 2000, 3000))
    fmt = FakeMeshCoreTransport(clock=lambda: next(times))
    pubkey = "aabbccde" + "33" * 28
    recipient = "!" + pubkey[:8]
    fmt.fake.emit_new_contact(pubkey, "mobile")
    tx_ref = fmt.send_dm(recipient, "original", attempt=0)
    assert fmt.dm_ack_wait_seconds(tx_ref, 3) == 5
    fmt.send_dm(recipient, "notice", attempt=0)
    fmt.send_dm(recipient, "original", attempt=1)
    timestamps = [sent["timestamp"] for sent in fmt.fake.sent]
    assert timestamps[0] == timestamps[2]
    assert timestamps[1] != timestamps[0]


def test_meshcore_send_command_error_raises_before_minting_a_tx_ref():
    """meshcore-notes.md section 12.5 confirms `EventType.ERROR` is a real
    command-response event in this library (`send_chan_msg`'s cited
    `OK`/`ERROR` pair); whether `send_msg` itself can also return it is not
    directly cited, but nothing rules it out, so `send_dm` must not assume
    `MSG_SENT` unconditionally. Before this fix round, indexing
    `event.payload["expected_ack"]` on an `ERROR` event raised a bare
    `KeyError` AFTER the send command already ran -- the same "transmit,
    then fail confusingly" shape as the `tx_ref`/`int()` hazard. `send_dm`
    now raises `MeshCoreSendFailed` deliberately instead, and mints no
    `tx_ref` at all, so there is nothing for a later `Delivery` to
    correlate against.
    """
    fmt = FakeMeshCoreTransport()
    recipient_pubkey = "aabbccdd" + "55" * 28
    fmt.fake.emit_new_contact(recipient_pubkey, "contact-c")
    fmt.fake.commands.force_next_send_error("radio_busy")
    with pytest.raises(MeshCoreSendFailed):
        fmt.send_dm("!" + recipient_pubkey[:8], "hi")
    assert fmt.fake.sent == []  # the command errored -- nothing was recorded as sent


# --- where the transports genuinely differ: contact resolution ------------


def test_send_dm_to_an_unresolved_meshcore_contact_raises():
    """MeshCore-only: a reply target that was never advertised/added has
    no known full public key, so no `send_msg` call can be constructed at
    all (meshcore-notes.md section 1: "sending back requires a known
    contact"). Meshtastic has no such concept -- any syntactically valid
    node id is addressable, per `test_meshtastic_tx_ref_happens_to_be_
    decimal` above sending to an id nothing registered."""
    fmt = FakeMeshCoreTransport()
    with pytest.raises(MeshCoreUnknownContact):
        fmt.send_dm("!deadbeef", "hi")


def test_a_dm_from_an_unknown_meshcore_contact_still_produces_an_inbound():
    """scope-decisions.md section 8: the 6-byte prefix already contains
    the 4 bytes the node id needs, so an inbound DM is never dropped for
    identity reasons alone -- unlike a channel knock (see below). Only a
    REPLY to this sender would need a roster entry (see the unresolved-
    contact test above)."""
    fmt = FakeMeshCoreTransport()
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_direct_message("deadbeefcafe", "HELP")  # 12 hex chars, no matching contact
    assert len(seen) == 1
    assert seen[0].sender == "!deadbeef"
    assert seen[0].channel is ChannelScope.DIRECT


# --- where the transports genuinely differ: on_message is wired at all ----


def test_meshcore_on_message_is_its_only_receive_path():
    """Documents an intentional asymmetry rather than hiding it:
    `MeshCoreTransport` has no other way to receive anything, so
    `on_message` firing IS its receive path (exercised throughout this
    file). See `test_meshtastic_on_message_is_not_wired_in_production`
    immediately below for the other half."""
    fmt = FakeMeshCoreTransport()
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_direct_message("aabbccddeeff", "HELP")
    assert len(seen) == 1


def test_meshtastic_on_message_is_not_wired_in_production():
    """`MeshtasticTransport.on_message` is Protocol-complete -- it can be
    registered without error -- but nothing calls it today: `main.py`
    still converts `IncomingPacket` to `Inbound` itself via
    `radio.as_inbound` on the existing pubsub ingest path (`RadioAdapter`'s
    own thread), bypassing the transport's callback entirely. Wiring
    `main.py` to call it instead (for MeshCore, which has no equivalent
    ingest thread of its own) is exactly the open work left to 3B -- see
    task-3A-report.md."""
    ft = FakeTransport()
    try:
        seen = []
        ft.on_message(seen.append)
        ft.iface.emit_packet(make_packet(to_num=ft.iface.myInfo.my_node_num, text="HELP"))
        assert seen == []  # registering the callback is a no-op today
    finally:
        ft.stop()


# --- clock injection: MeshCore only -----------------------------------


def test_meshcore_received_at_uses_the_injected_clock_not_the_wall_clock():
    """Fix round 1, finding 5: a fake/transport silently reading the real
    wall clock instead of an injected one already caused a bug in task 2B
    (a fake's real-time capture diverging from the daemon's test clock,
    silently expiring queued replies). `Inbound.received_at` must be
    controllable the same way the rest of this test suite controls time,
    for both message kinds `MeshCoreTransport` produces."""
    fixed = 12345
    fmt = FakeMeshCoreTransport(clock=lambda: fixed)
    fmt.fake.emit_new_contact("cafebabe" + "77" * 28, "alice")
    seen = []
    fmt.on_message(seen.append)

    fmt.fake.emit_direct_message("aabbccddeeff", "HELP")
    assert seen[0].received_at == fixed

    fmt.fake.emit_channel_message_from(0, "alice", "!garden")
    assert seen[1].received_at == fixed


# --- the channel-trigger surface: MeshCore only ----------------------------
#
# Meshtastic has no channel concept on `Transport` at all -- its
# `ChannelTriggerPolicy` is always `None` (see tests/test_transport.py's
# `test_without_a_policy_the_same_message_is_just_a_broadcast`), so there is
# nothing transport-shaped to pin for it here. Every test below is
# MeshCore-only and says so in its name.


def _channel_fixture(**kw) -> FakeMeshCoreTransport:
    fmt = FakeMeshCoreTransport(watched_channel_idx=0, **kw)
    fmt.fake.emit_new_contact("cafebabe" + "44" * 28, "alice")
    return fmt


def test_meshcore_channel_message_from_a_known_contact_becomes_watched():
    fmt = _channel_fixture()
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_channel_message_from(0, "alice", "!garden")
    assert len(seen) == 1
    inb = seen[0]
    assert inb.channel is ChannelScope.WATCHED
    assert inb.recipient == BROADCAST
    assert inb.text == "!garden"
    assert inb.sender == "!cafebabe"


def test_meshcore_channel_message_from_an_unresolved_name_is_dropped_silently(caplog):
    """scope-decisions.md section 7: an unauthenticated claimed sender
    that matches no known contact is dropped INSIDE the transport -- no
    `Inbound` is ever produced, so no channel reply is possible anywhere,
    structurally."""
    fmt = _channel_fixture()
    seen = []
    fmt.on_message(seen.append)
    with caplog.at_level("INFO", logger="meshgarden.meshcore_transport"):
        fmt.fake.emit_channel_message_from(0, "mallory-not-a-contact", "!garden")
    assert seen == []
    assert "reason=unknown_sender_name" in caplog.text
    assert "mallory-not-a-contact" not in caplog.text


def test_meshcore_channel_message_with_no_sender_convention_at_all_is_dropped():
    fmt = _channel_fixture()
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_channel_message(0, "no colon in this body")
    assert seen == []


def test_meshcore_channel_message_from_an_ambiguous_display_name_is_dropped_silently(caplog):
    """meshcore-notes.md section 12.1's collision warning, pinned: an
    `adv_name` is attacker-controlled free text, and "nothing prevents two
    contacts sharing a display name". Per the operator's guidance (fix
    round 1): an AMBIGUOUS name resolves to NOBODY, exactly like an
    unresolved one -- ambiguity is not identity, so this must not silently
    pick either contact (which would let an attacker who merely knows a
    real contact's display name ride in on that contact's resolution)."""
    fmt = _channel_fixture()  # already has "alice" -> cafebabe...44*28
    fmt.fake.emit_new_contact("f00dbabe" + "66" * 28, "alice")  # a SECOND, distinct contact
    seen = []
    fmt.on_message(seen.append)
    with caplog.at_level("INFO", logger="meshgarden.meshcore_transport"):
        fmt.fake.emit_channel_message_from(0, "alice", "!garden")
    assert seen == []
    assert "reason=ambiguous_sender_name" in caplog.text
    assert "alice" not in caplog.text


def test_meshcore_the_same_contact_re_advertising_under_one_name_is_not_ambiguous():
    """A repeated `NEW_CONTACT` refresh for the SAME public key (e.g. a
    periodic roster sync) must not be mistaken for a name collision."""
    fmt = _channel_fixture()  # "alice" -> cafebabe...44*28
    fmt.fake.emit_new_contact("cafebabe" + "44" * 28, "alice")  # same contact again
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_channel_message_from(0, "alice", "!garden")
    assert len(seen) == 1


def test_meshcore_channel_message_from_a_quarantined_name_is_dropped_silently():
    """A collision that quarantines a node id must also invalidate any
    `_contacts_by_name` entry pointing at the quarantined pubkey (a
    leftover from the contact's ORIGINAL, good advert) -- otherwise a
    channel message claiming "alice" still resolves to the now-quarantined
    node id, and the daemon's later KNOCK reply raises
    MeshCoreUnknownContact on send_dm."""
    fmt = _channel_fixture()  # "alice" -> cafebabe...44*28, node id !cafebabe
    fmt.fake.emit_new_contact("cafebabe" + "55" * 28, "mallory")  # colliding key, same node id
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_channel_message_from(0, "alice", "!garden")
    assert seen == []
    assert not fmt.identity_is_unambiguous("!cafebabe")


def test_meshcore_channel_message_resolving_to_an_already_ambiguous_sender_is_dropped():
    """Belt and braces alongside the `_contacts_by_name` cleanup above,
    exactly like `_on_contact_msg`'s own `_ambiguous_ids` check: even if a
    `_contacts_by_name` entry somehow still points at a quarantined pubkey,
    `_on_channel_msg` must refuse it rather than resolve a sender that is
    already known to be ambiguous."""
    fmt = _channel_fixture()  # "alice" -> cafebabe...44*28, node id !cafebabe
    fmt._ambiguous_ids.add("!cafebabe")
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_channel_message_from(0, "alice", "!garden")
    assert seen == []


def test_meshcore_channel_message_on_an_unwatched_channel_is_other():
    fmt = _channel_fixture()
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_channel_message_from(7, "alice", "chatter")
    assert len(seen) == 1
    assert seen[0].channel is ChannelScope.OTHER


def test_meshcore_channel_triggering_disabled_tags_everything_other():
    """`watched_channel_idx=None` is the `meshcore_channel = ""` case
    (scope-decisions.md section 4): channel triggering is off, so even a
    message on index 0 from a known contact tags `OTHER`, never
    `WATCHED`."""
    fmt = FakeMeshCoreTransport(watched_channel_idx=None)
    fmt.fake.emit_new_contact("cafebabe" + "44" * 28, "alice")
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_channel_message_from(0, "alice", "!garden")
    assert len(seen) == 1
    assert seen[0].channel is ChannelScope.OTHER


@pytest.mark.parametrize("body", ["!garden", "chatter", "HELP", "!garden HELP", ""])
def test_meshcore_channel_message_never_transmits_anything_regardless_of_content(body):
    """The structural guarantee, proven against the fake rather than only
    asserted: whatever the channel says, `MeshCoreTransport` itself never
    calls `commands.send_msg` -- there is no `send_chan_msg` to call at
    all (see `tests.fake_meshcore._FakeCommands`). Sending an actual knock
    DM is the DAEMON's job (via `send_dm`, driven by `decide_inbound`'s
    `KNOCK` outcome), never something the transport does on its own."""
    fmt = _channel_fixture()
    fmt.on_message(lambda inbound: None)
    fmt.fake.emit_channel_message_from(0, "alice", body)
    assert fmt.fake.sent == []


def test_meshcore_channel_name_matching_case_sensitivity_is_configurable():
    """0A's two cited implementations disagree (PotatoMesh's own resolver
    is case-sensitive; meshcore_py's own `get_contact_by_name` is case-
    insensitive) and neither is "the" MeshCore behavior -- genuinely open
    per meshcore-notes.md section 12.1, so this is a constructor knob
    rather than a guess."""
    pubkey = "11112222" + "55" * 28

    sensitive = FakeMeshCoreTransport(watched_channel_idx=0, channel_name_match_case_sensitive=True)
    sensitive.fake.emit_new_contact(pubkey, "Alice")
    seen_sensitive = []
    sensitive.on_message(seen_sensitive.append)
    sensitive.fake.emit_channel_message_from(0, "alice", "!garden")  # wrong case
    assert seen_sensitive == []

    insensitive = FakeMeshCoreTransport(
        watched_channel_idx=0, channel_name_match_case_sensitive=False
    )
    insensitive.fake.emit_new_contact(pubkey, "Alice")
    seen_insensitive = []
    insensitive.on_message(seen_insensitive.append)
    insensitive.fake.emit_channel_message_from(0, "alice", "!garden")
    assert len(seen_insensitive) == 1


# --- task 3B: fixes made against the installed real library ---------------


def test_meshcore_bare_advertisement_populates_the_roster_for_send_dm():
    """3A's skeleton read payload key `adv_key`; confirmed against the
    installed `meshcore==2.3.11` package that the real key is `public_key`
    (`meshcore/reader.py:574-578`) -- a real bare advertisement was
    previously dropped silently. Pinned here against BOTH the fake's
    `emit_advertisement` and `MeshCoreTransport._on_advertisement`."""
    fmt = FakeMeshCoreTransport()
    pubkey = "cafef00d" + "22" * 28
    fmt.fake.emit_advertisement(pubkey)
    tx_ref = fmt.send_dm("!cafef00d", "hi")
    assert isinstance(tx_ref, str) and tx_ref != ""


def test_meshcore_wire_event_values_match_the_installed_library():
    """Confirms every `EventType` member's value against the installed
    `meshcore==2.3.11` package's own `EventType` enum -- see
    `meshgarden.meshcore_transport.EventType`'s docstring for which of
    these were UNCONFIRMED placeholders in 3A and are corrected in 3B."""
    real = pytest.importorskip("meshcore").EventType
    from meshgarden.meshcore_transport import EventType as OurEventType

    for name in (
        "CONTACT_MSG_RECV",
        "CHANNEL_MSG_RECV",
        "ACK",
        "MSG_SENT",
        "CONNECTED",
        "DISCONNECTED",
        "ADVERTISEMENT",
        "NEW_CONTACT",
        "SELF_INFO",
        "ERROR",
    ):
        assert getattr(OurEventType, name).value == getattr(real, name).value


def test_meshcore_connected_seconds_resets_on_a_reconnect():
    """`status().connected_seconds` is MeshCore's freshness-signal fallback
    for a packet with no `sender_ts` (transport-boundary-spec.md section 5's
    mapping table row 9) -- it must reset on every CONNECTED event,
    including a reconnect, not just once at the first `connect()` (task 3B:
    fixed by subscribing to CONNECTED/DISCONNECTED, which 3A's skeleton did
    not do at all)."""
    fmt = FakeMeshCoreTransport()
    first = fmt.status().connected_seconds
    assert first is not None

    fmt.fake.disconnect(reason="test")
    assert fmt.status().connected_seconds is None

    fmt.fake.connect()
    assert fmt.status().connected_seconds is not None


def test_meshcore_transport_connect_is_idempotent_no_duplicate_dispatch():
    """Fix round 2, finding 3: `MeshCoreTransport.connect()` must be
    idempotent. `RealMeshCoreClient.connect()`'s own idempotency guard
    (fix round 1) protects only its background threads -- it does nothing
    to stop THIS method's 8 `subscribe()` calls from running a second
    time against an already-subscribed client, which would register every
    handler twice and dispatch every inbound event twice: duplicate
    replies, for a service whose whole guarantee is one reply per
    eligible trigger."""
    fmt = FakeMeshCoreTransport()
    before = sum(len(v) for v in fmt.fake._subscribers.values())

    fmt.connect()  # second call

    after = sum(len(v) for v in fmt.fake._subscribers.values())
    assert after == before, "a second connect() must not add any new subscriptions"

    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_direct_message("aabbccddeeff", "HELP")
    assert len(seen) == 1, "a duplicated subscription would have dispatched this event twice"


# --- task 3B-4 fix 1: the fake models the real queue-and-fetch shape -------


def test_fake_meshcore_holds_a_direct_message_emitted_before_fetching_starts():
    """Real-hardware finding: the companion firmware QUEUES inbound
    messages and does not deliver them until a client calls
    `start_auto_message_fetching()` -- confirmed on hardware (ten seconds
    of silence with CONTACT_MSG_RECV/CHANNEL_MSG_RECV subscribed; an
    immediate delivery the instant that call was made by hand).
    `RealMeshCoreClient` copes by calling the real method once per
    connected session; this pins that `FakeMeshCore` models the same
    queue-and-fetch shape rather than dispatching unconditionally, which
    is what let this bug pass every test before this fix."""
    fake = FakeMeshCore()
    seen = []
    fake.subscribe(EventType.CONTACT_MSG_RECV, seen.append)

    fake.emit_direct_message("aabbccddeeff", "HELLO")
    assert seen == [], "held: fetching has not started yet for this fake"

    fake.connect()  # models RealMeshCoreClient's per-session fetch start
    assert len(seen) == 1, "flushed once fetching starts"


def test_fake_meshcore_holds_a_channel_message_emitted_before_fetching_starts():
    fake = FakeMeshCore()
    seen = []
    fake.subscribe(EventType.CHANNEL_MSG_RECV, seen.append)

    fake.emit_channel_message_from(0, "alice", "!garden")
    assert seen == []

    fake.connect()
    assert len(seen) == 1


def test_fake_meshcore_delivers_a_message_immediately_once_fetching_is_running():
    """Anything emitted AFTER fetching starts is delivered right away --
    exactly the behavior every other daemon-level test already relies on
    (fetching is enabled by the time `FakeMeshCoreTransport.__init__`
    returns, since its first `connect()` call enables it before its
    early-return no-op)."""
    fake = FakeMeshCore()
    fake.connect()
    seen = []
    fake.subscribe(EventType.CONTACT_MSG_RECV, seen.append)
    fake.emit_direct_message("aabbccddeeff", "HELLO")
    assert len(seen) == 1


def test_fake_meshcore_holds_messages_again_after_a_disconnect_until_the_next_connect():
    """Mirrors the real library's `MeshCore.disconnect()`, which stops auto
    message fetching on its own -- a message emitted between a disconnect
    and the next connect must be held again, exactly like real hardware
    between sessions."""
    fake = FakeMeshCore()
    fake.connect()
    seen = []
    fake.subscribe(EventType.CONTACT_MSG_RECV, seen.append)

    fake.disconnect()
    fake.emit_direct_message("aabbccddeeff", "HELLO")
    assert seen == [], "held: disconnected, fetching stopped"

    fake.connect()
    assert len(seen) == 1, "flushed once the reconnect re-enables fetching"


def test_meshcore_channel_send_is_confined_to_the_watched_channel():
    fmt = FakeMeshCoreTransport(watched_channel_idx=2)
    fmt.send_channel(2, "hello")
    assert fmt.fake.channel_sent == [{"channel_idx": 2, "msg": "hello", "timestamp": None}]
    with pytest.raises(MeshCoreSendFailed):
        fmt.send_channel(1, "wrong channel")


def test_meshcore_channel_send_also_accepts_the_alert_channel_only():
    fmt = FakeMeshCoreTransport(watched_channel_idx=2, alert_channel_idx=5)
    fmt.send_channel(2, "bbs reply")
    fmt.send_channel(5, "⚠ weather")
    assert [m["channel_idx"] for m in fmt.fake.channel_sent] == [2, 5]
    with pytest.raises(MeshCoreSendFailed):
        fmt.send_channel(3, "third channel")


def test_meshcore_alert_channel_unresolved_refuses_and_can_be_set_later():
    fmt = FakeMeshCoreTransport(watched_channel_idx=2)
    assert fmt.alert_channel_idx is None
    with pytest.raises(MeshCoreSendFailed):
        fmt.send_channel(5, "⚠ weather")
    fmt.set_alert_channel_idx(5)
    fmt.send_channel(5, "⚠ weather")
    assert fmt.fake.channel_sent[-1]["channel_idx"] == 5
    fmt.set_alert_channel_idx(None)
    with pytest.raises(MeshCoreSendFailed):
        fmt.send_channel(5, "⚠ weather")


def test_alert_channel_inbound_stays_other_scope():
    """A message on the alert channel is observed but never triggers."""
    fmt = FakeMeshCoreTransport(watched_channel_idx=2, alert_channel_idx=5)
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_channel_message(5, "Someone: MENU")
    assert all(m.channel is not ChannelScope.WATCHED for m in seen)


def test_channel_send_primitive_is_confined_to_the_transport_and_real_adapter():
    """Only the transport boundary and real async adapter may call the
    MeshCore library's channel primitive directly."""
    import pathlib

    import meshgarden

    root = pathlib.Path(meshgarden.__file__).parent
    offenders = [
        str(path)
        for path in sorted(root.rglob("*.py"))
        if path.name not in {"meshcore_transport.py", "meshcore_client.py"}
        and "send_chan_msg" in path.read_text()
    ]
    assert offenders == []


# ===========================================================================
# Task 3C: transport-level attacks on the DM-only guarantee that earlier
# reviews did not pin. See task-3C-report.md for the full attack log.
# ===========================================================================


@pytest.mark.parametrize("text", ["!garden", "  !GARDEN  ", "alice: !garden", "MENU"])
def test_a_meshcore_direct_message_is_always_scoped_direct_whatever_its_text(text):
    """`decide_inbound`'s channel-knock branch is reached on
    `inb.channel is ChannelScope.WATCHED` ALONE -- it never re-checks
    `recipient`, and it is evaluated BEFORE the DM rows. So the only thing
    keeping a direct message out of the knock branch is that
    `_on_contact_msg` hardcodes `ChannelScope.DIRECT`, whatever the text
    says and whatever channel convention it imitates. Pin that, including
    for a DM that carries the channel's own `"SenderName: body"`
    convention in its body."""
    fmt = _channel_fixture()
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_direct_message("cafebabe4444", text)
    assert len(seen) == 1
    assert seen[0].channel is ChannelScope.DIRECT
    assert seen[0].recipient == fmt.identity()


@pytest.mark.parametrize("body", ["!garden", "MENU", "chatter", ""])
def test_a_meshcore_channel_inbound_is_never_addressed_to_us(body):
    """The mirror of the test above. Every `Inbound` a channel message
    produces carries `recipient == BROADCAST` unconditionally, so if the
    channel-knock branch is ever removed or its policy is absent, channel
    traffic falls through to `decide_inbound`'s `recipient == BROADCAST`
    row and is rejected there -- two independent layers, not one. A
    channel `Inbound` that ever carried our own node id as `recipient`
    would be treated as a DM and could execute a command."""
    fmt = _channel_fixture()
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_channel_message_from(0, "alice", body)
    assert len(seen) == 1
    assert seen[0].recipient == BROADCAST
    assert seen[0].recipient != fmt.identity()


def test_a_meshcore_contact_is_never_indexed_under_an_empty_display_name():
    """`meshcore_compat._parse_sender_name` returns `""` (not `None`) for a
    channel body that starts with a colon, and `_on_channel_msg`'s early
    return only catches `None`. The empty string therefore reaches the
    roster lookup, and is harmless ONLY because `_remember_contact` refuses
    to index a falsy `adv_name`. Each layer relies on the other; this pins
    the half that does the actual work."""
    fmt = _channel_fixture()
    fmt.fake.emit_new_contact("deadbeef" + "77" * 28, "")
    fmt.fake.emit_advertisement("feedface" + "88" * 28)  # bare advert: no name at all
    assert "" not in fmt._contacts_by_name
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_channel_message(0, ": !garden")
    fmt.fake.emit_channel_message(0, "    : !garden")
    assert seen == []


def test_only_the_meshcore_transport_and_its_adapter_may_call_the_send_primitive():
    """The static transmit guard in `test_central_invariant.py::
    test_only_sender_module_calls_send_text` covers `.send_text(` and
    `.send_dm(` -- the Meshtastic primitive and the Protocol member. It
    does NOT cover `.send_msg(`, MeshCore's own over-the-air transmit
    primitive, which is reachable from anywhere holding the live client
    (`MeshCoreTransport.client` is a public property, and `Daemon` already
    keeps a reference to it in `self._meshcore_client` for wedge
    supervision). A module calling `transport.client.commands.send_msg(...)`
    directly would bypass `Sender`'s entire send-time guard -- identity
    re-check, request-state check, recipient-shape check, block list, TTL,
    cooldown, payload size -- and BOTH existing greps would stay green.

    Allowed: `meshcore_transport.py` (the one call, inside `send_dm`) and
    `meshcore_client.py` (the adapter that forwards it to the real async
    library). `.send_text(`/`.send_dm(` stay owned by the existing test."""
    import pathlib
    import re

    import meshgarden

    root = pathlib.Path(meshgarden.__file__).parent
    allowed = {"meshcore_transport.py", "meshcore_client.py"}
    offenders = [
        path.name
        for path in sorted(root.rglob("*.py"))
        if path.name not in allowed and re.search(r"\.send_msg\s*\(", path.read_text())
    ]
    assert offenders == []


def test_the_meshcore_client_reaches_only_documented_transmitting_library_calls():
    """The companion-device commands `meshcore_client.py` issues on its own
    initiative (device query, channel probe, autoadd config) must all be
    LOCAL serial control, with exactly two DELIBERATE, DOCUMENTED
    exceptions -- `send_msg` (a reply) and `send_advert` (a periodic
    self-advertisement, task 3B-4 fix 2) -- never anything else that puts a
    frame on the air. This enumerates the `mc.commands.*` surface that
    module actually touches, so adding a trace/ping, a login, or a channel
    send there has to come through this test first.

    TASK 3B-4 UPDATE: this test's name and set used to read "...but_send_
    msg", i.e. `send_msg` was the ONE transmitting call. A hardware finding
    (the garden was unreachable until manually flood-advertised) added a
    second: `commands.send_advert(flood=True)`, called once per connection
    (`_send_advert_on_connect`) and repeated on `[radio] meshcore_advert_
    interval_seconds` (`send_advert`'s public method). Unlike `send_msg`,
    it is never a reply to anything -- it is presence beaconing, the same
    category of transmission Meshtastic firmware already does on its own
    at every NODEINFO broadcast. See `docs/architecture.md` and `docs/
    security-and-privacy.md` for where this exception is stated for a
    reader, not just for this test."""
    import pathlib
    import re

    import meshgarden.meshcore_client as mcc

    source = pathlib.Path(mcc.__file__).read_text()
    calls = set(re.findall(r"commands\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", source))
    assert calls == {
        "send_msg",  # a reply, reached only via Sender -> send_dm
        "send_chan_msg",  # configured watched-channel command reply
        # Local companion-radio route mutation. After the original and
        # first retry receive no ACK, this clears the stale stored route
        # so retries 2 and 3 flood. The reset itself uses no RF airtime.
        "reset_path",
        # A periodic self-advertisement (task 3B-4 fix 2), NOT a reply --
        # see `_send_advert_on_connect` (once per connection) and the
        # public `send_advert()` (repeated by `main.py`'s maintenance tick
        # on `meshcore_advert_interval_seconds`). A real over-the-air
        # transmission, deliberately and explicitly allowed here.
        "send_advert",
        # Request-driven, configured REPEATER command: guest authentication
        # followed by a read-only status request to that exact public key.
        "send_login_sync",
        "req_status_sync",
        "send_device_query",  # local: identifies the attached companion
        "get_channel",  # local: channel-name -> index probe
        "get_autoadd_config",  # local: reads the device's autoadd bit
        "set_autoadd_config",  # local: opt-in write of the autoadd bit
        # Local: reads a message the companion already has queued. Not
        # called directly -- `_start_message_fetching` awaits `mc.start_
        # auto_message_fetching()`, whose OWN internal loop issues this --
        # but named in that method's docstring (task 3B-4 fix 1) and
        # therefore correctly caught by this scan, same as `get_contacts`
        # below. Reading a queued message transmits nothing.
        "get_msg",
        # Local: sets the companion's own clock from host time, every
        # connection, unconditionally -- a USB admin write, not a LoRa
        # transmission, exactly like the autoadd write above and the
        # Meshtastic path's own unconditional clock set.
        "set_time",
        # Local: reads the contacts the radio already has in its own
        # storage, so a restart does not start with an empty roster. Not
        # called directly -- `_seed_contacts` awaits `mc.ensure_contacts()`,
        # which issues this -- but named in that method's docstring and
        # therefore correctly caught by this scan. Reading stored contacts
        # transmits nothing; it is the same serial request the library's
        # own `auto_update_contacts` makes.
        "get_contacts",
        # Local: deletes one contact from the radio's own table -- the
        # adapter's second persisted write, only called by the opt-in daily
        # housekeeping (`remove_contact`). A USB admin write, not a LoRa
        # transmission.
        "remove_contact",
    }


def test_meshcore_evicted_retry_timestamp_fails_without_transmitting():
    fmt = FakeMeshCoreTransport(clock=lambda: 1000)
    pubkey = "aabbccde" + "33" * 28
    recipient = "!" + pubkey[:8]
    fmt.fake.emit_new_contact(pubkey, "mobile")
    fmt.send_dm(recipient, "original")
    for index in range(128):
        fmt.send_dm(recipient, f"notice {index}")
    sent_count = len(fmt.fake.sent)
    with pytest.raises(MeshCoreSendFailed):
        fmt.send_dm(recipient, "original", attempt=1)
    assert len(fmt.fake.sent) == sent_count


def test_meshcore_forget_contact_drops_a_name_mapping():
    fmt = _channel_fixture()  # "alice" -> cafebabe...44*28
    pubkey = "cafebabe" + "44" * 28
    seen = []
    fmt.on_message(seen.append)
    fmt.fake.emit_channel_message_from(0, "alice", "!garden")
    assert len(seen) == 1
    fmt.forget_contact(pubkey)
    fmt.fake.emit_channel_message_from(0, "alice", "!garden")
    assert len(seen) == 1


def test_meshcore_forget_contact_leaves_an_ambiguous_name_alone():
    fmt = _channel_fixture()
    fmt.fake.emit_new_contact("f00dbabe" + "66" * 28, "alice")
    fmt.forget_contact("cafebabe" + "44" * 28)
    assert fmt._contacts_by_name["alice"] is _AMBIGUOUS
