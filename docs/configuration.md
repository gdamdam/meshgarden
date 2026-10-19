# Configuration reference

Every Meshgarden setting, by `config.toml` section, with its default. For a documented starting
point see [`../config.example.toml`](../config.example.toml); it is rejected unmodified, so replace
its placeholders first. Unknown keys and out-of-range values raise a config error at load time.

On a Raspberry Pi install the config is `/etc/meshgarden/config.toml`, and the `mgarden` wrapper
passes it for you. Otherwise the file comes from `--config`, then `$MESHTASTIC_OBSERVATORY_CONFIG`,
then `/etc/meshtastic-observatory/config.toml`.

**Upgrading.** `git pull && sudo ./scripts/install-rpi.sh` never rewrites your `config.toml`. It
runs `mgarden config-sync`, which appends keys added by a newer version (with defaults and
comments) and leaves every existing value alone. Use `--no-config-sync` to skip it; see the CLI
table in [operations.md](operations.md).

## `[radio]`

Keys starting with `meshcore_` are ignored when `transport = "meshtastic"`.

| Key | Default | Meaning |
|---|---|---|
| `device` | required | Serial device path (prefer `/dev/serial/by-id/...`); also the MeshCore serial port |
| `expected_node_id` | required | `!xxxxxxxx`, verified against the radio at every (re)connect |
| `transport` | `"meshtastic"` | `"meshtastic"` or `"meshcore"` |
| `meshcore_channel` | `"meshgarden"` | MeshCore channel watched for knocks (and public commands in channel mode); `""` disables channel interaction |
| `meshcore_knock_cooldown_seconds` | `60` | Min seconds between knock invite DMs to the same resolved node |
| `meshcore_max_pending_knocks` | `1` | Max knock invite DMs queued or in flight at once, system-wide |
| `meshcore_autoadd_overwrite` | `false` | Write the radio's `autoadd_config` firmware bit on every connection |
| `meshcore_max_text_bytes` | unset | Override the MeshCore reply byte budget (built-in default 120) |
| `meshcore_backoff_base_seconds` | `1.0` | Delay before the first reconnect retry |
| `meshcore_backoff_max_seconds` | `300.0` | Ceiling for the reconnect backoff |
| `meshcore_max_reconnect_attempts` | `3` | Retries by the MeshCore library before the connection is rebuilt from scratch |
| `meshcore_min_stable_session_seconds` | `30.0` | A session shorter than this does not reset the backoff |
| `meshcore_advert_interval_seconds` | `14400` | How often to repeat the flood self-advertisement; `0` disables only the repeat |
| `meshcore_contact_prune_enabled` | `false` | Trim the radio's contact table once per local day |
| `meshcore_contact_target` | `300` | Contact count to trim down to (1-10000) |
| `meshcore_contact_prune_hour` | `4` | Local hour (0-23, in `[service] timezone`) at or after which the trim runs |
| `meshcore_contact_prune_max_per_day` | `50` | Max contacts removed per daily run (1-1000) |
| `meshcore_repeater_public_key` | `""` | Full 64-hex-character key of the one repeater `REPEATER` queries; `""` disables the RF query |
| `meshcore_repeater_guest_password` | `""` | Guest password for that repeater (secret) |
| `meshcore_repeater_cache_seconds` | `300` | How long a successful repeater status is served without a new RF query |
| `meshcore_repeater_query_timeout_seconds` | `15` | Timeout per login/status stage |

Notes:

- **Knocks.** A channel sender's claimed name is unauthenticated, so the cooldown bounds the cost
  of a forged one. `meshcore_max_pending_knocks` bounds how much of the shared reply queue forged
  knocks can occupy, separately from `[service] max_pending_replies`. Both are ignored on a
  MeshCore install with no channel. See [MeshCore channel interaction](security-and-privacy.md#meshcore-channel-interaction).
- **Radio writes.** The adapter sets the radio's clock from host time on every connection
  (volatile, and the firmware never moves it backwards). `meshcore_autoadd_overwrite` and
  `meshcore_contact_prune_enabled` are its only persisted radio writes, both off by default.
- **Contact pruning** removes the least recently heard contacts first. It never removes a contact
  that has sent the garden a command or mail, holds a LAB unlock or mail prefs, or is a
  repeater or room. `meshcore_contact_target` must be below the radio's own maximum, or the run is
  skipped with a warning.
- **Adverts.** The advert is a real over-the-air transmission, not a reply, and one is always sent
  on every successful connection. See [Safety, security, and privacy](security-and-privacy.md).
  Meshtastic firmware beacons on its own.
- **Repeater password.** Keep it only in the owner-readable config. It is hidden from config
  representations and redacted from MeshCore debug packet logs. The whole background query is
  bounded to twice the timeout plus one second. Set the key and password together.

## `[service]`

| Key | Default | Meaning |
|---|---|---|
| `name` | `"Meshgarden Guestbook"` | Name announced in `INFO` |
| `mode` | `"dm_only"` | `"dm_only"` or `"channel"` (public read-only commands on `radio.meshcore_channel`) |
| `want_ack` | `true` | Only `true` is accepted |
| `application_retries` | `3` | Retries for an unacknowledged reply (0-3) |
| `application_retry_delay_seconds` | `15` | Fixed interval between retries |
| `max_guestbook_chars` | `100` | Max `SIGN` length, in normalized characters |
| `guestbook_entries_returned` | `3` | Entries returned by `GUESTBOOK` |
| `max_response_bytes` | `180` | Max UTF-8 bytes per outbound reply |
| `per_node_cooldown_seconds` | `30` | Min seconds between commands from one sender |
| `global_tx_cooldown_seconds` | `5` | Min seconds between any two outbound replies |
| `channel_reply_delay_seconds` | `6` | Delay before a channel reply is sent (0-30); DMs are never delayed |
| `sign_cooldown_seconds` | `3600` | Min seconds between `SIGN`s from one sender |
| `reply_ttl_seconds` | `60` | Seconds before a queued reply expires unsent |
| `max_pending_replies` | `4` | Outbound queue bound |
| `request_dedup_hours` | `24` | Replay window for `(sender, packet id)` |
| `identical_sign_dedup_hours` | `24` | Suppression window for identical `SIGN` text |
| `ack_timeout_seconds` | `300` | Seconds a reply may sit in `submitted` before it ages to `unknown` |
| `haiku_place` | `""` | Word `HAIKU` uses instead of its generic default, max 32 chars |
| `timezone` | `"UTC"` | IANA zone for local days (`ICHING`, quotas, daily limits); validated at load |
| `fortune_salt` | `""` | Optional secret mixed into `ICHING`'s daily hexagram, max 64 chars |
| `daily_display` | `"haiku"` | Daily draw served by `/api/daily.json`; `"haiku"` is the only value |
| `daily_command_quota` | `60` | Max accepted commands per node per local day |
| `daily_sign_quota` | `3` | Max new `SIGN` entries per node per local day |
| `daily_post_quota` | `5` | Max new `POST`s per node per local day |
| `daily_mail_quota` | `3` | Max `MAIL` sends per node per local day |
| `mail_notify_enabled` | `true` | Allow `MAIL NOTIFY ON`: an opted-in radio gets a short "N notes waiting" DM when mail arrives |
| `mail_notify_interval_seconds` | `1800` | Minimum time between two notices to one radio (at least 600) |
| `mail_notify_daily_cap` | `6` | Max notices per radio per local day (1-24) |
| `seen_enabled` | `true` | Answer `SEEN <node>`; `false` makes it behave like an unknown command |
| `max_topics` | `50` | Max board topics ever created, including hidden ones |
| `mail_pending_limit` | `5` | Max undelivered notes held per recipient |
| `mail_public` | `true` | `MAIL`/`INBOX`/`REPLY` work without LAB; recipients still opt in |
| `identity_linking` | `false` | `LINK`/`UNLINK` join one person's MeshCore and Meshtastic radios into one BBS account; requires `database.bbs_path` (see [security-and-privacy.md](security-and-privacy.md#linked-identities)) |
| `lab_unlock_word` | `""` | Word that unlocks the LAB tier; `""` turns the tier off for everyone |
| `mobile_dm_recovery_enabled` | `false` | Experimental DM fallback, see [below](#experimental-mobile-dm-recovery) |
| `mobile_dm_recovery_destinations` | `[]` | Nodes the fallback applies to; empty means all MeshCore recipients |
| `mobile_dm_recovery_cooldown_seconds` | `300` | Per-destination cooldown, at least 300 |
| `mobile_dm_global_recoveries_per_hour` | `6` | Hourly fallback limit (1-6) |
| `mobile_dm_recovery_min_interval_seconds` | `30` | Global spacing between fallbacks, at least 30 |
| `mobile_dm_max_retries` | `1` | `0` or `1` fallback retry |

Notes:

- **Channel mode** requires MeshCore and a non-empty channel. Mail, writes, stateful games,
  unlocks and personalized state stay DM-only.
- **Retries.** A late ACK, or a newer accepted reply to that node, cancels the remaining retries.
  The whole retry window must fit inside `ack_timeout_seconds`.
- **Quotas.** The first command over a quota gets one throttled private notice with the
  local-midnight reset time. Over `daily_sign_quota`, a `SIGN` gets a fixed reason reply and is
  not saved. Over `max_topics`, a `POST` to a brand-new topic is silently dropped; existing
  topics are never capped.
- **Mail.** A `MAIL` over `mail_pending_limit` is silently not stored; the sender still gets the
  same neutral acknowledgement.
- **LAB word.** It must be 6-24 letters or digits after stripping whitespace, is compared
  case-insensitively, and is never logged, echoed, or printed by `mgarden lab`, `doctor` or any
  other CLI output. It unlocks `POST`/`READ`/`BOARDS`, plus `MAIL`/`REPLY` when
  `mail_public = false`.

## `[database]`

| Key | Default | Meaning |
|---|---|---|
| `path` | required | SQLite file path |
| `bbs_path` | `""` | Shared BBS SQLite file attached as `bbs`; BBS tables move there on first start. `""` keeps everything in `path` |
| `peer_path` | `""` | The other mesh's `observatory.db` for the peer view; `""` means no peer |

## `[retention]`

| Key | Default | Meaning |
|---|---|---|
| `observations_days` | `7` | Raw observation retention |
| `aggregates_days` | `90` | Daily aggregate retention |
| `node_daily_days` | `365` | Per-node-per-day rollup retention |
| `links_days` | `30` | Days after a link's `last_seen` before it is pruned |
| `mail_expiry_days` | `7` | Days an undelivered `MAIL` note is kept (a delivered note is deleted at once) |
| `guestbook_expiry_days` | `0` | Only `0` (no automatic expiry) is supported; other values are rejected |
| `max_managed_storage_mib` | `1024` | Cap on database, backups and logs combined |

## `[backup]`

| Key | Default | Meaning |
|---|---|---|
| `directory` | required | Backup destination directory |
| `interval_hours` | `24` | Min hours between backups |
| `keep` | `7` | Backup files retained |

## `[metrics]`

| Key | Default | Meaning |
|---|---|---|
| `listen` | `"127.0.0.1"` | Bind address for `/metrics` |
| `port` | `9108` | Bind port for `/metrics` |

## `[moderation]`

| Key | Default | Meaning |
|---|---|---|
| `banned_words_file` | `""` | One-term-per-line banned-word list for `SIGN`; `""` disables the guard, `"default"` uses the packaged list |

A path must exist and be readable at load time.

## `[observatory]`

| Key | Default | Meaning |
|---|---|---|
| `latitude` | `0.0` | Observatory latitude, -90 to 90 |
| `longitude` | `0.0` | Observatory longitude, -180 to 180 |
| `enabled_position_capture` | `true` | Store `POSITION_APP` fields heard on the mesh |
| `dm_position_capture` | `false` | Record positions a visitor shares by DM (experimental, operator-only) |
| `max_direct_km` | `400.0` | Distance-plausibility bound for direct (0-hop) receptions, must be > 0 |
| `max_km_per_hop` | `150.0` | Bound added per extra hop, must be > 0 |
| `max_observed_km` | `1000.0` | Absolute ceiling regardless of hops, must be > 0 |

Notes:

- **Location.** `(0.0, 0.0)` means unset, and `GARDEN`/`RECORD` then omit their distance fields.
- **Position capture.** Nodes always advertise their positions voluntarily; this key only decides
  whether Meshgarden keeps them.
- **DM positions.** With `dm_position_capture` on, a DM of the form `Saved position: lat, lon` or
  `GPS position: lat, lon` (the MeshCore app's location share) is recorded in `dm_positions` and
  acknowledged with one DM. Off, it is silent and unstored like any unknown text. Only the parsed
  coordinates are kept, never the text. Browse with `mgarden positions`, remove with
  `mgarden positions delete`. Rows stay until deleted.

## `[web]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Serve the local read-only dashboard (see [dashboard.md](dashboard.md)) |
| `listen` | `"127.0.0.1"` | Bind address |
| `port` | `9109` | Bind port |
| `allow_lan` | `false` | Permit a non-loopback `listen` (logs a warning) |

A non-loopback `listen` is refused at startup unless `allow_lan = true`. There is no
authentication either way.

## `[weather]`

See [Weather alerts](#weather-alerts) below for behavior and the full key table.

## `[health]`

Operator health digest; see [Health digest](operations.md#health-digest). Disabled by default.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Write one report per local day from the daemon. |
| `hour` | `7` | Local hour (0-23, `[service] timezone`) at or after which it runs. |
| `history_days` | `14` | Reports kept in `<database dir>/health/` (1-365). |
| `email_enabled` | `false` | Email each scheduled report. Needs every SMTP key below. |
| `smtp_host`, `smtp_port` | `""`, `587` | SMTP server. |
| `smtp_starttls` | `true` | Use STARTTLS before login. |
| `smtp_username` | `""` | Login name. |
| `smtp_password_file` | `""` | Path to a file holding the password (never inline). |
| `email_from`, `email_to` | `""`, `[]` | Sender and recipient list. |

## `[[commands.info]]` and `[find]`

Operator information commands. Each `[[commands.info]]` entry (at most 16) adds a command that replies
with fixed text:

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | required | 1-12 ASCII letters/digits (`-`, `_` allowed). Must not be a built-in or reserved word, the `lab_unlock_word`, or used twice. |
| `aliases` | `[]` | Extra words, same rules. |
| `help` | `""` | One line shown in `MENU MORE` and `HELP <name>` (max 180 UTF-8 bytes). |
| `response` | required | Literal reply text, max 180 UTF-8 bytes. No templating, files or shell. |
| `scope` | `"dm"` | `"channel"` makes the command eligible on the MeshCore public channel; it still answers there only when `service.mode = "channel"`. |
| `enabled` | `true` | `false` removes it from parsing, `MENU MORE` and `HELP`. |

A MeshCore radio's text budget may be lower than 180 bytes (120 by default). Replies are truncated to the
active transport budget; if `radio.meshcore_max_text_bytes` is set, responses longer than it are rejected
at load. Replies count against the normal daily command quota. Logged as `CUSTOM`.

`[find] enabled = false` (default) controls `FIND <text>`: it searches visible guestbook notes (and LAB
boards for an unlocked sender in a DM; boards are never searched on the public channel), returns at
most 3 matches from the latest 2000 entries, and never reads mail, aliases, positions or games.

## `[forecast]`

Off by default. `FORECAST` replies with a short forecast read from an in-memory cache; a background
thread refreshes it, so a command never waits on the network. The one provider is the U.S. National
Weather Service (`api.weather.gov`: `/points/{lat},{lon}`, then the forecast URL it names).

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | `true` starts the fetcher thread and registers `FORECAST`. |
| `latitude`, `longitude` | observatory | Required when enabled; falls back to `[observatory]` if non-zero. |
| `units` | `"us"` | `"us"` (F) or `"si"` (C). |
| `refresh_interval_seconds` | `3600` | Minimum 600. |
| `timeout_seconds` | `10` | Per request. |
| `max_retries` | `2` | Extra attempts per refresh, with backoff. |
| `stale_after_seconds` | 3 x refresh | Older data is labelled `STALE, 5h old`. |
| `contact` | `""` | Goes in the `User-Agent`; required when enabled. |

Reply: `🌤 Forecast (NWS, 25m ago)` then the next two or three periods, trimmed to the radio budget.
Before the first fetch it says `Forecast not available yet.`; a failing provider leaves the last
data in place, which turns `STALE`. `FORECAST` also works on the public channel (public info).
`WX` is unchanged. There is no forecast worker heartbeat: `metrics.WORKERS` is a fixed label set.

## `[feeds]`

Off by default. Imports RSS 2.0 / Atom items into NEWS as plain text (`host: title`, at most 84
bytes), read on demand with `NEWS`; imports are never broadcast.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Starts the poller thread. |
| `urls` | `[]` | `https://` feed URLs only; required when enabled. |
| `poll_interval_seconds` | `3600` | Minimum 900. |
| `max_items_per_feed` | `5` | 1 to 20, newest-listed first. |
| `max_response_bytes` | `512000` | Larger responses are discarded. |
| `item_expiry_days` | `7` | Imported items expire this long after publication. |
| `timeout_seconds` | `10` | Per request. |
| `max_redirects` | `3` | https to https only; a redirect to `http://` is refused. |
| `max_stored_items` | `50` | Oldest imported rows are pruned beyond this. |
| `contact` | `""` | Goes in the `User-Agent`; required when enabled. |

Documents with a `<!DOCTYPE` or `<!ENTITY` are rejected (entity-expansion defence). Items are
de-duplicated on a hash of feed URL plus guid/link/title, so restarts do not re-import. Undated
items take fetch time, future-dated items are clamped to fetch time, items without a title are
skipped. Bookkeeping lives in the main database table `news_imports`; only rows listed there are
ever pruned, and manual NEWS (`mgarden news add`) is never touched. With `database.bbs_path` set,
enable feeds on one instance only, or each instance imports its own copy.

## `[federation]`

Off by default; shares selected public boards with trusted peer gardens. Keys, peers and the wire
protocol are in [federation.md](federation.md).

## Metrics
## Metrics

`GET http://<metrics.listen>:<metrics.port>/metrics` serves Prometheus text format. It is
loopback-only by default (`127.0.0.1:9108`); a non-loopback `metrics.listen` is allowed but logs
a warning at config load.

| Metric | Type | Meaning |
|---|---|---|
| `meshgarden_radio_connected` | Gauge | 1 if the radio interface is connected |
| `meshgarden_reply_ready` | Gauge | 1 if identity is verified and replies may be sent |
| `meshgarden_radio_connected_seconds` | Gauge | Seconds since the radio last connected |
| `meshgarden_observed_distinct_senders_1h` / `_24h` | Gauge | Distinct sender nodes observed |
| `meshgarden_guestbook_visible_entries` | Gauge | Currently visible guestbook entries |
| `meshgarden_reply_queue_depth` | Gauge | Replies pending in the outbound queue |
| `meshgarden_storage_used_bytes` / `_cap_bytes` | Gauge | Managed storage used vs. the configured cap |
| `meshgarden_storage_pressure` | Gauge | 0 = ok, 1 = warn, 2 = critical |
| `meshgarden_backup_last_success_timestamp_seconds` | Gauge | Unix time of the last successful backup |
| `meshgarden_backup_healthy` | Gauge | 1 if the last backup attempt succeeded (or none yet) |
| `meshgarden_service_uptime_seconds` | Gauge | Seconds since the process started |
| `meshgarden_rate_limited_drops_1h` | Gauge | Commands silently dropped in the last hour (cooldown, quota, sign_quota, queue_full) |
| `meshgarden_worker_heartbeat_age_seconds` | Gauge | Seconds since each worker (`db`, `tx`, `radio`, `maintenance`) last updated its heartbeat |
| `meshgarden_raw_receive_events` / `meshgarden_unique_packets` | Counter | By bounded `portnum` and `channel` labels |
| `meshgarden_commands` | Counter | By bounded `command` and `outcome` labels |
| `meshgarden_replies` | Counter | By bounded `outcome` label |
| `meshgarden_guestbook_entries_created` | Counter | Guestbook entries created |
| `meshgarden_guestbook_entries_moderated` | Counter | Entries auto-hidden by the banned-word guard |
| `meshgarden_mail_notes_stored` | Counter | `MAIL` notes stored (recipient opted in, had room) |
| `meshgarden_mail_notes_refused` | Counter | `MAIL` notes silently not stored (opted out or full) |
| `meshgarden_reconnects` / `_database_errors` / `_malformed_packets` / `_ingest_dropped` | Counter | Reliability counters |
| `meshgarden_rx_snr_db` / `meshgarden_rx_rssi_dbm` | Histogram | Reception quality, only when reported |
| `meshgarden_mobile_dm_recovery_total` | Counter | By bounded `stage` label, see [below](#experimental-mobile-dm-recovery) |

No label is ever a node ID, node name, guestbook text or free-form error string. Every label comes
from a small fixed allowlist.

**Label values.** `command` includes `SEEN`, `WHO`, `ME`, `PING`, `POST`, `READ`, `BOARDS`,
`MAIL`, `LAB_UNLOCK` and `CMD_HELP`. For `meshgarden_commands`, `outcome` includes `quota`,
`sign_quota`, `post_quota` and `mail_quota`. For `meshgarden_replies`, `submitted` counts
transmission attempts including retries; `acked`, `failed`, `expired` and `unknown` are logical
request outcomes.

### Worker health

The daemon has four workers: the `db` and `tx` threads, the `radio` reconnect thread, and the
periodic `maintenance` tick. Each updates a heartbeat on every loop pass and counts as `stalled`
once the heartbeat is too old:

| Worker | Stalled after |
|---|---|
| `db`, `tx` | 30 s |
| `radio` | the larger of the reconnect backoff cap and 60 s |
| `maintenance` | 180 s (3x its interval) |

`mgarden status` prints one line per worker (`worker <name>: age=<seconds>s alive=<bool>
stalled=<bool>`). The same fields appear under `workers` in `--json` output and the status file,
and `mgarden doctor`'s `worker_heartbeats` check fails if any worker is currently stalled. Only a
stall transition is logged (WARNING on stall, INFO on recovery), never a steady state.

## Distance records and MeshCore maps

`observatory.max_direct_km` is an adjustable plausibility filter, not a physical range guarantee.
The 400 km default leaves margin above Meshtastic's documented
[331 km ground-to-ground LoRa link](https://meshtastic.org/docs/overview/range-tests/). Existing
configs keep their explicit value until edited. Relayed records use `max_km_per_hop` (150 km) and
`max_observed_km` (1,000 km). Unknown hop counts never qualify as RF records.

**MeshCore evidence.**

- A received flood message with `path_len = 0` is zero-relay reception evidence. The
  [companion firmware](https://github.com/meshcore-dev/MeshCore/blob/main/examples/companion_radio/MyMesh.cpp)
  reports the flood path length and uses `0xFF` for direct routing.
- Direct *routing* (`path_len = 255`) does not prove that no repeater was involved, and bare
  advert notifications have no path count.
- Channel display names cannot establish sender identity.
- A direct distance also needs a valid advertised node position; `(0, 0)` counts as missing.

**Topology feed.** It keeps its direct and two-hop `nodes` and adds `other_nodes` for recently
heard nodes with longer or unknown routes. Imported contacts alone do not count as heard.
Lilycast's map labels these routes separately and shows geographic distances. Its Records screen
uses only qualifying RF distances, falling back to best signal when none qualify.

**Stored route.** Each topology node has `stored_route`: null if unavailable, otherwise
`{"hops": 3, "hashes": ["aa", "00", "bc"], "cached_at": 1789843554}`.

- It is the MeshCore contact's saved **outgoing** path, separate from received hop evidence.
- `hops` of zero means a saved path with no repeaters; null `hops` means flood routing, with no
  saved explicit path.
- Repeater hashes keep their 1-3 byte width, and collisions mean they cannot be treated as unique
  node identities.
- `cached_at` is when Meshgarden read the snapshot, not when the path was learned or verified.
- Snapshots are imported on connection and refreshed after adverts and path-update notifications,
  using local serial contact reads. No RF discovery probes are sent.
- They never create receptions, direct links or range records.
- Lilycast shows the received classification beside the saved relay count, up to four hashes and
  the cache age; the API keeps the complete path.

## Command cooldown feedback

When the per-node command cooldown triggers, a sender gets at most one explanatory notice per 30
minutes, counted separately for DMs and the configured channel.

- DM notices stay private. In channel mode, a rejected public command gets a channel notice
  addressed to the known node name. An earlier DM notice does not suppress the channel one.
- The interval is fixed and persists across restarts.
- Notices do not change `per_node_cooldown_seconds` or use daily command quota.
- They are best-effort: they respect the normal reply queue and global transmit cooldown, and are
  not retried. The requester must resend the command after waiting.

## Experimental mobile DM recovery

Off by default. For selected MeshCore destinations it replaces the three legacy retries with one
normal DM and, only if that stays unacknowledged, at most one encrypted flooded DM retry. It needs
no telemetry permission, private channel, GPS sharing or special recipient app. The normal send
still uses native routing, which may itself flood if no route is saved. A successful normal DM
causes no fallback traffic.

Enable it for a single node in `[service]`:

```toml
mobile_dm_recovery_enabled = true
mobile_dm_recovery_destinations = ["!a1b2c3d4"]
mobile_dm_recovery_cooldown_seconds = 300
mobile_dm_global_recoveries_per_hour = 6
mobile_dm_recovery_min_interval_seconds = 30
mobile_dm_max_retries = 1
```

**Limits and behavior.**

- An empty destination list applies the policy to all MeshCore recipients. Use one destination, or
  keep it disabled, during early field testing.
- `mobile_dm_max_retries` accepts only 0 or 1; 0 means one normal send with no retries.
- Cooldown must be at least 300 s, global spacing at least 30 s, and the hourly limit 1-6. The
  counters live in the running process and reset on restart.
- The fallback waits at least the application retry interval and respects the radio's suggested
  ACK timeout. It also obeys the global transmit cooldown.
- A late ACK or a newer accepted reply cancels a pending fallback. There are no periodic probes.

**Routing.** Resetting the contact path is local; the next DM is the actual RF flood. A received
path-and-ACK response lets the firmware learn a new route. Meshgarden does not infer a route from
an advert or hop count, keeps no mobile route cache, and does not claim a stored route is fresh.
No discovery or telemetry requests are sent.

**Why the limits matter.** Many repeaters can repeat a flood, even though Meshgarden sends only one
fallback packet. The limits bound fallbacks Meshgarden starts, not every firmware transmission or
normal DM the firmware chooses to flood. Lost ACKs can produce duplicate messages; keeping the
message timestamp helps clients deduplicate but cannot guarantee exactly-once display on
unmodified clients.

**Field test.**

1. Enable one node and restart Meshgarden.
2. Send one test command from the walking location and wait out the full ACK and fallback window
   before sending another.
3. Compare normal ACKs, fallback sends and fallback ACKs in logs and metrics.

Unit tests simulate the radio, so verify RF delivery on real hardware. Disable
`mobile_dm_recovery_enabled` to restore the legacy retry policy.

`meshgarden_mobile_dm_recovery_total{stage="..."}` has bounded stage labels for normal
success/timeout, fallback sent/success/timeout/failure, and suppression reasons. Debug logs show
known node names beside IDs. A timeout means no ACK was seen in that window; a later ACK can still
settle the original request.

## Weather alerts

Off by default. With `[weather] enabled = true`, the daemon polls the U.S. National Weather
Service active-alerts API for the configured counties and broadcasts Warnings and Watches, one
line each, on a second MeshCore channel. It is a supplemental tool, not an emergency service, so
keep an official warning source.

It requires `radio.transport = "meshcore"`. The channel must already exist on the radio (add it
in the companion app; a leading `#` makes it a public hashtag channel anyone can join by typing
the name).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Start the poller thread and resolve the second channel |
| `channel` | `"#wx-bay"` | Channel name, matched exactly against the radio; must differ from `radio.meshcore_channel` |
| `contact` | `""` | Email or URL for the `User-Agent` NWS requires; required when enabled |
| `counties` | nine Bay Area `CAC...` codes | NWS county UGC codes to query |
| `event_suffixes` | `["Warning", "Watch"]` | Keep an alert only if its event name ends with one of these |
| `min_severity` | `"Moderate"` | `Minor`, `Moderate`, `Severe` or `Extreme`; `Unknown` always passes |
| `poll_interval_seconds` | `120` | Minimum 30; failures back off exponentially up to 15 minutes |
| `max_sends_per_hour` | `6` | Hard cap on lines per rolling hour; the rest wait for the next poll |
| `heartbeat_hour` | `-1` | `-1` is off; local hour 0-23 in `service.timezone` for a daily `🌦 wx-bay daily` line |
| `[weather.zone_aliases]` | empty | `CAZ...` zone to short name overrides for the line |

Lines look like `⚠ Red Flag Warning · North Bay Mtns, East Bay Hills · until Sat 6PM`. A `↻` marks
an update that changed the event, areas or expiry, and `✓` an explicit cancellation. Expiry is
silent.

At most two lines are queued per poll so a visitor's DM is never starved. Seen alerts live in the
`weather_alerts` table and are pruned seven days after expiry, so a restart rebroadcasts nothing.

`WX`, by DM or on the public channel, replies with a five-line feed status: `🌦 wx-bay`,
`9 counties`, `NWS ok 2m ago`, `0 active`, `last sent Oct 10 11:40`.
