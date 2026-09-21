from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from gantry_check.api.app import create_app, in_singapore
from gantry_check.api.guards import country_guard
from gantry_check.domain.geo import decode_polyline
from gantry_check.domain.models import (
    DayType,
    Gantry,
    LatLng,
    PublicHoliday,
    RateBand,
    RateSnapshot,
    Route,
    VehicleType,
)
from gantry_check.repo.sqlite import SqliteRepo
from gantry_check.routing.base import RoutingError


@pytest.fixture
async def repo() -> SqliteRepo:
    r = SqliteRepo(":memory:")
    await r.apply_migrations()
    await r.upsert_gantries(
        [
            Gantry(
                number="35", name="CTE before Braddell Road", lat=1.34, lng=103.86, zone_id="CT4"
            ),
            Gantry(
                number="47", name="Orchard Road after YMCA", lat=1.30, lng=103.85, zone_id="OC2"
            ),
        ]
    )
    snap = RateSnapshot(
        effective_from=date(2026, 9, 7),
        fetched_at=datetime(2026, 9, 20, tzinfo=UTC),
        html_sha256="h",
        pdf_sha256="p",
    )
    bands = [
        RateBand("35", VehicleType.CAR, DayType.WEEKDAY, 420, 425, 100),
        RateBand("35", VehicleType.CAR, DayType.WEEKDAY, 485, 565, 300),
        RateBand("35", VehicleType.MOTORCYCLE, DayType.WEEKDAY, 485, 565, 150),
        RateBand("47", VehicleType.CAR, DayType.WEEKDAY, 665, 1135, 100),
    ]
    await r.write_snapshot(snap, bands)
    await r.replace_holidays([PublicHoliday(date(2026, 12, 25), "Christmas Day", is_major=True)])
    return r


@pytest.fixture
def client(repo: SqliteRepo) -> TestClient:
    return TestClient(create_app(lambda _req: repo))


def test_index_links_to_docs(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'href="/docs"' in response.text


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["active_snapshot"]["effective_from"] == "2026-09-07"


def test_gantries(client: TestClient) -> None:
    body = client.get("/gantries").json()
    assert [g["number"] for g in body] == ["35", "47"]
    assert body[0]["zone_id"] == "CT4"


def test_rate_weekday_peak(client: TestClient) -> None:
    body = client.get("/rates/35", params={"at": "2026-09-21T08:10:00"}).json()  # Monday
    assert body["day_type"] == "weekday"
    assert body["amount_cents"] == 300
    assert body["amount"] == "$3.00"
    assert body["band"] == {
        "start": "08:05",
        "end": "09:25",
        "amount_cents": 300,
        "amount": "$3.00",
    }
    assert body["at"].startswith("2026-09-21T08:10:00+08:00")


def test_rate_boundary_and_utc_input(client: TestClient) -> None:
    # 09:25 SGT is the exclusive end -> no charge. Sent as UTC.
    body = client.get("/rates/35", params={"at": "2026-09-21T01:25:00Z"}).json()
    assert body["amount_cents"] == 0 and body["band"] is None


def test_rate_motorcycle(client: TestClient) -> None:
    body = client.get(
        "/rates/35", params={"at": "2026-09-21T08:10:00", "vehicle": "motorcycle"}
    ).json()
    assert body["amount_cents"] == 150


def test_rate_sunday_and_holiday_free(client: TestClient) -> None:
    sun = client.get("/rates/35", params={"at": "2026-09-20T08:10:00"}).json()
    assert sun["day_type"] == "sunday_ph" and sun["amount_cents"] == 0
    xmas = client.get("/rates/35", params={"at": "2026-12-25T08:10:00"}).json()
    assert xmas["day_type"] == "sunday_ph" and xmas["amount_cents"] == 0
    eve = client.get("/rates/35", params={"at": "2026-12-24T08:10:00"}).json()
    assert eve["day_type"] == "eve_major_ph_weekday"


def test_rate_unknown_gantry(client: TestClient) -> None:
    assert client.get("/rates/999", params={"at": "2026-09-21T08:10:00"}).status_code == 404


def test_table(client: TestClient) -> None:
    body = client.get("/rates/47/table").json()
    assert body["bands"] == [
        {"start": "11:05", "end": "18:55", "amount_cents": 100, "amount": "$1.00"}
    ]


# ------------------------------------------------------------------- POST /estimate

# The `repo` fixture puts gantry 35 at (1.34, 103.86); this route runs straight through it.
FAKE_ROUTE = Route(
    points=[
        LatLng(lat=1.3415, lng=103.86),
        LatLng(lat=1.3400, lng=103.86),
        LatLng(lat=1.3385, lng=103.86),
    ],
    cumulative_seconds=[0.0, 60.0, 120.0],
    distance_m=334.0,
    duration_s=120.0,
    engine="fake",
    summary="via CTE",
)


class FakeEngine:
    name = "fake"

    def __init__(self, result: Route = FAKE_ROUTE) -> None:
        self._result = result

    async def route(
        self, origin: LatLng, destination: LatLng, depart_at: datetime | None = None
    ) -> Route:
        return self._result


@pytest.fixture
def estimating_client(repo: SqliteRepo) -> TestClient:
    return TestClient(create_app(lambda _req: repo, lambda _req: FakeEngine()))


def _body(**overrides: object) -> dict[str, object]:
    return {
        "origin": [1.3415, 103.86],
        "destination": [1.3385, 103.86],
        "depart_at": "2026-09-21T08:10:00",  # Monday
        **overrides,
    }


def test_estimate_returns_the_charged_gantries(estimating_client: TestClient) -> None:
    r = estimating_client.post("/estimate", json=_body())
    assert r.status_code == 200
    body = r.json()
    assert body["engine"] == "fake"
    assert body["summary"] == "via CTE"
    assert body["vehicle"] == "car"
    assert body["depart_at"].startswith("2026-09-21T08:10:00+08:00")
    assert body["total_cents"] == 300
    assert body["total"] == "$3.00"
    assert len(body["charges"]) == 1
    charge = body["charges"][0]
    assert charge["gantry"] == "35"
    assert charge["name"] == "CTE before Braddell Road"
    assert charge["zone_id"] == "CT4"
    assert charge["method"] == "point"
    assert charge["amount"] == "$3.00"
    assert charge["day_type"] == "weekday"
    assert charge["band"] == {
        "start": "08:05",
        "end": "09:25",
        "amount_cents": 300,
        "amount": "$3.00",
    }
    assert charge["crossed_at"].startswith("2026-09-21T08:11:00+08:00")
    assert any("proximity only" in w for w in body["warnings"])

    decoded = decode_polyline(body["polyline"])
    assert len(decoded) == len(FAKE_ROUTE.points)
    for got, want in zip(decoded, FAKE_ROUTE.points, strict=True):
        assert got.lat == pytest.approx(want.lat, abs=1e-5)
        assert got.lng == pytest.approx(want.lng, abs=1e-5)


def test_estimate_sunday_is_free(estimating_client: TestClient) -> None:
    body = estimating_client.post("/estimate", json=_body(depart_at="2026-09-20T08:10:00")).json()
    assert body["charges"][0]["day_type"] == "sunday_ph"
    assert body["total_cents"] == 0 and body["total"] == "$0.00"


def test_estimate_without_an_engine_is_503(client: TestClient) -> None:
    r = client.post("/estimate", json=_body())
    assert r.status_code == 503
    assert r.json() == {"detail": "no routing engine configured"}


def test_estimate_unconfigured_engine_factory_is_503(repo: SqliteRepo) -> None:
    def boom(_req: object) -> FakeEngine:
        raise RoutingError("no routing engine configured: set GOOGLE_MAPS_API_KEY")

    unconfigured = TestClient(create_app(lambda _req: repo, boom))
    r = unconfigured.post("/estimate", json=_body())
    assert r.status_code == 503
    assert r.json() == {"detail": "no routing engine configured"}


def test_estimate_routing_failure_is_502(repo: SqliteRepo) -> None:
    class FailingEngine:
        name = "failing"

        async def route(
            self, origin: LatLng, destination: LatLng, depart_at: datetime | None = None
        ) -> Route:
            raise RoutingError("upstream said no")

    failing = TestClient(create_app(lambda _req: repo, lambda _req: FailingEngine()))
    r = failing.post("/estimate", json=_body())
    assert r.status_code == 502
    assert r.json() == {"detail": "upstream said no"}


def test_estimate_validates_its_body(estimating_client: TestClient) -> None:
    assert estimating_client.post("/estimate", json={"origin": [1.34, 103.86]}).status_code == 422
    assert estimating_client.post("/estimate", json=_body(vehicle="hovercraft")).status_code == 422


def test_estimate_rejects_an_origin_outside_singapore(estimating_client: TestClient) -> None:
    r = estimating_client.post("/estimate", json=_body(origin=[3.14, 101.69]))  # Kuala Lumpur
    assert r.status_code == 422
    assert "outside Singapore" in r.text


def test_estimate_rejects_a_destination_outside_singapore(estimating_client: TestClient) -> None:
    r = estimating_client.post("/estimate", json=_body(destination=[1.05, 104.03]))  # Batam
    assert r.status_code == 422
    assert "outside Singapore" in r.json()["detail"][0]["msg"]


def test_estimate_accepts_a_singapore_trip(estimating_client: TestClient) -> None:
    r = estimating_client.post(
        "/estimate", json=_body(origin=[1.3644, 103.9915], destination=[1.2840, 103.8515])
    )
    assert r.status_code == 200


@pytest.mark.parametrize(
    ("name", "lat", "lng", "inside"),
    [
        ("Raffles Place", 1.2840, 103.8515, True),
        ("Tuas checkpoint", 1.3480, 103.6360, True),
        ("Changi Airport", 1.3644, 103.9915, True),
        ("Kuala Lumpur", 3.14, 101.69, False),
        ("Batam", 1.05, 104.03, False),
    ],
)
def test_in_singapore(name: str, lat: float, lng: float, inside: bool) -> None:
    assert in_singapore(lat, lng) is inside, name


# ------------------------------------------------------------------- guards


@pytest.fixture
def guarded_client(repo: SqliteRepo) -> TestClient:
    return TestClient(
        create_app(
            lambda _r: repo,
            lambda _r: FakeEngine(),
            estimate_guards=[country_guard(frozenset({"SG"}))],
        )
    )


def test_country_guard_allows_singapore(guarded_client: TestClient) -> None:
    r = guarded_client.post("/estimate", json=_body(), headers={"cf-ipcountry": "SG"})
    assert r.status_code == 200


def test_country_guard_refuses_other_countries(guarded_client: TestClient) -> None:
    r = guarded_client.post("/estimate", json=_body(), headers={"cf-ipcountry": "US"})
    assert r.status_code == 403
    assert r.json() == {"detail": "Estimates are only available from Singapore."}


def test_country_guard_allows_a_missing_header(guarded_client: TestClient) -> None:
    # No Cloudflare in front (local dev, tests): there is no country to check, so allow.
    assert guarded_client.post("/estimate", json=_body()).status_code == 200


def test_country_guard_runs_before_body_validation(guarded_client: TestClient) -> None:
    # Guards are route dependencies, which FastAPI resolves before validating the body: a
    # blocked country gets 403 even when the body would otherwise be a 422.
    r = guarded_client.post(
        "/estimate", json={"origin": [1.34, 103.86]}, headers={"cf-ipcountry": "US"}
    )
    assert r.status_code == 403
