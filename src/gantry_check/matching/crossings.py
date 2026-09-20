"""Which ERP gantries a route crosses, and when.

Pure stdlib geometry over `gantry_check.domain.geo`, so this module is importable inside
the Cloudflare Python Worker. Two matching methods:

* **line** -- the gantry has a `line_wkt` carriageway line (one line per carriageway). A
  route segment matches when it intersects that line or passes within `line_buffer_m` of
  it, which keeps a route on the opposite carriageway from matching.
* **point** -- the gantry only has its KML point. A route segment matches when it passes
  within `point_radius_m` of the point. Direction cannot be verified this way, so callers
  should surface that as a warning.

Where `Gantry.heading_deg` is known, the route's local bearing must also agree with it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from gantry_check.domain.geo import (
    Point,
    bearing_deg,
    bearing_diff_deg,
    interpolate_seconds,
    parse_wkt_lines,
    point_segment_distance_m,
    to_xy,
)
from gantry_check.domain.models import Gantry, LatLng, Route

Method = Literal["line", "point"]

DEFAULT_LINE_BUFFER_M = 8.0
DEFAULT_POINT_RADIUS_M = 15.0
DEFAULT_HEADING_TOLERANCE_DEG = 90.0

#: How far outside the route's bounding box a gantry may sit and still be a candidate.
#: Generous on purpose: a gantry's KML point can be tens of metres off the carriageway
#: line derived for it, and a false negative here is silent.
_BBOX_MARGIN_M = 100.0

_M_PER_DEG_LAT = 111_320.0


@dataclass(frozen=True, slots=True)
class Crossing:
    """One gantry crossed by a route, at `fraction` along segment `seg_index`."""

    gantry: Gantry
    seg_index: int
    fraction: float
    seconds_from_start: float
    method: Method
    distance_m: float


# --------------------------------------------------------------------------- geometry


def _project_fraction_xy(p: Point, a: Point, b: Point) -> tuple[float, float]:
    """(distance from `p` to segment a-b, fraction along a-b of the closest point)."""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(p[0] - ax, p[1] - ay), 0.0
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy)), t


def _segment_pair_xy(p1: Point, p2: Point, q1: Point, q2: Point) -> tuple[float, float]:
    """(distance between segments p and q, fraction along p1-p2 at the closest approach).

    Intersecting segments give distance 0 and the fraction of the intersection point.
    """
    rx, ry = p2[0] - p1[0], p2[1] - p1[1]
    sx, sy = q2[0] - q1[0], q2[1] - q1[1]
    denom = rx * sy - ry * sx
    if denom != 0.0:
        qpx, qpy = q1[0] - p1[0], q1[1] - p1[1]
        t = (qpx * sy - qpy * sx) / denom
        u = (qpx * ry - qpy * rx) / denom
        if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
            return 0.0, t

    best_d, best_t = _project_fraction_xy(q1, p1, p2)
    for cand_d, cand_t in (
        _project_fraction_xy(q2, p1, p2),
        (_project_fraction_xy(p1, q1, q2)[0], 0.0),
        (_project_fraction_xy(p2, q1, q2)[0], 1.0),
    ):
        if cand_d < best_d:
            best_d, best_t = cand_d, cand_t
    return best_d, best_t


def _heading_ok(a: LatLng, b: LatLng, heading_deg: float | None, tolerance_deg: float) -> bool:
    """True when the segment a->b travels in the charged direction (or no heading is known)."""
    if heading_deg is None:
        return True
    if a.lat == b.lat and a.lng == b.lng:
        return False
    return bearing_diff_deg(bearing_deg(a, b), heading_deg) <= tolerance_deg


# --------------------------------------------------------------------------- matching


def _route_bbox(points: Sequence[LatLng], margin_m: float) -> tuple[float, float, float, float]:
    """(min_lat, min_lng, max_lat, max_lng) around `points`, expanded by `margin_m`."""
    lats = [p.lat for p in points]
    lngs = [p.lng for p in points]
    d_lat = margin_m / _M_PER_DEG_LAT
    mid_lat = (min(lats) + max(lats)) / 2.0
    d_lng = margin_m / max(1.0, _M_PER_DEG_LAT * math.cos(math.radians(mid_lat)))
    return min(lats) - d_lat, min(lngs) - d_lng, max(lats) + d_lat, max(lngs) + d_lng


def _in_bbox(gantry: Gantry, bbox: tuple[float, float, float, float]) -> bool:
    min_lat, min_lng, max_lat, max_lng = bbox
    return min_lat <= gantry.lat <= max_lat and min_lng <= gantry.lng <= max_lng


def _gantry_lines(gantry: Gantry) -> list[list[LatLng]] | None:
    """The gantry's carriageway lines, or None to fall back to point matching.

    Unparseable geometry is treated as missing geometry rather than as a gantry that
    cannot be crossed: proximity matching still catches it, with its usual warning.
    """
    if not gantry.line_wkt:
        return None
    try:
        return parse_wkt_lines(gantry.line_wkt)
    except ValueError:
        return None


def _match_line(
    route: Route,
    gantry: Gantry,
    lines: Sequence[Sequence[LatLng]],
    route_xy: Sequence[Point],
    origin: LatLng,
    buffer_m: float,
    heading_tolerance_deg: float,
) -> Crossing | None:
    """First route segment that hits any of the gantry's carriageway lines."""
    points = route.points
    lines_xy = [[to_xy(p, origin) for p in line] for line in lines]
    vertices = [v for line_xy in lines_xy for v in line_xy]
    # Cheap per-segment reject box: nothing outside it can be within `buffer_m` of a line.
    min_x = min(v[0] for v in vertices) - buffer_m
    max_x = max(v[0] for v in vertices) + buffer_m
    min_y = min(v[1] for v in vertices) - buffer_m
    max_y = max(v[1] for v in vertices) + buffer_m

    for i in range(len(points) - 1):
        p1, p2 = route_xy[i], route_xy[i + 1]
        if min(p1[0], p2[0]) > max_x or max(p1[0], p2[0]) < min_x:
            continue
        if min(p1[1], p2[1]) > max_y or max(p1[1], p2[1]) < min_y:
            continue

        best_d = math.inf
        best_f = 0.0
        for line_xy in lines_xy:
            if len(line_xy) == 1:
                cand_d, cand_f = _project_fraction_xy(line_xy[0], p1, p2)
            else:
                cand_d, cand_f = math.inf, 0.0
                for j in range(len(line_xy) - 1):
                    d, f = _segment_pair_xy(p1, p2, line_xy[j], line_xy[j + 1])
                    if d < cand_d:
                        cand_d, cand_f = d, f
            if cand_d < best_d:
                best_d, best_f = cand_d, cand_f

        if best_d > buffer_m:
            continue
        if not _heading_ok(points[i], points[i + 1], gantry.heading_deg, heading_tolerance_deg):
            continue
        return Crossing(
            gantry=gantry,
            seg_index=i,
            fraction=best_f,
            seconds_from_start=interpolate_seconds(route, i, best_f),
            method="line",
            distance_m=best_d,
        )
    return None


def _match_point(
    route: Route,
    gantry: Gantry,
    route_xy: Sequence[Point],
    origin: LatLng,
    radius_m: float,
    heading_tolerance_deg: float,
) -> Crossing | None:
    """First route segment passing within `radius_m` of the gantry's point.

    This is `geo.route_passes_point`'s scan, inlined so the projection can be shared
    across gantries and each segment rejected with four float comparisons -- the Worker's
    CPU budget is small. Candidates are confirmed with `point_segment_distance_m`, and a
    segment rejected on heading does not stop the scan: the route may come back the
    other way round.
    """
    points = route.points
    target = LatLng(lat=gantry.lat, lng=gantry.lng)
    gx, gy = to_xy(target, origin)

    for i in range(len(points) - 1):
        p1, p2 = route_xy[i], route_xy[i + 1]
        if min(p1[0], p2[0]) > gx + radius_m or max(p1[0], p2[0]) < gx - radius_m:
            continue
        if min(p1[1], p2[1]) > gy + radius_m or max(p1[1], p2[1]) < gy - radius_m:
            continue
        approx_m, fraction = _project_fraction_xy((gx, gy), p1, p2)
        if approx_m > radius_m:
            continue
        distance_m = point_segment_distance_m(target, points[i], points[i + 1])
        if distance_m > radius_m:
            continue
        if not _heading_ok(points[i], points[i + 1], gantry.heading_deg, heading_tolerance_deg):
            continue
        return Crossing(
            gantry=gantry,
            seg_index=i,
            fraction=fraction,
            seconds_from_start=interpolate_seconds(route, i, fraction),
            method="point",
            distance_m=distance_m,
        )
    return None


def find_crossings(
    route: Route,
    gantries: Sequence[Gantry],
    *,
    line_buffer_m: float = DEFAULT_LINE_BUFFER_M,
    point_radius_m: float = DEFAULT_POINT_RADIUS_M,
    heading_tolerance_deg: float = DEFAULT_HEADING_TOLERANCE_DEG,
) -> list[Crossing]:
    """Gantries crossed by `route`, ordered by time from departure.

    Each gantry appears at most once, at its first crossing: a loop that passes the same
    gantry twice is charged once, which matches how a single journey is billed.
    """
    if len(route.points) < 2:
        return []

    bbox = _route_bbox(route.points, _BBOX_MARGIN_M)
    # One equirectangular frame for the whole route: over Singapore the scale error is
    # ~1e-4, i.e. well under a millimetre at the separations being tested here.
    origin = route.points[0]
    route_xy = [to_xy(p, origin) for p in route.points]

    crossings: list[Crossing] = []
    for gantry in gantries:
        if not _in_bbox(gantry, bbox):
            continue
        lines = _gantry_lines(gantry)
        if lines is not None:
            found = _match_line(
                route, gantry, lines, route_xy, origin, line_buffer_m, heading_tolerance_deg
            )
        else:
            found = _match_point(
                route, gantry, route_xy, origin, point_radius_m, heading_tolerance_deg
            )
        if found is not None:
            crossings.append(found)

    crossings.sort(key=lambda c: (c.seconds_from_start, c.gantry.number))
    return crossings
