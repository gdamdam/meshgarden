"""Daemon-level wiring tests for per-node daily quotas and the SIGN
banned-word guard. quota.py's DailyQuota and moderation.py's
load_banned_terms/find_banned_term are already exhaustively unit-tested
(tests/test_quota.py, tests/test_moderation.py); these prove main.py wires
them in correctly: a refused command/SIGN is a silent drop (bounded metric
outcome, no reply), and a banned-word SIGN is still stored and acked, only
hidden."""

from __future__ import annotations

from dataclasses import replace

import pytest

from meshgarden import guestbook
from meshgarden.config import (
    BackupConfig,
    Config,
    DatabaseConfig,
    MetricsConfig,
    ModerationConfig,
    RadioConfig,
    RetentionConfig,
    ServiceConfig,
)
from meshgarden.main import Daemon
from tests.fakes import FakeInterface, make_packet


def _cfg(tmp_path, **service_overrides):
    service = replace(ServiceConfig(), **service_overrides)
    return Config(
        radio=RadioConfig(device="/dev/fake", expected_node_id="!11223344"),
        service=service,
        database=DatabaseConfig(path=str(tmp_path / "o.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "backups")),
        metrics=MetricsConfig(),
    )


def _start(tmp_path, cfg):
    iface = FakeInterface(0x11223344)
    d = Daemon(cfg, interface_factory=lambda dev: iface, start_metrics=False)
    d.iface = iface
    d.start()
    return d


@pytest.fixture
def quota_daemon(tmp_path):
    d = _start(tmp_path, _cfg(tmp_path, daily_command_quota=1, per_node_cooldown_seconds=0))
    yield d
    d.stop()


@pytest.fixture
def sign_quota_daemon(tmp_path):
    d = _start(
        tmp_path,
        _cfg(
            tmp_path,
            daily_sign_quota=1,
            per_node_cooldown_seconds=0,
            sign_cooldown_seconds=0,
            identical_sign_dedup_hours=0,
        ),
    )
    yield d
    d.stop()


# --- command quota -----------------------------------------------------------------


def test_command_quota_explains_the_limit_without_consuming_more_quota(quota_daemon):
    d = quota_daemon
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=9900))
    d.drain()
    assert len(d.iface.sent) == 1

    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=9901))
    d.drain()
    assert len(d.iface.sent) == 2
    notice = d.iface.sent[-1]["text"]
    assert notice == (
        "📊 Daily command quota reached. "
        f"Resets at 00:00 {d.cfg.service.timezone}."
    )
    assert len(notice.encode("utf-8")) <= 120

    # claim() now runs BEFORE the quota check (see
    # test_duplicate_packet_id_does_not_consume_quota below for why), so a
    # quota-refused-but-not-a-duplicate request still gets a
    # processed_requests row -- settled to 'dropped'/'quota', the same
    # pattern _handle_sign already uses for sign_quota refusals.
    row = d.conn.execute(
        "SELECT state, reason FROM processed_requests WHERE packet_id=9901"
    ).fetchone()
    assert row["state"] in {"queued", "submitted", "acked"}

    used = d.conn.execute(
        "SELECT commands FROM node_daily_quota WHERE node_id='!aabbccdd'"
    ).fetchone()[0]
    assert used == 1
    assert d._limiter.check_command("!aabbccdd") is None
    assert ("!aabbccdd", 9901) not in d._sender._pending_retries

    assert d._metrics.commands_total.labels("MENU", "quota")._value.get() == 1


def test_command_quota_notice_deduplicates_and_is_throttled_until_next_local_day(
    quota_daemon,
):
    d = quota_daemon
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=9910))
    d.drain()
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=9911))
    d.drain()
    assert len(d.iface.sent) == 2

    # Retransmitting the quota-refused packet is request-deduplicated.
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=9911))
    d.drain()
    assert len(d.iface.sent) == 2

    # A distinct over-limit request inside the notice window stays silent.
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=9912))
    d.drain()
    assert len(d.iface.sent) == 2

    # The throttle key includes the configured local day, so a new day's
    # first refusal is eligible for a fresh explanation.
    d.advance_seconds(24 * 60 * 60)
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=9913))
    d.drain()
    assert len(d.iface.sent) == 3  # next day's accepted command
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=9914))
    d.drain()
    assert len(d.iface.sent) == 4
    assert d.iface.sent[-1]["text"].startswith("📊 Daily command quota reached.")


def test_typo_suggestion_stays_silent_when_command_quota_is_exhausted(tmp_path):
    d = _start(tmp_path, _cfg(tmp_path, daily_command_quota=1, per_node_cooldown_seconds=0))
    try:
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=9920))
        d.drain()
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="PONG", packet_id=9921))
        d.drain()
        assert len(d.iface.sent) == 1
        assert d._metrics.commands_total.labels("SUGGEST", "quota")._value.get() == 1
    finally:
        d.stop()


@pytest.mark.parametrize(
    ("quota_field", "text", "expected"),
    [
        ("daily_post_quota", "POST bike quota test", "Daily POST quota reached"),
        ("daily_mail_quota", "MAIL !99999999 quota test", "Daily MAIL quota reached"),
    ],
)
def test_post_and_mail_quota_refusals_get_scope_specific_notices(
    tmp_path, quota_field, text, expected
):
    d = _start(
        tmp_path,
        _cfg(
            tmp_path,
            lab_unlock_word="open-sesame",
            per_node_cooldown_seconds=0,
            **{quota_field: 0},
        ),
    )
    try:
        d.conn.execute(
            "INSERT INTO lab_nodes(node_id, unlocked_at) VALUES ('!aabbccdd', 0)"
        )
        d.conn.commit()
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text=text, packet_id=9930))
        d.drain()
        assert len(d.iface.sent) == 1
        notice = d.iface.sent[0]["text"]
        assert expected in notice
        assert f"00:00 {d.cfg.service.timezone}" in notice
        assert len(notice.encode("utf-8")) <= 120

        # A distinct refusal of the same quota kind stays silent.
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text=text, packet_id=9931))
        d.drain()
        assert len(d.iface.sent) == 1
    finally:
        d.stop()


def test_duplicate_packet_id_does_not_consume_quota(tmp_path):
    """Regression test: the daily command quota used to be counted BEFORE
    the dedup check, so a retransmitted/duplicate packet id consumed a
    quota slot even though it never produces a second reply. With
    daily_command_quota=2 and packet ids 1, 1, 2 (the second '1' a
    duplicate of the first), the bug made the legitimate, distinct id=2
    request quota-refused -- only one reply total instead of two."""
    d = _start(tmp_path, _cfg(tmp_path, daily_command_quota=2, per_node_cooldown_seconds=0))
    try:
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=1))
        d.drain()
        assert len(d.iface.sent) == 1

        # Same packet id again: a duplicate, must be silently deduplicated
        # and must NOT consume a second quota slot.
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=1))
        d.drain()
        assert len(d.iface.sent) == 1  # no second reply for the duplicate
        assert d._metrics.commands_total.labels("MENU", "deduplicated")._value.get() == 1

        # A genuinely new, distinct request must still get its reply --
        # only one quota slot has actually been consumed so far.
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=2))
        d.drain()
        assert len(d.iface.sent) == 2
        assert d._metrics.commands_total.labels("MENU", "quota")._value.get() == 0
    finally:
        d.stop()


def test_command_quota_is_scoped_per_node(quota_daemon):
    d = quota_daemon
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, from_num=0x11111111, text="MENU"))
    d.drain()
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, from_num=0x22222222, text="MENU"))
    d.drain()
    assert len(d.iface.sent) == 2  # each node has its own quota


# --- SIGN quota ----------------------------------------------------------------------


def test_sign_quota_reports_the_reason_instead_of_silence(sign_quota_daemon):
    """Fix round (SIGN reply-with-reason): a sign_quota refusal now gets
    guestbook.format_sign_quota() instead of no reply at all. The
    processed_requests row still records the sign_quota outcome via the
    METRIC label (asserted below) -- once a reply is actually queued for
    it, the row itself follows the ordinary queued/acked pipeline like any
    other sent reply, same as the pre-existing storage_full reply."""
    d = sign_quota_daemon
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="SIGN first entry", packet_id=9910))
    d.drain()
    assert len(d.iface.sent) == 1

    d.iface.emit_packet(
        make_packet(to_num=d.my_node_num, text="SIGN a second, different entry", packet_id=9911)
    )
    d.drain()
    assert len(d.iface.sent) == 2
    assert d.iface.sent[-1]["text"] == guestbook.format_sign_quota()

    count = d.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0]
    assert count == 1  # 2nd SIGN never inserted

    row = d.conn.execute(
        "SELECT state FROM processed_requests WHERE packet_id=9911"
    ).fetchone()
    assert row["state"] == "submitted"  # the reason reply, not the SIGN itself

    assert d._metrics.commands_total.labels("SIGN", "sign_quota")._value.get() == 1


def test_sign_quota_counts_only_new_entries_not_rejected_signs(tmp_path):
    """A SignError-rejected SIGN (e.g. too long) must not consume the sign
    quota -- check_and_count_sign only runs after validate() succeeds."""
    d = _start(
        tmp_path,
        _cfg(tmp_path, daily_sign_quota=1, per_node_cooldown_seconds=0, max_guestbook_chars=5),
    )
    try:
        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, text="SIGN this is way too long", packet_id=9912)
        )
        d.drain()
        assert d.iface.sent[0]["text"] == guestbook.format_sign_too_long(5)  # rejected, with reason

        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="SIGN ok", packet_id=9913))
        d.drain()
        assert len(d.iface.sent) == 2  # the quota was never consumed by the rejection
    finally:
        d.stop()


# --- banned-word moderation -----------------------------------------------------------


def test_sign_with_a_banned_word_is_hidden_but_still_acked(tmp_path):
    banned_path = tmp_path / "banned.txt"
    banned_path.write_text("badword\n")
    cfg = replace(
        _cfg(tmp_path), moderation=ModerationConfig(banned_words_file=str(banned_path))
    )
    d = _start(tmp_path, cfg)
    try:
        d.iface.emit_packet(
            make_packet(
                to_num=d.my_node_num, text="SIGN this has a badword in it", packet_id=9920
            )
        )
        d.drain()
        assert len(d.iface.sent) == 1
        assert d.iface.sent[0]["text"] == (
            "🌱 Signed!\nThanks! Others read it with GUESTBOOK"
        )  # normal ack, unaffected

        row = d.conn.execute("SELECT visible, moderation_reason FROM guestbook").fetchone()
        assert row["visible"] == 0
        assert row["moderation_reason"] == "banned_word"

        assert d._metrics.guestbook_entries_moderated._value.get() == 1
    finally:
        d.stop()


def test_moderated_entry_is_invisible_to_guestbook_reads(tmp_path):
    banned_path = tmp_path / "banned.txt"
    banned_path.write_text("badword\n")
    cfg = replace(
        _cfg(tmp_path), moderation=ModerationConfig(banned_words_file=str(banned_path))
    )
    d = _start(tmp_path, cfg)
    try:
        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, text="SIGN a badword here", packet_id=9921)
        )
        d.drain()
        d.advance_seconds(3601)
        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, text="GUESTBOOK", packet_id=9922)
        )
        d.drain()
        reply = d.iface.sent[-1]["text"]
        assert "is empty" in reply
    finally:
        d.stop()


def test_sign_without_a_matching_banned_word_is_visible_as_usual(tmp_path):
    banned_path = tmp_path / "banned.txt"
    banned_path.write_text("badword\n")
    cfg = replace(
        _cfg(tmp_path), moderation=ModerationConfig(banned_words_file=str(banned_path))
    )
    d = _start(tmp_path, cfg)
    try:
        d.iface.emit_packet(
            make_packet(to_num=d.my_node_num, text="SIGN a perfectly nice message", packet_id=9923)
        )
        d.drain()
        row = d.conn.execute("SELECT visible, moderation_reason FROM guestbook").fetchone()
        assert row["visible"] == 1
        assert row["moderation_reason"] is None
        assert d._metrics.guestbook_entries_moderated._value.get() == 0
    finally:
        d.stop()


def test_no_banned_words_file_configured_never_moderates(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="SIGN anything goes here", packet_id=9924)
    )
    daemon.drain()
    row = daemon.conn.execute("SELECT visible, moderation_reason FROM guestbook").fetchone()
    assert row["visible"] == 1
    assert row["moderation_reason"] is None


# --- rate-limit visibility summary -----------------------------------------------------


def test_status_snapshot_counts_a_cooldown_drop(daemon):
    assert daemon.status_snapshot()["rate_limited_drops_1h"] == 0
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="MENU", packet_id=9930))
    daemon.drain()
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="MENU", packet_id=9931))
    daemon.drain()  # 2nd MENU within the cooldown window -- silently dropped
    assert daemon.status_snapshot()["rate_limited_drops_1h"] == 1


def test_status_snapshot_counts_a_quota_drop(quota_daemon):
    d = quota_daemon
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=9932))
    d.drain()
    d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="MENU", packet_id=9933))
    d.drain()
    assert d.status_snapshot()["rate_limited_drops_1h"] == 1
