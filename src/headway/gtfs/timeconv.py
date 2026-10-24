"""Conversion of GTFS schedule times to UTC instants.

GTFS times are written as ``H:MM:SS`` relative to "noon minus 12 hours" of the service
day, in the agency's time zone, and may exceed 24:00 for trips that run past midnight.
"Noon minus 12 h" equals local midnight except on days when clocks change: on the day
daylight saving time ends it is 01:00 local time, and on the day it starts it is 23:00 of
the previous day. Every GTFS time in Headway goes through :func:`gtfs_time_to_utc`.
"""

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Europe/Prague"
_GTFS_TIME = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")


def parse_gtfs_time(value: str) -> int:
    """Return the number of seconds in a GTFS ``H:MM:SS`` string (hours may exceed 23)."""
    match = _GTFS_TIME.match(value.strip())
    if match is None:
        raise ValueError(f"not a GTFS time (H:MM:SS): {value!r}")
    hours, minutes, seconds = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def service_day_reference(service_date: date, tz: str = DEFAULT_TZ) -> datetime:
    """Return the UTC instant of "noon minus 12 h" of ``service_date`` in ``tz``."""
    noon = datetime.combine(service_date, time(12), tzinfo=ZoneInfo(tz))
    return noon.astimezone(UTC) - timedelta(hours=12)


def gtfs_time_to_utc(service_date: date, value: str, tz: str = DEFAULT_TZ) -> datetime:
    """Convert a GTFS time on ``service_date`` to an aware UTC datetime.

    >>> gtfs_time_to_utc(date(2026, 10, 24), "25:13:00").isoformat()
    '2026-10-24T23:13:00+00:00'
    """
    return service_day_reference(service_date, tz) + timedelta(seconds=parse_gtfs_time(value))
