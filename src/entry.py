"""Cloudflare Python Worker entrypoint (see wrangler.jsonc `main`).

The FastAPI app is served through the Workers ASGI adapter; each request gets a D1Repo over
the `DB` binding exposed on the request scope by the adapter.
"""

from fastapi import Request
from workers import asgi

from gantry_check.api.app import create_app
from gantry_check.repo.d1 import D1Repo


def _repo(request: Request) -> D1Repo:
    return D1Repo(request.scope["env"].DB)


app = create_app(_repo)
Default = asgi.entrypoint(app)
