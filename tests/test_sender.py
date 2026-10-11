import threading
import time
from dataclasses import replace

import pytest

from meshgarden.commands import Command, Reject, RequestLog, check_eligibility
from meshgarden.config import (
    BackupConfig,
    RetentionConfig,
    ServiceConfig,
    node_id_from_num,
)
from meshgarden.database import connect, migrate
from meshgarden.metrics import Metrics
from meshgarden.models import IncomingPacket, ReplyJob
from meshgarden.radio import RadioStatus
from meshgarden.retention import Retention
from meshgarden.sender import RateLimiter, ReplyQueue, Sender, truncate_utf8
from meshgarden.transport import Delivery, DeliveryOutcome
from tests.fake_meshcore import FakeMeshCoreTransport
from tests.fakes import FakeTransport

CFG = ServiceConfig()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, d):
        self.t += d


def job(sender="!aabbccdd", pid=1, body="hi", deadline=1060.0):
    return ReplyJob(
        request_sender=sender,
        request_packet_id=pid,
        request_received_at=1000,
        deadline_monotonic=deadline,
        body=body,
    )


# --- RateLimiter -----------------------------------------------------------


def test_per_node_command_cooldown_is_30s():
    c = Clock()
    rl = RateLimiter(CFG, clock=c)
    assert rl.check_command("!aabbccdd") is None
    rl.commit_command("!aabbccdd")
    assert rl.check_command("!aabbccdd") is Reject.RATE_LIMITED
    c.advance(29.9)
    assert rl.check_command("!aabbccdd") is Reject.RATE_LIMITED
    c.advance(0.2)
    assert rl.check_command("!aabbccdd") is None


def test_per_node_cooldown_is_per_node():
    c = Clock()
    rl = RateLimiter(CFG, clock=c)
    rl.commit_command("!aabbccdd")
    assert rl.check_command("!11112222") is None


def test_global_tx_cooldown_is_15s():
    c = Clock()
    rl = RateLimiter(CFG, clock=c)
    assert rl.check_global_tx() is None
    rl.commit_global_tx()
    assert rl.check_global_tx() is Reject.RATE_LIMITED
    c.advance(15.1)
    assert rl.check_global_tx() is None


def test_sign_cooldown_is_3600s():
    c = Clock()
    rl = RateLimiter(CFG, clock=c)
    rl.commit_sign("!aabbccdd")
    assert rl.check_sign("!aabbccdd") is Reject.SIGN_COOLDOWN
    c.advance(3599)
    assert rl.check_sign("!aabbccdd") is Reject.SIGN_COOLDOWN
    c.advance(2)
    assert rl.check_sign("!aabbccdd") is None


def test_sign_cooldown_remaining_counts_down_and_rounds_up():
    c = Clock()
    rl = RateLimiter(CFG, clock=c)
    rl.commit_sign("!aabbccdd")
    assert rl.sign_cooldown_remaining("!aabbccdd") == 3600
    c.advance(1)  # 3599 seconds exactly left -- still the full ceil'd count
    assert rl.sign_cooldown_remaining("!aabbccdd") == 3599
    c.advance(3599)
    assert rl.sign_cooldown_remaining("!aabbccdd") == 0  # cooldown cleared


def test_sign_cooldown_remaining_is_zero_for_a_sender_never_signed():
    rl = RateLimiter(CFG, clock=Clock())
    assert rl.sign_cooldown_remaining("!aabbccdd") == 0


def test_reset_sign_clears_the_cooldown_immediately():
    c = Clock()
    rl = RateLimiter(CFG, clock=c)
    rl.commit_sign("!aabbccdd")
    assert rl.check_sign("!aabbccdd") is Reject.SIGN_COOLDOWN
    rl.reset_sign("!aabbccdd")
    assert rl.check_sign("!aabbccdd") is None
    assert rl.sign_cooldown_remaining("!aabbccdd") == 0


def test_reset_sign_on_a_sender_with_no_cooldown_is_a_harmless_no_op():
    rl = RateLimiter(CFG, clock=Clock())
    rl.reset_sign("!aabbccdd")  # never signed -- nothing to clear, must not raise
    assert rl.check_sign("!aabbccdd") is None


# --- ReplyQueue --------------------------------------------------------------


def test_queue_holds_at_most_four():
    q = ReplyQueue(CFG)
    for i in range(4):
        assert q.offer(job(sender=f"!0000000{i}", pid=i)) is None
    assert q.offer(job(sender="!00000009", pid=9)) is Reject.QUEUE_FULL
    assert q.depth() == 4


def test_at_most_one_pending_reply_per_sender():
    q = ReplyQueue(CFG)
    assert q.offer(job(pid=1)) is None
    assert q.offer(job(pid=2)) is Reject.RATE_LIMITED
    assert q.depth() == 1


def test_reserve_does_not_enqueue():
    q = ReplyQueue(CFG)
    assert q.reserve("!aabbccdd") is None
    assert q.depth() == 0


def test_replies_expire_after_60s_from_request_receipt():
    q = ReplyQueue(CFG)
    q.offer(job(deadline=1000.0 + 60.0))
    ready, expired = q.pop_ready(now_monotonic=1000.0 + 61.0)
    assert ready is None and len(expired) == 1
    # The expired job resolved right there in pop_ready, releasing its
    # sender's slot automatically -- a fresh job for the same sender may
    # now be queued (spec-consistent sequence: the second offer() comes
    # AFTER the pop_ready that resolves the first job, not concurrently
    # with an unresolved one).
    assert q.offer(job(pid=2, deadline=1000.0 + 120.0)) is None
    ready, expired = q.pop_ready(now_monotonic=1000.0 + 61.0)
    assert ready is not None and expired == []


def test_pending_slot_is_held_across_pop_ready_until_release():
    """'At most one pending reply per sender' includes a reply that has
    been popped but not yet resolved -- not just one still sitting in the
    deque."""
    q = ReplyQueue(CFG)
    q.offer(job(deadline=1000.0 + 60.0))
    ready, expired = q.pop_ready(now_monotonic=1000.0 + 10.0)
    assert ready is not None and expired == []
    # The popped job hasn't resolved yet: a second job for the same sender
    # must still be refused, and the queue must not grow.
    assert q.offer(job(pid=2, deadline=1000.0 + 60.0)) is Reject.RATE_LIMITED
    assert q.depth() == 0
    q.release(ready.request_sender)
    # Only once released does the slot free up.
    assert q.offer(job(pid=3, deadline=1000.0 + 60.0)) is None
    assert q.depth() == 1


def test_reply_queue_offer_and_pop_ready_are_thread_safe():
    """offer() (DB worker thread) and pop_ready() (TX worker thread) run
    concurrently in the daemon; both are check-then-act and must not
    corrupt the shared deque/set or ever over-admit past the cap."""
    q = ReplyQueue(CFG)
    errors = []
    stop = threading.Event()

    def offer_loop():
        i = 0
        while not stop.is_set():
            try:
                q.offer(job(sender=f"!{i % 997:08x}", pid=i, deadline=1000.0 + 60.0))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
            i += 1

    def pop_loop():
        for _ in range(3000):
            try:
                job_out, expired = q.pop_ready(now_monotonic=1000.0)
                if job_out is not None:
                    q.release(job_out.request_sender)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

    t = threading.Thread(target=offer_loop)
    t.start()
    pop_loop()
    stop.set()
    t.join(timeout=5)

    assert errors == []
    assert q.depth() <= CFG.max_pending_replies


# --- truncate_utf8 -----------------------------------------------------------


def test_truncate_never_splits_a_codepoint():
    s = "é" * 100  # 2 bytes each
    out = truncate_utf8(s, 61)
    assert len(out.encode("utf-8")) <= 61
    out.encode("utf-8").decode("utf-8")  # must not raise
    assert out == "é" * 30


def test_truncate_handles_astral_plane():
    s = "🌲" * 20  # 4 bytes each
    out = truncate_utf8(s, 10)
    assert out == "🌲🌲"
    assert len(out.encode("utf-8")) == 8


def test_truncate_is_identity_when_short_enough():
    assert truncate_utf8("hello", 180) == "hello"


def test_truncate_is_idempotent():
    s = "🌲" * 20
    once = truncate_utf8(s, 10)
    assert truncate_utf8(once, 10) == once


def test_truncate_with_a_negative_max_bytes_returns_empty():
    """Regression: data[:-1] on a negative max_bytes returned text minus
    |n| bytes instead of the empty string, mirroring commands._truncate_utf8's
    own `if max_bytes <= 0: return ""` guard."""
    assert truncate_utf8("hello", -1) == ""


# --- Sender integration harness ----------------------------------------------

SERVICE_NODE_ID = "!11223344"
SERVICE_NODE_NUM = 0x11223344


class Harness:
    def __init__(self, tmp_path, is_blocked=None, cfg=CFG):
        self.clock = Clock()
        self.conn = connect(str(tmp_path / "sender.db"))
        migrate(self.conn)
        self.request_log = RequestLog(self.conn, dedup_hours=24)
        self.metrics = Metrics()
        self.transport = FakeTransport(SERVICE_NODE_NUM, SERVICE_NODE_ID, service_cfg=cfg)
        self.radio = self.transport.radio
        self.iface = self.transport.iface
        self.limiter = RateLimiter(cfg, clock=self.clock)
        self.queue = ReplyQueue(cfg)
        self.sender = Sender(
            transport=self.transport,
            cfg=cfg,
            limiter=self.limiter,
            queue=self.queue,
            request_log=self.request_log,
            metrics=self.metrics,
            expected_node_id=SERVICE_NODE_ID,
            is_blocked=is_blocked or (lambda sender: False),
            clock=self.clock,
        )
        # Wires the ack path exactly as main.py's Daemon.start() does:
        # MeshtasticTransport.note_routing_packet -> on_delivery -> Sender.
        self.transport.on_delivery(self.sender.on_delivery)

    def stop(self):
        self.transport.stop()

    def make_queued_job(
        self, sender="!aabbccdd", pid=1, body="hi", ttl=60.0, on_pretransmission_failure=None
    ):
        now_wall = int(time.time())
        self.request_log.claim(sender, pid, now_wall, Command.MENU)
        self.request_log.set_state(sender, pid, "queued", now_wall)
        return ReplyJob(
            request_sender=sender,
            request_packet_id=pid,
            request_received_at=now_wall,
            deadline_monotonic=self.clock() + ttl,
            body=body,
            release_on_pretransmission_failure=on_pretransmission_failure,
        )


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    h.stop()


# --- commit-before-send / outcomes -------------------------------------------


def test_commit_before_send_state_sequence(harness):
    j = harness.make_queued_job()
    assert harness.sender.enqueue(j) is None
    observed_state_at_send = []
    original_send_text = harness.radio.send_text

    def spy(*, text, destination_id):
        observed_state_at_send.append(
            harness.request_log.get_state(j.request_sender, j.request_packet_id)
        )
        return original_send_text(text=text, destination_id=destination_id)

    harness.radio.send_text = spy
    harness.sender.pump()

    assert observed_state_at_send == ["sending"]
    assert harness.request_log.get_state(j.request_sender, j.request_packet_id) == "submitted"
    row = harness.conn.execute(
        "SELECT tx_packet_id FROM processed_requests WHERE sender_node_id=? AND packet_id=?",
        (j.request_sender, j.request_packet_id),
    ).fetchone()
    assert row["tx_packet_id"] is not None
    sent = harness.iface.sent[0]
    assert sent["destinationId"] == j.request_sender
    assert sent["wantAck"] is True
    assert sent["wantResponse"] is False


def test_unacknowledged_reply_uses_three_equally_spaced_retries(harness):
    j = harness.make_queued_job(body="MENU SOCIAL")
    harness.sender.enqueue(j)
    harness.sender.pump()
    assert [sent["text"] for sent in harness.iface.sent] == ["MENU SOCIAL"]

    harness.clock.advance(CFG.application_retry_delay_seconds - 0.1)
    harness.sender.pump()
    assert len(harness.iface.sent) == 1

    harness.clock.advance(0.2)
    harness.sender.pump()
    assert [sent["text"] for sent in harness.iface.sent] == ["MENU SOCIAL", "MENU SOCIAL"]

    harness.clock.advance(CFG.application_retry_delay_seconds - 0.2)
    harness.sender.pump()
    assert len(harness.iface.sent) == 2

    harness.clock.advance(0.2)
    harness.sender.pump()
    assert len(harness.iface.sent) == 3

    harness.clock.advance(CFG.application_retry_delay_seconds)
    harness.sender.pump()
    assert len(harness.iface.sent) == 4

    harness.clock.advance(CFG.application_retry_delay_seconds * 8)
    harness.sender.pump()
    assert len(harness.iface.sent) == 4


def test_late_ack_cancels_the_scheduled_retry(harness):
    j = harness.make_queued_job(pid=20)
    harness.sender.enqueue(j)
    harness.sender.pump()
    tx_id = _tx_packet_id(harness, 20)
    ack = _routing_packet(
        from_num=0xAABBCCDD,
        request_id=tx_id,
        routing_error="NONE",
        packet_id=9020,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(ack)

    harness.clock.advance(CFG.application_retry_delay_seconds + 1)
    harness.sender.pump()
    assert len(harness.iface.sent) == 1
    assert harness.request_log.get_state(j.request_sender, 20) == "acked"


def test_newer_reply_to_same_node_supersedes_old_retry(harness):
    first = harness.make_queued_job(pid=21, body="old")
    harness.sender.enqueue(first)
    harness.sender.pump()

    harness.clock.advance(CFG.global_tx_cooldown_seconds + 1)
    second = harness.make_queued_job(pid=22, body="new")
    harness.sender.enqueue(second)
    harness.sender.pump()
    assert [sent["text"] for sent in harness.iface.sent] == ["old", "new"]

    harness.clock.advance(CFG.application_retry_delay_seconds + 1)
    harness.sender.pump()
    # Only the newest reply remains eligible for retries.
    assert [sent["text"] for sent in harness.iface.sent] == ["old", "new", "new"]


def test_explicit_delivery_failure_cancels_retry(harness):
    j = harness.make_queued_job(pid=23)
    harness.sender.enqueue(j)
    harness.sender.pump()
    tx_id = _tx_packet_id(harness, 23)
    failed = _routing_packet(
        from_num=0xAABBCCDD,
        request_id=tx_id,
        routing_error="MAX_RETRANSMIT",
        packet_id=9023,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(failed)

    harness.clock.advance(CFG.application_retry_delay_seconds + 1)
    harness.sender.pump()
    assert len(harness.iface.sent) == 1
    assert harness.request_log.get_state(j.request_sender, 23) == "failed"


def test_ack_for_retry_tx_ref_settles_logical_request(harness):
    j = harness.make_queued_job(pid=24)
    harness.sender.enqueue(j)
    harness.sender.pump()
    harness.clock.advance(CFG.application_retry_delay_seconds + 1)
    harness.sender.pump()
    assert len(harness.iface.sent) == 2

    retry_ack = _routing_packet(
        from_num=0xAABBCCDD,
        request_id=2,
        routing_error="NONE",
        packet_id=9024,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(retry_ack)
    assert harness.request_log.get_state(j.request_sender, 24) == "acked"


def test_one_failed_attempt_does_not_override_other_attempt_ack(harness):
    j = harness.make_queued_job(pid=25)
    harness.sender.enqueue(j)
    harness.sender.pump()
    harness.clock.advance(CFG.application_retry_delay_seconds + 1)
    harness.sender.pump()

    original_failed = _routing_packet(
        from_num=0xAABBCCDD,
        request_id=1,
        routing_error="MAX_RETRANSMIT",
        packet_id=9025,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(original_failed)
    assert harness.request_log.get_state(j.request_sender, 25) == "submitted"

    retry_acked = _routing_packet(
        from_num=0xAABBCCDD,
        request_id=2,
        routing_error="NONE",
        packet_id=9026,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(retry_acked)
    assert harness.request_log.get_state(j.request_sender, 25) == "acked"


def test_application_retry_can_be_disabled(tmp_path):
    h = Harness(tmp_path, cfg=replace(CFG, application_retries=0))
    try:
        j = h.make_queued_job(pid=26)
        h.sender.enqueue(j)
        h.sender.pump()
        h.clock.advance(CFG.application_retry_delay_seconds + 1)
        h.sender.pump()
        assert len(h.iface.sent) == 1
    finally:
        h.stop()


def test_second_offer_for_same_sender_is_refused_while_first_is_mid_send(harness):
    """The queue's per-sender slot must stay held for the whole send, not
    just while the job sits in the deque: a second reply for the same
    sender offered from inside send_text() (i.e. while the first job is
    popped and mid-flight) must be refused, and the queue must not grow."""
    j = harness.make_queued_job(sender="!aabbccdd", pid=1)
    harness.sender.enqueue(j)

    captured = {}
    original_send_text = harness.radio.send_text

    def spy(*, text, destination_id):
        second = harness.make_queued_job(sender="!aabbccdd", pid=2)
        captured["result"] = harness.sender.enqueue(second)
        return original_send_text(text=text, destination_id=destination_id)

    harness.radio.send_text = spy
    harness.sender.pump()

    assert captured["result"] is Reject.RATE_LIMITED
    assert harness.queue.depth() == 0  # the second job was refused, not queued


def test_failed_when_send_text_returns_none(harness):
    j = harness.make_queued_job()
    harness.sender.enqueue(j)
    harness.radio.send_text = lambda *, text, destination_id: None
    harness.sender.pump()
    assert harness.request_log.get_state(j.request_sender, j.request_packet_id) == "failed"
    assert harness.iface.sent == []


def test_failed_when_send_text_raises(harness):
    j = harness.make_queued_job()
    harness.sender.enqueue(j)

    def boom(*, text, destination_id):
        raise RuntimeError("radio exploded")

    harness.radio.send_text = boom
    harness.sender.pump()
    assert harness.request_log.get_state(j.request_sender, j.request_packet_id) == "failed"


def test_unexpected_exception_in_guard_does_not_crash_pump(harness):
    """An exception raised somewhere in the send-time guard other than the
    documented SendRefused path (e.g. a database failure on the 'sending'
    transition) must not raise UnboundLocalError or any other exception out
    of pump(): the request is marked failed/internal_error, the sender's
    queue slot is released, and nothing is sent."""
    j = harness.make_queued_job()
    harness.sender.enqueue(j)

    original_set_state = harness.request_log.set_state

    def flaky_set_state(sender_node_id, packet_id, state, now, **kwargs):
        if state == "sending":
            raise RuntimeError("db exploded")
        return original_set_state(sender_node_id, packet_id, state, now, **kwargs)

    harness.request_log.set_state = flaky_set_state

    harness.sender.pump()  # must not raise

    assert harness.iface.sent == []
    assert harness.request_log.get_state(j.request_sender, j.request_packet_id) == "failed"
    assert harness.queue.depth() == 0
    # the slot was released, so a fresh job for the same sender can be queued
    assert harness.queue.reserve(j.request_sender) is None


def test_identity_mismatch_at_send_time_refuses_and_preserves_global_cooldown(harness):
    j = harness.make_queued_job()
    harness.sender.enqueue(j)
    mismatched = RadioStatus(
        connected=True,
        reply_ready=True,
        actual_node_id="!99999999",
        expected_node_id=SERVICE_NODE_ID,
        mismatch_reason=None,
        connected_since=None,
        last_packet_at=None,
    )
    harness.radio.status = lambda: mismatched
    harness.sender.pump()
    assert harness.iface.sent == []
    assert harness.request_log.get_state(j.request_sender, j.request_packet_id) == "dropped"
    assert harness.limiter.check_global_tx() is None  # never consumed


def test_disconnected_at_send_time_refuses_and_preserves_global_cooldown(harness):
    j = harness.make_queued_job()
    harness.sender.enqueue(j)
    down = RadioStatus(
        connected=False,
        reply_ready=False,
        actual_node_id=SERVICE_NODE_ID,
        expected_node_id=SERVICE_NODE_ID,
        mismatch_reason=None,
        connected_since=None,
        last_packet_at=None,
    )
    harness.radio.status = lambda: down
    harness.sender.pump()
    assert harness.iface.sent == []
    assert harness.request_log.get_state(j.request_sender, j.request_packet_id) == "dropped"
    assert harness.limiter.check_global_tx() is None


def test_blocked_sender_gets_no_send(tmp_path):
    h = Harness(tmp_path, is_blocked=lambda sender: sender == "!aabbccdd")
    try:
        j = h.make_queued_job()
        h.sender.enqueue(j)
        h.sender.pump()
        assert h.iface.sent == []
        assert h.request_log.get_state(j.request_sender, j.request_packet_id) == "dropped"
    finally:
        h.stop()


# --- ReplyJob.release_on_pretransmission_failure (fix round 1, item 2) ------


def test_disconnected_at_send_time_calls_release_on_pretransmission_failure(harness):
    """A definite pre-transmission failure (the radio not being ready --
    `Reject.NOT_REPLY_READY`, checked before any radio call is attempted)
    calls the job's compensating action exactly once, with that reason."""
    calls = []
    j = harness.make_queued_job(on_pretransmission_failure=calls.append)
    harness.sender.enqueue(j)
    down = RadioStatus(
        connected=False,
        reply_ready=False,
        actual_node_id=SERVICE_NODE_ID,
        expected_node_id=SERVICE_NODE_ID,
        mismatch_reason=None,
        connected_since=None,
        last_packet_at=None,
    )
    harness.radio.status = lambda: down
    harness.sender.pump()
    assert calls == ["not_reply_ready"]


def test_blocked_sender_never_calls_release_on_pretransmission_failure(tmp_path):
    """The one hard exclusion (fix round 1, item 2): a blocked sender must
    never have its claim released -- a blocked node is not owed a retry."""
    calls = []
    h = Harness(tmp_path, is_blocked=lambda sender: sender == "!aabbccdd")
    try:
        j = h.make_queued_job(on_pretransmission_failure=calls.append)
        h.sender.enqueue(j)
        h.sender.pump()
        assert h.request_log.get_state(j.request_sender, j.request_packet_id) == "dropped"
        assert calls == []
    finally:
        h.stop()


def test_expired_reply_calls_release_on_pretransmission_failure(harness):
    """A job that expires in the queue (never popped in time, and so never
    even reaches the radio guard) is just as definitely never-transmitted
    as a `_guarded_send` refusal -- `_finish_expired` must call the
    compensating action too."""
    calls = []
    j = harness.make_queued_job(ttl=-1.0, on_pretransmission_failure=calls.append)
    harness.sender.enqueue(j)
    harness.sender.pump()
    assert harness.request_log.get_state(j.request_sender, j.request_packet_id) == "expired"
    assert calls == ["expired"]


# --- fix round 2: a raising callback must never wedge an unrelated job -----
#
# Fix round 1's callback was, in production, `Daemon._handle_knock`'s lambda
# calling `ChannelKnockCooldown.release` directly -- which touches a
# sqlite3.Connection owned by a DIFFERENT thread than the one `Sender._finish_
# expired`/`_mark_dropped` actually run on, and so always raised there (a
# Critical the reviewer caught; see task-3B-3-report.md's "Fix round 2"). This
# harness previously passed a plain `list.append` in place of that callback --
# which can never raise and can never observe a cross-thread fault -- so no
# test here could have caught it. These exercise a callback that DOES raise.


def test_a_raising_release_callback_never_escapes_mark_dropped(harness):
    """`_mark_dropped`'s own guard (fix round 2) around the callback call:
    a raising callback must not propagate out of `_mark_dropped` itself,
    and the reply's own outcome/state must be unaffected."""

    def raising_release(reason):
        raise RuntimeError("simulated cross-thread failure")

    j = harness.make_queued_job(on_pretransmission_failure=raising_release)
    harness.sender.enqueue(j)
    down = RadioStatus(
        connected=False,
        reply_ready=False,
        actual_node_id=SERVICE_NODE_ID,
        expected_node_id=SERVICE_NODE_ID,
        mismatch_reason=None,
        connected_since=None,
        last_packet_at=None,
    )
    harness.radio.status = lambda: down
    harness.sender.pump()  # must not raise
    assert harness.request_log.get_state(j.request_sender, j.request_packet_id) == "dropped"
    # The sender's slot is free again -- pump()'s own `finally` released it
    # regardless of the raising callback.
    assert harness.queue.offer(harness.make_queued_job(pid=2)) is None
    # Fix round 3, item 1: the failure is metered, not just logged.
    assert harness.metrics.reply_callback_errors_total._value.get() == 1


def test_a_raising_release_callback_never_escapes_finish_expired(harness):
    """`_finish_expired`'s own guard around the callback call, in isolation
    from `pump()`'s loop-level backstop below."""

    def raising_release(reason):
        raise RuntimeError("simulated cross-thread failure")

    j = harness.make_queued_job(ttl=-1.0, on_pretransmission_failure=raising_release)
    harness.sender.enqueue(j)
    harness.sender.pump()  # must not raise
    assert harness.request_log.get_state(j.request_sender, j.request_packet_id) == "expired"
    # Fix round 3, item 1: the failure is metered, not just logged.
    assert harness.metrics.reply_callback_errors_total._value.get() == 1


def test_a_raising_expired_callback_does_not_wedge_an_unrelated_ready_job(harness):
    """The Critical itself, reproduced structurally (no real threads
    needed: the bug was that an exception escaping the loop over EXPIRED
    jobs in `pump()` aborted before ever reaching a DIFFERENT, still-valid
    READY job's own try/finally -- permanently leaking THAT job's sender
    from `ReplyQueue._pending_senders` (`release()` never called), wedging
    every future reply to it for the life of the process. One already-
    expired job (sender `!aaaaaaaa`, callback raises) and one fresh, ready
    job for a DIFFERENT sender (`!bbbbbbbb`) are both present when the SAME
    `pump()` call pops the queue; FIFO order means the expired one is
    processed first, then the ready one. Before fix round 2's hardening,
    this `pump()` call would have raised, and `!bbbbbbbb` would never send
    and never free its slot again."""

    def raising_release(reason):
        raise RuntimeError("simulated cross-thread failure")

    expired_job = harness.make_queued_job(
        sender="!aaaaaaaa", pid=1, ttl=-1.0, on_pretransmission_failure=raising_release
    )
    ready_job = harness.make_queued_job(sender="!bbbbbbbb", pid=2, ttl=60.0)
    harness.sender.enqueue(expired_job)
    harness.sender.enqueue(ready_job)

    harness.sender.pump()  # must not raise, must not wedge !bbbbbbbb

    assert harness.request_log.get_state("!aaaaaaaa", 1) == "expired"
    # The ready job for a DIFFERENT sender was actually handed to the radio
    # -- proof its try/finally ran (release() was called), not silently
    # dropped along with the expired job's own failure.
    assert harness.iface.sent != []
    assert harness.request_log.get_state("!bbbbbbbb", 2) in ("submitted", "acked")
    # And its sender's slot is free again for a brand-new reply.
    assert harness.queue.offer(harness.make_queued_job(sender="!bbbbbbbb", pid=3)) is None
    # Fix round 3, item 1: the failure is metered, not just logged.
    assert harness.metrics.reply_callback_errors_total._value.get() == 1


def test_request_not_in_queued_state_is_refused(harness):
    now_wall = int(time.time())
    harness.request_log.claim("!aabbccdd", 1, now_wall, Command.MENU)  # left in 'received'
    j = ReplyJob(
        request_sender="!aabbccdd",
        request_packet_id=1,
        request_received_at=now_wall,
        deadline_monotonic=harness.clock() + 60.0,
        body="hi",
    )
    harness.sender.enqueue(j)
    harness.sender.pump()
    assert harness.iface.sent == []
    assert harness.request_log.get_state("!aabbccdd", 1) == "dropped"


def test_global_cooldown_consumed_even_when_send_text_raises_every_time(harness):
    """Ruling: the global TX cooldown must be committed on every actual
    send_text attempt -- including one that raises -- never only on
    success. Four queued jobs from different senders, radio always
    raising: only one sendText attempt should occur per
    global_tx_cooldown_seconds of (fake) clock, not four in a burst."""
    attempts = []

    def boom(*, text, destination_id):
        attempts.append(1)
        raise RuntimeError("radio exploded")

    harness.radio.send_text = boom
    for i in range(4):
        harness.sender.enqueue(harness.make_queued_job(sender=f"!0000000{i}", pid=i))

    for _ in range(4):
        harness.sender.pump()
    assert len(attempts) == 1  # the rest were deferred by the cooldown, not attempted

    harness.clock.advance(CFG.global_tx_cooldown_seconds + 0.1)
    harness.sender.pump()
    assert len(attempts) == 2


def test_reconnect_race_between_precheck_and_send_is_caught_atomically(harness):
    """Ruling: identity/readiness verification and interface acquisition
    happen under one lock acquisition inside RadioAdapter.send_text, so a
    reconnect landing between Sender's cheap pre-check and the actual send
    can never hand sendText an interface belonging to an unverified radio.
    Simulates that race: the pre-check (status()) still reports "ready",
    but the real state send_text re-checks has already flipped."""
    j = harness.make_queued_job()
    harness.sender.enqueue(j)

    stale_ready_status = RadioStatus(
        connected=True,
        reply_ready=True,
        actual_node_id=SERVICE_NODE_ID,
        expected_node_id=SERVICE_NODE_ID,
        mismatch_reason=None,
        connected_since=None,
        last_packet_at=None,
    )
    harness.radio.status = lambda: stale_ready_status  # Sender's pre-check: still "ready"
    harness.radio._reply_ready = False  # real state send_text re-checks: NOT ready

    harness.sender.pump()

    assert harness.iface.sent == []  # sendText itself was never called
    row = harness.conn.execute(
        "SELECT state, reason FROM processed_requests WHERE sender_node_id=? AND packet_id=?",
        (j.request_sender, j.request_packet_id),
    ).fetchone()
    assert row["state"] == "failed"
    assert row["reason"] == "not_ready_at_send"
    assert harness.limiter.check_global_tx() is Reject.RATE_LIMITED  # still consumed


def test_no_burst_after_reconnect_global_cooldown_still_spaces_sends(harness):
    j1 = harness.make_queued_job(sender="!aabbccdd", pid=1)
    j2 = harness.make_queued_job(sender="!11112222", pid=2)
    harness.sender.enqueue(j1)
    harness.sender.enqueue(j2)

    harness.sender.pump()  # sends j1, commits the global cooldown
    assert len(harness.iface.sent) == 1

    harness.sender.pump()  # j2 popped, but the global cooldown is still active
    assert len(harness.iface.sent) == 1  # NOT sent -- no burst
    assert harness.queue.depth() == 1  # requeued, not dropped
    assert harness.request_log.get_state(j2.request_sender, j2.request_packet_id) == "queued"

    harness.clock.advance(CFG.global_tx_cooldown_seconds + 0.1)
    harness.sender.pump()
    assert len(harness.iface.sent) == 2
    assert harness.iface.sent[1]["destinationId"] == j2.request_sender


# --- ack / routing classification --------------------------------------------


def _routing_packet(*, from_num, request_id, routing_error, packet_id, clock):
    return IncomingPacket(
        received_at=int(time.time()),
        received_monotonic=clock(),
        from_num=from_num,
        to_num=SERVICE_NODE_NUM,
        packet_id=packet_id,
        channel=0,
        portnum="ROUTING_APP",
        rx_time=None,
        rx_snr=None,
        rx_rssi=None,
        hop_limit=None,
        hop_start=None,
        via_mqtt=None,
        pki_encrypted=False,
        payload_len=0,
        text=None,
        request_id=request_id,
        routing_error=routing_error,
    )


def _tx_packet_id(harness, packet_id):
    row = harness.conn.execute(
        "SELECT tx_packet_id FROM processed_requests WHERE packet_id=?", (packet_id,)
    ).fetchone()
    return row["tx_packet_id"]


def test_routing_ack_from_far_end_marks_acked(harness):
    j = harness.make_queued_job(sender="!aabbccdd", pid=10)
    harness.sender.enqueue(j)
    harness.sender.pump()
    tx_id = _tx_packet_id(harness, 10)
    obs = _routing_packet(
        from_num=0xAABBCCDD,
        request_id=tx_id,
        routing_error="NONE",
        packet_id=9001,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(obs)
    assert harness.request_log.get_state("!aabbccdd", 10) == "acked"


def _replies_count(harness, outcome):
    return (
        harness.metrics.registry.get_sample_value("meshgarden_replies_total", {"outcome": outcome})
        or 0
    )


def test_implicit_self_ack_alone_leaves_state_submitted(harness):
    """Ruling: the firmware's implicit self-ack (routing NONE from our own
    node) means only "I transmitted it" -- not that the far end received
    anything. It must not settle the request into any terminal outcome, so
    a later genuine remote ack can still land."""
    j = harness.make_queued_job(sender="!aabbccdd", pid=11)
    harness.sender.enqueue(j)
    harness.sender.pump()
    tx_id = _tx_packet_id(harness, 11)
    acked_before = _replies_count(harness, "acked")
    unknown_before = _replies_count(harness, "unknown")

    obs = _routing_packet(
        from_num=SERVICE_NODE_NUM,
        request_id=tx_id,
        routing_error="NONE",
        packet_id=9002,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(obs)

    assert harness.request_log.get_state("!aabbccdd", 11) == "submitted"
    assert _replies_count(harness, "acked") == acked_before
    assert _replies_count(harness, "unknown") == unknown_before


def test_implicit_self_ack_then_remote_ack_marks_acked(harness):
    """The problematic real-radio ordering: the implicit self-ack is
    delivered before the genuine remote ack. Since the implicit ack no
    longer settles the row, the later remote ack must still succeed."""
    j = harness.make_queued_job(sender="!aabbccdd", pid=15)
    harness.sender.enqueue(j)
    harness.sender.pump()
    tx_id = _tx_packet_id(harness, 15)
    acked_before = _replies_count(harness, "acked")

    implicit = _routing_packet(
        from_num=SERVICE_NODE_NUM,
        request_id=tx_id,
        routing_error="NONE",
        packet_id=9007,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(implicit)
    assert harness.request_log.get_state("!aabbccdd", 15) == "submitted"

    remote = _routing_packet(
        from_num=0xAABBCCDD,
        request_id=tx_id,
        routing_error="NONE",
        packet_id=9008,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(remote)

    assert harness.request_log.get_state("!aabbccdd", 15) == "acked"
    assert _replies_count(harness, "acked") == acked_before + 1


def test_remote_ack_then_late_implicit_self_ack_stays_acked(harness):
    """The other ordering: a genuine remote ack arrives first. A late
    implicit self-ack must not raise and must not disturb the terminal
    'acked' state."""
    j = harness.make_queued_job(sender="!aabbccdd", pid=16)
    harness.sender.enqueue(j)
    harness.sender.pump()
    tx_id = _tx_packet_id(harness, 16)

    remote = _routing_packet(
        from_num=0xAABBCCDD,
        request_id=tx_id,
        routing_error="NONE",
        packet_id=9009,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(remote)
    assert harness.request_log.get_state("!aabbccdd", 16) == "acked"
    acked_after_remote = _replies_count(harness, "acked")

    late_implicit = _routing_packet(
        from_num=SERVICE_NODE_NUM,
        request_id=tx_id,
        routing_error="NONE",
        packet_id=9010,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(late_implicit)  # must not raise

    assert harness.request_log.get_state("!aabbccdd", 16) == "acked"
    assert _replies_count(harness, "acked") == acked_after_remote


def test_routing_nak_marks_failed(harness):
    j = harness.make_queued_job(sender="!aabbccdd", pid=12)
    harness.sender.enqueue(j)
    harness.sender.pump()
    tx_id = _tx_packet_id(harness, 12)
    obs = _routing_packet(
        from_num=0xAABBCCDD,
        request_id=tx_id,
        routing_error="MAX_RETRANSMIT",
        packet_id=9003,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(obs)
    assert harness.request_log.get_state("!aabbccdd", 12) == "failed"


def test_unknown_routing_error_collapses_to_other(harness):
    j = harness.make_queued_job(sender="!aabbccdd", pid=13)
    harness.sender.enqueue(j)
    harness.sender.pump()
    tx_id = _tx_packet_id(harness, 13)
    obs = _routing_packet(
        from_num=0xAABBCCDD,
        request_id=tx_id,
        routing_error="SOME_FUTURE_FIRMWARE_ERROR",
        packet_id=9005,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(obs)
    row = harness.conn.execute(
        "SELECT state, reason FROM processed_requests WHERE packet_id=13"
    ).fetchone()
    assert row["state"] == "failed"
    assert row["reason"] == "other"


def test_known_routing_error_passes_through_unchanged(harness):
    j = harness.make_queued_job(sender="!aabbccdd", pid=14)
    harness.sender.enqueue(j)
    harness.sender.pump()
    tx_id = _tx_packet_id(harness, 14)
    obs = _routing_packet(
        from_num=0xAABBCCDD,
        request_id=tx_id,
        routing_error="MAX_RETRANSMIT",
        packet_id=9006,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(obs)
    row = harness.conn.execute(
        "SELECT reason FROM processed_requests WHERE packet_id=14"
    ).fetchone()
    assert row["reason"] == "MAX_RETRANSMIT"


def test_unmatched_routing_packet_is_ignored(harness):
    obs = _routing_packet(
        from_num=0xAABBCCDD,
        request_id=999999,
        routing_error="NONE",
        packet_id=9004,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(obs)  # no matching tx id -- must not raise


def test_implicit_ack_then_aging_past_ack_timeout_marks_unknown(harness, tmp_path):
    """A `submitted` row with no remote ack is resolved to `unknown` only by
    ack_timeout_seconds aging (Retention.age_stale_submitted), never by the
    implicit self-ack itself."""
    j = harness.make_queued_job(sender="!aabbccdd", pid=17)
    harness.sender.enqueue(j)
    harness.sender.pump()
    tx_id = _tx_packet_id(harness, 17)

    implicit = _routing_packet(
        from_num=SERVICE_NODE_NUM,
        request_id=tx_id,
        routing_error="NONE",
        packet_id=9011,
        clock=harness.clock,
    )
    harness.transport.note_routing_packet(implicit)
    assert harness.request_log.get_state("!aabbccdd", 17) == "submitted"

    retention = Retention(
        harness.conn,
        RetentionConfig(),
        BackupConfig(directory=str(tmp_path / "backups")),
        log_dir=str(tmp_path / "logs"),
    )
    ack_timeout_seconds = CFG.ack_timeout_seconds
    now_wall = int(time.time())
    # updated_at was set to now_wall when pump() sent it; age past the cutoff.
    aged = retention.age_stale_submitted(now_wall + ack_timeout_seconds + 1, ack_timeout_seconds)

    assert aged == 1
    assert harness.request_log.get_state("!aabbccdd", 17) == "unknown"


# --- the invariant, exercised through eligibility + enqueue + pump ------------


def _maybe_enqueue_and_pump(harness, *, from_num, to_num, text, packet_id, channel=0):
    obs = IncomingPacket(
        received_at=int(time.time()),
        received_monotonic=harness.clock(),
        from_num=from_num,
        to_num=to_num,
        packet_id=packet_id,
        channel=channel,
        portnum="TEXT_MESSAGE_APP",
        rx_time=None,
        rx_snr=None,
        rx_rssi=None,
        hop_limit=None,
        hop_start=None,
        via_mqtt=None,
        pki_encrypted=False,
        payload_len=len((text or "").encode()),
        text=text,
        request_id=None,
        routing_error=None,
    )
    elig = check_eligibility(obs, my_node_num=SERVICE_NODE_NUM, reply_ready=True)
    if not elig.eligible:
        return
    sender = node_id_from_num(from_num)
    now_wall = int(time.time())
    if not harness.request_log.claim(sender, packet_id, now_wall, Command.MENU):
        return
    harness.request_log.set_state(sender, packet_id, "queued", now_wall)
    reply_job = ReplyJob(
        request_sender=sender,
        request_packet_id=packet_id,
        request_received_at=now_wall,
        deadline_monotonic=harness.clock() + 60.0,
        body="hi",
    )
    if harness.sender.enqueue(reply_job) is None:
        harness.sender.pump()


@pytest.mark.parametrize("channel", range(8))
def test_broadcast_on_any_channel_never_causes_a_send(harness, channel):
    _maybe_enqueue_and_pump(
        harness,
        from_num=0xAABBCCDD,
        to_num=0xFFFFFFFF,
        text="SIGN hello",
        packet_id=100 + channel,
        channel=channel,
    )
    assert harness.iface.sent == []


def test_dm_to_a_third_party_never_causes_a_send(harness):
    _maybe_enqueue_and_pump(
        harness, from_num=0xAABBCCDD, to_num=0x99999999, text="HELP", packet_id=200
    )
    assert harness.iface.sent == []


def test_self_echo_never_causes_a_send(harness):
    _maybe_enqueue_and_pump(
        harness,
        from_num=SERVICE_NODE_NUM,
        to_num=SERVICE_NODE_NUM,
        text="HELP",
        packet_id=201,
    )
    assert harness.iface.sent == []


# --- MeshCore tx_ref (task 3B-2 item D) -------------------------------------


class MeshCoreHarness:
    """Mirrors `Harness` above, but wired to a `FakeMeshCoreTransport`
    instead of a `FakeTransport` -- exercises `Sender` against a transport
    whose `tx_ref` is hex, never decimal (see
    tests/test_transport_contract.py::test_meshcore_tx_ref_is_not_safely_
    int_parseable)."""

    def __init__(self, tmp_path, is_blocked=None, cfg=CFG):
        self.clock = Clock()
        self.conn = connect(str(tmp_path / "sender_meshcore.db"))
        migrate(self.conn)
        self.request_log = RequestLog(self.conn, dedup_hours=24)
        self.metrics = Metrics()
        self.transport = FakeMeshCoreTransport(max_text_bytes=120)
        self.limiter = RateLimiter(cfg, clock=self.clock)
        self.queue = ReplyQueue(cfg)
        self.sender = Sender(
            transport=self.transport,
            cfg=cfg,
            limiter=self.limiter,
            queue=self.queue,
            request_log=self.request_log,
            metrics=self.metrics,
            expected_node_id=self.transport.identity(),
            is_blocked=is_blocked or (lambda sender: False),
            clock=self.clock,
        )
        # Wires the ack path exactly as main.py's Daemon.start() does for a
        # MeshCore transport: on_delivery -> Sender.on_delivery (routed
        # through the daemon's ingest queue in production, called directly
        # here since this harness is single-threaded).
        self.transport.on_delivery(self.sender.on_delivery)

    def make_queued_job(self, sender, pid=1, body="hi", ttl=60.0):
        now_wall = int(time.time())
        self.request_log.claim(sender, pid, now_wall, Command.MENU)
        self.request_log.set_state(sender, pid, "queued", now_wall)
        return ReplyJob(
            request_sender=sender,
            request_packet_id=pid,
            request_received_at=now_wall,
            deadline_monotonic=self.clock() + ttl,
            body=body,
        )


@pytest.fixture
def meshcore_harness(tmp_path):
    return MeshCoreHarness(tmp_path)


def test_meshcore_hex_tx_ref_is_correlated_in_memory_not_via_the_db_column(meshcore_harness):
    """Task 3B-2 item D: `sender.py`'s bare `int(tx_ref)` used to raise for
    MeshCore's hex tx_ref AFTER the transport had already "sent" (see
    tests/test_transport_contract.py::test_meshcore_tx_ref_is_not_safely_
    int_parseable). Pins the fix: the send completes without raising, the
    DB's `tx_packet_id` column stays NULL (no schema change, no corrupted
    int coercion of a hex string), and a later ACK still correlates via the
    in-memory map and settles the request to 'acked'.
    """
    h = meshcore_harness
    recipient_pubkey = "abcdef01" + "22" * 28
    h.transport.fake.emit_new_contact(recipient_pubkey, "contact-hex")
    recipient = "!" + recipient_pubkey[:8]
    # Forces a hex-lettered expected_ack, exactly like the transport-level
    # contract test does -- the real library's expected_ack is not a small
    # monotonic counter, so a letter-free value would be an artifact of the
    # fake, not a fact about MeshCore.
    h.transport.fake.commands._next_ack = 0xABCDEF01

    j = h.make_queued_job(sender=recipient)
    h.sender.enqueue(j)
    h.sender.pump()

    assert h.request_log.get_state(recipient, 1) == "submitted"
    row = h.conn.execute(
        "SELECT tx_packet_id FROM processed_requests WHERE sender_node_id=? AND packet_id=?",
        (recipient, 1),
    ).fetchone()
    assert row["tx_packet_id"] is None
    assert h.transport.fake.sent[0]["dst"] == recipient_pubkey

    h.transport.fake.emit_ack("abcdef01")
    assert h.request_log.get_state(recipient, 1) == "acked"


def test_meshcore_retry_ack_correlates_the_second_hex_tx_ref(meshcore_harness):
    h = meshcore_harness
    recipient_pubkey = "abcdef02" + "33" * 28
    h.transport.fake.emit_new_contact(recipient_pubkey, "retry-contact")
    recipient = "!" + recipient_pubkey[:8]
    h.transport.fake.commands._next_ack = 0xABCDEF10

    j = h.make_queued_job(sender=recipient, pid=2, body="MENU SOCIAL")
    h.sender.enqueue(j)
    h.sender.pump()
    h.clock.advance(CFG.application_retry_delay_seconds + 1)
    h.sender.pump()
    assert len(h.transport.fake.sent) == 2

    h.transport.fake.emit_ack("abcdef11")
    assert h.request_log.get_state(recipient, 2) == "acked"


def test_meshcore_retries_reset_stale_path_then_flood(meshcore_harness):
    """The upstream MeshCore retry strategy resets a cached route after two
    unanswered direct submissions. MeshGarden keeps its fixed 15-second
    schedule but follows the same recovery rule: original + retry 1 use the
    contact route, then retries 2 and 3 use flood mode.
    """
    h = meshcore_harness
    recipient_pubkey = "abcdef03" + "44" * 28
    h.transport.fake.emit_new_contact(recipient_pubkey, "mobile-contact")
    recipient = "!" + recipient_pubkey[:8]

    j = h.make_queued_job(sender=recipient, pid=3, body="PING")
    h.sender.enqueue(j)
    h.sender.pump()
    for _ in range(3):
        h.clock.advance(CFG.application_retry_delay_seconds)
        h.sender.pump()

    assert [sent["attempt"] for sent in h.transport.fake.sent] == [0, 1, 2, 3]
    assert h.transport.fake.paths_reset == [recipient_pubkey]


def _mobile_harness(tmp_path, **changes):
    cfg = replace(
        CFG,
        mobile_dm_recovery_enabled=True,
        mobile_dm_recovery_min_interval_seconds=30,
        **changes,
    )
    h = MeshCoreHarness(tmp_path, cfg=cfg)
    h.transport.dm_ack_wait_seconds = lambda tx_ref, configured: max(configured, 5.0)
    return h


def test_mobile_recovery_acks_normally_without_a_flood(tmp_path):
    h = _mobile_harness(tmp_path)
    pubkey = "abcdef04" + "55" * 28
    h.transport.fake.emit_new_contact(pubkey, "mobile-ok")
    recipient = "!" + pubkey[:8]
    h.sender.enqueue(h.make_queued_job(recipient, pid=40))
    h.sender.pump()
    h.transport.fake.emit_ack("00000001")
    h.clock.advance(CFG.application_retry_delay_seconds + 1)
    h.sender.pump()
    assert len(h.transport.fake.sent) == 1
    assert h.transport.fake.paths_reset == []
    assert h.metrics.mobile_dm_recovery_total.labels("normal_success")._value.get() == 1


def test_mobile_recovery_times_out_then_resets_and_sends_once(tmp_path):
    h = _mobile_harness(tmp_path)
    pubkey = "abcdef05" + "66" * 28
    h.transport.fake.emit_new_contact(pubkey, "mobile-moved")
    recipient = "!" + pubkey[:8]
    h.sender.enqueue(h.make_queued_job(recipient, pid=41))
    h.sender.pump()
    h.clock.advance(CFG.application_retry_delay_seconds)
    h.sender.pump()
    h.clock.advance(CFG.application_retry_delay_seconds * 4)
    h.sender.pump()
    assert [sent["attempt"] for sent in h.transport.fake.sent] == [0, 1]
    assert h.transport.fake.paths_reset == [pubkey]
    assert h.metrics.mobile_dm_recovery_total.labels("normal_timeout")._value.get() == 1
    assert h.metrics.mobile_dm_recovery_total.labels("fallback_sent")._value.get() == 1


def test_mobile_recovery_allowlist_and_zero_retry_preserve_legacy_policy(tmp_path):
    h = _mobile_harness(
        tmp_path,
        mobile_dm_recovery_destinations=["!11112222"],
        mobile_dm_max_retries=0,
    )
    pubkey = "abcdef06" + "77" * 28
    h.transport.fake.emit_new_contact(pubkey, "ordinary")
    recipient = "!" + pubkey[:8]
    h.sender.enqueue(h.make_queued_job(recipient, pid=42))
    h.sender.pump()
    h.clock.advance(CFG.application_retry_delay_seconds)
    h.sender.pump()
    assert [sent["attempt"] for sent in h.transport.fake.sent] == [0, 1]
    assert h.transport.fake.paths_reset == []


def test_mobile_recovery_max_zero_disables_all_retries_for_matching_destination(tmp_path):
    h = _mobile_harness(tmp_path, mobile_dm_max_retries=0)
    pubkey = "abcdef07" + "88" * 28
    h.transport.fake.emit_new_contact(pubkey, "no-fallback")
    recipient = "!" + pubkey[:8]
    h.sender.enqueue(h.make_queued_job(recipient, pid=43))
    h.sender.pump()
    h.clock.advance(100)
    h.sender.pump()
    assert [sent["attempt"] for sent in h.transport.fake.sent] == [0]


def test_mobile_recovery_uses_firmware_suggested_wait_when_longer(tmp_path):
    h = _mobile_harness(tmp_path, application_retry_delay_seconds=3)
    pubkey = "abcdef08" + "99" * 28
    h.transport.fake.emit_new_contact(pubkey, "slow-ack")
    recipient = "!" + pubkey[:8]
    h.sender.enqueue(h.make_queued_job(recipient, pid=44))
    h.sender.pump()
    h.clock.advance(4.9)
    h.sender.pump()
    assert len(h.transport.fake.sent) == 1
    h.clock.advance(0.2)
    h.sender.pump()
    assert len(h.transport.fake.sent) == 2


def test_mobile_recovery_fallback_ack_is_counted_by_its_tx_ref(tmp_path):
    h = _mobile_harness(tmp_path)
    pubkey = "abcdef09" + "aa" * 28
    h.transport.fake.emit_new_contact(pubkey, "fallback-ack")
    recipient = "!" + pubkey[:8]
    h.sender.enqueue(h.make_queued_job(recipient, pid=45))
    h.sender.pump()
    h.clock.advance(15)
    h.sender.pump()
    h.transport.fake.emit_ack("00000002")
    assert h.metrics.mobile_dm_recovery_total.labels("fallback_success")._value.get() == 1
    assert h.metrics.mobile_dm_recovery_total.labels("normal_success")._value.get() == 0


def test_mobile_recovery_late_ack_during_reset_cancels_fallback(tmp_path):
    h = _mobile_harness(tmp_path)
    pubkey = "abcdef0a" + "bb" * 28
    h.transport.fake.emit_new_contact(pubkey, "late-ack")
    recipient = "!" + pubkey[:8]
    h.sender.enqueue(h.make_queued_job(recipient, pid=46))
    h.sender.pump()
    original_reset = h.transport.reset_dm_path

    def reset_then_ack(to):
        result = original_reset(to)
        h.transport.fake.emit_ack("00000001")
        return result

    h.transport.reset_dm_path = reset_then_ack
    h.clock.advance(15)
    h.sender.pump()
    assert len(h.transport.fake.sent) == 1
    assert h.request_log.get_state(recipient, 46) == "acked"


def test_mobile_recovery_newer_reply_during_reset_cancels_old_fallback(tmp_path):
    h = _mobile_harness(tmp_path)
    pubkey = "abcdef0b" + "cc" * 28
    h.transport.fake.emit_new_contact(pubkey, "newer")
    recipient = "!" + pubkey[:8]
    h.sender.enqueue(h.make_queued_job(recipient, pid=47))
    h.sender.pump()
    original_reset = h.transport.reset_dm_path

    def reset_then_enqueue(to):
        result = original_reset(to)
        assert h.sender.enqueue(h.make_queued_job(recipient, pid=48)) is None
        return result

    h.transport.reset_dm_path = reset_then_enqueue
    h.clock.advance(15)
    h.sender.pump()
    assert len(h.transport.fake.sent) == 1


def test_mobile_recovery_reset_failure_never_sends_fallback(tmp_path):
    h = _mobile_harness(tmp_path)
    pubkey = "abcdef0c" + "dd" * 28
    h.transport.fake.emit_new_contact(pubkey, "reset-fail")
    recipient = "!" + pubkey[:8]
    h.sender.enqueue(h.make_queued_job(recipient, pid=49))
    h.sender.pump()
    h.transport.reset_dm_path = lambda to: False
    h.clock.advance(15)
    h.sender.pump()
    h.sender.pump()
    assert len(h.transport.fake.sent) == 1
    assert h.metrics.mobile_dm_recovery_total.labels("suppressed_path_reset")._value.get() == 1


def test_mobile_recovery_destination_cooldown_suppresses_second_fallback(tmp_path):
    h = _mobile_harness(tmp_path)
    pubkey = "abcdef0d" + "ee" * 28
    h.transport.fake.emit_new_contact(pubkey, "cooldown")
    recipient = "!" + pubkey[:8]
    h.sender.enqueue(h.make_queued_job(recipient, pid=50))
    h.sender.pump()
    h.clock.advance(15)
    h.sender.pump()
    h.sender.enqueue(h.make_queued_job(recipient, pid=51))
    h.clock.advance(5)
    h.sender.pump()
    h.clock.advance(16)
    h.sender.pump()
    h.sender.pump()
    assert [sent["attempt"] for sent in h.transport.fake.sent] == [0, 1, 0]
    assert h.metrics.mobile_dm_recovery_total.labels("suppressed_cooldown")._value.get() == 1


def test_mobile_recovery_global_spacing_suppresses_other_destination(tmp_path):
    h = _mobile_harness(tmp_path)
    recipients = []
    for prefix in ("abcdef0e", "abcdef0f"):
        pubkey = prefix + "ff" * 28
        h.transport.fake.emit_new_contact(pubkey, prefix)
        recipients.append("!" + prefix)
    h.sender.enqueue(h.make_queued_job(recipients[0], pid=52))
    h.sender.pump()
    h.clock.advance(15)
    h.sender.pump()
    h.sender.enqueue(h.make_queued_job(recipients[1], pid=53))
    h.clock.advance(5)
    h.sender.pump()
    h.clock.advance(16)
    h.sender.pump()
    h.sender.pump()
    assert [sent["attempt"] for sent in h.transport.fake.sent] == [0, 1, 0]
    assert h.metrics.mobile_dm_recovery_total.labels("suppressed_global")._value.get() == 1


def test_mobile_recovery_fallback_send_error_never_retries_again(tmp_path):
    h = _mobile_harness(tmp_path)
    pubkey = "abcdef10" + "11" * 28
    h.transport.fake.emit_new_contact(pubkey, "send-fail")
    recipient = "!" + pubkey[:8]
    h.sender.enqueue(h.make_queued_job(recipient, pid=54))
    h.sender.pump()
    h.transport.fake.commands.force_next_send_error()
    h.clock.advance(15)
    h.sender.pump()
    h.clock.advance(100)
    h.sender.pump()
    assert len(h.transport.fake.sent) == 1
    assert h.metrics.mobile_dm_recovery_total.labels("fallback_failed")._value.get() == 1


def test_mobile_recovery_hourly_cap_suppresses_after_configured_count(tmp_path):
    h = _mobile_harness(tmp_path, mobile_dm_global_recoveries_per_hour=1)
    recipients = []
    for prefix in ("abcdef11", "abcdef12"):
        pubkey = prefix + "22" * 28
        h.transport.fake.emit_new_contact(pubkey, prefix)
        recipients.append("!" + prefix)
    h.sender.enqueue(h.make_queued_job(recipients[0], pid=55))
    h.sender.pump()
    h.clock.advance(15)
    h.sender.pump()
    h.sender.enqueue(h.make_queued_job(recipients[1], pid=56))
    h.clock.advance(30)
    h.sender.pump()
    h.clock.advance(15)
    h.sender.pump()  # consumes the first fallback's timeout marker
    h.sender.pump()  # evaluates the second destination's recovery
    assert [sent["attempt"] for sent in h.transport.fake.sent] == [0, 1, 0]
    assert h.metrics.mobile_dm_recovery_total.labels("suppressed_global")._value.get() == 1


def test_meshcore_unrecognized_tx_ref_delivery_is_ignored_not_a_crash(meshcore_harness):
    """A delivery for a tx_ref this process never sent (e.g. a restart, or
    a request whose map entry already resolved and was popped) must be a
    silent, logged no-op -- never an exception that could take down the DB
    worker thread that calls `Sender.on_delivery`."""
    h = meshcore_harness
    h.sender.on_delivery(Delivery(tx_ref="deadbeef", outcome=DeliveryOutcome.ACKED))  # no raise


def test_meshcore_numeric_looking_tx_ref_still_correlates_by_the_hex_path(meshcore_harness):
    """A hex expected_ack that HAPPENS to contain only decimal digits (no
    a-f) would parse as an int -- but sender.py's own persistence path only
    ever writes what int(tx_ref) actually produces, so this must still
    correlate correctly either way; documents the boundary rather than
    asserting a specific internal branch."""
    h = meshcore_harness
    recipient_pubkey = "01020304" + "22" * 28
    h.transport.fake.emit_new_contact(recipient_pubkey, "contact-digits")
    recipient = "!" + recipient_pubkey[:8]
    h.transport.fake.commands._next_ack = 0x01020304  # hex "01020304" -- all decimal digits

    j = h.make_queued_job(sender=recipient)
    h.sender.enqueue(j)
    h.sender.pump()
    assert h.request_log.get_state(recipient, 1) == "submitted"

    h.transport.fake.emit_ack("01020304")
    assert h.request_log.get_state(recipient, 1) == "acked"


def test_pop_ready_holds_a_delayed_job_but_lets_a_ready_one_pass():
    """A channel reply stamped with not_before_monotonic stays queued (slot
    held, FIFO place kept) until that time, while a DM behind it goes
    first; once the clock passes the stamp it is returned."""
    from dataclasses import replace

    q = ReplyQueue(CFG)
    delayed = replace(
        job(sender="!00000001", pid=1), reply_channel_idx=2, not_before_monotonic=100.0
    )
    dm = job(sender="!00000002", pid=2)
    assert q.offer(delayed) is None and q.offer(dm) is None
    got, expired = q.pop_ready(50.0)
    assert got is dm and expired == []
    q.release(dm.queue_key())
    assert q.pop_ready(50.0) == (None, [])
    assert q.depth() == 1 and q.in_flight() == 1  # still queued, slot still held
    got, _ = q.pop_ready(100.0)
    assert got is delayed


def test_enqueue_delays_channel_replies_but_not_dms(harness):
    from dataclasses import replace

    dm = harness.make_queued_job()
    assert harness.sender.enqueue(dm) is None
    ch = replace(harness.make_queued_job(sender="!aabbccde", pid=2), reply_channel_idx=2)
    assert harness.sender.enqueue(ch) is None
    stamped = {j.request_packet_id: j.not_before_monotonic for j in harness.queue._queue}
    assert stamped[dm.request_packet_id] == 0.0
    assert stamped[ch.request_packet_id] == harness.clock() + CFG.channel_reply_delay_seconds
