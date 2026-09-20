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

#: An arbitrary southbound-CTE-shaped line, used where the tests below need *some* valid WKT.
BRADDELL_SOUTHBOUND = "LINESTRING(103.862260 1.333367, 103.862578 1.333378)"

#: The line the seeded file hands to gantry 31: the western part of the unnumbered 35 m line.
BRADDELL_MAINLINE_31 = "LINESTRING(103.862260 1.333367, 103.862398 1.333372)"

#: The line the seeded file hands to gantry 35: its auto-joined line with the western 7 m removed.
BRADDELL_35 = "LINESTRING(103.859314 1.346565, 103.859519 1.346641)"

#: The line the seeded file hands to gantry 46: the unnumbered line under the PIE loop.
BRADDELL_46 = "LINESTRING(103.862080 1.332735, 103.862281 1.332751)"

#: The line the seeded file hands to gantry 67: the unnumbered slip-road line, replacing the
#: southbound mainline line the auto-join had wrongly given it.
BRADDELL_67 = "LINESTRING(103.861925 1.333120, 103.861782 1.333189)"

#: The line the seeded file hands to gantry 68: the eastern part of that same 35 m line.
BRADDELL_SLIP_68 = "LINESTRING(103.862461 1.333374, 103.862578 1.333378)"


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


def test_the_seeded_file_loads_with_its_five_curated_rows() -> None:
    """The file shipped in `data/static` is what the scheduled refresh applies."""
    overrides = {o.number: o for o in load_overrides(DEFAULT_OVERRIDES_CSV)}

    assert set(overrides) == {"31", "35", "46", "67", "68"}
    # Gantries 31 and 68 split the unnumbered 35 m line between them: 31 gets the western part
    # (the southbound CTE mainline), 68 the eastern part (the exit slip road). The WKT contains
    # a comma, so this also proves the CSV quotes it properly -- an unquoted cell would truncate
    # the line at the first coordinate.
    assert overrides["31"].line_wkt == BRADDELL_MAINLINE_31
    assert overrides["31"].heading_deg is None
    assert overrides["31"].note
    assert overrides["35"].line_wkt == BRADDELL_35
    assert overrides["35"].heading_deg is None
    assert overrides["35"].note
    assert overrides["46"].line_wkt == BRADDELL_46
    assert overrides["46"].heading_deg is None
    assert overrides["46"].note
    # Gantry 67 is a northbound slip-road gantry; the auto-join had wrongly given it the
    # southbound mainline line, and the override replaces it with the slip road's own line.
    assert overrides["67"].line_wkt == BRADDELL_67
    assert overrides["67"].heading_deg is None
    assert overrides["67"].note
    assert overrides["68"].line_wkt == BRADDELL_SLIP_68
    assert overrides["68"].heading_deg is None
    assert overrides["68"].note


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
