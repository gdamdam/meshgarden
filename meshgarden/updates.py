"""Request-driven updates with durable, ACK-committed high-water marks."""

import json
import sqlite3

from meshgarden.sender import truncate_utf8

SCHEMA_SQL = """
CREATE TABLE update_cursors (
 node_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE update_pending (
 node_id TEXT NOT NULL, packet_id INTEGER NOT NULL, snapshot TEXT NOT NULL,
 created_at INTEGER NOT NULL, PRIMARY KEY(node_id, packet_id)
);
"""


def summary(conn, node_id, now, *, boards_enabled=False, mail_enabled=False, max_bytes=120):
    row = conn.execute("SELECT snapshot FROM update_cursors WHERE node_id=?", (node_id,)).fetchone()
    previous = json.loads(row[0]) if row else {}
    snapshot = dict(previous)
    counts = {}
    for key, table, column, extra in (
        ("guestbook", "guestbook", "entry_id", "topic IS NULL AND visible=1"),
        ("boards", "guestbook", "entry_id", "topic IS NOT NULL AND visible=1"),
        ("news", "news", "id", "visible=1 AND (expires_at IS NULL OR expires_at > :now)"),
    ):
        if key == "boards" and not boards_enabled:
            continue
        high = conn.execute(f"SELECT COALESCE(MAX({column}),0) FROM {table}").fetchone()[0]
        # First visit: show the last day rather than the entire archive.
        clause = "" if key in previous else " AND created_at >= :start"
        counts[key] = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {extra} AND {column}>:low "
            f"AND {column}<=:high" + clause,
            {"now": now, "start": now - 86400, "low": previous.get(key, 0), "high": high},
        ).fetchone()[0]
        snapshot[key] = high
    mail = (
        conn.execute(
            "SELECT COUNT(*) FROM mail WHERE recipient_node_id=? AND hidden=0", (node_id,)
        ).fetchone()[0]
        if mail_enabled
        else 0
    )
    lines = [
        "NEW since last check" if row else "NEW in last 24h",
        f"{counts['guestbook']} GUESTBOOK | {counts['news']} NEWS",
    ]
    if boards_enabled:
        lines.append(f"{counts['boards']} board posts: BOARDS")
    if mail_enabled:
        lines.append(f"{mail} mail waiting: INBOX")
    text = "\n".join(lines)
    if len(text.encode()) > max_bytes:
        text = f"NEW GB:{counts['guestbook']} NEWS:{counts['news']}"
        if boards_enabled:
            text += f" BOARDS:{counts['boards']}"
        if mail_enabled:
            text += f" INBOX:{mail}"
    if len(text.encode()) > max_bytes:
        return truncate_utf8("NEW reply budget too small", max_bytes), previous
    return text, snapshot


def remember(conn, node_id, packet_id, snapshot, now):
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO update_pending VALUES (?,?,?,?)",
            (node_id, packet_id, json.dumps(snapshot), now),
        )


def settle(conn: sqlite3.Connection, now: int, max_age: int):
    rows = conn.execute(
        "SELECT p.*,r.state FROM update_pending p LEFT JOIN processed_requests r "
        "ON r.sender_node_id=p.node_id AND r.packet_id=p.packet_id"
    ).fetchall()
    with conn:
        for row in rows:
            if row["state"] == "acked":
                old = conn.execute(
                    "SELECT snapshot FROM update_cursors WHERE node_id=?", (row["node_id"],)
                ).fetchone()
                merged = json.loads(old[0]) if old else {}
                for key, value in json.loads(row["snapshot"]).items():
                    merged[key] = max(merged.get(key, 0), value)
                conn.execute(
                    "INSERT INTO update_cursors VALUES (?,?,?) ON CONFLICT(node_id) "
                    "DO UPDATE SET snapshot=excluded.snapshot,updated_at=excluded.updated_at",
                    (row["node_id"], json.dumps(merged), now),
                )
            elif (
                row["state"] not in ("failed", "dropped", "expired", "unknown", None)
                and now - row["created_at"] <= max_age
            ):
                continue
            conn.execute(
                "DELETE FROM update_pending WHERE node_id=? AND packet_id=?",
                (row["node_id"], row["packet_id"]),
            )
        conn.execute("DELETE FROM update_cursors WHERE updated_at < ?", (now - 90 * 86400,))
