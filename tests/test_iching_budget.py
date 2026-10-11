"""Every deterministic I Ching result fits both supported radio budgets."""

import pytest

from meshgarden.fortune import HEXAGRAMS, format_hexagram, hexagram_symbol


@pytest.mark.parametrize("max_bytes", [120, 180])
@pytest.mark.parametrize("hexagram", HEXAGRAMS, ids=lambda h: str(h.number))
def test_all_hexagrams_fit_without_losing_meaning(hexagram, max_bytes):
    text = format_hexagram(hexagram, max_bytes=max_bytes)
    assert len(text.encode("utf-8")) <= max_bytes
    assert f"Hexagram {hexagram.number}" in text
    assert hexagram.english in text
    assert hexagram.judgment in text
    assert "same" in text and "day" in text
    text.encode("utf-8").decode("utf-8")


def test_meshcore_layout_removes_decoration_before_content():
    text = format_hexagram(HEXAGRAMS[-1], max_bytes=120)
    assert "🌱" not in text
    assert "☯" not in text
    assert hexagram_symbol(64) in text
    assert "Wèijì · Before Completion" in text
    assert HEXAGRAMS[-1].judgment in text


def test_unusually_tiny_budget_marks_shortened_judgment_explicitly():
    text = format_hexagram(HEXAGRAMS[-1], max_bytes=80)
    assert len(text.encode("utf-8")) <= 80
    assert "…" in text
