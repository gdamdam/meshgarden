"""Daily MeshCore contact-table housekeeping, end to end over the fake radio."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

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
from meshgarden.weather import WeatherBatch
from tests.fake_meshcore import FakeMeshCoreTransport

TZ = "America/Los_Angeles"
A = "aaaaaaaa" + "11" * 28  # sends a command -> protected
B = "bbbbbbbb" + "22" * 28  # stale
C = "cccccccc" + "33" * 28  # recent


def _at(h, m, day=10):
    return int(datetime(2026, 10, day, h, m, tzinfo=ZoneInfo(TZ)).timestamp())


def _daemon(tmp_path, **radio):
    cfg = Config(
        radio=RadioConfig(device="/dev/fake", expected_node_id="!11223344", transport="meshcore",
                          meshcore_contact_target=2, **radio),
        service=ServiceConfig(lab_unlock_word="SUPERGARDENME", timezone=TZ),
        database=DatabaseConfig(path=str(tmp_path / "o.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "backups")),
        metrics=MetricsConfig(),
    )
    ft = FakeMeshCoreTransport()
    ft._connected_since_monotonic -= 1000.0
    d = Daemon(cfg, transport=ft, start_metrics=False, weather_poller=False)
    d.start()
    now = _at(4, 30)
    ft.fake.emit_new_contact(A, "alpha")
    ft.fake.emit_new_contact(B, "bravo")
    ft.fake.add_contact(B, "bravo", 1, now - 30 * 86400)
    ft.fake.emit_new_contact(C, "charlie")
    ft.fake.add_contact(C, "charlie", 1, now - 3600)
    ft.fake.emit_direct_message(A[:12], "MENU")
    d.drain()
    return d, ft


def _maintain(d, now):
    """Borrow the DB worker (it owns the connection) via the batch hook."""
    d._process_weather_batch = lambda _b: d._run_maintenance(now=now)
    d._ingest.put_nowait(WeatherBatch(fetched_at=now, result="error", products=()))
    d.drain()


def _day(tmp_path):
    with sqlite3.connect(str(tmp_path / "o.db")) as conn:
        row = conn.execute("SELECT value FROM daemon_state WHERE key='contact_prune_day'")
        row = row.fetchone()
    return row[0] if row else None


def test_prune_removes_least_heard_unprotected_contact_once_per_day(tmp_path):
    d, ft = _daemon(tmp_path, meshcore_contact_prune_enabled=True)
    try:
        _maintain(d, _at(3, 30))  # before the prune hour
        assert len(ft.fake._contacts) == 3 and _day(tmp_path) is None
        _maintain(d, _at(4, 30))
        assert set(ft.fake._contacts) == {A, C}
        assert _day(tmp_path) == "2026-10-10"
        assert d.status_snapshot()["contacts"]["last_pruned"] == 1
        ft.fake.channel_sent.clear()
        # The pruned name no longer resolves on a channel message.
        seen = []
        ft.on_message(seen.append)
        ft.fake.emit_channel_message_from(0, "bravo", "!garden")
        assert seen == []
        ft.fake.add_contact(B, "bravo", 1, 1)
        _maintain(d, _at(5, 0))  # same day: nothing more
        assert B in ft.fake._contacts
    finally:
        d.stop()


def test_prune_disabled_does_nothing(tmp_path):
    d, ft = _daemon(tmp_path)
    try:
        _maintain(d, _at(4, 30))
        assert len(ft.fake._contacts) == 3 and _day(tmp_path) is None
    finally:
        d.stop()


def test_prune_skipped_with_warning_when_target_not_below_radio_max(tmp_path, caplog):
    d, ft = _daemon(tmp_path, meshcore_contact_prune_enabled=True)
    ft.fake.max_contacts = 2
    try:
        with caplog.at_level(logging.WARNING, logger="meshgarden.main"):
            _maintain(d, _at(4, 30))
        assert len(ft.fake._contacts) == 3
        assert "must be below the radio's maximum" in caplog.text
    finally:
        d.stop()
