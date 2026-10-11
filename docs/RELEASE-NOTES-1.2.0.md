# Release notes: 1.2.0

Operator-facing notes for upgrading a running rc2 (or 1.0.0) install to
1.2.0. Covers everything since [`1.0.0rc2`](RELEASE-NOTES-1.0.0rc2.md):
the [1.0.0], [1.1.1] and [1.2.0] sections. See [`CHANGELOG.md`](../CHANGELOG.md)
for the full list of changes.

**Status: experimental.** Not for emergencies; no uptime guarantee — see
[Known limitations](#known-limitations-in-this-release) below.

## After upgrading

1. **Pull/deploy the new code, then restart the daemon.**
   - Manual install: stop `mgarden run`, update the checkout, `uv sync
     --frozen --no-dev`, start it again.
   - systemd install: `sudo systemctl restart meshgarden` (or
     `meshtastic-observatory`, if still on the legacy unit name).
   - Raspberry Pi install: `git pull && sudo ./scripts/install-rpi.sh`
     (safe to re-run; see the README's quick start). This also runs
     `mgarden config-sync` automatically against an existing
     `config.toml`, adding any of the keys below that are missing, at
     their default, with a comment — an existing value is never changed.
   - Existing config files and databases are untouched; the boards/mail/
     LAB tables (schema v6-v8) and the distance-plausibility columns are
     created/backfilled automatically on first startup against an
     existing database, same as rc1 → rc2's observatory tables.

2. **New config keys since rc2 — all optional, all default to off/
   unchanged behavior.** Nothing needs to be added to `config.toml` to
   keep current behavior.

   | Section | Key | Default | Purpose |
   |---|---|---|---|
   | `[service]` | `seen_enabled` | `true` | Turns `SEEN` off entirely (silent, as if the command didn't exist) when `false`. |
   | `[service]` | `daily_post_quota` | `5` | Per-node daily cap on `POST` (topic boards). |
   | `[service]` | `daily_mail_quota` | `3` | Per-node daily cap on `MAIL <node> <msg>` sends. |
   | `[service]` | `max_topics` | `50` | Lifetime cap on distinct topic boards ever created. |
   | `[service]` | `mail_pending_limit` | `5` | Max undelivered notes a recipient can hold before new `MAIL` to them is refused (silently, same neutral ack). |
   | `[service]` | `lab_unlock_word` | `""` (off) | Set to a 6-24 character word to gate `POST`/`READ`/`BOARDS`/`MAIL` behind a per-node unlock DM. |
   | `[retention]` | `mail_expiry_days` | `7` | Days an undelivered `MAIL` note is kept before pruning (a delivered note is deleted immediately). |
   | `[observatory]` | `max_direct_km` | `300.0` | Plausibility cap for a "furthest direct" (0-hop) RF candidate. |
   | `[observatory]` | `max_km_per_hop` | `150.0` | Per-hop plausibility budget for a "furthest observed" candidate. |
   | `[observatory]` | `max_observed_km` | `1000.0` | Absolute plausibility cap for "furthest observed", regardless of hop count. |

   `config.example.toml` documents every key above at its code default;
   `mgarden config-sync --template config.example.toml` adds any you're
   missing to an existing `config.toml` without touching values already
   there.

3. **Enable the LAB tier, if wanted.** `POST`/`READ`/`BOARDS`/`MAIL` are
   experimental and off by default. Set `[service] lab_unlock_word` to a
   word visitors DM once to unlock the tier for their own node — see
   [`docs/security-and-privacy.md#lab-tier`](security-and-privacy.md#lab-tier).
   Locked nodes can't tell the tier exists (no reply, no
   `processed_requests` row).

4. **Distance plausibility is automatic, no action needed.** "Furthest
   direct"/"furthest observed" now exclude MQTT-relayed or bogus-position
   candidates by default; the three new `[observatory]` keys above only
   need adjusting if your mesh's real geography is unusually large or
   small.

5. **Run `mgarden doctor`** after restarting to confirm the upgrade
   landed cleanly, and `mgarden --version` (or the new hidden `VERSION`
   DM command) to confirm the running build.

## Known limitations in this release

- **MeshCore transport** and **weather-correlated propagation** remain
  unimplemented — tracked in the phase 2 backlog (maintained outside the
  repository).
- **Hardware acceptance checklist**: as of the last recorded hardware run
  (2026-09-13), only items 1 (broadcast rejection), 2 (DM on channel 0),
  and 9 (duplicate packet ID) have passed against real radios. Still
  outstanding:
  - Item 3 — DM to a different node (needs a third radio).
  - Item 4 — self echo.
  - Item 5 — identity mismatch by swapping radios.
  - Item 6 — reconnect after unplug, no reply burst (needs a physical
    unplug/replug).
  - Item 7 — a real ack.
  - Item 8 — a real NAK.
  - Item 10 — restart mid-flight (uncertain-state recovery).
  - Item 11 — 180-byte reply over a PKI DM (automated test exists, not
    yet run against hardware).
  - Item 12 — congestion behaviour.
  - Item 13 — WAL-active backup restored into a fresh directory.
  - Item 14 — SIGN publishes and is acknowledged, DM only (automated test
    exists, not yet run against hardware).
- **Raspberry Pi installer** has not yet been exercised on a real
  Raspberry Pi (tested via `--dry-run` and the install-script test suite
  only).
- Everything already listed under `[1.0.0rc2]`'s "Known limitations / not
  in this release" in [`CHANGELOG.md`](../CHANGELOG.md) that isn't
  superseded above still applies (e.g. the one sandbox-only WAL/checkpoint
  test flake, and `--install-dir` still being a no-op).

## Also in 1.2.0 (after the 1.1.1 changelog entry was written)

1.1.1 was never tagged or published; these changes shipped in the same release.

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
