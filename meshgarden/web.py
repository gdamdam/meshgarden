"""Local, read-only, no-authentication web dashboard (batch 3a).

Disabled by default (`[web] enabled = false`). When enabled, serves 11
server-rendered pages (`/`, `/health`, `/traffic`, `/guestbook`, `/boards`,
`/records`, `/digest[/<day>]`, `/nodes[/<id>]`, `/map`, `/topology`,
`/propagation`) plus 9 JSON endpoints for external displays (see
`_ROUTE_STATIC` below for the full list; `/api/summary.json` is the main
one: today's digest numbers, the 24-hour activity series and the most
recently heard nodes) over plain stdlib `http.server` from a daemon thread,
bound to `127.0.0.1:9109` unless the operator explicitly sets
`allow_lan = true`.

Hard operator rules, enforced here:

- GET only. Every other method (including POST) gets 405 -- there are no
  write actions of any kind, and there is no authentication to protect one.
- Every request opens its own short-lived READ-ONLY SQLite connection
  (`database.connect(path, read_only=True)`), so a page render can never
  block the daemon's single DB worker thread, and a bug here can never
  write to the database.
- A stalled or absent daemon must never break a page: connectivity/radio/
  worker/backup/storage fields are read from the JSON status file the
  daemon already writes (see main.py's `write_status_file`), not computed
  live, and are rendered as "stale"/"unknown" rather than raising when that
  file is missing or old.
- Every value that came from the database (guestbook text, display names,
  node names) is HTML-escaped by web_templates.esc() before it reaches a
  response body.
- No external assets, no JavaScript: web_templates.py inlines all CSS; the
  only graphics are tiny inline SVG bars.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import os
import random
import re
import sqlite3
import statistics
import threading
import time
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from meshgarden import __version__
from meshgarden import web_templates as tpl
from meshgarden.booktrivia import book_stats_summary
from meshgarden.collector import (
    Collector,
    haversine_km,
    is_distance_plausible_for_hops,
    is_position_plausible,
)
from meshgarden.config import Config, WebConfig
from meshgarden.database import connect, migrate
from meshgarden.digest import build_digest, format_digest
from meshgarden.fortune import (
    HEXAGRAMS,
    haiku_text,
    hexagram_for,
    local_day,
    local_midnight,
)
from meshgarden.guestbook import (
    Guestbook,
    clean_display_name,
    fallback_display_name,
    lookup_node_name,
)
from meshgarden.metrics import ALLOWED_PORTNUMS, bounded_portnum
from meshgarden.wordle import wordle_stats_summary

logger = logging.getLogger(__name__)

# Mirrors main.py's own _STATUS_FILE_STALE_SECONDS. web.py cannot import
# that constant without importing all of main.py (main.py imports THIS
# module for its start/stop hook, so the reverse import would be
# circular); duplicated here instead, same pattern as config.py's own
# _sanitize_config_text docstring explains for guestbook.py.
STATUS_STALE_SECONDS = 60

_SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

_NODE_ID_RE = re.compile(r"^![0-9a-fA-F]{8}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_REPLY_STATES = ("submitted", "acked", "failed", "expired", "unknown")


class WebRefused(Exception):
    """Raised by `start_web_server` when the configured bind address is not
    loopback and `[web] allow_lan` was not explicitly set to true."""


# ---------------------------------------------------------------------------
# config / bind guard
# ---------------------------------------------------------------------------


def _is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return addr == "localhost"


def _check_bind_allowed(cfg: WebConfig) -> None:
    if _is_loopback(cfg.listen):
        return
    if not cfg.allow_lan:
        raise WebRefused(
            f"[web] listen={cfg.listen!r} is not a loopback address; refusing to "
            "start the dashboard. Set [web] allow_lan = true to explicitly allow "
            "this (there is no authentication -- anyone who can reach this "
            "address can read everything the dashboard shows)."
        )
    logger.warning(
        "web.listen=%r is not a loopback address; the local dashboard will be "
        "reachable beyond localhost (allow_lan=true, no authentication)",
        cfg.listen,
    )


# ---------------------------------------------------------------------------
# status file / db helpers
# ---------------------------------------------------------------------------


def _status_file_path(cfg: Config) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(cfg.database.path)), "status.json")


def read_status(cfg: Config) -> tuple[dict[str, Any], bool]:
    """Returns (status, stale). `status` is `{}` if the file is missing or
    unreadable/corrupt. `stale` is True whenever the file is missing,
    unreadable, or its `generated_at` marker is older than
    STATUS_STALE_SECONDS -- callers must treat every status-file-derived
    field as unknown in that case, not raise. Pure JSON file read: never
    touches the database or the radio, so a stalled or absent daemon can
    never break a page."""
    path = _status_file_path(cfg)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}, True
    if not isinstance(data, dict):
        return {}, True
    generated_at = data.get("generated_at")
    stale = not isinstance(generated_at, (int, float)) or (
        time.time() - generated_at > STATUS_STALE_SECONDS
    )
    return data, stale


def _ro_connect(cfg: Config) -> sqlite3.Connection:
    """A short-lived read-only connection to the daemon's database, or (if
    the database file does not exist yet -- e.g. the daemon has never
    started) an empty in-memory database with the same schema, so every
    page renders as "empty" instead of erroring."""
    try:
        return connect(cfg.database.path, read_only=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        migrate(conn)
        return conn


def _digest_dir(cfg: Config) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(cfg.database.path)), "digest")


def _fmt_utc(epoch_seconds: int) -> str:
    if not epoch_seconds:
        return "not heard"
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# shared query helpers
# ---------------------------------------------------------------------------


def _hourly_activity(
    conn: sqlite3.Connection, now: int, tz: str
) -> tuple[list[str], list[float], list[float]]:
    """24 hourly buckets ending at the current (partial) hour: (labels,
    packet counts, distinct-sender counts). Bounded by observations
    retention (7 days by default), so this never scans more than a day's
    worth of rows."""
    hour_start = (now // 3600) * 3600
    window_start = hour_start - 23 * 3600
    rows = conn.execute(
        "SELECT server_time, sender_node_id FROM observations WHERE server_time >= ?",
        (window_start,),
    ).fetchall()
    packets = [0] * 24
    senders: list[set[str]] = [set() for _ in range(24)]
    for r in rows:
        idx = (r["server_time"] - window_start) // 3600
        if 0 <= idx < 24:
            packets[idx] += 1
            if r["sender_node_id"] != "^unknown":
                senders[idx].add(r["sender_node_id"])
    zone = ZoneInfo(tz)
    labels = [
        datetime.fromtimestamp(window_start + i * 3600, tz=zone).strftime("%H:00")
        for i in range(24)
    ]
    return labels, [float(p) for p in packets], [float(len(s)) for s in senders]


def _hourly_traffic_detail(
    conn: sqlite3.Connection, now: int, tz: str
) -> tuple[list[str], list[float], list[float], list[int], list[int]]:
    """Like `_hourly_activity`, but for `_page_traffic`: labels, packet
    counts, distinct-sender counts, unique (non-duplicate) counts, and
    direct (hops_away == 0) counts per hourly bucket, all computed from ONE
    24h `observations` scan instead of two (this used to call
    `_hourly_activity` for the first three series, then run a second,
    near-identical query over the same window for the last two)."""
    hour_start = (now // 3600) * 3600
    window_start = hour_start - 23 * 3600
    rows = conn.execute(
        "SELECT server_time, sender_node_id, is_duplicate, hops_away FROM observations "
        "WHERE server_time >= ?",
        (window_start,),
    ).fetchall()
    packets = [0] * 24
    senders: list[set[str]] = [set() for _ in range(24)]
    unique_per_hour = [0] * 24
    direct_per_hour = [0] * 24
    for r in rows:
        idx = (r["server_time"] - window_start) // 3600
        if 0 <= idx < 24:
            packets[idx] += 1
            if r["sender_node_id"] != "^unknown":
                senders[idx].add(r["sender_node_id"])
            if not r["is_duplicate"]:
                unique_per_hour[idx] += 1
            if r["hops_away"] == 0:
                direct_per_hour[idx] += 1
    zone = ZoneInfo(tz)
    labels = [
        datetime.fromtimestamp(window_start + i * 3600, tz=zone).strftime("%H:00")
        for i in range(24)
    ]
    return (
        labels,
        [float(p) for p in packets],
        [float(len(s)) for s in senders],
        unique_per_hour,
        direct_per_hour,
    )


def _reply_outcomes_since(conn: sqlite3.Connection, since: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM processed_requests WHERE first_seen_at >= ? "
        "AND state IN (?, ?, ?, ?, ?) GROUP BY state",
        (since, *_REPLY_STATES),
    ).fetchall()
    outcomes = dict.fromkeys(_REPLY_STATES, 0)
    for r in rows:
        outcomes[r["state"]] = r["n"]
    return outcomes


def _moderation_queue_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM guestbook WHERE visible=0 AND moderation_reason IS NOT NULL"
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# observatory helpers (batch 3b): map/topology/propagation projection,
# node classification, and small stats shared by those three pages
# ---------------------------------------------------------------------------

# Known Meshtastic device-role enum names (radio.py copies `user.role`
# verbatim from the protobuf's own name). Used only to validate /map's
# `?role=` filter -- an unrecognized value is ignored, never a KeyError.
_ROLE_ALLOWLIST = frozenset(
    {
        "CLIENT",
        "CLIENT_MUTE",
        "CLIENT_HIDDEN",
        "ROUTER",
        "ROUTER_CLIENT",
        "ROUTER_LATE",
        "REPEATER",
        "TRACKER",
        "SENSOR",
        "TAK",
        "TAK_TRACKER",
        "LOST_AND_FOUND",
    }
)
_MAP_RANGE_ALLOWED = ("today", "7d")
_MAP_MAX_NODES = 300
# Distance is computed in Python (it needs the configured observatory
# lat/lon), so the SQL query can't ORDER BY distance -- it instead fetches
# the most-recently-seen positioned nodes up to this bound, and the Python
# side sorts those by distance and slices to _MAP_MAX_NODES for display.
_MAP_FETCH_LIMIT = 2000
_TOPOLOGY_MAX_ROWS = 500
_PROPAGATION_MAX_NODES = 200
_RING_BASE_KM = (5.0, 10.0, 25.0, 50.0)
_RING_MAX_KM = 300.0
_FRESH_FADE_SECONDS = 7 * 86400  # opacity fades from 1.0 to 0.3 by this age


def _observatory_location(cfg: Config) -> tuple[float, float] | None:
    """The operator's `[observatory] latitude/longitude`, or None if unset
    ((0.0, 0.0), config.ObservatoryConfig's documented sentinel)."""
    lat, lon = cfg.observatory.latitude, cfg.observatory.longitude
    if (lat, lon) == (0.0, 0.0):
        return None
    return lat, lon


def _local_xy_km(obs_lat: float, obs_lon: float, lat: float, lon: float) -> tuple[float, float]:
    """(east_km, north_km) of (lat, lon) relative to (obs_lat, obs_lon): an
    equirectangular local projection built from two haversine calls (one
    pure-latitude, one pure-longitude) rather than a flat cos-lat formula --
    accurate enough for a "furthest heard" plot at LoRa mesh ranges (tens to
    a few hundred km), never used for anything precise."""
    north = haversine_km(obs_lat, obs_lon, lat, obs_lon)
    if lat < obs_lat:
        north = -north
    east = haversine_km(obs_lat, obs_lon, obs_lat, lon)
    if lon < obs_lon:
        east = -east
    return east, north


def _bearing_deg(obs_lat: float, obs_lon: float, lat: float, lon: float) -> float:
    """Initial great-circle bearing (0-360, 0 = north) from (obs_lat,
    obs_lon) to (lat, lon)."""
    phi1, phi2 = math.radians(obs_lat), math.radians(lat)
    dlambda = math.radians(lon - obs_lon)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _rings_km(furthest_km: float) -> list[float]:
    """5/10/25/50 km rings, auto-scaled as a group (doubled together) so
    the furthest plotted node falls within them, capped at 300 km."""
    if furthest_km <= 0:
        return [_RING_BASE_KM[0]]
    scale = 1
    while _RING_BASE_KM[-1] * scale < furthest_km and _RING_BASE_KM[-1] * scale < _RING_MAX_KM:
        scale *= 2
    out: list[float] = []
    for base in _RING_BASE_KM:
        r = min(base * scale, _RING_MAX_KM)
        if r not in out:
            out.append(r)
    return out


def _fade_opacity(age_seconds: float) -> float:
    """1.0 when just seen, fading linearly to 0.3 at 7 days old and beyond."""
    if age_seconds <= 0:
        return 1.0
    if age_seconds >= _FRESH_FADE_SECONDS:
        return 0.3
    return 1.0 - 0.7 * (age_seconds / _FRESH_FADE_SECONDS)


def _hops_class(*, direct_packets: int | None, min_hops: int | None) -> str:
    """direct / 2-hop / 3+ hops / unknown, from a node_daily rollup over the
    trailing 7 days: `direct_packets` (any day > 0) mirrors GARDEN's own
    "heard direct" rule (hop_start == hop_limit); `min_hops` (the best/
    lowest derived hop count seen) is used only when never heard direct."""
    if direct_packets:
        return "direct"
    if min_hops is None:
        return "unknown"
    if min_hops == 1:
        return "2-hop"
    return "3+ hops"


_HOPS_CLASS_COLOR = {
    "direct": "var(--bar)",
    "2-hop": "var(--link)",
    "3+ hops": "var(--warn)",
    "unknown": "var(--muted)",
}


def _day_floor(now: int, days: int) -> str:
    """UTC calendar day string `days` back from `now` -- the lower bound
    used to filter node_daily.day for a trailing N-day window."""
    return datetime.fromtimestamp(now - days * 86400, tz=UTC).strftime("%Y-%m-%d")


def _classify_nodes(conn: sqlite3.Connection, now: int) -> dict[str, str]:
    """node_id -> hops class (see `_hops_class`), from the trailing 7 days
    of node_daily. Shared by /map, /topology and /nodes/<id>."""
    since = _day_floor(now, 7)
    rows = conn.execute(
        "SELECT node_id, SUM(direct_packets) AS direct_packets, MIN(best_hops) AS min_hops "
        "FROM node_daily WHERE day >= ? GROUP BY node_id",
        (since,),
    ).fetchall()
    return {
        r["node_id"]: _hops_class(direct_packets=r["direct_packets"], min_hops=r["min_hops"])
        for r in rows
    }


def _node_min_hops(conn: sqlite3.Connection, now: int) -> dict[str, int | None]:
    """node_id -> lowest derived hop count observed in the trailing 7 days
    (None if never derivable in that window), from `node_daily.best_hops`.
    Used for the hop/distance plausibility check (see
    collector.is_distance_plausible_for_hops) on /map and /nodes/<id> --
    the most permissive (lowest) hop count a node was heard at is the one
    most likely to make its recorded distance plausible."""
    since = _day_floor(now, 7)
    rows = conn.execute(
        "SELECT node_id, MIN(best_hops) AS min_hops FROM node_daily "
        "WHERE day >= ? GROUP BY node_id",
        (since,),
    ).fetchall()
    return {r["node_id"]: r["min_hops"] for r in rows}


def _latest_precision_bits(conn: sqlite3.Connection) -> dict[str, int | None]:
    """node_id -> precision_bits of its most recent node_positions row.
    Bounded by however many distinct nodes have ever reported a position."""
    rows = conn.execute(
        "SELECT np.node_id AS node_id, np.precision_bits AS precision_bits "
        "FROM node_positions np JOIN "
        "(SELECT node_id, MAX(server_time) AS max_t FROM node_positions GROUP BY node_id) latest "
        "ON latest.node_id = np.node_id AND latest.max_t = np.server_time"
    ).fetchall()
    return {r["node_id"]: r["precision_bits"] for r in rows}


# ---------------------------------------------------------------------------
# page renderers -- each returns a full HTML document (bytes)
# ---------------------------------------------------------------------------


def _page_now(cfg: Config, conn: sqlite3.Connection, status: dict, stale: bool) -> bytes:
    now = int(time.time())
    tz = cfg.service.timezone
    midnight = local_midnight(now, tz)
    col = Collector(conn)
    gb = Guestbook(conn, cfg.service)

    nodes_today = col.distinct_senders_since(midnight)
    heard_direct_today = col.distinct_direct_senders_since(midnight)
    new_nodes_today = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE observed_first_seen >= ?", (midnight,)
    ).fetchone()[0]
    best_snr_24h = col.best_snr_since(now - 86400)

    furthest_direct = furthest_observed = furthest_observed_incl_mqtt = None
    have_location = (cfg.observatory.latitude, cfg.observatory.longitude) != (0.0, 0.0)
    if have_location:
        furthest_direct = col.furthest_direct_km_since(
            midnight,
            observatory_lat=cfg.observatory.latitude,
            observatory_lon=cfg.observatory.longitude,
            max_direct_km=cfg.observatory.max_direct_km,
            max_km_per_hop=cfg.observatory.max_km_per_hop,
            max_observed_km=cfg.observatory.max_observed_km,
        )
        furthest_observed = col.furthest_observed_km_with_node_since(
            midnight,
            observatory_lat=cfg.observatory.latitude,
            observatory_lon=cfg.observatory.longitude,
            max_direct_km=cfg.observatory.max_direct_km,
            max_km_per_hop=cfg.observatory.max_km_per_hop,
            max_observed_km=cfg.observatory.max_observed_km,
        )
        furthest_observed_incl_mqtt = col.furthest_observed_incl_mqtt_km_with_node_since(
            midnight,
            observatory_lat=cfg.observatory.latitude,
            observatory_lon=cfg.observatory.longitude,
            max_observed_km=cfg.observatory.max_observed_km,
        )

    guestbook_count = gb.visible_count()
    outcomes = _reply_outcomes_since(conn, midnight)

    labels, packets_series, senders_series = _hourly_activity(conn, now, tz)
    bars = tpl.svg_bar_chart(
        labels,
        [
            ("packets", packets_series, "var(--bar)"),
            ("distinct senders", senders_series, "var(--bar2)"),
        ],
    )

    reply_ready = status.get("reply_ready")
    actual_id = status.get("actual_node_id")
    expected_id = status.get("expected_node_id")
    mismatch = status.get("mismatch_reason")
    last_packet_at = status.get("last_packet_at")
    queue_depth = status.get("queue_depth")
    uptime = status.get("uptime_seconds")

    last_packet_age = "n/a"
    if isinstance(last_packet_at, (int, float)):
        last_packet_age = f"{max(0, now - int(last_packet_at))}s ago"

    identity_line = (
        f"{tpl.esc(_node_label(conn, actual_id))} "
        f"(expected {tpl.esc(_node_label(conn, expected_id))})"
    )
    if mismatch:
        identity_line += f' <span class="pill crit">{tpl.esc(mismatch)}</span>'

    outcomes_html = " · ".join(f"{name} {n}" for name, n in outcomes.items())

    banner = (
        '<div class="banner">Daemon status is stale or unavailable -- radio/queue/uptime '
        "fields below reflect the last known state, if any.</div>"
        if stale
        else ""
    )

    cards = "".join(
        [
            tpl.stat_card("Service", tpl.esc(cfg.service.name)),
            tpl.stat_card("Reply ready", tpl.bool_pill_yn(reply_ready)),
            tpl.stat_card("Nodes heard today", str(nodes_today)),
            tpl.stat_card("Heard direct today", str(heard_direct_today)),
            tpl.stat_card("New nodes today", str(new_nodes_today)),
            tpl.stat_card("Best SNR 24h", tpl.na(best_snr_24h, "{:+.1f} dB")),
            tpl.stat_card(
                "Furthest direct",
                tpl.na(furthest_direct, "{:.0f} km") if have_location else "n/a",
            ),
            tpl.stat_card(
                "Furthest observed",
                tpl.na(furthest_observed[0], "{:.0f} km")
                if have_location and furthest_observed is not None
                else "n/a",
            ),
            tpl.stat_card(
                "Furthest observed (incl. MQTT-relayed/unknown)",
                tpl.na(furthest_observed_incl_mqtt[0], "{:.0f} km")
                if have_location and furthest_observed_incl_mqtt is not None
                else "n/a",
            ),
            tpl.stat_card("Last packet", last_packet_age),
            tpl.stat_card("Queue depth", tpl.na(queue_depth)),
            tpl.stat_card("Uptime", tpl.na(uptime, "{}s")),
            tpl.stat_card("Guestbook entries", str(guestbook_count)),
        ]
    )

    body = f"""
{banner}
<h1>Right now</h1>
<p>Identity: {identity_line}</p>
<div class="grid">{cards}</div>
<h2>24h activity</h2>
{bars}
<h2>Reply outcomes today</h2>
<p>{tpl.esc(outcomes_html) if outcomes_html else "none"}</p>
<p class="muted">More: <a href="/map">Map</a> &middot; <a href="/topology">Topology</a> &middot;
<a href="/propagation">Propagation</a></p>
"""
    return tpl.layout(title="Right now", active="/", body=body)


def _page_health(cfg: Config, conn: sqlite3.Connection, status: dict, stale: bool) -> bytes:
    workers = status.get("workers", {}) or {}
    storage = status.get("storage", {}) or {}
    drops_1h = status.get("rate_limited_drops_1h")
    last_backup_at = status.get("last_backup_at")
    last_backup_error = status.get("last_backup_error")
    moderation_queue = _moderation_queue_count(conn)
    version = status.get("version") or __version__

    banner = (
        '<div class="banner">Status file missing or stale: the daemon may not be '
        "running. Fields below are unknown / last-known unless noted.</div>"
        if stale
        else ""
    )

    def _worker_row(name: str, info: dict) -> str:
        age = tpl.na(info.get("age_seconds"), "{:.1f}s")
        state = tpl.bool_pill(not info.get("stalled"), true_label="alive", false_label="stalled")
        return f"<tr><td>{tpl.esc(name)}</td><td>{age}</td><td>{state}</td></tr>"

    worker_rows = "".join(
        _worker_row(name, info) for name, info in sorted(workers.items())
    ) or '<tr><td colspan="3" class="muted">unknown</td></tr>'

    used = storage.get("total_bytes")
    cap = storage.get("cap_bytes")
    used_mib = f"{used / (1024 * 1024):.1f} MiB" if isinstance(used, (int, float)) else "n/a"
    cap_mib = f"{cap / (1024 * 1024):.1f} MiB" if isinstance(cap, (int, float)) else "n/a"

    backup_age = "n/a"
    if isinstance(last_backup_at, (int, float)) and last_backup_at:
        backup_age = f"{int(time.time()) - int(last_backup_at)}s ago"

    body = f"""
{banner}
<h1>Health</h1>
<h2>Workers</h2>
<table><thead><tr><th>Worker</th><th>Heartbeat age</th><th>Status</th></tr></thead>
<tbody>{worker_rows}</tbody></table>

<h2>Radio</h2>
<div class="grid">
{tpl.stat_card("Connected", tpl.bool_pill_yn(status.get("radio_connected")))}
{tpl.stat_card("Reply ready", tpl.bool_pill_yn(status.get("reply_ready")))}
{tpl.stat_card("Actual node", tpl.esc(_node_label(conn, status.get("actual_node_id"))))}
{tpl.stat_card("Expected node", tpl.esc(_node_label(conn, status.get("expected_node_id"))))}
{tpl.stat_card("Mismatch", tpl.esc(status.get("mismatch_reason")) or "none")}
</div>

<h2>Storage</h2>
<div class="grid">
{tpl.stat_card("Used", used_mib)}
{tpl.stat_card("Cap", cap_mib)}
{tpl.stat_card("Pressure", tpl.pressure_pill(storage.get("pressure")))}
</div>

<h2>Backups</h2>
<div class="grid">
{tpl.stat_card("Last backup", backup_age)}
{tpl.stat_card("Last backup error", tpl.esc(last_backup_error) or "none")}
</div>

<h2>Other</h2>
<div class="grid">
{tpl.stat_card("Rate-limit/quota drops (1h)", tpl.na(drops_1h))}
{tpl.stat_card("Moderation queue", str(moderation_queue))}
{tpl.stat_card("Version", tpl.esc(version))}
</div>
<p class="muted">Metrics endpoint: {tpl.esc(cfg.metrics.listen)}:{cfg.metrics.port}/metrics
(Prometheus text format)</p>
"""
    return tpl.layout(title="Health", active="/health", body=body)


def _page_traffic(cfg: Config, conn: sqlite3.Connection, status: dict, stale: bool) -> bytes:
    now = int(time.time())
    since_24h = now - 86400
    tz = cfg.service.timezone

    labels, packets_series, senders_series, unique_per_hour, direct_per_hour = (
        _hourly_traffic_detail(conn, now, tz)
    )

    table_rows = "".join(
        f"<tr><td>{tpl.esc(labels[i])}</td><td>{int(packets_series[i])}</td>"
        f"<td>{unique_per_hour[i]}</td><td>{int(senders_series[i])}</td>"
        f"<td>{direct_per_hour[i]}</td></tr>"
        for i in range(24)
    )

    portnum_rows = conn.execute(
        "SELECT portnum, COUNT(*) AS n FROM observations WHERE server_time >= ? GROUP BY portnum",
        (since_24h,),
    ).fetchall()
    mix: dict[str, int] = dict.fromkeys(ALLOWED_PORTNUMS, 0)
    for r in portnum_rows:
        mix[bounded_portnum(r["portnum"])] += r["n"]
    mix_html = "".join(
        f"<tr><td>{tpl.esc(name)}</td><td>{n}</td></tr>" for name, n in mix.items() if n
    ) or '<tr><td colspan="2" class="muted">no traffic</td></tr>'

    top_senders = conn.execute(
        "SELECT o.sender_node_id AS node_id, COUNT(*) AS n, nd.short_name, nd.long_name "
        "FROM observations o LEFT JOIN nodes nd ON nd.node_id = o.sender_node_id "
        "WHERE o.server_time >= ? GROUP BY o.sender_node_id ORDER BY n DESC LIMIT 10",
        (since_24h,),
    ).fetchall()
    top_rows = "".join(
        f"<tr><td>{tpl.esc(fallback_display_name(r['node_id']))}</td>"
        f"<td>{tpl.esc(r['short_name'] or r['long_name'] or '')}</td><td>{r['n']}</td></tr>"
        for r in top_senders
    ) or '<tr><td colspan="3" class="muted">no traffic</td></tr>'

    mqtt_rows = conn.execute(
        "SELECT via_mqtt, COUNT(*) AS n FROM observations WHERE server_time >= ? GROUP BY via_mqtt",
        (since_24h,),
    ).fetchall()
    mqtt_counts = {"MQTT": 0, "RF": 0, "unknown": 0}
    for r in mqtt_rows:
        if r["via_mqtt"] == 1:
            mqtt_counts["MQTT"] = r["n"]
        elif r["via_mqtt"] == 0:
            mqtt_counts["RF"] = r["n"]
        else:
            mqtt_counts["unknown"] = r["n"]

    body = f"""
<h1>Traffic</h1>
<h2>Last 24 hours</h2>
<table><thead><tr><th>Hour</th><th>Packets</th><th>Unique</th><th>Senders</th><th>Direct</th></tr></thead>
<tbody>{table_rows}</tbody></table>

<h2>Traffic mix (24h, by portnum)</h2>
<table><thead><tr><th>Portnum</th><th>Count</th></tr></thead><tbody>{mix_html}</tbody></table>

<h2>Top 10 senders (24h)</h2>
<table><thead><tr><th>Node</th><th>Name</th><th>Packets</th></tr></thead><tbody>{top_rows}</tbody></table>

<h2>Transport (24h)</h2>
<div class="grid">
{tpl.stat_card("MQTT", str(mqtt_counts["MQTT"]))}
{tpl.stat_card("RF", str(mqtt_counts["RF"]))}
{tpl.stat_card("Unknown", str(mqtt_counts["unknown"]))}
</div>
"""
    return tpl.layout(title="Traffic", active="/traffic", body=body)


def _topic_list_rows(gb: Guestbook) -> str:
    """Shared by `_page_boards` and `_page_guestbook`'s own boards section:
    every topic ever posted to, count desc, linking to `/guestbook?topic=`.
    """
    counts = gb.topic_counts()
    return "".join(
        f'<tr><td><a href="/guestbook?topic={tpl.esc(t)}">#{tpl.esc(t)}</a></td><td>{c}</td></tr>'
        for t, c in counts
    ) or '<tr><td colspan="2" class="muted">no topics yet</td></tr>'


def _page_guestbook(
    cfg: Config,
    conn: sqlite3.Connection,
    status: dict,
    stale: bool,
    page: int,
    topic: str | None = None,
) -> bytes:
    gb = Guestbook(conn, cfg.service)
    total_visible = gb.visible_count(topic=topic)
    entries = gb.page_visible(page, 50, topic=topic)

    def _entry_row(e) -> str:
        name = e.display_name or fallback_display_name(e.sender_node_id)
        when = datetime.fromtimestamp(e.created_at, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        return (
            f"<tr><td>#{e.entry_id}</td><td>{tpl.esc(when)}</td>"
            f"<td>{tpl.esc(name)}</td><td>{tpl.esc(e.message)}</td></tr>"
        )

    entry_rows = "".join(_entry_row(e) for e in entries) or (
        '<tr><td colspan="4" class="muted">no entries</td></tr>'
    )

    mod_entries = conn.execute(
        "SELECT entry_id, created_at, sender_node_id, display_name, message, moderation_reason "
        "FROM guestbook WHERE visible=0 AND moderation_reason IS NOT NULL "
        "ORDER BY entry_id DESC LIMIT 50"
    ).fetchall()
    mod_rows = "".join(
        f"<tr><td>#{r['entry_id']}</td>"
        f"<td>{tpl.esc(r['display_name'] or fallback_display_name(r['sender_node_id']))}</td>"
        f"<td>{tpl.esc(r['message'])}</td><td>{tpl.esc(r['moderation_reason'])}</td></tr>"
        for r in mod_entries
    ) or '<tr><td colspan="4" class="muted">empty</td></tr>'

    def _nav_href(p: int) -> str:
        if topic:
            return f"/guestbook?topic={tpl.esc(topic)}&page={p}"
        return f"/guestbook?page={p}"

    nav_bits = [f'<a href="{_nav_href(page + 1)}">older</a>']
    if page > 1:
        nav_bits.insert(0, f'<a href="{_nav_href(page - 1)}">newer</a>')
    pagination = f'<div class="pagination">Page {page} · {" · ".join(nav_bits)}</div>'

    heading = f"Board #{tpl.esc(topic)}" if topic else "Guestbook"
    count_label = f"{total_visible} visible posts" if topic else f"{total_visible} visible entries"

    body = f"""
<h1>{heading}</h1>
<p>{count_label}</p>
<table><thead><tr><th>#</th><th>When</th><th>Name</th><th>Message</th></tr></thead>
<tbody>{entry_rows}</tbody></table>
{pagination}

<h2>Moderation queue</h2>
<p class="muted">Hidden by the SIGN banned-word guard, pending operator review. Approve with
<code>mgarden guestbook approve &lt;entry-id&gt;</code>, or permanently remove with
<code>mgarden guestbook delete &lt;entry-id&gt;</code>.</p>
<table><thead><tr><th>#</th><th>Name</th><th>Message</th><th>Reason</th></tr></thead>
<tbody>{mod_rows}</tbody></table>

<h2>Boards</h2>
<table><thead><tr><th>Topic</th><th>Visible posts</th></tr></thead>
<tbody>{_topic_list_rows(gb)}</tbody></table>
"""
    return tpl.layout(title="Guestbook", active="/guestbook", body=body)


def _page_boards(cfg: Config, conn: sqlite3.Connection) -> bytes:
    gb = Guestbook(conn, cfg.service)
    rows = _topic_list_rows(gb)
    body = f"""
<h1>Boards</h1>
<table><thead><tr><th>Topic</th><th>Visible posts</th></tr></thead>
<tbody>{rows}</tbody></table>
"""
    return tpl.layout(title="Boards", active="/boards", body=body)


def _short_name_for_record(conn: sqlite3.Connection, node_id: str | None) -> str | None:
    """Full known name for dashboard records; no radio-sized abbreviation."""
    return lookup_node_name(conn, node_id)


def _name_and_tail(name: str | None, node_id: str | None) -> str:
    """"<name> · <tail>" when name is given, else the bare tail -- same
    convention as commands._name_and_tail, duplicated here rather than
    importing commands.py (a private helper, and web.py already has its
    own fallback_display_name-based tail convention)."""
    if node_id is None:
        return "unknown sender"
    tail = fallback_display_name(node_id)
    return f"{name} · {tail}" if name else tail


def _node_label(conn: sqlite3.Connection, node_id: str | None) -> str:
    """Known node name plus its full id, or the id by itself."""
    if node_id is None:
        return "n/a"
    name = lookup_node_name(conn, node_id)
    return f"{name} ({node_id})" if name else node_id


def _row_node_label(row: sqlite3.Row) -> str:
    return _name_and_tail(clean_display_name(row["short_name"], row["long_name"]), row["node_id"])


def _record_window(conn: sqlite3.Connection, cfg: Config, since: int) -> dict[str, Any]:
    col = Collector(conn)
    best = col.best_snr_with_sender_since(since)
    max_hops = col.max_hops_since(since)
    busiest = col.busiest_hour_since(since, cfg.service.timezone)
    furthest_direct = furthest_observed = furthest_observed_incl_mqtt = None
    if (cfg.observatory.latitude, cfg.observatory.longitude) != (0.0, 0.0):
        furthest_direct = col.furthest_direct_km_since(
            since,
            observatory_lat=cfg.observatory.latitude,
            observatory_lon=cfg.observatory.longitude,
            max_direct_km=cfg.observatory.max_direct_km,
            max_km_per_hop=cfg.observatory.max_km_per_hop,
            max_observed_km=cfg.observatory.max_observed_km,
        )
        furthest_observed = col.furthest_observed_km_with_node_since(
            since,
            observatory_lat=cfg.observatory.latitude,
            observatory_lon=cfg.observatory.longitude,
            max_direct_km=cfg.observatory.max_direct_km,
            max_km_per_hop=cfg.observatory.max_km_per_hop,
            max_observed_km=cfg.observatory.max_observed_km,
        )
        furthest_observed_incl_mqtt = col.furthest_observed_incl_mqtt_km_with_node_since(
            since,
            observatory_lat=cfg.observatory.latitude,
            observatory_lon=cfg.observatory.longitude,
            max_observed_km=cfg.observatory.max_observed_km,
        )
    return {
        "best_snr": best,
        "best_snr_name": _short_name_for_record(conn, best[1]) if best is not None else None,
        "max_hops": max_hops,
        "busiest_hour": busiest,
        "furthest_direct": furthest_direct,
        "furthest_observed": furthest_observed,
        "furthest_observed_name": (
            _short_name_for_record(conn, furthest_observed[1])
            if furthest_observed is not None
            else None
        ),
        "furthest_observed_incl_mqtt": furthest_observed_incl_mqtt,
        "furthest_observed_incl_mqtt_name": (
            _short_name_for_record(conn, furthest_observed_incl_mqtt[1])
            if furthest_observed_incl_mqtt is not None
            else None
        ),
    }


def _biggest_day(conn: sqlite3.Connection, day_floor: str) -> tuple[str, int] | None:
    row = conn.execute(
        "SELECT day, distinct_senders FROM daily_stats WHERE day >= ? "
        "ORDER BY distinct_senders DESC, day DESC LIMIT 1",
        (day_floor,),
    ).fetchone()
    return (row["day"], row["distinct_senders"]) if row is not None else None


def _render_window_section(title: str, w: dict[str, Any], biggest: tuple[str, int] | None) -> str:
    if w["best_snr"] is not None:
        snr, node_id = w["best_snr"]
        snr_s = f"{snr:+.1f} dB ({_name_and_tail(w['best_snr_name'], node_id)})"
    else:
        snr_s = "n/a"
    furthest_direct_s = tpl.na(w["furthest_direct"], "{:.0f} km")
    if w["furthest_observed"] is not None:
        km, node_id = w["furthest_observed"]
        label = _name_and_tail(w["furthest_observed_name"], node_id)
        furthest_observed_s = f"{km:.0f} km ({label})"
    else:
        furthest_observed_s = "n/a"
    if w["furthest_observed_incl_mqtt"] is not None:
        km, node_id = w["furthest_observed_incl_mqtt"]
        name = w["furthest_observed_incl_mqtt_name"]
        furthest_observed_incl_mqtt_s = f"{km:.0f} km ({_name_and_tail(name, node_id)})"
    else:
        furthest_observed_incl_mqtt_s = "n/a"
    biggest_s = f"{biggest[1]} senders ({biggest[0]})" if biggest else "n/a"
    return f"""
<h2>{tpl.esc(title)}</h2>
<div class="grid">
{tpl.stat_card("Best SNR", tpl.esc(snr_s))}
{tpl.stat_card("Most hops", tpl.na(w["max_hops"]))}
{tpl.stat_card("Busiest hour", tpl.na(w["busiest_hour"], "{:02d}:00"))}
{tpl.stat_card("Biggest day", biggest_s)}
{tpl.stat_card("Furthest direct", furthest_direct_s)}
{tpl.stat_card("Furthest observed", tpl.esc(furthest_observed_s))}
{tpl.stat_card(
    "Furthest observed (incl. MQTT-relayed/unknown)", tpl.esc(furthest_observed_incl_mqtt_s)
)}
</div>
"""


def _records_windows(
    cfg: Config, conn: sqlite3.Connection
) -> dict[str, tuple[dict[str, Any], tuple[str, int]]]:
    """The three record windows and their biggest days, keyed today/week/all.
    Shared by the /records page and /api/records.json so the two can never
    disagree about a figure."""
    now = int(time.time())
    tz = cfg.service.timezone
    midnight = local_midnight(now, tz)
    week_start = now - 7 * 86400
    # daily_stats.day is a UTC calendar day (retention.py) -- match _biggest_day's
    # convention here so the fallback tuple's label agrees with a real query result.
    today_biggest = (_day_floor(now, 0), Collector(conn).distinct_senders_since(midnight))
    return {
        "today": (_record_window(conn, cfg, midnight), today_biggest),
        "week": (
            _record_window(conn, cfg, week_start),
            _biggest_day(conn, _day_floor(now, 7)) or today_biggest,
        ),
        "all": (_record_window(conn, cfg, 0), _biggest_day(conn, "") or today_biggest),
    }


def _page_records(cfg: Config, conn: sqlite3.Connection, status: dict, stale: bool) -> bytes:
    windows = _records_windows(cfg, conn)
    today_w, today_biggest = windows["today"]
    week_w, week_biggest = windows["week"]
    all_w, all_biggest = windows["all"]

    book_rows = book_stats_summary(conn)
    book_total_solves = sum(r.solves for r in book_rows)
    book_total_guesses = sum(r.guesses for r in book_rows)
    # since_day bounds the window to the last 7 calendar days, not just the
    # newest 7 wordle_daily rows (which can span more days if play is sparse).
    wordle_rows = wordle_stats_summary(conn, days=7, since_day=_day_floor(int(time.time()), 6))
    wordle_total_solves = sum(r.solves for r in wordle_rows)

    book_table = "".join(
        f"<tr><td>{tpl.esc(r.book_key)}</td><td>{r.shown}</td><td>{r.guesses}</td><td>{r.solves}</td></tr>"
        for r in book_rows
        if r.shown or r.guesses or r.solves
    ) or '<tr><td colspan="4" class="muted">none yet</td></tr>'

    wordle_table = "".join(
        f"<tr><td>{tpl.esc(r.day)}</td><td>{r.players}</td><td>{r.solves}</td>"
        f"<td>{r.avg_guesses:.1f}</td></tr>"
        for r in wordle_rows
    ) or '<tr><td colspan="4" class="muted">none yet</td></tr>'

    body = f"""
<h1>Records</h1>
{_render_window_section("Today", today_w, today_biggest)}
{_render_window_section("Last 7 days", week_w, week_biggest)}
{_render_window_section("All time", all_w, all_biggest)}

<h2>Book trivia (all-time)</h2>
<p>{book_total_solves} solves across {book_total_guesses} guesses</p>
<table><thead><tr><th>Book</th><th>Shown</th><th>Guesses</th><th>Solves</th></tr></thead>
<tbody>{book_table}</tbody></table>

<h2>Wordle (last 7 days)</h2>
<p>{wordle_total_solves} solves in the last 7 days</p>
<table><thead><tr><th>Day</th><th>Players</th><th>Solves</th><th>Avg guesses</th></tr></thead>
<tbody>{wordle_table}</tbody></table>
"""
    return tpl.layout(title="Records", active="/records", body=body)


def _page_digest_index(cfg: Config) -> bytes:
    digest_dir = _digest_dir(cfg)
    try:
        names = sorted(
            (n[: -len(".json")] for n in os.listdir(digest_dir) if n.endswith(".json")),
            reverse=True,
        )
    except FileNotFoundError:
        names = []

    latest_html = '<p class="muted">no digests yet</p>'
    if names:
        d = _load_digest(cfg, names[0])
        if d is not None:
            latest_html = f"<pre>{tpl.esc(format_digest(d))}</pre>"

    list_html = "".join(
        f'<li><a href="/digest/{tpl.esc(n)}">{tpl.esc(n)}</a></li>' for n in names
    ) or '<li class="muted">none</li>'

    body = f"""
<h1>Digest</h1>
<h2>Latest ({tpl.esc(names[0]) if names else "n/a"})</h2>
{latest_html}
<h2>All digests</h2>
<ul>{list_html}</ul>
"""
    return tpl.layout(title="Digest", active="/digest", body=body)


def _load_digest(cfg: Config, day: str) -> dict | None:
    """`day` MUST already be validated (regex shape + a real calendar date)
    by the caller before this is invoked -- see `_match_route`."""
    digest_dir = os.path.realpath(_digest_dir(cfg))
    path = os.path.realpath(os.path.join(digest_dir, f"{day}.json"))
    if os.path.dirname(path) != digest_dir:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _page_digest_day(cfg: Config, day: str) -> bytes | None:
    d = _load_digest(cfg, day)
    if d is None:
        return None
    body = f"""
<h1>Digest — {tpl.esc(day)}</h1>
<pre>{tpl.esc(format_digest(d))}</pre>
<p><a href="/digest">back to all digests</a></p>
"""
    return tpl.layout(title=f"Digest {day}", active="/digest", body=body)


_NODES_PER_PAGE = 200


def _page_nodes(
    cfg: Config, conn: sqlite3.Connection, status: dict, stale: bool, page: int
) -> bytes:
    now = int(time.time())
    since7 = now - 7 * 86400
    # node_daily.day is a UTC calendar day (see collector.py / database.py),
    # not the operator's local day -- match that convention here.
    today = _day_floor(now, 0)
    offset = (page - 1) * _NODES_PER_PAGE

    rows = conn.execute(
        "SELECT node_id, short_name, long_name, hw_model, role, observed_first_seen, "
        "observed_last_seen, last_position_lat, last_position_lon FROM nodes "
        "WHERE observed_last_seen >= ? ORDER BY observed_last_seen DESC LIMIT ? OFFSET ?",
        (since7, _NODES_PER_PAGE, offset),
    ).fetchall()
    daily_today = {
        r["node_id"]: r
        for r in conn.execute(
            "SELECT node_id, packets, direct_packets, best_snr FROM node_daily WHERE day = ?",
            (today,),
        ).fetchall()
    }

    def _row(n) -> str:
        d = daily_today.get(n["node_id"])
        packets_24h = d["packets"] if d else 0
        direct = bool(d and d["direct_packets"])
        best_snr = d["best_snr"] if d else None
        has_position = n["last_position_lat"] is not None
        name = n["short_name"] or n["long_name"] or fallback_display_name(n["node_id"])
        tail = tpl.esc(fallback_display_name(n["node_id"]))
        first_seen = datetime.fromtimestamp(
            n["observed_first_seen"], tz=UTC
        ).strftime("%Y-%m-%d")
        last_seen = datetime.fromtimestamp(
            n["observed_last_seen"], tz=UTC
        ).strftime("%Y-%m-%d %H:%M")
        return (
            f'<tr><td><a href="/nodes/{tpl.esc(n["node_id"])}">{tail}</a></td>'
            f"<td>{tpl.esc(name)}</td><td>{tpl.esc(n['hw_model'])}</td>"
            f"<td>{tpl.esc(n['role'])}</td>"
            f"<td>{tpl.esc(first_seen)}</td><td>{tpl.esc(last_seen)}</td>"
            f"<td>{packets_24h}</td><td>{tpl.na(best_snr, '{:+.1f}')}</td>"
            f'<td>{"yes" if direct else "no"}</td><td>{"yes" if has_position else "no"}</td></tr>'
        )

    table = "".join(_row(n) for n in rows) or '<tr><td colspan="9" class="muted">no nodes</td></tr>'

    nav_bits = [f'<a href="/nodes?page={page + 1}">older</a>']
    if page > 1:
        nav_bits.insert(0, f'<a href="/nodes?page={page - 1}">newer</a>')
    pagination = f'<div class="pagination">Page {page} · {" · ".join(nav_bits)}</div>'

    body = f"""
<h1>Nodes (last 7 days)</h1>
<table><thead><tr><th>Id</th><th>Name</th><th>HW</th><th>Role</th><th>First seen</th>
<th>Last seen</th><th>Packets today (UTC)</th><th>Best SNR</th><th>Direct?</th>
<th>Position?</th></tr></thead>
<tbody>{table}</tbody></table>
{pagination}
"""
    return tpl.layout(title="Nodes", active="/nodes", body=body)


def _page_node_profile(cfg: Config, conn: sqlite3.Connection, node_id: str) -> bytes | None:
    node = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
    if node is None:
        return None

    now = int(time.time())
    since7 = _day_floor(now, 7)

    daily_rows_desc = conn.execute(
        "SELECT day, packets, direct_packets, best_snr, snr_sum, snr_count, typical_snr, "
        "best_hops, hops_sum, hops_count, typical_hops FROM node_daily "
        "WHERE node_id = ? ORDER BY day DESC LIMIT 30",
        (node_id,),
    ).fetchall()
    daily_rows_asc = list(reversed(daily_rows_desc))
    week_rows = [r for r in daily_rows_desc if r["day"] >= since7]

    days_seen_7d = len(week_rows)
    packets_7d = sum(r["packets"] for r in week_rows)
    direct_packets_7d = sum(r["direct_packets"] for r in week_rows)
    best_snr_7d = max(
        (r["best_snr"] for r in week_rows if r["best_snr"] is not None), default=None
    )
    snr_sum_7d = sum(r["snr_sum"] for r in week_rows)
    snr_count_7d = sum(r["snr_count"] for r in week_rows)
    typical_snr_7d = (snr_sum_7d / snr_count_7d) if snr_count_7d else None
    best_hops_7d = min(
        (r["best_hops"] for r in week_rows if r["best_hops"] is not None), default=None
    )
    hops_sum_7d = sum(r["hops_sum"] for r in week_rows)
    hops_count_7d = sum(r["hops_count"] for r in week_rows)
    typical_hops_7d = (hops_sum_7d / hops_count_7d) if hops_count_7d else None
    hops_class = _hops_class(direct_packets=direct_packets_7d, min_hops=best_hops_7d)

    packets_all = conn.execute(
        "SELECT COALESCE(SUM(packets), 0) AS total FROM node_daily WHERE node_id = ?",
        (node_id,),
    ).fetchone()["total"]

    positions_count = conn.execute(
        "SELECT COUNT(*) FROM node_positions WHERE node_id = ?", (node_id,)
    ).fetchone()[0]
    last_position = conn.execute(
        "SELECT lat, lon, server_time, precision_bits FROM node_positions WHERE node_id = ? "
        "ORDER BY server_time DESC LIMIT 1",
        (node_id,),
    ).fetchone()

    obs = _observatory_location(cfg)
    has_position = node["last_position_lat"] is not None and node["last_position_lon"] is not None
    precision_bits = last_position["precision_bits"] if last_position is not None else None

    if not has_position:
        distance_bearing_html = "<p>Position: unknown -- no position ever reported.</p>"
    else:
        precision_s = f"{precision_bits} bits" if precision_bits is not None else "unknown"
        if obs is not None:
            dist = haversine_km(
                obs[0], obs[1], node["last_position_lat"], node["last_position_lon"]
            )
            bearing = _bearing_deg(
                obs[0], obs[1], node["last_position_lat"], node["last_position_lon"]
            )
            plausible = is_position_plausible(
                node["last_position_lat"], node["last_position_lon"], precision_bits
            ) and is_distance_plausible_for_hops(
                dist,
                best_hops_7d,
                max_direct_km=cfg.observatory.max_direct_km,
                max_km_per_hop=cfg.observatory.max_km_per_hop,
                max_observed_km=cfg.observatory.max_observed_km,
            )
            verdict = (
                "plausible"
                if plausible
                else "position/distance implausible for hops (excluded from records)"
            )
            distance_bearing_html = (
                f"<p>Position: known. Distance {dist:.0f} km, bearing {bearing:.0f}&deg; from "
                f"the observatory (position precision: {tpl.esc(precision_s)} -- coarser "
                f"positions are less precise). Plausibility: {tpl.esc(verdict)}.</p>"
            )
        else:
            distance_bearing_html = (
                "<p>Position: known, but [observatory] latitude/longitude is not configured, "
                f"so distance/bearing are unavailable (position precision: "
                f"{tpl.esc(precision_s)}).</p>"
            )

    daily_table = "".join(
        f"<tr><td>{tpl.esc(r['day'])}</td><td>{r['packets']}</td><td>{r['direct_packets']}</td>"
        f"<td>{tpl.na(r['best_snr'], '{:+.1f}')}</td><td>{tpl.na(r['typical_snr'], '{:+.1f}')}</td>"
        f"<td>{tpl.na(r['best_hops'])}</td><td>{tpl.na(r['typical_hops'], '{:.1f}')}</td></tr>"
        for r in daily_rows_desc
    ) or '<tr><td colspan="7" class="muted">no daily rollups</td></tr>'

    sparkline_packets = tpl.svg_sparkline(
        [r["day"] for r in daily_rows_asc],
        [float(r["packets"]) for r in daily_rows_asc],
        color="var(--bar)",
        unit_fmt="{:.0f}",
    )
    sparkline_snr = tpl.svg_sparkline(
        [r["day"] for r in daily_rows_asc],
        [r["best_snr"] for r in daily_rows_asc],
        color="var(--bar2)",
        unit_fmt="{:+.1f}",
    )

    # Events: first discovered, then -- separately for best (lowest) hops and
    # strongest SNR -- report the first day observed, and only claim an
    # "improvement" once a later day beats an already-established baseline.
    # A single data point is never itself an improvement.
    events = [f"First discovered {tpl.esc(_fmt_utc(node['observed_first_seen']))}."]

    hops_days = [(r["day"], r["best_hops"]) for r in daily_rows_asc if r["best_hops"] is not None]
    if hops_days:
        first_day, baseline = hops_days[0]
        events.append(f"Best hops first observed: {baseline} on {tpl.esc(first_day)}.")
        for day, value in hops_days[1:]:
            if value < baseline:
                events.append(
                    f"Best hops improved to {value} (from {baseline}) on {tpl.esc(day)}."
                )
                baseline = value

    snr_days = [(r["day"], r["best_snr"]) for r in daily_rows_asc if r["best_snr"] is not None]
    if len(snr_days) >= 2:
        best_day, best_value = max(snr_days, key=lambda t: t[1])
        events.append(f"Strongest SNR day: {tpl.esc(best_day)} ({best_value:+.1f} dB).")
    elif len(snr_days) == 1:
        only_day, only_value = snr_days[0]
        events.append(
            f"Strongest SNR: only one day of data ({only_value:+.1f} dB on {tpl.esc(only_day)})."
        )
    events_html = "".join(f"<li>{e}</li>" for e in events)

    name = node["short_name"] or node["long_name"] or fallback_display_name(node_id)
    first_seen = _fmt_utc(node["observed_first_seen"])
    last_seen = _fmt_utc(node["observed_last_seen"])
    body = f"""
<h1>Node {tpl.esc(_row_node_label(node))}</h1>
<div class="grid">
{tpl.stat_card("Name", tpl.esc(name))}
{tpl.stat_card("Hardware", tpl.esc(node["hw_model"]) or "n/a")}
{tpl.stat_card("Role", tpl.esc(node["role"]) or "n/a")}
{tpl.stat_card("First seen", tpl.esc(first_seen))}
{tpl.stat_card("Last seen", tpl.esc(last_seen))}
{tpl.stat_card("Days seen (7d)", f"{days_seen_7d} / 7")}
{tpl.stat_card("Hop class (7d)", tpl.esc(hops_class))}
{tpl.stat_card("Best SNR (7d)", tpl.na(best_snr_7d, "{:+.1f} dB"))}
{tpl.stat_card("Typical SNR (7d, mean)", tpl.na(typical_snr_7d, "{:+.1f} dB"))}
{tpl.stat_card("Best hops (7d)", tpl.na(best_hops_7d))}
{tpl.stat_card("Typical hops (7d, mean)", tpl.na(typical_hops_7d, "{:.1f}"))}
{tpl.stat_card("Packets (7d)", str(packets_7d))}
{tpl.stat_card("Packets (all)", str(packets_all))}
{tpl.stat_card("Battery (last)", tpl.na(node["last_battery"], "{}%"))}
{tpl.stat_card("Observations", str(node["observation_count"]))}
{tpl.stat_card("Positions recorded", str(positions_count))}
</div>
{distance_bearing_html}
<h2>30-day trend</h2>
<p class="muted">Packets/day</p>
{sparkline_packets}
<p class="muted">Best SNR/day</p>
{sparkline_snr}
<h2>Events</h2>
<ul>{events_html}</ul>
<h2>Daily rollups (last 30 days)</h2>
<table><thead><tr><th>Day</th><th>Packets</th><th>Direct</th><th>Best SNR</th>
<th>Typical SNR</th><th>Best hops</th><th>Typical hops</th></tr></thead>
<tbody>{daily_table}</tbody></table>
<p><a href="/nodes">back to nodes</a> &middot; <a href="/map">map</a> &middot;
<a href="/topology">topology</a></p>
"""
    return tpl.layout(title=f"Node {node_id}", active="/nodes", body=body)


# ---------------------------------------------------------------------------
# /map (batch 3b)
# ---------------------------------------------------------------------------


def _parse_map_filters(
    query: dict[str, list[str]], *, tz: str, now: int
) -> tuple[int, bool, str | None, bool, str]:
    """Parses /map's query string: (since, direct_only, role, new_only,
    range_label). Every value is validated against a fixed allowlist;
    anything unrecognized is silently ignored (falls back to "no filter"/
    the default 7d range) rather than a 400 -- the same forgiving
    convention `_parse_page` already uses for `?page=`, documented in
    README's Local dashboard section."""
    range_raw = query.get("range", ["7d"])[0]
    range_label = range_raw if range_raw in _MAP_RANGE_ALLOWED else "7d"
    since = local_midnight(now, tz) if range_label == "today" else now - 7 * 86400
    direct_only = query.get("direct", [""])[0] == "1"
    role_raw = query.get("role", [""])[0].strip().upper()
    role = role_raw if role_raw in _ROLE_ALLOWLIST else None
    new_only = query.get("new", [""])[0] == "1"
    return since, direct_only, role, new_only, range_label


_MAP_SVG_SIZE = 640
_MAP_SVG_PAD = 24


def _map_svg(plotted: list[dict[str, Any]], rings: list[float]) -> str:
    max_ring = rings[-1] if rings else 5.0
    half = _MAP_SVG_SIZE / 2
    plot_radius = half - _MAP_SVG_PAD
    scale = plot_radius / max_ring

    def _px(east_km: float, north_km: float) -> tuple[float, float]:
        return half + east_km * scale, half - north_km * scale

    parts = [
        f'<svg class="bars" viewBox="0 0 {_MAP_SVG_SIZE} {_MAP_SVG_SIZE}" width="100%" '
        f'height="{_MAP_SVG_SIZE}" role="img" aria-label="node map, observatory at centre">'
    ]
    for r in rings:
        px_r = r * scale
        parts.append(
            f'<circle cx="{half}" cy="{half}" r="{px_r:.1f}" fill="none" '
            'stroke="var(--border)" stroke-dasharray="2,3"/>'
            f'<text x="{half + 3:.1f}" y="{half - px_r + 10:.1f}">{r:.0f} km</text>'
        )
    for e in plotted:
        if not e.get("linked"):
            continue
        x, y = _px(e["east_km"], e["north_km"])
        parts.append(
            f'<line x1="{half}" y1="{half}" x2="{x:.1f}" y2="{y:.1f}" '
            'stroke="var(--muted)" stroke-width="1" opacity="0.35">'
            "<title>inferred link</title></line>"
        )
    parts.append(
        f'<circle cx="{half}" cy="{half}" r="5" fill="var(--accent)">'
        "<title>observatory</title></circle>"
    )
    for e in plotted:
        x, y = _px(e["east_km"], e["north_km"])
        color = _HOPS_CLASS_COLOR.get(e["class"], "var(--muted)")
        plausible = e.get("plausible", True)
        title_suffix = "" if plausible else ", implausible position/distance for its hops"
        title = f"{tpl.esc(e['label'])} ({tpl.esc(e['class'])}{title_suffix})"
        if plausible:
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" '
                f'opacity="{e["opacity"]:.2f}"><title>{title}</title></circle>'
            )
        else:
            # Hollow marker: implausible position/distance for its hop
            # count (or an MQTT-relayed/unknown-origin candidate) --
            # excluded from ring auto-scaling too, see the `furthest`
            # computation above.
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="none" stroke="{color}" '
                f'stroke-width="1.5" opacity="{e["opacity"]:.2f}"><title>{title}</title></circle>'
            )
    parts.append("</svg>")
    legend_bits = [
        f'<span style="color:{color}">&#9679;</span> {tpl.esc(name)}'
        for name, color in (
            ("direct", _HOPS_CLASS_COLOR["direct"]),
            ("2-hop", _HOPS_CLASS_COLOR["2-hop"]),
            ("3+ hops", _HOPS_CLASS_COLOR["3+ hops"]),
            ("unknown", _HOPS_CLASS_COLOR["unknown"]),
        )
    ]
    legend_bits.append(
        '<span class="muted">opacity fades with last-seen age (fresh 1.0 &rarr; 7d 0.3)</span>'
    )
    legend_bits.append(
        '<span class="muted">faint line = inferred direct link to the observatory</span>'
    )
    legend_bits.append(
        '<span class="muted">hollow marker = implausible position/distance for its hops '
        "(excluded from records and ring scaling)</span>"
    )
    parts.append(f'<div class="muted">{" &nbsp; ".join(legend_bits)}</div>')
    return "".join(parts)


def _page_map(cfg: Config, conn: sqlite3.Connection, query: dict[str, list[str]]) -> bytes:
    now = int(time.time())
    tz = cfg.service.timezone
    since, direct_only, role, new_only, range_label = _parse_map_filters(query, tz=tz, now=now)
    obs = _observatory_location(cfg)
    classes = _classify_nodes(conn, now)

    sql = (
        "SELECT node_id, short_name, long_name, role, observed_first_seen, "
        "observed_last_seen, last_position_lat, last_position_lon FROM nodes "
        "WHERE last_position_lat IS NOT NULL AND last_position_lon IS NOT NULL "
        "AND observed_last_seen >= ?"
    )
    params: list[Any] = [since]
    if role is not None:
        sql += " AND role = ?"
        params.append(role)
    if new_only:
        sql += " AND observed_first_seen >= ?"
        params.append(now - 86400)
    sql += " ORDER BY observed_last_seen DESC LIMIT ?"
    params.append(_MAP_FETCH_LIMIT)
    rows = conn.execute(sql, params).fetchall()

    precision = _latest_precision_bits(conn)
    min_hops = _node_min_hops(conn, now)
    linked_node_ids = {
        r["a_node_id"]
        for r in conn.execute(
            "SELECT a_node_id FROM links WHERE kind = 'inferred_direct' AND last_seen >= ?",
            (now - 7 * 86400,),
        ).fetchall()
    }

    plotted: list[dict[str, Any]] = []
    for r in rows:
        cls = classes.get(r["node_id"], "unknown")
        if direct_only and cls != "direct":
            continue
        lat, lon = r["last_position_lat"], r["last_position_lon"]
        age = max(0, now - r["observed_last_seen"])
        entry: dict[str, Any] = {
            "node_id": r["node_id"],
            "name": r["short_name"] or r["long_name"] or fallback_display_name(r["node_id"]),
            "label": _row_node_label(r),
            "role": r["role"],
            "last_seen": r["observed_last_seen"],
            "class": cls,
            "opacity": _fade_opacity(age),
            "linked": r["node_id"] in linked_node_ids,
            "precision_bits": precision.get(r["node_id"]),
        }
        if obs is not None:
            east, north = _local_xy_km(obs[0], obs[1], lat, lon)
            entry["east_km"], entry["north_km"] = east, north
            distance_km = haversine_km(obs[0], obs[1], lat, lon)
            entry["distance_km"] = distance_km
            entry["bearing_deg"] = _bearing_deg(obs[0], obs[1], lat, lon)
            entry["plausible"] = is_position_plausible(
                lat, lon, precision.get(r["node_id"])
            ) and is_distance_plausible_for_hops(
                distance_km,
                min_hops.get(r["node_id"]),
                max_direct_km=cfg.observatory.max_direct_km,
                max_km_per_hop=cfg.observatory.max_km_per_hop,
                max_observed_km=cfg.observatory.max_observed_km,
            )
        else:
            entry["distance_km"] = None
            entry["plausible"] = True
        plotted.append(entry)

    if obs is not None:
        plotted.sort(key=lambda e: (e["distance_km"], -e["last_seen"]))
    else:
        plotted.sort(key=lambda e: -e["last_seen"])
    matched_count = len(plotted)  # before the display slice below
    truncated = matched_count > _MAP_MAX_NODES
    plotted = plotted[:_MAP_MAX_NODES]

    filter_bits = [f"range={tpl.esc(range_label)}"]
    if direct_only:
        filter_bits.append("direct only")
    if role:
        filter_bits.append(f"role={tpl.esc(role)}")
    if new_only:
        filter_bits.append("new (&le;24h)")
    filter_note = ", ".join(filter_bits)

    if obs is None:
        svg = ""
        note = (
            '<div class="banner">Observatory location not configured '
            "([observatory] latitude/longitude): showing a list of nodes with a known "
            "position only -- no map, no distance/bearing, no rings.</div>"
        )
    else:
        note = ""
        furthest = max(
            (e["distance_km"] for e in plotted if e["plausible"]), default=0.0
        )
        svg = _map_svg(plotted, _rings_km(furthest))

    def _row(e: dict[str, Any]) -> str:
        tail = tpl.esc(fallback_display_name(e["node_id"]))
        last_seen_s = _fmt_utc(e["last_seen"])
        dist = tpl.na(e.get("distance_km"), "{:.0f} km")
        if not e.get("plausible", True):
            dist += " ⚠ implausible"
        bearing = tpl.na(e.get("bearing_deg"), "{:.0f}°") if obs is not None else "n/a"
        bits = e.get("precision_bits")
        precision_s = f"{bits} bits (coarser = less precise)" if bits is not None else "n/a"
        return (
            f'<tr><td><a href="/nodes/{tpl.esc(e["node_id"])}">{tail}</a></td>'
            f"<td>{tpl.esc(e['name'])}</td><td>{dist}</td><td>{bearing}</td>"
            f"<td>{tpl.esc(e['class'])}</td><td>{tpl.esc(last_seen_s)}</td>"
            f"<td>{tpl.esc(precision_s)}</td></tr>"
        )

    table = "".join(_row(e) for e in plotted) or (
        '<tr><td colspan="7" class="muted">no positioned nodes match these filters</td></tr>'
    )
    cap_note = (
        f'<p class="muted">Showing the nearest {len(plotted)} of {matched_count:,} '
        "positioned nodes that matched these filters.</p>"
        if truncated
        else ""
    )

    body = f"""
{note}
<h1>Map</h1>
<p class="muted">Filters: {filter_note} &middot;
<a href="/map?range=today">today</a> &middot; <a href="/map?range=7d">7 d</a> &middot;
<a href="/map?direct=1">direct only</a> &middot; <a href="/map?new=1">new (&le;24h)</a> &middot;
<a href="/map">clear filters</a></p>
{svg}
{cap_note}
<table><thead><tr><th>Id</th><th>Name</th><th>Distance</th><th>Bearing</th>
<th>Hops class</th><th>Last seen</th><th>Position precision</th></tr></thead>
<tbody>{table}</tbody></table>
<p class="muted">Positions are voluntarily advertised by each node and may be coarse or stale.
"Direct"/"2-hop"/"3+ hops" are inferred from received packets over the last 7 days -- observed,
never a known physical route.</p>
"""
    return tpl.layout(title="Map", active="/map", body=body)


# ---------------------------------------------------------------------------
# /topology (batch 3b)
# ---------------------------------------------------------------------------


def _topology_svg(neighbours: list[sqlite3.Row], two_hop_rows: list[sqlite3.Row]) -> str:
    size = 480
    half = size / 2
    r1 = half * 0.45
    r2 = half * 0.85
    parts = [
        f'<svg class="bars" viewBox="0 0 {size} {size}" width="100%" height="{size}" '
        'role="img" aria-label="topology ring: observatory, direct neighbours, 2-hop nodes">',
        f'<circle cx="{half}" cy="{half}" r="{r1:.1f}" fill="none" stroke="var(--border)" '
        'stroke-dasharray="2,3"/>',
        f'<circle cx="{half}" cy="{half}" r="{r2:.1f}" fill="none" stroke="var(--border)" '
        'stroke-dasharray="2,3"/>',
        f'<circle cx="{half}" cy="{half}" r="6" fill="var(--accent)">'
        "<title>observatory</title></circle>",
    ]
    n = max(1, len(neighbours))
    for i, r in enumerate(neighbours):
        angle = 2 * math.pi * i / n
        x = half + r1 * math.sin(angle)
        y = half - r1 * math.cos(angle)
        title = (
            f"{tpl.esc(_row_node_label(r))}: "
            f"count {r['count']}, "
            f"last seen {tpl.esc(_fmt_utc(r['last_seen']))}"
        )
        parts.append(
            f'<line x1="{half}" y1="{half}" x2="{x:.1f}" y2="{y:.1f}" '
            'stroke="var(--muted)" opacity="0.4"/>'
        )
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="var(--bar)"><title>{title}</title>'
            "</circle>"
        )
    m = max(1, len(two_hop_rows))
    for i, r in enumerate(two_hop_rows):
        angle = 2 * math.pi * i / m
        x = half + r2 * math.sin(angle)
        y = half - r2 * math.cos(angle)
        label = _name_and_tail(clean_display_name(r["short_name"], r["long_name"]), r["node_id"])
        title = f"{tpl.esc(label)} (2-hop, unattached)"
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="var(--link)"><title>{title}</title>'
            "</circle>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _topology_rows(
    conn: sqlite3.Connection, now: int
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """Direct neighbours (7 days, busiest first) and the 2-hop nodes beyond
    them. Shared by the /topology page and /api/topology.json so the two can
    never disagree about who sits on which ring."""
    # Older MeshCore observations predate direct-link insertion. Include
    # their zero-relay evidence without rewriting historical data. MAX(count)
    # avoids counting the same packet once through each source.
    neighbours = conn.execute(
        "WITH direct AS ("
        " SELECT a_node_id AS node_id, first_seen, last_seen, count FROM links"
        " WHERE kind = 'inferred_direct' AND last_seen >= ?"
        " UNION ALL SELECT sender_node_id, MIN(server_time), MAX(server_time), COUNT(*)"
        " FROM observations WHERE transport = 'meshcore' AND hops_away = 0"
        " AND via_mqtt = 0 AND is_duplicate = 0 AND sender_node_id != '^unknown'"
        " AND server_time >= ? GROUP BY sender_node_id"
        ") SELECT d.node_id, MIN(d.first_seen) AS first_seen, MAX(d.last_seen) AS last_seen,"
        " MAX(d.count) AS count, n.short_name, n.long_name"
        " FROM direct d LEFT JOIN nodes n ON n.node_id = d.node_id"
        " GROUP BY d.node_id ORDER BY count DESC, d.node_id LIMIT ?",
        (now - 7 * 86400, now - 7 * 86400, _TOPOLOGY_MAX_ROWS),
    ).fetchall()
    direct_ids = {r["node_id"] for r in neighbours}
    classes = _classify_nodes(conn, now)
    two_hop_ids = [
        node_id for node_id, cls in classes.items() if cls == "2-hop" and node_id not in direct_ids
    ][:_TOPOLOGY_MAX_ROWS]
    two_hop_rows: list[sqlite3.Row] = []
    if two_hop_ids:
        placeholders = ",".join("?" for _ in two_hop_ids)
        two_hop_rows = conn.execute(
            f"SELECT node_id, short_name, long_name, observed_last_seen FROM nodes "
            f"WHERE node_id IN ({placeholders})",
            two_hop_ids,
        ).fetchall()
    return neighbours, two_hop_rows


def _page_topology(cfg: Config, conn: sqlite3.Connection) -> bytes:
    now = int(time.time())
    since24 = now - 86400
    since48 = now - 2 * 86400

    neighbours, two_hop_rows = _topology_rows(conn, now)
    disappeared = conn.execute(
        "SELECT l.a_node_id AS node_id, l.first_seen, l.last_seen, l.count, "
        "n.short_name, n.long_name FROM links l LEFT JOIN nodes n ON n.node_id = l.a_node_id "
        "WHERE l.kind = 'inferred_direct' AND l.last_seen >= ? AND l.last_seen < ? "
        "ORDER BY l.last_seen DESC LIMIT ?",
        (since48, since24, _TOPOLOGY_MAX_ROWS),
    ).fetchall()
    appeared = [r for r in neighbours if r["first_seen"] >= since24]

    stable_rows = conn.execute(
        "SELECT d.node_id, n.short_name, n.long_name, COUNT(*) AS days_seen FROM node_daily d "
        "LEFT JOIN nodes n ON n.node_id=d.node_id "
        "WHERE day >= ? AND direct_packets > 0 GROUP BY d.node_id HAVING days_seen >= 5 "
        "ORDER BY days_seen DESC LIMIT ?",
        (_day_floor(now, 7), _TOPOLOGY_MAX_ROWS),
    ).fetchall()

    def _name(short: str | None, long_: str | None, node_id: str) -> str:
        return short or long_ or fallback_display_name(node_id)

    def _neighbour_row(r: sqlite3.Row) -> str:
        tail = tpl.esc(fallback_display_name(r["node_id"]))
        name = tpl.esc(_name(r["short_name"], r["long_name"], r["node_id"]))
        return (
            f'<tr><td><a href="/nodes/{tpl.esc(r["node_id"])}">{tail}</a></td>'
            f"<td>{name}</td><td>{r['count']}</td>"
            f"<td>{tpl.esc(_fmt_utc(r['last_seen']))}</td></tr>"
        )

    neighbour_table = "".join(_neighbour_row(r) for r in neighbours) or (
        '<tr><td colspan="4" class="muted">no direct neighbours in the last 7 days</td></tr>'
    )
    stable_table = "".join(
        f'<tr><td><a href="/nodes/{tpl.esc(r["node_id"])}">'
        f'{tpl.esc(_row_node_label(r))}</a></td>'
        f"<td>{r['days_seen']} / 7</td></tr>"
        for r in stable_rows
    ) or '<tr><td colspan="2" class="muted">none</td></tr>'
    appeared_table = "".join(_neighbour_row(r) for r in appeared) or (
        '<tr><td colspan="4" class="muted">none</td></tr>'
    )
    disappeared_table = "".join(_neighbour_row(r) for r in disappeared) or (
        '<tr><td colspan="4" class="muted">none</td></tr>'
    )
    two_hop_table = "".join(
        f'<tr><td><a href="/nodes/{tpl.esc(r["node_id"])}">'
        f'{tpl.esc(fallback_display_name(r["node_id"]))}</a></td>'
        f"<td>{tpl.esc(_name(r['short_name'], r['long_name'], r['node_id']))}</td>"
        f"<td>{tpl.esc(_fmt_utc(r['observed_last_seen']))}</td></tr>"
        for r in two_hop_rows
    ) or '<tr><td colspan="3" class="muted">none</td></tr>'

    svg = _topology_svg(neighbours, two_hop_rows)

    body = f"""
<h1>Topology</h1>
<p class="muted">Observed/inferred only: Meshtastic gives no complete link knowledge. Every edge
here is a locally inferred "we heard this node direct" observation, never a confirmed physical
route.</p>
{svg}
<h2>Most frequently heard direct neighbours (7 d)</h2>
<table><thead><tr><th>Id</th><th>Name</th><th>Count</th><th>Last seen</th></tr></thead>
<tbody>{neighbour_table}</tbody></table>

<h2>Stable neighbours (seen &ge;5 of 7 days)</h2>
<table><thead><tr><th>Id</th><th>Days seen</th></tr></thead><tbody>{stable_table}</tbody></table>

<h2>Links appeared in the last 24 h</h2>
<table><thead><tr><th>Id</th><th>Name</th><th>Count</th><th>Last seen</th></tr></thead>
<tbody>{appeared_table}</tbody></table>

<h2>Links disappeared in the last 24 h</h2>
<p class="muted">Best-effort inference: a link last heard 24-48 h ago and not since. Not proof
the node is gone -- only that it has not been heard direct in the last day.</p>
<table><thead><tr><th>Id</th><th>Name</th><th>Count</th><th>Last seen</th></tr></thead>
<tbody>{disappeared_table}</tbody></table>

<h2>2-hop nodes ({len(two_hop_rows)})</h2>
<p class="muted">Drawn unattached on the outer ring: Meshtastic packets do not report which
neighbour relayed them, so their placement here is not derived from real relay data.</p>
<table><thead><tr><th>Id</th><th>Name</th><th>Last seen</th></tr></thead>
<tbody>{two_hop_table}</tbody></table>
"""
    return tpl.layout(title="Topology", active="/topology", body=body)


# ---------------------------------------------------------------------------
# /propagation (batch 3b)
# ---------------------------------------------------------------------------


def _page_propagation(cfg: Config, conn: sqlite3.Connection) -> bytes:
    now = int(time.time())
    obs = _observatory_location(cfg)

    since14 = _day_floor(now, 14)
    # Bound the number of distinct direct nodes considered (not just rows):
    # each qualifies on up to 14 daily rows, so limiting rows alone wouldn't
    # cap the node count. Pick the most recently active nodes first.
    capped_node_ids = [
        r["node_id"]
        for r in conn.execute(
            "SELECT node_id, MAX(day) AS last_day FROM node_daily "
            "WHERE day >= ? AND direct_packets > 0 GROUP BY node_id "
            "ORDER BY last_day DESC LIMIT ?",
            (since14, _PROPAGATION_MAX_NODES),
        ).fetchall()
    ]
    rows: list[sqlite3.Row] = []
    if capped_node_ids:
        placeholders = ",".join("?" for _ in capped_node_ids)
        rows = conn.execute(
            "SELECT node_id, day, best_snr, typical_snr, direct_packets, last_lat, last_lon "
            f"FROM node_daily WHERE day >= ? AND direct_packets > 0 "
            f"AND node_id IN ({placeholders}) ORDER BY node_id, day",
            [since14, *capped_node_ids],
        ).fetchall()

    by_node: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_node.setdefault(r["node_id"], []).append(r)

    node_names: dict[str, str] = {}
    if by_node:
        placeholders = ",".join("?" for _ in by_node)
        for n in conn.execute(
            f"SELECT node_id, short_name, long_name FROM nodes WHERE node_id IN ({placeholders})",
            list(by_node),
        ).fetchall():
            node_names[n["node_id"]] = (
                n["short_name"] or n["long_name"] or fallback_display_name(n["node_id"])
            )

    sections = []
    for node_id, node_rows in by_node.items():
        name = tpl.esc(node_names.get(node_id, fallback_display_name(node_id)))
        table_rows = "".join(
            f"<tr><td>{tpl.esc(r['day'])}</td><td>{tpl.na(r['best_snr'], '{:+.1f}')}</td>"
            f"<td>{tpl.na(r['typical_snr'], '{:+.1f}')}</td></tr>"
            for r in node_rows
        )
        heat = tpl.svg_heat_strip([r["day"] for r in node_rows], [r["best_snr"] for r in node_rows])
        sections.append(
            f"<h3>{tpl.esc(fallback_display_name(node_id))} &mdash; {name}</h3>{heat}"
            "<table><thead><tr><th>Day</th><th>Best SNR</th>"
            "<th>Mean SNR (typical)</th></tr></thead>"
            f"<tbody>{table_rows}</tbody></table>"
        )
    sections_html = "".join(sections) or '<p class="muted">no direct nodes in the last 14 days</p>'

    if obs is None:
        unusual_html = (
            '<p class="muted">Needs both node positions and [observatory] '
            "latitude/longitude -- observatory location is not configured.</p>"
        )
    else:
        # Only plausible RF distances feed "unusual distance": every row
        # here already came from a direct_packets > 0 day (so hops_away ==
        # 0 for at least one packet that day -- see collector._record_
        # node_daily), so is_distance_plausible_for_hops is checked against
        # hops_away=0 (max_direct_km). node_daily has no per-day precision_
        # bits column, so the node's current (latest known) precision is
        # used as a best-effort proxy for the position-plausibility check.
        precision = _latest_precision_bits(conn)
        per_day: dict[str, float] = {}
        for r in rows:
            if r["last_lat"] is None or r["last_lon"] is None:
                continue
            if not is_position_plausible(
                r["last_lat"], r["last_lon"], precision.get(r["node_id"])
            ):
                continue
            d = haversine_km(obs[0], obs[1], r["last_lat"], r["last_lon"])
            if not is_distance_plausible_for_hops(
                d,
                0,
                max_direct_km=cfg.observatory.max_direct_km,
                max_km_per_hop=cfg.observatory.max_km_per_hop,
                max_observed_km=cfg.observatory.max_observed_km,
            ):
                continue
            if d > per_day.get(r["day"], -1.0):
                per_day[r["day"]] = d
        if not per_day:
            unusual_html = '<p class="muted">No direct-node positions in the last 14 days.</p>'
        else:
            median = statistics.median(per_day.values())
            threshold = median * 1.5
            unusual_days = sorted((day, dist) for day, dist in per_day.items() if dist > threshold)
            flagged = (
                ", ".join(f"{tpl.esc(day)} ({dist:.0f} km)" for day, dist in unusual_days)
                if unusual_days
                else "none"
            )
            distance_rows = "".join(
                f"<tr><td>{tpl.esc(day)}</td><td>{dist:.0f} km</td>"
                f"<td>{'yes' if dist > threshold else 'no'}</td></tr>"
                for day, dist in sorted(per_day.items())
            )
            unusual_html = (
                f"<p>14-day median furthest-direct distance: {median:.0f} km "
                f"(1.5&times; = {threshold:.0f} km). Unusual days: {flagged}.</p>"
                "<table><thead><tr><th>Day</th><th>Furthest direct</th>"
                f"<th>Unusual?</th></tr></thead><tbody>{distance_rows}</tbody></table>"
            )

    body = f"""
<h1>Propagation</h1>
<h2>SNR by day, direct nodes (last 14 d)</h2>
{sections_html}
<h2>Unusual distance</h2>
{unusual_html}
<h2>Weather</h2>
<p class="muted">TODO: no weather correlation in this build (phase 2 ruling: propagation pages
work from SNR/RSSI/distance history alone).</p>
"""
    return tpl.layout(title="Propagation", active="/propagation", body=body)


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

_ROUTE_STATIC = {
    "/": "now",
    "/health": "health",
    "/traffic": "traffic",
    "/guestbook": "guestbook",
    "/boards": "boards",
    "/records": "records",
    "/digest": "digest_index",
    "/nodes": "nodes",
    "/map": "map",
    "/topology": "topology",
    "/propagation": "propagation",
    "/api/summary.json": "summary",
    "/api/records.json": "records_json",
    "/api/guestbook.json": "guestbook_json",
    "/api/topology.json": "topology_json",
    "/api/iching.json": "iching_json",
    "/api/haiku.json": "haiku_json",
    "/api/daily.json": "daily_json",
    "/api/week.json": "week_json",
}
_DIGEST_DAY_RE = re.compile(r"^/digest/([^/]+)$")
_NODE_PROFILE_RE = re.compile(r"^/nodes/([^/]+)$")


_HTML_CONTENT_TYPE = "text/html; charset=utf-8"
_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_SUMMARY_RECENT_NODES = 10
# What fits on the e-paper panel that reads these two endpoints.
_API_GUESTBOOK_LIMIT = 8
_API_TOPIC_LIMIT = 5
# Status-file fields worth passing through to a display; everything else in
# status.json is daemon-internal.
_SUMMARY_STATUS_KEYS = (
    "radio_connected",
    "reply_ready",
    "uptime_seconds",
    "queue_depth",
    "last_packet_at",
)


def _mesh_health_json(conn: sqlite3.Connection, now: int) -> dict[str, Any]:
    """Link-quality aggregates over the last 24 h for external displays.
    Hop figures count unique RF packets only: a duplicate is the mesh echoing
    a packet we already have, and an MQTT-borne packet carries hop fields
    that say nothing about our radio's reach (same rule as the records page).
    Channel utilisation is the median of our own radio's samples over the
    last hour, so it tracks the airwaves now rather than the day's average."""
    since_24h = now - 86400
    row = conn.execute(
        "SELECT COUNT(*) AS raw, "
        "SUM(CASE WHEN is_duplicate = 0 THEN 1 ELSE 0 END) AS unique_packets "
        "FROM observations WHERE server_time >= ?",
        (since_24h,),
    ).fetchone()
    hop_rows = conn.execute(
        "SELECT hops_away, COUNT(*) AS n FROM observations "
        "WHERE server_time >= ? AND is_duplicate = 0 AND hops_away IS NOT NULL "
        "AND hops_away >= 0 AND (via_mqtt IS NULL OR via_mqtt = 0) "
        "GROUP BY hops_away ORDER BY hops_away",
        (since_24h,),
    ).fetchall()
    hop_counts: list[int] = []
    for r in hop_rows:
        hop_counts.extend([0] * (r["hops_away"] + 1 - len(hop_counts)))
        hop_counts[r["hops_away"]] = r["n"]
    with_hops = sum(hop_counts)
    positioned = sum(
        is_position_plausible(row["last_position_lat"], row["last_position_lon"], None)
        for row in conn.execute(
            "SELECT last_position_lat, last_position_lon FROM nodes "
            "WHERE observed_last_seen >= ? AND last_position_lat IS NOT NULL "
            "AND last_position_lon IS NOT NULL", (since_24h,),
        )
    )
    samples = conn.execute(
        "SELECT channel_utilization, air_util_tx FROM local_radio_samples "
        "WHERE server_time >= ?",
        (now - 3600,),
    ).fetchall()
    util = [s["channel_utilization"] for s in samples if s["channel_utilization"] is not None]
    air_tx = [s["air_util_tx"] for s in samples if s["air_util_tx"] is not None]
    unique = row["unique_packets"] or 0
    return {
        "direct_share": (hop_counts[0] / with_hops) if with_hops else None,
        "echo_rate": (row["raw"] / unique) if unique else None,
        "avg_hops": (
            sum(i * n for i, n in enumerate(hop_counts)) / with_hops if with_hops else None
        ),
        "hop_counts": hop_counts,
        "nodes_with_position": positioned,
        "channel_utilization": statistics.median(util) if util else None,
        "air_util_tx": statistics.median(air_tx) if air_tx else None,
    }


def _bot_today_json(digest: dict) -> dict[str, Any]:
    """Today's BBS usage from the digest: bounded command names and reply
    outcomes, counts only."""
    return {
        "commands": digest["commands"],
        "replies": digest["replies"],
        "signatures": digest["guestbook"]["signatures"],
        "book_solves": digest["book_solves"],
        "wordle_solves": digest["wordle_solves"],
    }


def _api_summary(cfg: Config, conn: sqlite3.Connection, status: dict, stale: bool) -> bytes:
    """Aggregate JSON for external displays (e.g. an e-paper panel on the
    same host). Reuses build_digest for today's numbers so the figures match
    the /digest page, and _hourly_activity for the same series the front
    page charts. Node IDs and short names appear exactly as on /nodes; no
    guestbook text, positions or message content."""
    now = int(time.time())
    tz = cfg.service.timezone
    day = local_day(now, tz)
    digest = build_digest(conn, day=day, tz=tz, now=now)
    labels, packets, senders = _hourly_activity(conn, now, tz)
    best_snr_24h = Collector(conn).best_snr_since(now - 86400)
    rows = conn.execute(
        "SELECT n.node_id, n.short_name, n.observed_last_seen, "
        "(SELECT o.rx_snr FROM observations o WHERE o.sender_node_id = n.node_id "
        " AND o.rx_snr IS NOT NULL ORDER BY o.server_time DESC LIMIT 1) AS snr "
        "FROM nodes n ORDER BY n.observed_last_seen DESC LIMIT ?",
        (_SUMMARY_RECENT_NODES,),
    ).fetchall()
    payload = {
        "generated_at": now,
        "tz": tz,
        "day": day,
        "stale": stale,
        "packets_today": digest["packets"],
        "distinct_senders_today": digest["distinct_senders"],
        "new_nodes_today": digest["new_nodes"],
        "busiest_hour": digest["busiest_hour"],
        "guestbook_visible": Guestbook(conn, cfg.service).visible_count(),
        "best_snr_24h": best_snr_24h,
        "hourly": {
            "labels": labels,
            "packets": [int(p) for p in packets],
            "senders": [int(s) for s in senders],
        },
        "recent_nodes": [
            {
                "node_id": r["node_id"],
                "short_name": r["short_name"],
                "last_heard": r["observed_last_seen"],
                "snr": r["snr"],
            }
            for r in rows
        ],
        "status": {key: status.get(key) for key in _SUMMARY_STATUS_KEYS},
        "mesh_24h": _mesh_health_json(conn, now),
        "bot_today": _bot_today_json(digest),
    }
    return json.dumps(payload).encode("utf-8")


def _window_json(w: dict[str, Any], biggest: tuple[str, int]) -> dict[str, Any]:
    best = w["best_snr"]
    observed = w["furthest_observed"]
    located = w["furthest_observed_incl_mqtt"]
    return {
        "best_snr_db": None if best is None else best[0],
        "best_snr_name": None if best is None else _name_and_tail(w["best_snr_name"], best[1]),
        "max_hops": w["max_hops"],
        "busiest_hour": w["busiest_hour"],
        "biggest_day": list(biggest),
        # Separate from verified RF range: includes MQTT and unknown hops.
        "furthest_observed_incl_mqtt_km": None if located is None else located[0],
        "furthest_observed_incl_mqtt_name": (
            None if located is None
            else _name_and_tail(w["furthest_observed_incl_mqtt_name"], located[1])
        ),
        "furthest_direct_km": w["furthest_direct"],
        "furthest_observed_km": None if observed is None else observed[0],
        "furthest_observed_name": (
            None
            if observed is None
            else _name_and_tail(w["furthest_observed_name"], observed[1])
        ),
    }


def _api_records(cfg: Config, conn: sqlite3.Connection, status: dict, stale: bool) -> bytes:
    """Records as JSON for external displays. Reuses _records_windows so the
    figures match the /records page exactly. Aggregates only: no guestbook
    text, no positions."""
    book_rows = book_stats_summary(conn)
    # since_day bounds the window to the last 7 calendar days, not just the
    # newest 7 wordle_daily rows (which can span more days if play is sparse).
    wordle_rows = wordle_stats_summary(conn, days=7, since_day=_day_floor(int(time.time()), 6))
    payload = {
        "generated_at": int(time.time()),
        "tz": cfg.service.timezone,
        "stale": stale,
        "windows": {
            name: _window_json(w, biggest)
            for name, (w, biggest) in _records_windows(cfg, conn).items()
        },
        "games": {
            "book_solves": sum(r.solves for r in book_rows),
            "book_guesses": sum(r.guesses for r in book_rows),
            "wordle_solves_7d": sum(r.solves for r in wordle_rows),
        },
    }
    return json.dumps(payload).encode("utf-8")


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, 0..360 clockwise from north."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360.0


def _api_topology(cfg: Config, conn: sqlite3.Connection, stale: bool) -> bytes:
    """Hop rings plus other recently heard nodes for external displays.
    The neighbour and 2-hop sets match /topology. A node with a known position
    and a configured observatory location also gets its bearing and distance --
    polar coordinates only, never the position itself."""
    now = int(time.time())
    neighbours, two_hop_rows = _topology_rows(conn, now)
    obs = cfg.observatory
    has_origin = obs.latitude != 0.0 or obs.longitude != 0.0
    # A geographic map also needs heard nodes whose route is unknown. Keep
    # these separate so existing topology consumers retain their hop rings.
    represented = {r["node_id"] for r in neighbours + two_hop_rows}
    recent = conn.execute(
        "SELECT node_id, short_name, long_name, observed_last_seen FROM nodes "
        "WHERE observed_last_seen >= ? ORDER BY observed_last_seen DESC, node_id LIMIT ?",
        (now - 7 * 86400, _TOPOLOGY_MAX_ROWS),
    ).fetchall()
    other_rows = [r for r in recent if r["node_id"] not in represented]
    classes = _classify_nodes(conn, now)
    ids = list(represented) + [r["node_id"] for r in other_rows]
    positions: dict[str, tuple[float, float]] = {}
    if has_origin and ids:
        placeholders = ",".join("?" for _ in ids)
        for row in conn.execute(
            f"SELECT node_id, last_position_lat, last_position_lon FROM nodes "
            f"WHERE node_id IN ({placeholders}) AND last_position_lat IS NOT NULL "
            f"AND last_position_lon IS NOT NULL",
            ids,
        ):
            positions[row["node_id"]] = (row["last_position_lat"], row["last_position_lon"])

    routes = {}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        for row in conn.execute(
            f"SELECT node_id, stored_route FROM nodes WHERE node_id IN ({placeholders}) "
            "AND stored_route IS NOT NULL", ids,
        ):
            routes[row["node_id"]] = json.loads(row["stored_route"])

    def _node(r: sqlite3.Row, hops: int | None, last_seen: int | None, count: int | None) -> dict:
        entry: dict[str, Any] = {
            "node_id": r["node_id"],
            "name": r["short_name"] or r["long_name"] or fallback_display_name(r["node_id"]),
            "hops": hops,
            "last_seen": last_seen,
            "count": count,
            "bearing_deg": None,
            "distance_km": None,
            "stored_route": routes.get(r["node_id"]),
        }
        pos = positions.get(r["node_id"])
        if pos is not None and is_position_plausible(pos[0], pos[1], None):
            entry["bearing_deg"] = round(_bearing_deg(obs.latitude, obs.longitude, *pos), 1)
            entry["distance_km"] = round(haversine_km(obs.latitude, obs.longitude, *pos), 1)
        return entry

    payload = {
        "generated_at": now,
        "tz": cfg.service.timezone,
        "stale": stale,
        "nodes": [_node(r, 1, r["last_seen"], r["count"]) for r in neighbours]
        + [_node(r, 2, r["observed_last_seen"], None) for r in two_hop_rows],
        "other_nodes": [
            {**_node(r, None, r["observed_last_seen"], None),
             "route": "relayed" if classes.get(r["node_id"]) == "3+ hops" else "unknown"}
            for r in other_rows
        ],
    }
    return json.dumps(payload).encode("utf-8")


def _api_iching(cfg: Config) -> bytes:
    """The observatory's own hexagram for today: the same derivation ICHING
    uses for a visitor, keyed on this node's id, so it is fixed for the local
    day and never reveals any visitor's draw."""
    now = int(time.time())
    day = local_day(now, cfg.service.timezone)
    number = hexagram_for(cfg.radio.expected_node_id, day, cfg.service.fortune_salt)
    h = HEXAGRAMS[number - 1]
    payload = {
        "generated_at": now,
        "tz": cfg.service.timezone,
        "day": day,
        "number": h.number,
        "pinyin": h.pinyin,
        "english": h.english,
        "judgment": h.judgment,
    }
    return json.dumps(payload).encode("utf-8")


def _haiku_payload(cfg: Config, conn: sqlite3.Connection) -> dict:
    """The garden's haiku for the local day. HAIKU draws at random per
    request; a display wants one fixed poem a day, so the RNG is seeded from
    the salt and the day. The live counts inside it still move."""
    now = int(time.time())
    day = local_day(now, cfg.service.timezone)
    seed = hashlib.sha256(f"{cfg.service.fortune_salt}|haiku|{day}".encode()).digest()
    text = haiku_text(
        senders_24h=Collector(conn).distinct_senders_since(now - 86400),
        guestbook_visible=Guestbook(conn, cfg.service).visible_count(),
        place=cfg.service.haiku_place,
        rng=random.Random(int.from_bytes(seed[:8], "big")),
    )
    lines = text.split("\n")
    # The DM reply ends in the sprout emoji; a display draws its own mark.
    lines[-1] = lines[-1].removesuffix(" \U0001f331")
    return {
        "generated_at": now,
        "tz": cfg.service.timezone,
        "day": day,
        "lines": lines,
    }


def _api_haiku(cfg: Config, conn: sqlite3.Connection) -> bytes:
    """`/api/haiku.json` -- the day's haiku, whatever `[service]
    daily_display` says."""
    return json.dumps(_haiku_payload(cfg, conn)).encode("utf-8")


def _api_daily(cfg: Config, conn: sqlite3.Connection) -> bytes:
    """`/api/daily.json` -- the daily draw `[service] daily_display`
    selects, tagged with a `kind` field so a display can tell what it is
    rendering. "haiku" is the only kind today."""
    payload = _haiku_payload(cfg, conn)
    return json.dumps({"kind": cfg.service.daily_display, **payload}).encode("utf-8")


def _api_week(cfg: Config, conn: sqlite3.Connection, stale: bool) -> bytes:
    """The last seven local days as digests, today first. Past days come from
    the written digest files when present and are built live otherwise, so
    the feed does not depend on the digest job having run."""
    now = int(time.time())
    tz = cfg.service.timezone
    days = []
    today = date.fromisoformat(local_day(now, tz))
    for back in range(7):
        day = (today - timedelta(days=back)).isoformat()
        digest = _load_digest(cfg, day) if back else None
        if digest is None:
            digest = build_digest(conn, day=day, tz=tz, now=now)
        days.append(digest)
    payload = {"generated_at": now, "tz": tz, "stale": stale, "days": days}
    return json.dumps(payload).encode("utf-8")


def _api_guestbook(cfg: Config, conn: sqlite3.Connection, status: dict, stale: bool) -> bytes:
    """The latest visible guestbook entries for external displays -- the same
    text the /guestbook page already serves. Visible only: hidden entries and
    their moderation reasons never leave through here."""
    gb = Guestbook(conn, cfg.service)
    now = int(time.time())
    tz = cfg.service.timezone
    # Same digest as /api/summary.json, so both feeds quote one set of numbers.
    digest = build_digest(conn, day=local_day(now, tz), tz=tz, now=now)
    payload = {
        "generated_at": now,
        "tz": tz,
        "stale": stale,
        "visible_count": gb.visible_count(),
        "bot_today": _bot_today_json(digest),
        "topics": [{"topic": t, "count": c} for t, c in gb.topic_counts(_API_TOPIC_LIMIT)],
        "entries": [
            {
                "entry_id": e.entry_id,
                "created_at": e.created_at,
                "name": e.display_name or fallback_display_name(e.sender_node_id),
                "message": e.message,
                "topic": e.topic,
            }
            for e in gb.latest_visible(_API_GUESTBOOK_LIMIT)
        ],
    }
    return json.dumps(payload).encode("utf-8")


def _not_found(message: str = "not found") -> tuple[HTTPStatus, str, bytes]:
    return HTTPStatus.NOT_FOUND, _HTML_CONTENT_TYPE, tpl.error_page(404, message)


def _ok(body: bytes) -> tuple[HTTPStatus, str, bytes]:
    return HTTPStatus.OK, _HTML_CONTENT_TYPE, body


def _dispatch(cfg: Config, path: str, query: dict[str, list[str]]) -> tuple[HTTPStatus, str, bytes]:
    name = _ROUTE_STATIC.get(path)
    if name is None:
        m = _DIGEST_DAY_RE.match(path)
        if m:
            day = m.group(1)
            if not _DAY_RE.match(day) or not _valid_calendar_day(day):
                return _not_found()
            body = _page_digest_day(cfg, day)
            if body is None:
                return _not_found("digest not found")
            return _ok(body)

        m = _NODE_PROFILE_RE.match(path)
        if m:
            node_id = m.group(1)
            if not _NODE_ID_RE.match(node_id):
                return _not_found()
            conn = _ro_connect(cfg)
            try:
                body = _page_node_profile(cfg, conn, node_id.lower())
            finally:
                conn.close()
            if body is None:
                return _not_found("node not found")
            return _ok(body)

        return _not_found()

    if name == "digest_index":
        return _ok(_page_digest_index(cfg))

    status, stale = read_status(cfg)
    conn = _ro_connect(cfg)
    try:
        if name == "now":
            body = _page_now(cfg, conn, status, stale)
        elif name == "health":
            body = _page_health(cfg, conn, status, stale)
        elif name == "traffic":
            body = _page_traffic(cfg, conn, status, stale)
        elif name == "guestbook":
            page = _parse_page(query)
            topic = _parse_topic(query)
            body = _page_guestbook(cfg, conn, status, stale, page, topic)
        elif name == "boards":
            body = _page_boards(cfg, conn)
        elif name == "records":
            body = _page_records(cfg, conn, status, stale)
        elif name == "nodes":
            page = _parse_page(query)
            body = _page_nodes(cfg, conn, status, stale, page)
        elif name == "map":
            body = _page_map(cfg, conn, query)
        elif name == "topology":
            body = _page_topology(cfg, conn)
        elif name == "propagation":
            body = _page_propagation(cfg, conn)
        elif name == "summary":
            return HTTPStatus.OK, _JSON_CONTENT_TYPE, _api_summary(cfg, conn, status, stale)
        elif name == "records_json":
            return HTTPStatus.OK, _JSON_CONTENT_TYPE, _api_records(cfg, conn, status, stale)
        elif name == "guestbook_json":
            return HTTPStatus.OK, _JSON_CONTENT_TYPE, _api_guestbook(cfg, conn, status, stale)
        elif name == "topology_json":
            return HTTPStatus.OK, _JSON_CONTENT_TYPE, _api_topology(cfg, conn, stale)
        elif name == "iching_json":
            return HTTPStatus.OK, _JSON_CONTENT_TYPE, _api_iching(cfg)
        elif name == "haiku_json":
            return HTTPStatus.OK, _JSON_CONTENT_TYPE, _api_haiku(cfg, conn)
        elif name == "daily_json":
            return HTTPStatus.OK, _JSON_CONTENT_TYPE, _api_daily(cfg, conn)
        elif name == "week_json":
            return HTTPStatus.OK, _JSON_CONTENT_TYPE, _api_week(cfg, conn, stale)
        else:  # pragma: no cover -- defensive, unreachable given _ROUTE_STATIC above
            return _not_found()
    finally:
        conn.close()
    return _ok(body)


def _valid_calendar_day(day: str) -> bool:
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return False
    return True


_MAX_PAGE = 10000


def _parse_page(query: dict[str, list[str]]) -> int:
    """Parses `?page=`, folding anything unparseable or out of the allowed
    1..._MAX_PAGE range (too small, too large, or plain garbage) back to
    page 1 -- one consistent fallback rather than a 404 for some malformed
    values and a silent clamp for others."""
    raw = query.get("page", ["1"])[0]
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return 1
    if page < 1 or page > _MAX_PAGE:
        return 1
    return page


_CLI_TOPIC_RE = re.compile(r"^[a-z0-9]{2,12}$")


def _parse_topic(query: dict[str, list[str]]) -> str | None:
    """Parses `?topic=` (mirrors `_parse_page`): missing, blank, or anything
    not matching the topic shape (after `strip().removeprefix("#").lower()`)
    folds back to None -- the main guestbook, unchanged behavior."""
    raw = query.get("topic", [""])[0].strip().removeprefix("#").lower()
    if not _CLI_TOPIC_RE.match(raw):
        return None
    return raw


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _DashboardHandler(BaseHTTPRequestHandler):
    # Set by _make_handler_class below, once, before the server ever
    # accepts a connection -- constant for the handler class's lifetime.
    cfg: Config

    protocol_version = "HTTP/1.1"
    server_version = "MeshgardenDashboard/1"
    sys_version = ""  # never advertise the Python interpreter version

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.info("web: %s - %s", self.address_string(), format % args)

    def _write(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            status_code, content_type, body = _dispatch(self.cfg, parsed.path, query)
        except Exception:
            logger.exception("web: unhandled error rendering %s", self.path)
            status_code = HTTPStatus.INTERNAL_SERVER_ERROR
            content_type = "text/html; charset=utf-8"
            body = tpl.error_page(500, "internal error")
        self._write(status_code, content_type, body)

    def _reject_write(self) -> None:
        body = tpl.error_page(405, "read-only dashboard: this method is not supported")
        self._write(HTTPStatus.METHOD_NOT_ALLOWED, "text/html; charset=utf-8", body)

    do_HEAD = do_GET
    do_POST = _reject_write
    do_PUT = _reject_write
    do_DELETE = _reject_write
    do_PATCH = _reject_write


def _make_handler_class(cfg: Config) -> type[BaseHTTPRequestHandler]:
    return type("DashboardHandler", (_DashboardHandler,), {"cfg": cfg})


_ServerAndThread = tuple[ThreadingHTTPServer, threading.Thread] | tuple[None, None]


def start_web_server(cfg: Config) -> _ServerAndThread:
    """Starts the dashboard on a daemon thread, or does nothing (returns
    `(None, None)`) when `[web] enabled` is false. Raises `WebRefused`
    instead of binding if `[web] listen` is not loopback and
    `[web] allow_lan` was not explicitly set."""
    if not cfg.web.enabled:
        return None, None
    _check_bind_allowed(cfg.web)
    handler_cls = _make_handler_class(cfg)
    httpd = ThreadingHTTPServer((cfg.web.listen, cfg.web.port), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, name="web-server", daemon=True)
    thread.start()
    return httpd, thread


def stop_web_server(httpd: ThreadingHTTPServer | None, thread: threading.Thread | None) -> None:
    if httpd is None:
        return
    httpd.shutdown()
    if thread is not None:
        thread.join(timeout=5)
    httpd.server_close()
