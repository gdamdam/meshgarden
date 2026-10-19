import sqlite3

import pytest

from meshgarden import bbs_store
from meshgarden.bbs_store import BBS_TABLES, SplitConflict
from meshgarden.config import ServiceConfig
from meshgarden.database import SCHEMA_VERSION, connect, migrate
from meshgarden.guestbook import Guestbook


def _names(conn, schema, type_="table"):
    sql = f"SELECT name FROM {schema}.sqlite_master WHERE type = ?"
    return {r[0] for r in conn.execute(sql, (type_,))}


def _legacy(path, *, guestbook=(), mail=(), lab=(), blocked=()):
    """A 1.x single-file database holding the given BBS rows."""
    conn = connect(path)
    migrate(conn)
    for sender in guestbook:
        conn.execute(
            "INSERT INTO guestbook(created_at, sender_node_id, message, message_hash) "
            "VALUES (1, ?, 'hi', ?)",
            (sender, sender),
        )
    for sender, recipient in mail:
        conn.execute(
            "INSERT INTO mail(created_at, sender_node_id, recipient_node_id, message, "
            "message_hash) VALUES (1, ?, ?, 'note', 'h')",
            (sender, recipient),
        )
    for node in lab:
        conn.execute("INSERT INTO lab_nodes(node_id, unlocked_at) VALUES (?, 1)", (node,))
    for node in blocked:
        conn.execute("INSERT INTO blocked_senders(node_id, blocked_at) VALUES (?, 1)", (node,))
    conn.commit()
    conn.close()


def _split(main_path, bbs_path, transport="meshtastic"):
    conn = connect(main_path, bbs_path=bbs_path, transport="meshtastic")
    migrate(conn, transport=transport)
    return conn


@pytest.fixture
def paths(tmp_path):
    return str(tmp_path / "a" / "observatory.db"), str(tmp_path / "shared" / "bbs.db")


def test_fresh_install_creates_bbs_tables_and_main_has_none(paths):
    main_path, bbs_path = paths
    conn = _split(main_path, bbs_path)
    assert set(BBS_TABLES) <= _names(conn, "bbs")
    assert set(BBS_TABLES) & _names(conn, "main") == set()
    assert "book_stats_alltime" in _names(conn, "bbs", "view")
    assert "book_stats_alltime" not in _names(conn, "main", "view")
    assert conn.execute("PRAGMA bbs.user_version").fetchone()[0] == 1
    # Observatory tables stay per-instance.
    assert "nodes" in _names(conn, "main")


def test_legacy_meshtastic_rows_moved_verbatim(paths):
    main_path, bbs_path = paths
    _legacy(main_path, guestbook=["!0000beef", "!a1d3db88"])
    conn = _split(main_path, bbs_path)
    rows = conn.execute("SELECT entry_id, sender_node_id FROM bbs.guestbook").fetchall()
    assert [tuple(r) for r in rows] == [(1, "!0000beef"), (2, "!a1d3db88")]


def test_legacy_meshcore_rows_qualified(paths):
    main_path, bbs_path = paths
    _legacy(
        main_path,
        guestbook=["!e4d495e4", "mc:!00000001"],
        mail=[("!e4d495e4", "!0badf00d")],
        lab=["!e4d495e4"],
        blocked=["!0badf00d"],
    )
    conn = _split(main_path, bbs_path, transport="meshcore")
    senders = conn.execute("SELECT sender_node_id FROM guestbook ORDER BY entry_id")
    assert [r[0] for r in senders] == [
        "mc:!e4d495e4",
        "mc:!00000001",  # already qualified: left alone
    ]
    assert tuple(conn.execute("SELECT sender_node_id, recipient_node_id FROM mail").fetchone()) == (
        "mc:!e4d495e4",
        "mc:!0badf00d",
    )
    assert conn.execute("SELECT node_id FROM lab_nodes").fetchone()[0] == "mc:!e4d495e4"
    assert conn.execute("SELECT node_id FROM blocked_senders").fetchone()[0] == "mc:!0badf00d"


def test_crash_after_copy_before_drop_resumes_without_duplicates(paths):
    main_path, bbs_path = paths
    _legacy(main_path, guestbook=["!a1d3db88", "!0000beef"])
    # Replay ensure_split up to the copy commit of guestbook, then "crash".
    conn = connect(main_path, bbs_path=bbs_path, transport="meshtastic")
    with bbs_store._immediate(conn):
        bbs_store._create_schema(conn)
    instance_id = bbs_store._instance_id(conn)
    with bbs_store._immediate(conn):
        bbs_store._copy_table(conn, "guestbook", "meshtastic")
        conn.execute(
            "INSERT INTO bbs.bbs_meta(key, value) VALUES ('moved:guestbook', ?)", (instance_id,)
        )
    conn.close()

    conn = _split(main_path, bbs_path)
    assert conn.execute("SELECT count(*) FROM bbs.guestbook").fetchone()[0] == 2
    assert "guestbook" not in _names(conn, "main")


def test_second_instance_empty_legacy_tables_dropped(tmp_path):
    bbs_path = str(tmp_path / "bbs.db")
    a, b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    _legacy(a, guestbook=["!a1d3db88"])
    _split(a, bbs_path).close()
    _legacy(b)
    conn = _split(b, bbs_path, transport="meshcore")
    assert set(BBS_TABLES) & _names(conn, "main") == set()
    assert conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == 1


def test_second_instance_nonempty_legacy_conflict_raises(tmp_path):
    bbs_path = str(tmp_path / "bbs.db")
    a, b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    _legacy(a, guestbook=["!a1d3db88"])
    _split(a, bbs_path).close()
    _legacy(b, guestbook=["!e4d495e4"])
    conn = connect(b, bbs_path=bbs_path, transport="meshtastic")
    with pytest.raises(SplitConflict) as exc:
        migrate(conn, transport="meshcore")
    assert b in str(exc.value) and bbs_path in str(exc.value)
    # Nothing was dropped or merged.
    assert conn.execute("SELECT count(*) FROM main.guestbook").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM bbs.guestbook").fetchone()[0] == 1


def test_unqualified_queries_hit_bbs_after_split(paths):
    main_path, bbs_path = paths
    conn = _split(main_path, bbs_path)
    gb = Guestbook(conn, ServiceConfig())
    gb.insert("!a1d3db88", "Alice", "hello", 100)
    conn.commit()
    assert [e.message for e in gb.list_entries(10)] == ["hello"]
    assert conn.execute("SELECT count(*) FROM bbs.guestbook").fetchone()[0] == 1


def test_no_bbs_path_keeps_single_file_mode(paths):
    main_path, _ = paths
    conn = connect(main_path)
    assert migrate(conn) == SCHEMA_VERSION == 19
    assert not bbs_store.is_attached(conn)
    assert set(BBS_TABLES) <= _names(conn, "main")


def test_readonly_attach_sees_bbs_rows(paths):
    main_path, bbs_path = paths
    _legacy(main_path, guestbook=["!a1d3db88"])
    _split(main_path, bbs_path).close()
    conn = connect(main_path, read_only=True, bbs_path=bbs_path, transport="meshtastic")
    assert conn.execute("SELECT sender_node_id FROM guestbook").fetchone()[0] == "!a1d3db88"
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO guestbook(created_at, sender_node_id, message, message_hash) "
                     "VALUES (1, '!x', 'm', 'h')")
    conn.close()
    with pytest.raises(sqlite3.OperationalError):
        connect(main_path, read_only=True, bbs_path=bbs_path + ".missing", transport="meshtastic")


def test_mail_id_sequence_not_reused_after_split(paths):
    main_path, bbs_path = paths
    _legacy(main_path, mail=[("!a", "!b"), ("!a", "!c")])
    conn = connect(main_path)
    conn.execute("DELETE FROM mail WHERE id = 2")
    conn.commit()
    conn.close()
    conn = _split(main_path, bbs_path)
    conn.execute(
        "INSERT INTO mail(created_at, sender_node_id, recipient_node_id, message, message_hash) "
        "VALUES (2, '!a', '!d', 'n', 'h')"
    )
    assert conn.execute("SELECT max(id) FROM mail").fetchone()[0] == 3


def test_mail_id_sequence_carried_when_all_mail_was_collected(paths):
    """Empty mail table, surviving reply route: a new note must not reuse
    id 1, or REPLY #1 would resolve to the old sender."""
    main_path, bbs_path = paths
    _legacy(main_path, mail=[("!0000aaaa", "!0000bbbb")])
    conn = connect(main_path)
    conn.execute(
        "INSERT INTO mail_reply_routes(mail_id, sender_node_id, recipient_node_id, expires_at) "
        "VALUES (1, '!0000aaaa', '!0000bbbb', 9999999999)"
    )
    conn.execute("DELETE FROM mail")
    conn.commit()
    conn.close()
    conn = _split(main_path, bbs_path)
    conn.execute(
        "INSERT INTO mail(created_at, sender_node_id, recipient_node_id, message, message_hash) "
        "VALUES (2, '!0000cccc', '!0000bbbb', 'n', 'h')"
    )
    assert conn.execute("SELECT max(id) FROM mail").fetchone()[0] == 2


def test_meshcore_game_state_rekeyed_to_qualified_id(paths):
    from meshgarden.wordle import player_key

    main_path, bbs_path = paths
    _legacy(main_path)
    conn = connect(main_path)
    conn.execute(
        "INSERT INTO nodes(node_id, observed_first_seen, observed_last_seen) "
        "VALUES ('!e4d495e4', 1, 1)"
    )
    old = player_key("!e4d495e4", "2026-10-19", "salt")
    conn.execute(
        "INSERT INTO wordle_state(player_key, day, guesses, solved, updated_at) "
        "VALUES (?, '2026-10-19', 3, 1, 1)",
        (old,),
    )
    conn.execute(
        "INSERT INTO book_state(player_key, day, solved, updated_at) "
        "VALUES (?, '2026-10-19', 1, 1)",
        (old,),
    )
    conn.commit()
    conn.close()
    conn = connect(main_path, bbs_path=bbs_path, transport="meshcore")
    migrate(conn, transport="meshcore", salt="salt")
    new = player_key("mc:!e4d495e4", "2026-10-19", "salt")
    for table in ("wordle_state", "book_state"):
        assert [r[0] for r in conn.execute(f"SELECT player_key FROM {table}")] == [new]


def test_run_reports_split_conflict_without_traceback(monkeypatch, capsys):
    from meshgarden import main

    def boom(*_a, **_k):
        raise SplitConflict("both hold rows")

    monkeypatch.setattr(main, "Daemon", boom)
    assert main.cmd_run(None) == 1
    assert "both hold rows" in capsys.readouterr().err


def test_conflict_in_later_table_moves_nothing(tmp_path):
    bbs_path = str(tmp_path / "bbs.db")
    a, b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    _legacy(a, blocked=["!0badf00d"])
    _split(a, bbs_path).close()
    # b's guestbook alone would move cleanly; only blocked_senders conflicts.
    _legacy(b, guestbook=["!e4d495e4"], blocked=["!a1d3db88"])
    conn = connect(b, bbs_path=bbs_path, transport="meshtastic")
    with pytest.raises(SplitConflict, match="blocked_senders"):
        migrate(conn)
    assert set(BBS_TABLES) <= _names(conn, "main")
    assert conn.execute("SELECT count(*) FROM bbs.guestbook").fetchone()[0] == 0
    assert not conn.in_transaction


def test_split_main_without_bbs_path_refused(paths):
    main_path, bbs_path = paths
    _split(main_path, bbs_path).close()
    conn = connect(main_path)
    with pytest.raises(SplitConflict, match="database.bbs_path"):
        migrate(conn)
