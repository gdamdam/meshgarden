"""Retention, daily aggregates, backups, and storage accounting.

Runs on the DB worker thread with the daemon's single write connection.
Deletes always proceed in bounded batches (never an unbounded DELETE) so a
large backlog cannot block the daemon for an extended stretch. Guestbook
rows are never touched here: `guestbook_expiry_days == 0` means "never
expire" and no code path deletes guestbook entries at the storage cap
either -- pressure only pauses new publication and sheds optional writes.

Logging in this module is limited to lifecycle events and bounded reason
codes: never node IDs in aggregates, never message content anywhere.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from meshgarden.config import BackupConfig, RetentionConfig

logger = logging.getLogger(__name__)

BATCH_SIZE: int = 500  # bounded batches, never an unbounded DELETE

WARN_FRACTION: float = 0.80
CRITICAL_FRACTION: float = 0.95

_TERMINAL_REQUEST_STATES = ("acked", "failed", "expired", "unknown", "dropped")

_SNR_BUCKETS = ("lt_-15", "-15_-10", "-10_-5", "-5_0", "0_5", "ge_5", "missing")


@dataclass(frozen=True)
class WeekSummary:
    """Read-only rollup of the last `days` calendar days of `daily_stats`, keyed on
    the same UTC calendar day `rollup_day` writes. Used by the WEEK command
    and exposed for the CLI. `senders_min`/`senders_max` are a range across
    the days, never a sum (distinct_senders is only ever meaningful within
    one day); `total_packets` and `guestbook_entries`/`replies` ARE summed,
    since a packet/entry/reply count composes across days."""

    total_packets: int
    senders_min: int | None
    senders_max: int | None
    guestbook_entries: int
    replies: int
    days_counted: int


def week_summary(conn: sqlite3.Connection, today: str, days: int = 7) -> WeekSummary:
    """`today` is a "YYYY-MM-DD" UTC day (the same shape `rollup_day` keys
    daily_stats on); includes that day and the previous `days - 1` dates."""
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT unique_packets, distinct_senders, entries_created, replies_sent "
        "FROM daily_stats WHERE day >= ? AND day <= ? ORDER BY day DESC",
        (cutoff, today),
    ).fetchall()
    if not rows:
        return WeekSummary(
            total_packets=0,
            senders_min=None,
            senders_max=None,
            guestbook_entries=0,
            replies=0,
            days_counted=0,
        )
    senders = [r["distinct_senders"] for r in rows]
    return WeekSummary(
        total_packets=sum(r["unique_packets"] for r in rows),
        senders_min=min(senders),
        senders_max=max(senders),
        guestbook_entries=sum(r["entries_created"] for r in rows),
        replies=sum(r["replies_sent"] for r in rows),
        days_counted=len(rows),
    )


@dataclass(frozen=True)
class StorageReport:
    db_bytes: int  # .db + -wal + -shm
    backup_bytes: int
    log_bytes: int
    total_bytes: int
    cap_bytes: int  # max_managed_storage_mib * 1024 * 1024
    headroom_bytes: int  # cap_bytes - total_bytes (may be negative)
    pressure: str  # "ok" | "warn" | "critical"
    publication_paused: bool  # True when pressure == "critical"


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except FileNotFoundError:
        return 0


def _dir_size(path: str) -> int:
    total = 0
    try:
        entries = os.listdir(path)
    except FileNotFoundError:
        return 0
    for name in entries:
        total += _file_size(os.path.join(path, name))
    return total


class Retention:
    def __init__(
        self,
        conn: sqlite3.Connection,
        retention: RetentionConfig,
        backup: BackupConfig,
        *,
        log_dir: str | None = None,
        cap_bytes: int | None = None,
    ) -> None:
        # cap_bytes is a test-only override of the configured storage cap:
        # RetentionConfig.max_managed_storage_mib's MiB granularity is too
        # coarse to exercise the backup pre-flight refusal in tests. Callers
        # outside tests must not pass it -- the cap always comes from config.
        self._conn = conn
        self._retention = retention
        self._backup = backup
        self._log_dir = log_dir
        self._cap_bytes_override = cap_bytes
        self._last_backup_at: int | None = None
        self._last_backup_error: str | None = None

        row = conn.execute("PRAGMA database_list").fetchone()
        self._db_path = row["file"] if row is not None else ""

    @property
    def last_backup_at(self) -> int | None:
        """Epoch seconds of the last successful backup, or None if none yet."""
        return self._last_backup_at

    @property
    def last_backup_error(self) -> str | None:
        """Bounded reason code (exception class name, or 'storage_cap' /
        'disk_space') from the most recent backup attempt, or None if the
        most recent attempt succeeded (or none has been attempted yet)."""
        return self._last_backup_error

    def _cap_bytes(self) -> int:
        if self._cap_bytes_override is not None:
            return self._cap_bytes_override
        return self._retention.max_managed_storage_mib * 1024 * 1024

    def _delete_in_batches(self, sql: str, params: tuple) -> int:
        total = 0
        while True:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            if cur.rowcount <= 0:
                return total
            total += cur.rowcount

    def prune_observations(self, now: int) -> int:
        """Deletes observations older than observations_days in batches of
        BATCH_SIZE. Returns rows deleted."""
        cutoff = now - self._retention.observations_days * 86400
        deleted = self._delete_in_batches(
            "DELETE FROM observations WHERE id IN "
            "(SELECT id FROM observations WHERE server_time < ? LIMIT ?)",
            (cutoff, BATCH_SIZE),
        )
        if deleted:
            logger.info("retention: pruned observations rows=%d", deleted)
        return deleted

    def prune_nodes(self, now: int) -> int:
        """Deletes nodes whose observed_last_seen is older than
        observations_days AND that have no guestbook entry. Returns count."""
        cutoff = now - self._retention.observations_days * 86400
        deleted = self._delete_in_batches(
            "DELETE FROM nodes WHERE node_id IN "
            "(SELECT node_id FROM nodes WHERE MAX(observed_last_seen, identity_updated_at) < ? "
            "AND node_id NOT IN (SELECT DISTINCT sender_node_id FROM guestbook) LIMIT ?)",
            (cutoff, BATCH_SIZE),
        )
        if deleted:
            logger.info("retention: pruned nodes rows=%d", deleted)
        return deleted

    def prune_processed_requests(self, now: int) -> int:
        """Deletes rows whose expires_at < now AND whose state is terminal."""
        placeholders = ",".join("?" * len(_TERMINAL_REQUEST_STATES))
        deleted = self._delete_in_batches(
            "DELETE FROM processed_requests WHERE (sender_node_id, packet_id) IN "
            "(SELECT sender_node_id, packet_id FROM processed_requests "
            f"WHERE expires_at < ? AND state IN ({placeholders}) "
            "AND NOT EXISTS (SELECT 1 FROM daily_stats d WHERE d.finalized = 0 "
            "AND d.day = strftime('%Y-%m-%d', first_seen_at, 'unixepoch')) LIMIT ?)",
            (now, *_TERMINAL_REQUEST_STATES, BATCH_SIZE),
        )
        if deleted:
            logger.info("retention: pruned processed_requests rows=%d", deleted)
        return deleted

    def age_stale_submitted(self, now: int, ack_timeout_seconds: int) -> int:
        """Ages `submitted` rows whose updated_at is older than
        ack_timeout_seconds to `unknown`, in bounded batches. A reply whose
        matching ROUTING packet never arrives (or arrives but never
        matches) would otherwise sit in `submitted` forever: never
        terminal, and so never pruned. `unknown` is terminal, so retention
        prunes it once its own expiry window passes. Returns rows aged, so
        callers can bump a reply-outcome metric by that count."""
        cutoff = now - ack_timeout_seconds
        total = 0
        while True:
            cur = self._conn.execute(
                "UPDATE processed_requests SET state = 'unknown', updated_at = ? "
                "WHERE (sender_node_id, packet_id) IN "
                "(SELECT sender_node_id, packet_id FROM processed_requests "
                "WHERE state = 'submitted' AND updated_at < ? LIMIT ?)",
                (now, cutoff, BATCH_SIZE),
            )
            self._conn.commit()
            if cur.rowcount <= 0:
                break
            total += cur.rowcount
        if total:
            logger.info("retention: aged stale submitted requests rows=%d", total)
        return total

    def prune_positions(self, now: int) -> int:
        """Deletes node_positions rows older than observations_days -- same
        window as observations, since positions are just another
        observation stream (append-only, pruned together)."""
        cutoff = now - self._retention.observations_days * 86400
        deleted = self._delete_in_batches(
            "DELETE FROM node_positions WHERE id IN "
            "(SELECT id FROM node_positions WHERE server_time < ? LIMIT ?)",
            (cutoff, BATCH_SIZE),
        )
        if deleted:
            logger.info("retention: pruned node_positions rows=%d", deleted)
        return deleted

    def prune_local_radio_samples(self, now: int) -> int:
        """Deletes local_radio_samples rows older than observations_days."""
        cutoff = now - self._retention.observations_days * 86400
        deleted = self._delete_in_batches(
            "DELETE FROM local_radio_samples WHERE id IN "
            "(SELECT id FROM local_radio_samples WHERE server_time < ? LIMIT ?)",
            (cutoff, BATCH_SIZE),
        )
        if deleted:
            logger.info("retention: pruned local_radio_samples rows=%d", deleted)
        return deleted

    def prune_node_daily(self, now: int) -> int:
        """Deletes node_daily rows older than retention.node_daily_days."""
        cutoff_day = datetime.fromtimestamp(
            now - self._retention.node_daily_days * 86400, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        deleted = self._delete_in_batches(
            "DELETE FROM node_daily WHERE rowid IN "
            "(SELECT rowid FROM node_daily WHERE day < ? LIMIT ?)",
            (cutoff_day, BATCH_SIZE),
        )
        if deleted:
            logger.info("retention: pruned node_daily rows=%d", deleted)
        return deleted

    def prune_links(self, now: int) -> int:
        """Deletes links rows whose last_seen is older than
        retention.links_days -- a link that has gone quiet that long is no
        longer meaningful as a "current" inferred neighbour."""
        cutoff = now - self._retention.links_days * 86400
        deleted = self._delete_in_batches(
            "DELETE FROM links WHERE rowid IN "
            "(SELECT rowid FROM links WHERE last_seen < ? LIMIT ?)",
            (cutoff, BATCH_SIZE),
        )
        if deleted:
            logger.info("retention: pruned links rows=%d", deleted)
        return deleted

    def prune_aggregates(self, now: int) -> int:
        """Deletes daily_stats older than aggregates_days."""
        cutoff_day = datetime.fromtimestamp(
            now - self._retention.aggregates_days * 86400, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        deleted = self._delete_in_batches(
            "DELETE FROM daily_stats WHERE day IN "
            "(SELECT day FROM daily_stats WHERE day < ? LIMIT ?)",
            (cutoff_day, BATCH_SIZE),
        )
        if deleted:
            logger.info("retention: pruned daily_stats rows=%d", deleted)
        return deleted

    def rollup_day(self, day: str) -> None:
        """Recomputes one UTC day's daily_stats row from observations AND
        processed_requests. day is 'YYYY-MM-DD'. Stores no node IDs and no
        message content: the request-derived columns are bounded counts by
        command/state, matching the meaning their names already imply.

        replies_sent counts durable submissions, independent of terminal outcome.
        commands_accepted/rejected and replies_submitted/acked/failed are
        current-state snapshots as of THIS rollup (processed_requests keeps
        no history of a request's earlier states), so a row that resolves
        from 'submitted' to 'acked' between two rollups of the same day
        moves from one bucket to the other -- consistent with every other
        column here being fully recomputed, not incremented.

        A day already marked `finalized` in daily_stats is never
        recomputed: once processed_requests rows for that day start
        expiring and getting pruned (retention.prune_processed_requests),
        recomputing from what remains would silently lower already-final
        counters (e.g. commands_accepted/replies_acked dropping from 1 to
        0) -- most visibly right after a restart, which resets the
        daemon's in-memory _last_rollup_day to None and so re-triggers a
        "yesterday" rollup regardless of whether that day was already
        finalized.
        """
        existing = self._conn.execute(
            "SELECT finalized FROM daily_stats WHERE day = ?", (day,)
        ).fetchone()
        if existing is not None and existing["finalized"]:
            logger.info("retention: skipping rollup of already-finalized day=%s", day)
            return

        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start_ts = int(start.timestamp())
        end_ts = start_ts + 86400
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        outstanding = self._conn.execute(
            "SELECT 1 FROM processed_requests WHERE first_seen_at >= ? AND first_seen_at < ? "
            "AND state IN ('received', 'queued', 'sending', 'submitted') LIMIT 1",
            (start_ts, end_ts),
        ).fetchone()
        finalized = int(day < today and outstanding is None)

        row = self._conn.execute(
            """
            SELECT
                COUNT(*) AS raw_events,
                SUM(CASE WHEN is_duplicate = 0 THEN 1 ELSE 0 END) AS unique_packets,
                COUNT(DISTINCT NULLIF(sender_node_id, '^unknown')) AS distinct_senders,
                SUM(CASE WHEN portnum IN ('TEXT_MESSAGE_APP', 'MESHCORE_DM', 'MESHCORE_CHANNEL')
                    THEN 1 ELSE 0 END) AS text_packets,
                SUM(CASE WHEN rx_snr IS NULL THEN 1 ELSE 0 END) AS b_missing,
                SUM(CASE WHEN rx_snr < -15 THEN 1 ELSE 0 END) AS b_lt_15,
                SUM(CASE WHEN rx_snr >= -15 AND rx_snr < -10 THEN 1 ELSE 0 END) AS b_15_10,
                SUM(CASE WHEN rx_snr >= -10 AND rx_snr < -5 THEN 1 ELSE 0 END) AS b_10_5,
                SUM(CASE WHEN rx_snr >= -5 AND rx_snr < 0 THEN 1 ELSE 0 END) AS b_5_0,
                SUM(CASE WHEN rx_snr >= 0 AND rx_snr < 5 THEN 1 ELSE 0 END) AS b_0_5,
                SUM(CASE WHEN rx_snr >= 5 THEN 1 ELSE 0 END) AS b_ge_5
            FROM observations
            WHERE server_time >= ? AND server_time < ?
            """,
            (start_ts, end_ts),
        ).fetchone()

        req_row = self._conn.execute(
            """
            SELECT
                SUM(CASE WHEN state NOT IN ('received', 'dropped') THEN 1 ELSE 0 END)
                    AS commands_accepted,
                SUM(CASE WHEN state = 'dropped' THEN 1 ELSE 0 END) AS commands_rejected,
                SUM(CASE WHEN state = 'submitted' THEN 1 ELSE 0 END) AS replies_submitted,
                SUM(CASE WHEN submitted_at IS NOT NULL OR state IN ('submitted', 'acked')
                    THEN 1 ELSE 0 END) AS replies_sent,
                SUM(CASE WHEN state = 'acked' THEN 1 ELSE 0 END) AS replies_acked,
                SUM(CASE WHEN state = 'failed' THEN 1 ELSE 0 END) AS replies_failed,
                SUM(CASE WHEN entry_id IS NOT NULL THEN 1 ELSE 0 END) AS entries_created
            FROM processed_requests
            WHERE first_seen_at >= ? AND first_seen_at < ?
            """,
            (start_ts, end_ts),
        ).fetchone()

        # never sum these across days: distinct_senders is per-day only
        snr_bucket_counts = {
            "lt_-15": row["b_lt_15"] or 0,
            "-15_-10": row["b_15_10"] or 0,
            "-10_-5": row["b_10_5"] or 0,
            "-5_0": row["b_5_0"] or 0,
            "0_5": row["b_0_5"] or 0,
            "ge_5": row["b_ge_5"] or 0,
            "missing": row["b_missing"] or 0,
        }

        self._conn.execute(
            """
            INSERT INTO daily_stats
                (day, raw_events, unique_packets, distinct_senders, text_packets,
                 commands_accepted, commands_rejected, replies_submitted,
                 replies_acked, replies_failed, entries_created,
                 snr_bucket_counts, finalized, replies_sent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                raw_events = excluded.raw_events,
                unique_packets = excluded.unique_packets,
                distinct_senders = excluded.distinct_senders,
                text_packets = excluded.text_packets,
                commands_accepted = excluded.commands_accepted,
                commands_rejected = excluded.commands_rejected,
                replies_submitted = excluded.replies_submitted,
                replies_acked = excluded.replies_acked,
                replies_failed = excluded.replies_failed,
                entries_created = excluded.entries_created,
                snr_bucket_counts = excluded.snr_bucket_counts,
                finalized = excluded.finalized,
                replies_sent = excluded.replies_sent
            """,
            (
                day,
                row["raw_events"] or 0,
                row["unique_packets"] or 0,
                row["distinct_senders"] or 0,
                row["text_packets"] or 0,
                req_row["commands_accepted"] or 0,
                req_row["commands_rejected"] or 0,
                req_row["replies_submitted"] or 0,
                req_row["replies_acked"] or 0,
                req_row["replies_failed"] or 0,
                req_row["entries_created"] or 0,
                json.dumps(snr_bucket_counts),
                finalized,
                req_row["replies_sent"] or 0,
            ),
        )
        self._conn.commit()
        logger.info("retention: rolled up daily_stats day=%s", day)

    def storage_report(self) -> StorageReport:
        db_bytes = (
            _file_size(self._db_path)
            + _file_size(self._db_path + "-wal")
            + _file_size(self._db_path + "-shm")
        )
        backup_bytes = _dir_size(self._backup.directory)
        log_bytes = _dir_size(self._log_dir) if self._log_dir else 0
        total_bytes = db_bytes + backup_bytes + log_bytes
        cap_bytes = self._cap_bytes()
        headroom_bytes = cap_bytes - total_bytes

        if cap_bytes > 0 and total_bytes >= CRITICAL_FRACTION * cap_bytes:
            pressure = "critical"
        elif cap_bytes > 0 and total_bytes >= WARN_FRACTION * cap_bytes:
            pressure = "warn"
        else:
            pressure = "ok"

        return StorageReport(
            db_bytes=db_bytes,
            backup_bytes=backup_bytes,
            log_bytes=log_bytes,
            total_bytes=total_bytes,
            cap_bytes=cap_bytes,
            headroom_bytes=headroom_bytes,
            pressure=pressure,
            publication_paused=(pressure == "critical"),
        )

    def _expected_backup_size(self) -> int:
        """Rough size of the copy `backup()` is about to write: the current
        db file plus its WAL (the pages `Connection.backup()` will copy)."""
        return _file_size(self._db_path) + _file_size(self._db_path + "-wal")

    def _preflight_backup_check(self) -> str | None:
        """Refuses to start a backup that would itself blow the storage cap
        or fill the filesystem. Returns a bounded reason code ('storage_cap'
        / 'disk_space'), or None if the backup may proceed."""
        expected = self._expected_backup_size()
        cap = self._cap_bytes()
        if self.storage_report().total_bytes + expected > cap:
            return "storage_cap"
        # Same headroom notion as storage_report's pressure thresholds: keep
        # back the fraction of the cap reserved before "critical" so a
        # backup does not itself push disk usage to the edge.
        margin = int(cap * (1 - CRITICAL_FRACTION))
        free = shutil.disk_usage(self._backup.directory).free
        if free < expected + margin:
            return "disk_space"
        return None

    def backup(self, now: int) -> str | None:
        """Uses sqlite3 Connection.backup() (the supported online backup API),
        writes backups/observatory-YYYYMMDDTHHMMSSZ.db atomically (via a
        same-directory .tmp file and os.replace), then rotates to `keep`
        newest. Returns the created path, or None if the backup was refused
        (storage cap / disk space) or failed. Backups must never crash the
        daemon: failures are recorded in last_backup_error for health/status
        instead of being raised."""
        os.makedirs(self._backup.directory, exist_ok=True)

        reason = self._preflight_backup_check()
        if reason is not None:
            self._last_backup_error = reason
            logger.warning("retention: backup refused reason=%s", reason)
            return None

        final_name = time.strftime("observatory-%Y%m%dT%H%M%SZ.db", time.gmtime(now))
        dest = os.path.join(self._backup.directory, final_name)
        tmp_dest = dest + ".tmp"

        try:
            target = sqlite3.connect(tmp_dest)
            try:
                self._conn.backup(target)  # consistent while WAL is active
            finally:
                target.close()  # explicit: os.replace must not race an open handle
            os.chmod(tmp_dest, 0o600)
            os.replace(tmp_dest, dest)  # atomic: only a complete copy gets the final name
        except (OSError, sqlite3.Error) as exc:
            self._last_backup_error = type(exc).__name__
            logger.warning("retention: backup failed reason=%s", self._last_backup_error)
            for partial in (tmp_dest, dest):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(partial)
            return None

        self._rotate_backups()
        self._last_backup_at = now
        self._last_backup_error = None
        logger.info("retention: backup created")
        return dest

    def _rotate_backups(self) -> None:
        names = sorted(
            n
            for n in os.listdir(self._backup.directory)
            if n.startswith("observatory-") and not n.endswith(".tmp")
        )
        excess = max(0, len(names) - self._backup.keep)
        for name in names[:excess]:
            os.remove(os.path.join(self._backup.directory, name))

    def due_for_backup(self, now: int) -> bool:
        if self._last_backup_at is None:
            return True
        return now - self._last_backup_at >= self._backup.interval_hours * 3600
