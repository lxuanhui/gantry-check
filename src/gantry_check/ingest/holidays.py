"""Singapore public holidays from data.gov.sg (MOM's "Singapore Public Holidays" collection).

ERP needs these twice over: no ERP is charged on Sundays and public holidays, and the eve of a
*major* public holiday uses its own rate table (LTA's `d` indices 2 and 3).

The `?query=` dataset search endpoint ignores its query and returns unrelated datasets, so the
datasets are discovered through the collection instead: collection 691 lists one child dataset
per year, named "Public Holidays for <year>".
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import date

import httpx

from gantry_check.domain.models import PublicHoliday
from gantry_check.ingest.datagov import POLL_DOWNLOAD_URL as POLL_DOWNLOAD_URL  # re-export
from gantry_check.ingest.datagov import download_dataset_text

HOLIDAY_COLLECTION_ID = "691"
COLLECTION_METADATA_URL = (
    "https://api-production.data.gov.sg/v2/public/api/collections/{collection_id}/metadata"
)
DATASET_METADATA_URL = (
    "https://api-production.data.gov.sg/v2/public/api/datasets/{dataset_id}/metadata"
)

#: Holidays whose *eve* attracts the "eve of major public holiday" ERP rate tables.
#: Matched on the exact (normalised) holiday name, so the "(Observed)" rows that MOM adds when a
#: holiday falls on a Sunday are deliberately excluded: the eve belongs to the festival itself.
MAJOR_HOLIDAY_NAMES = frozenset(
    {
        "new year's day",
        "chinese new year",
        "hari raya puasa",
        "deepavali",
        "christmas day",
    }
)
_CHINESE_NEW_YEAR = "chinese new year"


def _normalise_name(name: str) -> str:
    """Case-fold and fix MOM's typographic apostrophe: the CSV says "New Year’s Day"."""
    return " ".join(name.replace("’", "'").split()).lower()


def parse_holidays_csv(text: str) -> list[PublicHoliday]:
    """Parse a data.gov.sg public-holidays CSV (columns: date, day, holiday)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = {"date", "holiday"} - set(reader.fieldnames or ())
    if missing:
        raise ValueError(f"public holiday CSV is missing column(s) {sorted(missing)}")
    holidays: list[PublicHoliday] = []
    for row in reader:
        raw_date = (row["date"] or "").strip()
        name = " ".join((row["holiday"] or "").split())
        if not raw_date and not name:
            continue
        if not raw_date or not name:
            raise ValueError(f"incomplete public holiday row {row!r}")
        try:
            day = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ValueError(f"unparseable public holiday date {raw_date!r}") from exc
        holidays.append(PublicHoliday(date=day, name=name))
    holidays.sort(key=lambda holiday: (holiday.date, holiday.name))
    return holidays


def mark_major(holidays: Iterable[PublicHoliday]) -> list[PublicHoliday]:
    """Flag the holidays whose eve carries a special ERP rate.

    Chinese New Year spans two days but only the first attracts an "eve" rate, so within each
    year only the earliest CNY date is marked.
    """
    holidays = list(holidays)
    first_cny: dict[int, date] = {}
    for holiday in holidays:
        if _normalise_name(holiday.name) == _CHINESE_NEW_YEAR:
            year = holiday.date.year
            if year not in first_cny or holiday.date < first_cny[year]:
                first_cny[year] = holiday.date

    marked: list[PublicHoliday] = []
    for holiday in holidays:
        name = _normalise_name(holiday.name)
        is_major = name in MAJOR_HOLIDAY_NAMES
        if name == _CHINESE_NEW_YEAR:
            is_major = first_cny.get(holiday.date.year) == holiday.date
        marked.append(PublicHoliday(date=holiday.date, name=holiday.name, is_major=is_major))
    return marked


def find_holiday_datasets(client: httpx.Client) -> dict[int, str]:
    """Map year -> data.gov.sg dataset id for MOM's per-year public holiday datasets."""
    response = client.get(COLLECTION_METADATA_URL.format(collection_id=HOLIDAY_COLLECTION_ID))
    response.raise_for_status()
    metadata = response.json()["data"]["collectionMetadata"]
    datasets: dict[int, str] = {}
    for dataset_id in metadata["childDatasets"]:
        detail = client.get(DATASET_METADATA_URL.format(dataset_id=dataset_id))
        detail.raise_for_status()
        name = detail.json()["data"]["name"]
        _, _, tail = name.rpartition(" ")
        if name.lower().startswith("public holidays for") and tail.isdigit():
            datasets[int(tail)] = dataset_id
    if not datasets:
        raise ValueError(
            f"collection {HOLIDAY_COLLECTION_ID} listed no 'Public Holidays for <year>' datasets"
        )
    return datasets


def fetch_holidays(client: httpx.Client, years: list[int]) -> list[PublicHoliday]:
    """Fetch and flag Singapore public holidays for the given years."""
    datasets = find_holiday_datasets(client)
    missing = sorted(set(years) - set(datasets))
    if missing:
        raise ValueError(f"data.gov.sg has no public holiday dataset for {missing}")
    holidays: list[PublicHoliday] = []
    for year in sorted(set(years)):
        holidays.extend(parse_holidays_csv(download_dataset_text(client, datasets[year])))
    holidays.sort(key=lambda holiday: (holiday.date, holiday.name))
    return mark_major(holidays)
