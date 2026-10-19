"""WP4: FIND <query> over public guestbook entries and LAB boards."""

import random
import sqlite3
from dataclasses import replace

import pytest

from meshgarden import modules
from meshgarden.config import FindConfig
from meshgarden.main import Daemon
from meshgarden.mod_find import MAX_RESULTS, SCAN_ROWS, factory, find_text
from meshgarden.modules import ModuleRequest
from tests.conftest import HAIKU_RNG_SEED, _cfg
from tests.fakes import FakeTransport, make_packet


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE guestbook (entry_id INTEGER PRIMARY KEY, message TEXT, "
              "visible INTEGER NOT NULL DEFAULT 1, topic TEXT)")
    c.execute("CREATE TABLE mail (id INTEGER PRIMARY KEY, body TEXT)")
    c.execute("INSERT INTO mail (body) VALUES ('secret ferret mail')")
    return c


def _add(conn, message, *, visible=1, topic=None):
    conn.execute("INSERT INTO guestbook (message, visible, topic) VALUES (?,?,?)",
                 (message, visible, topic))


def _find(conn, arg, *, max_bytes=180, channel=False, lab=False):
    return find_text(ModuleRequest(argument=arg, sender="!aabbccdd", now=0,
                                   max_bytes=max_bytes, channel=channel,
                                   lab_unlocked=lab, conn=conn))


def test_matches_case_insensitively_with_id_and_hint(conn):
    _add(conn, "Hello Garden friends")
    out = _find(conn, "garden")
    assert out.startswith("🔎 FIND garden\n#1 Hello Garden friends")
    assert "GUESTBOOK #id" in out


def test_hidden_entries_are_excluded(conn):
    _add(conn, "ferret hidden", visible=0)
    assert "No matches" in _find(conn, "ferret")


def test_boards_need_lab_and_never_appear_on_channel(conn):
    _add(conn, "radio tips", topic="gear")
    assert "No matches" in _find(conn, "radio")
    assert "#1 gear radio tips" in _find(conn, "radio", lab=True)
    assert "No matches" in _find(conn, "radio", lab=True, channel=True)


def test_empty_topic_counts_as_guestbook(conn):
    _add(conn, "plain note", topic="")
    assert "#1 plain note" in _find(conn, "plain")


@pytest.mark.parametrize("query", ["%", "__", "a%b", "\\", "'; DROP TABLE guestbook;--", "a_c"])
def test_wildcards_and_punctuation_are_literal(conn, query):
    _add(conn, "abc and a%b and a_c and back\\slash")
    out = _find(conn, query) if len(query) >= 2 else ""
    if query in ("a%b", "a_c"):
        assert "#1" in out
    elif query == "__":
        assert "No matches" in out
    elif query == "%":
        assert "needs 2-40" in _find(conn, "%")
    assert conn.execute("SELECT COUNT(*) FROM guestbook").fetchone()[0] == 1


def test_unicode_casefold_and_codepoint_safe_budget(conn):
    _add(conn, "Straße café 🌱🌱🌱🌱🌱🌱🌱🌱🌱🌱🌱🌱🌱🌱🌱🌱🌱🌱🌱🌱")
    assert "#1" in _find(conn, "STRASSE")
    out = _find(conn, "café", max_bytes=40)
    assert len(out.encode("utf-8")) <= 40
    out.encode("utf-8").decode("utf-8")  # no split codepoint


def test_query_length_bounds_and_usage(conn):
    assert _find(conn, "").startswith("FIND <text>")
    assert "needs 2-40" in _find(conn, "x")
    assert "needs 2-40" in _find(conn, "x" * 41)


def test_budget_120_and_result_cap(conn):
    for i in range(10):
        _add(conn, f"note {i} " + "word " * 40)
    out = _find(conn, "note", max_bytes=120)
    assert len(out.encode("utf-8")) <= 120
    assert out.count("\n#") <= MAX_RESULTS
    assert _find(conn, "note").count("\n#") == MAX_RESULTS


def test_scan_is_bounded_to_recent_rows(conn):
    _add(conn, "ancient needle")
    for _ in range(SCAN_ROWS):
        _add(conn, "filler")
    assert "No matches" in _find(conn, "needle")


def test_mail_is_never_searched(conn):
    assert "No matches" in _find(conn, "ferret")


def test_factory_disabled_by_default_and_enabled_registers(tmp_path):
    cfg = _cfg(tmp_path)
    assert factory(cfg) == []
    on = replace(cfg, find=FindConfig(enabled=True))
    (mod,) = factory(on)
    assert mod.channel_ok and mod.takes_argument
    assert modules.configured_registry(cfg).lookup("FIND") is None
    assert modules.configured_registry(on).lookup("FIND") is not None


def test_daemon_dm_find_end_to_end(tmp_path):
    cfg = replace(_cfg(tmp_path), find=FindConfig(enabled=True))
    ft = FakeTransport(0x11223344, cfg.radio.expected_node_id, service_cfg=cfg.service)
    d = Daemon(cfg, transport=ft, start_metrics=False, haiku_rng=random.Random(HAIKU_RNG_SEED))
    d.iface = ft.iface
    d.start()
    try:
        d.conn.execute(
            "INSERT INTO guestbook (created_at, sender_node_id, message, message_hash) "
            "VALUES (0, '!aabbccdd', 'sprouting ferns', 'h')")
        d.conn.commit()
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="FIND ferns"))
        d.drain()
        assert "sprouting ferns" in d.iface.sent[0]["text"]
    finally:
        d.stop()
