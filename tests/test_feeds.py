import io
import queue
import sqlite3
import threading
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

from meshgarden import community, database, feeds
from meshgarden.config import ConfigError, FeedsConfig, parse_config

FIX = Path(__file__).parent / "fixtures" / "feeds"
URL = "https://example.org/feed.xml"


def fx(name):
    return (FIX / name).read_bytes()


def at(iso):
    return feeds._parse_date(iso)


# Fixture dates are 2026-10-19; pick "now" a bit after the latest of them.
NOW = at("2026-10-19T12:00:00Z")


def parse(name, **kw):
    kw.setdefault("max_items", 20)
    kw.setdefault("expiry_days", 7)
    return feeds.parse_feed(fx(name), feed_url=URL, now=NOW, **kw)


def test_rss_plain_text_and_skips_malformed_items():
    items, skipped = parse("rss.xml")
    texts = [i.text for i in items]
    assert texts[0] == "example.org: Garden meeting & potluck"
    assert texts[1] == "example.org: Bold news"
    assert skipped == 1  # no title
    assert not any("<" in t or ">" in t for t in texts)
    # script element content is dropped by the tag stripper
    assert texts[2] == "example.org: Safe text"
    assert all(len(t.encode()) <= community.NEWS_MAX_BYTES for t in texts)
    assert items[0].published_at == at("2026-10-19T10:00:00Z")
    assert items[0].expires_at == items[0].published_at + 7 * 86400


def test_atom_future_undated_old():
    items, _ = parse("atom.xml")
    by = {i.text.split(": ")[1]: i for i in items}
    assert by["Atom entry one"].published_at == at("2026-10-19T11:00:00Z")
    assert by["Future entry"].published_at == NOW  # clamped
    assert by["Undated entry"].published_at == NOW
    assert "Old entry" not in by  # already expired, dropped


def test_max_items_per_feed():
    items, _ = parse("atom.xml", max_items=2)
    assert len(items) == 2


@pytest.mark.parametrize("name", ["malformed.xml", "doctype.xml"])
def test_bad_documents_rejected(name):
    with pytest.raises(feeds.FeedError):
        parse(name)


def test_utf16_doctype_rejected():
    data = '<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><rss/>'.encode("utf-16")
    with pytest.raises(feeds.FeedError):
        feeds.parse_feed(data, feed_url=URL, now=NOW, max_items=5, expiry_days=7)


def test_dedup_key_stable_and_per_feed():
    a, _ = parse("rss.xml")
    b, _ = parse("rss.xml")
    assert [i.dedup_key for i in a] == [i.dedup_key for i in b]
    other, _ = feeds.parse_feed(fx("rss.xml"), feed_url="https://other.example/f", now=NOW,
                                max_items=20, expiry_days=7)
    assert other[0].dedup_key != a[0].dedup_key


@pytest.fixture
def conn(tmp_path):
    c = database.connect(str(tmp_path / "t.db"))
    database.migrate(c)
    yield c
    c.close()


def do_import(conn, items, **kw):
    kw.setdefault("max_stored_items", 50)
    kw.setdefault("item_expiry_days", 7)
    return feeds.import_items(conn, items, now=NOW, **kw)


def visible(conn):
    return [r["text"] for r in community.list_news(conn, NOW)]


def test_import_dedup_across_polls_and_restart(tmp_path, conn):
    items, _ = parse("rss.xml")
    assert do_import(conn, items) == len(items)
    assert do_import(conn, items) == 0
    path = str(tmp_path / "t.db")
    conn.close()
    again = database.connect(path)  # "restart"
    try:
        assert do_import(again, items) == 0
        assert len(visible(again)) == len(items)
    finally:
        again.close()


def test_max_stored_prunes_only_imported(conn):
    with conn:
        community.add_news(conn, "manual one", NOW - 100)
    items, _ = parse("atom.xml")
    do_import(conn, items, max_stored_items=1)
    texts = visible(conn)
    assert "manual one" in texts
    assert len([t for t in texts if t.startswith("example.org")]) == 1
    assert conn.execute("SELECT COUNT(*) FROM news_imports").fetchone()[0] >= 1


def test_expired_imported_hidden_and_removed_manual_untouched(conn):
    with conn:
        community.add_news(conn, "manual", NOW - 10, NOW + 5)
    items, _ = parse("rss.xml")
    do_import(conn, items)
    later = NOW + 8 * 86400
    feeds.import_items(conn, [], now=later, max_stored_items=50, item_expiry_days=7)
    assert [r["text"] for r in community.list_news(conn, later)] == []
    rows = [r[0] for r in conn.execute("SELECT text FROM news")]
    assert rows == ["manual"]  # expired imports deleted; manual row never touched


def test_hidden_import_not_reimported(conn):
    items, _ = parse("rss.xml")
    do_import(conn, items)
    nid = conn.execute("SELECT news_id FROM news_imports LIMIT 1").fetchone()[0]
    with conn:
        community.hide_news(conn, nid)
    assert do_import(conn, items) == 0


# -- fetching ---------------------------------------------------------------


class Resp:
    def __init__(self, data):
        self._d = data

    def read(self, n=-1):
        return self._d if n < 0 else self._d[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def redirect(url, to, code=302):
    h = Message()
    h["Location"] = to
    return urllib.error.HTTPError(url, code, "r", h, io.BytesIO())


def poller(opener, **kw):
    cfg = FeedsConfig(enabled=True, urls=(URL,), contact="c", **kw)
    out = []
    return feeds.FeedPoller(cfg, emit=out.append, stop_event=threading.Event(),
                            opener=opener, clock=lambda: NOW), out


def test_redirect_to_http_rejected():
    def opener(req, timeout):
        raise redirect(req.full_url, "http://example.org/plain")

    p, out = poller(opener)
    batches = p.poll_once()
    assert not batches[0].ok and out[0].items == ()


def test_https_redirect_followed_and_bounded():
    seen = []

    def opener(req, timeout):
        seen.append(req.full_url)
        if req.full_url == URL:
            raise redirect(req.full_url, "https://cdn.example.org/f.xml")
        return Resp(fx("atom.xml"))

    p, _ = poller(opener)
    assert p.poll_once()[0].ok
    assert seen == [URL, "https://cdn.example.org/f.xml"]

    def loop(req, timeout):
        raise redirect(req.full_url, req.full_url)

    p, _ = poller(loop, max_redirects=3)
    assert not p.poll_once()[0].ok


def test_oversize_and_failure_isolated():
    p, _ = poller(lambda r, t: Resp(b"x" * 5000), max_response_bytes=2048)
    assert not p.poll_once()[0].ok

    def boom(r, t):
        raise OSError("down")

    p, _ = poller(boom)
    assert not p.poll_once()[0].ok  # never raises


def test_full_poll_hands_items_to_queue_consumer(conn):
    q: queue.Queue = queue.Queue(maxsize=4)
    cfg = FeedsConfig(enabled=True, urls=(URL,), contact="c")
    p = feeds.FeedPoller(cfg, emit=q.put_nowait, stop_event=threading.Event(),
                         opener=lambda r, t: Resp(fx("rss.xml")), clock=lambda: NOW)
    p.poll_once()
    batch = q.get_nowait()
    assert do_import(conn, batch.items) == len(batch.items) > 0


# -- config -----------------------------------------------------------------


def _toml(**t):
    radio = {"device": "/dev/x", "expected_node_id": "!11223344"}
    return {"radio": radio, "database": {"path": "a.db"}, "backup": {"directory": "b"},
            "feeds": t}


def test_config_validation():
    assert parse_config(_toml()).feeds.enabled is False
    for bad, match in (
        (dict(enabled=True, urls=["https://a.example/f"]), "contact"),
        (dict(enabled=True, contact="c"), "urls"),
        (dict(urls=["http://a.example/f"]), "https"),
        (dict(urls=["file:///etc/passwd"]), "https"),
        (dict(poll_interval_seconds=899), "poll_interval_seconds"),
        (dict(max_items_per_feed=21), "max_items_per_feed"),
    ):
        with pytest.raises(ConfigError, match=match):
            parse_config(_toml(**bad))
    ok = parse_config(_toml(enabled=True, contact="c", urls=["https://a.example/f"]))
    assert ok.feeds.max_stored_items == 50


def test_migration_present(conn):
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='news_imports'").fetchone()
    assert isinstance(conn, sqlite3.Connection)
