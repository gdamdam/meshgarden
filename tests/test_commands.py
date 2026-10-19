import pytest

from meshgarden.commands import (
    LAB_COMMANDS,
    LAB_HELP_WORDS,
    ChannelKnockCooldown,
    Command,
    ParsedCommand,
    Reject,
    RequestLog,
    boards_text,
    channel_menu_text,
    check_eligibility,
    garden_text,
    help_text,
    knock_text,
    lab_text,
    mail_empty_text,
    mail_list_shown_count,
    mail_list_text,
    mail_pref_ack_text,
    mail_sent_ack_text,
    me_text,
    menu_boards_text,
    menu_daily_text,
    menu_games_text,
    menu_reply_for,
    menu_stats_text,
    menu_text,
    parse_command,
    ping_text,
    quota_hint_suffix,
    record_text,
    seen_ambiguous_text,
    seen_not_heard_text,
    seen_text,
    suggest_command,
    version_text,
    week_text,
    who_text,
    with_quota_hint,
)
from meshgarden.database import connect, migrate
from meshgarden.radio import extract
from tests.fakes import make_packet

ME = 0x11223344


def b(s: str) -> int:
    return len(s.encode("utf-8"))


@pytest.mark.parametrize(
    "text,cmd",
    [
        ("MENU", Command.MENU),
        ("menu", Command.MENU),
        ("  MeNu  ", Command.MENU),
        ("?", Command.MENU),
        ("HELP", Command.NOT_EMERGENCY),
        ("help", Command.NOT_EMERGENCY),
        ("  HeLp  ", Command.NOT_EMERGENCY),
        ("SOS", Command.NOT_EMERGENCY),
        ("sos", Command.NOT_EMERGENCY),
        ("EMERGENCY", Command.NOT_EMERGENCY),
        ("emergency", Command.NOT_EMERGENCY),
        ("INFO", Command.INFO),
        ("info", Command.INFO),
        ("GUESTBOOK", Command.GUESTBOOK),
        ("guestbook", Command.GUESTBOOK),
        ("STATS", Command.STATS),
        ("stats", Command.STATS),
        ("REPEATER", Command.REPEATER),
        ("ICHING", Command.ICHING),
        ("iching", Command.ICHING),
        ("  IChInG  ", Command.ICHING),
        ("HAIKU", Command.HAIKU),
        ("haiku", Command.HAIKU),
        ("  HaIkU  ", Command.HAIKU),
        ("SIGN hi", Command.SIGN),
        ("sign hi", Command.SIGN),
        ("BOOK", Command.BOOK),
        ("book", Command.BOOK),
        ("  BoOk  ", Command.BOOK),
        ("BOOK 1984", Command.BOOK),
        ("book dune", Command.BOOK),
        ("WHOAMI", Command.ME),
        ("whoami", Command.ME),
        ("SIGNAL", Command.ME),
        ("signal", Command.ME),
        ("GARDEN", Command.GARDEN),
        ("garden", Command.GARDEN),
        ("RECORD", Command.RECORD),
        ("record", Command.RECORD),
        ("WEEK", Command.WEEK),
        ("week", Command.WEEK),
        ("UNSIGN", Command.UNSIGN),
        ("unsign", Command.UNSIGN),
        ("SEEN ccdd", Command.SEEN),
        ("seen ccdd", Command.SEEN),
        ("WHO", Command.WHO),
        ("who", Command.WHO),
        ("ME", Command.ME),
        ("me", Command.ME),
        ("PING", Command.PING),
        ("ping", Command.PING),
        ("WHO extra", Command.UNKNOWN),
        ("ME extra", Command.UNKNOWN),
        ("PING extra", Command.UNKNOWN),
        ("VERSION", Command.VERSION),
        ("version", Command.VERSION),
        ("  VeRsIoN  ", Command.VERSION),
        ("VERSION 1.0.0", Command.UNKNOWN),
        ("hello there", Command.UNKNOWN),
        ("", Command.UNKNOWN),
        ("HELPER", Command.UNKNOWN),
        ("MENUS", Command.UNKNOWN),
    ],
)
def test_parse_is_case_insensitive_and_exact(text, cmd):
    assert parse_command(text).command is cmd


def test_sign_preserves_argument_case_and_inner_spacing():
    p = parse_command("sign Hello There World")
    assert p.command is Command.SIGN
    assert p.argument == "Hello There World"


def test_sign_with_no_argument_parses_as_sign_with_empty_argument():
    p = parse_command("SIGN")
    assert p.command is Command.SIGN and p.argument == ""
    p2 = parse_command("SIGN    ")
    assert p2.command is Command.SIGN and p2.argument == ""


def test_book_preserves_argument_case_and_inner_spacing():
    p = parse_command("book The Hitchhiker's Guide")
    assert p.command is Command.BOOK
    assert p.argument == "The Hitchhiker's Guide"


def test_book_with_no_argument_parses_as_book_with_empty_argument():
    p = parse_command("BOOK")
    assert p.command is Command.BOOK and p.argument == ""
    p2 = parse_command("BOOK    ")
    assert p2.command is Command.BOOK and p2.argument == ""


def test_seen_preserves_argument_case_and_inner_spacing():
    p = parse_command("seen Some Node")
    assert p.command is Command.SEEN
    assert p.argument == "Some Node"


def test_seen_with_no_argument_parses_as_seen_with_empty_argument():
    p = parse_command("SEEN")
    assert p.command is Command.SEEN and p.argument == ""
    p2 = parse_command("SEEN    ")
    assert p2.command is Command.SEEN and p2.argument == ""


def test_bare_commands_have_empty_argument_even_with_trailing_junk():
    assert parse_command("HELP me please").command is Command.UNKNOWN
    assert parse_command("ICHING please").command is Command.UNKNOWN
    assert parse_command("HAIKU please").command is Command.UNKNOWN


# --- VERSION: hidden operator command -----------------------------------------


def test_version_text_format_and_budget():
    t = version_text("1.0.0")
    assert t == "\U0001f331 MeshGarden v1.0.0"
    assert b(t) <= 180


def test_help_version_is_none_like_any_unknown_word():
    """VERSION is deliberately absent from _HELP_USAGE/_KNOWN_COMMAND_WORDS
    so it never surfaces via HELP/?: help_text("VERSION") is None, and
    HELP VERSION / ? VERSION parse as UNKNOWN just like any other
    unrecognized word."""
    assert help_text("VERSION") is None
    assert parse_command("HELP VERSION").command is Command.UNKNOWN
    assert parse_command("? VERSION") == ParsedCommand(Command.MENU, "VERSION")


def test_version_is_not_named_in_any_menu_text():
    for text in (
        menu_text(),
        menu_text(lab_unlocked=True),
        menu_stats_text(),
        menu_games_text(),
        menu_daily_text(),
        menu_boards_text(),
        lab_text(),
    ):
        assert "VERSION" not in text


# --- MENU argument parsing (two-level menu) -----------------------------------


@pytest.mark.parametrize(
    "text,expected_argument",
    [
        ("MENU", ""),
        ("MENU STATS", "STATS"),
        ("menu stats", "stats"),
        ("MENU games", "games"),
        ("MENU GAMES", "GAMES"),
        ("MENU xyz", "xyz"),
        ("MENU 2", "2"),
        ("?", ""),
        ("? STATS", "STATS"),
        ("? GAMES", "GAMES"),
        ("? xyz", "xyz"),
    ],
)
def test_menu_and_question_mark_alias_carry_their_argument(text, expected_argument):
    p = parse_command(text)
    assert p.command is Command.MENU
    assert p.argument == expected_argument


# --- HELP <cmd> / ? <cmd>: one-line usage lookup (ruling 14) ------------------


def test_help_with_a_known_command_word_returns_its_usage():
    parsed = parse_command("HELP sign")
    assert parsed.command == Command.CMD_HELP
    assert parsed.argument == "SIGN"


def test_bare_help_is_still_not_emergency():
    assert parse_command("HELP") == ParsedCommand(Command.NOT_EMERGENCY, "")


def test_help_with_an_unknown_word_is_unknown():
    assert parse_command("HELP xyzzy") == ParsedCommand(Command.UNKNOWN, "")


def test_question_mark_with_a_known_command_word_returns_its_usage():
    parsed = parse_command("? sign")
    assert parsed.command == Command.CMD_HELP
    assert parsed.argument == "SIGN"


def test_question_mark_stats_still_selects_the_stats_page():
    assert parse_command("? stats") == ParsedCommand(Command.MENU, "stats")


def test_menu_boards_still_selects_the_boards_page():
    """Regression: BOARDS must be excluded from the CMD_HELP takeover the
    same way STATS/GAMES are, so MENU BOARDS/? BOARDS stay reachable."""
    assert parse_command("MENU BOARDS") == ParsedCommand(Command.MENU, "BOARDS")
    assert parse_command("? BOARDS") == ParsedCommand(Command.MENU, "BOARDS")
    assert parse_command("menu boards") == ParsedCommand(Command.MENU, "boards")


def test_help_boards_still_returns_its_usage():
    parsed = parse_command("HELP boards")
    assert parsed.command == Command.CMD_HELP
    assert parsed.argument == "BOARDS"


def test_help_text_known_and_unknown():
    assert help_text("SIGN") is not None
    assert len(help_text("SIGN").encode("utf-8")) <= 180
    assert help_text("NOPE") is None


def test_help_menu_does_not_leak_the_lab_tier():
    """Final-review fix: HELP MENU (unlike HELP POST/READ/BOARDS/MAIL) is
    not LAB-gated in main.py, so its own text must not name the LAB-only
    MENU BOARDS sub-page -- a locked node sending HELP MENU must not learn
    the tier exists from this string."""
    text = help_text("MENU")
    assert text is not None
    assert "BOARDS" not in text
    assert len(text.encode("utf-8")) <= 180


def test_menu_boards_text_fits_the_byte_budget_and_lists_its_commands():
    text = menu_boards_text()
    assert len(text.encode("utf-8")) <= 180
    for word in ("POST", "READ", "BOARDS", "MAIL"):
        assert word in text


def test_menu_stats_text_lists_canonical_directory_commands():
    text = menu_stats_text()
    assert len(text.encode("utf-8")) <= 180
    for word in ("SEEN", "ME", "CHECK"):
        assert word in text
    assert "WHO" not in text and "PING" not in text


def test_menu_games_and_wordle_help_mention_clue():
    games_text = menu_games_text()
    assert len(games_text.encode("utf-8")) <= 180
    assert "CLUE" in games_text

    usage = help_text("WORDLE")
    assert usage is not None
    assert len(usage.encode("utf-8")) <= 180
    assert "CLUE" in usage


def test_menu_games_mentions_score_for_both_book_and_wordle():
    games_text = menu_games_text()
    assert len(games_text.encode("utf-8")) <= 180
    book_line = next(line for line in games_text.splitlines() if "BOOK" in line)
    wordle_line = next(line for line in games_text.splitlines() if "WORDLE" in line)
    assert "SCORE" in book_line
    assert "SCORE" in wordle_line


def test_help_book_and_wordle_mention_score():
    for word in ("BOOK", "WORDLE"):
        usage = help_text(word)
        assert usage is not None
        assert len(usage.encode("utf-8")) <= 180
        assert "SCORE" in usage


# --- GUESTBOOK: optional page / #<id> argument --------------------------------


def test_guestbook_bare_has_empty_argument():
    p = parse_command("GUESTBOOK")
    assert p.command is Command.GUESTBOOK and p.argument == ""
    p2 = parse_command("guestbook")
    assert p2.command is Command.GUESTBOOK and p2.argument == ""


@pytest.mark.parametrize("page", ["1", "2", "3", "999"])
def test_guestbook_bare_digit_is_a_page_number(page):
    p = parse_command(f"GUESTBOOK {page}")
    assert p.command is Command.GUESTBOOK
    assert p.argument == page


def test_guestbook_hash_id_is_a_single_entry_lookup():
    p = parse_command("GUESTBOOK #12")
    assert p.command is Command.GUESTBOOK
    assert p.argument == "#12"


@pytest.mark.parametrize(
    "text",
    [
        "GUESTBOOK abc",
        "GUESTBOOK 0",
        "GUESTBOOK 1000",
        "GUESTBOOK -1",
        "GUESTBOOK #",
        "GUESTBOOK #abc",
        "GUESTBOOK 12 34",
    ],
)
def test_guestbook_invalid_argument_is_silently_unknown(text):
    """Consistent with the exact-match philosophy: a malformed GUESTBOOK
    argument gets no reply at all, same as any other unrecognized text --
    it is never a distinct error reply."""
    assert parse_command(text).command is Command.UNKNOWN


# --- WORDLE: optional guess argument, same shape as SIGN/BOOK -----------------


def test_wordle_bare_has_empty_argument():
    p = parse_command("WORDLE")
    assert p.command is Command.WORDLE and p.argument == ""
    p2 = parse_command("wordle")
    assert p2.command is Command.WORDLE and p2.argument == ""


def test_wordle_preserves_argument_case():
    p = parse_command("WORDLE CrAnE")
    assert p.command is Command.WORDLE
    assert p.argument == "CrAnE"


def test_wordle_lowercase_alias_works():
    p = parse_command("wordle crane")
    assert p.command is Command.WORDLE
    assert p.argument == "crane"


def test_claim_is_idempotent_per_sender_packet(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    log = RequestLog(conn, dedup_hours=24)
    assert log.claim("!aabbccdd", 42, 1000, Command.MENU) is True
    assert log.claim("!aabbccdd", 42, 1000, Command.MENU) is False
    assert log.claim("!aabbccdd", 43, 1000, Command.MENU) is True
    assert log.claim("!11112222", 42, 1000, Command.MENU) is True


def test_claim_stores_command_name_but_never_arguments(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    RequestLog(conn).claim("!aabbccdd", 42, 1000, Command.SIGN)
    dump = "\n".join(conn.iterdump())
    assert "SIGN" in dump  # the bounded command name is fine
    row = conn.execute("SELECT * FROM processed_requests").fetchone()
    assert set(row.keys()) >= {"command", "state"}
    assert row["command"] == "SIGN"


def test_claim_stores_book_command_name_but_never_the_guess(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    RequestLog(conn).claim("!aabbccdd", 42, 1000, Command.BOOK)
    dump = "\n".join(conn.iterdump())
    assert "BOOK" in dump  # the bounded command name is fine
    row = conn.execute("SELECT * FROM processed_requests").fetchone()
    assert row["command"] == "BOOK"


def test_dedup_survives_restart(tmp_path):
    p = str(tmp_path / "o.db")
    conn = connect(p)
    migrate(conn)
    RequestLog(conn).claim("!aabbccdd", 42, 1000, Command.MENU)
    conn.close()
    conn2 = connect(p)
    assert RequestLog(conn2).claim("!aabbccdd", 42, 1000, Command.MENU) is False


# --- ChannelKnockCooldown (task 3B-3) ---------------------------------------


def test_knock_text_does_not_claim_the_recipient_asked_for_it():
    text = knock_text()
    lowered = text.lower()
    assert "you asked" not in lowered
    assert "you requested" not in lowered
    assert "your request" not in lowered


def test_knock_text_fits_meshcores_default_max_text_bytes():
    # transport-boundary-spec.md section 5: 120 is MeshCore's default
    # max_text_bytes -- the tightest reply budget in the system. The knock
    # text is fixed and must fit with room to spare, not merely survive
    # truncate_utf8's last-line safety net.
    assert len(knock_text().encode("utf-8")) <= 120


def test_try_claim_allows_a_first_knock_and_then_blocks_a_second(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    cooldown = ChannelKnockCooldown(conn)
    assert cooldown.try_claim("!aabbccdd", 1_000, 86_400) is True
    assert cooldown.try_claim("!aabbccdd", 1_001, 86_400) is False


def test_try_claim_allows_again_once_the_window_has_fully_elapsed(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    cooldown = ChannelKnockCooldown(conn)
    assert cooldown.try_claim("!aabbccdd", 1_000, 86_400) is True
    assert cooldown.try_claim("!aabbccdd", 1_000 + 86_400 - 1, 86_400) is False
    assert cooldown.try_claim("!aabbccdd", 1_000 + 86_400, 86_400) is True


def test_try_claim_is_keyed_per_node_not_globally(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    cooldown = ChannelKnockCooldown(conn)
    assert cooldown.try_claim("!aabbccdd", 1_000, 86_400) is True
    # A different resolved recipient is unaffected by the first one's claim
    # -- the whole point of keying on the recipient, not the claimed name.
    assert cooldown.try_claim("!11112222", 1_000, 86_400) is True


def test_release_undoes_a_matching_claim_and_allows_a_knock_again(tmp_path):
    """Fix round 1, item 2: the compensating action for a knock whose DM
    definitely never transmitted."""
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    cooldown = ChannelKnockCooldown(conn)
    assert cooldown.try_claim("!aabbccdd", 1_000, 86_400) is True
    cooldown.release("!aabbccdd", 1_000)
    # Released -- a knock well inside the original window succeeds again.
    assert cooldown.try_claim("!aabbccdd", 1_050, 86_400) is True


def test_release_is_a_no_op_against_a_differently_timed_claim(tmp_path):
    """`release` only deletes the row it still recognizes as its own claim
    (matched by the exact `claimed_at` timestamp) -- a claim superseded by
    a later, legitimately-successful knock in the meantime must survive."""
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    cooldown = ChannelKnockCooldown(conn)
    assert cooldown.try_claim("!aabbccdd", 1_000, 86_400) is True
    # A stale release referencing an old timestamp that is no longer the
    # row's current value must not delete a newer, still-valid claim.
    cooldown.release("!aabbccdd", 999)
    assert cooldown.try_claim("!aabbccdd", 1_001, 86_400) is False


def test_try_claim_survives_restart(tmp_path):
    p = str(tmp_path / "o.db")
    conn = connect(p)
    migrate(conn)
    assert ChannelKnockCooldown(conn).try_claim("!aabbccdd", 1_000, 86_400) is True
    conn.close()

    conn2 = connect(p)
    assert ChannelKnockCooldown(conn2).try_claim("!aabbccdd", 1_001, 86_400) is False


def test_channel_knock_cooldown_prune_deletes_only_fully_elapsed_rows(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    cooldown = ChannelKnockCooldown(conn)
    cooldown.try_claim("!aabbccdd", 1_000, 86_400)
    cooldown.try_claim("!11112222", 90_000, 86_400)

    deleted = cooldown.prune(1_000 + 86_400 + 1, 86_400)
    assert deleted == 1
    remaining = {row["node_id"] for row in conn.execute("SELECT node_id FROM channel_knocks")}
    assert remaining == {"!11112222"}


def test_uncertain_states_become_unknown_on_startup(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    log = RequestLog(conn)
    log.claim("!aabbccdd", 1, 1000, Command.MENU)
    log.set_state("!aabbccdd", 1, "queued", 1000)
    log.claim("!aabbccdd", 2, 1000, Command.MENU)
    log.set_state("!aabbccdd", 2, "sending", 1000)
    log.claim("!aabbccdd", 3, 1000, Command.MENU)
    log.set_state("!aabbccdd", 3, "acked", 1000)
    # 'submitted' (a reply already handed to the radio, tx_packet_id set)
    # is exactly as uncertain across a restart as 'queued'/'sending': its
    # ack outcome can no longer be waited for, so it must resolve too --
    # never resent.
    log.claim("!aabbccdd", 4, 1000, Command.MENU)
    log.set_state("!aabbccdd", 4, "submitted", 1000, tx_packet_id=555)
    assert log.resolve_uncertain_on_startup(2000) == (3, 0)  # (unknown_count, failed_count)
    assert log.get_state("!aabbccdd", 1) == "unknown"
    assert log.get_state("!aabbccdd", 2) == "unknown"
    assert log.get_state("!aabbccdd", 3) == "acked"
    assert log.get_state("!aabbccdd", 4) == "unknown"
    conn.close()


def test_set_state_with_commit_false_does_not_open_its_own_transaction(tmp_path):
    """Round 1 fix: `commit=False` must not commit early on its own -- a
    caller wrapping it (plus another write, e.g. a guestbook insert) in one
    `with conn:` block is the sole transaction owner, so a later failure in
    that block rolls back both writes together."""
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    log = RequestLog(conn)
    log.claim("!aabbccdd", 1, 1000, Command.SIGN)

    with pytest.raises(RuntimeError), conn:
        log.set_state("!aabbccdd", 1, "queued", 1000, commit=False)
        raise RuntimeError("simulated failure before the block commits")

    assert log.get_state("!aabbccdd", 1) == "received"  # rolled back, not "queued"


def test_set_state_with_commit_false_persists_once_the_callers_block_commits(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    log = RequestLog(conn)
    log.claim("!aabbccdd", 1, 1000, Command.SIGN)

    with conn:
        log.set_state("!aabbccdd", 1, "queued", 1000, commit=False)

    assert log.get_state("!aabbccdd", 1) == "queued"


# --- freshness / reconnect-replay gate (check_eligibility) --------------------


def _obs(rx_time, now, to_num=ME, channel=0):
    return extract(
        make_packet(to_num=to_num, channel=channel, text="HELP", rx_time=rx_time),
        now=now,
        now_monotonic=1.0,
    )


def test_fresh_rx_time_is_eligible():
    obs = _obs(rx_time=940, now=1000)  # 60s old, at the ttl boundary (not over it)
    r = check_eligibility(obs, my_node_num=ME, reply_ready=True, reply_ttl_seconds=60)
    assert r.eligible is True


def test_old_rx_time_is_rejected_as_stale():
    obs = _obs(rx_time=800, now=1000)  # 200s old, over the 60s ttl
    r = check_eligibility(obs, my_node_num=ME, reply_ready=True, reply_ttl_seconds=60)
    assert r.eligible is False and r.reason is Reject.STALE


def test_absent_rx_time_inside_the_grace_window_is_rejected_as_stale():
    obs = _obs(rx_time=None, now=1000)
    r = check_eligibility(
        obs, my_node_num=ME, reply_ready=True, reply_ttl_seconds=60, connected_seconds=10
    )
    assert r.eligible is False and r.reason is Reject.STALE


def test_absent_rx_time_after_the_grace_window_is_eligible():
    obs = _obs(rx_time=None, now=1000)
    r = check_eligibility(
        obs, my_node_num=ME, reply_ready=True, reply_ttl_seconds=60, connected_seconds=120
    )
    assert r.eligible is True


def test_absent_rx_time_defaults_to_eligible_when_connected_seconds_is_not_passed():
    """Backward compatibility: existing callers that never pass
    connected_seconds (e.g. test_eligibility.py) must be unaffected."""
    obs = _obs(rx_time=None, now=1000)
    r = check_eligibility(obs, my_node_num=ME, reply_ready=True)
    assert r.eligible is True


# --- MENU (two-level: top level, MENU STATS, MENU GAMES) ----------------------


def test_menu_top_level_fits_the_budget_and_points_to_all_sub_pages():
    text = menu_text()
    assert b(text) <= 180
    assert "SIGN" in text and "GUESTBOOK" in text and "NEW" in text
    assert "MENU STATS" in text
    assert "MENU GAMES" in text
    assert "MENU DAILY" in text
    assert "MENU SOCIAL" in text


def test_menu_top_level_no_longer_mentions_boards():
    """LAB tier (Task 13): boards/mail are experimental and gated, so the
    top-level menu must not advertise MENU BOARDS -- MENU BOARDS/BOARDS
    itself stay reachable (main.py's LAB gate decides that), only the
    top-level pointer to them is removed."""
    assert "BOARDS" not in menu_text()


def test_menu_text_locked_fits_meshcore_without_revealing_lab():
    text = menu_text()
    assert menu_text(lab_unlocked=False) == text
    assert b(text + quota_hint_suffix(999, 999)) <= 120
    assert "LAB" not in text
    assert "MENU SOCIAL" in text


def test_menu_text_lab_unlocked_adds_the_lab_line():
    text = menu_text(lab_unlocked=True)
    assert b(text + quota_hint_suffix(999, 999)) <= 120
    assert "MENU SOCIAL" in text.splitlines()
    assert "MENU LAB" in menu_reply_for("SOCIAL", lab_unlocked=True)
    assert "INFO" in menu_reply_for("SOCIAL", lab_unlocked=True)
    for cmd in ("SIGN", "GUESTBOOK", "NEW", "MENU STATS", "MENU GAMES", "MENU DAILY"):
        assert cmd in text


def test_menu_text_lab_unlocked_fits_with_the_widest_quota_hint():
    text = with_quota_hint(
        menu_text(lab_unlocked=True), 999, 999, max_bytes=180
    )
    assert b(text) <= 180
    assert "quota 999/999 today" in text


def test_menu_reply_for_threads_lab_unlocked_through_to_the_fallback():
    assert menu_reply_for("", lab_unlocked=True) == menu_text(lab_unlocked=True)
    assert menu_reply_for("xyz", lab_unlocked=True) == menu_text(lab_unlocked=True)
    assert menu_reply_for("", lab_unlocked=False) == menu_text(lab_unlocked=False)


def test_menu_stats_lists_its_commands_and_fits_the_budget():
    text = menu_stats_text()
    assert b(text) <= 180
    for cmd in ("STATS", "REPEATER", "GARDEN", "WEEK", "ME", "CHECK", "PATH", "SEEN"):
        assert cmd in text
    for hidden_specialist in ("PING", "WHO", "RECORD"):
        assert hidden_specialist not in text


def test_menu_games_lists_its_two_commands_and_fits_the_budget():
    text = menu_games_text()
    assert b(text) <= 180
    for cmd in ("BOOK", "WORDLE"):
        assert cmd in text
    assert "ICHING" not in text
    assert "HAIKU" not in text


def test_menu_daily_text_fits_the_budget_and_lists_iching_and_haiku():
    text = menu_daily_text()
    assert b(text) <= 180
    for cmd in ("ICHING", "HAIKU"):
        assert cmd in text


def test_meteor_has_no_public_command_help_or_suggestion_surface():
    assert parse_command("METEOR").command is Command.UNKNOWN
    assert parse_command("!meteor").command is Command.UNKNOWN
    assert parse_command("HELP METEOR").command is Command.UNKNOWN
    assert help_text("METEOR") is None
    assert suggest_command("METEOR") is None


@pytest.mark.parametrize(
    "argument,expected",
    [
        ("", "menu"),
        ("STATS", "stats"),
        ("stats", "stats"),
        ("  StAtS  ", "stats"),
        ("GAMES", "games"),
        ("games", "games"),
        ("DAILY", "daily"),
        ("daily", "daily"),
        ("xyz", "menu"),
        ("2", "menu"),
    ],
)
def test_menu_reply_for_selects_the_page_case_insensitively(argument, expected):
    expected_text = {
        "menu": menu_text(),
        "stats": menu_stats_text(),
        "games": menu_games_text(),
        "daily": menu_daily_text(),
    }[expected]
    assert menu_reply_for(argument) == expected_text


def test_menu_reply_for_junk_argument_never_echoes_the_argument():
    assert "xyz" not in menu_reply_for("xyz")
    assert "2" not in menu_reply_for("2")


# --- LAB tier: unlock word, gated commands, lab_text/MENU LAB (Task 13) -------


def test_lab_commands_frozenset_contains_exactly_the_experimental_commands():
    assert frozenset(
        {Command.POST, Command.READ, Command.BOARDS, Command.MAIL, Command.REPLY}
    ) == LAB_COMMANDS


def test_lab_help_words_cover_every_lab_command_and_its_aliases():
    """A locked node's `HELP <word>` is gated off `LAB_HELP_WORDS`, so every
    LAB command word must be in it or it would leak a gated command's
    usage string."""
    assert {c.value for c in LAB_COMMANDS} <= LAB_HELP_WORDS
    # Nothing ungated crept in.
    assert "ICHING" not in LAB_HELP_WORDS and "HAIKU" not in LAB_HELP_WORDS


@pytest.mark.parametrize(
    "text",
    ["SUPERGARDENME", "supergardenme", "  SuperGardenMe  ", "SuPeRgArDeNmE"],
)
def test_unlock_word_parses_as_lab_unlock_case_insensitively(text):
    assert parse_command(text, lab_unlock_word="SUPERGARDENME") == ParsedCommand(
        Command.LAB_UNLOCK, ""
    )


def test_unlock_word_only_matches_as_the_whole_message():
    assert parse_command("SUPERGARDENME please", lab_unlock_word="SUPERGARDENME").command is (
        Command.UNKNOWN
    )


def test_unlock_word_is_unknown_when_lab_unlock_word_is_not_configured():
    assert parse_command("SUPERGARDENME", lab_unlock_word="").command is Command.UNKNOWN


def test_unlock_word_default_argument_never_matches_ordinary_text():
    assert parse_command("hello there").command is Command.UNKNOWN


def test_menu_lab_and_question_mark_lab_select_the_lab_page():
    assert parse_command("MENU LAB") == ParsedCommand(Command.MENU, "LAB")
    assert parse_command("? lab") == ParsedCommand(Command.MENU, "lab")


def test_menu_reply_for_lab_selects_lab_text():
    assert menu_reply_for("LAB") == lab_text()
    assert menu_reply_for("lab") == lab_text()


def test_lab_text_fits_the_byte_budget_and_lists_its_commands():
    text = lab_text()
    assert b(text) <= 180
    for word in ("POST", "READ", "BOARDS", "MAIL"):
        assert word in text


def test_menu_lab_names_every_gated_command():
    """MENU LAB is the only page an unlocked node can discover the tier's
    commands from, so no LAB command may be missing from it."""
    text = lab_text()
    for command in LAB_COMMANDS:
        assert command.value in text


# --- WHOAMI/SIGNAL are now aliases of ME (see the ME section below) --------------
# their own formerly-separate `whoami_text` coverage (missing fields, hops
# derivation, display-name fallback) now lives on `me_text`, the field it
# gained (RSSI) included -- see test_me_text_missing_fields_render_n_a and
# test_me_text_hops_derived_only_when_both_present.


# --- SEEN --------------------------------------------------------------------------


def test_seen_text_direct_includes_snr():
    text = seen_text(
        "!94ba",
        relative_ago="3 min ago",
        how="direct",
        snr=8.5,
        first_seen_date="2026-09-01",
        days_seen=5,
    )
    assert "SNR +8.5 dB" in text and "3/7" not in text and "heard 5/7 days" in text
    assert b(text) <= 180


def test_seen_text_relay_omits_snr():
    text = seen_text(
        "!94ba",
        relative_ago="3 min ago",
        how="via 4 hops",
        snr=None,
        first_seen_date="2026-09-01",
        days_seen=5,
    )
    assert "SNR" not in text
    assert b(text) <= 180


def test_seen_text_unknown_how_omits_snr():
    text = seen_text(
        "!94ba",
        relative_ago="3 days ago",
        how="unknown",
        snr=None,
        first_seen_date="2026-09-01",
        days_seen=1,
    )
    assert "SNR" not in text
    assert "hops not reported" in text
    assert b(text) <= 180


@pytest.mark.parametrize("how,snr", [("unknown", 8.5), ("direct", -99.9), ("via 4 hops", 8.5)])
@pytest.mark.parametrize("max_bytes", [120, 180])
def test_seen_preserves_core_fields_with_named_node(how, snr, max_bytes):
    text = seen_text(
        "🌀Fnor · !038b",
        relative_ago="6 days ago",
        how=how,
        snr=snr,
        first_seen_date="2026-09-19",
        days_seen=2,
        heard_24h=12345,
        max_bytes=max_bytes,
    )
    assert b(text) <= max_bytes
    assert "!038b" in text
    assert "🌀Fnor" in text
    assert "last heard 6 days ago" in text
    assert "first seen 2026-09-19" in text
    assert "heard 2/7 days" in text
    assert ("SNR" in text) == (how == "direct")
    if max_bytes == 180:
        assert "24h: 12345 packets" in text
    elif how == "direct":
        assert "24h:" not in text


def test_seen_not_heard_text_fits_and_never_echoes_input():
    text = seen_not_heard_text("!aabbccdd", listening_seconds=7 * 86400)
    assert "not heard in 7 days" in text
    assert b(text) <= 180


def test_seen_not_heard_text_short_listening_window_names_when_we_started():
    """The real bug this fixes: a daemon running a few hours must never
    claim a 7-day window it hasn't actually listened for."""
    text = seen_not_heard_text("!aabbccdd", listening_seconds=3 * 3600)
    assert "not heard in 7 days" not in text
    assert "not heard since I started listening 3 hr ago" in text
    assert b(text) <= 180


def test_seen_not_heard_text_with_no_observations_ever():
    text = seen_not_heard_text("!aabbccdd", listening_seconds=None)
    assert text == "📡 !aabbccdd not heard yet 🌱"
    assert b(text) <= 180


def test_seen_ambiguous_text_never_echoes_the_argument():
    text = seen_ambiguous_text()
    assert "!xxxxxxxx" in text
    assert b(text) <= 180


def test_seen_text_worst_case_fits_the_budget():
    """Byte-budget test: the longest tail label, direct+SNR line, and a
    far-past first-seen date/days_seen."""
    text = seen_text(
        "!ffffffff",
        relative_ago="999 days ago",
        how="direct",
        snr=-99.9,
        first_seen_date="2026-09-01",
        days_seen=7,
    )
    assert b(text) <= 180


# --- WHO ---------------------------------------------------------------------------


def test_who_text_fits_and_has_both_windows():
    text = who_text(heard_1h=12, direct_1h=5, heard_24h=134)
    assert "12" in text and "5" in text and "134" in text
    assert b(text) <= 180


def test_who_text_worst_case_fits_the_budget():
    text = who_text(heard_1h=9999, direct_1h=9999, heard_24h=9999)
    assert b(text) <= 180


# --- PING --------------------------------------------------------------------------


def test_ping_text_direct():
    text = ping_text(snr=10.5, hop_start=3, hop_limit=3, pki_encrypted=True)
    assert text == "🏓 pong\nSNR +10.5 dB · hops 0 direct · PKI ✓ 🌱"
    assert b(text) <= 180


def test_ping_text_relay_and_missing_pki():
    text = ping_text(snr=None, hop_start=7, hop_limit=3, pki_encrypted=False)
    assert "via relay" in text and "PKI ✗" in text and "SNR n/a" in text
    assert b(text) <= 180


def test_ping_text_worst_case_fits_the_budget():
    text = ping_text(snr=-99.9, hop_start=99, hop_limit=0, pki_encrypted=True)
    assert b(text) <= 180


# --- ME ----------------------------------------------------------------------------


def test_me_text_named_fits_and_includes_mail_status():
    text = me_text(
        "!23456789",
        display_name="fndr",
        first_seen_date="2026-09-01",
        days_seen=5,
        heard_24h=12,
        snr=8.5,
        rssi=-90,
        hop_start=3,
        hop_limit=3,
        pki_encrypted=True,
        signed_count=3,
        quota_used=12,
        quota_limit=60,
        mail_enabled=True,
        mail_pending=2,
    )
    assert "fndr (!23456789)" in text and "mail on · 2 waiting" in text
    assert "RSSI -90" in text
    assert b(text) <= 180


def test_me_text_unnamed_falls_back_to_bare_id():
    text = me_text(
        "!23456789",
        display_name=None,
        first_seen_date="2026-09-01",
        days_seen=5,
        heard_24h=12,
        snr=8.5,
        rssi=-90,
        hop_start=3,
        hop_limit=3,
        pki_encrypted=True,
        signed_count=3,
        quota_used=12,
        quota_limit=60,
        mail_enabled=False,
        mail_pending=0,
    )
    assert text.startswith("📡 You are !23456789\n")
    assert "mail off" in text
    assert b(text) <= 180


def test_me_text_missing_fields_render_n_a():
    text = me_text(
        "!b24094ba",
        display_name=None,
        first_seen_date="2026-09-01",
        days_seen=1,
        heard_24h=1,
        snr=None,
        rssi=None,
        hop_start=None,
        hop_limit=None,
        pki_encrypted=False,
        signed_count=0,
        quota_used=0,
        quota_limit=60,
        mail_enabled=False,
        mail_pending=0,
    )
    assert "SNR n/a" in text
    assert "RSSI n/a" in text
    assert "hops n/a" in text
    assert "PKI ✗" in text


def test_me_text_hops_derived_only_when_both_present():
    kwargs = dict(
        display_name=None,
        first_seen_date="2026-09-01",
        days_seen=1,
        heard_24h=1,
        snr=None,
        rssi=None,
        pki_encrypted=False,
        signed_count=0,
        quota_used=0,
        quota_limit=60,
        mail_enabled=False,
        mail_pending=0,
    )
    text = me_text("!b24094ba", hop_start=5, hop_limit=None, **kwargs)
    assert "hops n/a" in text
    text2 = me_text("!b24094ba", hop_start=5, hop_limit=3, **kwargs)
    assert "hops 2 via relay" in text2


def test_me_text_worst_case_fits_the_budget():
    """Byte-budget test: longest node id, a display name, worst-case
    counts/signal fields, and mail on with a pending count."""
    text = me_text(
        "!ffffffff",
        display_name="Xxxx",
        first_seen_date="2026-09-01",
        days_seen=7,
        heard_24h=9999,
        snr=-99.9,
        rssi=-999,
        hop_start=99,
        hop_limit=0,
        pki_encrypted=True,
        signed_count=9999,
        quota_used=999,
        quota_limit=999,
        mail_enabled=True,
        mail_pending=999,
    )
    assert b(text) <= 180


# --- quota hint (MENU top-level) ---------------------------------------------------


def test_quota_hint_suffix_shape():
    assert quota_hint_suffix(5, 60) == " · quota 5/60 today"


def test_with_quota_hint_requires_max_bytes_keyword():
    """max_bytes is a required keyword -- no silent fallback to this
    module's own _MAX_RESPONSE_BYTES, which would let a caller budgeting
    against a different configured limit (main.py's
    service.max_response_bytes) mismatch it and get re-truncated later."""
    with pytest.raises(TypeError):
        with_quota_hint(menu_text(), 5, 60)


def test_with_quota_hint_appends_to_the_last_line_and_fits_the_budget():
    t = with_quota_hint(menu_text(), 5, 60, max_bytes=180)
    assert t == menu_text() + " · quota 5/60 today"
    assert b(t) <= 180


def test_with_quota_hint_on_a_multiline_base_fits_the_budget():
    """A multi-line base (like a real MENU reply) still gets the suffix
    appended to its last line only."""
    base = "📡 You are !b24094ba\nSNR +10.5 dB · RSSI -72\nhops 0 direct · PKI ✓"
    t = with_quota_hint(base, 1, 60, max_bytes=180)
    assert t == base + " · quota 1/60 today"
    assert b(t) <= 180


def test_with_quota_hint_never_truncates_the_suffix_itself():
    """Even with an absurdly tight budget, the quota suffix -- the entire
    reason this call exists -- must always survive intact; only the
    caller's own last line gets shortened (down to nothing, if it must)."""
    text = "line one\nline two is much longer than the budget allows"
    t = with_quota_hint(text, 999999, 999999, max_bytes=40)
    suffix = quota_hint_suffix(999999, 999999)
    assert t.endswith(suffix)
    assert t.startswith("line one\n")
    assert b(t) == 40  # last line truncated exactly to what's left of the budget


def test_with_quota_hint_drops_the_last_line_entirely_when_the_budget_cannot_fit_it():
    text = "line one\nline two is much longer than the budget allows"
    suffix = quota_hint_suffix(999999, 999999)
    prefix_bytes = b("line one\n")
    # A budget that leaves no room at all for any of the last line.
    t = with_quota_hint(text, 999999, 999999, max_bytes=prefix_bytes + b(suffix))
    assert t == "line one\n" + suffix


def test_with_quota_hint_shortens_only_the_last_line_when_over_budget():
    text = "keep me\n" + ("x" * 170)
    t = with_quota_hint(text, 7, 60, max_bytes=180)
    assert t.startswith("keep me\n")
    assert b(t) <= 180
    assert t.endswith(quota_hint_suffix(7, 60))


def test_with_quota_hint_keeps_a_3_digit_quota_suffix_intact_under_a_narrow_budget():
    """Regression for the main.py bug where with_quota_hint was called
    without max_bytes, budgeting against commands.py's own internal 180
    default instead of the caller's configured limit -- a later generic
    truncate_utf8(..., configured_max_bytes) then cut the suffix itself
    (e.g. ending in "quota 45/9" instead of "quota 45/999 today")."""
    t = with_quota_hint(menu_text(), 45, 999, max_bytes=140)
    assert t.endswith(quota_hint_suffix(45, 999))
    assert b(t) <= 140


def test_with_quota_hint_keeps_a_3_digit_quota_suffix_intact_at_the_default_budget():
    t = with_quota_hint(menu_text(), 45, 999, max_bytes=180)
    assert t.endswith(quota_hint_suffix(45, 999))
    assert b(t) <= 180


# --- GARDEN ----------------------------------------------------------------------


def test_garden_text_shape_and_budget():
    t = garden_text(
        "Meshgarden", nodes_today=12, heard_direct=5, best_snr=8.2, guestbook_entries=9
    )
    assert t == (
        "🌱 Meshgarden\n"
        "12 nodes today\n"
        "5 confirmed direct\n"
        "Best SNR +8.2 dB\n"
        "📖 9 guestbook entries · ❓ MENU"
    )
    assert b(t) <= 180


def test_garden_text_missing_snr_is_n_a():
    t = garden_text("Meshgarden", nodes_today=0, heard_direct=0, best_snr=None, guestbook_entries=0)
    assert "Best SNR n/a" in t


def test_garden_text_truncates_only_the_name_when_the_name_is_huge():
    """Name-aware like info_text(): a 120-char service name must be
    truncated on a UTF-8 boundary so the fixed lines (nodes today, heard
    direct, best SNR, guestbook count, ❓ MENU) always survive within the
    180-byte budget."""
    t = garden_text(
        "X" * 120, nodes_today=12, heard_direct=5, best_snr=8.2, guestbook_entries=9
    )
    assert b(t) <= 180
    assert "nodes today" in t
    assert "confirmed direct" in t
    assert "Best SNR +8.2 dB" in t
    assert "guestbook entries" in t
    assert t.endswith("❓ MENU")
    assert "X" * 120 not in t


def test_garden_text_omits_furthest_direct_when_not_given():
    t = garden_text(
        "Meshgarden", nodes_today=12, heard_direct=5, best_snr=8.2, guestbook_entries=9
    )
    assert "furthest" not in t.lower()


def test_garden_text_includes_furthest_direct_when_given():
    t = garden_text(
        "Meshgarden",
        nodes_today=12,
        heard_direct=5,
        best_snr=8.2,
        guestbook_entries=9,
        furthest_direct_km=42.3,
    )
    assert "Furthest direct 42 km" in t
    assert t.endswith("❓ MENU")
    assert b(t) <= 180


def test_garden_text_worst_case_fits_the_budget():
    """Byte-budget test for the new multi-line GARDEN layout: a long name
    plus large counts and a furthest-direct distance -- the name must be the
    only part truncated to stay within 180 bytes."""
    t = garden_text(
        "X" * 200,
        nodes_today=9999,
        heard_direct=9999,
        best_snr=-99.9,
        guestbook_entries=99999,
        furthest_direct_km=99999.9,
    )
    assert b(t) <= 180
    assert t.endswith("❓ MENU")


# --- RECORD ----------------------------------------------------------------------


def test_record_text_full_shape_and_budget():
    t = record_text(
        best_snr=11.0,
        best_snr_node_id="!aabbfndr",
        max_hops=5,
        busiest_hour=20,
        total_packets=124,
    )
    assert t == (
        "🏆 Today's records\n"
        "Best SNR +11.0 dB (fndr)\n"
        "Most RX hops 5 (unk.)\n"
        "Busiest 20:00 · 124 packets 🌱"
    )
    assert b(t) <= 180


def test_record_text_missing_values_render_n_a():
    t = record_text(
        best_snr=None, best_snr_node_id=None, max_hops=None, busiest_hour=None, total_packets=0
    )
    assert "Best SNR n/a" in t and "Most RX hops n/a" in t and "Busiest n/a" in t


def test_record_text_identifies_the_node_behind_the_hop_record():
    t = record_text(
        best_snr=14.2,
        best_snr_node_id="!3f901133",
        best_snr_node_name="Hagba",
        max_hops=8,
        max_hops_node_id="!aabbccdd",
        max_hops_node_name="RelayFox",
        busiest_hour=0,
        total_packets=478,
        companion_events=True,
    )
    assert "Best RX SNR +14.2 dB (Hagba · 1133)" in t
    assert "Most RX hops 8 (RelayFox · ccdd)" in t


def test_record_meshcore_keeps_best_name_when_hop_sender_is_unknown():
    t = record_text(
        best_snr=14.2,
        best_snr_node_id="!3f901133",
        best_snr_node_name="⚓Hagba",
        max_hops=8,
        max_hops_node_id=None,
        busiest_hour=8,
        total_packets=528,
        companion_events=True,
        max_bytes=120,
    )
    assert t == (
        "🏆 Today's records\n"
        "Best RX SNR +14.2 dB (⚓Hagba · 1133)\n"
        "Most RX hops 8 (unk.)\n"
        "Busiest 08:00 · 528 events 🌱"
    )
    assert b(t) <= 120


def test_record_text_omits_furthest_observed_when_not_given():
    t = record_text(
        best_snr=11.0,
        best_snr_node_id="!aabbfndr",
        max_hops=5,
        busiest_hour=20,
        total_packets=124,
    )
    assert "furthest" not in t.lower()


def test_record_text_includes_furthest_observed_when_given():
    t = record_text(
        best_snr=11.0,
        best_snr_node_id="!aabbfndr",
        max_hops=5,
        busiest_hour=20,
        total_packets=124,
        furthest_observed_km=96.4,
        furthest_observed_node_id="!aabbf00d",
    )
    assert "Furthest observed 96 km (f00d)" in t
    assert t.endswith("🌱")
    assert b(t) <= 180


def test_record_text_omits_furthest_observed_when_only_one_of_the_pair_is_given():
    t = record_text(
        best_snr=None,
        best_snr_node_id=None,
        max_hops=None,
        busiest_hour=None,
        total_packets=0,
        furthest_observed_km=96.4,
        furthest_observed_node_id=None,
    )
    assert "furthest" not in t.lower()


def test_record_text_worst_case_fits_the_budget():
    """Byte-budget test: worst-case (largest) values across every field,
    including the furthest-observed line."""
    t = record_text(
        best_snr=-99.9,
        best_snr_node_id="!ffffffff",
        max_hops=99,
        busiest_hour=23,
        total_packets=999999,
        furthest_observed_km=99999.9,
        furthest_observed_node_id="!ffffffff",
    )
    assert b(t) <= 180


def test_record_text_shows_name_with_tail_when_known():
    text = record_text(
        best_snr=11.0,
        best_snr_node_id="!aabbccdd",
        best_snr_node_name="fndr",
        max_hops=7,
        busiest_hour=14,
        total_packets=128492,
        furthest_observed_km=1234.0,
        furthest_observed_node_id="!aabbccdd",
        furthest_observed_node_name="fndr",
    )
    assert "(fndr · ccdd)" in text
    assert b(text) <= 180


def test_record_text_falls_back_to_tail_only_without_a_name():
    text = record_text(
        best_snr=11.0,
        best_snr_node_id="!aabbfndr",
        best_snr_node_name=None,
        max_hops=5,
        busiest_hour=20,
        total_packets=124,
    )
    assert "(fndr)" in text


def test_record_text_worst_case_fits_the_budget_with_names():
    """Byte-budget test: worst-case values across every field, including
    both name-bearing lines at once."""
    t = record_text(
        best_snr=-99.9,
        best_snr_node_id="!ffffffff",
        best_snr_node_name="Xxxx",
        max_hops=99,
        busiest_hour=23,
        total_packets=999999,
        furthest_observed_km=99999.9,
        furthest_observed_node_id="!ffffffff",
        furthest_observed_node_name="Xxxx",
    )
    assert b(t) <= 180


# --- WEEK ------------------------------------------------------------------------


def test_week_text_shape_and_budget():
    t = week_text(
        total_packets=18421, senders_min=61, senders_max=128, guestbook_entries=9, replies=143
    )
    assert t == (
        "📅 Last 7 days\n"
        "18,421 packets\n"
        "61–128 senders per day\n"
        "📖 +9 guestbook entries\n"
        "💬 143 replies sent 🌱"
    )
    assert b(t) <= 180


def test_week_text_empty_range_is_n_a():
    t = week_text(
        total_packets=0, senders_min=None, senders_max=None, guestbook_entries=0, replies=0
    )
    assert "n/a senders per day" in t


def test_week_text_worst_case_fits_the_budget():
    t = week_text(
        total_packets=999_999_999,
        senders_min=0,
        senders_max=999_999,
        guestbook_entries=999_999,
        replies=999_999,
    )
    assert b(t) <= 180


def test_post_parses_the_raw_remainder_unvalidated():
    assert parse_command("POST bike lost my dog") == ParsedCommand(Command.POST, "bike lost my dog")


def test_read_parses_the_raw_remainder_unvalidated():
    assert parse_command("READ bike 2") == ParsedCommand(Command.READ, "bike 2")
    assert parse_command("READ") == ParsedCommand(Command.READ, "")


def test_boards_is_bare_only():
    assert parse_command("BOARDS") == ParsedCommand(Command.BOARDS, "")
    assert parse_command("BOARDS x") == ParsedCommand(Command.UNKNOWN, "")


def test_boards_text_lists_topics_by_count_and_fits_budget():
    text = boards_text([("bike", 12), ("lost", 5), ("events", 3), ("free", 2)])
    assert text.startswith("📌 Boards")
    assert "#bike · 12 posts" in text
    assert len(text.encode("utf-8")) <= 180


def test_boards_text_with_no_topics():
    text = boards_text([])
    assert "no topics yet" in text.lower()


def test_mail_bare_is_the_list_form():
    assert parse_command("MAIL") == ParsedCommand(Command.MAIL, "")


def test_mail_on_off_case_insensitive():
    # parse_command hands the raw remainder through unvalidated (same as
    # SEEN/SIGN) -- case is preserved here; _handle_mail (main.py) does the
    # case-insensitive ON/OFF comparison itself, not parse_command.
    assert parse_command("MAIL ON") == ParsedCommand(Command.MAIL, "ON")
    assert parse_command("mail off") == ParsedCommand(Command.MAIL, "off")


def test_mail_alias_form_is_a_mail_subcommand():
    assert parse_command("MAIL ALIAS gardenfox") == ParsedCommand(
        Command.MAIL, "ALIAS gardenfox"
    )


def test_mail_send_form_is_raw_remainder():
    assert parse_command("MAIL !aabbccdd hello there") == ParsedCommand(
        Command.MAIL, "!aabbccdd hello there"
    )


def test_mail_sent_ack_text_is_exact_and_never_reveals_optin_status():
    text = mail_sent_ack_text()
    assert text == (
        "📬 Note handed to MeshGarden. The recipient sees it only if they "
        "have MAIL ON and DM me."
    )
    assert len(text.encode("utf-8")) <= 180


def test_mail_pref_ack_text_on_and_off():
    assert "ON" in mail_pref_ack_text(True)
    assert "OFF" in mail_pref_ack_text(False)


def test_mail_empty_text():
    assert mail_empty_text() == "📬 No notes waiting 🌱"


def test_mail_list_text_fits_and_shows_newest_first():
    from meshgarden.mail import MailEntry

    entries = [
        MailEntry(2, 2000, "!11111111", "!22222222", "second note", False, None),
        MailEntry(1, 1000, "!11111111", "!22222222", "first note", False, None),
    ]
    text = mail_list_text(entries, now=2100, max_bytes=180)
    assert text.index("second note") < text.index("first note")
    assert len(text.encode("utf-8")) <= 180


def test_mail_list_shown_count_only_counts_entries_that_fit():
    """Ruling 11 / bug-fix regression: mail_list_text may only be able to
    fit a PREFIX of `entries` (newest first) within max_bytes -- the shown
    count must match exactly what got rendered, not just "how many notes
    were pending", so a note that didn't fit stays correctly reported as
    not-shown (and therefore not marked delivered by the caller)."""
    from meshgarden.mail import MailEntry

    entries = [
        MailEntry(2, 2000, "!11111111", "!22222222", "a" * 20, False, None),
        MailEntry(1, 1000, "!11111111", "!22222222", "b" * 20, False, None),
    ]
    # Only the header + the newest entry's line fits; the older one does not.
    header_and_one = len(mail_list_text(entries[:1], now=2100, max_bytes=180).encode("utf-8"))
    assert mail_list_shown_count(entries, now=2100, max_bytes=header_and_one) == 1
    assert mail_list_shown_count(entries, now=2100, max_bytes=180) == 2


@pytest.mark.parametrize("channel", [0, 1, 2, 3, 4, 5, 6, 7])
def test_staleness_composes_with_the_broadcast_rule(channel):
    """A stale-looking broadcast is rejected for being a broadcast, not for
    being stale -- staleness is only evaluated once every earlier gate
    (self-origin, broadcast, not-for-us, not-text, ...) has already passed."""
    obs = _obs(rx_time=1, now=1000, to_num=0xFFFFFFFF, channel=channel)
    r = check_eligibility(obs, my_node_num=ME, reply_ready=True, reply_ttl_seconds=60)
    assert r.eligible is False and r.reason is Reject.BROADCAST


def test_record_compact_keeps_both_known_record_holder_names():
    text = record_text(
        best_snr=14.2, best_snr_node_id="!11223344", best_snr_node_name="OakGrove",
        max_hops=8, max_hops_node_id="!aabbccdd", max_hops_node_name="RelayFox",
        busiest_hour=12, total_packets=99999, companion_events=True, max_bytes=120,
    )
    assert "OakGrove · 3344" in text
    assert "RelayFox · ccdd" in text
    assert b(text) <= 120


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("counts", [1234, 4294967295])
def test_repeater_layout_preserves_identity_and_freshness(stale, counts):
    from meshgarden.commands import repeater_text
    text = repeater_text(
        {"bat": 4123, "uptime": 183600, "nb_recv": counts, "nb_sent": counts,
         "last_rssi": -91, "last_snr": 7.5},
        label="OakGrove · ccdd", age=300, stale=stale, max_bytes=120,
    )
    assert "OakGrove · ccdd" in text
    assert "4.12V" in text and "up 2d3h" in text
    assert f"RX {counts} TX {counts}" in text
    assert "300s old" in text
    assert ("stale" in text) == stale
    assert len(text.encode()) <= 120
    assert len(text.splitlines()) >= 4


def test_garden_omits_unconfirmed_zero_direct_count():
    text = garden_text(
        "Potrero Hill", nodes_today=430, heard_direct=0,
        best_snr=14.2, guestbook_entries=1, max_bytes=120,
    )
    assert "direct" not in text
    assert "430 nodes today" in text and "Potrero Hill" in text


def test_wx_parses_and_channel_menu_name_it_within_budget():
    assert parse_command("WX").command is Command.WX
    assert parse_command("wx").command is Command.WX
    assert "WX" in channel_menu_text()
    assert len(channel_menu_text().encode("utf-8")) <= 120


# --- MC/MT mesh suffix (peer observatory) ---------------------------------


@pytest.mark.parametrize("text,command,mesh", [
    ("WHO MC", Command.WHO, "meshcore"),
    ("who mt", Command.WHO, "meshtastic"),
    ("GARDEN MC", Command.GARDEN, "meshcore"),
    ("RECORD MT", Command.RECORD, "meshtastic"),
])
def test_parse_who_mc(text, command, mesh):
    parsed = parse_command(text)
    assert (parsed.command, parsed.argument, parsed.mesh) == (command, "", mesh)


@pytest.mark.parametrize("text", ["WHO MX", "WHO MC MC", "GARDEN now", "WEEK MC", "ME MC"])
def test_parse_who_garbage_still_unknown(text):
    assert parse_command(text).command is Command.UNKNOWN


def test_parse_seen_suffix_needs_a_node():
    parsed = parse_command("SEEN !aabbccdd MC")
    assert (parsed.command, parsed.argument, parsed.mesh) == (
        Command.SEEN, "!aabbccdd", "meshcore")
    # A lone MC is a node named "MC", as before the suffix existed.
    lone = parse_command("SEEN MC")
    assert (lone.argument, lone.mesh) == ("MC", None)
    assert parse_command("SEEN bob").mesh is None


def test_mesh_suffix_is_inert_without_a_peer():
    """No peer_path: WHO/GARDEN/RECORD/SEEN parse as they did before the suffix."""
    for text in ("WHO MC", "GARDEN MT", "RECORD MC"):
        assert parse_command(text, mesh_suffix=False).command is Command.UNKNOWN
        assert parse_command(text).mesh is not None
    p = parse_command("SEEN alice MT", mesh_suffix=False)
    assert (p.command, p.argument, p.mesh) == (Command.SEEN, "alice MT", None)
