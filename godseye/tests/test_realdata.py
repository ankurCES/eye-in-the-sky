"""Real-world data ingestion tests (realdata.py + theaters.py hydration).

NOTHING here touches the network. The HTTP client is injected everywhere, the
payloads are recorded from the live God's Eye View proxies (the terrain and
OpenSky rows below are verbatim captures), and the one test that exercises the
default `urllib_fetch` client binds a loopback server on port 48101.

What these tests are actually guarding:

* the DATUM (T1) — terrain MSL is derived from the upstream's *ellipsoidal*
  height through `geo.canonical_altitude()`, never copied from the upstream's
  own EGM2008 orthometric value, which differs;
* FAIL SOFT BUT VISIBLE — every degraded path returns a value carrying
  `real=False` and a reason, and never a plausible-looking zero;
* NEVER BLOCK — `allow_network=False` reads make zero HTTP calls.
"""
import ast
import json
import keyword
import math
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from godseye_uav import geo, realdata, targets, theaters
from godseye_uav.realdata import (
    ATTRIBUTION,
    MAPPED_DATA_CAVEAT,
    HttpResponse,
    InstallationsProvider,
    Provenance,
    RealDataUnavailable,
    RealWorldData,
    TerrainProvider,
    TrafficProvider,
    WeatherProvider,
)
from godseye_uav.safety import FuelModel, headwind_component_mps

# ---------------------------------------------------------------------------
# Recorded fixtures (captured from the live GEV proxies on 2026-09-17).
# ---------------------------------------------------------------------------

#: GET /api/terrain/heights?points=-122.14017,47.64147 — verbatim.
REDMOND_ROW = {"lon": -122.14017, "lat": 47.64147,
               "elevation": 112.0397511050871,
               "geoid": -22.67383350771277,
               "ellipsoid": 89.36591759737433}

#: GET /api/weather-effects?latitude=32.6546&longitude=51.668 — verbatim.
ISFAHAN_WEATHER = {
    "status": "ready", "retrievedAt": "2026-09-17T19:10:35.831Z",
    "coordinates": {"latitude": 32.6546, "longitude": 51.668},
    "weather": {"observedAt": "2026-09-17T19:00:00.000Z", "temperatureC": 24.3,
                "apparentTemperatureC": 20.3, "precipitationMm": 0,
                "cloudCoverPct": 0, "windKph": 5.8, "windDirectionDeg": 266,
                "visibilityM": 68060, "weatherCode": 0},
}

#: GET /api/opensky?... — first three state vectors, verbatim.
OPENSKY_FRAME = {
    "time": 1789672265,
    "states": [
        ["39de4f", "TVF8603 ", "France", 1789672264, 1789672264, 5.3571, 46.839,
         10972.8, False, 217.56, 314.04, 0.33, None, 11346.18, "1000", False, 0, 0],
        ["39de4e", "TVF276D ", "France", 1789672264, 1789672264, -0.7882, 44.1377,
         11879.58, False, 227.46, 214.59, 0, None, 12329.16, "7636", False, 0, 0],
        ["c07d0b", "CGVJE   ", "Canada", 1789672264, 1789672264, -74.5475, 45.7369,
         487.68, False, 43.42, 256.29, 6.18, None, 525.0, "1200", False, 0, 0],
    ],
}

#: GET /api/military-installations?… — rows taken verbatim from GEV's own disk
#: cache (.gev-cache/military-installations/), which is where the `bounds` +
#: `geometry` way shape below comes from: despite the proxy's `out center`
#: query, 41 of its 51 cached elements are ways with NO `center` at all.
INSTALLATIONS_PAYLOAD = {
    "elements": [
        {"type": "node", "id": 4761898623, "lat": 27.6601617, "lon": 85.4177134,
         "tags": {"military": "barracks", "name:en": "Surybinyak Army Barrack"}},
        {"type": "node", "id": 6052247702, "lat": 27.6643837, "lon": 85.3242385,
         "tags": {"landuse": "military", "military": "office"}},
        {"type": "way", "id": 12345, "center": {"lat": 27.70, "lon": 85.40},
         "tags": {"military": "airfield", "name": "Northern Airfield"}},
        {"type": "node", "id": 999, "lat": 27.65, "lon": 85.35,
         "tags": {"military": "bunker", "name": "Radar Station Alpha"}},
        # Verbatim shape from the GEV cache: bounds + geometry, no center.
        {"type": "way", "id": 81148059,
         "bounds": {"minlat": 27.7021309, "minlon": 85.313759,
                    "maxlat": 27.7045426, "maxlon": 85.31633},
         "geometry": [{"lat": 27.7022085, "lon": 85.313759},
                      {"lat": 27.7021309, "lon": 85.31633}],
         "tags": {"access": "private", "landuse": "military", "name": "Tundikhel"}},
        # bounds only, no geometry.
        {"type": "relation", "id": 4242,
         "bounds": {"minlat": 27.80, "minlon": 85.20, "maxlat": 27.82, "maxlon": 85.24},
         "tags": {"military": "range", "name": "Western Range"}},
        # No position of any kind: MUST be dropped, never placed at (0, 0).
        {"type": "relation", "id": 777, "tags": {"military": "base", "name": "Ghost"}},
    ],
    "saturated": False, "elementCap": 700, "status": "ready",
}


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeGev:
    """An injectable stand-in for the GEV proxies. Records every call."""

    def __init__(self, *, terrain=None, weather=None, traffic=None,
                 installations=None, status=200, headers=None) -> None:
        self.terrain_fn = terrain            # (lat, lon) -> ellipsoidal height | None
        self.weather = weather
        self.traffic = traffic
        self.installations = installations
        self.status = status
        self.headers = headers or {}
        self.calls: list[str] = []

    @property
    def count(self) -> int:
        return len(self.calls)

    def __call__(self, url: str, timeout_s: float) -> HttpResponse:
        self.calls.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if self.status != 200:
            return HttpResponse(self.status, json.dumps({"error": "unavailable"}),
                                self.headers)
        if parsed.path == "/api/terrain/heights":
            results = []
            for pair in query["points"][0].split(";"):
                lon, lat = (float(v) for v in pair.split(","))
                height = self.terrain_fn(lat, lon) if self.terrain_fn else None
                results.append({"lon": lon, "lat": lat, "ellipsoid": height,
                                "geoid": None if height is None else 0.0,
                                "elevation": height})
            return HttpResponse(200, json.dumps({"results": results}), self.headers)
        if parsed.path == "/api/weather-effects":
            return HttpResponse(200, json.dumps(self.weather), self.headers)
        if parsed.path == "/api/opensky":
            return HttpResponse(200, json.dumps(self.traffic), self.headers)
        if parsed.path == "/api/military-installations":
            return HttpResponse(200, json.dumps(self.installations), self.headers)
        return HttpResponse(404, json.dumps({"error": "no route"}), self.headers)


def failing_fetch(reason="connection refused"):
    calls = []

    def fetch(url, timeout_s):
        calls.append(url)
        raise RealDataUnavailable("http", reason)

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def flat_terrain(height_hae_m):
    return lambda lat, lon: height_hae_m


@pytest.fixture(autouse=True)
def _no_hydration_leak():
    """Hydration is process-global; never let one test colour another."""
    theaters.clear_hydration()
    yield
    theaters.clear_hydration()


# ---------------------------------------------------------------------------
# Provenance — the flag that makes degradation visible
# ---------------------------------------------------------------------------


def test_a_synthetic_value_cannot_be_built_without_a_reason():
    """The whole no-silent-fallback rule, enforced at the type."""
    with pytest.raises(ValueError, match="no reason"):
        Provenance(feed="terrain", real=False, source="synthetic:flat-plane")
    ok = Provenance(feed="terrain", real=False, source="x", reason="upstream down")
    assert ok.degraded is True and ok.as_dict()["reason"] == "upstream down"
    assert Provenance(feed="terrain", real=True, source="gev").degraded is False


# ---------------------------------------------------------------------------
# (1) Terrain — the datum, the cache, the fail-soft path
# ---------------------------------------------------------------------------


def test_terrain_msl_comes_from_the_one_conversion_point_not_the_upstream_geoid():
    """T1: MSL is derived from the ELLIPSOIDAL height via canonical_altitude.

    The upstream serves both an ellipsoidal height and its own orthometric
    `elevation` on EGM2008. godSeye's datum is EGM96 and has exactly one
    conversion point, so copying `elevation` would quietly import a second
    geoid. On this recorded Redmond row the two disagree by ~0.5 m.
    """
    def fetch(url, timeout_s):
        return HttpResponse(200, json.dumps({"results": [REDMOND_ROW]}))

    provider = TerrainProvider(origin="http://test", fetch=fetch)
    sample = provider.height(REDMOND_ROW["lat"], REDMOND_ROW["lon"])

    assert sample.real is True
    assert sample.hae_m == pytest.approx(REDMOND_ROW["ellipsoid"])
    expected = geo.canonical_altitude(REDMOND_ROW["ellipsoid"], REDMOND_ROW["lat"],
                                      REDMOND_ROW["lon"], datum="hae").alt_msl
    assert sample.msl_m == pytest.approx(expected)
    # ... and that is NOT the upstream's own orthometric height.
    assert sample.upstream_elevation_m == pytest.approx(REDMOND_ROW["elevation"])
    assert abs(sample.msl_m - REDMOND_ROW["elevation"]) > 0.2
    # The divergence is carried, labelled, and never used.
    assert "NOT used" in sample.as_dict()["upstream_elevation_note"]
    assert sample.datum_source and sample.datum_degraded is False


def test_terrain_request_is_lon_lat_at_five_decimals_like_the_proxy_keys_it():
    """`points=lon,lat;…` — lon FIRST, 5 dp, matching the proxy's own cache key
    precision so our cache and its cache describe the same point."""
    gev = FakeGev(terrain=flat_terrain(100.0))
    provider = TerrainProvider(origin="http://test", fetch=gev)
    provider.heights([(47.6414683, -122.1401234), (32.6546, 51.668)])
    query = parse_qs(urlparse(gev.calls[0]).query)
    assert query["points"][0] == "-122.14012,47.64147;51.66800,32.65460"
    # The cache key uses the same canonical spelling.
    assert TerrainProvider._key(47.6414683, -122.1401234) == "-122.14012,47.64147"


def test_terrain_batches_and_never_exceeds_the_proxy_point_cap():
    gev = FakeGev(terrain=flat_terrain(50.0))
    provider = TerrainProvider(origin="http://test", fetch=gev, batch_points=128)
    points = [(30.0 + i * 1e-3, 40.0 + i * 1e-3) for i in range(300)]
    samples = provider.heights(points)

    assert len(samples) == 300 and all(s.real for s in samples)
    assert gev.count == 3  # 128 + 128 + 44
    for url in gev.calls:
        sent = parse_qs(urlparse(url).query)["points"][0].split(";")
        assert len(sent) <= realdata.TERRAIN_MAX_POINTS
        assert len(sent) <= 128
    # And an over-cap batch is refused rather than silently truncated.
    over = TerrainProvider(origin="http://test", fetch=gev,
                           batch_points=realdata.TERRAIN_MAX_POINTS)
    with pytest.raises(RealDataUnavailable, match="exceeds the proxy cap"):
        over._fetch_batch([(1.0, 2.0)] * (realdata.TERRAIN_MAX_POINTS + 1))


def test_terrain_caches_and_preserves_request_order_with_duplicates():
    gev = FakeGev(terrain=lambda lat, lon: 100.0 + lat)
    provider = TerrainProvider(origin="http://test", fetch=gev)
    first = provider.heights([(10.0, 20.0), (11.0, 21.0), (10.0, 20.0)])
    assert [round(s.hae_m, 3) for s in first] == [110.0, 111.0, 110.0]
    assert gev.count == 1
    # This line was missing its `assert` and therefore checked nothing: the
    # deduplication it claims to guard was untested. 3 points requested, 2
    # unique, so exactly one ";" separator goes upstream.
    assert parse_qs(urlparse(gev.calls[0]).query)["points"][0].count(";") == 1
    provider.heights([(11.0, 21.0), (10.0, 20.0)])
    assert gev.count == 1  # served entirely from cache


def test_terrain_unavailable_is_flagged_and_never_becomes_zero():
    """FAIL SOFT BUT VISIBLE. An unknown ground height is None, not 0.0."""
    fetch = failing_fetch("HTTP 502 from the terrain proxy")
    provider = TerrainProvider(origin="http://test", fetch=fetch)
    sample = provider.height(32.6546, 51.668)

    assert sample.real is False and sample.known is False
    assert sample.hae_m is None and sample.msl_m is None
    assert "502" in sample.provenance.reason
    assert "UNKNOWN (not zero)" in sample.provenance.reason
    assert sample.as_dict()["provenance"]["degraded"] is True


def test_terrain_fallback_plane_is_the_declared_theater_datum_and_says_so():
    """The synthetic fallback IS the old height-above-takeoff behaviour — and
    it is flagged, so a caller can tell an observation from an assumption."""
    provider = TerrainProvider(origin="http://test", fetch=failing_fetch(),
                               fallback_ground_msl_m=1570.0)
    sample = provider.height(32.6546, 51.668)

    assert sample.real is False
    assert sample.msl_m == pytest.approx(1570.0)
    assert sample.hae_m == pytest.approx(
        geo.msl_to_hae(1570.0, 32.6546, 51.668))
    assert "SYNTHETIC flat ground plane" in sample.provenance.reason
    assert "not above real terrain" in sample.provenance.reason


def test_terrain_upstream_row_without_a_height_is_not_cached_as_zero():
    """The proxy documents a transient null-height row. It must be re-asked,
    not frozen into the cache as a sea-level ground."""
    gev = FakeGev(terrain=lambda lat, lon: None)
    provider = TerrainProvider(origin="http://test", fetch=gev,
                               fallback_ground_msl_m=250.0)
    sample = provider.height(48.6, 37.9)
    assert sample.real is False and sample.msl_m == pytest.approx(250.0)
    assert provider.cache_size() == 0  # nothing poisoned the cache
    assert "no height" in sample.provenance.reason


def test_non_blocking_read_makes_zero_http_calls():
    """NEVER BLOCK a mission or a telemetry tick on a network fetch."""
    gev = FakeGev(terrain=flat_terrain(100.0))
    provider = TerrainProvider(origin="http://test", fetch=gev,
                               fallback_ground_msl_m=122.0)
    sample = provider.cached(47.64, -122.14)
    assert gev.count == 0
    assert sample.real is False
    assert "network lookups are disabled" in sample.provenance.reason
    # Once warmed off the hot path, the same non-blocking read is real.
    provider.prefetch([(47.64, -122.14)]).join(timeout=5)
    warm = provider.cached(47.64, -122.14)
    assert warm.real is True and gev.count == 1


def test_true_agl_is_above_terrain_and_is_none_when_terrain_is_unknown():
    gev = FakeGev(terrain=flat_terrain(1500.0))
    provider = TerrainProvider(origin="http://test", fetch=gev)
    fix = provider.agl(32.6546, 51.668, 1560.0, datum="hae")
    assert fix.agl_m == pytest.approx(60.0)
    assert fix.real is True
    assert fix.alt_msl_m == pytest.approx(
        geo.hae_to_msl(1560.0, 32.6546, 51.668))

    blind = TerrainProvider(origin="http://test", fetch=failing_fetch())
    dark = blind.agl(32.6546, 51.668, 1560.0, datum="hae")
    assert dark.agl_m is None and dark.real is False
    assert dark.as_dict()["alt_agl_m"] is None  # never a confident zero


# ---------------------------------------------------------------------------
# Terrain-aware line of sight
# ---------------------------------------------------------------------------


def _ridge_terrain(ridge_lat, ridge_height, base=100.0, width_deg=0.004):
    def terrain(lat, lon):
        return ridge_height if abs(lat - ridge_lat) < width_deg else base
    return terrain


def test_line_of_sight_is_blocked_by_a_real_ridge_and_clear_without_it():
    observer = (32.600, 51.668, 400.0)   # 300 m over 100 m ground
    target = (32.700, 51.668, 150.0)
    blocked = TerrainProvider(
        origin="http://test", fetch=FakeGev(terrain=_ridge_terrain(32.650, 900.0)))
    clear = TerrainProvider(
        origin="http://test", fetch=FakeGev(terrain=flat_terrain(100.0)))

    hit = blocked.line_of_sight(observer, target)
    assert hit.los is False and hit.known is True
    assert hit.first_obstacle is not None
    assert hit.first_obstacle["terrain_hae_m"] == pytest.approx(900.0)
    assert hit.first_obstacle["obstruction_m"] > 0
    assert 0 < hit.first_obstacle["range_m"] < hit.distance_m
    assert hit.min_clearance_m < 0

    ok = clear.line_of_sight(observer, target)
    assert ok.los is True and ok.known is True and ok.first_obstacle is None
    assert ok.min_clearance_m > 0


def test_line_of_sight_model_states_what_was_actually_modelled():
    """TOOL_CONTRACT §4.2: a LOS answer the harness cannot trust is worse than
    none, so the model string must name the source AND its blind spots."""
    provider = TerrainProvider(origin="http://test",
                               fetch=FakeGev(terrain=flat_terrain(100.0)))
    result = provider.line_of_sight((32.60, 51.66, 400.0), (32.62, 51.66, 300.0))
    assert "terrain-profile" in result.model
    assert "vegetation, buildings" in result.model and "NOT modelled" in result.model
    assert "curvature" in result.model.lower()
    assert result.as_dict()["provenance"]["real"] is True


def test_line_of_sight_refuses_to_assert_sight_with_no_terrain_data():
    """Never an unconditional `true`: with nothing to model, LOS is not asserted."""
    provider = TerrainProvider(origin="http://test", fetch=failing_fetch())
    result = provider.line_of_sight((32.60, 51.66, 400.0), (32.70, 51.66, 300.0))
    assert result.los is False and result.known is False
    assert "CANNOT be asserted" in result.model
    assert result.provenance.degraded is True


def test_line_of_sight_over_a_synthetic_plane_is_flagged_degraded():
    provider = TerrainProvider(origin="http://test", fetch=failing_fetch(),
                               fallback_ground_msl_m=100.0)
    result = provider.line_of_sight((32.60, 51.66, 400.0), (32.62, 51.66, 380.0))
    assert result.known is False
    assert "SYNTHETIC/DEGRADED" in result.model
    assert "NOT a terrain LOS check" in result.model


def test_line_of_sight_accounts_for_earth_curvature():
    """Two aircraft at 100 m HAE, 120 km apart over flat 0 m ground: the bulge
    (~283 m at midpoint) blocks them. A flat-earth check would say `true`."""
    provider = TerrainProvider(origin="http://test",
                               fetch=FakeGev(terrain=flat_terrain(0.0)))
    far = provider.line_of_sight((30.0, 50.0, 100.0), (31.08, 50.0, 100.0),
                                 samples=64)
    assert far.distance_m > 110_000
    assert far.los is False and far.known is True
    near = provider.line_of_sight((30.0, 50.0, 100.0), (30.05, 50.0, 100.0),
                                  samples=64)
    assert near.los is True


def test_geofence_floor_is_the_highest_terrain_plus_clearance():
    provider = TerrainProvider(
        origin="http://test", fetch=FakeGev(terrain=_ridge_terrain(32.65, 900.0)))
    floor = provider.floor([(32.60, 51.66), (32.65, 51.66), (32.70, 51.66)],
                           clearance_agl_m=60.0)
    assert floor.real is True
    assert floor.floor_hae_m == pytest.approx(960.0)
    assert floor.highest.hae_m == pytest.approx(900.0)
    assert floor.lowest.hae_m == pytest.approx(100.0)

    blind = TerrainProvider(origin="http://test", fetch=failing_fetch())
    dark = blind.floor([(32.60, 51.66)], clearance_agl_m=60.0)
    assert dark.floor_hae_m is None and dark.real is False
    assert "cannot be set" in dark.provenance.reason


# ---------------------------------------------------------------------------
# (2) Mapped installations -> order of battle
# ---------------------------------------------------------------------------


def _ob_provider(payload=INSTALLATIONS_PAYLOAD, status=200):
    gev = FakeGev(installations=payload, status=status)
    return InstallationsProvider(origin="http://test", fetch=gev), gev


def test_order_of_battle_hydrates_from_mapped_sites_onto_real_ob_classes():
    provider, _ = _ob_provider()
    order = provider.order_of_battle(27.6, 85.3, 27.71, 85.45)

    assert order.real is True
    assert len(order.sites) == 6  # the positionless relation is dropped
    for site in order.sites:
        assert site.ob_class in targets.OB_LIBRARY
        assert site.category == targets.ob_class(site.ob_class).category
    names = {s.name for s in order.sites}
    assert "Surybinyak Army Barrack" in names and "Northern Airfield" in names


def test_ways_without_a_center_are_located_not_discarded():
    """GEV's own cached payload is 41 ways with `bounds`+`geometry` and NO
    `center` out of 51 elements — reading only `center` silently throws 84% of
    the mapped order of battle away."""
    provider, _ = _ob_provider()
    sites = {s.name: s for s in provider.order_of_battle(27.6, 85.2, 27.9, 85.45).sites}

    tundikhel = sites["Tundikhel"]
    assert tundikhel.position_source == "geometry-centroid"
    assert tundikhel.lat == pytest.approx((27.7022085 + 27.7021309) / 2)
    assert tundikhel.lon == pytest.approx((85.313759 + 85.31633) / 2)

    western = sites["Western Range"]
    assert western.position_source == "bounds-midpoint"
    assert (western.lat, western.lon) == pytest.approx((27.81, 85.22))

    assert sites["Northern Airfield"].position_source == "center"
    assert sites["Surybinyak Army Barrack"].position_source == "node"
    assert all(s.as_dict()["position_source"] for s in sites.values())
    # A malformed geometry is still a drop, not an origin fix.
    assert realdata._element_position({"type": "way", "geometry": [{"x": 1}]}) is None
    assert realdata._element_position({"type": "node", "lat": 200.0, "lon": 5.0}) is None


def test_a_site_with_no_position_is_dropped_and_counted_never_placed_at_origin():
    """The exact bug class the brief names: a `.get()` default that landed every
    spawned target at (0, 0)."""
    provider, _ = _ob_provider()
    order = provider.order_of_battle(27.6, 85.3, 27.71, 85.45)
    assert order.dropped_no_position == 1
    assert all(abs(s.lat) > 1e-6 and abs(s.lon) > 1e-6 for s in order.sites)
    assert "Ghost" not in {s.name for s in order.sites}
    assert order.as_dict()["dropped_no_position"] == 1


def test_site_classification_prefers_the_name_then_the_tag_then_a_safe_default():
    provider, _ = _ob_provider()
    sites = {s.name: s for s in provider.order_of_battle(27.6, 85.3, 27.71, 85.45).sites}

    radar = sites["Radar Station Alpha"]
    assert radar.ob_class == "radar_acquisition" and radar.ob_source == "name-match"
    airfield = sites["Northern Airfield"]
    assert airfield.ob_class == "structure" and airfield.ob_source == "osm-tag"
    office = [s for s in sites.values() if s.site_type == "office"][0]
    assert office.ob_class == "c2_node" and office.ob_source == "osm-tag"
    # An unmapped tag falls to `structure`, which asserts NO weapons envelope:
    # a mapped building can never invent a threat ring.
    ob_key, _, source = realdata.classify_site({"military": "nonsense_value"})
    assert (ob_key, source) == (realdata.DEFAULT_SITE_OB, "default")
    assert targets.ob_class(ob_key).weapon_range_m == 0.0


def test_mapped_data_is_labelled_as_mapped_everywhere_it_surfaces():
    """Requirement (2): no report may imply this is a real order of battle."""
    provider, _ = _ob_provider()
    order = provider.order_of_battle(27.6, 85.3, 27.71, 85.45)
    roster = order.as_dict()
    assert roster["mapped_data"] is True and roster["authoritative"] is False
    assert roster["caveat"] == MAPPED_DATA_CAVEAT
    lowered = MAPPED_DATA_CAVEAT.lower()
    assert "not an order of battle" in lowered
    assert "do not report it as a confirmed order of battle" in lowered
    assert "incomplete" in lowered and "unverified" in lowered
    assert order.provenance.caveat == MAPPED_DATA_CAVEAT
    assert order.provenance.attribution == ATTRIBUTION["installations"]
    assert "OpenStreetMap" in order.provenance.attribution and "ODbL" in \
        order.provenance.attribution
    for site in roster["sites"]:
        assert site["mapped_data"] is True and site["authoritative"] is False
        assert site["caveat"] == MAPPED_DATA_CAVEAT


def test_order_of_battle_puts_sites_on_real_ground_and_omits_unknown_altitude():
    """TOOL_CONTRACT §4.4: `sim_spawn_target` altitude defaulting to 0.0 buries
    targets ~1550 m underground. Terrain fixes it — and when terrain is unknown
    the key is OMITTED rather than guessed."""
    provider, _ = _ob_provider()
    terrain = TerrainProvider(origin="http://test",
                              fetch=FakeGev(terrain=flat_terrain(1300.0)))
    order = provider.order_of_battle(27.6, 85.3, 27.71, 85.45, terrain=terrain)
    request = order.spawn_requests()[0]
    assert request["ob_class"] in targets.OB_LIBRARY
    assert order.sites[0].alt_provenance()["alt_source"] == "terrain:gev"
    assert request["alt_msl_m"] == pytest.approx(
        geo.hae_to_msl(1300.0, order.sites[0].lat, order.sites[0].lon))

    blind_provider, _ = _ob_provider()
    blind = TerrainProvider(origin="http://test", fetch=failing_fetch())
    dark = blind_provider.order_of_battle(27.6, 85.3, 27.71, 85.45, terrain=blind)
    assert "alt_msl_m" not in dark.spawn_requests()[0]
    assert dark.sites[0].alt_source.startswith("UNKNOWN")


def test_installations_503_degrades_to_an_empty_roster_that_says_why():
    """The live failure observed while writing this: Overpass down -> HTTP 503."""
    provider, gev = _ob_provider(status=503)
    order = provider.order_of_battle(32.63, 51.63, 32.68, 51.71)
    assert order.sites == () and order.real is False
    assert "503" in order.provenance.reason
    assert "SYNTHETIC hand-placed laydown" in order.provenance.reason
    assert order.provenance.caveat == MAPPED_DATA_CAVEAT
    assert gev.count == 1


def test_installations_serve_last_good_roster_when_a_refresh_fails():
    payloads = {"n": 0}

    def fetch(url, timeout_s):
        payloads["n"] += 1
        if payloads["n"] == 1:
            return HttpResponse(200, json.dumps(INSTALLATIONS_PAYLOAD))
        raise RealDataUnavailable("http", "upstream gone")

    provider = InstallationsProvider(origin="http://test", fetch=fetch, ttl_s=0.0)
    first = provider.order_of_battle(27.6, 85.3, 27.71, 85.45)
    assert first.real is True
    stale = provider.order_of_battle(27.6, 85.3, 27.71, 85.45)
    assert len(stale.sites) == len(first.sites)   # the roster survives
    assert stale.real is False            # ... and is flagged
    assert "last-good" in stale.provenance.reason


# ---------------------------------------------------------------------------
# (3) Live air traffic -> deconfliction
# ---------------------------------------------------------------------------


def test_traffic_filters_to_the_ao_and_converts_only_the_geodetic_altitude():
    gev = FakeGev(traffic=OPENSKY_FRAME)
    provider = TrafficProvider(origin="http://test", fetch=gev)
    traffic = provider.contacts(46.839, 5.3571, 50_000.0)

    assert traffic.real is True
    assert [c.icao24 for c in traffic.contacts] == ["39de4f"]  # the other two are far
    contact = traffic.contacts[0]
    assert contact.callsign == "TVF8603"
    # geo_altitude is GNSS-geometric == ellipsoidal, so it converts (T1).
    assert contact.alt_hae_m == pytest.approx(11346.18)
    assert contact.alt_msl_m == pytest.approx(
        geo.hae_to_msl(11346.18, contact.lat, contact.lon))
    # baro_altitude is a pressure altitude on no geodetic datum: never converted.
    assert contact.alt_baro_m == pytest.approx(10972.8)
    assert "NOT a geodetic datum" in contact.as_dict()["alt_baro_note"]
    assert contact.range_m < 1_000.0


def test_traffic_carries_the_non_commercial_opensky_terms():
    gev = FakeGev(traffic=OPENSKY_FRAME, headers={"x-opensky-auth-reason": "oauth_token"})
    opensky = TrafficProvider(origin="http://test", fetch=gev).contacts(
        46.839, 5.3571, 50_000.0)
    assert "NON-COMMERCIAL" in opensky.provenance.attribution
    assert "opensky-network.org" in opensky.provenance.attribution

    fallback = FakeGev(traffic=OPENSKY_FRAME, headers={
        "x-opensky-auth-reason": "opensky_cooldown_regional_fallback"})
    adsb = TrafficProvider(origin="http://test", fetch=fallback).contacts(
        46.839, 5.3571, 50_000.0)
    assert adsb.provenance.attribution == ATTRIBUTION["traffic_adsblol"]
    assert "adsb.lol" in adsb.provenance.source

    # No header at all: we cannot tell which upstream served it, so both terms
    # travel rather than one being guessed.
    unknown = TrafficProvider(origin="http://test",
                              fetch=FakeGev(traffic=OPENSKY_FRAME)).contacts(
        46.839, 5.3571, 50_000.0)
    assert "OpenSky" in unknown.provenance.attribution
    assert "adsb.lol" in unknown.provenance.attribution


def test_deconfliction_treats_an_unknown_altitude_as_not_separated():
    frame = {"time": 1, "states": [
        ["aaa111", "CLOSE   ", "X", 1, 1, 51.6680, 32.6560, 1500.0, False,
         100.0, 90.0, 0.0, None, 1600.0, None, False, 0, 0],
        ["bbb222", "HIGH    ", "X", 1, 1, 51.6680, 32.6561, 9000.0, False,
         200.0, 90.0, 0.0, None, 9000.0, None, False, 0, 0],
        ["ccc333", "NOALT   ", "X", 1, 1, 51.6680, 32.6562, None, False,
         200.0, 90.0, 0.0, None, None, None, False, 0, 0],
    ]}
    traffic = TrafficProvider(origin="http://test",
                              fetch=FakeGev(traffic=frame)).contacts(
        32.6546, 51.6680, 50_000.0)
    conflicts = traffic.deconflict(32.6546, 51.6680, 1600.0,
                                   horizontal_m=9_260.0, vertical_m=300.0)
    hexes = [c["contact"]["icao24"] for c in conflicts]
    assert "aaa111" in hexes           # co-altitude, close
    assert "bbb222" not in hexes       # 7400 m above: separated
    assert "ccc333" in hexes           # unknown altitude: NOT assumed separated
    unknown = [c for c in conflicts if c["contact"]["icao24"] == "ccc333"][0]
    assert unknown["vertical_m"] is None
    assert "cannot be asserted" in unknown["basis"]


def test_traffic_drops_positionless_and_ground_contacts_without_inventing_them():
    frame = {"time": 1, "states": [
        ["nopos1", "GHOST   ", "X", 1, 1, None, None, 1000.0, False,
         10.0, 0.0, 0.0, None, 1000.0, None, False, 0, 0],
        ["ongrnd", "TAXI    ", "X", 1, 1, 51.6680, 32.6546, None, True,
         5.0, 0.0, 0.0, None, 20.0, None, False, 0, 0],
    ]}
    provider = TrafficProvider(origin="http://test", fetch=FakeGev(traffic=frame))
    assert provider.contacts(32.6546, 51.668, 50_000.0).contacts == ()
    with_ground = provider.contacts(32.6546, 51.668, 50_000.0, include_ground=True)
    assert [c.icao24 for c in with_ground.contacts] == ["ongrnd"]


def test_no_traffic_feed_is_an_empty_feed_not_a_clear_sky():
    traffic = TrafficProvider(origin="http://test", fetch=failing_fetch()).contacts(
        32.6546, 51.668, 50_000.0)
    assert traffic.contacts == () and traffic.real is False
    assert "not a clear sky" in traffic.provenance.reason


# ---------------------------------------------------------------------------
# (4) Weather -> the fuel model (M15) and sensor degradation (M18)
# ---------------------------------------------------------------------------


def test_weather_reads_the_real_observation_and_converts_the_units():
    gev = FakeGev(weather=ISFAHAN_WEATHER)
    observation = WeatherProvider(origin="http://test", fetch=gev).observe(
        32.6546, 51.668)
    assert observation.real is True
    assert observation.wind_speed_mps == pytest.approx(5.8 / 3.6)
    assert observation.wind_from_deg == 266
    assert observation.visibility_m == 68060
    assert observation.condition == "clear"
    assert observation.provenance.attribution == ATTRIBUTION["weather"]
    assert "Open-Meteo" in observation.provenance.attribution
    assert observation.observed_at_ms == 1789671600000


def test_wind_vector_is_the_ned_air_mass_velocity_the_fuel_model_wants():
    """M15. Open-Meteo reports the direction wind comes FROM; `safety` wants the
    vector the air mass moves ALONG. Getting this backwards inverts every
    headwind in the fuel model."""
    north, east = realdata.wind_ne_mps(10.0, 270.0)   # a westerly
    assert north == pytest.approx(0.0, abs=1e-9)
    assert east == pytest.approx(10.0)                # air moves EAST
    # Flying east into a westerly is a TAILWIND; flying west is a headwind.
    assert headwind_component_mps(north, east, 90.0) == pytest.approx(-10.0)
    assert headwind_component_mps(north, east, 270.0) == pytest.approx(10.0)
    n2, e2 = realdata.wind_ne_mps(10.0, 180.0)        # a southerly
    assert n2 == pytest.approx(10.0) and e2 == pytest.approx(0.0, abs=1e-9)


def test_real_wind_actually_changes_the_fuel_burn():
    """End to end: a real observation -> wind_ne -> FuelModel (M15)."""
    weather = WeatherProvider(origin="http://test",
                              fetch=FakeGev(weather={"weather": {
                                  "temperatureC": 20.0, "windKph": 54.0,
                                  "windDirectionDeg": 90.0, "visibilityM": 20000,
                                  "cloudCoverPct": 0, "precipitationMm": 0,
                                  "weatherCode": 0}})).observe(32.6546, 51.668)
    assert weather.wind_speed_mps == pytest.approx(15.0)

    def cruise(**kwargs):
        model = FuelModel()
        for step in range(31):
            model.tick(20.0, 0.0, False, now=step * 2.0, **kwargs)
        return model

    calm = cruise()
    windy = cruise(wind_ne=weather.wind_ne, track_deg=90.0)
    tail = cruise(wind_ne=weather.wind_ne, track_deg=270.0)
    # Wind from 090 (an easterly): flying east is a headwind, west a tailwind.
    assert windy.last_headwind_mps == pytest.approx(15.0)
    assert tail.last_headwind_mps == pytest.approx(-15.0)
    assert windy.fuel_pct < calm.fuel_pct
    assert tail.fuel_pct == pytest.approx(calm.fuel_pct)


def test_sensor_degradation_keys_stay_compatible_with_the_targets_model():
    """M18. If either side gains or renames a condition this fails instead of
    silently producing a condition `targets` has never heard of."""
    assert set(realdata.WEATHER_SENSOR_FACTOR) == set(targets._WEATHER_FACTOR)
    for key, value in realdata.WEATHER_SENSOR_FACTOR.items():
        assert value == pytest.approx(targets._WEATHER_FACTOR[key])


def test_condition_takes_the_worse_of_the_code_and_the_visibility():
    assert realdata.condition_from(0, 20000) == "clear"
    assert realdata.condition_from(45, 20000) == "fog"        # code says fog
    assert realdata.condition_from(0, 800) == "fog"           # visibility says fog
    assert realdata.condition_from(0, 3000) == "haze"
    assert realdata.condition_from(71, 20000) == "snow"
    assert realdata.condition_from(7, 20000) == "dust"
    # A clear code never upgrades a bad visibility report.
    assert realdata.condition_from(1, 500) == "fog"
    assert realdata.WEATHER_SENSOR_FACTOR[realdata.condition_from(45, 20000)] < 1.0


def test_sim_weather_payload_matches_the_sim_set_weather_signature():
    """TOOL_CONTRACT §4.4 — the payload is handed straight to the tool."""
    foggy = WeatherProvider(origin="http://test", fetch=FakeGev(weather={"weather": {
        "temperatureC": 5.0, "windKph": 18.0, "windDirectionDeg": 0.0,
        "visibilityM": 500, "cloudCoverPct": 100, "precipitationMm": 2.5,
        "weatherCode": 45}})).observe(48.6, 37.9)
    payload = foggy.sim_weather_payload()
    assert set(payload) == {"rain", "snow", "fog", "dust", "wind_north_mps",
                            "wind_east_mps", "wind_down_mps"}
    assert 0.0 <= payload["fog"] <= 1.0 and payload["fog"] > 0.8
    assert all(0.0 <= payload[k] <= 1.0 for k in ("rain", "snow", "fog", "dust"))
    assert payload["wind_north_mps"] == pytest.approx(-5.0)  # from 0 -> moving south
    assert foggy.condition == "fog" and foggy.sensor_factor < 0.5


def test_weather_failure_is_calm_and_clear_but_flagged_as_an_assumption():
    observation = WeatherProvider(origin="http://test",
                                  fetch=failing_fetch("timed out")).observe(48.6, 37.9)
    assert observation.real is False
    assert observation.wind_ne == (0.0, 0.0) and observation.condition == "clear"
    assert "SYNTHETIC calm/clear" in observation.provenance.reason
    assert "NOT observed" in observation.provenance.reason
    assert observation.as_dict()["provenance"]["degraded"] is True


# ---------------------------------------------------------------------------
# (5) Theater hydration
# ---------------------------------------------------------------------------


def _full_gev():
    return FakeGev(terrain=flat_terrain(1580.0), weather=ISFAHAN_WEATHER,
                   traffic=OPENSKY_FRAME, installations=INSTALLATIONS_PAYLOAD)


def test_a_theater_is_static_until_it_is_hydrated_and_says_so():
    theater = theaters.get("iran-isfahan")
    assert theater.real_data() is None
    block = theater.as_dict()["real_data"]
    assert block["hydrated"] is False and block["real"] is False
    assert block["source"] == "static-table"
    assert "not measured terrain" in block["note"]
    assert theater.ground_msl_m() == (1570.0, False)
    assert "iran-isfahan" in theaters.hydration_status()["static_only"]


def test_hydration_fills_every_feed_and_leaves_the_static_table_alone():
    theater = theaters.get("iran-isfahan")
    client = RealWorldData(origin="http://test", fetch=_full_gev())
    result = theater.hydrate(client)

    assert result.real is True and result.degraded_feeds == []
    assert result.ground.hae_m == pytest.approx(1580.0)
    assert result.weather.real and result.order_of_battle.real and result.traffic.real
    assert result.floor is not None and result.floor.floor_hae_m == pytest.approx(1640.0)
    # The static table is untouched — hydration reports, it does not rewrite.
    assert theaters.get("iran-isfahan").home_alt_msl_m == 1570.0
    assert result.static_home_msl_m == 1570.0
    assert result.terrain_delta_m() == pytest.approx(
        geo.hae_to_msl(1580.0, theater.home_lat, theater.home_lon) - 1570.0)
    assert "never applied" in result.as_dict()["terrain_delta_note"]
    # ... and it is now readable without blocking.
    assert theater.real_data() is result
    assert theater.ground_msl_m()[1] is True


def test_hydrated_theater_row_carries_the_provenance_and_the_attribution():
    theater = theaters.get("iran-isfahan")
    theater.hydrate(RealWorldData(origin="http://test", fetch=_full_gev()))
    block = theater.as_dict()["real_data"]
    assert block["hydrated"] is True and block["real"] is True
    assert block["degraded_feeds"] == []
    joined = " | ".join(block["attribution"])
    assert "Re:Earth" in joined and "OpenStreetMap" in joined and "Open-Meteo" in joined
    assert any("NON-COMMERCIAL" in line for line in block["attribution"])
    assert MAPPED_DATA_CAVEAT in block["limits"]
    assert any("only the environment is real" in line for line in block["limits"])
    payload = theaters.as_payload()
    assert payload["real_data"]["hydrated"] == ["iran-isfahan"]
    assert "iran-isfahan" not in payload["real_data"]["static_only"]


def test_partial_outage_degrades_only_the_feed_that_failed():
    """One dead upstream must not take the others down, and must be named."""
    class PartlyDown(FakeGev):
        def __call__(self, url, timeout_s):
            if "/api/military-installations" in url:
                self.calls.append(url)
                raise RealDataUnavailable("http", "Overpass unavailable")
            return super().__call__(url, timeout_s)

    gev = PartlyDown(terrain=flat_terrain(1580.0), weather=ISFAHAN_WEATHER,
                     traffic=OPENSKY_FRAME)
    result = theaters.get("iran-isfahan").hydrate(
        RealWorldData(origin="http://test", fetch=gev))
    assert result.real is False
    assert result.degraded_feeds == ["installations"]
    assert result.ground.real and result.weather.real and result.traffic.real
    assert "Overpass unavailable" in result.order_of_battle.provenance.reason
    assert theaters.hydration_status()["degraded_feeds"] == {
        "iran-isfahan": ["installations"]}


def test_total_outage_hydrates_to_the_declared_synthetic_fallback():
    theater = theaters.get("ukraine-donbas")
    result = theater.hydrate(RealWorldData(origin="http://test",
                                           fetch=failing_fetch("connection refused")))
    assert result.real is False
    assert result.degraded_feeds == ["installations", "terrain", "traffic", "weather"]
    # The theater's own MSL is the declared fallback plane — the pre-existing
    # height-above-takeoff behaviour, now visibly flagged.
    assert result.ground.msl_m == pytest.approx(250.0)
    assert result.ground.real is False
    assert "SYNTHETIC flat ground plane" in result.ground.provenance.reason
    # NOT 0.0. The "measured" ground here is the static table's own value, so a
    # subtraction fabricates a perfect agreement out of a total outage. See
    # test_terrain_delta_is_unknown_when_the_ground_was_never_measured.
    assert result.terrain_delta_m() is None
    assert theater.ground_msl_m() == (250.0, False)


def test_hydration_never_leaks_the_fallback_plane_back_into_the_provider():
    """The per-theater fallback is scoped to one hydration; a second theater
    must not inherit the first theater's ground plane."""
    client = RealWorldData(origin="http://test", fetch=failing_fetch())
    assert client.terrain.fallback_ground_msl_m is None
    theaters.get("iran-isfahan").hydrate(client)
    assert client.terrain.fallback_ground_msl_m is None
    donbas = theaters.get("ukraine-donbas").hydrate(client)
    assert donbas.ground.msl_m == pytest.approx(250.0)  # not 1570.0


def test_terrain_sample_points_stay_inside_the_ao():
    for theater in theaters.all_theaters():
        points = theater.terrain_sample_points()
        assert len(points) >= len(theater.ao)
        assert len(points) <= realdata.TERRAIN_MAX_POINTS
        for lat, lon in points:
            assert theater.contains(lat, lon)


def test_async_hydration_does_not_block_the_caller():
    started = threading.Event()
    release = threading.Event()
    inner = _full_gev()

    def slow(url, timeout_s):
        started.set()
        release.wait(5.0)
        return inner(url, timeout_s)

    theater = theaters.get("iran-isfahan")
    thread = theater.hydrate_async(RealWorldData(origin="http://test", fetch=slow))
    assert started.wait(5.0)
    # The fetch is still parked and the caller is already here: the non-blocking
    # read serves the static default rather than waiting.
    assert theater.real_data() is None
    assert theater.ground_msl_m() == (1570.0, False)
    release.set()
    thread.join(timeout=10)
    assert theater.real_data() is not None


def test_background_refresher_stops_cleanly():
    results = []
    client = RealWorldData(origin="http://test", fetch=_full_gev())
    refresher = realdata.BackgroundRefresher(
        client, interval_s=0.01, on_result=results.append,
        theater_id="iran-isfahan", home_lat=32.6546, home_lon=51.668,
        bbox=(32.63, 51.63, 32.68, 51.71), static_home_msl_m=1570.0)
    refresher.start()
    deadline = time.monotonic() + 5.0
    while not results and time.monotonic() < deadline:
        time.sleep(0.01)
    refresher.stop()
    assert results and results[0].theater_id == "iran-isfahan"
    refresher.stop()  # idempotent


def test_hydrate_all_completes_even_when_every_upstream_is_down():
    client = RealWorldData(origin="http://test", fetch=failing_fetch())
    out = theaters.hydrate_all(client, ["iran-isfahan", "taiwan-strait"])
    assert set(out) == {"iran-isfahan", "taiwan-strait"}
    assert all(not data.real for data in out.values())
    assert theaters.hydration_status()["hydrated"] == ["iran-isfahan", "taiwan-strait"]
    theaters.clear_hydration("iran-isfahan")
    assert theaters.hydration_status()["hydrated"] == ["taiwan-strait"]


# ---------------------------------------------------------------------------
# The default HTTP client, over a loopback server (port range 48100-48199).
# ---------------------------------------------------------------------------


def test_default_http_client_talks_to_a_real_http_server():
    """`urllib_fetch` is the production path; exercise it for real, on loopback,
    so the one piece the injected fake never covers is not untested."""
    body = json.dumps({"results": [REDMOND_ROW]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's own name
            # The proxy answers 400 on a bad points parameter; stand in for that
            # with one coordinate so the non-200 path is exercised for real.
            if "points=" not in self.path or "2.00000%2C1.00000" in self.path:
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Test-Header", "present")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = None
    for port in range(48101, 48111):
        try:
            server = HTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    assert server is not None, "no free port in the assigned 48100-48199 range"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        response = realdata.urllib_fetch(f"{origin}/api/terrain/heights?points=x", 5.0)
        assert response.status == 200 and response.header("x-test-header") == "present"

        sample = TerrainProvider(origin=origin).height(
            REDMOND_ROW["lat"], REDMOND_ROW["lon"])
        assert sample.real is True
        assert sample.hae_m == pytest.approx(REDMOND_ROW["ellipsoid"])

        # A non-200 becomes a flagged value, not an exception and not a zero.
        bad = TerrainProvider(origin=origin).height(1.0, 2.0)
        assert bad.real is False and bad.hae_m is None and "400" in bad.provenance.reason
        # A dead origin is a transport failure, also flagged, also not fatal.
        dead = TerrainProvider(origin="http://127.0.0.1:48199").height(1.0, 2.0)
        assert dead.real is False and dead.provenance.reason
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_attribution_lines_cover_every_feed():
    lines = realdata.attribution_lines()
    assert len(lines) == len(ATTRIBUTION)
    assert any("CC BY 4.0" in line for line in lines)
    assert any("ODbL" in line for line in lines)
    assert any("NON-COMMERCIAL" in line for line in lines)
    assert realdata.attribution_lines(["weather"]) == [ATTRIBUTION["weather"]]


def test_great_circle_interpolation_is_actually_on_the_path():
    mid = realdata._interpolate(0.0, 0.0, 0.0, 10.0, 0.5)
    assert mid[0] == pytest.approx(0.0, abs=1e-9) and mid[1] == pytest.approx(5.0)
    start = realdata._interpolate(32.0, 51.0, 33.0, 52.0, 0.0)
    assert start == pytest.approx((32.0, 51.0))
    assert not math.isnan(realdata._interpolate(1.0, 1.0, 1.0, 1.0, 0.5)[0])


# ---------------------------------------------------------------------------
# Adversarial verification (Wave 3). Each test below reproduces a defect that
# the first implementation shipped: a value that was silently laundered as
# real, a shared mutable that two hydration threads fought over, and a
# discrepancy number fabricated out of the fallback it was meant to measure.
# Every one of these FAILED before the fix in the same commit.
# ---------------------------------------------------------------------------


def test_terrain_past_its_ttl_with_a_failed_refresh_is_flagged_stale_not_real():
    """A cache entry past its TTL whose refresh FAILED must not come back
    `real=True, degraded=False`. Installations/traffic/weather all say
    "refresh failed; serving the last-good"; terrain silently did not, so a
    height of any age read as a live measurement."""
    clock = [1_000_000.0]
    calls = []

    def fetch(url, timeout_s):
        calls.append(url)
        if len(calls) == 1:
            return HttpResponse(200, json.dumps({"results": [REDMOND_ROW]}))
        return HttpResponse(503, json.dumps({"error": "upstream down"}))

    provider = TerrainProvider(origin="http://test", fetch=fetch, ttl_s=10.0,
                               now=lambda: clock[0])
    fresh = provider.height(REDMOND_ROW["lat"], REDMOND_ROW["lon"])
    assert fresh.real is True and fresh.provenance.degraded is False

    clock[0] += 10_000.0                       # 1000x past the TTL
    stale = provider.height(REDMOND_ROW["lat"], REDMOND_ROW["lon"])
    # The height itself is still served — terrain does not move — but it is
    # no longer presented as a live reading.
    assert stale.hae_m == pytest.approx(REDMOND_ROW["ellipsoid"])
    assert stale.real is False
    assert stale.provenance.degraded is True
    assert "503" in stale.provenance.reason
    assert "stale" in stale.provenance.reason.lower()
    assert stale.provenance.age_s == pytest.approx(10_000.0)
    # And a LOS or a floor built on stale samples inherits the flag.
    assert provider.floor([(REDMOND_ROW["lat"], REDMOND_ROW["lon"])],
                          clearance_agl_m=60.0).real is False


def test_a_successful_refresh_after_the_ttl_is_real_again():
    """The stale flag tracks the refresh, not the age: once the upstream
    answers again the sample is real with a fresh age."""
    clock = [1_000_000.0]
    gev = FakeGev(terrain=flat_terrain(89.0))
    provider = TerrainProvider(origin="http://test", fetch=gev, ttl_s=10.0,
                               now=lambda: clock[0])
    assert provider.height(47.64, -122.14).real is True
    clock[0] += 100.0
    again = provider.height(47.64, -122.14)
    assert again.real is True and again.provenance.age_s == pytest.approx(0.0)
    assert gev.count == 2


def test_concurrent_hydrations_do_not_swap_each_others_ground_plane():
    """The per-theater fallback plane was a shared attribute on the provider,
    set and restored around each hydration. `hydrate_async()` and
    `BackgroundRefresher` are the module's own concurrent entry points, so two
    theaters hydrating at once gave one of them the OTHER theater's datum —
    here a sea-level theater's 0 m handed to a 1293 m plateau — and left the
    provider permanently poisoned afterwards."""
    barrier = threading.Barrier(2, timeout=10.0)

    def fetch(url, timeout_s):
        barrier.wait()          # force both hydrations to overlap
        raise RealDataUnavailable("http", "connection refused")

    client = RealWorldData(origin="http://test", fetch=fetch)
    results: dict[str, float | None] = {}
    errors: list[BaseException] = []

    def run(theater_id):
        try:
            results[theater_id] = theaters.get(theater_id).hydrate(client).ground.msl_m
        except BaseException as exc:            # noqa: BLE001 — surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(tid,))
               for tid in ("taiwan-strait", "iran-natanz")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not errors, errors

    # Each theater falls back to ITS OWN declared datum, not the other's.
    # Read from the table rather than repeated as a literal: this assertion
    # pinned 1580.0, which was the Natanz row's declared ground until it was
    # measured and found 286.5 m too high. Pinning the value under test to the
    # table keeps the property (own datum, not the other's) and stops this
    # test voting on what the altitude should be.
    assert results["taiwan-strait"] == pytest.approx(
        theaters.get("taiwan-strait").home_alt_msl_m)
    assert results["iran-natanz"] == pytest.approx(
        theaters.get("iran-natanz").home_alt_msl_m)
    assert results["taiwan-strait"] != results["iran-natanz"]
    # ...and nothing leaks back onto the shared provider.
    assert client.terrain.fallback_ground_msl_m is None


def test_terrain_delta_is_unknown_when_the_ground_was_never_measured():
    """`terrain_delta_m()` is "measured terrain minus the static table". With
    the upstream down the "measured" ground IS the static table's own value,
    so the old code reported a delta of exactly 0.0 — which reads as "the
    hand-entered altitude is spot on" precisely when nothing was measured."""
    theater = theaters.get("iran-fordow")
    client = RealWorldData(origin="http://test", fetch=failing_fetch())
    hydrated = theater.hydrate(client)
    assert "terrain" in hydrated.degraded_feeds
    assert hydrated.ground.real is False
    # the fallback plane IS the theater's declared ground, whatever it is
    assert hydrated.ground.msl_m == pytest.approx(theater.home_alt_msl_m)
    assert hydrated.terrain_delta_m() is None
    assert hydrated.as_dict()["terrain_delta_m"] is None


def test_terrain_delta_is_reported_when_the_ground_really_was_measured():
    """The counterpart: a real measurement still yields the discrepancy, which
    is the whole point of the field.

    The measured height is set 650 m above the theater's DECLARED ground — the
    size of the error this table really carried at Fordow (1550 m declared,
    902.8 m measured) until `test_theaters.py` started pinning declared ground
    against recorded terrain. Derived from the table rather than hard-coded, so
    correcting an altitude cannot silently reduce this to a delta of zero,
    which is the one answer the field must never give when it did measure."""
    theater = theaters.get("iran-fordow")
    measured_msl = theater.home_alt_msl_m + 650.0
    gev = FakeGev(terrain=flat_terrain(
        geo.msl_to_hae(measured_msl, theater.home_lat, theater.home_lon)))
    client = RealWorldData(origin="http://test", fetch=gev)
    hydrated = theater.hydrate(client)
    assert hydrated.ground.real is True
    delta = hydrated.terrain_delta_m()
    assert delta is not None and delta == pytest.approx(650.0, abs=0.5)


def test_a_real_observation_with_no_wind_is_not_reported_as_calm_weather():
    """M15. `wind_ne` is a plain tuple with no provenance of its own, so an
    observation that carried no wind field returned (0.0, 0.0) — a fabricated
    dead calm — under `real=True`. The fuel model cannot tell that apart from
    a measured calm."""
    payload = {"weather": {"temperatureC": 31.0, "cloudCoverPct": 0,
                           "visibilityM": 24000, "weatherCode": 0,
                           "precipitationMm": 0,
                           "observedAt": "2026-09-17T12:00:00.000Z"}}
    observation = WeatherProvider(origin="http://test",
                                  fetch=FakeGev(weather=payload)).observe(34.9, 51.0)
    assert observation.wind_speed_mps is None
    assert observation.wind_ne == (0.0, 0.0)          # the value is still calm...
    assert observation.wind_known is False            # ...but it says it is a guess
    assert observation.real is False
    assert "wind" in observation.provenance.reason.lower()
    assert observation.as_dict()["wind_known"] is False


def test_a_precipitation_condition_with_no_measured_rate_says_so():
    """WMO code 63 is moderate rain. With no `precipitationMm` the obscurant
    intensity in `sim_set_weather` falls out as 0.0 — indistinguishable from a
    measured dry spell — so the observation must not claim to be real."""
    payload = {"weather": {"weatherCode": 63, "visibilityM": 8000,
                           "windKph": 18.0, "windDirectionDeg": 270}}
    observation = WeatherProvider(origin="http://test",
                                  fetch=FakeGev(weather=payload)).observe(34.9, 51.0)
    assert observation.condition == "rain"
    assert observation.sim_weather_payload()["rain"] == 0.0
    assert observation.real is False
    assert "precipitation" in observation.provenance.reason.lower()


def test_a_complete_observation_stays_real():
    """The guard above must not degrade a healthy observation."""
    observation = WeatherProvider(origin="http://test",
                                  fetch=FakeGev(weather=ISFAHAN_WEATHER)).observe(
        32.6546, 51.668)
    assert observation.real is True and observation.wind_known is True
    assert observation.as_dict()["wind_known"] is True


def test_a_partially_failed_terrain_refresh_reports_the_real_error():
    """Two batches, the second 500s. The points from the failed batch used to
    come back blaming "the upstream returned no height for this point" — the
    transient-null case — instead of the HTTP error that actually happened."""
    calls = []

    def fetch(url, timeout_s):
        calls.append(url)
        if len(calls) == 1:
            return HttpResponse(200, json.dumps({"results": [
                {"lon": 51.0, "lat": 34.0, "ellipsoid": 900.0, "elevation": 899.0},
                {"lon": 51.0, "lat": 34.001, "ellipsoid": 901.0, "elevation": 900.0}]}))
        return HttpResponse(500, json.dumps({"error": "boom"}))

    provider = TerrainProvider(origin="http://test", fetch=fetch, batch_points=2)
    samples = provider.heights([(34.0, 51.0), (34.001, 51.0),
                                (34.002, 51.0), (34.003, 51.0)])
    assert [s.real for s in samples] == [True, True, False, False]
    assert [s.hae_m for s in samples][:2] == [900.0, 901.0]
    for failed in samples[2:]:
        assert failed.hae_m is None
        assert "500" in failed.provenance.reason


def test_the_spawn_payload_actually_binds_to_the_servers_sim_spawn_target():
    """`spawn_request()` claims to be `sim_spawn_target`'s arguments. It was
    not: it emitted `class` (the tool spells it `ob_class`, and Python cannot
    pass a keyword called `class` at all) plus an `alt_source` the tool does
    not take, so `sim_spawn_target(**request)` raised TypeError on two counts.
    Nothing in production called it, which is why it went unnoticed.

    This reads the signature out of `server.py` with `ast` — no import, no
    server instance, and no edit to a file this agent does not own.
    """
    source = (pathlib.Path(realdata.__file__).parent / "server.py").read_text()
    signature = None
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "sim_spawn_target"):
            signature = node.args
            break
    if signature is None:
        pytest.skip("sim_spawn_target not found in server.py — the spawn payload "
                    "contract is UNVERIFIED, not verified-clean")
    accepted = {a.arg for a in
                [*signature.posonlyargs, *signature.args, *signature.kwonlyargs]}
    assert signature.kwarg is None, "the tool takes **kwargs; tighten this test"

    provider, _ = _ob_provider()
    terrain = TerrainProvider(origin="http://test",
                              fetch=FakeGev(terrain=flat_terrain(1300.0)))
    order = provider.order_of_battle(27.6, 85.3, 27.71, 85.45, terrain=terrain)
    assert order.sites
    for request in order.spawn_requests():
        unknown = sorted(set(request) - accepted)
        assert not unknown, f"sim_spawn_target(**request) would TypeError on {unknown}"
        # And every key is a legal Python keyword, which "class" is not.
        for key in request:
            assert key.isidentifier() and not keyword.iskeyword(key)
    # The altitude provenance is still reachable — moved, not dropped.
    assert order.sites[0].alt_provenance()["alt_is_real"] is True


def test_a_measured_dead_calm_is_not_mistaken_for_a_missing_wind():
    """0.0 m/s is falsy. A measured calm must stay `real` and `wind_known`, or
    the incompleteness guard above would flag every genuinely still day."""
    payload = {"weather": {"temperatureC": 12.0, "cloudCoverPct": 10,
                           "precipitationMm": 0, "visibilityM": 30000,
                           "windKph": 0.0, "windDirectionDeg": 0.0,
                           "weatherCode": 0}}
    observation = WeatherProvider(origin="http://test",
                                  fetch=FakeGev(weather=payload)).observe(34.9, 51.0)
    assert observation.wind_known is True
    assert observation.real is True
    assert observation.wind_ne == pytest.approx((0.0, 0.0), abs=1e-9)
