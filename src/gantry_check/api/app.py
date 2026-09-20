"""FastAPI application. Runs unchanged on CPython (uvicorn / TestClient) and inside the
Cloudflare Python Worker (see src/entry.py). Storage is injected per request via `repo_factory`
so the Worker can hand over its D1 binding and tests can hand over an in-memory SQLite."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from gantry_check import __version__
from gantry_check.domain.daytype import day_type_for, minute_of_day, to_sgt
from gantry_check.domain.models import CHARGEABLE_DAY_TYPES, DayType, RateBand, VehicleType
from gantry_check.domain.pricing import charge_cents, format_sgd
from gantry_check.repo.base import Repo

RepoFactory = Callable[[Request], Repo]


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
    origin: tuple[float, float]
    destination: tuple[float, float]
    depart_at: datetime | None = None
    vehicle: VehicleType = VehicleType.CAR


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _band_out(b: RateBand) -> BandOut:
    return BandOut(
        start=_hhmm(b.start_min),
        end=_hhmm(b.end_min),
        amount_cents=b.amount_cents,
        amount=format_sgd(b.amount_cents),
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
<li><code>POST /estimate</code> — route cost estimate (phase 2)</li>
</ul>
<p><a href="https://github.com/lxuanhui/gantry-check">Source on GitHub</a></p>
</body></html>
"""


def create_app(repo_factory: RepoFactory) -> FastAPI:
    app = FastAPI(
        title="gantry-check",
        version=__version__,
        description="Singapore ERP gantry rates and route cost estimates.",
    )

    def repo_for(request: Request) -> Repo:
        return repo_factory(request)

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

    @app.post("/estimate", status_code=501)
    async def estimate(body: EstimateIn) -> dict[str, Any]:
        _ = minute_of_day  # keep import used until Phase 2 wires route matching
        return {
            "error": "not implemented",
            "detail": "Route matching (Phase 2) is not built yet; /rates/{gantry} is available.",
        }

    return app
