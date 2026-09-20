"""Tests for the only networked module in the ingest pipeline."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from gantry_check.domain.models import CHARGEABLE_DAY_TYPES, DayType, VehicleType
from gantry_check.ingest import sources
from gantry_check.ingest.sources import (
    cache_path,
    fetch_all_html_tables,
    fetch_bytes,
    html_table_url,
    sha256_hex,
)

EXAMPLE_URL = "https://example.test/erp-kml-0.kml"


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """LTA gets a 0.1s gap between requests; a mocked LTA does not need one."""
    monkeypatch.setattr(sources, "REQUEST_SPACING_S", 0.0)


# --------------------------------------------------------------------------- fetch_bytes


@respx.mock
def test_fetch_bytes_without_cache() -> None:
    route = respx.get(EXAMPLE_URL).mock(return_value=httpx.Response(200, content=b"<kml/>"))
    with httpx.Client() as client:
        assert fetch_bytes(client, EXAMPLE_URL) == b"<kml/>"
    assert route.call_count == 1


@respx.mock
def test_fetch_bytes_writes_then_reads_the_cache(tmp_path: Path) -> None:
    route = respx.get(EXAMPLE_URL).mock(return_value=httpx.Response(200, content=b"<kml/>"))
    with httpx.Client() as client:
        first = fetch_bytes(client, EXAMPLE_URL, tmp_path)
        # A cache hit must not touch the network at all.
        second = fetch_bytes(client, EXAMPLE_URL, tmp_path)

    assert first == second == b"<kml/>"
    assert route.call_count == 1
    cached = cache_path(tmp_path, EXAMPLE_URL)
    assert cached.exists()
    assert cached.name == f"{sha256_hex(EXAMPLE_URL.encode())}.kml"
    assert cached.read_bytes() == b"<kml/>"


@respx.mock
def test_fetch_bytes_serves_a_pre_existing_cache_entry(tmp_path: Path) -> None:
    route = respx.get(EXAMPLE_URL).mock(return_value=httpx.Response(200, content=b"fresh"))
    cache_path(tmp_path, EXAMPLE_URL).write_bytes(b"stale-but-cached")

    with httpx.Client() as client:
        assert fetch_bytes(client, EXAMPLE_URL, tmp_path) == b"stale-but-cached"
    assert route.call_count == 0


@respx.mock
def test_fetch_bytes_raises_on_404(tmp_path: Path) -> None:
    respx.get(EXAMPLE_URL).mock(return_value=httpx.Response(404))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError) as excinfo:
        fetch_bytes(client, EXAMPLE_URL, tmp_path)
    assert excinfo.value.response.status_code == 404
    # A failed fetch must not poison the cache.
    assert not cache_path(tmp_path, EXAMPLE_URL).exists()


# ------------------------------------------------------------------ fetch_all_html_tables


def test_html_table_url_rejects_free_days() -> None:
    with pytest.raises(ValueError):
        html_table_url("35", VehicleType.CAR, DayType.SUNDAY_PH)


@respx.mock(assert_all_called=False)
async def test_fetch_all_html_tables_covers_every_vehicle_and_day(
    respx_mock: respx.MockRouter,
) -> None:
    body = "<table class='styler'><tr><td>Not in operation.</td></tr></table>"
    respx_mock.get(url__regex=r".*/\d+-table-\d-\d\.html$").mock(
        return_value=httpx.Response(200, text=body)
    )

    async with httpx.AsyncClient() as client:
        tables = await fetch_all_html_tables(client, ["35"])

    assert len(tables) == len(VehicleType) * len(CHARGEABLE_DAY_TYPES) == 16
    assert set(tables) == {
        ("35", vehicle, day) for vehicle in VehicleType for day in CHARGEABLE_DAY_TYPES
    }
    assert all(text == body for text in tables.values())


@respx.mock(assert_all_called=False)
async def test_fetch_all_html_tables_caches(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    body = "<table class='styler'><tr><td>Not in operation.</td></tr></table>"
    route = respx_mock.get(url__regex=r".*/\d+-table-\d-\d\.html$").mock(
        return_value=httpx.Response(200, text=body)
    )

    async with httpx.AsyncClient() as client:
        await fetch_all_html_tables(client, ["35"], cache_dir=tmp_path)
        assert route.call_count == 16
        await fetch_all_html_tables(client, ["35"], cache_dir=tmp_path)

    assert route.call_count == 16  # second pass served entirely from disk


@respx.mock(assert_all_called=False)
async def test_fetch_all_html_tables_propagates_404(respx_mock: respx.MockRouter) -> None:
    body = "<table class='styler'><tr><td>Not in operation.</td></tr></table>"
    respx_mock.get(html_table_url("35", VehicleType.HGV, DayType.SATURDAY)).mock(
        return_value=httpx.Response(404)
    )
    respx_mock.get(url__regex=r".*/\d+-table-\d-\d\.html$").mock(
        return_value=httpx.Response(200, text=body)
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await fetch_all_html_tables(client, ["35"])
    assert excinfo.value.response.status_code == 404
