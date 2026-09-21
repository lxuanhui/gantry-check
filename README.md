# gantry-check

Estimates which Singapore ERP (Electronic Road Pricing) gantries a driving route crosses at a
given departure time, and the per-gantry and total charge — a sanity check against a Grab fare
that supposedly includes ERP.

**Phase 1:** ERP data ingestion, a rate-lookup API, and routing-engine wrappers (Google Routes,
OneMap). You can already ask "what does gantry X charge a car at 8:10am on a Tuesday". **Phase 2
(this repo today):** matching a route's geometry against the gantry list and producing a full
route estimate. `POST /estimate` is live — it takes an origin, destination, and departure time,
and returns the crossed gantries, their crossing times, and the total charge. See
[How route matching works](#how-route-matching-works) and its
[data quality caveats](#data-quality-caveats).

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
| LTA Gantry (GEOJSON) | data.gov.sg dataset `d_753090823cc9920ac41efaa6530c5893` | Gantry *lines* across the carriageway (106 WGS84 LineStrings), joined geometrically to the KML points so route matching can tell one carriageway from the other | Singapore Open Data Licence |
| Gantry geometry overrides | Hand-curated, bundled at `data/static/gantry_overrides.csv` | Per-gantry corrections to the geometric line join, applied during `refresh` | This repository (see [Data quality caveats](#data-quality-caveats)) |

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

Gantry crossing times are estimated as `depart_at + cumulative route duration at that point`
(`matching/estimate.py`), not a separate traffic model — see
[How route matching works](#how-route-matching-works).

## Stack

- Cloudflare Python Worker (FastAPI, served via `workers.asgi`) backed by D1.
- A prerendered SvelteKit UI (`web/`) served from the same Worker as static assets; see
  [Web UI](#web-ui).
- Local development uses SQLite with the same migrations (`src/gantry_check/repo/sqlite.py` vs.
  `repo/d1.py`), so the FastAPI app (`src/gantry_check/api/app.py`) runs unchanged under
  `pytest`/`uvicorn` and inside the Worker.

### Requirements

- Python 3.13 (`.python-version`; `pyproject.toml` allows 3.12+, but pin 3.13 to match ingestion
  and Worker tooling).
- [`uv`](https://docs.astral.sh/uv/) **>= 0.12.3** — `pywrangler` needs a recent uv (`brew upgrade uv` on macOS).
- Node.js **22** + `npx` for `wrangler` and the web UI build (`web/.node-version`; the SvelteKit
  toolchain refuses Node 23).
- A Cloudflare account. **Workers Paid is recommended for the API**: the Free plan's 10ms CPU
  limit is tight for a FastAPI request that hits D1.

## Setup

```sh
uv sync
cp .env.example .env              # CLI / local scripts: GOOGLE_MAPS_API_KEY, ONEMAP_EMAIL,
                                   # ONEMAP_PASSWORD, ROUTING_ENGINE, LOCAL_DB
cp .dev.vars.example .dev.vars    # `wrangler dev` secrets: GOOGLE_MAPS_API_KEY, ONEMAP_EMAIL,
                                   # ONEMAP_PASSWORD
cp web/.env.example web/.env      # web UI build: PUBLIC_GOOGLE_MAPS_BROWSER_KEY
```

`web/.env` is optional: SvelteKit reads `PUBLIC_GOOGLE_MAPS_BROWSER_KEY` through
`$env/dynamic/public` at build time, so a missing file or an empty value both build fine and
the map area then shows "Map unavailable". It is the browser key for the Maps JavaScript API,
not the server-side `GOOGLE_MAPS_API_KEY`: keep it a separate key, restrict it by HTTP referrer
to your origins (plus `http://localhost:5173/*` and `http://localhost:8787/*` for local dev;
Google rejects `http://localhost:*/*`) and to the Maps JavaScript API only. It ships inside the
prerendered bundle (`_app/env.js`), so it is public by design.

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
D1. It also pulls LTA's gantry GeoJSON from data.gov.sg and attaches each gantry's line across
the carriageway (65 of the 78 gantries match a line within 60 m; the rest keep a bare point).

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
| `POST /estimate` | Body: `{"origin": [lat, lng], "destination": [lat, lng], "depart_at": "<ISO-8601>", "vehicle": "car"}`. `depart_at` is optional (default: now); naive values are taken as Singapore time. `vehicle` is `car` \| `motorcycle` \| `hgv` \| `vhgv`. An `engine` field is accepted but ignored — the routing engine is fixed by server config. |

Examples:

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

```sh
curl -X POST "http://localhost:8787/estimate" \
  -H "content-type: application/json" \
  -d '{"origin": [1.3691, 103.8454], "destination": [1.2840, 103.8515],
       "depart_at": "2026-09-22T08:00:00", "vehicle": "car"}'
```

```json
{
  "engine": "google",
  "summary": "CTE",
  "distance_m": 14200.0,
  "duration_s": 1320.0,
  "depart_at": "2026-09-22T08:00:00+08:00",
  "vehicle": "car",
  "charges": [
    {
      "gantry": "35",
      "name": "CTE before Braddell Road",
      "zone_id": "CT4",
      "crossed_at": "2026-09-22T08:11:20+08:00",
      "day_type": "weekday",
      "band": { "start": "08:00", "end": "08:30", "amount_cents": 300, "amount": "$3.00" },
      "amount_cents": 300,
      "amount": "$3.00",
      "method": "line"
    }
  ],
  "total_cents": 300,
  "total": "$3.00",
  "warnings": []
}
```

Errors: `503` when no routing engine is configured (neither Google nor OneMap credentials are
set), `502` when the routing engine call itself fails (bad API key, upstream error), `422` for a
malformed request body (standard FastAPI/pydantic validation).

### Singapore-only restrictions

`POST /estimate` is the only endpoint restricted; `/health`, `/gantries`, `/rates/...`, `/docs`,
and the web page itself stay reachable from anywhere.

- **Bounding box (422).** Origin and destination must both fall within a rough Singapore box (lat
  1.20–1.47, lng 103.60–104.05), or the request is rejected with `422` and a detail like
  `"outside Singapore: (3.14, 101.69)"`. The box is deliberately coarse rather than border-accurate
  — it exists to stop the API being used as a free world-routing proxy, not to trace the border, so
  it also covers Johor Bahru city centre (~2 km north of Woodlands Checkpoint).
- **Country gate (403).** When Cloudflare's `cf-ipcountry` header is present and its value isn't in
  the Worker var `ALLOWED_COUNTRIES` (comma-separated, set to `SG` in `wrangler.jsonc`), the request
  is refused with `403` and detail `"Estimates are only available from Singapore."`. Set
  `ALLOWED_COUNTRIES` to a longer comma-separated list to widen it, or to an empty string to disable
  the gate entirely. Local dev (`pywrangler dev`, `uvicorn`, tests) never sees `cf-ipcountry`, so the
  gate is inactive there regardless of the setting.
- **Rate limit (429).** Requests are capped per client IP by the `ESTIMATE_RATE_LIMIT` Cloudflare
  Rate Limiting binding under `ratelimits` in `wrangler.jsonc`: 20 requests per 60 seconds, `429`
  with detail `"Too many estimates from your address. Try again in a minute."` when exceeded. Change
  the numbers there (`period` must be `10` or `60`); if the binding is absent the limit is skipped.

## Web UI

`web/` is a SvelteKit (Svelte 5, TypeScript, `adapter-static`) single page that geocodes both
ends of the trip with OneMap's search API directly from the browser (no key needed), calls
`POST /estimate`, and shows the total, the per-gantry breakdown (proximity-only matches are
flagged "direction unverified") and a Google map of the route with the charged gantries.
Departure defaults to the current Singapore time and is sent as a naive local timestamp.

```sh
uv run pywrangler dev            # terminal 1: the API on http://localhost:8787 (see "Running the API locally")
cd web && npm ci && npm run dev  # terminal 2: http://localhost:5173, proxies the API paths to 127.0.0.1:8787
npm run check                    # svelte-check
npm run build                    # prerenders into web/build
```

The build output is what `wrangler.jsonc` points `assets.directory` at, so `web/build` must
exist before `pywrangler dev` or `pywrangler deploy`. Paths with no matching asset fall through to
the Python Worker, which is how the API keeps working on the same origin; `/` is served by the
prerendered `index.html` and shadows FastAPI's fallback landing route. If you add another API
link to the footer, add its path to `apiPaths` in `web/vite.config.ts`, or the prerender crawl
fails.

The map is the Google Maps JavaScript API, loaded in the browser only (`onMount` in
`web/src/lib/RouteMap.svelte`, via `@googlemaps/js-api-loader`) so prerendering never touches
it. Google Maps Platform requires a Google-routed path to be drawn on a Google map, which is
why the route polyline is no longer shown on third-party tiles. The browser key comes from
`PUBLIC_GOOGLE_MAPS_BROWSER_KEY` (see [Setup](#setup)); with no key, or if the API fails to
load, the map area shows a muted "Map unavailable" note and everything else keeps working.

## Deploy

`main` is production. Every push to `main` that touches the Worker, its config or the schema
runs `.github/workflows/deploy.yml`, which builds the web UI, applies D1 migrations, deploys the
Worker with `pywrangler deploy`, mirrors the routing secrets into the Worker, and smoke-tests
`/health` and `/`.
Work on a branch, open a pull request (CI runs `ruff` and `pytest`), merge to deploy.

One-time setup:

1. Create the database and record its id in `wrangler.jsonc` (`d1_databases[0].database_id`):

   ```sh
   npx wrangler d1 create gantry-check
   ```

2. Add repository secrets (Settings → Secrets and variables → Actions):

   | Secret | Used by | Notes |
   |---|---|---|
   | `CLOUDFLARE_API_TOKEN` | deploy, refresh | Workers Scripts edit + D1 edit + Account Settings read |
   | `CLOUDFLARE_ACCOUNT_ID` | deploy, refresh | dashboard sidebar |
   | `GOOGLE_MAPS_API_KEY` | deploy (mirrored into the Worker) | Routes API enabled |
   | `ONEMAP_EMAIL`, `ONEMAP_PASSWORD` | deploy (mirrored into the Worker) | OneMap account |

   With `gh`: `gh secret set NAME` reads the value from stdin.

   And one repository **variable** (Settings → Secrets and variables → Actions → Variables):

   | Variable | Used by | Notes |
   |---|---|---|
   | `GOOGLE_MAPS_BROWSER_KEY` | ci, deploy (baked into the web build as `PUBLIC_GOOGLE_MAPS_BROWSER_KEY`) | Maps JavaScript API enabled, restricted by HTTP referrer to **every** origin the site answers on: `https://erp.xuanhuilee.com/*` (the custom domain) and the `*.workers.dev` address. A missing origin fails with `RefererNotAllowedMapError` on that host only |

   It is a variable, not a secret, because a browser key is public by design — it ships in the
   prerendered bundle, and the HTTP-referrer restriction is what protects it. With `gh`:
   `gh variable set GOOGLE_MAPS_BROWSER_KEY`.

3. Push to `main`, then run the **Refresh ERP data** workflow once (manual dispatch, `push_to_d1`
   on) to load the first rate snapshot into D1.

A manual deploy from a laptop is still possible (`uv run pywrangler deploy` with
`CLOUDFLARE_API_TOKEN` exported) but is not the normal path.

## GitHub Actions

- **`ci.yml`** — on every push to `main` and every pull request: `ruff check`, `ruff format
  --check`, `pytest`, plus a `web` job (`npm ci`, `npm run check`, `npm run build`).
- **`deploy.yml`** — on push to `main` (paths-filtered, including `web/**`) and manual dispatch:
  web UI build, D1 migrations, `pywrangler deploy`, `wrangler secret bulk` from the GitHub secrets
  above, `/health` and `/` smoke tests. Uses the `production` environment, so branch protection or required reviewers can be
  attached to it.
- **`refresh.yml`** — weekly, Sunday 20:17 UTC (Monday 04:17 SGT), plus manual dispatch with
  `allow_mismatch` and `push_to_d1` inputs. Runs `gantry-check refresh`, uploads the snapshot SQL
  and SQLite DB as a build artifact (90-day retention), and — on the scheduled run, or on manual
  dispatch with `push_to_d1` — applies migrations and pushes the snapshot to the remote D1
  database.

## CLI reference

```
gantry-check refresh [--db PATH] [--out PATH] [--allow-mismatch] [--cache-dir PATH] [--year Y]
                      [--no-lines]
gantry-check rates GANTRY --at ISO [--vehicle car|motorcycle|hgv|vhgv] [--table] [--db PATH]
gantry-check route --from LAT,LNG --to LAT,LNG [--engine google|onemap] [--depart-at ISO]
gantry-check estimate --from LAT,LNG --to LAT,LNG [--depart-at ISO]
                       [--vehicle car|motorcycle|hgv|vhgv] [--engine google|onemap] [--db PATH]
```

- `refresh` — run the ingestion pipeline and write a new snapshot; see
  [Refreshing data](#refreshing-data). `--no-lines` skips deriving per-carriageway gantry lines
  from the data.gov.sg GeoJSON, so every gantry falls back to point matching.
- `rates` — look up the charge for one gantry at one instant against a local snapshot
  (`--db`, default `data/local.sqlite`); `--table` prints the full band table instead of a single
  lookup.
- `route` — fetch a route between two coordinates from the chosen routing engine and print its
  distance, duration, and (with `--depart-at`) estimated crossing schedule.
- `estimate` — price the gantries a route crosses against a local snapshot (`--db`, default
  `data/local.sqlite`); prints each crossing (time, gantry, day type, band, amount, match method)
  and the total. `--depart-at` defaults to now (naive values are SGT).

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
            kml_gantries.py, holidays.py, datagov.py,        source (datagov.py: shared
            gantry_lines.py                                  data.gov.sg fetch/poll client)
routing/    base.py, google.py, onemap.py, polyline.py   — routing engine wrappers + geometry
api/        app.py                                       — FastAPI app (create_app)
matching/   crossings.py, estimate.py                    — route <-> gantry matching and pricing
cli.py                                                    — gantry-check entry point
config.py                                                 — Settings (env / .env / Worker `env`)
```

`src/entry.py` (outside the package) is the Cloudflare Worker entry point referenced by
`wrangler.jsonc`. `web/src/` holds the SvelteKit UI (`routes/+page.svelte`, `lib/PlaceInput.svelte`,
`lib/RouteMap.svelte`, `lib/api.ts`). Test fixtures — captured HTML tables for a few gantries, the KML file, and a
snapshot PDF — are pinned under `tests/fixtures/` so parser tests don't depend on the network.

## How route matching works

`POST /estimate` (and `gantry-check estimate`) fetches a `Route` from the configured routing
engine — a polyline with cumulative duration in seconds at each point, from `routing/base.py` —
and turns it into a list of crossed gantries and charges (`matching/crossings.py`,
`matching/estimate.py`):

1. For each gantry with a `line_wkt` carriageway line (attached during `refresh` by
   `ingest/gantry_lines.py`), the **line** method is used: a route segment matches when it
   intersects that line, or passes within 8 metres of it (`DEFAULT_LINE_BUFFER_M`). This is
   direction-aware in practice — a route on the correct carriageway doesn't come near the line for
   the opposite one.
2. For a gantry with only a KML point (no line), the **point** method is used instead: a route
   segment matches when it passes within 15 metres of the point (`DEFAULT_POINT_RADIUS_M`). This
   cannot tell which carriageway the route is on, so every point-matched crossing adds a
   `"matched by proximity only"` warning to the response.
3. Where a gantry's `heading_deg` is known, the route's local bearing at the matching segment must
   also agree with it, so a route on the opposite carriageway or a crossing road isn't counted. No
   gantry has `heading_deg` set today — a line across a carriageway is itself direction-ambiguous
   by 180°, so nothing currently populates it.
4. The crossing time is `depart_at + cumulative_seconds` interpolated to the matched point on the
   route. That instant's day type (`day_type_for`) and the requested vehicle class are then used to
   look up the applicable rate band and amount from the active snapshot, same as `/rates/{gantry}`.
5. Each gantry is counted at most once, at its first crossing — a route that loops past the same
   gantry twice is billed once, matching how a real journey is charged. Per-gantry charges are
   summed into `total_cents`.

### Data quality caveats

- Gantry line geometry comes from data.gov.sg's "LTA Gantry (GEOJSON)" dataset (see
  [Data sources and licences](#data-sources-and-licences)). It has no usable `type` field and
  unreliable gantry numbers, so lines are joined to the OneMotoring KML points purely by geometry
  — the nearest line within 60 m — with the file's own gantry number used only to break ties
  between two nearby carriageways. As of the refresh on 21 September 2026, 68 of the 78 gantries
  have a line; the rest fall back to point matching.
- Point-matched today (direction unverified): **28, 59** (just past the 60 m join radius); **36,
  38, 39, 65, 91, 93** (the OneMotoring point sits 100–145 m from the nearest line); **54** (no
  line in the dataset carries its number at all); **71** (Woodsville Tunnel — no surface
  structure exists in the dataset to give it a line).
- Gantries **31** and **68** (CTE after Braddell Road, and its exit slip road to PIE (Changi) /
  Serangoon Road) share one physical gantry structure. Both lines are set by an override rather
  than by the join, and both are checked against satellite imagery; see below for how the
  structure was split between them. **35**, **46** and **67** also have override lines checked
  against imagery; see below.
- Joined at the edge of the radius, unverified: gantries **20** (Havelock Road/CTE Exit, 57 m) and
  **34** (CTE from Balestier Road, 53 m). Both are inside the 60 m radius only just, so the line
  each one picked up may belong to a neighbouring structure.
- May have joined both carriageways, unverified: gantries **50** and **55** each joined two
  parallel lines, which may include the line for the opposite carriageway rather than just
  their own.
- Crossing times are estimates — `depart_at` plus the route's cumulative duration to that point,
  not a live read of conditions at the moment of crossing. A crossing estimated within a minute or
  two of a rate band boundary can land on either side of it.

**Manual overrides.** `data/static/gantry_overrides.csv` (`number,line_wkt,heading_deg,note`) is
applied by `refresh` immediately after the geometric join, for the cases where that join is known
to be wrong. A row *replaces* the gantry's `line_wkt` and `heading_deg` with its own values, and an
empty cell means `NULL` — so an empty `line_wkt` clears a bad auto-join and drops the gantry back to
point matching. An override naming a gantry that is no longer in the KML logs a warning and is
skipped, so a stale row cannot fail a scheduled refresh. The numbers applied are recorded in the
`gantry_overrides` meta key.

Five rows are seeded, all on the CTE around the Braddell Road / PIE interchange. Four of them (31,
46, 67, 68) fix the interchange itself, where four OneMotoring points sit within 40 m of each
other while the real gantries are spread over 200 m, and all three lines there are unnumbered; the
fifth (35) trims a line about 1.5 km north, before Braddell Road:

- **31** (CTE after Braddell Road) — given the western part of the unnumbered 35 m line (uid 646),
  `LINESTRING(103.862260 1.333367, 103.862398 1.333372)`, the southbound CTE mainline.
- **35** (CTE before Braddell Road) — kept its auto-joined 31 m line (uid 757), with the western
  7 m removed: `LINESTRING(103.859314 1.346565, 103.859519 1.346641)`. Imagery shows the structure
  spans only the southbound carriageway, but the untrimmed line started at the median, and OneMap
  draws northbound routes 3 m past that end — inside the 8 m line buffer — wrongly charging
  northbound trips $3 at morning peak; Google's northbound geometry already passed 8 m clear of
  the untrimmed end. After the trim, northbound routes on both engines pass 10+ m from the line's
  end, and southbound routes still cross it.
- **46** (CTE Northbound after PIE) — given the unnumbered 22 m line (uid 627),
  `LINESTRING(103.862080 1.332735, 103.862281 1.332751)`, which imagery shows as a structure
  across the northbound CTE mainline under the PIE loop, matching the gantry's name. It was
  previously point-matched; no line in the dataset carried its number.
- **67** (PIE to CTE Northbound before Braddell Road) — given the unnumbered 19 m line (uid 645),
  `LINESTRING(103.861925 1.333120, 103.861782 1.333189)`, which imagery shows as a structure
  across the PIE-to-CTE-northbound slip road, west of the mainline. The auto-join had instead
  given it the same unnumbered 35 m line as 31 and 68 (uid 646) — the *southbound* mainline/exit
  structure — which only southbound routes cross on either engine; a northbound slip-road gantry
  cannot sit on it. Before this override, a northbound mainline trip on OneMap point-matched both
  46 and 67 (32 m apart) and would have been charged $8 instead of $4 at evening peak.
- **68** (CTE exit slip road to PIE (Changi) / Serangoon Road) — given the eastern part of the
  35 m line (uid 646), `LINESTRING(103.862461 1.333374, 103.862578 1.333378)`, the slip road past
  the gore.

Satellite imagery shows one physical gantry structure spanning both the southbound CTE mainline
and the exit slip road beside it, at the latitude of this unnumbered 35 m line: the mainline is
the western ~15 m of the structure, the slip road the eastern ~13 m past the gore. Splitting the
line there, with a ~7 m gap at the gore between the two halves, lets each carriageway match only
its own gantry: on both routing engines, southbound mainline routes cross the 31 line 10-11 m from
its west end (12+ m from 68's line) and exit routes cross the 68 line 4-8 m from its west end
(11+ m from 31's line) — each route's crossing point sits well outside the 8 m line-matching
buffer of the other carriageway's line, so neither can be caught by the wrong one.

To add an override for another point-matched gantry (**54**, for example), draw the gantry's span
across the carriageway on imagery, read off the two end points, and add a row with
`LINESTRING(lng lat, lng lat)` — longitude first, latitude second, both in WGS84 decimal degrees.
Leave `heading_deg` empty unless you know the traffic direction: a line across a carriageway is
itself 180°-ambiguous. Always fill in `note` with the evidence.
