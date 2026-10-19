import argparse
import builtins
import os
import tomllib

import pytest

from meshgarden import setup_wizard as sw
from meshgarden.config import parse_config


def scripted(answers):
    it = iter(answers)

    def inp(prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return inp


def load(path):
    with open(path, "rb") as f:
        return parse_config(tomllib.load(f))


def single_answers(tmp_path, extra=()):
    return [
        "meshcore",
        "/dev/ttyACM0",
        "!11223344",
        str(tmp_path / "c.toml"),
        str(tmp_path / "s" / "obs.db"),
        str(tmp_path / "bk"),
        "9200",
        "9201",
        "n",  # no second instance
        "n",  # no shared bbs
        "y",  # write
        *extra,
    ]


def test_single_radio_session(tmp_path):
    out = []
    rc = sw.run_interactive(scripted(single_answers(tmp_path)), out.append, roots=[])
    assert rc == 0
    cfg = load(tmp_path / "c.toml")
    assert cfg.radio.transport == "meshcore"
    assert cfg.metrics.port == 9200 and cfg.web.enabled and cfg.web.port == 9201
    assert oct(os.stat(tmp_path / "c.toml").st_mode & 0o777) == "0o600"


def test_dual_radio_session(tmp_path):
    answers = [
        "meshcore", "/dev/ttyACM0", "!11223344",
        str(tmp_path / "c.toml"), str(tmp_path / "d1" / "obs.db"), str(tmp_path / "bk1"),
        "9108", "9109",
        "y",  # second instance
        "/dev/ttyUSB0", "!55667788",
        str(tmp_path / "mt.toml"), str(tmp_path / "d2" / "obs.db"), str(tmp_path / "bk2"),
        "9118", "",
        str(tmp_path / "bbs.db"),
        "y",
    ]  # fmt: skip
    assert sw.run_interactive(scripted(answers), lambda s: None, roots=[]) == 0
    a, b = load(tmp_path / "c.toml"), load(tmp_path / "mt.toml")
    assert a.database.bbs_path == b.database.bbs_path == str(tmp_path / "bbs.db")
    assert a.database.peer_path == b.database.path
    assert b.radio.transport == "meshtastic" and not b.web.enabled


def test_existing_config_kept_without_replace(tmp_path):
    target = tmp_path / "c.toml"
    target.write_text("old")
    sw.run_interactive(scripted(single_answers(tmp_path, ["n"])), lambda s: None, roots=[])
    assert target.read_text() == "old"
    assert not list(tmp_path.glob("*.bak-*"))


def test_replace_makes_backup(tmp_path):
    target = tmp_path / "c.toml"
    target.write_text("old")
    sw.run_interactive(scripted(single_answers(tmp_path, ["y"])), lambda s: None, roots=[])
    assert (tmp_path / "c.toml").read_text() != "old"
    baks = list(tmp_path.glob("c.toml.bak-*"))
    assert len(baks) == 1 and baks[0].read_text() == "old"


@pytest.mark.parametrize("cut", [3, 8, 10])
def test_cancel_leaves_nothing(tmp_path, cut):
    answers = single_answers(tmp_path)[:cut]  # EOF mid-flow
    assert sw.run_interactive(scripted(answers), lambda s: None, roots=[]) == 0
    assert list(tmp_path.iterdir()) == []


def test_decline_confirmation_leaves_nothing(tmp_path):
    answers = single_answers(tmp_path)
    answers[10] = "n"
    sw.run_interactive(scripted(answers), lambda s: None, roots=[])
    assert list(tmp_path.iterdir()) == []


def _inst(tmp_path, n, **kw):
    base = dict(
        transport="meshcore",
        device="/dev/x",
        config_path=str(tmp_path / f"c{n}.toml"),
        database=str(tmp_path / f"d{n}" / "obs.db"),
        backup_dir=str(tmp_path / f"bk{n}"),
        metrics_port=9100 + n,
    )
    base.update(kw)
    return sw.Instance(**base)


def test_collisions_via_symlink_alias(tmp_path):
    (tmp_path / "d1").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "d1")
    a = _inst(tmp_path, 1)
    b = _inst(tmp_path, 2, database=str(tmp_path / "alias" / "obs.db"))
    problems = sw.check_collisions([a, b])
    assert any("database" in p for p in problems)
    assert any("state directory" in p for p in problems)


def test_collisions_ports_backup_and_bbs(tmp_path):
    a = _inst(tmp_path, 1, bbs_path=str(tmp_path / "bbs.db"))
    b = _inst(tmp_path, 2, metrics_port=9101, backup_dir=a.backup_dir)
    problems = " | ".join(sw.check_collisions([a, b]))
    assert "port 9101" in problems
    assert "backup directory" in problems
    assert "inconsistent shared store" in problems
    c = _inst(tmp_path, 3, bbs_path=str(tmp_path / "d3" / "obs.db"))
    assert any("bbs_path" in p for p in sw.check_collisions([c]))


def test_default_metrics_port_collides(tmp_path):
    a = _inst(tmp_path, 1, metrics_port=None)
    b = _inst(tmp_path, 2, metrics_port=None)
    assert any("9108" in p for p in sw.check_collisions([a, b]))


def test_interactive_collision_writes_nothing(tmp_path):
    answers = single_answers(tmp_path)
    answers[6] = answers[7] = "9300"  # metrics == web port
    assert sw.run_interactive(scripted(answers), lambda s: None, roots=[]) == 2
    assert list(tmp_path.iterdir()) == []


def test_invalid_config_is_actionable(tmp_path):
    inst = _inst(tmp_path, 1, expected_node_id="not-an-id")
    msg = sw.validate_toml(sw.render_toml(inst), "x.toml")
    assert msg and "invalid" in msg and "Re-run setup" in msg


def test_no_device_is_opened(tmp_path, monkeypatch):
    dev = tmp_path / "ttyACM9"
    dev.write_text("")
    real_open = builtins.open

    def guarded(path, *a, **k):
        if "ttyACM" in str(path):
            raise AssertionError("device opened")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded)
    assert sw.discover_serial([str(tmp_path / "ttyACM*")]) == [str(dev)]
    answers = single_answers(tmp_path)
    answers[1] = "1"  # pick discovered candidate
    sw.run_interactive(scripted(answers), lambda s: None, roots=[str(tmp_path / "ttyACM*")])
    assert load(tmp_path / "c.toml").radio.device == str(dev)


def ns(tmp_path, **kw):
    p = argparse.ArgumentParser()
    sw.add_arguments(p)
    argv = [
        "--non-interactive", "--transport", "meshtastic", "--device", "/dev/ttyUSB0",
        "--output", str(tmp_path / "c.toml"), "--database", str(tmp_path / "o.db"),
        "--backup-dir", str(tmp_path / "bk"), "--expected-node-id", "!11223344",
    ]  # fmt: skip
    return p.parse_args(argv + kw.get("extra", []))


def test_noninteractive_dry_run_and_write(tmp_path):
    out = []
    assert sw.run_noninteractive(ns(tmp_path, extra=["--dry-run"]), out.append) == 0
    assert not (tmp_path / "c.toml").exists()
    assert sw.run_noninteractive(ns(tmp_path, extra=["--web-port", "9500"]), out.append) == 0
    assert load(tmp_path / "c.toml").web.port == 9500
    # existing file is kept without --replace
    before = (tmp_path / "c.toml").read_text()
    assert sw.run_noninteractive(ns(tmp_path), out.append) == 1
    assert (tmp_path / "c.toml").read_text() == before
    assert sw.run_noninteractive(ns(tmp_path, extra=["--replace"]), out.append) == 0
    assert len(list(tmp_path.glob("c.toml.bak-*"))) == 1


def test_noninteractive_missing_flags(tmp_path, capsys):
    args = ns(tmp_path)
    args.device = None
    assert sw.run_noninteractive(args) == 2
    assert "--device" in capsys.readouterr().err
