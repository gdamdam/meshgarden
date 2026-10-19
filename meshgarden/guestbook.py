"""Guestbook: SIGN validation/storage and the GUESTBOOK/STATS formatters.

Explicit publication consent is the SIGN command; an ordinary DM is never an
entry. This module owns normalization, the duplicate-suppression index (a
hash, never the text itself), storage, and the three fixed-format response
builders. It never sends anything and never decides eligibility or rate
limits on its own -- those stay in commands.py/sender.py; main.py wires this
module's pieces together on the DB worker thread.

Nothing here ever logs guestbook text or a raw SIGN argument.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata

from meshgarden.bbs_ids import MESHCORE_PREFIX
from meshgarden.commands import Reject
from meshgarden.config import MAX_PAYLOAD_BYTES, ServiceConfig
from meshgarden.fortune import local_day
from meshgarden.models import GuestbookEntry
from meshgarden.sender import truncate_utf8

_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})

_STORAGE_FULL_TEXT = "🌱 Guestbook is full right now\nNothing was saved · try again later"

# Formatting constants for format_guestbook's shortening algorithm.
_SHORT_NAME_BYTES = 8  # UTF-8 budget for names when an entry needs shortening
_SHORT_NAME_CHARS = 8
_ELLIPSIS = "…"

# Topic shape (ruling 4): "#?[a-z0-9]{2,12}" case-insensitively, stored
# lowercase without a leading "#".
_TOPIC_RE = re.compile(r"^#?[a-z0-9]{2,12}$")


def validate_topic(
    raw: str,
    *,
    max_topics_in_use: int,
    max_topics: int,
    existing_topics: frozenset[str] = frozenset(),
) -> str | Reject:
    """Returns the normalized lowercase topic (no leading '#') or a Reject
    code. Shape: `#?[a-z0-9]{2,12}` case-insensitively. A brand-new topic
    name is refused once max_topics_in_use >= max_topics; posting to an
    EXISTING topic (in `existing_topics`) is never capped by this."""
    candidate = raw.strip().lower()
    if not _TOPIC_RE.match(candidate):
        return Reject.POST_TOPIC_INVALID
    topic = candidate.removeprefix("#")
    if topic not in existing_topics and max_topics_in_use >= max_topics:
        return Reject.POST_TOPIC_LIMIT
    return topic


class SignError(Exception):
    """Raised by Guestbook.validate for a rejected SIGN argument."""

    def __init__(self, reason: Reject) -> None:
        super().__init__(reason.value)
        self.reason = reason


def normalize_sign_text(raw: str) -> str:
    """NFC normalize, strip all Cc/Cf/Cs/Co/Cn characters, collapse whitespace
    runs to a single U+0020, strip ends. Preserves case."""
    s = unicodedata.normalize("NFC", raw)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in _STRIPPED_CATEGORIES)
    return " ".join(s.split())


def message_hash(normalized: str) -> str:
    """sha256 hex of normalized.encode('utf-8'). Used for duplicate detection so
    no message text ever reaches an index or a log."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class Guestbook:
    def __init__(self, conn: sqlite3.Connection, cfg: ServiceConfig) -> None:
        self._conn = conn
        self._cfg = cfg

    def validate(self, raw_argument: str, *, inbound_payload_len: int) -> str:
        """Returns the normalized message or raises SignError with
        SIGN_EMPTY / SIGN_TOO_LONG.

        Order: the raw inbound UTF-8 payload length is checked against the
        tested transport limit BEFORE normalization; then normalize; then
        the (now-empty) check; then the 100-character cap on the normalized
        string.
        """
        if inbound_payload_len > MAX_PAYLOAD_BYTES:
            raise SignError(Reject.SIGN_TOO_LONG)
        normalized = normalize_sign_text(raw_argument)
        if not normalized:
            raise SignError(Reject.SIGN_EMPTY)
        if len(normalized) > self._cfg.max_guestbook_chars:
            raise SignError(Reject.SIGN_TOO_LONG)
        return normalized

    def is_duplicate_sign(
        self,
        sender_node_id: str,
        normalized: str,
        now: int,
        *,
        window_hours: int,
        topic: str | None = None,
    ) -> bool:
        """Only matches VISIBLE entries -- a hidden one (UNSIGN'd, operator-
        hidden, or auto-moderated) no longer blocks re-signing the same
        text. The anti-spam cap on repeat SIGNs is the daily sign quota
        (`daily_sign_quota`), not this rule: this is text-repeat
        suppression, not a rate limit, so it should not keep counting
        text a visitor has since taken back down.

        Re-normalizes `normalized` before hashing (idempotent when the caller
        already ran it through normalize_sign_text/validate): callers must
        never be able to dodge duplicate suppression by passing text that
        differs only in whitespace runs the sanitizer would have collapsed.

        `topic` scopes duplicate suppression per topic (ruling 5): the same
        text posted to a different topic, or to a topic and the main
        guestbook (`topic=None`), is not a duplicate of itself.
        """
        since = now - window_hours * 3600
        row = self._conn.execute(
            "SELECT 1 FROM guestbook WHERE sender_node_id=? AND message_hash=? "
            "AND topic IS ? AND visible=1 AND created_at >= ? LIMIT 1",
            (sender_node_id, message_hash(normalize_sign_text(normalized)), topic, since),
        ).fetchone()
        return row is not None

    def insert(
        self,
        sender_node_id: str,
        display_name: str | None,
        normalized: str,
        now: int,
        *,
        visible: bool = True,
        moderation_reason: str | None = None,
        topic: str | None = None,
    ) -> int:
        """Inserts one entry inside the CALLER's open transaction and
        returns the new entry_id. Does NOT commit -- the caller commits the
        entry and the processed_requests state change together.

        `visible`/`moderation_reason` default to a normal, unmoderated,
        visible entry -- unchanged behavior for existing callers. A SIGN
        that matched the banned-word guard is still stored (never silently
        discarded) but inserted with visible=False and a bounded
        moderation_reason, so the operator can review it later; the
        visitor still gets the normal ack (that choice lives in the
        caller, not here).

        `topic` defaults to None (the original, unchanged main guestbook).
        A non-None topic is a board entry (ruling 5) -- excluded from every
        main-guestbook read unless that read is also given the same topic."""
        cur = self._conn.execute(
            "INSERT INTO guestbook (created_at, sender_node_id, display_name, "
            "message, message_hash, visible, moderation_reason, topic) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                sender_node_id,
                display_name,
                normalized,
                message_hash(normalized),
                int(visible),
                moderation_reason,
                topic,
            ),
        )
        return cur.lastrowid

    def visible_count(self, topic: str | None = None) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM guestbook WHERE visible=1 AND topic IS ?", (topic,)
        ).fetchone()
        return row[0]

    def latest_visible(self, limit: int, topic: str | None = None) -> list[GuestbookEntry]:
        rows = self._conn.execute(
            "SELECT entry_id, created_at, sender_node_id, display_name, message, visible, "
            "moderation_reason, topic, nodes.short_name AS current_short_name, "
            "nodes.long_name AS current_long_name FROM guestbook "
            "LEFT JOIN nodes ON nodes.node_id=bbs_local_id(guestbook.sender_node_id) "
            "WHERE visible=1 AND topic IS ? "
            "ORDER BY entry_id DESC LIMIT ?",
            (topic, limit),
        ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def page_visible(
        self, page: int, per_page: int, topic: str | None = None
    ) -> list[GuestbookEntry]:
        """Page `page` (1 = latest `per_page` visible entries, 2 = the next
        `per_page` older, etc.), newest-first within the page. An empty
        list means `page` is past the end of the visible entries -- the
        caller (main.py) turns that into the "no more entries" reply."""
        offset = (page - 1) * per_page
        rows = self._conn.execute(
            "SELECT entry_id, created_at, sender_node_id, display_name, message, visible, "
            "moderation_reason, topic, nodes.short_name AS current_short_name, "
            "nodes.long_name AS current_long_name FROM guestbook "
            "LEFT JOIN nodes ON nodes.node_id=bbs_local_id(guestbook.sender_node_id) "
            "WHERE visible=1 AND topic IS ? "
            "ORDER BY entry_id DESC LIMIT ? OFFSET ?",
            (topic, per_page, offset),
        ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def get_visible(self, entry_id: int, topic: str | None = None) -> GuestbookEntry | None:
        """One visible entry by id, or None if it does not exist or is
        hidden -- a hidden entry is indistinguishable from a missing one to
        a radio requester, same as every other read here.

        `topic` here is a filter, not a lookup key: `entry_id` alone is
        unique. Passing `topic` returns None if the entry exists but is in
        a different topic (or the main guestbook vs. a board), supporting
        the CLI's cross-topic entry_id guard (ruling 9)."""
        row = self._conn.execute(
            "SELECT entry_id, created_at, sender_node_id, display_name, message, visible, "
            "moderation_reason, topic, nodes.short_name AS current_short_name, "
            "nodes.long_name AS current_long_name FROM guestbook "
            "LEFT JOIN nodes ON nodes.node_id=bbs_local_id(guestbook.sender_node_id) "
            "WHERE visible=1 AND entry_id=? AND topic IS ?",
            (entry_id, topic),
        ).fetchone()
        return _entry_from_row(row) if row is not None else None

    def list_entries(
        self, limit: int, *, include_hidden: bool = False, topic: str | None = None
    ) -> list[GuestbookEntry]:
        """Newest first (entry_id DESC). Visible entries only unless
        `include_hidden` is set, in which case hidden rows are included too
        (still tagged via `GuestbookEntry.visible`). Used by the CLI's
        `guestbook list`; never sends anything and never truncates."""
        where = "topic IS ? " if include_hidden else "visible=1 AND topic IS ? "
        rows = self._conn.execute(
            "SELECT entry_id, created_at, sender_node_id, display_name, message, visible, "
            f"moderation_reason, topic, nodes.short_name AS current_short_name, "
            "nodes.long_name AS current_long_name FROM guestbook "
            "LEFT JOIN nodes ON nodes.node_id=bbs_local_id(guestbook.sender_node_id) "
            f"WHERE {where}"
            "ORDER BY entry_id DESC LIMIT ?",
            (topic, limit),
        ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def topic_counts(self, limit: int = 50) -> list[tuple[str, int]]:
        """Distinct topics with their visible-entry count, count desc then
        topic asc. Never includes the main guestbook (topic IS NULL)."""
        rows = self._conn.execute(
            "SELECT topic, COUNT(*) AS n FROM guestbook WHERE visible=1 AND topic IS NOT NULL "
            "GROUP BY topic ORDER BY n DESC, topic ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(row["topic"], row["n"]) for row in rows]

    def distinct_topics_in_use(self) -> frozenset[str]:
        """ALL topics ever posted to (including hidden entries) -- feeds
        validate_topic's max_topics accounting (ruling 6)."""
        rows = self._conn.execute(
            "SELECT DISTINCT topic FROM guestbook WHERE topic IS NOT NULL"
        ).fetchall()
        return frozenset(row["topic"] for row in rows)

    def hide(self, entry_id: int) -> bool:
        with self._conn:
            cur = self._conn.execute("UPDATE guestbook SET visible=0 WHERE entry_id=?", (entry_id,))
            return cur.rowcount == 1

    def approve(self, entry_id: int) -> bool:
        """Reverses the SIGN banned-word guard's auto-hide: sets visible=1
        and clears moderation_reason. Only matches an entry that is
        currently hidden AND carries a moderation_reason -- a missing
        entry, an already-visible entry, or an entry an operator hid
        directly (`hide`, no moderation_reason) all return False and are
        left untouched. Operator-hidden entries are deliberately not
        reversible here: `approve` only undoes automatic moderation."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE guestbook SET visible=1, moderation_reason=NULL "
                "WHERE entry_id=? AND visible=0 AND moderation_reason IS NOT NULL",
                (entry_id,),
            )
            return cur.rowcount == 1

    def delete(self, entry_id: int) -> bool:
        with self._conn:
            cur = self._conn.execute("DELETE FROM guestbook WHERE entry_id=?", (entry_id,))
            return cur.rowcount == 1

    def hide_own_latest(self, sender_node_id: str, now: int) -> int | None:
        """Hides `sender_node_id`'s own newest still-visible entry (for the
        upcoming UNSIGN command) and returns its entry_id, or None if that
        sender has no visible entry to hide. `now` is accepted for
        signature symmetry with the module's other mutating calls; the
        guestbook table has no per-hide timestamp column to store it in.

        Select-then-update inside one transaction on this connection --
        same pattern as this class's other reads immediately followed by a
        write; unlike quota.py's cross-connection counters, nothing here
        needs to survive a race with a second connection."""
        with self._conn:
            row = self._conn.execute(
                "SELECT entry_id FROM guestbook WHERE sender_node_id=? AND visible=1"
                " ORDER BY entry_id DESC LIMIT 1",
                (sender_node_id,),
            ).fetchone()
            if row is None:
                return None
            entry_id = row["entry_id"]
            self._conn.execute("UPDATE guestbook SET visible=0 WHERE entry_id=?", (entry_id,))
            return entry_id


def format_signed_ack(entry_id: int) -> str:
    return "🌱 Signed!\nThanks! Others read it with GUESTBOOK"


def format_storage_full() -> str:
    return _STORAGE_FULL_TEXT


def format_unsign_hidden(entry_id: int) -> str:
    return "🌱 Your newest note is now hidden\nOperators can still delete it"


def format_unsign_nothing() -> str:
    return "🌱 Nothing to hide"


def format_sign_cooldown(remaining_seconds: int) -> str:
    """SIGN rejected by RateLimiter.check_sign. `remaining_seconds` is
    RateLimiter.sign_cooldown_remaining's already-rounded-up value; shown
    in whole minutes once at least a minute remains, otherwise seconds, so
    a visitor is never told "0 min" while still blocked."""
    if remaining_seconds < 60:
        wait = f"{remaining_seconds} sec"
    else:
        wait = f"{-(-remaining_seconds // 60)} min"  # ceil division, no float rounding
    return f"🌱 Too soon to sign again\nTry again in {wait}"


def format_sign_duplicate(window_hours: int) -> str:
    """SIGN rejected by Guestbook.is_duplicate_sign."""
    return f"🌱 You already signed that\nSame note within {window_hours}h is skipped"


def format_sign_quota() -> str:
    """SIGN rejected by DailyQuota.check_and_count_sign (daily_sign_quota)."""
    return "🌱 Daily sign limit reached\nBack tomorrow · GUESTBOOK to read"


def format_sign_empty(max_chars: int) -> str:
    """SignError(Reject.SIGN_EMPTY) from Guestbook.validate."""
    return f"🌱 Nothing to sign\nSIGN <msg> writes a note ({max_chars} chars)"


def format_sign_too_long(max_chars: int) -> str:
    """SignError(Reject.SIGN_TOO_LONG) from Guestbook.validate."""
    return f"🌱 Too long to sign\n{max_chars} chars max"


def _entry_header(name: str) -> str:
    return f"{name}: "


def fallback_display_name(sender_node_id: str) -> str:
    """Last 4 hex characters of the sender node id, used in place of a
    missing display_name. Derived at format time only -- nothing new is
    stored, and the radio's cached node DB is never consulted. Always <=4
    ASCII characters, so it already fits the short-name budget untouched.

    Shared by the radio reply's GUESTBOOK formatter (`format_guestbook`,
    below) and the CLI's `guestbook list`, so both ever present the exact
    same fallback label for an entry with no stored display name.

    A shared-BBS MeshCore id (`mc:!xxxxxxxx`) yields the same 4-hex tail as
    its bare form."""
    return sender_node_id.removeprefix(MESHCORE_PREFIX).removeprefix("!")[-4:]


def clean_display_name(*names: str | None) -> str | None:
    """First usable name, normalized for safe single-line radio display."""
    for name in names:
        if name:
            cleaned = normalize_sign_text(name)
            if cleaned:
                return cleaned
    return None


def lookup_node_name(conn: sqlite3.Connection, node_id: str | None) -> str | None:
    """Latest known display name; labels never replace IDs for identity decisions."""
    if node_id is None:
        return None
    row = conn.execute(
        "SELECT short_name, long_name FROM nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    return clean_display_name(row["short_name"], row["long_name"]) if row else None


def lookup_bbs_node_name(conn: sqlite3.Connection, stored: str | None) -> str | None:
    """`lookup_node_name` for an id stored in a BBS table: the other mesh's
    ids get no name (see `bbs_ids.LOCAL_ID_SQL`)."""
    if stored is None:
        return None
    row = conn.execute(
        "SELECT short_name, long_name FROM nodes WHERE node_id=bbs_local_id(?)", (stored,)
    ).fetchone()
    return clean_display_name(row["short_name"], row["long_name"]) if row else None


def _entry_from_row(row: sqlite3.Row) -> GuestbookEntry:
    return GuestbookEntry(
        entry_id=row["entry_id"],
        created_at=row["created_at"],
        sender_node_id=row["sender_node_id"],
        display_name=clean_display_name(
            row["current_short_name"], row["current_long_name"], row["display_name"]
        ),
        message=row["message"],
        visible=bool(row["visible"]),
        moderation_reason=row["moderation_reason"],
        topic=row["topic"],
    )


def _guestbook_header(page: int, total_visible: int, shown: int) -> str:
    """Page 1 (the default/latest view) names how many of the total are
    shown here; page >= 2 (an explicit `GUESTBOOK <page>`) names the page
    number instead, since "latest N" would be misleading once paging back
    through older entries.

    Page 1 with nothing ever visible gets an honest "is empty" reply
    instead of the technically-true-but-confusing "latest 0 of 0" -- a
    first-time visitor asking GUESTBOOK should be invited to SIGN, not
    handed a count."""
    if page <= 1 and total_visible == 0:
        return "📖 Guestbook is empty\nSIGN <msg> to be first 🌱"
    if page <= 1:
        return f"📖 Guestbook · latest {shown} of {total_visible}"
    return f"📖 Guestbook · page {page} · {total_visible} total"


def _guestbook_footer(page: int) -> str:
    return f"→ GUESTBOOK {page + 1} for older"


def format_guestbook(
    entries: list[GuestbookEntry],
    max_bytes: int,
    *,
    page: int = 1,
    total_visible: int | None = None,
    has_more: bool = False,
) -> str:
    """Implements the exact truncate-after-formatting-every-field algorithm:
    full names/messages first; then shorten names to <=8 UTF-8 bytes and divide
    the remaining byte budget evenly across excerpts; then drop the oldest
    entry and retry; then a header-only fallback; a final truncate_utf8
    safety net guarantees the result never exceeds max_bytes and never
    splits a codepoint. Never expands into a second packet.

    `total_visible` (all-time visible count, not just this page's `entries`)
    drives the header; it defaults to `len(entries)` for callers that don't
    track it separately. `has_more` (whether a further, older page exists)
    controls whether the "GUESTBOOK <page+1> for older" footer is appended
    -- independent of any entry dropped from THIS page purely to fit the
    byte budget."""
    if total_visible is None:
        total_visible = len(entries)
    if not entries:
        return _guestbook_header(page, total_visible, 0)

    footer = _guestbook_footer(page) if has_more else None
    n = len(entries)
    result = _guestbook_header(page, total_visible, 0)
    while n > 0:
        subset = entries[:n]
        header = _guestbook_header(page, total_visible, n)
        full_names = [e.display_name or fallback_display_name(e.sender_node_id) for e in subset]

        lines = [header] + [
            f"{_entry_header(name)}{e.message}"
            for e, name in zip(subset, full_names, strict=True)
        ]
        if footer:
            lines.append(footer)
        candidate = "\n".join(lines)
        if len(candidate.encode("utf-8")) <= max_bytes:
            result = candidate
            break

        short_names = [
            truncate_utf8(name, _SHORT_NAME_BYTES)[:_SHORT_NAME_CHARS] for name in full_names
        ]

        newlines_total = n + (1 if footer else 0)
        overhead = len(header.encode("utf-8")) + newlines_total
        overhead += sum(
            len(_entry_header(name).encode("utf-8"))
            for e, name in zip(subset, short_names, strict=True)
        )
        if footer:
            overhead += len(footer.encode("utf-8"))
        remaining = max_bytes - overhead
        per_entry = remaining // n if remaining > 0 else 0

        excerpts = []
        for e in subset:
            msg_bytes = e.message.encode("utf-8")
            if len(msg_bytes) <= per_entry:
                excerpts.append(e.message)
                continue
            truncated = truncate_utf8(e.message, per_entry)
            with_ellipsis = truncated + _ELLIPSIS
            if len(with_ellipsis.encode("utf-8")) <= per_entry:
                excerpts.append(with_ellipsis)
            else:
                excerpts.append(truncated)

        shortened_lines = [header] + [
            f"{_entry_header(name)}{excerpt}"
            for e, name, excerpt in zip(subset, short_names, excerpts, strict=True)
        ]
        if footer:
            shortened_lines.append(footer)
        shortened = "\n".join(shortened_lines)
        if len(shortened.encode("utf-8")) <= max_bytes:
            result = shortened
            break

        n -= 1
    else:
        result = _guestbook_header(page, total_visible, 0)

    return truncate_utf8(result, max_bytes)


def format_guestbook_entry(entry: GuestbookEntry, max_bytes: int, tz: str) -> str:
    """`GUESTBOOK #<id>`'s single-entry reply: two lines, `"📖 #12 · Ali ·
    2026-09-13"` then `"<message> 🌱"`. `tz` is the operator-configured IANA
    zone used to render `entry.created_at` as a local calendar day (see
    fortune.local_day). Same truncate_utf8 safety net as every other reply
    -- never splits a codepoint, never exceeds max_bytes."""
    name = entry.display_name or fallback_display_name(entry.sender_node_id)
    day = local_day(entry.created_at, tz)
    text = f"📖 #{entry.entry_id} · {name} · {day}\n{entry.message} 🌱"
    return truncate_utf8(text, max_bytes)


def format_guestbook_not_found(entry_id: int) -> str:
    """`GUESTBOOK #<id>` for a hidden or nonexistent id: the two cases are
    indistinguishable to a radio requester, same as every other read here."""
    return f"📖 #{entry_id} not found 🌱"


def format_guestbook_no_more_entries(total_visible: int) -> str:
    """`GUESTBOOK <page>` past the last page of visible entries."""
    return f"📖 Guestbook\nNo more entries · {total_visible} total 🌱"


def _board_header(topic: str, page: int, total_visible: int, shown: int) -> str:
    """Mirrors _guestbook_header's page-1-empty honesty fix. `topic` is
    always the validate_topic-normalized `[a-z0-9]{2,12}` form by the time
    it reaches here (main.py's `_handle_read` never calls this -- or
    format_board -- for a topic shape it rejected, or one nobody has ever
    posted to), never raw visitor text, so it is safe to echo."""
    if page <= 1 and total_visible == 0:
        return f"📌 #{topic} is empty\nPOST {topic} <msg> to be first 🌱"
    if page <= 1:
        return f"📌 #{topic} · latest {shown} of {total_visible}"
    return f"📌 #{topic} · page {page} · {total_visible} total"


def _board_footer(topic: str, page: int) -> str:
    return f"→ READ {topic} {page + 1} for older"


def format_board(
    entries: list[GuestbookEntry],
    max_bytes: int,
    *,
    topic: str,
    page: int = 1,
    total_visible: int | None = None,
    has_more: bool = False,
) -> str:
    """Same bounded name/excerpt formatting as the main guestbook."""
    if total_visible is None:
        total_visible = len(entries)
    if not entries:
        return _board_header(topic, page, total_visible, 0)

    footer = _board_footer(topic, page) if has_more else None
    n = len(entries)
    result = _board_header(topic, page, total_visible, 0)
    while n > 0:
        subset = entries[:n]
        header = _board_header(topic, page, total_visible, n)
        full_names = [e.display_name or fallback_display_name(e.sender_node_id) for e in subset]
        lines = [header] + [
            f"{name}: {e.message}" for e, name in zip(subset, full_names, strict=True)
        ]
        if footer:
            lines.append(footer)
        candidate = "\n".join(lines)
        if len(candidate.encode("utf-8")) <= max_bytes:
            result = candidate
            break

        short_names = [
            truncate_utf8(name, _SHORT_NAME_BYTES)[:_SHORT_NAME_CHARS] for name in full_names
        ]
        newlines_total = n + (1 if footer else 0)
        overhead = len(header.encode("utf-8")) + newlines_total
        overhead += sum(
            len(f"{name}: ".encode())
            for e, name in zip(subset, short_names, strict=True)
        )
        if footer:
            overhead += len(footer.encode("utf-8"))
        remaining = max_bytes - overhead
        per_entry = remaining // n if remaining > 0 else 0
        excerpts = []
        for e in subset:
            msg_bytes = e.message.encode("utf-8")
            if len(msg_bytes) <= per_entry:
                excerpts.append(e.message)
                continue
            truncated = truncate_utf8(e.message, per_entry)
            with_ellipsis = truncated + _ELLIPSIS
            excerpts.append(
                with_ellipsis if len(with_ellipsis.encode("utf-8")) <= per_entry else truncated
            )
        shortened_lines = [header] + [
            f"{name}: {excerpt}"
            for e, name, excerpt in zip(subset, short_names, excerpts, strict=True)
        ]
        if footer:
            shortened_lines.append(footer)
        shortened = "\n".join(shortened_lines)
        if len(shortened.encode("utf-8")) <= max_bytes:
            result = shortened
            break
        n -= 1
    else:
        result = _board_header(topic, page, total_visible, 0)
    return truncate_utf8(result, max_bytes)


def format_board_no_more_entries(topic: str, total_visible: int) -> str:
    return f"📌 #{topic}\nNo more entries · {total_visible} total 🌱"


def format_uptime(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_stats(
    *,
    senders_24h: int,
    unique_packets_24h: int,
    guestbook_visible: int,
    uptime_seconds: int,
    max_bytes: int,
    companion_events: bool = False,
) -> str:
    """Aggregates only -- no node ids, no names, no message text, no
    per-node history."""
    text = "\n".join(
        (
            "📊 Local 24h",
            f"{senders_24h} senders",
            f"{unique_packets_24h} unique {'events' if companion_events else 'packets'}",
            f"📖 Guestbook {guestbook_visible}",
            f"⏱ Uptime {format_uptime(uptime_seconds)}",
            ("🌱 companion events, not all RF" if companion_events
             else "🌱 counts what this radio heard"),
        )
    )
    return truncate_utf8(text, max_bytes)
