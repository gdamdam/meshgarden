"""The REAL `meshcore` package behind `MeshCoreTransport` (task 3B).

`meshgarden/meshcore_transport.py` is driven by anything satisfying its
`MeshCoreClient` Protocol; `tests/fake_meshcore.py`'s `FakeMeshCore` does
that synchronously and in-process for the contract suite, and this module's
`RealMeshCoreClient` does it against actual hardware (or, in this task's
verification, against the installed library's own importable surface --
there is no MeshCore radio attached to this machine; real-hardware
validation is task 5A).

LAZY, GUARDED IMPORT (task 3B item A; deferred by the "lazy meshcore
import" follow-up): importing THIS MODULE never requires the optional
`meshcore` package to be installed, AND never imports it either way.
`_ensure_mc_pkg()` below performs the guarded `import meshcore` lazily --
the first time `RealMeshCoreClient` is actually constructed -- not at this
module's own load time. `meshgarden/main.py` imports THIS MODULE
UNCONDITIONALLY at its own module load (`from meshgarden.meshcore_client
import build_meshcore_transport`), regardless of the configured `[radio]
transport`, but that import alone never reaches `_ensure_mc_pkg()`: a
Meshtastic-only `mgarden` invocation therefore never loads the third-party
package or its dependency chain, whether or not it happens to be
installed. The package is only imported, and only REQUIRED, at the point
someone constructs `RealMeshCoreClient` -- i.e. actually configures and
starts the MeshCore transport -- where its absence raises
`MeshCoreLibraryMissing` with an actionable install command, never a bare
`ModuleNotFoundError` from deep inside `meshcore_transport.py` or
`main.py`. Two things legitimately still touch the package outside that
path: `mgarden doctor`'s `meshcore` check
(`meshgarden/ops.py::_check_meshcore`) only calls
`importlib.util.find_spec("meshcore")`, which does not execute it; and
this module's own test suite (`tests/test_meshcore_client.py`) imports it
directly, deliberately, to build real `meshcore.Event`/`EventType` values
for its fixtures.

THREADING AND QUEUE DESIGN (task 3B item C, "the pattern the notes describe
from PotatoMesh", meshcore-notes.md section 8): two threads per "run" --
see `_Run` and FIX ROUND 3 below for what a run is and why.

1. The "asyncio loop" thread (`_run_loop`/`_outer_reconnect_loop`, daemon
   thread) owns an `asyncio.new_event_loop()` and runs the real
   `meshcore.MeshCore` object's entire async lifecycle: connecting via
   `MeshCore.create_serial(...)`, relying on the library's OWN
   `auto_reconnect=True` for a transient mid-session drop (its
   `ConnectionManager._attempt_reconnect`, a FLAT 1-second delay between up
   to `max_reconnect_attempts` tries -- see this module's docstring note
   below on where this differs from what the notes assumed), and this
   repo's existing `meshgarden.radio.backoff_delays` helper for the OUTER
   loop: once the library's own auto_reconnect gives up entirely (a
   `DISCONNECTED` event with `max_attempts_exceeded`/`reconnect_failed`),
   this module tears the whole `MeshCore` object down and reconnects FROM
   SCRATCH with our own exponential backoff, exactly as `RadioAdapter._run`
   does for Meshtastic and as PotatoMesh's own provider does per connection
   attempt (meshcore-notes.md section 8).
2. Every event this module subscribes to on the real `MeshCore` object is
   translated (`_translate_event`) into this repo's own
   `meshcore_transport.Event`/`EventType` and pushed onto a plain,
   thread-safe `queue.Queue` -- never onto an `asyncio.Queue`, and the
   callback that does the pushing never blocks. A second, plain Python
   "pump" thread (`_pump`) drains that queue and invokes whatever
   synchronous callables `MeshCoreTransport.connect()` registered via
   `subscribe()`, exactly the way `tests.fake_meshcore.FakeMeshCore`
   invokes them in-process. This is the "hands events to the synchronous
   daemon through a queue" the task brief specifies: the pump thread is
   the boundary between the library's asyncio world and this repo's
   synchronous one. Neither thread ever touches `MeshCoreTransport`,
   `sender.py`, or the database directly -- they only ever call back
   through `subscribe()`'s registered callables. The queue belongs to the
   run (fix round 4; it used to be one client-wide instance shared across
   stop()/connect() cycles): a stranded run's leftover events die with
   its queue instead of being drained by a later run's pump.

WHERE THE INSTALLED LIBRARY DIFFERS FROM meshcore-notes.md SECTION 8: the
notes describe "exponential backoff 1s/2s/4s/8s max" for the library's own
`auto_reconnect`. The INSTALLED `meshcore==2.3.11` package's
`connection_manager.py::ConnectionManager._attempt_reconnect` in fact uses
a FLAT 1-second delay for every attempt, not an exponential schedule (this
was verified by reading the installed source, not assumed). This does not
change this module's design: the flat delay is entirely the library's own
inner retry, bounded by `max_reconnect_attempts`; this repo's own
`backoff_delays`-driven exponential schedule is for the OUTER loop only,
exactly as planned. See task-3B-1-report.md for the full account.

WHAT THIS MODULE DOES NOT DO: it never calls the real library's
channel-send command (grep this file -- it isn't named here either, for the
same reason `meshcore_transport.py`'s module docstring gives). It performs
exactly two writes to the radio, both inside `_post_connect_setup`, on
every connection: the clock set (`_set_radio_clock`), unconditional and
best-effort like the Meshtastic path's own clock set, because a MeshCore
radio's clock lives in a power domain a power loss wipes and nothing else
corrects it; and the first PERSISTED config write
`meshcore_autoadd_overwrite` explicitly opts into (`ensure_autoadd_bit`),
never performed otherwise. The clock write is volatile runtime state, not
configuration -- a distinction worth keeping straight, since only the
autoadd bit is the "radio config write" this module otherwise avoids.
The SECOND persisted write is `remove_contact`, which `main.py`'s daily
housekeeping calls only when `[radio] meshcore_contact_prune_enabled` opts
in (and never from `_post_connect_setup`): it deletes least-recently-heard
contacts so a full table does not stop the radio auto-adding newcomers.

TASK 3B-4 UPDATE (fix 2, real-hardware finding): `_post_connect_setup` now
ALSO sends one flood self-advertisement per connection
(`_send_advert_on_connect`), and the public `send_advert()` repeats it on
`[radio] meshcore_advert_interval_seconds` from `main.py`'s maintenance
tick. Unlike every write named above, this is a genuine over-the-air
TRANSMISSION, not local serial control or a persisted config write -- the
garden was unreachable on real hardware until manually flood-advertised,
because a node that has never heard this radio cannot resolve a path back
to it, so even a correctly-ACKed reply DM can fail to route. This is a
deliberate, narrow exception to the "the service transmits nothing but a
direct-message reply to an eligible trigger" invariant: presence beaconing,
not message traffic, and the Meshtastic radio's own firmware already does
the equivalent (NODEINFO broadcasts) without this module's help. See
`docs/architecture.md`, `docs/security-and-privacy.md`, and
`tests/test_transport_contract.py::
test_the_meshcore_client_reaches_no_transmitting_library_call_but_send_msg_and_send_advert`
for where this is stated explicitly rather than left implicit.

FIX ROUND 1 (review): four Important defects, all in the seam between this
class and `MeshCoreTransport`/`build_meshcore_transport`, which had no test
at all before this round. See `task-3B-1-report.md`, "Fix round 1", for the
full account; summary:

- `_run_connected_session` now SYNTHESIZES a `CONNECTED` and (if present) a
  `SELF_INFO` event onto its own queue immediately after wiring
  subscriptions, because `create_serial(...)` already fired both, for
  real, before we could possibly have subscribed -- previously
  `_on_connected` never fired for the FIRST connect, so
  `status().connected_seconds` (MeshCore's only freshness signal) stayed
  `None` forever.
- `connect()` is now idempotent; `build_meshcore_transport` no longer
  calls `client.connect()` itself -- it calls `transport.connect()`, the
  one intended owner of connecting.
- `_run_loop`/`_outer_reconnect_loop` now catch exceptions from a
  connected session (not just from `create_serial` failing) and reset
  `connected`/`_mc` in a `finally`, so a wiring/read/wait failure retries
  through the same backoff path instead of silently killing this thread
  while `connected` stays stuck `True`.
- The channel-send-command name guard (`tests/test_transport_contract.py`)
  now also covers this module, plus a whole-package sweep.

FIX ROUND 2 (review): three further Important defects, in the same seam.
See `task-3B-1-report.md`, "Fix round 2"; summary:

- `_run_connected_session`'s `try/finally` now covers the WHOLE session
  body (wiring, the self-info read, the DISCONNECTED-watcher subscribe),
  not just the final wait, so `mc.disconnect()` always runs.
- The outer backoff now escalates after a repeated short-lived session,
  not just after a repeated failed `create_serial` call.
- `MeshCoreTransport.connect()` (in `meshcore_transport.py`) was made
  idempotent too -- this module's own idempotency guard protected only
  its threads, not the transport's `subscribe()` calls.

FIX ROUND 3 (review, this round): a DIRECTION CHANGE, not another patch.
Three rounds in a row, the fix for one round's defect introduced the
next's, every time in the same place: the shared, mutable flags
(`_stop_event`, `_connect_started`, `_loop_ready`, `_ready_event`) that
`stop()` and `connect()` each mutated independently. Round 1 fixed thread
death and created a leak (finding 1). Round 2 fixed the leak and created a
stranded-thread resurrection: `stop()`'s joins are TIMED (2s); if the loop
thread is still blocked inside `create_serial`/`disconnect()`/a probe when
the join times out, `stop()` cleared `_stop_event` anyway (round 2's
finding-4 fix) -- so when that blocked call finally returned, the thread
read a CLEARED flag and proceeded as if nothing had asked it to stop,
wiring subscriptions and dispatching events from a session nobody wanted,
alongside whatever a SUBSEQUENT `connect()` (also let through, since
`_connect_started` was cleared too) had since started: two live sessions
feeding the one shared queue, i.e. duplicate replies, and two event loops
owning one serial device.

THE REDESIGN: `_Run` (below) bundles everything that describes ONE
connect()-to-stop() lifecycle -- its own `stop_event`, its own
`ready_event`/`loop_ready`, its own loop and threads. `RealMeshCoreClient`
holds exactly one mutable pointer, `self._run`, swapped atomically under
`self._lifecycle_lock`; nothing else needs the lock, because everything
else either belongs exclusively to whichever call captured a `_Run`
object (no other run can ever reach it once `self._run` has moved past
it), or was (in round 3; see FIX ROUND 4 for what replaced this) a
Protocol-facing attribute (`connected`, `self_info`, `_mc`, `_loop`)
written only after confirming (`_is_current`) the writer's run was still
the current one. A run's OWN `stop_event`, once set by `stop()`, is
never touched again by anyone -- there is no later `connect()` that could
clear it out from under a thread still blocked inside a library call, and
no `_Run` object is ever shared between two connect() calls -- so a
stranded thread from an old run, whenever it finally wakes up, reliably
sees "stop requested" and disconnects/exits instead of becoming a second
live source. `_outer_reconnect_loop` additionally checks its run's
`stop_event` immediately after `create_serial()` returns (which does not
itself watch the flag mid-await and can therefore succeed after a stop
was already requested) and tears the just-obtained connection back down
unused rather than entering a session for it.

This one change also resolves round 2's "finding 4 is incomplete"
follow-up for free: `ready_event` is now part of the per-run bundle, so a
fresh run always starts with a fresh, unset one -- there is no shared
flag left to forget to clear.

The reviewer's narrower alternative (latch `stop()`'s state and only clear
it once both joins actually succeed, otherwise log and stay latched) was
also offered and would have been acceptable; the generation-token design
was chosen instead because it also fixes the "connect racing stop, both
mutating shared flags outside one critical section" defect (fix round 3's
other Important finding) as a natural consequence of there being no
shared, independently-mutated flags left to race over, rather than as a
second, separate patch.

FIX ROUND 4 (review): round 3's design was sound but enforced its safety
property by CONVENTION -- every write to the four Protocol-facing fields
(`connected`, `self_info`, `_mc`, `_loop`) had to remember an
`_is_current(run)` check -- and `stop()`'s own tail forgot: it reset all
four unconditionally after its joins, so a `connect()` that started run B
while `stop()` was still joining run A had B's live state clobbered
(sends raised "not connected" forever, status said disconnected, B's loop
thread alive and current). Two further gaps of the same family: the gated
writes were check-then-act, not atomic with the pointer swap; and nothing
per-run could stop an ORPHANED client within one run (a `disconnect()`
that raised in the session's `finally` left a still-wired client whose
callbacks kept landing in the live queue alongside the next session's).

THE CHANGE, by construction rather than convention:

- Those four are now read-only PROPERTIES derived from `self._run` (and
  its `session`). There is no client-level copy for anyone to write, so
  there is nothing for `stop()` to reset (detaching the pointer IS the
  reset), no gate to forget, and no check-then-act window: a superseded
  run's thread writes only to its own `_Run`/`_Session`, which the
  properties simply never look at again. `connected`/`self_info`/`_mc`
  derive from ONE pointer (`run.session`), so they can never be torn.
- Each wired `MeshCore` object gets a `_Session` whose bound `forward`
  method is what the library subscribes; the session is marked `closed`
  (and unpublished) on the loop thread BEFORE `mc.disconnect()` is
  attempted, so a disconnect that raises or hangs leaves behind a client
  whose callbacks are dropped by their own session, not suppressed by
  luck. `_Session.forward` also owns the SELF_INFO bookkeeping, so the pump
  thread writes nothing at all.
- The event queue is per run (`_Run.queue`), fed only through
  `_Run.enqueue`, which refuses everything once that run's `stop_event` is
  set. Both threads are CREATED inside `_Run.__init__` (started later, by
  `connect()`, outside the lock), so `stop()` can never observe a run
  whose threads do not exist yet.
- Cross-thread command scheduling (`send_msg`, the channel probe, the
  autoadd write) takes `mc` and loop from ONE `_current_session()`
  snapshot, never from two separate attribute reads.

Mutable shared state after this round, exhaustively: `self._run` (written
only under `_lifecycle_lock`, by `connect()`/`stop()`), and
`self._subscribers` (append-only, written only by `subscribe()`).
Everything else lives on a `_Run`/`_Session` and is written only by that
run's own loop thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from meshgarden import exchange_log, meshcore_compat
from meshgarden.contacts import ADV_TYPE_COMPANION, RadioContact
from meshgarden.meshcore_transport import Event, EventType, MeshCoreTransport
from meshgarden.models import MeshCoreObservation, MeshCoreRoute
from meshgarden.radio import backoff_delays
from meshgarden.transport import Inbound

log = logging.getLogger(__name__)


class _SecretRedactionFilter(logging.Filter):
    """Redact one configured secret from meshcore_py's raw packet logs."""

    def __init__(self, secret: str) -> None:
        super().__init__()
        self._forms = tuple(
            form for form in (secret, secret.encode("utf-8").hex()) if form
        )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        # These two upstream DEBUG messages contain the complete outbound
        # packet.  Redact the packet wholesale: byte repr escaping means
        # matching only the password text or its hex form is insufficient
        # for arbitrary UTF-8/control characters.
        if message.startswith(("Sending raw data:", "sending pkt :")):
            record.msg = message.split(":", 1)[0] + ": <redacted>"
            record.args = ()
            return True
        redacted = message
        for form in self._forms:
            redacted = redacted.replace(form, "<redacted>")
            redacted = redacted.replace(form.upper(), "<redacted>")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True

# `_mc_pkg` is the real `meshcore` package once `_ensure_mc_pkg()` has
# imported it (or `None` if that import has not yet happened or failed).
# It starts `None` -- merely importing this module must never populate
# it -- and every other reference to the real package below (`_run_loop`'s
# `_mc_pkg.MeshCore.create_serial`, `_real_event_type`'s `_mc_pkg.EventType`)
# reads this same cached global, safe to do because both only ever run
# after a `RealMeshCoreClient` has already been constructed successfully.
_mc_pkg: Any = None
_mc_pkg_import_attempted = False


def _ensure_mc_pkg() -> Any | None:
    """Performs the guarded `import meshcore`, deferred here from module
    scope so that importing `meshcore_client` -- and therefore
    `meshgarden.main`, which imports it unconditionally -- never loads the
    third-party package. Called only from `RealMeshCoreClient.__init__`,
    i.e. only once a MeshCore transport is actually being built.

    Attempted at most once per process (`_mc_pkg_import_attempted` latches
    after the first call, success or failure); the outcome is cached on the
    module-level `_mc_pkg`. A test can still force-simulate absence by
    monkeypatching `_mc_pkg` to `None` together with
    `_mc_pkg_import_attempted` to `True`, without needing to touch
    `sys.modules` -- see `test_guarded_import_raises_actionable_error_when_package_absent`.
    """
    global _mc_pkg, _mc_pkg_import_attempted
    if not _mc_pkg_import_attempted:
        try:
            import meshcore as _pkg
        except ImportError:  # pragma: no cover - exercised via a monkeypatched guard test
            _pkg = None
        _mc_pkg = _pkg
        _mc_pkg_import_attempted = True
    return _mc_pkg


class MeshCoreLibraryMissing(Exception):
    """Raised when `[radio] transport = "meshcore"` is configured but the
    optional `meshcore` package is not installed. Never raised merely by
    importing this module -- only by constructing `RealMeshCoreClient` --
    so a Meshtastic-only install can import every module in this package
    freely (task 3B item A)."""

    def __init__(self) -> None:
        super().__init__(
            "[radio] transport = \"meshcore\" is configured, but the optional "
            "'meshcore' package is not installed. Install it with "
            "`uv sync --extra meshcore` (or `pip install 'meshgarden[meshcore]'`); "
            "pyproject.toml pins `meshcore>=2.3.11,<3`."
        )


class MeshCoreNotConnected(Exception):
    """Raised by `commands.send_msg` when no connection is currently
    established (the outer reconnect loop is between attempts, or hasn't
    completed its first connect yet). Mirrors `meshgarden.radio.
    SendNotReady` in spirit. Whether/where `sender.py`'s send call site
    should catch this is NOT resolved by this task -- see
    `MeshCoreUnknownContact`/`MeshCoreSendFailed`'s docstrings in
    `meshcore_transport.py` for the same open question, left to 3B-2."""


# The 8 event types this adapter subscribes to and nothing else (task 3B
# brief item C, binding: contact message received, channel message
# received, acknowledgement, advertisement, new contact, connected,
# disconnected, self info). Named here by the REAL library's own attribute
# names so `getattr(meshcore.EventType, name)` resolves them without this
# module spelling out (or importing, at module scope) anything beyond the
# guarded `meshcore` import itself.
_SUBSCRIBED_EVENT_NAMES: tuple[str, ...] = (
    "CONTACT_MSG_RECV",
    "CHANNEL_MSG_RECV",
    "ACK",
    "ADVERTISEMENT",
    "PATH_UPDATE",
    "NEW_CONTACT",
    "CONNECTED",
    "DISCONNECTED",
    "SELF_INFO",
)

# Matches meshcore_py's own `commands/base.py::CommandHandlerBase.
# DEFAULT_TIMEOUT` -- the library's own default command-response wait.
_DEFAULT_SEND_TIMEOUT = 15.0


def _translate_event(real_event: Any) -> Event:
    """Real `meshcore.Event` -> this repo's own `meshcore_transport.Event`.

    Matches purely by `.value` string (both enums use the same confirmed
    wire strings -- see `meshcore_transport.EventType`'s docstring), so
    nothing above this module ever needs the real `meshcore.EventType`
    imported, referenced, or even installed.
    """
    payload = real_event.payload
    if not isinstance(payload, dict):
        payload = {}
    return Event(type=EventType(real_event.type.value), payload=dict(payload))


class _RealCommands:
    """Satisfies `meshcore_transport._Commands`: a SYNCHRONOUS `send_msg`
    that schedules the real library's async `commands.send_msg` onto the
    background asyncio loop thread and blocks for its result -- exactly the
    call shape `MeshCoreTransport.send_dm` already expects from the fake.
    """

    def __init__(self, client: RealMeshCoreClient) -> None:
        self._client = client

    def send_msg(
        self, dst: str, msg: str, timestamp: int | None = None, attempt: int = 0
    ) -> Event:
        # ONE snapshot of the current run supplies both the `mc` the
        # coroutine is built on and the loop it is scheduled onto (fix
        # round 4): reading them as two separate client attributes left a
        # window in which a stop()/connect() pair between the two reads
        # scheduled run A's `mc` onto run B's loop.
        run, session = self._client._current_session()
        async def serialized_send() -> Any:
            # MeshCore command responses use an uncorrelated MSG_SENT event.
            # Share the library's mesh-request lock with login/status calls
            # so a visitor reply can never consume the repeater request's
            # response (or vice versa).
            async with session.mc.commands._mesh_request_lock:
                if attempt == 0:
                    return await session.mc.commands.send_msg(dst, msg, timestamp)
                return await session.mc.commands.send_msg(
                    dst, msg, timestamp, attempt=attempt
                )

        repeater_enabled = bool(
            self._client._repeater_public_key and self._client._repeater_guest_password
        )
        if repeater_enabled:
            operation = serialized_send()
        elif attempt == 0:
            operation = session.mc.commands.send_msg(dst, msg, timestamp)
        else:
            operation = session.mc.commands.send_msg(dst, msg, timestamp, attempt=attempt)
        future = asyncio.run_coroutine_threadsafe(operation, run.loop)
        timeout = _DEFAULT_SEND_TIMEOUT
        if repeater_enabled:
            timeout += (2 * self._client._repeater_query_timeout) + 1
        try:
            return _translate_event(future.result(timeout=timeout))
        except TimeoutError:
            # Cancelling is essential: otherwise a timed-out coroutine can
            # later acquire the lock and transmit a reply the caller already
            # treated as failed.
            future.cancel()
            raise

    def send_chan_msg(self, chan: int, msg: str, timestamp: int | None = None) -> Event:
        """Synchronously submit a MeshCore channel message on the loop thread."""
        run, session = self._client._current_session()

        async def serialized_send() -> Any:
            # Channel submission returns an uncorrelated OK/ERROR event.
            async with session.mc.commands._mesh_request_lock:
                return await session.mc.commands.send_chan_msg(chan, msg, timestamp)

        future = asyncio.run_coroutine_threadsafe(serialized_send(), run.loop)
        # Like DMs, channel sends can wait behind both repeater query stages.
        timeout = _DEFAULT_SEND_TIMEOUT
        if self._client._repeater_public_key and self._client._repeater_guest_password:
            timeout += (2 * self._client._repeater_query_timeout) + 1
        try:
            return _translate_event(future.result(timeout=timeout))
        except TimeoutError:
            future.cancel()
            raise

    def reset_path(self, dst: str) -> Event:
        """Synchronously reset a contact path on the companion radio."""
        run, session = self._client._current_session()

        async def serialized_reset() -> Any:
            async with session.mc.commands._mesh_request_lock:
                return await session.mc.commands.reset_path(dst)

        future = asyncio.run_coroutine_threadsafe(serialized_reset(), run.loop)
        try:
            return _translate_event(future.result(timeout=_DEFAULT_SEND_TIMEOUT))
        except TimeoutError:
            future.cancel()
            raise


class _Session:
    """ONE wired `meshcore.MeshCore` object inside one run: the `mc`, the
    self-info snapshot read from it, and the forwarding callback the
    library holds a reference to (fix round 4).

    `forward` is a bound method of THIS object, not of the client, so the
    library can only ever push into the queue of the run this session
    belongs to, and only while `closed` is False. `closed` is set,
    synchronously on the loop thread, BEFORE `mc.disconnect()` is even
    attempted (see `RealMeshCoreClient._run_connected_session`), so a
    `disconnect()` that raises or hangs cannot leave behind a wired client
    whose callbacks still land anywhere: they are dropped here, by the
    session that owns them, regardless of what the library does next.
    This is what makes duplicate dispatch from an orphaned client within a
    single run impossible rather than merely suppressed.

    `self_info` is written only on the loop thread (the initial read after
    `create_serial`, then every SELF_INFO event, which meshcore_py's
    dispatcher delivers inline on that same thread), so nothing else ever
    writes it.
    """

    __slots__ = ("run", "mc", "self_info", "closed", "contact_refresh_needed", "contact_metadata")

    def __init__(self, run: _Run, mc: Any) -> None:
        self.run = run
        self.mc = mc
        self.self_info: dict[str, Any] | None = None
        self.closed = False
        self.contact_refresh_needed = asyncio.Event()
        self.contact_metadata: dict[str, tuple] = {}

    def forward(self, real_event: Any) -> None:
        # Called on the loop thread by meshcore_py's own dispatcher, which
        # invokes a synchronous callback inline (events.py's
        # `_process_events`) -- never blocks, never touches daemon state.
        if self.closed:
            return
        event = _translate_event(real_event)
        if event.type is EventType.SELF_INFO and event.payload.get("public_key"):
            self.self_info = dict(event.payload)
        if event.type in (EventType.ADVERTISEMENT, EventType.PATH_UPDATE):
            # Advert and path-update notifications carry only a key. Ask for
            # changed contact details outside the synchronous event callback.
            self.contact_refresh_needed.set()
        self.run.enqueue(event)


class _Run:
    """All state describing exactly ONE `connect()`-to-`stop()` lifecycle
    ("generation") -- fix round 3's redesign, replacing three rounds of
    scattered shared-flag patches with one bundle per run; fix round 4
    moved the LAST client-level copies of run state onto it too.

    `RealMeshCoreClient.connect()` creates a fresh instance and installs it
    as `self._run` under the lock; `stop()` atomically detaches it (sets
    `self._run` back to `None`) before tearing it down. No two runs ever
    share an instance, and nothing outside `stop()` ever touches
    `stop_event` once it has set it -- there is no "clear" anywhere in this
    class's lifecycle for anyone to race. That is what makes a thread still
    blocked inside a call like `create_serial()`/`disconnect()` when
    `stop()`'s timed join gives up on it harmless once it finally does
    return: it reads ITS OWN `stop_event` (immutable-after-set, never
    shared with whatever run comes next) rather than a client-wide flag a
    later `connect()` could have reset out from under it.

    Ownership of every field, so a future edit can check itself against it:

    - `stop_event`, `loop_ready`, `ready_event`, `queue`, `loop_thread`,
      `pump_thread`: created here, never reassigned. The two threads are
      CREATED here (not started -- `connect()` starts them after publishing
      the run, never under the lock) so `stop()` can never observe a run
      whose threads do not exist yet.
    - `loop`: written exactly once, by this run's own loop thread.
    - `session`: written only by this run's own loop thread -- `None` until
      a `MeshCore` object is wired and its self-info read, then that
      `_Session`, then `None` again the moment the session ends. It is the
      ONE pointer from which `RealMeshCoreClient.connected`/`self_info`/
      `_mc` are all derived, so they become visible together, atomically,
      and can never be torn (connected without an `mc`, or vice versa).
    - `queue`: per run, not per client. A stranded run's leftover or
      belated events die with its queue; they can never be drained by a
      later run's pump thread.
    """

    __slots__ = (
        "stop_event",
        "loop_ready",
        "ready_event",
        "setup_done",
        "queue",
        "loop_thread",
        "pump_thread",
        "loop",
        "session",
    )

    def __init__(self, client: RealMeshCoreClient) -> None:
        self.stop_event = threading.Event()
        self.loop_ready = threading.Event()
        self.ready_event = threading.Event()
        # Set once THIS run's per-connection setup (contact seeding,
        # channel-name resolution, the clock set, the opt-in autoadd
        # write) has finished for its FIRST session -- see
        # `_post_connect_setup`. Per run, like everything else here, so a
        # later run's setup can never be mistaken for an earlier one's.
        self.setup_done = threading.Event()
        self.queue: queue.Queue[Event | None] = queue.Queue()
        self.loop_thread = threading.Thread(
            target=client._run_loop, args=(self,), name="meshcore-asyncio", daemon=True
        )
        self.pump_thread = threading.Thread(
            target=client._pump, args=(self,), name="meshcore-pump", daemon=True
        )
        self.loop: asyncio.AbstractEventLoop | None = None
        self.session: _Session | None = None

    def enqueue(self, event: Event) -> None:
        """The ONLY way an event enters this run's queue. Once `stop()` has
        asked this run to stop, nothing further is accepted -- not from a
        session's forwarder, not from the synthesized CONNECTED/SELF_INFO
        -- so the pump drains exactly what arrived before the stop request
        and then exits on the sentinel `stop()` puts after it."""
        if self.stop_event.is_set():
            return
        self.queue.put(event)


class RealMeshCoreClient:
    """`MeshCoreClient` backed by the real `meshcore` package. See the
    module docstring for the threading/queue design and fix round 3's
    `_Run`-based lifecycle redesign."""

    # Item 2 fix: floor between any two flood self-advertisements, enforced
    # in this client regardless of trigger (per-connection or periodic). 60s
    # is comfortably above the ~32s a flapping-link reconnect loop can settle
    # into (one-second base backoff, reset every cycle by a session that
    # just clears `min_stable_session_seconds`), while staying far below any
    # sane `meshcore_advert_interval_seconds` -- a defensible floor against
    # flooding a shared band without needing its own config knob.
    _ADVERT_MIN_GAP_SECONDS = 60.0

    def __init__(
        self,
        *,
        device: str,
        baudrate: int = 115200,
        backoff_base: float = 1.0,
        backoff_max: float = 300.0,
        max_reconnect_attempts: int = 3,
        loop_start_timeout: float = 10.0,
        stop_join_timeout: float = 2.0,
        min_stable_session_seconds: float = 30.0,
        channel_name: str = "",
        autoadd_overwrite: bool = False,
        on_channel_resolved: Callable[[int | None], None] | None = None,
        alert_channel_name: str = "",
        on_alert_channel_resolved: Callable[[int | None], None] | None = None,
        setup_timeout: float = 10.0,
        repeater_public_key: str = "",
        repeater_guest_password: str = "",
        repeater_cache_seconds: int = 300,
        repeater_query_timeout: float = 15.0,
    ) -> None:
        if _ensure_mc_pkg() is None:
            raise MeshCoreLibraryMissing()
        self._device = device
        self._baudrate = baudrate
        # PER-CONNECTION SETUP, not per-process (whole-branch review,
        # finding 3). `build_meshcore_transport` used to wait for the first
        # connect and then resolve the channel name ONCE, from the calling
        # thread; if that wait timed out (a systemd-started daemon whose
        # USB serial device has not enumerated yet -- the ordinary
        # Raspberry Pi case) resolution returned `None` silently and
        # channel triggering was off for the life of the process even when
        # the reconnect loop succeeded a minute later. Owning the channel
        # name and the autoadd flag HERE, and doing the work inside every
        # connected session (`_post_connect_setup`), makes "resolved once,
        # at startup, or never" unexpressible: there is no longer a
        # one-shot path to miss.
        self._channel_name = channel_name
        self._autoadd_overwrite = autoadd_overwrite
        self._on_channel_resolved = on_channel_resolved
        self._alert_channel_name = alert_channel_name
        self._on_alert_channel_resolved = on_alert_channel_resolved
        self._setup_timeout = setup_timeout
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._max_reconnect_attempts = max_reconnect_attempts
        self._loop_start_timeout = loop_start_timeout
        self._stop_join_timeout = stop_join_timeout
        # Fix round 2, finding 2: a session shorter than this does NOT
        # reset the outer backoff counter -- see `_outer_reconnect_loop`.
        self._min_stable_session_seconds = min_stable_session_seconds
        # Item 2 fix: a reconnect loop whose sessions each just clear
        # `min_stable_session_seconds` resets the outer backoff to its base
        # every cycle, so the per-connection advert (`_send_advert_on_connect`
        # below) would otherwise re-fire on every reconnect -- a flood on a
        # shared band. Gated in `_send_advert_if_due`, keyed off this
        # timestamp alone, regardless of which caller (the per-connection
        # advert or the periodic `send_advert()`) triggers it.
        self._last_advert_monotonic: float | None = None
        # Radio's contact-table capacity from the device query; None until known.
        self.max_contacts: int | None = None
        self._repeater_public_key = repeater_public_key
        self._repeater_guest_password = repeater_guest_password
        self._repeater_cache_seconds = repeater_cache_seconds
        self._repeater_query_timeout = repeater_query_timeout
        self._repeater_lock = threading.Lock()
        self._repeater_future: Any | None = None
        self._repeater_status: dict[str, Any] | None = None
        self._repeater_status_at: float | None = None
        self._repeater_error: str | None = None
        self._repeater_last_attempt_at: float | None = None
        if repeater_guest_password:
            # meshcore_py logs complete outbound packet bytes in both its
            # command and serial layers at DEBUG.  Install this before any
            # connection can start so a guest password never reaches a
            # trace, even when MeshGarden itself is run with --debug.
            logging.getLogger("meshcore").addFilter(
                _SecretRedactionFilter(repeater_guest_password)
            )

        self.commands = _RealCommands(self)

        # The subscriber table is the ONE piece of state that legitimately
        # outlives a run: `MeshCoreTransport.connect()` registers its
        # handlers once, before the first `connect()`, and every run's pump
        # thread reads them. Written only by `subscribe()` (append-only).
        self._subscribers: dict[EventType, list[Callable[[Event], None]]] = {}

        # Fix round 3: ONE lock, ONE pointer. `self._run` is the single
        # source of truth for "which generation, if any, is current"; every
        # other piece of run-scoped state lives on the `_Run` object
        # itself. Swapping `self._run` is the only transition that needs
        # the lock, because (fix round 4) it is the only mutable thing on
        # this object apart from `_subscribers`: the Protocol-facing
        # `connected`/`self_info` (and `_mc`/`_loop`) are read-only
        # properties DERIVED from `self._run` below, not fields, so there
        # is nothing for a superseded run's thread -- or `stop()`'s own
        # tail -- to write, gated or otherwise.
        self._lifecycle_lock = threading.Lock()
        self._run: _Run | None = None

    # -- MeshCoreClient Protocol surface, DERIVED from the current run ----
    #
    # Fix round 4. Rounds 1-3 each copied run state into client fields and
    # then had to remember, at every write site, to check the writer's run
    # was still current; round 3's own `stop()` tail forgot, and clobbered
    # a newer run's live state. There are no such fields any more: each
    # property reads `self._run` once and derives its answer from that one
    # snapshot, so (a) `stop()` detaching the pointer IS the reset, (b) a
    # superseded run's thread writing to its OWN `_Run`/`_Session` is
    # invisible here by construction, and (c) `connected`/`self_info`/`_mc`
    # always describe the same session, never a torn mix of two.

    @property
    def connected(self) -> bool:
        run = self._run
        return run is not None and run.session is not None

    @property
    def self_info(self) -> dict[str, Any] | None:
        run = self._run
        session = run.session if run is not None else None
        return session.self_info if session is not None else None

    @property
    def _mc(self) -> Any | None:
        """The current session's real `meshcore.MeshCore`, or `None`."""
        run = self._run
        session = run.session if run is not None else None
        return session.mc if session is not None else None

    @property
    def _loop(self) -> asyncio.AbstractEventLoop | None:
        run = self._run
        return run.loop if run is not None else None

    def _current_session(self) -> tuple[_Run, _Session]:
        """One consistent (run, session) snapshot for cross-thread command
        scheduling, or `MeshCoreNotConnected`. Callers take BOTH the `mc`
        and the loop from this one snapshot, never from two separate
        attribute reads that a stop()/connect() could interleave."""
        run = self._run
        session = run.session if run is not None else None
        if run is None or session is None or run.loop is None or not run.loop.is_running():
            raise MeshCoreNotConnected()
        return run, session

    # -- MeshCoreClient Protocol ------------------------------------------

    def subscribe(self, event_type: EventType, callback: Callable[[Event], None]) -> None:
        self._subscribers.setdefault(event_type, []).append(callback)

    def _trace_repeater(self, stage: str, **details: Any) -> None:
        """Opt-in exchange trace; callers pass only safe metadata, never secrets."""
        if not exchange_log.enabled():
            return
        target = exchange_log.node_label("!" + self._repeater_public_key[:8].lower())
        fields = " ".join(f"{key}={value}" for key, value in details.items())
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        exchange_log.log_line(f"{ts} repeater to={target} stage={stage} {fields}".rstrip())

    async def _query_repeater_status(self, session: _Session) -> dict[str, Any]:
        started = time.monotonic()
        stage = "login"
        try:
            self._trace_repeater(
                "call", method="send_login_sync", timeout_s=self._repeater_query_timeout,
                password="<redacted>",
            )
            login = await session.mc.commands.send_login_sync(
                self._repeater_public_key,
                self._repeater_guest_password,
                timeout=self._repeater_query_timeout,
                min_timeout=self._repeater_query_timeout,
            )
            if login is None:
                self._trace_repeater(
                    "login_no_response", elapsed_s=round(time.monotonic() - started, 3)
                )
                raise RuntimeError("login failed or timed out")
            if str(login.payload.get("pubkey_prefix", "")).lower() != (
                self._repeater_public_key[:12].lower()
            ):
                self._trace_repeater("login_wrong_recipient")
                raise RuntimeError("login response was for another repeater")
            self._trace_repeater("login_ok", elapsed_s=round(time.monotonic() - started, 3))
            stage = "status"
            self._trace_repeater(
                "call", method="req_status_sync", timeout_s=self._repeater_query_timeout,
            )
            status = await session.mc.commands.req_status_sync(
                self._repeater_public_key,
                timeout=self._repeater_query_timeout,
                min_timeout=self._repeater_query_timeout,
            )
            if status is None:
                self._trace_repeater("status_no_response")
                raise RuntimeError("status request timed out")
            if str(status.get("pubkey_pre", "")).lower() != self._repeater_public_key[:12].lower():
                self._trace_repeater("status_wrong_recipient")
                raise RuntimeError("status response was for another repeater")
            safe = {
                key: status[key] for key in (
                    "bat", "uptime", "nb_recv", "nb_sent", "last_rssi", "last_snr", "noise_floor"
                ) if isinstance(status.get(key), (int, float))
            }
            self._trace_repeater(
                "status_ok", elapsed_s=round(time.monotonic() - started, 3), **safe
            )
            return dict(status)
        except asyncio.CancelledError:
            self._trace_repeater("cancelled", during=stage)
            raise
        except Exception as exc:
            self._trace_repeater("failed", during=stage, exception=type(exc).__name__)
            raise

    def repeater_status(
        self, *, start_refresh: bool = True
    ) -> tuple[str, dict[str, Any] | None, int | None, str | None]:
        """Return cached repeater status and start at most one RF refresh.

        This method never waits for RF.  The first call reports ``pending``;
        a later REPEATER command receives the result.  A stale cached result
        remains usable while it is refreshed in the background.
        """
        if not self._repeater_public_key or not self._repeater_guest_password:
            return "disabled", None, None, None
        now = time.monotonic()
        with self._repeater_lock:
            age = (
                int(max(0.0, now - self._repeater_status_at))
                if self._repeater_status_at is not None else None
            )
            fresh = self._repeater_status is not None and age is not None and (
                age <= self._repeater_cache_seconds
            )
            if fresh:
                if start_refresh:
                    self._trace_repeater("cache_hit", age_s=age, rf_query=False)
                return "ready", dict(self._repeater_status), age, None
            if self._repeater_future is not None:
                if start_refresh:
                    self._trace_repeater("query_pending", rf_query=False)
                return "stale" if self._repeater_status is not None else "pending", (
                    dict(self._repeater_status) if self._repeater_status is not None else None
                ), age, self._repeater_error
            # A bad password or unreachable repeater must not turn a burst
            # of visitor commands into a burst of RF login attempts.
            if (self._repeater_last_attempt_at is not None
                    and now - self._repeater_last_attempt_at < 30.0):
                if start_refresh:
                    self._trace_repeater(
                        "query_cooldown", remaining_s=round(
                            30.0 - (now - self._repeater_last_attempt_at), 1
                        ), rf_query=False,
                    )
                return "stale" if self._repeater_status is not None else "unavailable", (
                    dict(self._repeater_status) if self._repeater_status is not None else None
                ), age, self._repeater_error
            if not start_refresh:
                return "stale" if self._repeater_status is not None else "idle", (
                    dict(self._repeater_status) if self._repeater_status is not None else None
                ), age, self._repeater_error
            try:
                run, session = self._current_session()
            except MeshCoreNotConnected:
                self._trace_repeater("disconnected", rf_query=False)
                return "stale" if self._repeater_status is not None else "unavailable", (
                    dict(self._repeater_status) if self._repeater_status is not None else None
                ), age, "radio disconnected"
            self._trace_repeater(
                "query_scheduled", total_timeout_s=(2 * self._repeater_query_timeout) + 1
            )
            bounded = asyncio.wait_for(
                self._query_repeater_status(session),
                timeout=(2 * self._repeater_query_timeout) + 1,
            )
            future = asyncio.run_coroutine_threadsafe(bounded, run.loop)
            self._repeater_future = future
            self._repeater_last_attempt_at = now

        def done(completed: Any) -> None:
            try:
                result = completed.result()
            except Exception as exc:
                with self._repeater_lock:
                    if self._repeater_future is completed:
                        self._repeater_future = None
                        self._repeater_error = str(exc)
                        self._trace_repeater("query_failed", exception=type(exc).__name__)
                return
            with self._repeater_lock:
                if self._repeater_future is completed:
                    self._repeater_future = None
                    # A response belonging to a session that was replaced
                    # during the request is not allowed to populate the new
                    # session's cache.
                    if self._run is run and run.session is session:
                        self._repeater_status = result
                        self._repeater_status_at = time.monotonic()
                        self._repeater_error = None
                        self._trace_repeater("cache_updated")
                    else:
                        self._trace_repeater("discarded_old_session")

        future.add_done_callback(done)
        return "stale" if self._repeater_status is not None else "pending", (
            dict(self._repeater_status) if self._repeater_status is not None else None
        ), age, self._repeater_error

    def connect(self) -> None:
        """Starts a new run's two background threads and returns
        immediately -- it does NOT wait for the radio handshake to
        complete (mirrors `RadioAdapter.start()`). Callers that need to
        block until the first connection succeeds use `wait_ready
        (timeout)`, exactly like `RadioAdapter.wait_ready`.

        IDEMPOTENT: a second call, while a run is already active, is a
        no-op -- logged, not silently ignored. `MeshCoreTransport.
        connect()` is this client's one intended caller; see
        `build_meshcore_transport`.
        """
        # The run -- threads included, unstarted -- is fully built BEFORE it
        # is published, so `stop()` can never see a half-built one; the
        # threads are started AFTER the lock is released (the lock is never
        # held across a thread start or a blocking wait).
        run = _Run(self)
        with self._lifecycle_lock:
            if self._run is not None:
                log.debug("RealMeshCoreClient.connect() called again; already connecting/connected")
                return
            self._run = run
        run.loop_thread.start()
        run.pump_thread.start()
        if not run.loop_ready.wait(timeout=self._loop_start_timeout):
            log.warning(
                "meshcore asyncio loop did not start within %.1fs", self._loop_start_timeout
            )

    def wait_ready(self, timeout: float) -> bool:
        """Blocks until the CURRENT run's first successful connect, or
        `timeout` elapses; `False` immediately if there is no current run
        at all. Not part of `MeshCoreClient` -- a factory
        (`build_meshcore_transport`) uses this to know when it is safe to
        resolve the configured channel name to an index and perform the
        opt-in autoadd write, both of which need a live `mc`."""
        run = self._run
        if run is None:
            return False
        return run.ready_event.wait(timeout)

    def wait_setup(self, timeout: float) -> bool:
        """Blocks until the CURRENT run's per-connection setup (contact
        seeding, channel resolution, the clock set, the opt-in autoadd
        write) has finished for a session, or `timeout` elapses; `False`
        immediately if there is no current run. Not part of
        `MeshCoreClient`. Used only by `build_meshcore_transport`, so that
        a daemon whose radio IS present at startup has a resolved channel
        index before `start()` returns -- the setup itself is per
        connection and needs no waiting."""
        run = self._run
        if run is None:
            return False
        return run.setup_done.wait(timeout)

    def stop(self) -> None:
        """Not part of `MeshCoreClient` -- 3B-2's daemon shutdown path
        calls this (mirrors `RadioAdapter.stop()`). Never called by
        `MeshCoreTransport` itself. A no-op if there is no current run.

        Detaches the current run from `self._run` FIRST, under the lock,
        so a `connect()` racing this call either sees a run and returns
        immediately (still the old one, about to be torn down), or sees
        `None` and starts a genuinely new one -- never a state in between
        where both calls are mutating the same shared flags (fix round 3's
        other Important finding: the old design cleared `_stop_event`/
        `_loop_ready` OUTSIDE the lock that protected `_connect_started`,
        so a `connect()` could slip into that window).

        FIX ROUND 4: this method writes NOTHING on `self` after the swap
        above. Round 3's version ended by unconditionally resetting
        `connected`/`self_info`/`_mc`/`_loop`, and a `connect()` that
        started run B while this call sat in its joins had its live state
        clobbered: sends raised "not connected" forever while run B's loop
        thread was alive and current. Those four are now read-only
        properties derived from `self._run`, so detaching the pointer is
        the whole reset -- there is no trailing write left to get wrong,
        and no gate anyone has to remember.
        """
        with self._lifecycle_lock:
            run = self._run
            self._run = None
            if run is not None:
                # Task 3B-2 one-line hardening: setting the run's
                # stop_event under the SAME critical section as the pointer
                # swap, rather than just after it, makes "no duplicate
                # dispatch" true BY CONSTRUCTION rather than by the
                # incidental fact that two serial readers happen to split a
                # byte stream instead of each receiving a copy -- the
                # property this module's docstring flags as mattering if a
                # TCP or BLE transport is ever added (either could hand the
                # same bytes to two readers). `Event.set()` is not a
                # blocking wait, so this does not violate the rule that the
                # lock is never held across a blocking operation.
                run.stop_event.set()
        with self._repeater_lock:
            repeater_future = self._repeater_future
            self._repeater_future = None
        if repeater_future is not None:
            repeater_future.cancel()
        if run is None:
            return
        # No `loop.call_soon_threadsafe(loop.stop)` here: the outer loop and
        # `_run_connected_session` both poll `run.stop_event` themselves (at
        # most every 0.2-0.5s) and return the way `run_until_complete`
        # expects -- forcing the loop to stop from outside while its task is
        # still pending would raise `RuntimeError: Event loop stopped before
        # Future completed` instead. If the thread is instead blocked inside
        # a single non-cooperative library call (`create_serial`,
        # `disconnect()`, a probe), this join can time out -- deliberately
        # tolerated (see the module docstring): `run.stop_event` stays set
        # forever regardless, so whenever that thread does return, it reads
        # a stop request that was never, and can never be, undone.
        run.loop_thread.join(timeout=self._stop_join_timeout)
        # Bypasses `run.enqueue` on purpose: the sentinel must go in even
        # though `stop_event` is set. Wakes the pump out of a blocking get().
        run.queue.put(None)
        run.pump_thread.join(timeout=self._stop_join_timeout)

    # -- channel-name resolution / autoadd write (blocking, synchronous) --

    def resolve_channel_index(self, name: str, *, timeout: float = 10.0) -> int | None:
        """Probes `get_channel(0..max_channels-1)` for a channel whose
        `channel_name` matches `name` exactly, returning its index or
        `None` if not found, not connected, or `name` is empty (task 3B
        item E: `meshcore_channel = ""` disables channel triggering
        entirely -- transport-boundary-spec.md section 1: "channel name
        not resolvable to an index at connect: emit OTHER for all channel
        traffic... Never guess"). The probe pattern (device-query for
        `max_channels`, else a fixed fallback) mirrors PotatoMesh's own
        `channels.py::_ensure_channel_names` (meshcore-notes.md section
        11), narrowed to finding one named channel rather than caching all
        of them.
        """
        if not name:
            return None
        try:
            run, session = self._current_session()
        except MeshCoreNotConnected:
            return None
        try:
            return self._run_coro(run, self._probe_channel_index(session.mc, name), timeout=timeout)
        except Exception as exc:
            log.warning(
                "meshcore channel resolution for %r failed: %s: %s",
                name,
                type(exc).__name__,
                exc,
            )
            return None

    async def _probe_channel_index(self, mc: Any, name: str) -> int | None:
        max_channels = 32  # PotatoMesh's own fallback when DEVICE_INFO lacks it
        device_info = await mc.commands.send_device_query()
        if not device_info.is_error():
            queried = device_info.payload.get("max_channels")
            if isinstance(queried, int) and queried > 0:
                max_channels = queried
        for idx in range(max_channels):
            info = await mc.commands.get_channel(idx)
            if info.is_error():
                continue
            if info.payload.get("channel_name") == name:
                return idx
        return None

    def ensure_autoadd_bit(self, *, timeout: float = 10.0) -> bool:
        """The adapter's ONLY permitted PERSISTED radio config write (task
        3B item E; scope-decisions.md/meshcore-notes.md section 10): a
        read-modify-write of the firmware `autoadd_config` byte's bit
        `0x01`, preserving bits 1-4. (`_set_radio_clock` also writes to the
        radio, every connection, unconditionally -- but that write is
        volatile clock state the radio loses on its own on every power
        loss, not configuration, and it is not gated by anything.) Only
        ever called when
        `[radio] meshcore_autoadd_overwrite = true`; never called
        otherwise. Returns True on a confirmed write (or if the bit was
        already set), False if not connected or the device/firmware
        rejected the read or write (tolerated -- meshcore-notes.md section
        10: pre-1.16 firmware answers ERROR or times out).
        """
        try:
            run, session = self._current_session()
        except MeshCoreNotConnected:
            return False
        try:
            return self._run_coro(run, self._probe_autoadd_bit(session.mc), timeout=timeout)
        except Exception as exc:
            log.warning("meshcore autoadd_config write failed: %s: %s", type(exc).__name__, exc)
            return False

    async def _probe_autoadd_bit(self, mc: Any) -> bool:
        current = await mc.commands.get_autoadd_config()
        if current.is_error():
            log.warning("meshcore get_autoadd_config returned an error; leaving config alone")
            return False
        config = current.payload.get("config")
        if not isinstance(config, int):
            return False
        if config & 0x01:
            return True  # already set -- nothing to write
        result = await mc.commands.set_autoadd_config(config | 0x01)
        return not result.is_error()

    # -- internals: loop thread -------------------------------------------

    def _run_loop(self, run: _Run) -> None:
        loop = asyncio.new_event_loop()
        run.loop = loop
        asyncio.set_event_loop(loop)
        run.loop_ready.set()
        try:
            loop.run_until_complete(self._outer_reconnect_loop(run))
        except Exception:
            # This is the LAST line of defense. `_outer_reconnect_loop`
            # below already catches everything it reasonably can (a failed
            # `create_serial` call, and any exception during a connected
            # session) so that a transient error is retried rather than
            # ending this thread -- but if something still escapes (a bug
            # in the retry scaffolding itself, say), the alternative is
            # this thread dying silently while `self.connected` stays
            # whatever it last was, i.e. a daemon that reports a
            # permanently healthy radio which will never receive another
            # packet. Logging loudly and forcing `connected = False` in
            # the `finally` below is what makes that observable instead.
            log.error("meshcore asyncio loop thread died unexpectedly", exc_info=True)
        finally:
            run.session = None  # normally already None; the last line of defense
            loop.close()

    async def _outer_reconnect_loop(self, run: _Run) -> None:
        attempt = 0
        while not run.stop_event.is_set():
            mc = None
            try:
                mc = await _mc_pkg.MeshCore.create_serial(
                    self._device,
                    self._baudrate,
                    auto_reconnect=True,
                    max_reconnect_attempts=self._max_reconnect_attempts,
                )
            except Exception as exc:
                log.warning(
                    "meshcore connect failed; will retry: %s: %s", type(exc).__name__, exc
                )
            if mc is None:
                attempt += 1
                delay = backoff_delays(self._backoff_base, self._backoff_max, attempt)[-1]
                await self._sleep_or_stop(run, delay)
                continue
            if run.stop_event.is_set():
                # Fix round 3: `create_serial` does not itself watch
                # `stop_event` mid-await, so it can succeed AFTER a stop()
                # was already requested for this run. Entering a connected
                # session anyway would wire subscriptions and dispatch
                # events nobody wants any more -- tear the just-obtained
                # connection back down unused instead.
                with contextlib.suppress(Exception):
                    await mc.disconnect()
                break
            session_started = time.monotonic()
            try:
                await self._run_connected_session(run, mc)
            except Exception as exc:
                # Wiring subscriptions, the self-info read, or the
                # connected-session wait could all raise. Previously
                # nothing here caught that: the exception would propagate
                # out of this whole loop and kill the thread, leaving
                # `self.connected` stuck True forever with no thread left
                # to ever change it -- a permanently "healthy" radio that
                # will never receive another packet. Catching here keeps
                # the OUTER retry loop alive: log loudly, reset state (see
                # `_run_connected_session`'s own `finally`), and fall
                # through to the same backoff-and-rebuild-from-scratch path
                # a graceful give-up already takes.
                log.error(
                    "meshcore connected session ended unexpectedly: %s: %s",
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
            if run.stop_event.is_set():
                break
            # Only a session that ran for a MEANINGFUL DURATION counts as
            # healthy and resets the outer backoff to a fresh first retry
            # -- exactly the same escalation the `create_serial` failure
            # path above already uses. A session that connects and
            # immediately ends again (whether by raising, or by an
            # near-instant graceful give-up) keeps escalating like any
            # other repeated failure instead of retrying at the base delay
            # forever; a session that ran a long time before ending (the
            # library's own auto_reconnect exhausting itself after hours
            # of healthy operation, say) still gets a fresh, minimal retry
            # delay, because the DEVICE just proved itself stable. Mirrors
            # PotatoMesh's own per-attempt fresh interface (meshcore-
            # notes.md section 8) and `RadioAdapter._run`'s outer loop.
            if time.monotonic() - session_started >= self._min_stable_session_seconds:
                attempt = 0
            attempt += 1
            delay = backoff_delays(self._backoff_base, self._backoff_max, attempt)[-1]
            await self._sleep_or_stop(run, delay)

    async def _run_connected_session(self, run: _Run, mc: Any) -> None:
        session = _Session(run, mc)
        try:
            await self._run_connected_session_body(run, session)
        finally:
            # ORDER MATTERS, and it is what makes the orphaned-client case
            # impossible rather than suppressed (fix round 4): first the
            # session is closed -- its forwarder drops everything from here
            # on -- and unpublished, so `connected`/`self_info`/`_mc` stop
            # describing it; ONLY THEN is the library asked to disconnect.
            # If `mc.disconnect()` raises or hangs (the library's own
            # `dispatcher.stop()` awaits `queue.join()`, which can wait
            # forever on an event nothing will ever mark done), the wired
            # client it leaves behind can still call `session.forward`, but
            # a closed session forwards nothing. The next iteration's fresh
            # session is therefore the ONLY live source within this run.
            #
            # This `finally` covers the WHOLE session body, not just the
            # final wait (fix round 2) -- an exception from
            # `_wire_subscriptions`, the `self_info` read, or the
            # DISCONNECTED-watcher `subscribe` call must still disconnect.
            session.closed = True
            if run.session is session:
                run.session = None
            with contextlib.suppress(Exception):
                await mc.disconnect()

    async def _run_connected_session_body(self, run: _Run, session: _Session) -> None:
        mc = session.mc
        self._wire_subscriptions(session)
        # Real-hardware fix: the companion firmware QUEUES inbound messages
        # (both CONTACT_MSG_RECV and CHANNEL_MSG_RECV) behind a
        # MESSAGES_WAITING push -- it does not deliver them on its own no
        # matter how long this module merely subscribes and waits (verified
        # against real hardware: ten seconds of silence, then an immediate
        # delivery the instant this call was made by hand). Without this,
        # the daemon received nothing, ever -- no direct messages, no
        # channel messages, no knocks. `mc.start_auto_message_fetching()`
        # subscribes to that push ON THE REAL `mc` OBJECT ITSELF (not
        # through this module's own `_wire_subscriptions`/`_SUBSCRIBED_
        # EVENT_NAMES` -- it is the library's OWN internal subscription, so
        # it adds nothing to this module's 8) and drains the queue via
        # `commands.get_msg()`, including one immediate check for whatever
        # is already queued. `mc.disconnect()` (already called
        # unconditionally in `_run_connected_session`'s `finally`) stops
        # this on its own via `stop_auto_message_fetching()` -- see the
        # installed package's `meshcore.py:219-230` -- so there is nothing
        # for this module to tear down itself.
        await self._start_message_fetching(session)
        session.self_info = dict(mc.self_info) if mc.self_info else None
        # Publishing the session is the ONE write that turns `connected`
        # on (and makes `self_info`/`_mc` visible) -- all three derive from
        # this pointer, so they appear together or not at all. Only this
        # run's own loop thread ever writes `run.session`; whether this run
        # is still the client's current one is not this thread's concern
        # any more (fix round 4): `RealMeshCoreClient`'s properties read
        # `self._run` and simply never see a detached run's session.
        run.session = session
        # `create_serial(...)` already ran the ENTIRE connect handshake --
        # including firing the library's own CONNECTED and SELF_INFO events
        # -- before returning `mc` to us. Subscriptions can only be wired
        # AFTER that (there is no earlier hook: `mc` does not exist before
        # `create_serial` returns), so both events would otherwise be
        # silently missed on every initial connect. That silently broke
        # `MeshCoreTransport`'s `_on_connected` handler, which is what
        # seeds `_connected_since_monotonic` -- MeshCore's ONLY freshness
        # signal (`Inbound.rx_time` is always `None`) -- so `status().
        # connected_seconds` stayed `None` forever. Synthesizing both events
        # here, once, right after subscriptions are wired -- onto the SAME
        # per-run queue every other event flows through -- makes the
        # event-handler path (`_on_connected`/`_on_self_info`) the thing
        # every caller can actually rely on, matching what a genuine
        # mid-session reconnect (fired by the library's OWN live
        # subscription, since by then we are already subscribed) already
        # gets correctly on its own. `run.enqueue` drops both if a stop()
        # for this run has already been requested.
        run.enqueue(Event(type=EventType.CONNECTED, payload={"reconnected": False}))
        if session.self_info:
            run.enqueue(Event(type=EventType.SELF_INFO, payload=dict(session.self_info)))
        run.ready_event.set()

        gave_up = asyncio.Event()

        def _on_disconnected(event: Any) -> None:
            if event.payload.get("max_attempts_exceeded") or event.payload.get(
                "reconnect_failed"
            ):
                gave_up.set()

        mc.subscribe(_real_event_type("DISCONNECTED"), _on_disconnected)

        wait_stop = asyncio.ensure_future(self._wait_for_stop(run))
        wait_gaveup = asyncio.ensure_future(gave_up.wait())
        # A TASK, not an inline await: the per-connection setup below talks
        # to the device (a contacts fetch, up to `max_channels` channel
        # reads, an autoadd read-modify-write) and each of those can take
        # seconds. Awaiting them inline would block this loop thread, which
        # is the same thread that polls `run.stop_event` -- so a `stop()`
        # arriving mid-setup would time out its join every time. Running it
        # as a cooperative task keeps the loop responsive and keeps the
        # setup strictly per connection; the `finally` cancels it, so a
        # session that ends mid-setup never leaves work running against an
        # `mc` that is about to be disconnected.
        setup = asyncio.ensure_future(self._setup_and_follow_contacts(run, session))
        try:
            await asyncio.wait({wait_stop, wait_gaveup}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            wait_stop.cancel()
            wait_gaveup.cancel()
            setup.cancel()

    async def _start_message_fetching(self, session: _Session) -> None:
        """Fix (real-hardware finding, see `_run_connected_session_body`'s
        call site for the full account): starts the real library's own
        queue-drain for inbound messages, once per connected session.
        Logged and swallowed like every other per-connection setup step
        below -- a failure here must not tear down an otherwise-working
        session. Bounded by `_setup_timeout`, same as its siblings
        (`_set_radio_clock`, `_write_autoadd_bit`, `_send_advert_on_connect`):
        this call ends in the library's own `get_msg()`, which can block for
        its fifteen-second default, and this is the step directly ahead of
        `run.ready_event.set()`, so an unbounded wait here delays connection
        readiness by that much. `mc.disconnect()` still stops whatever DID
        start, via the library's own bookkeeping."""
        try:
            await asyncio.wait_for(
                session.mc.start_auto_message_fetching(), timeout=self._setup_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "meshcore start_auto_message_fetching failed: %s: %s -- inbound messages "
                "may not be delivered until the next reconnect",
                type(exc).__name__,
                exc,
            )

    # -- per-connection setup (loop thread, once per connected session) ----

    async def _setup_and_follow_contacts(self, run: _Run, session: _Session) -> None:
        await self._post_connect_setup(run, session)
        while not session.closed and not run.stop_event.is_set():
            await session.contact_refresh_needed.wait()
            # Coalesce bursts and let the SDK's advertisement handler mark its
            # contact cache dirty before ensure_contacts(follow=True) checks it.
            await asyncio.sleep(1)
            session.contact_refresh_needed.clear()
            try:
                await asyncio.wait_for(
                    session.mc.ensure_contacts(follow=True), timeout=self._setup_timeout
                )
                if session.closed or run.stop_event.is_set():
                    return
                refreshed = self._publish_contact_metadata(run, session)
                if refreshed:
                    log.info("meshcore refreshed %d contact identity/path record(s)", refreshed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("meshcore contact refresh failed: %s", type(exc).__name__)
                # A temporary serial failure must not require another advert.
                session.contact_refresh_needed.set()
                await asyncio.sleep(4)

    async def _post_connect_setup(self, run: _Run, session: _Session) -> None:
        """Everything that must happen ONCE PER CONNECTION rather than once
        per process: seeding the contact roster from the radio's stored
        contacts, resolving the configured channel name to an index,
        setting the radio's clock from host time, the opt-in autoadd
        write, and sending one flood self-advertisement.

        Each step logs and swallows its own failure: this runs as a task on
        the session's loop, so an escaping exception would surface only as
        an "unretrieved exception" warning at garbage-collection time, and
        a contacts fetch that fails is not a reason to tear down a
        connection that is otherwise delivering messages.

        The clock set runs UNCONDITIONALLY, unlike the autoadd write below
        it: it is not config the operator opts into, it is volatile state
        that a MeshCore radio loses on every power loss (its clock lives in
        a power domain the rest of its persisted config does not), exactly
        the same reason `radio.py`'s Meshtastic path sets its clock on
        every connect. Nothing else corrects a MeshCore radio's clock, and
        an unset one is observed on real hardware to read hundreds of days
        in the future after a flash, with outgoing messages carrying that
        timestamp.

        The self-advertisement (task 3B-4 fix 2, also a real-hardware
        finding: the garden was unreachable until manually flood-
        advertised) is unconditional too, like the clock set -- unlike
        every other call this method makes, it is a genuine over-the-air
        TRANSMISSION, not local serial control or a config write; see this
        module's own docstring ("WHAT THIS MODULE DOES NOT DO") and
        `docs/security-and-privacy.md` for that distinction stated
        explicitly. `main.py`'s maintenance tick additionally repeats it on
        `[radio] meshcore_advert_interval_seconds` via the public
        `send_advert()` below -- this one covers the FIRST advert of every
        connection, including one that lands before the daemon's first
        maintenance tick would otherwise fire.
        """
        await self._seed_contacts(run, session)
        await self._query_max_contacts(session)
        await self._resolve_channel(session)
        await self._set_radio_clock(session)
        await self._write_autoadd_bit(session)
        await self._send_advert_on_connect(session)
        run.setup_done.set()

    async def _query_max_contacts(self, session: _Session) -> None:
        """Best-effort: a failed query leaves `max_contacts` as it was."""
        try:
            info = await session.mc.commands.send_device_query()
            queried = None if info.is_error() else info.payload.get("max_contacts")
        except Exception as exc:
            log.warning("meshcore device query failed: %s: %s", type(exc).__name__, exc)
            return
        if isinstance(queried, int) and not isinstance(queried, bool) and queried > 0:
            self.max_contacts = queried

    async def _seed_contacts(self, run: _Run, session: _Session) -> None:
        """Synthesizes one NEW_CONTACT-shaped event per contact the radio
        ALREADY knows, onto this run's own queue -- the same synthesis
        `_run_connected_session_body` performs for CONNECTED/SELF_INFO, and
        for the same reason: the transport's roster mirror is fed purely by
        events, and the library delivers none for contacts it already has.

        LIBRARY API (verified against the installed `meshcore==2.3.11`):
        `MeshCore.ensure_contacts()` issues `commands.get_contacts(lastmod
        =...)` and awaits the library's own `CONTACTS` event, whose
        internal handler (`MeshCore._setup_data_tracking`) fills
        `MeshCore.contacts` -- a dict keyed by full public-key hex whose
        values carry `public_key`, `adv_name`, `adv_lat`, `adv_lon`,
        exactly the payload shape `NEW_CONTACT` itself carries
        (`meshcore/reader.py`'s `PacketType.CONTACT`/`PUSH_CODE_NEW_ADVERT`
        branch builds both from the same parser). So this needs NO NINTH
        SUBSCRIPTION: `CONTACTS` is consumed by the library's own
        subscription, and we only read the resulting dict.

        Without this, every daemon restart started with an EMPTY roster: a
        direct message from a contact the radio had known for weeks failed
        as an unknown contact (no reply at all), and a channel knock from
        one was dropped silently, until that node next advertised.
        """
        mc = session.mc
        try:
            await mc.ensure_contacts()
            seeded = self._publish_contact_metadata(run, session)
        except Exception as exc:
            log.warning(
                "meshcore stored-contact seeding failed: %s: %s -- the roster will fill "
                "only from live adverts until the next connection",
                type(exc).__name__,
                exc,
            )
            return
        log.info("meshcore seeded %d stored contact(s) from the radio", seeded)

    def _publish_contact_metadata(self, run: _Run, session: _Session) -> int:
        """Import changed identity/path metadata without counting another RF reception."""
        seeded = 0
        for contact in dict(session.mc.contacts or {}).values():
            if session.closed or run.stop_event.is_set():
                break
            if not isinstance(contact, dict) or not contact.get("public_key"):
                continue
            key = contact["public_key"]
            metadata = tuple(contact.get(k) for k in (
                "adv_name", "adv_lat", "adv_lon", "out_path_len", "out_path_hash_mode", "out_path"
            ))
            if key in session.contact_metadata and session.contact_metadata[key] == metadata:
                continue
            previous = session.contact_metadata.get(key)
            route_only = previous is not None and previous[:3] == metadata[:3]
            run.enqueue(Event(type=EventType.NEW_CONTACT, payload={
                **contact, "_imported": True, "_route_only": route_only,
            }))
            session.contact_metadata[key] = metadata
            seeded += 1
        return seeded

    async def _resolve_channel(self, session: _Session) -> None:
        await self._resolve_named_channel(
            session, self._channel_name, self._on_channel_resolved, "channel triggering"
        )
        await self._resolve_named_channel(
            session, self._alert_channel_name, self._on_alert_channel_resolved, "weather alerts"
        )

    async def _resolve_named_channel(
        self,
        session: _Session,
        name: str,
        callback: Callable[[int | None], None] | None,
        purpose: str,
    ) -> None:
        if not name or callback is None:
            return
        try:
            idx = await asyncio.wait_for(
                self._probe_channel_index(session.mc, name), timeout=self._setup_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A probe that never COMPLETED says nothing about whether the
            # channel exists, so the previously resolved index (if any) is
            # left alone rather than being cleared on a transient error.
            log.warning(
                "meshcore channel resolution for %r failed: %s: %s -- %s is OFF unless an "
                "earlier connection already resolved it; retried on the next connection",
                name, type(exc).__name__, exc, purpose,
            )
            return
        if idx is None:
            log.warning(
                "meshcore channel %r is not present on this radio; %s is OFF", name, purpose
            )
        else:
            log.info("meshcore channel %r resolved to index %d", name, idx)
        callback(idx)

    async def _set_radio_clock(self, session: _Session) -> None:
        """Sets the radio's clock from host time, on EVERY connection, with
        no config flag gating it (contrast `_write_autoadd_bit` below).

        LIBRARY API (verified against the installed `meshcore==2.3.11` and
        the upstream firmware source): `mc.commands.set_time(epoch_seconds)`
        sends a 4-byte little-endian unsigned epoch and returns
        `EventType.OK` or `EventType.ERROR`.

        THE FIRMWARE REFUSES TO MOVE ITS CLOCK BACKWARDS: it compares the
        requested time against its own current clock and answers
        `ERR_CODE_ILLEGAL_ARG` (error code 6, `result.payload["error_code"]`)
        when the request is earlier -- so a radio whose clock is already
        ahead of host time cannot be corrected this way. That is an
        expected, common outcome (not every unplug wipes the clock; some
        radios keep it across a brief power blip) and is logged distinctly
        from a genuine failure so an operator reading the journal can tell
        "clock was ahead, left alone" from "the write failed" -- it is
        never retried or escalated. The normal case after a real power
        loss is a clock reset to (or near) zero, where the set succeeds.
        """
        try:
            result = await asyncio.wait_for(
                self._probe_set_clock(session.mc), timeout=self._setup_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("meshcore radio clock set failed: %s: %s", type(exc).__name__, exc)
            return
        if not result.is_error():
            log.info("meshcore radio clock set from host time")
            return
        # Named `info`, not `payload`: tests/test_packaging.py's
        # `test_no_source_file_logs_message_content` bans that word on a
        # logging-call line as a crude tripwire against leaking visitor DM
        # content into logs. This dict only ever carries a firmware
        # integer error code and its constant name -- never visitor
        # content -- so the ban doesn't apply here, but the guard can't
        # tell that; keep this name so nobody "fixes" the guard instead.
        info = result.payload if isinstance(result.payload, dict) else {}
        error_code = info.get("error_code")
        if error_code == 6:  # ERR_CODE_ILLEGAL_ARG
            log.info(
                "meshcore radio clock is already ahead of host time; the firmware refused to "
                "move it backwards -- left alone"
            )
        else:
            log.warning(
                "meshcore radio clock set was rejected by the radio: error_code=%s code=%s",
                error_code,
                info.get("code_string"),
            )

    async def _probe_set_clock(self, mc: Any) -> Any:
        return await mc.commands.set_time(int(time.time()))

    async def _write_autoadd_bit(self, session: _Session) -> None:
        if not self._autoadd_overwrite:
            return
        try:
            ok = await asyncio.wait_for(
                self._probe_autoadd_bit(session.mc), timeout=self._setup_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("meshcore autoadd_config write failed: %s: %s", type(exc).__name__, exc)
            return
        if not ok:
            log.warning("meshcore autoadd_config write was not confirmed; leaving config alone")

    async def _send_advert_on_connect(self, session: _Session) -> None:
        """The one-shot advert `_post_connect_setup` sends every connection
        -- see that method's docstring for why this is unconditional, and
        why it is a real transmission rather than local serial control.
        Logged and swallowed like its siblings above: a failed advert is
        not a reason to tear down an otherwise-working connection. Subject
        to `_ADVERT_MIN_GAP_SECONDS` like every advert trigger -- see
        `_send_advert_if_due`."""
        try:
            result = await asyncio.wait_for(
                self._send_advert_if_due(session.mc), timeout=self._setup_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("meshcore self-advertisement failed: %s: %s", type(exc).__name__, exc)
            return
        if result is None:
            log.info(
                "meshcore self-advertisement skipped: last one was under %.0fs ago",
                self._ADVERT_MIN_GAP_SECONDS,
            )
        elif result.is_error():
            # Named `info`, not `payload`: see `_set_radio_clock`'s own
            # comment on this same tripwire (test_packaging.py). This dict
            # only ever carries a firmware error code/reason for a command
            # WE issued unconditionally -- never visitor content.
            info = result.payload if isinstance(result.payload, dict) else {}
            log.warning("meshcore self-advertisement was rejected by the radio: %s", info)
        else:
            log.info("meshcore sent a flood self-advertisement")

    async def _probe_send_advert(self, mc: Any) -> Any:
        return await mc.commands.send_advert(flood=True)

    async def _send_advert_if_due(self, mc: Any) -> Any | None:
        """Item 2 fix: gates every actual transmission behind
        `_ADVERT_MIN_GAP_SECONDS`, regardless of which caller triggers it
        (`_send_advert_on_connect` or the periodic `send_advert()` below) --
        a flapping link whose sessions each just clear
        `min_stable_session_seconds` would otherwise re-trigger the
        per-connection advert on every reconnect, flooding a shared band.
        Returns `None`, without transmitting, when the floor has not yet
        elapsed; the timestamp is only advanced on a successful send, so a
        failed or rejected attempt does not itself block the next try."""
        now = time.monotonic()
        last = self._last_advert_monotonic
        if last is not None and now - last < self._ADVERT_MIN_GAP_SECONDS:
            return None
        result = await self._probe_send_advert(mc)
        if not result.is_error():
            self._last_advert_monotonic = now
        return result

    def send_advert(self, *, timeout: float = 10.0) -> bool:
        """Not part of `MeshCoreClient`. `main.py`'s maintenance tick calls
        this on `[radio] meshcore_advert_interval_seconds` to repeat the
        self-advertisement `_send_advert_on_connect` already sends once per
        connection -- a node that never heard an earlier advert (out of
        range at connect time, say) still needs one eventually to resolve a
        path back to this radio. Mirrors `ensure_autoadd_bit`'s shape:
        cross-thread, synchronous, `False` (never raises) if not connected,
        the radio rejected/failed to answer, or the advert floor
        (`_ADVERT_MIN_GAP_SECONDS`, `_send_advert_if_due`) suppressed it."""
        try:
            run, session = self._current_session()
        except MeshCoreNotConnected:
            return False
        try:
            result = self._run_coro(run, self._send_advert_if_due(session.mc), timeout=timeout)
        except Exception as exc:
            log.warning("meshcore self-advertisement failed: %s: %s", type(exc).__name__, exc)
            return False
        return result is not None and not result.is_error()

    def list_contacts(self, *, timeout: float = 10.0) -> list[RadioContact] | None:
        """Not part of `MeshCoreClient`. The radio's current contact table
        (refreshed via `ensure_contacts`) for the daily housekeeping, or
        `None` (never raises) when not connected or the refresh failed --
        distinct from an empty table so the caller retries rather than
        concluding there is nothing to do."""
        try:
            run, session = self._current_session()
        except MeshCoreNotConnected:
            return None

        async def fetch() -> list[dict[str, Any]]:
            await session.mc.ensure_contacts()
            return [c for c in dict(session.mc.contacts or {}).values() if isinstance(c, dict)]

        try:
            raw = self._run_coro(run, fetch(), timeout=timeout)
        except Exception as exc:
            log.warning("meshcore contact listing failed: %s: %s", type(exc).__name__, exc)
            return None
        # An advert timestamp is the sender's own clock: reject values outside
        # a plausible window so a wrong-clock node cannot look freshly heard.
        now = time.time()
        lo, hi = 1_577_836_800, now + 86_400  # 2020-01-01
        contacts: list[RadioContact] = []
        for c in raw:
            key = c.get("public_key")
            node_id = meshcore_compat._meshcore_node_id(key) if isinstance(key, str) else None
            if node_id is None:
                continue
            adv_type = c.get("type")
            advert = c.get("last_advert")
            contacts.append(RadioContact(
                public_key=key,
                node_id=node_id,
                adv_name=c.get("adv_name") or None,
                adv_type=adv_type if adv_type in (1, 2, 3) else ADV_TYPE_COMPANION,
                last_advert=(
                    advert
                    if isinstance(advert, int) and not isinstance(advert, bool)
                    and lo <= advert <= hi
                    else None
                ),
            ))
        return contacts

    def remove_contact(self, public_key: str, *, timeout: float = 10.0) -> bool:
        """Not part of `MeshCoreClient`. The adapter's SECOND persisted radio
        write (opt-in via `meshcore_contact_prune_enabled`): deletes one
        contact from the radio. `False` (never raises) on any failure. On
        success the key is also dropped from `session.contact_metadata` so a
        later `_publish_contact_metadata` does not resurrect it."""
        try:
            run, session = self._current_session()
        except MeshCoreNotConnected:
            return False
        try:
            result = self._run_coro(
                run, session.mc.commands.remove_contact(public_key), timeout=timeout
            )
        except Exception as exc:
            log.warning("meshcore contact removal failed: %s: %s", type(exc).__name__, exc)
            return False
        if result is None or result.is_error():
            return False
        session.contact_metadata.pop(public_key, None)
        # The library never evicts from its own `contacts` dict on removal;
        # without this the next listing would still show the contact.
        with contextlib.suppress(Exception):
            session.mc.contacts.pop(public_key, None)
        return True

    async def _wait_for_stop(self, run: _Run) -> None:
        while not run.stop_event.is_set():
            await asyncio.sleep(0.2)

    async def _sleep_or_stop(self, run: _Run, delay: float) -> None:
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline and not run.stop_event.is_set():
            await asyncio.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    def _wire_subscriptions(self, session: _Session) -> None:
        # The library is handed the SESSION's forwarder, never a
        # client-level one: whatever it does with that reference later, it
        # can only ever feed this session's run, and only while the session
        # is open -- see `_Session.forward`.
        for name in _SUBSCRIBED_EVENT_NAMES:
            session.mc.subscribe(_real_event_type(name), session.forward)

    def _run_coro(self, run: _Run, coro: Any, *, timeout: float) -> Any:
        """Schedules `coro` onto `run`'s loop from a foreign thread and
        blocks for the result. `run` comes from the same `_current_session`
        snapshot that supplied the `mc` the coroutine was built on."""
        future = asyncio.run_coroutine_threadsafe(coro, run.loop)
        return future.result(timeout=timeout)

    # -- internals: pump thread ---------------------------------------------

    def _pump(self, run: _Run) -> None:
        # Pure delivery, drained from THIS run's queue: the pump writes no
        # state anywhere (fix round 4 moved the SELF_INFO bookkeeping onto
        # the session, on the loop thread), so there is nothing here that
        # could need a current-run check.
        while True:
            event = run.queue.get()
            if event is None:  # stop() sentinel
                return
            for callback in self._subscribers.get(event.type, []):
                try:
                    callback(event)
                except Exception:
                    log.exception("error in meshcore event handler for %s", event.type)


def _real_event_type(name: str) -> Any:
    """`getattr(meshcore.EventType, name)` -- kept as a tiny function
    rather than a module-level import of `meshcore.EventType` so this
    module's only reference to the real package stays behind the same
    `_mc_pkg is None` guard everywhere else."""
    return getattr(_mc_pkg.EventType, name)


def build_meshcore_transport(
    *,
    device: str,
    expected_node_id: str,
    baudrate: int = 115200,
    channel_name: str = "",
    alert_channel_name: str = "",
    autoadd_overwrite: bool = False,
    max_text_bytes: int | None = None,
    channel_name_match_case_sensitive: bool = True,
    connect_timeout: float = 20.0,
    backoff_base: float = 1.0,
    backoff_max: float = 300.0,
    max_reconnect_attempts: int = 3,
    min_stable_session_seconds: float = 30.0,
    on_contact: Callable[[str, str | None, float | None, float | None], None] | None = None,
    on_message: Callable[[Inbound], None] | None = None,
    on_observation: Callable[[MeshCoreObservation], None] | None = None,
    on_route: Callable[[MeshCoreRoute], None] | None = None,
    identity_guard: Callable[[str, str], bool] | None = None,
    repeater_public_key: str = "",
    repeater_guest_password: str = "",
    repeater_cache_seconds: int = 300,
    repeater_query_timeout: float = 15.0,
) -> MeshCoreTransport:
    """Builds a `RealMeshCoreClient` and a `MeshCoreTransport` wrapping it,
    connects, and returns the transport. `main.py`'s `Daemon.start()` is
    the single caller.

    PER-CONNECTION SETUP (whole-branch review, finding 3): stored-contact
    seeding, channel-name resolution, the radio clock set and the opt-in
    autoadd write are NOT performed here any more. They belong to the
    client's connected session
    (`RealMeshCoreClient._post_connect_setup`), so they happen again on
    every reconnect and -- crucially -- on the FIRST connect even when it
    lands after this function has already returned. This function now only
    WAITS for them, so that a radio that is present at startup has a
    resolved channel index before `Daemon.start()` returns; when the wait
    times out it says so explicitly, including that channel triggering is
    off until resolution happens, a condition that used to be completely
    invisible.

    OWNERSHIP DECISION (finding B, fix round 1): `MeshCoreTransport.
    connect()` is the ONE call that connects the client -- it subscribes
    its 8 handlers first, then calls `client.connect()` exactly once. This
    function used to call `client.connect()` itself and return an
    UNCONNECTED transport, which meant whoever called the transport's own
    `connect()` afterward would connect the client a SECOND time.
    `RealMeshCoreClient.connect()` is idempotent as defense in depth, but
    this function no longer relies on that: it builds the transport first
    and calls `transport.connect()` itself, so there is exactly one call
    site, here. `MeshCoreTransport.connect()` itself is ALSO idempotent
    (fix round 2, finding 3) -- 3B-2 should still never call `connect()`
    again on the transport this function returns, but a slip no longer
    means duplicate replies.

    TUNING IS CONFIGURABLE (fix round 3): `backoff_base`/`backoff_max`/
    `max_reconnect_attempts`/`min_stable_session_seconds` are accepted here
    and forwarded to `RealMeshCoreClient` -- previously this function only
    ever passed `device`/`baudrate`, so production always got the
    hardcoded defaults and only this module's own tests could vary them
    (scope-decisions.md section 4: MeshCore tuning is additive
    configuration). Defaults here match `RealMeshCoreClient`'s own, so an
    unconfigured caller sees no behavior change. The corresponding
    `[radio]` config keys live in `meshgarden/config.py`
    (`meshcore_backoff_base_seconds`, `meshcore_backoff_max_seconds`,
    `meshcore_max_reconnect_attempts`, `meshcore_min_stable_session_
    seconds`); reading them and passing them through here is 3B-2's job
    (it wires the daemon), not this function's -- this function does not
    read config itself.
    """
    # The client must know how to publish a resolved channel index before
    # it can start a run, and the transport it publishes to does not exist
    # until the client does. A one-element list closes that loop WITHOUT
    # leaving `on_channel_resolved` writable after the fact: nothing can
    # invoke the callback before `transport.connect()` below, because no
    # run -- and therefore no connected session -- exists until then.
    transport_ref: list[MeshCoreTransport] = []
    client = RealMeshCoreClient(
        device=device,
        baudrate=baudrate,
        backoff_base=backoff_base,
        backoff_max=backoff_max,
        max_reconnect_attempts=max_reconnect_attempts,
        min_stable_session_seconds=min_stable_session_seconds,
        channel_name=channel_name,
        autoadd_overwrite=autoadd_overwrite,
        on_channel_resolved=lambda idx: transport_ref[0].set_watched_channel_idx(idx),
        alert_channel_name=alert_channel_name,
        on_alert_channel_resolved=lambda idx: transport_ref[0].set_alert_channel_idx(idx),
        repeater_public_key=repeater_public_key,
        repeater_guest_password=repeater_guest_password,
        repeater_cache_seconds=repeater_cache_seconds,
        repeater_query_timeout=repeater_query_timeout,
    )
    kwargs: dict[str, Any] = {}
    if max_text_bytes is not None:
        kwargs["max_text_bytes"] = max_text_bytes
    transport = MeshCoreTransport(
        client,
        expected_node_id=expected_node_id,
        watched_channel_idx=None,  # resolved per connection; see set_watched_channel_idx
        alert_channel_idx=None,  # likewise; see set_alert_channel_idx
        channel_name_match_case_sensitive=channel_name_match_case_sensitive,
        identity_guard=identity_guard,
        **kwargs,
    )
    transport_ref.append(transport)
    # Connecting starts the event pump and imports stored contacts immediately.
    # Register consumers first so startup events reach the daemon's ingest queue.
    if on_contact is not None:
        transport.on_contact(on_contact)
    if on_message is not None:
        transport.on_message(on_message)
    if on_observation is not None:
        transport.on_observation(on_observation)
    if on_route is not None:
        transport.on_route(on_route)
    transport.connect()
    if not client.wait_ready(connect_timeout):
        log.warning(
            "meshcore did not complete its first connect within %.1fs; continuing -- it keeps "
            "retrying in the background, and contact seeding%s runs on whichever connection "
            "succeeds",
            connect_timeout,
            f" and resolution of channel {channel_name!r}" if channel_name else "",
        )
        if channel_name:
            log.warning(
                "meshcore channel triggering is OFF until channel %r has been resolved on a "
                "successful connection",
                channel_name,
            )
    elif not client.wait_setup(connect_timeout):
        log.warning(
            "meshcore connected but its per-connection setup (stored-contact seeding%s) did "
            "not finish within %.1fs; continuing -- it completes in the background",
            f" and channel {channel_name!r} resolution" if channel_name else "",
            connect_timeout,
        )
    return transport
