"""`run --debug`'s opt-in exchange trace: stderr only, never the rotating
file log, the database, metrics, or the status file.

Deliberately its own tiny module (not main.py or sender.py) so both can call
`log_line` without a circular import (main.py already imports sender.py), and
so the packaging guard that forbids logging message content
(tests/test_packaging.py::test_no_source_file_logs_message_content) can carry
one narrow, named exemption -- calls of the exact shape `exchange_log.log_line(`
-- instead of a blanket one.

`_logger` has `propagate = False` and, until `configure(True)` is called (only
ever from `Daemon.__init__` when the operator passes `run --debug`), carries
no handler and stays above INFO level -- so `log_line`'s `isEnabledFor` guard
makes every call a complete no-op in the default (debug off) case.
"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime

NAME = "meshgarden.exchange"

_logger = logging.getLogger(NAME)
_logger.propagate = False
_logger.setLevel(logging.WARNING)  # disabled by default: isEnabledFor(INFO) is False
_names: dict[str, str] = {}
_names_lock = threading.Lock()


def configure(enabled: bool) -> logging.Handler | None:
    """Attaches a bare stderr StreamHandler and lowers the logger to INFO
    when `enabled`; otherwise a no-op, leaving the logger disabled. Returns
    the handler (or None) so the caller can pass it to `deconfigure` at
    shutdown."""
    if not enabled:
        return None
    _logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    return handler


def deconfigure(handler: logging.Handler | None) -> None:
    """Reverses `configure`: detaches the handler (if any) and restores the
    disabled default level, so a later Daemon in the same process (as tests
    construct many) starts clean."""
    if handler is None:
        return
    _logger.removeHandler(handler)
    handler.close()
    _logger.setLevel(logging.WARNING)
    with _names_lock:
        _names.clear()


def remember_node_name(node_id: str, name: str | None) -> None:
    """Cache a display name only for the lifetime of an enabled debug run."""
    if not enabled() or not name:
        return
    with _names_lock:
        _names[node_id] = name


def node_label(node_id: str) -> str:
    """Keep the stable ID in every log label even when a name is known."""
    with _names_lock:
        name = _names.get(node_id)
    return f"{name} ({node_id})" if name else node_id


def log_line(line: str) -> None:
    """THE single call site exempted from the packaging guard against
    logging message content -- narrowly named so the guard's regex can
    target exactly this and nothing else. See
    test_no_source_file_logs_message_content and its companion negative
    self-check."""
    if _logger.isEnabledFor(logging.INFO):
        _logger.info(line)


def enabled() -> bool:
    return _logger.isEnabledFor(logging.INFO)


def log_rejection(
    sender: str,
    reason: str,
    *,
    command: str = "UNKNOWN",
    now: int | None = None,
    packet_id: int | None = None,
    cooldown_seconds: int | None = None,
    text: str | None = None,
) -> None:
    """Debug-only diagnostic using IDs and reason codes. `text` (the
    rejected message itself) is included only when the caller passes it --
    the eligibility, unknown-command and channel_dm_only sites do, so an
    operator reading `mgarden tail` can see WHAT was refused; it is debug
    output only, like every line this module writes."""
    if not enabled():
        return
    ts = (
        datetime.fromtimestamp(now).astimezone() if now is not None else datetime.now().astimezone()
    )
    fields = (
        f"{ts.isoformat(timespec='seconds')} rejected from={node_label(sender)} "
        f"cmd={command} reason={reason}"
    )
    if packet_id is not None:
        fields += f" packet={packet_id}"
    if cooldown_seconds is not None:
        fields += f" cooldown_seconds={cooldown_seconds}"
    if text is not None:
        fields += f" text={text!r}"
    log_line(fields)
