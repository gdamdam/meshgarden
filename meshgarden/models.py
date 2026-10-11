from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class MeshCoreRoute:
    """Saved outgoing path; cached_at is our read time, not RF verification."""

    node_id: str
    hops: int | None
    hashes: tuple[str, ...]
    cached_at: int

    @classmethod
    def from_contact(cls, node_id: str, payload: dict, cached_at: int):
        count = payload.get("out_path_len")
        if type(count) is not int:
            return None
        if count in (-1, 255):
            return cls(node_id, None, (), cached_at)
        mode = payload.get("out_path_hash_mode", 0)
        path = payload.get("out_path")
        if (type(mode) is not int or mode not in (0, 1, 2)
                or not 0 <= count <= 63 or count * (mode + 1) > 64
                or not isinstance(path, str) or len(path) != count * (mode + 1) * 2
                or any(c not in "0123456789abcdefABCDEF" for c in path)):
            return None
        width = (mode + 1) * 2
        return cls(node_id, count, tuple(path[i:i + width].lower()
                                        for i in range(0, len(path), width)), cached_at)


@dataclass(frozen=True)
class MeshCoreObservation:
    """Content-free companion reception, independent of command eligibility.

    Channel names are forgeable, so their sender is unknown. Direct routing
    does not reveal the number of repeaters traversed.
    """

    received_at: int
    sender: str | None
    recipient: str
    packet_id: int | None
    kind: str
    channel: int = -1
    snr: float | None = None
    rssi: int | None = None
    hops: int | None = None
    routing_mode: str | None = None
    payload_len: int = 0


@dataclass(frozen=True)
class IncomingPacket:
    received_at: int  # server UTC epoch seconds
    received_monotonic: float
    from_num: int
    to_num: int
    packet_id: int | None
    channel: int  # 0 when absent
    portnum: str  # e.g. "TEXT_MESSAGE_APP"
    rx_time: int | None
    rx_snr: float | None
    rx_rssi: int | None
    hop_limit: int | None
    hop_start: int | None
    # None only for rows recorded before 2026-09-14 (legacy) or synthetic
    # observations; radio.py never emits None for a decoded packet -- absent
    # viaMqtt means RF, not unknown origin.
    via_mqtt: bool | None
    pki_encrypted: bool | None
    payload_len: int
    text: str | None  # transient only; NEVER persisted or logged
    request_id: int | None  # ROUTING responses
    routing_error: str | None
    short_name: str | None = None  # NODEINFO_APP only; display label, not an identity
    long_name: str | None = None
    # NODEINFO_APP only: hardware/role display fields and a derived
    # presence flag for the operator's public key -- the key material
    # itself is NEVER read or stored, only whether the field was present.
    hw_model: str | None = None
    role: str | None = None
    public_key_present: bool | None = None  # None = no NODEINFO_APP user data this packet
    is_licensed: bool | None = None
    # POSITION_APP only: latitudeI/longitudeI converted from 1e-7 fixed
    # point to degrees; None when the position sub-message was absent, per
    # proto3's omit-zero-valued-fields rule (an exact 0.0 reading is
    # indistinguishable from "no position" and is likewise reported None).
    lat: float | None = None
    lon: float | None = None
    position_alt: int | None = None  # meters
    position_precision_bits: int | None = None
    position_time: int | None = None  # radio-reported GPS fix time, epoch seconds
    # TELEMETRY_APP deviceMetrics only.
    battery_level: int | None = None  # percent 0-100
    voltage: float | None = None
    channel_utilization: float | None = None  # percent
    air_util_tx: float | None = None  # percent
    uptime_seconds: int | None = None
    # Transient reply routing for a MeshCore command received on the configured
    # channel. None means reply by DM. This is never persisted as observation
    # data and Meshtastic packets always leave it unset.
    reply_channel_idx: int | None = None


@dataclass(frozen=True)
class ReplyJob:
    request_sender: str  # "!xxxxxxxx"
    request_packet_id: int
    request_received_at: int
    deadline_monotonic: float
    body: str
    # Fix round 1 (task 3B-3, item 2): an optional compensating action.
    # `Sender` calls this with a bounded reason string if, and only if,
    # this job settles to a DEFINITE pre-transmission failure -- this
    # job's own TTL expiry (never popped in time), or a `_guarded_send`
    # refusal that happens BEFORE any radio call is attempted (e.g. the
    # radio not being ready) -- and NEVER for a blocked sender, and NEVER
    # when a transmission was actually attempted or its outcome is
    # uncertain (acked, failed, unknown: `Sender` withholds the call in
    # every one of those cases). `None` for every ordinary command reply;
    # only the channel knock sets this, to release its persistent cooldown
    # claim rather than silencing a legitimate knocker for an entire
    # window over a failure that was never their fault.
    #
    # THREADING (fix round 2, a Critical the first cut got wrong): `Sender`
    # calls this from the TX worker thread (`_finish_expired`/
    # `_mark_dropped`) -- NEVER assume the DB worker thread. A callback
    # that touches a `sqlite3.Connection` opened on a different thread
    # raises (`check_same_thread` is never relaxed anywhere in this
    # codebase), and `Sender` guards against a raising callback itself
    # (never lets it escape `_finish_expired`/`_mark_dropped`, and
    # `pump()`'s own per-expired-job try/except is a second backstop) --
    # but a caller whose callback silently does nothing because it raised
    # every time is not actually getting the release it asked for. A
    # callback set here MUST marshal any DB-owning work onto the correct
    # thread itself (`Daemon._enqueue_knock_release` does this via the
    # existing ingest queue) rather than touching a worker-owned
    # connection directly.
    release_on_pretransmission_failure: Callable[[str], None] | None = None
    # Task 3C finding 1: `ReplyQueue`'s "at most one pending reply per
    # sender" rule is keyed on `request_sender` by default -- correct for
    # every ordinary command reply, where the sender IS the recipient. A
    # channel knock's reply is also addressed to `request_sender` (the
    # resolved contact), but that node did not send the triggering
    # message -- an ATTACKER did, by forging its name on the channel -- so
    # sharing that node's own per-sender slot let a forged knock occupy
    # the slot the impersonated node's own genuine next command needed,
    # refusing it with no reply after it had already burned a dedup row
    # and a quota slot. `reply_queue_key`, when set, is what `ReplyQueue`
    # actually uses for that one-pending-reply bookkeeping INSTEAD of
    # `request_sender`; the knock passes a value that can never collide
    # with a real node id (`NODE_ID_RE` can never match it), so a knock's
    # pending reply and a real command's pending reply to the SAME node
    # are never the same slot. `None` (the default) means "use
    # `request_sender`", unchanged for every ordinary reply.
    reply_queue_key: str | None = None
    # Task 3C finding 2: marks this job as knock-class, subject to
    # `ReplyQueue`'s separate, smaller `max_pending_knocks` cap -- a burst
    # of forged knocks, each naming a DIFFERENT (attacker-minted) resolved
    # contact, would otherwise have a DIFFERENT `reply_queue_key` each and
    # so never collide with each other either, defeating `reply_queue_
    # key`'s own fix by simply filling the shared queue with many knocks
    # instead of one. `False` for every ordinary command reply.
    is_knock: bool = False
    # A cooldown explanation is bookkeeping for a rejected command, not a
    # new application reply. It must neither cancel a legitimate reply's
    # pending retries nor acquire application retries of its own.
    is_cooldown_notice: bool = False
    # A MeshCore channel command replies once on that same configured channel.
    # None retains the normal DM delivery path. Channel replies have no remote
    # ACK and therefore are never application-retried.
    reply_channel_idx: int | None = None
    # Earliest monotonic time the TX worker may send this job. `Sender.enqueue`
    # stamps channel replies with now + service.channel_reply_delay_seconds:
    # a reply transmitted in the same second the request arrived collides
    # with the repeaters still re-flooding that request (hardware finding,
    # 2026-10-10: channel replies lost with no trace while DMs got through).
    # 0.0 means "send as soon as the cooldowns allow", unchanged for DMs.
    not_before_monotonic: float = 0.0

    def queue_key(self) -> str:
        """The key `ReplyQueue` uses for the "one pending reply" rule --
        `reply_queue_key` if set, else `request_sender`. One place this
        ternary lives, used identically by `ReplyQueue.offer` and by
        `Sender.pump`'s `release`/`requeue` call sites."""
        return self.reply_queue_key if self.reply_queue_key is not None else self.request_sender


@dataclass(frozen=True)
class GuestbookEntry:
    entry_id: int
    created_at: int
    sender_node_id: str
    display_name: str | None
    message: str
    visible: bool
    moderation_reason: str | None = None
    topic: str | None = None
