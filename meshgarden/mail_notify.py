"""Opt-in mail notices: a short DM telling a radio that notes wait for it.

Meshgarden otherwise only answers requests, so this is a transmission
nobody asked for at that moment. It is therefore doubly opt-in (the operator
switch `service.mail_notify_enabled`, on by default, and the visitor's own
`MAIL NOTIFY ON` on that radio) and bounded: at most one notice per
`mail_notify_interval_seconds`, `mail_notify_daily_cap` a local day, and
only when a note newer than the last notice arrived. The notice carries a
count, never a sender or any note text; the visitor still collects with MAIL.

State is per radio identity and per instance (each daemon notifies its own
mesh), so it lives in the instance's own database, not the shared BBS file.
"""

from __future__ import annotations

import sqlite3

SCHEMA_SQL = """
CREATE TABLE mail_notify (
  node_id         TEXT PRIMARY KEY,   -- stored BBS id of the radio that opted in
  enabled         INTEGER NOT NULL DEFAULT 0,
  notified_upto   INTEGER NOT NULL DEFAULT 0,  -- highest mail id already announced
  last_notice_at  INTEGER,
  day             TEXT,               -- local day count_today belongs to
  count_today     INTEGER NOT NULL DEFAULT 0,
  updated_at      INTEGER NOT NULL
);
"""


def notice_text(pending: int) -> str:
    noun = "note" if pending == 1 else "notes"
    return f"📬 {pending} {noun} waiting\nDM MAIL to read · MAIL NOTIFY OFF to stop"


def set_enabled(
    conn: sqlite3.Connection, node_id: str, enabled: bool, now: int, *, mailbox: str
) -> None:
    """Toggle notices for one radio. Turning them on starts after the notes
    already waiting, so opting in never triggers a notice by itself."""
    high = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM mail WHERE recipient_node_id = ? AND hidden = 0",
        (mailbox,),
    ).fetchone()[0]
    with conn:
        conn.execute(
            "INSERT INTO mail_notify(node_id, enabled, notified_upto, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET "
            "enabled = excluded.enabled, updated_at = excluded.updated_at, "
            "notified_upto = MAX(mail_notify.notified_upto, excluded.notified_upto)",
            (node_id, int(enabled), high, now),
        )


def is_enabled(conn: sqlite3.Connection, node_id: str) -> bool:
    row = conn.execute("SELECT enabled FROM mail_notify WHERE node_id = ?", (node_id,)).fetchone()
    return bool(row and row[0])


def due(
    conn: sqlite3.Connection,
    *,
    mailbox_of,
    now: int,
    day: str,
    interval_seconds: int,
    daily_cap: int,
) -> list[tuple[str, int, int]]:
    """(radio id, notes waiting, newest mail id) for every opted-in radio
    owed a notice now. `mailbox_of` maps a radio id to the mailbox it reads
    (its linked account, see accounts.py)."""
    out = []
    for row in conn.execute(
        "SELECT node_id, notified_upto, last_notice_at, day, count_today "
        "FROM mail_notify WHERE enabled = 1"
    ).fetchall():
        node_id, upto, last, row_day, count = row
        if row_day == day and count >= daily_cap:
            continue
        if last is not None and now - last < interval_seconds:
            continue
        mailbox = mailbox_of(node_id)
        newest, pending = conn.execute(
            "SELECT COALESCE(MAX(id), 0), COUNT(*) FROM mail "
            "WHERE recipient_node_id = ? AND hidden = 0",
            (mailbox,),
        ).fetchone()
        if pending and newest > upto:
            out.append((node_id, pending, newest))
    return out


def record_sent(conn: sqlite3.Connection, node_id: str, newest: int, now: int, day: str) -> None:
    """Called once Sender accepted the notice. The mark moves at hand-off, not
    on ACK: a lost notice is not resent (the note itself is never lost, and
    NEW/MAIL still show it), which keeps the airtime this feature can use
    strictly bounded."""
    with conn:
        conn.execute(
            "UPDATE mail_notify SET notified_upto = ?, last_notice_at = ?, updated_at = ?, "
            "count_today = CASE WHEN day = ? THEN count_today + 1 ELSE 1 END, day = ? "
            "WHERE node_id = ?",
            (newest, now, now, day, day, node_id),
        )
