"""Regression coverage for identity, delivery ordering, mail, and midnight rollups."""

from datetime import UTC, datetime, timedelta

import pytest

from meshgarden.commands import Command, RequestLog, mail_page
from meshgarden.database import connect, migrate
from meshgarden.identity import bind_meshcore_identity
from meshgarden.mail import Mail
from meshgarden.meshcore_transport import MeshCoreTransport, MeshCoreUnknownContact
from meshgarden.transport import Delivery, DeliveryOutcome
from tests.fake_meshcore import FakeMeshCore
from tests.test_retention import mk
from tests.test_sender import Harness


def test_colliding_contact_keys_never_redirect_replies_or_commands():
    client = FakeMeshCore()
    transport = MeshCoreTransport(client, expected_node_id='!11223344')
    received = []
    transport.on_message(received.append)
    transport.connect()
    first, second = 'aabbccdd1111' + '00' * 26, 'aabbccdd2222' + '00' * 26
    client.emit_new_contact(first, 'Alice')
    client.emit_new_contact(second, 'Bob')
    client.emit_direct_message(first[:12], 'MENU')
    client.emit_direct_message(second[:12], 'MAIL')
    assert received == []
    assert not transport.identity_is_unambiguous('!aabbccdd')
    with pytest.raises(MeshCoreUnknownContact):
        transport.send_dm('!aabbccdd', 'private')
    assert client.sent == []
    # Re-advertising the original cannot silently remove quarantine.
    client.emit_new_contact(first, 'Alice')
    assert not transport.identity_is_unambiguous('!aabbccdd')


def test_identity_binding_and_quarantine_survive_restarts(tmp_path):
    path = str(tmp_path / 'identity.db')
    conn = connect(path)
    migrate(conn)
    conn.close()
    first, second = 'aabbccdd1111' + '00' * 26, 'aabbccdd2222' + '00' * 26
    assert bind_meshcore_identity(path, '!aabbccdd', first)
    assert bind_meshcore_identity(path, '!aabbccdd', first.upper())
    # A fresh connection encounters a replacement key after a restart.
    assert not bind_meshcore_identity(path, '!aabbccdd', second)
    assert not bind_meshcore_identity(path, '!aabbccdd', first)
    conn = connect(path)
    row = conn.execute('SELECT * FROM meshcore_identity_bindings').fetchone()
    assert row['key_hash'] not in (first, second)
    conn.close()


@pytest.mark.parametrize('ref', ['123456', 'abcdef12'])
def test_ack_before_send_returns_is_settled_on_db_worker(tmp_path, ref):
    h = Harness(tmp_path)
    try:
        job = h.make_queued_job()

        def send(to, text):
            h.sender.on_delivery(Delivery(tx_ref=ref, outcome=DeliveryOutcome.ACKED))
            return ref

        h.transport.send_dm = send
        h.sender.enqueue(job)
        h.sender.pump()
        assert h.request_log.get_state(job.request_sender, job.request_packet_id) == 'submitted'
        h.sender.drain_deliveries()
        assert h.request_log.get_state(job.request_sender, job.request_packet_id) == 'acked'
        h.sender.on_delivery(Delivery(tx_ref=ref, outcome=DeliveryOutcome.ACKED))
        assert h.request_log.get_state(job.request_sender, job.request_packet_id) == 'acked'
    finally:
        h.stop()


@pytest.mark.parametrize('budget', [40, 120, 180])
def test_partial_mail_ack_preserves_every_unseen_character(tmp_path, budget):
    conn = connect(str(tmp_path / 'mail.db'))
    migrate(conn)
    mail = Mail(conn)
    original = 'é' * 90
    mail.send('!aabbccdd', '!11223344', original, 1000)
    delivered = ''
    while mail.pending_count('!11223344'):
        pending = mail.pending_for('!11223344')
        text, receipts = mail_page(pending, now=1010, max_bytes=budget)
        assert len(text.encode()) <= budget
        assert receipts
        entry_id, snapshot, count = receipts[0]
        assert count > 0
        chunk = snapshot[:count]
        assert chunk in text
        delivered += chunk
        # No ACK yet: failed/expired replies must leave this original untouched.
        assert mail.pending_for('!11223344')[0].message == snapshot
        mail.consume_delivered(receipts)
        after = mail.pending_for('!11223344')
        mail.consume_delivered(receipts)  # overlapping reads/duplicate ACKs are idempotent
        assert mail.pending_for('!11223344') == after
        if count < len(snapshot):
            assert 'More: MAIL' in text
            assert after[0].message == snapshot[count:]
            assert after[0].id == entry_id
    assert delivered == original
    conn.close()


def test_tiny_mail_budget_does_not_consume_a_note(tmp_path):
    conn = connect(str(tmp_path / 'mail.db'))
    migrate(conn)
    mail = Mail(conn)
    mail.send('!aabbccdd', '!11223344', '🌱', 1000)
    body, receipts = mail_page(mail.pending_for('!11223344'), now=1000, max_bytes=1)
    assert len(body.encode()) <= 1
    assert receipts == []
    assert mail.pending_count('!11223344') == 1
    conn.close()


def test_midnight_rollup_waits_for_pending_requests_and_retains_sources(tmp_path):
    conn, retention = mk(tmp_path)
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    day = (midnight - timedelta(days=1)).strftime('%Y-%m-%d')
    ts = int(midnight.timestamp())
    requests = RequestLog(conn, dedup_hours=24)
    requests.claim('!aabbccdd', 1, ts - 1, Command.MENU)
    requests.set_state('!aabbccdd', 1, 'queued', ts - 1)
    requests.claim('!aabbccdd', 2, ts - 86000, Command.MENU)
    requests.set_state('!aabbccdd', 2, 'dropped', ts - 86000)
    retention.rollup_day(day)
    assert conn.execute('SELECT finalized FROM daily_stats WHERE day=?', (day,)).fetchone()[0] == 0
    # Expired terminal rows still contribute to this unfinished day's totals.
    assert retention.prune_processed_requests(ts + 500) == 0
    requests.set_state('!aabbccdd', 1, 'sending', ts + 10)
    requests.set_state('!aabbccdd', 1, 'submitted', ts + 10, tx_packet_id=42)
    requests.set_state('!aabbccdd', 1, 'acked', ts + 11)
    retention.rollup_day(day)
    row = conn.execute('SELECT * FROM daily_stats WHERE day=?', (day,)).fetchone()
    assert (row['finalized'], row['replies_sent'], row['replies_acked']) == (1, 1, 1)
    assert row['commands_rejected'] == 1
    assert retention.prune_processed_requests(ts + 500) == 1
    conn.close()
