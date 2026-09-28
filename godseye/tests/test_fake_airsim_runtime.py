"""A2 (WG spec §4.1.6, §3.10): the fake's sim speed and its runtime origin move.

Scale tests 1, 2, 4, 5 and 6 of §4.1.6 live here (3 and 7 are fuel-coupled and
live in test_safety_scale.py), plus `relocate_origin`, `park_vehicle` and
`object_geo`. Almost everything drives the fake through an injected clock
(`FakeAirSim(clock=...)`) and calls `_integrate()` directly, so no test waits
on wall time except scale test 1, which is ABOUT wall time. The one wire test
binds a port in this unit's band, 53500-53599, and never leaves loopback.
"""
from __future__ import annotations

import itertools
import math
import threading
import time

import pytest
from godseye_uav import fake_airsim as fa
from godseye_uav.fake_airsim import (
    MAX_TICK_DT_S,
    MAX_TIME_SCALE,
    MIN_TIME_SCALE,
    FakeAirSim,
    _ObjectRoute,
    _Task,
    _Vehicle,
)
from godseye_uav.geo import GeoPoint, HomeGeoPoint, NedPoint, geodetic_to_ned, ned_to_geodetic
from godseye_uav.safety import haversine_m

REDMOND = fa.DEFAULT_HOME
#: A far-away runtime theater home (HAE), the E2E A1 place.
BENGALURU = GeoPoint(12.9716, 77.5946, 838.6)
_WIRE_PORTS = itertools.cycle(range(53500, 53600))


class ManualClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _manual_sim(**kw) -> tuple[FakeAirSim, ManualClock]:
    clock = ManualClock()
    return FakeAirSim(clock=clock, **kw), clock


def _airborne(sim: FakeAirSim, name: str = "Drone1", z: float = -60.0) -> _Vehicle:
    with sim._lock:
        v = sim._vehicles.setdefault(name, _Vehicle(name))
        v.ned = NedPoint(0.0, 0.0, z)
        v.armed, v.landed = True, False
    return v


def _command_leg(sim: FakeAirSim, v: _Vehicle, north_m: float, speed: float) -> None:
    with sim._lock:
        v.task = _Task("move_pos", NedPoint(north_m, 0.0, v.ned.z), speed)


def _step(sim: FakeAirSim, clock: ManualClock, wall_s: float, tick: float = 0.02) -> None:
    """Advance the manual clock `wall_s` in 50 Hz physics ticks."""
    for _ in range(round(wall_s / tick)):
        clock.advance(tick)
        sim._integrate()


def _run_physics(sim: FakeAirSim) -> threading.Thread:
    """The real 50 Hz physics loop, without an RPC server (no port)."""
    sim._stop.clear()
    sim._last_tick = sim._clock()
    th = threading.Thread(target=sim._physics_loop, daemon=True)
    th.start()
    return th


# ---------------------------------------------------------------------------
# Scale test 1: a 200 m leg at 10 m/s under x10 takes 2.0 +/- 0.5 s of wall.
# ---------------------------------------------------------------------------
def test_scale1_a_200m_leg_at_x10_takes_two_wall_seconds():
    sim = FakeAirSim()
    assert sim.set_time_scale(10) == 1.0
    v = _airborne(sim)
    th = _run_physics(sim)
    try:
        t0 = time.monotonic()
        _command_leg(sim, v, 200.0, 10.0)
        while not v.task.done and time.monotonic() - t0 < 10.0:
            time.sleep(0.005)
        wall = time.monotonic() - t0
    finally:
        sim._stop.set()
        th.join(timeout=2.0)
    assert v.task.done, "the leg never finished"
    assert wall == pytest.approx(2.0, abs=0.5)
    assert v.ned.x == pytest.approx(200.0, abs=0.6)


@pytest.mark.parametrize(("scale", "wall_s"), [(1.0, 20.0), (4.0, 5.0), (10.0, 2.0)])
def test_scale1_deterministic_leg_time_is_sim_time_over_scale(scale, wall_s):
    """Same leg, injected clock: the wall time is exactly 20 sim-s / scale."""
    sim, clock = _manual_sim()
    sim.set_time_scale(scale)
    v = _airborne(sim)
    _command_leg(sim, v, 200.0, 10.0)
    _step(sim, clock, wall_s - 0.1)
    assert not v.task.done, "arrived early: physics ran faster than the scale"
    _step(sim, clock, 0.2)
    assert v.task.done
    assert v.ned.x == pytest.approx(200.0, abs=0.5)  # the fake arrives within 0.5 m


# ---------------------------------------------------------------------------
# Scale test 2: every physics sub-step dt <= MAX_TICK_DT_S.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("scale", [1.0, 2.5, 7.3, 10.0])
@pytest.mark.parametrize("gap_s", [0.02, 0.1, 0.5, 3.0])
def test_scale2_every_substep_is_within_the_physics_clamp(scale, gap_s, monkeypatch):
    sim, clock = _manual_sim()
    sim.set_time_scale(scale)
    _airborne(sim)
    _airborne(sim, "Drone2")
    veh_dts: list[float] = []
    obj_dts: list[float] = []
    step_vehicle, advance_objects = sim._step_vehicle, sim._advance_objects

    def spy_vehicle(v, dt):
        veh_dts.append(dt)
        step_vehicle(v, dt)

    def spy_objects(dt):
        obj_dts.append(dt)
        advance_objects(dt)

    monkeypatch.setattr(sim, "_step_vehicle", spy_vehicle)
    monkeypatch.setattr(sim, "_advance_objects", spy_objects)
    clock.advance(gap_s)
    sim._integrate()

    n = math.ceil(scale)
    assert len(obj_dts) == n, "objects must integrate in the same sub-step loop"
    assert len(veh_dts) == 2 * n
    assert all(0.0 <= dt <= MAX_TICK_DT_S + 1e-12 for dt in veh_dts + obj_dts)
    # ...and together they fly exactly the clamped wall slice at sim speed.
    assert sum(obj_dts) == pytest.approx(min(gap_s, MAX_TICK_DT_S) * scale)


def test_scale2_a_stall_at_x10_is_counted_in_sim_seconds():
    sim, clock = _manual_sim()
    sim.set_time_scale(10)
    clock.advance(3.0)
    sim._integrate()
    env = sim.environment()
    assert env["sim_tick_clamped"] == 1
    assert env["sim_time_lost_s"] == pytest.approx((3.0 - MAX_TICK_DT_S) * 10.0)


# ---------------------------------------------------------------------------
# Scale test 4: the accepted range is 1..10 inclusive.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [0.5, 10.5, 0.0, -1.0, 0.999, 10.001, math.nan,
                                 math.inf, -math.inf, True, False, "fast", None, [2]])
def test_scale4_out_of_range_or_non_numbers_raise_and_change_nothing(bad):
    sim = FakeAirSim()
    sim.set_time_scale(3)
    with pytest.raises(ValueError):
        sim.set_time_scale(bad)
    assert sim.time_scale == 3.0


@pytest.mark.parametrize("good", [1, 10, 1.0, 10.0, 2.5, "4"])
def test_scale4_the_bounds_are_accepted_and_the_previous_value_returned(good):
    sim = FakeAirSim()
    assert (MIN_TIME_SCALE, MAX_TIME_SCALE) == (1.0, 10.0)
    assert sim.time_scale == 1.0
    assert sim.set_time_scale(good) == 1.0
    assert sim.time_scale == float(good)
    assert isinstance(sim.time_scale, float)
    assert sim.set_time_scale(1) == float(good)


# ---------------------------------------------------------------------------
# Scale test 5: sim_time() is continuous across a scale change.
# ---------------------------------------------------------------------------
def test_scale5_sim_time_is_continuous_across_a_scale_change_on_the_real_clock():
    sim = FakeAirSim()
    before = sim.sim_time()
    sim.set_time_scale(10)
    after = sim.sim_time()
    assert abs((after - before).total_seconds()) < 0.2
    sim.set_time_scale(1)
    assert abs((sim.sim_time() - after).total_seconds()) < 0.2


def test_scale5_sim_time_rebases_and_then_runs_at_the_new_speed():
    sim, clock = _manual_sim()
    t0 = sim.sim_time()
    clock.advance(100.0)
    assert (sim.sim_time() - t0).total_seconds() == pytest.approx(100.0)
    t1 = sim.sim_time()
    sim.set_time_scale(10)
    assert (sim.sim_time() - t1).total_seconds() == pytest.approx(0.0, abs=1e-9)
    clock.advance(10.0)
    assert (sim.sim_time() - t1).total_seconds() == pytest.approx(100.0)
    # the celestial multiplier composes with the sim speed (sun: clock x scale)
    sim.set_time("2026-06-21 06:00:00", clock_speed=2.0)
    clock.advance(30.0)
    assert sim.sim_time().strftime("%H:%M:%S") == "06:10:00"
    # a frozen sun stays frozen at any sim speed
    sim.set_time("2026-06-21 12:00:00", clock_speed=0.0)
    clock.advance(500.0)
    assert sim.sim_time().strftime("%H:%M:%S") == "12:00:00"


def test_sim_elapsed_s_is_scaled_continuous_and_independent_of_the_sun():
    sim, clock = _manual_sim()
    assert sim.sim_elapsed_s() == pytest.approx(0.0)
    clock.advance(5.0)
    assert sim.sim_elapsed_s() == pytest.approx(5.0)
    sim.set_time_scale(10)
    assert sim.sim_elapsed_s() == pytest.approx(5.0)
    clock.advance(2.0)
    assert sim.sim_elapsed_s() == pytest.approx(25.0)
    sim.set_time(clock_speed=0.0)  # freezing the sun does not stop sim time
    clock.advance(1.0)
    assert sim.sim_elapsed_s() == pytest.approx(35.0)
    sim.set_time_scale(1)
    clock.advance(1.0)
    assert sim.sim_elapsed_s() == pytest.approx(36.0)


# ---------------------------------------------------------------------------
# Scale test 6: environment() publishes the sim speed.
# ---------------------------------------------------------------------------
def test_scale6_environment_publishes_time_scale():
    sim = FakeAirSim()
    assert sim.environment()["time_scale"] == 1.0
    for s in (10, 2.5, 1):
        sim.set_time_scale(s)
        assert sim.environment()["time_scale"] == float(s)


def test_x1_physics_is_one_step_of_the_clamped_slice(monkeypatch):
    """At x1 `_integrate` is exactly the pre-D5 tick: one step, dt = the slice."""
    sim, clock = _manual_sim()
    dts: list[float] = []
    monkeypatch.setattr(sim, "_advance_objects", dts.append)
    clock.advance(0.02)
    sim._integrate()
    assert dts == [pytest.approx(0.02)]


# ---------------------------------------------------------------------------
# relocate_origin: the fake's copy of the NED origin (§4.1.3 step 2b).
# ---------------------------------------------------------------------------
def _spawn(sim: FakeAirSim, name: str, lat: float, lon: float, alt: float) -> GeoPoint:
    gp = GeoPoint(lat, lon, alt)
    with sim._lock:
        sim._objects[name] = gp
    return gp


def _route_geo(route: _ObjectRoute, fallback: HomeGeoPoint) -> list[GeoPoint]:
    anchor = route.anchor or fallback
    return [ned_to_geodetic(p, anchor) for p in route.waypoints]


def _close(a: GeoPoint, b: GeoPoint, *, horiz_m: float = 0.01, vert_m: float = 0.01) -> bool:
    return (haversine_m(a.latitude, a.longitude, b.latitude, b.longitude) <= horiz_m
            and abs(a.altitude - b.altitude) <= vert_m)


def test_relocate_parks_every_vehicle_at_ned0_landed_and_disarmed():
    sim, clock = _manual_sim()
    v1 = _airborne(sim)
    _command_leg(sim, v1, 500.0, 12.0)
    _step(sim, clock, 1.0)
    v2 = _airborne(sim, "Drone2", z=-30.0)
    with sim._lock:
        v2.collision = True
        v2.pitch_deg, v2.roll_deg = 10.0, -20.0
    old_task = v1.task

    origin = sim.relocate_origin(BENGALURU)

    assert origin == {"latitude": BENGALURU.latitude, "longitude": BENGALURU.longitude,
                      "altitude": BENGALURU.altitude}
    assert sim.home_geo.geo == BENGALURU
    assert old_task.cancelled, "a waiter on the old maneuver must unblock"
    for v in (v1, v2):
        assert v.ned == NedPoint(0.0, 0.0, 0.0)
        assert v.vel == NedPoint(0.0, 0.0, 0.0) == v.air_vel
        assert v.landed and not v.armed and not v.collision
        assert v.task.kind == "none" and v.task.done
        assert (v.pitch_deg, v.roll_deg) == (0.0, 0.0)
        gp = sim._reported_geo(v)
        assert _close(gp, BENGALURU, horiz_m=1e-6, vert_m=1e-9)
    # a parked vehicle stays put: nothing drifts it off the new home
    sim.set_wind(8.0, 3.0)
    _step(sim, clock, 1.0)
    assert v1.ned == NedPoint(0.0, 0.0, 0.0)


def test_relocate_accepts_a_home_geopoint_and_refuses_a_bad_origin():
    sim = FakeAirSim()
    assert sim.relocate_origin(HomeGeoPoint.from_geo(BENGALURU))["latitude"] == 12.9716
    for bad in (GeoPoint(math.nan, 0.0, 0.0), GeoPoint(91.0, 0.0, 0.0),
                GeoPoint(0.0, 181.0, 0.0), GeoPoint(0.0, 0.0, math.inf)):
        with pytest.raises(ValueError):
            sim.relocate_origin(bad)
    assert sim.home_geo.geo == BENGALURU, "a refused move must change nothing"


def test_relocate_keeps_scene_objects_and_obstructions_where_they_are():
    sim = FakeAirSim()
    truck = _spawn(sim, "truck_1", 47.6450, -122.1350, 125.0)
    sim.add_obstruction(47.6420, -122.1390, 180.0, name="mast")
    sim.relocate_origin(BENGALURU)
    assert sim.objects()["truck_1"] == truck
    assert sim.object_geo("truck_1") == (truck.latitude, truck.longitude, truck.altitude)
    assert sim.obstructions()[0]["lat"] == 47.6420


def _convoy_pair(far: bool):
    """Two identical sims flying the same convoy; only the second relocates."""
    pair = []
    for _ in range(2):
        sim, clock = _manual_sim()
        _spawn(sim, "convoy_1", 47.6400, -122.1450, 130.0)
        sim.set_object_route("convoy_1", [GeoPoint(47.6440, -122.1450, 130.0),
                                          GeoPoint(47.6440, -122.1380, 130.0)],
                             speed_mps=15.0)
        pair.append((sim, clock))
    return pair, (BENGALURU if far else GeoPoint(47.6600, -122.1100, 140.0))


@pytest.mark.parametrize("far", [True, False], ids=["far-13000km", "near-3km"])
def test_relocate_reanchors_routes_so_a_convoy_keeps_its_geodetic_path(far):
    """Mid-route, a convoy in the old area keeps flying the SAME lat/lon/alt path.

    The waypoints were written in NED about the old origin. Re-anchoring pins
    the route to that origin, so after the move its waypoints decode to the
    same geodetic points, the object resumes from where it stood, and it
    tracks a control sim that never moved to the millimetre. A far move is
    the case a naive NED->geo(old)->NED(new) conversion gets wrong (measured:
    7,300 km off and 9,273 km underground for Redmond -> Bengaluru; 38 m off
    and 48 m low for a 25 km move).
    """
    ((ctl, ctl_clock), (sim, clock)), new_home = _convoy_pair(far)
    route = sim._object_routes["convoy_1"]
    before = _route_geo(route, sim.home_geo)
    for s, c in ((ctl, ctl_clock), (sim, clock)):
        _step(s, c, 5.0)
    mid = sim._objects["convoy_1"]
    assert haversine_m(47.6400, -122.1450, mid.latitude, mid.longitude) > 50.0

    sim.relocate_origin(new_home)

    assert sim._objects["convoy_1"] == mid, "relocation must not move the object"
    after = _route_geo(route, sim.home_geo)
    assert all(_close(a, b, horiz_m=1e-6, vert_m=1e-6) for a, b in zip(before, after))
    for wall in (1.0, 30.0, 40.0):  # resumes, keeps going, then finishes
        for s, c in ((ctl, ctl_clock), (sim, clock)):
            _step(s, c, wall)
        assert _close(sim._objects["convoy_1"], ctl._objects["convoy_1"],
                      horiz_m=1e-3, vert_m=1e-3)
    assert route.done
    assert abs(sim._objects["convoy_1"].altitude - 130.0) < 0.2


def test_relocate_pins_a_legacy_unanchored_route_to_the_old_origin():
    target = GeoPoint(47.6420, -122.1450, 122.0)
    wp = geodetic_to_ned(target, REDMOND)
    sims = []
    for _ in range(2):
        sim, clock = _manual_sim()
        _spawn(sim, "boat_1", 47.6400, -122.1450, 122.0)
        with sim._lock:
            sim._object_routes["boat_1"] = _ObjectRoute([wp], 5.0)  # anchor=None
        sims.append((sim, clock))
    (ctl, ctl_clock), (sim, clock) = sims
    old = sim.home_geo
    sim.relocate_origin(BENGALURU)
    assert sim._object_routes["boat_1"].anchor is old
    _step(ctl, ctl_clock, 60.0)
    _step(sim, clock, 60.0)
    assert _close(sim._objects["boat_1"], ctl._objects["boat_1"], horiz_m=1e-3, vert_m=1e-3)
    assert _close(sim._objects["boat_1"], target, horiz_m=1.0, vert_m=0.1)


def test_a_route_20km_from_home_starts_where_the_object_stands():
    """Routes are anchored at the object: no jump, no altitude drop, far out.

    Anchored at the sim origin instead, this convoy jumped 23.7 m sideways and
    31 m down on its first routed tick (the NED model is local, and a group-3
    AO puts objects 25 km out).
    """
    sim, clock = _manual_sim()
    start = _spawn(sim, "convoy_far", REDMOND.latitude + 20_000.0 / 111_320.0,
                   REDMOND.longitude, 130.0)
    wp = GeoPoint(start.latitude + 500.0 / 111_320.0, start.longitude + 0.002, 130.0)
    sim.set_object_route("convoy_far", [wp], speed_mps=10.0)
    _step(sim, clock, 0.1)
    first = sim._objects["convoy_far"]
    assert haversine_m(start.latitude, start.longitude,
                       first.latitude, first.longitude) == pytest.approx(1.0, abs=0.05)
    assert abs(first.altitude - 130.0) < 0.01
    _step(sim, clock, 70.0)
    assert sim._object_routes["convoy_far"].done
    assert _close(sim._objects["convoy_far"], wp, horiz_m=1.0, vert_m=0.1)


def test_relocate_clears_the_gps_denial_fix_and_the_stale_telemetry_cache():
    sim = FakeAirSim()
    v = _airborne(sim)
    with sim._lock:
        v.ned = NedPoint(300.0, 0.0, -60.0)
    sim._reported_geo(v)  # records the last good fix at the OLD origin
    sim.set_gps_denied(True)
    sim._stale("state:Drone1", lambda: {"old": True})
    assert sim._last_good_geo and sim._stale_cache
    sim.relocate_origin(BENGALURU)
    assert sim._last_good_geo == {} and sim._stale_cache == {}
    # under denial the fix now freezes at the NEW home, not 13,000 km away
    gp = sim._reported_geo(v)
    assert _close(gp, BENGALURU, horiz_m=1e-6, vert_m=1e-9)


# ---------------------------------------------------------------------------
# park_vehicle / object_geo (§3.10; Phase B revives and reads through them).
# ---------------------------------------------------------------------------
def test_park_vehicle_puts_one_vehicle_home_and_leaves_the_rest():
    sim, clock = _manual_sim()
    v1, v2 = _airborne(sim), _airborne(sim, "Drone2")
    _command_leg(sim, v1, 300.0, 10.0)
    _command_leg(sim, v2, 300.0, 10.0)
    _step(sim, clock, 1.0)
    with sim._lock:
        v1.collision = True
    sim._reported_geo(v1)
    sim._reported_geo(v2)
    sim._stale("state:Drone1", lambda: 1)
    sim._stale("state:Drone2", lambda: 2)

    got = sim.park_vehicle("Drone1")

    assert got["vehicle"] == "Drone1" and got["landed"] is True
    assert (got["latitude"], got["longitude"]) == (REDMOND.latitude, REDMOND.longitude)
    assert v1.ned == NedPoint(0.0, 0.0, 0.0) and v1.landed and not v1.armed
    assert not v1.collision
    assert "Drone1" not in sim._last_good_geo and "state:Drone1" not in sim._stale_cache
    assert "Drone2" in sim._last_good_geo and "state:Drone2" in sim._stale_cache
    assert v2.ned.x > 5.0 and not v2.landed, "the other vehicle must keep flying"


def test_park_vehicle_refuses_an_unknown_name_without_minting_a_vehicle():
    sim = FakeAirSim()
    _spawn(sim, "truck_1", 47.64, -122.14, 122.0)
    for name in ("Ghost", "truck_1"):
        with pytest.raises(KeyError):
            sim.park_vehicle(name)
    assert set(sim._vehicles) == {"Drone1"}


def test_object_geo_is_ground_truth_hae_or_none():
    sim, clock = _manual_sim()
    assert sim.object_geo("nope") is None
    _spawn(sim, "sam_1", 47.6420, -122.1300, 131.5)
    assert sim.object_geo("sam_1") == (47.6420, -122.1300, 131.5)
    sim.set_object_route("sam_1", [GeoPoint(47.6440, -122.1300, 131.5)], speed_mps=10.0)
    _step(sim, clock, 2.0)
    lat, lon, alt = sim.object_geo("sam_1")
    assert haversine_m(47.6420, -122.1300, lat, lon) == pytest.approx(20.0, abs=0.5)
    assert alt == pytest.approx(131.5, abs=0.1)  # the NED model's own curvature term
    sim._dispatch().simDestroyObject("sam_1")
    assert sim.object_geo("sam_1") is None


# ---------------------------------------------------------------------------
# Wire: the REAL airsim client sees the moved origin (port band 53500-53599).
# ---------------------------------------------------------------------------
def _start_wire_sim() -> FakeAirSim:
    err = None
    for port in itertools.islice(_WIRE_PORTS, 100):
        s = FakeAirSim(port=port)
        try:
            s.start()
        except OSError as exc:
            err = exc
            s.stop()
            continue
        return s
    raise AssertionError(f"no free port in 53500-53599: {err}")


def test_wire_after_relocation_the_client_flies_at_the_new_home_at_x10():
    import airsim

    sim = _start_wire_sim()
    try:
        c = airsim.MultirotorClient(ip="127.0.0.1", port=sim.port)
        c.confirmConnection()
        c.enableApiControl(True)
        sim.set_time_scale(10)
        sim.relocate_origin(BENGALURU)

        hp = c.getHomeGeoPoint()
        assert (hp.latitude, hp.longitude, hp.altitude) == (
            BENGALURU.latitude, BENGALURU.longitude, BENGALURU.altitude)
        st = c.getMultirotorState()
        assert st.landed_state == 0
        g = st.gps_location
        assert haversine_m(g.latitude, g.longitude, BENGALURU.latitude,
                           BENGALURU.longitude) < 1e-3
        assert c.client.call("simGetEnvironment")["time_scale"] == 10.0

        c.armDisarm(True)
        c.takeoffAsync().join()
        tgt_lat = BENGALURU.latitude + 100.0 / 111_320.0
        t0 = time.monotonic()
        c.moveToGPSAsync(tgt_lat, BENGALURU.longitude, BENGALURU.altitude + 30.0, 10.0).join()
        deadline = t0 + 10.0
        # The fake stops inside its 0.5 m (3-D) arrival sphere, and this test
        # measures on a sphere while the fake converts on its local plane: a
        # loaded machine (bigger physics steps at x10) measured 1.03 m (A14).
        near_m = 1.5
        while time.monotonic() < deadline:
            g = c.getMultirotorState().gps_location
            if haversine_m(g.latitude, g.longitude, tgt_lat, BENGALURU.longitude) < near_m:
                break
            time.sleep(0.05)
        wall = time.monotonic() - t0
        assert haversine_m(g.latitude, g.longitude, tgt_lat, BENGALURU.longitude) < near_m
        assert wall < 5.0, f"a ~100 m leg at x10 took {wall:.1f} s of wall time"
        assert g.altitude == pytest.approx(BENGALURU.altitude + 30.0, abs=1.5)
    finally:
        sim.stop()
