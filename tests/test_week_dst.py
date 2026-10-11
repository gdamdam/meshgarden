"""The display feed walks local calendar dates, including DST transitions."""

import json
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from meshgarden import web
from meshgarden.database import connect, migrate
from tests.test_web import _cfg


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("2026-03-09T00:30:00", ["2026-03-09", "2026-03-08", "2026-03-07",
                                "2026-03-06", "2026-03-05", "2026-03-04", "2026-03-03"]),
        ("2026-11-01T23:30:00", ["2026-11-01", "2026-10-31", "2026-10-30",
                                "2026-10-29", "2026-10-28", "2026-10-27", "2026-10-26"]),
    ],
)
def test_week_feed_has_seven_consecutive_local_dates(tmp_path, monkeypatch, stamp, expected):
    cfg = _cfg(tmp_path)
    cfg = replace(cfg, service=replace(cfg.service, timezone="America/Los_Angeles"))
    now = datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo(cfg.service.timezone))
    monkeypatch.setattr(web.time, "time", lambda: now.timestamp())
    conn = connect(cfg.database.path)
    try:
        migrate(conn)
        result = json.loads(web._api_week(cfg, conn, False))
    finally:
        conn.close()
    assert [day["day"] for day in result["days"]] == expected
