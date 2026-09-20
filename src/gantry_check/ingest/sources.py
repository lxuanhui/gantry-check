"""Upstream source URLs and the only place in the ingest pipeline that touches the network.

Everything else in `gantry_check.ingest` is a pure parser over bytes/str, so the parsers can be
unit-tested against the fixtures in `tests/fixtures/` without any I/O.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import time
import zipfile
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from gantry_check.domain.models import CHARGEABLE_DAY_TYPES, DayType, VehicleType

KML_URL = "https://onemotoring.lta.gov.sg/mapapp/kml/erp-kml/erp-kml-0.kml"
RATES_ZIP_URL = (
    "https://datamall.lta.gov.sg/content/dam/datamall/datasets/Facts_Figures/"
    "Traffic_and_Trips/ERP%20Rates.zip"
)
HTML_TABLE_URL = "https://datamall.lta.gov.sg/mapapp/pages/tables/{gantry}-table-{v}-{d}.html"

# datamall serves the rate tables to anything, but a plain httpx UA occasionally trips their WAF.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-SG,en;q=0.9",
}

#: Minimum wall-clock gap between two outgoing requests. LTA is a small public site; be polite.
REQUEST_SPACING_S = 0.1


def sha256_hex(data: bytes) -> str:
    """Hex digest used both for cache filenames and for snapshot provenance."""
    return hashlib.sha256(data).hexdigest()


def html_table_url(gantry_number: str, vehicle: VehicleType, day: DayType) -> str:
    """URL of LTA's per-gantry rate table for one vehicle class and day type."""
    day_index = day.lta_table_index
    if day_index is None:
        raise ValueError(f"{day.value} is not charged, so LTA publishes no rate table for it")
    return HTML_TABLE_URL.format(gantry=gantry_number, v=vehicle.lta_table_index, d=day_index)


def cache_path(cache_dir: Path, url: str) -> Path:
    """Deterministic on-disk location for `url`: <sha256-of-url>.<ext-from-url>."""
    suffix = Path(unquote(urlparse(url).path)).suffix or ".bin"
    return cache_dir / f"{sha256_hex(url.encode('utf-8'))}{suffix}"


def fetch_bytes(client: httpx.Client, url: str, cache_dir: Path | None = None) -> bytes:
    """GET `url`, optionally reading from / writing to a content cache.

    Raises `httpx.HTTPStatusError` on any non-2xx response: a missing upstream page is a bug in
    our gantry list or a change at LTA, never something to silently skip.
    """
    path = cache_path(cache_dir, url) if cache_dir is not None else None
    if path is not None and path.exists():
        return path.read_bytes()
    response = client.get(url, headers=DEFAULT_HEADERS, follow_redirects=True)
    response.raise_for_status()
    data = response.content
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return data


def extract_rates_pdf(zip_bytes: bytes) -> bytes:
    """Pull the single PDF out of LTA's "ERP Rates.zip"."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        pdfs = [n for n in archive.namelist() if n.lower().endswith(".pdf")]
        if len(pdfs) != 1:
            raise ValueError(f"expected exactly one .pdf in the rates zip, found {pdfs!r}")
        return archive.read(pdfs[0])


class _Pacer:
    """Serialises request *starts* so they are at least `spacing` seconds apart."""

    def __init__(self, spacing: float) -> None:
        self._spacing = spacing
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_at = time.monotonic() + self._spacing


async def fetch_all_html_tables(
    client: httpx.AsyncClient,
    gantry_numbers: Iterable[str],
    concurrency: int = 8,
    cache_dir: Path | None = None,
) -> dict[tuple[str, VehicleType, DayType], str]:
    """Fetch every `{gantry}-table-{v}-{d}.html` for all vehicle types x chargeable day types.

    A 404 (or any other error status) aborts the whole crawl: a gantry that has disappeared
    upstream must be noticed, not quietly dropped from the snapshot.
    """
    keys = [
        (number, vehicle, day)
        for number in gantry_numbers
        for vehicle in VehicleType
        for day in CHARGEABLE_DAY_TYPES
    ]
    semaphore = asyncio.Semaphore(concurrency)
    pacer = _Pacer(REQUEST_SPACING_S)

    async def fetch_one(
        key: tuple[str, VehicleType, DayType],
    ) -> tuple[tuple[str, VehicleType, DayType], str]:
        url = html_table_url(*key)
        path = cache_path(cache_dir, url) if cache_dir is not None else None
        if path is not None and path.exists():
            return key, path.read_text(encoding="utf-8", errors="replace")
        async with semaphore:
            await pacer.wait()
            response = await client.get(url, headers=DEFAULT_HEADERS, follow_redirects=True)
        response.raise_for_status()
        text = response.text
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return key, text

    tasks = [asyncio.create_task(fetch_one(key)) for key in keys]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        # Stop the remaining ~1200 paced requests instead of hammering LTA for another two
        # minutes after the caller has already given up. The original error is re-raised as-is.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return dict(results)
