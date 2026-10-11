"""Covering tests for `meshgarden.meshcore_client` (task 3B): the real
`meshcore`-backed `MeshCoreClient`.

These run against the INSTALLED `meshcore==2.3.11` package's own importable
surface (`meshcore.Event`/`EventType`, and a monkeypatched
`MeshCore.create_serial`) -- never against real hardware. There is no
MeshCore radio attached to this machine; real-hardware validation is task
5A. Every test that needs the real package calls `pytest.importorskip
("meshcore")` first, so this file degrades to "skipped" rather than
"failed" on a Meshtastic-only install that never added the optional group.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
import time

import pytest

from meshgarden import meshcore_client as mcc
from meshgarden.meshcore_client import _SecretRedactionFilter


def _event(event_type, payload):
    """Constructs a real `meshcore.events.Event`. `Event` is NOT re-exported
    from the top-level `meshcore` package (only `EventType` is --
    `meshcore/__init__.py`'s `__all__` confirms this), so every test that
    needs one reaches into `meshcore.events` directly through this helper
    rather than repeating the deeper import at each call site."""
    from meshcore.events import Event

    return Event(event_type, payload)


def test_guarded_import_raises_actionable_error_when_package_absent(monkeypatch):
    """Task 3B item A: absence of the optional `meshcore` package is a
    clean, actionable error only when a real client is actually
    constructed -- never an import-time failure. Simulated here by
    monkeypatching the module's own guarded reference, since the package
    IS installed in this dev/test environment. `_mc_pkg_import_attempted`
    must be forced `True` too (lazy-import follow-up): `_ensure_mc_pkg()`
    otherwise treats a bare `_mc_pkg = None` as "not yet tried" and
    genuinely (successfully) imports the real, installed package,
    defeating the simulation."""
    monkeypatch.setattr(mcc, "_mc_pkg", None)
    monkeypatch.setattr(mcc, "_mc_pkg_import_attempted", True)
    with pytest.raises(mcc.MeshCoreLibraryMissing, match="meshcore"):
        mcc.RealMeshCoreClient(device="/dev/null")


_BLOCK_MESHCORE_SCRIPT = """
import sys
import importlib.abc

class _BlockMeshCore(importlib.abc.MetaPathFinder):
    '''Makes `import meshcore` (and any submodule) fail with ImportError,
    without needing the package to actually be uninstalled -- a proper
    meta-path block rather than poking a sentinel into sys.modules, which
    would only affect a module not already imported and would leave stale
    global state behind for whatever runs next in the SAME process.'''

    def find_spec(self, fullname, path, target=None):
        if fullname == "meshcore" or fullname.startswith("meshcore."):
            raise ImportError(f"{fullname} is blocked for this test")
        return None

sys.meta_path.insert(0, _BlockMeshCore())

import meshgarden.main
import meshgarden.ops
import meshgarden.meshcore_transport
import meshgarden.meshcore_client
import meshgarden.meshcore_compat

assert meshgarden.meshcore_client._mc_pkg is None, "guard did not see the package as absent"
print("OK")
"""


def _run_blocked_import_script() -> subprocess.CompletedProcess:
    """Runs `_BLOCK_MESHCORE_SCRIPT` in a FRESH subprocess (same
    interpreter/venv this test suite runs under) rather than mutating
    `sys.modules`/reloading modules in THIS process: `meshgarden.
    meshcore_client` (and everything importing it) is already imported by
    the time this test runs, in whatever order the suite happens to
    collect it, so any in-process trick would be order-fragile and would
    leak mutated global state to every test that runs afterward. A
    subprocess sidesteps both problems entirely -- nothing in this
    process's module cache is ever touched."""
    return subprocess.run(
        [sys.executable, "-c", _BLOCK_MESHCORE_SCRIPT],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_importing_this_module_never_requires_the_package():
    """Merely importing `meshgarden.meshcore_client` (as any module that
    references its types must be able to, even on a Meshtastic-only
    install) must not raise, regardless of whether `meshcore` is
    importable -- only constructing `RealMeshCoreClient` may."""
    result = _run_blocked_import_script()
    assert result.returncode == 0, result.stdout + result.stderr


def test_repeater_password_filter_redacts_text_and_hex_forms():
    secret = "guest-password"
    record = logging.LogRecord(
        "meshcore", logging.DEBUG, __file__, 1,
        f"raw={secret.encode().hex()} text={secret}", (), None,
    )
    assert _SecretRedactionFilter(secret).filter(record)
    rendered = record.getMessage()
    assert secret not in rendered
    assert secret.encode().hex() not in rendered
    assert rendered.count("<redacted>") == 2


@pytest.mark.parametrize("secret", ["påss", "line\\slash\nnext"])
def test_repeater_password_filter_drops_raw_packet_debug_for_any_encoding(secret):
    raw = b"\x1a" + secret.encode("utf-8")
    filt = _SecretRedactionFilter(secret)
    for message in (f"Sending raw data: {raw.hex()}", f"sending pkt : {raw!r}"):
        record = logging.LogRecord("meshcore", logging.DEBUG, __file__, 1, message, (), None)
        assert filt.filter(record)
        assert record.getMessage().endswith("<redacted>")
        assert secret not in record.getMessage()


def test_repeater_query_requires_matching_login_and_status_keys():
    meshcore = pytest.importorskip("meshcore")
    key = "ac6e4f81755c" + "00" * 26

    class Commands:
        async def send_login_sync(self, *args, **kwargs):
            return _event(meshcore.EventType.LOGIN_SUCCESS, {"pubkey_prefix": key[:12]})

        async def req_status_sync(self, *args, **kwargs):
            return {"pubkey_pre": key[:12], "bat": 4100, "nb_recv": 7, "nb_sent": 3}

    client = mcc.RealMeshCoreClient(
        device="/dev/null", repeater_public_key=key, repeater_guest_password="guest"
    )
    session = mcc._Session(None, type("MC", (), {"commands": Commands()})())
    result = asyncio.run(client._query_repeater_status(session))
    assert result["bat"] == 4100

    status_calls = 0

    async def failed_login(*args, **kwargs):
        return None

    async def counted_status(*args, **kwargs):
        nonlocal status_calls
        status_calls += 1
        return {"pubkey_pre": key[:12]}

    session.mc.commands.send_login_sync = failed_login
    session.mc.commands.req_status_sync = counted_status
    with pytest.raises(RuntimeError, match="login failed"):
        asyncio.run(client._query_repeater_status(session))
    assert status_calls == 0

    async def wrong_login(*args, **kwargs):
        return _event(meshcore.EventType.LOGIN_SUCCESS, {"pubkey_prefix": "deadbeef0000"})

    session.mc.commands.send_login_sync = wrong_login
    with pytest.raises(RuntimeError, match="another repeater"):
        asyncio.run(client._query_repeater_status(session))

    session.mc.commands.send_login_sync = Commands().send_login_sync

    async def no_status(*args, **kwargs):
        return None

    session.mc.commands.req_status_sync = no_status
    with pytest.raises(RuntimeError, match="status request timed out"):
        asyncio.run(client._query_repeater_status(session))

    async def wrong_status(*args, **kwargs):
        return {"pubkey_pre": "deadbeef0000", "bat": 4100}

    session.mc.commands.req_status_sync = wrong_status
    with pytest.raises(RuntimeError, match="status response was for another repeater"):
        asyncio.run(client._query_repeater_status(session))


@pytest.mark.parametrize("channel", [False, True])
def test_configured_dm_send_waits_behind_repeater_request_lock(channel, monkeypatch):
    meshcore = pytest.importorskip("meshcore")
    monkeypatch.setattr(mcc, "_DEFAULT_SEND_TIMEOUT", 0.03)
    key = "ac" * 32
    sent = threading.Event()

    class Commands:
        def __init__(self):
            self._mesh_request_lock = asyncio.Lock()

        async def send_msg(self, dst, msg, timestamp=None):
            sent.set()
            return _event(meshcore.EventType.MSG_SENT, {"expected_ack": b"\x01\x02\x03\x04"})

        async def send_chan_msg(self, chan, msg, timestamp=None):
            sent.set()
            return _event(meshcore.EventType.OK, {})

    commands = Commands()
    mc = type("MC", (), {"commands": commands})()
    client = mcc.RealMeshCoreClient(
        device="/dev/null", repeater_public_key=key, repeater_guest_password="guest",
        repeater_query_timeout=0.1,
    )
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    run = mcc._Run(client)
    run.loop = loop
    session = mcc._Session(run, mc)
    run.session = session
    client._run = run
    asyncio.run_coroutine_threadsafe(commands._mesh_request_lock.acquire(), loop).result(timeout=1)
    result = []
    sender = threading.Thread(
        target=lambda: result.append(
            client.commands.send_chan_msg(1, "reply") if channel
            else client.commands.send_msg("aabbcc", "reply")
        ), daemon=True
    )
    try:
        sender.start()
        assert not sent.wait(0.1), "DM bypassed the in-flight repeater request lock"
        loop.call_soon_threadsafe(commands._mesh_request_lock.release)
        sender.join(timeout=1)
        assert not sender.is_alive()
        assert sent.is_set()
        assert result[0].type is (mcc.EventType.OK if channel else mcc.EventType.MSG_SENT)
    finally:
        client._run = None
        if commands._mesh_request_lock.locked():
            loop.call_soon_threadsafe(commands._mesh_request_lock.release)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=1)


def test_repeater_status_coalesces_queries_caches_result_and_bounds_timeout(monkeypatch):
    key = "ac" * 32
    client = mcc.RealMeshCoreClient(
        device="/dev/null", repeater_public_key=key, repeater_guest_password="guest",
        repeater_query_timeout=0.02,
    )
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    run = mcc._Run(client)
    run.loop = loop
    session = mcc._Session(run, object())
    run.session = session
    client._run = run
    calls = 0

    async def slow_query(_session):
        nonlocal calls
        calls += 1
        await asyncio.sleep(10)
        return {"pubkey_pre": key[:12], "bat": 4000}

    monkeypatch.setattr(client, "_query_repeater_status", slow_query)
    try:
        assert client.repeater_status()[0] == "pending"
        assert client.repeater_status()[0] == "pending"
        assert calls <= 1
        assert _wait_until(lambda: client._repeater_future is None, timeout=2.0)
        assert calls == 1
        assert client.repeater_status()[0] == "unavailable"  # 30s failure floor

        client._repeater_last_attempt_at = None

        async def success(_session):
            return {"pubkey_pre": key[:12], "bat": 4000}

        monkeypatch.setattr(client, "_query_repeater_status", success)
        assert client.repeater_status()[0] == "pending"
        assert _wait_until(lambda: client._repeater_status is not None)
        state, status, age, _ = client.repeater_status()
        assert state == "ready" and status["bat"] == 4000 and age is not None
    finally:
        client._run = None
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=1)


def test_meshtastic_only_install_can_import_main_with_meshcore_absent():
    """The headline invariant (task 3B item A), promoted to a durable test
    per fix round 1: a Meshtastic-only install must be able to import
    `meshgarden.main` -- and everything it in turn imports -- with the
    optional `meshcore` package entirely absent. Previously verified only
    by hand (`python -c "..."`, recorded in task-3B-1-report.md); nothing
    pinned it until now."""
    result = _run_blocked_import_script()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


_ASSERT_MESHCORE_NOT_LOADED_SCRIPT = """
import sys

import meshgarden.main

assert "meshcore" not in sys.modules, (
    "importing meshgarden.main loaded the optional meshcore package into "
    "sys.modules even though no MeshCore transport was ever configured or "
    "constructed"
)
print("OK")
"""


def test_importing_main_does_not_load_the_installed_meshcore_package():
    """The other half of the claim the previous test leaves untested --
    and the one three docs and three code comments got wrong before this
    lazy-import follow-up (see `meshgarden/meshcore_client.py`'s "LAZY,
    GUARDED IMPORT" module docstring section): the previous test proves
    only that ABSENCE of the package is handled cleanly. It says nothing
    about what happens when the package IS installed -- which used to be
    "it gets imported anyway, regardless of the configured transport",
    because the guarded `import meshcore` ran at `meshcore_client.py`'s own
    module load time, and `main.py` imports that module unconditionally.

    The `meshcore` package genuinely IS installed in this dev/test
    environment (every other test in this file that isn't skipped relies
    on it), so this test is meaningful here. Run in a FRESH subprocess, for
    the same reason `_run_blocked_import_script` is (this process may
    already have imported `meshcore` via an earlier test in this same
    file, or via pytest's own collection of other test modules): it
    imports only `meshgarden.main` (never constructing a MeshCore
    transport -- `[radio] transport` defaults to `"meshtastic"`, and
    nothing here configures otherwise) and asserts `meshcore` never
    entered `sys.modules`. Reverting the lazy-import change (restoring the
    module-scope `try: import meshcore as _mc_pkg`) makes this fail, since
    `meshgarden.main`'s unconditional `from meshgarden.meshcore_client
    import build_meshcore_transport` would then reach that eager import
    every time, package present or not.
    """
    result = subprocess.run(
        [sys.executable, "-c", _ASSERT_MESHCORE_NOT_LOADED_SCRIPT],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_translate_event_maps_real_event_to_our_event_by_value():
    meshcore = pytest.importorskip("meshcore")
    real_event = _event(meshcore.EventType.CONTACT_MSG_RECV, {"pubkey_prefix": "aabbcc"})
    ours = mcc._translate_event(real_event)
    assert ours.type is mcc.EventType.CONTACT_MSG_RECV
    assert ours.payload == {"pubkey_prefix": "aabbcc"}


def test_translate_event_tolerates_a_non_dict_payload():
    meshcore = pytest.importorskip("meshcore")
    real_event = _event(meshcore.EventType.ACK, None)
    ours = mcc._translate_event(real_event)
    assert ours.payload == {}


# --- channel-name resolution / autoadd write: pure coroutine logic ---------
# (called directly via asyncio.run against a stub `mc`, bypassing the
# background threads entirely -- `_run_coro`'s cross-thread scheduling is
# exercised separately below, in the full connected-session test.)


def test_probe_channel_index_finds_the_matching_name():
    meshcore = pytest.importorskip("meshcore")

    class _Commands:
        async def send_device_query(self):
            return _event(meshcore.EventType.DEVICE_INFO, {"max_channels": 3})

        async def get_channel(self, idx):
            names = {0: "public", 1: "meshgarden", 2: "ops"}
            return _event(meshcore.EventType.CHANNEL_INFO, {"channel_name": names[idx]})

    class _MC:
        commands = _Commands()

    client = mcc.RealMeshCoreClient(device="/dev/null")
    assert asyncio.run(client._probe_channel_index(_MC(), "meshgarden")) == 1


def test_probe_channel_index_returns_none_when_not_found():
    meshcore = pytest.importorskip("meshcore")

    class _Commands:
        async def send_device_query(self):
            return _event(meshcore.EventType.DEVICE_INFO, {"max_channels": 2})

        async def get_channel(self, idx):
            return _event(meshcore.EventType.CHANNEL_INFO, {"channel_name": "other"})

    class _MC:
        commands = _Commands()

    client = mcc.RealMeshCoreClient(device="/dev/null")
    assert asyncio.run(client._probe_channel_index(_MC(), "meshgarden")) is None


def test_resolve_channel_index_returns_none_when_disabled_or_not_connected():
    client = mcc.RealMeshCoreClient(device="/dev/null")
    assert client.resolve_channel_index("") is None  # meshcore_channel = "" disables it
    assert client.resolve_channel_index("meshgarden") is None  # never connected: _mc is None


def test_ensure_autoadd_bit_writes_when_unset_preserving_other_bits():
    """Read-modify-write, preserving bits 1-4 (meshcore-notes.md section
    10): only bit 0x01 changes."""
    meshcore = pytest.importorskip("meshcore")
    written = {}

    class _Commands:
        async def get_autoadd_config(self):
            return _event(meshcore.EventType.AUTOADD_CONFIG, {"config": 0b00010})

        async def set_autoadd_config(self, value):
            written["value"] = value
            return _event(meshcore.EventType.OK, {})

    class _MC:
        commands = _Commands()

    client = mcc.RealMeshCoreClient(device="/dev/null")
    assert asyncio.run(client._probe_autoadd_bit(_MC())) is True
    assert written["value"] == 0b00011


def test_ensure_autoadd_bit_is_a_noop_when_already_set():
    meshcore = pytest.importorskip("meshcore")

    class _Commands:
        async def get_autoadd_config(self):
            return _event(meshcore.EventType.AUTOADD_CONFIG, {"config": 0b00011})

        async def set_autoadd_config(self, value):
            raise AssertionError("must not write when the bit is already set")

    class _MC:
        commands = _Commands()

    client = mcc.RealMeshCoreClient(device="/dev/null")
    assert asyncio.run(client._probe_autoadd_bit(_MC())) is True


def test_ensure_autoadd_bit_tolerates_a_pre_1_16_firmware_error():
    """meshcore-notes.md section 10: pre-1.16 firmware answers ERROR or
    times out; both are tolerated, never raised."""
    meshcore = pytest.importorskip("meshcore")

    class _Commands:
        async def get_autoadd_config(self):
            return _event(meshcore.EventType.ERROR, {"reason": "unsupported"})

        async def set_autoadd_config(self, value):
            raise AssertionError("must not be called")

    class _MC:
        commands = _Commands()

    client = mcc.RealMeshCoreClient(device="/dev/null")
    assert asyncio.run(client._probe_autoadd_bit(_MC())) is False


def test_ensure_autoadd_bit_false_when_not_connected():
    client = mcc.RealMeshCoreClient(device="/dev/null")
    assert client.ensure_autoadd_bit() is False


# --- radio clock set (fix: every connection sets the clock from host time) -

def test_probe_set_clock_sends_the_current_host_time():
    """`_probe_set_clock` is the raw async call, mirroring
    `_probe_autoadd_bit`/`_probe_channel_index`'s split: no interpretation,
    just `commands.set_time(epoch_seconds)` with a 4-byte little-endian
    epoch per the firmware, sent as a plain int here (the real library
    handles the packing -- see `commands/device.py::set_time`)."""
    meshcore = pytest.importorskip("meshcore")
    sent = {}

    class _Commands:
        async def set_time(self, val):
            sent["val"] = val
            return _event(meshcore.EventType.OK, {})

    class _MC:
        commands = _Commands()

    client = mcc.RealMeshCoreClient(device="/dev/null")
    result = asyncio.run(client._probe_set_clock(_MC()))
    assert result.type is meshcore.EventType.OK
    assert abs(sent["val"] - int(time.time())) <= 5


def test_set_radio_clock_logs_success_from_host_time(caplog):
    meshcore = pytest.importorskip("meshcore")

    class _Commands:
        async def set_time(self, val):
            return _event(meshcore.EventType.OK, {})

    class _MC:
        commands = _Commands()

    client = mcc.RealMeshCoreClient(device="/dev/null")
    session = mcc._Session(None, _MC())
    with caplog.at_level(logging.INFO, logger="meshgarden.meshcore_client"):
        asyncio.run(client._set_radio_clock(session))
    assert any(
        "clock set from host time" in r.getMessage() for r in caplog.records
    ), caplog.text


def test_set_radio_clock_logs_distinctly_when_the_firmware_refuses_a_backwards_move(caplog):
    """The firmware answers `ERR_CODE_ILLEGAL_ARG` (error code 6) when the
    requested time is earlier than its own current clock -- expected on a
    radio whose clock is already ahead of host time. This must be logged
    as an expected outcome, not a failure, and distinctly from any other
    rejection so an operator can tell the two apart in the journal."""
    meshcore = pytest.importorskip("meshcore")

    class _Commands:
        async def set_time(self, val):
            return _event(
                meshcore.EventType.ERROR,
                {"error_code": 6, "code_string": "ERR_CODE_ILLEGAL_ARG"},
            )

    class _MC:
        commands = _Commands()

    client = mcc.RealMeshCoreClient(device="/dev/null")
    session = mcc._Session(None, _MC())
    with caplog.at_level(logging.INFO, logger="meshgarden.meshcore_client"):
        asyncio.run(client._set_radio_clock(session))
    messages = [r.getMessage() for r in caplog.records]
    assert any("ahead of host time" in m and "refused" in m for m in messages), caplog.text
    # Distinct from a genuine failure: no "rejected"/"failed" wording here.
    assert not any("rejected" in m or "failed" in m for m in messages)


def test_set_radio_clock_logs_a_warning_for_a_non_backwards_rejection(caplog):
    """Any OTHER error code (or a raised exception) is a genuine failure,
    not the expected "clock is ahead" case, and must be distinguishable
    from it in the log."""
    meshcore = pytest.importorskip("meshcore")

    class _Commands:
        async def set_time(self, val):
            return _event(
                meshcore.EventType.ERROR,
                {"error_code": 1, "code_string": "ERR_CODE_UNSUPPORTED_CMD"},
            )

    class _MC:
        commands = _Commands()

    client = mcc.RealMeshCoreClient(device="/dev/null")
    session = mcc._Session(None, _MC())
    with caplog.at_level(logging.WARNING, logger="meshgarden.meshcore_client"):
        asyncio.run(client._set_radio_clock(session))
    assert any("rejected" in r.getMessage() for r in caplog.records), caplog.text


def test_set_radio_clock_swallows_an_exception_from_the_transport():
    """Matches `_write_autoadd_bit`'s own contract: a raised exception
    (e.g. a transport error mid-write) is logged and swallowed, never
    propagated -- `_post_connect_setup` runs as a task on the session
    loop, so an escaping exception here must not tear down an otherwise
    working connection."""

    class _Commands:
        async def set_time(self, val):
            raise RuntimeError("boom: simulated serial write failure")

    class _MC:
        commands = _Commands()

    client = mcc.RealMeshCoreClient(device="/dev/null")
    session = mcc._Session(None, _MC())
    asyncio.run(client._set_radio_clock(session))  # must not raise


# --- message fetching (fix: the companion firmware queues inbound
# messages behind a MESSAGES_WAITING push; the client must fetch them) ------


def test_start_message_fetching_calls_the_real_library_api():
    """Real-hardware finding: subscribing to CONTACT_MSG_RECV/
    CHANNEL_MSG_RECV and merely waiting produced zero events for ten
    seconds; calling `start_auto_message_fetching()` immediately delivered
    a queued message. This pins that `_start_message_fetching` reaches
    that exact library call."""
    calls = []

    class _MC:
        async def start_auto_message_fetching(self):
            calls.append(1)

    client = mcc.RealMeshCoreClient(device="/dev/null")
    session = mcc._Session(None, _MC())
    asyncio.run(client._start_message_fetching(session))
    assert calls == [1]


def test_start_message_fetching_swallows_an_exception_from_the_transport():
    """Matches `_set_radio_clock`/`_write_autoadd_bit`'s own contract: a
    raised exception is logged and swallowed, never propagated -- this
    runs inline in `_run_connected_session_body`, right after subscriptions
    are wired, so an escaping exception here must not tear down the
    session before it even starts receiving."""

    class _MC:
        async def start_auto_message_fetching(self):
            raise RuntimeError("boom: simulated serial failure")

    client = mcc.RealMeshCoreClient(device="/dev/null")
    session = mcc._Session(None, _MC())
    asyncio.run(client._start_message_fetching(session))  # must not raise


def test_start_message_fetching_is_bounded_by_the_setup_timeout():
    """Item 3 fix: unlike its siblings (`_set_radio_clock`,
    `_write_autoadd_bit`, `_send_advert_on_connect`), this call used to be
    awaited inline with no `asyncio.wait_for(..., self._setup_timeout)`
    bound -- it ends in the library's own `get_msg()`, which can block for
    its fifteen-second default, delaying `run.ready_event.set()` by that
    much. Now bounded the same way its siblings are: a fetch that hangs
    past `setup_timeout` is logged and swallowed, not awaited forever."""

    class _MC:
        async def start_auto_message_fetching(self):
            await asyncio.sleep(10)

    client = mcc.RealMeshCoreClient(device="/dev/null", setup_timeout=0.05)
    session = mcc._Session(None, _MC())
    started = time.monotonic()
    asyncio.run(client._start_message_fetching(session))  # must not hang or raise
    assert time.monotonic() - started < 5.0


# --- self-advertisement (task 3B-4 fix 2: the garden was unreachable until
# manually flood-advertised) -------------------------------------------------


def test_probe_send_advert_sends_a_flood_advertisement():
    meshcore = pytest.importorskip("meshcore")
    calls = []

    class _Commands:
        async def send_advert(self, flood=False):
            calls.append(flood)
            return _event(meshcore.EventType.OK, {})

    class _MC:
        commands = _Commands()

    client = mcc.RealMeshCoreClient(device="/dev/null")
    result = asyncio.run(client._probe_send_advert(_MC()))
    assert result.type is meshcore.EventType.OK
    assert calls == [True]


def test_send_advert_on_connect_swallows_an_exception_from_the_transport():
    class _Commands:
        async def send_advert(self, flood=False):
            raise RuntimeError("boom: simulated serial write failure")

    class _MC:
        commands = _Commands()

    client = mcc.RealMeshCoreClient(device="/dev/null")
    session = mcc._Session(None, _MC())
    asyncio.run(client._send_advert_on_connect(session))  # must not raise


def test_send_advert_false_when_not_connected():
    client = mcc.RealMeshCoreClient(device="/dev/null")
    assert client.send_advert() is False


# --- full connected-session test: real threads, monkeypatched hardware ----


class _StubMC:
    """Stands in for the real `meshcore.MeshCore` object `create_serial`
    would normally return -- exercises `RealMeshCoreClient`'s actual
    threading/queue/subscribe wiring against something shaped exactly like
    the real library's own `subscribe`/`Event` surface (constructed with
    the REAL `meshcore.Event`/`EventType`), without opening a real serial
    port.
    """

    def __init__(self, self_info: dict, contacts: dict | None = None) -> None:
        self.self_info = self_info
        self._subs: dict = {}
        self.commands = self._Commands()
        # The radio's OWN stored contact list, exactly as the real
        # `MeshCore.contacts` exposes it: keyed by full public-key hex,
        # each value carrying `public_key`/`adv_name`/`adv_lat`/`adv_lon`
        # (meshcore/reader.py builds these and NEW_CONTACT's payload from
        # the same parser branch). Populated by `ensure_contacts()` in the
        # real library; pre-populated here, since there is no device.
        self.contacts = dict(contacts or {})
        self.ensure_contacts_calls = 0
        # Task 3B-4 fix 1: models `meshcore.MeshCore.start_auto_message_
        # fetching()` -- `True` once `RealMeshCoreClient` has called it for
        # this session.
        self.auto_fetch_started = False

    async def ensure_contacts(self, follow: bool = False) -> bool:
        self.ensure_contacts_calls += 1
        return True

    async def start_auto_message_fetching(self) -> None:
        self.auto_fetch_started = True

    class _Commands:
        def __init__(self) -> None:
            self.set_time_calls: list[int] = []
            self._mesh_request_lock = asyncio.Lock()
            # Task 3B-4 fix 2: every `flood` argument `send_advert` was
            # called with, in order.
            self.send_advert_calls: list[bool] = []

        async def send_msg(self, dst, msg, timestamp=None):
            import meshcore

            return _event(
                meshcore.EventType.MSG_SENT,
                {"expected_ack": b"\x01\x02\x03\x04", "suggested_timeout": 5000},
            )

        async def send_advert(self, flood: bool = False):
            import meshcore

            self.send_advert_calls.append(flood)
            return _event(meshcore.EventType.OK, {})

        async def send_device_query(self):
            import meshcore

            return _event(meshcore.EventType.DEVICE_INFO, {"max_contacts": 350})

        async def remove_contact(self, key):
            import meshcore

            self.removed = getattr(self, "removed", []) + [key]
            return _event(meshcore.EventType.OK, {})

        async def set_time(self, val):
            import meshcore

            self.set_time_calls.append(val)
            return _event(meshcore.EventType.OK, {})

    def subscribe(self, event_type, callback, attribute_filters=None):
        self._subs.setdefault(event_type, []).append(callback)
        return object()

    def fire(self, event_type, payload) -> None:

        event = _event(event_type, payload)
        for cb in list(self._subs.get(event_type, [])):
            cb(event)

    async def disconnect(self) -> None:
        return None


def test_a_connected_session_sets_the_radios_clock_from_host_time(monkeypatch):
    """End-to-end (fix): every connected session sets the radio's clock,
    unconditionally -- unlike the autoadd write, this needs no
    `meshcore_autoadd_overwrite` opt-in."""
    meshcore = pytest.importorskip("meshcore")
    stub = _StubMC({"public_key": "aa" * 32})

    async def fake_create_serial(cls, *args, **kwargs):
        return stub

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null", backoff_base=0.01, backoff_max=0.02, loop_start_timeout=5.0
    )
    client.connect()
    try:
        assert client.wait_ready(5.0)
        assert client.wait_setup(5.0), "per-connection setup never completed"
        assert len(stub.commands.set_time_calls) == 1
        assert abs(stub.commands.set_time_calls[0] - int(time.time())) <= 5
    finally:
        client.stop()


def test_a_connected_session_starts_auto_message_fetching(monkeypatch):
    """End-to-end (fix 1): every connected session starts the real
    library's queue-drain for inbound messages. Real-hardware evidence:
    subscribing to CONTACT_MSG_RECV/CHANNEL_MSG_RECV and merely waiting
    produced zero events for ten seconds; calling
    `start_auto_message_fetching()` immediately delivered a queued
    message."""
    meshcore = pytest.importorskip("meshcore")
    stub = _StubMC({"public_key": "aa" * 32})

    async def fake_create_serial(cls, *args, **kwargs):
        return stub

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null", backoff_base=0.01, backoff_max=0.02, loop_start_timeout=5.0
    )
    client.connect()
    try:
        assert client.wait_ready(5.0)
        assert stub.auto_fetch_started is True
    finally:
        client.stop()


def test_a_connected_session_sends_one_flood_self_advertisement(monkeypatch):
    """End-to-end (fix 2, hardware finding: the garden was unreachable
    until manually flood-advertised): every connected session sends one
    `commands.send_advert(flood=True)`, unconditionally, same as the clock
    set above."""
    meshcore = pytest.importorskip("meshcore")
    stub = _StubMC({"public_key": "aa" * 32})

    async def fake_create_serial(cls, *args, **kwargs):
        return stub

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null", backoff_base=0.01, backoff_max=0.02, loop_start_timeout=5.0
    )
    client.connect()
    try:
        assert client.wait_ready(5.0)
        assert client.wait_setup(5.0), "per-connection setup never completed"
        assert stub.commands.send_advert_calls == [True]

        # The public method (main.py's periodic repeat) reaches the same
        # command, cross-thread -- but item 2's floor (_ADVERT_MIN_GAP_
        # SECONDS) suppresses it this soon after the connect-time advert.
        assert client.send_advert() is False
        assert stub.commands.send_advert_calls == [True]

        # Once the floor has elapsed, the next trigger goes through.
        client._last_advert_monotonic -= mcc.RealMeshCoreClient._ADVERT_MIN_GAP_SECONDS
        assert client.send_advert() is True
        assert stub.commands.send_advert_calls == [True, True]
    finally:
        client.stop()


def test_advert_floor_suppresses_a_flood_advertisement_within_the_gap(monkeypatch):
    """Item 2 fix: a reconnect loop whose sessions each just clear
    `min_stable_session_seconds` must not re-flood the mesh with an advert
    every cycle. The floor is enforced in the client (`_send_advert_if_due`)
    off a monotonic timestamp alone, regardless of which caller -- here,
    two connections in quick succession -- triggers it."""
    meshcore = pytest.importorskip("meshcore")
    calls: list[bool] = []

    class _Commands:
        async def send_advert(self, flood=False):
            calls.append(flood)
            return _event(meshcore.EventType.OK, {})

    class _MC:
        commands = _Commands()

    client = mcc.RealMeshCoreClient(device="/dev/null")
    session = mcc._Session(None, _MC())

    now = [1000.0]
    monkeypatch.setattr(mcc.time, "monotonic", lambda: now[0])

    # First connection: sends.
    asyncio.run(client._send_advert_on_connect(session))
    assert calls == [True]

    # A second connection arriving well inside the floor: no second advert.
    now[0] += 5.0
    asyncio.run(client._send_advert_on_connect(session))
    assert calls == [True]

    # Once the floor has elapsed, the next trigger goes through.
    now[0] += mcc.RealMeshCoreClient._ADVERT_MIN_GAP_SECONDS
    asyncio.run(client._send_advert_on_connect(session))
    assert calls == [True, True]


def test_real_client_connects_and_forwards_events_through_the_queue(monkeypatch):
    """The end-to-end threading/queue design (task 3B item C): a
    background asyncio-loop thread "connects" (here, a monkeypatched
    `MeshCore.create_serial` standing in for hardware) and a separate pump
    thread drains the translated events to a synchronously-registered
    subscriber -- exactly the shape `MeshCoreTransport.connect()` relies
    on."""
    meshcore = pytest.importorskip("meshcore")
    stub = _StubMC({"public_key": "aa" * 32})

    async def fake_create_serial(cls, *args, **kwargs):
        return stub

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null", backoff_base=0.01, backoff_max=0.02, loop_start_timeout=5.0
    )
    seen = []
    connected_events = []
    self_info_events = []
    client.subscribe(mcc.EventType.ADVERTISEMENT, seen.append)
    client.subscribe(mcc.EventType.CONNECTED, connected_events.append)
    client.subscribe(mcc.EventType.SELF_INFO, self_info_events.append)
    client.connect()
    try:
        assert client.wait_ready(5.0), "client never reached a connected session"
        assert client.self_info == {"public_key": "aa" * 32}
        assert client.connected is True

        # Finding A (fix round 1): `create_serial` above already "fired"
        # CONNECTED/SELF_INFO internally (the real library's own send_
        # appstart handshake) before we could possibly have subscribed --
        # this must still reach a subscriber, via the synthesized event
        # `_run_connected_session` puts on the queue right after wiring.
        deadline = time.monotonic() + 5.0
        while (not connected_events or not self_info_events) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(connected_events) == 1
        assert len(self_info_events) == 1
        assert self_info_events[0].payload == {"public_key": "aa" * 32}

        stub.fire(meshcore.EventType.ADVERTISEMENT, {"public_key": "bb" * 32})

        deadline = time.monotonic() + 5.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(seen) == 1
        assert seen[0].type is mcc.EventType.ADVERTISEMENT
        assert seen[0].payload == {"public_key": "bb" * 32}

        # `_RealCommands.send_msg` schedules the real async `send_msg` onto
        # the loop thread and blocks for the translated result -- the same
        # synchronous call shape `MeshCoreTransport.send_dm` expects.
        sent = client.commands.send_msg("aabbcc", "hi")
        assert sent.type is mcc.EventType.MSG_SENT
        assert sent.payload["expected_ack"] == b"\x01\x02\x03\x04"
    finally:
        client.stop()


def test_real_client_retries_with_the_repos_own_backoff_when_connect_fails(monkeypatch):
    """The OUTER reconnect loop (task 3B item C): when the library's own
    connect attempt fails outright (`create_serial` returns `None`), this
    client retries using `meshgarden.radio.backoff_delays`, not the
    library's own (inapplicable, since there is no session yet) retry
    logic."""
    meshcore = pytest.importorskip("meshcore")
    attempts = []

    async def failing_create_serial(cls, *args, **kwargs):
        attempts.append(1)
        return None

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(failing_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null", backoff_base=0.01, backoff_max=0.02, loop_start_timeout=5.0
    )
    client.connect()
    try:
        deadline = time.monotonic() + 2.0
        while len(attempts) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(attempts) >= 3, "outer loop did not retry after a failed connect"
        assert client.connected is False
        assert not client.wait_ready(0.1)
    finally:
        client.stop()


def test_commands_send_msg_raises_not_connected_before_any_session():
    client = mcc.RealMeshCoreClient(device="/dev/null")
    with pytest.raises(mcc.MeshCoreNotConnected):
        client.commands.send_msg("aabbcc", "hi")


def test_connect_is_idempotent_a_second_call_starts_no_new_threads(monkeypatch):
    """Finding B (fix round 1): a second `connect()` call must not start a
    second asyncio-loop thread and a second pump thread -- two event loops
    racing one serial device, two pumps racing one queue, and a `stop()`
    that can only join one of each."""
    meshcore = pytest.importorskip("meshcore")
    stub = _StubMC({"public_key": "aa" * 32})

    async def fake_create_serial(cls, *args, **kwargs):
        return stub

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null", backoff_base=0.01, backoff_max=0.02, loop_start_timeout=5.0
    )
    client.connect()
    try:
        assert client.wait_ready(5.0)
        run = client._run
        first_loop_thread = run.loop_thread
        first_pump_thread = run.pump_thread

        client.connect()  # second call: must be a no-op

        assert client._run is run
        assert client._run.loop_thread is first_loop_thread
        assert client._run.pump_thread is first_pump_thread
        assert first_loop_thread.is_alive()
        assert first_pump_thread.is_alive()
    finally:
        client.stop()


def test_real_client_recovers_from_an_exception_during_a_connected_session(monkeypatch):
    """Finding C (fix round 1): an exception raised while a session is
    "connected" (here, simulated via a `subscribe` that always raises,
    standing in for a wiring failure) must not kill the asyncio-loop
    thread outright with `connected` stuck `True` forever. The outer loop
    must recover: `connected` goes back to `False`, and it keeps retrying
    (proven by `create_serial` being called again) rather than the thread
    silently dying.
    """
    meshcore = pytest.importorskip("meshcore")
    attempts = []

    class _BadMC:
        def __init__(self) -> None:
            self.self_info = {"public_key": "aa" * 32}

        def subscribe(self, *args, **kwargs):
            raise RuntimeError("boom: simulated wiring failure")

        async def disconnect(self) -> None:
            return None

    async def fake_create_serial(cls, *args, **kwargs):
        attempts.append(1)
        return _BadMC()

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null", backoff_base=0.01, backoff_max=0.02, loop_start_timeout=5.0
    )
    client.connect()
    try:
        deadline = time.monotonic() + 3.0
        while len(attempts) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(attempts) >= 3, "outer loop died instead of retrying after a wiring exception"
        assert client.connected is False, "connected must not stay stuck True"
        assert not client.wait_ready(0.1), "a broken session must never look ready"
    finally:
        client.stop()


def test_build_meshcore_transport_connects_exactly_once_and_resolves_the_channel(monkeypatch):
    """The seam `build_meshcore_transport` exposes had no test at all
    before fix round 1, which is how findings A and B both shipped. This
    covers it end to end with a monkeypatched `create_serial`: exactly one
    connected session is ever started (not two, from `build_meshcore_
    transport` connecting the client and then a caller connecting the
    returned transport again), and the configured channel name is resolved
    to the transport's `_watched_channel_idx` once ready.
    """
    meshcore = pytest.importorskip("meshcore")
    from meshgarden.transport import ChannelScope

    connect_calls = []

    class _Commands:
        async def send_device_query(self):
            return _event(meshcore.EventType.DEVICE_INFO, {"max_channels": 2})

        async def get_channel(self, idx):
            names = {0: "public", 1: "meshgarden"}
            return _event(meshcore.EventType.CHANNEL_INFO, {"channel_name": names[idx]})

    class _MC:
        def __init__(self) -> None:
            self.self_info = {"public_key": "cc" * 32}
            self.commands = _Commands()
            self._subs: dict = {}
            self.contacts: dict = {}

        async def ensure_contacts(self, follow: bool = False) -> bool:
            return True

        def subscribe(self, event_type, callback, attribute_filters=None):
            connect_calls.append(1)
            self._subs.setdefault(event_type, []).append(callback)
            return object()

        async def disconnect(self):
            return None

    async def fake_create_serial(cls, *args, **kwargs):
        return _MC()

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    transport = mcc.build_meshcore_transport(
        device="/dev/null",
        expected_node_id="!cccccccc",
        channel_name="meshgarden",
        connect_timeout=5.0,
    )
    try:
        # Exactly one connected session: `_wire_subscriptions` (9 calls) +
        # the internal DISCONNECTED watcher (1 call) = 10 `subscribe` calls.
        # A second `connect()` (the double-connect finding B fixed) would
        # double this.
        assert len(connect_calls) == 10

        seen = []
        transport.on_message(seen.append)
        # Feeds the fake's own channel-index resolution back through the
        # SAME _MC instance the transport is wired to, proving
        # `set_watched_channel_idx` actually took effect end to end.
        from meshgarden.meshcore_transport import Event as OurEvent
        from meshgarden.meshcore_transport import EventType as OurEventType

        transport._on_new_contact(
            OurEvent(OurEventType.NEW_CONTACT, {"public_key": "cc" * 32, "adv_name": "alice"})
        )
        transport._on_channel_msg(
            OurEvent(
                OurEventType.CHANNEL_MSG_RECV,
                {
                    "channel_idx": 1,
                    "text": "alice: !garden",
                    "sender_timestamp": 0,
                    "path_len": 255,
                },
            )
        )
        assert len(seen) == 1
        assert seen[0].channel is ChannelScope.WATCHED
    finally:
        transport._client.stop()


def test_a_connected_session_seeds_the_radios_stored_contacts(monkeypatch):
    """Whole-branch review, finding 1. The library delivers NO event for a
    contact the radio already knows -- connecting loads none, and the
    NEW_CONTACT push covers only contacts the radio did not previously
    know -- so the roster must be seeded from `MeshCore.contacts` after
    `MeshCore.ensure_contacts()`, synthesized onto the run's own queue as
    NEW_CONTACT-shaped events. Verified against the installed
    `meshcore==2.3.11`: `ensure_contacts()` issues
    `commands.get_contacts(lastmod=...)` and awaits the library's own
    `CONTACTS` event, whose internal handler fills `MeshCore.contacts`; no
    ninth subscription of ours is involved.
    """
    meshcore = pytest.importorskip("meshcore")
    stored = {
        "aa" * 32: {
            "public_key": "aa" * 32,
            "adv_name": "alice",
            "adv_lat": 1.5,
            "adv_lon": -2.5,
        },
        "bb" * 32: {"public_key": "bb" * 32, "adv_name": "bob"},
    }
    stub = _StubMC({"public_key": "cc" * 32}, contacts=stored)

    async def fake_create_serial(cls, *args, **kwargs):
        return stub

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null", backoff_base=0.01, backoff_max=0.02, loop_start_timeout=5.0
    )
    seen: list = []
    client.subscribe(mcc.EventType.NEW_CONTACT, seen.append)
    client.connect()
    try:
        assert client.wait_ready(5.0)
        assert client.wait_setup(5.0), "per-connection setup never completed"
        deadline = time.monotonic() + 5.0
        while len(seen) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert stub.ensure_contacts_calls == 1
        assert {e.payload["public_key"] for e in seen} == set(stored)
        alice = next(e for e in seen if e.payload["adv_name"] == "alice")
        # The full NEW_CONTACT payload shape, not just the key: this is
        # what carries a contact's name and position to the nodes table.
        assert alice.payload["adv_lat"] == 1.5
        assert alice.payload["adv_lon"] == -2.5
    finally:
        client.stop()


def test_builder_delivers_imported_contacts_to_consumers_registered_before_connect(monkeypatch):
    meshcore = pytest.importorskip("meshcore")
    stored = {
        "aa" * 32: {"public_key": "aa" * 32, "adv_name": "apple", "adv_lat": 1.5, "adv_lon": -2.5}
    }
    stub = _StubMC({"public_key": "cc" * 32}, contacts=stored)

    async def fake_create_serial(cls, *args, **kwargs):
        return stub

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))
    contacts, observations, messages = [], [], []
    received = threading.Event()

    def on_contact(*args):
        contacts.append(args)
        received.set()

    transport = mcc.build_meshcore_transport(
        device="/dev/null",
        expected_node_id="!cccccccc",
        connect_timeout=5.0,
        on_contact=on_contact,
        on_observation=observations.append,
        on_message=messages.append,
    )
    try:
        assert received.wait(5.0)
        assert contacts == [("!aaaaaaaa", "apple", 1.5, -2.5)]
        assert observations == []  # Imported contacts are not live receptions.
        transport._on_channel_msg(
            mcc.Event(
                mcc.EventType.CHANNEL_MSG_RECV,
                {
                    "channel_idx": 0,
                    "text": "apple: !garden",
                    "sender_timestamp": int(time.time()),
                },
            )
        )
        assert len(observations) == 1
        assert len(messages) == 1
        assert messages[0].sender == "!aaaaaaaa"
    finally:
        transport.client.stop()


@pytest.mark.parametrize("fail_first_refresh", [False, True])
def test_advertisement_refreshes_channel_sender_name_without_reconnect(
    monkeypatch, fail_first_refresh
):
    meshcore = pytest.importorskip("meshcore")
    from meshgarden.commands import Outcome, decide_inbound
    from meshgarden.transport import ChannelTriggerPolicy

    public_key = "a1b2c3d4" + "19" * 28
    current_contact = {"public_key": public_key, "adv_name": "🌀Node23"}

    class UpdatingRadio(_StubMC):
        follow_calls = 0

        async def ensure_contacts(self, follow=False):
            if follow:
                self.follow_calls += 1
                if fail_first_refresh and self.follow_calls == 1:
                    raise TimeoutError("temporary serial failure")
                self.contacts[public_key] = dict(current_contact)
            return True

    stub = UpdatingRadio({"public_key": "cc" * 32}, contacts={
        public_key: {"public_key": public_key, "adv_name": "old name"},
    })

    async def create_serial(cls, *args, **kwargs):
        return stub

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(create_serial))
    contacts, observations, messages = [], [], []
    named = threading.Event()

    def on_contact(*args):
        contacts.append(args)
        if args[1] == "🌀Node23":
            named.set()

    transport = mcc.build_meshcore_transport(
        device="/dev/null", expected_node_id="!cccccccc", connect_timeout=5,
        on_contact=on_contact, on_observation=observations.append, on_message=messages.append,
    )
    transport.set_watched_channel_idx(1)
    knock = mcc.Event(mcc.EventType.CHANNEL_MSG_RECV, {
        "channel_idx": 1, "text": "🌀Node23: !garden", "sender_timestamp": int(time.time()),
    })
    try:
        transport._on_channel_msg(knock)
        assert messages == []
        # Real SDK callbacks run on the client's asyncio loop, not the test thread.
        transport.client._run.loop.call_soon_threadsafe(
            stub.fire, meshcore.EventType.ADVERTISEMENT, {"public_key": public_key}
        )
        assert named.wait(9), "stored contact name was never refreshed after its advertisement"
        transport._on_channel_msg(knock)
        assert len(messages) == 1
        assert messages[0].sender == "!a1b2c3d4"
        decision = decide_inbound(
            messages[0], me="!cccccccc", reply_ready=True,
            channel_policy=ChannelTriggerPolicy(), connected_seconds=1000,
        )
        assert decision.outcome is Outcome.KNOCK
        assert [c[1] for c in contacts] == ["old name", "🌀Node23"]
        assert len([o for o in observations if o.kind == "MESHCORE_ADVERT"]) == 1
        # Re-reading the same metadata must not manufacture contact changes.
        assert transport.client._publish_contact_metadata(
            transport.client._run, transport.client._run.session
        ) == 0
    finally:
        transport.client.stop()


def test_the_channel_is_resolved_on_a_later_connection_when_the_first_connect_fails(
    monkeypatch, caplog
):
    """Whole-branch review, finding 3: the startup case. A daemon started
    by systemd before its USB serial device enumerates (the ordinary
    Raspberry Pi case) used to miss channel resolution entirely -- the
    builder waited for the first connect, gave up SILENTLY, resolved
    nothing, and channel triggering stayed off for the life of the process
    even though the reconnect loop succeeded moments later.

    Here `create_serial` fails until `gate` is set, so the builder's wait
    times out exactly as it would on that Pi; the channel must still be
    resolved by the connection that eventually succeeds, and the interim
    state must be LOGGED rather than invisible.
    """
    meshcore = pytest.importorskip("meshcore")
    gate = threading.Event()

    class _Commands:
        async def send_device_query(self):
            return _event(meshcore.EventType.DEVICE_INFO, {"max_channels": 2})

        async def get_channel(self, idx):
            names = {0: "public", 1: "meshgarden"}
            return _event(meshcore.EventType.CHANNEL_INFO, {"channel_name": names[idx]})

    class _MC:
        def __init__(self) -> None:
            self.self_info = {"public_key": "cc" * 32}
            self.commands = _Commands()
            self.contacts: dict = {}

        async def ensure_contacts(self, follow: bool = False) -> bool:
            return True

        def subscribe(self, event_type, callback, attribute_filters=None):
            return object()

        async def disconnect(self):
            return None

    async def fake_create_serial(cls, *args, **kwargs):
        if not gate.is_set():
            return None  # the serial device has not enumerated yet
        return _MC()

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    with caplog.at_level(logging.WARNING, logger="meshgarden.meshcore_client"):
        transport = mcc.build_meshcore_transport(
            device="/dev/null",
            expected_node_id="!cccccccc",
            channel_name="meshgarden",
            connect_timeout=0.3,
            backoff_base=0.01,
            backoff_max=0.02,
        )
    try:
        assert transport._watched_channel_idx is None
        assert any("channel triggering is OFF until" in r.getMessage() for r in caplog.records), (
            "the daemon ran with channel triggering off and said nothing about it"
        )

        gate.set()  # the device appears; the reconnect loop takes it from here
        deadline = time.monotonic() + 10.0
        while transport._watched_channel_idx is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert transport._watched_channel_idx == 1, (
            "channel resolution never ran again after the initial wait timed out"
        )
    finally:
        transport._client.stop()


def test_run_connected_session_disconnects_even_when_setup_raises(monkeypatch):
    """Finding 1 (fix round 2): an exception raised anywhere in the
    session-SETUP window -- wiring subscriptions, the `self_info` read,
    the DISCONNECTED-watcher `subscribe` call -- must not skip `mc.
    disconnect()`. Before round 1's fix that leaked once and killed the
    thread; after it, the outer loop retries forever, so every iteration
    leaked another open serial connection AND left the orphaned `mc`'s
    already-wired forwarding callbacks able to keep pushing events into
    the SAME queue a later, live session also uses -- duplicate inbound
    events, i.e. duplicate replies for a DM-only service. This test
    forces the failure at the `self_info` read (after `_wire_subscriptions`
    has already succeeded, so the leak window is real) and checks both
    halves the review asked for: `disconnect()` was actually called, and a
    stub whose `disconnect()` faithfully models the real library's
    guarantee (no further dispatch afterward) delivers nothing once it has
    been called.
    """
    meshcore = pytest.importorskip("meshcore")
    disconnect_calls = []
    created: list[_BadMC] = []

    class _BadMC:
        def __init__(self) -> None:
            self._subs: dict = {}
            self.disconnected = False

        @property
        def self_info(self):
            raise RuntimeError("boom: simulated self-info read failure")

        def subscribe(self, event_type, callback, attribute_filters=None):
            self._subs.setdefault(event_type, []).append(callback)
            return object()

        def fire(self, event_type, payload) -> None:
            if self.disconnected:
                return  # a real disconnect() stops the library's dispatcher entirely
            for cb in list(self._subs.get(event_type, [])):
                cb(_event(event_type, payload))

        async def disconnect(self) -> None:
            disconnect_calls.append(1)
            self.disconnected = True

    async def fake_create_serial(cls, *args, **kwargs):
        mc = _BadMC()
        created.append(mc)
        return mc

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null", backoff_base=0.01, backoff_max=0.02, loop_start_timeout=5.0
    )
    seen = []
    client.subscribe(mcc.EventType.ADVERTISEMENT, seen.append)
    client.connect()
    try:
        deadline = time.monotonic() + 3.0
        while not disconnect_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert disconnect_calls, "disconnect() was never called after a setup-window exception"

        first_bad_mc = created[0]
        assert first_bad_mc.disconnected is True

        # No leaked/duplicate delivery: the dead session's own (already-
        # wired) forwarding callback must deliver nothing once its
        # disconnect() has run -- exactly what the real library's own
        # dispatcher.stop()/connection close guarantees, and what this
        # stub models.
        first_bad_mc.fire(meshcore.EventType.ADVERTISEMENT, {"public_key": "bb" * 32})
        time.sleep(0.05)
        assert seen == [], "a disconnected session must never deliver another event"
    finally:
        client.stop()


def test_backoff_escalates_across_repeated_short_sessions(monkeypatch):
    """Finding 2 (fix round 2): a session that ends almost immediately,
    over and over, must make the outer loop's retry delay escalate the
    same way a repeated `create_serial` failure already does. Previously
    `attempt` was unconditionally pinned to `1` after every session end,
    so this rebuilt once per `backoff_base` (one second, by default)
    forever instead of backing off -- combined with finding 1, that was
    one leaked serial connection per second.
    """
    meshcore = pytest.importorskip("meshcore")
    attempt_times: list[float] = []

    class _BadMC:
        @property
        def self_info(self):
            raise RuntimeError("boom: force an immediate session end, every time")

        def subscribe(self, event_type, callback, attribute_filters=None):
            return object()

        async def disconnect(self) -> None:
            return None

    async def fake_create_serial(cls, *args, **kwargs):
        attempt_times.append(time.monotonic())
        return _BadMC()

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null", backoff_base=0.02, backoff_max=1.0, loop_start_timeout=5.0
    )
    client.connect()
    try:
        deadline = time.monotonic() + 8.0
        while len(attempt_times) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(attempt_times) >= 5, (
            f"outer loop did not retry enough times to observe escalation: {attempt_times}"
        )
        gaps = [b - a for a, b in zip(attempt_times, attempt_times[1:], strict=False)]
        # backoff_delays(0.02, 1.0, attempt)[-1] for attempt=1,2,3,4 is
        # 0.02, 0.04, 0.08, 0.16 -- an 8x spread. Before the fix every gap
        # was pinned to ~0.02s (attempt hardcoded to 1 every time).
        assert gaps[-1] > gaps[0] * 1.5, f"backoff never escalated: gaps={gaps}"
    finally:
        client.stop()


def test_stop_timeout_followed_by_connect_leaves_no_stranded_live_source(monkeypatch):
    """Fix round 3, THE CRITICAL finding: `stop()`'s join is timed (here,
    deliberately shorter than a slow `create_serial` call), so it can
    return while the loop thread is still blocked inside that call.
    Before the generation-token redesign, that stranded thread's blocked
    call would eventually return, read a shared `_stop_event` the timed-
    out `stop()` had already cleared (so it could allow a genuine restart
    -- fix round 2's finding 4), and wire itself up as a live session --
    a SECOND source feeding the same queue as whatever a following
    `connect()` had since started: duplicate `CONNECTED` dispatch,
    duplicate everything else, and two event loops owning one serial
    device. With the redesign, the stranded run's own `stop_event` was
    set once by `stop()` and is never touched again by anyone -- when its
    blocked `create_serial()` call finally returns, it disconnects
    immediately without ever wiring a single subscription.
    """
    meshcore = pytest.importorskip("meshcore")
    created: list[_TrackedMC] = []
    call_count = 0

    class _TrackedMC:
        def __init__(self) -> None:
            self.self_info = {"public_key": "aa" * 32}
            self.subscribe_calls = 0
            self.disconnect_calls = 0

        def subscribe(self, event_type, callback, attribute_filters=None):
            self.subscribe_calls += 1
            return object()

        async def disconnect(self) -> None:
            self.disconnect_calls += 1

    async def fake_create_serial(cls, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        mc = _TrackedMC()
        created.append(mc)
        if call_count == 1:
            await asyncio.sleep(0.6)  # simulates a slow serial handshake
        return mc

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null",
        backoff_base=0.01,
        backoff_max=0.02,
        loop_start_timeout=5.0,
        # Deliberately much shorter than the 0.6s the first create_serial
        # blocks for, so stop()'s join times out while that thread is
        # still inside it -- exactly the reviewer's reproduction.
        stop_join_timeout=0.05,
    )
    connected_events = []
    client.subscribe(mcc.EventType.CONNECTED, connected_events.append)

    client.connect()  # starts run A; its create_serial() begins the 0.6s sleep
    time.sleep(0.05)  # give run A's thread a moment to actually enter create_serial
    client.stop()  # times out joining run A's loop thread

    client.connect()  # starts run B immediately afterward
    try:
        assert client.wait_ready(5.0), "run B never became ready"
        run_b = client._run
        assert run_b is not None

        # Give run A's stranded thread time to finish its blocked
        # create_serial() call and notice its OWN (never-cleared) stop_event.
        deadline = time.monotonic() + 3.0
        while (len(created) < 1 or created[0].disconnect_calls < 1) and time.monotonic() < deadline:
            time.sleep(0.02)

        assert len(created) >= 2, "the second connect() never even attempted create_serial"
        run_a_mc, run_b_mc = created[0], created[1]

        assert run_a_mc.disconnect_calls >= 1, "the stranded run never disconnected its own mc"
        assert run_a_mc.subscribe_calls == 0, (
            "the stranded run wired subscriptions after being told to stop "
            "-- a live duplicate source feeding the same queue"
        )
        assert run_b_mc.subscribe_calls == 10  # 9 handlers + the DISCONNECTED watcher

        time.sleep(0.1)  # let any (wrongly) duplicated CONNECTED dispatch land
        assert len(connected_events) == 1, "duplicate CONNECTED dispatch from two live sources"
        assert client.connected is True  # run B's status, not run A's belated cleanup
        assert client._run is run_b
    finally:
        client.stop()


# --- fix round 4: state derives from the current run; sessions own their
# forwarders; queues belong to runs ------------------------------------------


class _GateMC:
    """A `MeshCore` stand-in whose `disconnect()` can be made to block until
    the test releases it (or to raise), and whose `fire()` deliberately
    does NOT model the real library's "no dispatch after disconnect"
    guarantee -- so a test can check that THIS module drops a dead
    session's events by itself, rather than relying on the library to stop
    sending them."""

    def __init__(self, *, disconnect_blocks_until=None, disconnect_raises=False) -> None:
        self.self_info = {"public_key": "aa" * 32}
        self._subs: dict = {}
        self.disconnect_calls = 0
        self._release = disconnect_blocks_until
        self._raises = disconnect_raises
        self.commands = self._Commands()

    class _Commands:
        def __init__(self):
            self._mesh_request_lock = asyncio.Lock()

        async def send_msg(self, dst, msg, timestamp=None):
            import meshcore

            return _event(meshcore.EventType.MSG_SENT, {"expected_ack": b"\x09\x09\x09\x09"})

    def subscribe(self, event_type, callback, attribute_filters=None):
        self._subs.setdefault(event_type, []).append(callback)
        return object()

    def fire(self, event_type, payload) -> None:
        for cb in list(self._subs.get(event_type, [])):
            cb(_event(event_type, payload))

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self._release is not None:
            while not self._release.is_set():
                await asyncio.sleep(0.01)
        if self._raises:
            raise RuntimeError("boom: simulated serial close failure")


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def test_stop_racing_a_new_connect_never_clobbers_the_new_runs_state(monkeypatch):
    """Fix round 4, the OPEN finding: `stop()` used to end by
    unconditionally resetting `connected`/`self_info`/`_mc`/`_loop`. If a
    `connect()` on another thread started run B while `stop()` sat in its
    joins on run A, that tail clobbered B's live state: B's loop thread
    alive and current, yet `connected` False, `send_msg` raising "not
    connected" forever. Reproduced here deterministically: run A's
    `disconnect()` blocks until the test releases it, so `stop()` (on its
    own thread, generous join timeout) is provably still inside its join
    when run B comes up ready on the main thread; only then is A released
    and `stop()` allowed to finish. Everything B exposes must survive."""
    meshcore = pytest.importorskip("meshcore")
    release_a = threading.Event()
    created: list[_GateMC] = []

    async def fake_create_serial(cls, *args, **kwargs):
        mc = _GateMC(disconnect_blocks_until=release_a if not created else None)
        created.append(mc)
        return mc

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null",
        backoff_base=0.01,
        backoff_max=0.02,
        loop_start_timeout=5.0,
        stop_join_timeout=10.0,  # long: stop() must genuinely WAIT on run A
    )
    client.connect()
    stopper = None
    try:
        assert client.wait_ready(5.0)
        run_a = client._run

        stopper = threading.Thread(target=client.stop, name="test-stopper")
        stopper.start()
        # stop() detaches run A under the lock, sets its stop_event, then
        # blocks in loop_thread.join() because A's disconnect() is held.
        assert _wait_until(lambda: client._run is None), "stop() never detached run A"
        assert _wait_until(lambda: created[0].disconnect_calls == 1), (
            "run A never reached its blocked disconnect()"
        )
        assert stopper.is_alive(), "stop() returned before run A's thread finished"

        client.connect()  # run B, started while stop() is still joining A
        assert client.wait_ready(5.0), "run B never became ready"
        run_b = client._run
        assert run_b is not None and run_b is not run_a
        assert client.connected is True

        release_a.set()  # let run A's thread finish so stop() can return
        stopper.join(timeout=5.0)
        assert not stopper.is_alive(), "stop() did not return after run A was released"

        # The whole point: stop()'s tail must not have touched run B.
        assert client._run is run_b
        assert client.connected is True, "stop()'s tail clobbered run B's connected state"
        assert client.self_info == {"public_key": "aa" * 32}
        assert client._mc is created[1]
        assert client._loop is run_b.loop
        assert run_b.loop_thread.is_alive()
        sent = client.commands.send_msg("aabbcc", "hi")
        assert sent.type is mcc.EventType.MSG_SENT
    finally:
        release_a.set()
        if stopper is not None:
            stopper.join(timeout=5.0)
        client.stop()


def test_protocol_facing_state_is_derived_not_stored(monkeypatch):
    """Structural pin for fix round 4 -- NOT a race test. The check-then-act
    window between `_is_current(run)` and the write it guarded cannot be
    driven deterministically from a test (it is a few bytecodes wide), so
    rather than a test that passes either way this pins the construction
    that removes the window: `connected`/`self_info`/`_mc`/`_loop` are
    read-only properties derived from the current run, so there is no
    client-level field for ANY thread -- a superseded run's, or `stop()`'s
    tail -- to write, gated or otherwise. A future edit that reintroduces
    a writable copy fails here before it can forget a gate anywhere."""
    client = mcc.RealMeshCoreClient(device="/dev/null")
    for name, value in (("connected", True), ("self_info", {}), ("_mc", object()), ("_loop", 1)):
        with pytest.raises(AttributeError):
            setattr(client, name, value)
    assert client.connected is False
    assert client.self_info is None
    assert client._mc is None
    assert client._loop is None


def test_orphaned_client_within_one_run_delivers_nothing_after_its_session_closes(monkeypatch):
    """Fix round 4, the within-one-run gap no per-run token can close: if
    the library's `disconnect()` RAISES inside the session's `finally`, the
    (suppressed) failure leaves an orphaned, still-wired client. Round 3
    relied on the library having stopped its dispatcher before the raise;
    `_GateMC` deliberately keeps dispatching after a failed disconnect to
    prove THIS module drops the dead session's events by itself. Session 1
    ends at the `self_info` read (after wiring, so the orphan is real) and
    its disconnect raises; session 2 wires a fresh client in the SAME run.
    Firing the same advertisement through both must deliver exactly once,
    from session 2."""
    meshcore = pytest.importorskip("meshcore")
    created: list[_GateMC] = []

    class _FirstMC(_GateMC):
        @property
        def self_info(self):
            raise RuntimeError("boom: end session 1 after wiring")

        @self_info.setter
        def self_info(self, value):
            pass

    async def fake_create_serial(cls, *args, **kwargs):
        mc = _FirstMC(disconnect_raises=True) if not created else _GateMC()
        created.append(mc)
        return mc

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null", backoff_base=0.01, backoff_max=0.02, loop_start_timeout=5.0
    )
    seen = []
    client.subscribe(mcc.EventType.ADVERTISEMENT, seen.append)
    client.connect()
    try:
        assert client.wait_ready(5.0), "session 2 never became ready"
        assert len(created) >= 2
        orphan, live = created[0], created[1]
        assert orphan.disconnect_calls == 1
        assert len(orphan._subs) == 9, "session 1 must have been wired before it failed"
        assert client._mc is live

        orphan.fire(meshcore.EventType.ADVERTISEMENT, {"public_key": "bb" * 32})
        live.fire(meshcore.EventType.ADVERTISEMENT, {"public_key": "bb" * 32})
        assert _wait_until(lambda: len(seen) >= 1)
        time.sleep(0.1)  # let any (wrongly) forwarded orphan event land
        assert len(seen) == 1, "the orphaned session's client still reached subscribers"
    finally:
        client.stop()


def test_stranded_runs_belated_events_never_reach_a_later_run(monkeypatch):
    """Fix round 4, the cross-run half of the same gap. Run A is connected;
    `stop()` is called while A's `disconnect()` hangs (the library's own
    `dispatcher.stop()` awaits `queue.join()`, which can genuinely hang),
    so `stop()`'s short join times out and A is stranded mid-disconnect
    with its client still wired. Run B then starts. Round 3 gave every run
    the ONE client-wide queue, so anything A's client still pushed was
    drained by B's pump and delivered as if B had received it. Now the
    queue belongs to the run and the session's forwarder is closed before
    disconnect is attempted, so A's belated events go nowhere; B's arrive
    exactly once."""
    meshcore = pytest.importorskip("meshcore")
    release_a = threading.Event()
    created: list[_GateMC] = []

    async def fake_create_serial(cls, *args, **kwargs):
        mc = _GateMC(disconnect_blocks_until=release_a if not created else None)
        created.append(mc)
        return mc

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))

    client = mcc.RealMeshCoreClient(
        device="/dev/null",
        backoff_base=0.01,
        backoff_max=0.02,
        loop_start_timeout=5.0,
        stop_join_timeout=0.05,  # short: stop() gives up on the hung disconnect
    )
    seen = []
    client.subscribe(mcc.EventType.ADVERTISEMENT, seen.append)
    client.connect()
    try:
        assert client.wait_ready(5.0)
        client.stop()  # times out; run A is stranded inside disconnect()
        assert _wait_until(lambda: created[0].disconnect_calls == 1)

        client.connect()
        assert client.wait_ready(5.0), "run B never became ready"
        assert len(created) == 2
        stranded, live = created

        stranded.fire(meshcore.EventType.ADVERTISEMENT, {"public_key": "bb" * 32})
        live.fire(meshcore.EventType.ADVERTISEMENT, {"public_key": "bb" * 32})
        assert _wait_until(lambda: len(seen) >= 1)
        time.sleep(0.1)
        assert len(seen) == 1, "a stranded run's client reached subscribers through a later run"
        assert client.connected is True
    finally:
        release_a.set()
        client.stop()


@pytest.mark.parametrize("event_name", ["PATH_UPDATE", "ADVERTISEMENT"])
def test_path_change_refreshes_saved_route_without_fake_reception(monkeypatch, event_name):
    meshcore = pytest.importorskip("meshcore")
    key = "aa" * 32
    original = {"public_key": key, "adv_name": "alice", "out_path_len": 0, "out_path": ""}

    class UpdatingRadio(_StubMC):
        async def ensure_contacts(self, follow=False):
            if follow:
                self.contacts[key] = {**original, "out_path_len": 3, "out_path": "aa00bc"}
            return True

    stub = UpdatingRadio({"public_key": "cc" * 32}, contacts={key: original})

    async def create_serial(cls, *args, **kwargs):
        return stub

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(create_serial))
    routes, contacts, observations = [], [], []
    seeded, updated = threading.Event(), threading.Event()

    def on_route(route):
        routes.append(route)
        (seeded if route.hops == 0 else updated).set()

    transport = mcc.build_meshcore_transport(
        device="/dev/null", expected_node_id="!cccccccc", connect_timeout=5,
        on_route=on_route, on_contact=lambda *args: contacts.append(args),
        on_observation=observations.append,
    )
    try:
        assert seeded.wait(5)
        transport.client._run.loop.call_soon_threadsafe(
            stub.fire, getattr(meshcore.EventType, event_name), {"public_key": key}
        )
        assert updated.wait(5)
        assert [r.hops for r in routes] == [0, 3]
        assert routes[-1].hashes == ("aa", "00", "bc")
        assert len(contacts) == 1  # A path change is not a new position fix.
        assert len(observations) == (1 if event_name == "ADVERTISEMENT" else 0)
    finally:
        transport.client.stop()


@pytest.mark.parametrize("fail_login", [False, True])
def test_repeater_debug_trace_calls_results_and_redacts_secrets(monkeypatch, fail_login):
    meshcore = pytest.importorskip("meshcore")
    key = "ac" * 32
    secret = "never-log-this-password"
    lines = []
    monkeypatch.setattr(mcc.exchange_log, "enabled", lambda: True)
    monkeypatch.setattr(mcc.exchange_log, "log_line", lines.append)
    monkeypatch.setattr(mcc.exchange_log, "node_label", lambda node: f"Roof ({node})")

    class Commands:
        async def send_login_sync(self, destination, password, **kwargs):
            assert destination == key and password == secret
            if fail_login:
                raise TimeoutError(secret)
            return _event(meshcore.EventType.LOGIN_SUCCESS, {"pubkey_prefix": key[:12]})

        async def req_status_sync(self, destination, **kwargs):
            return {"pubkey_pre": key[:12], "bat": 4100, "nb_recv": 7, "secret": secret}

    client = mcc.RealMeshCoreClient(
        device="/dev/null", repeater_public_key=key, repeater_guest_password=secret
    )
    session = mcc._Session(None, type("MC", (), {"commands": Commands()})())
    if fail_login:
        with pytest.raises(TimeoutError):
            asyncio.run(client._query_repeater_status(session))
    else:
        asyncio.run(client._query_repeater_status(session))
    rendered = "\n".join(lines)
    assert "Roof (!acacacac)" in rendered
    assert "method=send_login_sync" in rendered
    assert "password=<redacted>" in rendered
    assert secret not in rendered and secret.encode().hex() not in rendered
    if fail_login:
        assert "during=login exception=TimeoutError" in rendered
        assert "method=req_status_sync" not in rendered
    else:
        assert "method=req_status_sync" in rendered
        assert "stage=status_ok" in rendered and "bat=4100" in rendered
        assert "elapsed_s=" in rendered


def test_repeater_trace_is_silent_outside_debug(monkeypatch):
    lines = []
    monkeypatch.setattr(mcc.exchange_log, "enabled", lambda: False)
    monkeypatch.setattr(mcc.exchange_log, "log_line", lines.append)
    client = mcc.RealMeshCoreClient(device="/dev/null")
    client._trace_repeater("cache_hit", age_s=1)
    assert lines == []


def test_list_and_remove_contact_through_a_connected_session(monkeypatch):
    meshcore = pytest.importorskip("meshcore")
    key, odd = "ab" * 32, "cd" * 32
    now = int(time.time())
    stub = _StubMC({"public_key": "aa" * 32}, contacts={
        key: {"public_key": key, "adv_name": "n", "type": 2, "last_advert": now - 60},
        odd: {"public_key": odd, "type": 9, "last_advert": 5},  # unsane advert, unknown type
    })

    async def fake_create_serial(cls, *args, **kwargs):
        return stub

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))
    client = mcc.RealMeshCoreClient(
        device="/dev/null", backoff_base=0.01, backoff_max=0.02, loop_start_timeout=5.0
    )
    assert client.list_contacts() is None and client.remove_contact(key) is False
    client.connect()
    try:
        assert client.wait_setup(5.0)
        assert client.max_contacts == 350
        by_id = {c.node_id: c for c in client.list_contacts()}
        assert by_id["!abababab"].adv_type == 2 and by_id["!abababab"].last_advert == now - 60
        assert by_id["!cdcdcdcd"].adv_type == 1 and by_id["!cdcdcdcd"].last_advert is None
        assert client.remove_contact(key) is True
        assert stub.commands.removed == [key]
        assert [c.node_id for c in client.list_contacts()] == ["!cdcdcdcd"]
    finally:
        client.stop()
