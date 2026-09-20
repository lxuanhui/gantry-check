"""Physical ERP gantry lines from LTA's "LTA Gantry (GEOJSON)" dataset on data.gov.sg.

The OneMotoring KML gives one *point* per ERP gantry, which is all Phase 1 needed. Route
matching needs the gantry's line across the carriageway instead: expressway gantries come in
per-direction pairs a few metres apart, and a route must only be charged when it actually
crosses the carriageway that gantry spans.

The dataset is a KML export dressed as GeoJSON, so every attribute is buried in an HTML table
inside `properties.Description`:

    <th>GNTRY_NUM</th> <td>35</td> <th>UNIQUE_ID</th> <td>3847</td> ...

`GNTRY_NUM` cannot be trusted as a join key -- it is blank for 19 of the 106 features, `UNK`
for 6 more, uses `OS0xx` / `8xx` numbers for LTA's non-ERP (EMAS / directional) gantries, and
numbers two features 28 that sit 3.4 km apart. The join is therefore geometric: each line goes
to the nearest KML gantry point within `max_distance_m`. `GNTRY_NUM` is used only to
disambiguate, and only when it names a gantry that is itself inside the radius -- without that
tie-break the nearest-point rule hands both carriageways of Fullerton Road to gantry 64 and
leaves 63 (the opposite carriageway, 18 m away) with nothing.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from gantry_check.domain.geo import point_segment_distance_m
from gantry_check.domain.models import Gantry, LatLng
from gantry_check.ingest.datagov import download_dataset_text

#: "LTA Gantry (GEOJSON)" -- WGS84 LineStrings, one per gantry structure.
GANTRY_GEOJSON_DATASET_ID = "d_753090823cc9920ac41efaa6530c5893"

SOURCE = "lta-gantry-geojson"

#: Default join radius. See the module docstring / README: LTA's KML points sit anywhere from
#: 0 m to ~60 m off the gantry structure, and 60 m is the largest radius that still produces no
#: join the `GNTRY_NUM` hints contradict.
DEFAULT_MAX_DISTANCE_M = 60.0

#: Attribute keys that may carry the gantry number, in order of preference.
_NUMBER_KEYS = ("GNTRY_NUM", "GANTRY_NUM", "GANTRY_NO", "GNTRY_NO")
#: Attribute keys that may carry the gantry type (`ERP` vs EMAS / directional).
_TYPE_KEYS = ("TYPE", "GNTRY_TYPE", "GANTRY_TYPE", "GNTRY_TYP", "CATEGORY")
#: The only type we keep when the file actually has a type column.
_ERP_TYPE = "ERP"

#: `<th>KEY</th> <td>VALUE</td>` rows of the HTML attribute table in `Description`. The key cell
#: must be markup-free, which skips the table's `<th ...><em>Attributes</em></th>` caption row.
_ATTR_ROW_RE = re.compile(r"<th[^>]*>([^<]*)</th>\s*<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

#: Coordinates are rounded to this many decimals everywhere: ~0.1 m at the equator, which is
#: finer than the data's own precision and keeps the stored WKT stable across refreshes.
_WKT_PRECISION = 6


@dataclass(frozen=True, slots=True)
class GantryLine:
    """One gantry structure: a line across the carriageway(s) it spans."""

    points: list[LatLng]
    number_hint: str | None = None
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class LineJoinStats:
    """What `attach_lines` managed to join."""

    gantries_with_lines: int
    gantries_without_lines: list[str]
    unjoined_lines: int
    joined_lines: int

    def summary(self, total_gantries: int) -> str:
        """One-line log summary, e.g. `65/78 gantries matched, 28 lines unjoined, missing: ...`."""
        missing = ", ".join(self.gantries_without_lines) or "none"
        return (
            f"{self.gantries_with_lines}/{total_gantries} gantries matched, "
            f"{self.unjoined_lines} lines unjoined, missing: {missing}"
        )


# ------------------------------------------------------------------------------ fetching


def fetch_gantry_geojson(client: httpx.Client, cache_dir: Path | None = None) -> str:
    """Download the LTA gantry GeoJSON, optionally reading from / writing to a content cache.

    The cache is checked *before* the poll endpoint is touched: data.gov.sg rate-limits it
    aggressively, and a cached refresh must not need it at all.
    """
    path = cache_dir / f"{GANTRY_GEOJSON_DATASET_ID}.geojson" if cache_dir is not None else None
    if path is not None and path.exists():
        return path.read_text(encoding="utf-8")
    text = download_dataset_text(client, GANTRY_GEOJSON_DATASET_ID)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return text


# ------------------------------------------------------------------------------- parsing


def _clean(value: str) -> str:
    """Flatten one `<td>` of the Description table into plain text."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", value)).split())


def _feature_attributes(properties: dict[str, object]) -> dict[str, str]:
    """Flatten a feature's attributes: plain properties plus the HTML `Description` table."""
    attributes: dict[str, str] = {}
    for key, value in properties.items():
        if isinstance(value, str | int | float) and key.lower() != "description":
            attributes[str(key).upper()] = _clean(str(value))
    description = properties.get("Description") or properties.get("description")
    if isinstance(description, str):
        for key, value in _ATTR_ROW_RE.findall(description):
            attributes[_clean(key).upper()] = _clean(value)
    return attributes


def _first(attributes: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = attributes.get(key)
        if value:
            return value
    return None


def _coordinates_to_points(coordinates: object, feature_index: int) -> list[LatLng]:
    """GeoJSON `[[lng, lat, z?], ...]` -> points. The z ordinate (always 0.0 here) is dropped."""
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise ValueError(f"feature #{feature_index} has fewer than two coordinates")
    points: list[LatLng] = []
    for raw in coordinates:
        if not isinstance(raw, list | tuple) or len(raw) < 2:
            raise ValueError(f"feature #{feature_index} has a malformed coordinate {raw!r}")
        points.append(LatLng(lat=float(raw[1]), lng=float(raw[0])))
    return points


def _geometry_lines(geometry: object, feature_index: int) -> list[list[LatLng]]:
    """One point list per LineString; a MultiLineString yields several."""
    if not isinstance(geometry, dict):
        raise ValueError(f"feature #{feature_index} has no geometry")
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "LineString":
        return [_coordinates_to_points(coordinates, feature_index)]
    if kind == "MultiLineString":
        if not isinstance(coordinates, list) or not coordinates:
            raise ValueError(f"feature #{feature_index} has an empty MultiLineString")
        return [_coordinates_to_points(part, feature_index) for part in coordinates]
    if kind in {"Point", "MultiPoint", "Polygon", "MultiPolygon", None}:
        return []
    raise ValueError(f"feature #{feature_index} has unsupported geometry type {kind!r}")


def _geometry_key(points: list[LatLng]) -> tuple[tuple[float, float], ...]:
    """Identity of a line for de-duplication, direction-insensitive."""
    rounded = tuple((round(p.lng, _WKT_PRECISION), round(p.lat, _WKT_PRECISION)) for p in points)
    return min(rounded, rounded[::-1])


def parse_gantry_lines(geojson_text: str) -> list[GantryLine]:
    """Parse LTA's gantry GeoJSON into one `GantryLine` per distinct line geometry.

    Keeps only `ERP` lines *if* the file carries a type attribute. The GeoJSON export does not
    (unlike the shapefile twin, which has one), so in practice every line is kept and the join
    radius in `attach_lines` is what separates ERP gantries from LTA's EMAS / directional ones.

    Five features in the current file duplicate another feature's geometry under a different
    `UNIQUE_ID`; duplicates are dropped so a gantry does not end up with a MULTILINESTRING made
    of two identical parts.
    """
    try:
        document = json.loads(geojson_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"gantry GeoJSON is not valid JSON: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("features"), list):
        raise ValueError("gantry GeoJSON has no 'features' array")

    features = document["features"]
    if not features:
        raise ValueError("gantry GeoJSON contains no features")

    parsed: list[tuple[dict[str, str], list[LatLng]]] = []
    for index, feature in enumerate(features):
        if not isinstance(feature, dict):
            raise ValueError(f"feature #{index} is not an object")
        properties = feature.get("properties")
        attributes = _feature_attributes(properties if isinstance(properties, dict) else {})
        for points in _geometry_lines(feature.get("geometry"), index):
            parsed.append((attributes, points))

    has_type = any(_first(attributes, _TYPE_KEYS) for attributes, _ in parsed)

    lines: list[GantryLine] = []
    seen: set[tuple[tuple[float, float], ...]] = set()
    for attributes, points in parsed:
        kind = _first(attributes, _TYPE_KEYS)
        if has_type and (kind or "").upper() != _ERP_TYPE:
            continue
        key = _geometry_key(points)
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            GantryLine(points=points, number_hint=_first(attributes, _NUMBER_KEYS), kind=kind)
        )

    if not lines:
        raise ValueError("gantry GeoJSON yielded no usable lines")
    return lines


# -------------------------------------------------------------------------------- joining


def line_distance_m(point: LatLng, line: GantryLine) -> float:
    """Shortest distance in metres from `point` to any segment of `line`."""
    points = line.points
    if len(points) == 1:
        return point_segment_distance_m(point, points[0], points[0])
    best = float("inf")
    for i in range(len(points) - 1):
        distance = point_segment_distance_m(point, points[i], points[i + 1])
        if distance < best:
            best = distance
    return best


def _format_line(points: list[LatLng]) -> str:
    return ", ".join(f"{p.lng:.{_WKT_PRECISION}f} {p.lat:.{_WKT_PRECISION}f}" for p in points)


def to_wkt(lines: list[GantryLine]) -> str:
    """WGS84 WKT for one gantry's line(s): LINESTRING for one, MULTILINESTRING for several."""
    if not lines:
        raise ValueError("no lines to format")
    bodies = sorted(_format_line(line.points) for line in lines)
    if len(bodies) == 1:
        return f"LINESTRING({bodies[0]})"
    return "MULTILINESTRING(" + ",".join(f"({body})" for body in bodies) + ")"


def attach_lines(
    gantries: list[Gantry],
    lines: list[GantryLine],
    max_distance_m: float = DEFAULT_MAX_DISTANCE_M,
) -> tuple[list[Gantry], LineJoinStats]:
    """Join each line to a gantry and return the gantries with `line_wkt` filled in.

    A line joins the nearest gantry within `max_distance_m`, except that a line whose
    `number_hint` names a gantry *also* within `max_distance_m` joins that one instead: two
    gantries on opposite carriageways can be closer to each other than to their own lines.

    A gantry may collect several lines (one per carriageway of an undivided road, or LTA's
    duplicate numbering) and then gets a MULTILINESTRING. Gantries with no line keep
    `line_wkt=None`; `heading_deg` is never set, because a line across a carriageway is
    direction-ambiguous by 180 degrees.
    """
    by_number = {gantry.number: gantry for gantry in gantries}
    joined: dict[str, list[GantryLine]] = {}
    unjoined = 0
    joined_lines = 0

    for line in lines:
        target: str | None = None
        hinted = by_number.get(line.number_hint or "")
        hinted_distance = (
            line_distance_m(LatLng(hinted.lat, hinted.lng), line)
            if hinted is not None
            else float("inf")
        )
        if hinted is not None and hinted_distance <= max_distance_m:
            target = hinted.number
        else:
            best_distance = float("inf")
            for gantry in gantries:
                distance = line_distance_m(LatLng(gantry.lat, gantry.lng), line)
                if distance < best_distance:
                    best_distance = distance
                    target = gantry.number
            if best_distance > max_distance_m:
                target = None
        if target is None:
            unjoined += 1
            continue
        joined.setdefault(target, []).append(line)
        joined_lines += 1

    updated = [
        replace(gantry, line_wkt=to_wkt(joined[gantry.number]))
        if gantry.number in joined
        else gantry
        for gantry in gantries
    ]
    without = [gantry.number for gantry in updated if gantry.line_wkt is None]
    stats = LineJoinStats(
        gantries_with_lines=len(joined),
        gantries_without_lines=without,
        unjoined_lines=unjoined,
        joined_lines=joined_lines,
    )
    return updated, stats
