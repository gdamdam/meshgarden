"""Test doubles emulating meshtastic's SerialInterface and pubsub behaviour.

Only this module and meshgarden/radio.py may import meshtastic;
see the containment test in tests/test_radio.py.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from meshtastic import BROADCAST_ADDR
from pubsub import pub

from meshgarden.config import RadioConfig, ServiceConfig
from meshgarden.meshtastic_transport import MeshtasticTransport
from meshgarden.radio import RadioAdapter


class FakeInterface:
    """Emulates meshtastic SerialInterface + its pubsub behaviour.

    sent: list[dict] records every sendText call with ALL kwargs.
    """

    def __init__(self, my_node_num: int = 0x11223344, devPath: str | None = None, **kw) -> None:
        self.myInfo = SimpleNamespace(my_node_num=my_node_num)
        self.devPath = devPath
        self.sent: list[dict] = []
        self.closed = False
        self._next_id = 1
        # Mirrors meshtastic's interface.localNode.setTime(), which the
        # adapter calls on every verified (re)connect to keep the radio's
        # clock (no battery-backed RTC) aligned with the host's.
        self.set_times: list[int] = []
        self.set_time_error: Exception | None = None
        self.localNode = SimpleNamespace(setTime=self._set_time)

    def _set_time(self, timeSec: int = 0) -> None:
        if self.set_time_error is not None:
            raise self.set_time_error
        self.set_times.append(timeSec)

    def getShortName(self) -> str | None:
        return None

    def getLongName(self) -> str | None:
        return None

    def sendText(
        self,
        text,
        destinationId=BROADCAST_ADDR,
        wantAck=False,
        wantResponse=False,
        onResponse=None,
        channelIndex=0,
        **kw,
    ):
        packet_id = self._next_id
        self._next_id += 1
        self.sent.append(
            dict(
                text=text,
                destinationId=destinationId,
                wantAck=wantAck,
                wantResponse=wantResponse,
                channelIndex=channelIndex,
                ts=time.time(),
            )
        )
        return SimpleNamespace(id=packet_id)

    def close(self) -> None:
        self.closed = True

    # test-side injection, mirrors real pubsub exactly:
    def emit_packet(self, packet: dict) -> None:
        pub.sendMessage("meshtastic.receive", packet=packet, interface=self)

    def emit_connected(self) -> None:
        pub.sendMessage("meshtastic.connection.established", interface=self)

    def emit_lost(self) -> None:
        pub.sendMessage("meshtastic.connection.lost", interface=self)


_UNSET = object()


def make_packet(
    *,
    from_num=0xAABBCCDD,
    to_num=0x11223344,
    packet_id=4242,
    channel=None,
    text=None,
    portnum="TEXT_MESSAGE_APP",
    rx_time=_UNSET,
    rx_snr=None,
    rx_rssi=None,
    hop_limit=None,
    hop_start=None,
    via_mqtt=None,
    pki_encrypted=None,
    request_id=None,
    routing_error=None,
    user=None,
    position=None,
    telemetry=None,
) -> dict:
    """Builds a dict with the SAME omission rules as real meshtastic.

    A key is OMITTED when its value is None, and `channel`/`rxSnr`/`rxRssi`/
    `hopLimit`/`hopStart`/`viaMqtt`/`pkiEncrypted` are omitted when falsy.
    `decoded.payload` is always bytes. `user` is a NODEINFO_APP payload's
    decoded sub-message (e.g. {"shortName": ..., "longName": ..., "hwModel":
    ..., "role": ..., "publicKey": ..., "isLicensed": ...}), omitted when
    None. `position` is a POSITION_APP payload's decoded sub-message (e.g.
    {"latitudeI": ..., "longitudeI": ..., "altitude": ..., "precisionBits":
    ..., "time": ...}); `telemetry` is a TELEMETRY_APP payload's decoded
    sub-message (e.g. {"deviceMetrics": {"batteryLevel": ..., "voltage":
    ..., "channelUtilization": ..., "airUtilTx": ..., "uptimeSeconds":
    ...}}). Both omitted when None.

    `rx_time` defaults to the current wall-clock second, mirroring how
    Meshtastic firmware timestamps a just-received packet (so a caller that
    doesn't care about freshness gets a realistically "fresh" packet by
    default); pass `rx_time=None` explicitly to simulate a packet that
    genuinely carries no rxTime.
    """
    if rx_time is _UNSET:
        rx_time = int(time.time())
    payload = text.encode() if text is not None else b""
    decoded: dict = {"portnum": portnum, "payload": payload}
    if text is not None:
        decoded["text"] = text
    if request_id is not None:
        decoded["requestId"] = request_id
    if routing_error is not None:
        decoded["routing"] = {"errorReason": routing_error}
    if user is not None:
        decoded["user"] = user
    if position is not None:
        decoded["position"] = position
    if telemetry is not None:
        decoded["telemetry"] = telemetry

    packet: dict = {"from": from_num, "to": to_num, "decoded": decoded}
    if packet_id is not None:
        packet["id"] = packet_id
    if channel:
        packet["channel"] = channel
    if rx_time is not None:
        packet["rxTime"] = rx_time
    if rx_snr:
        packet["rxSnr"] = rx_snr
    if rx_rssi:
        packet["rxRssi"] = rx_rssi
    if hop_limit:
        packet["hopLimit"] = hop_limit
    if hop_start:
        packet["hopStart"] = hop_start
    if via_mqtt:
        packet["viaMqtt"] = via_mqtt
    if pki_encrypted:
        packet["pkiEncrypted"] = pki_encrypted
    return packet


class FakeTransport(MeshtasticTransport):
    """A `MeshtasticTransport` wired to a `FakeInterface` instead of a real
    serial radio -- the real production class, running against a fake
    radio library, not a hand-rolled double with its own (potentially
    divergent) send/ack behavior. `.iface` and `.radio` stay reachable for
    tests that want to inject packets (`iface.emit_packet(...)`), read what
    was sent (`iface.sent`), or patch the lower layer directly
    (`radio.status = lambda: ...`), exactly as `tests/test_sender.py`'s
    harness already does for a plain `RadioAdapter`.

    Owns its own `RadioAdapter`/`FakeInterface` pair -- constructed and
    `.start()`ed here -- so a caller gets a fully connected, reply-ready
    transport with one call. `.stop()` tears the radio down; call it in
    teardown exactly as you would `RadioAdapter.stop()`.
    """

    def __init__(
        self,
        my_node_num: int = 0x11223344,
        expected_node_id: str = "!11223344",
        *,
        service_cfg: ServiceConfig | None = None,
        wait_ready_timeout: float = 2.0,
        **iface_kw,
    ) -> None:
        self.iface = FakeInterface(my_node_num, **iface_kw)
        radio = RadioAdapter(
            RadioConfig(device="/dev/fake", expected_node_id=expected_node_id),
            sink=lambda obs: None,
            interface_factory=lambda dev: self.iface,
        )
        radio.start()
        radio.wait_ready(timeout=wait_ready_timeout)
        super().__init__(
            radio,
            service_cfg=service_cfg or ServiceConfig(),
            expected_node_id=expected_node_id,
        )

    def stop(self) -> None:
        self.radio.stop()
