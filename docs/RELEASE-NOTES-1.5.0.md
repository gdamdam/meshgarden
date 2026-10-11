# Release notes: 1.5.0

Operator-facing notes for upgrading to 1.5.0. `1.3.0`, `1.3.1`, and `1.4.0`
were never given their own release-notes file — see their sections in
[`CHANGELOG.md`](../CHANGELOG.md) for the radio watchdog/`notify-test`,
`QUOTE`/`FNORD`, and `/api/daily.json` changes that shipped in between.
This file covers `1.5.0` only: a second radio transport, MeshCore, running
alongside the existing Meshtastic one.

**Status: experimental.** Not for emergencies; no uptime guarantee.

## If you run Meshtastic today: no action needed

**Nothing changes for an existing Meshtastic install.** `[radio] transport`
defaults to `"meshtastic"`, exactly as before; every command, reply,
quota, rate limit, and the DM-only guarantee are byte-for-byte identical
to `1.4.0` (the one operator-visible change in this release, the CLI
whitespace widening below, applies equally to a Meshtastic install — it's
not MeshCore-specific). You do not need to touch `config.toml`, and a
plain `git pull` + restart (or re-running `install-rpi.sh`) is the entire
code-level upgrade — **though we recommend `mgarden backup` first**: this
release adds a database migration (a new `channel_knocks` table) that
runs unconditionally on every install, Meshtastic included, and the
migration path is one-way, with no guard against downgrading the code
afterward. The new `meshcore` package is an optional dependency: it is
not required, it is never imported unless `[radio] transport = "meshcore"`
is actually configured and the daemon starts, and a Meshtastic-only
install works normally whether or not it is present. Configuring
`transport = "meshcore"` without it installed fails with a clear,
actionable message rather than an import error.

## What's new: a MeshCore radio backend

Meshgarden can now run the same bulletin board — the same commands, the
same eligibility rules, the same quota/rate limiting, and the same single
guarantee that the service transmits nothing but a direct message to the
sender of an eligible trigger — over a MeshCore companion radio instead of
a Meshtastic one. Pick the transport per install; the two cannot run at
once in this release (see "Not in this release" below).

### Running on MeshCore

1. Install the optional extra: `uv sync --extra meshcore` (or `pip install
   'meshgarden[meshcore]'`). A Meshtastic-only install has no reason to
   run this and can skip it.
2. Set, at minimum:

   ```toml
   [radio]
   device = "/dev/serial/by-id/..."   # the MeshCore radio's serial port
   expected_node_id = "!xxxxxxxx"
   transport = "meshcore"
   meshcore_channel = "meshgarden"    # "" disables channel listening entirely
   ```

3. `mgarden doctor` gained a `meshcore` check: it reports whether the
   `meshcore` package is installed and which channel/autoadd settings are
   in force (it never connects to the radio itself).

See [`docs/configuration.md`](configuration.md) for every `meshcore_*`
key (reconnect backoff, the reply byte budget, the knock cooldown and
concurrency cap, and the opt-in firmware `autoadd_config` write) and its
default.

### The channel knock — and why it's this narrow

On MeshCore only, the service listens on one configured channel
(`[radio] meshcore_channel`, default `"meshgarden"`) for exactly one
thing: a message whose entire body is `!garden` (case-insensitive,
nothing else). **The garden never transmits on that channel — not that
reply, not an acknowledgement, not a rate-limit notice, nothing, ever.**
A recognized knock produces exactly one outcome: a single direct message
back to the resolved sender, inviting them to `MENU`/`HELP`, rate-limited
to once per `meshcore_knock_cooldown_seconds` (default 24h) **per
resolved recipient**. It cannot run a command — not `HELP`, not `MENU`,
nothing reaches the command dispatcher this way — and an unresolvable
claimed sender is dropped silently, never answered on the channel.

This is deliberately this narrow because a MeshCore channel message
carries **no verified sender identity** — no public key, no key prefix,
only a free-text display name the sending client adds, matched against
whichever contacts the service already knows. Anyone on the channel can
type anyone else's name. Because that "sender" is unauthenticated, the
one thing it's allowed to trigger is bounded to a single, cheap,
per-recipient-rate-limited DM, so a forged name costs an attacker at most
one bounded reply per recipient per day and never runs a command under a
borrowed identity. Meshtastic has no channel-trigger concept at all and
is unaffected.

See [`docs/security-and-privacy.md`](security-and-privacy.md#meshcore-channel-knock)
for the full treatment, including the stated limitation: a sustained
burst of forged knocks (an attacker can mint a fresh contact identity per
knock) can still cost real reply throughput and airtime even though it
never produces more than the one bounded DM per victim — a deliberate
trade-off, disclosed there rather than silently accepted. If you don't
want this exposure at all, set `[radio] meshcore_channel = ""` — it
disables channel listening (and therefore knocking) entirely and runs
MeshCore strictly DM-only, exactly like Meshtastic.

### Operator-visible CLI change

`mgarden block`/`unblock`/`lab revoke`/`quota reset`/`quota show`/`mail
purge` now **tolerate surrounding whitespace** in their `<node>` argument
(e.g. `mgarden block "  !aabbccdd  "` used to exit 2 as an invalid node
id; it's now accepted). This is a widening, not a tightening: a
genuinely malformed argument, whitespace-padded or not, still exits 2
with the same message. It fell out of routing these six subcommands
through the same node-id parser both transports already use.

## Not in this release

- **Running two radios at once.** One transport per daemon process, same
  as every prior release; dual-radio operation is planned for a later
  release.
- **A shared bulletin-board database across instances.** Still one
  SQLite database file, exactly as today.
- **Per-instance operations work** (systemd instance templates, a
  per-mesh dashboard/status distinction, and related deployment tooling).
- **Removing the deprecated `meshtastic-observatory` entry point.** Still
  installed as an alias, same as every release since `1.2.0`; its removal
  is deferred to the same dual-radio release as the items above.

## Known limitations in this release

- **MeshCore has not been run against real hardware in this repository.**
  This is not the same caveat as Meshtastic's own "manual and incomplete"
  hardware validation (which at least has a tested-hardware table with
  firmware versions behind it) — MeshCore has had zero hardware exercise
  here. An acceptance checklist exists for it, and none of its items are
  signed off yet. Treat MeshCore support as validated in the test suite
  and on paper only, not on real radios, until that changes. The
  real-world safe reply size in bytes is likewise unmeasured;
  `meshcore_max_text_bytes` defaults to a conservative 120 and is
  configurable once a firm number is established.
- The channel-knock concurrency cap (`meshcore_max_pending_knocks`,
  default 1) bounds how many knock replies are queued at once, not the
  rate at which new ones are admitted — see the security doc linked
  above for the honest disclosure of what that does and doesn't bound,
  and the `meshcore_channel = ""` escape if you'd rather not carry it.
