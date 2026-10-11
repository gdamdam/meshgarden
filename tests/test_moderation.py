import pytest

from meshgarden.moderation import (
    BANNED_WORD_REASON,
    find_banned_term,
    load_banned_terms,
    normalize_for_match,
)


def write(tmp_path, text):
    p = tmp_path / "banned.txt"
    p.write_text(text)
    return str(p)


# --- normalize_for_match ---------------------------------------------------------


def test_normalize_nfc_composes():
    decomposed = "e" + "́"  # "e" + COMBINING ACUTE ACCENT
    assert normalize_for_match(decomposed) == "é"


def test_normalize_casefolds():
    assert normalize_for_match("BadWord") == "badword"


def test_normalize_collapses_whitespace_and_strips_ends():
    assert normalize_for_match("  bad   word  ") == "bad word"


# --- load_banned_terms -----------------------------------------------------------


def test_load_banned_terms_reads_one_term_per_line(tmp_path):
    path = write(tmp_path, "badword\nother\n")
    terms = load_banned_terms(path)
    assert terms == frozenset({"badword", "other"})


def test_load_banned_terms_strips_comments(tmp_path):
    path = write(tmp_path, "badword # this is bad\n# a full comment line\nother\n")
    terms = load_banned_terms(path)
    assert terms == frozenset({"badword", "other"})


def test_load_banned_terms_skips_blank_lines(tmp_path):
    path = write(tmp_path, "badword\n\n   \nother\n")
    terms = load_banned_terms(path)
    assert terms == frozenset({"badword", "other"})


def test_load_banned_terms_normalizes_case_and_whitespace(tmp_path):
    path = write(tmp_path, "  BadWord  \nMulti   Word  Term\n")
    terms = load_banned_terms(path)
    assert terms == frozenset({"badword", "multi word term"})


def test_load_banned_terms_empty_file_yields_empty_set(tmp_path):
    path = write(tmp_path, "")
    assert load_banned_terms(path) == frozenset()


# --- find_banned_term -------------------------------------------------------------


def test_find_banned_term_matches_whole_word():
    terms = frozenset({"badword"})
    assert find_banned_term("this is a badword here", terms) == "badword"


def test_find_banned_term_returns_none_when_no_match():
    terms = frozenset({"badword"})
    assert find_banned_term("this is fine", terms) is None


def test_find_banned_term_does_not_match_partial_word():
    # "ass" must not match inside "class" -- whole-word only.
    terms = frozenset({"ass"})
    assert find_banned_term("i took a class today", terms) is None


def test_find_banned_term_matches_multi_word_terms():
    terms = frozenset({"multi word term"})
    assert find_banned_term("this has a multi word term inside", terms) == "multi word term"


def test_find_banned_term_does_not_match_multi_word_term_out_of_order():
    terms = frozenset({"multi word term"})
    assert find_banned_term("word multi term", terms) is None


def test_find_banned_term_respects_punctuation_boundaries():
    terms = frozenset({"badword"})
    assert find_banned_term("wow, badword! really", terms) == "badword"


def test_find_banned_term_case_matches_when_text_already_normalized():
    # Caller is expected to normalize text the same way (NFC, casefold,
    # collapse whitespace) before calling; a mismatched case in the input
    # (as if the caller forgot to casefold) legitimately fails to match.
    terms = frozenset({"badword"})
    assert find_banned_term(normalize_for_match("BADWORD"), terms) == "badword"


def test_find_banned_term_empty_terms_never_matches():
    assert find_banned_term("anything at all", frozenset()) is None


def test_find_banned_term_is_first_match_among_multiple_terms():
    terms = frozenset({"foo", "bar"})
    result = find_banned_term("this has foo and bar in it", terms)
    assert result in {"foo", "bar"}


# --- moderation reason constant ---------------------------------------------------


def test_banned_word_reason_constant():
    assert BANNED_WORD_REASON == "banned_word"


def test_load_banned_terms_raises_for_missing_file(tmp_path):
    missing = str(tmp_path / "nope.txt")
    with pytest.raises(OSError):
        load_banned_terms(missing)


# --- packaged default list ---------------------------------------------------------


def test_default_loads_the_packaged_list():
    terms = load_banned_terms("default")
    assert isinstance(terms, frozenset)
    assert len(terms) >= 40  # operator-curated list; keep a sane floor only


def test_default_terms_are_unique_lowercase_and_at_least_four_chars():
    terms = load_banned_terms("default")
    assert all(t == t.lower() for t in terms)
    assert all(len(t) >= 4 for t in terms)
    # frozenset already guarantees uniqueness; assert non-trivially large too.
    assert len(terms) == len(set(terms))


@pytest.mark.parametrize(
    "text",
    [
        "class",
        "assistant",
        "scunthorpe",
        "therapist",
        "shitake mushrooms are great",
    ],
)
def test_default_list_does_not_false_positive_on_ordinary_words(text):
    terms = load_banned_terms("default")
    assert find_banned_term(normalize_for_match(text), terms) is None
