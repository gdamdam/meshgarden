"""Transport-qualified node ids for the shared BBS tables.

A MeshCore node id is the first 4 bytes of its public key, which anyone can
brute-force to match any Meshtastic node id. Once both meshes share one
`bbs.db`, a bare `!xxxxxxxx` in a BBS table would therefore let a MeshCore
identity read a Meshtastic user's mail, use their LAB unlock, escape their
block or play their games. MeshCore ids are stored as `mc:!xxxxxxxx`
instead; Meshtastic ids stay bare so existing rows need no rewrite.

These functions are pure: whether qualification applies at all (only when
a shared `bbs_path` is configured) is decided by the daemon/CLI callers.
"""

from meshgarden.config import parse_node_id_argument

MESHCORE_PREFIX = "mc:"
# Accepted only as typed input (operator CLI, MAIL recipient): never stored,
# because a stored Meshtastic id is the bare one.
MESHTASTIC_PREFIX = "mt:"

_PREFIX_TRANSPORT = {MESHCORE_PREFIX: "meshcore", MESHTASTIC_PREFIX: "meshtastic"}


def qualify(node_id: str, transport: str) -> str:
    """The stored form of `node_id` heard on `transport`."""
    if transport == "meshcore" and not node_id.startswith(MESHCORE_PREFIX):
        return MESHCORE_PREFIX + node_id
    return node_id


def split(stored: str) -> tuple[str, str]:
    """(transport, bare id) of a stored id."""
    if stored.startswith(MESHCORE_PREFIX):
        return "meshcore", stored.removeprefix(MESHCORE_PREFIX)
    return "meshtastic", stored


def local_or_none(stored: str, transport: str) -> str | None:
    """The bare id when `stored` belongs to `transport`, else None: a
    foreign-mesh id is that mesh's daemon's to deliver, protect or label."""
    owner, bare = split(stored)
    return bare if owner == transport else None


# SQL function every `database.connect` connection carries: this daemon's
# bare `nodes` id for a stored BBS id, or NULL for the other mesh's. Name
# lookups join `nodes` through it, because `nodes` is per-instance: the same
# bare id in it is a different node on the other mesh, and a MeshCore id is
# cheap to forge into any Meshtastic one.
LOCAL_ID_SQL = "bbs_local_id"


def register_local_id_sql(conn, transport: str, *, shared: bool) -> None:
    """Install `LOCAL_ID_SQL` on `conn`. Without a shared bbs file every
    stored id is this mesh's own (bare) one, so it is the identity."""
    def local_id(stored):
        if stored is None or not shared:
            return stored
        return local_or_none(stored, transport)

    conn.create_function(LOCAL_ID_SQL, 1, local_id, deterministic=True)


def has_transport_prefix(text: str) -> bool:
    return text.strip()[:3].lower() in _PREFIX_TRANSPORT


def parse_qualified_argument(text: str) -> tuple[str | None, str]:
    """(transport named by an `mc:`/`mt:` prefix or None, canonical bare id)
    for typed input. Raises ValueError when the id part is not a full
    `!xxxxxxxx` node id."""
    t = text.strip()
    transport = _PREFIX_TRANSPORT.get(t[:3].lower())
    if transport is not None:
        t = t[3:]
    node_id = parse_node_id_argument(t)
    if node_id is None:
        raise ValueError(f"invalid node id: {text!r}")
    return transport, node_id


def argument_form(stored: str) -> str:
    """A stored id written so `parse_qualified_argument` reads it back to
    the same mesh whichever daemon parses it (a bare id would be taken as
    the parser's own mesh)."""
    owner, bare = split(stored)
    return stored if owner == "meshcore" else MESHTASTIC_PREFIX + bare


def typed_node_id(text: str, transport: str, *, shared: bool) -> str | None:
    """Stored form of a typed node id (operator CLI, MAIL recipient), or None.

    A bare id means the config's own transport. Without a shared bbs file
    nothing is qualified, and an id naming the other mesh is meaningless
    (that mesh has no rows here), so it is rejected rather than silently
    matched against this mesh's bare ids."""
    try:
        prefix_transport, node_id = parse_qualified_argument(text)
    except ValueError:
        return None
    owner = prefix_transport or transport
    if not shared:
        return node_id if owner == transport else None
    return qualify(node_id, owner)
