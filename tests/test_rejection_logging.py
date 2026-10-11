import time

import pytest

from meshgarden import ops
from meshgarden.main import Daemon
from tests.fake_meshcore import FakeMeshCoreTransport
from tests.test_daemon_meshcore import LAB_UNLOCK_WORD, VISITOR_NODE_ID, VISITOR_PUBKEY, _cfg


@pytest.mark.parametrize("debug", [False, True])
def test_debug_explains_lab_cooldown_duplicates_and_stale_messages(tmp_path, capsys, debug):
    cfg = _cfg(tmp_path, per_node_cooldown_seconds=10)
    radio = FakeMeshCoreTransport()
    radio._connected_since_monotonic -= 1000
    daemon = Daemon(cfg, transport=radio, start_metrics=False, debug=debug)
    try:
        daemon.start()
        radio.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")

        def send(text, **kwargs):
            radio.fake.emit_direct_message(VISITOR_PUBKEY[:12], text, **kwargs)
            daemon.drain()

        send("BOARDS")  # LAB-gated; this visitor is locked.
        send("not a command")
        send("STATS")
        send("HAIKU")
        assert len(radio.fake.sent) == 2
        assert "then resend your command" in radio.fake.sent[-1]["msg"]
        daemon.advance_seconds(11)
        send("STATS")  # Same wire message, beyond the per-node cooldown.
        send("HAIKU", sender_timestamp=int(time.time()) - 1000)
        send(LAB_UNLOCK_WORD)
        assert len(radio.fake.sent) == 3  # STATS, cooldown notice, unlock acknowledgement.
    finally:
        daemon.stop()
    output = capsys.readouterr().err
    assert LAB_UNLOCK_WORD not in output
    reasons = (
        "lab_locked", "unknown_command_or_invalid_arguments", "command_cooldown",
        "duplicate_request", "stale",
    )
    if debug:
        for reason in reasons:
            assert f"reason={reason}" in output
        assert (
            f"rejected from=visitor ({VISITOR_NODE_ID}) "
            "cmd=HAIKU reason=command_cooldown"
        ) in output
        assert "cooldown_seconds=10" in output
        with open(ops.exchange_trace_path(cfg)) as trace_file:
            trace = trace_file.read()
        assert "reason=lab_locked" in trace
        assert LAB_UNLOCK_WORD not in trace
    else:
        assert "rejected from=" not in output


@pytest.mark.parametrize("debug", [False, True])
def test_debug_explains_quota_and_blocked_sender(tmp_path, capsys, debug):
    cfg = _cfg(tmp_path, daily_command_quota=1, per_node_cooldown_seconds=1)
    radio = FakeMeshCoreTransport()
    radio._connected_since_monotonic -= 1000
    daemon = Daemon(cfg, transport=radio, start_metrics=False, debug=debug)
    try:
        daemon.start()
        radio.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
        radio.fake.emit_direct_message(VISITOR_PUBKEY[:12], "STATS")
        daemon.drain()
        daemon.advance_seconds(2)
        radio.fake.emit_direct_message(VISITOR_PUBKEY[:12], "HAIKU")
        daemon.drain()
        with daemon.conn:
            daemon.conn.execute(
                "INSERT INTO blocked_senders(node_id, blocked_at) VALUES (?, ?)",
                (VISITOR_NODE_ID, int(time.time())),
            )
        radio.fake.emit_direct_message(VISITOR_PUBKEY[:12], "ICHING")
        daemon.drain()
        assert len(radio.fake.sent) == 2
        assert "Daily command quota reached" in radio.fake.sent[-1]["msg"]
    finally:
        daemon.stop()
    output = capsys.readouterr().err
    if debug:
        assert "cmd=HAIKU reason=quota" in output
        assert "cmd=ICHING reason=blocked" in output
    else:
        assert "rejected from=" not in output


def test_debug_explains_send_failure_and_unknown_channel_sender(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    radio = FakeMeshCoreTransport()
    radio._connected_since_monotonic -= 1000
    daemon = Daemon(cfg, transport=radio, start_metrics=False, debug=True)
    try:
        daemon.start()
        radio.fake.emit_channel_message_from(0, "unrecognized visitor", "!dm")
        radio.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
        radio.fake.commands.force_next_send_error()
        radio.fake.emit_direct_message(VISITOR_PUBKEY[:12], "HAIKU")
        daemon.drain()
    finally:
        daemon.stop()
    output = capsys.readouterr().err
    assert "cmd=KNOCK reason=unknown_sender_name" in output
    with open(ops.exchange_trace_path(cfg)) as trace_file:
        assert "unrecognized visitor" not in trace_file.read()
    assert "cmd=HAIKU reason=send_exception" in output
