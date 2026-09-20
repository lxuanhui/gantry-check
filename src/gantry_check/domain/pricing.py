"""Charge lookup and money formatting. Pure stdlib, Worker-safe."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from gantry_check.domain.daytype import minute_of_day
from gantry_check.domain.models import DayType, RateBand, VehicleType


def find_band(bands: Sequence[RateBand], minute: int) -> RateBand | None:
    """The band whose half-open window [start_min, end_min) contains `minute`.

    Bands are assumed non-overlapping; the first match wins.
    """
    for band in bands:
        if band.contains(minute):
            return band
    return None


def charge_cents(
    bands: Sequence[RateBand], at: datetime, day_type: DayType
) -> tuple[int, RateBand | None]:
    """The charge at `at` (converted to SGT) plus the band it came from.

    Returns (0, None) on Sundays/public holidays, when ERP is not charged, and whenever
    no band covers the minute.
    """
    if day_type is DayType.SUNDAY_PH:
        return 0, None
    band = find_band(bands, minute_of_day(at))
    if band is None:
        return 0, None
    return band.amount_cents, band


def scale_for_vehicle(base_cents: int, vehicle: VehicleType) -> int:
    """Derive a non-car rate from the car rate via the PCU factor, rounded half-up to a cent.

    Fallback only: LTA publishes a separate table per vehicle class, so this is used solely
    when a per-vehicle table is missing from a snapshot.
    """
    scaled = Decimal(base_cents) * Decimal(str(vehicle.pcu_factor))
    return int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def format_sgd(cents: int) -> str:
    """Format a cent amount as SGD, e.g. 300 -> '$3.00'. Negatives render as '-$1.50'."""
    sign = "-" if cents < 0 else ""
    whole, remainder = divmod(abs(cents), 100)
    return f"{sign}${whole}.{remainder:02d}"
