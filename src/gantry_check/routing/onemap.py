"""OneMap (Singapore Land Authority) routing engine."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Protocol

import httpx

from gantry_check.domain.geo import decode_polyline
from gantry_check.domain.models import LatLng, Route
from gantry_check.routing.base import RoutingError, distribute_duration, haversine

_TOKEN_URL = "https://www.onemap.gov.sg/api/auth/post/getToken"
_ROUTE_URL = "https://www.onemap.gov.sg/api/public/routingsvc/route"
_REFRESH_MARGIN_S = 3600.0  # refresh when within 1 hour of expiry
_DISTANCE_TOLERANCE = 0.15  # 15% slack between summed instruction distance and total_distance

# ASSUMPTION (see module docstring / final report): OneMap's route_instructions
# follows the OSRM RouteInstruction layout
# [type, name, length_m, geometry_position, time_s, length_str, direction, azimuth],
# i.e. distance is at index 2 and time at index 4 -- NOT index 3/4 as the original
# spec text guessed (index 3 there is actually the position into route_geometry,
# not a distance). We try both layouts and gate on whether the summed distances
# are consistent with route_summary.total_distance, falling back to a uniform,
# distance-proportional split (with a warning) if neither lines up.
_DISTANCE_TIME_INDEX_CANDIDATES: tuple[tuple[int, int], ...] = ((2, 4), (3, 4))


class TokenCache(Protocol):
    async def get(self) -> tuple[str, float] | None: ...

    async def set(self, token: str, expiry: float) -> None: ...


class OneMapEngine:
    name = "onemap"

    def __init__(
        self,
        client: httpx.AsyncClient,
        email: str,
        password: str,
        token_cache: TokenCache | None = None,
    ) -> None:
        self._client = client
        self._email = email
        self._password = password
        self._token_cache = token_cache
        self._token: str | None = None
        self._expiry: float | None = None

    async def _fetch_token(self) -> tuple[str, float]:
        try:
            resp = await self._client.post(
                _TOKEN_URL, json={"email": self._email, "password": self._password}
            )
        except httpx.HTTPError as exc:
            raise RoutingError(f"onemap: token request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise RoutingError(f"onemap: token HTTP {resp.status_code}: {resp.text}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise RoutingError(f"onemap: could not parse token response JSON: {exc}") from exc

        token = data.get("access_token")
        expiry_raw = data.get("expiry_timestamp")
        if not token or expiry_raw is None:
            raise RoutingError("onemap: token response missing access_token/expiry_timestamp")
        return token, float(expiry_raw)

    async def _get_token(self, force: bool = False) -> str:
        now = time.time()

        if not force:
            if (
                self._token is not None
                and self._expiry is not None
                and self._expiry - now > _REFRESH_MARGIN_S
            ):
                return self._token
            if self._token_cache is not None:
                cached = await self._token_cache.get()
                if cached is not None:
                    token, expiry = cached
                    if expiry - now > _REFRESH_MARGIN_S:
                        self._token, self._expiry = token, expiry
                        return token

        token, expiry = await self._fetch_token()
        self._token, self._expiry = token, expiry
        if self._token_cache is not None:
            await self._token_cache.set(token, expiry)
        return token

    async def _request(self, params: dict[str, str], token: str) -> httpx.Response:
        try:
            return await self._client.get(
                _ROUTE_URL, params=params, headers={"Authorization": token}
            )
        except httpx.HTTPError as exc:
            raise RoutingError(f"onemap: route request failed: {exc}") from exc

    async def route(
        self, origin: LatLng, destination: LatLng, depart_at: datetime | None = None
    ) -> Route:
        params = {
            "start": f"{origin.lat},{origin.lng}",
            "end": f"{destination.lat},{destination.lng}",
            "routeType": "drive",
        }

        token = await self._get_token()
        resp = await self._request(params, token)
        if resp.status_code == 401:
            token = await self._get_token(force=True)
            resp = await self._request(params, token)

        if resp.status_code >= 400:
            raise RoutingError(f"onemap: HTTP {resp.status_code}: {resp.text}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise RoutingError(f"onemap: could not parse response JSON: {exc}") from exc

        return self._parse_route(data)

    def _parse_route(self, data: dict[str, Any]) -> Route:
        geometry = data.get("route_geometry", "")
        points = decode_polyline(geometry)
        if not points:
            raise RoutingError("onemap: route had no geometry")

        summary_block = data.get("route_summary") or {}
        try:
            total_time = float(summary_block.get("total_time", 0.0))
            total_distance = float(summary_block.get("total_distance", 0.0))
        except (TypeError, ValueError) as exc:
            raise RoutingError(f"onemap: invalid route_summary: {exc}") from exc

        warnings: list[str] = []
        instructions = data.get("route_instructions") or []
        cumulative = _distribute_by_instructions(
            points, instructions, total_time, total_distance, warnings
        )

        return Route(
            points=points,
            cumulative_seconds=cumulative,
            distance_m=total_distance,
            duration_s=total_time,
            engine=self.name,
            summary="",
            warnings=warnings,
        )


def _extract_instruction_pairs(
    instructions: list[Any], distance_idx: int, time_idx: int
) -> list[tuple[float, float]] | None:
    pairs: list[tuple[float, float]] = []
    for instr in instructions:
        try:
            dist = float(instr[distance_idx])
            secs = float(instr[time_idx])
        except (IndexError, TypeError, ValueError):
            return None
        pairs.append((dist, secs))
    return pairs


def _distribute_by_instructions(
    points: list[LatLng],
    instructions: list[Any],
    total_time: float,
    total_distance: float,
    warnings: list[str],
) -> list[float]:
    seg_lengths = [haversine(points[i], points[i + 1]) for i in range(len(points) - 1)]

    best_pairs: list[tuple[float, float]] | None = None
    if instructions and total_distance > 0:
        for distance_idx, time_idx in _DISTANCE_TIME_INDEX_CANDIDATES:
            pairs = _extract_instruction_pairs(instructions, distance_idx, time_idx)
            if not pairs:
                continue
            dist_sum = sum(d for d, _ in pairs)
            if dist_sum <= 0:
                continue
            if abs(dist_sum - total_distance) / total_distance <= _DISTANCE_TOLERANCE:
                best_pairs = pairs
                break

    if best_pairs is None:
        warnings.append(
            "onemap: route_instructions missing or inconsistent with total_distance; "
            "distributed time uniformly by segment length"
        )
        return distribute_duration(points, total_time)

    cum_instr_dist = [0.0]
    cum_instr_time = [0.0]
    for dist, secs in best_pairs:
        cum_instr_dist.append(cum_instr_dist[-1] + dist)
        cum_instr_time.append(cum_instr_time[-1] + secs)

    cumulative = [0.0]
    running_dist = 0.0
    seg_idx = 0
    n_instr = len(best_pairs)
    for seg in seg_lengths:
        running_dist += seg
        while seg_idx < n_instr - 1 and running_dist > cum_instr_dist[seg_idx + 1]:
            seg_idx += 1
        seg_start_d = cum_instr_dist[seg_idx]
        seg_end_d = cum_instr_dist[seg_idx + 1]
        seg_start_t = cum_instr_time[seg_idx]
        seg_end_t = cum_instr_time[seg_idx + 1]
        if seg_end_d > seg_start_d:
            frac = (running_dist - seg_start_d) / (seg_end_d - seg_start_d)
        else:
            frac = 1.0
        frac = min(max(frac, 0.0), 1.0)
        cumulative.append(seg_start_t + frac * (seg_end_t - seg_start_t))

    last = cumulative[-1]
    if last > 0 and total_time > 0:
        scale = total_time / last
        cumulative = [c * scale for c in cumulative]
    return cumulative
