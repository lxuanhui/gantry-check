"""SqliteRepo (writes, reads, SQL export) and D1Repo driven through a fake D1 binding."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from gantry_check.domain.models import (
    DayType,
    Gantry,
    PublicHoliday,
    RateBand,
    RateSnapshot,
    VehicleType,
)
from gantry_check.repo.d1 import D1Repo
from gantry_check.repo.sqlite import SqliteRepo, sql_literal

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

GANTRIES = [
    Gantry(
        number="35",
        name="CTE before Braddell Road",
        lat=1.3345,
        lng=103.8452,
        zone_id="CT4",
        line_wkt="LINESTRING(103.8450 1.3344, 103.8455 1.3347)",
        heading_deg=182.0,
    ),
    Gantry(number="47", name="PIE slip road", lat=1.3201, lng=103.8600),
]

SNAPSHOT = RateSnapshot(
    effective_from=date(2026, 9, 7),
    fetched_at=datetime(2026, 9, 8, 2, 0, tzinfo=UTC),
    html_sha256="a" * 64,
    pdf_sha256="b" * 64,
)


def make_bands(gantry: str = "35") -> list[RateBand]:
    return [
        RateBand(gantry, VehicleType.CAR, DayType.WEEKDAY, 565, 600, 200),
        RateBand(gantry, VehicleType.CAR, DayType.WEEKDAY, 485, 565, 300),
        RateBand(gantry, VehicleType.CAR, DayType.SATURDAY, 600, 660, 100),
        RateBand(gantry, VehicleType.MOTORCYCLE, DayType.WEEKDAY, 485, 565, 150),
    ]


@pytest.fixture
async def repo() -> Any:
    r = SqliteRepo(":memory:", migrations_dir=MIGRATIONS)
    await r.apply_migrations()
    yield r
    r.close()


@pytest.fixture
async def seeded(repo: SqliteRepo) -> SqliteRepo:
    await repo.upsert_gantries(GANTRIES)
    await repo.write_snapshot(SNAPSHOT, make_bands(), activate=True)
    await repo.replace_holidays(
        [
            PublicHoliday(date(2026, 8, 9), "National Day"),
            PublicHoliday(date(2026, 12, 25), "Christmas Day", is_major=True),
        ]
    )
    await repo.set_meta("source_url", "https://onemotoring.lta.gov.sg/")
    return repo


# --------------------------------------------------------------------------- migrations


async def test_apply_migrations_creates_the_schema(repo: SqliteRepo) -> None:
    names = {
        row["name"]
        for row in repo.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"gantry", "rate_snapshot", "rate_band", "public_holiday", "meta"} <= names


async def test_apply_migrations_is_idempotent(repo: SqliteRepo) -> None:
    await repo.apply_migrations()  # second run must not raise


async def test_foreign_keys_are_enforced(repo: SqliteRepo) -> None:
    assert repo.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        await repo.write_snapshot(SNAPSHOT, make_bands("nope"))


async def test_apply_migrations_reports_a_missing_directory(tmp_path: Path) -> None:
    r = SqliteRepo(":memory:", migrations_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        await r.apply_migrations()
    r.close()


def test_default_migrations_dir_resolves() -> None:
    from gantry_check.repo.sqlite import DEFAULT_MIGRATIONS_DIR

    assert (DEFAULT_MIGRATIONS_DIR / "0001_init.sql").exists()


# ------------------------------------------------------------------------------ gantries


async def test_upsert_gantries_inserts_then_updates(repo: SqliteRepo) -> None:
    await repo.upsert_gantries(GANTRIES)
    assert len(await repo.gantries()) == 2

    renamed = Gantry(
        number="35",
        name="CTE after Braddell Road",
        lat=1.3345,
        lng=103.8452,
        zone_id="CT4",
    )
    await repo.upsert_gantries([renamed])

    stored = await repo.gantries()
    assert len(stored) == 2
    by_number = {g.number: g for g in stored}
    assert by_number["35"].name == "CTE after Braddell Road"
    assert by_number["35"].line_wkt is None  # overwritten by the new row
    assert by_number["47"].name == "PIE slip road"


async def test_gantry_roundtrips_all_columns(seeded: SqliteRepo) -> None:
    g = await seeded.gantry("35")
    assert g == GANTRIES[0]
    assert await seeded.gantry("does-not-exist") is None


# ----------------------------------------------------------------------------- snapshots


async def test_write_snapshot_activation_flips(repo: SqliteRepo) -> None:
    await repo.upsert_gantries(GANTRIES)
    first = await repo.write_snapshot(SNAPSHOT, make_bands(), activate=True)
    active = await repo.active_snapshot()
    assert active is not None and active.id == first and active.is_active

    newer = RateSnapshot(
        effective_from=date(2027, 2, 1),
        fetched_at=datetime(2027, 2, 2, 1, 0, tzinfo=UTC),
        html_sha256="c" * 64,
        pdf_sha256="d" * 64,
    )
    second = await repo.write_snapshot(newer, make_bands("47"), activate=True)
    assert second != first

    active = await repo.active_snapshot()
    assert active is not None
    assert active.id == second
    assert active.effective_from == date(2027, 2, 1)
    assert active.fetched_at == newer.fetched_at

    flags = dict(repo.connection.execute("SELECT id, is_active FROM rate_snapshot").fetchall())
    assert flags == {first: 0, second: 1}


async def test_write_snapshot_without_activating(repo: SqliteRepo) -> None:
    await repo.upsert_gantries(GANTRIES)
    await repo.write_snapshot(SNAPSHOT, make_bands(), activate=True)
    newer = RateSnapshot(
        effective_from=date(2027, 2, 1),
        fetched_at=datetime(2027, 2, 2, 1, 0, tzinfo=UTC),
        html_sha256="c" * 64,
        pdf_sha256="d" * 64,
    )
    staged = await repo.write_snapshot(newer, make_bands("47"), activate=False)
    active = await repo.active_snapshot()
    assert active is not None and active.id != staged


async def test_no_active_snapshot_means_empty_reads(repo: SqliteRepo) -> None:
    assert await repo.active_snapshot() is None
    assert await repo.bands("35", VehicleType.CAR, DayType.WEEKDAY) == []
    assert await repo.all_bands() == []


# --------------------------------------------------------------------------------- bands


async def test_bands_are_sorted_and_filtered(seeded: SqliteRepo) -> None:
    bands = await seeded.bands("35", VehicleType.CAR, DayType.WEEKDAY)
    assert [(b.start_min, b.amount_cents) for b in bands] == [(485, 300), (565, 200)]
    assert all(b.vehicle_type is VehicleType.CAR for b in bands)
    assert await seeded.bands("47", VehicleType.CAR, DayType.WEEKDAY) == []


async def test_bands_only_come_from_the_active_snapshot(seeded: SqliteRepo) -> None:
    old_id = (await seeded.active_snapshot()).id  # type: ignore[union-attr]
    newer = RateSnapshot(
        effective_from=date(2027, 2, 1),
        fetched_at=datetime(2027, 2, 2, 1, 0, tzinfo=UTC),
        html_sha256="c" * 64,
        pdf_sha256="d" * 64,
    )
    new_id = await seeded.write_snapshot(
        newer,
        [RateBand("35", VehicleType.CAR, DayType.WEEKDAY, 485, 565, 999)],
        activate=True,
    )

    bands = await seeded.bands("35", VehicleType.CAR, DayType.WEEKDAY)
    assert [b.amount_cents for b in bands] == [999]
    # The superseded snapshot is still queryable by id.
    assert len(await seeded.all_bands(old_id)) == 4
    assert len(await seeded.all_bands()) == 1
    assert len(await seeded.all_bands(new_id)) == 1


async def test_all_bands_ordering(seeded: SqliteRepo) -> None:
    bands = await seeded.all_bands()
    keys = [(b.gantry_number, b.vehicle_type.value, b.day_type.value, b.start_min) for b in bands]
    assert keys == sorted(keys)
    assert len(bands) == 4


# ---------------------------------------------------------------------- holidays / meta


async def test_holidays_roundtrip(seeded: SqliteRepo) -> None:
    holidays = await seeded.holidays()
    assert [h.date for h in holidays] == [date(2026, 8, 9), date(2026, 12, 25)]
    assert holidays[0].is_major is False
    assert holidays[1].is_major is True


async def test_replace_holidays_clears_the_old_set(seeded: SqliteRepo) -> None:
    await seeded.replace_holidays([PublicHoliday(date(2027, 1, 1), "New Year's Day")])
    assert [h.name for h in await seeded.holidays()] == ["New Year's Day"]


async def test_meta_roundtrip(seeded: SqliteRepo) -> None:
    assert await seeded.get_meta("source_url") == "https://onemotoring.lta.gov.sg/"
    await seeded.set_meta("source_url", "https://example.test/")
    assert await seeded.get_meta("source_url") == "https://example.test/"
    assert await seeded.get_meta("missing") is None


# -------------------------------------------------------------------------- export_sql


def test_sql_literal_escaping() -> None:
    assert sql_literal("O'Reilly Ave") == "'O''Reilly Ave'"
    assert sql_literal(None) == "NULL"
    assert sql_literal(True) == "1"
    assert sql_literal(7) == "7"
    assert sql_literal(1.5) == "1.5"


async def test_export_sql_rejects_an_empty_database(repo: SqliteRepo) -> None:
    with pytest.raises(ValueError):
        repo.export_sql()


async def test_export_sql_reproduces_the_database(seeded: SqliteRepo) -> None:
    script = seeded.export_sql()
    assert "'CTE before Braddell Road'" in script
    assert "BEGIN" not in script  # D1 rejects explicit transactions

    target = SqliteRepo(":memory:", migrations_dir=MIGRATIONS)
    await target.apply_migrations()
    target.connection.executescript(script)

    assert await target.gantries() == await seeded.gantries()
    assert await target.holidays() == await seeded.holidays()
    assert await target.get_meta("source_url") == await seeded.get_meta("source_url")

    src_active = await seeded.active_snapshot()
    dst_active = await target.active_snapshot()
    assert dst_active == src_active
    assert await target.all_bands() == await seeded.all_bands()
    target.close()


async def test_export_sql_is_rerunnable(seeded: SqliteRepo) -> None:
    script = seeded.export_sql()
    target = SqliteRepo(":memory:", migrations_dir=MIGRATIONS)
    await target.apply_migrations()
    target.connection.executescript(script)
    target.connection.executescript(script)  # idempotent

    assert len(await target.gantries()) == 2
    assert len(await target.all_bands()) == 4
    assert len(target.connection.execute("SELECT id FROM rate_snapshot").fetchall()) == 1
    target.close()


async def test_export_sql_escapes_quotes(repo: SqliteRepo) -> None:
    await repo.upsert_gantries(
        [Gantry(number="99", name="O'Reilly Ave 'north'", lat=1.0, lng=103.0)]
    )
    await repo.write_snapshot(
        SNAPSHOT, [RateBand("99", VehicleType.CAR, DayType.WEEKDAY, 0, 60, 50)]
    )
    script = repo.export_sql()

    target = SqliteRepo(":memory:", migrations_dir=MIGRATIONS)
    await target.apply_migrations()
    target.connection.executescript(script)
    gantry = await target.gantry("99")
    assert gantry is not None and gantry.name == "O'Reilly Ave 'north'"
    target.close()


async def test_export_sql_accepts_an_explicit_snapshot_id(seeded: SqliteRepo) -> None:
    snapshot = await seeded.active_snapshot()
    assert snapshot is not None
    assert seeded.export_sql(snapshot.id) == seeded.export_sql()
    with pytest.raises(ValueError):
        seeded.export_sql(9999)


# ------------------------------------------------------------------------------- D1Repo


class FakeD1Result:
    """Mimics D1's result object: rows already materialised as Python dicts."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.results = rows


class JsArray(list):  # type: ignore[type-arg]
    """Mimics a Pyodide JsProxy array, which needs `.to_py()` before use."""

    def to_py(self) -> list[Any]:
        return list(self)


class JsD1Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.results = JsArray(rows)


class FakeD1Statement:
    def __init__(self, connection: sqlite3.Connection, sql: str, js_rows: bool) -> None:
        self._connection = connection
        self._sql = sql
        self._params: tuple[Any, ...] = ()
        self._js_rows = js_rows

    def bind(self, *params: Any) -> FakeD1Statement:
        if any(p is None for p in params):
            raise TypeError("D1 rejects undefined bindings")
        stmt = FakeD1Statement(self._connection, self._sql, self._js_rows)
        stmt._params = params
        return stmt

    async def all(self) -> Any:
        rows = [dict(r) for r in self._connection.execute(self._sql, self._params).fetchall()]
        return JsD1Result(rows) if self._js_rows else FakeD1Result(rows)


class FakeD1:
    """A stand-in for `env.DB`, backed by the same sqlite connection the tests seeded."""

    def __init__(self, connection: sqlite3.Connection, js_rows: bool = False) -> None:
        self._connection = connection
        self._js_rows = js_rows

    def prepare(self, sql: str) -> FakeD1Statement:
        return FakeD1Statement(self._connection, sql, self._js_rows)


@pytest.fixture(params=[False, True], ids=["dict-rows", "jsproxy-rows"])
def d1(request: pytest.FixtureRequest, seeded: SqliteRepo) -> D1Repo:
    return D1Repo(FakeD1(seeded.connection, js_rows=request.param))


async def test_d1_gantries(d1: D1Repo, seeded: SqliteRepo) -> None:
    assert await d1.gantries() == await seeded.gantries()
    assert await d1.gantry("35") == GANTRIES[0]
    assert await d1.gantry("missing") is None


async def test_d1_active_snapshot(d1: D1Repo, seeded: SqliteRepo) -> None:
    assert await d1.active_snapshot() == await seeded.active_snapshot()


async def test_d1_bands_match_sqlite(d1: D1Repo, seeded: SqliteRepo) -> None:
    expected = await seeded.bands("35", VehicleType.CAR, DayType.WEEKDAY)
    assert await d1.bands("35", VehicleType.CAR, DayType.WEEKDAY) == expected
    assert [b.amount_cents for b in expected] == [300, 200]
    assert await d1.all_bands() == await seeded.all_bands()


async def test_d1_resolves_the_active_snapshot_without_binding_none(d1: D1Repo) -> None:
    # FakeD1Statement raises on a None binding, exactly like D1 does.
    bands = await d1.all_bands()
    assert len(bands) == 4
    snapshot = await d1.active_snapshot()
    assert snapshot is not None
    assert await d1.all_bands(snapshot.id) == bands


async def test_d1_holidays_and_meta(d1: D1Repo, seeded: SqliteRepo) -> None:
    assert await d1.holidays() == await seeded.holidays()
    assert await d1.get_meta("source_url") == "https://onemotoring.lta.gov.sg/"
    assert await d1.get_meta("missing") is None


async def test_d1_empty_database_is_safe(repo: SqliteRepo) -> None:
    d1 = D1Repo(FakeD1(repo.connection))
    assert await d1.gantries() == []
    assert await d1.active_snapshot() is None
    assert await d1.bands("35", VehicleType.CAR, DayType.WEEKDAY) == []
    assert await d1.all_bands() == []
