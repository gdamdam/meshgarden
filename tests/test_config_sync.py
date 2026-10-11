"""Unit tests for meshgarden.config_sync.sync_missing_keys --
a text-preserving TOML "add what's missing" merge (Task 16). Detection is
TOML-aware (tomllib parses both texts to diff key sets); every existing
line in `existing_text` must survive byte-for-byte -- only ever appended
to, never rewritten, reordered, or removed.
"""

import tomllib

from meshgarden.config_sync import sync_missing_keys

TEMPLATE = """\
[radio]
# Serial device path.
device = "/dev/serial/by-id/REPLACE_WITH_YOUR_RADIO"
# Expected node id.
expected_node_id = "!REPLACE_WITH_NODE_ID"

[service]
# Operator-facing name.
name = "Meshgarden Guestbook"
# Interaction mode.
mode = "dm_only"
# Whether SEEN is answered at all.
seen_enabled = true
# Max new SIGNs per day.
daily_sign_quota = 3

[retention]
# Raw observation retention.
observations_days = 7
# Undelivered MAIL retention.
mail_expiry_days = 7

[database]
path = "/var/lib/meshgarden/observatory.db"
"""


def test_adds_a_missing_key_with_its_comment_at_the_end_of_its_section():
    existing = """\
[radio]
device = "/dev/ttyUSB0"
expected_node_id = "!11223344"

[service]
name = "My Node"
mode = "dm_only"
daily_sign_quota = 5

[retention]
observations_days = 7
mail_expiry_days = 7

[database]
path = "/tmp/o.db"
"""
    new_text, additions, skipped = sync_missing_keys(existing, TEMPLATE)

    assert additions == ["service.seen_enabled"]
    assert skipped == []
    expected = """\
[radio]
device = "/dev/ttyUSB0"
expected_node_id = "!11223344"

[service]
name = "My Node"
mode = "dm_only"
daily_sign_quota = 5
# Whether SEEN is answered at all.
seen_enabled = true

[retention]
observations_days = 7
mail_expiry_days = 7

[database]
path = "/tmp/o.db"
"""
    assert new_text == expected
    # Still valid TOML, and the pre-existing non-default values survive.
    data = tomllib.loads(new_text)
    assert data["service"]["name"] == "My Node"
    assert data["service"]["daily_sign_quota"] == 5
    assert data["service"]["seen_enabled"] is True


def test_adds_a_missing_whole_section_at_eof():
    existing = """\
[radio]
device = "/dev/ttyUSB0"
expected_node_id = "!11223344"

[service]
name = "My Node"
mode = "dm_only"
seen_enabled = true
daily_sign_quota = 3

[database]
path = "/tmp/o.db"
"""
    new_text, additions, skipped = sync_missing_keys(existing, TEMPLATE)

    assert additions == ["retention.observations_days", "retention.mail_expiry_days"]
    assert skipped == []
    assert new_text == existing + (
        "\n[retention]\n"
        "# Raw observation retention.\n"
        "observations_days = 7\n"
        "# Undelivered MAIL retention.\n"
        "mail_expiry_days = 7\n"
    )
    data = tomllib.loads(new_text)
    assert data["retention"]["observations_days"] == 7
    assert data["retention"]["mail_expiry_days"] == 7


def test_leaves_an_existing_key_with_a_non_default_value_untouched():
    existing = """\
[radio]
device = "/dev/ttyUSB0"
expected_node_id = "!11223344"

[service]
name = "My Node"
mode = "dm_only"
seen_enabled = false
daily_sign_quota = 3

[retention]
observations_days = 30
mail_expiry_days = 1

[database]
path = "/tmp/o.db"
"""
    new_text, additions, skipped = sync_missing_keys(existing, TEMPLATE)
    assert additions == []
    assert skipped == []
    assert new_text == existing
    data = tomllib.loads(new_text)
    assert data["service"]["seen_enabled"] is False
    assert data["retention"]["observations_days"] == 30
    assert data["retention"]["mail_expiry_days"] == 1


def test_byte_identical_when_nothing_is_missing():
    existing = """\
[radio]
device = "/dev/ttyUSB0"
expected_node_id = "!11223344"

[service]
name = "My Node"
mode = "dm_only"
seen_enabled = true
daily_sign_quota = 3

[retention]
observations_days = 7
mail_expiry_days = 7

[database]
path = "/tmp/o.db"
"""
    new_text, additions, skipped = sync_missing_keys(existing, TEMPLATE)
    assert additions == []
    assert skipped == []
    assert new_text is existing


def test_second_run_is_a_no_op():
    existing = """\
[radio]
device = "/dev/ttyUSB0"
expected_node_id = "!11223344"

[service]
name = "My Node"
mode = "dm_only"
daily_sign_quota = 5

[database]
path = "/tmp/o.db"
"""
    first_text, first_additions, first_skipped = sync_missing_keys(existing, TEMPLATE)
    assert first_additions != []
    assert first_skipped == []

    second_text, second_additions, second_skipped = sync_missing_keys(first_text, TEMPLATE)
    assert second_additions == []
    assert second_skipped == []
    assert second_text == first_text


def test_key_present_in_existing_but_absent_from_template_is_kept():
    existing = """\
[radio]
device = "/dev/ttyUSB0"
expected_node_id = "!11223344"

[service]
name = "My Node"
mode = "dm_only"
seen_enabled = true
daily_sign_quota = 3
totally_custom_operator_field = 42

[retention]
observations_days = 7
mail_expiry_days = 7

[database]
path = "/tmp/o.db"
"""
    new_text, additions, skipped = sync_missing_keys(existing, TEMPLATE)
    assert additions == []
    assert skipped == []
    assert new_text == existing
    data = tomllib.loads(new_text)
    assert data["service"]["totally_custom_operator_field"] == 42


def test_result_parses_with_tomllib():
    existing = """\
[radio]
device = "/dev/ttyUSB0"
expected_node_id = "!11223344"

[service]
name = "My Node"
mode = "dm_only"

[database]
path = "/tmp/o.db"
"""
    new_text, additions, skipped = sync_missing_keys(existing, TEMPLATE)
    assert additions  # sanity: something was actually added
    assert skipped == []
    tomllib.loads(new_text)  # must not raise


def test_two_adjacent_missing_keys_are_both_added_in_template_order():
    existing = """\
[radio]
device = "/dev/ttyUSB0"
expected_node_id = "!11223344"

[service]
name = "My Node"
mode = "dm_only"

[retention]
observations_days = 7
mail_expiry_days = 7

[database]
path = "/tmp/o.db"
"""
    new_text, additions, skipped = sync_missing_keys(existing, TEMPLATE)
    assert additions == ["service.seen_enabled", "service.daily_sign_quota"]
    assert skipped == []
    expected = """\
[radio]
device = "/dev/ttyUSB0"
expected_node_id = "!11223344"

[service]
name = "My Node"
mode = "dm_only"
# Whether SEEN is answered at all.
seen_enabled = true
# Max new SIGNs per day.
daily_sign_quota = 3

[retention]
observations_days = 7
mail_expiry_days = 7

[database]
path = "/tmp/o.db"
"""
    assert new_text == expected


def test_never_touches_radio_placeholder_values():
    """Guard: even though [radio] is a template table too, a real existing
    config's already-set radio values must never be altered -- there's
    nothing to add there since both fields are always present (no
    RadioConfig defaults), but this pins that down explicitly."""
    existing = """\
[radio]
device = "/dev/serial/by-id/usb-real-radio-if00"
expected_node_id = "!deadbeef"

[service]
name = "My Node"
mode = "dm_only"
seen_enabled = true
daily_sign_quota = 3

[retention]
observations_days = 7
mail_expiry_days = 7

[database]
path = "/tmp/o.db"
"""
    new_text, _, skipped = sync_missing_keys(existing, TEMPLATE)
    assert 'device = "/dev/serial/by-id/usb-real-radio-if00"' in new_text
    assert 'expected_node_id = "!deadbeef"' in new_text
    assert new_text == existing
    assert skipped == []


def test_nested_template_table_is_skipped_with_a_note():
    """A template key whose value is itself a nested table ([service.limits]
    under [service]) can't be handled by this line-splicing merge -- it must
    be reported back via `skipped` (not silently dropped), and never spliced
    into the output."""
    template = """\
[service]
name = "Meshgarden Guestbook"
seen_enabled = true

[service.limits]
max_topics = 5
"""
    existing = """\
[service]
name = "My Node"
"""
    new_text, additions, skipped = sync_missing_keys(existing, template)

    assert additions == ["service.seen_enabled"]
    assert skipped == ["service.limits"]
    assert "seen_enabled = true" in new_text
    assert "max_topics" not in new_text
    tomllib.loads(new_text)
