from meshgarden.database import connect, migrate
from meshgarden.lab import Lab


def _conn(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    return conn


def test_is_unlocked_false_then_unlock_makes_it_true(tmp_path):
    lab = Lab(_conn(tmp_path))
    assert lab.is_unlocked("!aabbccdd") is False
    lab.unlock("!aabbccdd", 1000)
    assert lab.is_unlocked("!aabbccdd") is True


def test_unlock_twice_keeps_the_original_unlocked_at(tmp_path):
    lab = Lab(_conn(tmp_path))
    lab.unlock("!aabbccdd", 1000)
    lab.unlock("!aabbccdd", 2000)
    [(node_id, unlocked_at)] = lab.list_nodes()
    assert node_id == "!aabbccdd"
    assert unlocked_at == 1000


def test_revoke_true_then_false(tmp_path):
    lab = Lab(_conn(tmp_path))
    lab.unlock("!aabbccdd", 1000)
    assert lab.revoke("!aabbccdd") is True
    assert lab.is_unlocked("!aabbccdd") is False
    assert lab.revoke("!aabbccdd") is False


def test_list_nodes_ordered_by_unlocked_at(tmp_path):
    lab = Lab(_conn(tmp_path))
    lab.unlock("!22222222", 2000)
    lab.unlock("!11111111", 1000)
    assert lab.list_nodes() == [("!11111111", 1000), ("!22222222", 2000)]
