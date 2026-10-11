"""SIGN banned-word guard.

Loads an operator-provided list of banned terms and matches normalized SIGN
text against it, whole word only (multi-word terms match as an exact
contiguous phrase). Never logs the matched text or the input text -- only
the bounded `BANNED_WORD_REASON` code is ever surfaced to a caller.

Normalization here (`normalize_for_match`) is deliberately its own function,
distinct from guestbook.normalize_sign_text: the guestbook's stored
normalization preserves case (so a signed entry displays as typed), while
matching against the banned-word list needs case folded away too. Both
apply NFC and collapse whitespace the same way.
"""

from __future__ import annotations

import re
import unicodedata
from importlib import resources

BANNED_WORD_REASON = "banned_word"

# Special `banned_words_file` value (config.py accepts it too) meaning "use
# the list packaged with Meshgarden" instead of an operator-provided path.
DEFAULT_BANNED_WORDS_VALUE = "default"

_DEFAULT_BANNED_WORDS_RELATIVE_PATH = ("data", "banned-words.default.txt")


def normalize_for_match(text: str) -> str:
    """NFC normalize, casefold, collapse whitespace runs to a single
    U+0020, strip ends. Used both to normalize loaded banned terms and
    (by the caller) the SIGN text being checked, so both sides of the
    comparison are shaped identically."""
    s = unicodedata.normalize("NFC", text).casefold()
    return " ".join(s.split())


def _parse_terms(lines) -> frozenset[str]:
    """Shared parser for both the operator-provided file and the packaged
    default list: one term per line, "#" starts a trailing or full-line
    comment, blank lines skipped, each term normalized the same way as the
    text it will be matched against."""
    terms: set[str] = set()
    for line in lines:
        term = line.split("#", 1)[0]
        term = normalize_for_match(term)
        if term:
            terms.add(term)
    return frozenset(terms)


def load_default_banned_terms() -> frozenset[str]:
    """Loads the banned-word list packaged with Meshgarden
    (`meshgarden/data/banned-words.default.txt`), via
    `importlib.resources` so it works the same whether installed from a
    wheel or run from a checkout."""
    subdir, filename = _DEFAULT_BANNED_WORDS_RELATIVE_PATH
    text = (
        resources.files("meshgarden")
        .joinpath(subdir, filename)
        .read_text(encoding="utf-8")
    )
    return _parse_terms(text.splitlines())


def load_banned_terms(path: str) -> frozenset[str]:
    """Reads `path`: one term per line, "#" starts a trailing or full-line
    comment, blank lines skipped. Each term is normalized the same way as
    the text it will be matched against. Raises OSError if the file cannot
    be read (config.py already validates readability at load time; this
    is a second read at the point of use).

    The special value `"default"` (`DEFAULT_BANNED_WORDS_VALUE`) loads the
    list packaged with Meshgarden instead of reading a filesystem path --
    see `load_default_banned_terms`."""
    if path == DEFAULT_BANNED_WORDS_VALUE:
        return load_default_banned_terms()
    with open(path, encoding="utf-8") as f:
        return _parse_terms(f)


def find_banned_term(normalized_text: str, terms: frozenset[str]) -> str | None:
    """Returns the first banned term found as a whole word (or, for a
    multi-word term, an exact contiguous phrase) inside `normalized_text`,
    or None. `normalized_text` is assumed already normalized the same way
    as `terms` (see `normalize_for_match`) -- this function does not
    re-normalize its input.

    Whole-word matching uses regex \\b boundaries, so "ass" never matches
    inside "class": there is no word-character transition between "c" and
    "a" for \\b to anchor on.
    """
    for term in terms:
        pattern = r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, normalized_text):
            return term
    return None
