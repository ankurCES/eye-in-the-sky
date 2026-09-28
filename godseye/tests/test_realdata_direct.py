"""realdata DIRECT mode and the non-blocking refresher stop (WG v2 §4.1.5,
§4.1.9 #11, A3).

NOTHING here touches the network. Re:Earth and Open-Meteo answers are
hand-written in each upstream's documented shape (tests/fixtures/geodata/) and
served by an injected `fetch`; the egress tests additionally make any socket a
test failure.

What these tests guard:
* direct terrain asks Re:Earth itself (5 dp `lon,lat;…`, batches of 64) and
  still derives MSL from the ELLIPSOIDAL height through canonical_altitude (T1);
* direct weather normalises Open-Meteo into the proxy's shape, so the fuel
  model's wind and the sensor factor come out exactly as before;
* the proxy-only feeds (installations, traffic) degrade with a reason instead
  of silently vanishing, and nothing in direct mode calls a GEV URL;
* `GODSEYE_NO_EGRESS=1` refuses before any socket;
* `BackgroundRefresher.stop(wait=False)` returns at once and never publishes
  a hydration that finishes after it.
"""
from __future__ import annotations

import json
import pathlib
import socket
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from godseye_uav import geo, geo_http, realdata
from godseye_uav.realdata import (
    HttpResponse,
    RealDataUnavailable,
    RealWorldData,
    TerrainProvider,
    WeatherProvider,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "geodata"
BENGALURU = (12.97843, 77.58738)


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Upstreams:
    """Answers Re:Earth per requested point and Open-Meteo from fixtures."""

    def __init__(self, *, terrain_fail: bool = False, weather: object = None) -> None:
        self.calls: list[str] = []
        self.terrain_fail = terrain_fail
        self.weather = load("openmeteo_forecast.json") if weather is None else weather

    def __call__(self, url: str, timeout_s: float, data: bytes | None = None) -> HttpResponse:
        self.calls.append(url)
        parsed = urlparse(url)
        if url.startswith(realdata.REEARTH_HEIGHTS_URL):
            if self.terrain_fail:
                return HttpResponse(503, "busy")
            points = unquote(parse_qs(parsed.query)["points"][0]).split(";")
            rows = []
            for point in points:
                lon, lat = (float(v) for v in point.split(","))
                rows.append({"lon": lon, "lat": lat, "elevation": 924.6, "geoid": -86.4,
                             "ellipsoid": 838.6})
            return HttpResponse(200, json.dumps({"results": rows}))
        if url.startswith(realdata.OPEN_METEO_FORECAST_URL):
            return HttpResponse(200, json.dumps(self.weather))
        if url.startswith(realdata.OPEN_METEO_ELEVATION_URL):
            return HttpResponse(200, json.dumps(load("openmeteo_elevation.json")))
        raise AssertionError(f"direct mode called an unexpected upstream: {url}")


def forbid_sockets(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)


# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------


def test_direct_terrain_url_batches_of_64_and_reearth_provenance():
    up = Upstreams()
    terrain = TerrainProvider(direct=True, fetch=up)
    points = [(12.9 + i * 1e-3, 77.5 + i * 1e-3) for i in range(100)]
    samples = terrain.heights(points)
    assert len(up.calls) == 2 and terrain.requests == 2              # 64 + 36
    first = up.calls[0]
    assert first.startswith("https://terrain.reearth.land/heights.json?points=")
    sent = unquote(parse_qs(urlparse(first).query)["points"][0]).split(";")
    assert len(sent) == 64
    assert sent[0] == "77.50000,12.90000" and sent[1] == "77.50100,12.90100"   # lon,lat 5 dp
    assert all(s.real for s in samples)
    assert {s.provenance.source for s in samples} == {"reearth:heights.json"}
    assert terrain.batch_points == realdata.REEARTH_BATCH_POINTS == 64


def test_direct_terrain_keeps_the_t1_datum_rule():
    """MSL comes from the ellipsoidal height via canonical_altitude (EGM96),
    never from the upstream's EGM2008 `elevation`."""
    row = load("reearth_heights.json")["results"][0]

    def fetch(url, timeout_s, data=None):
        return HttpResponse(200, json.dumps({"results": [row]}))

    sample = TerrainProvider(direct=True, fetch=fetch).height(row["lat"], row["lon"])
    fix = geo.canonical_altitude(row["ellipsoid"], row["lat"], row["lon"], datum="hae")
    assert sample.hae_m == pytest.approx(838.6)
    assert sample.msl_m == pytest.approx(fix.alt_msl)
    assert sample.upstream_elevation_m == pytest.approx(924.6)
    assert sample.as_dict()["provenance"]["source"] == "reearth:heights.json"


def test_direct_terrain_failure_is_flagged_not_zero():
    sample = TerrainProvider(direct=True, fetch=Upstreams(terrain_fail=True)).height(*BENGALURU)
    assert not sample.real and sample.hae_m is None and sample.msl_m is None
    assert "Re:Earth: HTTP 503" in sample.provenance.reason


def test_direct_terrain_goes_through_geo_http_on_the_reearth_gate(monkeypatch):
    seen = []

    def spy(upstream, url, *, timeout_s, data=None, fetch=None):
        seen.append((upstream, fetch))
        return {"results": [{"ellipsoid": 838.6, "elevation": 924.6}]}

    monkeypatch.setattr(geo_http, "fetch_json", spy)
    TerrainProvider(direct=True).height(*BENGALURU)
    assert seen == [("reearth", None)]          # None: the real, gated, egress-checked client


def test_gev_mode_is_unchanged():
    terrain = TerrainProvider(origin="http://test", fetch=lambda u, t: HttpResponse(500, ""))
    assert not terrain.direct and terrain._source == "gev:/api/terrain/heights"
    assert terrain._url([(1.0, 2.0)]).startswith("http://test/api/terrain/heights?points=")
    assert WeatherProvider(origin="http://test")._url(1.0, 2.0).startswith(
        "http://test/api/weather-effects?")


# ---------------------------------------------------------------------------
# Weather (Open-Meteo)
# ---------------------------------------------------------------------------


def test_openmeteo_to_effects_matches_the_proxy_shape():
    effects = realdata._openmeteo_to_effects(load("openmeteo_forecast.json"))
    assert effects["status"] == "ready"
    assert effects["weather"] == {
        "observedAt": "2026-09-28T10:15:00.000Z",          # zone-naive time pinned to UTC
        "temperatureC": 18.2, "apparentTemperatureC": 16.9, "precipitationMm": 0.0,
        "cloudCoverPct": 81.0, "windKph": 18.0, "windDirectionDeg": 270.0,
        "visibilityM": 24140.0, "weatherCode": 3.0}
    assert realdata._openmeteo_to_effects({"current": {"time": "x"}}) is None
    assert realdata._openmeteo_to_effects([]) is None


def test_direct_weather_feeds_the_fuel_model_wind_like_the_proxy_did():
    up = Upstreams()
    obs = WeatherProvider(direct=True, fetch=up).observe(46.6354, 32.6169)
    url = urlparse(up.calls[0])
    assert f"{url.scheme}://{url.netloc}{url.path}" == realdata.OPEN_METEO_FORECAST_URL
    query = parse_qs(url.query)
    assert query["latitude"] == ["46.63540"] and query["longitude"] == ["32.61690"]
    assert query["current"] == [realdata.OPEN_METEO_CURRENT] and query["timezone"] == ["UTC"]
    assert obs.real and obs.provenance.source == "open-meteo"
    assert obs.wind_speed_mps == pytest.approx(5.0)            # 18 km/h
    north, east = obs.wind_ne                                  # a westerly blows east
    assert north == pytest.approx(0.0, abs=1e-9) and east == pytest.approx(5.0)
    assert obs.condition == "clear" and obs.observed_at_ms is not None


def test_direct_weather_malformed_or_down_is_synthetic_with_a_reason():
    obs = WeatherProvider(direct=True, fetch=Upstreams(weather={"error": True})).observe(1, 2)
    assert not obs.real and "malformed Open-Meteo response" in obs.provenance.reason
    obs = WeatherProvider(direct=True,
                          fetch=lambda u, t, data=None: HttpResponse(503, "")).observe(1, 2)
    assert not obs.real and "Open-Meteo: HTTP 503" in obs.provenance.reason


def test_open_meteo_elevation_is_the_documented_fallback_ground():
    up = Upstreams()
    assert realdata.open_meteo_elevation(*BENGALURU, fetch=up) == 925.0
    query = parse_qs(urlparse(up.calls[0]).query)
    assert query == {"latitude": ["12.97843"], "longitude": ["77.58738"]}
    with pytest.raises(RealDataUnavailable, match="no elevation"):
        realdata.open_meteo_elevation(
            1, 2, fetch=lambda u, t, data=None: HttpResponse(200, '{"elevation": []}'))
    with pytest.raises(RealDataUnavailable, match="HTTP 500"):
        realdata.open_meteo_elevation(
            1, 2, fetch=lambda u, t, data=None: HttpResponse(500, ""))


# ---------------------------------------------------------------------------
# The client and the egress switch
# ---------------------------------------------------------------------------


def test_direct_client_hydrates_terrain_and_weather_and_says_why_the_rest_is_empty():
    up = Upstreams()
    client = realdata.default_client(fallback_ground_msl_m=None, direct=True, fetch=up)
    assert client.direct and client.origin == "direct"
    data = client.hydrate_theater(
        theater_id="dyn-test", home_lat=BENGALURU[0], home_lon=BENGALURU[1],
        bbox=(12.95, 77.56, 13.0, 77.61), floor_points=[BENGALURU, (12.96, 77.57)])
    assert data.ground.real and data.ground.provenance.source == "reearth:heights.json"
    assert data.weather.real and data.weather.provenance.source == "open-meteo"
    assert data.floor is not None and data.floor.real
    assert data.degraded_feeds == ["installations", "traffic"]
    assert data.order_of_battle.provenance.reason == realdata.DIRECT_INSTALLATIONS_REASON
    assert data.order_of_battle.sites == ()
    assert data.traffic.provenance.reason.startswith(realdata.DIRECT_TRAFFIC_REASON)
    assert data.traffic.contacts == ()
    hosts = {urlparse(u).netloc for u in up.calls}
    assert hosts == {"terrain.reearth.land", "api.open-meteo.com"}      # never GEV
    assert all("/api/" not in urlparse(u).path for u in up.calls)


def test_no_egress_refuses_every_direct_fetch_before_a_socket(monkeypatch):
    monkeypatch.setenv(geo_http.NO_EGRESS_ENV, "1")
    forbid_sockets(monkeypatch)
    sample = TerrainProvider(direct=True).height(*BENGALURU)
    assert not sample.known and "egress disabled" in sample.provenance.reason
    obs = WeatherProvider(direct=True).observe(*BENGALURU)
    assert not obs.real and "egress disabled" in obs.provenance.reason
    with pytest.raises(RealDataUnavailable, match="egress disabled"):
        realdata.open_meteo_elevation(*BENGALURU)
    data = RealWorldData(direct=True).hydrate_theater(
        theater_id="dyn-test", home_lat=BENGALURU[0], home_lon=BENGALURU[1],
        bbox=(12.95, 77.56, 13.0, 77.61))
    assert not data.real
    assert set(data.degraded_feeds) == {"terrain", "weather", "installations", "traffic"}


# ---------------------------------------------------------------------------
# BackgroundRefresher.stop(wait=False)  (§4.1.9 #11)
# ---------------------------------------------------------------------------


class ParkedClient:
    """A client whose hydration parks until released."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def hydrate_theater(self, **kwargs):
        self.started.set()
        self.release.wait(5.0)
        return "result"


def test_stop_without_wait_returns_at_once_and_never_publishes_late():
    client = ParkedClient()
    published = []
    refresher = realdata.BackgroundRefresher(client, interval_s=0.01,
                                             on_result=published.append).start()
    thread = refresher._thread
    assert client.started.wait(5.0)
    t0 = time.perf_counter()
    refresher.stop(wait=False)
    assert time.perf_counter() - t0 < 0.05
    assert thread.is_alive()                    # still parked; nobody waited on it
    client.release.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert published == []                      # the late hydration was dropped
    refresher.stop(wait=False)                  # idempotent


def test_stop_with_wait_still_joins():
    client = ParkedClient()
    client.release.set()
    refresher = realdata.BackgroundRefresher(client, interval_s=60.0).start()
    thread = refresher._thread
    assert client.started.wait(5.0)
    refresher.stop()
    assert not thread.is_alive()
