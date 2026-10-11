# Safety, security, and privacy

What Meshgarden stores, what it never stores, what is visible over the air, and how it limits abuse.
The operator is the only person who can read the database, so this page is written for both
operators and curious visitors.

## Summary

**Stored**
- A guestbook entry or board post, only when someone sends `SIGN` or `POST` on purpose.
- Mail notes, in readable form while they wait for pickup. The operator can read them with
  `mgarden mail --reveal`. They are encrypted over the air only, not at rest.
- Packet metadata for every node heard: sender id, signal quality, hop count, port, timestamps,
  and positions that nodes broadcast themselves.
- Experimental, off by default: coordinates a visitor shares by DM (`dm_positions`). Operator-only,
  kept until `mgarden positions delete`.
- Bounded, pseudonymous usage counters. No message text.

**Not stored**
- Ordinary DMs and unrecognized text, `BOOK` and `WORDLE` guesses, and the text of any packet.
- The LAB unlock word, which is never logged or printed.
- Raw message text anywhere, except under `run --debug` (see
  [below](#what-is-stored-what-is-not)).

**Over the air**
- In clear, for anyone in range: packet headers (`from`/`to` node ids, hop counts, timing) and the
  names and positions nodes broadcast.
- Encrypted: DM payloads. Public MQTT uplinks of other nodes can still turn header traffic into a
  public log (see [Metadata visible on the mesh](#metadata-visible-on-the-mesh)).
- Channel messages are visible to everyone on the channel.

## Radio identity, enrollment, and replacement

At startup and on every reconnect, the daemon reads the attached radio's own `myInfo.my_node_num`
and compares it with `radio.expected_node_id` in `config.toml`. A match enables replies. A
mismatch pauses all replies (passive observation continues) and shows as
`mismatch_reason: node_id_mismatch` in `status` and the logs.

**Replacing the service radio is a local, explicit operator action only.** Edit
`expected_node_id` to the new radio's ID and restart the daemon. No radio command, DM or protocol
message can change it, and the daemon never rewrites its own configuration or adopts a new radio
automatically.

### Identity collision quarantine

Each observed MeshCore node ID is pinned to a SHA-256 fingerprint of its full contact key.
Different keys that share the same shortened ID are quarantined: commands and outgoing replies
are refused, and the quarantine survives restarts.

- The raw full key is not stored in SQLite.
- After an upgrade, contacts are bound as they are first observed; historical full keys cannot be
  recovered.
- An unknown contact can contribute reception metadata but cannot run commands until its identity
  is resolved.
- Re-advertising or pruning activity rows does not clear a quarantine.

## Reliability and authentication caveats

- **Sender names are display labels, not identities.** A node's short and long name come from an
  unauthenticated `NODEINFO` packet it broadcasts about itself, so anyone can claim any name. Only
  the numeric node ID is used for rate limiting, blocking and deduplication. It is verified for
  the service radio and taken at face value for a remote sender's `from` field.
- **RSSI and SNR describe reception at this radio**, not necessarily the sender's link quality,
  because a relay may have carried the packet. Missing readings are never recorded as zero.
  Hop-derived values are labelled as derived and computed only when both inputs exist.
- **USB keepalive is not radio traffic.** The Meshtastic Python library keeps a 300-second
  heartbeat to the attached radio. That is a USB-serial keepalive, not a LoRa transmission.

### What the service transmits

The physical radio is never silent: Meshtastic firmware sends acknowledgements, telemetry, node
info and forwarded traffic on its own schedule. What Meshgarden itself originates is limited and
documented.

- **Replies.** On both transports, an audited reply to an eligible request.
- **MeshCore flood advert (MeshCore only).** Meshgarden sends a flood self-advertisement once on
  every successful connection and then every `[radio] meshcore_advert_interval_seconds` (default 4
  hours; `0` disables only the repeat). On real hardware the garden was unreachable until it had
  flood-advertised, because a node that never heard the radio cannot resolve a path back to it.
  It is presence beaconing: it carries no reply content and is never triggered by, or an answer
  to, any inbound message. It is the MeshCore analogue of the `NODEINFO` broadcasts Meshtastic
  firmware already sends. See [architecture.md](architecture.md) for where it sits in the
  connection lifecycle.
- **Contact pruning (MeshCore, opt-in).** With `[radio] meshcore_contact_prune_enabled = true`
  (off by default), Meshgarden deletes contacts from the radio's own table once per local day,
  least recently heard first, down to `meshcore_contact_target`, so a full table does not stop
  the radio adding new nodes. It never removes a contact that has sent the garden a command or
  mail, holds a LAB unlock or mail prefs, or is a repeater or room. It never logs a removed
  contact's name or key. This and `meshcore_autoadd_overwrite` are the only persisted radio
  writes.
- **`REPEATER` query (MeshCore, when configured).** Only an eligible command can start a bounded
  RF query, and only after its reply has reserved queue capacity. One query can be in flight,
  successful results are cached, and failures have a retry floor. The guest password stays in
  the private config, is omitted from `RadioConfig` representations, and is redacted from the
  MeshCore library's raw command and serial DEBUG records.

### Weather alerts and the network

With `[weather] enabled = true`, the daemon makes one outbound HTTPS request to `api.weather.gov`
every `poll_interval_seconds` (default 120). It carries only a `User-Agent` of
`meshgarden/<version> (<contact>)` and an `If-None-Match` ETag. Nothing about the mesh, its nodes
or its traffic is sent. The resulting lines are public broadcasts on the alert channel and hold
only NWS event names, zone names and times.

## MeshCore channel interaction

**MeshCore channel senders are unauthenticated, more starkly than Meshtastic display names.** A
`CHANNEL_MSG_RECV` frame carries no sender key or key prefix, only the free-text
`"SenderName: body"` convention client apps add, so anyone on the channel can type any known name.
Meshgarden requires the name to resolve uniquely to one contact before it accepts a knock or
command. That is an abuse bound, not authentication. Unknown and ambiguous names are dropped
silently.

Meshtastic has no channel-command surface.

### MeshCore channel knock

`[service] mode = "dm_only"` is the default. The configured channel then accepts only an exact
`!garden` or `!dm` knock.

- A knock sends one fixed invitation DM to the resolved contact. It truthfully says that
  *someone using their name* knocked.
- The cooldown is persistent and keyed by resolved node ID. A knock never uses up the
  impersonated node's command cooldown or quota.

### Channel mode

`mode = "channel"` also runs a fixed public, read-only subset and posts one response on the same
channel. The channel menu is the source of truth for that subset. Mail and inbox operations,
guestbook and board writes, stateful games, LAB unlocks and personalized node state stay DM-only.

- Channel responses are visible to everyone, get no remote ACK, and are never retried.
- The transport refuses to send on any channel index except the one resolved from
  `radio.meshcore_channel` at connection time.
- Because a claimed name can be spoofed, a channel command can use up the named contact's command
  quota or cooldown and can pick that contact's deterministic daily draw. It cannot read private
  mail or change private or published state.

If you do not accept that denial-of-service tradeoff, keep `dm_only` or set
`[radio] meshcore_channel = ""` to turn off all channel listening.

### Honest limit: sustained forged knocks

A sustained burst of forged knocks can still cost real reply throughput and airtime, not just one
bounded DM. Adverts are unauthenticated, so an attacker can mint a fresh contact identity for every
knock. `[radio] meshcore_max_pending_knocks` (default 1) bounds how many knock replies are queued
at once, but not the rate at which new ones are admitted.

An attacker who keeps cycling identities can keep that one slot occupied. Every admitted knock
uses a real transmit slot and a full `global_tx_cooldown_seconds`, so a long burst can cost roughly
half of the daemon's reply throughput and airtime. This is a deliberate trade: bounding
instantaneous queue occupancy needs no new persistent state, while bounding the rate over time
would. To remove the surface entirely, set `[radio] meshcore_channel = ""`.

## What is stored, what is not

- **`SIGN <message>` is the only thing published.** It is an explicit act of consent to add a
  visible guestbook entry.
- **Ordinary DMs are never stored or published.** `MENU`, `?`, `INFO`, `GUESTBOOK`, `STATS`,
  `HELP`, `SOS`, `EMERGENCY` and any unrecognized text leave no message content behind.
  Observation records (sender, channel, port number, signal quality and so on) never include
  packet text.
- **`BOOK <title>` guesses are never stored or logged**, like `SIGN`'s argument. Only the fixed
  reply text and a bounded correct or wrong outcome are counted.
- **`WORDLE <guess>` text is never stored or logged.** Only a bounded guess count and solved flag
  are kept for the current local day, then pruned, under a one-way hash of sender, day and salt.
  The raw node id and the guessed word are never kept.
- **`UNSIGN` hides, it never deletes.** Your newest visible entry stops appearing in `GUESTBOOK`,
  but the row stays in storage like an operator-hidden entry (`mgarden guestbook hide`). The
  operator can see it with `mgarden guestbook list --hidden` or remove it permanently with
  `mgarden guestbook delete`.
- **Mail bodies are the one kind of message text that is kept**, because both parties consented.
  See [Mailbox](#mailbox-mail).
- **A SIGN caught by the banned-word guard is stored but hidden**, and the visitor is not told. See
  [Banned-word guard](#banned-word-guard).
- **`run --debug` is the one exception to all of the above.** It prints every DM's raw text as
  received, to stderr and to a small owner-only rotated trace file (`mgarden tail` follows it).
  The only redaction is that a `LAB_UNLOCK` message logs as `<lab unlock redacted>`. The output
  never reaches the database, metrics or status file, and never runs unless the operator passes
  `--debug`. Do not use it for an unattended or systemd deployment, and keep its output readable
  by the operator only. See [operations.md](operations.md).

## Metadata visible on the mesh

- **Packet headers are cleartext mesh-wide by protocol.** `from`/`to` node IDs, hop counts and
  timing are readable by any node or listener in range. Only the DM payload is end-to-end
  encrypted (PKI). The daemon cannot change this.
- **Public MQTT uplinks can turn headers into a public log.** A third-party site fed by other
  nodes' MQTT uplinks (for example MeshView) can show which nodes exchanged DMs with the service
  node, never the content, which stays encrypted.
- **Meshgarden stores no more than that metadata** (sender, channel, port, signal quality, never
  packet text), so a public MQTT observer learns nothing beyond what Meshgarden's own records
  already limit themselves to.
- **Mitigations are radio-side.** The service radio sets `lora.config_ok_to_mqtt = false`
  (firmware 2.5 and later uplinkers should drop packets without the OK-to-MQTT bit), keeps its own
  MQTT uplink off, and disables Neighbor Info and position broadcasts (or uses coarse position
  precision). The tested service radio already ships this way; see "Hardware tested" in the
  README.
- **Visitors control their own exposure.** A visitor's DMs carry the visitor's own OK-to-MQTT
  flag, so a visitor who cares can set `ok_to_mqtt=false` on their own radio.

## What the observatory retains, and for how long

Besides guestbook, board and mail content, the observatory keeps packet metadata for every node it
hears, not just visitors who send commands. None of these tables hold packet or message text.

| Data | Table | Kept for |
|---|---|---|
| Sender id, SNR/RSSI, hop count, port, timestamps | `observations` | `[retention] observations_days` (default 7) |
| Per-node-per-day rollups | `node_daily` | `node_daily_days` (default 365) |
| Inferred mesh neighbours | `links` | `links_days` (default 30) |
| Aggregate daily stats | `daily_stats` | `aggregates_days` (default 90) |
| Node names (short and long, from unauthenticated `NODEINFO`) | `nodes` | While the node has been heard within `observations_days`, or indefinitely if it has a guestbook entry |
| Mail notes | see [Mailbox](#mailbox-mail) | see [Mailbox](#mailbox-mail) |

### Positions broadcast by nodes

Position data (`node_positions`, plus `nodes.last_position_*` and `node_daily.last_lat`/
`last_lon`) is captured only when a node's own `POSITION_APP` packet voluntarily broadcasts it, and
only while `[observatory] enabled_position_capture` (default `true`) is on. Turning it off stops
new capture but does not delete what is already stored. Captured positions are pruned on the same
`observations_days` schedule as other observations.

`[observatory] latitude`/`longitude` is the operator's own configured location. It is used only
for `GARDEN`/`RECORD` distance fields and is never derived from mesh traffic.

### Positions shared by DM (experimental)

`dm_positions` is a separate, experimental, operator-only table. It is filled only while
`[observatory] dm_position_capture` (default `false`) is on, and only from a visitor's own DM of
the MeshCore app's location share.

- Only the sender, receive time, kind and coordinates are stored, never the text.
- Rows are **not** pruned automatically. They stay until the operator removes them with
  `mgarden positions delete` (by id, id range, node or date range).
- Nothing on the radio, the dashboard or the JSON feeds reads them, and a channel message can
  never create one.

See [configuration.md](configuration.md) for the full `[retention]` and `[observatory]` keys, and
[operations.md](operations.md) for the storage cap and backup behavior around these tables.

## Directory (SEEN/WHO/ME/PING)

**SEEN, WHO and PING reveal only what the mesh already broadcasts.** Every field (last-heard time,
hop count, SNR, first-seen date, days active) comes from packets the sender already transmitted
over the air to anyone listening. Meshgarden adds no new transmission and reveals nothing about a
third party beyond structural metadata: never message text, never another node's guestbook or mail
content.

`SEEN` never reveals position, another node's display name beyond the one looked up, or a count of
their messages. A node not heard in the last 7 days is deliberately indistinguishable from one
never seen, because telling them apart would leak more than the mesh already broadcasts.

## Topic boards

**Boards are public by consent, exactly like the guestbook.** `POST <topic> <msg>` is the same
explicit, one-time act of publication consent as `SIGN`. Normalization, moderation and duplicate
suppression (scoped per topic) are identical. `READ` and `BOARDS` reveal nothing not already
published this way.

`UNSIGN` covers boards without special-casing: it hides whichever of a sender's guestbook or board
entries is newest overall, because boards share the `guestbook` table.

## Mailbox (MAIL)

**Mail is opt-in and two-party.** `MAIL ON`/`MAIL OFF` is per-node consent and defaults to
**off**. A note is stored only when the recipient has turned `MAIL ON` and has fewer than
`[service] mail_pending_limit` pending notes. Both parties consented to the note existing in
storage, which is why mail bodies, unlike ordinary DMs, are kept.

**Who can read a note.**
- While a note waits, its body sits in the database in readable form. Encryption protects it only
  over the air.
- The operator can read waiting bodies with `mgarden mail --reveal`, an explicit action recorded
  nowhere beyond normal shell history. The default `mgarden mail` output is counts only.
- After an acknowledged collection the body is deleted; see [community.md](community.md#mail) for
  what the reply route keeps.

**No probing.**
- The sender is never told whether the recipient opted in or whether their mailbox is full. Every
  well-formed `MAIL <node> <msg>` gets the same neutral acknowledgement, so `MAIL` cannot probe
  another node's settings.
- The same holds for a syntactically valid but unknown `@alias`, so sending cannot enumerate
  aliases, and there is no over-the-radio alias directory.
- Claiming an alias reports whether the name is available but reveals no owner.
- Stored-versus-refused is visible only in aggregate, through `meshgarden_mail_notes_stored` and
  `meshgarden_mail_notes_refused`.

**Aliases.** Mappings keep node IDs and a change timestamp locally. They are case-insensitively
unique and can be changed or released once per 24 hours.

**Retention.** A delivered note is deleted immediately. An undelivered note is pruned after
`[retention] mail_expiry_days` (default 7).

**Continuation.** A long note comes back in chunks, one per explicit `MAIL` request. Only an
acknowledged chunk is consumed, and the unseen rest stays pending. A failure or uncertain outcome
leaves the chunk available. Duplicate ACKs and overlapping reads cannot consume a later chunk.
Expiry still counts from the note's original creation time.

`MAIL`, `INBOX` and `REPLY` are public by default. `mail_public = false` puts them behind the
[LAB tier](#lab-tier). `NEW` cursors and the usefulness counters are described in
[community.md](community.md).

## Banned-word guard

When `[moderation] banned_words_file` is set, every `SIGN` is checked after normalization against
that file's terms, whole word only.

- **A match is stored, never silently discarded.** It is inserted with `visible=false` and
  `moderation_reason="banned_word"`, so it is invisible to `GUESTBOOK` and shows only in
  `mgarden guestbook list --hidden`.
- **It is silent to the visitor.** They get the same `🌱 Signed #N. Thanks!` ack as any other
  `SIGN`, with no sign it was flagged.
- **The term is not logged.** Only the bounded `moderation_reason` code and the
  `meshgarden_guestbook_entries_moderated` counter record it.
- The list is loaded once at daemon startup.

`banned_words_file = "default"` uses the conservative, English-only starting list packaged with
Meshgarden (about 65 common profanity and slur terms, whole-word matched, chosen to avoid
ambiguous everyday words). To find it on disk:

```bash
python -c "from importlib.resources import files; print(files('meshgarden') / 'data' / 'banned-words.default.txt')"
```

To review or extend the list, copy that file somewhere writable and point `banned_words_file` at
the copy. See [operations.md](operations.md) for `guestbook review` and `guestbook approve`, which
review or reverse an auto-hide.

## Quotas

Quotas are per node per local day (`[service] timezone`).

| Setting | Limits |
|---|---|
| `daily_command_quota` | Accepted commands |
| `daily_sign_quota` | New `SIGN` entries |
| `daily_post_quota` | New `POST`s |
| `daily_mail_quota` | `MAIL` sends |

**What a visitor sees.** The first recognized command refused over quota gets a throttled private
notice with the local-midnight reset time, and repeated refusals stay quiet. A `SIGN` over
`daily_sign_quota` gets a short fixed reply (`🌱 Daily sign limit reached ...`), as do the
cooldown and duplicate replies, so a visitor can tell "come back tomorrow" from a lost packet.
`POST` and `MAIL` each get their own throttled reset-time notice. Notices use no quota or command
cooldown and are not retried.

**Checking use.** The top-level `MENU`/`?` reply ends with `· quota N/60 today`, and `ME` (also
`WHOAMI` or `SIGNAL`) always includes a `quota N/60` line. `N` is the node's accepted-command count
so far today and `60` is the configured `daily_command_quota`.

**Visibility for operators.** Drops show in aggregate through the bounded
`meshgarden_commands{outcome="quota"}` and `{outcome="sign_quota"}` counters, and through
`rate_limited_drops_1h` (a rolling count of cooldown, quota, sign_quota and queue_full drops) in
`mgarden status` and the status file.

**Operator override.** `mgarden quota reset <node-id> [--day YYYY-MM-DD] [--commands-only |
--signs-only]` clears a node's quota early, for example after a false-positive block. The default
day is today in `[service] timezone`.

- With neither flag it deletes the whole `node_daily_quota` row; `--commands-only` or
  `--signs-only` zero just that counter.
- `mgarden quota show <node-id> [--day ...]` prints the current counts without changing anything.
- Neither command touches the in-memory `sign_cooldown_seconds` cooldown (default 1 hour; see
  [configuration.md](configuration.md)). That sender-level limit is cleared only by a daemon
  restart or the sender's own `UNSIGN`.

See [operations.md](operations.md) for the full CLI reference.

## LAB tier

`POST`, `READ` and `BOARDS` are experimental and gated behind an operator-set word
(`[service] lab_unlock_word`; the default `""` turns the tier off for everyone). `MAIL` and `REPLY`
join the gate only when `mail_public = false`; by default they are public, with receiving still
opt-in.

- **Unlocking.** A node unlocks the tier by DMing the exact word as its whole message. The
  comparison is case-insensitive, and the word is never a valid argument to anything else.
- **What is stored.** `lab_nodes` keeps only a node id and the UTC time it unlocked, never the
  word and never anything the node later posted, read or mailed.
- **The word stays secret.** It is never logged, included in a reply, or printed by any CLI or
  `doctor` output. Only the bounded `Command.LAB_UNLOCK` value appears in `processed_requests` and
  metrics, like every other command.
- **Locked nodes see nothing.** A locked node's gated commands, their `HELP` entries and
  `MENU LAB`/`MENU BOARDS` behave exactly like an unrecognized command: no reply, no
  `processed_requests` row, no quota used. A locked node cannot tell the tier exists.

## LAN exposure

The optional local dashboard (`[web]`) listens on loopback only by default. It refuses to start on
a non-loopback address unless `allow_lan = true` is set, which then logs a standing warning on
every daemon start. It has no authentication of any kind. See [dashboard.md](dashboard.md) for the
full security model and the SSH port-forward alternative to opening it on the LAN.

`SUN` and `MOON` perform no network work.
