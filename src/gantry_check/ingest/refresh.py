"""The full ingest pipeline: fetch every upstream source, cross-check it, store a snapshot.

This is the only module that wires the pure parsers in `gantry_check.ingest` to the network
(`sources`) and to a repository. `gantry_check.cli` is a thin argparse shell over `run_refresh`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from gantry_check.domain.models import (
    SGT,
    DayType,
    Gantry,
    RateBand,
    RateSnapshot,
    VehicleType,
)
from gantry_check.ingest.gantry_lines import (
    GANTRY_GEOJSON_DATASET_ID,
    attach_lines,
    fetch_gantry_geojson,
    parse_gantry_lines,
)
from gantry_check.ingest.holidays import fetch_holidays
from gantry_check.ingest.html_rates import parse_html_table
from gantry_check.ingest.kml_gantries import load_zones, parse_kml
from gantry_check.ingest.pdf_rates import compare, parse_rates_pdf
from gantry_check.ingest.sources import (
    KML_URL,
    RATES_ZIP_URL,
    extract_rates_pdf,
    fetch_all_html_tables,
    fetch_bytes,
    sha256_hex,
)
from gantry_check.repo.sqlite import SqliteRepo

#: Repository root -- src/gantry_check/ingest/refresh.py -> parents[3].
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ZONES_CSV = PROJECT_ROOT / "data" / "static" / "annex_d_zones.csv"

HTTP_TIMEOUT_S = 30.0
USER_AGENT = "gantry-check/0.1 (+https://github.com/)"


class RefreshError(Exception):
    """A refresh could not produce a trustworthy snapshot."""


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """What one successful `run_refresh` wrote."""

    snapshot_id: int
    effective_from: date
    gantry_count: int
    band_count: int
    holiday_count: int
    mismatches: list[str]
    html_sha256: str
    pdf_sha256: str
    lines_matched: int = 0  # gantries that got a `line_wkt` from LTA's gantry GeoJSON
    sql_path: Path | None = None


def _as_utc(moment: datetime | None) -> datetime:
    """Normalise to an aware UTC datetime; a naive value is assumed to already be UTC."""
    if moment is None:
        return datetime.now(tz=UTC)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _table_sort_key(key: tuple[str, VehicleType, DayType]) -> tuple[int, str, str, str]:
    """Deterministic ordering for the HTML digest: numeric gantry, then vehicle, then day."""
    number, vehicle, day = key
    try:
        numeric = int(number)
    except ValueError:
        numeric = 1 << 30
    return (numeric, number, vehicle.value, day.value)


def _parse_tables(
    tables: dict[tuple[str, VehicleType, DayType], str], gantries: list[Gantry]
) -> tuple[list[RateBand], str]:
    """Parse every fetched HTML table into bands, plus a digest of the raw HTML."""
    missing = [
        g.number for g in gantries if (g.number, VehicleType.CAR, DayType.WEEKDAY) not in tables
    ]
    if missing:
        raise RefreshError(
            "no car/weekday rate table was fetched for "
            f"{len(missing)} gantries: {', '.join(missing[:10])}"
        )

    keys = sorted(tables, key=_table_sort_key)
    bands: list[RateBand] = []
    for key in keys:
        number, vehicle, day = key
        try:
            bands.extend(parse_html_table(tables[key], number, vehicle, day))
        except ValueError as exc:
            raise RefreshError(f"could not parse an LTA rate table: {exc}") from exc

    digest_source = "".join(tables[key] for key in keys).encode("utf-8")
    return bands, sha256_hex(digest_source)


async def run_refresh(
    repo: SqliteRepo,
    *,
    out_path: Path | None,
    allow_mismatch: bool = False,
    cache_dir: Path | None = None,
    years: list[int] | None = None,
    include_lines: bool = True,
    client: httpx.Client | None = None,
    async_client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
    zones_csv: Path | None = None,
    log: Callable[[str], None] = print,
) -> RefreshResult:
    """Fetch gantries, rates and holidays, cross-check them, and write an active snapshot.

    Raises `RefreshError` when the HTML tables and the base-rate PDF disagree, unless
    `allow_mismatch` is set (LTA's PDF lags the tables by a few days around a rate change).
    """
    fetched_at = _as_utc(now)
    headers = {"User-Agent": USER_AGENT}

    if client is not None:
        return await _run(
            repo,
            client=client,
            async_client=async_client,
            out_path=out_path,
            allow_mismatch=allow_mismatch,
            cache_dir=cache_dir,
            years=years,
            include_lines=include_lines,
            fetched_at=fetched_at,
            zones_csv=zones_csv,
            log=log,
        )

    with httpx.Client(timeout=HTTP_TIMEOUT_S, headers=headers) as owned:
        return await _run(
            repo,
            client=owned,
            async_client=async_client,
            out_path=out_path,
            allow_mismatch=allow_mismatch,
            cache_dir=cache_dir,
            years=years,
            include_lines=include_lines,
            fetched_at=fetched_at,
            zones_csv=zones_csv,
            log=log,
        )


async def _fetch_tables(
    async_client: httpx.AsyncClient | None,
    gantry_numbers: list[str],
    cache_dir: Path | None,
) -> dict[tuple[str, VehicleType, DayType], str]:
    if async_client is not None:
        return await fetch_all_html_tables(async_client, gantry_numbers, cache_dir=cache_dir)
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_S, headers={"User-Agent": USER_AGENT}
    ) as owned:
        return await fetch_all_html_tables(owned, gantry_numbers, cache_dir=cache_dir)


async def _run(
    repo: SqliteRepo,
    *,
    client: httpx.Client,
    async_client: httpx.AsyncClient | None,
    out_path: Path | None,
    allow_mismatch: bool,
    cache_dir: Path | None,
    years: list[int] | None,
    include_lines: bool,
    fetched_at: datetime,
    zones_csv: Path | None,
    log: Callable[[str], None],
) -> RefreshResult:
    await repo.apply_migrations()

    # 1. Gantries, from the OneMotoring KML plus the static Annex D zone mapping.
    log(f"fetching gantries from {KML_URL}")
    zones = load_zones(zones_csv or DEFAULT_ZONES_CSV)
    gantries = parse_kml(fetch_bytes(client, KML_URL, cache_dir), zones=zones)
    if not gantries:
        raise RefreshError("the ERP KML yielded no gantries")
    numbers = [g.number for g in gantries]
    log(f"  {len(gantries)} gantries")

    # 1b. Gantry *lines* across the carriageway, from LTA's gantry GeoJSON on data.gov.sg.
    #     Route matching needs these to tell one carriageway from the other; a KML point alone
    #     would charge a route travelling the opposite way.
    lines_matched = 0
    without_lines: list[str] = []
    if include_lines:
        log(f"fetching gantry lines from data.gov.sg dataset {GANTRY_GEOJSON_DATASET_ID}")
        lines = parse_gantry_lines(fetch_gantry_geojson(client, cache_dir))
        gantries, line_stats = attach_lines(gantries, lines)
        lines_matched = line_stats.gantries_with_lines
        without_lines = line_stats.gantries_without_lines
        log(f"  lines: {line_stats.summary(len(gantries))}")

    await repo.upsert_gantries(gantries)

    # 2. Rate bands, from LTA's per-gantry HTML tables (4 vehicle types x 4 day types each).
    log(f"fetching {len(numbers) * 16} rate tables")
    tables = await _fetch_tables(async_client, numbers, cache_dir)
    bands, html_sha256 = _parse_tables(tables, gantries)
    log(f"  {len(bands)} rate bands")

    # 3. The base-rate PDF: our only source for the effective date, and a cross-check.
    log(f"fetching base rates from {RATES_ZIP_URL}")
    pdf_bytes = extract_rates_pdf(fetch_bytes(client, RATES_ZIP_URL, cache_dir))
    pdf_sha256 = sha256_hex(pdf_bytes)
    pdf = parse_rates_pdf(pdf_bytes)
    log(f"  effective from {pdf.effective_from.isoformat()}")

    # 4. Cross-check. The PDF covers car weekday/Saturday rates only; `compare` restricts itself.
    mismatches = compare(bands, pdf.bands)
    if set(numbers) != pdf.gantry_numbers:
        only_kml = sorted(set(numbers) - pdf.gantry_numbers, key=int)
        only_pdf = sorted(pdf.gantry_numbers - set(numbers), key=int)
        mismatches.append(
            f"gantry lists differ: only in KML {only_kml or '[]'}, only in PDF {only_pdf or '[]'}"
        )
    if mismatches:
        if not allow_mismatch:
            raise RefreshError(
                f"{len(mismatches)} HTML/PDF mismatches (re-run with --allow-mismatch to "
                "store the snapshot anyway):\n" + "\n".join(mismatches)
            )
        log(f"WARNING: {len(mismatches)} HTML/PDF mismatches, stored anyway:")
        for line in mismatches:
            log(f"  WARNING: {line}")

    # 5. Public holidays, which decide the day type at charge time.
    sgt_year = fetched_at.astimezone(SGT).year
    if years:
        log(f"fetching public holidays for {years}")
        holidays = fetch_holidays(client, years)
    else:
        # MOM publishes next year's holidays around mid-year; until then the
        # scheduled refresh must not fail just because next year is missing.
        log(f"fetching public holidays for {[sgt_year, sgt_year + 1]}")
        try:
            holidays = fetch_holidays(client, [sgt_year, sgt_year + 1])
        except ValueError as exc:
            log(f"  WARNING: {exc}; storing {sgt_year} only")
            holidays = fetch_holidays(client, [sgt_year])
    await repo.replace_holidays(holidays)
    log(f"  {len(holidays)} holidays")

    # 6. Store, activate, and record provenance.
    snapshot = RateSnapshot(
        effective_from=pdf.effective_from,
        fetched_at=fetched_at,
        html_sha256=html_sha256,
        pdf_sha256=pdf_sha256,
    )
    snapshot_id = await repo.write_snapshot(snapshot, bands, activate=True)
    await repo.set_meta("last_refresh_at", fetched_at.isoformat())
    await repo.set_meta("last_refresh_effective_from", pdf.effective_from.isoformat())
    await repo.set_meta("last_refresh_mismatches", str(len(mismatches)))
    if include_lines:
        await repo.set_meta("gantry_lines_dataset", GANTRY_GEOJSON_DATASET_ID)
        await repo.set_meta("gantries_without_lines", ",".join(without_lines))

    # 7. Optionally emit the D1-loadable SQL for `wrangler d1 execute --file`.
    sql_path: Path | None = None
    if out_path is not None:
        sql_path = Path(out_path)
        sql_path.parent.mkdir(parents=True, exist_ok=True)
        sql_path.write_text(repo.export_sql(snapshot_id), encoding="utf-8")
        log(f"wrote {sql_path}")

    return RefreshResult(
        snapshot_id=snapshot_id,
        effective_from=pdf.effective_from,
        gantry_count=len(gantries),
        band_count=len(bands),
        holiday_count=len(holidays),
        mismatches=mismatches,
        html_sha256=html_sha256,
        pdf_sha256=pdf_sha256,
        lines_matched=lines_matched,
        sql_path=sql_path,
    )
