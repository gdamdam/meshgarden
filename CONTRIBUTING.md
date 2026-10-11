# Contributing to Meshgarden

Thanks for helping out. AI-assisted contributions are welcome and are reviewed the same way as any
other.

## Dev setup

```bash
uv sync
uv run pytest -q      # unit/integration suite; hardware tests are deselected by default
uv run ruff check .
```

Tests marked `hardware` need a real Meshtastic radio on USB. Run them with
`uv run pytest -m hardware` only if you have one attached and follow the etiquette below.

## Radio etiquette

**Never test on public channels.** Keep all test traffic to DMs, and put the word "test" in every
test message so anyone reading logs can tell it is not production traffic.

## Pull requests

- CI (lint and tests) must pass.
- Add a `CHANGELOG.md` entry under `[Unreleased]`.
- Keep the core rules intact for anything that touches replies:
  - DMs work in both modes. Optional MeshCore channel replies stay limited to the documented
    public command subset.
  - Replies fit the transport byte budget (at most 180 UTF-8 bytes).
  - See the safety guarantees in the README for the full list.
- For bug reports, use the bug report form instead of a blank issue.

## Design decisions worth knowing

- **`MENU` and `?`** replaced `HELP`, because "HELP" over a mesh can be a genuine distress message.
- **`dm_only` is the default.** MeshCore `channel` mode also answers supported public commands on
  the configured channel; DMs still work.
- **DM-only features:** mail, writes, stateful games, unlocks and personalized state. Meshtastic
  never replies on a channel.
- **Retries:** replies go through the audited `Sender` path with a bounded retry policy. Opt-in
  mobile DM recovery swaps the legacy retries for at most one encrypted flooded retry for selected
  destinations. Channel replies are never retried.
