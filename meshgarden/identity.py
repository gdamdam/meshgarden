"""Durable MeshCore identity bindings; public keys themselves are not stored."""

import hashlib

from meshgarden.database import connect


def bind_meshcore_identity(db_path: str, node_id: str, public_key: str) -> bool:
    """Pin an ID to its first full key; collisions remain quarantined across restarts.

    Each call owns its connection on the calling thread. A failed write raises,
    so the transport cannot admit an identity whose binding was not recorded.
    """
    fingerprint = hashlib.sha256(public_key.lower().encode("ascii")).hexdigest()
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO meshcore_identity_bindings (node_id, key_hash) VALUES (?, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET ambiguous = "
                "meshcore_identity_bindings.ambiguous OR key_hash != excluded.key_hash",
                (node_id, fingerprint),
            )
            row = conn.execute(
                "SELECT ambiguous FROM meshcore_identity_bindings WHERE node_id = ?", (node_id,)
            ).fetchone()
            return not row[0]
    finally:
        conn.close()
