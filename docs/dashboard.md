# Local dashboard

An optional, **read-only, no-authentication** web dashboard for looking at what the daemon has
observed, without radio access or a terminal. It is off by default (`[web] enabled = false`).
When enabled it serves eleven small server-rendered HTML pages and a set of JSON endpoints from a
daemon thread, with no external CSS, JS or fonts, no live updates, and no map tiles.

## Enable it

Add this to your config, restart the daemon, and browse to `http://127.0.0.1:9109/`:

```toml
[web]
enabled = true
listen = "127.0.0.1"   # loopback by default
port = 9109
```

All `[web]` keys are in [configuration.md](configuration.md).

## Network access

- **Loopback by default.** `listen` must be a loopback address (`127.0.0.1`, `::1` or
  `localhost`) unless you also set `allow_lan = true`. Otherwise the daemon refuses to start the
  dashboard (a logged `WebRefused` error; the rest of the daemon runs normally).
- **LAN opt-in.** With `allow_lan = true` and a non-loopback `listen`, a WARNING is logged on every
  start as a reminder that the dashboard is reachable beyond this machine.
- **Read-only, no login.** Every request is `GET`; any other method (including `POST`) gets `405`.
  There is no login and there will not be one.

To reach the dashboard from another machine without exposing it on the LAN, use an SSH tunnel
instead of `allow_lan`:

```console
ssh -N -L 9109:127.0.0.1:9109 you@your-meshgarden-host
# then browse to http://127.0.0.1:9109/ on your own machine
```

## Pages

| Page | Shows |
|---|---|
| `/` | Today's counts, best SNR, last packet age, queue depth, 24-hour activity bars |
| `/health` | Worker heartbeats, radio identity, storage and backup status, drop counts, moderation queue size |
| `/traffic` | 24-hour per-hour table, portnum mix, top senders, MQTT/RF split |
| `/guestbook` | Visible entries (paginated) and the moderation queue |
| `/boards` | Topic boards |
| `/records` | Today, 7-day and all-time bests; book and wordle aggregates |
| `/digest`, `/digest/<YYYY-MM-DD>` | The daily digest files |
| `/nodes` | Nodes seen in the last 7 days |
| `/nodes/<node-id>` | One node's profile: days seen, best and typical SNR and hops, packets (7 days and all-time), position status, battery, a 30-day packets/SNR sparkline, and an event list (first discovered, best hops improved, strongest SNR) |
| `/map` | Inline-SVG plot centred on the configured observatory (see below) |
| `/topology` | Radial layout from inferred links: direct neighbours on an inner ring, 2-hop nodes on an outer ring, plus frequency, stability, appeared and disappeared tables |
| `/propagation` | Per-direct-node SNR-by-day tables and heat strips over 14 days, and an "unusual distance" section flagging days when the furthest direct distance exceeded 1.5x the 14-day median |

**`/map` details.** Distance rings and node markers show where and how far. Marker colour shows
the hop class, and opacity fades with age. Faint lines show inferred links. It plots at most 300
nodes and accepts these filters: `?range=today|7d`, `?direct=1`, `?role=`, `?new=1`.

**Why no map tiles.** `/map` uses an equirectangular local projection (km east and north of the
configured observatory location) rather than Leaflet or OSM. Tiles need internet access and an
external asset, which the dashboard's local-only rules rule out.

## JSON endpoints

Eight endpoints for external displays on the same host, with the same GET-only, no-auth rules.
`_ROUTE_STATIC` in `web.py` is the authoritative list.

| Endpoint | Returns |
|---|---|
| `/api/summary.json` | Today's digest numbers, the 24-hour activity series, the most recently heard nodes |
| `/api/records.json` | The same bests as `/records` |
| `/api/guestbook.json` | Latest visible guestbook entries and topic counts, as `/guestbook`. The only endpoint that carries guestbook text |
| `/api/topology.json` | The same hop rings and recently heard nodes as `/topology` |
| `/api/iching.json` | The observatory's own I Ching hexagram for today |
| `/api/haiku.json` | Today's haiku, regardless of `[service] daily_display` |
| `/api/daily.json` | The daily draw that `[service] daily_display` selects, tagged with a `kind` field |
| `/api/week.json` | The last seven local days as digests, today first |

## How to read the data

Every hop count, link and distance comes from what Meshtastic packets happen to report (rx hop
counters, voluntarily advertised positions), never from a known network topology.

- **Hop classes** ("direct", "2-hop", "3+ hops") are *observed* classifications from the last 7
  days. Every `links` edge is labelled *inferred*, since Meshtastic gives no complete link
  knowledge. `/map` and `/topology` say so on the page.
- **2-hop nodes** on `/topology` are drawn unattached on the outer ring. Which neighbour relayed
  them is not exposed by Meshtastic, so the page does not guess.
- **Missing data.** `/map`, `/nodes/<id>` and `/propagation` show a plain note (no error, no empty
  map) when `[observatory] latitude/longitude` is unset or the database has no rows yet.

## Security model

Enforced in `web.py` regardless of config:

- **Read-only database access.** Each request opens its own short-lived read-only SQLite
  connection, so it can never block the daemon's database worker or write anything.
- **Status from the status file.** Connectivity, radio, worker, backup and storage fields come
  from the same JSON status file `mgarden status` reads, never computed live. A stalled daemon
  makes a page show "stale" or "unknown" rather than break.
- **Escaped output.** Every database-sourced string (guestbook text, node names) is HTML-escaped.
- **Strict headers.** Every response sends `Content-Security-Policy: default-src 'none';
  style-src 'unsafe-inline'; img-src data:`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.
