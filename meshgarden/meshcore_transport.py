"""`MeshCoreTransport`: a SKELETON of 2A's `Transport` Protocol for a
MeshCore companion radio (task 3A). No real `meshcore` package is imported
anywhere in this module -- it is driven entirely by whatever satisfies
`MeshCoreClient` below, which `tests/fake_meshcore.py`'s `FakeMeshCore`
does today and 3B's thin real-adapter wrapper will do tomorrow. The event
vocabulary (`EventType`, `Event`) and the small pure helpers live HERE, not
in the test file, so the fake and this transport share one definition of
the contract; 3B's swap should be able to replace `EventType`/`Event` with
`from meshcore import EventType` (and the real `Event` shape) as close to a
rename as the library's actual shape allows.

Binding inputs: `docs/superpowers/plans/transport-boundary-spec.md` and
`.superpowers/sdd/2026-09-16-epic-meshcore-shared-bbs/scope-decisions.md`
sections 2, 7, 8 (operator decisions; where they disagree with anything
else, they win). Cited MeshCore library facts are
`docs/superpowers/plans/meshcore-notes.md` ("0A notes" below).

The transport can send a response on the one watched channel. It refuses any
other index, so no caller can turn an inbound value into an arbitrary-channel
broadcast. Direct messages retain their ACK-correlated path.

Two operator-decided asymmetries this module exists to implement (NOT
merely to document -- see scope-decisions.md sections 7-8):

- A channel message (`CHANNEL_MSG_RECV`) carries no sender identity at
  all, only the free-text `"SenderName: body"` convention (0A notes
  section 12.1). A claimed name that matches no contact in this
  transport's own roster mirror is DROPPED SILENTLY inside
  `_on_channel_msg` -- no `Inbound` is ever produced, so no channel reply
  is possible anywhere, structurally.
- A direct message (`CONTACT_MSG_RECV`) carries a 6-byte `pubkey_prefix`,
  which already contains the 4 bytes the `!xxxxxxxx` node id needs
  (scope-decisions.md section 8). `_on_contact_msg` therefore ALWAYS
  derives a `sender` and produces an `Inbound`, even for a sender this
  radio has never seen before -- unlike the channel case, receiving a DM
  never depends on the roster. Only a REPLY does: `send_dm` needs the
  sender's full public key, which is only known once the roster mirror
  has it (from a `NEW_CONTACT`/`ADVERTISEMENT` event), and raises
  `MeshCoreUnknownContact` when it does not.

TASK 3B STATUS: the real library import/connection setup, the reconnect
model, the per-connection radio clock set, the `autoadd_config` opt-in
write, and the channel-name-to-index probe loop are now implemented --
see `meshgarden/meshcore_client.py`
(`RealMeshCoreClient`, `resolve_channel_index`, `ensure_autoadd_bit`,
`build_meshcore_transport`). This class (`MeshCoreTransport` itself) still
takes `watched_channel_idx=None` at construction and learns the real index
later via `set_watched_channel_idx` -- `build_meshcore_transport`
constructs the transport, calls `connect()`, waits for the first connect,
THEN resolves the configured channel name against the now-live client and
sets it. `_derive_message_id`/`_normalize_hops`'s hop-count half/
`_meshcore_node_id`/`_parse_sender_name` are now sourced from
`meshgarden/meshcore_compat.py` (ported from PotatoMesh, Apache-2.0) rather
than reimplemented independently here.

STILL left for 3B-2 (not this task): wiring `main.py` to call
`meshcore_client.build_meshcore_transport(...)` and register `on_message`/
`on_delivery` against the running daemon -- `build_meshcore_transport`
ALREADY calls `connect()` internally and returns an already-connected
transport (fix round 2, finding 3: an earlier version of this note told
3B-2 to call `connect()` itself afterward, which would have re-subscribed
every handler a second time and dispatched every inbound event twice;
`connect()` is now idempotent as a backstop, but 3B-2 should not rely on
that -- it should simply not call `connect()` again). MeshCore has no
separate ingest thread to lean on, unlike `RadioAdapter`'s pubsub thread --
this module's own pump thread, see `meshcore_client.py`, is the closest
analogue. Also left for 3B-2: the knock cooldown, and the `sender.py`
`tx_ref`/`int()` hazard.

TASK 3B UPDATE: `MeshCoreClient.subscribe`'s shape is now CONFIRMED against
the installed `meshcore==2.3.11` package (`meshcore/meshcore.py:237-262`,
`MeshCore.subscribe(event_type, callback, attribute_filters=None) ->
Subscription`, delegating to `meshcore/events.py`'s `EventDispatcher`). The
real method accepts a synchronous OR async callback (the dispatcher checks
`asyncio.iscoroutinefunction` and calls a sync one inline,
`events.py:237-244`) and returns a `Subscription` object this transport
never uses -- both are additions over this skeleton's original assumed
`-> None` signature, not contradictions of it. `meshgarden/meshcore_client.py`
(task 3B) wraps the real `meshcore.MeshCore` behind this same `MeshCoreClient`
Protocol, running its asyncio loop in a background daemon thread and handing
translated events to this transport's registered (synchronous) callbacks
through a plain `queue.Queue`, drained by a second "pump" thread -- see that
module's docstring.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from meshgarden import exchange_log, meshcore_compat
from meshgarden.config import parse_node_id_argument
from meshgarden.models import MeshCoreObservation, MeshCoreRoute
from meshgarden.transport import (
    BROADCAST,
    ChannelScope,
    ChannelTriggerPolicy,
    Delivery,
    DeliveryOutcome,
    Inbound,
    NodeId,
    Signal,
    TransportStatus,
)

# Conservative default (transport-boundary-spec.md section 5): no numeric
# MeshCore DM/channel byte limit is establishable from any of 0A's three
# allowed sources, so the default sits below any plausible real figure
# rather than near one. 5A measures the real number on hardware; until
# then this default is what a MeshCoreTransport gets unless the caller
# (3B's config wiring) overrides it via `max_text_bytes=`.
_DEFAULT_MAX_TEXT_BYTES = 120

# Sentinel `identity()` reports before `connect()` has resolved a self
# public key. Cannot collide with a real node id (`config.NODE_ID_RE`
# requires "!" + 8 hex chars) or `BROADCAST` -- same rationale as
# `meshtastic_transport._UNRESOLVED_IDENTITY`, duplicated rather than
# imported so this module stays free of any Meshtastic-side dependency.
_UNRESOLVED_IDENTITY: NodeId = "^unresolved^"

log = logging.getLogger(__name__)


class EventType(StrEnum):
    """Event-name vocabulary. TASK 3B: every member below is now CONFIRMED
    against the installed `meshcore==2.3.11` package's own
    `meshcore/events.py:12-71` `EventType` enum (a plain `Enum`, not a
    `StrEnum` -- these values are copied from it by hand rather than
    imported, so THIS MODULE never needs to import the real `meshcore`
    package just to name an event type -- it has no import of `meshcore`
    anywhere in it. That is narrower than a claim about the whole install:
    `meshgarden/meshcore_client.py` is the module that does the real,
    guarded import -- lazily, only once a MeshCore transport is actually
    being constructed, not at its own module load (see its module
    docstring) -- and translates a real `meshcore.EventType` member to this
    one by `.value`. Three members
    (`CONTACT_MSG_RECV`, `CHANNEL_MSG_RECV`, `ACK`) already had the right
    value from task 3A; the other five (`MSG_SENT`, `CONNECTED`,
    `DISCONNECTED`, `ADVERTISEMENT`, `NEW_CONTACT`, `ERROR`) were placeholder
    guesses in 3A's skeleton and are corrected here -- e.g. `ADVERTISEMENT`
    was `"ADVERTISEMENT"`, the real wire string is `"advertisement"`.
    `SELF_INFO` is new in 3B (task 3B brief item C's subscription list).
    """

    CONTACT_MSG_RECV = "contact_message"  # CONFIRMED -- events.py:15
    CHANNEL_MSG_RECV = "channel_message"  # CONFIRMED -- events.py:16
    ACK = "acknowledgement"  # CONFIRMED -- events.py:33
    MSG_SENT = "message_sent"  # CONFIRMED -- events.py:23 (was "MSG_SENT" in 3A; corrected)
    CONNECTED = "connected"  # CONFIRMED -- events.py:70 (was "CONNECTED" in 3A; corrected)
    DISCONNECTED = "disconnected"  # CONFIRMED -- events.py:71 (was "DISCONNECTED"; corrected)
    ADVERTISEMENT = "advertisement"  # CONFIRMED -- events.py:31 (was "ADVERTISEMENT"; corrected)
    PATH_UPDATE = "path_update"
    NEW_CONTACT = "new_contact"  # CONFIRMED -- events.py:24 (was "NEW_CONTACT"; corrected)
    SELF_INFO = "self_info"  # CONFIRMED -- events.py:14 (new in 3B: task 3B brief item C)
    # `ERROR` is a real command-response event (events.py:67, "command_error"
    # -- was the placeholder "ERROR" in 3A). `commands/messaging.py:110`
    # (installed package) confirms `send_msg` itself can return it (its
    # `send`, from `commands/base.py:213`, always lists `EventType.ERROR`
    # alongside the success type) -- so `send_dm`'s `MeshCoreSendFailed`
    # guard is exercised in practice, not merely defensive.
    ERROR = "command_error"
    OK = "command_ok"  # CONFIRMED -- events.py command response types


@dataclass(frozen=True)
class Event:
    """One event handed to a subscriber: a bare `(type, payload)` pair,
    mirroring how meshcore_py's dispatcher/`wait_for_event` deliver events
    (0A notes section 1, "Event Filtering"). `payload` keys are cited from
    0A's notes wherever a source pinned them (e.g. `pubkey_prefix`,
    `expected_ack`, `code`); this dataclass itself is a stand-in shape for
    this skeleton, not a documented library type.
    """

    type: EventType
    payload: dict[str, Any]


class _Commands(Protocol):
    def send_msg(
        self, dst: str, msg: str, timestamp: int | None = None, attempt: int = 0
    ) -> Event: ...
    def send_chan_msg(self, chan: int, msg: str, timestamp: int | None = None) -> Event: ...
    def reset_path(self, dst: str) -> Event: ...


class MeshCoreClient(Protocol):
    """The slice of a MeshCore companion client this transport drives.
    Satisfied by `tests.fake_meshcore.FakeMeshCore` (fake) and
    `meshgarden.meshcore_client.RealMeshCoreClient` (real, task 3B) --
    a thin wrapper around the real `meshcore.MeshCore` that runs its
    asyncio loop in a background thread and forwards translated events to
    whatever this transport `subscribe()`s here through its own queue-fed
    pump thread; see that module's docstring for the full design. Kept
    intentionally narrow -- see `docs/superpowers/plans/
    meshcore-notes.md` for the fuller documented library surface.
    """

    connected: bool
    self_info: dict[str, Any] | None
    commands: _Commands

    def connect(self) -> None: ...

    def subscribe(self, event_type: EventType, callback: Callable[[Event], None]) -> None:
        """CONFIRMED against the installed `meshcore==2.3.11` package
        (`meshcore/meshcore.py:237-262`): the real method's signature is
        `subscribe(event_type, callback, attribute_filters=None) ->
        Subscription`, strictly a superset of this Protocol's narrower
        shape -- this transport never passes `attribute_filters` and never
        uses the returned `Subscription` (it never unsubscribes), so both
        implementations satisfy this Protocol as declared. The real
        library also accepts an async `callback`; this transport only ever
        registers synchronous ones.
        """
        ...


class MeshCoreUnknownContact(Exception):
    """Raised by `send_dm` when `to` has no known full public key in this
    transport's roster mirror -- e.g. a reply attempted to a DM sender who
    has never advertised or been added as a contact (meshcore-notes.md
    section 1: "sending back requires a known contact"; scope-decisions.md
    section 8). `sender.py`'s send call site currently only catches
    `meshgarden.radio.SendNotReady` by name (see `sender.py` around the
    `send_dm` call) -- 3B must decide how (or whether) this exception is
    caught there too. NOT resolved in this task.
    """


class MeshCoreSendFailed(Exception):
    """Raised by `send_dm` when `commands.send_msg` returns a command-level
    `EventType.ERROR` instead of `EventType.MSG_SENT`. `EventType.ERROR` is
    a real event in this library (meshcore-notes.md section 12.5, via the
    channel-send command's own confirmed `OK`/`ERROR` pair); `send_msg`
    itself returning it is not directly cited, but nothing rules it out, and
    indexing `event.payload["expected_ack"]` unconditionally would instead
    raise a bare `KeyError` -- the same "transmit, then fail confusingly"
    shape as the `tx_ref`/`int()` hazard flagged in task-3A-report.md, just
    one step earlier. No `tx_ref` is ever minted when this is raised (there
    is nothing to correlate a later `Delivery` against), so `sender.py`'s
    send call site must treat it as an immediate failure -- the same open
    question as `MeshCoreUnknownContact`'s docstring above. NOT resolved in
    this task.
    """

    def __init__(self, to: NodeId, event: Event) -> None:
        super().__init__(f"send_msg to {to} returned {event.type}: {event.payload}")
        self.to = to
        self.event = event


class _Ambiguous:
    """Sentinel: a channel display name matched by MORE THAN ONE known
    contact's public key. `meshcore-notes.md` section 12.1 flags exactly
    this collision -- an `adv_name` is attacker-controlled free text, not a
    cryptographic identity, and "nothing prevents two contacts sharing a
    display name". Per the operator's guidance carried in task-3A-report.md
    ("Fix round 1"): an AMBIGUOUS name resolves to NOBODY, exactly like an
    UNRESOLVED one -- ambiguity is not identity, and picking either
    candidate would let an attacker who merely knows a real contact's
    display name ride in on that contact's resolution.
    """


_AMBIGUOUS: Final = _Ambiguous()


# `_meshcore_node_id` and `_parse_sender_name` are used exactly as imported
# from `meshgarden.meshcore_compat` (task 3B: ported from PotatoMesh,
# Apache-2.0 -- see that module's header and THIRD_PARTY_NOTICES.md). 3A's
# skeleton had independent reimplementations of both here; this task
# replaced them with the compat module's versions so there is one
# definition, not two that could drift.


def _normalize_hops(path_len: int | None) -> tuple[int | None, bool | None]:
    """Return observed flood hops and zero-hop RF evidence, independently of routing."""
    hops = meshcore_compat._normalize_hops(path_len)
    return hops, (hops == 0 if hops is not None else None)


class MeshCoreTransport:
    """Wraps a `MeshCoreClient` (the fake today; 3B's real adapter later).
    Constructed once; `connect()` performs the one-shot subscribe + self-
    identity resolution. No reconnection is attempted by this class -- see
    the module docstring's "deliberately left for 3B" list.
    """

    def __init__(
        self,
        client: MeshCoreClient,
        *,
        expected_node_id: NodeId,
        watched_channel_idx: int | None = None,
        alert_channel_idx: int | None = None,
        identity_guard: Callable[[str, str], bool] | None = None,
        max_text_bytes: int = _DEFAULT_MAX_TEXT_BYTES,
        channel_name_match_case_sensitive: bool = True,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self._client = client
        self._identity_guard = identity_guard
        self._identity_lock = threading.RLock()
        self._ambiguous_ids: set[str] = set()
        # THE IDENTITY INTERLOCK, and why it is a REQUIRED keyword argument
        # rather than an optional one (whole-branch review, finding 2).
        # `RadioAdapter._check_identity` forces `reply_ready = False` the
        # moment the radio's actual node id differs from the configured
        # `[radio] expected_node_id`, so a misidentified Meshtastic radio
        # is rejected by `decide_inbound`'s row 1b BEFORE anything happens:
        # no quota slot consumed, no rate-limiter commit, no lab-unlock
        # row, no guestbook signature. This transport used never to be told
        # the expected id at all and derived readiness purely from "we are
        # connected and we know our own id", so every one of those side
        # effects ran under a misidentified radio and only the send itself
        # was refused (by `Sender._guarded_send`'s own check, which still
        # stands as defence in depth). Making the parameter REQUIRED is the
        # fix rather than defaulting it to `None`: an optional expectation
        # is exactly the state that diverged once already, and there is
        # only ever one construction site in production
        # (`build_meshcore_transport`) plus one in tests
        # (`FakeMeshCoreTransport`), both of which have the configured id
        # to hand.
        self._expected_node_id = expected_node_id
        # Injectable so a test (or a future daemon-supplied clock) controls
        # `Inbound.received_at` instead of this class silently reading the
        # wall clock -- 2B was already bitten once by a fake capturing real
        # time and diverging from the daemon's injected test clock, which
        # silently expired queued replies (task-3A-report.md, "Fix round
        # 1"). Defaults to the real wall clock so production callers that
        # don't care can ignore this parameter entirely.
        self._clock = clock
        # None disables channel triggering entirely (`meshcore_channel = ""`
        # per scope-decisions.md section 4): every channel message then
        # tags `ChannelScope.OTHER`, exactly as transport-boundary-spec.md
        # section 1's mapping table requires when a channel cannot be
        # resolved to an index -- "emit OTHER for all channel traffic,
        # i.e. channel triggering is off. Never guess."
        self._watched_channel_idx = watched_channel_idx
        # Spec 2026-10-10-weather-alerts: a second channel index that
        # send_channel ACCEPTS (weather lines) but that never triggers
        # commands -- _on_channel_msg compares against the watched index
        # only, so alert-channel traffic stays ChannelScope.OTHER.
        self._alert_channel_idx = alert_channel_idx
        self._max_text_bytes = max_text_bytes
        # 0A's two cited implementations disagree on case sensitivity for
        # the channel `"SenderName: body"` match (PotatoMesh's own
        # resolver is case-sensitive; meshcore_py's own `get_contact_by_
        # name` is case-insensitive) -- genuinely open per meshcore-notes.md
        # section 12.1, so this is a knob, not a guess.
        self._case_sensitive = channel_name_match_case_sensitive
        self._route_callback: Callable[[MeshCoreRoute], None] | None = None
        self._observation_callback: Callable[[MeshCoreObservation], None] | None = None
        self._message_callback: Callable[[Inbound], None] | None = None
        self._delivery_callback: Callable[[Delivery], None] | None = None
        # Not part of the Transport Protocol (task 3B-2 item F): the only
        # MeshCore event that carries a display name AND position TOGETHER
        # is NEW_CONTACT (a bare ADVERTISEMENT carries neither -- see
        # `_on_advertisement`). There is no `on_contact` member on
        # `Transport` because Meshtastic has nothing analogous to register;
        # this mirrors `MeshtasticTransport.note_routing_packet` in being a
        # transport-specific extra the daemon reaches via `isinstance`.
        self._contact_callback: (
            Callable[[NodeId, str | None, float | None, float | None], None] | None
        ) = None
        self._identity: NodeId | None = None
        self._connected_since_monotonic: float | None = None
        self._suggested_ack_wait: dict[str, float] = {}
        self._retry_timestamps: dict[tuple[str, str], int] = {}
        # Fix round 2 (finding 3): `connect()` must be idempotent. The
        # client-side guard `RealMeshCoreClient.connect()` gained in round 1
        # only protects against starting a SECOND pair of threads -- it does
        # nothing to stop THIS method's own 8 `subscribe()` calls from
        # running twice against the same (already-subscribed) client, which
        # would register every handler twice and dispatch every inbound
        # event twice, i.e. duplicate replies for a service whose whole
        # guarantee is one reply per eligible trigger. See `connect()`.
        self._connect_called = False
        # This transport's own mirror of the radio's contact roster,
        # populated only from `NEW_CONTACT`/`ADVERTISEMENT` events seen
        # after `connect()` -- NOT queried live against the client on
        # every lookup. A real adapter could instead call the library's
        # own `lookup_node_id`/`lookup_node_id_by_name`-shaped primitives
        # against its live cache; mirroring via events is this skeleton's
        # choice because `MeshCoreClient` need expose no extra query
        # surface to make it work.
        self._roster: dict[NodeId, str] = {}  # NodeId -> full public_key hex
        # name key -> public_key hex, or `_AMBIGUOUS` when two or more
        # DISTINCT contacts have advertised under the same name key (see
        # `_Ambiguous`'s docstring).
        self._contacts_by_name: dict[str, str | _Ambiguous] = {}

    # -- Transport Protocol --------------------------------------------

    @property
    def max_text_bytes(self) -> int:
        return self._max_text_bytes

    @property
    def client(self) -> MeshCoreClient:
        """Not part of the Transport Protocol. Exposes the underlying
        `MeshCoreClient` (the real `RealMeshCoreClient`, or a test double
        such as `FakeMeshCore`) so a daemon can supervise the connection
        directly -- specifically, calling `.stop()`/`.connect()` again to
        recover from a wedged run (task-3B-1-report.md's fix round 3/4: the
        pinned library's `disconnect()` can hang forever, which makes a
        run's own outer reconnect loop stop retrying while honestly
        reporting `status().connected == False`). Neither `stop()` nor
        `connect()` on the returned client is part of `MeshCoreClient`
        itself; a caller that uses this must know it is holding a
        `RealMeshCoreClient`-shaped object, not merely the narrow Protocol.
        """
        return self._client

    def on_observation(self, callback: Callable[[MeshCoreObservation], None]) -> None:
        self._observation_callback = callback

    def _observe(self, payload: dict, *, sender: str | None, kind: str) -> None:
        if self._observation_callback is None:
            return
        raw_hops = payload.get("path_len")
        hops, _ = _normalize_hops(raw_hops)
        mode = "direct_route" if raw_hops == 255 else ("flood" if hops is not None else None)
        text = payload.get("text") or ""
        channel = payload.get("channel_idx", -1)
        packet_id = None
        if kind != "MESHCORE_ADVERT":
            packet_id = meshcore_compat._derive_message_id(
                sender_identity=payload.get("pubkey_prefix") or "channel",
                sender_ts=int(payload.get("sender_timestamp") or 0),
                discriminator=f"{kind}:{channel}",
                text=text,
            )
        self._observation_callback(
            MeshCoreObservation(
                received_at=self._clock(),
                sender=sender,
                recipient=(self.identity() or "^unknown") if kind == "MESHCORE_DM" else BROADCAST,
                packet_id=packet_id,
                kind=kind,
                channel=channel,
                snr=payload.get("SNR"),
                hops=hops,
                routing_mode=mode,
                payload_len=len(text.encode("utf-8")),
            )
        )

    def on_route(self, callback: Callable[[MeshCoreRoute], None]) -> None:
        """Register a nonblocking sink for local contact path snapshots."""
        self._route_callback = callback

    def on_contact(
        self, callback: Callable[[NodeId, str | None, float | None, float | None], None]
    ) -> None:
        """Not part of the Transport Protocol (task 3B-2 item F). Registers
        a sink for node identity/position learned from a `NEW_CONTACT`
        event -- `(node_id, adv_name, adv_lat, adv_lon)`, any of the last
        three possibly `None`. Fires on whatever thread delivered the
        event (the real client's pump thread; see `meshcore_client.py`), so
        the callback itself must only perform a bounded, nonblocking
        enqueue, exactly like `main.py`'s `_ingest_sink` does for
        Meshtastic packets -- it must never touch the database directly.
        """
        self._contact_callback = callback

    def set_watched_channel_idx(self, idx: int | None) -> None:
        """Not part of the Protocol. `meshcore_client.build_meshcore_
        transport` needs this: channel-name-to-index resolution
        (`RealMeshCoreClient.resolve_channel_index`) requires a LIVE
        connection, which only exists after `connect()` has already run,
        so the watched index cannot always be known at construction time
        for the real adapter. Safe to call at any time -- `_on_channel_msg`
        reads `self._watched_channel_idx` fresh on every message."""
        self._watched_channel_idx = idx

    def set_alert_channel_idx(self, idx: int | None) -> None:
        """Resolved per connection like the watched index (see
        `build_meshcore_transport`); None refuses every alert send."""
        self._alert_channel_idx = idx

    @property
    def alert_channel_idx(self) -> int | None:
        return self._alert_channel_idx

    def connect(self) -> None:
        """Subscribes to EXACTLY 9 event types -- contact message received,
        channel message received, acknowledgement, advertisement, path
        update, new contact, connected, disconnected, self info (task 3B
        brief item C; PATH_UPDATE added later) -- and nothing else, then
        connects. Subscribing BEFORE calling
        `self._client.connect()` (reversing 3A's order) matters for a real
        client: `meshcore_py`'s own command helper documents the same
        "subscribe-before-send" discipline (`commands/base.py:222-224`,
        installed package) to avoid missing a fast response.

        FIX ROUND 1: neither client can be relied on to deliver its FIRST
        `CONNECTED`/`SELF_INFO` through these subscriptions, and for two
        different, deliberate reasons -- so this method also seeds
        `_identity`/`_connected_since_monotonic` directly, once, as a
        fallback that is load-bearing for at least one of them:

        - The real client (`meshcore_client.py`) can only subscribe AFTER
          `meshcore.MeshCore.create_serial(...)` returns, and that one
          atomic call already connected and fired both events internally
          before returning -- there is no earlier point at which a
          subscription could exist. `RealMeshCoreClient` copes by
          SYNTHESIZING both events onto its own queue right after wiring
          subscriptions (see its `_run_connected_session`), which is what
          makes `_on_connected`/`_on_self_info` the reliable, primary path
          for it -- the direct reads below are redundant for it (its
          `connect()` returns before the background handshake completes,
          so `self._client.self_info`/`.connected` are usually still
          unset at this exact point anyway).
        - `tests.fake_meshcore.FakeMeshCore` fires its FIRST `CONNECTED`/
          `SELF_INFO` at CONSTRUCTION time, before any subscription can
          possibly exist -- modeling the same structural gap rather than
          flattering it (a previous version of this fake fired them from
          `connect()`, i.e. AFTER this method's subscriptions were already
          registered, which is backwards from how the real library
          behaves and let a real bug, `_connected_since_monotonic` never
          getting seeded on the real client's first connect, pass every
          test). The fake does NOT synthesize anything, so for it, the
          direct reads below are the ONLY path for the first connect --
          `_on_connected`/`_on_self_info` only ever fire for the fake on a
          later, explicit reconnect (after a `disconnect()`), which is
          visible because subscriptions are live by then.

        IDEMPOTENT (fix round 2, finding 3): a second call is a no-op,
        logged, not silently ignored. `RealMeshCoreClient.connect()`'s own
        idempotency guard (fix round 1) only stops a second pair of
        background threads from starting -- it does nothing to stop THIS
        method's 8 `subscribe()` calls from running again against the
        SAME already-subscribed client, which would register every
        handler twice and dispatch every inbound event twice: duplicate
        replies, for a service whose whole guarantee is one reply per
        eligible trigger. `build_meshcore_transport` already calls this
        exactly once; this guard is for whatever 3B-2 does next.
        """
        if self._connect_called:
            log.debug("MeshCoreTransport.connect() called again; ignoring (already connected)")
            return
        self._connect_called = True
        self._client.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)
        self._client.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
        self._client.subscribe(EventType.ACK, self._on_ack)
        self._client.subscribe(EventType.NEW_CONTACT, self._on_new_contact)
        self._client.subscribe(EventType.ADVERTISEMENT, self._on_advertisement)
        self._client.subscribe(EventType.CONNECTED, self._on_connected)
        self._client.subscribe(EventType.DISCONNECTED, self._on_disconnected)
        self._client.subscribe(EventType.SELF_INFO, self._on_self_info)
        self._client.connect()
        self_info = self._client.self_info
        pubkey = self_info.get("public_key") if self_info else None
        if pubkey:
            self._identity = meshcore_compat._meshcore_node_id(pubkey)
        if self._client.connected and self._connected_since_monotonic is None:
            self._connected_since_monotonic = time.monotonic()

    def identity(self) -> NodeId:
        return self._identity if self._identity is not None else _UNRESOLVED_IDENTITY

    def is_broadcast(self, addr: NodeId) -> bool:
        return addr == BROADCAST

    def on_message(self, callback: Callable[[Inbound], None]) -> None:
        self._message_callback = callback

    def send_dm(self, to: NodeId, text: str, *, attempt: int = 0) -> str:
        """Looks `to` up in this transport's roster mirror for a full
        public key, then calls `commands.send_msg`. Raises
        `MeshCoreUnknownContact` when `to` is not (yet) a known contact --
        see the module/class docstrings for why that is a real, expected
        outcome and not a bug: an inbound DM's sender can be derived
        without the roster, but a reply cannot.
        """
        with self._identity_lock:
            if to in self._ambiguous_ids:
                raise MeshCoreUnknownContact(to)
            pubkey = self._roster.get(to)
        if pubkey is None:
            raise MeshCoreUnknownContact(to)
        if attempt == 0:
            timestamp = self._clock()
            if len(self._retry_timestamps) >= 128:
                self._retry_timestamps.pop(next(iter(self._retry_timestamps)))
            self._retry_timestamps[(to, text)] = timestamp
        else:
            timestamp = self._retry_timestamps.get((to, text))
            if timestamp is None:
                # Never turn a retry into a fresh user-visible message after
                # bounded bookkeeping evicts its original timestamp.
                raise MeshCoreSendFailed(
                    to, Event(EventType.ERROR, {"reason": "retry_timestamp_unavailable"})
                )
        event = self._client.commands.send_msg(pubkey, text, timestamp=timestamp, attempt=attempt)
        if event.type is not EventType.MSG_SENT:
            # Guard against indexing a payload that isn't there: a
            # command-level ERROR carries no `expected_ack` at all, and
            # `event.payload["expected_ack"]` would otherwise raise a bare
            # `KeyError` -- see `MeshCoreSendFailed`'s docstring for why
            # that shape matters.
            raise MeshCoreSendFailed(to, event)
        expected_ack = event.payload["expected_ack"]
        if isinstance(expected_ack, (bytes, bytearray)):
            tx_ref = expected_ack.hex()
        else:
            tx_ref = str(expected_ack)
        suggested = event.payload.get("suggested_timeout")
        if isinstance(suggested, (int, float)) and suggested > 0:
            # Firmware reports milliseconds for MSG_SENT.
            if len(self._suggested_ack_wait) >= 128:
                self._suggested_ack_wait.pop(next(iter(self._suggested_ack_wait)))
            self._suggested_ack_wait[tx_ref] = float(suggested) / 1000.0
        return tx_ref

    def dm_ack_wait_seconds(self, tx_ref: str, configured: float) -> float:
        """Consume firmware timing advice for one send, never shortening policy."""
        return max(float(configured), self._suggested_ack_wait.pop(tx_ref, 0.0))

    def reset_dm_path(self, to: NodeId) -> bool:
        """Reset one known contact's stored route to flood mode.

        This is local to the companion radio and consumes no RF airtime.
        The following retry floods, recovering from a path cached before a
        mobile contact moved.
        """
        with self._identity_lock:
            if to in self._ambiguous_ids:
                raise MeshCoreUnknownContact(to)
            pubkey = self._roster.get(to)
        if pubkey is None:
            raise MeshCoreUnknownContact(to)
        event = self._client.commands.reset_path(pubkey)
        if event.type is not EventType.OK:
            raise MeshCoreSendFailed(to, event)
        return True

    def send_channel(self, channel_idx: int, text: str) -> None:
        """Submit one message to the watched channel or the alert channel.

        MeshCore confirms local command acceptance with OK but provides no
        remote delivery ACK for channel messages. Any other channel index is
        rejected before reaching the radio.
        """
        allowed = {i for i in (self._watched_channel_idx, self._alert_channel_idx) if i is not None}
        if channel_idx not in allowed:
            raise MeshCoreSendFailed(
                str(channel_idx), Event(EventType.ERROR, {"reason": "channel_not_watched"})
            )
        event = self._client.commands.send_chan_msg(channel_idx, text)
        if event.type is not EventType.OK:
            raise MeshCoreSendFailed(str(channel_idx), event)

    def on_delivery(self, callback: Callable[[Delivery], None]) -> None:
        self._delivery_callback = callback

    def status(self) -> TransportStatus:
        connected = self._client.connected
        connected_seconds = None
        if connected and self._connected_since_monotonic is not None:
            connected_seconds = time.monotonic() - self._connected_since_monotonic
        # `reply_ready` gates ELIGIBILITY (`decide_inbound` row 1b), not
        # just transmission -- so the identity check belongs here, exactly
        # where `RadioAdapter._check_identity` puts it for Meshtastic.
        # Without the `== self._expected_node_id` clause this method
        # reported ready and `main._current_status` simultaneously reported
        # `mismatch_reason = "node_id_mismatch"`: a contradiction that let
        # a misidentified radio consume quota, commit rate limits and write
        # unlock/guestbook rows for replies that could never be sent.
        reply_ready = (
            connected and self._identity is not None and self._identity == self._expected_node_id
        )
        return TransportStatus(
            connected=connected,
            reply_ready=reply_ready,
            actual_node_id=self._identity,
            connected_seconds=connected_seconds,
        )

    def display_label(self, node_id: NodeId) -> str:
        return node_id

    def short_label(self, node_id: NodeId) -> str:
        return "!" + node_id.removeprefix("!")[-4:]

    def parse_node_argument(self, text: str) -> NodeId | None:
        """Same accepted shape as `MeshtasticTransport`'s: a MeshCore node
        id is format-compatible with `config.NODE_ID_RE` (meshcore-notes.md
        "Meshtastic concept -> MeshCore equivalent" table), so no separate
        short-id form is invented here -- both transports delegate to the
        one shared `config.parse_node_id_argument` (task 3D).
        """
        return parse_node_id_argument(text)

    # -- event handlers: not part of the Protocol, called via subscribe --

    def _name_key(self, name: str) -> str:
        # Strip to match `_parse_sender_name`, which strips a channel
        # sender's name before it ever reaches this key -- an `adv_name`
        # with trailing/leading whitespace (e.g. "Bob ") must key the same
        # way on both sides or the roster lookup can never match it.
        name = name.strip()
        return name if self._case_sensitive else name.casefold()

    def identity_is_unambiguous(self, node_id: str) -> bool:
        with self._identity_lock:
            return node_id in self._roster and node_id not in self._ambiguous_ids

    def _remember_contact(self, pubkey: str, adv_name: str | None) -> str | None:
        with self._identity_lock:
            return self._remember_contact_locked(pubkey, adv_name)

    def _remember_contact_locked(self, pubkey: str, adv_name: str | None) -> NodeId | None:
        """Returns the derived node id, or `None` when `pubkey` is too
        short to derive one from -- the SINGLE place that decision is
        made. Every caller that might otherwise act on this contact
        (`_on_new_contact`'s daemon-facing callback, in particular) must
        use THIS return value rather than re-deriving the id itself, so
        the two can never disagree (fix round 2, item 1: re-deriving it a
        second time at the call site, ungated, is exactly what let a
        short-key advert still reach the daemon as `sender=None` even
        though the roster itself correctly refused it)."""
        node_id = meshcore_compat._meshcore_node_id(pubkey)
        if node_id is None:
            # Task 3C finding 3: a public key too short to derive a node id
            # from (a truncated NEW_CONTACT/ADVERTISEMENT frame -- the
            # library's reader short-reads rather than raising) must never
            # enter the roster at all. Indexing it anyway (the previous
            # behavior) let `self._roster[None]` and
            # `self._contacts_by_name[key] = pubkey` both resolve later,
            # producing an `Inbound` with `sender=None` -- which poisoned
            # the per-recipient knock cooldown under a NULL node id that
            # SQLite lets repeat without limit. Dropped here, before either
            # index is touched, exactly like an unresolvable channel sender
            # is already dropped in `_on_channel_msg`.
            return None
        pubkey = pubkey.lower()
        previous = self._roster.get(node_id)
        if (
            node_id in self._ambiguous_ids
            or (previous is not None and previous != pubkey)
            or (self._identity_guard is not None and not self._identity_guard(node_id, pubkey))
        ):
            # Record the collision durably even when the in-memory check found it first.
            if self._identity_guard is not None:
                self._identity_guard(node_id, pubkey)
            self._ambiguous_ids.add(node_id)
            self._roster.pop(node_id, None)
            # A `_contacts_by_name` entry pointing at either the pubkey just
            # quarantined (`previous`) or the incoming one (`pubkey`) must go
            # too, or a channel message claiming that name still resolves
            # to a node id that's now in `_ambiguous_ids` -- see
            # `_on_channel_msg`'s own check of the same set.
            for key, value in list(self._contacts_by_name.items()):
                if value in (previous, pubkey):
                    del self._contacts_by_name[key]
            log.error("MeshCore identity collision quarantined node=%s", node_id)
            return None
        self._roster[node_id] = pubkey
        if not adv_name:
            return node_id
        key = self._name_key(adv_name)
        existing = self._contacts_by_name.get(key)
        if existing is None or existing == pubkey:
            # First contact under this name, or the same contact
            # re-advertising (e.g. a repeated NEW_CONTACT refresh) --
            # neither is a collision.
            self._contacts_by_name[key] = pubkey
        else:
            # A DIFFERENT contact already claims this name key: the name
            # is no longer identifying, and stays that way even if one of
            # the two contacts later disappears -- see `_Ambiguous`.
            self._contacts_by_name[key] = _AMBIGUOUS
        return node_id

    def forget_contact(self, public_key: str) -> None:
        """Drops name mappings for a contact removed from the radio, so a
        channel message under that name no longer resolves to it. An
        `_AMBIGUOUS` name stays (another contact may still claim it), and
        `_ambiguous_ids`/identity bindings are deliberately untouched."""
        public_key = public_key.lower()
        with self._identity_lock:
            for key, value in list(self._contacts_by_name.items()):
                if value == public_key:
                    del self._contacts_by_name[key]

    def _on_new_contact(self, event: Event) -> None:
        pubkey = event.payload.get("public_key")
        if not pubkey:
            return
        adv_name = event.payload.get("adv_name")
        node_id = self._remember_contact(pubkey, adv_name)
        if node_id is None:
            # Fix round 2, item 1: a pubkey `_remember_contact` refused to
            # index (too short to derive a node id from) must not reach the
            # daemon's contact callback either -- that callback ends at
            # `Collector.record_contact_identity(None, ...)`, whose
            # INSERT-OR-IGNORE never actually ignores a null primary key
            # (SQLite permits unlimited NULLs in a TEXT PRIMARY KEY), so
            # every such advert wrote another row with `node_id = NULL`.
            # Same defect as finding 3, same fix: refuse before acting on
            # it, using the ONE decision `_remember_contact` already made
            # rather than re-deriving (and this time forgetting to check)
            # a second one.
            return
        if not event.payload.get("_imported", False):
            self._observe({}, sender=node_id, kind="MESHCORE_ADVERT")
        if self._contact_callback is not None and not event.payload.get("_route_only"):
            # NEW_CONTACT (0A notes section 6 / meshcore-notes.md) carries
            # `adv_lat`/`adv_lon` alongside `adv_name` -- the only MeshCore
            # event that does. A bare ADVERTISEMENT (`_on_advertisement`)
            # carries only `public_key`, never a name or a position.
            self._contact_callback(
                node_id,
                adv_name,
                event.payload.get("adv_lat"),
                event.payload.get("adv_lon"),
            )

        route = MeshCoreRoute.from_contact(node_id, event.payload, self._clock())
        if route is not None and self._route_callback is not None:
            self._route_callback(route)

    def _on_advertisement(self, event: Event) -> None:
        # A bare ADVERTISEMENT carries only the public key, no name
        # (meshcore-notes.md section 6) -- still enough to answer a later
        # `send_dm` even though channel-name resolution gets nothing from
        # it. TASK 3B FIX: 3A's skeleton read payload key `adv_key` here,
        # guessed from 0A's `_rx_advert_to_node_dict` citation (a DIFFERENT,
        # RX-log-only event: `decode.py:97-159`'s field name, not this bare
        # event's). Confirmed directly against the installed package that
        # the bare (non-RX-log) `ADVERTISEMENT` event's payload key is
        # `public_key`, not `adv_key` (`meshcore/reader.py:574-578`:
        # `res["public_key"] = dbuf.read(32).hex(); ...dispatch(Event(
        # EventType.ADVERTISEMENT, res, res))`). Reading `adv_key` here
        # meant every real bare advertisement was silently dropped (the
        # `.get()` returned None); this is now a real bug fix, not just a
        # marker resolution -- see task-3B-1-report.md.
        pubkey = event.payload.get("public_key")
        if not pubkey:
            return
        node_id = self._remember_contact(pubkey, adv_name=None)
        if node_id is not None:
            self._observe({}, sender=node_id, kind="MESHCORE_ADVERT")

    def _on_connected(self, event: Event) -> None:
        # Fires on the initial connect AND on every reconnect the client
        # performs on its own (task 3B: a real client's own `auto_reconnect`
        # loop, or its outer restart loop once that gives up -- see
        # `meshcore_client.py`). Resetting the clock here, rather than only
        # once inside `connect()` as 3A's skeleton did, is what makes
        # `status().connected_seconds` -- `decide_inbound` row 9's ONLY
        # freshness signal for MeshCore (transport-boundary-spec.md section
        # 5's mapping table) -- correct across a reconnect instead of just
        # counting from the very first connect forever.
        self._connected_since_monotonic = time.monotonic()

    def _on_disconnected(self, event: Event) -> None:
        self._connected_since_monotonic = None

    def _on_self_info(self, event: Event) -> None:
        # The primary path for identity resolution on a REAL client, whose
        # `connect()` returns as soon as its background thread starts, not
        # once the radio handshake (and thus `self_info`) is actually ready
        # (see `meshcore_client.py`). This is reliably true only as of fix
        # round 1: `RealMeshCoreClient._run_connected_session` SYNTHESIZES a
        # SELF_INFO event onto its queue right after wiring subscriptions,
        # specifically because the real library's own first SELF_INFO fires
        # inside `create_serial(...)`, before any subscription of ours can
        # exist -- without that synthesis this handler would never fire for
        # a real client's first connect at all. `connect()`'s own immediate
        # read of `self._client.self_info` is the fallback -- the ONLY path
        # for a synchronous client like the fake, which does not synthesize
        # anything for its own first connect either (see `FakeMeshCore.
        # __init__`).
        pubkey = event.payload.get("public_key")
        if pubkey:
            self._identity = meshcore_compat._meshcore_node_id(pubkey)

    def _on_contact_msg(self, event: Event) -> None:
        payload = event.payload
        prefix = payload.get("pubkey_prefix")
        if not prefix:
            exchange_log.log_rejection("unknown", "missing_sender_key")
            return
        sender = meshcore_compat._meshcore_node_id(prefix)
        if sender is None:
            exchange_log.log_rejection("unknown", "invalid_sender_key")
            # Task 3C finding 3: `prefix` can be truthy but still too short
            # (under 4 bytes) to derive a node id from -- `Inbound`'s own
            # contract requires an already-resolved sender (enforced
            # structurally by `Inbound.__post_init__`), so this is dropped
            # here, before any `Inbound` is constructed, exactly like a
            # missing prefix is dropped above.
            return
        self._observe(payload, sender=sender, kind="MESHCORE_DM")
        with self._identity_lock:
            key = self._roster.get(sender)
            if (
                sender in self._ambiguous_ids
                or len(prefix) < 12
                or (key is not None and not key.startswith(prefix.lower()))
            ):
                exchange_log.log_rejection(sender, "ambiguous_or_unknown_sender_key")
                return
        if self._message_callback is None:
            return
        path_len, direct = _normalize_hops(payload.get("path_len"))
        text = payload.get("text")
        raw_sender_ts = payload.get("sender_timestamp")
        inbound = Inbound(
            received_at=self._clock(),
            sender=sender,
            recipient=self.identity(),
            is_text=True,
            text=text,
            raw_id=meshcore_compat._derive_message_id(
                sender_identity=prefix,
                sender_ts=int(raw_sender_ts or 0),
                discriminator="dm",
                text=text or "",
            ),
            rx_time=None,  # sender_timestamp is the SENDER's clock, never ours (spec section 0)
            # Freshness fallback only (never rx_time): untrusted, but can only
            # make a stale packet look fresh, never the reverse -- see
            # `Inbound.sender_ts` and `commands._is_stale`.
            sender_ts=int(raw_sender_ts) if raw_sender_ts else None,
            channel=ChannelScope.DIRECT,
            signal=Signal(snr=payload.get("SNR"), rssi=None),
            path_len=path_len,
            direct=direct,
        )
        self._message_callback(inbound)

    def _on_channel_msg(self, event: Event) -> None:
        payload = event.payload
        self._observe(payload, sender=None, kind="MESHCORE_CHANNEL")
        raw_text = payload.get("text")
        name, body = meshcore_compat._parse_sender_name(raw_text)
        is_knock = ChannelTriggerPolicy().matches(body)
        if name is None:
            if is_knock:
                exchange_log.log_rejection("unknown", "missing_sender_name", command="KNOCK")
                log.info("meshcore knock ignored reason=missing_sender_name")
            return  # no sender convention at all -- nothing to resolve
        pubkey = self._contacts_by_name.get(self._name_key(name))
        if pubkey is None or pubkey is _AMBIGUOUS:
            if is_knock:
                exchange_log.log_rejection(
                    "unknown",
                    "unknown_sender_name" if pubkey is None else "ambiguous_sender_name",
                    command="KNOCK",
                )
                log.info(
                    "meshcore knock ignored reason=%s",
                    "unknown_sender_name" if pubkey is None else "ambiguous_sender_name",
                )
            # scope-decisions.md section 7: an unresolvable claimed sender
            # is dropped SILENTLY. An AMBIGUOUS one (two+ distinct contacts
            # sharing this name -- see `_Ambiguous`) is dropped the exact
            # same way: ambiguity is not identity. With no resolved contact,
            # there is no safe identity to attribute a channel command to.
            return
        sender = meshcore_compat._meshcore_node_id(pubkey)
        if sender is None:
            # Task 3C finding 3, second layer: `_remember_contact` now
            # refuses to index a pubkey this short in the first place, so
            # this should be unreachable in practice -- but `Inbound`
            # construction must never depend on that alone. Belt and
            # braces: an unresolvable sender is dropped here too, exactly
            # like the ambiguous/unknown case just above.
            return
        with self._identity_lock:
            if sender in self._ambiguous_ids:
                # Same quarantine `_on_contact_msg` already respects: a
                # collision can leave a stale `_contacts_by_name` entry
                # pointing at a now-quarantined node id (belt and braces
                # alongside `_remember_contact_locked`'s own cleanup of
                # that entry) -- never resolve a sender out of it.
                exchange_log.log_rejection(sender, "ambiguous_or_unknown_sender_key")
                return
        if self._message_callback is None:
            return
        channel_idx = payload.get("channel_idx")
        watched = self._watched_channel_idx is not None and channel_idx == self._watched_channel_idx
        path_len, direct = _normalize_hops(payload.get("path_len"))
        raw_sender_ts = payload.get("sender_timestamp")
        inbound = Inbound(
            received_at=self._clock(),
            sender=sender,
            recipient=BROADCAST,
            is_text=True,
            text=body,
            raw_id=meshcore_compat._derive_message_id(
                # casefold regardless of `_case_sensitive`: this is a dedup
                # fingerprint, not the roster identity match, so it should
                # not vary with that config knob.
                sender_identity=name.casefold(),
                sender_ts=int(raw_sender_ts or 0),
                discriminator=f"c{channel_idx}",
                text=body,
            ),
            rx_time=None,
            # Freshness fallback only -- see `_on_contact_msg` above.
            sender_ts=int(raw_sender_ts) if raw_sender_ts else None,
            channel=ChannelScope.WATCHED if watched else ChannelScope.OTHER,
            signal=Signal(snr=payload.get("SNR"), rssi=None),
            path_len=path_len,
            direct=direct,
            channel_index=channel_idx,
        )
        self._message_callback(inbound)

    def _on_ack(self, event: Event) -> None:
        if self._delivery_callback is None:
            return
        code = event.payload.get("code")
        if code is None:
            return
        # No failure vocabulary exists for MeshCore (meshcore-notes.md
        # section 3): only ACKED is ever reported here. A send that never
        # gets an ACK looks identical to one that is merely slow -- it
        # ages to `unknown` in sender.py's own TTL maintenance tick, never
        # `FAILED`/`EXPIRED` through this callback (transport-boundary-
        # spec.md section 4).
        self._delivery_callback(Delivery(tx_ref=code, outcome=DeliveryOutcome.ACKED))
