"""Simulated wargame vectors (WG v2 §5.2.8, B2): `wargame_vectors.py`.

The B2 acceptance list: horizon and visibility against a ridge fixture;
exposure 0 outside range and above the ceiling; the planner avoids a
high-hazard disc and respects the fence, with at most 40 waypoints in under
1.5 s on a 48 x 48 grid with 40 threats; the `red_axes` math. Also: lattice
sizing and coverage, leg buckets (§3.9), refusal codes, determinism, and no
real-system token in any output (M14a, D1).

Pure and offline: synthetic AOs, synthetic ground functions, B1's notional
classes. Positions are metres from an arbitrary synthetic origin.
"""
from __future__ import annotations

import math
import random
import time
from itertools import pairwise
from types import SimpleNamespace

import pytest
from godseye_uav import safety, theaters, wargame_adjudicate, wargame_tables
from godseye_uav import wargame_vectors as wv
from support.wg_tokens import assert_no_real_system_tokens

LAT0, LON0 = 47.6415, -122.1400
KY = math.pi / 180.0 * wv.EARTH_RADIUS_M
KX = KY * math.cos(math.radians(LAT0))
C = wargame_tables.CLASSES
AD_GUN, AD_SHORT, AD_MEDIUM = C["ad_gun"], C["ad_short"], C["ad_medium"]
SPEED, ALT = 10.0, 60.0


def at(north_m: float, east_m: float) -> tuple[float, float]:
    """A point `north_m`, `east_m` from the synthetic origin (lattice metres)."""
    return (LAT0 + north_m / KY, LON0 + east_m / KX)


def box(half_m: float, *, home_msl: float = 100.0, **extra):
    """A square AO mapping (bbox [s, w, n, e]) and its geofence polygon."""
    s, w = at(-half_m, -half_m)
    n, e = at(half_m, half_m)
    ao = {"bbox": [s, w, n, e], "home_msl_m": home_msl, **extra}
    return ao, [(s, w), (s, e), (n, e), (n, w)]


def flat(height: float = 100.0):
    return lambda lat, lon: height


def ridge(east_m: float = 1500.0, *, half_width: float = 200.0, height: float = 300.0,
          base: float = 100.0):
    """A north-south ridge `height` metres high, centred `east_m` east of the origin."""
    def ground(lat: float, lon: float) -> float:
        x = (lon - LON0) * KX
        return base + height if abs(x - east_m) <= half_width else base
    return ground


def horizontal_m(lattice: wv.Lattice, a, b) -> float:
    ax, ay = lattice.xy(*a)
    bx, by = lattice.xy(*b)
    return math.hypot(ax - bx, ay - by)


def threat(cls, north_m: float = 0.0, east_m: float = 0.0, *, alt_hae: float = 100.0,
           damaged: bool = False, horizon=None):
    return (*at(north_m, east_m), alt_hae, cls, damaged, horizon)


def samples(path, per_leg: int = 200):
    for a, b in pairwise(path):
        for k in range(per_leg + 1):
            t = k / per_leg
            yield (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


# ---------------------------------------------------------------- lattice ----
@pytest.mark.parametrize(("half_m", "cell_m", "cells"), [
    (25_000.0, 50_000.0 / 48, 48),   # group 3 cap: 48 cells of ~1042 m
    (2_940.0, 5_880.0 / 48, 48),     # quad AO
    (1_000.0, 100.0, 20),            # clamped up to 100 m
    (50_000.0, 100_000.0 / 48, 48),  # beyond the cap: the cell grows, not the grid
])
def test_lattice_cell_size_and_grid(half_m, cell_m, cells):
    ao, _ = box(half_m)
    lat = wv.build_lattice(ao, flat())
    assert lat.cell_m == pytest.approx(cell_m, rel=1e-6)
    assert (lat.nx, lat.ny) == (cells, cells)
    assert lat.cells == cells * cells


def test_lattice_coverage_and_home_fallback():
    ao, _ = box(5000.0, home_msl=321.0)
    real = wv.build_lattice(ao, flat(50.0))
    assert real.coverage_pct == 100.0 and real.terrain_source == wv.TERRAIN_REAL
    none = wv.build_lattice(ao, None)
    assert none.coverage_pct == 0.0 and none.terrain_source == wv.TERRAIN_FLAT
    assert set(none.ground_msl) == {321.0}
    half = wv.build_lattice(ao, lambda lat, lon: 50.0 if lon < LON0 else None)
    assert half.coverage_pct == 50.0
    assert half.ground_at(*at(0, -2000)) == 50.0 and half.ground_at(*at(0, 2000)) == 321.0
    nan = wv.build_lattice(ao, lambda lat, lon: math.nan)
    assert nan.covered == 0
    assert real.as_dict()["source"] == "real terrain" and none.as_dict()["coverage_pct"] == 0.0


def test_lattice_undulation_and_ground_hae():
    ao, _ = box(3000.0, home_msl=100.0, home_hae_m=80.0)
    lat = wv.build_lattice(ao, flat(100.0))
    assert lat.undulation_m == pytest.approx(-20.0)
    assert wv.ground_hae_m(lat, *at(0, 0)) == pytest.approx(80.0)
    explicit = wv.build_lattice({**ao, "undulation_m": 17.0}, flat(100.0))
    assert explicit.undulation_m == 17.0


def test_lattice_accepts_a_theater_and_rejects_a_bad_bbox():
    t = theaters.get("default")
    lat = wv.build_lattice(t, None)
    assert (lat.south, lat.west, lat.north, lat.east) == tuple(t.bbox())
    assert lat.home_msl_m == t.home_alt_msl_m
    with pytest.raises(ValueError):
        wv.build_lattice({"bbox": [1.0, 0.0, 0.0, 1.0], "home_msl_m": 0.0}, None)
    with pytest.raises(ValueError):
        wv.build_lattice({"bbox": [0.0, 0.0, 1.0, math.inf], "home_msl_m": 0.0}, None)


def test_cell_of_clamps_and_round_trips():
    ao, _ = box(5000.0)
    lat = wv.build_lattice(ao, None)
    c = lat.cell_of(*at(1234.0, -2345.0))
    assert horizontal_m(lat, lat.center(c), at(1234.0, -2345.0)) <= lat.cell_m * 0.71
    assert lat.cell_of(*at(99_999.0, 99_999.0)) == lat.cells - 1
    assert lat.cell_of(*at(-99_999.0, -99_999.0)) == 0


# -------------------------------------------------------------- visibility ----
@pytest.fixture
def ridge_lattice():
    ao, fence = box(5000.0)
    return wv.build_lattice(ao, ridge(1500.0)), fence


def test_ridge_masks_a_ground_target_behind_it(ridge_lattice):
    lat, _ = ridge_lattice
    h = wv.horizon(*at(0, 0), 100.0, lat, 8000.0)
    assert h.eye_msl_m == pytest.approx(100.0 + wv.EYE_AGL_M)  # lifted off the ground
    assert not wv.visible(h, *at(0, 3000), 102.0)       # behind the ridge, on the ground
    assert not wv.visible(h, *at(800, 3000), 160.0)     # a drone at 60 m behind it
    assert wv.visible(h, *at(0, 3000), 2000.0)          # high above the ridge line
    assert wv.visible(h, *at(0, 900), 101.0)            # short of the ridge
    assert wv.visible(h, *at(0, -3000), 101.0)          # the open side
    assert wv.visible(h, *at(3000, 0), 101.0)           # along the ridge's axis
    assert wv.visible(None, *at(0, 3000), 0.0)          # no horizon yet: clear (§5.2.4)
    assert wv.visible(h, *at(0, 0), -50.0)              # the eye's own point


def test_earth_curvature_hides_a_distant_ground_target():
    ao, _ = box(25_000.0)
    lat = wv.build_lattice(ao, flat(100.0))
    h = wv.horizon(*at(0, -20_000), 100.0, lat, 45_000.0)
    assert not wv.visible(h, *at(0, 20_000), 102.0)     # 40 km on flat ground: below the bulge
    assert wv.visible(h, *at(0, 20_000), 600.0)
    assert wv.visible(h, *at(0, -12_000), 102.0)        # 8 km: inside the radio horizon


def test_coverage_fan_stops_at_the_ridge(ridge_lattice):
    lat, _ = ridge_lattice
    eye = at(0, 0)
    h = wv.horizon(*eye, 100.0, lat, 4000.0)
    fan = wv.coverage_fan(h, 4000.0, 102.0, n=72)
    assert len(fan) == 72
    east, west = fan[18], fan[54]                       # 90 and 270 degrees
    assert horizontal_m(lat, eye, east) < 1500.0
    assert horizontal_m(lat, eye, west) == pytest.approx(4000.0, abs=1.0)
    high = wv.coverage_fan(h, 4000.0, 3000.0, n=72)
    assert horizontal_m(lat, eye, high[18]) == pytest.approx(4000.0, abs=1.0)
    ring = wv.coverage_fan(None, 1000.0, 0.0, n=12, center=eye)
    assert len(ring) == 12
    assert all(safety.haversine_m(*eye, *p) == pytest.approx(1000.0, abs=0.5) for p in ring)
    assert wv.coverage_fan(None, 1000.0, 0.0) == []


# ---------------------------------------------------------------- exposure ----
@pytest.fixture
def flat_lattice():
    ao, fence = box(5000.0)
    return wv.build_lattice(ao, flat(100.0)), fence


def expected_rate(cls, slant, dz, *, damaged=False, speed=SPEED):
    cycle = cls.cycle_s * (wargame_tables.DAMAGED_CYCLE_FACTOR if damaged else 1.0)
    return (wargame_adjudicate.p_detect(cls, slant, {})
            * wargame_adjudicate.p_kill_air(cls, slant, dz, speed, damaged) / cycle)


def test_exposure_is_zero_outside_range(flat_lattice):
    lat, _ = flat_lattice
    src = at(0, 0)
    exp = wv.exposure_grid(lat, [threat(AD_GUN)], ALT, SPEED)
    assert exp.threats == 1 and exp.threats_dropped == 0
    inside = 0
    for c in range(lat.cells):
        h = horizontal_m(lat, lat.center(c), src)
        slant = math.hypot(h, ALT)
        if slant > AD_GUN.threat_range_m:
            assert exp.rate[c] == 0.0, (c, h)
        else:
            inside += 1
            assert exp.rate[c] == pytest.approx(expected_rate(AD_GUN, slant, ALT))
    assert inside > 100
    assert exp.rate_at(*src) > 0 and exp.rate_at(*at(0, 4000)) == 0.0


def test_exposure_is_zero_above_the_ceiling_and_below_the_unit(flat_lattice):
    lat, _ = flat_lattice
    ceiling = AD_GUN.threat_ceiling_m
    assert wv.exposure_grid(lat, [threat(AD_GUN)], ceiling + 1.0, SPEED).max_rate == 0.0
    at_ceiling = wv.exposure_grid(lat, [threat(AD_GUN)], ceiling, SPEED)
    assert at_ceiling.rate_at(*at(0, 0)) > 0                  # 0 <= dz <= ceiling is inclusive
    high_unit = threat(AD_GUN, alt_hae=100.0 + ALT + 50.0)    # the unit sits above the drone
    assert wv.exposure_grid(lat, [high_unit], ALT, SPEED).max_rate == 0.0


def test_exposure_uses_hae_for_dz():
    ao, _ = box(5000.0, home_msl=100.0, undulation_m=-30.0)
    lat = wv.build_lattice(ao, flat(100.0))
    unit_hae = wv.ground_hae_m(lat, *at(0, 0))               # 70 m HAE on 100 m MSL ground
    exp = wv.exposure_grid(lat, [threat(AD_GUN, alt_hae=unit_hae)], ALT, SPEED)
    c = lat.cell_of(*at(0, 0))
    slant = math.hypot(horizontal_m(lat, lat.center(c), at(0, 0)), ALT)
    assert exp.rate[c] == pytest.approx(expected_rate(AD_GUN, slant, ALT))
    wrong_datum = wv.exposure_grid(lat, [threat(AD_GUN, alt_hae=100.0)], ALT, SPEED)
    assert wrong_datum.rate[c] != pytest.approx(exp.rate[c])


def test_exposure_respects_terrain_masking(ridge_lattice):
    lat, _ = ridge_lattice
    h = wv.horizon(*at(0, 0), 100.0, lat, AD_SHORT.detection_range_m)
    masked = wv.exposure_grid(lat, [threat(AD_SHORT, horizon=h)], ALT, SPEED)
    clear = wv.exposure_grid(lat, [threat(AD_SHORT)], ALT, SPEED)
    behind, open_side = at(0, 3000), at(0, -3000)
    assert masked.rate_at(*behind) == 0.0 and clear.rate_at(*behind) > 0
    assert masked.rate_at(*open_side) == pytest.approx(clear.rate_at(*open_side))
    assert masked.rate_at(*open_side) > 0


def test_damaged_threat_shrinks_range_and_rate(flat_lattice):
    lat, _ = flat_lattice
    ok = wv.exposure_grid(lat, [threat(AD_GUN)], ALT, SPEED)
    hurt = wv.exposure_grid(lat, [threat(AD_GUN, damaged=True)], ALT, SPEED)
    r_damaged = AD_GUN.threat_range_m * wargame_tables.DAMAGED_RANGE_FACTOR
    for c in range(lat.cells):
        slant = math.hypot(horizontal_m(lat, lat.center(c), at(0, 0)), ALT)
        if r_damaged < slant <= AD_GUN.threat_range_m:
            assert hurt.rate[c] == 0.0 and ok.rate[c] > 0
        elif slant <= r_damaged:
            assert hurt.rate[c] == pytest.approx(expected_rate(AD_GUN, slant, ALT, damaged=True))
            assert hurt.rate[c] < ok.rate[c]


def test_exposure_ignores_classes_without_an_air_envelope(flat_lattice):
    lat, _ = flat_lattice
    rows = [threat(C[k]) for k in ("radar_early_warning", "armour_company", "supply_depot")]
    assert wv.exposure_grid(lat, rows, ALT, SPEED).max_rate == 0.0


def test_exposure_caps_the_threat_basis(flat_lattice):
    lat, _ = flat_lattice
    exp = wv.exposure_grid(lat, [threat(AD_GUN, 100.0 * i) for i in range(45)], ALT, SPEED)
    assert (exp.threats, exp.threats_dropped) == (wv.MAX_THREATS, 5)


def test_kernel_can_be_injected_and_defaults_to_b1(flat_lattice):
    lat, _ = flat_lattice
    kernel = SimpleNamespace(p_detect=lambda cls, slant, env: 1.0,
                             p_kill_air=lambda cls, slant, dz, speed, damaged: 0.5,
                             DAMAGED_CYCLE_FACTOR=2.0, DAMAGED_RANGE_FACTOR=0.8)
    exp = wv.exposure_grid(lat, [threat(AD_GUN)], ALT, SPEED, kernel=kernel)
    assert exp.rate_at(*at(0, 0)) == pytest.approx(0.5 / AD_GUN.cycle_s)
    default = wv._default_kernel()
    assert default.p_detect is wargame_adjudicate.p_detect
    assert default.p_kill_air is wargame_adjudicate.p_kill_air


def test_optical_threats_read_the_environment(flat_lattice):
    lat, _ = flat_lattice
    clear = wv.exposure_grid(lat, [threat(AD_GUN)], ALT, SPEED)
    murky = wv.exposure_grid(lat, [threat(AD_GUN)], ALT, SPEED, env={"visibility_factor": 0.5})
    assert murky.rate_at(*at(0, 0)) == pytest.approx(clear.rate_at(*at(0, 0)) * 0.5)


# --------------------------------------------------------------- summarize ----
def uniform(lattice, rate):
    return wv.Exposure(lattice=lattice, rate=(rate,) * lattice.cells, alt_agl_m=ALT,
                       speed_mps=SPEED, threats=0)


@pytest.mark.parametrize(("rate", "bucket"), [(0.0, "low"), (1e-4, "moderate"), (1e-2, "high")])
def test_summarize_buckets_legs(flat_lattice, rate, bucket):
    lat, _ = flat_lattice
    a = at(0, -1000)
    b = wv.destination(*a, 1000.0, 90.0)
    s = wv.summarize([a, {"lat": b[0], "lon": b[1]}], uniform(lat, rate), SPEED)
    assert s["length_m"] == pytest.approx(1000.0, abs=0.1)
    assert s["eta_s"] == pytest.approx(100.0, abs=0.01)
    assert s["exposure_s"] == (0.0 if rate == 0 else pytest.approx(100.0, abs=0.5))
    assert s["p_survive"] == pytest.approx(math.exp(-rate * 100.0), abs=2e-3)
    assert [leg["exposure"] for leg in s["legs"]] == [bucket]
    assert s["simulated"] is True


def test_summarize_totals_are_the_sum_of_legs(flat_lattice):
    lat, _ = flat_lattice
    exp = wv.exposure_grid(lat, [threat(AD_GUN)], ALT, SPEED)
    path = [at(0, -4000), at(0, 0), at(3000, 3000), at(4000, 4000)]
    s = wv.summarize(path, exp, SPEED)
    assert len(s["legs"]) == 3
    assert s["length_m"] == pytest.approx(sum(leg["length_m"] for leg in s["legs"]), abs=0.2)
    assert s["exposure_s"] == pytest.approx(sum(leg["exposure_s"] for leg in s["legs"]), abs=0.2)
    assert s["p_survive"] == pytest.approx(math.exp(-s["hazard"]), abs=1e-4)
    assert s["legs"][0]["exposure"] == "high" and s["legs"][2]["exposure"] == "low"
    with pytest.raises(ValueError):
        wv.summarize(path, exp, 0.0)


# ----------------------------------------------------------------- planner ----
@pytest.fixture
def disc(flat_lattice):
    """An air-defence gun at the AO centre and a start and goal either side of it."""
    lat, fence = flat_lattice
    exp = wv.exposure_grid(lat, [threat(AD_GUN)], ALT, SPEED)
    return lat, fence, exp, at(0, -4000), at(0, 4000)


def plan(lat, exp, start, goal, fence, **kw):
    return wv.plan(lat, exp, start, goal, fence, alt_agl_m=ALT, speed_mps=SPEED, **kw)


def test_planner_avoids_a_high_hazard_disc(disc):
    lat, fence, exp, start, goal = disc
    t0 = time.perf_counter()
    route = plan(lat, exp, start, goal, fence)
    assert time.perf_counter() - t0 < 1.5
    assert route.path[0] == start and route.path[-1] == goal
    assert 2 < len(route.path) <= wv.MAX_WAYPOINTS and route.decimated == 0
    planned = wv.summarize(route.path, exp, SPEED)
    straight = wv.summarize([start, goal], exp, SPEED)
    assert straight["exposure_s"] > 300 and straight["p_survive"] < 0.01
    assert planned["exposure_s"] == 0.0 and planned["p_survive"] == 1.0
    assert all(leg["exposure"] == "low" for leg in planned["legs"])
    envelope = math.sqrt(AD_GUN.threat_range_m ** 2 - ALT ** 2)
    assert min(horizontal_m(lat, p, at(0, 0)) for p in samples(route.path)) > envelope - lat.cell_m
    assert all(safety.point_in_polygon(*p, fence) for p in samples(route.path))
    assert route.waypoints()[0] == {"lat": round(start[0], 7), "lon": round(start[1], 7)}


def test_planner_goes_straight_with_no_threats(flat_lattice):
    lat, fence = flat_lattice
    exp = wv.exposure_grid(lat, [], ALT, SPEED)
    route = plan(lat, exp, at(-4000, -4000), at(4000, 4000), fence)
    assert route.path == (at(-4000, -4000), at(4000, 4000))
    same = plan(lat, exp, at(10, 10), at(10, 10), fence)
    assert len(same.path) == 2


def u_fence():
    """The 10 km AO box with a 3 km notch cut from its north edge to 2.5 km south."""
    return [at(n, e) for n, e in [(-5000, -5000), (-5000, 5000), (5000, 5000), (5000, 1500),
                                  (-2500, 1500), (-2500, -1500), (5000, -1500), (5000, -5000)]]


def test_planner_respects_a_concave_fence(flat_lattice):
    lat, _ = flat_lattice
    fence = u_fence()
    exp = wv.exposure_grid(lat, [], ALT, SPEED)
    start, goal = at(4000, -3500), at(4000, 3500)
    route = plan(lat, exp, start, goal, fence)
    pts = list(samples(route.path, 400))
    assert all(safety.point_in_polygon(*p, fence) for p in pts)
    assert min(safety.distance_to_polygon_edge_m(*p, fence) for p in pts) \
        >= wv.FENCE_BUFFER_M - 5.0
    assert min(p[0] for p in route.path) < at(-2500, 0)[0]   # it went round the notch
    assert len(route.path) <= wv.MAX_WAYPOINTS


@pytest.mark.parametrize(("neck_m", "routable"), [(50.0, False), (600.0, True)])
def test_the_fence_is_a_hard_mask(flat_lattice, neck_m, routable):
    lat, _ = flat_lattice
    h = neck_m / 2
    fence = [at(n, e) for n, e in [(-5000, -5000), (-5000, -500), (-h, -500), (-h, 500),
                                   (-5000, 500), (-5000, 5000), (5000, 5000), (5000, 500),
                                   (h, 500), (h, -500), (5000, -500), (5000, -5000)]]
    exp = wv.exposure_grid(lat, [], ALT, SPEED)
    if routable:
        route = plan(lat, exp, at(0, -3000), at(0, 3000), fence)
        assert all(safety.point_in_polygon(*p, fence) for p in samples(route.path, 400))
    else:
        with pytest.raises(wv.NoRoute) as err:
            plan(lat, exp, at(0, -3000), at(0, 3000), fence)
        assert err.value.code == "no_route"


def test_planner_refusals(disc):
    lat, fence, exp, start, goal = disc
    outside = at(0, 9000)
    for s, g, code in ((outside, goal, "start_outside_fence"),
                       (start, outside, "target_outside_fence")):
        with pytest.raises(wv.NoRoute) as err:
            plan(lat, exp, s, g, fence)
        assert err.value.as_result() == {"rejected": True, "error": code,
                                         "message": err.value.message}
    with pytest.raises(wv.NoRoute) as err:
        plan(lat, exp, start, goal, fence, max_expansions=3)
    assert err.value.code == "plan_budget"
    other = wv.build_lattice(box(5000.0)[0], None)
    with pytest.raises(ValueError):
        plan(other, exp, start, goal, fence)
    with pytest.raises(ValueError):
        wv.plan(lat, exp, start, goal, fence, alt_agl_m=ALT + 1, speed_mps=SPEED)
    with pytest.raises(ValueError):
        wv.plan(lat, exp, start, goal, fence, alt_agl_m=ALT, speed_mps=0.0)


def test_waypoint_cap_drops_the_cheapest_points(disc, monkeypatch):
    lat, fence, exp, start, goal = disc
    full = plan(lat, exp, start, goal, fence)
    monkeypatch.setattr(wv, "MAX_WAYPOINTS", len(full.path) - 1)
    capped = plan(lat, exp, start, goal, fence)
    assert len(capped.path) == len(full.path) - 1 and capped.decimated == 1
    assert capped.path[0] == start and capped.path[-1] == goal
    straight = wv.summarize([start, goal], exp, SPEED)["hazard"]
    assert wv.summarize(capped.path, exp, SPEED)["hazard"] < straight
    monkeypatch.setattr(wv, "MAX_WAYPOINTS", 2)           # the notch needs a corner point
    with pytest.raises(wv.NoRoute) as err:
        plan(lat, wv.exposure_grid(lat, [], ALT, SPEED), at(4000, -3500), at(4000, 3500),
             u_fence())
    assert err.value.code == "route_too_complex"


def test_planner_budget_on_48x48_with_40_threats():
    ao, fence = box(25_000.0, home_msl=100.0)
    rng = random.Random(4417)
    t0 = time.perf_counter()
    lat = wv.build_lattice(
        ao, lambda la, lo: 150.0 + 120.0 * math.sin((la - LAT0) * 90) * math.cos((lo - LON0) * 70))
    assert (lat.nx, lat.ny) == (48, 48)
    rows = []
    for k in range(40):
        p = at(rng.uniform(-20_000, 20_000), rng.uniform(-20_000, 20_000))
        cls = (AD_MEDIUM, AD_SHORT, AD_GUN)[k % 3]
        h = wv.horizon(*p, lat.ground_at(*p), lat, cls.detection_range_m)
        rows.append((*p, wv.ground_hae_m(lat, *p), cls, k % 7 == 0, h))
    exp = wv.exposure_grid(lat, rows, ALT, SPEED)
    start, goal = at(-23_000, -23_000), at(23_000, 23_000)
    t1 = time.perf_counter()
    route = plan(lat, exp, start, goal, fence)
    t2 = time.perf_counter()
    planned = wv.summarize(route.path, exp, SPEED)
    straight = wv.summarize([start, goal], exp, SPEED)
    assert t2 - t1 < 1.5, f"plan took {t2 - t1:.2f} s"
    assert time.perf_counter() - t0 < 1.5 * 3, "lattice, 40 horizons, exposure and plan"
    assert len(route.path) <= wv.MAX_WAYPOINTS
    assert planned["hazard"] <= straight["hazard"]
    assert plan(lat, exp, start, goal, fence) == route          # deterministic


# -------------------------------------------------------------------- axes ----
def unit(unit_id, side, wg_class, north_m, east_m, *, objective=None, state="active"):
    lat, lon = at(north_m, east_m)
    return SimpleNamespace(unit_id=unit_id, side=side, wg_class=wg_class, lat=lat, lon=lon,
                           objective=objective, state=state)


def test_red_axes_math():
    depot = unit("blue-depot-1", "blue", "blue_defended_point", 3000, 3000)
    armour = unit("red-armour-1", "red", "armour_company", 0, 0, objective="blue-depot-1")
    [row] = wv.red_axes([armour], {"blue-depot-1": depot})
    length = safety.haversine_m(armour.lat, armour.lon, depot.lat, depot.lon)
    assert length == pytest.approx(math.hypot(3000, 3000), rel=1e-3)
    assert row == {
        "id": "axis-red-armour-1", "kind": "axis", "unit_id": "red-armour-1",
        "objective_id": "blue-depot-1",
        "from": [round(armour.lat, 7), round(armour.lon, 7)],
        "to": [round(depot.lat, 7), round(depot.lon, 7)],
        "bearing_deg": pytest.approx(45.0, abs=0.1), "length_m": pytest.approx(length, abs=0.1),
        "speed_mps": C["armour_company"].speed_mps,
        "eta_s": pytest.approx(length / C["armour_company"].speed_mps, abs=0.1),
        "alt_band": "surface", "simulated": True}


def test_red_axes_only_join_red_movers_to_blue_units():
    depot = unit("blue-depot-1", "blue", "blue_defended_point", 3000, 0)
    lost = unit("blue-depot-2", "blue", "blue_defended_point", 0, 3000, state="destroyed")
    red_cp = unit("red-cp-1", "red", "command_post", 0, -3000)
    site = {"lat": at(0, 4000)[0], "lon": at(0, 4000)[1], "name": "a mapped place"}
    objectives = {"blue-depot-1": depot, "blue-depot-2": lost, "red-cp-1": red_cp, "site": site}
    units = {u.unit_id: u for u in [
        unit("red-infantry-1", "red", "mech_infantry", 0, 0, objective="blue-depot-1"),
        unit("red-armour-2", "red", "armour_company", 100, 0, objective="blue-depot-1"),
        unit("red-sam-1", "red", "ad_medium", 0, 500, objective="blue-depot-1"),      # static
        unit("red-armour-3", "red", "armour_company", 0, 900, objective="blue-depot-1",
             state="destroyed"),
        unit("blue-mech-1", "blue", "blue_mech", 0, -900, objective="blue-depot-1"),
        unit("red-armour-4", "red", "armour_company", 0, 1200, objective="blue-depot-2"),
        unit("red-armour-5", "red", "armour_company", 0, 1500, objective="red-cp-1"),
        unit("red-armour-6", "red", "armour_company", 0, 1800, objective="site"),     # D1
        unit("red-armour-7", "red", "armour_company", 0, 2100, objective="nowhere"),
        unit("red-armour-8", "red", "armour_company", 0, 2400),
    ]}
    rows = wv.red_axes(units, objectives)
    assert [r["unit_id"] for r in rows] == ["red-armour-2", "red-infantry-1"]
    assert rows[1]["speed_mps"] == C["mech_infantry"].speed_mps
    assert wv.red_axes(list(units.values()), objectives) == rows
    dicts = [dict(vars(u)) for u in units.values()]
    assert wv.red_axes(dicts, {"blue-depot-1": dict(vars(depot))}) == rows


# ---------------------------------------------------------- ring and tokens ----
@pytest.mark.parametrize("bearing", [0.0, 45.0, 137.5, 270.0])
def test_destination_round_trip(bearing):
    p = wv.destination(LAT0, LON0, 1234.5, bearing)
    assert safety.haversine_m(LAT0, LON0, *p) == pytest.approx(1234.5, abs=0.01)
    assert safety.bearing_deg(LAT0, LON0, *p) == pytest.approx(bearing, abs=0.01)


def test_ring_points_drop_outside_and_blocked(flat_lattice):
    _, fence = flat_lattice
    full = wv.ring_points(*at(0, 0), 400.0)
    assert len(full) == wv.RING_POINTS
    assert all(safety.haversine_m(*at(0, 0), *p) == pytest.approx(400.0, abs=0.5) for p in full)
    edge = wv.ring_points(*at(0, 4800), 400.0, geofence=fence)
    assert 0 < len(edge) < wv.RING_POINTS
    assert all(safety.point_in_polygon(*p, fence) for p in edge)
    south = wv.ring_points(*at(0, 0), 400.0, blocked=lambda la, lo: la > LAT0)
    assert all(p[0] <= LAT0 for p in south) and len(south) == 9


def test_outputs_carry_no_real_system_tokens(disc):
    lat, fence, exp, start, goal = disc
    route = plan(lat, exp, start, goal, fence)
    depot = unit("blue-depot-1", "blue", "blue_defended_point", 3000, 3000)
    armour = unit("red-armour-1", "red", "armour_company", 0, 0, objective="blue-depot-1")
    refusals = []
    for kw in ({"max_expansions": 1}, {}):
        try:
            plan(lat, exp, at(0, 9000) if not kw else start, goal, fence, **kw)
        except wv.NoRoute as err:
            refusals.append(err.as_result())
    assert len(refusals) == 2
    assert_no_real_system_tokens({
        "summary": wv.summarize(route.path, exp, SPEED), "waypoints": route.waypoints(),
        "axes": wv.red_axes([armour], {"blue-depot-1": depot}), "lattice": lat.as_dict(),
        "refusals": refusals, "words": [wv.TERRAIN_REAL, wv.TERRAIN_FLAT, wv.ALT_BAND_SURFACE],
        "doc": wv.__doc__})
