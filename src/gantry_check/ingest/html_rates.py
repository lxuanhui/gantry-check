"""Parse LTA's per-gantry `{gantry}-table-{v}-{d}.html` rate tables into `RateBand`s.

The payload is a fragment, not a document: a little JS that highlights the current row, followed
by `<table class="styler">` with one `<tr>` per charging window. A gantry that is not charged for
that vehicle/day combination renders a single "Not in operation." row.

Zero-amount rows are kept. They are not noise: LTA uses them to state the full operating window
of the gantry, which is informative when diffing snapshots. `pdf_rates.normalize` drops them when
comparing against the PDF.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from gantry_check.domain.models import DayType, RateBand, VehicleType

_WHITESPACE_RE = re.compile(r"\s+")
_TIME_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
_AMOUNT_RE = re.compile(r"^\$(\d+)\.(\d{2})$")
_NOT_IN_OPERATION_RE = re.compile(r"^not in operation\.?$", re.IGNORECASE)


class _StylerTableParser(HTMLParser):
    """Collect the rows of the first `<table class="styler">` as lists of cell texts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] | None = None
        self._depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self._depth:
                self._depth += 1
                return
            classes = (dict(attrs).get("class") or "").split()
            if "styler" in classes and self.rows is None:
                self._depth = 1
                self.rows = []
        elif self._depth == 1:
            if tag == "tr":
                self._row = []
            elif tag in ("td", "th") and self._row is not None:
                self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._depth:
            self._depth -= 1
        elif self._depth == 1:
            if tag in ("td", "th") and self._cell is not None and self._row is not None:
                self._row.append(_WHITESPACE_RE.sub(" ", "".join(self._cell)).strip())
                self._cell = None
            elif tag == "tr" and self._row is not None:
                if self.rows is not None:
                    self.rows.append(self._row)
                self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _to_minutes(hours: str, minutes: str) -> int:
    hour, minute = int(hours), int(minutes)
    if hour > 24 or minute > 59 or (hour == 24 and minute != 0):
        raise ValueError(f"impossible clock time {hours}:{minutes}")
    return hour * 60 + minute


def parse_html_table(
    html_text: str, gantry_number: str, vehicle: VehicleType, day: DayType
) -> list[RateBand]:
    """Parse one LTA rate table. Returns `[]` when the gantry is not in operation."""
    where = (
        f"gantry {gantry_number} {vehicle.value}/{day.value} "
        f"({gantry_number}-table-{vehicle.lta_table_index}-{day.lta_table_index})"
    )
    parser = _StylerTableParser()
    parser.feed(html_text)
    parser.close()
    if parser.rows is None:
        raise ValueError(f"{where}: no <table class='styler'> found (error page or layout change?)")
    rows = [row for row in parser.rows if any(cell for cell in row)]
    if not rows:
        raise ValueError(f"{where}: rate table is empty")

    if len(rows) == 1 and len(rows[0]) == 1:
        if _NOT_IN_OPERATION_RE.match(rows[0][0]):
            return []
        raise ValueError(f"{where}: unexpected single-cell row {rows[0][0]!r}")

    bands: list[RateBand] = []
    for row in rows:
        if len(row) != 2:
            raise ValueError(f"{where}: expected 2 cells per row, got {row!r}")
        time_match = _TIME_RANGE_RE.match(row[0])
        if time_match is None:
            raise ValueError(f"{where}: unparseable time range {row[0]!r}")
        amount_match = _AMOUNT_RE.match(row[1])
        if amount_match is None:
            raise ValueError(f"{where}: unparseable amount {row[1]!r}")
        start = _to_minutes(time_match.group(1), time_match.group(2))
        end = _to_minutes(time_match.group(3), time_match.group(4))
        if end <= start:
            raise ValueError(f"{where}: band {row[0]!r} does not end after it starts")
        cents = int(amount_match.group(1)) * 100 + int(amount_match.group(2))
        if bands:
            previous = bands[-1]
            if start < previous.end_min:
                raise ValueError(
                    f"{where}: band {row[0]!r} overlaps or precedes the previous band "
                    f"ending at minute {previous.end_min}"
                )
        bands.append(
            RateBand(
                gantry_number=gantry_number,
                vehicle_type=vehicle,
                day_type=day,
                start_min=start,
                end_min=end,
                amount_cents=cents,
            )
        )
    return bands
