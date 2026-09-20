"""Turn a route into a per-gantry ERP bill.

`estimate_route` prices an existing `Route`; `estimate_trip` fetches one from a routing
engine first. Both are pure orchestration over `matching.crossings`, `domain.daytype` and
`domain.pricing`, with storage behind the `Repo` protocol, so they run unchanged locally
and inside the Cloudflare Worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from gantry_check.domain.daytype import day_type_for, to_sgt
from gantry_check.domain.models import (
    SGT,
    DayType,
    Gantry,
    LatLng,
    RateBand,
    Route,
    VehicleType,
)
from gantry_check.domain.pricing import charge_cents
from gantry_check.matching.crossings import find_crossings
from gantry_check.repo.base import Repo
from gantry_check.routing.base import RoutingEngine

NO_SNAPSHOT_WARNING = "no active rate snapshot; all charges reported as $0.00"


@dataclass(frozen=True, slots=True)
class GantryCharge:
    """One gantry crossing, priced."""

    gantry: Gantry
    crossed_at: datetime  # SGT-aware
    day_type: DayType
    band: RateBand | None  # None when nothing is charged at that minute
    amount_cents: int
    method: str  # "line" | "point"


@dataclass(frozen=True, slots=True)
class Estimate:
    route: Route
    vehicle: VehicleType
    depart_at: datetime  # SGT-aware
    charges: list[GantryCharge]
    total_cents: int
    warnings: list[str]


def _point_warning(gantry: Gantry) -> str:
    return f"gantry {gantry.number} matched by proximity only; direction not verified"


async def estimate_route(
    route: Route,
    repo: Repo,
    vehicle: VehicleType,
    depart_at: datetime,
) -> Estimate:
    """Price every gantry `route` crosses, departing at `depart_at` (naive values are SGT)."""
    depart = to_sgt(depart_at)
    warnings = list(route.warnings)

    crossings = find_crossings(route, await repo.gantries())
    snapshot = await repo.active_snapshot()
    if snapshot is None:
        warnings.append(NO_SNAPSHOT_WARNING)
    holidays = await repo.holidays() if crossings else []

    charges: list[GantryCharge] = []
    for crossing in crossings:
        crossed_at = depart + timedelta(seconds=crossing.seconds_from_start)
        day_type = day_type_for(crossed_at, holidays)
        cents = 0
        band: RateBand | None = None
        # Sundays/PHs have no table at all, and without a snapshot there is nothing to read.
        if snapshot is not None and day_type is not DayType.SUNDAY_PH:
            bands = await repo.bands(crossing.gantry.number, vehicle, day_type)
            cents, band = charge_cents(bands, crossed_at, day_type)
        charges.append(
            GantryCharge(
                gantry=crossing.gantry,
                crossed_at=crossed_at,
                day_type=day_type,
                band=band,
                amount_cents=cents,
                method=crossing.method,
            )
        )
        if crossing.method == "point":
            warnings.append(_point_warning(crossing.gantry))

    return Estimate(
        route=route,
        vehicle=vehicle,
        depart_at=depart,
        charges=charges,
        total_cents=sum(c.amount_cents for c in charges),
        warnings=warnings,
    )


async def estimate_trip(
    engine: RoutingEngine,
    repo: Repo,
    origin: LatLng,
    destination: LatLng,
    depart_at: datetime | None = None,
    vehicle: VehicleType = VehicleType.CAR,
) -> Estimate:
    """Route origin -> destination with `engine`, then price the crossings.

    `depart_at` defaults to now in Singapore time; a naive value is taken as SGT.
    """
    depart = datetime.now(tz=SGT) if depart_at is None else to_sgt(depart_at)
    route = await engine.route(origin, destination, depart)
    return await estimate_route(route, repo, vehicle, depart)
