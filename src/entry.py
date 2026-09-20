"""Cloudflare Python Worker entrypoint (see wrangler.jsonc `main`).

The FastAPI app is served through the Workers ASGI adapter; each request gets a D1Repo over
the `DB` binding exposed on the request scope by the adapter, and — for `POST /estimate` — a
routing engine built from the same `env` (vars + secrets).
"""

import httpx
from fastapi import Request
from workers import asgi

from gantry_check.api.app import create_app
from gantry_check.config import Settings
from gantry_check.repo.d1 import D1Repo
from gantry_check.routing.base import RoutingEngine, build_engine

ROUTING_TIMEOUT_S = 20.0


def _repo(request: Request) -> D1Repo:
    return D1Repo(request.scope["env"].DB)


def _engine(request: Request) -> RoutingEngine:
    # A fresh client per request: the Worker isolate may be torn down between requests, so
    # there is nothing longer-lived to attach a connection pool to. `build_engine` raises
    # RoutingError when no credentials are configured, which the app turns into a 503.
    settings = Settings.from_object(request.scope["env"])
    return build_engine(settings, httpx.AsyncClient(timeout=ROUTING_TIMEOUT_S))


app = create_app(_repo, _engine)
Default = asgi.entrypoint(app)
