"""Linked identities: one person's MeshCore and Meshtastic radios as one
BBS account (WP10).

Only meaningful with a shared bbs.db, where both meshes' rows already live.
An identity is the stored BBS id the radio authenticated (`!xxxxxxxx` for
Meshtastic, `mc:!xxxxxxxx` for MeshCore); a display name or a short-id
match never proves anything. A link joins at most one identity per mesh.

Proof of control: the person sends `LINK` from one radio and receives a
single-use code by DM; sending `LINK <code>` from a radio on the *other*
mesh within `CHALLENGE_SECONDS` completes the link. Only the code's hash is
stored. The identity that started the link is the account's canonical id,
so every BBS table already keyed by that id (mail, reply routes, prefs,
alias, NEW cursors, LAB unlock, games) serves both radios through
`canonical()` with no second key to keep in sync.

Merge policy at link time, for the joining identity's rows (deterministic,
documented in docs/security-and-privacy.md):
- mail and reply routes addressed to it move to the account;
- mail prefs and alias: the account's own win, the joiner's apply only when
  the account has none (a conflicting joiner alias is released);
- NEW and board cursors: per key the lower mark, so nothing unread is
  skipped; pending (un-ACKed) cursor advances are dropped;
- LAB unlock: unlocked if either was;
- games: per day the further-along state (bbs_store's merge), with the
  duplicate taken back out of the day's totals, so no extra guesses and no
  double solve;
- a blocked identity cannot link, and a block on either identity blocks
  the account;
- command quotas stay per radio, as before linking.

UNLINK (either radio) or `mgarden identity unlink` splits the account. The
mailbox and everything else stays with the canonical identity; the other
radio starts fresh, keeping a copy of today's game state so unlinking
never hands out a new game.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3

from meshgarden import bbs_ids, bbs_store
from meshgarden.wordle import player_key

CHALLENGE_SECONDS = 600
MAX_FAILED_PER_DAY = 5
# Crockford base32 without I/L/O/U: unambiguous to read off a phone screen.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8  # 40 bits; single-use, 10 minutes, 5 tries per identity a day

SCHEMA_SQL = """
CREATE TABLE identity_links (
  node_id     TEXT PRIMARY KEY,   -- stored BBS id of one radio
  account_id  TEXT NOT NULL,      -- canonical stored id (the identity that started the link)
  linked_at   INTEGER NOT NULL
);
CREATE INDEX idx_identity_links_account ON identity_links(account_id);
CREATE TABLE identity_challenges (
  node_id     TEXT PRIMARY KEY,   -- identity that asked for the code; one live code each
  code_hash   TEXT NOT NULL UNIQUE,
  created_at  INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL
);
-- Link history for the operator: ids and event names only, never a code
-- or any message text.
CREATE TABLE identity_audit (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  at          INTEGER NOT NULL,
  event       TEXT NOT NULL,      -- link_started|linked|link_failed|unlinked|operator_unlinked
  node_id     TEXT NOT NULL,
  account_id  TEXT
);
CREATE INDEX idx_identity_audit_node_at ON identity_audit(node_id, at);
"""

TABLES = ("identity_links", "identity_challenges", "identity_audit")


class LinkError(Exception):
    """Refusal with a short, visitor-safe reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def normalize_code(text: str) -> str | None:
    code = "".join(text.split()).replace("-", "").upper()
    if len(code) != CODE_LENGTH or any(c not in _ALPHABET for c in code):
        return None
    return code


def _audit(conn, now: int, event: str, node_id: str, account_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO identity_audit(at, event, node_id, account_id) VALUES (?, ?, ?, ?)",
        (now, event, node_id, account_id),
    )


def canonical(conn: sqlite3.Connection, stored: str) -> str:
    row = conn.execute(
        "SELECT account_id FROM identity_links WHERE node_id = ?", (stored,)
    ).fetchone()
    return row[0] if row else stored


def linked_peer(conn: sqlite3.Connection, stored: str) -> str | None:
    """The other identity of `stored`'s account, or None."""
    account = canonical(conn, stored)
    row = conn.execute(
        "SELECT node_id FROM identity_links WHERE account_id = ? AND node_id <> ?",
        (account, stored),
    ).fetchone()
    return row[0] if row else None


def members(conn: sqlite3.Connection, stored: str) -> tuple[str, ...]:
    account = canonical(conn, stored)
    rows = conn.execute(
        "SELECT node_id FROM identity_links WHERE account_id = ?", (account,)
    ).fetchall()
    return tuple(sorted({stored, *(r[0] for r in rows)}))


def _blocked(conn, stored: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM blocked_senders WHERE node_id = ?", (stored,)
    ).fetchone() is not None


def is_blocked_any(conn: sqlite3.Connection, stored: str) -> bool:
    """A block on either identity of an account blocks both radios."""
    return any(_blocked(conn, node) for node in members(conn, stored))


def start(conn: sqlite3.Connection, stored: str, now: int) -> str:
    """Issue a fresh code for `stored` (replacing any earlier one)."""
    if linked_peer(conn, stored) is not None:
        raise LinkError("already_linked")
    if _blocked(conn, stored):
        raise LinkError("blocked")
    code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))
    with conn:
        conn.execute("DELETE FROM identity_challenges WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO identity_challenges(node_id, code_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET "
            "code_hash = excluded.code_hash, created_at = excluded.created_at, "
            "expires_at = excluded.expires_at",
            (stored, _hash(code), now, now + CHALLENGE_SECONDS),
        )
        _audit(conn, now, "link_started", stored)
    return code


def _failures_today(conn, stored: str, now: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM identity_audit WHERE node_id = ? AND event = 'link_failed' "
        "AND at > ?",
        (stored, now - 86400),
    ).fetchone()[0]


def complete(
    conn: sqlite3.Connection, stored: str, code_text: str, now: int, *, salt: str = ""
) -> str:
    """Link `stored` to the identity that was issued `code_text`. Returns the
    account (canonical) id. Raises LinkError; a failure is audited and
    counts toward the identity's daily limit."""
    if _failures_today(conn, stored, now) >= MAX_FAILED_PER_DAY:
        raise LinkError("too_many_attempts")
    code = normalize_code(code_text)
    row = None
    if code is not None:
        row = conn.execute(
            "SELECT node_id FROM identity_challenges WHERE code_hash = ? AND expires_at > ?",
            (_hash(code), now),
        ).fetchone()
    reason = None
    if row is None:
        reason = "bad_code"
    else:
        initiator = row[0]
        if bbs_ids.split(initiator)[0] == bbs_ids.split(stored)[0]:
            # Both radios on one mesh proves nothing new and would let one
            # person hold two identities' state on the same radio network.
            reason = "same_mesh"
        elif linked_peer(conn, stored) is not None or linked_peer(conn, initiator) is not None:
            reason = "already_linked"
        elif _blocked(conn, stored) or _blocked(conn, initiator):
            reason = "blocked"
    if reason is not None:
        with conn:
            _audit(conn, now, "link_failed", stored)
            if reason != "bad_code":
                # Valid code but refused: burn it so it cannot be retried.
                conn.execute("DELETE FROM identity_challenges WHERE node_id = ?", (row[0],))
        raise LinkError(reason)
    account = initiator
    with bbs_store._immediate(conn):
        conn.execute("DELETE FROM identity_challenges WHERE node_id = ?", (initiator,))
        _merge_into(conn, joiner=stored, account=account, salt=salt, now=now)
        conn.executemany(
            "INSERT INTO identity_links(node_id, account_id, linked_at) VALUES (?, ?, ?)",
            [(account, account, now), (stored, account, now)],
        )
        _audit(conn, now, "linked", stored, account)
    return account


def _merge_into(conn, *, joiner: str, account: str, salt: str, now: int) -> None:
    conn.execute("UPDATE mail SET recipient_node_id = ? WHERE recipient_node_id = ?",
                 (account, joiner))
    conn.execute(
        "UPDATE mail_reply_routes SET recipient_node_id = ? WHERE recipient_node_id = ?",
        (account, joiner),
    )
    conn.execute(
        "UPDATE mail_reply_routes SET sender_node_id = ? WHERE sender_node_id = ?",
        (account, joiner),
    )
    for table in ("mail_prefs", "mail_aliases", "lab_nodes"):
        # The account's own row wins; the joiner's moves only into a gap.
        conn.execute(
            f"UPDATE OR IGNORE {table} SET node_id = ? WHERE node_id = ?", (account, joiner)
        )
        conn.execute(f"DELETE FROM {table} WHERE node_id = ?", (joiner,))
    _merge_cursors(conn, "update_cursors", joiner, account, now)
    if _table_exists(conn, "board_cursors"):
        _merge_board_cursors(conn, joiner, account)
    conn.execute("DELETE FROM update_pending WHERE node_id = ?", (joiner,))
    for table in bbs_store.PLAYER_KEY_TABLES:
        _merge_games(conn, table, joiner, account, salt)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM pragma_table_list WHERE name = ?", (name,)
    ).fetchone() is not None


def _merge_cursors(conn, table: str, joiner: str, account: str, now: int) -> None:
    rows = {
        r[0]: json.loads(r[1])
        for r in conn.execute(
            f"SELECT node_id, snapshot FROM {table} WHERE node_id IN (?, ?)", (joiner, account)
        )
    }
    if joiner not in rows:
        return
    merged = rows[joiner]
    if account in rows:
        theirs = rows[account]
        # A key one side never saw stays unseen: keep only shared keys, at
        # the lower mark, so neither radio's unread entries are skipped.
        merged = {
            k: min(merged[k], theirs[k])
            for k in merged.keys() & theirs.keys()
            if isinstance(merged[k], int) and isinstance(theirs[k], int)
        }
    conn.execute(f"DELETE FROM {table} WHERE node_id IN (?, ?)", (joiner, account))
    if merged:
        conn.execute(
            f"INSERT INTO {table}(node_id, snapshot, updated_at) VALUES (?, ?, ?)",
            (account, json.dumps(merged), now),
        )


def _merge_board_cursors(conn, joiner: str, account: str) -> None:
    # Per board, the lower last-read mark; a board only one side read keeps
    # no mark, so its posts stay unread for the account.
    conn.execute(
        "UPDATE board_cursors SET last_read_id = MIN(last_read_id, ("
        "SELECT j.last_read_id FROM board_cursors AS j "
        "WHERE j.node_id = ? AND j.board = board_cursors.board)) "
        "WHERE node_id = ? AND board IN (SELECT board FROM board_cursors WHERE node_id = ?)",
        (joiner, account, joiner),
    )
    conn.execute(
        "DELETE FROM board_cursors WHERE node_id = ? AND board NOT IN "
        "(SELECT board FROM board_cursors WHERE node_id = ?)",
        (account, joiner),
    )
    conn.execute("DELETE FROM board_cursors WHERE node_id = ?", (joiner,))
    if _table_exists(conn, "board_cursor_pending"):
        conn.execute("DELETE FROM board_cursor_pending WHERE node_id = ?", (joiner,))


def _merge_games(conn, table: str, joiner: str, account: str, salt: str) -> None:
    schema = "bbs" if bbs_store.is_attached(conn) else "main"
    days = [r[0] for r in conn.execute(f"SELECT DISTINCT day FROM {schema}.{table}")]
    for day in days:
        old, new = player_key(joiner, day, salt), player_key(account, day, salt)
        o, n = (
            conn.execute(
                f"SELECT * FROM {schema}.{table} WHERE player_key = ? AND day = ?", (k, day)
            ).fetchone()
            for k in (old, new)
        )
        if o is None:
            continue
        if n is None:
            conn.execute(
                f"UPDATE {schema}.{table} SET player_key = ? WHERE player_key = ? AND day = ?",
                (new, old, day),
            )
            continue
        bbs_store._merge_player_rows(conn, table, day, salt, o, n)
        conn.execute(
            f"DELETE FROM {schema}.{table} WHERE player_key = ? AND day = ?", (old, day)
        )


def unlink(
    conn: sqlite3.Connection, stored: str, now: int, *, salt: str = "", today: str | None = None,
    operator: bool = False,
) -> str | None:
    """Split `stored`'s account. Returns the identity that left with a fresh
    mailbox (the non-canonical one), or None when `stored` was not linked."""
    peer = linked_peer(conn, stored)
    if peer is None:
        return None
    account = canonical(conn, stored)
    leaving = peer if stored == account else stored
    with bbs_store._immediate(conn):
        if today is not None:
            schema = "bbs" if bbs_store.is_attached(conn) else "main"
            for table in bbs_store.PLAYER_KEY_TABLES:
                # A copy, not a new player: totals are untouched, and the
                # leaving radio cannot start today's game over.
                cols = "guesses, solved" if table == "wordle_state" else "solved"
                conn.execute(
                    f"INSERT OR IGNORE INTO {schema}.{table}(player_key, day, {cols}, updated_at) "
                    f"SELECT ?, day, {cols}, updated_at FROM {schema}.{table} "
                    "WHERE player_key = ? AND day = ?",
                    (player_key(leaving, today, salt), player_key(account, today, salt), today),
                )
        conn.execute("DELETE FROM identity_links WHERE account_id = ?", (account,))
        _audit(conn, now, "operator_unlinked" if operator else "unlinked", stored, account)
    return leaving


def prune(conn: sqlite3.Connection, now: int, *, audit_days: int = 365) -> None:
    with conn:
        conn.execute("DELETE FROM identity_challenges WHERE expires_at <= ?", (now,))
        conn.execute("DELETE FROM identity_audit WHERE at < ?", (now - audit_days * 86400,))
