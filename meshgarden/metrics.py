"""Prometheus metrics for Meshgarden.

Detailed node history belongs in SQLite, not here: no metric in this module
ever takes a node id, a node name, guestbook entry text, or an arbitrary
error string as a label value. Every label value is drawn from a small,
fixed allowlist defined in this module (ALLOWED_PORTNUMS, COMMANDS,
COMMAND_OUTCOMES, REPLY_OUTCOMES, and the bounded channel buckets);
anything outside that allowlist collapses to a fixed "other"/"OTHER"
bucket so label cardinality is provably bounded up front.

This module only builds the registry, the metric objects, and the bounded
label helpers, plus a small HTTP server starter. The daemon (see main.py)
owns the periodic refresh of gauges and calls into the counters/histograms
as events happen.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

from meshgarden.config import MetricsConfig

ALLOWED_PORTNUMS: tuple[str, ...] = (
    "TEXT_MESSAGE_APP",
    "POSITION_APP",
    "NODEINFO_APP",
    "TELEMETRY_APP",
    "ROUTING_APP",
    "NEIGHBORINFO_APP",
    "TRACEROUTE_APP",
    "STORE_FORWARD_APP",
    "RANGE_TEST_APP",
    "DETECTION_SENSOR_APP",
    "ADMIN_APP",
    "MESHCORE_DM",
    "MESHCORE_CHANNEL",
    "MESHCORE_ADVERT",
    "OTHER",
)
COMMANDS: tuple[str, ...] = (
    "WX",
    "WEATHER",  # processed_requests label for alert-channel broadcasts, never typed
    "NEW",
    "NEWS",
    "GUIDE",
    "CHECK",
    "REPLY",
    "SUGGEST",
    "MENU",
    "NOT_EMERGENCY",
    "INFO",
    "SIGN",
    "GUESTBOOK",
    "STATS",
    "REPEATER",
    "ICHING",
    "HAIKU",
    "BOOK",
    "WORDLE",
    "GARDEN",
    "RECORD",
    "WEEK",
    "UNSIGN",
    "SEEN",
    "WHO",
    "ME",
    "PING",
    "POSITION",
    "PATH",
    "MOON",
    "SUN",
    "POST",
    "READ",
    "BOARDS",
    "MAIL",
    "LAB_UNLOCK",
    "CMD_HELP",
    "VERSION",
    "UNKNOWN",
    "KNOCK",
)
COMMAND_OUTCOMES: tuple[str, ...] = (
    "accepted",
    "rejected",
    "deduplicated",
    "rate_limited",
    "blocked",
    "not_ready",
    "queue_full",
    "storage_full",
    "quota",
    "sign_quota",
    "post_quota",
    "mail_quota",
)
REPLY_OUTCOMES: tuple[str, ...] = ("submitted", "acked", "failed", "expired", "unknown")
MOBILE_RECOVERY_STAGES: tuple[str, ...] = (
    "normal_success",
    "normal_timeout",
    "fallback_sent",
    "fallback_success",
    "fallback_timeout",
    "fallback_failed",
    "suppressed_cooldown",
    "suppressed_global",
    "suppressed_newer_reply",
    "suppressed_path_reset",
)
WORKERS: tuple[str, ...] = ("db", "tx", "radio", "maintenance")

SNR_BUCKETS: tuple[float, ...] = (-20.0, -15.0, -10.0, -5.0, 0.0, 5.0, 10.0, 15.0)
RSSI_BUCKETS: tuple[float, ...] = (-130.0, -120.0, -110.0, -100.0, -90.0, -80.0, -70.0, -60.0)

_ALLOWED_PORTNUMS_SET = frozenset(ALLOWED_PORTNUMS)
_MIN_CHANNEL = 0
_MAX_CHANNEL = 7


def bounded_portnum(portnum: str) -> str:
    """Maps to one of a fixed allowlist, else 'OTHER'."""
    return portnum if portnum in _ALLOWED_PORTNUMS_SET else "OTHER"


def bounded_channel(channel: int) -> str:
    """'0'..'7', else 'other'."""
    if _MIN_CHANNEL <= channel <= _MAX_CHANNEL:
        return str(channel)
    return "other"


class Metrics:
    """All metrics live on a private CollectorRegistry so tests are isolated."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()

        # -- Gauges --
        self.radio_connected = Gauge(
            "meshgarden_radio_connected",
            "1 if the radio interface is currently connected, else 0.",
            registry=self.registry,
        )
        self.radio_contacts = Gauge(
            "meshgarden_radio_contacts",
            "Contacts held in the MeshCore radio's table as of the last housekeeping listing.",
            registry=self.registry,
        )
        self.radio_contacts_max = Gauge(
            "meshgarden_radio_contacts_max",
            "The MeshCore radio's contact-table capacity from its device query.",
            registry=self.registry,
        )
        self.reply_ready = Gauge(
            "meshgarden_reply_ready",
            "1 if the radio identity is verified and the service may send replies, else 0.",
            registry=self.registry,
        )
        self.distinct_senders_1h = Gauge(
            "meshgarden_observed_distinct_senders_1h",
            "Distinct sender node count observed in the last 1 hour.",
            registry=self.registry,
        )
        self.distinct_senders_24h = Gauge(
            "meshgarden_observed_distinct_senders_24h",
            "Distinct sender node count observed in the last 24 hours.",
            registry=self.registry,
        )
        # The metrics endpoint can start before the DB worker/radio is ready.
        # Unknown is not evidence of a disconnected radio or an empty history.
        for gauge in (self.radio_connected, self.distinct_senders_1h, self.distinct_senders_24h):
            gauge.set(float("nan"))
        self.guestbook_visible = Gauge(
            "meshgarden_guestbook_visible_entries",
            "Number of guestbook entries currently visible.",
            registry=self.registry,
        )
        self.queue_depth = Gauge(
            "meshgarden_reply_queue_depth",
            "Number of replies currently pending in the outbound queue.",
            registry=self.registry,
        )
        self.storage_used_bytes = Gauge(
            "meshgarden_storage_used_bytes",
            "Total managed storage used in bytes (database + backups + logs).",
            registry=self.registry,
        )
        self.storage_cap_bytes = Gauge(
            "meshgarden_storage_cap_bytes",
            "Configured managed storage cap in bytes.",
            registry=self.registry,
        )
        self.storage_pressure = Gauge(
            "meshgarden_storage_pressure",
            "Storage pressure level: 0=ok, 1=warn, 2=critical.",
            registry=self.registry,
        )
        self.backup_last_success_ts = Gauge(
            "meshgarden_backup_last_success_timestamp_seconds",
            "Unix timestamp of the last successful backup, or 0 if none yet.",
            registry=self.registry,
        )
        self.backup_healthy = Gauge(
            "meshgarden_backup_healthy",
            "1 if the most recent backup attempt succeeded or none has been attempted yet, else 0.",
            registry=self.registry,
        )
        self.service_uptime_seconds = Gauge(
            "meshgarden_service_uptime_seconds",
            "Seconds since the service process started.",
            registry=self.registry,
        )
        self.radio_connected_seconds = Gauge(
            "meshgarden_radio_connected_seconds",
            "Seconds since the radio interface last connected, or 0 if not currently connected.",
            registry=self.registry,
        )
        self.rate_limited_drops_1h = Gauge(
            "meshgarden_rate_limited_drops_1h",
            "Count of commands silently dropped in the last hour for "
            "cooldown, quota, sign_quota, or queue_full reasons.",
            registry=self.registry,
        )
        self.worker_heartbeat_age_seconds = Gauge(
            "meshgarden_worker_heartbeat_age_seconds",
            "Seconds since each worker last updated its heartbeat, bounded "
            "by the fixed worker label set.",
            ("worker",),
            registry=self.registry,
        )

        # -- Counters --
        self.raw_receive_events = Counter(
            "meshgarden_raw_receive_events",
            "Count of raw receive events observed from the radio, bounded by portnum and channel.",
            ("portnum", "channel"),
            registry=self.registry,
        )
        self.unique_packets = Counter(
            "meshgarden_unique_packets",
            "Count of unique (deduplicated) packets observed, bounded by portnum and channel.",
            ("portnum", "channel"),
            registry=self.registry,
        )
        self.commands_total = Counter(
            "meshgarden_commands",
            "Count of guestbook commands processed, by command and outcome.",
            ("command", "outcome"),
            registry=self.registry,
        )
        self.replies_total = Counter(
            "meshgarden_replies",
            "Count of outbound replies, by outcome.",
            ("outcome",),
            registry=self.registry,
        )
        self.weather_polls_total = Counter(
            "meshgarden_weather_polls",
            "Count of NWS alert polls, by result (ok, not_modified, error).",
            ("result",),
            registry=self.registry,
        )
        self.weather_lines_total = Counter(
            "meshgarden_weather_lines",
            "Count of weather lines enqueued for the alert channel, by kind.",
            ("type",),
            registry=self.registry,
        )
        self.weather_last_success_timestamp_seconds = Gauge(
            "meshgarden_weather_last_success_timestamp_seconds",
            "Unix time of the last successful NWS poll (0 = never).",
            registry=self.registry,
        )
        self.guestbook_entries_created = Counter(
            "meshgarden_guestbook_entries_created",
            "Count of guestbook entries created.",
            registry=self.registry,
        )
        self.guestbook_entries_moderated = Counter(
            "meshgarden_guestbook_entries_moderated",
            "Count of guestbook entries auto-hidden by the SIGN banned-word guard.",
            registry=self.registry,
        )
        self.mail_notes_stored = Counter(
            "meshgarden_mail_notes_stored",
            "Count of MAIL notes actually stored (recipient opted in, had room).",
            registry=self.registry,
        )
        self.mail_notes_refused = Counter(
            "meshgarden_mail_notes_refused",
            "Count of MAIL notes silently not stored (opted out, or pending-full).",
            registry=self.registry,
        )
        self.reconnects_total = Counter(
            "meshgarden_reconnects",
            "Count of radio reconnect attempts.",
            registry=self.registry,
        )
        self.database_errors_total = Counter(
            "meshgarden_database_errors",
            "Count of database errors encountered.",
            registry=self.registry,
        )
        self.malformed_packets_total = Counter(
            "meshgarden_malformed_packets",
            "Count of malformed radio packets dropped.",
            registry=self.registry,
        )
        self.ingest_dropped_total = Counter(
            "meshgarden_ingest_dropped",
            "Count of packets dropped because the bounded ingest queue was full.",
            registry=self.registry,
        )
        self.contacts_pruned_total = Counter(
            "meshgarden_contacts_pruned",
            "Count of contacts removed from the radio by the daily contact-table "
            "housekeeping (opt-in).",
            registry=self.registry,
        )
        self.reply_callback_errors_total = Counter(
            "meshgarden_reply_callback_errors",
            "Count of unexpected errors settling a reply's terminal outcome "
            "or running its post-outcome callback (ReplyJob.release_on_"
            "pretransmission_failure) -- contained by Sender so a single "
            "bad reply/callback can never wedge an unrelated one.",
            registry=self.registry,
        )
        self.mobile_dm_recovery_total = Counter(
            "meshgarden_mobile_dm_recovery",
            "Bounded outcomes of the opt-in MeshCore mobile DM recovery flow.",
            ("stage",),
            registry=self.registry,
        )

        # -- Histograms --
        # Observed only when the underlying value is present: a missing
        # RSSI/SNR reading is never recorded as 0.
        self.rx_snr = Histogram(
            "meshgarden_rx_snr_db",
            "Distribution of received signal-to-noise ratio in dB, when reported.",
            buckets=SNR_BUCKETS,
            registry=self.registry,
        )
        self.rx_rssi = Histogram(
            "meshgarden_rx_rssi_dbm",
            "Distribution of received signal strength in dBm, when reported.",
            buckets=RSSI_BUCKETS,
            registry=self.registry,
        )

        # Pre-allocate every command/outcome and reply-outcome label
        # combination so a dashboard panel never shows a gap, and so
        # cardinality is provably bounded from the moment the process starts.
        for command in COMMANDS:
            for outcome in COMMAND_OUTCOMES:
                self.commands_total.labels(command, outcome)
        for outcome in REPLY_OUTCOMES:
            self.replies_total.labels(outcome)
        for stage in MOBILE_RECOVERY_STAGES:
            self.mobile_dm_recovery_total.labels(stage)
        for worker in WORKERS:
            self.worker_heartbeat_age_seconds.labels(worker)


def start_metrics_server(cfg: MetricsConfig, registry: CollectorRegistry) -> Any:
    """Starts the Prometheus HTTP exporter bound to cfg.listen:cfg.port.

    Binding is always exactly what MetricsConfig says -- never a hardcoded
    0.0.0.0 default -- so the endpoint is loopback-only unless an operator
    has explicitly configured otherwise. Config loading (config.py's
    _parse_metrics) already logs a warning for any non-loopback listen
    address, including 0.0.0.0, at config load time; this function does not
    duplicate that check.
    """
    return start_http_server(cfg.port, addr=cfg.listen, registry=registry)
