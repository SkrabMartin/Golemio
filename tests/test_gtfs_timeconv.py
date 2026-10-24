from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from headway.gtfs.timeconv import gtfs_time_to_utc, parse_gtfs_time, service_day_reference

PRAGUE = ZoneInfo("Europe/Prague")


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_trip_past_midnight_25_13() -> None:
    # 2026-10-24 is CEST (UTC+2): 25:13 = 01:13 local on the 25th = 23:13 UTC on the 24th.
    assert gtfs_time_to_utc(date(2026, 10, 24), "25:13:00") == utc(2026, 10, 24, 23, 13)


def test_ordinary_day_is_local_midnight_based() -> None:
    assert gtfs_time_to_utc(date(2026, 7, 1), "08:30:00") == utc(2026, 7, 1, 6, 30)
    assert gtfs_time_to_utc(date(2026, 1, 15), "08:30:00") == utc(2026, 1, 15, 7, 30)


def test_dst_end_2026_10_25() -> None:
    # Clocks go back 03:00 CEST -> 02:00 CET. Noon is CET (11:00 UTC), so the reference is
    # 23:00 UTC on the 24th, which is 01:00 local (CEST), not midnight.
    day = date(2026, 10, 25)
    assert service_day_reference(day) == utc(2026, 10, 24, 23, 0)
    assert gtfs_time_to_utc(day, "00:00:00").astimezone(PRAGUE).hour == 1
    assert gtfs_time_to_utc(day, "12:00:00") == utc(2026, 10, 25, 11, 0)
    # The service day has 25 hours of wall-clock time but GTFS seconds stay linear.
    assert gtfs_time_to_utc(day, "02:30:00") == utc(2026, 10, 25, 1, 30)
    assert gtfs_time_to_utc(day, "24:00:00") == utc(2026, 10, 25, 23, 0)


def test_dst_start_2027_03_28() -> None:
    # Clocks go forward 02:00 CET -> 03:00 CEST. Noon is CEST (10:00 UTC), so the reference
    # is 22:00 UTC on the 27th, which is 23:00 local on the previous day.
    day = date(2027, 3, 28)
    assert service_day_reference(day) == utc(2027, 3, 27, 22, 0)
    assert gtfs_time_to_utc(day, "12:00:00") == utc(2027, 3, 28, 10, 0)
    assert gtfs_time_to_utc(day, "08:00:00").astimezone(PRAGUE).hour == 8


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("0:00:00", 0), ("5:07:09", 18429), ("25:13:00", 90780), ("48:00:00", 172800)],
)
def test_parse_gtfs_time(text: str, seconds: int) -> None:
    assert parse_gtfs_time(text) == seconds


@pytest.mark.parametrize("text", ["", "8:30", "08:60:00", "08:00:61", "x8:00:00", "-1:00:00"])
def test_parse_gtfs_time_rejects_invalid(text: str) -> None:
    with pytest.raises(ValueError, match="not a GTFS time"):
        parse_gtfs_time(text)


service_dates = st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31))
gtfs_seconds = st.integers(min_value=0, max_value=48 * 3600 - 1)


def _fmt(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


@given(service_dates, gtfs_seconds)
def test_offset_from_reference_is_exactly_the_gtfs_seconds(day: date, seconds: int) -> None:
    result = gtfs_time_to_utc(day, _fmt(seconds))
    assert result.tzinfo == UTC
    assert result - service_day_reference(day) == timedelta(seconds=seconds)


@given(service_dates, gtfs_seconds, gtfs_seconds)
def test_conversion_is_strictly_monotonic(day: date, a: int, b: int) -> None:
    if a < b:
        assert gtfs_time_to_utc(day, _fmt(a)) < gtfs_time_to_utc(day, _fmt(b))


@given(service_dates)
def test_noon_is_always_local_noon(day: date) -> None:
    local = gtfs_time_to_utc(day, "12:00:00").astimezone(PRAGUE)
    assert (local.date(), local.hour, local.minute) == (day, 12, 0)


@given(service_dates, gtfs_seconds)
def test_matches_local_midnight_on_days_without_a_clock_change(day: date, seconds: int) -> None:
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=PRAGUE)
    noon = datetime.combine(day, datetime.min.time().replace(hour=12), tzinfo=PRAGUE)
    if midnight.utcoffset() == noon.utcoffset():
        expected = midnight.astimezone(UTC) + timedelta(seconds=seconds)
        assert gtfs_time_to_utc(day, _fmt(seconds)) == expected
