"""Request guards for the expensive endpoints.

A guard is an async callable that inspects the request and either returns (allow) or raises
``fastapi.HTTPException`` (refuse). They are mounted as route dependencies by ``create_app``,
so FastAPI resolves them *before* the request body is validated: a refused request never pays
for parsing, routing or a rate-limiter round trip.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request

Guard = Callable[[Request], Awaitable[None]]

#: Set by Cloudflare on every request that reaches a Worker, from the connecting IP's
#: geolocation. Clients cannot spoof it: Cloudflare overwrites whatever the client sent.
COUNTRY_HEADER = "cf-ipcountry"


def country_guard(allowed: frozenset[str]) -> Guard:
    """Refuse requests whose Cloudflare-reported country is not in ``allowed``.

    ``cf-ipcountry`` only exists behind Cloudflare. Off the edge -- local dev, tests, a plain
    uvicorn deployment -- it is absent, and the guard allows the request: treating "no header"
    as "refuse" would make the API unusable anywhere but production, and there is no country to
    check in the first place. The gate is a cost control on the edge deployment, not an
    authentication boundary.
    """

    async def guard(request: Request) -> None:
        country = request.headers.get(COUNTRY_HEADER)
        if country is None:
            return
        if country.upper() not in allowed:
            raise HTTPException(403, "Estimates are only available from Singapore.")

    return guard
