"""End-to-end MeshCore coverage for task 3B-2: the daemon driven over a
FakeMeshCoreTransport (tests/fake_meshcore.py), with no real hardware and no
`meshcore` package import at all. This is the capability the task brief asks
for explicitly ("Use them to run the daemon end to end over MeshCore without
hardware... if it does not exist after your work, say so loudly") and that
3B-3's adversarial review depends on.

Also pins the wedged-run supervision (`Daemon._check_meshcore_wedge`, task
3B-2's "three lifecycle facts" #1): a persistently disconnected transport
must be treated as a reason to stop() and reconnect(), never waited out.
"""

import json
import sqlite3
import threading
import time

import pytest

from meshgarden.commands import knock_text, mail_sent_ack_text
from meshgarden.config import (
    BackupConfig,
    Config,
    DatabaseConfig,
    MetricsConfig,
    RadioConfig,
    RetentionConfig,
    ServiceConfig,
)
from meshgarden.main import Daemon
from meshgarden.transport import TransportStatus
from tests.fake_meshcore import FakeMeshCoreTransport

LAB_UNLOCK_WORD = "SUPERGARDENME"
SELF_PUBLIC_KEY = "11223344" + "00" * 28  # -> identity "!11223344", FakeMeshCoreTransport's default
VISITOR_PUBKEY = "aabbccdd" + "11" * 28
VISITOR_NODE_ID = "!" + VISITOR_PUBKEY[:8]


def _cfg(tmp_path, *, radio_kwargs=None, bbs_path="", **service_kwargs):
    return Config(
        radio=RadioConfig(
            device="/dev/fake",
            expected_node_id="!11223344",
            transport="meshcore",
            **(radio_kwargs or {}),
        ),
        service=ServiceConfig(lab_unlock_word=LAB_UNLOCK_WORD, **service_kwargs),
        database=DatabaseConfig(path=str(tmp_path / "o.db"), bbs_path=bbs_path),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "backups")),
        metrics=MetricsConfig(),
    )


def _start_meshcore_daemon(cfg, *, self_public_key=SELF_PUBLIC_KEY, stored_contacts=()):
    """Builds and starts a real `Daemon` over a real `FakeMeshCoreTransport`,
    same recipe the `meshcore_daemon` fixture uses -- factored out so the
    restart test (task 3B-3) can build a genuinely SECOND daemon/transport
    pair over the same database file, rather than reusing the fixture's."""
    ft = FakeMeshCoreTransport(self_public_key=self_public_key, stored_contacts=stored_contacts)
    # decide_inbound row 9 (transport-boundary-spec.md section 1): MeshCore's
    # Inbound.rx_time is always None, so freshness falls back entirely to
    # `connected_seconds < reply_ttl_seconds` -- a transport that "just
    # connected" microseconds ago in test time is otherwise STALE on every
    # single message. Backdating this private clock is the same shape as a
    # real install that has been connected for a while before its first DM.
    ft._connected_since_monotonic -= 1000.0
    d = Daemon(cfg, transport=ft, start_metrics=False)
    d.start()
    return d, ft


@pytest.fixture
def meshcore_daemon(tmp_path):
    cfg = _cfg(tmp_path)
    # watched_channel_idx=0 (the default): channel triggering is "on" for
    # this fixture, exercising both the DM and the channel-knock paths.
    d, ft = _start_meshcore_daemon(cfg)
    yield d, ft
    d.stop()


@pytest.fixture
def meshcore_channel_daemon(tmp_path):
    d, ft = _start_meshcore_daemon(_cfg(tmp_path, mode="channel", mail_public=True))
    yield d, ft
    d.stop()


# --- end to end: DM command dispatch ----------------------------------------


def test_owned_transport_imports_contact_names_into_database_on_start(tmp_path, monkeypatch):
    meshcore = pytest.importorskip("meshcore")
    from tests.test_meshcore_client import _StubMC

    stub = _StubMC({"public_key": SELF_PUBLIC_KEY}, contacts={
        VISITOR_PUBKEY: {"public_key": VISITOR_PUBKEY, "adv_name": "Apple visitor",
                         "out_path_len": 2, "out_path": "00ab"},
    })

    async def fake_create_serial(cls, *args, **kwargs):
        return stub

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(fake_create_serial))
    cfg = _cfg(tmp_path, radio_kwargs={"meshcore_channel": ""})
    daemon = Daemon(cfg, start_metrics=False)
    try:
        daemon.start()
        deadline = time.monotonic() + 5
        with sqlite3.connect(cfg.database.path) as conn:
            while time.monotonic() < deadline:
                row = conn.execute(
                    "SELECT short_name, observation_count, stored_route "
                    "FROM nodes WHERE node_id = ?",
                    (VISITOR_NODE_ID,),
                ).fetchone()
                if row is not None and row[2] is not None:
                    break
                time.sleep(0.01)
            assert row[:2] == ("Apple visitor", 0)
            route = json.loads(row[2])
            assert route["hops"] == 2 and route["hashes"] == ["00", "ab"]
    finally:
        daemon.stop()


def test_daemon_answers_a_meshcore_direct_message_end_to_end(meshcore_daemon):
    """No hardware, no meshcore package import -- the whole receive ->
    decide_inbound -> parse_command -> dispatch -> Sender.pump() -> send_dm
    path, driven purely through the fake companion and the real
    MeshCoreTransport/Daemon production classes."""
    d, ft = meshcore_daemon
    full_pubkey = "aabbccdd" + "11" * 28
    ft.fake.emit_new_contact(full_pubkey, "visitor")
    ft.fake.emit_direct_message(full_pubkey[:12], "MENU")
    d.drain()

    assert len(ft.fake.sent) == 1
    assert ft.fake.sent[0]["dst"] == full_pubkey
    assert "MENU" in ft.fake.sent[0]["msg"] or len(ft.fake.sent[0]["msg"]) > 0


def test_repeater_command_returns_cached_stats_and_does_not_wait_for_rf(meshcore_daemon):
    d, ft = meshcore_daemon
    visitor = "aabbccdd" + "44" * 28
    from dataclasses import replace
    d.cfg = replace(d.cfg, radio=replace(d.cfg.radio, meshcore_repeater_public_key=visitor))
    ft.fake.emit_new_contact(visitor, "visitor")
    calls = []

    def repeater_status(*, start_refresh=True):
        calls.append(start_refresh)
        return "ready", {
            "bat": 4123, "uptime": 183600, "nb_recv": 1234, "nb_sent": 456,
            "last_rssi": -91, "last_snr": 7.5,
        }, 12, None

    ft.fake.repeater_status = repeater_status
    ft.fake.emit_direct_message(visitor[:12], "REPEATER")
    d.drain()

    assert calls == [False]
    reply = ft.fake.sent[-1]["msg"]
    assert "visitor" in reply and "ccdd" in reply
    assert "4.12V" in reply and "up 2d3h" in reply
    assert "RX 1234" in reply and "TX 456" in reply
    assert "RSSI -91" in reply and "SNR 7.5" in reply
    assert len(reply.encode()) <= ft.max_text_bytes


def test_repeater_command_starts_refresh_only_after_reply_is_reserved(meshcore_daemon):
    d, ft = meshcore_daemon
    visitor = "bbccddee" + "55" * 28
    ft.fake.emit_new_contact(visitor, "visitor")
    calls = []

    def query(*, start_refresh=True):
        calls.append(start_refresh)
        return ("pending" if start_refresh else "idle"), None, None, None

    ft.fake.repeater_status = query
    ft.fake.emit_direct_message(visitor[:12], "REPEATER")
    d.drain()
    assert calls == [False, True]
    assert "querying repeater" in ft.fake.sent[-1]["msg"].lower()


def test_repeater_queue_refusal_never_starts_an_rf_query(meshcore_daemon, monkeypatch):
    d, ft = meshcore_daemon
    visitor = "ccddee11" + "66" * 28
    ft.fake.emit_new_contact(visitor, "visitor")
    calls = []

    def query(*, start_refresh=True):
        calls.append(start_refresh)
        return ("pending" if start_refresh else "idle"), None, None, None

    ft.fake.repeater_status = query
    monkeypatch.setattr(d, "_reserve_and_send", lambda *args, **kwargs: False)
    ft.fake.emit_direct_message(visitor[:12], "REPEATER")
    d.drain()
    assert calls == [False]
    assert ft.fake.sent == []


def test_channel_repeater_text_never_starts_a_query(meshcore_daemon):
    d, ft = meshcore_daemon
    calls = []

    def query(*, start_refresh=True):
        calls.append(start_refresh)
        return "idle", None, None, None

    ft.fake.repeater_status = query
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", "REPEATER")
    d.drain()
    assert calls == []
    assert ft.fake.sent == []


def test_daemon_drops_a_meshcore_dm_from_an_unresolvable_sender_silently(meshcore_daemon):
    """A DM's sender is always derivable from the 6-byte pubkey_prefix
    alone (no roster lookup needed to RECEIVE it) -- but replying needs the
    full public key, which this sender never advertised. The reply must
    fail cleanly (no send, no crash), never a duplicate or a channel
    fallback."""
    d, ft = meshcore_daemon
    ft.fake.emit_direct_message("abcdef012345", "MENU")
    d.drain()
    assert ft.fake.sent == []


# --- task 3D: node identifiers through the transport ------------------------
#
# SEEN/MAIL/ME must accept and display node ids in whichever shape the
# CONFIGURED transport uses. MeshCore renders/accepts the identical
# "!xxxxxxxx" shape Meshtastic does (no separate short form -- transport-
# boundary-spec.md section 6), so these tests pin that the SAME resolution
# and formatting rules (`Collector.resolve_node`'s `parse_node_argument`,
# `transport.short_label`, `transport.display_label`) actually fire when the
# configured transport is MeshCore, not just when it happens to be
# Meshtastic.


def test_meshcore_seen_accepts_both_a_full_id_and_a_bare_tail_argument(meshcore_daemon):
    """MeshCore's trigger/reply path never populates `node_daily`
    (transport-boundary-spec.md section 6: the observation path stays
    Meshtastic-shaped), so a MeshCore-known contact is always "not heard"
    from SEEN's point of view -- this lands in `_node_label_for`, exactly
    the function task 3D rewired onto `transport.parse_node_argument`/
    `transport.short_label`. Both the full id and the bare 4-hex tail must
    still resolve to (and display) the SAME node."""
    d, ft = meshcore_daemon
    target_pubkey = "aabbccdd" + "22" * 28  # -> "!aabbccdd", tail "ccdd"
    ft.fake.emit_new_contact(target_pubkey, "target")

    asker_pubkey = "99887766" + "33" * 28
    ft.fake.emit_new_contact(asker_pubkey, "asker")

    ft.fake.emit_direct_message(asker_pubkey[:12], "SEEN !aabbccdd")
    d.drain()
    assert "ccdd" in ft.fake.sent[-1]["msg"]

    d.advance_seconds(d.cfg.service.per_node_cooldown_seconds + 1)
    ft.fake.emit_direct_message(asker_pubkey[:12], "SEEN ccdd")
    d.drain()
    assert "ccdd" in ft.fake.sent[-1]["msg"]


def test_meshcore_seen_unresolvable_name_never_echoes_the_argument(meshcore_daemon):
    """The no-echo rule (ruling 1) holds over MeshCore too: an unresolvable
    short-name argument must never be reflected back, on any transport."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "asker")
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "SEEN nosuchname")
    d.drain()
    reply = ft.fake.sent[-1]["msg"]
    assert "nosuchname" not in reply
    assert "that node" in reply


def test_meshcore_me_reply_shows_the_requesters_own_id_via_display_label(meshcore_daemon):
    """ME's header names the REQUESTER's own id (not the daemon's), routed
    through `transport.display_label` (task 3D) -- the identity function on
    both current transports, so this must still be the visitor's own full
    "!xxxxxxxx" id, unabbreviated."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "asker")
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "ME")
    d.drain()
    reply = ft.fake.sent[-1]["msg"]
    assert f"({VISITOR_NODE_ID})" in reply


def test_meshcore_mail_accepts_a_bare_tail_recipient_argument(meshcore_daemon):
    """MAIL <node> <msg> must resolve a bare 4-hex tail recipient the same
    way SEEN does (`Collector.resolve_node` with `transport.
    parse_node_argument`) -- a well-formed send always gets the same
    neutral ack, whether or not the note was actually stored (ruling 10).

    MAIL is LAB-gated (`commands.LAB_COMMANDS`), so the asker unlocks LAB
    first -- same as any other LAB command over any transport."""
    d, ft = meshcore_daemon
    # A DIFFERENT first-4-bytes prefix than VISITOR_PUBKEY's -- otherwise
    # the recipient and the sender would derive to the same node id.
    recipient_pubkey = "ffeeddcc" + "44" * 28  # -> "!ffeeddcc", tail "ddcc"
    ft.fake.emit_new_contact(recipient_pubkey, "recipient")
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "asker")

    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], LAB_UNLOCK_WORD)
    d.drain()
    d.advance_seconds(d.cfg.service.per_node_cooldown_seconds + 1)

    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MAIL ddcc hello there")
    d.drain()
    assert ft.fake.sent[-1]["msg"] == mail_sent_ack_text()


def test_meshcore_mail_rejects_a_malformed_recipient_argument(meshcore_daemon):
    """An argument that resolves to nothing (no full id, no tail match, no
    short-name match) is a rejected MAIL, not a send -- same as Meshtastic.
    (The LAB unlock word itself always gets its own reply, so this checks
    that MAIL adds no FURTHER send, rather than `sent == []`.)"""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "asker")
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], LAB_UNLOCK_WORD)
    d.drain()
    d.advance_seconds(d.cfg.service.per_node_cooldown_seconds + 1)

    sent_before = len(ft.fake.sent)
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MAIL nosuchname hello")
    d.drain()
    assert len(ft.fake.sent) == sent_before


# --- the channel knock (task 3B-3) ------------------------------------------
#
# scope-decisions.md section 7: a channel trigger may produce EXACTLY ONE
# outcome -- a single rate-limited direct message to the resolved contact,
# carrying invite text, never a command. These tests pin the guarantee that,
# before this task, existed only in prose (task-3B-2-report.md).


def test_meshcore_knock_from_a_resolved_contact_sends_exactly_one_dm_and_no_command(
    meshcore_daemon,
):
    """The positive case. Asserts the sent body is `knock_text()` itself --
    not merely "some non-empty body" -- so this test cannot pass for the
    same reason `test_daemon_answers_a_meshcore_direct_message_end_to_end`
    passes (a real MENU dispatch) or for no reason at all (item E: the
    pre-3B-3 knock test and the ordinary-chatter test asserted the
    identical `sent == []`, so neither pinned that a knock actually reaches
    the KNOCK outcome)."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", "!garden")
    d.drain()

    assert len(ft.fake.sent) == 1
    assert ft.fake.sent[0]["dst"] == VISITOR_PUBKEY
    assert ft.fake.sent[0]["msg"] == knock_text()


def test_a_forged_knock_never_denies_the_impersonated_nodes_own_next_command(meshcore_daemon):
    """Fix round 1, item 1 (the important one). A reviewer's probe found
    that reusing `_reserve_and_send` unmodified committed the GENERIC
    per-node `RateLimiter` for the knock's resolved recipient -- so the
    impersonated node's own next genuine DM command was silently REJECTED
    (not merely delayed) at `_claim_and_gate`'s `check_command` gate: no
    reply, no `processed_requests` row at all. Unauthenticated channel
    input imposing a service denial on an innocent third party is exactly
    what the bounded-knock design exists to prevent. `_reserve_and_send`
    now takes `commit_rate_limit=False` for the knock specifically."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", "!garden")
    d.drain()
    assert len(ft.fake.sent) == 1
    assert ft.fake.sent[0]["msg"] == knock_text()

    # Immediately afterward (same real time -- well inside
    # per_node_cooldown_seconds, the window this bug would have tripped),
    # the impersonated node sends a genuine DM. It must get its normal
    # reply, not silence.
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MENU")
    d.drain()

    assert len(ft.fake.sent) == 2
    assert ft.fake.sent[1]["dst"] == VISITOR_PUBKEY
    assert ft.fake.sent[1]["msg"] != knock_text()  # a real MENU reply, not another knock


def test_knock_cooldown_release_reaches_the_db_worker_even_from_a_different_thread(
    meshcore_daemon,
):
    """Fix round 2, item 3 (the Critical). In production, `Sender._finish_
    expired`/`_mark_dropped` call the knock's release callback from the TX
    WORKER thread; `self._knock_cooldown` is built on the DB worker
    thread's own connection. Racing the daemon's OWN live TX worker thread
    to force a real send failure at exactly the right moment would be
    inherently flaky (it might just send the reply first) -- so this
    drives the identical cross-thread contract deterministically instead:
    a genuinely separate, manually spawned thread (never one of the
    daemon's own two worker threads) calls `_enqueue_knock_release`
    directly, exactly as `Sender` would. Before fix round 2 (when the
    callback called `ChannelKnockCooldown.release` directly instead of
    enqueuing), this would have raised `sqlite3.ProgrammingError` on that
    thread -- cross-thread `sqlite3.Connection` use is never relaxed
    anywhere in this codebase.
    """
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", "!garden")
    d.drain()
    assert len(ft.fake.sent) == 1

    row = d.conn.execute(
        "SELECT last_knock_at FROM channel_knocks WHERE node_id = ?", (VISITOR_NODE_ID,)
    ).fetchone()
    assert row is not None
    claimed_at = row["last_knock_at"]

    errors: list[BaseException] = []

    def call_from_another_thread() -> None:
        try:
            d._enqueue_knock_release(VISITOR_NODE_ID, claimed_at)
        except BaseException as exc:  # noqa: BLE001 -- must observe ANY failure here
            errors.append(exc)

    t = threading.Thread(target=call_from_another_thread)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive()
    assert errors == []  # the enqueue itself never touches the DB connection

    d.drain()  # waits on _ingest.join(): the DB worker has now applied the release

    row = d.conn.execute(
        "SELECT last_knock_at FROM channel_knocks WHERE node_id = ?", (VISITOR_NODE_ID,)
    ).fetchone()
    assert row is None  # released

    # A second knock, well inside the original cooldown window, now succeeds
    # -- proof the release genuinely reached the DB worker's own connection,
    # not merely that the enqueue call didn't raise. A fresh sender_timestamp
    # (rather than the default 0) both distinguishes this from the first
    # knock's derived raw_id and keeps it inside the freshness gate now that
    # `_is_stale` trusts `sender_ts` when present (fix round: freshness).
    ft.fake.emit_channel_message_from(0, "visitor", "!garden", sender_timestamp=int(time.time()))
    d.drain()
    assert len(ft.fake.sent) == 2
    assert ft.fake.sent[1]["dst"] == VISITOR_PUBKEY
    assert ft.fake.sent[1]["msg"] == knock_text()


def test_a_knock_refused_at_send_time_releases_its_claim_through_the_real_wiring(meshcore_daemon):
    """Fix round 3, item 2. The reviewer's precise finding: the test above
    calls `_enqueue_knock_release` directly, which pins that method's own
    thread-safety but pins NOTHING about what `_handle_knock` actually
    passes as `on_pretransmission_failure` -- rewiring `_handle_knock`
    back to `lambda reason: self._knock_cooldown.release(node_id, now)`
    (fix round 1's version, exactly the Critical fix round 2 fixed) would
    still pass every existing test, since none of them go through
    `_handle_knock`'s own callback wiring at all.

    This drives a real knock through the REAL event path (`emit_channel_
    message_from` -> `_process_meshcore_inbound` -> `_handle_knock` ->
    `_reserve_and_send` -> `Sender.enqueue`) and forces its reply to be
    refused at SEND time via `Sender._guarded_send`'s node-identity check
    (`Reject.NOT_REPLY_READY`, the same reason `test_identity_mismatch_
    at_send_time_...` in test_sender.py exercises for Meshtastic) --
    NEVER via `reply_ready=False`, which `decide_inbound`'s row 1b ALSO
    consults for ELIGIBILITY: setting that would make the knock rejected
    before it ever reaches `Outcome.KNOCK`/`_handle_knock` at all, so
    `channel_knocks` would never gain a row to release in the first place
    -- proving nothing about the callback wiring (caught by this test's
    own first draft: see task-3B-3-report.md's "Fix round 3").

    The stand-in `status()` below therefore reports a combination the REAL
    transport can no longer produce: `reply_ready=True` alongside a
    mismatched `actual_node_id`. As of the whole-branch review's finding 2,
    `MeshCoreTransport.status()` gates `reply_ready` on the configured
    `expected_node_id` exactly as `RadioAdapter._check_identity` does, so a
    genuinely misidentified MeshCore radio is now rejected at ELIGIBILITY
    and never reaches `_handle_knock` at all. That is the correct
    production behaviour and precisely why this test has to fabricate the
    state instead: its subject is `Sender._guarded_send`'s send-time
    identity check (`Reject.NOT_REPLY_READY`, the same one
    `test_identity_mismatch_at_send_time_...` exercises for Meshtastic),
    which remains as defence in depth, and the release callback it fires.
    Fabricating it keeps the test deterministic and race-free, since the
    mismatch holds regardless of exactly when `pump()` gets to the job.
    `Sender._mark_dropped` then calls `job.release_
    on_pretransmission_failure` FOR REAL, on the TX WORKER thread --
    exactly where the direct-release call raised in fix round 1. Verified
    empirically (see task-3B-3-report.md's "Fix round 3"): reverting
    `_handle_knock`'s callback to the direct call makes this test fail
    (the row survives, caught-and-swallowed by fix round 2's own
    hardening rather than crashing); restoring it makes this test pass
    again.
    """
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")

    orig_status = d._transport.status()
    d._transport.status = lambda: TransportStatus(
        connected=True,
        reply_ready=True,  # eligibility must still pass, so the claim is real
        # Wrong on purpose. The real transport would also report
        # reply_ready=False for this (finding 2), which is why the pair is
        # fabricated here: this test's subject is the send-time check.
        actual_node_id="!99999999",
        connected_seconds=orig_status.connected_seconds,
    )

    ft.fake.emit_channel_message_from(0, "visitor", "!garden")
    d.drain()
    # Task 3C finding 4 (test quality): `drain()`'s `_ingest.join()` waits
    # for the DB worker to finish processing the INBOUND channel message,
    # but `_enqueue_knock_release` is only called AFTER that -- from the TX
    # worker thread, once it pops and refuses the now-queued reply, which
    # can still be in flight when the first `drain()` call above returns
    # (observed racy at roughly 1 in 14 full-file runs). A second `drain()`
    # joins the ingest queue again, which now waits on the release event
    # `_enqueue_knock_release` puts there, so the DB worker has genuinely
    # applied it before the assertion below runs.
    d.drain()

    assert ft.fake.sent == []  # never actually transmitted

    row = d.conn.execute(
        "SELECT last_knock_at FROM channel_knocks WHERE node_id = ?", (VISITOR_NODE_ID,)
    ).fetchone()
    assert row is None  # released -- proves the REAL _handle_knock wiring, not a stand-in


def test_a_knock_refused_at_the_reserve_step_releases_its_claim(meshcore_daemon):
    """The reviewer's own suggested scenario (make the reserve step
    refuse, by having the sender already pending for that node), included
    alongside the test above for the exact synchronous failure mode it
    names: `_reserve_and_send`'s own immediate `ReplyQueue.reserve()`
    refusal, which -- unlike the async guard-refusal above -- fires
    synchronously on the DB worker thread (the same thread `_handle_knock`
    itself always runs on), so it is safe either way `_handle_knock`'s
    callback is wired; it is included here as a genuine positive case for
    this failure mode, not as the regression-catching test (that is the
    one above -- see this task's report for why the two are not
    interchangeable).

    Task 3C finding 1 changed WHAT occupies a knock's slot: a knock now
    reserves under a namespaced `reply_queue_key` (`f"knock:{node_id}"`,
    `is_knock=True`), never the plain resolved node id, precisely so it
    can no longer collide with that node's own genuine command. The
    blocker below is built with the SAME namespaced key and `is_knock=True`
    so it still forces the real knock's reserve() to refuse -- occupying
    the plain `VISITOR_NODE_ID` key (as a pre-3C version of this test did)
    would no longer collide with anything, now that the knock does not use
    that key.

    The slot is occupied by marking the key held directly, NOT by
    offering a real blocker `ReplyJob`: any job the live TX worker can
    pop is one it resolves within a single `_TX_LOOP_POLL_SECONDS` (0.1s)
    cycle, which releases the very slot this test needs held. A blocker
    with no `processed_requests` row -- as an earlier cut of this test
    used -- is dropped at `_guarded_send` step 2 (`get_state() !=
    "queued"` -> `Reject.DUPLICATE`), never reaching the global-cooldown
    soft defer at step 6 that the earlier version believed was holding
    it, and `drain()` clears `_global_tx_last` on every poll anyway. The
    knock then reserved and transmitted whenever the TX worker won that
    ~100ms race, which is exactly how this failed on CI. Seeding the
    pending sets leaves the TX worker nothing to pop, so the refusal is
    genuine (`_would_accept` sees the namespaced key held) and the
    outcome no longer depends on thread scheduling.
    """
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")

    knock_key = f"knock:{VISITOR_NODE_ID}"
    with d._queue._lock:
        d._queue._pending_senders.add(knock_key)
        d._queue._pending_knock_keys.add(knock_key)
    assert d._queue.reserve(knock_key, is_knock=True) is not None

    ft.fake.emit_channel_message_from(0, "visitor", "!garden")
    d.drain()

    assert ft.fake.sent == []  # the knock was refused at reserve(), never queued at all

    row = d.conn.execute(
        "SELECT last_knock_at FROM channel_knocks WHERE node_id = ?", (VISITOR_NODE_ID,)
    ).fetchone()
    assert row is None  # released


def test_meshcore_second_knock_within_the_cooldown_window_sends_nothing(meshcore_daemon):
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", "!garden")
    d.drain()
    assert len(ft.fake.sent) == 1

    # A distinct, FRESH sender_timestamp (rather than repeating the identical
    # wire message, or the default 0) gives this second knock its OWN
    # content-derived raw_id (meshcore_compat._derive_message_id) and keeps
    # it inside the freshness gate, so it is a genuinely new, non-stale
    # request from processed_requests' own (sender, packet_id) dedup's point
    # of view -- isolating the cooldown, not packet-content dedup or
    # staleness, as what blocks it.
    ft.fake.emit_channel_message_from(0, "visitor", "!garden", sender_timestamp=int(time.time()))
    d.drain()
    assert len(ft.fake.sent) == 1  # still one -- the second knock sent nothing


@pytest.mark.parametrize("first,second", [("!dm", "!garden"), ("!garden", "!DM")])
def test_channel_dm_alias_sends_same_invite_and_shares_cooldown(meshcore_daemon, first, second):
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", first)
    d.drain()
    assert len(ft.fake.sent) == 1
    assert ft.fake.sent[0]["dst"] == VISITOR_PUBKEY
    assert ft.fake.sent[0]["msg"] == knock_text()
    ft.fake.emit_channel_message_from(0, "visitor", second, sender_timestamp=int(time.time()))
    d.drain()
    assert len(ft.fake.sent) == 1


def test_meshcore_knock_after_the_cooldown_window_sends_again(meshcore_daemon):
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", "!garden")
    d.drain()
    assert len(ft.fake.sent) == 1

    d.advance_seconds(d.cfg.radio.meshcore_knock_cooldown_seconds + 1)
    # `d.advance_seconds` moves the Daemon's own clock (which `_meshcore_
    # message_sink` re-stamps `received_at` from) forward too, so the fresh
    # sender_timestamp must be read off THAT clock, not real wall time.
    ft.fake.emit_channel_message_from(
        0, "visitor", "!garden", sender_timestamp=int(d._clock.time())
    )
    d.drain()

    assert len(ft.fake.sent) == 2
    assert ft.fake.sent[1]["dst"] == VISITOR_PUBKEY
    assert ft.fake.sent[1]["msg"] == knock_text()


def test_meshcore_knock_cooldown_seconds_is_configurable(tmp_path):
    """Task 3B-3 item C: `[radio] meshcore_knock_cooldown_seconds` actually
    reaches the running cooldown (via `ChannelTriggerPolicy.cooldown_
    seconds`), not just `RadioConfig`'s own dataclass default."""
    cfg = _cfg(tmp_path, radio_kwargs={"meshcore_knock_cooldown_seconds": 5})
    d, ft = _start_meshcore_daemon(cfg)
    try:
        ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
        ft.fake.emit_channel_message_from(0, "visitor", "!garden")
        d.drain()
        assert len(ft.fake.sent) == 1

        ft.fake.emit_channel_message_from(
            0, "visitor", "!garden", sender_timestamp=int(time.time())
        )
        d.drain()
        assert len(ft.fake.sent) == 1  # still cooling down at the configured 5s

        d.advance_seconds(6)
        ft.fake.emit_channel_message_from(
            0, "visitor", "!garden", sender_timestamp=int(d._clock.time())
        )
        d.drain()
        assert len(ft.fake.sent) == 2  # past the configured window
    finally:
        d.stop()


def test_meshcore_knock_cooldown_survives_a_simulated_restart(tmp_path):
    """The whole reason `channel_knocks` is a table, not a dict on the
    Daemon: a restart (this test) or a crash loop must not reset the
    cooldown. Builds a genuinely SECOND `Daemon`/`FakeMeshCoreTransport`
    pair over the SAME database file -- not the same Python objects, which
    would prove nothing about persistence."""
    cfg = _cfg(tmp_path)

    d1, ft1 = _start_meshcore_daemon(cfg)
    ft1.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft1.fake.emit_channel_message_from(0, "visitor", "!garden")
    d1.drain()
    assert len(ft1.fake.sent) == 1
    d1.stop()

    d2, ft2 = _start_meshcore_daemon(cfg)
    try:
        # A fresh transport has an empty roster -- the resolved contact
        # must be re-taught to THIS process, exactly as a real restart
        # would need a fresh NEW_CONTACT/roster sync before it could ever
        # resolve the channel sender at all. The cooldown table, unlike
        # the roster, is not in-memory -- it is what this test is about.
        #
        # A fresh, distinct sender_timestamp (rather than d1's default 0)
        # gives this attempt its OWN content-derived raw_id, so it cannot be
        # blocked by processed_requests' ordinary (sender, packet_id) dedup
        # (which persists to the same db file too, but is a DIFFERENT, pre-
        # existing guarantee unrelated to this task), and keeps it inside
        # the freshness gate -- only `channel_knocks`' own persisted
        # cooldown can be responsible for the assertion below.
        ft2.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
        ft2.fake.emit_channel_message_from(
            0, "visitor", "!garden", sender_timestamp=int(time.time())
        )
        d2.drain()
        assert ft2.fake.sent == []  # still cooling down, in a brand-new process
    finally:
        d2.stop()


def test_meshcore_knock_from_an_unresolvable_sender_produces_nothing(meshcore_daemon):
    """scope-decisions.md section 7's last bullet: a claimed channel sender
    that resolves to no known contact is dropped silently -- no DM (there is
    no contact to address), and no channel reply."""
    d, ft = meshcore_daemon
    ft.fake.emit_channel_message_from(0, "ghost", "!garden")  # "ghost" is in no roster
    d.drain()
    assert ft.fake.sent == []


def test_meshcore_ordinary_channel_chatter_produces_no_send(meshcore_daemon):
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", "anyone been to the garden lately")
    d.drain()
    assert ft.fake.sent == []


def test_meshcore_channel_chatter_shaped_like_a_real_command_still_never_dispatches_it(
    meshcore_daemon,
):
    """`MENU` is a real, resolvable command word -- if it arrived as a DM it
    would dispatch and send the menu. On the channel it is not the knock
    token either (whole-body equality, transport-boundary-spec.md section
    3.1), so `decide_inbound` rejects it as ordinary chatter before
    `parse_command` ever sees it: no command output, and no knock either."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", "MENU")
    d.drain()
    assert ft.fake.sent == []


def test_channel_mode_replies_to_menu_on_the_same_channel(meshcore_channel_daemon):
    d, ft = meshcore_channel_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", "MENU")
    d.drain()

    assert ft.fake.sent == []
    assert len(ft.fake.channel_sent) == 1
    assert ft.fake.channel_sent[0]["channel_idx"] == 0
    assert "MeshGarden" in ft.fake.channel_sent[0]["msg"]
    assert d._sender._pending_retries == {}


def test_channel_mode_keeps_dm_commands_and_replies_private(meshcore_channel_daemon):
    d, ft = meshcore_channel_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MENU")
    d.drain()

    assert len(ft.fake.sent) == 1
    assert ft.fake.channel_sent == []


@pytest.mark.parametrize("text", ["MAIL", "INBOX", "MAIL ON", "HELP MAIL", "REPLY #1 hi"])
def test_mail_commands_remain_dm_only_in_channel_mode(meshcore_channel_daemon, text):
    d, ft = meshcore_channel_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", text)
    d.drain()

    assert ft.fake.sent == []
    assert ft.fake.channel_sent == []


@pytest.mark.parametrize(
    "text",
    ["SIGN hello", "UNSIGN", "POST bike hello", "ME", "CHECK", "PATH", "NEW", "BOOK fern"],
)
def test_identity_sensitive_or_stateful_commands_remain_dm_only_in_channel_mode(
    meshcore_channel_daemon, text
):
    d, ft = meshcore_channel_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", text)
    d.drain()

    assert ft.fake.sent == []
    assert ft.fake.channel_sent == []


def test_meshcore_knock_token_with_a_trailing_command_still_sends_nothing(meshcore_daemon):
    """A knock carries no payload (transport-boundary-spec.md section 3.1):
    `!garden MENU` is not a whole-body match for the token, so it is
    ordinary chatter, not a knock with an attached command -- there is no
    way to knock AND get a command to run in the same message."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", "!garden MENU")
    d.drain()
    assert ft.fake.sent == []


def test_meshcore_new_contact_feeds_the_nodes_table_for_name_resolution(meshcore_daemon):
    """Task 3B-2 item F: without this, collector.resolve_node's short-name
    lookup (collector.py ~417-422) finds nothing at all for a MeshCore
    install, because no MeshCore contact otherwise reaches `nodes`."""
    d, ft = meshcore_daemon
    full_pubkey = "aabbccdd" + "11" * 28
    ft.fake.emit_new_contact(full_pubkey, "gardener", adv_lat=12.5, adv_lon=-1.25)
    d.drain()

    node_id = "!" + full_pubkey[:8]
    row = d.conn.execute(
        "SELECT short_name, last_position_lat, last_position_lon FROM nodes WHERE node_id = ?",
        (node_id,),
    ).fetchone()
    assert row is not None
    assert row["short_name"] == "gardener"
    assert row["last_position_lat"] == 12.5
    assert row["last_position_lon"] == -1.25


def test_meshcore_bare_advertisement_does_not_populate_a_name_or_position(meshcore_daemon):
    """Item F, negative half: a bare ADVERTISEMENT carries only a public
    key (meshcore_transport.py's `_on_advertisement`) -- it records a reception
    without inventing name or position metadata."""
    d, ft = meshcore_daemon
    full_pubkey = "99887766" + "33" * 28
    ft.fake.emit_advertisement(full_pubkey)
    d.drain()

    node_id = "!" + full_pubkey[:8]
    row = d.conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
    assert row is not None
    assert row["short_name"] is None
    assert row["last_position_lat"] is None
    assert row["observation_count"] == 1


# --- wedged-run supervision (task 3B-2, "three lifecycle facts" #1) --------


class _StubMeshCoreClient:
    def __init__(self):
        self.stop_calls = 0
        self.connect_calls = 0
        self.send_advert_calls = 0

    def stop(self):
        self.stop_calls += 1

    def connect(self):
        self.connect_calls += 1

    def send_advert(self):
        self.send_advert_calls += 1
        return True


class _StubStatus:
    def __init__(self, connected):
        self.connected = connected
        self.reply_ready = connected
        self.actual_node_id = "!11223344"
        self.connected_seconds = 1.0 if connected else None


def test_check_meshcore_wedge_forces_a_fresh_reconnect_past_the_threshold(meshcore_daemon):
    """Simulates fact #1: the transport reports connected=False forever (a
    wedged disconnect() inside the run's own reconnect loop -- see
    task-3B-1-report.md's fix rounds 3/4). Before the threshold, this must
    do nothing (a normal backoff wait looks identical from the outside);
    past it, exactly one stop() THEN one connect(), never the reverse
    order and never more than once per continuous outage."""
    d, ft = meshcore_daemon
    stub = _StubMeshCoreClient()
    d._meshcore_client = stub
    d._transport.status = lambda: _StubStatus(connected=False)

    mono = {"t": 1_000.0}
    d._clock.monotonic = lambda: mono["t"]

    threshold = 120.0  # flat threshold, no longer scaled off meshcore_backoff_max_seconds

    d._check_meshcore_wedge()  # first observation: starts the clock, no action
    assert stub.stop_calls == 0

    mono["t"] += threshold - 1
    d._check_meshcore_wedge()  # still within the grace window
    assert stub.stop_calls == 0

    mono["t"] += 2  # now past the threshold
    d._check_meshcore_wedge()
    assert stub.stop_calls == 1
    assert stub.connect_calls == 1


def test_check_meshcore_wedge_is_a_no_op_once_reconnected(meshcore_daemon):
    d, ft = meshcore_daemon
    stub = _StubMeshCoreClient()
    d._meshcore_client = stub
    d._transport.status = lambda: _StubStatus(connected=False)
    d._check_meshcore_wedge()
    assert d._meshcore_wedge_since_monotonic is not None

    d._transport.status = lambda: _StubStatus(connected=True)
    d._check_meshcore_wedge()
    assert d._meshcore_wedge_since_monotonic is None
    assert stub.stop_calls == 0
    assert stub.connect_calls == 0


# --- task 3B-4 fix 2: periodic self-advertisement ---------------------------


def test_check_meshcore_advert_interval_repeats_after_the_configured_window(meshcore_daemon):
    """The periodic self-advertisement repeat (hardware finding: the garden
    was unreachable until manually flood-advertised) lives on the same
    maintenance tick `_check_meshcore_wedge` uses, above. Before the
    configured interval elapses this must do nothing (the connection-time
    advert already covers "just connected"); past it, exactly one
    `send_advert()` call, and the clock resets so it does not fire again
    every tick thereafter."""
    d, ft = meshcore_daemon
    stub = _StubMeshCoreClient()
    d._meshcore_client = stub

    mono = {"t": 1_000.0}
    d._clock.monotonic = lambda: mono["t"]

    interval = d.cfg.radio.meshcore_advert_interval_seconds
    d._check_meshcore_advert_interval()  # first observation: starts the clock, no send yet
    assert stub.send_advert_calls == 0

    mono["t"] += interval - 1
    d._check_meshcore_advert_interval()  # still within the window
    assert stub.send_advert_calls == 0

    mono["t"] += 2  # now past the interval
    d._check_meshcore_advert_interval()
    assert stub.send_advert_calls == 1

    d._check_meshcore_advert_interval()  # immediately again: clock just reset
    assert stub.send_advert_calls == 1


def test_check_meshcore_advert_interval_zero_disables_only_the_repeat(tmp_path):
    """`meshcore_advert_interval_seconds = 0` disables the periodic repeat
    only -- the per-connection advert lives entirely in
    `meshcore_client.py`/`fake_meshcore.py`, not in this method, so it is
    unaffected (see `test_a_meshcore_connection_sends_one_self_
    advertisement` below)."""
    cfg = _cfg(tmp_path, radio_kwargs={"meshcore_advert_interval_seconds": 0})
    d, ft = _start_meshcore_daemon(cfg)
    try:
        stub = _StubMeshCoreClient()
        d._meshcore_client = stub
        d._check_meshcore_advert_interval()
        d._clock.monotonic = lambda: 10_000_000.0
        d._check_meshcore_advert_interval()
        assert stub.send_advert_calls == 0
    finally:
        d.stop()


def test_a_meshcore_connection_sends_one_self_advertisement(tmp_path):
    """Connection half of fix 2: `FakeMeshCore.connect()` mirrors
    `RealMeshCoreClient._send_advert_on_connect` by sending one flood
    self-advertisement on every successful connection -- proven against the
    real `Daemon`/`FakeMeshCoreTransport` pipeline, not just the fake in
    isolation."""
    cfg = _cfg(tmp_path)
    d, ft = _start_meshcore_daemon(cfg)
    try:
        assert ft.fake.adverts_sent == 1
    finally:
        d.stop()


# ===========================================================================
# Task 3C: adversarial re-verification of the DM-only guarantee.
#
# Everything below attacks a seam earlier reviews did NOT cover. The map of
# what was already established (and is therefore deliberately NOT retested
# here) is in task-3C-report.md. Each test names the attack it pins, so a
# later weakening of the guarantee fails a test that says what was lost.
# ===========================================================================


# --- the garden's own identity ---------------------------------------------


def test_a_channel_knock_claiming_the_gardens_own_name_sends_nothing(meshcore_daemon):
    """Attack: make the garden knock on ITSELF. A MeshCore radio can hear
    its own advert relayed back (and `_remember_contact` mirrors any
    NEW_CONTACT/ADVERTISEMENT indiscriminately, including our own public
    key), so the garden's own display name CAN become resolvable in the
    roster. A channel message claiming that name resolves to our own node
    id -- and `decide_inbound`'s row 1 (`inb.sender == me` ->
    `Reject.SELF_ORIGIN`) is checked BEFORE the channel-knock branch, which
    is the only thing standing between this and the garden DM-ing itself
    once per cooldown window forever. If that row is ever moved below the
    channel branch, this test is what notices."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(SELF_PUBLIC_KEY, "meshgarden")
    ft.fake.emit_channel_message_from(0, "meshgarden", "!garden")
    d.drain()
    assert ft.fake.sent == []


def test_a_meshcore_direct_message_from_our_own_identity_sends_nothing(meshcore_daemon):
    """The MeshCore counterpart of `test_central_invariant.py::
    test_self_echo_causes_no_send`, which only ever covered Meshtastic. A
    DM whose `pubkey_prefix` is our own key derives `sender == me`."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(SELF_PUBLIC_KEY, "meshgarden")
    ft.fake.emit_direct_message(SELF_PUBLIC_KEY[:12], "MENU")
    d.drain()
    assert ft.fake.sent == []


# --- what a knock may cost the impersonated party --------------------------


def test_a_knock_naming_a_blocked_node_transmits_nothing_and_keeps_its_claim(meshcore_daemon):
    """A forged knock naming a BLOCKED node must transmit nothing: the
    knock path deliberately skips `_claim_and_gate` (and therefore its
    `is_blocked` gate), so the ONLY thing stopping a knock DM to a blocked
    node is `Sender._guarded_send`'s own step-4 block check. This pins that
    the two-layer arrangement actually holds end to end.

    It also pins the other half, which is easy to "fix" wrongly: the
    cooldown claim is NOT released for a blocked recipient (`_mark_dropped`
    withholds the release for `Reject.BLOCKED` alone). A blocked node is not
    owed a retry, and releasing here would let an attacker re-knock a
    blocked node every pump cycle forever."""
    d, ft = meshcore_daemon
    with d.conn:
        d.conn.execute(
            "INSERT INTO blocked_senders (node_id, blocked_at) VALUES (?, ?)",
            (VISITOR_NODE_ID, 0),
        )
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", "!garden")
    d.drain()

    assert ft.fake.sent == []
    row = d.conn.execute(
        "SELECT last_knock_at FROM channel_knocks WHERE node_id = ?", (VISITOR_NODE_ID,)
    ).fetchone()
    assert row is not None  # claimed and NOT released -- blocked is never released


def test_a_forged_knock_does_not_swallow_the_impersonated_nodes_own_genuine_command(
    meshcore_daemon,
):
    """TASK 3C FINDING 1, FIXED. Was: a forged channel knock enqueued its
    invite DM against the IMPERSONATED node's single `ReplyQueue` slot
    (`ReplyQueue` allowed at most one pending reply per sender, keyed on
    the plain node id). While that knock job was pending, the
    impersonated node's own genuine DM command was refused at
    `_reserve_and_send`'s `reserve()` step with `Reject.RATE_LIMITED` --
    no reply at all -- AND it had already burned a `processed_requests`
    dedup row plus one daily quota slot at `_claim_and_gate`, which runs
    first. That was a service denial imposed on an innocent third party by
    unauthenticated channel input: the same harm class fix round 1 item 1
    fixed for the shared `RateLimiter`, left unfixed for the shared
    `ReplyQueue` slot.

    Fixed by giving the knock's reply a namespaced `reply_queue_key`
    (`f"knock:{node_id}"`) instead of the plain node id -- see
    `_handle_knock`'s and `ReplyQueue`'s own docstrings. The knock's
    pending slot and the victim's own pending slot are now never the same
    key, so both can be genuinely in flight to the same node at once.

    Attack (composition of two individually safe behaviors, still worth
    keeping as a live test): the knock reuses `_reserve_and_send` --
    correct for truncation/TTL/ack reuse -- and `ReplyQueue` enforces one
    pending reply per KEY. Before the fix, composing them let the knock's
    reply, addressed to the victim, occupy the victim's own slot.

    The window is held open deterministically here by committing the global
    TX cooldown before the two messages are ingested, so the TX worker
    requeues (rather than resolves) the knock job for the whole of
    `_ingest.join()`. In production the same window is the queue latency
    plus up to `global_tx_cooldown_seconds`.

    Desired behavior, asserted below: the victim's genuine MENU still gets
    its reply. It now does."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    d.drain()

    # Hold every popped reply in requeue-limbo for the duration of the
    # ingest drain below (the same deterministic device
    # `test_a_knock_refused_at_the_reserve_step_releases_its_claim` uses).
    d._limiter.commit_global_tx()

    ft.fake.emit_channel_message_from(0, "visitor", "!garden")  # forged knock
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MENU")  # the victim's OWN command
    d.drain()

    bodies = [s["msg"] for s in ft.fake.sent]
    rows = d.conn.execute(
        "SELECT command, state, reason FROM processed_requests WHERE sender_node_id = ?",
        (VISITOR_NODE_ID,),
    ).fetchall()
    assert knock_text() in bodies
    assert any(b != knock_text() for b in bodies), (
        "the impersonated node's own genuine command got no reply: "
        f"sent={bodies}, request rows={[dict(r) for r in rows]}"
    )


def test_an_identical_knock_replayed_after_the_cooldown_expires_sends_nothing(meshcore_daemon):
    """The un-pinned half of `_handle_knock`'s dedup branch. Every existing
    cooldown test varies `sender_timestamp` specifically so the replay is a
    NEW request; nobody pinned what a byte-identical replay does once the
    cooldown has expired. Answer: `try_claim` succeeds (fresh window), then
    `RequestLog.claim` refuses (same content -> same derived raw_id) and the
    attempt is counted `deduplicated` -- so nothing is sent, and the
    re-claimed cooldown row is deliberately NOT released. That is a
    stricter bound on the recipient, never a looser one; this test exists so
    that a future edit which "helpfully" releases on the dedup path (and
    thereby makes a replay loop re-knockable) fails here."""
    d, ft = meshcore_daemon
    ts = int(time.time())  # fixed and FRESH: must stay identical across both emits below
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message_from(0, "visitor", "!garden", sender_timestamp=ts)
    d.drain()
    assert len(ft.fake.sent) == 1

    d.advance_seconds(d.cfg.radio.meshcore_knock_cooldown_seconds + 1)
    ft.fake.emit_channel_message_from(0, "visitor", "!garden", sender_timestamp=ts)  # identical
    d.drain()

    assert len(ft.fake.sent) == 1  # deduplicated, not re-knocked
    row = d.conn.execute(
        "SELECT last_knock_at FROM channel_knocks WHERE node_id = ?", (VISITOR_NODE_ID,)
    ).fetchone()
    assert row is not None  # re-claimed and kept: stricter, never looser


# --- trigger-half attacks the transport and the daemon each half-own -------


def test_a_channel_message_with_an_empty_claimed_name_sends_nothing(meshcore_daemon):
    """`_parse_sender_name` returns `""` -- not `None` -- for a body that
    begins with a colon, so `_on_channel_msg`'s `name is None` early return
    does NOT catch it; the empty string falls through to the roster lookup.
    It is safe only because `_remember_contact` refuses to index a falsy
    `adv_name`, so `""` can never be a key. Two layers, and each assumes
    something about the other: pin it."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message(0, ": !garden")
    ft.fake.emit_channel_message(0, "   : !garden")
    d.drain()
    assert ft.fake.sent == []


def test_a_direct_message_whose_text_is_the_channel_token_is_never_a_knock(meshcore_daemon):
    """A DM saying exactly `!garden` must take the DM row of
    `decide_inbound`, not the channel-knock row -- the branch is guarded on
    `inb.channel is ChannelScope.WATCHED`, and `_on_contact_msg` hardcodes
    `ChannelScope.DIRECT`. `!garden` is not a command, so the correct
    outcome is silence; what this pins is that it is never `knock_text()`
    and never claims a `channel_knocks` row."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "!garden")
    d.drain()

    assert knock_text() not in [s["msg"] for s in ft.fake.sent]
    row = d.conn.execute(
        "SELECT last_knock_at FROM channel_knocks WHERE node_id = ?", (VISITOR_NODE_ID,)
    ).fetchone()
    assert row is None


def test_an_advert_contact_and_ack_flood_transmits_nothing(meshcore_daemon):
    """The three event types that arrive unsolicited and in bulk on a real
    mesh -- ADVERTISEMENT, NEW_CONTACT and ACK -- drive roster/collector/
    delivery bookkeeping and must drive no transmission at all, however
    many of them arrive. An ACK whose code matches no outstanding send is
    included deliberately: `Sender.on_delivery`'s unrecognized-tx_ref path
    must stay a log line, never traffic."""
    d, ft = meshcore_daemon
    for i in range(40):
        ft.fake.emit_advertisement(f"{i:02x}" + "ff" * 31)
        ft.fake.emit_new_contact(f"{i:02x}" + "ee" * 31, f"n{i}")
        ft.fake.emit_ack(f"{i:08x}")
    d.drain()
    assert ft.fake.sent == []


# --- lifecycle: reconnect and restart --------------------------------------


def test_nothing_is_transmitted_in_the_freshness_window_after_a_reconnect(meshcore_daemon):
    """MeshCore's `Inbound.rx_time` is ALWAYS None, and a packet with no
    `sender_ts` either (the default here) has `connected_seconds` as its
    ONLY freshness signal (`_is_stale`'s last resort). `_on_connected`
    resets that clock on every reconnect, which is what makes a post-
    reconnect backlog -- messages the companion buffered while the link was
    down, replayed all at once -- unanswerable for `reply_ttl_seconds` when
    they carry no timestamp of their own. Both a DM and a knock are tried
    here: the knock path runs the same `_is_stale` gate, and must not be the
    one that got it wrong. See `test_a_fresh_sender_timestamp_is_answered_
    within_the_connected_grace_window_after_a_reconnect` below for the
    fix: a packet that DOES carry a `sender_timestamp` is no longer bound
    by this window at all."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.disconnect()
    ft.fake.connect()  # a genuine mid-session reconnect (fires CONNECTED)

    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MENU")
    ft.fake.emit_channel_message_from(0, "visitor", "!garden")
    d.drain()

    assert ft.fake.sent == []
    # ... and no cooldown row was burned for the victim either.
    row = d.conn.execute(
        "SELECT last_knock_at FROM channel_knocks WHERE node_id = ?", (VISITOR_NODE_ID,)
    ).fetchone()
    assert row is None


def test_a_fresh_sender_timestamp_is_answered_within_the_connected_grace_window_after_a_reconnect(
    meshcore_daemon,
):
    """The fix: real MeshCore DMs and channel messages carry `sender_
    timestamp`, and the radio's clock is now set from host time on every
    connection (see `docs/architecture.md`), so it is a meaningful
    freshness signal -- `_is_stale` now prefers it over `connected_seconds`
    when present. A fresh DM must therefore be answered immediately after a
    reconnect, unlike the timestamp-less case pinned just above, which is
    still bound by the `connected_seconds` grace window."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.disconnect()
    ft.fake.connect()  # a genuine mid-session reconnect (fires CONNECTED)

    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MENU", sender_timestamp=int(d._clock.time()))
    d.drain()

    assert len(ft.fake.sent) == 1
    assert ft.fake.sent[0]["dst"] == VISITOR_PUBKEY


def test_an_old_sender_timestamp_is_still_rejected_as_stale_after_a_reconnect(meshcore_daemon):
    """The trust boundary: `sender_timestamp` is the SENDER's own clock, so
    a genuinely old one (a backlog message the companion actually queued
    a long time ago and only now flushed) must still be rejected -- the
    fix only removes the blanket post-reconnect deaf window, it does not
    make every backlogged message answerable."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.disconnect()
    ft.fake.connect()  # a genuine mid-session reconnect (fires CONNECTED)

    old_ts = int(d._clock.time()) - d.cfg.service.reply_ttl_seconds - 1
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MENU", sender_timestamp=old_ts)
    d.drain()

    assert ft.fake.sent == []


def test_a_replayed_direct_message_is_never_answered_twice_across_a_restart(tmp_path):
    """Restart is the one place in-flight state is deliberately discarded
    (`RequestLog.resolve_uncertain_on_startup`, the in-memory tx_ref map,
    the roster mirror). What must NOT be discarded is the record that this
    exact request was already answered -- otherwise a companion radio that
    re-delivers its buffer after the daemon restarts earns a second reply
    to one physical message. MeshCore has no packet id and no receive
    timestamp, so `processed_requests` keyed on the CONTENT-derived
    `raw_id` is the only thing that can notice, and only because
    `_derive_message_id` is built from sender-side fields alone."""
    cfg = _cfg(tmp_path)
    ts = int(time.time())  # fixed and FRESH: must stay identical across both emits below
    d1, ft1 = _start_meshcore_daemon(cfg)
    ft1.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft1.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MENU", sender_timestamp=ts)
    d1.drain()
    assert len(ft1.fake.sent) == 1
    d1.stop()

    d2, ft2 = _start_meshcore_daemon(cfg)
    try:
        ft2.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
        # Byte-for-byte the same wire message: same prefix, same
        # sender_timestamp, same text -> the same derived raw_id.
        ft2.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MENU", sender_timestamp=ts)
        d2.drain()
        assert ft2.fake.sent == []
    finally:
        d2.stop()


def test_a_previously_known_contact_can_dm_and_knock_after_a_restart(tmp_path):
    """Whole-branch review, finding 1: the headline feature, in the most
    ordinary deployment there is.

    The radio has known this contact for weeks. After a daemon restart the
    library fires NOTHING for it -- connecting loads no contacts, and the
    NEW_CONTACT push only ever covers contacts the radio did NOT previously
    know -- so a roster mirrored purely from live events stays empty until
    that node happens to advertise again. Before the fix, the DM below got
    no reply at all (`MeshCoreUnknownContact` -> the reply lands `failed`)
    and the knock below was dropped silently with no cooldown row.

    The second daemon gets a genuinely fresh transport whose fake radio
    STORES the contact (`stored_contacts`) and fires no advert for it, so
    the only thing that can make either work is the stored-contact seeding
    `RealMeshCoreClient._seed_contacts` performs on every connection.
    """
    cfg = _cfg(tmp_path)
    d1, ft1 = _start_meshcore_daemon(cfg)
    ft1.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    d1.stop()

    d2, ft2 = _start_meshcore_daemon(cfg, stored_contacts=[(VISITOR_PUBKEY, "visitor")])
    try:
        # No emit_new_contact and no emit_advertisement anywhere below:
        # nothing but the seeding can put this contact in the roster.
        ft2.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MENU")
        d2.drain()
        assert len(ft2.fake.sent) == 1, "a DM from a previously known contact went unanswered"
        assert ft2.fake.sent[0]["dst"] == VISITOR_PUBKEY

        ft2.fake.emit_channel_message_from(0, "visitor", "!garden")
        d2.drain()
        assert len(ft2.fake.sent) == 2, "a knock from a previously known contact was dropped"
        assert ft2.fake.sent[1]["msg"] == knock_text()
        row = d2.conn.execute(
            "SELECT last_knock_at FROM channel_knocks WHERE node_id = ?", (VISITOR_NODE_ID,)
        ).fetchone()
        assert row is not None, "the knock left no cooldown row"
    finally:
        d2.stop()


def test_a_command_level_send_error_is_never_retransmitted(meshcore_daemon):
    """`commands.send_msg` can answer `EventType.ERROR` instead of
    MSG_SENT; `send_dm` then raises `MeshCoreSendFailed` having minted no
    tx_ref. `Sender._guarded_send` has already committed 'sending' at that
    point, so the request is terminal: the delayed retry applies only after
    a successful submission with a tx_ref, and further pumps must not re-send. Pinned at the DAEMON
    level because the existing transport-level test cannot see whether the
    daemon retries."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.commands.force_next_send_error()
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MENU")
    d.drain()
    assert ft.fake.sent == []
    d.drain()
    d.drain()
    assert ft.fake.sent == []  # never retried, in any later pump cycle


# --- malformed identity material (task 3C findings 2 and 3) ----------------

SHORT_KEY = "abcd"  # 2 bytes: too short for _meshcore_node_id's 4-byte minimum


def test_a_contact_whose_key_is_too_short_to_resolve_still_transmits_nothing(meshcore_daemon):
    """`meshcore_compat._meshcore_node_id` returns None for a public key
    under 4 bytes, and a truncated ADVERTISEMENT/NEW_CONTACT frame really
    can produce one (`reader.py` builds the key with `dbuf.read(32).hex()`,
    which short-reads rather than raising). Task 3C finding 3, now fixed:
    `_remember_contact` refuses to index such a key at all (see the test
    below for the full defect this closes), so a channel message naming
    "shorty" and a DM with a too-short prefix both resolve to nothing and
    never reach `Inbound` construction. This test pins the part that must
    never regress regardless: whatever a None-resolving contact might do,
    it never puts a frame on the air."""
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(SHORT_KEY, "shorty")
    ft.fake.emit_channel_message_from(0, "shorty", "!garden")
    ft.fake.emit_direct_message("abcdef", "MENU")  # 3-byte prefix: same unresolvable sender
    d.drain()
    assert ft.fake.sent == []


def test_a_contact_whose_key_is_too_short_never_reaches_the_knock_cooldown(meshcore_daemon):
    """TASK 3C FINDING 3, FIXED. Was: `_on_contact_msg`/`_on_channel_msg`
    only guarded against a FALSY key, never against `_meshcore_node_id`
    returning None for a key that is truthy but under 4 bytes -- so
    `sender=None` reached `decide_inbound`, which graded a channel message
    `Outcome.KNOCK`, and `_handle_knock` wrote a `channel_knocks` row with
    `node_id` NULL. Because SQLite lets a TEXT PRIMARY KEY hold many NULLs,
    `ON CONFLICT(node_id)` never matched, so the per-recipient knock
    cooldown was bypassed entirely and the table grew once per knock.

    Fixed in the transport, at the source: `_remember_contact` now refuses
    to index a pubkey `_meshcore_node_id` cannot resolve, so it never
    enters `_roster`/`_contacts_by_name` at all; `_on_contact_msg`/
    `_on_channel_msg` each also check the derived sender directly (belt
    and braces) before ever constructing an `Inbound`; and `Inbound.
    __post_init__` now raises on a falsy `sender`, so a null sender is
    unrepresentable rather than merely unlikely. Desired behavior, now
    real: a contact whose public key cannot produce a node id never enters
    the roster, so a channel message naming it is dropped inside the
    transport exactly like an unresolvable name -- no `Inbound`, no KNOCK
    outcome, and above all no `channel_knocks` row."""
    d, ft = meshcore_daemon
    for i in range(5):
        ft.fake.emit_new_contact(SHORT_KEY, f"shorty{i}")
        ft.fake.emit_channel_message_from(0, f"shorty{i}", "!garden", sender_timestamp=i)
    d.drain()

    assert ft.fake.sent == []
    rows = d.conn.execute("SELECT node_id FROM channel_knocks").fetchall()
    assert rows == [], f"unauthenticated channel input wrote {len(rows)} cooldown rows"


def test_a_burst_of_short_key_advertisements_writes_no_contact_rows(meshcore_daemon):
    """Fix round 2, item 1. `_remember_contact` (finding 3's fix) refused to
    index a public key too short to derive a node id from -- but
    `_on_new_contact` still re-derived the node id a SECOND time, ungated,
    purely to hand it to the daemon's contact callback, which reaches
    `Collector.record_contact_identity(None, ...)`. Its INSERT-OR-IGNORE
    never actually ignored anything there: SQLite permits unlimited NULLs
    in a TEXT PRIMARY KEY, so every short-key `NEW_CONTACT` event wrote
    another `nodes` row with `node_id = NULL` -- unbounded table growth
    from unauthenticated advertisement spam ALONE, with no channel message
    and no knock involved at all. The exact mechanism finding 3 was about,
    reached by a second, independent path finding 3's own fix missed.

    Fixed by hoisting the one derivation decision out of `_remember_
    contact` (it now returns the node id, or `None`) and having
    `_on_new_contact` act on THAT return value instead of re-deriving and
    forgetting to check a second one -- so there is exactly one decision,
    not two that can disagree."""
    d, ft = meshcore_daemon
    for i in range(5):
        ft.fake.emit_new_contact(SHORT_KEY, f"shorty{i}")
    d.drain()

    rows = d.conn.execute("SELECT node_id FROM nodes").fetchall()
    assert rows == [], f"unauthenticated advert spam wrote {len(rows)} contact rows"


def test_a_burst_of_forged_knocks_transmits_only_bounded_knock_dms(meshcore_daemon):
    """Attack: the attacker mints its own roster entries (adverts are
    unauthenticated and the companion's autoadd bit is what
    `meshcore_client.ensure_autoadd_bit` turns on) and then knocks once as
    each, to see whether volume can turn the knock into something other
    than one invite DM per claimed contact.

    It cannot, and that is what this pins: every transmission is still
    `knock_text()`, addressed to the claimed contact's own full public key,
    with never a channel frame.

    TASK 3C FINDING 2, FIXED. Was: the knocks that got queued occupied the
    SHARED `ReplyQueue` up to `max_pending_replies` (4 by default), so a
    genuine visitor's DM arriving in the same window was dropped
    `queue_full` with no reply -- each forged name is a different
    recipient, so the per-recipient cooldown never bounded this.

    Fixed with a SEPARATE, smaller cap (`[radio] meshcore_max_pending_
    knocks`, default 1) tracked independently of the per-key slot
    (`ReplyQueue._pending_knock_keys`, gated on `ReplyJob.is_knock`): no
    matter how many distinct attacker-minted contacts knock at once, at
    most `meshcore_max_pending_knocks` of their replies may be
    concurrently queued, leaving the rest of the shared queue's capacity
    free. Both halves are asserted below: the bound on knocks themselves,
    AND that a genuine visitor's DM arriving in the same burst still gets
    answered."""
    d, ft = meshcore_daemon
    names = []
    for i in range(20):
        name = f"bot{i}"
        names.append((name, f"{i:02x}" + "ee" * 31))
        ft.fake.emit_new_contact(names[-1][1], name)
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    d.drain()

    d._limiter.commit_global_tx()  # hold the TX worker so the queue really fills
    for name, _ in names:
        ft.fake.emit_channel_message_from(0, name, "!garden")
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MENU")  # a genuine visitor, same burst
    d.drain()

    by_name = dict(names)
    knock_sends = [s for s in ft.fake.sent if s["msg"] == knock_text()]
    genuine_sends = [s for s in ft.fake.sent if s["dst"] == VISITOR_PUBKEY]

    assert knock_sends, "the knock path stopped working entirely"
    assert len(knock_sends) <= d.cfg.radio.meshcore_max_pending_knocks
    for sent in knock_sends:
        assert sent["dst"] in by_name.values()  # a full contact public key: a DM, not a channel

    # The fix itself: a genuine visitor's DM in the same burst is NOT
    # starved out by the knock flood, unlike before this finding was fixed.
    assert len(genuine_sends) == 1
    assert genuine_sends[0]["msg"] != knock_text()


def test_zero_relay_dm_populates_direct_topology(meshcore_daemon):
    daemon, transport = meshcore_daemon
    transport.fake.emit_direct_message(VISITOR_PUBKEY[:12], "hello", path_len=0)
    daemon.drain()
    rows = daemon.conn.execute("SELECT a_node_id, b_node_id FROM links").fetchall()
    assert [tuple(row) for row in rows] == [(VISITOR_NODE_ID, "!11223344")]


def test_health_gauges_are_populated_at_startup(tmp_path):
    from meshgarden.collector import Collector
    from meshgarden.database import connect, migrate
    from meshgarden.models import MeshCoreObservation

    cfg = _cfg(tmp_path)
    conn = connect(cfg.database.path)
    migrate(conn)
    Collector(conn).record_meshcore(MeshCoreObservation(
        received_at=int(time.time()), sender=VISITOR_NODE_ID, recipient="!11223344",
        packet_id=None, kind="MESHCORE_ADVERT",
    ))
    conn.close()
    daemon, transport = _start_meshcore_daemon(cfg)
    try:
        # No maintenance tick or new packet is needed after a restart.
        assert daemon._metrics.radio_connected._value.get() == 1
        assert daemon._metrics.distinct_senders_1h._value.get() == 1
        assert daemon._metrics.distinct_senders_24h._value.get() == 1
    finally:
        daemon.stop()


@pytest.mark.parametrize("name", ["visitor", "🌀" * 30])
def test_channel_cooldown_notice_is_named_bounded_and_independent_of_dm(
    meshcore_channel_daemon, name
):
    d, ft = meshcore_channel_daemon
    base = int(d._clock.time()) - 10
    ft.fake.emit_new_contact(VISITOR_PUBKEY, name)
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MENU", sender_timestamp=base + 1)
    d.drain()
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "PING", sender_timestamp=base + 2)
    d.drain()
    assert len(ft.fake.sent) == 2
    assert "Please wait" in ft.fake.sent[-1]["msg"]
    started = d._limiter._command_last[VISITOR_NODE_ID]
    ft.fake.emit_channel_message_from(0, name, "STATS", sender_timestamp=base + 3)
    d.drain()
    assert len(ft.fake.channel_sent) == 1
    notice = ft.fake.channel_sent[-1]
    assert notice["channel_idx"] == 0
    assert notice["msg"].startswith("⏳ " + name[:1])
    assert "wait " in notice["msg"]
    assert notice["msg"].endswith("s, then resend your command.")
    assert len(notice["msg"].encode("utf-8")) <= ft.max_text_bytes
    assert d._limiter._command_last[VISITOR_NODE_ID] == started
    for stamp in (3, 4):
        ft.fake.emit_channel_message_from(0, name, "STATS", sender_timestamp=base + stamp)
        d.drain()
    assert len(ft.fake.channel_sent) == 1
    assert len(ft.fake.sent) == 2
    assert all(entry.job.reply_channel_idx is None
               for entry in d._sender._pending_retries.values())
    d.advance_seconds(1801)
    base = int(d._clock.time()) - 10
    ft.fake.emit_channel_message_from(0, name, "PING", sender_timestamp=base + 5)
    d.drain()
    ft.fake.emit_channel_message_from(0, name, "STATS", sender_timestamp=base + 6)
    d.drain()
    assert len(ft.fake.channel_sent) == 3
    assert "wait " in ft.fake.channel_sent[-1]["msg"]


def test_channel_send_failure_logs_exception_type(meshcore_channel_daemon, monkeypatch, caplog):
    d, ft = meshcore_channel_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")

    def fail(*args):
        raise TimeoutError("private payload must not be logged")

    monkeypatch.setattr(ft, "send_channel", fail)
    ft.fake.emit_channel_message_from(0, "visitor", "MENU")
    d.drain()
    assert "exception=TimeoutError" in caplog.text
    assert "private payload must not be logged" not in caplog.text
    assert ft.fake.channel_sent == []
    assert d._sender._pending_retries == {}


def test_other_channel_chatter_is_not_written_to_the_exchange_trace(meshcore_daemon, monkeypatch):
    """Public-channel chatter from a known contact is rejected as BROADCAST
    (not for us) but must not reach the exchange trace, which exists for
    DMs and the watched channel; a BROADCAST rejection ON the watched
    channel (dm_only mode) still does, since that is the garden's own room."""
    from meshgarden import exchange_log

    d, ft = meshcore_daemon
    seen: list[tuple] = []
    monkeypatch.setattr(exchange_log, "log_rejection", lambda *a, **k: seen.append((a, k)))
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_channel_message(5, "visitor: hello everyone")  # not the watched channel (0)
    d.drain()
    assert all(a[1] != "broadcast" for a, _k in seen)
    ft.fake.emit_channel_message(0, "visitor: hello garden")  # watched channel, dm_only mode
    d.drain()
    assert any(a[1] == "broadcast" for a, _k in seen)


# --- transport-qualified ids in a shared bbs.db ------------------------------


@pytest.fixture
def shared_meshcore_daemon(tmp_path):
    d, ft = _start_meshcore_daemon(_cfg(tmp_path, bbs_path=str(tmp_path / "bbs.db")))
    yield d, ft
    d.stop()


def test_meshcore_sign_stores_qualified_sender(shared_meshcore_daemon):
    d, ft = shared_meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "SIGN hello from meshcore")
    d.drain()
    assert [tuple(r) for r in d.conn.execute("SELECT sender_node_id FROM guestbook")] == [
        ("mc:" + VISITOR_NODE_ID,)
    ]


def test_meshcore_sign_without_shared_bbs_keeps_bare_sender(meshcore_daemon):
    d, ft = meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "SIGN hello from meshcore")
    d.drain()
    assert [tuple(r) for r in d.conn.execute("SELECT sender_node_id FROM guestbook")] == [
        (VISITOR_NODE_ID,)
    ]


def test_meshcore_blocked_sender_qualified_is_blocked(shared_meshcore_daemon):
    """A block on the Meshtastic node with the same bare id must not touch
    the MeshCore node, and the MeshCore block must hold."""
    d, ft = shared_meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    d.conn.execute("INSERT INTO blocked_senders(node_id, blocked_at) VALUES (?, 0)",
                   (VISITOR_NODE_ID,))
    d.conn.commit()
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MENU")
    d.drain()
    assert len(ft.fake.sent) == 1

    d.conn.execute("INSERT INTO blocked_senders(node_id, blocked_at) VALUES (?, 0)",
                   ("mc:" + VISITOR_NODE_ID,))
    d.conn.commit()
    d.advance_seconds(d.cfg.service.per_node_cooldown_seconds + 1)
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "PING")
    d.drain()
    assert len(ft.fake.sent) == 1


def test_meshcore_lab_unlock_qualified(shared_meshcore_daemon):
    """A Meshtastic node's LAB unlock never unlocks the same bare id here,
    and a MeshCore unlock is stored qualified."""
    d, ft = shared_meshcore_daemon
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    d.conn.execute("INSERT INTO lab_nodes(node_id, unlocked_at) VALUES (?, 0)",
                   (VISITOR_NODE_ID,))
    d.conn.commit()
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "BOARDS")
    d.drain()
    assert ft.fake.sent == []  # still locked: LAB commands answer like UNKNOWN

    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], LAB_UNLOCK_WORD)
    d.drain()
    assert {r[0] for r in d.conn.execute("SELECT node_id FROM lab_nodes")} == {
        VISITOR_NODE_ID, "mc:" + VISITOR_NODE_ID,
    }
    d.advance_seconds(d.cfg.service.per_node_cooldown_seconds + 1)
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "BOARDS")
    d.drain()
    assert len(ft.fake.sent) == 2  # unlock reply, then BOARDS answered
    d.advance_seconds(d.cfg.service.per_node_cooldown_seconds + 1)
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "MAIL ON")
    d.drain()
    assert [tuple(r) for r in d.conn.execute("SELECT node_id, enabled FROM mail_prefs")] == [
        ("mc:" + VISITOR_NODE_ID, 1)
    ]


def _seed_name_spoof(conn, entries):
    conn.execute("INSERT INTO nodes(node_id, short_name, long_name, observed_first_seen, "
                 "observed_last_seen) VALUES ('!a1d3db88', 'Evil', 'Evil', 0, 0)")
    for sender, name, text in entries:
        conn.execute(
            "INSERT INTO guestbook(created_at, sender_node_id, display_name, message, "
            "message_hash, visible) VALUES (0, ?, ?, ?, ?, 1)", (sender, name, text, text))
    conn.commit()


def test_meshcore_guestbook_never_names_a_meshtastic_entry_from_its_nodes(
    shared_meshcore_daemon,
):
    """`nodes` here is MeshCore's: its `!a1d3db88` is not the Meshtastic
    signer with that bare id, so the stored snapshot name must be shown."""
    d, ft = shared_meshcore_daemon
    d.conn.execute("INSERT INTO nodes(node_id, short_name, long_name, observed_first_seen, "
                   "observed_last_seen) VALUES ('!c0ffee00', 'Carol', 'Carol', 0, 0)")
    _seed_name_spoof(d.conn, [("!a1d3db88", "Alice", "hi from alice"),
                              ("mc:!c0ffee00", "old", "hi from carol")])
    ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
    ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], "GUESTBOOK")
    d.drain()
    reply = ft.fake.sent[-1]["msg"]
    assert "Alice" in reply and "Evil" not in reply
    assert "Carol" in reply  # this mesh's own entry still gets its current name
