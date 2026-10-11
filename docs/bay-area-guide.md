# Bay Area MeshCore guide content

Meshgarden's `GUIDE` replies are a hand-curated snapshot, not a runtime web lookup. Source pages
were checked 2026-09-20 and may have changed since. Operators should review this file and
`meshgarden/community.py` when updating the service.

Most likely to go stale: device availability, firmware guidance, channel keys, Discord links and
network settings.

Sources:

- <https://bayareameshcore.com/> — project scope, links, no-license claim,
  approximate entry cost and range.
- <https://bayareameshcore.com/companion-setup/> — companion flashing,
  app pairing, and 910.525 MHz / 62.5 kHz / SF7 / CR5 settings.
- <https://bayareameshcore.com/repeater-setup/> — repeater setup, US/Canada
  preset, location/admin settings, and unique 2-byte prefixes.
- <https://bayareameshcore.com/recommended-devices/> — device examples and
  changing prices/specifications.
- <https://bayareameshcore.com/channels/> — public-channel purpose and test
  guidance; channel keys are intentionally not copied into static replies.

What the site says about channels:

- Public channels are not private.
- Use `#test` for mesh testing (send `test` for an automated response), not the default public
  channel.
- Do not present emergency channels or automated alerts as a substitute for emergency services.

## Channels this garden uses

- `#kallisti` — the garden's public channel. Public read-only commands are answered here;
  everything personal stays in DMs. Type the name in your app to join.
- `#wx-bay` — NWS Warnings and Watches for the nine Bay Area counties, one line each, polled every
  2 minutes. Advisories are not relayed.
  - **Silence** means no active Warnings or Watches. A daily `🌦 wx-bay daily` line at 06:00
    confirms the feed is alive.
  - **Status on demand:** DM `WX` to the garden (or send it on `#kallisti`) for a one-line status.
  - **Not an emergency service.** Keep an official warning source such as Wireless Emergency
    Alerts or NOAA Weather Radio. SF Skywarn also posts NWS alerts on `#sfoskywarn`.
