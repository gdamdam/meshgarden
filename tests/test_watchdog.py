"""Tests for meshgarden.watchdog.

The watchdog watches the daemon from OUTSIDE it (a systemd timer polling the
metrics endpoint), so that a daemon which has died, hung, or gone deaf is
still reported. Its decision logic is a pure function so every anti-flap and
power-cycle guard is testable without sleeping or touching hardware.
"""

import json

import pytest

from meshgarden import watchdog as wd
from meshgarden.config import MetricsConfig


def _cfg(**over):
    base = dict(grace_seconds=300.0, max_cycles_per_day=3, cycle_cooldown_seconds=900.0)
    base.update(over)
    return wd.Thresholds(**base)


def _state(**over):
    s = wd.new_state()
    s.update(over)
    return s


# -- metrics parsing ---------------------------------------------------


def test_parses_a_connected_gauge_of_one():
    text = "# HELP x\nmeshgarden_radio_connected 1.0\nmeshgarden_queue_depth 0.0\n"
    assert wd.parse_radio_connected(text) is True


def test_parses_a_connected_gauge_of_zero():
    assert wd.parse_radio_connected("meshgarden_radio_connected 0.0\n") is False


def test_missing_gauge_is_unknown_not_healthy():
    """A metrics body without the gauge must never read as connected --
    that would make a broken endpoint look fine."""
    assert wd.parse_radio_connected("meshgarden_queue_depth 0.0\n") is None


def test_gauge_name_must_match_exactly_not_as_a_prefix():
    """meshgarden_radio_connected_seconds is a different gauge and is
    nonzero while connected -- matching it as a prefix would invert the
    reading."""
    assert wd.parse_radio_connected("meshgarden_radio_connected_seconds 1234.0\n") is None


def test_comment_lines_mentioning_the_gauge_are_not_samples():
    text = "# HELP meshgarden_radio_connected 1 if connected\n# TYPE x gauge\n"
    assert wd.parse_radio_connected(text) is None


# -- metrics URL -------------------------------------------------------


def test_metrics_url_uses_loopback_even_when_bound_to_all_interfaces():
    """0.0.0.0 is a bind address, not a destination: the watchdog runs on
    the same host and must always scrape loopback."""
    assert wd.metrics_url(MetricsConfig(listen="0.0.0.0", port=9108)) == (
        "http://127.0.0.1:9108/metrics"
    )


def test_metrics_url_preserves_a_specific_bind_address():
    assert wd.metrics_url(MetricsConfig(listen="192.168.8.107", port=9999)) == (
        "http://192.168.8.107:9999/metrics"
    )


def test_metrics_url_brackets_an_ipv6_literal():
    assert wd.metrics_url(MetricsConfig(listen="::1", port=9108)) == ("http://[::1]:9108/metrics")


# -- decide(): healthy path --------------------------------------------


def test_healthy_does_nothing_when_nothing_was_wrong():
    d = wd.decide(_state(), status="ok", now=1000.0, today="2026-09-17", cfg=_cfg())
    assert d.notifications == [] and d.cycle is False
    assert d.state["bad_since"] is None


def test_recovery_notifies_only_if_the_outage_was_announced():
    d = wd.decide(
        _state(bad_since=100.0, notified=True),
        status="ok",
        now=1000.0,
        today="2026-09-17",
        cfg=_cfg(),
    )
    assert len(d.notifications) == 1
    title, body = d.notifications[0]
    assert "recovered" in (title + " " + body).lower()
    assert d.state["bad_since"] is None and d.state["notified"] is False


def test_recovery_is_silent_if_the_outage_never_passed_the_grace_period():
    """The radio re-enumerates on any config change from the phone app;
    that must not produce a down/up pair of pages."""
    d = wd.decide(_state(bad_since=990.0), status="ok", now=1000.0, today="2026-09-17", cfg=_cfg())
    assert d.notifications == []


# -- decide(): outage path ---------------------------------------------


def test_first_bad_poll_only_records_the_time():
    d = wd.decide(_state(), status="radio_down", now=1000.0, today="2026-09-17", cfg=_cfg())
    assert d.state["bad_since"] == 1000.0
    assert d.notifications == [] and d.cycle is False


def test_within_grace_period_stays_quiet():
    d = wd.decide(
        _state(bad_since=1000.0),
        status="radio_down",
        now=1200.0,
        today="2026-09-17",
        cfg=_cfg(),
    )
    assert d.notifications == [] and d.cycle is False


def test_past_grace_period_notifies_and_cycles():
    d = wd.decide(
        _state(bad_since=1000.0),
        status="radio_down",
        now=1400.0,
        today="2026-09-17",
        cfg=_cfg(),
    )
    assert len(d.notifications) == 2  # the outage, and the power cycle
    assert d.cycle is True
    assert d.state["notified"] is True
    assert d.state["cycles"] == {"2026-09-17": 1}


def test_a_continuing_outage_never_re_notifies():
    s = _state(bad_since=1000.0, notified=True, cycles={"2026-09-17": 1}, last_cycle_at=1400.0)
    for now in (1500.0, 1600.0, 1700.0):
        d = wd.decide(s, status="radio_down", now=now, today="2026-09-17", cfg=_cfg())
        assert d.notifications == [], now
        s = d.state


# -- decide(): power-cycle guards --------------------------------------


def test_cycle_respects_the_cooldown():
    d = wd.decide(
        _state(bad_since=1000.0, notified=True, cycles={"2026-09-17": 1}, last_cycle_at=1400.0),
        status="radio_down",
        now=1500.0,
        today="2026-09-17",
        cfg=_cfg(),
    )
    assert d.cycle is False  # 100s since the last cycle, cooldown is 900s


def test_cycle_resumes_once_the_cooldown_elapses():
    d = wd.decide(
        _state(bad_since=1000.0, notified=True, cycles={"2026-09-17": 1}, last_cycle_at=1400.0),
        status="radio_down",
        now=2400.0,
        today="2026-09-17",
        cfg=_cfg(),
    )
    assert d.cycle is True
    assert d.state["cycles"] == {"2026-09-17": 2}


def test_daily_cap_stops_cycling():
    """A hardware fault must not loop on power-cycling the radio forever."""
    d = wd.decide(
        _state(bad_since=1000.0, notified=True, cycles={"2026-09-17": 3}, last_cycle_at=1400.0),
        status="radio_down",
        now=9000.0,
        today="2026-09-17",
        cfg=_cfg(),
    )
    assert d.cycle is False
    assert d.state["cycles"] == {"2026-09-17": 3}


def test_hitting_the_cap_notifies_once_so_the_operator_knows_to_intervene():
    s = _state(bad_since=1000.0, cycles={"2026-09-17": 3}, last_cycle_at=1400.0)
    d = wd.decide(s, status="radio_down", now=9000.0, today="2026-09-17", cfg=_cfg())
    blob = " ".join(b for _, b in d.notifications).lower()
    assert "cap" in blob or "manual" in blob


def test_the_cap_resets_on_a_new_day_and_old_days_are_pruned():
    d = wd.decide(
        _state(bad_since=1000.0, notified=True, cycles={"2026-09-16": 3}, last_cycle_at=1400.0),
        status="radio_down",
        now=90000.0,
        today="2026-09-17",
        cfg=_cfg(),
    )
    assert d.cycle is True
    assert d.state["cycles"] == {"2026-09-17": 1}  # yesterday's counter dropped


def test_cycling_can_be_disabled_entirely():
    d = wd.decide(
        _state(bad_since=1000.0),
        status="radio_down",
        now=1400.0,
        today="2026-09-17",
        cfg=_cfg(max_cycles_per_day=0),
    )
    assert d.cycle is False
    assert len(d.notifications) == 1  # still paged


# -- decide(): the daemon itself is gone -------------------------------


def test_an_unreachable_daemon_pages_but_never_power_cycles():
    """Cutting power to the radio cannot fix a node whose daemon is simply
    stopped -- possibly stopped by the operator for maintenance."""
    d = wd.decide(
        _state(bad_since=1000.0),
        status="unreachable",
        now=1400.0,
        today="2026-09-17",
        cfg=_cfg(),
    )
    assert d.cycle is False
    assert len(d.notifications) == 1
    assert "unreachable" in d.notifications[0][1].lower()


def test_switching_from_unreachable_to_radio_down_keeps_the_outage_clock():
    """The daemon coming back but the radio still being dead is the same
    outage continuing, not a fresh one -- restarting the clock would delay
    the power cycle by another grace period."""
    s = _state(bad_since=1000.0, notified=True)
    d = wd.decide(s, status="radio_down", now=1400.0, today="2026-09-17", cfg=_cfg())
    assert d.state["bad_since"] == 1000.0
    assert d.cycle is True


# -- message hygiene ---------------------------------------------------


def test_notifications_never_contain_a_node_id_or_message_text():
    """Alerts transit a third-party server: operational facts only."""
    d = wd.decide(
        _state(bad_since=1000.0),
        status="radio_down",
        now=1400.0,
        today="2026-09-17",
        cfg=_cfg(),
    )
    blob = " ".join(t + b for t, b in d.notifications)
    assert "!" not in blob  # no "!a1d3d810"-style node ids
    assert "mgrd" not in blob


# -- state file --------------------------------------------------------


def test_state_file_round_trips_and_tolerates_corruption(tmp_path):
    p = tmp_path / "state.json"
    assert wd.load_state(p) == wd.new_state()  # missing file
    s = _state(bad_since=5.0, notified=True, cycles={"2026-09-17": 2})
    wd.save_state(p, s)
    assert wd.load_state(p) == s
    p.write_text("{not json")
    assert wd.load_state(p) == wd.new_state()  # corrupt file must not wedge it


def test_state_file_is_owner_only(tmp_path):
    p = tmp_path / "state.json"
    wd.save_state(p, wd.new_state())
    assert (p.stat().st_mode & 0o777) == 0o600


def test_load_state_rejects_a_wrong_shaped_payload(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"bad_since": "not-a-number", "cycles": "nope"}')
    assert wd.load_state(p) == wd.new_state()


def test_saved_state_never_contains_the_ntfy_url(tmp_path):
    """The state file is the one thing the watchdog writes; the alert URL is
    a shared secret and must not leak into it."""
    p = tmp_path / "state.json"
    wd.save_state(p, _state(bad_since=1.0))
    assert "ntfy" not in p.read_text().lower()
    assert set(json.loads(p.read_text())) == set(wd.new_state())


# -- thresholds --------------------------------------------------------


def test_thresholds_come_from_the_environment_with_sane_defaults():
    cfg = wd.thresholds_from_env({})
    assert cfg.grace_seconds == 300.0
    assert cfg.max_cycles_per_day == 3
    assert cfg.cycle_cooldown_seconds == 900.0


def test_thresholds_ignore_unparseable_values_rather_than_crashing():
    cfg = wd.thresholds_from_env({"MESHGARDEN_WATCHDOG_GRACE_SECONDS": "banana"})
    assert cfg.grace_seconds == 300.0


def test_negative_thresholds_are_rejected_in_favor_of_the_default():
    cfg = wd.thresholds_from_env({"MESHGARDEN_WATCHDOG_MAX_CYCLES_PER_DAY": "-1"})
    assert cfg.max_cycles_per_day == 3


@pytest.mark.parametrize("value,expected", [("60", 60.0), ("0", 0.0), ("  120 ", 120.0)])
def test_thresholds_read_valid_overrides(value, expected):
    cfg = wd.thresholds_from_env({"MESHGARDEN_WATCHDOG_GRACE_SECONDS": value})
    assert cfg.grace_seconds == expected


# -- notify ------------------------------------------------------------


def test_notify_posts_the_title_and_body_to_the_configured_url():
    sent = {}

    def fake_post(url, data, headers, timeout):
        sent.update(url=url, data=data, headers=headers, timeout=timeout)

    wd.notify("https://ntfy.sh/topic", "Radio down", "no packets", priority="high", _post=fake_post)
    assert sent["url"] == "https://ntfy.sh/topic"
    assert sent["data"] == b"no packets"
    assert sent["headers"]["Title"] == "Radio down"
    assert sent["headers"]["Priority"] == "high"


def test_notify_without_a_url_is_a_no_op_not_a_crash():
    """An unconfigured watchdog must still run (and still power-cycle);
    it simply cannot page."""
    assert wd.notify("", "t", "b", _post=lambda *a, **k: pytest.fail("posted")) is False


def test_notify_swallows_a_delivery_failure():
    """A dead notification channel must never take down the watchdog, or a
    transient ntfy outage would stop the power-cycle recovery too."""

    def boom(*a, **k):
        raise OSError("network unreachable")

    assert wd.notify("https://ntfy.sh/t", "t", "b", _post=boom) is False


def test_the_cap_warning_survives_the_state_file_and_never_repeats(tmp_path):
    """Regression: `cap_notified` was not persisted, so once the daily cap
    was reached the "manual intervention needed" page went out on EVERY
    poll -- once a minute for the rest of the day."""
    p = tmp_path / "state.json"
    s = _state(bad_since=1000.0, notified=True, cycles={"2026-09-17": 3}, last_cycle_at=1400.0)
    pages = 0
    for now in (9000.0, 9060.0, 9120.0):
        wd.save_state(p, s)
        s = wd.load_state(p)
        d = wd.decide(s, status="radio_down", now=now, today="2026-09-17", cfg=_cfg())
        pages += len(d.notifications)
        s = d.state
    assert pages == 1


def test_recovery_rearms_the_cap_warning_for_a_later_outage():
    s = _state(bad_since=1000.0, notified=True, cap_notified=True, cycles={"2026-09-17": 3})
    d = wd.decide(s, status="ok", now=2000.0, today="2026-09-17", cfg=_cfg())
    assert d.state["cap_notified"] is False


# -- env file (the ntfy URL lives root-only, outside the repo) ---------


def test_env_file_is_parsed_with_comments_quotes_and_exports(tmp_path):
    f = tmp_path / "watchdog.env"
    f.write_text(
        "# a comment\n"
        "\n"
        "MESHGARDEN_NTFY_URL=https://ntfy.sh/sometopic\n"
        'export MESHGARDEN_WATCHDOG_GRACE_SECONDS="60"\n'
        "MESHGARDEN_WATCHDOG_HUB_PORT='4'\n"
        "NOT A VALID LINE\n"
    )
    env = wd.load_env_file(f)
    assert env["MESHGARDEN_NTFY_URL"] == "https://ntfy.sh/sometopic"
    assert env["MESHGARDEN_WATCHDOG_GRACE_SECONDS"] == "60"
    assert env["MESHGARDEN_WATCHDOG_HUB_PORT"] == "4"
    assert "NOT A VALID LINE" not in env


def test_missing_or_unreadable_env_file_is_empty_not_an_error(tmp_path):
    assert wd.load_env_file(tmp_path / "nope.env") == {}


def test_effective_env_prefers_the_process_environment_over_the_file(tmp_path):
    f = tmp_path / "watchdog.env"
    f.write_text("MESHGARDEN_NTFY_URL=https://ntfy.sh/from-file\n")
    env = wd.effective_env({"MESHGARDEN_NTFY_URL": "https://ntfy.sh/from-process"}, env_file=f)
    assert env["MESHGARDEN_NTFY_URL"] == "https://ntfy.sh/from-process"


def test_effective_env_falls_back_to_the_file(tmp_path):
    f = tmp_path / "watchdog.env"
    f.write_text("MESHGARDEN_NTFY_URL=https://ntfy.sh/from-file\n")
    env = wd.effective_env({}, env_file=f)
    assert env["MESHGARDEN_NTFY_URL"] == "https://ntfy.sh/from-file"


# -- reboot detection --------------------------------------------------


def test_first_poll_ever_records_the_boot_id_silently():
    s, rebooted = wd.note_boot(_state(), boot_id="aaa", now=100.0)
    assert (s["boot_id"], s["last_seen_at"], rebooted) == ("aaa", 100.0, False)


def test_same_boot_id_is_not_a_reboot():
    s, rebooted = wd.note_boot(_state(boot_id="aaa"), boot_id="aaa", now=100.0)
    assert not rebooted and s["boot_id"] == "aaa"


def test_a_changed_boot_id_is_a_reboot():
    s, rebooted = wd.note_boot(_state(boot_id="aaa"), boot_id="bbb", now=100.0)
    assert rebooted and s["boot_id"] == "bbb"


def test_an_unreadable_boot_id_never_pages_and_keeps_the_old_one():
    s, rebooted = wd.note_boot(_state(boot_id="aaa"), boot_id=None, now=100.0)
    assert not rebooted and s["boot_id"] == "aaa" and s["last_seen_at"] == 100.0


def test_boot_id_and_last_seen_round_trip_and_reject_wrong_types(tmp_path):
    p = tmp_path / "state.json"
    wd.save_state(p, _state(boot_id="aaa", last_seen_at=5.0))
    s = wd.load_state(p)
    assert (s["boot_id"], s["last_seen_at"]) == ("aaa", 5.0)
    p.write_text(json.dumps({**wd.new_state(), "boot_id": 7, "last_seen_at": "x"}))
    s = wd.load_state(p)
    assert (s["boot_id"], s["last_seen_at"]) == (None, None)


def test_reboot_notification_reports_downtime_and_clues():
    title, body = wd.reboot_notification(
        now=7200.0, last_seen_at=3600.0, clues=["a: System is rebooting"]
    )
    assert "rebooted" in title
    assert "gone about 60 min" in body and "System is rebooting" in body


def test_reboot_notification_without_clues_suggests_power_loss():
    _, body = wd.reboot_notification(now=7200.0, last_seen_at=None, clues=[])
    assert "Last alive: unknown" in body and "power cut" in body


def test_previous_boot_clues_swallow_journalctl_failures():
    class R:
        returncode, stdout, stderr = 1, "", "No journal for boot -1"

    assert wd.previous_boot_clues(_run=lambda *a, **k: R()) == []
    assert wd.previous_boot_clues(_run=lambda *a, **k: (_ for _ in ()).throw(OSError())) == []


def test_previous_boot_clues_trims_and_caps_lines():
    class R:
        returncode, stderr = 0, ""
        # Newest-first, as a journal left inconsistent by a power cut returns it.
        stdout = "\n".join(
            f"2026-09-19T10:{i:02d}:00 line {i} " + "x" * 300 for i in range(19, -1, -1)
        )

    clues = wd.previous_boot_clues(_run=lambda *a, **k: R())
    assert len(clues) == wd._MAX_CLUE_LINES
    assert all(len(c) <= wd._MAX_CLUE_CHARS for c in clues)
    assert "line 19" in clues[-1] and "line 8" in clues[0]


def test_run_once_pages_about_a_reboot_before_any_outage_alert(tmp_path):
    p = tmp_path / "state.json"
    wd.save_state(p, _state(boot_id="aaa", last_seen_at=1000.0))
    sent = []
    env = {wd.ENV_NTFY_URL: "https://ntfy.example/t"}
    wd.run_once(
        metrics=MetricsConfig(),
        state_path=p,
        env=env,
        now=4000.0,
        _poll=lambda url: "ok",
        _notify=lambda url, title, body, **kw: sent.append(title) or True,
        _cycle=lambda **kw: True,
        _boot_id=lambda: "bbb",
        _clues=lambda: ["k: Powering off."],
    )
    assert sent == ["Meshgarden host rebooted"]
    assert wd.load_state(p)["boot_id"] == "bbb"
    # Same boot again: silent.
    wd.run_once(
        metrics=MetricsConfig(),
        state_path=p,
        env=env,
        now=4060.0,
        _poll=lambda url: "ok",
        _notify=lambda url, title, body, **kw: sent.append(title) or True,
        _cycle=lambda **kw: True,
        _boot_id=lambda: "bbb",
        _clues=lambda: [],
    )
    assert sent == ["Meshgarden host rebooted"]


# -- soft USB reset and several instances -----------------------------


def _fake_sysfs(tmp_path):
    """/sys/class/tty/ttyACM1/device -> .../1-1.2/1-1.2:1.0, with the
    `authorized` switch on the USB device, plus a by-id style symlink."""
    sys_root = tmp_path / "sys"
    usb = sys_root / "devices" / "usb1" / "1-1" / "1-1.2"
    iface = usb / "1-1.2:1.0"
    iface.mkdir(parents=True)
    (usb / "idVendor").write_text("2886\n")
    (usb / "authorized").write_text("1\n")
    tty = sys_root / "class" / "tty" / "ttyACM1"
    tty.mkdir(parents=True)
    (tty / "device").symlink_to(iface)
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "ttyACM1").write_text("")
    (dev / "by-id-radio").symlink_to(dev / "ttyACM1")
    return sys_root, usb / "authorized", str(dev / "by-id-radio")


def test_usb_reset_toggles_only_the_radios_own_device(tmp_path):
    sys_root, switch, device = _fake_sysfs(tmp_path)
    writes = []

    def spy_sleep(seconds):
        writes.append(switch.read_text())

    assert wd.usb_reset(device, sys_root=str(sys_root), _sleep=spy_sleep)
    assert writes == ["0"] and switch.read_text() == "1"


def test_usb_reset_missing_device_is_reported_not_raised(tmp_path):
    sys_root, switch, _ = _fake_sysfs(tmp_path)
    assert wd.usb_reset(str(tmp_path / "dev" / "gone"), sys_root=str(sys_root),
                        _sleep=lambda s: None) is False
    assert switch.read_text() == "1\n"


def test_run_once_uses_usb_reset_without_a_hub_and_labels_alerts(tmp_path):
    p = tmp_path / "state.json"
    wd.save_state(p, _state(bad_since=0.0))
    sent, resets, cycles = [], [], []
    wd.run_once(
        metrics=MetricsConfig(), state_path=p, env={wd.ENV_NTFY_URL: "https://n/t"},
        now=1000.0, device="/dev/radio", label="Meshtastic", detect_reboot=False,
        _poll=lambda url: "radio_down",
        _notify=lambda url, title, body, **kw: sent.append(title) or True,
        _cycle=lambda **kw: cycles.append(kw) or True,
        _usb_reset=lambda dev: resets.append(dev) or True,
        _boot_id=lambda: (_ for _ in ()).throw(AssertionError("no reboot check")),
    )
    assert resets == ["/dev/radio"] and cycles == []
    assert sent == ["Meshgarden Meshtastic radio down",
                    "Meshgarden Meshtastic resetting the radio's USB link"]


def test_run_once_keeps_the_hub_power_cycle_when_a_hub_is_configured(tmp_path):
    p = tmp_path / "state.json"
    wd.save_state(p, _state(bad_since=0.0))
    resets, cycles = [], []
    wd.run_once(
        metrics=MetricsConfig(), state_path=p,
        env={wd.ENV_UHUBCTL_LOCATION: "1-1", wd.ENV_UHUBCTL_PORT: "2"},
        now=1000.0, device="/dev/radio",
        _poll=lambda url: "radio_down", _notify=lambda *a, **k: True,
        _cycle=lambda **kw: cycles.append(kw) or True,
        _usb_reset=lambda dev: resets.append(dev) or True,
        _usb_dir=lambda dev: "/sys/devices/usb1/1-1/1-1.2",
        _boot_id=lambda: None, _clues=lambda: [],
    )
    assert cycles == [{"hub": "1-1", "port": "2"}] and resets == []


def test_run_once_never_power_cycles_another_radios_hub_port(tmp_path):
    """Two radios, hub port 4 configured for the MeshCore one: the
    Meshtastic radio on port 2 gets its own soft reset, never port 4."""
    p = tmp_path / "state.json"
    wd.save_state(p, _state(bad_since=0.0))
    resets, cycles = [], []
    wd.run_once(
        metrics=MetricsConfig(), state_path=p,
        env={wd.ENV_UHUBCTL_LOCATION: "1-1", wd.ENV_UHUBCTL_PORT: "4"},
        now=1000.0, device="/dev/mt-radio", detect_reboot=False,
        _poll=lambda url: "radio_down", _notify=lambda *a, **k: True,
        _cycle=lambda **kw: cycles.append(kw) or True,
        _usb_reset=lambda dev: resets.append(dev) or True,
        _usb_dir=lambda dev: "/sys/devices/usb1/1-1/1-1.2",
    )
    assert resets == ["/dev/mt-radio"] and cycles == []
