import sqlite3

import pytest

from meshgarden.community import (
    NEWS_SQL,
    add_news,
    delete_news,
    guide_text,
    hide_news,
    list_news,
    news_text,
)


def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(NEWS_SQL)
    return conn


def test_news_insert_list_hide_and_expiry():
    conn = db()
    with conn:
        first = add_news(conn, "Meetup tonight", 10)
        second = add_news(conn, "Old notice", 9, expires_at=20)
    assert [r["id"] for r in list_news(conn, 19)] == [first, second]
    assert [r["id"] for r in list_news(conn, 20)] == [first]
    assert hide_news(conn, first)
    assert not hide_news(conn, first)
    assert list_news(conn, 0)[0]["id"] == second


def test_delete_news_removes_the_row_unlike_hide():
    """`hide_news` leaves the row on disk (nothing in the tree purges
    this table); `delete_news` is the escape hatch that actually removes
    it, including a notice already hidden, and reports a miss rather
    than silently succeeding twice."""
    conn = db()
    with conn:
        hidden = add_news(conn, "Retired notice", 10)
        kept = add_news(conn, "Meetup tonight", 11)
    assert hide_news(conn, hidden)
    assert conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 2

    with conn:
        assert delete_news(conn, hidden) is True
    assert [r[0] for r in conn.execute("SELECT id FROM news")] == [kept]
    assert delete_news(conn, hidden) is False  # already gone
    assert [r["id"] for r in list_news(conn, 0)] == [kept]


def test_news_text_is_compact_and_paged():
    conn = db()
    with conn:
        for i in range(4):
            add_news(conn, f"notice {i}", i)
    assert "→ NEWS 2" in news_text(conn, page=1, now=0, max_bytes=120)
    assert "notice 2" in news_text(conn, page=2, now=0, max_bytes=120)
    assert len(news_text(conn, max_bytes=30).encode()) <= 30


def test_news_validation():
    conn = db()
    with pytest.raises(ValueError):
        add_news(conn, "  ", 1)
    with pytest.raises(ValueError):
        add_news(conn, "x" * 85, 1)
    with pytest.raises(ValueError):
        add_news(conn, "x", 10, expires_at=10)


def test_news_default_expiry_is_seven_days():
    conn = db()
    with conn:
        ident = add_news(conn, "temporary", 100)
    row = conn.execute("SELECT expires_at FROM news WHERE id=?", (ident,)).fetchone()
    assert row[0] == 100 + 7 * 24 * 60 * 60


def test_guide_topics_and_byte_budget():
    for topic in ("local", "start", "radio", "#channels", "regions", "source", "unknown"):
        reply = guide_text(topic, 40)
        assert len(reply.encode()) <= 40
    assert "910.525" in guide_text("radio", 120)
    assert "2026-09-20" in guide_text("source", 120)


def test_radio_copy_has_readable_line_layouts():
    conn = db()
    with conn:
        add_news(conn, "Meetup tonight", 1)
    assert news_text(conn, now=1).splitlines() == ["📰 News #1", "Meetup tonight"]
    assert guide_text("radio").splitlines() == [
        "📻 Bay Area radio",
        "910.525MHz · BW 62.5kHz · SF7 · CR5",
        "Source: 2026-09-20",
    ]
    assert "bayareameshcore.com" in guide_text("tools")


def test_max_size_news_preserves_notice_when_decoration_would_not_fit():
    conn = db()
    with conn:
        add_news(conn, "x" * 84, 1)
        add_news(conn, "newer", 2)
    reply = news_text(conn, page=2, now=2, max_bytes=84)
    assert reply == "x" * 84
    assert len(reply.encode("utf-8")) == 84
