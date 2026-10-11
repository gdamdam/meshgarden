import pathlib, tomllib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_config_example_parses_and_validates_after_placeholder_substitution():
    from meshgarden.config import parse_config
    raw = (ROOT / "config.example.toml").read_text()
    raw = raw.replace("REPLACE_WITH_YOUR_RADIO", "usb-0001")
    raw = raw.replace("!REPLACE_WITH_NODE_ID", "!11223344")
    cfg = parse_config(tomllib.loads(raw))
    assert cfg.service.mode == "dm_only"
    assert cfg.service.want_ack is True
    assert cfg.service.application_retries == 3
    assert cfg.service.application_retry_delay_seconds == 15
    assert cfg.service.name == "Meshgarden Guestbook"
    assert cfg.metrics.listen == "127.0.0.1"


def test_config_example_is_rejected_unmodified():
    from meshgarden.config import parse_config, ConfigError
    raw = (ROOT / "config.example.toml").read_text()
    with pytest.raises(ConfigError):
        parse_config(tomllib.loads(raw))     # the placeholder node id must fail


def test_readme_contains_the_required_disclaimer():
    text = (ROOT / "README.md").read_text()
    assert "Experimental community service." in text
    assert "Not an emergency communication service. No uptime guarantee." in text


def test_readme_documents_the_v1_exclusions_and_the_invariant():
    text = (ROOT / "README.md").read_text()
    # Channel mode is an explicit, bounded exception; the invariant is that
    # the response stays on the request's interaction surface.
    assert "Every reply goes back the way the request came." in text
    assert "Channel features stay public, read-only, and bounded." in text
    for word in ["MQTT bridging", "federation", "web frontend"]:
        assert word in text


SYSTEMD_UNITS = ["meshtastic-observatory.service", "meshgarden.service"]


@pytest.mark.parametrize("unit_name", SYSTEMD_UNITS)
def test_systemd_unit_is_unprivileged_and_restarts(unit_name):
    text = (ROOT / "systemd" / unit_name).read_text()
    assert "User=" in text and "User=root" not in text
    assert "Restart=on-failure" in text
    assert "NoNewPrivileges=true" in text


@pytest.mark.parametrize("unit_name", SYSTEMD_UNITS)
def test_systemd_unit_state_directory_is_owner_only_not_group_dialout(unit_name):
    """Finding 7: Group= sets the process's PRIMARY group, so a service
    file with Group=dialout makes every file the daemon creates
    group-dialout -- readable by anyone else with serial access. The unit
    must use its own private group, granting dialout only as a
    supplementary group, and lock the state directory down to owner
    (optionally also group) access -- never dialout or world."""
    text = (ROOT / "systemd" / unit_name).read_text()
    assert "Group=dialout" not in text
    assert "SupplementaryGroups=dialout" in text
    mode = re.search(r"^StateDirectoryMode=(\d+)$", text, re.MULTILINE)
    assert mode is not None
    assert mode.group(1) in ("0700", "0750")


def test_license_exists():
    assert (ROOT / "LICENSE").read_text().strip()


def test_pyproject_version_matches_the_package_version():
    """The operator-facing "1.0.0-pre-002" release name's PEP 440 form:
    pyproject.toml's [project].version and meshgarden.__version__
    must always agree -- one source of truth, checked both ways."""
    from meshgarden import __version__

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__
    assert __version__ == "1.10.0"


def test_readme_version_badge_matches_the_package_version():
    """A version bump that forgets to update the README's static shields.io
    badge should fail here rather than silently drifting from
    meshgarden.__version__. PEP 440 strings like "1.0.0rc2"
    contain no characters shields.io needs escaped in a /badge/ URL
    (no "-", "_", or spaces), so this compares the raw version directly."""
    from meshgarden import __version__

    text = (ROOT / "README.md").read_text()
    assert f"version-{__version__}-" in text


@pytest.mark.parametrize("unit_name", SYSTEMD_UNITS)
def test_systemd_unit_never_uses_the_debug_flag(unit_name):
    """`--debug` (run's opt-in message-content trace to stderr) must never
    be baked into the unattended/systemd ExecStart line."""
    text = (ROOT / "systemd" / unit_name).read_text()
    exec_start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert "--debug" not in exec_start


def test_readme_documents_the_debug_flag_and_its_privacy_caveat():
    text = (ROOT / "README.md").read_text()
    assert "--debug" in text
    assert "unattended" in text.lower() or "never use" in text.lower()


_MESSAGE_CONTENT_RE = re.compile(
    r"log(ger)?\.\w+\(.*\b(text|body|message|payload|argument)\b"
)

# Named, narrow exemption: `run --debug`'s exchange trace (see
# meshgarden/exchange_log.py) is the ONLY call site in the
# package allowed to carry message content into a logger, and only through
# this exact call shape -- `exchange_log.log_line(...)` -- gated by
# --debug and writing to stderr only, never the rotating file log, the
# database, metrics, or the status file. Nothing else is exempted: a
# plain `log`/`logger` call, or any other name that merely mentions
# "exchange", still trips the guard above.
_EXCHANGE_LOG_EXEMPTION_RE = re.compile(r"\bexchange_log\.log_line\(")


def _message_content_violations(lines: list[str]) -> list[str]:
    bad = []
    for line in lines:
        if _EXCHANGE_LOG_EXEMPTION_RE.search(line):
            continue
        if _MESSAGE_CONTENT_RE.search(line):
            bad.append(line.strip())
    return bad


def test_no_source_file_logs_message_content():
    """Guard: no f-string or %-format of a text/body/message variable in a log call.

    The exchange logger (`exchange_log.log_line`) is exempted -- see
    `_EXCHANGE_LOG_EXEMPTION_RE` -- since it exists specifically to print
    message content, gated behind operator opt-in `run --debug`, to stderr
    only.
    """
    bad = []
    for p in (ROOT / "meshgarden").glob("*.py"):
        for line in _message_content_violations(p.read_text().splitlines()):
            bad.append(f"{p.name}: {line}")
    assert bad == []


def test_message_content_guard_exempts_only_the_exchange_logger():
    """Negative self-check: the exemption must be narrow. If any of these
    ever stops behaving as asserted, the exemption itself has regressed --
    either swallowing violations it shouldn't, or no longer accepting the
    one call shape it exists for."""

    # The one shape actually used in the package: exempted.
    assert _message_content_violations(
        ['exchange_log.log_line(f"reply to={r} text={body!r}")']
    ) == []

    # A plain logger call with the same content is NOT exempted.
    assert _message_content_violations(['log.info(f"text={body!r}")']) != []
    assert _message_content_violations(['logger.info(f"message={m!r}")']) != []

    # A name that merely CONTAINS "exchange" (not the exact exempted call)
    # must still be flagged -- the exemption is not a substring match.
    assert _message_content_violations(
        ['exchange_rate_log.info(f"text={body!r}")']
    ) != []
    assert _message_content_violations(
        ['my_exchange_log.info(f"text={body!r}")']
    ) != []
    assert _message_content_violations(
        ['exchange_log.info(f"text={body!r}")']  # right object, wrong method
    ) != []


def test_readme_has_the_required_title():
    text = (ROOT / "README.md").read_text()
    assert text.startswith('<h1 align="center">🌱 Meshgarden 🌱</h1>\n')
    assert "MeshCore-first, Meshtastic supported." in text


def test_readme_has_gpl_badge():
    text = (ROOT / "README.md").read_text()
    assert "License-GPL--3.0" in text


def test_hardware_fixtures_never_hold_the_service_device_open():
    """Regression guard (fix round 2): a module- or session-scoped fixture
    in tests/test_hardware.py that opens MESHGARDEN_TEST_DEVICE would hold
    that exclusive serial connection open across tests, making every later
    test's own connection attempt to the same device fail. The hardware
    suite itself can never catch this in CI (it never runs there, and it is
    deselected by default), so it is asserted statically here instead: no
    fixture with a wider-than-function scope may reference the service
    device.
    """
    import ast
    import re

    tree = ast.parse((ROOT / "tests" / "test_hardware.py").read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        is_fixture = False
        scope = "function"
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            if ast.unparse(target).rsplit(".", 1)[-1] != "fixture":
                continue
            is_fixture = True
            if isinstance(deco, ast.Call):
                for kw in deco.keywords:
                    if kw.arg == "scope" and isinstance(kw.value, ast.Constant):
                        scope = kw.value.value
        if not is_fixture or scope == "function":
            continue
        if re.search(r"\bDEVICE\b", ast.unparse(node)):
            offenders.append(node.name)
    assert offenders == [], f"module/session-scoped fixture(s) touch DEVICE: {offenders}"


def _hardware_dm_only_violations(source: str) -> list[str]:
    """Static DM-only guard for tests/test_hardware.py: every `sendText`/
    `sendData` call in the source must address a specific node -- never the
    broadcast destination, in any form the code could spell it -- and every
    `sendText` `text=` string must be a bare command or contain "test".

    Operates on parsed source text (not a live import) so it can be run
    against both the real module and small synthetic snippets in the
    negative self-check below. Flags, per call:

    (a) the call is `sendText` OR `sendData` (an earlier version of this
        guard only looked at `sendText`; `sendData` can address a node just
        as well and was an unchecked bypass);
    (b) `destinationId` resolves to a broadcast value -- the literal
        `"^all"`/`0xFFFFFFFF`, the names `BROADCAST_ADDR`/`BROADCAST_NUM`
        (meshtastic's own constants, however imported/aliased), or a
        module-level name in THIS source whose own simple
        `NAME = <constant>` assignment equals one of those values;
    (c) `destinationId` given positionally (it's the second positional arg
        to both `sendText` and `sendData`) is checked exactly like a
        keyword -- a second positional broadcast destination was another
        unchecked bypass;
    (d) `destinationId` omitted entirely -- meshtastic defaults this to
        broadcast, so omission is itself a violation;
    (e) the call passes `**kwargs` (or `*args`) -- nothing in it can be
        proven safe statically, so it is flagged outright regardless of
        what else is present.

    Returns a list of human-readable violation descriptions (empty means no
    violations found).
    """
    import ast

    tree = ast.parse(source)

    # Resolve simple module-level `NAME = <constant>` aliases of a broadcast
    # value, so a local rename doesn't slip past (b).
    module_constants: dict[str, object] = {}
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Constant)
        ):
            module_constants[stmt.targets[0].id] = stmt.value.value

    BROADCAST_VALUES = {"^all", 0xFFFFFFFF}
    BROADCAST_NAMES = {"BROADCAST_ADDR", "BROADCAST_NUM"}
    BARE_COMMANDS = {
        "menu", "?", "info", "guestbook", "stats", "iching", "haiku", "book", "wordle",
        "whoami", "signal", "garden", "record", "week", "unsign",
        # Two-level MENU sub-pages: still fixed, non-free-text literals, so
        # allowed alongside the bare commands above (free-text rule for
        # everything else is unchanged).
        "menu stats", "menu games",
        # Directory/mail commands (directory/boards/mail epic): "who" takes
        # no argument at all; "mail" (bare) and "mail on" are the two
        # argument-free/closed-set MAIL sub-forms -- none of these three is
        # free text, exactly like the bare commands above.
        "who", "mail", "mail on",
    }

    def _is_broadcast_expr(node: ast.AST) -> bool:
        if isinstance(node, ast.Constant) and node.value in BROADCAST_VALUES:
            return True
        if isinstance(node, ast.Name):
            if node.id in BROADCAST_NAMES:
                return True
            return module_constants.get(node.id) in BROADCAST_VALUES
        return isinstance(node, ast.Attribute) and node.attr in BROADCAST_NAMES

    def _names_in(node: ast.AST) -> set[str]:
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in ("sendText", "sendData")):
            continue
        label = f"{func.attr}() at line {node.lineno}"

        if any(kw.arg is None for kw in node.keywords):
            violations.append(
                f"{label}: uses a **kwargs splat, cannot verify destinationId/text statically"
            )
            continue
        if any(isinstance(a, ast.Starred) for a in node.args):
            violations.append(
                f"{label}: uses a *args splat, cannot verify destinationId/text statically"
            )
            continue

        dest_kw = next((kw for kw in node.keywords if kw.arg == "destinationId"), None)
        dest_positional = node.args[1] if len(node.args) > 1 else None
        dest_node = dest_kw.value if dest_kw is not None else dest_positional

        if dest_node is None:
            violations.append(f"{label}: destinationId omitted (meshtastic defaults to broadcast)")
        elif _is_broadcast_expr(dest_node):
            violations.append(
                f"{label}: destinationId is a broadcast destination ({ast.unparse(dest_node)})"
            )

        if func.attr != "sendText":
            continue

        text_kw = next((kw for kw in node.keywords if kw.arg == "text"), None)
        text_node = text_kw.value if text_kw is not None else (node.args[0] if node.args else None)
        if text_node is None:
            continue
        if isinstance(text_node, ast.Constant) and isinstance(text_node.value, str):
            text = text_node.value
            if text.strip().lower() not in BARE_COMMANDS and "test" not in text.lower():
                violations.append(
                    f"{label}: text literal {text!r} is neither a bare command nor contains 'test'"
                )
        else:
            if not any("test" in n.lower() for n in _names_in(text_node)):
                violations.append(
                    f"{label}: non-literal text= expression not built from a "
                    "variable named with 'test'"
                )

    return violations


def test_hardware_suite_is_dm_only_and_never_broadcasts():
    """Regression guard: tests/test_hardware.py must never broadcast (see
    `_hardware_dm_only_violations` for exactly what counts as a broadcast)
    and every sendText text= literal must be a bare command or contain
    "test". This guard replaces an earlier, narrower one that only checked
    sendText with a literal destinationId="^all" -- see
    test_dm_only_guard_catches_every_bypass_it_was_tightened_for for the
    bypasses that prompted the rewrite.
    """
    source = (ROOT / "tests" / "test_hardware.py").read_text()
    violations = _hardware_dm_only_violations(source)
    assert violations == [], "\n".join(violations)


def test_dm_only_guard_catches_every_bypass_it_was_tightened_for():
    """Negative self-check, one assertion per bypass the guard must catch --
    if any of these ever stops failing, the guard itself has regressed."""

    def violates(source: str) -> bool:
        return len(_hardware_dm_only_violations(source)) > 0

    # Original two cases: a non-test, non-command text literal, and a
    # bare-command text literal sent with no destinationId at all.
    assert violates('iface.sendText(text="hello")')
    assert violates('iface.sendText(text="MENU")')

    # (a) sendData must be scanned too, not just sendText.
    assert violates('iface.sendData(payload, destinationId="^all")')

    # (b) a module-level alias of a broadcast constant must be resolved,
    # not just the literal or the well-known meshtastic names.
    assert violates(
        'MY_DEST = "^all"\niface.sendText(text="MENU", destinationId=MY_DEST)'
    )
    assert violates('iface.sendText(text="MENU", destinationId=BROADCAST_ADDR)')

    # (c) a positional destinationId must be inspected, not just the keyword.
    assert violates('iface.sendText("MENU", "^all")')
    assert violates('iface.sendText("MENU", BROADCAST_ADDR)')

    # (d) an omitted destinationId defaults to broadcast in the library.
    assert violates('iface.sendText(text="INFO")')

    # (e) a **kwargs or *args splat cannot be proven safe statically.
    assert violates('iface.sendText(**kwargs)')
    assert violates('iface.sendText(*args)')

    # Sanity: a properly-addressed, test-worded call must NOT be flagged,
    # so the guard isn't just failing everything.
    assert not violates('iface.sendText(text="SIGN test hi", destinationId=service_id)')

    # (f) HELP is no longer a bare command a test may send: it now reads as
    # a distress word, so even a properly-addressed DM with a literal
    # "HELP" text (no "test" in it) must still be flagged. SOS/EMERGENCY
    # likewise never sent by tests -- unit tests with the fake radio cover
    # them instead.
    assert violates('iface.sendText(text="HELP", destinationId=service_id)')
    assert violates('iface.sendText(text="SOS", destinationId=service_id)')
    assert violates('iface.sendText(text="EMERGENCY", destinationId=service_id)')

    # Sanity: the new bare commands (MENU, ?) ARE allowed, properly addressed.
    assert not violates('iface.sendText(text="MENU", destinationId=service_id)')
    assert not violates('iface.sendText(text="?", destinationId=service_id)')

    # (g) BOOK is a bare-allowed command too, but BOOK <guess> is free text:
    # a guess literal must still contain "test" like any other free text.
    assert not violates('iface.sendText(text="BOOK", destinationId=service_id)')
    assert not violates('iface.sendText(text="book", destinationId=service_id)')
    assert not violates('iface.sendText(text="BOOK test", destinationId=service_id)')
    assert violates('iface.sendText(text="BOOK 1984", destinationId=service_id)')

    # (h) WORDLE is a bare-allowed command too, same free-text rule for its
    # guess argument.
    assert not violates('iface.sendText(text="WORDLE", destinationId=service_id)')
    assert not violates('iface.sendText(text="wordle", destinationId=service_id)')
    assert not violates('iface.sendText(text="WORDLE test", destinationId=service_id)')
    assert violates('iface.sendText(text="WORDLE crane", destinationId=service_id)')

    # (i) the two-level menu sub-page requests are allowed literals too, not
    # free text -- but a junk argument still is free text.
    assert not violates('iface.sendText(text="MENU STATS", destinationId=service_id)')
    assert not violates('iface.sendText(text="MENU GAMES", destinationId=service_id)')
    assert not violates('iface.sendText(text="menu games", destinationId=service_id)')
    assert violates('iface.sendText(text="MENU xyz", destinationId=service_id)')


def test_watchdog_units_exist_and_reference_the_real_command():
    svc = (ROOT / "systemd" / "meshgarden-watchdog.service").read_text()
    timer = (ROOT / "systemd" / "meshgarden-watchdog.timer").read_text()
    assert "radio-watchdog" in svc
    assert "--debug" not in svc
    assert "Type=oneshot" in svc
    assert "Unit=meshgarden-watchdog.service" in timer


def test_watchdog_env_file_is_optional_in_the_unit():
    """The leading "-" matters: with no env file the watchdog must still
    poll and still power-cycle, rather than the unit refusing to start."""
    svc = (ROOT / "systemd" / "meshgarden-watchdog.service").read_text()
    line = next(x for x in svc.splitlines() if x.startswith("EnvironmentFile="))
    assert line.startswith("EnvironmentFile=-")


def test_watchdog_timer_does_not_catch_up_missed_polls():
    """Persistent=true would fire a burst of catch-up polls at boot, which
    could power-cycle the radio before the daemon has connected."""
    timer = (ROOT / "systemd" / "meshgarden-watchdog.timer").read_text()
    directives = [
        x.strip() for x in timer.splitlines() if x.strip() and not x.strip().startswith("#")
    ]
    assert not any(d.lower().startswith("persistent=true") for d in directives)


def test_the_example_env_file_carries_no_real_ntfy_topic():
    """Guard against ever committing a working alert URL."""
    text = (ROOT / "systemd" / "watchdog.env.example").read_text()
    url_line = next(x for x in text.splitlines() if x.startswith("MESHGARDEN_NTFY_URL="))
    assert "CHANGE-ME" in url_line


def test_no_real_ntfy_url_is_committed_anywhere():
    """A topic URL in any tracked file would be a leaked shared secret.
    The .example placeholder is the only allowed occurrence."""
    offenders = []
    for path in list(ROOT.glob("*.md")) + list((ROOT / "docs").glob("*.md")) + \
            list((ROOT / "meshgarden").glob("*.py")) + list((ROOT / "systemd").glob("*")):
        text = path.read_text(errors="replace")
        for line in text.splitlines():
            if "ntfy.sh/" not in line:
                continue
            if "CHANGE-ME" in line or "<" in line or "TOPIC" in line.upper():
                continue
            offenders.append(f"{path.name}: {line.strip()}")
    assert offenders == [], f"possible real ntfy URL committed: {offenders}"


def test_watchdog_unit_can_still_reach_a_checkout_under_home():
    """Regression: ProtectHome=yes hid /home, so the unit could not exec the
    venv that install-rpi.sh runs in place from the operator's home
    directory -- it failed with status=127 "No such file or directory".
    read-only keeps the checkout visible, as meshgarden.service does."""
    svc = (ROOT / "systemd" / "meshgarden-watchdog.service").read_text()
    directives = [
        x.strip() for x in svc.splitlines() if x.strip() and not x.strip().startswith("#")
    ]
    protect_home = [d for d in directives if d.lower().startswith("protecthome=")]
    assert protect_home == ["ProtectHome=read-only"], protect_home


def test_both_shipped_units_agree_on_protect_home():
    """If meshgarden.service ever needs a different value, the watchdog that
    execs the same binary needs it too."""
    main_svc = (ROOT / "systemd" / "meshgarden.service").read_text()
    wd_svc = (ROOT / "systemd" / "meshgarden-watchdog.service").read_text()

    def value(text):
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("protecthome="):
                return line.split("=", 1)[1]
        return None

    assert value(main_svc) == value(wd_svc)
