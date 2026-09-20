"""Band lookup, per-vehicle scaling and money formatting."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gantry_check.domain.models import DayType, RateBand, VehicleType
from gantry_check.domain.pricing import charge_cents, find_band, format_sgd, scale_for_vehicle


def band(start: int, end: int, cents: int, day: DayType = DayType.WEEKDAY) -> RateBand:
    return RateBand(
        gantry_number="35",
        vehicle_type=VehicleType.CAR,
        day_type=day,
        start_min=start,
        end_min=end,
        amount_cents=cents,
    )


BANDS = [band(480, 485, 100), band(485, 565, 300), band(565, 600, 200)]


def test_find_band_is_half_open() -> None:
    assert find_band(BANDS, 485) is BANDS[1]
    assert find_band(BANDS, 564) is BANDS[1]
    assert find_band(BANDS, 565) is BANDS[2]


def test_find_band_outside_all_windows() -> None:
    assert find_band(BANDS, 479) is None
    assert find_band(BANDS, 600) is None
    assert find_band([], 500) is None


def test_charge_cents_uses_sgt_minute() -> None:
    cents, matched = charge_cents(BANDS, datetime(2026, 9, 21, 8, 10), DayType.WEEKDAY)
    assert (cents, matched) == (300, BANDS[1])


def test_charge_cents_converts_utc() -> None:
    # 00:10Z == 08:10 SGT.
    cents, matched = charge_cents(BANDS, datetime(2026, 9, 21, 0, 10, tzinfo=UTC), DayType.WEEKDAY)
    assert cents == 300
    assert matched is BANDS[1]


def test_boundary_minute_485_is_charged_565_is_not() -> None:
    two_bands = [band(485, 565, 300)]
    assert charge_cents(two_bands, datetime(2026, 9, 21, 8, 5), DayType.WEEKDAY)[0] == 300
    assert charge_cents(two_bands, datetime(2026, 9, 21, 9, 25), DayType.WEEKDAY) == (0, None)


def test_sunday_ph_is_always_free() -> None:
    always = [band(0, 1440, 500, DayType.SUNDAY_PH)]
    assert charge_cents(always, datetime(2026, 9, 20, 8, 10), DayType.SUNDAY_PH) == (0, None)


def test_no_band_means_no_charge() -> None:
    assert charge_cents(BANDS, datetime(2026, 9, 21, 22, 0), DayType.WEEKDAY) == (0, None)


@pytest.mark.parametrize(
    ("vehicle", "expected"),
    [
        (VehicleType.MOTORCYCLE, 150),
        (VehicleType.CAR, 300),
        (VehicleType.HGV, 450),
        (VehicleType.VHGV, 600),
    ],
)
def test_scale_for_vehicle(vehicle: VehicleType, expected: int) -> None:
    assert scale_for_vehicle(300, vehicle) == expected


def test_scale_for_vehicle_rounds_half_up() -> None:
    # 125 * 0.5 == 62.5 -> 63 (banker's rounding would give 62).
    assert scale_for_vehicle(125, VehicleType.MOTORCYCLE) == 63
    assert scale_for_vehicle(75, VehicleType.HGV) == 113  # 112.5


def test_format_sgd() -> None:
    assert format_sgd(300) == "$3.00"
    assert format_sgd(0) == "$0.00"
    assert format_sgd(5) == "$0.05"
    assert format_sgd(1234) == "$12.34"
    assert format_sgd(-150) == "-$1.50"
