"""`mgarden setup`: guided, validated config creation.

Filesystem-only by design: serial candidates come from globbing device
directories (a port is never opened), nothing is started, installed or
transmitted, and the only side effect is writing the chosen config file(s)
after a preview, `config.parse_config` validation and explicit confirmation.
Input/output are injectable so the whole flow is scriptable in tests.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import shutil
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from meshgarden.config import ConfigError, MetricsConfig, parse_config

SERIAL_GLOBS = (
    "/dev/serial/by-id/*",
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
    "/dev/cu.usbmodem*",
)
TRANSPORTS = ("meshcore", "meshtastic")
_PLACEHOLDER_NODE_ID = "!00000000"
_SUFFIX = {"meshcore": "mc", "meshtastic": "mt"}


class Cancelled(Exception):
    """Operator cancelled (EOF, Ctrl-C or an explicit no)."""


@dataclass
class Instance:
    transport: str
    device: str
    config_path: str
    database: str
    backup_dir: str
    expected_node_id: str = _PLACEHOLDER_NODE_ID
    metrics_port: int | None = None  # None = daemon default (still binds 9108)
    web_port: int | None = None  # None = dashboard disabled
    bbs_path: str = ""
    peer_path: str = ""


def discover_serial(patterns: Sequence[str] = SERIAL_GLOBS) -> list[str]:
    """Candidate device paths from the filesystem only (glob; never opens)."""
    found: list[str] = []
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            if path not in found:
                found.append(path)
    return found


def _abs(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _real(path: str) -> str:
    # realpath resolves symlinks (and tolerates paths that do not exist yet),
    # so two spellings of one file/dir compare equal.
    return os.path.realpath(os.path.expanduser(path))


def effective_ports(inst: Instance) -> dict[str, int]:
    """Ports the daemon would actually bind: metrics always (default 9108),
    the dashboard only when a web port is set."""
    ports = {"metrics": inst.metrics_port or MetricsConfig().port}
    if inst.web_port is not None:
        ports["web"] = inst.web_port
    return ports


def check_collisions(instances: Sequence[Instance]) -> list[str]:
    """Human-readable problems that would make instances clobber each other
    (empty list = fine). Comparison is by realpath, so symlink aliases count."""
    problems: list[str] = []

    for i, inst in enumerate(instances):
        label = f"instance {i + 1} ({inst.transport})"
        db = _real(inst.database)
        if inst.bbs_path and _real(inst.bbs_path) == db:
            problems.append(f"{label}: bbs_path resolves to the same file as its database")
        if _real(inst.config_path) == db:
            problems.append(f"{label}: config path and database are the same file")
        if inst.peer_path and _real(inst.peer_path) == db:
            problems.append(f"{label}: peer_path resolves to its own database")
        ports = effective_ports(inst)
        if len(set(ports.values())) != len(ports):
            problems.append(f"{label}: metrics and dashboard ports are the same")

    if len(instances) < 2:
        return problems

    # Every instance must own its state dir (status.json, health/, digest/
    # live beside the database), config file, backup dir and database.
    def owners(name: str, key: Callable[[Instance], str]) -> None:
        seen: dict[str, int] = {}
        for i, inst in enumerate(instances):
            real = key(inst)
            if real in seen:
                problems.append(
                    f"instances {seen[real] + 1} and {i + 1} share the same {name}: {real}"
                )
            seen.setdefault(real, i)

    owners("config file", lambda x: _real(x.config_path))
    owners("database", lambda x: _real(x.database))
    owners("state directory", lambda x: os.path.dirname(_real(x.database)))
    owners("backup directory", lambda x: _real(x.backup_dir))

    seen_ports: dict[int, str] = {}
    for i, inst in enumerate(instances):
        for kind, port in effective_ports(inst).items():
            tag = f"instance {i + 1} {kind}"
            if port in seen_ports and not seen_ports[port].startswith(f"instance {i + 1} "):
                problems.append(f"port {port} used by both {seen_ports[port]} and {tag}")
            seen_ports.setdefault(port, tag)

    bbs = {i for i, x in enumerate(instances) if x.bbs_path}
    if bbs and len(bbs) != len(instances):
        problems.append(
            "inconsistent shared store: only some instances set bbs_path; "
            "set it on every instance or none"
        )
    elif bbs:
        reals = {_real(x.bbs_path) for x in instances}
        if len(reals) > 1:
            problems.append("instances point bbs_path at different files; they must share one")
        db_reals = {_real(x.database) for x in instances}
        if reals & db_reals:
            problems.append("bbs_path equals an instance's database path")
    return problems


def _q(value: str) -> str:
    # JSON string escaping is a valid TOML basic string for our inputs.
    return json.dumps(value, ensure_ascii=True)


def render_toml(inst: Instance) -> str:
    lines = [
        f"# Generated by `mgarden setup` ({inst.transport}).",
        "",
        "[radio]",
        f"transport = {_q(inst.transport)}",
        f"device = {_q(inst.device)}",
        f"expected_node_id = {_q(inst.expected_node_id)}",
        "",
        "[database]",
        f"path = {_q(inst.database)}",
    ]
    if inst.bbs_path:
        lines.append(f"bbs_path = {_q(inst.bbs_path)}")
    if inst.peer_path:
        lines.append(f"peer_path = {_q(inst.peer_path)}")
    lines += ["", "[backup]", f"directory = {_q(inst.backup_dir)}"]
    if inst.metrics_port is not None:
        lines += ["", "[metrics]", f"port = {inst.metrics_port}"]
    if inst.web_port is not None:
        lines += ["", "[web]", "enabled = true", f"port = {inst.web_port}"]
    return "\n".join(lines) + "\n"


def validate_toml(text: str, config_path: str) -> str | None:
    """None if `text` parses as a config, else an actionable message."""
    try:
        parse_config(tomllib.loads(text))
    except ConfigError as exc:
        return (
            f"The config for {config_path} is invalid: {exc}. Re-run setup and correct that answer."
        )
    except tomllib.TOMLDecodeError as exc:
        return f"The config for {config_path} is not valid TOML: {exc}."
    return None


def _write_atomic(path: str, text: str) -> None:
    target = _real(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".setup-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _backup_existing(path: str, now: float) -> str:
    target = _real(path)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(now))
    dest = f"{target}.bak-{stamp}"
    n = 1
    while os.path.exists(dest):
        dest = f"{target}.bak-{stamp}-{n}"
        n += 1
    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as out, open(target, "rb") as src:
        shutil.copyfileobj(src, out)
    os.chmod(dest, 0o600)
    return dest


def apply(
    instances: Sequence[Instance],
    *,
    replace: bool,
    out: Callable[[str], None],
    now: float | None = None,
    decide: Callable[[str], bool] | None = None,
) -> int:
    """Writes each instance's config. An existing file is kept unless
    `replace` (or `decide(path)` says so); a replaced file is copied to
    `<path>.bak-<timestamp>` first. Returns the number of files written."""
    now = time.time() if now is None else now
    written = 0
    for inst in instances:
        path = inst.config_path
        exists = os.path.exists(_real(path))
        if exists:
            if not (replace or (decide is not None and decide(path))):
                out(f"Kept existing {path} (not replaced).")
                continue
            out(f"Backed up {path} to {_backup_existing(path, now)}")
        _write_atomic(path, render_toml(inst))
        out(f"Wrote {path} (mode 0600).")
        written += 1
    return written


# ---------------------------------------------------------------- interactive


def _ask(
    inp: Callable[[str], str],
    prompt: str,
    default: str = "",
    valid: Callable[[str], str | None] | None = None,
    out: Callable[[str], None] = print,
) -> str:
    suffix = f" [{default}]" if default else ""
    for _ in range(10):
        try:
            answer = inp(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise Cancelled from None
        answer = answer or default
        err = valid(answer) if valid else None
        if err is None:
            return answer
        out(f"  {err}")
    raise Cancelled


def _yes(inp: Callable[[str], str], prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        answer = inp(f"{prompt} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise Cancelled from None
    if not answer:
        return default
    return answer in ("y", "yes")


def _port_ok(text: str) -> str | None:
    if text.isdigit() and 1 <= int(text) <= 65535:
        return None
    return "enter a port from 1 to 65535"


def _ask_instance(
    inp: Callable[[str], str],
    out: Callable[[str], None],
    transport: str,
    roots: Sequence[str],
    defaults: Instance | None,
    ordinal: int,
) -> Instance:
    sfx = _SUFFIX[transport]
    if defaults is None:
        config_d, db_d, bk_d = (
            "/etc/meshgarden/config.toml",
            "/var/lib/meshgarden/observatory.db",
            "/var/lib/meshgarden/backups",
        )
        metrics_d, web_d = "", ""
    else:
        cdir = os.path.dirname(defaults.config_path)
        ddir = os.path.dirname(defaults.database)
        config_d = os.path.join(cdir, f"{sfx}.toml")
        db_d = os.path.join(ddir, sfx, "observatory.db")
        bk_d = os.path.join(os.path.dirname(defaults.backup_dir), f"backups-{sfx}")
        metrics_d, web_d = "9118", ""

    out(f"\n== Instance {ordinal}: {transport} ==")
    candidates = discover_serial(roots)
    if candidates:
        out("Serial devices found (not opened):")
        for i, c in enumerate(candidates, 1):
            out(f"  {i}. {c}")
    else:
        out("No serial devices found; type the device path.")
    raw = _ask(
        inp,
        "Radio device (number or path)",
        candidates[0] if candidates else "",
        lambda t: None if t else "a device path is required",
        out,
    )
    device = candidates[int(raw) - 1] if raw.isdigit() and 0 < int(raw) <= len(candidates) else raw
    node_id = _ask(
        inp,
        "Expected node id (!xxxxxxxx; placeholder if unknown, edit before running)",
        _PLACEHOLDER_NODE_ID,
        None,
        out,
    )
    config_path = _abs(_ask(inp, "Config file path", config_d, None, out))
    database = _abs(_ask(inp, "Database path", db_d, None, out))
    backup = _abs(_ask(inp, "Backup directory", bk_d, None, out))
    mp = _ask(
        inp,
        "Metrics port (blank = daemon default 9108)",
        metrics_d,
        lambda t: None if not t else _port_ok(t),
        out,
    )
    wp = _ask(
        inp,
        "Dashboard port (blank = dashboard off)",
        web_d,
        lambda t: None if not t else _port_ok(t),
        out,
    )
    return Instance(
        transport=transport,
        device=device,
        config_path=config_path,
        database=database,
        backup_dir=backup,
        expected_node_id=node_id,
        metrics_port=int(mp) if mp else None,
        web_port=int(wp) if wp else None,
    )


def run_interactive(
    inp: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    roots: Sequence[str] = SERIAL_GLOBS,
    now: float | None = None,
) -> int:
    """Exit codes: 0 done/cancelled-without-changes, 2 invalid/collision."""
    try:
        out("Meshgarden setup. Nothing is written until you confirm; no radio is contacted.")
        transport = _ask(
            inp,
            "Transport (meshcore|meshtastic)",
            "meshcore",
            lambda t: None if t in TRANSPORTS else "choose meshcore or meshtastic",
            out,
        )
        first = _ask_instance(inp, out, transport, roots, None, 1)
        instances = [first]
        other = "meshtastic" if transport == "meshcore" else "meshcore"
        if _yes(inp, f"\nAdd a second instance ({other}) sharing one BBS?"):
            second = _ask_instance(inp, out, other, roots, first, 2)
            shared = _abs(
                _ask(
                    inp,
                    "Shared bbs.db path",
                    os.path.join(os.path.dirname(first.database), "bbs.db"),
                    None,
                    out,
                )
            )
            first.bbs_path = second.bbs_path = shared
            first.peer_path, second.peer_path = second.database, first.database
            instances.append(second)
        elif _yes(inp, "Use a shared bbs.db for this instance anyway?"):
            first.bbs_path = _abs(
                _ask(
                    inp,
                    "bbs.db path",
                    os.path.join(os.path.dirname(first.database), "bbs.db"),
                    None,
                    out,
                )
            )

        problems = check_collisions(instances)
        for inst in instances:
            err = validate_toml(render_toml(inst), inst.config_path)
            if err:
                problems.append(err)
        if problems:
            out("\nCannot continue; nothing was written:")
            for p in problems:
                out(f"  - {p}")
            return 2

        for inst in instances:
            out(f"\n--- {inst.config_path} ---")
            out(render_toml(inst).rstrip("\n"))
        if not _yes(inp, "\nWrite these files?"):
            raise Cancelled

        def decide(path: str) -> bool:
            return _yes(inp, f"{path} exists. Replace it (a .bak-<timestamp> copy is kept)?")

        apply(instances, replace=False, out=out, now=now, decide=decide)
        out("\nDone. Services were not started; review the files, then start the daemon yourself.")
        return 0
    except Cancelled:
        out("\nCancelled; nothing was changed.")
        return 0


# ------------------------------------------------------------- non-interactive


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--non-interactive", action="store_true", help="no prompts; use flags")
    parser.add_argument("--transport", choices=TRANSPORTS, help="radio transport")
    parser.add_argument("--device", help="radio serial device path")
    parser.add_argument("--expected-node-id", default=_PLACEHOLDER_NODE_ID, metavar="!ID")
    parser.add_argument("--output", metavar="PATH", help="config file to write")
    parser.add_argument("--database", metavar="PATH", help="observatory database path")
    parser.add_argument("--backup-dir", metavar="PATH", help="backup directory")
    parser.add_argument("--metrics-port", type=int, metavar="N")
    parser.add_argument("--web-port", type=int, metavar="N", help="enable the dashboard on N")
    parser.add_argument("--bbs-path", metavar="PATH", help="shared bbs.db path")
    parser.add_argument(
        "--replace", action="store_true", help="replace an existing config (backed up)"
    )
    parser.add_argument("--dry-run", action="store_true", help="print the preview only")


def run_noninteractive(args: argparse.Namespace, out: Callable[[str], None] = print) -> int:
    required = {
        "--transport": args.transport,
        "--device": args.device,
        "--output": args.output,
        "--database": args.database,
        "--backup-dir": args.backup_dir,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"error: --non-interactive requires {', '.join(missing)}", file=sys.stderr)
        return 2
    for flag in ("metrics_port", "web_port"):
        v = getattr(args, flag)
        if v is not None and _port_ok(str(v)):
            print(f"error: --{flag.replace('_', '-')} must be 1-65535", file=sys.stderr)
            return 2
    inst = Instance(
        transport=args.transport,
        device=args.device,
        config_path=_abs(args.output),
        database=_abs(args.database),
        backup_dir=_abs(args.backup_dir),
        expected_node_id=args.expected_node_id,
        metrics_port=args.metrics_port,
        web_port=args.web_port,
        bbs_path=_abs(args.bbs_path) if args.bbs_path else "",
    )
    problems = check_collisions([inst])
    text = render_toml(inst)
    err = validate_toml(text, inst.config_path)
    if err:
        problems.append(err)
    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 2
    out(text.rstrip("\n"))
    if args.dry_run:
        out("(dry run: nothing written)")
        return 0
    if os.path.exists(_real(inst.config_path)) and not args.replace:
        print(
            f"error: {inst.config_path} exists; kept unchanged. Use --replace to overwrite "
            "(a .bak-<timestamp> copy is made first).",
            file=sys.stderr,
        )
        return 1
    apply([inst], replace=args.replace, out=out)
    return 0


def run(args: argparse.Namespace) -> int:
    if args.non_interactive:
        return run_noninteractive(args)
    return run_interactive()
