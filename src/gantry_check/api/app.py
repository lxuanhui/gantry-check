"""FastAPI application. Runs unchanged on CPython (uvicorn / TestClient) and inside the
Cloudflare Python Worker (see src/entry.py). Storage is injected per request via `repo_factory`
so the Worker can hand over its D1 binding and tests can hand over an in-memory SQLite."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from gantry_check import __version__
from gantry_check.api.guards import Guard
from gantry_check.domain.daytype import day_type_for, to_sgt
from gantry_check.domain.geo import encode_polyline
from gantry_check.domain.models import (
    CHARGEABLE_DAY_TYPES,
    DayType,
    LatLng,
    RateBand,
    VehicleType,
)
from gantry_check.domain.pricing import charge_cents, format_sgd
from gantry_check.matching.estimate import Estimate, estimate_trip
from gantry_check.repo.base import Repo
from gantry_check.routing.base import RoutingEngine, RoutingError

RepoFactory = Callable[[Request], Repo]
EngineFactory = Callable[[Request], RoutingEngine]

#: Rough box around Singapore's road network (Tuas to Changi Bay, Sentosa to Woodlands
#: Checkpoint). Coarse on purpose: it stops the API being used as a free world-routing proxy,
#: it does not try to follow the border (Johor Bahru city centre is ~2 km north of the
#: checkpoint and falls inside it).
SINGAPORE_BOUNDS = ((1.20, 103.60), (1.47, 104.05))  # (min_lat, min_lng), (max_lat, max_lng)


def in_singapore(lat: float, lng: float) -> bool:
    """True if (lat, lng) falls inside :data:`SINGAPORE_BOUNDS`."""
    (min_lat, min_lng), (max_lat, max_lng) = SINGAPORE_BOUNDS
    return min_lat <= lat <= max_lat and min_lng <= lng <= max_lng


class GantryOut(BaseModel):
    number: str
    name: str
    zone_id: str | None
    lat: float
    lng: float
    has_line: bool


class BandOut(BaseModel):
    start: str = Field(description="HH:MM, inclusive")
    end: str = Field(description="HH:MM, exclusive")
    amount_cents: int
    amount: str


class RateOut(BaseModel):
    gantry: str
    name: str
    vehicle: VehicleType
    at: datetime = Field(description="The queried instant, in Singapore time")
    day_type: DayType
    band: BandOut | None
    amount_cents: int
    amount: str
    effective_from: str | None


class TableOut(BaseModel):
    gantry: str
    vehicle: VehicleType
    day_type: DayType
    bands: list[BandOut]


class EstimateIn(BaseModel):
    origin: tuple[float, float] = Field(description="[lat, lng]")
    destination: tuple[float, float] = Field(description="[lat, lng]")
    depart_at: datetime | None = Field(
        default=None, description="ISO-8601; naive values are Singapore time. Default: now."
    )
    vehicle: VehicleType = VehicleType.CAR
    engine: Literal["google", "onemap"] | None = Field(
        default=None,
        description="Accepted but ignored: the routing engine is chosen by server config.",
    )

    @field_validator("origin", "destination")
    @classmethod
    def _within_singapore(cls, point: tuple[float, float]) -> tuple[float, float]:
        lat, lng = point
        if not in_singapore(lat, lng):
            raise ValueError(f"outside Singapore: ({lat}, {lng})")
        return point


class ChargeOut(BaseModel):
    gantry: str
    name: str
    zone_id: str | None
    crossed_at: datetime = Field(description="Estimated crossing time, Singapore time")
    day_type: DayType
    band: BandOut | None
    amount_cents: int
    amount: str
    method: str = Field(description='"line" (carriageway geometry) or "point" (proximity)')


class EstimateOut(BaseModel):
    engine: str
    summary: str
    distance_m: float
    duration_s: float
    polyline: str = Field(description="Route geometry as a Google encoded polyline (precision 5)")
    depart_at: datetime = Field(description="Singapore time")
    vehicle: VehicleType
    charges: list[ChargeOut]
    total_cents: int
    total: str
    warnings: list[str]


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _band_out(b: RateBand) -> BandOut:
    return BandOut(
        start=_hhmm(b.start_min),
        end=_hhmm(b.end_min),
        amount_cents=b.amount_cents,
        amount=format_sgd(b.amount_cents),
    )


def _estimate_out(result: Estimate) -> EstimateOut:
    return EstimateOut(
        engine=result.route.engine,
        summary=result.route.summary,
        distance_m=result.route.distance_m,
        duration_s=result.route.duration_s,
        polyline=encode_polyline(result.route.points, precision=5),
        depart_at=result.depart_at,
        vehicle=result.vehicle,
        charges=[
            ChargeOut(
                gantry=c.gantry.number,
                name=c.gantry.name,
                zone_id=c.gantry.zone_id,
                crossed_at=c.crossed_at,
                day_type=c.day_type,
                band=None if c.band is None else _band_out(c.band),
                amount_cents=c.amount_cents,
                amount=format_sgd(c.amount_cents),
                method=c.method,
            )
            for c in result.charges
        ],
        total_cents=result.total_cents,
        total=format_sgd(result.total_cents),
        warnings=result.warnings,
    )


_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>gantry-check</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;
padding:0 1rem;color:#222}
code{background:#f3f3f3;padding:.1em .3em;border-radius:3px}li{margin:.4em 0}</style></head>
<body><h1>gantry-check</h1>
<p>Singapore ERP gantry rates and route cost estimates. Times are Singapore time unless an
offset is given.</p>
<ul>
<li><a href="/docs">Interactive API docs</a></li>
<li><a href="/health">/health</a> — active rate snapshot</li>
<li><a href="/gantries">/gantries</a> — all gantries with coordinates and zones</li>
<li><a href="/rates/35?at=2026-09-21T08:10:00&amp;vehicle=car">
/rates/35?at=2026-09-21T08:10:00&amp;vehicle=car</a>
 — charge at gantry 35 (CTE before Braddell Road) on a weekday morning</li>
<li><a href="/rates/35/table?vehicle=car&amp;day_type=weekday">
/rates/35/table?vehicle=car&amp;day_type=weekday</a>
 — the whole weekday table</li>
<li><code>POST /estimate</code> — route cost estimate: which gantries a drive crosses,
 when, and what each one charges.
 Body: <code>{"origin": [1.3691, 103.8454], "destination": [1.2840, 103.8515],
 "depart_at": "2026-09-22T08:00:00", "vehicle": "car"}</code></li>
</ul>
<p><a href="https://github.com/lxuanhui/gantry-check">Source on GitHub</a></p>
</body></html>
"""


def create_app(
    repo_factory: RepoFactory,
    engine_factory: EngineFactory | None = None,
    *,
    estimate_guards: Sequence[Guard] = (),
) -> FastAPI:
    app = FastAPI(
        title="gantry-check",
        version=__version__,
        description="Singapore ERP gantry rates and route cost estimates.",
    )

    def repo_for(request: Request) -> Repo:
        return repo_factory(request)

    def engine_for(request: Request) -> RoutingEngine:
        if engine_factory is None:
            raise HTTPException(503, "no routing engine configured")
        try:
            return engine_factory(request)
        except RoutingError as exc:
            raise HTTPException(503, "no routing engine configured") from exc

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> str:
        return _INDEX_HTML

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        repo = repo_for(request)
        snap = await repo.active_snapshot()
        return {
            "ok": True,
            "version": __version__,
            "active_snapshot": None
            if snap is None
            else {
                "id": snap.id,
                "effective_from": snap.effective_from.isoformat(),
                "fetched_at": snap.fetched_at.isoformat(),
            },
        }

    @app.get("/gantries", response_model=list[GantryOut])
    async def gantries(request: Request) -> list[GantryOut]:
        return [
            GantryOut(
                number=g.number,
                name=g.name,
                zone_id=g.zone_id,
                lat=g.lat,
                lng=g.lng,
                has_line=g.line_wkt is not None,
            )
            for g in await repo_for(request).gantries()
        ]

    @app.get("/rates/{gantry}", response_model=RateOut)
    async def rate_at(
        request: Request,
        gantry: str,
        at: Annotated[
            datetime, Query(description="ISO-8601; naive values are taken as Singapore time")
        ],
        vehicle: VehicleType = VehicleType.CAR,
    ) -> RateOut:
        repo = repo_for(request)
        g = await repo.gantry(gantry)
        if g is None:
            raise HTTPException(404, f"unknown gantry {gantry!r}")
        snap = await repo.active_snapshot()
        local = to_sgt(at)
        day_type = day_type_for(local, await repo.holidays())
        bands = (
            await repo.bands(gantry, vehicle, day_type) if day_type in CHARGEABLE_DAY_TYPES else []
        )
        cents, band = charge_cents(bands, local, day_type)
        return RateOut(
            gantry=g.number,
            name=g.name,
            vehicle=vehicle,
            at=local,
            day_type=day_type,
            band=None if band is None else _band_out(band),
            amount_cents=cents,
            amount=format_sgd(cents),
            effective_from=None if snap is None else snap.effective_from.isoformat(),
        )

    @app.get("/rates/{gantry}/table", response_model=TableOut)
    async def rate_table(
        request: Request,
        gantry: str,
        vehicle: VehicleType = VehicleType.CAR,
        day_type: DayType = DayType.WEEKDAY,
    ) -> TableOut:
        repo = repo_for(request)
        if await repo.gantry(gantry) is None:
            raise HTTPException(404, f"unknown gantry {gantry!r}")
        bands = (
            await repo.bands(gantry, vehicle, day_type) if day_type in CHARGEABLE_DAY_TYPES else []
        )
        return TableOut(
            gantry=gantry, vehicle=vehicle, day_type=day_type, bands=[_band_out(b) for b in bands]
        )

    @app.post(
        "/estimate",
        response_model=EstimateOut,
        dependencies=[Depends(g) for g in estimate_guards],
    )
    async def estimate(request: Request, body: EstimateIn) -> EstimateOut:
        engine = engine_for(request)
        try:
            result = await estimate_trip(
                engine,
                repo_for(request),
                LatLng(lat=body.origin[0], lng=body.origin[1]),
                LatLng(lat=body.destination[0], lng=body.destination[1]),
                body.depart_at,
                body.vehicle,
            )
        except RoutingError as exc:
            raise HTTPException(502, str(exc)) from exc
        return _estimate_out(result)

    return app
