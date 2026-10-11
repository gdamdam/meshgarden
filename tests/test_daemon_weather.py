"""Daemon end to end over FakeMeshCoreTransport: a WeatherBatch in the
ingest queue becomes one channel send on the alert channel through the
ordinary Sender path. No poller thread (weather_poller=False)."""
from __future__ import annotations

import time
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
    WeatherConfig,
)
from meshgarden.main import Daemon
from meshgarden.weather import WeatherBatch, WeatherProduct
from tests.fake_meshcore import FakeMeshCoreTransport

NOW = 1791830000


def _cfg(tmp_path, **weather):
    return Config(
        radio=RadioConfig(device="/dev/fake", expected_node_id="!11223344",
                          transport="meshcore", meshcore_channel="#kallisti"),
        service=ServiceConfig(lab_unlock_word="SUPERGARDENME", mode="channel",
                              timezone="America/Los_Angeles"),
        database=DatabaseConfig(path=str(tmp_path / "o.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "backups")),
        metrics=MetricsConfig(),
        weather=WeatherConfig(enabled=True, contact="ops@example.org", **weather),
    )


def _daemon(tmp_path, *, alert_idx=5, **weather):
    ft = FakeMeshCoreTransport(watched_channel_idx=2, alert_channel_idx=alert_idx)
    d = Daemon(_cfg(tmp_path, **weather), transport=ft, start_metrics=False,
               weather_poller=False)
    d.start()
    return d, ft


def product(key="a1", zones=("CAZ006",), mtype="Alert", refs=()):
    return WeatherProduct(product_key=key, alert_ids=(key,), references=tuple(refs),
                          event="High Wind Warning", message_type=mtype, severity="Severe",
                          zones=zones, sent_at=NOW - 60, expires_at=NOW + 3600)


def batch(*products, result="ok"):
    return WeatherBatch(fetched_at=NOW, result=result, products=tuple(products))


def test_batch_becomes_one_send_on_the_alert_channel(tmp_path):
    d, ft = _daemon(tmp_path)
    try:
        d._ingest.put_nowait(batch(product()))
        d.drain()
        sent = ft.fake.channel_sent
        assert len(sent) == 1
        assert sent[0]["channel_idx"] == 5
        assert sent[0]["msg"].startswith("⚠ High Wind Warning · SF · until ")
        assert len(sent[0]["msg"].encode("utf-8")) <= 120
        row = d.conn.execute("SELECT outcome FROM weather_alerts").fetchone()
        assert row["outcome"] == "sent"
        pr = d.conn.execute(
            "SELECT command, state FROM processed_requests WHERE sender_node_id = '!11223344'"
        ).fetchone()
        assert (pr["command"], pr["state"]) == ("WEATHER", "submitted")
        assert d._metrics.weather_lines_total.labels("Alert")._value.get() == 1
        assert d._metrics.weather_polls_total.labels("ok")._value.get() == 1
    finally:
        d.stop()


def test_same_batch_twice_sends_once(tmp_path):
    d, ft = _daemon(tmp_path)
    try:
        d._ingest.put_nowait(batch(product()))
        d._ingest.put_nowait(batch(product()))
        d.drain()
        assert len(ft.fake.channel_sent) == 1
    finally:
        d.stop()


def test_unresolved_alert_channel_marks_dropped_and_daemon_keeps_running(tmp_path):
    d, ft = _daemon(tmp_path, alert_idx=None)
    try:
        d._ingest.put_nowait(batch(product()))
        d.drain()
        assert ft.fake.channel_sent == []
        row = d.conn.execute("SELECT outcome FROM weather_alerts").fetchone()
        assert row["outcome"] == "dropped"
        assert d.status_snapshot()["weather"]["channel_resolved"] is False
        # The operator adds the channel; the next poll retries.
        ft.set_alert_channel_idx(5)
        d._ingest.put_nowait(batch(product()))
        d.drain()
        assert len(ft.fake.channel_sent) == 1
    finally:
        d.stop()


def test_error_batch_only_counts_a_metric(tmp_path):
    d, ft = _daemon(tmp_path)
    try:
        d._ingest.put_nowait(batch(result="error"))
        d.drain()
        assert ft.fake.channel_sent == []
        assert d._metrics.weather_polls_total.labels("error")._value.get() == 1
    finally:
        d.stop()


def test_status_snapshot_reports_weather_and_worker(tmp_path):
    d, ft = _daemon(tmp_path)
    try:
        snap = d.status_snapshot()
        assert snap["weather"]["enabled"] is True
        assert snap["weather"]["channel_resolved"] is True
        assert "weather" in snap["workers"]
    finally:
        d.stop()


def test_weather_disabled_adds_no_worker_and_ignores_batches(tmp_path):
    ft = FakeMeshCoreTransport(watched_channel_idx=2, alert_channel_idx=5)
    cfg = _cfg(tmp_path)
    cfg = Config(**{**cfg.__dict__, "weather": WeatherConfig()})
    d = Daemon(cfg, transport=ft, start_metrics=False, weather_poller=False)
    d.start()
    try:
        d._ingest.put_nowait(batch(product()))
        d.drain()
        assert ft.fake.channel_sent == []
        assert "weather" not in d.status_snapshot()["workers"]
        assert d.status_snapshot()["weather"]["enabled"] is False
    finally:
        d.stop()


def test_dropped_product_is_resent_not_suppressed(tmp_path):
    d, ft = _daemon(tmp_path)
    try:
        d._ingest.put_nowait(batch(product()))
        d.drain()
        assert len(ft.fake.channel_sent) == 1
        key = d.conn.execute("SELECT product_key FROM weather_alerts").fetchone()["product_key"]
        # The ledger is DB-worker-thread only; this is the same thread-safe
        # path Sender's release callback uses.
        d._enqueue_weather_drop(key)
        d.drain()
        d._ingest.put_nowait(batch(product()))
        d.drain()
        assert len(ft.fake.channel_sent) == 2
    finally:
        d.stop()


def test_channel_send_error_releases_ledger_to_dropped(tmp_path):
    d, ft = _daemon(tmp_path)
    try:
        ft.fake.commands.force_next_send_error()
        d._ingest.put_nowait(batch(product()))
        d.drain()
        # The release callback only ENQUEUES the drop from the TX thread, so
        # a second drain lets the DB worker record it before we read the row.
        d.drain()
        assert ft.fake.channel_sent == []
        row = d.conn.execute("SELECT outcome FROM weather_alerts").fetchone()
        assert row["outcome"] == "dropped"
    finally:
        d.stop()


VISITOR_PUBKEY = "aabbccdd" + "11" * 28


def _la(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp())


def test_wx_by_dm_replies_with_status_line(tmp_path):
    d, ft = _daemon(tmp_path)
    try:
        ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
        ft.fake.emit_direct_message(
            VISITOR_PUBKEY[:12], "WX", sender_timestamp=int(time.time())
        )
        d.drain()
        msg = ft.fake.sent[-1]["msg"]
        assert msg.startswith("🌦 wx-bay\n9 counties")
        assert len(msg.encode("utf-8")) <= 120
    finally:
        d.stop()


def test_wx_on_watched_channel_replies_on_that_channel(tmp_path):
    d, ft = _daemon(tmp_path)
    try:
        ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
        ft.fake.emit_channel_message_from(
            2, "visitor", "WX", sender_timestamp=int(time.time())
        )
        d.drain()
        sent = ft.fake.channel_sent
        assert len(sent) == 1 and sent[0]["channel_idx"] == 2
        assert "🌦 wx-bay\n9 counties" in sent[0]["msg"]
    finally:
        d.stop()


def _maintain(d, now):
    """_run_maintenance must run on the DB worker (the ledger's connection is
    thread-owned), so borrow the worker via the batch hook and wait for it."""
    d._process_weather_batch = lambda _b: d._run_maintenance(now=now)
    d._ingest.put_nowait(batch(result="error"))
    d.drain()


def _hb_cfg_daemon(tmp_path, hour):
    ft = FakeMeshCoreTransport(watched_channel_idx=2, alert_channel_idx=3)
    cfg = _cfg(tmp_path, heartbeat_hour=hour)
    d = Daemon(cfg, transport=ft, start_metrics=False, weather_poller=False)
    d.start()
    return d, ft


def test_heartbeat_sends_once_per_day_and_survives_restart(tmp_path):
    t0 = _la(2026, 10, 10, 6, 30)
    d, ft = _hb_cfg_daemon(tmp_path, 6)
    try:
        _maintain(d, t0)
        assert len(ft.fake.channel_sent) == 1
        assert ft.fake.channel_sent[0]["channel_idx"] == 3
        assert ft.fake.channel_sent[0]["msg"].startswith("🌦 wx-bay daily")
        _maintain(d, t0 + 60)
        assert len(ft.fake.channel_sent) == 1
    finally:
        d.stop()
    d2, ft2 = _hb_cfg_daemon(tmp_path, 6)
    try:
        _maintain(d2, _la(2026, 10, 10, 6, 45))
        assert ft2.fake.channel_sent == []
        _maintain(d2, _la(2026, 10, 10, 7, 30))
        assert ft2.fake.channel_sent == []  # wrong hour
        _maintain(d2, _la(2026, 10, 11, 6, 30))
        assert len(ft2.fake.channel_sent) == 1
    finally:
        d2.stop()


def test_heartbeat_off_by_default_sends_nothing(tmp_path):
    d, ft = _hb_cfg_daemon(tmp_path, -1)
    try:
        _maintain(d, _la(2026, 10, 10, 6, 30))
        assert ft.fake.channel_sent == []
    finally:
        d.stop()
