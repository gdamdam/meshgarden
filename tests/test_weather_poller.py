"""WeatherPoller against an injected opener: no network."""
from __future__ import annotations

import io
import json
import queue
import threading
import urllib.error

from meshgarden.config import WeatherConfig
from meshgarden.weather import BACKOFF_MAX_SECONDS, WeatherBatch, WeatherPoller

CFG = WeatherConfig(enabled=True, contact="ops@example.org")


class Resp:
    def __init__(self, body: dict, etag="W/\"1\""):
        self.status = 200
        self.headers = {"ETag": etag}
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def feature(fid):
    return {"id": fid, "properties": {
        "id": fid, "event": "High Wind Warning", "severity": "Severe", "status": "Actual",
        "messageType": "Alert", "sent": "2026-10-10T12:00:00-07:00",
        "expires": "2026-10-10T18:00:00-07:00", "senderName": "NWS",
        "geocode": {"UGC": ["CAZ006"]}, "parameters": {}, "references": []}}


def make(opener, emit=None, clock=None):
    out: list[WeatherBatch] = []
    return WeatherPoller(
        CFG, emit=emit or out.append, heartbeat=lambda: None,
        stop_event=threading.Event(), opener=opener, clock=clock or (lambda: 1791830000.0),
    ), out


def test_url_and_headers_identify_the_client():
    seen = {}

    def opener(req, timeout):
        seen["url"] = req.full_url
        seen["ua"] = req.get_header("User-agent")
        seen["accept"] = req.get_header("Accept")
        seen["timeout"] = timeout
        return Resp({"features": []})

    p, out = make(opener)
    batch = p.poll_once()
    assert seen["url"] == p.url
    assert p.url.startswith("https://api.weather.gov/alerts/active?zone=CAC001%2CCAC013")
    assert "meshgarden/" in seen["ua"] and "ops@example.org" in seen["ua"]
    assert seen["accept"] == "application/geo+json"
    assert seen["timeout"] == 10.0
    assert batch.result == "ok" and batch.products == ()
    assert out == [batch]


def test_second_poll_sends_if_none_match_and_304_reemits_last_products():
    calls = []

    def opener(req, timeout):
        calls.append(req.get_header("If-none-match"))
        if len(calls) == 1:
            return Resp({"features": [feature("a")]}, etag='W/"abc"')
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, io.BytesIO())

    p, out = make(opener)
    first = p.poll_once()
    second = p.poll_once()
    assert calls == [None, 'W/"abc"']
    assert first.result == "ok" and len(first.products) == 1
    assert second.result == "not_modified" and second.products == first.products


def test_error_backs_off_exponentially_to_the_ceiling_and_resets_on_success():
    state = {"fail": True}

    def opener(req, timeout):
        if state["fail"]:
            raise urllib.error.URLError("down")
        return Resp({"features": []})

    p, out = make(opener)
    assert p.backoff_seconds == 0
    delays = []
    for _ in range(12):
        b = p.poll_once()
        assert b.result == "error"
        delays.append(p.backoff_seconds)
    assert delays[0] == CFG.poll_interval_seconds * 2
    assert delays[-1] == BACKOFF_MAX_SECONDS
    assert all(x <= y for x, y in zip(delays, delays[1:], strict=False))
    state["fail"] = False
    assert p.poll_once().result == "ok"
    assert p.backoff_seconds == 0
    assert p.last_success_at == 1791830000


def test_malformed_json_is_an_error_not_a_crash():
    class Bad(Resp):
        def read(self):
            return b"<html>"

    p, out = make(lambda req, timeout: Bad({}))
    assert p.poll_once().result == "error"


def test_full_ingest_queue_is_counted_not_raised():
    q: queue.Queue = queue.Queue(maxsize=1)
    q.put_nowait("occupied")
    p, _ = make(lambda req, timeout: Resp({"features": []}), emit=q.put_nowait)
    batch = p.poll_once()  # emit raises queue.Full internally
    assert batch.result == "ok"
    assert p.emit_failures == 1


def test_run_loop_stops_promptly_and_beats_each_pass():
    beats = []
    stop = threading.Event()
    p = WeatherPoller(
        CFG, emit=lambda b: None, heartbeat=lambda: beats.append(1), stop_event=stop,
        opener=lambda req, timeout: Resp({"features": []}),
    )
    t = p.start()
    import time
    deadline = time.monotonic() + 2
    while not beats and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
    assert beats
