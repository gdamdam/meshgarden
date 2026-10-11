"""Presentation checks for compact, human-readable command replies."""

from meshgarden.commands import (
    knock_text,
    mail_alias_text,
    mail_pref_ack_text,
    menu_reply_for,
)


def test_channel_knock_is_a_scannable_invitation_within_meshcore_budget():
    text = knock_text()
    assert text.splitlines() == [
        "🌱 MeshGarden channel knock",
        "Someone used your name on the channel.",
        "Send MENU here to explore.",
    ]
    assert len(text.encode("utf-8")) <= 120


def test_social_menu_has_a_heading_and_fits_meshcore_budget():
    text = menu_reply_for("SOCIAL")
    assert text.startswith("🤝 Social\n")
    assert len(text.encode("utf-8")) <= 120


def test_mail_preferences_use_short_status_first_layouts():
    enabled = mail_pref_ack_text(True)
    disabled = mail_pref_ack_text(False)
    assert enabled.startswith("📬 Mail is ON\n")
    assert "\n↩ Reply:" in enabled
    assert disabled == "📬 Mail is OFF\nNew notes are paused"
    assert len(enabled.encode("utf-8")) <= 120


def test_mail_alias_results_have_a_clear_heading_and_next_step():
    assert mail_alias_text("claimed", "gardenfox").splitlines() == [
        "📬 Alias: @gardenfox",
        "Send: MAIL @gardenfox <msg>",
    ]
    for status in ("taken", "cooldown", "none", "invalid"):
        text = mail_alias_text(status)
        assert text.startswith("📬 ")
        assert "\n" in text
        assert len(text.encode("utf-8")) <= 120
