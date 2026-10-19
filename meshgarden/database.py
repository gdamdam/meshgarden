"""SQLite storage layer for Meshgarden: WAL mode, migrations, and schema.

Every daemon write goes through a single DB worker thread's connection; the
CLI opens its own separate connection (with the same busy_timeout, so it
waits rather than fails on a momentary writer lock). `connect` never touches
schema; `migrate` is the only thing that creates or upgrades tables, and it
is idempotent so callers may invoke it unconditionally at startup.
"""

from __future__ import annotations

import os
import sqlite3

from meshgarden import bbs_store
from meshgarden.community import NEWS_SQL
from meshgarden.mail import MAIL_ALIASES_SQL, REPLY_ROUTES_SQL
from meshgarden.updates import SCHEMA_SQL as UPDATES_SQL
from meshgarden.usefulness import SCHEMA_SQL as USEFULNESS_SQL

SCHEMA_VERSION = 19

MIGRATION_1_SQL = """
CREATE TABLE nodes (
  node_id             TEXT PRIMARY KEY,          -- "!xxxxxxxx"
  short_name          TEXT,                      -- display label, NOT authenticated
  long_name           TEXT,
  observed_first_seen INTEGER NOT NULL,          -- UTC epoch seconds, from our own observations only
  observed_last_seen  INTEGER NOT NULL,
  observation_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_nodes_last_seen ON nodes(observed_last_seen);

CREATE TABLE observations (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  server_time     INTEGER NOT NULL,   -- UTC epoch seconds, our clock
  packet_time     INTEGER,            -- radio-reported rxTime; NULL when absent
  sender_node_id  TEXT NOT NULL,
  dest_node_id    TEXT NOT NULL,      -- "!xxxxxxxx" or "^all"
  channel         INTEGER NOT NULL,   -- 0 when absent from the packet dict
  portnum         TEXT NOT NULL,
  packet_id       INTEGER,            -- NULL when the packet carried none
  is_duplicate    INTEGER NOT NULL,   -- 1 = raw event whose (sender,packet_id) was already seen in the window
  rx_snr          REAL,               -- NULL = missing, never 0
  rx_rssi         INTEGER,
  hop_limit       INTEGER,
  hop_start       INTEGER,
  hops_away       INTEGER,            -- DERIVED: hop_start - hop_limit, only when both present
  via_mqtt        INTEGER,            -- NULL = unknown origin; never default to RF
  pki_encrypted   INTEGER NOT NULL DEFAULT 0,
  payload_len     INTEGER             -- length only; content is never stored
);
CREATE INDEX idx_obs_server_time ON observations(server_time);
CREATE INDEX idx_obs_sender_time ON observations(sender_node_id, server_time);
CREATE INDEX idx_obs_dedup ON observations(sender_node_id, packet_id, server_time);

CREATE TABLE guestbook (
  entry_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at   INTEGER NOT NULL,      -- UTC epoch seconds
  sender_node_id TEXT NOT NULL,
  display_name TEXT,                  -- snapshot at signing time, may be NULL
  message      TEXT NOT NULL,         -- NFC-normalized, case preserved
  message_hash TEXT NOT NULL,         -- sha256 hex of the normalized message
  visible      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_gb_visible_id ON guestbook(visible, entry_id DESC);
CREATE INDEX idx_gb_sender_hash ON guestbook(sender_node_id, message_hash, created_at);
CREATE INDEX idx_gb_sender_time ON guestbook(sender_node_id, created_at);

CREATE TABLE processed_requests (
  sender_node_id  TEXT NOT NULL,
  packet_id       INTEGER NOT NULL,
  first_seen_at   INTEGER NOT NULL,   -- UTC epoch seconds
  expires_at      INTEGER NOT NULL,   -- first_seen_at + request_dedup_hours*3600
  command         TEXT,               -- bounded enum: MENU|NOT_EMERGENCY|INFO|SIGN|GUESTBOOK|STATS|UNKNOWN. NEVER arguments
  state           TEXT NOT NULL,      -- received|queued|sending|submitted|acked|failed|expired|unknown|dropped
  reason          TEXT,               -- bounded reject/failure reason code, never free text
  entry_id        INTEGER,            -- guestbook.entry_id when the request published one
  tx_packet_id    INTEGER,            -- id of the outbound MeshPacket, for ack matching
  updated_at      INTEGER NOT NULL,
  PRIMARY KEY (sender_node_id, packet_id)
);
CREATE INDEX idx_pr_expires ON processed_requests(expires_at);
CREATE INDEX idx_pr_state ON processed_requests(state);
CREATE INDEX idx_pr_txid ON processed_requests(tx_packet_id);

CREATE TABLE daily_stats (
  day                 TEXT PRIMARY KEY,  -- "YYYY-MM-DD" UTC
  raw_events          INTEGER NOT NULL DEFAULT 0,
  unique_packets      INTEGER NOT NULL DEFAULT 0,
  distinct_senders    INTEGER NOT NULL DEFAULT 0,  -- for THIS day only; never sum across days
  text_packets        INTEGER NOT NULL DEFAULT 0,
  commands_accepted   INTEGER NOT NULL DEFAULT 0,
  commands_rejected   INTEGER NOT NULL DEFAULT 0,
  replies_submitted   INTEGER NOT NULL DEFAULT 0,
  replies_acked       INTEGER NOT NULL DEFAULT 0,
  replies_failed      INTEGER NOT NULL DEFAULT 0,
  entries_created     INTEGER NOT NULL DEFAULT 0,
  snr_bucket_counts   TEXT,   -- JSON object of fixed SNR buckets; no node IDs
  finalized           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE blocked_senders (
  node_id    TEXT PRIMARY KEY,
  blocked_at INTEGER NOT NULL,
  note       TEXT
);
"""

MIGRATION_2_SQL = """
CREATE TABLE wordle_state (
  player_key  TEXT NOT NULL,      -- sha256(salt|sender_node_id|day)[:32]; never a raw node id
  day         TEXT NOT NULL,      -- "YYYY-MM-DD" local day
  guesses     INTEGER NOT NULL DEFAULT 0,
  solved      INTEGER NOT NULL DEFAULT 0,
  updated_at  INTEGER NOT NULL,   -- UTC epoch seconds
  PRIMARY KEY (player_key, day)
);
CREATE INDEX idx_wordle_state_updated ON wordle_state(updated_at);

CREATE TABLE wordle_daily (
  day                    TEXT PRIMARY KEY,  -- "YYYY-MM-DD" local day
  players                INTEGER NOT NULL DEFAULT 0,
  solves                 INTEGER NOT NULL DEFAULT 0,
  total_guesses_on_solve INTEGER NOT NULL DEFAULT 0
);
"""

MIGRATION_3_SQL = """
CREATE TABLE node_daily_quota (
  node_id     TEXT NOT NULL,          -- "!xxxxxxxx"
  day         TEXT NOT NULL,          -- "YYYY-MM-DD" local day, from fortune.local_day
  commands    INTEGER NOT NULL DEFAULT 0,
  signs       INTEGER NOT NULL DEFAULT 0,
  updated_at  INTEGER NOT NULL,       -- UTC epoch seconds
  PRIMARY KEY (node_id, day)
);
CREATE INDEX idx_node_daily_quota_updated ON node_daily_quota(updated_at);

ALTER TABLE guestbook ADD COLUMN moderation_reason TEXT;
"""

MIGRATION_4_SQL = """
CREATE TABLE book_stats (
  book_key    TEXT NOT NULL,       -- BookPuzzle.title; stable per-puzzle key, never a node id
  day         TEXT NOT NULL,       -- "YYYY-MM-DD" local day
  shown       INTEGER NOT NULL DEFAULT 0,
  guesses     INTEGER NOT NULL DEFAULT 0,
  solves      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (book_key, day)
);
CREATE INDEX idx_book_stats_key ON book_stats(book_key);

-- All-time aggregate per book, used for the first-ever-solve and rare-catch
-- tiers: no per-day breakdown, no node ids.
CREATE VIEW book_stats_alltime AS
  SELECT book_key,
         SUM(shown) AS shown,
         SUM(guesses) AS guesses,
         SUM(solves) AS solves
  FROM book_stats
  GROUP BY book_key;
"""

MIGRATION_5_SQL = """
-- Observatory data model (phase 2): structured, append-only position log,
-- per-node-per-day rollups, inferred-direct link observations, and passive
-- local radio samples. No packet text, no keys, ever.

CREATE TABLE node_positions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id        TEXT NOT NULL,        -- "!xxxxxxxx", the position's own sender
  server_time    INTEGER NOT NULL,     -- UTC epoch seconds, our clock
  lat            REAL,                 -- degrees; may be coarse, voluntarily advertised
  lon            REAL,
  alt            INTEGER,              -- meters; NULL when absent
  precision_bits INTEGER,              -- NULL when absent
  reported_time  INTEGER               -- radio-reported GPS fix time; NULL when absent
);
CREATE INDEX idx_node_positions_time ON node_positions(server_time);
CREATE INDEX idx_node_positions_node_time ON node_positions(node_id, server_time);

ALTER TABLE nodes ADD COLUMN hw_model TEXT;
ALTER TABLE nodes ADD COLUMN role TEXT;
ALTER TABLE nodes ADD COLUMN last_position_lat REAL;
ALTER TABLE nodes ADD COLUMN last_position_lon REAL;
ALTER TABLE nodes ADD COLUMN last_position_at INTEGER;
ALTER TABLE nodes ADD COLUMN last_battery INTEGER;

CREATE TABLE node_daily (
  node_id        TEXT NOT NULL,
  day            TEXT NOT NULL,          -- "YYYY-MM-DD" UTC, same shape as daily_stats.day
  packets        INTEGER NOT NULL DEFAULT 0,
  direct_packets INTEGER NOT NULL DEFAULT 0,
  best_snr       REAL,                   -- highest rx_snr observed this day; NULL = none
  snr_sum        REAL NOT NULL DEFAULT 0,   -- accumulator for typical_snr (mean); never read directly
  snr_count      INTEGER NOT NULL DEFAULT 0,
  typical_snr    REAL,                   -- mean rx_snr this day; NULL = none observed
  best_hops      INTEGER,                -- fewest derived hops observed this day; NULL = none
  hops_sum       INTEGER NOT NULL DEFAULT 0, -- accumulator for typical_hops (mean); never read directly
  hops_count     INTEGER NOT NULL DEFAULT 0,
  typical_hops   REAL,                   -- mean derived hops this day; NULL = none observed
  last_lat       REAL,                   -- most recent position observed this day, if any
  last_lon       REAL,
  PRIMARY KEY (node_id, day)
);
CREATE INDEX idx_node_daily_day ON node_daily(day);

CREATE TABLE links (
  a_node_id  TEXT NOT NULL,             -- sender heard direct
  b_node_id  TEXT NOT NULL,             -- our own node id (the only side we ever infer)
  first_seen INTEGER NOT NULL,
  last_seen  INTEGER NOT NULL,
  count      INTEGER NOT NULL DEFAULT 0,
  kind       TEXT NOT NULL CHECK(kind IN ('inferred_direct')),
  PRIMARY KEY (a_node_id, b_node_id)
);
CREATE INDEX idx_links_last_seen ON links(last_seen);

CREATE TABLE local_radio_samples (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  server_time         INTEGER NOT NULL,
  channel_utilization REAL,
  air_util_tx         REAL,
  battery             INTEGER
);
CREATE INDEX idx_local_radio_samples_time ON local_radio_samples(server_time);
"""

MIGRATION_6_SQL = """
-- Topic boards: reuse the guestbook table. topic IS NULL means the
-- original, unchanged main guestbook; every existing Guestbook read method
-- keeps that meaning as its default.
ALTER TABLE guestbook ADD COLUMN topic TEXT;
CREATE INDEX idx_gb_topic_visible_id ON guestbook(topic, visible, entry_id DESC);

-- Separate daily counters, same generic upsert-and-increment shape as
-- commands/signs.
ALTER TABLE node_daily_quota ADD COLUMN posts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE node_daily_quota ADD COLUMN mails_sent INTEGER NOT NULL DEFAULT 0;

-- Mailbox: per-node opt-in, default off (enabled=0).
CREATE TABLE mail_prefs (
  node_id     TEXT PRIMARY KEY,
  enabled     INTEGER NOT NULL DEFAULT 0,
  updated_at  INTEGER NOT NULL
);

-- One row per stored note. Bodies are stored by design (both parties
-- consented -- see security-and-privacy.md): sender opted in by sending,
-- recipient opted in via MAIL ON.
CREATE TABLE mail (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at         INTEGER NOT NULL,
  sender_node_id     TEXT NOT NULL,
  recipient_node_id  TEXT NOT NULL,
  message            TEXT NOT NULL,
  message_hash       TEXT NOT NULL,
  hidden             INTEGER NOT NULL DEFAULT 0,
  moderation_reason  TEXT
);
CREATE INDEX idx_mail_recipient ON mail(recipient_node_id, hidden, created_at);
CREATE INDEX idx_mail_created ON mail(created_at);
"""

MIGRATION_7_SQL = """
-- BOOK: one solve per player per local day. Same one-way hashed key as
-- wordle_state (sha256(salt|node|day)[:32]); never a raw node id, never a
-- guess. Fixes repeat correct guesses re-counting in book_stats.solves.
CREATE TABLE book_state (
  player_key  TEXT NOT NULL,
  day         TEXT NOT NULL,
  solved      INTEGER NOT NULL DEFAULT 0,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (player_key, day)
);
CREATE INDEX idx_book_state_updated ON book_state(updated_at);
"""

MIGRATION_8_SQL = """
-- LAB tier: per-node unlock state (main.py's Lab class), gated by the
-- operator-configured [service] lab_unlock_word. An explicit opt-in event,
-- like mail_prefs -- never the word itself, only the node id and when it
-- unlocked.
CREATE TABLE lab_nodes (
  node_id      TEXT PRIMARY KEY,
  unlocked_at  INTEGER NOT NULL
);
"""

MIGRATION_9_SQL = """
-- Task 3B-3 (scope-decisions.md section 7; transport-boundary-spec.md
-- section 3.2): the per-recipient rate limit for a MeshCore channel
-- "knock". Keyed on the RESOLVED recipient node id, never the claimed
-- channel display name -- a forged name must cost an attacker at most one
-- bounded DM to the node actually being impersonated. Lives in the
-- database, not an in-memory dict, so the cooldown survives a restart, a
-- crash, or a restart loop; that is the entire reason this table exists
-- rather than a dict on the Daemon (see commands.ChannelKnockCooldown).
CREATE TABLE channel_knocks (
  node_id       TEXT PRIMARY KEY,
  last_knock_at INTEGER NOT NULL
);
CREATE INDEX idx_channel_knocks_last_knock_at ON channel_knocks(last_knock_at);
"""

MIGRATION_10_SQL = """
ALTER TABLE nodes ADD COLUMN identity_updated_at INTEGER NOT NULL DEFAULT 0;
ALTER TABLE observations ADD COLUMN transport TEXT NOT NULL DEFAULT 'meshtastic';
ALTER TABLE observations ADD COLUMN routing_mode TEXT;
ALTER TABLE processed_requests ADD COLUMN submitted_at INTEGER;
UPDATE processed_requests SET submitted_at = updated_at WHERE state IN ('submitted', 'acked');
-- Historical finalized rows retain submitted snapshots plus known ACKed replies.
-- Past unknown/failed outcomes have no recoverable submission history.
ALTER TABLE daily_stats ADD COLUMN replies_sent INTEGER NOT NULL DEFAULT 0;
UPDATE daily_stats SET replies_sent = replies_submitted + replies_acked;
-- Legacy MeshCore contacts had observation_count=0 but fabricated heard times.
UPDATE nodes SET observed_first_seen=0, observed_last_seen=0 WHERE observation_count=0;
"""

MIGRATION_11_SQL = """
ALTER TABLE nodes ADD COLUMN stored_route TEXT;
"""

MIGRATION_12_SQL = """
CREATE TABLE meshcore_identity_bindings (
    node_id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL,
    ambiguous INTEGER NOT NULL DEFAULT 0
);
"""

MIGRATION_15_SQL = """
-- A recognized command rejected by the short per-node command cooldown may
-- receive one explanatory DM per 30 minutes. Persist the notification time
-- so restarting the daemon cannot turn a noisy sender into another burst.
CREATE TABLE command_cooldown_notices (
  node_id         TEXT PRIMARY KEY,
  last_notice_at  INTEGER NOT NULL
);
CREATE INDEX idx_command_cooldown_notices_at
  ON command_cooldown_notices(last_notice_at);
"""

MIGRATION_16_SQL = """
-- Weather broadcasts (spec 2026-10-10-weather-alerts-spec.md section 5):
-- one row per NWS product the garden has decided about. Dedup across
-- restarts and the hourly cap both read this table; nothing else does.
CREATE TABLE weather_alerts (
  product_key    TEXT PRIMARY KEY,   -- smallest NWS alert id of the first group seen
  last_alert_id  TEXT NOT NULL,      -- most recent NWS feature id folded into this row
  event          TEXT NOT NULL,
  areas          TEXT NOT NULL,      -- aliased zone names, comma-joined, as last sent
  expires_at     INTEGER NOT NULL,   -- UTC epoch seconds
  message_type   TEXT NOT NULL,      -- Alert | Update | Cancel
  outcome        TEXT NOT NULL,      -- sent | suppressed | dropped | capped
  updated_at     INTEGER NOT NULL
);
CREATE INDEX idx_weather_alerts_last_id ON weather_alerts(last_alert_id);
CREATE INDEX idx_weather_alerts_expires ON weather_alerts(expires_at);
"""

MIGRATION_17_SQL = """
-- Small key/value state for the weather feature (daily heartbeat day).
CREATE TABLE weather_state (
  key        TEXT PRIMARY KEY,   -- e.g. 'heartbeat_day'
  value      TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
"""

MIGRATION_18_SQL = """
-- Generic daemon key/value state (daily contact-prune day). weather_state
-- stays separate: it belongs to the weather ledger.
CREATE TABLE daemon_state (
  key        TEXT PRIMARY KEY,   -- e.g. 'contact_prune_day'
  value      TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
"""

MIGRATION_19_SQL = """
-- Positions a visitor deliberately shared by DM ("Saved position: lat, lon"
-- or "GPS position: lat, lon" from the MeshCore app). Append-only; holds
-- the parsed coordinates only, never the message text. Separate from
-- node_positions (radio-advertised POSITION_APP) so the observatory's
-- plausibility and distance logic is unaffected.
CREATE TABLE dm_positions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id     TEXT NOT NULL,      -- "!xxxxxxxx", the sender
  server_time INTEGER NOT NULL,   -- UTC epoch seconds, our clock
  kind        TEXT NOT NULL,      -- 'saved' | 'gps'
  lat         REAL NOT NULL,
  lon         REAL NOT NULL
);
CREATE INDEX idx_dm_positions_node_time ON dm_positions(node_id, server_time);
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, MIGRATION_1_SQL),
    (2, MIGRATION_2_SQL),
    (3, MIGRATION_3_SQL),
    (4, MIGRATION_4_SQL),
    (5, MIGRATION_5_SQL),
    (6, MIGRATION_6_SQL),
    (7, MIGRATION_7_SQL),
    (8, MIGRATION_8_SQL),
    (9, MIGRATION_9_SQL),
    (10, MIGRATION_10_SQL),
    (11, MIGRATION_11_SQL),
    (12, MIGRATION_12_SQL),
    (13, NEWS_SQL + REPLY_ROUTES_SQL + UPDATES_SQL + USEFULNESS_SQL),
    (14, MAIL_ALIASES_SQL),
    (15, MIGRATION_15_SQL),
    (16, MIGRATION_16_SQL),
    (17, MIGRATION_17_SQL),
    (18, MIGRATION_18_SQL),
    (19, MIGRATION_19_SQL),
]


def _ensure_private_parent(path: str) -> None:
    dirname = os.path.dirname(path)
    if dirname:
        is_new_dir = not os.path.isdir(dirname)
        os.makedirs(dirname, exist_ok=True)
        if is_new_dir:
            # Belt and braces alongside the systemd unit's UMask=0077:
            # a state directory this process creates itself (rather
            # than one systemd's StateDirectory already prepared) must
            # not rely on the ambient umask alone to keep the
            # guestbook/observation data and status file owner-only.
            os.chmod(dirname, 0o700)


def connect(
    path: str, *, read_only: bool = False, bbs_path: str | None = None
) -> sqlite3.Connection:
    """Open a connection with WAL mode and the pragmas the daemon and CLI both need.

    The daemon's single DB worker thread and a separately-started CLI process
    each call this independently; busy_timeout lets a CLI reader/writer wait
    out a brief lock instead of failing immediately.

    Every connection this returns is owned by exactly one thread (Python's
    default check_same_thread=True is never relaxed): the daemon opens a
    separate connection per worker thread rather than sharing one across
    threads, so WAL + busy_timeout is what provides safe concurrency here,
    not a relaxed same-thread guard.

    `bbs_path` attaches the shared BBS file as schema `bbs` (see bbs_store);
    None keeps the legacy single-file layout. A read-only attach of a
    missing file raises sqlite3.OperationalError, same as a missing main.
    """
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        _ensure_private_parent(path)
        is_new = not os.path.exists(path)
        conn = sqlite3.connect(path)
        if is_new:
            os.chmod(path, 0o600)

    if bbs_path:
        try:
            if read_only:
                # The main connection was opened with uri=True, which is
                # what lets ATTACH honour the mode=ro URI too.
                conn.execute("ATTACH DATABASE ? AS bbs", (f"file:{bbs_path}?mode=ro",))
            else:
                _ensure_private_parent(bbs_path)
                bbs_is_new = not os.path.exists(bbs_path)
                conn.execute("ATTACH DATABASE ? AS bbs", (bbs_path,))
                if bbs_is_new:
                    os.chmod(bbs_path, 0o600)
        except BaseException:
            conn.close()
            raise

    conn.row_factory = sqlite3.Row
    # Unqualified journal_mode applies to every attached database.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if bbs_path:
        conn.execute("PRAGMA bbs.synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def get_daemon_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_daemon_state(conn: sqlite3.Connection, key: str, value: str, now: int) -> None:
    with conn:
        conn.execute(
            "INSERT INTO daemon_state(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, now),
        )


def get_schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, *, transport: str = "meshtastic") -> int:
    """Apply pending migrations. Idempotent.

    Each migration's DDL and its `user_version` bump run as a single
    `executescript` wrapped in an explicit BEGIN/COMMIT, so SQLite applies
    both atomically: a failure partway through one migration's DDL leaves
    user_version at the last successfully completed migration, never a
    partially-applied schema paired with a version bump that would make a
    later `migrate()` call retry (and fail on) already-created tables.

    With a `bbs` schema attached, the BBS tables are then moved into the
    shared file (bbs_store.ensure_split); `transport` decides whether their
    node ids get the `mc:` qualifier. The returned version is main's.
    """
    current = get_schema_version(conn)
    for version, sql in MIGRATIONS:
        if version > current:
            script = f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;"
            try:
                conn.executescript(script)
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
            current = version
    if bbs_store.is_attached(conn):
        bbs_store.ensure_split(conn, transport)
    return current
