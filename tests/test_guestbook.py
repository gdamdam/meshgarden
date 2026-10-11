import hashlib
import re

import pytest

from meshgarden.commands import Reject
from meshgarden.config import ServiceConfig
from meshgarden.database import connect, migrate
from meshgarden.guestbook import (
    Guestbook,
    SignError,
    format_board,
    format_board_no_more_entries,
    format_guestbook,
    format_guestbook_entry,
    format_guestbook_no_more_entries,
    format_guestbook_not_found,
    format_sign_cooldown,
    format_sign_duplicate,
    format_sign_empty,
    format_sign_quota,
    format_sign_too_long,
    format_signed_ack,
    format_stats,
    format_storage_full,
    format_unsign_hidden,
    format_unsign_nothing,
    format_uptime,
    message_hash,
    normalize_sign_text,
    validate_topic,
)
from meshgarden.models import GuestbookEntry

CFG = ServiceConfig()


def gb(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    return conn, Guestbook(conn, CFG)


def b(s):
    return len(s.encode("utf-8"))


def test_normalization_is_nfc():
    # Controller ruling 1: use a DECOMPOSED input so the assertion actually
    # exercises NFC composition rather than being a no-op on two identical
    # precomposed literals.
    decomposed = "é"  # "e" + COMBINING ACUTE ACCENT
    normalized = normalize_sign_text(decomposed)
    assert normalized == "é"  # precomposed "é"
    assert len(normalized) == 1


def test_case_is_preserved():
    assert normalize_sign_text("Hello World") == "Hello World"


def test_control_characters_are_stripped():
    assert normalize_sign_text("a\x00b\x07c\nd\te") == "abcde"


def test_zero_width_and_bidi_controls_are_stripped():
    assert normalize_sign_text("a​b‮c") == "abc"


def test_whitespace_runs_collapse_and_ends_strip():
    assert normalize_sign_text("  hello    there  ") == "hello there"


def test_empty_after_sanitizing_is_rejected(tmp_path):
    conn, g = gb(tmp_path)
    for raw in ["", "   ", "\x00\x01", "​"]:
        with pytest.raises(SignError) as e:
            g.validate(raw, inbound_payload_len=10)
        assert e.value.reason is Reject.SIGN_EMPTY


def test_over_100_characters_rejected(tmp_path):
    conn, g = gb(tmp_path)
    with pytest.raises(SignError) as e:
        g.validate("x" * 101, inbound_payload_len=110)
    assert e.value.reason is Reject.SIGN_TOO_LONG
    assert g.validate("x" * 100, inbound_payload_len=110) == "x" * 100


def test_character_limit_counts_unicode_characters_not_bytes(tmp_path):
    conn, g = gb(tmp_path)
    assert g.validate("é" * 100, inbound_payload_len=210) == "é" * 100


def test_inbound_payload_over_transport_limit_rejected(tmp_path):
    conn, g = gb(tmp_path)
    with pytest.raises(SignError) as e:
        g.validate("hello", inbound_payload_len=234)
    assert e.value.reason is Reject.SIGN_TOO_LONG


def test_identical_sign_is_duplicate_within_24h(tmp_path):
    conn, g = gb(tmp_path)
    now = 1_700_000_000
    with conn:
        g.insert("!aabbccdd", "Abcd", "hello there", now)
    assert g.is_duplicate_sign("!aabbccdd", "hello there", now + 3600, window_hours=24)
    assert g.is_duplicate_sign("!aabbccdd", "  hello   there ".strip(), now + 3600, window_hours=24)
    assert not g.is_duplicate_sign("!11112222", "hello there", now + 3600, window_hours=24)
    assert not g.is_duplicate_sign("!aabbccdd", "hello there", now + 24 * 3600 + 1, window_hours=24)


def test_hidden_entry_is_no_longer_a_duplicate(tmp_path):
    """UNSIGN frees the slot: a HIDDEN entry (whether via `hide` -- what
    UNSIGN calls under the hood -- or an operator's own `hide`/moderation)
    must not keep blocking a visitor from re-signing the same text, even
    though it is still within the identical_sign_dedup_hours window."""
    conn, g = gb(tmp_path)
    now = 1_700_000_000
    with conn:
        entry_id = g.insert("!aabbccdd", "Abcd", "hello there", now)
    assert g.is_duplicate_sign("!aabbccdd", "hello there", now + 10, window_hours=24)
    g.hide(entry_id)
    assert not g.is_duplicate_sign("!aabbccdd", "hello there", now + 10, window_hours=24)


def test_message_hash_is_sha256_hex_and_stable_and_distinct():
    # Controller ruling 2: assert the actual algorithm (lowercase hex sha256
    # of the normalized UTF-8 text), stability, and distinctness -- not the
    # near-tautological "hash != text".
    text = "hello there"
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert message_hash(text) == expected
    assert message_hash(text) == message_hash(text)
    assert message_hash("something else entirely") != message_hash(text)


def test_message_text_is_not_in_any_index(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        g.insert("!aabbccdd", "Abcd", "secret words", 1)
    row = conn.execute("SELECT message_hash FROM guestbook").fetchone()
    assert "secret" not in row[0]


def test_hidden_entries_are_excluded_from_reads_and_counts(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        a = g.insert("!aabbccdd", "A", "one", 1)
        bb = g.insert("!11112222", "B", "two", 2)
    assert g.visible_count() == 2
    assert g.hide(a) is True
    assert g.visible_count() == 1
    assert [e.entry_id for e in g.latest_visible(3)] == [bb]
    assert conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == 2


def test_delete_removes_the_row(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        a = g.insert("!aabbccdd", "A", "one", 1)
    assert g.delete(a) is True
    assert conn.execute("SELECT count(*) FROM guestbook").fetchone()[0] == 0
    assert g.delete(a) is False


def test_approve_reverses_moderation_hide(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        a = g.insert(
            "!aabbccdd", "A", "flagged text", 1, visible=False, moderation_reason="banned_word"
        )
    assert g.approve(a) is True
    row = conn.execute(
        "SELECT visible, moderation_reason FROM guestbook WHERE entry_id=?", (a,)
    ).fetchone()
    assert row["visible"] == 1
    assert row["moderation_reason"] is None
    assert [e.entry_id for e in g.latest_visible(3)] == [a]


def test_approve_returns_false_for_missing_entry(tmp_path):
    conn, g = gb(tmp_path)
    assert g.approve(999) is False


def test_approve_returns_false_for_already_visible_entry(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        a = g.insert("!aabbccdd", "A", "normal entry", 1)
    assert g.approve(a) is False
    row = conn.execute("SELECT visible FROM guestbook WHERE entry_id=?", (a,)).fetchone()
    assert row["visible"] == 1


def test_approve_returns_false_for_operator_hidden_entry(tmp_path):
    # hide() (operator action, no moderation_reason) must not be reversible
    # by approve() -- approve only undoes automatic moderation.
    conn, g = gb(tmp_path)
    with conn:
        a = g.insert("!aabbccdd", "A", "normal entry", 1)
    assert g.hide(a) is True
    assert g.approve(a) is False
    row = conn.execute(
        "SELECT visible, moderation_reason FROM guestbook WHERE entry_id=?", (a,)
    ).fetchone()
    assert row["visible"] == 0
    assert row["moderation_reason"] is None


def test_signed_ack_format():
    assert format_signed_ack(42) == "🌱 Signed!\nThanks! Others read it with GUESTBOOK"
    assert b(format_signed_ack(999999)) <= 180
    assert re.search(r"#\d+", format_signed_ack(42)) is None


def test_storage_full_format_and_budget():
    t = format_storage_full()
    assert t == "🌱 Guestbook is full right now\nNothing was saved · try again later"
    assert b(t) <= 180


def test_unsign_hidden_format_and_budget():
    t = format_unsign_hidden(42)
    assert t == "🌱 Your newest note is now hidden\nOperators can still delete it"
    assert b(format_unsign_hidden(999999999)) <= 180
    assert re.search(r"#\d+", t) is None


def test_unsign_nothing_format():
    assert format_unsign_nothing() == "🌱 Nothing to hide"


def test_sign_cooldown_format_minutes_and_seconds():
    assert format_sign_cooldown(90) == "🌱 Too soon to sign again\nTry again in 2 min"  # rounds up
    assert format_sign_cooldown(60) == "🌱 Too soon to sign again\nTry again in 1 min"
    assert format_sign_cooldown(59) == "🌱 Too soon to sign again\nTry again in 59 sec"
    assert format_sign_cooldown(0) == "🌱 Too soon to sign again\nTry again in 0 sec"
    assert b(format_sign_cooldown(3600)) <= 180


def test_sign_duplicate_format_and_budget():
    assert (
        format_sign_duplicate(24)
        == "🌱 You already signed that\nSame note within 24h is skipped"
    )
    assert b(format_sign_duplicate(999)) <= 180


def test_sign_quota_format_and_budget():
    t = format_sign_quota()
    assert t == "🌱 Daily sign limit reached\nBack tomorrow · GUESTBOOK to read"
    assert b(t) <= 180


def test_sign_empty_format_and_budget():
    assert (
        format_sign_empty(100) == "🌱 Nothing to sign\nSIGN <msg> writes a note (100 chars)"
    )
    assert b(format_sign_empty(999999)) <= 180


def test_sign_too_long_format_and_budget():
    assert format_sign_too_long(100) == "🌱 Too long to sign\n100 chars max"
    assert b(format_sign_too_long(999999)) <= 180


def ent(i, name, msg):
    return GuestbookEntry(
        entry_id=i,
        created_at=0,
        sender_node_id="!aabbccdd",
        display_name=name,
        message=msg,
        visible=True,
    )


def test_guestbook_response_always_fits_180_bytes():
    entries = [
        ent(999, "VeryLongDisplayName", "x" * 100),
        ent(998, "AnotherLongName", "y" * 100),
        ent(997, "ThirdLongName", "z" * 100),
    ]
    out = format_guestbook(entries, 180)
    assert b(out) <= 180
    out.encode("utf-8").decode("utf-8")


def test_guestbook_listing_never_shows_an_entry_id():
    """Operator decision: entry ids are an internal SQLite autoincrement
    with no meaning to a visitor, so a GUESTBOOK listing line is just
    "<name>: <message>" -- never "#<id> <name>: <message>"."""
    entries = [ent(12345, "Ali", "hello there")]
    out = format_guestbook(entries, 180)
    assert out == "📖 Guestbook · latest 1 of 1\nAli: hello there"
    assert re.search(r"#\d+", out) is None


def test_guestbook_response_fits_with_multibyte_content():
    entries = [
        ent(1, "🌲🌲🌲🌲🌲", "🌲" * 100),
        ent(2, "ünïcödé", "é" * 100),
        ent(3, "日本語名前", "日" * 100),
    ]
    out = format_guestbook(entries, 180)
    assert b(out) <= 180
    out.encode("utf-8").decode("utf-8")


def test_guestbook_returns_fewer_entries_rather_than_overflowing():
    entries = [ent(1000000, "N" * 30, "m" * 100) for _ in range(3)]
    out = format_guestbook(entries, 180)
    assert b(out) <= 180


def test_guestbook_page_header_names_the_page_for_page_two_and_up():
    entries = [ent(5, "N", "hi")]
    out = format_guestbook(entries, 180, page=2, total_visible=50, has_more=False)
    assert out == "📖 Guestbook · page 2 · 50 total\nN: hi"


def test_guestbook_footer_appears_only_when_more_entries_exist():
    entries = [ent(5, "N", "hi")]
    with_more = format_guestbook(entries, 180, page=1, total_visible=10, has_more=True)
    assert with_more.endswith("→ GUESTBOOK 2 for older")
    without_more = format_guestbook(entries, 180, page=1, total_visible=1, has_more=False)
    assert "GUESTBOOK 2 for older" not in without_more


def test_guestbook_worst_case_page_with_footer_fits_the_budget():
    """Byte-budget test: many long names/messages, a large page/total, and
    the "for older" footer all at once."""
    entries = [ent(1_000_000 + i, "N" * 30, "m" * 100) for i in range(5)]
    out = format_guestbook(entries, 180, page=999, total_visible=999_999, has_more=True)
    assert b(out) <= 180
    out.encode("utf-8").decode("utf-8")


def test_empty_guestbook_response():
    assert format_guestbook([], 180) == "📖 Guestbook is empty\nSIGN <msg> to be first 🌱"


def test_empty_guestbook_response_on_an_explicit_later_page_stays_the_page_header():
    """The honest "is empty" text only replaces page 1's own "latest 0 of
    0" -- an explicit later page with total_visible == 0 (not reachable
    from main.py's own dispatch, but format_guestbook's contract for any
    caller) still names the page, since "is empty" wouldn't make sense
    worded as page 2+."""
    out = format_guestbook([], 180, page=2, total_visible=0)
    assert out == "📖 Guestbook · page 2 · 0 total"


def test_missing_display_name_falls_back_to_last_four_hex_chars_of_node_id():
    """No display_name was captured for this entry (e.g. a NODEINFO exchange
    never happened) -- the reply must show the last 4 hex characters of the
    sender's node id, never a bare `?`, and must not consult any cached node
    DB (there is nothing to derive it from but sender_node_id itself)."""
    entries = [
        GuestbookEntry(
            entry_id=1,
            created_at=0,
            sender_node_id="!123494ba",
            display_name=None,
            message="hellio",
            visible=True,
        )
    ]
    out = format_guestbook(entries, 180)
    assert out == "📖 Guestbook · latest 1 of 1\n94ba: hellio"
    assert "?" not in out


def test_missing_display_name_fallback_survives_the_short_name_path():
    """Same fallback, but with entries long enough to force the <=4-char
    short-name shortening path -- the 4 hex characters already fit that
    budget untouched, so they must appear verbatim, not `?`."""
    entries = [
        GuestbookEntry(
            entry_id=1,
            created_at=0,
            sender_node_id="!123494ba",
            display_name=None,
            message="m" * 100,
            visible=True,
        ),
        ent(2, "AnotherLongName", "n" * 100),
    ]
    out = format_guestbook(entries, 180)
    assert b(out) <= 180
    assert "94ba" in out
    assert "?" not in out


def test_stats_is_aggregate_only_and_fits():
    s = format_stats(
        senders_24h=24,
        unique_packets_24h=350,
        guestbook_visible=17,
        uptime_seconds=3 * 86400 + 8 * 3600,
        max_bytes=180,
    )
    assert b(s) <= 180
    assert s.startswith("📊 Local 24h")
    assert "24 senders\n350 unique packets" in s
    assert "📖 Guestbook 17" in s
    assert "⏱ Uptime 3d 8h" in s
    assert "!" not in s  # no node ids


def test_uptime_formatting():
    assert format_uptime(45) == "45s"
    assert format_uptime(3 * 86400 + 8 * 3600) == "3d 8h"
    assert format_uptime(8 * 3600 + 5 * 60) == "8h 5m"


# --- GUESTBOOK pagination: page_visible / get_visible -------------------------


def test_page_visible_page_one_matches_latest_visible(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        for i in range(5):
            g.insert("!aabbccdd", "N", f"msg{i}", i)
    assert [e.entry_id for e in g.page_visible(1, 3)] == [
        e.entry_id for e in g.latest_visible(3)
    ]


def test_page_visible_page_two_returns_the_next_older_entries(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        ids = [g.insert("!aabbccdd", "N", f"msg{i}", i) for i in range(5)]
    # newest-first: ids[4], ids[3], ids[2] | ids[1], ids[0]
    page1 = [e.entry_id for e in g.page_visible(1, 3)]
    page2 = [e.entry_id for e in g.page_visible(2, 3)]
    assert page1 == [ids[4], ids[3], ids[2]]
    assert page2 == [ids[1], ids[0]]


def test_page_visible_past_the_end_is_empty(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        g.insert("!aabbccdd", "N", "one", 1)
    assert g.page_visible(2, 3) == []


def test_page_visible_excludes_hidden_entries(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        a = g.insert("!aabbccdd", "N", "one", 1)
        g.insert("!aabbccdd", "N", "two", 2)
    g.hide(a)
    entries = g.page_visible(1, 10)
    assert a not in [e.entry_id for e in entries]


def test_get_visible_returns_the_entry(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        entry_id = g.insert("!aabbccdd", "Ali", "hello there", 1)
    entry = g.get_visible(entry_id)
    assert entry is not None
    assert entry.entry_id == entry_id
    assert entry.message == "hello there"


def test_get_visible_returns_none_for_a_hidden_entry(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        entry_id = g.insert("!aabbccdd", "N", "one", 1)
    g.hide(entry_id)
    assert g.get_visible(entry_id) is None


def test_get_visible_returns_none_for_a_missing_entry(tmp_path):
    conn, g = gb(tmp_path)
    assert g.get_visible(999) is None


# --- GUESTBOOK pagination: formatters ------------------------------------------


def test_format_guestbook_entry_matches_the_documented_shape():
    entry = GuestbookEntry(
        entry_id=12,
        created_at=1_757_808_000,  # 2025-09-14T00:00:00Z
        sender_node_id="!aabbccdd",
        display_name="Ali",
        message="hello there",
        visible=True,
    )
    assert format_guestbook_entry(entry, 180, "UTC") == (
        "📖 #12 · Ali · 2025-09-14\nhello there 🌱"
    )


def test_format_guestbook_entry_falls_back_to_last_four_hex_chars():
    entry = GuestbookEntry(
        entry_id=12,
        created_at=1_757_808_000,
        sender_node_id="!123494ba",
        display_name=None,
        message="hi",
        visible=True,
    )
    assert format_guestbook_entry(entry, 180, "UTC") == "📖 #12 · 94ba · 2025-09-14\nhi 🌱"


def test_format_guestbook_entry_stays_within_the_byte_budget():
    entry = ent(999999, "VeryLongDisplayName", "x" * 150)
    out = format_guestbook_entry(entry, 180, "UTC")
    assert b(out) <= 180
    out.encode("utf-8").decode("utf-8")


def test_format_guestbook_not_found():
    assert format_guestbook_not_found(12) == "📖 #12 not found 🌱"
    assert b(format_guestbook_not_found(999999999999999999)) <= 180


def test_format_guestbook_no_more_entries():
    assert format_guestbook_no_more_entries(7) == "📖 Guestbook\nNo more entries · 7 total 🌱"
    assert b(format_guestbook_no_more_entries(999999999)) <= 180

# --- insert: backward-compatible visible/moderation_reason extension -----------


def test_insert_defaults_to_visible_with_no_moderation_reason(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        entry_id = g.insert("!aabbccdd", "A", "one", 1)
    row = conn.execute(
        "SELECT visible, moderation_reason FROM guestbook WHERE entry_id=?", (entry_id,)
    ).fetchone()
    assert row["visible"] == 1
    assert row["moderation_reason"] is None


def test_insert_hidden_with_moderation_reason(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        entry_id = g.insert(
            "!aabbccdd", "A", "one", 1, visible=False, moderation_reason="banned_word"
        )
    row = conn.execute(
        "SELECT visible, moderation_reason FROM guestbook WHERE entry_id=?", (entry_id,)
    ).fetchone()
    assert row["visible"] == 0
    assert row["moderation_reason"] == "banned_word"
    # A hidden moderated entry is not returned by visible reads.
    assert g.visible_count() == 0
    assert g.latest_visible(3) == []


def test_list_entries_exposes_moderation_reason_for_hidden_rows(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        visible_id = g.insert("!aabbccdd", "A", "one", 1)
        hidden_id = g.insert(
            "!11112222", "B", "two", 2, visible=False, moderation_reason="banned_word"
        )
    visible_only = g.list_entries(10)
    assert [e.entry_id for e in visible_only] == [visible_id]
    assert visible_only[0].moderation_reason is None

    all_entries = g.list_entries(10, include_hidden=True)
    by_id = {e.entry_id: e for e in all_entries}
    assert by_id[visible_id].moderation_reason is None
    assert by_id[hidden_id].visible is False
    assert by_id[hidden_id].moderation_reason == "banned_word"


# --- hide_own_latest -------------------------------------------------------------


def test_hide_own_latest_hides_the_senders_newest_visible_entry(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        first = g.insert("!aabbccdd", "A", "one", 1)
        second = g.insert("!aabbccdd", "A", "two", 2)
    hidden_id = g.hide_own_latest("!aabbccdd", 3)
    assert hidden_id == second
    assert g.visible_count() == 1
    assert [e.entry_id for e in g.latest_visible(3)] == [first]


def test_hide_own_latest_returns_none_when_sender_has_no_visible_entries(tmp_path):
    conn, g = gb(tmp_path)
    assert g.hide_own_latest("!aabbccdd", 1) is None


def test_hide_own_latest_ignores_other_senders_entries(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        g.insert("!11112222", "B", "other", 1)
    assert g.hide_own_latest("!aabbccdd", 2) is None
    assert g.visible_count() == 1


def test_hide_own_latest_ignores_already_hidden_entries(tmp_path):
    conn, g = gb(tmp_path)
    with conn:
        a = g.insert("!aabbccdd", "A", "one", 1)
    g.hide(a)
    assert g.hide_own_latest("!aabbccdd", 2) is None


# --- topic boards -----------------------------------------------------------------


def test_insert_with_a_topic_is_excluded_from_the_main_guestbook(tmp_path):
    conn, g = gb(tmp_path)
    g.insert("!aabbccdd", None, "hello", 1000, topic="bike")
    assert g.visible_count() == 0
    assert g.visible_count(topic="bike") == 1


def test_duplicate_suppression_is_scoped_per_topic(tmp_path):
    conn, g = gb(tmp_path)
    g.insert("!aabbccdd", None, "same text", 1000, topic="bike")
    assert (
        g.is_duplicate_sign("!aabbccdd", "same text", 1001, window_hours=24, topic="bike") is True
    )
    assert (
        g.is_duplicate_sign("!aabbccdd", "same text", 1001, window_hours=24, topic="lost")
        is False
    )
    assert g.is_duplicate_sign("!aabbccdd", "same text", 1001, window_hours=24) is False  # main gb


def test_get_visible_with_a_topic_filter_rejects_a_mismatched_entry(tmp_path):
    conn, g = gb(tmp_path)
    entry_id = g.insert("!aabbccdd", None, "hello", 1000, topic="bike")
    assert g.get_visible(entry_id, topic="lost") is None
    assert g.get_visible(entry_id, topic="bike") is not None
    assert g.get_visible(entry_id) is None  # main-guestbook filter also excludes it


def test_page_visible_and_list_entries_are_scoped_to_the_given_topic(tmp_path):
    conn, g = gb(tmp_path)
    g.insert("!aabbccdd", None, "main entry", 1)
    g.insert("!aabbccdd", None, "bike entry", 2, topic="bike")
    assert [e.entry_id for e in g.page_visible(1, 10, topic="bike")] == [
        e.entry_id for e in g.latest_visible(10, topic="bike")
    ]
    assert [e.message for e in g.page_visible(1, 10, topic="bike")] == ["bike entry"]
    assert [e.message for e in g.list_entries(10, topic="bike")] == ["bike entry"]
    assert [e.message for e in g.list_entries(10)] == ["main entry"]


def test_topic_counts_orders_by_count_desc_then_name(tmp_path):
    conn, g = gb(tmp_path)
    g.insert("!aabbccdd", None, "a", 1000, topic="bike")
    g.insert("!aabbccdd", None, "b", 1001, topic="bike")
    g.insert("!aabbccdd", None, "c", 1002, topic="lost")
    assert g.topic_counts() == [("bike", 2), ("lost", 1)]


def test_topic_counts_never_includes_the_main_guestbook(tmp_path):
    conn, g = gb(tmp_path)
    g.insert("!aabbccdd", None, "main entry", 1)
    assert g.topic_counts() == []


def test_distinct_topics_in_use_includes_hidden_entries(tmp_path):
    conn, g = gb(tmp_path)
    hidden_id = g.insert("!aabbccdd", None, "a", 1, topic="bike")
    g.insert("!aabbccdd", None, "b", 2, topic="lost")
    g.hide(hidden_id)
    assert g.distinct_topics_in_use() == frozenset({"bike", "lost"})


def test_validate_topic_accepts_the_documented_shape():
    assert validate_topic("#Bike", max_topics_in_use=1, max_topics=50) == "bike"
    assert validate_topic("lost99", max_topics_in_use=1, max_topics=50) == "lost99"


def test_validate_topic_rejects_bad_shape():
    assert validate_topic("a", max_topics_in_use=1, max_topics=50) == Reject.POST_TOPIC_INVALID
    assert validate_topic("x" * 13, max_topics_in_use=1, max_topics=50) == Reject.POST_TOPIC_INVALID
    assert validate_topic("bike!", max_topics_in_use=1, max_topics=50) == Reject.POST_TOPIC_INVALID


def test_validate_topic_enforces_the_cap_only_for_brand_new_topics():
    assert (
        validate_topic("newtopic", max_topics_in_use=50, max_topics=50, existing_topics={"bike"})
        == Reject.POST_TOPIC_LIMIT
    )
    assert (
        validate_topic("bike", max_topics_in_use=50, max_topics=50, existing_topics={"bike"})
        == "bike"
    )


def test_format_board_matches_the_guestbook_shrink_algorithm():
    entries = [
        GuestbookEntry(1, 1000, "!aabbccdd", "fndr", "lost my dog", True, None, topic="bike")
    ]
    text = format_board(entries, 180, topic="bike", page=1, total_visible=1, has_more=False)
    assert text.startswith("📌 #bike")
    assert b(text) <= 180


def test_format_board_listing_never_shows_an_entry_id():
    """Same rule as format_guestbook: the "#topic" prefix stays (it names
    the board), but no per-entry "#<id>" ever appears."""
    entries = [
        GuestbookEntry(12345, 1000, "!aabbccdd", "Ali", "lost my dog", True, None, topic="bike")
    ]
    text = format_board(entries, 180, topic="bike", page=1, total_visible=1, has_more=False)
    assert text == "📌 #bike · latest 1 of 1\nAli: lost my dog"
    assert re.search(r"#\d+", text) is None


def test_format_board_empty_entries_shows_the_honest_empty_reply():
    text = format_board([], 180, topic="bike", page=1, total_visible=0)
    assert text == "📌 #bike is empty\nPOST bike <msg> to be first 🌱"


def test_format_board_page_header_names_the_page_for_page_two_and_up():
    entries = [GuestbookEntry(1, 1000, "!aabbccdd", "fndr", "hi", True, None, topic="bike")]
    text = format_board(entries, 180, topic="bike", page=2, total_visible=5, has_more=True)
    assert text.startswith("📌 #bike · page 2 · 5 total")
    assert "→ READ bike 3 for older" in text


def test_format_board_no_more_entries():
    assert format_board_no_more_entries("bike", 7) == "📌 #bike\nNo more entries · 7 total 🌱"
    assert b(format_board_no_more_entries("x" * 12, 999999999)) <= 180
