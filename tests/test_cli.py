import json
import logging
import os
import pathlib
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from meshgarden import __version__
from meshgarden.commands import Command, RequestLog
from meshgarden.config import ServiceConfig
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

CONFIG_TZ = """
[radio]
device = "/dev/fake"
expected_node_id = "!11223344"
[service]
timezone = "America/Los_Angeles"
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


@pytest.fixture
def cfgfile_tz(tmp_path):
    p = tmp_path / "config_tz.toml"
    p.write_text(CONFIG_TZ.format(db=tmp_path / "o.db", bk=tmp_path / "backups"))
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    return str(p), conn


def test_help_exits_zero():
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0


def test_unknown_subcommand_is_usage_error():
    with pytest.raises(SystemExit) as e:
        main(["nonsense"])
    assert e.value.code == 2


def test_version_flag_prints_mgarden_and_the_pep440_version(capsys):
    """`mgarden --version` / `meshtastic-observatory --version` (same
    entry point, see pyproject.toml's [project.scripts]): must exit 0
    without requiring a config file, and print exactly `mgarden
    <__version__>` regardless of which alias invoked it."""
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert out.strip() == f"mgarden {__version__}"


def test_status_runs_without_a_daemon(cfgfile, capsys):
    path, conn = cfgfile
    assert main(["--config", path, "status"]) == 0
    out = capsys.readouterr().out
    assert "expected_node_id" in out or "!11223344" in out
    assert "stale" in out.lower() or "not running" in out.lower()


def test_status_json_is_parseable(cfgfile, capsys):
    path, conn = cfgfile
    assert main(["--config", path, "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    for key in [
        "connected",
        "reply_ready",
        "expected_node_id",
        "actual_node_id",
        "mismatch_reason",
        "uptime_seconds",
        "raw_receive_events",
        "unique_packets",
        "distinct_senders_1h",
        "distinct_senders_24h",
        "guestbook_visible",
        "queue_depth",
        "last_packet_at",
        "storage_used_bytes",
        "storage_pressure",
        "backup_last_success",
        "rate_limited_drops_1h",
        "version",
    ]:
        assert key in data
    assert data["version"] == __version__


def test_db_stats_reports_table_rows(cfgfile, capsys):
    path, conn = cfgfile
    assert main(["--config", path, "db-stats"]) == 0
    out = capsys.readouterr().out
    for t in [
        "observations",
        "nodes",
        "guestbook",
        "processed_requests",
        "daily_stats",
        "blocked_senders",
    ]:
        assert t in out


def test_books_reports_all_time_aggregates(cfgfile, capsys):
    path, conn = cfgfile
    with conn:
        conn.execute(
            "INSERT INTO book_stats (book_key, day, shown, guesses, solves)"
            " VALUES ('1984', '2026-09-13', 2, 3, 1)"
        )
    assert main(["--config", path, "books"]) == 0
    out = capsys.readouterr().out
    assert "1984" in out and "shown=2" in out and "guesses=3" in out and "solves=1" in out


def test_books_json_is_parseable(cfgfile, capsys):
    path, conn = cfgfile
    with conn:
        conn.execute(
            "INSERT INTO book_stats (book_key, day, shown, guesses, solves)"
            " VALUES ('Dune', '2026-09-13', 1, 1, 1)"
        )
    assert main(["--config", path, "books", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == [{"book": "Dune", "shown": 1, "guesses": 1, "solves": 1}]


def test_books_missing_database_exits_one(tmp_path, capsys):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG.format(db=tmp_path / "missing.db", bk=tmp_path / "backups"))
    assert main(["--config", str(p), "books"]) == 1


def test_wordle_reports_daily_aggregates(cfgfile, capsys):
    path, conn = cfgfile
    with conn:
        conn.execute(
            "INSERT INTO wordle_daily (day, players, solves, total_guesses_on_solve)"
            " VALUES ('2026-09-13', 5, 3, 9)"
        )
    assert main(["--config", path, "wordle"]) == 0
    out = capsys.readouterr().out
    assert "2026-09-13" in out and "players=5" in out and "solves=3" in out
    assert "avg_guesses=3.0" in out


def test_wordle_json_is_parseable(cfgfile, capsys):
    path, conn = cfgfile
    with conn:
        conn.execute(
            "INSERT INTO wordle_daily (day, players, solves, total_guesses_on_solve)"
            " VALUES ('2026-09-13', 5, 3, 9)"
        )
    assert main(["--config", path, "wordle", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == [{"day": "2026-09-13", "players": 5, "solves": 3, "avg_guesses": 3.0}]


def test_wordle_respects_days_limit(cfgfile, capsys):
    path, conn = cfgfile
    with conn:
        for i in range(10):
            conn.execute(
                "INSERT INTO wordle_daily (day, players, solves, total_guesses_on_solve)"
                " VALUES (?, 1, 1, 1)",
                (f"2026-09-{i + 1:02d}",),
            )
    assert main(["--config", path, "wordle", "--days", "3", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 3


def test_guestbook_hide_then_delete(cfgfile, capsys):
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO guestbook(created_at,sender_node_id,message,"
        "message_hash) VALUES (1,'!aabbccdd','hi','h')"
    )
    conn.commit()
    assert main(["--config", path, "guestbook", "hide", "1"]) == 0
    assert conn.execute("SELECT visible FROM guestbook WHERE entry_id=1").fetchone()[0] == 0
    assert main(["--config", path, "guestbook", "delete", "1"]) == 0
    assert conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == 0


def test_hiding_a_missing_entry_returns_three(cfgfile):
    path, conn = cfgfile
    assert main(["--config", path, "guestbook", "hide", "999"]) == 3


def test_guestbook_hide_refuses_when_topic_does_not_match(cfgfile, capsys):
    """Ruling 9: --topic on hide/delete/approve is a guard, not a filter --
    the entry_id must belong to the given topic (or, if omitted, be a
    main-guestbook entry) or the command refuses and does nothing."""
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO guestbook(created_at,sender_node_id,message,message_hash,topic)"
        " VALUES (1,'!aabbccdd','hi','h','bike')"
    )
    conn.commit()
    rc = main(["--config", path, "guestbook", "hide", "1", "--topic", "lost"])
    assert rc == 3
    assert "not in topic lost" in capsys.readouterr().err
    assert conn.execute("SELECT visible FROM guestbook WHERE entry_id=1").fetchone()[0] == 1


def test_guestbook_hide_succeeds_when_topic_matches(cfgfile):
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO guestbook(created_at,sender_node_id,message,message_hash,topic)"
        " VALUES (1,'!aabbccdd','hi','h','bike')"
    )
    conn.commit()
    assert main(["--config", path, "guestbook", "hide", "1", "--topic", "bike"]) == 0
    assert conn.execute("SELECT visible FROM guestbook WHERE entry_id=1").fetchone()[0] == 0


def test_guestbook_delete_refuses_when_topic_does_not_match(cfgfile, capsys):
    """Same guard shape as hide (ruling 9), applied to delete."""
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO guestbook(created_at,sender_node_id,message,message_hash,topic)"
        " VALUES (1,'!aabbccdd','hi','h','bike')"
    )
    conn.commit()
    rc = main(["--config", path, "guestbook", "delete", "1", "--topic", "lost"])
    assert rc == 3
    assert "not in topic lost" in capsys.readouterr().err
    assert conn.execute("SELECT count(*) FROM guestbook WHERE entry_id=1").fetchone()[0] == 1


def test_guestbook_list_with_topic_filters_to_that_topic(cfgfile, capsys):
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO guestbook(created_at,sender_node_id,message,message_hash)"
        " VALUES (1,'!aabbccdd','main entry','h1')"
    )
    conn.execute(
        "INSERT INTO guestbook(created_at,sender_node_id,message,message_hash,topic)"
        " VALUES (1,'!aabbccdd','board entry','h2','bike')"
    )
    conn.commit()
    assert main(["--config", path, "guestbook", "list", "--topic", "bike"]) == 0
    out = capsys.readouterr().out
    assert "board entry" in out
    assert "main entry" not in out


def test_guestbook_list_topic_is_normalized_before_filtering(cfgfile, capsys):
    """Final-review fix (item 5): --topic is normalized
    (strip().removeprefix('#').lower()) at the CLI dispatch boundary
    before being compared/used, so '#Bike' still filters to the 'bike'
    board instead of matching nothing."""
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO guestbook(created_at,sender_node_id,message,message_hash,topic)"
        " VALUES (1,'!aabbccdd','board entry','h2','bike')"
    )
    conn.commit()
    assert main(["--config", path, "guestbook", "list", "--topic", "#Bike"]) == 0
    out = capsys.readouterr().out
    assert "board entry" in out


def test_guestbook_list_invalid_topic_exits_two(cfgfile, capsys):
    path, conn = cfgfile
    rc = main(["--config", path, "guestbook", "list", "--topic", "bad topic!"])
    assert rc == 2
    assert "invalid topic" in capsys.readouterr().err


def test_guestbook_hide_topic_is_normalized_before_the_guard(cfgfile):
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO guestbook(created_at,sender_node_id,message,message_hash,topic)"
        " VALUES (1,'!aabbccdd','hi','h','bike')"
    )
    conn.commit()
    assert main(["--config", path, "guestbook", "hide", "1", "--topic", "#Bike"]) == 0
    assert conn.execute("SELECT visible FROM guestbook WHERE entry_id=1").fetchone()[0] == 0


def test_guestbook_hide_invalid_topic_exits_two(cfgfile, capsys):
    path, conn = cfgfile
    rc = main(["--config", path, "guestbook", "hide", "1", "--topic", "bad topic!"])
    assert rc == 2
    assert "invalid topic" in capsys.readouterr().err


def test_guestbook_delete_invalid_topic_exits_two(cfgfile, capsys):
    path, conn = cfgfile
    rc = main(["--config", path, "guestbook", "delete", "1", "--topic", "bad topic!"])
    assert rc == 2
    assert "invalid topic" in capsys.readouterr().err


def test_block_and_unblock(cfgfile):
    path, conn = cfgfile
    assert main(["--config", path, "block", "!aabbccdd"]) == 0
    assert conn.execute("SELECT count(*) FROM blocked_senders").fetchone()[0] == 1
    assert main(["--config", path, "unblock", "!aabbccdd"]) == 0
    assert conn.execute("SELECT count(*) FROM blocked_senders").fetchone()[0] == 0


def test_block_rejects_a_malformed_node_id(cfgfile):
    path, conn = cfgfile
    assert main(["--config", path, "block", "not-a-node"]) == 2


def test_unblocking_a_node_that_is_not_blocked_returns_three(cfgfile):
    path, conn = cfgfile
    assert main(["--config", path, "unblock", "!aabbccdd"]) == 3


@pytest.mark.parametrize(
    "argv_suffix",
    [
        ["block", "  !aabbccdd  "],
        ["unblock", "  !aabbccdd  "],
        ["quota", "reset", "  !aabbccdd  ", "--day", "2026-01-01"],
        ["quota", "show", "  !aabbccdd  ", "--day", "2026-01-01"],
    ],
)
def test_operator_node_id_arguments_tolerate_surrounding_whitespace(cfgfile, argv_suffix):
    """Task 3D fix round 1: `config.parse_node_id_argument` (now shared by
    both transports' `parse_node_argument` AND these operator subcommands)
    strips whitespace before matching, whereas the subcommands' previous
    local `NODE_ID_RE.match(node_id)` check did not -- so a node id argument
    padded with leading/trailing whitespace, which used to exit 2, is now
    accepted. This is a deliberate, flagged widening of the CLI (see
    CHANGELOG.md), not an accident: it only accepts MORE than before, never
    rejects something that used to work. A `blocked_senders` row for
    `!aabbccdd` is pre-seeded so `unblock` also hits its 0 (deleted) exit
    rather than its unrelated 3 (not blocked)."""
    path, conn = cfgfile
    conn.execute("INSERT INTO blocked_senders(node_id, blocked_at) VALUES ('!aabbccdd', 1)")
    conn.commit()
    assert main(["--config", path, *argv_suffix]) == 0


@pytest.mark.parametrize(
    "argv_suffix",
    [
        ["block", "  not-a-node  "],
        ["unblock", "  not-a-node  "],
        ["quota", "reset", "  not-a-node  "],
        ["quota", "show", "  not-a-node  "],
    ],
)
def test_operator_node_id_arguments_still_reject_malformed_ids_with_whitespace(
    cfgfile, argv_suffix
):
    """The whitespace-tolerance above only ever accepts MORE than before --
    an argument that is not a valid node id shape at all, whitespace or not,
    must still exit 2."""
    path, conn = cfgfile
    assert main(["--config", path, *argv_suffix]) == 2


def test_quota_reset_deletes_the_row(cfgfile):
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO node_daily_quota (node_id, day, commands, signs, updated_at) "
        "VALUES ('!aabbccdd', '2026-01-01', 5, 2, 0)"
    )
    conn.commit()
    assert main(["--config", path, "quota", "reset", "!aabbccdd", "--day", "2026-01-01"]) == 0
    assert (
        conn.execute(
            "SELECT 1 FROM node_daily_quota WHERE node_id='!aabbccdd' AND day='2026-01-01'"
        ).fetchone()
        is None
    )


def test_quota_reset_on_a_missing_row_is_a_success(cfgfile, capsys):
    path, conn = cfgfile
    rc = main(["--config", path, "quota", "reset", "!aabbccdd", "--day", "2026-01-01"])
    assert rc == 0
    assert "nothing to reset" in capsys.readouterr().out


def test_quota_reset_signs_only_zeroes_just_the_signs_counter(cfgfile):
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO node_daily_quota (node_id, day, commands, signs, updated_at) "
        "VALUES ('!aabbccdd', '2026-01-01', 5, 2, 0)"
    )
    conn.commit()
    rc = main(
        ["--config", path, "quota", "reset", "!aabbccdd", "--day", "2026-01-01", "--signs-only"]
    )
    assert rc == 0
    row = conn.execute(
        "SELECT commands, signs FROM node_daily_quota WHERE node_id='!aabbccdd' "
        "AND day='2026-01-01'"
    ).fetchone()
    assert row["commands"] == 5
    assert row["signs"] == 0


def test_quota_reset_commands_only_zeroes_just_the_commands_counter(cfgfile):
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO node_daily_quota (node_id, day, commands, signs, updated_at) "
        "VALUES ('!aabbccdd', '2026-01-01', 5, 2, 0)"
    )
    conn.commit()
    rc = main(
        [
            "--config", path, "quota", "reset", "!aabbccdd", "--day", "2026-01-01",
            "--commands-only",
        ]
    )
    assert rc == 0
    row = conn.execute(
        "SELECT commands, signs FROM node_daily_quota WHERE node_id='!aabbccdd' "
        "AND day='2026-01-01'"
    ).fetchone()
    assert row["commands"] == 0
    assert row["signs"] == 2


def test_quota_reset_rejects_a_malformed_node_id(cfgfile):
    path, conn = cfgfile
    assert main(["--config", path, "quota", "reset", "not-a-node"]) == 2


def test_quota_show_prints_the_rows_counts(cfgfile, capsys):
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO node_daily_quota (node_id, day, commands, signs, updated_at) "
        "VALUES ('!aabbccdd', '2026-01-01', 5, 2, 0)"
    )
    conn.commit()
    rc = main(["--config", path, "quota", "show", "!aabbccdd", "--day", "2026-01-01"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "commands=5" in out
    assert "signs=2" in out


def test_quota_show_reports_no_row(cfgfile, capsys):
    path, conn = cfgfile
    rc = main(["--config", path, "quota", "show", "!aabbccdd", "--day", "2026-01-01"])
    assert rc == 0
    assert "no row" in capsys.readouterr().out


def test_quota_show_without_node_lists_all_days_all_counters_and_full_names(cfgfile, capsys):
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO nodes "
        "(node_id, short_name, long_name, observed_first_seen, observed_last_seen) "
        "VALUES ('!aabbccdd', 'Hag', '⚓️Hagbard', 1, 2)"
    )
    conn.executemany(
        "INSERT INTO node_daily_quota "
        "(node_id, day, commands, signs, posts, mails_sent, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        [
            ('!aabbccdd', '2026-01-02', 5, 2, 3, 1),
            ('!11223344', '2026-01-01', 7, 1, 4, 2),
        ],
    )
    conn.commit()

    assert main(["--config", path, "quota", "show"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "⚓️Hagbard (!aabbccdd) 2026-01-02: commands=5 signs=2 posts=3 mails_sent=1"
    assert lines[1] == "!11223344 2026-01-01: commands=7 signs=1 posts=4 mails_sent=2"


def test_quota_show_without_node_can_filter_one_day(cfgfile, capsys):
    path, conn = cfgfile
    conn.executemany(
        "INSERT INTO node_daily_quota "
        "(node_id, day, commands, signs, posts, mails_sent, updated_at) "
        "VALUES (?, ?, 1, 2, 3, 4, 0)",
        [('!aabbccdd', '2026-01-02'), ('!11223344', '2026-01-01')],
    )
    conn.commit()

    assert main(["--config", path, "quota", "show", "--day", "2026-01-01"]) == 0
    out = capsys.readouterr().out
    assert "!11223344 2026-01-01" in out
    assert "!aabbccdd" not in out


def test_quota_reset_accepts_an_exact_long_name_with_emoji_and_spaces(cfgfile):
    path, conn = cfgfile
    conn.execute(
        "INSERT INTO nodes "
        "(node_id, short_name, long_name, observed_first_seen, observed_last_seen) "
        "VALUES ('!aabbccdd', 'Hag', '⚓️ Hagbard Celine', 1, 2)"
    )
    conn.execute(
        "INSERT INTO node_daily_quota "
        "(node_id, day, commands, signs, posts, mails_sent, updated_at) "
        "VALUES ('!aabbccdd', '2026-09-20', 5, 2, 3, 1, 0)"
    )
    conn.commit()

    assert main([
        "--config", path, "quota", "reset", "--day", "2026-09-20",
        "⚓️ Hagbard Celine",
    ]) == 0
    assert conn.execute("SELECT count(*) FROM node_daily_quota").fetchone()[0] == 0


def test_quota_show_rejects_an_ambiguous_exact_name_with_helpful_ids(cfgfile, capsys):
    path, conn = cfgfile
    conn.executemany(
        "INSERT INTO nodes "
        "(node_id, short_name, long_name, observed_first_seen, observed_last_seen) "
        "VALUES (?, ?, ?, 1, 2)",
        [
            ('!aabbccdd', 'same', 'First',),
            ('!11223344', 'Other', 'same',),
        ],
    )
    conn.commit()

    assert main(["--config", path, "quota", "show", "same"]) == 2
    err = capsys.readouterr().err
    assert "ambiguous node name" in err
    assert "!11223344" in err and "!aabbccdd" in err


def test_quota_reset_all_deletes_every_node_and_day_from_temp_database(cfgfile, capsys):
    path, conn = cfgfile
    conn.executemany(
        "INSERT INTO node_daily_quota "
        "(node_id, day, commands, signs, posts, mails_sent, updated_at) "
        "VALUES (?, ?, 1, 2, 3, 4, 0)",
        [('!aabbccdd', '2026-01-02'), ('!11223344', '2026-01-01')],
    )
    conn.commit()

    assert main(["--config", path, "quota", "--reset-all"]) == 0
    assert conn.execute("SELECT count(*) FROM node_daily_quota").fetchone()[0] == 0
    assert "reset all quota rows: 2" in capsys.readouterr().out


def test_quota_reset_default_day_uses_the_configured_timezone_not_system(cfgfile_tz, monkeypatch):
    """Default day must come from fortune.local_day(now, [service]
    timezone) -- never the system tz. Freeze time.time() at a moment
    where America/Los_Angeles and UTC disagree on the calendar day, and
    confirm the row keyed by the LOCAL (LA) day is the one reset."""
    path, conn = cfgfile_tz
    # 2026-09-15 02:00 UTC == 2026-09-14 19:00 America/Los_Angeles (PDT,
    # UTC-7): the two timezones disagree on today's date here.
    fixed_ts = int(datetime(2026, 9, 15, 2, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    monkeypatch.setattr("meshgarden.cli_ops.time.time", lambda: fixed_ts)

    conn.execute(
        "INSERT INTO node_daily_quota (node_id, day, commands, signs, updated_at) "
        "VALUES ('!aabbccdd', '2026-09-14', 5, 2, 0)"
    )
    conn.commit()
    assert main(["--config", path, "quota", "reset", "!aabbccdd"]) == 0
    assert (
        conn.execute(
            "SELECT 1 FROM node_daily_quota WHERE node_id='!aabbccdd' AND day='2026-09-14'"
        ).fetchone()
        is None
    )


def test_backup_creates_a_file(cfgfile, tmp_path):
    path, conn = cfgfile
    assert main(["--config", path, "backup"]) == 0
    assert len(os.listdir(str(tmp_path / "backups"))) == 1


def test_cli_works_while_a_writer_holds_a_transaction(cfgfile):
    """Concurrent CLI access must not fail - busy_timeout + WAL."""
    path, conn = cfgfile
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO blocked_senders(node_id,blocked_at) VALUES ('!1',1)")
    assert main(["--config", path, "db-stats"]) == 0  # readers never block in WAL
    conn.commit()


def test_bad_config_is_usage_error(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text(
        '[radio]\ndevice="/dev/x"\nexpected_node_id="nope"\n'
        '[database]\npath="/tmp/x.db"\n[backup]\ndirectory="/tmp/b"\n'
    )
    assert main(["--config", str(p), "status"]) == 2


def test_no_cli_command_ever_transmits(cfgfile, monkeypatch):
    """No CLI subcommand except `run` may construct a radio interface."""
    path, conn = cfgfile
    import meshtastic.serial_interface as si

    def boom(*a, **k):
        raise AssertionError("CLI must not open the radio")

    monkeypatch.setattr(si, "SerialInterface", boom)
    for argv in (
        ["status"],
        ["db-stats"],
        ["backup"],
        ["block", "!aabbccdd"],
        ["unblock", "!aabbccdd"],
        ["requests"],
        ["guestbook", "list"],
    ):
        main(["--config", path, *argv])


def test_status_file_is_written_atomically_and_scrubbed(cfgfile, tmp_path):
    """The daemon's periodic status-file writer: atomic (temp + os.replace,
    no leftover temp file), owner-only permissions, carries every field the
    CLI needs, and never carries node history, message text, or secrets."""
    from meshgarden.config import load_config
    from meshgarden.main import Daemon
    from tests.fakes import FakeInterface

    path, conn = cfgfile
    cfg = load_config(path)
    iface = FakeInterface(0x11223344)
    daemon = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    daemon.start()
    try:
        daemon.write_status_file()
    finally:
        daemon.stop()

    status_path = os.path.join(str(tmp_path), "status.json")
    assert os.path.exists(status_path)
    assert not os.path.exists(status_path + ".tmp")
    mode = os.stat(status_path).st_mode & 0o777
    assert mode == 0o600

    with open(status_path, encoding="utf-8") as f:
        data = json.load(f)

    for key in (
        "radio_connected",
        "reply_ready",
        "actual_node_id",
        "expected_node_id",
        "mismatch_reason",
        "uptime_seconds",
        "last_packet_at",
        "queue_depth",
        "last_backup_at",
        "last_backup_error",
        "version",
    ):
        assert key in data
    assert data["version"] == __version__

    raw = json.dumps(data).lower()
    assert "guestbook" not in raw
    assert "sign" not in raw


# -- fix round 1 -------------------------------------------------------------


def test_status_missing_database_exits_one_and_creates_nothing(tmp_path, capsys):
    """status must never silently open-and-migrate a fresh, empty database
    when the configured path is wrong -- that would report all-zero counts
    as if the daemon simply hadn't seen anything yet."""
    dbpath = tmp_path / "missing.db"
    cfgpath = tmp_path / "config.toml"
    cfgpath.write_text(CONFIG.format(db=dbpath, bk=tmp_path / "backups"))

    assert main(["--config", str(cfgpath), "status"]) == 1
    err = capsys.readouterr().err
    assert "database not found" in err
    assert str(dbpath) in err
    assert not dbpath.exists()


def test_db_stats_missing_database_exits_one_and_creates_nothing(tmp_path, capsys):
    dbpath = tmp_path / "missing.db"
    cfgpath = tmp_path / "config.toml"
    cfgpath.write_text(CONFIG.format(db=dbpath, bk=tmp_path / "backups"))

    assert main(["--config", str(cfgpath), "db-stats"]) == 1
    err = capsys.readouterr().err
    assert "database not found" in err
    assert str(dbpath) in err
    assert not dbpath.exists()


def test_cli_dispatch_failure_exits_one_and_logs_traceback(cfgfile, monkeypatch, caplog):
    """A runtime failure inside a cmd_* helper must be logged with its
    traceback (for operator diagnosis) even though stdout/stderr stay a
    single bounded line."""
    path, conn = cfgfile

    def boom(cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr("meshgarden.main.cmd_db_stats", boom)

    with caplog.at_level(logging.ERROR, logger="meshgarden.main"):
        rc = main(["--config", path, "db-stats"])

    assert rc == 1
    assert "RuntimeError: boom" in caplog.text


def test_run_failure_exits_one_and_logs_traceback(cfgfile, monkeypatch, caplog):
    path, conn = cfgfile

    class BoomDaemon:
        def __init__(self, cfg, *, debug=False):
            raise RuntimeError("boom")

    monkeypatch.setattr("meshgarden.main.Daemon", BoomDaemon)

    with caplog.at_level(logging.ERROR, logger="meshgarden.main"):
        rc = main(["--config", path, "run"])

    assert rc == 1
    assert "RuntimeError: boom" in caplog.text


def test_status_file_cadence_produces_multiple_writes(cfgfile, tmp_path, monkeypatch):
    """Proves the periodic loop itself -- not just write_status_file() called
    directly -- rewrites status.json repeatedly. Bounded to well under 2s."""
    import meshgarden.main as main_mod
    from meshgarden.config import load_config
    from tests.fakes import FakeInterface

    path, conn = cfgfile
    monkeypatch.setattr(main_mod, "_STATUS_FILE_INTERVAL_SECONDS", 0.05)

    cfg = load_config(path)
    iface = FakeInterface(0x11223344)
    daemon = main_mod.Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    daemon.start()
    try:
        status_path = os.path.join(str(tmp_path), "status.json")
        seen_generations = set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(seen_generations) < 2:
            if os.path.exists(status_path):
                try:
                    with open(status_path, encoding="utf-8") as f:
                        data = json.load(f)
                    seen_generations.add(data["generated_at"])
                except (OSError, ValueError, KeyError):
                    pass
            time.sleep(0.02)
        assert len(seen_generations) >= 2
    finally:
        daemon.stop()


# -- `requests` (local-only, read-only) --------------------------------------


def test_requests_lists_newest_first_with_default_limit(cfgfile, capsys):
    path, conn = cfgfile
    log = RequestLog(conn)
    log.claim("!aabbccdd", 1, 100, Command.MENU)
    log.set_state("!aabbccdd", 1, "acked", 101)
    log.claim("!11112222", 2, 200, Command.STATS)
    log.set_state("!11112222", 2, "queued", 200)

    assert main(["--config", path, "requests", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 2
    # Newest (highest first_seen_at) first.
    assert data[0]["sender_node_id"] == "!11112222"
    assert data[0]["command"] == "STATS"
    assert data[0]["state"] == "queued"
    assert data[1]["sender_node_id"] == "!aabbccdd"
    assert data[1]["command"] == "MENU"
    assert data[1]["state"] == "acked"


def test_requests_never_includes_message_text(cfgfile, capsys):
    """processed_requests carries no message column at all -- this just
    proves the reported fields are exactly the bounded set."""
    path, conn = cfgfile
    RequestLog(conn).claim("!aabbccdd", 1, 100, Command.SIGN)

    assert main(["--config", path, "requests", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data[0]) == {
        "time",
        "sender_node_id",
        "command",
        "state",
        "reason",
        "tx_packet_id",
    }


def test_requests_respects_limit(cfgfile, capsys):
    path, conn = cfgfile
    log = RequestLog(conn)
    for i in range(5):
        log.claim("!aabbccdd", i + 1, 100 + i, Command.MENU)

    assert main(["--config", path, "requests", "--limit", "2", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 2
    assert data[0]["time"] is not None  # newest first, sanity


def test_requests_default_limit_is_twenty(cfgfile, capsys):
    path, conn = cfgfile
    log = RequestLog(conn)
    for i in range(25):
        log.claim("!aabbccdd", i + 1, 100 + i, Command.MENU)

    assert main(["--config", path, "requests", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 20


def test_requests_reports_local_time_in_configured_timezone(cfgfile_tz, capsys):
    path, conn = cfgfile_tz
    now = 1700000000
    RequestLog(conn).claim("!aabbccdd", 1, now, Command.MENU)

    assert main(["--config", path, "requests", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    expected = datetime.fromtimestamp(now, tz=ZoneInfo("America/Los_Angeles")).isoformat()
    assert data[0]["time"] == expected


def test_requests_human_output_is_one_row_per_line(cfgfile, capsys):
    path, conn = cfgfile
    with conn:
        conn.execute(
            "INSERT INTO nodes(node_id, short_name, observed_first_seen, observed_last_seen) "
            "VALUES ('!aabbccdd', 'OakGrove', 1, 1)"
        )
    log = RequestLog(conn)
    log.claim("!aabbccdd", 1, 100, Command.MENU)
    log.set_state("!aabbccdd", 1, "dropped", 101, reason="queue_full")

    assert main(["--config", path, "requests"]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1
    line = lines[0]
    assert "OakGrove (!aabbccdd)" in line
    assert "MENU" in line
    assert "dropped" in line
    assert "queue_full" in line


def test_requests_limit_must_be_between_one_and_a_thousand(cfgfile):
    path, conn = cfgfile
    assert main(["--config", path, "requests", "--limit", "0"]) == 2
    assert main(["--config", path, "requests", "--limit", "1001"]) == 2


def test_requests_missing_database_exits_one(tmp_path, capsys):
    dbpath = tmp_path / "missing.db"
    cfgpath = tmp_path / "config.toml"
    cfgpath.write_text(CONFIG.format(db=dbpath, bk=tmp_path / "backups"))

    assert main(["--config", str(cfgpath), "requests"]) == 1
    err = capsys.readouterr().err
    assert "database not found" in err
    assert not dbpath.exists()


def test_requests_works_while_a_writer_holds_a_transaction(cfgfile):
    path, conn = cfgfile
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO blocked_senders(node_id,blocked_at) VALUES ('!1',1)")
    assert main(["--config", path, "requests"]) == 0
    conn.commit()


# -- `guestbook list` (local-only, read-only) --------------------------------


def test_guestbook_list_shows_visible_only_by_default(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        visible_id = g.insert("!aabbccdd", "Alice", "hello there", 100)
        hidden_id = g.insert("!11112222", "Bob", "secret note", 200)
    g.hide(hidden_id)

    assert main(["--config", path, "guestbook", "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    ids = [e["entry_id"] for e in data]
    assert visible_id in ids
    assert hidden_id not in ids


def test_guestbook_list_hidden_includes_hidden_rows_marked(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        visible_id = g.insert("!aabbccdd", "Alice", "hello there", 100)
        hidden_id = g.insert("!11112222", "Bob", "secret note", 200)
    g.hide(hidden_id)

    assert main(["--config", path, "guestbook", "list", "--hidden", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    by_id = {e["entry_id"]: e for e in data}
    assert by_id[visible_id]["visibility"] == "visible"
    assert by_id[hidden_id]["visibility"] == "hidden"


def test_guestbook_list_newest_first_and_limit(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    ids = []
    with conn:
        for i in range(3):
            ids.append(g.insert("!aabbccdd", "Alice", f"msg {i}", 100 + i))

    assert main(["--config", path, "guestbook", "list", "--limit", "2", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [e["entry_id"] for e in data] == [ids[2], ids[1]]


def test_guestbook_list_falls_back_to_last_four_hex_chars(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        g.insert("!aabbccdd", None, "hello there", 100)

    assert main(["--config", path, "guestbook", "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["display_name"] == "ccdd"


def test_guestbook_list_reports_local_time_and_never_a_display_name_column_extra(
    cfgfile_tz, capsys
):
    path, conn = cfgfile_tz
    g = Guestbook(conn, ServiceConfig())
    now = 1700000000
    with conn:
        g.insert("!aabbccdd", "Alice", "hello there", now)

    assert main(["--config", path, "guestbook", "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    expected = datetime.fromtimestamp(now, tz=ZoneInfo("America/Los_Angeles")).isoformat()
    assert data[0]["time"] == expected
    assert set(data[0]) == {
        "entry_id",
        "time",
        "sender_node_id",
        "display_name",
        "visibility",
        "message",
    }


def test_guestbook_list_default_limit_is_ten(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        for i in range(15):
            g.insert("!aabbccdd", "Alice", f"msg {i}", 100 + i)

    assert main(["--config", path, "guestbook", "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 10


def test_guestbook_list_human_output_is_one_row_per_line(cfgfile, capsys):
    path, conn = cfgfile
    g = Guestbook(conn, ServiceConfig())
    with conn:
        g.insert("!aabbccdd", "Alice", "hello there", 100)

    assert main(["--config", path, "guestbook", "list"]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "Alice" in lines[0]
    assert "hello there" in lines[0]


def test_guestbook_list_limit_must_be_between_one_and_a_thousand(cfgfile):
    path, conn = cfgfile
    assert main(["--config", path, "guestbook", "list", "--limit", "0"]) == 2
    assert main(["--config", path, "guestbook", "list", "--limit", "1001"]) == 2


def test_guestbook_list_missing_database_exits_one(tmp_path, capsys):
    dbpath = tmp_path / "missing.db"
    cfgpath = tmp_path / "config.toml"
    cfgpath.write_text(CONFIG.format(db=dbpath, bk=tmp_path / "backups"))

    assert main(["--config", str(cfgpath), "guestbook", "list"]) == 1
    err = capsys.readouterr().err
    assert "database not found" in err
    assert not dbpath.exists()


def test_guestbook_list_works_while_a_writer_holds_a_transaction(cfgfile):
    path, conn = cfgfile
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO blocked_senders(node_id,blocked_at) VALUES ('!1',1)")
    assert main(["--config", path, "guestbook", "list"]) == 0
    conn.commit()


# -- `run --debug` (operator opt-in exchange trace) --------------------------


def test_run_parser_accepts_and_defaults_the_debug_flag():
    from meshgarden.main import build_parser

    parser = build_parser()
    assert parser.parse_args(["run"]).debug is False
    assert parser.parse_args(["run", "--debug"]).debug is True


def test_mail_parser_accepts_reveal_json_and_purge_subcommand():
    from meshgarden.main import build_parser

    parser = build_parser()
    args = parser.parse_args(["mail"])
    assert args.command == "mail"
    assert args.reveal is False
    assert args.json is False
    assert args.mail_command is None

    args = parser.parse_args(["mail", "--reveal", "--json"])
    assert args.reveal is True
    assert args.json is True

    args = parser.parse_args(["mail", "purge", "!22222222"])
    assert args.mail_command == "purge"
    assert args.node_id == "!22222222"

    args = parser.parse_args(["mail", "alias", "list", "--json"])
    assert args.mail_command == "alias"
    assert args.mail_alias_command == "list"
    assert args.json is True

    args = parser.parse_args(
        ["mail", "alias", "set", "!22222222", "gardenfox", "--replace"]
    )
    assert args.mail_alias_command == "set"
    assert args.node_id == "!22222222"
    assert args.alias == "gardenfox"
    assert args.replace is True

    args = parser.parse_args(["mail", "alias", "release", "gardenfox"])
    assert args.mail_alias_command == "release"
    assert args.alias == "gardenfox"


def test_mail_purge_routes_to_cmd_mail_purge_not_cmd_mail(cfgfile, monkeypatch):
    """`mail purge <node-id>` shares `args.command == "mail"` with plain
    `mail`; this pins that dispatch checks the purge subcommand first."""
    path, conn = cfgfile
    from meshgarden import cli_ops

    called = {}

    def fake_purge(cfg, node_id):
        called["purge"] = node_id
        return 0

    def fake_mail(cfg, **kwargs):
        called["mail"] = True
        return 0

    monkeypatch.setattr(cli_ops, "cmd_mail_purge", fake_purge)
    monkeypatch.setattr(cli_ops, "cmd_mail", fake_mail)

    rc = main(["--config", path, "mail", "purge", "!22222222"])
    assert rc == 0
    assert called == {"purge": "!22222222"}


def test_cmd_run_passes_the_debug_flag_through_to_the_daemon(cfgfile, monkeypatch):
    path, conn = cfgfile
    captured = {}

    class FakeDaemon:
        def __init__(self, cfg, *, debug=False):
            captured["debug"] = debug

        def start(self):
            pass

        def run_forever(self):
            pass

    monkeypatch.setattr("meshgarden.main.Daemon", FakeDaemon)
    assert main(["--config", path, "run", "--debug"]) == 0
    assert captured["debug"] is True

    assert main(["--config", path, "run"]) == 0
    assert captured["debug"] is False


def test_notify_test_reports_missing_configuration_clearly(cfgfile, capsys, monkeypatch):
    """An operator who has not set the ntfy URL must get told exactly that,
    not a generic failure."""
    from meshgarden.main import main

    path, _conn = cfgfile
    monkeypatch.delenv("MESHGARDEN_NTFY_URL", raising=False)
    monkeypatch.setenv("MESHGARDEN_WATCHDOG_ENV_FILE", "/nonexistent/watchdog.env")
    rc = main(["--config", path, "notify-test"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "MESHGARDEN_NTFY_URL" in err


def test_notify_test_sends_one_notification_and_reports_success(
    cfgfile, capsys, monkeypatch
):
    from meshgarden import watchdog
    from meshgarden.main import main

    path, _conn = cfgfile
    sent = []
    monkeypatch.setenv("MESHGARDEN_NTFY_URL", "https://ntfy.sh/topic")
    monkeypatch.setattr(
        watchdog, "_http_post", lambda url, data, headers, timeout: sent.append((url, headers))
    )
    rc = main(["--config", path, "notify-test"])
    out = capsys.readouterr().out
    assert rc == 0
    assert len(sent) == 1
    assert sent[0][0] == "https://ntfy.sh/topic"
    assert "test" in sent[0][1]["Title"].lower()
    assert "sent" in out.lower()


def test_notify_test_never_prints_the_url_it_used(cfgfile, capsys, monkeypatch):
    """The topic is a shared secret; printing it would leak it into shell
    history and terminal scrollback."""
    from meshgarden import watchdog
    from meshgarden.main import main

    path, _conn = cfgfile
    monkeypatch.setenv("MESHGARDEN_NTFY_URL", "https://ntfy.sh/supersecrettopic")
    monkeypatch.setattr(watchdog, "_http_post", lambda url, data, headers, timeout: None)
    main(["--config", path, "notify-test"])
    captured = capsys.readouterr()
    assert "supersecrettopic" not in captured.out + captured.err


def test_notify_test_reports_a_delivery_failure_with_a_nonzero_exit(
    cfgfile, capsys, monkeypatch
):
    from meshgarden import watchdog
    from meshgarden.main import main

    def boom(url, data, headers, timeout):
        raise OSError("network unreachable")

    path, _conn = cfgfile
    monkeypatch.setenv("MESHGARDEN_NTFY_URL", "https://ntfy.sh/topic")
    monkeypatch.setattr(watchdog, "_http_post", boom)
    rc = main(["--config", path, "notify-test"])
    assert rc == 1
    assert "failed" in capsys.readouterr().err.lower()


def test_radio_watchdog_polls_and_prints_the_status(cfgfile, capsys, monkeypatch, tmp_path):
    """The timer path: one poll, a status line, exit 0 even when the radio
    is down (the alert is the ntfy push, not the exit code -- a failing
    unit every minute would be its own kind of noise)."""
    from meshgarden import watchdog
    from meshgarden.main import main

    path, _conn = cfgfile
    monkeypatch.setenv("MESHGARDEN_NTFY_URL", "https://ntfy.sh/topic")
    monkeypatch.setenv("MESHGARDEN_WATCHDOG_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(watchdog, "poll_status", lambda url, **kw: "radio_down")
    monkeypatch.setattr(watchdog, "_http_post", lambda url, data, headers, timeout: None)
    rc = main(["--config", path, "radio-watchdog"])
    assert rc == 0
    assert "radio_down" in capsys.readouterr().out


def test_radio_watchdog_defaults_its_state_file_next_to_the_database(
    cfgfile, capsys, monkeypatch
):
    """The production path: no MESHGARDEN_WATCHDOG_STATE set, so the state
    file must land in the database dir on its own."""
    from meshgarden import watchdog
    from meshgarden.main import main

    path, _conn = cfgfile
    monkeypatch.delenv("MESHGARDEN_WATCHDOG_STATE", raising=False)
    monkeypatch.delenv("MESHGARDEN_NTFY_URL", raising=False)
    monkeypatch.setenv("MESHGARDEN_WATCHDOG_ENV_FILE", "/nonexistent/watchdog.env")
    monkeypatch.setattr(watchdog, "poll_status", lambda url, **kw: "radio_down")
    assert main(["--config", path, "radio-watchdog"]) == 0
    assert "radio_down" in capsys.readouterr().out
    state = pathlib.Path(path).parent / "watchdog-state.json"
    assert state.exists()
    assert (state.stat().st_mode & 0o777) == 0o600


def _second_instance(tmp_path, name="mt.toml", transport="meshcore"):
    """A sibling instance config, like /etc/meshgarden/mt.toml beside config.toml."""
    other = tmp_path / "second"
    p = tmp_path / name
    text = CONFIG.format(db=other / "o.db", bk=other / "backups")
    if transport == "meshcore":
        text = text.replace("[radio]\n", '[radio]\ntransport = "meshcore"\n', 1)
    p.write_text(text)
    conn = connect(str(other / "o.db"))
    migrate(conn)
    conn.close()
    return str(p)


def test_status_reports_every_instance_by_default(cfgfile, tmp_path, capsys):
    path, conn = cfgfile
    mt = _second_instance(tmp_path)
    (tmp_path / "broken.toml").write_text("not = [valid")
    assert main(["--config", path, "status"]) == 0
    out = capsys.readouterr().out
    assert f"== Meshtastic ({path})" in out and f"== MeshCore ({mt})" in out
    assert out.count("expected_node_id") == 2


def test_status_transport_flag_shows_one_instance(cfgfile, tmp_path, capsys):
    path, conn = cfgfile
    mt = _second_instance(tmp_path)
    assert main(["--config", path, "status", "--meshcore"]) == 0
    out = capsys.readouterr().out
    assert "==" not in out and out.count("expected_node_id") == 1
    assert main(["--config", path, "status", "--meshcore", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["version"]
    assert main(["--config", mt, "status", "--meshtastic", "--json"]) == 0
    assert "expected_node_id" in json.loads(capsys.readouterr().out)


def test_status_json_stays_one_object_and_missing_transport_errors(cfgfile, tmp_path, capsys):
    path, conn = cfgfile
    _second_instance(tmp_path)
    assert main(["--config", path, "status", "--json"]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), dict)
    (tmp_path / "mt.toml").unlink()
    assert main(["--config", path, "status", "--meshcore"]) == 2
