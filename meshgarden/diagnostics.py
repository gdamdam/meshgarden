"""Compact route diagnostics; cached outgoing paths are never reception evidence."""

import json
import math
import sqlite3

from meshgarden.models import IncomingPacket
from meshgarden.sender import truncate_utf8


def route_reply(
    conn: sqlite3.Connection, node_id: str, obs: IncomingPacket, *,
    meshcore: bool, now: int, max_bytes: int,
) -> str:
    hops = None
    if obs.hop_start is not None and obs.hop_limit is not None:
        difference = obs.hop_start - obs.hop_limit
        if difference >= 0:
            hops = difference
    incoming = f"PATH RX: {hops} hops" if hops is not None else "PATH RX: hops unknown"
    if obs.rx_snr is not None and math.isfinite(obs.rx_snr):
        incoming += f"; SNR {obs.rx_snr:g}dB"
    if not meshcore:
        return truncate_utf8(incoming + "\nTX route unavailable on Meshtastic", max_bytes)
    row = conn.execute("SELECT stored_route FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
    try:
        route = json.loads(row[0]) if row and row[0] else None
        if not isinstance(route, dict):
            raise ValueError
        hashes = route['hashes']
        count = route['hops']
        cached_at = route['cached_at']
        if (not isinstance(hashes, list) or type(cached_at) is not int
                or (count is not None and (type(count) is not int or not 0 <= count <= 63))
                or (count is None and hashes) or (count is not None and count != len(hashes))
                or any(not isinstance(h, str) or len(h) not in (2, 4, 6)
                       or any(c not in '0123456789abcdefABCDEF' for c in h) for h in hashes)):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        return truncate_utf8(incoming + "\nTX cache: unavailable", max_bytes)
    age = max(0, now - cached_at)
    age_label = f"{age // 60}m" if age < 86400 else f"{age // 86400}d"
    header = incoming + f"\nTX cache {age_label}: "
    if count is None:
        return truncate_utf8(header + "route unknown", max_bytes)
    if not hashes:
        return truncate_utf8(header + "no repeater hashes\nCached; not RX hop evidence", max_bytes)
    footer = "\nRepeater hashes unverified"
    for n in range(len(hashes), -1, -1):
        path = '>'.join(h[:2].upper() for h in hashes[:n])
        if n < len(hashes):
            path += f" (+{len(hashes) - n})"
        body = header + path + footer
        if len(body.encode('utf-8')) <= max_bytes:
            return body
    return truncate_utf8(incoming + "\nTX cached route; HELP PATH", max_bytes)
