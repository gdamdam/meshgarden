<h1 align="center">Meshgarden</h1>

<p align="center"><strong>A small LoRa BBS and network observatory. MeshCore first, Meshtastic supported.</strong></p>

<p align="center">
  <a href="https://github.com/gdamdam/meshgarden/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/gdamdam/meshgarden/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="version 1.12.0" src="https://img.shields.io/badge/version-1.12.0-blue">
  <a href="LICENSE"><img alt="GPL-3.0" src="https://img.shields.io/badge/license-GPL--3.0-green"></a>
</p>

One Raspberry Pi, one radio (or two, a Meshtastic and a MeshCore daemon can share one garden). Visitors DM the node to sign the guestbook, post on boards, send
mail, play daily games, or check their signal. The node logs packet metadata, never message
text, and serves a local read-only dashboard.

```text
you  → MENU
node → 🌱 MeshGarden
       NEW NEWS GUIDE
       SIGN <msg> GUESTBOOK
       MENU SOCIAL · STATS · GAMES · DAILY

you  → SIGN hello from the hill
node → 🌱 Signed!
       Thanks! Others read it with GUESTBOOK
```

> Experimental community service. Not an emergency communication service. No uptime guarantee.

## What it does

Every reply goes back the way the request came: a DM gets a DM. On MeshCore, an optional
`channel` mode answers a small read-only command set on one public channel, where everyone can
read both the request and the reply.

Features and reply-size budgets target a MeshCore companion radio. The same daemon also runs on
one Meshtastic node.

The whole BBS fits in one-packet DMs: guestbook, topic boards, a mailbox with memorable aliases,
two games (BOOK, WORDLE) and daily draws (ICHING, HAIKU).

Packet metadata, node activity and signal stats go into SQLite, with a Prometheus endpoint and
an optional dashboard. No login, no external assets, loopback by default.

It can relay NWS Warnings and Watches for configured counties as one-line broadcasts on a public
MeshCore channel. This is off by default; see [configuration](docs/configuration.md#weather-alerts).

Two optional, off-by-default features make outbound HTTPS requests: `FORECAST` (cached NWS forecast)
and RSS/Atom feeds imported into `NEWS`; see [configuration](docs/configuration.md#forecast).
Optional [federation](docs/federation.md) shares selected public boards with peer gardens you trust.

## Visitor commands

Commands are case-insensitive. Every reply is one message back to the requester, at most 180
UTF-8 bytes. Start with `MENU`, then `HELP <cmd>` for one-line usage of any command.

| Command | What it does |
|---|---|
| `MENU` (alias `?`) | Top-level menu, with `SOCIAL`, `STATS`, `GAMES`, `DAILY` sub-pages |
| `HELP <cmd>` | One-line usage for a single command |
| `INFO` | Service name, what `SIGN` publishes, experimental disclaimer |
| `NEW` / `NEWS` / `GUIDE` | Updates since your last visit, operator notices, local MeshCore guide |
| `SIGN <message>` | Publish a guestbook entry, up to 100 characters, 3 per day |
| `GUESTBOOK [page \| #id]` | Latest 3 entries, an older page, or one entry in full |
| `UNSIGN` | Hide your own newest entry and reset your SIGN cooldown |
| `PING` / `CHECK` | One-line signal check, or compare your radio signal with the service |
| `ME` (aliases `WHOAMI`, `SIGNAL`) | Your ID, activity, this request's SNR/RSSI/hops, quota, mail status |
| `PATH` (alias `ROUTE`) | Received hop count and signal, plus the cached MeshCore route |
| `SEEN <node>` / `WHO` | When a node was last heard, or who was heard in the last hour and day |
| `STATS` / `GARDEN` / `RECORD` / `WEEK` | Totals, today's activity, today's records, 7-day aggregates |
| `REPEATER` | Cached battery, uptime, and signal stats for the operator's MeshCore repeater |
| `WX` | Weather alert feed status |
| `BOOK [title \| SCORE]` | Today's emoji book-trivia puzzle, a guess, or mesh-wide scores |
| `WORDLE [guess \| CLUE \| GIVEUP \| SCORE]` | Today's 5-letter word, up to 6 guesses |
| `ICHING` / `HAIKU` | One hexagram per sender per day, or one garden or mesh haiku |
| `MOON` / `SUN` | Phase and illumination, or today's sunrise and sunset, offline |
| `FIND <text>` | Search public guestbook notes (LAB boards too once unlocked); off unless `[find] enabled = true` |
| operator commands | Fixed-text info commands you define with `[[commands.info]]`; listed under `MENU MORE` |

Bare `HELP`, `SOS`, and `EMERGENCY` are not menu words. They get a fixed reply making clear
Meshgarden cannot relay for help.

Full reply text, edge cases, and game rules are in [`docs/games.md`](docs/games.md) and
[`docs/community.md`](docs/community.md).

### Mail

Mail lets visitors leave short notes for each other at the garden, like a shared post box.

- Sending is open. Any visitor can send mail by DM. No LAB unlock is needed.
- Receiving is opt-in. You only get notes after you DM `MAIL ON`, and `MAIL OFF` stops them.
- Notes wait at the garden. A note is held on the garden node until the recipient sends
  `INBOX`, then deleted once delivery is acknowledged. Uncollected notes expire after 7 days.
  Notes travel over the air as encrypted DMs, but the garden operator's database holds them
  while they wait.
- Aliases hide node IDs. `MAIL ALIAS gardenfox` claims a short name, so others can write to
  `@gardenfox` without knowing your node ID. Aliases are 3 to 16 characters and can change once
  a day.
- Senders cannot probe for recipients. The radio offers no list of aliases. A note to an
  unknown alias, a mailbox that is off, or a full mailbox all get the same neutral reply as a
  delivered one, so nobody can use mail to discover who is there.

Mail is DM-only and never works on a public channel. A channel message only carries a display
name, which anyone can fake, so every mail command needs a DM.

| Command | What it does |
|---|---|
| `MAIL <node \| @alias> <msg>` | Send a note by node ID, 4-hex tail, exact name, or alias, 3 per day |
| `MAIL ON` / `MAIL OFF` | Opt in or out of receiving |
| `MAIL` / `INBOX` | Collect pending notes |
| `MAIL ALIAS [name \| OFF]` | Show, claim, or release your alias |
| `REPLY #<id> <msg>` | Reply to a collected note |
| `LINK` / `LINK <code>` / `UNLINK` | Opt-in (`identity_linking`): join your MeshCore and Meshtastic radios so they share one mailbox, LAB unlock and game state |

### Sharing your position

*Experimental and off by default.* Lets a visitor send their location to the garden by DM.

- How to share. Use the MeshCore app's share-location option in a DM to the garden. It sends
  `Saved position: lat, lon` or `GPS position: lat, lon`, and the garden replies once to
  confirm.
- What is kept. Only the coordinates, the sender, and the time. The message text is not
  stored.
- How long. Until the operator deletes them with `mgarden positions delete`. There is no
  automatic expiry.
- Who sees it. Only the operator, with `mgarden positions`. Nothing on the radio or the
  dashboard reads positions back.
- Turning it on. Set `[observatory] dm_position_capture = true`. While it is off, a
  location DM gets no reply and nothing is stored.

### Lab tier

`POST`, `READ`, and `BOARDS` are off by default. Set `[service] lab_unlock_word` and visitors
unlock them once by DMing that word. Set `mail_public = false` to put mail behind the same gate.
See [security and privacy](docs/security-and-privacy.md#lab-tier).

| Command | What it does |
|---|---|
| `POST <topic> <msg>` | Post to a topic wall, 100 characters, 5 per day |
| `READ <topic> [page]` | Read a topic wall, 3 posts per page |
| `READ <topic> NEW` | Oldest unread posts of that wall; repeat to continue (`more: READ <topic> NEW`) |
| `BOARDS` | List topic walls by post count |
| `BOARDS NEW` | Unread posts per board, e.g. `wx:3` |

## Quick start

On a Raspberry Pi or any Debian 12 host, for MeshCore:

```bash
git clone https://github.com/gdamdam/meshgarden.git
cd meshgarden
sudo ./scripts/install-rpi.sh --transport meshcore
```

Then edit `/etc/meshgarden/config.toml`:

```toml
[radio]
device = "/dev/serial/by-id/REPLACE_WITH_YOUR_RADIO"
expected_node_id = "!REPLACE_WITH_NODE_ID"
transport = "meshcore"
meshcore_channel = "meshgarden"   # "" runs strictly DM-only

[service]
mode = "dm_only"                  # or "channel" for public read-only replies
```

```bash
sudo systemctl restart meshgarden.service
mgarden status && sudo mgarden doctor
```

The installer creates a `meshgarden` system user, detects the radio, generates the config,
installs the systemd unit, and puts `mgarden` on your path. For Meshtastic, omit `--transport`.
It is safe to re-run after `git pull`: it never changes an existing value, and only appends
new keys with their defaults.

| Flag | Effect |
|---|---|
| `--web-listen 127.0.0.1` | Enable the local dashboard |
| `--debug` | Run the service with the DM exchange trace (not for unattended use) |
| `--force-config` | Regenerate the config, backing up the old one |
| `--reset-data` | Wipe the database and daemon state. Config and backups are kept |

To develop locally:

```bash
uv sync --extra meshcore
cp config.example.toml config.toml   # edit device, expected_node_id, paths
uv run mgarden --config config.toml run
uv run pytest -q                     # hardware tests deselected
uv run ruff check .
```

The test suite never broadcasts. Every transmission is a DM, enforced by a packaging guard.

## Operating the garden

- The CLI, `mgarden [--config PATH] <subcommand>`, covers `status`, `doctor`, moderation
  (`guestbook`, `block`, `quota`, `mail`, `lab`), `positions`, leaderboards, `backup`, and
  `restore`. Most subcommands take `--json`. Full options and exit codes are in
  [`docs/operations.md`](docs/operations.md).
- `mgarden setup` writes a validated config (single or two-radio) without starting services, and
  `mgarden health-report` prints an aggregate-only health digest (optional daily file and email via
  `[health]`; see operations).
- Configuration is one TOML file, `/etc/meshgarden/config.toml` on a Raspberry Pi install.
  Start from [`config.example.toml`](config.example.toml).
  Every key and default is in [`docs/configuration.md`](docs/configuration.md).
- The dashboard is a read-only web view of what the daemon observed, disabled by default. Enable
  it with `[web] enabled = true`. `listen` must stay on loopback unless you also set
  `allow_lan = true`, otherwise it refuses to start. See [`docs/dashboard.md`](docs/dashboard.md).
- The architecture is one process with a transport boundary for Meshtastic and MeshCore. See
  [`docs/architecture.md`](docs/architecture.md).

## Safety and privacy

- Replies stay in DMs. Meshtastic replies only by DM. MeshCore does the same unless
  `service.mode = "channel"` is set, which posts only the documented read-only subset on a
  public channel that anyone can read.
- Bounded retries. An unacknowledged DM is retried at most three times at 15-second
  intervals. Channel replies are never retried.
- Minimal storage. `SIGN` and `POST` publish explicitly. Mail bodies are kept only until
  delivered, or 7 days if never delivered. Ordinary DMs, game guesses, and public keys are never
  stored. `run --debug` is the one exception. It prints raw DM text to stderr and a local trace
  file for live troubleshooting, so never use it for an unattended service.
- Silent moderation. A `SIGN` or `POST` caught by the banned-word guard is stored hidden,
  with the same acknowledgement as any other entry.
- Quotas with polite notices. Cooldown and quota refusals get one short private notice,
  throttled per sender. Notices consume no quota and have no retries.
- Names are labels, not identity. Only the numeric node ID is used for rate limiting,
  blocking, and dedup. Channel sender names are forgeable.
- Packet headers are cleartext mesh-wide. Sender, recipient, hop count, and timing are
  visible to anyone on the mesh, including public MQTT observers. DM payloads are encrypted, so
  an observer sees that a node talked to the service but not what the DM said. Messages on a
  public channel are readable by everyone.

The full model is in [`docs/security-and-privacy.md`](docs/security-and-privacy.md).

## Project status

Tested on Python 3.13 (requires 3.11+), `meshcore>=2.3.11,<3` (optional extra), Meshtastic
client `2.7.11`. Service radio is a Seeed XIAO ESP32S3 & Wio-SX1262 Kit, visitor radio a Seeed
Wio Tracker L1 Pro, both on firmware `2.7.26.54e0d8d`. MeshCore is in live field use, but a
repeatable hardware matrix is still incomplete for both transports.

## Documentation

| Document | Covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Threads, send path, state machine, transport boundary |
| [`docs/configuration.md`](docs/configuration.md) | Every config key, default, and validation rule |
| [`docs/operations.md`](docs/operations.md) | Deployment, systemd, backups, moderation, CLI details |
| [`docs/dashboard.md`](docs/dashboard.md) | Every dashboard page and its security model |
| [`docs/security-and-privacy.md`](docs/security-and-privacy.md) | Identity, storage rules, quotas, LAB tier, LAN exposure |
| [`docs/games.md`](docs/games.md) | ICHING, HAIKU, BOOK, WORDLE rules and leaderboards |
| [`docs/community.md`](docs/community.md) | NEW, NEWS, guides, mail replies, CHECK |
| [`docs/TODO.md`](docs/TODO.md) | Deferred work and hardware acceptance |

## License

[GPL-3.0-only](LICENSE). Copyright (c) 2026 Meshgarden contributors.
