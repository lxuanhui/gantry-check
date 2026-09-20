"""Curated gantry-geometry overrides: the seeded file, and what a bad file must reject."""

from __future__ import annotations

from pathlib import Path

import pytest

from gantry_check.domain.models import Gantry
from gantry_check.ingest.overrides import (
    GantryOverride,
    apply_overrides,
    load_overrides,
)
from gantry_check.ingest.refresh import DEFAULT_OVERRIDES_CSV

#: The line the seeded file hands to gantry 31; also what the auto-join wrongly gave 67.
BRADDELL_SOUTHBOUND = "LINESTRING(103.862260 1.333367, 103.862578 1.333378)"


def _gantry(number: str, **kwargs: object) -> Gantry:
    defaults: dict[str, object] = {
        "name": f"gantry {number}",
        "lat": 1.33,
        "lng": 103.86,
    }
    defaults.update(kwargs)
    return Gantry(number=number, **defaults)  # type: ignore[arg-type]


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "overrides.csv"
    path.write_text("number,line_wkt,heading_deg,note\n" + body, encoding="utf-8")
    return path


# ----------------------------------------------------------------------------- loading


def test_the_seeded_file_loads_with_its_two_curated_rows() -> None:
    """The file shipped in `data/static` is what the scheduled refresh applies."""
    overrides = {o.number: o for o in load_overrides(DEFAULT_OVERRIDES_CSV)}

    assert set(overrides) == {"31", "67"}
    # Gantry 67 is a *northbound* slip-road gantry; the auto-join gave it a southbound line.
    assert overrides["67"].line_wkt is None
    assert overrides["67"].heading_deg is None
    assert "northbound" in overrides["67"].note
    # Gantry 31 takes over that line. The WKT contains a comma, so this also proves the CSV
    # quotes it properly -- an unquoted cell would truncate the line at the first coordinate.
    assert overrides["31"].line_wkt == BRADDELL_SOUTHBOUND
    assert overrides["31"].heading_deg is None
    assert overrides["31"].note


def test_load_reads_a_heading_and_ignores_blank_rows(tmp_path: Path) -> None:
    path = _write(tmp_path, '12,"LINESTRING(103.8 1.3, 103.81 1.31)",359.5,checked\n\n,,,\n')
    assert load_overrides(path) == [
        GantryOverride(
            number="12",
            line_wkt="LINESTRING(103.8 1.3, 103.81 1.31)",
            heading_deg=359.5,
            note="checked",
        )
    ]


def test_load_rejects_a_missing_column(tmp_path: Path) -> None:
    path = tmp_path / "overrides.csv"
    path.write_text("number,line_wkt\n12,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing column"):
        load_overrides(path)


def test_load_rejects_unparseable_wkt(tmp_path: Path) -> None:
    path = _write(tmp_path, "12,POINT(103.8 1.3),,nope\n")
    with pytest.raises(ValueError, match="not a LINESTRING"):
        load_overrides(path)


@pytest.mark.parametrize("heading", ["360", "-1", "north"])
def test_load_rejects_a_bad_heading(tmp_path: Path, heading: str) -> None:
    path = _write(tmp_path, f"12,,{heading},nope\n")
    with pytest.raises(ValueError, match="heading_deg"):
        load_overrides(path)


def test_load_rejects_a_duplicate_gantry_number(tmp_path: Path) -> None:
    path = _write(tmp_path, "12,,,first\n12,,,second\n")
    with pytest.raises(ValueError, match="duplicate gantry number '12'"):
        load_overrides(path)


# ---------------------------------------------------------------------------- applying


def test_apply_sets_a_line_and_a_heading() -> None:
    gantries = [_gantry("31"), _gantry("35", line_wkt="LINESTRING(1 1, 2 2)")]
    override = GantryOverride("31", BRADDELL_SOUTHBOUND, 190.0, "inferred")

    updated, applied = apply_overrides(gantries, [override], log=lambda _: None)

    assert applied == ["31"]
    assert updated[0].line_wkt == BRADDELL_SOUTHBOUND
    assert updated[0].heading_deg == 190.0
    assert updated[0].name == "gantry 31"  # nothing else about the gantry changes
    assert updated[1] == gantries[1]  # untouched gantries are passed straight through
    assert gantries[0].line_wkt is None  # the input list is not mutated


def test_apply_clears_a_wrongly_joined_line() -> None:
    gantries = [_gantry("67", line_wkt=BRADDELL_SOUTHBOUND, heading_deg=10.0)]
    override = GantryOverride("67", None, None, "wrong carriageway")

    updated, applied = apply_overrides(gantries, [override], log=lambda _: None)

    assert applied == ["67"]
    assert updated[0].line_wkt is None
    assert updated[0].heading_deg is None


def test_apply_returns_numbers_in_gantry_order_not_file_order() -> None:
    """The log line and the stored meta value must be stable across refreshes."""
    gantries = [_gantry("31"), _gantry("46"), _gantry("67")]
    overrides = [GantryOverride("67", None, None, ""), GantryOverride("31", None, None, "")]

    _, applied = apply_overrides(gantries, overrides, log=lambda _: None)

    assert applied == ["31", "67"]


def test_apply_warns_and_skips_an_override_for_an_unknown_gantry() -> None:
    """A stale override must not fail a scheduled refresh."""
    gantries = [_gantry("31")]
    overrides = [
        GantryOverride("31", None, None, "known"),
        GantryOverride("999", BRADDELL_SOUTHBOUND, None, "retired"),
    ]
    logged: list[str] = []

    updated, applied = apply_overrides(gantries, overrides, log=logged.append)

    assert applied == ["31"]
    assert len(updated) == 1
    assert any("WARNING" in line and "999" in line for line in logged)
