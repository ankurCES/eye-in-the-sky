"""Real-world data ingestion for godSeye — terrain, order of battle, air
traffic and weather (REAL_DATA_INTEGRATION.md).

Until this module existed godSeye ingested **nothing**: theaters were hardcoded
lat/lon boxes, "AGL" everywhere actually meant *height above the takeoff point*
because there was no terrain model at all, line-of-sight was a straight-line
assertion, and the wind vector that drives the fuel model (M15) was whatever the
operator typed in. This module is the ingestion layer that makes the environment
real while the vehicle stays simulated.

Feeds, in the order they earn their keep:

1. **Terrain elevation** (`TerrainProvider`) — the big one. It converts three
   fake quantities into real ones: true AGL, terrain-aware LOS, and a geofence
   *floor*. Source: God's Eye View's `GET /api/terrain/heights?points=lon,lat;…`
   proxy (Re:Earth / Mapterhorn, CC BY 4.0), which is already disk+memory
   cached, single-flight, and serves stale points rather than failing.
2. **Mapped military installations** (`InstallationsProvider`) — a plausible
   order of battle anchored to real mapped sites, via GEV's
   `GET /api/military-installations?south=&west=&north=&east=` (OSM/Overpass,
   ODbL). This is **mapped, incomplete, unverified** data: every site and every
   roster carries `MAPPED_DATA_CAVEAT` so no report can imply it is an
   authoritative order of battle.
3. **Live air traffic** (`TrafficProvider`) — real aircraft in the AO as
   airspace contacts a mission can deconflict against, via GEV's
   `GET /api/opensky?lat=&lon=` (OpenSky primary, adsb.lol regional fallback).
4. **Weather and wind** (`WeatherProvider`) — via GEV's
   `GET /api/weather-effects?latitude=&longitude=` (Open-Meteo, CC BY 4.0).
   Produces the NED wind vector `safety.FuelModel` wants (M15) and the sensor
   degradation factor `targets` uses (M18).

DATUM (T1). The terrain upstream returns **ellipsoidal** heights (`ellipsoid`)
*and* its own orthometric `elevation` computed against EGM2008. godSeye's datum
rule is that `geo.canonical_altitude()` is the ONE conversion point, over EGM96.
So this module takes the **ellipsoidal** height — which is datum-independent —
and derives MSL through `canonical_altitude(..., datum="hae")`. It never copies
the upstream's `elevation`, which is a different geoid: at Redmond the two
disagree by ~0.46 m and at other points by several metres. The upstream value is
kept as `upstream_elevation_m` purely so that divergence stays visible instead
of being laundered into our datum.

DESIGN RULES, which are the whole point of this module:

* **Never block a mission or a telemetry tick on a network fetch.** Every
  provider has a `cached(...)` / `allow_network=False` read that touches memory
  only, and `prefetch()` / `BackgroundRefresher` do the fetching off the hot
  path. Caches are aggressive (terrain does not move: 30-day TTL) and timeouts
  are short.
* **Fail soft, but VISIBLE.** Every value this module returns carries a
  `Provenance` saying whether it is real, what produced it, and — when it is
  not real — *why*. A synthetic value is never returned without a reason;
  `Provenance.__post_init__` refuses to construct one. This is deliberate: the
  worst bugs in this codebase were a `.get()` default that put every spawned
  target at the origin and a bare `except` that made the real geoid dead code
  for the whole project. A missing terrain height here becomes `None` or an
  explicitly-flagged fallback plane, never `0.0`.
* **Respect provider terms.** Requests go through GEV's proxies so its caching,
  attribution and rate-limit handling are not bypassed. `ATTRIBUTION` travels
  with every value. OpenSky is licensed for **non-commercial research and
  education only**; adsb.lol and OSM are ODbL; Open-Meteo and the terrain source
  are CC BY 4.0. The optional direct-upstream adsb.lol path is off by default
  and rate-gated.
* **ISR-only (M14).** Mapped installations are observation and reporting
  context. Nothing here targets anything.

Tests never touch the network: the HTTP client is injected (`fetch=`).
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import targets as ob
from .geo import GeoidUnavailableError, canonical_altitude
from .safety import haversine_m

_LOG = logging.getLogger(__name__)

SCHEMA = "godseye.realdata/v1"

# ---------------------------------------------------------------------------
# Provider terms. These strings travel with the data; they are not decoration.
# ---------------------------------------------------------------------------

#: Attribution required by each upstream, keyed by feed.
ATTRIBUTION: Mapping[str, str] = {
    "terrain": ("Terrain: Re:Earth / Mapterhorn terrain heights (CC BY 4.0); "
                "geoid EGM2008 (NGA, public domain)"),
    "installations": ("Mapped sites: (c) OpenStreetMap contributors, ODbL, "
                      "via Overpass API"),
    "traffic_opensky": ("Air traffic: The OpenSky Network (opensky-network.org) "
                        "- licensed for NON-COMMERCIAL research and education use only"),
    "traffic_adsblol": "Air traffic: adsb.lol (ODbL)",
    "weather": "Weather data by Open-Meteo.com (CC BY 4.0)",
}

#: Stamped on every mapped installation and every hydrated order of battle.
#: REAL_DATA_INTEGRATION.md "Honest limits to document": mapped data is context,
#: not an authoritative order of battle, and any report using it must say so.
MAPPED_DATA_CAVEAT = (
    "MAPPED DATA, NOT AN ORDER OF BATTLE: sites come from OpenStreetMap/Overpass "
    "mapped features. The coverage is incomplete, the tagging is unverified, and "
    "nothing here is confirmed by observation. Use as ISR context only (M14); do "
    "not report it as a confirmed order of battle."
)

# ---------------------------------------------------------------------------
# Endpoints and limits, read off the GEV providers rather than guessed.
# ---------------------------------------------------------------------------

#: Default God's Eye View dev-server origin (its Vite middleware hosts the proxies).
DEFAULT_GEV_ORIGIN = "http://localhost:5199"

#: Environment override for the origin above.
GEV_ORIGIN_ENV = "GODSEYE_GEV_ORIGIN"

#: Hard per-request cap enforced by gods-eye-view/server/providers/terrain.js
#: (`MAX_POINTS = 2000`); exceeding it is answered with HTTP 500, not a clamp.
TERRAIN_MAX_POINTS = 2000

#: Points per request we actually send. The proxy chunks upstream at 64 points
#: costing 5.6-12 s per chunk (its own measured figure), so a 2000-point request
#: would sit for minutes on a cold cache. 128 keeps one request inside
#: TERRAIN_TIMEOUT_S at two chunks while still amortising the round trip.
TERRAIN_BATCH_POINTS = 128

#: Decimal places the GEV proxy keys its cache on (`TERRAIN_POINT_PRECISION`).
#: Matching it means our cache key and its cache key describe the same point.
TERRAIN_POINT_DECIMALS = 5

#: Terrain does not move. GEV keeps 30 days on disk; so do we, in memory.
TERRAIN_TTL_S = 30 * 86_400.0

#: Cold terrain fetches are slow by design (see TERRAIN_BATCH_POINTS). This is a
#: startup/background budget, never a telemetry-tick budget.
TERRAIN_TIMEOUT_S = 25.0

#: Short-lived feeds: the caller is usually a background refresh.
WEATHER_TIMEOUT_S = 8.0
TRAFFIC_TIMEOUT_S = 8.0
INSTALLATIONS_TIMEOUT_S = 30.0

#: Cache TTLs for the live feeds, matched to the GEV proxies' own TTLs so we do
#: not ask more often than they will answer from upstream anyway.
WEATHER_TTL_S = 300.0          # weather-effects proxy caches 5 min
TRAFFIC_TTL_S = 12.0           # opensky proxy caches ~9 s, adsb.lol 12 s
INSTALLATIONS_TTL_S = 86_400.0  # mapped sites change on a survey timescale

#: Mean Earth radius used for the line-of-sight curvature correction, metres.
EARTH_MEAN_RADIUS_M = 6_371_008.8

#: Default ground spacing between LOS profile samples, metres. ~1 SRTM3 posting.
LOS_SAMPLE_SPACING_M = 90.0
LOS_MIN_SAMPLES = 8
LOS_MAX_SAMPLES = 256

#: Default deconfliction box around the ownship (ICAO-ish en-route separation
#: scaled down for a small UAV). Callers override per mission.
DECONFLICT_HORIZONTAL_M = 9_260.0   # 5 NM
DECONFLICT_VERTICAL_M = 300.0       # ~1000 ft

#: Sensor-degradation factors by condition (M18). These MUST stay key-compatible
#: with `targets._WEATHER_FACTOR` — `test_realdata` pins that, so a change on
#: either side is caught instead of silently producing an unknown condition.
WEATHER_SENSOR_FACTOR: Mapping[str, float] = {
    "clear": 1.00, "haze": 0.85, "rain": 0.60, "snow": 0.55,
    "dust": 0.40, "fog": 0.35,
}

#: Response body cap for the default HTTP client, bytes.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

_USER_AGENT = "godSeye/0.1 (UAV ISR simulation; real-data ingestion)"


# ---------------------------------------------------------------------------
# HTTP: injectable, so no test ever needs the network.
# ---------------------------------------------------------------------------


class RealDataUnavailable(RuntimeError):
    """A real-world feed could not be read.

    Always caught by the provider that raised it and turned into a value whose
    `Provenance` carries `real=False` and this message as its `reason` — the
    failure becomes *data*, never silence.
    """

    def __init__(self, feed: str, reason: str) -> None:
        super().__init__(f"{feed}: {reason}")
        self.feed = feed
        self.reason = reason


@dataclass(frozen=True)
class HttpResponse:
    """Minimal HTTP response the providers need. `headers` are lower-cased."""

    status: int
    body: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> Any:
        return json.loads(self.body)


#: `fetch(url, timeout_s) -> HttpResponse`. Raise for transport failures; return
#: the response (any status) otherwise. Injected in tests.
HttpFetch = Callable[[str, float], HttpResponse]


def urllib_fetch(url: str, timeout_s: float) -> HttpResponse:
    """Default HTTP client: stdlib only, bounded body, no new dependency."""
    request = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:  # noqa: S310
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RealDataUnavailable("http", f"response exceeded {MAX_RESPONSE_BYTES} bytes")
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return HttpResponse(int(resp.status), raw.decode("utf-8", "replace"), headers)
    except urllib.error.HTTPError as exc:  # a status, not a transport failure
        raw = exc.read(MAX_RESPONSE_BYTES) if hasattr(exc, "read") else b""
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        return HttpResponse(int(exc.code), raw.decode("utf-8", "replace"), headers)
    except RealDataUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — reported as the reason, never swallowed
        raise RealDataUnavailable("http", f"{type(exc).__name__}: {exc}") from exc


class RateGate:
    """Minimum interval between requests to one upstream (e.g. Nominatim 1/s).

    Only needed on the direct-upstream paths; the GEV proxies run their own
    limiters, which is the whole reason this module prefers them.
    """

    def __init__(self, min_interval_s: float, *, now: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.min_interval_s = float(min_interval_s)
        self._now = now
        self._sleep = sleep
        self._last = -1e18
        self._lock = threading.Lock()

    def wait(self) -> float:
        """Block until the next request is allowed. Returns seconds waited."""
        with self._lock:
            delay = self.min_interval_s - (self._now() - self._last)
            if delay > 0:
                self._sleep(delay)
            else:
                delay = 0.0
            self._last = self._now()
            return delay


# ---------------------------------------------------------------------------
# Provenance — the flag that makes a degraded feed visible.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """Where a value came from, and whether it is real.

    A value with `real=False` MUST carry a `reason`. That is enforced here
    rather than by convention because "fall back silently to the synthetic
    default" is the exact bug class this module exists to avoid.
    """

    feed: str
    real: bool
    source: str
    attribution: str | None = None
    retrieved_at_ms: int | None = None
    reason: str | None = None
    age_s: float | None = None
    caveat: str | None = None

    def __post_init__(self) -> None:
        if not self.real and not self.reason:
            raise ValueError(
                f"Provenance({self.feed!r}) is not real and carries no reason; "
                "a synthetic value must always say why (no silent fallbacks)")

    @property
    def degraded(self) -> bool:
        """True whenever the value is synthetic or a fallback — the flag UIs read."""
        return not self.real

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"feed": self.feed, "real": self.real,
                               "degraded": self.degraded, "source": self.source}
        for key in ("attribution", "retrieved_at_ms", "reason", "age_s", "caveat"):
            value = getattr(self, key)
            if value is not None:
                out[key] = round(value, 3) if key == "age_s" else value
        return out


def _synthetic(feed: str, source: str, reason: str, **kw: Any) -> Provenance:
    return Provenance(feed=feed, real=False, source=source, reason=reason, **kw)


def _ms(ts: float) -> int:
    return int(ts * 1000.0)


class _WarnOnce:
    """Log each distinct degradation once; the flag in the data is the record."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def __call__(self, message: str, *args: Any) -> None:
        key = message % args if args else message
        with self._lock:
            if key in self._seen:
                return
            self._seen.add(key)
        _LOG.warning("godSeye real-data DEGRADED: %s", key)

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()


# ---------------------------------------------------------------------------
# (1) Terrain — true AGL, terrain-aware LOS, geofence floor.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerrainSample:
    """Ground height at one point, in BOTH datums, with provenance (T1).

    `hae_m` is the ellipsoidal height the upstream actually serves. `msl_m` is
    derived from it through `geo.canonical_altitude()` — the ONE conversion
    point — and therefore sits on godSeye's EGM96 datum, NOT on the upstream's
    EGM2008 one. `upstream_elevation_m` is the upstream's own orthometric value,
    carried only so the geoid difference stays visible.
    """

    lat: float
    lon: float
    hae_m: float | None
    msl_m: float | None
    provenance: Provenance
    undulation_m: float | None = None
    datum_source: str | None = None
    datum_degraded: bool = False
    upstream_elevation_m: float | None = None

    @property
    def real(self) -> bool:
        return self.provenance.real

    @property
    def known(self) -> bool:
        """True when a height is available at all (real or flagged fallback)."""
        return self.hae_m is not None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "lat": round(self.lat, 6), "lon": round(self.lon, 6),
            "terrain_hae_m": None if self.hae_m is None else round(self.hae_m, 2),
            "terrain_msl_m": None if self.msl_m is None else round(self.msl_m, 2),
            "known": self.known,
            "provenance": self.provenance.as_dict(),
        }
        if self.undulation_m is not None:
            out["undulation_m"] = round(self.undulation_m, 3)
            out["datum_source"] = self.datum_source
            out["datum_degraded"] = self.datum_degraded
        if self.upstream_elevation_m is not None:
            out["upstream_elevation_m"] = round(self.upstream_elevation_m, 2)
            out["upstream_elevation_note"] = (
                "upstream orthometric height on EGM2008; NOT used — terrain_msl_m "
                "is derived from terrain_hae_m via geo.canonical_altitude (EGM96, T1)")
        return out


@dataclass(frozen=True)
class AglFix:
    """An altitude resolved against real terrain: true AGL instead of
    height-above-takeoff."""

    lat: float
    lon: float
    alt_hae_m: float
    alt_msl_m: float
    agl_m: float | None
    terrain: TerrainSample

    @property
    def real(self) -> bool:
        return self.terrain.real

    def as_dict(self) -> dict[str, Any]:
        return {
            "lat": round(self.lat, 6), "lon": round(self.lon, 6),
            "alt_hae_m": round(self.alt_hae_m, 2),
            "alt_msl_m": round(self.alt_msl_m, 2),
            "alt_agl_m": None if self.agl_m is None else round(self.agl_m, 2),
            "agl_is_real": self.real,
            "terrain": self.terrain.as_dict(),
        }


@dataclass(frozen=True)
class LosResult:
    """Terrain-aware line-of-sight between two geodetic points.

    `los` is never asserted without saying what was modelled. `known` is False
    whenever the answer rests on anything but real terrain, which is what stops
    `uav_los_check` from becoming an unconditional `true` (TOOL_CONTRACT §4.2).
    """

    los: bool
    known: bool
    model: str
    provenance: Provenance
    first_obstacle: dict[str, Any] | None = None
    min_clearance_m: float | None = None
    samples: int = 0
    distance_m: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "los": self.los,
            "known": self.known,
            "first_obstacle": self.first_obstacle,
            "model": self.model,
            "min_clearance_m": (None if self.min_clearance_m is None
                                else round(self.min_clearance_m, 2)),
            "samples": self.samples,
            "distance_m": round(self.distance_m, 1),
            "provenance": self.provenance.as_dict(),
        }


@dataclass(frozen=True)
class FloorFix:
    """A geofence *floor*: the lowest altitude that clears terrain across an AO."""

    floor_hae_m: float | None
    floor_msl_m: float | None
    clearance_agl_m: float
    highest: TerrainSample | None
    lowest: TerrainSample | None
    samples: int
    provenance: Provenance

    @property
    def real(self) -> bool:
        return self.provenance.real

    def as_dict(self) -> dict[str, Any]:
        return {
            "floor_hae_m": None if self.floor_hae_m is None else round(self.floor_hae_m, 2),
            "floor_msl_m": None if self.floor_msl_m is None else round(self.floor_msl_m, 2),
            "clearance_agl_m": self.clearance_agl_m,
            "highest_terrain": None if self.highest is None else self.highest.as_dict(),
            "lowest_terrain": None if self.lowest is None else self.lowest.as_dict(),
            "samples": self.samples,
            "provenance": self.provenance.as_dict(),
        }


def _interpolate(lat1: float, lon1: float, lat2: float, lon2: float,
                 fraction: float) -> tuple[float, float]:
    """Point at `fraction` along the great circle from 1 to 2 (spherical slerp)."""
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    d = 2.0 * math.asin(math.sqrt(
        math.sin((p2 - p1) / 2.0) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2.0) ** 2))
    if d < 1e-12:
        return (lat1, lon1)
    a = math.sin((1.0 - fraction) * d) / math.sin(d)
    b = math.sin(fraction * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return (math.degrees(math.atan2(z, math.hypot(x, y))),
            math.degrees(math.atan2(y, x)))


class TerrainProvider:
    """Real ground elevation, and everything that falls out of having it.

    All public reads take `allow_network`; pass `False` (or use `cached`) from
    anything on a mission or telemetry path — those calls touch memory only and
    return a flagged value on a miss rather than waiting on a socket.

    `fallback_ground_msl_m` is the *declared* synthetic ground plane used when
    the real data cannot be had — normally a theater's `home_alt_msl_m`, which
    reproduces exactly the pre-existing height-above-takeoff behaviour. It is an
    explicit argument, never a default: with no fallback the heights come back
    `None` and flagged, because an unknown terrain height silently becoming
    `0.0` is how spawned targets ended up 1550 m underground.
    """

    def __init__(self, *, origin: str | None = None, fetch: HttpFetch | None = None,
                 timeout_s: float = TERRAIN_TIMEOUT_S, ttl_s: float = TERRAIN_TTL_S,
                 fallback_ground_msl_m: float | None = None,
                 batch_points: int = TERRAIN_BATCH_POINTS,
                 allow_approx_geoid: bool = False,
                 now: Callable[[], float] = time.time) -> None:
        self.origin = (origin or DEFAULT_GEV_ORIGIN).rstrip("/")
        self._fetch = fetch or urllib_fetch
        self.timeout_s = float(timeout_s)
        self.ttl_s = float(ttl_s)
        self.fallback_ground_msl_m = fallback_ground_msl_m
        self.batch_points = max(1, min(int(batch_points), TERRAIN_MAX_POINTS))
        self.allow_approx_geoid = bool(allow_approx_geoid)
        self._now = now
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._warn = _WarnOnce()
        #: Per-thread override of `fallback_ground_msl_m`, so two concurrent
        #: hydrations cannot hand each other their ground planes. See
        #: `fallback_plane()`.
        self._local = threading.local()
        #: Number of upstream requests made — tests and diagnostics read it.
        self.requests = 0

    # -- the declared fallback plane, scoped to the calling thread -----------
    def effective_fallback_msl_m(self) -> float | None:
        """The fallback ground plane in force on THIS thread.

        `fallback_plane()` scopes a plane to one hydration; without one this is
        the instance-wide value the constructor was given.
        """
        override = getattr(self._local, "fallback", None)
        return self.fallback_ground_msl_m if override is None else override

    @contextmanager
    def fallback_plane(self, msl_m: float | None) -> Any:
        """Declare a synthetic ground plane for the duration of this block, on
        this thread only.

        The plane used to be set and restored on the shared attribute. Two
        overlapping hydrations then fought over it: a sea-level theater's 0 m
        was handed to a 1580 m plateau, and whichever thread restored last left
        the provider permanently holding the other's value. A thread-local
        override cannot do either.
        """
        if msl_m is None:
            yield self
            return
        previous = getattr(self._local, "fallback", None)
        self._local.fallback = float(msl_m)
        try:
            yield self
        finally:
            self._local.fallback = previous

    # -- cache keys -------------------------------------------------------
    @staticmethod
    def _key(lat: float, lon: float) -> str:
        return (f"{round(float(lon), TERRAIN_POINT_DECIMALS):.{TERRAIN_POINT_DECIMALS}f},"
                f"{round(float(lat), TERRAIN_POINT_DECIMALS):.{TERRAIN_POINT_DECIMALS}f}")

    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    # -- upstream ---------------------------------------------------------
    def _url(self, points: Sequence[tuple[float, float]]) -> str:
        param = ";".join(
            f"{round(lon, TERRAIN_POINT_DECIMALS):.{TERRAIN_POINT_DECIMALS}f},"
            f"{round(lat, TERRAIN_POINT_DECIMALS):.{TERRAIN_POINT_DECIMALS}f}"
            for lat, lon in points)
        return f"{self.origin}/api/terrain/heights?points={urllib.parse.quote(param)}"

    def _fetch_batch(self, points: Sequence[tuple[float, float]]) -> int:
        """Fetch one batch; store what came back. Returns points cached."""
        if len(points) > TERRAIN_MAX_POINTS:
            raise RealDataUnavailable(
                "terrain", f"{len(points)} points exceeds the proxy cap of {TERRAIN_MAX_POINTS}")
        self.requests += 1
        resp = self._fetch(self._url(points), self.timeout_s)
        if resp.status != 200:
            raise RealDataUnavailable("terrain", f"HTTP {resp.status} from {self.origin}")
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 — becomes the visible reason
            raise RealDataUnavailable("terrain", f"unparsable response: {exc}") from exc
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise RealDataUnavailable("terrain", "malformed response (no results array)")
        at = self._now()
        stored = 0
        for (lat, lon), row in zip(points, results):
            # A row the upstream answered with no usable height is left UNCACHED
            # (the proxy documents this as transient, ~0.16%) so the next poll
            # re-asks. It must not be cached as a height of zero.
            if not isinstance(row, dict):
                continue
            ellipsoid = row.get("ellipsoid")
            if not isinstance(ellipsoid, (int, float)) or not math.isfinite(float(ellipsoid)):
                continue
            with self._lock:
                self._cache[self._key(lat, lon)] = (at, {
                    "ellipsoid": float(ellipsoid),
                    "elevation": (float(row["elevation"])
                                  if isinstance(row.get("elevation"), (int, float))
                                  else None),
                })
            stored += 1
        return stored

    def _fetch_points(self, points: Sequence[tuple[float, float]]) -> str | None:
        """Fetch every point, batched. Raises only if nothing at all landed.

        Returns the reason a PARTIAL refresh failed, so the points that batch
        would have covered blame the error that actually happened instead of
        the unrelated transient-null case.
        """
        stored = 0
        first_error: RealDataUnavailable | None = None
        for i in range(0, len(points), self.batch_points):
            batch = points[i:i + self.batch_points]
            try:
                stored += self._fetch_batch(batch)
            except RealDataUnavailable as exc:
                first_error = first_error or exc
        if first_error is not None and stored == 0:
            raise first_error
        if first_error is not None:
            self._warn("terrain: partial refresh (%s); flagged points fall back", first_error.reason)
            return f"partial refresh failed: {first_error.reason}"
        return None

    # -- sample construction ---------------------------------------------
    def _sample_from_cache(self, lat: float, lon: float, entry: tuple[float, dict[str, Any]],
                           now: float, *, stale_reason: str | None = None) -> TerrainSample:
        """Build a sample from a cached row.

        `stale_reason` is set when the row is past its TTL and the refresh that
        should have replaced it failed. The height is still served — terrain
        does not move — but it is NOT presented as a live reading: the sibling
        feeds all say "refresh failed; serving the last-good" and terrain used
        to be the one that laundered any age into `real=True`.
        """
        at, row = entry
        hae = row["ellipsoid"]
        # T1: MSL is derived HERE, at the one conversion point, from the
        # datum-independent ellipsoidal height. The upstream's own EGM2008
        # `elevation` is carried but never used.
        try:
            fix = canonical_altitude(hae, lat, lon, datum="hae",
                                     allow_approx=self.allow_approx_geoid)
        except GeoidUnavailableError as exc:
            self._warn("terrain: geoid unavailable (%s); MSL withheld", exc)
            return TerrainSample(
                lat=lat, lon=lon, hae_m=hae, msl_m=None,
                upstream_elevation_m=row.get("elevation"),
                provenance=_synthetic(
                    "terrain", "gev:/api/terrain/heights",
                    f"ellipsoidal height is real but MSL could not be derived: {exc}",
                    attribution=ATTRIBUTION["terrain"], retrieved_at_ms=_ms(at),
                    age_s=max(0.0, now - at)))
        if stale_reason is not None:
            provenance = _synthetic(
                "terrain", "gev:/api/terrain/heights (STALE)", stale_reason,
                attribution=ATTRIBUTION["terrain"], retrieved_at_ms=_ms(at),
                age_s=max(0.0, now - at))
        else:
            provenance = Provenance(
                feed="terrain", real=True, source="gev:/api/terrain/heights",
                attribution=ATTRIBUTION["terrain"], retrieved_at_ms=_ms(at),
                age_s=max(0.0, now - at))
        return TerrainSample(
            lat=lat, lon=lon, hae_m=fix.alt_hae, msl_m=fix.alt_msl,
            undulation_m=fix.undulation_m, datum_source=fix.source,
            datum_degraded=fix.degraded, upstream_elevation_m=row.get("elevation"),
            provenance=provenance)

    def _fallback_sample(self, lat: float, lon: float, reason: str) -> TerrainSample:
        """The declared synthetic ground plane, or an explicit unknown."""
        plane = self.effective_fallback_msl_m()
        if plane is None:
            return TerrainSample(
                lat=lat, lon=lon, hae_m=None, msl_m=None,
                provenance=_synthetic(
                    "terrain", "none",
                    f"{reason}; no fallback ground plane declared - terrain height "
                    "is UNKNOWN (not zero)"))
        msl = float(plane)
        try:
            fix = canonical_altitude(msl, lat, lon, datum="msl",
                                     allow_approx=self.allow_approx_geoid)
            hae, und, src, deg = fix.alt_hae, fix.undulation_m, fix.source, fix.degraded
        except GeoidUnavailableError as exc:
            self._warn("terrain fallback: geoid unavailable (%s)", exc)
            return TerrainSample(
                lat=lat, lon=lon, hae_m=None, msl_m=msl,
                provenance=_synthetic(
                    "terrain", "synthetic:flat-plane",
                    f"{reason}; flat plane at {msl:.1f} m MSL and the geoid is "
                    f"also unavailable ({exc}), so HAE is UNKNOWN"))
        return TerrainSample(
            lat=lat, lon=lon, hae_m=hae, msl_m=msl, undulation_m=und,
            datum_source=src, datum_degraded=deg,
            provenance=_synthetic(
                "terrain", "synthetic:flat-plane",
                f"{reason}; SYNTHETIC flat ground plane at {msl:.1f} m MSL - "
                "'AGL' here is height above that plane, not above real terrain"))

    # -- public reads -----------------------------------------------------
    def heights(self, points: Sequence[tuple[float, float]], *,
                allow_network: bool = True) -> list[TerrainSample]:
        """Ground height at each `(lat, lon)`, in request order (duplicates kept).

        Note the argument order: this module speaks `(lat, lon)` like the rest of
        godSeye; the `lon,lat` the proxy wants is built inside `_url`.
        """
        pts = [(float(lat), float(lon)) for lat, lon in points]
        if not pts:
            return []
        now = self._now()
        wanted: dict[str, tuple[float, float]] = {}
        for lat, lon in pts:
            wanted.setdefault(self._key(lat, lon), (lat, lon))
        with self._lock:
            missing = [k for k in wanted
                       if not (k in self._cache and now - self._cache[k][0] < self.ttl_s)]
        reason: str | None = None
        if missing and not allow_network:
            reason = (f"{len(missing)} point(s) not in cache and network lookups are "
                      "disabled on this path (non-blocking read)")
        elif missing:
            try:
                reason = self._fetch_points([wanted[k] for k in missing])
            except RealDataUnavailable as exc:
                reason = exc.reason
                self._warn("terrain: %s", exc.reason)
        out: list[TerrainSample] = []
        for lat, lon in pts:
            key = self._key(lat, lon)
            with self._lock:
                entry = self._cache.get(key)
            if entry is not None:
                age = now - entry[0]
                # Past the TTL and still the old row => the refresh that should
                # have replaced it did not happen or failed. Serve it, flagged.
                stale_reason = None if age < self.ttl_s else (
                    f"terrain cache entry is STALE: {age:.0f}s old, past the "
                    f"{self.ttl_s:.0f}s TTL, and the refresh failed "
                    f"({reason or 'no refresh was attempted on this path'}); "
                    "serving the last-good height rather than none")
                out.append(self._sample_from_cache(lat, lon, entry, now,
                                                   stale_reason=stale_reason))
            else:
                out.append(self._fallback_sample(
                    lat, lon, reason or "terrain upstream returned no height for this point"))
        return out

    def height(self, lat: float, lon: float, *, allow_network: bool = True) -> TerrainSample:
        """Ground height at one point."""
        return self.heights([(lat, lon)], allow_network=allow_network)[0]

    def cached(self, lat: float, lon: float) -> TerrainSample:
        """Memory-only read — safe on a telemetry tick. Never touches the network."""
        return self.height(lat, lon, allow_network=False)

    def prefetch(self, points: Sequence[tuple[float, float]]) -> threading.Thread:
        """Warm the cache on a daemon thread. Returns it so tests can join."""
        thread = threading.Thread(
            target=lambda: self.heights(points), name="godseye-terrain-prefetch", daemon=True)
        thread.start()
        return thread

    def profile(self, start: tuple[float, float], end: tuple[float, float], *,
                samples: int | None = None, allow_network: bool = True) -> list[TerrainSample]:
        """Terrain along the great circle from `start` to `end`, endpoints included."""
        n = self._profile_samples(start, end, samples)
        pts = [_interpolate(start[0], start[1], end[0], end[1], i / (n - 1))
               for i in range(n)] if n > 1 else [start]
        return self.heights(pts, allow_network=allow_network)

    def _profile_samples(self, start: tuple[float, float], end: tuple[float, float],
                         samples: int | None) -> int:
        if samples is not None:
            return max(2, min(int(samples), TERRAIN_MAX_POINTS))
        distance = haversine_m(start[0], start[1], end[0], end[1])
        n = int(distance / LOS_SAMPLE_SPACING_M) + 2
        return max(LOS_MIN_SAMPLES, min(n, LOS_MAX_SAMPLES))

    def agl(self, lat: float, lon: float, alt_m: float, *, datum: str = "hae",
            allow_network: bool = True) -> AglFix:
        """True AGL at a point: vehicle altitude minus real ground height.

        `datum` says which datum `alt_m` is in; the conversion runs through
        `geo.canonical_altitude()` (T1). `AglFix.agl_m` is `None` — never a
        plausible-looking number — when the terrain height is unknown.
        """
        fix = canonical_altitude(alt_m, lat, lon, datum=datum,
                                 allow_approx=self.allow_approx_geoid)
        terrain = self.height(lat, lon, allow_network=allow_network)
        agl = None if terrain.hae_m is None else fix.alt_hae - terrain.hae_m
        return AglFix(lat=lat, lon=lon, alt_hae_m=fix.alt_hae, alt_msl_m=fix.alt_msl,
                      agl_m=agl, terrain=terrain)

    def line_of_sight(self, observer: tuple[float, float, float],
                      target: tuple[float, float, float], *,
                      samples: int | None = None, clearance_m: float = 0.0,
                      refraction_k: float = 1.0, datum: str = "hae",
                      allow_network: bool = True) -> LosResult:
        """Terrain-aware LOS between two geodetic points.

        `observer` / `target` are `(lat, lon, alt)` with `alt` in `datum`. The
        profile is sampled along the great circle and each sample is compared
        against the straight sight line, corrected for Earth curvature with
        `R_eff = refraction_k * EARTH_MEAN_RADIUS_M` (k=1 optical, 4/3 radar).

        What is NOT modelled, and says so in `model`: vegetation, buildings and
        any structure above bare earth; the upstream is a bare-earth terrain
        model. `known` is False whenever any sample was synthetic, and with no
        terrain data at all the result is `los=False` — refusing to assert sight
        is the honest failure for an ISR system, and TOOL_CONTRACT §4.2 forbids
        an unconditional `true`.
        """
        o_lat, o_lon, o_alt = observer
        t_lat, t_lon, t_alt = target
        o_fix = canonical_altitude(o_alt, o_lat, o_lon, datum=datum,
                                   allow_approx=self.allow_approx_geoid)
        t_fix = canonical_altitude(t_alt, t_lat, t_lon, datum=datum,
                                   allow_approx=self.allow_approx_geoid)
        distance = haversine_m(o_lat, o_lon, t_lat, t_lon)
        n = self._profile_samples((o_lat, o_lon), (t_lat, t_lon), samples)
        r_eff = max(1.0, float(refraction_k)) * EARTH_MEAN_RADIUS_M

        # Interior samples only: the endpoints are the observer and the target.
        fractions = [i / (n - 1) for i in range(1, n - 1)] if n > 2 else []
        pts = [_interpolate(o_lat, o_lon, t_lat, t_lon, f) for f in fractions]
        profile = self.heights(pts, allow_network=allow_network) if pts else []

        synthetic = [s for s in profile if not s.real]
        unknown = [s for s in profile if s.hae_m is None]
        if profile and len(unknown) == len(profile):
            return LosResult(
                los=False, known=False, samples=len(profile), distance_m=distance,
                model=("UNKNOWN: no terrain heights available along the profile and no "
                       "fallback ground plane declared - line of sight CANNOT be asserted"),
                provenance=_synthetic(
                    "terrain", "none",
                    "no terrain height for any profile sample; LOS not asserted"))

        first_obstacle: dict[str, Any] | None = None
        min_clearance: float | None = None
        for fraction, sample in zip(fractions, profile):
            if sample.hae_m is None:
                continue
            d1 = fraction * distance
            sight = (o_fix.alt_hae + (t_fix.alt_hae - o_fix.alt_hae) * fraction
                     - (d1 * (distance - d1)) / (2.0 * r_eff))
            clearance = sight - (sample.hae_m + clearance_m)
            if min_clearance is None or clearance < min_clearance:
                min_clearance = clearance
            if clearance < 0.0 and first_obstacle is None:
                first_obstacle = {
                    "lat": round(sample.lat, 6), "lon": round(sample.lon, 6),
                    "range_m": round(d1, 1),
                    "terrain_hae_m": round(sample.hae_m, 2),
                    "terrain_msl_m": (None if sample.msl_m is None
                                      else round(sample.msl_m, 2)),
                    "sight_line_hae_m": round(sight, 2),
                    "obstruction_m": round(-clearance, 2),
                    "terrain_is_real": sample.real,
                }

        real = bool(profile) and not synthetic
        if real:
            model = (f"terrain-profile: {len(profile)} bare-earth samples of "
                     f"Re:Earth/Mapterhorn ellipsoidal heights along the great circle, "
                     f"Earth curvature R_eff={r_eff:.0f} m (k={refraction_k:g}); "
                     f"vegetation, buildings and other above-ground structure NOT modelled")
            provenance = Provenance(
                feed="terrain", real=True, source="gev:/api/terrain/heights",
                attribution=ATTRIBUTION["terrain"], retrieved_at_ms=_ms(self._now()))
        else:
            plane = self.effective_fallback_msl_m()
            reason = (f"{len(synthetic)} of {len(profile)} profile samples are synthetic"
                      if profile else "profile has no samples (endpoints only)")
            model = (f"SYNTHETIC/DEGRADED: flat ground plane at {plane} m MSL, "
                     f"{len(profile)} samples, Earth curvature R_eff={r_eff:.0f} m - "
                     f"this is NOT a terrain LOS check ({reason})")
            provenance = _synthetic(
                "terrain", "synthetic:flat-plane" if plane is not None else "none", reason)
        return LosResult(
            los=first_obstacle is None, known=real, model=model, provenance=provenance,
            first_obstacle=first_obstacle, min_clearance_m=min_clearance,
            samples=len(profile), distance_m=distance)

    def floor(self, points: Sequence[tuple[float, float]], *, clearance_agl_m: float,
              allow_network: bool = True) -> FloorFix:
        """Geofence FLOOR over an area: highest terrain plus a clearance.

        Feed it the AO polygon vertices (and any interior samples the caller
        wants). The returned `floor_hae_m` is the lowest altitude that keeps
        `clearance_agl_m` above the highest ground sampled.
        """
        samples = self.heights(points, allow_network=allow_network)
        usable = [s for s in samples if s.hae_m is not None]
        if not usable:
            return FloorFix(
                floor_hae_m=None, floor_msl_m=None, clearance_agl_m=clearance_agl_m,
                highest=None, lowest=None, samples=len(samples),
                provenance=_synthetic(
                    "terrain", "none",
                    "no terrain height for any AO sample; a geofence floor cannot be set"))
        highest = max(usable, key=lambda s: s.hae_m)
        lowest = min(usable, key=lambda s: s.hae_m)
        real = all(s.real for s in samples) and len(usable) == len(samples)
        if real:
            provenance = Provenance(
                feed="terrain", real=True, source="gev:/api/terrain/heights",
                attribution=ATTRIBUTION["terrain"], retrieved_at_ms=_ms(self._now()))
        else:
            provenance = _synthetic(
                "terrain", highest.provenance.source,
                f"{sum(1 for s in samples if not s.real)} of {len(samples)} AO samples "
                "are synthetic; this floor does not reflect real terrain")
        return FloorFix(
            floor_hae_m=highest.hae_m + clearance_agl_m,
            floor_msl_m=(None if highest.msl_m is None else highest.msl_m + clearance_agl_m),
            clearance_agl_m=float(clearance_agl_m), highest=highest, lowest=lowest,
            samples=len(samples), provenance=provenance)


# ---------------------------------------------------------------------------
# (2) Mapped military installations -> a plausible order of battle.
# ---------------------------------------------------------------------------

#: OSM tag value -> `targets.OB_LIBRARY` key. The GEV proxy's Overpass query
#: selects `military ~ ^(airfield|naval_base|range|barracks|base)$` plus
#: `landuse=military`, but the elements it returns carry many other `military`
#: values, so this table is deliberately wider than the query.
OSM_MILITARY_TO_OB: Mapping[str, str] = {
    "airfield": "structure",
    "naval_base": "structure",
    "range": "structure",
    "barracks": "structure",
    "base": "structure",
    "training_area": "structure",
    "danger_area": "structure",
    "bunker": "bunker",
    "checkpoint": "observation_post",
    "office": "c2_node",
    "depot": "depot_ammo",
    "ammunition": "depot_ammo",
    "radar": "radar_acquisition",
    "trench": "structure",
    "obstacle_course": "structure",
    "nuclear_explosion_site": "structure",
}

#: Used when a mapped site matches nothing more specific. `structure` asserts no
#: weapons envelope, so a mapped building can never invent a threat ring.
DEFAULT_SITE_OB = "structure"


@dataclass(frozen=True)
class Installation:
    """One mapped military site. MAPPED data — see `MAPPED_DATA_CAVEAT`."""

    osm_id: str
    name: str
    lat: float
    lon: float
    ob_class: str
    category: str
    site_type: str
    ob_source: str                    # name-match | osm-tag | default
    tags: Mapping[str, str] = field(default_factory=dict)
    alt_msl_m: float | None = None
    alt_source: str | None = None
    #: How the position was derived: node | center | geometry-centroid |
    #: bounds-midpoint. A centroid is the middle of a mapped area, not a
    #: surveyed point, so it is surfaced rather than presented as a fix.
    position_source: str = "node"

    #: Always true for this feed, and always surfaced.
    mapped_data: bool = True
    authoritative: bool = False

    def spawn_request(self) -> dict[str, Any]:
        """`sim_spawn_target(**request)` payload (TOOL_CONTRACT §4.4).

        Every key here is a real parameter of the tool. It used to emit
        `class` — which TOOL_CONTRACT's prose column calls the argument, but
        which the tool spells `ob_class` and which Python cannot pass as a
        keyword at all — plus an `alt_source` the tool does not take, so
        splatting this dict raised `TypeError` on the first two arguments. The
        provenance moved to `alt_provenance()`; `test_realdata` now binds this
        payload against the server's actual signature so it cannot drift again.

        `alt_msl_m` is omitted rather than defaulted when terrain is unknown, so
        the server applies its own terrain default instead of burying the target
        at 0 m MSL — the exact defect §4.4 calls out.
        """
        out: dict[str, Any] = {"ob_class": self.ob_class, "lat": self.lat,
                               "lon": self.lon, "name": self.name}
        if self.alt_msl_m is not None:
            out["alt_msl_m"] = self.alt_msl_m
        return out

    def alt_provenance(self) -> dict[str, Any]:
        """Where `spawn_request()["alt_msl_m"]` came from — kept OUT of the tool
        payload (the tool takes no such argument) but never dropped, so a
        synthetic or unknown ground height stays visible to the caller."""
        return {"alt_msl_m": self.alt_msl_m, "alt_source": self.alt_source,
                "alt_is_real": self.alt_source == "terrain:gev"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "osm_id": self.osm_id, "name": self.name,
            "lat": round(self.lat, 6), "lon": round(self.lon, 6),
            "ob_class": self.ob_class, "category": self.category,
            "site_type": self.site_type, "ob_source": self.ob_source,
            "position_source": self.position_source,
            "alt_msl_m": None if self.alt_msl_m is None else round(self.alt_msl_m, 1),
            "alt_source": self.alt_source,
            "tags": dict(self.tags),
            "mapped_data": self.mapped_data,
            "authoritative": self.authoritative,
            "caveat": MAPPED_DATA_CAVEAT,
        }


@dataclass(frozen=True)
class OrderOfBattle:
    """A theater's OB hydrated from mapped sites. Context, not an OB (M14)."""

    sites: tuple[Installation, ...]
    provenance: Provenance
    saturated: bool = False
    dropped_no_position: int = 0

    @property
    def real(self) -> bool:
        return self.provenance.real

    def spawn_requests(self) -> list[dict[str, Any]]:
        return [s.spawn_request() for s in self.sites]

    def by_category(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for site in self.sites:
            counts[site.category] = counts.get(site.category, 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": len(self.sites),
            "sites": [s.as_dict() for s in self.sites],
            "by_category": self.by_category(),
            "saturated": self.saturated,
            "dropped_no_position": self.dropped_no_position,
            "mapped_data": True,
            "authoritative": False,
            "caveat": MAPPED_DATA_CAVEAT,
            "provenance": self.provenance.as_dict(),
        }


def classify_site(tags: Mapping[str, str]) -> tuple[str, str, str]:
    """Map OSM tags to `(ob_class_key, site_type, ob_source)`.

    Order: an explicit order-of-battle cue in the site NAME wins (a mapped
    "Radar Station" really is a radar), then the `military`/`landuse` tag table,
    then `DEFAULT_SITE_OB`. `ob_source` records which rule fired so a weak
    mapping is visible rather than indistinguishable from a strong one.
    """
    site_type = str(tags.get("military") or tags.get("landuse") or "unknown")
    name = str(tags.get("name:en") or tags.get("name") or "")
    if name:
        entry, evidence = ob.match_ob(name)
        if evidence.get("matched"):
            return entry.key, site_type, "name-match"
    mapped = OSM_MILITARY_TO_OB.get(site_type)
    if mapped:
        return mapped, site_type, "osm-tag"
    return DEFAULT_SITE_OB, site_type, "default"


def _valid_lat_lon(lat: Any, lon: Any) -> tuple[float, float] | None:
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    lat, lon = float(lat), float(lon)
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if abs(lat) > 90.0 or abs(lon) > 180.0:
        return None
    return (lat, lon)


def _element_position(element: Mapping[str, Any]) -> tuple[tuple[float, float], str] | None:
    """Position of one Overpass element, and which rule produced it.

    The proxy's query is `out center tags geom`, and real responses turn out to
    carry any of three shapes: a node's own `lat`/`lon`, a `center` centroid, or
    — for most ways and relations in the captured payloads — `bounds` plus a
    `geometry` vertex list with no `center` at all. Handling only `center` drops
    84% of the mapped sites in GEV's own cache, so all four are read.

    Returns None — the element is then DROPPED and counted — rather than
    defaulting to (0, 0). A `.get("lat", 0.0)` here is precisely the bug that
    once put every spawned target at the origin.
    """
    direct = _valid_lat_lon(element.get("lat"), element.get("lon"))
    if direct:
        return (direct, "node")
    center = element.get("center")
    if isinstance(center, Mapping):
        found = _valid_lat_lon(center.get("lat"), center.get("lon"))
        if found:
            return (found, "center")
    geometry = element.get("geometry")
    if isinstance(geometry, list) and geometry:
        vertices = [v for v in (
            _valid_lat_lon(p.get("lat"), p.get("lon"))
            for p in geometry if isinstance(p, Mapping)) if v]
        if vertices:
            return ((sum(v[0] for v in vertices) / len(vertices),
                     sum(v[1] for v in vertices) / len(vertices)), "geometry-centroid")
    bounds = element.get("bounds")
    if isinstance(bounds, Mapping):
        sw = _valid_lat_lon(bounds.get("minlat"), bounds.get("minlon"))
        ne = _valid_lat_lon(bounds.get("maxlat"), bounds.get("maxlon"))
        if sw and ne:
            return (((sw[0] + ne[0]) / 2.0, (sw[1] + ne[1]) / 2.0), "bounds-midpoint")
    return None


def parse_installations(payload: Mapping[str, Any], *, limit: int | None = None,
                        ) -> tuple[list[Installation], bool, int]:
    """Parse a `/api/military-installations` payload into `Installation`s.

    Returns `(sites, saturated, dropped_no_position)`. Pure — no network, no
    clock — so the fixture tests exercise exactly this.
    """
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise RealDataUnavailable("installations", "malformed response (no elements array)")
    sites: list[Installation] = []
    dropped = 0
    for element in elements:
        if not isinstance(element, Mapping):
            dropped += 1
            continue
        located = _element_position(element)
        if located is None:
            dropped += 1
            continue
        (lat, lon), position_source = located
        tags = element.get("tags")
        tags = {str(k): str(v) for k, v in tags.items()} if isinstance(tags, Mapping) else {}
        ob_key, site_type, ob_source = classify_site(tags)
        osm_id = f"{element.get('type', 'element')}/{element.get('id', 'unknown')}"
        name = (tags.get("name:en") or tags.get("name")
                or f"mapped {site_type} {osm_id}")
        sites.append(Installation(
            osm_id=osm_id, name=name, lat=lat, lon=lon,
            ob_class=ob_key, category=ob.ob_class(ob_key).category,
            site_type=site_type, ob_source=ob_source, tags=tags,
            position_source=position_source))
        if limit is not None and len(sites) >= limit:
            break
    return sites, bool(payload.get("saturated")), dropped


class InstallationsProvider:
    """Mapped military sites near a place, as a plausible order of battle.

    Everything this returns is labelled `mapped_data=True`,
    `authoritative=False` and carries `MAPPED_DATA_CAVEAT`, at the site level
    and at the roster level, so a report cannot quietly present it as a real OB.
    """

    def __init__(self, *, origin: str | None = None, fetch: HttpFetch | None = None,
                 timeout_s: float = INSTALLATIONS_TIMEOUT_S,
                 ttl_s: float = INSTALLATIONS_TTL_S,
                 now: Callable[[], float] = time.time) -> None:
        self.origin = (origin or DEFAULT_GEV_ORIGIN).rstrip("/")
        self._fetch = fetch or urllib_fetch
        self.timeout_s = float(timeout_s)
        self.ttl_s = float(ttl_s)
        self._now = now
        self._cache: dict[str, tuple[float, Mapping[str, Any]]] = {}
        self._lock = threading.Lock()
        self._warn = _WarnOnce()

    @staticmethod
    def _key(south: float, west: float, north: float, east: float) -> str:
        return f"{south:.4f},{west:.4f},{north:.4f},{east:.4f}"

    def _url(self, south: float, west: float, north: float, east: float) -> str:
        query = urllib.parse.urlencode({"south": south, "west": west,
                                        "north": north, "east": east})
        return f"{self.origin}/api/military-installations?{query}"

    def order_of_battle(self, south: float, west: float, north: float, east: float, *,
                        limit: int | None = None, terrain: TerrainProvider | None = None,
                        allow_network: bool = True) -> OrderOfBattle:
        """Hydrate an OB from mapped sites inside the bbox.

        With a `TerrainProvider` each site gets a real ground `alt_msl_m`, so a
        spawned target sits on the ground instead of underground.
        """
        if not (south < north and west < east):
            raise ValueError(f"bbox must satisfy south<north and west<east, got "
                             f"{south},{west},{north},{east}")
        key = self._key(south, west, north, east)
        now = self._now()
        with self._lock:
            entry = self._cache.get(key)
        fresh = entry is not None and now - entry[0] < self.ttl_s
        reason: str | None = None
        if not fresh and not allow_network:
            reason = "not cached and network lookups are disabled on this path"
        elif not fresh:
            try:
                payload = self._request(south, west, north, east)
                entry = (now, payload)
                with self._lock:
                    self._cache[key] = entry
            except RealDataUnavailable as exc:
                reason = exc.reason
                self._warn("installations: %s", exc.reason)
        if entry is None:
            return OrderOfBattle(
                sites=(), saturated=False,
                provenance=_synthetic(
                    "installations", "none",
                    f"{reason or 'no mapped-site data'}; the theater keeps its "
                    "SYNTHETIC hand-placed laydown - no real sites were ingested",
                    caveat=MAPPED_DATA_CAVEAT))
        at, payload = entry
        try:
            sites, saturated, dropped = parse_installations(payload, limit=limit)
        except RealDataUnavailable as exc:
            self._warn("installations: %s", exc.reason)
            return OrderOfBattle(
                sites=(), saturated=False,
                provenance=_synthetic("installations", "gev:/api/military-installations",
                                      exc.reason, caveat=MAPPED_DATA_CAVEAT))
        if terrain is not None and sites:
            sites = self._attach_terrain(sites, terrain, allow_network=allow_network)
        stale = reason is not None
        provenance = (
            _synthetic("installations", "gev:/api/military-installations",
                       f"refresh failed ({reason}); serving the last-good mapped roster",
                       attribution=ATTRIBUTION["installations"], retrieved_at_ms=_ms(at),
                       age_s=max(0.0, now - at), caveat=MAPPED_DATA_CAVEAT)
            if stale else
            Provenance(feed="installations", real=True,
                       source="gev:/api/military-installations",
                       attribution=ATTRIBUTION["installations"], retrieved_at_ms=_ms(at),
                       age_s=max(0.0, now - at), caveat=MAPPED_DATA_CAVEAT))
        return OrderOfBattle(sites=tuple(sites), provenance=provenance,
                             saturated=saturated, dropped_no_position=dropped)

    def _attach_terrain(self, sites: Sequence[Installation], terrain: TerrainProvider,
                        *, allow_network: bool) -> list[Installation]:
        samples = terrain.heights([(s.lat, s.lon) for s in sites],
                                  allow_network=allow_network)
        out: list[Installation] = []
        for site, sample in zip(sites, samples):
            out.append(Installation(
                osm_id=site.osm_id, name=site.name, lat=site.lat, lon=site.lon,
                ob_class=site.ob_class, category=site.category, site_type=site.site_type,
                ob_source=site.ob_source, tags=site.tags,
                position_source=site.position_source, alt_msl_m=sample.msl_m,
                alt_source=("terrain:gev" if sample.real else
                            f"UNKNOWN ({sample.provenance.reason})"
                            if sample.msl_m is None else
                            f"SYNTHETIC ({sample.provenance.reason})")))
        return out

    def _request(self, south: float, west: float, north: float,
                 east: float) -> Mapping[str, Any]:
        resp = self._fetch(self._url(south, west, north, east), self.timeout_s)
        if resp.status != 200:
            detail = ""
            try:
                body = resp.json()
                if isinstance(body, Mapping):
                    detail = f" ({body.get('reason') or body.get('error')})"
            except Exception:  # noqa: BLE001 — the status is the real signal
                pass
            raise RealDataUnavailable(
                "installations", f"HTTP {resp.status} from {self.origin}{detail}")
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RealDataUnavailable("installations", f"unparsable response: {exc}") from exc
        if not isinstance(body, Mapping):
            raise RealDataUnavailable("installations", "malformed response (not an object)")
        return body


# ---------------------------------------------------------------------------
# (3) Live air traffic -> deconfliction contacts.
# ---------------------------------------------------------------------------

#: OpenSky state-vector field order (`/states/all`). The adsb.lol fallback the
#: GEV proxy substitutes is normalised into this same shape upstream.
_OPENSKY_FIELDS = ("icao24", "callsign", "origin_country", "time_position",
                   "last_contact", "longitude", "latitude", "baro_altitude",
                   "on_ground", "velocity", "true_track", "vertical_rate",
                   "sensors", "geo_altitude", "squawk", "spi", "position_source",
                   "category")


@dataclass(frozen=True)
class AirspaceContact:
    """One real aircraft in the AO — traffic to deconflict against, not a target.

    `alt_hae_m` is OpenSky's GNSS *geometric* altitude, which is ellipsoidal, so
    it is the one that converts through `geo.canonical_altitude()` (T1).
    `alt_baro_m` is a pressure altitude on no geodetic datum at all and is
    carried unconverted and separately labelled.
    """

    icao24: str
    callsign: str
    lat: float
    lon: float
    alt_hae_m: float | None
    alt_msl_m: float | None
    alt_baro_m: float | None
    on_ground: bool
    velocity_mps: float | None
    track_deg: float | None
    vertical_rate_mps: float | None
    origin_country: str
    squawk: str | None
    last_contact_ms: int | None
    range_m: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "icao24": self.icao24, "callsign": self.callsign,
            "lat": round(self.lat, 6), "lon": round(self.lon, 6),
            "alt_hae_m": None if self.alt_hae_m is None else round(self.alt_hae_m, 1),
            "alt_msl_m": None if self.alt_msl_m is None else round(self.alt_msl_m, 1),
            "alt_baro_m": None if self.alt_baro_m is None else round(self.alt_baro_m, 1),
            "alt_baro_note": "barometric pressure altitude - NOT a geodetic datum",
            "on_ground": self.on_ground,
            "velocity_mps": self.velocity_mps, "track_deg": self.track_deg,
            "vertical_rate_mps": self.vertical_rate_mps,
            "origin_country": self.origin_country, "squawk": self.squawk,
            "last_contact_ms": self.last_contact_ms,
            "range_m": round(self.range_m, 1),
        }


@dataclass(frozen=True)
class AirspaceTraffic:
    """Real aircraft within a radius of a point, nearest first."""

    contacts: tuple[AirspaceContact, ...]
    center: tuple[float, float]
    radius_m: float
    provenance: Provenance

    @property
    def real(self) -> bool:
        return self.provenance.real

    def deconflict(self, lat: float, lon: float, alt_hae_m: float | None, *,
                   horizontal_m: float = DECONFLICT_HORIZONTAL_M,
                   vertical_m: float = DECONFLICT_VERTICAL_M) -> list[dict[str, Any]]:
        """Contacts violating the separation box around `(lat, lon, alt_hae_m)`.

        A contact with no usable altitude conflicts on horizontal separation
        alone: unknown vertical separation is treated as *not* separated, which
        is the only safe reading. The returned rows say which it was.
        """
        out: list[dict[str, Any]] = []
        for contact in self.contacts:
            horizontal = haversine_m(lat, lon, contact.lat, contact.lon)
            if horizontal > horizontal_m:
                continue
            if alt_hae_m is None or contact.alt_hae_m is None:
                vertical: float | None = None
                separated = False
            else:
                vertical = abs(alt_hae_m - contact.alt_hae_m)
                separated = vertical > vertical_m
            if separated:
                continue
            out.append({
                "contact": contact.as_dict(),
                "horizontal_m": round(horizontal, 1),
                "vertical_m": None if vertical is None else round(vertical, 1),
                "basis": ("horizontal separation only - one altitude is unknown, "
                          "so vertical separation cannot be asserted"
                          if vertical is None else
                          f"inside {horizontal_m:.0f} m x {vertical_m:.0f} m separation box"),
            })
        out.sort(key=lambda row: row["horizontal_m"])
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": len(self.contacts),
            "center": {"lat": self.center[0], "lon": self.center[1]},
            "radius_m": self.radius_m,
            "contacts": [c.as_dict() for c in self.contacts],
            "provenance": self.provenance.as_dict(),
        }


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


class TrafficProvider:
    """Live aircraft in the AO, via GEV's OpenSky proxy (adsb.lol fallback).

    Terms travel with the data: OpenSky is licensed for **non-commercial
    research and education use only** and adsb.lol is ODbL. When the proxy's
    headers do not say which upstream served the frame, BOTH attributions are
    carried rather than guessing.
    """

    def __init__(self, *, origin: str | None = None, fetch: HttpFetch | None = None,
                 timeout_s: float = TRAFFIC_TIMEOUT_S, ttl_s: float = TRAFFIC_TTL_S,
                 allow_approx_geoid: bool = False,
                 now: Callable[[], float] = time.time) -> None:
        self.origin = (origin or DEFAULT_GEV_ORIGIN).rstrip("/")
        self._fetch = fetch or urllib_fetch
        self.timeout_s = float(timeout_s)
        self.ttl_s = float(ttl_s)
        self.allow_approx_geoid = bool(allow_approx_geoid)
        self._now = now
        self._cache: dict[str, tuple[float, Mapping[str, Any], str]] = {}
        self._lock = threading.Lock()
        self._warn = _WarnOnce()

    @staticmethod
    def _key(lat: float, lon: float) -> str:
        # The proxy answers with a worldwide frame; a coarse anchor is enough to
        # share one cached frame across every AO in the same region.
        return f"{round(lat * 4) / 4:.2f},{round(lon * 4) / 4:.2f}"

    def _url(self, lat: float, lon: float) -> str:
        return f"{self.origin}/api/opensky?{urllib.parse.urlencode({'lat': lat, 'lon': lon})}"

    @staticmethod
    def _attribution(headers: Mapping[str, str]) -> tuple[str, str]:
        """(source_detail, attribution) from the proxy's X-OpenSky-* headers."""
        reason = (headers.get("x-opensky-auth-reason") or "").lower()
        if "regional_fallback" in reason or "adsb" in reason:
            return ("gev:/api/opensky (adsb.lol regional fallback)",
                    ATTRIBUTION["traffic_adsblol"])
        if reason:
            return ("gev:/api/opensky (OpenSky)", ATTRIBUTION["traffic_opensky"])
        return ("gev:/api/opensky (upstream not declared by the proxy)",
                f"{ATTRIBUTION['traffic_opensky']} | {ATTRIBUTION['traffic_adsblol']}")

    def contacts(self, lat: float, lon: float, radius_m: float, *,
                 allow_network: bool = True, include_ground: bool = False,
                 ) -> AirspaceTraffic:
        """Real aircraft within `radius_m` of `(lat, lon)`, nearest first."""
        key = self._key(lat, lon)
        now = self._now()
        with self._lock:
            entry = self._cache.get(key)
        fresh = entry is not None and now - entry[0] < self.ttl_s
        reason: str | None = None
        if not fresh and not allow_network:
            reason = "not cached and network lookups are disabled on this path"
        elif not fresh:
            try:
                payload, detail, attribution = self._request(lat, lon)
                entry = (now, payload, json.dumps([detail, attribution]))
                with self._lock:
                    self._cache[key] = entry
            except RealDataUnavailable as exc:
                reason = exc.reason
                self._warn("traffic: %s", exc.reason)
        if entry is None:
            return AirspaceTraffic(
                contacts=(), center=(lat, lon), radius_m=float(radius_m),
                provenance=_synthetic(
                    "traffic", "none",
                    f"{reason or 'no traffic data'}; the AO shows NO real air traffic - "
                    "this is an empty feed, not a clear sky"))
        at, payload, meta = entry
        detail, attribution = json.loads(meta)
        contacts = self._parse(payload, lat, lon, radius_m, include_ground=include_ground)
        stale = reason is not None
        provenance = (
            _synthetic("traffic", detail,
                       f"refresh failed ({reason}); serving the last-good frame",
                       attribution=attribution, retrieved_at_ms=_ms(at),
                       age_s=max(0.0, now - at))
            if stale else
            Provenance(feed="traffic", real=True, source=detail, attribution=attribution,
                       retrieved_at_ms=_ms(at), age_s=max(0.0, now - at)))
        return AirspaceTraffic(contacts=tuple(contacts), center=(lat, lon),
                               radius_m=float(radius_m), provenance=provenance)

    def _parse(self, payload: Mapping[str, Any], lat: float, lon: float, radius_m: float,
               *, include_ground: bool) -> list[AirspaceContact]:
        states = payload.get("states")
        if not isinstance(states, list):
            return []
        out: list[AirspaceContact] = []
        for state in states:
            if not isinstance(state, (list, tuple)) or len(state) < 12:
                continue
            row = dict(zip(_OPENSKY_FIELDS, state))
            c_lat, c_lon = _finite(row.get("latitude")), _finite(row.get("longitude"))
            if c_lat is None or c_lon is None:
                continue  # positionless contact: dropped, never placed at 0,0
            distance = haversine_m(lat, lon, c_lat, c_lon)
            if distance > radius_m:
                continue
            on_ground = bool(row.get("on_ground"))
            if on_ground and not include_ground:
                continue
            geo_alt = _finite(row.get("geo_altitude"))
            alt_msl = None
            if geo_alt is not None:
                try:
                    alt_msl = canonical_altitude(
                        geo_alt, c_lat, c_lon, datum="hae",
                        allow_approx=self.allow_approx_geoid).alt_msl
                except (GeoidUnavailableError, ValueError) as exc:
                    self._warn("traffic: MSL not derivable for a contact (%s)", exc)
            last = _finite(row.get("last_contact"))
            out.append(AirspaceContact(
                icao24=str(row.get("icao24") or "").strip(),
                callsign=str(row.get("callsign") or "").strip(),
                lat=c_lat, lon=c_lon, alt_hae_m=geo_alt, alt_msl_m=alt_msl,
                alt_baro_m=_finite(row.get("baro_altitude")), on_ground=on_ground,
                velocity_mps=_finite(row.get("velocity")),
                track_deg=_finite(row.get("true_track")),
                vertical_rate_mps=_finite(row.get("vertical_rate")),
                origin_country=str(row.get("origin_country") or ""),
                squawk=(str(row["squawk"]) if row.get("squawk") else None),
                last_contact_ms=None if last is None else int(last * 1000),
                range_m=distance))
        out.sort(key=lambda c: c.range_m)
        return out

    def _request(self, lat: float, lon: float) -> tuple[Mapping[str, Any], str, str]:
        resp = self._fetch(self._url(lat, lon), self.timeout_s)
        if resp.status != 200:
            raise RealDataUnavailable("traffic", f"HTTP {resp.status} from {self.origin}")
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RealDataUnavailable("traffic", f"unparsable response: {exc}") from exc
        if not isinstance(body, Mapping) or not isinstance(body.get("states"), list):
            raise RealDataUnavailable("traffic", "malformed response (no states array)")
        detail, attribution = self._attribution(resp.headers)
        return body, detail, attribution


# ---------------------------------------------------------------------------
# (4) Weather and wind -> the fuel model (M15) and sensor degradation (M18).
# ---------------------------------------------------------------------------

#: WMO weather code -> a `WEATHER_SENSOR_FACTOR` condition (Open-Meteo codes).
_WMO_CONDITION: tuple[tuple[range, str], ...] = (
    (range(0, 4), "clear"),        # 0 clear .. 3 overcast
    (range(4, 10), "dust"),        # 4-9 smoke / haze / dust / sand
    (range(10, 20), "haze"),
    (range(20, 30), "rain"),
    (range(30, 40), "dust"),       # 30-39 duststorm / sandstorm
    (range(40, 50), "fog"),        # 45/48 fog, depositing rime fog
    (range(50, 60), "rain"),       # drizzle
    (range(60, 70), "rain"),
    (range(70, 80), "snow"),
    (range(80, 85), "rain"),
    (range(85, 95), "snow"),
    (range(95, 100), "rain"),      # thunderstorm; obscurant behaves as heavy rain
)

#: Visibility below this reads as haze even under a clear-sky code, metres.
HAZE_VISIBILITY_M = 5_000.0
#: Visibility below this reads as fog, metres.
FOG_VISIBILITY_M = 1_000.0


def condition_from(weather_code: float | None, visibility_m: float | None) -> str:
    """Condition key for `WEATHER_SENSOR_FACTOR` (M18), worst of code and visibility."""
    condition = "clear"
    if weather_code is not None and math.isfinite(weather_code):
        code = int(weather_code)
        for span, name in _WMO_CONDITION:
            if code in span:
                condition = name
                break
    if visibility_m is not None and math.isfinite(visibility_m):
        if visibility_m < FOG_VISIBILITY_M:
            visual = "fog"
        elif visibility_m < HAZE_VISIBILITY_M:
            visual = "haze"
        else:
            visual = "clear"
        # Keep whichever degrades the sensor more; never upgrade the report.
        if WEATHER_SENSOR_FACTOR[visual] < WEATHER_SENSOR_FACTOR[condition]:
            condition = visual
    return condition


def wind_ne_mps(speed_mps: float, from_deg: float) -> tuple[float, float]:
    """Meteorological wind -> the NED air-mass velocity `safety.FuelModel` wants.

    Open-Meteo reports the direction the wind blows FROM; `safety` wants the
    vector the air mass is moving ALONG, so the bearing is flipped 180 deg. A
    wind from 270 (a westerly) becomes air moving east: `(0, +speed)`.
    """
    to_rad = math.radians((float(from_deg) + 180.0) % 360.0)
    return (float(speed_mps) * math.cos(to_rad), float(speed_mps) * math.sin(to_rad))


@dataclass(frozen=True)
class WeatherObservation:
    """Current weather at a real place, in the shapes godSeye consumes."""

    lat: float
    lon: float
    temperature_c: float | None
    cloud_cover_pct: float | None
    precipitation_mm: float | None
    visibility_m: float | None
    wind_speed_mps: float | None
    wind_from_deg: float | None
    weather_code: float | None
    condition: str
    observed_at_ms: int | None
    provenance: Provenance

    @property
    def real(self) -> bool:
        return self.provenance.real

    @property
    def wind_known(self) -> bool:
        """True only when BOTH wind components were actually observed.

        `wind_ne` is a bare tuple with no provenance of its own, so without
        this a fabricated dead calm is indistinguishable from a measured one.
        `observe()` also degrades the whole observation when this is False.
        """
        return self.wind_speed_mps is not None and self.wind_from_deg is not None

    @property
    def wind_ne(self) -> tuple[float, float]:
        """NED air-mass velocity (m/s) for `FuelModel(..., wind_ne=...)` (M15).

        `(0.0, 0.0)` when the wind was not observed — check `wind_known` (or
        `provenance.degraded`) before treating a calm as measured.
        """
        if not self.wind_known:
            return (0.0, 0.0)
        return wind_ne_mps(self.wind_speed_mps, self.wind_from_deg)

    @property
    def sensor_factor(self) -> float:
        """Sensor-quality multiplier for this condition (M18)."""
        return WEATHER_SENSOR_FACTOR[self.condition]

    def sim_weather_payload(self) -> dict[str, Any]:
        """Exactly the arguments `sim_set_weather` takes (TOOL_CONTRACT §4.4).

        Obscurant intensities are derived from the real observation: rain and
        snow from precipitation rate under the matching condition, fog from
        visibility, dust from the WMO dust/sand codes.
        """
        precipitation = self.precipitation_mm or 0.0
        intensity = max(0.0, min(1.0, precipitation / 5.0))
        rain = intensity if self.condition == "rain" else 0.0
        snow = intensity if self.condition == "snow" else 0.0
        fog = 0.0
        if self.visibility_m is not None and self.visibility_m < HAZE_VISIBILITY_M:
            fog = max(0.0, min(1.0, 1.0 - self.visibility_m / HAZE_VISIBILITY_M))
        dust = 0.6 if self.condition == "dust" else 0.0
        north, east = self.wind_ne
        return {"rain": round(rain, 3), "snow": round(snow, 3), "fog": round(fog, 3),
                "dust": round(dust, 3), "wind_north_mps": round(north, 3),
                "wind_east_mps": round(east, 3), "wind_down_mps": 0.0}

    def as_dict(self) -> dict[str, Any]:
        north, east = self.wind_ne
        return {
            "lat": round(self.lat, 6), "lon": round(self.lon, 6),
            "temperature_c": self.temperature_c,
            "cloud_cover_pct": self.cloud_cover_pct,
            "precipitation_mm": self.precipitation_mm,
            "visibility_m": self.visibility_m,
            "wind_speed_mps": (None if self.wind_speed_mps is None
                               else round(self.wind_speed_mps, 2)),
            "wind_from_deg": self.wind_from_deg,
            "wind_ne_mps": [round(north, 3), round(east, 3)],
            "wind_known": self.wind_known,
            "weather_code": self.weather_code,
            "condition": self.condition,
            "sensor_factor": self.sensor_factor,
            "observed_at_ms": self.observed_at_ms,
            "sim_set_weather": self.sim_weather_payload(),
            "provenance": self.provenance.as_dict(),
        }


#: The calm, clear fallback. Identical to the pre-existing synthetic default —
#: and always returned with `real=False` plus a reason, so a mission can see
#: that its wind is an assumption rather than an observation.
def _synthetic_weather(lat: float, lon: float, reason: str) -> WeatherObservation:
    return WeatherObservation(
        lat=lat, lon=lon, temperature_c=None, cloud_cover_pct=None,
        precipitation_mm=None, visibility_m=None, wind_speed_mps=0.0,
        wind_from_deg=0.0, weather_code=None, condition="clear", observed_at_ms=None,
        provenance=_synthetic(
            "weather", "synthetic:calm-clear", reason))


class WeatherProvider:
    """Real current weather at a point, via GEV's Open-Meteo proxy (CC BY 4.0)."""

    def __init__(self, *, origin: str | None = None, fetch: HttpFetch | None = None,
                 timeout_s: float = WEATHER_TIMEOUT_S, ttl_s: float = WEATHER_TTL_S,
                 now: Callable[[], float] = time.time) -> None:
        self.origin = (origin or DEFAULT_GEV_ORIGIN).rstrip("/")
        self._fetch = fetch or urllib_fetch
        self.timeout_s = float(timeout_s)
        self.ttl_s = float(ttl_s)
        self._now = now
        self._cache: dict[str, tuple[float, Mapping[str, Any]]] = {}
        self._lock = threading.Lock()
        self._warn = _WarnOnce()

    @staticmethod
    def _key(lat: float, lon: float) -> str:
        # The proxy itself keys at 0.1 deg; match it so we never out-ask it.
        return f"{round(lat, 1):.1f},{round(lon, 1):.1f}"

    def _url(self, lat: float, lon: float) -> str:
        query = urllib.parse.urlencode({"latitude": lat, "longitude": lon})
        return f"{self.origin}/api/weather-effects?{query}"

    def observe(self, lat: float, lon: float, *,
                allow_network: bool = True) -> WeatherObservation:
        """Current conditions at `(lat, lon)`; calm-and-clear flagged on failure."""
        key = self._key(lat, lon)
        now = self._now()
        with self._lock:
            entry = self._cache.get(key)
        fresh = entry is not None and now - entry[0] < self.ttl_s
        reason: str | None = None
        if not fresh and not allow_network:
            reason = "not cached and network lookups are disabled on this path"
        elif not fresh:
            try:
                payload = self._request(lat, lon)
                entry = (now, payload)
                with self._lock:
                    self._cache[key] = entry
            except RealDataUnavailable as exc:
                reason = exc.reason
                self._warn("weather: %s", exc.reason)
        if entry is None:
            return _synthetic_weather(
                lat, lon,
                f"{reason or 'no weather data'}; SYNTHETIC calm/clear assumed - the "
                "wind feeding the fuel model (M15) and the sensor factor (M18) are "
                "NOT observed")
        at, payload = entry
        weather = payload.get("weather") if isinstance(payload, Mapping) else None
        if not isinstance(weather, Mapping):
            self._warn("weather: malformed payload (no weather object)")
            return _synthetic_weather(
                lat, lon, "malformed weather payload; SYNTHETIC calm/clear assumed")
        speed_kph = _finite(weather.get("windKph"))
        visibility = _finite(weather.get("visibilityM"))
        code = _finite(weather.get("weatherCode"))
        observed = weather.get("observedAt")
        observed_ms = None
        if isinstance(observed, str):
            try:
                from datetime import datetime

                observed_ms = int(datetime.fromisoformat(
                    observed.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                observed_ms = None
        stale = reason is not None
        wind_from = _finite(weather.get("windDirectionDeg"))
        precipitation = _finite(weather.get("precipitationMm"))
        condition = condition_from(code, visibility)
        # An observation can arrive REAL yet incomplete. Both gaps below fall
        # out of `sim_set_weather` / `wind_ne` as a plausible-looking zero — a
        # dead calm, a dry spell — which is exactly the silent-default pattern
        # this module exists to prevent. Say so instead.
        gaps: list[str] = []
        if speed_kph is None or wind_from is None:
            gaps.append("the observation carries no wind speed/direction, so wind_ne "
                        "is a SYNTHETIC dead calm (0,0) feeding the fuel model (M15)")
        if precipitation is None and condition in ("rain", "snow"):
            gaps.append(f"condition is {condition} but no precipitation rate was "
                        "reported, so the obscurant intensity in sim_set_weather is "
                        "0.0 by default, not measured")
        if stale:
            provenance = _synthetic(
                "weather", "gev:/api/weather-effects",
                f"refresh failed ({reason}); serving the last-good observation",
                attribution=ATTRIBUTION["weather"], retrieved_at_ms=_ms(at),
                age_s=max(0.0, now - at))
        elif gaps:
            self._warn("weather: incomplete observation (%s)", "; ".join(gaps))
            provenance = _synthetic(
                "weather", "gev:/api/weather-effects (INCOMPLETE)",
                "observation is real but incomplete: " + "; ".join(gaps),
                attribution=ATTRIBUTION["weather"], retrieved_at_ms=_ms(at),
                age_s=max(0.0, now - at))
        else:
            provenance = Provenance(
                feed="weather", real=True, source="gev:/api/weather-effects",
                attribution=ATTRIBUTION["weather"], retrieved_at_ms=_ms(at),
                age_s=max(0.0, now - at))
        return WeatherObservation(
            lat=lat, lon=lon,
            temperature_c=_finite(weather.get("temperatureC")),
            cloud_cover_pct=_finite(weather.get("cloudCoverPct")),
            precipitation_mm=precipitation,
            visibility_m=visibility,
            wind_speed_mps=None if speed_kph is None else speed_kph / 3.6,
            wind_from_deg=wind_from,
            weather_code=code, condition=condition,
            observed_at_ms=observed_ms, provenance=provenance)

    def _request(self, lat: float, lon: float) -> Mapping[str, Any]:
        resp = self._fetch(self._url(lat, lon), self.timeout_s)
        if resp.status != 200:
            raise RealDataUnavailable("weather", f"HTTP {resp.status} from {self.origin}")
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RealDataUnavailable("weather", f"unparsable response: {exc}") from exc
        if not isinstance(body, Mapping):
            raise RealDataUnavailable("weather", "malformed response (not an object)")
        return body


# ---------------------------------------------------------------------------
# The aggregate: one client, one hydrated snapshot per theater.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TheaterRealData:
    """Everything real that could be learned about one theater, plus what could not.

    `degraded_feeds` and `feeds` are the visible-degradation contract: a consumer
    can render exactly which parts of its world are observed and which are
    assumed, without having to inspect each value.
    """

    theater_id: str
    hydrated_at_ms: int
    ground: TerrainSample
    weather: WeatherObservation
    order_of_battle: OrderOfBattle
    traffic: AirspaceTraffic
    floor: FloorFix | None = None
    static_home_msl_m: float | None = None

    @property
    def feeds(self) -> dict[str, Provenance]:
        return {"terrain": self.ground.provenance, "weather": self.weather.provenance,
                "installations": self.order_of_battle.provenance,
                "traffic": self.traffic.provenance}

    @property
    def degraded_feeds(self) -> list[str]:
        return sorted(name for name, p in self.feeds.items() if p.degraded)

    @property
    def real(self) -> bool:
        """True only when EVERY feed is real."""
        return not self.degraded_feeds

    def terrain_delta_m(self) -> float | None:
        """MEASURED ground height minus the static table's `home_alt_msl_m`.

        Surfaced rather than applied. A theater whose hand-entered altitude is
        hundreds of metres off the real terrain is a fact an operator should
        see, not something this module should silently rewrite underneath a
        running geofence.

        `None` unless the ground was really measured. With the upstream down
        the "measured" ground IS the static value — it is the declared fallback
        plane — so a subtraction would report a delta of exactly 0.0 and read
        as "the hand-entered altitude is spot on" at the precise moment nothing
        was measured at all.
        """
        if self.ground.msl_m is None or self.static_home_msl_m is None:
            return None
        if not self.ground.real:
            return None
        return self.ground.msl_m - self.static_home_msl_m

    def attributions(self) -> list[str]:
        seen: list[str] = []
        for provenance in self.feeds.values():
            if provenance.attribution and provenance.attribution not in seen:
                seen.append(provenance.attribution)
        return seen

    def as_dict(self) -> dict[str, Any]:
        delta = self.terrain_delta_m()
        return {
            "schema": SCHEMA,
            "theater": self.theater_id,
            "hydrated_at_ms": self.hydrated_at_ms,
            "real": self.real,
            "degraded_feeds": self.degraded_feeds,
            "ground": self.ground.as_dict(),
            "static_home_msl_m": self.static_home_msl_m,
            "terrain_delta_m": None if delta is None else round(delta, 2),
            "terrain_delta_note": (
                "measured terrain minus the static table value; reported, never "
                "applied - the static table stays the offline default. null when "
                "the terrain feed is degraded: the 'ground' then IS the static "
                "value, so a delta would be a fabricated zero"),
            "weather": self.weather.as_dict(),
            "order_of_battle": self.order_of_battle.as_dict(),
            "traffic": self.traffic.as_dict(),
            "geofence_floor": None if self.floor is None else self.floor.as_dict(),
            "attribution": self.attributions(),
            "limits": [
                "The UAV itself is simulated; only the environment is real.",
                "Air-traffic coverage is uneven - an empty AO is an empty feed, "
                "not a guarantee of clear airspace.",
                MAPPED_DATA_CAVEAT,
            ],
        }


class RealWorldData:
    """One client over all four feeds, sharing an origin and an HTTP client.

    Nothing here is called from a telemetry tick. `hydrate_theater()` blocks (it
    is a startup / background-thread call); `hydrate_theater_async()` and
    `BackgroundRefresher` keep it off the hot path, and every provider's
    `allow_network=False` read serves whatever was learned.
    """

    def __init__(self, *, origin: str | None = None, fetch: HttpFetch | None = None,
                 now: Callable[[], float] = time.time,
                 allow_approx_geoid: bool = False,
                 fallback_ground_msl_m: float | None = None) -> None:
        import os

        self.origin = (origin or os.environ.get(GEV_ORIGIN_ENV)
                       or DEFAULT_GEV_ORIGIN).rstrip("/")
        self._now = now
        self.terrain = TerrainProvider(
            origin=self.origin, fetch=fetch, now=now,
            allow_approx_geoid=allow_approx_geoid,
            fallback_ground_msl_m=fallback_ground_msl_m)
        self.installations = InstallationsProvider(origin=self.origin, fetch=fetch, now=now)
        self.traffic = TrafficProvider(origin=self.origin, fetch=fetch, now=now,
                                       allow_approx_geoid=allow_approx_geoid)
        self.weather = WeatherProvider(origin=self.origin, fetch=fetch, now=now)

    def hydrate_theater(self, *, theater_id: str, home_lat: float, home_lon: float,
                        bbox: tuple[float, float, float, float],
                        static_home_msl_m: float | None = None,
                        traffic_radius_m: float = 50_000.0,
                        floor_points: Sequence[tuple[float, float]] | None = None,
                        floor_clearance_agl_m: float = 60.0,
                        ob_limit: int | None = 40,
                        allow_network: bool = True) -> TheaterRealData:
        """Read every feed for one theater. BLOCKS — background/startup only.

        `bbox` is `(south, west, north, east)`. Each feed fails independently:
        one dead upstream degrades its own feed and nothing else.
        """
        # A theater's own MSL is the honest declared fallback plane: it
        # reproduces the pre-existing height-above-takeoff behaviour, and the
        # samples are still flagged synthetic. It is scoped to THIS THREAD —
        # `hydrate_theater_async` and `BackgroundRefresher` run hydrations
        # concurrently, and a plane parked on the shared provider let a
        # sea-level theater hand its 0 m datum to a 1580 m plateau.
        plane = (float(static_home_msl_m)
                 if self.terrain.fallback_ground_msl_m is None
                 and static_home_msl_m is not None else None)
        with self.terrain.fallback_plane(plane):
            ground = self.terrain.height(home_lat, home_lon, allow_network=allow_network)
            floor = None
            if floor_points:
                floor = self.terrain.floor(floor_points,
                                           clearance_agl_m=floor_clearance_agl_m,
                                           allow_network=allow_network)
            south, west, north, east = bbox
            order = self.installations.order_of_battle(
                south, west, north, east, limit=ob_limit, terrain=self.terrain,
                allow_network=allow_network)
            traffic = self.traffic.contacts(home_lat, home_lon, traffic_radius_m,
                                            allow_network=allow_network)
            weather = self.weather.observe(home_lat, home_lon, allow_network=allow_network)
        return TheaterRealData(
            theater_id=theater_id, hydrated_at_ms=_ms(self._now()), ground=ground,
            weather=weather, order_of_battle=order, traffic=traffic, floor=floor,
            static_home_msl_m=static_home_msl_m)

    def hydrate_theater_async(self, *, on_result: Callable[[TheaterRealData], None] | None = None,
                              **kwargs: Any) -> threading.Thread:
        """`hydrate_theater` on a daemon thread. Returns it so tests can join."""
        def run() -> None:
            try:
                result = self.hydrate_theater(**kwargs)
            except Exception as exc:  # noqa: BLE001 — a thread must not die silently
                _LOG.warning("godSeye real-data hydration failed for %s: %s",
                             kwargs.get("theater_id"), exc)
                return
            if on_result is not None:
                on_result(result)
        thread = threading.Thread(target=run, name="godseye-realdata-hydrate", daemon=True)
        thread.start()
        return thread


class BackgroundRefresher:
    """Re-run a hydration on an interval, off every hot path.

    `stop()` is idempotent and the thread is a daemon, so a forgotten refresher
    never keeps the process alive.
    """

    def __init__(self, client: RealWorldData, *, interval_s: float = 300.0,
                 on_result: Callable[[TheaterRealData], None] | None = None,
                 **hydrate_kwargs: Any) -> None:
        self.client = client
        self.interval_s = float(interval_s)
        self.on_result = on_result
        self.hydrate_kwargs = hydrate_kwargs
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.client.hydrate_theater(**self.hydrate_kwargs)
                if self.on_result is not None:
                    self.on_result(result)
            except Exception as exc:  # noqa: BLE001 — logged, loop survives
                _LOG.warning("godSeye real-data refresh failed: %s", exc)
            self._stop.wait(self.interval_s)

    def start(self) -> "BackgroundRefresher":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop,
                                            name="godseye-realdata-refresh", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout_s)


def default_client(**kwargs: Any) -> RealWorldData:
    """A `RealWorldData` on the configured GEV origin (`GODSEYE_GEV_ORIGIN`)."""
    return RealWorldData(**kwargs)


def attribution_lines(feeds: Iterable[str] | None = None) -> list[str]:
    """Attribution text for the named feeds (all of them by default)."""
    keys = list(feeds) if feeds is not None else list(ATTRIBUTION)
    return [ATTRIBUTION[k] for k in keys if k in ATTRIBUTION]
