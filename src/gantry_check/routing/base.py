"""Routing engine protocol, shared geometry helpers, and engine selection.

Only httpx + stdlib -- must run on CPython and inside a Cloudflare Python Worker (Pyodide).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime
from typing import Protocol

import httpx

from gantry_check.config import Settings
from gantry_check.domain.models import LatLng, Route

_EARTH_RADIUS_M = 6371000.0


class RoutingError(Exception):
    """Raised when a routing engine cannot produce a route."""


class RoutingEngine(Protocol):
    name: str

    async def route(
        self, origin: LatLng, destination: LatLng, depart_at: datetime | None = None
    ) -> Route: ...


def haversine(a: LatLng, b: LatLng) -> float:
    """Great-circle distance between two points, in metres."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def distribute_duration(points: list[LatLng], duration_s: float) -> list[float]:
    """Cumulative seconds at each point, proportional to haversine segment length.

    ``result[0] == 0.0`` and ``result[-1] == duration_s`` (for len(points) >= 2).
    Falls back to an equal split across segments when all points coincide.
    """
    if len(points) < 2:
        return [0.0] * len(points)

    seg_lengths = [haversine(points[i], points[i + 1]) for i in range(len(points) - 1)]
    total = sum(seg_lengths)

    cumulative = [0.0]
    if total <= 0:
        n = len(seg_lengths)
        for _ in range(n):
            cumulative.append(cumulative[-1] + duration_s / n)
        return cumulative

    acc = 0.0
    for seg in seg_lengths:
        acc += seg
        cumulative.append(duration_s * acc / total)
    return cumulative


class FallbackEngine:
    """Tries engines in order; the first success wins, prior failures become warnings."""

    name = "fallback"

    def __init__(self, engines: list[RoutingEngine]) -> None:
        if not engines:
            raise RoutingError("FallbackEngine requires at least one engine")
        self._engines = engines

    async def route(
        self, origin: LatLng, destination: LatLng, depart_at: datetime | None = None
    ) -> Route:
        failures: list[str] = []
        for engine in self._engines:
            try:
                result = await engine.route(origin, destination, depart_at)
            except RoutingError as exc:
                failures.append(f"{engine.name} failed: {exc}")
                continue
            if failures:
                note = "; ".join(failures) + f"; used {engine.name}"
                result = replace(result, warnings=[*result.warnings, note])
            return result
        raise RoutingError("all routing engines failed: " + "; ".join(failures))


def build_engine(settings: Settings, client: httpx.AsyncClient) -> RoutingEngine:
    """Select a routing engine (or a Google->OneMap fallback chain) from settings.

    - routing_engine == "onemap": prefer OneMap; fall back to Google if OneMap
      creds are missing and a Google key is configured.
    - routing_engine == "google" (default) and a Google key exists: fall back
      chain Google -> OneMap when OneMap creds are also present, else bare Google.
    - Google key missing regardless of routing_engine: use OneMap if configured.
    - Nothing configured: raise RoutingError at build time.
    """
    # Imported locally to avoid a circular import (google.py/onemap.py import
    # helpers from this module).
    from gantry_check.routing.google import GoogleRoutesEngine
    from gantry_check.routing.onemap import OneMapEngine

    google: RoutingEngine | None = None
    if settings.google_maps_api_key:
        google = GoogleRoutesEngine(client, settings.google_maps_api_key)

    onemap: RoutingEngine | None = None
    if settings.onemap_email and settings.onemap_password:
        onemap = OneMapEngine(client, settings.onemap_email, settings.onemap_password)

    if settings.routing_engine == "onemap" or google is None:
        if onemap is not None:
            return onemap
        if google is not None:
            return google
        raise RoutingError(
            "no routing engine configured: set GOOGLE_MAPS_API_KEY or ONEMAP_EMAIL/ONEMAP_PASSWORD"
        )

    if onemap is not None:
        return FallbackEngine([google, onemap])
    return google
