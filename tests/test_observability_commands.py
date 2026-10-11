"""Daemon-level wiring tests for WHOAMI/SIGNAL (now aliases of ME), GARDEN,
RECORD, WEEK, and UNSIGN: each command's own unit-level formatter
(commands.py) is already covered in tests/test_commands.py; these prove the
daemon dispatches to it correctly, with real observations/guestbook state
and through the normal _reserve_and_send path (same cooldowns/dedup/queue/
blocked/identity rules as every other command)."""

from __future__ import annotations

import re

from tests.fakes import make_packet


def b(s: str) -> int:
    return len(s.encode("utf-8"))


# --- WHOAMI / SIGNAL (aliases of ME) ----------------------------------------------


def test_whoami_is_an_alias_for_me_and_reports_the_requests_own_packet_fields(daemon):
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num,
            text="WHOAMI",
            packet_id=9800,
            rx_snr=10.5,
            rx_rssi=-72,
            hop_start=3,
            hop_limit=3,
            pki_encrypted=True,
        )
    )
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    reply = daemon.iface.sent[0]["text"]
    # WHOAMI dispatches to _handle_me (see commands.py's _BARE_COMMANDS), so
    # the reply is ME's own -- signal line plus Since/Signed/quota/mail,
    # with quota shown inline rather than an appended with_quota_hint
    # suffix.
    assert reply.startswith("📡 You are !aabbccdd\n")
    assert "1/7d · 1×/24h" in reply
    assert "SNR +10.5 · RSSI -72 · hops 0 direct · PKI ✓" in reply
    assert "Signed 0 · quota 1/60 · mail off" in reply
    row = daemon.conn.execute(
        "SELECT command FROM processed_requests WHERE packet_id=9800"
    ).fetchone()
    assert row["command"] == "ME"
    assert b(reply) <= 180


def test_signal_is_an_alias_for_me(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="SIGNAL", packet_id=9801))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]["text"].startswith("📡 You are !aabbccdd")


def test_whoami_missing_fields_render_n_a(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="WHOAMI", packet_id=9802))
    daemon.drain()
    reply = daemon.iface.sent[0]["text"]
    assert "SNR n/a" in reply
    assert "RSSI n/a" in reply
    assert "hops n/a" in reply
    assert "PKI ✗" in reply


def test_whoami_heard_24h_counts_every_observation_from_the_sender(daemon):
    # Non-command text is still recorded as an observation (before command
    # parsing/dispatch) but never consumes the per-node cooldown, so these
    # two can be sent back to back without waiting.
    for i in range(2):
        daemon.iface.emit_packet(
            make_packet(to_num=daemon.my_node_num, text="not a command", packet_id=9810 + i)
        )
        daemon.drain()
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="WHOAMI", packet_id=9812))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "3×/24h" in reply


# --- GARDEN ------------------------------------------------------------------------


def test_garden_reports_todays_nodes_and_guestbook_count(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="SIGN test entry", packet_id=9820)
    )
    daemon.drain()
    daemon.advance_seconds(31)  # clear the per-node command cooldown
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="GARDEN", packet_id=9821))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert reply.startswith(f"🌱 {daemon.cfg.service.name}\n1 node today")
    assert "1 guestbook entry" in reply
    assert reply.endswith("❓ MENU")
    assert b(reply) <= 180


def test_garden_heard_direct_counts_only_zero_hop_senders(daemon):
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num, text="gibberish", packet_id=9830, hop_start=3, hop_limit=3
        )
    )
    daemon.drain()
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="GARDEN", packet_id=9831))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "1 confirmed direct" in reply


def test_garden_best_snr_over_24h_window(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="gibberish", packet_id=9840, rx_snr=8.2)
    )
    daemon.drain()
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="GARDEN", packet_id=9841))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "Best SNR +8.2 dB" in reply


def test_garden_with_no_signal_reports_n_a(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="GARDEN", packet_id=9842))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "Best SNR n/a" in reply


# --- RECORD ------------------------------------------------------------------------


def test_record_reports_best_snr_with_node_tail_and_max_hops(daemon):
    daemon.iface.emit_packet(
        make_packet(
            to_num=daemon.my_node_num,
            text="gibberish",
            packet_id=9850,
            rx_snr=11.0,
            hop_start=6,
            hop_limit=1,
        )
    )
    daemon.drain()
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="RECORD", packet_id=9851))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "Best SNR +11.0 dB (ccdd)" in reply  # !aabbccdd's last 4 hex chars
    assert "Most RX hops 5 (ccdd)" in reply
    assert re.search(r"Busiest \d{2}:00", reply)
    assert reply.endswith("packets 🌱")
    assert b(reply) <= 180


def test_record_total_packets_counts_todays_observations(daemon):
    for i in range(3):
        daemon.iface.emit_packet(
            make_packet(to_num=daemon.my_node_num, text="gibberish", packet_id=9860 + i)
        )
        daemon.drain()
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="RECORD", packet_id=9863))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "4 packets" in reply  # 3 gibberish + RECORD's own packet


def test_record_with_no_signal_data_renders_n_a(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="RECORD", packet_id=9870))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "Best SNR n/a" in reply
    assert "Most RX hops n/a" in reply
    assert "1 packets" in reply


# --- WEEK --------------------------------------------------------------------------


def test_week_reports_7_day_aggregates_from_daily_stats(daemon):
    import time
    from datetime import UTC, datetime

    today = datetime.fromtimestamp(int(time.time()), tz=UTC).strftime("%Y-%m-%d")
    with daemon.conn:
        daemon.conn.execute(
            "INSERT INTO daily_stats (day, unique_packets, distinct_senders, "
            "entries_created, replies_sent) VALUES (?, ?, ?, ?, ?)",
            (today, 100, 20, 3, 15),
        )
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="WEEK", packet_id=9880))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "100 packets" in reply
    assert "20–20 senders per day" in reply
    assert "+3 guestbook entries" in reply
    assert "15 replies sent" in reply
    assert b(reply) <= 180


def test_week_with_no_daily_stats_rows_reports_n_a_range(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="WEEK", packet_id=9881))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert "n/a senders per day" in reply


# --- UNSIGN ------------------------------------------------------------------------


def test_unsign_hides_the_newest_visible_entry(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="SIGN test entry", packet_id=9890)
    )
    daemon.drain()
    daemon.advance_seconds(31)
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="UNSIGN", packet_id=9891))
    daemon.drain()
    reply = daemon.iface.sent[-1]["text"]
    assert reply == "🌱 Your newest note is now hidden\nOperators can still delete it"
    row = daemon.conn.execute(
        "SELECT visible FROM guestbook ORDER BY entry_id DESC LIMIT 1"
    ).fetchone()
    assert row["visible"] == 0


def test_unsign_with_nothing_to_hide(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="UNSIGN", packet_id=9892))
    daemon.drain()
    assert daemon.iface.sent[-1]["text"] == "🌱 Nothing to hide"


def test_unsign_counts_as_a_command(daemon):
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="UNSIGN", packet_id=9893))
    daemon.drain()
    row = daemon.conn.execute(
        "SELECT command FROM processed_requests WHERE packet_id=9893"
    ).fetchone()
    assert row["command"] == "UNSIGN"


def test_unsign_does_not_delete_the_entry(daemon):
    daemon.iface.emit_packet(
        make_packet(to_num=daemon.my_node_num, text="SIGN test entry", packet_id=9894)
    )
    daemon.drain()
    daemon.advance_seconds(31)
    daemon.iface.emit_packet(make_packet(to_num=daemon.my_node_num, text="UNSIGN", packet_id=9895))
    daemon.drain()
    count = daemon.conn.execute("SELECT count(*) FROM guestbook").fetchone()[0]
    assert count == 1  # still stored, only hidden -- an operator can still delete it
