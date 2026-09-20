# gantry-check

Estimates which Singapore ERP (Electronic Road Pricing) gantries a driving route crosses at a
given departure time, and the per-gantry and total charge — a sanity check against a Grab fare
that supposedly includes ERP.

**Phase 1 (this repo today):** ERP data ingestion, a rate-lookup API, and routing-engine wrappers
(Google Routes, OneMap). You can already ask "what does gantry X charge a car at 8:10am on a
Tuesday". **Phase 2 (not built):** matching a route's geometry against the gantry list and
producing a full route estimate. `POST /estimate` exists and returns `501 Not Implemented`.

## Background

LTA DataMall's ERP Rates API was removed on 30 September 2024. There is no supported API for ERP
rates any more. Singapore is also partway through the ERP 2.0 transition: physical gantries are
being dismantled in favour of GPS-based charging, but the charging points and rate structure are
unchanged, and LTA still publishes per-location rate tables under the old gantry numbering. This
project scrapes those tables directly.

## Data sources and licences

| Source | URL | Used for | Licence |
| --- | --- | --- | --- |
| Per-gantry HTML rate tables | `https://datamall.lta.gov.sg/mapapp/pages/tables/{gantry}-table-{v}-{d}.html` | Primary rate data (78 gantries x 4 vehicle classes x 4 chargeable day types) | Public, undocumented — could change silently |
| ERP Rates PDF (`ERP Rates.zip` → `1PCU_ERP_Rates_Report.pdf`) | LTA DataMall static datasets | Effective date, and a cross-check of car weekday/Saturday bands against the HTML tables | LTA DataMall Terms of Use (attribution required) |
| OneMotoring KML | `https://onemotoring.lta.gov.sg/mapapp/kml/erp-kml/erp-kml-0.kml` | Gantry coordinates (78 placemarks, gantry number embedded in the placemark name) | Public, undocumented |
| Annex D zone list | DataMall API guide, bundled at `data/static/annex_d_zones.csv` | Zone labels (e.g. `CT4`) for each gantry | LTA DataMall Terms of Use (attribution required) |
| Public holidays | data.gov.sg, MOM's "Singapore Public Holidays" collection (id `691`), current year + next year | Sunday/PH-free days, and picking the eve-of-major-PH rate tables | Singapore Open Data Licence |

The `v` index in the HTML table URL selects vehicle class: `0` car/taxi/LGV, `1` motorcycle, `2`
HGV/small bus, `3` VHGV/big bus. The `d` index selects day type: `0` weekday, `1` Saturday, `2`
eve of a major public holiday falling on a weekday, `3` eve of a major public holiday falling on
a Saturday.

The HTML tables are primary because they're the only ones still live for all vehicle classes and
day types; the PDF only covers cars on weekdays/Saturdays but carries the official effective
date, so `gantry-check refresh` fetches both and fails the refresh if they disagree (see
[Refreshing data](#refreshing-data)).

PCU (passenger car unit) factors, used only as a fallback when a per-vehicle table is missing
from a snapshot: motorcycle 0.5, car 1.0, HGV 1.5, VHGV 2.0. Sundays and public holidays are free
(no ERP charged, no rate table published). Rate bands are half-open: `[start, end)`.

**Caveat:** the list of "major" public holidays used to decide when the eve-of-major-PH tables
apply — New Year's Day, Chinese New Year (first day only), Hari Raya Puasa, Deepavali, Christmas
Day — is an assumption based on which eves LTA has historically surcharged. It has not been
verified against an authoritative LTA source, and MOM's "(Observed)" substitute-holiday rows are
deliberately excluded (the eve belongs to the festival date, not the substitute). See
`src/gantry_check/ingest/holidays.py`.

## Routing

Two routing engines, selected by `ROUTING_ENGINE` (`google` | `onemap`):

- **Google Routes API** (default): traffic-aware when the departure time is in the future. Free
  for 10,000 calls/month, then roughly US$5 per 1,000. Requires a GCP project with billing
  enabled and `GOOGLE_MAPS_API_KEY`.
- **OneMap routing**: free fallback, no traffic awareness. Requires `ONEMAP_EMAIL` /
  `ONEMAP_PASSWORD` (OneMap issues a bearer token per login).

When both Google and OneMap are configured and `ROUTING_ENGINE=google`, Google is tried first and
OneMap is used as a fallback if Google fails (the failure is recorded in the route's `warnings`).
Setting `ROUTING_ENGINE=onemap` prefers OneMap instead, falling back to Google if OneMap creds are
missing.

Gantry crossing times (Phase 2) are estimated as `depart_at + cumulative route duration at that
point`, not a separate traffic model.

## Stack

- Cloudflare Python Worker (FastAPI, served via `workers.asgi`) backed by D1.
- Local development uses SQLite with the same migrations (`src/gantry_check/repo/sqlite.py` vs.
  `repo/d1.py`), so the FastAPI app (`src/gantry_check/api/app.py`) runs unchanged under
  `pytest`/`uvicorn` and inside the Worker.

### Requirements

- Python 3.13 (`.python-version`; `pyproject.toml` allows 3.12+, but pin 3.13 to match ingestion
  and Worker tooling).
- [`uv`](https://docs.astral.sh/uv/) **>= 0.12.3** — `pywrangler` needs a recent uv (`brew upgrade uv` on macOS).
- Node.js + `npx` for `wrangler`.
- A Cloudflare account. **Workers Paid is recommended for the API**: the Free plan's 10ms CPU
  limit is tight for a FastAPI request that hits D1.

## Setup

```sh
uv sync
cp .env.example .env              # CLI / local scripts: GOOGLE_MAPS_API_KEY, ONEMAP_EMAIL,
                                   # ONEMAP_PASSWORD, ROUTING_ENGINE, LOCAL_DB
cp .dev.vars.example .dev.vars    # `wrangler dev` secrets: GOOGLE_MAPS_API_KEY, ONEMAP_EMAIL,
                                   # ONEMAP_PASSWORD
```

Both `.env` and `.dev.vars` are gitignored. Never commit secrets. `ROUTING_ENGINE` for the
deployed Worker is a plain (non-secret) var in `wrangler.jsonc`; production secrets are set with
`wrangler secret put` (see [Deploy](#deploy)).

`uv sync` installs both dependency groups by default (`[tool.uv] default-groups` in
`pyproject.toml`): `ingest` (`pdfplumber`, `pyshp`, `pyproj` — used only by the CLI/refresh
pipeline) and `dev` (`workers-py`, `workers-runtime-sdk`, `pytest`, `ruff`, …). The base runtime
dependencies (`fastapi`, `httpx`, `pydantic`) are kept deliberately minimal and pure-Python
because they're bundled into the Cloudflare Worker.

## Refreshing data

```sh
uv run gantry-check refresh --db data/local.sqlite --out data/snapshots/snapshot.sql
```

Fetches all 78 gantries x 4 vehicle classes x 4 chargeable day types (1,248 paced HTML requests),
the ERP Rates PDF, the OneMotoring KML, and public holidays for the current and next year; parses
and cross-checks them; writes a new active rate snapshot to the SQLite DB at `--db` (older
snapshots are kept, not deleted); and exports an idempotent SQL file at `--out` for loading into
D1.

Flags:

- `--allow-mismatch` — continue even if the HTML tables and the PDF disagree on car
  weekday/Saturday rates (otherwise the refresh aborts).
- `--cache-dir PATH` — cache fetched HTTP responses on disk, so a re-run (e.g. after a
  `--allow-mismatch` decision) doesn't re-fetch everything.
- `--year Y` — fetch public holidays for a specific year instead of the current/next year default.

This is also what `.github/workflows/refresh.yml` runs on a schedule (see
[GitHub Actions](#github-actions)).

## Running the API locally

```sh
npx wrangler d1 migrations apply gantry-check --local
npx wrangler d1 execute gantry-check --local --file data/snapshots/snapshot.sql
uv run pywrangler dev
```

Endpoints:

| Method & path | Notes |
| --- | --- |
| `GET /health` | Service status and the active rate snapshot's id/effective date/fetch time. |
| `GET /gantries` | All gantries: number, name, zone, coordinates, whether a carriageway line is known. |
| `GET /rates/{gantry}?at=<ISO-8601>&vehicle=car` | Charge for one gantry at one instant. Naive `at` values are taken as Singapore time. `vehicle` is `car` \| `motorcycle` \| `hgv` \| `vhgv`. |
| `GET /rates/{gantry}/table?vehicle=car&day_type=weekday` | The full band table for one gantry/vehicle/day type. `day_type` is `weekday` \| `saturday` \| `eve_major_ph_weekday` \| `eve_major_ph_saturday` \| `sunday_ph` (the last always returns an empty `bands` list — no ERP is charged). |
| `POST /estimate` | Body: `{"origin": [lat, lng], "destination": [lat, lng], "depart_at": "<ISO-8601>", "vehicle": "car"}`. Currently returns `501` — Phase 2 route matching isn't built. |

Example:

```sh
curl "http://localhost:8787/rates/35?at=2026-09-21T08:10:00&vehicle=car"
```

```json
{
  "gantry": "35",
  "name": "CTE before Braddell Road",
  "vehicle": "car",
  "at": "2026-09-21T08:10:00+08:00",
  "day_type": "weekday",
  "band": { "start": "08:00", "end": "08:30", "amount_cents": 300, "amount": "$3.00" },
  "amount_cents": 300,
  "amount": "$3.00",
  "effective_from": "2026-09-07"
}
```

## Deploy

```sh
npx wrangler d1 create gantry-check
# paste the returned database_id into wrangler.jsonc (d1_databases[0].database_id)
npx wrangler d1 migrations apply gantry-check --remote
npx wrangler secret put GOOGLE_MAPS_API_KEY
npx wrangler secret put ONEMAP_EMAIL
npx wrangler secret put ONEMAP_PASSWORD
uv run pywrangler deploy
```

Load a rate snapshot into the deployed D1 database the same way as locally, with `--remote`
instead of `--local`:

```sh
npx wrangler d1 execute gantry-check --remote --file data/snapshots/snapshot.sql
```

## GitHub Actions

- **`ci.yml`** — on every push to `main` and every pull request: `ruff check`, `ruff format
  --check`, `pytest`.
- **`refresh.yml`** — weekly, Sunday 20:17 UTC (Monday 04:17 SGT), plus manual dispatch with
  `allow_mismatch` and `push_to_d1` inputs. Runs `gantry-check refresh`, uploads the snapshot SQL
  and SQLite DB as a build artifact (90-day retention), and — on the scheduled run, or on manual
  dispatch with `push_to_d1` — applies migrations and pushes the snapshot to the remote D1
  database. Requires repo secrets `CLOUDFLARE_API_TOKEN` (needs D1 edit permission) and
  `CLOUDFLARE_ACCOUNT_ID`.

## CLI reference

```
gantry-check refresh [--db PATH] [--out PATH] [--allow-mismatch] [--cache-dir PATH] [--year Y]
gantry-check rates GANTRY --at ISO [--vehicle car|motorcycle|hgv|vhgv] [--table] [--db PATH]
gantry-check route --from LAT,LNG --to LAT,LNG [--engine google|onemap] [--depart-at ISO]
```

- `refresh` — run the ingestion pipeline and write a new snapshot; see
  [Refreshing data](#refreshing-data).
- `rates` — look up the charge for one gantry at one instant against a local snapshot
  (`--db`, default `data/local.sqlite`); `--table` prints the full band table instead of a single
  lookup.
- `route` — fetch a route between two coordinates from the chosen routing engine and print its
  distance, duration, and (with `--depart-at`) estimated crossing schedule.

## Development

```sh
uv run pytest
uv run ruff check src tests
uv run ruff format src tests
```

Layout of `src/gantry_check/`:

```
domain/     daytype.py, geo.py, models.py, pricing.py   — pure logic, no I/O, Worker-safe
repo/       base.py, sqlite.py, d1.py                    — storage backends behind one interface
ingest/     sources.py, html_rates.py, pdf_rates.py,      — network + parsing for each upstream
            kml_gantries.py, holidays.py                    source
routing/    base.py, google.py, onemap.py, polyline.py   — routing engine wrappers + geometry
api/        app.py                                       — FastAPI app (create_app)
matching/                                                 — Phase 2, currently empty
cli.py                                                    — gantry-check entry point
config.py                                                 — Settings (env / .env / Worker `env`)
```

`src/entry.py` (outside the package) is the Cloudflare Worker entry point referenced by
`wrangler.jsonc`. Test fixtures — captured HTML tables for a few gantries, the KML file, and a
snapshot PDF — are pinned under `tests/fixtures/` so parser tests don't depend on the network.

## Phase 2 (not built): route matching design

The plan for turning a `Route` (a polyline with cumulative duration at each point, from
`routing/base.py`) into a list of crossed gantries and charges:

1. For each gantry with a `line_wkt` carriageway line, test each route segment for intersection
   with that line, buffered by roughly 8 metres to absorb GPS/geometry noise; for gantries with
   only a KML point (no line), instead test whether any route point passes within about 15 metres.
2. Where a gantry has a known `heading_deg`, also check the route's local bearing against it, so a
   route on the opposite carriageway or a crossing road isn't counted.
3. At each detected crossing, interpolate the crossing time as `depart_at + cumulative_seconds`
   for that point on the route.
4. Classify that instant's day type (`day_type_for`) and vehicle class, then look up the
   applicable rate band and amount from the active snapshot, same as `/rates/{gantry}`.
5. Sum the per-gantry charges into a total, and return both engines' results side by side where
   practical (Google vs. OneMap), since their polylines differ enough to change which gantries are
   crossed.
