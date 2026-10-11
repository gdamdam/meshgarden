import dataclasses
import pathlib
import tomllib

import pytest

from meshgarden.config import (
    ConfigError,
    RetentionConfig,
    ServiceConfig,
    node_id_from_num,
    parse_config,
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def minimal() -> dict:
    return {
        "radio": {"device": "/dev/ttyUSB0", "expected_node_id": "!AABBCCDD"},
        "database": {"path": "/tmp/o.db"},
        "backup": {"directory": "/tmp/b"},
    }


def test_defaults_match_spec():
    c = parse_config(minimal())
    s = c.service
    assert (s.mode, s.want_ack, s.application_retries) == ("dm_only", True, 3)
    assert s.application_retry_delay_seconds == 15
    assert s.max_guestbook_chars == 100
    assert s.guestbook_entries_returned == 3
    assert s.max_response_bytes == 180
    assert s.per_node_cooldown_seconds == 30
    assert s.global_tx_cooldown_seconds == 5
    assert s.sign_cooldown_seconds == 3600
    assert s.reply_ttl_seconds == 60
    assert s.max_pending_replies == 4
    assert s.request_dedup_hours == 24
    assert s.identical_sign_dedup_hours == 24
    assert s.ack_timeout_seconds == 300
    # A config with no daily_display key shows the haiku.
    assert s.daily_display == "haiku"
    assert c.retention.observations_days == 7
    assert c.retention.aggregates_days == 90
    assert c.retention.guestbook_expiry_days == 0
    assert c.retention.max_managed_storage_mib == 1024
    assert c.retention.node_daily_days == 365
    assert c.retention.links_days == 30
    assert (c.backup.interval_hours, c.backup.keep) == (24, 7)
    assert (c.metrics.listen, c.metrics.port) == ("127.0.0.1", 9108)
    assert c.service.name == "Meshgarden Guestbook"
    assert c.observatory.latitude == 0.0
    assert c.observatory.longitude == 0.0
    assert c.observatory.enabled_position_capture is True
    assert c.observatory.max_direct_km == 400.0
    assert c.observatory.max_km_per_hop == 150.0
    assert c.observatory.max_observed_km == 1000.0
    assert c.web.enabled is False
    assert (c.web.listen, c.web.port, c.web.allow_lan) == ("127.0.0.1", 9109, False)


def test_daily_display_accepts_haiku():
    table = minimal()
    table["service"] = {"daily_display": "haiku"}
    assert parse_config(table).service.daily_display == "haiku"


@pytest.mark.parametrize("bad", ["quote", "Haiku", "iching", "", 1, None, True])
def test_daily_display_rejects_anything_else(bad):
    table = minimal()
    table["service"] = {"daily_display": bad}
    with pytest.raises(ConfigError, match="daily_display"):
        parse_config(table)


def test_node_id_is_lowercased():
    assert parse_config(minimal()).radio.expected_node_id == "!aabbccdd"


@pytest.mark.parametrize("bad", ["", "aabbccdd", "!xyz", "!aabbccd", "!REPLACE_WITH_NODE_ID"])
def test_invalid_node_id_rejected(bad):
    d = minimal()
    d["radio"]["expected_node_id"] = bad
    with pytest.raises(ConfigError):
        parse_config(d)


def test_missing_node_id_rejected():
    d = minimal()
    del d["radio"]["expected_node_id"]
    with pytest.raises(ConfigError):
        parse_config(d)


# =============================================================================
# radio.transport / MeshCore config (additive, task 3B)
# =============================================================================


def test_radio_transport_defaults_to_meshtastic():
    r = parse_config(minimal()).radio
    assert r.transport == "meshtastic"
    assert r.meshcore_channel == "meshgarden"
    assert r.meshcore_autoadd_overwrite is False
    assert r.meshcore_max_text_bytes is None


def test_radio_transport_accepts_meshcore():
    d = minimal()
    d["radio"]["transport"] = "meshcore"
    assert parse_config(d).radio.transport == "meshcore"


@pytest.mark.parametrize("bad", ["MESHCORE", "lora", "", 1, None, True])
def test_radio_transport_rejects_anything_else(bad):
    d = minimal()
    d["radio"]["transport"] = bad
    with pytest.raises(ConfigError, match="transport"):
        parse_config(d)


def test_meshcore_channel_empty_string_disables_channel_triggering():
    d = minimal()
    d["radio"]["meshcore_channel"] = ""
    assert parse_config(d).radio.meshcore_channel == ""


def test_meshcore_channel_must_be_a_string():
    d = minimal()
    d["radio"]["meshcore_channel"] = 7
    with pytest.raises(ConfigError, match="meshcore_channel"):
        parse_config(d)


def test_meshcore_autoadd_overwrite_accepts_true():
    d = minimal()
    d["radio"]["meshcore_autoadd_overwrite"] = True
    assert parse_config(d).radio.meshcore_autoadd_overwrite is True


@pytest.mark.parametrize("bad", ["true", 1, 0])
def test_meshcore_autoadd_overwrite_rejects_non_bool(bad):
    d = minimal()
    d["radio"]["meshcore_autoadd_overwrite"] = bad
    with pytest.raises(ConfigError, match="meshcore_autoadd_overwrite"):
        parse_config(d)


def test_meshcore_max_text_bytes_override():
    d = minimal()
    d["radio"]["meshcore_max_text_bytes"] = 100
    assert parse_config(d).radio.meshcore_max_text_bytes == 100


@pytest.mark.parametrize("bad", [0, -1, "100", True])
def test_meshcore_max_text_bytes_rejects_non_positive_or_wrong_type(bad):
    d = minimal()
    d["radio"]["meshcore_max_text_bytes"] = bad
    with pytest.raises(ConfigError, match="meshcore_max_text_bytes"):
        parse_config(d)


def test_radio_meshcore_reconnect_tuning_defaults():
    r = parse_config(minimal()).radio
    assert r.meshcore_backoff_base_seconds == 1.0
    assert r.meshcore_backoff_max_seconds == 300.0
    assert r.meshcore_max_reconnect_attempts == 3
    assert r.meshcore_min_stable_session_seconds == 30.0


@pytest.mark.parametrize(
    "key",
    [
        "meshcore_backoff_base_seconds",
        "meshcore_backoff_max_seconds",
        "meshcore_min_stable_session_seconds",
    ],
)
def test_radio_meshcore_reconnect_tuning_overrides(key):
    d = minimal()
    d["radio"][key] = 5.5
    assert getattr(parse_config(d).radio, key) == 5.5


@pytest.mark.parametrize(
    "key",
    [
        "meshcore_backoff_base_seconds",
        "meshcore_backoff_max_seconds",
        "meshcore_min_stable_session_seconds",
    ],
)
@pytest.mark.parametrize("bad", [0, -1, "1", True])
def test_radio_meshcore_reconnect_tuning_rejects_non_positive_or_wrong_type(key, bad):
    d = minimal()
    d["radio"][key] = bad
    with pytest.raises(ConfigError, match=key):
        parse_config(d)


def test_radio_meshcore_max_reconnect_attempts_override():
    d = minimal()
    d["radio"]["meshcore_max_reconnect_attempts"] = 7
    assert parse_config(d).radio.meshcore_max_reconnect_attempts == 7


@pytest.mark.parametrize("bad", [0, -1, "3", True])
def test_radio_meshcore_max_reconnect_attempts_rejects_non_positive_or_wrong_type(bad):
    d = minimal()
    d["radio"]["meshcore_max_reconnect_attempts"] = bad
    with pytest.raises(ConfigError, match="meshcore_max_reconnect_attempts"):
        parse_config(d)


def test_radio_meshcore_advert_interval_seconds_default_and_override():
    assert parse_config(minimal()).radio.meshcore_advert_interval_seconds == 14_400
    d = minimal()
    d["radio"]["meshcore_advert_interval_seconds"] = 3600
    assert parse_config(d).radio.meshcore_advert_interval_seconds == 3600


def test_radio_meshcore_advert_interval_seconds_zero_is_allowed():
    """Unlike the reconnect-tuning keys above, 0 is a valid, meaningful
    value here -- it disables the periodic repeat (the per-connection
    advert is unaffected, since it lives entirely in meshcore_client.py)."""
    d = minimal()
    d["radio"]["meshcore_advert_interval_seconds"] = 0
    assert parse_config(d).radio.meshcore_advert_interval_seconds == 0


@pytest.mark.parametrize("bad", [-1, "3600", True, 1.5])
def test_radio_meshcore_advert_interval_seconds_rejects_negative_or_wrong_type(bad):
    d = minimal()
    d["radio"]["meshcore_advert_interval_seconds"] = bad
    with pytest.raises(ConfigError, match="meshcore_advert_interval_seconds"):
        parse_config(d)


def test_radio_unknown_key_still_rejected():
    """The new MeshCore keys extend RadioConfig's field set, but an
    unrelated typo must still be caught -- `_reject_unknown_keys` derives
    its allowlist from `RadioConfig`'s fields dynamically."""
    d = minimal()
    d["radio"]["mesh_core_channel"] = "typo"
    with pytest.raises(ConfigError, match="unknown key"):
        parse_config(d)


@pytest.mark.parametrize(
    "key,value",
    [
        ("mode", "broadcast"),
        ("mode", "DM_ONLY"),
        ("want_ack", False),
        ("application_retries", -1),
        ("application_retries", True),
        ("application_retries", 4),
    ],
)
def test_policy_values_are_hard_rejected(key, value):
    d = minimal()
    d["service"] = {key: value}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_channel_mode_requires_meshcore_and_a_named_channel():
    d = minimal()
    d["radio"]["transport"] = "meshcore"
    d["service"] = {"mode": "channel"}
    assert parse_config(d).service.mode == "channel"

    d["radio"]["meshcore_channel"] = ""
    with pytest.raises(ConfigError, match="non-empty radio.meshcore_channel"):
        parse_config(d)

    d = minimal()
    d["service"] = {"mode": "channel"}
    with pytest.raises(ConfigError, match="radio.transport='meshcore'"):
        parse_config(d)


@pytest.mark.parametrize("value", [0, 1, 2, 3])
def test_application_retries_accepts_only_bounded_values(value):
    d = minimal()
    d["service"] = {"application_retries": value}
    assert parse_config(d).service.application_retries == value


@pytest.mark.parametrize(
    "key,value",
    [
        ("mobile_dm_recovery_enabled", "yes"),
        ("mobile_dm_recovery_destinations", ["bad"]),
        ("mobile_dm_recovery_cooldown_seconds", 299),
        ("mobile_dm_global_recoveries_per_hour", 7),
        ("mobile_dm_recovery_min_interval_seconds", 29),
        ("mobile_dm_max_retries", 2),
    ],
)
def test_mobile_recovery_safety_bounds_are_validated(key, value):
    d = minimal()
    d["service"] = {key: value}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_retry_delay_must_precede_ack_timeout():
    d = minimal()
    d["service"] = {"application_retry_delay_seconds": 300, "ack_timeout_seconds": 300}
    with pytest.raises(ConfigError, match="retry_delay"):
        parse_config(d)


def test_complete_retry_window_must_fit_ack_timeout():
    d = minimal()
    d["service"] = {
        "application_retries": 3,
        "application_retry_delay_seconds": 100,
        "ack_timeout_seconds": 300,
    }
    with pytest.raises(ConfigError, match="retry window"):
        parse_config(d)


def test_response_bytes_cannot_exceed_transport_limit():
    d = minimal()
    d["service"] = {"max_response_bytes": 234}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_unknown_key_rejected():
    d = minimal()
    d["service"] = {"enable_broadcast_fallback": True}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_node_id_from_num():
    assert node_id_from_num(0xAABBCCDD) == "!aabbccdd"
    assert node_id_from_num(0x1) == "!00000001"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!AABBCCDD", "!aabbccdd"),
        ("  !aabbccdd  ", "!aabbccdd"),
        ("ccdd", None),  # a bare tail is not a full id
        ("fndr", None),  # a short name is not a full id either
        ("", None),
    ],
)
def test_parse_node_id_argument(text, expected):
    """The single node-id-argument shape check both `MeshtasticTransport.
    parse_node_argument`/`MeshCoreTransport.parse_node_argument` delegate
    to, and that the operator CLI (`cli_ops.py`, `main.cmd_block`/
    `cmd_unblock`) calls directly since it never constructs a live
    Transport (task 3D). Same cases as
    `tests/test_transport_contract.py::test_parse_node_argument_only_resolves_a_full_id`."""
    from meshgarden.config import parse_node_id_argument

    assert parse_node_id_argument(text) == expected


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("service", "max_response_bytes", "180"),
        ("service", "max_guestbook_chars", "100"),
        ("service", "guestbook_entries_returned", "3"),
        ("service", "max_pending_replies", "4"),
        ("metrics", "port", "9108"),
        ("service", "max_response_bytes", True),
        ("metrics", "port", True),
    ],
)
def test_numeric_bound_fields_reject_non_int(section, key, value):
    d = minimal()
    d[section] = {key: value}
    with pytest.raises(ConfigError):
        parse_config(d)


@pytest.mark.parametrize("value", [0, -1])
def test_ack_timeout_seconds_must_be_positive(value):
    d = minimal()
    d["service"] = {"ack_timeout_seconds": value}
    with pytest.raises(ConfigError):
        parse_config(d)


# --- guestbook_expiry_days -----------------------------------------------------


@pytest.mark.parametrize("value", [1, 30, 365])
def test_guestbook_expiry_days_nonzero_rejected(value):
    """Regression test: guestbook entries are kept until the operator
    deletes them (spec) -- guestbook_expiry_days was never actually wired
    to any expiry logic, so a nonzero value silently did nothing instead of
    behaving as configured. Only 0 (no automatic expiry) is supported."""
    d = minimal()
    d["retention"] = {"guestbook_expiry_days": value}
    with pytest.raises(ConfigError, match="guestbook_expiry_days"):
        parse_config(d)


def test_guestbook_expiry_days_zero_accepted():
    d = minimal()
    d["retention"] = {"guestbook_expiry_days": 0}
    assert parse_config(d).retention.guestbook_expiry_days == 0


# --- daily_command_quota / daily_sign_quota -----------------------------------


def test_quota_defaults_match_spec():
    c = parse_config(minimal())
    assert c.service.daily_command_quota == 60
    assert c.service.daily_sign_quota == 3


@pytest.mark.parametrize("value", [1, 60, 1000])
def test_daily_command_quota_accepts_positive_ints(value):
    d = minimal()
    d["service"] = {"daily_command_quota": value}
    assert parse_config(d).service.daily_command_quota == value


@pytest.mark.parametrize("value", [0, -1])
def test_daily_command_quota_must_be_at_least_one(value):
    d = minimal()
    d["service"] = {"daily_command_quota": value}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_daily_command_quota_rejects_non_int():
    d = minimal()
    d["service"] = {"daily_command_quota": "60"}
    with pytest.raises(ConfigError):
        parse_config(d)


@pytest.mark.parametrize("value", [0, 3, 1000])
def test_daily_sign_quota_accepts_non_negative_ints(value):
    d = minimal()
    d["service"] = {"daily_sign_quota": value}
    assert parse_config(d).service.daily_sign_quota == value


def test_daily_sign_quota_rejects_negative():
    d = minimal()
    d["service"] = {"daily_sign_quota": -1}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_daily_sign_quota_rejects_non_int():
    d = minimal()
    d["service"] = {"daily_sign_quota": True}
    with pytest.raises(ConfigError):
        parse_config(d)


# --- directory/boards/mail epic: seen_enabled, post/mail quotas, max_topics,
# mail_pending_limit, mail_expiry_days --------------------------------------


def test_service_config_new_defaults():
    cfg = parse_config(
        {
            "radio": {"device": "/dev/x", "expected_node_id": "!11223344"},
            "database": {"path": "x.db"},
            "backup": {"directory": "b"},
        }
    )
    assert cfg.service.seen_enabled is True
    assert cfg.service.daily_post_quota == 5
    assert cfg.service.daily_mail_quota == 3
    assert cfg.service.max_topics == 50
    assert cfg.service.mail_pending_limit == 5
    assert cfg.retention.mail_expiry_days == 7


def test_max_topics_must_be_at_least_1():
    with pytest.raises(ConfigError, match="max_topics"):
        parse_config(
            {
                "radio": {"device": "/dev/x", "expected_node_id": "!11223344"},
                "service": {"max_topics": 0},
                "database": {"path": "x.db"},
                "backup": {"directory": "b"},
            }
        )


# --- LAB tier: [service] lab_unlock_word ---------------------------------------


def test_lab_unlock_word_defaults_to_empty_string_meaning_disabled():
    cfg = parse_config(minimal())
    assert cfg.service.lab_unlock_word == ""


def test_lab_unlock_word_valid_value_is_accepted_and_stored_as_given():
    d = minimal()
    d["service"] = {"lab_unlock_word": "SuperGarden"}
    cfg = parse_config(d)
    assert cfg.service.lab_unlock_word == "SuperGarden"


@pytest.mark.parametrize("word", ["ab", "has space", "x" * 25])
def test_lab_unlock_word_rejects_invalid_values(word):
    d = minimal()
    d["service"] = {"lab_unlock_word": word}
    with pytest.raises(ConfigError, match="lab_unlock_word"):
        parse_config(d)


@pytest.mark.parametrize("word", ["wordle", "WORDLE", "  wordle  ", "signal"])
def test_lab_unlock_word_rejects_a_word_that_is_an_existing_command(word):
    """Final-review fix (item 7): a lab_unlock_word equal to an existing
    command word (case-insensitively, after stripping) would silently
    disable that command in parse_command's dispatch -- refuse at config
    load instead."""
    d = minimal()
    d["service"] = {"lab_unlock_word": word}
    with pytest.raises(ConfigError, match="lab_unlock_word"):
        parse_config(d)


# --- [moderation] --------------------------------------------------------------


def test_moderation_defaults_to_disabled():
    c = parse_config(minimal())
    assert c.moderation.banned_words_file == ""


def test_moderation_section_is_optional():
    d = minimal()
    assert "moderation" not in d
    parse_config(d)  # must not raise


def test_moderation_unknown_key_rejected():
    d = minimal()
    d["moderation"] = {"bogus": 1}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_moderation_banned_words_file_must_exist(tmp_path):
    d = minimal()
    d["moderation"] = {"banned_words_file": str(tmp_path / "does-not-exist.txt")}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_moderation_banned_words_file_accepted_when_readable(tmp_path):
    p = tmp_path / "banned.txt"
    p.write_text("badword\n")
    d = minimal()
    d["moderation"] = {"banned_words_file": str(p)}
    c = parse_config(d)
    assert c.moderation.banned_words_file == str(p)


def test_moderation_banned_words_file_accepts_the_default_special_value():
    d = minimal()
    d["moderation"] = {"banned_words_file": "default"}
    c = parse_config(d)
    assert c.moderation.banned_words_file == "default"


def test_moderation_banned_words_file_rejects_non_string():
    d = minimal()
    d["moderation"] = {"banned_words_file": 123}
    with pytest.raises(ConfigError):
        parse_config(d)


# --- [observatory] -----------------------------------------------------------


def test_observatory_section_is_optional():
    c = parse_config(minimal())
    assert c.observatory.latitude == 0.0
    assert c.observatory.longitude == 0.0
    assert c.observatory.enabled_position_capture is True


def test_observatory_accepts_configured_lat_lon():
    d = minimal()
    d["observatory"] = {"latitude": 37.7749, "longitude": -122.4194}
    c = parse_config(d)
    assert c.observatory.latitude == pytest.approx(37.7749)
    assert c.observatory.longitude == pytest.approx(-122.4194)


@pytest.mark.parametrize("value", [90.1, -90.1, 91, -91])
def test_observatory_latitude_out_of_range_rejected(value):
    d = minimal()
    d["observatory"] = {"latitude": value}
    with pytest.raises(ConfigError):
        parse_config(d)


@pytest.mark.parametrize("value", [180.1, -180.1, 200])
def test_observatory_longitude_out_of_range_rejected(value):
    d = minimal()
    d["observatory"] = {"longitude": value}
    with pytest.raises(ConfigError):
        parse_config(d)


@pytest.mark.parametrize("value", ["37.7", True, None])
def test_observatory_latitude_rejects_non_number(value):
    d = minimal()
    d["observatory"] = {"latitude": value}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_observatory_enabled_position_capture_rejects_non_bool():
    d = minimal()
    d["observatory"] = {"enabled_position_capture": "yes"}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_observatory_unknown_key_rejected():
    d = minimal()
    d["observatory"] = {"bogus": 1}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_observatory_boundary_values_accepted():
    d = minimal()
    d["observatory"] = {"latitude": 90.0, "longitude": -180.0}
    c = parse_config(d)
    assert c.observatory.latitude == 90.0
    assert c.observatory.longitude == -180.0


def test_observatory_accepts_configured_distance_thresholds():
    d = minimal()
    d["observatory"] = {"max_direct_km": 100, "max_km_per_hop": 50, "max_observed_km": 500}
    c = parse_config(d)
    assert c.observatory.max_direct_km == 100.0
    assert c.observatory.max_km_per_hop == 50.0
    assert c.observatory.max_observed_km == 500.0


@pytest.mark.parametrize("key", ["max_direct_km", "max_km_per_hop", "max_observed_km"])
@pytest.mark.parametrize("value", [0, -1, -0.5])
def test_observatory_distance_thresholds_reject_non_positive(key, value):
    d = minimal()
    d["observatory"] = {key: value}
    with pytest.raises(ConfigError):
        parse_config(d)


@pytest.mark.parametrize("key", ["max_direct_km", "max_km_per_hop", "max_observed_km"])
def test_observatory_distance_thresholds_reject_non_number(key):
    d = minimal()
    d["observatory"] = {key: "300"}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_retention_node_daily_days_and_links_days_configurable():
    d = minimal()
    d["retention"] = {"node_daily_days": 30, "links_days": 7}
    c = parse_config(d)
    assert c.retention.node_daily_days == 30
    assert c.retention.links_days == 7


@pytest.mark.parametrize("value", [0, -1, "7", True])
def test_retention_max_managed_storage_mib_rejects_non_positive_or_wrong_type(value):
    """Regression test: an unvalidated max_managed_storage_mib raised
    TypeError from storage_report's comparison instead of ConfigError."""
    d = minimal()
    d["retention"] = {"max_managed_storage_mib": value}
    with pytest.raises(ConfigError):
        parse_config(d)


# --- [backup] -------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1, "7", True])
def test_backup_keep_rejects_non_positive_or_wrong_type(value):
    """Regression test: keep=0 (or an unvalidated string) reached
    _rotate_backups unchecked, which would delete the backup just written."""
    d = minimal()
    d["backup"] = {"directory": "/tmp/b", "keep": value}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_backup_keep_accepts_positive_int():
    d = minimal()
    d["backup"] = {"directory": "/tmp/b", "keep": 1}
    assert parse_config(d).backup.keep == 1


# --- non-table sections ----------------------------------------------------------


def test_non_table_section_rejected():
    """Regression test: `radio = 5` raised TypeError from set(table) inside
    _reject_unknown_keys instead of the documented ConfigError."""
    d = minimal()
    d["radio"] = 5
    with pytest.raises(ConfigError, match=r"\[radio\] must be a table"):
        parse_config(d)


# --- [web] --------------------------------------------------------------------


def test_web_section_is_optional_and_disabled_by_default():
    c = parse_config(minimal())
    assert c.web.enabled is False
    assert c.web.listen == "127.0.0.1"
    assert c.web.port == 9109
    assert c.web.allow_lan is False


def test_web_accepts_explicit_values():
    d = minimal()
    d["web"] = {"enabled": True, "listen": "127.0.0.1", "port": 9200, "allow_lan": True}
    c = parse_config(d)
    assert c.web.enabled is True
    assert c.web.port == 9200
    assert c.web.allow_lan is True


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_web_enabled_rejects_non_bool(value):
    d = minimal()
    d["web"] = {"enabled": value}
    with pytest.raises(ConfigError):
        parse_config(d)


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_web_allow_lan_rejects_non_bool(value):
    d = minimal()
    d["web"] = {"allow_lan": value}
    with pytest.raises(ConfigError):
        parse_config(d)


@pytest.mark.parametrize("value", [0, 65536, -1, "9109", True])
def test_web_port_out_of_range_or_wrong_type_rejected(value):
    d = minimal()
    d["web"] = {"port": value}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_web_unknown_key_rejected():
    d = minimal()
    d["web"] = {"bogus": 1}
    with pytest.raises(ConfigError):
        parse_config(d)


def test_web_listen_rejects_non_string():
    d = minimal()
    d["web"] = {"listen": 123}
    with pytest.raises(ConfigError):
        parse_config(d)


# =============================================================================
# config.example.toml completeness (Task 16)
# =============================================================================


def test_config_example_service_and_retention_have_every_field_at_its_default():
    """config.example.toml's [service]/[retention] tables must literally
    spell out every ServiceConfig/RetentionConfig field (not merely parse
    successfully, which would tolerate a missing key falling back to its
    in-code default) -- one line per field, each equal to the dataclass
    default. Task 16: the epic added seven fields to the dataclasses
    without adding them to the example file."""
    raw = (_ROOT / "config.example.toml").read_text()
    data = tomllib.loads(raw)

    for cls, section in ((ServiceConfig, "service"), (RetentionConfig, "retention")):
        table = data[section]
        defaults = cls()
        for f in dataclasses.fields(cls):
            assert f.name in table, f"{section}.{f.name} missing from config.example.toml"
            assert table[f.name] == getattr(defaults, f.name), (
                f"{section}.{f.name} = {table[f.name]!r} "
                f"!= dataclass default {getattr(defaults, f.name)!r}"
            )

    # Still a valid config once the two REPLACE_WITH_ placeholders are filled in.
    substituted = raw.replace("REPLACE_WITH_YOUR_RADIO", "usb-0001").replace(
        "!REPLACE_WITH_NODE_ID", "!11223344"
    )
    parse_config(tomllib.loads(substituted))


def test_config_example_radio_documents_the_meshcore_keys_at_their_default(tmp_path):
    """Task 3B: `[radio]`'s additive MeshCore keys must be spelled out in
    config.example.toml (same convention `test_config_example_service_and_
    retention_have_every_field_at_its_default` pins for [service]/
    [retention]), so `mgarden config-sync` can append them to an existing
    installation's config with the right default and comment.
    `meshcore_max_text_bytes` is deliberately commented out rather than an
    active key (its default is `None`, not representable as a bare TOML
    value), so it is intentionally excluded from this check.
    """
    raw = (_ROOT / "config.example.toml").read_text()
    data = tomllib.loads(raw)
    radio = data["radio"]
    assert radio["transport"] == "meshtastic"
    assert radio["meshcore_channel"] == "meshgarden"
    assert radio["meshcore_autoadd_overwrite"] is False
    # Task 3B-3: the channel-knock cooldown, also additive.
    assert radio["meshcore_knock_cooldown_seconds"] == 60
    # Task 3C finding 2: the knock's own share of the shared reply queue.
    assert radio["meshcore_max_pending_knocks"] == 1
    # Fix round 3: reconnect/backoff tuning, also additive.
    assert radio["meshcore_backoff_base_seconds"] == 1.0
    assert radio["meshcore_backoff_max_seconds"] == 300.0
    assert radio["meshcore_max_reconnect_attempts"] == 3
    assert radio["meshcore_min_stable_session_seconds"] == 30.0
    # Task 3B-4 fix 2: the periodic self-advertisement repeat, also additive.
    assert radio["meshcore_advert_interval_seconds"] == 14_400
    assert radio["meshcore_repeater_public_key"] == ""
    assert radio["meshcore_repeater_guest_password"] == ""
    assert radio["meshcore_repeater_cache_seconds"] == 300
    assert radio["meshcore_repeater_query_timeout_seconds"] == 15


def test_repeater_config_requires_key_and_password_together():
    d = minimal()
    d["radio"]["meshcore_repeater_public_key"] = "AC" * 32
    with pytest.raises(ConfigError, match="must be set together"):
        parse_config(d)


def test_repeater_config_validates_and_normalizes_public_key():
    d = minimal()
    d["radio"]["meshcore_repeater_public_key"] = "ac" * 32
    d["radio"]["meshcore_repeater_guest_password"] = "private"
    cfg = parse_config(d)
    assert cfg.radio.meshcore_repeater_public_key == "AC" * 32
    assert cfg.radio.meshcore_repeater_guest_password == "private"
    assert "private" not in repr(cfg.radio)


@pytest.mark.parametrize("key", ["not-hex", "AA" * 31, "GG" * 32])
def test_repeater_config_rejects_invalid_public_key(key):
    d = minimal()
    d["radio"]["meshcore_repeater_public_key"] = key
    d["radio"]["meshcore_repeater_guest_password"] = "private"
    with pytest.raises(ConfigError, match="64 hex"):
        parse_config(d)


def test_weather_section_is_optional_and_off():
    c = parse_config(minimal())
    w = c.weather
    assert w.enabled is False
    assert w.channel == "#wx-bay"
    assert w.counties == (
        "CAC001", "CAC013", "CAC041", "CAC055", "CAC075",
        "CAC081", "CAC085", "CAC095", "CAC097",
    )
    assert w.event_suffixes == ("Warning", "Watch")
    assert w.min_severity == "Moderate"
    assert w.poll_interval_seconds == 120
    assert w.max_sends_per_hour == 6
    assert w.zone_aliases == {}
    assert w.heartbeat_hour == -1


def _weather_enabled(extra=None):
    d = minimal()
    d["radio"]["transport"] = "meshcore"
    d["radio"]["meshcore_channel"] = "#kallisti"
    d["weather"] = {"enabled": True, "contact": "ops@example.org", **(extra or {})}
    return d


def test_weather_enabled_parses_with_contact():
    c = parse_config(_weather_enabled({"zone_aliases": {"CAZ006": "SF"}}))
    assert c.weather.enabled is True
    assert c.weather.zone_aliases == {"CAZ006": "SF"}


@pytest.mark.parametrize(
    "extra, needle",
    [
        ({"contact": ""}, "weather.contact"),
        ({"channel": ""}, "weather.channel"),
        ({"counties": []}, "weather.counties"),
        ({"counties": ["CA075"]}, "weather.counties"),
        ({"event_suffixes": []}, "weather.event_suffixes"),
        ({"min_severity": "Unknown"}, "weather.min_severity"),
        ({"min_severity": "severe"}, "weather.min_severity"),
        ({"poll_interval_seconds": 10}, "weather.poll_interval_seconds"),
        ({"max_sends_per_hour": 0}, "weather.max_sends_per_hour"),
        ({"zone_aliases": {"CAZ006": 7}}, "weather.zone_aliases"),
        ({"bogus": 1}, "bogus"),
    ],
)
def test_weather_rejects_bad_values(extra, needle):
    with pytest.raises(ConfigError, match=needle):
        parse_config(_weather_enabled(extra))


def test_weather_enabled_requires_meshcore_transport():
    d = _weather_enabled()
    d["radio"]["transport"] = "meshtastic"
    with pytest.raises(ConfigError, match="radio.transport"):
        parse_config(d)


def test_weather_channel_must_differ_from_bbs_channel():
    with pytest.raises(ConfigError, match="meshcore_channel"):
        parse_config(_weather_enabled({"channel": "#kallisti"}))


def test_weather_disabled_skips_cross_checks():
    d = minimal()
    d["weather"] = {"enabled": False, "channel": "meshgarden"}
    assert parse_config(d).weather.enabled is False


def test_weather_heartbeat_hour_accepts_valid_and_rejects_out_of_range():
    assert parse_config(_weather_enabled({"heartbeat_hour": 6})).weather.heartbeat_hour == 6
    for bad in (24, -2, "6", True):
        with pytest.raises(ConfigError, match="weather.heartbeat_hour"):
            parse_config(_weather_enabled({"heartbeat_hour": bad}))


def test_channel_reply_delay_default_and_bounds():
    assert parse_config(minimal()).service.channel_reply_delay_seconds == 6
    d = minimal()
    d["service"] = {"channel_reply_delay_seconds": 0}
    assert parse_config(d).service.channel_reply_delay_seconds == 0
    d["service"] = {"channel_reply_delay_seconds": 31}
    with pytest.raises(ConfigError, match="channel_reply_delay_seconds"):
        parse_config(d)


def test_radio_contact_prune_defaults_and_override():
    r = parse_config(minimal()).radio
    assert (r.meshcore_contact_prune_enabled, r.meshcore_contact_target) == (False, 300)
    assert (r.meshcore_contact_prune_hour, r.meshcore_contact_prune_max_per_day) == (4, 50)
    d = minimal()
    d["radio"].update(
        meshcore_contact_prune_enabled=True, meshcore_contact_target=1,
        meshcore_contact_prune_hour=0, meshcore_contact_prune_max_per_day=1000,
    )
    r = parse_config(d).radio
    assert r.meshcore_contact_prune_enabled and r.meshcore_contact_target == 1
    assert r.meshcore_contact_prune_hour == 0 and r.meshcore_contact_prune_max_per_day == 1000


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("meshcore_contact_prune_enabled", 1),
        ("meshcore_contact_target", 0),
        ("meshcore_contact_target", 10_001),
        ("meshcore_contact_prune_hour", -1),
        ("meshcore_contact_prune_hour", 24),
        ("meshcore_contact_prune_max_per_day", 0),
        ("meshcore_contact_prune_max_per_day", 1001),
        ("meshcore_contact_target", True),
    ],
)
def test_radio_contact_prune_rejects_out_of_range(key, bad):
    d = minimal()
    d["radio"][key] = bad
    with pytest.raises(ConfigError, match=key):
        parse_config(d)
