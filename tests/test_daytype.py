"""Day-type classification, including the SGT conversion."""

from __future__ import annotations

from datetime import UTC, date, datetime

from gantry_check.domain.daytype import day_type_for, minute_of_day, to_sgt
from gantry_check.domain.models import SGT, DayType, PublicHoliday

# Declared explicitly so the tests never depend on real holiday data.
NATIONAL_DAY = PublicHoliday(date(2026, 8, 9), "National Day", is_major=False)
NATIONAL_DAY_OBSERVED = PublicHoliday(date(2026, 8, 10), "National Day (observed)")
CHRISTMAS = PublicHoliday(date(2026, 12, 25), "Christmas Day", is_major=True)
MINOR_HOLIDAY = PublicHoliday(date(2026, 5, 1), "Labour Day", is_major=False)

HOLIDAYS = [NATIONAL_DAY, NATIONAL_DAY_OBSERVED, CHRISTMAS, MINOR_HOLIDAY]


def test_plain_weekday() -> None:
    assert day_type_for(datetime(2026, 9, 21, 8, 30), HOLIDAYS) is DayType.WEEKDAY


def test_plain_saturday() -> None:
    assert day_type_for(datetime(2026, 9, 19, 8, 30), HOLIDAYS) is DayType.SATURDAY


def test_sunday_is_free() -> None:
    assert day_type_for(datetime(2026, 9, 20, 8, 30), HOLIDAYS) is DayType.SUNDAY_PH


def test_public_holiday_is_free() -> None:
    # 2026-08-09 National Day (a Sunday) and the observed Monday holiday after it.
    assert day_type_for(datetime(2026, 8, 9, 8, 30), HOLIDAYS) is DayType.SUNDAY_PH
    assert day_type_for(datetime(2026, 8, 10, 8, 30), HOLIDAYS) is DayType.SUNDAY_PH


def test_eve_of_major_holiday_on_a_weekday() -> None:
    # 2026-12-24, the day before Christmas (is_major).
    assert day_type_for(datetime(2026, 12, 24, 18, 0), HOLIDAYS) is DayType.EVE_MAJOR_PH_WEEKDAY


def test_eve_of_major_holiday_on_a_saturday() -> None:
    # Saturday 2026-09-19, with a major holiday declared on the Sunday after it.
    holidays = [*HOLIDAYS, PublicHoliday(date(2026, 9, 20), "Major PH", is_major=True)]
    assert day_type_for(datetime(2026, 9, 19, 19, 0), holidays) is DayType.EVE_MAJOR_PH_SATURDAY


def test_eve_of_non_major_holiday_is_a_plain_weekday() -> None:
    # 2026-04-30 is a Thursday; Labour Day 2026-05-01 is not a "major" PH.
    assert day_type_for(datetime(2026, 4, 30, 18, 0), HOLIDAYS) is DayType.WEEKDAY


def test_holiday_wins_over_eve_rule() -> None:
    # 2026-12-25 is itself a holiday even though it is a Friday.
    assert day_type_for(datetime(2026, 12, 25, 8, 30), HOLIDAYS) is DayType.SUNDAY_PH


def test_accepts_a_mapping_of_holidays() -> None:
    by_date = {h.date: h for h in HOLIDAYS}
    assert day_type_for(datetime(2026, 12, 24, 18, 0), by_date) is DayType.EVE_MAJOR_PH_WEEKDAY


def test_utc_input_is_converted_to_sgt_first() -> None:
    # Saturday 17:00Z == Sunday 01:00 SGT -> free day.
    at = datetime(2026, 9, 19, 17, 0, tzinfo=UTC)
    assert at.weekday() == 5
    assert day_type_for(at, HOLIDAYS) is DayType.SUNDAY_PH


def test_empty_holidays_is_fine() -> None:
    assert day_type_for(datetime(2026, 12, 24, 18, 0), []) is DayType.WEEKDAY


def test_minute_of_day_naive_is_sgt() -> None:
    assert minute_of_day(datetime(2026, 9, 21, 8, 5)) == 8 * 60 + 5


def test_minute_of_day_converts_from_utc() -> None:
    assert minute_of_day(datetime(2026, 9, 21, 0, 5, tzinfo=UTC)) == 8 * 60 + 5


def test_to_sgt_keeps_the_instant() -> None:
    at = datetime(2026, 9, 21, 0, 5, tzinfo=UTC)
    local = to_sgt(at)
    assert local.utcoffset() == SGT.utcoffset(None)
    assert local == at
