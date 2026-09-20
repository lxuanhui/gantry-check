"""Pure-python geometry helpers used for route <-> gantry matching."""

from __future__ import annotations

import math

import pytest

from gantry_check.domain.geo import (
    EARTH_RADIUS_M,
    bearing_deg,
    bearing_diff_deg,
    decode_polyline,
    haversine_m,
    interpolate_seconds,
    parse_linestring_wkt,
    point_segment_distance_m,
    route_crosses_line,
    route_passes_point,
    segments_intersect,
    to_xy,
)
from gantry_check.domain.models import LatLng, Route

#: Degrees of latitude that span exactly one metre on our sphere.
DEG_PER_M = 1.0 / (EARTH_RADIUS_M * math.pi / 180.0)


def test_decode_polyline_google_example() -> None:
    points = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert [(p.lat, p.lng) for p in points] == [
        (38.5, -120.2),
        (40.7, -120.95),
        (43.252, -126.453),
    ]


def test_decode_polyline_empty() -> None:
    assert decode_polyline("") == []


def test_decode_polyline_precision_6_roundtrip_shape() -> None:
    # Same encoding decoded at precision 6 yields coordinates 10x smaller.
    p5 = decode_polyline("_p~iF~ps|U")[0]
    p6 = decode_polyline("_p~iF~ps|U", precision=6)[0]
    assert p6.lat == pytest.approx(p5.lat / 10)
    assert p6.lng == pytest.approx(p5.lng / 10)


def test_decode_polyline_rejects_truncated_input() -> None:
    with pytest.raises(ValueError):
        decode_polyline("_p~iF")


def test_haversine_one_km_along_a_meridian() -> None:
    a = LatLng(1.3, 103.8)
    b = LatLng(1.3 + 1000 * DEG_PER_M, 103.8)
    assert haversine_m(a, b) == pytest.approx(1000.0, abs=1.0)


def test_haversine_zero_distance() -> None:
    a = LatLng(1.3, 103.8)
    assert haversine_m(a, a) == 0.0


def test_bearing_north_and_east() -> None:
    origin = LatLng(1.3, 103.8)
    north = LatLng(1.31, 103.8)
    east = LatLng(1.3, 103.81)
    assert bearing_deg(origin, north) == pytest.approx(0.0, abs=0.05)
    assert bearing_deg(origin, east) == pytest.approx(90.0, abs=0.05)
    assert bearing_deg(origin, LatLng(1.29, 103.8)) == pytest.approx(180.0, abs=0.05)
    assert bearing_deg(origin, LatLng(1.3, 103.79)) == pytest.approx(270.0, abs=0.05)


def test_bearing_diff_wraps_around_north() -> None:
    assert bearing_diff_deg(350.0, 10.0) == pytest.approx(20.0)
    assert bearing_diff_deg(10.0, 350.0) == pytest.approx(20.0)
    assert bearing_diff_deg(0.0, 180.0) == pytest.approx(180.0)
    assert bearing_diff_deg(90.0, 90.0) == 0.0
    assert 0.0 <= bearing_diff_deg(359.9, 0.1) <= 180.0


def test_to_xy_is_metres_east_and_north() -> None:
    origin = LatLng(1.3, 103.8)
    x, y = to_xy(LatLng(1.3 + 100 * DEG_PER_M, 103.8), origin)
    assert x == pytest.approx(0.0, abs=1e-6)
    assert y == pytest.approx(100.0, abs=0.01)
    assert to_xy(origin, origin) == (0.0, 0.0)


def test_segments_intersect_proper_and_touching() -> None:
    assert segments_intersect((0, 0), (10, 0), (5, -5), (5, 5)) is True
    assert segments_intersect((0, 0), (10, 0), (5, 0), (5, 5)) is True  # touching (T)
    assert segments_intersect((0, 0), (10, 0), (10, 0), (20, 0)) is True  # collinear endpoint
    assert segments_intersect((0, 0), (10, 0), (0, 1), (10, 1)) is False  # parallel
    assert segments_intersect((0, 0), (1, 0), (2, -5), (2, 5)) is False  # too short


def test_point_segment_distance() -> None:
    a = LatLng(1.3, 103.8)
    b = LatLng(1.3, 103.81)
    # 50 m north of the middle of the segment.
    p = LatLng(1.3 + 50 * DEG_PER_M, 103.805)
    assert point_segment_distance_m(p, a, b) == pytest.approx(50.0, abs=0.5)
    # Beyond the end of the segment: distance to the endpoint.
    past = LatLng(1.3, 103.81 + 100 * DEG_PER_M / math.cos(math.radians(1.3)))
    assert point_segment_distance_m(past, a, b) == pytest.approx(100.0, abs=1.0)


def test_point_segment_distance_degenerate_segment() -> None:
    a = LatLng(1.3, 103.8)
    p = LatLng(1.3 + 25 * DEG_PER_M, 103.8)
    assert point_segment_distance_m(p, a, a) == pytest.approx(25.0, abs=0.5)


def test_parse_linestring_wkt() -> None:
    points = parse_linestring_wkt("LINESTRING(103.8 1.3, 103.81 1.31)")
    assert [(p.lat, p.lng) for p in points] == [(1.3, 103.8), (1.31, 103.81)]


def test_parse_linestring_wkt_is_lenient() -> None:
    points = parse_linestring_wkt("  linestring (103.8 1.3,103.81 1.31)  ")
    assert len(points) == 2
    assert points[0].lng == 103.8


def test_parse_linestring_wkt_rejects_other_geometry() -> None:
    with pytest.raises(ValueError):
        parse_linestring_wkt("POINT(103.8 1.3)")
    with pytest.raises(ValueError):
        parse_linestring_wkt("LINESTRING(103.8)")


# A west->east route along latitude 1.3, four segments of ~222 m each.
ROUTE = [LatLng(1.3, 103.800 + 0.002 * i) for i in range(5)]


def test_route_crosses_a_perpendicular_line() -> None:
    # A short line across the carriageway at lng 103.805 -> inside segment index 2.
    line = [LatLng(1.3 - 0.0005, 103.805), LatLng(1.3 + 0.0005, 103.805)]
    assert route_crosses_line(ROUTE, line, buffer_m=0.0) == 2


def test_route_misses_a_parallel_line_within_a_tight_buffer() -> None:
    offset = 50 * DEG_PER_M
    line = [LatLng(1.3 + offset, 103.802), LatLng(1.3 + offset, 103.808)]
    assert route_crosses_line(ROUTE, line, buffer_m=8.0) is None
    assert route_crosses_line(ROUTE, line, buffer_m=60.0) is not None


def test_route_crosses_line_returns_the_first_segment() -> None:
    # The line starts above the 103.802 vertex, so segment 0 is already within 50 m of it.
    offset = 50 * DEG_PER_M
    line = [LatLng(1.3 + offset, 103.802), LatLng(1.3 + offset, 103.808)]
    assert route_crosses_line(ROUTE, line, buffer_m=60.0) == 0
    # Shift the line east and the first hit moves with it.
    east = [LatLng(1.3 + offset, 103.8055), LatLng(1.3 + offset, 103.808)]
    assert route_crosses_line(ROUTE, east, buffer_m=60.0) == 2


def test_route_crosses_line_edge_cases() -> None:
    line = [LatLng(1.3, 103.805), LatLng(1.31, 103.805)]
    assert route_crosses_line([], line, buffer_m=10.0) is None
    assert route_crosses_line([ROUTE[0]], line, buffer_m=10.0) is None
    assert route_crosses_line(ROUTE, [], buffer_m=10.0) is None
    # A one-point "line" falls back to a point distance check.
    assert route_crosses_line(ROUTE, [LatLng(1.3, 103.805)], buffer_m=1.0) == 2


def test_route_passes_point() -> None:
    near = LatLng(1.3 + 20 * DEG_PER_M, 103.8031)
    assert route_passes_point(ROUTE, near, radius_m=30.0) == 1
    assert route_passes_point(ROUTE, near, radius_m=5.0) is None
    far = LatLng(1.5, 103.9)
    assert route_passes_point(ROUTE, far, radius_m=100.0) is None


def make_route() -> Route:
    return Route(
        points=list(ROUTE),
        cumulative_seconds=[0.0, 30.0, 60.0, 90.0, 120.0],
        distance_m=888.0,
        duration_s=120.0,
        engine="google",
    )


def test_interpolate_seconds() -> None:
    route = make_route()
    assert interpolate_seconds(route, 0) == 15.0
    assert interpolate_seconds(route, 2, 0.25) == 67.5
    assert interpolate_seconds(route, 3, 1.0) == 120.0
    assert interpolate_seconds(route, 4) == 120.0  # last vertex has no segment


def test_interpolate_seconds_clamps_and_validates() -> None:
    route = make_route()
    assert interpolate_seconds(route, 0, -1.0) == 0.0
    assert interpolate_seconds(route, 0, 5.0) == 30.0
    with pytest.raises(IndexError):
        interpolate_seconds(route, 9)
