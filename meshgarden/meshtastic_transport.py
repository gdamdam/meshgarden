"""`MeshtasticTransport`: 2A's `Transport` Protocol, implemented over the
existing `RadioAdapter`.

This is the wiring layer, not a redesign: `RadioAdapter` keeps owning the
serial connection, the reconnect thread, and identity verification exactly
as it does today, and this module wraps an already-constructed
`RadioAdapter` instance rather than replacing it. What moves here is the
Meshtastic-specific ACK correlation that used to live in `sender.py`'s
`note_routing_packet` (see its docstring below for why it belongs behind
the boundary, not above it) and the small set of pure translations
(`identity`, `parse_node_argument`, `status`) that let `main.py` and
`sender.py` stop reaching into `RadioAdapter`/`config` helpers directly for
these purposes.

This transport does not subscribe or reply on channels. Its `send_channel`
implementation raises `SendNotReady`; the method exists only to satisfy the
shared transport contract used by MeshCore.

Import discipline: this module never imports the `meshtastic` package
directly (only `RadioAdapter`, in `radio.py`, does) -- see
`tests/test_radio.py::test_only_radio_py_imports_meshtastic`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from meshgarden.config import (
    ServiceConfig,
    node_id_from_num,
    parse_node_id_argument,
)
from meshgarden.models import IncomingPacket
from meshgarden.radio import RadioAdapter, SendNotReady
from meshgarden.transport import (
    BROADCAST,
    Delivery,
    DeliveryOutcome,
    Inbound,
    NodeId,
    TransportStatus,
)

log = logging.getLogger(__name__)

# The full mesh_pb2.Routing.Error enum name set (moved here from sender.py,
# verbatim, as of the plan this was recorded against). A bounded allowlist:
# anything outside it (a future firmware enum value, or malformed input)
# collapses to "other" rather than being reported through verbatim, since
# `processed_requests.reason` must never carry unbounded free text.
_KNOWN_ROUTING_ERRORS = frozenset(
    (
        "NONE",
        "NO_ROUTE",
        "GOT_NAK",
        "TIMEOUT",
        "NO_INTERFACE",
        "MAX_RETRANSMIT",
        "NO_CHANNEL",
        "TOO_LARGE",
        "NO_RESPONSE",
        "DUTY_CYCLE_LIMIT",
        "BAD_REQUEST",
        "NOT_AUTHORIZED",
        "PKI_FAILED",
        "PKI_UNKNOWN_PUBKEY",
        "ADMIN_BAD_SESSION_KEY",
        "ADMIN_PUBLIC_KEY_UNAUTHORIZED",
        "RATE_LIMIT_EXCEEDED",
        "PKI_SEND_FAIL_PUBLIC_KEY",
    )
)

# Sentinel `identity()` reports before the radio's own node number is known
# (no `myInfo` yet). Shaped so it can never equal a real node id
# (`config.NODE_ID_RE` requires "!" + 8 hex chars) or the `BROADCAST`
# sentinel -- unlike reusing "!ffffffff" (what `node_id_from_num(-1)` would
# render), which is a real, if never-issued, node id shape and could in
# principle collide with a forged or malformed packet's `from`/`to` field.
_UNRESOLVED_IDENTITY: NodeId = "^unresolved^"


def _bounded_routing_error(value: str | None) -> str | None:
    """Maps a firmware-supplied routing error name to the known
    mesh_pb2.Routing.Error name set, or "other" for anything else."""
    if value is None:
        return None
    return value if value in _KNOWN_ROUTING_ERRORS else "other"


class MeshtasticTransport:
    """Wraps an existing `RadioAdapter`. Constructed once the adapter (and
    its `expected_node_id`) are known; never constructs its own
    `RadioAdapter` -- the daemon's radio lifecycle (start/stop/reconnect) is
    unchanged by this class's existence.
    """

    def __init__(
        self,
        radio: RadioAdapter,
        *,
        service_cfg: ServiceConfig,
        expected_node_id: str,
    ) -> None:
        self._radio = radio
        self._service_cfg = service_cfg
        self._expected_node_id = expected_node_id
        self._message_callback: Callable[[Inbound], None] | None = None
        self._delivery_callback: Callable[[Delivery], None] | None = None

    @property
    def radio(self) -> RadioAdapter:
        """Escape hatch for the daemon's remaining direct `RadioAdapter`
        needs (start/stop/wait_ready/backoff_max/my_node_num) -- the
        observation/collector path stays Meshtastic-shaped in this epic
        (transport-boundary-spec.md section 6), so main.py still reaches
        the adapter directly for those, not through the Protocol."""
        return self._radio

    # -- Transport Protocol --------------------------------------------

    @property
    def max_text_bytes(self) -> int:
        return self._service_cfg.max_response_bytes

    def connect(self) -> None:
        self._radio.start()

    def identity(self) -> NodeId:
        num = self._radio.my_node_num
        return node_id_from_num(num) if num is not None else _UNRESOLVED_IDENTITY

    def is_broadcast(self, addr: NodeId) -> bool:
        return addr == BROADCAST

    def on_message(self, callback: Callable[[Inbound], None]) -> None:
        self._message_callback = callback

    def send_dm(self, to: NodeId, text: str, *, attempt: int = 0) -> str:
        """Transmits via `RadioAdapter.send_text`. `SendNotReady` (raised
        when connected/reply_ready/interface/identity cannot all be
        verified together) propagates unchanged -- `sender.py` catches it
        by name, imported from `meshgarden.radio`, exactly as it always
        has. Returns `""` (never `None`) when the radio accepted the call
        but produced no packet id, so `tx_ref` stays a plain `str` as the
        Protocol requires; `Sender` treats an empty ref as "no tx id"."""
        tx_id = self._radio.send_text(text=text, destination_id=to)
        return "" if tx_id is None else str(tx_id)

    def reset_dm_path(self, to: NodeId) -> bool:
        """Meshtastic owns routing below this transport boundary."""
        return False

    def send_channel(self, channel_idx: int, text: str) -> None:
        raise SendNotReady("Meshtastic channel replies are unsupported")

    def on_delivery(self, callback: Callable[[Delivery], None]) -> None:
        self._delivery_callback = callback

    def status(self) -> TransportStatus:
        st = self._radio.status()
        connected_seconds = None
        if st.connected and st.connected_since_monotonic is not None:
            connected_seconds = time.monotonic() - st.connected_since_monotonic
        return TransportStatus(
            connected=st.connected,
            reply_ready=st.reply_ready,
            actual_node_id=st.actual_node_id,
            connected_seconds=connected_seconds,
        )

    def display_label(self, node_id: NodeId) -> str:
        return node_id

    def short_label(self, node_id: NodeId) -> str:
        return "!" + node_id.removeprefix("!")[-4:]

    def parse_node_argument(self, text: str) -> NodeId | None:
        return parse_node_id_argument(text)

    # -- Meshtastic-specific: not part of the abstract Transport Protocol --
    # The observation path stays Meshtastic-shaped in this epic (2C decides
    # what a MeshCore advert becomes), so `main.py` calls this directly for
    # every ROUTING_APP packet it already sees on its existing ingest path,
    # rather than this class re-subscribing to the radio's packet stream.

    def note_routing_packet(self, obs: IncomingPacket) -> None:
        """Matches a ROUTING_APP packet to the tx_ref of an earlier
        `send_dm` and, for a genuine remote result, reports it via the
        registered `on_delivery` callback. Moved here verbatim from
        `sender.py`'s former `note_routing_packet` (2A section 4): the
        implicit-self-ack rule and the routing-error vocabulary are
        Meshtastic firmware semantics with no MeshCore counterpart, so they
        belong behind the boundary, not in the transport-agnostic
        `sender.py`.

        A ROUTING_APP packet whose `request_id` equals our tx packet id and
        whose `routing_error` is absent/NONE, from a node OTHER than
        ourselves, is a genuine remote ack. The same packet from OUR OWN
        node with routing_error absent/NONE is the firmware's implicit
        self-ack -- "I transmitted it", not evidence the far end received
        anything -- so no `Delivery` is reported at all (the request stays
        exactly as it was, so a genuine remote ack can still land later,
        whichever order the two arrive in). Any non-NONE routing_error,
        from any origin, is a failure.

        Whether this ROUTING_APP packet's `request_id` actually matches an
        outstanding request is `Sender.on_delivery`'s question, not this
        method's -- this method only ever decides what the raw frame
        MEANS, never whether we're still waiting on it.
        """
        if obs.portnum != "ROUTING_APP" or obs.request_id is None:
            return
        from_id = node_id_from_num(obs.from_num)
        is_none_error = obs.routing_error in (None, "NONE")
        if is_none_error and from_id == self._expected_node_id:
            log.debug(
                "implicit self-ack for tx_ref=%s; not reported as a delivery",
                obs.request_id,
            )
            return
        if self._delivery_callback is None:
            return
        outcome = DeliveryOutcome.ACKED if is_none_error else DeliveryOutcome.FAILED
        self._delivery_callback(
            Delivery(
                tx_ref=str(obs.request_id),
                outcome=outcome,
                reason=_bounded_routing_error(obs.routing_error),
            )
        )
