"""FORECAST data: an NWS fetcher thread and the cache the command reads.

The one provider is the U.S. National Weather Service (api.weather.gov):
/points/{lat},{lon} names a forecast URL, which lists periods. The fetcher
thread is the only code here that touches the network; the command handler
only reads `ForecastCache`, so a slow or dead provider can never stall a
reply. Nothing in this module touches SQLite or the transport.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meshgarden import __version__
from meshgarden.sender import truncate_utf8

if TYPE_CHECKING:
    from meshgarden.config import ForecastConfig

log = logging.getLogger(__name__)

SOURCE = "NWS"
_API_HOST = "api.weather.gov"
MAX_RESPONSE_BYTES = 262144  # a point forecast is ~10-20 KB; cap well above that
MAX_PERIODS = 3
_BACKOFF_CAP_SECONDS = 60.0
_SHORT_MAX_CHARS = 28


@dataclass(frozen=True)
class ForecastEntry:
    lines: tuple[str, ...]
    fetched_at: int
    source: str = SOURCE


class ForecastCache:
    """Thread-safe single-slot cache. `put` is called by the fetcher thread,
    `get` by the DB worker via the handler."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entry: ForecastEntry | None = None
        self.last_error: str | None = None

    def get(self) -> ForecastEntry | None:
        with self._lock:
            return self._entry

    def put(self, entry: ForecastEntry) -> None:
        with self._lock:
            self._entry = entry
            self.last_error = None

    def note_error(self, message: str) -> None:
        with self._lock:
            self.last_error = message


def _age_label(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    return f"{seconds // 3600}h"


def render_forecast(
    entry: ForecastEntry | None, *, now: int, stale_after: int, max_bytes: int
) -> str:
    """Never presents data older than `stale_after` as current."""
    if entry is None:
        return truncate_utf8("Forecast not available yet.", max_bytes)
    age = now - entry.fetched_at
    label = _age_label(age)
    if age > stale_after:
        header = f"🌤 Forecast ({entry.source}, STALE, {label} old)"
    else:
        header = f"🌤 Forecast ({entry.source}, {label} ago)"
    body = header
    for line in entry.lines:
        if len((body + "\n" + line).encode("utf-8")) > max_bytes:
            break
        body += "\n" + line
    return truncate_utf8(body, max_bytes)


def _clean(text: object, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def parse_periods(payload: Any, *, units: str = "us") -> tuple[str, ...]:
    """First MAX_PERIODS periods as 'Name: Short 52°F'. Raises ValueError
    when the payload has no usable period."""
    if not isinstance(payload, dict):
        raise ValueError("forecast payload is not an object")
    periods = (payload.get("properties") or {}).get("periods")
    if not isinstance(periods, list):
        raise ValueError("forecast payload has no periods")
    lines: list[str] = []
    for period in periods:
        if len(lines) >= MAX_PERIODS:
            break
        if not isinstance(period, dict):
            continue
        name = _clean(period.get("name") or "", 20)
        short = _clean(period.get("shortForecast") or "", _SHORT_MAX_CHARS)
        temp = period.get("temperature")
        unit = period.get("temperatureUnit")
        if not name or isinstance(temp, bool) or not isinstance(temp, int | float):
            continue
        if unit not in ("F", "C"):
            continue
        if units == "si" and unit == "F":
            temp, unit = (temp - 32) * 5 / 9, "C"
        elif units == "us" and unit == "C":
            temp, unit = temp * 9 / 5 + 32, "F"
        lines.append(f"{name}: {short} {round(temp)}°{unit}".replace("  ", " "))
    if not lines:
        raise ValueError("forecast payload has no usable period")
    return tuple(lines)


def _default_opener(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(req, timeout=timeout)


class ForecastFetcher:
    def __init__(
        self,
        cfg: ForecastConfig,
        cache: ForecastCache,
        *,
        stop_event: threading.Event | None = None,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = cfg
        self._cache = cache
        self._stop = stop_event or threading.Event()
        self._open = opener or _default_opener
        self._clock = clock
        self._user_agent = f"meshgarden/{__version__} ({cfg.contact})"
        self._forecast_url: str | None = None  # /points result, reused
        self._thread: threading.Thread | None = None

    def _get_json(self, url: str) -> Any:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname != _API_HOST:
            raise ValueError(f"refusing non-NWS url {url!r}")
        req = urllib.request.Request(
            url, headers={"User-Agent": self._user_agent, "Accept": "application/geo+json"}
        )
        with self._open(req, float(self._cfg.timeout_seconds)) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        return json.loads(raw.decode("utf-8"))

    def _fetch(self) -> tuple[str, ...]:
        if self._forecast_url is None:
            lat, lon = self._cfg.latitude, self._cfg.longitude
            points = self._get_json(f"https://{_API_HOST}/points/{lat:.4f},{lon:.4f}")
            url = (points.get("properties") or {}).get("forecast")
            if not isinstance(url, str):
                raise ValueError("points response has no forecast url")
            self._forecast_url = url
        try:
            return parse_periods(self._get_json(self._forecast_url), units=self._cfg.units)
        except Exception:
            self._forecast_url = None  # grid assignments can change; re-resolve
            raise

    def refresh_once(self) -> bool:
        """One refresh with bounded retries (1 + max_retries attempts). On
        failure the previous cache entry is left in place."""
        attempts = 1 + self._cfg.max_retries
        for attempt in range(attempts):
            try:
                lines = self._fetch()
            except Exception as exc:
                self._cache.note_error(f"{type(exc).__name__}: {exc}")
                log.warning("forecast: fetch failed (%d/%d): %s: %s",
                            attempt + 1, attempts, type(exc).__name__, exc)
                if attempt + 1 < attempts and self._stop.wait(
                    min(2.0 ** (attempt + 1), _BACKOFF_CAP_SECONDS)
                ):
                    return False
                continue
            self._cache.put(ForecastEntry(lines=lines, fetched_at=int(self._clock())))
            return True
        return False

    def run(self) -> None:
        while not self._stop.is_set():
            self.refresh_once()
            if self._stop.wait(float(self._cfg.refresh_interval_seconds)):
                break

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, name="forecast-fetcher", daemon=True)
        self._thread.start()
        return self._thread


class ForecastService:
    """Cache plus fetcher for one daemon. Constructed only when enabled."""

    def __init__(self, cfg: ForecastConfig) -> None:
        self.cfg = cfg
        self.cache = ForecastCache()
        self.stop_event = threading.Event()
        self.fetcher = ForecastFetcher(cfg, self.cache, stop_event=self.stop_event)

    def start(self) -> threading.Thread:
        return self.fetcher.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.fetcher._thread is not None:
            self.fetcher._thread.join(timeout=5)
