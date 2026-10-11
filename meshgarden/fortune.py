"""ICHING and HAIKU: two small reply generators.

Neither decides eligibility, rate limits, or storage; that stays in
commands.py/main.py exactly as for every other command.

ICHING is deterministic, not random: `hexagram_for` derives a hexagram
number (1..64) purely from `(sender_node_id, local calendar day, salt)` via
a sha256 digest -- the same visitor gets the same hexagram all day and a
different one the next day, with no per-user state ever stored. `local_day`
computes that calendar day from a unix timestamp and an IANA zone name
(config.py validates the zone at load time via `zoneinfo.ZoneInfo`).

HAIKU stays randomly selected per call, via an injectable RNG (default
`random.SystemRandom()`) so tests can pass a seeded `random.Random` (or any
object with a `.choice` method) for determinism.

The 64 I Ching judgments below are ORIGINAL one-line paraphrases written for
this project, in plain contemporary English -- not quotations or close
paraphrases of the Wilhelm/Baynes translation or any other copyrighted
edition. The hexagram numbers, pinyin names, and English titles are
traditional/public-domain reference facts (the King Wen sequence), not
copyrighted text.

Nothing here ever hardcodes a real place name: `haiku_place` is entirely
operator-configured (see config.py), and every template's own fallback
(`default_place`) is a generic, non-specific word.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

_TRAILING_EMOJI = " \U0001f331"  # " 🌱"


@dataclass(frozen=True)
class Hexagram:
    number: int  # 1..64, King Wen sequence
    pinyin: str
    english: str
    judgment: str  # original one-line paraphrase; no trailing punctuation/emoji


def hexagram_symbol(number: int) -> str:
    """U+4DC0 + (number - 1); the Yijing Hexagram Symbols block is exactly
    64 code points (U+4DC0..U+4DFF), one per King Wen sequence number."""
    return chr(0x4DC0 + number - 1)


_ICHING_FOOTER = "🌱 same for you all day"
_ICHING_COMPACT_FOOTER = "same all day"


def _truncate_with_ellipsis(text: str, max_bytes: int) -> str:
    """UTF-8-safe explicit shortening; never silently clips a judgment."""
    ellipsis = "…"
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    if max_bytes < len(ellipsis.encode("utf-8")):
        return ""
    cut = text.encode("utf-8")[: max_bytes - len(ellipsis.encode("utf-8"))]
    while cut:
        try:
            return cut.decode("utf-8") + ellipsis
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ellipsis


def format_hexagram(h: Hexagram, *, max_bytes: int = 180) -> str:
    """Four lines, joined by '\\n': line 1 is `"☯ Hexagram {n}"`, line 2 is
    `"{symbol} {pinyin} · {english}"`, line 3 is the bare judgment, line 4 is
    the fixed "🌱 same for you all day" footer. The number sits on its own
    line so the title reads cleanly on a narrow phone screen. Kept <= 180
    UTF-8 bytes for all 64 hexagrams. For a tighter radio budget, decorative
    emoji are removed first and the determinism note is shortened. All 64
    complete judgments fit the normal MeshCore 120-byte budget. Only an
    unusually smaller caller budget uses an explicit ellipsis."""
    line1 = f"☯ Hexagram {h.number}"
    line2 = f"{hexagram_symbol(h.number)} {h.pinyin} · {h.english}"
    full = "\n".join((line1, line2, h.judgment, _ICHING_FOOTER))
    if len(full.encode("utf-8")) <= max_bytes:
        return full

    compact = "\n".join(
        (f"Hexagram {h.number}", line2, h.judgment, _ICHING_COMPACT_FOOTER)
    )
    if len(compact.encode("utf-8")) <= max_bytes:
        return compact

    header = f"Hexagram {h.number} · {h.english}"
    suffix = "\n" + _ICHING_COMPACT_FOOTER
    judgment_budget = max_bytes - len((header + "\n" + suffix).encode("utf-8"))
    return "\n".join(
        (header, _truncate_with_ellipsis(h.judgment, max(0, judgment_budget)),
         _ICHING_COMPACT_FOOTER)
    )


# King Wen sequence, 1-64. Judgments are original paraphrases (see module
# docstring) -- short, plain-English gestures at each hexagram's traditional
# theme, never a translation of any specific copyrighted edition.
HEXAGRAMS: tuple[Hexagram, ...] = (
    Hexagram(1, "Qián", "The Creative", "great strength rising; act with clear purpose"),
    Hexagram(2, "Kūn", "The Receptive", "yield and support; steady ground carries all"),
    Hexagram(3, "Zhūn", "Difficulty at the Beginning", "hard sprouting; patience roots the new"),
    Hexagram(4, "Méng", "Youthful Folly", "not knowing yet; ask sincerely, learn slowly"),
    Hexagram(5, "Xū", "Waiting", "hold steady; the moment ripens on its own"),
    Hexagram(6, "Sòng", "Conflict", "dispute drains; settle early, avoid the courtroom"),
    Hexagram(7, "Shī", "The Army", "discipline in numbers; a firm hand guides many"),
    Hexagram(8, "Bǐ", "Holding Together", "unite freely; alliance strengthens the willing"),
    Hexagram(9, "Xiǎochù", "Small Taming", "gentle restraint; small gains accumulate quietly"),
    Hexagram(10, "Lǚ", "Treading", "step carefully among the strong; courtesy protects"),
    Hexagram(11, "Tài", "Peace", "heaven and earth meet; harmony flows both ways"),
    Hexagram(12, "Pǐ", "Standstill", "channels blocked; withdraw and wait it out"),
    Hexagram(13, "Tóngrén", "Fellowship", "shared purpose; open company outlasts a clique"),
    Hexagram(14, "Dàyǒu", "Great Possession", "abundance held lightly; share what you gather"),
    Hexagram(15, "Qiān", "Modesty", "the humble mountain; quiet worth needs no boast"),
    Hexagram(16, "Yù", "Enthusiasm", "willing hearts follow; joy moves the whole mesh"),
    Hexagram(17, "Suí", "Following", "adapt to the moment; loyalty earns loyalty"),
    Hexagram(18, "Gǔ", "Work on the Decayed", "mend what rotted; effort restores the neglected"),
    Hexagram(19, "Lín", "Approach", "growing influence; lead gently as trust builds"),
    Hexagram(20, "Guān", "Contemplation", "step back and observe; clarity comes from distance"),
    Hexagram(21, "Shìhé", "Biting Through", "cut through what obstructs; justice clears the path"),
    Hexagram(22, "Bì", "Grace", "beauty adorns the essential; substance still matters most"),
    Hexagram(23, "Bō", "Splitting Apart", "decline sets in; shelter what remains, wait patiently"),
    Hexagram(24, "Fù", "Return", "turning point; the light comes back after darkness"),
    Hexagram(25, "Wúwàng", "Innocence", "act without scheming; sincerity needs no disguise"),
    Hexagram(
        26, "Dàchù", "Great Taming", "great restraint stores great power; discipline pays later"
    ),
    Hexagram(27, "Yí", "Nourishment", "mind what feeds you, in word and in food"),
    Hexagram(28, "Dàguò", "Great Excess", "the beam bends; extraordinary times ask for care"),
    Hexagram(29, "Kǎn", "The Abysmal", "danger repeats; stay true and the water finds a way"),
    Hexagram(30, "Lí", "The Clinging", "clarity needs an anchor; light clings to what holds it"),
    Hexagram(
        31, "Xián", "Influence", "mutual attraction; respond honestly and connection deepens"
    ),
    Hexagram(32, "Héng", "Duration", "steady course; endurance is built, not merely felt"),
    Hexagram(
        33, "Dùn", "Retreat", "withdraw with dignity; distance preserves what closeness spends"
    ),
    Hexagram(
        34, "Dàzhuàng", "Great Power", "strength without restraint self-defeats; mind the gate"
    ),
    Hexagram(35, "Jìn", "Progress", "advance in the open; recognition follows honest effort"),
    Hexagram(36, "Míngyí", "Darkening of the Light", "hide your brightness when the hour is dark"),
    Hexagram(
        37, "Jiārén", "The Family", "order at home radiates outward; small bonds hold the rest"
    ),
    Hexagram(38, "Kuí", "Opposition", "differences persist; seek small common ground first"),
    Hexagram(39, "Jiǎn", "Obstruction", "the path is blocked; turn inward, gather allies, wait"),
    Hexagram(40, "Xiè", "Deliverance", "tension releases; forgive quickly once danger passes"),
    Hexagram(41, "Sǚn", "Decrease", "give up a little now; sincerity outweighs the offering"),
    Hexagram(
        42, "Yì", "Increase", "benefit flows downward; generosity above lifts those below"
    ),
    Hexagram(
        43, "Guài", "Breakthrough", "resolve firmly but without violence; announce your stand"
    ),
    Hexagram(44, "Gòu", "Coming to Meet", "an unplanned meeting; watch what you let in"),
    Hexagram(
        45, "Cuì", "Gathering Together", "people gather to one center; keep the altar honest"
    ),
    Hexagram(
        46, "Shēng", "Pushing Upward", "slow ascent; small steady steps outclimb a single leap"
    ),
    Hexagram(47, "Kùn", "Oppression", "resources thin; keep your word even when unheard"),
    Hexagram(
        48, "Jǐng", "The Well", "the source stays constant; keep the well clean for all"
    ),
    Hexagram(49, "Gé", "Revolution", "real change waits for its day, then moves quickly"),
    Hexagram(
        50, "Dǐng", "The Cauldron", "transform raw into useful; tend the fire, mind the vessel"
    ),
    Hexagram(
        51, "Zhèn", "The Arousing", "shock arrives; steady nerves turn alarm into readiness"
    ),
    Hexagram(
        52, "Gèn", "Keeping Still", "stillness in its own place; stop pursuing, and rest"
    ),
    Hexagram(
        53, "Jiàn", "Development", "gradual growth roots deepest; do not rush the seedling"
    ),
    Hexagram(
        54, "Guīmèi", "The Marrying Maiden", "know your place in the arrangement; act with care"
    ),
    Hexagram(55, "Fēng", "Abundance", "peak brightness casts the longest shadow; stay humble"),
    Hexagram(56, "Lǚ", "The Wanderer", "a guest everywhere; travel light and keep your manners"),
    Hexagram(57, "Xùn", "The Gentle", "wind finds every gap; gentle persistence outlasts force"),
    Hexagram(58, "Duì", "The Joyous", "shared pleasure strengthens bonds; joy spreads honestly"),
    Hexagram(
        59, "Huàn", "Dispersion", "scattered parts; a shared center gathers them home again"
    ),
    Hexagram(
        60, "Jié", "Limitation", "healthy limits sweeten what they bound; too tight breaks it"
    ),
    Hexagram(
        61, "Zhōngfú", "Inner Truth", "sincerity reaches even the wary; truth needs no loud proof"
    ),
    Hexagram(
        62, "Xiǎoguò", "Small Excess", "mind small matters now; leave the grand gesture for later"
    ),
    Hexagram(
        63,
        "Jìjì",
        "After Completion",
        "the crossing is done; watch closely, order still needs tending",
    ),
    Hexagram(
        64,
        "Wèijì",
        "Before Completion",
        "almost there; the last step wants the same care as the first",
    ),
)

assert len(HEXAGRAMS) == 64


def local_day(now: int, tz: str) -> str:
    """`YYYY-MM-DD` calendar day at unix timestamp `now`, in IANA zone `tz`.

    `tz` is assumed already validated (config.py does this eagerly at load
    time via `zoneinfo.ZoneInfo`); an unknown zone name raises here too,
    but that path should never be reached in production.
    """
    return datetime.fromtimestamp(now, tz=ZoneInfo(tz)).strftime("%Y-%m-%d")


def local_midnight(now: int, tz: str) -> int:
    """Unix epoch seconds of local midnight (start of the local calendar
    day) at unix timestamp `now`, in IANA zone `tz`. Companion to
    `local_day`; used by GARDEN/RECORD's "since local midnight" windows.
    Same validity assumption as `local_day`.
    """
    dt = datetime.fromtimestamp(now, tz=ZoneInfo(tz))
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def local_iso(now: int, tz: str) -> str:
    """Full local ISO-8601 timestamp (with UTC offset) at unix timestamp
    `now`, in IANA zone `tz` -- e.g. "2024-01-01T12:00:00-08:00". Companion
    to `local_day`; used by the CLI's `requests`/`guestbook list` timestamp
    columns. Same validity assumption as `local_day`.
    """
    return datetime.fromtimestamp(now, tz=ZoneInfo(tz)).isoformat()


def hexagram_for(sender_node_id: str, day: str, salt: str) -> int:
    """Deterministic hexagram number (1..64) for one sender on one local
    calendar day: `sha256(f"{salt}|{sender_node_id}|{day}")`, first 4 bytes
    as a big-endian int, mod 64, plus 1.

    Same `(sender_node_id, day, salt)` always yields the same number; a
    different sender, day, or salt yields (with high probability, not a
    guarantee) a different one. Pure function -- no per-user state is ever
    stored to make this determinism happen.
    """
    digest = hashlib.sha256(f"{salt}|{sender_node_id}|{day}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 64 + 1


def iching_text(sender_node_id: str, day: str, salt: str, *, max_bytes: int = 180) -> str:
    """The day's deterministic hexagram, formatted for the transport budget."""
    return format_hexagram(
        HEXAGRAMS[hexagram_for(sender_node_id, day, salt) - 1],
        max_bytes=max_bytes,
    )


@dataclass(frozen=True)
class HaikuTemplate:
    lines: tuple[str, str, str]
    # Used only when the operator has not configured [service] haiku_place
    # (or it sanitizes to empty). Deliberately generic -- never a real place.
    default_place: str = "the mesh"


# 14 original garden/mesh haiku. `{senders}`/`{guestbook}` are live counts
# (distinct senders in the last 24h, visible guestbook entries); `{place}`
# is the operator-configured haiku_place, or this template's default_place
# when that's empty. Every template's default_place is a generic word, never
# a real, specific location.
HAIKUS: tuple[HaikuTemplate, ...] = (
    HaikuTemplate(("Packets drift like leaves", "over {place} at dusk", "signal finds its way")),
    HaikuTemplate(
        ("{senders} voices tonight", "murmur across the still air", "the node listens on")
    ),
    HaikuTemplate(
        ("{place} keeps its book", "{guestbook} signatures glow soft", "static hums approval"),
        default_place="this hill",
    ),
    HaikuTemplate(
        ("Cold rain, warm antenna", "{senders} strangers say hello", "the mesh remembers"),
    ),
    HaikuTemplate(
        ("Battery holds on", "one more hop through cold static", "morning finds the node")
    ),
    HaikuTemplate(
        ("Moss grows on the mast", "{place} exhales slow static", "spring waits under snow"),
        default_place="this ridge",
    ),
    HaikuTemplate(
        ("A single ping lands", "far light answers, faint and true", "the mesh breathes at night")
    ),
    HaikuTemplate(
        ("{guestbook} names in the book", "each one a small lit window", "the garden remembers")
    ),
    HaikuTemplate(
        ("Seedling in the queue", "waiting for its careful turn", "growth needs no hurry")
    ),
    HaikuTemplate(
        ("Fog over {place}", "packets hop from ridge to ridge", "quiet does its work"),
        default_place="the garden",
    ),
    HaikuTemplate(
        ("Cold antenna hums", "{senders} calls since the sun rose", "static learns your name")
    ),
    HaikuTemplate(("Old radio sleeps", "one bright packet wakes it now", "the mesh stays awake")),
    HaikuTemplate(
        ("Stars above the mast", "{place} answers in low tones", "signal, then silence"),
        default_place="this garden",
    ),
    HaikuTemplate(
        ("Night settles in slow", "the counters tick past {guestbook}", "the mesh keeps its watch")
    ),
)

assert 12 <= len(HAIKUS) <= 16


def haiku_text(
    *,
    senders_24h: int,
    guestbook_visible: int,
    place: str,
    rng: random.Random | random.SystemRandom | None = None,
    max_bytes: int = 180,
) -> str:
    """One random haiku, three lines within the transport UTF-8 byte budget.
    `place` is the operator-configured haiku_place (empty string when
    unset); each template falls back to its own generic default_place in
    that case. `rng` defaults to a fresh `random.SystemRandom()`."""
    if rng is None:
        rng = random.SystemRandom()
    template = rng.choice(HAIKUS)
    word = place or template.default_place
    def render(location: str) -> str:
        return "\n".join(
            line.format(senders=senders_24h, guestbook=guestbook_visible, place=location)
            for line in template.lines
        )

    rendered = render(word)
    if len((rendered + _TRAILING_EMOJI).encode()) <= max_bytes:
        return rendered + _TRAILING_EMOJI
    if len(rendered.encode()) <= max_bytes:
        return rendered
    occurrences = sum(line.count("{place}") for line in template.lines)
    available = max_bytes - len(render("").encode())
    if occurrences and available >= 3 * occurrences:
        return render(_truncate_with_ellipsis(word, available // occurrences))
    return _truncate_with_ellipsis("HAIKU unavailable: message budget too small.", max_bytes)

