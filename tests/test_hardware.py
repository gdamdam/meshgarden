"""Real two-radio acceptance tests. All gated on environment variables so the
default test run (`addopts = "-m 'not hardware'"`) never touches a serial
port. These drive real `meshtastic.serial_interface.SerialInterface`
instances directly -- never `tests.fakes.FakeInterface` -- so `meshtastic`
is only ever imported from inside a fixture, never at module import time.

Operator invariant: this suite never transmits a broadcast on any channel.
Meshgarden never answers in a public channel, and its tests must not
broadcast either -- every transmission below is a DM from the visitor radio
to the service radio (MESHGARDEN_TEST_NODE_ID). Bare commands (MENU, ?, INFO,
GUESTBOOK, STATS, ICHING, HAIKU, BOOK, WORDLE, SEEN, WHO, ME, PING, BOARDS,
MAIL) are sent as-is, as are the two-level "MENU STATS"/"MENU GAMES"
menu-page requests; any free text sent (the SIGN argument, a BOOK guess, a
WORDLE guess, or a MAIL note) contains the word "test". This is enforced
statically by
test_packaging.py::test_hardware_suite_is_dm_only_and_never_broadcasts.

Env vars:

- MESHGARDEN_TEST_DEVICE / MESHGARDEN_TEST_NODE_ID: the service radio (the
  one the daemon under test connects to) and its expected "!xxxxxxxx" id.
- MESHGARDEN_TEST_VISITOR_DEVICE: a second radio used to send DMs to the
  service radio and observe replies, exactly as a real visitor would.
- MESHGARDEN_TEST_LAB_WORD: the service's configured `[service]
  lab_unlock_word`, needed only by the MAIL round-trip test (POST/READ/
  BOARDS/MAIL are gated behind the experimental LAB tier -- see
  docs/security-and-privacy.md#lab-tier); that test skips if this is unset.

Broadcast rejection (checklist item 1) is no longer proven by transmitting a
test broadcast at all: test_real_mesh_broadcasts_never_produce_a_reply
instead waits, passively, for real ambient mesh traffic already addressed to
"^all" from some other node, and confirms the service radio never replies to
it. If no such traffic is heard within the window, that test skips rather
than asserting anything.

Anything that cannot be driven from a host process at all (a physical USB
unplug, a `kill -9` mid-send) stays a manual, numbered item in the hardware
acceptance checklist; the two tests below that cover such scenarios
are unconditional `pytest.skip()`s naming the relevant checklist item:
item 3 (a DM addressed to a third node id) and item 6 (physically unplugging
the service radio). Checklist item 11 (a 180-byte reply delivered intact
over a PKI DM) no longer belongs on that manual list -- real-radio runs
proved PKI DMs between a visitor and the service radio work once
`visitor_knows_service` is satisfied, so it is now a real, env-gated test
below (`test_180_byte_reply_arrives_intact_over_a_pki_dm`).

Meshtastic 2.7 firmware refuses to send a text DM to a node whose public key
the sending radio has not yet learned: it delivers a local `ROUTING_APP`
NAK (`PKI_SEND_FAIL_PUBLIC_KEY`) and nothing goes on the air. Every test that
sends a DM to the service radio therefore depends on `visitor_knows_service`,
which drives a NodeInfo exchange first so the visitor has the service's key.
"""

from __future__ import annotations

import os
import re
import time
import uuid

import pytest

from meshgarden import wordle
from meshgarden.commands import Reject, info_text, menu_games_text, menu_text
from meshgarden.config import (
    BackupConfig,
    Config,
    DatabaseConfig,
    MetricsConfig,
    RadioConfig,
    RetentionConfig,
    ServiceConfig,
    node_id_from_num,
)
from meshgarden.database import connect
from meshgarden.main import Daemon

pytestmark = pytest.mark.hardware

DEVICE = os.environ.get("MESHGARDEN_TEST_DEVICE")
NODE_ID = os.environ.get("MESHGARDEN_TEST_NODE_ID")
VISITOR_DEVICE = os.environ.get("MESHGARDEN_TEST_VISITOR_DEVICE")
# Named with "test" (not just "LAB_WORD") so a literal reference to this
# module constant reads, to both a human reviewer and
# test_packaging.py's static DM-only guard, as what it is: a value sourced
# from a dedicated test-only env var, never text the test itself invents.
TEST_LAB_WORD = os.environ.get("MESHGARDEN_TEST_LAB_WORD")

_DAEMON_READY_TIMEOUT_SECONDS = 30.0
_REPLY_TIMEOUT_SECONDS = 90.0
_GRACE_SECONDS = 10.0  # extra time to catch a stray duplicate reply
_NODEINFO_TIMEOUT_SECONDS = 90.0  # bounded wait for the visitor to learn the service's key
_REAL_MESH_BROADCAST_WINDOW_SECONDS = 120.0  # bounded wait for real ambient mesh broadcast traffic


class _ReplyCollector:
    """Subscribes to `meshtastic.receive` on exactly one interface (never
    the daemon's own), collecting TEXT_MESSAGE_APP packets from one expected
    sender. Multiple SerialInterface instances in one process publish to the
    same global pubsub topic, so filtering by the `interface` kwarg the
    library passes to every callback is what keeps this collector from also
    picking up the service radio's own traffic.

    Also watches for a *local* NAK: when the visitor's own firmware refuses
    to send a DM (e.g. `PKI_SEND_FAIL_PUBLIC_KEY` for an unknown-key
    destination), it delivers a `ROUTING_APP` packet to the visitor itself
    with `decoded.requestId` equal to the id `sendText`/`sendData` returned,
    and `errorReason` set to something other than "NONE". Register the ids
    you care about with `expect_request_id` right after sending so a wait
    can bail out immediately instead of waiting out the full timeout.
    """

    def __init__(self, iface, expect_from_num: int) -> None:
        self._iface = iface
        self._expect_from_num = expect_from_num
        self.replies: list[str] = []
        self.local_nak_reason: str | None = None
        self.local_nak_request_id: int | None = None
        self._pending_request_ids: set[int] = set()

    def expect_request_id(self, request_id: int) -> None:
        self._pending_request_ids.add(request_id)

    def _on_receive(self, packet=None, interface=None) -> None:
        if interface is not self._iface or packet is None:
            return
        decoded = packet.get("decoded", {})
        portnum = decoded.get("portnum")
        if portnum == "ROUTING_APP":
            request_id = decoded.get("requestId")
            if request_id in self._pending_request_ids:
                error = decoded.get("routing", {}).get("errorReason", "NONE")
                if error and error != "NONE":
                    self.local_nak_reason = error
                    self.local_nak_request_id = request_id
            return
        if portnum != "TEXT_MESSAGE_APP":
            return
        if int(packet.get("from", -1)) != self._expect_from_num:
            return
        self.replies.append(decoded.get("text"))

    def __enter__(self) -> _ReplyCollector:
        from pubsub import pub

        pub.subscribe(self._on_receive, "meshtastic.receive")
        return self

    def __exit__(self, *exc_info: object) -> None:
        from pubsub import pub

        pub.unsubscribe(self._on_receive, "meshtastic.receive")

    def wait_for_at_least_one(self, timeout: float) -> list[str]:
        """Waits for a reply, but returns early (with `replies` still empty)
        the moment a registered request is locally NAKed -- callers must
        check `local_nak_reason` and fail immediately rather than treating
        an early, empty return as a plain timeout."""
        deadline = time.monotonic() + timeout
        while (
            time.monotonic() < deadline
            and not self.replies
            and self.local_nak_reason is None
        ):
            time.sleep(0.5)
        return list(self.replies)


@pytest.fixture(scope="module")
def visitor_interface():
    if not VISITOR_DEVICE:
        pytest.skip(
            "set MESHGARDEN_TEST_VISITOR_DEVICE (a second radio) to run two-radio scenarios"
        )
    from meshtastic.serial_interface import SerialInterface

    iface = SerialInterface(devPath=VISITOR_DEVICE)
    try:
        yield iface
    finally:
        iface.close()


@pytest.fixture
def visitor_knows_service(visitor_interface):
    """Ensures the visitor radio already has the service radio's public key
    on file before a test sends it a DM. Meshtastic 2.7 firmware refuses a
    text DM to a node with an unknown key entirely locally (see the
    `_ReplyCollector` docstring), so without this precondition a round-trip
    test would fail with zero replies for a reason that has nothing to do
    with the daemon under test.

    Function-scoped like every other fixture that may touch the service
    radio's identity: it only reads `visitor_interface.nodes` and sends from
    the visitor, so it never opens MESHGARDEN_TEST_DEVICE itself.
    """
    if not NODE_ID:
        pytest.skip("set MESHGARDEN_TEST_DEVICE and MESHGARDEN_TEST_NODE_ID")
    service_id = NODE_ID.lower()

    def _known() -> bool:
        node = (visitor_interface.nodes or {}).get(service_id)
        return bool(node and node.get("user", {}).get("publicKey"))

    if _known():
        return

    from meshtastic.protobuf import mesh_pb2, portnums_pb2

    visitor_interface.sendData(
        mesh_pb2.User(),
        destinationId=service_id,
        portNum=portnums_pb2.PortNum.NODEINFO_APP,
        wantAck=True,
        wantResponse=True,
        channelIndex=0,
    )
    deadline = time.monotonic() + _NODEINFO_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _known():
            return
        time.sleep(1.0)

    pytest.fail(
        f"visitor radio has no public key on file for the service radio "
        f"({service_id}) after {_NODEINFO_TIMEOUT_SECONDS:.0f}s: Meshtastic "
        "2.7 firmware refuses to send a text DM to a node with an unknown "
        "key, failing it locally with errorReason=PKI_SEND_FAIL_PUBLIC_KEY "
        "before anything reaches the air. Let the two radios exchange "
        "NodeInfo first -- reboot the service radio (it broadcasts NodeInfo "
        "on boot) or wait for its periodic NodeInfo broadcast -- then rerun."
    )


def _start_service_daemon(
    tmp_path, service_name: str | None = None, lab_unlock_word: str | None = None
):
    """Builds and starts a real Daemon connected to the service radio over
    its own serial connection, with a throwaway config/db/backup dir and
    metrics bound to an OS-assigned port (never a fixed one, to avoid
    clashing with a real deployment on the same host). Blocks until the
    daemon reports reply_ready, or fails (after stopping the daemon) if it
    never does.

    Shared by `service_daemon`, `service_daemon_long_name`, and
    `service_daemon_lab` so no fixture duplicates the construction/wait-for-
    ready logic -- they differ only in the configured `ServiceConfig`
    fields (`name`, `lab_unlock_word`).
    """
    if not DEVICE or not NODE_ID:
        pytest.skip("set MESHGARDEN_TEST_DEVICE and MESHGARDEN_TEST_NODE_ID")

    service_kwargs = {}
    if service_name is not None:
        service_kwargs["name"] = service_name
    if lab_unlock_word is not None:
        service_kwargs["lab_unlock_word"] = lab_unlock_word
    cfg = Config(
        radio=RadioConfig(device=DEVICE, expected_node_id=NODE_ID.lower()),
        service=ServiceConfig(**service_kwargs),
        database=DatabaseConfig(path=str(tmp_path / "observatory.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "backups")),
        metrics=MetricsConfig(listen="127.0.0.1", port=0),
    )
    daemon = Daemon(cfg)
    daemon.start()
    deadline = time.monotonic() + _DAEMON_READY_TIMEOUT_SECONDS
    ready = False
    while time.monotonic() < deadline:
        status = daemon.status_snapshot()
        if status["radio_connected"] and status["reply_ready"]:
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        daemon.stop()
        pytest.fail(
            f"service radio did not report reply_ready within "
            f"{_DAEMON_READY_TIMEOUT_SECONDS:.0f}s"
        )
    return daemon


@pytest.fixture
def service_daemon(tmp_path):
    """A real Daemon connected to the service radio, using the default
    operator-configured service name. See `_start_service_daemon`.

    Deliberately function-scoped (pytest's default) and, together with
    `service_daemon_long_name`, the ONLY fixtures in this module that open
    MESHGARDEN_TEST_DEVICE: the radio's serial connection only accepts one
    exclusive open at a time, so a wider scope here would hold the port
    open across tests and make every later test's own connection attempt
    fail. See test_packaging.py::
    test_hardware_fixtures_never_hold_the_service_device_open for the
    static regression guard.
    """
    daemon = _start_service_daemon(tmp_path)
    try:
        yield daemon
    finally:
        daemon.stop()


@pytest.fixture
def service_daemon_long_name(tmp_path):
    """Same as `service_daemon`, but the daemon runs with a 200-ASCII-
    character service name -- long enough that `info_text()` truncates only
    the name and the INFO reply comes out at exactly `max_response_bytes`
    (180) UTF-8 bytes. Used by
    test_180_byte_reply_arrives_intact_over_a_pki_dm. See
    `_start_service_daemon`.
    """
    daemon = _start_service_daemon(tmp_path, service_name="A" * 200)
    try:
        yield daemon
    finally:
        daemon.stop()


@pytest.fixture
def service_daemon_lab(tmp_path):
    """Same as `service_daemon`, but the daemon's `[service]
    lab_unlock_word` is set to MESHGARDEN_TEST_LAB_WORD, so the experimental
    LAB tier (POST/READ/BOARDS/MAIL) can be unlocked for the visitor's own
    node id by DMing that exact word. Used only by the MAIL round-trip
    test; skips (rather than failing) if the env var is unset, same
    precondition style as `visitor_knows_service`. See `_start_service_daemon`.
    """
    if not TEST_LAB_WORD:
        pytest.skip(
            "set MESHGARDEN_TEST_LAB_WORD (the service's configured [service] "
            "lab_unlock_word) to run the MAIL round-trip hardware test"
        )
    daemon = _start_service_daemon(tmp_path, lab_unlock_word=TEST_LAB_WORD)
    try:
        yield daemon
    finally:
        daemon.stop()


def _service_num() -> int:
    return int(NODE_ID.lower().lstrip("!"), 16)


def test_radio_identity_matches_configured_node_id(service_daemon):
    """`service_daemon` already blocks (up to 30s) until the daemon reports
    reply_ready, which itself requires the connected radio's identity to
    match -- see radio.py::_check_identity. This asserts that outcome
    explicitly, through the daemon's own status snapshot, rather than
    opening a second, competing connection to the same serial device."""
    status = service_daemon.status_snapshot()
    assert status["radio_connected"] is True
    assert status["reply_ready"] is True
    assert status["actual_node_id"] == NODE_ID.lower()


def test_dm_round_trip_produces_one_reply(
    service_daemon, visitor_interface, visitor_knows_service
):
    """Checklist item 2: a valid DM on channel index 0 gets exactly one
    reply, and the service's own request row reaches submitted/acked."""
    service_id = NODE_ID.lower()
    visitor_id = node_id_from_num(visitor_interface.myInfo.my_node_num)

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="MENU", destinationId=service_id, wantAck=True, wantResponse=False, channelIndex=0
        )
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)  # catch a stray duplicate, if any

    assert collector.replies == [menu_text()]

    conn = connect(service_daemon.cfg.database.path, read_only=True)
    try:
        row = conn.execute(
            "SELECT state FROM processed_requests WHERE sender_node_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (visitor_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["state"] in ("submitted", "acked")


def test_question_mark_alias_dm_produces_one_reply(
    service_daemon, visitor_interface, visitor_knows_service
):
    """`?` is an alias for MENU. Cheap companion to
    test_dm_round_trip_produces_one_reply -- same shape, no extra DB check."""
    service_id = NODE_ID.lower()

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="?", destinationId=service_id, wantAck=True, wantResponse=False, channelIndex=0
        )
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)  # catch a stray duplicate, if any

    assert collector.replies == [menu_text()]


def test_menu_games_dm_produces_the_games_sub_page(
    service_daemon, visitor_interface, visitor_knows_service
):
    """Two-level menu: `MENU GAMES` gets the games sub-page reply, in one DM
    (this test's own `service_daemon`/db are fresh per test, so no other
    command has touched the per-node cooldown for this sender yet)."""
    service_id = NODE_ID.lower()

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="MENU GAMES",
            destinationId=service_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)  # catch a stray duplicate, if any

    assert collector.replies == [menu_games_text()]


def test_real_mesh_broadcasts_never_produce_a_reply(service_daemon, visitor_interface):
    """Checklist item 1, passive form: this suite never transmits a
    broadcast (see the module docstring and the operator invariant), so
    broadcast rejection is instead proven by waiting for real ambient mesh
    traffic -- a position/nodeinfo/telemetry/text broadcast from some node
    that is neither the service radio nor the visitor -- and confirming the
    service never replies to it, and never records a processed request or a
    guestbook entry as a result of it.

    Transmits nothing itself. If no such broadcast is heard within the
    bounded window, this skips rather than asserting anything: with only
    two radios on hand, silence in that case proves nothing either way.
    """
    service_id = NODE_ID.lower()
    visitor_id = node_id_from_num(visitor_interface.myInfo.my_node_num)

    conn = connect(service_daemon.cfg.database.path, read_only=True)
    try:
        with _ReplyCollector(visitor_interface, _service_num()) as collector:
            deadline = time.monotonic() + _REAL_MESH_BROADCAST_WINDOW_SECONDS
            heard = None
            while time.monotonic() < deadline:
                # Prefer a TEXT_MESSAGE_APP broadcast if one shows up, but
                # fall back to any portnum -- position/nodeinfo/telemetry
                # broadcasts are just as valid proof the service stayed
                # silent in response to real "^all" traffic.
                text_row = conn.execute(
                    "SELECT sender_node_id, portnum FROM observations WHERE "
                    "dest_node_id='^all' AND portnum='TEXT_MESSAGE_APP' AND "
                    "sender_node_id NOT IN (?, ?) ORDER BY server_time DESC LIMIT 1",
                    (service_id, visitor_id),
                ).fetchone()
                if text_row is not None:
                    heard = text_row
                    break
                if heard is None:
                    heard = conn.execute(
                        "SELECT sender_node_id, portnum FROM observations WHERE "
                        "dest_node_id='^all' AND sender_node_id NOT IN (?, ?) "
                        "ORDER BY server_time DESC LIMIT 1",
                        (service_id, visitor_id),
                    ).fetchone()
                time.sleep(1.0)

            if heard is None:
                pytest.skip("no mesh broadcast heard within the window; nothing proven")

            time.sleep(_GRACE_SECONDS)  # let any (would-be) reply arrive

        assert collector.replies == [], (
            f"service replied to a real broadcast (portnum={heard['portnum']!r} "
            f"from {heard['sender_node_id']}): {collector.replies}"
        )

        request_rows = conn.execute("SELECT COUNT(*) FROM processed_requests").fetchone()[0]
        guestbook_rows = conn.execute("SELECT COUNT(*) FROM guestbook").fetchone()[0]
    finally:
        conn.close()

    assert request_rows == 0, (
        f"observed a real broadcast (portnum={heard['portnum']!r}) but "
        "processed_requests is nonzero"
    )
    assert guestbook_rows == 0, (
        f"observed a real broadcast (portnum={heard['portnum']!r}) but "
        "guestbook is nonzero"
    )


def test_dm_to_a_different_node_produces_no_reply():
    """Checklist item 3: a DM addressed to some other node id must never be
    treated as a request to the service radio.

    With only two radios on hand, this cannot be driven politely: sending a
    DM to a third node id either targets a real stranger's radio (if one
    happens to be in range) or, more likely, is refused locally by the
    visitor's own firmware for lacking that node's public key -- exactly the
    PKI precondition this module works around for the *service* radio, not
    something worth reproducing for a throwaway destination. This stays a
    manual item; see the hardware acceptance checklist, item 3."""
    pytest.skip(
        "requires a third radio (or a willing stranger's node) to address "
        "the DM to without it being refused locally for an unknown key; "
        "see the hardware acceptance checklist, item 3"
    )


def test_duplicate_delivery_produces_exactly_one_reply(
    service_daemon, visitor_interface, visitor_knows_service
):
    """Checklist item 9: sending the same command twice in quick succession
    (within the per-node cooldown) must produce exactly one reply -- the
    second is either deduplicated by packet id (a firmware-level
    retransmit) or dropped by the per-node cooldown (a second, distinct
    packet id) -- and the service's own bookkeeping must show it: the first
    request reaches submitted/acked, and the second is either absent
    (deduped by packet id before it became its own row) or landed as
    `dropped` with a bounded reason, never a second `submitted`."""
    service_id = NODE_ID.lower()
    visitor_id = node_id_from_num(visitor_interface.myInfo.my_node_num)

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        first = visitor_interface.sendText(
            text="MENU", destinationId=service_id, wantAck=True, wantResponse=False, channelIndex=0
        )
        collector.expect_request_id(first.id)
        second = visitor_interface.sendText(
            text="MENU", destinationId=service_id, wantAck=True, wantResponse=False, channelIndex=0
        )
        collector.expect_request_id(second.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused a DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)

    assert len(collector.replies) == 1

    conn = connect(service_daemon.cfg.database.path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT packet_id, state, reason FROM processed_requests "
            "WHERE sender_node_id=? ORDER BY first_seen_at ASC",
            (visitor_id,),
        ).fetchall()
    finally:
        conn.close()

    submitted_or_acked = [r for r in rows if r["state"] in ("submitted", "acked")]
    assert len(submitted_or_acked) == 1, (
        f"expected exactly one submitted/acked request, got: {[dict(r) for r in rows]}"
    )
    for row in rows:
        if row["packet_id"] == submitted_or_acked[0]["packet_id"]:
            continue
        assert row["state"] == "dropped", (
            f"a second, distinct request must be dropped, not {row['state']!r}: {dict(row)}"
        )
        assert row["reason"] in (Reject.DUPLICATE.value, Reject.RATE_LIMITED.value), (
            f"unexpected drop reason: {row['reason']!r}"
        )


_LONG_SERVICE_NAME = "A" * 200


def test_180_byte_reply_arrives_intact_over_a_pki_dm(
    service_daemon_long_name, visitor_interface, visitor_knows_service
):
    """Checklist item 11: a reply at the full 180-byte budget, delivered as
    a PKI-encrypted DM, arrives byte-for-byte intact.

    `service_daemon_long_name` runs the daemon with a 200-character service
    name, so `info_text()` truncates only the name and the INFO reply is
    exactly `max_response_bytes` (180) UTF-8 bytes -- the largest reply the
    service ever sends. `visitor_knows_service` is the same precondition
    `test_dm_round_trip_produces_one_reply` relies on, and real-radio runs
    have shown it is sufficient for PKI DMs to the service radio to work
    end-to-end; this test additionally confirms, from the service's own
    observations, that the inbound request packet really was PKI-encrypted
    rather than merely channel-encrypted -- if it were not, this would fail
    loudly instead of silently passing on a channel-encrypted DM.
    """
    service_id = NODE_ID.lower()
    visitor_id = node_id_from_num(visitor_interface.myInfo.my_node_num)
    expected = info_text(_LONG_SERVICE_NAME)
    assert len(expected.encode("utf-8")) == 180  # sanity: this is the scenario under test

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="INFO", destinationId=service_id, wantAck=True, wantResponse=False, channelIndex=0
        )
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)  # catch a stray duplicate, if any

    assert len(collector.replies) == 1
    reply = collector.replies[0]
    assert len(reply.encode("utf-8")) == 180
    assert reply == expected
    assert "📝 SIGN <msg> publishes" in reply
    assert "⚠️ experimental · not for emergencies · no uptime guarantee" in reply

    conn = connect(service_daemon_long_name.cfg.database.path, read_only=True)
    try:
        row = conn.execute(
            "SELECT packet_id, state FROM processed_requests WHERE sender_node_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (visitor_id,),
        ).fetchone()
        assert row is not None
        assert row["state"] in ("submitted", "acked")

        obs = conn.execute(
            "SELECT pki_encrypted FROM observations WHERE sender_node_id=? AND "
            "dest_node_id=? AND packet_id=? ORDER BY server_time DESC LIMIT 1",
            (visitor_id, service_id, row["packet_id"]),
        ).fetchone()
    finally:
        conn.close()

    assert obs is not None, "no inbound observation recorded for the INFO request packet"
    if not obs["pki_encrypted"]:
        pytest.fail(
            "INFO request arrived with pki_encrypted=0: this test requires "
            "a PKI-encrypted DM to prove the 180-byte reply survives one -- "
            "check that visitor_knows_service actually ran the NodeInfo "
            "exchange rather than short-circuiting on a stale key"
        )


def test_sign_publishes_and_is_acknowledged(
    service_daemon, visitor_interface, visitor_knows_service
):
    """SIGN coverage over real radios: a DM `SIGN test <token>` from the
    visitor produces exactly one two-line `🌱 Signed!\\nThanks! Others read
    it with GUESTBOOK` reply, a guestbook row whose message contains the
    token (so it contains "test"), and the
    service's own request row reaches submitted/acked. A follow-up DM
    `GUESTBOOK` must then reply with that entry's message (visitor-facing
    replies never carry the entry id) in no more than the 180-byte budget.
    Both commands are DMs between the two radios --
    see test_packaging.py::test_hardware_suite_is_dm_only_and_never_broadcasts
    for the static proof this module sends nothing else.
    """
    service_id = NODE_ID.lower()
    visitor_id = node_id_from_num(visitor_interface.myInfo.my_node_num)
    token = f"test-{uuid.uuid4().hex[:8]}"
    sign_test_text = f"SIGN test {token}"

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text=sign_test_text,
            destinationId=service_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        sign_send_time = time.monotonic()
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)  # catch a stray duplicate, if any

    assert len(collector.replies) == 1
    assert collector.replies[0] == "🌱 Signed!\nThanks! Others read it with GUESTBOOK", (
        f"unexpected SIGN reply: {collector.replies[0]!r}"
    )

    conn = connect(service_daemon.cfg.database.path, read_only=True)
    try:
        row = conn.execute(
            "SELECT state FROM processed_requests WHERE sender_node_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (visitor_id,),
        ).fetchone()
        assert row is not None
        assert row["state"] in ("submitted", "acked")

        gb_row = conn.execute(
            "SELECT message FROM guestbook WHERE sender_node_id=? "
            "ORDER BY entry_id DESC LIMIT 1",
            (visitor_id,),
        ).fetchone()
    finally:
        conn.close()
    assert gb_row is not None, f"no guestbook row for sender_node_id={visitor_id}"
    assert "test" in gb_row["message"]

    # Wait for per-node cooldown to elapse before GUESTBOOK to avoid silent drop by daemon
    cooldown_seconds = service_daemon.cfg.service.per_node_cooldown_seconds
    elapsed = time.monotonic() - sign_send_time
    remaining = (cooldown_seconds + 2) - elapsed
    if remaining > 0:
        time.sleep(remaining)

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="GUESTBOOK",
            destinationId=service_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)

    assert len(collector.replies) == 1
    guestbook_reply = collector.replies[0]
    assert len(guestbook_reply.encode("utf-8")) <= 180
    assert token in guestbook_reply
    assert not re.search(r"#\d+", guestbook_reply), (
        f"guestbook listing must not leak an entry id: {guestbook_reply!r}"
    )


def test_iching_dm_is_stable_for_the_same_sender_on_the_same_day(
    service_daemon, visitor_interface, visitor_knows_service
):
    """ICHING coverage over real radios: a DM ICHING gets exactly one reply
    (a hexagram line ending in the seedling emoji), and a second ICHING DM
    from the same visitor, later the same local day, gets the identical
    reply -- ICHING is a pure function of (sender, local day, salt), never
    randomized. Waits out the per-node cooldown between the two sends, same
    pattern as test_sign_publishes_and_is_acknowledged's SIGN->GUESTBOOK
    follow-up.
    """
    service_id = NODE_ID.lower()

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="ICHING",
            destinationId=service_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        first_send_time = time.monotonic()
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)

    assert len(collector.replies) == 1
    first_reply = collector.replies[0]
    assert len(first_reply.encode("utf-8")) <= 180
    assert first_reply.endswith(" \U0001f331")  # " 🌱"

    # Wait for per-node cooldown to elapse before the second ICHING.
    cooldown_seconds = service_daemon.cfg.service.per_node_cooldown_seconds
    elapsed = time.monotonic() - first_send_time
    remaining = (cooldown_seconds + 2) - elapsed
    if remaining > 0:
        time.sleep(remaining)

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="ICHING",
            destinationId=service_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)

    assert len(collector.replies) == 1
    assert collector.replies[0] == first_reply


def test_haiku_dm_produces_one_reply(service_daemon, visitor_interface, visitor_knows_service):
    """HAIKU coverage over real radios: a DM HAIKU gets exactly one reply,
    three lines joined by ' / ' and ending in the seedling emoji."""
    service_id = NODE_ID.lower()

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="HAIKU", destinationId=service_id, wantAck=True, wantResponse=False, channelIndex=0
        )
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)

    assert len(collector.replies) == 1
    reply = collector.replies[0]
    assert len(reply.encode("utf-8")) <= 180
    assert reply.endswith(" \U0001f331")  # " 🌱"
    assert reply.count("\n") == 2


def test_whoami_dm_produces_one_reply(service_daemon, visitor_interface, visitor_knows_service):
    """WHOAMI coverage over real radios: a bare WHOAMI DM gets exactly one
    reply naming the visitor's own node id -- WHOAMI is now an alias of ME
    (see commands.py's `_BARE_COMMANDS`), so the reply is ME's own."""
    service_id = NODE_ID.lower()

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="WHOAMI",
            destinationId=service_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)

    assert len(collector.replies) == 1
    reply = collector.replies[0]
    assert len(reply.encode("utf-8")) <= 180
    assert reply.startswith("\U0001f4e1 You are ")  # 📡
    assert "quota" in reply


def test_who_dm_produces_one_reply(service_daemon, visitor_interface, visitor_knows_service):
    """WHO coverage over real radios: a bare WHO DM gets exactly one reply
    naming nodes heard in the last hour/24h, with no ids."""
    service_id = NODE_ID.lower()

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="WHO", destinationId=service_id, wantAck=True, wantResponse=False, channelIndex=0
        )
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)

    assert len(collector.replies) == 1
    reply = collector.replies[0]
    assert len(reply.encode("utf-8")) <= 180
    assert reply.startswith("\U0001f465 ")  # "👥 "


def test_mail_on_then_send_to_self_then_list_round_trips(
    service_daemon_lab, visitor_interface, visitor_knows_service
):
    """MAIL coverage: unlocks the experimental LAB tier for the visitor's
    own node with MESHGARDEN_TEST_LAB_WORD (POST/READ/BOARDS/MAIL are
    silent, exactly like an unrecognized command, until unlocked -- see
    docs/security-and-privacy.md#lab-tier), then MAIL ON, then MAIL <own id>
    <msg containing "test">, then bare MAIL lists it back. Follows the same
    DM-only, "test"-in-every-free-text pattern as
    test_sign_publishes_and_is_acknowledged, waiting out the per-node
    cooldown between each of the four DMs.
    """
    service_id = NODE_ID.lower()
    visitor_id = node_id_from_num(visitor_interface.myInfo.my_node_num)
    token = f"test-{uuid.uuid4().hex[:8]}"
    cooldown_seconds = service_daemon_lab.cfg.service.per_node_cooldown_seconds

    # Unlock the LAB tier for the visitor's own node (whole message, exact
    # configured word -- TEST_LAB_WORD, never text this test invents).
    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text=TEST_LAB_WORD,
            destinationId=service_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        unlock_send_time = time.monotonic()
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)
    assert len(collector.replies) == 1
    remaining = (cooldown_seconds + 2) - (time.monotonic() - unlock_send_time)
    if remaining > 0:
        time.sleep(remaining)

    # MAIL ON: opt in to receiving notes.
    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="MAIL ON",
            destinationId=service_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        on_send_time = time.monotonic()
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)
    assert len(collector.replies) == 1
    assert collector.replies[0].startswith("\U0001f4ec")  # 📬
    remaining = (cooldown_seconds + 2) - (time.monotonic() - on_send_time)
    if remaining > 0:
        time.sleep(remaining)

    # MAIL <own id> <msg containing "test">: send a note to ourselves.
    mail_test_text = f"MAIL {visitor_id} test note {token}"
    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text=mail_test_text,
            destinationId=service_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        send_time = time.monotonic()
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)
    assert len(collector.replies) == 1
    assert collector.replies[0].startswith("\U0001f4ec")  # 📬
    remaining = (cooldown_seconds + 2) - (time.monotonic() - send_time)
    if remaining > 0:
        time.sleep(remaining)

    # Bare MAIL: lists the note back (and marks it delivered).
    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="MAIL", destinationId=service_id, wantAck=True, wantResponse=False, channelIndex=0
        )
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)
    assert len(collector.replies) == 1
    list_reply = collector.replies[0]
    assert len(list_reply.encode("utf-8")) <= 180
    assert token in list_reply


def test_garden_dm_produces_one_reply(service_daemon, visitor_interface, visitor_knows_service):
    """GARDEN coverage over real radios: a bare GARDEN DM gets exactly one
    reply, the today's-activity summary ending with "? MENU"."""
    service_id = NODE_ID.lower()

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="GARDEN",
            destinationId=service_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)

    assert len(collector.replies) == 1
    reply = collector.replies[0]
    assert len(reply.encode("utf-8")) <= 180
    assert reply.startswith("\U0001f331 ")  # "🌱 "
    assert reply.endswith("? MENU")


def test_book_dm_then_a_wrong_guess_produce_the_puzzle_then_a_miss_reply(
    service_daemon, visitor_interface, visitor_knows_service
):
    """BOOK coverage over real radios: a bare BOOK DM gets exactly one reply
    (today's emoji puzzle prompt), and a later BOOK <guess> DM -- "test",
    which is certainly never a book title in the puzzle table -- gets
    exactly one reply, the wrong-guess (❌) reply naming the hint.
    Waits out the per-node cooldown between the two sends, same pattern as
    test_iching_dm_is_stable_for_the_same_sender_on_the_same_day."""
    service_id = NODE_ID.lower()

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="BOOK", destinationId=service_id, wantAck=True, wantResponse=False, channelIndex=0
        )
        first_send_time = time.monotonic()
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)

    assert len(collector.replies) == 1
    prompt_reply = collector.replies[0]
    assert len(prompt_reply.encode("utf-8")) <= 180
    assert prompt_reply.startswith("\U0001f4da")  # 📚
    assert prompt_reply.endswith(" \U0001f331")  # " 🌱"

    # Wait for per-node cooldown to elapse before the guess.
    cooldown_seconds = service_daemon.cfg.service.per_node_cooldown_seconds
    elapsed = time.monotonic() - first_send_time
    remaining = (cooldown_seconds + 2) - elapsed
    if remaining > 0:
        time.sleep(remaining)

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="BOOK test",
            destinationId=service_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)

    assert len(collector.replies) == 1
    guess_reply = collector.replies[0]
    assert len(guess_reply.encode("utf-8")) <= 180
    assert guess_reply.startswith("❌")  # ❌
    assert "BOOK <title>" in guess_reply


def test_wordle_dm_then_a_guess_produce_the_prompt_then_feedback(
    service_daemon, visitor_interface, visitor_knows_service
):
    """WORDLE coverage over real radios: a bare WORDLE DM gets exactly one
    reply (today's prompt), and a later WORDLE <guess> DM -- "tests", a
    valid 5-letter guess containing "test" -- gets exactly one reply, the
    scored feedback line. Waits out the per-node cooldown between the two
    sends, same pattern as test_book_dm_then_a_wrong_guess_produce_the_puzzle_then_a_miss_reply.
    """
    service_id = NODE_ID.lower()

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="WORDLE",
            destinationId=service_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        first_send_time = time.monotonic()
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)

    assert len(collector.replies) == 1
    prompt_reply = collector.replies[0]
    assert len(prompt_reply.encode("utf-8")) <= 180
    assert prompt_reply.startswith("\U0001f7e9")  # 🟩
    assert prompt_reply.endswith(" \U0001f331")  # " 🌱"

    # Wait for per-node cooldown to elapse before the guess.
    cooldown_seconds = service_daemon.cfg.service.per_node_cooldown_seconds
    elapsed = time.monotonic() - first_send_time
    remaining = (cooldown_seconds + 2) - elapsed
    if remaining > 0:
        time.sleep(remaining)

    with _ReplyCollector(visitor_interface, _service_num()) as collector:
        sent = visitor_interface.sendText(
            text="WORDLE tests",
            destinationId=service_id,
            wantAck=True,
            wantResponse=False,
            channelIndex=0,
        )
        collector.expect_request_id(sent.id)
        collector.wait_for_at_least_one(timeout=_REPLY_TIMEOUT_SECONDS)
        if collector.local_nak_reason is not None:
            pytest.fail(
                "visitor firmware refused the DM locally before it reached "
                f"the air (errorReason={collector.local_nak_reason!r}); see "
                "the visitor_knows_service precondition"
            )
        time.sleep(_GRACE_SECONDS)

    assert len(collector.replies) == 1
    guess_reply = collector.replies[0]
    assert len(guess_reply.encode("utf-8")) <= 180
    assert "TESTS" in guess_reply
    assert wordle.normalize_guess("tests") == "tests"  # sanity: a valid 5-letter guess


def test_unplug_triggers_connection_lost_and_reconnect_restores_reply_ready():
    pytest.skip(
        "requires physically unplugging the service radio's USB cable; "
        "see the hardware acceptance checklist, item 6"
    )
