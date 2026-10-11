"""The transport boundary: what a mesh backend must provide, and nothing more.

This module is a contract, not an implementation. It names the small set of
operations the daemon performs against a radio (`Transport`) and the shape of
an inbound message once every Meshtastic-specific field has been normalized
away (`Inbound`). `meshgarden/radio.py` (Meshtastic) and a later MeshCore
backend both satisfy it; neither is imported here, and this module imports
nothing from the rest of the package, so it can never acquire a dependency on
a particular radio library.

MeshCore may reply on one explicitly configured channel. Channel messages carry
no sender key, only an unverified `"SenderName: body"` convention, so the
transport resolves that name to one unique known contact before emitting an
Inbound. The daemon uses that identity for bounds and moderation, while keeping
MAIL/INBOX/REPLY DM-only. Meshtastic refuses channel sends.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

# An opaque node handle. Every transport formats its own ids; the daemon only
# ever compares them for equality and hands them back to the transport for
# display or for addressing a DM. Both current transports happen to render
# "!xxxxxxxx" (`config.NODE_ID_RE`), but no caller may assume that.
NodeId = str

# The recipient of a message that was addressed to everyone rather than to a
# node. Deliberately NOT "!ffffffff" (Meshtastic's broadcast node number run
# through `node_id_from_num`): a MeshCore node id is the truncated hash of a
# public key and could, however improbably, render as "!ffffffff". A sentinel
# that cannot match `NODE_ID_RE` can never be confused with a real node.
BROADCAST: Final[NodeId] = "^all"


class ChannelScope(StrEnum):
    """Which delivery envelope an `Inbound` arrived in.

    `Inbound.recipient` alone cannot answer this: a message on the channel we
    watch and a message on a channel we do not both have `recipient` of
    `BROADCAST`. The value is RESOLVED BY THE TRANSPORT, which is the only
    component that knows which channel it was configured to watch -- the
    eligibility rule never sees a raw channel index and therefore cannot
    regress into treating an index as proof of anything (the property
    `commands.check_eligibility` has always had and must keep).

    A transport with no channel concept at all, or with channel triggering
    disabled, emits only `DIRECT` and `OTHER`.
    """

    DIRECT = "direct"  # addressed to a node: a DM, ours or someone else's
    WATCHED = "watched"  # the one channel this transport was told to watch
    OTHER = "other"  # any other channel, or a plain broadcast


@dataclass(frozen=True)
class Signal:
    """Reception quality, where the transport can measure it.

    Both fields are optional because MeshCore carries SNR only on the V3
    message events and never carries RSSI on a message event at all.
    """

    snr: float | None = None
    rssi: int | None = None


@dataclass(frozen=True)
class Inbound:
    """One received message, normalized. The trigger/reply path only.

    This is NOT a replacement for `models.IncomingPacket`: the observation
    path (NODEINFO/POSITION/TELEMETRY/ROUTING fields, the collector, the
    dashboard) keeps that record. `Inbound` carries exactly what deciding
    "may this ever produce a reply, and to whom" needs, plus the few signal
    fields a reply body quotes back.

    Contracts a transport must honour when constructing one:

    - `sender` is already RESOLVED. A message whose sender cannot be resolved
      to an addressable node never becomes an `Inbound` at all -- it is
      dropped inside the transport. That is what keeps "we can always DM the
      sender of an eligible trigger" true by construction. Task 3C finding 3:
      this was previously enforced only by every transport's own discipline
      (a MeshCore public key too short to derive a node id from -- a
      truncated advert frame -- could still reach `Inbound(sender=None,
      ...)`, silently poisoning the per-recipient knock cooldown, which
      SQLite lets repeat under a NULL key). `__post_init__` below makes an
      unresolved sender unrepresentable rather than merely unlikely: a
      transport must drop the event BEFORE constructing this object, because
      constructing it with `sender` falsy now raises.
    - `text` is the MESSAGE BODY ONLY. MeshCore's `"SenderName: body"` channel
      convention is stripped by the transport, so no policy above this layer
      has to know that the convention exists.
    - `rx_time` is a RECEIVER-side clock reading or `None`. A sender-supplied
      timestamp (MeshCore's `sender_timestamp`) is NOT a receive time and must
      be passed as `None`; see the freshness rule in the spec.
    - `sender_ts` carries that sender-supplied timestamp instead (or `None`
      when the transport has none), for the freshness gate ONLY: it is the
      SENDER's own clock, never verified, so it must never be used for
      anything but "is this old enough to reject" -- it can only make a
      stale packet look fresh, never the reverse, and the existing
      duplicate-request dedup bounds that failure mode.
    - `raw_id` is an integer so that it keeps fitting the existing
      `processed_requests.packet_id` column. Meshtastic supplies the packet
      id; MeshCore has no packet id and must derive a stable 53-bit message
      id from the message's own content.
    """

    received_at: int  # our UTC epoch seconds at reception
    sender: NodeId
    recipient: NodeId  # a node id, or BROADCAST
    is_text: bool
    text: str | None  # transient only; NEVER persisted or logged
    raw_id: int | None
    rx_time: int | None = None
    sender_ts: int | None = None  # sender's own clock; freshness fallback only
    channel: ChannelScope = ChannelScope.DIRECT
    signal: Signal = Signal()
    # Hop count as the transport reports it, already normalized (MeshCore's
    # 0xFF direct-route sentinel has unknown hops here, never 0 or 255).
    path_len: int | None = None
    direct: bool | None = None  # heard with no repeater in between
    # Meshtastic's per-packet PKI flag. None where the concept does not exist:
    # MeshCore encrypts all contact traffic by construction and surfaces no
    # per-message flag, and "encrypted by default" is not the same claim as
    # "this packet was PKI-encrypted to us", so it is not hardcoded True.
    encrypted_to_us: bool | None = None
    via_gateway: bool | None = None  # Meshtastic viaMqtt; None where absent
    # Present only for a MeshCore message on a concrete channel. The daemon
    # mirrors a recognized watched-channel command's reply to this index.
    channel_index: int | None = None

    def __post_init__(self) -> None:
        if not self.sender:
            raise ValueError(
                "Inbound.sender must already be resolved to an addressable "
                "node -- an unresolvable sender must be dropped inside the "
                "transport BEFORE constructing an Inbound, never carried "
                "through as None or empty (task 3C finding 3)"
            )


class DeliveryOutcome(StrEnum):
    """Bounded outcomes of one `send_dm`. Matches the terminal states
    `sender.py`/`processed_requests` already record, so the ack correlation
    can move behind the boundary without the outcome vocabulary changing."""

    ACKED = "acked"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Delivery:
    """The resolution of one `send_dm`, correlated by its `tx_ref`.

    `reason` is a bounded string or None -- never firmware free text. A
    Meshtastic transport maps `routing_error` through its existing
    known-values table; a MeshCore transport has no error vocabulary and
    reports None.
    """

    tx_ref: str
    outcome: DeliveryOutcome
    reason: str | None = None


@dataclass(frozen=True)
class ChannelTriggerPolicy:
    """How a channel message addresses the garden, and how often it may.

    A transport that does not watch a channel has NO policy (`None`), which
    is how Meshtastic stays strictly DM-triggered: the eligibility rule is
    one table with a branch that is unreachable when the policy is absent,
    not a per-transport fork.

    `token` and `aliases` match the WHOLE message body, case-insensitively,
    after stripping surrounding whitespace. Whole-body equality is the point:
    a substring or prefix match would fire on ordinary chatter, and a knock
    can carry no payload anyway (a channel trigger never executes a command),
    so there is nothing for a sender to append.
    """

    token: str = "!garden"
    # Per RESOLVED RECIPIENT, not per claimed name: a forged display name must
    # cost an attacker at most one bounded DM to the node being impersonated.
    cooldown_seconds: int = 60
    aliases: tuple[str, ...] = ("!dm",)

    def matches(self, text: str | None) -> bool:
        """True when `text` is exactly a token or alias and nothing else."""
        if text is None:
            return False
        return text.strip().casefold() in (
            token.casefold() for token in (self.token, *self.aliases)
        )


@dataclass(frozen=True)
class TransportStatus:
    """Connection health, bounded to what the send-time guard and the
    freshness gate's `connected_seconds` fallback (`decide_inbound` row 9)
    need -- not the full `RadioStatus` a Meshtastic caller might reach for
    directly. Added in 2B: 2A deferred the reconnect model's shape to
    whichever transport needs one, on the condition that a real need (not a
    guess) earns a Protocol member rather than the Protocol being quietly
    widened. Meshtastic needs `connected_since_monotonic` for exactly this
    row; MeshCore, which never carries a receive timestamp at all (its
    `Inbound.rx_time` is always `None`), prefers `Inbound.sender_ts` (the
    sender's own clock) when a packet carries one -- `connected_seconds`
    remains PERMANENT plumbing for it as the fallback for a packet that
    doesn't.
    """

    connected: bool
    reply_ready: bool
    # The transport's own live identity, or None if it cannot report one
    # (e.g. not yet connected). Compared against a configured expected id
    # by the send-time guard -- a re-check independent of `reply_ready`,
    # since the two are deliberately allowed to disagree in a test that
    # simulates a race between them.
    actual_node_id: NodeId | None
    # Seconds since the current connection began, or None while not
    # connected. `float("inf")` is never encoded here -- callers map an
    # absent value to whatever "effectively fresh forever" means for them.
    connected_seconds: float | None


class Transport(Protocol):
    """Every radio operation the daemon may perform."""

    def status(self) -> TransportStatus:
        """Connection health for the send-time guard and the freshness
        gate. See `TransportStatus`."""
        ...

    @property
    def max_text_bytes(self) -> int:
        """UTF-8 byte budget for one `send_dm` body on THIS transport. A
        transport property because the figure is a property of the radio, not
        of the service: 180 on Meshtastic today, materially smaller and not
        yet measured on MeshCore."""
        ...

    def connect(self) -> None: ...

    def identity(self) -> NodeId:
        """This radio's own node id."""

    def is_broadcast(self, addr: NodeId) -> bool:
        """True when `addr` addresses everyone rather than one node."""

    def on_message(self, callback: Callable[[Inbound], None]) -> None:
        """Registers the sink for received messages."""

    def send_dm(self, to: NodeId, text: str, *, attempt: int = 0) -> str:
        """Transmits one direct message and returns an opaque `tx_ref`.

        `attempt` is zero for the original submission and 1..3 for bounded
        application retries. MeshCore carries it on wire; transports without
        that concept may ignore it.
        """

    def reset_dm_path(self, to: NodeId) -> bool:
        """Reset a cached route so the next DM floods, if supported."""

    def send_channel(self, channel_idx: int, text: str) -> None:
        """Send one reply on a configured channel, or raise when unsupported."""

    def on_delivery(self, callback: Callable[[Delivery], None]) -> None:
        """Registers the sink for ack/fail/expiry of earlier `send_dm`s."""

    def display_label(self, node_id: NodeId) -> str:
        """The node's visitor-facing label in this transport's id form."""

    def short_label(self, node_id: NodeId) -> str:
        """The abbreviated label a reply uses when the full id would not fit."""

    def parse_node_argument(self, text: str) -> NodeId | None:
        """A node id typed by a visitor (full id or this transport's accepted
        short form) as a canonical `NodeId`, or None when it is not one."""
