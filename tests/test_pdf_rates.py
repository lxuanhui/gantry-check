"""Tests for the LTA base rate PDF parser and the PDF-vs-HTML cross-check."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from gantry_check.domain.models import DayType, RateBand, VehicleType
from gantry_check.ingest.html_rates import parse_html_table
from gantry_check.ingest.pdf_rates import PdfRates, compare, normalize, parse_rates_pdf

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def rates() -> PdfRates:
    return parse_rates_pdf((FIXTURES / "erp_rates_2026-09-07.pdf").read_bytes())


def triples(bands: list[RateBand]) -> list[tuple[int, int, int]]:
    return [(b.start_min, b.end_min, b.amount_cents) for b in bands]


def for_gantry(rates: PdfRates, number: str, day: DayType | None = None) -> list[RateBand]:
    return [
        band
        for band in rates.bands
        if band.gantry_number == number and (day is None or band.day_type is day)
    ]


def band(number: str, day: DayType, start: int, end: int, cents: int) -> RateBand:
    return RateBand(
        gantry_number=number,
        vehicle_type=VehicleType.CAR,
        day_type=day,
        start_min=start,
        end_min=end,
        amount_cents=cents,
    )


def test_effective_from(rates: PdfRates) -> None:
    assert rates.effective_from == date(2026, 9, 7)


def test_gantry_numbers(rates: PdfRates) -> None:
    assert len(rates.gantry_numbers) == 78
    assert {"1", "35", "47", "93"} <= rates.gantry_numbers


def test_every_band_is_a_base_car_rate(rates: PdfRates) -> None:
    assert {b.vehicle_type for b in rates.bands} == {VehicleType.CAR}
    assert {b.day_type for b in rates.bands} <= {DayType.WEEKDAY, DayType.SATURDAY}


def test_gantry_35_weekday(rates: PdfRates) -> None:
    """Gantry 35 sits in the arterial/expressway table ('CTE between Ang Mo Kio Ave 1 ...')."""
    assert triples(for_gantry(rates, "35", DayType.WEEKDAY)) == [
        (420, 425, 100),
        (425, 480, 200),
        (480, 485, 250),
        (485, 565, 300),
        (565, 570, 200),
        (570, 595, 100),
        (595, 600, 50),
    ]
    assert band("35", DayType.WEEKDAY, 485, 565, 300) in rates.bands


def test_gantry_47_weekday(rates: PdfRates) -> None:
    """Gantry 47 is a CBD column ('YMCA Gantry and Fort Canning Tunnel Gantry' = '47,49')."""
    assert triples(for_gantry(rates, "47", DayType.WEEKDAY)) == [
        (660, 665, 50),
        (665, 1135, 100),
        (1135, 1140, 50),
    ]
    assert band("47", DayType.WEEKDAY, 665, 1135, 100) in rates.bands


def test_gantry_49_shares_gantry_47s_column(rates: PdfRates) -> None:
    assert triples(for_gantry(rates, "49")) == triples(for_gantry(rates, "47"))


def test_refer_to_lookups_are_expanded(rates: PdfRates) -> None:
    """'Refer to 1' on the CBD pages resolves via the 'Refer #1:' table on the last page."""
    assert {"1", "2", "9", "10", "11", "16", "17", "18", "23"} <= rates.gantry_numbers
    # Every Bugis-Marina Centre cell is blank in this edition, so no bands for gantry 1.
    assert for_gantry(rates, "1") == []


def test_gantry_47_has_no_saturday_charge(rates: PdfRates) -> None:
    assert for_gantry(rates, "47", DayType.SATURDAY) == []


def test_this_edition_has_no_saturday_charges_at_all(rates: PdfRates) -> None:
    """The whole Saturdays half of the CBD tables is blank in the 07 Sep 2026 edition."""
    assert [b for b in rates.bands if b.day_type is DayType.SATURDAY] == []


def test_bands_span_the_page_break(rates: PdfRates) -> None:
    """Orchard runs 15:35-15:55 at the foot of page 1 into 15:55-16:00 at the top of page 2."""
    orchard = [b for b in for_gantry(rates, "13", DayType.WEEKDAY) if b.contains(955)]
    assert len(orchard) == 1
    assert orchard[0].start_min < 955 < orchard[0].end_min


def test_normalize_drops_zero_bands_and_merges() -> None:
    raw = [
        band("99", DayType.WEEKDAY, 420, 600, 0),
        band("99", DayType.WEEKDAY, 600, 660, 100),
        band("99", DayType.WEEKDAY, 660, 700, 100),
        band("99", DayType.WEEKDAY, 700, 720, 200),
        band("99", DayType.WEEKDAY, 720, 800, 0),
    ]
    assert triples(normalize(raw)) == [(600, 700, 100), (700, 720, 200)]


def test_normalize_does_not_merge_across_a_gap() -> None:
    raw = [
        band("99", DayType.WEEKDAY, 600, 660, 100),
        band("99", DayType.WEEKDAY, 700, 760, 100),
    ]
    assert triples(normalize(raw)) == [(600, 660, 100), (700, 760, 100)]


def test_normalize_keeps_gantries_and_day_types_apart() -> None:
    raw = [
        band("98", DayType.WEEKDAY, 600, 660, 100),
        band("99", DayType.WEEKDAY, 660, 700, 100),
        band("98", DayType.SATURDAY, 660, 700, 100),
    ]
    assert len(normalize(raw)) == 3


def test_html_matches_pdf_for_gantry_35(rates: PdfRates) -> None:
    html = parse_html_table(
        (FIXTURES / "35-table-0-0.html").read_text(encoding="utf-8"),
        "35",
        VehicleType.CAR,
        DayType.WEEKDAY,
    )
    assert compare(html, for_gantry(rates, "35")) == []


def test_html_matches_pdf_for_gantry_47(rates: PdfRates) -> None:
    html = parse_html_table(
        (FIXTURES / "47-table-0-0.html").read_text(encoding="utf-8"),
        "47",
        VehicleType.CAR,
        DayType.WEEKDAY,
    )
    assert compare(html, for_gantry(rates, "47")) == []


def test_html_saturday_matches_the_pdfs_silence(rates: PdfRates) -> None:
    html = parse_html_table(
        (FIXTURES / "35-table-0-1.html").read_text(encoding="utf-8"),
        "35",
        VehicleType.CAR,
        DayType.SATURDAY,
    )
    assert html == []
    assert compare(html, for_gantry(rates, "35", DayType.SATURDAY)) == []


def test_compare_reports_a_changed_amount(rates: PdfRates) -> None:
    html = parse_html_table(
        (FIXTURES / "47-table-0-0.html").read_text(encoding="utf-8"),
        "47",
        VehicleType.CAR,
        DayType.WEEKDAY,
    )
    tampered = [b for b in html if b.amount_cents != 100]
    tampered.append(band("47", DayType.WEEKDAY, 665, 1135, 150))
    problems = compare(tampered, for_gantry(rates, "47"))
    assert len(problems) == 2
    assert any("11:05-18:55 $1.50" in p and "not in the PDF" in p for p in problems)
    assert any("11:05-18:55 $1.00" in p and "not in the HTML" in p for p in problems)
    assert all("gantry 47 car/weekday" in p for p in problems)


def test_compare_ignores_non_car_and_eve_tables(rates: PdfRates) -> None:
    eve = parse_html_table(
        (FIXTURES / "35-table-0-2.html").read_text(encoding="utf-8"),
        "35",
        VehicleType.CAR,
        DayType.EVE_MAJOR_PH_WEEKDAY,
    )
    assert eve  # the eve table is populated, but the PDF says nothing about it
    assert compare(eve, for_gantry(rates, "35", DayType.SATURDAY)) == []
