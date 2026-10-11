"""Offline sunrise and sunset at the configured garden location."""

import math
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astral import Observer
from astral.sun import noon, sunrise, sunset, zenith


def sun_text(
    now: int, *, latitude: float | None, longitude: float | None, timezone: str
) -> str:
    """Return today's local events, independently reporting missing polar events."""
    if latitude is None or longitude is None:
        return "⚠️ SUN unavailable\nGarden location is not configured"
    if (
        not math.isfinite(latitude) or not -90 <= latitude <= 90
        or not math.isfinite(longitude) or not -180 <= longitude <= 180
    ):
        return "⚠️ SUN unavailable\nGarden location is invalid"
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return "⚠️ SUN unavailable\nGarden timezone is invalid"
    today = datetime.fromtimestamp(now, zone).date()
    observer = Observer(latitude, longitude)
    lines = [f"☀️ SUN · {today.isoformat()} · approx"]
    for label, calculate in (("🌅 Rise", sunrise), ("🌇 Set", sunset)):
        try:
            event = calculate(observer, date=today, tzinfo=zone)
            text = event.strftime("%H:%M %Z")
        except ValueError as exc:
            # Astral distinguishes polar conditions from an unresolvable date.
            # Do not infer the other event's status from this event's failure.
            error = str(exc)
            # Python 3.14 changed acos's domain-error wording; Astral 3.2 only
            # recognizes the older wording. Apply its same polar classification.
            if error.startswith("expected a number in range from -1 up to 1"):
                angle = zenith(observer, noon(observer, today))
                error = "always below" if angle > 90 else "always above"
            if "always above" in error:
                text = "none (sun stays above horizon)"
            elif "always below" in error:
                text = "none (sun stays below horizon)"
            else:
                text = "unavailable"
        lines.append(f"{label} {text}")
    return "\n".join(lines)
