"""LAB-tier per-node unlock state.

The LAB tier (POST/READ/BOARDS/MAIL) is gated by an operator-configured
word (config.py's `service.lab_unlock_word`); a node unlocks it, once, by
DMing that word as its whole message (see commands.py's `parse_command`
and main.py's `_process_obs` gate). Nothing here ever decides whether a
node's unlock attempt is well-formed or rate-limited -- that is the
caller's job (main.py's `_handle_lab_unlock`) -- this module only stores
and reads which node ids have unlocked, and when. The word itself is
never stored, logged, or returned by anything here.
"""

from __future__ import annotations

import sqlite3


class Lab:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def is_unlocked(self, node_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM lab_nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        return row is not None

    def unlock(self, node_id: str, now: int) -> None:
        """Idempotent: a node that unlocks again keeps its ORIGINAL
        unlocked_at (INSERT OR IGNORE), same first-write-wins convention as
        an explicit opt-in event elsewhere in this codebase."""
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO lab_nodes (node_id, unlocked_at) VALUES (?, ?)",
                (node_id, now),
            )

    def revoke(self, node_id: str) -> bool:
        """True if a row was actually deleted, False if the node was not
        unlocked to begin with."""
        with self._conn:
            cur = self._conn.execute("DELETE FROM lab_nodes WHERE node_id=?", (node_id,))
            return cur.rowcount > 0

    def list_nodes(self) -> list[tuple[str, int]]:
        """Oldest unlock first."""
        rows = self._conn.execute(
            "SELECT node_id, unlocked_at FROM lab_nodes ORDER BY unlocked_at"
        ).fetchall()
        return [(row["node_id"], row["unlocked_at"]) for row in rows]
