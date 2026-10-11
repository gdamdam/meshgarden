"""`run --debug`'s opt-in exchange trace: printed to stderr only, gated by an
explicit operator flag, and never reaching the rotating file log, the
database, metrics, or the status file. See meshgarden/
exchange_log.py and its two call sites (main.py's _process_obs, sender.py's
_guarded_send/note_routing_packet/_finish_expired)."""

import logging
import os
import stat

from meshgarden import exchange_log, ops
from meshgarden.commands import menu_text, with_quota_hint
from meshgarden.config import (
    BackupConfig,
    Config,
    DatabaseConfig,
    MetricsConfig,
    RadioConfig,
    RetentionConfig,
    ServiceConfig,
)
from meshgarden.main import Daemon, _create_owner_only_file, _SecureRotatingFileHandler
from tests.conftest import LAB_UNLOCK_WORD
from tests.fakes import FakeInterface, make_packet


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _cfg(tmp_path):
    return Config(
        radio=RadioConfig(device="/dev/fake", expected_node_id="!11223344"),
        service=ServiceConfig(),
        database=DatabaseConfig(path=str(tmp_path / "o.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "backups")),
        metrics=MetricsConfig(),
    )


def test_debug_off_leaves_the_exchange_logger_disabled(tmp_path):
    """With --debug off (the default), the exchange logger's own level
    keeps it below INFO (pytest's log-capturing plugin attaches its own
    handler to every propagate=False logger regardless, so handler
    presence itself isn't a useful signal here -- see log_line's
    isEnabledFor guard, which this exercises directly), so every
    log_line() call is a complete no-op -- the guarantee the rest of this
    file's "off" tests exercise end to end via real stderr/file/db output."""
    iface = FakeInterface(0x11223344)
    d = Daemon(_cfg(tmp_path), interface_factory=lambda dev: iface, start_metrics=False)
    try:
        d.start()
        assert not exchange_log._logger.isEnabledFor(logging.INFO)
    finally:
        d.stop()


def test_debug_off_dm_text_never_reaches_stderr_the_file_log_or_the_db(tmp_path, capsys, caplog):
    caplog.set_level("DEBUG")
    iface = FakeInterface(0x11223344)
    d = Daemon(_cfg(tmp_path), interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    try:
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=51001))
        d.drain()
        dump = "\n".join(d.conn.iterdump())
        assert menu_text() not in dump
    finally:
        d.stop()

    err = capsys.readouterr().err
    assert menu_text() not in err
    assert menu_text() not in caplog.text

    log_path = tmp_path / "logs" / "meshgarden.log"
    if log_path.exists():
        assert menu_text() not in log_path.read_text()


def test_debug_on_logs_a_prominent_startup_warning(tmp_path, caplog):
    iface = FakeInterface(0x11223344)
    with caplog.at_level(logging.WARNING, logger="meshgarden.main"):
        d = Daemon(
            _cfg(tmp_path), interface_factory=lambda dev: iface, start_metrics=False, debug=True
        )
        try:
            d.start()
        finally:
            d.stop()

    assert any(rec.levelno == logging.WARNING for rec in caplog.records)
    assert "unattended" in caplog.text or "systemd" in caplog.text


def test_debug_on_traces_the_dm_and_reply_to_stderr_only(tmp_path, capsys):
    """Note: this deliberately does NOT use caplog to assert absence from
    the rotating file log -- pytest's logging plugin attaches its own
    capturing handler directly to any propagate=False logger (ours
    included), so caplog would see the exchange trace regardless of
    whether it ever reaches the real rotating file handler. The real
    guarantee is checked against the actual log file and the database
    below."""
    iface = FakeInterface(0x11223344)
    d = Daemon(
        _cfg(tmp_path), interface_factory=lambda dev: iface, start_metrics=False, debug=True
    )
    d.iface = iface
    d.start()
    try:
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=51002))
        d.drain()
        dump = "\n".join(d.conn.iterdump())
        assert menu_text() not in dump
    finally:
        d.stop()

    err = capsys.readouterr().err
    assert "!aabbccdd" in err  # sender node id
    assert "cmd=MENU" in err  # parsed command
    assert "MENU" in err  # the DM text itself
    # The reply text handed to the radio: traced via `{body!r}`, so a
    # multi-line MENU reply appears with its embedded newline escaped.
    # (This is the first MENU command from this sender today, so the
    # quota hint reads 1/<default limit 60>.)
    assert repr(with_quota_hint(menu_text(), 1, 60, max_bytes=180)) in err

    # Still never reaches the rotating file log.
    log_path = tmp_path / "logs" / "meshgarden.log"
    if log_path.exists():
        assert menu_text() not in log_path.read_text()


def test_debug_on_traces_an_unknown_command_too(tmp_path, capsys):
    iface = FakeInterface(0x11223344)
    d = Daemon(
        _cfg(tmp_path), interface_factory=lambda dev: iface, start_metrics=False, debug=True
    )
    d.iface = iface
    d.start()
    try:
        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, text="not a command", packet_id=51003)
        )
        d.drain()
    finally:
        d.stop()

    err = capsys.readouterr().err
    assert "cmd=unknown" in err
    assert "not a command" in err


def _lab_cfg(tmp_path):
    return Config(
        radio=RadioConfig(device="/dev/fake", expected_node_id="!11223344"),
        service=ServiceConfig(lab_unlock_word=LAB_UNLOCK_WORD),
        database=DatabaseConfig(path=str(tmp_path / "o.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "backups")),
        metrics=MetricsConfig(),
    )


def test_debug_on_never_logs_the_lab_unlock_word(tmp_path, capsys):
    """Finding-4 regression: docs promise the LAB unlock word is never
    logged, but the debug exchange trace's `text={obs.text!r}` ran
    unconditionally, including for Command.LAB_UNLOCK -- whose whole
    message IS the configured word."""
    iface = FakeInterface(0x11223344)
    d = Daemon(
        _lab_cfg(tmp_path), interface_factory=lambda dev: iface, start_metrics=False, debug=True
    )
    d.iface = iface
    d.start()
    try:
        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, text=LAB_UNLOCK_WORD, packet_id=51010)
        )
        d.drain()
    finally:
        d.stop()

    err = capsys.readouterr().err
    assert "cmd=LAB_UNLOCK" in err
    assert LAB_UNLOCK_WORD not in err
    assert "redacted" in err


def test_debug_on_traces_the_ack_outcome(tmp_path, capsys):
    iface = FakeInterface(0x11223344)
    d = Daemon(
        _cfg(tmp_path), interface_factory=lambda dev: iface, start_metrics=False, debug=True
    )
    d.iface = iface
    d.start()
    try:
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="HELP", packet_id=51004))
        d.drain()
        tx = d.conn.execute(
            "SELECT tx_packet_id FROM processed_requests WHERE packet_id=51004"
        ).fetchone()[0]
        d.iface.emit_packet(
            make_packet(
                from_num=0xAABBCCDD,
                to_num=d.my_node_num,
                portnum="ROUTING_APP",
                text=None,
                packet_id=51005,
                request_id=tx,
                routing_error="NONE",
            )
        )
        d.drain()
    finally:
        d.stop()

    err = capsys.readouterr().err
    assert "outcome=acked" in err


def test_create_owner_only_file_is_0600_even_under_a_permissive_umask(tmp_path):
    """Pre-creating the exchange trace file must never leave it briefly
    readable at the ambient umask's default mode: `os.open`'s own `mode`
    argument is still subject to umask, so the explicit `os.chmod` inside
    `_create_owner_only_file` is what actually guarantees 0600 regardless."""
    path = str(tmp_path / "exchange.log")
    old_umask = os.umask(0o022)
    try:
        _create_owner_only_file(path)
    finally:
        os.umask(old_umask)
    assert os.path.exists(path)
    assert _mode(path) == 0o600


def test_create_owner_only_file_never_truncates_an_existing_file(tmp_path):
    path = str(tmp_path / "exchange.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write("existing line\n")
    _create_owner_only_file(path)
    assert (tmp_path / "exchange.log").read_text() == "existing line\n"
    assert _mode(path) == 0o600


def test_secure_rotating_file_handler_keeps_every_reopened_file_at_0600(tmp_path):
    """Regression guard: the stock RotatingFileHandler only ever gets
    chmod'ed once, right after construction -- the FRESH active file it
    opens after a rollover is never re-chmod'ed, so it would be left at
    the ambient umask's default mode. `_SecureRotatingFileHandler._open()`
    must chmod every file it opens, including the one doRollover creates."""
    path = str(tmp_path / "exchange.log")
    old_umask = os.umask(0o022)
    try:
        _create_owner_only_file(path)
        handler = _SecureRotatingFileHandler(
            path, maxBytes=1, backupCount=2, delay=True
        )
        try:
            handler.setFormatter(logging.Formatter("%(message)s"))
            record = logging.LogRecord(
                "x", logging.INFO, __file__, 1, "a long enough line to roll over", (), None
            )
            handler.emit(record)
            handler.emit(record)  # forces doRollover (maxBytes=1)
            assert _mode(path) == 0o600
            backup = path + ".1"
            if os.path.exists(backup):
                assert _mode(backup) == 0o600
        finally:
            handler.close()
    finally:
        os.umask(old_umask)


def test_debug_on_writes_the_exchange_trace_file_at_0600_with_dm_and_reply(tmp_path):
    """`run --debug`'s trace file (ops.exchange_trace_path(cfg)) must exist,
    be owner-only (0600), and contain both the DM text and the reply after
    a MENU round trip -- separate from (and never reaching) the rotating
    daemon log."""
    iface = FakeInterface(0x11223344)
    d = Daemon(_cfg(tmp_path), interface_factory=lambda dev: iface, start_metrics=False, debug=True)
    d.iface = iface
    d.start()
    try:
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=51006))
        d.drain()
    finally:
        d.stop()

    trace_path = ops.exchange_trace_path(d.cfg)
    assert os.path.exists(trace_path)
    assert _mode(trace_path) == 0o600
    with open(trace_path, encoding="utf-8") as f:
        text = f.read()
    assert "MENU" in text
    assert repr(with_quota_hint(menu_text(), 1, 60, max_bytes=180)) in text

    log_path = tmp_path / "logs" / "meshgarden.log"
    if log_path.exists():
        assert menu_text() not in log_path.read_text()


def test_debug_off_never_creates_the_exchange_trace_file(tmp_path):
    iface = FakeInterface(0x11223344)
    d = Daemon(_cfg(tmp_path), interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    try:
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=51007))
        d.drain()
    finally:
        d.stop()

    trace_path = ops.exchange_trace_path(d.cfg)
    assert not os.path.exists(trace_path)

    for root, _dirs, files in os.walk(tmp_path / "logs"):
        for name in files:
            with open(os.path.join(root, name), encoding="utf-8") as f:
                assert "MENU" not in f.read()


def test_debug_flag_never_appears_in_the_systemd_unit(tmp_path):
    """Companion to tests/test_packaging.py's static guard: same assertion,
    exercised here as a design sanity check alongside the daemon-level
    behavior tests in this file."""
    import pathlib

    unit = (
        pathlib.Path(__file__).resolve().parent.parent
        / "systemd"
        / "meshtastic-observatory.service"
    ).read_text()
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert "--debug" not in exec_start


def test_rejection_line_carries_the_text_only_when_given(capsys):
    """Operator request: a refused message (unknown command, channel_dm_only,
    ineligible packet) is traced WITH its text so `mgarden tail` shows what
    was refused. Callers that do not pass `text` keep the old shape."""
    handler = exchange_log.configure(True)
    try:
        exchange_log.log_rejection("!11223344", "channel_dm_only", command="SUGGEST",
                                   text="SUGGEST a bench")
        exchange_log.log_rejection("!11223344", "blocked", command="MENU")
    finally:
        exchange_log.deconfigure(handler)
    err = capsys.readouterr().err
    first, second = [line for line in err.splitlines() if "rejected" in line]
    assert "reason=channel_dm_only" in first and "text='SUGGEST a bench'" in first
    assert "reason=blocked" in second and "text=" not in second
