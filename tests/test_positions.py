"""DM location shares ("Saved position: lat, lon" / "GPS position: lat, lon")
-> dm_positions + one ack, gated by [observatory] dm_position_capture."""

import json
import random
from dataclasses import replace

import pytest

from meshgarden import cli_ops
from meshgarden.commands import Command, parse_command, parse_position_text, position_ack_text
from meshgarden.config import ObservatoryConfig, load_config
from meshgarden.main import Daemon
from meshgarden.positions import list_dm_positions, record_dm_position
from tests.conftest import HAIKU_RNG_SEED, _cfg
from tests.fakes import FakeTransport, make_packet

SAVED = "Saved position: 37.774929, -122.419416"
GPS = "GPS position: 37.808674, -122.409821"


@pytest.mark.parametrize(
    "text, expected",
    [
        (SAVED, ("saved", 37.774929, -122.419416)),
        (GPS, ("gps", 37.808674, -122.409821)),
        ("  gps POSITION:  -33.9, 151.2  ", ("gps", -33.9, 151.2)),
        ("Saved position: 91.0, 0.0", None),  # latitude out of range
        ("Saved position: 37.7, -122.4, 10", None),  # extra field
        ("Saved position 37.7, -122.4", None),  # no colon
        ("position: 37.7, -122.4", None),
        ("PING", None),
    ],
)
def test_parse_position_text(text, expected):
    assert parse_position_text(text) == expected


def test_parse_command_yields_position_with_whole_text_as_argument():
    parsed = parse_command(SAVED)
    assert parsed.command is Command.POSITION
    assert parsed.argument == SAVED
    assert parse_command("Saved position: nope").command is Command.UNKNOWN


def test_ack_text_fits_meshcore_budget():
    body = position_ack_text("saved", -89.123456, -179.123456)
    assert len(body.encode("utf-8")) <= 120


def _daemon(tmp_path, *, capture: bool):
    cfg = _cfg(tmp_path)
    cfg = replace(cfg, observatory=ObservatoryConfig(dm_position_capture=capture))
    ft = FakeTransport(0x11223344, cfg.radio.expected_node_id, service_cfg=cfg.service)
    d = Daemon(cfg, transport=ft, start_metrics=False, haiku_rng=random.Random(HAIKU_RNG_SEED))
    d.iface = ft.iface
    d.start()
    return d


def test_dm_position_is_recorded_and_acked_when_enabled(tmp_path):
    d = _daemon(tmp_path, capture=True)
    try:
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text=SAVED, packet_id=9301))
        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, text=GPS, packet_id=9302, from_num=0x99999999)
        )
        d.drain()
        assert [m["text"] for m in d.iface.sent] == [
            "📍 saved position saved · 37.7749, -122.4194",
            "📍 gps position saved · 37.8087, -122.4098",
        ]
        rows = list_dm_positions(d.conn)
        assert [(r["node_id"], r["kind"], r["lat"], r["lon"]) for r in rows] == [
            ("!99999999", "gps", 37.808674, -122.409821),
            ("!aabbccdd", "saved", 37.774929, -122.419416),
        ]
        assert list_dm_positions(d.conn, node_id="!99999999")[0]["kind"] == "gps"
    finally:
        d.stop()


def test_dm_position_is_silent_and_unstored_by_default(tmp_path):
    d = _daemon(tmp_path, capture=False)
    try:
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text=SAVED, packet_id=9303))
        d.drain()
        assert d.iface.sent == []
        assert list_dm_positions(d.conn) == []
    finally:
        d.stop()


CONFIG = """
[radio]
device = "/dev/fake"
expected_node_id = "!11223344"
[database]
path = "{db}"
[backup]
directory = "{bk}"
"""


def test_cmd_positions_text_and_json(tmp_path, capsys):
    from meshgarden.database import connect, migrate

    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(db=tmp_path / "o.db", bk=tmp_path / "backups"))
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    with conn:
        conn.execute(
            "INSERT INTO nodes(node_id,observed_first_seen,observed_last_seen,long_name) "
            "VALUES ('!a1b2c3d4',1,1,'Node23')"
        )
    record_dm_position(conn, "!a1b2c3d4", "saved", 37.774929, -122.419416, 1_700_000_000)
    record_dm_position(conn, "!00000001", "gps", 1.0, 2.0, 1_700_000_100)
    conn.close()
    cfg = load_config(str(path))

    assert cli_ops.cmd_positions(cfg) == 0
    out = capsys.readouterr().out
    assert "positions: 2" in out
    assert "Node23 (!a1b2c3d4)  saved  37.774929, -122.419416" in out
    assert out.index("!00000001") < out.index("!a1b2c3d4")  # newest first

    assert cli_ops.cmd_positions(cfg, node_id="!a1b2c3d4", as_json=True) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 1
    assert data["positions"][0]["node_id"] == "!a1b2c3d4"
    assert data["positions"][0]["server_time"] == 1_700_000_000


def _seed_positions(tmp_path):
    """Five shared positions, ids 1..5, one per UTC day from 2026-10-01."""
    from meshgarden.database import connect, migrate

    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(db=tmp_path / "o.db", bk=tmp_path / "backups"))
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    day0 = 1_790_812_800  # 2026-10-01 00:00:00 UTC
    for i in range(5):
        node = "!a1b2c3d4" if i % 2 == 0 else "!00000001"
        record_dm_position(conn, node, "saved", 1.0 + i, 2.0, day0 + i * 86400 + 3600)
    conn.close()
    return str(path), load_config(str(path))


def _remaining_ids(cfg):
    from meshgarden.database import connect

    conn = connect(cfg.database.path)
    try:
        return sorted(r["id"] for r in list_dm_positions(conn, limit=100))
    finally:
        conn.close()


@pytest.mark.parametrize(
    "argv, remaining",
    [
        (["3"], [1, 2, 4, 5]),
        (["1", "5"], [2, 3, 4]),
        (["--range", "2-4"], [1, 5]),
        (["--node", "!a1b2c3d4"], [2, 4]),
        (["--from", "2026-10-02", "--to", "2026-10-03"], [1, 4, 5]),
        (["--to", "2026-10-02"], [3, 4, 5]),
        (["--from", "2026-10-04"], [1, 2, 3]),
        (["--node", "!a1b2c3d4", "--from", "2026-10-02"], [1, 2, 4]),
    ],
)
def test_positions_delete_filters_combine_with_and(tmp_path, capsys, argv, remaining):
    from meshgarden.main import main

    path, cfg = _seed_positions(tmp_path)
    assert main(["--config", path, "positions", "delete", *argv]) == 0
    assert capsys.readouterr().out.startswith(f"deleted: {5 - len(remaining)}")
    assert _remaining_ids(cfg) == remaining


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--range", "4-2"],
        ["--range", "abc"],
        ["--from", "2026-10-05", "--to", "2026-10-01"],
        ["--from", "10/01/2026"],
        ["--node", "nonsense"],
        ["0"],
    ],
)
def test_positions_delete_rejects_bad_or_missing_filters(tmp_path, argv):
    from meshgarden.main import main

    path, cfg = _seed_positions(tmp_path)
    assert main(["--config", path, "positions", "delete", *argv]) == 2
    assert _remaining_ids(cfg) == [1, 2, 3, 4, 5]


def test_positions_delete_reports_no_match(tmp_path):
    from meshgarden.main import main

    path, cfg = _seed_positions(tmp_path)
    assert main(["--config", path, "positions", "delete", "99"]) == 3
    assert _remaining_ids(cfg) == [1, 2, 3, 4, 5]


def test_positions_list_shows_ids_and_still_works_after_adding_delete(tmp_path, capsys):
    from meshgarden.main import main

    path, _cfg_ = _seed_positions(tmp_path)
    assert main(["--config", path, "positions", "--days", "36500"]) == 0
    out = capsys.readouterr().out
    assert "positions: 5" in out and "  #5  " in out and "  #1  " in out


def test_delete_dm_positions_refuses_to_run_without_a_filter(tmp_path):
    from meshgarden.positions import delete_dm_positions

    _path, cfg = _seed_positions(tmp_path)
    from meshgarden.database import connect

    conn = connect(cfg.database.path)
    try:
        with pytest.raises(ValueError):
            delete_dm_positions(conn)
    finally:
        conn.close()
