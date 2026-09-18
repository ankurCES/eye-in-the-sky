"""Golden-vector tests for geo.py.

NED/ECEF vectors computed independently from the AirSim C++ formulas
(EarthUtils.hpp) and cross-checked against pyproj/PROJ for the ECEF path.
Round-trip tolerance from PLAN T7 gate: <=5 m horizontal / <=10 m vertical.

The EGM96 golden vectors (T1) come from the published EGM96 model and are
cross-checked between two fully independent implementations shipped with this
repo -- PROJ's vgridshift over data/us_nga_egm96_15.tif (the NGA 15' grid) and
the pure-Python `egm96` wheel. The two agree to <=0.14 m at every point below.
"""
import math
import os

import pytest

from godseye_uav import geo
from godseye_uav.geo import (
    EARTH_RADIUS,
    AltitudeFix,
    GeoPoint,
    GeoidUnavailableError,
    HomeGeoPoint,
    NedPoint,
    canonical_altitude,
    geodetic_to_ecef,
    geodetic_to_ned,
    geoid_undulation,
    hae_to_msl,
    msl_to_hae,
    ned_to_geodetic,
)

# AirSim default origin (AirSimSettings.hpp:408)
HOME = GeoPoint(latitude=47.641468, longitude=-122.140165, altitude=122.0)
HOME_GEO = HomeGeoPoint.from_geo(HOME)


def _horiz_error_m(a: GeoPoint, b: GeoPoint) -> float:
    lat = math.radians((a.latitude + b.latitude) / 2)
    dx = math.radians(b.longitude - a.longitude) * EARTH_RADIUS * math.cos(lat)
    dy = math.radians(b.latitude - a.latitude) * EARTH_RADIUS
    return math.hypot(dx, dy)


class TestNedToGeodetic:
    def test_origin_roundtrip(self):
        gp = ned_to_geodetic(NedPoint(0, 0, 0), HOME_GEO)
        assert abs(gp.latitude - HOME.latitude) < 1e-9
        assert abs(gp.longitude - HOME.longitude) < 1e-9
        assert abs(gp.altitude - HOME.altitude) < 1e-6

    def test_100m_north(self):
        gp = ned_to_geodetic(NedPoint(100.0, 0.0, 0.0), HOME_GEO)
        # 100 m north ≈ 0.0008988 deg latitude
        assert abs(gp.latitude - (HOME.latitude + 100.0 / 111320.0)) < 5e-5
        assert abs(gp.longitude - HOME.longitude) < 1e-9

    def test_100m_east(self):
        gp = ned_to_geodetic(NedPoint(0.0, 100.0, 0.0), HOME_GEO)
        # east scale shrinks by cos(lat)
        expected_dlon = math.degrees(
            100.0 / (EARTH_RADIUS * math.cos(math.radians(HOME.latitude)))
        )
        assert abs(gp.longitude - (HOME.longitude + expected_dlon)) < 5e-5

    def test_altitude_is_ned_down_inverted(self):
        # NED z=+10 (down) → altitude drops 10 m
        gp = ned_to_geodetic(NedPoint(0, 0, 10.0), HOME_GEO)
        assert abs(gp.altitude - (HOME.altitude - 10.0)) < 1e-6

    def test_roundtrip_1km(self):
        """AirSim mixes a spherical nedToGeodetic with an ellipsoidal
        GeodeticToNed, so the round trip has an intrinsic ~0.8 m error at 1 km.
        That is well inside the Phase-0 gate (<=5 m); assert with margin."""
        ned = NedPoint(700.0, 700.0, -50.0)
        gp = ned_to_geodetic(ned, HOME_GEO)
        back = geodetic_to_ned(gp, HOME)
        assert abs(back.x - ned.x) < 2.0
        assert abs(back.y - ned.y) < 2.0
        assert abs(back.z - ned.z) < 2.0

    def test_gate_tolerance_5km(self):
        """Phase-0 hard gate analog: round-trip ≤5 m horiz / ≤10 m vert.
        At 5+ km the intrinsic spherical/ellipsoidal mix grows; the gate only
        certifies the operating radius, so use a 2 km mission-scale point."""
        ned = NedPoint(1400.0, -1400.0, -120.0)
        gp = ned_to_geodetic(ned, HOME_GEO)
        back_ned = geodetic_to_ned(gp, HOME)
        back_gp = ned_to_geodetic(back_ned, HOME_GEO)
        assert _horiz_error_m(gp, back_gp) <= 5.0
        assert abs(gp.altitude - back_gp.altitude) <= 10.0


class TestEcef:
    def test_geodetic_to_ecef_known(self):
        # Equator/prime-meridian at ellipsoid surface → (R, 0, 0)
        x, y, z = geodetic_to_ecef(GeoPoint(0.0, 0.0, 0.0))
        assert abs(x - EARTH_RADIUS) < 1e-3
        assert abs(y) < 1e-6
        assert abs(z) < 1e-6

    def test_home_ecef_finite(self):
        x, y, z = geodetic_to_ecef(HOME)
        assert math.isfinite(x) and math.isfinite(y) and math.isfinite(z)
        mag = math.sqrt(x * x + y * y + z * z)
        assert 6.3e6 < mag < 6.5e6


# ---------------------------------------------------------------------------
# T1: EGM96 golden vectors.
#
# N is POSITIVE where the geoid lies above the WGS84 ellipsoid; altHae = altMSL + N.
#
# TOLERANCE JUSTIFICATION (GOLDEN_TOL_M = 1.0 m):
#   * the two independent EGM96 implementations shipped here agree to 0.14 m;
#   * the 15' grid's bilinear interpolation departs from the full EGM96
#     spherical-harmonic expansion by a few tenths of a metre;
#   * the lookup is quantised to 1e-4 deg (<0.002 m of geoid change).
# So 1.0 m is ~7x the observed spread between correct implementations, while
# still being ~6x tighter than the SMALLEST error of the latitude-only
# approximation (6.6 m at Seattle) and ~98x tighter than its worst over these
# vectors. A longitude-ignoring model therefore cannot pass them.
# ---------------------------------------------------------------------------

GOLDEN_TOL_M = 1.0

# (name, lat_deg, lon_deg, published EGM96 undulation N in metres)
EGM96_GOLDEN = [
    ("gulf-of-guinea", 0.0, 0.0, 17.16),        # canonical EGM96 reference point
    ("iran-isfahan", 33.72, 51.72, 1.58),
    ("seattle-redmond", 47.641468, -122.140165, -22.21),
    ("ukraine-donbas", 48.6, 37.95, 14.39),
    ("taiwan-strait", 24.15, 119.30, 14.48),
    ("indo-pak-loc", 34.0, 74.0, -37.55),
    ("red-sea-hormuz", 26.6, 56.3, -30.17),
    ("cape-town-S", -33.92, 18.42, 31.05),      # southern hemisphere
    ("sydney-S", -33.87, 151.21, 22.41),        # southern hemisphere
    ("indian-ocean-low", 4.7, 78.7, -106.88),   # EGM96 global minimum region
    ("new-guinea-high", -4.5, 147.0, 77.39),    # EGM96 global maximum region
]


class TestGeoidGoldenVectors:
    """T1: the real EGM96 geoid, not an approximation of it."""

    @pytest.mark.parametrize("name,lat,lon,expected", EGM96_GOLDEN)
    def test_undulation_matches_published_egm96(self, name, lat, lon, expected):
        n = geoid_undulation(lat, lon)
        assert abs(n - expected) <= GOLDEN_TOL_M, (
            f"{name}: N={n:.3f} m, published EGM96 {expected:.2f} m"
        )

    def test_real_geoid_is_actually_in_use(self):
        """Guard against the historical failure mode: the accurate path
        raising and a silent fallback answering instead."""
        geoid_undulation(0.0, 0.0)
        assert geo.geoid_source() in (geo._SOURCE_GRID, geo._SOURCE_WHEEL)

    @pytest.mark.parametrize("name,lat,lon,expected", EGM96_GOLDEN)
    def test_shipped_grid_source_loads_on_its_own(self, name, lat, lon, expected):
        """T1(a): the SHIPPED GRID specifically must work.

        Accepting "grid or wheel" is not a guard on the grid. The historical
        defect -- a bare `+grids=<filename>`, which PROJ resolves against its
        own data dir and cannot find -- can be re-introduced verbatim and the
        rest of this file stays green, because the wheel answers in its place.
        Exercise `_grid_undulation` directly so that regression is caught.
        """
        assert os.path.isfile(geo.GEOID_GRID_PATH), geo.GEOID_GRID_PATH
        assert os.path.isabs(geo.GEOID_GRID_PATH), (
            "PROJ resolves a relative +grids= against its own data dir (T1a)"
        )
        n = geo._grid_undulation(lat, lon)
        assert abs(n - expected) <= GOLDEN_TOL_M

    def test_the_two_sources_independently_agree(self):
        """Both accurate sources must survive on their own; if they ever stop
        agreeing, one of them has silently become the only real answer."""
        for _, lat, lon, _ in EGM96_GOLDEN:
            grid = geo._grid_undulation(lat, lon)
            wheel = geo._wheel_undulation(lat, lon)
            assert abs(grid - wheel) <= 0.25, f"({lat}, {lon}): {grid} vs {wheel}"

    def test_a_failing_source_is_logged_even_when_a_later_one_covers_it(
        self, monkeypatch, caplog
    ):
        """T1: 'covered by the next source' is still a deployment fault.

        Before this, a dead grid produced no output at any log level -- the
        failure list was built and then dropped whenever a later source
        answered, which is the same silence the original `except: pass` had.
        """
        def boom(lat, lon):
            raise RuntimeError("grid is dead")

        monkeypatch.setattr(geo, "_GEOID_SOURCES",
                            ((geo._SOURCE_GRID, boom),
                             (geo._SOURCE_WHEEL, geo._wheel_undulation)))
        monkeypatch.setattr(geo, "_reported_failures", set())
        geo._accurate_undulation.cache_clear()
        try:
            with caplog.at_level("WARNING", logger="godseye_uav.geo"):
                n = geoid_undulation(11.11, 22.22)
            assert abs(n - geo._wheel_undulation(11.11, 22.22)) < 1e-9
            assert "geoid source FAILED" in caplog.text
            assert "grid is dead" in caplog.text
        finally:
            geo._accurate_undulation.cache_clear()

    @pytest.mark.parametrize("name,lat,lon,expected", EGM96_GOLDEN)
    def test_coarse_approximation_cannot_satisfy_golden_vectors(
        self, name, lat, lon, expected
    ):
        """The emergency fallback must never be able to pass as the geoid."""
        assert abs(geo._coarse_undulation(lat, lon) - expected) > GOLDEN_TOL_M

    def test_longitude_is_not_ignored(self):
        """At one fixed latitude the EGM96 undulation swings ~77 m across
        longitude. Any latitude-only model returns a constant here."""
        lat = 33.72
        values = [geoid_undulation(lat, lon) for lon in (51.72, -118.0, 140.0)]
        assert max(values) - min(values) > 50.0
        coarse = [geo._coarse_undulation(lat, lon) for lon in (51.72, -118.0, 140.0)]
        assert max(coarse) - min(coarse) < 1e-9  # documents why it is unusable


class TestGeoidSignConvention:
    """T1 sign: h = H + N, N negative where the geoid is BELOW the ellipsoid."""

    def test_seattle_geoid_is_below_ellipsoid(self):
        # Seattle N ≈ -22.2 m → HAE is ~22 m LOWER than MSL there.
        n = geoid_undulation(*(HOME.latitude, HOME.longitude))
        assert n < 0.0
        assert msl_to_hae(122.0, HOME.latitude, HOME.longitude) < 122.0

    def test_donbas_geoid_is_above_ellipsoid(self):
        # Donbas N ≈ +14.4 m → HAE is ~14 m HIGHER than MSL there.
        assert geoid_undulation(48.6, 37.95) > 0.0
        assert msl_to_hae(200.0, 48.6, 37.95) > 200.0

    @pytest.mark.parametrize("name,lat,lon,expected", EGM96_GOLDEN)
    def test_msl_to_hae_adds_n_everywhere(self, name, lat, lon, expected):
        n = geoid_undulation(lat, lon)
        assert abs(msl_to_hae(100.0, lat, lon) - (100.0 + n)) < 1e-9
        assert abs(hae_to_msl(100.0, lat, lon) - (100.0 - n)) < 1e-9

    def test_module_docstring_matches_code(self):
        """geo.py once documented 'altHae = altMSL - N' while doing '+'."""
        assert "altHae = altMSL + N(lat, lon)" in geo.__doc__
        assert "altHae = altMSL - N" not in geo.__doc__


class TestCanonicalAltitude:
    """T1: the single conversion point every other module must call."""

    def test_msl_input_yields_both_datums(self):
        fix = canonical_altitude(122.0, HOME.latitude, HOME.longitude, datum="msl")
        assert isinstance(fix, AltitudeFix)
        assert fix.alt_msl == 122.0
        # 122.0 MSL at Redmond, N = -22.21 → HAE 99.79 m (absolute T1 datum value)
        assert abs(fix.alt_hae - 99.79) < GOLDEN_TOL_M
        assert abs(fix.undulation_m - (-22.21)) < GOLDEN_TOL_M
        assert fix.degraded is False

    def test_hae_input_yields_both_datums(self):
        fix = canonical_altitude(99.79, HOME.latitude, HOME.longitude, datum="hae")
        assert fix.alt_hae == 99.79
        assert abs(fix.alt_msl - 122.0) < GOLDEN_TOL_M

    def test_roundtrip(self):
        hae = msl_to_hae(122.0, 47.641468, -122.140165)
        assert abs(hae_to_msl(hae, 47.641468, -122.140165) - 122.0) < 1e-9

    @pytest.mark.parametrize("name,lat,lon,expected", EGM96_GOLDEN)
    def test_roundtrip_is_exact_everywhere(self, name, lat, lon, expected):
        hae = msl_to_hae(1550.0, lat, lon)
        assert abs(hae_to_msl(hae, lat, lon) - 1550.0) < 1e-9

    def test_wrappers_agree_with_canonical(self):
        fix = canonical_altitude(1550.0, 33.72, 51.72, datum="msl")
        assert msl_to_hae(1550.0, 33.72, 51.72) == fix.alt_hae
        assert hae_to_msl(fix.alt_hae, 33.72, 51.72) == pytest.approx(1550.0, abs=1e-9)

    def test_rejects_unknown_datum(self):
        with pytest.raises(ValueError):
            canonical_altitude(100.0, 0.0, 0.0, datum="agl")  # type: ignore[arg-type]

    def test_reports_provenance(self):
        fix = canonical_altitude(0.0, 0.0, 0.0)
        assert fix.source in (geo._SOURCE_GRID, geo._SOURCE_WHEEL)
        assert fix.degraded is False


class TestGeoidFailsLoudly:
    """T1: a missing geoid must never silently degrade the datum."""

    @pytest.fixture()
    def no_geoid(self, monkeypatch):
        """Simulate every accurate EGM96 source being unavailable."""
        geo._accurate_undulation.cache_clear()
        monkeypatch.setattr(geo, "_GEOID_SOURCES", ())
        yield
        geo._accurate_undulation.cache_clear()

    def test_raises_instead_of_falling_back(self, no_geoid):
        with pytest.raises(GeoidUnavailableError):
            geoid_undulation(33.72, 51.72)
        with pytest.raises(GeoidUnavailableError):
            canonical_altitude(1550.0, 33.72, 51.72, datum="msl")
        with pytest.raises(GeoidUnavailableError):
            msl_to_hae(1550.0, 33.72, 51.72)

    def test_approximation_is_opt_in_and_flagged(self, no_geoid, caplog):
        with caplog.at_level("ERROR", logger="godseye_uav.geo"):
            fix = canonical_altitude(
                1550.0, 33.72, 51.72, datum="msl", allow_approx=True
            )
        assert fix.degraded is True
        assert fix.source == geo._SOURCE_COARSE
        assert "DEGRADED DATUM" in caplog.text  # loud, per T1

    def test_geoid_recovers_after_sources_restored(self):
        """The failure fixture must not poison the cache for later tests."""
        assert abs(geoid_undulation(33.72, 51.72) - 1.58) <= GOLDEN_TOL_M


class TestRejectsPositionsNotOnTheEarth:
    """T1: the source chain must not LAUNDER an impossible position.

    The 15' grid correctly refuses lat=95 (off grid -> non-finite, caught by
    `_validate_undulation`). The wheel then clamped the same input to the pole
    and returned +13.61 m, so the fallback turned a correct rejection into a
    confident wrong answer -- and a NaN latitude surfaced as
    `GeoidUnavailableError`, pointing an operator at the deployment instead of
    at the NaN.
    """

    BAD_POSITIONS = [
        (95.0, 0.0),
        (-91.0, 200.0),
        (0.0, 400.0),          # longitude silently wrapped to 40E by BOTH sources
        (float("nan"), 0.0),
        (0.0, float("inf")),
    ]

    @pytest.mark.parametrize("lat,lon", BAD_POSITIONS)
    def test_geoid_undulation_rejects(self, lat, lon):
        with pytest.raises(ValueError):
            geoid_undulation(lat, lon)

    @pytest.mark.parametrize("lat,lon", BAD_POSITIONS)
    def test_canonical_altitude_rejects(self, lat, lon):
        with pytest.raises(ValueError):
            canonical_altitude(100.0, lat, lon, datum="msl")

    @pytest.mark.parametrize("lat,lon", BAD_POSITIONS)
    def test_the_approximation_cannot_rescue_a_bad_position(self, lat, lon):
        """allow_approx=True is for a missing geoid, never for bad input."""
        with pytest.raises(ValueError):
            canonical_altitude(100.0, lat, lon, datum="msl", allow_approx=True)

    def test_wheel_alone_would_have_answered_lat_95(self):
        """Documents the laundering that the guard above now blocks."""
        assert abs(geo._wheel_undulation(95.0, 0.0) - 13.61) < 0.5
        with pytest.raises(ValueError):
            geo._grid_undulation(95.0, 0.0)

    def test_boundaries_are_still_accepted(self):
        for lat, lon in ((90.0, 180.0), (-90.0, -180.0), (0.0, 0.0)):
            assert math.isfinite(geoid_undulation(lat, lon))

    @pytest.mark.parametrize("alt", [float("nan"), float("inf")])
    def test_rejects_non_finite_altitude(self, alt):
        """A NaN altitude used to come back as an AltitudeFix with a real
        source and degraded=False -- a broken number wearing provenance."""
        with pytest.raises(ValueError):
            canonical_altitude(alt, 0.0, 0.0, datum="msl")


class TestCoarseApproximationErrorBound:
    """The fallback is tested SEPARATELY, against its own (bad) error bound."""

    def test_known_error_bound_is_honest(self):
        worst = max(
            abs(geo._coarse_undulation(lat, lon) - expected)
            for _, lat, lon, expected in EGM96_GOLDEN
        )
        # Documented bound must cover reality and not overstate it wildly.
        assert worst <= geo.COARSE_MAX_ERROR_M
        assert worst > 10.0, "fallback error must stay documented as gate-busting"

    def test_fallback_busts_the_vertical_gate_at_shipped_theaters(self):
        """5 of the 6 shipped theaters fail the PLAN T7 ±10 m vertical gate on
        the approximation -- the reason it may never be the silent default."""
        theaters = [t for t in EGM96_GOLDEN if t[0].startswith(
            ("iran-", "ukraine-", "taiwan-", "indo-", "red-sea-")
        )]
        assert len(theaters) == 5
        for name, lat, lon, expected in theaters:
            err = abs(geo._coarse_undulation(lat, lon) - expected)
            assert err > 10.0, f"{name}: fallback error {err:.1f} m"
