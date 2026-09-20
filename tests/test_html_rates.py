"""Tests for the per-gantry LTA HTML rate table parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from gantry_check.domain.models import DayType, RateBand, VehicleType
from gantry_check.ingest.html_rates import parse_html_table

FIXTURES = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def triples(bands: list[RateBand]) -> list[tuple[int, int, int]]:
    return [(b.start_min, b.end_min, b.amount_cents) for b in bands]


def test_gantry_35_weekday() -> None:
    bands = parse_html_table(read("35-table-0-0.html"), "35", VehicleType.CAR, DayType.WEEKDAY)
    assert triples(bands) == [
        (420, 425, 100),
        (425, 480, 200),
        (480, 485, 250),
        (485, 565, 300),
        (565, 570, 200),
        (570, 595, 100),
        (595, 600, 50),
        (600, 1200, 0),
    ]
    assert len(bands) == 8
    assert all(b.gantry_number == "35" for b in bands)
    assert all(b.vehicle_type is VehicleType.CAR for b in bands)
    assert all(b.day_type is DayType.WEEKDAY for b in bands)


def test_gantry_35_saturday_is_not_in_operation() -> None:
    assert (
        parse_html_table(read("35-table-0-1.html"), "35", VehicleType.CAR, DayType.SATURDAY) == []
    )


def test_gantry_35_eve_of_major_ph_matches_the_weekday_table() -> None:
    weekday = parse_html_table(read("35-table-0-0.html"), "35", VehicleType.CAR, DayType.WEEKDAY)
    eve = parse_html_table(
        read("35-table-0-2.html"), "35", VehicleType.CAR, DayType.EVE_MAJOR_PH_WEEKDAY
    )
    assert len(eve) == 8
    assert triples(eve) == triples(weekday)
    assert all(b.day_type is DayType.EVE_MAJOR_PH_WEEKDAY for b in eve)


def test_gantry_47_weekday_keeps_zero_amount_bands() -> None:
    bands = parse_html_table(read("47-table-0-0.html"), "47", VehicleType.CAR, DayType.WEEKDAY)
    assert triples(bands) == [
        (420, 660, 0),
        (660, 665, 50),
        (665, 1135, 100),
        (1135, 1140, 50),
        (1140, 1200, 0),
    ]
    assert len(bands) == 5


def test_gantry_1_weekday_is_not_in_operation() -> None:
    assert parse_html_table(read("1-table-0-0.html"), "1", VehicleType.CAR, DayType.WEEKDAY) == []


def test_bands_are_half_open_and_contiguous() -> None:
    bands = parse_html_table(read("35-table-0-0.html"), "35", VehicleType.CAR, DayType.WEEKDAY)
    for previous, following in zip(bands, bands[1:], strict=False):
        assert previous.end_min == following.start_min
    assert bands[3].contains(485)
    assert bands[3].contains(564)
    assert not bands[3].contains(565)


def test_missing_table_raises_with_identity() -> None:
    with pytest.raises(ValueError, match=r"gantry 35 car/weekday \(35-table-0-0\)"):
        parse_html_table(
            "<html><body>404 Not Found</body></html>", "35", VehicleType.CAR, DayType.WEEKDAY
        )


def test_unparseable_amount_raises() -> None:
    html = '<table class="styler"><tr><td>07:00 - 08:00</td><td>1.00</td></tr></table>'
    with pytest.raises(ValueError, match="unparseable amount"):
        parse_html_table(html, "99", VehicleType.HGV, DayType.SATURDAY)


def test_overlapping_bands_raise() -> None:
    html = (
        '<table class="styler">'
        "<tr><td>07:00 - 08:00</td><td>$1.00</td></tr>"
        "<tr><td>07:30 - 09:00</td><td>$2.00</td></tr>"
        "</table>"
    )
    with pytest.raises(ValueError, match="overlaps"):
        parse_html_table(html, "99", VehicleType.CAR, DayType.WEEKDAY)


def test_backwards_band_raises() -> None:
    html = '<table class="styler"><tr><td>09:00 - 08:00</td><td>$1.00</td></tr></table>'
    with pytest.raises(ValueError, match="does not end after it starts"):
        parse_html_table(html, "99", VehicleType.CAR, DayType.WEEKDAY)
