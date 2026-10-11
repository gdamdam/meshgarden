import dataclasses

import pytest

from meshgarden.collector import (
    Collector,
    haversine_km,
    is_distance_plausible_for_hops,
    is_position_plausible,
)
from meshgarden.database import connect, migrate
from meshgarden.radio import extract
from tests.fakes import make_packet

# Explicit operator thresholds for the distance-filter tests below.
# Default configuration is tested separately.
_THRESH = {"max_direct_km": 300.0, "max_km_per_hop": 150.0, "max_observed_km": 1000.0}


def mk(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    return conn, Collector(conn)


def obs(**kw):
    now = kw.pop("now", 1000)
    now_monotonic = kw.pop("now_monotonic", 1.0)
    return extract(make_packet(**kw), now=now, now_monotonic=now_monotonic)


def test_message_text_is_never_persisted(tmp_path):
    conn, c = mk(tmp_path)
    c.record(extract(make_packet(text="SIGN my secret message"), now=1000, now_monotonic=1.0))
    # Restrict to actual data rows: full conn.iterdump() also includes schema
    # DDL comments (e.g. processed_requests.command's enum doc mentions
    # "SIGN"), which are static schema text, not persisted packet content.
    dump = "\n".join(line for line in conn.iterdump() if line.startswith("INSERT"))
    assert "secret" not in dump
    assert "SIGN" not in dump


def test_duplicate_packet_id_counts_as_raw_event_not_unique(tmp_path):
    conn, c = mk(tmp_path)
    o = extract(make_packet(packet_id=99, text="hi"), now=1000, now_monotonic=1.0)
    assert c.record(o) is True
    assert c.record(o) is False
    assert c.raw_events_since(0) == 2
    assert c.unique_packets_since(0) == 1


def test_same_packet_id_outside_window_is_unique_again(tmp_path):
    conn, c = mk(tmp_path)
    c.record(extract(make_packet(packet_id=99), now=1000, now_monotonic=1.0))
    later = extract(make_packet(packet_id=99), now=1000 + 601, now_monotonic=2.0)
    assert c.record(later) is True


def test_packet_without_id_is_always_unique(tmp_path):
    conn, c = mk(tmp_path)
    assert c.record(extract(make_packet(packet_id=None), now=1, now_monotonic=1.0)) is True
    assert c.record(extract(make_packet(packet_id=None), now=1, now_monotonic=1.0)) is True


def test_missing_signal_stored_as_null_not_zero(tmp_path):
    conn, c = mk(tmp_path)
    c.record(extract(make_packet(), now=1000, now_monotonic=1.0))
    row = conn.execute("SELECT rx_snr, rx_rssi, via_mqtt FROM observations").fetchone()
    # rx_snr/rx_rssi have no RF-default meaning when absent, so they stay
    # NULL; via_mqtt now defaults to explicit RF (0) for a decoded packet --
    # see test_radio.py's test_via_mqtt_absent_is_rf_not_unknown.
    assert row["rx_snr"] is None and row["rx_rssi"] is None
    assert row["via_mqtt"] == 0


def test_hops_away_derived_only_when_both_present(tmp_path):
    conn, c = mk(tmp_path)
    c.record(extract(make_packet(hop_start=3, hop_limit=1), now=1, now_monotonic=1.0))
    c.record(extract(make_packet(packet_id=5, hop_start=3), now=1, now_monotonic=1.0))
    rows = [r["hops_away"] for r in conn.execute(
        "SELECT hops_away FROM observations ORDER BY id")]
    assert rows == [2, None]


def test_node_first_and_last_seen_come_from_observations(tmp_path):
    conn, c = mk(tmp_path)
    c.record(extract(make_packet(packet_id=1), now=1000, now_monotonic=1.0))
    c.record(extract(make_packet(packet_id=2), now=2000, now_monotonic=2.0))
    row = conn.execute("SELECT * FROM nodes WHERE node_id='!aabbccdd'").fetchone()
    assert row["observed_first_seen"] == 1000
    assert row["observed_last_seen"] == 2000
    assert row["observation_count"] == 2


def test_distinct_senders_counts_observations_only(tmp_path):
    conn, c = mk(tmp_path)
    c.record(extract(make_packet(from_num=0x1, packet_id=1), now=1000, now_monotonic=1.0))
    c.record(extract(make_packet(from_num=0x2, packet_id=2), now=1000, now_monotonic=1.0))
    c.record(extract(make_packet(from_num=0x1, packet_id=3), now=1000, now_monotonic=1.0))
    assert c.distinct_senders_since(0) == 2


def test_update_node_names_does_not_move_last_seen_backwards_or_create_rows(tmp_path):
    conn, c = mk(tmp_path)
    c.update_node_names("!00000009", short_name="ABCD", long_name="Long",
                        observed_at=500)
    row = conn.execute("SELECT * FROM nodes WHERE node_id='!00000009'").fetchone()
    assert row is None or row["observed_last_seen"] >= 500


def test_broadcast_destination_recorded_as_all(tmp_path):
    conn, c = mk(tmp_path)
    c.record(extract(make_packet(to_num=0xFFFFFFFF), now=1, now_monotonic=1.0))
    assert conn.execute("SELECT dest_node_id FROM observations").fetchone()[0] == "^all"


# --- POSITION_APP capture ----------------------------------------------------


def test_position_app_persists_node_position_and_node_snapshot(tmp_path):
    conn, c = mk(tmp_path)
    c.record(
        obs(
            portnum="POSITION_APP",
            position={
                "latitudeI": 377749000,
                "longitudeI": -1224194000,
                "altitude": 10,
                "precisionBits": 16,
                "time": 900,
            },
            packet_id=1,
        )
    )
    row = conn.execute(
        "SELECT * FROM node_positions WHERE node_id='!aabbccdd'"
    ).fetchone()
    assert row["lat"] == pytest.approx(37.7749)
    assert row["lon"] == pytest.approx(-122.4194)
    assert row["alt"] == 10
    assert row["precision_bits"] == 16
    assert row["reported_time"] == 900
    assert row["server_time"] == 1000

    node = conn.execute("SELECT * FROM nodes WHERE node_id='!aabbccdd'").fetchone()
    assert node["last_position_lat"] == pytest.approx(37.7749)
    assert node["last_position_lon"] == pytest.approx(-122.4194)
    assert node["last_position_at"] == 1000


def test_position_capture_disabled_skips_storage(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    c = Collector(conn, enabled_position_capture=False)
    c.record(
        extract(
            make_packet(
                portnum="POSITION_APP",
                position={"latitudeI": 377749000, "longitudeI": -1224194000},
                packet_id=1,
            ),
            now=1000,
            now_monotonic=1.0,
        )
    )
    assert conn.execute("SELECT count(*) FROM node_positions").fetchone()[0] == 0
    node = conn.execute("SELECT * FROM nodes WHERE node_id='!aabbccdd'").fetchone()
    assert node["last_position_lat"] is None


# --- NODEINFO_APP hw_model/role snapshot -------------------------------------


def test_nodeinfo_updates_hw_model_and_role_snapshot(tmp_path):
    conn, c = mk(tmp_path)
    c.record(
        obs(
            portnum="NODEINFO_APP",
            text=None,
            user={"shortName": "G", "longName": "Ali", "hwModel": "TBEAM", "role": "CLIENT"},
            packet_id=1,
        )
    )
    node = conn.execute("SELECT * FROM nodes WHERE node_id='!aabbccdd'").fetchone()
    assert node["hw_model"] == "TBEAM"
    assert node["role"] == "CLIENT"


def test_nodeinfo_snapshot_keeps_prior_value_when_field_absent(tmp_path):
    conn, c = mk(tmp_path)
    c.record(
        obs(
            portnum="NODEINFO_APP",
            text=None,
            user={"hwModel": "TBEAM", "role": "CLIENT"},
            packet_id=1,
        )
    )
    c.record(
        obs(portnum="NODEINFO_APP", text=None, user={"hwModel": "TBEAM"}, packet_id=2)
    )
    node = conn.execute("SELECT * FROM nodes WHERE node_id='!aabbccdd'").fetchone()
    assert node["role"] == "CLIENT"  # not blanked by the second packet


# --- TELEMETRY_APP last_battery snapshot -------------------------------------


def test_telemetry_updates_last_battery_snapshot(tmp_path):
    conn, c = mk(tmp_path)
    c.record(
        obs(
            portnum="TELEMETRY_APP",
            text=None,
            telemetry={"deviceMetrics": {"batteryLevel": 55}},
            packet_id=1,
        )
    )
    node = conn.execute("SELECT * FROM nodes WHERE node_id='!aabbccdd'").fetchone()
    assert node["last_battery"] == 55


# --- node_daily incremental rollup -------------------------------------------


def test_node_daily_accumulates_packets_direct_snr_and_hops(tmp_path):
    conn, c = mk(tmp_path)
    day = "1970-01-01"  # now=1000 falls on the UTC epoch day
    c.record(obs(hop_start=3, hop_limit=3, rx_snr=5.0, packet_id=1))  # direct, hops_away=0
    c.record(obs(hop_start=3, hop_limit=1, rx_snr=-3.0, packet_id=2))  # hops_away=2
    row = conn.execute(
        "SELECT * FROM node_daily WHERE node_id='!aabbccdd' AND day=?", (day,)
    ).fetchone()
    assert row["packets"] == 2
    assert row["direct_packets"] == 1
    assert row["best_snr"] == pytest.approx(5.0)
    assert row["typical_snr"] == pytest.approx(1.0)
    assert row["best_hops"] == 0
    assert row["typical_hops"] == pytest.approx(1.0)


def test_node_daily_last_lat_lon_tracks_latest_position(tmp_path):
    conn, c = mk(tmp_path)
    day = "1970-01-01"
    c.record(
        obs(
            portnum="POSITION_APP",
            position={"latitudeI": 10_000_000, "longitudeI": 10_000_000},
            packet_id=1,
        )
    )
    c.record(
        obs(
            portnum="POSITION_APP",
            position={"latitudeI": 20_000_000, "longitudeI": 20_000_000},
            packet_id=2,
        )
    )
    row = conn.execute(
        "SELECT * FROM node_daily WHERE node_id='!aabbccdd' AND day=?", (day,)
    ).fetchone()
    assert row["last_lat"] == pytest.approx(2.0)
    assert row["last_lon"] == pytest.approx(2.0)


def test_node_daily_separate_rows_per_node_and_day(tmp_path):
    conn, c = mk(tmp_path)
    c.record(obs(from_num=0x1, packet_id=1, now=1000))
    c.record(obs(from_num=0x2, packet_id=2, now=1000))
    c.record(obs(from_num=0x1, packet_id=3, now=1000 + 86400))
    rows = conn.execute(
        "SELECT node_id, day, packets FROM node_daily ORDER BY node_id, day"
    ).fetchall()
    assert len(rows) == 3


# --- links: inferred-direct only ----------------------------------------------


MY_NODE_NUM = 0x99999999


def test_direct_packet_creates_inferred_link_when_my_node_num_known(tmp_path):
    conn, c = mk(tmp_path)
    c.record(obs(hop_start=2, hop_limit=2, packet_id=1), my_node_num=MY_NODE_NUM)
    c.record(obs(hop_start=2, hop_limit=2, packet_id=2), my_node_num=MY_NODE_NUM)
    from meshgarden.config import node_id_from_num

    row = conn.execute(
        "SELECT * FROM links WHERE a_node_id='!aabbccdd' AND b_node_id=?",
        (node_id_from_num(MY_NODE_NUM),),
    ).fetchone()
    assert row["count"] == 2
    assert row["kind"] == "inferred_direct"


def test_no_link_created_without_my_node_num(tmp_path):
    conn, c = mk(tmp_path)
    c.record(obs(hop_start=2, hop_limit=2, packet_id=1))
    assert conn.execute("SELECT count(*) FROM links").fetchone()[0] == 0


def test_no_link_created_for_indirect_packet(tmp_path):
    conn, c = mk(tmp_path)
    c.record(obs(hop_start=3, hop_limit=1, packet_id=1), my_node_num=MY_NODE_NUM)
    assert conn.execute("SELECT count(*) FROM links").fetchone()[0] == 0


def test_duplicate_reception_does_not_double_count_node_daily_positions_or_links(tmp_path):
    """Honest-statistics rule: a second reception of the exact same
    packet_id within the dedup window (e.g. relayed via two different
    paths) is still recorded as its own raw `observations` row
    (is_duplicate=1, same as today) but must NOT inflate `node_daily`'s
    packet/direct/snr/hops accumulators, must not append a second
    `node_positions` history row, and must not double-count
    `links.count` -- those all describe genuinely distinct receptions,
    not retransmissions of one."""
    conn, c = mk(tmp_path)
    day = "1970-01-01"  # now=1000 (obs()'s default) falls on the UTC epoch day
    o = obs(
        portnum="POSITION_APP",
        position={"latitudeI": 10_000_000, "longitudeI": 10_000_000},
        hop_start=2,
        hop_limit=2,  # direct: hops_away == 0
        packet_id=1,
    )
    assert c.record(o, my_node_num=MY_NODE_NUM) is True
    assert c.record(o, my_node_num=MY_NODE_NUM) is False  # duplicate: same packet_id, same window

    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2

    row = conn.execute(
        "SELECT packets, direct_packets FROM node_daily WHERE node_id='!aabbccdd' AND day=?",
        (day,),
    ).fetchone()
    assert row["packets"] == 1
    assert row["direct_packets"] == 1

    assert (
        conn.execute(
            "SELECT COUNT(*) FROM node_positions WHERE node_id='!aabbccdd'"
        ).fetchone()[0]
        == 1
    )

    from meshgarden.config import node_id_from_num

    link = conn.execute(
        "SELECT count FROM links WHERE a_node_id='!aabbccdd' AND b_node_id=?",
        (node_id_from_num(MY_NODE_NUM),),
    ).fetchone()
    assert link["count"] == 1


# --- local_radio_samples: passive, own-node telemetry only --------------------


def test_local_radio_sample_recorded_only_for_own_node_telemetry(tmp_path):
    conn, c = mk(tmp_path)
    c.record(
        obs(
            from_num=MY_NODE_NUM,
            portnum="TELEMETRY_APP",
            text=None,
            telemetry={
                "deviceMetrics": {
                    "batteryLevel": 90,
                    "channelUtilization": 12.5,
                    "airUtilTx": 3.0,
                }
            },
            packet_id=1,
        ),
        my_node_num=MY_NODE_NUM,
    )
    c.record(
        obs(
            from_num=0xAABBCCDD,
            portnum="TELEMETRY_APP",
            text=None,
            telemetry={"deviceMetrics": {"batteryLevel": 50}},
            packet_id=2,
        ),
        my_node_num=MY_NODE_NUM,
    )
    rows = conn.execute("SELECT * FROM local_radio_samples").fetchall()
    assert len(rows) == 1
    assert rows[0]["battery"] == 90
    assert rows[0]["channel_utilization"] == pytest.approx(12.5)
    assert rows[0]["air_util_tx"] == pytest.approx(3.0)


# --- haversine_km / furthest queries ------------------------------------------


def test_haversine_km_known_city_distance():
    sf = (37.7749, -122.4194)
    la = (34.0522, -118.2437)
    assert haversine_km(*sf, *la) == pytest.approx(559, abs=5)


def _record_rf(c, **kw):
    """Records `obs(**kw)` with via_mqtt forced to explicit RF (False/0) --
    make_packet has no way to encode an explicit `viaMqtt: false` (proto3
    omits default-valued scalar bools, see radio.extract), so this patches
    the extracted IncomingPacket directly, same as a real radio would only
    ever report via_mqtt=True (relayed) or omit the field entirely."""
    c.record(dataclasses.replace(obs(**kw), via_mqtt=False))


def test_furthest_direct_km_none_when_observatory_unset(tmp_path):
    conn, c = mk(tmp_path)
    _record_rf(
        c,
        portnum="POSITION_APP",
        position={"latitudeI": 300_000_000, "longitudeI": 100_000_000},
        hop_start=2,
        hop_limit=2,
        packet_id=1,
    )
    assert (
        c.furthest_direct_km_since(0, observatory_lat=0.0, observatory_lon=0.0, **_THRESH)
        is None
    )


def test_furthest_direct_km_only_considers_direct_nodes(tmp_path):
    conn, c = mk(tmp_path)
    # Direct node, close by.
    _record_rf(
        c,
        from_num=0x1,
        portnum="POSITION_APP",
        position={"latitudeI": 100_100_000, "longitudeI": 100_000_000},
        hop_start=1,
        hop_limit=1,
        packet_id=1,
    )
    # Indirect node, much farther (and irrelevant here regardless of
    # distance -- furthest_direct only ever considers hops_away == 0).
    _record_rf(
        c,
        from_num=0x2,
        portnum="POSITION_APP",
        position={"latitudeI": 300_000_000, "longitudeI": 100_000_000},
        hop_start=3,
        hop_limit=1,
        packet_id=2,
    )
    direct_km = c.furthest_direct_km_since(0, observatory_lat=10.0, observatory_lon=10.0, **_THRESH)
    expected = haversine_km(10.0, 10.0, 10.01, 10.0)
    assert direct_km == pytest.approx(expected, rel=0.01)


def test_furthest_observed_km_considers_any_hop_count_and_returns_node_id(tmp_path):
    conn, c = mk(tmp_path)
    _record_rf(
        c,
        from_num=0x1,
        portnum="POSITION_APP",
        position={"latitudeI": 100_100_000, "longitudeI": 100_000_000},
        hop_start=1,
        hop_limit=1,
        packet_id=1,
    )
    # Indirect (2 hops), further away than node 1 but still within the
    # 2-hop budget (2 * max_km_per_hop=150 = 300 km) -- must still win as
    # "furthest observed" even though it is not direct.
    _record_rf(
        c,
        from_num=0x2,
        portnum="POSITION_APP",
        position={"latitudeI": 120_000_000, "longitudeI": 100_000_000},
        hop_start=3,
        hop_limit=1,
        packet_id=2,
    )
    result = c.furthest_observed_km_with_node_since(
        0, observatory_lat=10.0, observatory_lon=10.0, **_THRESH
    )
    assert result is not None
    km, node_id = result
    assert node_id == "!00000002"
    assert km == pytest.approx(haversine_km(10.0, 10.0, 12.0, 10.0), rel=0.01)


def test_furthest_returns_none_when_no_position_known(tmp_path):
    conn, c = mk(tmp_path)
    _record_rf(c, hop_start=1, hop_limit=1, packet_id=1)
    assert (
        c.furthest_direct_km_since(0, observatory_lat=10.0, observatory_lon=10.0, **_THRESH)
        is None
    )
    assert (
        c.furthest_observed_km_with_node_since(
            0, observatory_lat=10.0, observatory_lon=10.0, **_THRESH
        )
        is None
    )


# --- distance-plausibility rulings (RF-only, hop/distance agreement) ---------


@pytest.mark.parametrize(
    "lat,lon,precision_bits,expected",
    [
        (37.7, -122.4, None, True),  # precision unknown (older firmware) -> usable
        (37.7, -122.4, 10, True),  # exactly at the floor -> usable
        (37.7, -122.4, 9, False),  # below the floor -> too coarse (~10+ km cell)
        (0.0, 0.0, None, False),  # (0.0, 0.0) sentinel -> never a real fix
        (0.0, 0.0, 20, False),  # sentinel wins even with fine precision
        (91.0, 0.0, None, False),  # out-of-range latitude
        (0.0, 181.0, None, False),  # out-of-range longitude
        (None, -122.4, None, False),  # missing lat
        (37.7, None, None, False),  # missing lon
    ],
)
def test_is_position_plausible(lat, lon, precision_bits, expected):
    assert is_position_plausible(lat, lon, precision_bits) is expected


@pytest.mark.parametrize(
    "distance_km,hops_away,expected",
    [
        (300.0, 0, True),  # at the direct ceiling
        (300.1, 0, False),  # just past the direct ceiling
        (450.0, 3, True),  # at the 3-hop ceiling (3 * 150)
        (450.1, 3, False),  # just past it
        (999.0, 20, True),  # capped by max_observed_km, not hops * per_hop
        (1000.1, 20, False),  # past the max_observed_km cap
        (10.0, None, False),  # unknown hops are never eligible
    ],
)
def test_is_distance_plausible_for_hops(distance_km, hops_away, expected):
    assert (
        is_distance_plausible_for_hops(
            distance_km,
            hops_away,
            max_direct_km=300.0,
            max_km_per_hop=150.0,
            max_observed_km=1000.0,
        )
        is expected
    )


def _seed_plausibility_scenario(conn, *, now: int) -> None:
    """Direct SQL inserts (not extract()/make_packet()) so via_mqtt can be
    an explicit 0, 1, or NULL and hops_away can be an explicit None --
    combinations a real radio packet can't produce for via_mqtt=0 (see
    `_record_rf`), but that database.py's own tri-state column must still
    be handled correctly for.

    Six nodes, covering every ruling from the operator's distance-
    plausibility report (RECORD showing "furthest observed 4325 km" from
    an MQTT-relayed/bogus node):
      W: via_mqtt=0, hops=0,   ~50 km   -- plausible; wins furthest_direct.
      X: via_mqtt=0, hops=3,   ~400 km  -- plausible (<= 3*150=450); the
         true furthest_observed winner (further than W, but not direct).
      Y: via_mqtt=0, hops=3,   ~500 km  -- RF, but > 450 km for 3 hops:
         excluded by hop/distance agreement alone.
      Z: via_mqtt=1, hops=0,   ~4325 km -- MQTT-relayed: excluded from the
         strict records regardless of distance, and past max_observed_km
         so excluded from the incl.-MQTT figure too (a bridged/bogus node
         used to win every window with it). Y wins incl.-MQTT: it is only
         hop-inconsistent, which that figure deliberately ignores.
      U: via_mqtt=NULL, hops=0, ~80 km -- unknown origin: excluded from
         the strict records, same as Z.
      V: via_mqtt=0, hops=NULL, ~90 km -- RF, but hops unknown: excluded
         from furthest_observed entirely (never eligible per ruling).
    """

    # Base observatory location for this scenario -- deliberately not
    # (0.0, 0.0), which is config.ObservatoryConfig's "unset" sentinel.
    base_lat, base_lon = 10.0, 10.0

    def _node_lat(km: float) -> float:
        return base_lat + km / 111.0  # ~111 km per degree of latitude; good enough here

    def _insert(node_id: str, *, hops_away, via_mqtt, distance_km: float) -> None:
        conn.execute(
            "INSERT INTO observations (server_time, packet_time, sender_node_id, "
            "dest_node_id, channel, portnum, packet_id, is_duplicate, rx_snr, rx_rssi, "
            "hop_limit, hop_start, hops_away, via_mqtt, pki_encrypted, payload_len) "
            "VALUES (?, NULL, ?, '^all', 0, 'POSITION_APP', NULL, 0, NULL, NULL, "
            "NULL, NULL, ?, ?, 0, 0)",
            (now, node_id, hops_away, via_mqtt),
        )
        conn.execute(
            "INSERT INTO nodes (node_id, observed_first_seen, observed_last_seen, "
            "observation_count) VALUES (?, ?, ?, 1)",
            (node_id, now, now),
        )
        lat = _node_lat(distance_km)
        conn.execute(
            "INSERT INTO node_positions (node_id, server_time, lat, lon, precision_bits) "
            "VALUES (?, ?, ?, ?, NULL)",
            (node_id, now, lat, base_lon),
        )
        conn.execute(
            "UPDATE nodes SET last_position_lat = ?, last_position_lon = ? WHERE node_id = ?",
            (lat, base_lon, node_id),
        )

    _insert("!0000000a", hops_away=0, via_mqtt=0, distance_km=50.0)
    _insert("!0000000b", hops_away=3, via_mqtt=0, distance_km=400.0)
    _insert("!0000000c", hops_away=3, via_mqtt=0, distance_km=500.0)
    _insert("!0000000d", hops_away=0, via_mqtt=1, distance_km=4325.0)
    _insert("!0000000e", hops_away=0, via_mqtt=None, distance_km=80.0)
    _insert("!0000000f", hops_away=None, via_mqtt=0, distance_km=90.0)
    conn.commit()
    return base_lat, base_lon


def test_records_pick_only_the_rf_hop_consistent_plausible_packet(tmp_path):
    """Regression test for the reported bug: an MQTT-relayed or bogus-
    position/hop-inconsistent node must never win 'furthest direct' or
    'furthest observed', but IS surfaced (labelled) by the incl.-MQTT
    dashboard figure."""
    conn, c = mk(tmp_path)
    obs_lat, obs_lon = _seed_plausibility_scenario(conn, now=1000)

    direct_km = c.furthest_direct_km_since(
        0, observatory_lat=obs_lat, observatory_lon=obs_lon, **_THRESH
    )
    assert direct_km == pytest.approx(50.0, rel=0.02)

    observed = c.furthest_observed_km_with_node_since(
        0, observatory_lat=obs_lat, observatory_lon=obs_lon, **_THRESH
    )
    assert observed is not None
    km, node_id = observed
    assert node_id == "!0000000b"
    assert km == pytest.approx(400.0, rel=0.02)

    incl_mqtt = c.furthest_observed_incl_mqtt_km_with_node_since(
        0, observatory_lat=obs_lat, observatory_lon=obs_lon, max_observed_km=1000.0
    )
    assert incl_mqtt is not None
    km2, node_id2 = incl_mqtt
    assert node_id2 == "!0000000c"
    assert km2 == pytest.approx(500.0, rel=0.02)


def test_meshcore_observations_count_as_rf_for_range_records(tmp_path):
    """MeshCore never sets via_mqtt (NULL), so the strict range records
    were permanently empty on a MeshCore mesh and the dashboards fell
    through to the incl.-MQTT figure. A MeshCore row must qualify as RF; a
    Meshtastic NULL row must still not."""
    conn, c = mk(tmp_path)
    base_lat, base_lon = 10.0, 10.0
    for node_id, transport, km in (
        ("!0000000a", "meshcore", 60.0),
        ("!0000000b", "meshtastic", 70.0),
    ):
        conn.execute(
            "INSERT INTO observations (server_time, sender_node_id, dest_node_id, channel, "
            "portnum, is_duplicate, hops_away, via_mqtt, pki_encrypted, payload_len, transport) "
            "VALUES (1000, ?, '^all', 0, 'POSITION_APP', 0, 0, NULL, 0, 0, ?)",
            (node_id, transport),
        )
        conn.execute(
            "INSERT INTO nodes (node_id, observed_first_seen, observed_last_seen, "
            "observation_count) VALUES (?, 1000, 1000, 1)",
            (node_id,),
        )
        conn.execute(
            "INSERT INTO node_positions (node_id, server_time, lat, lon, precision_bits) "
            "VALUES (?, 1000, ?, ?, NULL)",
            (node_id, base_lat + km / 111.0, base_lon),
        )
    conn.commit()

    direct_km = c.furthest_direct_km_since(
        0, observatory_lat=base_lat, observatory_lon=base_lon, **_THRESH
    )
    assert direct_km == pytest.approx(60.0, rel=0.02)
    observed = c.furthest_observed_km_with_node_since(
        0, observatory_lat=base_lat, observatory_lon=base_lon, **_THRESH
    )
    assert observed is not None and observed[1] == "!0000000a"


# --- resolve_node / days_seen_last_n (SEEN/WHO/ME/PING support) -----------------


def test_resolve_node_by_full_id_always_resolves(tmp_path):
    conn, col = mk(tmp_path)
    r = col.resolve_node("!AABBCCDD")
    assert r.node_id == "!aabbccdd" and r.ambiguous is False


def test_resolve_node_by_tail_with_one_match(tmp_path):
    conn, col = mk(tmp_path)
    conn.execute(
        "INSERT INTO nodes (node_id, observed_first_seen, observed_last_seen) "
        "VALUES ('!aabbccdd', 1, 1)"
    )
    r = col.resolve_node("ccdd")
    assert r.node_id == "!aabbccdd" and r.ambiguous is False


def test_resolve_node_by_tail_with_two_matches_is_ambiguous(tmp_path):
    conn, col = mk(tmp_path)
    conn.executemany(
        "INSERT INTO nodes (node_id, observed_first_seen, observed_last_seen) VALUES (?, 1, 1)",
        [("!11aaccdd",), ("!22bbccdd",)],
    )
    r = col.resolve_node("ccdd")
    assert r.node_id is None and r.ambiguous is True


def test_resolve_node_by_short_name_case_insensitive(tmp_path):
    conn, col = mk(tmp_path)
    conn.execute(
        "INSERT INTO nodes (node_id, short_name, observed_first_seen, observed_last_seen) "
        "VALUES ('!aabbccdd', 'Fndr', 1, 1)"
    )
    r = col.resolve_node("fndr")
    assert r.node_id == "!aabbccdd" and r.ambiguous is False


def test_resolve_node_no_match_is_not_ambiguous(tmp_path):
    conn, col = mk(tmp_path)
    r = col.resolve_node("nope")
    assert r.node_id is None and r.ambiguous is False


def test_days_seen_last_n_counts_distinct_node_daily_rows(tmp_path):
    conn, col = mk(tmp_path)
    for day in ("2026-09-08", "2026-09-09", "2026-09-13"):
        conn.execute(
            "INSERT INTO node_daily (node_id, day, packets) VALUES ('!aabbccdd', ?, 1)", (day,)
        )
    assert col.days_seen_last_n("!aabbccdd", "2026-09-14", 7) == 3


def test_days_seen_last_n_excludes_days_outside_the_window(tmp_path):
    conn, col = mk(tmp_path)
    for day in ("2026-09-06", "2026-09-07", "2026-09-08"):
        conn.execute(
            "INSERT INTO node_daily (node_id, day, packets) VALUES ('!aabbccdd', ?, 1)", (day,)
        )
    # 7-day window counting back from 2026-09-14 is 2026-09-08..2026-09-14.
    assert col.days_seen_last_n("!aabbccdd", "2026-09-14", 7) == 1


def test_last_observation_returns_the_most_recent_row(tmp_path):
    conn, col = mk(tmp_path)
    col.record(
        obs(from_num=0xAABBCCDD, packet_id=1, rx_snr=3.0, hop_start=3, hop_limit=3, now=1000)
    )
    col.record(
        obs(from_num=0xAABBCCDD, packet_id=2, rx_snr=9.0, hop_start=3, hop_limit=3, now=2000)
    )
    row = col.last_observation("!aabbccdd")
    assert row is not None
    assert row["server_time"] == 2000
    assert row["rx_snr"] == 9.0
    assert row["hops_away"] == 0


def test_last_observation_returns_none_when_never_observed(tmp_path):
    conn, col = mk(tmp_path)
    assert col.last_observation("!aabbccdd") is None


def test_first_seen_date_renders_the_local_calendar_day(tmp_path):
    conn, col = mk(tmp_path)
    conn.execute(
        "INSERT INTO nodes (node_id, observed_first_seen, observed_last_seen) "
        "VALUES ('!aabbccdd', ?, ?)",
        (1757808000, 1757808000),  # 2025-09-14T00:00:00Z
    )
    assert col.first_seen_date("!aabbccdd", "UTC") == "2025-09-14"


def test_first_seen_date_is_none_when_never_observed(tmp_path):
    conn, col = mk(tmp_path)
    assert col.first_seen_date("!aabbccdd", "UTC") is None


def test_meshcore_zero_relay_creates_direct_link_but_routed_or_unknown_does_not(tmp_path):
    from meshgarden.models import MeshCoreObservation

    conn, collector = mk(tmp_path)
    for i, hops in enumerate((0, 1, None)):
        event = MeshCoreObservation(
            received_at=1000 + i, sender=f"!aabbccd{i}", recipient="!11223344",
            packet_id=i, kind="MESHCORE_DM", hops=hops,
        )
        collector.record_meshcore(event, my_node_num=0x11223344)
        collector.record_meshcore(event, my_node_num=0x11223344)
    rows = conn.execute("SELECT a_node_id, b_node_id, count FROM links").fetchall()
    assert [tuple(row) for row in rows] == [("!aabbccd0", "!11223344", 1)]


def test_default_direct_limit_accepts_331_km_but_rejects_unknown_hops():
    from dataclasses import asdict

    from meshgarden.config import ObservatoryConfig

    cfg = asdict(ObservatoryConfig())
    limits = {key: cfg[key] for key in ("max_direct_km", "max_km_per_hop", "max_observed_km")}
    assert is_distance_plausible_for_hops(331, 0, **limits)
    assert not is_distance_plausible_for_hops(401, 0, **limits)
    assert not is_distance_plausible_for_hops(311, None, **limits)


def test_meshcore_direct_distance_needs_valid_position(tmp_path):
    from meshgarden.config import ObservatoryConfig
    from meshgarden.models import MeshCoreObservation

    conn, collector = mk(tmp_path)
    event = MeshCoreObservation(
        received_at=1000, sender="!aabbccdd", recipient="!11223344",
        packet_id=1, kind="MESHCORE_DM", hops=0,
    )
    collector.record_contact_identity(
        event.sender, display_name="Direct", lat=0, lon=0, observed_at=999,
    )
    collector.record_meshcore(event, my_node_num=0x11223344)
    cfg = ObservatoryConfig()
    kwargs = dict(observatory_lat=37.7749, observatory_lon=-122.4194,
                  max_direct_km=cfg.max_direct_km, max_km_per_hop=cfg.max_km_per_hop,
                  max_observed_km=cfg.max_observed_km)
    assert collector.furthest_direct_km_since(0, **kwargs) is None
    collector.record_contact_identity(
        event.sender, display_name="Direct", lat=37.8, lon=-122.3, observed_at=1001,
    )
    assert 0 < collector.furthest_direct_km_since(0, **kwargs) < 20
