"""Inbound eligibility gate, command parsing, and persistent request dedup.

This is the first half of the reply policy: it decides whether an
inbound packet may ever produce a reply (`check_eligibility`), what command
it names (`parse_command`), and whether it has already been handled
(`RequestLog`, backed by `processed_requests`). Nothing here sends anything;
the single send path is a later task.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from meshgarden import exchange_log
from meshgarden.models import IncomingPacket
from meshgarden.transport import (
    BROADCAST,
    ChannelScope,
    ChannelTriggerPolicy,
    Inbound,
    NodeId,
)

BROADCAST_NUM = 0xFFFFFFFF


class Command(StrEnum):
    """Enum values name reply *behaviors*, not the words a visitor typed.

    Bare `HELP`, `SOS`, and `EMERGENCY` are three different input words that
    all resolve to the single `NOT_EMERGENCY` behavior (one fixed disclaimer
    reply) -- someone reading `Command.NOT_EMERGENCY` in a log or a test
    should not have to ask whether it means the menu or the distress-word
    reply. `MENU` (with `?` as an input alias, same command) is the menu
    behavior instead.

    `CMD_HELP` is a distinct behavior from either of those: `HELP <cmd>` and
    `? <cmd>` (an argument matching a known command word) return that
    command's one-line usage via `help_text`, never the not-emergency
    disclaimer or the menu.
    """

    MENU = "MENU"
    NEW = "NEW"
    NEWS = "NEWS"
    GUIDE = "GUIDE"
    CHECK = "CHECK"
    WX = "WX"
    REPLY = "REPLY"
    SUGGEST = "SUGGEST"
    NOT_EMERGENCY = "NOT_EMERGENCY"
    INFO = "INFO"
    SIGN = "SIGN"
    GUESTBOOK = "GUESTBOOK"
    STATS = "STATS"
    REPEATER = "REPEATER"
    ICHING = "ICHING"
    HAIKU = "HAIKU"
    # Not a visitor command: labels the processed_requests row a weather
    # broadcast claims so `Sender`'s "originating request must be queued"
    # guard holds for it too. Never dispatched from inbound text.
    WEATHER = "WEATHER"
    BOOK = "BOOK"
    WORDLE = "WORDLE"
    GARDEN = "GARDEN"
    RECORD = "RECORD"
    WEEK = "WEEK"
    UNSIGN = "UNSIGN"
    SEEN = "SEEN"
    WHO = "WHO"
    ME = "ME"
    PING = "PING"
    PATH = "PATH"
    MOON = "MOON"
    SUN = "SUN"
    POST = "POST"
    READ = "READ"
    BOARDS = "BOARDS"
    MAIL = "MAIL"
    LAB_UNLOCK = "LAB_UNLOCK"
    # A MeshCore app location share ("Saved position: lat, lon" / "GPS
    # position: lat, lon") sent as a DM. Only the two coordinates are
    # kept (dm_positions); the raw text is never stored.
    POSITION = "POSITION"
    CMD_HELP = "CMD_HELP"
    VERSION = "VERSION"
    # Optional command modules (meshgarden.modules). CUSTOM labels every
    # operator-defined information command, so metric cardinality stays fixed.
    FIND = "FIND"
    FORECAST = "FORECAST"
    CUSTOM = "CUSTOM"
    LINK = "LINK"
    UNKNOWN = "UNKNOWN"
    # A bookkeeping label ONLY (task 3B-3) -- `parse_command` never produces
    # it, and it is never passed to `Daemon._dispatch_command`. It exists
    # solely so a channel knock's reply can claim a `processed_requests` row
    # (see `RequestLog.claim`) and thereby reuse `Sender`'s existing
    # queueing/truncation/ack-correlation machinery exactly like every real
    # command's reply does. A knock reaching the command dispatcher is not
    # merely prevented -- there is no code path that could ever hand this
    # value to `_dispatch_command`'s `if parsed.command == ...` chain, since
    # nothing constructs a `ParsedCommand(Command.KNOCK, ...)` anywhere.
    KNOCK = "KNOCK"


class Reject(StrEnum):
    """Bounded reason codes. These are the ONLY strings ever logged or counted."""

    NOT_TEXT = "not_text"
    NO_DECODED = "no_decoded"
    NO_TEXT = "no_text"
    SELF_ORIGIN = "self_origin"
    BROADCAST = "broadcast"
    NOT_FOR_US = "not_for_us"
    NO_PACKET_ID = "no_packet_id"
    NOT_REPLY_READY = "not_reply_ready"
    BLOCKED = "blocked"
    DUPLICATE = "duplicate"
    UNKNOWN_COMMAND = "unknown_command"
    RATE_LIMITED = "rate_limited"
    QUEUE_FULL = "queue_full"
    SIGN_COOLDOWN = "sign_cooldown"
    SIGN_DUPLICATE = "sign_duplicate"
    SIGN_EMPTY = "sign_empty"
    SIGN_TOO_LONG = "sign_too_long"
    STORAGE_FULL = "storage_full"
    STALE = "stale"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    QUOTA = "quota"
    SIGN_QUOTA = "sign_quota"
    POST_DUPLICATE = "post_duplicate"
    POST_TOPIC_INVALID = "post_topic_invalid"
    POST_TOPIC_LIMIT = "post_topic_limit"
    POST_EMPTY = "post_empty"
    POST_TOO_LONG = "post_too_long"
    POST_QUOTA = "post_quota"
    MAIL_EMPTY = "mail_empty"
    MAIL_NODE_INVALID = "mail_node_invalid"
    MAIL_TOO_LONG = "mail_too_long"
    MAIL_QUOTA = "mail_quota"


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: Reject | None


# Terminal states a set_state call must never move backward out of. Once a
# request reaches one of these, its outcome is settled.
_TERMINAL_STATES = ("acked", "failed", "expired", "unknown", "dropped")


def check_eligibility(
    obs: IncomingPacket,
    *,
    my_node_num: int,
    reply_ready: bool,
    reply_ttl_seconds: int = 60,
    connected_seconds: float = float("inf"),
) -> Eligibility:
    """RETAINED, NOT PRODUCTION CODE (as of task 2B): this function is kept
    solely as the frozen reference implementation that
    `tests/test_transport.py::test_the_new_table_returns_the_same_reason_as_
    the_old_one` checks `decide_inbound` against, row for row -- see
    transport-boundary-spec.md section 2 ("`check_eligibility` itself is not
    removed by this epic"). Production calls `decide_inbound` (over
    `Inbound`) exclusively; `main.py` has no call site for this function.
    Do not re-wire it into production, and do not let a future edit to
    `decide_inbound` drift out of sync with this table without updating the
    parity test.

    Implements the eligibility decision table, top to bottom, first match wins.

    `obs.channel` is not consulted at any row: a channel index is not proof
    that a packet is a DM, nor is channel index 0 proof that a DM is a public
    broadcast. Only `to_num` decides whether a packet is addressed to us.

    Freshness gate (last row): a historical/replayed packet must never be
    executed as a new command.
    - When `obs.rx_time` (the radio-reported receive time) is present, a
      packet older than `reply_ttl_seconds` relative to `obs.received_at`
      (our own clock) is `Reject.STALE`.
    - When `rx_time` is absent, freshness cannot be established from the
      packet itself. `connected_seconds` -- how long the radio has been
      continuously connected, from the caller -- stands in for it: a packet
      is only treated as a command candidate once the radio has been
      connected for at least `reply_ttl_seconds` (a grace window that
      swallows a backlog of replayed packets delivered right after a
      (re)connect); otherwise it is `Reject.STALE`.
    `connected_seconds` defaults to infinity so callers that don't pass it
    (and every packet with a present `rx_time`) are unaffected.

    This gate only decides whether a packet may ever produce a reply --
    observation/collector recording of the packet is unaffected.
    """
    if obs.from_num == my_node_num:
        return Eligibility(False, Reject.SELF_ORIGIN)
    if obs.to_num == BROADCAST_NUM:
        return Eligibility(False, Reject.BROADCAST)
    if obs.to_num != my_node_num:
        return Eligibility(False, Reject.NOT_FOR_US)
    if obs.portnum != "TEXT_MESSAGE_APP":
        return Eligibility(False, Reject.NOT_TEXT)
    if obs.text is None:
        return Eligibility(False, Reject.NO_TEXT)
    if obs.packet_id is None or obs.packet_id == 0:
        return Eligibility(False, Reject.NO_PACKET_ID)
    if not reply_ready:
        return Eligibility(False, Reject.NOT_REPLY_READY)
    if obs.rx_time is not None:
        if obs.received_at - obs.rx_time > reply_ttl_seconds:
            return Eligibility(False, Reject.STALE)
    elif connected_seconds < reply_ttl_seconds:
        return Eligibility(False, Reject.STALE)
    return Eligibility(True, None)


class Outcome(StrEnum):
    """What an inbound message is allowed to become. Bounded, like `Reject`.

    `KNOCK` is terminal at the dispatcher's door: it may produce exactly one
    rate-limited invite DM and nothing else. It NEVER reaches `parse_command`
    -- see `InboundDecision.command_text`.
    """

    COMMAND = "command"
    KNOCK = "knock"
    REJECTED = "rejected"


@dataclass(frozen=True)
class InboundDecision:
    """The eligibility verdict for one `Inbound`.

    `command_text` is the structural half of the knock guarantee: the command
    dispatcher is fed from this field, never from `Inbound.text`, and the
    field is non-None if and only if the outcome is `COMMAND`. A channel
    knock therefore has nothing to hand `parse_command` -- a channel message
    executing a command is not "prevented", it is unexpressible.
    """

    outcome: Outcome
    reason: Reject | None = None
    command_text: str | None = None

    def __post_init__(self) -> None:
        if (self.command_text is not None) != (self.outcome is Outcome.COMMAND):
            raise ValueError("command_text is set if and only if outcome is COMMAND")
        if (self.reason is not None) != (self.outcome is Outcome.REJECTED):
            raise ValueError("reason is set if and only if outcome is REJECTED")


def decide_inbound(
    inb: Inbound,
    *,
    me: NodeId,
    reply_ready: bool,
    reply_ttl_seconds: int = 60,
    connected_seconds: float = float("inf"),
    channel_policy: ChannelTriggerPolicy | None = None,
    channel_commands: bool = False,
) -> InboundDecision:
    """`check_eligibility`'s decision table, re-derived over `Inbound`.

    The DM rows are the same rows in the same order, returning the same
    `Reject` values, so a Meshtastic packet gets the same verdict it gets
    today. The additions are the channel-knock and optional channel-command
    branches; both are unreachable when `channel_policy` is None, which is
    how Meshtastic stays strictly DM-triggered without eligibility forking.

    A knock is subject to the same not-text/no-text/no-id/not-reply-ready/
    freshness gates as a DM, because every one of them is a reason the
    service could not answer soundly, not a reason peculiar to DMs. The
    per-recipient knock cooldown is NOT applied here: this function is pure,
    and the cooldown is persistent state the caller consults after a `KNOCK`
    verdict (rejecting with `Reject.RATE_LIMITED`, silently).

    Freshness prefers `rx_time` when the transport can supply a
    receiver-side one (Meshtastic), then `sender_ts` (MeshCore's own
    `sender_timestamp`) when the packet carries one, and only otherwise
    falls back to the `connected_seconds` grace window -- see
    `_is_stale`.
    """
    if inb.sender == me:
        return InboundDecision(Outcome.REJECTED, Reject.SELF_ORIGIN)
    if channel_policy is not None and inb.channel is ChannelScope.WATCHED:
        if not inb.is_text:
            return InboundDecision(Outcome.REJECTED, Reject.NOT_TEXT)
        if inb.text is None:
            return InboundDecision(Outcome.REJECTED, Reject.NO_TEXT)
        if inb.raw_id is None or inb.raw_id == 0:
            return InboundDecision(Outcome.REJECTED, Reject.NO_PACKET_ID)
        if not reply_ready:
            return InboundDecision(Outcome.REJECTED, Reject.NOT_REPLY_READY)
        if _is_stale(inb, reply_ttl_seconds, connected_seconds):
            return InboundDecision(Outcome.REJECTED, Reject.STALE)
        if channel_policy.matches(inb.text):
            return InboundDecision(Outcome.KNOCK)
        if not channel_commands:
            return InboundDecision(Outcome.REJECTED, Reject.BROADCAST)
        return InboundDecision(Outcome.COMMAND, command_text=inb.text)
    if inb.recipient == BROADCAST:
        return InboundDecision(Outcome.REJECTED, Reject.BROADCAST)
    if inb.recipient != me:
        return InboundDecision(Outcome.REJECTED, Reject.NOT_FOR_US)
    if not inb.is_text:
        return InboundDecision(Outcome.REJECTED, Reject.NOT_TEXT)
    if inb.text is None:
        return InboundDecision(Outcome.REJECTED, Reject.NO_TEXT)
    if inb.raw_id is None or inb.raw_id == 0:
        return InboundDecision(Outcome.REJECTED, Reject.NO_PACKET_ID)
    if not reply_ready:
        return InboundDecision(Outcome.REJECTED, Reject.NOT_REPLY_READY)
    if _is_stale(inb, reply_ttl_seconds, connected_seconds):
        return InboundDecision(Outcome.REJECTED, Reject.STALE)
    return InboundDecision(Outcome.COMMAND, command_text=inb.text)


def _is_stale(inb: Inbound, reply_ttl_seconds: int, connected_seconds: float) -> bool:
    """Freshness, in order of trust: `rx_time` (a receiver-side clock
    reading -- Meshtastic only) first; then `sender_ts` (MeshCore's
    `sender_timestamp`, the SENDER's own clock -- untrusted, but the radio's
    clock is now set from host time on every connection, so it is
    meaningful, and the direction of the risk is one-way: a forged/late
    `sender_ts` can only make a stale packet look fresh, never the reverse,
    and the existing duplicate-request dedup bounds that); only when
    neither is available does this fall back to `connected_seconds` -- how
    long the radio has been continuously connected -- which is what used to
    reject every DM for `reply_ttl_seconds` after EVERY reconnect, not just
    a cold start.
    """
    if inb.rx_time is not None:
        return inb.received_at - inb.rx_time > reply_ttl_seconds
    if inb.sender_ts is not None:
        return inb.received_at - inb.sender_ts > reply_ttl_seconds
    return connected_seconds < reply_ttl_seconds


@dataclass(frozen=True)
class ParsedCommand:
    command: Command
    argument: str  # raw remainder; EMPTY for bare commands with no argument
    rejection_reason: str | None = None  # Diagnostic for a command hidden by a feature gate.
    # Mesh named by a trailing MC/MT suffix (WHO/GARDEN/RECORD/SEEN): the
    # parser cannot know which mesh the daemon itself serves, so the daemon
    # treats its own mesh's suffix as no suffix. None = no suffix given.
    mesh: str | None = None
    # Name of the meshgarden.modules command that handles this request.
    module: str | None = None


# LAB tier (Task 13): POST/READ/BOARDS are always experimental.
# MAIL/REPLY share the gate only when service.mail_public is false. This is
# the single source of truth for potentially LAB-gated commands; main.py
# applies the public-mail exception rather than duplicating the command set.
LAB_COMMANDS = frozenset(
    {Command.POST, Command.READ, Command.BOARDS, Command.MAIL, Command.REPLY}
)

# `MENU`/`?` sub-page arguments that are LAB-gated (main.py's `_process_obs`
# rewrites `MENU LAB`/`MENU BOARDS` to UNKNOWN for a locked node) -- kept
# next to `LAB_COMMANDS` so both "what's LAB-gated" tables live in one
# place; main.py consults this directly rather than re-listing the two
# page names inline.
LAB_MENU_PAGES = ("LAB", "BOARDS")

# MENU (and its `?` alias) take an optional argument that selects a menu
# page (see `menu_reply_for`) -- unlike the other bare commands below, whose
# generic `if rest: return UNKNOWN` handling in `parse_command` still applies
# unchanged.
_MENU_ALIASES = {"MENU", "?"}

# Trailing mesh suffix accepted by the observatory commands: reads the
# other mesh's observatory (see ParsedCommand.mesh).
_MESH_SUFFIXES = {"MC": "meshcore", "MT": "meshtastic"}
_MESH_SUFFIX_COMMANDS = frozenset({Command.WHO, Command.GARDEN, Command.RECORD})

_BARE_COMMANDS = {
    "NEW": Command.NEW,
    "CHECK": Command.CHECK,
    "WX": Command.WX,
    "SOS": Command.NOT_EMERGENCY,
    "EMERGENCY": Command.NOT_EMERGENCY,
    "INFO": Command.INFO,
    "STATS": Command.STATS,
    "REPEATER": Command.REPEATER,
    "ICHING": Command.ICHING,
    "HAIKU": Command.HAIKU,
    # WHOAMI/SIGNAL are aliases of ME (operator decision): same reply,
    # same _handle_me dispatch, one less command surface to maintain. ME
    # already carries every field WHOAMI's own reply had (RSSI included).
    "WHOAMI": Command.ME,
    "SIGNAL": Command.ME,
    "GARDEN": Command.GARDEN,
    "RECORD": Command.RECORD,
    "WEEK": Command.WEEK,
    "UNSIGN": Command.UNSIGN,
    "WHO": Command.WHO,
    "ME": Command.ME,
    "PING": Command.PING,
    "PATH": Command.PATH,
    "ROUTE": Command.PATH,
    # MOON and SUN are command modules (meshgarden.modules.builtin_modules).
    "BOARDS": Command.BOARDS,
    # Hidden operator command: deliberately absent from _KNOWN_COMMAND_WORDS
    # (so HELP VERSION/? VERSION is UNKNOWN, not CMD_HELP) and from every
    # MENU page/_HELP_USAGE entry -- a visitor only learns it exists by
    # already knowing to send it. Still bare-only (an argument -> UNKNOWN)
    # via the shared _BARE_COMMANDS handling below, and still subject to
    # the normal per-node command quota/rate limit like every other command.
    "VERSION": Command.VERSION,
}

# Every word a locked node's `HELP <word>`/`? <word>` must be silent about:
# the LAB commands' own names PLUS any `_BARE_COMMANDS` alias that resolves
# to one of them. Derived rather than listed, so adding a
# LAB command or an alias for one cannot leave its usage string reachable
# by a node that has not unlocked the tier. main.py's gate consults this.
LAB_HELP_WORDS = frozenset(
    {c.value for c in LAB_COMMANDS}
    | {word for word, cmd in _BARE_COMMANDS.items() if cmd in LAB_COMMANDS}
    | {"INBOX"}
)

# Every word `HELP <cmd>`/`? <cmd>` recognizes for a one-line usage lookup
# (see `_HELP_USAGE`/`help_text`). Includes words for commands later tasks
# haven't wired up yet (POST/READ/BOARDS/MAIL) -- harmless dead entries
# until those commands exist, so this table is written once.
_KNOWN_COMMAND_WORDS = frozenset(
    {
        "MENU", "INFO", "SIGN", "GUESTBOOK", "UNSIGN", "STATS", "REPEATER", "WHOAMI",
        "SIGNAL", "GARDEN", "RECORD", "WEEK", "ICHING", "HAIKU", "BOOK",
        "WORDLE", "SEEN", "WHO", "ME", "PING", "POST", "READ", "BOARDS",
        "MAIL", "HELP", "PATH", "ROUTE", "MOON", "SUN",
        "NEW", "NEWS", "GUIDE", "LOCAL", "CHECK", "INBOX", "REPLY", "WX",
    }
)

# GUESTBOOK's optional argument is validated HERE, not left to main.py/
# guestbook.py, unlike SIGN/BOOK's free-text arguments: its syntax is
# strict (a bare page number 1-999, or "#<entry id>"), so anything else is
# indistinguishable from ordinary unrecognized text and gets the same
# silent no-reply treatment -- consistent with the exact-match philosophy
# every other command already follows. "GUESTBOOK 12" (no "#") is always
# page 12, never entry #12: a bare digit is deliberately never treated as
# an entry id, so the two syntaxes never collide.
_GUESTBOOK_ENTRY_ARG_RE = re.compile(r"^#\d+$")


def _parse_guestbook_argument(rest: str) -> ParsedCommand:
    if not rest:
        return ParsedCommand(Command.GUESTBOOK, "")
    if _GUESTBOOK_ENTRY_ARG_RE.match(rest):
        return ParsedCommand(Command.GUESTBOOK, rest)
    # isascii() guard: some non-ASCII digits (e.g. "²") satisfy
    # isdigit() but raise ValueError from int(), which int()-ing this
    # rest would otherwise crash on.
    if rest.isascii() and rest.isdigit() and 1 <= int(rest) <= 999:
        return ParsedCommand(Command.GUESTBOOK, rest)
    return ParsedCommand(Command.UNKNOWN, "")


# The MeshCore companion app shares a location as plain DM text. Both
# spellings carry "lat, lon" in signed decimal degrees; anything else
# (altitude, a third field, a trailing note) is not a position share.
_POSITION_RE = re.compile(
    r"^(?P<kind>saved|gps)\s+position:\s*"
    r"(?P<lat>[-+]?\d{1,2}(?:\.\d+)?)\s*,\s*"
    r"(?P<lon>[-+]?\d{1,3}(?:\.\d+)?)$",
    re.IGNORECASE,
)


def parse_position_text(text: str) -> tuple[str, float, float] | None:
    """`("saved"|"gps", lat, lon)` for a MeshCore app location-share DM,
    else None. Coordinates outside the valid range are rejected so the
    table never holds an impossible point."""
    m = _POSITION_RE.match(text.strip())
    if m is None:
        return None
    lat, lon = float(m["lat"]), float(m["lon"])
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return m["kind"].lower(), lat, lon


def position_ack_text(kind: str, lat: float, lon: float) -> str:
    """One-line DM acknowledgement. Four decimals (about 11 m) is enough
    for the sender to recognise their own point and well under budget."""
    return f"📍 {kind} position saved · {lat:.4f}, {lon:.4f}"


# Words an operator-defined command (or a module alias) may never take: every
# built-in word and alias, the distress words, the knock triggers, menu page
# names and words reserved for planned commands.
RESERVED_COMMAND_WORDS = frozenset(
    set(_BARE_COMMANDS) | _KNOWN_COMMAND_WORDS
    | {
        "?", "HELP", "SOS", "EMERGENCY", "MAYDAY", "911", "!GARDEN", "!DM", "KNOCK",
        "SIGN", "POST", "READ", "BOOK", "WORDLE", "GUESTBOOK", "SEEN", "MAIL", "INBOX",
        "NEWS", "GUIDE", "LOCAL", "REPLY", "!PATH", "!ROUTE", "!MOON", "!SUN",
        "MOON", "SUN", "VERSION", "MORE", "LAB", "SOCIAL", "GAMES", "DAILY",
        "FIND", "FORECAST", "LINK", "UNLINK", "PRIVACY", "MYDATA", "FORGET",
    }
)


def parse_command(
    text: str, *, lab_unlock_word: str = "", mesh_suffix: bool = True, modules=None
) -> ParsedCommand:
    """Commands are case-insensitive, exact matches -- no prefix matching, so
    e.g. `MENUS` is `UNKNOWN`. `MENU` and `?` are aliases for the same menu
    command; bare `HELP`, `SOS`, and `EMERGENCY` are aliases for
    `NOT_EMERGENCY`, the fixed non-emergency disclaimer.

    `mesh_suffix=False` (no `peer_path` configured) makes a trailing MC/MT
    inert: WHO/GARDEN/RECORD/SEEN parse exactly as before the suffix existed.

    `lab_unlock_word` (default `""`, meaning the LAB tier is off -- see
    config.py's `service.lab_unlock_word`) is checked FIRST, before every
    other branch below: when non-empty and the message's whole stripped
    text matches it case-insensitively (no arguments, ever), this returns
    `Command.LAB_UNLOCK` regardless of what the text would otherwise parse
    as. main.py passes `self.cfg.service.lab_unlock_word`; every other
    caller keeps the default, so this is a no-op unless a caller opts in.

    `HELP <cmd>`/`? <cmd>` (an argument matching a known command word, see
    `_KNOWN_COMMAND_WORDS`) is `Command.CMD_HELP` instead: both forms resolve
    identically (ruling 14). `STATS`/`GAMES`/`BOARDS` still select their
    `MENU` sub-pages, not `CMD_HELP`, for either alias; an unrecognized
    argument to bare `HELP` is `Command.UNKNOWN` (silent), while an
    unrecognized argument to `MENU`/`?` still falls back to the top-level
    menu unchanged.

    `MENU`/`?` take an optional argument, carried raw (case preserved, only
    stripped) in `ParsedCommand.argument` -- unlike every other bare command,
    trailing text does not make it `UNKNOWN`. `menu_reply_for` (not this
    function) decides what the argument means: "STATS"/"GAMES"/"BOARDS"
    case-insensitively select a menu page, anything else (empty, junk, a
    bare number) falls back to the top-level menu. `processed_requests.
    command` is unaffected -- it is still logged as plain `MENU`.

    `BOOK` and `WORDLE` take an optional argument exactly like `SIGN`: bare
    is the day's prompt, `BOOK <guess>`/`WORDLE <guess>` a guess against it.
    Neither argument is ever stored or logged, same as SIGN's.

    `SEEN <node>` also takes its argument lazily like `SIGN`'s: the raw
    remainder is handed through unvalidated (an arbitrary short name has no
    fixed shape to validate against) -- `Collector.resolve_node` (main.py)
    decides what it means. `WHO`/`ME`/`PING` take no argument at all and
    fall into `_BARE_COMMANDS` like `WHOAMI`/`GARDEN`/`RECORD`.

    `POST <topic> <msg>`/`READ <topic> [page]` take their argument lazily
    like `SIGN`'s too (ruling 3): the raw remainder is handed through
    unvalidated -- main.py's handlers split `<topic>` from `<msg>`/`[page]`
    and validate the topic shape themselves (`guestbook.validate_topic`),
    since topic names are free-form, not a closed grammar the way
    `GUESTBOOK`'s argument is. `BOARDS` takes no argument and falls into
    `_BARE_COMMANDS`.

    `GUESTBOOK` also takes an optional argument, but its shape is validated
    eagerly (see `_parse_guestbook_argument`): empty (unchanged, latest
    page), a bare page number 1-999, or `#<entry id>` for a single entry.
    Anything else -- `GUESTBOOK abc`, an out-of-range page number, a bare
    digit meant as an entry id -- is silently `UNKNOWN`, exactly like any
    other unrecognized text.

    `MAIL` also takes its argument lazily like `SIGN`'s: bare is the list
    form, `MAIL ON`/`MAIL OFF` (case-insensitive) is the opt-in toggle, and
    anything else is a `MAIL <node> <msg>` send -- main.py's `_handle_mail`
    tells the three apart, since none of them has a fixed shape `parse_command`
    could validate eagerly.

    `modules` (a `meshgarden.modules.ModuleRegistry`, default: the built-in
    modules only) resolves words no built-in claims. A module that takes no
    argument is UNKNOWN with one, like a bare command.

    Guestbook text case is preserved. Never logs the argument or the raw
    text; callers must do the same.
    """
    if modules is None:
        from meshgarden.modules import default_registry

        modules = default_registry()
    stripped = text.strip()
    if lab_unlock_word and stripped.upper() == lab_unlock_word.upper():
        return ParsedCommand(Command.LAB_UNLOCK, "")
    if parse_position_text(stripped) is not None:
        # The argument is the whole text so the handler can re-parse it;
        # main.py rewrites this to UNKNOWN when dm_position_capture is off.
        return ParsedCommand(Command.POSITION, stripped)
    # split(None, 1) splits on the first RUN of whitespace (not just one
    # space), which is what lets "SIGN\thi" or "SIGN   hi" parse correctly.
    parts = stripped.split(None, 1)
    head = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    upper = head.upper()
    if upper in ("NEWS", "GUIDE", "LOCAL", "REPLY"):
        return ParsedCommand(Command.GUIDE if upper == "LOCAL" else Command(upper), rest.strip())
    if upper == "INBOX":
        return ParsedCommand(Command.MAIL, "") if not rest else ParsedCommand(Command.UNKNOWN, "")
    if upper in {"!PATH", "!ROUTE"}:
        upper = upper[1:]
    if upper == "SIGN":
        return ParsedCommand(Command.SIGN, rest.strip())
    if upper == "POST":
        return ParsedCommand(Command.POST, rest.strip())
    if upper == "READ":
        return ParsedCommand(Command.READ, rest.strip())
    if upper == "BOOK":
        return ParsedCommand(Command.BOOK, rest.strip())
    if upper == "WORDLE":
        return ParsedCommand(Command.WORDLE, rest.strip())
    if upper == "GUESTBOOK":
        return _parse_guestbook_argument(rest.strip())
    if upper == "SEEN":
        argument = rest.strip()
        # Needs a node before the suffix: a lone `SEEN MC` is a node named
        # "MC", exactly as it parsed before the suffix existed.
        words = argument.rsplit(None, 1)
        mesh = (
            _MESH_SUFFIXES.get(words[1].upper()) if mesh_suffix and len(words) == 2 else None
        )
        if mesh is not None:
            return ParsedCommand(Command.SEEN, words[0].strip(), mesh=mesh)
        return ParsedCommand(Command.SEEN, argument)
    if upper == "MAIL":
        return ParsedCommand(Command.MAIL, rest.strip())
    if upper == "HELP":
        if not rest.strip():
            return ParsedCommand(Command.NOT_EMERGENCY, "")
        word = rest.strip().upper()
        if word in _KNOWN_COMMAND_WORDS or modules.lookup(word) is not None:
            return ParsedCommand(Command.CMD_HELP, word)
        return ParsedCommand(Command.UNKNOWN, "")
    if upper in _MENU_ALIASES:
        arg = rest.strip()
        arg_upper = arg.upper()
        if (
            arg_upper not in ("STATS", "GAMES", "BOARDS", "LAB")
            and (arg_upper in _KNOWN_COMMAND_WORDS or modules.lookup(arg_upper) is not None)
        ):
            return ParsedCommand(Command.CMD_HELP, arg_upper)
        return ParsedCommand(Command.MENU, arg)
    if upper in _BARE_COMMANDS:
        mesh = _MESH_SUFFIXES.get(rest.strip().upper()) if mesh_suffix else None
        if mesh is not None and _BARE_COMMANDS[upper] in _MESH_SUFFIX_COMMANDS:
            return ParsedCommand(_BARE_COMMANDS[upper], "", mesh=mesh)
        if rest:
            return ParsedCommand(Command.UNKNOWN, "")
        return ParsedCommand(_BARE_COMMANDS[upper], "")
    module = modules.lookup(upper)
    if module is not None:
        if rest.strip() and not module.takes_argument:
            return ParsedCommand(Command.UNKNOWN, "")
        return ParsedCommand(module.command, rest.strip(), module=module.name)
    return ParsedCommand(Command.UNKNOWN, "")


def suggest_command(text: str, *, hidden_words: frozenset[str] = frozenset()) -> str | None:
    """One-character typo hints for short single words; never echo arbitrary text."""
    word = text.strip().upper()
    if not word.isascii() or not word.isalpha() or not 3 <= len(word) <= 12:
        return None
    if word in _KNOWN_COMMAND_WORDS or word in hidden_words:
        return None

    def close(candidate):
        if abs(len(word)-len(candidate)) > 1:
            return False
        if len(word) == len(candidate):
            mismatches = [i for i in range(len(word)) if word[i] != candidate[i]]
            return len(mismatches) == 1 or (
                len(mismatches) == 2 and mismatches[1] == mismatches[0]+1
                and word[mismatches[0]] == candidate[mismatches[1]]
                and word[mismatches[1]] == candidate[mismatches[0]]
            )
        longer, shorter = sorted((word, candidate), key=len, reverse=True)
        return any(longer[:i]+longer[i+1:] == shorter for i in range(len(longer)))

    # No guessing content, distress words, hidden commands, or short ordinary words.
    choices = [w for w in sorted(_KNOWN_COMMAND_WORDS-hidden_words-{"HELP"})
               if len(w) >= 4 and close(w)]
    return choices[0] if len(choices) == 1 else None


class RequestLog:
    def __init__(self, conn: sqlite3.Connection, *, dedup_hours: int = 24) -> None:
        self._conn = conn
        self._dedup_hours = dedup_hours

    def claim(self, sender_node_id: str, packet_id: int, now: int, command: Command) -> bool:
        """Atomically inserts a `received` row.

        Returns False if the (sender, packet_id) is already present, i.e. a
        duplicate; a repeated packet must never cause another guestbook
        insert or response.
        """
        expires_at = now + self._dedup_hours * 3600
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO processed_requests
                    (sender_node_id, packet_id, first_seen_at, expires_at,
                     command, state, updated_at)
                VALUES (?, ?, ?, ?, ?, 'received', ?)
                """,
                (sender_node_id, packet_id, now, expires_at, command.value, now),
            )
            return cur.rowcount == 1

    def set_state(
        self,
        sender_node_id: str,
        packet_id: int,
        state: str,
        now: int,
        *,
        reason: str | None = None,
        entry_id: int | None = None,
        tx_packet_id: int | None = None,
        commit: bool = True,
    ) -> None:
        """Transitions one request's state.

        `commit=False` runs the UPDATE without opening (or closing) its own
        transaction, so a caller that already holds `with conn:` open (e.g.
        the SIGN path's insert-then-queue transaction) is the sole owner of
        the commit/rollback -- otherwise this method's own `with self._conn:`
        would silently commit that outer transaction early, on this
        statement alone, before the caller's block even exits.
        """
        current = self.get_state(sender_node_id, packet_id)
        if current in _TERMINAL_STATES:
            raise ValueError(
                f"refusing to move terminal state {current!r} back to {state!r} "
                f"for ({sender_node_id}, {packet_id})"
            )
        if commit:
            with self._conn:
                self._update_state(
                    sender_node_id, packet_id, state, now, reason, entry_id, tx_packet_id
                )
        else:
            self._update_state(
                sender_node_id, packet_id, state, now, reason, entry_id, tx_packet_id
            )
        if state in ("dropped", "failed", "expired") and exchange_log.enabled():
            row = self._conn.execute(
                "SELECT command FROM processed_requests WHERE sender_node_id = ? AND packet_id = ?",
                (sender_node_id, packet_id),
            ).fetchone()
            if row is not None:
                exchange_log.log_rejection(
                    sender_node_id, reason or state, command=row[0], now=now, packet_id=packet_id
                )

    def _update_state(
        self,
        sender_node_id: str,
        packet_id: int,
        state: str,
        now: int,
        reason: str | None,
        entry_id: int | None,
        tx_packet_id: int | None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE processed_requests
            SET state = ?,
                updated_at = ?,
                reason = COALESCE(?, reason),
                entry_id = COALESCE(?, entry_id),
                tx_packet_id = COALESCE(?, tx_packet_id),
                submitted_at = CASE WHEN ? IN ('submitted', 'acked')
                    THEN COALESCE(submitted_at, ?) ELSE submitted_at END
            WHERE sender_node_id = ? AND packet_id = ?
            """,
            (state, now, reason, entry_id, tx_packet_id, state, now, sender_node_id, packet_id),
        )

    def get_state(self, sender_node_id: str, packet_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT state FROM processed_requests WHERE sender_node_id = ? AND packet_id = ?",
            (sender_node_id, packet_id),
        ).fetchone()
        return row["state"] if row is not None else None

    def resolve_uncertain_on_startup(
        self, now: int, *, reply_ttl_seconds: int = 60
    ) -> tuple[int, int]:
        """Returns `(unknown_count, received_settled_count)` -- both counts
        land in state 'unknown' (nothing is ever resent); reported
        separately only so the caller can log/count each distinctly.

        `reply_ttl_seconds` is accepted for call-site/signature stability
        but no longer used here: see the 'received' handling below for why.
        """
        # Spec: at restart, do not blindly resend records with uncertain send
        # outcomes. 'submitted' is included: a reply already handed to the
        # radio (it has a tx_packet_id) but whose ack outcome we can no
        # longer wait for across a restart is exactly as uncertain as one
        # still 'queued' or 'sending' -- it moves to 'unknown', never
        # resent, so it becomes terminal and prunable instead of sitting
        # forever.
        with self._conn:
            cur = self._conn.execute(
                "UPDATE processed_requests SET state = 'unknown', updated_at = ? "
                "WHERE state IN ('queued', 'sending', 'submitted')",
                (now,),
            )
            unknown_count = cur.rowcount

            # 'received' rows are claimed but never dispatched to any
            # further state -- normally a request moves past 'received'
            # within the same tick that claimed it. Settled unconditionally
            # here, regardless of age: a restart means NO in-memory work
            # survives for that claim (whatever thread was about to process
            # it is gone), so a 'received' row found at startup can never
            # make further progress no matter how recently it was claimed.
            #
            # Round 2 fix: a previous version of this method only aged
            # 'received' rows OLDER than reply_ttl_seconds, which left a
            # row claimed shortly before a restart (e.g. 10s before, well
            # under the TTL) stuck as 'received' forever -- this same
            # startup call judged it "too young" to touch, and nothing else
            # ever revisited it: not maintenance (only 'submitted' was
            # aged), not retention pruning (terminal states only). Settling
            # every 'received' row here, unconditionally, closes that gap.
            # (`RequestLog.age_stale_received`, called from maintenance, is
            # the separate safety net for a 'received' row stuck in a
            # daemon that never restarts.)
            received = self._conn.execute(
                "UPDATE processed_requests SET state = 'unknown', updated_at = ? "
                "WHERE state = 'received'",
                (now,),
            )
            return unknown_count, received.rowcount

    def age_stale_received(self, now: int, reply_ttl_seconds: int) -> int:
        """Ages any 'received' row older than `reply_ttl_seconds` to
        'unknown' (never resent). Returns the number of rows aged.

        Safety net for a 'received' row stuck in a daemon that never
        restarts: normally every 'received' row is settled well before
        this, either by moving forward in the same tick that claimed it,
        or by `Daemon._settle_failed_request` after `_process_obs` raises
        and is caught. This only catches the case neither of those did
        (e.g. the process died in a way that skipped both, without also
        restarting) -- retention only prunes terminal states, so a row
        stuck as 'received' would otherwise sit forever, invisible to
        pruning and revisited only by `resolve_uncertain_on_startup` if
        the process ever restarts, which it may not.
        """
        cutoff = now - reply_ttl_seconds
        with self._conn:
            cur = self._conn.execute(
                "UPDATE processed_requests SET state = 'unknown', updated_at = ? "
                "WHERE state = 'received' AND first_seen_at < ?",
                (now, cutoff),
            )
            return cur.rowcount

    def find_by_tx_packet_id(self, tx_packet_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM processed_requests WHERE tx_packet_id = ?",
            (tx_packet_id,),
        ).fetchone()

    def recent(self, limit: int) -> list[sqlite3.Row]:
        """Newest first (first_seen_at DESC, rowid DESC as a tiebreaker for
        requests claimed in the same second). Used by the CLI's `requests`
        subcommand -- processed_requests carries no message text, so this
        never risks exposing one."""
        return self._conn.execute(
            "SELECT sender_node_id, first_seen_at, command, state, reason, tx_packet_id "
            "FROM processed_requests ORDER BY first_seen_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()


class ChannelKnockCooldown:
    """Persistent per-RESOLVED-RECIPIENT rate limit for a MeshCore channel
    knock (`channel_knocks`, database.py migration 9; scope-decisions.md
    section 7; transport-boundary-spec.md section 3.2). Keyed on the node
    id a knock would actually DM, never the claimed channel display name
    (see `transport.ChannelTriggerPolicy`'s own docstring): a forged name
    must cost an attacker at most one bounded DM to the node being
    impersonated, not to whichever spelling of a name the attacker types.

    Backed by SQLite, not an in-memory dict, on purpose: a restart, a
    crash, or a restart loop must not reset the cooldown -- the whole
    point of the limit is an attacker-cost bound, and a bound a crash loop
    erases is not one (transport-boundary-spec.md section 3.2's rejected
    alternative).

    `try_claim` is the ONLY way to consult or advance this cooldown; there
    is deliberately no separate "peek" method. An earlier review flagged,
    explicitly, that a rate limit consulted AFTER a send is not a rate
    limit -- fusing the check and the record into one atomic UPSERT makes
    "checked before any send" a property of this one statement rather than
    of caller discipline that a later edit could quietly reorder.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def try_claim(self, node_id: str, now: int, cooldown_seconds: int) -> bool:
        """Returns True, and atomically records `now` as this node's last
        knock, only if `node_id` was NOT already knocked within
        `cooldown_seconds` before `now`. Returns False -- and changes
        nothing -- otherwise.

        The caller MUST NOT enqueue a knock DM except inside the branch
        this method's own True return guards, and MUST NOT call this again
        (or any variant of it) after a send to decide whether the send
        "counted" -- there is only ever one call, and it happens first.
        """
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO channel_knocks (node_id, last_knock_at)
                VALUES (?, ?)
                ON CONFLICT(node_id) DO UPDATE SET last_knock_at = excluded.last_knock_at
                WHERE channel_knocks.last_knock_at <= excluded.last_knock_at - ?
                """,
                (node_id, now, cooldown_seconds),
            )
            return cur.rowcount > 0

    def release(self, node_id: str, claimed_at: int) -> None:
        """Compensating action (fix round 1, item 2): undoes a `try_claim`
        whose DM definitely never transmitted -- queue-full, a TTL expiry,
        or a pre-send refusal such as the radio not being ready -- so a
        genuine knocker is not silenced for the entire cooldown window over
        a failure that was never their fault. Callers MUST NEVER call this
        for a blocked-sender refusal (a blocked node is not owed a retry)
        and MUST NEVER call this for an outcome where a transmission was
        attempted or is uncertain (acked, failed, unknown) -- see
        `models.ReplyJob.release_on_pretransmission_failure`'s own
        docstring for the exact, narrow set of callers that may invoke this.

        Deletes the row only if it still holds exactly `claimed_at` -- the
        timestamp THIS call's own earlier `try_claim` recorded -- so a
        differently-timed claim (a later knock that legitimately went
        through in the meantime) is never accidentally undone. This does
        NOT reorder or weaken `try_claim`'s own atomic claim-before-send
        property: it is a separate, later statement, called only after a
        definite failure is already known, never a retraction of the
        ordering guarantee itself.
        """
        with self._conn:
            self._conn.execute(
                "DELETE FROM channel_knocks WHERE node_id = ? AND last_knock_at = ?",
                (node_id, claimed_at),
            )

    def prune(self, now: int, cooldown_seconds: int, batch: int = 500) -> int:
        """Deletes rows whose cooldown window has fully elapsed -- once a
        node's `last_knock_at` is more than `cooldown_seconds` in the past
        it carries no more information than its absence would. Bounded
        batches (never an unbounded DELETE), same style as
        `quota.DailyQuota.prune`. Returns the number of rows deleted."""
        cutoff = now - cooldown_seconds
        total = 0
        while True:
            cur = self._conn.execute(
                "DELETE FROM channel_knocks WHERE node_id IN"
                " (SELECT node_id FROM channel_knocks WHERE last_knock_at < ? LIMIT ?)",
                (cutoff, batch),
            )
            self._conn.commit()
            if cur.rowcount <= 0:
                return total
            total += cur.rowcount


class CommandCooldownNotices:
    """Persistent keyed throttle for command rejection explanations.

    DM cooldown notices use the sender node id directly; channel notices use
    ``channel:<index>:<node-id>`` so private notices cannot silence the channel.
    Daily quota notices use
    a ``quota:<kind>:<local-day>:<node-id>`` key so notice classes cannot
    suppress each other and a new local quota day always gets a fresh slot.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def try_claim(self, node_id: str, now: int, window_seconds: int = 1800) -> bool:
        """Claim one notice slot without changing the command cooldown itself."""
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO command_cooldown_notices (node_id, last_notice_at)
                VALUES (?, ?)
                ON CONFLICT(node_id) DO UPDATE SET last_notice_at = excluded.last_notice_at
                WHERE command_cooldown_notices.last_notice_at
                      <= excluded.last_notice_at - ?
                """,
                (node_id, now, window_seconds),
            )
            return cur.rowcount > 0

    def prune(self, now: int, window_seconds: int = 1800, batch: int = 500) -> int:
        """Bound retained sender keys once they can no longer suppress a notice."""
        cutoff = now - window_seconds
        total = 0
        while True:
            cur = self._conn.execute(
                "DELETE FROM command_cooldown_notices WHERE node_id IN "
                "(SELECT node_id FROM command_cooldown_notices "
                "WHERE last_notice_at < ? LIMIT ?)",
                (cutoff, batch),
            )
            self._conn.commit()
            if cur.rowcount <= 0:
                return total
            total += cur.rowcount


def is_blocked(conn: sqlite3.Connection, node_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM blocked_senders WHERE node_id = ?", (node_id,)).fetchone()
    return row is not None


# Two-level menu (operator decision: option 2). MENU/? is the top level,
# pointing at sub-pages (MENU STATS, MENU GAMES, MENU DAILY) rather than
# listing every command flat. All pages fit MeshCore's 120 UTF-8 byte
# default as well as Meshtastic. Top-level variants leave space for the
# quota hint (including 3-digit counters); a menu must not depend on the
# outbound truncation safety net to fit. One command per line, with optional
# arguments/subcommands on that same line. HELP <cmd> supplies fuller usage.
# LAB remains visible only after unlocking; daily draws remain separate
# from games.
_MENU_TEXT = (
    "🌱 MeshGarden\n"
    "NEW NEWS GUIDE\n"
    "SIGN <msg> GUESTBOOK\n"
    "MENU SOCIAL\n"
    "MENU STATS\n"
    "MENU GAMES\n"
    "MENU DAILY"
)

_CHANNEL_MENU_TEXT = (
    "MeshGarden · channel\n"
    "STATS GARDEN WEEK RECORD\n"
    "REPEATER PING\n"
    "ICHING HAIKU\n"
    "MOON SUN\n"
    "NEWS GUIDE WX\n"
    "DM MENU: more"
)

_MENU_STATS_TEXT = (
    "📊 Stats & signal\n"
    "STATS local 24h\n"
    "GARDEN today · WEEK 7d\n"
    "REPEATER\n"
    "ME my node · CHECK history\n"
    "PATH route\n"
    "SEEN <node>"
)

_MENU_GAMES_TEXT = (
    "🎲 Games\n"
    "BOOK <title> guess · SCORE\n"
    "WORDLE <guess> · CLUE · GIVEUP · SCORE"
)

_MENU_DAILY_TEXT = (
    "🍃 Daily draws\n"
    "ICHING hexagram\n"
    "HAIKU poem\n"
    "MOON phase\n"
    "SUN rise/set"
)

_MENU_BOARDS_TEXT = (
    "📌 Boards\n"
    "POST <topic> <msg>\n"
    "READ <topic>\n"
    "BOARDS list topics\n"
    "MAIL <node|@alias> <msg>\n"
    "HELP MAIL for options"
)

# This is also the LAB unlock reply. MAIL with no arguments reads notes;
# its optional arguments send one. ON/OFF are alternatives, not a literal.
_LAB_TEXT = (
    "🧪 Lab\n"
    "POST <topic> <msg>\n"
    "READ <topic>\n"
    "BOARDS\n"
    "MAIL ON/OFF\n"
    "MAIL [<node|@alias> <msg>]\n"
    "REPLY #id <msg>"
)

# Fixed reply for HELP/SOS/EMERGENCY. No service name variable -- this reply
# must never depend on operator config, so it can be verified byte-for-byte
# regardless of deployment.
_NOT_EMERGENCY_TEXT = (
    "⚠️ MeshGarden is an automated guestbook.\n"
    "Not an emergency service.\n"
    "It cannot relay calls for help.\n"
    "❓ MENU for commands"
)

# The channel-knock invite DM (task 3B-3; scope-decisions.md section 7).
# Fixed, deployment-independent, like _NOT_EMERGENCY_TEXT -- no operator
# config is interpolated, so it can be verified byte-for-byte regardless of
# deployment. Two binding wording constraints: it must NOT claim the
# recipient asked for this (a channel sender's claimed name is unverified,
# so they may not have -- see ChannelTriggerPolicy's docstring), and it must
# fit inside MeshCore's max_text_bytes (120 default, the tightest budget in
# the system) with headroom to spare.
_KNOCK_TEXT = (
    "🌱 MeshGarden channel knock\n"
    "Someone used your name on the channel.\n"
    "Send MENU here to explore."
)

# Everything after the operator-configured service name in info_text(). Kept
# separate so the name is the only part ever truncated -- the fixed lines
# (SIGN publishes, MENU for commands, and the experimental / no emergency /
# no uptime guarantee disclaimer) must always survive. The warning emoji is
# U+26A0 U+FE0F (6 UTF-8 bytes).
_INFO_SUFFIX = (
    " 🌷\n"
    "📝 SIGN <msg> publishes\n"
    "❓ MENU for commands\n"
    "⚠️ experimental · not for emergencies · no uptime guarantee"
)

# Same lines minus "· no uptime guarantee", for transports whose text budget
# (MeshCore: 120 bytes) cannot hold the full suffix plus a service name.
_INFO_SUFFIX_COMPACT = (
    " 🌷\n"
    "📝 SIGN <msg> publishes\n"
    "❓ MENU for commands\n"
    "⚠️ experimental · not for emergencies"
)

# Leading seedling emoji shared by MENU and INFO (4 UTF-8 bytes + a space).
_GARDEN_EMOJI_PREFIX = "🌱 "

_MAX_RESPONSE_BYTES = 180
# Shortest service name worth showing before INFO drops its shared line.
_MIN_INFO_NAME_BYTES = 8


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncates on a valid UTF-8 character boundary; never splits a codepoint.

    A local copy of sender.truncate_utf8's logic: commands.py cannot import
    sender.py (sender.py already imports commands.py), so the tiny helper is
    duplicated here rather than restructured across a package boundary.
    """
    if max_bytes <= 0:
        return ""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    cut = data[:max_bytes]
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""


def menu_text(*, lab_unlocked: bool = False) -> str:
    """Top-level menu with room for the quota hint within 120 UTF-8 bytes.

    The unlocked variant adds MENU LAB; the default never reveals it.
    """
    return _MENU_TEXT


def channel_menu_text() -> str:
    """Public, read-only MeshCore channel command surface (<=120 bytes)."""
    return _CHANNEL_MENU_TEXT


def menu_stats_text() -> str:
    """`MENU STATS` / `? STATS` reply. Fixed, <= 120 UTF-8 bytes. Lists the
    canonical stats commands while legacy/specialist WHO, RECORD and PING
    remain accepted and documented through HELP."""
    return _MENU_STATS_TEXT


def menu_games_text() -> str:
    """`MENU GAMES` / `? GAMES` reply. Fixed, <= 120 UTF-8 bytes. Lists the
    one-DM-each games: BOOK, WORDLE -- both also point at their SCORE
    sub-command. ICHING/HAIKU are daily draws, not games -- see
    `menu_daily_text`."""
    return _MENU_GAMES_TEXT


def menu_daily_text() -> str:
    """`MENU DAILY` / `? DAILY` reply. Fixed, <= 120 UTF-8 bytes. Lists the
    one-DM-each daily draws: ICHING, HAIKU -- operator decision: these are
    daily draws, not games, so they get their own sub-page rather than
    living on `MENU GAMES` with BOOK/WORDLE."""
    return _MENU_DAILY_TEXT


def menu_boards_text() -> str:
    """`MENU BOARDS` / `? BOARDS` reply. Fixed, <= 120 UTF-8 bytes. Lists the
    topic-wall commands: POST, READ, BOARDS, MAIL. `BOARDS` itself is a
    separate bare command (Task 6); this is only the `MENU` sub-page."""
    return _MENU_BOARDS_TEXT


def lab_text() -> str:
    """`MENU LAB` / `? LAB` reply (Task 13). Fixed, <= 120 UTF-8 bytes.
    Lists POST/READ/BOARDS/MAIL: every command in
    `LAB_COMMANDS` belongs on this page and on no ungated one. Only
    ever reachable by a node that has already unlocked the LAB tier --
    main.py's gate turns `MENU LAB` into `Command.UNKNOWN` for a locked
    node before this is ever called. Also the reply to a successful
    `LAB_UNLOCK` (the operator-configured word, sent as the whole
    message)."""
    return _LAB_TEXT


# Usage strings for `HELP <cmd>`/`? <cmd>` (ruling 14). Every value is a
# fixed, <= 180-UTF-8-byte string with no interpolated visitor input --
# same "never echo visitor text" convention as MENU's own fallback. Layout:
# the syntax on its own first line, then one short line per thing worth
# knowing (what it does, limits, related sub-commands) -- still one packet,
# just easier to scan on a phone than a single run-on line. Includes
# entries for POST/READ/BOARDS/MAIL, whose commands don't exist yet (a
# later task wires up their `parse_command` branches) -- harmless dead
# entries reachable only once `_KNOWN_COMMAND_WORDS` lets `parse_command`
# emit `CMD_HELP` for them, which it already does.
_HELP_USAGE: dict[str, str] = {
    "NEW": ("NEW\nUpdates since your last acknowledged NEW; first visit covers 24h"
            "\nGUESTBOOK, NEWS, boards and mail"),
    "NEWS": "NEWS [page]\nLocal notices from this garden's operator; expired notices disappear",
    "GUIDE": ("GUIDE [topic]\nBay Area MeshCore reference; cached source information"
              "\nLOCAL lists topics"),
    "LOCAL": "LOCAL\nList Bay Area MeshCore guide topics; GUIDE <topic> to read",
    "CHECK": ("CHECK\nCompare this request's RX SNR with 24h samples"
              "\nSNR measures the final RF link"),
    "REPLY": ("REPLY #<id> <msg>\nReply to a collected mail note before its original expiry"
              "\nRecipient must opt in"),
    "INBOX": "INBOX (alias MAIL)\nCollect waiting notes\nMAIL ON to accept notes; MAIL OFF to stop",
    "PATH": ("PATH (or !path / ROUTE)"
            "\nRX hops/signal; TX cached hashes + age\nRepeater identities unverified"),
    "ROUTE": ("ROUTE (alias PATH)"
            "\nRX hops/signal; TX cached hashes + age\nRepeater identities unverified"),
    "SUN": "SUN (or !sun)\nSunrise/sunset at the garden, today in local time; offline",
    "MOON": "MOON (or !moon)\nApproximate phase and illumination; entirely offline",
    "MENU": (
        "MENU (alias ?)\nthe command menu\n"
        "MENU STATS · MENU GAMES · MENU DAILY for the sub-pages"
    ),
    "INFO": "INFO\nwhat this service is\nreminds you SIGN publishes · not for emergencies",
    "SIGN": "SIGN <msg>\npublish a note to the shared guestbook\n100 chars · 3 per day",
    "GUESTBOOK": "GUESTBOOK [page]\nread the shared guestbook\nlatest 3 per page",
    "UNSIGN": "UNSIGN\nhide your own newest visible guestbook entry",
    "STATS": "STATS\nlocal 24h counts\nsenders · unique packets · guestbook · uptime",
    "REPEATER": "REPEATER\nconfigured repeater battery, uptime, packet counts and last RF signal",
    "GARDEN": "GARDEN\ntoday's local activity\nnodes · heard direct · best SNR",
    "RECORD": "RECORD\ntoday's records\nbest SNR · most hops · busiest hour",
    "WEEK": "WEEK\n7-day summary\npackets · senders per day · guestbook · replies",
    "ICHING": "ICHING\nyour daily hexagram\nsame draw all day",
    "HAIKU": "HAIKU\na random garden or mesh haiku",
    "BOOK": (
        "BOOK [title]\ntoday's emoji book-trivia puzzle\n"
        "BOOK <title> to guess · SCORE for today & 7-day totals"
    ),
    "WORDLE": (
        "WORDLE [guess]\n6 tries to guess a 5-letter word\n"
        "CLUE reveals a letter (costs a try)\nGIVEUP ends it · SCORE for totals"
    ),
    "SEEN": "SEEN <node>\nwhen and how I last heard a node\nlooks back 7 days",
    "WHO": "WHO\nnodes heard in the last hour and last 24h\nplus how many were direct",
    "ME": (
        "ME (aliases WHOAMI, SIGNAL)\nyour id and activity\n"
        "this request's signal (SNR/RSSI/hops/PKI) · quota · mail status"
    ),
    "WX": "WX\nStatus of the #wx-bay NWS alert feed\nWarnings and Watches, 9 Bay Area counties",
    "PING": "PING\nquick signal check\nSNR · hops · direct or relay · PKI",
    "POST": "POST <topic> <msg>\npost to a topic wall\n100 chars · 5 per day",
    "READ": "READ <topic> [page]\nread a topic wall\nlatest 3 per page",
    "BOARDS": "BOARDS\nlist topic walls by post count",
    "MAIL": (
        "MAIL <node|@alias> <msg>\nMAIL ON/OFF · MAIL checks notes\n"
        "MAIL ALIAS <name|OFF>"
    ),
    "HELP": "HELP <cmd> (or ? <cmd>)\nusage for a single command",
}
# WHOAMI/SIGNAL are aliases of ME (see _BARE_COMMANDS): `HELP WHOAMI`/
# `HELP SIGNAL` return ME's own usage rather than a separate entry, so the
# alias is invisible to a visitor asking for help either way.
_HELP_USAGE["WHOAMI"] = _HELP_USAGE["SIGNAL"] = _HELP_USAGE["ME"]


def help_text(word: str) -> str | None:
    """Usage for a known command word (case-insensitive), or None if `word`
    isn't recognized. Every value is a fixed, <=180-UTF-8-byte string with
    no interpolated visitor input; syntax on the first line, then one short
    line per detail."""
    return _HELP_USAGE.get(word.strip().upper())


def menu_reply_for(argument: str, *, lab_unlocked: bool = False, mail_public: bool = True) -> str:
    """Selects the MENU/? reply body from `ParsedCommand.argument` (case-
    insensitive, like everywhere else): "STATS" -> `menu_stats_text()`,
    "GAMES" -> `menu_games_text()`, "DAILY" -> `menu_daily_text()`,
    "BOARDS" -> `menu_boards_text()`. Anything else -- empty, a typo, a
    bare number -- falls back to the top-level `menu_text(lab_unlocked=
    lab_unlocked)`, with no echo of the argument itself anywhere in the
    reply. `lab_unlocked` only ever affects that fallback; "LAB" always
    returns `lab_text()` regardless (main.py never lets argument reach here
    as "LAB" for a locked node in the first place)."""
    upper = argument.strip().upper()
    if upper == "SOCIAL":
        text = "🤝 Social\nSIGN <msg>\nGUESTBOOK [page]\nUNSIGN | INFO"
        if mail_public or lab_unlocked:
            text += "\nMAIL | MAIL ON/OFF\nMAIL <node|@alias> <msg>\nREPLY #id <msg>"
        if lab_unlocked:
            text += "\nMENU LAB"
        return text
    if upper == "STATS":
        return menu_stats_text()
    if upper == "GAMES":
        return menu_games_text()
    if upper == "DAILY":
        return menu_daily_text()
    if upper == "BOARDS":
        return menu_boards_text()
    if upper == "LAB":
        return lab_text()
    return menu_text(lab_unlocked=lab_unlocked)


def quota_hint_suffix(quota_used: int, quota_limit: int) -> str:
    """`" · quota N/60 today"` -- appended (via `with_quota_hint`) to the
    top-level MENU reply, so a visitor silently refused on
    cooldown/quota/queue_full (no reply at all for that attempt) still has
    somewhere to check today's count (ME's own reply shows the same count
    inline, on its own "quota N/N" line, with no need for this suffix).
    Never appended to MENU STATS/MENU BOARDS/MENU GAMES/MENU DAILY, and
    never sent as a reply of its own."""
    return f" · quota {quota_used}/{quota_limit} today"


def with_quota_hint(text: str, quota_used: int, quota_limit: int, *, max_bytes: int) -> str:
    """Appends `quota_hint_suffix(quota_used, quota_limit)` to `text`.

    If the combined reply would exceed `max_bytes` UTF-8 bytes, only
    `text`'s own last line is shortened (on a UTF-8 boundary, via
    `_truncate_utf8`) to make room -- every earlier line, and the quota
    suffix itself, always survive intact. Surfacing the quota count is the
    entire point of this call, so it is never the part sacrificed.

    `max_bytes` is a required keyword, not defaulted to this module's own
    `_MAX_RESPONSE_BYTES` -- a caller budgeting against any other constant
    (e.g. main.py's configured `service.max_response_bytes`) must say so
    explicitly, so the hinted reply is never re-truncated by a *different*,
    mismatched budget afterwards."""
    suffix = quota_hint_suffix(quota_used, quota_limit)
    candidate = text + suffix
    if len(candidate.encode("utf-8")) <= max_bytes:
        return candidate
    head, sep, last = text.rpartition("\n")
    prefix = head + sep
    budget = max_bytes - len(prefix.encode("utf-8")) - len(suffix.encode("utf-8"))
    return prefix + _truncate_utf8(last, max(0, budget)) + suffix


def not_emergency_text(*, max_bytes: int = _MAX_RESPONSE_BYTES) -> str:
    """Fixed, <= 180 UTF-8 bytes, reply for HELP/SOS/EMERGENCY. Carries no
    operator-configured service name -- unlike info_text(), nothing here is
    ever interpolated.

    At a tighter budget than 180 (e.g. MeshCore's 120-byte
    `max_text_bytes`), the middle elaboration line ("It cannot relay calls
    for help.") is dropped first -- the guestbook disclaimer and the MENU
    pointer are the two lines that must always survive."""
    if len(_NOT_EMERGENCY_TEXT.encode("utf-8")) <= max_bytes:
        return _NOT_EMERGENCY_TEXT
    lines = _NOT_EMERGENCY_TEXT.split("\n")
    del lines[2]
    return "\n".join(lines)


def knock_text() -> str:
    """The ONLY reply a channel knock may ever produce: one direct message
    to the resolved contact (see `Daemon._handle_knock`). Fixed, 96 UTF-8
    bytes -- well inside MeshCore's `max_text_bytes` (120 default) even
    before `truncate_utf8`'s last-line safety net. Never asserts the
    recipient asked for this, since a channel sender's claimed name is
    unauthenticated (see `transport.ChannelTriggerPolicy`)."""
    return _KNOCK_TEXT


def info_text(
    service_name: str,
    shared_with: str | None = None,
    max_bytes: int = _MAX_RESPONSE_BYTES,
) -> str:
    """<= 180 UTF-8 bytes, multi-line. Names the operator-configured service,
    states that SIGN publishes, points to MENU for the full command list,
    and carries the experimental/no-emergency/no-uptime disclaimer --
    e.g. "🌱 {name} 🌷\\n📝 SIGN <msg> publishes\\n❓ MENU for commands\\n⚠️
    experimental · not for emergencies · no uptime guarantee".
    If the full text would exceed the budget, only the service name is
    truncated (on a UTF-8 boundary) so the leading emoji and the fixed
    suffix always survive.

    `shared_with` (the OTHER mesh's label, only when a shared bbs file is in
    use) adds one line before the disclaimer, so visitors learn their posts
    and mail are readable from the other mesh; the name budget shrinks to
    match. `max_bytes` is the transport's text budget: the disclaimer always
    survives, so the shared line is dropped when the budget cannot hold it
    plus a minimal name (the name is ellipsized first)."""
    prefix_bytes = len(_GARDEN_EMOJI_PREFIX.encode("utf-8"))

    def room(suffix: str) -> int:
        return max_bytes - prefix_bytes - len(suffix.encode("utf-8"))

    suffix = _INFO_SUFFIX
    if shared_with:
        head, _, tail = suffix.rpartition("\n")
        shared = f"{head}\n🔗 Shared with {shared_with}\n{tail}"
        if room(shared) >= _MIN_INFO_NAME_BYTES:
            suffix = shared
    if room(suffix) < _MIN_INFO_NAME_BYTES:
        # Tight transport (MeshCore, 120 bytes): the compact suffix drops
        # only the uptime clause, so "not for emergencies" always survives.
        suffix = _INFO_SUFFIX_COMPACT
    name = _truncate_utf8(service_name, max(0, room(suffix)))
    return _GARDEN_EMOJI_PREFIX + name + suffix


def version_text(version: str) -> str:
    """Hidden operator command `VERSION`'s reply: one line, <= 180 UTF-8
    bytes, "🌱 MeshGarden v<version>". Not listed in any MENU page or
    HELP_USAGE (see `_BARE_COMMANDS`'s comment) -- an operator/support tool,
    not a documented visitor command."""
    return f"{_GARDEN_EMOJI_PREFIX}MeshGarden v{version}"


# --- SEEN / WHO / ME / PING / GARDEN / RECORD / WEEK: local -----------------------
# observability formatters
#
# All four are pure formatters: every count/value is computed by the caller
# (main.py, from collector.py/retention.py queries) and handed in already
# resolved. None of these ever take a message body, guess text, or anything
# beyond bounded numbers and node ids the caller already observed itself.


def _node_id_tail(node_id: str) -> str:
    """Last 4 hex characters of a node id -- same shape as
    guestbook.fallback_display_name, duplicated here rather than importing
    guestbook (guestbook.py already imports commands.py)."""
    return node_id.removeprefix("!")[-4:]


def _name_and_tail(name: str | None, node_id: str) -> str:
    """"<name> · <tail>" when name is given, else the bare tail. name is
    assumed already sanitized/short (callers truncate via the same 4-char
    convention format_guestbook uses)."""
    tail = _node_id_tail(node_id)
    return f"{name} · {tail}" if name else tail


def _relative_ago(seconds: int) -> str:
    """"just now" / "N min ago" / "N hr ago" / "N days ago" -- coarse,
    bucketed to whichever unit is largest without being zero."""
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    return f"{days} days ago"


def _format_hops_parts(hop_start: int | None, hop_limit: int | None) -> tuple[str, str]:
    """(hop count, category) where category is "direct"/"via relay"/"n/a".
    "n/a"/"n/a" unless both hop_start and hop_limit are present (same
    derivation rule as collector.py's hops_away: a negative difference is
    treated as absent, never shown)."""
    if hop_start is None or hop_limit is None:
        return ("n/a", "n/a")
    diff = hop_start - hop_limit
    if diff < 0:
        return ("n/a", "n/a")
    if diff == 0:
        return ("0", "direct")
    return (str(diff), "via relay")


def _format_hops_text(hop_start: int | None, hop_limit: int | None) -> str:
    """"hops 0 direct" / "hops 3 via relay" / "hops n/a" -- the category is
    folded into the same phrase so an unknown hop count never renders as
    the puzzling "hops n/a · n/a"."""
    hops_n, hops_cat = _format_hops_parts(hop_start, hop_limit)
    return "hops n/a" if hops_cat == "n/a" else f"hops {hops_n} {hops_cat}"


def seen_text(
    node_label: str,
    *,
    relative_ago: str,
    how: str,
    snr: float | None,
    first_seen_date: str,
    days_seen: int,
    heard_24h: int | None = None,
    max_bytes: int = _MAX_RESPONSE_BYTES,
) -> str:
    """Report the last reception and activity, within the transport budget.

    Missing hops do not imply a direct reception. Only direct receptions
    expose SNR; the optional packet count is omitted before core fields.
    """
    route = "hops not reported" if how == "unknown" else f"route: {how}"
    if how == "direct" and snr is not None:
        route += f" · SNR {snr:+.1f} dB"
    details = (
        f"\nlast heard {relative_ago}\n{route}\n"
        f"first seen {first_seen_date}\nheard {days_seen}/7 days"
    )
    prefix = "📡 "
    label_budget = max_bytes - len((prefix + details).encode("utf-8"))
    if len(node_label.encode("utf-8")) > label_budget:
        # Spend decoration/route wording before sacrificing a known identity.
        prefix = ""
        details = details.replace("route: ", "")
        label_budget = max_bytes - len(details.encode("utf-8"))
    if len(node_label.encode("utf-8")) > label_budget and " · " in node_label:
        name, tail = node_label.rsplit(" · ", 1)
        name_budget = label_budget - len((" · " + tail).encode("utf-8"))
        node_label = _truncate_utf8(name, max(0, name_budget)) + " · " + tail
    body = prefix + _truncate_utf8(node_label, max(0, label_budget)) + details
    if heard_24h is not None:
        activity = f"\n24h: {heard_24h} packets"
        if len((body + activity).encode("utf-8")) <= max_bytes:
            body += activity
    return body


def seen_not_heard_text(node_label: str, *, listening_seconds: int | None) -> str:
    """Covers BOTH "never observed" and "observed, but not in the last 7
    days" -- deliberately indistinguishable (see plan's privacy ruling).

    The claimed window must never be longer than we have actually been
    listening: `listening_seconds` (from `Collector.first_packet_at` via
    main.py's `_handle_seen`) `>=` 7 days keeps the original "not heard in
    7 days" wording; less than that instead names how long ago we started
    (`_relative_ago`), never a fixed window we haven't earned yet.
    `listening_seconds=None` means we have recorded no observations at
    all, ever -- "not heard yet" rather than a claim about a window."""
    if listening_seconds is None:
        return f"📡 {node_label} not heard yet 🌱"
    if listening_seconds >= 7 * 86400:
        return f"📡 {node_label} not heard in 7 days 🌱"
    return f"📡 {node_label} not heard since I started listening {_relative_ago(listening_seconds)}"


def seen_ambiguous_text() -> str:
    """Never echoes the visitor's raw argument -- same "no echo" rule as
    MENU's unrecognized-argument fallback."""
    return "📡 Multiple nodes match that name\nUse !xxxxxxxx or a 4-hex tail instead"


def who_text(*, heard_1h: int, direct_1h: int, heard_24h: int) -> str:
    """No node ids -- aggregate counts only. One time window per line."""
    return (
        "👥 Nodes heard\n"
        f"last hour {heard_1h} · {direct_1h} direct\n"
        f"last 24h {heard_24h} 🌱"
    )


def ping_text(
    *, snr: float | None, hop_start: int | None, hop_limit: int | None, pki_encrypted: bool | None
) -> str:
    """Two lines: "pong", then the signal facts for THIS request."""
    snr_s = f"{snr:+.1f} dB" if snr is not None else "n/a"
    hops_s = _format_hops_text(hop_start, hop_limit)
    pki_s = "n/a" if pki_encrypted is None else ("✓" if pki_encrypted else "✗")
    return f"🏓 pong\nSNR {snr_s} · {hops_s} · PKI {pki_s} 🌱"


def me_text(
    node_id: str,
    *,
    display_name: str | None,
    first_seen_date: str,
    days_seen: int,
    heard_24h: int,
    snr: float | None,
    rssi: int | None,
    hop_start: int | None,
    hop_limit: int | None,
    pki_encrypted: bool | None,
    signed_count: int,
    quota_used: int,
    quota_limit: int,
    mail_enabled: bool,
    mail_pending: int,
    max_bytes: int = _MAX_RESPONSE_BYTES,
) -> str:
    """ME: the requester's own directory/status summary -- WHOAMI/SIGNAL are
    aliases of ME (operator decision), so this reply now carries every
    field WHOAMI's own used to (RSSI included) on its signal line. Line 1:
    the name-aware "You are" opener. Then one line per theme: when I've
    seen you, how I hear you right now, what you've done here. "Since"/
    "...d" (not "First seen"/"days") keep line 2 compact now that line 3
    also carries RSSI -- still <= 180 UTF-8 bytes worst case (see
    test_me_text_worst_case_fits_the_budget). mail_pending is only ever
    shown when mail_enabled.

    At a tighter budget (e.g. MeshCore's 120-byte `max_text_bytes`), detail
    is dropped in order of importance: RSSI, the PKI flag, the 24h count,
    then the mail pending count. Keep the node name and ID in the header;
    shorten names only when the complete reply cannot fit."""
    snr_s = f"{snr:+.1f}" if snr is not None else "n/a"
    rssi_s = str(rssi) if rssi is not None else "n/a"
    hops_s = _format_hops_text(hop_start, hop_limit)
    pki_s = "n/a" if pki_encrypted is None else ("✓" if pki_encrypted else "✗")
    name = display_name or None
    show_rssi = True
    show_pki = True
    show_heard = True
    mail_s = f"mail on · {mail_pending} waiting" if mail_enabled else "mail off"

    def render() -> str:
        header = f"📡 You are {name} ({node_id})" if name else f"📡 You are {node_id}"
        since = f"Since {first_seen_date} · {days_seen}/7d"
        if show_heard:
            since += f" · {heard_24h}×/24h"
        signal = f"SNR {snr_s}"
        if show_rssi:
            signal += f" · RSSI {rssi_s}"
        signal += f" · {hops_s}"
        if show_pki:
            signal += f" · PKI {pki_s}"
        status = f"Signed {signed_count} · quota {quota_used}/{quota_limit} · {mail_s}"
        return "\n".join((header, since, signal, status))

    text = render()
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    show_rssi = False
    text = render()
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    show_pki = False
    text = render()
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    show_heard = False
    text = render()
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    mail_s = "mail on" if mail_enabled else "mail off"
    text = render()
    if len(text.encode("utf-8")) > max_bytes:
        text = text.replace("📡 You are ", "📡 ", 1)
        if name and len(text.encode("utf-8")) > max_bytes:
            budget = max_bytes - len(text.encode("utf-8")) + len(name.encode("utf-8"))
            name = _truncate_utf8(name, max(0, budget))
            text = render().replace("📡 You are ", "📡 ", 1)
    return _truncate_utf8(text, max_bytes)


def _fit_node_label(label: str, budget: int) -> str:
    if len(label.encode("utf-8")) <= budget:
        return label
    if " · " in label:
        name, tail = label.rsplit(" · ", 1)
        available = budget - len((" · " + tail).encode("utf-8"))
        if available > 0:
            return _truncate_utf8(name, available) + " · " + tail
    return _truncate_utf8(label, max(0, budget))


def repeater_text(
    stats: dict, *, label: str | None, age: int | None, stale: bool,
    max_bytes: int = 120,
) -> str:
    """Self-reported repeater telemetry; last RX signal is measured there.

    Keep identity and freshness. Omit the complete optional signal line
    before shortening core telemetry; never leave a clipped measurement.
    """
    voltage = stats.get("bat")
    volts = f"{voltage / 1000:.2f}V" if isinstance(voltage, (int, float)) else "?V"
    uptime = stats.get("uptime")
    up = "?"
    if isinstance(uptime, (int, float)):
        days, remainder = divmod(max(0, int(uptime)), 86400)
        hours, remainder = divmod(remainder, 3600)
        up = f"{days}d{hours}h" if days else f"{hours}h{remainder // 60}m"
    rx, tx = stats.get("nb_recv"), stats.get("nb_sent")
    header = f"📡 {label or 'Repeater'}"
    power = f"🔋 {volts} · up {up}"
    packets = f"📦 RX {rx if rx is not None else '?'} TX {tx if tx is not None else '?'}"
    freshness = f"🕒 {age}s old" if age is not None else "🕒 age unknown"
    if stale:
        freshness += " · stale"
    signal = []
    if stats.get("last_rssi") is not None:
        signal.append(f"RSSI {stats['last_rssi']}dBm")
    if stats.get("last_snr") is not None:
        signal.append(f"SNR {stats['last_snr']:g}dB")
    lines = [header, power, packets]
    if signal:
        lines.append("Last RX: " + " ".join(signal))
    lines.append(freshness)
    text = "\n".join(lines)
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    text = "\n".join((header, power, packets, freshness))
    if len(text.encode("utf-8")) > max_bytes:
        text = text.replace("🔋 ", "").replace("📦 ", "").replace("🕒 ", "")
    if len(text.encode("utf-8")) > max_bytes:
        other_lines = text.splitlines()[1:]
        budget = max_bytes - len(("📡 \n" + "\n".join(other_lines)).encode("utf-8"))
        header = "📡 " + _fit_node_label(label or "Repeater", budget)
        text = "\n".join([header, *other_lines])
    return _truncate_utf8(text, max_bytes)


def garden_text(
    name: str,
    *,
    nodes_today: int,
    heard_direct: int,
    best_snr: float | None,
    guestbook_entries: int,
    furthest_direct_km: float | None = None,
    max_bytes: int = _MAX_RESPONSE_BYTES,
) -> str:
    """GARDEN reply: today's local activity summary.

    `furthest_direct_km` (farthest known position among nodes heard direct
    today, from the configured observatory location -- see
    Collector.furthest_direct_km_since) is appended only when given; the
    caller omits it entirely (leaves the default None) when the observatory
    location is unset or no direct node has a known position.

    Name-aware like info_text(): if the full text would exceed `max_bytes`
    (e.g. MeshCore's tighter 120-byte `max_text_bytes`), only the service
    name is truncated (on a UTF-8 boundary) so the fixed lines (nodes
    today/heard direct, best SNR, guestbook count, furthest direct when
    present, ❓ MENU) always survive."""
    snr_line = f"Best SNR {best_snr:+.1f} dB" if best_snr is not None else "Best SNR n/a"
    node_word = "node" if nodes_today == 1 else "nodes"
    lines = [f"{nodes_today} {node_word} today"]
    if heard_direct > 0:
        lines.append(f"{heard_direct} confirmed direct")
    lines.append(snr_line)
    if furthest_direct_km is not None:
        lines.append(f"Furthest direct {furthest_direct_km:.0f} km")
    entry_word = "entry" if guestbook_entries == 1 else "entries"
    lines.append(f"📖 {guestbook_entries} guestbook {entry_word} · ❓ MENU")
    suffix = "\n" + "\n".join(lines)
    max_name_bytes = (
        max_bytes - len(_GARDEN_EMOJI_PREFIX.encode("utf-8")) - len(suffix.encode("utf-8"))
    )
    if max_name_bytes < 8:
        # Keep room for a recognizable garden name before verbose labels.
        suffix = suffix.replace("guestbook entries", "entries").replace("guestbook entry", "entry")
        max_name_bytes = (
            max_bytes - len(_GARDEN_EMOJI_PREFIX.encode("utf-8")) - len(suffix.encode("utf-8"))
        )
    name_t = _truncate_utf8(name, max(0, max_name_bytes))
    return _truncate_utf8(_GARDEN_EMOJI_PREFIX + name_t + suffix, max_bytes)


def record_text(
    *,
    best_snr: float | None,
    best_snr_node_id: str | None,
    best_snr_node_name: str | None = None,
    max_hops: int | None,
    busiest_hour: int | None,
    total_packets: int,
    max_hops_node_id: str | None = None,
    max_hops_node_name: str | None = None,
    furthest_observed_km: float | None = None,
    furthest_observed_node_id: str | None = None,
    furthest_observed_node_name: str | None = None,
    companion_events: bool = False,
    max_bytes: int = _MAX_RESPONSE_BYTES,
) -> str:
    """RECORD reply: today's records. `best_snr_node_id` and
    `furthest_observed_node_id` are shown as "<name> · <tail>" when a
    short name is on file (ruling 16), else the bare tail (e.g. "94ba"),
    never the full id.

    `furthest_observed_km`/`furthest_observed_node_id` (farthest known
    position among ANY node observed today, from the configured
    observatory location -- see Collector.furthest_observed_km_with_node_since)
    are appended only when both are given; the caller omits them entirely
    when the observatory location is unset or no observed node has a known
    position.

    If the full text would exceed `max_bytes` (e.g. MeshCore's tighter
    120-byte `max_text_bytes`), omit the optional distance line, then use
    compact wording. Known names stay attached to their node IDs."""
    hour_s = f"{busiest_hour:02d}:00" if busiest_hour is not None else "n/a"
    has_furthest = furthest_observed_km is not None and furthest_observed_node_id is not None
    unit = "events" if companion_events else "packets"

    def render(
        *,
        distance: bool,
        snr_name: bool,
        hops_name: bool,
        distance_name: bool,
        hops_sender: bool,
    ) -> str:
        if best_snr is not None and best_snr_node_id is None:
            snr_line = f"Best SNR {best_snr:+.1f} dB (unk.)"
        elif best_snr is not None:
            best_name = best_snr_node_name if snr_name else None
            snr_line = (
                f"Best SNR {best_snr:+.1f} dB "
                f"({_name_and_tail(best_name, best_snr_node_id)})"
            )
        else:
            snr_line = "Best SNR n/a"
        if companion_events:
            snr_line = snr_line.replace("Best SNR", "Best RX SNR")

        if max_hops is None:
            hops_line = "Most RX hops n/a"
        elif hops_sender and max_hops_node_id is None:
            hops_line = f"Most RX hops {max_hops} (unk.)"
        elif hops_sender:
            hop_name = max_hops_node_name if hops_name else None
            hops_line = (
                f"Most RX hops {max_hops} "
                f"({_name_and_tail(hop_name, max_hops_node_id)})"
            )
        else:
            hops_line = f"Most RX hops {max_hops}"

        lines = ["🏆 Today's records", snr_line, hops_line]
        if distance and has_furthest:
            far_name = furthest_observed_node_name if distance_name else None
            lines.append(
                f"Furthest observed {furthest_observed_km:.0f} km "
                f"({_name_and_tail(far_name, furthest_observed_node_id)})"
            )
        lines.append(f"Busiest {hour_s} · {total_packets} {unit} 🌱")
        return "\n".join(lines)

    for distance in (has_furthest, False):
        text = render(
            distance=distance, snr_name=True, hops_name=True,
            distance_name=True, hops_sender=True,
        )
        if len(text.encode("utf-8")) <= max_bytes:
            return text
    # Before shortening identities, try the compact record-only layout with
    # their full known names. The heading and activity total are optional;
    # the record holders are the useful part of this reply.
    text = render(distance=False, snr_name=True, hops_name=True,
                  distance_name=True, hops_sender=True)
    lines = text.splitlines()[1:-1]
    compact = "\n".join(lines)
    activity = f"\nBusiest {hour_s} · {total_packets} {unit}"
    if len(compact.encode("utf-8")) <= max_bytes:
        if len((compact + activity).encode("utf-8")) <= max_bytes:
            compact += activity
        return compact

    # Only shorten names when the compact record lines themselves do not fit.
    best_snr_node_name = _truncate_utf8(best_snr_node_name, 8) if best_snr_node_name else None
    max_hops_node_name = _truncate_utf8(max_hops_node_name, 8) if max_hops_node_name else None
    text = render(distance=False, snr_name=True, hops_name=True,
                  distance_name=True, hops_sender=True)
    compact = "\n".join(text.splitlines()[1:-1])
    if len((compact + activity).encode("utf-8")) <= max_bytes:
        compact += activity
    return _truncate_utf8(compact, max_bytes)


def week_text(
    *,
    total_packets: int,
    senders_min: int | None,
    senders_max: int | None,
    guestbook_entries: int,
    replies: int,
    companion_events: bool = False,
) -> str:
    """WEEK reply: 7-day aggregates from daily_stats. senders/day is always
    a range across the days, never a summed total."""
    senders_s = (
        f"{senders_min}–{senders_max} senders per day"
        if senders_min is not None
        else "n/a senders per day"
    )
    return "\n".join(
        (
            "📅 Last 7 days",
            f"{total_packets:,} {'events' if companion_events else 'packets'}",
            senders_s,
            f"📖 +{guestbook_entries} guestbook entries",
            f"💬 {replies} replies sent 🌱",
        )
    )


def boards_text(topic_counts: list[tuple[str, int]]) -> str:
    """BOARDS: top topics by count, one "#topic · N posts" line each.
    Truncates the topic list (never the header) to fit the byte budget,
    safety-netted by truncate_utf8."""
    if not topic_counts:
        return "📌 Boards\nno topics yet · POST <topic> <msg> to start one 🌱"
    n = len(topic_counts)
    while n > 0:
        body = "\n".join(
            f"#{t} · {c} post{'s' if c != 1 else ''}" for t, c in topic_counts[:n]
        )
        candidate = f"📌 Boards\n{body}\n🌱 POST <topic> <msg>"
        if len(candidate.encode("utf-8")) <= _MAX_RESPONSE_BYTES:
            return candidate
        n -= 1
    fallback = "📌 Boards\nno topics yet · POST <topic> <msg> to start one 🌱"
    return _truncate_utf8(fallback, _MAX_RESPONSE_BYTES)


# --- MAIL: request-driven mailbox -------------------------------------------
#
# Ruling 10: `mail_sent_ack_text` is the exact same fixed reply for EVERY
# well-formed `MAIL <node> <msg>`, regardless of whether the note was
# actually stored -- opt-in/pending-cap state is never visible in it.

_MAIL_NOTE_TAKEN_TEXT = (
    "📬 Note handed to MeshGarden. The recipient sees it only if they have "
    "MAIL ON and DM me."
)


def mail_sent_ack_text() -> str:
    """Fixed, sent for EVERY well-formed MAIL <node> <msg> regardless of
    whether the note was actually stored (ruling 10) -- never varies by
    opt-in/pending-full state."""
    return _MAIL_NOTE_TAKEN_TEXT


def mail_pref_ack_text(enabled: bool) -> str:
    if enabled:
        return (
            "📬 Mail is ON\n"
            "Others can leave notes: MAIL <you> <msg>\n"
            "↩ Reply: REPLY #id <msg>"
        )
    return "📬 Mail is OFF\nNew notes are paused"


def mail_alias_text(status: str, alias: str | None = None) -> str:
    """Bounded self-service alias replies; aliases are canonical safe tokens."""
    if status in ("claimed", "unchanged") and alias is not None:
        return f"📬 Alias: @{alias}\nSend: MAIL @{alias} <msg>"
    if status == "removed":
        return "📬 Alias removed"
    if status == "taken":
        return "📬 Alias unavailable\nChoose another name"
    if status == "cooldown":
        return "📬 Alias unchanged\nYou can change it once daily"
    if status == "none":
        return "📬 No mail alias\nSet: MAIL ALIAS <name>"
    return "📬 Alias format\n3–16 letters, numbers, _ or -"


def mail_empty_text() -> str:
    return "📬 No notes waiting 🌱"


def _mail_reply_time_left(created_at: int, now: int, expiry_days: int | None) -> str:
    """Compact, conservative route lifetime for a rendered note."""
    if expiry_days is None:
        return ""
    remaining = created_at + expiry_days * 86400 - now
    if remaining <= 0:
        return " · reply expired"
    if remaining < 3600:
        return " · reply <1h left"
    if remaining < 86400:
        hours = remaining // 3600
        return f" · reply {hours}h left"
    days = remaining // 86400
    return f" · reply {days}d left"


def _mail_reply_hint(text: str, hint: str, max_bytes: int) -> str:
    """Add a usage hint only when it fits without displacing note content."""
    candidate = f"{text}\n{hint}"
    return candidate if len(candidate.encode("utf-8")) <= max_bytes else text


def mail_page(
    entries: list,
    *,
    now: int,
    max_bytes: int,
    labels: dict[str, str] | None = None,
    expiry_days: int | None = None,
) -> tuple[str, list[tuple[int, str, int]]]:
    """Return text and exact (id, original body, character count) delivery receipts.

    Receipts are applied only after ACK. A partial note retains its unseen suffix;
    the original body guards against two outstanding reads consuming it twice.
    Stable IDs remain visible on full and partial notes so a body-free reply route
    can outlive ACK consumption. Reply hints are opportunistic: they never reduce
    the body chunk that would otherwise fit.
    """
    if not entries:
        return _truncate_utf8(mail_empty_text(), max_bytes), []
    for n in range(len(entries), 0, -1):
        subset = entries[:n]
        lines = [f"📬 {n} note{'s' if n != 1 else ''}"]
        for e in subset:
            label = (labels or {}).get(e.sender_node_id, _node_id_tail(e.sender_node_id))
            ago = _relative_ago(max(0, now - e.created_at))
            left = _mail_reply_time_left(e.created_at, now, expiry_days)
            lines.append(f"#{e.id} from {label} · {ago}{left}: {e.message}")
        candidate = "\n".join(lines)
        if len(candidate.encode("utf-8")) <= max_bytes:
            candidate = _mail_reply_hint(candidate, "↩ REPLY #id <msg>", max_bytes)
            return candidate, [(e.id, e.message, len(e.message)) for e in subset]
    first = entries[0]
    tail = (labels or {}).get(first.sender_node_id, _node_id_tail(first.sender_node_id))
    tail = _fit_node_label(tail, max_bytes - len(f"#{first.id} : \nMore: MAIL".encode()) - 16)
    left = _mail_reply_time_left(first.created_at, now, expiry_days)
    rich_header = f"#{first.id} {tail}{left}: "
    plain_header = f"#{first.id} {tail}: "

    # The compact form can sometimes fit the whole body after the richer list
    # form (display label + age) did not. Do not reserve a continuation suffix
    # until one is actually needed.
    for header in dict.fromkeys((rich_header, plain_header)):
        compact = header + first.message
        if len(compact.encode("utf-8")) <= max_bytes:
            compact = _mail_reply_hint(
                compact, f"↩ REPLY #{first.id} <msg>", max_bytes
            )
            return compact, [(first.id, first.message, len(first.message))]

    suffix = "\nMore: MAIL"
    header = rich_header
    part = _truncate_utf8(
        first.message, max_bytes - len((header + suffix).encode("utf-8"))
    )
    if not part and header != plain_header:
        # On very small transports, retain the stable ID and body before the
        # expiry annotation. The configured deadline still governs lookup.
        header = plain_header
        part = _truncate_utf8(
            first.message, max_bytes - len((header + suffix).encode("utf-8"))
        )
    if not part:
        return _truncate_utf8("MAIL reply budget too small", max_bytes), []
    text = header + part + suffix
    text = _mail_reply_hint(text, f"↩ REPLY #{first.id} <msg>", max_bytes)
    return text, [(first.id, first.message, len(part))]


def mail_list_text(
    entries: list,
    *,
    now: int,
    max_bytes: int,
    labels: dict[str, str] | None = None,
    expiry_days: int | None = None,
) -> str:
    return mail_page(
        entries, now=now, max_bytes=max_bytes, labels=labels, expiry_days=expiry_days
    )[0]


def mail_list_shown_count(
    entries: list,
    *,
    now: int,
    max_bytes: int,
    labels: dict[str, str] | None = None,
    expiry_days: int | None = None,
) -> int:
    return len(
        mail_page(
            entries,
            now=now,
            max_bytes=max_bytes,
            labels=labels,
            expiry_days=expiry_days,
        )[1]
    )
