"""Exercise every static entry against the real radio byte budgets."""

import pytest

from meshgarden.fortune import HAIKUS, haiku_text
from meshgarden.moon import _NEW_MOON, _PHASES, _SYNODIC_DAYS, moon_text


@pytest.mark.parametrize('budget', [120, 180])
@pytest.mark.parametrize('template', HAIKUS)
def test_haiku_preserves_three_lines_with_long_unicode_place(template, budget):
    class Fixed:
        def choice(self, choices):
            return template
    body = haiku_text(senders_24h=2**63-1, guestbook_visible=2**63-1,
                      place='🌁 Potrero Hill ' * 20, rng=Fixed(), max_bytes=budget)
    assert len(body.encode()) <= budget
    assert len(body.splitlines()) == 3
    for line, pattern in zip(body.splitlines(), template.lines, strict=True):
        if '{place}' not in pattern:
            expected = pattern.format(senders=2**63-1, guestbook=2**63-1)
            assert line.removesuffix(' 🌱') == expected


@pytest.mark.parametrize('index,emoji', list(enumerate('🌑🌒🌓🌔🌕🌖🌗🌘')))
def test_moon_emoji_matches_phase(index, emoji):
    body = moon_text(int(_NEW_MOON + _SYNODIC_DAYS * 86400 * index / 8))
    assert body.startswith(f'{emoji} MOON · {_PHASES[index]}\n')
    assert len(body.encode()) <= 120


def test_dispatch_uses_transport_budget(daemon, monkeypatch):
    from types import SimpleNamespace

    from meshgarden.commands import Command
    from meshgarden.fortune import iching_text, local_day

    monkeypatch.setattr(daemon, '_transport', SimpleNamespace(max_text_bytes=120))
    now = 1_800_000_000
    day = local_day(now, daemon.cfg.service.timezone)
    assert daemon._build_response_body(Command.ICHING, '!aabbccdd', now) == iching_text(
        '!aabbccdd', day, daemon.cfg.service.fortune_salt, max_bytes=120
    )
    monkeypatch.setattr(daemon._collector, 'distinct_senders_since', lambda since: 42)
    monkeypatch.setattr(daemon._guestbook, 'visible_count', lambda: 2)
    body = daemon._build_response_body(Command.HAIKU, '!aabbccdd', now)
    assert len(body.encode()) <= 120
    assert len(body.splitlines()) == 3
