"""Positions a visitor deliberately shared by DM.

The MeshCore companion app can send its location as plain text
("Saved position: 37.774929, -122.419416" or "GPS position: ..."). When
`[observatory] dm_position_capture` is on, the daemon records the parsed
coordinates in `dm_positions` and acknowledges by DM. The raw text is
never stored; only the sender, our receive time, the kind, and lat/lon.

This table is independent of `node_positions` (radio-advertised
POSITION_APP packets), so the observatory's plausibility, distance, and
dashboard logic never reads it.

Experimental and operator-facing for now: positions are kept until the
operator deletes them with `mgarden positions delete` (no automatic
retention), and nothing on the radio or the dashboard reads them back. A
future shared use, such as an opt-in map, would build on this table.
"""

from __future__ import annotations

import sqlite3


def record_dm_position(
    conn: sqlite3.Connection, node_id: str, kind: str, lat: float, lon: float, now: int
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO dm_positions(node_id, server_time, kind, lat, lon) "
            "VALUES (?, ?, ?, ?, ?)",
            (node_id, now, kind, lat, lon),
        )


def list_dm_positions(
    conn: sqlite3.Connection,
    *,
    node_id: str | None = None,
    since: int | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Newest first. `node_id` and `since` (UTC epoch seconds) narrow the result."""
    where, params = [], []
    if node_id is not None:
        where.append("node_id = ?")
        params.append(node_id)
    if since is not None:
        where.append("server_time >= ?")
        params.append(since)
    sql = "SELECT id, node_id, server_time, kind, lat, lon FROM dm_positions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY server_time DESC, id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def delete_dm_positions(
    conn: sqlite3.Connection,
    *,
    ids: list[int] | None = None,
    id_range: tuple[int, int] | None = None,
    node_id: str | None = None,
    since: int | None = None,
    until: int | None = None,
) -> int:
    """Deletes the rows matching every given filter and returns how many.

    Filters combine with AND. `since` is inclusive and `until` exclusive
    (UTC epoch seconds). At least one filter is required, so a call can
    never silently empty the table.
    """
    where: list[str] = []
    params: list[object] = []
    if ids:
        where.append(f"id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    if id_range is not None:
        where.append("id BETWEEN ? AND ?")
        params.extend(id_range)
    if node_id is not None:
        where.append("node_id = ?")
        params.append(node_id)
    if since is not None:
        where.append("server_time >= ?")
        params.append(since)
    if until is not None:
        where.append("server_time < ?")
        params.append(until)
    if not where:
        raise ValueError("delete_dm_positions needs at least one filter")
    with conn:
        cur = conn.execute("DELETE FROM dm_positions WHERE " + " AND ".join(where), params)
    return cur.rowcount
