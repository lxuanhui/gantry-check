"""Pure-Python geometry for matching a driving route against ERP gantry lines.

No shapely / pyproj: this module is imported by the Cloudflare Python Worker, so it must
stay stdlib-only. Distances use a spherical earth; the local work (segment intersection,
point-to-segment distance) happens in a metre-scale equirectangular projection around a
nearby origin, which is accurate well beyond the few hundred metres we care about.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from gantry_check.domain.models import LatLng, Route

#: Mean earth radius (IUGG), metres.
EARTH_RADIUS_M = 6371008.8

Point = tuple[float, float]


# --------------------------------------------------------------------------- polylines


def decode_polyline(encoded: str, precision: int = 5) -> list[LatLng]:
    """Decode a Google encoded polyline into points.

    `precision` is the number of decimal digits the encoder used (5 for Google Directions,
    6 for OSRM / Valhalla).
    """
    factor = float(10**precision)
    points: list[LatLng] = []
    index = 0
    lat = 0
    lng = 0
    length = len(encoded)

    while index < length:
        for is_lat in (True, False):
            result = 0
            shift = 0
            while True:
                if index >= length:
                    raise ValueError("truncated polyline")
                chunk = ord(encoded[index]) - 63
                index += 1
                result |= (chunk & 0x1F) << shift
                shift += 5
                if chunk < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if is_lat:
                lat += delta
            else:
                lng += delta
        points.append(LatLng(lat=lat / factor, lng=lng / factor))

    return points


def encode_polyline(points: Sequence[LatLng], precision: int = 5) -> str:
    """Encode points into a Google encoded polyline string.

    The inverse of `decode_polyline`. Used mainly by tests to build fixtures without
    hand-crafting encoded strings.
    """
    factor = 10**precision
    out: list[str] = []
    prev_lat = 0
    prev_lng = 0

    for point in points:
        lat = round(point.lat * factor)
        lng = round(point.lng * factor)
        out.append(_encode_value(lat - prev_lat))
        out.append(_encode_value(lng - prev_lng))
        prev_lat, prev_lng = lat, lng

    return "".join(out)


def _encode_value(value: int) -> str:
    shifted = ~(value << 1) if value < 0 else value << 1
    chunks: list[str] = []
    while shifted >= 0x20:
        chunks.append(chr((0x20 | (shifted & 0x1F)) + 63))
        shifted >>= 5
    chunks.append(chr(shifted + 63))
    return "".join(chunks)


def _parse_coord_list(body: str, wkt: str, label: str = "LINESTRING") -> list[LatLng]:
    """Parse a bare `lng lat, lng lat, ...` coordinate list (no surrounding parens)."""
    points: list[LatLng] = []
    for raw in body.split(","):
        parts = raw.split()
        if len(parts) < 2:
            raise ValueError(f"malformed coordinate {raw!r} in {wkt!r}")
        lng, lat = float(parts[0]), float(parts[1])
        points.append(LatLng(lat=lat, lng=lng))
    if not points:
        raise ValueError(f"empty {label}: {wkt!r}")
    return points


def parse_linestring_wkt(wkt: str) -> list[LatLng]:
    """Parse `LINESTRING(lng lat, lng lat, ...)` (WKT is x=lng, y=lat) into points."""
    text = wkt.strip()
    upper = text.upper()
    if not upper.startswith("LINESTRING"):
        raise ValueError(f"not a LINESTRING: {wkt!r}")
    open_paren = text.find("(")
    close_paren = text.rfind(")")
    if open_paren == -1 or close_paren <= open_paren:
        raise ValueError(f"malformed LINESTRING: {wkt!r}")
    return _parse_coord_list(text[open_paren + 1 : close_paren], wkt)


def _paren_groups(body: str, wkt: str) -> list[str]:
    """Split `(a b, c d),(e f, g h)` into its top-level parenthesised groups."""
    groups: list[str] = []
    depth = 0
    start = 0
    for i, char in enumerate(body):
        if char == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                groups.append(body[start:i])
            elif depth < 0:
                raise ValueError(f"malformed MULTILINESTRING: {wkt!r}")
    if depth != 0:
        raise ValueError(f"malformed MULTILINESTRING: {wkt!r}")
    return groups


def parse_wkt_lines(wkt: str) -> list[list[LatLng]]:
    """Parse `LINESTRING(...)` or `MULTILINESTRING((...),(...))` into one point list per line.

    A gantry's geometry is one line per carriageway, so a MULTILINESTRING yields several
    independent lines; a LINESTRING yields a single-element list.
    """
    text = wkt.strip()
    upper = text.upper()
    if upper.startswith("MULTILINESTRING"):
        open_paren = text.find("(")
        close_paren = text.rfind(")")
        if open_paren == -1 or close_paren <= open_paren:
            raise ValueError(f"malformed MULTILINESTRING: {wkt!r}")
        groups = _paren_groups(text[open_paren + 1 : close_paren], wkt)
        if not groups:
            raise ValueError(f"empty MULTILINESTRING: {wkt!r}")
        return [_parse_coord_list(g, wkt, "MULTILINESTRING") for g in groups]
    return [parse_linestring_wkt(text)]


# --------------------------------------------------------------------------- distances


def haversine_m(a: LatLng, b: LatLng) -> float:
    """Great-circle distance between two points, in metres."""
    lat1 = math.radians(a.lat)
    lat2 = math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def bearing_deg(a: LatLng, b: LatLng) -> float:
    """Initial great-circle bearing from `a` to `b`, in degrees clockwise from north (0-360)."""
    lat1 = math.radians(a.lat)
    lat2 = math.radians(b.lat)
    dlng = math.radians(b.lng - a.lng)
    y = math.sin(dlng) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return math.degrees(math.atan2(y, x)) % 360.0


def bearing_diff_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two *bearings* in degrees, in [0, 180]."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def to_xy(p: LatLng, origin: LatLng) -> Point:
    """Project `p` to local metres (x=east, y=north) relative to `origin`."""
    x = math.radians(p.lng - origin.lng) * math.cos(math.radians(origin.lat)) * EARTH_RADIUS_M
    y = math.radians(p.lat - origin.lat) * EARTH_RADIUS_M
    return x, y


def _orientation(p: Point, q: Point, r: Point) -> float:
    """Cross product of (q-p) x (r-p): >0 counter-clockwise, <0 clockwise, 0 collinear."""
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    """True when collinear `p` lies within the bounding box of segment a-b."""
    return min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])


def segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """True if segments p1-p2 and q1-q2 intersect, including touching / collinear overlap.

    Operates on projected xy tuples (see `to_xy`).
    """
    d1 = _orientation(q1, q2, p1)
    d2 = _orientation(q1, q2, p2)
    d3 = _orientation(p1, p2, q1)
    d4 = _orientation(p1, p2, q2)

    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        # Proper crossing (strict sign change on both sides).
        if d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0:
            return True

    if d1 == 0 and _on_segment(p1, q1, q2):
        return True
    if d2 == 0 and _on_segment(p2, q1, q2):
        return True
    if d3 == 0 and _on_segment(q1, p1, p2):
        return True
    if d4 == 0 and _on_segment(q2, p1, p2):
        return True
    return False


def _point_segment_distance_xy(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def point_segment_distance_m(p: LatLng, a: LatLng, b: LatLng) -> float:
    """Shortest distance in metres from `p` to the segment a-b."""
    origin = a
    return _point_segment_distance_xy(to_xy(p, origin), to_xy(a, origin), to_xy(b, origin))


def _segment_segment_distance_xy(p1: Point, p2: Point, q1: Point, q2: Point) -> float:
    if segments_intersect(p1, p2, q1, q2):
        return 0.0
    return min(
        _point_segment_distance_xy(p1, q1, q2),
        _point_segment_distance_xy(p2, q1, q2),
        _point_segment_distance_xy(q1, p1, p2),
        _point_segment_distance_xy(q2, p1, p2),
    )


# ----------------------------------------------------------------------- route matching


def route_crosses_line(
    route: Sequence[LatLng], line: Sequence[LatLng], buffer_m: float
) -> int | None:
    """Index of the first route segment (points[i] -> points[i+1]) that hits `line`.

    "Hits" means it intersects the line, or passes within `buffer_m` metres of it.
    Returns None when the route never comes close enough.
    """
    if len(route) < 2 or not line:
        return None

    for i in range(len(route) - 1):
        a, b = route[i], route[i + 1]
        origin = a
        pa, pb = to_xy(a, origin), to_xy(b, origin)

        if len(line) == 1:
            if _point_segment_distance_xy(to_xy(line[0], origin), pa, pb) <= buffer_m:
                return i
            continue

        for j in range(len(line) - 1):
            qa = to_xy(line[j], origin)
            qb = to_xy(line[j + 1], origin)
            if _segment_segment_distance_xy(pa, pb, qa, qb) <= buffer_m:
                return i
    return None


def route_passes_point(route: Sequence[LatLng], p: LatLng, radius_m: float) -> int | None:
    """Index of the first route segment passing within `radius_m` metres of `p`, else None."""
    if len(route) < 2:
        return None
    for i in range(len(route) - 1):
        if point_segment_distance_m(p, route[i], route[i + 1]) <= radius_m:
            return i
    return None


def interpolate_seconds(route: Route, seg_index: int, fraction: float = 0.5) -> float:
    """Cumulative travel seconds at `fraction` along route segment `seg_index`."""
    n = len(route.cumulative_seconds)
    if n == 0:
        raise IndexError("route has no points")
    if not 0 <= seg_index < n:
        raise IndexError(f"segment index {seg_index} out of range for {n} points")
    start = route.cumulative_seconds[seg_index]
    if seg_index == n - 1:
        return start
    end = route.cumulative_seconds[seg_index + 1]
    f = max(0.0, min(1.0, fraction))
    return start + (end - start) * f
