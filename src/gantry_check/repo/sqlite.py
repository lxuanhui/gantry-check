"""Writable repository backed by stdlib sqlite3 -- used locally, by the ingest CLI and by tests.

The query strings and row mappers are shared with the D1 repo; they live in
`gantry_check.repo.d1` so that the Worker never has to import `sqlite3`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gantry_check.domain.models import (
    DayType,
    Gantry,
    PublicHoliday,
    RateBand,
    RateSnapshot,
    VehicleType,
)
from gantry_check.repo.d1 import (
    SNAPSHOT_COLUMNS,
    SQL_ACTIVE_SNAPSHOT,
    SQL_ACTIVE_SNAPSHOT_ID,
    SQL_ALL_BANDS,
    SQL_BANDS,
    SQL_GANTRIES,
    SQL_GANTRY,
    SQL_HOLIDAYS,
    SQL_META,
    row_to_band,
    row_to_gantry,
    row_to_holiday,
    row_to_snapshot,
)

#: Repository root -- src/gantry_check/repo/sqlite.py -> parents[3].
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

SQL_UPSERT_GANTRY = """
INSERT INTO gantry (number, name, zone_id, lat, lng, line_wkt, heading_deg, source, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(number) DO UPDATE SET
    name = excluded.name,
    zone_id = excluded.zone_id,
    lat = excluded.lat,
    lng = excluded.lng,
    line_wkt = excluded.line_wkt,
    heading_deg = excluded.heading_deg,
    source = excluded.source,
    updated_at = excluded.updated_at
"""

SQL_INSERT_SNAPSHOT = """
INSERT INTO rate_snapshot (effective_from, fetched_at, html_sha256, pdf_sha256, is_active)
VALUES (?, ?, ?, ?, 0)
"""

SQL_INSERT_BAND = """
INSERT INTO rate_band
    (snapshot_id, gantry_number, vehicle_type, day_type, start_min, end_min, amount_cents)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

SQL_SET_ACTIVE = "UPDATE rate_snapshot SET is_active = (id = ?)"

SQL_SNAPSHOT_BY_ID = f"SELECT {SNAPSHOT_COLUMNS} FROM rate_snapshot WHERE id = ?"

SQL_UPSERT_META = """
INSERT INTO meta (key, value) VALUES (?, ?)
ON CONFLICT(key) DO UPDATE SET value = excluded.value
"""


def _utc_iso(at: datetime | None = None) -> str:
    """ISO-8601 UTC. A naive datetime is assumed to already be UTC."""
    moment = at or datetime.now(tz=UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def sql_literal(value: Any) -> str:
    """Render a Python value as a SQL literal (single quotes doubled)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def _values(*items: Any) -> str:
    return "(" + ", ".join(sql_literal(i) for i in items) + ")"


class SqliteRepo:
    """`WritableRepo` over a local SQLite file (or `:memory:`)."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        migrations_dir: str | Path | None = None,
    ) -> None:
        self.path = str(path)
        self.migrations_dir = Path(migrations_dir) if migrations_dir else DEFAULT_MIGRATIONS_DIR
        # check_same_thread=False: the FastAPI TestClient and uvicorn call us from worker threads.
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        # Must run outside a transaction, so: immediately after connecting.
        self.connection.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> SqliteRepo:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------- internals

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        cursor = self.connection.cursor()
        cursor.execute("BEGIN")
        try:
            yield cursor
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def _query(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        return self.connection.execute(sql, params).fetchall()

    def _active_snapshot_id(self, snapshot_id: int | None) -> int | None:
        if snapshot_id is not None:
            return snapshot_id
        rows = self._query(SQL_ACTIVE_SNAPSHOT_ID)
        return int(rows[0]["id"]) if rows else None

    # ------------------------------------------------------------------- writes

    async def apply_migrations(self) -> None:
        """Run every `migrations/*.sql` in filename order. Migrations are idempotent."""
        files = sorted(self.migrations_dir.glob("*.sql"))
        if not files:
            raise FileNotFoundError(f"no migrations found in {self.migrations_dir}")
        for path in files:
            self.connection.executescript(path.read_text(encoding="utf-8"))

    async def upsert_gantries(self, gantries: list[Gantry]) -> None:
        now = _utc_iso()
        rows = [
            (
                g.number,
                g.name,
                g.zone_id,
                float(g.lat),
                float(g.lng),
                g.line_wkt,
                g.heading_deg,
                g.source,
                now,
            )
            for g in gantries
        ]
        with self._transaction() as cursor:
            cursor.executemany(SQL_UPSERT_GANTRY, rows)

    async def write_snapshot(
        self, snapshot: RateSnapshot, bands: list[RateBand], activate: bool = True
    ) -> int:
        with self._transaction() as cursor:
            cursor.execute(
                SQL_INSERT_SNAPSHOT,
                (
                    snapshot.effective_from.isoformat(),
                    _utc_iso(snapshot.fetched_at),
                    snapshot.html_sha256,
                    snapshot.pdf_sha256,
                ),
            )
            snapshot_id = int(cursor.lastrowid or 0)
            cursor.executemany(
                SQL_INSERT_BAND,
                [
                    (
                        snapshot_id,
                        b.gantry_number,
                        b.vehicle_type.value,
                        b.day_type.value,
                        int(b.start_min),
                        int(b.end_min),
                        int(b.amount_cents),
                    )
                    for b in bands
                ],
            )
            if activate:
                cursor.execute(SQL_SET_ACTIVE, (snapshot_id,))
        return snapshot_id

    async def replace_holidays(self, holidays: list[PublicHoliday]) -> None:
        with self._transaction() as cursor:
            cursor.execute("DELETE FROM public_holiday")
            cursor.executemany(
                "INSERT INTO public_holiday (date, name, is_major) VALUES (?, ?, ?)",
                [(h.date.isoformat(), h.name, 1 if h.is_major else 0) for h in holidays],
            )

    async def set_meta(self, key: str, value: str) -> None:
        with self._transaction() as cursor:
            cursor.execute(SQL_UPSERT_META, (key, value))

    # ------------------------------------------------------------------- reads

    async def gantries(self) -> list[Gantry]:
        return [row_to_gantry(r) for r in self._query(SQL_GANTRIES)]

    async def gantry(self, number: str) -> Gantry | None:
        rows = self._query(SQL_GANTRY, number)
        return row_to_gantry(rows[0]) if rows else None

    async def active_snapshot(self) -> RateSnapshot | None:
        rows = self._query(SQL_ACTIVE_SNAPSHOT)
        return row_to_snapshot(rows[0]) if rows else None

    async def bands(
        self,
        gantry_number: str,
        vehicle_type: VehicleType,
        day_type: DayType,
        snapshot_id: int | None = None,
    ) -> list[RateBand]:
        sid = self._active_snapshot_id(snapshot_id)
        if sid is None:
            return []
        rows = self._query(SQL_BANDS, sid, gantry_number, vehicle_type.value, day_type.value)
        return [row_to_band(r) for r in rows]

    async def all_bands(self, snapshot_id: int | None = None) -> list[RateBand]:
        sid = self._active_snapshot_id(snapshot_id)
        if sid is None:
            return []
        return [row_to_band(r) for r in self._query(SQL_ALL_BANDS, sid)]

    async def holidays(self) -> list[PublicHoliday]:
        return [row_to_holiday(r) for r in self._query(SQL_HOLIDAYS)]

    async def get_meta(self, key: str) -> str | None:
        rows = self._query(SQL_META, key)
        return str(rows[0]["value"]) if rows else None

    # ------------------------------------------------------------------- export

    def export_sql(self, snapshot_id: int | None = None) -> str:
        """Re-runnable SQL for `wrangler d1 execute --file`, carrying one snapshot.

        Emits gantries before the snapshot before its bands, so the export loads cleanly
        with foreign keys enforced. No BEGIN/COMMIT: D1 does not accept explicit
        transactions in a script.
        """
        sid = self._active_snapshot_id(snapshot_id)
        if sid is None:
            raise ValueError("no snapshot to export (none given and none active)")
        snapshot = self._query(SQL_SNAPSHOT_BY_ID, sid)
        if not snapshot:
            raise ValueError(f"snapshot {sid} not found")
        snap = snapshot[0]

        lines: list[str] = [
            "-- gantry-check export",
            f"-- snapshot {sid} effective_from={snap['effective_from']}",
        ]

        lines.append("")
        for g in self._query(
            "SELECT number, name, zone_id, lat, lng, line_wkt, heading_deg, source, updated_at"
            " FROM gantry ORDER BY number"
        ):
            lines.append(
                "INSERT OR REPLACE INTO gantry"
                " (number, name, zone_id, lat, lng, line_wkt, heading_deg, source, updated_at)"
                f" VALUES {_values(*[g[k] for k in g.keys()])};"
            )

        lines.append("")
        for h in self._query(SQL_HOLIDAYS):
            lines.append(
                "INSERT OR REPLACE INTO public_holiday (date, name, is_major)"
                f" VALUES {_values(h['date'], h['name'], int(h['is_major']))};"
            )

        lines.append("")
        for m in self._query("SELECT key, value FROM meta ORDER BY key"):
            lines.append(
                f"INSERT OR REPLACE INTO meta (key, value) VALUES {_values(m['key'], m['value'])};"
            )

        lines.append("")
        lines.append(f"DELETE FROM rate_band WHERE snapshot_id = {sid};")
        snapshot_values = _values(
            sid,
            snap["effective_from"],
            snap["fetched_at"],
            snap["html_sha256"],
            snap["pdf_sha256"],
            int(snap["is_active"]),
        )
        lines.append(
            "INSERT OR REPLACE INTO rate_snapshot"
            " (id, effective_from, fetched_at, html_sha256, pdf_sha256, is_active)"
            f" VALUES {snapshot_values};"
        )

        lines.append("")
        for b in self._query(SQL_ALL_BANDS, sid):
            band_values = _values(
                sid,
                b["gantry_number"],
                b["vehicle_type"],
                b["day_type"],
                int(b["start_min"]),
                int(b["end_min"]),
                int(b["amount_cents"]),
            )
            lines.append(
                "INSERT INTO rate_band"
                " (snapshot_id, gantry_number, vehicle_type, day_type,"
                " start_min, end_min, amount_cents)"
                f" VALUES {band_values};"
            )

        lines.append("")
        lines.append(f"UPDATE rate_snapshot SET is_active = (id = {sid});")
        lines.append("")
        return "\n".join(lines)
