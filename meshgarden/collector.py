"""Observation persistence: raw-vs-unique packet dedup and observed node tracking.

Called from the DB worker thread with the daemon's single write connection.
Never opens its own connection, never spawns threads, and never persists
packet text/content -- only bounded counters and structural metadata.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from meshgarden.config import NODE_ID_RE, node_id_from_num
from meshgarden.fortune import local_day
from meshgarden.models import IncomingPacket, MeshCoreObservation, MeshCoreRoute

DEDUP_WINDOW_SECONDS: int = 600  # bounded window; packet ids are not unique forever

BROADCAST_NUM = 0xFFFFFFFF

# SEEN's <node> argument: a bare 4-hex tail (no "!" prefix, no full 8-hex
# id) -- see Collector.resolve_node.
_NODE_TAIL_RE = re.compile(r"^[0-9a-fA-F]{4}$")


@dataclass(frozen=True)
class NodeResolution:
    node_id: str | None
    ambiguous: bool


EARTH_RADIUS_KM = 6371.0088  # IUGG mean radius; plenty for a "furthest heard" estimate

# (0.0, 0.0) is config.ObservatoryConfig's documented "unset" sentinel: real
# operator coordinates are validated to (-90..90, -180..180) at config load
# time, so this exact pair can only mean "never configured".
_OBSERVATORY_UNSET = (0.0, 0.0)

# A POSITION_APP precisionBits value below this corresponds to a roughly
# 10+ km uncertainty cell (Meshtastic halves the cell size per extra bit) --
# too coarse to trust for a "furthest" distance record. precision_bits is
# only ever compared against this floor when it is present: older firmware
# omits the field entirely, and an unknown precision is treated as usable
# rather than rejected (see is_position_plausible).
MIN_PLAUSIBLE_PRECISION_BITS = 10


def is_position_plausible(
    lat: float | None, lon: float | None, precision_bits: int | None
) -> bool:
    """Position plausibility (operator ruling, distance-plausibility fix):
    usable only if both lat/lon are present and within valid ranges, not
    the exact (0.0, 0.0) sentinel (Meshtastic's protobuf encoding cannot
    tell an exact 0,0 fix from "absent" -- see collector.record), and
    precision_bits, when known, is not below MIN_PLAUSIBLE_PRECISION_BITS.
    A missing precision_bits (older firmware never sent it) is treated as
    usable, never rejected -- only a known, too-coarse value is."""
    if lat is None or lon is None:
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return False
    if (lat, lon) == _OBSERVATORY_UNSET:
        return False
    return precision_bits is None or precision_bits >= MIN_PLAUSIBLE_PRECISION_BITS


def is_distance_plausible_for_hops(
    distance_km: float,
    hops_away: int | None,
    *,
    max_direct_km: float,
    max_km_per_hop: float,
    max_observed_km: float,
) -> bool:
    """Hop/distance agreement (operator ruling, distance-plausibility fix).

    `hops_away` is None (unknown) -> never eligible for a "furthest"
    record, regardless of distance. `hops_away == 0` (direct) -> plausible
    only up to `max_direct_km`. `hops_away >= 1` (routed) -> plausible up
    to `min(max_observed_km, hops_away * max_km_per_hop)`, i.e. bounded
    both by a per-hop budget and by the absolute ceiling."""
    if hops_away is None:
        return False
    if hops_away == 0:
        return distance_km <= max_direct_km
    return distance_km <= min(max_observed_km, hops_away * max_km_per_hop)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometers.
    Positions here are voluntarily advertised by the sending node and may be
    coarse (see README) -- this is an estimate, not a survey measurement."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


class Collector:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        dedup_window_seconds: int = DEDUP_WINDOW_SECONDS,
        enabled_position_capture: bool = True,
    ) -> None:
        self._conn = conn
        self._dedup_window_seconds = dedup_window_seconds
        # Local capture toggle only (see config.ObservatoryConfig): when
        # False, POSITION_APP fields are still parsed by radio.extract but
        # never written to node_positions/nodes/node_daily.
        self._enabled_position_capture = enabled_position_capture

    def record_meshcore(
        self, event: MeshCoreObservation, *, my_node_num: int | None = None
    ) -> bool:
        """Persist companion metadata without inventing Meshtastic hop/identity fields."""
        obs = IncomingPacket(
            received_at=event.received_at,
            received_monotonic=0,
            from_num=0,
            to_num=0,
            packet_id=event.packet_id,
            channel=event.channel,
            portnum=event.kind,
            rx_time=None,
            rx_snr=event.snr,
            rx_rssi=event.rssi,
            hop_limit=None,
            hop_start=None,
            via_mqtt=False,
            pki_encrypted=False,
            payload_len=event.payload_len,
            text=None,
            request_id=None,
            routing_error=None,
        )
        return self.record(obs, meshcore=event, my_node_num=my_node_num)

    def record(
        self,
        obs: IncomingPacket,
        *,
        my_node_num: int | None = None,
        meshcore: MeshCoreObservation | None = None,
    ) -> bool:
        """Persists one observation. Returns True if it was a NEW unique packet,
        False if it was a duplicate raw event. NEVER writes obs.text.

        `my_node_num` (our own radio's node number, when known) enables two
        observatory-only, best-effort features: inferring a direct `links`
        edge to us, and passively sampling our own node's telemetry into
        `local_radio_samples`. Both are skipped (not an error) when it is
        None -- e.g. before the radio has completed identity verification.
        """
        sender_node_id = node_id_from_num(obs.from_num)
        dest_node_id = (
            "^all" if obs.to_num == BROADCAST_NUM else node_id_from_num(obs.to_num)
        )

        if meshcore is not None:
            sender_node_id = meshcore.sender or "^unknown"
            dest_node_id = meshcore.recipient
        transport = "meshcore" if meshcore is not None else "meshtastic"
        is_duplicate = False
        if obs.packet_id is not None:
            since = obs.received_at - self._dedup_window_seconds
            row = self._conn.execute(
                "SELECT 1 FROM observations "
                "WHERE sender_node_id=? AND packet_id=? AND transport=? "
                "AND server_time >= ? LIMIT 1",
                (sender_node_id, obs.packet_id, transport, since),
            ).fetchone()
            is_duplicate = row is not None

        hops_away = None
        if obs.hop_start is not None and obs.hop_limit is not None:
            diff = obs.hop_start - obs.hop_limit
            if diff >= 0:
                hops_away = diff

        if meshcore is not None:
            hops_away = meshcore.hops

        via_mqtt = None if obs.via_mqtt is None else int(obs.via_mqtt)

        # NOTE: obs.text is intentionally never read or written here.
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO observations (
                    server_time, packet_time, sender_node_id, dest_node_id,
                    channel, portnum, packet_id, is_duplicate,
                    rx_snr, rx_rssi, hop_limit, hop_start, hops_away,
                    via_mqtt, pki_encrypted, payload_len, transport, routing_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    obs.received_at,
                    obs.rx_time,
                    sender_node_id,
                    dest_node_id,
                    obs.channel,
                    obs.portnum,
                    obs.packet_id,
                    int(is_duplicate),
                    obs.rx_snr,
                    obs.rx_rssi,
                    obs.hop_limit,
                    obs.hop_start,
                    hops_away,
                    via_mqtt,
                    int(bool(obs.pki_encrypted)),
                    obs.payload_len,
                    transport,
                    meshcore.routing_mode if meshcore is not None else None,
                ),
            )
            if sender_node_id == "^unknown":
                return not is_duplicate
            self._conn.execute(
                """
                INSERT INTO nodes
                    (node_id, observed_first_seen, observed_last_seen, observation_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(node_id) DO UPDATE SET
                    observed_first_seen = CASE WHEN observed_first_seen=0
                        THEN excluded.observed_first_seen
                        ELSE min(observed_first_seen, excluded.observed_first_seen) END,
                    observed_last_seen = max(observed_last_seen, excluded.observed_last_seen),
                    observation_count = observation_count + 1
                """,
                (sender_node_id, obs.received_at, obs.received_at),
            )

            # Honest-statistics rule: a duplicate reception (same
            # sender+packet_id within the dedup window, e.g. relayed via
            # two different paths) is still recorded above as its own raw
            # `observations` row (is_duplicate=1) -- but it is NOT a
            # genuinely distinct packet, so it must not inflate
            # node_daily's accumulators, append a second node_positions
            # history row, or double-count a links edge.
            self._record_node_snapshot(sender_node_id, obs, record_position=not is_duplicate)
            if not is_duplicate:
                self._record_node_daily(sender_node_id, obs, hops_away)
                if hops_away == 0 and my_node_num is not None:
                    self._record_direct_link(sender_node_id, my_node_num, obs.received_at)
            if my_node_num is not None and obs.from_num == my_node_num:
                self._record_local_radio_sample(obs)

        return not is_duplicate

    def _record_node_snapshot(
        self, sender_node_id: str, obs: IncomingPacket, *, record_position: bool
    ) -> None:
        """Rolling nodes-table snapshot columns from structured NODEINFO_APP/
        POSITION_APP/TELEMETRY_APP fields -- COALESCE keeps the prior value
        when this particular observation didn't carry that field, rather
        than blanking it.

        `record_position` gates only the `node_positions` history insert
        (and its paired `nodes.last_position_*` snapshot update): a
        duplicate reception carries the same position as the original and
        must not append a second history row (see `record`'s honest-
        statistics rule). hw_model/role/battery snapshot updates are
        unaffected -- they overwrite with the same value either way, so
        gating them buys nothing."""
        if obs.hw_model is not None or obs.role is not None:
            self._conn.execute(
                "UPDATE nodes SET hw_model = COALESCE(?, hw_model), "
                "role = COALESCE(?, role) WHERE node_id = ?",
                (obs.hw_model, obs.role, sender_node_id),
            )
        if (
            record_position
            and self._enabled_position_capture
            and obs.lat is not None
            and obs.lon is not None
        ):
            self._conn.execute(
                "INSERT INTO node_positions "
                "(node_id, server_time, lat, lon, alt, precision_bits, reported_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    sender_node_id,
                    obs.received_at,
                    obs.lat,
                    obs.lon,
                    obs.position_alt,
                    obs.position_precision_bits,
                    obs.position_time,
                ),
            )
            self._conn.execute(
                "UPDATE nodes SET last_position_lat = ?, last_position_lon = ?, "
                "last_position_at = ? WHERE node_id = ?",
                (obs.lat, obs.lon, obs.received_at, sender_node_id),
            )
        if obs.battery_level is not None:
            self._conn.execute(
                "UPDATE nodes SET last_battery = ? WHERE node_id = ?",
                (obs.battery_level, sender_node_id),
            )

    def _record_node_daily(
        self, sender_node_id: str, obs: IncomingPacket, hops_away: int | None
    ) -> None:
        """Incremental per-node-per-UTC-day rollup. best_snr/best_hops are
        running MIN/MAX; typical_snr/typical_hops are running means, kept
        incremental via a sum+count accumulator (an exact running median
        would need to retain every sample -- overkill here)."""
        day = datetime.fromtimestamp(obs.received_at, tz=UTC).strftime("%Y-%m-%d")
        direct = 1 if hops_away == 0 else 0
        snr = obs.rx_snr
        snr_count = 1 if snr is not None else 0
        hops_count = 1 if hops_away is not None else 0
        last_lat = obs.lat if self._enabled_position_capture else None
        last_lon = obs.lon if self._enabled_position_capture else None
        self._conn.execute(
            """
            INSERT INTO node_daily (
                node_id, day, packets, direct_packets,
                best_snr, snr_sum, snr_count, typical_snr,
                best_hops, hops_sum, hops_count, typical_hops,
                last_lat, last_lon
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id, day) DO UPDATE SET
                packets = packets + 1,
                direct_packets = direct_packets + excluded.direct_packets,
                best_snr = CASE
                    WHEN excluded.best_snr IS NULL THEN best_snr
                    WHEN best_snr IS NULL THEN excluded.best_snr
                    ELSE MAX(best_snr, excluded.best_snr) END,
                snr_sum = snr_sum + excluded.snr_sum,
                snr_count = snr_count + excluded.snr_count,
                typical_snr = CASE WHEN snr_count + excluded.snr_count > 0
                    THEN (snr_sum + excluded.snr_sum) / (snr_count + excluded.snr_count)
                    ELSE NULL END,
                best_hops = CASE
                    WHEN excluded.best_hops IS NULL THEN best_hops
                    WHEN best_hops IS NULL THEN excluded.best_hops
                    ELSE MIN(best_hops, excluded.best_hops) END,
                hops_sum = hops_sum + excluded.hops_sum,
                hops_count = hops_count + excluded.hops_count,
                typical_hops = CASE WHEN hops_count + excluded.hops_count > 0
                    THEN CAST(hops_sum + excluded.hops_sum AS REAL)
                        / (hops_count + excluded.hops_count)
                    ELSE NULL END,
                last_lat = CASE WHEN excluded.last_lat IS NULL THEN last_lat
                    ELSE excluded.last_lat END,
                last_lon = CASE WHEN excluded.last_lon IS NULL THEN last_lon
                    ELSE excluded.last_lon END
            """,
            (
                sender_node_id,
                day,
                direct,
                snr,
                snr if snr is not None else 0,
                snr_count,
                snr,
                hops_away,
                hops_away if hops_away is not None else 0,
                hops_count,
                float(hops_away) if hops_away is not None else None,
                last_lat,
                last_lon,
            ),
        )

    def _record_direct_link(
        self, sender_node_id: str, my_node_num: int, observed_at: int
    ) -> None:
        my_node_id = node_id_from_num(my_node_num)
        if sender_node_id == my_node_id:
            return  # never a self-link
        self._conn.execute(
            """
            INSERT INTO links (a_node_id, b_node_id, first_seen, last_seen, count, kind)
            VALUES (?, ?, ?, ?, 1, 'inferred_direct')
            ON CONFLICT(a_node_id, b_node_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                count = count + 1
            """,
            (sender_node_id, my_node_id, observed_at, observed_at),
        )

    def _record_local_radio_sample(self, obs: IncomingPacket) -> None:
        """Passive: only ever fires when WE are the sender of a
        TELEMETRY_APP packet we ourselves heard flow through the same
        receive path as every other observation -- no separate polling."""
        if obs.portnum != "TELEMETRY_APP":
            return
        if (
            obs.channel_utilization is None
            and obs.air_util_tx is None
            and obs.battery_level is None
        ):
            return
        self._conn.execute(
            "INSERT INTO local_radio_samples "
            "(server_time, channel_utilization, air_util_tx, battery) VALUES (?, ?, ?, ?)",
            (obs.received_at, obs.channel_utilization, obs.air_util_tx, obs.battery_level),
        )

    def update_node_names(
        self,
        node_id: str,
        *,
        short_name: str | None,
        long_name: str | None,
        observed_at: int,
    ) -> None:
        """Only called for NODEINFO_APP packets WE observed. Imported node-DB
        records must never reach this method.

        Imported node records do not update observed last_seen (spec): this
        only UPDATEs an existing row's names, never INSERTs, and never
        touches observed_first_seen/observed_last_seen/observation_count.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE nodes SET short_name=?, long_name=? WHERE node_id=?",
                (short_name, long_name, node_id),
            )

    def record_meshcore_route(self, route: MeshCoreRoute) -> None:
        """Persist outgoing cache metadata without adding heard times or RF links."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO nodes (node_id, observed_first_seen, observed_last_seen, "
                "observation_count, stored_route) VALUES (?, 0, 0, 0, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET stored_route=excluded.stored_route",
                (route.node_id, json.dumps({"hops": route.hops, "hashes": route.hashes,
                                           "cached_at": route.cached_at})),
            )

    def record_contact_identity(
        self,
        node_id: str,
        *,
        display_name: str | None,
        lat: float | None,
        lon: float | None,
        observed_at: int,
    ) -> None:
        """Store contact metadata without claiming a live reception.

        Both imported and live contacts enter here. Only observation events
        update heard times. Zero means no reception yet in the legacy NOT NULL
        nodes schema. Positions are capture-time snapshots, not GPS fix times.
        """
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO nodes
                    (node_id, observed_first_seen, observed_last_seen, observation_count)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(node_id) DO NOTHING
                """,
                (node_id, 0, 0),
            )
            self._conn.execute(
                "UPDATE nodes SET identity_updated_at = ? WHERE node_id = ?",
                (observed_at, node_id),
            )
            if display_name is not None:
                self._conn.execute(
                    "UPDATE nodes SET short_name = ? WHERE node_id = ?",
                    (display_name, node_id),
                )
            if lat is not None and lon is not None and self._enabled_position_capture:
                self._conn.execute(
                    "INSERT INTO node_positions(node_id, server_time, lat, lon) "
                    "VALUES (?, ?, ?, ?)",
                    (node_id, observed_at, lat, lon),
                )
                self._conn.execute(
                    "UPDATE nodes SET last_position_lat = ?, last_position_lon = ?, "
                    "last_position_at = ? WHERE node_id = ?",
                    (lat, lon, observed_at, node_id),
                )

    def resolve_node(
        self,
        identifier: str,
        *,
        parse_node_argument: Callable[[str], str | None] | None = None,
    ) -> NodeResolution:
        """Resolves SEEN's/MAIL's <node> argument: a full "!xxxxxxxx" id
        always resolves to itself (whether or not we've observed it); a
        bare 4-hex tail or an exact short name (case-insensitive) is looked
        up against the `nodes` table -- 0 matches is NOT_FOUND (ambiguous=
        False, node_id=None), 1 match resolves, >=2 matches is AMBIGUOUS
        (node_id=None, ambiguous=True). Never queries anything but our own
        observed `nodes` table.

        `parse_node_argument` (production callers pass
        `transport.parse_node_argument`) decides whether the argument is
        already a full, canonical id in the running transport's own shape;
        it defaults to the Meshtastic `NODE_ID_RE` check so a caller (and
        every existing direct test of this method) that doesn't pass one
        keeps today's behavior unchanged.
        """
        text = identifier.strip()
        full_id = (
            parse_node_argument(text)
            if parse_node_argument is not None
            else (text.lower() if NODE_ID_RE.match(text) else None)
        )
        if full_id is not None:
            return NodeResolution(full_id, False)
        if _NODE_TAIL_RE.match(text):
            rows = self._conn.execute(
                "SELECT node_id FROM nodes WHERE lower(substr(node_id, -4)) = ?",
                (text.lower(),),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT node_id FROM nodes WHERE short_name IS NOT NULL "
                "AND lower(short_name) = lower(?)",
                (text,),
            ).fetchall()
        if not rows:
            return NodeResolution(None, False)
        if len(rows) > 1:
            return NodeResolution(None, True)
        return NodeResolution(rows[0]["node_id"], False)

    def last_observation(self, node_id: str) -> sqlite3.Row | None:
        """Most recent observation row for `node_id` (any time window),
        for SEEN's how-heard/SNR fields. None if never observed."""
        return self._conn.execute(
            "SELECT server_time, rx_snr, hops_away FROM observations "
            "WHERE sender_node_id = ? ORDER BY server_time DESC LIMIT 1",
            (node_id,),
        ).fetchone()

    def days_seen_last_n(self, node_id: str, today: str, n: int = 7) -> int:
        """Distinct node_daily days for `node_id` within the last `n` UTC
        calendar days counting back from `today` (a "YYYY-MM-DD" string,
        inclusive)."""
        cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=n - 1)).strftime(
            "%Y-%m-%d"
        )
        row = self._conn.execute(
            "SELECT COUNT(*) FROM node_daily WHERE node_id = ? AND day >= ? AND day <= ?",
            (node_id, cutoff, today),
        ).fetchone()
        return row[0]

    def first_seen_date(self, node_id: str, tz: str) -> str | None:
        """`nodes.observed_first_seen` rendered as a local calendar day
        (fortune.local_day), or None if the node has never been observed."""
        row = self._conn.execute(
            "SELECT observed_first_seen FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None or not row["observed_first_seen"]:
            return None
        return local_day(row["observed_first_seen"], tz)

    def raw_events_since(self, since: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM observations WHERE server_time >= ?", (since,)
        ).fetchone()
        return row[0]

    def unique_packets_since(self, since: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM observations WHERE server_time >= ? AND is_duplicate=0",
            (since,),
        ).fetchone()
        return row[0]

    def distinct_senders_since(self, since: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT NULLIF(sender_node_id, '^unknown')) "
            "FROM observations WHERE server_time >= ?",
            (since,),
        ).fetchone()
        return row[0]

    def last_packet_at(self) -> int | None:
        row = self._conn.execute("SELECT MAX(server_time) FROM observations").fetchone()
        return row[0]

    def first_packet_at(self) -> int | None:
        """Epoch of our EARLIEST observation ever, None if we have recorded
        none -- SEEN's "not heard" reply (main.py's `_handle_seen`) uses this
        so it never claims a window longer than we have actually been
        listening."""
        row = self._conn.execute("SELECT MIN(server_time) FROM observations").fetchone()
        return row[0]

    def sender_observations_since(self, sender_node_id: str, since: int) -> int:
        """Count of observations recorded from `sender_node_id` (any packet
        type) at or after `since` -- used by ME's (also reachable as
        WHOAMI/SIGNAL) "N×/24h" count."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM observations WHERE sender_node_id=? AND server_time >= ?",
            (sender_node_id, since),
        ).fetchone()
        return row[0]

    def distinct_direct_senders_since(self, since: int) -> int:
        """Distinct senders heard with zero derived hops (hop_start ==
        hop_limit, both present) at or after `since`. Used by GARDEN's
        "heard direct" count."""
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT NULLIF(sender_node_id, '^unknown')) FROM observations "
            "WHERE server_time >= ? AND hops_away = 0",
            (since,),
        ).fetchone()
        return row[0]

    def best_snr_since(self, since: int) -> float | None:
        """Highest reported rx_snr at or after `since`, or None if no
        packet with a reported SNR exists in the window."""
        row = self._conn.execute(
            "SELECT MAX(rx_snr) FROM observations WHERE server_time >= ? AND rx_snr IS NOT NULL",
            (since,),
        ).fetchone()
        return row[0]

    def best_snr_with_sender_since(self, since: int) -> tuple[float, str | None] | None:
        """(best rx_snr, its sender_node_id) at or after `since`, or None if
        no packet with a reported SNR exists in the window. Used by RECORD,
        which names only the node id tail, never logs or stores anything
        new."""
        row = self._conn.execute(
            "SELECT sender_node_id, rx_snr FROM observations "
            "WHERE server_time >= ? AND rx_snr IS NOT NULL "
            "ORDER BY rx_snr DESC, id ASC LIMIT 1",
            (since,),
        ).fetchone()
        if row is None:
            return None
        sender = None if row["sender_node_id"] == "^unknown" else row["sender_node_id"]
        return (row["rx_snr"], sender)

    def max_hops_since(self, since: int) -> int | None:
        """Highest derived hop count (hop_start - hop_limit, only when both
        were present) at or after `since`, or None if none observed."""
        row = self._conn.execute(
            "SELECT MAX(hops_away) FROM observations WHERE server_time >= ? "
            "AND hops_away IS NOT NULL",
            (since,),
        ).fetchone()
        return row[0]

    def max_hops_with_sender_since(self, since: int) -> tuple[int, str | None] | None:
        """(highest received hop count, its sender) at or after `since`.

        Ties use the earliest observation, matching the deterministic RECORD
        treatment of best SNR. Unknown senders are returned as None.
        """
        row = self._conn.execute(
            "SELECT sender_node_id, hops_away FROM observations "
            "WHERE server_time >= ? AND hops_away IS NOT NULL "
            "ORDER BY hops_away DESC, id ASC LIMIT 1",
            (since,),
        ).fetchone()
        if row is None:
            return None
        sender = None if row["sender_node_id"] == "^unknown" else row["sender_node_id"]
        return (row["hops_away"], sender)

    def busiest_hour_since(self, since: int, tz: str) -> int | None:
        """The local hour (0-23, in IANA zone `tz`) with the most packets at
        or after `since`, or None if there are none in the window. Ties
        break toward the earlier hour, so the result is deterministic.
        Bounded by the caller's own `since` window (RECORD always passes
        local midnight), so this never scans more than one day's rows."""
        rows = self._conn.execute(
            "SELECT server_time FROM observations WHERE server_time >= ?", (since,)
        ).fetchall()
        if not rows:
            return None
        zone = ZoneInfo(tz)
        counts = Counter(datetime.fromtimestamp(r[0], tz=zone).hour for r in rows)
        best_hour, _ = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return best_hour

    def _position_candidates_since(
        self, since: int, *, direct_only: bool, require_rf: bool
    ) -> list[sqlite3.Row]:
        """One row per (sender_node_id, hops_away) pair observed at or after
        `since` that matches the requested filters, joined to that node's
        latest known position (lat/lon/precision_bits, from `node_positions`
        -- the same "current position" `nodes.last_position_lat/lon` already
        reflects, but paired with the precision_bits `nodes` itself does not
        store).

        `require_rf` restricts to observations that are known RF: MeshCore
        rows (that transport has no MQTT path, so it never sets via_mqtt --
        see usefulness.py's rf_clause) or Meshtastic rows with
        `via_mqtt = 0` (per the operator's distance-plausibility ruling);
        Meshtastic `via_mqtt = 1` and `NULL` (unknown origin) are excluded
        when True.
        `direct_only` additionally restricts to `hops_away = 0`. Multiple
        rows per node (one per distinct hops_away it was observed at) are
        intentional: the caller checks hop/distance plausibility per row,
        so a node heard both direct and via relay gets an independent
        plausibility check for each."""
        clauses = ["o.server_time >= ?"]
        params: list[int] = [since]
        if require_rf:
            clauses.append("(o.transport = 'meshcore' OR o.via_mqtt = 0)")
        if direct_only:
            clauses.append("o.hops_away = 0")
        where = " AND ".join(clauses)
        return self._conn.execute(
            f"""
            SELECT DISTINCT o.sender_node_id AS node_id, o.hops_away AS hops_away,
                   latest.lat AS lat, latest.lon AS lon,
                   latest.precision_bits AS precision_bits
            FROM observations o
            JOIN (
                SELECT np.node_id, np.lat, np.lon, np.precision_bits
                FROM node_positions np
                JOIN (
                    SELECT node_id, MAX(server_time) AS max_t FROM node_positions GROUP BY node_id
                ) latest_t ON latest_t.node_id = np.node_id AND latest_t.max_t = np.server_time
            ) latest ON latest.node_id = o.sender_node_id
            WHERE {where}
            """,
            params,
        ).fetchall()

    def furthest_direct_km_since(
        self,
        since: int,
        *,
        observatory_lat: float,
        observatory_lon: float,
        max_direct_km: float,
        max_km_per_hop: float,
        max_observed_km: float,
    ) -> float | None:
        """Farthest PLAUSIBLE known position among nodes heard DIRECT
        (derived hops_away == 0), explicitly RF (via_mqtt == 0), at or
        after `since`, in km from the configured observatory location.
        "Plausible" means both `is_position_plausible` and
        `is_distance_plausible_for_hops` (against the given thresholds,
        normally `[observatory] max_direct_km`/`max_km_per_hop`/
        `max_observed_km`) -- an MQTT-relayed, unknown-origin, or
        physically-implausible-for-its-hop-count candidate never counts.
        None when the observatory location is unset ((0.0, 0.0), see
        config.ObservatoryConfig) or no candidate is both known and
        plausible -- GARDEN omits the field in either case."""
        if (observatory_lat, observatory_lon) == _OBSERVATORY_UNSET:
            return None
        rows = self._position_candidates_since(since, direct_only=True, require_rf=True)
        best_km: float | None = None
        for r in rows:
            if not is_position_plausible(r["lat"], r["lon"], r["precision_bits"]):
                continue
            km = haversine_km(observatory_lat, observatory_lon, r["lat"], r["lon"])
            if not is_distance_plausible_for_hops(
                km,
                r["hops_away"],
                max_direct_km=max_direct_km,
                max_km_per_hop=max_km_per_hop,
                max_observed_km=max_observed_km,
            ):
                continue
            if best_km is None or km > best_km:
                best_km = km
        return best_km

    def furthest_observed_km_with_node_since(
        self,
        since: int,
        *,
        observatory_lat: float,
        observatory_lon: float,
        max_direct_km: float,
        max_km_per_hop: float,
        max_observed_km: float,
    ) -> tuple[float, str] | None:
        """Farthest PLAUSIBLE known position among ANY node observed (any
        KNOWN hop count -- `hops_away IS NULL` is never eligible),
        explicitly RF (via_mqtt == 0), at or after `since`, paired with its
        node id -- RECORD shows only the id's tail (see
        commands._node_id_tail), never the full id. See
        `furthest_direct_km_since` for what "plausible" means and the same
        None conditions. For the dashboard-only figure that also counts
        MQTT-relayed/unknown-origin candidates, see
        `furthest_observed_incl_mqtt_km_with_node_since`."""
        if (observatory_lat, observatory_lon) == _OBSERVATORY_UNSET:
            return None
        rows = self._position_candidates_since(since, direct_only=False, require_rf=True)
        best: tuple[float, str] | None = None
        for r in rows:
            if not is_position_plausible(r["lat"], r["lon"], r["precision_bits"]):
                continue
            km = haversine_km(observatory_lat, observatory_lon, r["lat"], r["lon"])
            if not is_distance_plausible_for_hops(
                km,
                r["hops_away"],
                max_direct_km=max_direct_km,
                max_km_per_hop=max_km_per_hop,
                max_observed_km=max_observed_km,
            ):
                continue
            if best is None or km > best[0]:
                best = (km, r["node_id"])
        return best

    def furthest_observed_incl_mqtt_km_with_node_since(
        self,
        since: int,
        *,
        observatory_lat: float,
        observatory_lon: float,
        max_observed_km: float,
    ) -> tuple[float, str] | None:
        """Dashboard-only figure: farthest known PLAUSIBLE *position* (see
        `is_position_plausible`) within `max_observed_km` among ANY node
        observed at or after `since`, regardless of via_mqtt (MQTT-relayed
        and unknown-origin packets included) and regardless of hop/distance
        agreement -- the very candidates
        `furthest_observed_km_with_node_since` excludes. Deliberately NOT
        hop-checked: those packets may have come in over an MQTT bridge,
        where the mesh-side hop count says nothing about the physical
        distance. The absolute ceiling still applies: a position further
        than any radio on this mesh could ever reach is a bridged or bogus
        node, not a record, and used to win every window on the dashboards.
        Must be labelled "furthest incl. MQTT-relayed/unknown" wherever it
        is shown -- it is not a verified RF distance record. None under the
        same "no observatory location"/"no plausible candidate" conditions
        as the strict figure."""
        if (observatory_lat, observatory_lon) == _OBSERVATORY_UNSET:
            return None
        rows = self._position_candidates_since(since, direct_only=False, require_rf=False)
        best: tuple[float, str] | None = None
        seen_node_ids: set[str] = set()
        for r in rows:
            if r["node_id"] in seen_node_ids:
                continue  # same node/position regardless of which hops_away row matched
            if not is_position_plausible(r["lat"], r["lon"], r["precision_bits"]):
                continue
            seen_node_ids.add(r["node_id"])
            km = haversine_km(observatory_lat, observatory_lon, r["lat"], r["lon"])
            if km > max_observed_km:
                continue
            if best is None or km > best[0]:
                best = (km, r["node_id"])
        return best
