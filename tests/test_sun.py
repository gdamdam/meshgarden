"""Solar calculations stay local, respect DST, and preserve absent events."""

from datetime import UTC, datetime

import pytest

from meshgarden.sun import sun_text


def _sun(iso, **kwargs):
    options = dict(latitude=37.7749, longitude=-122.4194, timezone="America/Los_Angeles")
    options.update(kwargs)
    text = sun_text(int(datetime.fromisoformat(iso).timestamp()), **options)
    assert len(text.encode("utf-8")) <= 120
    return text


@pytest.mark.parametrize(
    ("date", "rise", "setting", "offset"),
    [
        ("2026-06-21T12:00:00-07:00", "05:4", "20:3", "PDT"),
        ("2026-12-21T12:00:00-08:00", "07:2", "16:5", "PST"),
    ],
)
def test_bay_area_solstices(date, rise, setting, offset):
    text = _sun(date)
    assert f"Rise {rise}" in text
    assert f"Set {setting}" in text
    assert text.count(offset) == 2


def test_local_date_and_event_dst_offset():
    # It is still March 7 locally; tomorrow's DST transition must not leak in.
    text = _sun("2026-03-08T01:00:00+00:00")
    assert "SUN · 2026-03-07" in text
    assert text.count("PST") == 2
    # Before the clock change, today's solar events already use the new offset.
    text = _sun("2026-03-08T00:30:00-08:00")
    assert "SUN · 2026-03-08" in text
    assert text.count("PDT") == 2


@pytest.mark.parametrize(("date", "condition"), [("06-21", "above"), ("12-21", "below")])
def test_polar_events(date, condition):
    text = _sun(
        f"2026-{date}T12:00:00+00:00",
        latitude=78.2232, longitude=15.6469, timezone="Arctic/Longyearbyen",
    )
    assert f"Rise none (sun stays {condition} horizon)" in text
    assert f"Set none (sun stays {condition} horizon)" in text


@pytest.mark.parametrize("missing", [{"latitude": None}, {"longitude": None}])
def test_missing_location(missing):
    assert "not configured" in _sun("2026-06-21T12:00:00+00:00", **missing)


@pytest.mark.parametrize(
    "invalid", [{"latitude": 91}, {"longitude": float("nan")}, {"timezone": "Bad/Zone"}]
)
def test_invalid_configuration(invalid):
    text = _sun("2026-06-21T12:00:00+00:00", **invalid)
    assert "SUN unavailable" in text
    assert "invalid" in text


def test_one_missing_event_does_not_hide_other(monkeypatch):
    def missing(*args, **kwargs):
        raise ValueError("Unable to find a sunrise time on the date specified")

    monkeypatch.setattr("meshgarden.sun.sunrise", missing)
    monkeypatch.setattr(
        "meshgarden.sun.sunset", lambda *args, **kwargs: datetime(2026, 6, 21, 23, tzinfo=UTC)
    )
    text = _sun("2026-06-21T12:00:00+00:00")
    assert "Rise unavailable" in text
    assert "Set 23:00 UTC" in text
    assert "stays" not in text
