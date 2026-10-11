# Release notes: 1.0.0rc2 (`v1.0.0-pre-002`)

Operator-facing notes for upgrading a running rc1 install to rc2. See
[`CHANGELOG.md`](../CHANGELOG.md) for the full list of changes.

## After upgrading

1. **Pull/deploy the new code, then restart the daemon.**
   - Manual install: stop `mgarden run`, update the checkout, `uv sync
     --frozen --no-dev`, start it again.
   - systemd install: `sudo systemctl restart meshgarden` (or
     `meshtastic-observatory`, if still on the legacy unit name).
   - Existing config files and databases are untouched; no schema migration
     step is required beyond letting the daemon start normally (the
     observatory tables introduced since rc1 are created automatically on
     first startup against an existing database).

2. **New config keys — all optional, all default to off/unchanged
   behavior.** Nothing needs to be added to `config.toml` to keep current
   behavior.

   | Section | Key | Default | Purpose |
   |---|---|---|---|
   | `[web]` | `enabled` | `false` | Turns on the local read-only dashboard. |
   | `[web]` | `listen` | `127.0.0.1` | Dashboard bind address. |
   | `[web]` | `port` | `9109` | Dashboard port. |
   | `[web]` | `allow_lan` | `false` | Must be explicitly `true` to bind a non-loopback `listen` address. |
   | `[observatory]` | `latitude` / `longitude` | `0.0` / `0.0` (unset) | Enables distance/bearing fields in `GARDEN`/`RECORD`/the dashboard once set to a real location. |
   | `[observatory]` | `enabled_position_capture` | `true` | Persist `POSITION_APP` data locally; set `false` to opt out. |
   | `[retention]` | `node_daily_days` | `365` | Rollup retention for the new observatory tables. |
   | `[retention]` | `links_days` | `30` | Retention for inferred-neighbour `links` rows. |
   | `[moderation]` | `banned_words_file` | `""` (disabled) | Set to `"default"` to enable the packaged banned-word list, or a path to your own file. |

   Quotas are unchanged from rc1 defaults (`daily_command_quota = 60`,
   `daily_sign_quota = 3`) and need no action.

3. **Enable the dashboard, if wanted.** Set `[web] enabled = true` (leave
   `listen`/`allow_lan` at their loopback defaults unless you specifically
   want LAN access, which requires `allow_lan = true` and is unauthenticated
   — put it behind an SSH tunnel or a trusted LAN only). On a Raspberry Pi
   install, `install-rpi.sh --web-listen ADDR` does this for you when the
   config is (re)generated.

4. **systemd watchdog (optional, automatic on the shipped unit).** The
   packaged `meshgarden.service` now sets `Type=notify` and
   `WatchdogSec=120`; if you maintain your own unit file, no action is
   required to keep the old behavior, but you can adopt the same watchdog
   integration by copying those two lines — the daemon already emits
   `READY=1`/`WATCHDOG=1` and no new dependency is needed.

5. **Run `mgarden doctor`** after restarting to confirm the upgrade landed
   cleanly: it reports worker heartbeat staleness (new in this release),
   the dashboard's status if enabled, and the usual radio/backup/storage
   checks.

## Known limitations in this release

MeshCore transport, weather-correlated propagation, and hardware-in-the-loop
testing of the newer commands/dashboard/installer remain outstanding — see
CHANGELOG's "Known limitations / not in this release" for the full list.
