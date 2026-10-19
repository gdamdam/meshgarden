"""Per-reader, per-board unread tracking with ACK-committed cursors.

Same contract as updates.py: a reply records a pending cursor keyed by its
request packet id, and `settle` merges it into board_cursors only once that
request is 'acked'. A lost ACK therefore re-delivers the same posts.
"""

import sqlite3

from meshgarden.guestbook import fallback_display_name
from meshgarden.sender import truncate_utf8

CURSOR_RETENTION_SECONDS = 90 * 86400
_PAGE_FETCH = 20  # more than any reply can hold; bounds the query

_UNREAD = (
    "SELECT g.entry_id, g.sender_node_id, g.display_name, g.message FROM guestbook g "
    "WHERE g.topic=:board AND g.visible=1 AND g.entry_id>:low {first} ORDER BY g.entry_id"
)


def _low(conn, node_id, board):
    row = conn.execute(
        "SELECT last_read_id FROM board_cursors WHERE node_id=? AND board=?", (node_id, board)
    ).fetchone()
    return (row[0], False) if row else (0, True)


def unread_counts(conn: sqlite3.Connection, node_id: str, now: int) -> list[tuple[str, int]]:
    """Boards with unread visible posts, most unread first. First visit to a
    board counts only the last 24h, like NEW. Read-only."""
    cursors = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT board, last_read_id FROM board_cursors WHERE node_id=?", (node_id,)
        )
    }
    out = []
    for (board,) in conn.execute(
        "SELECT DISTINCT topic FROM guestbook WHERE topic IS NOT NULL AND visible=1"
    ).fetchall():
        seen = board in cursors
        n = conn.execute(
            "SELECT COUNT(*) FROM guestbook WHERE topic=? AND visible=1 AND entry_id>?"
            + ("" if seen else " AND created_at>=?"),
            (board, cursors.get(board, 0)) + (() if seen else (now - 86400,)),
        ).fetchone()[0]
        if n:
            out.append((board, n))
    return sorted(out, key=lambda t: (-t[1], t[0]))


def boards_new_text(counts: list[tuple[str, int]], max_bytes: int) -> str:
    if not counts:
        return "Boards: nothing new"
    header = "Boards new"
    kept = []
    for board, n in counts:
        candidate = "\n".join([header, *kept, f"{board}:{n}"])
        if len(candidate.encode()) > max_bytes:
            break
        kept.append(f"{board}:{n}")
    if not kept:
        return truncate_utf8(header, max_bytes)
    return "\n".join([header, *kept])


def read_new(conn, node_id, board, now, max_bytes):
    """Oldest unread posts of `board` that fit one reply. Returns
    (text, high_id): high_id is the highest id actually included, or None
    when nothing was unread (nothing to remember)."""
    low, first = _low(conn, node_id, board)
    rows = conn.execute(
        _UNREAD.format(first="AND g.created_at>=:start" if first else ""),
        {"board": board, "low": low, "start": now - 86400},
    ).fetchall()
    if not rows:
        return truncate_utf8(f"#{board}: no new posts", max_bytes), None
    rows = rows[:_PAGE_FETCH]
    total = conn.execute(
        "SELECT COUNT(*) FROM guestbook WHERE topic=? AND visible=1 AND entry_id>?"
        + (" AND created_at>=?" if first else ""),
        (board, low) + ((now - 86400,) if first else ()),
    ).fetchone()[0]
    footer = f"more: READ {board} NEW"
    shown: list[str] = []
    high = None
    for r in rows:
        name = r["display_name"] or fallback_display_name(r["sender_node_id"])
        line = f"{name}: {r['message']}"
        remaining = total - len(shown) - 1
        head = f"#{board} new {len(shown) + 1} of {total}"
        tail = [footer] if remaining else []
        candidate = "\n".join([head, *shown, line, *tail])
        if len(candidate.encode()) > max_bytes:
            if shown:
                break
            # A single post longer than a reply is truncated, not skipped,
            # or the reader could never advance past it.
            room = max_bytes - len("\n".join([head, "", *tail]).encode())
            line = truncate_utf8(line, max(room, 0))
        shown.append(line)
        high = r["entry_id"]
    n = len(shown)
    body = [f"#{board} new {n} of {total}", *shown]
    if total > n:
        body.append(footer)
    return truncate_utf8("\n".join(body), max_bytes), high


def remember(conn, node_id, packet_id, board, high_id, now):
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO board_cursor_pending VALUES (?,?,?,?,?)",
            (node_id, packet_id, board, high_id, now),
        )


def settle(
    conn: sqlite3.Connection, now: int, max_age: int, *, local_id=lambda node_id: node_id,
    cursor_id=lambda node_id: node_id,
):
    """Commit pending cursors of acked requests (max with the stored cursor,
    so overlapping replies settle independently); drop failed/expired ones.
    `local_id` skips rows belonging to the other mesh in a shared BBS file.
    Pending rows are keyed by the asking radio (its ack is in that radio's
    request log); `cursor_id` maps it to the reader's linked account."""
    rows = conn.execute("SELECT * FROM board_cursor_pending").fetchall()
    with conn:
        for row in rows:
            local = local_id(row["node_id"])
            if local is None:
                continue
            req = conn.execute(
                "SELECT state FROM processed_requests WHERE sender_node_id=? AND packet_id=?",
                (local, row["packet_id"]),
            ).fetchone()
            state = req["state"] if req else None
            if state == "acked":
                conn.execute(
                    "INSERT INTO board_cursors VALUES (?,?,?,?) ON CONFLICT(node_id, board) "
                    "DO UPDATE SET last_read_id=MAX(last_read_id, excluded.last_read_id), "
                    "updated_at=excluded.updated_at",
                    (cursor_id(row["node_id"]), row["board"], row["high_id"], now),
                )
            elif (
                state not in ("failed", "dropped", "expired", "unknown", None)
                and now - row["created_at"] <= max_age
            ):
                continue
            conn.execute(
                "DELETE FROM board_cursor_pending WHERE node_id=? AND packet_id=? AND board=?",
                (row["node_id"], row["packet_id"], row["board"]),
            )
        conn.execute(
            "DELETE FROM board_cursors WHERE updated_at < ?", (now - CURSOR_RETENTION_SECONDS,)
        )
