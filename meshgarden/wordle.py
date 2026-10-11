"""WORDLE: a daily 5-letter word guessing game with classic square feedback.

Same determinism shape as BOOK (see booktrivia.py): `answer_for` derives the
day's answer word from `sha256(f"{salt}|wordle|{day}")` alone -- no sender
component -- so every player gets the identical word on the same local day.
The `|wordle|` domain tag keeps this hash space distinct from ICHING/BOOK
even when they share the same operator-configured `fortune_salt` and day.

Unlike BOOK, WORDLE tracks a little per-player state across a player's
guesses for the day (how many guesses used, whether solved) so it can
enforce the 6-guess cap and give feedback squares. That state is
deliberately minimal and never reversible to a node id: `player_key` is a
truncated sha256 of `(salt, sender_node_id, day)`, and no guessed word is
ever stored -- only counts and a solved flag. `wordle_daily` aggregates
players/solves/total-guesses-on-solve per day, again with no node ids and
no words.

Guess validation (`normalize_guess`) accepts any 5-letter alphabetic string
-- there is no dictionary check, so a guess is scored even if it is not
itself a real word (as long as it is exactly 5 ASCII letters).
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

_TRAILING_EMOJI = " \U0001f331"  # " 🌱"

MAX_GUESSES = 6

_PRUNE_BATCH = 500

# ~400 ORIGINAL common, family-friendly, unambiguous 5-letter English words:
# lowercase, no proper nouns, plurals ending in "s" avoided except a
# handful of non-plural nouns that happen to end in "s" (e.g. "cross",
# "guess") where no plural sense is involved.
WORD_LIST: tuple[str, ...] = (
    "about", "abuse", "adapt", "adopt", "after", "again", "agree", "alarm",
    "alert", "align", "alive", "alone", "aloud", "amber", "ample", "angel",
    "angle", "apple", "arena", "arise", "aroma", "arrow", "asset", "awake",
    "aware", "badge", "basic", "batch", "beard", "begin", "bench", "bison",
    "blank", "blaze", "block", "blood", "blues", "blush", "boast", "boost",
    "bound", "brand", "brave", "break", "brick", "bring", "broil", "brook",
    "brown", "build", "cabin", "candy", "caper", "carve", "cause", "chair",
    "charm", "chase", "cheer", "chest", "child", "chimp", "chord", "claim",
    "clash", "clean", "climb", "cloak", "close", "cloud", "coach", "cocoa",
    "comic", "coral", "could", "court", "craft", "crash", "cream", "crest",
    "cross", "crown", "crush", "curve", "daisy", "dealt", "decor", "delta",
    "diary", "donor", "drain", "drawn", "dress", "drift", "drive", "drove",
    "eager", "earth", "eaten", "eight", "elder", "email", "emote", "empty",
    "enter", "error", "ether", "every", "exile", "extra", "faint", "faith",
    "false", "fault", "feast", "fever", "field", "fight", "first", "flame",
    "flash", "flesh", "flick", "flock", "floor", "flour", "forth", "found",
    "fresh", "front", "fruit", "gauge", "geese", "giant", "glare", "gleam",
    "glory", "going", "grade", "grand", "graph", "grass", "great", "green",
    "grief", "grind", "growl", "guess", "guide", "happy", "haste", "haven",
    "honey", "horse", "house", "humid", "ideal", "index", "inner", "inter",
    "issue", "ivory", "jewel", "joker", "joust", "juice", "karma", "knead",
    "knife", "known", "lapse", "large", "latch", "laugh", "learn", "least",
    "legal", "level", "limit", "liver", "loose", "lover", "loyal", "lunar",
    "lyric", "major", "mango", "march", "match", "medal", "media", "mercy",
    "merit", "meter", "mimic", "miner", "mirth", "moist", "month", "mound",
    "mount", "mouse", "moved", "music", "myrrh", "nerve", "newer", "ninth",
    "noise", "novel", "nudge", "nylon", "obese", "often", "optic", "organ",
    "otter", "outer", "ozone", "paint", "panic", "parch", "party", "pause",
    "peach", "pedal", "perch", "phase", "pinch", "pitch", "pixel", "plumb",
    "poise", "pound", "press", "pride", "print", "prize", "proud", "prove",
    "punch", "quack", "quart", "quest", "quiet", "quirk", "radar", "raise",
    "range", "rapid", "reach", "realm", "refer", "relax", "remit", "reply",
    "reset", "retro", "rhyme", "ridge", "rinse", "risky", "river", "robot",
    "rogue", "rouge", "route", "royal", "ruler", "rusty", "salad", "salty",
    "sandy", "sauce", "scare", "scary", "scoop", "scrap", "sense", "setup",
    "shade", "shake", "shape", "shark", "shave", "shell", "shine", "shirt",
    "shock", "short", "shrub", "silky", "since", "sixth", "skirt", "slate",
    "sleep", "slice", "slope", "smart", "smile", "snack", "snake", "sneak",
    "solid", "sound", "space", "spark", "spice", "spine", "sport", "spray",
    "squad", "staff", "stain", "stall", "stand", "stark", "state", "steam",
    "steep", "stick", "still", "stone", "stout", "strap", "strip", "stuck",
    "stump", "sugar", "sunny", "swamp", "swear", "sweep", "swift", "swirl",
    "sworn", "table", "teach", "theme", "thief", "thing", "third", "throw",
    "tidal", "tight", "timid", "title", "today", "tonic", "touch", "tower",
    "trace", "trade", "train", "tramp", "treat", "trial", "trick", "truly",
    "trust", "tulip", "tunic", "twice", "ultra", "uncle", "union", "untie",
    "upset", "usher", "vague", "value", "venue", "video", "vinyl", "viola",
    "visit", "vivid", "voice", "waltz", "watch", "waver", "weary", "wedge",
    "whale", "wheat", "where", "while", "whole", "width", "windy", "woman",
    "worry", "would", "woven", "wreck", "write", "yield", "youth", "zesty",
)

assert len(WORD_LIST) == len(set(WORD_LIST))
for _w in WORD_LIST:
    assert len(_w) == 5 and _w.isascii() and _w.isalpha() and _w.islower()
del _w


def answer_for(day: str, salt: str) -> str:
    """Deterministic daily answer word, the same for every player on one
    local calendar day: `sha256(f"{salt}|wordle|{day}")`, first 4 bytes as
    a big-endian int, mod len(WORD_LIST). Pure function -- no per-day state
    is ever stored to make this determinism happen; see BOOK's
    `puzzle_for` for the same shape."""
    digest = hashlib.sha256(f"{salt}|wordle|{day}".encode()).digest()
    index = int.from_bytes(digest[:4], "big") % len(WORD_LIST)
    return WORD_LIST[index]


def normalize_guess(text: str) -> str | None:
    """casefold + strip; the result must be exactly 5 ASCII letters, else
    None. No dictionary check -- any 5-letter alphabetic string is
    accepted, scored against the day's answer regardless of whether it is
    itself a real word."""
    candidate = text.strip().casefold()
    if len(candidate) == 5 and candidate.isascii() and candidate.isalpha():
        return candidate
    return None


def score(guess: str, answer: str) -> str:
    """Wordle-style feedback for a normalized `guess` against `answer`:
    🟩 right letter/right place, 🟨 right letter/wrong place, ⬜ absent.

    Two-pass so duplicate letters are handled correctly: greens are
    marked first (consuming one count of that letter from `answer`), then
    yellows are marked left-to-right limited by whatever count of that
    letter remains -- so a guess with more copies of a letter than the
    answer has never earns more colored squares than the answer actually
    contains.
    """
    remaining = Counter(answer)
    result = ["⬜"] * len(answer)

    for i, (g, a) in enumerate(zip(guess, answer, strict=True)):
        if g == a:
            result[i] = "🟩"
            remaining[g] -= 1

    for i, g in enumerate(guess):
        if result[i] == "🟩":
            continue
        if remaining.get(g, 0) > 0:
            result[i] = "🟨"
            remaining[g] -= 1

    return "".join(result)


def player_key(sender_node_id: str, day: str, salt: str) -> str:
    """Truncated sha256 of `(salt, sender_node_id, day)` -- never the raw
    node id itself, and never reversible to it in practice. Used as the
    primary key into `wordle_state` instead of `sender_node_id`."""
    digest = hashlib.sha256(f"{salt}|{sender_node_id}|{day}".encode()).hexdigest()
    return digest[:32]


def get_state(conn: sqlite3.Connection, key: str, day: str) -> sqlite3.Row | None:
    """This player's wordle_state row for `day`, or None if they have not
    guessed yet today."""
    return conn.execute(
        "SELECT player_key, day, guesses, solved, updated_at FROM wordle_state"
        " WHERE player_key = ? AND day = ?",
        (key, day),
    ).fetchone()


def record_give_up(conn: sqlite3.Connection, key: str, day: str, now: int) -> None:
    """Marks (key, day) done for the day via GIVEUP, without ever counting
    as a solve: upserts wordle_state to guesses = MAX_GUESSES, solved = 0;
    increments wordle_daily.players only on this player's first interaction
    of the day (mirrors record_guess's first-guess counting), and never
    touches solves/total_guesses_on_solve. Never stores the answer or a raw
    node id.
    """
    with conn:
        existing = conn.execute(
            "SELECT guesses FROM wordle_state WHERE player_key = ? AND day = ?",
            (key, day),
        ).fetchone()
        is_first_guess = existing is None

        conn.execute(
            "INSERT INTO wordle_state (player_key, day, guesses, solved, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(player_key, day) DO UPDATE SET"
            " guesses = excluded.guesses, solved = excluded.solved,"
            " updated_at = excluded.updated_at",
            (key, day, MAX_GUESSES, 0, now),
        )
        conn.execute(
            "INSERT INTO wordle_daily (day, players, solves, total_guesses_on_solve)"
            " VALUES (?, ?, 0, 0)"
            " ON CONFLICT(day) DO UPDATE SET"
            " players = players + excluded.players",
            (day, 1 if is_first_guess else 0),
        )


def record_guess(conn: sqlite3.Connection, key: str, day: str, solved: bool, now: int) -> None:
    """Atomically records one accepted guess for (key, day): upserts
    wordle_state (increments guesses, sets solved), and maintains the
    wordle_daily aggregate -- players is counted once, on a key's first
    guess of the day; solves and total_guesses_on_solve are counted only
    when this guess is the one that solves it. Never stores the guessed
    word or a raw node id.
    """
    with conn:
        existing = conn.execute(
            "SELECT guesses FROM wordle_state WHERE player_key = ? AND day = ?",
            (key, day),
        ).fetchone()
        is_first_guess = existing is None
        new_guesses = (existing["guesses"] if existing is not None else 0) + 1

        conn.execute(
            "INSERT INTO wordle_state (player_key, day, guesses, solved, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(player_key, day) DO UPDATE SET"
            " guesses = excluded.guesses, solved = excluded.solved,"
            " updated_at = excluded.updated_at",
            (key, day, new_guesses, int(solved), now),
        )
        conn.execute(
            "INSERT INTO wordle_daily (day, players, solves, total_guesses_on_solve)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(day) DO UPDATE SET"
            " players = players + excluded.players,"
            " solves = solves + excluded.solves,"
            " total_guesses_on_solve = total_guesses_on_solve + excluded.total_guesses_on_solve",
            (
                day,
                1 if is_first_guess else 0,
                1 if solved else 0,
                new_guesses if solved else 0,
            ),
        )


def prune_wordle_state(
    conn: sqlite3.Connection, now: int, keep_seconds: int = 48 * 3600, batch: int = _PRUNE_BATCH
) -> int:
    """Deletes wordle_state rows whose updated_at is older than
    keep_seconds, in bounded batches (never an unbounded DELETE) -- same
    style as retention.py's `_delete_in_batches`. Returns rows deleted.
    wordle_daily is never touched here: it holds no per-player state to
    expire."""
    cutoff = now - keep_seconds
    total = 0
    while True:
        cur = conn.execute(
            "DELETE FROM wordle_state WHERE (player_key, day) IN"
            " (SELECT player_key, day FROM wordle_state WHERE updated_at < ? LIMIT ?)",
            (cutoff, batch),
        )
        conn.commit()
        if cur.rowcount <= 0:
            return total
        total += cur.rowcount


def wordle_prompt_text(day: str) -> str:
    """Invitation to play today's puzzle, three lines, <= 180 UTF-8 bytes."""
    return f"\U0001f7e9 Wordle {day}\n5 letters · 6 tries\n→ reply WORDLE <guess>" + _TRAILING_EMOJI


def wordle_invalid_text() -> str:
    """Reply for a malformed guess argument, three lines, <= 180 UTF-8
    bytes."""
    return (
        "\U0001f7e9 Wordle\nGuess must be 5 letters a–z\n→ WORDLE <guess>" + _TRAILING_EMOJI
    )


def wordle_feedback_text(squares: str, guess: str, n: int, max_guesses: int) -> str:
    """Reply for an accepted, unsolved guess, four lines, <= 180 UTF-8
    bytes: header; the guessed letters space-separated (`C R A N E`); the
    feedback row with the same spacing, green squares rendered as the
    guessed letter so the two rows line up as a column-aligned table
    (`C 🟨 ⬜ ⬜ ⬜`); the footer."""
    letters = " ".join(guess.upper())
    row = " ".join(
        g.upper() if sq == "🟩" else sq for sq, g in zip(squares, guess, strict=True)
    )
    return (
        f"\U0001f7e9 Wordle · try {n}/{max_guesses}\n{letters}\n{row}"
        "\n→ WORDLE <guess>" + _TRAILING_EMOJI
    )


def wordle_solved_text(
    n: int, max_guesses: int, answer: str, *, solved_today: int, avg_guesses: float
) -> str:
    """Reply for the guess that solves the day's puzzle, three lines,
    <= 180 UTF-8 bytes. `solved_today`/`avg_guesses` come from `wordle_daily`
    (see `_daily_solve_stats`) -- an aggregate, never a node id or a
    per-player detail."""
    return (
        f"\U0001f389 Solved in {n}/{max_guesses} · {answer.upper()}\n"
        f"{solved_today} solved today · avg {avg_guesses:.1f}\n\U0001f331 new word tomorrow"
    )


def wordle_out_text(answer: str) -> str:
    """Reply once a player has used all MAX_GUESSES without solving, three
    lines, <= 180 UTF-8 bytes."""
    return f"❌ Out of guesses\nIt was {answer.upper()}\n\U0001f331 new word tomorrow"


def _daily_solve_stats(conn: sqlite3.Connection, day: str) -> tuple[int, float]:
    """(solves, average guesses-to-solve) for `day`'s wordle_daily row, or
    (0, 0.0) if nothing has solved yet today."""
    row = conn.execute(
        "SELECT solves, total_guesses_on_solve FROM wordle_daily WHERE day = ?", (day,)
    ).fetchone()
    if row is None or not row["solves"]:
        return (0, 0.0)
    return (row["solves"], row["total_guesses_on_solve"] / row["solves"])


@dataclass(frozen=True)
class WordleDaySummary:
    day: str
    players: int
    solves: int
    avg_guesses: float


def wordle_stats_summary(
    conn: sqlite3.Connection, days: int = 7, since_day: str | None = None
) -> list[WordleDaySummary]:
    """Newest-first `wordle_daily` rows, up to `days` of them, for the CLI.
    No node ids, no words -- only bounded daily aggregates. `since_day`, if
    given, additionally excludes rows before that UTC day, so a sparse solve
    history doesn't pull rows from further back than the intended window."""
    if since_day is not None:
        rows = conn.execute(
            "SELECT day, players, solves, total_guesses_on_solve FROM wordle_daily "
            "WHERE day >= ? ORDER BY day DESC LIMIT ?",
            (since_day, days),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT day, players, solves, total_guesses_on_solve FROM wordle_daily "
            "ORDER BY day DESC LIMIT ?",
            (days,),
        ).fetchall()
    return [
        WordleDaySummary(
            day=r["day"],
            players=r["players"],
            solves=r["solves"],
            avg_guesses=(r["total_guesses_on_solve"] / r["solves"]) if r["solves"] else 0.0,
        )
        for r in rows
    ]


def wordle_done_text(day: str) -> str:
    """Reply for any further interaction once a player is done for the
    day (solved, or out of guesses), three lines, <= 180 UTF-8 bytes."""
    return f"\U0001f7e9 Wordle {day}\nYou're done for today\n\U0001f331 new word tomorrow"


def wordle_clue_text(pattern: str, n: int, max_guesses: int) -> str:
    """Reply for `WORDLE CLUE`, three lines, <= 180 UTF-8 bytes. `pattern`
    is the five answer positions space-separated, the revealed one as its
    uppercase letter and the rest as `_` (see `_play_clue`) -- the same
    letter-for-green, space-separated layout as `wordle_feedback_text`'s
    rows."""
    return (
        f"\U0001f7e9 Wordle · clue used try {n}/{max_guesses}\n{pattern}"
        "\n→ WORDLE <guess>" + _TRAILING_EMOJI
    )


def _wordle_week_stats(conn: sqlite3.Connection, day: str) -> tuple[int, int, int]:
    """(players, solves, total_guesses_on_solve) summed over the rolling
    7-local-day window ending on `day` (today + 6 previous local days,
    inclusive). `day` strings are `YYYY-MM-DD`, so lexicographic
    comparison sorts the same as chronological order -- no need to
    enumerate the individual day strings."""
    start_day = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(players), 0), COALESCE(SUM(solves), 0),"
        " COALESCE(SUM(total_guesses_on_solve), 0)"
        " FROM wordle_daily WHERE day >= ? AND day <= ?",
        (start_day, day),
    ).fetchone()
    return (row[0], row[1], row[2])


def wordle_score_text(conn: sqlite3.Connection, day: str) -> str:
    """`WORDLE SCORE`/`SCORES` reply, four lines, <= 180 UTF-8 bytes:
    mesh-wide aggregates only -- never node ids or per-player data.
    Today's line reads `wordle_daily`'s row for `day` directly ("nobody
    yet" if no one has played today); the 7-day line sums the rolling
    window ending on `day` via `_wordle_week_stats`. avg is omitted
    whenever its denominator (solves) is 0. Pure read -- never touches
    player state and never counts as a guess."""
    today_row = conn.execute(
        "SELECT players, solves, total_guesses_on_solve FROM wordle_daily WHERE day = ?",
        (day,),
    ).fetchone()

    if today_row is None:
        today_line = "Today: nobody yet"
    else:
        today_avg = (
            f" · avg {today_row['total_guesses_on_solve'] / today_row['solves']:.1f}"
            if today_row["solves"]
            else ""
        )
        today_line = (
            f"Today: {today_row['solves']} solved of {today_row['players']} players{today_avg}"
        )

    week_players, week_solves, week_guesses = _wordle_week_stats(conn, day)
    week_avg = f" · avg {week_guesses / week_solves:.1f}" if week_solves else ""
    week_line = f"7 days: {week_solves} solved of {week_players}{week_avg}"

    return f"\U0001f7e9 Wordle scores\n{today_line}\n{week_line}\n\U0001f331 WORDLE to play"


def wordle_gave_up_text(answer: str) -> str:
    """Reply for `WORDLE GIVEUP`/`GIVE UP`/`I GIVE UP`, three lines,
    <= 180 UTF-8 bytes. Giving up never counts as a solve -- see
    `record_give_up`."""
    return (
        f"🏳️ Gave up · it was {answer.upper()}\n"
        "CLUE costs a try, GIVEUP ends the day\n\U0001f331 new word tomorrow"
    )


def _play_clue(conn: sqlite3.Connection, key: str, day: str, salt: str, now: int) -> str:
    """`WORDLE CLUE`: reveals the answer letter at the 0-based position
    equal to the player's guesses used so far today (capped at index 4),
    and costs one guess exactly like a wrong guess. Already-done players
    get the done reply and nothing is recorded. If this clue consumes the
    player's last try, the whole word is revealed via `wordle_out_text`
    instead of a partial pattern."""
    state = get_state(conn, key, day)
    if state is not None and (state["solved"] or state["guesses"] >= MAX_GUESSES):
        return wordle_done_text(day)

    answer = answer_for(day, salt)
    used = state["guesses"] if state is not None else 0
    pos = min(used, 4)
    pattern = " ".join(answer[i].upper() if i == pos else "_" for i in range(5))
    record_guess(conn, key, day, False, now)

    n = used + 1
    if n >= MAX_GUESSES:
        return wordle_out_text(answer)
    return wordle_clue_text(pattern, n, MAX_GUESSES)


def _play_give_up(conn: sqlite3.Connection, key: str, day: str, salt: str, now: int) -> str:
    """`WORDLE GIVEUP`/`GIVE UP`/`I GIVE UP`: reveals the answer and marks
    the player done for today. Already-done players get the done reply
    and nothing is recorded; giving up never counts as a solve."""
    state = get_state(conn, key, day)
    if state is not None and (state["solved"] or state["guesses"] >= MAX_GUESSES):
        return wordle_done_text(day)

    answer = answer_for(day, salt)
    record_give_up(conn, key, day, now)
    return wordle_gave_up_text(answer)


def play(
    conn: sqlite3.Connection,
    *,
    sender_node_id: str,
    text_argument: str,
    day: str,
    salt: str,
    now: int,
) -> str:
    """Pure driver for the whole WORDLE flow, so wiring it into main.py is
    a one-line call: empty argument -> prompt; `CLUE` -> a revealed letter
    that costs a guess; `GIVEUP`/`GIVE UP`/`I GIVE UP` -> reveal the answer
    and end the day; `SCORE`/`SCORES` -> mesh-wide today/7-day aggregates
    (touches no player state, never counts as a guess); invalid guess -> the
    5 letters a-z hint; already done
    for today (solved, or MAX_GUESSES used) -> the done reply; otherwise
    the guess is scored against today's answer, recorded, and the
    feedback/solved/out reply is returned. Every string this can return is
    <= 180 UTF-8 bytes.
    """
    key = player_key(sender_node_id, day, salt)
    stripped = text_argument.strip()

    if not stripped:
        return wordle_prompt_text(day)

    keyword = " ".join(stripped.upper().split())
    if keyword == "CLUE":
        return _play_clue(conn, key, day, salt, now)
    if keyword in ("GIVEUP", "GIVE UP", "I GIVE UP"):
        return _play_give_up(conn, key, day, salt, now)
    if keyword in ("SCORE", "SCORES"):
        return wordle_score_text(conn, day)

    normalized = normalize_guess(stripped)
    if normalized is None:
        return wordle_invalid_text()

    state = get_state(conn, key, day)
    if state is not None and (state["solved"] or state["guesses"] >= MAX_GUESSES):
        return wordle_done_text(day)

    answer = answer_for(day, salt)
    squares = score(normalized, answer)
    solved = normalized == answer
    n = (state["guesses"] if state is not None else 0) + 1
    record_guess(conn, key, day, solved, now)

    if solved:
        solved_today, avg_guesses = _daily_solve_stats(conn, day)
        return wordle_solved_text(
            n, MAX_GUESSES, answer, solved_today=solved_today, avg_guesses=avg_guesses
        )
    if n >= MAX_GUESSES:
        return wordle_out_text(answer)
    return wordle_feedback_text(squares, normalized, n, MAX_GUESSES)
