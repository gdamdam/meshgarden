# Federated public boards

Optional and off by default. Two or more gardens that share a secret exchange the text of selected
board posts (`POST <board> ...`). Nothing is sent over radio by federation.

## Configure

See `[federation]` in `config.example.toml`. Each garden sets `origin_id`, the `boards` to share
(same names on both sides), and one `[[federation.peers]]` entry per peer with its URL and a
`secret_file` holding the shared secret (at least 32 bytes, e.g. `openssl rand -hex 32`). The same
secret file content must be used on both gardens. Enabling it requires `origin_id` and a non-empty
`boards` list. The plain guestbook is never federated, and board names follow the usual
`[a-z0-9]{2,12}` shape.

The listener binds `listen_host:listen_port` (loopback by default). The protocol is plain HTTP with
HMAC authentication, so put a TLS reverse proxy in front of any non-local listener.

## Protocol

`GET /federation/v1/posts?since=<cursor>&limit=N` returns
`{origin, entries:[{id, board, created_at, body, author_tag, hidden}], tombstones:[ids], next_cursor}`.
Requests and responses carry `X-MG-Origin`, `X-MG-Timestamp` and `X-MG-Signature`
(HMAC-SHA256 over `method|path|query|timestamp|body`; responses are bound to the request
signature). The server rejects unknown origins, bad signatures, timestamps more than 300 s off,
and replays inside that window. It serves only that one GET path; every other request is a 404.
Peers cannot run commands or change configuration.

## What crosses the link

Only local-origin, visible posts of the configured boards: body text, board, time, a post id and an
opaque per-origin author tag (a keyed hash; it is never a node id or name, and is not linkable
across gardens). Imported posts are never re-exported, so there are no loops. Hidden posts are sent
without body, and recent hides and `mgarden guestbook delete` of board posts are sent as tombstones.

## On the receiving garden

Posts are stored once per `(origin, id)`, clamped (future times, body length as for POST),
checked against your banned-word list (matches are stored hidden), restricted to your configured
boards, and capped per peer (`max_stored_per_peer`, oldest pruned). `READ <board>` page 1 appends
visible federated posts after the local ones as `@origin/tag: text`. The cursor moves only after
the posts are committed, so interrupted, repeated or reordered exchanges are safe. Failures back
off exponentially up to one hour.

Operator controls: `mgarden federation status`, `mgarden federation hide <origin>:<id>` and
`unhide`. A local hide always wins; later remote updates never unhide. Removing a peer from
config purges its stored posts and cursor at the next start.

Tables (`federated_posts`, `federation_cursors`, `federation_deleted`, migration 23) live in the
main database even with a shared `database.bbs_path`; run federation on one instance of a shared
BBS only.

## Trust assumptions and limits

- Peers are trusted operators sharing a secret. A peer with the secret can post anything to the
  selected boards of your garden, subject to your moderation. Anyone holding the secret can also
  read what the server exports.
- You trust a peer's moderation only as far as your local overrides.
- Deletion is best-effort: a peer that already copied a post may keep it, and tombstones cover the
  latest 500 hides and deletes. Remote un-hides are not propagated.
- Clocks should agree within a few minutes; the allowed skew is 300 s.
- Author tags use `service.fortune_salt` as key material; set a private value.
- Boards are LAB-gated on the radio, but there is no per-board privacy setting: do not list a board
  you do not want shared.
