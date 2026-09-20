-- Migration number: 0001 	 2026-09-20
-- Applied by `wrangler d1 migrations apply` (D1) and by gantry_check.repo.sqlite (local).

CREATE TABLE IF NOT EXISTS gantry (
    number      TEXT PRIMARY KEY,          -- LTA gantry number as text, e.g. '35'
    name        TEXT NOT NULL,             -- e.g. 'CTE before Braddell Road'
    zone_id     TEXT,                      -- Annex D zone, e.g. 'CT4'
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    line_wkt    TEXT,                      -- optional LINESTRING (lng lat, ...) spanning the carriageway
    heading_deg REAL,                      -- optional traffic heading (0-360); NULL = no direction check
    source      TEXT NOT NULL,             -- e.g. 'onemotoring-kml'
    updated_at  TEXT NOT NULL              -- ISO-8601 UTC
);

CREATE TABLE IF NOT EXISTS rate_snapshot (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_from TEXT NOT NULL,          -- ISO date from the PDF title, e.g. '2026-09-07'
    fetched_at     TEXT NOT NULL,          -- ISO-8601 UTC
    html_sha256    TEXT NOT NULL,          -- sha256 over the concatenated HTML tables
    pdf_sha256     TEXT NOT NULL,
    is_active      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rate_band (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id   INTEGER NOT NULL REFERENCES rate_snapshot(id),
    gantry_number TEXT NOT NULL REFERENCES gantry(number),
    vehicle_type  TEXT NOT NULL,           -- car | motorcycle | hgv | vhgv
    day_type      TEXT NOT NULL,           -- weekday | saturday | eve_major_ph_weekday | eve_major_ph_saturday
    start_min     INTEGER NOT NULL,        -- minutes since midnight, inclusive
    end_min       INTEGER NOT NULL,        -- minutes since midnight, exclusive
    amount_cents  INTEGER NOT NULL,
    UNIQUE (snapshot_id, gantry_number, vehicle_type, day_type, start_min)
);
CREATE INDEX IF NOT EXISTS rate_band_lookup
    ON rate_band (snapshot_id, gantry_number, vehicle_type, day_type);

CREATE TABLE IF NOT EXISTS public_holiday (
    date     TEXT PRIMARY KEY,             -- ISO date
    name     TEXT NOT NULL,
    is_major INTEGER NOT NULL DEFAULT 0    -- eve of these uses the 'eve_major_ph_*' tables
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
