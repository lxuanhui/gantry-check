"""End-to-end test of the ingest pipeline, with every upstream source mocked."""

from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from gantry_check.cli import main
from gantry_check.domain.models import (
    SGT,
    DayType,
    Gantry,
    PublicHoliday,
    RateBand,
    RateSnapshot,
    VehicleType,
)
from gantry_check.ingest import refresh as refresh_module
from gantry_check.ingest import sources
from gantry_check.ingest.refresh import RefreshError, run_refresh
from gantry_check.ingest.sources import KML_URL, RATES_ZIP_URL, html_table_url
from gantry_check.repo.sqlite import SqliteRepo

FIXTURES = Path(__file__).parent / "fixtures"

#: What LTA serves for a gantry/vehicle/day combination that is never charged.
NOT_IN_OPERATION = "<table class='styler'><tr><td>Not in operation.</td></tr></table>"

#: Fixtures we have captured for real, keyed by the URL they came from.
REAL_TABLES = {
    ("35", VehicleType.CAR, DayType.WEEKDAY): "35-table-0-0.html",
    ("35", VehicleType.CAR, DayType.SATURDAY): "35-table-0-1.html",
    ("35", VehicleType.CAR, DayType.EVE_MAJOR_PH_WEEKDAY): "35-table-0-2.html",
    ("1", VehicleType.CAR, DayType.WEEKDAY): "1-table-0-0.html",
    ("47", VehicleType.CAR, DayType.WEEKDAY): "47-table-0-0.html",
}

HOLIDAYS = [
    PublicHoliday(date=date(2026, 1, 1), name="New Year's Day", is_major=False),
    PublicHoliday(date=date(2026, 2, 17), name="Chinese New Year", is_major=True),
]


def _rates_zip() -> bytes:
    """LTA ships the base-rate PDF inside `ERP Rates.zip`; rebuild that in memory."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "1PCU_ERP_Rates_Report.pdf",
            (FIXTURES / "erp_rates_2026-09-07.pdf").read_bytes(),
        )
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """78 gantries x 16 tables at the polite 0.1s spacing would take two minutes."""
    monkeypatch.setattr(sources, "REQUEST_SPACING_S", 0.0)


@pytest.fixture(autouse=True)
def _stub_holidays(monkeypatch: pytest.MonkeyPatch) -> None:
    """data.gov.sg is out of scope here; `test_holidays.py` covers it."""

    def fake_fetch(client: httpx.Client, years: list[int]) -> list[PublicHoliday]:
        return list(HOLIDAYS)

    monkeypatch.setattr(refresh_module, "fetch_holidays", fake_fetch)


@pytest.fixture
def upstream() -> Iterator[respx.MockRouter]:
    """Mock every URL `run_refresh` touches. Exact routes must precede the catch-all."""
    with respx.mock(assert_all_called=False) as router:
        router.get(KML_URL).mock(
            return_value=httpx.Response(200, content=(FIXTURES / "erp-kml-0.kml").read_bytes())
        )
        router.get(RATES_ZIP_URL).mock(return_value=httpx.Response(200, content=_rates_zip()))
        for key, filename in REAL_TABLES.items():
            router.get(html_table_url(*key)).mock(
                return_value=httpx.Response(
                    200, text=(FIXTURES / filename).read_text(encoding="utf-8")
                )
            )
        router.get(url__regex=r".*/\d+-table-\d-\d\.html$").mock(
            return_value=httpx.Response(200, text=NOT_IN_OPERATION)
        )
        yield router


# --------------------------------------------------------------------------- refresh


async def test_refresh_refuses_to_store_a_mismatching_snapshot(
    upstream: respx.MockRouter, tmp_path: Path
) -> None:
    """Most gantries are stubbed as "not in operation", so the PDF disagrees loudly."""
    with SqliteRepo(":memory:") as repo:
        with pytest.raises(RefreshError) as excinfo:
            await run_refresh(
                repo,
                out_path=None,
                cache_dir=tmp_path / "cache",
                log=lambda _: None,
            )
        assert "--allow-mismatch" in str(excinfo.value)
        assert "is not in the HTML" in str(excinfo.value)
        # Nothing was activated.
        assert await repo.active_snapshot() is None


async def test_refresh_end_to_end(upstream: respx.MockRouter, tmp_path: Path) -> None:
    out_path = tmp_path / "snapshots" / "snapshot.sql"
    now = datetime(2026, 9, 20, 12, 0, tzinfo=SGT)

    with SqliteRepo(":memory:") as repo:
        result = await run_refresh(
            repo,
            out_path=out_path,
            allow_mismatch=True,
            cache_dir=tmp_path / "cache",
            now=now,
            log=lambda _: None,
        )

        assert result.gantry_count == 78
        assert result.band_count > 0
        assert result.holiday_count == len(HOLIDAYS)
        assert result.effective_from == date(2026, 9, 7)
        assert result.mismatches  # stored under protest
        assert len(result.html_sha256) == 64
        assert len(result.pdf_sha256) == 64

        assert len(await repo.gantries()) == 78
        assert [h.date for h in await repo.holidays()] == [h.date for h in HOLIDAYS]

        snapshot = await repo.active_snapshot()
        assert snapshot is not None
        assert snapshot.id == result.snapshot_id
        assert snapshot.effective_from == date(2026, 9, 7)
        assert snapshot.is_active
        assert snapshot.html_sha256 == result.html_sha256
        assert snapshot.pdf_sha256 == result.pdf_sha256

        # The one gantry/vehicle/day whose real table we serve: CTE before Braddell Rd, 08:05.
        bands = await repo.bands("35", VehicleType.CAR, DayType.WEEKDAY)
        peak = [b for b in bands if b.start_min == 8 * 60 + 5]
        assert len(peak) == 1
        assert peak[0].end_min == 9 * 60 + 25
        assert peak[0].amount_cents == 300

        # `fetched_at` is normalised to UTC before it is recorded.
        assert await repo.get_meta("last_refresh_at") == "2026-09-20T04:00:00+00:00"
        assert await repo.get_meta("last_refresh_effective_from") == "2026-09-07"
        assert await repo.get_meta("last_refresh_mismatches") == str(len(result.mismatches))

        total_bands = len(await repo.all_bands())

    assert result.sql_path == out_path
    assert out_path.exists()
    sql = out_path.read_text(encoding="utf-8")

    # The export must load into an empty database with foreign keys enforced.
    with SqliteRepo(":memory:") as fresh:
        await fresh.apply_migrations()
        fresh.connection.executescript(sql)
        assert len(await fresh.all_bands()) == total_bands == result.band_count
        assert len(await fresh.gantries()) == 78
        reloaded = await fresh.active_snapshot()
        assert reloaded is not None
        assert reloaded.effective_from == date(2026, 9, 7)


async def test_refresh_tolerates_a_missing_next_year_holiday_dataset(
    upstream: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From January until MOM publishes next year's list, only the current year exists."""
    asked: list[list[int]] = []

    def fake_fetch(client: httpx.Client, years: list[int]) -> list[PublicHoliday]:
        asked.append(list(years))
        if len(years) > 1:
            raise ValueError(f"data.gov.sg has no public holiday dataset for [{years[-1]}]")
        return list(HOLIDAYS)

    monkeypatch.setattr(refresh_module, "fetch_holidays", fake_fetch)
    logged: list[str] = []
    with SqliteRepo(":memory:") as repo:
        result = await run_refresh(
            repo,
            out_path=None,
            allow_mismatch=True,
            cache_dir=tmp_path / "cache",
            now=datetime(2027, 1, 5, 9, 0, tzinfo=SGT),
            log=logged.append,
        )
        assert asked == [[2027, 2028], [2027]]
        assert result.holiday_count == len(HOLIDAYS)
        assert any("WARNING" in line and "2027 only" in line for line in logged)


async def test_refresh_is_deterministic_over_the_html_digest(
    upstream: respx.MockRouter, tmp_path: Path
) -> None:
    """The same upstream bytes must hash the same way regardless of fetch order."""
    digests = []
    for i in range(2):
        with SqliteRepo(":memory:") as repo:
            result = await run_refresh(
                repo,
                out_path=None,
                allow_mismatch=True,
                cache_dir=tmp_path / f"cache-{i}",
                log=lambda _: None,
            )
            digests.append(result.html_sha256)
    assert digests[0] == digests[1]


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def populated_db(tmp_path: Path) -> Path:
    """A minimal snapshot written directly, so the CLI test needs no HTTP at all."""
    path = tmp_path / "local.sqlite"

    async def build() -> None:
        with SqliteRepo(path) as repo:
            await repo.apply_migrations()
            # Foreign keys are ON: the gantry must exist before its bands.
            await repo.upsert_gantries(
                [
                    Gantry(
                        number="35",
                        name="CTE before Braddell Road",
                        lat=1.3402,
                        lng=103.8464,
                        zone_id="CT4",
                    )
                ]
            )
            await repo.replace_holidays([])
            await repo.write_snapshot(
                RateSnapshot(
                    effective_from=date(2026, 9, 7),
                    fetched_at=datetime(2026, 9, 20, 4, 0),
                    html_sha256="0" * 64,
                    pdf_sha256="1" * 64,
                ),
                [
                    RateBand("35", VehicleType.CAR, DayType.WEEKDAY, 485, 565, 300),
                    RateBand("35", VehicleType.CAR, DayType.WEEKDAY, 565, 570, 200),
                ],
                activate=True,
            )

    asyncio.run(build())
    return path


def test_cli_rates(populated_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # 2026-09-21 is a Monday, and no holiday is stored -> WEEKDAY.
    code = main(["rates", "35", "--at", "2026-09-21T08:10:00", "--db", str(populated_db)])
    out = capsys.readouterr().out
    assert code == 0
    assert "3.00" in out
    assert "CTE before Braddell Road" in out
    assert "weekday" in out


def test_cli_rates_table(populated_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["rates", "35", "--at", "2026-09-21T08:10:00", "--db", str(populated_db), "--table"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "08:05-09:25" in out
    assert "$3.00" in out
    assert "$2.00" in out


def test_cli_rates_unknown_gantry(populated_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["rates", "999", "--at", "2026-09-21T08:10:00", "--db", str(populated_db)])
    assert code == 1
    assert "999" in capsys.readouterr().err


def test_cli_rates_sunday_is_free(populated_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # 2026-09-20 is a Sunday.
    code = main(["rates", "35", "--at", "2026-09-20T08:10:00", "--db", str(populated_db)])
    out = capsys.readouterr().out
    assert code == 0
    assert "$0.00" in out
    assert "sunday_ph" in out


def test_cli_rates_missing_db(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["rates", "35", "--db", str(tmp_path / "nope.sqlite")])
