"""Opt-in mail notices (MAIL NOTIFY ON): throttling and the daemon path."""

import time

import pytest

from meshgarden import mail_notify
from meshgarden.database import connect, migrate
from meshgarden.mail import Mail, MailPrefs

NOW = 1_800_000_000
DAY = "2027-01-15"


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "o.db"))
    migrate(c)
    yield c
    c.close()


def _due(conn, now=NOW, day=DAY, interval=1800, cap=6):
    return mail_notify.due(conn, mailbox_of=lambda n: n, now=now, day=day,
                           interval_seconds=interval, daily_cap=cap)


def test_opting_in_never_announces_notes_already_waiting(conn):
    Mail(conn).send("!aaaa0001", "!bbbb0002", "old", NOW - 100)
    mail_notify.set_enabled(conn, "!bbbb0002", True, NOW, mailbox="!bbbb0002")
    assert _due(conn) == []
    Mail(conn).send("!aaaa0001", "!bbbb0002", "new", NOW + 1)
    [(node, pending, newest)] = _due(conn)
    assert (node, pending) == ("!bbbb0002", 2) and newest > 0


def test_interval_daily_cap_and_off(conn):
    mail_notify.set_enabled(conn, "!bbbb0002", True, NOW, mailbox="!bbbb0002")
    mail = Mail(conn)
    for i in range(3):
        mail.send("!aaaa0001", "!bbbb0002", f"n{i}", NOW + i)
        [(node, _, newest)] = _due(conn, now=NOW + i * 2000)
        mail_notify.record_sent(conn, node, newest, NOW + i * 2000, DAY)
        # Nothing newer: no second notice, even after the interval.
        assert _due(conn, now=NOW + i * 2000 + 1900) == []
    mail.send("!aaaa0001", "!bbbb0002", "more", NOW + 9000)
    assert _due(conn, now=NOW + 4000 + 100) == []  # inside the interval
    assert _due(conn, now=NOW + 9000, cap=3) == []  # cap reached today
    assert len(_due(conn, now=NOW + 9000, day="2027-01-16", cap=3)) == 1  # new day
    mail_notify.set_enabled(conn, "!bbbb0002", False, NOW + 9001, mailbox="!bbbb0002")
    assert _due(conn, now=NOW + 99999, day="2027-01-17") == []


def test_notice_names_no_sender_or_text():
    text = mail_notify.notice_text(1)
    assert text.startswith("📬 1 note waiting") and len(text.encode()) <= 120


@pytest.fixture
def notify_daemon(tmp_path, monkeypatch):
    from meshgarden import main as main_mod
    from meshgarden.main import Daemon
    from tests.conftest import _cfg
    from tests.fakes import FakeTransport

    # Maintenance on every DB-worker poll, so the notice goes out promptly.
    monkeypatch.setattr(main_mod, "_MAINTENANCE_INTERVAL_SECONDS", 0)
    cfg = _cfg(tmp_path)
    ft = FakeTransport(0x11223344, cfg.radio.expected_node_id, service_cfg=cfg.service)
    d = Daemon(cfg, transport=ft, start_metrics=False)
    d.iface = ft.iface
    d.start()
    yield d
    d.stop()


def _dm(daemon, text, packet_id, from_num=0xAABBCCDD):
    from tests.fakes import make_packet

    daemon.iface.emit_packet(make_packet(
        to_num=daemon.my_node_num, from_num=from_num, text=text, packet_id=packet_id))
    daemon.drain()
    daemon.advance_seconds(60)
    return daemon.iface.sent[-1]["text"]


def _wait_for_notice(daemon, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for sent in daemon.iface.sent:
            if "waiting" in sent["text"]:
                return sent
        # drain() lifts the global TX cooldown the injected clock never ends.
        daemon.drain(timeout=0.2)
        time.sleep(0.05)
    return None


def test_daemon_sends_one_notice_by_dm_through_the_sender(notify_daemon):
    d = notify_daemon
    assert "Mail notices ON" in _dm(d, "MAIL NOTIFY ON", 9701)
    MailPrefs(d.conn).set("!aabbccdd", True, NOW)
    Mail(d.conn).send("!0badf00d", "!aabbccdd", "secret words", int(time.time()))
    d.conn.commit()
    notice = _wait_for_notice(d)
    assert notice is not None
    assert notice["destinationId"] in ("!aabbccdd", 0xAABBCCDD)
    assert "secret words" not in notice["text"] and "0badf00d" not in notice["text"]
    assert "1 note waiting" in notice["text"]
    time.sleep(0.5)  # several more maintenance ticks: still only one notice
    assert sum("waiting" in s["text"] for s in d.iface.sent) == 1


def test_daemon_without_opt_in_or_with_operator_off_sends_nothing(tmp_path, monkeypatch):
    from dataclasses import replace

    from meshgarden import main as main_mod
    from meshgarden.main import Daemon
    from tests.conftest import _cfg
    from tests.fakes import FakeTransport

    monkeypatch.setattr(main_mod, "_MAINTENANCE_INTERVAL_SECONDS", 0)
    cfg = _cfg(tmp_path)
    cfg = replace(cfg, service=replace(cfg.service, mail_notify_enabled=False))
    ft = FakeTransport(0x11223344, cfg.radio.expected_node_id, service_cfg=cfg.service)
    d = Daemon(cfg, transport=ft, start_metrics=False)
    d.iface = ft.iface
    d.start()
    try:
        assert "off on this garden" in _dm(d, "MAIL NOTIFY ON", 9711)
        MailPrefs(d.conn).set("!aabbccdd", True, NOW)
        Mail(d.conn).send("!0badf00d", "!aabbccdd", "hi", int(time.time()))
        d.conn.commit()
        assert _wait_for_notice(d, timeout=1.0) is None
    finally:
        d.stop()
