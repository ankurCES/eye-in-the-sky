"""Phase-0 HARD GATE (PLAN T7 + T1): geo-registration round-trip.

This gate certifies that a point of interest survives the ACTUAL conversion
path the product uses, end to end, INCLUDING the altitude datum leg:

    known truth (lat, lon, alt_MSL)                 -- what an operator states
      -> canonical_altitude(datum="msl").alt_hae    -- T1, the ONE datum point
      -> geodetic_to_ned(..., home_hae)             -- server.py llh_to_ned
      -> [sim: simSpawnObject over msgpack-rpc; the sim registers the marker
          geodetically and hands it back through simGetObjectPose]
      -> ned_to_geodetic(...)                       -- bridge.py snapshot path
      -> canonical_altitude(datum="hae").alt_msl    -- T1, back to the datum
    assert <=5 m horizontal and <=10 m vertical against the known truth.

The predecessor of this file asserted only ned_to_geodetic -> geodetic_to_ned
-> ned_to_geodetic at ONE mid-latitude origin: a pure-math self-consistency
loop that never touched a marker, the sim, or any altitude datum.

WHAT THIS ROUND TRIP CAN AND CANNOT CATCH -- measured, not assumed:

  * it catches a datum MISMATCH between the two legs (the MCP server
    converting one way and the bridge another): `test_gate_detects_datum_error`.
  * it is STRUCTURALLY BLIND to a datum that is wrong but CONSISTENT, because
    the outbound `+N` and the inbound `-N` cancel algebraically. Measured
    residual on the real geoid is 0.063 m against a 10 m tolerance -- there is
    no headroom being consumed, so there is nothing for a consistent error to
    push out of tolerance. Injecting a globally sign-flipped EGM96 (historical
    defect T1(b)) leaves all 36 registration cases and all 10 sim cases GREEN;
    only the VALUE assertions catch it. See
    `test_registration_roundtrip_is_blind_to_a_consistent_datum_error`.

So the real guards on the datum's VALUE are `test_datum_leg_is_actually_exercised`
here and the EGM96 golden vectors in tests/test_geo.py -- not this round trip.

Home altitudes are declared MSL, matching AirSim settings.json OriginGeopoint
(PLAN.md:66), and are converted to HAE exactly once, because geo.GeoPoint
.altitude is HAE by contract (geo.py GeoPoint docstring).

Gate 1 runs the full conversion path offline across all six shipped theaters
out to the certified operating radius. Gate 2 proves the gate now fails on a
broken datum. Gate 3 repeats the round trip across the real msgpack-rpc sim
boundary using FakeAirSim and the in-repo airsim PythonClient (port 46017).
"""
import math
import sys
import time

import pytest

# The AirSim PythonClient is put on sys.path by tests/conftest.py, which
# resolves it RELATIVE to the repo (sibling checkout, ci.sh's vendored copy,
# or $GODSEYE_AIRSIM_PYTHONCLIENT) and raises a message naming everything it
# searched when it cannot find one. This module used to carry an absolute
# path into one developer's home directory, which no clone or CI runner
# could satisfy.

from godseye_uav import geo  # noqa: E402
from godseye_uav.fake_airsim import FakeAirSim  # noqa: E402
from godseye_uav.geo import (  # noqa: E402
    EARTH_RADIUS,
    GeoPoint,
    HomeGeoPoint,
    NedPoint,
    canonical_altitude,
    geodetic_to_ned,
    ned_to_geodetic,
)

import airsim  # noqa: E402  (the in-repo PythonClient)

GATE_HORIZ_M = 5.0
GATE_VERT_M = 10.0

#: Certified operating radius, metres.
#:
#: The AirSim port mixes a SPHERICAL nedToGeodetic (EarthUtils.hpp:291) with an
#: ELLIPSOIDAL GeodeticToNed, so the round trip carries an intrinsic horizontal
#: error that grows linearly with range, peaks along the north/south azimuths,
#: and worsens toward the equator. Measured worst-over-azimuth error per km
#: (test_horizontal_error_scales_with_range): 5.01 m/km at taiwan-strait
#: (24.15N, the lowest-latitude shipped theater), 4.70 at red-sea-hormuz, 3.36
#: at iran-isfahan, 1.86 at the Redmond default. taiwan-strait therefore hits
#: the 5 m gate at ~1.0 km, NOT at the 2 km the previous gate implied -- that
#: file only ever ran at 47.6N, where 2 km costs just 3.7 m. 900 m keeps every
#: theater inside the gate with ~10% margin.
CERTIFIED_RADIUS_M = 900.0

# Top of the 46000-46099 band reserved for this module. tests/test_realism.py
# allocates upward from 46000 with itertools.count, so take the far end to stay
# clear of it; tests/test_fake_airsim.py owns 46100+.
SIM_PORT = 46097

# Theater origins as an operator enters them: lat, lon, altitude MSL
# (launch.py THEATERS). EGM96 undulation across this set spans -33.2 m
# (indo-pak-loc) to +14.5 m (taiwan-strait), so a datum defect cannot hide
# behind a single sign across the theaters.
THEATERS = {
    "indo-pak-loc": (34.08, 74.79, 1600.0),
    "iran-isfahan": (33.72, 51.72, 1550.0),
    "taiwan-strait": (24.15, 119.30, 0.0),
    "ukraine-donbas": (48.60, 37.95, 200.0),
    "red-sea-hormuz": (26.55, 56.25, 0.0),
    "default": (47.641468, -122.140165, 122.0),
}

# Marker offsets from the origin, metres (north, east, height above origin).
# Spans 0.1 km to the certified radius and includes the pure north/south
# azimuths, which are the worst case for the spherical/ellipsoidal mismatch.
MISSION_OFFSETS = [
    (100, 0, 0),
    (0, 100, 10),
    (500, 500, 50),
    (-636, 636, 80),
    (900, 0, 150),
    (-900, 0, 60),
]


# ---------------------------------------------------------------------------
# helpers: the exact conversion path the product uses
# ---------------------------------------------------------------------------


def _horiz_m(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    lat = math.radians((lat_a + lat_b) / 2)
    dx = math.radians(lon_b - lon_a) * EARTH_RADIUS * math.cos(lat)
    dy = math.radians(lat_b - lat_a) * EARTH_RADIUS
    return math.hypot(dx, dy)


def _home_hae(theater: str) -> HomeGeoPoint:
    """Theater origin declared MSL -> HAE once, via the canonical point (T1)."""
    lat, lon, alt_msl = THEATERS[theater]
    fix = canonical_altitude(alt_msl, lat, lon, datum="msl")
    return HomeGeoPoint.from_geo(GeoPoint(lat, lon, fix.alt_hae))


def _truth_marker(home: HomeGeoPoint, dn: float, de: float, dh: float):
    """A known ground-truth marker, stated the way an operator states one: a
    geodetic position with an MSL altitude."""
    gp_hae = ned_to_geodetic(NedPoint(dn, de, -dh), home)
    fix = canonical_altitude(
        gp_hae.altitude, gp_hae.latitude, gp_hae.longitude, datum="hae"
    )
    return gp_hae.latitude, gp_hae.longitude, fix.alt_msl


def _msl_to_ned(lat: float, lon: float, alt_msl: float, home: HomeGeoPoint,
                *, undulation=None) -> NedPoint:
    """Operator-facing geodetic+MSL -> NED: the server.py llh_to_ned path with
    the T1 datum leg in front of it. `undulation` overrides the geoid purely so
    `test_gate_detects_datum_error` can inject a broken datum."""
    if undulation is None:
        alt_hae = canonical_altitude(alt_msl, lat, lon, datum="msl").alt_hae
    else:
        alt_hae = alt_msl + undulation(lat, lon)
    return geodetic_to_ned(GeoPoint(lat, lon, alt_hae), home.geo)


def _hae_to_msl_point(lat: float, lon: float, alt_hae: float, *, undulation=None):
    """Published geodetic+HAE -> operator MSL: the readback datum leg (T1)."""
    if undulation is None:
        return canonical_altitude(alt_hae, lat, lon, datum="hae").alt_msl
    return alt_hae - undulation(lat, lon)


def _ned_to_msl(ned: NedPoint, home: HomeGeoPoint, *, undulation=None):
    """NED -> geodetic + MSL: the bridge.py snapshot path with the datum leg."""
    gp = ned_to_geodetic(ned, home)
    return (
        gp.latitude,
        gp.longitude,
        _hae_to_msl_point(gp.latitude, gp.longitude, gp.altitude,
                          undulation=undulation),
    )


# ---------------------------------------------------------------------------
# Gate 1: full conversion path, every theater, out to the certified radius
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("theater", sorted(THEATERS))
@pytest.mark.parametrize("dn,de,dh", MISSION_OFFSETS)
def test_registration_roundtrip_within_gate(theater, dn, de, dh):
    """T7: a marker at a known geodetic/MSL point survives the round trip."""
    home = _home_hae(theater)
    lat, lon, alt_msl = _truth_marker(home, dn, de, dh)

    ned = _msl_to_ned(lat, lon, alt_msl, home)
    lat2, lon2, alt_msl2 = _ned_to_msl(ned, home)

    h = _horiz_m(lat, lon, lat2, lon2)
    v = abs(alt_msl2 - alt_msl)
    assert h <= GATE_HORIZ_M, f"{theater}: horizontal {h:.3f} m > {GATE_HORIZ_M} m"
    assert v <= GATE_VERT_M, f"{theater}: vertical {v:.3f} m > {GATE_VERT_M} m"


def test_gate_summary():
    """Worst registration error across every theater and offset."""
    worst_h = worst_v = 0.0
    worst_at = ""
    for theater in sorted(THEATERS):
        home = _home_hae(theater)
        for dn, de, dh in MISSION_OFFSETS:
            lat, lon, alt_msl = _truth_marker(home, dn, de, dh)
            lat2, lon2, alt2 = _ned_to_msl(_msl_to_ned(lat, lon, alt_msl, home), home)
            h = _horiz_m(lat, lon, lat2, lon2)
            v = abs(alt2 - alt_msl)
            if h > worst_h or v > worst_v:
                worst_at = f"{theater} {(dn, de, dh)}"
            worst_h, worst_v = max(worst_h, h), max(worst_v, v)
    print(f"\nGATE worst: horiz={worst_h:.3f} m  vert={worst_v:.3f} m  at {worst_at}")
    assert worst_h <= GATE_HORIZ_M and worst_v <= GATE_VERT_M


def test_horizontal_error_scales_with_range():
    """Record the envelope behind CERTIFIED_RADIUS_M so the operating radius is
    a measured claim, not a guess. Error is ~linear in range, worst along the
    north/south azimuths and at the lowest latitude, so taiwan-strait sets it.
    """
    rates = {}
    for theater in sorted(THEATERS):
        home = _home_hae(theater)
        worst = 0.0
        for az_deg in range(0, 360, 15):
            az = math.radians(az_deg)
            gp = ned_to_geodetic(
                NedPoint(1000.0 * math.cos(az), 1000.0 * math.sin(az), -50.0), home
            )
            gp2 = ned_to_geodetic(geodetic_to_ned(gp, home.geo), home)
            worst = max(
                worst,
                _horiz_m(gp.latitude, gp.longitude, gp2.latitude, gp2.longitude),
            )
        rates[theater] = worst
    print("\nworst horizontal error per km: " + ", ".join(
        f"{k}={v:.2f}" for k, v in sorted(rates.items(), key=lambda kv: -kv[1])))

    worst_rate = max(rates.values())
    assert rates["taiwan-strait"] == worst_rate, "lowest-latitude theater is worst"
    # The certified radius must sit inside the 5 m gate for the worst theater...
    assert worst_rate * (CERTIFIED_RADIUS_M / 1000.0) <= GATE_HORIZ_M
    # ...and every offset the gate exercises must sit inside that radius.
    assert max(math.hypot(dn, de) for dn, de, _ in MISSION_OFFSETS) <= (
        CERTIFIED_RADIUS_M + 1e-6
    ), "gate offsets must stay inside the certified radius"
    # Documented finding: 2 km (the radius the old gate implied) does NOT hold.
    assert worst_rate * 2.0 > GATE_HORIZ_M


def test_datum_leg_is_actually_exercised():
    """The gate is worthless if the datum leg is a no-op. The undulation is
    +1.58 m at Isfahan and -33.19 m at indo-pak-loc, so HAE and MSL must differ
    by those amounts, with opposite signs, inside the loop."""
    for theater, expected_n in (("iran-isfahan", 1.58), ("indo-pak-loc", -33.19)):
        lat, lon, alt_msl = THEATERS[theater]
        fix = canonical_altitude(alt_msl, lat, lon, datum="msl")
        assert abs(fix.undulation_m - expected_n) < 1.0, (
            f"{theater}: N={fix.undulation_m:.3f} m, expected ~{expected_n} m"
        )
        assert fix.alt_hae - fix.alt_msl == pytest.approx(fix.undulation_m, abs=1e-9)
        assert fix.degraded is False


# ---------------------------------------------------------------------------
# Gate 2: the gate must FAIL on a broken datum (regression guard)
# ---------------------------------------------------------------------------


def test_gate_detects_datum_error():
    """Inject the two historical datum defects and prove the gate catches them.

    Both are applied to the OUTBOUND leg only, which is what a per-call-site
    conversion mismatch looks like in the field: the MCP server converting one
    way and the bridge another (the measured 17.4 m divergence, T1).
    """
    # (a) the longitude-ignoring approximation standing in for the real geoid.
    home = _home_hae("iran-isfahan")
    lat, lon, alt_msl = _truth_marker(home, 500, 500, 50)
    ned = _msl_to_ned(lat, lon, alt_msl, home, undulation=geo._coarse_undulation)
    _, _, alt_bad = _ned_to_msl(ned, home)
    assert abs(alt_bad - alt_msl) > GATE_VERT_M, (
        "gate is blind to the coarse-approximation datum defect"
    )

    # (b) the sign inversion the EGM96 branch used to carry (`return -N`).
    def flipped(la, lo):
        return -geo.geoid_undulation(la, lo)

    home_pk = _home_hae("indo-pak-loc")
    lat_pk, lon_pk, msl_pk = _truth_marker(home_pk, 500, 500, 50)
    ned_pk = _msl_to_ned(lat_pk, lon_pk, msl_pk, home_pk, undulation=flipped)
    _, _, alt_flip = _ned_to_msl(ned_pk, home_pk)
    assert abs(alt_flip - msl_pk) > GATE_VERT_M, (
        "gate is blind to the EGM96 sign inversion"
    )


@pytest.mark.parametrize("broken", ["flip", "zero", "coarse"])
def test_registration_roundtrip_is_blind_to_a_consistent_datum_error(broken):
    """The honest limit of this gate (T1/T7), asserted rather than assumed.

    `test_gate_detects_datum_error` injects on the OUTBOUND leg only. Applied
    CONSISTENTLY -- which is what the historical defect actually was, one
    process with one silent fallback and every caller equally wrong -- the
    outbound `+N` and inbound `-N` cancel and the round trip cannot see it.
    Measured: the gate's worst real vertical residual is 0.063 m against a
    10 m tolerance, so there is no headroom for a consistent error to consume.

    This test exists so nobody reads a green gate as "the datum is correct".
    The datum's VALUE is guarded by `test_datum_leg_is_actually_exercised` and
    the EGM96 golden vectors, and by nothing here.
    """
    if broken == "flip":
        bad = lambda la, lo: -geo.geoid_undulation(la, lo)  # noqa: E731
    elif broken == "zero":
        bad = lambda la, lo: 0.0  # noqa: E731
    else:
        bad = geo._coarse_undulation

    worst_v = 0.0
    for theater in sorted(THEATERS):
        home = _home_hae(theater)
        for dn, de, dh in MISSION_OFFSETS:
            lat, lon, alt_msl = _truth_marker(home, dn, de, dh)
            ned = _msl_to_ned(lat, lon, alt_msl, home, undulation=bad)
            _, _, alt2 = _ned_to_msl(ned, home, undulation=bad)
            worst_v = max(worst_v, abs(alt2 - alt_msl))

    assert worst_v <= GATE_VERT_M, (
        f"{broken}: the round trip unexpectedly caught a consistent datum "
        f"error ({worst_v:.3f} m) -- if this ever fires, the docstring above "
        f"and CERTIFIED_RADIUS_M's rationale need revisiting"
    )
    # ...and the value assertion that DOES catch it must still be doing so.
    lat, lon, _ = THEATERS["iran-isfahan"]
    assert abs(bad(lat, lon) - geo.geoid_undulation(lat, lon)) > 1.0


# ---------------------------------------------------------------------------
# Gate 3: the same round trip across the real msgpack-rpc sim boundary
# ---------------------------------------------------------------------------

# Isfahan: a 1550 m MSL origin whose undulation (+1.58 m) has the OPPOSITE sign
# to the old fallback's (-17.4 m).
#
# NOTE, measured: choosing this origin does NOT make the sim leg able to see a
# datum regression. A consistently applied wrong datum cancels across the sim
# round trip exactly as it does offline -- a globally sign-flipped EGM96 leaves
# every test below green. The sim leg measures REGISTRATION across the wire;
# the datum's value is guarded by `test_sim_home_geo_point_carries_the_declared
# _datum` and by the golden vectors, not by the round trip.
SIM_THEATER = "iran-isfahan"

#: Certified radius for the sim leg, metres -- half the offline radius, for two
#: measured reasons:
#:   1. simGetObjectPose resolves a stored marker back through geodetic_to_ned,
#:      so that readback crosses the spherical/ellipsoidal mismatch TWICE and
#:      costs ~2x the offline error (6.04 m at 900 m, vs 3.02 m one way).
#:      simGetDetections, the path the product actually consumes (PLAN.md:62),
#:      pays it once.
#:   2. simGetDetections is range-limited by the sensor model (500 m by
#:      default), and this gate must measure registration, not sensor range.
SIM_RADIUS_M = 450.0

SIM_OFFSETS = [
    (100, 0, 0),
    (0, 100, 10),
    (300, 300, 50),
    (-450, 0, 80),
    (0, -450, 25),
]


@pytest.fixture(scope="module")
def sim_home():
    home = _home_hae(SIM_THEATER)
    s = FakeAirSim(home=home.geo, port=SIM_PORT)
    s.start()
    time.sleep(0.3)
    try:
        yield s, home
    finally:
        s.stop()


@pytest.fixture(scope="module")
def sim_client(sim_home):
    c = airsim.MultirotorClient(ip="127.0.0.1", port=SIM_PORT)
    c.confirmConnection()
    return c


def test_sim_home_geo_point_carries_the_declared_datum(sim_client, sim_home):
    """The origin the sim publishes must be the HAE the datum leg produced --
    not the MSL number the operator typed."""
    _, home = sim_home
    hp = sim_client.getHomeGeoPoint()
    assert abs(hp.latitude - home.geo.latitude) < 1e-6
    assert abs(hp.longitude - home.geo.longitude) < 1e-6
    assert abs(hp.altitude - home.geo.altitude) < 1e-6

    lat, lon, alt_msl = THEATERS[SIM_THEATER]
    assert abs(hp.altitude - canonical_altitude(alt_msl, lat, lon).alt_hae) < 1e-9
    # ...and the two datums must genuinely differ (N = +1.58 m at Isfahan).
    assert abs(hp.altitude - alt_msl) > 1.0
    assert abs(
        _hae_to_msl_point(hp.latitude, hp.longitude, hp.altitude) - alt_msl
    ) < 1e-9


def _spawn_marker(sim_client, name: str, ned: NedPoint) -> None:
    sim_client.simSpawnObject(
        name, "Cylinder",
        airsim.Pose(airsim.Vector3r(ned.x, ned.y, ned.z)),
        airsim.Vector3r(1.0, 1.0, 1.0),
    )


@pytest.mark.parametrize("dn,de,dh", SIM_OFFSETS)
def test_detection_geo_point_registers_within_gate(sim_client, sim_home, dn, de, dh):
    """T7 end to end on the path the product actually consumes: PLAN.md:62
    takes simGetDetections()[].geo_point as target ground truth.

    Marker out through the product's conversion path and the msgpack-rpc wire,
    registered geodetically inside the sim, read back as a geo_point, closed
    through the T1 datum leg.
    """
    _, home = sim_home
    lat, lon, alt_msl = _truth_marker(home, dn, de, dh)
    ned = _msl_to_ned(lat, lon, alt_msl, home)

    name = f"gate_detect_{dn}_{de}_{dh}"
    _spawn_marker(sim_client, name, ned)
    try:
        hits = {d.name: d for d in
                sim_client.simGetDetections("0", airsim.ImageType.Scene)}
        assert name in hits, (
            f"marker at {math.hypot(dn, de):.0f} m not returned by "
            f"simGetDetections; sensor range is "
            f"{sim_home[0].sensor_range_m('Drone1', '0', 0):.0f} m"
        )
        gp = hits[name].geo_point  # altitude is HAE by contract
        alt_msl2 = _hae_to_msl_point(gp.latitude, gp.longitude, gp.altitude)

        h = _horiz_m(lat, lon, gp.latitude, gp.longitude)
        v = abs(alt_msl2 - alt_msl)
        assert h <= GATE_HORIZ_M, f"horizontal {h:.3f} m > {GATE_HORIZ_M} m"
        assert v <= GATE_VERT_M, f"vertical {v:.3f} m > {GATE_VERT_M} m"
    finally:
        sim_client.simDestroyObject(name)


@pytest.mark.parametrize("dn,de,dh", SIM_OFFSETS)
def test_marker_roundtrip_through_sim_within_gate(sim_client, sim_home, dn, de, dh):
    """T7 with the full NED round trip: spawn, then resolve the marker back to
    NED with simGetObjectPose. This crosses the registration path twice (see
    SIM_RADIUS_M), so it is the stricter of the two sim legs."""
    _, home = sim_home
    lat, lon, alt_msl = _truth_marker(home, dn, de, dh)
    ned = _msl_to_ned(lat, lon, alt_msl, home)

    name = f"gate_marker_{dn}_{de}_{dh}"
    _spawn_marker(sim_client, name, ned)
    try:
        pose = sim_client.simGetObjectPose(name)
        assert math.isfinite(pose.position.x_val), f"{name} not registered by the sim"
        back = NedPoint(pose.position.x_val, pose.position.y_val, pose.position.z_val)
        lat2, lon2, alt_msl2 = _ned_to_msl(back, home)

        h = _horiz_m(lat, lon, lat2, lon2)
        v = abs(alt_msl2 - alt_msl)
        assert h <= GATE_HORIZ_M, f"horizontal {h:.3f} m > {GATE_HORIZ_M} m"
        assert v <= GATE_VERT_M, f"vertical {v:.3f} m > {GATE_VERT_M} m"
    finally:
        sim_client.simDestroyObject(name)


def test_sim_gate_summary(sim_client, sim_home):
    """Worst-case registration error over both sim readback paths."""
    _, home = sim_home
    worst = {"detect_h": 0.0, "detect_v": 0.0, "pose_h": 0.0, "pose_v": 0.0}
    for dn, de, dh in SIM_OFFSETS:
        lat, lon, alt_msl = _truth_marker(home, dn, de, dh)
        name = f"gate_sum_{dn}_{de}_{dh}"
        _spawn_marker(sim_client, name, _msl_to_ned(lat, lon, alt_msl, home))
        try:
            gp = {d.name: d for d in
                  sim_client.simGetDetections("0", airsim.ImageType.Scene)
                  }[name].geo_point
            alt2 = _hae_to_msl_point(gp.latitude, gp.longitude, gp.altitude)
            worst["detect_h"] = max(
                worst["detect_h"], _horiz_m(lat, lon, gp.latitude, gp.longitude))
            worst["detect_v"] = max(worst["detect_v"], abs(alt2 - alt_msl))

            p = sim_client.simGetObjectPose(name).position
            lat2, lon2, alt3 = _ned_to_msl(
                NedPoint(p.x_val, p.y_val, p.z_val), home)
            worst["pose_h"] = max(worst["pose_h"], _horiz_m(lat, lon, lat2, lon2))
            worst["pose_v"] = max(worst["pose_v"], abs(alt3 - alt_msl))
        finally:
            sim_client.simDestroyObject(name)
    print(
        f"\nSIM GATE worst: detections horiz={worst['detect_h']:.3f} m "
        f"vert={worst['detect_v']:.3f} m | objectpose horiz={worst['pose_h']:.3f} m "
        f"vert={worst['pose_v']:.3f} m"
    )
    assert max(worst["detect_h"], worst["pose_h"]) <= GATE_HORIZ_M
    assert max(worst["detect_v"], worst["pose_v"]) <= GATE_VERT_M
    # The doubled readback must cost more than the one-way product path, or the
    # SIM_RADIUS_M rationale above is wrong.
    assert worst["pose_h"] > worst["detect_h"]
