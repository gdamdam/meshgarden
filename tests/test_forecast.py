import json
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from meshgarden import forecast, mod_forecast
from meshgarden.config import ConfigError, ForecastConfig, parse_config
from meshgarden.modules import ModuleRequest

FIX = Path(__file__).parent / "fixtures" / "forecast"
POINTS = (FIX / "points.json").read_bytes()
FORECAST = (FIX / "forecast.json").read_bytes()


class Resp:
    def __init__(self, data):
        self._data = data

    def read(self, n=-1):
        return self._data if n < 0 else self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def cfg(**kw):
    base = dict(enabled=True, latitude=37.7, longitude=-122.4, contact="me@example.org",
                stale_after_seconds=10800, max_retries=2)
    base.update(kw)
    return ForecastConfig(**base)


def good_opener(req, timeout):
    return Resp(POINTS if "/points/" in req.full_url else FORECAST)


def fetcher(opener, **kw):
    cache = forecast.ForecastCache()
    f = forecast.ForecastFetcher(cfg(**kw), cache, opener=opener, clock=lambda: 1000)
    f._stop = SimpleNamespace(wait=lambda t: False, is_set=lambda: False)
    return f, cache


def test_parse_periods_us_and_si():
    payload = json.loads(FORECAST)
    assert forecast.parse_periods(payload) == (
        "Tonight: Clear 52°F", "Tuesday: Sunny 70°F", "Tuesday Night: Mostly Clear 50°F")
    assert forecast.parse_periods(payload, units="si")[0] == "Tonight: Clear 11°C"
    with pytest.raises(ValueError):
        forecast.parse_periods({"properties": {"periods": []}})


def test_render_age_stale_and_missing():
    e = forecast.ForecastEntry(lines=("Tonight: Clear 52°F", "Tue: Sunny 70°F"), fetched_at=1000)
    fresh = forecast.render_forecast(e, now=1000 + 25 * 60, stale_after=10800, max_bytes=200)
    assert fresh == "🌤 Forecast (NWS, 25m ago)\nTonight: Clear 52°F\nTue: Sunny 70°F"
    stale = forecast.render_forecast(e, now=1000 + 5 * 3600, stale_after=10800, max_bytes=200)
    assert stale.splitlines()[0] == "🌤 Forecast (NWS, STALE, 5h old)"
    assert forecast.render_forecast(None, now=1, stale_after=1, max_bytes=120) == (
        "Forecast not available yet.")
    small = forecast.render_forecast(e, now=1000, stale_after=10800, max_bytes=60)
    assert len(small.encode()) <= 60


def test_fetch_success_populates_cache():
    f, cache = fetcher(good_opener)
    assert f.refresh_once() is True
    assert cache.get().fetched_at == 1000
    assert cache.get().lines[1] == "Tuesday: Sunny 70°F"


def test_failure_keeps_old_data_and_goes_stale():
    f, cache = fetcher(good_opener)
    f.refresh_once()

    def boom(req, timeout):
        raise OSError("down")

    f._open = boom
    assert f.refresh_once() is False
    assert cache.get().lines  # old data kept
    assert "OSError" in cache.last_error
    out = forecast.render_forecast(cache.get(), now=1000 + 4 * 3600, stale_after=10800,
                                   max_bytes=200)
    assert "STALE" in out


def test_retries_bounded_and_timeout_passed():
    calls = []

    def boom(req, timeout):
        calls.append(timeout)
        raise TimeoutError("slow")

    f, _ = fetcher(boom, max_retries=2, timeout_seconds=7)
    assert f.refresh_once() is False
    assert calls == [7.0, 7.0, 7.0]  # 1 + max_retries, no more


def test_oversized_and_foreign_host_refused():
    f, cache = fetcher(lambda r, t: Resp(b"x" * (forecast.MAX_RESPONSE_BYTES + 10)),
                       max_retries=0)
    assert f.refresh_once() is False and cache.get() is None
    bad_points = json.dumps({"properties": {"forecast": "https://evil.example/f"}}).encode()
    f, cache = fetcher(lambda r, t: Resp(bad_points), max_retries=0)
    assert f.refresh_once() is False and cache.get() is None


def test_disabled_builds_nothing():
    with patch.object(threading, "Thread") as thread:
        assert mod_forecast.factory(SimpleNamespace(forecast=ForecastConfig())) == []
    thread.assert_not_called()


def test_handler_reads_cache_only_never_network():
    c = cfg()
    modules = mod_forecast.factory(SimpleNamespace(forecast=c))
    assert [m.name for m in modules] == ["FORECAST"] and modules[0].channel_ok
    service = modules[0].service
    req = ModuleRequest("", "!a", 5000, 120, True, False, None)
    with patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")):
        assert modules[0].handler(req) == "Forecast not available yet."
        service.cache.put(forecast.ForecastEntry(("Tonight: Clear 52°F",), 4700))
        assert "5m ago" in modules[0].handler(req)
    assert service.fetcher._thread is None  # constructed, not started


def _toml(**forecast_table):
    radio = {"device": "/dev/x", "expected_node_id": "!11223344"}
    return {"radio": radio, "database": {"path": "a.db"},
            "backup": {"directory": "b"}, "forecast": forecast_table}


def test_config_rules():
    d = parse_config(_toml())
    assert d.forecast.enabled is False
    with pytest.raises(ConfigError, match="contact"):
        parse_config(_toml(enabled=True, latitude=1, longitude=2))
    with pytest.raises(ConfigError, match="latitude"):
        parse_config(_toml(enabled=True, contact="c"))
    with pytest.raises(ConfigError, match="refresh_interval_seconds"):
        parse_config(_toml(refresh_interval_seconds=599))
    ok = parse_config(_toml(enabled=True, contact="c", latitude=1, longitude=2,
                            refresh_interval_seconds=600))
    assert ok.forecast.stale_after_seconds == 1800
    cfg_obs = _toml(enabled=True, contact="c")
    cfg_obs["observatory"] = {"latitude": 37.0, "longitude": -122.0}
    assert parse_config(cfg_obs).forecast.latitude == 37.0


def test_sqlite_untouched():
    assert "sqlite3" not in Path(forecast.__file__).read_text()
