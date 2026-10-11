"""Strict TOML configuration loading for Meshgarden.

Configuration is validated eagerly and exhaustively: unknown keys, invalid
values, and violations of the hard-coded service policy (private by default, ack
required, at most three application-level retries) all raise ConfigError at load time.
Nothing here is silently coerced or downgraded to a warning.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import tomllib
import unicodedata
from dataclasses import dataclass, field, fields
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised for any invalid, missing, or unsupported configuration value."""


NODE_ID_RE = re.compile(r"^![0-9a-fA-F]{8}$")
LAB_UNLOCK_WORD_RE = re.compile(r"^[A-Za-z0-9]{6,24}$")
MAX_PAYLOAD_BYTES = 233  # meshtastic DATA_PAYLOAD_LEN, verified

# Accepted values for [service] daily_display. Only "haiku" remains: the
# key is still accepted so configs that carry it keep loading.
_DAILY_DISPLAY_CHOICES = frozenset({"haiku"})

_SECONDS_HOURS_DAYS_SUFFIXES = ("_seconds", "_hours", "_days")
_EXTRA_NON_NEGATIVE_INT_FIELDS = {
    "max_pending_replies",
    "guestbook_entries_returned",
    "max_guestbook_chars",
    "daily_sign_quota",
}


def node_id_from_num(num: int) -> str:
    return "!%08x" % (num & 0xFFFFFFFF)


def parse_node_id_argument(text: str) -> str | None:
    """A node id argument in the one shape every current transport accepts:
    a full "!xxxxxxxx" id (`NODE_ID_RE`), matched case-insensitively with
    surrounding whitespace stripped, canonicalized to lowercase -- or None
    when `text` is not one.

    This is the single definition `MeshtasticTransport.parse_node_argument`
    and `MeshCoreTransport.parse_node_argument` both delegate to (task 2A/
    3B: a MeshCore node id is format-compatible with `NODE_ID_RE`, so no
    separate short form was ever invented for it -- see those modules'
    docstrings). It is also what the operator CLI's node-id-validating
    subcommands (`cli_ops.py`'s `mail purge`/`lab revoke`/`quota reset`/
    `quota show`, `main.py`'s `block`/`unblock`) call directly: none of
    them construct a live `RadioAdapter`/`Transport`, so there is no
    `transport.parse_node_argument` to ask, and this keeps their
    validation as the exact same rule rather than a second copy that could
    drift (task 3D)."""
    t = text.strip()
    return t.lower() if NODE_ID_RE.match(t) else None


@dataclass(frozen=True)
class RadioConfig:
    device: str
    expected_node_id: str  # lowercase "!xxxxxxxx"
    # Additive, backward-compatible config (epic: MeshCore shared BBS,
    # scope-decisions.md section 4). `device` doubles as the MeshCore
    # serial port/device string when `transport == "meshcore"` -- no
    # separate key, since both transports need exactly one "where do I
    # connect" string. No existing key's meaning changes.
    transport: str = "meshtastic"  # "meshtastic" | "meshcore"
    # Name of the one MeshCore channel used for knocks and optional public
    # commands; "" disables channel interaction. Meshtastic ignores this key.
    meshcore_channel: str = "meshgarden"
    # When true, and ONLY then, the MeshCore adapter performs its single
    # permitted PERSISTED radio config write on every connection: a
    # read-modify-write of the firmware `autoadd_config` byte's bit 0x01
    # (meshcore-notes.md section 10). Default false: the adapter makes no
    # persisted config writes unless the operator opts in explicitly.
    # Independent of this flag, the adapter also sets the radio's volatile
    # clock from host time on every connection -- not configuration, and
    # not gated by anything, for the same reason the Meshtastic path
    # already does this unconditionally.
    meshcore_autoadd_overwrite: bool = False
    # Override for `Transport.max_text_bytes` on the MeshCore transport.
    # `None` means "use the transport's own conservative default"
    # (currently 120 -- transport-boundary-spec.md section 5); the real
    # figure is not established by any source until task 5A measures it on
    # hardware.
    meshcore_max_text_bytes: int | None = None
    # Reconnect/backoff tuning for `meshgarden.meshcore_client.
    # RealMeshCoreClient` (fix round 3: previously hardcoded, so only
    # tests could vary them -- scope-decisions.md section 4 is explicit
    # that MeshCore tuning is additive configuration). Defaults match the
    # client's own hardcoded defaults, so an unconfigured install sees no
    # behavior change. Meshtastic ignores all four.
    #
    # Seconds before the FIRST outer-loop retry after a failed connect, or
    # after a connected session that did not last
    # `meshcore_min_stable_session_seconds`; doubles on each further
    # consecutive failure/short session, up to `meshcore_backoff_max_
    # seconds`.
    meshcore_backoff_base_seconds: float = 1.0
    meshcore_backoff_max_seconds: float = 300.0
    # Passed to the library's own `MeshCore.create_serial(...,
    # max_reconnect_attempts=...)` -- how many times its INNER
    # `auto_reconnect` retries a transient mid-session drop (flat 1s delay
    # between tries) before giving up and letting the OUTER backoff above
    # rebuild the connection from scratch.
    meshcore_max_reconnect_attempts: int = 3
    # A connected session shorter than this does not reset the outer
    # backoff counter -- a session that connects and immediately drops
    # again keeps escalating like any other repeated failure, rather than
    # retrying at the base delay forever.
    meshcore_min_stable_session_seconds: float = 30.0
    # Task 3B-3 (scope-decisions.md section 7, transport-boundary-spec.md
    # section 3.2): how long a single resolved recipient's channel-knock
    # invite DM is rate-limited for, in seconds. Additive; does not change
    # what any existing key means. Default matches `ChannelTriggerPolicy`'s
    # default (60 seconds -- once per minute).
    meshcore_knock_cooldown_seconds: int = 60
    # Task 3C finding 2: a burst of forged knocks (adverts are
    # unauthenticated, so an attacker mints as many distinct roster
    # entries as it likes) can fill the shared reply queue with knock DMs,
    # starving genuine visitors' replies -- the per-recipient cooldown
    # above does not bound this, since each forged name is a different
    # recipient. This caps how many knock replies may be concurrently
    # queued/in-flight, SEPARATELY from `[service] max_pending_replies`
    # (which still bounds the queue overall). Default 1: at most one knock
    # DM in flight at a time, system-wide, leaving the rest of the shared
    # queue's capacity for real commands under an attack.
    meshcore_max_pending_knocks: int = 1
    # Task 3B-4 fix 2 (hardware finding: the garden was unreachable until
    # manually flood-advertised): how often, in seconds, the MeshCore
    # adapter repeats its self-advertisement (`commands.send_advert(flood=
    # True)`) beyond the one it already sends on every successful
    # connection -- a node that never heard an earlier advert cannot
    # resolve a path to this radio, so a DM to it (even an otherwise-
    # correct reply) can fail to route. Default 14400 (4 hours). 0 disables
    # the periodic repeat only; the per-connection advert is unconditional.
    # This is a real over-the-air transmission, not a reply -- see
    # docs/security-and-privacy.md and docs/architecture.md for the
    # documented exception to request-driven transmissions. Ignored by
    # Meshtastic, whose firmware already beacons on its own.
    meshcore_advert_interval_seconds: int = 14_400
    # Daily contact-table housekeeping (second persisted radio write; opt-in).
    # A radio at its contact cap refuses to auto-add newcomers, so the garden
    # goes deaf to strangers. When enabled, once per local day at or after
    # `meshcore_contact_prune_hour` the daemon removes least-recently-heard
    # contacts until at most `meshcore_contact_target` remain. Never removes
    # a contact that ever sent the garden a command or mail, holds a LAB
    # unlock or mail prefs, or is a repeater/room. Ignored by Meshtastic.
    meshcore_contact_prune_enabled: bool = False
    meshcore_contact_target: int = 300
    meshcore_contact_prune_hour: int = 4  # local hour in [service] timezone
    meshcore_contact_prune_max_per_day: int = 50
    # Optional remote repeater queried by the REPEATER command. Both
    # values must be supplied together.  The password is an operator secret
    # and must live only in the private deployment config.
    meshcore_repeater_public_key: str = ""
    meshcore_repeater_guest_password: str = field(default="", repr=False)
    meshcore_repeater_cache_seconds: int = 300
    meshcore_repeater_query_timeout_seconds: int = 15


@dataclass(frozen=True)
class ServiceConfig:
    name: str = "Meshgarden Guestbook"
    # dm_only retains the channel-knock invitation. channel additionally
    # executes recognized commands received on the configured MeshCore channel
    # and replies there; private/state-changing commands remain DM-only.
    mode: str = "dm_only"
    want_ack: bool = True  # ONLY True accepted
    # Three delayed retries, spaced 15 seconds apart, recover from
    # lossy MeshCore DMs without turning a weak path into a rapid repeat loop.
    application_retries: int = 3
    application_retry_delay_seconds: int = 15
    mobile_dm_recovery_enabled: bool = False
    mobile_dm_recovery_destinations: list[str] = field(default_factory=list)
    mobile_dm_recovery_cooldown_seconds: int = 300
    mobile_dm_global_recoveries_per_hour: int = 6
    mobile_dm_recovery_min_interval_seconds: int = 30
    mobile_dm_max_retries: int = 1
    max_guestbook_chars: int = 100
    guestbook_entries_returned: int = 3
    max_response_bytes: int = 180
    per_node_cooldown_seconds: int = 30
    global_tx_cooldown_seconds: int = 5
    # Seconds a reply ON THE CHANNEL waits before transmission, so the
    # request's own flood (repeaters re-sending it) has died down. 0 sends
    # immediately. DMs are never delayed. Hardware finding 2026-10-10: with a
    # repeater on the roof, 3 s still collided with its re-flood; 6 s did not.
    channel_reply_delay_seconds: int = 6
    sign_cooldown_seconds: int = 3600
    reply_ttl_seconds: int = 60
    max_pending_replies: int = 4
    request_dedup_hours: int = 24
    identical_sign_dedup_hours: int = 24
    # Age past which a 'submitted' request (sent, but never resolved to
    # acked/failed by a matching ROUTING packet) is aged to 'unknown' by the
    # DB worker's maintenance tick, so it becomes terminal and prunable
    # instead of growing processed_requests forever.
    ack_timeout_seconds: int = 300
    # Optional word used by HAIKU templates in place of their own generic
    # default (e.g. "the mesh") when set. Sanitized like SIGN text; never a
    # real place name by default -- purely operator-configured.
    haiku_place: str = ""
    # IANA zone name (validated via zoneinfo.ZoneInfo) used to compute the
    # local calendar day ICHING's per-sender-per-day hexagram is keyed on.
    timezone: str = "UTC"
    # Optional secret mixed into ICHING's hexagram derivation so a visitor
    # cannot compute another sender's draw from their node id and the date
    # alone.
    fortune_salt: str = ""
    # Which daily draw a display feed (/api/daily.json) shows. "haiku" is
    # the only value; the key stays accepted for configs that already set it.
    daily_display: str = "haiku"
    # Maximum accepted commands per node per local day before the quota
    # module silently drops further commands (no reply).
    daily_command_quota: int = 60
    # Maximum new SIGN entries per node per local day before the quota
    # module replies with a "Daily sign limit reached" message instead of
    # recording another entry.
    daily_sign_quota: int = 3
    # New in the directory/boards/mail epic. seen_enabled lets an operator
    # turn off SEEN alone; the other three commands have no such toggle.
    seen_enabled: bool = True
    daily_post_quota: int = 5
    daily_mail_quota: int = 3
    max_topics: int = 50
    mail_pending_limit: int = 5
    mail_public: bool = True
    # Operator-configured word that unlocks the LAB tier (POST/READ/BOARDS/
    # MAIL) for the node that DMs it, one time. "" (default) means the LAB
    # tier is off entirely -- no node can unlock it. Never logged, never
    # echoed in a reply, never printed by any CLI/doctor output.
    lab_unlock_word: str = ""


@dataclass(frozen=True)
class DatabaseConfig:
    path: str


@dataclass(frozen=True)
class RetentionConfig:
    observations_days: int = 7
    aggregates_days: int = 90
    guestbook_expiry_days: int = 0  # 0 = no automatic expiry
    max_managed_storage_mib: int = 1024
    # Days node_daily rollups are kept before pruning.
    node_daily_days: int = 365
    # Days since a links row's last_seen before it is pruned.
    links_days: int = 30
    # Days an undelivered MAIL note is kept before the maintenance tick
    # prunes it (delivered notes are deleted immediately at delivery time).
    mail_expiry_days: int = 7


@dataclass(frozen=True)
class BackupConfig:
    directory: str
    interval_hours: int = 24
    keep: int = 7


@dataclass(frozen=True)
class MetricsConfig:
    listen: str = "127.0.0.1"
    port: int = 9108


@dataclass(frozen=True)
class ObservatoryConfig:
    # Operator-set observatory location, used only to compute distances
    # (haversine) for GARDEN/RECORD's "furthest direct"/"furthest observed"
    # fields. (0.0, 0.0) is the "unset" sentinel -- an operator sitting
    # exactly on Null Island is not a real-world case this loses -- and
    # distance features report "n/a" until both are set to something else.
    latitude: float = 0.0
    longitude: float = 0.0
    # Whether POSITION_APP fields are persisted at all (node_positions rows,
    # nodes.last_position_*, node_daily.last_lat/lon). Positions are always
    # voluntarily advertised by the sending node; this is a local capture
    # toggle only, never something Meshgarden broadcasts.
    enabled_position_capture: bool = True
    # Whether a DM of the form "Saved position: lat, lon" / "GPS position:
    # lat, lon" (the MeshCore app's location share) is recorded in
    # dm_positions and acknowledged. Off by default: it is the one case
    # where coordinates typed into a DM are persisted.
    dm_position_capture: bool = False
    # Distance-plausibility thresholds (see collector.is_distance_plausible_
    # for_hops): a packet's distance-from-observatory must fit its derived
    # hop count to be eligible for "furthest direct"/"furthest observed".
    # max_direct_km bounds hops_away == 0; max_km_per_hop scales the bound
    # per additional hop; max_observed_km is the absolute ceiling regardless
    # of hop count. All three must be > 0.
    max_direct_km: float = 400.0
    max_km_per_hop: float = 150.0
    max_observed_km: float = 1000.0


@dataclass(frozen=True)
class WebConfig:
    # Read-only local dashboard (batch 3a). Disabled by default: opting in
    # is an explicit operator choice, same convention as metrics.listen but
    # stricter -- see _parse_web and web.start_web_server, which REFUSE to
    # bind a non-loopback address unless allow_lan is explicitly set (metrics
    # only warns, since /metrics has no page rendering / guestbook text to
    # leak).
    enabled: bool = False
    listen: str = "127.0.0.1"
    port: int = 9109
    allow_lan: bool = False


_WEATHER_SEVERITIES = ("Minor", "Moderate", "Severe", "Extreme")
_WEATHER_COUNTY_RE = re.compile(r"^[A-Z]{2}C\d{3}$")
_DEFAULT_WEATHER_COUNTIES = (
    "CAC001", "CAC013", "CAC041", "CAC055", "CAC075",
    "CAC081", "CAC085", "CAC095", "CAC097",
)


@dataclass(frozen=True)
class WeatherConfig:
    # Off by default: no thread starts and nothing below is read while False.
    enabled: bool = False
    # Public MeshCore hashtag channel, matched exactly against the radio's
    # channel list at connect. Must differ from radio.meshcore_channel.
    channel: str = "#wx-bay"
    # NWS requires an identifying User-Agent; this goes in it. Required
    # when enabled.
    contact: str = ""
    counties: tuple[str, ...] = _DEFAULT_WEATHER_COUNTIES
    event_suffixes: tuple[str, ...] = ("Warning", "Watch")
    min_severity: str = "Moderate"
    poll_interval_seconds: int = 120
    max_sends_per_hour: int = 6
    # Local hour (service.timezone) of the daily "feed is alive" line on the
    # alert channel; -1 disables it.
    heartbeat_hour: int = -1
    # Overrides/additions to weather.DEFAULT_ZONE_ALIASES (UGC zone -> short name).
    zone_aliases: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModerationConfig:
    # Path to a banned-terms file (one term per line, "#" comments); empty
    # string disables the SIGN banned-word guard. When non-empty, the file
    # must exist and be readable at config load time.
    banned_words_file: str = ""


@dataclass(frozen=True)
class Config:
    radio: RadioConfig
    service: ServiceConfig
    database: DatabaseConfig
    retention: RetentionConfig
    backup: BackupConfig
    metrics: MetricsConfig
    # Defaulted (rather than required) so existing direct `Config(...)`
    # construction elsewhere (tests/conftest.py's fixture, in particular)
    # keeps working unchanged -- parse_config always passes this
    # explicitly.
    moderation: ModerationConfig = field(default_factory=ModerationConfig)
    observatory: ObservatoryConfig = field(default_factory=ObservatoryConfig)
    web: WebConfig = field(default_factory=WebConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)


def _reject_unknown_keys(table: dict, cls: type, table_name: str) -> None:
    # A non-table value (e.g. `radio = 5`) would otherwise raise TypeError
    # from set(table) below instead of the documented ConfigError.
    if not isinstance(table, dict):
        raise ConfigError(f"[{table_name}] must be a table")
    allowed = {f.name for f in fields(cls)}
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"unknown key(s) in [{table_name}]: {sorted(unknown)}")


def _check_non_negative_ints(values: dict, table_name: str) -> None:
    for key, value in values.items():
        is_tracked = (
            key.endswith(_SECONDS_HOURS_DAYS_SUFFIXES) or key in _EXTRA_NON_NEGATIVE_INT_FIELDS
        )
        if not is_tracked:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"{table_name}.{key} must be a non-negative int (got {value!r})")


def _require_int_in_range(value: object, lo: int, hi: int, table_name: str, key: str) -> None:
    """Type-safe bound check: rejects non-int, bool, and out-of-range values alike.

    TOML can hand back a str (e.g. a quoted number) for any field; without an
    isinstance guard a bare `lo <= value <= hi` comparison raises TypeError
    instead of the documented ConfigError. bool is excluded even though it is
    an int subclass, since True/False are not valid values for these fields.
    """
    if not isinstance(value, int) or isinstance(value, bool) or not (lo <= value <= hi):
        raise ConfigError(
            f"{table_name}.{key} must be an int between {lo} and {hi} (got {value!r})"
        )


def _require_int_at_least(value: object, lo: int, table_name: str, key: str) -> None:
    """Same guard as _require_int_in_range, but with no upper bound."""
    if not isinstance(value, int) or isinstance(value, bool) or value < lo:
        raise ConfigError(f"{table_name}.{key} must be an int >= {lo} (got {value!r})")


def _require_str(value: object, table_name: str, key: str) -> None:
    """Type-safe string check, same convention as the isinstance guards above."""
    if not isinstance(value, str):
        raise ConfigError(f"{table_name}.{key} must be a string (got {value!r})")


_STRIPPED_TEXT_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def _sanitize_config_text(raw: str) -> str:
    """NFC normalize, strip Cc/Cf/Cs/Co/Cn characters, collapse whitespace
    runs to a single U+0020, strip ends.

    A local copy of guestbook.normalize_sign_text's logic: config.py cannot
    import guestbook.py (guestbook.py already imports config.py), so this
    tiny helper is duplicated here rather than restructured across a
    package boundary -- same pattern as commands.py's own
    `_truncate_utf8`.
    """
    s = unicodedata.normalize("NFC", raw)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in _STRIPPED_TEXT_CATEGORIES)
    return " ".join(s.split())


_RADIO_TRANSPORTS = frozenset({"meshtastic", "meshcore"})


def _parse_radio(table: dict) -> RadioConfig:
    _reject_unknown_keys(table, RadioConfig, "radio")
    if "device" not in table:
        raise ConfigError("radio.device is required")
    if "expected_node_id" not in table:
        raise ConfigError("radio.expected_node_id is required")
    device = table["device"]
    _require_str(device, "radio", "device")
    node_id = table["expected_node_id"]
    if not isinstance(node_id, str) or not NODE_ID_RE.match(node_id):
        raise ConfigError(
            f"radio.expected_node_id must match {NODE_ID_RE.pattern!r} (got {node_id!r})"
        )
    transport = table.get("transport", "meshtastic")
    # An unhashable transport (e.g. a list) would otherwise raise TypeError
    # from the `in` check against a frozenset below.
    if not isinstance(transport, str) or transport not in _RADIO_TRANSPORTS:
        raise ConfigError(
            f"radio.transport must be one of {sorted(_RADIO_TRANSPORTS)} (got {transport!r})"
        )
    meshcore_channel = table.get("meshcore_channel", "meshgarden")
    if not isinstance(meshcore_channel, str):
        raise ConfigError(f"radio.meshcore_channel must be a string (got {meshcore_channel!r})")
    meshcore_autoadd_overwrite = table.get("meshcore_autoadd_overwrite", False)
    if not isinstance(meshcore_autoadd_overwrite, bool):
        raise ConfigError(
            f"radio.meshcore_autoadd_overwrite must be a bool (got {meshcore_autoadd_overwrite!r})"
        )
    meshcore_max_text_bytes = table.get("meshcore_max_text_bytes")
    if meshcore_max_text_bytes is not None:
        _require_int_at_least(meshcore_max_text_bytes, 1, "radio", "meshcore_max_text_bytes")
    meshcore_backoff_base_seconds = _require_positive_number(
        table.get("meshcore_backoff_base_seconds", 1.0),
        "radio",
        "meshcore_backoff_base_seconds",
    )
    meshcore_backoff_max_seconds = _require_positive_number(
        table.get("meshcore_backoff_max_seconds", 300.0),
        "radio",
        "meshcore_backoff_max_seconds",
    )
    meshcore_max_reconnect_attempts = table.get("meshcore_max_reconnect_attempts", 3)
    _require_int_at_least(
        meshcore_max_reconnect_attempts, 1, "radio", "meshcore_max_reconnect_attempts"
    )
    meshcore_min_stable_session_seconds = _require_positive_number(
        table.get("meshcore_min_stable_session_seconds", 30.0),
        "radio",
        "meshcore_min_stable_session_seconds",
    )
    meshcore_knock_cooldown_seconds = table.get("meshcore_knock_cooldown_seconds", 60)
    _require_int_at_least(
        meshcore_knock_cooldown_seconds, 1, "radio", "meshcore_knock_cooldown_seconds"
    )
    meshcore_max_pending_knocks = table.get("meshcore_max_pending_knocks", 1)
    _require_int_at_least(meshcore_max_pending_knocks, 1, "radio", "meshcore_max_pending_knocks")
    meshcore_advert_interval_seconds = table.get("meshcore_advert_interval_seconds", 14_400)
    _require_int_at_least(
        meshcore_advert_interval_seconds, 0, "radio", "meshcore_advert_interval_seconds"
    )
    prune_enabled = table.get("meshcore_contact_prune_enabled", False)
    if not isinstance(prune_enabled, bool):
        raise ConfigError(
            f"radio.meshcore_contact_prune_enabled must be a bool (got {prune_enabled!r})"
        )
    prune_target = table.get("meshcore_contact_target", 300)
    _require_int_in_range(prune_target, 1, 10_000, "radio", "meshcore_contact_target")
    prune_hour = table.get("meshcore_contact_prune_hour", 4)
    _require_int_in_range(prune_hour, 0, 23, "radio", "meshcore_contact_prune_hour")
    prune_max = table.get("meshcore_contact_prune_max_per_day", 50)
    _require_int_in_range(prune_max, 1, 1000, "radio", "meshcore_contact_prune_max_per_day")
    repeater_key = table.get("meshcore_repeater_public_key", "")
    repeater_password = table.get("meshcore_repeater_guest_password", "")
    _require_str(repeater_key, "radio", "meshcore_repeater_public_key")
    _require_str(repeater_password, "radio", "meshcore_repeater_guest_password")
    if repeater_key and not re.fullmatch(r"[0-9A-Fa-f]{64}", repeater_key):
        raise ConfigError("radio.meshcore_repeater_public_key must be exactly 64 hex characters")
    if bool(repeater_key) != bool(repeater_password):
        raise ConfigError(
            "radio.meshcore_repeater_public_key and "
            "radio.meshcore_repeater_guest_password must be set together"
        )
    if len(repeater_password.encode("utf-8")) > 64:
        raise ConfigError("radio.meshcore_repeater_guest_password must be at most 64 UTF-8 bytes")
    repeater_cache = table.get("meshcore_repeater_cache_seconds", 300)
    repeater_timeout = table.get("meshcore_repeater_query_timeout_seconds", 15)
    _require_int_at_least(repeater_cache, 1, "radio", "meshcore_repeater_cache_seconds")
    _require_int_at_least(repeater_timeout, 1, "radio", "meshcore_repeater_query_timeout_seconds")
    return RadioConfig(
        device=device,
        expected_node_id=node_id.lower(),
        transport=transport,
        meshcore_channel=meshcore_channel,
        meshcore_autoadd_overwrite=meshcore_autoadd_overwrite,
        meshcore_max_text_bytes=meshcore_max_text_bytes,
        meshcore_backoff_base_seconds=meshcore_backoff_base_seconds,
        meshcore_backoff_max_seconds=meshcore_backoff_max_seconds,
        meshcore_max_reconnect_attempts=meshcore_max_reconnect_attempts,
        meshcore_min_stable_session_seconds=meshcore_min_stable_session_seconds,
        meshcore_knock_cooldown_seconds=meshcore_knock_cooldown_seconds,
        meshcore_max_pending_knocks=meshcore_max_pending_knocks,
        meshcore_advert_interval_seconds=meshcore_advert_interval_seconds,
        meshcore_contact_prune_enabled=prune_enabled,
        meshcore_contact_target=prune_target,
        meshcore_contact_prune_hour=prune_hour,
        meshcore_contact_prune_max_per_day=prune_max,
        meshcore_repeater_public_key=repeater_key.upper(),
        meshcore_repeater_guest_password=repeater_password,
        meshcore_repeater_cache_seconds=repeater_cache,
        meshcore_repeater_query_timeout_seconds=repeater_timeout,
    )


def _parse_service(table: dict) -> ServiceConfig:
    _reject_unknown_keys(table, ServiceConfig, "service")
    defaults = ServiceConfig()
    kwargs = {f.name: table.get(f.name, getattr(defaults, f.name)) for f in fields(ServiceConfig)}

    destinations = kwargs["mobile_dm_recovery_destinations"]
    if not isinstance(destinations, (list, tuple)) or any(
        not isinstance(node, str) or not NODE_ID_RE.fullmatch(node.strip()) for node in destinations
    ):
        raise ConfigError(
            "service.mobile_dm_recovery_destinations must be an array of !xxxxxxxx node ids"
        )
    kwargs["mobile_dm_recovery_destinations"] = list(
        dict.fromkeys(node.strip().lower() for node in destinations)
    )

    _require_str(kwargs["name"], "service", "name")
    if kwargs["mode"] not in ("dm_only", "channel"):
        raise ConfigError(f"service.mode must be 'dm_only' or 'channel' (got {kwargs['mode']!r})")
    if kwargs["want_ack"] is not True:
        raise ConfigError(
            f"service.want_ack must be true (got {kwargs['want_ack']!r}); "
            "the spec requires want_ack=True for every outgoing message"
        )
    if not isinstance(kwargs["mobile_dm_recovery_enabled"], bool):
        raise ConfigError("service.mobile_dm_recovery_enabled must be a bool")
    _require_int_at_least(
        kwargs["mobile_dm_recovery_cooldown_seconds"],
        300,
        "service",
        "mobile_dm_recovery_cooldown_seconds",
    )
    _require_int_in_range(
        kwargs["mobile_dm_global_recoveries_per_hour"],
        1,
        6,
        "service",
        "mobile_dm_global_recoveries_per_hour",
    )
    _require_int_at_least(
        kwargs["mobile_dm_recovery_min_interval_seconds"],
        30,
        "service",
        "mobile_dm_recovery_min_interval_seconds",
    )
    _require_int_in_range(kwargs["mobile_dm_max_retries"], 0, 1, "service", "mobile_dm_max_retries")
    _require_int_in_range(
        kwargs["channel_reply_delay_seconds"], 0, 30, "service", "channel_reply_delay_seconds"
    )
    _require_int_in_range(kwargs["application_retries"], 0, 3, "service", "application_retries")
    _require_int_at_least(
        kwargs["application_retry_delay_seconds"],
        1,
        "service",
        "application_retry_delay_seconds",
    )
    _require_int_in_range(
        kwargs["max_response_bytes"], 1, MAX_PAYLOAD_BYTES, "service", "max_response_bytes"
    )
    _require_int_in_range(
        kwargs["max_guestbook_chars"], 1, MAX_PAYLOAD_BYTES, "service", "max_guestbook_chars"
    )
    _require_int_at_least(
        kwargs["guestbook_entries_returned"], 1, "service", "guestbook_entries_returned"
    )
    _require_int_at_least(kwargs["max_pending_replies"], 1, "service", "max_pending_replies")
    _require_int_at_least(kwargs["ack_timeout_seconds"], 1, "service", "ack_timeout_seconds")
    retry_window = kwargs["application_retry_delay_seconds"] * kwargs["application_retries"]
    if retry_window >= kwargs["ack_timeout_seconds"]:
        raise ConfigError(
            "service.application_retry_delay_seconds retry window must be less than "
            "service.ack_timeout_seconds"
        )
    _require_int_at_least(kwargs["daily_command_quota"], 1, "service", "daily_command_quota")

    if not isinstance(kwargs["seen_enabled"], bool):
        raise ConfigError(f"service.seen_enabled must be a bool (got {kwargs['seen_enabled']!r})")
    if not isinstance(kwargs["mail_public"], bool):
        raise ConfigError("service.mail_public must be a bool")
    _require_int_at_least(kwargs["daily_post_quota"], 0, "service", "daily_post_quota")
    _require_int_at_least(kwargs["daily_mail_quota"], 0, "service", "daily_mail_quota")
    _require_int_at_least(kwargs["max_topics"], 1, "service", "max_topics")
    _require_int_at_least(kwargs["mail_pending_limit"], 1, "service", "mail_pending_limit")

    # The raw value is never echoed in these errors (same convention as
    # fortune_salt's own validation below) -- it is operator-secret, and a
    # ConfigError can end up pasted into a bug report or support channel.
    if not isinstance(kwargs["lab_unlock_word"], str):
        raise ConfigError("service.lab_unlock_word must be a string")
    if kwargs["lab_unlock_word"] != "":
        stripped_word = kwargs["lab_unlock_word"].strip()
        if not LAB_UNLOCK_WORD_RE.match(stripped_word):
            raise ConfigError(
                f"service.lab_unlock_word must match {LAB_UNLOCK_WORD_RE.pattern!r} "
                "after stripping whitespace"
            )
        # A word equal to an existing command word would silently disable
        # that command in parse_command's dispatch (the unlock-word branch
        # is checked before the ordinary command tables) -- refuse it here
        # instead. Imported locally (not at module level) to avoid a
        # config.py <-> commands.py import cycle.
        from meshgarden.commands import _BARE_COMMANDS, _KNOWN_COMMAND_WORDS

        conflicts_with_a_command = (
            stripped_word.upper() in _KNOWN_COMMAND_WORDS or stripped_word.upper() in _BARE_COMMANDS
        )
        if conflicts_with_a_command:
            # Never echo the word itself here -- same operator-secret
            # convention as the shape-mismatch error above.
            raise ConfigError("service.lab_unlock_word must not be an existing command word")
        kwargs["lab_unlock_word"] = stripped_word

    if not isinstance(kwargs["haiku_place"], str):
        raise ConfigError(f"service.haiku_place must be a string (got {kwargs['haiku_place']!r})")
    sanitized_place = _sanitize_config_text(kwargs["haiku_place"])
    if len(sanitized_place) > 32:
        raise ConfigError(
            f"service.haiku_place must be <=32 characters after sanitizing "
            f"(got {len(sanitized_place)})"
        )
    kwargs["haiku_place"] = sanitized_place

    if not isinstance(kwargs["fortune_salt"], str):
        raise ConfigError(f"service.fortune_salt must be a string (got {kwargs['fortune_salt']!r})")
    if len(kwargs["fortune_salt"]) > 64:
        raise ConfigError(
            f"service.fortune_salt must be <=64 characters (got {len(kwargs['fortune_salt'])})"
        )

    if kwargs["daily_display"] not in _DAILY_DISPLAY_CHOICES:
        raise ConfigError(
            f"service.daily_display must be one of "
            f"{sorted(_DAILY_DISPLAY_CHOICES)} (got {kwargs['daily_display']!r})"
        )

    if not isinstance(kwargs["timezone"], str):
        raise ConfigError(f"service.timezone must be a string (got {kwargs['timezone']!r})")
    try:
        ZoneInfo(kwargs["timezone"])
    except Exception as exc:
        raise ConfigError(
            f"service.timezone is not a known IANA zone: {kwargs['timezone']!r}"
        ) from exc

    _check_non_negative_ints(kwargs, "service")
    return ServiceConfig(**kwargs)


def _parse_database(table: dict) -> DatabaseConfig:
    _reject_unknown_keys(table, DatabaseConfig, "database")
    if "path" not in table:
        raise ConfigError("database.path is required")
    _require_str(table["path"], "database", "path")
    return DatabaseConfig(path=table["path"])


def _parse_retention(table: dict) -> RetentionConfig:
    _reject_unknown_keys(table, RetentionConfig, "retention")
    defaults = RetentionConfig()
    kwargs = {f.name: table.get(f.name, getattr(defaults, f.name)) for f in fields(RetentionConfig)}
    _check_non_negative_ints(kwargs, "retention")
    _require_int_at_least(kwargs["mail_expiry_days"], 1, "retention", "mail_expiry_days")
    # An unvalidated max_managed_storage_mib (e.g. a string) raises TypeError
    # in storage_report's comparison instead of the documented ConfigError.
    _require_int_at_least(
        kwargs["max_managed_storage_mib"], 1, "retention", "max_managed_storage_mib"
    )
    if kwargs["guestbook_expiry_days"] != 0:
        # Spec: guestbook entries are kept until the operator deletes them.
        # A nonzero value was previously accepted but never wired to any
        # expiry logic -- silently a no-op rather than behaving as
        # configured. v1 only supports 0 (no automatic expiry); this is
        # tightened validation, not a behavior change, since nonzero never
        # worked in the first place.
        raise ConfigError("guestbook_expiry_days: only 0 (no automatic expiry) is supported in v1")
    return RetentionConfig(**kwargs)


def _parse_backup(table: dict) -> BackupConfig:
    _reject_unknown_keys(table, BackupConfig, "backup")
    if "directory" not in table:
        raise ConfigError("backup.directory is required")
    _require_str(table["directory"], "backup", "directory")
    defaults = BackupConfig(directory=table["directory"])
    kwargs = {f.name: table.get(f.name, getattr(defaults, f.name)) for f in fields(BackupConfig)}
    _check_non_negative_ints(kwargs, "backup")
    # keep must be >= 1: _rotate_backups treats keep as "how many backups to
    # retain" and a keep=0 (or unvalidated string) would delete the backup
    # just written instead of raising a config error at startup.
    _require_int_at_least(kwargs["keep"], 1, "backup", "keep")
    return BackupConfig(**kwargs)


def _parse_metrics(table: dict) -> MetricsConfig:
    _reject_unknown_keys(table, MetricsConfig, "metrics")
    defaults = MetricsConfig()
    kwargs = {f.name: table.get(f.name, getattr(defaults, f.name)) for f in fields(MetricsConfig)}
    _require_int_in_range(kwargs["port"], 1, 65535, "metrics", "port")

    listen = kwargs["listen"]
    try:
        is_loopback = ipaddress.ip_address(listen).is_loopback
    except ValueError:
        is_loopback = listen == "localhost"
    if not is_loopback:
        logger.warning(
            "metrics.listen=%r is not a loopback address; the metrics endpoint will be "
            "reachable beyond localhost",
            listen,
        )
    return MetricsConfig(**kwargs)


def _parse_web(table: dict) -> WebConfig:
    _reject_unknown_keys(table, WebConfig, "web")
    defaults = WebConfig()
    kwargs = {f.name: table.get(f.name, getattr(defaults, f.name)) for f in fields(WebConfig)}
    if not isinstance(kwargs["enabled"], bool):
        raise ConfigError(f"web.enabled must be a bool (got {kwargs['enabled']!r})")
    if not isinstance(kwargs["allow_lan"], bool):
        raise ConfigError(f"web.allow_lan must be a bool (got {kwargs['allow_lan']!r})")
    if not isinstance(kwargs["listen"], str):
        raise ConfigError(f"web.listen must be a string (got {kwargs['listen']!r})")
    _require_int_in_range(kwargs["port"], 1, 65535, "web", "port")
    return WebConfig(**kwargs)


def _parse_moderation(table: dict) -> ModerationConfig:
    _reject_unknown_keys(table, ModerationConfig, "moderation")
    defaults = ModerationConfig()
    kwargs = {
        f.name: table.get(f.name, getattr(defaults, f.name)) for f in fields(ModerationConfig)
    }
    path = kwargs["banned_words_file"]
    if not isinstance(path, str):
        raise ConfigError(f"moderation.banned_words_file must be a string (got {path!r})")
    if path == "default":
        # The special value means "use the list packaged with Meshgarden"
        # (see moderation.load_default_banned_terms) -- not a filesystem
        # path, so the readability check below does not apply. Still
        # validated eagerly here, same as every other config value.
        from meshgarden.moderation import load_default_banned_terms

        try:
            load_default_banned_terms()
        except OSError as exc:
            raise ConfigError(
                f"moderation.banned_words_file='default' failed to load the packaged list: {exc}"
            ) from exc
    elif path:
        try:
            with open(path, "rb") as f:
                f.read(0)
        except OSError as exc:
            raise ConfigError(
                f"moderation.banned_words_file is not readable: {path!r} ({exc})"
            ) from exc
    return ModerationConfig(**kwargs)


def _require_number_in_range(
    value: object, lo: float, hi: float, table_name: str, key: str
) -> float:
    """Same guard shape as _require_int_in_range, but accepts int OR float
    (TOML renders e.g. `latitude = 0` as an int) and returns a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (lo <= value <= hi):
        raise ConfigError(
            f"{table_name}.{key} must be a number between {lo} and {hi} (got {value!r})"
        )
    return float(value)


def _require_positive_number(value: object, table_name: str, key: str) -> float:
    """Same guard shape as `_require_number_in_range`, but with no upper
    bound and a strict `> 0` floor (0 or negative is never valid) -- used
    for the observatory distance-plausibility thresholds, which would
    silently exclude every packet (or admit implausible ones) if zero or
    negative."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{table_name}.{key} must be a positive number (got {value!r})")
    return float(value)


def _parse_observatory(table: dict) -> ObservatoryConfig:
    _reject_unknown_keys(table, ObservatoryConfig, "observatory")
    defaults = ObservatoryConfig()
    kwargs = {
        f.name: table.get(f.name, getattr(defaults, f.name)) for f in fields(ObservatoryConfig)
    }
    kwargs["latitude"] = _require_number_in_range(
        kwargs["latitude"], -90.0, 90.0, "observatory", "latitude"
    )
    kwargs["longitude"] = _require_number_in_range(
        kwargs["longitude"], -180.0, 180.0, "observatory", "longitude"
    )
    if not isinstance(kwargs["enabled_position_capture"], bool):
        raise ConfigError(
            "observatory.enabled_position_capture must be a bool "
            f"(got {kwargs['enabled_position_capture']!r})"
        )
    if not isinstance(kwargs["dm_position_capture"], bool):
        raise ConfigError(
            "observatory.dm_position_capture must be a bool "
            f"(got {kwargs['dm_position_capture']!r})"
        )
    kwargs["max_direct_km"] = _require_positive_number(
        kwargs["max_direct_km"], "observatory", "max_direct_km"
    )
    kwargs["max_km_per_hop"] = _require_positive_number(
        kwargs["max_km_per_hop"], "observatory", "max_km_per_hop"
    )
    kwargs["max_observed_km"] = _require_positive_number(
        kwargs["max_observed_km"], "observatory", "max_observed_km"
    )
    return ObservatoryConfig(**kwargs)


def _parse_weather(table: dict) -> WeatherConfig:
    _reject_unknown_keys(table, WeatherConfig, "weather")
    defaults = WeatherConfig()
    kwargs = {f.name: table.get(f.name, getattr(defaults, f.name)) for f in fields(WeatherConfig)}
    if not isinstance(kwargs["enabled"], bool):
        raise ConfigError(f"weather.enabled must be a bool (got {kwargs['enabled']!r})")
    for key in ("channel", "contact", "min_severity"):
        _require_str(kwargs[key], "weather", key)
    for key in ("counties", "event_suffixes"):
        value = kwargs[key]
        if not isinstance(value, list | tuple) or not all(
            isinstance(v, str) and v for v in value
        ):
            raise ConfigError(f"weather.{key} must be a list of non-empty strings")
        kwargs[key] = tuple(value)
    for county in kwargs["counties"]:
        if not _WEATHER_COUNTY_RE.match(county):
            raise ConfigError(f"weather.counties entry {county!r} is not a UGC county code")
    if kwargs["min_severity"] not in _WEATHER_SEVERITIES:
        raise ConfigError(
            f"weather.min_severity must be one of {list(_WEATHER_SEVERITIES)} "
            f"(got {kwargs['min_severity']!r})"
        )
    _require_int_at_least(kwargs["poll_interval_seconds"], 30, "weather", "poll_interval_seconds")
    _require_int_at_least(kwargs["max_sends_per_hour"], 1, "weather", "max_sends_per_hour")
    hb = kwargs["heartbeat_hour"]
    if isinstance(hb, bool) or not isinstance(hb, int) or not -1 <= hb <= 23:
        raise ConfigError("weather.heartbeat_hour must be -1 or 0..23")
    aliases = kwargs["zone_aliases"]
    if not isinstance(aliases, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in aliases.items()
    ):
        raise ConfigError("weather.zone_aliases must be a table of zone -> non-empty string")
    kwargs["zone_aliases"] = dict(aliases)
    if kwargs["enabled"]:
        for key in ("channel", "contact"):
            if not kwargs[key]:
                raise ConfigError(f"weather.{key} is required when weather.enabled = true")
        if not kwargs["counties"]:
            raise ConfigError("weather.counties must not be empty when weather.enabled = true")
        if not kwargs["event_suffixes"]:
            raise ConfigError(
                "weather.event_suffixes must not be empty when weather.enabled = true"
            )
    return WeatherConfig(**kwargs)


_TOP_LEVEL_SECTIONS = {
    "radio",
    "service",
    "database",
    "retention",
    "backup",
    "metrics",
    "moderation",
    "observatory",
    "web",
    "weather",
}


def parse_config(data: dict) -> Config:
    unknown = set(data) - _TOP_LEVEL_SECTIONS
    if unknown:
        raise ConfigError(f"unknown top-level section(s): {sorted(unknown)}")

    for required in ("radio", "database", "backup"):
        if required not in data:
            raise ConfigError(f"[{required}] section is required")

    radio = _parse_radio(data["radio"])
    service = _parse_service(data.get("service", {}))
    if service.mode == "channel" and radio.transport != "meshcore":
        raise ConfigError("service.mode='channel' requires radio.transport='meshcore'")
    if service.mode == "channel" and not radio.meshcore_channel:
        raise ConfigError("service.mode='channel' requires a non-empty radio.meshcore_channel")
    weather = _parse_weather(data.get("weather", {}))
    if weather.enabled and radio.transport != "meshcore":
        raise ConfigError("weather.enabled = true requires radio.transport = 'meshcore'")
    if weather.enabled and weather.channel == radio.meshcore_channel:
        raise ConfigError("weather.channel must differ from radio.meshcore_channel")
    return Config(
        radio=radio,
        service=service,
        database=_parse_database(data["database"]),
        retention=_parse_retention(data.get("retention", {})),
        backup=_parse_backup(data["backup"]),
        metrics=_parse_metrics(data.get("metrics", {})),
        moderation=_parse_moderation(data.get("moderation", {})),
        observatory=_parse_observatory(data.get("observatory", {})),
        web=_parse_web(data.get("web", {})),
        weather=weather,
    )


def load_config(path: str | os.PathLike[str]) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return parse_config(data)
