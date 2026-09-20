from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from gantry_check.api.app import create_app
from gantry_check.domain.models import (
    DayType,
    Gantry,
    PublicHoliday,
    RateBand,
    RateSnapshot,
    VehicleType,
)
from gantry_check.repo.sqlite import SqliteRepo


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


def test_estimate_not_implemented(client: TestClient) -> None:
    r = client.post("/estimate", json={"origin": [1.37, 103.85], "destination": [1.28, 103.85]})
    assert r.status_code == 501
