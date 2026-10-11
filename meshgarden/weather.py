"""NWS weather alerts on a public MeshCore channel.

Spec: docs/superpowers/plans/2026-10-10-weather-alerts-spec.md. Three
units live here. `parse_active_alerts`/`format_alert_line` are pure.
`WeatherLedger` (Task 4) owns the `weather_alerts` table and runs on the
DB worker thread. `WeatherPoller` (Task 5) is the only thread that talks
HTTP and never touches SQLite or the transport: it emits `WeatherBatch`
objects into the daemon's ingest queue, exactly like a MeshCore contact
event. Nothing in this module imports the transport; the daemon turns a
`WeatherSendPlan` into an ordinary `ReplyJob` for `Sender`.

This is the second documented exception to "the garden transmits nothing
but a reply" (the first is the periodic self-advert): unlike the advert
it is message traffic, so it is off by default, confined to one channel,
Warnings and Watches only, and capped per hour.
"""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from meshgarden import __version__

if TYPE_CHECKING:
    from meshgarden.config import WeatherConfig

log = logging.getLogger(__name__)

SEVERITY_RANK: dict[str, int] = {
    "Minor": 0, "Moderate": 1, "Severe": 2, "Extreme": 3, "Unknown": 4,
}
_MESSAGE_TYPES = frozenset({"Alert", "Update", "Cancel"})
_PREFIX = {"Alert": "⚠", "Update": "↻", "Cancel": "✓"}
_FALLBACK_EXPIRY_SECONDS = 6 * 3600
_OTHER = "other"

# NWS forecast zone -> short name that fits a 120-byte line. Spec section 4.
DEFAULT_ZONE_ALIASES: dict[str, str] = {
    "CAZ006": "SF",
    "CAZ508": "Bay Shoreline",
    "CAZ509": "Peninsula Coast",
    "CAZ515": "East Bay Hills",
    "CAZ510": "East Bay Valleys",
    "CAZ513": "Santa Clara Valley",
    "CAZ514": "E Santa Clara Hills",
    "CAZ512": "Santa Cruz Mtns",
    "CAZ505": "Coastal North Bay",
    "CAZ506": "North Bay Valleys",
    "CAZ504": "North Bay Mtns",
    "CAZ502": "Marin Coast Range",
    "CAZ503": "Sonoma Coast Range",
    "CAZ151": "Solano",
    "CAZ153": "Solano",
    "CAZ155": "Solano",
    "CAZ156": "Solano",
}


def effective_aliases(overrides: Mapping[str, str]) -> dict[str, str]:
    return {**DEFAULT_ZONE_ALIASES, **overrides}


@dataclass(frozen=True)
class WeatherProduct:
    """One NWS issuance after grouping its per-zone features together."""

    product_key: str          # smallest alert id in the group
    alert_ids: tuple[str, ...]
    references: tuple[str, ...]  # ids of earlier issuances this one supersedes
    event: str
    message_type: str         # Alert | Update | Cancel (NWS's own)
    severity: str
    zones: tuple[str, ...]    # CAZ... codes, NWS order, deduped
    sent_at: int              # UTC epoch seconds
    expires_at: int           # UTC epoch seconds


def _parse_iso(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _passes_filters(props: dict, suffixes: Sequence[str], min_rank: int) -> bool:
    if props.get("status") != "Actual":
        return False
    if props.get("messageType") not in _MESSAGE_TYPES:
        return False
    event = props.get("event")
    if not isinstance(event, str) or not event.strip():
        return False
    last = event.strip().split()[-1].lower()
    if last not in {s.lower() for s in suffixes}:
        return False
    rank = SEVERITY_RANK.get(str(props.get("severity")), SEVERITY_RANK["Unknown"])
    return rank == SEVERITY_RANK["Unknown"] or rank >= min_rank


def parse_active_alerts(
    payload: dict, *, event_suffixes: Sequence[str], min_severity: str
) -> list[WeatherProduct]:
    """Filters and groups one `alerts/active` GeoJSON payload.

    Grouping key: NWS splits one issuance into several features by zone
    group. Features that share (event, sender, messageType, NWSheadline or
    sent) are one product; its zones are merged in NWS order.
    """
    min_rank = SEVERITY_RANK.get(min_severity, SEVERITY_RANK["Moderate"])
    groups: dict[tuple, list[dict]] = {}
    for feat in payload.get("features") or []:
        props = feat.get("properties") or {}
        if not _passes_filters(props, event_suffixes, min_rank):
            continue
        headline = (props.get("parameters") or {}).get("NWSheadline") or [None]
        key = (
            props["event"].strip(),
            props.get("senderName"),
            props["messageType"],
            headline[0] or props.get("sent"),
        )
        groups.setdefault(key, []).append(props)

    products: list[WeatherProduct] = []
    for (event, _sender, mtype, _h), members in groups.items():
        # Merge in id order so zone order is deterministic however NWS
        # happened to order the features in the payload.
        members = sorted(members, key=lambda m: str(m.get("id") or ""))
        ids = tuple(sorted(str(m.get("id") or "") for m in members))
        zones: list[str] = []
        refs: list[str] = []
        for m in members:
            for ugc in (m.get("geocode") or {}).get("UGC") or []:
                if isinstance(ugc, str) and ugc[2:3] == "Z" and ugc not in zones:
                    zones.append(ugc)
            for ref in m.get("references") or []:
                ident = ref.get("identifier") if isinstance(ref, dict) else None
                if isinstance(ident, str) and ident not in refs:
                    refs.append(ident)
        first = members[0]
        sent_at = _parse_iso(first.get("sent")) or int(datetime.now(UTC).timestamp())
        expires_at = (
            _parse_iso(first.get("ends"))
            or _parse_iso(first.get("expires"))
            or sent_at + _FALLBACK_EXPIRY_SECONDS
        )
        products.append(
            WeatherProduct(
                product_key=ids[0],
                alert_ids=ids,
                references=tuple(refs),
                event=event,
                message_type=mtype,
                severity=str(first.get("severity") or "Unknown"),
                zones=tuple(zones),
                sent_at=sent_at,
                expires_at=expires_at,
            )
        )
    return products


def zone_names(zones: Sequence[str], aliases: Mapping[str, str]) -> list[str]:
    names: list[str] = []
    other = False
    for z in zones:
        name = aliases.get(z)
        if name is None:
            other = True
        elif name not in names:
            names.append(name)
    if other or not names:
        names.append(_OTHER)
    return names


def _until(expires_at: int, tz: str) -> str:
    dt = datetime.fromtimestamp(expires_at, tz=ZoneInfo(tz))
    hour = dt.hour % 12 or 12
    return f"until {dt:%a} {hour}{'AM' if dt.hour < 12 else 'PM'}"


def _blen(s: str) -> int:
    return len(s.encode("utf-8"))


def _truncate_utf8(s: str, max_bytes: int) -> str:
    out = s.encode("utf-8")[:max_bytes]
    return out.decode("utf-8", errors="ignore")


def format_alert_line(
    product: WeatherProduct,
    *,
    kind: str,
    aliases: Mapping[str, str],
    timezone: str,
    max_bytes: int = 120,
) -> str:
    """One line, at most `max_bytes` UTF-8 bytes. Truncation order (spec
    section 4): drop area names from the end with `+N`, then drop the
    `until` clause, then cut the event name with `…`."""
    prefix = _PREFIX[kind]
    event = product.event + (" cancelled" if kind == "Cancel" else "")
    names = zone_names(product.zones, aliases)
    until = None if kind == "Cancel" else _until(product.expires_at, timezone)

    def build(shown: list[str], dropped: int, with_until: bool) -> str:
        areas = ", ".join(shown) + (f" +{dropped}" if dropped else "")
        parts = [f"{prefix} {event}", areas]
        if with_until and until:
            parts.append(until)
        return " · ".join(parts)

    dropped = 0
    line = build(names, dropped, True)
    while _blen(line) > max_bytes and len(names) > 1:
        names = names[:-1]
        dropped += 1
        line = build(names, dropped, True)
    if _blen(line) > max_bytes:
        line = build(names, dropped, False)
    if _blen(line) > max_bytes:
        ellipsis = "…"
        line = _truncate_utf8(line, max_bytes - _blen(ellipsis)) + ellipsis
    return line


_POLL_STALE_SECONDS = 3 * 120  # three default poll intervals without a success


def _age(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def wx_status_text(
    *,
    enabled: bool,
    counties: int,
    last_poll_ok_at: int | None,
    active: int,
    last_sent_at: int | None,
    now: int,
    timezone: str,
    lead: str = "wx-bay",
    max_bytes: int = 120,
) -> str:
    """Shared by the WX command and the daily heartbeat so both read alike."""
    if not enabled:
        return _truncate_utf8("🌦 weather alerts off", max_bytes)
    if last_poll_ok_at is None:
        nws = "NWS never"
    else:
        age = now - last_poll_ok_at
        nws = f"NWS down {_age(age)}" if age > _POLL_STALE_SECONDS else f"NWS ok {_age(age)} ago"
    if last_sent_at is None:
        sent = "never"
    else:
        dt = datetime.fromtimestamp(last_sent_at, tz=ZoneInfo(timezone))
        # strftime %-d is not portable; build month/day by hand.
        sent = f"{dt:%b} {dt.day} {dt:%H:%M}"
    # One field per line: a 120-byte MeshCore bubble reads better as a
    # short column than as one long dotted row (operator decision).
    line = f"🌦 {lead}\n{counties} counties\n{nws}\n{active} active\nlast sent {sent}"
    if _blen(line) > max_bytes:
        ellipsis = "…"
        line = _truncate_utf8(line, max_bytes - _blen(ellipsis)) + ellipsis
    return line


@dataclass(frozen=True)
class WeatherSendPlan:
    product_key: str
    kind: str   # Alert | Update | Cancel -- the prefix the line carries
    line: str


class WeatherLedger:
    """Owns the `weather_alerts` table. DB worker thread only.

    `apply` is the whole decision procedure (spec section 5): first
    sighting -> Alert; superseding Update with changed fields -> Update,
    unchanged -> suppressed; Cancel of something announced -> Cancel once;
    plain expiry -> silence. A plan is recorded `sent` optimistically; the
    daemon calls `mark(..., "dropped")` when the send never left the
    queue, and the next poll retries it.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        max_sends_per_hour: int,
        aliases: Mapping[str, str],
        timezone: str,
        max_bytes: int = 120,
    ) -> None:
        self._conn = conn
        self._cap = max_sends_per_hour
        self._aliases = dict(aliases)
        self._tz = timezone
        self._max_bytes = max_bytes
        # One entry per send, not per product: Alert+Update+Cancel on one
        # product overwrite a single row, so the table undercounts. Seeded
        # best-effort from `sent` rows so a restart does not reset the cap.
        now = int(time.time())
        self._sends: deque[int] = deque(
            r[0]
            for r in conn.execute(
                "SELECT updated_at FROM weather_alerts WHERE outcome = 'sent' AND updated_at > ?",
                (now - 3600,),
            )
        )

    # -- lookups -----------------------------------------------------------

    def _row_for(self, product: WeatherProduct) -> sqlite3.Row | None:
        row = self._conn.execute(
            "SELECT * FROM weather_alerts WHERE product_key = ?", (product.product_key,)
        ).fetchone()
        if row is not None:
            return row
        ids = tuple(product.references) + tuple(product.alert_ids)
        if not ids:
            return None
        marks = ",".join("?" * len(ids))
        return self._conn.execute(
            f"SELECT * FROM weather_alerts WHERE last_alert_id IN ({marks}) "
            "ORDER BY updated_at DESC LIMIT 1",
            ids,
        ).fetchone()

    def sent_in_last_hour(self, now: int) -> int:
        while self._sends and self._sends[0] <= now - 3600:
            self._sends.popleft()
        return len(self._sends)

    def active_count(self, now: int) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM weather_alerts WHERE message_type != 'Cancel' "
            "AND expires_at > ? AND outcome = 'sent'",
            (now,),
        ).fetchone()[0]

    def last_sent_at(self) -> int | None:
        return self._conn.execute(
            "SELECT MAX(updated_at) FROM weather_alerts WHERE outcome = 'sent'"
        ).fetchone()[0]

    def get_state(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM weather_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    # -- writes ------------------------------------------------------------

    def set_state(self, key: str, value: str, now: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO weather_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, now),
            )

    def _upsert(
        self,
        key: str,
        product: WeatherProduct,
        *,
        areas: str,
        kind: str,
        outcome: str,
        now: int,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO weather_alerts
                    (product_key, last_alert_id, event, areas, expires_at,
                     message_type, outcome, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_key) DO UPDATE SET
                    last_alert_id = excluded.last_alert_id,
                    event = excluded.event,
                    areas = excluded.areas,
                    expires_at = excluded.expires_at,
                    message_type = excluded.message_type,
                    outcome = excluded.outcome,
                    updated_at = excluded.updated_at
                """,
                (key, product.alert_ids[-1], product.event, areas, product.expires_at,
                 kind, outcome, now),
            )

    def mark(self, product_key: str, outcome: str, now: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE weather_alerts SET outcome = ?, updated_at = ? WHERE product_key = ?",
                (outcome, now, product_key),
            )

    def prune(self, now: int, keep_seconds: int = 7 * 86400) -> int:
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM weather_alerts WHERE expires_at < ?", (now - keep_seconds,)
            )
            return cur.rowcount

    # -- the decision procedure -------------------------------------------

    def apply(
        self, products: Sequence[WeatherProduct], now: int, *, max_plans: int = 2
    ) -> list[WeatherSendPlan]:
        plans: list[WeatherSendPlan] = []
        budget = self._cap - self.sent_in_last_hour(now)
        for product in products:
            try:
                if len(plans) >= max_plans:
                    break
                row = self._row_for(product)
                areas = ", ".join(zone_names(product.zones, self._aliases))

                if product.message_type == "Cancel":
                    if row is None or (
                        row["message_type"] == "Cancel"
                        and row["outcome"] not in ("dropped", "capped")
                    ):
                        continue
                    kind = "Cancel"
                    key = row["product_key"]
                elif row is None:
                    kind = "Alert"
                    key = product.product_key
                else:
                    key = row["product_key"]
                    if row["outcome"] in ("dropped", "capped"):
                        kind = row["message_type"]
                    elif row["last_alert_id"] == product.alert_ids[-1]:
                        continue  # same poll result as last time: nothing new
                    elif (row["event"], row["areas"], row["expires_at"]) == (
                        product.event, areas, product.expires_at
                    ):
                        # Superseded by an Update that changes nothing we print.
                        self._upsert(key, product, areas=areas, kind=row["message_type"],
                                     outcome=row["outcome"], now=row["updated_at"])
                        continue
                    else:
                        kind = "Update"

                if budget <= 0:
                    self._upsert(key, product, areas=areas, kind=kind, outcome="capped", now=now)
                    log.info("weather: hourly cap reached; %s for %s deferred", kind, key)
                    continue

                line = format_alert_line(
                    product, kind=kind, aliases=self._aliases, timezone=self._tz,
                    max_bytes=self._max_bytes,
                )
                self._upsert(key, product, areas=areas, kind=kind, outcome="sent", now=now)
                self._sends.append(now)
                plans.append(WeatherSendPlan(product_key=key, kind=kind, line=line))
                budget -= 1
            except Exception:
                # One malformed product must not abort the rest of the batch.
                log.exception("weather: failed to process product %s", product.product_key)
        return plans


BACKOFF_MAX_SECONDS = 900.0
HTTP_TIMEOUT_SECONDS = 10.0
_NWS_ACTIVE = "https://api.weather.gov/alerts/active"


@dataclass(frozen=True)
class WeatherBatch:
    """What the poller hands the DB worker. `result` is ok | not_modified |
    error. On not_modified `products` repeats the last parsed set so the
    ledger can retry dropped/capped rows; on error it is empty."""

    fetched_at: int
    result: str
    products: tuple[WeatherProduct, ...]


def _default_opener(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(req, timeout=timeout)


class WeatherPoller:
    """The only thread that talks HTTP. Never touches SQLite or the
    transport: it emits `WeatherBatch` objects via `emit` (the daemon's
    `queue.put_nowait`) and beats `heartbeat` once per attempt."""

    def __init__(
        self,
        cfg: WeatherConfig,
        *,
        emit: Callable[[WeatherBatch], None],
        heartbeat: Callable[[], None],
        stop_event: threading.Event,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = cfg
        self._emit = emit
        self._heartbeat = heartbeat
        self._stop = stop_event
        self._open = opener or _default_opener
        self._clock = clock
        self._etag: str | None = None
        self._last_products: tuple[WeatherProduct, ...] = ()
        self._failures = 0
        self._last_error_type: str | None = None
        self.last_success_at: int | None = None
        self.emit_failures = 0
        self.url = f"{_NWS_ACTIVE}?zone={urllib.parse.quote(','.join(cfg.counties), safe='')}"
        self._user_agent = f"meshgarden/{__version__} ({cfg.contact})"

    @property
    def backoff_seconds(self) -> float:
        if self._failures == 0:
            return 0.0
        return min(self._cfg.poll_interval_seconds * (2 ** self._failures), BACKOFF_MAX_SECONDS)

    def _request(self) -> urllib.request.Request:
        headers = {"User-Agent": self._user_agent, "Accept": "application/geo+json"}
        if self._etag:
            headers["If-None-Match"] = self._etag
        return urllib.request.Request(self.url, headers=headers)

    def poll_once(self) -> WeatherBatch:
        now = int(self._clock())
        try:
            with self._open(self._request(), HTTP_TIMEOUT_SECONDS) as resp:
                etag = resp.headers.get("ETag") if hasattr(resp.headers, "get") else None
                payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
            products = tuple(
                parse_active_alerts(
                    payload,
                    event_suffixes=self._cfg.event_suffixes,
                    min_severity=self._cfg.min_severity,
                )
            )
            self._etag = etag or self._etag
            self._last_products = products
            batch = WeatherBatch(fetched_at=now, result="ok", products=products)
            self._on_success(now)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                batch = WeatherBatch(fetched_at=now, result="not_modified",
                                     products=self._last_products)
                self._on_success(now)
            else:
                batch = self._on_error(now, exc)
        except Exception as exc:  # network, JSON, parse: never escapes the thread
            batch = self._on_error(now, exc)

        try:
            self._emit(batch)
        except queue.Full:
            self.emit_failures += 1
            log.warning("weather: ingest queue full; batch dropped (%d so far)", self.emit_failures)
        finally:
            self._heartbeat()
        return batch

    def _on_success(self, now: int) -> None:
        if self._failures:
            log.info("weather: poll recovered after %d failure(s)", self._failures)
        self._failures = 0
        self._last_error_type = None
        self.last_success_at = now

    def _on_error(self, now: int, exc: Exception) -> WeatherBatch:
        self._failures += 1
        kind = type(exc).__name__
        if kind != self._last_error_type:  # log on transition only
            log.warning("weather: poll failed: %s: %s; backing off to %.0fs",
                        kind, exc, self.backoff_seconds)
            self._last_error_type = kind
        return WeatherBatch(fetched_at=now, result="error", products=())

    def run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            delay = self.backoff_seconds or float(self._cfg.poll_interval_seconds)
            if self._stop.wait(timeout=delay):
                break

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="weather-poller", daemon=True)
        thread.start()
        return thread
