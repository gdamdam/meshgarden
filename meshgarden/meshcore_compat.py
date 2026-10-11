"""MeshCore identity/decode helpers, reimplemented from PotatoMesh.

Source: PotatoMesh (https://github.com/l5yth/potato-mesh),
`data/mesh_ingestor/protocols/meshcore/identity.py` and
`.../meshcore/messages.py`, fetched at `main` on 2026-09-18.
Copyright (c) 2025-26 l5yth & contributors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

PROVENANCE NOTE (read before "fixing" a function here to look more like an
import): this session's URL-fetch pipeline converts fetched pages through an
HTML-to-Markdown step that silently drops bare `<`/`>` comparison operators
from inline code (e.g. `len(x) < 8` came back as `len(x)  str:`, with the
condition simply gone). That makes a byte-exact copy of the upstream
functions unverifiable in this environment. What follows is a faithful
REIMPLEMENTATION of the documented contract -- every docstring below is
either the fetched upstream docstring verbatim (those came through the
conversion intact; only inline code samples were affected) or a close
paraphrase of it, and the logic matches the described behavior, cross-checked
against the field shapes this repo independently confirmed by reading the
installed `meshcore==2.3.11` package directly (see meshgarden/meshcore_transport.py
and task-3B-1-report.md). Only the functions actually called from this repo
are included -- see the report for which upstream helpers were deliberately
left unported (they have no call site: `_meshcore_short_name`,
`_meshcore_adv_type_to_role`, `_normalize_path`, `_derive_synthetic_node_id`,
`_synthetic_node_dict` feed either a short-label scheme this repo's 3A
skeleton implements differently on purpose, or the observation/collector
path and synthetic-id path this epic's scope-decisions.md explicitly defers).
"""

from __future__ import annotations

import hashlib

# The Companion API's 255 sentinel means direct-route mode. It does not
# report the RF hop count; never classify it as a zero-hop reception.
_DIRECT_PATH_LEN = 255

# A cross-receiver-stable message id must fit the same 53-bit space PotatoMesh
# uses (safe as a JS/JSON number and as a SQLite INTEGER); this is the mask
# PotatoMesh's own `_MESHCORE_ID_MASK` applies to its digest.
_MESHCORE_ID_MASK = (1 << 53) - 1


def _meshcore_node_id(public_key_hex: str | None) -> str | None:
    """Derive a canonical ``!xxxxxxxx`` node ID from a MeshCore public key.

    Uses the first four bytes (eight hex characters) of the 32-byte public
    key, formatted as ``!xxxxxxxx``.

    Parameters:
        public_key_hex: 64-character lowercase hex string for the node's
            public key as returned by the MeshCore library. Also works when
            only a 6-byte (12 hex char) `pubkey_prefix` is supplied, because
            the prefix is by construction the first 6 bytes of the same key
            (meshcore-notes.md section 1).

    Returns:
        Canonical ``!xxxxxxxx`` node ID string, or ``None`` when the key is
        absent or too short.
    """
    if not public_key_hex or len(public_key_hex) < 8:
        return None
    return "!" + public_key_hex[:8].lower()


def _normalize_hops(path_len: object) -> int | None:
    """Return the library-normalized flood hop count; direct-route hops are unknown.

    The companion's 255 sentinel describes routing mode, not a zero-hop
    RF reception. The library decodes multi-byte hash widths before this call.
    """
    if path_len is None:
        return None
    try:
        value = int(path_len)
    except (TypeError, ValueError):
        return None
    if value == _DIRECT_PATH_LEN:
        return None
    if value < 0 or value > 63:
        return None
    return value


def _parse_sender_name(text: str | None) -> tuple[str | None, str]:
    """Split MeshCore's channel ``"SenderName: body"`` convention.

    MeshCore channel messages use the convention ``"SenderName: body"``.
    Only the first colon is treated as the separator (meshcore-notes.md
    sections 11, 12.1, quoting PotatoMesh's own docstring for
    `_parse_sender_name`).

    Parameters:
        text: Raw channel message body, or ``None``.

    Returns:
        ``(name, body)``. ``name`` is ``None`` when `text` is ``None`` or has
        no colon at all -- there is nothing to resolve against the contact
        roster either way.
    """
    if text is None:
        return None, ""
    if ":" not in text:
        return None, text
    name, _, body = text.partition(":")
    return name.strip(), body.lstrip()


def _derive_message_id(
    sender_identity: str,
    sender_ts: int,
    discriminator: str,
    text: str,
) -> int:
    """Derive a stable 53-bit message ID from sender-side MeshCore fields.

    MeshCore does not assign firmware-side packet IDs. This function produces
    a deterministic 53-bit integer fingerprint of a physical transmission so
    that the same packet heard by multiple receivers collapses to a single
    dedup key. Every component of the fingerprint is sender-side, ensuring
    two receivers with different clocks or roster state still compute the
    same value.

    Parameters:
        sender_identity: Stable sender identifier shared across receivers.
            For channel messages this is the lowercased+stripped sender name
            parsed from the message text via `_parse_sender_name`; for direct
            messages it is the sender's MeshCore ``pubkey_prefix``. Must be a
            string (use ``""`` when unavailable).
        sender_ts: Unix timestamp from the sender's clock (identical across
            receivers regardless of receiver-side clock skew).
        discriminator: Namespace tag separating message classes that could
            otherwise collide -- e.g. ``"dm"`` for direct messages, or
            ``"c<channel_idx>"`` for a channel message on that channel.
        text: The message body (for a channel message, with the
            ``"SenderName: "`` prefix already stripped).

    Returns:
        A non-negative integer in ``[0, 2**53)``.
    """
    digest = hashlib.sha256(
        "|".join((discriminator, sender_identity, str(sender_ts), text)).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") & _MESHCORE_ID_MASK
