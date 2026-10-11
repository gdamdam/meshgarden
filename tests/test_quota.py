import threading

from meshgarden import database
from meshgarden.quota import DailyQuota

DAY = "2026-09-13"


def _db(tmp_path):
    conn = database.connect(str(tmp_path / "o.db"))
    database.migrate(conn)
    return conn


# --- migration -----------------------------------------------------------------


def test_migration_creates_node_daily_quota_on_a_fresh_db(tmp_path):
    conn = _db(tmp_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "node_daily_quota" in names
    assert database.get_schema_version(conn) == database.SCHEMA_VERSION


def test_migration_adds_moderation_reason_column_to_guestbook(tmp_path):
    conn = _db(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(guestbook)")}
    assert "moderation_reason" in cols


def test_migration_applies_atomically_onto_an_existing_migration_2_db(tmp_path):
    p = str(tmp_path / "o.db")
    conn = database.connect(p)
    script = (
        f"BEGIN;\n{database.MIGRATION_1_SQL}\n{database.MIGRATION_2_SQL}\n"
        "PRAGMA user_version = 2;\nCOMMIT;"
    )
    conn.executescript(script)
    assert database.migrate(conn) == database.SCHEMA_VERSION
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "node_daily_quota" in names
    cols = {r[1] for r in conn.execute("PRAGMA table_info(guestbook)")}
    assert "moderation_reason" in cols


def test_migration_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    assert database.migrate(conn) == database.SCHEMA_VERSION


def test_node_daily_quota_columns_are_exactly_spec(tmp_path):
    conn = _db(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(node_daily_quota)")}
    assert cols == {"node_id", "day", "commands", "signs", "posts", "mails_sent", "updated_at"}


# --- check_and_count_command ----------------------------------------------------


def test_command_quota_allows_up_to_the_limit_then_refuses(tmp_path):
    conn = _db(tmp_path)
    q = DailyQuota(conn, command_limit=60, sign_limit=3)
    for i in range(59):
        assert q.check_and_count_command("!aabbccdd", DAY, 1000 + i) is True
    # 60th accepted command
    assert q.check_and_count_command("!aabbccdd", DAY, 2000) is True
    # 61st refused
    assert q.check_and_count_command("!aabbccdd", DAY, 3000) is False
    row = conn.execute(
        "SELECT commands FROM node_daily_quota WHERE node_id=? AND day=?", ("!aabbccdd", DAY)
    ).fetchone()
    assert row["commands"] == 60


def test_command_quota_does_not_count_a_refused_attempt(tmp_path):
    conn = _db(tmp_path)
    q = DailyQuota(conn, command_limit=1, sign_limit=3)
    assert q.check_and_count_command("!aabbccdd", DAY, 1000) is True
    assert q.check_and_count_command("!aabbccdd", DAY, 1001) is False
    assert q.check_and_count_command("!aabbccdd", DAY, 1002) is False
    row = conn.execute(
        "SELECT commands FROM node_daily_quota WHERE node_id=? AND day=?", ("!aabbccdd", DAY)
    ).fetchone()
    assert row["commands"] == 1


def test_command_quota_is_scoped_per_node(tmp_path):
    conn = _db(tmp_path)
    q = DailyQuota(conn, command_limit=1, sign_limit=3)
    assert q.check_and_count_command("!aaaaaaaa", DAY, 1000) is True
    assert q.check_and_count_command("!bbbbbbbb", DAY, 1000) is True
    assert q.check_and_count_command("!aaaaaaaa", DAY, 1001) is False


def test_command_quota_resets_on_a_new_day(tmp_path):
    conn = _db(tmp_path)
    q = DailyQuota(conn, command_limit=1, sign_limit=3)
    assert q.check_and_count_command("!aabbccdd", DAY, 1000) is True
    assert q.check_and_count_command("!aabbccdd", DAY, 1001) is False
    assert q.check_and_count_command("!aabbccdd", "2026-09-14", 90000) is True


# --- check_and_count_sign --------------------------------------------------------


def test_sign_quota_allows_three_then_refuses_the_fourth(tmp_path):
    conn = _db(tmp_path)
    q = DailyQuota(conn, command_limit=60, sign_limit=3)
    assert q.check_and_count_sign("!aabbccdd", DAY, 1000) is True
    assert q.check_and_count_sign("!aabbccdd", DAY, 1001) is True
    assert q.check_and_count_sign("!aabbccdd", DAY, 1002) is True
    assert q.check_and_count_sign("!aabbccdd", DAY, 1003) is False
    row = conn.execute(
        "SELECT signs FROM node_daily_quota WHERE node_id=? AND day=?", ("!aabbccdd", DAY)
    ).fetchone()
    assert row["signs"] == 3


def test_sign_quota_of_zero_refuses_every_sign(tmp_path):
    conn = _db(tmp_path)
    q = DailyQuota(conn, command_limit=60, sign_limit=0)
    assert q.check_and_count_sign("!aabbccdd", DAY, 1000) is False


def test_sign_and_command_quota_counters_are_independent(tmp_path):
    conn = _db(tmp_path)
    q = DailyQuota(conn, command_limit=60, sign_limit=3)
    q.check_and_count_command("!aabbccdd", DAY, 1000)
    q.check_and_count_sign("!aabbccdd", DAY, 1001)
    row = conn.execute(
        "SELECT commands, signs FROM node_daily_quota WHERE node_id=? AND day=?",
        ("!aabbccdd", DAY),
    ).fetchone()
    assert (row["commands"], row["signs"]) == (1, 1)


# --- atomicity under two connections ---------------------------------------------


def test_command_quota_is_atomic_under_two_connections(tmp_path):
    path = str(tmp_path / "o.db")
    conn_setup = database.connect(path)
    database.migrate(conn_setup)
    conn_setup.close()

    # Each thread opens its OWN connection to the same file -- this is the
    # real concurrency model (daemon worker thread vs. a separate CLI
    # process), and sqlite3 connections are not shareable across threads
    # (check_same_thread=True, per database.connect's docstring).
    results = []
    lock = threading.Lock()

    def worker(offset):
        conn = database.connect(path)
        try:
            q = DailyQuota(conn, command_limit=100, sign_limit=3)
            for i in range(100):
                r = q.check_and_count_command("!aabbccdd", DAY, 1000 + offset + i)
                with lock:
                    results.append(r)
        finally:
            conn.close()

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(100000,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    accepted = sum(1 for r in results if r)
    assert accepted == 100

    conn_check = database.connect(path)
    row = conn_check.execute(
        "SELECT commands FROM node_daily_quota WHERE node_id=? AND day=?", ("!aabbccdd", DAY)
    ).fetchone()
    assert row["commands"] == 100


# --- usage (read-only) -------------------------------------------------------------


def test_usage_is_zero_zero_before_any_activity(tmp_path):
    conn = _db(tmp_path)
    q = DailyQuota(conn, command_limit=60, sign_limit=3)
    assert q.usage("!aabbccdd", DAY) == (0, 0)
    # Never inserts a row just by being asked.
    row = conn.execute(
        "SELECT 1 FROM node_daily_quota WHERE node_id=? AND day=?", ("!aabbccdd", DAY)
    ).fetchone()
    assert row is None


def test_usage_reflects_commands_and_signs_counted_so_far(tmp_path):
    conn = _db(tmp_path)
    q = DailyQuota(conn, command_limit=60, sign_limit=3)
    q.check_and_count_command("!aabbccdd", DAY, 1000)
    q.check_and_count_command("!aabbccdd", DAY, 1001)
    q.check_and_count_sign("!aabbccdd", DAY, 1002)
    assert q.usage("!aabbccdd", DAY) == (2, 1)


def test_usage_is_scoped_per_node_and_day(tmp_path):
    conn = _db(tmp_path)
    q = DailyQuota(conn, command_limit=60, sign_limit=3)
    q.check_and_count_command("!aaaaaaaa", DAY, 1000)
    assert q.usage("!bbbbbbbb", DAY) == (0, 0)
    assert q.usage("!aaaaaaaa", "2026-09-14") == (0, 0)


# --- prune -------------------------------------------------------------------------


def test_prune_deletes_rows_older_than_keep_days(tmp_path):
    conn = _db(tmp_path)
    q = DailyQuota(conn, command_limit=60, sign_limit=3)
    now = 1_000_000
    q.check_and_count_command("!aaaaaaaa", "2026-09-01", now - 3 * 86400)
    q.check_and_count_command("!bbbbbbbb", "2026-09-13", now - 3600)
    deleted = q.prune(now, keep_days=2)
    assert deleted == 1
    remaining = {r[0] for r in conn.execute("SELECT node_id FROM node_daily_quota")}
    assert remaining == {"!bbbbbbbb"}


def test_prune_runs_in_bounded_batches(tmp_path):
    conn = _db(tmp_path)
    now = 1_000_000
    for i in range(1200):
        conn.execute(
            "INSERT INTO node_daily_quota(node_id, day, commands, signs, updated_at)"
            " VALUES (?,?,?,?,?)",
            (f"!{i:08x}", "2026-09-01", 1, 0, now - 3 * 86400),
        )
    conn.commit()
    q = DailyQuota(conn, command_limit=60, sign_limit=3)
    deleted = q.prune(now, keep_days=2, batch=500)
    assert deleted == 1200
    assert conn.execute("SELECT count(*) FROM node_daily_quota").fetchone()[0] == 0


def test_prune_keeps_rows_within_keep_days(tmp_path):
    conn = _db(tmp_path)
    q = DailyQuota(conn, command_limit=60, sign_limit=3)
    now = 1_000_000
    q.check_and_count_command("!aaaaaaaa", DAY, now - 1 * 86400)
    deleted = q.prune(now, keep_days=2)
    assert deleted == 0


# --- check_and_count_post / check_and_count_mail_sent -----------------------------


def test_check_and_count_post_and_mail_sent_respect_their_own_limits(tmp_path):
    conn = database.connect(str(tmp_path / "o.db"))
    database.migrate(conn)
    q = DailyQuota(
        conn, command_limit=60, sign_limit=3, post_limit=2, mail_sent_limit=1
    )
    day = "2026-09-14"
    assert q.check_and_count_post("!11223344", day, 1000) is True
    assert q.check_and_count_post("!11223344", day, 1001) is True
    assert q.check_and_count_post("!11223344", day, 1002) is False  # 3rd refused
    assert q.check_and_count_mail_sent("!11223344", day, 1000) is True
    assert q.check_and_count_mail_sent("!11223344", day, 1001) is False  # 2nd refused
    conn.close()
