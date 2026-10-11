import re
import threading
import urllib.request

from prometheus_client import CollectorRegistry, generate_latest

from meshgarden.config import MetricsConfig
from meshgarden.metrics import (
    ALLOWED_PORTNUMS,
    COMMAND_OUTCOMES,
    COMMANDS,
    REPLY_OUTCOMES,
    WORKERS,
    Metrics,
    bounded_channel,
    bounded_portnum,
    start_metrics_server,
)

NODE_ID_RE = re.compile(rb"![0-9a-f]{8}")


def test_no_metric_label_can_contain_a_node_id():
    m = Metrics()
    m.raw_receive_events.labels(bounded_portnum("TEXT_MESSAGE_APP"), bounded_channel(0)).inc()
    m.commands_total.labels("SIGN", "accepted").inc()
    m.replies_total.labels("acked").inc()
    out = generate_latest(m.registry)
    assert not NODE_ID_RE.search(out)


def test_unknown_portnum_collapses_to_other():
    assert bounded_portnum("WEIRD_FUTURE_APP") == "OTHER"
    assert bounded_portnum("TEXT_MESSAGE_APP") == "TEXT_MESSAGE_APP"


def test_channel_label_is_bounded():
    assert bounded_channel(0) == "0"
    assert bounded_channel(7) == "7"
    assert bounded_channel(99) == "other"
    assert bounded_channel(-1) == "other"


def test_label_cardinality_is_bounded():
    m = Metrics()
    for p in ALLOWED_PORTNUMS:
        for c in [str(i) for i in range(8)] + ["other"]:
            m.raw_receive_events.labels(p, c).inc()
    out = generate_latest(m.registry).decode()
    n = sum(
        1 for line in out.splitlines() if line.startswith("meshgarden_raw_receive_events_total{")
    )
    assert n == len(ALLOWED_PORTNUMS) * 9


def test_all_label_values_are_preallocated():
    m = Metrics()
    out = generate_latest(m.registry).decode()
    for cmd in COMMANDS:
        for outcome in COMMAND_OUTCOMES:
            assert f'command="{cmd}",outcome="{outcome}"' in out
    for outcome in REPLY_OUTCOMES:
        assert f'outcome="{outcome}"' in out


def test_commands_allowlist_covers_every_command_enum_value():
    """Regression: QUOTE/PATH/MOON/SUN/KNOCK were missing from
    COMMANDS, so main.py labelling those via `parsed.command.value` created
    their series lazily instead of pre-allocated (imported here, not in
    metrics.py, to keep metrics.py's own import graph unchanged)."""
    from meshgarden import commands

    assert set(COMMANDS) >= {c.value for c in commands.Command}


def test_metrics_expose_required_series():
    m = Metrics()
    out = generate_latest(m.registry).decode()
    for name in [
        "meshgarden_radio_connected",
        "meshgarden_reply_ready",
        "meshgarden_observed_distinct_senders_1h",
        "meshgarden_observed_distinct_senders_24h",
        "meshgarden_guestbook_visible_entries",
        "meshgarden_reply_queue_depth",
        "meshgarden_storage_used_bytes",
        "meshgarden_storage_pressure",
        "meshgarden_backup_healthy",
        "meshgarden_rx_snr_db",
        "meshgarden_rx_rssi_dbm",
        "meshgarden_reconnects_total",
        "meshgarden_database_errors_total",
        "meshgarden_guestbook_entries_created_total",
        "meshgarden_service_uptime_seconds",
        "meshgarden_worker_heartbeat_age_seconds",
    ]:
        assert name in out


def test_worker_heartbeat_age_seconds_is_preallocated_for_every_bounded_worker():
    m = Metrics()
    out = generate_latest(m.registry).decode()
    for worker in WORKERS:
        assert f'meshgarden_worker_heartbeat_age_seconds{{worker="{worker}"}}' in out
    lines = [
        line
        for line in out.splitlines()
        if line.startswith("meshgarden_worker_heartbeat_age_seconds{")
    ]
    assert len(lines) == len(WORKERS)


def test_worker_heartbeat_age_seconds_reflects_set_calls():
    m = Metrics()
    m.worker_heartbeat_age_seconds.labels("db").set(12.5)
    out = generate_latest(m.registry).decode()
    assert 'meshgarden_worker_heartbeat_age_seconds{worker="db"} 12.5' in out


def test_label_values_are_exactly_the_preallocated_bounded_set():
    """Scrapes the registry and checks two things at once: no label value
    anywhere looks like a node id, and the command/outcome label values
    present are exactly the fixed, preallocated sets -- nothing more, nothing
    less.
    """
    m = Metrics()
    out = generate_latest(m.registry).decode()

    command_lines = [
        line for line in out.splitlines() if line.startswith("meshgarden_commands_total{")
    ]
    commands_seen = {re.search(r'command="([^"]*)"', line).group(1) for line in command_lines}
    outcomes_seen = {re.search(r'outcome="([^"]*)"', line).group(1) for line in command_lines}
    assert commands_seen == set(COMMANDS)
    assert outcomes_seen == set(COMMAND_OUTCOMES)
    assert len(command_lines) == len(COMMANDS) * len(COMMAND_OUTCOMES)

    reply_lines = [
        line for line in out.splitlines() if line.startswith("meshgarden_replies_total{")
    ]
    reply_outcomes_seen = {re.search(r'outcome="([^"]*)"', line).group(1) for line in reply_lines}
    assert reply_outcomes_seen == set(REPLY_OUTCOMES)
    assert len(reply_lines) == len(REPLY_OUTCOMES)

    assert not NODE_ID_RE.search(out.encode())


def test_rx_snr_and_rssi_are_not_observed_when_missing():
    m = Metrics()
    # Simulate the daemon's rule: a missing reading is never observed as 0.
    for rx_snr, rx_rssi in [(None, None)]:
        if rx_snr is not None:
            m.rx_snr.observe(rx_snr)
        if rx_rssi is not None:
            m.rx_rssi.observe(rx_rssi)
    out = generate_latest(m.registry).decode()
    assert "meshgarden_rx_snr_db_count 0.0" in out
    assert "meshgarden_rx_rssi_dbm_count 0.0" in out


def test_start_metrics_server_binds_loopback_and_serves_metrics():
    registry = CollectorRegistry()
    Metrics(registry=registry)
    cfg = MetricsConfig(listen="127.0.0.1", port=0)

    threads_before = threading.active_count()
    httpd, thread = start_metrics_server(cfg, registry)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
        host, port = httpd.server_address[0], httpd.server_port
        with urllib.request.urlopen(f"http://{host}:{port}/metrics", timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode()
        assert "meshgarden_radio_connected" in body
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()

    assert threading.active_count() == threads_before


def test_health_gauges_are_unknown_until_first_refresh():
    import math

    metrics = Metrics()
    for gauge in (
        metrics.radio_connected, metrics.distinct_senders_1h, metrics.distinct_senders_24h
    ):
        assert math.isnan(gauge._value.get())
