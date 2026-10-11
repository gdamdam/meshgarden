"""Request-driven mailbox: per-node opt-in prefs and stored notes.

Unlike an ordinary DM, a mail note's body IS retained -- both parties
consented (the recipient by turning MAIL ON, the sender by sending it; see
docs/security-and-privacy.md). Nothing here ever decides eligibility, rate
limits, or whether a note *should* be stored (opt-in/pending-cap checks are
the caller's job, main.py._handle_mail) -- this module only stores/reads/
prunes what it's told to.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from meshgarden.guestbook import message_hash

_PRUNE_BATCH = 500
MAIL_ALIAS_CHANGE_SECONDS = 86_400
_MAIL_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,15}$")
_RESERVED_MAIL_ALIASES = frozenset(
    {
        "alias", "boards", "book", "check", "daily", "emergency",
        "games", "garden", "guestbook", "guide", "help", "inbox", "info",
        "local", "mail", "menu", "moon", "new", "news",
        "path", "ping", "post", "read", "record", "reply", "route",
        "seen", "sign", "signal", "social", "sos", "stats", "sun", "unsign",
        "version", "week", "who", "whoami",
        "wordle",
    }
)


# Kept beside the storage API so the database migration can import one exact
# schema definition.  A reply route deliberately contains no message text (or
# hash): it is only the minimum addressing information needed for an
# authenticated recipient to answer a delivered note after the note itself is
# deleted.
REPLY_ROUTES_SQL = """
CREATE TABLE mail_reply_routes (
  mail_id            INTEGER PRIMARY KEY,
  sender_node_id     TEXT NOT NULL,
  recipient_node_id  TEXT NOT NULL,
  expires_at         INTEGER NOT NULL
);
CREATE INDEX idx_mail_reply_routes_expires ON mail_reply_routes(expires_at);
"""

MAIL_ALIASES_SQL = """
CREATE TABLE mail_aliases (
  node_id     TEXT PRIMARY KEY,
  alias       TEXT COLLATE NOCASE UNIQUE,
  changed_at  INTEGER NOT NULL
);
CREATE INDEX idx_mail_aliases_alias ON mail_aliases(alias COLLATE NOCASE);
"""


def normalize_mail_alias(value: str, *, allow_at: bool = True) -> str | None:
    """Return a canonical lowercase alias, or None when its shape is invalid."""
    text = value.strip().lower()
    if allow_at and text.startswith("@"):
        text = text[1:]
    if not _MAIL_ALIAS_RE.fullmatch(text) or text in _RESERVED_MAIL_ALIASES:
        return None
    return text


class MailAliases:
    """Stable, locally-scoped mail aliases bound to authenticated node IDs."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, node_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT alias FROM mail_aliases WHERE node_id=?", (node_id,)
        ).fetchone()
        return row["alias"] if row is not None else None

    def resolve(self, alias: str) -> str | None:
        normalized = normalize_mail_alias(alias)
        if normalized is None:
            return None
        row = self._conn.execute(
            "SELECT node_id FROM mail_aliases WHERE alias=? COLLATE NOCASE", (normalized,)
        ).fetchone()
        return row["node_id"] if row is not None else None

    def claim(self, node_id: str, alias: str, now: int) -> str:
        """Claim/change an alias. Returns claimed/unchanged/taken/cooldown/invalid."""
        normalized = normalize_mail_alias(alias, allow_at=False)
        if normalized is None:
            return "invalid"
        current = self._conn.execute(
            "SELECT alias, changed_at FROM mail_aliases WHERE node_id=?", (node_id,)
        ).fetchone()
        if current is not None and current["alias"] == normalized:
            return "unchanged"
        owner = self._conn.execute(
            "SELECT node_id FROM mail_aliases WHERE alias=? COLLATE NOCASE", (normalized,)
        ).fetchone()
        if owner is not None and owner["node_id"] != node_id:
            return "taken"
        if current is not None and now - current["changed_at"] < MAIL_ALIAS_CHANGE_SECONDS:
            return "cooldown"
        with self._conn:
            self._conn.execute(
                "INSERT INTO mail_aliases(node_id,alias,changed_at) VALUES (?,?,?) "
                "ON CONFLICT(node_id) DO UPDATE SET alias=excluded.alias, "
                "changed_at=excluded.changed_at",
                (node_id, normalized, now),
            )
        return "claimed"

    def release(self, node_id: str, now: int) -> bool:
        """Release the active alias while retaining the anti-churn timestamp."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE mail_aliases SET alias=NULL, changed_at=? "
                "WHERE node_id=? AND alias IS NOT NULL",
                (now, node_id),
            )
        return cur.rowcount > 0

    def list_active(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT node_id, alias, changed_at FROM mail_aliases "
            "WHERE alias IS NOT NULL ORDER BY lower(alias)"
        ).fetchall()

    def operator_set(self, node_id: str, alias: str, now: int, *, replace: bool = False) -> str:
        normalized = normalize_mail_alias(alias, allow_at=False)
        if normalized is None:
            return "invalid"
        owner = self.resolve(normalized)
        if owner is not None and owner != node_id and not replace:
            return "taken"
        with self._conn:
            if owner is not None and owner != node_id:
                self._conn.execute(
                    "UPDATE mail_aliases SET alias=NULL, changed_at=? WHERE node_id=?",
                    (now, owner),
                )
            self._conn.execute(
                "INSERT INTO mail_aliases(node_id,alias,changed_at) VALUES (?,?,?) "
                "ON CONFLICT(node_id) DO UPDATE SET alias=excluded.alias, "
                "changed_at=excluded.changed_at",
                (node_id, normalized, now),
            )
        return "claimed"

    def operator_release(self, alias: str, now: int) -> bool:
        normalized = normalize_mail_alias(alias)
        if normalized is None:
            return False
        with self._conn:
            cur = self._conn.execute(
                "UPDATE mail_aliases SET alias=NULL, changed_at=? "
                "WHERE alias=? COLLATE NOCASE",
                (now, normalized),
            )
        return cur.rowcount > 0


@dataclass(frozen=True)
class MailEntry:
    id: int
    created_at: int
    sender_node_id: str
    recipient_node_id: str
    message: str
    hidden: bool
    moderation_reason: str | None


@dataclass(frozen=True)
class ReplyRoute:
    mail_id: int
    sender_node_id: str
    recipient_node_id: str
    expires_at: int


def _entry_from_row(row: sqlite3.Row) -> MailEntry:
    return MailEntry(
        id=row["id"], created_at=row["created_at"], sender_node_id=row["sender_node_id"],
        recipient_node_id=row["recipient_node_id"], message=row["message"],
        hidden=bool(row["hidden"]), moderation_reason=row["moderation_reason"],
    )


class MailPrefs:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, node_id: str) -> bool:
        row = self._conn.execute(
            "SELECT enabled FROM mail_prefs WHERE node_id=?", (node_id,)
        ).fetchone()
        return bool(row["enabled"]) if row is not None else False

    def set(self, node_id: str, enabled: bool, now: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO mail_prefs (node_id, enabled, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET enabled=excluded.enabled, "
                "updated_at=excluded.updated_at",
                (node_id, int(enabled), now),
            )


class Mail:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def send(
        self, sender_node_id: str, recipient_node_id: str, normalized: str, now: int,
        *, moderation_reason: str | None = None,
    ) -> int:
        """Unconditionally inserts one note. Caller (main.py._handle_mail)
        has already decided the recipient is opted in and has room."""
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO mail (created_at, sender_node_id, recipient_node_id, message, "
                "message_hash, hidden, moderation_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    now, sender_node_id, recipient_node_id, normalized,
                    message_hash(normalized), int(moderation_reason is not None), moderation_reason,
                ),
            )
            return cur.lastrowid

    def pending_count(self, recipient_node_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM mail WHERE recipient_node_id=? AND hidden=0",
            (recipient_node_id,),
        ).fetchone()
        return row[0]

    def pending_for(self, recipient_node_id: str, limit: int = 50) -> list[MailEntry]:
        """Newest first. hidden=1 (moderated) notes are excluded -- same
        silent-to-the-visitor moderation convention as guestbook SIGN."""
        rows = self._conn.execute(
            "SELECT id, created_at, sender_node_id, recipient_node_id, message, hidden, "
            "moderation_reason FROM mail WHERE recipient_node_id=? AND hidden=0 "
            "ORDER BY id DESC LIMIT ?",
            (recipient_node_id, limit),
        ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def mark_delivered(self, ids: list[int]) -> None:
        """Hard-deletes: a delivered note is gone immediately (ruling 12).
        A no-op for an empty list."""
        if not ids:
            return
        with self._conn:
            self._conn.executemany("DELETE FROM mail WHERE id=?", [(i,) for i in ids])

    def consume_delivered(
        self,
        receipts: list[tuple[int, str, int]],
        *,
        expiry_days: int | None = None,
    ) -> int:
        """Delete fully delivered notes; retain any unread suffix after a partial ACK.

        Compare the original body in SQL so overlapping reads and duplicate ACKs
        cannot consume a later chunk. IDs are AUTOINCREMENT and never reused.

        When ``expiry_days`` is supplied, remember the body-free reply route in
        the same transaction as the guarded consume.  Its deadline is anchored
        to the note's original creation time, so acknowledging a note late can
        never extend retention past the configured mail window.  Omitting the
        argument preserves the pre-reply-routing behavior for callers that do
        not expose numbered replies. Returns the number of notes whose full,
        still-current body was deleted; partial and stale/duplicate receipts do
        not count as collected notes.
        """
        if expiry_days is not None and expiry_days < 1:
            raise ValueError("expiry_days must be at least 1")
        fully_consumed = 0
        with self._conn:
            for entry_id, original, count in receipts:
                if count <= 0:
                    continue
                row = self._conn.execute(
                    "SELECT created_at, sender_node_id, recipient_node_id FROM mail "
                    "WHERE id = ? AND message = ?",
                    (entry_id, original),
                ).fetchone()
                if row is None:
                    # An overlapping/duplicate receipt is stale.  In
                    # particular, do not manufacture a reply route for it.
                    continue
                if expiry_days is not None:
                    self._remember_reply_route(
                        entry_id,
                        row["sender_node_id"],
                        row["recipient_node_id"],
                        row["created_at"] + expiry_days * 86400,
                    )
                if count >= len(original):
                    cur = self._conn.execute(
                        "DELETE FROM mail WHERE id = ? AND message = ?", (entry_id, original)
                    )
                    fully_consumed += max(0, cur.rowcount)
                else:
                    remaining = original[count:]
                    self._conn.execute(
                        "UPDATE mail SET message = ?, message_hash = ? "
                        "WHERE id = ? AND message = ?",
                        (remaining, message_hash(remaining), entry_id, original),
                    )
        return fully_consumed

    def _remember_reply_route(
        self,
        mail_id: int,
        sender_node_id: str,
        recipient_node_id: str,
        expires_at: int,
    ) -> bool:
        """Insert an immutable route using the caller's current transaction."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO mail_reply_routes "
            "(mail_id, sender_node_id, recipient_node_id, expires_at) VALUES (?, ?, ?, ?)",
            (mail_id, sender_node_id, recipient_node_id, expires_at),
        )
        return cur.rowcount > 0

    def remember_reply_route(
        self,
        mail_id: int,
        sender_node_id: str,
        recipient_node_id: str,
        created_at: int,
        expiry_days: int,
    ) -> bool:
        """Persist a body-free route, bounded by the original note's lifetime.

        Routes are immutable because mail IDs are AUTOINCREMENT and never
        reused.  Returns ``True`` only when this call inserted the route.
        """
        if expiry_days < 1:
            raise ValueError("expiry_days must be at least 1")
        with self._conn:
            return self._remember_reply_route(
                mail_id,
                sender_node_id,
                recipient_node_id,
                created_at + expiry_days * 86400,
            )

    def lookup_reply_route(
        self, mail_id: int, recipient_node_id: str, now: int
    ) -> ReplyRoute | None:
        """Return an unexpired route only to the note's authenticated recipient."""
        row = self._conn.execute(
            "SELECT mail_id, sender_node_id, recipient_node_id, expires_at "
            "FROM mail_reply_routes WHERE mail_id = ? AND recipient_node_id = ? "
            "AND expires_at > ?",
            (mail_id, recipient_node_id, now),
        ).fetchone()
        if row is None:
            return None
        return ReplyRoute(
            mail_id=row["mail_id"],
            sender_node_id=row["sender_node_id"],
            recipient_node_id=row["recipient_node_id"],
            expires_at=row["expires_at"],
        )

    def prune_reply_routes(self, now: int, batch: int = _PRUNE_BATCH) -> int:
        """Delete expired body-free routes in bounded batches."""
        if batch < 1:
            raise ValueError("batch must be at least 1")
        total = 0
        while True:
            cur = self._conn.execute(
                "DELETE FROM mail_reply_routes WHERE mail_id IN "
                "(SELECT mail_id FROM mail_reply_routes WHERE expires_at <= ? LIMIT ?)",
                (now, batch),
            )
            self._conn.commit()
            if cur.rowcount <= 0:
                return total
            total += cur.rowcount

    def prune_expired(self, now: int, expiry_days: int, batch: int = _PRUNE_BATCH) -> int:
        """Deletes undelivered mail rows older than expiry_days, in bounded
        batches -- same shape as DailyQuota.prune/Retention.prune_*."""
        cutoff = now - expiry_days * 86400
        total = 0
        while True:
            cur = self._conn.execute(
                "DELETE FROM mail WHERE id IN "
                "(SELECT id FROM mail WHERE created_at < ? LIMIT ?)",
                (cutoff, batch),
            )
            self._conn.commit()
            if cur.rowcount <= 0:
                return total
            total += cur.rowcount
