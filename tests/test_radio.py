import logging
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshgarden.config import RadioConfig
from meshgarden.radio import RadioAdapter, backoff_delays, extract
from tests.fakes import FakeInterface, make_packet


def test_absent_channel_becomes_zero_not_none():
    p = make_packet(channel=None)
    assert "channel" not in p
    assert extract(p, now=100, now_monotonic=1.0).channel == 0


def test_missing_signal_metrics_are_none_not_zero():
    o = extract(make_packet(), now=100, now_monotonic=1.0)
    assert o.rx_snr is None and o.rx_rssi is None and o.hop_limit is None


def test_via_mqtt_absent_is_rf_not_unknown():
    # MessageToDict omits viaMqtt when False (proto3's omit-default rule), so
    # a decoded packet that never sets the dict key is RF, not "unknown
    # origin" -- via_mqtt=None is reserved for legacy rows recorded before
    # this fix (or synthetic observations), never emitted by extract().
    assert extract(make_packet(), now=100, now_monotonic=1.0).via_mqtt is False
    assert extract(make_packet(via_mqtt=True), now=100, now_monotonic=1.0).via_mqtt is True


def test_raw_via_mqtt_wins_over_absent_dict_key():
    p = make_packet()
    assert "viaMqtt" not in p
    p["raw"] = SimpleNamespace(via_mqtt=True)
    assert extract(p, now=100, now_monotonic=1.0).via_mqtt is True


def test_raw_hop_start_and_hop_limit_zero_are_read_not_omitted():
    # Same MessageToDict omit-default rule applies to hopStart/hopLimit: a
    # real hop_start=0 (packet sent with no relay budget) is indistinguishable
    # in the dict from "never set". The raw protobuf always carries the true
    # value, so prefer it when available.
    p = make_packet()
    assert "hopStart" not in p and "hopLimit" not in p
    p["raw"] = SimpleNamespace(hop_start=0, hop_limit=0, via_mqtt=False)
    o = extract(p, now=100, now_monotonic=1.0)
    assert o.hop_start == 0
    assert o.hop_limit == 0


def test_text_is_extracted_but_payload_len_recorded():
    o = extract(make_packet(text="SIGN hi"), now=100, now_monotonic=1.0)
    assert o.text == "SIGN hi"
    assert o.payload_len == len(b"SIGN hi")


def test_packet_without_id_yields_none_packet_id():
    o = extract(make_packet(packet_id=None), now=100, now_monotonic=1.0)
    assert o.packet_id is None


def test_routing_ack_fields_extracted():
    o = extract(
        make_packet(portnum="ROUTING_APP", text=None, request_id=77, routing_error="NONE"),
        now=1,
        now_monotonic=1.0,
    )
    assert o.request_id == 77 and o.routing_error == "NONE"


def test_identity_match_enables_replies():
    got = []
    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=got.append,
        interface_factory=lambda dev: FakeInterface(0x11223344),
    )
    a.start()
    a.wait_ready(timeout=2)
    s = a.status()
    assert s.connected and s.reply_ready
    assert s.actual_node_id == "!11223344" and s.mismatch_reason is None
    a.stop()


def test_identity_mismatch_pauses_replies_but_keeps_observing():
    got = []
    iface = FakeInterface(0xDEADBEEF)
    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=got.append,
        interface_factory=lambda dev: iface,
    )
    a.start()
    a.wait_ready(timeout=2)
    s = a.status()
    assert s.connected is True
    assert s.reply_ready is False
    assert s.mismatch_reason == "node_id_mismatch"
    iface.emit_packet(make_packet(to_num=0xDEADBEEF, text="HELP"))
    assert len(got) == 1  # observation continues
    a.stop()


def test_adapter_never_rewrites_configured_identity():
    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=lambda p: None,
        interface_factory=lambda dev: FakeInterface(0xDEADBEEF),
    )
    a.start()
    a.wait_ready(timeout=2)
    assert a.status().expected_node_id == "!11223344"
    a.stop()


def test_backoff_is_exponential_and_capped():
    d = backoff_delays(1.0, 300.0, 12)
    assert d[0] == 1.0 and d[1] == 2.0 and d[2] == 4.0
    assert all(x <= 300.0 for x in d) and d[-1] == 300.0
    assert d == sorted(d)


def test_backoff_survives_large_attempt_counts_without_overflow():
    """Regression test: `base * 2**i` computes `2**i` as an exact
    (arbitrarily large) Python int before converting to float for the
    multiplication, which raises OverflowError once `2**i` exceeds what a
    float can hold -- around i=1024, so n=1100 used to blow up here even
    though every value past the first few is capped to `maximum` anyway.
    A long-running reconnect loop reaches attempt counts this large simply
    by never reconnecting for a long time."""
    d = backoff_delays(1.0, 300.0, 1100)
    assert len(d) == 1100
    assert all(x == 300.0 for x in d[10:])  # well past the point base*2**i > maximum
    assert d[-1] == 300.0


def test_reconnect_rechecks_identity(monkeypatch):
    ifaces = [FakeInterface(0x11223344), FakeInterface(0xDEADBEEF)]
    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=lambda p: None,
        interface_factory=lambda dev: ifaces.pop(0),
        backoff_base=0.01,
    )
    a.start()
    a.wait_ready(timeout=2)
    assert a.status().reply_ready is True
    first_iface = a._interface
    first_iface.emit_lost()
    a.wait_ready(timeout=2)
    s = a.status()
    assert s.connected is True  # the SECOND interface is actually connected...
    assert s.reply_ready is False  # ...but the replaced radio is NOT adopted
    assert ifaces == []  # both fakes were consumed: reconnect actually happened
    assert first_iface.closed is True  # superseded interface was closed, not leaked
    a.stop()


def test_backoff_keeps_escalating_after_immediate_drop_not_stable_session(monkeypatch):
    """Regression: `attempt` used to reset to 0 unconditionally right after
    every identity-verified connect (before the fix), so a radio that
    connects and drops within a second reconnected at the base delay
    forever. The reset must only happen when the previous session was
    stable for at least `_MIN_STABLE_SESSION_SECONDS` -- a connect-then-
    immediate-drop, twice in a row, must produce a STRICTLY larger second
    backoff delay than the first."""
    ifaces = [FakeInterface(0x11223344) for _ in range(3)]
    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=lambda p: None,
        interface_factory=lambda dev: ifaces.pop(0),
        backoff_base=0.01,
        backoff_max=10.0,
    )
    delays: list[float] = []

    def capturing_wait_backoff(delay):
        delays.append(delay)
        return False  # never stop; proceed to the next attempt immediately

    monkeypatch.setattr(a, "_wait_backoff", capturing_wait_backoff)
    a.start()
    a.wait_ready(timeout=2)
    a._interface.emit_lost()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and len(delays) < 1:
        time.sleep(0.01)
    assert len(delays) >= 1
    first_delay = delays[0]

    a.wait_ready(timeout=2)  # second interface connected + identity-verified
    a._interface.emit_lost()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and len(delays) < 2:
        time.sleep(0.01)
    assert len(delays) >= 2
    second_delay = delays[1]

    assert second_delay > first_delay
    assert ifaces == []  # all three fakes consumed: reconnect actually happened twice
    a.stop()


def test_reconnect_survives_lost_event_during_identity_check(monkeypatch):
    """Regression for a race where connection.lost firing inside the identity-check
    window (before the adapter reaches its wait-for-next-event point) used to be
    wiped by an unconditional wake-event clear placed after the check, hanging the
    reconnect loop forever. A lost signal that arrives at any point after the
    interface is created must still be observed and trigger a reconnect."""
    ifaces = [FakeInterface(0x11223344), FakeInterface(0x11223344)]
    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=lambda p: None,
        interface_factory=lambda dev: ifaces.pop(0),
        backoff_base=0.01,
    )
    original_check = a._check_identity
    triggered = []

    def racing_check(iface):
        original_check(iface)
        if not triggered:
            triggered.append(True)
            # Simulate connection.lost firing during the identity check itself,
            # i.e. before _run() reaches the point where it waits for the next
            # lost/stop signal.
            iface.emit_lost()

    monkeypatch.setattr(a, "_check_identity", racing_check)
    a.start()
    a.wait_ready(timeout=2)

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not a.status().connected:
        time.sleep(0.01)

    assert a.status().connected is True  # reconnect completed, did not hang
    assert ifaces == []  # both fakes were consumed
    a.stop()


def test_stop_does_not_hang_when_stop_lands_between_stop_check_and_wake_clear(monkeypatch):
    """Regression: `_run` re-checks `while not self._stop_event.is_set()`,
    then clears the wake event. If stop() lands in that narrow window, it
    sets both events and the clear() discards the wake it just set --
    without the fix, `_run` would open a NEW connection during shutdown and
    then park forever on an unconditional wait, so stop()'s join() never
    returns. Simulated with the same monkeypatch-hook technique as
    test_reconnect_survives_lost_event_during_identity_check: the hook pauses
    `_run` exactly inside the clear() call, a real stop() (on another
    thread) is let land there, and then the clear() is allowed to proceed --
    reproducing the exact reported race deterministically."""
    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=lambda p: None,
        interface_factory=lambda dev: FakeInterface(0x11223344),
        backoff_base=0.01,
    )
    real_clear = a._wake_event.clear
    reached_clear = threading.Event()
    release_clear = threading.Event()

    def paused_clear():
        reached_clear.set()
        release_clear.wait(2)
        real_clear()

    monkeypatch.setattr(a._wake_event, "clear", paused_clear)
    a.start()
    assert reached_clear.wait(2)  # _run is now paused inside the clear() call

    # Deterministic handshake for "stop() has set both events and is about
    # to block on join()": stop() calls _stop_event.set() then
    # _wake_event.set(), in that order, with nothing blocking in between --
    # so hooking _wake_event.set() (the same instance-attribute-shadowing
    # technique used for `clear` above) marks that exact point, with no
    # sleep-based guessing about timing.
    real_wake_set = a._wake_event.set
    stop_landed = threading.Event()

    def signaling_set():
        real_wake_set()
        stop_landed.set()

    monkeypatch.setattr(a._wake_event, "set", signaling_set)

    result: dict = {}

    def call_stop():
        start = time.monotonic()
        a.stop()
        result["elapsed"] = time.monotonic() - start

    t = threading.Thread(target=call_stop)
    t.start()
    assert stop_landed.wait(2)  # stop() has set both events; about to join()
    release_clear.set()  # now let _run's clear() run, discarding the wake stop() just set
    t.join(timeout=5)

    assert "elapsed" in result and result["elapsed"] < 2.0


def test_malformed_packet_does_not_raise():
    got = []
    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=got.append,
        interface_factory=lambda dev: FakeInterface(0x11223344),
    )
    a.start()
    a.wait_ready(timeout=2)
    a._interface.emit_packet({"garbage": True})  # no from/to/decoded
    a._interface.emit_packet({"from": "not-an-int", "to": 1, "id": 2})
    assert got == []  # dropped, nothing raised
    a.stop()


def test_nodeinfo_extracts_short_and_long_name():
    packet = make_packet(
        portnum="NODEINFO_APP",
        text=None,
        user={"shortName": "Ali", "longName": "Alice's Node"},
    )
    o = extract(packet, now=100, now_monotonic=1.0)
    assert o.short_name == "Ali"
    assert o.long_name == "Alice's Node"


def test_nodeinfo_ignores_non_string_name_fields_and_extra_payload():
    packet = make_packet(
        portnum="NODEINFO_APP",
        text=None,
        user={"shortName": 123, "longName": None, "macaddr": "unused", "hwModel": "unused"},
    )
    o = extract(packet, now=100, now_monotonic=1.0)
    assert o.short_name is None
    assert o.long_name is None


def test_text_packet_never_carries_names():
    o = extract(make_packet(text="HELP"), now=100, now_monotonic=1.0)
    assert o.short_name is None and o.long_name is None


# --- NODEINFO hw_model/role/publicKey/isLicensed ----------------------------


def test_nodeinfo_extracts_hw_model_role_and_key_presence_flag():
    packet = make_packet(
        portnum="NODEINFO_APP",
        text=None,
        user={
            "shortName": "Ali",
            "longName": "Alice's Node",
            "hwModel": "TBEAM",
            "role": "CLIENT",
            "publicKey": "c2VjcmV0a2V5",  # base64 stand-in; must never surface anywhere
            "isLicensed": True,
        },
    )
    o = extract(packet, now=100, now_monotonic=1.0)
    assert o.hw_model == "TBEAM"
    assert o.role == "CLIENT"
    assert o.public_key_present is True
    assert o.is_licensed is True
    # The key material itself is never kept on IncomingPacket anywhere.
    assert "c2VjcmV0a2V5" not in repr(o)


def test_nodeinfo_missing_public_key_and_is_licensed_default_absent():
    packet = make_packet(
        portnum="NODEINFO_APP", text=None, user={"shortName": "Ali", "longName": "G"}
    )
    o = extract(packet, now=100, now_monotonic=1.0)
    assert o.public_key_present is False
    assert o.is_licensed is False
    assert o.hw_model is None
    assert o.role is None


def test_non_nodeinfo_packet_never_carries_hw_role_or_key_flag():
    o = extract(make_packet(text="HELP"), now=100, now_monotonic=1.0)
    assert o.hw_model is None and o.role is None
    assert o.public_key_present is None and o.is_licensed is None


# --- POSITION_APP ------------------------------------------------------------


def test_position_lat_lon_converted_from_fixed_point():
    packet = make_packet(
        portnum="POSITION_APP",
        text=None,
        position={
            "latitudeI": 377749000,
            "longitudeI": -1224194000,
            "altitude": 15,
            "precisionBits": 16,
            "time": 1700000000,
        },
    )
    o = extract(packet, now=100, now_monotonic=1.0)
    assert o.lat == pytest.approx(37.7749)
    assert o.lon == pytest.approx(-122.4194)
    assert o.position_alt == 15
    assert o.position_precision_bits == 16
    assert o.position_time == 1700000000


def test_position_absent_fields_are_none_not_zero():
    packet = make_packet(portnum="POSITION_APP", text=None, position={})
    o = extract(packet, now=100, now_monotonic=1.0)
    assert o.lat is None and o.lon is None
    assert o.position_alt is None
    assert o.position_precision_bits is None
    assert o.position_time is None


def test_non_position_packet_never_carries_lat_lon():
    o = extract(make_packet(text="HELP"), now=100, now_monotonic=1.0)
    assert o.lat is None and o.lon is None


# --- TELEMETRY_APP deviceMetrics ---------------------------------------------


def test_telemetry_extracts_device_metrics():
    packet = make_packet(
        portnum="TELEMETRY_APP",
        text=None,
        telemetry={
            "deviceMetrics": {
                "batteryLevel": 87,
                "voltage": 4.01,
                "channelUtilization": 12.5,
                "airUtilTx": 3.2,
                "uptimeSeconds": 123456,
            }
        },
    )
    o = extract(packet, now=100, now_monotonic=1.0)
    assert o.battery_level == 87
    assert o.voltage == pytest.approx(4.01)
    assert o.channel_utilization == pytest.approx(12.5)
    assert o.air_util_tx == pytest.approx(3.2)
    assert o.uptime_seconds == 123456


def test_telemetry_missing_device_metrics_is_none_not_zero():
    packet = make_packet(portnum="TELEMETRY_APP", text=None, telemetry={})
    o = extract(packet, now=100, now_monotonic=1.0)
    assert o.battery_level is None
    assert o.voltage is None
    assert o.channel_utilization is None
    assert o.air_util_tx is None
    assert o.uptime_seconds is None


def test_non_telemetry_packet_never_carries_device_metrics():
    o = extract(make_packet(text="HELP"), now=100, now_monotonic=1.0)
    assert o.battery_level is None and o.voltage is None


def test_status_exposes_a_monotonic_connected_since():
    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=lambda p: None,
        interface_factory=lambda dev: FakeInterface(0x11223344),
    )
    a.start()
    a.wait_ready(timeout=2)
    s = a.status()
    assert s.connected_since_monotonic is not None
    a.stop()


def test_only_radio_py_imports_meshtastic():
    """Transport containment: meshtastic must be imported only from radio.py."""
    package_dir = Path(__file__).resolve().parent.parent / "meshgarden"
    pattern = re.compile(r"^\s*(import meshtastic\b|from meshtastic\b)")
    offenders = []
    for path in sorted(package_dir.glob("*.py")):
        if path.name == "radio.py":
            continue
        for line in path.read_text().splitlines():
            if pattern.match(line):
                offenders.append(f"{path.name}: {line.strip()}")
    assert offenders == [], f"unexpected meshtastic import(s): {offenders}"


def test_connect_failure_logs_the_underlying_reason(caplog):
    """Regression: the reconnect loop used to swallow the exception entirely
    and log a bare "will retry", leaving an operator with no way to tell a
    missing device from a wedged one from a permissions problem -- an
    85-minute outage produced no diagnostic trail at all."""
    failed = threading.Event()

    def boom(dev):
        failed.set()
        raise OSError(5, "Input/output error")

    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=lambda p: None,
        interface_factory=boom,
        backoff_base=0.01,
        backoff_max=0.01,
    )
    with caplog.at_level(logging.WARNING, logger="meshgarden.radio"):
        a.start()
        assert failed.wait(2)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not any(
            "will retry" in r.getMessage() for r in caplog.records
        ):
            time.sleep(0.02)
        a.stop()
    retries = [r.getMessage() for r in caplog.records if "will retry" in r.getMessage()]
    assert retries, "no connect-failure warning was logged at all"
    assert any("Input/output error" in m for m in retries), retries
    assert any("OSError" in m for m in retries), retries


def test_heartbeat_fires_during_the_reconnect_backoff_wait():
    """The constructor documents on_heartbeat as firing on every pass through
    `_run`'s outer loop "including the backoff wait". It did not: the whole
    backoff slept inside a single `_stop_event.wait(delay)`, so with
    backoff_max at its 300s default the radio worker aged past its own
    (equal) stall threshold every cycle and flapped stalled -> recovered,
    which kept systemd's WatchdogSec from ever firing."""
    beats: list[float] = []
    failed = threading.Event()

    def boom(dev):
        failed.set()
        raise OSError("device not answering")

    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=lambda p: None,
        interface_factory=boom,
        backoff_base=3.0,
        backoff_max=3.0,
        on_heartbeat=lambda: beats.append(time.monotonic()),
    )
    a.start()
    assert failed.wait(2)
    before = len(beats)
    # We are now inside a 3s backoff wait. Without the fix no beat lands here.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and len(beats) <= before:
        time.sleep(0.05)
    a.stop()
    assert len(beats) > before, "no heartbeat during the backoff wait"


def test_status_tracks_how_long_the_radio_has_been_disconnected():
    """A dead radio must be measurable, not merely reported as
    `connected: False`: the daemon escalates on this age."""
    failed = threading.Event()

    def boom(dev):
        failed.set()
        raise OSError("device not answering")

    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=lambda p: None,
        interface_factory=boom,
        backoff_base=0.01,
        backoff_max=0.01,
    )
    assert a.status().disconnected_since_monotonic is not None  # never connected
    a.start()
    assert failed.wait(2)
    assert a.status().disconnected_since_monotonic is not None
    a.stop()


def test_disconnected_since_clears_on_a_verified_connect():
    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=lambda p: None,
        interface_factory=lambda dev: FakeInterface(0x11223344),
    )
    a.start()
    a.wait_ready(timeout=2)
    assert a.status().disconnected_since_monotonic is None
    a.stop()


def test_verified_connect_sets_the_radio_clock():
    """The ESP32 has no battery-backed RTC, so after any reboot its clock is
    wrong. A skew beyond `reply_ttl_seconds` makes commands.py reject every
    DM as a replayed packet -- observed but never answered, with no reply
    and no requests row. Setting the clock on connect is the fix, and
    connect time is exactly when it is needed: the radio just booted."""
    iface = FakeInterface(0x11223344)
    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=lambda p: None,
        interface_factory=lambda dev: iface,
    )
    a.start()
    a.wait_ready(timeout=2)
    a.stop()
    assert len(iface.set_times) == 1
    assert iface.set_times[0] == 0  # 0 = "use the host's current time"


def test_identity_mismatch_never_sets_the_clock():
    """Never write to a radio we have not confirmed is ours."""
    iface = FakeInterface(0xDEADBEEF)
    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=lambda p: None,
        interface_factory=lambda dev: iface,
    )
    a.start()
    a.wait_ready(timeout=2)
    a.stop()
    assert iface.set_times == []


def test_a_failed_clock_set_never_breaks_the_connection():
    """Best-effort: a radio that refuses the admin write must still be used
    for observation and replies."""
    iface = FakeInterface(0x11223344)
    iface.set_time_error = OSError("admin write refused")
    a = RadioAdapter(
        RadioConfig("/dev/fake", "!11223344"),
        sink=lambda p: None,
        interface_factory=lambda dev: iface,
    )
    a.start()
    assert a.wait_ready(timeout=2)
    s = a.status()
    assert s.connected is True and s.reply_ready is True
    a.stop()
