# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project's version numbers follow [PEP 440](https://peps.python.org/pep-0440/)
(pre-release/candidate identifiers such as `rc1`), tagged in git under the
`vX.Y.Z-pre-NNN`-style operator name for the corresponding PEP 440 version.

## [Unreleased]

## [1.15.0] - 2026-10-30

### Changed

- Dashboard reorganized. Every page now reads title, headline numbers, then detail sections, with
  long tables folded until clicked. `?mesh=both` shows one title and a Meshtastic | MeshCore table
  of the headline numbers instead of two full pages stacked (which also repeated the page title
  under each mesh name). Tabs are grouped into Overview, Community and Network. `/records` is one
  table by period instead of 21 cards; `/propagation` is one row per node instead of a table per
  node. Ages and uptime read as `1h 22m` instead of raw seconds. URLs and `/api/records.json` are
  unchanged.

## [1.14.0] - 2026-10-30

### Added

- Visitor count: how many messages reach the garden from anyone other than the operator.
  LAB-unlocked nodes are left out, so your own testing does not hide whether strangers use it.
  Shown on `/traffic` (messages, senders, senders in 7 days, valid commands, guestbook signs, last
  visitor) and as a `visitors` block in `mgarden usefulness`. `mgarden usefulness --reset` restarts
  the count from now without deleting any history.

## [1.13.3] - 2026-10-19

### Added

- Dashboard: every table sorts by column. Click a header (or Enter/Space on it) to sort, again to
  reverse; numbers sort numerically, empty and `n/a` cells last. One small inline script, allowed by
  the `Content-Security-Policy` only through its SHA-256 hash; no external files.

## [1.13.2] - 2026-10-19

### Fixed

- Dashboard `/map` and node pages flagged every MeshCore node "⚠ implausible", even a neighbour
  0 km away: MeshCore traffic rarely carries a hop count, and an unknown hop count was treated as
  implausible. On display an unknown hop count now only applies the absolute `max_observed_km`
  ceiling; a known one keeps the per-hop check. Such nodes still never claim "furthest" records.

## [1.13.1] - 2026-10-19

### Fixed

- Dashboard navigation: the page tabs no longer run together ("Right nowHealthTraffic…"). Each is a
  padded, spaced tab, the current one is highlighted and marked `aria-current="page"`, and the row
  wraps on narrow screens.

## [1.13.0] - 2026-10-19

### Added

- `MAIL NOTIFY ON|OFF`: a radio that opts in gets one short DM, `📬 N notes waiting`, when new mail
  arrives for it (count only, never sender or text), at most every 30 minutes and 6 times a local
  day. Works across meshes: a note sent from Meshtastic notifies the recipient's MeshCore radio.
  Operator settings `[service] mail_notify_enabled` (default on), `mail_notify_interval_seconds`,
  `mail_notify_daily_cap`. Migration 24 adds the per-instance `mail_notify` table.

## [1.12.4] - 2026-10-19

### Changed

- The top-level `MENU` lists its four sub-pages on one line (`MENU SOCIAL|STATS|GAMES|DAILY`);
  each is still typed as `MENU <page>`. With the space saved, MeshCore's 120-byte menu also names
  the shared mesh, in the short form `🌱 MeshGarden ⇄ Meshtastic` (Meshtastic keeps
  `· shared with MeshCore`). The quota hint stays on both.

## [1.12.3] - 2026-10-19

### Changed

- With a shared BBS, the top-level `MENU` title names the other mesh: `🌱 MeshGarden · shared with
  MeshCore` on Meshtastic and `· shared with Meshtastic` on MeshCore. It is added only when it fits
  the transport budget with the quota hint, so no menu line is cut; on MeshCore's default 120 bytes
  the title stays `🌱 MeshGarden`.

## [1.12.2] - 2026-10-19

### Changed

- `mgarden radio-watchdog` polls every instance configured beside the selected config (e.g. both
  MeshCore and Meshtastic), each with its own state file, and names the mesh in its alerts. The
  host-reboot page is sent once.
- Without `MESHGARDEN_WATCHDOG_HUB`, the watchdog's recovery step is now a soft USB reset of the
  radio's own device (sysfs `authorized` 0 then 1) instead of a uhubctl power cycle on the default
  hub `1-1` port `4`, which could name the wrong port. It recovers ESP32-S3 radios whose USB serial
  link wedges after a firmware reboot. A configured `MESHGARDEN_WATCHDOG_HUB`/`_HUB_PORT` is
  power-cycled only for the radio actually on that port; another instance's radio gets the soft
  reset, never a cycle of the wrong port.

## [1.12.1] - 2026-10-19

### Changed

- `mgarden status` now reports every instance configured beside the selected config (e.g. both
  `/etc/meshgarden/config.toml` and `mt.toml`), each under a `== MeshCore (...)` / `== Meshtastic (...)`
  header. `--meshcore` / `--meshtastic` show one instance. `--json` still prints a single object (the
  selected instance, or the one the flag names), so scripts parsing it are unaffected. A single-instance
  install prints exactly as before.
- `mgarden status` (and `status.json`) include `node_name`, the garden radio's own name as the
  radio reports it (MeshCore self-info, Meshtastic local long name). A label for the operator only.

## [1.12.0] - 2026-10-19

### Added

- `LINK` / `UNLINK` (opt-in `[service] identity_linking = true`, needs `database.bbs_path`): a person
  joins their MeshCore and Meshtastic radios into one BBS account with an expiring, single-use code
  sent from one radio and entered on the other. Mail, reply routes, alias, prefs, NEW/board cursors,
  LAB and games then follow the account; blocks on either radio block both; quotas stay per radio.
  `mgarden identity status|unlink` for operators. Migration 22 / bbs migration 4 add the tables.
- Optional command modules (`meshgarden/modules.py`): one small interface for bundled commands, with
  routing, limits and sending kept central; MOON and SUN moved onto it. `MENU MORE` lists enabled
  optional commands.

- `BOARDS NEW` and `READ <board> NEW` (LAB): per-reader, per-board unread tracking. A read advances the cursor
  only once its reply is ACKed (lost ACKs re-deliver); hidden posts are skipped. Migration 21 adds
  `board_cursors` / `board_cursor_pending`; shared BBS files get them via bbs migration 3.
- `FORECAST` command and `[forecast]` (off by default): a background thread caches the NWS forecast; the
  command reads only the cache and labels old data `STALE`.
- `[federation]` (off by default): pull-based federation of selected public boards between trusted
  gardens over HMAC-authenticated HTTP; imported posts show as `@origin/tag`, local hide always wins,
  `mgarden federation status|hide|unhide`. Migration 23. See docs/federation.md.
- `[feeds]` (off by default): RSS/Atom items from https feeds imported into NEWS as plain text, deduplicated,
  expiring, size- and redirect-bounded; DOCTYPE/ENTITY documents rejected. Migration 20 adds `news_imports`.
- `mgarden setup`: interactive and `--non-interactive` config wizard (serial candidates from the
  filesystem, optional second instance with shared `bbs_path`, collision/alias detection, previewed
  and validated TOML, backup of a replaced file, atomic `0600` write).
- `mgarden health-report` and `[health]`: aggregate-only operator health digest with deltas, daily
  local-day file with bounded history, and optional SMTP email (off by default).
- Operator information commands: `[[commands.info]]` defines fixed-text commands (name, aliases, help,
  response, `scope = "dm"|"channel"`, `enabled`), validated against built-in words, the unlock word and
  the byte budget, and listed under `MENU MORE` / `HELP <word>`.
- `FIND <query>` (opt-in via `[find] enabled = true`): at most 3 matches from visible guestbook notes,
  plus LAB boards for unlocked senders (never on the public channel). Mail, aliases, positions and
  games are never searched.
- `mgarden upgrade-check`: rehearses an upgrade on snapshots of the main and shared
  BBS databases (live files opened read-only, never modified), runs integrity,
  foreign-key and application checks, reports reply routes whose mail id now
  belongs to another recipient, and verifies the newest backup. The 1.11.x
  shared-BBS repairs are now versioned `bbs.user_version` migrations (v2); files
  already repaired by 1.11.2 are recognised and only bumped.

## [1.11.2] - 2026-10-19

### Fixed

- One-time repair for a `bbs.db` already split by 1.11.0, which 1.11.1's
  split-time fixes never reached: id sequences are lifted above every mail id
  still named by a reply route and every NEW cursor, a reply route hijacked by
  a reused mail id (same reader) is repointed at the note they now hold, and
  the MeshCore instance re-keys the game rows it brought to its `mc:` ids,
  merging a player's duplicate same-day WORDLE/BOOK rows and taking the
  double count back out of the day's totals. Recorded in `bbs_meta`, so it
  runs once.
- `install-rpi.sh --reset-data` now checks the state directory itself (a
  shared `bbs.db` or another instance's `<name>/observatory.db`) and the
  installed `/etc/meshgarden` configs, so a custom `--config` path can no
  longer bypass the guard and wipe a peer instance's state and backups.

## [1.11.1] - 2026-10-19

### Fixed

- Shared-BBS split no longer restarts an empty table's id sequence: with every
  note collected, new mail reused old ids and `REPLY #1` could reach the
  previous sender through a surviving reply route.
- Retention no longer prunes MeshCore guestbook authors from `nodes` after the
  split (guestbook ids are now `mc:`-qualified); their names and name-based
  mail recipients survive.
- The split re-keys a MeshCore instance's same-day WORDLE/BOOK state to the
  qualified id, so a player can't solve twice and inflate the day's totals.
- `install-rpi.sh --reset-data` refuses while another instance config exists:
  it would have wiped that instance's state, its backups and the shared
  `bbs.db` under its running daemon.
- A failed restore removes its temporary copy's `-wal`/`-shm` sidecars.

## [1.11.0] - 2026-10-19

### Added

- Shared-garden bridge: a Meshtastic daemon and a MeshCore daemon share
  guestbook, boards, news, mail, games, LAB and blocks through an attached
  `bbs.db` (opt-in `database.bbs_path`). Each daemon keeps its own
  observatory. `meshgarden@<instance>` systemd template and
  `install-rpi.sh --instance` run a second daemon on one host.
- Cross-mesh MAIL: an explicit `mc:`/`mt:` prefix, or a name/id resolved on
  the other mesh.
- `WHO`/`GARDEN`/`RECORD`/`SEEN` accept a trailing `MC`/`MT` to read the
  other mesh's observatory (`database.peer_path`).
- One dashboard for both meshes: `?mesh=mt|mc|both`.
- `INFO` adds a "Shared with <other mesh>" line when `bbs_path` is set.

### Changed

- `backup`, `restore` and `doctor` cover `bbs.db`.

### Upgrade

- Sharing is opt-in via `database.bbs_path`. Without it nothing changes.
- Setting it moves the BBS tables into `bbs.db` on next start
  (automatic, with verified row counts).
- MeshCore ids in moved rows become `mc:!...`.
- A MeshCore player's same-day wordle/book state resets once.
- Rolling back to 1.10 requires restoring the pre-upgrade backup. Stop both
  daemons before restoring `bbs.db`.
- Developer note: future schema changes to BBS tables need a bbs-side
  migration.
- This is a minor bump by operator decision despite the storage layout
  change.

### Also in this release

- Add `mgarden positions delete` to remove shared DM positions by id
  (`delete 12 15`), id range (`--range 10-20`), node (`--node`) or local
  date range (`--from`/`--to`, inclusive). Filters combine with AND and at
  least one is required. `mgarden positions` now prints each row's `#id`.
  Shared positions remain experimental, operator-only and kept until
  deleted.

- Remove the `FNORD` command and the Discordian quotation collection
  (`QUOTES`, `quote_for`, `quote_text` in `meshgarden/fortune.py`, the
  `/api/quote.json` feed). `FNORD`/`QUOTE` are now unknown text and stay
  silent; `MENU LAB` and the channel menu no longer list it. `[service]
  daily_display` is still accepted but `"haiku"` is its only value, and
  `/api/daily.json` always serves the haiku. `fortune_salt` now affects
  `ICHING` only. The feature lives on in the `fnord` branch, tagged
  `fnord-last`.

- Remove the `ISS` command and the satellite tracker (`meshgarden/satellites.py`,
  the `skyfield` dependency, the CelesTrak fetch). `ISS`/`!iss` is now
  unknown text and stays silent; `MENU DAILY` and the channel menu no
  longer list it. MOON and SUN are unchanged.

- Add opt-in capture of DM location shares: with `[observatory]
  dm_position_capture = true`, a DM of `Saved position: lat, lon` or
  `GPS position: lat, lon` (the MeshCore app's share-location text) is
  recorded in the new `dm_positions` table (schema 19; coordinates only,
  never the text) and acknowledged with one DM. New `mgarden positions
  [--node] [--days] [--limit] [--json]` lists them. Off by default, such
  a DM stays silent and unstored.

## [1.10.0] - 2026-10-10

- Add opt-in daily MeshCore contact-table housekeeping
  (`[radio] meshcore_contact_prune_enabled`, default off; with
  `meshcore_contact_target`, `meshcore_contact_prune_hour`,
  `meshcore_contact_prune_max_per_day`). A radio at its contact cap refuses
  to auto-add new nodes, so the garden cannot read a newcomer's DMs. Once
  per local day the daemon removes least-recently-heard contacts down to the
  target, never touching a node that has sent a command or mail, holds a LAB
  unlock or mail prefs, or is a repeater/room. This is the adapter's second
  persisted radio write. Adds `meshgarden_contacts_pruned_total`,
  `meshgarden_radio_contacts`, `meshgarden_radio_contacts_max`, a `contacts`
  entry in `mgarden status`, and schema migration 18 (`daemon_state`).

## [1.9.2] - 2026-10-10

- Hold MeshCore channel replies for `[service] channel_reply_delay_seconds`
  (default 6, range 0-30; 3 was still lost under a rooftop repeater) before transmission. A reply sent in the same
  second its request arrived collided with the repeaters still re-flooding
  that request and was lost without trace (hardware finding); DMs are never
  delayed, and a DM queued behind a held channel reply goes first.
- Rejection lines in the `run --debug` exchange trace now carry the refused
  message text, and Public-channel chatter no longer appears there at all.

## [1.9.1] - 2026-10-10

- Overhaul `mgarden` help: a grouped command overview with examples at the top level, a description, examples and option help on every command, and a new `help [command]` command; running `mgarden` with no command now prints the full help (exit 2).

## [1.9.0] - 2026-10-10

- Add the `WX` command, by DM or on the public channel: a one-line status
  of the `#wx-bay` feed with the county count, NWS poll health, active
  Warnings and Watches, and the last line sent.
- Add an optional daily weather heartbeat (`[weather] heartbeat_hour`,
  default `-1` for off): one line per local day on the alert channel,
  persisted in the new `weather_state` table (schema 17). A failed send is
  not retried until the next day, and it sits outside the hourly alert cap.

## [1.8.0] - 2026-10-10

- Add `[weather]`: NWS Warnings and Watches for the nine Bay Area counties
  broadcast as one 120-byte line each on a second, public MeshCore hashtag
  channel (default `#wx-bay`). Off by default. Polls `api.weather.gov`
  every 2 minutes with ETag reuse, dedupes products across restarts in the
  new `weather_alerts` table (schema 16), sends one line on explicit
  cancellation and nothing on expiry, and caps lines per rolling hour.
  Advisories and Statements are never sent. This is the second documented
  exception to "the garden transmits nothing but a reply"; see
  `docs/architecture.md`.
- The MeshCore transport resolves a second channel index for weather sends
  only. Traffic on that channel never triggers commands.
- Add `Command.WEATHER` as the processed_requests label for those sends,
  three `meshgarden_weather_*` metrics, a `weather` worker heartbeat, and a
  `weather` block in `mgarden status`.

## [1.7.1] - 2026-09-21

- Add `mgarden news delete <id>`, which removes a notice from the database
  outright. `news hide` remains the ordinary way to retire one; nothing else
  ever purges the table, so this is the only way to get the text off disk.

- Count MeshCore receptions as RF in the furthest-direct and furthest-observed
  range records, and cap the incl.-MQTT figure at `max_observed_km`.

- Trace repeater login/status calls, timing, safe numeric results, cache use,
  cooldowns and failures in debug mode, with passwords redacted.

- Allow channel replies to wait for an in-flight repeater query and report
  channel-send exception types and numeric firmware error codes in logs.

- Keep channel cooldown feedback independent of earlier DM notices and address
  public cooldown notices to the known node name.

- Correct outdated interaction-mode and retry guidance in contributor docs.

## [1.7.0] - 2026-09-20

- Add opt-in mobile DM fallback: one normal send, then at most one encrypted
  flooded retry after an ACK timeout. Replaces legacy retries for selected
  destinations and works without telemetry permissions or special clients.
- Bound fallback with per-node cooldowns and global rate limits, cancel on
  late ACKs/new replies, and expose outcomes in logs and Prometheus.
- Preserve the original DM timestamp across fallback retries to help recipient
  applications recognize duplicates; exactly-once display is not guaranteed.

- Page once from the radio watchdog when the host has rebooted, with the
  downtime and the previous boot's shutdown/power journal lines as the cause.
- Allow `AF_NETLINK` in `meshgarden-watchdog.service` so `uhubctl` can
  initialize libusb; the USB power cycle previously always failed.

## [1.6.4] - 2026-09-20

- Add optional `service.mode = "channel"` for public, read-only MeshCore
  commands on the configured channel. Channel replies are submitted once and
  are not retried because MeshCore provides no remote ACK for broadcasts.
- Keep mail, writes, stateful games, unlocks, and personalized node state
  DM-only; the existing `dm_only` mode remains the default.
- Include a known node name beside its stable ID in debug receive, rejection,
  reply, retry, and outcome lines.
- Make MeshCore retries carry their attempt number and reset a stale cached
  contact path before retry 2, so retries 2 and 3 flood. Debug traces now show
  path fallback and why a retry series was cancelled.

## [1.6.3] - 2026-09-20

- Add `quota --reset-all`, list every stored node/day and all four counters
  with `quota show`, and accept unique known node names for quota targets.

- Fit ICHING and HAIKU to the transport byte budget without clipping their
  content; paginate long FNORD quotations with explicit FNORD page requests.
- Match MOON emoji to its phase; add readable SUN and ISS layouts.
- Simplify menus, retire QUOTE in favor of FNORD, and remove METEOR commands.

- Explain a recognized DM command's cooldown at most once per sender every
  30 minutes, persisted by schema migration 15. Notices do not restart cooldowns,
  consume command quota, retry, or cancel earlier legitimate reply retries.
- Send one persisted, throttled private notice when a node reaches its daily
  command, `POST`, or `MAIL` quota, including the configured local-midnight
  reset time. Notices are scoped by quota class, consume no quota or cooldown,
  are not retried, and get a fresh throttle slot on the next local day.

- Hide GARDEN's direct count when no zero-hop reception is confirmed; positive
  counts say "confirmed direct" rather than treating missing paths as proof.

- Align help, status, community, astronomy, and radio-check reply layouts with
  concise headings and readable lines while preserving transport byte limits.

- Use full known node names in radio replies whenever the complete reply fits;
  apply name shortening only within the individual formatter byte budget.

- Retry unacknowledged replies three times at fixed 15-second intervals.

- Lay out REPEATER telemetry with emoji-led lines, units, uptime, and explicit
  cache age/stale status; drop optional signal details as a whole to fit RF replies.

## [1.6.2] - 2026-09-20

- Add a cached, request-driven REPEATER status command with private guest
  credentials, bounded queries, and serialized MeshCore command handling.
- Keep known node names alongside IDs in compact radio reports, partial mail,
  dashboards, and operator output; preserve names in ME's 120-byte replies.

- Keep the known best-SNR node name in MeshCore's 120-byte RECORD reply when
  the independent hop record has no attributable sender; shorten that marker
  to the conventional abbreviation `unk.`.

## [1.6.1] - 2026-09-20

- Add stable, self-service `@aliases` for MAIL, with one alias per node,
  case-insensitive uniqueness, a 24-hour change limit, neutral unknown-alias
  acknowledgements, schema migration 14, and local operator administration.
- Use three bounded application retries with 5/10/20-second exponential
  backoff. ACKs, explicit terminal failures, and newer replies still cancel
  remaining attempts; every attempt retains the shared five-second cooldown.
- Identify the node behind RECORD's independent highest received-hop result,
  avoiding confusion with the best-SNR node.
- Rewrite the README around MeshCore as the primary feature focus while
  documenting maintained Meshtastic support and its compatibility default.

## [1.6.0] - 2026-09-20

- Retry an unacknowledged DM once after 45 seconds. Late ACKs and newer replies
  cancel the retry, explicit route failures remain terminal, all retry attempts
  obey the global airtime cooldown, and no channel fallback is introduced.

- Add ACK-committed NEW summaries, expiring operator NEWS notices, cached Bay
  Area MeshCore LOCAL/GUIDE references, and comparative CHECK radio reports.
- Make MAIL public with recipient opt-in; add INBOX and authorized numbered
  REPLY routes that expire with the original note. `mail_public=false` restores LAB gating.
- Add bounded typo suggestions, MENU SOCIAL, and local 90-day usefulness
  measurements. Add migration 13 and defer self-service privacy controls to TODO.

- Keep the daemon and CLI available after code-only upgrades: load astronomy
  dependencies on demand, with an unavailable reply if packages are missing.
  Document dependency synchronization before a systemd restart.

- Fix MeshCore shortened-ID collisions: quarantine conflicting identities and
  persist contact-key fingerprints across restarts (schema 12).
- Preserve unread mail suffixes; consume only acknowledged text, idempotently.
- Buffer early delivery acknowledgements until send correlation is registered.
- Keep daily rollups provisional until queued/submitted requests settle.
- Add PATH/ROUTE diagnostics, offline MOON and SUN, and online satellite
  radio-pass estimates with optional POS coordinates; all remain DM-only.
- Fix the MeshCore daemon test's lint failure.


### Added

- Debug rejection traces explain command and invite cooldowns, LAB restrictions,
  invalid commands, eligibility failures, quotas, queue refusals, and send failures.

- `mgarden advert` requests a MeshCore flood advertisement through the running
  daemon, with radio acceptance feedback and the existing 60-second advert floor.

### Fixed

- Clarify SEEN’s unexplained “unknown” as “hops not reported”, label known
  routes, and include a 24-hour packet count when it fits. Preserve the
  reception and activity fields within each transport’s message budget.

- Default channel-invite cooldown to 60 seconds per node, shared by `!garden`
  and `!dm`; explicit configured intervals remain respected.

- Refresh stored MeshCore contact names after advertisements so channel knocks
  from newly learned companions work without restarting the daemon. Refreshes
  update metadata without counting an extra radio reception.

- Accept `!dm` as a channel-knock alias for `!garden`, sharing the same private
  invite, sender resolution, and per-recipient cooldown.

- Register MeshCore event consumers before connecting so imported contact names
  and startup receptions reach the database. Log ignored channel-knock reasons
  without recording channel text or claimed sender names.

- Guestbook and board labels resolve the current known node name, falling back
  to the saved signing name and finally a short ID. Node reports and mail add
  known names beside IDs; display names remain sanitized and radio-bounded.

- Rewrite every menu page for MeshCore's 120-byte default, including the LAB
  unlock reply. Main menus reserve space for the quota hint; complete command
  names and the final menu lines now survive transmission on both transports.

## [1.5.1] - 2026-09-19

### Fixed

- MeshCore companion receptions now feed persisted observations, daily node
  totals, records, and receive metrics independently of command eligibility.
  Channel sender names remain unattributed; imported contacts no longer
  fabricate activity, live receptions refresh last-seen, and contact positions
  populate history used by distance records.
- MeshCore direct routing no longer implies zero RF hops. Unknown PKI is shown
  as unavailable; MeshCore command counts are labelled companion events.
- WEEK uses seven calendar days and retains reply submission counts after
  delivery outcomes change. Schema 10 preserves known historical submitted
  and ACKed totals while adding durable submission tracking.

- **The optional `meshcore` package is now genuinely never imported unless
  a MeshCore transport is actually constructed.** Previously
  `meshgarden/meshcore_client.py`'s guarded `import meshcore` ran at that
  module's own load time, and `main.py` imports that module
  unconditionally regardless of the configured `[radio] transport` — so on
  any install where the optional package happened to be present, merely
  running `mgarden` loaded it and its dependency chain even on a
  Meshtastic-only configuration. The import is now deferred into
  `RealMeshCoreClient.__init__`, the point at which the transport is
  actually being built, so a Meshtastic-only daemon never loads it. No
  behavior change when `meshcore` is absent (still the same actionable
  `MeshCoreLibraryMissing` error) or when the MeshCore transport is
  installed and configured.
- **The MeshCore adapter now sets the radio's clock from host time on
  every connection**, unconditionally, the same way the Meshtastic path
  already does. A MeshCore radio's clock lives in a power domain any power
  loss wipes, and nothing else corrects it — observed on real hardware:
  after a power loss, the radio's clock read 831 days in the future, and
  outgoing messages carried that timestamp. The firmware refuses to move
  its clock backwards (`ERR_CODE_ILLEGAL_ARG`), so a radio whose clock is
  already ahead of host time is left alone and logged distinctly from a
  genuine failure, never retried. This means the MeshCore adapter no
  longer performs exactly one write to the radio: it now performs two —
  this unconditional clock set, and the pre-existing `autoadd_config` bit
  write, which remains persisted configuration, gated behind
  `meshcore_autoadd_overwrite`, and unchanged in every other respect.
- **`scripts/install-rpi.sh` now installs the optional `meshcore` extra**
  (`uv sync --extra meshcore`) when the deployment uses the MeshCore
  transport: pass the new `--transport meshcore` flag, or re-run the
  installer against an existing config whose `[radio] transport` is
  already `"meshcore"`. Previously `uv sync --frozen --no-dev` never
  requested the extra, so a fresh Raspberry Pi install for a MeshCore
  radio came up without the library and the daemon exited at startup. No
  effect on a Meshtastic-only install, which continues to skip the extra
  exactly as before.
- **The MeshCore adapter now actually fetches queued messages — previously
  the daemon received nothing, ever.** Confirmed on real hardware: the
  companion firmware queues every inbound direct/channel message behind a
  `MESSAGES_WAITING` push and never delivers it on its own, no matter how
  long a client subscribes and waits; the daemon never called the
  library's `start_auto_message_fetching()`, so it received zero direct
  messages, zero channel messages, and zero knocks on a real radio despite
  passing every existing test. That call now happens once per connected
  session, right after subscriptions are wired; a failure is logged and
  swallowed like every other per-connection setup step, and no explicit
  teardown is needed (the library's own `disconnect()` stops it). The test
  fake (`tests/fake_meshcore.py`) now models the same queue-and-fetch
  shape instead of dispatching unconditionally.
- **The MeshCore adapter now sends a flood self-advertisement, so other
  nodes can find it.** Also confirmed on real hardware: the radio only
  became reachable after a manual flood advertisement — without one, a
  node that has never heard this radio cannot resolve a path back to it,
  so even a correctly-acknowledged reply can fail to route. The daemon now
  sends one self-advertisement (`commands.send_advert(flood=True)`) on
  every successful connection, and repeats it thereafter on the new
  `[radio] meshcore_advert_interval_seconds` (default 14400 = 4h; `0`
  disables the repeat only). This is a deliberate, narrow exception to the
  "transmits nothing but a direct-message reply" guarantee — presence
  beaconing, not message traffic, and the same category of transmission
  Meshtastic firmware already performs on its own (NODEINFO broadcasts).
  Documented in `docs/configuration.md`, `docs/architecture.md`, and
  `docs/security-and-privacy.md`. Meshtastic is unaffected: the new key is
  ignored under `transport = "meshtastic"`.

## [1.5.0] - 2026-09-19

### Added

- **MeshCore radio support: a second radio backend, selected with
  `[radio] transport = "meshcore"`.** The default remains
  `"meshtastic"`, and a Meshtastic install is unaffected in every respect —
  same behaviour, same dependencies, same config. A MeshCore daemon runs
  the same bulletin board over a MeshCore companion radio: the same
  commands, the same eligibility rules, the same quota/rate limiting, and
  the same single guarantee that the service transmits nothing but a direct
  message to the sender of an eligible trigger. The `meshcore` library is an
  **optional dependency** (`uv sync --extra meshcore`, or
  `pip install 'meshgarden[meshcore]'`); it is never imported unless the
  MeshCore transport is actually configured and started, so a
  Meshtastic-only install works normally whether or not the package is
  present, and configuring the MeshCore transport without it installed
  fails with an actionable message rather than an import error. `mgarden
  doctor` gained a `meshcore` check reporting whether the
  package is installed and which channel/autoadd settings are in force.
  New `[radio]` keys, all optional and all ignored under
  `transport = "meshtastic"`: `transport`, `meshcore_channel` (default
  `"meshgarden"`; empty string disables channel listening entirely),
  `meshcore_autoadd_overwrite` (default `false` — the only radio config
  write the daemon will ever make), `meshcore_max_text_bytes`,
  `meshcore_backoff_base_seconds`, `meshcore_backoff_max_seconds`,
  `meshcore_max_reconnect_attempts`, `meshcore_min_stable_session_seconds`,
  `meshcore_knock_cooldown_seconds`, `meshcore_max_pending_knocks`. On every
  connection (not merely at startup) the daemon seeds its contact roster
  from the contacts the radio already stores, resolves the configured
  channel name to an index, and performs the opt-in autoadd write — so a
  restart answers a long-known contact immediately, and a daemon started
  before its USB serial device enumerates still gets channel triggering once
  the radio appears. Documented in `docs/configuration.md` and
  `docs/architecture.md`.

- **MeshCore channel knock: the one thing a channel message may ever
  trigger.** With `[radio] transport = "meshcore"` and `[radio]
  meshcore_channel` set (default `"meshgarden"`), the service now listens on
  that one channel for a message whose entire body is exactly `!garden`
  (case-insensitive, nothing else). Because a MeshCore channel message
  carries no verified sender identity, a knock is bounded to exactly one
  outcome: a single direct-message invite to the resolved contact, worded so
  it never claims the recipient asked for it, rate-limited to once per
  `[radio] meshcore_knock_cooldown_seconds` (new key, default 86400 seconds =
  24h) **per resolved recipient node**, persisted in a new `channel_knocks`
  database table so the limit survives a restart or crash loop. A knock
  never reaches the command dispatcher and never produces a channel reply of
  any kind — the transport still has no channel-send capability at all.
  Ordinary channel chatter, an unresolvable/ambiguous claimed sender, and any
  channel message that merely resembles a command all continue to produce
  nothing. Meshtastic is unaffected: it has no channel-trigger concept.
  Documented in `docs/security-and-privacy.md#meshcore-channel-knock` and
  `docs/operations.md`.

### Fixed

- **`Collector.record_contact_identity`'s docstring was wrong about its own
  behavior.** It claimed the insert never touches `observed_last_seen`; the
  INSERT statement plainly sets it on a node's first-ever MeshCore contact.
  Corrected the docstring, not the code — a `NEW_CONTACT` event is itself
  evidence of reception, so seeding "last seen" from it is a reasonable
  choice — and documented the real, narrower guarantee (a repeat contact
  event never re-touches it, and MeshCore's trigger/reply path never touches
  it either, so a MeshCore node's "last heard" freezes at its first contact).
  No behavior change.

### Changed

- **Internal: a `Transport` boundary now sits between the daemon and the
  radio, in preparation for a second (MeshCore) backend.** The daemon
  decides trigger eligibility (`commands.decide_inbound`) and sends every
  reply through a `Transport` Protocol (`send_dm`, `status`, `identity`,
  `on_delivery`, and no channel-send member at all) instead of talking to
  `RadioAdapter` directly; a normalized `Inbound` model (sender, recipient,
  channel scope, text, ids, signal) carries one received message across
  that boundary. `meshgarden.meshtastic_transport.MeshtasticTransport`
  wraps the existing `RadioAdapter`; the MeshCore backend above is the
  second implementation of the same Protocol. The reply byte budget
  (180 bytes) is now read from the transport (`Transport.max_text_bytes`)
  rather than hardcoded at each truncation site. `Daemon` also gained a
  `transport=` constructor seam so tests can inject a fake transport.
  **No user-visible behavior change:** Meshtastic replies, eligibility, and
  the 180-byte budget are byte-for-byte the same as before.

- **Internal: node-id parsing/display for `SEEN`, `ME` and the operator CLI
  now goes through the `Transport` boundary instead of daemon-local
  helpers.** `Collector.resolve_node` (already routing `SEEN`'s/`MAIL`'s
  `<node>` argument through `transport.parse_node_argument`) is unchanged;
  newly wired are `_handle_seen`'s found-node tail (`transport.short_label`,
  replacing the removed `main._node_id_tail_label`), `_handle_seen`'s
  not-found/not-heard label (`main._node_label_for`, now shape-checked via
  `transport.parse_node_argument` and formatted via `transport.short_label`,
  still derived from the visitor's ARGUMENT rather than a resolved id — see
  ruling 1/privacy), and `_handle_me`'s own-id header
  (`transport.display_label`). The operator CLI's node-id validation
  (`mgarden block`/`unblock`/`lab revoke`/`quota reset`/`quota show`/
  `mail purge`) now shares one function, `config.parse_node_id_argument`,
  with both transports' `parse_node_argument` implementation, rather than
  four separate copies of the same regex check. **No user-visible behavior
  change on Meshtastic:** every one of these methods' current
  implementations reproduces exactly what the code it replaced already did
  (same "!xxxxxxxx"/4-hex-tail shape, same lowercase canonicalization, same
  `"!" + last 4 hex chars` label); `RECORD`'s and the `MAIL` list's own
  node-tail formatting (bare 4 hex chars, no `!`) were deliberately left
  alone, since that is a different, pre-existing display convention from
  `short_label`'s and routing it through `short_label` would have added a
  `!` to those replies.

- **CLI: `mgarden block`/`unblock`/`lab revoke`/`quota reset`/`quota show`/
  `mail purge` now tolerate surrounding whitespace in their `<node>`
  argument.** A node id argument padded with leading/trailing whitespace
  (e.g. `mgarden block "  !aabbccdd  "`) used to exit 2 ("invalid node id");
  it is now accepted, because these subcommands were moved onto the same
  `config.parse_node_id_argument` both transports' `parse_node_argument`
  already used, which strips whitespace before matching. This only accepts
  MORE than before — a genuinely malformed argument (whitespace-padded or
  not) still exits 2 with the same message.

## [1.4.0] - 2026-09-17

### Added

- **`QUOTE` (alias `FNORD`): one Discordian quotation a day, LAB tier.**
  Gated per node behind `[service] lab_unlock_word` alongside
  `POST`/`READ`/`BOARDS`/`MAIL`, so the collection soft-launches to nodes
  that have unlocked the tier. A locked node gets the ordinary
  unrecognized-command treatment for both words (no reply, no
  `processed_requests` row, no quota spent), and `HELP QUOTE`/`HELP FNORD`
  are silent too, so it cannot learn the command exists. `MENU DAILY`
  (ungated) never names it; `MENU LAB` does. The gate reads `LAB_COMMANDS`
  and the new derived `LAB_HELP_WORDS`, which folds in alias words, so an
  alias can never leak a gated command's usage string. A small,
  source-checked local collection in `meshgarden/fortune.py` (`QUOTES`)
  covering Robert Anton Wilson, the jointly written *Illuminatus!*
  (credited to both Robert Shea and Robert Anton Wilson), and the
  *Principia Discordia*. No external API and no generation at runtime.
  Unlike `ICHING`, the draw is keyed on `(fortune_salt, local calendar
  day)` alone with no sender id and no RNG, so every node on the mesh
  reads the same line all day, it survives a restart, and it rolls over at
  local midnight in `[service] timezone`. Each entry records the
  author(s), the work, the speaking character where the line belongs to
  one, and a `source_ref` citation; only the short attribution rides in
  the reply, and a test asserts quotation plus attribution fits the
  180-UTF-8-byte budget for every entry, since a quotation must never be
  truncated. `QUOTE`/`FNORD` ride the existing per-node cooldown, dedup,
  daily quota, and one-reply-per-request path unchanged.
- **`/api/quote.json`** serves the same quotation, with its `source_ref`
  and the exact `reply` body a DM would carry. The JSON feeds are not
  LAB-gated: the dashboard is operator-facing and loopback by default, and
  the unlock is a per-node radio gate with nothing to key an HTTP request
  on.
- **`/api/daily.json`** serves whichever daily draw
  **`[service] daily_display`** selects -- `"haiku"` (the default, so an
  existing config keeps the behavior it had) or `"quote"` -- tagged with a
  `kind` field so one display can render either. `/api/haiku.json` and the
  `HAIKU` command are unchanged either way.

### Changed

- **`MENU LAB` no longer tags its own header "experimental".** The page had
  to make room for `QUOTE` inside the 180-byte budget; the word is already
  on the top-level `🧪 MENU LAB · experimental` line that leads there, and
  the page's own "Things may change or vanish" trailer is unchanged, so the
  header gave up the tag rather than a command giving up its line.

### Notes

- *Principia Discordia* carries its own "All Rites Reversed -- reprint what
  you like" notice. The Wilson and Shea & Wilson works are in copyright and
  are bundled as short attributed quotations only; no redistribution
  permission was sought or granted for them.

## [1.3.1] - 2026-09-17

### Fixed

- **A radio with a drifted clock no longer silently swallows every DM.**
  The ESP32 has no battery-backed RTC, so after any reboot its clock can be
  wrong; once the skew exceeds `reply_ttl_seconds`,
  `commands.check_eligibility` rejects every incoming DM as `STALE` (a
  replayed packet). Each one was still recorded as an observation and the
  sender's radio still acked it, so the node looked perfectly healthy from
  outside while answering nothing. Observed in the field at 89s of skew
  against a 60s window.

  `RadioAdapter` now sets the radio's clock from the host's on every
  identity-verified (re)connect -- the right moment, since a reconnect
  usually means the radio just rebooted. It is a local USB admin write, not
  a LoRa transmission; it is skipped for a radio whose identity did not
  verify, and a refused write is logged and ignored rather than costing an
  otherwise usable connection.

- **A stale-rejected DM is now visible.** It previously left no
  `processed_requests` row, no INFO/WARNING log line, and only a folded
  `not_ready` metric label -- the same silent-failure shape as a dead
  radio. The daemon now logs one bounded WARNING per episode naming the
  measured skew and the configured window, re-armed by the first DM that
  gets through.


## [1.3.0] - 2026-09-17

### Added

- **Radio watchdog (`mgarden radio-watchdog`), with alerting and optional
  auto-recovery.** Run once a minute by the new
  `meshgarden-watchdog.timer`, it polls the metrics endpoint from *outside*
  the daemon and pages via [ntfy](https://ntfy.sh) when the node has gone
  deaf -- the one fault the daemon cannot report on its own behalf, since a
  dead radio leaves its reconnect thread perfectly healthy. Polling
  externally also catches a daemon that has died or hung outright.

  It distinguishes `radio_down` (daemon up, radio deaf) from `unreachable`
  (daemon stopped, crashed or hung) and will power-cycle the radio's USB
  port via `uhubctl` only for the former -- cutting power cannot fix a
  daemon someone stopped deliberately. Cycles are capped per day with a
  cooldown between attempts, so a hardware fault cannot loop on
  power-cycling forever; set the cap to 0 for alert-only behavior. A grace
  period (default 300s) keeps the routine re-enumeration that follows any
  phone-app config change from paging anyone, and escalation is latched to
  one page per outage.

  Alerts carry durations, counts and state names only -- never a node id, a
  node name, or message text. The ntfy URL lives in a root-only
  `/etc/meshgarden/watchdog.env` (template: `systemd/watchdog.env.example`),
  never in `config.toml` and never in the watchdog's own state file.

- **`mgarden notify-test`** sends one test alert through the exact notifier
  the watchdog uses, so a pass proves the real alert path works rather than
  merely that some HTTP request can be made. It never echoes the topic URL.


## [1.2.1] - 2026-09-17

### Fixed

- **A dead radio is no longer silent.** The daemon could sit indefinitely
  with `connected: False` while still reporting `daemon_status: ok` and all
  four workers healthy, because a radio that stops answering leaves the
  reconnect thread perfectly healthy -- looping, beating and backing off
  with nothing to talk to. A sustained outage (over 5 minutes) now logs one
  ERROR naming the duration, and one INFO when it clears; `mgarden status`
  and the status file gained `radio_disconnected_seconds`.

- **The reconnect loop now logs why a connect attempt failed** (exception
  type and message, no traceback). It previously discarded the exception
  and logged a bare "radio connect failed; will retry", leaving a missing
  device node, a permissions problem and a radio that enumerates but never
  answers indistinguishable from each other.

- **The reconnect backoff no longer suppresses the systemd watchdog.** The
  whole backoff slept inside a single `_stop_event.wait(delay)` without
  beating, contradicting `on_heartbeat`'s documented contract ("including
  the backoff wait"). Since the radio worker's stall threshold is sized from
  `backoff_max`, at the 300s default the two were equal and every cycle
  crossed it, so the worker flapped stalled -> recovered every 5 minutes --
  which kept `WatchdogSec` being pinged instead of tripping it. The wait is
  now chunked so the heartbeat keeps firing, making a heartbeat lapse mean
  only that the thread has genuinely stopped iterating.

### Added

- **`mgarden tail -n N`** prints the last N existing trace lines before
  following new ones (like `tail -n N -f`); mutually exclusive with
  `--from-start`.

### Changed

- **Guestbook/board entry ids no longer appear in visitor-facing replies.**
  `SIGN`'s ack is now `🌱 Signed!`, `UNSIGN`'s is `🌱 Your newest note is
  now hidden`, `POST`'s ack drops the trailing `#<id>`, and `GUESTBOOK`/
  `READ <topic>` listing lines drop the leading `#<id>` (just `<name>:
  <message>`). `GUESTBOOK #<id>` itself still works for a visitor who
  already has an id, but it is no longer advertised in `HELP`/`MENU`. The
  operator CLI (`mgarden guestbook list/hide/delete`) is unchanged — ids
  remain the operator's handle.

## [1.2.0] - 2026-09-15

### Added

- **Repository hygiene:** `SECURITY.md` (private vulnerability reporting via
  GitHub advisories), `CONTRIBUTING.md`, a bug-report issue form, and a
  Dependabot config for `uv` and GitHub Actions updates. The historical
  `prompt.md` build brief is untracked (its design rulings live in
  `CONTRIBUTING.md`); `ccc.toml` and `local/` are gitignored.

- **Unlocked nodes see `MENU LAB` on the top-level menu**: a node that has
  unlocked the LAB tier now gets a `🧪 MENU LAB · experimental` line on its
  own `MENU`/`?` reply (the `⚠️ experimental` tag moves off the `INFO` line
  onto this new one); a still-locked node's `MENU` is unchanged and never
  mentions the tier.

### Changed

- **Python package directory renamed `meshtastic_observatory/` -> `meshgarden/`**
  (imports, tests, packaging metadata, and docs updated to match). The
  `mgarden` console script is unchanged; `meshtastic-observatory` remains
  installed as a deprecated alias for one more release. DB, config, and
  data paths are unaffected — none are derived from the package name.
  Existing installs pick this up with `uv sync --frozen` followed by
  `sudo systemctl restart meshgarden`.
- **`VERSION`'s reply now reads `🌱 MeshGarden v<version>` (e.g. `v1.2.0`)** (previously no
  `v` before the version number).
- **Deployment-specific and hardware-validation docs moved to `local/`**
  (gitignored, untracked): the observatory data model doc, the hardware
  testing doc, the manual acceptance checklist, and the service radio
  setup notes. The README keeps a generic "Hardware tested" table
  (board models, firmware, and library versions only) and the statement
  that hardware tests are deselected by default and real-radio
  validation is manual and incomplete.

## [1.1.1] - 2026-09-15

### Added

- **GitHub Actions CI** (`.github/workflows/ci.yml`): `uv sync --frozen`,
  `ruff check`, and the pytest suite (hardware tests deselected) on
  Python 3.11 and 3.13 for every push to `main` and every pull request.
  CI badge added to the README.

- **`/api/summary.json`** on the local dashboard: aggregate JSON for
  external displays on the same host (today's digest numbers, the 24-hour
  packets/senders series, the ten most recently heard nodes with their last
  SNR, and the radio/uptime/queue fields from the status file). Same
  read-only, loopback-only rules as the HTML pages; no guestbook text or
  positions.
- **`WORDLE SCORE`/`SCORES`** and **`BOOK SCORE`/`SCORES`**: mesh-wide
  today and rolling-7-day aggregate scores for each game (solved counts,
  average guesses for WORDLE, guess counts for BOOK) — never node ids or
  per-player data, and neither counts as a guess or a shown. `MENU GAMES`
  and `HELP BOOK`/`HELP WORDLE` now mention SCORE.
- **`VERSION`**: hidden, operator-only bare command that replies with the
  running Meshgarden version. Not listed in `MENU`/`HELP` — a support/
  debugging aid, not a visitor-facing feature.
- **`mgarden quota reset <node-id>`/`quota show <node-id>`**: operator
  adjustment/inspection of one node's daily command/`SIGN` quota, default
  day today in `[service] timezone`. `reset` deletes the whole
  `node_daily_quota` row by default, or zeroes just one counter with
  `--commands-only`/`--signs-only`; a missing row ("nothing to reset") is
  still exit `0`. Neither command touches the in-memory `SIGN` cooldown.

### Changed

- **Docs: privacy corrections, sanitized hardware notes, copy-pasteable
  quick start, and 1.1.1 release notes.** `security-and-privacy.md` and
  the README now state plainly that `MAIL` bodies are stored until
  delivered/expired and that `run --debug` prints full DM text (LAB
  unlock word redacted) to a local trace file, correcting a README bullet
  that claimed `MAIL` bodies are "never stored"; the same doc gained a
  retention summary (`observations`/`node_daily`/`links`/positions, with
  their config keys and defaults). Real hardware identifiers (node IDs,
  MAC, serial paths, a personal path, a pairing PIN) in the service radio
  setup notes and the hardware testing doc were
  replaced with generic placeholders. The README's Raspberry Pi quick
  start now uses the real clone URL and runs as-is, with optional
  installer flags split into their own example blocks. New
  `docs/RELEASE-NOTES-1.1.1.md` (removed in 1.2.0) summarizes
  this and the `[1.0.0]` release for upgraders, with a known-limitations
  list.
- **README: accuracy pass (commands table, config keys, versioning) and a
  cleaner layout**, plus a static shields.io version badge checked by the
  packaging test.
- **An empty guestbook or topic board now says so, honestly.** `GUESTBOOK`
  on page 1 with nothing ever visible replies `📖 Guestbook is empty` (was
  the technically-true-but-confusing `📖 Guestbook · latest 0 of 0`); `READ
  <topic>` on an existing-but-currently-invisible topic (every post
  hidden/moderated) replies `📌 #<topic> is empty` instead of `No more
  entries`, which wrongly implied there was ever something to page
  through. The topic name is always the already-validated, normalized
  form by the time it's echoed here, never raw visitor text.
- **A rejected `SIGN` now explains why instead of staying silent.**
  Cooldown (`🌱 Too soon to sign again`), identical-text suppression
  (`🌱 You already signed that`), the daily sign quota (`🌱 Daily sign
  limit reached`), an empty message, and an over-length message each get
  their own short, fixed reply — never the visitor's own text, never more
  detail than the reason. `POST`/`MAIL` are unaffected (they still share
  `Guestbook.validate` but keep their own reply behavior); the
  `queue_full` case is still silent, since no reply can be sent at all
  then. Bounded metrics outcomes (`rejected`/`rate_limited`/`sign_quota`)
  are unchanged.
- **`WHOAMI` and `SIGNAL` are now aliases of `ME`** (operator decision):
  both still work as input words, but dispatch to `ME`'s reply, which now
  carries every field `WHOAMI`'s own used to (RSSI included) on its signal
  line. `MENU STATS` and `HELP`/`? WHOAMI`/`SIGNAL` reflect the merge
  (`HELP WHOAMI`/`HELP SIGNAL` return `ME`'s usage).
- **`ICHING` and `HAIKU` moved off `MENU GAMES` onto their own `MENU DAILY`
  sub-page** (also `? DAILY`) (operator decision): they're daily draws, not
  games. `MENU GAMES` keeps `BOOK`/`WORDLE`. The top-level `MENU`/`?` reply
  now points to all three sub-pages (`MENU STATS`/`MENU GAMES`/`MENU
  DAILY`); its `GUESTBOOK`/`UNSIGN` lines lost their short glosses to make
  room.

### Fixed

- **UNSIGN now actually frees the slot it hides.** Operator-confirmed bug:
  SIGN, then UNSIGN, then the same SIGN again minutes later got no reply
  at all. `is_duplicate_sign` matched hidden entries too, so the just-
  hidden entry blocked re-signing the identical text; UNSIGN also never
  reset the sender's SIGN cooldown, so an immediate retry was cooldown-
  blocked regardless. `is_duplicate_sign` now only matches visible entries
  (the anti-spam cap on repeat SIGNs is the daily sign quota, not this
  rule), and UNSIGN resets the SIGN cooldown via the new
  `RateLimiter.reset_sign`.
- **`SEEN`'s "not heard" reply no longer claims a 7-day window we haven't
  actually listened for.** A node that resolves but wasn't observed (or
  never resolves at all) now says "not heard since I started listening
  <ago>" while the service has been running less than 7 days, and "not
  heard yet" if it has recorded no observations at all — "not heard in 7
  days" only once we've genuinely been listening that long.
- **Old/replayed DMs no longer bypass the freshness guard.** `_ingest_sink`
  was overwriting every packet's `rx_time` with the ingestion clock, so
  `check_eligibility`'s staleness check (`received_at - rx_time >
  reply_ttl_seconds`) could never fire — a DM replayed long after it was
  actually sent still got a live reply. `rx_time` is now shifted by the
  clock's own test-only offset instead of overwritten, so genuinely stale
  packets are rejected while `advance_seconds()`-based tests are unaffected.
- **Reading `MAIL` no longer deletes notes before they are actually
  delivered.** `_handle_mail_list` used to hard-delete the shown notes as
  soon as the reply was merely queued -- a send failure or expiry then
  lost them for good even though the requester never received the list.
  Deletion is now deferred until that reply's own outcome is known: a
  genuine ack marks the notes delivered (and deletes them, same as
  before), while any other terminal outcome leaves them pending for the
  next `MAIL` read. The delivery tracking is kept for the full
  `ack_timeout_seconds` window (plus the reply TTL), and the request's
  state is consulted before any age bound -- an ack arriving after the
  60 s reply TTL but inside the 300 s ack window settles the notes
  instead of being discarded.
- **The daily digest is now scheduled on `[service] timezone`'s calendar
  day, not the UTC day.** The digest's own queries are entirely in local-
  day terms, but its write used to be triggered by the UTC day changing
  (`_on_local_midnight`) -- for a timezone far enough from UTC, that
  fires hours before (or after) local midnight has actually arrived,
  writing a day's digest under its complete label while missing however
  many hours of that local day hadn't happened yet. Tracked separately
  now (`_last_digest_local_day`); the UTC-day rollup/prune scheduling is
  unchanged.
- **The `--debug` exchange trace no longer logs the LAB unlock word.**
  `Command.LAB_UNLOCK`'s whole message IS the operator-configured word,
  but the trace's `text={obs.text!r}` ran unconditionally for every DM;
  it's now redacted to `<lab unlock redacted>` for that one command,
  matching the promise already documented in security-and-privacy.md.

## [1.0.0] - 2026-09-14

First stable release. Same feature set as 1.0.0rc2 plus the fixes and
additions below.

### Changed

- **Reply layouts reworked for readability on a phone screen** (every
  reply is still one packet, <= 180 UTF-8 bytes). `ICHING` puts the
  hexagram number on its own line above the symbol and title. `MENU
  GAMES`, `MENU STATS` and `MENU BOARDS` use the same emoji-led,
  one-group-per-line layout as the top-level menu. `HELP <cmd>` replies
  are now the syntax on the first line, then one short line per detail,
  instead of a single run-on line. `PING`, `WHO`, `GARDEN`, `STATS` and
  `BOARDS` split their facts onto separate lines (`BOARDS` lists one
  `#topic · N posts` line per topic). `ME` and `SEEN` spell out "First
  seen … · 5/7 days" and "mail on · 2 waiting". An unknown hop count now
  renders as "hops n/a" instead of "hops n/a · n/a".

### Fixed

- **`mgarden doctor` reported `metrics_port ... not bindable (Address
  already in use)` as a failure even when it was our own daemon holding
  the port** (e.g. right after `install-rpi.sh` restarts the service and
  immediately runs `doctor`). `metrics_port` now checks `status_file`'s
  liveness first and reports the held port as ok when the daemon is
  alive; a stale/missing status file still fails as before. The
  installer's post-`doctor` hint no longer blames `expected_node_id`/
  `device` and instead points at re-running `mgarden doctor`.
- **`mgarden guestbook export` silently included board posts; `--topic`
  was compared raw.** `export` now defaults to the main guestbook only
  (`topic IS NULL`, matching `list`/`review`/`approve`'s ruling-9 default)
  and gains its own `--topic <name>` to export one board's entries instead;
  `--topic` on `export`/`review`/`approve` (and `list`/`hide`/`delete`) is
  now normalized (`strip().removeprefix("#").lower()`) before use and
  refused with exit 2 and "invalid topic" on stderr if it doesn't match
  `^[a-z0-9]{2,12}$`, instead of silently matching nothing. `mail purge`
  and `lab revoke` now validate their `node_id` argument the same way
  `block` does, exiting 2 on a malformed id instead of silently deleting
  nothing. `web.py`'s `/guestbook?topic=` now falls back to the main
  guestbook for a malformed topic instead of rendering a board page with
  no entries under a raw, unvalidated name.
- **Distance-plausibility for "furthest direct"/"furthest observed".** An
  operator report showed `RECORD` claiming "furthest observed 4325 km" from
  an MQTT-relayed or bogus-position node. Every "furthest" candidate is now
  RF-only (`observations.via_mqtt = 0`; `1`/`NULL` excluded), position-
  plausible (valid lat/lon range, not the `(0.0, 0.0)` sentinel, and
  `precisionBits >= 10` when present), and distance-consistent with its
  derived hop count (new `[observatory] max_direct_km` / `max_km_per_hop` /
  `max_observed_km`, defaulting to `300` / `150` / `1000` km, all validated
  positive; unknown hops are never eligible). Nothing implausible is
  deleted — only excluded from records. `/` and `/records` gain a
  separately labelled "Furthest observed (incl. MQTT-relayed/unknown)"
  figure for visibility; `/map` marks implausible nodes with a hollow
  marker and excludes them from ring auto-scaling; `/nodes/<id>` states the
  plausibility verdict; `/propagation`'s "unusual distance" now only
  considers plausible RF distances.
- **RF packets were being recorded as `via_mqtt` unknown (`NULL`) instead of
  RF.** The Meshtastic library's `MessageToDict` omits a field entirely when
  it is at its protobuf default (`False`/`0`), so a genuinely-RF packet's
  absent `viaMqtt` key was stored as `NULL` ("unknown origin") rather than
  `0` ("RF"), silently shrinking the pool of packets eligible for "furthest
  direct"/"furthest observed" (see the distance-plausibility fix above,
  which only trusts `via_mqtt = 0`). `radio.extract` now reads `via_mqtt`
  (and, for the same reason, `hop_start`/`hop_limit`) from the raw protobuf
  when available, falling back to the dict only when it isn't; `NULL` is now
  reserved for rows recorded before this fix. `RECORD`/`GARDEN`'s distance
  lines work again for ordinary RF traffic.

### Added

- **`config.example.toml` completeness + installer config sync
  (`mgarden config-sync`).** The example file was missing the seven keys
  the directory/boards/mail epic added to `[service]`/`[retention]`
  (`seen_enabled`, `daily_post_quota`, `daily_mail_quota`, `max_topics`,
  `mail_pending_limit`, `lab_unlock_word`, `mail_expiry_days`); it now
  documents every `ServiceConfig`/`RetentionConfig` field, each at its code
  default (enforced by a test against the dataclasses). New
  `meshgarden/config_sync.py` (`sync_missing_keys`) and `mgarden
  config-sync --template PATH [--config PATH] [--dry-run]` add to an
  existing config any key present in a template but missing from it -- with
  its default value and comment -- without ever changing, reordering, or
  reformatting a line already there; a real run backs the file up to
  `.bak.<timestamp>` first. `install-rpi.sh`, when re-run against an
  existing config (no `--force-config`), now runs this automatically (new
  `--no-config-sync` to skip), so upgrading in place picks up new config
  keys' defaults without ever touching an operator's existing values.
- **Directory: `SEEN`/`WHO`/`ME`/`PING`, name-aware `WHOAMI`/`RECORD`.**
  Four new bare/argument commands report only what the mesh already
  broadcasts, never a third party's position or message counts: `SEEN
  <node>` (accepts `!xxxxxxxx`, a 4-hex tail, or an exact short name) reports
  when/how the service last heard that node in the last 7 days -- a node
  never heard and a node not heard recently get the deliberately identical
  reply, and `[service] seen_enabled = false` turns it off entirely (silent,
  as if the command didn't exist); `WHO` reports nodes heard in the last
  hour (direct count) and last 24h, no ids; `ME` is a superset of `WHOAMI`
  (own id, activity, this request's signal, quota, and mail status); `PING`
  is a one-line signal check (SNR/hops/direct-or-relay/PKI) from the
  request's own packet fields. `WHOAMI`/`ME`'s "You are ..." line and
  `RECORD`'s `Best SNR`/`Furthest observed` lines now show `<name> · <tail>`
  when a short name is on file (sanitized, truncated to 4 characters),
  falling back to the tail alone otherwise.
- **Topic boards: `POST`/`READ`/`BOARDS` (schema v6).** `POST <topic> <msg>`
  publishes to a named topic wall (topic shape `#?[a-z0-9]{2,12}`,
  case-insensitive, stored lowercase; up to 100 characters, 5/day per node,
  capped at 50 distinct topics ever created); `READ <topic> [page]` reads it
  back, latest 3 per page; `BOARDS` lists topics by post count. Boards reuse
  the `guestbook` table (a nullable `topic` column) with the same
  publication-consent, moderation, and duplicate-suppression rules as
  `SIGN` (scoped per topic); `UNSIGN` already covers boards with no code
  change, since its query has no topic filter. `mgarden guestbook
  list/hide/delete/review/approve` all gain `--topic <name>` (a filter for
  `list`/`review`, a guard for `hide`/`delete`/`approve`).
- **Mailbox: `MAIL` (schema v6).** `MAIL ON`/`MAIL OFF` is a per-node,
  default-off opt-in; `MAIL <node> <msg>` hands a note to a recipient (3/day
  per sender) -- stored only if the recipient has opted in and has fewer
  than 5 pending notes, but the sender always gets the same neutral
  acknowledgement either way, so a sender can never probe a recipient's
  settings (stored-vs-refused is visible only in aggregate, via the new
  `meshgarden_mail_notes_stored`/`_refused` counters); bare `MAIL` lists the
  requester's own pending notes newest-first and marks every shown note
  delivered (deleted) in the same request. Undelivered notes are pruned
  after `[retention] mail_expiry_days` (default 7 days); delivered notes are
  deleted immediately. New `mgarden mail [--reveal] [--json]` (counts by
  default, plus a `hidden` count of moderated notes; `--reveal` prints
  message bodies for operator abuse review) and `mgarden mail purge
  <node-id>`.
- **`HELP <cmd>` / `? <cmd>` usage lookup.** An argument matching a known
  command word now returns that command's one-line usage instead of the
  top-level menu (`STATS`/`GAMES`/`BOARDS` still select their `MENU`
  sub-pages); an unrecognized argument to plain `HELP` is silent, same
  exact-match-or-silence convention as every other command argument. Bare
  `HELP`/`SOS`/`EMERGENCY` are unchanged.
- **LAB tier: per-node unlock word gates `POST`/`READ`/`BOARDS`/`MAIL`
  (schema v8, `mgarden lab`).** These four commands are now off by default
  and unlisted in `MENU`; an operator turns them on with `[service]
  lab_unlock_word`, and a visitor unlocks them for their own node, once, by
  DMing that exact word as their whole message. A locked node's attempts at
  any of the four commands, their `HELP` entries, and `MENU LAB`/`MENU
  BOARDS` are all silent -- no reply, no `processed_requests` row, no quota
  consumed -- so a locked node cannot tell the tier exists. `lab_nodes`
  stores only a node id and unlock timestamp, never the word itself, which
  is never logged, echoed in a reply, or printed by any CLI/doctor output.
  New `mgarden lab [--json]` / `mgarden lab revoke <node-id>`.
- **BOOK: one solve per player per day (schema v7, `book_state`); puzzles
  capped at 6 emoji.** Once a visitor has solved today's puzzle, any further
  `BOOK <title>` gets a fixed "already solved today" reply instead of a new
  leaderboard tier, and no longer counts another guess/solve in
  `book_stats`. The 27-puzzle table is also capped at 6 emoji per puzzle
  (down from the previous 4-8 range).
- **WORDLE: `CLUE` and `GIVEUP`.** `WORDLE CLUE` reveals the next unrevealed
  answer letter and costs a try, exactly like a wrong guess; `WORDLE GIVEUP`
  (also `GIVE UP`, `I GIVE UP`) reveals the answer and ends the day
  immediately, never counting as a solve. The feedback row is also
  redesigned: a green letter is now shown as itself (e.g. `⬜ 🟨 ⬜ N ⬜`) in a
  four-line layout, rather than a green square.
- **`install-rpi.sh --reset-data [--yes]`.** Wipes the database and all
  other daemon-written state under `/var/lib/meshgarden`, keeping `backups/`
  and the config untouched; prompts for typed confirmation unless `--yes` is
  given.

- **Reply layouts: multi-line.** Every visitor reply (INFO, NOT_EMERGENCY,
  SIGN ack, storage-full, GUESTBOOK/UNSIGN, STATS, WHOAMI, GARDEN, RECORD,
  WEEK, ICHING, BOOK, WORDLE) is redesigned as an operator-approved
  multi-line layout (still one DM, still <= 180 UTF-8 bytes); GUESTBOOK's
  page reply gains a header naming how many of the total are shown (or
  which page) and a "GUESTBOOK <page+1> for older" footer when more entries
  exist. BOOK's per-puzzle emoji count was raised from a 5-emoji cap to 4-8
  here, and the 27 puzzles reworked to use 6-8 emoji where it told the story
  better -- superseded later in this same release by the 6-emoji cap noted
  above (BOOK: one solve per player per day).
- **System-wide `mgarden` / `meshtastic-observatory` commands.**
  `scripts/install-rpi.sh` now installs thin wrappers to
  `/usr/local/bin/mgarden` and `/usr/local/bin/meshtastic-observatory` that
  `exec` `<checkout>/.venv/bin/mgarden`, inserting
  `--config /etc/meshgarden/config.toml` before the subcommand when the
  caller didn't pass `--config`/`--config=...` and that file exists. Fixes
  the post-install `mgarden: command not found` (it previously only lived
  in `<checkout>/.venv/bin`, off the default `PATH`). Re-running the
  installer always regenerates both wrappers; `scripts/uninstall-rpi.sh`
  removes them.
- **`install-rpi.sh --fix-home-traversal`.** When the service-user
  traversal pre-flight fails, this flag applies `chmod o+x` (execute bit
  only, read/write bits untouched) to every ancestor directory from the
  checkout up to `/` that lacks it, printing each change; `--dry-run`
  previews the same chmods without applying them. Without the flag, the
  pre-flight's existing actionable error is unchanged, now also mentioning
  the new flag.
- **Docs: mesh metadata visibility and the OK-to-MQTT mitigation.**

## [1.0.0rc2] - 2026-09-14

### Changed

- Docs restructured: README.md trimmed from ~1176 lines to a concise
  overview (quick starts, commands table, safety/config/CLI summaries,
  architecture diagram); detailed reference material moved into
  `docs/architecture.md`, `docs/configuration.md`, `docs/operations.md`,
  the observatory data model doc, `docs/dashboard.md`,
  the hardware testing doc, `docs/security-and-privacy.md`, and
  `docs/games.md`, all linked from a new README "Documentation" section.

### Added

- **Two-level `MENU`/`?`.** `MENU`/`?` now takes an optional argument that
  selects a sub-page instead of listing every command flat: the top level
  names `SIGN`/`GUESTBOOK`/`UNSIGN`/`INFO` and points to `MENU STATS`
  (`STATS`/`WHOAMI`/`GARDEN`/`RECORD`/`WEEK`) and `MENU GAMES`
  (`ICHING`/`HAIKU`/`BOOK`/`WORDLE`). An unrecognized argument (`MENU xyz`,
  `MENU 2`) falls back to the top-level page with no echo of the argument;
  `processed_requests.command` still records plain `MENU`, and the
  `--debug` trace is unaffected. `INFO` is reworded to point at `MENU` for
  the full command list, keeping its existing name-only UTF-8 truncation
  so the fixed lines always survive.
- **Observatory dashboard pages (batch 3b): `/map`, `/topology`,
  `/propagation`, and a full `/nodes/<id>` profile.** Four more read-only,
  no-authentication pages on top of batch 3a's dashboard, same rules:
  local-only, no external assets, zero JavaScript (inline SVG + CSS
  only), per-request read-only SQLite connections, bounded queries, every
  page labelled observed/inferred where it matters, and every page
  renders a plain note (not an error) against an empty database or an
  unset `[observatory] latitude/longitude`.
  `/map` plots nodes with a known last position on an inline-SVG
  equirectangular local projection centred on the configured observatory
  (km east/north via `collector.haversine_km`) -- deliberately not a
  tile-based map, since fetching tiles needs internet access and an
  external asset, both disallowed here. Distance rings (5/10/25/50 km,
  auto-scaled to the furthest plotted node, capped at 300 km); marker
  colour by hop class (direct / 2-hop / 3+ hops / unknown, from the last
  7 days of `node_daily`); marker opacity fades from 1.0 (fresh) to 0.3
  (7 days old); faint lines for `links` rows, labelled "inferred"; a
  table below listing every plotted node (distance, bearing, hop class,
  last seen, position precision); query-string filters `?range=today|7d`,
  `?direct=1`, `?role=<allowlisted role>`, `?new=1` (first seen <=24h) --
  an unrecognized filter value is silently ignored (falls back to no
  filter/the 7d range) rather than a 400, the same forgiving convention
  `_parse_page` already uses; capped at the nearest 300 plotted nodes.
  `/nodes/<id>` gained: days seen of the last 7, best/typical (mean) SNR
  and hops over 7 days, packets 7d/all (from `node_daily` sums), position
  status (known/unknown, precision bits, distance/bearing when the
  observatory is set), last battery, a 30-day inline-SVG sparkline of
  packets/day and best SNR/day, and an events list (first discovered, the
  day best hops first improved, the strongest SNR day).
  `/topology` draws a radial ring layout from `links` (observatory at
  centre, direct neighbours on an inner ring, 2-hop nodes -- derived from
  `node_daily`, not `links` -- drawn unattached on an outer ring since
  Meshtastic never reports which neighbour relayed them), plus tables:
  most frequently heard direct neighbours (7d), stable neighbours (seen
  >=5 of 7 days), and links appeared/disappeared in the last 24h (a
  link last heard 24-48h ago and not since is treated as "disappeared" --
  a best-effort inference, not proof the node is gone).
  `/propagation` shows, per direct node, a best/mean-SNR-by-day table and
  heat strip (last 14 days), and an "unusual distance" section flagging
  days where the furthest direct distance (from `node_daily.last_lat/lon`)
  exceeded 1.5x the 14-day median -- rendered as a note when positions or
  the observatory location are unavailable. No weather correlation (TODO,
  per the phase-2 ruling that propagation stays SNR/RSSI/distance-only
  for now).
  Nav gained Map/Topology/Propagation links (also added as small links on
  `/`); see README's "Local dashboard" section for the full page list,
  the no-tiles rationale, and the labelling caveats.
- **Local read-only dashboard (batch 3a, new `[web]` config section).**
  An optional, no-authentication web dashboard (`meshgarden/web.py`
  + `web_templates.py`), disabled by default (`[web] enabled = false`),
  bound to `127.0.0.1:9109` and refusing to start on any non-loopback
  `listen` address unless `[web] allow_lan = true` is explicitly set
  (logged as a WARNING). Served from a daemon thread via stdlib
  `http.server.ThreadingHTTPServer`; every request opens its own
  short-lived read-only SQLite connection
  (`database.connect(path, read_only=True)`), so it can never block the
  DB worker thread or write anything -- every method but `GET` is `405`.
  Seven server-rendered pages, one shared layout (dark/light via
  `prefers-color-scheme`, no external assets, no JavaScript): `/` (today's
  counts, best SNR, 24h activity bars), `/health` (worker heartbeats,
  radio identity, storage/backup status, drop counts, moderation queue),
  `/traffic` (24h per-hour table, portnum mix, top senders, MQTT/RF
  split), `/guestbook` (paginated visible entries plus the moderation
  queue), `/records` (today/7d/all-time bests, book/wordle aggregates),
  `/digest` and `/digest/<YYYY-MM-DD>` (existing digest files, path
  strictly validated), and `/nodes`/`/nodes/<node-id>` (nodes seen in the
  last 7 days and one node's `node_daily` history). Connectivity/radio/
  worker/backup/storage fields are read from the same status file
  `mgarden status` already reads (never computed live), so a stalled or
  absent daemon degrades a page to "stale" rather than breaking it; every
  database-sourced string is HTML-escaped; every response carries
  `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline';
  img-src data:`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. `main.py`'s
  `Daemon.start`/`stop` gained a two-line hook to start/stop the server
  thread; see README's new "Local dashboard" section for the SSH
  port-forward tip and the full page/config reference.
- **Raspberry Pi installer** (`scripts/install-rpi.sh`, `scripts/uninstall-rpi.sh`):
  idempotent, `--dry-run`-capable install/uninstall for Debian
  12/bookworm-compatible hosts. Runs Meshgarden in place from the checkout
  under a dedicated `meshgarden` system user; config at
  `/etc/meshgarden/config.toml`, state under `/var/lib/meshgarden/`. Installs
  missing `apt` packages, installs `uv` for the repo owner if needed, runs
  `uv sync --frozen --no-dev`, auto-detects the radio's serial device and
  (read-only, over USB only) its node id, and installs/enables/starts a new
  `systemd/meshgarden.service` unit. Never overwrites an existing config or
  state (`--force-config` backs up first). The prior
  `systemd/meshtastic-observatory.service` unit and manual install recipe
  remain in the repo, documented as legacy, for existing installs. See
  README's new "Raspberry Pi installation" section.
- **Raspberry Pi installer `--web-listen` option**: enables the read-only web
  dashboard on the local network (or just loopback). When given and the config
  is being (re)generated (first install or `--force-config`), writes `[web]`
  section with `enabled = true`, `listen = ADDR` (IPv4/IPv6 literal or
  `0.0.0.0`), `port = 9109`, and `allow_lan = true` iff ADDR is not loopback
  (`127.*`, `::1`, `localhost`). If config exists without `--force-config`,
  prints the exact manual config keys instead. Final summary displays the
  dashboard URL and a warning: read-only but unauthenticated, expose only on
  a trusted LAN or behind a firewall.
- **Observatory data model (schema v5)**: structured, text-free capture of
  `POSITION_APP` (lat/lon/altitude/precision, converted from the radio's
  fixed-point encoding), `NODEINFO_APP` (hardware model, role, `isLicensed`,
  and a public-key-presence flag -- never the key itself), and
  `TELEMETRY_APP` `deviceMetrics` (battery, voltage, channel utilization,
  air-time-tx, uptime). New tables `node_positions` (append-only, 7-day
  retention alongside observations), `node_daily` (per-node-per-day rollup:
  packets, direct packets, best/typical SNR and hops, last position; 365-day
  retention, configurable via `[retention] node_daily_days`), `links`
  (inferred-direct neighbour observations, `kind` fixed to
  `'inferred_direct'`; 30-day retention since last seen, configurable via
  `[retention] links_days`), and `local_radio_samples` (our own node's
  passively-observed radio metrics, 7-day retention); `nodes` gains
  `hw_model`/`role`/`last_position_*`/`last_battery` snapshot columns. New
  `[observatory]` config section (`latitude`, `longitude`,
  `enabled_position_capture`) and `Collector.haversine_km` power two new
  reply fields, both omitted (not `n/a`) when the observatory location is
  unset or no relevant position is known: `GARDEN`'s "furthest direct" and
  `RECORD`'s "furthest observed" (with the observed node's id tail). See
  README's "Observatory data model" section for the full field-by-field
  breakdown and precision caveats.
- **`mgarden guestbook approve <entry-id>`**: reverses the SIGN banned-word
  guard's auto-hide on one entry (`visible=1`, `moderation_reason`
  cleared), via new `Guestbook.approve`. An entry that does not exist, is
  already visible, or was hidden directly by an operator (`guestbook hide`,
  no moderation reason) is left untouched and reported as such -- exit `3`
  in every one of those cases; `guestbook review`'s own output is
  unchanged.
- **Default banned-word list**: `[moderation] banned_words_file = "default"`
  now loads a conservative, ~65-term English profanity/slur list packaged
  with Meshgarden (`meshgarden/data/banned-words.default.txt`,
  loaded via `importlib.resources`), instead of requiring an
  operator-supplied file from day one. Whole-word matching (unchanged)
  keeps it from false-positiving on ordinary words that merely contain a
  listed term (e.g. "class", "shitake").
- **Daily digest**: new `digest.py` module (`build_digest`, `format_digest`,
  `write_digest_file`, `prune_digests`) computes an aggregate-only summary
  of one local calendar day -- packets, distinct senders, new nodes,
  busiest local hour, guestbook signatures/held, reply outcomes, commands
  by type, BOOK/WORDLE solves -- from existing tables only; never a node id
  or message text. Exposed via `mgarden digest [--date YYYY-MM-DD] [--json]`
  (default: yesterday in `[service] timezone`). Persisted digest files
  (`<database dir>/digest/<day>.json`, 90-day retention) are written by
  `write_digest_file`/`prune_digests` -- see "Digest scheduling hook" below
  for the daemon-side midnight wiring.
- **Worker health.** Each of the four bounded workers (DB worker, TX
  worker, radio reconnect thread, and the periodic maintenance tick) now
  updates a thread-safe heartbeat on every loop pass. `mgarden status`
  (and its `--json` output) and the daemon's status file report each
  worker's `age_seconds`/`alive`/`stalled` (`stalled` = heartbeat age past
  a fixed per-worker threshold: 30s for DB/TX, `max(reconnect backoff cap,
  60s)` for radio, 3x the maintenance interval for maintenance); `mgarden
  doctor` reports any currently stalled worker from the status file. A new
  `meshgarden_worker_heartbeat_age_seconds{worker=...}` gauge exposes the
  same ages, and a bounded WARNING (one per stall transition, never
  repeated every tick) is logged the moment a worker stalls, with an INFO
  on recovery.
- **systemd watchdog integration (optional, no new dependency).** Under a
  `Type=notify` unit, the daemon sends `READY=1` once at startup and
  `WATCHDOG=1` every `WATCHDOG_USEC/2` from its main loop -- but only
  while no worker is stalled, so systemd's own `WatchdogSec` restarts the
  unit exactly as if the process had hung. Implemented as a ~20-line
  `AF_UNIX SOCK_DGRAM` `sd_notify`, not the optional `sdnotify` package.
  The shipped unit file now sets `Type=notify` and `WatchdogSec=120`.
- **Quota hint on MENU/WHOAMI.** The top-level `MENU`/`?` reply's last
  line and the `WHOAMI`/`SIGNAL` reply now end with `· quota N/60 today`
  (N = that node's accepted-command count so far today, out of the
  configured `daily_command_quota`) -- both stay within the 180-byte
  reply budget (the last line is shortened, never the quota hint itself,
  if it would not otherwise fit). `MENU STATS`/`MENU GAMES` are
  unaffected, and a refused command still gets no reply of its own.
  `DailyQuota` gained a read-only `usage(node_id, day)` for this.
- **Digest scheduling hook, now wired to the daily digest.** A new
  `_on_local_midnight` maintenance-tick hook fires exactly once per day
  change (reusing the existing `_last_rollup_day` day-change tracking) and
  runs `Daemon._midnight_tasks: list[Callable[[str], None]]`. Its one
  default entry builds and writes the previous day's digest
  (`digest.build_digest`/`write_digest_file`, using `[service]
  timezone`/`haiku_place`); a broken task never blocks another or the
  maintenance tick itself. `prune_digests` (90-day retention) now runs on
  every maintenance tick alongside the other retention prunes, and the DB
  worker writes a catch-up digest for yesterday at startup if that file is
  still missing and yesterday actually had activity (covers a restart, or
  first start of the day, spanning local midnight). Never a node id or
  message text; any failure is caught and logged rather than raised.

### Fixed

- **Dashboard batch 3b follow-up fixes**: `/nodes/<id>`'s events list no
  longer claims a single-day-old "best hops improved"/"strongest SNR"
  reading as an improvement -- it now reports "first observed" (and, for
  SNR, "only one day of data") until a later day actually beats an
  established baseline; `/map`, `/topology`, `/propagation`, and
  `/nodes/<id>` gained explicit HTML-escaping regression tests (incl. SVG
  `<title>` text) for attacker-controlled node names; the `/map` (fetch
  capped at 2000 most-recently-seen positioned nodes, still displayed
  nearest-300), `/topology` (links/nodes capped at 500 rows), and
  `/propagation` (capped at 200 direct nodes) queries are now bounded so a
  large `nodes` table can't make a page unbounded; and `/map`'s cap note
  now shows the real pre-slice match count (e.g. "Showing the nearest 300
  of 1,204 positioned nodes") instead of the post-slice length.
- **Raspberry Pi installer: `ProtectHome=true` made the shipped unit
  unstartable.** The documented install runs the checkout in place under
  the repo owner's home directory (e.g. `~/meshgarden`), but
  `ProtectHome=true` hides `/home` from the service entirely, so
  `ExecStart`/`WorkingDirectory` could never be found. Changed to
  `ProtectHome=read-only`: the checkout stays visible (never writable),
  state still lives only under `/var/lib/meshgarden` via the existing
  `ReadWritePaths`. `install-rpi.sh` now also pre-flights, as the
  `meshgarden` service user, that `<checkout>/.venv/bin/mgarden` is
  executable and every directory above it is traversable
  (`runuser -u meshgarden -- test -x ...`), failing with an actionable
  `chmod o+x`/move-to-`/opt/meshgarden` message instead of silently
  installing a dead unit.
- **Raspberry Pi installer `--web-listen`: dashboard URL printed even when
  the config wasn't touched.** The final summary used to print the
  dashboard URL whenever `--web-listen` was given, even when an existing
  config was left untouched (no `--force-config`) and `[web]` was never
  actually written -- misleadingly implying the dashboard was live. The
  URL is now printed only when `[web]` was written this run; otherwise
  only the existing manual-edit notice is shown.
- **Raspberry Pi installer, minor hardening**: `--web-listen`'s address
  validation now parses with Python's `ipaddress` (via the repo venv when
  present) instead of a loose regex, so values like `foo:bar` (which
  merely contain a colon) are correctly rejected; the nested
  `uv`-installer `curl | sh` pipeline now runs under `set -o pipefail` so
  a failed download can't be masked by a successful `sh`; the state
  directory's ownership is only `chown -R`'d on first creation (a
  pre-existing `/var/lib/meshgarden` gets a non-recursive `chown` on
  subsequent runs); the rendered config/unit temp files are no longer
  generated at all under `--dry-run` (a placeholder path is used in the
  printed plan instead); and the `runuser -c` command strings are now
  built with `printf %q` instead of manual quoting.
- **MENU/WHOAMI quota hint vs. a configured `max_response_bytes` narrower
  than 180**: `with_quota_hint` was called without `max_bytes`, so it
  budgeted the quota suffix against commands.py's own internal 180-byte
  default rather than the operator-configured
  `service.max_response_bytes`; the later generic
  `truncate_utf8(..., max_response_bytes)` could then cut the suffix
  itself (e.g. `max_response_bytes=140` with a 3-digit quota ending in
  `quota 45/9` instead of `quota 45/999 today`). `max_bytes` is now a
  required keyword of `with_quota_hint`, and both call sites in `main.py`
  pass the configured `service.max_response_bytes` explicitly, so the
  quota-hinted reply is never re-truncated afterwards.
- **`ops.restore_backup`**: an unrelated-but-structurally-valid sqlite file
  (passes `PRAGMA integrity_check` but is not a Meshgarden database) could
  replace the target and only then fail with `no such table: guestbook`,
  losing the target's data. The application schema (required tables,
  `user_version` in `1..SCHEMA_VERSION`) is now validated on the *source*
  before any copy; any other unexpected exception before the atomic
  `os.replace` is also caught and reported as a bounded `ok=False` rather
  than escaping, so the target is guaranteed to be left untouched.
- **`ops.restore_backup`, further hardening**: the safety net around the
  copy/swap steps now also covers `os.makedirs`/`tempfile.mkstemp`, so a
  failure there returns a bounded `ok=False` report instead of escaping;
  a new `RestoreReport.leftover_files` records any `.pre-restore` aside
  file that fails to clean up (success path) or move back (failure path)
  as an `OSError`, folded into the reported error rather than silently
  left on disk; and the post-swap read-back (row count for reporting) is
  now guarded too, so a failure there can no longer turn an
  already-successful restore into a raised exception -- it returns
  `ok=True` with `guestbook_row_count=None` and a bounded note instead.
- **`Collector.record` inflated per-node stats on duplicate receptions**:
  the `node_daily` upsert, `links.count` increment, and `node_positions`
  insert all ran for a duplicate reception of the same packet (same
  sender+packet_id within the 600s dedup window) too, double-counting
  those rows beyond genuinely distinct packets. Only non-duplicate
  packets now update `node_daily`/`links.count`/`node_positions`; the raw
  `observations` row is still recorded with `is_duplicate=1` as before.
  Also replaced `snr or 0`/`hops_away or 0` truthiness accumulators in the
  `node_daily` upsert with explicit `is not None` checks.
- **`radio.backoff_delays`**: `base * 2**i` overflowed converting a huge
  Python int to float around attempt 1025, raising `OverflowError` in the
  reconnect thread (which then silently died, daemon otherwise still
  running). The exponent is now capped; the reconnect loop also falls back
  to `backoff_max` if delay computation ever raises unexpectedly.
- **Daily command quota vs. request dedup ordering**: the quota was counted
  *before* the dedup check, so a retransmitted/duplicate packet id could
  consume a quota slot even though it never produces a second reply (e.g.
  quota=2 with packet ids 1, 1, 2 yielded only one reply instead of two).
  Dedup (`claim`) now runs first; a quota-refused (but not duplicate)
  request still settles its row to `dropped`/`quota`, same as every other
  refusal.
- **Maintenance rollup vs. prune ordering**: `_run_maintenance` pruned
  expired `processed_requests` rows and then recomputed yesterday's
  `daily_stats` from what remained, so a previously *finalized* day's
  `commands_accepted`/`replies_acked` counters could silently drop (e.g.
  1 → 0) after a restart (which resets `_last_rollup_day` and so
  re-triggers yesterday's rollup). `retention.rollup_day` now refuses to
  recompute a day already marked `finalized`, and the maintenance tick
  rolls up before it prunes.
- **`GUESTBOOK #<id>` with an out-of-range entry id**: an id past SQLite's
  64-bit `INTEGER` range (e.g. `GUESTBOOK #9223372036854775808`) parsed
  fine but raised `OverflowError` binding it into a query; the exception
  escaped into the DB worker's generic handler, which only logs, leaving
  the request stuck `received` forever (invisible to both pruning and
  startup recovery). Such ids are now treated as "not found"; the generic
  exception path settles any still-`received` row to `failed`/
  `internal_error`; and startup recovery now also settles stale `received`
  rows older than the reply TTL.
- **`config.guestbook_expiry_days`**: nonzero values were accepted but
  never wired to any expiry logic (a silent no-op). Per spec, guestbook
  entries are kept until the operator deletes them; only `0` is now
  accepted, and any other value raises `ConfigError` at load time.
- **`ops.restore_backup` (overwrite)**: stale `-wal`/`-shm` sidecars of the
  target were deleted *before* the final `os.replace`; if that replace then
  failed (e.g. disk full), any data committed only to the target's own WAL
  (never checkpointed) was permanently lost even though the restore
  correctly reported `ok=False`. The existing target and its sidecars are
  now moved aside (never deleted) before the swap and only removed once
  the swap actually succeeds; on any failure they are moved back, so a
  failed restore never loses data. A best-effort `PRAGMA
  wal_checkpoint(TRUNCATE)` also folds the target's own WAL first when
  nothing else holds it open.
- **Startup/maintenance recovery of stuck `received` requests**: a restart
  shortly after a request was claimed (well within `reply_ttl_seconds`)
  left that row `received` forever — startup recovery only aged `received`
  rows *older* than the reply TTL, and no in-memory work survives a
  restart to ever move it further; maintenance never touched `received`
  rows either, and retention pruning only deletes terminal states. Startup
  now settles every `received` row unconditionally to `unknown` (never
  resent), and maintenance ages any `received` row older than the reply
  TTL to `unknown` as a safety net, so retention eventually prunes it.
- **Local dashboard: a misconfigured `[web]` no longer aborts the daemon.**
  `Daemon.__init__` called `web.start_web_server` unguarded, so a
  non-loopback `[web] listen` without `allow_lan` (`web.WebRefused`) or a
  port already in use (`OSError`) killed daemon startup entirely --
  contradicting README/CHANGELOG's own claim that the dashboard degrades
  independently. Both are now caught, logged as one bounded ERROR, and
  recorded as a new `web_status` field (`"disabled"`/`"running"`/
  `"refused"`/`"failed"`) in `status_snapshot()`, the status file, and
  `mgarden status`; every other worker (radio, DB, TX) starts normally
  either way.
- **`/nodes` had no row cap**: unlike `/guestbook`, it rendered every node
  seen in the last 7 days in one unbounded query/table. It is now paginated
  the same way (`LIMIT 200` + `?page=`, newer/older links); `_parse_page`
  also now caps `page` to `1..10000` (previously unbounded on the high end),
  folding anything out of range back to page 1, consistent with how an
  unparseable value was already handled.
- **`web.py` used the deprecated `datetime.utcfromtimestamp`** (4 call
  sites: `_fmt_utc`, the guestbook entry timestamp, and the nodes-list
  first-/last-seen columns), which raises `DeprecationWarning` under
  Python's planned removal. Replaced with timezone-aware
  `datetime.fromtimestamp(x, tz=timezone.utc)` (same rendered output); a
  package-wide grep found no other `utcfromtimestamp`/`utcnow` call sites.
- **Dashboard `HEAD` requests returned `501`**: `_DashboardHandler` wrote
  its response body conditionally on `self.command != "HEAD"` but never
  defined `do_HEAD`, so the branch was dead and every `HEAD` request fell
  through to `http.server`'s default "Unsupported method" response. `HEAD`
  now reuses `do_GET` (headers only, per HTTP semantics already coded for).
  Also merged `_page_traffic`'s two identical 24h `observations` scans
  (one via `_hourly_activity`, one for the unique/direct breakdown) into
  one pass (`_hourly_traffic_detail`).

### Known limitations / not in this release

- **MeshCore backend**: not implemented; the transport boundary
  (`RadioAdapter`) is ready for it, but there is no second backend yet
  (TODO, tracked in
  the phase 2 backlog (maintained outside the repository))).
- **Weather import**: not implemented (TODO); `/propagation` stays
  SNR/RSSI/distance-only.
- **Hardware tests of the newer commands** (two-level `MENU`, the games'
  leaderboards, digest, worker-health/watchdog surfaces, the dashboard)
  have not yet been run against real radios.
- **Raspberry Pi installer** has not yet been exercised on a real
  Raspberry Pi (tested via `--dry-run` and the install-script test suite
  only).
- **Manual acceptance checklist items 3 and 6** remain unexercised against
  real hardware (DM to a third node; reconnect-after-unplug) -- see the
  manual hardware acceptance checklist.
- **One restore test failed only inside two sandboxed worktrees** (passes
  on `main`); treated as environment-sensitive (WAL/checkpoint timing
  under that sandbox), not a product defect.
- **`--install-dir`** (Raspberry Pi installer) is currently a no-op.

## [1.0.0rc1] - 2026-09-13

First release candidate. DM-only Meshtastic guestbook and network
observatory, with a MeshCore-ready transport boundary.

### Added

- **DM-only guestbook**: `MENU`/`?`, `INFO`, `SIGN <msg>`, `GUESTBOOK`
  (with pagination and `#<id>` lookup), `STATS`, `WHOAMI`/`SIGNAL`,
  `GARDEN`, `RECORD`, `WEEK`, and `UNSIGN`. Every reply is a direct
  response to an eligible, explicit DM — no channel broadcast can ever
  cause an application reply (the central invariant, enforced at the send
  boundary and exercised across every channel index).
- **Games**: `ICHING` (a daily hexagram, stable per sender/day), `HAIKU`,
  `BOOK` trivia (27-title pool, prompt/guess flow with first-solve/
  rare-catch/nth-today leaderboard reply tiers), and `WORDLE` (daily,
  per-player guess state).
- **Moderation and quotas**: a banned-word guard (silent to the sender —
  a match is stored hidden with a bounded moderation reason, never
  discarded), per-sender command/sign daily quotas, sign cooldown and
  identical-text suppression, and blocklist management.
- **Observability**: an in-process metrics registry (Prometheus
  endpoint), a periodic status file consumed by the CLI, retention
  (bounded pruning of observations/nodes/requests/aggregates), and
  scheduled database backups.
- **Operator CLI** (`mgarden` alias, same entry point as
  `meshtastic-observatory`): `status`/`db-stats`/`requests`, `guestbook
  list`/`hide`/`delete`/`review`, `block`/`unblock`, `backup`, `books`,
  `wordle`, `run [--debug]`, plus `tail`, `export`, `restore`, and
  `doctor`.
- **`run --debug`**: an opt-in DM/reply exchange trace (stderr and a
  dedicated rotated file, `mgarden tail`'s target), gated to the operator
  and never baked into the systemd unit; the trace file is created
  owner-only (0600), including across rotation.
- **Hardware acceptance suite** (`tests/test_hardware.py`,
  `@pytest.mark.hardware`, gated on real USB devices and never run by
  default): DM-only end to end — every transmission the suite makes is a
  DM, never a broadcast, statically enforced by a packaging guard.
- **MeshCore-ready transport boundary**: the radio adapter and send path
  are structured so a second transport backend can be added later without
  touching the DM-only guarantee or the command/reply logic.
- `mgarden --version` / `meshtastic-observatory --version`, and the
  running version exposed in `status --json` and the periodic status
  file.

### Known limitations

- Two manual-only acceptance checklist items remain unexercised against
  real hardware in this release: **item 3** (DM to a third node — needs a
  second visitor radio) and **item 6** (reconnect-after-unplug, no reply
  burst — needs a physical unplug/replug cycle). See the manual hardware
  acceptance checklist.
- No positions or distance yet in `GARDEN`/`RECORD` (local activity/
  records only); no local-only observatory web UI (dashboard, map, node
  profiles, topology, health, propagation, daily digest) and no MeshCore
  transport implementation yet — tracked in
  the phase 2 backlog (maintained outside the repository)).
