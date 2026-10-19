"""RSS/Atom feeds imported into NEWS (WP6).

`FeedPoller` is the only thread that talks HTTP. Like `WeatherPoller` it
never touches SQLite or the transport: it parses each feed into plain-text
`FeedItem`s and hands a `FeedBatch` to the daemon's ingest queue, where the
DB worker calls `import_items`. Imported items are ordinary visible NEWS
rows read on demand; nothing is ever broadcast.

Storage: `news_imports` (main database, migration 20) maps a dedup key to the
news row it created. Only rows listed there are ever pruned; manual NEWS is
never modified. The table stays in main even when `database.bbs_path` moves
`news` into the shared file: it is not in `bbs_store.BBS_TABLES`, so no split
logic changes. The cost is that two instances sharing one bbs.db must not
both enable feeds (each would import its own copy).
"""
from __future__ import annotations

import hashlib
import logging
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET  # noqa: S405 - DOCTYPE/ENTITY rejected first
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

from meshgarden import __version__
from meshgarden.community import NEWS_MAX_BYTES, add_news
from meshgarden.sender import truncate_utf8

if TYPE_CHECKING:
    from meshgarden.config import FeedsConfig

log = logging.getLogger(__name__)

NEWS_IMPORTS_SQL = """
CREATE TABLE IF NOT EXISTS news_imports (
  dedup_key TEXT PRIMARY KEY,
  news_id INTEGER NOT NULL,
  feed TEXT NOT NULL,
  published_at INTEGER NOT NULL,
  imported_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_imports_imported ON news_imports(imported_at);
"""

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_SECONDS_PER_DAY = 86400
_FUTURE_SLACK = 0  # a future-dated item is clamped to fetch time, no grace


class FeedError(ValueError):
    """A feed document or fetch that must be rejected."""


@dataclass(frozen=True)
class FeedItem:
    dedup_key: str
    feed: str  # host, shown as attribution
    text: str
    published_at: int
    expires_at: int


@dataclass(frozen=True)
class FeedBatch:
    fetched_at: int
    feed_url: str
    ok: bool
    items: tuple[FeedItem, ...] = ()
    skipped: int = 0  # malformed items


class _TextExtractor(HTMLParser):
    """Visible text only: tags dropped, script/style content dropped,
    entities unescaped (convert_charrefs)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def plain_text(raw: str | None) -> str:
    if not raw:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
        text = parser.text()
    except Exception:
        text = ""
    # Double-escaped markup survives one unescape as literal angle brackets;
    # the stored text must never look like HTML.
    text = text.replace("<", " ").replace(">", " ")
    return " ".join(text.split())


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child_text(node: ET.Element, name: str) -> str:
    for child in node:
        if _local(child.tag) == name:
            return child.text or ""
    return ""


def _atom_link(entry: ET.Element) -> str:
    fallback = ""
    for child in entry:
        if _local(child.tag) == "link":
            href = child.get("href") or ""
            if child.get("rel") in (None, "alternate"):
                return href
            fallback = fallback or href
    return fallback


def _parse_date(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def parse_feed(
    data: bytes, *, feed_url: str, now: int, max_items: int, expiry_days: int
) -> tuple[list[FeedItem], int]:
    """Returns (items, skipped). Raises FeedError for an unusable document.
    Items already past their expiry are dropped silently (not malformed)."""
    # Entity-expansion defence: stdlib ElementTree expands internal entities,
    # so refuse any DTD outright. NULs are stripped so UTF-16 cannot hide it.
    probe = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
        raise FeedError("feed contains a DOCTYPE/ENTITY declaration")
    try:
        root = ET.fromstring(data)  # noqa: S314 - see above
    except ET.ParseError as exc:
        raise FeedError(f"malformed XML: {exc}") from exc
    kind = _local(root.tag)
    if kind == "rss":
        channel = next((c for c in root if _local(c.tag) == "channel"), None)
        nodes = [n for n in (channel if channel is not None else []) if _local(n.tag) == "item"]
    elif kind == "feed":
        nodes = [n for n in root if _local(n.tag) == "entry"]
    else:
        raise FeedError(f"not an RSS or Atom document (root {kind!r})")

    host = urllib.parse.urlsplit(feed_url).hostname or "feed"
    items: list[FeedItem] = []
    skipped = 0
    for node in nodes[:max_items]:
        title = plain_text(_child_text(node, "title"))
        if not title:
            skipped += 1
            continue
        if kind == "rss":
            ident = _child_text(node, "guid").strip() or _child_text(node, "link").strip()
            raw_date = _child_text(node, "pubDate") or _child_text(node, "date")
        else:
            ident = _child_text(node, "id").strip() or _atom_link(node).strip()
            raw_date = _child_text(node, "published") or _child_text(node, "updated")
        published = _parse_date(raw_date)
        if published is None or published > now + _FUTURE_SLACK:
            published = now  # undated or future-dated: fetch time
        ident = ident or f"{title}|{raw_date.strip()}"
        key = hashlib.sha256(f"{feed_url}\n{ident}".encode()).hexdigest()
        expires = published + expiry_days * _SECONDS_PER_DAY
        if expires <= now:
            continue
        text = truncate_utf8(f"{host}: {title}", NEWS_MAX_BYTES)
        items.append(FeedItem(key, host, text, published, expires))
    return items, skipped


def import_items(
    conn: sqlite3.Connection,
    items: tuple[FeedItem, ...] | list[FeedItem],
    *,
    now: int,
    max_stored_items: int,
    item_expiry_days: int,
) -> int:
    """DB worker only. Inserts unseen items, then prunes imported rows only
    (expired, then oldest beyond `max_stored_items`). Returns rows inserted."""
    inserted = 0
    with conn:
        for item in items:
            if conn.execute(
                "SELECT 1 FROM news_imports WHERE dedup_key = ?", (item.dedup_key,)
            ).fetchone():
                continue
            news_id = add_news(conn, item.text, item.published_at, item.expires_at)
            conn.execute(
                "INSERT INTO news_imports(dedup_key, news_id, feed, published_at, imported_at) "
                "VALUES (?,?,?,?,?)",
                (item.dedup_key, news_id, item.feed, item.published_at, now),
            )
            inserted += 1
        conn.execute(
            "DELETE FROM news WHERE expires_at <= ? "
            "AND id IN (SELECT news_id FROM news_imports)", (now,)
        )
        conn.execute(
            "DELETE FROM news WHERE id IN (SELECT ni.news_id FROM news_imports ni "
            "JOIN news n ON n.id = ni.news_id "
            "ORDER BY ni.imported_at DESC, ni.news_id DESC LIMIT -1 OFFSET ?)",
            (max_stored_items,),
        )
        # Keys outlive their news row only while the item could still be
        # re-fetched as unexpired; after that they are dead weight.
        conn.execute(
            "DELETE FROM news_imports WHERE published_at < ?",
            (now - (item_expiry_days + 1) * _SECONDS_PER_DAY,),
        )
    return inserted


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # surfaced as HTTPError so the poller can vet the target


def _default_opener(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


class FeedPoller:
    def __init__(
        self,
        cfg: FeedsConfig,
        *,
        emit: Callable[[FeedBatch], None],
        stop_event: threading.Event,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = cfg
        self._emit = emit
        self._stop = stop_event
        self._open = opener or _default_opener
        self._clock = clock
        self._user_agent = f"meshgarden/{__version__} ({cfg.contact})"
        self.emit_failures = 0

    def fetch(self, url: str) -> bytes:
        """GET with manual, bounded, https-only redirects and a size cap."""
        for _ in range(self._cfg.max_redirects + 1):
            if urllib.parse.urlsplit(url).scheme != "https":
                raise FeedError(f"refusing non-https url {url!r}")
            req = urllib.request.Request(url, headers={
                "User-Agent": self._user_agent,
                "Accept": "application/rss+xml, application/atom+xml, application/xml",
            })
            try:
                with self._open(req, float(self._cfg.timeout_seconds)) as resp:
                    data = resp.read(self._cfg.max_response_bytes + 1)
            except urllib.error.HTTPError as exc:
                location = exc.headers.get("Location") if exc.headers else None
                if exc.code in _REDIRECT_CODES and location:
                    url = urllib.parse.urljoin(url, location)
                    continue
                raise
            if len(data) > self._cfg.max_response_bytes:
                raise FeedError("response too large")
            return data
        raise FeedError("too many redirects")

    def poll_once(self) -> list[FeedBatch]:
        batches: list[FeedBatch] = []
        for url in self._cfg.urls:
            now = int(self._clock())
            try:
                items, skipped = parse_feed(
                    self.fetch(url), feed_url=url, now=now,
                    max_items=self._cfg.max_items_per_feed,
                    expiry_days=self._cfg.item_expiry_days,
                )
                batch = FeedBatch(now, url, True, tuple(items), skipped)
            except Exception as exc:  # one bad feed never stops the others
                log.warning("feeds: %s failed: %s: %s", url, type(exc).__name__, exc)
                batch = FeedBatch(now, url, False)
            batches.append(batch)
            try:
                self._emit(batch)
            except queue.Full:
                self.emit_failures += 1
                log.warning("feeds: ingest queue full; batch dropped")
        return batches

    def run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            if self._stop.wait(float(self._cfg.poll_interval_seconds)):
                break

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="feed-poller", daemon=True)
        thread.start()
        return thread
