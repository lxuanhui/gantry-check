"""Curated corrections to the automatic gantry-line join, applied after `attach_lines`.

The join in `gantry_check.ingest.gantry_lines` is purely geometric: nearest line within 60 m,
with LTA's own (unreliable) gantry number used only as a tie-break. Where several gantries
cluster inside that radius and none of the nearby lines is numbered, the nearest-line rule can
hand a gantry the line of a different carriageway -- a silent, expensive error, because a route
on the wrong carriageway then gets charged.

This module reads `data/static/gantry_overrides.csv`, a hand-curated file of per-gantry
corrections, and applies them on top of the join. A row *replaces* the gantry's `line_wkt` and
`heading_deg` with its own values, so an empty `line_wkt` cell clears a bad auto-join and drops
the gantry back to (direction-blind, but honest) point matching. See the "Data quality caveats"
section of the README for the rows currently seeded and why.

An override naming a gantry that is not in the KML is a warning, not an error: the scheduled
refresh must not start failing because LTA renumbered or retired a gantry an override mentions.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from gantry_check.domain.geo import parse_wkt_lines
from gantry_check.domain.models import Gantry

#: Columns every overrides file must carry.
REQUIRED_COLUMNS = ("number", "line_wkt", "heading_deg", "note")


@dataclass(frozen=True, slots=True)
class GantryOverride:
    """One curated correction: what this gantry's geometry should be, and why."""

    number: str
    line_wkt: str | None  # None clears whatever the automatic join attached
    heading_deg: float | None  # None means "no direction check"
    note: str  # the evidence for the override; required reading before changing a row


def _cell(row: dict[str, str | None], key: str) -> str:
    return (row.get(key) or "").strip()


def _parse_heading(raw: str, path: Path, number: str) -> float | None:
    if not raw:
        return None
    try:
        heading = float(raw)
    except ValueError as exc:
        raise ValueError(f"{path}: gantry {number}: heading_deg {raw!r} is not a number") from exc
    if not 0.0 <= heading < 360.0:
        raise ValueError(f"{path}: gantry {number}: heading_deg {heading} is outside [0, 360)")
    return heading


def load_overrides(path: str | Path) -> list[GantryOverride]:
    """Read an overrides CSV. Raises `ValueError` on anything a refresh should not silently use.

    Rows without a gantry number are skipped, so blank lines and trailing commas are harmless.
    A `line_wkt` cell is validated through `parse_wkt_lines`, a `heading_deg` cell must be a
    number in [0, 360), and a gantry number may appear at most once.
    """
    csv_path = Path(path)
    overrides: list[GantryOverride] = []
    seen: set[str] = set()
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(REQUIRED_COLUMNS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{csv_path}: missing column(s) {sorted(missing)}")
        for row in reader:
            number = _cell(row, "number")
            if not number:
                continue
            if number in seen:
                raise ValueError(f"{csv_path}: duplicate gantry number {number!r}")
            seen.add(number)

            line_wkt = _cell(row, "line_wkt") or None
            if line_wkt is not None:
                try:
                    parse_wkt_lines(line_wkt)
                except ValueError as exc:
                    raise ValueError(f"{csv_path}: gantry {number}: {exc}") from exc

            overrides.append(
                GantryOverride(
                    number=number,
                    line_wkt=line_wkt,
                    heading_deg=_parse_heading(_cell(row, "heading_deg"), csv_path, number),
                    note=_cell(row, "note"),
                )
            )
    return overrides


def apply_overrides(
    gantries: list[Gantry],
    overrides: list[GantryOverride],
    *,
    log: Callable[[str], None] = print,
) -> tuple[list[Gantry], list[str]]:
    """Replace the geometry of every overridden gantry; return the new list and what was applied.

    The applied numbers come back in `gantries` order, so the log line and the stored meta value
    are stable across refreshes. An override for a gantry that is not in `gantries` is logged as
    a warning and skipped.
    """
    by_number = {override.number: override for override in overrides}
    known = {gantry.number for gantry in gantries}
    for number in by_number:
        if number not in known:
            log(
                f"  WARNING: override for gantry {number}, which is not in the gantry list; skipped"
            )

    applied: list[str] = []
    updated: list[Gantry] = []
    for gantry in gantries:
        override = by_number.get(gantry.number)
        if override is None:
            updated.append(gantry)
            continue
        applied.append(gantry.number)
        updated.append(
            replace(gantry, line_wkt=override.line_wkt, heading_deg=override.heading_deg)
        )
    return updated, applied
