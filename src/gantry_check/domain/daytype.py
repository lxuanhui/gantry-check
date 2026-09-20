"""Which ERP rate table applies at a given instant.

Pure stdlib (no zoneinfo: Pyodide may ship without tzdata), so this module is safe to
import inside the Cloudflare Python Worker.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta

from gantry_check.domain.models import SGT, DayType, PublicHoliday

_SATURDAY = 5
_SUNDAY = 6


def to_sgt(at: datetime) -> datetime:
    """Return `at` in Singapore time. A naive datetime is assumed to already be SGT."""
    if at.tzinfo is None:
        return at.replace(tzinfo=SGT)
    return at.astimezone(SGT)


def minute_of_day(at: datetime) -> int:
    """Minutes since midnight SGT (0-1439)."""
    local = to_sgt(at)
    return local.hour * 60 + local.minute


def _as_holiday_map(
    holidays: Mapping[date, PublicHoliday] | Iterable[PublicHoliday],
) -> Mapping[date, PublicHoliday]:
    if isinstance(holidays, Mapping):
        return holidays
    return {h.date: h for h in holidays}


def day_type_for(
    at: datetime,
    holidays: Mapping[date, PublicHoliday] | Iterable[PublicHoliday],
) -> DayType:
    """Classify the SGT calendar date of `at` into the rate table that applies.

    Rules (in order):
      * the date is a public holiday, or a Sunday -> SUNDAY_PH (ERP is not charged)
      * the next calendar day is a *major* public holiday -> EVE_MAJOR_PH_SATURDAY on a
        Saturday, EVE_MAJOR_PH_WEEKDAY Mon-Fri
      * otherwise SATURDAY on a Saturday, WEEKDAY Mon-Fri
    """
    by_date = _as_holiday_map(holidays)
    local_date = to_sgt(at).date()
    weekday = local_date.weekday()

    if local_date in by_date or weekday == _SUNDAY:
        return DayType.SUNDAY_PH

    tomorrow = by_date.get(local_date + timedelta(days=1))
    if tomorrow is not None and tomorrow.is_major:
        if weekday == _SATURDAY:
            return DayType.EVE_MAJOR_PH_SATURDAY
        return DayType.EVE_MAJOR_PH_WEEKDAY

    return DayType.SATURDAY if weekday == _SATURDAY else DayType.WEEKDAY
