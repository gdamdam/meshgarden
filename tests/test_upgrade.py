import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from meshgarden import bbs_store, database, upgrade
from meshgarden.database import connect, migrate


def _cfg(tmp_path, *, bbs=True, transport="meshtastic"):
    return SimpleNamespace(
        database=SimpleNamespace(
            path=str(tmp_path / "a" / "observatory.db"),
            resolved_bbs_path=str(tmp_path / "shared" / "bbs.db") if bbs else None,
            peer_path="",
        ),
        backup=SimpleNamespace(directory=str(tmp_path / "backups")),
        radio=SimpleNamespace(transport=transport),
        service=SimpleNamespace(fortune_salt="s"),
    )


def _digest(*paths):
    return [hashlib.sha256(open(p, "rb").read()).hexdigest() for p in paths]


def _legacy(cfg):
    conn = connect(cfg.database.path)
    migrate(conn)
    conn.execute(
        "INSERT INTO mail(created_at, sender_node_id, recipient_node_id, message, "
        "message_hash) VALUES (1, '!0000aaaa', '!0000bbbb', 'n', 'h')"
    )
    conn.commit()
    conn.close()


def _split(cfg):
    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
                   transport=cfg.radio.transport)
    migrate(conn, transport=cfg.radio.transport, salt="s")
    return conn


def _as_1_11_0(conn):
    conn.execute("DELETE FROM bbs.bbs_meta WHERE key LIKE 'repair:%'")
    conn.execute("PRAGMA bbs.user_version = 1")
    conn.commit()


def _checkpointed(conn):
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def test_fresh_install_ok_and_nothing_created(tmp_path):
    cfg = _cfg(tmp_path)
    report = upgrade.preflight(cfg)
    assert report.ok
    assert not os.path.exists(cfg.database.path)
    assert report.info["rehearsal"]["bbs_version"] == bbs_store.BBS_SCHEMA_VERSION
    assert upgrade.NOTICE in upgrade.render(report)


def test_legacy_single_file_reports_pending_and_leaves_bytes(tmp_path):
    cfg = _cfg(tmp_path)
    _legacy(cfg)
    c = sqlite3.connect(cfg.database.path)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    before = _digest(cfg.database.path)
    report = upgrade.preflight(cfg)
    assert report.ok, report.problems
    assert report.info["main"]["version"] == database.MIGRATIONS[-1][0]
    assert report.info["bbs"]["exists"] is False
    assert report.info["rehearsal"]["bbs_version"] == bbs_store.BBS_SCHEMA_VERSION
    assert _digest(cfg.database.path) == before
    assert not os.path.exists(cfg.database.resolved_bbs_path)


def test_already_split_1_11_0_emptied_mail_with_routes(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _split(cfg)
    conn.execute(
        "INSERT INTO mail_reply_routes(mail_id, sender_node_id, recipient_node_id, expires_at) "
        "VALUES (7, '!0000aaaa', '!0000bbbb', 9999999999)"
    )
    conn.execute("DELETE FROM bbs.sqlite_sequence WHERE name = 'mail'")
    _as_1_11_0(conn)
    _checkpointed(conn)
    before = _digest(cfg.database.path, cfg.database.resolved_bbs_path)
    report = upgrade.preflight(cfg)
    assert report.ok, report.problems
    assert report.info["bbs"]["version"] == 1
    assert report.info["bbs"]["pending"] == [v for v, _ in bbs_store.BBS_MIGRATIONS]
    assert report.info["rehearsal"]["bbs_version"] == bbs_store.BBS_SCHEMA_VERSION
    assert _digest(cfg.database.path, cfg.database.resolved_bbs_path) == before
    # The live file still has the unrepaired sequence.
    live = sqlite3.connect(cfg.database.resolved_bbs_path)
    assert live.execute("SELECT count(*) FROM sqlite_sequence WHERE name='mail'").fetchone()[0] == 0
    live.close()


def test_1_11_2_marker_file_is_bumped_not_rerun(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    conn = _split(cfg)
    conn.execute("PRAGMA bbs.user_version = 1")  # markers stay
    _checkpointed(conn)
    monkeypatch.setattr(bbs_store, "_repair_sequences",
                        lambda c: pytest.fail("repair re-ran"))
    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
                   transport="meshtastic")
    migrate(conn)
    assert conn.execute("PRAGMA bbs.user_version").fetchone()[0] == bbs_store.BBS_SCHEMA_VERSION
    conn.close()


def test_ambiguous_reply_route_is_warned_not_changed(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _split(cfg)
    conn.execute(
        "INSERT INTO mail(id, created_at, sender_node_id, recipient_node_id, message, "
        "message_hash) VALUES (1, 1, '!0000cccc', '!0000dddd', 'n', 'h')"
    )
    conn.execute(
        "INSERT INTO mail_reply_routes(mail_id, sender_node_id, recipient_node_id, expires_at) "
        "VALUES (1, '!0000aaaa', '!0000bbbb', 9999999999)"
    )
    _as_1_11_0(conn)
    _checkpointed(conn)
    report = upgrade.preflight(cfg)
    assert any("different recipient" in w for w in report.warnings)
    live = sqlite3.connect(cfg.database.resolved_bbs_path)
    assert live.execute("SELECT count(*) FROM mail_reply_routes").fetchone()[0] == 1
    live.close()


def test_interrupted_split_rehearses_to_completion(tmp_path):
    cfg = _cfg(tmp_path)
    _legacy(cfg)
    conn = connect(cfg.database.path, bbs_path=cfg.database.resolved_bbs_path,
                   transport="meshtastic")
    migrate(conn)
    # Simulate a crash after copy, before drop: put the table back in main.
    instance = conn.execute("SELECT value FROM daemon_state WHERE key='instance_id'").fetchone()[0]
    ddl = conn.execute("SELECT sql FROM bbs.sqlite_master WHERE name='news'").fetchone()[0]
    conn.execute(ddl.replace("CREATE TABLE news", "CREATE TABLE main.news"))
    conn.execute("INSERT OR REPLACE INTO bbs.bbs_meta(key, value) VALUES ('moved:news', ?)",
                 (instance,))
    _checkpointed(conn)
    report = upgrade.preflight(cfg)
    assert report.ok, report.problems


def test_main_split_but_bbs_path_unset_is_a_problem(tmp_path):
    cfg = _cfg(tmp_path)
    _checkpointed(_split(cfg))
    report = upgrade.preflight(_cfg(tmp_path, bbs=False))
    assert not report.ok
    assert any("shared BBS" in p for p in report.problems)


def test_main_split_but_bbs_file_missing(tmp_path):
    cfg = _cfg(tmp_path)
    _checkpointed(_split(cfg))
    os.remove(cfg.database.resolved_bbs_path)
    report = upgrade.preflight(cfg)
    assert not report.ok


def test_repeated_migrate_is_stable(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _split(cfg)
    migrate(conn, salt="s")
    migrate(conn, salt="s")
    assert conn.execute("PRAGMA bbs.user_version").fetchone()[0] == bbs_store.BBS_SCHEMA_VERSION
    conn.close()


def test_backup_reported_and_verified(tmp_path):
    cfg = _cfg(tmp_path)
    os.makedirs(cfg.backup.directory)
    good = os.path.join(cfg.backup.directory, "observatory-20260101.db")
    c = sqlite3.connect(good)
    c.execute("CREATE TABLE t(x)")
    c.commit()
    c.close()
    report = upgrade.preflight(cfg)
    assert report.info["backup"]["verified"] is True
    bad = os.path.join(cfg.backup.directory, "observatory-20260102.db")
    with open(bad, "wb") as f:
        f.write(b"not a database" * 100)
    report = upgrade.preflight(cfg)
    assert not report.ok
    assert report.info["backup"]["verified"] is False


def test_failed_rehearsal_removes_tempdir(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _legacy(cfg)
    made = []
    orig = upgrade._make_tempdir
    monkeypatch.setattr(upgrade, "_make_tempdir", lambda: made.append(orig()) or made[-1])

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(database, "migrate", boom)
    report = upgrade.preflight(cfg)
    assert not report.ok and any("boom" in p for p in report.problems)
    assert made and not os.path.exists(made[0])


def test_tempdir_removed_on_success_and_private(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _legacy(cfg)
    made = []
    orig = upgrade._make_tempdir

    def spy():
        d = orig()
        made.append((d, oct(os.stat(d).st_mode & 0o777)))
        return d

    monkeypatch.setattr(upgrade, "_make_tempdir", spy)
    upgrade.preflight(cfg)
    assert made[0][1] == "0o700" and not os.path.exists(made[0][0])


def test_run_json_exit_codes(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    assert upgrade.run(cfg, as_json=True) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and "not authorization" in data["notice"]
    assert upgrade.run(cfg) == 0
    assert "Rehearsal only" in capsys.readouterr().out
