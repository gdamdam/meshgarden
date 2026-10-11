import json

import pytest

from meshgarden.collector import Collector
from meshgarden.database import connect, migrate
from meshgarden.models import MeshCoreRoute


@pytest.mark.parametrize("count,mode,path,expected", [
    (3, 0, "aa00bC", ("aa", "00", "bc")),
    (2, 1, "aa0000bc", ("aa00", "00bc")),
    (1, 2, "0000ff", ("0000ff",)),
    (0, 0, "", ()), (-1, -1, "", ()), (255, -1, "", ()),
])
def test_route_decoding_preserves_hash_width_and_zero_bytes(count, mode, path, expected):
    route = MeshCoreRoute.from_contact("!12345678", {
        "out_path_len": count, "out_path_hash_mode": mode, "out_path": path,
    }, 100)
    assert route.hashes == expected
    assert route.hops == (None if count in (-1, 255) else count)
    assert route.cached_at == 100


@pytest.mark.parametrize("payload", [
    {}, {"out_path_len": True}, {"out_path_len": 1, "out_path": "00ff"},
    {"out_path_len": 1, "out_path": "gg"},
    {"out_path_len": 1, "out_path": "  "},
    {"out_path_len": 0, "out_path": "", "out_path_hash_mode": 3},
    {"out_path_len": 33, "out_path_hash_mode": 1, "out_path": "00" * 66},
])
def test_invalid_contact_routes_are_not_guessed(payload):
    assert MeshCoreRoute.from_contact("!12345678", payload, 100) is None


def test_cached_route_does_not_manufacture_receptions_or_links(tmp_path):
    conn = connect(str(tmp_path / "routes.db"))
    migrate(conn)
    collector = Collector(conn)
    try:
        collector.record_meshcore_route(MeshCoreRoute("!12345678", 0, (), 100))
        node = conn.execute("SELECT * FROM nodes").fetchone()
        assert node["observed_last_seen"] == node["observation_count"] == 0
        assert json.loads(node["stored_route"])["hops"] == 0
        assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        collector.record_meshcore_route(MeshCoreRoute("!12345678", None, (), 200))
        node = conn.execute("SELECT * FROM nodes").fetchone()
        assert json.loads(node["stored_route"]) == {"hops": None, "hashes": [], "cached_at": 200}
        assert node["observation_count"] == 0
    finally:
        conn.close()
