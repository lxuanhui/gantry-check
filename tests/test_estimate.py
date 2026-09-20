"""Pricing a route: crossings -> day type -> band -> total."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from gantry_check.domain.models import (
    SGT,
    DayType,
    Gantry,
    LatLng,
    PublicHoliday,
    RateBand,
    RateSnapshot,
    Route,
    VehicleType,
)
from gantry_check.matching.estimate import NO_SNAPSHOT_WARNING, estimate_route, estimate_trip
from gantry_check.repo.sqlite import SqliteRepo

# `CTE before Braddell Road (35)`, from tests/fixtures/erp-kml-0.kml.
G35 = Gantry(
    number="35",
    name="CTE before Braddell Road",
    lat=1.34630898728722,
    lng=103.859544595156,
    zone_id="CT4",
)
G47 = Gantry(number="47", name="Orchard Road after YMCA", lat=1.30, lng=103.85, zone_id="OC2")

BANDS = [
    RateBand("35", VehicleType.CAR, DayType.WEEKDAY, 420, 425, 100),
    RateBand("35", VehicleType.CAR, DayType.WEEKDAY, 485, 565, 300),
    RateBand("35", VehicleType.MOTORCYCLE, DayType.WEEKDAY, 485, 565, 150),
    RateBand("47", VehicleType.CAR, DayType.WEEKDAY, 665, 1135, 100),
]


async def _repo(*, with_snapshot: bool = True) -> SqliteRepo:
    repo = SqliteRepo(":memory:")
    await repo.apply_migrations()
    await repo.upsert_gantries([G35, G47])
    if with_snapshot:
        snap = RateSnapshot(
            effective_from=date(2026, 9, 7),
            fetched_at=datetime(2026, 9, 20, tzinfo=UTC),
            html_sha256="h",
            pdf_sha256="p",
        )
        await repo.write_snapshot(snap, BANDS)
    await repo.replace_holidays([PublicHoliday(date(2026, 12, 25), "Christmas Day", is_major=True)])
    return repo


@pytest.fixture
async def repo() -> SqliteRepo:
    return await _repo()


def _route_through_g35() -> Route:
    """Southbound on the CTE past gantry 35; ~48 s in, inside segment 1."""
    lats = (1.3475, 1.3468, 1.3460, 1.3452)
    return Route(
        points=[LatLng(lat=lat, lng=G35.lng) for lat in lats],
        cumulative_seconds=[0.0, 30.0, 60.0, 90.0],
        distance_m=256.0,
        duration_s=90.0,
        engine="test",
        summary="via CTE",
    )


MONDAY_0810 = datetime(2026, 9, 21, 8, 10, tzinfo=SGT)
SUNDAY_0810 = datetime(2026, 9, 20, 8, 10, tzinfo=SGT)


async def test_weekday_peak_charges_once(repo: SqliteRepo) -> None:
    result = await estimate_route(_route_through_g35(), repo, VehicleType.CAR, MONDAY_0810)
    assert [c.gantry.number for c in result.charges] == ["35"]
    charge = result.charges[0]
    assert charge.day_type is DayType.WEEKDAY
    assert charge.amount_cents == 300
    assert charge.band is not None and charge.band.start_min == 485
    assert charge.method == "point"
    assert result.total_cents == 300
    # 08:10 + ~48 s, still inside the 08:05-09:25 band.
    assert charge.crossed_at.utcoffset() == SGT.utcoffset(None)
    assert charge.crossed_at.hour == 8 and charge.crossed_at.minute == 10


async def test_naive_departure_is_singapore_time(repo: SqliteRepo) -> None:
    naive = datetime(2026, 9, 21, 8, 10)
    result = await estimate_route(_route_through_g35(), repo, VehicleType.CAR, naive)
    assert result.depart_at == MONDAY_0810
    assert result.total_cents == 300


async def test_vehicle_class_uses_its_own_table(repo: SqliteRepo) -> None:
    result = await estimate_route(_route_through_g35(), repo, VehicleType.MOTORCYCLE, MONDAY_0810)
    assert result.total_cents == 150


async def test_point_matches_warn_about_direction(repo: SqliteRepo) -> None:
    result = await estimate_route(_route_through_g35(), repo, VehicleType.CAR, MONDAY_0810)
    assert "gantry 35 matched by proximity only; direction not verified" in result.warnings


async def test_route_warnings_are_carried_through(repo: SqliteRepo) -> None:
    route = _route_through_g35()
    route.warnings.append("google failed: boom; used onemap")
    result = await estimate_route(route, repo, VehicleType.CAR, MONDAY_0810)
    assert result.warnings[0] == "google failed: boom; used onemap"


async def test_sunday_is_free(repo: SqliteRepo) -> None:
    result = await estimate_route(_route_through_g35(), repo, VehicleType.CAR, SUNDAY_0810)
    assert [c.day_type for c in result.charges] == [DayType.SUNDAY_PH]
    assert result.charges[0].band is None
    assert result.charges[0].amount_cents == 0
    assert result.total_cents == 0


async def test_outside_every_band_is_free(repo: SqliteRepo) -> None:
    midnight = datetime(2026, 9, 21, 0, 10, tzinfo=SGT)
    result = await estimate_route(_route_through_g35(), repo, VehicleType.CAR, midnight)
    assert result.charges[0].amount_cents == 0 and result.charges[0].band is None
    assert result.total_cents == 0


async def test_missing_snapshot_warns_and_charges_nothing() -> None:
    repo = await _repo(with_snapshot=False)
    result = await estimate_route(_route_through_g35(), repo, VehicleType.CAR, MONDAY_0810)
    assert NO_SNAPSHOT_WARNING in result.warnings
    assert [c.amount_cents for c in result.charges] == [0]
    assert result.charges[0].band is None
    assert result.total_cents == 0


async def test_route_missing_every_gantry_costs_nothing(repo: SqliteRepo) -> None:
    far = Route(
        points=[LatLng(lat=1.40, lng=103.70), LatLng(lat=1.41, lng=103.71)],
        cumulative_seconds=[0.0, 120.0],
        distance_m=1500.0,
        duration_s=120.0,
        engine="test",
    )
    result = await estimate_route(far, repo, VehicleType.CAR, MONDAY_0810)
    assert result.charges == [] and result.total_cents == 0 and result.warnings == []


async def test_estimate_trip_routes_then_prices(repo: SqliteRepo) -> None:
    route = _route_through_g35()

    class FakeEngine:
        name = "fake"
        seen: datetime | None = None

        async def route(
            self, origin: LatLng, destination: LatLng, depart_at: datetime | None = None
        ) -> Route:
            FakeEngine.seen = depart_at
            return route

    result = await estimate_trip(
        FakeEngine(),
        repo,
        LatLng(lat=1.3475, lng=G35.lng),
        LatLng(lat=1.3452, lng=G35.lng),
        datetime(2026, 9, 21, 8, 10),  # naive -> SGT
        VehicleType.CAR,
    )
    assert FakeEngine.seen == MONDAY_0810
    assert result.total_cents == 300
