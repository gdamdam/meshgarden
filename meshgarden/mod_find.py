"""FIND <query>: search public guestbook entries, and LAB boards for an
unlocked sender (WP4). Mail, aliases, positions, games and DMs are never read.
"""

from __future__ import annotations

import unicodedata

from meshgarden.commands import Command
from meshgarden.modules import CommandModule, ModuleRequest

MIN_QUERY = 2
MAX_QUERY = 40
MAX_RESULTS = 3
# Bounds the work per request however large the guestbook grows. Matching is
# done in Python with casefold() rather than SQL LIKE: LIKE is ASCII-only
# case-insensitive, and this keeps %, _ and \ literal with no escaping.
SCAN_ROWS = 2000
_HINT = "GUESTBOOK #id / READ <board>"
_USAGE = "FIND <text>\nSearch public notes (boards too once LAB is unlocked). 2-40 chars"


def _clip(text: str, max_bytes: int) -> str:
    """Truncate on a UTF-8 boundary (never splits a codepoint)."""
    if max_bytes <= 0:
        return ""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def find_text(req: ModuleRequest) -> str:
    query = _norm(req.argument)
    if not query:
        return _USAGE
    if not MIN_QUERY <= len(query) <= MAX_QUERY:
        return f"FIND needs {MIN_QUERY}-{MAX_QUERY} characters"
    # LAB boards stay out of public-channel replies even for an unlocked sender.
    boards = req.lab_unlocked and not req.channel
    where = "visible=1" if boards else "visible=1 AND (topic IS NULL OR topic='')"
    rows = req.conn.execute(
        "SELECT entry_id, topic, message FROM ("
        f"SELECT entry_id, topic, message FROM guestbook WHERE {where} "
        "ORDER BY entry_id DESC LIMIT ?) ORDER BY entry_id DESC",
        (SCAN_ROWS,),
    ).fetchall()
    needle = query.casefold()
    hits = [r for r in rows if needle in _norm(r["message"]).casefold()][:MAX_RESULTS]
    header = _clip(f"🔎 FIND {query}", req.max_bytes)
    if not hits:
        return _clip(f"{header}\nNo matches", req.max_bytes)
    out = header
    used = len(out.encode("utf-8"))
    for row in hits:
        prefix = f"\n#{row['entry_id']} " + (f"{row['topic']} " if row["topic"] else "")
        # Share the budget so one long entry cannot crowd out the others.
        share = (req.max_bytes - used) // (len(hits) - hits.index(row))
        room = min(req.max_bytes - used, share) - len(prefix.encode("utf-8"))
        if room < 8:  # not enough left for a useful excerpt
            break
        line = prefix + _clip(_norm(row["message"]), room)
        out += line
        used += len(line.encode("utf-8"))
    if used + 1 + len(_HINT) <= req.max_bytes:
        out += "\n" + _HINT
    return out


def factory(cfg) -> list[CommandModule]:
    if not cfg.find.enabled:
        return []
    return [CommandModule(
        name="FIND", command=Command.FIND, usage=_USAGE, handler=find_text,
        menu="FIND <text> search notes", channel_ok=True, takes_argument=True,
    )]
