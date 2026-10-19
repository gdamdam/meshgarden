# Architecture

How the Meshgarden daemon is put together: threads, the transport boundary, the two radio backends,
and how replies are acknowledged. For what visitors can do, see the [README](../README.md).

## Overview

One process with three long-lived threads, plus short-lived CLI commands and an optional
Prometheus scraper.

```text
 Radio (USB serial)
    │  receive callback (non-blocking enqueue only)
    ▼
 bounded ingest queue (1000)
    ▼
 DB worker thread (own SQLite connection, WAL)
    ├─ collector     observations, nodes
    ├─ request log   processed_requests
    ├─ guestbook     SIGN validate + insert
    └─ retention     prune / rollup / backup
    │  ReplyJob
    ▼
 TX worker thread (own SQLite connection, WAL)
    └─ Sender.pump()   the ONLY send path
    │  Transport.send_dm / send_channel
    ▼
 Radio ──▶ mesh

 Separate read-only processes:
   mgarden status / db-stats / guestbook / block / backup
       ├─▶ SQLite database (WAL, own connection + busy_timeout)
       └─▶ <database dir>/status.json (daemon rewrites it every 10s)
   Prometheus ─▶ http://127.0.0.1:9108/metrics (loopback-only by default)
```

Each `sqlite3.Connection` belongs to exactly one thread. The DB worker and TX worker open their own
at thread start, and the CLI uses a separate one. WAL mode plus `busy_timeout` makes this
concurrent access safe.

## Transport boundary

`meshgarden.transport.Transport` is a `Protocol` with thirteen members: `connect`, `identity`,
`is_broadcast`, `on_message`, `send_dm`, `reset_dm_path`, `send_channel`, `on_delivery`,
`max_text_bytes`, `display_label`, `short_label`, `parse_node_argument` and `status`. The list is
pinned by `TRANSPORT_SURFACE` in `tests/test_transport.py`.

The daemon uses it to decide eligibility and to send replies:

- `radio.as_inbound()` turns an `IncomingPacket` observation into a normalized `transport.Inbound`
  (sender, recipient, channel scope, text, ids, signal). `commands.decide_inbound` then decides
  eligibility by comparing `Inbound.recipient` with the `BROADCAST` sentinel and the transport's own
  id.
- Every reply goes through `Sender.pump()`, which calls `Transport.send_dm` or, for an accepted
  MeshCore channel command, `Transport.send_channel`. Only one configured MeshCore channel can send;
  Meshtastic rejects `send_channel`.
- `parse_node_argument`, `display_label` and `short_label` keep node ids consistent across radios.
  `Collector.resolve_node` (the `<node>` argument of `SEEN` and `MAIL`) accepts a full id or a bare
  4-hex tail, `SEEN` uses `short_label` for its "!xxxx" tail, and `ME` uses `display_label`.

How much of the Protocol carries traffic depends on the backend. `is_broadcast` has no production
call site on either backend.

| Backend | Chosen by | `connect` / `on_message` | Notes |
| ------- | --------- | ------------------------ | ----- |
| Meshtastic | `[radio] transport = "meshtastic"` (default) | Shape only. The daemon starts and receives via `RadioAdapter` and the observation path. | `radio.py` is the only module that may `import meshtastic`. |
| MeshCore | `[radio] transport = "meshcore"` | Load-bearing. `on_message` is the only way inbound messages arrive. | No `RadioAdapter`, no observation path. |

Guard tests: `tests/test_radio.py::test_only_radio_py_imports_meshtastic` and
`tests/test_central_invariant.py::test_only_sender_module_calls_send_text`.

## Meshtastic backend

`meshtastic_transport.MeshtasticTransport` wraps `radio.RadioAdapter`. It adds one method outside the
Protocol, `note_routing_packet`, which `main.py` calls for every `ROUTING_APP` packet. That
ack-correlation logic is Meshtastic firmware behavior with no MeshCore equivalent.

## MeshCore backend

`meshcore_transport.MeshCoreTransport` is driven by a `MeshCoreClient`. In production that is
`meshcore_client.RealMeshCoreClient`, which wraps the optional `meshcore` package.

- **Lazy import.** The package is imported only when a `RealMeshCoreClient` is constructed, so a
  Meshtastic-only install never loads it.
- **Threads.** The library's asyncio loop runs on a background thread and hands translated events
  to a "pump" thread, which calls the transport's synchronous handlers.
- **Events.** It subscribes to exactly eight event types.
- **Extras beyond the Protocol.** `on_contact` (a `NEW_CONTACT`'s name and position, the only way a
  MeshCore node's identity reaches the `nodes` table) and `set_watched_channel_idx`.
- **Channel.** MeshCore is the only backend that listens on a channel. In the default
  `service.mode = "dm_only"` the channel accepts only `!garden` / `!dm` knocks and answers by DM. In
  `mode = "channel"` the daemon also accepts a fixed public, read-only command subset and sends one
  response on that channel. Every other channel index is refused. Channel sends get no remote
  delivery ACK, so they are never retried.

### On every connection

`RealMeshCoreClient._post_connect_setup` runs five steps on each connection, not once at startup.
This matters because a daemon started before its USB device enumerates would otherwise run with
channel triggering silently off for its whole life.

1. **Seed contacts** from the radio's stored list. The library sends no event for a contact the
   radio already knows, so without this a DM from a long-known contact would fail as unknown after
   each restart.
2. **Resolve the channel name** to an index.
3. **Set the radio clock** from host time. This is unconditional, because the clock lives in a
   power domain that loses its value on power loss. The firmware refuses to move its clock
   backwards, so a radio already ahead is left alone and logged.
4. **Write `autoadd_config`**, only if `meshcore_autoadd_overwrite` is enabled.
5. **Send one flood self-advertisement.**

## Transmissions that are not replies

Meshgarden otherwise transmits only in reply to a request. There are two narrow exceptions, plus
one request-driven RF action. All of them still go through `Sender`, so the central send guards
hold. See [security-and-privacy.md](security-and-privacy.md) for the privacy side.

- **Self-advertisement (MeshCore).** A node that has never heard this radio cannot route back to it,
  so even a correctly ACKed reply DM can fail. The advert is sent on every connection and repeated
  by `Daemon._check_meshcore_advert_interval` (the DB worker's maintenance tick) every
  `[radio] meshcore_advert_interval_seconds` (default 14400, or 4h). `0` disables the repeat but not
  the per-connection advert. It is presence beaconing, like Meshtastic's NODEINFO broadcasts.
- **Weather alerts** (`[weather]`, off by default, `meshgarden/weather.py`). This is real message
  traffic, so it is bounded harder: one configured channel (accepted for sends only), Warnings and
  Watches only, at most two lines per poll, and a hard per-hour cap. The poller thread never touches
  SQLite or the transport. It queues `WeatherBatch` objects, the DB worker's `WeatherLedger`
  decides, and the daemon enqueues an ordinary `ReplyJob` with the garden's own node id as
  `request_sender` and `Command.WEATHER` on the `processed_requests` row.
- **Mail notices (opt-in).** `Daemon._check_mail_notify` (the DB worker's maintenance tick) sends a
  radio that opted in with `MAIL NOTIFY ON` one DM when new mail waits for it, bounded by an
  interval and a daily cap (`meshgarden/mail_notify.py`). Each is an ordinary `ReplyJob` addressed
  to that radio, logged in `processed_requests` as `MAIL_NOTIFY`; each daemon notifies only radios
  on its own mesh.
- **`REPEATER` (request-driven).** After an eligible request reserves its reply, the first request
  starts one background guest login and status request to the single configured full public key and
  replies that the query is in progress. Later requests read a bounded cache. Concurrent requests
  share one in-flight operation, failures have a 30-second retry floor, and a reconnect invalidates
  an older session's result. The DB worker never waits for RF.

## Contact pruning (MeshCore, opt-in)

A companion radio at its contact cap refuses to auto-add new nodes, so the garden cannot decrypt a
newcomer's DMs. With `[radio] meshcore_contact_prune_enabled` (default off), the second persisted
radio write is housekeeping:

- `Daemon._check_meshcore_contact_prune` runs once per local day, at or after
  `meshcore_contact_prune_hour`. The day is recorded in the `daemon_state` table.
- It lists the contacts, picks the least recently heard (`contacts.select_contacts_to_prune`), and
  removes them until `meshcore_contact_target` remain, at most `meshcore_contact_prune_max_per_day`.
- It never removes a contact that ever sent a command or mail (`processed_requests`), holds a LAB
  unlock or mail prefs, is a `mobile_dm_recovery_destinations` entry, or is a repeater or room.

## Acknowledgements and the commit-before-send window

A firmware acknowledgement means the destination **radio** received the reply. It says nothing about
whether a person read it. Each queued reply moves through these states:

| State | Meaning |
| ----- | ------- |
| `submitted` | Handed to the radio, tx packet id recorded. |
| `acked` | A `ROUTING_APP` ack with no error, from a node other than ourselves (normally the destination). |
| `failed` | A `ROUTING_APP` packet with a non-NONE error reason, bounded to a known error code. |
| `expired` | The reply's TTL passed before it could be sent. |
| `unknown` | No ack within `ack_timeout_seconds` (default 300), or the daemon restarted mid-flight. |

The firmware's implicit self-acknowledgement (no error, from our own node: "transmitted / heard
rebroadcast") does **not** settle the outcome. The request stays `submitted` until a remote ack, a
NAK or the timeout.

State changes are committed *before* their side effect: `sending` just before the radio call,
`submitted` just after it returns a packet id. That leaves one honest failure window: **a crash
between those two commits leaves the request in an unknown state.** On restart every non-terminal
request (`queued` / `sending`) becomes `unknown` and is **never resent**, since a missed response
is preferred over duplicate traffic. For `SIGN`, the guestbook entry was committed with its `queued`
state, so a crash in that window can leave a published entry with no confirmation DM. That
trade-off is intentional.

## Delivery ordering and daily finalization

- **Ordering.** The TX worker marks a submission in progress before calling the transport. ACKs that
  arrive before correlation is registered go into a bounded queue, and the DB worker settles them
  afterwards, so SQLite stays thread-owned. Buffering adds no attempt.
- **Retries.** A bounded application retry policy may resend an unacknowledged reply up to three
  times, 15 seconds apart by default. A newer accepted reply to the same node cancels the older
  retry series, so stale responses never arrive out of order.
- **MeshCore retry paths.** The original send and retry 1 use the contact's stored path. Before
  retry 2, Meshgarden issues the companion's local `reset_path`, so retries 2 and 3 flood. This
  matches MeshCore's upstream strategy and recovers a mobile contact's stale cached route.
- **Rollups.** A past day stays provisional while its requests are queued, sending or awaiting
  delivery. Maintenance revisits provisional days and keeps their expired request rows until
  finalization, so replies crossing UTC midnight are counted. Finalized days are immutable.
- **Key fingerprints.** MeshCore contact-key fingerprints are bound durably, through short-lived
  thread-owned connections, before contacts are admitted. This keeps the single send path and
  quarantines shortened-ID collisions across restarts.
