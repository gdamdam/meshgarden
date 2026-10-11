"""Pure-function coverage for meshgarden/weather.py: parsing, filtering,
grouping and the 120-byte line formatter. No network, no SQLite."""
from __future__ import annotations

from meshgarden.weather import (
    DEFAULT_ZONE_ALIASES,
    WeatherProduct,
    effective_aliases,
    format_alert_line,
    parse_active_alerts,
    wx_status_text,
    zone_names,
)

TZ = "America/Los_Angeles"
# 2026-10-10 18:00 PDT == 2026-10-11T01:00:00Z
SAT_6PM = 1791680400


def feature(
    fid,
    *,
    event="Red Flag Warning",
    severity="Severe",
    status="Actual",
    mtype="Alert",
    zones=("CAZ504", "CAZ515"),
    sent="2026-10-10T12:00:00-07:00",
    expires="2026-10-10T18:00:00-07:00",
    ends=None,
    headline="RED FLAG WARNING IN EFFECT",
    references=(),
):
    return {
        "id": fid,
        "properties": {
            "id": fid,
            "event": event,
            "severity": severity,
            "status": status,
            "messageType": mtype,
            "sent": sent,
            "expires": expires,
            "ends": ends,
            "senderName": "NWS San Francisco CA",
            "geocode": {"UGC": list(zones) + ["CAC041"]},
            "parameters": {"NWSheadline": [headline]} if headline else {},
            "references": [{"identifier": r} for r in references],
        },
    }


def parse(*feats, suffixes=("Warning", "Watch"), min_severity="Moderate"):
    return parse_active_alerts(
        {"features": list(feats)}, event_suffixes=suffixes, min_severity=min_severity
    )


def test_keeps_a_warning_and_drops_statements_and_advisories():
    out = parse(
        feature("a1"),
        feature("b1", event="Beach Hazards Statement", severity="Moderate"),
        feature("c1", event="Coastal Flood Advisory", severity="Minor"),
    )
    assert [p.event for p in out] == ["Red Flag Warning"]


def test_drops_non_actual_and_unknown_message_types():
    out = parse(feature("t1", status="Test"), feature("x1", mtype="Ack"))
    assert out == []


def test_suffix_match_is_case_insensitive_on_last_word():
    out = parse(feature("w1", event="flood WATCH", severity="Moderate"))
    assert len(out) == 1


def test_min_severity_filters_but_unknown_passes():
    out = parse(
        feature("m1", event="Flood Watch", severity="Minor"),
        feature("u1", event="Flood Watch", severity="Unknown"),
        min_severity="Moderate",
    )
    assert [p.alert_ids for p in out] == [("u1",)]


def test_groups_split_features_into_one_product_with_merged_zones():
    out = parse(
        feature("z2", zones=("CAZ515",)),
        feature("z1", zones=("CAZ504", "CAZ506")),
    )
    assert len(out) == 1
    p = out[0]
    assert p.product_key == "z1"  # smallest id in the group
    assert p.alert_ids == ("z1", "z2")
    assert p.zones == ("CAZ504", "CAZ506", "CAZ515")  # NWS order, deduped


def test_different_headlines_are_different_products():
    out = parse(feature("a", headline="H1"), feature("b", headline="H2"))
    assert len(out) == 2


def test_expiry_prefers_ends_then_expires_then_sent_plus_six_hours():
    p_ends = parse(feature("e", ends="2026-10-10T20:00:00-07:00"))[0]
    p_exp = parse(feature("x"))[0]
    p_none = parse(feature("n", expires=None))[0]
    assert p_ends.expires_at == SAT_6PM + 7200
    assert p_exp.expires_at == SAT_6PM
    assert p_none.expires_at == p_none.sent_at + 6 * 3600


def test_references_are_carried():
    p = parse(feature("u2", mtype="Update", references=("u1",)))[0]
    assert p.message_type == "Update"
    assert p.references == ("u1",)


def test_zone_names_alias_dedupe_and_collapse_unknown():
    names = zone_names(("CAZ006", "CAZ999", "CAZ006", "CAZ888"), DEFAULT_ZONE_ALIASES)
    assert names == ["SF", "other"]


def test_effective_aliases_overrides_defaults():
    a = effective_aliases({"CAZ006": "City"})
    assert a["CAZ006"] == "City" and a["CAZ515"] == "East Bay Hills"


def product(**kw):
    base = dict(
        product_key="k",
        alert_ids=("k",),
        references=(),
        event="Red Flag Warning",
        message_type="Alert",
        severity="Severe",
        zones=("CAZ504", "CAZ515"),
        sent_at=SAT_6PM - 6 * 3600,
        expires_at=SAT_6PM,
    )
    base.update(kw)
    return WeatherProduct(**base)


def test_format_alert_line_basic():
    line = format_alert_line(product(), kind="Alert", aliases=DEFAULT_ZONE_ALIASES, timezone=TZ)
    assert line == "⚠ Red Flag Warning · North Bay Mtns, East Bay Hills · until Sat 6PM"


def test_format_update_and_cancel_prefixes():
    upd = format_alert_line(product(), kind="Update", aliases=DEFAULT_ZONE_ALIASES, timezone=TZ)
    can = format_alert_line(product(), kind="Cancel", aliases=DEFAULT_ZONE_ALIASES, timezone=TZ)
    assert upd.startswith("↻ Red Flag Warning · ")
    assert can == "✓ Red Flag Warning cancelled · North Bay Mtns, East Bay Hills"


def test_format_all_unknown_zones_prints_other():
    line = format_alert_line(
        product(zones=("CAZ529", "CAZ530")), kind="Alert", aliases=DEFAULT_ZONE_ALIASES, timezone=TZ
    )
    assert " · other · " in line


def test_format_truncates_areas_with_plus_n_then_until_then_event():
    many = tuple(DEFAULT_ZONE_ALIASES)  # every known zone
    line = format_alert_line(
        product(zones=many), kind="Alert", aliases=DEFAULT_ZONE_ALIASES, timezone=TZ
    )
    assert len(line.encode("utf-8")) <= 120
    assert "+" in line and "until Sat 6PM" in line
    long_event = "X" * 110 + " Warning"
    line2 = format_alert_line(
        product(event=long_event, zones=many),
        kind="Alert", aliases=DEFAULT_ZONE_ALIASES, timezone=TZ,
    )
    assert len(line2.encode("utf-8")) <= 120
    assert line2.endswith("…")
    assert "until" not in line2


def test_format_never_splits_a_multibyte_character():
    line = format_alert_line(
        product(event="É" * 200 + " Warning"), kind="Alert",
        aliases=DEFAULT_ZONE_ALIASES, timezone=TZ, max_bytes=60,
    )
    line.encode("utf-8")  # would raise on a split surrogate; also check the bound
    assert len(line.encode("utf-8")) <= 60


def test_format_morning_hour_and_midnight():
    noon = format_alert_line(
        product(expires_at=SAT_6PM - 6 * 3600), kind="Alert",
        aliases=DEFAULT_ZONE_ALIASES, timezone=TZ,
    )
    assert noon.endswith("until Sat 12PM")
    midnight = format_alert_line(
        product(expires_at=SAT_6PM + 6 * 3600), kind="Alert",
        aliases=DEFAULT_ZONE_ALIASES, timezone=TZ,
    )
    assert midnight.endswith("until Sun 12AM")


def _wx(**kw):
    base = dict(enabled=True, counties=9, last_poll_ok_at=SAT_6PM - 120, active=0,
                last_sent_at=None, now=SAT_6PM, timezone=TZ)
    base.update(kw)
    return wx_status_text(**base)


def test_wx_status_off():
    assert _wx(enabled=False) == "🌦 weather alerts off"


def test_wx_status_ok_and_last_sent():
    line = _wx(last_sent_at=SAT_6PM - 6 * 3600 - 20 * 60)
    assert line.startswith("🌦 wx-bay\n9 counties\nNWS ok 2m ago\n0 active\nlast sent Oct 10 ")
    assert _wx(last_sent_at=None).endswith("last sent never")


def test_wx_status_last_sent_formats_day_without_padding():
    # 2026-10-05 08:05 PDT == 15:05 UTC
    ts = 1791212700
    line = _wx(last_sent_at=ts)
    assert line.endswith("last sent Oct 5 08:05"), line


def test_wx_status_never_polled():
    assert "NWS never" in _wx(last_poll_ok_at=None)


def test_wx_status_age_units():
    assert "NWS ok 30s ago" in _wx(last_poll_ok_at=SAT_6PM - 30)
    assert "NWS ok 5m ago" in _wx(last_poll_ok_at=SAT_6PM - 300)


def test_wx_status_down_in_minutes_then_hours():
    assert "NWS down 35m" in _wx(last_poll_ok_at=SAT_6PM - 35 * 60)
    assert "NWS down 2h" in _wx(last_poll_ok_at=SAT_6PM - 2 * 3600)
    # exactly at the threshold is still ok
    assert "NWS ok 6m ago" in _wx(last_poll_ok_at=SAT_6PM - 360)


def test_wx_status_lead_and_byte_budget():
    assert _wx(lead="wx-bay daily").startswith("🌦 wx-bay daily\n9 counties")
    out = _wx(max_bytes=40)
    assert len(out.encode("utf-8")) <= 40
    assert out.endswith("…")
