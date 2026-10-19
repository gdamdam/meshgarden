"""Federated public boards (opt-in, `[federation]`).

Two gardens that share a secret exchange the *bodies* of selected board posts
over authenticated HTTP. Pull-based and one-directional per request: each
garden serves its own local posts and polls its peers.

Trust model (see docs/federation.md): peers are mutually trusted operators.
Every request and response carries an HMAC-SHA256 over
`method|path|query|timestamp|body`, so a stranger who finds the port learns
nothing and cannot inject posts. The server never exports imported posts (no
loops), mail, ids, positions, DMs, the plain guestbook, or any node identity:
authors are an opaque per-origin hash tag.

Threading mirrors feeds.py: the poller thread only does HTTP and hands
`FederationBatch` objects to the DB worker, which commits the posts and the
cursor in one transaction and then acknowledges. The poller advances its
cursor only on that acknowledgement, so interrupted, duplicated, replayed or
reordered exchanges are safe. Nothing here ever touches a radio transport.

Tables live in the main database (not the shared BBS file): adding them to
`bbs_store.BBS_TABLES` would make already-split databases fail
`assert_not_split`. Board posts themselves still come from the shared file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from meshgarden import moderation
from meshgarden.guestbook import normalize_sign_text
from meshgarden.sender import truncate_utf8

if TYPE_CHECKING:
    from meshgarden.config import FederationConfig, FederationPeer

log = logging.getLogger(__name__)

POSTS_PATH = "/federation/v1/posts"
MAX_SKEW_SECONDS = 300
MAX_LIMIT = 200
MAX_TOMBSTONES = 500
MAX_RESPONSE_BYTES = 512 * 1024
BACKOFF_MAX_SECONDS = 3600
REPLAY_CACHE_MAX = 10_000
_MAX_ID = 2**53
_MAX_BATCHES_PER_CYCLE = 10

FEDERATION_SQL = """
CREATE TABLE IF NOT EXISTS federated_posts (
  origin TEXT NOT NULL,
  remote_id INTEGER NOT NULL,
  board TEXT NOT NULL,
  body TEXT NOT NULL,
  author_tag TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  imported_at INTEGER NOT NULL,
  remote_hidden INTEGER NOT NULL DEFAULT 0,
  local_hidden INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (origin, remote_id)
);
CREATE INDEX IF NOT EXISTS idx_fed_board_time
  ON federated_posts(board, remote_hidden, local_hidden, created_at DESC);
CREATE TABLE IF NOT EXISTS federation_cursors (
  origin TEXT PRIMARY KEY,
  cursor TEXT,
  last_ok_at INTEGER,
  failures INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER
);
-- Local board posts an operator deleted, so peers can still be told to drop
-- their copy (a deleted row leaves nothing for the export query to find).
CREATE TABLE IF NOT EXISTS federation_deleted (
  entry_id INTEGER PRIMARY KEY,
  deleted_at INTEGER NOT NULL
);
"""


# ---------------------------------------------------------------- signing


def _canonical(method: str, path: str, query: str, timestamp: str, body: bytes) -> bytes:
    head = "|".join((method, path, query, timestamp)).encode("utf-8")
    return head + b"|" + body


def sign(secret: bytes, method: str, path: str, query: str, timestamp: str, body: bytes) -> str:
    return hmac.new(
        secret, _canonical(method, path, query, timestamp, body), hashlib.sha256
    ).hexdigest()


def _parse_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        ts = float(value)
    except ValueError:
        return None
    return ts if ts == ts and abs(ts) < 1e12 else None  # rejects NaN/inf/huge


class Authenticator:
    """Verifies inbound requests: known origin, constant-time signature,
    timestamp window, and a bounded replay cache of (origin, ts, signature)."""

    def __init__(self, secrets: dict[str, bytes], clock: Callable[[], float] = time.time) -> None:
        self._secrets = secrets
        self._clock = clock
        self._seen: OrderedDict[tuple[str, str, str], float] = OrderedDict()
        self._lock = threading.Lock()

    def verify(
        self, method: str, path: str, query: str, origin: str | None,
        timestamp: str | None, signature: str | None, body: bytes = b"",
    ) -> str | None:
        """None when accepted, else a short internal reason (never sent to
        the peer: every failure is the same bare 401)."""
        secret = self._secrets.get(origin or "")
        if secret is None or not signature:
            return "unknown origin or missing signature"
        ts = _parse_timestamp(timestamp)
        if ts is None:
            return "bad timestamp"
        now = self._clock()
        if abs(now - ts) > MAX_SKEW_SECONDS:
            return "timestamp outside window"
        expected = sign(secret, method, path, query, timestamp or "", body)
        if not hmac.compare_digest(expected, signature):
            return "bad signature"
        key = (origin or "", timestamp or "", signature)
        with self._lock:
            # Entries older than the window can no longer pass the check
            # above, so they are safe to forget; the size cap bounds memory.
            while self._seen:
                oldest_key, seen_at = next(iter(self._seen.items()))
                if now - seen_at > MAX_SKEW_SECONDS or len(self._seen) >= REPLAY_CACHE_MAX:
                    del self._seen[oldest_key]
                else:
                    break
            if key in self._seen:
                return "replayed request"
            self._seen[key] = now
        return None


# ----------------------------------------------------------------- server


def author_key(origin_id: str, salt: str) -> bytes:
    return hashlib.sha256(f"mg-fed-author|{origin_id}|{salt}".encode()).digest()


def author_tag(key: bytes, sender_node_id: str) -> str:
    """Opaque per-origin tag: stable per author within one garden, not
    reversible to a node id (keyed), unlinkable across gardens."""
    return hmac.new(key, sender_node_id.encode("utf-8"), hashlib.sha256).hexdigest()[:8]


def export_posts(
    conn: sqlite3.Connection, boards: Iterable[str], key: bytes, since: int, limit: int
) -> dict[str, Any]:
    """Local-origin posts of the selected boards only. Hidden posts are sent
    as bodiless `hidden` entries; earlier hides/deletions as tombstones."""
    boards = tuple(boards)
    marks = ",".join("?" * len(boards))
    rows = conn.execute(
        "SELECT entry_id, created_at, topic, sender_node_id, message, visible FROM guestbook "
        f"WHERE topic IN ({marks}) AND entry_id > ? ORDER BY entry_id ASC LIMIT ?",
        (*boards, since, limit),
    ).fetchall()
    entries: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        hidden = not row["visible"]
        item = {
            "id": row["entry_id"],
            "board": row["topic"],
            "created_at": row["created_at"],
            "body": "" if hidden else row["message"],
            "author_tag": author_tag(key, row["sender_node_id"]),
            "hidden": hidden,
        }
        size += len(json.dumps(item))
        if entries and size > MAX_RESPONSE_BYTES // 2:
            break
        entries.append(item)
    hidden_ids = [
        r[0] for r in conn.execute(
            f"SELECT entry_id FROM guestbook WHERE topic IN ({marks}) AND visible=0 "
            "ORDER BY entry_id DESC LIMIT ?", (*boards, MAX_TOMBSTONES),
        )
    ]
    deleted_ids = [
        r[0] for r in conn.execute(
            "SELECT entry_id FROM federation_deleted ORDER BY entry_id DESC LIMIT ?",
            (MAX_TOMBSTONES,),
        )
    ]
    next_cursor = str(entries[-1]["id"]) if entries else str(since)
    return {
        "entries": entries,
        "tombstones": sorted(set(hidden_ids) | set(deleted_ids)),
        "next_cursor": next_cursor,
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "MeshgardenFederation/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    fed: FederationServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        log.debug("federation: %s - %s", self.address_string(), format % args)

    def _send(self, status: HTTPStatus, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != POSTS_PATH:
            self._send(HTTPStatus.NOT_FOUND, b"{}")
            return
        try:
            self.fed.handle_posts(self, parts.path, parts.query)
        except Exception:
            log.exception("federation: unhandled error")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b"{}")

    def _not_found(self) -> None:
        self._send(HTTPStatus.NOT_FOUND, b"{}")

    do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_found


class FederationServer:
    def __init__(
        self,
        cfg: FederationConfig,
        conn_factory: Callable[[], sqlite3.Connection],
        key: bytes,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = cfg
        self._conn_factory = conn_factory
        self._key = key
        self._clock = clock
        self._auth = Authenticator({p.origin_id: p.secret for p in cfg.peers}, clock)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self._httpd is not None
        return self._httpd.server_address[1]

    def handle_posts(self, handler: _Handler, path: str, query: str) -> None:
        origin = handler.headers.get("X-MG-Origin")
        timestamp = handler.headers.get("X-MG-Timestamp")
        signature = handler.headers.get("X-MG-Signature")
        reason = self._auth.verify("GET", path, query, origin, timestamp, signature)
        if reason is not None:
            log.warning("federation: rejected %s: %s", handler.address_string(), reason)
            handler._send(HTTPStatus.UNAUTHORIZED, b"{}")
            return
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        try:
            since = int(params.get("since", ["0"])[0] or 0)
            limit = int(params.get("limit", [str(self._cfg.batch_size)])[0])
        except ValueError:
            handler._send(HTTPStatus.BAD_REQUEST, b"{}")
            return
        since = max(0, min(since, _MAX_ID))
        limit = max(1, min(limit, MAX_LIMIT))
        conn = self._conn_factory()
        try:
            data = export_posts(conn, self._cfg.boards, self._key, since, limit)
        finally:
            conn.close()
        payload = json.dumps({"origin": self._cfg.origin_id, **data}).encode("utf-8")
        ts = f"{self._clock():.3f}"
        secret = next(p.secret for p in self._cfg.peers if p.origin_id == origin)
        handler._send(HTTPStatus.OK, payload, {
            "X-MG-Origin": self._cfg.origin_id,
            "X-MG-Timestamp": ts,
            # Bound to the request's signature so a captured response cannot
            # be replayed against a different request.
            "X-MG-Signature": sign(secret, "RESPONSE", path, signature or "", ts, payload),
        })

    def start(self) -> None:
        handler_cls = type("FedHandler", (_Handler,), {"fed": self})
        self._httpd = ThreadingHTTPServer(
            (self._cfg.listen_host, self._cfg.listen_port), handler_cls
        )
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="federation-server", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd.server_close()
        self._httpd = None


# ----------------------------------------------------------------- poller


@dataclass
class FederationBatch:
    origin: str
    ok: bool
    fetched_at: int
    entries: tuple[dict, ...] = ()
    tombstones: tuple[int, ...] = ()
    next_cursor: str = ""
    error: str = ""
    # Set by the DB worker once the batch (and cursor) is committed.
    committed: bool = False
    acked: threading.Event = field(default_factory=threading.Event, repr=False)


class FederationError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _default_opener(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


class FederationPoller:
    def __init__(
        self,
        cfg: FederationConfig,
        *,
        emit: Callable[[FederationBatch], None],
        stop_event: threading.Event,
        cursors: dict[str, str] | None = None,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
        clock: Callable[[], float] = time.time,
        ack_timeout: float = 30.0,
    ) -> None:
        self._cfg = cfg
        self._emit = emit
        self._stop = stop_event
        self._open = opener or _default_opener
        self._clock = clock
        self._ack_timeout = ack_timeout
        self.cursors: dict[str, str] = dict(cursors or {})
        self._failures: dict[str, int] = {}
        self._next_at: dict[str, float] = {}
        self._last_ts = 0.0
        self.emit_failures = 0

    def _timestamp(self) -> str:
        # Strictly increasing so two requests in the same instant never
        # collide in the server's replay cache.
        self._last_ts = max(self._clock(), self._last_ts + 0.001)
        return f"{self._last_ts:.3f}"

    def backoff_seconds(self, origin: str) -> float:
        n = self._failures.get(origin, 0)
        if n == 0:
            return 0.0
        return min(self._cfg.poll_interval_seconds * (2 ** n), BACKOFF_MAX_SECONDS)

    def fetch(self, peer: FederationPeer, since: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"since": since, "limit": self._cfg.batch_size})
        url = f"{peer.url}{POSTS_PATH}?{query}"
        path = urllib.parse.urlsplit(url).path
        ts = self._timestamp()
        sig = sign(peer.secret, "GET", path, query, ts, b"")
        req = urllib.request.Request(url, method="GET", headers={
            "X-MG-Origin": self._cfg.origin_id,
            "X-MG-Timestamp": ts,
            "X-MG-Signature": sig,
            "Accept": "application/json",
        })
        with self._open(req, float(self._cfg.timeout_seconds)) as resp:
            body = resp.read(MAX_RESPONSE_BYTES + 1)
            headers = resp.headers
        if len(body) > MAX_RESPONSE_BYTES:
            raise FederationError("response too large")
        r_ts = headers.get("X-MG-Timestamp")
        parsed_ts = _parse_timestamp(r_ts)
        if headers.get("X-MG-Origin") != peer.origin_id or parsed_ts is None:
            raise FederationError("bad response origin or timestamp")
        if abs(self._clock() - parsed_ts) > MAX_SKEW_SECONDS:
            raise FederationError("response timestamp outside window")
        expected = sign(peer.secret, "RESPONSE", path, sig, r_ts or "", body)
        if not hmac.compare_digest(expected, headers.get("X-MG-Signature") or ""):
            raise FederationError("bad response signature")
        payload = json.loads(body.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("origin") != peer.origin_id
            or not isinstance(payload.get("entries"), list)
            or not isinstance(payload.get("tombstones"), list)
            or not isinstance(payload.get("next_cursor"), str)
        ):
            raise FederationError("malformed payload")
        return payload

    def poll_peer(self, peer: FederationPeer) -> bool:
        """One bounded batch. Returns True when the peer likely has more."""
        now = int(self._clock())
        since = self.cursors.get(peer.origin_id, "0")
        try:
            payload = self.fetch(peer, since)
            entries = tuple(e for e in payload["entries"] if isinstance(e, dict))
            batch = FederationBatch(
                peer.origin_id, True, now, entries[: self._cfg.batch_size],
                tuple(t for t in payload["tombstones"][:MAX_TOMBSTONES] if isinstance(t, int)),
                payload["next_cursor"],
            )
        except Exception as exc:  # network, auth, JSON: never escapes the thread
            log.warning("federation: %s failed: %s: %s", peer.origin_id, type(exc).__name__, exc)
            return self._failed(peer, now, f"{type(exc).__name__}: {exc}"[:200])
        try:
            self._emit(batch)
        except queue.Full:
            self.emit_failures += 1
            log.warning("federation: ingest queue full; batch dropped")
            return self._failed(peer, now, "queue full", record=False)
        batch.acked.wait(self._ack_timeout)
        if not batch.committed:
            return self._failed(peer, now, "import not committed", record=False)
        # Only now is it safe to move the cursor: the rows are durable.
        self.cursors[peer.origin_id] = batch.next_cursor
        self._failures.pop(peer.origin_id, None)
        self._next_at[peer.origin_id] = 0.0
        return len(batch.entries) >= self._cfg.batch_size

    def _failed(self, peer: FederationPeer, now: int, error: str, *, record: bool = True) -> bool:
        self._failures[peer.origin_id] = self._failures.get(peer.origin_id, 0) + 1
        wait = self.backoff_seconds(peer.origin_id)
        self._next_at[peer.origin_id] = self._clock() + wait
        if record:
            try:
                self._emit(FederationBatch(peer.origin_id, False, now, error=error,
                                           next_cursor=str(int(self._next_at[peer.origin_id]))))
            except queue.Full:
                self.emit_failures += 1
        return False

    def poll_once(self) -> None:
        for peer in self._cfg.peers:
            if self._stop.is_set():
                return
            if self._next_at.get(peer.origin_id, 0.0) > self._clock():
                continue
            for _ in range(_MAX_BATCHES_PER_CYCLE):
                if not self.poll_peer(peer) or self._stop.is_set():
                    break
            else:
                continue
            if self._failures.get(peer.origin_id, 0) == 0:
                self._next_at[peer.origin_id] = self._clock() + self._cfg.poll_interval_seconds

    def run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            if self._stop.wait(5.0):
                break

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="federation-poller", daemon=True)
        thread.start()
        return thread


# ---------------------------------------------------------------- storage


def load_cursors(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["origin"]: r["cursor"]
        for r in conn.execute("SELECT origin, cursor FROM federation_cursors")
        if r["cursor"] is not None
    }


def record_deletion(conn: sqlite3.Connection, entry_id: int, now: int) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO federation_deleted(entry_id, deleted_at) VALUES (?, ?)",
            (entry_id, now),
        )


def purge_removed_peers(conn: sqlite3.Connection, origins: Iterable[str]) -> int:
    """Drop every stored post and cursor of an origin no longer configured."""
    keep = tuple(origins)
    marks = ",".join("?" * len(keep)) or "''"
    with conn:
        cur = conn.execute(
            f"DELETE FROM federated_posts WHERE origin NOT IN ({marks})", keep
        )
        conn.execute(f"DELETE FROM federation_cursors WHERE origin NOT IN ({marks})", keep)
    return cur.rowcount


def _clean_entry(
    raw: dict, boards: frozenset[str], max_chars: int, now: int
) -> tuple[int, str, str, str, int, bool] | None:
    rid, board, created = raw.get("id"), raw.get("board"), raw.get("created_at")
    body, tag, hidden = raw.get("body"), raw.get("author_tag"), raw.get("hidden", False)
    if not (isinstance(rid, int) and not isinstance(rid, bool) and 0 < rid <= _MAX_ID):
        return None
    if not isinstance(board, str) or board not in boards:
        return None  # a peer cannot push content into boards we did not select
    if not isinstance(created, int) or isinstance(created, bool) or created < 0:
        return None
    if not isinstance(body, str) or not isinstance(tag, str) or not isinstance(hidden, bool):
        return None
    created = min(created, now)  # a skewed or hostile clock cannot pin a post to the top
    tag = "".join(c for c in tag if c.isalnum())[:16]
    if hidden:
        return rid, board, "", tag, created, True
    text = normalize_sign_text(body)[:max_chars]
    if not text:
        return None
    return rid, board, text, tag, created, False


def import_batch(
    conn: sqlite3.Connection,
    batch: FederationBatch,
    *,
    boards: Iterable[str],
    max_chars: int,
    max_stored_per_peer: int,
    banned_terms: frozenset[str] = frozenset(),
    now: int,
) -> int:
    """DB worker only. One transaction: posts, tombstones, prune and cursor
    commit together or not at all. Returns the number of new posts."""
    origin = batch.origin
    boardset = frozenset(boards)
    added = 0
    with conn:
        for raw in batch.entries:
            cleaned = _clean_entry(raw, boardset, max_chars, now)
            if cleaned is None:
                continue
            rid, board, body, tag, created, hidden = cleaned
            flagged = bool(
                body and banned_terms
                and moderation.find_banned_term(moderation.normalize_for_match(body), banned_terms)
            )
            if conn.execute(
                "SELECT 1 FROM federated_posts WHERE origin=? AND remote_id=?", (origin, rid)
            ).fetchone() is None:
                added += 1
            # local_hidden is only ever raised here for changed, flagged text;
            # a replay never undoes an operator's `federation unhide`, and a
            # remote update never lifts a local hide. remote_hidden is sticky.
            conn.execute(
                "INSERT INTO federated_posts(origin, remote_id, board, body, author_tag, "
                "created_at, imported_at, remote_hidden, local_hidden) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(origin, remote_id) DO UPDATE SET board=excluded.board, "
                "local_hidden = CASE WHEN excluded.local_hidden=1 AND "
                "federated_posts.body != excluded.body THEN 1 "
                "ELSE federated_posts.local_hidden END, "
                "body=excluded.body, author_tag=excluded.author_tag, "
                "created_at=excluded.created_at, "
                "remote_hidden=MAX(federated_posts.remote_hidden, excluded.remote_hidden)",
                (origin, rid, board, body, tag, created, now, int(hidden), int(flagged)),
            )
        for tomb in batch.tombstones:
            if not (isinstance(tomb, int) and not isinstance(tomb, bool) and 0 < tomb <= _MAX_ID):
                continue
            # A tombstone can overtake its entry: keep a hidden placeholder so
            # the entry cannot show up visible afterwards.
            conn.execute(
                "INSERT INTO federated_posts(origin, remote_id, board, body, author_tag, "
                "created_at, imported_at, remote_hidden) VALUES (?, ?, '', '', '', 0, ?, 1) "
                "ON CONFLICT(origin, remote_id) DO UPDATE SET remote_hidden=1, body=''",
                (origin, tomb, now),
            )
        conn.execute(
            "DELETE FROM federated_posts WHERE origin=? AND remote_id NOT IN ("
            "SELECT remote_id FROM federated_posts WHERE origin=? "
            "ORDER BY created_at DESC, remote_id DESC LIMIT ?)",
            (origin, origin, max_stored_per_peer),
        )
        old = conn.execute(
            "SELECT cursor FROM federation_cursors WHERE origin=?", (origin,)
        ).fetchone()
        cursor = batch.next_cursor
        if not cursor.isdigit():
            cursor = old["cursor"] if old and old["cursor"] else "0"
        elif old and old["cursor"] and old["cursor"].isdigit() and int(old["cursor"]) > int(cursor):
            cursor = old["cursor"]  # never move backwards on a replayed batch
        conn.execute(
            "INSERT INTO federation_cursors(origin, cursor, last_ok_at, failures, "
            "next_attempt_at) VALUES (?, ?, ?, 0, 0) ON CONFLICT(origin) DO UPDATE SET "
            "cursor=excluded.cursor, last_ok_at=excluded.last_ok_at, failures=0, "
            "next_attempt_at=0",
            (origin, cursor, now),
        )
    return added


def record_failure(conn: sqlite3.Connection, batch: FederationBatch) -> None:
    next_at = int(batch.next_cursor) if batch.next_cursor.isdigit() else 0
    with conn:
        conn.execute(
            "INSERT INTO federation_cursors(origin, cursor, failures, next_attempt_at) "
            "VALUES (?, NULL, 1, ?) ON CONFLICT(origin) DO UPDATE SET "
            "failures=failures+1, next_attempt_at=excluded.next_attempt_at",
            (batch.origin, next_at),
        )


def process_batch(
    conn: sqlite3.Connection,
    batch: FederationBatch,
    cfg: FederationConfig,
    *,
    max_chars: int,
    banned_terms: frozenset[str] = frozenset(),
    now: int | None = None,
) -> bool:
    """DB-worker entry point: commit (or record failure), then acknowledge."""
    now = int(time.time()) if now is None else now
    try:
        if batch.origin not in {p.origin_id for p in cfg.peers}:
            return False
        if not batch.ok:
            record_failure(conn, batch)
            return False
        import_batch(
            conn, batch, boards=cfg.boards, max_chars=max_chars,
            max_stored_per_peer=cfg.max_stored_per_peer, banned_terms=banned_terms, now=now,
        )
        batch.committed = True
        return True
    except Exception:
        log.exception("federation: import failed for %s", batch.origin)
        return False
    finally:
        batch.acked.set()


# ---------------------------------------------------------------- display


def visible_posts(conn: sqlite3.Connection, board: str, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT origin, remote_id, author_tag, body, created_at FROM federated_posts "
        "WHERE board=? AND remote_hidden=0 AND local_hidden=0 AND body != '' "
        "ORDER BY created_at DESC, remote_id DESC LIMIT ?",
        (board, limit),
    ).fetchall()


def format_line(row: sqlite3.Row, max_bytes: int) -> str:
    """`@origin/tag: body`: the leading `@origin` cannot be mistaken for a
    local author and the body is cut to fit one radio message."""
    return truncate_utf8(f"@{row['origin']}/{row['author_tag']}: {row['body']}", max_bytes)


def append_federated(local_body: str, rows: list[sqlite3.Row], max_bytes: int) -> str:
    out = local_body
    for row in rows:
        line = format_line(row, max_bytes)
        candidate = f"{out}\n{line}" if out else line
        if len(candidate.encode("utf-8")) > max_bytes:
            break
        out = candidate
    return out


def reserve_bytes(rows: list[sqlite3.Row], max_bytes: int) -> int:
    """Room to leave for federated lines when sizing the local board text."""
    if not rows:
        return 0
    return len(format_line(rows[0], max_bytes // 3).encode("utf-8")) + 1


# -------------------------------------------------------------------- CLI


def parse_ref(ref: str) -> tuple[str, int] | None:
    origin, sep, rid = ref.partition(":")
    if not sep or not origin or not rid.isascii() or not rid.isdigit():
        return None
    return origin, int(rid)


def set_local_hidden(conn: sqlite3.Connection, origin: str, remote_id: int, hidden: bool) -> bool:
    with conn:
        cur = conn.execute(
            "UPDATE federated_posts SET local_hidden=? WHERE origin=? AND remote_id=?",
            (int(hidden), origin, remote_id),
        )
    return cur.rowcount == 1


def status_rows(conn: sqlite3.Connection, cfg: FederationConfig) -> list[dict[str, Any]]:
    out = []
    for peer in cfg.peers:
        cur = conn.execute(
            "SELECT cursor, last_ok_at, failures, next_attempt_at FROM federation_cursors "
            "WHERE origin=?", (peer.origin_id,),
        ).fetchone()
        counts = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(remote_hidden OR local_hidden), 0) "
            "FROM federated_posts WHERE origin=?", (peer.origin_id,),
        ).fetchone()
        out.append({
            "origin": peer.origin_id,
            "url": peer.url,
            "posts": counts[0],
            "hidden": counts[1],
            "cursor": cur["cursor"] if cur else None,
            "last_ok_at": cur["last_ok_at"] if cur else None,
            "failures": cur["failures"] if cur else 0,
            "next_attempt_at": cur["next_attempt_at"] if cur else None,
        })
    return out
