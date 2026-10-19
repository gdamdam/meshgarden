"""Tests for the local read-only dashboard: meshgarden.web.

Batch 3a tests (top of file) exercise `/`, `/health`, `/traffic`,
`/guestbook`, `/records`, `/digest[/<day>]`, `/nodes[/<id>]`. Batch 3b tests
(bottom of file) exercise the four observatory pages added on top of that:
`/map`, the extended `/nodes/<id>` profile, `/topology`, `/propagation`.

Each test starts the dashboard on port 0 (the OS picks a free port) against a
seeded temp SQLite database and status file, fetches routes with urllib, and
tears the server down again. No serial device, no Daemon, no radio -- web.py
is exercised directly, exactly as main.py's tiny start/stop hook uses it.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime

import pytest

from meshgarden import web
from meshgarden.collector import haversine_km
from meshgarden.config import (
    BackupConfig,
    Config,
    DatabaseConfig,
    MetricsConfig,
    ObservatoryConfig,
    RadioConfig,
    RetentionConfig,
    ServiceConfig,
    WebConfig,
)
from meshgarden.database import connect, migrate


def _cfg(tmp_path, *, service_kwargs=None, **web_kwargs) -> Config:
    web_defaults = {"enabled": True, "listen": "127.0.0.1", "port": 0, "allow_lan": False}
    web_defaults.update(web_kwargs)
    return Config(
        radio=RadioConfig(device="/dev/fake", expected_node_id="!11223344"),
        service=ServiceConfig(timezone="UTC", **(service_kwargs or {})),
        database=DatabaseConfig(path=str(tmp_path / "o.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "backups")),
        metrics=MetricsConfig(),
        observatory=ObservatoryConfig(latitude=37.7749, longitude=-122.4194),
        web=WebConfig(**web_defaults),
    )


NODE_A = "!aabbccdd"
XSS_MESSAGE = "hi <script>alert(1)</script> there"


def _seed_db(db_path: str, *, now: int) -> None:
    conn = connect(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO nodes (node_id, short_name, long_name, observed_first_seen, "
            "observed_last_seen, observation_count, hw_model, role, "
            "last_position_lat, last_position_lon, last_position_at) "
            "VALUES (?, 'Abcd', 'Node Abcd', ?, ?, 5, 'TBEAM', 'CLIENT', 37.8, -122.3, ?)",
            (NODE_A, now - 3600, now, now - 60),
        )
        for i in range(5):
            conn.execute(
                "INSERT INTO observations (server_time, packet_time, sender_node_id, "
                "dest_node_id, channel, portnum, packet_id, is_duplicate, rx_snr, rx_rssi, "
                "hop_limit, hop_start, hops_away, via_mqtt, pki_encrypted, payload_len) "
                "VALUES (?, ?, ?, '^all', 0, 'TEXT_MESSAGE_APP', ?, 0, ?, -80, 3, 3, 0, 0, 0, 10)",
                (now - i * 600, now - i * 600, NODE_A, 1000 + i, 4.0 + i),
            )
        conn.execute(
            "INSERT INTO guestbook (created_at, sender_node_id, display_name, message, "
            "message_hash, visible) VALUES (?, ?, 'Abcd', ?, 'h1', 1)",
            (now, NODE_A, XSS_MESSAGE),
        )
        conn.execute(
            "INSERT INTO guestbook (created_at, sender_node_id, display_name, message, "
            "message_hash, visible, moderation_reason) VALUES (?, ?, 'Abcd', 'held msg', "
            "'h2', 0, 'banned_word')",
            (now, NODE_A),
        )
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        conn.execute(
            "INSERT INTO node_daily (node_id, day, packets, direct_packets, best_snr, "
            "snr_sum, snr_count, typical_snr) VALUES (?, ?, 5, 5, 8.0, 20.0, 5, 4.0)",
            (NODE_A, today),
        )
        conn.execute(
            "INSERT INTO node_positions (node_id, server_time, lat, lon) "
            "VALUES (?, ?, 37.8, -122.3)",
            (NODE_A, now),
        )
        conn.execute(
            "INSERT INTO links (a_node_id, b_node_id, first_seen, last_seen, count, kind) "
            "VALUES (?, '!11223344', ?, ?, 5, 'inferred_direct')",
            (NODE_A, now - 3600, now),
        )
        conn.execute(
            "INSERT INTO book_stats (book_key, day, shown, guesses, solves) VALUES "
            "('Some Book', ?, 3, 2, 1)",
            (today,),
        )
        conn.execute(
            "INSERT INTO wordle_daily (day, players, solves, total_guesses_on_solve) "
            "VALUES (?, 2, 1, 4)",
            (today,),
        )
        conn.commit()
    finally:
        conn.close()


def _write_status(db_path: str, *, generated_at: float, **overrides) -> None:
    status_dir = os.path.dirname(db_path)
    os.makedirs(status_dir, exist_ok=True)
    payload = {
        "version": "1.0.0rc1",
        "started_at": int(generated_at) - 100,
        "uptime_seconds": 100,
        "radio_connected": True,
        "reply_ready": True,
        "actual_node_id": "!11223344",
        "expected_node_id": "!11223344",
        "mismatch_reason": None,
        "last_packet_at": int(generated_at) - 5,
        "queue_depth": 0,
        "storage": {"total_bytes": 1000, "cap_bytes": 1024 * 1024 * 1024, "pressure": "ok"},
        "last_backup_at": int(generated_at) - 3600,
        "last_backup_error": None,
        "rate_limited_drops_1h": 0,
        "workers": {
            "db": {"age_seconds": 1.0, "alive": True, "stalled": False},
            "tx": {"age_seconds": 1.0, "alive": True, "stalled": False},
            "radio": {"age_seconds": 1.0, "alive": True, "stalled": False},
            "maintenance": {"age_seconds": 1.0, "alive": True, "stalled": False},
        },
        "generated_at": generated_at,
    }
    payload.update(overrides)
    with open(os.path.join(status_dir, "status.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)


@pytest.fixture
def running_server(tmp_path):
    now = int(time.time())
    _seed_db(str(tmp_path / "o.db"), now=now)
    _write_status(str(tmp_path / "o.db"), generated_at=time.time())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    digest_dir = tmp_path / "digest"
    digest_dir.mkdir()
    digest_payload = {
        "day": day,
        "tz": "UTC",
        "generated_at": now,
        "packets": {"raw": 1, "unique": 1},
        "distinct_senders": 1,
        "new_nodes": 0,
        "busiest_hour": 12,
        "guestbook": {"signatures": 1, "held": 1},
        "replies": {"submitted": 0, "acked": 0, "failed": 0, "expired": 0, "unknown": 0},
        "commands": {},
        "drops": None,
        "book_solves": 1,
        "wordle_solves": 1,
    }
    (digest_dir / f"{day}.json").write_text(json.dumps(digest_payload))

    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    assert httpd is not None and thread is not None
    port = httpd.server_port
    base = f"http://127.0.0.1:{port}"
    yield base, cfg, day
    web.stop_web_server(httpd, thread)
    assert not thread.is_alive()


def _get(url: str, method: str = "GET") -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


_SECURITY_HEADER_EXPECTATIONS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _assert_security_headers(headers: dict) -> None:
    for key, value in _SECURITY_HEADER_EXPECTATIONS.items():
        assert headers.get(key) == value


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/health",
        "/traffic",
        "/guestbook",
        "/records",
        "/digest",
        "/nodes",
        "/map",
        "/topology",
        "/propagation",
    ],
)
def test_pages_render_ok_with_expected_headers(running_server, path):
    base, _cfg, _day = running_server
    status, headers, body = _get(base + path)
    assert status == 200
    assert headers.get("Content-Type", "").startswith("text/html")
    _assert_security_headers(headers)
    assert b"<html" in body.lower()


def test_digest_day_page_renders(running_server):
    base, _cfg, day = running_server
    status, _headers, body = _get(f"{base}/digest/{day}")
    assert status == 200
    assert day.encode() in body


def test_node_profile_page_renders(running_server):
    base, _cfg, _day = running_server
    status, _headers, body = _get(f"{base}/nodes/{NODE_A}")
    assert status == 200
    assert b"aabbccdd" in body or b"bccd" in body  # tail of the node id


def test_now_page_shows_expected_numbers(running_server):
    base, _cfg, _day = running_server
    status, _headers, body = _get(base + "/")
    assert status == 200
    text = body.decode()
    assert "Nodes heard today" in text
    assert "Guestbook entries" in text


def test_status_pages_show_known_radio_name_alongside_id(running_server):
    base, cfg, _day = running_server
    conn = connect(cfg.database.path)
    try:
        now = int(time.time())
        conn.execute(
            "INSERT INTO nodes (node_id, short_name, observed_first_seen, observed_last_seen) "
            "VALUES ('!11223344', 'Garden Radio', ?, ?)",
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()

    for path in ("/", "/health"):
        status, _headers, body = _get(base + path)
        assert status == 200
        assert "Garden Radio (!11223344)" in body.decode()


def test_guestbook_message_is_html_escaped(running_server):
    base, _cfg, _day = running_server
    status, _headers, body = _get(base + "/guestbook")
    assert status == 200
    text = body.decode()
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_guestbook_moderation_queue_lists_held_entry(running_server):
    base, _cfg, _day = running_server
    status, _headers, body = _get(base + "/guestbook")
    assert status == 200
    text = body.decode()
    assert "held msg" in text
    assert "banned_word" in text
    assert "mgarden guestbook approve" in text


def _insert_board_entry(db_path: str, *, topic: str, message: str, message_hash: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO guestbook (created_at, sender_node_id, display_name, message, "
            "message_hash, visible, topic) VALUES (?, ?, 'Abcd', ?, ?, 1, ?)",
            (int(time.time()), NODE_A, message, message_hash, topic),
        )
        conn.commit()
    finally:
        conn.close()


def test_boards_page_lists_a_topic_with_its_count(running_server):
    base, cfg, _day = running_server
    _insert_board_entry(cfg.database.path, topic="bike", message="lost my dog", message_hash="hb1")
    status, _headers, body = _get(base + "/boards")
    assert status == 200
    text = body.decode()
    assert "#bike" in text
    assert '<a href="/guestbook?topic=bike">' in text


def test_boards_page_shows_empty_state_with_no_topics(running_server):
    base, _cfg, _day = running_server
    status, _headers, body = _get(base + "/boards")
    assert status == 200
    assert "no topics yet" in body.decode().lower()


def test_guestbook_topic_query_shows_only_that_topics_entries(running_server):
    base, cfg, _day = running_server
    _insert_board_entry(cfg.database.path, topic="bike", message="lost my dog", message_hash="hb2")
    status, _headers, body = _get(base + "/guestbook?topic=bike")
    assert status == 200
    text = body.decode()
    assert "lost my dog" in text
    assert "1 visible posts" in text
    assert "alert(1)" not in text  # the main guestbook's (topic=NULL) entry is excluded


def test_guestbook_invalid_topic_query_renders_main_guestbook(running_server):
    base, _cfg, _day = running_server
    status, _headers, body = _get(base + "/guestbook")
    main_text = body.decode()
    status2, _headers2, body2 = _get(base + "/guestbook?topic=bad%20topic%21")
    assert status2 == 200
    assert body2.decode() == main_text


def test_records_page_reports_book_and_wordle(running_server):
    base, _cfg, _day = running_server
    status, _headers, body = _get(base + "/records")
    assert status == 200
    text = body.decode()
    assert "Some Book" in text
    assert "Book trivia" in text
    assert "Wordle" in text


def test_records_page_shows_name_with_tail_for_best_snr_and_furthest_holders(running_server):
    """_seed_db gives NODE_A ("!aabbccdd") short_name 'Abcd', the highest
    recorded SNR, and a known plausible position -- so it wins Best SNR
    and Furthest observed everywhere those rows appear, and ruling 16
    means the label is "<name> · <tail>", not the bare tail alone."""
    base, _cfg, _day = running_server
    status, _headers, body = _get(base + "/records")
    assert status == 200
    text = body.decode()
    assert "Abcd · ccdd" in text


def test_records_wordle_7d_excludes_rows_older_than_seven_days(tmp_path, monkeypatch):
    """wordle_solves_7d must count only wordle_daily rows within the trailing
    7 calendar days, not just the newest 7 ROWS -- a sparse solve history can
    have far fewer than 7 rows in that window plus much older rows beyond it,
    which the old `ORDER BY day DESC LIMIT 7` (no date bound) would still
    pull in and sum."""
    now = 1705370400  # 2024-01-16 02:00 UTC
    db_path = str(tmp_path / "o.db")
    conn = connect(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO wordle_daily (day, players, solves, total_guesses_on_solve) "
            "VALUES ('2024-01-16', 2, 2, 8)"
        )
        conn.execute(
            "INSERT INTO wordle_daily (day, players, solves, total_guesses_on_solve) "
            "VALUES ('2023-12-01', 50, 100, 400)"
        )
        conn.commit()

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(web.time, "time", lambda: now)
        payload = json.loads(web._api_records(cfg, conn, status={}, stale=False))
    finally:
        conn.close()

    assert payload["games"]["wordle_solves_7d"] == 2


def test_records_windows_week_biggest_day_uses_utc_floor_not_local_day(tmp_path, monkeypatch):
    """_records_windows' 'week' query must bound daily_stats.day (a UTC
    calendar day) by a UTC day floor, not the operator's local day for
    week_start -- otherwise, near a UTC/local day boundary, it can include
    a day one calendar day outside the intended 7-day window."""
    tz = "America/Los_Angeles"
    now = 1705370400  # 2024-01-16 02:00 UTC == 2024-01-15 18:00 PT
    # week_start = now - 7d = 2024-01-09 02:00 UTC == 2024-01-08 18:00 PT:
    # local day is "2024-01-08", UTC day is "2024-01-09".
    db_path = str(tmp_path / "o.db")
    conn = connect(db_path)
    try:
        migrate(conn)
        conn.execute("INSERT INTO daily_stats (day, distinct_senders) VALUES ('2024-01-08', 999)")
        conn.commit()

        base_cfg = _cfg(tmp_path)
        cfg = replace(base_cfg, service=replace(base_cfg.service, timezone=tz))
        monkeypatch.setattr(web.time, "time", lambda: now)
        windows = web._records_windows(cfg, conn)
    finally:
        conn.close()

    _week_w, week_biggest = windows["week"]
    assert week_biggest[0] != "2024-01-08"


@pytest.mark.parametrize(
    "path", ["/nope", "/digest/../x", "/digest/not-a-day", "/nodes/not-a-node"]
)
def test_unknown_or_invalid_paths_404(running_server, path):
    base, _cfg, _day = running_server
    status, _headers, _body = _get(base + path)
    assert status == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_write_methods_are_405(running_server, method):
    base, _cfg, _day = running_server
    status, _headers, _body = _get(base + "/", method=method)
    assert status == 405


def test_disabled_by_default_starts_no_thread(tmp_path):
    cfg = _cfg(tmp_path)
    cfg = replace(cfg, web=WebConfig(enabled=False))
    before = threading.active_count()
    httpd, thread = web.start_web_server(cfg)
    assert httpd is None
    assert thread is None
    assert threading.active_count() == before


def test_refuses_non_loopback_without_allow_lan(tmp_path):
    cfg = _cfg(tmp_path, listen="0.0.0.0", port=0, allow_lan=False)
    with pytest.raises(web.WebRefused):
        web.start_web_server(cfg)


def test_allows_non_loopback_with_allow_lan(tmp_path):
    cfg = _cfg(tmp_path, listen="0.0.0.0", port=0, allow_lan=True)
    httpd, thread = web.start_web_server(cfg)
    try:
        assert httpd is not None
    finally:
        web.stop_web_server(httpd, thread)


def test_empty_database_renders_every_page(tmp_path):
    db_path = str(tmp_path / "o.db")
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    _write_status(db_path, generated_at=time.time())
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        for path in [
            "/",
            "/health",
            "/traffic",
            "/guestbook",
            "/records",
            "/digest",
            "/nodes",
            "/map",
            "/topology",
            "/propagation",
        ]:
            status, _headers, _body = _get(base + path)
            assert status == 200, path
    finally:
        web.stop_web_server(httpd, thread)


def test_missing_database_file_renders_pages(tmp_path):
    """The daemon has never started (no db file at all): every page must
    still render rather than 500."""
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        for path in ["/", "/health", "/nodes", "/map", "/topology", "/propagation"]:
            status, _headers, _body = _get(base + path)
            assert status == 200, path
    finally:
        web.stop_web_server(httpd, thread)


def test_missing_status_file_is_handled_gracefully(tmp_path):
    db_path = str(tmp_path / "o.db")
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    # No status.json written at all.
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, _headers, body = _get(base + "/health")
        assert status == 200
        assert b"stale" in body.lower() or b"unknown" in body.lower()
    finally:
        web.stop_web_server(httpd, thread)


def test_stale_status_file_is_flagged(tmp_path):
    db_path = str(tmp_path / "o.db")
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    _write_status(db_path, generated_at=time.time() - 3600)  # 1h old, far past the 60s threshold
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, _headers, body = _get(base + "/")
        assert status == 200
        assert b"stale" in body.lower()
    finally:
        web.stop_web_server(httpd, thread)


def test_read_status_reports_stale_for_missing_file(tmp_path):
    cfg = _cfg(tmp_path)
    status, stale = web.read_status(cfg)
    assert status == {}
    assert stale is True


def _seed_many_nodes(db_path: str, *, now: int, count: int) -> None:
    conn = connect(db_path)
    try:
        migrate(conn)
        for i in range(count):
            conn.execute(
                "INSERT INTO nodes (node_id, short_name, long_name, observed_first_seen, "
                "observed_last_seen, observation_count, hw_model, role) "
                "VALUES (?, ?, ?, ?, ?, 1, 'TBEAM', 'CLIENT')",
                (f"!{i:08x}", f"N{i}", f"Node {i}", now - 3600, now - i),
            )
        conn.commit()
    finally:
        conn.close()


def test_nodes_page_is_capped_at_200_rows_with_pagination(tmp_path):
    now = int(time.time())
    _seed_many_nodes(str(tmp_path / "o.db"), now=now, count=250)
    _write_status(str(tmp_path / "o.db"), generated_at=time.time())
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, _headers, body = _get(base + "/nodes")
        assert status == 200
        text = body.decode()
        assert text.count('href="/nodes/!') == 200
        assert 'href="/nodes?page=2">older</a>' in text
        assert "newer</a>" not in text

        status2, _headers2, body2 = _get(base + "/nodes?page=2")
        assert status2 == 200
        text2 = body2.decode()
        assert text2.count('href="/nodes/!') == 50
        assert 'href="/nodes?page=1">newer</a>' in text2
    finally:
        web.stop_web_server(httpd, thread)


def test_nodes_page_looks_up_todays_stats_by_utc_day(tmp_path, monkeypatch):
    """node_daily.day is a UTC calendar day (collector.py/database.py), like
    every other node_daily consumer in web.py -- /nodes must match that, not
    the operator's local day, which can name a different calendar date."""
    from zoneinfo import ZoneInfo

    tz = "America/Los_Angeles"
    now_dt = datetime(2024, 1, 15, 18, 0, 0, tzinfo=ZoneInfo(tz))  # 02:00 UTC on the 16th
    now = int(now_dt.timestamp())
    utc_day = "2024-01-16"

    db_path = str(tmp_path / "o.db")
    conn = connect(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO nodes (node_id, short_name, long_name, observed_first_seen, "
            "observed_last_seen, observation_count, hw_model, role) "
            "VALUES (?, 'Zzzz', 'Node Zzzz', ?, ?, 1, 'TBEAM', 'CLIENT')",
            (NODE_A, now - 3600, now),
        )
        conn.execute(
            "INSERT INTO node_daily (node_id, day, packets, direct_packets, best_snr, "
            "snr_sum, snr_count, typical_snr) VALUES (?, ?, 9, 9, 5.0, 5.0, 1, 5.0)",
            (NODE_A, utc_day),
        )
        conn.commit()

        base_cfg = _cfg(tmp_path)
        cfg = replace(base_cfg, service=replace(base_cfg.service, timezone=tz))
        monkeypatch.setattr(web.time, "time", lambda: now)
        body = web._page_nodes(cfg, conn, status={}, stale=False, page=1).decode()
    finally:
        conn.close()

    assert "<td>9</td>" in body


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0", 1),
        ("-5", 1),
        ("abc", 1),
        ("", 1),
        ("1", 1),
        ("10000", 10000),
        ("10001", 1),
        ("999999", 1),
    ],
)
def test_parse_page_caps_out_of_range_values(raw, expected):
    assert web._parse_page({"page": [raw]}) == expected


def test_head_request_on_nodes_returns_headers_and_no_body(running_server):
    base, _cfg, _day = running_server
    status, headers, body = _get(base + "/nodes", method="HEAD")
    assert status == 200
    assert headers.get("Content-Type", "").startswith("text/html")
    assert int(headers.get("Content-Length", "0")) > 0
    assert body == b""


def test_server_stop_leaves_no_leaked_threads(running_server):
    # The fixture's own teardown already asserts `not thread.is_alive()`;
    # this test just confirms the active thread count settles back down
    # after a request has been served (per-connection handler threads are
    # daemon_threads=True and short-lived).
    base, _cfg, _day = running_server
    _get(base + "/")
    time.sleep(0.05)
    baseline = threading.active_count()
    for _ in range(3):
        _get(base + "/")
    time.sleep(0.2)
    assert threading.active_count() <= baseline + 1


# ---------------------------------------------------------------------------
# batch 3b: /map, extended /nodes/<id>, /topology, /propagation
# ---------------------------------------------------------------------------

OBS_LAT, OBS_LON = 37.7749, -122.4194  # matches _cfg()'s ObservatoryConfig

NODE_B = "!bbbbbbbb"  # 2-hop, ~100km away
NODE_C = "!cccccccc"  # 3+ hops, further away
NODE_D = "!dddddddd"  # has a position but no node_daily row -> "unknown" class
NODE_E = "!eeeeeeee"  # link appeared in the last 24h
NODE_F = "!ffffffff"  # link "disappeared" (quiet for the last 24h)

NODE_B_POS = (38.6, -121.4)
NODE_C_POS = (40.0, -120.0)
NODE_D_POS = (36.9, -121.9)
FAR_POS = (39.0, -121.0)  # ~150km -- used for the propagation "unusual distance" day
NEAR_POS = (37.8, -122.35)  # a few km -- used for the other 13 days


def _seed_observatory_extra(db_path: str, *, now: int) -> None:
    """Adds batch-3b fixtures on top of `_seed_db`'s NODE_A: extra nodes at
    known positions/hop classes, extra `links` rows for the topology
    appeared/disappeared sections, and 14 days of `node_daily` history
    (with positions) on NODE_A for the propagation page."""
    conn = connect(db_path)
    try:
        migrate(conn)

        def _insert_node(node_id, name, pos, *, role, first_seen, last_seen):
            conn.execute(
                "INSERT INTO nodes (node_id, short_name, long_name, observed_first_seen, "
                "observed_last_seen, observation_count, hw_model, role, "
                "last_position_lat, last_position_lon, last_position_at) "
                "VALUES (?, ?, ?, ?, ?, 3, 'TBEAM', ?, ?, ?, ?)",
                (node_id, name, f"Node {name}", first_seen, last_seen, role, *pos, last_seen),
            )

        old_first_seen = now - 3 * 86400  # well outside the "new" (<=24h) window
        _insert_node(
            NODE_B,
            "Bbbb",
            NODE_B_POS,
            role="ROUTER",
            first_seen=old_first_seen,
            last_seen=now - 100,
        )
        _insert_node(
            NODE_C,
            "Cccc",
            NODE_C_POS,
            role="CLIENT",
            first_seen=old_first_seen,
            last_seen=now - 200,
        )
        _insert_node(
            NODE_D,
            "Dddd",
            NODE_D_POS,
            role="CLIENT",
            first_seen=old_first_seen,
            last_seen=now - 300,
        )
        # NODE_E: brand new node, "new" filter candidate, first seen 1h ago.
        _insert_node(
            NODE_E, "Eeee", (37.9, -122.2), role="CLIENT", first_seen=now - 3600, last_seen=now - 60
        )
        _insert_node(
            NODE_F,
            "Ffff",
            (37.6, -122.5),
            role="CLIENT",
            first_seen=now - 5 * 86400,
            last_seen=now - 30 * 3600,
        )

        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        conn.execute(
            "INSERT INTO node_daily (node_id, day, packets, direct_packets, best_snr, "
            "snr_sum, snr_count, typical_snr, best_hops, hops_sum, hops_count, typical_hops, "
            "last_lat, last_lon) VALUES (?, ?, 3, 0, 2.0, 6.0, 3, 2.0, 1, 3, 3, 1.0, ?, ?)",
            (NODE_B, today, *NODE_B_POS),
        )
        conn.execute(
            "INSERT INTO node_daily (node_id, day, packets, direct_packets, best_snr, "
            "snr_sum, snr_count, typical_snr, best_hops, hops_sum, hops_count, typical_hops, "
            "last_lat, last_lon) VALUES (?, ?, 2, 0, -3.0, -6.0, 2, -3.0, 2, 4, 2, 2.0, ?, ?)",
            (NODE_C, today, *NODE_C_POS),
        )

        # NODE_A: 13 more days of direct history (today's row already exists
        # from _seed_db), with positions -- 12 "near" days plus one "far"
        # day 13 days ago, for the propagation page's unusual-distance flag.
        for i in range(1, 14):
            day = time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400))
            pos = FAR_POS if i == 13 else NEAR_POS
            snr = 3.0 + (i % 5) * 0.5
            conn.execute(
                "INSERT INTO node_daily (node_id, day, packets, direct_packets, best_snr, "
                "snr_sum, snr_count, typical_snr, best_hops, hops_sum, hops_count, typical_hops, "
                "last_lat, last_lon) VALUES (?, ?, 4, 4, ?, ?, 4, ?, 0, 0, 4, 0.0, ?, ?)",
                (NODE_A, day, snr, snr * 4, snr, *pos),
            )
        # NODE_A now has direct_packets>0 on 14 distinct days in the last
        # 7+ days, well past the "stable neighbour (>=5 of 7 days)" bar.

        # links: NODE_A's own link (from _seed_db) already covers "most
        # frequently heard" / stable neighbours. Add one that appeared in
        # the last 24h and one that went quiet in the 24-48h window.
        conn.execute(
            "INSERT INTO links (a_node_id, b_node_id, first_seen, last_seen, count, kind) "
            "VALUES (?, '!11223344', ?, ?, 2, 'inferred_direct')",
            (NODE_E, now - 3600, now - 60),
        )
        conn.execute(
            "INSERT INTO links (a_node_id, b_node_id, first_seen, last_seen, count, kind) "
            "VALUES (?, '!11223344', ?, ?, 4, 'inferred_direct')",
            (NODE_F, now - 5 * 86400, now - 30 * 3600),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def observatory_server(tmp_path):
    now = int(time.time())
    db_path = str(tmp_path / "o.db")
    _seed_db(db_path, now=now)
    _seed_observatory_extra(db_path, now=now)
    _write_status(db_path, generated_at=time.time())
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    assert httpd is not None and thread is not None
    port = httpd.server_port
    base = f"http://127.0.0.1:{port}"
    yield base, cfg, now
    web.stop_web_server(httpd, thread)


def _no_external_urls(text: str) -> bool:
    return "http://" not in text and "https://" not in text


def test_map_page_plots_nodes_with_classes_and_rings(observatory_server):
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(base + "/map")
    assert status == 200
    text = body.decode()
    assert "<svg" in text
    assert "direct" in text
    assert "2-hop" in text
    assert "3+ hops" in text
    assert "inferred" in text
    assert f'/nodes/{NODE_A}' in text or "aabbccdd" in text
    assert "Abcd · ccdd (direct)" in text
    assert _no_external_urls(text)


def test_map_direct_filter_excludes_non_direct_nodes(observatory_server):
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(base + "/map?direct=1")
    assert status == 200
    text = body.decode()
    assert "bbccdd" in text  # NODE_A tail, direct
    assert "bbbbbb" not in text  # NODE_B tail, 2-hop, filtered out


def test_map_role_filter(observatory_server):
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(base + "/map?role=ROUTER")
    assert status == 200
    text = body.decode()
    assert "bbbbbb" in text  # NODE_B is ROUTER
    assert "cccccc" not in text  # NODE_C is CLIENT


def test_map_invalid_filters_are_ignored_not_500(observatory_server):
    base, _cfg, _now = observatory_server
    for qs in ["?role=NOT_A_ROLE", "?range=bogus", "?direct=maybe", "?new=nah"]:
        status, _headers, _body = _get(base + "/map" + qs)
        assert status == 200, qs


def test_map_new_filter(observatory_server):
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(base + "/map?new=1")
    assert status == 200
    text = body.decode()
    assert "eeeeee" in text  # NODE_E first seen 1h ago
    assert "bbbbbb" not in text  # NODE_B first seen 3 days ago


def test_map_without_observatory_location_is_list_only(tmp_path):
    now = int(time.time())
    db_path = str(tmp_path / "o.db")
    _seed_db(db_path, now=now)
    _write_status(db_path, generated_at=time.time())
    cfg = replace(_cfg(tmp_path), observatory=ObservatoryConfig())
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, _headers, body = _get(base + "/map")
        assert status == 200
        text = body.decode()
        assert "not configured" in text.lower() or "not set" in text.lower()
        assert "<svg" not in text
    finally:
        web.stop_web_server(httpd, thread)


def test_map_empty_database_renders_note(tmp_path):
    db_path = str(tmp_path / "o.db")
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    _write_status(db_path, generated_at=time.time())
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, _headers, body = _get(base + "/map")
        assert status == 200
    finally:
        web.stop_web_server(httpd, thread)


def _seed_many_positioned_nodes(db_path: str, *, now: int, count: int) -> None:
    conn = connect(db_path)
    try:
        migrate(conn)
        for i in range(count):
            conn.execute(
                "INSERT INTO nodes (node_id, short_name, long_name, observed_first_seen, "
                "observed_last_seen, observation_count, hw_model, role, "
                "last_position_lat, last_position_lon, last_position_at) "
                "VALUES (?, ?, ?, ?, ?, 1, 'TBEAM', 'CLIENT', ?, ?, ?)",
                (
                    f"!{i:08x}",
                    f"N{i}",
                    f"Node {i}",
                    now - 3600,
                    now - i,
                    37.7 + i * 0.001,
                    -122.4 + i * 0.001,
                    now - i,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_map_caps_plotted_nodes_at_300(tmp_path):
    now = int(time.time())
    db_path = str(tmp_path / "o.db")
    _seed_many_positioned_nodes(db_path, now=now, count=350)
    _write_status(db_path, generated_at=time.time())
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, _headers, body = _get(base + "/map")
        assert status == 200
        text = body.decode()
        assert text.count('href="/nodes/!') <= 300
        # cap_note must show the real pre-slice match count (350), not the
        # post-slice length (300) for both halves of the sentence.
        assert "Showing the nearest 300 of 350 positioned nodes" in text
    finally:
        web.stop_web_server(httpd, thread)


def test_map_sql_fetch_is_bounded_before_the_python_distance_sort(tmp_path):
    """More positioned nodes than the map's SQL fetch bound (_MAP_FETCH_LIMIT
    = 2000): the query must not pull every matching row -- pre-slice
    `matched_count` (shown in cap_note) should reflect the SQL LIMIT, not
    the true 2,500-row total, and the page must still render only the
    nearest 300."""
    now = int(time.time())
    db_path = str(tmp_path / "o.db")
    _seed_many_positioned_nodes(db_path, now=now, count=2500)
    _write_status(db_path, generated_at=time.time())
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, _headers, body = _get(base + "/map")
        assert status == 200
        text = body.decode()
        assert text.count('href="/nodes/!') <= 300
        assert "of 2,000 positioned nodes" in text
        assert "of 2,500 positioned nodes" not in text
    finally:
        web.stop_web_server(httpd, thread)


def test_node_profile_extended_fields(observatory_server):
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(f"{base}/nodes/{NODE_A}")
    assert status == 200
    text = body.decode()
    assert "Days seen" in text
    assert "Best SNR" in text
    assert "Typical" in text
    assert "Packets" in text
    assert "Battery" in text
    assert "30 day" in text.lower() or "30-day" in text.lower()
    assert "first discovered" in text.lower()
    assert "strongest snr" in text.lower()
    assert _no_external_urls(text)


def test_node_profile_position_known_with_distance_and_bearing(observatory_server):
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(f"{base}/nodes/{NODE_B}")
    assert status == 200
    text = body.decode()
    assert "km" in text
    assert "bearing" in text.lower() or "°" in text


def test_node_profile_unknown_position_or_hops(observatory_server):
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(f"{base}/nodes/{NODE_D}")
    assert status == 200
    text = body.decode()
    assert "unknown" in text.lower()


def test_topology_page_has_neighbour_tables_and_caveat(observatory_server):
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(base + "/topology")
    assert status == 200
    text = body.decode()
    assert "most frequently heard" in text.lower()
    assert "stable neighbours" in text.lower()
    assert "appeared" in text.lower()
    assert "disappeared" in text.lower()
    assert "observed" in text.lower() and "inferred" in text.lower()
    assert "eeeeee" in text  # appeared in the last 24h
    assert "ffffff" in text  # disappeared (quiet for the last 24h)
    assert _no_external_urls(text)


def test_topology_two_hop_nodes_marked_not_derivable(observatory_server):
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(base + "/topology")
    assert status == 200
    text = body.decode()
    assert "bbbbbb" in text  # NODE_B, 2-hop
    assert "not derived" in text.lower() or "not derivable" in text.lower()


def test_propagation_page_has_snr_table_and_unusual_distance(observatory_server):
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(base + "/propagation")
    assert status == 200
    text = body.decode()
    assert "snr" in text.lower()
    assert "unusual distance" in text.lower()
    assert "todo" in text.lower()  # weather TODO stays
    assert _no_external_urls(text)


def test_propagation_flags_the_far_day(observatory_server):
    base, _cfg, now = observatory_server
    status, _headers, body = _get(base + "/propagation")
    assert status == 200
    text = body.decode()
    far_day = time.strftime("%Y-%m-%d", time.gmtime(now - 13 * 86400))
    assert far_day in text
    near_dist = haversine_km(OBS_LAT, OBS_LON, *NEAR_POS)
    far_dist = haversine_km(OBS_LAT, OBS_LON, *FAR_POS)
    assert far_dist > 1.5 * near_dist  # sanity check on the fixture itself


def test_propagation_empty_database_renders_note(tmp_path):
    db_path = str(tmp_path / "o.db")
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    _write_status(db_path, generated_at=time.time())
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, _headers, body = _get(base + "/propagation")
        assert status == 200
    finally:
        web.stop_web_server(httpd, thread)


def test_nav_includes_observatory_pages(observatory_server):
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(base + "/")
    assert status == 200
    text = body.decode()
    assert 'href="/map"' in text
    assert 'href="/topology"' in text
    assert 'href="/propagation"' in text


@pytest.mark.parametrize("path", ["/map", "/topology", "/propagation"])
def test_observatory_pages_head_request(observatory_server, path):
    base, _cfg, _now = observatory_server
    status, headers, body = _get(base + path, method="HEAD")
    assert status == 200
    assert headers.get("Content-Type", "").startswith("text/html")
    assert int(headers.get("Content-Length", "0")) > 0
    assert body == b""


@pytest.mark.parametrize("path", ["/map", "/topology", "/propagation"])
def test_observatory_pages_keep_security_headers(observatory_server, path):
    base, _cfg, _now = observatory_server
    status, headers, _body = _get(base + path)
    assert status == 200
    _assert_security_headers(headers)


# ---------------------------------------------------------------------------
# batch 3b fixes: node-profile events discipline, XSS escaping, bounded
# queries on /map, /topology, /propagation
# ---------------------------------------------------------------------------


def _seed_hops_snr_history(db_path: str, *, now: int, node_id: str, days) -> None:
    """Minimal node + `node_daily` rows for testing the /nodes/<id> events
    list. `days` is an iterable of (days_ago, best_hops, best_snr) tuples."""
    conn = connect(db_path)
    try:
        migrate(conn)
        oldest_days_ago = max(d for d, _, _ in days)
        conn.execute(
            "INSERT INTO nodes (node_id, short_name, long_name, observed_first_seen, "
            "observed_last_seen, observation_count) VALUES (?, 'Ev', 'Event Node', ?, ?, 1)",
            (node_id, now - (oldest_days_ago + 1) * 86400, now),
        )
        for days_ago, best_hops, best_snr in days:
            day = time.strftime("%Y-%m-%d", time.gmtime(now - days_ago * 86400))
            conn.execute(
                "INSERT INTO node_daily (node_id, day, packets, direct_packets, best_snr, "
                "snr_sum, snr_count, typical_snr, best_hops, hops_sum, hops_count, typical_hops) "
                "VALUES (?, ?, 1, 1, ?, ?, 1, ?, ?, ?, 1, ?)",
                (node_id, day, best_snr, best_snr, best_snr, best_hops, best_hops, best_hops),
            )
        conn.commit()
    finally:
        conn.close()


def _get_node_profile_text(tmp_path, db_path, node_id) -> str:
    _write_status(db_path, generated_at=time.time())
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, _headers, body = _get(f"{base}/nodes/{node_id}")
        assert status == 200
        return body.decode().lower()
    finally:
        web.stop_web_server(httpd, thread)


def test_node_profile_hops_single_day_is_not_reported_as_an_improvement(tmp_path):
    now = int(time.time())
    db_path = str(tmp_path / "o.db")
    node_id = "!e0000001"
    _seed_hops_snr_history(db_path, now=now, node_id=node_id, days=[(0, 4, 3.0)])
    text = _get_node_profile_text(tmp_path, db_path, node_id)
    assert "best hops first observed: 4" in text
    assert "best hops improved" not in text
    assert "strongest snr day" not in text
    assert "only one day of data" in text


def test_node_profile_hops_two_flat_days_claim_no_improvement(tmp_path):
    now = int(time.time())
    db_path = str(tmp_path / "o.db")
    node_id = "!e0000002"
    _seed_hops_snr_history(
        db_path, now=now, node_id=node_id, days=[(1, 4, 3.0), (0, 4, 3.0)]
    )
    text = _get_node_profile_text(tmp_path, db_path, node_id)
    assert "best hops first observed: 4" in text
    assert "best hops improved" not in text


def test_node_profile_hops_two_days_improving_is_reported_with_baseline(tmp_path):
    now = int(time.time())
    db_path = str(tmp_path / "o.db")
    node_id = "!e0000003"
    _seed_hops_snr_history(
        db_path, now=now, node_id=node_id, days=[(1, 5, 2.0), (0, 3, 4.0)]
    )
    text = _get_node_profile_text(tmp_path, db_path, node_id)
    assert "best hops first observed: 5" in text
    assert "best hops improved to 3 (from 5)" in text
    assert "strongest snr day" in text  # >=2 SNR days -> reports the max


NODE_XSS_SHORT_ID = "!e00000a1"
NODE_XSS_LONG_ID = "!e00000a2"
XSS_SHORT_NAME = "<script>alert(1)</script>"
XSS_LONG_NAME = 'Ampersand & "Quoted" <b>Name</b>'


def _seed_xss_node(db_path: str, *, now: int, node_id: str, short_name, long_name) -> None:
    """A node with a position, a direct link, and today's `node_daily` row,
    so it appears on /map, /topology, /propagation and /nodes/<id> alike --
    with an attacker-controlled short_name/long_name."""
    conn = connect(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO nodes (node_id, short_name, long_name, observed_first_seen, "
            "observed_last_seen, observation_count, hw_model, role, "
            "last_position_lat, last_position_lon, last_position_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 'TBEAM', 'CLIENT', 37.8, -122.3, ?)",
            (node_id, short_name, long_name, now - 3600, now, now),
        )
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        conn.execute(
            "INSERT INTO node_daily (node_id, day, packets, direct_packets, best_snr, "
            "snr_sum, snr_count, typical_snr, best_hops, hops_sum, hops_count, typical_hops, "
            "last_lat, last_lon) VALUES (?, ?, 3, 3, 4.0, 12.0, 3, 4.0, 0, 0, 3, 0.0, ?, ?)",
            (node_id, today, 37.8, -122.3),
        )
        conn.execute(
            "INSERT INTO links (a_node_id, b_node_id, first_seen, last_seen, count, kind) "
            "VALUES (?, '!11223344', ?, ?, 3, 'inferred_direct')",
            (node_id, now - 3600, now),
        )
        conn.commit()
    finally:
        conn.close()


def test_xss_node_names_are_escaped_on_every_observatory_page(tmp_path):
    """short_name and long_name are both attacker-controlled (radio-reported,
    never authenticated). Covers the SVG <title> text nodes on /map and
    /topology as well as the plain HTML tables."""
    now = int(time.time())
    db_path = str(tmp_path / "o.db")
    _seed_xss_node(
        db_path, now=now, node_id=NODE_XSS_SHORT_ID, short_name=XSS_SHORT_NAME, long_name="unused"
    )
    _seed_xss_node(
        db_path, now=now, node_id=NODE_XSS_LONG_ID, short_name=None, long_name=XSS_LONG_NAME
    )
    _write_status(db_path, generated_at=time.time())
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    escaped_short = "&lt;script&gt;alert(1)&lt;/script&gt;"
    escaped_long = "Ampersand &amp; &quot;Quoted&quot; &lt;b&gt;Name&lt;/b&gt;"
    # Each single-node profile page only ever renders that one node's own
    # short_name/long_name; the multi-node list pages render both.
    expectations = {
        f"/nodes/{NODE_XSS_SHORT_ID}": (escaped_short,),
        f"/nodes/{NODE_XSS_LONG_ID}": (escaped_long,),
        "/map": (escaped_short, escaped_long),
        "/topology": (escaped_short, escaped_long),
        "/propagation": (escaped_short, escaped_long),
    }
    try:
        for path, expected_escaped in expectations.items():
            status, _headers, body = _get(base + path)
            assert status == 200, path
            text = body.decode()
            assert "<script>alert(1)</script>" not in text, path
            assert "<b>Name</b>" not in text, path
            for snippet in expected_escaped:
                assert snippet in text, (path, snippet)
    finally:
        web.stop_web_server(httpd, thread)


def _seed_many_links(db_path: str, *, now: int, count: int) -> None:
    """`count` distinct direct-link rows, all recent (last 24h) and none
    younger than 24h -- lands squarely in /topology's "most frequently
    heard" neighbours table, none in "appeared"/"disappeared"."""
    conn = connect(db_path)
    try:
        migrate(conn)
        for i in range(count):
            conn.execute(
                "INSERT INTO links (a_node_id, b_node_id, first_seen, last_seen, count, kind) "
                "VALUES (?, '!11223344', ?, ?, ?, 'inferred_direct')",
                (f"!f{i:07x}", now - 5 * 86400, now - 60, count - i),
            )
        conn.commit()
    finally:
        conn.close()


def test_topology_neighbour_query_is_capped_at_500(tmp_path):
    now = int(time.time())
    db_path = str(tmp_path / "o.db")
    _seed_many_links(db_path, now=now, count=600)
    _write_status(db_path, generated_at=time.time())
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, _headers, body = _get(base + "/topology")
        assert status == 200
        text = body.decode()
        assert text.count('href="/nodes/!f') <= 500
    finally:
        web.stop_web_server(httpd, thread)


def _seed_many_direct_nodes(db_path: str, *, now: int, count: int) -> None:
    conn = connect(db_path)
    try:
        migrate(conn)
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        for i in range(count):
            conn.execute(
                "INSERT INTO node_daily (node_id, day, packets, direct_packets, best_snr, "
                "snr_sum, snr_count, typical_snr) VALUES (?, ?, 1, 1, 3.0, 3.0, 1, 3.0)",
                (f"!d{i:07x}", today),
            )
        conn.commit()
    finally:
        conn.close()


def test_propagation_direct_node_query_is_capped_at_200(tmp_path):
    now = int(time.time())
    db_path = str(tmp_path / "o.db")
    _seed_many_direct_nodes(db_path, now=now, count=250)
    _write_status(db_path, generated_at=time.time())
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, _headers, body = _get(base + "/propagation")
        assert status == 200
        text = body.decode()
        assert text.count("<h3>") <= 200
    finally:
        web.stop_web_server(httpd, thread)


# ---------------------------------------------------------------------------
# distance-plausibility rulings (RF-only records, position/hop-distance
# agreement): / and /records incl.-MQTT figure, /map hollow markers,
# /nodes/<id> verdict, /propagation excluding implausible distances.
# ---------------------------------------------------------------------------


def test_now_and_records_show_incl_mqtt_labelled_figure(observatory_server):
    base, _cfg, _now = observatory_server
    for path in ("/", "/records"):
        status, _headers, body = _get(base + path)
        assert status == 200
        text = body.decode()
        assert "Furthest observed (incl. MQTT-relayed/unknown)" in text


def test_map_marks_implausible_node_hollow_and_excludes_it_from_rings(observatory_server):
    """NODE_C (best_hops=2, ~324 km from the observatory) is implausible
    under the default thresholds (2 hops * 150 km/hop = 300 km ceiling);
    NODE_B (best_hops=1, ~128 km) is well within its 150 km ceiling."""
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(base + "/map")
    assert status == 200
    text = body.decode()
    assert "implausible" in text.lower()
    assert "hollow marker" in text.lower()
    # NODE_C's row is flagged in the table.
    c_row = [line for line in text.splitlines() if "cccccc" in line]
    assert any("implausible" in line.lower() for line in c_row)


def test_node_profile_shows_implausible_verdict_for_far_node(observatory_server):
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(f"{base}/nodes/{NODE_C}")
    assert status == 200
    text = body.decode()
    assert "position/distance implausible for hops (excluded from records)" in text


def test_node_profile_shows_plausible_verdict_for_near_direct_node(observatory_server):
    base, _cfg, _now = observatory_server
    status, _headers, body = _get(f"{base}/nodes/{NODE_A}")
    assert status == 200
    text = body.decode()
    assert "Plausibility: plausible" in text


def test_propagation_excludes_implausible_distance_from_unusual(tmp_path):
    """A day whose recorded direct-node distance is too far for hops=0
    (> max_direct_km) must never show up in /propagation's per-day
    "unusual distance" table at all -- it is excluded from records, not
    merely flagged."""
    now = int(time.time())
    db_path = str(tmp_path / "o.db")
    conn = connect(db_path)
    try:
        migrate(conn)
        near = (37.79, -122.41)  # a few km from OBS_LAT/OBS_LON
        far = (41.5, -122.4194)  # ~414 km -- past the 400 km default ceiling
        for i in range(1, 4):
            day = time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400))
            pos = far if i == 2 else near
            conn.execute(
                "INSERT INTO node_daily (node_id, day, packets, direct_packets, best_snr, "
                "snr_sum, snr_count, typical_snr, best_hops, hops_sum, hops_count, "
                "typical_hops, last_lat, last_lon) VALUES (?, ?, 4, 4, 3.0, 12.0, 4, 3.0, "
                "0, 0, 4, 0.0, ?, ?)",
                (NODE_A, day, *pos),
            )
        conn.commit()
    finally:
        conn.close()
    _write_status(db_path, generated_at=time.time())
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, _headers, body = _get(base + "/propagation")
        assert status == 200
        text = body.decode()
        far_day = time.strftime("%Y-%m-%d", time.gmtime(now - 2 * 86400))
        far_dist = haversine_km(OBS_LAT, OBS_LON, *far)
        assert far_dist > cfg.observatory.max_direct_km  # sanity check on the fixture
        # The far day legitimately appears in the unrelated SNR-by-day
        # section (SNR is unaffected by distance plausibility) -- only the
        # "Unusual distance" section itself must exclude it.
        unusual_section = text.split("Unusual distance</h2>", 1)[1]
        assert far_day not in unusual_section
    finally:
        web.stop_web_server(httpd, thread)


def test_summary_json_endpoint_reports_today_hourly_and_recent_nodes(running_server):
    base, _cfg, day = running_server
    status, headers, body = _get(base + "/api/summary.json")
    assert status == 200
    assert headers.get("Content-Type", "").startswith("application/json")
    _assert_security_headers(headers)
    data = json.loads(body)
    assert data["day"] == day and data["tz"] == "UTC" and data["stale"] is False
    assert set(data["packets_today"]) == {"raw", "unique"}
    assert data["distinct_senders_today"] >= 1
    assert data["busiest_hour"] is None or 0 <= data["busiest_hour"] <= 23
    assert data["guestbook_visible"] >= 0
    assert len(data["hourly"]["labels"]) == len(data["hourly"]["packets"]) == 24
    assert sum(data["hourly"]["packets"]) >= 1
    node_ids = [n["node_id"] for n in data["recent_nodes"]]
    assert NODE_A in node_ids
    assert set(data["recent_nodes"][0]) == {"node_id", "short_name", "last_heard", "snr"}
    assert set(data["status"]) == {
        "radio_connected", "reply_ready", "uptime_seconds", "queue_depth", "last_packet_at"
    }
    # Seed: five unique RF packets, all heard direct, one positioned node.
    health = data["mesh_24h"]
    assert set(health) == {
        "direct_share", "echo_rate", "avg_hops", "hop_counts",
        "nodes_with_position", "channel_utilization", "air_util_tx",
    }
    assert health["direct_share"] == 1.0 and health["echo_rate"] == 1.0
    assert health["avg_hops"] == 0.0 and health["hop_counts"] == [5]
    assert health["nodes_with_position"] == 1
    assert health["channel_utilization"] is None and health["air_util_tx"] is None
    bot = data["bot_today"]
    assert set(bot) == {"commands", "replies", "signatures", "book_solves", "wordle_solves"}
    assert bot["signatures"] == 2 and bot["book_solves"] == 1 and bot["wordle_solves"] == 1
    # Aggregates only: no message text and no positions leave through here.
    assert XSS_MESSAGE.encode() not in body
    assert b"lat" not in body and b"lon" not in body


def test_summary_json_mesh_health_counts_echoes_relays_and_radio_samples(running_server):
    base, cfg, _day = running_server
    now = int(time.time())
    conn = connect(cfg.database.path)
    try:
        # One echo of an existing packet, one packet relayed twice, one via
        # MQTT (excluded from hop figures), and three radio samples.
        conn.execute(
            "INSERT INTO observations (server_time, sender_node_id, dest_node_id, channel, "
            "portnum, packet_id, is_duplicate, hops_away, via_mqtt) "
            "VALUES (?, ?, '^all', 0, 'TEXT_MESSAGE_APP', 1000, 1, 0, 0)",
            (now, NODE_A),
        )
        conn.execute(
            "INSERT INTO observations (server_time, sender_node_id, dest_node_id, channel, "
            "portnum, packet_id, is_duplicate, hops_away, via_mqtt) "
            "VALUES (?, '!22222222', '^all', 0, 'POSITION_APP', 7, 0, 2, 0)",
            (now,),
        )
        conn.execute(
            "INSERT INTO observations (server_time, sender_node_id, dest_node_id, channel, "
            "portnum, packet_id, is_duplicate, hops_away, via_mqtt) "
            "VALUES (?, '!33333333', '^all', 0, 'POSITION_APP', 8, 0, 0, 1)",
            (now,),
        )
        conn.executemany(
            "INSERT INTO local_radio_samples (server_time, channel_utilization, air_util_tx) "
            "VALUES (?, ?, ?)",
            [(now - 10, 4.0, 0.5), (now - 20, 9.0, 1.5), (now - 30, 30.0, None)],
        )
        conn.commit()
    finally:
        conn.close()
    _status, _headers, body = _get(base + "/api/summary.json")
    health = json.loads(body)["mesh_24h"]
    assert health["hop_counts"] == [5, 0, 1]
    assert health["direct_share"] == pytest.approx(5 / 6)
    assert health["avg_hops"] == pytest.approx(2 / 6)
    # 8 raw events over 7 unique packets (the MQTT one is still a packet).
    assert health["echo_rate"] == pytest.approx(8 / 7)
    assert health["channel_utilization"] == 9.0 and health["air_util_tx"] == 1.0


def test_summary_json_renders_on_an_empty_database(tmp_path):
    db_path = str(tmp_path / "o.db")
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    try:
        status, _headers, body = _get(f"http://127.0.0.1:{httpd.server_port}/api/summary.json")
    finally:
        web.stop_web_server(httpd, thread)
    assert status == 200
    data = json.loads(body)
    assert data["stale"] is True and data["recent_nodes"] == [] and data["busiest_hour"] is None
    assert data["status"]["radio_connected"] is None
    assert data["mesh_24h"]["direct_share"] is None and data["mesh_24h"]["hop_counts"] == []
    assert data["mesh_24h"]["nodes_with_position"] == 0
    assert data["bot_today"]["signatures"] == 0 and data["bot_today"]["commands"] == {}


def test_topology_json_endpoint_lists_hop_rings(running_server):
    base, _cfg, _day = running_server
    status, headers, body = _get(base + "/api/topology.json")
    assert status == 200
    assert headers.get("Content-Type", "").startswith("application/json")
    _assert_security_headers(headers)
    data = json.loads(body)
    assert data["tz"] == "UTC" and data["stale"] is False
    assert isinstance(data["nodes"], list)
    for node in data["nodes"]:
        assert node["hops"] in (1, 2)
        assert set(node) == {
            "node_id", "name", "hops", "last_seen", "count", "bearing_deg", "distance_km",
            "stored_route"
        }
        # Polar coordinates only: the position itself never leaves.
        assert "lat" not in node and "lon" not in node


def test_iching_json_endpoint_is_fixed_for_the_day(running_server):
    base, cfg, day = running_server
    status, headers, body = _get(base + "/api/iching.json")
    assert status == 200
    assert headers.get("Content-Type", "").startswith("application/json")
    data = json.loads(body)
    assert data["day"] == day and 1 <= data["number"] <= 64
    assert data["pinyin"] and data["english"] and data["judgment"]
    _status, _h, again = _get(base + "/api/iching.json")
    assert json.loads(again)["number"] == data["number"]


def test_daily_json_defaults_to_the_haiku_for_an_existing_config(running_server):
    """An operator who never set daily_display gets the haiku."""
    base, cfg, day = running_server
    assert cfg.service.daily_display == "haiku"
    data = json.loads(_get(base + "/api/daily.json")[2])
    assert data["kind"] == "haiku"
    assert data["day"] == day and len(data["lines"]) == 3
    assert "text" not in data


def test_haiku_json_endpoint_is_fixed_for_the_day(running_server):
    base, _cfg, day = running_server
    status, headers, body = _get(base + "/api/haiku.json")
    assert status == 200
    assert headers.get("Content-Type", "").startswith("application/json")
    data = json.loads(body)
    assert data["day"] == day and len(data["lines"]) == 3
    assert all(line and "\U0001f331" not in line for line in data["lines"])
    _status, _h, again = _get(base + "/api/haiku.json")
    assert json.loads(again)["lines"] == data["lines"]


def test_week_json_endpoint_lists_seven_days_today_first(running_server):
    base, _cfg, day = running_server
    status, _headers, body = _get(base + "/api/week.json")
    assert status == 200
    data = json.loads(body)
    days = data["days"]
    assert len(days) == 7 and days[0]["day"] == day
    assert [d["day"] for d in days] == sorted((d["day"] for d in days), reverse=True)
    assert {"packets", "distinct_senders", "new_nodes", "guestbook", "replies"} <= set(days[1])


def test_records_json_endpoint_reports_three_windows(running_server):
    base, _cfg, _day = running_server
    status, headers, body = _get(base + "/api/records.json")
    assert status == 200
    assert headers.get("Content-Type", "").startswith("application/json")
    _assert_security_headers(headers)
    data = json.loads(body)
    assert data["tz"] == "UTC" and data["stale"] is False
    assert set(data["windows"]) == {"today", "week", "all"}
    assert set(data["windows"]["today"]) == {
        "best_snr_db",
        "best_snr_name",
        "max_hops",
        "busiest_hour",
        "biggest_day",
        "furthest_observed_incl_mqtt_km",
        "furthest_observed_incl_mqtt_name",
        "furthest_direct_km",
        "furthest_observed_km",
        "furthest_observed_name",
    }
    assert data["windows"]["today"]["biggest_day"][1] >= 1
    assert set(data["games"]) == {"book_solves", "book_guesses", "wordle_solves_7d"}
    # Aggregates only: no guestbook text leaves through here.
    assert XSS_MESSAGE.encode() not in body


def test_guestbook_json_endpoint_lists_visible_entries_only(running_server):
    base, _cfg, _day = running_server
    status, headers, body = _get(base + "/api/guestbook.json")
    assert status == 200
    assert headers.get("Content-Type", "").startswith("application/json")
    _assert_security_headers(headers)
    data = json.loads(body)
    assert data["visible_count"] == 1
    assert [e["message"] for e in data["entries"]] == [XSS_MESSAGE]
    assert set(data["entries"][0]) == {"entry_id", "created_at", "name", "message", "topic"}
    assert data["entries"][0]["name"] == "Abcd"
    # Today's BBS usage rides along, same digest figures as summary.json.
    bot = data["bot_today"]
    assert set(bot) == {"commands", "replies", "signatures", "book_solves", "wordle_solves"}
    assert bot["signatures"] == 2 and bot["wordle_solves"] == 1
    # Held entries and their moderation reasons stay out of the API.
    assert b"held msg" not in body and b"banned_word" not in body


def test_records_and_guestbook_json_render_on_an_empty_database(tmp_path):
    db_path = str(tmp_path / "o.db")
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    cfg = _cfg(tmp_path)
    httpd, thread = web.start_web_server(cfg)
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        rec_status, _h, rec_body = _get(base + "/api/records.json")
        gb_status, _h2, gb_body = _get(base + "/api/guestbook.json")
    finally:
        web.stop_web_server(httpd, thread)
    assert rec_status == 200 and gb_status == 200
    rec = json.loads(rec_body)
    assert rec["stale"] is True
    assert rec["windows"]["all"]["best_snr_db"] is None
    gb = json.loads(gb_body)
    assert gb["entries"] == [] and gb["visible_count"] == 0


def test_records_feed_keeps_unknown_hop_locations_separate_from_rf_range(tmp_path):
    cfg = _cfg(tmp_path)
    now = int(time.time())
    _seed_db(cfg.database.path, now=now)
    conn = connect(cfg.database.path)
    try:
        conn.execute("UPDATE observations SET hops_away = NULL")
        conn.commit()
        payload = json.loads(web._api_records(cfg, conn, {}, False))
    finally:
        conn.close()
    today = payload["windows"]["today"]
    assert today["furthest_direct_km"] is None
    assert today["furthest_observed_km"] is None
    assert today["furthest_observed_incl_mqtt_km"] > 0
    assert "Abcd" in today["furthest_observed_incl_mqtt_name"]


def test_topology_includes_unknown_routes_but_not_imported_contacts(tmp_path):
    cfg = _cfg(tmp_path)
    now = int(time.time())
    _seed_db(cfg.database.path, now=now)
    conn = connect(cfg.database.path)
    try:
        conn.execute("DELETE FROM links")
        conn.execute("UPDATE observations SET hops_away=NULL, transport='meshcore'")
        conn.execute("UPDATE node_daily SET direct_packets=0, best_hops=NULL")
        conn.execute(
            "INSERT INTO nodes (node_id, observed_first_seen, observed_last_seen) "
            "VALUES ('!00000001', 0, 0)"
        )
        conn.commit()
        payload = json.loads(web._api_topology(cfg, conn, False))
        assert payload["nodes"] == []
        assert len(payload["other_nodes"]) == 1
        node = payload["other_nodes"][0]
        assert node["hops"] is None and node["route"] == "unknown"
        assert node["distance_km"] > 0
        # Saved outgoing routes enrich the API without promoting received routes.
        from meshgarden.collector import Collector
        from meshgarden.models import MeshCoreRoute
        Collector(conn).record_meshcore_route(MeshCoreRoute(node["node_id"], 0, (), now))
        enriched = json.loads(web._api_topology(cfg, conn, False))
        assert enriched["nodes"] == []
        assert enriched["other_nodes"][0]["stored_route"] == {
            "hops": 0, "hashes": [], "cached_at": now,
        }
        # Historical zero-relay MeshCore receptions work before new links arrive.
        conn.execute("UPDATE observations SET hops_away=0")
        conn.commit()
        payload = json.loads(web._api_topology(cfg, conn, False))
        assert len(payload["nodes"]) == 1 and payload["nodes"][0]["hops"] == 1
        assert payload["other_nodes"] == []
    finally:
        conn.close()


def test_health_located_count_excludes_placeholder_and_invalid_positions(tmp_path):
    cfg = _cfg(tmp_path)
    now = int(time.time())
    _seed_db(cfg.database.path, now=now)
    conn = connect(cfg.database.path)
    try:
        for lat, lon, expected in [(0, 0, 0), (91, 10, 0), (37, -122, 1)]:
            conn.execute("UPDATE nodes SET last_position_lat=?, last_position_lon=?", (lat, lon))
            assert web._mesh_health_json(conn, now)["nodes_with_position"] == expected
    finally:
        conn.close()


# --- mesh switcher (shared BBS bridge) ---------------------------------------

PEER_NODE = "!99887766"


def _seed_peer(path: str, *, now: int, short_name: str = "Peerz") -> None:
    conn = connect(path)
    try:
        migrate(conn, transport="meshcore")
        conn.execute(
            "INSERT INTO nodes (node_id, short_name, long_name, observed_first_seen, "
            "observed_last_seen, observation_count) VALUES (?, ?, 'Peer Node', ?, ?, 1)",
            (PEER_NODE, short_name, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _mesh_cfg(tmp_path, *, with_peer: bool, seed_peer: bool = True) -> Config:
    tmp_path.mkdir(exist_ok=True)
    cfg = _cfg(tmp_path)
    now = int(time.time())
    _seed_db(cfg.database.path, now=now)
    peer = str(tmp_path / "peer.db") if with_peer else ""
    if with_peer and seed_peer:
        _seed_peer(peer, now=now)
    return replace(cfg, database=replace(cfg.database, peer_path=peer))


def _page(cfg, path, query=None):
    status, _ctype, body = web._dispatch(cfg, path, query or {})
    return status, body.decode("utf-8")


def test_mesh_switcher_hidden_without_peer(tmp_path):
    for cfg in (
        _mesh_cfg(tmp_path / "a", with_peer=False),
        # Peer configured but the other daemon has not created its file yet.
        _mesh_cfg(tmp_path / "b", with_peer=True, seed_peer=False),
    ):
        status, text = _page(cfg, "/", {"mesh": ["mc"]})
        assert status == 200
        assert "?mesh=" not in text
        assert PEER_NODE not in text


def test_now_page_mesh_mc_uses_peer(tmp_path):
    cfg = _mesh_cfg(tmp_path, with_peer=True)
    _, own = _page(cfg, "/nodes")
    _, peer = _page(cfg, "/nodes", {"mesh": ["mc"]})
    assert "Abcd" in own and "Peerz" not in own
    assert "Peerz" in peer and "Abcd" not in peer
    assert '<a href="/nodes?mesh=mt">Meshtastic</a>' in peer
    assert "<b>MeshCore</b>" in peer
    status, now_page = _page(cfg, "/", {"mesh": ["mc"]})
    assert status == 200 and "<b>MeshCore</b>" in now_page


def test_both_renders_two_sections(tmp_path):
    cfg = _mesh_cfg(tmp_path, with_peer=True)
    status, text = _page(cfg, "/nodes", {"mesh": ["both"]})
    assert status == 200
    assert text.index("<h2>Meshtastic</h2>") < text.index("<h2>MeshCore</h2>")
    assert "Abcd" in text and "Peerz" in text
    assert text.count("</main>") == 1


def test_guestbook_page_reads_bbs(tmp_path):
    cfg = _mesh_cfg(tmp_path, with_peer=True)
    status, text = _page(cfg, "/guestbook", {"mesh": ["mc"]})
    assert status == 200
    # `mesh` is ignored: the guestbook is the own file's, with no switcher.
    assert "Peerz" not in text and "?mesh=" not in text
    assert "hi &lt;script&gt;" in text
    status, body = web._dispatch(cfg, "/api/guestbook.json", {"mesh": ["mc"]})[0::2]
    assert status == 200 and json.loads(body)["entries"]


def test_invalid_mesh_param_falls_back(tmp_path):
    cfg = _mesh_cfg(tmp_path, with_peer=True)
    _, own = _page(cfg, "/nodes")
    for value in ("xx", "", "MC"):
        status, text = _page(cfg, "/nodes", {"mesh": [value]})
        assert status == 200 and "Abcd" in text and "Peerz" not in text
    # Unavailable peer: ?mesh=mc is the own mesh, not an error.
    solo = _mesh_cfg(tmp_path / "solo", with_peer=False)
    status, text = _page(solo, "/nodes", {"mesh": ["mc"]})
    assert status == 200 and "Abcd" in text
