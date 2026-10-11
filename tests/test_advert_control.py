import json
import os
import socket
import stat
import tempfile
from pathlib import Path

import pytest

from meshgarden.advert_control import AdvertControl, request_advert, socket_path
from meshgarden.main import Daemon, main
from tests.test_daemon_meshcore import SELF_PUBLIC_KEY, _cfg
from tests.test_meshcore_client import _StubMC


@pytest.fixture
def state_dir():
    # Unix socket paths have a short platform limit; pytest's default may exceed it.
    with tempfile.TemporaryDirectory(prefix="mg-advert-", dir="/tmp") as path:
        yield Path(path)


def test_cli_advert_uses_running_radio_and_respects_cooldown(state_dir, monkeypatch, capsys):
    meshcore = pytest.importorskip("meshcore")
    stub = _StubMC({"public_key": SELF_PUBLIC_KEY})
    connections = []

    async def create_serial(cls, *args, **kwargs):
        connections.append(args)
        return stub

    monkeypatch.setattr(meshcore.MeshCore, "create_serial", classmethod(create_serial))
    cfg = _cfg(state_dir, radio_kwargs={"meshcore_channel": ""})
    config_path = state_dir / "config.toml"
    config_path.write_text(
        '[radio]\ndevice = "/dev/fake"\nexpected_node_id = "!11223344"\n'
        'transport = "meshcore"\n[database]\n'
        f'path = "{cfg.database.path}"\n'
        f'[backup]\ndirectory = "{state_dir / "backups"}"\n'
    )
    daemon = Daemon(cfg, start_metrics=False)
    try:
        daemon.start()
        path = socket_path(cfg.database.path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stub.commands.send_advert_calls == [True]  # Startup advert.
        daemon._meshcore_client._last_advert_monotonic -= 61
        assert main(["--config", str(config_path), "advert"]) == 0
        assert "flood advertisement accepted by the radio" in capsys.readouterr().out
        assert stub.commands.send_advert_calls == [True, True]
        assert len(connections) == 1
        assert main(["--config", str(config_path), "advert"]) == 1
        assert "cooldown" in capsys.readouterr().err
        assert stub.commands.send_advert_calls == [True, True]
        daemon._meshcore_client._last_advert_monotonic -= 61
        daemon._transport._expected_node_id = "!deadbeef"
        assert main(["--config", str(config_path), "advert"]) == 1
        assert "radio not ready" in capsys.readouterr().err
        assert stub.commands.send_advert_calls == [True, True]
    finally:
        daemon.stop()
    assert not os.path.exists(path)


def test_advert_requires_meshcore(state_dir, capsys):
    config_path = state_dir / "config.toml"
    config_path.write_text(
        '[radio]\ndevice="/dev/fake"\nexpected_node_id="!11223344"\n'
        f'[database]\npath="{state_dir / "o.db"}"\n'
        f'[backup]\ndirectory="{state_dir / "backups"}"\n'
    )
    assert main(["--config", str(config_path), "advert"]) == 2
    assert "requires radio.transport = meshcore" in capsys.readouterr().err


def test_missing_daemon_has_actionable_error(state_dir):
    ok, message = request_advert(str(state_dir / "o.db"))
    assert not ok
    assert "start or restart" in message


def test_control_rejects_unknown_requests_and_recovers_stale_socket(state_dir):
    path = socket_path(str(state_dir / "o.db"))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(path)
    calls = []
    server = AdvertControl(path, lambda: calls.append(1))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(path)
            client.sendall(b"anything else\n")
            server.poll()
            assert json.loads(client.recv(2048))["ok"] is False
        assert calls == []
        with pytest.raises(OSError, match="already in use"):
            AdvertControl(path, lambda: (True, "unexpected"))
        server.poll()  # Active-socket probe must not send an advert.
        assert calls == []
    finally:
        server.close()


def test_control_never_replaces_a_regular_file(state_dir):
    path = state_dir / "advert.sock"
    path.write_text("keep")
    with pytest.raises(OSError, match="non-socket"):
        AdvertControl(str(path), lambda: (True, "unexpected"))
    assert path.read_text() == "keep"
