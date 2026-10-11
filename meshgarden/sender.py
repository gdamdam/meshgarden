"""The single audited application transmission path.

`Sender.pump()` is the ONLY code in this package that may call
`Transport.send_dm` (which, for `MeshtasticTransport`, is the only caller
of the underlying `RadioAdapter.send_text`). Every application reply -- to
every command, on every code path -- flows through `Sender.enqueue()` then
`Sender.pump()`. Enforced statically by `tests/test_central_invariant.py::
test_only_sender_module_calls_send_text`, and behaviourally by the
channel-broadcast invariant tests alongside it.

`pump()` re-validates radio identity, request state, recipient shape,
block-list membership, TTL, and rate limits immediately before every send --
never relying on checks done at enqueue time, since the world can change
while a reply sits in the queue (reconnect, block-list edit, a cooldown
started by another reply). Refusals are silent to the network: a rejected
job is dropped with a bounded reason code, never turned into error traffic.

Up to three delayed application-level retries may be configured. They use
a fixed interval (15 seconds by default), run only while the logical reply is still unacknowledged,
are cancelled by a late ACK or a newer accepted reply to the same node, and
still obey the global airtime cooldown. MeshCore resets a stale contact route
before retry 2 so the final two retries flood; it never changes destination.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace

from meshgarden import exchange_log
from meshgarden.commands import Reject, RequestLog
from meshgarden.config import NODE_ID_RE, ServiceConfig
from meshgarden.fortune import local_iso
from meshgarden.metrics import Metrics
from meshgarden.models import ReplyJob
from meshgarden.radio import SendNotReady
from meshgarden.transport import Delivery, DeliveryOutcome, Transport

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PendingRetry:
    job: ReplyJob
    attempt: int
    due_monotonic: float
    expires_monotonic: float


class SendRefused(Exception):
    """Raised by the send-time guard for any hard refusal. Never retried."""

    def __init__(self, reason: Reject) -> None:
        super().__init__(reason.value)
        self.reason = reason


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncates on a valid UTF-8 character boundary. Never splits a codepoint."""
    if max_bytes <= 0:
        return ""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    cut = data[:max_bytes]
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""


class RateLimiter:
    """Monotonic-clock limiter. Separate from the queue so it is unit-testable.

    Three independent windows: per-sender command cooldown, a single global
    transmit cooldown, and a per-sender SIGN cooldown. All comparisons use
    the injected clock (`time.monotonic` in production) so a wall-clock step
    (NTP adjustment, DST, operator changing the system clock) can never
    unlock or shorten a cooldown.
    """

    def __init__(self, cfg: ServiceConfig, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._cfg = cfg
        self._clock = clock
        self._lock = threading.Lock()
        self._command_last: dict[str, float] = {}
        self._sign_last: dict[str, float] = {}
        self._global_tx_last: float | None = None

    def check_command(self, sender: str) -> Reject | None:
        """per_node_cooldown_seconds. Read-only; does not consume."""
        with self._lock:
            last = self._command_last.get(sender)
            if last is not None and self._clock() - last < self._cfg.per_node_cooldown_seconds:
                return Reject.RATE_LIMITED
            return None

    def commit_command(self, sender: str) -> None:
        with self._lock:
            self._command_last[sender] = self._clock()

    def command_cooldown_remaining(self, sender: str) -> int:
        """Whole seconds until another command is allowed, rounded up."""
        with self._lock:
            last = self._command_last.get(sender)
            if last is None:
                return 0
            remaining = self._cfg.per_node_cooldown_seconds - (self._clock() - last)
            return max(0, math.ceil(remaining))

    def check_global_tx(self) -> Reject | None:
        """global_tx_cooldown_seconds."""
        with self._lock:
            if (
                self._global_tx_last is not None
                and self._clock() - self._global_tx_last < self._cfg.global_tx_cooldown_seconds
            ):
                return Reject.RATE_LIMITED
            return None

    def commit_global_tx(self) -> None:
        with self._lock:
            self._global_tx_last = self._clock()

    def check_sign(self, sender: str) -> Reject | None:
        """sign_cooldown_seconds."""
        with self._lock:
            last = self._sign_last.get(sender)
            if last is not None and self._clock() - last < self._cfg.sign_cooldown_seconds:
                return Reject.SIGN_COOLDOWN
            return None

    def commit_sign(self, sender: str) -> None:
        with self._lock:
            self._sign_last[sender] = self._clock()

    def sign_cooldown_remaining(self, sender: str) -> int:
        """Seconds left on `sender`'s SIGN cooldown, rounded up so a visitor
        is never told "0" while still blocked; 0 if not on cooldown. Read-
        only -- callers use this only after `check_sign` has already
        returned SIGN_COOLDOWN, to word the rejection reply."""
        with self._lock:
            last = self._sign_last.get(sender)
            if last is None:
                return 0
            remaining = self._cfg.sign_cooldown_seconds - (self._clock() - last)
            return max(0, math.ceil(remaining))

    def reset_sign(self, sender: str) -> None:
        """Clears `sender`'s SIGN cooldown entirely (UNSIGN frees the slot
        it just hid) -- unlike `commit_sign`, which sets it to "now", this
        drops the timestamp so an immediate re-SIGN is not cooldown-blocked."""
        with self._lock:
            self._sign_last.pop(sender, None)

    def purge(self) -> None:
        """Drops entries older than the longest window so the dicts stay bounded."""
        window = max(self._cfg.per_node_cooldown_seconds, self._cfg.sign_cooldown_seconds)
        with self._lock:
            now = self._clock()
            self._command_last = {
                sender: t for sender, t in self._command_last.items() if now - t < window
            }
            self._sign_last = {
                sender: t for sender, t in self._sign_last.items() if now - t < window
            }


class ReplyQueue:
    """Bounded, at most one pending reply per sender.

    "Pending" spans the whole lifetime of a reply, not just the time it
    sits in the deque: a sender's slot is held from `offer()` until the
    job is fully resolved (submitted, failed, dropped, or expired), so a
    sender can never have two replies in flight at once -- one popped and
    mid-send, plus a second one freshly enqueued. `pop_ready` therefore
    frees a slot immediately only for jobs it expires (those resolve right
    there, as `expired`); the job it *returns* keeps its slot held until
    the caller calls `release(key)`. `requeue` is the escape hatch for
    the one case a popped-but-unresolved job goes back in unsent: a global
    cooldown still in effect at send time (see `Sender.pump`'s "no burst
    after reconnect" comment) -- the slot stays held across that requeue,
    since the reply still hasn't resolved.

    Task 3C finding 1: the "one pending reply" slot is keyed on
    `job.queue_key()` (`ReplyJob.reply_queue_key`, or `request_sender` when
    unset), NOT always `request_sender` directly -- a channel knock's reply
    is addressed to the resolved contact, but that contact did not send the
    triggering message; sharing that node's own slot let a forged knock
    occupy the slot the node's own genuine next command needed. Every
    ordinary command reply leaves `reply_queue_key` unset, so its behavior
    is unchanged: keyed on its own `request_sender`, exactly as before.

    Task 3C finding 2: a SEPARATE, smaller cap (`max_pending_knocks`) bounds
    how many `is_knock` jobs may be concurrently held (queued or popped-but-
    unresolved), independent of the per-key slot above and independent of
    `max_pending_replies` -- because each forged knock can name a DIFFERENT
    (attacker-minted) contact, giving each its OWN `queue_key()`, so the
    per-key rule alone would never bound how many of them can pile up
    together and crowd out genuine replies in the shared deque.

    All mutable state is guarded by one lock: `enqueue()` (DB worker
    thread) and `pump()` (TX worker thread) touch this queue concurrently,
    and `offer`/`pop_ready` are each check-then-act. The lock is only ever
    held for these in-memory operations, never across a radio call or a
    database commit.
    """

    def __init__(self, cfg: ServiceConfig, *, max_pending_knocks: int = 1) -> None:
        self._holds_waived = False
        self._max = cfg.max_pending_replies
        self._max_knocks = max_pending_knocks
        self._queue: deque[ReplyJob] = deque()
        self._pending_senders: set[str] = set()
        self._pending_knock_keys: set[str] = set()
        self._lock = threading.Lock()

    def _would_accept(self, key: str, *, is_knock: bool) -> Reject | None:
        if key in self._pending_senders:
            return Reject.RATE_LIMITED
        if is_knock and len(self._pending_knock_keys) >= self._max_knocks:
            return Reject.QUEUE_FULL
        if len(self._queue) >= self._max:
            return Reject.QUEUE_FULL
        return None

    def offer(self, job: ReplyJob) -> Reject | None:
        """Returns None on success; QUEUE_FULL or RATE_LIMITED on refusal.

        Refuses a second job for the same `queue_key()` that already has
        one pending -- including one already popped and mid-send -- and,
        for a knock, also refuses once `max_pending_knocks` are already
        pending regardless of key.
        """
        key = job.queue_key()
        with self._lock:
            reason = self._would_accept(key, is_knock=job.is_knock)
            if reason is not None:
                return reason
            self._queue.append(job)
            self._pending_senders.add(key)
            if job.is_knock:
                self._pending_knock_keys.add(key)
            return None

    def reserve(self, key: str, *, is_knock: bool = False) -> Reject | None:
        """Checks whether offer() would succeed, WITHOUT enqueueing.

        `key` must be the SAME value the caller will use as `job.
        queue_key()` if it goes on to construct a `ReplyJob` -- for a
        knock, that is `job.reply_queue_key`, never the plain resolved
        node id.
        """
        with self._lock:
            return self._would_accept(key, is_knock=is_knock)

    def pop_ready(self, now_monotonic: float) -> tuple[ReplyJob | None, list[ReplyJob]]:
        """Returns (next job or None, list of jobs that expired). FIFO.

        The returned job's slot stays held -- the caller must call
        `release(job.queue_key())` once it resolves the job (or
        `requeue(job)` if a soft cooldown defer means it isn't resolved
        yet).
        """
        expired: list[ReplyJob] = []
        held: list[ReplyJob] = []
        with self._lock:
            try:
                while self._queue:
                    candidate = self._queue.popleft()
                    if now_monotonic > candidate.deadline_monotonic:
                        key = candidate.queue_key()
                        self._pending_senders.discard(key)
                        self._pending_knock_keys.discard(key)
                        expired.append(candidate)
                        continue
                    if (
                        not self._holds_waived
                        and now_monotonic < candidate.not_before_monotonic
                    ):
                        # Not yet sendable (a delayed channel reply): keep its
                        # slot and its place, but let a ready job behind it
                        # (a DM, typically) go first.
                        held.append(candidate)
                        continue
                    return candidate, expired
                return None, expired
            finally:
                for job in reversed(held):
                    self._queue.appendleft(job)

    def waive_holds(self) -> None:
        """TEST SUPPORT ONLY (Daemon.drain): makes pop_ready ignore
        `not_before_monotonic`, so a test driven by an injected clock is not
        stuck behind a channel-reply delay it never advanced past."""
        with self._lock:
            self._holds_waived = True

    def requeue(self, job: ReplyJob) -> None:
        """Puts a job popped this cycle but not sent (soft cooldown defer)
        back at the front, preserving FIFO order for the next pump(). Its
        slot was never released, so it is simply re-marked here (idempotent)
        rather than re-acquired."""
        with self._lock:
            self._queue.appendleft(job)
            key = job.queue_key()
            self._pending_senders.add(key)
            if job.is_knock:
                self._pending_knock_keys.add(key)

    def release(self, key: str) -> None:
        """Frees a slot once its popped job has fully resolved (submitted,
        failed, or dropped). Idempotent. `key` must be `job.queue_key()`,
        not necessarily the plain node id."""
        with self._lock:
            self._pending_senders.discard(key)
            self._pending_knock_keys.discard(key)

    def depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def in_flight(self) -> int:
        """Count of jobs that are queued OR popped-but-not-yet-resolved.

        Stronger than depth() for detecting when all outstanding work has
        truly finished: depth() drops to 0 the instant pop_ready() pops a
        job, well before pump() has actually sent (or failed/dropped) it --
        the sender's slot, tracked here, isn't freed until release() is
        called once the job is fully resolved.
        """
        with self._lock:
            return len(self._pending_senders)


class Sender:
    """THE single audited application transmission path.

    No module but this one's `transport.send_dm` call may transmit. Enforced
    by tests/test_central_invariant.py::test_only_sender_module_calls_send_text
    (the underlying `RadioAdapter.send_text`/`sendText` call itself now lives
    in `meshtastic_transport.py`, which that test also allows).

    Thread ownership (every sqlite3.Connection is used by exactly one
    thread; see database.connect): `enqueue()` and `on_delivery()` are
    called from the daemon's DB worker thread and use `request_log`, which
    is built on the DB worker's own connection. `pump()` (and everything it
    calls: `_finish_expired`, `_guarded_send`, `_mark_dropped`,
    `_mark_internal_error`) runs on the TX worker thread and uses
    `tx_request_log`, built on the TX worker's own, separate connection.
    Callers that only ever run single-threaded (e.g. tests) may pass one
    `RequestLog` and omit `tx_request_log`; it then defaults to the same
    object as `request_log`.
    """

    def __init__(
        self,
        *,
        transport: Transport,
        cfg: ServiceConfig,
        limiter: RateLimiter,
        queue: ReplyQueue,
        request_log: RequestLog,
        metrics: Metrics,
        expected_node_id: str,
        is_blocked: Callable[[str], bool],
        tx_request_log: RequestLog | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._cfg = cfg
        self._limiter = limiter
        self._queue = queue
        self._request_log = request_log
        self._tx_request_log = tx_request_log if tx_request_log is not None else request_log
        self._metrics = metrics
        self._expected_node_id = expected_node_id
        self._is_blocked = is_blocked
        self._clock = clock
        # tx_ref correlation for every attempt. The DB's integer
        # `tx_packet_id` remains useful for operator inspection and restart
        # compatibility, while this map can retain both the original and
        # retry refs for one logical request. Written on the TX worker and
        # popped by `on_delivery` on the DB worker -- genuinely cross-thread,
        # hence the lock (same pattern as RateLimiter's).
        # Bounded by opportunistic pruning keyed to ack_timeout_seconds:
        # a reply that never gets an ACK ages to 'unknown' in the DB via
        # Retention.age_stale_submitted regardless of this map, so an entry
        # nothing will ever pop is swept once it is that old too, keeping
        # this from growing without bound on a lossy MeshCore link.
        self._delivery_lock = threading.Lock()
        self._send_in_progress = False
        self._early_deliveries: deque[Delivery] = deque(maxlen=128)
        self._tx_ref_lock = threading.Lock()
        self._pending_by_tx_ref: dict[str, tuple[str, int, float, int]] = {}
        self._retry_lock = threading.Lock()
        self._pending_retries: dict[tuple[str, int], _PendingRetry] = {}
        self._mobile_recovery_last: dict[str, float] = {}
        self._mobile_recovery_global: deque[float] = deque()

    def _record_mobile_stage(self, stage: str, recipient: str) -> None:
        self._metrics.mobile_dm_recovery_total.labels(stage).inc()
        ts = local_iso(int(time.time()), self._cfg.timezone)
        exchange_log.log_line(
            f"{ts} mobile DM to={exchange_log.node_label(recipient)} stage={stage}"
        )

    def _mobile_policy_for(self, recipient: str) -> bool:
        allowed = self._cfg.mobile_dm_recovery_destinations
        return (
            self._cfg.mobile_dm_recovery_enabled
            and hasattr(self._transport, "dm_ack_wait_seconds")
            and (not allowed or recipient.lower() in allowed)
        )

    def _mobile_recovery_enabled_for(self, recipient: str) -> bool:
        return self._mobile_policy_for(recipient) and self._cfg.mobile_dm_max_retries == 1

    def enqueue(self, job: ReplyJob) -> Reject | None:
        """Called from the daemon's DB worker thread. Touches only the
        in-memory queue/metrics -- no database connection."""
        delay = self._cfg.channel_reply_delay_seconds
        if job.reply_channel_idx is not None and delay > 0 and job.not_before_monotonic == 0.0:
            # See ReplyJob.not_before_monotonic: let the request's own flood
            # (the repeaters re-sending it) die down before answering on the
            # channel. DMs are never delayed.
            job = replace(job, not_before_monotonic=self._clock() + delay)
        reason = self._queue.offer(job)
        if reason is None and not job.is_cooldown_notice:
            # A newer reply makes an older, unacknowledged response stale and
            # potentially confusing. Let the new command's response serve as
            # the recovery instead of sending both.
            self._cancel_retries_for_recipient(job.request_sender, "newer_reply")
        self._metrics.queue_depth.set(self._queue.depth())
        return reason

    def pump(self) -> None:
        """Called repeatedly by the TX worker thread. Sends AT MOST ONE reply.
        Everything this method (transitively) touches on `tx_request_log`
        must only ever run on the TX worker thread.

        No burst after reconnect: at most one job is popped per call, and
        the global cooldown is (re)checked and committed on every send, so a
        backlog built up while disconnected cannot flush faster than one
        reply per global_tx_cooldown_seconds once the radio reconnects.
        """
        self._limiter.purge()
        job, expired = self._queue.pop_ready(self._clock())
        now = int(time.time())
        for expired_job in expired:
            # Hardened (fix round 2): this loop runs BEFORE `job`'s own
            # try/finally below, which is what actually releases (or
            # requeues) `job`'s sender slot. `pop_ready` already discarded
            # each `expired_job`'s OWN sender from `_pending_senders`
            # before returning it here, so an exception finishing one
            # expired job cannot wedge that job's own sender -- but,
            # uncaught, it WOULD abort this loop and skip straight past
            # `job`'s try/finally entirely, permanently leaking THAT
            # unrelated, still-valid job's sender from `_pending_senders`
            # (`release()` never called) for the rest of the process's
            # life. A single bad expired job must never take an unrelated
            # ready job's slot down with it -- and this hazard predates
            # (and is broader than) any particular callback: any future
            # exception here, for any reason, must stay contained to this
            # one expired job.
            try:
                self._finish_expired(expired_job, now)
            except Exception:
                # Fix round 3 (item 1): log.exception (not log.error with a
                # fixed string) so the traceback/exception identity survives
                # -- matching this module's own convention at _send_one's
                # "unexpected error in send-time guard" branch and main.py's
                # db-worker loop -- plus a metric, so this is visible even
                # without --debug and without reading logs at all.
                log.exception(
                    "unexpected error finishing expired reply for sender=%s",
                    expired_job.request_sender,
                )
                self._metrics.reply_callback_errors_total.inc()
        self._metrics.queue_depth.set(self._queue.depth())

        if job is None:
            self._pump_retry(now)
            return
        defer = False  # never leave this unbound: an unexpected exception below
        # must not turn into an UnboundLocalError that masks the real error.
        try:
            with self._delivery_lock:
                self._send_in_progress = True
            try:
                defer = self._send_one(job, now)
            finally:
                with self._delivery_lock:
                    self._send_in_progress = False
        finally:
            if defer:
                self._queue.requeue(job)
            else:
                # Task 3C finding 1: release by queue_key(), not always
                # request_sender -- a knock's slot is namespaced (see
                # ReplyQueue's own docstring), and releasing the plain
                # node id here would be a silent no-op for it while
                # leaking the actual knock-namespaced key forever.
                self._queue.release(job.queue_key())
            self._metrics.queue_depth.set(self._queue.depth())

    def _finish_expired(self, job: ReplyJob, now: int) -> None:
        try:
            self._tx_request_log.set_state(
                job.request_sender, job.request_packet_id, "expired", now
            )
        except ValueError:
            log.warning("expired reply's request row already terminal")
            return
        self._metrics.replies_total.labels("expired").inc()
        self._log_reply_outcome(job.request_sender, "expired", now)
        # Fix round 1 (task 3B-3, item 2): a job that expired in the queue
        # never reached the radio at all -- a DEFINITE pre-transmission
        # failure, and never a blocked-sender outcome (this path is TTL-only).
        # Fix round 2: guarded in its own try/except -- a caller-supplied
        # callback (e.g. Daemon._enqueue_knock_release) runs on THIS
        # thread (the TX worker thread) and must never be trusted to be
        # exception-safe on our behalf. `pump()`'s own per-job try/except
        # around this whole method is a second, redundant backstop; this
        # one keeps `_finish_expired` itself exception-safe regardless of
        # who calls it.
        if job.release_on_pretransmission_failure is not None:
            try:
                job.release_on_pretransmission_failure("expired")
            except Exception:
                log.exception("release_on_pretransmission_failure raised for an expired reply")
                self._metrics.reply_callback_errors_total.inc()

    def _log_reply_outcome(self, recipient: str, outcome: str, now: int) -> None:
        """--debug only (see exchange_log.py): a no-op unless the operator
        started `run --debug`. Traces the terminal ack/failed/expired
        outcome of a reply already handed to the radio (see
        `_guarded_send`'s own trace line at the moment of the send)."""
        exchange_log.log_line(
            f"{local_iso(now, self._cfg.timezone)} "
            f"reply outcome recipient={exchange_log.node_label(recipient)} outcome={outcome}"
        )

    def _send_one(self, job: ReplyJob, now: int) -> bool:
        """Returns True if the job must be requeued (global cooldown only).

        `_guarded_send` is expected to raise only `SendRefused`; anything
        else is an internal error (e.g. a database failure mid-guard). It
        is still caught here so a single bad reply can never take down the
        TX worker loop -- the request is marked failed and the loop moves
        on, never retried.
        """
        try:
            return self._guarded_send(job, now)
        except SendRefused as exc:
            log.info("reply refused reason=%s", exc.reason.value)
            self._mark_dropped(job, now, exc.reason.value)
            return False
        except Exception as exc:
            log.error("unexpected error in send-time guard: %s", type(exc).__name__)
            self._mark_internal_error(job, now)
            return False

    def _mark_dropped(self, job: ReplyJob, now: int, reason: str) -> None:
        try:
            self._tx_request_log.set_state(
                job.request_sender, job.request_packet_id, "dropped", now, reason=reason
            )
        except ValueError:
            log.warning("refused reply's request row already terminal")
            return
        # Fix round 1 (task 3B-3, item 2): every reason `_guarded_send` can
        # raise SendRefused for BEFORE committing 'sending' (i.e. before any
        # radio call is attempted) is a DEFINITE pre-transmission failure --
        # EXCEPT a blocked sender, which must never be released (a blocked
        # node is not owed a retry). `Reject.BLOCKED` is the only reason
        # this guard withholds; every other `_mark_dropped` reason (queue
        # refusal, not-reply-ready, stale, shape checks) is safe to release.
        # Fix round 2: guarded the same way as `_finish_expired` -- this
        # runs on the TX worker thread (via `_send_one`), and a raising
        # callback must never escape `_mark_dropped` itself. `_send_one`'s
        # caller, `pump()`, already has a `finally` that releases/requeues
        # `job` regardless (so this path was never the wedge risk item 2's
        # callback introduced), but keeping this method exception-safe on
        # its own terms is worth the two extra lines.
        if job.release_on_pretransmission_failure is not None and reason != Reject.BLOCKED.value:
            try:
                job.release_on_pretransmission_failure(reason)
            except Exception:
                log.exception("release_on_pretransmission_failure raised for a dropped reply")
                self._metrics.reply_callback_errors_total.inc()

    def _mark_internal_error(self, job: ReplyJob, now: int) -> None:
        try:
            self._tx_request_log.set_state(
                job.request_sender,
                job.request_packet_id,
                "failed",
                now,
                reason="internal_error",
            )
        except ValueError:
            log.warning("internal error on an already-terminal request row")
            return
        except Exception as exc:  # this recovery path itself must never crash pump()
            log.error("failed to record internal_error outcome: %s", type(exc).__name__)
            return
        self._metrics.replies_total.labels("failed").inc()

    def _guarded_send(self, job: ReplyJob, now: int) -> bool:
        # 1. service readiness + radio identity, re-read NOW (not at enqueue time)
        st = self._transport.status()
        if not st.connected or not st.reply_ready:
            raise SendRefused(Reject.NOT_REPLY_READY)
        if st.actual_node_id != self._expected_node_id:
            raise SendRefused(Reject.NOT_REPLY_READY)

        # 2. originating request must exist and be in state 'queued'
        row_state = self._tx_request_log.get_state(job.request_sender, job.request_packet_id)
        if row_state != "queued":
            raise SendRefused(Reject.DUPLICATE)

        # 3. recipient must be a concrete unicast node id, never broadcast.
        # Broadcast checks run BEFORE the format check so a broadcast
        # recipient is always reported as BROADCAST, never NOT_FOR_US.
        if job.request_sender in ("^all", "^local"):
            raise SendRefused(Reject.BROADCAST)
        if not NODE_ID_RE.match(job.request_sender):
            raise SendRefused(Reject.NOT_FOR_US)
        if int(job.request_sender[1:], 16) == 0xFFFFFFFF:
            raise SendRefused(Reject.BROADCAST)

        # 4. blocked sender
        if self._is_blocked(job.request_sender):
            raise SendRefused(Reject.BLOCKED)

        # 5. queue age / TTL, using the monotonic deadline captured at receive time
        if self._clock() > job.deadline_monotonic:
            raise SendRefused(Reject.STALE)

        # 6. global tx cooldown -- soft defer, not a refusal: leave queued, try later
        if self._limiter.check_global_tx() is not None:
            return True

        # 7. payload size on the COMPLETE formatted response. Handlers already
        # truncate via truncate_utf8() when formatting; this is the last-line
        # guard and should normally never fire.
        body = job.body
        if len(body.encode("utf-8")) > self._transport.max_text_bytes:
            raise SendRefused(Reject.PAYLOAD_TOO_LARGE)

        # 8. commit 'sending' BEFORE the radio call. The global cooldown is
        # committed here too, immediately after -- on every actual attempt,
        # whether it ends up returning an id, returning None, or raising --
        # never only on success: otherwise a failing radio bursts through
        # the queue at up to one send per pump() cycle instead of being
        # spaced by global_tx_cooldown_seconds. A successful, unacknowledged
        # submission may schedule bounded delayed retries below; a send that
        # raises or returns no tx ref remains terminal and is never retried.
        self._tx_request_log.set_state(job.request_sender, job.request_packet_id, "sending", now)
        self._limiter.commit_global_tx()
        # --debug only (see exchange_log.py): a no-op unless the operator
        # started `run --debug`. One trace line per reply handed to the
        # radio, right before the actual send_text call; the outcome
        # (ack/failed/expired) is traced separately once it resolves -- see
        # `_log_reply_outcome`.
        ts = local_iso(now, self._cfg.timezone)
        recipient_label = exchange_log.node_label(job.request_sender)
        if job.reply_channel_idx is not None:
            exchange_log.log_line(
                f"{ts} channel reply idx={job.reply_channel_idx} "
                f"for={recipient_label} text={body!r}"
            )
            try:
                self._transport.send_channel(job.reply_channel_idx, body)
            except Exception as exc:
                # Keep message bodies and arbitrary exception payloads out of
                # normal logs, but retain useful transport/firmware diagnostics.
                payload = getattr(getattr(exc, "event", None), "payload", {})
                code = payload.get("error_code") if isinstance(payload, dict) else None
                code = code if isinstance(code, int) else "unknown"
                log.warning(
                    "channel send failed idx=%s exception=%s firmware_code=%s",
                    job.reply_channel_idx, type(exc).__name__, code,
                )
                exchange_log.log_line(
                    f"{ts} channel send failed idx={job.reply_channel_idx} "
                    f"for={recipient_label} exception={type(exc).__name__} firmware_code={code}"
                )
                self._tx_request_log.set_state(
                    job.request_sender,
                    job.request_packet_id,
                    "failed",
                    now,
                    reason="channel_send_exception",
                )
                self._metrics.replies_total.labels("failed").inc()
                self._log_reply_outcome(job.request_sender, "failed", now)
                # Nothing left the radio: let the owner (weather ledger) retry.
                # Guarded like `_mark_dropped`: a raising callback must not
                # escape the TX worker.
                if job.release_on_pretransmission_failure is not None:
                    try:
                        job.release_on_pretransmission_failure("channel_send_exception")
                    except Exception:
                        log.exception(
                            "release_on_pretransmission_failure raised for a failed channel send"
                        )
                        self._metrics.reply_callback_errors_total.inc()
                return False
            # MeshCore returns local OK/ERROR for a channel submission but no
            # remote ACK. Record it as submitted and do not application-retry:
            # repeating a public reply would add channel noise without any
            # delivery signal that could stop the retries.
            self._tx_request_log.set_state(
                job.request_sender, job.request_packet_id, "submitted", now
            )
            self._metrics.replies_total.labels("submitted").inc()
            return False
        exchange_log.log_line(f"{ts} reply to={recipient_label} text={body!r}")
        try:
            tx_ref = self._transport.send_dm(job.request_sender, body)
        except SendNotReady:
            log.info("radio not ready at send time; marking failed")
            self._tx_request_log.set_state(
                job.request_sender,
                job.request_packet_id,
                "failed",
                now,
                reason="not_ready_at_send",
            )
            self._metrics.replies_total.labels("failed").inc()
            self._log_reply_outcome(job.request_sender, "failed", now)
            return False
        except Exception:
            log.info("send_text raised; marking failed")
            self._tx_request_log.set_state(
                job.request_sender, job.request_packet_id, "failed", now, reason="send_exception"
            )
            self._metrics.replies_total.labels("failed").inc()
            self._log_reply_outcome(job.request_sender, "failed", now)
            return False
        if not tx_ref:
            self._tx_request_log.set_state(
                job.request_sender, job.request_packet_id, "failed", now, reason="no_tx_id"
            )
            self._metrics.replies_total.labels("failed").inc()
            self._log_reply_outcome(job.request_sender, "failed", now)
            return False

        # `tx_ref` is a `str` (an opaque, transport-defined correlation
        # handle) but `processed_requests.tx_packet_id` is an INTEGER
        # column. `MeshtasticTransport.send_dm`'s `tx_ref` is always
        # `str(<the real firmware packet id>)`, so `int(tx_ref)` round-trips
        # losslessly for it and the column/schema and every existing row
        # stay exactly as they were -- Meshtastic behavior here is
        # byte-for-byte unchanged.
        #
        # `MeshCoreTransport.send_dm`'s `tx_ref` is hex (`expected_ack.
        # hex()`), which `int(tx_ref)` raises on (base-10 parse of a string
        # that generally contains a-f). DECISION (task 3B-2 item D): widen
        # the INTEGER column, or keep tx_ref -> request correlation in
        # memory. Chosen: IN MEMORY (`_pending_by_tx_ref`, below), not a
        # schema change -- `tx_packet_id` stays INTEGER, unmigrated, every
        # existing row untouched. This also fits an existing invariant
        # rather than fighting it: `RequestLog.resolve_uncertain_on_startup`
        # already discards any in-flight send/ack correlation across a
        # restart ("no in-memory work survives a restart" -- see
        # `Daemon._db_worker_run`'s own comment), so a purely in-memory
        # MeshCore correlation loses nothing a restart wasn't already going
        # to throw away. No information is lost while the process runs: the
        # full tx_ref is kept in `_pending_by_tx_ref` until popped by a
        # matching `on_delivery`, or swept once `ack_timeout_seconds` old
        # (a MeshCore send that never gets an ACK ages to 'unknown' via
        # Retention.age_stale_submitted either way -- see that pruning's
        # own comment).
        try:
            tx_packet_id: int | None = int(tx_ref)
        except ValueError:
            tx_packet_id = None
        self._tx_request_log.set_state(
            job.request_sender,
            job.request_packet_id,
            "submitted",
            now,
            tx_packet_id=tx_packet_id,
        )
        self._metrics.replies_total.labels("submitted").inc()
        self._remember_pending_tx_ref(tx_ref, job.request_sender, job.request_packet_id)
        if not job.is_cooldown_notice:
            if self._mobile_policy_for(job.request_sender):
                if self._cfg.mobile_dm_max_retries == 1:
                    self._schedule_mobile_recovery(job, tx_ref)
            else:
                self._schedule_retry(job)
        return False

    def _remember_pending_tx_ref(
        self, tx_ref: str, sender_node_id: str, packet_id: int, attempt: int = 0
    ) -> None:
        """Records a tx_ref's correlation in memory for every send attempt. Sweeps
        anything older than `ack_timeout_seconds` first, bounding this
        dict's size to roughly one `ack_timeout_seconds` window's worth of
        in-flight MeshCore sends regardless of how many are ever ACKed."""
        now = self._clock()
        with self._tx_ref_lock:
            cutoff = now - self._cfg.ack_timeout_seconds
            stale = [ref for ref, (_, _, at, _) in self._pending_by_tx_ref.items() if at < cutoff]
            for ref in stale:
                del self._pending_by_tx_ref[ref]
            self._pending_by_tx_ref[tx_ref] = (sender_node_id, packet_id, now, attempt)

    def _pop_pending_tx_ref(self, tx_ref: str) -> tuple[str, int, bool, int] | None:
        with self._tx_ref_lock:
            entry = self._pending_by_tx_ref.pop(tx_ref, None)
            if entry is None:
                return None
            sender_node_id, packet_id, _, attempt = entry
            has_other_attempt = any(
                sender == sender_node_id and pid == packet_id
                for sender, pid, _at, _attempt in self._pending_by_tx_ref.values()
            )
        return sender_node_id, packet_id, has_other_attempt, attempt

    def _forget_request_tx_refs(self, sender_node_id: str, packet_id: int) -> None:
        with self._tx_ref_lock:
            stale = [
                ref
                for ref, (sender, pid, _at, _attempt) in self._pending_by_tx_ref.items()
                if sender == sender_node_id and pid == packet_id
            ]
            for ref in stale:
                del self._pending_by_tx_ref[ref]

    def _schedule_retry(
        self,
        job: ReplyJob,
        *,
        attempt: int = 1,
        expires_monotonic: float | None = None,
    ) -> None:
        if attempt > self._cfg.application_retries:
            return
        now = self._clock()
        if expires_monotonic is None:
            expires_monotonic = now + self._cfg.ack_timeout_seconds
        delay = self._cfg.application_retry_delay_seconds
        if now + delay >= expires_monotonic:
            return
        entry = _PendingRetry(
            job=job,
            attempt=attempt,
            due_monotonic=now + delay,
            expires_monotonic=expires_monotonic,
        )
        with self._retry_lock:
            self._pending_retries[(job.request_sender, job.request_packet_id)] = entry

    def _schedule_mobile_recovery(self, job: ReplyJob, tx_ref: str) -> None:
        configured = float(self._cfg.application_retry_delay_seconds)
        delay = self._transport.dm_ack_wait_seconds(tx_ref, configured)  # type: ignore[attr-defined]
        now = self._clock()
        expires = now + self._cfg.ack_timeout_seconds
        if now + delay >= expires:
            return
        with self._retry_lock:
            self._pending_retries[(job.request_sender, job.request_packet_id)] = _PendingRetry(
                job=job, attempt=1, due_monotonic=now + delay, expires_monotonic=expires
            )

    def _trace_retry_cancel(self, entry: _PendingRetry, reason: str) -> None:
        ts = local_iso(int(time.time()), self._cfg.timezone)
        exchange_log.log_line(
            f"{ts} reply retry cancelled "
            f"to={exchange_log.node_label(entry.job.request_sender)} "
            f"next_attempt={entry.attempt} reason={reason}"
        )

    def _cancel_retry(self, sender_node_id: str, packet_id: int, reason: str) -> None:
        with self._retry_lock:
            removed = self._pending_retries.pop((sender_node_id, packet_id), None)
        if removed is not None:
            self._trace_retry_cancel(removed, reason)
            log.debug("reply retry cancelled recipient=%s reason=%s", sender_node_id, reason)

    def _cancel_retries_for_recipient(self, recipient: str, reason: str) -> None:
        with self._retry_lock:
            removed = [
                self._pending_retries.pop(key)
                for key in list(self._pending_retries)
                if key[0] == recipient
            ]
        for entry in removed:
            self._trace_retry_cancel(entry, reason)
            if reason == "newer_reply" and self._mobile_recovery_enabled_for(recipient):
                self._record_mobile_stage("suppressed_newer_reply", recipient)
        if removed:
            log.debug("reply retry cancelled recipient=%s reason=%s", recipient, reason)

    def _due_retry(self) -> _PendingRetry | None:
        now = self._clock()
        with self._retry_lock:
            expired = [
                key for key, entry in self._pending_retries.items() if now > entry.expires_monotonic
            ]
            for key in expired:
                entry = self._pending_retries[key]
                if (
                    self._mobile_recovery_enabled_for(entry.job.request_sender)
                    and entry.attempt == 2
                ):
                    self._record_mobile_stage("fallback_timeout", entry.job.request_sender)
                del self._pending_retries[key]
            due = [entry for entry in self._pending_retries.values() if now >= entry.due_monotonic]
        return min(due, key=lambda entry: entry.due_monotonic, default=None)

    def _consume_retry(self, entry: _PendingRetry) -> bool:
        key = (entry.job.request_sender, entry.job.request_packet_id)
        with self._retry_lock:
            if self._pending_retries.get(key) is not entry:
                return False
            del self._pending_retries[key]
            return True

    def _pump_retry(self, now: int) -> None:
        """Attempts at most one due retry and schedules the next backoff step."""
        entry = self._due_retry()
        if entry is None:
            return
        job = entry.job
        mobile = self._mobile_recovery_enabled_for(job.request_sender)
        if mobile and entry.attempt == 2:
            if self._consume_retry(entry):
                self._record_mobile_stage("fallback_timeout", job.request_sender)
            return
        if self._tx_request_log.get_state(job.request_sender, job.request_packet_id) != "submitted":
            self._cancel_retry(job.request_sender, job.request_packet_id, "request_settled")
            return
        st = self._transport.status()
        if not st.connected or not st.reply_ready or st.actual_node_id != self._expected_node_id:
            return
        if self._is_blocked(job.request_sender):
            self._cancel_retry(job.request_sender, job.request_packet_id, "blocked")
            return
        if len(job.body.encode("utf-8")) > self._transport.max_text_bytes:
            self._cancel_retry(job.request_sender, job.request_packet_id, "payload_too_large")
            return
        if self._limiter.check_global_tx() is not None:
            return
        if mobile:
            self._record_mobile_stage("normal_timeout", job.request_sender)
            current = self._clock()
            cutoff = current - 3600.0
            cooldown_cutoff = current - self._cfg.mobile_dm_recovery_cooldown_seconds
            self._mobile_recovery_last = {
                recipient: at
                for recipient, at in self._mobile_recovery_last.items()
                if at > cooldown_cutoff
            }
            while self._mobile_recovery_global and self._mobile_recovery_global[0] <= cutoff:
                self._mobile_recovery_global.popleft()
            last = self._mobile_recovery_last.get(job.request_sender)
            if last is not None and current - last < self._cfg.mobile_dm_recovery_cooldown_seconds:
                self._consume_retry(entry)
                self._record_mobile_stage("suppressed_cooldown", job.request_sender)
                return
            if (
                self._mobile_recovery_global
                and current - self._mobile_recovery_global[-1]
                < self._cfg.mobile_dm_recovery_min_interval_seconds
            ) or len(
                self._mobile_recovery_global
            ) >= self._cfg.mobile_dm_global_recoveries_per_hour:
                self._consume_retry(entry)
                self._record_mobile_stage("suppressed_global", job.request_sender)
                return
        if not mobile and not self._consume_retry(entry):
            return

        ts = local_iso(now, self._cfg.timezone)
        route_note = ""
        if mobile:
            try:
                if self._transport.reset_dm_path(job.request_sender):
                    route_note = " route=flood"
                else:
                    self._consume_retry(entry)
                    self._record_mobile_stage("suppressed_path_reset", job.request_sender)
                    return
            except Exception as exc:
                self._consume_retry(entry)
                route_note = " route_reset=failed"
                log.info("reply retry path reset failed type=%s", type(exc).__name__)
                self._record_mobile_stage("suppressed_path_reset", job.request_sender)
                return
            # reset_path may wait behind another serialized MeshCore request.
            # A late ACK or newer reply can settle/cancel this recovery while
            # that happens; recheck before putting the fallback on air.
            if (
                self._tx_request_log.get_state(job.request_sender, job.request_packet_id)
                != "submitted"
            ):
                return
            # Keep the retry entry cancellable throughout reset_path. An ACK
            # or newer reply removes it; only the winner may put RF on air.
            if not self._consume_retry(entry):
                return
            current = self._clock()
            self._mobile_recovery_last[job.request_sender] = current
            self._mobile_recovery_global.append(current)
        elif entry.attempt == 2:
            try:
                if self._transport.reset_dm_path(job.request_sender):
                    route_note = " route=flood"
            except Exception as exc:
                route_note = " route_reset=failed"
                log.info("reply retry path reset failed type=%s", type(exc).__name__)
        self._limiter.commit_global_tx()
        exchange_log.log_line(
            f"{ts} reply retry to={exchange_log.node_label(job.request_sender)} "
            f"attempt={entry.attempt}{route_note} text={job.body!r}"
        )
        with self._delivery_lock:
            self._send_in_progress = True
        try:
            try:
                tx_ref = self._transport.send_dm(
                    job.request_sender, job.body, attempt=entry.attempt
                )
            except Exception as exc:
                # The original attempt may still receive a late ACK, so a retry
                # transport error must not overwrite the logical request state.
                log.info("reply retry send failed type=%s", type(exc).__name__)
                if mobile:
                    self._record_mobile_stage("fallback_failed", job.request_sender)
                else:
                    self._schedule_retry(
                        job,
                        attempt=entry.attempt + 1,
                        expires_monotonic=entry.expires_monotonic,
                    )
                return
        finally:
            with self._delivery_lock:
                self._send_in_progress = False
        if not tx_ref:
            log.info("reply retry produced no tx ref")
            if mobile:
                self._record_mobile_stage("fallback_failed", job.request_sender)
            else:
                self._schedule_retry(
                    job,
                    attempt=entry.attempt + 1,
                    expires_monotonic=entry.expires_monotonic,
                )
            return
        try:
            tx_packet_id: int | None = int(tx_ref)
        except ValueError:
            tx_packet_id = None
        try:
            self._tx_request_log.set_state(
                job.request_sender,
                job.request_packet_id,
                "submitted",
                now,
                tx_packet_id=tx_packet_id,
            )
        except ValueError:
            # A late ACK won the race with this retry. The packet may already
            # be on air, but the terminal ACK must remain authoritative.
            log.debug("reply retry raced with a settled request")
            return
        self._remember_pending_tx_ref(
            tx_ref, job.request_sender, job.request_packet_id, attempt=entry.attempt
        )
        self._metrics.replies_total.labels("submitted").inc()
        if mobile:
            self._record_mobile_stage("fallback_sent", job.request_sender)
            delay = self._transport.dm_ack_wait_seconds(  # type: ignore[attr-defined]
                tx_ref, self._cfg.application_retry_delay_seconds
            )
            with self._retry_lock:
                self._pending_retries[(job.request_sender, job.request_packet_id)] = _PendingRetry(
                    job=job,
                    attempt=2,
                    due_monotonic=self._clock() + delay,
                    expires_monotonic=entry.expires_monotonic,
                )
        else:
            self._schedule_retry(
                job,
                attempt=entry.attempt + 1,
                expires_monotonic=entry.expires_monotonic,
            )

    def drain_deliveries(self) -> None:
        """DB-worker-only: resolve ACKs buffered during a radio submission."""
        with self._delivery_lock:
            if self._send_in_progress:
                return
            pending = list(self._early_deliveries)
            self._early_deliveries.clear()
        for delivery in pending:
            self._resolve_delivery(delivery)

    def on_delivery(self, delivery: Delivery) -> None:
        with self._delivery_lock:
            if self._send_in_progress:
                self._early_deliveries.append(delivery)
                return
        self.drain_deliveries()
        self._resolve_delivery(delivery)

    def _resolve_delivery(self, delivery: Delivery) -> None:
        """Correlates a transport-reported delivery outcome to its outbound
        request and records acked/failed. Never records `unknown` here --
        that state is reached only by the ack_timeout_seconds aging in the
        maintenance tick (and at startup), never by a delivery.

        Called from the daemon's DB worker thread (via observation
        processing, through the registered `Transport.on_delivery`
        callback) -- uses `request_log`, not `tx_request_log`.

        Replaces the former `note_routing_packet`: matching a raw ack/fail
        frame to a `tx_ref` and deciding what it MEANS (including the
        Meshtastic-only implicit-self-ack rule, which now never reaches
        here at all -- see `MeshtasticTransport.note_routing_packet`) is the
        transport's job. This method only ever owns the DB-side state
        transition for a delivery the transport has already decided is a
        real outcome.
        """
        # Prefer the live map for both transports: unlike the single DB
        # column it can retain the original and retry refs simultaneously.
        # The numeric DB lookup remains as restart/backward compatibility.
        pending = self._pop_pending_tx_ref(delivery.tx_ref)
        if pending is not None:
            sender_node_id, packet_id, has_other_attempt, delivered_attempt = pending
        else:
            try:
                tx_packet_id: int | None = int(delivery.tx_ref)
            except (TypeError, ValueError):
                tx_packet_id = None
            if tx_packet_id is None:
                log.warning("delivery with an unrecognized tx_ref; ignored")
                return
            row = self._request_log.find_by_tx_packet_id(tx_packet_id)
            if row is None:
                return
            sender_node_id, packet_id = row["sender_node_id"], row["packet_id"]
            has_other_attempt = False
            delivered_attempt = 0

        outcome = "acked" if delivery.outcome is DeliveryOutcome.ACKED else "failed"
        if outcome == "failed" and has_other_attempt:
            log.debug("one reply attempt failed; another is still awaiting delivery")
            return
        self._cancel_retry(sender_node_id, packet_id, outcome)
        self._forget_request_tx_refs(sender_node_id, packet_id)
        now = int(time.time())
        try:
            self._request_log.set_state(
                sender_node_id,
                packet_id,
                outcome,
                now,
                reason=delivery.reason,
            )
        except ValueError:
            log.warning("delivery for an already-terminal request; ignored")
            return
        if outcome == "acked" and self._mobile_policy_for(sender_node_id):
            stage = "fallback_success" if delivered_attempt > 0 else "normal_success"
            self._record_mobile_stage(stage, sender_node_id)
        self._metrics.replies_total.labels(outcome).inc()
        self._log_reply_outcome(sender_node_id, outcome, now)
