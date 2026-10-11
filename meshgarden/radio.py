"""Meshtastic transport adapter.

`RadioAdapter` owns the single serial connection, verifies the connected
radio's identity against config, and turns raw pubsub packets into
`IncomingPacket` records for the sink. Its public surface (start/stop/status/
my_node_num/send_text) is the transport contract, kept intentionally small so
a future MeshCore backend can be swapped in later; every `import meshtastic`
in production code lives in this module only.

Only meshgarden.sender may call RadioAdapter.send_text.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from meshtastic.serial_interface import SerialInterface
from pubsub import pub

from meshgarden.config import RadioConfig, node_id_from_num
from meshgarden.models import IncomingPacket
from meshgarden.transport import BROADCAST, ChannelScope, Inbound, Signal

log = logging.getLogger(__name__)

# Meshtastic's own broadcast node number (`0xFFFFFFFF`) -- duplicated from
# commands.py rather than imported, so this module (already the transport
# boundary's Meshtastic side) never depends on the command layer's
# vocabulary; see transport-boundary-spec.md section 0's "commands ->
# transport, never back" rule.
_BROADCAST_NUM = 0xFFFFFFFF

# Mirrors `RealMeshCoreClient`'s own `min_stable_session_seconds`
# (meshcore_client.py): only a connection that stayed up at least this long
# counts as healthy enough to reset the backoff counter. A connect-then-
# immediate-drop must keep escalating instead of retrying at the base delay
# forever.
_MIN_STABLE_SESSION_SECONDS = 30.0


def as_inbound(obs: IncomingPacket) -> Inbound:
    """The mapping table from `transport-boundary-spec.md` section 1,
    executed: exactly what a Meshtastic transport must do to produce an
    `Inbound` from an `IncomingPacket`.

    A Meshtastic sender is always resolvable -- `node_id_from_num` is a
    pure formatting function over `from_num`, never a lookup that can fail
    -- so, unlike a MeshCore contact-roster lookup, this never drops a
    message; it always returns an `Inbound`.

    `channel` is always `DIRECT`: Meshtastic channel triggering is out of
    scope for this transport (see `MeshtasticTransport`'s docstring), so the
    channel index is discarded exactly as `check_eligibility` has always
    discarded it, never promoted to `WATCHED`/`OTHER`.
    """
    hops_away = None
    if obs.hop_start is not None and obs.hop_limit is not None:
        diff = obs.hop_start - obs.hop_limit
        if diff >= 0:
            hops_away = diff
    return Inbound(
        received_at=obs.received_at,
        sender=node_id_from_num(obs.from_num),
        recipient=BROADCAST if obs.to_num == _BROADCAST_NUM else node_id_from_num(obs.to_num),
        is_text=obs.portnum == "TEXT_MESSAGE_APP",
        text=obs.text,
        raw_id=obs.packet_id,
        rx_time=obs.rx_time,
        channel=ChannelScope.DIRECT,
        signal=Signal(snr=obs.rx_snr, rssi=obs.rx_rssi),
        path_len=hops_away,
        direct=None if hops_away is None else (hops_away == 0),
        encrypted_to_us=obs.pki_encrypted,
        via_gateway=obs.via_mqtt,
    )


class SendNotReady(Exception):
    """Raised by `send_text` when connected/reply_ready/interface/identity
    cannot all be verified together under one lock acquisition; `sendText`
    is never called in that case. `Sender` catches this and marks the reply
    failed with the bounded reason `not_ready_at_send`, never retried."""


@dataclass(frozen=True)
class RadioStatus:
    connected: bool
    reply_ready: bool
    actual_node_id: str | None
    expected_node_id: str
    mismatch_reason: str | None  # bounded enum: "no_myinfo" | "node_id_mismatch" | None
    connected_since: int | None
    last_packet_at: int | None
    # Monotonic-clock twin of connected_since, for freshness/elapsed-time
    # comparisons that must never be perturbed by a wall-clock step (NTP
    # correction, DST, operator changing the system clock). Defaulted so
    # existing direct RadioStatus(...) construction sites are unaffected.
    connected_since_monotonic: float | None = None
    # Monotonic stamp of the moment the radio stopped being connected (and
    # None while it is). Lets the daemon escalate a sustained outage that
    # the worker heartbeats cannot see: the reconnect thread stays healthy
    # -- looping, beating, backing off -- while the radio itself is dead.
    # Defaulted for the same reason as the field above.
    disconnected_since_monotonic: float | None = None


def _as_str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# Fixed-point degrees -> float degrees, per Meshtastic's Position proto.
_POSITION_I_SCALE = 1e-7


def extract(packet: dict, *, now: int, now_monotonic: float) -> IncomingPacket:
    from_num = int(packet["from"])
    to_num = int(packet["to"])
    decoded = packet.get("decoded", {})
    portnum = decoded.get("portnum", "UNKNOWN_APP")

    # Display names only: read only the specific string/bool fields we need,
    # out of a NODEINFO_APP packet's user sub-message, and never keep the
    # raw dict. The public key itself is never read here -- only whether
    # the field was present.
    short_name = None
    long_name = None
    hw_model = None
    role = None
    public_key_present = None
    is_licensed = None
    if portnum == "NODEINFO_APP":
        user = decoded.get("user")
        if isinstance(user, dict):
            short_name = _as_str_or_none(user.get("shortName"))
            long_name = _as_str_or_none(user.get("longName"))
            hw_model = _as_str_or_none(user.get("hwModel"))
            role = _as_str_or_none(user.get("role"))
            public_key_present = bool(user.get("publicKey"))
            is_licensed = bool(user.get("isLicensed", False))

    # Structured position only: no text body, ever. latitudeI/longitudeI
    # absent (proto3 omits an exact 0) means "no position", never 0.0.
    lat = None
    lon = None
    position_alt = None
    position_precision_bits = None
    position_time = None
    if portnum == "POSITION_APP":
        position = decoded.get("position")
        if isinstance(position, dict):
            lat_i = _as_int_or_none(position.get("latitudeI"))
            lon_i = _as_int_or_none(position.get("longitudeI"))
            lat = lat_i * _POSITION_I_SCALE if lat_i is not None else None
            lon = lon_i * _POSITION_I_SCALE if lon_i is not None else None
            position_alt = _as_int_or_none(position.get("altitude"))
            position_precision_bits = _as_int_or_none(position.get("precisionBits"))
            position_time = _as_int_or_none(position.get("time"))

    # Structured device metrics only, from our own or a neighbour's
    # TELEMETRY_APP report -- never the packet's other telemetry variants
    # (environment/power/air-quality metrics are out of scope).
    battery_level = None
    voltage = None
    channel_utilization = None
    air_util_tx = None
    uptime_seconds = None
    if portnum == "TELEMETRY_APP":
        telemetry = decoded.get("telemetry")
        if isinstance(telemetry, dict):
            device_metrics = telemetry.get("deviceMetrics")
            if isinstance(device_metrics, dict):
                battery_level = _as_int_or_none(device_metrics.get("batteryLevel"))
                voltage = _as_float_or_none(device_metrics.get("voltage"))
                channel_utilization = _as_float_or_none(device_metrics.get("channelUtilization"))
                air_util_tx = _as_float_or_none(device_metrics.get("airUtilTx"))
                uptime_seconds = _as_int_or_none(device_metrics.get("uptimeSeconds"))

    # The Meshtastic library builds this dict with protobuf's MessageToDict,
    # which OMITS a field entirely when it's at its default value (False/0)
    # -- so `viaMqtt`/`hopStart`/`hopLimit` are only present in the dict when
    # truthy. A decoded packet is always RF unless explicitly flagged
    # MQTT-relayed, so an absent `viaMqtt` means False, never "unknown"; the
    # raw protobuf (present as `packet["raw"]` on a real radio packet, per
    # mesh_interface.py) carries the true value even when it's the default,
    # so prefer it when available, for all three fields.
    raw = packet.get("raw")
    via_mqtt = (
        bool(raw.via_mqtt)
        if raw is not None and hasattr(raw, "via_mqtt")
        else bool(packet.get("viaMqtt", False))
    )
    hop_start = (
        raw.hop_start
        if raw is not None and hasattr(raw, "hop_start")
        else packet.get("hopStart")
    )
    hop_limit = (
        raw.hop_limit
        if raw is not None and hasattr(raw, "hop_limit")
        else packet.get("hopLimit")
    )

    return IncomingPacket(
        received_at=now,
        received_monotonic=now_monotonic,
        from_num=from_num,
        to_num=to_num,
        packet_id=packet.get("id"),
        channel=int(packet.get("channel", 0)),
        portnum=portnum,
        rx_time=packet.get("rxTime"),
        rx_snr=packet.get("rxSnr"),
        rx_rssi=packet.get("rxRssi"),
        hop_limit=hop_limit,
        hop_start=hop_start,
        via_mqtt=via_mqtt,
        pki_encrypted=bool(packet.get("pkiEncrypted", False)),
        payload_len=len(decoded.get("payload", b"")),
        text=decoded.get("text"),
        request_id=decoded.get("requestId"),
        routing_error=decoded.get("routing", {}).get("errorReason"),
        short_name=short_name,
        long_name=long_name,
        hw_model=hw_model,
        role=role,
        public_key_present=public_key_present,
        is_licensed=is_licensed,
        lat=lat,
        lon=lon,
        position_alt=position_alt,
        position_precision_bits=position_precision_bits,
        position_time=position_time,
        battery_level=battery_level,
        voltage=voltage,
        channel_utilization=channel_utilization,
        air_util_tx=air_util_tx,
        uptime_seconds=uptime_seconds,
    )


def backoff_delays(base: float, maximum: float, n: int) -> list[float]:
    # Cap the EXPONENT, not just the final result: `base * 2**i` first
    # computes `2**i` as an exact (arbitrarily large) Python int, then
    # converts it to float for the multiplication -- which raises
    # OverflowError once `2**i` exceeds what a float can hold (around
    # i=1024), long before any realistic attempt count. `min(maximum, ...)`
    # caps the result anyway, so once 2**i has already blown past any
    # plausible `maximum`, further growth is pointless: capping i at 20
    # (2**20 is over a million times `base`) keeps the computation itself
    # overflow-safe for arbitrarily large `n` (a reconnect loop that has
    # been retrying for a very long time) while never changing a single
    # returned value for any realistic base/maximum pair.
    return [min(maximum, base * 2 ** min(i, 20)) for i in range(n)]


class RadioAdapter:
    def __init__(
        self,
        cfg: RadioConfig,
        *,
        sink: Callable[[IncomingPacket], None],
        interface_factory: Callable[[str], Any] | None = None,
        backoff_base: float = 1.0,
        backoff_max: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        on_reconnect: Callable[[], None] | None = None,
        on_malformed: Callable[[], None] | None = None,
        on_heartbeat: Callable[[], None] | None = None,
        stop_join_timeout: float = 2.0,
    ) -> None:
        self._cfg = cfg
        self._sink = sink
        self._interface_factory = interface_factory or (
            lambda dev: SerialInterface(devPath=dev)
        )
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._clock = clock
        # Metrics wiring only -- radio.py stays decoupled from metrics.py;
        # the daemon supplies these to bump its own counters.
        self._on_reconnect = on_reconnect
        self._on_malformed = on_malformed
        # Worker-health wiring (see main.py's Heartbeats): called on every
        # pass through `_run`'s outer loop (each connect/reconnect attempt,
        # including the backoff wait) and on every tick of the inner
        # connected-poll loop, so a heartbeat lapse can only mean this
        # thread has truly stopped iterating, never that it merely lacked
        # a reason to call in on a given pass.
        self._on_heartbeat = on_heartbeat
        self._stop_join_timeout = stop_join_timeout

        self._interface: Any | None = None
        self._connected = False
        self._reply_ready = False
        self._actual_node_id: str | None = None
        self._mismatch_reason: str | None = None
        self._connected_since: int | None = None
        self._connected_since_monotonic: float | None = None
        self._last_packet_at: int | None = None
        self._malformed = 0
        self._ever_connected = False
        # Stamped at construction, not at first failure: a daemon that never
        # reaches the radio at all must escalate on the same clock as one
        # that lost it later.
        self._disconnected_since_monotonic: float | None = self._clock()

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._ready_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._subscribed = False
        self._stopped = False

    def start(self) -> None:
        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_established, "meshtastic.connection.established")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        self._subscribed = True
        self._thread = threading.Thread(target=self._run, name="radio-adapter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._subscribed:
            pub.unsubscribe(self._on_receive, "meshtastic.receive")
            pub.unsubscribe(self._on_established, "meshtastic.connection.established")
            pub.unsubscribe(self._on_lost, "meshtastic.connection.lost")
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._stop_join_timeout)
            if self._thread.is_alive():
                log.warning(
                    "radio adapter thread did not stop within %.1fs", self._stop_join_timeout
                )
        with self._lock:
            iface = self._interface
            self._interface = None
        if iface is not None:
            iface.close()

    def rebind_hooks(
        self,
        *,
        sink: Callable[[IncomingPacket], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
        on_malformed: Callable[[], None] | None = None,
        on_heartbeat: Callable[[], None] | None = None,
    ) -> None:
        """TEST/DAEMON-WIRING SUPPORT (task 2B): replaces callbacks after
        construction, each only when the corresponding argument is not
        None. Needed when this adapter is built (and even started) before
        the object that will supply its real callbacks exists yet -- e.g.
        `tests.fakes.FakeTransport`, constructed by a test fixture before
        the `Daemon` that owns the ingest sink/metrics/heartbeats it wants
        wired in. Safe to call while the reconnect thread is already
        running: every hook this replaces is read fresh on each use and
        guarded by `is not None` before being called, so a hook fired in
        the narrow window before a rebind is a silent no-op, never a crash
        on an attribute that doesn't exist yet."""
        if sink is not None:
            self._sink = sink
        if on_reconnect is not None:
            self._on_reconnect = on_reconnect
        if on_malformed is not None:
            self._on_malformed = on_malformed
        if on_heartbeat is not None:
            self._on_heartbeat = on_heartbeat

    def status(self) -> RadioStatus:
        with self._lock:
            return RadioStatus(
                connected=self._connected,
                reply_ready=self._reply_ready,
                actual_node_id=self._actual_node_id,
                expected_node_id=self._cfg.expected_node_id,
                mismatch_reason=self._mismatch_reason,
                connected_since=self._connected_since,
                last_packet_at=self._last_packet_at,
                connected_since_monotonic=self._connected_since_monotonic,
                disconnected_since_monotonic=self._disconnected_since_monotonic,
            )

    @property
    def my_node_num(self) -> int | None:
        with self._lock:
            iface = self._interface
        mi = getattr(iface, "myInfo", None) if iface is not None else None
        return mi.my_node_num if mi is not None else None

    def send_text(self, *, text: str, destination_id: str) -> int | None:
        """LOW-LEVEL. Only sender.py may call this. Returns the outbound packet id.

        Reads interface/connected/reply_ready/identity together under a
        single lock acquisition -- the authoritative check -- so a reconnect
        landing between Sender's own (cheaper, earlier) pre-check and this
        call can never hand `sendText` an interface belonging to a
        different, unverified radio. Raises `SendNotReady` (without ever
        calling `sendText`) when they don't all hold; the lock is released
        before the actual radio call.
        """
        with self._lock:
            if not (
                self._connected
                and self._reply_ready
                and self._interface is not None
                and self._actual_node_id == self._cfg.expected_node_id
            ):
                raise SendNotReady()
            iface = self._interface
        result = iface.sendText(
            text=text,
            destinationId=destination_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        return getattr(result, "id", None)

    def wait_ready(self, timeout: float) -> bool:
        return self._ready_event.wait(timeout)

    @property
    def backoff_max(self) -> float:
        """The configured reconnect backoff cap, in seconds -- read by the
        daemon to size the radio worker's own heartbeat-stall threshold
        (`max(backoff_max, 60s)`) without duplicating this constant."""
        return self._backoff_max

    def _beat(self) -> None:
        if self._on_heartbeat is not None:
            self._on_heartbeat()

    # -- pubsub callbacks: never block, never call close(), never log packet contents --

    def _on_receive(self, packet=None, interface=None) -> None:
        try:
            now = int(time.time())
            obs = extract(packet, now=now, now_monotonic=self._clock())
            with self._lock:
                self._last_packet_at = now
            self._sink(obs)
        except Exception:
            self._malformed += 1
            if self._on_malformed is not None:
                self._on_malformed()
            log.warning("malformed packet dropped")

    def _on_established(self, interface=None) -> None:
        log.info("radio connection established")

    def _on_lost(self, interface=None) -> None:
        with self._lock:
            self._connected = False
            self._reply_ready = False
            # Only stamp the FIRST transition: `_run` marks itself
            # disconnected too, and an outage's age must be measured from
            # when it began, not from the most recent retry.
            if self._disconnected_since_monotonic is None:
                self._disconnected_since_monotonic = self._clock()
        self._ready_event.clear()
        log.warning("radio connection lost")
        self._wake_event.set()

    # -- connect/reconnect loop (daemon thread) --

    def _check_identity(self, iface: Any) -> None:
        mi = getattr(iface, "myInfo", None)
        if mi is None:
            with self._lock:
                self._actual_node_id = None
                self._reply_ready = False
                self._mismatch_reason = "no_myinfo"
            log.info("radio connected; myInfo not yet available")
            return
        actual = node_id_from_num(mi.my_node_num)
        if actual != self._cfg.expected_node_id:
            with self._lock:
                self._actual_node_id = actual
                self._reply_ready = False
                self._mismatch_reason = "node_id_mismatch"
            log.error("radio identity mismatch; replies paused")
            log.info(
                "radio identity check: actual=%s expected=%s", actual, self._cfg.expected_node_id
            )
        else:
            with self._lock:
                self._actual_node_id = actual
                self._reply_ready = True
                self._mismatch_reason = None
            log.info("radio identity verified: %s", actual)

    # Backoff waits are chunked at this interval so `_beat()` keeps firing
    # while the reconnect loop is merely waiting. A single
    # `_stop_event.wait(delay)` spanning the whole backoff let the radio
    # worker's heartbeat age past its own stall threshold -- which main.py
    # sizes from `backoff_max`, so at the 300s default the two are equal and
    # every cycle crossed it -- making the worker flap stalled/recovered.
    # That flapping actively SUPPRESSED systemd's WatchdogSec (which only
    # stops being pinged while a worker is stalled) instead of tripping it.
    _BACKOFF_BEAT_INTERVAL = 0.5

    def _wait_backoff(self, delay: float) -> bool:
        """Waits up to `delay` seconds for stop, beating throughout.

        Returns True if stop was signaled (the caller must break out of the
        reconnect loop), False if the full delay elapsed.
        """
        deadline = self._clock() + delay
        while True:
            self._beat()
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False
            if self._stop_event.wait(min(self._BACKOFF_BEAT_INTERVAL, remaining)):
                return True

    def _sync_clock(self, iface: Any) -> None:
        """Sets the radio's clock from the host's, on every verified connect.

        The ESP32 has no battery-backed RTC, so after any reboot its clock
        is wrong -- and a skew larger than `reply_ttl_seconds` makes
        `commands.check_eligibility` reject every incoming DM as a replayed
        packet: observed and recorded, but never answered. Connect time is
        exactly when this needs doing, because a reconnect usually means
        the radio just rebooted.

        Only for a radio whose identity we verified -- never write to one
        that is not ours. Best effort: a refused admin write must not cost
        us a connection that is otherwise fine for observing and replying.
        This is a local USB admin write, not a LoRa transmission.
        """
        with self._lock:
            if not self._reply_ready:
                return
        node = getattr(iface, "localNode", None)
        set_time = getattr(node, "setTime", None)
        if set_time is None:
            return
        try:
            set_time(0)  # 0 = "use the host's current time"
        except Exception as exc:
            log.warning("could not set the radio clock: %s: %s", type(exc).__name__, exc)
            return
        log.info("radio clock set from host time")

    def _next_backoff_delay(self, attempt: int) -> float:
        """Defense in depth alongside `backoff_delays`'s own overflow fix:
        any unexpected exception computing the delay must never escape the
        reconnect loop and kill this thread (silently, with the daemon
        otherwise still running) -- fall back to the configured maximum
        instead."""
        try:
            return backoff_delays(self._backoff_base, self._backoff_max, attempt)[-1]
        except Exception:
            log.warning("backoff delay computation failed; using backoff_max")
            return self._backoff_max

    def _run(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            self._beat()
            # Clear the wake signal BEFORE creating the interface, while nothing
            # is connected yet: no `connection.lost` can legitimately fire before
            # an interface exists, so this clear can never discard a real signal.
            # From this point on the event is only ever consumed (via wait()
            # below), never cleared again out from under a concurrent set() --
            # that ordering is what let a lost event fired during the identity
            # check (or any time before we reach wait()) go missing and hang the
            # reconnect loop forever.
            self._wake_event.clear()
            if self._stop_event.is_set():
                # stop() landed between the loop's stop-check above and this
                # clear(): it already set (and we just discarded) the wake it
                # left behind. Re-check here, immediately, or we would open a
                # NEW connection during shutdown and then park forever on the
                # wait below, since nothing will ever wake it again.
                break
            try:
                iface = self._interface_factory(self._cfg.device)
            except Exception as exc:
                # The reason matters: a missing device node, a permissions
                # problem, and a radio that enumerates but never answers are
                # indistinguishable without it, and this loop is the only
                # place that ever sees the exception. Type + message only,
                # never a traceback -- this can repeat for hours.
                log.warning(
                    "radio connect failed; will retry: %s: %s", type(exc).__name__, exc
                )
                with self._lock:
                    if self._disconnected_since_monotonic is None:
                        self._disconnected_since_monotonic = self._clock()
                attempt += 1
                delay = self._next_backoff_delay(attempt)
                if self._wait_backoff(delay):
                    break
                continue

            is_reconnect = self._ever_connected
            with self._lock:
                self._interface = iface
                self._connected = True
                self._connected_since = int(time.time())
                self._connected_since_monotonic = self._clock()
                self._disconnected_since_monotonic = None
            self._ever_connected = True
            if is_reconnect and self._on_reconnect is not None:
                self._on_reconnect()
            self._check_identity(iface)
            self._sync_clock(iface)
            self._ready_event.set()

            # Poll with a bounded timeout, re-checking _stop_event on every
            # wake and every timeout, rather than an unconditional wait():
            # a stop() that lands here must never be able to leave this
            # thread parked forever with nothing left to wake it.
            while not self._wake_event.wait(timeout=0.5):
                self._beat()
                if self._stop_event.is_set():
                    return
            if self._stop_event.is_set():
                break

            # connection lost: mark disconnected, close the superseded interface
            # (never from a callback), back off, then reconnect and re-verify
            # identity from scratch. The replaced radio is never adopted.
            with self._lock:
                # Only a session that was both identity-verified and up for
                # a MEANINGFUL DURATION counts as healthy and resets the
                # backoff to a fresh first retry -- a connect that drops
                # within `_MIN_STABLE_SESSION_SECONDS` (or never verified)
                # keeps escalating like any other repeated failure instead
                # of retrying at the base delay forever.
                stable_session = (
                    self._reply_ready
                    and self._connected_since_monotonic is not None
                    and self._clock() - self._connected_since_monotonic
                    >= _MIN_STABLE_SESSION_SECONDS
                )
                self._connected = False
                self._reply_ready = False
                self._interface = None
                if self._disconnected_since_monotonic is None:
                    self._disconnected_since_monotonic = self._clock()
            self._ready_event.clear()
            try:
                iface.close()
            except Exception:
                log.warning("error closing superseded radio interface")

            if stable_session:
                attempt = 0
            attempt += 1
            delay = self._next_backoff_delay(attempt)
            if self._wait_backoff(delay):
                break
