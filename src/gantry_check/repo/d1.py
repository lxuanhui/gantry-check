"""Read-only repository over a Cloudflare D1 binding, plus the SQL shared with the sqlite repo.

The Worker imports this module, so it must stay stdlib-only -- in particular it must never
pull in `sqlite3`. The query strings and row mappers therefore live *here*, and
`gantry_check.repo.sqlite` imports them, not the other way round.

D1's JavaScript API, reached through the Pyodide FFI:

    result = await db.prepare(sql).bind(*params).all()
    rows = result.results          # JS array of plain objects
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol

from gantry_check.domain.models import (
    DayType,
    Gantry,
    PublicHoliday,
    RateBand,
    RateSnapshot,
    VehicleType,
)

# --------------------------------------------------------------------------------- SQL

GANTRY_COLUMNS = "number, name, zone_id, lat, lng, line_wkt, heading_deg, source"
SNAPSHOT_COLUMNS = "id, effective_from, fetched_at, html_sha256, pdf_sha256, is_active"
BAND_COLUMNS = "gantry_number, vehicle_type, day_type, start_min, end_min, amount_cents"

SQL_GANTRIES = f"SELECT {GANTRY_COLUMNS} FROM gantry ORDER BY number"
SQL_GANTRY = f"SELECT {GANTRY_COLUMNS} FROM gantry WHERE number = ?"
SQL_ACTIVE_SNAPSHOT = f"SELECT {SNAPSHOT_COLUMNS} FROM rate_snapshot WHERE is_active = 1 LIMIT 1"
SQL_ACTIVE_SNAPSHOT_ID = "SELECT id FROM rate_snapshot WHERE is_active = 1 LIMIT 1"
SQL_BANDS = (
    f"SELECT {BAND_COLUMNS} FROM rate_band "
    "WHERE snapshot_id = ? AND gantry_number = ? AND vehicle_type = ? AND day_type = ? "
    "ORDER BY start_min"
)
SQL_ALL_BANDS = (
    f"SELECT {BAND_COLUMNS} FROM rate_band WHERE snapshot_id = ? "
    "ORDER BY gantry_number, vehicle_type, day_type, start_min"
)
SQL_HOLIDAYS = "SELECT date, name, is_major FROM public_holiday ORDER BY date"
SQL_META = "SELECT value FROM meta WHERE key = ?"


class Row(Protocol):
    """Anything indexable by column name: a dict from D1, a `sqlite3.Row` locally."""

    def __getitem__(self, key: str, /) -> Any: ...


# ------------------------------------------------------------------------- row mappers
# Values are coerced explicitly: D1 (via Pyodide) and sqlite3 do not hand back identical
# Python types for the same column.


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def row_to_gantry(row: Row) -> Gantry:
    return Gantry(
        number=str(row["number"]),
        name=str(row["name"]),
        lat=float(row["lat"]),
        lng=float(row["lng"]),
        zone_id=_opt_str(row["zone_id"]),
        line_wkt=_opt_str(row["line_wkt"]),
        heading_deg=_opt_float(row["heading_deg"]),
        source=str(row["source"]),
    )


def row_to_snapshot(row: Row) -> RateSnapshot:
    return RateSnapshot(
        id=int(row["id"]),
        effective_from=date.fromisoformat(str(row["effective_from"])),
        fetched_at=datetime.fromisoformat(str(row["fetched_at"])),
        html_sha256=str(row["html_sha256"]),
        pdf_sha256=str(row["pdf_sha256"]),
        is_active=bool(int(row["is_active"])),
    )


def row_to_band(row: Row) -> RateBand:
    return RateBand(
        gantry_number=str(row["gantry_number"]),
        vehicle_type=VehicleType(str(row["vehicle_type"])),
        day_type=DayType(str(row["day_type"])),
        start_min=int(row["start_min"]),
        end_min=int(row["end_min"]),
        amount_cents=int(row["amount_cents"]),
    )


def row_to_holiday(row: Row) -> PublicHoliday:
    return PublicHoliday(
        date=date.fromisoformat(str(row["date"])),
        name=str(row["name"]),
        is_major=bool(int(row["is_major"])),
    )


def _rows(result: Any) -> list[Row]:
    """Normalise a D1 result into a list of dict-like rows.

    Under Pyodide `result.results` is a JS array that needs `.to_py()`; a plain Python fake
    (and the test double) already yields dicts.
    """
    results = result.results
    if hasattr(results, "to_py"):
        results = results.to_py()
    return list(results)


# --------------------------------------------------------------------------------- repo


class D1Repo:
    """Read-only `Repo` over the Worker's D1 binding (`env.DB`)."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def _query(self, sql: str, *params: Any) -> list[Row]:
        stmt = self.db.prepare(sql)
        if params:
            stmt = stmt.bind(*params)
        return _rows(await stmt.all())

    async def gantries(self) -> list[Gantry]:
        return [row_to_gantry(r) for r in await self._query(SQL_GANTRIES)]

    async def gantry(self, number: str) -> Gantry | None:
        rows = await self._query(SQL_GANTRY, number)
        return row_to_gantry(rows[0]) if rows else None

    async def active_snapshot(self) -> RateSnapshot | None:
        rows = await self._query(SQL_ACTIVE_SNAPSHOT)
        return row_to_snapshot(rows[0]) if rows else None

    async def _resolve_snapshot_id(self, snapshot_id: int | None) -> int | None:
        # Never bind None through D1: Pyodide turns it into `undefined`, which D1 rejects.
        if snapshot_id is not None:
            return snapshot_id
        rows = await self._query(SQL_ACTIVE_SNAPSHOT_ID)
        return int(rows[0]["id"]) if rows else None

    async def bands(
        self,
        gantry_number: str,
        vehicle_type: VehicleType,
        day_type: DayType,
        snapshot_id: int | None = None,
    ) -> list[RateBand]:
        sid = await self._resolve_snapshot_id(snapshot_id)
        if sid is None:
            return []
        rows = await self._query(SQL_BANDS, sid, gantry_number, vehicle_type.value, day_type.value)
        return [row_to_band(r) for r in rows]

    async def all_bands(self, snapshot_id: int | None = None) -> list[RateBand]:
        sid = await self._resolve_snapshot_id(snapshot_id)
        if sid is None:
            return []
        return [row_to_band(r) for r in await self._query(SQL_ALL_BANDS, sid)]

    async def holidays(self) -> list[PublicHoliday]:
        return [row_to_holiday(r) for r in await self._query(SQL_HOLIDAYS)]

    async def get_meta(self, key: str) -> str | None:
        rows = await self._query(SQL_META, key)
        return str(rows[0]["value"]) if rows else None
