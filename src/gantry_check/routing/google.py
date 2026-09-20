"""Google Routes API (computeRoutes) engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from gantry_check.domain.geo import decode_polyline
from gantry_check.domain.models import SGT, LatLng, Route
from gantry_check.routing.base import RoutingError, distribute_duration

_ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"
_FIELD_MASK = (
    "routes.distanceMeters,routes.duration,routes.staticDuration,"
    "routes.polyline.encodedPolyline,routes.description,"
    "routes.legs.duration,routes.legs.staticDuration,"
    "routes.legs.steps.polyline.encodedPolyline,routes.legs.steps.staticDuration,"
    "routes.legs.steps.distanceMeters"
)
_DEPARTURE_MARGIN = timedelta(seconds=30)


def _parse_duration(value: str | None) -> float | None:
    """Parse a Google `"123s"` / `"123.5s"` duration string into seconds."""
    if not value:
        return None
    return float(value[:-1]) if value.endswith("s") else float(value)


def _extend(
    points: list[LatLng],
    cum: list[float],
    new_points: list[LatLng],
    new_cum: list[float],
) -> None:
    """Append new_points/new_cum onto points/cum, dropping a duplicate shared vertex."""
    if points and new_points and new_points[0] == points[-1]:
        points.extend(new_points[1:])
        cum.extend(new_cum[1:])
    else:
        points.extend(new_points)
        cum.extend(new_cum)


class GoogleRoutesEngine:
    name = "google"

    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        self._client = client
        self._api_key = api_key

    async def route(
        self, origin: LatLng, destination: LatLng, depart_at: datetime | None = None
    ) -> Route:
        body: dict[str, Any] = {
            "origin": {"location": {"latLng": {"latitude": origin.lat, "longitude": origin.lng}}},
            "destination": {
                "location": {"latLng": {"latitude": destination.lat, "longitude": destination.lng}}
            },
            "travelMode": "DRIVE",
            "languageCode": "en-SG",
            "units": "METRIC",
            "regionCode": "SG",
            "polylineQuality": "HIGH_QUALITY",
        }

        if depart_at is not None:
            depart_at_aware = (
                depart_at if depart_at.tzinfo is not None else depart_at.replace(tzinfo=SGT)
            )
            depart_at_utc = depart_at_aware.astimezone(UTC)
            now_utc = datetime.now(UTC)
            if depart_at_utc > now_utc + _DEPARTURE_MARGIN:
                body["routingPreference"] = "TRAFFIC_AWARE"
                body["departureTime"] = depart_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            resp = await self._client.post(
                _ENDPOINT,
                json=body,
                headers={
                    "X-Goog-Api-Key": self._api_key,
                    "X-Goog-FieldMask": _FIELD_MASK,
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise RoutingError(f"google: request failed: {exc}") from exc

        if resp.status_code >= 400:
            message = resp.text
            try:
                payload = resp.json()
                message = payload.get("error", {}).get("message", message)
            except ValueError:
                pass
            raise RoutingError(f"google: HTTP {resp.status_code}: {message}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise RoutingError(f"google: could not parse response JSON: {exc}") from exc

        routes = data.get("routes") or []
        if not routes:
            raise RoutingError("google: no routes returned")

        gr = routes[0]
        route_duration = _parse_duration(gr.get("duration"))
        distance_m = float(gr.get("distanceMeters", 0.0))
        summary = gr.get("description", "") or ""
        legs = gr.get("legs") or []

        points: list[LatLng] = []
        cumulative: list[float] = []
        running_time = 0.0

        for leg in legs:
            leg_duration = _parse_duration(leg.get("duration"))
            leg_static_duration = _parse_duration(leg.get("staticDuration"))
            steps = leg.get("steps") or []

            leg_points: list[LatLng] = []
            leg_static_cum: list[float] = []
            static_running = 0.0

            for step in steps:
                encoded = (step.get("polyline") or {}).get("encodedPolyline", "")
                step_points = decode_polyline(encoded)
                if not step_points:
                    continue
                step_static = _parse_duration(step.get("staticDuration")) or 0.0
                step_cum = distribute_duration(step_points, step_static)
                offset_cum = [static_running + c for c in step_cum]
                _extend(leg_points, leg_static_cum, step_points, offset_cum)
                static_running += step_static

            if not leg_points:
                continue

            if leg_duration is not None and leg_static_duration and leg_static_duration > 0:
                factor = leg_duration / leg_static_duration
            elif leg_duration is not None and static_running > 0:
                factor = leg_duration / static_running
            else:
                factor = 1.0

            leg_scaled_cum = [c * factor for c in leg_static_cum]
            leg_total_time = leg_scaled_cum[-1] if leg_scaled_cum else (leg_duration or 0.0)

            offset_leg_cum = [running_time + c for c in leg_scaled_cum]
            _extend(points, cumulative, leg_points, offset_leg_cum)
            running_time += leg_total_time

        if not points:
            # Steps were unavailable/empty; fall back to the route-level polyline
            # distributed uniformly (by segment length) across the whole route.
            route_polyline = (gr.get("polyline") or {}).get("encodedPolyline", "")
            fallback_points = decode_polyline(route_polyline)
            if not fallback_points:
                raise RoutingError("google: route had no usable polyline")
            fallback_duration = route_duration if route_duration is not None else 0.0
            points = fallback_points
            cumulative = distribute_duration(fallback_points, fallback_duration)
            running_time = cumulative[-1] if cumulative else 0.0

        if route_duration is None:
            route_duration = running_time

        return Route(
            points=points,
            cumulative_seconds=cumulative,
            distance_m=distance_m,
            duration_s=route_duration,
            engine=self.name,
            summary=summary,
            warnings=[],
        )
