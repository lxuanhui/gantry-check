"""`gantry-check` command line: refresh the local snapshot, price a gantry, probe a route.

Deliberately thin -- every command is a few lines of argument shuffling over
`gantry_check.ingest.refresh` and the pure domain modules.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx

from gantry_check.config import Settings, load_dotenv
from gantry_check.domain.daytype import day_type_for, to_sgt
from gantry_check.domain.models import SGT, DayType, LatLng, VehicleType
from gantry_check.domain.pricing import charge_cents, format_sgd
from gantry_check.ingest.refresh import RefreshError, run_refresh
from gantry_check.repo.sqlite import SqliteRepo
from gantry_check.routing.base import RoutingError, build_engine

DEFAULT_SNAPSHOT_SQL = "data/snapshots/snapshot.sql"
HTTP_TIMEOUT_S = 30.0


# --------------------------------------------------------------------------- helpers


def _settings() -> Settings:
    """`.env` first, then the real environment (which wins)."""
    return Settings.from_env({**load_dotenv(), **os.environ})


def _parse_when(value: str | None) -> datetime:
    """Parse an ISO-8601 instant; naive values are SGT, and no value means 'now'."""
    if value is None:
        return datetime.now(tz=SGT)
    try:
        return to_sgt(datetime.fromisoformat(value))
    except ValueError as exc:
        raise SystemExit(f"error: not an ISO-8601 date/time: {value!r} ({exc})") from exc


def _parse_latlng(value: str) -> LatLng:
    parts = value.split(",")
    if len(parts) != 2:
        raise SystemExit(f"error: expected LAT,LNG, got {value!r}")
    try:
        return LatLng(lat=float(parts[0].strip()), lng=float(parts[1].strip()))
    except ValueError as exc:
        raise SystemExit(f"error: expected LAT,LNG, got {value!r}") from exc


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _open_repo(db: str) -> SqliteRepo:
    if db != ":memory:" and not Path(db).exists():
        raise SystemExit(f"error: no database at {db} -- run `gantry-check refresh` first")
    return SqliteRepo(db)


# --------------------------------------------------------------------------- refresh


def _cmd_refresh(args: argparse.Namespace) -> int:
    async def go() -> int:
        with SqliteRepo(args.db) as repo:
            result = await run_refresh(
                repo,
                out_path=Path(args.out) if args.out else None,
                allow_mismatch=args.allow_mismatch,
                cache_dir=Path(args.cache_dir) if args.cache_dir else None,
                years=args.years,
            )
        print()
        print(f"snapshot        {result.snapshot_id}")
        print(f"effective from  {result.effective_from.isoformat()}")
        print(f"gantries        {result.gantry_count}")
        print(f"rate bands      {result.band_count}")
        print(f"holidays        {result.holiday_count}")
        print(f"mismatches      {len(result.mismatches)}")
        print(f"sql             {result.sql_path or '(not written)'}")
        return 0

    try:
        return asyncio.run(go())
    except RefreshError as exc:
        print(f"refresh failed: {exc}", file=sys.stderr)
        return 1


# --------------------------------------------------------------------------- rates


def _cmd_rates(args: argparse.Namespace) -> int:
    at = _parse_when(args.at)
    vehicle = VehicleType(args.vehicle)

    async def go(repo: SqliteRepo) -> int:
        gantry = await repo.gantry(args.gantry)
        if gantry is None:
            print(f"error: no gantry numbered {args.gantry}", file=sys.stderr)
            return 1
        if await repo.active_snapshot() is None:
            print("error: no active rate snapshot -- run `gantry-check refresh`", file=sys.stderr)
            return 1

        day_type = day_type_for(at, await repo.holidays())
        bands = await repo.bands(args.gantry, vehicle, day_type)

        print(f"gantry    {gantry.number}  {gantry.name}")
        print(f"at        {at.isoformat()}")
        print(f"vehicle   {vehicle.value}")
        print(f"day type  {day_type.value}")

        if args.table:
            if not bands:
                print("bands     (not in operation)")
                return 0
            print("bands")
            for row in bands:
                window = f"{_hhmm(row.start_min)}-{_hhmm(row.end_min)}"
                print(f"  {window}  {format_sgd(row.amount_cents)}")
            return 0

        cents, band = charge_cents(bands, at, day_type)
        if band is None:
            reason = "ERP is not charged on Sundays and public holidays"
            if day_type is not DayType.SUNDAY_PH:
                reason = "outside every charging window"
            print(f"charge    {format_sgd(cents)}  ({reason})")
        else:
            window = f"{_hhmm(band.start_min)}-{_hhmm(band.end_min)}"
            print(f"band      {window}")
            print(f"charge    {format_sgd(cents)}")
        return 0

    try:
        with _open_repo(args.db) as repo:
            return asyncio.run(go(repo))
    except sqlite3.OperationalError as exc:
        print(f"error: unusable database {args.db}: {exc}", file=sys.stderr)
        return 1


# --------------------------------------------------------------------------- route


def _cmd_route(args: argparse.Namespace) -> int:
    settings = _settings()
    if args.engine:
        settings = replace(settings, routing_engine=args.engine)
    origin = _parse_latlng(args.origin)
    destination = _parse_latlng(args.destination)
    depart_at = _parse_when(args.depart_at) if args.depart_at else None

    async def go() -> int:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            engine = build_engine(settings, client)
            route = await engine.route(origin, destination, depart_at)
        print(f"engine     {route.engine}")
        print(f"summary    {route.summary or '(none)'}")
        print(f"distance   {route.distance_m / 1000:.2f} km")
        print(f"duration   {route.duration_s / 60:.1f} min")
        print(f"points     {len(route.points)}")
        if route.cumulative_seconds:
            first = route.cumulative_seconds[0]
            last = route.cumulative_seconds[-1]
            print(f"cumulative {first:.1f}s .. {last:.1f}s")
        for warning in route.warnings:
            print(f"warning    {warning}")
        return 0

    try:
        return asyncio.run(go())
    except RoutingError as exc:
        print(f"routing failed: {exc}", file=sys.stderr)
        return 1


# --------------------------------------------------------------------------- argparse


def _build_parser(settings: Settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gantry-check",
        description="Singapore ERP gantry rate tools.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser("refresh", help="fetch every upstream source into a new snapshot")
    refresh.add_argument("--db", default=settings.local_db, help="SQLite file to write")
    refresh.add_argument("--out", default=DEFAULT_SNAPSHOT_SQL, help="D1-loadable SQL to write")
    refresh.add_argument(
        "--allow-mismatch",
        action="store_true",
        help="store the snapshot even when the HTML tables and the PDF disagree",
    )
    refresh.add_argument("--cache-dir", default=None, help="cache downloads under this directory")
    refresh.add_argument(
        "--year",
        dest="years",
        action="append",
        type=int,
        default=None,
        help="public holiday year to fetch (repeatable; default: this year and next)",
    )
    refresh.set_defaults(func=_cmd_refresh)

    rates = sub.add_parser("rates", help="show the charge at one gantry at a given time")
    rates.add_argument("gantry", help="LTA gantry number, e.g. 35")
    rates.add_argument("--at", default=None, help="ISO-8601 instant (naive = SGT; default: now)")
    rates.add_argument(
        "--vehicle",
        default=VehicleType.CAR.value,
        choices=[v.value for v in VehicleType],
    )
    rates.add_argument("--db", default=settings.local_db, help="SQLite file to read")
    rates.add_argument(
        "--table", action="store_true", help="print the whole band table instead of one charge"
    )
    rates.set_defaults(func=_cmd_rates)

    route = sub.add_parser("route", help="probe the configured routing engine")
    route.add_argument("--from", dest="origin", required=True, metavar="LAT,LNG")
    route.add_argument("--to", dest="destination", required=True, metavar="LAT,LNG")
    route.add_argument("--engine", default=None, choices=["google", "onemap"])
    route.add_argument("--depart-at", default=None, help="ISO-8601 instant (naive = SGT)")
    route.set_defaults(func=_cmd_route)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser(_settings())
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
