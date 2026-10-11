import datetime as dt
from collections import Counter

import pytest

from meshgarden import booktrivia, database

_VS16 = "️"
_KEYCAP = "⃣"
_ZWJ = "‍"
_SKIN_TONES = range(0x1F3FB, 0x1F400)


def b(s: str) -> int:
    return len(s.encode("utf-8"))


def count_emoji_graphemes(s: str) -> int:
    """Approximate extended-grapheme-cluster count for an emoji-only
    string: a variation selector, keycap combiner, or skin tone modifier
    merges into the previous cluster instead of starting a new one, and a
    ZWJ merges the following character into the current cluster too."""
    count = 0
    prev_was_joiner = False
    for ch in s:
        cp = ord(ch)
        if ch in (_VS16, _KEYCAP) or cp in _SKIN_TONES:
            continue
        if ch == _ZWJ:
            prev_was_joiner = True
            continue
        if prev_was_joiner:
            prev_was_joiner = False
            continue
        count += 1
    return count


# --- the puzzle table -------------------------------------------------------


def test_exactly_27_books():
    assert len(booktrivia.BOOKS) == 27


def test_no_duplicate_titles():
    titles = [p.title for p in booktrivia.BOOKS]
    assert len(titles) == len(set(titles))


@pytest.mark.parametrize("p", booktrivia.BOOKS, ids=lambda p: p.title)
def test_every_emoji_string_has_between_4_and_6_graphemes(p):
    assert 4 <= count_emoji_graphemes(p.emoji) <= 6


@pytest.mark.parametrize("p", booktrivia.BOOKS, ids=lambda p: p.title)
def test_every_hint_is_at_most_60_characters(p):
    assert len(p.hint) <= 60


@pytest.mark.parametrize("p", booktrivia.BOOKS, ids=lambda p: p.title)
def test_every_prompt_and_wrong_reply_fit_the_byte_budget(p):
    assert b(booktrivia.book_prompt_text(p)) <= 180
    assert b(booktrivia.book_wrong_text(p)) <= 180


@pytest.mark.parametrize("p", booktrivia.BOOKS, ids=lambda p: p.title)
def test_prompt_reply_shape(p):
    prompt = booktrivia.book_prompt_text(p)
    assert prompt.startswith("\U0001f4da Guess: ")
    assert p.emoji in prompt
    assert "→ reply BOOK <title>" in prompt
    assert prompt.endswith("\U0001f331 new puzzle tomorrow")


@pytest.mark.parametrize("p", booktrivia.BOOKS, ids=lambda p: p.title)
def test_wrong_reply_shape(p):
    wrong = booktrivia.book_wrong_text(p)
    assert wrong.startswith("\U0001f4da ❌ Not it")
    assert p.hint in wrong
    assert "BOOK <title>" in wrong
    assert wrong.endswith(" \U0001f331")


def test_example_reply_shape_matches_the_documented_format():
    """The spec's illustrative example (not one of the 27 puzzles) fixes the
    exact template every prompt/correct/wrong reply must follow."""
    p = booktrivia.BookPuzzle(
        "Moby-Dick", "Herman Melville", (), "\U0001f40b⚓️\U0001f624\U0001f9b5\U0001f30a", ""
    )
    assert booktrivia.book_prompt_text(p) == (
        "\U0001f4da Guess: \U0001f40b⚓️\U0001f624\U0001f9b5\U0001f30a"
        "\n→ reply BOOK <title>\n\U0001f331 new puzzle tomorrow"
    )


# --- matching ----------------------------------------------------------------


@pytest.mark.parametrize("p", booktrivia.BOOKS, ids=lambda p: p.title)
def test_every_title_matches_itself(p):
    assert booktrivia.guess_matches(p.title, p)
    assert booktrivia.guess_matches(p.title.upper(), p)
    assert booktrivia.guess_matches(p.title.lower(), p)


@pytest.mark.parametrize(
    "p", [p for p in booktrivia.BOOKS if p.aliases], ids=lambda p: p.title
)
def test_every_alias_matches(p):
    for alias in p.aliases:
        assert booktrivia.guess_matches(alias, p)


@pytest.mark.parametrize(
    "guess,title",
    [
        ("fear and loathing", "Fear and Loathing in Las Vegas"),
        ("nineteen eighty four", "1984"),
        ("the hitchhikers guide", "The Hitchhiker's Guide to the Galaxy"),
    ],
)
def test_near_misses_match(guess, title):
    puzzle = next(p for p in booktrivia.BOOKS if p.title == title)
    assert booktrivia.guess_matches(guess, puzzle)


@pytest.mark.parametrize(
    "guess,title",
    [
        ("banana bread", "1984"),
        ("the great gatsby", "Dune"),
        ("", "1984"),
        ("   ", "Dune"),
        ("22", "Catch-22"),  # too short to count under the min-4 rule
    ],
)
def test_clear_misses_do_not_match(guess, title):
    puzzle = next(p for p in booktrivia.BOOKS if p.title == title)
    assert not booktrivia.guess_matches(guess, puzzle)


def test_q_requires_an_exact_normalized_match_no_substring_rule():
    q = next(p for p in booktrivia.BOOKS if p.title == "Q")
    assert booktrivia.guess_matches("Q", q)
    assert booktrivia.guess_matches("q", q)
    assert booktrivia.guess_matches("  q  ", q)
    # A random guess merely containing the letter "q" must NOT match.
    assert not booktrivia.guess_matches("quick", q)
    assert not booktrivia.guess_matches("Iraq", q)
    assert not booktrivia.guess_matches("dune", q)


def test_normalize_guess_drops_leading_articles_punctuation_and_diacritics():
    assert booktrivia.normalize_guess("The Hitchhiker's Guide") == "hitchhikers guide"
    assert booktrivia.normalize_guess("  A   Clockwork   Orange  ") == "clockwork orange"
    assert booktrivia.normalize_guess("Catch-22") == "catch 22"
    assert booktrivia.normalize_guess("An Andventure") == "andventure"  # leading "an " dropped


# --- daily determinism -------------------------------------------------------


def test_puzzle_for_is_deterministic_for_the_same_day_and_salt():
    a = booktrivia.puzzle_for("2026-09-13", "")
    b_ = booktrivia.puzzle_for("2026-09-13", "")
    assert a == b_


def test_puzzle_for_has_no_sender_component_by_construction():
    """puzzle_for takes only (day, salt) -- there is no way to pass a
    sender id, so the puzzle is structurally the same for every visitor on
    a given local day."""
    import inspect

    params = list(inspect.signature(booktrivia.puzzle_for).parameters)
    assert params == ["day", "salt"]


def test_puzzle_for_varies_across_most_days():
    day0 = dt.date(2026, 1, 1)
    titles = [
        booktrivia.puzzle_for((day0 + dt.timedelta(days=i)).isoformat(), "salt").title
        for i in range(60)
    ]
    counts = Counter(titles)
    # Not every day need differ from its neighbor, but 60 days should cover
    # well more than a handful of distinct puzzles.
    assert len(counts) >= 15


def test_puzzle_for_salt_changes_the_result_for_some_day():
    days = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(30)]
    differs = any(
        booktrivia.puzzle_for(day.isoformat(), "") != booktrivia.puzzle_for(day.isoformat(), "salt")
        for day in days
    )
    assert differs


# --- book_stats aggregates (no node ids) --------------------------------------


def _db(tmp_path):
    conn = database.connect(str(tmp_path / "o.db"))
    database.migrate(conn)
    return conn


def test_book_key_is_the_title():
    p = booktrivia.BOOKS[0]
    assert booktrivia.book_key(p) == p.title


def test_migration_creates_book_stats_table(tmp_path):
    conn = _db(tmp_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "book_stats" in names
    assert database.get_schema_version(conn) == database.SCHEMA_VERSION


def test_record_shown_increments_shown_only(tmp_path):
    conn = _db(tmp_path)
    booktrivia.record_shown(conn, "1984", "2026-09-13")
    booktrivia.record_shown(conn, "1984", "2026-09-13")
    row = conn.execute(
        "SELECT shown, guesses, solves FROM book_stats WHERE book_key=? AND day=?",
        ("1984", "2026-09-13"),
    ).fetchone()
    assert (row["shown"], row["guesses"], row["solves"]) == (2, 0, 0)


def test_record_book_guess_counts_guesses_and_solves(tmp_path):
    conn = _db(tmp_path)
    booktrivia.record_book_guess(conn, "1984", "2026-09-13", False)
    booktrivia.record_book_guess(conn, "1984", "2026-09-13", True)
    row = conn.execute(
        "SELECT shown, guesses, solves FROM book_stats WHERE book_key=? AND day=?",
        ("1984", "2026-09-13"),
    ).fetchone()
    assert (row["shown"], row["guesses"], row["solves"]) == (0, 2, 1)


def test_alltime_totals_sum_across_days(tmp_path):
    conn = _db(tmp_path)
    booktrivia.record_book_guess(conn, "Dune", "2026-09-12", True)
    booktrivia.record_book_guess(conn, "Dune", "2026-09-13", False)
    assert booktrivia.alltime_totals(conn, "Dune") == (2, 1)


def test_alltime_totals_zero_for_an_unknown_key(tmp_path):
    conn = _db(tmp_path)
    assert booktrivia.alltime_totals(conn, "nope") == (0, 0)


def test_today_solves_reads_only_the_given_day(tmp_path):
    conn = _db(tmp_path)
    booktrivia.record_book_guess(conn, "Dune", "2026-09-12", True)
    booktrivia.record_book_guess(conn, "Dune", "2026-09-13", True)
    booktrivia.record_book_guess(conn, "Dune", "2026-09-13", True)
    assert booktrivia.today_solves(conn, "Dune", "2026-09-13") == 2
    assert booktrivia.today_solves(conn, "Dune", "2026-09-12") == 1
    assert booktrivia.today_solves(conn, "Dune", "2026-09-01") == 0


@pytest.mark.parametrize(
    "prior_guesses,prior_solves,expected",
    [
        (0, 0, False),  # 1 guess total (this one) -- below _RARE_MIN_GUESSES
        (3, 0, False),  # 4 guesses total -- still below the minimum
        (4, 0, False),  # 5 guesses, 1 solve -- exactly 20%, not STRICTLY below
        (4, 1, False),  # 5 guesses, 2 solves -- 40%, not rare
        (9, 0, True),  # 10 guesses, 1 solve -- 10% < 20%
        (19, 0, True),  # 20 guesses, 1 solve -- 5% < 20%
        (9, 1, False),  # 10 guesses, 2 solves -- exactly 20%, not STRICTLY below
    ],
)
def test_is_rare_solve_thresholds(prior_guesses, prior_solves, expected):
    assert booktrivia.is_rare_solve(prior_guesses, prior_solves) is expected


def test_book_first_solve_text_shape():
    p = next(x for x in booktrivia.BOOKS if x.title == "Dune")
    t = booktrivia.book_first_solve_text(p)
    assert t.startswith("\U0001f4da \U0001f389\U0001f947 First to crack it!")
    assert p.title in t and p.author in t
    assert t.endswith("\U0001f331 new puzzle tomorrow")
    assert len(t.encode("utf-8")) <= 180


def test_book_rare_solve_text_shape():
    p = next(x for x in booktrivia.BOOKS if x.title == "Dune")
    t = booktrivia.book_rare_solve_text(p)
    assert t.startswith("\U0001f4da \U0001f3c6 Rare catch!")
    assert p.title in t and p.author in t
    assert t.endswith("\U0001f331 few ever get this one")
    assert len(t.encode("utf-8")) <= 180


def test_book_nth_solve_text_shape():
    p = next(x for x in booktrivia.BOOKS if x.title == "Dune")
    t = booktrivia.book_nth_solve_text(p, 7)
    assert t.startswith("\U0001f4da ✅ ")
    assert p.title in t and p.author in t
    assert "#7 to solve it today" in t
    assert len(t.encode("utf-8")) <= 180


@pytest.mark.parametrize("p", booktrivia.BOOKS, ids=lambda p: p.title)
def test_every_book_tier_reply_fits_the_byte_budget(p):
    assert len(booktrivia.book_first_solve_text(p).encode("utf-8")) <= 180
    assert len(booktrivia.book_rare_solve_text(p).encode("utf-8")) <= 180
    assert len(booktrivia.book_nth_solve_text(p, 999).encode("utf-8")) <= 180


def test_book_stats_summary_is_empty_for_a_fresh_db(tmp_path):
    conn = _db(tmp_path)
    assert booktrivia.book_stats_summary(conn) == []


def test_book_stats_summary_aggregates_across_days(tmp_path):
    conn = _db(tmp_path)
    booktrivia.record_shown(conn, "1984", "2026-09-12")
    booktrivia.record_book_guess(conn, "1984", "2026-09-12", True)
    booktrivia.record_book_guess(conn, "1984", "2026-09-13", False)

    summary = booktrivia.book_stats_summary(conn)
    assert summary == [booktrivia.BookStatsRow(book_key="1984", shown=1, guesses=2, solves=1)]


# --- book_state: one solve per player per day (schema v7) --------------------


def test_has_solved_false_before_record_solved(tmp_path):
    conn = _db(tmp_path)
    key = booktrivia.player_key("!aabbccdd", "2026-09-13", "salt")
    assert booktrivia.has_solved(conn, key, "2026-09-13") is False


def test_has_solved_true_after_record_solved(tmp_path):
    conn = _db(tmp_path)
    key = booktrivia.player_key("!aabbccdd", "2026-09-13", "salt")
    booktrivia.record_solved(conn, key, "2026-09-13", 1_000_000)
    assert booktrivia.has_solved(conn, key, "2026-09-13") is True


def test_has_solved_is_scoped_to_day(tmp_path):
    conn = _db(tmp_path)
    key = booktrivia.player_key("!aabbccdd", "2026-09-13", "salt")
    booktrivia.record_solved(conn, key, "2026-09-13", 1_000_000)
    assert booktrivia.has_solved(conn, key, "2026-09-14") is False


def test_has_solved_is_scoped_to_player_key(tmp_path):
    conn = _db(tmp_path)
    key_a = booktrivia.player_key("!aabbccdd", "2026-09-13", "salt")
    key_b = booktrivia.player_key("!11223344", "2026-09-13", "salt")
    booktrivia.record_solved(conn, key_a, "2026-09-13", 1_000_000)
    assert booktrivia.has_solved(conn, key_b, "2026-09-13") is False


def test_record_solved_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    key = booktrivia.player_key("!aabbccdd", "2026-09-13", "salt")
    booktrivia.record_solved(conn, key, "2026-09-13", 1_000_000)
    booktrivia.record_solved(conn, key, "2026-09-13", 1_000_100)
    assert booktrivia.has_solved(conn, key, "2026-09-13") is True
    count = conn.execute(
        "SELECT count(*) FROM book_state WHERE player_key=? AND day=?",
        (key, "2026-09-13"),
    ).fetchone()[0]
    assert count == 1


def test_book_already_solved_text_is_the_fixed_string():
    t = booktrivia.book_already_solved_text()
    assert t == "\U0001f4da You already solved today's puzzle\n\U0001f331 new puzzle tomorrow"
    assert len(t.encode("utf-8")) == 63
    assert len(t.encode("utf-8")) <= 180


def test_prune_book_state_deletes_rows_older_than_keep_seconds(tmp_path):
    conn = _db(tmp_path)
    now = 1_000_000
    booktrivia.record_solved(conn, "old", "2026-09-01", now - 3 * 24 * 3600)
    booktrivia.record_solved(conn, "new", "2026-09-13", now - 3600)
    deleted = booktrivia.prune_book_state(conn, now, keep_seconds=48 * 3600)
    assert deleted == 1
    remaining = {r[0] for r in conn.execute("SELECT player_key FROM book_state")}
    assert remaining == {"new"}


def test_prune_book_state_runs_in_bounded_batches(tmp_path):
    conn = _db(tmp_path)
    now = 1_000_000
    for i in range(1200):
        conn.execute(
            "INSERT INTO book_state(player_key, day, solved, updated_at)"
            " VALUES (?,?,?,?)",
            (f"k{i}", "2026-09-01", 1, now - 3 * 24 * 3600),
        )
    conn.commit()
    deleted = booktrivia.prune_book_state(conn, now, keep_seconds=48 * 3600, batch=500)
    assert deleted == 1200
    assert conn.execute("SELECT count(*) FROM book_state").fetchone()[0] == 0


def test_book_state_contains_no_node_ids_or_guess_text(tmp_path):
    conn = _db(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(book_state)")}
    assert cols == {"player_key", "day", "solved", "updated_at"}


# --- BOOK SCORE: mesh-wide aggregates, never player state -----------------------


def test_book_score_text_is_all_zero_for_an_empty_day(tmp_path):
    conn = _db(tmp_path)
    t = booktrivia.book_score_text(conn, "2026-09-13")
    assert t == (
        "\U0001f4da Book scores\nToday: 0 solved · 0 guesses\n7 days: 0 solved · 0 guesses\n"
        "\U0001f331 BOOK to play"
    )
    assert b(t) <= 180


def test_book_score_text_sums_across_book_keys_for_today(tmp_path):
    conn = _db(tmp_path)
    booktrivia.record_book_guess(conn, "1984", "2026-09-13", True)
    booktrivia.record_book_guess(conn, "Dune", "2026-09-13", False)
    t = booktrivia.book_score_text(conn, "2026-09-13")
    assert "Today: 1 solved · 2 guesses" in t
    assert "7 days: 1 solved · 2 guesses" in t


def test_book_score_text_seven_day_window_excludes_the_8th_day_back(tmp_path):
    conn = _db(tmp_path)
    day = "2026-09-13"
    booktrivia.record_book_guess(conn, "1984", "2026-09-07", True)  # 6 days before day
    booktrivia.record_book_guess(conn, "1984", "2026-09-06", True)  # 7 days before: excluded
    t = booktrivia.book_score_text(conn, day)
    assert "7 days: 1 solved · 1 guesses" in t


def test_book_score_text_worst_case_fits_the_budget(tmp_path):
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO book_stats (book_key, day, shown, guesses, solves)"
        " VALUES ('X', '9999-12-31', 0, 9999, 9999)"
    )
    conn.commit()
    t = booktrivia.book_score_text(conn, "9999-12-31")
    assert b(t) <= 180


def test_book_score_text_is_a_pure_read_never_writing_book_stats(tmp_path):
    conn = _db(tmp_path)
    booktrivia.book_score_text(conn, "2026-09-13")
    assert conn.execute("SELECT count(*) FROM book_stats").fetchone()[0] == 0
