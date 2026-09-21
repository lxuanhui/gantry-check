"""Cloudflare Python Worker entrypoint (see wrangler.jsonc `main`).

The FastAPI app is served through the Workers ASGI adapter; each request gets a D1Repo over
the `DB` binding exposed on the request scope by the adapter, and — for `POST /estimate` — a
routing engine built from the same `env` (vars + secrets).

`POST /estimate` is the expensive endpoint (a routing-provider call per request), so it is
guarded: first by country (`ALLOWED_COUNTRIES`), then by a per-IP rate limit over the
`ESTIMATE_RATE_LIMIT` binding. This module only runs inside the Worker, so it may import
Pyodide-only modules at the top level, as it already does for `workers`.
"""

import httpx
import js
from fastapi import HTTPException, Request
from pyodide.ffi import to_js
from workers import asgi

from gantry_check.api.app import create_app
from gantry_check.api.guards import country_guard
from gantry_check.config import Settings
from gantry_check.repo.d1 import D1Repo
from gantry_check.routing.base import RoutingEngine, build_engine

ROUTING_TIMEOUT_S = 20.0

RATE_LIMIT_BINDING = "ESTIMATE_RATE_LIMIT"


def _repo(request: Request) -> D1Repo:
    return D1Repo(request.scope["env"].DB)


def _engine(request: Request) -> RoutingEngine:
    # A fresh client per request: the Worker isolate may be torn down between requests, so
    # there is nothing longer-lived to attach a connection pool to. `build_engine` raises
    # RoutingError when no credentials are configured, which the app turns into a 503.
    settings = Settings.from_object(request.scope["env"])
    return build_engine(settings, httpx.AsyncClient(timeout=ROUTING_TIMEOUT_S))


async def _country_gate(request: Request) -> None:
    """Apply `country_guard` with the allow-list from this request's `env`.

    `env` only exists on the request scope, so the allowed set is read per request rather than
    at module import. An empty `ALLOWED_COUNTRIES` means no country gate.
    """
    allowed = Settings.from_object(request.scope["env"]).allowed_countries
    if allowed:
        await country_guard(allowed)(request)


async def _rate_limit(request: Request) -> None:
    """Per-IP rate limit over the Cloudflare Rate Limiting binding (see wrangler.jsonc)."""
    limiter = getattr(request.scope["env"], RATE_LIMIT_BINDING, None)
    if limiter is None:
        # Binding not configured (e.g. a local setup without it): nothing to enforce.
        return
    key = request.headers.get("cf-connecting-ip", "local")
    # `to_js` with `Object.fromEntries`: a plain dict would cross as a JS Map, which the
    # binding rejects.
    outcome = await limiter.limit(to_js({"key": key}, dict_converter=js.Object.fromEntries))
    if not outcome.success:
        raise HTTPException(429, "Too many estimates from your address. Try again in a minute.")


app = create_app(_repo, _engine, estimate_guards=[_country_gate, _rate_limit])
Default = asgi.entrypoint(app)
