"""Tests for the LTA gantry-line ingest (data.gov.sg "LTA Gantry (GEOJSON)").

`tests/fixtures/lta_gantry.geojson` is the real download, pinned, so the join statistics
asserted here are the ones a live refresh produces.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from gantry_check.domain.models import Gantry, LatLng
from gantry_check.ingest.datagov import POLL_DOWNLOAD_URL
from gantry_check.ingest.gantry_lines import (
    GANTRY_GEOJSON_DATASET_ID,
    GantryLine,
    attach_lines,
    fetch_gantry_geojson,
    parse_gantry_lines,
    to_wkt,
)
from gantry_check.ingest.kml_gantries import parse_kml

FIXTURES = Path(__file__).parent / "fixtures"

#: 106 features, of which 5 duplicate another feature's geometry under a fresh UNIQUE_ID.
FIXTURE_FEATURES = 106
FIXTURE_LINES = 101

#: What `attach_lines` achieves on the real KML + the real GeoJSON at the default radius.
EXPECTED_WITHOUT_LINES = [
    "28",
    "31",
    "36",
    "38",
    "39",
    "46",
    "54",
    "59",
    "65",
    "68",
    "71",
    "91",
    "93",
]


def _description(**attributes: str) -> str:
    """Rebuild data.gov.sg's HTML attribute table, caption row and all."""
    rows = "".join(
        f"<tr> <th>{key}</th> <td>{value}</td> </tr>" for key, value in attributes.items()
    )
    return (
        "<center><table><tr><th colspan='2' align='center'><em>Attributes</em></th></tr>"
        f"{rows}</table></center>"
    )


def _feature(
    coordinates: list[list[float]], geometry_type: str = "LineString", **attrs: str
) -> dict:
    return {
        "type": "Feature",
        "properties": {"Name": "kml_1", "Description": _description(**attrs)},
        "geometry": {"type": geometry_type, "coordinates": coordinates},
    }


def _collection(*features: dict) -> str:
    return json.dumps({"type": "FeatureCollection", "features": list(features)})


@pytest.fixture(scope="module")
def real_lines() -> list[GantryLine]:
    return parse_gantry_lines((FIXTURES / "lta_gantry.geojson").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def real_gantries() -> list[Gantry]:
    return parse_kml((FIXTURES / "erp-kml-0.kml").read_bytes())


# --------------------------------------------------------------------------- parsing


def test_the_fixture_is_the_real_download() -> None:
    document = json.loads((FIXTURES / "lta_gantry.geojson").read_text(encoding="utf-8"))
    assert len(document["features"]) == FIXTURE_FEATURES
    assert {f["geometry"]["type"] for f in document["features"]} == {"LineString"}


def test_parse_gantry_lines_drops_duplicate_geometries(real_lines: list[GantryLine]) -> None:
    assert len(real_lines) == FIXTURE_LINES
    keys = {tuple((p.lng, p.lat) for p in line.points) for line in real_lines}
    assert len(keys) == FIXTURE_LINES


def test_parse_gantry_lines_reads_the_description_table(real_lines: list[GantryLine]) -> None:
    """Attributes live in an HTML table inside `properties.Description`, not in `properties`."""
    hints = [line.number_hint for line in real_lines if line.number_hint]
    assert len(hints) == 85
    assert "47" in hints  # gantry 35's own line has a *blank* GNTRY_NUM
    # LTA's own numbering is unusable as a join key: blanks, UNK, and non-ERP series.
    assert "UNK" in hints
    assert any(hint.startswith("OS") for hint in hints)
    assert any(hint.startswith("80") for hint in hints)


def test_parse_gantry_lines_finds_no_type_field(real_lines: list[GantryLine]) -> None:
    """The GeoJSON export has no type column (the shapefile twin does), so nothing is filtered."""
    assert {line.kind for line in real_lines} == {None}


def test_parse_gantry_lines_keeps_only_erp_when_a_type_field_exists() -> None:
    text = _collection(
        _feature([[103.85, 1.30, 0.0], [103.8501, 1.3001, 0.0]], GNTRY_NUM="1", TYPE="ERP"),
        _feature([[103.86, 1.31, 0.0], [103.8601, 1.3101, 0.0]], GNTRY_NUM="801", TYPE="EMAS"),
    )
    lines = parse_gantry_lines(text)
    assert len(lines) == 1
    assert lines[0].number_hint == "1"
    assert lines[0].kind == "ERP"


def test_parse_gantry_lines_drops_the_z_ordinate() -> None:
    lines = parse_gantry_lines(
        _collection(_feature([[103.85, 1.30, 0.0], [103.8501, 1.3001, 0.0]]))
    )
    assert lines[0].points == [LatLng(lat=1.30, lng=103.85), LatLng(lat=1.3001, lng=103.8501)]


def test_parse_gantry_lines_splits_a_multilinestring() -> None:
    text = _collection(
        _feature(
            [
                [[103.85, 1.30, 0.0], [103.8501, 1.3001, 0.0]],
                [[103.8502, 1.30, 0.0], [103.8503, 1.3001, 0.0]],
            ],
            geometry_type="MultiLineString",
            GNTRY_NUM="7",
        )
    )
    lines = parse_gantry_lines(text)
    assert len(lines) == 2
    assert [line.number_hint for line in lines] == ["7", "7"]


def test_parse_gantry_lines_rejects_rubbish() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_gantry_lines("<html>nope</html>")
    with pytest.raises(ValueError, match="no 'features' array"):
        parse_gantry_lines('{"type": "FeatureCollection"}')
    with pytest.raises(ValueError, match="fewer than two coordinates"):
        parse_gantry_lines(_collection(_feature([[103.85, 1.30, 0.0]])))


# ------------------------------------------------------------------------------- WKT


def _line(*coords: tuple[float, float], hint: str | None = None) -> GantryLine:
    return GantryLine(points=[LatLng(lat=lat, lng=lng) for lng, lat in coords], number_hint=hint)


def test_to_wkt_is_longitude_first_at_six_decimals() -> None:
    wkt = to_wkt([_line((103.8592551, 1.3465431), (103.8595194, 1.3466412))])
    assert wkt == "LINESTRING(103.859255 1.346543, 103.859519 1.346641)"


def test_to_wkt_of_several_lines_is_a_multilinestring() -> None:
    wkt = to_wkt([_line((103.86, 1.30), (103.861, 1.301)), _line((103.85, 1.30), (103.851, 1.301))])
    assert wkt == (
        "MULTILINESTRING((103.850000 1.300000, 103.851000 1.301000),"
        "(103.860000 1.300000, 103.861000 1.301000))"
    )


# --------------------------------------------------------------------------- attaching


def _gantry(number: str, lat: float, lng: float) -> Gantry:
    return Gantry(number=number, name=f"Gantry {number}", lat=lat, lng=lng)


def test_attach_lines_honours_the_distance_threshold() -> None:
    """One degree of latitude is ~111 km, so 0.00045 deg is ~50 m north of the line."""
    gantry = _gantry("1", 1.30045, 103.85)
    line = _line((103.8499, 1.30), (103.8501, 1.30))

    near, stats = attach_lines([gantry], [line], max_distance_m=60.0)
    assert near[0].line_wkt == "LINESTRING(103.849900 1.300000, 103.850100 1.300000)"
    assert stats == type(stats)(
        gantries_with_lines=1, gantries_without_lines=[], unjoined_lines=0, joined_lines=1
    )

    far, far_stats = attach_lines([gantry], [line], max_distance_m=30.0)
    assert far[0].line_wkt is None
    assert far_stats.gantries_without_lines == ["1"]
    assert far_stats.unjoined_lines == 1
    assert far_stats.joined_lines == 0


def test_attach_lines_never_sets_a_heading() -> None:
    """A line across a carriageway is 180-degree ambiguous, so direction stays unknown."""
    updated, _ = attach_lines(
        [_gantry("1", 1.30, 103.85)], [_line((103.8499, 1.30), (103.8501, 1.30))]
    )
    assert updated[0].heading_deg is None


def test_attach_lines_gives_one_gantry_several_lines_a_multilinestring() -> None:
    """An undivided road charged both ways has one line per carriageway."""
    gantry = _gantry("1", 1.30, 103.85)
    updated, stats = attach_lines(
        [gantry],
        [
            _line((103.8499, 1.30005), (103.8501, 1.30005)),
            _line((103.8499, 1.2999), (103.8501, 1.2999)),
        ],
    )
    assert updated[0].line_wkt is not None
    assert updated[0].line_wkt.startswith("MULTILINESTRING((")
    assert updated[0].line_wkt.count("(") == 3
    assert stats.gantries_with_lines == 1
    assert stats.joined_lines == 2


def test_attach_lines_prefers_the_numbered_gantry_over_a_nearer_neighbour() -> None:
    """Fullerton Road in miniature: 63 and 64 are closer to each other than to their own lines."""
    sixty_three = _gantry("63", 1.30020, 103.85)
    sixty_four = _gantry("64", 1.30002, 103.85)
    line = _line((103.8499, 1.30010), (103.8501, 1.30010), hint="63")

    updated, _ = attach_lines([sixty_three, sixty_four], [line])
    by_number = {g.number: g for g in updated}
    assert by_number["63"].line_wkt is not None
    assert by_number["64"].line_wkt is None


def test_attach_lines_ignores_a_hint_that_is_nowhere_near() -> None:
    """LTA numbers two far-apart features 28; the bogus one must not teleport onto gantry 28."""
    gantry = _gantry("28", 1.40, 103.90)
    line = _line((103.8499, 1.30), (103.8501, 1.30), hint="28")
    updated, stats = attach_lines([gantry], [line])
    assert updated[0].line_wkt is None
    assert stats.unjoined_lines == 1


def test_attach_lines_leaves_unknown_hints_to_geometry() -> None:
    gantry = _gantry("1", 1.30, 103.85)
    line = _line((103.8499, 1.30), (103.8501, 1.30), hint="OS041")
    updated, _ = attach_lines([gantry], [line])
    assert updated[0].line_wkt is not None


# ------------------------------------------------------------------- against real data


def test_attach_lines_against_the_real_sources(
    real_gantries: list[Gantry], real_lines: list[GantryLine]
) -> None:
    updated, stats = attach_lines(real_gantries, real_lines)
    assert len(updated) == 78
    assert stats.gantries_with_lines == 65
    assert stats.gantries_without_lines == EXPECTED_WITHOUT_LINES
    assert stats.joined_lines == 71
    assert stats.unjoined_lines == FIXTURE_LINES - 71

    by_number = {g.number: g for g in updated}
    # CTE before Braddell Road: one carriageway, so a plain LINESTRING.
    assert by_number["35"].line_wkt == "LINESTRING(103.859255 1.346543, 103.859519 1.346641)"
    # The two Fullerton Road carriageways each keep their own line.
    assert by_number["63"].line_wkt != by_number["64"].line_wkt
    assert by_number["63"].line_wkt is not None
    assert by_number["64"].line_wkt is not None
    # Five gantries span more than one carriageway.
    multi = sorted(
        (g.number for g in updated if (g.line_wkt or "").startswith("MULTILINESTRING")), key=int
    )
    assert multi == ["9", "23", "50", "55", "56"]
    # Everything else about the gantry is untouched.
    assert by_number["35"].name == "CTE before Braddell Road"
    assert all(g.heading_deg is None for g in updated)


def test_a_tighter_radius_only_loses_gantries(
    real_gantries: list[Gantry], real_lines: list[GantryLine]
) -> None:
    """Sanity check on the chosen radius: 30 m drops a third of the joins, including gantry 35."""
    _, tight = attach_lines(real_gantries, real_lines, max_distance_m=30.0)
    _, default = attach_lines(real_gantries, real_lines)
    assert tight.gantries_with_lines < default.gantries_with_lines
    assert "35" in tight.gantries_without_lines
    assert set(default.gantries_without_lines) < set(tight.gantries_without_lines)


# -------------------------------------------------------------------------- fetching


DOWNLOAD_URL = "https://example.test/gantry.geojson"


@respx.mock
def test_fetch_gantry_geojson_polls_then_downloads(tmp_path: Path) -> None:
    poll = respx.get(POLL_DOWNLOAD_URL.format(dataset_id=GANTRY_GEOJSON_DATASET_ID)).mock(
        return_value=httpx.Response(
            200, json={"data": {"status": "DOWNLOAD_SUCCESS", "url": DOWNLOAD_URL}}
        )
    )
    download = respx.get(DOWNLOAD_URL).mock(return_value=httpx.Response(200, text='{"a": 1}'))

    with httpx.Client() as client:
        assert fetch_gantry_geojson(client, tmp_path) == '{"a": 1}'
        # Second call is served from the cache: data.gov.sg rate-limits the poll endpoint hard.
        assert fetch_gantry_geojson(client, tmp_path) == '{"a": 1}'

    assert poll.call_count == 1
    assert download.call_count == 1
    assert (tmp_path / f"{GANTRY_GEOJSON_DATASET_ID}.geojson").exists()


@respx.mock
def test_fetch_gantry_geojson_accepts_a_response_without_a_status() -> None:
    """The real gantry dataset returns `{"data": {"url": ...}}` with no `status` at all."""
    respx.get(POLL_DOWNLOAD_URL.format(dataset_id=GANTRY_GEOJSON_DATASET_ID)).mock(
        return_value=httpx.Response(201, json={"code": 0, "data": {"url": DOWNLOAD_URL}})
    )
    respx.get(DOWNLOAD_URL).mock(return_value=httpx.Response(200, text='{"a": 1}'))
    with httpx.Client() as client:
        assert fetch_gantry_geojson(client) == '{"a": 1}'
