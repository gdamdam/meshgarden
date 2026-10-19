import json
import os
import sqlite3
import time
from unittest import mock

import pytest

from meshgarden import health_report as hr
from meshgarden.config import (
    BackupConfig,
    Config,
    ConfigError,
    DatabaseConfig,
    HealthConfig,
    MetricsConfig,
    RadioConfig,
    RetentionConfig,
    ServiceConfig,
    parse_config,
)
from meshgarden.database import connect, migrate

NOW = 1_800_000_000  # fixed epoch; tests pass `now` explicitly


def make_cfg(tmp_path, **health):
    return Config(
        radio=RadioConfig(device="/dev/fake", expected_node_id="!11223344"),
        service=ServiceConfig(timezone="UTC"),
        database=DatabaseConfig(path=str(tmp_path / "o.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "bk")),
        metrics=MetricsConfig(),
        health=HealthConfig(**health),
    )


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "o.db"))
    migrate(c)
    return c


def seed(conn, state, n, sender="!deadbeef"):
    for i in range(n):
        conn.execute(
            "INSERT INTO processed_requests(sender_node_id, packet_id, first_seen_at, "
            "expires_at, command, state, reason, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (sender, hash((state, i)) % 10**9, NOW - 60, NOW + 3600, "SIGN", state,
             "secret reason text", NOW - 60),
        )  # fmt: skip
    conn.commit()


def test_report_contents_have_no_ids_or_text(tmp_path, conn):
    cfg = make_cfg(tmp_path)
    seed(conn, "acked", 3)
    seed(conn, "failed", 2)
    seed(conn, "expired", 1)
    r = hr.build_report(conn, cfg, NOW)
    assert r["requests"]["acked"] == 3 and r["requests"]["failed"] == 2
    assert "delivery_failures" in r["problems"]
    assert r["schema"]["pending"] == 0
    blob = json.dumps(r) + hr.render_text(r)
    for forbidden in ("deadbeef", "secret reason", "Alice", "hello body"):
        assert forbidden not in blob


def test_backup_age_and_verification(tmp_path, conn):
    cfg = make_cfg(tmp_path)
    os.makedirs(cfg.backup.directory)
    good = os.path.join(cfg.backup.directory, "observatory-20270101T000000Z.db")
    sqlite3.connect(good).execute("CREATE TABLE t(x)").connection.close()
    os.utime(good, (NOW - 100, NOW - 100))
    r = hr.build_report(conn, cfg, NOW)
    assert r["backup"]["verified"] is True and r["backup"]["newest_age_seconds"] == 100
    bad = os.path.join(cfg.backup.directory, "observatory-20270102T000000Z.db")
    with open(bad, "wb") as f:
        f.write(b"not a database" * 100)
    r = hr.build_report(conn, cfg, NOW)
    assert "backup_failed_verification" in r["problems"]


def test_deltas_against_previous(tmp_path, conn):
    cfg = make_cfg(tmp_path)
    seed(conn, "failed", 2)
    first = hr.build_report(conn, cfg, NOW)
    seed(conn, "failed", 3, sender="!aaaa0000")
    second = hr.build_report(conn, cfg, NOW + 30, previous=first)
    assert second["deltas"]["failed_24h"] == 3
    assert second["deltas"]["requests_24h"] == 3
    assert hr.build_report(conn, cfg, NOW)["deltas"] == {}


def test_once_per_day_and_pruning(tmp_path, conn):
    cfg = make_cfg(tmp_path, enabled=True, hour=0, history_days=3)
    assert hr.run_scheduled(conn, cfg, NOW) is not None
    assert hr.run_scheduled(conn, cfg, NOW + 60) is None  # same local day
    files = os.listdir(hr.health_dir(cfg))
    assert len(files) == 1
    mode = os.stat(os.path.join(hr.health_dir(cfg), files[0])).st_mode & 0o777
    assert mode == 0o600
    for d in range(1, 6):
        assert hr.run_scheduled(conn, cfg, NOW + d * 86400) is not None
    assert len(os.listdir(hr.health_dir(cfg))) == 3


def test_write_report_never_duplicates(tmp_path, conn):
    cfg = make_cfg(tmp_path)
    r = hr.build_report(conn, cfg, NOW)
    assert hr.write_report(cfg, r) is not None
    assert hr.write_report(cfg, r) is None


def test_disabled_and_before_hour_do_nothing(tmp_path, conn):
    assert hr.run_scheduled(conn, make_cfg(tmp_path), NOW) is None
    late = make_cfg(tmp_path, enabled=True, hour=23)
    midnight = NOW - NOW % 86400
    assert hr.run_scheduled(conn, late, midnight + 3600) is None


def test_email_disabled_never_constructs_smtp(tmp_path, conn):
    cfg = make_cfg(tmp_path, enabled=True, hour=0)
    with mock.patch("smtplib.SMTP") as smtp:
        hr.run_scheduled(conn, cfg, NOW)
        assert hr.cmd_health_report(cfg, email=True, now=NOW) == 2
    smtp.assert_not_called()


def test_email_enabled_uses_smtp_once_per_day(tmp_path, conn):
    pw = tmp_path / "pw"
    pw.write_text("s3cret\n")
    cfg = make_cfg(
        tmp_path, enabled=True, hour=0, email_enabled=True, smtp_host="mail.test",
        smtp_username="u", smtp_password_file=str(pw), email_from="a@x", email_to=("b@x",),
    )  # fmt: skip
    with mock.patch("smtplib.SMTP") as smtp:
        hr.run_scheduled(conn, cfg, NOW)
        hr.run_scheduled(conn, cfg, NOW + 30)
    assert smtp.call_count == 1
    assert smtp.call_args.kwargs["timeout"] > 0
    inst = smtp.return_value.__enter__.return_value
    inst.starttls.assert_called_once()
    inst.login.assert_called_once_with("u", "s3cret")
    inst.send_message.assert_called_once()


def test_email_failure_not_retried(tmp_path, conn):
    pw = tmp_path / "pw"
    pw.write_text("x")
    cfg = make_cfg(
        tmp_path, enabled=True, hour=0, email_enabled=True, smtp_host="mail.test",
        smtp_username="u", smtp_password_file=str(pw), email_from="a@x", email_to=("b@x",),
    )  # fmt: skip
    with mock.patch("smtplib.SMTP", side_effect=OSError("down")) as smtp:
        hr.run_scheduled(conn, cfg, NOW)
        hr.run_scheduled(conn, cfg, NOW + 60)
    assert smtp.call_count == 1


def test_cli_prints_without_credentials(tmp_path, conn, capsys):
    cfg = make_cfg(tmp_path)
    conn.close()
    assert hr.cmd_health_report(cfg, as_json=True, write=True, now=int(time.time())) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["schema"]["pending"] == 0
    assert len(os.listdir(hr.health_dir(cfg))) == 1


def _toml(**health):
    return {
        "radio": {"device": "/d", "expected_node_id": "!11223344"},
        "database": {"path": "/x/o.db"},
        "backup": {"directory": "/x/bk"},
        "health": health,
    }


def test_config_validation():
    assert parse_config(_toml()).health.enabled is False
    with pytest.raises(ConfigError, match="requires"):
        parse_config(_toml(email_enabled=True, smtp_host="h"))
    with pytest.raises(ConfigError, match="hour"):
        parse_config(_toml(hour=24))
    with pytest.raises(ConfigError, match="unknown key"):
        parse_config(_toml(smtp_password="inline"))
    ok = parse_config(
        _toml(email_enabled=True, smtp_host="h", smtp_username="u",
              smtp_password_file="/p", email_from="a@x", email_to=["b@x"])
    )  # fmt: skip
    assert ok.health.email_to == ("b@x",)
