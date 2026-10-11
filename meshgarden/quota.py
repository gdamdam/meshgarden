"""Per-node daily quotas: accepted-command cap and SIGN cap.

Enforced against `node_daily_quota` (migration 3), keyed on the caller's
already-computed local calendar day (`fortune.local_day`) rather than any
timezone logic of its own. This module only decides accept/refuse and
counts -- it never sends a reply and never inspects command content; the
silent-drop behavior for a refused count (no reply) is a wiring-step
decision.

Atomicity across two separate connections (the daemon's DB worker thread
vs. a CLI process) comes from doing the ensure-row-exists insert and the
check-and-increment update inside one `with conn:` transaction: SQLite
promotes the connection's write lock at the first write statement (the
`INSERT ... DO NOTHING`), so a second connection's own transaction blocks
(up to busy_timeout) until the first commits -- the increment can never
race past the limit.
"""

from __future__ import annotations

import sqlite3

_PRUNE_BATCH = 500


class DailyQuota:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        command_limit: int,
        sign_limit: int,
        post_limit: int = 5,
        mail_sent_limit: int = 3,
    ) -> None:
        self._conn = conn
        self._command_limit = command_limit
        self._sign_limit = sign_limit
        self._post_limit = post_limit
        self._mail_sent_limit = mail_sent_limit

    def _check_and_count(self, column: str, limit: int, node_id: str, day: str, now: int) -> bool:
        with self._conn:
            self._conn.execute(
                "INSERT INTO node_daily_quota (node_id, day, commands, signs, updated_at)"
                " VALUES (?, ?, 0, 0, ?) ON CONFLICT(node_id, day) DO NOTHING",
                (node_id, day, now),
            )
            cur = self._conn.execute(
                f"UPDATE node_daily_quota SET {column} = {column} + 1, updated_at = ?"
                f" WHERE node_id = ? AND day = ? AND {column} < ?",
                (now, node_id, day, limit),
            )
        return cur.rowcount > 0

    def check_and_count_command(self, node_id: str, day: str, now: int) -> bool:
        """Atomically upserts+increments this node's accepted-command count
        for `day`. Returns False (and does not count) once `command_limit`
        is already reached."""
        return self._check_and_count("commands", self._command_limit, node_id, day, now)

    def check_and_count_sign(self, node_id: str, day: str, now: int) -> bool:
        """Same as `check_and_count_command`, for new SIGN entries against
        `sign_limit`. A `sign_limit` of 0 refuses every SIGN."""
        return self._check_and_count("signs", self._sign_limit, node_id, day, now)

    def check_and_count_post(self, node_id: str, day: str, now: int) -> bool:
        """Same shape as check_and_count_sign, for daily_post_quota."""
        return self._check_and_count("posts", self._post_limit, node_id, day, now)

    def check_and_count_mail_sent(self, node_id: str, day: str, now: int) -> bool:
        """Same shape as check_and_count_sign, for daily_mail_quota."""
        return self._check_and_count("mails_sent", self._mail_sent_limit, node_id, day, now)

    def usage(self, node_id: str, day: str) -> tuple[int, int]:
        """Read-only: returns `(commands, signs)` counted so far for
        `(node_id, day)`, or `(0, 0)` if no row exists yet (e.g. queried
        before that node's first `check_and_count_*` call of the day).
        Never inserts a row and never mutates state -- unlike
        `check_and_count_command`/`check_and_count_sign`, this is safe to
        call purely to render a count (e.g. the MENU quota hint, or ME's
        own inline quota line)."""
        row = self._conn.execute(
            "SELECT commands, signs FROM node_daily_quota WHERE node_id = ? AND day = ?",
            (node_id, day),
        ).fetchone()
        if row is None:
            return (0, 0)
        return (row["commands"], row["signs"])

    def prune(self, now: int, keep_days: int = 2, batch: int = _PRUNE_BATCH) -> int:
        """Deletes node_daily_quota rows whose updated_at is older than
        `keep_days` days before `now`, in bounded batches (never an
        unbounded DELETE) -- same style as wordle.prune_wordle_state.
        Returns the number of rows deleted."""
        cutoff = now - keep_days * 86400
        total = 0
        while True:
            cur = self._conn.execute(
                "DELETE FROM node_daily_quota WHERE (node_id, day) IN"
                " (SELECT node_id, day FROM node_daily_quota WHERE updated_at < ? LIMIT ?)",
                (cutoff, batch),
            )
            self._conn.commit()
            if cur.rowcount <= 0:
                return total
            total += cur.rowcount
