"""Storage interface. Two implementations: stdlib sqlite3 (local/tests) and Cloudflare D1 (Worker).

All methods are async so the Worker (where D1 is async-only) and local code share one API.
Only the sqlite implementation supports writes; the Worker is read-only.
"""

from __future__ import annotations

from typing import Protocol

from gantry_check.domain.models import (
    DayType,
    Gantry,
    PublicHoliday,
    RateBand,
    RateSnapshot,
    VehicleType,
)


class Repo(Protocol):
    async def gantries(self) -> list[Gantry]: ...

    async def gantry(self, number: str) -> Gantry | None: ...

    async def active_snapshot(self) -> RateSnapshot | None: ...

    async def bands(
        self,
        gantry_number: str,
        vehicle_type: VehicleType,
        day_type: DayType,
        snapshot_id: int | None = None,
    ) -> list[RateBand]:
        """Bands for one gantry/vehicle/day type, ordered by start_min. Default: active snapshot."""
        ...

    async def all_bands(self, snapshot_id: int | None = None) -> list[RateBand]: ...

    async def holidays(self) -> list[PublicHoliday]: ...

    async def get_meta(self, key: str) -> str | None: ...


class WritableRepo(Repo, Protocol):
    async def apply_migrations(self) -> None: ...

    async def upsert_gantries(self, gantries: list[Gantry]) -> None: ...

    async def write_snapshot(
        self, snapshot: RateSnapshot, bands: list[RateBand], activate: bool = True
    ) -> int:
        """Insert a snapshot + its bands; optionally deactivate all others. Returns snapshot id."""
        ...

    async def replace_holidays(self, holidays: list[PublicHoliday]) -> None: ...

    async def set_meta(self, key: str, value: str) -> None: ...
