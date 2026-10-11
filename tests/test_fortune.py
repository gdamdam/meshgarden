import random
from collections import Counter

import pytest

from meshgarden import fortune


def b(s: str) -> int:
    return len(s.encode("utf-8"))


# --- ICHING: hexagram table -------------------------------------------------


def test_exactly_64_hexagrams_numbered_1_to_64():
    assert len(fortune.HEXAGRAMS) == 64
    assert [h.number for h in fortune.HEXAGRAMS] == list(range(1, 65))


def test_hexagram_symbol_maps_number_to_the_right_yijing_code_point():
    assert fortune.hexagram_symbol(1) == "䷀"
    assert fortune.hexagram_symbol(64) == "䷿"
    for h in fortune.HEXAGRAMS:
        assert fortune.hexagram_symbol(h.number) == chr(0x4DC0 + h.number - 1)


@pytest.mark.parametrize("h", fortune.HEXAGRAMS, ids=lambda h: f"{h.number}-{h.pinyin}")
def test_every_hexagram_line_fits_the_byte_budget(h):
    line = fortune.format_hexagram(h)
    assert b(line) <= 180
    assert line.endswith("\U0001f331 same for you all day")
    assert str(h.number) in line


def test_format_hexagram_is_four_lines_number_title_judgment_then_footer():
    """Line 1: "☯ Hexagram N". Line 2: "symbol pinyin · English title".
    Line 3: the bare judgment. Line 4: the fixed "same for you all day"
    footer."""
    h = fortune.HEXAGRAMS[0]
    line = fortune.format_hexagram(h)
    assert line == (
        "☯ Hexagram 1\n"
        "䷀ Qián · The Creative\n"
        "great strength rising; act with clear purpose\n"
        "\U0001f331 same for you all day"
    )
    line1, line2, line3, line4 = line.split("\n")
    assert line1 == "☯ Hexagram 1"
    assert line2 == "䷀ Qián · The Creative"
    assert line3 == "great strength rising; act with clear purpose"
    assert line4 == "\U0001f331 same for you all day"


@pytest.mark.parametrize("h", fortune.HEXAGRAMS, ids=lambda h: f"{h.number}-{h.pinyin}")
def test_every_hexagram_is_exactly_four_lines(h):
    line = fortune.format_hexagram(h)
    assert line.count("\n") == 3
    line1, line2, line3, line4 = line.split("\n")
    assert line1 == f"☯ Hexagram {h.number}"
    assert line2 == f"{fortune.hexagram_symbol(h.number)} {h.pinyin} · {h.english}"
    assert line3 == h.judgment
    assert line4 == "\U0001f331 same for you all day"


def test_no_duplicate_pinyin_and_number_pairs():
    pairs = [(h.number, h.pinyin) for h in fortune.HEXAGRAMS]
    assert len(pairs) == len(set(pairs))


# --- ICHING: deterministic per (sender, day, salt) --------------------------


def test_hexagram_for_is_deterministic_for_the_same_inputs():
    a = fortune.hexagram_for("!aabbccdd", "2026-09-13", "")
    b_ = fortune.hexagram_for("!aabbccdd", "2026-09-13", "")
    assert a == b_
    assert 1 <= a <= 64


def test_hexagram_for_changes_across_days():
    day1 = fortune.hexagram_for("!aabbccdd", "2026-09-13", "")
    day2 = fortune.hexagram_for("!aabbccdd", "2026-09-14", "")
    assert day1 != day2  # true for this fixed pair; see the distribution test for the general case
    # Deterministic per its own day:
    assert fortune.hexagram_for("!aabbccdd", "2026-09-13", "") == day1
    assert fortune.hexagram_for("!aabbccdd", "2026-09-14", "") == day2


def test_hexagram_for_changes_across_senders():
    same_day = "2026-09-13"
    a = fortune.hexagram_for("!aaaaaaaa", same_day, "")
    b_ = fortune.hexagram_for("!bbbbbbbb", same_day, "")
    # Not mathematically guaranteed distinct, but deterministic and, per the
    # distribution test below, collisions between two fixed ids are rare.
    assert (a, b_) == (
        fortune.hexagram_for("!aaaaaaaa", same_day, ""),
        fortune.hexagram_for("!bbbbbbbb", same_day, ""),
    )
    assert a != b_


def test_hexagram_for_salt_changes_the_result():
    plain = fortune.hexagram_for("!aabbccdd", "2026-09-13", "")
    salted = fortune.hexagram_for("!aabbccdd", "2026-09-13", "some-secret-salt")
    assert plain != salted


def test_hexagram_for_is_roughly_uniform_over_64_for_many_synthetic_senders():
    """Loose distribution bound: a few thousand distinct synthetic sender
    ids on one day should spread across all 64 buckets, none wildly
    over/under-represented."""
    n = 6400
    counts = Counter(
        fortune.hexagram_for(f"!{i:08x}", "2026-09-13", "") for i in range(n)
    )
    assert len(counts) == 64  # every hexagram reachable
    expected = n / 64
    for count in counts.values():
        assert 0.5 * expected <= count <= 1.5 * expected


def test_hexagram_for_covers_the_full_1_64_range_eventually():
    seen = {fortune.hexagram_for(f"!{i:08x}", "2026-09-13", "") for i in range(2000)}
    assert seen == set(range(1, 65))


# --- ICHING: local_day -------------------------------------------------------


def test_local_day_utc():
    # 2026-09-13T12:00:00Z
    ts = 1789300800  # arbitrary sanity anchor recomputed below
    import datetime as dt

    ts = int(dt.datetime(2026, 9, 13, 12, 0, 0, tzinfo=dt.UTC).timestamp())
    assert fortune.local_day(ts, "UTC") == "2026-09-13"


def test_local_day_zone_boundary_crosses_to_previous_day():
    """06:30 UTC on 2026-09-13 is still 2026-09-12 in America/Los_Angeles
    (UTC-7 during PDT, so local midnight hasn't arrived yet)."""
    import datetime as dt

    ts = int(dt.datetime(2026, 9, 13, 6, 30, 0, tzinfo=dt.UTC).timestamp())
    assert fortune.local_day(ts, "UTC") == "2026-09-13"
    assert fortune.local_day(ts, "America/Los_Angeles") == "2026-09-12"


def test_iching_text_uses_local_day_and_sender_deterministically():
    t1 = fortune.iching_text("!aabbccdd", "2026-09-13", "")
    t2 = fortune.iching_text("!aabbccdd", "2026-09-13", "")
    assert t1 == t2
    assert b(t1) <= 180


# --- HAIKU -------------------------------------------------------------------


def test_between_12_and_16_haiku_templates():
    assert 12 <= len(fortune.HAIKUS) <= 16
    for t in fortune.HAIKUS:
        assert len(t.lines) == 3


@pytest.mark.parametrize("t", fortune.HAIKUS, ids=lambda t: t.lines[0])
def test_every_haiku_fits_the_byte_budget_at_worst_case_numbers_and_place(t):
    place32 = "P" * 32
    rendered = "\n".join(
        line.format(senders=9999, guestbook=9999, place=place32) for line in t.lines
    ) + " \U0001f331"
    assert b(rendered) <= 180


def test_haiku_text_ends_with_the_seedling_emoji_and_joins_three_lines():
    out = fortune.haiku_text(
        senders_24h=3, guestbook_visible=5, place="", rng=random.Random(1)
    )
    assert out.endswith(" \U0001f331")
    assert out.count("\n") == 2
    assert b(out) <= 180


def test_haiku_text_is_deterministic_with_an_injected_rng():
    a = fortune.haiku_text(senders_24h=1, guestbook_visible=1, place="", rng=random.Random(42))
    b_ = fortune.haiku_text(senders_24h=1, guestbook_visible=1, place="", rng=random.Random(42))
    assert a == b_


def test_haiku_text_uses_configured_place_when_given():
    template = next(t for t in fortune.HAIKUS if "{place}" in " ".join(t.lines))
    idx = fortune.HAIKUS.index(template)

    class _FixedChoice:
        def choice(self, seq):
            return seq[idx]

    out = fortune.haiku_text(
        senders_24h=0, guestbook_visible=0, place="Potrero", rng=_FixedChoice()
    )
    assert "Potrero" in out


def test_haiku_text_falls_back_to_the_templates_own_default_place_when_empty():
    template = next(t for t in fortune.HAIKUS if "{place}" in " ".join(t.lines))
    idx = fortune.HAIKUS.index(template)

    class _FixedChoice:
        def choice(self, seq):
            return seq[idx]

    out = fortune.haiku_text(
        senders_24h=0, guestbook_visible=0, place="", rng=_FixedChoice()
    )
    assert template.default_place in out


def test_haiku_default_places_are_never_a_specific_real_location():
    """Sanity guard: every template's fallback word is a generic noun
    phrase, not something that reads as a specific, real place name."""
    generic_words = {
        "the mesh",
        "this hill",
        "this ridge",
        "the garden",
        "this garden",
    }
    for t in fortune.HAIKUS:
        assert t.default_place in generic_words
