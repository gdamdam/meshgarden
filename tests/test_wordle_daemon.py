"""Daemon-level WORDLE wiring: dispatch, per-player state persistence, the
6-guess cap, and the "queue-refused play must not consume a guess" ordering.

`wordle.py`'s own module tests (tests/test_wordle.py) already cover
`play()`'s pure logic exhaustively; these tests only prove the wiring: a
`WORDLE` DM reaches `wordle.play` via the daemon's normal dispatch/reserve/
send path, with the same guess-text-never-stored/logged guarantee as
SIGN/BOOK.
"""

import time

from meshgarden import fortune, wordle
from meshgarden.commands import Reject
from tests.fakes import make_packet


def b(s: str) -> int:
    return len(s.encode("utf-8"))


def _day_and_answer(daemon):
    day = fortune.local_day(int(time.time()), daemon.cfg.service.timezone)
    answer = wordle.answer_for(day, daemon.cfg.service.fortune_salt)
    return day, answer


def test_wordle_bare_dm_produces_the_prompt(daemon):
    day, _ = _day_and_answer(daemon)
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="WORDLE", packet_id=9401))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]
    assert reply == wordle.wordle_prompt_text(day)
    assert b(reply) <= 180


def test_lowercase_wordle_works(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="wordle", packet_id=9402))
    daemon.drain()
    assert len(daemon.iface.sent) == 1


def test_wordle_invalid_guess_returns_the_invalid_reply(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="WORDLE hi", packet_id=9403)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"] == wordle.wordle_invalid_text()


def test_wordle_wrong_guess_returns_feedback(daemon):
    _, answer = _day_and_answer(daemon)
    guess = next(w for w in wordle.WORD_LIST if w != answer)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=f"WORDLE {guess}", packet_id=9404)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]
    squares = wordle.score(guess, answer)
    assert reply == wordle.wordle_feedback_text(squares, guess, 1, wordle.MAX_GUESSES)
    assert b(reply) <= 180


def test_wordle_correct_guess_solves(daemon):
    _, answer = _day_and_answer(daemon)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=f"WORDLE {answer}", packet_id=9405)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]
    assert reply == wordle.wordle_solved_text(
        1, wordle.MAX_GUESSES, answer, solved_today=1, avg_guesses=1.0
    )


def test_wordle_guess_text_is_never_written_to_the_database_or_logged(daemon, caplog):
    caplog.set_level("DEBUG")
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="WORDLE zesty", packet_id=9406)
    )
    daemon.drain()
    dump = "\n".join(daemon.conn.iterdump())
    assert "zesty" not in dump
    assert "zesty" not in caplog.text
    row = daemon.conn.execute(
        "SELECT command FROM processed_requests WHERE packet_id=9406"
    ).fetchone()
    assert row["command"] == "WORDLE"


def test_wordle_six_guess_limit_then_done_state(daemon):
    day, answer = _day_and_answer(daemon)
    wrong_words = [w for w in wordle.WORD_LIST if w != answer][:6]
    for i, w in enumerate(wrong_words):
        daemon.iface.emit_packet(
            make_packet(to_num=daemon.my_node_num, text=f"WORDLE {w}", packet_id=9500 + i)
        )
        daemon.drain()
        daemon.advance_seconds(31)  # clear the per-node command cooldown between guesses

    last_reply = daemon.iface.sent[-1]["text"]
    assert last_reply == wordle.wordle_out_text(answer)

    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=f"WORDLE {wrong_words[0]}", packet_id=9510)
    )
    daemon.drain()
    assert daemon.iface.sent[-1]["text"] == wordle.wordle_done_text(day)


def test_wordle_play_refused_by_queue_does_not_consume_a_guess(daemon, monkeypatch):
    """A play whose reply cannot be queued must not consume a guess: queue
    availability is checked BEFORE wordle.play() runs, exactly like SIGN
    checks queue.reserve() before guestbook.insert(). Forcing reserve() to
    always refuse (rather than racing the real queue/TX worker) makes this
    deterministic."""
    monkeypatch.setattr(daemon._queue, "reserve", lambda sender: Reject.QUEUE_FULL)
    day, answer = _day_and_answer(daemon)

    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=f"WORDLE {answer}", packet_id=9600)
    )
    daemon.drain()

    assert daemon.iface.sent == []
    key = wordle.player_key("!aabbccdd", day, daemon.cfg.service.fortune_salt)
    assert wordle.get_state(daemon.conn, key, day) is None

    row = daemon.conn.execute(
        "SELECT state, reason FROM processed_requests WHERE packet_id=9600"
    ).fetchone()
    assert row["state"] == "dropped"
    assert row["reason"] == Reject.QUEUE_FULL.value


def test_wordle_clue_dm_reveals_one_letter_of_the_pattern(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="WORDLE CLUE", packet_id=9800)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]
    pattern_line = reply.splitlines()[1]
    positions = pattern_line.split(" ")
    assert len(positions) == 5
    assert sum(1 for p in positions if p != "_") == 1
    assert sum(1 for p in positions if p == "_") == 4
    assert b(reply) <= 180


def test_wordle_i_give_up_dm_reveals_the_answer(daemon):
    _, answer = _day_and_answer(daemon)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="WORDLE I GIVE UP", packet_id=9801)
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]
    assert answer.upper() in reply
    assert reply == wordle.wordle_gave_up_text(answer)
    assert b(reply) <= 180


def test_wordle_score_dm_reports_mesh_wide_aggregates_and_writes_no_state(daemon):
    day, answer = _day_and_answer(daemon)
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text=f"WORDLE {answer}", packet_id=9900)
    )
    daemon.drain()

    daemon.iface.emit_packet(
        make_packet(
            from_num=0x22222222, to_num=daemon.my_node_num, text="WORDLE SCORE", packet_id=9901
        )
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 2
    reply = daemon.iface.sent[1]["text"]
    assert reply == wordle.wordle_score_text(daemon.conn, day)
    assert b(reply) <= 180

    key = wordle.player_key("!22222222", day, daemon.cfg.service.fortune_salt)
    assert wordle.get_state(daemon.conn, key, day) is None

    row_before = daemon.conn.execute(
        "SELECT players, solves, total_guesses_on_solve FROM wordle_daily WHERE day=?", (day,)
    ).fetchone()

    daemon.iface.emit_packet(
        make_packet(
            from_num=0x33333333, to_num=daemon.my_node_num, text="wordle scores", packet_id=9902
        )
    )
    daemon.drain()
    assert daemon.iface.sent[2]["text"] == reply

    row_after = daemon.conn.execute(
        "SELECT players, solves, total_guesses_on_solve FROM wordle_daily WHERE day=?", (day,)
    ).fetchone()
    assert tuple(row_after) == tuple(row_before)


def test_wordle_reply_never_exceeds_the_byte_budget_via_reserve_and_send(daemon):
    """Belt-and-braces: the daemon-level path truncates/sends through the
    same `_reserve_and_send` every other command uses."""
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="WORDLE", packet_id=9700))
    daemon.drain()
    assert b(daemon.iface.sent[0]["text"]) <= daemon.cfg.service.max_response_bytes
