# Operations

The operator runbook for Meshgarden: install and upgrade, daily checks, backups, moderation,
alerting, and a complete CLI reference. Settings live in [configuration.md](configuration.md);
privacy and quota details in [security-and-privacy.md](security-and-privacy.md).

## Contents

- [Install and upgrade](#install-and-upgrade)
- [Two radios (shared garden)](#two-radios-shared-garden)
- [Connecting the radio](#connecting-the-radio)
- [How visitors reach the node](#how-visitors-reach-the-node)
- [Daily checks](#daily-checks)
- [Storage, retention and backups](#storage-retention-and-backups)
- [Moderation](#moderation)
- [LAB tier](#lab-tier-experimental)
- [Radio watchdog](#radio-watchdog-alerting-and-auto-recovery)
- [CLI reference](#cli-reference)

## Install and upgrade

### Raspberry Pi installation

On a Raspberry Pi (or any Debian 12 / bookworm-compatible host with systemd), use
`scripts/install-rpi.sh`. It runs Meshgarden **in place** from the checkout under a dedicated
`meshgarden` system user. Config is at `/etc/meshgarden/config.toml`; state (database, backups,
logs, status, digests) is under `/var/lib/meshgarden/`. It is idempotent and never overwrites an
existing config or any state.

**Fresh install:**

```bash
git clone <repo-url> meshgarden && cd meshgarden
sudo ./scripts/install-rpi.sh
```

The installer:

1. Creates the `meshgarden` user (in `dialout` for serial access only).
2. Installs missing `apt` packages, and `uv` for the repo owner if needed.
3. Runs `uv sync --frozen --no-dev`, adding `--extra meshcore` for MeshCore (see `--transport`
   below).
4. Detects the radio under `/dev/serial/by-id`, falling back to `/dev/ttyACM*` / `/dev/ttyUSB*`.
   If there is more than one candidate, pass `--device PATH`.
5. Generates `config.toml` from `config.example.toml`, and tries to read the radio's node id over
   USB to fill in `expected_node_id` (a read-only config request, never a LoRa transmission).
6. Installs, enables and starts `meshgarden.service`.
7. Installs `mgarden` and `meshtastic-observatory` wrappers in `/usr/local/bin`.

It does not copy the checkout elsewhere, does not touch `/etc/meshtastic-observatory` or the
legacy unit, and never transmits over the mesh.

**Upgrade:**

```bash
cd ~/meshgarden && git pull && sudo ./scripts/install-rpi.sh
```

Re-running syncs dependencies, re-installs the unit and restarts the service. The database and
existing config values are left untouched. `mgarden config-sync` may append keys that a newer
version added, each with its default and comment (`--no-config-sync` skips this).

A plain `git pull` does not install new Python dependencies, and systemd launches
`.venv/bin/mgarden` directly. If you update without the installer, run
`uv sync --frozen --no-dev` yourself from the checkout (add `--extra meshcore` for MeshCore).
The SUN astronomy library loads only when a SUN command is used; if it is missing, SUN reports
unavailable and logs a hint while everything else keeps working.

**Home directory checkout.** The unit sets `ProtectHome=read-only`, so the `meshgarden` user
must be able to traverse every directory down to `<checkout>/.venv/bin/mgarden`. A checkout under
a `0700` home directory blocks this. The installer checks first and fails with a message. Fix it
with `chmod o+x /home/<user>`, install into `/opt/meshgarden`, or re-run with
`--fix-home-traversal`.

**Reset or remove:**

```bash
sudo ./scripts/install-rpi.sh --reset-data [--yes]   # wipe database and state, keep config and backups
sudo ./scripts/uninstall-rpi.sh                      # remove the unit only
sudo ./scripts/uninstall-rpi.sh --purge              # also delete config and state (asks first)
```

**Limitations:** requires systemd and a Debian-based `apt`. aarch64 is the tested target (x86_64
works with a warning). No udev rule is installed (see [Connecting the radio](#connecting-the-radio)).
Radio auto-selection only works when exactly one serial candidate is present.

### Installer options

Both scripts also accept `--dry-run` and `--verbose`; run `--help` for the current list.

| `install-rpi.sh` option | Effect |
|---|---|
| `--user USER` | Repo-owning, non-root user that runs `uv sync` (default: `$SUDO_USER`). Not the service account. |
| `--device PATH` | Serial device for `[radio] device`. Required if more than one candidate is found. |
| `--transport meshtastic\|meshcore` | Which optional extra `uv sync` installs. Does not edit `[radio] transport`. Defaults to the existing config's transport, else `meshtastic`, so re-runs keep the MeshCore extra. |
| `--config PATH` | Config file path (default `/etc/meshgarden/config.toml`). |
| `--web-listen ADDR` | Enable the read-only [dashboard](dashboard.md) (IPv4/IPv6 literal or `0.0.0.0`). When generating the config, writes `[web] enabled=true`, `listen=ADDR`, `port=9109`, and `allow_lan=true` unless ADDR is loopback. If the config already exists, prints the keys instead. |
| `--install-dir PATH` | Reserved; currently a no-op (always runs in place). |
| `--no-start` / `--no-enable` | Skip starting / enabling the service. |
| `--skip-apt` / `--skip-uv-install` | Skip package / uv installation. |
| `--force-config` | Replace an existing config, after backing it up to `config.toml.bak.<timestamp>`. |
| `--no-config-sync` | Skip the `mgarden config-sync` step that adds new keys to an existing config. Existing values are never changed either way. |
| `--fix-home-traversal` | If the traversal check fails, `chmod o+x` each ancestor directory that lacks it (execute bit only), printing every change. |
| `--reset-data` | Wipe the database and other daemon state under `/var/lib/meshgarden`, keeping `backups/`. Config is never touched. Asks for confirmation unless `--yes`. |
| `--yes` | Skip the `--reset-data` confirmation. |
| `--debug` | Install the service running `mgarden run --debug`. Not sticky: re-run without it to revert. See [Debug trace](#debug-trace). |

| `uninstall-rpi.sh` option | Effect |
|---|---|
| `--purge` | Also remove `/etc/meshgarden` and `/var/lib/meshgarden` (confirmation unless `--yes`). |
| `--remove-user` | Also remove the `meshgarden` system user. |
| `--yes` | Skip the `--purge` confirmation. |

### System-wide `mgarden` command

The installer puts `mgarden` and `meshtastic-observatory` (same entry point) in
`/usr/local/bin`. Each is a small wrapper that runs `<checkout>/.venv/bin/mgarden`. If you do not
pass `--config` and `/etc/meshgarden/config.toml` exists, the wrapper adds it for you, so
`mgarden status` works from any shell. Re-running the installer regenerates the wrappers;
`uninstall-rpi.sh` removes them.

- **Read-only commands** (`status`, `db-stats`, `requests`, `guestbook list`, ...) work as the
  `pi` user if the state directory is readable.
- **Write commands** (`guestbook hide/approve`, `block`, `quota reset`, `backup`, `restore`, ...)
  must run as root or the `meshgarden` user, for example `sudo mgarden backup`.

### Manual install (legacy unit)

The manual recipe still works and is the reference for the legacy `meshtastic-observatory.service`
unit. New installs should use the installer above.

```bash
# Service user with its own private group; dialout is only a supplementary group (serial access),
# so the 0700 state directory is not group-readable by other dialout members.
sudo useradd --system --user-group --home /var/lib/meshtastic-observatory \
    --shell /usr/sbin/nologin meshobs
sudo usermod -aG dialout meshobs

# Application (adjust the source path as needed).
sudo mkdir -p /opt/meshtastic-observatory
sudo cp -r . /opt/meshtastic-observatory
cd /opt/meshtastic-observatory && sudo uv sync --frozen

# Config and unit.
sudo mkdir -p /etc/meshtastic-observatory
sudo install -m 0640 -o meshobs -g meshobs config.example.toml \
    /etc/meshtastic-observatory/config.toml   # then edit the placeholders
sudo install -m 0644 systemd/meshtastic-observatory.service \
    /etc/systemd/system/meshtastic-observatory.service

sudo systemctl daemon-reload
sudo systemctl enable --now meshtastic-observatory
```

The legacy unit (labeled **LEGACY** in the file) runs as `meshobs` with `dialout` only as a
`SupplementaryGroups=` entry, a `0700` `StateDirectory`, `ProtectSystem=strict` and
`Restart=on-failure`. It exposes nothing beyond the loopback metrics endpoint. Existing installs
keep working unchanged.

**systemd watchdog (`Type=notify`, `WatchdogSec=120`).** The daemon sends `READY=1` at startup and
`WATCHDOG=1` every `WATCHDOG_USEC/2`, but only while no worker is stalled (see "Worker health" in
[configuration.md](configuration.md)). When a worker stalls, it stops pinging and systemd
restarts the unit as if the process had hung. It needs no extra dependency, and does nothing under
a unit without `NOTIFY_SOCKET` / `WATCHDOG_USEC`.

## Two radios (shared garden)

A MeshCore and a Meshtastic daemon can serve one BBS (guestbook, news, mail, games) from a single
Pi. The default instance (`meshgarden.service`, `/etc/meshgarden/config.toml`) is unchanged; add a
second one with:

```bash
sudo ./scripts/install-rpi.sh --instance mt --transport meshtastic
```

This installs the `meshgarden@.service` template, seeds `/etc/meshgarden/mt.toml` from
`config.example.toml` if absent (an existing file is never overwritten), creates
`/var/lib/meshgarden/mt/`, and enables `meshgarden@mt` without starting it. Both instances run as
the `meshgarden` user and share `/var/lib/meshgarden`.

```
/var/lib/meshgarden/bbs.db                 shared BBS (both daemons)
/var/lib/meshgarden/observatory.db         default instance (MeshCore)
/var/lib/meshgarden/mt/observatory.db      mt instance (Meshtastic)
```

Edit `/etc/meshgarden/mt.toml` (radio device, `expected_node_id`, `transport`, a distinct
`[metrics] port` and `[web] port`, `[backup] directory`) and point both configs at the shared files:

```toml
# /etc/meshgarden/config.toml (default, MeshCore)
[database]
bbs_path = "/var/lib/meshgarden/bbs.db"
peer_path = "/var/lib/meshgarden/mt/observatory.db"

# /etc/meshgarden/mt.toml
[database]
path = "/var/lib/meshgarden/mt/observatory.db"
bbs_path = "/var/lib/meshgarden/bbs.db"
peer_path = "/var/lib/meshgarden/observatory.db"
```

- **Start order.** Start the instance that already holds data first (normally the default one):
  on first start with `bbs_path` set it splits the BBS tables out of its observatory database
  into `bbs.db`. Then start `sudo systemctl start meshgarden@mt`.
- **Watchdog.** `meshgarden-watchdog@.service` / `.timer` are per-instance copies of the radio
  watchdog using `/etc/meshgarden/%i.toml`; install and enable like the default watchdog:
  `sudo systemctl enable --now meshgarden-watchdog@mt.timer`.
- **Backups and restore.** Back up both instances; the BBS is in `bbs.db`. Stop both daemons
  before restoring `bbs.db`.
- **Rollback.** Once the split has happened, rolling back to 1.x requires restoring the
  pre-upgrade backup; 1.x does not know about `bbs.db`.

## Connecting the radio

Plug the service radio into the host over USB. It usually appears as `/dev/ttyUSB0` or
`/dev/ttyACM0`, but those names can change across reboots. Use the stable
`/dev/serial/by-id/...` path as `radio.device` in `config.toml`.

```bash
# Stable aliases for current serial devices:
ls -l /dev/serial/by-id/

# If nothing appears, check that the kernel sees the device and read its vendor/product IDs:
dmesg | tail -20
udevadm info -a -n /dev/ttyUSB0 | grep -E 'idVendor|idProduct|serial'
```

- **Permissions.** The daemon runs as an unprivileged user with its own private group that owns
  the state directory. `dialout` is only a supplementary group, so serial access does not make
  guestbook and observation data readable by other `dialout` members.
- **udev.** No custom rule is needed for a single fixed radio. One is only useful for predictable
  names across several attached serial devices.

## How visitors reach the node

**Meshtastic.** Meshgarden never announces itself: no startup broadcast, no periodic
advertisement, no public-channel discovery message. Visitors learn the node's ID outside the radio
network (a sign at the location, a wiki page, word of mouth) and send it a direct message (DM), not
a channel broadcast.

**MeshCore.** The daemon sends a flood self-advertisement once per connection and then every
`meshcore_advert_interval_seconds`, so other nodes can resolve a path back to it. This is presence
beaconing, not a reply. Visitors can also knock on a channel (see below).

### Before the first DM (Meshtastic)

Meshtastic 2.5+ DMs are encrypted to the recipient's public key, so both radios must have
exchanged node info first. If the app says "recipient needs your key" (or a public-key error):

1. Open the Meshgarden node in the app.
2. Use "Exchange user info" / "Request user info" and wait for the acknowledgement.
3. Send the DM.

The service radio beacons its node info periodically. After you re-flash the radio, visitors may
need to repeat the exchange.

### Manual MeshCore advertisement

```bash
sudo mgarden advert
mgarden --config /path/to/config.toml advert   # custom install, as the daemon's user
```

The command asks the running daemon, over an owner-only `advert.sock` beside the database, to
send a flood advertisement through its existing radio connection. Restart the service once after
installing a version that has this command.

- **Success** means the radio accepted the advertisement, not that another node received it.
- **Cooldown.** The 60-second minimum between advertisements also applies to manual requests,
  including right after startup.
- **Failures** return a nonzero exit code: no daemon, identity mismatch, disconnected radio,
  cooldown, or unconfirmed send.
- **No automatic retry.** After a timeout, check the daemon log before trying again.

### Contact table (MeshCore only)

A MeshCore companion radio holds a fixed number of contacts (350 on the XIAO S3 WIO, firmware
v1.17.1). When it is full, the firmware stops adding new nodes, so the garden cannot decrypt a
newcomer's DMs or attribute their channel messages.

Set `[radio] meshcore_contact_prune_enabled = true` (off by default) to trim the table once per
local day, at or after `meshcore_contact_prune_hour`:

- It removes the least recently heard contacts until `meshcore_contact_target` remain (default
  300, must be below the radio's maximum), at most `meshcore_contact_prune_max_per_day`.
- It never removes nodes that have sent a command or mail, hold a LAB unlock or mail prefs, or
  are repeaters or rooms.
- `mgarden status` shows a `contacts` entry (`count`, `max`, `last_prune_at`, `last_pruned`).
  Prometheus exposes `meshgarden_radio_contacts`, `meshgarden_radio_contacts_max` and
  `meshgarden_contacts_pruned_total`.

### MeshCore channel interaction

With `[radio] meshcore_channel` set (default `"meshgarden"`), the node listens on that one channel
for a **knock**: a message whose entire body is exactly `!garden` or `!dm` (case-insensitive). The
service replies with one invite DM, pointing at `MENU` / `HELP`, to the contact that the
channel's `"SenderName: "` convention resolves to. This is the one exception to "learn the node's
ID outside the radio network". The knock is rate-limited in either mode.

| `[service] mode` | Channel behavior |
|---|---|
| `dm_only` (default) | The knock is the channel's only active behavior. |
| `channel` | Recognized public, read-only commands also run, and their one response is posted to the same channel. The channel menu lists the supported subset. |

`MAIL` / `INBOX` / `REPLY`, `SIGN` / `UNSIGN` / `POST`, game state, LAB unlocks and personalized
`ME` / `CHECK` / `PATH` / `NEW` stay DM-only. Channel messages have no verified sender, so channel
responses are never retried and cannot reach private content. Set `meshcore_channel = ""` to
disable channel listening. Threat model:
[security-and-privacy.md](security-and-privacy.md#meshcore-channel-interaction).

## Daily checks

```bash
mgarden status                  # daemon up? radio connected? queue, storage, backup health
mgarden doctor                  # identity, radio device, database, disk, backups, timezone, ports
mgarden digest                  # yesterday's aggregate summary
mgarden requests --limit 20     # what visitors asked recently
sudo systemctl status meshgarden
journalctl -u meshgarden -f
```

- **`status`** reports `radio_disconnected_seconds` (`null` while connected). A sustained outage
  also logs one ERROR to the daemon log.
- **`doctor`** exits `1` if any check fails. A taken metrics port counts as ok when the status
  file shows the daemon itself is alive.
- **Read-only commands** (`requests`, `guestbook list`, `books`, `wordle`, `digest`, `status`,
  `db-stats`) never transmit and are safe while the daemon is writing. Timestamps are local time
  (ISO 8601, in `[service] timezone`).

Example output:

```text
$ mgarden digest
Meshgarden — 2026-09-13 (Potrero): 118 packets · 9 nodes (2 new) · busiest 20:00 · 3 signatures (1 held) · replies 12 acked / 1 failed / 0 expired / 0 unknown / 0 submitted
Commands: GUESTBOOK 3 · SIGN 5 · STATS 2
Games: 2 book solves · 3 wordle solves

$ mgarden requests --limit 3
2026-09-13T09:14:02-07:00  !a1b2c3d4  SIGN         acked      -                 1234
2026-09-13T09:12:47-07:00  !e5f60718  GUESTBOOK    acked      -                 1230
2026-09-13T09:10:15-07:00  !a1b2c3d4  STATS        dropped    queue_full        -

$ mgarden guestbook list --limit 2
#42 2026-09-13T09:14:02-07:00  !a1b2c3d4  c3d4: Hello from the valley!
#41 2026-09-13T08:03:11-07:00  !e5f60718  Rae: Passing through, great signal here
```

### Daily digest

The digest is aggregate-only: bounded counts built from existing tables, never a node id, node
name, or guestbook/DM text. The daemon also writes it to `<database dir>/digest/<day>.json`
(owner-only, mode `0600`) once per local day just after midnight, for the day that just ended.

- **Missed midnight.** If the daemon was not running, it writes yesterday's file at startup, as
  long as the file does not exist and yesterday had activity.
- **Retention.** Files are kept for 90 days and count toward the storage cap.

### Debug trace

`mgarden run --debug` prints every DM's raw text, sender, recipient and parsed command to stderr
and to a small rotated, owner-only (`0600`) file under the state directory's `logs/` that
`mgarden tail` follows. A `LAB_UNLOCK` message is logged as `<lab unlock redacted>`, never the
word. Nothing goes to the daemon log, database, metrics or status file.

- **Operator opt-in only.** Do not leave it on for an unattended deployment, and do not run it
  or `mgarden tail` on a shared terminal.
- **systemd.** Prefer `install-rpi.sh --debug`. The trace's stderr lands in the journal and stays
  there, so run `journalctl --vacuum-time=<duration>` when you are done troubleshooting.

## Storage, retention and backups

| Data | Default retention |
|---|---|
| Raw observations, observatory-only node records | 7 days |
| Node positions, local radio samples | 7 days (same window as observations) |
| Positions shared by DM (`dm_positions`) | Until deleted with `mgarden positions delete` |
| Daily aggregate statistics | 90 days |
| Daily digest files | 90 days |
| Per-node-per-day observatory rollups (`node_daily`) | 365 days |
| Inferred-direct link observations (`links`) | 30 days since last seen |
| Guestbook entries | Until an operator hides or deletes them (no automatic expiry) |
| Database, backups, logs and digests combined | Capped at 1 GiB (configurable); pressure reported at 80% and 95% |

**Backups** run daily through SQLite's online backup API (safe while WAL is active; copying only
the live `.db` file is not). Each is written atomically and the newest 7 are kept. A backup
refuses to start if it would exceed the storage cap or the destination is nearly full. Failures
never crash the daemon and show as `backup_healthy` in `status` and metrics.

> **Deleted content may survive in a retained backup until that backup rotates out.** SQLite
> `DELETE` and removing a backup file are not forensic secure erasure; bytes may stay recoverable
> on the storage media until overwritten.

### Restore

```bash
# 1. Stop the daemon (never restore over a live database).
sudo systemctl stop meshtastic-observatory

# 2. Copy the backup into place.
cp /var/lib/meshtastic-observatory/backups/observatory-20260101T000000Z.db \
   /var/lib/meshtastic-observatory/observatory.db

# 3. Start the daemon; it applies migrations idempotently.
sudo systemctl start meshtastic-observatory
```

`mgarden restore <backup-path> [--overwrite] [--yes]` does step 2 for you and refuses an existing
target without `--yes`. To check a backup without touching the live database, point a throwaway
`config.toml` (different `database.path`) at a copy and run `mgarden db-stats`, or open it with
`sqlite3`. (On a Pi install the service is `meshgarden` and state is under `/var/lib/meshgarden`.)

## Moderation

Moderation is local only and never available over radio.

```bash
mgarden guestbook list [--hidden]       # find entry ids
mgarden guestbook hide <entry-id>
mgarden guestbook delete <entry-id>
mgarden guestbook review [--limit N]
mgarden guestbook approve <entry-id>
mgarden block <node-id>
mgarden unblock <node-id>
mgarden quota show [<node-id-or-exact-name>] [--day YYYY-MM-DD]
mgarden quota reset <node-id-or-exact-name> [--day YYYY-MM-DD] [--commands-only | --signs-only]
mgarden quota --reset-all
```

### Guestbook entries

- **`entry-id`** is an operator-only handle (the raw table id). Visitor replies never show it, and
  it is no longer advertised in `HELP` / `MENU`; `guestbook list` is how you look one up. A
  visitor's `GUESTBOOK #<id>` still works if they already know an id.
- **Hide vs delete.** `hide` keeps the entry but excludes it from reads. `delete` is permanent.
  Visitors can hide their own newest entry over the radio with `UNSIGN` (still stored, only hidden).
- **Review queue.** `guestbook review` lists entries auto-hidden by the SIGN banned-word guard
  (see [security-and-privacy.md](security-and-privacy.md)). `guestbook approve <entry-id>`
  undoes that auto-hide only; an entry you hid with `guestbook hide` is left alone.
- **Topic boards.** All five `guestbook` actions accept `--topic <name>` (see
  [LAB tier](#lab-tier-experimental)). For `list` and `review` it filters to that topic; without
  it you see the main guestbook only, never "all topics". For `hide`, `delete` and `approve` it is
  a guard: the entry must belong to that topic (or be a main-guestbook entry when `--topic` is
  omitted), otherwise the command refuses and changes nothing.

### Blocking

A blocked sender gets no service replies. Requests are dropped silently, checked again on every
send.

### Quotas

- **`quota show`** prints all stored node/day entries with four counters (`commands`, `signs`,
  `posts`, `mails_sent`). Give a node id for a single-node view, or `--day` to filter the
  all-node listing.
- **`quota reset`** clears one node's daily command/`SIGN` quota early. The default day is today
  in `[service] timezone`. By default it deletes the whole row; `--commands-only` or
  `--signs-only` zeroes just that counter.
- **`quota --reset-all`** deletes every stored quota row for all nodes and days.
- **Targets** can be a node id or a quoted, exact known short/long name. Ambiguous names are
  rejected and the matching ids shown.
- Neither reset changes the daemon's in-memory `SIGN` cooldown. See
  [security-and-privacy.md](security-and-privacy.md#quotas).

## LAB tier (experimental)

`POST` / `READ` / `BOARDS` / `MAIL` are experimental and off by default.

- **Turn it on** by setting `[service] lab_unlock_word` to a word of 6-24 letters or digits.
- **Visitors unlock** their own node once by DMing that exact word as the whole message
  (case-insensitive, no other text).
- **Locked nodes** get silence for those four commands, their `HELP` entries, and
  `MENU LAB` / `MENU BOARDS`, exactly as if they did not exist.
- **The word is never logged, echoed, or printed** by `mgarden lab`, `mgarden doctor` or any other
  CLI output. Only unlocked node ids and unlock times are shown.

```bash
mgarden lab                # list unlocked nodes
mgarden lab --json
mgarden lab revoke <node-id>
```

## Radio watchdog (alerting and auto-recovery)

A dead radio is invisible from inside the daemon: the reconnect thread keeps looping and backing
off correctly, so worker heartbeats look fine and the daemon's own `Type=notify` watchdog has
nothing to trip on. The daemon logs an ERROR and reports `radio_disconnected_seconds`, but a log
line pages nobody.

`mgarden radio-watchdog` closes that gap from outside. `meshgarden-watchdog.timer` runs it once a
minute; it polls the metrics endpoint and sorts the result into three states:

| Poll result | Meaning | Action |
|---|---|---|
| `ok` | `meshgarden_radio_connected` is 1 | Nothing (one recovery alert if an outage was announced) |
| `radio_down` | Gauge is 0: daemon up, radio deaf | Alert, then power-cycle (within the caps below) |
| `unreachable` | Endpoint refused: daemon stopped, crashed or hung | Alert only, **never** a power cycle |

Power-cycling cannot fix a stopped daemon (possibly stopped by you for maintenance), hence the
split.

- **Anti-flap.** Nothing happens until the fault has lasted `MESHGARDEN_WATCHDOG_GRACE_SECONDS`
  (default 300), because the radio re-enumerates on any config change from the phone app. A
  recovery inside the grace period is silent. Escalation is latched: one page per outage.
- **Power cycling** uses `uhubctl` and needs a hub that reports `ppps` (per-port power
  switching). Find your hub and port with `sudo uhubctl`:

  ```text
  Current status for hub 1-1 [2109:3431 USB2.0 Hub, USB 2.10, 4 ports, ppps]
    Port 4: 0103 power enable connect [2886:0059 Espressif Systems seeed-xiao-s3 ...]
  ```

  Then set `MESHGARDEN_WATCHDOG_HUB=1-1` and `MESHGARDEN_WATCHDOG_HUB_PORT=4`.
- **Caps.** `MESHGARDEN_WATCHDOG_MAX_CYCLES_PER_DAY` (default 3) and
  `MESHGARDEN_WATCHDOG_CYCLE_COOLDOWN_SECONDS` (default 900) stop a hardware fault from cycling
  forever. At the cap it pages once more asking for manual intervention. **Set the cap to 0 for
  alert-only behavior.** Cutting power mid-write carries a small risk to the radio's stored state,
  which is why the caps exist.

### Install the watchdog

```bash
# 1. Alert channel. Pick a LONG, unguessable ntfy topic: anyone who knows the name can read it.
sudo install -m 0600 -o root -g root systemd/watchdog.env.example \
    /etc/meshgarden/watchdog.env
sudo nano /etc/meshgarden/watchdog.env        # set MESHGARDEN_NTFY_URL

# 2. Verify the alert path end to end BEFORE relying on it.
sudo mgarden notify-test

# 3. Install and start the timer.
sudo install -m 0644 systemd/meshgarden-watchdog.service \
    systemd/meshgarden-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshgarden-watchdog.timer

# 4. Confirm.
systemctl list-timers meshgarden-watchdog.timer
sudo mgarden radio-watchdog                  # one poll, by hand
journalctl -u meshgarden-watchdog -n 20
```

- **Runs as root** on purpose: `uhubctl` needs root to switch port power, and
  `/etc/meshgarden/watchdog.env` is root-only because the ntfy URL is effectively a secret. It
  holds no open sockets and makes one loopback HTTP request a minute.
- **State** (outage start, whether you were paged, today's cycle count) is in
  `<database dir>/watchdog-state.json`, owner-only, and never contains the alert URL.
- **Exit code.** `radio-watchdog` exits `0` whenever the poll completed, even during an outage.
  The alert is the notification, and a unit failing every minute would bury a genuinely broken
  watchdog in `systemctl --failed`.

### Host reboot pages

The watchdog remembers the kernel boot id. Its first poll after any reboot (clean, power cut or
panic) sends one "host rebooted" page. It says when the host was last seen alive and quotes
previous-boot journal lines about shutdown, reboot, power-off, undervoltage, thermal throttling or
a panic. No clues means the boot ended without warning, most likely a power cut or hard hang.

- The very first poll after installing only records the boot id and does not page.
- Clues need a persistent journal (`/var/log/journal`); without one the page still goes out.

### Alert privacy

Alerts carry operational facts only: durations, counts, which of the three states was seen, and
on a reboot page, systemd/kernel journal lines about the shutdown. Never a node id, node name or
message text. They pass through a third-party server by default; run your own ntfy instance and
point `MESHGARDEN_NTFY_URL` at it if that matters to you.

## CLI reference

```text
mgarden [--config PATH] <subcommand> ...
```

`meshtastic-observatory` is the long-form equivalent. Run `mgarden help [command]` for help.

**General exit codes:** `0` success, `1` runtime failure, `2` usage or config error, `3` target not
found. The table lists per-command exceptions.

### Daemon and health

| Subcommand | Purpose | Exit codes |
|---|---|---|
| `run [--debug]` | Connect to the radio and run the daemon. `--debug` adds the [debug trace](#debug-trace). | `0` clean shutdown, `1` runtime failure |
| `status [--json]` | Connectivity, identity, queue depth, storage, backup health. | `0` ok, `1` database not found |
| `db-stats` | Per-table row counts. | `0` ok, `1` database not found |
| `doctor [--json]` | Operator health checks (identity, radio device, database, disk, backups, timezone, status file, metrics port). | `0` all ok, `1` one or more failed |
| `help [command]` | Full help, or one command's help; an unknown command lists the valid ones. With no command, prints full help to stderr. | `0` ok, `2` unknown or no command |
| `tail [-n N \| --from-start]` | Follow the `run --debug` trace file (exists only while running with `--debug`). `-n N` prints the last N lines first; Ctrl-C exits cleanly. | `0` ok, `1` trace file not found |
| `advert` | Ask the running MeshCore daemon for a flood advertisement. Transmits. | nonzero on any failure |
| `config-sync --template PATH [--config PATH] [--dry-run]` | Add keys present in the template but missing from the config, with the template's default and comment. Never changes an existing value. Prints one `section.key` line per addition; `--dry-run` writes nothing; a real run first backs up to `.bak.<timestamp>`. Used by the installer; rarely run by hand. | `0` ok, `1` validation/read/write failure |

### Reports (read-only, never transmit)

| Subcommand | Purpose | Exit codes |
|---|---|---|
| `requests [--limit N] [--json]` | Recent processed requests, newest first (default 20). | `0` ok, `1` database not found, `2` bad `--limit` |
| `digest [--date YYYY-MM-DD] [--json]` | Aggregate-only daily digest (packets, nodes, busiest hour, signatures/held, reply outcomes, commands by type, BOOK/WORDLE solves). Default date: yesterday in `[service] timezone`. | `0` ok, `1` database not found, `2` bad `--date` |
| `usefulness` | JSON report of how often visitors return and which commands they use. Aggregate. | `0` ok |
| `books [--json]` | All-time BOOK leaderboard, one row per title (shown/guesses/solves). | `0` ok, `1` database not found |
| `wordle [--days N] [--json]` | Daily WORDLE aggregates, newest first (default 7 days: players, solves, average guesses). | `0` ok, `1` database not found |
| `positions [--node <node-id>] [--days N] [--limit N] [--json]` | Positions visitors deliberately shared by DM (`Saved position: ...` / `GPS position: ...`), newest first. Recorded only when `[observatory] dm_position_capture = true`. | `0` ok, `1` database not found, `2` bad `--limit`/`--days` |
| `export [--format csv\|json] [--hidden] [--out PATH]` | Export guestbook entries (default: visible-only CSV to stdout). | `0` ok, `1` database not found |

### Backups

| Subcommand | Purpose | Exit codes |
|---|---|---|
| `backup` | Run a backup now. | `0` ok, `1` failed or refused |
| `restore <backup-path> [--overwrite] [--yes]` | Restore a backup into the configured database; refuses an existing target without `--yes`. | `0` ok, `1` refused or failed |

### Moderation and visitors

| Subcommand | Purpose | Exit codes |
|---|---|---|
| `guestbook list [--limit N] [--hidden] [--json] [--topic NAME]` | Entries, newest first (default 10; visible only unless `--hidden`). | `0` ok, `1` database not found, `2` bad `--limit` |
| `guestbook hide <entry-id> [--topic NAME]` | Hide an entry (kept, excluded from reads). | `0` ok, `3` not found |
| `guestbook delete <entry-id> [--topic NAME]` | Permanently delete an entry. | `0` ok, `3` not found |
| `guestbook review [--limit N] [--topic NAME]` | Entries auto-hidden by the SIGN banned-word guard, newest first. | `0` ok, `1` database not found |
| `guestbook approve <entry-id> [--topic NAME]` | Undo the banned-word guard's auto-hide on one entry. | `0` ok, `3` not found or not auto-hidden |
| `block <node-id>` | Add a sender to the block list. | `0` ok, `2` invalid node id |
| `unblock <node-id>` | Remove a sender from the block list. | `0` ok, `2` invalid node id, `3` not blocked |
| `quota show [<node-id-or-exact-name>] [--day YYYY-MM-DD]` | Stored quota rows with `commands`, `signs`, `posts`, `mails_sent`. Without a node: all days and nodes (`--day` filters). With a node: day defaults to today. | `0` ok, `1` database not found, `2` unknown or ambiguous target |
| `quota reset <node-id-or-exact-name> [--day YYYY-MM-DD] [--commands-only \| --signs-only]` | Reset one node's daily quota (default: today, whole row). "Nothing to reset" is still `0`. | `0` ok, `2` unknown or ambiguous target |
| `quota --reset-all` | Delete all stored quota rows. | `0` ok |
| `lab [--json]` | Nodes that unlocked the LAB tier (`POST`/`READ`/`BOARDS`, plus MAIL when `mail_public=false`): node id and unlock time only. | `0` ok, `1` database not found |
| `lab revoke <node-id>` | Revoke one node's LAB unlock. | `0` ok, `3` not unlocked |
| `positions delete [ID ...] [--range FIRST-LAST] [--node <node-id>] [--from YYYY-MM-DD] [--to YYYY-MM-DD]` | Delete shared positions matching every given filter (AND). At least one filter is required. Dates are local days in `[service] timezone`, both inclusive; ids are the `#N` column of `positions`. | `0` deleted, `2` invalid or missing filter, `3` nothing matched |
| `news add TEXT [--days N]` | Publish a short notice visitors can read until it expires (1-90 days, default 7). Database only; nothing is transmitted. | `0` ok |
| `news list` | List unexpired notices. | `0` ok |
| `news hide <id>` | Hide a notice (kept in the database). | `0` ok |
| `news delete <id>` | Permanently remove a notice (prefer `hide`). | `0` ok |

### Mail

| Subcommand | Purpose | Exit codes |
|---|---|---|
| `mail [--reveal] [--json]` | Pending count overall and per recipient, plus a `hidden` count of moderated notes. `--reveal` also prints every note's sender, recipient and body (hidden ones marked with their reason); for abuse review only. | `0` ok, `1` database not found |
| `mail purge <node-id>` | Delete all mail for a node id, including hidden notes. | `0` ok |
| `mail alias list [--json]` | Active local aliases and their node ids. Never transmits. | `0` ok, `1` database not found |
| `mail alias set <node-id> <alias> [--replace]` | Set an alias locally; `--replace` takes it from its current owner. | `0` set, `2` invalid node or alias, `3` owned without `--replace` |
| `mail alias release <alias>` | Release an alias, keeping its former owner's anti-churn timestamp. | `0` released, `2` invalid alias, `3` not found |

### Watchdog

| Subcommand | Purpose | Exit codes |
|---|---|---|
| `notify-test` | Send one test alert through the watchdog's ntfy path (same notifier, so a pass proves the real path). Never echoes the URL. | `0` sent, `1` delivery failed, `2` `MESHGARDEN_NTFY_URL` unset |
| `radio-watchdog` | One watchdog poll: alert, and optionally power-cycle, if the radio went deaf. Driven by `meshgarden-watchdog.timer`. | `0` poll completed, `1` poll itself failed |
