"""Tests for the OneMotoring ERP KML parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from gantry_check.ingest.kml_gantries import load_zones, parse_kml

FIXTURES = Path(__file__).parent / "fixtures"
ZONES_CSV = Path(__file__).resolve().parents[1] / "data" / "static" / "annex_d_zones.csv"


@pytest.fixture(scope="module")
def kml_bytes() -> bytes:
    return (FIXTURES / "erp-kml-0.kml").read_bytes()


@pytest.fixture(scope="module")
def zones() -> dict[str, str]:
    return load_zones(ZONES_CSV)


def test_parses_every_gantry(kml_bytes: bytes) -> None:
    gantries = parse_kml(kml_bytes)
    assert len(gantries) == 78


def test_gantry_numbers_are_unique(kml_bytes: bytes) -> None:
    numbers = [gantry.number for gantry in parse_kml(kml_bytes)]
    assert len(set(numbers)) == len(numbers)


def test_sorted_numerically_not_lexicographically(kml_bytes: bytes) -> None:
    numbers = [int(gantry.number) for gantry in parse_kml(kml_bytes)]
    assert numbers == sorted(numbers)
    assert numbers[0] == 1


def test_gantry_35(kml_bytes: bytes, zones: dict[str, str]) -> None:
    gantry = next(g for g in parse_kml(kml_bytes, zones) if g.number == "35")
    assert gantry.name == "CTE before Braddell Road"
    assert gantry.lat == pytest.approx(1.34630898728722)
    assert gantry.lng == pytest.approx(103.859544595156)
    assert gantry.zone_id == "CT4"
    assert gantry.source == "onemotoring-kml"


def test_gantry_1(kml_bytes: bytes, zones: dict[str, str]) -> None:
    gantry = next(g for g in parse_kml(kml_bytes, zones) if g.number == "1")
    assert gantry.name == "Victoria Street"
    assert gantry.zone_id == "BMC"


def test_zone_id_is_none_without_zones(kml_bytes: bytes) -> None:
    assert all(gantry.zone_id is None for gantry in parse_kml(kml_bytes))


def test_every_gantry_has_a_zone(kml_bytes: bytes, zones: dict[str, str]) -> None:
    unmapped = [g.number for g in parse_kml(kml_bytes, zones) if g.zone_id is None]
    assert unmapped == []


def test_load_zones(zones: dict[str, str]) -> None:
    assert zones["1"] == "BMC"
    assert zones["35"] == "CT4"
    assert zones["47"] == "OC2"
    assert len(zones) == 78


def test_rejects_non_kml() -> None:
    with pytest.raises(ValueError, match="no <Placemark>"):
        parse_kml(b"<kml xmlns='http://earth.google.com/kml/2.2'><Document/></kml>")


def test_rejects_name_without_gantry_number() -> None:
    bad = (
        b"<kml xmlns='http://earth.google.com/kml/2.2'><Document><Placemark>"
        b"<name><![CDATA[<td>Somewhere</td>]]></name>"
        b"<Point><coordinates>103.8,1.3,0</coordinates></Point>"
        b"</Placemark></Document></kml>"
    )
    with pytest.raises(ValueError, match="no trailing"):
        parse_kml(bad)
