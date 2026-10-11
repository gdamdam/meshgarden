"""Small, operator-curated community information for Meshgarden.

The guide is deliberately static: radio visitors must never make a web
request.  NEWS is dynamic, but stores only short operator-authored notices.
Callers own transactions (the helpers do not commit).
"""

from __future__ import annotations

import sqlite3

from meshgarden.sender import truncate_utf8

# Root's next database migration should execute this statement.
NEWS_SQL = """
CREATE TABLE IF NOT EXISTS news (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at INTEGER NOT NULL,
  text TEXT NOT NULL,
  visible INTEGER NOT NULL DEFAULT 1,
  expires_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_news_visible_time ON news(visible, created_at DESC);
"""

# Leave room for the NEWS id and continuation hint. This makes every stored
# notice renderable in the default 120-byte radio response without truncation.
NEWS_MAX_BYTES = 84
NEWS_DEFAULT_TTL = 7 * 24 * 60 * 60

# Content is intentionally compact and points users to the canonical site for
# details that change (availability, prices, channel keys, and firmware).
GUIDE: dict[str, str] = {
    "local": ("🧭 Guide\nSTART · RADIO · COMPANION · REPEATER\n"
              "CHANNELS · TOOLS · DEVICES · COMMUNITY\nREGIONS · SOURCE"),
    "start": "🧭 Bay Area setup\nbayareameshcore.com",
    "radio": "📻 Bay Area radio\n910.525MHz · BW 62.5kHz · SF7 · CR5\nSource: 2026-09-20",
    "companion": ("📱 Companion\nFlash Community Firmware → Companion Bluetooth\n"
                  "Pair in the MeshCore app"),
    "repeater": ("📡 Repeater\nUse US/Canada preset · set location + admin password\n"
                 "Choose a unique 2-byte prefix"),
    "channels": ("💬 Channels are public\n#test is for tests\n"
                 "Send ‘test’ for a bot check"),
    "tools": ("🗺️ Maps + tools\nMeshMapper · CoreScope · Mesh Analyzer · Health Check\n"
              "bayareameshcore.com"),
    "devices": ("📱 Companion radios\nWio Tracker L1 Pro · Meshnology N39\n"
                "LilyGo T-Echo"),
    "community": "🌉 Bay Area help\nDiscord · docs · forums · maps\nbayareameshcore.com",
    "regions": ("📍 Regions\n#sf · #alameda · #eastbay · #sanleandro\n"
                "#novato · #marin · #sonoma · #95033"),
    "source": "🔗 Source · 2026-09-20\nbayareameshcore.com\nCheck the site for changes",
}

GUIDE_SOURCE = "https://bayareameshcore.com/"
GUIDE_SOURCES = {
    "start": GUIDE_SOURCE,
    "radio": "https://bayareameshcore.com/companion-setup/",
    "companion": "https://bayareameshcore.com/companion-setup/",
    "repeater": "https://bayareameshcore.com/repeater-setup/",
    "channels": "https://bayareameshcore.com/channels/",
    "tools": GUIDE_SOURCE,
    "devices": "https://bayareameshcore.com/recommended-devices/",
    "community": GUIDE_SOURCE,
    "regions": "https://bayareameshcore.com/channels/",
    "source": GUIDE_SOURCE,
    "local": GUIDE_SOURCE,
}


def _clean_news(text: str) -> str:
    text = " ".join(str(text).split())
    if not text:
        raise ValueError("news text is empty")
    if len(text.encode("utf-8")) > NEWS_MAX_BYTES:
        raise ValueError(f"news text exceeds {NEWS_MAX_BYTES} UTF-8 bytes")
    return text


def add_news(
    conn: sqlite3.Connection,
    text: str,
    created_at: int,
    expires_at: int | None = None,
) -> int:
    """Insert a visible notice and return its id; does not commit."""
    if expires_at is None:
        expires_at = int(created_at) + NEWS_DEFAULT_TTL
    if int(expires_at) <= int(created_at):
        raise ValueError("news expiry must be after creation")
    cur = conn.execute(
        "INSERT INTO news(created_at,text,visible,expires_at) VALUES (?,?,1,?)",
        (int(created_at), _clean_news(text), expires_at),
    )
    return int(cur.lastrowid)


def hide_news(conn: sqlite3.Connection, news_id: int) -> bool:
    """Hide a notice (recoverable); return whether a visible row changed."""
    cur = conn.execute("UPDATE news SET visible=0 WHERE id=? AND visible=1", (int(news_id),))
    return cur.rowcount == 1


def delete_news(conn: sqlite3.Connection, news_id: int) -> bool:
    """Remove a notice from the database entirely; does not commit.

    Unlike `hide_news`, this is not recoverable and leaves no row behind
    -- the operator escape hatch for content that should not persist on
    disk at all (a notice published in error, or one carrying something
    that must actually be erased rather than merely un-shown). Hiding
    stays the ordinary way to retire a notice, since nothing else in the
    tree ever purges this table: `expires_at` only stops a row being
    selected, it never removes it.

    Matches any row, hidden or visible, and returns whether one was
    actually removed.
    """
    cur = conn.execute("DELETE FROM news WHERE id=?", (int(news_id),))
    return cur.rowcount == 1


def list_news(
    conn: sqlite3.Connection, now: int, include_expired: bool = False
) -> list[sqlite3.Row]:
    """Return newest-first notices for operator CLI rendering."""
    if include_expired:
        return list(
            conn.execute("SELECT * FROM news WHERE visible=1 ORDER BY created_at DESC, id DESC")
        )
    return list(
        conn.execute(
            "SELECT * FROM news WHERE visible=1 AND (expires_at IS NULL OR expires_at>?) "
            "ORDER BY created_at DESC, id DESC",
            (int(now),),
        )
    )


def news_text(conn: sqlite3.Connection, page: int = 1, now: int = 0, max_bytes: int = 120) -> str:
    """Format one complete notice and advertise the next page."""
    if page < 1:
        page = 1
    rows = list_news(conn, now)
    rows = rows[page - 1 : page]
    if not rows:
        return truncate_utf8("📰 News\nNothing posted", max_bytes)
    row = rows[0]
    body = f"📰 News #{row['id']}\n{row['text']}"
    if page < len(list_news(conn, now)):
        body += f"\n→ NEWS {page + 1}"
    # Stored notices reserve enough room for the normal 120-byte reply. If a
    # caller asks for an unusually small budget, do not silently destroy text.
    if len(body.encode("utf-8")) > max_bytes:
        # Prefer the complete operator-authored notice over decoration or a
        # continuation hint. NEWS_MAX_BYTES guarantees this final form fits
        # the normal 120-byte transport budget.
        body = row["text"]
    if len(body.encode("utf-8")) > max_bytes:
        return "⚠️ News unavailable\nReply budget too small" if max_bytes >= 44 else ""
    return body


def guide_text(topic: str = "local", max_bytes: int = 120) -> str:
    """Return compact static local guidance, always within max_bytes."""
    key = topic.lower().strip().lstrip("#") or "local"
    text = GUIDE.get(key)
    if text is None:
        text = GUIDE["local"]
    return truncate_utf8(text, max_bytes)
