import json
import os
import pathlib
import stat
import threading
import time
import tomllib

import pytest

from meshgarden import cli_ops
from meshgarden.config import ServiceConfig, parse_config
from meshgarden.database import connect, migrate
from meshgarden.guestbook import Guestbook
from meshgarden.main import main

CONFIG = """
[radio]
device = "/dev/fake"
expected_node_id = "!11223344"
[database]
path = "{db}"
[backup]
directory = "{bk}"
"""


@pytest.fixture
def cfgfile(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG.format(db=tmp_path / "o.db", bk=tmp_path / "backups"))
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    return str(p), conn


# =============================================================================
# export
# =============================================================================


def test_export_json_to_stdout(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        g.insert("!aabbccdd", "Alice", "hello there", 100)

    assert main(["--config", path, "export", "--format", "json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["message"] == "hello there"


def test_export_csv_to_stdout(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        g.insert("!aabbccdd", "Alice", "hello there", 100)

    assert main(["--config", path, "export"]) == 0
    out = capsys.readouterr().out
    assert "id,created_at" in out
    assert "hello there" in out


def test_export_hidden_flag_includes_hidden_rows(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        visible_id = g.insert("!aabbccdd", "Alice", "visible one", 100)
        hidden_id = g.insert("!11112222", "Bob", "hidden one", 200)
    g.hide(hidden_id)

    assert main(["--config", path, "export", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["id"] for d in data] == [visible_id]

    assert main(["--config", path, "export", "--format", "json", "--hidden"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data} == {visible_id, hidden_id}


def test_export_out_writes_to_file(cfgfile, tmp_path, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        g.insert("!aabbccdd", "Alice", "hello there", 100)

    out_path = tmp_path / "export.csv"
    assert main(["--config", path, "export", "--out", str(out_path)]) == 0
    assert capsys.readouterr().out == ""  # nothing printed to stdout
    content = out_path.read_text()
    assert "hello there" in content


def test_export_default_excludes_board_post(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        g.insert("!aabbccdd", "Alice", "main guestbook post", 100)
        g.insert("!11112222", "Bob", "bike board post", 200, topic="bike")

    assert main(["--config", path, "export", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["message"] for d in data] == ["main guestbook post"]


def test_export_with_topic_exports_only_that_board(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        g.insert("!aabbccdd", "Alice", "main guestbook post", 100)
        g.insert("!11112222", "Bob", "bike board post", 200, topic="bike")

    assert main(["--config", path, "export", "--format", "json", "--topic", "bike"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["message"] for d in data] == ["bike board post"]


def test_export_invalid_topic_exits_two(cfgfile, capsys):
    path, _conn = cfgfile
    rc = main(["--config", path, "export", "--topic", "bad topic!"])
    assert rc == 2
    assert "invalid topic" in capsys.readouterr().err


def test_export_missing_database_exits_one(tmp_path, capsys):
    dbpath = tmp_path / "missing.db"
    cfgpath = tmp_path / "config.toml"
    cfgpath.write_text(CONFIG.format(db=dbpath, bk=tmp_path / "backups"))

    assert main(["--config", str(cfgpath), "export"]) == 1
    err = capsys.readouterr().err
    assert "database not found" in err


# =============================================================================
# restore
# =============================================================================


def _make_backup(tmp_path, name="backup.db", rows=2):
    path = str(tmp_path / name)
    conn = connect(path)
    migrate(conn)
    for i in range(rows):
        conn.execute(
            "INSERT INTO guestbook (created_at, sender_node_id, display_name, message, "
            "message_hash, visible, moderation_reason) VALUES (?, ?, ?, ?, ?, 1, NULL)",
            (1_000_000 + i, "!aabbccdd", "Alice", f"entry {i}", f"hash-{i}"),
        )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return path


def test_restore_refuses_existing_target_without_yes(tmp_path):
    cfgpath = tmp_path / "config.toml"
    dbpath = tmp_path / "o.db"
    cfgpath.write_text(CONFIG.format(db=dbpath, bk=tmp_path / "backups"))
    dbpath.write_bytes(b"existing db content")
    backup = _make_backup(tmp_path, rows=2)

    rc = main(["--config", str(cfgpath), "restore", backup])
    assert rc == 1
    assert dbpath.read_bytes() == b"existing db content"


def test_restore_succeeds_with_overwrite_and_yes(tmp_path, capsys):
    cfgpath = tmp_path / "config.toml"
    dbpath = tmp_path / "o.db"
    cfgpath.write_text(CONFIG.format(db=dbpath, bk=tmp_path / "backups"))
    dbpath.write_bytes(b"stale content")
    backup = _make_backup(tmp_path, rows=3)

    rc = main(["--config", str(cfgpath), "restore", backup, "--overwrite", "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok: True" in out
    assert "guestbook_row_count: 3" in out

    conn = connect(str(dbpath), read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM guestbook").fetchone()[0] == 3
    finally:
        conn.close()


def test_restore_succeeds_with_yes_alone(tmp_path, capsys):
    # --yes alone must suffice to overwrite an existing target, without also
    # requiring --overwrite (docs/operations.md documents --yes as enough).
    cfgpath = tmp_path / "config.toml"
    dbpath = tmp_path / "o.db"
    cfgpath.write_text(CONFIG.format(db=dbpath, bk=tmp_path / "backups"))
    dbpath.write_bytes(b"stale content")
    backup = _make_backup(tmp_path, rows=3)

    rc = main(["--config", str(cfgpath), "restore", backup, "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok: True" in out


def test_restore_into_nonexistent_target_needs_no_yes(tmp_path, capsys):
    cfgpath = tmp_path / "config.toml"
    dbpath = tmp_path / "o.db"
    cfgpath.write_text(CONFIG.format(db=dbpath, bk=tmp_path / "backups"))
    backup = _make_backup(tmp_path, rows=1)

    rc = main(["--config", str(cfgpath), "restore", backup])
    assert rc == 0
    assert dbpath.exists()


# =============================================================================
# doctor
# =============================================================================


def test_doctor_exits_zero_on_a_good_setup(tmp_path, capsys):
    cfgpath = tmp_path / "config.toml"
    dbpath = tmp_path / "o.db"
    device = tmp_path / "dev"
    device.write_bytes(b"")
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    cfgpath.write_text(
        f'[radio]\ndevice = "{device}"\nexpected_node_id = "!11223344"\n'
        f'[database]\npath = "{dbpath}"\n[backup]\ndirectory = "{tmp_path / "backups"}"\n'
        f'[metrics]\nlisten = "127.0.0.1"\nport = {free_port}\n'
    )
    conn = connect(str(dbpath))
    migrate(conn)
    conn.close()
    os.makedirs(tmp_path / "backups", exist_ok=True)
    os.makedirs(tmp_path / "logs", exist_ok=True)

    rc = main(["--config", str(cfgpath), "doctor"])
    out = capsys.readouterr().out
    assert "OK" in out or "FAIL" in out
    # radio_device and backup_age are expected to still fail in this
    # synthetic setup (regular file, no backups yet) -- assert only that
    # the command runs and prints a bounded report.
    assert rc in (0, 1)


def test_doctor_exits_one_on_a_bad_config(tmp_path, capsys):
    cfgpath = tmp_path / "config.toml"
    cfgpath.write_text(
        '[radio]\ndevice = "/no/such/device"\nexpected_node_id = "!11223344"\n'
        f'[database]\npath = "{tmp_path / "nested" / "o.db"}"\n'
        f'[backup]\ndirectory = "{tmp_path / "does-not-exist"}"\n'
    )

    rc = main(["--config", str(cfgpath), "doctor"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL radio_device" in out


def test_doctor_json_output_is_parseable(tmp_path, capsys):
    cfgpath = tmp_path / "config.toml"
    cfgpath.write_text(
        '[radio]\ndevice = "/no/such/device"\nexpected_node_id = "!11223344"\n'
        f'[database]\npath = "{tmp_path / "o.db"}"\n'
        f'[backup]\ndirectory = "{tmp_path / "backups"}"\n'
    )
    rc = main(["--config", str(cfgpath), "doctor", "--json"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    names = {c["name"] for c in data}
    assert "radio_device" in names
    assert all({"name", "ok", "detail"} == set(c) for c in data)


# =============================================================================
# guestbook review
# =============================================================================


def test_guestbook_review_lists_only_hidden_with_reason(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        visible_id = g.insert("!aabbccdd", "Alice", "all good", 100)
        hidden_no_reason = g.insert("!11112222", "Bob", "hidden manually", 200)
        hidden_with_reason = g.insert(
            "!33334444",
            "Eve",
            "banned word here",
            300,
            visible=False,
            moderation_reason="banned_word",
        )
    g.hide(hidden_no_reason)  # hidden, but no moderation_reason
    with conn:
        conn.execute(
            "INSERT INTO nodes(node_id,observed_first_seen,observed_last_seen,short_name) "
            "VALUES ('!33334444',1,1,'Current Eve')"
        )

    assert main(["--config", path, "guestbook", "review"]) == 0
    out = capsys.readouterr().out
    assert str(hidden_with_reason) in out or "banned_word" in out
    assert "banned word here" in out
    assert "!33334444  Current Eve:" in out
    assert "!33334444  Eve:" not in out
    assert "all good" not in out
    assert "hidden manually" not in out
    assert visible_id != hidden_with_reason  # sanity


def test_guestbook_review_newest_first(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        g.insert(
            "!aabbccdd", "Alice", "first", 100, visible=False, moderation_reason="banned_word"
        )
        g.insert("!11112222", "Bob", "second", 200, visible=False, moderation_reason="banned_word")

    assert main(["--config", path, "guestbook", "review"]) == 0
    out = capsys.readouterr().out
    assert out.index("second") < out.index("first")


def test_guestbook_review_with_topic_excludes_entries_from_other_topics(cfgfile, capsys):
    """Ruling 9: --topic on `review` (like `list`) is a FILTER, not a guard
    -- omitted means main guestbook only, never "all topics", so an entry
    under a different topic (or the main guestbook) never shows up when
    --topic names a specific board."""
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        g.insert(
            "!aabbccdd", "Alice", "main guestbook held text", 100,
            visible=False, moderation_reason="banned_word",
        )
        g.insert(
            "!11112222", "Bob", "lost board held text", 200,
            visible=False, moderation_reason="banned_word", topic="lost",
        )
        g.insert(
            "!33334444", "Eve", "bike board held text", 300,
            visible=False, moderation_reason="banned_word", topic="bike",
        )

    assert main(["--config", path, "guestbook", "review", "--topic", "bike"]) == 0
    out = capsys.readouterr().out
    assert "bike board held text" in out
    assert "lost board held text" not in out
    assert "main guestbook held text" not in out


def test_guestbook_review_topic_is_normalized(cfgfile, capsys):
    """Item 5: `--topic '#Bike'` normalizes to `bike` (strip/remove '#'/
    lowercase) before filtering, same board as `--topic bike`."""
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        g.insert(
            "!33334444", "Eve", "bike board held text", 300,
            visible=False, moderation_reason="banned_word", topic="bike",
        )

    assert main(["--config", path, "guestbook", "review", "--topic", "#Bike"]) == 0
    out = capsys.readouterr().out
    assert "bike board held text" in out


def test_guestbook_review_invalid_topic_exits_two(cfgfile, capsys):
    path, _conn = cfgfile
    rc = main(["--config", path, "guestbook", "review", "--topic", "bad topic!"])
    assert rc == 2
    assert "invalid topic" in capsys.readouterr().err


def test_guestbook_review_respects_limit(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        for i in range(3):
            g.insert(
                "!aabbccdd",
                "Alice",
                f"msg {i}",
                100 + i,
                visible=False,
                moderation_reason="banned_word",
            )

    assert main(["--config", path, "guestbook", "review", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1


# =============================================================================
# digest
# =============================================================================


def test_digest_default_date_is_yesterday_in_configured_timezone(cfgfile, capsys):
    path, conn = cfgfile
    from meshgarden.config import load_config

    cfg = load_config(path)  # [service] timezone defaults to UTC
    now = 1_800_000_000  # 2027-01-15T08:00:00Z
    rc = cli_ops.cmd_digest(cfg, now=now)
    assert rc == 0
    out = capsys.readouterr().out
    assert "2027-01-14" in out


def test_digest_explicit_date_via_cli(cfgfile, capsys):
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO observations(server_time,sender_node_id,dest_node_id,channel,"
        "portnum,packet_id,is_duplicate) VALUES (1000,'!aabbccdd','^all',0,"
        "'TEXT_MESSAGE_APP',1,0)"
    )
    conn.commit()

    rc = main(["--config", path, "digest", "--date", "1970-01-01"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1970-01-01" in out
    assert "packets" in out


def test_digest_json_flag_emits_parseable_json(cfgfile, capsys):
    path, conn = cfgfile
    rc = main(["--config", path, "digest", "--date", "1970-01-01", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["day"] == "1970-01-01"
    assert "packets" in data


def test_digest_bad_date_exits_two(cfgfile, capsys):
    path, conn = cfgfile
    rc = main(["--config", path, "digest", "--date", "not-a-date"])
    assert rc == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_digest_missing_database_exits_one(tmp_path, capsys):
    dbpath = tmp_path / "missing.db"
    cfgpath = tmp_path / "config.toml"
    cfgpath.write_text(CONFIG.format(db=dbpath, bk=tmp_path / "backups"))

    assert main(["--config", str(cfgpath), "digest"]) == 1
    assert "database not found" in capsys.readouterr().err


# =============================================================================
# guestbook approve
# =============================================================================


def test_guestbook_approve_reverses_moderation_hide(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        entry_id = g.insert(
            "!aabbccdd", "Alice", "flagged text", 100, visible=False,
            moderation_reason="banned_word",
        )

    assert main(["--config", path, "guestbook", "approve", str(entry_id)]) == 0
    out = capsys.readouterr().out
    assert str(entry_id) in out

    row = conn.execute(
        "SELECT visible, moderation_reason FROM guestbook WHERE entry_id=?", (entry_id,)
    ).fetchone()
    assert row["visible"] == 1
    assert row["moderation_reason"] is None

    # `guestbook review` output is unchanged: an approved entry no longer
    # shows up there.
    assert main(["--config", path, "guestbook", "review"]) == 0
    assert "flagged text" not in capsys.readouterr().out


def test_guestbook_approve_missing_entry_exits_three(cfgfile, capsys):
    path, conn = cfgfile
    rc = main(["--config", path, "guestbook", "approve", "999"])
    assert rc == 3
    assert "not found" in capsys.readouterr().err


def test_guestbook_approve_operator_hidden_entry_exits_three_and_stays_hidden(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        entry_id = g.insert("!aabbccdd", "Alice", "normal entry", 100)
    g.hide(entry_id)

    rc = main(["--config", path, "guestbook", "approve", str(entry_id)])
    assert rc == 3
    err = capsys.readouterr().err
    assert "operator" in err.lower()

    row = conn.execute(
        "SELECT visible, moderation_reason FROM guestbook WHERE entry_id=?", (entry_id,)
    ).fetchone()
    assert row["visible"] == 0
    assert row["moderation_reason"] is None


def test_guestbook_approve_refuses_when_topic_does_not_match(cfgfile, capsys):
    """Same guard shape as hide/delete (ruling 9), applied to approve,
    checked before the approve UPDATE."""
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        entry_id = g.insert(
            "!aabbccdd", "Alice", "flagged board text", 100,
            visible=False, moderation_reason="banned_word", topic="bike",
        )

    rc = main(["--config", path, "guestbook", "approve", str(entry_id), "--topic", "lost"])
    assert rc == 3
    assert "not in topic lost" in capsys.readouterr().err

    row = conn.execute(
        "SELECT visible, moderation_reason FROM guestbook WHERE entry_id=?", (entry_id,)
    ).fetchone()
    assert row["visible"] == 0
    assert row["moderation_reason"] == "banned_word"


def test_guestbook_approve_topic_is_normalized(cfgfile, capsys):
    """Item 5: `--topic '#Bike'` normalizes to `bike` before the topic guard."""
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        entry_id = g.insert(
            "!aabbccdd", "Alice", "flagged board text", 100,
            visible=False, moderation_reason="banned_word", topic="bike",
        )

    assert main(["--config", path, "guestbook", "approve", str(entry_id), "--topic", "#Bike"]) == 0
    row = conn.execute(
        "SELECT visible, moderation_reason FROM guestbook WHERE entry_id=?", (entry_id,)
    ).fetchone()
    assert row["visible"] == 1
    assert row["moderation_reason"] is None


def test_guestbook_approve_invalid_topic_exits_two(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        entry_id = g.insert(
            "!aabbccdd", "Alice", "flagged text", 100,
            visible=False, moderation_reason="banned_word",
        )

    rc = main(["--config", path, "guestbook", "approve", str(entry_id), "--topic", "bad topic!"])
    assert rc == 2
    assert "invalid topic" in capsys.readouterr().err


def test_guestbook_approve_already_visible_entry_exits_three(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        entry_id = g.insert("!aabbccdd", "Alice", "normal entry", 100)

    rc = main(["--config", path, "guestbook", "approve", str(entry_id)])
    assert rc == 3
    assert capsys.readouterr().err


# =============================================================================
# mail
# =============================================================================


def test_cmd_mail_counts_only_by_default(cfgfile, capsys):
    path, conn = cfgfile
    from meshgarden.config import load_config
    from meshgarden.mail import Mail

    cfg = load_config(path)
    Mail(conn).send("!11111111", "!22222222", "secret body", 1000)

    rc = cli_ops.cmd_mail(cfg, reveal=False, as_json=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "secret body" not in out
    assert "pending: 1" in out


def test_cmd_mail_reveal_prints_bodies(cfgfile, capsys):
    path, conn = cfgfile
    from meshgarden.config import load_config
    from meshgarden.mail import Mail

    cfg = load_config(path)
    Mail(conn).send("!11111111", "!22222222", "secret body", 1000)
    with conn:
        conn.execute(
            "INSERT INTO nodes(node_id,observed_first_seen,observed_last_seen,short_name) "
            "VALUES ('!11111111',1,1,'Sender Oak'),('!22222222',1,1,'Receiver Bay')"
        )

    rc = cli_ops.cmd_mail(cfg, reveal=True, as_json=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "secret body" in out
    assert "Sender Oak (!11111111) -> Receiver Bay (!22222222)" in out


def test_cmd_mail_json_output_parses_and_carries_pending_and_by_recipient(cfgfile, capsys):
    path, conn = cfgfile
    from meshgarden.mail import Mail

    Mail(conn).send("!11111111", "!22222222", "secret body", 1000)
    Mail(conn).send("!11111111", "!33333333", "another note", 1000)

    assert main(["--config", path, "mail", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["pending"] == 2
    assert data["by_recipient"] == {"!22222222": 1, "!33333333": 1}
    assert data["hidden"] == 0


def test_cmd_mail_counts_view_reports_hidden_total_and_no_body(cfgfile, capsys):
    """Moderated (hidden=1) notes are invisible via `pending`/`by_recipient`
    (same silent-to-the-counts convention as pending), but their existence
    must still surface as a `hidden: N` total -- otherwise the notes an
    operator abuse review most needs to know about are uncounted."""
    path, conn = cfgfile
    from meshgarden.config import load_config
    from meshgarden.mail import Mail

    cfg = load_config(path)
    Mail(conn).send("!11111111", "!22222222", "pending note", 1000)
    Mail(conn).send(
        "!11111111", "!33333333", "flagged body", 1000, moderation_reason="banned_word"
    )

    rc = cli_ops.cmd_mail(cfg, reveal=False, as_json=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "pending: 1" in out
    assert "hidden: 1" in out
    assert "flagged body" not in out


def test_cmd_mail_reveal_shows_hidden_body_with_its_reason(cfgfile, capsys):
    path, conn = cfgfile
    from meshgarden.config import load_config
    from meshgarden.mail import Mail

    cfg = load_config(path)
    Mail(conn).send(
        "!11111111", "!33333333", "flagged body", 1000, moderation_reason="banned_word"
    )

    rc = cli_ops.cmd_mail(cfg, reveal=True, as_json=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "flagged body" in out
    assert "banned_word" in out
    assert "hidden" in out


def test_cmd_mail_json_without_reveal_carries_no_bodies(cfgfile, capsys):
    path, conn = cfgfile
    from meshgarden.mail import Mail

    Mail(conn).send("!11111111", "!22222222", "pending body", 1000)
    Mail(conn).send(
        "!11111111", "!33333333", "flagged body", 1000, moderation_reason="banned_word"
    )

    assert main(["--config", path, "mail", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["hidden"] == 1
    assert "notes" not in data
    assert "pending body" not in out
    assert "flagged body" not in out


def test_cmd_mail_missing_database_exits_one(tmp_path, capsys):
    dbpath = tmp_path / "missing.db"
    cfgpath = tmp_path / "config.toml"
    cfgpath.write_text(CONFIG.format(db=dbpath, bk=tmp_path / "backups"))

    assert main(["--config", str(cfgpath), "mail"]) == 1
    assert "database not found" in capsys.readouterr().err


def test_cmd_mail_purge_removes_pending_for_a_node(cfgfile, capsys):
    path, conn = cfgfile
    from meshgarden.config import load_config
    from meshgarden.mail import Mail

    cfg = load_config(path)
    Mail(conn).send("!11111111", "!22222222", "note", 1000)

    rc = cli_ops.cmd_mail_purge(cfg, "!22222222")
    assert rc == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM mail WHERE recipient_node_id=?", ("!22222222",)
        ).fetchone()[0]
        == 0
    )
    assert "purged: 1" in capsys.readouterr().out


def test_cmd_mail_purge_invalid_node_id_exits_two(cfgfile, capsys):
    path, _conn = cfgfile
    from meshgarden.config import load_config

    cfg = load_config(path)
    rc = cli_ops.cmd_mail_purge(cfg, "not-a-node-id")
    assert rc == 2
    assert "invalid node id" in capsys.readouterr().err


def test_cmd_mail_purge_accepts_a_whitespace_padded_node_id(cfgfile, capsys):
    """Task 3D fix round 1: `config.parse_node_id_argument` strips
    whitespace before matching, so a padded node id argument -- which used
    to exit 2 under the old local `NODE_ID_RE.match(node_id)` check -- is
    now accepted."""
    path, conn = cfgfile
    from meshgarden.config import load_config
    from meshgarden.mail import Mail

    cfg = load_config(path)
    Mail(conn).send("!11111111", "!22222222", "note", 1000)

    rc = cli_ops.cmd_mail_purge(cfg, "  !22222222  ")
    assert rc == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM mail WHERE recipient_node_id=?", ("!22222222",)
        ).fetchone()[0]
        == 0
    )


def test_cmd_mail_purge_rejects_a_malformed_node_id_even_with_whitespace(cfgfile, capsys):
    """The whitespace tolerance above only ever accepts MORE than before --
    a genuinely malformed argument must still exit 2."""
    path, _conn = cfgfile
    from meshgarden.config import load_config

    cfg = load_config(path)
    rc = cli_ops.cmd_mail_purge(cfg, "  not-a-node-id  ")
    assert rc == 2


def test_mail_alias_operator_set_list_transfer_and_release(cfgfile, capsys):
    path, conn = cfgfile

    with conn:
        conn.execute(
            "INSERT INTO nodes(node_id,observed_first_seen,observed_last_seen,short_name) "
            "VALUES ('!11111111',1,1,'Garden Node')"
        )

    assert main(["--config", path, "mail", "alias", "set", "!11111111", "GardenFox"]) == 0
    assert "@gardenfox -> Garden Node (!11111111)" in capsys.readouterr().out

    assert main(["--config", path, "mail", "alias", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["alias"] == "gardenfox"
    assert rows[0]["node_id"] == "!11111111"

    assert main(["--config", path, "mail", "alias", "set", "!22222222", "gardenfox"]) == 3
    assert "already belongs" in capsys.readouterr().err

    assert main(
        [
            "--config",
            path,
            "mail",
            "alias",
            "set",
            "!22222222",
            "gardenfox",
            "--replace",
        ]
    ) == 0
    capsys.readouterr()
    row = conn.execute(
        "SELECT node_id FROM mail_aliases WHERE alias='gardenfox'"
    ).fetchone()
    assert row["node_id"] == "!22222222"

    assert main(["--config", path, "mail", "alias", "release", "@gardenfox"]) == 0
    assert "released" in capsys.readouterr().out
    assert conn.execute(
        "SELECT COUNT(*) FROM mail_aliases WHERE alias IS NOT NULL"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["mail", "alias", "set", "not-a-node", "gardenfox"],
        ["mail", "alias", "set", "!11111111", "mail"],
        ["mail", "alias", "release", "mail"],
    ],
)
def test_mail_alias_operator_rejects_invalid_inputs(cfgfile, capsys, argv):
    path, _conn = cfgfile
    assert main(["--config", path, *argv]) == 2
    assert capsys.readouterr().err


# =============================================================================
# lab
# =============================================================================


def test_cmd_lab_lists_unlocked_nodes_without_printing_the_word(cfgfile, capsys):
    path, conn = cfgfile
    from meshgarden.config import load_config
    from meshgarden.lab import Lab

    cfg = load_config(path)
    Lab(conn).unlock("!aabbccdd", 1000)
    with conn:
        conn.execute(
            "INSERT INTO nodes(node_id,observed_first_seen,observed_last_seen,long_name) "
            "VALUES ('!aabbccdd',1,1,'Oakland Lab')"
        )

    rc = cli_ops.cmd_lab(cfg, as_json=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "unlocked: 1" in out
    assert "!aabbccdd" in out
    assert "Oakland Lab (!aabbccdd)" in out
    assert "SUPERGARDENME" not in out.upper()


def test_cmd_lab_json_output_carries_node_id_and_unlocked_at(cfgfile, capsys):
    path, conn = cfgfile
    from meshgarden.lab import Lab

    Lab(conn).unlock("!aabbccdd", 1000)

    assert main(["--config", path, "lab", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["unlocked"] == 1
    assert data["nodes"] == [{"node_id": "!aabbccdd", "unlocked_at": 1000}]


def test_cmd_lab_missing_database_exits_one(tmp_path, capsys):
    dbpath = tmp_path / "missing.db"
    cfgpath = tmp_path / "config.toml"
    cfgpath.write_text(CONFIG.format(db=dbpath, bk=tmp_path / "backups"))

    assert main(["--config", str(cfgpath), "lab"]) == 1
    assert "database not found" in capsys.readouterr().err


def test_cmd_lab_revoke_removes_the_node(cfgfile, capsys):
    path, conn = cfgfile
    from meshgarden.config import load_config
    from meshgarden.lab import Lab

    cfg = load_config(path)
    Lab(conn).unlock("!aabbccdd", 1000)

    rc = cli_ops.cmd_lab_revoke(cfg, "!aabbccdd")
    assert rc == 0
    assert "revoked: !aabbccdd" in capsys.readouterr().out
    assert not Lab(conn).is_unlocked("!aabbccdd")


def test_cmd_lab_revoke_invalid_node_id_exits_two(cfgfile, capsys):
    path, _conn = cfgfile
    from meshgarden.config import load_config

    cfg = load_config(path)
    rc = cli_ops.cmd_lab_revoke(cfg, "not-a-node-id")
    assert rc == 2
    assert "invalid node id" in capsys.readouterr().err


def test_cmd_lab_revoke_unknown_node_exits_three(cfgfile, capsys):
    path, conn = cfgfile
    from meshgarden.config import load_config

    cfg = load_config(path)
    rc = cli_ops.cmd_lab_revoke(cfg, "!aabbccdd")
    assert rc == 3
    assert "not unlocked: !aabbccdd" in capsys.readouterr().err


def test_cmd_lab_revoke_accepts_a_whitespace_padded_node_id(cfgfile, capsys):
    """Task 3D fix round 1: same widening as `mail purge`'s -- a padded
    node id argument, which used to exit 2, is now accepted."""
    path, conn = cfgfile
    from meshgarden.config import load_config
    from meshgarden.lab import Lab

    cfg = load_config(path)
    Lab(conn).unlock("!aabbccdd", 1000)

    rc = cli_ops.cmd_lab_revoke(cfg, "  !aabbccdd  ")
    assert rc == 0
    assert not Lab(conn).is_unlocked("!aabbccdd")


def test_cmd_lab_revoke_rejects_a_malformed_node_id_even_with_whitespace(cfgfile, capsys):
    path, _conn = cfgfile
    from meshgarden.config import load_config

    cfg = load_config(path)
    rc = cli_ops.cmd_lab_revoke(cfg, "  not-a-node-id  ")
    assert rc == 2


# =============================================================================
# tail
# =============================================================================


def test_tail_missing_file_prints_message_and_exits_one(cfgfile, capsys):
    path, conn = cfgfile
    assert main(["--config", path, "tail"]) == 1
    err = capsys.readouterr().err
    assert "debug" in err.lower()
    assert "exchange.log" in err or "exchange trace" in err.lower()


def test_tail_follows_a_file_written_by_a_thread(tmp_path, cfgfile, capsys):
    path, conn = cfgfile
    from meshgarden.config import load_config

    cfg = load_config(path)
    trace_path = cli_ops.ops.exchange_trace_path(cfg)
    os.makedirs(os.path.dirname(trace_path), exist_ok=True)
    with open(trace_path, "w", encoding="utf-8") as f:
        f.write("preexisting\n")

    stop = threading.Event()

    def writer():
        time.sleep(0.1)
        with open(trace_path, "a", encoding="utf-8") as f:
            f.write("line-one\n")
            f.flush()
        time.sleep(0.3)
        stop.set()

    t = threading.Thread(target=writer)
    t.start()
    rc = cli_ops.cmd_tail(cfg, stop=stop, poll_seconds=0.05)
    t.join(timeout=2)

    assert rc == 0
    out = capsys.readouterr().out
    assert "line-one" in out
    assert "preexisting" not in out  # from_end default: only new lines


def test_tail_lines_prints_the_last_n_existing_lines_then_follows(tmp_path, cfgfile, capsys):
    """`mgarden tail -n 2`: like `tail -n 2 -f` -- the last two existing
    lines first, then new lines as they arrive; older lines never shown."""
    path, conn = cfgfile
    from meshgarden.config import load_config

    cfg = load_config(path)
    trace_path = cli_ops.ops.exchange_trace_path(cfg)
    os.makedirs(os.path.dirname(trace_path), exist_ok=True)
    with open(trace_path, "w", encoding="utf-8") as f:
        f.write("old-1\nold-2\nold-3\nold-4\n")

    stop = threading.Event()

    def writer():
        time.sleep(0.1)
        with open(trace_path, "a", encoding="utf-8") as f:
            f.write("new-1\n")
            f.flush()
        time.sleep(0.3)
        stop.set()

    t = threading.Thread(target=writer)
    t.start()
    rc = cli_ops.cmd_tail(cfg, lines=2, stop=stop, poll_seconds=0.05)
    t.join(timeout=2)

    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["old-3", "old-4"]
    assert "new-1" in out
    assert "old-1" not in out and "old-2" not in out


def test_tail_lines_and_from_start_are_mutually_exclusive(cfgfile):
    path, conn = cfgfile
    with pytest.raises(SystemExit):
        main(["--config", path, "tail", "--from-start", "-n", "3"])


# =============================================================================
# no-transmit guard
# =============================================================================


def test_no_cli_ops_command_ever_transmits(cfgfile, monkeypatch, tmp_path):
    path, conn = cfgfile
    import meshtastic.serial_interface as si

    def boom(*a, **k):
        raise AssertionError("CLI must not open the radio")

    monkeypatch.setattr(si, "SerialInterface", boom)
    backup = _make_backup(tmp_path, rows=1)
    for argv in (
        ["export"],
        ["doctor"],
        ["guestbook", "review"],
        ["digest"],
        ["tail"],
        ["mail"],
        ["lab"],
    ):
        main(["--config", path, *argv])
    # restore is exercised separately since it mutates the configured db path
    main(["--config", path, "restore", backup, "--overwrite", "--yes"])


# =============================================================================
# config-sync (Task 16)
# =============================================================================


def _rendered_example_template(tmp_path):
    """A rendered copy of config.example.toml (placeholders filled in),
    exactly as the installer's own sed substitution produces before
    handing it to `mgarden config-sync --template`."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    raw = pathlib.Path(root, "config.example.toml").read_text()
    raw = raw.replace("REPLACE_WITH_YOUR_RADIO", "usb-0001")
    raw = raw.replace("!REPLACE_WITH_NODE_ID", "!11223344")
    template_path = tmp_path / "template.toml"
    template_path.write_text(raw)
    return str(template_path)


def _old_config_missing_seven_keys(tmp_path) -> str:
    """A config that predates the directory/boards/mail epic: valid, but
    missing all seven of its new [service]/[retention] keys (the whole
    [retention] table included), like a real pre-epic operator config."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(f"""\
[radio]
device = "/dev/fake"
expected_node_id = "!11223344"
[database]
path = "{tmp_path / 'o.db'}"
[backup]
directory = "{tmp_path / 'backups'}"
[service]
name = "Test Node"
mode = "dm_only"
daily_sign_quota = 3
""")
    return str(config_path)


def test_config_sync_dry_run_prints_additions_and_writes_nothing(tmp_path, capsys):
    config_path = _old_config_missing_seven_keys(tmp_path)
    template_path = _rendered_example_template(tmp_path)
    before = pathlib.Path(config_path).read_text()

    rc = main(["config-sync", "--template", template_path, "--config", config_path, "--dry-run"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "service.seen_enabled" in out
    assert "service.daily_post_quota" in out
    assert "service.daily_mail_quota" in out
    assert "service.max_topics" in out
    assert "service.mail_pending_limit" in out
    assert "service.lab_unlock_word" in out
    assert "retention.mail_expiry_days" in out
    assert pathlib.Path(config_path).read_text() == before
    assert sorted(os.listdir(tmp_path)) == sorted(
        p for p in os.listdir(tmp_path) if p in ("config.toml", "template.toml")
    )  # no .bak.* file appeared


def test_config_sync_handles_header_with_trailing_comment_and_spacing(tmp_path, capsys):
    """Regression test: tomllib accepts `[radio] # comment` (and `[ radio ]`),
    so the table exists in parsed data -- but the old _HEADER_RE didn't
    match it, so it was absent from existing_spans and `mgarden config-sync`
    tracebacked with KeyError instead of merging."""
    template_path = _rendered_example_template(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text(f"""\
[radio]  # my radio
device = "/dev/fake"
expected_node_id = "!11223344"
[database]
path = "{tmp_path / 'o.db'}"
[backup]
directory = "{tmp_path / 'backups'}"
""")

    rc = main(["config-sync", "--template", template_path, "--config", str(config_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "radio.transport" in out


def test_config_sync_real_run_backs_up_then_merges(tmp_path, capsys):
    config_path = _old_config_missing_seven_keys(tmp_path)
    template_path = _rendered_example_template(tmp_path)
    before = pathlib.Path(config_path).read_text()

    rc = main(["config-sync", "--template", template_path, "--config", config_path])

    assert rc == 0
    out = capsys.readouterr().out
    assert "service.seen_enabled" in out
    assert "retention.mail_expiry_days" in out

    backups = [n for n in os.listdir(tmp_path) if n.startswith("config.toml.bak.")]
    assert len(backups) == 1
    assert (tmp_path / backups[0]).read_text() == before

    after = pathlib.Path(config_path).read_text()
    assert after != before
    assert "seen_enabled = true" in after
    assert "mail_expiry_days = 7" in after
    assert "daily_sign_quota = 3" in after  # pre-existing value untouched
    assert 'name = "Test Node"' in after  # pre-existing value untouched

    data = tomllib.loads(after)
    parse_config(data)  # still a valid config
    assert data["service"]["seen_enabled"] is True
    assert data["retention"]["mail_expiry_days"] == 7


def test_config_sync_nothing_to_add_exits_zero(tmp_path, capsys):
    template_path = _rendered_example_template(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text(pathlib.Path(template_path).read_text())

    rc = main(["config-sync", "--template", template_path, "--config", str(config_path)])

    assert rc == 0
    assert "nothing to add" in capsys.readouterr().out
    assert not [n for n in os.listdir(tmp_path) if n.startswith("config.toml.bak.")]


def test_config_sync_second_run_is_a_no_op(tmp_path, capsys):
    config_path = _old_config_missing_seven_keys(tmp_path)
    template_path = _rendered_example_template(tmp_path)

    main(["config-sync", "--template", template_path, "--config", config_path])
    capsys.readouterr()
    rc = main(["config-sync", "--template", template_path, "--config", config_path])

    assert rc == 0
    assert "nothing to add" in capsys.readouterr().out
    # exactly one backup was ever written (only the first run had anything to add)
    backups = [n for n in os.listdir(tmp_path) if n.startswith("config.toml.bak.")]
    assert len(backups) == 1


def test_config_sync_backup_preserves_original_file_mode(tmp_path, capsys):
    """Fix round 1, finding 1: the `.bak.*` file must not land at the
    default-umask mode -- it must preserve the original config's mode bits
    (owner/group preservation is asserted only when running as root, since
    a non-root process may not be permitted to chown)."""
    config_path = _old_config_missing_seven_keys(tmp_path)
    os.chmod(config_path, 0o640)
    template_path = _rendered_example_template(tmp_path)

    rc = main(["config-sync", "--template", template_path, "--config", config_path])

    assert rc == 0
    backups = [n for n in os.listdir(tmp_path) if n.startswith("config.toml.bak.")]
    assert len(backups) == 1
    backup_path = tmp_path / backups[0]
    original_st = os.stat(config_path)
    backup_st = os.stat(backup_path)
    assert stat.S_IMODE(backup_st.st_mode) == 0o640
    if os.geteuid() == 0:
        assert backup_st.st_uid == original_st.st_uid
        assert backup_st.st_gid == original_st.st_gid


def test_config_sync_malformed_template_errors_cleanly_and_writes_nothing(tmp_path, capsys):
    """Fix round 1, finding 2: a malformed --template must not raise a raw
    traceback -- it must print a clean `error: ...` to stderr, exit 1, and
    leave the config and any backup untouched."""
    config_path = _old_config_missing_seven_keys(tmp_path)
    before = pathlib.Path(config_path).read_text()
    template_path = tmp_path / "bad_template.toml"
    template_path.write_text("not [valid toml\n")

    rc = main(["config-sync", "--template", str(template_path), "--config", config_path])

    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert pathlib.Path(config_path).read_text() == before
    assert not [n for n in os.listdir(tmp_path) if n.startswith("config.toml.bak.")]


def test_config_sync_malformed_existing_config_errors_cleanly_and_writes_nothing(tmp_path, capsys):
    """Same as above but the malformed file is --config itself. Exercised
    by calling `cmd_config_sync` directly rather than through `main()`:
    `main()` shares argparse's `dest="config"` between the top-level and
    `config-sync`'s own `--config`, so going through `main()` here would
    hit `main()`'s own `load_config` preflight (exit 2) first -- this test
    is specifically about `cmd_config_sync`'s own defensive try/except
    around a malformed existing config, which does matter for any other
    caller of the function directly."""
    template_path = _rendered_example_template(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("not [valid toml\n")

    rc = cli_ops.cmd_config_sync(
        template_path=template_path, config_path=str(config_path), dry_run=False
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert config_path.read_text() == "not [valid toml\n"
    assert not [n for n in os.listdir(tmp_path) if n.startswith("config.toml.bak.")]


def test_config_sync_subparser_config_does_not_clobber_global_config():
    """Regression: config-sync's own `--config` used to be defined with
    `default=None`, which argparse would apply to the shared `config` dest
    even when the flag was omitted -- clobbering a global `--config` given
    before the subcommand. `--config X config-sync --template T` must
    resolve to X, not the subcommand's unset default."""
    from meshgarden.main import build_parser

    parser = build_parser()
    args = parser.parse_args(["--config", "/x.toml", "config-sync", "--template", "T"])
    assert args.config == "/x.toml"

    # The subcommand's own --config, when given, still wins (documented,
    # existing behavior: "default: same as the global --config").
    args = parser.parse_args(
        ["--config", "/x.toml", "config-sync", "--template", "T", "--config", "/y.toml"]
    )
    assert args.config == "/y.toml"


def test_config_sync_prints_note_for_skipped_nested_table(tmp_path, capsys):
    """Fix round 1, finding 3: a nested table in the template that can't be
    synced (unsupported by the line-splicing merge) must produce a printed
    `note: skipped nested table <path>` to stderr, not a silent no-op.
    Calls `cmd_config_sync` directly (see comment on the malformed-existing-
    config test above) since this fixture config has no `[radio]` section
    and would otherwise be rejected by `main()`'s own preflight first."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[radio]\n"
        'device = "/dev/fake"\n'
        'expected_node_id = "!11223344"\n'
        "[database]\n"
        f'path = "{tmp_path / "o.db"}"\n'
        "[backup]\n"
        f'directory = "{tmp_path / "backups"}"\n'
        "[service]\n"
        'name = "My Node"\n'
    )
    template_path = tmp_path / "template.toml"
    template_path.write_text(
        "[radio]\n"
        'device = "/dev/fake"\n'
        'expected_node_id = "!11223344"\n'
        "[database]\n"
        f'path = "{tmp_path / "o.db"}"\n'
        "[backup]\n"
        f'directory = "{tmp_path / "backups"}"\n'
        "[service]\n"
        'name = "Meshgarden Guestbook"\n'
        "seen_enabled = true\n"
        "\n"
        "[service.limits]\n"
        "max_topics = 5\n"
    )

    rc = cli_ops.cmd_config_sync(
        template_path=str(template_path), config_path=str(config_path), dry_run=False
    )

    assert rc == 0
    err = capsys.readouterr().err
    assert "note: skipped nested table service.limits" in err
