"""Full advertised names survive whenever the radio reply has room."""

import pytest

from meshgarden.commands import mail_page, me_text, repeater_text
from meshgarden.mail import MailEntry


@pytest.mark.parametrize("name", ["Potrero Hill West", "🌳" * 80])
def test_repeater_full_name_or_safe_shortening(name):
    text = repeater_text(
        {"bat": 4100, "uptime": 60, "nb_recv": 1, "nb_sent": 2},
        label=f"{name} · abcd", age=12, stale=False,
    )
    assert "abcd" in text and "12s old" in text
    assert "4.10V" in text and "up 0h1m" in text
    assert len(text.encode()) <= 120
    if len(name) < 30:
        assert name in text


@pytest.mark.parametrize("name", ["Potrero Hill West", "🌳" * 80])
def test_me_full_name_or_safe_shortening(name):
    text = me_text(
        "!1234abcd", display_name=name, first_seen_date="2026-09-20",
        days_seen=1, heard_24h=1, snr=None, rssi=None,
        hop_start=None, hop_limit=None, pki_encrypted=None,
        signed_count=0, quota_used=1, quota_limit=60,
        mail_enabled=False, mail_pending=0, max_bytes=120,
    )
    assert "!1234abcd" in text and "mail off" in text
    assert len(text.encode()) <= 120
    if len(name) < 30:
        assert name in text


@pytest.mark.parametrize("name", ["Potrero Hill West", "🌳" * 80])
def test_partial_mail_fits_full_name_or_shortens_only_header(name):
    entry = MailEntry(1, 100, "!1234abcd", "!11223344", "hello " * 100, False, None)
    text, receipts = mail_page(
        [entry], now=110, max_bytes=120, labels={entry.sender_node_id: f"{name} · abcd"}
    )
    assert "abcd" in text and "More: MAIL" in text
    assert receipts and receipts[0][2] > 0
    assert len(text.encode()) <= 120
    if len(name) < 30:
        assert name in text
