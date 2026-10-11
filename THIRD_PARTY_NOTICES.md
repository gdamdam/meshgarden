# Third-Party Notices

## meshgarden/meshcore_compat.py

`meshgarden/meshcore_compat.py` reimplements the documented behavior of a small number of helper
functions (`_meshcore_node_id`, `_normalize_hops`, `_parse_sender_name`, `_derive_message_id`) from
PotatoMesh's MeshCore ingestor package.

| Item      | Detail |
| --------- | ------ |
| Source    | https://github.com/l5yth/potato-mesh, `data/mesh_ingestor/protocols/meshcore/identity.py` and `data/mesh_ingestor/protocols/meshcore/messages.py`, fetched at `main` on 2026-09-18 |
| Copyright | (c) 2025-26 l5yth & contributors |
| License   | Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0) |

See the file header of `meshgarden/meshcore_compat.py` for why this is a faithful reimplementation
of the documented contract rather than a byte-verified copy (the fetch pipeline available in the
implementing session could not reliably preserve `<`/`>` comparison operators in inline code
samples), and for which upstream helpers were deliberately left unported.
