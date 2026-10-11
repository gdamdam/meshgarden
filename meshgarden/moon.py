"""Approximate moon phase from a mean synodic month; entirely offline."""

import math
from datetime import UTC, datetime

_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=UTC).timestamp()
_SYNODIC_DAYS = 29.53058867
_PHASES = (
    'New moon', 'Waxing crescent', 'First quarter', 'Waxing gibbous',
    'Full moon', 'Waning gibbous', 'Last quarter', 'Waning crescent',
)

_PHASE_EMOJI = ("🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘")

def moon_text(now: int) -> str:
    phase = ((now - _NEW_MOON) / 86400 / _SYNODIC_DAYS) % 1
    illuminated = round(50 * (1 - math.cos(2 * math.pi * phase)))
    index = int(phase * 8 + 0.5) % 8
    name = _PHASES[index]
    return f"{_PHASE_EMOJI[index]} MOON · {name}\n💡 ~{illuminated}% illuminated · approx"
