"""Parse LTA's "Base ERP Rate Table" PDF and cross-check it against the HTML rate tables.

Layout of the 07 Sep 2026 edition (5 pages, one table per page except the last):

* pages 1-2  "Orchard Cordon and Rest of CBD", 15 columns: col 0 is Time, cols 1-7 are Weekdays
  and cols 8-14 repeat the same seven column groups for Saturdays.
* pages 3-4  "Arterial Roads" / "Expressways", 29 columns, all Weekdays.
* page 5     a PCU multiplier table plus three "Refer #N:" lookup tables that expand the
  "Refer to N" placeholders used in the CBD gantry-number row.

In every rate table row 0 is the title (carrying the effective date), row 2 holds the day-type
banner, row 3 the column titles, row 4 the gantry numbers and rows 5+ the charging windows.
A blank cell means "no charge in this window" - the PDF never prints $0.00.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pdfplumber

from gantry_check.domain.models import DayType, RateBand, VehicleType

Row = list[str | None]
Table = list[Row]

_WHITESPACE_RE = re.compile(r"\s+")
_EFFECTIVE_FROM_RE = re.compile(r"With Effect From\s+(\d{1,2}\s+\w{3,}\s+\d{4})", re.IGNORECASE)
_TIME_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
_AMOUNT_RE = re.compile(r"^\$(\d+)\.(\d{2})$")
_REFER_HEADING_RE = re.compile(r"^Refer\s*#\s*(\d+)\s*:?$", re.IGNORECASE)
_REFER_TO_RE = re.compile(r"^Refer\s+to\s+(\d+)$", re.IGNORECASE)
_GANTRY_NUMBER_RE = re.compile(r"^\d+$")

_DAY_LABELS = {
    "weekdays": DayType.WEEKDAY,
    "weekday": DayType.WEEKDAY,
    "saturdays": DayType.SATURDAY,
    "saturday": DayType.SATURDAY,
}

#: Row indices that are fixed across all four rate-table pages.
_DAY_BANNER_ROW = 2
_GANTRY_ROW = 4
_FIRST_DATA_ROW = 5


@dataclass(frozen=True, slots=True)
class PdfRates:
    """Everything the base-rate PDF says, restricted to what it actually covers.

    The PDF publishes *base* rates only, i.e. `VehicleType.CAR`; other classes are the base rate
    times the PCU factor on page 5. Day types are limited to WEEKDAY and SATURDAY.
    """

    effective_from: date
    bands: list[RateBand]
    gantry_numbers: set[str]


def _norm(cell: str | None) -> str:
    return _WHITESPACE_RE.sub(" ", cell).strip() if cell else ""


def _to_minutes(hours: str, minutes: str) -> int:
    hour, minute = int(hours), int(minutes)
    if hour > 24 or minute > 59 or (hour == 24 and minute != 0):
        raise ValueError(f"impossible clock time {hours}:{minutes}")
    return hour * 60 + minute


def _to_cents(amount: str) -> int:
    match = _AMOUNT_RE.match(amount)
    if match is None:
        raise ValueError(f"unparseable amount {amount!r} in the rates PDF")
    return int(Decimal(match.group(0).lstrip("$")) * 100)


def _parse_effective_from(tables: list[Table]) -> date:
    for table in tables:
        if not table:
            continue
        match = _EFFECTIVE_FROM_RE.search(_norm(table[0][0]))
        if match is not None:
            raw = _WHITESPACE_RE.sub(" ", match.group(1))
            for fmt in ("%d %b %Y", "%d %B %Y"):
                try:
                    return datetime.strptime(raw, fmt).date()
                except ValueError:
                    continue
            raise ValueError(f"unparseable effective date {raw!r} in the rates PDF")
    raise ValueError("no 'With Effect From <date>' title row found in the rates PDF")


def _parse_refer_tables(tables: list[Table]) -> dict[str, list[str]]:
    """Read the "Refer #N:" lookup tables on the last page into {N: [gantry numbers]}."""
    lookups: dict[str, list[str]] = {}
    for table in tables:
        if not table or not table[0]:
            continue
        heading = _REFER_HEADING_RE.match(_norm(table[0][0]))
        if heading is None:
            continue
        numbers = [
            _norm(row[0]) for row in table[1:] if row and _GANTRY_NUMBER_RE.match(_norm(row[0]))
        ]
        if not numbers:
            raise ValueError(f"'Refer #{heading.group(1)}' lookup table has no gantry numbers")
        lookups[heading.group(1)] = numbers
    return lookups


def _is_rate_table(table: Table) -> bool:
    return (
        len(table) > _FIRST_DATA_ROW
        and len(table[0]) > 1
        and _norm(table[_GANTRY_ROW][0]).lower() == "gantry no."
    )


def _column_day_types(table: Table) -> dict[int, DayType]:
    """Walk the day-type banner row, carrying the last label forward across merged cells."""
    banner = table[_DAY_BANNER_ROW]
    day_types: dict[int, DayType] = {}
    current: DayType | None = None
    for index in range(1, len(banner)):
        label = _norm(banner[index]).lower()
        if label:
            if label not in _DAY_LABELS:
                raise ValueError(f"unknown day-type banner {banner[index]!r} in the rates PDF")
            current = _DAY_LABELS[label]
        if current is None:
            raise ValueError("rate table column precedes any day-type banner label")
        day_types[index] = current
    return day_types


def _column_gantries(table: Table, lookups: dict[str, list[str]]) -> dict[int, list[str]]:
    """Expand the 'Gantry No.' row into the gantry numbers each column stands for."""
    row = table[_GANTRY_ROW]
    columns: dict[int, list[str]] = {}
    for index in range(1, len(row)):
        cell = _norm(row[index])
        if not cell:
            continue
        numbers: list[str] = []
        for token in (part.strip() for part in cell.split(",")):
            if not token:
                continue
            refer = _REFER_TO_RE.match(token)
            if refer is not None:
                key = refer.group(1)
                if key not in lookups:
                    raise ValueError(f"'Refer to {key}' has no matching 'Refer #{key}:' table")
                numbers.extend(lookups[key])
            elif _GANTRY_NUMBER_RE.match(token):
                numbers.append(token)
            else:
                raise ValueError(f"unparseable gantry-number cell {cell!r} in the rates PDF")
        columns[index] = numbers
    return columns


def _merge(bands: list[RateBand]) -> list[RateBand]:
    """Merge touching bands with equal amounts. Input must already be sorted by start."""
    merged: list[RateBand] = []
    for band in bands:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous.end_min == band.start_min
            and previous.amount_cents == band.amount_cents
        ):
            merged[-1] = RateBand(
                gantry_number=previous.gantry_number,
                vehicle_type=previous.vehicle_type,
                day_type=previous.day_type,
                start_min=previous.start_min,
                end_min=band.end_min,
                amount_cents=previous.amount_cents,
            )
        else:
            merged.append(band)
    return merged


def _sort_key(band: RateBand) -> tuple[int, str, str, int]:
    return (int(band.gantry_number), band.day_type.value, band.vehicle_type.value, band.start_min)


def parse_rates_pdf(pdf_bytes: bytes) -> PdfRates:
    """Parse the base ERP rate PDF into merged `RateBand`s for `VehicleType.CAR`."""
    tables: list[Table] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            tables.extend(page.extract_tables())
    if not tables:
        raise ValueError("no tables found in the rates PDF")

    effective_from = _parse_effective_from(tables)
    lookups = _parse_refer_tables(tables)
    rate_tables = [table for table in tables if _is_rate_table(table)]
    if not rate_tables:
        raise ValueError("no 'Gantry No.' rate tables found in the rates PDF")

    gantry_numbers: set[str] = set()
    # Cells are accumulated across every page first: a charging window can span a page break
    # (e.g. Orchard runs 15:35-15:55 at the foot of page 1 into 15:55-16:00 at the top of page 2).
    raw: dict[tuple[str, DayType], list[RateBand]] = {}
    for table in rate_tables:
        day_types = _column_day_types(table)
        columns = _column_gantries(table, lookups)
        for numbers in columns.values():
            gantry_numbers.update(numbers)
        for row_index in range(_FIRST_DATA_ROW, len(table)):
            row = table[row_index]
            time_cell = _norm(row[0])
            time_match = _TIME_RANGE_RE.match(time_cell)
            if time_match is None:
                raise ValueError(f"unparseable time range {time_cell!r} in the rates PDF")
            start = _to_minutes(time_match.group(1), time_match.group(2))
            end = _to_minutes(time_match.group(3), time_match.group(4))
            if end <= start:
                raise ValueError(f"time range {time_cell!r} does not end after it starts")
            for index, numbers in columns.items():
                cell = _norm(row[index]) if index < len(row) else ""
                if not cell:
                    continue  # blank == no charge in this window
                cents = _to_cents(cell)
                day_type = day_types[index]
                for number in numbers:
                    raw.setdefault((number, day_type), []).append(
                        RateBand(
                            gantry_number=number,
                            vehicle_type=VehicleType.CAR,
                            day_type=day_type,
                            start_min=start,
                            end_min=end,
                            amount_cents=cents,
                        )
                    )

    bands: list[RateBand] = []
    for group in raw.values():
        group.sort(key=lambda band: band.start_min)
        bands.extend(_merge(group))
    bands.sort(key=_sort_key)
    return PdfRates(effective_from=effective_from, bands=bands, gantry_numbers=gantry_numbers)


def normalize(bands: list[RateBand]) -> list[RateBand]:
    """Drop zero-amount bands and merge touching bands of equal amount.

    This is the common shape in which the HTML tables and the PDF can be compared: the HTML lists
    a gantry's whole operating window including `$0.00` stretches, the PDF simply leaves those
    cells blank.
    """
    groups: dict[tuple[str, VehicleType, DayType], list[RateBand]] = {}
    for band in bands:
        if band.amount_cents == 0:
            continue
        key = (band.gantry_number, band.vehicle_type, band.day_type)
        groups.setdefault(key, []).append(band)

    result: list[RateBand] = []
    for group in groups.values():
        group.sort(key=lambda band: band.start_min)
        result.extend(_merge(group))
    result.sort(key=_sort_key)
    return result


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _describe(band: RateBand) -> str:
    return f"{_hhmm(band.start_min)}-{_hhmm(band.end_min)} ${band.amount_cents / 100:.2f}"


#: Only these combinations exist on both sides: the PDF publishes base (car) rates, and only
#: for weekdays and Saturdays.
COMPARABLE = ((VehicleType.CAR, DayType.WEEKDAY), (VehicleType.CAR, DayType.SATURDAY))


def compare(html: list[RateBand], pdf: list[RateBand]) -> list[str]:
    """Describe every disagreement between HTML-derived and PDF-derived bands.

    Both sides are normalized first. A (gantry, day type) absent from one side is treated as "no
    bands" rather than "unknown" - the PDF, for instance, lists no Saturday charges at all for
    arterial and expressway gantries.
    """

    def index(bands: list[RateBand]) -> dict[tuple[str, VehicleType, DayType], list[RateBand]]:
        grouped: dict[tuple[str, VehicleType, DayType], list[RateBand]] = {}
        for band in normalize(bands):
            key = (band.gantry_number, band.vehicle_type, band.day_type)
            if (band.vehicle_type, band.day_type) in COMPARABLE:
                grouped.setdefault(key, []).append(band)
        return grouped

    html_index, pdf_index = index(html), index(pdf)
    problems: list[str] = []
    for key in sorted(set(html_index) | set(pdf_index), key=lambda k: (int(k[0]), k[2].value)):
        number, vehicle, day_type = key
        html_bands = {(b.start_min, b.end_min, b.amount_cents) for b in html_index.get(key, [])}
        pdf_bands = {(b.start_min, b.end_min, b.amount_cents) for b in pdf_index.get(key, [])}
        where = f"gantry {number} {vehicle.value}/{day_type.value}"
        for band in sorted(
            (b for b in html_index.get(key, []) if _key(b) not in pdf_bands), key=_key
        ):
            problems.append(f"{where}: HTML band {_describe(band)} is not in the PDF")
        for band in sorted(
            (b for b in pdf_index.get(key, []) if _key(b) not in html_bands), key=_key
        ):
            problems.append(f"{where}: PDF band {_describe(band)} is not in the HTML")
    return problems


def _key(band: RateBand) -> tuple[int, int, int]:
    return (band.start_min, band.end_min, band.amount_cents)
