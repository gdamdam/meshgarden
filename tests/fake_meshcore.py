"""A fake MeshCore companion radio.

Mirrors the library surface `docs/superpowers/plans/meshcore-notes.md` ("0A
notes") documents: inbound direct messages (`CONTACT_MSG_RECV`), inbound
channel messages (`CHANNEL_MSG_RECV`), outbound sends
(`commands.send_msg` -> `expected_ack`/`suggested_timeout`), delivery
reports (`EventType.ACK`), adverts (`ADVERTISEMENT`) and new contacts
(`NEW_CONTACT`), connect/disconnect, and self info. No real `meshcore`
import anywhere in this module or in `meshgarden/meshcore_transport.py` --
3B adds that dependency; this fake and that skeleton share one event
vocabulary (`EventType`/`Event`, defined in `meshcore_transport.py`) so
swapping the fake for the real library later is a driver change, not a
redesign.

Two facts this fake exists to make testable (0A notes section 12.1;
scope-decisions.md section 8, binding):

- A channel message carries NO sender identity on the wire -- only a
  free-text `"SenderName: body"` convention. `emit_channel_message`/
  `emit_channel_message_from` can emit a name that matches no known
  contact (or no colon at all); `MeshCoreTransport` must drop it silently.
- An inbound DM carries only a 6-byte pubkey prefix -- enough to derive
  the `!xxxxxxxx` node id directly, but NOT enough to send a reply (that
  needs the contact's full public key from the roster). `emit_direct_
  message` can emit a prefix that matches no roster entry at all.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from meshgarden.contacts import RadioContact
from meshgarden.meshcore_compat import _meshcore_node_id
from meshgarden.meshcore_transport import (
    Event,
    EventType,
    MeshCoreTransport,
)


@dataclass
class _Contact:
    public_key: str  # hex, 64 chars (32 bytes) for a fully known contact
    adv_name: str | None = None
    adv_type: int = 1  # ADV_TYPE_CHAT (0A notes section 6)
    last_advert: int | None = None


class _FakeCommands:
    """Stands in for the MeshCore direct- and channel-message commands."""

    def __init__(self, fake: FakeMeshCore) -> None:
        self._fake = fake
        self._next_ack = 1
        self._force_error_reason: str | None = None

    def force_next_send_error(self, reason: str = "test-forced-error") -> None:
        """Test-side only: makes the NEXT `send_msg` call return a command-
        level `EventType.ERROR` instead of `MSG_SENT` -- see
        `meshgarden.meshcore_transport.MeshCoreSendFailed`'s docstring for
        why `send_dm` must be able to handle this rather than assume
        `MSG_SENT` unconditionally."""
        self._force_error_reason = reason

    def send_msg(
        self, dst: str, msg: str, timestamp: int | None = None, attempt: int = 0
    ) -> Event:
        if self._force_error_reason is not None:
            reason = self._force_error_reason
            self._force_error_reason = None
            return Event(type=EventType.ERROR, payload={"reason": reason})
        self._fake.sent.append(
            {"dst": dst, "msg": msg, "timestamp": timestamp, "attempt": attempt}
        )
        expected_ack = struct.pack(">I", self._next_ack)
        self._next_ack += 1
        return Event(
            type=EventType.MSG_SENT,
            payload={"expected_ack": expected_ack, "suggested_timeout": 5000},
        )

    def send_chan_msg(self, chan: int, msg: str, timestamp: int | None = None) -> Event:
        if self._force_error_reason is not None:
            reason = self._force_error_reason
            self._force_error_reason = None
            return Event(type=EventType.ERROR, payload={"reason": reason})
        self._fake.channel_sent.append({"channel_idx": chan, "msg": msg, "timestamp": timestamp})
        return Event(type=EventType.OK, payload={})

    def reset_path(self, dst: str) -> Event:
        self._fake.paths_reset.append(dst)
        return Event(type=EventType.OK, payload={})


class FakeMeshCore:
    """A fake companion radio implementing the slice of `meshcore_py`'s
    surface `MeshCoreTransport` (and 3B's real adapter) drives.

    Test-side methods (`emit_*`, `add_contact`) simulate wire events or
    roster state; production-shaped methods (`connect`, `subscribe`,
    `commands.send_msg`, `.connected`, `.self_info`) are exactly what
    `MeshCoreTransport` calls, satisfying its `MeshCoreClient` Protocol.
    """

    def __init__(
        self,
        *,
        self_public_key: str = "11223344" + "00" * 28,
        self_adv_name: str = "meshgarden",
        stored_contacts: Sequence[tuple[str, str | None]] = (),
    ) -> None:
        self.connected = False
        self.self_info: dict[str, Any] | None = {
            "public_key": self_public_key,
            "adv_name": self_adv_name,
        }
        self.commands = _FakeCommands(self)
        # Direct and channel sends are recorded separately for assertions.
        self.sent: list[dict[str, Any]] = []
        self.channel_sent: list[dict[str, Any]] = []
        self.paths_reset: list[str] = []
        # Every `send_advert()` call, test-visible (task 3B-4 fix 2): a
        # flood self-advertisement never reaches any subscriber of THIS
        # radio (it is a broadcast the sending radio does not hear itself),
        # so there is nothing to dispatch -- just a count/log a test can
        # assert against, mirroring how `sent` tracks `send_msg`.
        self.adverts_sent = 0
        # Mirrors `RealMeshCoreClient.max_contacts` (device query), settable by tests.
        self.max_contacts: int | None = 350
        self._subscribers: dict[EventType, list[Any]] = {}
        self._contacts: dict[str, _Contact] = {}  # keyed by public_key hex
        self._connect_calls = 0
        # Real-hardware finding (task 3B-4 fix 1): the companion firmware
        # QUEUES inbound messages (CONTACT_MSG_RECV/CHANNEL_MSG_RECV)
        # behind a MESSAGES_WAITING push -- it does not deliver them
        # spontaneously, no matter how long a client merely subscribes and
        # waits. `RealMeshCoreClient` copes by calling the real library's
        # `mc.start_auto_message_fetching()` once per connected session
        # (`meshcore_client.py::_start_message_fetching`); this fake models
        # the same queue-and-fetch shape: `emit_direct_message`/
        # `emit_channel_message*` HOLD their event here instead of
        # dispatching it until fetching is enabled (see `connect()`,
        # `_start_message_fetching`), so a test -- or a daemon under test --
        # that never enables fetching sees nothing, exactly like real
        # hardware. Only these two message-shaped events are gated: adverts,
        # new contacts, acks, and connect/disconnect are pushed by the
        # library's own dispatcher, never queued behind `get_msg()`.
        self._fetching_enabled = False
        self._pending_messages: list[Event] = []
        # Contacts the RADIO already has in its own storage before this
        # process ever ran -- the ordinary state after any daemon restart.
        # The library fires NO event for these (its NEW_CONTACT push only
        # covers contacts the radio did not previously know, and connecting
        # loads none of them), so a client that mirrors the roster purely
        # from live events starts every restart blind. `RealMeshCoreClient.
        # _seed_contacts` closes that by fetching them and synthesizing one
        # NEW_CONTACT-shaped event each; `connect()` below models exactly
        # that, which is what makes the daemon-level restart test in
        # tests/test_daemon_meshcore.py meaningful.
        for public_key, adv_name in stored_contacts:
            self.add_contact(public_key, adv_name)

        # FIX ROUND 1 (review finding: fake ordering flattered the
        # implementation). `meshcore.MeshCore.create_serial` connects AND
        # fires its own CONNECTED/SELF_INFO events (via `send_appstart`,
        # confirmed against the installed `meshcore==2.3.11` package)
        # INSIDE one atomic async call, before returning a usable `mc`
        # object at all -- there is no earlier point at which calling code
        # could have subscribed, because subscribing requires an `mc`
        # reference that does not exist yet. Firing both events HERE, at
        # construction (before `_subscribers` can possibly hold anything,
        # since `MeshCoreTransport` cannot have subscribed to an object it
        # does not have a reference to until after this returns), models
        # that structural gap instead of flattering it: the previous
        # version fired these from `connect()`, AFTER
        # `MeshCoreTransport.connect()` had already subscribed, which is
        # backwards from what the real library actually does and is what
        # let a real bug (this task's finding A: `_on_connected` never
        # firing on the real client's first connect, so
        # `status().connected_seconds` -- MeshCore's only freshness signal
        # -- stayed `None` forever) pass every test. `meshgarden/
        # meshcore_client.py`'s `RealMeshCoreClient` copes with the same
        # gap by synthesizing both events onto its queue right after
        # wiring subscriptions; this fake does NOT synthesize anything, so
        # any test relying on the FIRST connect's identity/connected_
        # seconds now genuinely exercises `MeshCoreTransport.connect()`'s
        # direct fallback reads of `self._client.self_info`/`.connected`,
        # not the event-handler path -- see task-3B-1-report.md, "Fix
        # round 1", for exactly which tests that turned out to be.
        self.connected = True
        self._dispatch(Event(EventType.CONNECTED, {"reconnected": False}))
        if self.self_info:
            self._dispatch(Event(EventType.SELF_INFO, dict(self.self_info)))

    # -- production-shaped surface: what MeshCoreTransport calls ---------

    def connect(self) -> None:
        """The FIRST call (made by `MeshCoreTransport.connect()`
        immediately after construction) is a deliberate no-op: this fake
        already "connected" in `__init__`, for the structural reason
        explained there. A LATER call -- after an explicit `disconnect()`,
        simulating a genuine mid-session reconnect -- fires CONNECTED/
        SELF_INFO for real, visible to whatever subscribed in between,
        exactly like a real reconnect does: unlike the very first connect,
        `meshcore_py`'s own `ConnectionManager._attempt_reconnect` fires
        its CONNECTED event through subscriptions that are already live by
        then.
        """
        self._connect_calls += 1
        # Message fetching and stored-contact seeding happen on EVERY
        # connect, first included, and before the early return below:
        # `RealMeshCoreClient` performs both inside each connected session,
        # after its subscriptions are wired, so these ARE visible to
        # whatever subscribed -- unlike the CONNECTED/SELF_INFO pair in
        # `__init__`, which models the library's genuinely unsubscribable
        # first handshake.
        self._start_message_fetching()
        self._seed_stored_contacts()
        self.send_advert()
        if self._connect_calls == 1:
            return
        self.connected = True
        self._dispatch(Event(EventType.CONNECTED, {"reconnected": True}))
        if self.self_info:
            self._dispatch(Event(EventType.SELF_INFO, dict(self.self_info)))

    def disconnect(self, *, reason: str | None = None) -> None:
        """Not driven by `MeshCoreTransport` in this task (its `connect()`
        is the Protocol's only connection member; see 3A's report for why
        the reconnect model is deliberately left to 3B). Exposed here so
        3B's reconnect work has something to simulate against.
        """
        self.connected = False
        # Mirrors the real library's `MeshCore.disconnect()`, which stops
        # auto message fetching on its own (`stop_auto_message_fetching()`)
        # -- a message emitted after this point (before the next `connect()`
        # re-enables fetching) is held again, exactly like real hardware
        # between sessions.
        self._fetching_enabled = False
        self._dispatch(Event(EventType.DISCONNECTED, {"reason": reason}))

    def send_advert(self) -> bool:
        """Models `RealMeshCoreClient.send_advert()`: a flood self-
        advertisement. Called once per connection by `connect()` above
        (mirroring `_post_connect_setup`'s new per-connection advert) and,
        in production, repeated by `main.py`'s maintenance tick on `[radio]
        meshcore_advert_interval_seconds`. An advertisement is a broadcast
        the sending radio never hears back from itself, so there is
        nothing to dispatch here -- just the test-visible `adverts_sent`
        counter."""
        self.adverts_sent += 1
        return True

    def list_contacts(self, *, timeout: float = 10.0) -> list[RadioContact] | None:
        """Models `RealMeshCoreClient.list_contacts()` over the fake's store."""
        out = []
        for c in self._contacts.values():
            node_id = _meshcore_node_id(c.public_key)
            if node_id is not None:
                out.append(RadioContact(
                    c.public_key, node_id, c.adv_name, c.adv_type, c.last_advert
                ))
        return out

    def remove_contact(self, public_key: str, *, timeout: float = 10.0) -> bool:
        """Models `RealMeshCoreClient.remove_contact()`: False if unknown."""
        return self._contacts.pop(public_key, None) is not None

    def subscribe(self, event_type: EventType, callback: Any) -> None:
        self._subscribers.setdefault(event_type, []).append(callback)

    # -- test-side surface: roster management -----------------------------

    def add_contact(
        self,
        public_key: str,
        adv_name: str | None = None,
        adv_type: int = 1,
        last_advert: int | None = None,
    ) -> None:
        """Adds a contact to the FAKE's own roster with no event fired --
        for tests that want the underlying radio to know about a contact
        without `MeshCoreTransport`'s roster mirror learning about it
        (it only mirrors from `NEW_CONTACT`/`ADVERTISEMENT` events)."""
        self._contacts[public_key] = _Contact(public_key, adv_name, adv_type, last_advert)

    def emit_new_contact(
        self,
        public_key: str,
        adv_name: str,
        adv_type: int = 1,
        *,
        adv_lat: float | None = None,
        adv_lon: float | None = None,
    ) -> None:
        """Simulates a `NEW_CONTACT` refresh (0A notes section 6): adds
        the contact AND fires the event, e.g. after auto-add accepts an
        advertiser. This is how a test gives `MeshCoreTransport` a contact
        it can later `send_dm` to, or resolve a channel sender's name
        against. `adv_lat`/`adv_lon` (task 3B-2 item F) are the only place
        a MeshCore position reaches the daemon's collector -- a bare
        ADVERTISEMENT (`emit_advertisement`) carries neither a name nor a
        position."""
        self.add_contact(public_key, adv_name, adv_type)
        self._dispatch(
            Event(
                EventType.NEW_CONTACT,
                {
                    "public_key": public_key,
                    "adv_name": adv_name,
                    "adv_type": adv_type,
                    "adv_lat": adv_lat,
                    "adv_lon": adv_lon,
                },
            )
        )

    def emit_advertisement(self, public_key: str) -> None:
        """A bare `ADVERTISEMENT` push carries ONLY the advertiser's
        public key -- no name, type, or position (0A notes section 6).
        Payload key `public_key`, CONFIRMED (task 3B) directly against the
        installed `meshcore==2.3.11` package: `meshcore/reader.py:574-578`
        (`res["public_key"] = dbuf.read(32).hex()`). 3A's skeleton guessed
        `adv_key` here (borrowed from a DIFFERENT, RX-log-only event's field
        name); that was wrong and is fixed in both this fake and
        `MeshCoreTransport._on_advertisement`.
        """
        self.add_contact(public_key, adv_name=None)
        self._dispatch(Event(EventType.ADVERTISEMENT, {"public_key": public_key}))

    # -- test-side surface: inbound message injection ---------------------

    def emit_direct_message(
        self,
        pubkey_prefix: str,
        text: str,
        *,
        sender_timestamp: int = 0,
        path_len: int = 255,
        txt_type: int = 0,
    ) -> None:
        """Simulates a `CONTACT_MSG_RECV`. `pubkey_prefix` is a 6-byte
        (12 hex char) prefix and need NOT match any contact in the roster
        -- a DM from a sender the radio has never seen before is exactly
        the case 0A notes section 1 / scope-decisions.md section 8 flag as
        real: the node id is derivable from the prefix alone; only a
        REPLY needs the contact.

        HELD, not dispatched immediately, until fetching is enabled (task
        3B-4 fix 1) -- see `_deliver_or_hold`.
        """
        self._deliver_or_hold(
            Event(
                EventType.CONTACT_MSG_RECV,
                {
                    "pubkey_prefix": pubkey_prefix,
                    "text": text,
                    "sender_timestamp": sender_timestamp,
                    "path_len": path_len,
                    "txt_type": txt_type,
                },
            )
        )

    def emit_channel_message(
        self,
        channel_idx: int,
        text: str,
        *,
        sender_timestamp: int = 0,
        path_len: int = 255,
        txt_type: int = 0,
    ) -> None:
        """Simulates a `CHANNEL_MSG_RECV`. `text` is the RAW wire body,
        including any `"SenderName: "` prefix the sender's client chose
        to send -- the convention is client-side text, not a protocol
        field (0A notes section 12.1), so this fake carries it exactly as
        the real event would. Pass a name matching NO known contact (or a
        body with no colon at all) to exercise the silent-drop path
        scope-decisions.md section 7 requires.

        HELD, not dispatched immediately, until fetching is enabled (task
        3B-4 fix 1) -- see `_deliver_or_hold`.
        """
        self._deliver_or_hold(
            Event(
                EventType.CHANNEL_MSG_RECV,
                {
                    "channel_idx": channel_idx,
                    "text": text,
                    "sender_timestamp": sender_timestamp,
                    "path_len": path_len,
                    "txt_type": txt_type,
                },
            )
        )

    def emit_channel_message_from(
        self, channel_idx: int, sender_name: str, body: str, **kw: Any
    ) -> None:
        """Convenience wrapper building the `"SenderName: body"`
        convention (0A notes section 12.1) so tests don't hand-format
        it."""
        self.emit_channel_message(channel_idx, f"{sender_name}: {body}", **kw)

    def emit_ack(self, expected_ack: bytes | str) -> None:
        """Simulates the delayed `EventType.ACK` for an earlier
        `send_msg`. `code` is the HEX of `expected_ack` (0A notes section
        3) -- exactly what `MeshCoreTransport.send_dm`'s returned `tx_ref`
        already is, so a test can pass that same string straight back.
        Accepts raw bytes too, for tests building `expected_ack` by hand.
        """
        code = expected_ack.hex() if isinstance(expected_ack, (bytes, bytearray)) else expected_ack
        self._dispatch(Event(EventType.ACK, {"code": code}))

    # -- internals ----------------------------------------------------

    def _start_message_fetching(self) -> None:
        """Models `meshcore.MeshCore.start_auto_message_fetching()`
        (task 3B-4 fix 1): flips this fake into "fetching enabled" and
        flushes, in order, whatever `emit_direct_message`/`emit_channel_
        message*` held while it was disabled -- exactly the "immediately
        delivered a queued message" behavior hardware evidence showed.
        Called once per connection by `connect()`, mirroring where
        `RealMeshCoreClient` calls the real method (once per connected
        session, right after wiring subscriptions)."""
        self._fetching_enabled = True
        pending, self._pending_messages = self._pending_messages, []
        for event in pending:
            self._dispatch(event)

    def _deliver_or_hold(self, event: Event) -> None:
        """`CONTACT_MSG_RECV`/`CHANNEL_MSG_RECV` only (task 3B-4 fix 1):
        dispatched immediately once fetching is enabled, held in arrival
        order otherwise -- modeling the companion firmware's queue, which
        every OTHER event type this fake dispatches (adverts, new
        contacts, acks, connect/disconnect) is not subject to."""
        if self._fetching_enabled:
            self._dispatch(event)
        else:
            self._pending_messages.append(event)

    def _seed_stored_contacts(self) -> None:
        """One NEW_CONTACT-shaped event per contact the fake radio already
        stores -- the fake's counterpart of `RealMeshCoreClient.
        _seed_contacts`, which does the same from `MeshCore.contacts` after
        awaiting `MeshCore.ensure_contacts()`."""
        for contact in list(self._contacts.values()):
            self._dispatch(
                Event(
                    EventType.NEW_CONTACT,
                    {
                        "public_key": contact.public_key,
                        "adv_name": contact.adv_name,
                        "adv_type": contact.adv_type,
                        "_imported": True,
                        "adv_lat": None,
                        "adv_lon": None,
                    },
                )
            )

    def _dispatch(self, event: Event) -> None:
        for callback in self._subscribers.get(event.type, []):
            callback(event)


class FakeMeshCoreTransport(MeshCoreTransport):
    """A `MeshCoreTransport` wired to a `FakeMeshCore` instead of a real
    companion radio -- mirrors `tests.fakes.FakeTransport`'s role for
    Meshtastic: the real production class, running against a fake radio
    library, not a hand-rolled double with its own (potentially
    divergent) send/ack behavior. `.fake` stays reachable for injecting
    events (`fake.emit_direct_message(...)`) or reading what was sent
    (`fake.sent`).

    Connects immediately (subscribes + resolves identity), so a caller
    gets a ready-to-use transport with one call, exactly like
    `FakeTransport`.
    """

    def __init__(
        self,
        *,
        self_public_key: str = "11223344" + "00" * 28,
        expected_node_id: str | None = None,
        watched_channel_idx: int | None = 0,
        alert_channel_idx: int | None = None,
        max_text_bytes: int = 120,
        channel_name_match_case_sensitive: bool = True,
        stored_contacts: Sequence[tuple[str, str | None]] = (),
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.fake = FakeMeshCore(self_public_key=self_public_key, stored_contacts=stored_contacts)
        kwargs: dict[str, Any] = {}
        if clock is not None:
            kwargs["clock"] = clock
        # Defaults to the id this fake radio actually reports, so the
        # identity interlock is satisfied unless a test deliberately
        # mismatches it -- mirroring `FakeTransport`'s `expected_node_id`
        # defaulting to match its `my_node_num`.
        if expected_node_id is None:
            expected_node_id = "!" + self_public_key[:8].lower()
        super().__init__(
            self.fake,
            expected_node_id=expected_node_id,
            watched_channel_idx=watched_channel_idx,
            alert_channel_idx=alert_channel_idx,
            max_text_bytes=max_text_bytes,
            channel_name_match_case_sensitive=channel_name_match_case_sensitive,
            **kwargs,
        )
        self.connect()
