from __future__ import annotations

import time
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from gantry_check.config import Settings
from gantry_check.domain.geo import decode_polyline, encode_polyline
from gantry_check.domain.models import SGT, LatLng
from gantry_check.routing.base import FallbackEngine, RoutingError, build_engine
from gantry_check.routing.google import GoogleRoutesEngine
from gantry_check.routing.onemap import OneMapEngine

ORIGIN = LatLng(lat=1.3000, lng=103.8000)
DESTINATION = LatLng(lat=1.3100, lng=103.8200)

GOOGLE_ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"
ONEMAP_TOKEN_URL = "https://www.onemap.gov.sg/api/auth/post/getToken"
ONEMAP_ROUTE_URL = "https://www.onemap.gov.sg/api/public/routingsvc/route"


# ---------------------------------------------------------------------------
# polyline
# ---------------------------------------------------------------------------


def test_polyline_decode_canonical_example():
    # Google's own documented example.
    points = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert [(round(p.lat, 5), round(p.lng, 5)) for p in points] == [
        (38.5, -120.2),
        (40.7, -120.95),
        (43.252, -126.453),
    ]


def test_polyline_roundtrip():
    points = [
        LatLng(lat=1.3000, lng=103.8000),
        LatLng(lat=1.3021, lng=103.8055),
        LatLng(lat=1.3050, lng=103.8100),
    ]
    encoded = encode_polyline(points)
    decoded = decode_polyline(encoded)
    assert len(decoded) == len(points)
    for original, rt in zip(points, decoded, strict=True):
        assert original.lat == pytest.approx(rt.lat, abs=1e-5)
        assert original.lng == pytest.approx(rt.lng, abs=1e-5)


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------

_STEP1_POINTS = [
    LatLng(lat=1.3000, lng=103.8000),
    LatLng(lat=1.3020, lng=103.8050),
    LatLng(lat=1.3050, lng=103.8100),
]
_STEP2_POINTS = [
    LatLng(lat=1.3050, lng=103.8100),  # duplicate of step1's last point
    LatLng(lat=1.3080, lng=103.8150),
    LatLng(lat=1.3100, lng=103.8200),
]


def _google_body(*, leg_duration: str = "300s", leg_static: str = "250s") -> dict:
    return {
        "routes": [
            {
                "distanceMeters": 5000,
                "duration": "300s",
                "staticDuration": "250s",
                "description": "via ECP",
                "legs": [
                    {
                        "duration": leg_duration,
                        "staticDuration": leg_static,
                        "steps": [
                            {
                                "polyline": {"encodedPolyline": encode_polyline(_STEP1_POINTS)},
                                "staticDuration": "100s",
                                "distanceMeters": 600,
                            },
                            {
                                "polyline": {"encodedPolyline": encode_polyline(_STEP2_POINTS)},
                                "staticDuration": "150s",
                                "distanceMeters": 700,
                            },
                        ],
                    }
                ],
            }
        ]
    }


@respx.mock
async def test_google_happy_path():
    route_mock = respx.post(GOOGLE_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_google_body())
    )
    async with httpx.AsyncClient() as client:
        engine = GoogleRoutesEngine(client, api_key="test-key")
        route = await engine.route(ORIGIN, DESTINATION)

    assert route_mock.called
    assert route.engine == "google"
    assert len(route.points) == 5  # 3 + 3 - 1 duplicate vertex dropped
    assert route.cumulative_seconds[0] == 0.0
    cs = route.cumulative_seconds
    for i in range(len(cs) - 1):
        assert cs[i + 1] >= cs[i]
    # leg static total (100 + 150 = 250s) scaled by leg.duration/leg.staticDuration (300/250)
    assert route.cumulative_seconds[-1] == pytest.approx(300.0)
    assert route.duration_s == pytest.approx(300.0)
    assert route.distance_m == pytest.approx(5000.0)
    assert route.summary == "via ECP"
    assert route.warnings == []


@respx.mock
async def test_google_past_departure_omits_routing_preference():
    route_mock = respx.post(GOOGLE_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_google_body())
    )
    past = datetime.now(SGT) - timedelta(hours=1)
    async with httpx.AsyncClient() as client:
        engine = GoogleRoutesEngine(client, api_key="test-key")
        await engine.route(ORIGIN, DESTINATION, depart_at=past)

    sent_body = route_mock.calls.last.request.content
    import json

    payload = json.loads(sent_body)
    assert "routingPreference" not in payload
    assert "departureTime" not in payload


@respx.mock
async def test_google_future_departure_includes_routing_preference():
    route_mock = respx.post(GOOGLE_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_google_body())
    )
    future = datetime.now(SGT) + timedelta(hours=2)
    async with httpx.AsyncClient() as client:
        engine = GoogleRoutesEngine(client, api_key="test-key")
        await engine.route(ORIGIN, DESTINATION, depart_at=future)

    import json

    payload = json.loads(route_mock.calls.last.request.content)
    assert payload["routingPreference"] == "TRAFFIC_AWARE"
    assert payload["departureTime"].endswith("Z")


@respx.mock
async def test_google_403_raises_routing_error():
    error_body = {
        "error": {"code": 403, "message": "API key invalid", "status": "PERMISSION_DENIED"}
    }
    respx.post(GOOGLE_ENDPOINT).mock(return_value=httpx.Response(403, json=error_body))
    async with httpx.AsyncClient() as client:
        engine = GoogleRoutesEngine(client, api_key="bad-key")
        with pytest.raises(RoutingError) as exc_info:
            await engine.route(ORIGIN, DESTINATION)

    assert "403" in str(exc_info.value)
    assert "API key invalid" in str(exc_info.value)


# ---------------------------------------------------------------------------
# OneMap
# ---------------------------------------------------------------------------

_ONEMAP_POINTS = [
    LatLng(lat=1.3000, lng=103.8000),
    LatLng(lat=1.3020, lng=103.8050),
    LatLng(lat=1.3050, lng=103.8100),
]


def _onemap_token_body(expires_in: float = 7200.0) -> dict:
    return {"access_token": "tok-1", "expiry_timestamp": str(time.time() + expires_in)}


def _onemap_route_body(*, with_instructions: bool = True) -> dict:
    body = {
        "route_geometry": encode_polyline(_ONEMAP_POINTS),
        "route_summary": {"total_time": 200, "total_distance": 1000},
    }
    if with_instructions:
        body["route_instructions"] = [
            ["Head", "Yishun Ave", 400, 0, 80, "400m", "N", 0],
            ["Turn", "CTE", 600, 2, 120, "600m", "N", 0],
        ]
    return body


@respx.mock
async def test_onemap_happy_path():
    respx.post(ONEMAP_TOKEN_URL).mock(return_value=httpx.Response(200, json=_onemap_token_body()))
    respx.get(ONEMAP_ROUTE_URL).mock(return_value=httpx.Response(200, json=_onemap_route_body()))

    async with httpx.AsyncClient() as client:
        engine = OneMapEngine(client, email="a@b.com", password="pw")
        route = await engine.route(ORIGIN, DESTINATION)

    assert route.engine == "onemap"
    assert route.distance_m == pytest.approx(1000.0)
    assert route.duration_s == pytest.approx(200.0)
    assert route.cumulative_seconds[0] == 0.0
    assert route.cumulative_seconds[-1] == pytest.approx(200.0)
    cs = route.cumulative_seconds
    for i in range(len(cs) - 1):
        assert cs[i + 1] >= cs[i]
    # instructions' distances (400 + 600) match total_distance (1000) exactly.
    assert route.warnings == []


@respx.mock
async def test_onemap_inconsistent_instructions_falls_back_with_warning():
    respx.post(ONEMAP_TOKEN_URL).mock(return_value=httpx.Response(200, json=_onemap_token_body()))
    body = _onemap_route_body(with_instructions=False)
    body["route_instructions"] = [["Head", "Nowhere Rd", 1, 0, 1, "1m", "N", 0]]  # wildly off
    respx.get(ONEMAP_ROUTE_URL).mock(return_value=httpx.Response(200, json=body))

    async with httpx.AsyncClient() as client:
        engine = OneMapEngine(client, email="a@b.com", password="pw")
        route = await engine.route(ORIGIN, DESTINATION)

    assert route.cumulative_seconds[-1] == pytest.approx(200.0)
    assert len(route.warnings) == 1
    assert "uniform" in route.warnings[0]


@respx.mock
async def test_onemap_401_refreshes_token_and_retries():
    token_route = respx.post(ONEMAP_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json=_onemap_token_body()),
            httpx.Response(200, json=_onemap_token_body()),
        ]
    )
    route_route = respx.get(ONEMAP_ROUTE_URL).mock(
        side_effect=[
            httpx.Response(401, text="unauthorized"),
            httpx.Response(200, json=_onemap_route_body()),
        ]
    )

    async with httpx.AsyncClient() as client:
        engine = OneMapEngine(client, email="a@b.com", password="pw")
        route = await engine.route(ORIGIN, DESTINATION)

    assert route.engine == "onemap"
    assert token_route.call_count == 2
    assert route_route.call_count == 2


# ---------------------------------------------------------------------------
# FallbackEngine
# ---------------------------------------------------------------------------


@respx.mock
async def test_fallback_engine_uses_onemap_after_google_failure():
    respx.post(GOOGLE_ENDPOINT).mock(return_value=httpx.Response(500, text="server error"))
    respx.post(ONEMAP_TOKEN_URL).mock(return_value=httpx.Response(200, json=_onemap_token_body()))
    respx.get(ONEMAP_ROUTE_URL).mock(return_value=httpx.Response(200, json=_onemap_route_body()))

    async with httpx.AsyncClient() as client:
        google = GoogleRoutesEngine(client, api_key="k")
        onemap = OneMapEngine(client, email="a@b.com", password="pw")
        fallback = FallbackEngine([google, onemap])
        route = await fallback.route(ORIGIN, DESTINATION)

    assert route.engine == "onemap"
    assert len(route.warnings) == 1
    assert "google failed" in route.warnings[0]
    assert "used onemap" in route.warnings[0]


@respx.mock
async def test_fallback_engine_raises_when_all_fail():
    respx.post(GOOGLE_ENDPOINT).mock(return_value=httpx.Response(500, text="server error"))
    respx.post(ONEMAP_TOKEN_URL).mock(return_value=httpx.Response(500, text="server error"))

    async with httpx.AsyncClient() as client:
        google = GoogleRoutesEngine(client, api_key="k")
        onemap = OneMapEngine(client, email="a@b.com", password="pw")
        fallback = FallbackEngine([google, onemap])
        with pytest.raises(RoutingError):
            await fallback.route(ORIGIN, DESTINATION)


# ---------------------------------------------------------------------------
# build_engine / Settings selection logic
# ---------------------------------------------------------------------------


async def test_build_engine_google_only():
    settings = Settings(google_maps_api_key="k", routing_engine="google")
    async with httpx.AsyncClient() as client:
        engine = build_engine(settings, client)
    assert engine.name == "google"


async def test_build_engine_google_and_onemap_fallback_chain():
    settings = Settings(
        google_maps_api_key="k",
        onemap_email="a@b.com",
        onemap_password="pw",
        routing_engine="google",
    )
    async with httpx.AsyncClient() as client:
        engine = build_engine(settings, client)
    assert engine.name == "fallback"
    assert isinstance(engine, FallbackEngine)


async def test_build_engine_onemap_only():
    settings = Settings(onemap_email="a@b.com", onemap_password="pw", routing_engine="google")
    async with httpx.AsyncClient() as client:
        engine = build_engine(settings, client)
    assert engine.name == "onemap"


async def test_build_engine_prefers_onemap_when_requested():
    settings = Settings(
        google_maps_api_key="k",
        onemap_email="a@b.com",
        onemap_password="pw",
        routing_engine="onemap",
    )
    async with httpx.AsyncClient() as client:
        engine = build_engine(settings, client)
    assert engine.name == "onemap"


async def test_build_engine_raises_when_nothing_configured():
    settings = Settings()
    async with httpx.AsyncClient() as client:
        with pytest.raises(RoutingError):
            build_engine(settings, client)


# ---------------------------------------------------------------------------
# config.Settings / load_dotenv
# ---------------------------------------------------------------------------


def test_settings_from_env_mapping():
    env = {
        "GOOGLE_MAPS_API_KEY": "gkey",
        "ONEMAP_EMAIL": "a@b.com",
        "ONEMAP_PASSWORD": "pw",
        "ROUTING_ENGINE": "onemap",
        "LOCAL_DB": "data/custom.sqlite",
    }
    settings = Settings.from_env(env)
    assert settings.google_maps_api_key == "gkey"
    assert settings.onemap_email == "a@b.com"
    assert settings.routing_engine == "onemap"
    assert settings.local_db == "data/custom.sqlite"


def test_settings_from_object():
    class FakeWorkerEnv:
        GOOGLE_MAPS_API_KEY = "gkey"
        ONEMAP_EMAIL = None
        ONEMAP_PASSWORD = None
        ROUTING_ENGINE = "google"

    settings = Settings.from_object(FakeWorkerEnv())
    assert settings.google_maps_api_key == "gkey"
    assert settings.onemap_email is None
    assert settings.local_db == "data/local.sqlite"


def test_load_dotenv(tmp_path):
    from gantry_check.config import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "GOOGLE_MAPS_API_KEY=abc123",
                'ONEMAP_EMAIL="a@b.com"',
                "ROUTING_ENGINE=google          # google | onemap",
                "LOCAL_DB='data/local.sqlite'",
            ]
        )
    )
    result = load_dotenv(str(env_file))
    assert result == {
        "GOOGLE_MAPS_API_KEY": "abc123",
        "ONEMAP_EMAIL": "a@b.com",
        "ROUTING_ENGINE": "google",
        "LOCAL_DB": "data/local.sqlite",
    }


def test_load_dotenv_missing_file(tmp_path):
    from gantry_check.config import load_dotenv

    result = load_dotenv(str(tmp_path / "does-not-exist.env"))
    assert result == {}
