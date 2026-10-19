"""Default-suite (no hardware, no root) tests for the Raspberry Pi
install/uninstall scripts and the meshgarden.service unit template.

Everything here is read-only or --dry-run: nothing installs a system user,
touches /etc or /var/lib, or requires actual root. MESHGARDEN_INSTALL_FAKE_ROOT=1
is a test-only escape hatch documented in both scripts' --help output.
"""

import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_SCRIPT = os.path.join(ROOT, "scripts", "install-rpi.sh")
UNINSTALL_SCRIPT = os.path.join(ROOT, "scripts", "uninstall-rpi.sh")
UNIT_TEMPLATE = os.path.join(ROOT, "systemd", "meshgarden.service")
LEGACY_UNIT = os.path.join(ROOT, "systemd", "meshtastic-observatory.service")

INSTALL_OPTIONS = [
    "--user",
    "--device",
    "--transport",
    "--config",
    "--web-listen",
    "--install-dir",
    "--no-start",
    "--no-enable",
    "--skip-apt",
    "--skip-uv-install",
    "--force-config",
    "--no-config-sync",
    "--fix-home-traversal",
    "--reset-data",
    "--yes",
    "--debug",
    "--dry-run",
    "--verbose",
    "--help",
]

UNINSTALL_OPTIONS = [
    "--purge",
    "--remove-user",
    "--yes",
    "--dry-run",
    "--verbose",
    "--help",
]


# =============================================================================
# bash -n
# =============================================================================


def test_install_script_is_valid_bash():
    result = subprocess.run(
        ["bash", "-n", INSTALL_SCRIPT], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_uninstall_script_is_valid_bash():
    result = subprocess.run(
        ["bash", "-n", UNINSTALL_SCRIPT], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_install_script_is_executable():
    assert os.access(INSTALL_SCRIPT, os.X_OK)


def test_uninstall_script_is_executable():
    assert os.access(UNINSTALL_SCRIPT, os.X_OK)


# =============================================================================
# --help
# =============================================================================


def test_install_help_exits_zero_and_lists_every_option():
    result = subprocess.run(
        [INSTALL_SCRIPT, "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    for opt in INSTALL_OPTIONS:
        assert opt in result.stdout, f"{opt!r} missing from --help output"


def test_uninstall_help_exits_zero_and_lists_every_option():
    result = subprocess.run(
        [UNINSTALL_SCRIPT, "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    for opt in UNINSTALL_OPTIONS:
        assert opt in result.stdout, f"{opt!r} missing from --help output"


def test_install_unknown_option_fails():
    result = subprocess.run(
        [INSTALL_SCRIPT, "--not-a-real-option"], capture_output=True, text=True
    )
    assert result.returncode != 0


def test_install_help_documents_traversal_preflight_and_remedies():
    """The service-user traversal pre-flight (runuser -u meshgarden --
    test -x <checkout>/.venv/bin/mgarden) only fails loudly at real-install
    time (it needs the "meshgarden" user and an actual venv, neither of
    which exist in --dry-run/test runs), so its actionable remedies must
    be documented in --help instead."""
    result = subprocess.run(
        [INSTALL_SCRIPT, "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "chmod o+x" in result.stdout
    assert "/opt/meshgarden" in result.stdout


def test_install_dry_run_queues_traversal_preflight_command():
    result = subprocess.run(
        [
            INSTALL_SCRIPT,
            "--dry-run",
            "--skip-apt",
            "--skip-uv-install",
            "--no-start",
            "--no-enable",
        ],
        capture_output=True,
        text=True,
        env=_dry_run_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "would run: runuser -u meshgarden -- test -x" in result.stdout
    assert ".venv/bin/mgarden" in result.stdout


# =============================================================================
# --dry-run (no root required, via the documented test-only env override)
# =============================================================================


def _dry_run_env():
    env = dict(os.environ)
    env["MESHGARDEN_INSTALL_FAKE_ROOT"] = "1"
    env.setdefault("SUDO_USER", os.environ.get("USER", "nobody"))
    # This checkout's .venv is frequently a symlink shared across git
    # worktrees (its editable install then points at whichever checkout
    # `uv sync` was last run from, not necessarily this one). Any
    # dry-run test that has install-rpi.sh actually invoke
    # `$REPO_DIR/.venv/bin/mgarden` (config-sync's own --dry-run makes
    # that safe to do for real, unlike the OS-mutating commands the
    # `run()` wrapper stubs out) needs PYTHONPATH pointed at ROOT so that
    # subprocess resolves `meshgarden` from this checkout,
    # not wherever the shared venv happens to be installed from.
    env["PYTHONPATH"] = ROOT
    return env


def test_install_dry_run_creates_no_files_and_prints_a_command_plan():
    wrapper_existed_before = {
        name: os.path.exists(f"/usr/local/bin/{name}")
        for name in ("mgarden", "meshtastic-observatory")
    }
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.toml")
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                config_path,
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        out = result.stdout

        # No real changes: the config was only ever staged in a scratch
        # temp file, never actually installed at --config's path.
        assert not os.path.exists(config_path)

        # A representative slice of the mutating commands the plan should
        # include, all prefixed by the dry-run marker rather than executed.
        assert "[dry-run] would run:" in out
        assert "useradd" in out
        assert "usermod" in out
        assert "install -o root -g meshgarden" in out
        assert "install -o root -g root -m 0644" in out
        assert "systemctl daemon-reload" in out

        # System-wide CLI wrappers: the dry-run plan shows both installs,
        # each with an absolute destination path, rather than actually
        # writing to /usr/local/bin.
        assert "install -o root -g root -m 0755" in out
        assert "/usr/local/bin/mgarden" in out
        assert "/usr/local/bin/meshtastic-observatory" in out
        for name, existed_before in wrapper_existed_before.items():
            assert os.path.exists(f"/usr/local/bin/{name}") == existed_before

        # --no-start/--no-enable are honored: neither is actually queued as
        # a "would run" command (the skip notices themselves legitimately
        # mention the command names, so check the dry-run plan lines only).
        assert "would run: systemctl enable" not in out
        assert "would run: systemctl start" not in out
        assert "would run: systemctl restart" not in out
        assert "skipping systemctl enable (--no-enable)" in out
        assert "skipping start/restart (--no-start)" in out

        # System-level directories are never created either.
        assert not os.path.exists("/etc/meshgarden")
        assert not os.path.exists("/var/lib/meshgarden")


def test_install_dry_run_without_fake_root_env_still_requires_root():
    """The FAKE_ROOT override is opt-in: without it, --dry-run still
    refuses to run unless actually root (defense against accidentally
    treating a plain `./scripts/install-rpi.sh --dry-run` as safe when the
    override isn't set)."""
    if os.geteuid() == 0:
        pytest.skip("test invoked as root; the check is a no-op here")
    env = dict(os.environ)
    env.pop("MESHGARDEN_INSTALL_FAKE_ROOT", None)
    result = subprocess.run(
        [INSTALL_SCRIPT, "--dry-run", "--skip-apt", "--skip-uv-install"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0
    assert "root" in (result.stdout + result.stderr).lower()


def test_install_dry_run_respects_existing_config():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.toml")
        with open(config_path, "w") as f:
            f.write("# pre-existing config\n")
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                config_path,
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "leaving it untouched" in result.stdout
        with open(config_path) as f:
            assert f.read() == "# pre-existing config\n"


def test_install_dry_run_force_config_backs_up_first():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.toml")
        with open(config_path, "w") as f:
            f.write("# pre-existing config\n")
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                config_path,
                "--force-config",
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "backing up existing config" in result.stdout
        assert "would run: cp -a" in result.stdout
        # Still --dry-run: no backup file actually created, original intact.
        assert os.listdir(tmp) == ["config.toml"]
        with open(config_path) as f:
            assert f.read() == "# pre-existing config\n"


def test_install_dry_run_web_listen_prints_url_when_config_is_new():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.toml")
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                config_path,
                "--web-listen",
                "0.0.0.0",
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "dashboard: http://<pi-ip>:9109/" in result.stdout


def test_install_dry_run_web_listen_omits_url_when_config_not_rewritten():
    """Without --force-config, an existing config.toml is left untouched,
    so [web] was never written -- only the manual-edit notice should
    appear, never a dashboard URL implying the dashboard is live."""
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.toml")
        with open(config_path, "w") as f:
            f.write("# pre-existing config\n")
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                config_path,
                "--web-listen",
                "0.0.0.0",
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "manually add/update these lines" in result.stderr
        assert "dashboard: http" not in result.stdout


# =============================================================================
# config-sync (Task 16): re-running against an existing config adds keys
# missing from it, with their defaults, and never touches an existing value.
# =============================================================================

_CONFIG_MISSING_SEVEN_KEYS = """\
[radio]
device = "/dev/fake"
expected_node_id = "!11223344"
[database]
path = "{db}"
[backup]
directory = "{bk}"
[service]
name = "Pre-epic Node"
mode = "dm_only"
daily_sign_quota = 3
"""


def test_install_dry_run_existing_config_runs_config_sync_and_never_rewrites_it():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.toml")
        before = _CONFIG_MISSING_SEVEN_KEYS.format(
            db=os.path.join(tmp, "o.db"), bk=os.path.join(tmp, "backups")
        )
        with open(config_path, "w") as f:
            f.write(before)
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                config_path,
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "leaving it untouched" in result.stdout
        # The config-sync --dry-run call's own output (one line per missing
        # key) is logged -- proof the plan actually ran it, not just a
        # generic notice.
        assert "service.seen_enabled" in result.stdout
        assert "retention.mail_expiry_days" in result.stdout
        # Never the config-install rewrite used for a brand-new/--force-config config.
        install_lines = [
            line for line in result.stdout.splitlines() if "install -o root -g meshgarden" in line
        ]
        assert not any(config_path in line for line in install_lines)
        # Still --dry-run end to end: the original file is completely unchanged.
        with open(config_path) as f:
            assert f.read() == before
        assert sorted(os.listdir(tmp)) == ["config.toml"]


def test_install_dry_run_no_config_sync_skips_it():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.toml")
        before = _CONFIG_MISSING_SEVEN_KEYS.format(
            db=os.path.join(tmp, "o.db"), bk=os.path.join(tmp, "backups")
        )
        with open(config_path, "w") as f:
            f.write(before)
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                config_path,
                "--no-config-sync",
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "leaving it untouched" in result.stdout
        assert "skipping config-sync" in result.stdout
        assert "service.seen_enabled" not in result.stdout
        with open(config_path) as f:
            assert f.read() == before


def test_install_dry_run_force_config_does_not_run_config_sync():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.toml")
        before = _CONFIG_MISSING_SEVEN_KEYS.format(
            db=os.path.join(tmp, "o.db"), bk=os.path.join(tmp, "backups")
        )
        with open(config_path, "w") as f:
            f.write(before)
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                config_path,
                "--force-config",
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "backing up existing config" in result.stdout
        assert "service.seen_enabled" not in result.stdout
        assert "mgarden config-sync" not in result.stdout
        assert "running config-sync" not in result.stdout


def test_install_help_documents_no_config_sync():
    result = subprocess.run([INSTALL_SCRIPT, "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "--no-config-sync" in result.stdout


def test_install_web_listen_rejects_bogus_address():
    result = subprocess.run(
        [INSTALL_SCRIPT, "--dry-run", "--skip-apt", "--skip-uv-install", "--web-listen", "foo:bar"],
        capture_output=True,
        text=True,
        env=_dry_run_env(),
    )
    assert result.returncode != 0
    assert "foo:bar" in (result.stdout + result.stderr)


# =============================================================================
# --transport (installs the optional `meshcore` extra via `uv sync`)
# =============================================================================


def test_install_transport_rejects_a_bogus_value():
    result = subprocess.run(
        [INSTALL_SCRIPT, "--dry-run", "--skip-apt", "--skip-uv-install", "--transport", "bogus"],
        capture_output=True,
        text=True,
        env=_dry_run_env(),
    )
    assert result.returncode != 0
    assert "bogus" in (result.stdout + result.stderr)


def test_install_dry_run_default_transport_never_requests_the_meshcore_extra():
    """No `--transport` and no pre-existing config: a Meshtastic-only
    install (today's default) must see no behavior change -- `uv sync`
    runs exactly as before, without `--extra meshcore`."""
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                os.path.join(tmp, "config.toml"),
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "sync --frozen --no-dev in" in result.stdout
        assert "extra meshcore" not in result.stdout


def test_install_dry_run_transport_meshcore_requests_the_extra():
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                os.path.join(tmp, "config.toml"),
                "--transport",
                "meshcore",
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "sync --frozen --no-dev --extra meshcore in" in result.stdout
        assert "MeshCore transport selected" in result.stdout


def test_install_dry_run_detects_meshcore_transport_from_an_existing_config():
    """A re-run (e.g. after `git pull`) against an already-deployed
    MeshCore install must keep installing the extra even without
    `--transport` being repeated -- otherwise `uv sync` would drop it on
    the next upgrade."""
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.toml")
        with open(config_path, "w") as f:
            f.write('[radio]\ntransport = "meshcore"\n')
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                config_path,
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "detected transport" in result.stdout
        assert "sync --frozen --no-dev --extra meshcore in" in result.stdout


def test_install_dry_run_instance_keeps_meshcore_extra_for_default_config():
    """`--instance mt --transport meshtastic` must not strip the meshcore
    extra the default MeshCore daemon runs from."""
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "config.toml"), "w") as f:
            f.write('[radio]\ntransport = "meshcore"\n')
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--instance",
                "mt",
                "--transport",
                "meshtastic",
                "--config",
                os.path.join(tmp, "mt.toml"),
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "sync --frozen --no-dev --extra meshcore in" in result.stdout


def test_install_dry_run_existing_meshtastic_config_never_requests_the_extra():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.toml")
        with open(config_path, "w") as f:
            f.write('[radio]\ntransport = "meshtastic"\n')
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                config_path,
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "extra meshcore" not in result.stdout


def test_install_help_documents_transport():
    result = subprocess.run([INSTALL_SCRIPT, "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "--transport" in result.stdout
    assert "meshcore" in result.stdout


# =============================================================================
# --reset-data
# =============================================================================


def test_install_help_documents_reset_data_wording():
    result = subprocess.run([INSTALL_SCRIPT, "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "config is never touched; backups/ is kept" in result.stdout.lower()


def test_install_dry_run_reset_data_plans_db_removal_and_preserves_backups_and_config():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.toml")
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                config_path,
                "--reset-data",
                "--yes",
            ],
            capture_output=True,
            text=True,
            env=_dry_run_env(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        out = result.stdout

        rm_lines = [line for line in out.splitlines() if "would run: rm" in line]
        assert any(
            "/var/lib/meshgarden/observatory.db" in line
            and "/var/lib/meshgarden/observatory.db-wal" in line
            and "/var/lib/meshgarden/observatory.db-shm" in line
            for line in rm_lines
        ), out

        # backups/ and the config path are never planned for removal.
        assert not any("backups" in line for line in rm_lines)
        assert not any(config_path in line for line in rm_lines)
        assert not any("/etc/meshgarden" in line for line in rm_lines)

        # No systemctl binary on this dev machine and DRY_RUN short-circuits
        # the is-active check (same guard style as the rest of the script),
        # so no stop is ever queued here.
        assert "would run: systemctl stop" not in out


def test_install_dry_run_reset_data_removes_state_dir_contents_except_backups():
    """Exercises the --reset-data removal loop against a real (throwaway)
    state directory via MESHGARDEN_INSTALL_STATE_DIR, honored by the script
    only together with MESHGARDEN_INSTALL_FAKE_ROOT=1 (never for a real
    root run). Without this override STATE_DIR is hardcoded to
    /var/lib/meshgarden, which doesn't exist on this dev/CI machine, so the
    removal loop's `[ -d "$STATE_DIR" ]` guard is always false and the loop
    body never runs -- this test is what actually proves the loop deletes
    the right things, including a dotfile (no `dotglob` needed: the script
    uses `find -mindepth 1 -maxdepth 1`, which lists dotfiles too)."""
    with tempfile.TemporaryDirectory() as state_dir:
        for name in ("observatory.db", "observatory.db-wal", "observatory.db-shm"):
            with open(os.path.join(state_dir, name), "w") as f:
                f.write("x")
        os.makedirs(os.path.join(state_dir, "digest"))
        with open(os.path.join(state_dir, "digest", "2026-09-14.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(state_dir, ".hidden-state"), "w") as f:
            f.write("x")
        with open(os.path.join(state_dir, "status.json"), "w") as f:
            f.write("{}")
        backups_dir = os.path.join(state_dir, "backups")
        os.makedirs(backups_dir)
        with open(os.path.join(backups_dir, "observatory-2026.db"), "w") as f:
            f.write("backup")

        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.toml")
            env = _dry_run_env()
            env["MESHGARDEN_INSTALL_STATE_DIR"] = state_dir
            result = subprocess.run(
                [
                    INSTALL_SCRIPT,
                    "--dry-run",
                    "--skip-apt",
                    "--skip-uv-install",
                    "--no-start",
                    "--no-enable",
                    "--config",
                    config_path,
                    "--reset-data",
                    "--yes",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
        assert result.returncode == 0, result.stdout + result.stderr
        out = result.stdout

        rm_lines = [line for line in out.splitlines() if "would run: rm" in line]

        # The dedicated db/wal/shm rm plans all three, unconditionally.
        assert any(
            f"{state_dir}/observatory.db" in line
            and f"{state_dir}/observatory.db-wal" in line
            and f"{state_dir}/observatory.db-shm" in line
            for line in rm_lines
        ), out

        # The loop over the rest of the directory plans exactly: digest/,
        # .hidden-state, and status.json -- each its own "rm -rf" line.
        looped_targets = {
            line.split("would run: rm -rf ", 1)[1]
            for line in rm_lines
            if "would run: rm -rf " in line
        }
        assert looped_targets == {
            os.path.join(state_dir, "digest"),
            os.path.join(state_dir, ".hidden-state"),
            os.path.join(state_dir, "status.json"),
        }, out

        # backups/ (and anything inside it), the config path, and anything
        # under /etc are never planned for removal.
        assert not any("backups" in line for line in rm_lines)
        assert not any(config_path in line for line in rm_lines)
        assert not any("/etc" in line for line in rm_lines)

        # Still --dry-run: nothing on disk was actually touched.
        assert os.path.exists(os.path.join(state_dir, "observatory.db"))
        assert os.path.exists(os.path.join(state_dir, ".hidden-state"))
        assert os.path.exists(os.path.join(backups_dir, "observatory-2026.db"))


def test_install_dry_run_reset_data_refuses_when_config_is_under_state_dir():
    """CONFIG_PATH resolving under STATE_DIR would make --reset-data's
    removal loop delete the config itself; refuse before any side effects
    (no confirmation prompt, no service stop, no rm planned) rather than
    letting the loop silently sweep it up."""
    with tempfile.TemporaryDirectory() as state_dir:
        config_path = os.path.join(state_dir, "config.toml")
        env = _dry_run_env()
        env["MESHGARDEN_INSTALL_STATE_DIR"] = state_dir
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                config_path,
                "--reset-data",
                "--yes",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0, result.stdout + result.stderr
        combined = result.stdout + result.stderr
        assert "config" in combined.lower()
        assert "would run: rm" not in result.stdout


def test_install_dry_run_reset_data_refuses_when_another_instance_exists():
    """A second instance's config means STATE_DIR also holds its <name>/
    state and backups plus the shared bbs.db: resetting the default
    instance must refuse rather than wipe them under a running peer."""
    with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as cfg_dir:
        config_path = os.path.join(cfg_dir, "config.toml")
        with open(os.path.join(cfg_dir, "mt.toml"), "w") as f:
            f.write('[radio]\ntransport = "meshtastic"\n')
        env = _dry_run_env()
        env["MESHGARDEN_INSTALL_STATE_DIR"] = state_dir
        result = subprocess.run(
            [
                INSTALL_SCRIPT,
                "--dry-run",
                "--skip-apt",
                "--skip-uv-install",
                "--no-start",
                "--no-enable",
                "--config",
                config_path,
                "--reset-data",
                "--yes",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0, result.stdout + result.stderr
        assert "mt.toml" in result.stdout + result.stderr
        assert "would run: rm" not in result.stdout


def test_install_dry_run_without_reset_data_plans_no_state_removal():
    result = subprocess.run(
        [
            INSTALL_SCRIPT,
            "--dry-run",
            "--skip-apt",
            "--skip-uv-install",
            "--no-start",
            "--no-enable",
        ],
        capture_output=True,
        text=True,
        env=_dry_run_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "observatory.db" not in result.stdout


def test_install_dry_run_reset_data_without_yes_skips_prompt_but_still_plans():
    result = subprocess.run(
        [
            INSTALL_SCRIPT,
            "--dry-run",
            "--skip-apt",
            "--skip-uv-install",
            "--no-start",
            "--no-enable",
            "--reset-data",
        ],
        capture_output=True,
        text=True,
        env=_dry_run_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "observatory.db" in result.stdout
    assert 'type "yes"' not in result.stdout.lower()


# =============================================================================
# --debug
# =============================================================================


def _exec_start_preview(stdout):
    """The dry-run log line previewing the rendered unit's ExecStart line
    (distinct from any other line that merely mentions "ExecStart=")."""
    for line in stdout.splitlines():
        if "ExecStart=" in line and "would render" in line:
            return line
    return None


def test_install_dry_run_renders_debug_exec_start():
    result = subprocess.run(
        [
            INSTALL_SCRIPT,
            "--dry-run",
            "--skip-apt",
            "--skip-uv-install",
            "--no-start",
            "--no-enable",
            "--debug",
        ],
        capture_output=True,
        text=True,
        env=_dry_run_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    exec_line = _exec_start_preview(result.stdout)
    assert exec_line is not None, result.stdout
    assert exec_line.rstrip().endswith("run --debug"), exec_line


def test_install_dry_run_without_debug_renders_plain_exec_start():
    result = subprocess.run(
        [
            INSTALL_SCRIPT,
            "--dry-run",
            "--skip-apt",
            "--skip-uv-install",
            "--no-start",
            "--no-enable",
        ],
        capture_output=True,
        text=True,
        env=_dry_run_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    exec_line = _exec_start_preview(result.stdout)
    assert exec_line is not None, result.stdout
    assert exec_line.rstrip().endswith(" run"), exec_line
    assert "--debug" not in result.stdout


def test_install_dry_run_debug_prints_notice():
    result = subprocess.run(
        [
            INSTALL_SCRIPT,
            "--dry-run",
            "--skip-apt",
            "--skip-uv-install",
            "--no-start",
            "--no-enable",
            "--debug",
        ],
        capture_output=True,
        text=True,
        env=_dry_run_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "NOTICE" in combined
    assert "journal" in combined.lower()
    assert "logs" in combined.lower()
    assert "exchange" in combined.lower()


def test_install_dry_run_without_debug_omits_notice():
    result = subprocess.run(
        [
            INSTALL_SCRIPT,
            "--dry-run",
            "--skip-apt",
            "--skip-uv-install",
            "--no-start",
            "--no-enable",
        ],
        capture_output=True,
        text=True,
        env=_dry_run_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "NOTICE" not in combined


def test_uninstall_dry_run_preserves_config_and_state_by_default():
    result = subprocess.run(
        [UNINSTALL_SCRIPT, "--dry-run"],
        capture_output=True,
        text=True,
        env=_dry_run_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "preserving" in result.stdout
    assert "would run: rm -rf /etc/meshgarden" not in result.stdout


def test_uninstall_dry_run_purge_with_yes_plans_removal():
    result = subprocess.run(
        [UNINSTALL_SCRIPT, "--dry-run", "--purge", "--yes"],
        capture_output=True,
        text=True,
        env=_dry_run_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "would run: rm -rf /etc/meshgarden" in result.stdout
    assert "would run: rm -rf /var/lib/meshgarden" in result.stdout


def test_uninstall_removes_both_cli_wrappers():
    text = _read(UNINSTALL_SCRIPT)
    assert "/usr/local/bin/$bin_name" in text
    assert "for bin_name in mgarden meshtastic-observatory" in text


def test_uninstall_dry_run_handles_cli_wrappers_based_on_ambient_state():
    """Deterministic regardless of whether this machine happens to already
    have the wrappers installed: either way, the dry-run plan must say the
    right thing about each one."""
    result = subprocess.run(
        [UNINSTALL_SCRIPT, "--dry-run"], capture_output=True, text=True, env=_dry_run_env()
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("mgarden", "meshtastic-observatory"):
        path = f"/usr/local/bin/{name}"
        if os.path.exists(path):
            assert f"would run: rm -f {path}" in result.stdout
        else:
            assert f"{path} not present; nothing to remove" in result.stdout


def test_uninstall_script_stops_disables_and_removes_watchdog_units():
    """The watchdog timer/service (docs/operations.md), if the operator ever
    installed them, must be stopped/disabled/removed alongside the main
    unit -- otherwise the timer keeps firing every minute after
    /usr/local/bin/mgarden is deleted."""
    text = _read(UNINSTALL_SCRIPT)
    assert "meshgarden-watchdog.timer" in text
    assert "meshgarden-watchdog.service" in text
    assert "systemctl stop \"$WATCHDOG_TIMER_NAME\"" in text
    assert "systemctl disable \"$WATCHDOG_TIMER_NAME\"" in text
    assert "rm -f \"$watchdog_unit\"" in text


def test_uninstall_dry_run_handles_watchdog_units_based_on_ambient_state():
    result = subprocess.run(
        [UNINSTALL_SCRIPT, "--dry-run"], capture_output=True, text=True, env=_dry_run_env()
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for unit in ("meshgarden-watchdog.timer", "meshgarden-watchdog.service"):
        path = f"/etc/systemd/system/{unit}"
        if os.path.exists(path):
            assert f"would run: rm -f {path}" in result.stdout
        else:
            assert f"{path} not present; nothing to remove" in result.stdout


# =============================================================================
# CLI wrapper (/usr/local/bin/mgarden, /usr/local/bin/meshtastic-observatory)
# =============================================================================


def _extract_bash_function(text, name):
    """Pulls a single top-level `name() { ... }` function out of a script's
    source, by locating its unindented open/close braces. Used to render
    install-rpi.sh's render_cli_wrapper() in isolation, without running the
    rest of the (root-requiring, systemd-requiring) script."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}()"))
    end = start
    while lines[end] != "}":
        end += 1
    return "\n".join(lines[start : end + 1])


def _sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _render_wrapper(bin_name, repo_dir):
    func_src = _extract_bash_function(_read(INSTALL_SCRIPT), "render_cli_wrapper")
    script = (
        f"REPO_DIR={_sh_quote(repo_dir)}\n"
        f"{func_src}\n"
        f"render_cli_wrapper {_sh_quote(bin_name)}\n"
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_cli_wrapper_config_insertion_logic():
    with tempfile.TemporaryDirectory() as tmp:
        checkout = os.path.join(tmp, "checkout")
        venv_bin = os.path.join(checkout, ".venv", "bin")
        os.makedirs(venv_bin)
        stub = os.path.join(venv_bin, "mgarden")
        with open(stub, "w") as f:
            f.write('#!/usr/bin/env bash\nfor a in "$@"; do printf \'%s\\n\' "$a"; done\n')
        os.chmod(stub, 0o755)

        wrapper_text = _render_wrapper("mgarden", checkout)
        assert wrapper_text.startswith("#!/usr/bin/env bash")
        assert f"{checkout}/.venv/bin/mgarden" in wrapper_text

        wrapper_path = os.path.join(tmp, "mgarden")
        with open(wrapper_path, "w") as f:
            f.write(wrapper_text)
        os.chmod(wrapper_path, 0o755)

        config_present = os.path.exists("/etc/meshgarden/config.toml")

        def run_wrapper(*args):
            r = subprocess.run([wrapper_path, *args], capture_output=True, text=True)
            assert r.returncode == 0, r.stdout + r.stderr
            return r.stdout.splitlines()

        # No --config at all: --config is inserted before the subcommand,
        # but only iff the default install config actually exists on this
        # machine -- deterministic in either case.
        out = run_wrapper("status")
        if config_present:
            assert out == ["--config", "/etc/meshgarden/config.toml", "status"]
        else:
            assert out == ["status"]

        # --config already given (two-token form): passed through
        # unmodified and never duplicated, regardless of ambient state.
        assert run_wrapper("--config", "/tmp/x.toml", "status") == [
            "--config",
            "/tmp/x.toml",
            "status",
        ]

        # --config=... (single-token form): also detected, unmodified.
        assert run_wrapper("--config=/tmp/x.toml", "status") == [
            "--config=/tmp/x.toml",
            "status",
        ]

        # --help alone, no subcommand: the same insertion rule still
        # applies ahead of it.
        out = run_wrapper("--help")
        if config_present:
            assert out == ["--config", "/etc/meshgarden/config.toml", "--help"]
        else:
            assert out == ["--help"]


# =============================================================================
# --fix-home-traversal
# =============================================================================


def _copied_checkout(tmp):
    """Copies install-rpi.sh into <tmp>/checkout/scripts/, so the script's
    own REPO_DIR (derived from BASH_SOURCE) resolves to a throwaway
    directory tree this test controls, instead of this repo's real
    location."""
    checkout = os.path.join(tmp, "checkout")
    scripts_dir = os.path.join(checkout, "scripts")
    os.makedirs(scripts_dir)
    dest = os.path.join(scripts_dir, "install-rpi.sh")
    shutil.copy(INSTALL_SCRIPT, dest)
    os.chmod(dest, 0o755)
    return checkout, dest


def _run_install_copy(dest, *extra_args):
    return subprocess.run(
        [
            dest,
            "--dry-run",
            "--skip-apt",
            "--skip-uv-install",
            "--no-start",
            "--no-enable",
            *extra_args,
        ],
        capture_output=True,
        text=True,
        env=_dry_run_env(),
    )


def test_fix_home_traversal_dry_run_chmods_a_non_traversable_ancestor():
    with tempfile.TemporaryDirectory() as tmp:
        fakehome = os.path.join(tmp, "fakehome")
        os.makedirs(fakehome)
        os.chmod(fakehome, 0o700)  # simulate a mode-0700 home dir: no o+x
        checkout, dest = _copied_checkout(fakehome)

        result = _run_install_copy(dest, "--fix-home-traversal")
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"would run: chmod o+x {fakehome}" in result.stdout


def test_fix_home_traversal_dry_run_skips_an_already_traversable_ancestor():
    with tempfile.TemporaryDirectory() as tmp:
        fakehome = os.path.join(tmp, "fakehome")
        os.makedirs(fakehome)
        os.chmod(fakehome, 0o755)  # already traversable: nothing to fix here
        checkout, dest = _copied_checkout(fakehome)

        result = _run_install_copy(dest, "--fix-home-traversal")
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"would run: chmod o+x {fakehome}" not in result.stdout


def test_without_fix_home_traversal_flag_no_chmod_is_planned():
    with tempfile.TemporaryDirectory() as tmp:
        fakehome = os.path.join(tmp, "fakehome")
        os.makedirs(fakehome)
        os.chmod(fakehome, 0o700)
        checkout, dest = _copied_checkout(fakehome)

        result = _run_install_copy(dest)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "would run: chmod o+x" not in result.stdout


def test_install_help_mentions_fix_home_traversal_remedy():
    result = subprocess.run([INSTALL_SCRIPT, "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "--fix-home-traversal" in result.stdout
    # Mentioned both in the Options table and in the Pre-flight remedy text.
    assert result.stdout.count("--fix-home-traversal") >= 2


# =============================================================================
# systemd unit template
# =============================================================================


def _read(path):
    with open(path) as f:
        return f.read()


def test_unit_template_uses_a_placeholder_checkout_path():
    text = _read(UNIT_TEMPLATE)
    assert "__CHECKOUT__" in text


def test_unit_template_renders_with_an_absolute_exec_start():
    text = _read(UNIT_TEMPLATE)
    rendered = text.replace("__CHECKOUT__", "/opt/meshgarden")
    exec_start = next(
        line for line in rendered.splitlines() if line.startswith("ExecStart=")
    )
    exec_path = exec_start[len("ExecStart=") :].split()[0]
    assert os.path.isabs(exec_path)
    assert exec_path == "/opt/meshgarden/.venv/bin/mgarden"
    assert "--config" in exec_start
    assert "/etc/meshgarden/config.toml" in exec_start
    assert "--debug" not in exec_start

    working_dir = next(
        line for line in rendered.splitlines() if line.startswith("WorkingDirectory=")
    )
    assert working_dir == "WorkingDirectory=/opt/meshgarden"


def test_unit_template_is_unprivileged_with_dedicated_group():
    text = _read(UNIT_TEMPLATE)
    assert "User=meshgarden" in text
    assert "Group=meshgarden" in text
    assert "Group=dialout" not in text
    assert "SupplementaryGroups=dialout" in text
    assert "NoNewPrivileges=true" in text
    assert "Restart=on-failure" in text
    assert "WatchdogSec=120" in text
    assert "Type=notify" in text
    assert "ReadWritePaths=/var/lib/meshgarden" in text
    assert "StateDirectory=meshgarden" in text
    assert "ConfigurationDirectory=meshgarden" in text


def test_unit_template_keeps_checkout_visible_via_protecthome_read_only():
    """ProtectHome=true would hide /home entirely, breaking ExecStart/
    WorkingDirectory for the documented in-place-under-~/meshgarden
    install; read-only keeps the checkout visible without making it
    writable (state still only lives under /var/lib/meshgarden)."""
    text = _read(UNIT_TEMPLATE)
    assert "ProtectHome=read-only" in text
    assert "ProtectHome=true" not in text


def _unit_files():
    return [UNIT_TEMPLATE, LEGACY_UNIT]


def _minimal_unit_key_check(path):
    text = _read(path)
    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in text, f"{path} missing {section}"
    assert "ExecStart=" in text
    assert "WantedBy=" in text


def test_repo_unit_files_parse():
    systemd_analyze = shutil.which("systemd-analyze")
    for path in _unit_files():
        if systemd_analyze:
            # __CHECKOUT__ isn't a valid path on its own; render it first
            # so systemd-analyze verify parses the same unit an install
            # would actually produce.
            text = _read(path)
            rendered = text.replace("__CHECKOUT__", "/opt/meshgarden")
            with tempfile.NamedTemporaryFile(
                "w", suffix=".service", delete=False
            ) as f:
                f.write(rendered)
                tmp_path = f.name
            try:
                result = subprocess.run(
                    [systemd_analyze, "verify", tmp_path],
                    capture_output=True,
                    text=True,
                )
                # systemd-analyze verify exits non-zero for warnings about
                # things only meaningful on a live system (missing users,
                # devices); a hard parse failure is what we actually guard.
                assert "Failed to load" not in result.stderr, result.stderr
                assert "Trailing garbage" not in result.stderr, result.stderr
            finally:
                os.unlink(tmp_path)
        else:
            _minimal_unit_key_check(path)


def test_legacy_unit_unchanged_assertions_still_hold():
    """Guards that our new header comment on the legacy unit didn't
    disturb anything tests/test_packaging.py depends on."""
    text = _read(LEGACY_UNIT)
    assert "User=" in text and "User=root" not in text
    assert "Restart=on-failure" in text
    assert "NoNewPrivileges=true" in text
    assert "Group=dialout" not in text
    assert "Group=meshobs" in text
    assert "SupplementaryGroups=dialout" in text
    assert "StateDirectoryMode=0700" in text
    exec_start = next(
        line for line in text.splitlines() if line.startswith("ExecStart=")
    )
    assert "--debug" not in exec_start


def test_legacy_unit_points_to_the_new_unit():
    text = _read(LEGACY_UNIT)
    assert "meshgarden.service" in text
    assert "LEGACY" in text.upper()
