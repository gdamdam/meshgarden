import datetime as dt
import random
from collections import Counter

import pytest

from meshgarden import database, wordle


def b(s: str) -> int:
    return len(s.encode("utf-8"))


def _db(tmp_path):
    conn = database.connect(str(tmp_path / "o.db"))
    database.migrate(conn)
    return conn


# --- word list ---------------------------------------------------------------


def test_word_list_size_is_around_400():
    assert 390 <= len(wordle.WORD_LIST) <= 410


def test_word_list_has_no_duplicates():
    assert len(wordle.WORD_LIST) == len(set(wordle.WORD_LIST))


@pytest.mark.parametrize("w", wordle.WORD_LIST)
def test_every_word_is_five_lowercase_ascii_letters(w):
    assert len(w) == 5
    assert w.isascii()
    assert w.isalpha()
    assert w.islower()


# --- answer_for: daily determinism -------------------------------------------


def test_answer_for_is_deterministic_for_the_same_day_and_salt():
    a = wordle.answer_for("2026-09-13", "")
    b_ = wordle.answer_for("2026-09-13", "")
    assert a == b_
    assert a in wordle.WORD_LIST


def test_answer_for_has_no_sender_component_by_construction():
    import inspect

    params = list(inspect.signature(wordle.answer_for).parameters)
    assert params == ["day", "salt"]


def test_answer_for_usually_varies_across_consecutive_days():
    day0 = dt.date(2026, 1, 1)
    answers = [
        wordle.answer_for((day0 + dt.timedelta(days=i)).isoformat(), "salt") for i in range(60)
    ]
    counts = Counter(answers)
    # Not every day need differ from its neighbor, but 60 days should cover
    # well more than a handful of distinct answers out of ~400 candidates.
    assert len(counts) >= 45


def test_answer_for_salt_changes_the_result_for_some_day():
    days = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(30)]
    differs = any(
        wordle.answer_for(day.isoformat(), "") != wordle.answer_for(day.isoformat(), "salt")
        for day in days
    )
    assert differs


# --- normalize_guess -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("crane", "crane"),
        ("CRANE", "crane"),
        ("  crane  ", "crane"),
        ("CrAnE", "crane"),
        ("\tabbey\n", "abbey"),
    ],
)
def test_normalize_guess_accepts_valid_five_letter_guesses(raw, expected):
    assert wordle.normalize_guess(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "cra", "cranes", "cr4ne", "cr-ne", "hello world", "café!", "12345", "ab"],
)
def test_normalize_guess_rejects_invalid_input(raw):
    assert wordle.normalize_guess(raw) is None


# --- score: classic feedback cases --------------------------------------------


def test_score_exact_match_is_all_green():
    assert wordle.score("abbey", "abbey") == "🟩🟩🟩🟩🟩"


def test_score_babes_against_abbey_handles_duplicate_letters():
    assert wordle.score("babes", "abbey") == "🟨🟨🟩🟩⬜"


def test_score_kebab_against_abbey_handles_duplicate_letters():
    assert wordle.score("kebab", "abbey") == "⬜🟨🟩🟨🟨"


def test_score_eerie_against_crane_limits_yellow_to_remaining_count():
    assert wordle.score("eerie", "crane") == "⬜⬜🟨⬜🟩"


def test_score_with_no_shared_letters_is_all_white():
    assert wordle.score("clump", "shred") == "⬜⬜⬜⬜⬜"


# --- reply formatters: exact shape + byte budget -------------------------------


def test_wordle_prompt_text_format_and_budget():
    day = "2026-09-13"
    t = wordle.wordle_prompt_text(day)
    assert t == (
        "\U0001f7e9 Wordle 2026-09-13\n5 letters · 6 tries\n→ reply WORDLE <guess> \U0001f331"
    )
    assert b(t) <= 180


def test_wordle_invalid_text_format_and_budget():
    t = wordle.wordle_invalid_text()
    assert t == (
        "\U0001f7e9 Wordle\nGuess must be 5 letters a–z\n→ WORDLE <guess> \U0001f331"
    )
    assert b(t) <= 180


def test_wordle_feedback_text_format_and_budget():
    squares = "⬜🟨⬜🟩⬜"
    t = wordle.wordle_feedback_text(squares, "crane", 3, 6)
    assert t == (
        "\U0001f7e9 Wordle · try 3/6\nC R A N E\n⬜ 🟨 ⬜ N ⬜\n→ WORDLE <guess> \U0001f331"
    )
    assert b(t) <= 180


def test_wordle_solved_text_format_and_budget():
    t = wordle.wordle_solved_text(3, 6, "crane", solved_today=12, avg_guesses=3.8)
    assert t == (
        "\U0001f389 Solved in 3/6 · CRANE\n12 solved today · avg 3.8\n\U0001f331 new word tomorrow"
    )
    assert b(t) <= 180


def test_wordle_out_text_format_and_budget():
    t = wordle.wordle_out_text("crane")
    assert t == "❌ Out of guesses\nIt was CRANE\n\U0001f331 new word tomorrow"
    assert b(t) <= 180


def test_wordle_done_text_format_and_budget():
    day = "2026-09-13"
    t = wordle.wordle_done_text(day)
    assert t == "\U0001f7e9 Wordle 2026-09-13\nYou're done for today\n\U0001f331 new word tomorrow"
    assert b(t) <= 180


def test_wordle_replies_worst_case_fit_the_budget():
    """Byte-budget tests for every new WORDLE layout at worst-case inputs:
    a max-length day string, max guess count, and large daily aggregates."""
    day = "9999-99-99"
    assert b(wordle.wordle_prompt_text(day)) <= 180
    assert b(wordle.wordle_invalid_text()) <= 180
    assert b(wordle.wordle_feedback_text("⬜⬜⬜⬜⬜", "zzzzz", 6, 6)) <= 180
    assert (
        b(wordle.wordle_solved_text(6, 6, "zzzzz", solved_today=999999, avg_guesses=5.999))
        <= 180
    )
    assert b(wordle.wordle_out_text("zzzzz")) <= 180
    assert b(wordle.wordle_done_text(day)) <= 180


# --- player_key: no raw node ids ----------------------------------------------


def test_player_key_is_deterministic_and_32_chars():
    k1 = wordle.player_key("!aabbccdd", "2026-09-13", "salt")
    k2 = wordle.player_key("!aabbccdd", "2026-09-13", "salt")
    assert k1 == k2
    assert len(k1) == 32
    assert "!aabbccdd" not in k1
    assert "aabbccdd" not in k1


def test_player_key_differs_by_sender_day_or_salt():
    base = wordle.player_key("!aaaaaaaa", "2026-09-13", "salt")
    assert wordle.player_key("!bbbbbbbb", "2026-09-13", "salt") != base
    assert wordle.player_key("!aaaaaaaa", "2026-09-14", "salt") != base
    assert wordle.player_key("!aaaaaaaa", "2026-09-13", "other") != base


# --- migration -----------------------------------------------------------------


def test_migration_creates_wordle_tables_on_a_fresh_db(tmp_path):
    conn = _db(tmp_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"wordle_state", "wordle_daily"} <= names
    assert database.get_schema_version(conn) == database.SCHEMA_VERSION


def test_migration_applies_atomically_onto_an_existing_migration_1_db(tmp_path):
    p = str(tmp_path / "o.db")
    conn = database.connect(p)
    conn.executescript(
        f"BEGIN;\n{database.MIGRATION_1_SQL}\nPRAGMA user_version = 1;\nCOMMIT;"
    )
    assert database.migrate(conn) == database.SCHEMA_VERSION
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"wordle_state", "wordle_daily"} <= names


def test_migration_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    assert database.migrate(conn) == database.SCHEMA_VERSION


# --- get_state / record_guess ----------------------------------------------------


def test_get_state_returns_none_when_absent(tmp_path):
    conn = _db(tmp_path)
    assert wordle.get_state(conn, "somekey", "2026-09-13") is None


def test_record_guess_creates_row_and_increments_guesses(tmp_path):
    conn = _db(tmp_path)
    key, day = "somekey", "2026-09-13"
    wordle.record_guess(conn, key, day, False, 1000)
    state = wordle.get_state(conn, key, day)
    assert state["guesses"] == 1
    assert state["solved"] == 0
    assert state["updated_at"] == 1000

    wordle.record_guess(conn, key, day, False, 1001)
    state = wordle.get_state(conn, key, day)
    assert state["guesses"] == 2
    assert state["solved"] == 0

    wordle.record_guess(conn, key, day, True, 1002)
    state = wordle.get_state(conn, key, day)
    assert state["guesses"] == 3
    assert state["solved"] == 1
    assert state["updated_at"] == 1002


def test_record_guess_is_scoped_per_day(tmp_path):
    conn = _db(tmp_path)
    key = "somekey"
    wordle.record_guess(conn, key, "2026-09-13", False, 1000)
    wordle.record_guess(conn, key, "2026-09-14", False, 1001)
    assert wordle.get_state(conn, key, "2026-09-13")["guesses"] == 1
    assert wordle.get_state(conn, key, "2026-09-14")["guesses"] == 1


# --- wordle_daily aggregates -----------------------------------------------------


def test_wordle_daily_counts_players_once_and_solves_with_guess_total(tmp_path):
    conn = _db(tmp_path)
    day = "2026-09-13"
    wordle.record_guess(conn, "k1", day, False, 100)
    wordle.record_guess(conn, "k1", day, True, 101)  # k1 solves on its 2nd guess
    wordle.record_guess(conn, "k2", day, False, 102)
    row = conn.execute(
        "SELECT players, solves, total_guesses_on_solve FROM wordle_daily WHERE day=?", (day,)
    ).fetchone()
    assert row["players"] == 2
    assert row["solves"] == 1
    assert row["total_guesses_on_solve"] == 2


def test_wordle_daily_never_stores_node_ids_or_words(tmp_path):
    conn = _db(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(wordle_daily)")}
    assert cols == {"day", "players", "solves", "total_guesses_on_solve"}


# --- prune_wordle_state ----------------------------------------------------------


def test_prune_wordle_state_deletes_rows_older_than_keep_seconds(tmp_path):
    conn = _db(tmp_path)
    now = 1_000_000
    wordle.record_guess(conn, "old", "2026-09-01", False, now - 3 * 24 * 3600)
    wordle.record_guess(conn, "new", "2026-09-13", False, now - 3600)
    deleted = wordle.prune_wordle_state(conn, now, keep_seconds=48 * 3600)
    assert deleted == 1
    remaining = {r[0] for r in conn.execute("SELECT player_key FROM wordle_state")}
    assert remaining == {"new"}


def test_prune_wordle_state_runs_in_bounded_batches(tmp_path):
    conn = _db(tmp_path)
    now = 1_000_000
    for i in range(1200):
        conn.execute(
            "INSERT INTO wordle_state(player_key, day, guesses, solved, updated_at)"
            " VALUES (?,?,?,?,?)",
            (f"k{i}", "2026-09-01", 1, 0, now - 3 * 24 * 3600),
        )
    conn.commit()
    deleted = wordle.prune_wordle_state(conn, now, keep_seconds=48 * 3600, batch=500)
    assert deleted == 1200
    assert conn.execute("SELECT count(*) FROM wordle_state").fetchone()[0] == 0


# --- wordle_state contains no node ids and no words ------------------------------


def test_wordle_state_rows_never_contain_a_node_id_or_a_word(tmp_path):
    conn = _db(tmp_path)
    day = "2026-09-13"
    key = wordle.player_key("!aabbccdd", day, "salt")
    wordle.record_guess(conn, key, day, False, 1000)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(wordle_state)")]
    assert set(cols) == {"player_key", "day", "guesses", "solved", "updated_at"}
    rows = conn.execute("SELECT * FROM wordle_state").fetchall()
    assert rows
    for row in rows:
        assert "aabbccdd" not in row["player_key"]
        for value in tuple(row):
            if isinstance(value, str):
                assert "!" not in value
                assert value not in wordle.WORD_LIST


# --- play: the pure driver -------------------------------------------------------


def test_play_empty_argument_returns_prompt(tmp_path):
    conn = _db(tmp_path)
    out = wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument="", day="2026-09-13", salt="s", now=1000
    )
    assert out == wordle.wordle_prompt_text("2026-09-13")


def test_play_whitespace_only_argument_returns_prompt(tmp_path):
    conn = _db(tmp_path)
    out = wordle.play(
        conn,
        sender_node_id="!aabbccdd",
        text_argument="   ",
        day="2026-09-13",
        salt="s",
        now=1000,
    )
    assert out == wordle.wordle_prompt_text("2026-09-13")


def test_play_invalid_guess_returns_invalid_text(tmp_path):
    conn = _db(tmp_path)
    out = wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument="hi", day="2026-09-13", salt="s", now=1000
    )
    assert out == wordle.wordle_invalid_text()


def test_play_correct_guess_returns_solved_text(tmp_path):
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    answer = wordle.answer_for(day, salt)
    out = wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument=answer, day=day, salt=salt, now=1000
    )
    assert out == wordle.wordle_solved_text(
        1, wordle.MAX_GUESSES, answer, solved_today=1, avg_guesses=1.0
    )


def test_play_wrong_guess_returns_feedback_text(tmp_path):
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    answer = wordle.answer_for(day, salt)
    guess = next(w for w in wordle.WORD_LIST if w != answer)
    out = wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument=guess, day=day, salt=salt, now=1000
    )
    squares = wordle.score(guess, answer)
    assert out == wordle.wordle_feedback_text(squares, guess, 1, wordle.MAX_GUESSES)


def test_play_after_solve_returns_done(tmp_path):
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    answer = wordle.answer_for(day, salt)
    wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument=answer, day=day, salt=salt, now=1000
    )
    out = wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument=answer, day=day, salt=salt, now=1001
    )
    assert out == wordle.wordle_done_text(day)


def test_play_enforces_the_six_guess_limit_then_done(tmp_path):
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    answer = wordle.answer_for(day, salt)
    wrong_words = [w for w in wordle.WORD_LIST if w != answer][:6]
    outs = [
        wordle.play(
            conn, sender_node_id="!aabbccdd", text_argument=w, day=day, salt=salt, now=1000 + i
        )
        for i, w in enumerate(wrong_words)
    ]
    assert outs[-1] == wordle.wordle_out_text(answer)
    out7 = wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument=wrong_words[0], day=day, salt=salt, now=2000
    )
    assert out7 == wordle.wordle_done_text(day)


def test_max_guesses_is_six():
    assert wordle.MAX_GUESSES == 6


def test_play_state_is_independent_per_player(tmp_path):
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    answer = wordle.answer_for(day, salt)
    wordle.play(
        conn, sender_node_id="!aaaaaaaa", text_argument=answer, day=day, salt=salt, now=1000
    )
    guess2 = next(w for w in wordle.WORD_LIST if w != answer)
    out2 = wordle.play(
        conn, sender_node_id="!bbbbbbbb", text_argument=guess2, day=day, salt=salt, now=1001
    )
    squares2 = wordle.score(guess2, answer)
    assert out2 == wordle.wordle_feedback_text(squares2, guess2, 1, wordle.MAX_GUESSES)


def test_play_replies_always_fit_the_byte_budget(tmp_path):
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    rng = random.Random(42)
    for i in range(200):
        sender = f"!{i:08x}"
        guess = rng.choice(wordle.WORD_LIST)
        out = wordle.play(
            conn, sender_node_id=sender, text_argument=guess, day=day, salt=salt, now=1000 + i
        )
        assert len(out.encode("utf-8")) <= 180


def test_play_solved_reply_includes_the_daily_aggregate(tmp_path):
    """Two players solve the same day; the second solver's reply reports
    solved_today=2 and the average guesses-to-solve across both."""
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    answer = wordle.answer_for(day, salt)
    out1 = wordle.play(
        conn, sender_node_id="!aaaaaaaa", text_argument=answer, day=day, salt=salt, now=1000
    )
    assert out1 == wordle.wordle_solved_text(
        1, wordle.MAX_GUESSES, answer, solved_today=1, avg_guesses=1.0
    )

    wrong = next(w for w in wordle.WORD_LIST if w != answer)
    wordle.play(conn, sender_node_id="!bbbbbbbb", text_argument=wrong, day=day, salt=salt, now=1001)
    out2 = wordle.play(
        conn, sender_node_id="!bbbbbbbb", text_argument=answer, day=day, salt=salt, now=1002
    )
    assert out2 == wordle.wordle_solved_text(
        2, wordle.MAX_GUESSES, answer, solved_today=2, avg_guesses=1.5
    )


# --- WORDLE CLUE: costs a try, reveals one letter --------------------------------


def test_wordle_clue_text_format_and_budget():
    t = wordle.wordle_clue_text("_ _ E _ _", 3, 6)
    assert t == "\U0001f7e9 Wordle · clue used try 3/6\n_ _ E _ _\n→ WORDLE <guess> \U0001f331"
    assert b(t) <= 180


def test_wordle_gave_up_text_format_and_budget():
    t = wordle.wordle_gave_up_text("crane")
    assert t == (
        "🏳️ Gave up · it was CRANE\nCLUE costs a try, GIVEUP ends the day\n"
        "\U0001f331 new word tomorrow"
    )
    assert b(t) <= 180


def test_wordle_clue_replies_worst_case_fit_the_budget():
    assert b(wordle.wordle_clue_text("Z _ _ _ _", 6, 6)) <= 180
    assert b(wordle.wordle_gave_up_text("zzzzz")) <= 180


def test_clue_with_no_prior_state_reveals_letter_1_and_costs_a_guess(tmp_path):
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    answer = wordle.answer_for(day, salt)
    out = wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument="CLUE", day=day, salt=salt, now=1000
    )
    pattern = " ".join(answer[i].upper() if i == 0 else "_" for i in range(5))
    assert out == wordle.wordle_clue_text(pattern, 1, wordle.MAX_GUESSES)

    key = wordle.player_key("!aabbccdd", day, salt)
    state = wordle.get_state(conn, key, day)
    assert state["guesses"] == 1
    assert state["solved"] == 0


def test_clue_after_two_wrong_guesses_reveals_letter_3(tmp_path):
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    answer = wordle.answer_for(day, salt)
    wrong_words = [w for w in wordle.WORD_LIST if w != answer][:2]
    for i, w in enumerate(wrong_words):
        wordle.play(
            conn, sender_node_id="!aabbccdd", text_argument=w, day=day, salt=salt, now=1000 + i
        )

    out = wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument="clue", day=day, salt=salt, now=1010
    )
    pattern = " ".join(answer[i].upper() if i == 2 else "_" for i in range(5))
    assert out == wordle.wordle_clue_text(pattern, 3, wordle.MAX_GUESSES)

    key = wordle.player_key("!aabbccdd", day, salt)
    assert wordle.get_state(conn, key, day)["guesses"] == 3


def test_clue_as_the_sixth_try_reveals_the_whole_word_then_done(tmp_path):
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    answer = wordle.answer_for(day, salt)
    wrong_words = [w for w in wordle.WORD_LIST if w != answer][:5]
    for i, w in enumerate(wrong_words):
        wordle.play(
            conn, sender_node_id="!aabbccdd", text_argument=w, day=day, salt=salt, now=1000 + i
        )

    out = wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument="CLUE", day=day, salt=salt, now=1010
    )
    assert out == wordle.wordle_out_text(answer)

    out2 = wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument="crane", day=day, salt=salt, now=1020
    )
    assert out2 == wordle.wordle_done_text(day)


def test_clue_after_solving_returns_done_and_does_not_change_state(tmp_path):
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    answer = wordle.answer_for(day, salt)
    wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument=answer, day=day, salt=salt, now=1000
    )
    key = wordle.player_key("!aabbccdd", day, salt)
    before = dict(wordle.get_state(conn, key, day))

    out = wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument="  clue  ", day=day, salt=salt, now=1001
    )
    assert out == wordle.wordle_done_text(day)
    assert dict(wordle.get_state(conn, key, day)) == before


def test_clue_keyword_is_never_treated_as_a_five_letter_guess():
    assert wordle.normalize_guess("CLUE!") is None
    assert wordle.normalize_guess("clues") == "clues"


# --- WORDLE GIVEUP: reveals the answer, ends the day, never a solve --------------


@pytest.mark.parametrize("text", ["GIVEUP", "give up", "  I GIVE UP  ", "I   Give   Up"])
def test_giveup_variants_reveal_the_answer_and_mark_done(tmp_path, text):
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    answer = wordle.answer_for(day, salt)
    out = wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument=text, day=day, salt=salt, now=1000
    )
    assert out == wordle.wordle_gave_up_text(answer)

    key = wordle.player_key("!aabbccdd", day, salt)
    state = wordle.get_state(conn, key, day)
    assert state["guesses"] == wordle.MAX_GUESSES
    assert state["solved"] == 0

    row = conn.execute("SELECT solves FROM wordle_daily WHERE day=?", (day,)).fetchone()
    assert row["solves"] == 0

    out2 = wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument="crane", day=day, salt=salt, now=1001
    )
    assert out2 == wordle.wordle_done_text(day)


def test_giveup_increments_players_once_for_a_first_time_player(tmp_path):
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument="GIVEUP", day=day, salt=salt, now=1000
    )
    row = conn.execute("SELECT players FROM wordle_daily WHERE day=?", (day,)).fetchone()
    assert row["players"] == 1

    # A second give-up call would only occur via the done branch (no state
    # change) since the player is already done after the first; players
    # must not be incremented again.
    wordle.play(
        conn, sender_node_id="!aabbccdd", text_argument="GIVEUP", day=day, salt=salt, now=1001
    )
    row = conn.execute("SELECT players FROM wordle_daily WHERE day=?", (day,)).fetchone()
    assert row["players"] == 1


# --- wordle_stats_summary ----------------------------------------------------------


def test_wordle_stats_summary_is_empty_for_a_fresh_db(tmp_path):
    conn = _db(tmp_path)
    assert wordle.wordle_stats_summary(conn) == []


def test_wordle_stats_summary_reports_newest_days_first(tmp_path):
    conn = _db(tmp_path)
    wordle.record_guess(conn, "k1", "2026-09-11", True, 100)
    wordle.record_guess(conn, "k2", "2026-09-12", True, 200)
    wordle.record_guess(conn, "k2", "2026-09-12", False, 201)  # k2's own 2nd, no 2nd solve
    wordle.record_guess(conn, "k3", "2026-09-13", True, 300)

    summary = wordle.wordle_stats_summary(conn)
    assert [row.day for row in summary] == ["2026-09-13", "2026-09-12", "2026-09-11"]
    assert summary[0] == wordle.WordleDaySummary(
        day="2026-09-13", players=1, solves=1, avg_guesses=1.0
    )
    assert summary[1] == wordle.WordleDaySummary(
        day="2026-09-12", players=1, solves=1, avg_guesses=1.0
    )


def test_wordle_stats_summary_respects_the_days_limit(tmp_path):
    conn = _db(tmp_path)
    for i in range(10):
        wordle.record_guess(conn, "k", f"2026-09-{i + 1:02d}", True, 100 + i)
    summary = wordle.wordle_stats_summary(conn, days=3)
    assert len(summary) == 3
    assert [row.day for row in summary] == ["2026-09-10", "2026-09-09", "2026-09-08"]


# --- WORDLE SCORE: mesh-wide aggregates, never player state --------------------


def test_wordle_score_text_reports_nobody_yet_for_an_empty_day(tmp_path):
    conn = _db(tmp_path)
    t = wordle.wordle_score_text(conn, "2026-09-13")
    assert t == (
        "\U0001f7e9 Wordle scores\nToday: nobody yet\n7 days: 0 solved of 0\n"
        "\U0001f331 WORDLE to play"
    )
    assert b(t) <= 180


def test_wordle_score_text_reports_todays_row_and_omits_avg_when_no_solves(tmp_path):
    conn = _db(tmp_path)
    wordle.record_guess(conn, "k1", "2026-09-13", False, 100)
    t = wordle.wordle_score_text(conn, "2026-09-13")
    assert t == (
        "\U0001f7e9 Wordle scores\nToday: 0 solved of 1 players\n7 days: 0 solved of 1\n"
        "\U0001f331 WORDLE to play"
    )


def test_wordle_score_text_shows_todays_solves_and_avg(tmp_path):
    conn = _db(tmp_path)
    wordle.record_guess(conn, "k1", "2026-09-13", True, 100)
    wordle.record_guess(conn, "k2", "2026-09-13", False, 101)
    wordle.record_guess(conn, "k2", "2026-09-13", True, 102)
    t = wordle.wordle_score_text(conn, "2026-09-13")
    assert t == (
        "\U0001f7e9 Wordle scores\nToday: 2 solved of 2 players · avg 1.5\n"
        "7 days: 2 solved of 2 · avg 1.5\n\U0001f331 WORDLE to play"
    )


def test_wordle_score_text_seven_day_window_excludes_the_8th_day_back(tmp_path):
    conn = _db(tmp_path)
    wordle.record_guess(conn, "k1", "2026-09-07", True, 1)  # exactly 6 days before day
    wordle.record_guess(conn, "k2", "2026-09-06", True, 1)  # 7 days before day: excluded
    day = "2026-09-13"
    t = wordle.wordle_score_text(conn, day)
    assert "7 days: 1 solved of 1" in t


def test_wordle_score_text_worst_case_fits_the_budget(tmp_path):
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO wordle_daily (day, players, solves, total_guesses_on_solve)"
        " VALUES ('9999-12-31', 9999, 9999, 59994)"
    )
    conn.commit()
    t = wordle.wordle_score_text(conn, "9999-12-31")
    assert b(t) <= 180


def test_score_variants_return_the_score_text_without_touching_state(tmp_path):
    conn = _db(tmp_path)
    day, salt = "2026-09-13", "s"
    for text in ("SCORE", "scores", "  Score  "):
        out = wordle.play(
            conn, sender_node_id="!aabbccdd", text_argument=text, day=day, salt=salt, now=1000
        )
        assert out == wordle.wordle_score_text(conn, day)

    key = wordle.player_key("!aabbccdd", day, salt)
    assert wordle.get_state(conn, key, day) is None
    row = conn.execute("SELECT * FROM wordle_daily WHERE day=?", (day,)).fetchone()
    assert row is None
