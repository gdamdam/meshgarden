import json
import logging
import os
import random
import shutil
import socket
import stat
import tempfile
import threading
import time
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from prometheus_client import generate_latest

from meshgarden import booktrivia, commands, digest, fortune, main
from meshgarden.collector import Collector
from meshgarden.commands import (
    info_text,
    menu_daily_text,
    menu_games_text,
    menu_stats_text,
    menu_text,
    not_emergency_text,
    version_text,
    with_quota_hint,
)
from meshgarden.config import (
    BackupConfig,
    Config,
    DatabaseConfig,
    MetricsConfig,
    ModerationConfig,
    RadioConfig,
    RetentionConfig,
    ServiceConfig,
    WebConfig,
)
from meshgarden.database import connect, migrate
from meshgarden.guestbook import (
    Guestbook,
    format_sign_cooldown,
    format_sign_duplicate,
    format_sign_too_long,
)
from meshgarden.lab import Lab
from meshgarden.mail import Mail
from meshgarden.main import Daemon
from meshgarden.metrics import Metrics
from meshgarden.quota import DailyQuota
from meshgarden.radio import RadioAdapter, RadioStatus
from meshgarden.retention import Retention
from meshgarden.sender import ReplyQueue
from tests.conftest import HAIKU_RNG_SEED, LAB_UNLOCK_WORD
from tests.fakes import FakeInterface, make_packet


def _cfg(tmp_path):
    return Config(
        radio=RadioConfig(device="/dev/fake", expected_node_id="!11223344"),
        service=ServiceConfig(lab_unlock_word=LAB_UNLOCK_WORD),
        database=DatabaseConfig(path=str(tmp_path / "o.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "backups")),
        metrics=MetricsConfig(),
    )


def _bare_daemon_for_maintenance(tmp_path):
    """A Daemon stand-in for testing `_run_maintenance` in isolation, on the
    single (test) thread: real Retention/Collector/Guestbook/ReplyQueue on
    one connection opened by this thread (so no cross-thread sqlite3 use --
    the real Daemon's _retention lives on the DB worker thread's own
    connection, which only that thread may touch), and a RadioAdapter that
    is never started (so .status() just returns its safe default, no
    thread/pubsub involved)."""
    cfg = _cfg(tmp_path)
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    d = Daemon.__new__(Daemon)
    d.cfg = cfg
    d.started_at = int(time.time())
    d._metrics = Metrics()
    d._collector = Collector(conn)
    d._guestbook = Guestbook(conn, cfg.service)
    d._mail = Mail(conn)
    d._quota = DailyQuota(
        conn, command_limit=cfg.service.daily_command_quota, sign_limit=cfg.service.daily_sign_quota
    )
    d._queue = ReplyQueue(cfg.service)
    d._retention = Retention(
        conn, cfg.retention, cfg.backup, log_dir=str(tmp_path / "logs")
    )
    d._radio = RadioAdapter(cfg.radio, sink=lambda p: None)
    d._meshcore_client = None  # task 3B-2: _run_maintenance checks this first
    d._meshcore_channel_policy = None  # task 3B-3: _run_maintenance's channel_knocks prune guard
    d._knock_cooldown = commands.ChannelKnockCooldown(conn)
    d._drop_events = deque()
    d._last_rollup_day = None
    d._last_digest_local_day = None
    d._mail_delivery_pending = {}
    d._db_conn = conn  # WORDLE's prune_wordle_state runs on this connection
    d._db_request_log = commands.RequestLog(conn, dedup_hours=cfg.service.request_dedup_hours)
    d._clock = main._Clock()
    d._heartbeats = main.Heartbeats(
        {"db": 30.0, "tx": 30.0, "radio": 300.0, "maintenance": 180.0}, clock=d._clock.monotonic
    )
    d._worker_stall_state = {}
    d._midnight_tasks = []
    return d, conn


def b(s):
    return len(s.encode("utf-8"))


def test_menu_fits_the_byte_budget_and_lists_top_level_commands():
    t = menu_text()
    assert b(t) <= 180
    for cmd in (
        "SIGN",
        "GUESTBOOK",
        "NEW",
        "NEWS",
        "GUIDE",
        "MENU SOCIAL",
        "MENU STATS",
        "MENU GAMES",
        "MENU DAILY",
    ):
        assert cmd in t


def test_menu_stats_fits_the_byte_budget_and_lists_its_commands():
    t = menu_stats_text()
    assert b(t) <= 180
    for cmd in ("STATS", "GARDEN", "WEEK", "ME", "CHECK", "PATH", "SEEN"):
        assert cmd in t
    for hidden_specialist in ("PING", "WHO", "RECORD"):
        assert hidden_specialist not in t


def test_menu_games_fits_the_byte_budget_and_lists_its_commands():
    t = menu_games_text()
    assert b(t) <= 180
    for cmd in ("BOOK", "WORDLE"):
        assert cmd in t
    assert "ICHING" not in t
    assert "HAIKU" not in t


def test_menu_daily_fits_the_byte_budget_and_lists_its_commands():
    t = menu_daily_text()
    assert b(t) <= 180
    for cmd in ("ICHING", "HAIKU"):
        assert cmd in t


def test_not_emergency_fits_the_byte_budget_and_has_no_service_name():
    t = not_emergency_text()
    assert b(t) <= 180
    assert "emergenc" in t.lower()
    assert "menu" in t.lower()


def test_info_fits_the_byte_budget_and_carries_the_disclaimers():
    t = info_text("Meshgarden Guestbook")
    assert b(t) <= 180
    assert "Meshgarden Guestbook" in t
    assert "emergenc" in t.lower()
    assert "uptime" in t.lower()
    assert "publish" in t.lower()
    assert "menu" in t.lower()


def test_info_names_the_other_mesh_only_when_shared():
    assert "Shared with" not in info_text("Garden")
    t = info_text("Garden", "MeshCore")
    assert "🔗 Shared with MeshCore\n" in t
    assert t.endswith(commands._INFO_SUFFIX.rpartition("\n")[2])
    assert b(t) <= 180
    big = info_text("X" * 400, "Meshtastic")
    assert b(big) == 180 and "uptime" in big


def test_info_truncates_only_the_name_when_the_name_is_huge():
    t = info_text("X" * 400)
    assert b(t) <= 180
    assert "emergenc" in t.lower() and "uptime" in t.lower()


def test_info_name_budget_is_recomputed_from_the_fixed_prefix_and_suffix():
    """The name budget must always net out the leading `🌱 ` prefix and the
    fixed suffix (everything after the name: the closing tulip, the tiny
    mesh guestbook / SIGN line, the MENU-for-commands line, and the
    experimental/no-emergency/no-uptime disclaimer) so a truncated name can
    never crowd out the fixed lines."""
    prefix_bytes = b(commands._GARDEN_EMOJI_PREFIX)
    suffix_bytes = b(commands._INFO_SUFFIX)
    assert prefix_bytes == 5
    max_name_bytes = 180 - prefix_bytes - suffix_bytes

    t = info_text("X" * 400)
    assert b(t) == 180
    assert t.endswith(commands._INFO_SUFFIX)
    assert max_name_bytes > 0


def test_info_with_a_200_char_name_still_fits_the_budget():
    name = "N" * 200
    t = info_text(name)
    assert b(t) <= 180
    assert "emergenc" in t.lower() and "uptime" in t.lower() and "MENU" in t


def test_info_with_an_emoji_bearing_name_fits_the_budget_and_keeps_the_full_name():
    name = "MeshGarden BOT 🌁 Potrero Hill SF"
    t = info_text(name)
    assert name in t
    assert b(t) <= 180


def test_menu_dm_produces_exactly_one_reply(daemon):
    # The `daemon` fixture pre-unlocks the default make_packet sender
    # ("!aabbccdd") for the LAB tier (see conftest.py's
    # _unlock_standard_lab_senders), so its own MENU reply is the
    # lab_unlocked variant -- see test_locked_node_menu_has_no_lab_line
    # below for the locked case.
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="MENU"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    # First command from this sender today: quota hint reads 1/60.
    assert daemon.iface.sent[0]["text"] == with_quota_hint(
        menu_text(lab_unlocked=True), 1, 60, max_bytes=180
    )


def test_question_mark_alias_produces_the_same_menu_reply(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="?"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"] == with_quota_hint(
        menu_text(lab_unlocked=True), 1, 60, max_bytes=180
    )


def test_menu_stats_dm_produces_the_stats_sub_page(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="MENU STATS"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"] == menu_stats_text()


def test_menu_games_dm_produces_the_games_sub_page(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="MENU GAMES"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"] == menu_games_text()


def test_question_mark_games_alias_produces_the_games_sub_page(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="? GAMES"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"] == menu_games_text()


def test_menu_daily_dm_produces_the_daily_sub_page(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="MENU DAILY"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"] == menu_daily_text()


def test_question_mark_daily_alias_produces_the_daily_sub_page(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="? daily"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"] == menu_daily_text()


def test_menu_lowercase_sub_page_argument_works(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="menu stats"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"] == menu_stats_text()


def test_menu_with_junk_argument_falls_back_to_the_top_level_menu(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="MENU xyz"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"] == with_quota_hint(
        menu_text(lab_unlocked=True), 1, 60, max_bytes=180
    )
    assert "xyz" not in daemon.iface.sent[0]["text"]


def test_help_with_a_command_argument_gives_its_one_line_usage(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="HELP sign", packet_id=9001)
    )
    daemon.drain()
    body = daemon.iface.sent[0]["text"]
    assert "SIGN <msg>" in body
    assert len(body.encode("utf-8")) <= 180


def test_question_mark_with_a_command_argument_gives_its_usage(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="? sign", packet_id=9002)
    )
    daemon.drain()
    assert "SIGN <msg>" in daemon.iface.sent[0]["text"]


def test_menu_boards_dm_produces_the_boards_sub_page(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MENU BOARDS", packet_id=9003)
    )
    daemon.drain()
    body = daemon.iface.sent[0]["text"]
    assert body.startswith("📌 Boards\n")
    assert "POST <topic> <msg>" in body


def test_menu_quota_hint_survives_a_narrow_configured_max_response_bytes(tmp_path):
    """Regression: `_build_response_body`'s call to `with_quota_hint` must
    budget against the DAEMON's configured `service.max_response_bytes`
    (140 here), not commands.py's own internal 180 default -- otherwise the
    later generic `truncate_utf8(..., max_response_bytes)` re-truncates the
    already-budgeted reply and can cut the quota suffix itself."""
    cfg = replace(
        _cfg(tmp_path),
        service=ServiceConfig(max_response_bytes=140, daily_command_quota=999),
    )
    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    try:
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU"))
        d.drain()
        text = d.iface.sent[0]["text"]
        assert b(text) <= 140
        assert text.endswith(" · quota 1/999 today")
    finally:
        d.stop()


@pytest.mark.parametrize("word", ["HELP", "SOS", "EMERGENCY"])
def test_distress_words_produce_the_fixed_not_emergency_reply(daemon, word):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text=word))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"] == not_emergency_text()


def test_lowercase_info_works(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="info"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1


def test_version_dm_replies_with_the_hidden_version_string(daemon):
    """Mirrors test_lowercase_info_works: VERSION is a hidden bare command
    (never listed in MENU/HELP -- see commands._BARE_COMMANDS) but still
    reaches a visitor the same way INFO does, through the normal dispatch/
    quota/rate-limit path."""
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="VERSION", packet_id=9910)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]
    assert reply == version_text(main.__version__)
    assert b(reply) <= 180


def test_version_with_an_argument_is_unknown_and_gets_no_reply(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="VERSION please", packet_id=9911)
    )
    daemon.drain()
    assert daemon.iface.sent == []


def test_iching_dm_produces_one_reply_matching_the_deterministic_hexagram(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="ICHING", packet_id=9101))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]
    assert b(reply) <= 180

    row = daemon.conn.execute(
        "SELECT first_seen_at FROM processed_requests WHERE packet_id=9101"
    ).fetchone()
    assert row is not None
    day = fortune.local_day(row["first_seen_at"], daemon.cfg.service.timezone)
    expected = fortune.iching_text("!aabbccdd", day, daemon.cfg.service.fortune_salt)
    assert reply == expected


def test_lowercase_iching_works(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="iching"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1


def test_iching_gives_the_same_sender_the_same_hexagram_later_the_same_day(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="ICHING", packet_id=1))
    daemon.drain()
    first = daemon.iface.sent[0]["text"]

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="ICHING", packet_id=2))
    daemon.drain()
    second = daemon.iface.sent[1]["text"]

    assert first == second  # same sender, same local day -> same hexagram


def test_haiku_dm_produces_one_reply_matching_the_fixed_rng_choice(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="HAIKU", packet_id=9201))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]
    assert b(reply) <= 180

    expected_template = random.Random(HAIKU_RNG_SEED).choice(fortune.HAIKUS)
    word = daemon.cfg.service.haiku_place or expected_template.default_place
    # senders=1: the HAIKU DM's own sender is itself an observation within
    # the trailing 24h window computed at the moment of the reply.
    expected = (
        "\n".join(
            line.format(senders=1, guestbook=0, place=word) for line in expected_template.lines
        )
        + " \U0001f331"
    )
    assert reply == expected


def test_lowercase_haiku_works(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="haiku"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1


def test_book_dm_produces_todays_puzzle_prompt(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="BOOK", packet_id=9301))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]
    assert b(reply) <= 180

    row = daemon.conn.execute(
        "SELECT first_seen_at FROM processed_requests WHERE packet_id=9301"
    ).fetchone()
    assert row is not None
    day = fortune.local_day(row["first_seen_at"], daemon.cfg.service.timezone)
    puzzle = booktrivia.puzzle_for(day, daemon.cfg.service.fortune_salt)
    assert reply == booktrivia.book_prompt_text(puzzle)


def test_lowercase_book_works(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="book"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1


def test_book_correct_guess_produces_the_correct_reply(daemon):
    day = fortune.local_day(int(time.time()), daemon.cfg.service.timezone)
    puzzle = booktrivia.puzzle_for(day, daemon.cfg.service.fortune_salt)

    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=f"BOOK {puzzle.title}", packet_id=9302)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]
    # First-ever solve of this book (fresh db): the "first to crack it" tier.
    assert reply == booktrivia.book_first_solve_text(puzzle)
    assert b(reply) <= 180


def test_book_second_solve_gets_the_nth_today_tier(daemon):
    """Two different senders solve the same book on the same day: the
    first gets the "first to crack it" tier (see above), the second (not
    rare -- only 2 all-time guesses, both solves) gets "you're #2 today"."""
    day = fortune.local_day(int(time.time()), daemon.cfg.service.timezone)
    puzzle = booktrivia.puzzle_for(day, daemon.cfg.service.fortune_salt)

    daemon.iface.emit_packet(
        make_packet(
            from_num=0x11111111,
            to_num=daemon.my_node_num,
            text=f"BOOK {puzzle.title}",
            packet_id=9310,
        )
    )
    daemon.drain()
    daemon.iface.emit_packet(
        make_packet(
            from_num=0x22222222,
            to_num=daemon.my_node_num,
            text=f"BOOK {puzzle.title}",
            packet_id=9311,
        )
    )
    daemon.drain()

    assert len(daemon.iface.sent) == 2
    assert daemon.iface.sent[0]["text"] == booktrivia.book_first_solve_text(puzzle)
    assert daemon.iface.sent[1]["text"] == booktrivia.book_nth_solve_text(puzzle, 2)

    key = booktrivia.book_key(puzzle)
    row = daemon.conn.execute(
        "SELECT guesses, solves FROM book_stats WHERE book_key=? AND day=?", (key, day)
    ).fetchone()
    assert (row["guesses"], row["solves"]) == (2, 2)


def test_book_repeat_correct_guess_from_same_sender_gets_already_solved_reply(daemon):
    """Operator-reported bug: `BOOK <correct title>` was accepted every time
    it was sent, and `book_stats.solves` went up on every repeat, because
    the day's puzzle carries no per-sender state. Fixed via `book_state`
    (schema v7, mirroring `wordle_state`): a second correct guess from the
    SAME sender, after they've already solved today, gets the fixed
    already-solved reply instead, and records nothing further in
    `book_stats` (no extra guess, no extra solve). A different sender's
    correct guess still counts."""
    day = fortune.local_day(int(time.time()), daemon.cfg.service.timezone)
    puzzle = booktrivia.puzzle_for(day, daemon.cfg.service.fortune_salt)
    key = booktrivia.book_key(puzzle)

    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=f"BOOK {puzzle.title}", packet_id=9320)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"] == booktrivia.book_first_solve_text(puzzle)

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=f"BOOK {puzzle.title}", packet_id=9321)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 2
    assert daemon.iface.sent[1]["text"] == booktrivia.book_already_solved_text()

    row = daemon.conn.execute(
        "SELECT guesses, solves FROM book_stats WHERE book_key=? AND day=?", (key, day)
    ).fetchone()
    assert (row["guesses"], row["solves"]) == (1, 1)

    daemon.iface.emit_packet(
        make_packet(
            from_num=0x33333333,
            to_num=daemon.my_node_num,
            text=f"BOOK {puzzle.title}",
            packet_id=9322,
        )
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 3
    assert daemon.iface.sent[2]["text"] == booktrivia.book_nth_solve_text(puzzle, 2)

    row = daemon.conn.execute(
        "SELECT guesses, solves FROM book_stats WHERE book_key=? AND day=?", (key, day)
    ).fetchone()
    assert (row["guesses"], row["solves"]) == (2, 2)


def test_book_rare_catch_tier_after_many_wrong_guesses(daemon):
    """A book with a low all-time solve rate (many wrong guesses, then one
    solve) gets the "rare catch" tier instead of "first to crack it" (a
    prior solve already exists) or the plain Nth-today reply."""
    day = fortune.local_day(int(time.time()), daemon.cfg.service.timezone)
    puzzle = booktrivia.puzzle_for(day, daemon.cfg.service.fortune_salt)
    key = booktrivia.book_key(puzzle)

    # Seed all-time history directly: one prior solve (so this is not the
    # "first ever"), plus enough prior guesses that the post-guess rate
    # falls under the rare threshold.
    with daemon.conn:
        daemon.conn.execute(
            "INSERT INTO book_stats (book_key, day, shown, guesses, solves)"
            " VALUES (?, '2020-01-01', 0, 29, 1)",
            (key,),
        )

    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=f"BOOK {puzzle.title}", packet_id=9312)
    )
    daemon.drain()
    reply = daemon.iface.sent[0]["text"]
    assert reply == booktrivia.book_rare_solve_text(puzzle)
    assert b(reply) <= 180


def test_book_prompt_shown_increments_book_stats(daemon):
    day = fortune.local_day(int(time.time()), daemon.cfg.service.timezone)
    puzzle = booktrivia.puzzle_for(day, daemon.cfg.service.fortune_salt)
    key = booktrivia.book_key(puzzle)

    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="BOOK", packet_id=9313))
    daemon.drain()

    row = daemon.conn.execute(
        "SELECT shown, guesses, solves FROM book_stats WHERE book_key=? AND day=?", (key, day)
    ).fetchone()
    assert (row["shown"], row["guesses"], row["solves"]) == (1, 0, 0)


def test_book_wrong_guess_produces_the_wrong_reply(daemon):
    day = fortune.local_day(int(time.time()), daemon.cfg.service.timezone)
    puzzle = booktrivia.puzzle_for(day, daemon.cfg.service.fortune_salt)

    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, text="BOOK this is certainly not it", packet_id=9303
        )
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]
    assert reply == booktrivia.book_wrong_text(puzzle)
    assert b(reply) <= 180


def test_book_guess_text_is_never_written_to_the_database_or_logged(daemon, caplog):
    caplog.set_level("DEBUG")
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num,
            text="BOOK a very specific unmistakable guess phrase",
            packet_id=9304,
        )
    )
    daemon.drain()
    dump = "\n".join(daemon.conn.iterdump())
    assert "unmistakable guess phrase" not in dump
    assert "unmistakable guess phrase" not in caplog.text

    row = daemon.conn.execute(
        "SELECT command FROM processed_requests WHERE packet_id=9304"
    ).fetchone()
    assert row["command"] == "BOOK"


def test_book_with_whitespace_only_argument_returns_the_puzzle(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="BOOK   ", packet_id=9305)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]
    assert reply.startswith("📚 Guess:")


def test_book_very_long_guess_is_handled_without_storage(daemon, caplog):
    caplog.set_level("DEBUG")

    # Create a long guess: ~220 UTF-8 bytes of padding (make sure total ≤ 233 bytes)
    # Use a pattern that is definitely not a correct answer
    long_guess = "zzz test guess " * 15  # Creates ~240 chars, trim to fit
    long_guess = long_guess[:210]  # Ensure ~210 bytes for the guess part
    text = f"BOOK {long_guess}"

    # Verify total payload is within budget (≤ 233 bytes)
    assert b(text) <= 233

    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=text, packet_id=9306)
    )
    daemon.drain()

    # Exactly one reply
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]

    # Reply starts with the BOOK prefix + ❌ and is ≤ 180 bytes
    assert reply.startswith("📚 ❌")
    assert b(reply) <= 180

    # Verify daemon is still healthy by checking reply_ready
    status = daemon.status_snapshot()
    assert status["reply_ready"] is True

    # Verify guess text doesn't appear in database dump
    dump = "\n".join(daemon.conn.iterdump())
    assert long_guess not in dump

    # Verify guess text doesn't appear in logs
    assert long_guess not in caplog.text

    # Verify the request was logged but with only the command, not the guess
    row = daemon.conn.execute(
        "SELECT command FROM processed_requests WHERE packet_id=9306"
    ).fetchone()
    assert row["command"] == "BOOK"


def test_book_score_dm_reports_mesh_wide_aggregates_and_writes_nothing(daemon):
    day = fortune.local_day(int(time.time()), daemon.cfg.service.timezone)
    puzzle = booktrivia.puzzle_for(day, daemon.cfg.service.fortune_salt)

    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=f"BOOK {puzzle.title}", packet_id=9330)
    )
    daemon.drain()

    daemon.iface.emit_packet(
        make_packet(
            from_num=0x22222222, to_num=daemon.my_node_num, text="BOOK SCORE", packet_id=9331
        )
    )
    daemon.drain()

    assert len(daemon.iface.sent) == 2
    reply = daemon.iface.sent[1]["text"]
    assert reply == booktrivia.book_score_text(daemon.conn, day)
    assert b(reply) <= 180

    key = booktrivia.book_key(puzzle)
    before = daemon.conn.execute(
        "SELECT shown, guesses, solves FROM book_stats WHERE book_key=? AND day=?", (key, day)
    ).fetchone()

    daemon.iface.emit_packet(
        make_packet(
            from_num=0x33333333, to_num=daemon.my_node_num, text="book scores", packet_id=9332
        )
    )
    daemon.drain()
    assert daemon.iface.sent[2]["text"] == reply

    after = daemon.conn.execute(
        "SELECT shown, guesses, solves FROM book_stats WHERE book_key=? AND day=?", (key, day)
    ).fetchone()
    assert tuple(after) == tuple(before)


# --- SEEN / WHO / ME / PING, WHOAMI name addendum ---------------------------------


def test_seen_reports_missing_hops_and_target_packet_count(daemon):
    for packet_id in (9190, 9191):
        daemon.iface.emit_packet(make_packet(
            from_num=0x99999999, to_num=daemon.my_node_num,
            packet_id=packet_id, text="hello",
        ))
    daemon.drain()
    daemon.iface.emit_packet(make_packet(
        to_num=daemon.my_node_num, text="SEEN !99999999", packet_id=9192,
    ))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "hops not reported" in reply
    assert "24h: 2 packets" in reply
    assert "heard 1/7 days" in reply
    assert "SNR" not in reply
    assert b(reply) <= 180


def test_seen_falls_back_to_not_heard_when_the_observation_row_was_pruned(daemon, monkeypatch):
    """Regression: retention.observations_days can be shorter than SEEN's
    7-day days_seen_last_n window, so the observations row backing
    last_observation() can be pruned while node_daily rows (which
    days_seen_last_n reads) survive longer -- last_observation() then
    returns None even though days_seen > 0, which used to raise a
    TypeError indexing last_obs["server_time"]."""
    daemon.iface.emit_packet(make_packet(
        from_num=0x99999999, to_num=daemon.my_node_num, packet_id=9193, text="hello",
    ))
    daemon.drain()
    monkeypatch.setattr(daemon._collector, "last_observation", lambda node_id: None)
    daemon.iface.emit_packet(make_packet(
        to_num=daemon.my_node_num, text="SEEN !99999999", packet_id=9194,
    ))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "not heard" in reply


def test_seen_dm_for_an_unseen_node_gets_not_heard(daemon):
    """A fresh daemon has only just started listening (its own DB has no
    observations older than this very SEEN request), so the reply must not
    claim a 7-day window it hasn't actually listened for -- see
    test_seen_dm_for_an_unseen_node_after_listening_7_days for the case
    where it has."""
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="SEEN !99999999", packet_id=9101)
    )
    daemon.drain()
    reply = daemon.iface.sent[0]["text"]
    assert "not heard in 7 days" not in reply
    assert "not heard since I started listening" in reply


def test_seen_dm_for_an_unseen_node_after_listening_7_days_gets_not_heard(daemon):
    """Once our own earliest observation is >= 7 days old, SEEN's original
    "not heard in 7 days" wording is honest again."""
    daemon.conn.execute(
        "INSERT INTO observations (server_time, packet_time, sender_node_id, dest_node_id, "
        "portnum, channel, is_duplicate) VALUES (?, ?, '!aabbccdd', '!11223344', "
        "'TEXT_MESSAGE_APP', 0, 0)",
        (int(time.time()) - 8 * 86400, int(time.time()) - 8 * 86400),
    )
    daemon.conn.commit()
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="SEEN !99999999", packet_id=9103)
    )
    daemon.drain()
    assert "not heard in 7 days" in daemon.iface.sent[0]["text"]


def test_seen_bare_with_no_argument_gets_no_reply(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="SEEN", packet_id=9102))
    daemon.drain()
    assert daemon.iface.sent == []


def test_unknown_command_leaves_no_processed_requests_row_or_quota_usage(daemon):
    """Baseline for the SEEN-disabled parity test below: a genuinely
    unrecognized command's early return in `_process_obs` (before
    is_blocked/rate-limit/dedup-claim/quota) means it never claims a
    processed_requests row and never consumes a quota slot."""
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="what is this thing", packet_id=9199)
    )
    daemon.drain()
    assert daemon.iface.sent == []
    assert (
        daemon.conn.execute(
            "SELECT 1 FROM processed_requests WHERE packet_id=9199"
        ).fetchone()
        is None
    )
    assert daemon.conn.execute("SELECT COUNT(*) FROM node_daily_quota").fetchone()[0] == 0


def test_seen_disabled_by_config_is_silent_and_leaves_no_footprint(tmp_path):
    """Ruling 15: seen_enabled=False makes SEEN behave "exactly as if the
    command didn't exist" -- not merely a silent reply, but the same zero
    processed_requests/quota footprint as a genuinely unrecognized command
    (see the UNKNOWN-command test above, which this mirrors)."""
    cfg = replace(_cfg(tmp_path), service=replace(ServiceConfig(), seen_enabled=False))
    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    try:
        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, text="SEEN !99999999", packet_id=9103)
        )
        d.drain()
        assert d.iface.sent == []
        assert (
            d.conn.execute("SELECT 1 FROM processed_requests WHERE packet_id=9103").fetchone()
            is None
        )
        assert d.conn.execute("SELECT COUNT(*) FROM node_daily_quota").fetchone()[0] == 0
    finally:
        d.stop()


def test_an_old_replayed_dm_gets_no_reply_and_is_rejected_as_stale(daemon, caplog):
    """Regression test: `_ingest_sink` used to re-stamp `rx_time` to the
    ingestion clock on every packet, so `check_eligibility`'s freshness gate
    (`received_at - rx_time > reply_ttl_seconds`) could never fire -- a DM
    replayed an hour late would still get a live reply. `rx_time` must
    survive ingestion unchanged (mod whatever offset a test injects via
    advance_seconds) so a genuinely stale packet is rejected, and the
    observations row must keep the radio's own timestamp, not ours."""
    caplog.set_level("DEBUG")
    old_rx_time = int(time.time()) - 3600  # well past the 60s default reply_ttl_seconds
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MENU", packet_id=8801, rx_time=old_rx_time)
    )
    daemon.drain()
    assert daemon.iface.sent == []
    assert "reason=stale" in caplog.text
    row = daemon.conn.execute(
        "SELECT packet_time FROM observations WHERE packet_id=8801"
    ).fetchone()
    assert row is not None
    assert row[0] == old_rx_time


def test_a_knock_outcome_never_crashes_the_db_worker(daemon, monkeypatch):
    """Regression (2B fix round 1, finding 2): `InboundDecision.reason` is
    None whenever the outcome is not REJECTED (commands.py's own
    __post_init__ invariant), so `_process_obs`'s ineligible branch must
    never unconditionally do `decision.reason.value` -- KNOCK carries no
    reason at all. Unreachable via a real Meshtastic packet today
    (`_process_obs` hardcodes `channel_policy=None`, so `decide_inbound` can
    never actually return KNOCK for this transport), so this forces the
    outcome directly by monkeypatching `main.decide_inbound` -- the only
    way to exercise this code path before a channel-triggering transport
    exists."""

    def force_knock(*args, **kwargs):
        return commands.InboundDecision(commands.Outcome.KNOCK)

    monkeypatch.setattr(main, "decide_inbound", force_knock)
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="HELP"))
    daemon.drain()  # must not raise (AttributeError on `None.value`, pre-fix)
    assert daemon.iface.sent == []
    # The distinguishing assertion (fix round 2): _db_worker_loop wraps
    # _process_obs in a broad `except Exception` that logs and increments
    # database_errors_total, then settles the request as failed -- so the
    # pre-fix AttributeError never crosses the thread boundary and
    # "daemon.drain() must not raise" alone passes against BOTH the fixed
    # and the broken code. This counter is the tell: it stays 0 only when
    # _process_obs actually completes cleanly.
    out = generate_latest(daemon._metrics.registry).decode()
    assert "meshgarden_database_errors_total 0.0" in out


def test_seen_ambiguous_short_name_never_echoes_the_argument(daemon):
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x11AACCDD, packet_id=9104,
            user={"shortName": "dup", "longName": "Dup One"}, portnum="NODEINFO_APP",
        )
    )
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x22BBCCDD, packet_id=9105,
            user={"shortName": "dup", "longName": "Dup Two"}, portnum="NODEINFO_APP",
        )
    )
    daemon.drain()
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="SEEN dup", packet_id=9106)
    )
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "dup" not in reply
    assert "Multiple nodes match" in reply


def test_seen_stale_short_name_matches_the_never_seen_reply_byte_for_byte(daemon):
    """Ruling 1 / final-review fix: a short name that RESOLVES but hasn't
    been heard in the last 7 days must produce the exact same reply as an
    unknown short name -- the label is derived from the ARGUMENT's shape
    (`_node_label_for`), not from the resolved node id, in both the
    unresolved and the resolved-but-stale branch. Previously the stale
    branch echoed the node's own id tail, distinguishing "never seen" from
    "seen too long ago"."""
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x55556666, packet_id=9107,
            user={"shortName": "gone", "longName": "Gone Node"}, portnum="NODEINFO_APP",
        )
    )
    daemon.drain()
    daemon.advance_seconds(8 * 86400)

    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="SEEN gone", packet_id=9108)
    )
    daemon.drain()
    stale_reply = daemon.iface.sent[-1]["text"]
    assert "gone" not in stale_reply

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="SEEN nosuchname", packet_id=9109)
    )
    daemon.drain()
    unknown_reply = daemon.iface.sent[-1]["text"]

    assert stale_reply == unknown_reply


def test_seen_stale_full_id_still_shows_the_tail(daemon):
    """The id/tail branches of `_node_label_for` are unaffected -- a stale
    lookup by full id (or bare 4-hex tail) still shows that id's own
    tail, same as before."""
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x77778888, packet_id=9110,
            user={"shortName": "tailn", "longName": "Tail Node"}, portnum="NODEINFO_APP",
        )
    )
    daemon.drain()
    daemon.advance_seconds(8 * 86400)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="SEEN !77778888", packet_id=9111)
    )
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "8888" in reply


def test_who_dm_produces_one_reply(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="WHO", packet_id=9107))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"].startswith("👥")


def test_ping_dm_produces_one_reply_with_signal_fields(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="PING", packet_id=9108, rx_snr=10.5)
    )
    daemon.drain()
    assert daemon.iface.sent[0]["text"].startswith("🏓 pong")


def test_post_then_read_round_trips_through_a_topic(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="POST bike lost my dog", packet_id=9201)
    )
    daemon.drain()
    assert daemon.iface.sent[0]["text"] == "📌 #bike · posted"
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x99999999, text="READ bike", packet_id=9202
        )
    )
    daemon.drain()
    assert "lost my dog" in daemon.iface.sent[1]["text"]


def test_read_with_a_non_ascii_digit_page_falls_back_to_page_1(daemon):
    """Regression: '²'.isdigit() is True but int('²') raises
    ValueError -- READ's page parsing must guard with isascii() first
    instead of crashing."""
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="POST bike lost my dog", packet_id=9220)
    )
    daemon.drain()
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x99999999, text="READ bike ²", packet_id=9221
        )
    )
    daemon.drain()
    assert "lost my dog" in daemon.iface.sent[-1]["text"]


def test_post_over_max_topics_is_silently_dropped(tmp_path):
    cfg = replace(
        _cfg(tmp_path), service=ServiceConfig(max_topics=1, lab_unlock_word=LAB_UNLOCK_WORD,
                              per_node_cooldown_seconds=0, sign_cooldown_seconds=0)
    )
    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    d.conn.execute("INSERT INTO lab_nodes (node_id, unlocked_at) VALUES ('!aabbccdd', 0)")
    d.conn.commit()
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="POST bike lost dog", packet_id=1))
    d.drain()
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="POST lost found cat", packet_id=2))
    d.drain()
    assert len(d.iface.sent) == 1  # second, new-topic POST silently dropped
    d.stop()


def test_boards_dm_lists_topics_by_count(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="POST bike test1", packet_id=9203)
    )
    daemon.drain()
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="BOARDS", packet_id=9204))
    daemon.drain()
    assert "#bike" in daemon.iface.sent[-1]["text"]


def test_read_of_a_topic_with_nothing_visible_gets_the_honest_empty_reply(daemon):
    """A topic can exist (distinct_topics_in_use includes hidden entries,
    so READ's own gate lets it through) yet have nothing visible right now
    -- here, because the only post was UNSIGN'd. Page 1 must say the
    board is empty, not "No more entries" (which would wrongly imply
    there was ever something to page through)."""
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="POST bike lost my dog", packet_id=9210)
    )
    daemon.drain()
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="UNSIGN", packet_id=9211))
    daemon.drain()
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="READ bike", packet_id=9212)
    )
    daemon.drain()
    assert daemon.iface.sent[-1]["text"] == "📌 #bike is empty\nPOST bike <msg> to be first 🌱"


def test_sign_cooldown_gets_a_reason_reply_not_silence(tmp_path):
    """Fix round (SIGN reply-with-reason), finding: a visitor who SIGNs
    twice within sign_cooldown_seconds used to get no reply at all for the
    2nd attempt -- indistinguishable from a lost packet. It now gets
    guestbook.format_sign_cooldown's reason, computed from the limiter's
    actual remaining time (default sign_cooldown_seconds=3600, so ~60 min
    remain immediately after the first SIGN). per_node_cooldown_seconds is
    zeroed here so the GENERAL per-command cooldown doesn't itself block
    the 2nd packet before it ever reaches the SIGN-specific check."""
    cfg = replace(
        _cfg(tmp_path),
        service=replace(
            ServiceConfig(lab_unlock_word=LAB_UNLOCK_WORD), per_node_cooldown_seconds=0
        ),
    )
    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    try:
        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, text="SIGN first entry", packet_id=9930)
        )
        d.drain()
        assert d.iface.sent[0]["text"] == "🌱 Signed!\nThanks! Others read it with GUESTBOOK"

        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="SIGN too soon", packet_id=9931))
        d.drain()
        assert len(d.iface.sent) == 2
        assert d.iface.sent[1]["text"] == format_sign_cooldown(3600)

        count = d.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0]
        assert count == 1  # the 2nd SIGN was never inserted
        assert d._metrics.commands_total.labels("SIGN", "rate_limited")._value.get() == 1
    finally:
        d.stop()


def test_sign_duplicate_gets_a_reason_reply_not_silence(tmp_path):
    """Same fix, for the identical-text suppression path: SIGNing the exact
    same normalized text twice (with the per-sender SIGN cooldown disabled
    here so the duplicate check, not the cooldown, is what fires) now
    explains why nothing new was saved instead of staying silent."""
    cfg = replace(
        _cfg(tmp_path),
        service=replace(
            ServiceConfig(lab_unlock_word=LAB_UNLOCK_WORD),
            sign_cooldown_seconds=0,
            per_node_cooldown_seconds=0,
        ),
    )
    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    try:
        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, text="SIGN the same note", packet_id=9932)
        )
        d.drain()
        assert d.iface.sent[0]["text"] == "🌱 Signed!\nThanks! Others read it with GUESTBOOK"

        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, text="SIGN the same note", packet_id=9933)
        )
        d.drain()
        assert len(d.iface.sent) == 2
        assert d.iface.sent[1]["text"] == format_sign_duplicate(
            cfg.service.identical_sign_dedup_hours
        )
        count = d.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0]
        assert count == 1  # the 2nd, duplicate SIGN was never inserted
        assert d._metrics.commands_total.labels("SIGN", "rejected")._value.get() == 1
    finally:
        d.stop()


def test_sign_too_long_gets_a_reason_reply_not_silence(daemon):
    """Same fix, for Guestbook.validate's SIGN_TOO_LONG rejection."""
    long_message = "a" * 101  # over ServiceConfig.max_guestbook_chars (100)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=f"SIGN {long_message}", packet_id=9934)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"] == format_sign_too_long(
        daemon.cfg.service.max_guestbook_chars
    )
    count = daemon.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0]
    assert count == 0  # never inserted


def test_unsign_then_sign_again_succeeds_with_a_new_entry(daemon):
    """Fix round (UNSIGN frees the slot), the operator-confirmed bug: SIGN,
    then UNSIGN, then the same SIGN again minutes later used to get no
    reply at all -- `is_duplicate_sign` matched the now-hidden entry, and
    the SIGN cooldown (still running from the first SIGN) would have
    blocked it either way. `hide_own_latest` (what UNSIGN calls) now also
    resets the sender's SIGN cooldown, and the hidden entry no longer
    counts as a duplicate, so the retry succeeds with a brand-new entry."""
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="SIGN hello there", packet_id=9940)
    )
    daemon.drain()
    assert daemon.iface.sent[0]["text"] == "🌱 Signed!\nThanks! Others read it with GUESTBOOK"

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="UNSIGN", packet_id=9941))
    daemon.drain()
    assert "now hidden" in daemon.iface.sent[1]["text"]

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="SIGN hello there", packet_id=9942)
    )
    daemon.drain()
    assert daemon.iface.sent[2]["text"] == "🌱 Signed!\nThanks! Others read it with GUESTBOOK"

    rows = daemon.conn.execute(
        "SELECT entry_id, visible FROM guestbook ORDER BY entry_id"
    ).fetchall()
    assert [(r["entry_id"], r["visible"]) for r in rows] == [(1, 0), (2, 1)]


def test_post_is_refused_with_the_same_storage_full_reply_as_sign(daemon_full_storage):
    """Fix round 1, finding 1: boards share the guestbook table with SIGN
    (ruling 5), so POST must respect the same storage-pressure guard --
    same fixed reply, no new guestbook row."""
    d = daemon_full_storage
    before = d.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0]
    d.iface.emit_packet(
        make_packet(to_num=d.my_node_num, text="POST bike please store me", packet_id=1)
    )
    d.drain()
    assert d.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == before
    assert len(d.iface.sent) == 1
    assert "full" in d.iface.sent[-1]["text"].lower()


def test_post_over_long_message_logs_post_too_long(daemon):
    """Fix round 1, finding 2: a POST message rejected by
    Guestbook.validate (SignError(Reject.SIGN_TOO_LONG)) must be remapped
    to Reject.POST_TOO_LONG in processed_requests -- never logged under a
    SIGN code."""
    long_message = "a" * 101  # over ServiceConfig.max_guestbook_chars (100)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=f"POST bike {long_message}", packet_id=9205)
    )
    daemon.drain()
    row = daemon.conn.execute(
        "SELECT reason FROM processed_requests WHERE packet_id=9205"
    ).fetchone()
    assert row["reason"] == "post_too_long"
    assert daemon.iface.sent == []


def test_post_duplicate_logs_post_duplicate_not_sign_duplicate(daemon):
    """Final-review fix (item 6): a duplicate POST's
    `Guestbook.is_duplicate_sign` rejection must be logged under its own
    `Reject.POST_DUPLICATE` code, not the SIGN one -- an audit reading
    processed_requests.reason must be able to tell a POST duplicate from a
    SIGN duplicate, same reason POST_TOO_LONG exists instead of reusing
    SIGN_TOO_LONG."""
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="POST bike same message", packet_id=9207)
    )
    daemon.drain()
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="POST bike same message", packet_id=9208)
    )
    daemon.drain()
    row = daemon.conn.execute(
        "SELECT reason FROM processed_requests WHERE packet_id=9208"
    ).fetchone()
    assert row["reason"] == "post_duplicate"


def test_post_increments_guestbook_entries_created(daemon):
    """Fix round 1, finding 3: a successful POST must bump
    guestbook_entries_created, same as SIGN."""
    out = generate_latest(daemon._metrics.registry).decode()
    assert "meshgarden_guestbook_entries_created_total 0.0" in out
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="POST bike hello board", packet_id=9206)
    )
    daemon.drain()
    out = generate_latest(daemon._metrics.registry).decode()
    assert "meshgarden_guestbook_entries_created_total 1.0" in out


def test_mail_on_then_off_acknowledges_each_toggle(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL ON", packet_id=9301)
    )
    daemon.drain()
    assert "Mail is ON" in daemon.iface.sent[0]["text"]
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL OFF", packet_id=9302)
    )
    daemon.drain()
    assert "Mail is OFF" in daemon.iface.sent[1]["text"]


def test_mail_alias_claim_status_and_release(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL ALIAS Garden_Fox", packet_id=9290)
    )
    daemon.drain()
    assert "@garden_fox" in daemon.iface.sent[-1]["text"]
    row = daemon.conn.execute(
        "SELECT alias FROM mail_aliases WHERE node_id='!aabbccdd'"
    ).fetchone()
    assert row["alias"] == "garden_fox"

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL ALIAS", packet_id=9291)
    )
    daemon.drain()
    assert "@garden_fox" in daemon.iface.sent[-1]["text"]

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL ALIAS OFF", packet_id=9292)
    )
    daemon.drain()
    assert "removed" in daemon.iface.sent[-1]["text"]
    assert daemon.conn.execute(
        "SELECT alias FROM mail_aliases WHERE node_id='!aabbccdd'"
    ).fetchone()[0] is None


def test_mail_can_route_to_alias_without_revealing_unknown_aliases(daemon):
    recipient = 0x99999999
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num,
            from_num=recipient,
            text="MAIL ON",
            packet_id=9280,
        )
    )
    daemon.drain()
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num,
            from_num=recipient,
            text="MAIL ALIAS foxden",
            packet_id=9281,
        )
    )
    daemon.drain()

    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL @foxden hello alias", packet_id=9282)
    )
    daemon.drain()
    assert daemon.conn.execute(
        "SELECT message FROM mail WHERE recipient_node_id='!99999999'"
    ).fetchone()[0] == "hello alias"
    known_ack = daemon.iface.sent[-1]["text"]

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL @missingfox hidden", packet_id=9283)
    )
    daemon.drain()
    assert daemon.iface.sent[-1]["text"] == known_ack
    assert daemon.conn.execute("SELECT COUNT(*) FROM mail").fetchone()[0] == 1


def test_mail_send_gives_the_same_neutral_ack_whether_or_not_recipient_opted_in(daemon):
    # recipient (0x99999999) never opts in for the first send.
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL !99999999 hi test", packet_id=9303)
    )
    daemon.drain()
    ack_not_opted_in = daemon.iface.sent[0]["text"]

    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x99999999, text="MAIL ON", packet_id=9304
        )
    )
    daemon.drain()
    # Two MAIL sends from the same default sender (0xAABBCCDD) in this test
    # -- advance past per_node_cooldown_seconds so the second isn't
    # silently rate-limited, same pattern as test_post_then_read etc.
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL !99999999 hi again test", packet_id=9305)
    )
    daemon.drain()
    ack_opted_in = daemon.iface.sent[-1]["text"]
    assert (
        ack_not_opted_in
        == ack_opted_in
        == (
            "📬 Note handed to MeshGarden. The recipient sees it only if they "
            "have MAIL ON and DM me."
        )
    )


def test_mail_list_delivers_and_removes_pending_notes(daemon):
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x99999999, text="MAIL ON", packet_id=9306
        )
    )
    daemon.drain()
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL !99999999 hi test", packet_id=9307)
    )
    daemon.drain()
    # 0x99999999's own second command (bare MAIL) after its earlier "MAIL
    # ON" -- advance past the per-node cooldown.
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, from_num=0x99999999, text="MAIL", packet_id=9308)
    )
    daemon.drain()
    assert "hi test" in daemon.iface.sent[-1]["text"]
    # The list is only actually delivered (and its notes removed) once
    # this reply is genuinely acked -- see _resolve_pending_mail_deliveries.
    tx = daemon.conn.execute(
        "SELECT tx_packet_id FROM processed_requests WHERE packet_id=9308"
    ).fetchone()[0]
    daemon.iface.emit_packet(
        make_packet(
            from_num=0x99999999,
            to_num=daemon.my_node_num,
            portnum="ROUTING_APP",
            text=None,
            packet_id=9350,
            request_id=tx,
            routing_error="NONE",
        )
    )
    daemon.drain()
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, from_num=0x99999999, text="MAIL", packet_id=9309)
    )
    daemon.drain()
    assert "No notes waiting" in daemon.iface.sent[-1]["text"]


def test_mail_list_only_marks_shown_notes_delivered_when_one_does_not_fit(tmp_path):
    """Ruling 11 / bug-fix regression: two pending notes from the SAME
    sender, with a byte budget small enough that only the newest fits --
    the older one must stay pending (never marked delivered) because it
    was never actually shown. This is exactly the scenario a marker-
    matching approach (matching by `from <tail> · ` in the rendered text)
    gets wrong: both notes share the same sender tail, so it would wrongly
    count both as shown."""
    cfg = replace(
        _cfg(tmp_path),
        service=ServiceConfig(max_response_bytes=70, lab_unlock_word=LAB_UNLOCK_WORD),
    )
    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    d.conn.execute("INSERT INTO lab_nodes (node_id, unlocked_at) VALUES ('!aabbccdd', 0)")
    d.conn.execute("INSERT INTO lab_nodes (node_id, unlocked_at) VALUES ('!99999999', 0)")
    d.conn.commit()

    d.iface.emit_packet(
        make_packet(to_num=d.my_node_num, from_num=0x99999999, text="MAIL ON", packet_id=1)
    )
    d.drain()
    d.advance_seconds(d.cfg.service.per_node_cooldown_seconds + 1)
    d.iface.emit_packet(
        make_packet(to_num=d.my_node_num, text="MAIL !99999999 older note test", packet_id=2)
    )
    d.drain()
    d.advance_seconds(d.cfg.service.per_node_cooldown_seconds + 1)
    d.iface.emit_packet(
        make_packet(to_num=d.my_node_num, text="MAIL !99999999 newer note test", packet_id=3)
    )
    d.drain()
    d.advance_seconds(d.cfg.service.per_node_cooldown_seconds + 1)

    d.iface.emit_packet(
        make_packet(to_num=d.my_node_num, from_num=0x99999999, text="MAIL", packet_id=4)
    )
    d.drain()
    first_list = d.iface.sent[-1]["text"]
    assert "newer note" in first_list
    assert "older note" not in first_list  # didn't fit -- must stay pending

    # Ack the first list's reply so its shown note is actually removed --
    # see _resolve_pending_mail_deliveries.
    tx = d.conn.execute(
        "SELECT tx_packet_id FROM processed_requests WHERE packet_id=4"
    ).fetchone()[0]
    d.iface.emit_packet(
        make_packet(
            from_num=0x99999999,
            to_num=d.my_node_num,
            portnum="ROUTING_APP",
            text=None,
            packet_id=9350,
            request_id=tx,
            routing_error="NONE",
        )
    )
    d.drain()
    d.advance_seconds(d.cfg.service.per_node_cooldown_seconds + 1)
    d.iface.emit_packet(
        make_packet(to_num=d.my_node_num, from_num=0x99999999, text="MAIL", packet_id=5)
    )
    d.drain()
    second_list = d.iface.sent[-1]["text"]
    assert "older note" in second_list  # the one note left pending
    d.stop()


def test_mail_list_send_failure_leaves_the_note_pending(daemon):
    """Finding-2 regression: `_handle_mail_list` used to `mark_delivered`
    a note as soon as its reply was merely QUEUED, not actually delivered
    -- a send failure then lost the note for good even though the
    requester never received the list. Simulate that failure the same
    way tests/test_sender.py does (monkeypatching RadioAdapter.send_text
    to raise) and confirm the note survives, still pending."""
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x99999999, text="MAIL ON", packet_id=9360
        )
    )
    daemon.drain()
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL !99999999 must survive", packet_id=9361)
    )
    daemon.drain()

    def boom(*, text, destination_id):
        raise RuntimeError("radio exploded")

    daemon._radio.send_text = boom
    sent_before = len(daemon.iface.sent)
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, from_num=0x99999999, text="MAIL", packet_id=9362)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == sent_before  # the failed send never reached the radio
    assert (
        daemon.conn.execute(
            "SELECT state FROM processed_requests WHERE packet_id=9362"
        ).fetchone()[0]
        == "failed"
    )
    row = daemon.conn.execute(
        "SELECT message FROM mail WHERE recipient_node_id='!99999999'"
    ).fetchone()
    assert row is not None
    assert row[0] == "must survive"


def test_mail_list_delivery_is_settled_by_a_genuine_ack(daemon):
    """The happy path for the finding-2 fix: once the MAIL-list reply is
    genuinely acked (not merely queued), the shown note is removed --
    same end state as before the fix, just gated on the real outcome."""
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x99999999, text="MAIL ON", packet_id=9370
        )
    )
    daemon.drain()
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL !99999999 ack path test", packet_id=9371)
    )
    daemon.drain()
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, from_num=0x99999999, text="MAIL", packet_id=9372)
    )
    daemon.drain()
    assert "ack path test" in daemon.iface.sent[-1]["text"]
    # Still pending immediately after the reply is only queued.
    assert (
        daemon.conn.execute(
            "SELECT COUNT(*) FROM mail WHERE recipient_node_id='!99999999'"
        ).fetchone()[0]
        == 1
    )
    tx = daemon.conn.execute(
        "SELECT tx_packet_id FROM processed_requests WHERE packet_id=9372"
    ).fetchone()[0]
    daemon.iface.emit_packet(
        make_packet(
            from_num=0x99999999,
            to_num=daemon.my_node_num,
            portnum="ROUTING_APP",
            text=None,
            packet_id=9351,
            request_id=tx,
            routing_error="NONE",
        )
    )
    daemon.drain()
    assert (
        daemon.conn.execute(
            "SELECT COUNT(*) FROM mail WHERE recipient_node_id='!99999999'"
        ).fetchone()[0]
        == 0
    )


def test_mail_list_delivery_tracking_survives_past_the_reply_ttl_until_the_ack(daemon):
    """Follow-up finding on the finding-2 fix: an ack can legitimately
    arrive any time within ack_timeout_seconds (300 s), but the delivery
    tracking used to be discarded after reply_ttl_seconds (60 s) BEFORE
    the ack state was consulted -- so an ack at 61 s left the note pending
    and re-deliverable forever. The mapping must outlive the reply TTL and
    still settle on the late ack."""
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x99999999, text="MAIL ON", packet_id=9380
        )
    )
    daemon.drain()
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL !99999999 ack path test", packet_id=9381)
    )
    daemon.drain()
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, from_num=0x99999999, text="MAIL", packet_id=9382)
    )
    daemon.drain()
    assert "ack path test" in daemon.iface.sent[-1]["text"]
    # Still pending immediately after the reply is only queued.
    assert (
        daemon.conn.execute(
            "SELECT COUNT(*) FROM mail WHERE recipient_node_id='!99999999'"
        ).fetchone()[0]
        == 1
    )
    tx = daemon.conn.execute(
        "SELECT tx_packet_id FROM processed_requests WHERE packet_id=9382"
    ).fetchone()[0]
    # Past the reply TTL, still inside the ack window. The ack handler
    # itself runs the resolver (on the DB thread), so this alone exercised
    # the bug: the age bound fired before the freshly-acked state was read.
    daemon.advance_seconds(daemon.cfg.service.reply_ttl_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(
            from_num=0x99999999,
            to_num=daemon.my_node_num,
            portnum="ROUTING_APP",
            text=None,
            packet_id=9352,
            request_id=tx,
            routing_error="NONE",
        )
    )
    daemon.drain()
    assert (
        daemon.conn.execute(
            "SELECT COUNT(*) FROM mail WHERE recipient_node_id='!99999999'"
        ).fetchone()[0]
        == 0
    )


def test_mail_over_long_message_logs_mail_too_long(daemon):
    """Fix round 1, review finding: a MAIL message rejected by
    Guestbook.validate (SignError(Reject.SIGN_TOO_LONG)) must be remapped
    to Reject.MAIL_TOO_LONG in processed_requests -- never logged under a
    SIGN code, mirroring _handle_post's own _POST_SIGN_ERROR_REASON
    remap/test (test_post_over_long_message_logs_post_too_long)."""
    long_message = "a" * 101  # over ServiceConfig.max_guestbook_chars (100)
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, text=f"MAIL !99999999 {long_message}", packet_id=9310
        )
    )
    daemon.drain()
    row = daemon.conn.execute(
        "SELECT reason FROM processed_requests WHERE packet_id=9310"
    ).fetchone()
    assert row["reason"] == "mail_too_long"
    assert daemon.iface.sent == []


def test_mail_self_send_is_allowed_by_design(daemon):
    """Self-mail is allowed by design -- MAIL <own id> <msg> from an
    opted-in sender stores a note to self, and bare MAIL lists it back."""
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL ON", packet_id=9320)
    )
    daemon.drain()
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, text="MAIL !aabbccdd note to myself test", packet_id=9321
        )
    )
    daemon.drain()
    assert daemon.iface.sent[-1]["text"] == (
        "📬 Note handed to MeshGarden. The recipient sees it only if they "
        "have MAIL ON and DM me."
    )
    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="MAIL", packet_id=9322)
    )
    daemon.drain()
    assert "note to myself" in daemon.iface.sent[-1]["text"]


def test_mail_with_a_banned_word_is_stored_hidden_and_never_listed(tmp_path):
    """Same moderation contract as SIGN's own banned-word test
    (test_sign_with_a_banned_word_is_hidden_but_still_acked): a banned-word
    MAIL body is still stored (never silently discarded) but hidden, gets
    the ordinary neutral ack (ruling 10 -- moderation state is invisible to
    the sender, same as opt-in/pending-cap state), counts as "stored" in
    the bounded metric, and never appears in the recipient's bare MAIL
    listing (Mail.pending_for filters hidden=0, same convention as the
    guestbook)."""
    banned_path = tmp_path / "banned.txt"
    banned_path.write_text("badword\n")
    cfg = replace(_cfg(tmp_path), moderation=ModerationConfig(banned_words_file=str(banned_path)))
    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    d.conn.execute("INSERT INTO lab_nodes (node_id, unlocked_at) VALUES ('!99999999', 0)")
    d.conn.execute("INSERT INTO lab_nodes (node_id, unlocked_at) VALUES ('!aabbccdd', 0)")
    d.conn.commit()
    try:
        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, from_num=0x99999999, text="MAIL ON", packet_id=1)
        )
        d.drain()
        d.advance_seconds(d.cfg.service.per_node_cooldown_seconds + 1)
        d.iface.emit_packet(
            make_packet(
                to_num=d.my_node_num, text="MAIL !99999999 this has a badword in it", packet_id=2
            )
        )
        d.drain()
        assert d.iface.sent[-1]["text"] == (
            "📬 Note handed to MeshGarden. The recipient sees it only if they "
            "have MAIL ON and DM me."
        )

        row = d.conn.execute("SELECT hidden, moderation_reason FROM mail").fetchone()
        assert row["hidden"] == 1
        assert row["moderation_reason"] == "banned_word"
        assert d._metrics.mail_notes_stored._value.get() == 1

        d.advance_seconds(d.cfg.service.per_node_cooldown_seconds + 1)
        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, from_num=0x99999999, text="MAIL", packet_id=3)
        )
        d.drain()
        assert "No notes waiting" in d.iface.sent[-1]["text"]
    finally:
        d.stop()


# --- LAB tier: per-node unlock word gates POST/READ/BOARDS/MAIL (Task 13) ----


def test_lab_locked_node_gets_unknown_behavior_for_post_with_no_footprint(daemon):
    """A node that never sent the unlock word is locked (the `daemon`
    fixture's word is configured, but this sender -- unlike the standard
    pre-unlocked senders -- was never added to lab_nodes): POST must behave
    exactly like an unrecognized command, same zero processed_requests/
    quota footprint as test_seen_disabled_by_config_is_silent... mirrors."""
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x33333333, text="POST bike hi test",
            packet_id=9401,
        )
    )
    daemon.drain()
    assert daemon.iface.sent == []
    assert (
        daemon.conn.execute("SELECT 1 FROM processed_requests WHERE packet_id=9401").fetchone()
        is None
    )
    assert daemon.conn.execute("SELECT COUNT(*) FROM node_daily_quota").fetchone()[0] == 0


def test_locked_node_help_menu_reply_does_not_leak_the_lab_tier(daemon):
    """Final-review fix: HELP MENU is not LAB-gated (unlike HELP POST/READ/
    BOARDS/MAIL, which main.py rewrites to UNKNOWN for a locked node) --
    the daemon fixture's default sender never sent the unlock word, so it
    stays locked, and HELP MENU must still reply (no gate), just without
    naming the LAB-only MENU BOARDS sub-page."""
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="HELP menu", packet_id=9404)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert "BOARDS" not in daemon.iface.sent[0]["text"]


@pytest.mark.parametrize("cmd", sorted(c.value for c in commands.LAB_COMMANDS))
def test_lab_locked_node_help_is_silent_for_every_lab_command(daemon, cmd):
    """Final-review fix (item 3): `_process_obs`'s LAB gate derives
    `lab_gated_help` from `LAB_COMMANDS` itself (`{c.value for c in
    LAB_COMMANDS}`) rather than a hardcoded tuple, so this must hold for
    every current and future LAB command, not just the four checked by
    name in the older test below."""
    daemon.cfg = replace(daemon.cfg, service=replace(daemon.cfg.service, mail_public=False))
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x33333333, text=f"HELP {cmd}", packet_id=9405,
        )
    )
    daemon.drain()
    assert daemon.iface.sent == []


def test_lab_locked_node_help_post_and_menu_lab_are_also_silent(daemon):
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x33333333, text="HELP post", packet_id=9402
        )
    )
    daemon.drain()
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x33333333, text="MENU LAB", packet_id=9403
        )
    )
    daemon.drain()
    assert daemon.iface.sent == []


def test_lab_unlock_word_unlocks_the_node_then_post_menu_lab_and_help_work(daemon):
    """A never-before-seen node sends the unlock word -- a normal simple
    command through the pipeline: one reply starting with the lab emoji,
    a lab_nodes row created. Afterwards the SAME node's POST/MENU LAB/HELP
    post all work, while a DIFFERENT node stays locked."""
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x33333333, text="SUPERGARDENME", packet_id=9410,
        )
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"].startswith("🧪")
    row = daemon.conn.execute(
        "SELECT unlocked_at FROM lab_nodes WHERE node_id='!33333333'"
    ).fetchone()
    assert row is not None

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x33333333, text="POST bike hi test",
            packet_id=9411,
        )
    )
    daemon.drain()
    assert daemon.iface.sent[-1]["text"] == "📌 #bike · posted"

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, from_num=0x33333333, text="MENU LAB", packet_id=9412)
    )
    daemon.drain()
    assert daemon.iface.sent[-1]["text"].startswith("🧪")

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x33333333, text="HELP post", packet_id=9413
        )
    )
    daemon.drain()
    assert "POST <topic>" in daemon.iface.sent[-1]["text"]

    # A different, never-unlocked node stays locked.
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x44444444, text="POST bike still locked",
            packet_id=9414,
        )
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 4  # no new send for the still-locked node


def test_lab_unlock_word_repeated_keeps_the_original_unlock_time_and_still_replies(daemon):
    """LAB_UNLOCK is idempotent (Lab.unlock) but is still a normal command
    that gets a normal reply each time -- unlike e.g. a duplicate packet id,
    resending the word (with a fresh packet id) unlocks nothing new but
    still acks."""
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x33333333, text="SUPERGARDENME", packet_id=9420,
        )
    )
    daemon.drain()
    first_unlocked_at = daemon.conn.execute(
        "SELECT unlocked_at FROM lab_nodes WHERE node_id='!33333333'"
    ).fetchone()["unlocked_at"]

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x33333333, text="supergardenme", packet_id=9421,
        )
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 2
    assert daemon.iface.sent[-1]["text"].startswith("🧪")
    second_unlocked_at = daemon.conn.execute(
        "SELECT unlocked_at FROM lab_nodes WHERE node_id='!33333333'"
    ).fetchone()["unlocked_at"]
    assert second_unlocked_at == first_unlocked_at


def test_lab_unlock_word_is_silent_when_the_lab_tier_is_disabled(tmp_path):
    """service.lab_unlock_word="" (the default) means the LAB tier is off
    entirely: the word text itself must be Command.UNKNOWN, not
    LAB_UNLOCK, even sent verbatim."""
    cfg = replace(_cfg(tmp_path), service=ServiceConfig(lab_unlock_word=""))
    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    try:
        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, text="SUPERGARDENME", packet_id=9430)
        )
        d.drain()
        assert d.iface.sent == []
        assert (
            d.conn.execute("SELECT 1 FROM processed_requests WHERE packet_id=9430").fetchone()
            is None
        )
        assert d.conn.execute("SELECT COUNT(*) FROM lab_nodes").fetchone()[0] == 0
    finally:
        d.stop()


def test_lab_unlock_word_never_appears_as_a_processed_requests_command(daemon):
    """processed_requests.command stays the bounded enum value LAB_UNLOCK --
    never the configured word itself."""
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0x33333333, text="SUPERGARDENME", packet_id=9440,
        )
    )
    daemon.drain()
    row = daemon.conn.execute(
        "SELECT command FROM processed_requests WHERE packet_id=9440"
    ).fetchone()
    assert row["command"] == "LAB_UNLOCK"
    out = generate_latest(daemon._metrics.registry).decode()
    assert "SUPERGARDENME" not in out
    assert 'meshgarden_commands_total{command="LAB_UNLOCK",outcome="accepted"} 1.0' in out


def test_locked_node_menu_has_no_lab_line(daemon):
    """A node that has never unlocked the LAB tier gets the ordinary
    (locked) top-level MENU -- no "LAB" anywhere in it."""
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, from_num=0x55555555, text="MENU", packet_id=9450)
    )
    daemon.drain()
    text = daemon.iface.sent[0]["text"]
    assert "LAB" not in text
    assert text == with_quota_hint(menu_text(lab_unlocked=False), 1, 60, max_bytes=180)


def test_unlocking_the_lab_tier_then_revoking_it_flips_the_menu_lab_line(daemon):
    """Full lifecycle for a never-before-seen node: locked MENU -> DM the
    configured unlock word (mirrors tests/test_lab.py's own unlock/revoke
    coverage, at the daemon/DM level instead of calling Lab directly) ->
    MENU now carries the LAB line -> Lab.revoke -> MENU is locked again."""
    sender = 0x66666666

    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, from_num=sender, text="MENU", packet_id=9460)
    )
    daemon.drain()
    assert "LAB" not in daemon.iface.sent[-1]["text"]

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=sender, text="SUPERGARDENME", packet_id=9461,
        )
    )
    daemon.drain()
    assert daemon.iface.sent[-1]["text"].startswith("🧪")

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, from_num=sender, text="MENU SOCIAL", packet_id=9462)
    )
    daemon.drain()
    text = daemon.iface.sent[-1]["text"]
    assert "MENU LAB" in text
    assert text == commands.menu_reply_for("SOCIAL", lab_unlocked=True)

    # daemon._lab lives on the DB worker thread's own connection; use
    # daemon.conn (safe from this thread, same pattern as
    # conftest.py's _unlock_standard_lab_senders) via a throwaway Lab.
    assert Lab(daemon.conn).revoke("!66666666") is True

    daemon.advance_seconds(daemon.cfg.service.per_node_cooldown_seconds + 1)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, from_num=sender, text="MENU SOCIAL", packet_id=9463)
    )
    daemon.drain()
    text = daemon.iface.sent[-1]["text"]
    assert "LAB" not in text
    assert text == commands.menu_reply_for("SOCIAL", lab_unlocked=False)


def test_me_dm_includes_the_requesters_own_id(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="ME", packet_id=9109))
    daemon.drain()
    assert "!aabbccdd" in daemon.iface.sent[0]["text"]
    assert "mail off" in daemon.iface.sent[0]["text"]


def test_me_dm_signed_count_excludes_board_posts(daemon):
    """Final-review fix (item 8): ME's "Signed N" count describes the main
    guestbook, same as SIGN/GUESTBOOK -- a board post (topic NOT NULL)
    must not inflate it."""
    daemon.conn.execute(
        "INSERT INTO guestbook (created_at, sender_node_id, message, message_hash, "
        "visible, topic) VALUES (0, '!aabbccdd', 'board post', 'h1', 1, 'bike')"
    )
    daemon.conn.commit()
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="ME", packet_id=9111))
    daemon.drain()
    assert "Signed 0" in daemon.iface.sent[0]["text"]


def test_me_dm_reports_mail_on_and_pending_when_the_sender_opted_in(daemon):
    daemon.conn.execute(
        "INSERT INTO mail_prefs (node_id, enabled, updated_at) VALUES ('!aabbccdd', 1, 0)"
    )
    daemon.conn.execute(
        "INSERT INTO mail (created_at, sender_node_id, recipient_node_id, message, "
        "message_hash, hidden) VALUES (0, '!11223344', '!aabbccdd', 'hi', 'h', 0)"
    )
    daemon.conn.commit()
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="ME", packet_id=9110))
    daemon.drain()
    assert "mail on · 1 waiting" in daemon.iface.sent[0]["text"]


def test_me_dm_budgets_a_hostile_long_name_without_losing_identity(daemon):
    """Same finding-2 fix as WHOAMI's, applied to ME's identical "You are"
    opener: the rest of ME's reply (Seen/Hops/Signed/mail lines) must
    survive intact regardless of an unbounded observed short name."""
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0xAABBCCDD, packet_id=9118,
            user={"shortName": "Y" * 200, "longName": "Hostile"}, portnum="NODEINFO_APP",
        )
    )
    daemon.drain()
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="ME", packet_id=9119))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "YYYYY" in reply and "Y" * 200 not in reply
    assert "(!aabbccdd)\n" in reply
    assert "Since" in reply and "hops" in reply and "Signed" in reply and "mail" in reply
    assert b(reply) <= 180


def test_whoami_dm_uses_the_observed_short_name_when_known(daemon):
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0xAABBCCDD, packet_id=9111,
            user={"shortName": "fndr", "longName": "Finder"}, portnum="NODEINFO_APP",
        )
    )
    daemon.drain()
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="WHOAMI", packet_id=9112))
    daemon.drain()
    assert daemon.iface.sent[-1]["text"].startswith("📡 You are fndr (!aabbccdd)")


def test_whoami_dm_budgets_a_hostile_long_name_without_losing_identity(daemon):
    """A long observed name may use available space, but must preserve the
    node ID and core status within the 180-byte reply budget."""
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0xAABBCCDD, packet_id=9116,
            user={"shortName": "X" * 200, "longName": "Hostile"}, portnum="NODEINFO_APP",
        )
    )
    daemon.drain()
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="WHOAMI", packet_id=9117))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "XXXXX" in reply and "X" * 200 not in reply
    assert "(!aabbccdd)\n" in reply
    assert "SNR" in reply and "hops" in reply and "Signed" in reply and "mail off" in reply
    assert b(reply) <= 180


def test_record_dm_shows_the_best_snr_senders_name_when_known(daemon):
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0xAABBCCDD, packet_id=9113,
            user={"shortName": "fndr", "longName": "Finder"}, portnum="NODEINFO_APP",
        )
    )
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, from_num=0xAABBCCDD, packet_id=9114,
            text="just relaying signal", rx_snr=12.0, hop_start=3, hop_limit=3,
        )
    )
    daemon.drain()
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="RECORD", packet_id=9115))
    daemon.drain()
    assert "fndr" in daemon.iface.sent[-1]["text"]


def test_unknown_text_gets_no_reply(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="what is this thing")
    )
    daemon.drain()
    assert daemon.iface.sent == []


def test_duplicate_packet_id_produces_only_one_reply(daemon):
    p = make_packet(to_num=daemon.my_node_num, text="HELP", packet_id=555)
    daemon.iface.emit_packet(p)
    daemon.drain()
    daemon.iface.emit_packet(p)
    daemon.drain()
    assert len(daemon.iface.sent) == 1


def test_per_node_cooldown_explains_the_first_rejected_command(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="HELP", packet_id=1)
    )
    daemon.drain()
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="INFO", packet_id=2)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 2
    assert "then resend your command" in daemon.iface.sent[-1]["text"]


def test_blocked_sender_gets_no_reply(daemon):
    daemon.conn.execute(
        "INSERT INTO blocked_senders(node_id,blocked_at) VALUES ('!aabbccdd',1)"
    )
    daemon.conn.commit()
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="HELP"))
    daemon.drain()
    assert daemon.iface.sent == []


def test_routing_ack_marks_the_request_acked(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="HELP", packet_id=77)
    )
    daemon.drain()
    tx = daemon.conn.execute(
        "SELECT tx_packet_id FROM processed_requests WHERE packet_id=77"
    ).fetchone()[0]
    daemon.iface.emit_packet(
        make_packet(
            from_num=0xAABBCCDD,
            to_num=daemon.my_node_num,
            portnum="ROUTING_APP",
            text=None,
            packet_id=9001,
            request_id=tx,
            routing_error="NONE",
        )
    )
    daemon.drain()
    assert (
        daemon.conn.execute(
            "SELECT state FROM processed_requests WHERE packet_id=77"
        ).fetchone()[0]
        == "acked"
    )


def test_routing_nak_marks_the_request_failed(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="HELP", packet_id=78)
    )
    daemon.drain()
    tx = daemon.conn.execute(
        "SELECT tx_packet_id FROM processed_requests WHERE packet_id=78"
    ).fetchone()[0]
    daemon.iface.emit_packet(
        make_packet(
            from_num=0xAABBCCDD,
            to_num=daemon.my_node_num,
            portnum="ROUTING_APP",
            text=None,
            packet_id=9002,
            request_id=tx,
            routing_error="MAX_RETRANSMIT",
        )
    )
    daemon.drain()
    assert (
        daemon.conn.execute(
            "SELECT state FROM processed_requests WHERE packet_id=78"
        ).fetchone()[0]
        == "failed"
    )


def test_implicit_self_ack_before_remote_ack_still_marks_acked(daemon):
    """Reproduces the 2026-09-13 hardware run: the firmware's implicit
    self-ack (routing NONE from our own node) is delivered before the
    genuine remote ack. The implicit ack must leave the row 'submitted'
    rather than settling it, so the later remote ack still lands as
    'acked' instead of being rejected as an illegal transition out of a
    terminal state."""
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="HELP", packet_id=88)
    )
    daemon.drain()
    tx = daemon.conn.execute(
        "SELECT tx_packet_id FROM processed_requests WHERE packet_id=88"
    ).fetchone()[0]

    # 1) the implicit self-ack, arriving first.
    daemon.iface.emit_packet(
        make_packet(
            from_num=daemon.my_node_num,
            to_num=daemon.my_node_num,
            portnum="ROUTING_APP",
            text=None,
            packet_id=9003,
            request_id=tx,
            routing_error="NONE",
        )
    )
    daemon.drain()
    assert (
        daemon.conn.execute(
            "SELECT state FROM processed_requests WHERE packet_id=88"
        ).fetchone()[0]
        == "submitted"
    )

    # 2) the genuine remote ack, arriving second.
    daemon.iface.emit_packet(
        make_packet(
            from_num=0xAABBCCDD,
            to_num=daemon.my_node_num,
            portnum="ROUTING_APP",
            text=None,
            packet_id=9004,
            request_id=tx,
            routing_error="NONE",
        )
    )
    daemon.drain()
    assert (
        daemon.conn.execute(
            "SELECT state FROM processed_requests WHERE packet_id=88"
        ).fetchone()[0]
        == "acked"
    )


def test_dm_text_is_never_written_to_the_database(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="this is an ordinary private DM")
    )
    daemon.drain()
    dump = "\n".join(daemon.conn.iterdump())
    assert "ordinary private DM" not in dump


def test_daemon_never_sends_on_startup(daemon):
    daemon.drain()
    assert daemon.iface.sent == []


def test_malformed_packets_do_not_kill_the_daemon(daemon):
    daemon.iface.emit_packet({"nonsense": 1})
    daemon.iface.emit_packet({"from": None, "to": None})
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="HELP"))
    daemon.drain()
    assert len(daemon.iface.sent) == 1


def test_malformed_packets_increment_the_malformed_packets_counter(daemon):
    daemon.iface.emit_packet({"nonsense": 1})
    daemon.iface.emit_packet({"from": None, "to": None})
    out = generate_latest(daemon._metrics.registry).decode()
    assert "meshgarden_malformed_packets_total 2.0" in out


def test_reconnect_increments_the_reconnects_counter(daemon):
    out = generate_latest(daemon._metrics.registry).decode()
    assert "meshgarden_reconnects_total 0.0" in out

    daemon.iface.emit_lost()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not daemon._radio.status().connected:
        time.sleep(0.02)
    assert daemon._radio.status().connected is True  # reconnected, did not hang

    out = generate_latest(daemon._metrics.registry).decode()
    assert "meshgarden_reconnects_total 1.0" in out


def test_missing_snr_and_rssi_are_never_observed_as_zero(daemon):
    """Task 6 review note: a packet with absent rx_snr/rx_rssi must never be
    recorded as a 0 reading in the histograms -- it must simply not be
    observed at all."""
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="HELP", rx_snr=None, rx_rssi=None)
    )
    daemon.drain()
    out = generate_latest(daemon._metrics.registry).decode()
    assert "meshgarden_rx_snr_db_count 0.0" in out
    assert "meshgarden_rx_rssi_dbm_count 0.0" in out


def test_daemon_uses_two_distinct_single_threaded_connections(daemon):
    """Ruling (fix round 1): every sqlite3.Connection is used by exactly one
    thread. The DB worker's connection and the TX worker's connection must
    be two distinct objects, each created by (and therefore only usable
    from, per check_same_thread's default) its own worker thread -- neither
    is the main/test thread's own `daemon.conn`."""
    assert daemon._db_conn is not daemon._tx_conn
    assert daemon._db_conn is not daemon.conn
    assert daemon._tx_conn is not daemon.conn
    assert daemon._db_conn_owner_ident == daemon._db_thread.ident
    assert daemon._tx_conn_owner_ident == daemon._tx_thread.ident
    assert daemon._db_conn_owner_ident != daemon._tx_conn_owner_ident
    assert daemon._db_conn_owner_ident != threading.get_ident()
    assert daemon._tx_conn_owner_ident != threading.get_ident()

    # Exercise a full command + routing-ack round trip end to end: this
    # touches the DB worker's connection (collector/request_log claim/
    # note_routing_packet) and the TX worker's connection (pump()'s state
    # transitions) for real, not just at construction, with no cross-thread
    # ProgrammingError silently swallowed by a worker's broad exception
    # handler (which would otherwise leave the row stuck and this assertion
    # failing).
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="HELP", packet_id=4711)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    row = daemon.conn.execute(
        "SELECT state, tx_packet_id FROM processed_requests WHERE packet_id=4711"
    ).fetchone()
    assert row["state"] == "submitted"
    tx = row["tx_packet_id"]

    daemon.iface.emit_packet(
        make_packet(
            from_num=0xAABBCCDD,
            to_num=daemon.my_node_num,
            portnum="ROUTING_APP",
            text=None,
            packet_id=9999,
            request_id=tx,
            routing_error="NONE",
        )
    )
    daemon.drain()
    assert (
        daemon.conn.execute(
            "SELECT state FROM processed_requests WHERE packet_id=4711"
        ).fetchone()["state"]
        == "acked"
    )

    out = generate_latest(daemon._metrics.registry).decode()
    assert "meshgarden_database_errors_total 0.0" in out


def test_stop_returns_promptly_when_db_worker_thread_has_already_exited(tmp_path, monkeypatch):
    """Ruling: stop() must not hang forever on a blocking put() of the stop
    sentinel when the DB worker thread has already died (e.g. an unexpected
    exception escaping its loop) and the ingest queue is full."""
    from meshgarden import main as main_module

    monkeypatch.setattr(main_module, "_INGEST_QUEUE_MAXSIZE", 1)
    cfg = Config(
        radio=RadioConfig(device="/dev/fake", expected_node_id="!11223344"),
        service=ServiceConfig(),
        database=DatabaseConfig(path=str(tmp_path / "o.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "backups")),
        metrics=MetricsConfig(),
    )
    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)

    # Simulate the DB worker thread having already died: replace its loop
    # with a no-op so the thread exits immediately after signalling ready,
    # never consuming anything from the ingest queue (and never seeing the
    # stop sentinel).
    monkeypatch.setattr(d, "_db_worker_loop", lambda: None)
    d.start()
    d._db_thread.join(timeout=2)
    assert not d._db_thread.is_alive()

    # Fill the now-unconsumed, maxsize=1 ingest queue so a plain blocking
    # put(_STOP_SENTINEL) would hang forever.
    d._ingest.put_nowait(object())

    start = time.monotonic()
    d.stop()
    elapsed = time.monotonic() - start
    assert elapsed < 2.0


class _FrozenDateTime(datetime):
    """A datetime subclass whose .now() returns a fixed, settable moment.

    retention.rollup_day's `finalized` decision compares against the REAL
    wall clock (`datetime.now(timezone.utc)`; Retention has no injected
    clock), which never itself advances meaningfully during a fast test --
    so a naive test calling rollup_day twice with only a fake `now` fed
    into _run_maintenance cannot actually distinguish "fixed" from
    "unfixed": whichever tick a day is rolled up on, `finalized` is
    computed from the same (real) "now" either way. Monkeypatching
    retention.datetime to this class is what lets a test genuinely move
    "real now" across a UTC midnight between two ticks.
    """

    _frozen: "datetime | None" = None

    @classmethod
    def now(cls, tz=None):
        return cls._frozen


def test_maintenance_finalizes_yesterday_when_the_utc_day_changes(tmp_path, monkeypatch):
    """Residual fix: production only ever calls rollup_day(today), so
    daily_stats.finalized was permanently 0. _run_maintenance must also
    roll up yesterday (UTC) once the UTC day has changed since the last
    tick -- or on the very first tick (self._last_rollup_day is still
    None), covering a restart shortly after midnight."""
    from meshgarden import retention as retention_module

    monkeypatch.setattr(retention_module, "datetime", _FrozenDateTime)

    d, conn = _bare_daemon_for_maintenance(tmp_path)

    day1 = "2024-01-14"
    day1_ts = int(datetime(2024, 1, 14, tzinfo=UTC).timestamp())
    day2 = "2024-01-15"
    day2_ts = day1_ts + 86400

    _FrozenDateTime._frozen = datetime(2024, 1, 14, 1, 0, tzinfo=UTC)  # "real now": still day1
    d._run_maintenance(now=day1_ts + 3600)  # first tick ever

    # "real now" has crossed into day2:
    _FrozenDateTime._frozen = datetime(2024, 1, 15, 0, 1, tzinfo=UTC)
    d._run_maintenance(now=day2_ts + 60)  # the UTC day changed

    row1 = conn.execute("SELECT finalized FROM daily_stats WHERE day=?", (day1,)).fetchone()
    row2 = conn.execute("SELECT finalized FROM daily_stats WHERE day=?", (day2,)).fetchone()
    assert row1["finalized"] == 1  # yesterday, re-rolled-up once real "now" passed midnight
    assert row2["finalized"] == 0  # today, as of that same tick: not yet finalized
    conn.close()


def test_maintenance_does_not_reroll_yesterday_within_the_same_utc_day(tmp_path):
    """Bounded: at most one extra rollup_day call per tick, and only when
    the day actually changes -- not on every tick within the same day."""
    d, conn = _bare_daemon_for_maintenance(tmp_path)
    day1 = "2024-01-14"
    day1_ts = int(datetime(2024, 1, 14, tzinfo=UTC).timestamp())

    calls = []
    original_rollup = d._retention.rollup_day

    def counting_rollup(day):
        calls.append(day)
        return original_rollup(day)

    d._retention.rollup_day = counting_rollup
    d._run_maintenance(now=day1_ts + 100)  # first tick: rolls up yesterday + today
    assert calls == ["2024-01-13", day1]

    calls.clear()
    d._run_maintenance(now=day1_ts + 200)  # still day1: today only, no extra call
    assert calls == [day1]
    conn.close()


def test_maintenance_prunes_stale_node_daily_quota_rows(tmp_path):
    """Wiring check: _run_maintenance calls DailyQuota.prune (already
    exhaustively unit-tested in tests/test_quota.py) alongside every other
    prune_* call."""
    d, conn = _bare_daemon_for_maintenance(tmp_path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO node_daily_quota (node_id, day, commands, signs, updated_at)"
        " VALUES ('!aaaaaaaa', '2020-01-01', 1, 0, ?)",
        (now - 3 * 86400,),
    )
    conn.commit()
    d._run_maintenance(now=now)
    remaining = conn.execute("SELECT count(*) FROM node_daily_quota").fetchone()[0]
    assert remaining == 0
    conn.close()


def test_restart_does_not_lower_a_finalized_days_counters(tmp_path):
    """Regression test: _run_maintenance used to prune expired
    processed_requests rows and THEN recompute yesterday's daily_stats from
    whatever remained -- so once the acked row backing a finalized day's
    accepted/acked counts expired and got pruned, the very next maintenance
    tick after a restart (which resets _last_rollup_day to None, and so
    re-rolls-up yesterday one more time) silently dropped those already
    -finalized counters from 1 back down to 0."""
    day1 = "2024-01-14"
    day1_ts = int(datetime(2024, 1, 14, tzinfo=UTC).timestamp())
    day2_ts = day1_ts + 86400  # "2024-01-15" 00:00 UTC

    d1, conn1 = _bare_daemon_for_maintenance(tmp_path)
    first_seen_at = day1_ts + 100
    expires_at = day2_ts + 100  # 24h dedup window past first_seen_at
    conn1.execute(
        "INSERT INTO processed_requests (sender_node_id, packet_id, first_seen_at, "
        "expires_at, command, state, reason, entry_id, tx_packet_id, updated_at) "
        "VALUES ('!aaaaaaaa', 1, ?, ?, 'MENU', 'acked', NULL, NULL, NULL, ?)",
        (first_seen_at, expires_at, first_seen_at),
    )
    conn1.commit()

    # Tick 1 (before the row expires, first tick ever for this daemon
    # instance): finalizes day1 correctly with accepted=1, acked=1.
    d1._run_maintenance(now=day2_ts + 50)
    row = conn1.execute(
        "SELECT commands_accepted, replies_acked, finalized FROM daily_stats WHERE day=?",
        (day1,),
    ).fetchone()
    assert (row["commands_accepted"], row["replies_acked"], row["finalized"]) == (1, 1, 1)
    conn1.close()

    # Simulate a restart: a fresh Daemon/connection, _last_rollup_day reset
    # to None -- which alone (regardless of whether the UTC day has
    # actually changed again) re-triggers a "yesterday" rollup this tick.
    d2, conn2 = _bare_daemon_for_maintenance(tmp_path)
    # Now past expires_at: the acked row is prunable.
    d2._run_maintenance(now=day2_ts + 150)

    row = conn2.execute(
        "SELECT commands_accepted, replies_acked, finalized FROM daily_stats WHERE day=?",
        (day1,),
    ).fetchone()
    assert (row["commands_accepted"], row["replies_acked"]) == (1, 1), (
        "a restart must never lower a previously-finalized day's counters"
    )
    conn2.close()


def test_restart_within_reply_ttl_does_not_leave_a_received_row_stuck_forever(tmp_path):
    """Regression test for the reported bug: `resolve_uncertain_on_startup`
    used to only settle 'received' rows OLDER than reply_ttl_seconds. A
    restart shortly after a claim (well within the TTL) left that row
    'received' forever -- neither this startup call (too young to meet
    the old age check) nor maintenance (only 'submitted' was aged; a
    'received' row was never touched) nor retention pruning (terminal
    states only) would ever revisit it.

    Reproduces the exact scenario from the bug report: claim, restart the
    daemon (a new `Daemon` on the same DB) within the TTL. A restart means
    no in-memory work survives for that claim, so the row must be settled
    (to 'unknown' -- never resent) at THIS startup, regardless of age.
    """
    cfg = _cfg(tmp_path)
    iface = FakeInterface(0x11223344)

    d1 = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    now = int(time.time())
    commands.RequestLog(d1.conn, dedup_hours=cfg.service.request_dedup_hours).claim(
        "!aabbccdd", 1, now, commands.Command.MENU
    )
    d1.conn.close()

    # "Restart... within the TTL": constructing a new Daemon on the same DB
    # path re-runs startup recovery. Real elapsed wall-clock time between
    # the claim above and here is a tiny fraction of a second -- nowhere
    # near reply_ttl_seconds (60s default) -- exactly the "10s after a
    # claim" scenario from the report.
    d2 = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    state_after_startup = commands.RequestLog(
        d2.conn, dedup_hours=cfg.service.request_dedup_hours
    ).get_state("!aabbccdd", 1)
    d2.conn.close()

    assert state_after_startup == "unknown", (
        "a 'received' row must be settled at startup regardless of age -- "
        "no in-memory work survives a restart to ever move it further"
    )


def test_maintenance_ages_and_prunes_a_stray_received_row_as_a_safety_net(tmp_path):
    """Even without a restart, a 'received' row must not sit forever if it
    somehow never gets settled by the normal in-process paths (moving
    forward in the same tick, or `Daemon._settle_failed_request` after a
    caught exception) -- e.g. the process died in a way that skipped both.
    Maintenance must age any 'received' row older than reply_ttl_seconds to
    a terminal state ('unknown'), same as it already does for 'submitted'
    via `age_stale_submitted`, so retention can eventually prune it. Time
    is advanced past both the reply TTL and the dedup window in one tick,
    matching the bug report's reproduction steps."""
    d, conn = _bare_daemon_for_maintenance(tmp_path)
    log = commands.RequestLog(conn, dedup_hours=d.cfg.service.request_dedup_hours)
    t0 = 1_700_000_000
    log.claim("!aabbccdd", 1, t0, commands.Command.MENU)
    assert log.get_state("!aabbccdd", 1) == "received"

    past_ttl_and_dedup = (
        t0 + d.cfg.service.reply_ttl_seconds + d.cfg.service.request_dedup_hours * 3600 + 10
    )
    d._run_maintenance(now=past_ttl_and_dedup)

    # Aged to a terminal state and (since the dedup/expiry window has also
    # passed) pruned away in the same tick -- never resent, never stuck.
    assert log.get_state("!aabbccdd", 1) is None
    conn.close()


def test_daemon_creates_its_state_directory_owner_only_regardless_of_umask(tmp_path):
    """Residual fix: _configure_logging's os.makedirs (no explicit mode)
    used to create <db_dir> first, under the ambient umask, before
    database.connect() ever got a chance to see it as new and chmod it --
    so the state directory ended up e.g. 0755 under a typical umask 022."""
    nested = tmp_path / "fresh_state_dir"
    cfg = Config(
        radio=RadioConfig(device="/dev/fake", expected_node_id="!11223344"),
        service=ServiceConfig(),
        database=DatabaseConfig(path=str(nested / "o.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(nested / "backups")),
        metrics=MetricsConfig(),
    )
    assert not nested.exists()
    old_umask = os.umask(0o022)
    try:
        iface = FakeInterface(0x11223344)
        d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    finally:
        os.umask(old_umask)
    try:
        assert oct(os.stat(nested).st_mode & 0o777) == oct(0o700)
    finally:
        d.stop()


def test_configure_logging_creates_its_directory_owner_only_regardless_of_umask(
    tmp_path, monkeypatch
):
    from meshgarden import main as main_module

    monkeypatch.setattr(main_module, "_logging_configured", False)
    log_dir = tmp_path / "fresh_logs_dir"
    assert not log_dir.exists()
    old_umask = os.umask(0o022)
    try:
        main_module._configure_logging(str(log_dir))
    finally:
        os.umask(old_umask)
    assert oct(os.stat(log_dir).st_mode & 0o777) == oct(0o700)


def test_nodeinfo_packet_updates_node_display_names(daemon):
    """Ruling (fix round 1, #3): a NODEINFO packet through the fake updates
    nodes.short_name/long_name; a text packet leaves them untouched."""
    daemon.iface.emit_packet(
        make_packet(
            from_num=0xAABBCCDD,
            to_num=0xFFFFFFFF,
            portnum="NODEINFO_APP",
            text=None,
            packet_id=5001,
            user={"shortName": "Ali", "longName": "Alice's Node"},
        )
    )
    daemon.drain()
    row = daemon.conn.execute(
        "SELECT short_name, long_name FROM nodes WHERE node_id='!aabbccdd'"
    ).fetchone()
    assert row["short_name"] == "Ali"
    assert row["long_name"] == "Alice's Node"


def test_ordinary_text_packet_never_touches_node_names(daemon):
    daemon.iface.emit_packet(
        make_packet(from_num=0xAABBCCDD, to_num=0xFFFFFFFF, text="random chatter", packet_id=5002)
    )
    daemon.drain()
    row = daemon.conn.execute(
        "SELECT short_name, long_name FROM nodes WHERE node_id='!aabbccdd'"
    ).fetchone()
    assert row["short_name"] is None
    assert row["long_name"] is None


# =============================================================================
# worker heartbeats (Heartbeats registry)
# =============================================================================


class _FakeClock:
    """A trivial monotonic-style clock a test can advance without sleeping,
    for Heartbeats unit tests that don't need a full Daemon."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_heartbeats_starts_fresh_and_not_stalled():
    clock = _FakeClock()
    hb = main.Heartbeats({"db": 30.0, "tx": 30.0}, clock=clock)
    assert hb.snapshot()["db"] == {"age_seconds": 0.0, "alive": True, "stalled": False}
    assert hb.any_stalled() is False


def test_heartbeats_beat_resets_the_age():
    clock = _FakeClock()
    hb = main.Heartbeats({"db": 30.0}, clock=clock)
    clock.advance(10)
    hb.beat("db")
    clock.advance(5)
    snap = hb.snapshot()
    assert snap["db"]["age_seconds"] == 5.0
    assert snap["db"]["stalled"] is False


def test_heartbeats_flags_stalled_once_age_exceeds_its_own_threshold():
    clock = _FakeClock()
    hb = main.Heartbeats({"db": 30.0}, clock=clock)
    hb.beat("db")
    clock.advance(30.0)
    assert hb.snapshot()["db"]["stalled"] is False  # exactly at threshold: not yet stalled
    clock.advance(0.001)
    snap = hb.snapshot()
    assert snap["db"]["stalled"] is True
    assert snap["db"]["alive"] is False
    assert hb.any_stalled() is True


def test_heartbeats_any_stalled_is_true_if_even_one_worker_is_stalled():
    clock = _FakeClock()
    hb = main.Heartbeats({"db": 30.0, "tx": 30.0}, clock=clock)
    hb.beat("db")
    hb.beat("tx")
    clock.advance(31.0)
    hb.beat("db")  # db recovers/keeps beating; tx does not
    assert hb.any_stalled() is True


def test_heartbeats_thresholds_are_per_worker():
    clock = _FakeClock()
    hb = main.Heartbeats({"db": 30.0, "radio": 300.0}, clock=clock)
    hb.beat("db")
    hb.beat("radio")
    clock.advance(60.0)
    snap = hb.snapshot()
    assert snap["db"]["stalled"] is True
    assert snap["radio"]["stalled"] is False


def test_daemon_worker_stall_thresholds_match_spec(tmp_path):
    """db/tx: 30s. radio: max(backoff cap, 60s) -- RadioAdapter's own
    default backoff_max is 300s, so this is 300s absent a smaller
    configured cap. maintenance: 3x the maintenance tick interval (60s),
    i.e. 180s."""
    iface = FakeInterface(0x11223344)
    d = Daemon(_cfg(tmp_path), interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    try:
        assert d._heartbeats._thresholds["db"] == 30.0
        assert d._heartbeats._thresholds["tx"] == 30.0
        assert d._heartbeats._thresholds["radio"] == 300.0
        assert d._heartbeats._thresholds["maintenance"] == 180.0
    finally:
        d.stop()


def test_status_snapshot_includes_worker_health(daemon):
    daemon.drain()
    workers = daemon.status_snapshot()["workers"]
    assert set(workers) == {"db", "tx", "radio", "maintenance"}
    for name, info in workers.items():
        assert info["stalled"] is False, name
        assert info["alive"] is True, name
        assert isinstance(info["age_seconds"], float)


def test_write_status_file_persists_worker_health(daemon, tmp_path):
    daemon.drain()
    daemon.write_status_file()
    status_path = os.path.join(os.path.dirname(daemon.cfg.database.path), "status.json")
    with open(status_path, encoding="utf-8") as f:
        payload = json.load(f)
    assert set(payload["workers"]) == {"db", "tx", "radio", "maintenance"}
    assert all(not info["stalled"] for info in payload["workers"].values())


def test_status_snapshot_reports_a_stalled_worker_once_its_heartbeat_ages_past_threshold(daemon):
    """Simulates a stalled worker without touching any real thread: freezes
    the "db" worker's heartbeat by monkeypatching Heartbeats.beat to a
    no-op, then advances the daemon's own injected clock (the same clock
    Heartbeats reads) past the 30s db/tx threshold."""
    daemon.drain()
    original_beat = daemon._heartbeats.beat
    daemon._heartbeats.beat = lambda name: (None if name == "db" else original_beat(name))
    daemon.advance_seconds(31)
    time.sleep(0.05)  # let the real worker threads observe the frozen callable
    workers = daemon.status_snapshot()["workers"]
    assert workers["db"]["stalled"] is True
    assert workers["db"]["alive"] is False


def test_report_worker_health_logs_a_bounded_warning_once_per_stall_transition(caplog):
    d = Daemon.__new__(Daemon)
    d._metrics = Metrics()
    d._worker_stall_state = {}
    with caplog.at_level(logging.INFO, logger="meshgarden.main"):
        d._report_worker_health({"db": {"age_seconds": 1.0, "alive": True, "stalled": False}})
        d._report_worker_health({"db": {"age_seconds": 40.0, "alive": False, "stalled": True}})
        d._report_worker_health({"db": {"age_seconds": 41.0, "alive": False, "stalled": True}})
        d._report_worker_health({"db": {"age_seconds": 42.0, "alive": False, "stalled": True}})
    stall_warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "stalled" in r.message
    ]
    assert len(stall_warnings) == 1  # never once per tick -- only on the transition


def test_report_worker_health_logs_info_on_recovery(caplog):
    d = Daemon.__new__(Daemon)
    d._metrics = Metrics()
    d._worker_stall_state = {}
    with caplog.at_level(logging.INFO, logger="meshgarden.main"):
        d._report_worker_health({"db": {"age_seconds": 40.0, "alive": False, "stalled": True}})
        d._report_worker_health({"db": {"age_seconds": 1.0, "alive": True, "stalled": False}})
    recoveries = [r for r in caplog.records if "recovered" in r.message]
    assert len(recoveries) == 1


def test_report_worker_health_updates_the_heartbeat_age_gauge():
    d = Daemon.__new__(Daemon)
    d._metrics = Metrics()
    d._worker_stall_state = {}
    d._report_worker_health({"db": {"age_seconds": 12.5, "alive": True, "stalled": False}})
    out = generate_latest(d._metrics.registry).decode()
    assert 'meshgarden_worker_heartbeat_age_seconds{worker="db"} 12.5' in out


# =============================================================================
# local dashboard startup guard: a misconfigured [web] must never abort the
# daemon -- see web_status in status_snapshot()/the status file/`mgarden
# status`.
# =============================================================================


def test_web_disabled_by_default_reports_web_status_disabled(daemon):
    assert daemon.status_snapshot()["web_status"] == "disabled"
    assert daemon._web_httpd is None


def test_non_loopback_listen_without_allow_lan_is_refused_but_daemon_still_runs(tmp_path):
    cfg = replace(
        _cfg(tmp_path),
        web=WebConfig(enabled=True, listen="0.0.0.0", port=0, allow_lan=False),
    )
    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    assert d._web_httpd is None
    try:
        d.start()
        assert d._db_thread.is_alive()
        assert d._tx_thread.is_alive()
        assert d.status_snapshot()["web_status"] == "refused"
    finally:
        d.stop()


def test_port_already_bound_is_reported_as_failed_but_daemon_still_runs(tmp_path):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        cfg = replace(
            _cfg(tmp_path),
            web=WebConfig(enabled=True, listen="127.0.0.1", port=port, allow_lan=False),
        )
        iface = FakeInterface(0x11223344)
        d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
        assert d._web_httpd is None
        try:
            d.start()
            assert d._db_thread.is_alive()
            assert d.status_snapshot()["web_status"] == "failed"
        finally:
            d.stop()
    finally:
        blocker.close()


def test_web_enabled_and_bindable_reports_running(tmp_path):
    cfg = replace(
        _cfg(tmp_path),
        web=WebConfig(enabled=True, listen="127.0.0.1", port=0, allow_lan=False),
    )
    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    assert d._web_httpd is not None
    try:
        d.start()
        assert d.status_snapshot()["web_status"] == "running"
    finally:
        d.stop()


# =============================================================================
# mgarden status: one line per worker
# =============================================================================


def test_cmd_status_text_output_prints_one_line_per_worker(daemon, capsys):
    daemon.drain()
    daemon.write_status_file()
    from meshgarden.main import cmd_status

    assert cmd_status(daemon.cfg, as_json=False) == 0
    out = capsys.readouterr().out
    for name in ("db", "tx", "radio", "maintenance"):
        assert f"worker {name}: age=" in out


def test_cmd_status_json_includes_workers(daemon, capsys):
    daemon.drain()
    daemon.write_status_file()
    from meshgarden.main import cmd_status

    assert cmd_status(daemon.cfg, as_json=True) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data["workers"]) == {"db", "tx", "radio", "maintenance"}


def test_cmd_status_json_includes_web_status(daemon, capsys):
    daemon.drain()
    daemon.write_status_file()
    from meshgarden.main import cmd_status

    assert cmd_status(daemon.cfg, as_json=True) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["web_status"] == "disabled"


# =============================================================================
# digest scheduling hook: _on_local_midnight / _midnight_tasks
# =============================================================================


def test_on_local_midnight_fires_exactly_once_per_day_change(tmp_path):
    d, conn = _bare_daemon_for_maintenance(tmp_path)
    calls = []
    d._midnight_tasks.append(lambda prev_day: calls.append(prev_day))

    day1_ts = int(datetime(2024, 1, 14, tzinfo=UTC).timestamp())
    day2_ts = day1_ts + 86400
    day3_ts = day2_ts + 86400

    d._run_maintenance(now=day1_ts + 100)  # first tick ever: no prior day, no hook fire
    assert calls == []

    d._run_maintenance(now=day1_ts + 200)  # same UTC day: no fire
    assert calls == []

    d._run_maintenance(now=day2_ts + 50)  # day changed: fires once, with the finalized day
    assert calls == ["2024-01-14"]

    d._run_maintenance(now=day2_ts + 60)  # still day2: no additional fire
    assert calls == ["2024-01-14"]

    d._run_maintenance(now=day3_ts + 50)  # day changed again: fires once more
    assert calls == ["2024-01-14", "2024-01-15"]
    conn.close()


def test_midnight_tasks_seam_starts_empty(tmp_path):
    d, conn = _bare_daemon_for_maintenance(tmp_path)
    assert d._midnight_tasks == []
    conn.close()


def test_a_broken_midnight_task_never_blocks_another_or_the_maintenance_tick(tmp_path, caplog):
    d, conn = _bare_daemon_for_maintenance(tmp_path)
    calls = []
    d._midnight_tasks.append(lambda prev_day: (_ for _ in ()).throw(RuntimeError("boom")))
    d._midnight_tasks.append(lambda prev_day: calls.append(prev_day))

    day1_ts = int(datetime(2024, 1, 14, tzinfo=UTC).timestamp())
    day2_ts = day1_ts + 86400
    d._run_maintenance(now=day1_ts + 100)
    with caplog.at_level(logging.ERROR, logger="meshgarden.main"):
        d._run_maintenance(now=day2_ts + 50)
    assert calls == ["2024-01-14"]  # the second task still ran
    conn.close()


# =============================================================================
# daily digest midnight hook-up: _digest_midnight_task / startup catch-up
# =============================================================================


def _insert_observation(conn, ts, sender="!aaaaaaaa", pid=1):
    conn.execute(
        "INSERT INTO observations(server_time,sender_node_id,dest_node_id,channel,"
        "portnum,packet_id,is_duplicate) VALUES (?,?,'^all',0,'TEXT_MESSAGE_APP',?,0)",
        (ts, sender, pid),
    )
    conn.commit()


def test_digest_midnight_task_writes_yesterdays_digest_on_day_change(tmp_path):
    """_digest_midnight_task now fires on its own (`_last_digest_local_day`
    tracking in `_run_maintenance`), not via `_midnight_tasks`."""
    d, conn = _bare_daemon_for_maintenance(tmp_path)

    day1_ts = int(datetime(2024, 1, 14, tzinfo=UTC).timestamp())
    day2_ts = day1_ts + 86400
    _insert_observation(conn, day1_ts + 3600, sender="!aabbccdd")

    d._run_maintenance(now=day1_ts + 100)  # first tick: no prior day, no digest yet
    d._run_maintenance(now=day2_ts + 50)  # day changed: writes yesterday's (day1) digest

    dest = os.path.join(tmp_path, "digest", "2024-01-14.json")
    assert os.path.exists(dest)
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600
    with open(dest, encoding="utf-8") as f:
        blob = f.read()
    assert "!aabbccdd" not in blob  # no node id
    payload = json.loads(blob)
    assert payload["packets"]["raw"] == 1
    conn.close()


def test_digest_midnight_task_is_not_rewritten_on_later_ticks_the_same_day(tmp_path):
    d, conn = _bare_daemon_for_maintenance(tmp_path)

    day1_ts = int(datetime(2024, 1, 14, tzinfo=UTC).timestamp())
    day2_ts = day1_ts + 86400
    _insert_observation(conn, day1_ts + 3600)

    d._run_maintenance(now=day1_ts + 100)
    d._run_maintenance(now=day2_ts + 50)  # writes day1's digest
    dest = os.path.join(tmp_path, "digest", "2024-01-14.json")
    first_mtime = os.stat(dest).st_mtime_ns

    d._run_maintenance(now=day2_ts + 60)  # still day2: no day change, no rewrite
    d._run_maintenance(now=day2_ts + 3600)
    assert os.stat(dest).st_mtime_ns == first_mtime
    conn.close()


def test_digest_midnight_task_swallows_and_logs_a_failure(tmp_path, monkeypatch, caplog):
    d, conn = _bare_daemon_for_maintenance(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(digest, "build_digest", _boom)

    day1_ts = int(datetime(2024, 1, 14, tzinfo=UTC).timestamp())
    day2_ts = day1_ts + 86400
    d._run_maintenance(now=day1_ts + 100)
    with caplog.at_level(logging.ERROR, logger="meshgarden.main"):
        d._run_maintenance(now=day2_ts + 50)  # must not raise
    assert not os.path.exists(os.path.join(tmp_path, "digest", "2024-01-14.json"))
    conn.close()


def test_maintenance_tick_prunes_digest_files_older_than_90_days(tmp_path):
    d, conn = _bare_daemon_for_maintenance(tmp_path)

    digest_dir = os.path.join(tmp_path, "digest")
    os.makedirs(digest_dir, exist_ok=True)
    old_path = os.path.join(digest_dir, "2020-01-01.json")
    recent_path = os.path.join(digest_dir, "2024-01-13.json")
    for p in (old_path, recent_path):
        with open(p, "w", encoding="utf-8") as f:
            json.dump({}, f)

    day1_ts = int(datetime(2024, 1, 14, tzinfo=UTC).timestamp())
    d._run_maintenance(now=day1_ts + 100)

    assert not os.path.exists(old_path)
    assert os.path.exists(recent_path)
    conn.close()


def test_digest_is_scheduled_on_the_configured_timezones_day_not_utc(tmp_path):
    """Finding-3 regression: the digest used to be scheduled off the UTC
    day (`_last_rollup_day`, via `_on_local_midnight`) even though its own
    queries (`build_digest`/`_local_day_window`) are entirely in the
    configured timezone's day. For America/Los_Angeles (UTC-7 in
    September), UTC midnight lands at 17:00 local -- 7 hours before local
    midnight actually arrives -- so the old code wrote (and permanently
    under-counted) Sept 14's digest hours early, missing every packet from
    17:00 to midnight local."""
    tz = "America/Los_Angeles"
    d, conn = _bare_daemon_for_maintenance(tmp_path)
    d.cfg = replace(d.cfg, service=replace(d.cfg.service, timezone=tz))

    local_sep14_10am = int(datetime(2026, 9, 14, 10, 0, tzinfo=ZoneInfo(tz)).timestamp())
    local_sep14_8pm = int(datetime(2026, 9, 14, 20, 0, tzinfo=ZoneInfo(tz)).timestamp())
    # UTC midnight is 17:00 on the 14th, local -- well before local midnight.
    just_after_utc_midnight = int(datetime(2026, 9, 15, 0, 30, tzinfo=UTC).timestamp())
    still_before_local_midnight = int(
        datetime(2026, 9, 14, 22, 0, tzinfo=ZoneInfo(tz)).timestamp()
    )
    after_local_midnight = int(datetime(2026, 9, 15, 1, 0, tzinfo=ZoneInfo(tz)).timestamp())

    _insert_observation(conn, local_sep14_10am, sender="!aabbccdd", pid=1)
    d._run_maintenance(now=local_sep14_10am)  # first tick ever: establishes baseline

    d._run_maintenance(now=just_after_utc_midnight)  # UTC day changed; local day has not
    dest = os.path.join(tmp_path, "digest", "2026-09-14.json")
    assert not os.path.exists(dest)  # must NOT fire before local midnight

    _insert_observation(conn, local_sep14_8pm, sender="!99999999", pid=2)
    d._run_maintenance(now=still_before_local_midnight)  # still the 14th, locally
    assert not os.path.exists(dest)

    d._run_maintenance(now=after_local_midnight)  # local day has genuinely changed
    assert os.path.exists(dest)
    with open(dest, encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["packets"]["raw"] == 2  # both packets counted
    conn.close()


def test_daemon_starts_with_the_digest_scheduled_by_local_day_not_midnight_tasks(tmp_path):
    """Finding-3 fix: the digest used to be registered in `_midnight_tasks`
    (fired on the UTC day changing) -- it now fires on its own from
    `_last_digest_local_day`, so the generic seam stays genuinely unused
    by default."""
    cfg = _cfg(tmp_path)
    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    assert d._midnight_tasks == []
    assert d._last_digest_local_day is None


def test_daemon_writes_startup_catchup_digest_when_missing_and_yesterday_has_data(tmp_path):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.database.path)
    migrate(conn)
    tz = cfg.service.timezone
    yesterday = digest.default_digest_day(int(time.time()), tz)
    yesterday_ts = int(
        datetime.strptime(yesterday, "%Y-%m-%d").replace(hour=12, tzinfo=UTC).timestamp()
    )
    _insert_observation(conn, yesterday_ts, sender="!aabbccdd")
    conn.close()

    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    try:
        dest = os.path.join(tmp_path, "digest", f"{yesterday}.json")
        assert os.path.exists(dest)
        assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600
        with open(dest, encoding="utf-8") as f:
            blob = f.read()
        assert "!aabbccdd" not in blob
    finally:
        d.stop()


def test_daemon_skips_startup_catchup_digest_when_yesterday_has_no_data(tmp_path):
    cfg = _cfg(tmp_path)
    tz = cfg.service.timezone
    yesterday = digest.default_digest_day(int(time.time()), tz)

    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    try:
        dest = os.path.join(tmp_path, "digest", f"{yesterday}.json")
        assert not os.path.exists(dest)
    finally:
        d.stop()


def test_daemon_does_not_overwrite_an_existing_yesterday_digest_at_startup(tmp_path):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.database.path)
    migrate(conn)
    tz = cfg.service.timezone
    yesterday = digest.default_digest_day(int(time.time()), tz)
    yesterday_ts = int(
        datetime.strptime(yesterday, "%Y-%m-%d").replace(hour=12, tzinfo=UTC).timestamp()
    )
    _insert_observation(conn, yesterday_ts)
    conn.close()

    digest_dir = os.path.join(tmp_path, "digest")
    os.makedirs(digest_dir, exist_ok=True)
    dest = os.path.join(digest_dir, f"{yesterday}.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump({"sentinel": "pre-existing"}, f)
    os.chmod(dest, 0o600)
    before_mtime = os.stat(dest).st_mtime_ns

    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    try:
        assert os.stat(dest).st_mtime_ns == before_mtime
        with open(dest, encoding="utf-8") as f:
            assert json.load(f) == {"sentinel": "pre-existing"}
    finally:
        d.stop()


# =============================================================================
# systemd watchdog: _sd_notify / _watchdog_interval_seconds / _watchdog_loop
# =============================================================================


def _short_unix_socket_dir():
    """AF_UNIX socket paths are limited to ~104-108 bytes (sun_path) --
    pytest's own `tmp_path` (nested several directories deep, especially
    under macOS's default TMPDIR) routinely exceeds that. A short-lived
    directory made directly under the system temp root keeps the full
    path well within budget."""
    return tempfile.mkdtemp()


def _unix_socket_or_skip(sock_path):
    """Binds an `AF_UNIX SOCK_DGRAM` socket at `sock_path`, or skips the
    test (rather than failing it) when the sandbox denies the bind.

    Some sandboxes (this dev environment included) deny `AF_UNIX` binds
    outright with `PermissionError` -- or, in principle, some other
    `OSError` -- which is an environment limitation, not a regression in
    the code under test. Message formatting is still covered without a
    real socket by the monkeypatched tests below.
    """
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        server.bind(sock_path)
    except (PermissionError, OSError) as exc:
        server.close()
        pytest.skip(f"AF_UNIX socket bind denied by sandbox: {exc!r}")
    return server


def test_sd_notify_sends_a_datagram_to_notify_socket(monkeypatch):
    sock_dir = _short_unix_socket_dir()
    sock_path = os.path.join(sock_dir, "n.sock")
    server = _unix_socket_or_skip(sock_path)
    server.settimeout(2.0)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
        main._sd_notify("READY=1")
        data, _ = server.recvfrom(1024)
        assert data == b"READY=1"
    finally:
        server.close()
        shutil.rmtree(sock_dir, ignore_errors=True)


class _FakeNotifySocket:
    """Stand-in for `socket.socket(AF_UNIX, SOCK_DGRAM)` that records what
    `_sd_notify` did with it instead of touching any real socket."""

    def __init__(self, family, type_):
        self.family = family
        self.type = type_
        self.connected_to = None
        self.sent = None
        self.closed = False

    def connect(self, addr):
        self.connected_to = addr

    def sendall(self, data):
        self.sent = data

    def close(self):
        self.closed = True


def test_sd_notify_formats_and_sends_the_message_without_a_real_socket(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/whatever.sock")
    created = {}

    def fake_socket_factory(family, type_):
        fake = _FakeNotifySocket(family, type_)
        created["sock"] = fake
        return fake

    monkeypatch.setattr(socket, "socket", fake_socket_factory)

    main._sd_notify("READY=1")

    fake = created["sock"]
    assert fake.family == socket.AF_UNIX
    assert fake.type == socket.SOCK_DGRAM
    assert fake.connected_to == "/tmp/whatever.sock"
    assert fake.sent == b"READY=1"  # ASCII-encoded, verbatim
    assert fake.closed is True  # always closed, even on the happy path


def test_sd_notify_converts_an_at_prefixed_address_to_the_abstract_namespace(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "@abstract-name")
    created = {}

    def fake_socket_factory(family, type_):
        fake = _FakeNotifySocket(family, type_)
        created["sock"] = fake
        return fake

    monkeypatch.setattr(socket, "socket", fake_socket_factory)

    main._sd_notify("WATCHDOG=1")

    assert created["sock"].connected_to == "\0abstract-name"


def test_sd_notify_is_a_silent_no_op_without_notify_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    main._sd_notify("READY=1")  # must not raise


def test_sd_notify_never_raises_when_the_socket_is_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "does-not-exist.sock"))
    main._sd_notify("WATCHDOG=1")  # must not raise


def test_watchdog_interval_seconds_is_none_without_notify_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    monkeypatch.setenv("WATCHDOG_USEC", "1000000")
    assert main._watchdog_interval_seconds() is None


def test_watchdog_interval_seconds_is_none_without_watchdog_usec(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/whatever.sock")
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert main._watchdog_interval_seconds() is None


def test_watchdog_interval_seconds_is_half_of_watchdog_usec(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/whatever.sock")
    monkeypatch.setenv("WATCHDOG_USEC", "2000000")
    assert main._watchdog_interval_seconds() == 1.0


def test_watchdog_interval_seconds_is_none_for_a_non_positive_or_invalid_value(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/whatever.sock")
    for raw in ("0", "-5", "not-a-number", ""):
        monkeypatch.setenv("WATCHDOG_USEC", raw)
        assert main._watchdog_interval_seconds() is None


class _ScriptedStopEvent:
    """A fake `threading.Event`-shaped stand-in whose `wait(timeout=...)`
    returns a scripted sequence of results (False = "keep looping", True =
    "stop"), so `_watchdog_loop` can be driven for an exact number of
    iterations synchronously -- no real thread, no real waiting, no timing
    race between a test assertion and a background thread's next tick."""

    def __init__(self, results):
        self._results = list(results)

    def wait(self, timeout=None):
        return self._results.pop(0)


def test_watchdog_loop_pings_while_healthy_and_stops_once_a_worker_stalls(monkeypatch):
    sock_dir = _short_unix_socket_dir()
    sock_path = os.path.join(sock_dir, "n.sock")
    server = _unix_socket_or_skip(sock_path)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

        d = Daemon.__new__(Daemon)
        # Two loop passes (ping, then would-ping-but-stalled), then stop.
        d._stop_event = _ScriptedStopEvent([False, False, True])
        d._heartbeats = main.Heartbeats({"db": 30.0}, clock=_FakeClock())
        d._heartbeats.beat("db")

        healthy_then_stalled = iter([False, True])  # any_stalled() per tick
        d._heartbeats.any_stalled = lambda: next(healthy_then_stalled)

        d._watchdog_loop(0.0)

        server.settimeout(0.5)
        data, _ = server.recvfrom(1024)
        assert data == b"WATCHDOG=1"  # tick 1: healthy, pinged

        server.settimeout(0.2)
        with pytest.raises(TimeoutError):
            server.recvfrom(1024)  # tick 2: stalled, no second ping ever sent
    finally:
        server.close()
        shutil.rmtree(sock_dir, ignore_errors=True)


def _radio_status(**over):
    base = dict(
        connected=False,
        reply_ready=False,
        actual_node_id=None,
        expected_node_id="!11223344",
        mismatch_reason=None,
        connected_since=None,
        last_packet_at=None,
        connected_since_monotonic=None,
        disconnected_since_monotonic=None,
    )
    base.update(over)
    return RadioStatus(**base)


def test_report_radio_health_logs_an_error_once_a_disconnection_passes_threshold(caplog):
    """A radio that stops answering is the one fault the daemon cannot fix by
    itself and cannot detect via heartbeats (the reconnect thread stays
    healthy, looping correctly, with nothing to talk to). It must say so
    loudly -- and exactly once, not once per status tick."""
    d = Daemon.__new__(Daemon)
    d._clock = main._Clock()
    d._radio_disconnected_alerted = False
    since = d._clock.monotonic() - (main._RADIO_DISCONNECTED_ALERT_SECONDS + 60)
    status = _radio_status(disconnected_since_monotonic=since)
    with caplog.at_level(logging.INFO, logger="meshgarden.main"):
        d._report_radio_health(status)
        d._report_radio_health(status)
        d._report_radio_health(status)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    assert "radio" in errors[0].getMessage().lower()


def test_report_radio_health_stays_quiet_during_an_ordinary_brief_reconnect(caplog):
    """A momentary reconnect is normal (the radio reboots on a config change);
    only a sustained outage is worth an ERROR."""
    d = Daemon.__new__(Daemon)
    d._clock = main._Clock()
    d._radio_disconnected_alerted = False
    since = d._clock.monotonic() - 5.0
    with caplog.at_level(logging.INFO, logger="meshgarden.main"):
        d._report_radio_health(_radio_status(disconnected_since_monotonic=since))
    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []


def test_report_radio_health_logs_recovery_and_rearms(caplog):
    d = Daemon.__new__(Daemon)
    d._clock = main._Clock()
    d._radio_disconnected_alerted = False
    since = d._clock.monotonic() - (main._RADIO_DISCONNECTED_ALERT_SECONDS + 60)
    down = _radio_status(disconnected_since_monotonic=since)
    up = _radio_status(connected=True, reply_ready=True, disconnected_since_monotonic=None)
    with caplog.at_level(logging.INFO, logger="meshgarden.main"):
        d._report_radio_health(down)
        d._report_radio_health(up)
        d._report_radio_health(down)  # re-armed: a second outage warns again
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 2
    recoveries = [r for r in caplog.records if "reconnected" in r.getMessage()]
    assert len(recoveries) == 1


def test_report_radio_health_never_alerts_when_never_connected_is_unknown(caplog):
    """Defensive: a RadioStatus carrying no disconnected-since stamp must not
    synthesize an age (and so must not alert)."""
    d = Daemon.__new__(Daemon)
    d._clock = main._Clock()
    d._radio_disconnected_alerted = False
    with caplog.at_level(logging.INFO, logger="meshgarden.main"):
        d._report_radio_health(_radio_status(disconnected_since_monotonic=None))
    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []


def test_status_snapshot_exposes_radio_disconnected_seconds(daemon):
    """`mgarden status` must be able to show how long the radio has been
    dead, not just that it is."""
    payload = daemon.status_snapshot()
    assert "radio_disconnected_seconds" in payload
    assert payload["radio_connected"] is True
    assert payload["radio_disconnected_seconds"] is None


def test_cmd_status_reports_radio_disconnected_seconds(daemon, capsys):
    """An operator reading `mgarden status` must be able to see how long the
    radio has been dead, not have to infer it from dmesg."""
    daemon.drain()
    daemon.write_status_file()
    from meshgarden.main import cmd_status

    assert cmd_status(daemon.cfg, as_json=True) == 0
    data = json.loads(capsys.readouterr().out)
    assert "radio_disconnected_seconds" in data
    assert data["radio_disconnected_seconds"] is None  # connected in this fixture


def _stale_obs(*, received_at=10_000, rx_time=9_800, packet_id=4242):
    """A DM to us whose radio-reported rx_time is far behind our clock --
    the shape a radio with an unsynced clock produces."""
    from meshgarden.models import IncomingPacket

    return IncomingPacket(
        received_at=received_at,
        received_monotonic=float(received_at),
        rx_time=rx_time,
        from_num=0xB24094BA,
        to_num=0x11223344,
        channel=0,
        portnum="TEXT_MESSAGE_APP",
        packet_id=packet_id,
        rx_snr=None,
        rx_rssi=None,
        hop_limit=None,
        hop_start=None,
        via_mqtt=False,
        pki_encrypted=True,
        payload_len=4,
        text="INFO",
        request_id=None,
        routing_error=None,
    )


def test_a_skewed_radio_clock_makes_every_dm_stale():
    """The gate itself: 200s of skew against a 60s TTL rejects a perfectly
    good DM as a replayed packet. This is the whole failure mode -- the
    radio has no battery-backed RTC, so any reboot can produce it."""
    from meshgarden.commands import Reject, check_eligibility

    elig = check_eligibility(
        _stale_obs(received_at=10_000, rx_time=9_800),
        my_node_num=0x11223344,
        reply_ready=True,
        reply_ttl_seconds=60,
    )
    assert elig.eligible is False
    assert elig.reason is Reject.STALE


def test_the_same_dm_is_eligible_once_the_clock_is_synced():
    from meshgarden.commands import check_eligibility

    elig = check_eligibility(
        _stale_obs(received_at=10_000, rx_time=9_997),  # 3s skew, as after a sync
        my_node_num=0x11223344,
        reply_ready=True,
        reply_ttl_seconds=60,
    )
    assert elig.eligible is True


def test_a_stale_dm_logs_a_warning_naming_the_skew(caplog):
    """Regression: a clock-skewed radio made every DM silently vanish --
    recorded as an observation, no requests row, and only a DEBUG line
    nobody sees at the default level. The operator had nothing to look at."""
    from meshgarden.commands import Reject

    d = Daemon.__new__(Daemon)
    d._stale_warned = False
    with caplog.at_level(logging.INFO, logger="meshgarden.main"):
        d._warn_if_stale(_stale_obs(), Reject.STALE, 60)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "200" in text  # the skew, in seconds
    assert "clock" in text.lower()


def test_repeated_stale_dms_warn_only_once(caplog):
    """A wrong clock rejects EVERY DM; one warning per episode, not per
    packet, or a busy node floods the log."""
    from meshgarden.commands import Reject

    d = Daemon.__new__(Daemon)
    d._stale_warned = False
    with caplog.at_level(logging.INFO, logger="meshgarden.main"):
        for pid in range(4):
            d._warn_if_stale(_stale_obs(packet_id=5000 + pid), Reject.STALE, 60)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_a_non_stale_rejection_never_warns(caplog):
    """BROADCAST, NOT_FOR_US and friends are entirely routine -- the mesh is
    full of them. Only a stale DM indicates a misconfigured clock."""
    from meshgarden.commands import Reject

    d = Daemon.__new__(Daemon)
    d._stale_warned = False
    with caplog.at_level(logging.INFO, logger="meshgarden.main"):
        d._warn_if_stale(_stale_obs(), Reject.BROADCAST, 60)
        d._warn_if_stale(_stale_obs(), Reject.NOT_FOR_US, 60)
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_a_stale_warning_rearms_after_a_good_command(caplog):
    """So a second episode (the radio reboots again) warns again."""
    from meshgarden.commands import Reject

    d = Daemon.__new__(Daemon)
    d._stale_warned = False
    with caplog.at_level(logging.INFO, logger="meshgarden.main"):
        d._warn_if_stale(_stale_obs(), Reject.STALE, 60)
        d._stale_warned = False  # what _process_obs does on an eligible packet
        d._warn_if_stale(_stale_obs(), Reject.STALE, 60)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_a_stale_dm_without_an_rx_time_still_warns_without_a_bogus_skew(caplog):
    """The other STALE path (no rx_time, radio freshly connected) must not
    print a fabricated skew number."""
    from dataclasses import replace as dc_replace

    from meshgarden.commands import Reject

    d = Daemon.__new__(Daemon)
    d._stale_warned = False
    with caplog.at_level(logging.INFO, logger="meshgarden.main"):
        d._warn_if_stale(dc_replace(_stale_obs(), rx_time=None), Reject.STALE, 60)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "200" not in warnings[0].getMessage()
