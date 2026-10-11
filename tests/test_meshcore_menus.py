"""Menu replies must survive the actual MeshCore send boundary intact."""

import pytest

from meshgarden.commands import (
    _HELP_USAGE,
    garden_text,
    lab_text,
    me_text,
    menu_reply_for,
    menu_text,
    not_emergency_text,
    quota_hint_suffix,
    record_text,
    with_quota_hint,
)
from tests.test_daemon_meshcore import (
    LAB_UNLOCK_WORD,
    VISITOR_NODE_ID,
    VISITOR_PUBKEY,
    _cfg,
    _start_meshcore_daemon,
)


@pytest.mark.parametrize(
    ("command_text", "page", "unlocked"),
    [
        ("MENU", "", False),
        ("?", "", False),
        ("MENU typo", "", False),
        ("MENU", "", True),
        ("MENU STATS", "STATS", False),
        ("? STATS", "STATS", False),
        ("MENU GAMES", "GAMES", False),
        ("MENU DAILY", "DAILY", False),
        ("MENU BOARDS", "BOARDS", True),
        ("MENU LAB", "LAB", True),
        (LAB_UNLOCK_WORD, "LAB", False),
    ],
)
def test_entire_menu_reaches_meshcore_recipient(tmp_path, command_text, page, unlocked):
    d, ft = _start_meshcore_daemon(_cfg(tmp_path))
    try:
        assert ft.max_text_bytes == 120
        ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
        d.drain()
        if unlocked:
            with d.conn:
                d.conn.execute(
                    "INSERT INTO lab_nodes(node_id,unlocked_at) VALUES (?,1)", (VISITOR_NODE_ID,)
                )
        ft.fake.emit_direct_message(VISITOR_PUBKEY[:12], command_text)
        d.drain()
        assert len(ft.fake.sent) == 1
        reply = ft.fake.sent[0]["msg"]
        expected = menu_reply_for(page, lab_unlocked=unlocked)
        if not page:
            expected += quota_hint_suffix(1, d.cfg.service.daily_command_quota)
        assert reply == expected
        assert len(reply.encode("utf-8")) <= 120
    finally:
        d.stop()


@pytest.mark.parametrize("unlocked", [False, True])
def test_main_menu_keeps_all_commands_with_three_digit_quota(unlocked):
    base = menu_text(lab_unlocked=unlocked)
    reply = with_quota_hint(base, 999, 999, max_bytes=120)
    assert reply == base + quota_hint_suffix(999, 999)
    assert len(reply.encode("utf-8")) <= 120
    assert reply.endswith("MENU DAILY · quota 999/999 today")


def test_command_help_and_lab_unlock_also_fit_meshcore():
    for text in [*_HELP_USAGE.values(), lab_text()]:
        assert len(text.encode("utf-8")) <= 120


def test_not_emergency_text_fits_meshcore():
    reply = not_emergency_text(max_bytes=120)
    assert len(reply.encode("utf-8")) <= 120
    assert "MENU" in reply


def test_garden_text_worst_case_fits_meshcore():
    reply = garden_text(
        "MeshGarden Prime",
        nodes_today=99,
        heard_direct=99,
        best_snr=-99.9,
        guestbook_entries=9999,
        furthest_direct_km=999.9,
        max_bytes=120,
    )
    assert len(reply.encode("utf-8")) <= 120
    assert reply.endswith("❓ MENU")


def test_me_text_worst_case_fits_meshcore():
    reply = me_text(
        "!ffffffff",
        display_name="LongName",
        first_seen_date="2026-09-01",
        days_seen=7,
        heard_24h=99,
        snr=-99.9,
        rssi=-120,
        hop_start=7,
        hop_limit=0,
        pki_encrypted=True,
        signed_count=99,
        quota_used=9,
        quota_limit=99,
        mail_enabled=True,
        mail_pending=9,
        max_bytes=120,
    )
    assert len(reply.encode("utf-8")) <= 120
    assert "Signed" in reply and "mail on" in reply


def test_record_text_worst_case_fits_meshcore():
    reply = record_text(
        best_snr=-99.9,
        best_snr_node_id="!ffffffff",
        best_snr_node_name="LongName",
        max_hops=9,
        busiest_hour=23,
        total_packets=99999,
        furthest_observed_km=999.9,
        furthest_observed_node_id="!ffffffff",
        furthest_observed_node_name="LongName",
        max_bytes=120,
    )
    assert len(reply.encode("utf-8")) <= 120
    assert reply.endswith("🌱")
