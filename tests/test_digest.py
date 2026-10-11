import json
import os
import stat
from datetime import datetime
from zoneinfo import ZoneInfo

from meshgarden.config import (
    BackupConfig,
    Config,
    DatabaseConfig,
    MetricsConfig,
    RadioConfig,
    RetentionConfig,
    ServiceConfig,
)
from meshgarden.database import connect, migrate
from meshgarden.digest import (
    build_digest,
    default_digest_day,
    format_digest,
    prune_digests,
    write_digest_file,
)

TZ = "America/Los_Angeles"
DAY = "2026-09-14"


def local_ts(hour, minute=0, day=DAY, tz=TZ):
    dt = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hour, minute=minute, tzinfo=ZoneInfo(tz)
    )
    return int(dt.timestamp())


def mk(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    return conn


def cfg(tmp_path):
    return Config(
        radio=RadioConfig(device="/dev/fake", expected_node_id="!11223344"),
        service=ServiceConfig(timezone=TZ),
        database=DatabaseConfig(path=str(tmp_path / "o.db")),
        retention=RetentionConfig(),
        backup=BackupConfig(directory=str(tmp_path / "backups")),
        metrics=MetricsConfig(),
    )


def ins_obs(conn, ts, sender="!aabbccdd", pid=1, dup=0, portnum="TEXT_MESSAGE_APP"):
    conn.execute(
        "INSERT INTO observations(server_time,sender_node_id,dest_node_id,channel,"
        "portnum,packet_id,is_duplicate) VALUES (?,?,'^all',0,?,?,?)",
        (ts, sender, portnum, pid, dup),
    )
    conn.commit()


def ins_node(conn, node_id, first_seen, last_seen=None):
    conn.execute(
        "INSERT INTO nodes(node_id,observed_first_seen,observed_last_seen) VALUES (?,?,?)",
        (node_id, first_seen, last_seen if last_seen is not None else first_seen),
    )
    conn.commit()


def ins_gb(conn, created_at, sender="!aabbccdd", visible=1, reason=None, topic=None):
    conn.execute(
        "INSERT INTO guestbook(created_at,sender_node_id,message,message_hash,visible,"
        "moderation_reason,topic) VALUES (?,?,'msg','h',?,?,?)",
        (created_at, sender, visible, reason, topic),
    )
    conn.commit()


def ins_req(conn, sender, pid, first_seen_at, state, command=None):
    conn.execute(
        "INSERT INTO processed_requests(sender_node_id,packet_id,first_seen_at,expires_at,"
        "command,state,updated_at) VALUES (?,?,?,?,?,?,?)",
        (sender, pid, first_seen_at, first_seen_at + 86400, command, state, first_seen_at),
    )
    conn.commit()


# =============================================================================
# build_digest
# =============================================================================


def test_build_digest_counts_packets_unique_and_distinct_senders_in_window(tmp_path):
    conn = mk(tmp_path)
    ins_obs(conn, local_ts(10), sender="!aaaaaaaa", pid=1, dup=0)
    ins_obs(conn, local_ts(10, 1), sender="!aaaaaaaa", pid=1, dup=1)  # duplicate
    ins_obs(conn, local_ts(11), sender="!bbbbbbbb", pid=2, dup=0)
    ins_obs(conn, local_ts(23, 59), sender="!bbbbbbbb", pid=3, dup=0)
    # outside the window (previous/next day)
    ins_obs(conn, local_ts(10, day="2026-09-13"), sender="!cccccccc", pid=4, dup=0)
    ins_obs(conn, local_ts(0, day="2026-09-15"), sender="!dddddddd", pid=5, dup=0)

    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(12, day="2026-09-15"))
    assert d["packets"]["raw"] == 4
    assert d["packets"]["unique"] == 3
    assert d["distinct_senders"] == 2


def test_build_digest_counts_new_nodes_first_seen_that_local_day(tmp_path):
    conn = mk(tmp_path)
    ins_node(conn, "!11111111", local_ts(1))
    ins_node(conn, "!22222222", local_ts(2))
    ins_node(conn, "!33333333", local_ts(1, day="2026-09-13"))  # previous day

    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(12))
    assert d["new_nodes"] == 2


def test_build_digest_busiest_local_hour(tmp_path):
    conn = mk(tmp_path)
    for _ in range(3):
        ins_obs(conn, local_ts(20, 5), pid=10 + _)
    ins_obs(conn, local_ts(9), pid=20)

    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(23))
    assert d["busiest_hour"] == 20


def test_build_digest_busiest_hour_is_none_with_no_packets(tmp_path):
    conn = mk(tmp_path)
    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(23))
    assert d["busiest_hour"] is None


def test_build_digest_guestbook_signatures_and_held_count(tmp_path):
    conn = mk(tmp_path)
    ins_gb(conn, local_ts(9), visible=1)
    ins_gb(conn, local_ts(10), visible=0, reason="banned_word")
    ins_gb(conn, local_ts(11), visible=0, reason=None)  # operator-hidden, not "held"
    ins_gb(conn, local_ts(9, day="2026-09-13"))  # previous day, excluded

    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(23))
    assert d["guestbook"]["signatures"] == 3
    assert d["guestbook"]["held"] == 1


def test_build_digest_guestbook_total_excludes_board_posts(tmp_path):
    """Final-review fix (item 8): the digest's guestbook total describes
    the main guestbook, same as SIGN/GUESTBOOK/ME's Signed count -- a
    board post (topic NOT NULL) must not inflate it."""
    conn = mk(tmp_path)
    ins_gb(conn, local_ts(9), visible=1)
    ins_gb(conn, local_ts(10), visible=1, topic="bike")

    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(23))
    assert d["guestbook"]["signatures"] == 1


def test_build_digest_reply_outcomes(tmp_path):
    conn = mk(tmp_path)
    ins_req(conn, "!aaaaaaaa", 1, local_ts(9), "acked", command="SIGN")
    ins_req(conn, "!aaaaaaaa", 2, local_ts(9, 5), "acked", command="GUESTBOOK")
    ins_req(conn, "!bbbbbbbb", 3, local_ts(10), "failed", command="STATS")
    ins_req(conn, "!bbbbbbbb", 4, local_ts(11), "expired", command="SIGN")
    ins_req(conn, "!bbbbbbbb", 5, local_ts(12), "unknown", command="MENU")
    ins_req(conn, "!bbbbbbbb", 6, local_ts(13), "submitted", command="MENU")
    ins_req(conn, "!bbbbbbbb", 7, local_ts(1, day="2026-09-13"), "acked")  # excluded

    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(23))
    assert d["replies"] == {
        "submitted": 1,
        "acked": 2,
        "failed": 1,
        "expired": 1,
        "unknown": 1,
    }


def test_build_digest_commands_by_type(tmp_path):
    conn = mk(tmp_path)
    ins_req(conn, "!aaaaaaaa", 1, local_ts(9), "acked", command="SIGN")
    ins_req(conn, "!aaaaaaaa", 2, local_ts(9, 5), "acked", command="SIGN")
    ins_req(conn, "!bbbbbbbb", 3, local_ts(10), "dropped", command=None)  # unknown cmd, excluded

    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(23))
    assert d["commands"] == {"SIGN": 2}


def test_build_digest_book_and_wordle_solves(tmp_path):
    conn = mk(tmp_path)
    conn.execute(
        "INSERT INTO book_stats(book_key,day,shown,guesses,solves) VALUES ('Emma', ?, 5, 4, 2)",
        (DAY,),
    )
    conn.execute(
        "INSERT INTO wordle_daily(day,players,solves,total_guesses_on_solve) VALUES (?, 3, 2, 8)",
        (DAY,),
    )
    conn.commit()

    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(23))
    assert d["book_solves"] == 2
    assert d["wordle_solves"] == 2


def test_build_digest_drops_absent_when_daily_stats_has_no_such_columns(tmp_path):
    conn = mk(tmp_path)
    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(23))
    assert d["drops"] is None


def test_build_digest_drops_present_when_daily_stats_has_the_columns(tmp_path):
    conn = mk(tmp_path)
    conn.execute("ALTER TABLE daily_stats ADD COLUMN quota_drops INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE daily_stats ADD COLUMN cooldown_drops INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "INSERT INTO daily_stats(day, quota_drops, cooldown_drops) VALUES (?, 4, 7)", (DAY,)
    )
    conn.commit()

    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(23))
    assert d["drops"] == {"quota_drops": 4, "cooldown_drops": 7}


def test_build_digest_never_contains_a_node_id_or_message_text(tmp_path):
    conn = mk(tmp_path)
    ins_obs(conn, local_ts(10), sender="!aaaaaaaa", pid=1)
    ins_node(conn, "!aaaaaaaa", local_ts(10))
    ins_gb(conn, local_ts(10), sender="!aaaaaaaa")
    ins_req(conn, "!aaaaaaaa", 1, local_ts(10), "acked", command="SIGN")

    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(23))
    blob = json.dumps(d)
    assert "!aaaaaaaa" not in blob
    assert "msg" not in blob  # the seeded guestbook message text


# =============================================================================
# format_digest
# =============================================================================


def test_format_digest_contains_the_expected_pieces(tmp_path):
    conn = mk(tmp_path)
    for i in range(3):
        ins_obs(conn, local_ts(20, 5), pid=i, sender=f"!{i:08x}")
    ins_gb(conn, local_ts(9))
    ins_gb(conn, local_ts(10), visible=0, reason="banned_word")
    ins_req(conn, "!aaaaaaaa", 100, local_ts(9), "acked", command="SIGN")
    ins_req(conn, "!bbbbbbbb", 101, local_ts(9), "failed", command="STATS")

    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(23))
    d["place"] = "Potrero"
    text = format_digest(d)
    assert "2026-09-14" in text
    assert "Potrero" in text
    assert "packets" in text
    assert "busiest 20:00" in text
    assert "2 signatures (1 held)" in text
    assert "1 acked" in text
    assert "1 failed" in text


def test_format_digest_omits_place_when_not_configured(tmp_path):
    conn = mk(tmp_path)
    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(23))
    text = format_digest(d)
    assert "()" not in text


def test_format_digest_never_contains_a_node_id():
    d = {
        "day": DAY,
        "place": "",
        "packets": {"raw": 1, "unique": 1},
        "distinct_senders": 1,
        "new_nodes": 0,
        "busiest_hour": 9,
        "guestbook": {"signatures": 0, "held": 0},
        "replies": {"submitted": 0, "acked": 0, "failed": 0, "expired": 0, "unknown": 0},
        "commands": {"SIGN": 1},
        "drops": None,
        "book_solves": 0,
        "wordle_solves": 0,
    }
    text = format_digest(d)
    assert "!" not in text


# =============================================================================
# default_digest_day
# =============================================================================


def test_default_digest_day_is_yesterday_in_the_configured_timezone():
    now = local_ts(1)  # 2026-09-14 01:00 local
    assert default_digest_day(now, TZ) == "2026-09-13"


def test_default_digest_day_handles_the_utc_offset_boundary():
    # Just before local midnight UTC-shifted: still "today" locally.
    now = local_ts(23, 59)
    assert default_digest_day(now, TZ) == "2026-09-13"


# =============================================================================
# write_digest_file / prune_digests
# =============================================================================


def test_write_digest_file_creates_owner_only_file_and_dir(tmp_path):
    c = cfg(tmp_path)
    conn = mk(tmp_path)
    d = build_digest(conn, day=DAY, tz=TZ, now=local_ts(23))

    path = write_digest_file(c, d)

    digest_dir = os.path.dirname(path)
    assert os.path.basename(path) == f"{DAY}.json"
    assert stat.S_IMODE(os.stat(digest_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with open(path, encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded == d


def test_prune_digests_keeps_90_days(tmp_path):
    c = cfg(tmp_path)
    conn = mk(tmp_path)
    digest_dir = os.path.join(os.path.dirname(c.database.path), "digest")
    os.makedirs(digest_dir, exist_ok=True)

    kept_day = "2026-09-14"
    old_day = "2026-01-01"  # well over 90 days before kept_day
    for day in (kept_day, old_day):
        d = build_digest(conn, day=day, tz=TZ, now=local_ts(23))
        write_digest_file(c, d)

    deleted = prune_digests(c, today=kept_day, keep_days=90)
    assert deleted == 1
    remaining = sorted(os.listdir(digest_dir))
    assert remaining == [f"{kept_day}.json"]
