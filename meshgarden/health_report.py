"""Operator health digest: aggregate-only counts about the daemon's own health.

Complements digest.py (which summarizes mesh *activity* for one day): this
reports whether the garden itself is healthy -- delivery outcomes, radio
outages, queue pressure, storage, backup freshness/verification and schema
state -- with deltas against the previous stored report. Like every
operator-facing report here it never contains message bodies, node ids or
names. It never touches the radio; email is a separate, opt-in step.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import smtplib
import sqlite3
from datetime import datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

from meshgarden.database import MIGRATIONS, get_daemon_state, get_schema_version, set_daemon_state
from meshgarden.fortune import local_day

if TYPE_CHECKING:
    from meshgarden.config import Config

log = logging.getLogger(__name__)

HEALTH_DAY_KEY = "health_report_day"
_STATES = ("acked", "failed", "unknown", "expired", "dropped")
_SMTP_TIMEOUT_SECONDS = 20
_STATUS_STALE_SECONDS = 300


def health_dir(cfg: Config) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(cfg.database.path)), "health")


def _size(path: str) -> int:
    total = 0
    for p in (path, path + "-wal"):
        with contextlib.suppress(OSError):
            total += os.path.getsize(p)
    return total


def _read_status(cfg: Config) -> dict | None:
    path = os.path.join(os.path.dirname(os.path.abspath(cfg.database.path)), "status.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _backup_info(cfg: Config, now: int) -> dict:
    # Imported lazily: ops pulls in a lot, and only this check needs it.
    from meshgarden.ops import _check_db_integrity, _list_backup_files

    directory = cfg.backup.directory
    names = [n for n in _list_backup_files(directory) if n.endswith(".db")]
    info: dict[str, Any] = {"count": len(names), "newest_age_seconds": None, "verified": None}
    if not names:
        return info
    newest = os.path.join(directory, names[-1])
    try:
        info["newest_age_seconds"] = max(0, int(now - os.path.getmtime(newest)))
    except OSError:
        return info
    info["verified"] = _check_db_integrity(newest, "backup_integrity").ok
    return info


def build_report(
    conn: sqlite3.Connection, cfg: Config, now: int, previous: dict | None = None
) -> dict:
    tz = cfg.service.timezone
    since = now - 86400
    counts = dict.fromkeys(_STATES, 0)
    rows = conn.execute(
        "SELECT state, COUNT(*) FROM processed_requests WHERE first_seen_at >= ? GROUP BY state",
        (since,),
    ).fetchall()
    total = 0
    for state, n in rows:
        total += n
        if state in counts:
            counts[state] = n

    latest = MIGRATIONS[-1][0]
    version = get_schema_version(conn)
    status = _read_status(cfg)
    fresh = bool(status) and now - float(status.get("generated_at", 0)) <= _STATUS_STALE_SECONDS
    radio: dict[str, Any] = {"status_available": status is not None, "status_fresh": fresh}
    queue: dict[str, Any] = {}
    if status:
        radio["connected"] = bool(status.get("radio_connected"))
        radio["disconnected_seconds"] = status.get("radio_disconnected_seconds") or 0
        queue["depth"] = status.get("queue_depth") or 0
        queue["drops_1h"] = status.get("rate_limited_drops_1h") or 0
        queue["storage_pressure"] = (status.get("storage") or {}).get("pressure")
        # Only whether the last backup failed, never the error text.
        queue["last_backup_failed"] = bool(status.get("last_backup_error"))

    db_bytes = _size(cfg.database.path)
    bbs_bytes = _size(cfg.database.bbs_path) if cfg.database.bbs_path else 0
    backup = _backup_info(cfg, now)

    problems: list[str] = []
    if version < latest:
        problems.append("schema_migrations_pending")
    if status is None or not fresh:
        problems.append("status_file_missing_or_stale")
    elif not radio["connected"]:
        problems.append("radio_disconnected")
    if counts["failed"] + counts["unknown"] + counts["expired"]:
        problems.append("delivery_failures")
    if queue.get("last_backup_failed"):
        problems.append("last_backup_failed")
    if backup["count"] == 0:
        problems.append("no_backups")
    elif backup["verified"] is False:
        problems.append("backup_failed_verification")
    elif (backup["newest_age_seconds"] or 0) > max(1, cfg.backup.interval_hours) * 3600 * 2:
        problems.append("backup_stale")
    if queue.get("storage_pressure") not in (None, "ok"):
        problems.append("storage_pressure")

    metrics = {
        "requests_24h": total,
        "acked_24h": counts["acked"],
        "failed_24h": counts["failed"],
        "unknown_24h": counts["unknown"],
        "expired_24h": counts["expired"],
        "db_bytes": db_bytes,
        "bbs_bytes": bbs_bytes,
        "queue_depth": queue.get("depth", 0),
        "drops_1h": queue.get("drops_1h", 0),
        "radio_disconnected_seconds": radio.get("disconnected_seconds", 0),
        "backup_age_seconds": backup["newest_age_seconds"] or 0,
    }
    deltas: dict[str, int] = {}
    if previous and isinstance(previous.get("metrics"), dict):
        for key, value in metrics.items():
            old = previous["metrics"].get(key)
            if isinstance(old, (int, float)) and not isinstance(old, bool):
                deltas[key] = int(value - old)

    return {
        "day": local_day(now, tz),
        "generated_at": now,
        "schema": {"version": version, "latest": latest, "pending": max(0, latest - version)},
        "requests": {"window_hours": 24, "total": total, **counts},
        "radio": radio,
        "queue": queue,
        "storage": {"db_bytes": db_bytes, "bbs_bytes": bbs_bytes},
        "backup": backup,
        "problems": problems,
        "metrics": metrics,
        "deltas": deltas,
        "previous_day": previous.get("day") if previous else None,
    }


def _signed(n: int) -> str:
    return f"{n:+d}"


def render_text(report: dict) -> str:
    d = report.get("deltas", {})

    def delta(key: str) -> str:
        return f" ({_signed(d[key])})" if key in d else ""

    r = report["requests"]
    sc = report["schema"]
    b = report["backup"]
    m = report["metrics"]
    lines = [f"Meshgarden health report {report['day']}"]
    lines.append(f"Problems: {', '.join(report['problems']) if report['problems'] else 'none'}")
    lines.append(
        f"Requests (24h): {r['total']}{delta('requests_24h')}; acked {r['acked']}, "
        f"failed {r['failed']}{delta('failed_24h')}, unknown {r['unknown']}, "
        f"expired {r['expired']}, dropped {r['dropped']}"
    )
    radio = report["radio"]
    if radio.get("status_available"):
        conn = "connected" if radio.get("connected") else "DISCONNECTED"
        lines.append(f"Radio: {conn}, down {radio.get('disconnected_seconds', 0)}s now")
    else:
        lines.append("Radio: status unavailable (daemon not running?)")
    q = report["queue"]
    if q:
        lines.append(
            f"Queue: depth {q.get('depth', 0)}, drops last hour {q.get('drops_1h', 0)}"
            f"{delta('drops_1h')}"
        )
    lines.append(
        f"Storage: db {m['db_bytes']} B{delta('db_bytes')}, bbs {m['bbs_bytes']} B"
        f"{delta('bbs_bytes')}"
    )
    if b["count"]:
        ver = {True: "verified", False: "FAILED verification", None: "unverified"}[b["verified"]]
        lines.append(f"Backups: {b['count']}, newest {b['newest_age_seconds']}s old, {ver}")
    else:
        lines.append("Backups: none found")
    lines.append(f"Schema: {sc['version']}/{sc['latest']} ({sc['pending']} pending)")
    return "\n".join(lines) + "\n"


def _report_path(cfg: Config, day: str) -> str:
    return os.path.join(health_dir(cfg), f"health-{day}.json")


def _list_days(cfg: Config) -> list[str]:
    try:
        names = os.listdir(health_dir(cfg))
    except OSError:
        return []
    days = []
    for n in names:
        if n.startswith("health-") and n.endswith(".json"):
            day = n[len("health-") : -len(".json")]
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                continue
            days.append(day)
    return sorted(days)


def load_previous(cfg: Config, before_day: str) -> dict | None:
    for day in reversed([d for d in _list_days(cfg) if d < before_day]):
        try:
            with open(_report_path(cfg, day), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def write_report(cfg: Config, report: dict) -> str | None:
    """Writes the day's report once. Returns the path, or None if that day's
    file already exists (no duplicates, never overwritten)."""
    directory = health_dir(cfg)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    dest = _report_path(cfg, report["day"])
    if os.path.exists(dest):
        return None
    tmp = dest + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(report, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    return dest


def prune_reports(cfg: Config) -> int:
    days = _list_days(cfg)
    excess = days[: max(0, len(days) - cfg.health.history_days)]
    for day in excess:
        os.remove(_report_path(cfg, day))
    return len(excess)


def send_email(cfg: Config, report: dict) -> None:
    """Sends the text report over SMTP. Callers must have checked
    `cfg.health.email_enabled`; raises on any failure."""
    h = cfg.health
    if not h.email_enabled:
        raise RuntimeError("email is disabled: set [health] email_enabled = true and SMTP fields")
    with open(h.smtp_password_file, encoding="utf-8") as f:
        password = f.read().strip()
    msg = EmailMessage()
    msg["Subject"] = f"Meshgarden health {report['day']}: " + (
        "attention" if report["problems"] else "ok"
    )
    msg["From"] = h.email_from
    msg["To"] = ", ".join(h.email_to)
    msg.set_content(render_text(report))
    with smtplib.SMTP(h.smtp_host, h.smtp_port, timeout=_SMTP_TIMEOUT_SECONDS) as smtp:
        if h.smtp_starttls:
            smtp.starttls()
        smtp.login(h.smtp_username, password)
        smtp.send_message(msg)


def run_scheduled(conn: sqlite3.Connection, cfg: Config, now: int) -> dict | None:
    """Daemon hook: once per local day, at/after `[health] hour`, when enabled.
    The day is recorded BEFORE any work, so a failure (disk, SMTP) is logged
    and not retried until tomorrow. Never touches the radio."""
    h = cfg.health
    if not h.enabled:
        return None
    local = datetime.fromtimestamp(now, tz=_zone(cfg))
    if local.hour < h.hour:
        return None
    today = local.strftime("%Y-%m-%d")
    if get_daemon_state(conn, HEALTH_DAY_KEY) == today:
        return None
    set_daemon_state(conn, HEALTH_DAY_KEY, today, now)
    try:
        report = build_report(conn, cfg, now, load_previous(cfg, today))
        write_report(cfg, report)
        prune_reports(cfg)
    except Exception:
        log.exception("health report failed for %s", today)
        return None
    if h.email_enabled:
        try:
            send_email(cfg, report)
        except Exception:
            log.exception("health report email failed for %s (not retried today)", today)
    return report


def _zone(cfg: Config):
    from zoneinfo import ZoneInfo

    return ZoneInfo(cfg.service.timezone)


def cmd_health_report(
    cfg: Config,
    *,
    as_json: bool = False,
    write: bool = False,
    email: bool = False,
    now: int | None = None,
) -> int:
    """CLI body. Read-only against the database; `--write` stores the day's
    report (once), `--email` sends only when `[health] email_enabled`."""
    import sys
    import time

    now = int(time.time()) if now is None else now
    if email and not cfg.health.email_enabled:
        print(
            "error: email is not enabled. Set [health] email_enabled = true plus smtp_host, "
            "smtp_username, smtp_password_file, email_from and email_to in the config.",
            file=sys.stderr,
        )
        return 2
    if not os.path.exists(cfg.database.path):
        print(f"error: database not found: {cfg.database.path}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{cfg.database.path}?mode=ro", uri=True)
    try:
        today = local_day(now, cfg.service.timezone)
        report = build_report(conn, cfg, now, load_previous(cfg, today))
    finally:
        conn.close()
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report), end="")
    if write:
        path = write_report(cfg, report)
        note = f"wrote {path}" if path else "report for today already exists; not overwritten"
        print(note, file=sys.stderr)
        prune_reports(cfg)
    if email:
        try:
            send_email(cfg, report)
        except Exception as exc:
            print(f"error: email failed: {exc}", file=sys.stderr)
            return 1
    return 0
