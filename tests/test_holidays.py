"""Tests for the data.gov.sg public holiday ingest."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from gantry_check.ingest.holidays import (
    fetch_holidays,
    find_holiday_datasets,
    mark_major,
    parse_holidays_csv,
)

# Straight from d_149b61ad0a22f61c09dc80f2df5bbec8 ("Public Holidays for 2026"), including MOM's
# typographic apostrophe in "New Year’s Day" and the "(Observed)" rows.
SAMPLE_CSV = """date,day,holiday
2026-01-01,Thursday,New Year’s Day
2026-02-17,Tuesday,Chinese New Year
2026-02-18,Wednesday,Chinese New Year
2026-03-21,Saturday,Hari Raya Puasa
2026-04-03,Friday,Good Friday
2026-05-01,Friday,Labour Day
2026-05-27,Wednesday,Hari Raya Haji
2026-05-31,Sunday,Vesak Day
2026-06-01,Monday,Vesak Day (Observed)
2026-08-09,Sunday,National Day
2026-08-10,Monday,National Day (Observed)
2026-11-08,Sunday,Deepavali
2026-11-09,Monday,Deepavali (Observed)
2026-12-25,Friday,Christmas Day
"""


def test_parse_holidays_csv() -> None:
    holidays = parse_holidays_csv(SAMPLE_CSV)
    assert len(holidays) == 14
    assert holidays[0].date == date(2026, 1, 1)
    assert holidays[0].name == "New Year’s Day"
    assert holidays[-1].date == date(2026, 12, 25)
    assert all(not holiday.is_major for holiday in holidays)


def test_parse_holidays_csv_tolerates_a_bom() -> None:
    assert parse_holidays_csv("﻿" + SAMPLE_CSV) == parse_holidays_csv(SAMPLE_CSV)


def test_parse_holidays_csv_rejects_a_wrong_schema() -> None:
    with pytest.raises(ValueError, match="missing column"):
        parse_holidays_csv("day,name\nMonday,Something\n")


def test_parse_holidays_csv_rejects_a_bad_date() -> None:
    with pytest.raises(ValueError, match="unparseable public holiday date"):
        parse_holidays_csv("date,day,holiday\n01/01/2026,Thursday,New Year\n")


def test_mark_major() -> None:
    marked = {(h.date, h.name): h.is_major for h in mark_major(parse_holidays_csv(SAMPLE_CSV))}
    assert marked[(date(2026, 1, 1), "New Year’s Day")] is True
    assert marked[(date(2026, 3, 21), "Hari Raya Puasa")] is True
    assert marked[(date(2026, 11, 8), "Deepavali")] is True
    assert marked[(date(2026, 12, 25), "Christmas Day")] is True
    assert marked[(date(2026, 4, 3), "Good Friday")] is False
    assert marked[(date(2026, 5, 1), "Labour Day")] is False
    assert marked[(date(2026, 5, 27), "Hari Raya Haji")] is False
    assert marked[(date(2026, 8, 9), "National Day")] is False


def test_mark_major_takes_only_the_first_day_of_chinese_new_year() -> None:
    marked = {h.date: h.is_major for h in mark_major(parse_holidays_csv(SAMPLE_CSV))}
    assert marked[date(2026, 2, 17)] is True
    assert marked[date(2026, 2, 18)] is False


def test_mark_major_ignores_observed_substitute_days() -> None:
    """The eve rate attaches to the festival itself, not to MOM's Monday substitute."""
    marked = {(h.date, h.name): h.is_major for h in mark_major(parse_holidays_csv(SAMPLE_CSV))}
    assert marked[(date(2026, 11, 9), "Deepavali (Observed)")] is False
    assert marked[(date(2026, 6, 1), "Vesak Day (Observed)")] is False


def test_mark_major_handles_a_straight_apostrophe() -> None:
    csv_text = "date,day,holiday\n2027-01-01,Friday,New Year's Day\n"
    assert mark_major(parse_holidays_csv(csv_text))[0].is_major is True


def test_mark_major_is_per_year() -> None:
    csv_text = (
        "date,day,holiday\n"
        "2026-02-17,Tuesday,Chinese New Year\n"
        "2026-02-18,Wednesday,Chinese New Year\n"
        "2027-02-06,Saturday,Chinese New Year\n"
        "2027-02-07,Sunday,Chinese New Year\n"
    )
    marked = {h.date: h.is_major for h in mark_major(parse_holidays_csv(csv_text))}
    assert marked == {
        date(2026, 2, 17): True,
        date(2026, 2, 18): False,
        date(2027, 2, 6): True,
        date(2027, 2, 7): False,
    }


def test_live_fetch() -> None:
    """Hits data.gov.sg for real; skipped when the network is unavailable."""
    years = [2026, 2027]
    try:
        with httpx.Client(timeout=30.0) as client:
            datasets = find_holiday_datasets(client)
            missing = [year for year in years if year not in datasets]
            assert not missing, f"no dataset for {missing}: {sorted(datasets)}"
            holidays = fetch_holidays(client, years)
    except (httpx.HTTPError, OSError) as exc:  # pragma: no cover - network dependent
        pytest.skip(f"data.gov.sg unreachable: {exc}")

    assert holidays
    for year in years:
        of_year = [holiday for holiday in holidays if holiday.date.year == year]
        assert of_year, f"no public holidays returned for {year}"
        assert date(year, 1, 1) in {holiday.date for holiday in of_year}
        assert any(holiday.is_major for holiday in of_year)
    assert {holiday.date.year for holiday in holidays} == set(years)
