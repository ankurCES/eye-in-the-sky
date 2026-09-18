"""Geo math for godSeye: NED<->WGS84, and THE canonical altitude datum point.

Ported from AirSim AirLib/include/common/EarthUtils.hpp (verified).
EARTH_RADIUS = 6378137.0 (WGS84 semi-major, common_utils/Utils.hpp:49).

Datum convention (T1) -- the standard geodetic identity, no exceptions:

    altHae = altMSL + N(lat, lon)        h = H + N
    altMSL = altHae - N(lat, lon)        H = h - N

where h = height above the WGS84 ellipsoid (HAE, the canonical altitude in
this system), H = orthometric height above the EGM96 geoid (what settings and
operators call "MSL"), and N = the EGM96 geoid undulation, POSITIVE where the
geoid lies above the ellipsoid.  Reference values from the shipped NGA 15'
grid: N(0, 0) = +17.16 m, N(Seattle) = -22.21 m, N(Isfahan) = +1.58 m,
N(Donbas) = +14.39 m.

AirSim works in NED relative to a home geopoint whose altitude is entered as
MSL (settings.json OriginGeopoint).  Every module in the system MUST route its
datum conversion through `canonical_altitude()` below and nowhere else -- ONE
conversion point, never per-call-site (PLAN.md:83, T1).  Everything downstream
(GEV Cesium, track store, mission overlays) consumes altHae only.

The geoid never degrades silently (T1): if no accurate EGM96 source can be
loaded, `canonical_altitude()` raises `GeoidUnavailableError` rather than fall
back to an approximation.  The crude `_coarse_undulation()` model is opt-in
only via `allow_approx=True`, logs at ERROR, and flags the result degraded.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Literal

EARTH_RADIUS = 6378137.0  # WGS84 semi-major axis, metres (AirSim Utils.hpp:49)
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = 6.69437999014e-3  # first eccentricity squared (matches AirSim constant)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeoPoint:
    """WGS84 geodetic point. altitude = altHae (ellipsoid), metres."""

    latitude: float  # degrees
    longitude: float  # degrees
    altitude: float  # altHae metres


@dataclass(frozen=True)
class NedPoint:
    """Local tangent-plane NED offset from home, metres."""

    x: float  # North
    y: float  # East
    z: float  # Down (positive down)


@dataclass(frozen=True)
class HomeGeoPoint:
    geo: GeoPoint
    lat_rad: float
    lon_rad: float
    cos_lat: float
    sin_lat: float

    @classmethod
    def from_geo(cls, geo: GeoPoint) -> "HomeGeoPoint":
        lat_rad = math.radians(geo.latitude)
        lon_rad = math.radians(geo.longitude)
        return cls(
            geo=geo,
            lat_rad=lat_rad,
            lon_rad=lon_rad,
            cos_lat=math.cos(lat_rad),
            sin_lat=math.sin(lat_rad),
        )


def ned_to_geodetic(v: NedPoint, home: HomeGeoPoint) -> GeoPoint:
    """Exact port of EarthUtils::nedToGeodetic (EarthUtils.hpp:291)."""
    x_rad = v.x / EARTH_RADIUS
    y_rad = v.y / EARTH_RADIUS
    c = math.sqrt(x_rad * x_rad + y_rad * y_rad)
    sin_c, cos_c = math.sin(c), math.cos(c)
    if c > 1e-12:
        lat_rad = math.asin(
            cos_c * home.sin_lat + (x_rad * sin_c * home.cos_lat) / c
        )
        lon_rad = home.lon_rad + math.atan2(
            y_rad * sin_c,
            c * home.cos_lat * cos_c - x_rad * home.sin_lat * sin_c,
        )
        return GeoPoint(
            math.degrees(lat_rad),
            math.degrees(lon_rad),
            home.geo.altitude - v.z,
        )
    return GeoPoint(home.geo.latitude, home.geo.longitude, home.geo.altitude - v.z)


def geodetic_to_ecef(geo: GeoPoint) -> tuple[float, float, float]:
    """Exact port of EarthUtils::GeodeticToEcef."""
    lat_rad = math.radians(geo.latitude)
    lon_rad = math.radians(geo.longitude)
    xi = math.sqrt(1.0 - _WGS84_E2 * math.sin(lat_rad) * math.sin(lat_rad))
    x = (EARTH_RADIUS / xi + geo.altitude) * math.cos(lat_rad) * math.cos(lon_rad)
    y = (EARTH_RADIUS / xi + geo.altitude) * math.cos(lat_rad) * math.sin(lon_rad)
    z = (EARTH_RADIUS / xi * (1.0 - _WGS84_E2) + geo.altitude) * math.sin(lat_rad)
    return (x, y, z)


def ecef_to_ned(
    ecef: tuple[float, float, float],
    ecef_home: tuple[float, float, float],
    geo_home: GeoPoint,
) -> NedPoint:
    """Exact port of EarthUtils::EcefToNed."""
    lat_rad = math.radians(geo_home.latitude)
    lon_rad = math.radians(geo_home.longitude)
    vx, vy, vz = (
        ecef[0] - ecef_home[0],
        ecef[1] - ecef_home[1],
        ecef[2] - ecef_home[2],
    )
    slat, clat = math.sin(lat_rad), math.cos(lat_rad)
    slon, clon = math.sin(lon_rad), math.cos(lon_rad)
    # rotation matrix rows
    n = -slat * clon * vx + -slat * slon * vy + clat * vz
    e = -slon * vx + clon * vy
    d = clat * clon * vx + clat * slon * vy + slat * vz
    return NedPoint(n, e, -d)


def geodetic_to_ned(geo: GeoPoint, home: GeoPoint) -> NedPoint:
    """Port of EarthUtils::GeodeticToNed."""
    return ecef_to_ned(geodetic_to_ecef(geo), geodetic_to_ecef(home), home)


# ---------------------------------------------------------------------------
# EGM96 geoid undulation (T1).
#
# N = height of the EGM96 geoid ABOVE the WGS84 ellipsoid, metres, signed.
#     altHae = altMSL + N        altMSL = altHae - N
#
# Two independent accurate sources are shipped with the repo and agree to
# <=0.15 m at every point checked:
#   1. data/us_nga_egm96_15.tif   -- the NGA 15' grid, read through PROJ's
#      vgridshift (pyproj).  The grid MUST be named by ABSOLUTE PATH: PROJ
#      resolves a bare filename against its own data dir, which is why the
#      previous pipeline raised "Error 1029 ... could not find required
#      grid(s)" on EVERY call and silently fell through to the approximation.
#      The absolute path is the whole fix.  The previous attempt to force it
#      with pyproj.datadir.set_data_dir(<our data dir>) plus PROJ_DATA/
#      PROJ_NETWORK env writes did not work and is not repeated here: it
#      mutates process-global PROJ state for every other pyproj user in the
#      process, and the bare filename still failed to resolve.
#   2. the vendored `egm96` wheel (egm96-0.3.0-py3-none-any.whl), a pure
#      Python reader of the same model (max interpolation error 0.06 m).
# ---------------------------------------------------------------------------

GEOID_GRID_PATH = os.path.join(os.path.dirname(__file__), "data", "us_nga_egm96_15.tif")

_SOURCE_GRID = "egm96-grid:us_nga_egm96_15.tif"
_SOURCE_WHEEL = "egm96-wheel"
_SOURCE_COARSE = "coarse-approx:DEGRADED"

#: Measured worst-case error of `_coarse_undulation` against real EGM96:
#: 111.4 m globally (sampled on a 2-degree grid; worst near 65N 18W) and 58 m
#: over the shipped theaters.  Far outside the PLAN T7 +/-10 m vertical gate
#: -- hence the approximation is opt-in only and never the default.
COARSE_MAX_ERROR_M = 115.0

#: Undulation lookups are quantised to this many decimal degrees before the
#: cache.  1e-4 deg ~= 11 m on the ground; the EGM96 geoid gradient is below
#: 1e-4 m/m, so the quantisation error is under 0.002 m -- negligible against
#: the metre-level tolerances this module is gated on, and it turns a hovering
#: vehicle's per-sample lookup into a cache hit.
_CACHE_DECIMALS = 4

_local = threading.local()  # pyproj Transformers are not thread-safe
_active_source: str | None = None
_reported_failures: set[str] = set()


class GeoidUnavailableError(RuntimeError):
    """No accurate EGM96 source could be loaded (T1).

    Raised instead of silently degrading to `_coarse_undulation`, which is
    wrong by up to ~111 m and ignores longitude entirely.
    """


def _validate_geodetic(lat_deg: float, lon_deg: float) -> tuple[float, float]:
    """Reject positions that are not on the Earth, before any lookup (T1).

    Without this the source chain LAUNDERS a bad position: the 15' grid
    correctly refuses lat=95 (off grid -> non-finite), and the wheel then
    clamps it to the pole and answers +13.61 m as if nothing happened. A
    non-finite input was likewise reported as `GeoidUnavailableError`, which
    sends an operator to look at the deployment instead of at the NaN.
    """
    lat, lon = float(lat_deg), float(lon_deg)
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError(f"non-finite geodetic position ({lat_deg}, {lon_deg})")
    if abs(lat) > 90.0:
        raise ValueError(f"latitude {lat} outside [-90, 90]")
    if abs(lon) > 180.0:
        raise ValueError(f"longitude {lon} outside [-180, 180]")
    return lat, lon


def _grid_undulation(lat_deg: float, lon_deg: float) -> float:
    """N from the shipped NGA 15' grid via PROJ vgridshift (T1)."""
    tr = getattr(_local, "vgridshift", None)
    if tr is None:
        import pyproj  # type: ignore

        if not os.path.isfile(GEOID_GRID_PATH):
            raise FileNotFoundError(f"EGM96 grid missing: {GEOID_GRID_PATH}")
        # Absolute path + no datadir mutation: see the note above.
        tr = pyproj.Transformer.from_pipeline(
            f"+proj=vgridshift +grids={GEOID_GRID_PATH} +multiplier=1"
        )
        _local.vgridshift = tr
    # Forward vgridshift on z=0 (geoid-referenced) yields exactly N.
    _, _, n = tr.transform(lon_deg, lat_deg, 0.0)
    return _validate_undulation(float(n))


def _wheel_undulation(lat_deg: float, lon_deg: float) -> float:
    """N from the vendored pure-Python `egm96` wheel (T1)."""
    import egm96  # type: ignore

    return _validate_undulation(float(egm96.undulation(lat_deg, lon_deg)))


def _validate_undulation(n: float) -> float:
    """Reject NaN / off-grid / physically impossible undulations (T1).

    PROJ hands back a non-finite value for a point its grid does not cover, so
    this is what lets a partial grid fall through to the next source instead of
    poisoning an altitude.
    """
    if not math.isfinite(n):
        raise ValueError("geoid undulation is not finite (point off grid?)")
    if abs(n) > 120.0:  # EGM96 global range is -106.9 .. +85.4 m
        raise ValueError(f"geoid undulation {n:.3f} m outside EGM96 range")
    return n


_GEOID_SOURCES: tuple[tuple[str, Callable[[float, float], float]], ...] = (
    (_SOURCE_GRID, _grid_undulation),
    (_SOURCE_WHEEL, _wheel_undulation),
)


@lru_cache(maxsize=8192)
def _accurate_undulation(lat_q: float, lon_q: float) -> tuple[float, str]:
    """Try each accurate EGM96 source in order; raise if none works (T1)."""
    global _active_source
    _validate_geodetic(lat_q, lon_q)
    failures: list[str] = []
    for name, fn in _GEOID_SOURCES:
        try:
            n = fn(lat_q, lon_q)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        # A source that fell over but was covered by the next one is still a
        # deployment fault: the shipped grid dying and the wheel answering in
        # its place is invisible otherwise, which is how the dead EGM96 path
        # survived undetected in the first place (T1).
        for f in failures:
            if f not in _reported_failures:
                _reported_failures.add(f)
                _LOG.warning(
                    "godSeye geoid source FAILED, falling through to %s -> %s",
                    name, f,
                )
        if _active_source != name:
            _active_source = name
            _LOG.info("godSeye geoid source active: %s", name)
        return n, name
    raise GeoidUnavailableError(
        "No accurate EGM96 geoid source available; refusing to degrade "
        "silently (T1). Tried -> " + " | ".join(failures)
    )


def geoid_source() -> str | None:
    """Name of the EGM96 source currently in use, or None before the first
    lookup. Diagnostics only -- callers get provenance on every AltitudeFix."""
    return _active_source


def geoid_undulation(
    lat_deg: float, lon_deg: float, *, allow_approx: bool = False
) -> float:
    """EGM96 geoid undulation N in metres, POSITIVE where the geoid is above
    the WGS84 ellipsoid (T1).

    Accuracy: ~0.1 m against the published EGM96 model. Raises
    `GeoidUnavailableError` if no accurate source loads, unless
    `allow_approx=True`, which permits the ~111 m-error emergency fallback and
    logs it at ERROR. A position that is not on the Earth raises `ValueError`
    and is NEVER rescued by the approximation.
    """
    lat_deg, lon_deg = _validate_geodetic(lat_deg, lon_deg)
    lat_q = round(float(lat_deg), _CACHE_DECIMALS)
    lon_q = round(float(lon_deg), _CACHE_DECIMALS)
    try:
        return _accurate_undulation(lat_q, lon_q)[0]
    except GeoidUnavailableError:
        if not allow_approx:
            raise
        _LOG.error(
            "DEGRADED DATUM: EGM96 unavailable, using coarse undulation at "
            "(%.4f, %.4f); error up to %.0f m -- altitudes are NOT gate-grade",
            lat_q, lon_q, COARSE_MAX_ERROR_M,
        )
        return _coarse_undulation(lat_q, lon_q)


def _coarse_undulation(lat_deg: float, lon_deg: float) -> float:
    """EMERGENCY-ONLY latitude-only fudge; NOT a geoid (T1).

    Ignores longitude entirely and is wrong by up to ~111 m globally
    (COARSE_MAX_ERROR_M) against real EGM96 -- 5 of the 6 shipped theaters bust
    the +/-10 m vertical gate on it. Reachable only via `allow_approx=True`;
    never used implicitly.
    """
    lat = math.radians(lat_deg)
    return -30.0 + 20.0 * math.cos(2.0 * lat) + 5.0 * math.sin(3.0 * lat)


# ---------------------------------------------------------------------------
# THE canonical altitude conversion point (PLAN.md:83, T1).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AltitudeFix:
    """One altitude resolved into BOTH datums, with provenance (T1).

    Carrying both plus `source`/`degraded` is what stops callers re-deriving
    the conversion themselves -- the exact divergence T1 exists to prevent.
    """

    alt_hae: float  # metres above the WGS84 ellipsoid (canonical)
    alt_msl: float  # metres above the EGM96 geoid (orthometric)
    undulation_m: float  # N, signed, positive where geoid is above ellipsoid
    source: str  # which EGM96 source produced N
    degraded: bool  # True only when the coarse fallback was used


def canonical_altitude(
    alt_m: float,
    lat_deg: float,
    lon_deg: float,
    *,
    datum: Literal["msl", "hae"] = "msl",
    allow_approx: bool = False,
) -> AltitudeFix:
    """THE single altitude datum conversion point for godSeye (PLAN.md:83, T1).

    Every module that needs MSL<->HAE MUST call this and nothing else; the
    bridge, the MCP server, the track store and the mission overlays all
    converting separately is what made the bridge and the server disagree by
    17.4 m on the same vehicle at the same instant.

    Args:
        alt_m: the altitude to convert, metres.
        lat_deg, lon_deg: WGS84 geodetic position of that altitude, degrees.
        datum: which datum `alt_m` is expressed in -- "msl" (orthometric,
            e.g. settings.json OriginGeopoint and AirSim GPS altitude) or
            "hae" (already ellipsoidal).
        allow_approx: permit the degraded coarse fallback if EGM96 cannot be
            loaded. Default False, which raises instead.

    Returns:
        AltitudeFix with both datums, the undulation used, and its provenance.

    Raises:
        GeoidUnavailableError: no accurate EGM96 source and allow_approx=False.
        ValueError: unknown `datum`, non-finite altitude, or a position that is
            not on the Earth.
    """
    key = str(datum).lower()
    if key not in ("msl", "hae"):
        raise ValueError(f"datum must be 'msl' or 'hae', got {datum!r}")
    if not math.isfinite(float(alt_m)):
        # Otherwise a NaN altitude comes back out as an AltitudeFix carrying a
        # real source and degraded=False -- a broken value wearing provenance.
        raise ValueError(f"altitude must be finite, got {alt_m!r}")

    lat_deg, lon_deg = _validate_geodetic(lat_deg, lon_deg)
    lat_q = round(float(lat_deg), _CACHE_DECIMALS)
    lon_q = round(float(lon_deg), _CACHE_DECIMALS)
    try:
        n, source = _accurate_undulation(lat_q, lon_q)
        degraded = False
    except GeoidUnavailableError:
        if not allow_approx:
            raise
        _LOG.error(
            "DEGRADED DATUM: EGM96 unavailable at (%.4f, %.4f); coarse "
            "fallback error up to %.0f m -- altitudes are NOT gate-grade",
            lat_q, lon_q, COARSE_MAX_ERROR_M,
        )
        n, source, degraded = _coarse_undulation(lat_q, lon_q), _SOURCE_COARSE, True

    alt = float(alt_m)
    if key == "msl":
        return AltitudeFix(alt + n, alt, n, source, degraded)  # h = H + N
    return AltitudeFix(alt, alt - n, n, source, degraded)  # H = h - N


def msl_to_hae(
    alt_msl: float, lat_deg: float, lon_deg: float, *, allow_approx: bool = False
) -> float:
    """altHae = altMSL + N. Sugar over `canonical_altitude` (T1)."""
    return canonical_altitude(
        alt_msl, lat_deg, lon_deg, datum="msl", allow_approx=allow_approx
    ).alt_hae


def hae_to_msl(
    alt_hae: float, lat_deg: float, lon_deg: float, *, allow_approx: bool = False
) -> float:
    """altMSL = altHae - N. Sugar over `canonical_altitude` (T1)."""
    return canonical_altitude(
        alt_hae, lat_deg, lon_deg, datum="hae", allow_approx=allow_approx
    ).alt_msl
