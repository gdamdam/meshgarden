import contextlib
import os
import random
import sqlite3
from dataclasses import replace

import pytest

from meshgarden.config import (
    BackupConfig,
    Config,
    DatabaseConfig,
    MetricsConfig,
    RadioConfig,
    RetentionConfig,
    ServiceConfig,
)
from meshgarden.main import Daemon
from tests.fakes import FakeTransport

# LAB tier (Task 13): the shared test daemon has the tier enabled, with the
# standard senders these fixtures/tests use ("!aabbccdd", the default
# make_packet sender, and "!99999999", the secondary sender several MAIL
# tests use) pre-unlocked -- see `_unlock_standard_lab_senders` below -- so
# every pre-existing POST/READ/BOARDS/MAIL test written before this tier
# existed keeps passing unchanged. A test that wants to exercise the LOCKED
# path uses a node id that was never unlocked, or its own bespoke Config
# with `lab_unlock_word=""`.
LAB_UNLOCK_WORD = "SUPERGARDENME"


def _cfg(tmp_path, expected="!11223344"):
    return Config(
        radio=RadioConfig(device="/dev/fake", expected_node_id=expected),
        service=ServiceConfig(lab_unlock_word=LAB_UNLOCK_WORD),
        database=DatabaseConfig(path=str(tmp_path / "o.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "backups")),
        metrics=MetricsConfig(),
    )


def _unlock_standard_lab_senders(conn) -> None:
    conn.execute("INSERT INTO lab_nodes (node_id, unlocked_at) VALUES ('!aabbccdd', 0)")
    conn.execute("INSERT INTO lab_nodes (node_id, unlocked_at) VALUES ('!99999999', 0)")
    conn.commit()


@pytest.fixture(autouse=True)
def _close_sqlite_connections_opened_during_test(monkeypatch):
    """L50: every sqlite3.Connection opened anywhere during a test (however
    it got there -- database.connect(), a test's own sqlite3.connect(), a
    Daemon's worker threads, ...) is closed at teardown.

    A number of tests open a raw connection via database.connect() (or
    sqlite3.connect() directly) and never close it, which the garbage
    collector eventually flags as a ResourceWarning -- often attributed to
    an unrelated LATER test, since collection happens whenever the GC
    happens to run. That makes `pytest -W error::ResourceWarning` unusable
    as a leak detector. Tracking every connection at the point it's opened
    (by wrapping the one function everything funnels through --
    sqlite3.connect itself) and closing it here, once, closes the leak at
    the root instead of editing every test. A connection a test or fixture
    already closed itself is unaffected: closing an already-closed
    sqlite3.Connection is a no-op.
    """
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def _tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", _tracking_connect)
    yield
    for conn in opened:
        with contextlib.suppress(Exception):
            conn.close()


# Fixed seed for the `daemon` fixture's HAIKU template selection, so a test
# asserting an exact HAIKU reply gets a reproducible choice across runs.
# ICHING needs no such seam -- it is a pure function of (sender, day, salt),
# never randomized; see fortune.py.
HAIKU_RNG_SEED = 20260913


@pytest.fixture
def daemon(tmp_path):
    cfg = _cfg(tmp_path)
    # FakeTransport is the real MeshtasticTransport wired to a FakeInterface
    # (see tests/fakes.py) -- Daemon's `transport=` seam (task 2B fix round
    # 1) lets the fixture inject it fully built, and Daemon rebinds its
    # ingest sink/metrics/heartbeat hooks onto the FakeInterface-backed
    # RadioAdapter it already owns.
    ft = FakeTransport(0x11223344, cfg.radio.expected_node_id, service_cfg=cfg.service)
    d = Daemon(
        cfg,
        transport=ft,
        start_metrics=False,
        haiku_rng=random.Random(HAIKU_RNG_SEED),
    )
    d.iface = ft.iface
    d.start()
    _unlock_standard_lab_senders(d.conn)
    yield d
    d.stop()


@pytest.fixture
def daemon_mismatched(tmp_path):
    cfg = _cfg(tmp_path)
    ft = FakeTransport(0xDEADBEEF, cfg.radio.expected_node_id, service_cfg=cfg.service)
    d = Daemon(cfg, transport=ft, start_metrics=False)
    d.iface = ft.iface
    d.start()
    yield d
    d.stop()


@pytest.fixture
def daemon_full_storage(tmp_path):
    """A daemon whose storage_report().publication_paused is True from the
    start: a tiny 1 MiB cap with a pre-filled log file already over the
    critical (95%) threshold."""
    logs_dir = tmp_path / "logs"
    os.makedirs(logs_dir, exist_ok=True)
    with open(logs_dir / "filler.log", "wb") as f:
        f.write(b"0" * 1_030_000)

    cfg = replace(_cfg(tmp_path), retention=RetentionConfig(max_managed_storage_mib=1))
    ft = FakeTransport(0x11223344, cfg.radio.expected_node_id, service_cfg=cfg.service)
    d = Daemon(cfg, transport=ft, start_metrics=False)
    d.iface = ft.iface
    d.start()
    _unlock_standard_lab_senders(d.conn)
    yield d
    d.stop()
