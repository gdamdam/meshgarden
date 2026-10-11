"""`MeshtasticTransport`'s own tests: the Protocol implementation wired to a
real `RadioAdapter` + `FakeInterface`, and the ROUTING_APP ack correlation
moved here from `sender.py` (transport-boundary-spec.md section 4).
"""

from __future__ import annotations

import time

import pytest

from meshgarden.config import RadioConfig, ServiceConfig
from meshgarden.meshtastic_transport import MeshtasticTransport
from meshgarden.models import IncomingPacket
from meshgarden.radio import RadioAdapter
from meshgarden.transport import BROADCAST, Delivery, DeliveryOutcome
from tests.fakes import FakeInterface

NODE_NUM = 0x11223344
NODE_ID = "!11223344"


def _radio(node_num=NODE_NUM, expected=NODE_ID):
    iface = FakeInterface(node_num)
    radio = RadioAdapter(
        RadioConfig(device="/dev/fake", expected_node_id=expected),
        sink=lambda p: None,
        interface_factory=lambda dev: iface,
    )
    return radio, iface


@pytest.fixture
def transport():
    radio, iface = _radio()
    radio.start()
    radio.wait_ready(timeout=2)
    t = MeshtasticTransport(radio, service_cfg=ServiceConfig(), expected_node_id=NODE_ID)
    yield t, iface
    radio.stop()


def test_max_text_bytes_reflects_the_configured_service_budget():
    radio, _ = _radio()
    t = MeshtasticTransport(
        radio, service_cfg=ServiceConfig(max_response_bytes=120), expected_node_id=NODE_ID
    )
    assert t.max_text_bytes == 120
    radio.stop()


def test_identity_is_unresolved_before_the_radio_connects():
    radio, _ = _radio()
    t = MeshtasticTransport(radio, service_cfg=ServiceConfig(), expected_node_id=NODE_ID)
    identity = t.identity()
    assert identity != NODE_ID
    assert identity != BROADCAST
    radio.stop()


def test_identity_matches_the_connected_radios_node_id(transport):
    t, _ = transport
    assert t.identity() == NODE_ID


def test_is_broadcast(transport):
    t, _ = transport
    assert t.is_broadcast(BROADCAST) is True
    assert t.is_broadcast("!aabbccdd") is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!AABBCCDD", "!aabbccdd"),
        ("  !aabbccdd  ", "!aabbccdd"),
        ("ccdd", None),  # a bare tail is not a full id -- collector's job
        ("fndr", None),  # a short name is not a full id either
        ("", None),
    ],
)
def test_parse_node_argument_only_resolves_a_full_id(transport, text, expected):
    t, _ = transport
    assert t.parse_node_argument(text) == expected


def test_display_and_short_label(transport):
    t, _ = transport
    assert t.display_label("!aabbccdd") == "!aabbccdd"
    assert t.short_label("!aabbccdd") == "!ccdd"


def test_send_dm_returns_the_tx_id_as_a_string(transport):
    t, iface = transport
    tx_ref = t.send_dm("!aabbccdd", "hi")
    assert isinstance(tx_ref, str)
    assert tx_ref != ""
    assert int(tx_ref) > 0
    assert iface.sent[0]["destinationId"] == "!aabbccdd"
    assert iface.sent[0]["text"] == "hi"


def test_send_dm_returns_empty_string_when_the_radio_gives_no_tx_id(transport):
    t, iface = transport
    iface.sendText = lambda *, text, destinationId, **kw: None
    assert t.send_dm("!aabbccdd", "hi") == ""


def test_status_reports_connected_and_reply_ready(transport):
    t, _ = transport
    st = t.status()
    assert st.connected is True
    assert st.reply_ready is True
    assert st.actual_node_id == NODE_ID
    assert st.connected_seconds is not None
    assert st.connected_seconds >= 0


def test_status_reports_no_connected_seconds_before_connecting():
    radio, _ = _radio()
    t = MeshtasticTransport(radio, service_cfg=ServiceConfig(), expected_node_id=NODE_ID)
    st = t.status()
    assert st.connected is False
    assert st.connected_seconds is None
    radio.stop()


def _routing_packet(*, from_num, request_id, routing_error):
    return IncomingPacket(
        received_at=int(time.time()),
        received_monotonic=0.0,
        from_num=from_num,
        to_num=NODE_NUM,
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


def test_a_remote_ack_is_reported_as_a_delivery(transport):
    t, _ = transport
    seen = []
    t.on_delivery(seen.append)
    t.note_routing_packet(_routing_packet(from_num=0xAABBCCDD, request_id=42, routing_error="NONE"))
    assert seen == [Delivery(tx_ref="42", outcome=DeliveryOutcome.ACKED, reason="NONE")]


def test_a_remote_nak_is_reported_as_failed_with_a_bounded_reason(transport):
    t, _ = transport
    seen = []
    t.on_delivery(seen.append)
    t.note_routing_packet(
        _routing_packet(from_num=0xAABBCCDD, request_id=42, routing_error="MAX_RETRANSMIT")
    )
    assert seen == [Delivery(tx_ref="42", outcome=DeliveryOutcome.FAILED, reason="MAX_RETRANSMIT")]


def test_an_unknown_routing_error_collapses_to_other(transport):
    t, _ = transport
    seen = []
    t.on_delivery(seen.append)
    t.note_routing_packet(
        _routing_packet(from_num=0xAABBCCDD, request_id=42, routing_error="SOME_FUTURE_ERROR")
    )
    assert seen[0].reason == "other"


def test_the_implicit_self_ack_is_never_reported_as_a_delivery(transport):
    t, _ = transport
    seen = []
    t.on_delivery(seen.append)
    t.note_routing_packet(_routing_packet(from_num=NODE_NUM, request_id=42, routing_error="NONE"))
    assert seen == []


def test_a_self_originated_failure_is_still_reported(transport):
    """Only a NONE-error packet from our own node is the implicit self-ack
    -- a genuine routing failure attributed to us is still a real outcome."""
    t, _ = transport
    seen = []
    t.on_delivery(seen.append)
    t.note_routing_packet(
        _routing_packet(from_num=NODE_NUM, request_id=42, routing_error="MAX_RETRANSMIT")
    )
    assert seen == [Delivery(tx_ref="42", outcome=DeliveryOutcome.FAILED, reason="MAX_RETRANSMIT")]


def test_a_non_routing_packet_is_ignored(transport):
    t, _ = transport
    seen = []
    t.on_delivery(seen.append)
    non_routing = _routing_packet(from_num=0xAABBCCDD, request_id=42, routing_error="NONE")
    from dataclasses import replace

    t.note_routing_packet(replace(non_routing, portnum="TEXT_MESSAGE_APP"))
    assert seen == []


def test_a_routing_packet_with_no_request_id_is_ignored(transport):
    t, _ = transport
    seen = []
    t.on_delivery(seen.append)
    t.note_routing_packet(
        _routing_packet(from_num=0xAABBCCDD, request_id=None, routing_error="NONE")
    )
    assert seen == []


def test_note_routing_packet_is_a_no_op_with_no_registered_callback(transport):
    t, _ = transport
    # must not raise even though on_delivery was never called
    t.note_routing_packet(_routing_packet(from_num=0xAABBCCDD, request_id=42, routing_error="NONE"))
