"""Core domain types. Pure Python, no I/O, importable inside the Cloudflare Worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum

# Singapore has no DST; a fixed offset avoids depending on tzdata inside Pyodide.
SGT = timezone(timedelta(hours=8), name="Asia/Singapore")


class VehicleType(StrEnum):
    """Vehicle classes as used by LTA's ERP tables. Value = DB / API string."""

    CAR = "car"  # passenger cars, light goods vehicles, taxis (base rate)
    MOTORCYCLE = "motorcycle"
    HGV = "hgv"  # heavy goods vehicles, small buses
    VHGV = "vhgv"  # very heavy goods vehicles, big buses

    @property
    def pcu_factor(self) -> float:
        return _PCU[self]

    @property
    def lta_table_index(self) -> int:
        """The `v` index in LTA's `{gantry}-table-{v}-{d}.html` URLs."""
        return _LTA_VEHICLE_INDEX[self]


_PCU = {
    VehicleType.MOTORCYCLE: 0.5,
    VehicleType.CAR: 1.0,
    VehicleType.HGV: 1.5,
    VehicleType.VHGV: 2.0,
}
_LTA_VEHICLE_INDEX = {
    VehicleType.CAR: 0,
    VehicleType.MOTORCYCLE: 1,
    VehicleType.HGV: 2,
    VehicleType.VHGV: 3,
}


class DayType(StrEnum):
    """Which rate table applies on a given date."""

    WEEKDAY = "weekday"
    SATURDAY = "saturday"
    SUNDAY_PH = "sunday_ph"  # no ERP charged; no table exists
    EVE_MAJOR_PH_WEEKDAY = "eve_major_ph_weekday"
    EVE_MAJOR_PH_SATURDAY = "eve_major_ph_saturday"

    @property
    def lta_table_index(self) -> int | None:
        """The `d` index in LTA's `{gantry}-table-{v}-{d}.html` URLs (None = free day)."""
        return _LTA_DAY_INDEX[self]


_LTA_DAY_INDEX: dict[DayType, int | None] = {
    DayType.WEEKDAY: 0,
    DayType.SATURDAY: 1,
    DayType.EVE_MAJOR_PH_WEEKDAY: 2,
    DayType.EVE_MAJOR_PH_SATURDAY: 3,
    DayType.SUNDAY_PH: None,
}

CHARGEABLE_DAY_TYPES = tuple(d for d in DayType if d.lta_table_index is not None)


@dataclass(frozen=True, slots=True)
class Gantry:
    number: str  # LTA gantry number, e.g. "35"
    name: str  # e.g. "CTE before Braddell Road"
    lat: float
    lng: float
    zone_id: str | None = None  # Annex D zone, e.g. "CT4"
    line_wkt: str | None = None  # LINESTRING(lng lat, lng lat) across the carriageway
    heading_deg: float | None = None  # traffic heading; None = no direction check
    source: str = "onemotoring-kml"


@dataclass(frozen=True, slots=True)
class RateBand:
    """A half-open charging window [start_min, end_min) on one gantry for one vehicle/day type."""

    gantry_number: str
    vehicle_type: VehicleType
    day_type: DayType
    start_min: int  # minutes since midnight, inclusive
    end_min: int  # minutes since midnight, exclusive
    amount_cents: int

    def contains(self, minute_of_day: int) -> bool:
        return self.start_min <= minute_of_day < self.end_min


@dataclass(frozen=True, slots=True)
class RateSnapshot:
    effective_from: date
    fetched_at: datetime
    html_sha256: str
    pdf_sha256: str
    id: int | None = None
    is_active: bool = False


@dataclass(frozen=True, slots=True)
class PublicHoliday:
    date: date
    name: str
    is_major: bool = False


@dataclass(frozen=True, slots=True)
class LatLng:
    lat: float
    lng: float


@dataclass(frozen=True, slots=True)
class Route:
    """A driving route as a polyline with cumulative travel time at each vertex."""

    points: list[LatLng]
    cumulative_seconds: list[float]  # same length as points; [0] == 0.0
    distance_m: float
    duration_s: float
    engine: str  # "google" | "onemap"
    summary: str = ""  # human-readable, e.g. "via CTE"
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.points) != len(self.cumulative_seconds):
            raise ValueError("points and cumulative_seconds must have the same length")
