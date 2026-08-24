"""Turning what someone typed into a moment in time.

Two shapes, because those are the two ways people say it:

* **relative** — ``2h``, ``90m``, ``1d``, ``3h 30m``. No timezone is involved,
  so it cannot be wrong.
* **a clock time** — ``21:30``. This one *does* need a timezone, and getting it
  wrong is a silent five-and-a-half-hour error rather than a visible failure.
  It is resolved in the customer's own zone and always confirmed back as both
  an absolute time and a "in X" — a wrong zone is obvious in the second form
  even when the first looks right.

A clock time that has already passed today means tomorrow. Nobody schedules
something for the past, and refusing would be pedantry.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: ``2h``, ``90m``, ``1d``, ``3h 30m`` — the same spelling the repeat interval
#: takes, so one thing learned works in both places.
_RELATIVE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(days|day|d|hours|hour|hrs|hr|h|minutes|minute|mins|min|m)"
)
_CLOCK = re.compile(r"^(\d{1,2})[:.](\d{2})$")

_UNIT_SECONDS = {"d": 86_400, "h": 3_600, "m": 60}


def zone(name: str) -> ZoneInfo:
    """The customer's timezone, or UTC if the stored name is not one.

    Falling back rather than raising: a bad name in a settings row should cost
    a wrong-looking time on a screen, not a dead scheduler.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def is_a_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def parse_when(text: str, *, now: datetime, tz: ZoneInfo) -> datetime | None:
    """A UTC moment, or None if this is not a time anyone meant."""
    cleaned = text.strip().lower()
    if not cleaned:
        return None

    clock = _CLOCK.match(cleaned)
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        if hour > 23 or minute > 59:
            return None
        local = now.astimezone(tz)
        when = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= local:
            # Already gone today, so they mean tomorrow.
            when += timedelta(days=1)
        return when.astimezone(UTC)

    total = 0.0
    consumed = 0
    for match in _RELATIVE.finditer(cleaned):
        if cleaned[consumed : match.start()].strip():
            return None
        total += float(match.group(1)) * _UNIT_SECONDS[match.group(2)[0]]
        consumed = match.end()
    if consumed == 0 or cleaned[consumed:].strip() or total <= 0:
        return None
    # A year out is not a schedule, and a number large enough to overflow a
    # timestamp is a crash rather than a message anyone can act on.
    if total > 365 * 86_400:
        return None
    return now + timedelta(seconds=total)
