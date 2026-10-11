"""BOOK: a daily emoji book-trivia puzzle.

Same determinism shape as ICHING (see fortune.py), but keyed on the local
calendar day ALONE -- there is deliberately no sender component, so every
visitor gets the identical puzzle on the same local day; puzzle selection
itself stores no state. `puzzle_for` derives the day's `BookPuzzle` from
`sha256(f"{salt}|book|{day}")`; the `|book|` domain tag keeps this hash space
distinct from ICHING's even when both share the same operator-configured
`fortune_salt` and the same day.

`book_state` tracks a little per-player state, exactly the `wordle_state`
pattern: whether this player_key has solved today, so a repeat correct
guess after a solve is answered with a fixed already-solved reply instead
of counting again in `book_stats.solves`. `player_key` (reused from
`wordle.py`) is a truncated sha256 of `(salt, sender_node_id, day)` --
never the raw node id, never reversible to it in practice -- and no
guessed word is ever stored, only the solved flag.

Guess matching (`guess_matches`) is deliberately forgiving: the guess and
every candidate (the title, plus its aliases) are NFC-normalized, casefolded,
stripped of diacritics and punctuation (hyphens/dashes first become spaces,
so "Catch-22" normalizes to "catch 22", not "catch22"), have leading English
articles ("the"/"a"/"an") dropped, and whitespace-collapsed. A guess is
correct if it equals a candidate, or one contains the other and the shorter
side is at least 4 characters (so a trivial one- or two-character overlap
never counts). "Q" is the sole exception: its normalized title is a single
character, so it requires an exact match -- the containment rule is skipped
entirely for it, or nearly any guess would "contain" it.

The guess text itself is never part of this module's job to store or log;
callers (main.py) must never persist or log it either -- only the bounded
correct/wrong outcome may ever be counted.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

from meshgarden.wordle import player_key as player_key

_TRAILING_EMOJI = " \U0001f331"  # " 🌱"

_ARTICLE_RE = re.compile(r"^(the|a|an)\s+")
_DASH_RE = re.compile(r"[-‐-―]")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

_MIN_MATCH_CHARS = 4

_PRUNE_BATCH = 500


@dataclass(frozen=True)
class BookPuzzle:
    title: str
    author: str
    aliases: tuple[str, ...]
    emoji: str  # 4-6 emoji (grapheme clusters); a keycap digit sequence counts as one
    hint: str  # <= 60 characters


# Exactly these 27 books, nothing else, books only.
BOOKS: tuple[BookPuzzle, ...] = (
    BookPuzzle(
        "Naked Lunch",
        "William S. Burroughs",
        (),
        "\U0001f374\U0001f489\U0001f9e0\U0001f6f8\U0001f441\U0001f41c",  # 🍴💉🧠🛸👁🐜
        "Beat writer's hallucinatory, insect-ridden junk novel",
    ),
    BookPuzzle(
        "Fear and Loathing in Las Vegas",
        "Hunter S. Thompson",
        (),
        "\U0001f335\U0001f697\U0001f48a\U0001f98e\U0001f3b0\U0001f576",  # 🌵🚗💊🦎🎰🕶
        "Gonzo journalist's drug-fueled Vegas road trip",
    ),
    BookPuzzle(
        "The Illuminatus! Trilogy",
        "Robert Shea & Robert Anton Wilson",
        ("Illuminatus", "Illuminatus Trilogy"),
        "\U0001f441\U0001f53a\U0001f40d\U0001f300\U0001f310\U0001f0cf",  # 👁🔺🐍🌀🌐🃏
        "Conspiracy-soaked countercultural trilogy, all-seeing eye",
    ),
    BookPuzzle(
        "Fahrenheit 451",
        "Ray Bradbury",
        (),
        "\U0001f525\U0001f4da\U0001f692\U0001f321\U0001f4fa\U0001f415",  # 🔥📚🚒🌡📺🐕
        "Firemen burn books in a future that outlaws reading",
    ),
    BookPuzzle(
        "1984",
        "George Orwell",
        ("Nineteen Eighty-Four",),
        "\U0001f441\U0001f4fa\U0001f400⏰\U0001f6a9\U0001f97e",  # 👁📺🐀⏰🚩🥾
        "Big Brother watches; thoughtcrime in a surveillance state",
    ),
    BookPuzzle(
        "Animal Farm",
        "George Orwell",
        (),
        "\U0001f437\U0001f434\U0001f6a9\U0001f3da\U0001f411\U0001f95b",  # 🐷🐴🚩🏚🐑🥛
        "Barnyard revolt where some animals end up more equal",
    ),
    BookPuzzle(
        "V for Vendetta",
        "Alan Moore",
        (),
        "\U0001f3ad\U0001f52a\U0001f386\U0001f3db\U0001f5a4\U0001f525",  # 🎭🔪🎆🏛🖤🔥
        "Masked anarchist topples a fascist future England",
    ),
    BookPuzzle(
        "Star Wars",
        "George Lucas",
        ("A New Hope", "Star Wars: From the Adventures of Luke Skywalker"),
        "⚔\U0001f680\U0001f451\U0001f916\U0001f30c\U0001f3dc",  # ⚔🚀👑🤖🌌🏜
        "Farm boy and a rogue smuggler save a princess from an empire",
    ),
    BookPuzzle(
        "Fight Club",
        "Chuck Palahniuk",
        (),
        "\U0001f9fc\U0001f44a\U0001f3da\U0001f4a3\U0001fa78\U0001f634",  # 🧼👊🏚💣🩸😴
        "Insomniac's soap-making alter ego starts an underground club",
    ),
    BookPuzzle(
        "Foundation",
        "Isaac Asimov",
        (),
        "\U0001f30c\U0001f52e\U0001f4dc\U0001f3db⚛\U0001f9ee",  # 🌌🔮📜🏛⚛🧮
        "Psychohistory predicts an empire's fall and a new rise",
    ),
    BookPuzzle(
        "Q",
        "Luther Blissett",
        (),
        "⛪\U0001f5e1\U0001f4dc\U0001f525\U0001f464\U0001f575",  # ⛪🗡📜🔥👤🕵
        "Anonymous collective's Reformation-era heretic thriller",
    ),
    BookPuzzle(
        "Brave New World",
        "Aldous Huxley",
        (),
        "\U0001f9ec\U0001f476\U0001f48a\U0001f3ed\U0001f60a\U0001f52c",  # 🧬👶💊🏭😊🔬
        "Test-tube babies and soma keep an engineered society docile",
    ),
    BookPuzzle(
        "A Clockwork Orange",
        "Anthony Burgess",
        (),
        "\U0001f34a⚙\U0001f52a\U0001f95b\U0001f441\U0001f3bb",  # 🍊⚙🔪🥛👁🎻
        "Ultraviolent teen gang, aversion therapy, and milk-plus",
    ),
    BookPuzzle(
        "The Handmaid's Tale",
        "Margaret Atwood",
        (),
        "\U0001f534\U0001f9d5\U0001f930\U0001f6ab\U0001f441⛪",  # 🔴🧕🤰🚫👁⛪
        "Red-cloaked handmaids in a totalitarian fertility regime",
    ),
    BookPuzzle(
        "Snow Crash",
        "Neal Stephenson",
        (),
        "❄\U0001f4a5\U0001f576\U0001f5e1\U0001f4bb\U0001f355",  # ❄💥🕶🗡💻🍕
        "Pizza-delivery hacker battles a linguistic virus online",
    ),
    BookPuzzle(
        "The Dispossessed",
        "Ursula K. Le Guin",
        (),
        "\U0001f313\U0001f52c\U0001f680\U0001f335⚖\U0001f9f1",  # 🌓🔬🚀🌵⚖🧱
        "Anarchist physicist shuttles between two rival worlds",
    ),
    BookPuzzle(
        "The Hitchhiker's Guide to the Galaxy",
        "Douglas Adams",
        ("Hitchhiker's Guide", "HHGTTG", "H2G2"),
        "\U0001f30d\U0001f4a5\U0001f680\U0001f40b\U0001f4d6\U0001f401",  # 🌍💥🚀🐋📖🐁
        "Earth is demolished; hitch a ride with a towel and a guide",
    ),
    BookPuzzle(
        "The Man in the High Castle",
        "Philip K. Dick",
        (),
        "\U0001f5fa\U0001f38c\U0001f3ef\U0001f464\U0001f52e\U0001f3f4",  # 🗺🎌🏯👤🔮🏴
        "Alternate history where the Axis won the war",
    ),
    BookPuzzle(
        "Metropolis",
        "Thea von Harbou",
        (),
        "\U0001f3d9\U0001f916⚙\U0001f477\U0001f525\U0001f451",  # 🏙🤖⚙👷🔥👑
        "Silent-era sci-fi city; a robot double incites the workers",
    ),
    BookPuzzle(
        "Dune",
        "Frank Herbert",
        (),
        "\U0001f3dc\U0001f41b\U0001f451\U0001f4a7\U0001f52e⚔",  # 🏜🐛👑💧🔮⚔
        "Desert planet, giant sandworms, and a prophesied messiah",
    ),
    BookPuzzle(
        "Neuromancer",
        "William Gibson",
        (),
        "\U0001f5a5\U0001f9e0\U0001f306\U0001f576\U0001f50c\U0001f916",  # 🖥🧠🌆🕶🔌🤖
        "Burned-out hacker jacks into cyberspace for one last job",
    ),
    BookPuzzle(
        "Lord of the Flies",
        "William Golding",
        (),
        "\U0001f3dd\U0001f416\U0001f525\U0001f466\U0001f41a⚔",  # 🏝🐖🔥👦🐚⚔
        "Stranded boys descend into savagery on a desert island",
    ),
    BookPuzzle(
        "On the Road",
        "Jack Kerouac",
        (),
        "\U0001f697\U0001f6e3\U0001f3b7\U0001f37a\U0001f335⛽",  # 🚗🛣🎷🍺🌵⛽
        "Beat-era cross-country road trip, freedom and jazz",
    ),
    BookPuzzle(
        "One Flew Over the Cuckoo's Nest",
        "Ken Kesey",
        ("Cuckoo's Nest",),
        "\U0001f3e5\U0001f426\U0001f6cb\U0001f635\U0001f6ac\U0001f469‍⚕️",  # 🏥🐦🛋😵🚬👩‍⚕️
        "Rebel patient upends a domineering nurse's mental ward",
    ),
    BookPuzzle(
        "Slaughterhouse-Five",
        "Kurt Vonnegut",
        ("Slaughterhouse 5", "Slaughterhouse Five"),
        "⏳\U0001f4a3\U0001f47d\U0001f4045️⃣✈",  # ⏳💣👽🐄5️⃣✈
        "Unstuck in time, a POW witnesses the firebombing of Dresden",
    ),
    BookPuzzle(
        "Catch-22",
        "Joseph Heller",
        ("Catch 22",),
        "✈\U0001f3002️⃣2️⃣\U0001f4a3\U0001fa96",  # ✈🌀2️⃣2️⃣💣🪖
        "Bureaucratic paradox traps a WWII bomber pilot",
    ),
    BookPuzzle(
        "Trainspotting",
        "Irvine Welsh",
        (),
        "\U0001f489\U0001f682\U0001f635\U0001f3da\U0001f6bd\U0001f943",  # 💉🚂😵🏚🚽🥃
        "Edinburgh heroin addicts choose life, sort of",
    ),
)

assert len(BOOKS) == 27


def normalize_guess(text: str) -> str:
    """NFC-normalize, casefold, strip diacritics/punctuation, turn dashes
    into spaces (so "Catch-22" and "Catch 22" normalize identically), drop a
    single leading English article, and collapse whitespace. Pure text
    transform; never stores or logs anything."""
    nfc = unicodedata.normalize("NFC", text)
    folded = nfc.casefold()
    decomposed = unicodedata.normalize("NFKD", folded)
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    de_hyphenated = _DASH_RE.sub(" ", no_marks)
    no_punct = _PUNCT_RE.sub("", de_hyphenated)
    collapsed = _WHITESPACE_RE.sub(" ", no_punct).strip()
    return _ARTICLE_RE.sub("", collapsed)


def puzzle_for(day: str, salt: str) -> BookPuzzle:
    """Deterministic puzzle for one local calendar day: the day alone
    decides it (no sender component), so every visitor sees the same puzzle
    that day. `sha256(f"{salt}|book|{day}")`, first 4 bytes as a big-endian
    int, mod len(BOOKS). Pure function -- no per-day state is ever stored to
    make this determinism happen."""
    digest = hashlib.sha256(f"{salt}|book|{day}".encode()).digest()
    index = int.from_bytes(digest[:4], "big") % len(BOOKS)
    return BOOKS[index]


def guess_matches(guess: str, puzzle: BookPuzzle) -> bool:
    """Whether `guess` (raw, never stored/logged) answers `puzzle`. See the
    module docstring for the normalization and containment rules; "Q" is the
    sole exact-match-only title."""
    norm_guess = normalize_guess(guess)
    if not norm_guess:
        return False
    norm_title = normalize_guess(puzzle.title)
    if norm_title == "q":
        return norm_guess == "q"
    for candidate in (puzzle.title, *puzzle.aliases):
        norm_candidate = normalize_guess(candidate)
        if not norm_candidate:
            continue
        if norm_guess == norm_candidate:
            return True
        shorter_len = min(len(norm_guess), len(norm_candidate))
        if shorter_len >= _MIN_MATCH_CHARS and (
            norm_candidate in norm_guess or norm_guess in norm_candidate
        ):
            return True
    return False


def book_prompt_text(puzzle: BookPuzzle) -> str:
    """Today's puzzle prompt, three lines, <= 180 UTF-8 bytes."""
    return f"\U0001f4da Guess: {puzzle.emoji}\n→ reply BOOK <title>\n\U0001f331 new puzzle tomorrow"


def book_wrong_text(puzzle: BookPuzzle) -> str:
    """Reply for a wrong guess, three lines, <= 180 UTF-8 bytes. Never
    echoes the guess itself -- only the fixed hint and a nudge to try
    again."""
    return (
        f"\U0001f4da ❌ Not it\nHint: {puzzle.hint}"
        f"\n→ BOOK <title> to try again \U0001f331"
    )


# --- book_state: one solve per player per day (schema v7) --------------------

# `player_key` is `wordle.player_key` reused as-is (see its docstring): the
# same truncated sha256(salt|sender_node_id|day) scheme, never the raw node
# id, never reversible to it in practice -- imported rather than duplicated
# so BOOK and WORDLE share one hashing convention.


def has_solved(conn: sqlite3.Connection, key: str, day: str) -> bool:
    """Whether this player_key already has a recorded solve for `day`."""
    row = conn.execute(
        "SELECT solved FROM book_state WHERE player_key = ? AND day = ?", (key, day)
    ).fetchone()
    return bool(row is not None and row["solved"])


def record_solved(conn: sqlite3.Connection, key: str, day: str, now: int) -> None:
    """Marks (key, day) solved -- upserts book_state, idempotent across
    repeat calls for the same (key, day). Never stores the guess text or a
    raw node id."""
    with conn:
        conn.execute(
            "INSERT INTO book_state (player_key, day, solved, updated_at)"
            " VALUES (?, ?, 1, ?)"
            " ON CONFLICT(player_key, day) DO UPDATE SET"
            " solved = 1, updated_at = excluded.updated_at",
            (key, day, now),
        )


def prune_book_state(
    conn: sqlite3.Connection, now: int, keep_seconds: int = 48 * 3600, batch: int = _PRUNE_BATCH
) -> int:
    """Deletes book_state rows whose updated_at is older than keep_seconds,
    in bounded batches (never an unbounded DELETE) -- same style as
    wordle.prune_wordle_state. Returns rows deleted."""
    cutoff = now - keep_seconds
    total = 0
    while True:
        cur = conn.execute(
            "DELETE FROM book_state WHERE (player_key, day) IN"
            " (SELECT player_key, day FROM book_state WHERE updated_at < ? LIMIT ?)",
            (cutoff, batch),
        )
        conn.commit()
        if cur.rowcount <= 0:
            return total
        total += cur.rowcount


def book_already_solved_text() -> str:
    """Reply for any further guess after a player has already solved
    today's puzzle, two lines, <= 180 UTF-8 bytes. Fixed string -- never
    echoes the guess, never reveals the answer."""
    return "\U0001f4da You already solved today's puzzle\n\U0001f331 new puzzle tomorrow"


# --- book_stats: all-time / daily leaderboard aggregates (no node ids) -----------

# "Rare catch" threshold: an all-time solve rate below this, with at least
# _RARE_MIN_GUESSES all-time guesses recorded, so a book that has barely been
# guessed at all cannot look "rare" on a fluke.
_RARE_SOLVE_RATE = 0.20
_RARE_MIN_GUESSES = 5


def book_key(puzzle: BookPuzzle) -> str:
    """Stable key into book_stats for one puzzle. The 27-title table has no
    duplicate titles (see test_booktrivia.py), so the title itself is a
    stable, human-legible key -- never a node id, never guess text."""
    return puzzle.title


def record_shown(conn: sqlite3.Connection, key: str, day: str) -> None:
    """Counts one BOOK prompt shown (bare `BOOK`, no argument) for `key` on
    `day`. Never records who saw it."""
    with conn:
        conn.execute(
            "INSERT INTO book_stats (book_key, day, shown, guesses, solves) "
            "VALUES (?, ?, 1, 0, 0) "
            "ON CONFLICT(book_key, day) DO UPDATE SET shown = shown + 1",
            (key, day),
        )


def record_book_guess(conn: sqlite3.Connection, key: str, day: str, solved: bool) -> None:
    """Counts one BOOK <guess> for `key` on `day`: always a guess, a solve
    too when `solved`. Never records the guess text or who guessed."""
    with conn:
        conn.execute(
            "INSERT INTO book_stats (book_key, day, shown, guesses, solves) "
            "VALUES (?, ?, 0, 1, ?) "
            "ON CONFLICT(book_key, day) DO UPDATE SET "
            "guesses = guesses + 1, solves = solves + excluded.solves",
            (key, day, int(solved)),
        )


def alltime_totals(conn: sqlite3.Connection, key: str) -> tuple[int, int]:
    """(guesses, solves) summed across every day recorded for `key`."""
    row = conn.execute(
        "SELECT COALESCE(SUM(guesses), 0), COALESCE(SUM(solves), 0) "
        "FROM book_stats WHERE book_key = ?",
        (key,),
    ).fetchone()
    return (row[0], row[1])


def today_solves(conn: sqlite3.Connection, key: str, day: str) -> int:
    """Solves recorded for `key` on `day` alone (used for the "you're the
    Nth today" tier -- this INCLUDES the guess that was just recorded, so
    the caller must call this AFTER `record_book_guess`)."""
    row = conn.execute(
        "SELECT solves FROM book_stats WHERE book_key = ? AND day = ?", (key, day)
    ).fetchone()
    return row["solves"] if row is not None else 0


def is_rare_solve(prior_guesses: int, prior_solves: int) -> bool:
    """Whether the guess that just brought totals to
    (prior_guesses + 1, prior_solves + 1) counts as a "rare catch": an
    all-time solve rate below _RARE_SOLVE_RATE, with at least
    _RARE_MIN_GUESSES all-time guesses (including this one)."""
    new_guesses = prior_guesses + 1
    new_solves = prior_solves + 1
    if new_guesses < _RARE_MIN_GUESSES:
        return False
    return (new_solves / new_guesses) < _RARE_SOLVE_RATE


def book_first_solve_text(puzzle: BookPuzzle) -> str:
    """Reply for the first-ever solve of `puzzle`, three lines, <= 180
    UTF-8 bytes."""
    return (
        f"\U0001f4da \U0001f389\U0001f947 First to crack it!\n"
        f"{puzzle.title} · {puzzle.author}\n\U0001f331 new puzzle tomorrow"
    )


def book_rare_solve_text(puzzle: BookPuzzle) -> str:
    """Reply for a "rare catch" solve (see `is_rare_solve`), three lines,
    <= 180 UTF-8 bytes."""
    return (
        f"\U0001f4da \U0001f3c6 Rare catch!\n"
        f"{puzzle.title} · {puzzle.author}\n\U0001f331 few ever get this one"
    )


def book_nth_solve_text(puzzle: BookPuzzle, nth_today: int) -> str:
    """Ordinary correct-guess reply naming today's solve rank, three lines,
    <= 180 UTF-8 bytes."""
    return (
        f"\U0001f4da ✅ {puzzle.title}\n{puzzle.author}\n"
        f"\U0001f389 you're #{nth_today} to solve it today \U0001f331"
    )


def _book_range_stats(conn: sqlite3.Connection, start_day: str, end_day: str) -> tuple[int, int]:
    """(solves, guesses) summed across every book_key for days in
    [start_day, end_day] inclusive. `day` strings are `YYYY-MM-DD`, so
    lexicographic comparison sorts the same as chronological order."""
    row = conn.execute(
        "SELECT COALESCE(SUM(solves), 0), COALESCE(SUM(guesses), 0)"
        " FROM book_stats WHERE day >= ? AND day <= ?",
        (start_day, end_day),
    ).fetchone()
    return (row[0], row[1])


def book_score_text(conn: sqlite3.Connection, day: str) -> str:
    """`BOOK SCORE`/`SCORES` reply, four lines, <= 180 UTF-8 bytes:
    mesh-wide aggregates summed across every `book_key` (only the day's own
    puzzle key has rows for a given day, but summing is correct regardless)
    -- never node ids or guess text. Today's line covers `day` alone; the
    7-day line covers the rolling window ending on `day` (today + 6
    previous local days). Pure read -- never touches book_state and never
    counts as a guess or a shown."""
    today_solves, today_guesses = _book_range_stats(conn, day, day)
    start_day = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
    week_solves, week_guesses = _book_range_stats(conn, start_day, day)
    return (
        "\U0001f4da Book scores\n"
        f"Today: {today_solves} solved · {today_guesses} guesses\n"
        f"7 days: {week_solves} solved · {week_guesses} guesses\n"
        "\U0001f331 BOOK to play"
    )


@dataclass(frozen=True)
class BookStatsRow:
    book_key: str
    shown: int
    guesses: int
    solves: int


def book_stats_summary(conn: sqlite3.Connection) -> list[BookStatsRow]:
    """All-time per-book aggregates (book_stats_alltime view), for the CLI.
    No node ids, no guess text -- only bounded counts per book title."""
    rows = conn.execute(
        "SELECT book_key, shown, guesses, solves FROM book_stats_alltime "
        "ORDER BY book_key"
    ).fetchall()
    return [
        BookStatsRow(
            book_key=r["book_key"],
            shown=r["shown"] or 0,
            guesses=r["guesses"] or 0,
            solves=r["solves"] or 0,
        )
        for r in rows
    ]
