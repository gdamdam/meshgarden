import threading
import time
import urllib.error
import urllib.request

import pytest

from meshgarden import database, federation
from meshgarden.config import (
    ConfigError,
    FederationConfig,
    FederationPeer,
    ServiceConfig,
)
from meshgarden.guestbook import Guestbook

SECRET = b"s" * 40
BOARD = "chat"


def make_db(tmp_path, name):
    path = str(tmp_path / f"{name}.db")
    conn = database.connect(path)
    database.migrate(conn)
    return path, conn


def post(conn, text, *, topic=BOARD, sender="!aaaa0001", visible=True, created=None):
    gb = Guestbook(conn, ServiceConfig())
    with conn:
        return gb.insert(sender, "Alice", text, created or int(time.time()) - 10,
                         visible=visible, topic=topic)


def fed_cfg(origin, peers, **kw):
    return FederationConfig(enabled=True, origin_id=origin, listen_port=0, boards=(BOARD,),
                            peers=tuple(peers), **kw)


class Node:
    """One in-process garden: its own DB, server and (optionally) poller."""

    def __init__(self, tmp_path, origin, peer_origin, peer_url=""):
        self.path, self.conn = make_db(tmp_path, origin)
        self.cfg = fed_cfg(origin, [FederationPeer(peer_origin, peer_url, "f", SECRET)])
        self.server = federation.FederationServer(
            self.cfg, lambda: database.connect(self.path, read_only=True),
            federation.author_key(origin, "salt"),
        )
        self.server.start()

    def poller(self, peer_url, **kw):
        peer = self.cfg.peers[0]
        peer = FederationPeer(peer.origin_id, peer_url, "f", SECRET)
        cfg = fed_cfg(self.cfg.origin_id, [peer])
        self.cfg = cfg
        self.sent = []

        def emit(batch):
            self.sent.append(batch)
            federation.process_batch(self.conn, batch, cfg, max_chars=100,
                                     banned_terms=kw.pop("terms", frozenset()))

        return federation.FederationPoller(cfg, emit=emit, stop_event=threading.Event()), peer


@pytest.fixture
def pair(tmp_path):
    a = Node(tmp_path, "alpha", "beta")
    b = Node(tmp_path, "beta", "alpha", f"http://127.0.0.1:{a.server.port}")
    poller, peer = b.poller(f"http://127.0.0.1:{a.server.port}")
    yield a, b, poller, peer
    a.server.stop()
    b.server.stop()


def rows(conn):
    return conn.execute(
        "SELECT * FROM federated_posts ORDER BY remote_id").fetchall()


# ---------------------------------------------------------------- HMAC


def test_auth_accepts_and_rejects():
    now = [1000.0]
    auth = federation.Authenticator({"alpha": SECRET}, lambda: now[0])

    def req(secret=SECRET, ts="1000.000", origin="alpha", query="since=0"):
        sig = federation.sign(secret, "GET", federation.POSTS_PATH, query, ts, b"")
        return auth.verify("GET", federation.POSTS_PATH, query, origin, ts, sig)

    assert req() is None
    assert req(secret=b"x" * 40, ts="1000.001") == "bad signature"
    assert req(origin="mallory", ts="1000.002") is not None
    assert req(ts="1400.000") == "timestamp outside window"
    assert req(ts="600.000") == "timestamp outside window"
    assert req(ts="nan") == "bad timestamp"
    assert req() == "replayed request"
    sig = federation.sign(SECRET, "GET", federation.POSTS_PATH, "since=0", "1001.000", b"")
    assert auth.verify("GET", federation.POSTS_PATH, "since=1", "alpha", "1001.000", sig)


def test_replay_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(federation, "REPLAY_CACHE_MAX", 5)
    auth = federation.Authenticator({"alpha": SECRET}, lambda: 1000.0)
    for i in range(20):
        ts = f"1000.{i:03d}"
        sig = federation.sign(SECRET, "GET", "/p", "", ts, b"")
        assert auth.verify("GET", "/p", "", "alpha", ts, sig) is None
    assert len(auth._seen) <= 5


# -------------------------------------------------------------- server


def http(server, path, headers=None, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_server_exports_only_local_visible_selected(pair):
    a, b, poller, peer = pair
    post(a.conn, "keep me")
    post(a.conn, "secret plain", topic=None)
    post(a.conn, "other board", topic="lab")
    post(a.conn, "hidden one", visible=False)
    with a.conn:  # an imported post must never be re-exported
        a.conn.execute("INSERT INTO federated_posts VALUES ('gamma',1,'chat','imp','t',1,1,0,0)")
    payload = poller.fetch(peer, "0")
    bodies = {e["body"] for e in payload["entries"]}
    assert bodies == {"keep me", ""}  # hidden post goes out bodiless
    assert any(e["hidden"] for e in payload["entries"])
    text = str(payload)
    assert "!aaaa0001" not in text and "Alice" not in text
    assert "imp" not in text and "secret plain" not in text and "other board" not in text
    assert len(payload["entries"][0]["author_tag"]) == 8


def test_server_rejects_unauthenticated_and_other_methods(pair):
    a, *_ = pair
    assert http(a.server, federation.POSTS_PATH)[0] == 401
    assert http(a.server, "/federation/v1/other")[0] == 404
    assert http(a.server, "/")[0] == 404
    for m in ("POST", "PUT", "DELETE"):
        assert http(a.server, federation.POSTS_PATH, method=m)[0] == 404
    ts = f"{time.time():.3f}"
    hdr = {"X-MG-Origin": "beta", "X-MG-Timestamp": ts, "X-MG-Signature": "00" * 32}
    assert http(a.server, federation.POSTS_PATH + "?since=0", hdr)[0] == 401


def test_wrong_secret_and_tampered_response_rejected(pair):
    a, b, poller, peer = pair
    bad = FederationPeer("alpha", peer.url, "f", b"w" * 40)
    with pytest.raises(urllib.error.HTTPError):
        poller.fetch(bad, "0")  # server does not know that secret -> 401

    def tamper(req, timeout):
        resp = urllib.request.urlopen(req, timeout=timeout)
        body = resp.read().replace(b'"entries"', b'"entriez"')

        class R:
            headers = resp.headers

            def read(self, n=-1):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()

    poller._open = tamper
    with pytest.raises(federation.FederationError):
        poller.fetch(peer, "0")


# ---------------------------------------------------------------- sync


def test_two_instances_sync(pair):
    a, b, poller, peer = pair
    ids = [post(a.conn, f"msg {i}") for i in range(3)]
    assert poller.poll_peer(peer) is False
    got = rows(b.conn)
    assert [r["body"] for r in got] == ["msg 0", "msg 1", "msg 2"]
    assert [r["remote_id"] for r in got] == ids
    assert all(r["origin"] == "alpha" for r in got)
    assert poller.cursors["alpha"] == str(ids[-1])
    assert federation.load_cursors(b.conn)["alpha"] == str(ids[-1])
    # nothing new: idempotent, no duplicates
    poller.poll_peer(peer)
    assert len(rows(b.conn)) == 3


def test_interrupted_batch_recovers_without_dupes(pair):
    a, b, poller, peer = pair
    post(a.conn, "one")
    post(a.conn, "two")
    real = poller._emit
    poller._emit = lambda batch: batch.acked.set()  # fetched, never committed
    poller.poll_peer(peer)
    assert rows(b.conn) == [] and poller.cursors.get("alpha") is None
    poller._emit = real
    poller._next_at.clear()
    poller.poll_peer(peer)
    assert [r["body"] for r in rows(b.conn)] == ["one", "two"]
    poller.cursors.clear()  # replay the same batch
    poller.poll_peer(peer)
    assert len(rows(b.conn)) == 2
    top = max(r["remote_id"] for r in rows(b.conn))
    assert federation.load_cursors(b.conn)["alpha"] == str(top)


def test_replayed_and_reordered_batches(pair):
    a, b, poller, peer = pair
    post(a.conn, "x")
    post(a.conn, "y")
    batch = federation.FederationBatch(
        "alpha", True, int(time.time()),
        tuple(reversed(poller.fetch(peer, "0")["entries"])), (), "2")
    for _ in range(2):
        federation.import_batch(b.conn, batch, boards=[BOARD], max_chars=100,
                                max_stored_per_peer=10, now=int(time.time()))
    assert [r["body"] for r in rows(b.conn)] == ["x", "y"]
    old = federation.FederationBatch("alpha", True, 1, (), (), "1")
    federation.import_batch(b.conn, old, boards=[BOARD], max_chars=100,
                            max_stored_per_peer=10, now=int(time.time()))
    assert federation.load_cursors(b.conn)["alpha"] == "2"  # never moves backwards


def test_tombstone_propagation_hide_and_delete(pair):
    a, b, poller, peer = pair
    p1 = post(a.conn, "will hide")
    p2 = post(a.conn, "will delete")
    poller.poll_peer(peer)
    assert len(federation.visible_posts(b.conn, BOARD, 10)) == 2
    Guestbook(a.conn, ServiceConfig()).hide(p1)
    federation.record_deletion(a.conn, p2, int(time.time()))
    with a.conn:
        a.conn.execute("DELETE FROM guestbook WHERE entry_id=?", (p2,))
    poller.poll_peer(peer)
    assert federation.visible_posts(b.conn, BOARD, 10) == []
    assert all(r["remote_hidden"] for r in rows(b.conn))


def test_tombstone_before_entry_stays_hidden(tmp_path):
    _, conn = make_db(tmp_path, "t")
    now = int(time.time())
    federation.import_batch(conn, federation.FederationBatch("alpha", True, now, (), (7,), "0"),
                            boards=[BOARD], max_chars=100, max_stored_per_peer=10, now=now)
    entry = {"id": 7, "board": BOARD, "created_at": now, "body": "late", "author_tag": "ab",
             "hidden": False}
    federation.import_batch(conn, federation.FederationBatch("alpha", True, now, (entry,), (), "7"),
                            boards=[BOARD], max_chars=100, max_stored_per_peer=10, now=now)
    assert federation.visible_posts(conn, BOARD, 10) == []


def test_local_hide_wins_over_remote_update(pair):
    a, b, poller, peer = pair
    pid = post(a.conn, "original")
    poller.poll_peer(peer)
    assert federation.set_local_hidden(b.conn, "alpha", pid, True)
    entry = {"id": pid, "board": BOARD, "created_at": 1, "body": "edited", "author_tag": "ab",
             "hidden": False}
    for _ in range(2):
        federation.import_batch(
            b.conn, federation.FederationBatch("alpha", True, 5, (entry,), (), str(pid)),
            boards=[BOARD], max_chars=100, max_stored_per_peer=10, now=int(time.time()))
    assert federation.visible_posts(b.conn, BOARD, 10) == []
    assert rows(b.conn)[0]["body"] == "edited" and rows(b.conn)[0]["local_hidden"] == 1
    federation.set_local_hidden(b.conn, "alpha", pid, False)
    assert len(federation.visible_posts(b.conn, BOARD, 10)) == 1


def test_import_sanitises_clamps_caps_and_drops(tmp_path):
    _, conn = make_db(tmp_path, "i")
    now = int(time.time())
    ents = [
        {"id": 1, "board": BOARD, "created_at": now + 10**6, "body": "x" * 500,
         "author_tag": "ab", "hidden": False},
        {"id": 2, "board": "unknown", "created_at": now, "body": "no", "author_tag": "a",
         "hidden": False},
        {"id": "3", "board": BOARD, "created_at": now, "body": "no", "author_tag": "a",
         "hidden": False},
        {"id": 4, "board": BOARD, "created_at": now, "body": "  \x00 ", "author_tag": "a",
         "hidden": False},
    ]
    batch = federation.FederationBatch("alpha", True, now, tuple(ents), (), "4")
    federation.import_batch(conn, batch,
                            boards=[BOARD], max_chars=100, max_stored_per_peer=10, now=now)
    got = rows(conn)
    assert len(got) == 1 and len(got[0]["body"]) == 100 and got[0]["created_at"] == now


def test_per_peer_cap_prunes_oldest(tmp_path):
    _, conn = make_db(tmp_path, "c")
    now = int(time.time())
    ents = tuple({"id": i, "board": BOARD, "created_at": now - 100 + i, "body": f"m{i}",
                  "author_tag": "a", "hidden": False} for i in range(1, 8))
    federation.import_batch(conn, federation.FederationBatch("alpha", True, now, ents, (), "7"),
                            boards=[BOARD], max_chars=100, max_stored_per_peer=3, now=now)
    assert [r["remote_id"] for r in rows(conn)] == [5, 6, 7]


def test_peer_removal_purges(pair):
    a, b, poller, peer = pair
    post(a.conn, "hello")
    poller.poll_peer(peer)
    assert rows(b.conn)
    federation.purge_removed_peers(b.conn, ["beta-other"])
    assert rows(b.conn) == [] and federation.load_cursors(b.conn) == {}


def test_moderation_on_import(tmp_path):
    a = Node(tmp_path, "alpha", "beta")
    b = Node(tmp_path, "beta", "alpha")
    try:
        poller, peer = b.poller(f"http://127.0.0.1:{a.server.port}", terms=frozenset({"badword"}))
        post(a.conn, "this has badword inside")
        post(a.conn, "fine")
        poller.poll_peer(peer)
        assert [r["local_hidden"] for r in rows(b.conn)] == [1, 0]
        assert [r["body"] for r in federation.visible_posts(b.conn, BOARD, 10)] == ["fine"]
    finally:
        a.server.stop()
        b.server.stop()


def test_failure_backoff_and_no_cursor_move(tmp_path):
    path, conn = make_db(tmp_path, "f")
    peer = FederationPeer("alpha", "http://127.0.0.1:9", "f", SECRET)  # nothing listens
    cfg = fed_cfg("beta", [peer], timeout_seconds=1)
    sent = []
    p = federation.FederationPoller(cfg, emit=sent.append, stop_event=threading.Event())
    assert p.poll_peer(peer) is False
    assert p.backoff_seconds("alpha") == 600 and p.cursors == {}
    p._failures["alpha"] = 20
    assert p.backoff_seconds("alpha") == federation.BACKOFF_MAX_SECONDS
    assert sent and sent[0].ok is False
    federation.process_batch(conn, sent[0], cfg, max_chars=100)
    assert conn.execute("SELECT failures FROM federation_cursors").fetchone()[0] == 1


def test_read_display_tag_and_unchanged_when_empty(tmp_path):
    _, conn = make_db(tmp_path, "d")
    assert federation.visible_posts(conn, BOARD, 5) == []
    assert federation.append_federated("local", [], 100) == "local"
    now = int(time.time())
    ent = {"id": 1, "board": BOARD, "created_at": now, "body": "hi there", "author_tag": "abcd",
           "hidden": False}
    federation.import_batch(conn, federation.FederationBatch("alpha", True, now, (ent,), (), "1"),
                            boards=[BOARD], max_chars=100, max_stored_per_peer=10, now=now)
    out = federation.append_federated("local", federation.visible_posts(conn, BOARD, 5), 100)
    assert out == "local\n@alpha/abcd: hi there"


def test_never_touches_transport():
    import inspect

    src = inspect.getsource(federation)
    for word in ("RadioAdapter", "send_text", "_reserve_and_send", "import meshtastic"):
        assert word not in src


# -------------------------------------------------------------- config


def test_config_disabled_by_default_and_validation(tmp_path):
    from meshgarden.config import _parse_federation

    assert _parse_federation({}).enabled is False and _parse_federation({}).peers == ()
    sf = tmp_path / "s"
    sf.write_bytes(SECRET)
    short = tmp_path / "short"
    short.write_bytes(b"abc")
    good = {"enabled": True, "origin_id": "my-garden", "boards": ["#Chat"],
            "peers": [{"origin_id": "north", "url": "http://x:1", "secret_file": str(sf)}]}
    c = _parse_federation(good)
    assert c.boards == ("chat",) and c.peers[0].secret == SECRET
    assert "secret" not in repr(c.peers[0]).replace("secret_file", "")
    for bad in (
        {**good, "origin_id": ""},
        {**good, "boards": []},
        {**good, "boards": ["bad board"]},
        {**good, "poll_interval_seconds": 10},
        {**good, "batch_size": 500},
        {**good, "peers": [{"origin_id": "north", "url": "ftp://x", "secret_file": str(sf)}]},
        {**good, "peers": [{"origin_id": "north", "url": "http://x", "secret_file": str(short)}]},
        {**good, "peers": [{"origin_id": "north", "url": "http://x",
                            "secret_file": str(tmp_path / "none")}]},
        {**good, "bogus": 1},
    ):
        with pytest.raises(ConfigError):
            _parse_federation(bad)


def test_disabled_starts_nothing():
    threads = {t.name for t in threading.enumerate()}
    assert "federation-server" not in threads and "federation-poller" not in threads
