"""Route/gantry matching, built around the real coordinates of gantry 35.

`CTE before Braddell Road (35)` sits at 1.34630898728722, 103.859544595156 in
`tests/fixtures/erp-kml-0.kml`. The CTE runs roughly north-south there, so the synthetic
routes below run down a constant longitude through the gantry and the synthetic
carriageway lines run east-west across them.
"""

from __future__ import annotations

from gantry_check.domain.models import Gantry, LatLng, Route
from gantry_check.matching.crossings import find_crossings

G35_LAT = 1.34630898728722
G35_LNG = 103.859544595156

#: Degrees of longitude per metre at this latitude (cos(1.35 deg) is ~1).
DEG_LNG_PER_M = 1.0 / 111_289.0
DEG_LAT_PER_M = 1.0 / 111_320.0

# Southbound along the CTE: the gantry falls inside segment 1 (1.3468 -> 1.3460).
SOUTHBOUND_LATS = (1.3475, 1.3468, 1.3460, 1.3452)
CUMULATIVE = (0.0, 30.0, 60.0, 90.0)


def _route(
    lats: tuple[float, ...],
    lng: float = G35_LNG,
    seconds: tuple[float, ...] | None = None,
) -> Route:
    points = [LatLng(lat=lat, lng=lng) for lat in lats]
    cumulative = list(seconds if seconds is not None else CUMULATIVE[: len(lats)])
    return Route(
        points=points,
        cumulative_seconds=cumulative,
        distance_m=300.0,
        duration_s=cumulative[-1],
        engine="test",
    )


def _gantry(
    number: str = "35",
    *,
    lat: float = G35_LAT,
    lng: float = G35_LNG,
    line_wkt: str | None = None,
    heading_deg: float | None = None,
) -> Gantry:
    return Gantry(
        number=number,
        name="CTE before Braddell Road",
        lat=lat,
        lng=lng,
        zone_id="CT4",
        line_wkt=line_wkt,
        heading_deg=heading_deg,
    )


def _crossing_line(offset_m: float = 0.0, half_width_m: float = 20.0, lat: float = G35_LAT) -> str:
    """An east-west line across the route, its centre shifted `offset_m` east."""
    centre = G35_LNG + offset_m * DEG_LNG_PER_M
    half = half_width_m * DEG_LNG_PER_M
    return f"LINESTRING({centre - half} {lat}, {centre + half} {lat})"


# ------------------------------------------------------------------ point method


def test_point_method_matches_route_through_the_gantry() -> None:
    route = _route(SOUTHBOUND_LATS)
    (crossing,) = find_crossings(route, [_gantry()])
    assert crossing.method == "point"
    assert crossing.seg_index == 1
    assert crossing.distance_m < 1.0


def test_point_method_ignores_a_parallel_road_40m_away() -> None:
    route = _route(SOUTHBOUND_LATS, lng=G35_LNG + 40.0 * DEG_LNG_PER_M)
    assert find_crossings(route, [_gantry()]) == []


def test_seconds_interpolate_between_the_bracketing_points() -> None:
    route = _route(SOUTHBOUND_LATS)
    (crossing,) = find_crossings(route, [_gantry()])
    assert 30.0 < crossing.seconds_from_start < 60.0
    assert 0.0 < crossing.fraction < 1.0
    expected = 30.0 + 30.0 * crossing.fraction
    assert abs(crossing.seconds_from_start - expected) < 1e-6


# ------------------------------------------------------------------- line method


def test_line_method_matches_a_line_across_the_carriageway() -> None:
    route = _route(SOUTHBOUND_LATS)
    (crossing,) = find_crossings(route, [_gantry(line_wkt=_crossing_line())])
    assert crossing.method == "line"
    assert crossing.seg_index == 1
    assert crossing.distance_m == 0.0
    assert 30.0 < crossing.seconds_from_start < 60.0


def test_line_method_ignores_the_opposite_carriageway() -> None:
    # A short line 30 m east of the route: its nearest end is ~20 m away, well outside
    # the 8 m buffer, so the route on this carriageway is not charged.
    other_side = _crossing_line(offset_m=30.0, half_width_m=10.0)
    assert find_crossings(_route(SOUTHBOUND_LATS), [_gantry(line_wkt=other_side)]) == []


def test_line_method_handles_multilinestring() -> None:
    near = _crossing_line()
    far = _crossing_line(offset_m=30.0, half_width_m=10.0)
    both = "MULTILINESTRING(({}),({}))".format(
        near[len("LINESTRING(") : -1], far[len("LINESTRING(") : -1]
    )
    (crossing,) = find_crossings(_route(SOUTHBOUND_LATS), [_gantry(line_wkt=both)])
    assert crossing.method == "line"
    assert crossing.seg_index == 1


def test_line_method_respects_the_buffer() -> None:
    # A line that stops 5 m short of the route still counts (within the 8 m buffer).
    near_miss = f"LINESTRING({G35_LNG + 5.0 * DEG_LNG_PER_M} {G35_LAT}, {G35_LNG + 25.0 * DEG_LNG_PER_M} {G35_LAT})"  # noqa: E501
    (crossing,) = find_crossings(_route(SOUTHBOUND_LATS), [_gantry(line_wkt=near_miss)])
    assert 0.0 < crossing.distance_m <= 8.0


# ----------------------------------------------------------------------- heading


def test_heading_rejects_the_opposite_direction() -> None:
    southbound = _gantry(heading_deg=180.0)
    assert find_crossings(_route(SOUTHBOUND_LATS), [southbound])  # travelling south: charged
    northbound_route = _route(tuple(reversed(SOUTHBOUND_LATS)))
    assert find_crossings(northbound_route, [southbound]) == []


def test_heading_rejects_the_opposite_direction_for_lines() -> None:
    southbound = _gantry(line_wkt=_crossing_line(), heading_deg=180.0)
    assert find_crossings(_route(SOUTHBOUND_LATS), [southbound])
    northbound_route = _route(tuple(reversed(SOUTHBOUND_LATS)))
    assert find_crossings(northbound_route, [southbound]) == []


def test_heading_allows_a_later_segment_in_the_right_direction() -> None:
    # South past the gantry, U-turn, north past it again: only the northbound pass counts.
    route = _route((1.3475, 1.3455, 1.3475), seconds=(0.0, 60.0, 120.0))
    (crossing,) = find_crossings(route, [_gantry(heading_deg=0.0)])
    assert crossing.seg_index == 1


# --------------------------------------------------------- de-duplication, ordering


def test_a_gantry_crossed_twice_is_charged_once() -> None:
    route = _route((1.3475, 1.3455, 1.3475), seconds=(0.0, 60.0, 120.0))
    crossings = find_crossings(route, [_gantry()])
    assert len(crossings) == 1
    assert crossings[0].seg_index == 0


def test_crossings_are_ordered_by_time() -> None:
    earlier = _gantry("34", lat=1.3470)
    route = _route(SOUTHBOUND_LATS)
    crossings = find_crossings(route, [_gantry(), earlier])
    assert [c.gantry.number for c in crossings] == ["34", "35"]
    assert crossings[0].seconds_from_start < crossings[1].seconds_from_start


def test_distant_gantries_are_filtered_out() -> None:
    far = _gantry("47", lat=1.30, lng=103.85)
    assert [c.gantry.number for c in find_crossings(_route(SOUTHBOUND_LATS), [_gantry(), far])] == [
        "35"
    ]


def test_a_degenerate_route_matches_nothing() -> None:
    single = Route(
        points=[LatLng(lat=G35_LAT, lng=G35_LNG)],
        cumulative_seconds=[0.0],
        distance_m=0.0,
        duration_s=0.0,
        engine="test",
    )
    assert find_crossings(single, [_gantry()]) == []


def test_unparseable_geometry_falls_back_to_proximity() -> None:
    broken = _gantry(line_wkt="POLYGON((103.85 1.34))")
    (crossing,) = find_crossings(_route(SOUTHBOUND_LATS), [broken])
    assert crossing.method == "point"
