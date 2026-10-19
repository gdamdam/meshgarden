import pytest

from meshgarden import bbs_ids


def test_qualify_roundtrip():
    assert bbs_ids.MESHCORE_PREFIX == "mc:"
    assert bbs_ids.qualify("!e4d495e4", "meshcore") == "mc:!e4d495e4"
    assert bbs_ids.qualify("mc:!e4d495e4", "meshcore") == "mc:!e4d495e4"
    assert bbs_ids.qualify("!e4d495e4", "meshtastic") == "!e4d495e4"
    assert bbs_ids.split("mc:!e4d495e4") == ("meshcore", "!e4d495e4")
    assert bbs_ids.split("!e4d495e4") == ("meshtastic", "!e4d495e4")
    for transport in ("meshcore", "meshtastic"):
        assert bbs_ids.split(bbs_ids.qualify("!e4d495e4", transport)) == (transport, "!e4d495e4")


def test_local_or_none_filters_foreign():
    assert bbs_ids.local_or_none("mc:!e4d495e4", "meshcore") == "!e4d495e4"
    assert bbs_ids.local_or_none("mc:!e4d495e4", "meshtastic") is None
    assert bbs_ids.local_or_none("!e4d495e4", "meshtastic") == "!e4d495e4"
    assert bbs_ids.local_or_none("!e4d495e4", "meshcore") is None


def test_parse_qualified_argument_forms():
    assert bbs_ids.parse_qualified_argument("mc:!E4D495E4") == ("meshcore", "!e4d495e4")
    assert bbs_ids.parse_qualified_argument(" MC:!e4d495e4 ") == ("meshcore", "!e4d495e4")
    assert bbs_ids.parse_qualified_argument("Mt:!e4d495e4") == ("meshtastic", "!e4d495e4")
    assert bbs_ids.parse_qualified_argument("!e4d495e4") == (None, "!e4d495e4")
    for bad in ("mc:", "mc:e4d495e4", "xx:!e4d495e4", "alice", ""):
        with pytest.raises(ValueError):
            bbs_ids.parse_qualified_argument(bad)


def test_argument_form_reads_back_to_the_same_mesh():
    for stored in ("mc:!e4d495e4", "!e4d495e4"):
        transport, bare = bbs_ids.parse_qualified_argument(bbs_ids.argument_form(stored))
        assert bbs_ids.qualify(bare, transport) == stored


def test_typed_node_id_respects_the_shared_switch():
    op = bbs_ids.typed_node_id
    assert op("!e4d495e4", "meshcore", shared=True) == "mc:!e4d495e4"
    assert op("mt:!e4d495e4", "meshcore", shared=True) == "!e4d495e4"
    assert op("mc:!e4d495e4", "meshtastic", shared=True) == "mc:!e4d495e4"
    # Single-file install: ids stay bare, and the other mesh has no rows.
    assert op("!e4d495e4", "meshcore", shared=False) == "!e4d495e4"
    # Prefixes are a shared-file feature: single-file parsing is unchanged.
    assert op("mc:!e4d495e4", "meshcore", shared=False) is None
    assert op("mc:!e4d495e4", "meshtastic", shared=False) is None
    assert op("mt:!e4d495e4", "meshtastic", shared=False) is None
    assert op("bogus", "meshtastic", shared=True) is None
