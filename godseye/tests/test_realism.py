"""Phase 6: realism — wind (M15), GPS denial (M16), sensor noise (M17),
weather (M18), time of day + sun (M6), datalink (M9), moving targets."""
import itertools
import math
import re
import sys, pathlib

import airsim
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "mcp"))

from godseye_uav.fake_airsim import IMG_H, IMG_W, FakeAirSim, _frame_offset
from godseye_uav.geo import GeoPoint

# 46100-46199 is the fake-sim agent's reserved band (46000-46099 belongs to
# the geo gate). Cycle it and probe, so a concurrent run of the same file
# cannot collide on a fixed port.
_port = itertools.cycle(range(46140, 46200))


def _start_sim(**kw) -> FakeAirSim:
    """Start a sim on a free port in this band, stepping past one a concurrent
    run of this file already holds (several agents share this repo's tests)."""
    err: Exception | None = None
    for port in itertools.islice(_port, 60):
        s = FakeAirSim(port=port, **kw)
        try:
            s.start()
        except OSError as exc:  # port taken between probe and listen
            err = exc
            s.stop()
            continue
        return s
    raise AssertionError(f"no free port in 46140-46199: {err}")


@pytest.fixture
def sim():
    s = _start_sim(seed=1234)
    yield s
    s.stop()


def _gps(sim, veh="Drone1"):
    c = airsim.MultirotorClient(port=sim.port)
    st = c.getMultirotorState(vehicle_name=veh)
    g = st.gps_location
    return g.latitude, g.longitude, g.altitude


def _fly(sim, veh="Drone1"):
    # Fake maneuver handlers are non-blocking (command-ack); poll telemetry
    # until the vehicle is actually airborne before returning.
    import time
    c = airsim.MultirotorClient(port=sim.port)
    c.enableApiControl(True, veh)
    c.armDisarm(True, veh)
    c.takeoffAsync(vehicle_name=veh)
    c.moveToZAsync(-30.0, 3.0, vehicle_name=veh)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        st = c.getMultirotorState(vehicle_name=veh)
        if st.kinematics_estimated.position.z_val <= -25.0:
            return c
        time.sleep(0.1)
    raise AssertionError("vehicle did not reach altitude in time")


class TestWind:
    def test_wind_drifts_airborne_vehicle(self, sim):
        c = _fly(sim)
        # The drift case is the UNTASKED one, which is what this test always
        # said it was measuring ("hover (no task)"). `_fly` returns as soon as
        # the vehicle passes -25 m, so its moveToZ leg is usually still being
        # flown — and a commanded leg now CRABS: the autopilot holds the
        # ground track against the wind and the vehicle does not drift at all
        # (measured: 0.14 m of northing in 1.5 s of a 5 m/s wind, against the
        # 7.5 m this asserts). Let go of the controls first.
        c.cancelLastTask(vehicle_name="Drone1")
        lat0, lon0, _ = _gps(sim)
        # 5 m/s steady north wind, hover (no task) for ~1.5 s
        sim.set_wind(5.0, 0.0, 0.0)
        import time
        time.sleep(1.5)
        lat1, lon1, _ = _gps(sim)
        sim.set_wind(0.0, 0.0, 0.0)
        # north wind increases latitude
        assert lat1 > lat0 + 1e-5

    def test_no_wind_no_drift_when_landed(self, sim):
        lat0, lon0, _ = _gps(sim)
        sim.set_wind(5.0, 5.0, 0.0)
        import time
        time.sleep(0.5)
        lat1, lon1, _ = _gps(sim)
        sim.set_wind(0.0, 0.0, 0.0)
        assert abs(lat1 - lat0) < 1e-6 and abs(lon1 - lon0) < 1e-6


class TestGpsDenial:
    def test_denied_gps_freezes_fix(self, sim):
        c = _fly(sim)
        lat0, lon0, _ = _gps(sim)
        sim.set_gps_denied(True)
        # command a real move; truth moves but reported GPS must not
        c.moveToGPSAsync(lat0 + 0.001, lon0 + 0.001, 30.0, 5.0, vehicle_name="Drone1")
        import time
        time.sleep(2.0)
        lat1, lon1, _ = _gps(sim)
        sim.set_gps_denied(False)
        assert abs(lat1 - lat0) < 1e-6 and abs(lon1 - lon0) < 1e-6

    def test_regain_after_denial(self, sim):
        c = _fly(sim)
        sim.set_gps_denied(True)
        import time
        time.sleep(0.2)
        sim.set_gps_denied(False)
        lat0, lon0, _ = _gps(sim)
        c.moveToGPSAsync(lat0 + 0.0005, lon0, 30.0, 5.0, vehicle_name="Drone1")
        time.sleep(2.0)
        lat1, _, _ = _gps(sim)
        assert lat1 > lat0 + 1e-5  # fix updates again after denial clears


class TestSensorNoise:
    def test_noise_jitters_reported_gps(self, sim):
        sim.set_gps_noise(5.0)  # 5 m jitter
        fixes = [_gps(sim) for _ in range(12)]
        sim.set_gps_noise(0.0)
        lats = [f[0] for f in fixes]
        spread = max(lats) - min(lats)
        assert spread > 1e-6  # jitter present

    def test_zero_noise_is_stable(self, sim):
        sim.set_gps_noise(0.0)
        a = _gps(sim)
        b = _gps(sim)
        assert abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9


class TestDetectionRealism:
    def _spawn_near(self, sim):
        c = airsim.MultirotorClient(port=sim.port)
        g = c.getMultirotorState(vehicle_name="Drone1").gps_location
        c.simSpawnObject("truck_1", "truck",
                         airsim.Pose(airsim.Vector3r(0, 0, 0),
                                     airsim.Quaternionr()), False, False)
        # place object at the drone's location via direct handle
        from godseye_uav.geo import GeoPoint
        sim._objects["truck_1"] = GeoPoint(g.latitude, g.longitude, g.altitude)
        return c

    def test_false_negative_drops_contacts(self, sim):
        c = self._spawn_near(sim)
        sim.set_detection_realism(false_neg=1.0)  # always drop
        dets = c.simGetDetections("0", 0, vehicle_name="Drone1")
        sim.set_detection_realism(false_neg=0.0)
        assert all(d.name != "truck_1" for d in dets)

    def test_false_positive_invents_phantom(self, sim):
        c = self._spawn_near(sim)
        sim.set_detection_realism(false_pos=1.0)  # always invent
        dets = c.simGetDetections("0", 0, vehicle_name="Drone1")
        sim.set_detection_realism(false_pos=0.0)
        # The phantom is no longer self-labelling, so the ONLY way to know
        # which contact it was is the sim's out-of-band ground truth.
        assert sim.phantom_names() & {d.name for d in dets}

    def test_no_realism_is_clean(self, sim):
        c = self._spawn_near(sim)
        sim.set_detection_realism(0.0, 0.0)
        dets = c.simGetDetections("0", 0, vehicle_name="Drone1")
        names = [d.name for d in dets]
        assert names == ["truck_1"]
        assert not sim.phantom_names()

    def test_detection_noise_is_seedable(self, sim):
        """M17: the FP/FN draws must be reproducible from a seed, not from
        an inline random.random() a test cannot control."""
        c = self._spawn_near(sim)
        sim.set_detection_realism(false_neg=0.0, false_pos=0.5)

        def stream():
            # A phantom is indistinguishable by name, so count CONTACTS: a
            # frame carrying more than the one real truck carries a phantom.
            return [len(c.simGetDetections("0", 0, vehicle_name="Drone1")) > 1
                    for _ in range(24)]

        sim.seed(99)
        first = stream()
        sim.seed(99)
        assert stream() == first
        assert len(set(first)) == 2, "seeded stream should not be degenerate"
        sim.set_detection_realism(0.0, 0.0)


def _wait(predicate, message: str, timeout: float = 10.0):
    """Poll until `predicate()`, or fail with `message`."""
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message)


def _place(sim, name: str, north_m: float, east_m: float, down_m: float = 0.0):
    """Put a spawned object at a NED offset from home (no RPC needed)."""
    from godseye_uav.geo import NedPoint, ned_to_geodetic
    sim._objects[name] = ned_to_geodetic(NedPoint(north_m, east_m, down_m),
                                         sim.home_geo)
    return sim._objects[name]


class TestWeather:
    """M18: weather is a sensor-degradation knob, not a no-op stub."""

    def test_weather_over_rpc_is_stored(self, sim):
        c = airsim.MultirotorClient(port=sim.port)
        c.simSetWeatherParameter(airsim.WeatherParameter.Fog, 0.75)
        assert sim.weather()["fog"] == pytest.approx(0.75)
        assert sim.weather()["visibility_factor"] < 1.0

    def test_fog_shrinks_detection_range(self, sim):
        clear = sim.sensor_range_m("Drone1", "0", 0)
        sim.set_weather(fog=0.8)
        assert sim.sensor_range_m("Drone1", "0", 0) < clear * 0.5
        sim.set_weather(fog=0.0, enabled=False)
        assert sim.sensor_range_m("Drone1", "0", 0) == pytest.approx(clear)

    def test_fog_hides_a_distant_contact(self, sim):
        c = airsim.MultirotorClient(port=sim.port)
        _place(sim, "far_truck", 400.0, 0.0)
        assert any(d.name == "far_truck"
                   for d in c.simGetDetections("0", 0, vehicle_name="Drone1"))
        sim.set_weather(fog=0.9)
        assert not any(d.name == "far_truck"
                       for d in c.simGetDetections("0", 0, vehicle_name="Drone1"))
        sim.set_weather(fog=0.0, enabled=False)


class TestTimeOfDay:
    """M6: a real sun position — sun-side orbit doctrine depends on it."""

    def test_noon_is_high_and_midnight_is_below_the_horizon(self, sim):
        sim.set_time("2026-06-21 12:00:00")
        _, noon_el = sim.sun_position()
        sim.set_time("2026-06-21 00:00:00")
        _, night_el = sim.sun_position()
        assert noon_el > 40.0 and night_el < 0.0

    def test_sun_swings_east_to_west_over_rpc(self, sim):
        c = airsim.MultirotorClient(port=sim.port)
        c.simSetTimeOfDay(True, "2026-06-21 09:00:00", False, 1.0, 60, True)
        morning = c.client.call("simGetSunPosition", "Drone1")
        c.simSetTimeOfDay(True, "2026-06-21 15:00:00", False, 1.0, 60, True)
        afternoon = c.client.call("simGetSunPosition", "Drone1")
        assert 45.0 < morning["azimuth_deg"] < 150.0  # east-ish
        assert 210.0 < afternoon["azimuth_deg"] < 315.0  # west-ish
        assert morning["is_day"] and afternoon["is_day"]

    def test_night_degrades_eo_but_not_thermal(self, sim):
        sim.set_time("2026-06-21 01:00:00")
        eo = sim.sensor_range_m("Drone1", "0", 0)
        ir = sim.sensor_range_m("Drone1", "0", 7)  # ImageType.Infrared
        assert eo < ir
        assert sim.light_factor() < 0.5
        sim.set_time("2026-06-21 12:00:00")
        assert sim.sensor_range_m("Drone1", "0", 0) == pytest.approx(ir)


class TestLinkState:
    """M9: lost link must be exercisable UE-free."""

    def test_lost_link_stops_telemetry_and_recovers(self, sim):
        c = airsim.MultirotorClient(port=sim.port)
        assert c.getMultirotorState().landed_state == 0
        sim.set_link_state("Drone1", "lost", duration_s=0.6)
        with pytest.raises(Exception, match="datalink lost"):
            c.getMultirotorState()
        import time
        time.sleep(0.8)
        assert sim.link_state("Drone1")["state"] == "nominal"
        assert c.getMultirotorState().landed_state == 0

    def test_link_state_over_rpc(self, sim):
        c = airsim.MultirotorClient(port=sim.port)
        out = c.client.call("simSetLinkState", "Drone1", "degraded", 5.0)
        assert out["state"] == "degraded" and out["remaining_s"] > 0
        assert c.client.call("simGetLinkState", "Drone1")["state"] == "degraded"
        sim.set_link_state("Drone1", "nominal")

    def test_degraded_link_serves_stale_telemetry(self, sim):
        import time
        c = airsim.MultirotorClient(port=sim.port)
        c.enableApiControl(True, "Drone1")
        c.armDisarm(True, "Drone1")
        c.takeoffAsync(vehicle_name="Drone1")
        c.moveToZAsync(-60.0, 8.0, vehicle_name="Drone1")
        time.sleep(0.3)  # let the climb get going; no need to reach altitude
        sim.set_link_state("Drone1", "degraded")
        z0 = c.getMultirotorState().kinematics_estimated.position.z_val
        time.sleep(0.4)
        z1 = c.getMultirotorState().kinematics_estimated.position.z_val
        assert z1 == z0  # frozen inside the degraded hold window
        assert sim._veh("Drone1").ned.z < z0  # truth kept moving
        sim.set_link_state("Drone1", "nominal")

    def test_lost_link_takes_the_sensor_feed_with_it(self, sim):
        """M9: gating only telemetry left video and contacts streaming through
        a 'lost' link, so a lost-link plan lost nothing it had to react to."""
        c = airsim.MultirotorClient(port=sim.port)
        _place(sim, "lk_truck", 50.0, 0.0)
        reqs = [airsim.ImageRequest("0", 0, False, True)]
        assert c.simGetImages(reqs)[0].width == 256
        assert any(d.name == "lk_truck" for d in c.simGetDetections("0", 0))
        sim.set_link_state("Drone1", "lost")
        with pytest.raises(Exception, match="datalink lost"):
            c.simGetImages(reqs)
        with pytest.raises(Exception, match="datalink lost"):
            c.simGetDetections("0", 0)
        sim.set_link_state("Drone1", "nominal")
        assert any(d.name == "lk_truck" for d in c.simGetDetections("0", 0))

    def test_rejects_an_unknown_link_state(self, sim):
        with pytest.raises(ValueError):
            sim.set_link_state("Drone1", "jammed-ish")


class TestMovingTargets:
    """M17 Phase 6: convoys — a spawned object follows a route over time."""

    def test_routed_object_advances_toward_its_waypoint(self, sim):
        _place(sim, "convoy_1", 0.0, 0.0)
        start = sim.objects()["convoy_1"]
        sim.set_object_route("convoy_1", [GeoPoint(start.latitude + 0.01,
                                                   start.longitude,
                                                   start.altitude)], speed_mps=15.0)
        import time
        time.sleep(1.0)
        now = sim.objects()["convoy_1"]
        moved_n = (now.latitude - start.latitude) * 111320.0
        assert 8.0 < moved_n < 22.0, moved_n  # ~15 m/s north
        assert abs(now.longitude - start.longitude) * 90000.0 < 2.0

    def test_route_is_reported_and_finishes(self, sim):
        _place(sim, "convoy_2", 0.0, 0.0)
        start = sim.objects()["convoy_2"]
        sim.set_object_route("convoy_2",
                             [(start.latitude + 0.0002, start.longitude,
                               start.altitude)], speed_mps=40.0)
        import time
        time.sleep(1.2)
        state = sim.object_route("convoy_2")
        assert state["done"] is True and state["speed_mps"] == 40.0

    def test_moving_target_detections_carry_the_motion(self, sim):
        c = airsim.MultirotorClient(port=sim.port)
        _place(sim, "convoy_3", 0.0, 0.0)
        start = sim.objects()["convoy_3"]
        sim.set_object_route("convoy_3", [GeoPoint(start.latitude + 0.01,
                                                   start.longitude,
                                                   start.altitude)], speed_mps=12.0)
        first = next(d for d in c.simGetDetections("0", 0, vehicle_name="Drone1")
                     if d.name == "convoy_3")
        import time
        time.sleep(0.6)
        second = next(d for d in c.simGetDetections("0", 0, vehicle_name="Drone1")
                      if d.name == "convoy_3")
        assert second.geo_point.latitude > first.geo_point.latitude
        assert (second.relative_pose.position.x_val
                > first.relative_pose.position.x_val)

    def test_route_needs_a_spawned_object(self, sim):
        with pytest.raises(KeyError):
            sim.set_object_route("nobody_home", [(1.0, 2.0, 3.0)])


class TestGpsNoiseDeterminism:
    def test_seeded_gps_jitter_repeats(self, sim):
        sim.set_gps_noise(5.0)
        sim.seed(7)
        a = [_gps(sim) for _ in range(5)]
        sim.seed(7)
        b = [_gps(sim) for _ in range(5)]
        sim.set_gps_noise(0.0)
        assert a == b


class TestFuelJournalFromAFlownProfile:
    """T5/M4: the fuel journal the sim's own telemetry produces.

    Measured on the shipped stack: a full live flight wrote 1978 fuel rows and
    not ONE of them was a `descend`, because the fake reported
    linear_velocity.z_val = 0.00 through takeoffs and landings. The phase
    model the whole integrator rests on could not see the vertical phases it
    exists to distinguish, and a 2.94 m/s touchdown was charged as a climb.
    """

    def _fly_and_journal(self, sim, fm):
        """Fly takeoff -> climb -> cruise -> descend -> land, journalling."""
        import time

        import airsim as _airsim
        from godseye_uav.safety import FuelModel  # noqa: F401

        c = _airsim.MultirotorClient(port=sim.port)
        rows = []

        def journal(seconds, leg):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                st = c.getMultirotorState(vehicle_name="Drone1")
                v = st.kinematics_estimated.linear_velocity
                speed = (v.x_val ** 2 + v.y_val ** 2 + v.z_val ** 2) ** 0.5
                fm.tick(speed, v.z_val, st.landed_state == 0)
                # `leg` is what was COMMANDED; `phase` is what the integrator
                # measured. The whole point of T5 is that the second is not
                # read off the first, so the journal carries both.
                rows.append(fm.fuel_record(
                    leg=leg, vz=v.z_val,
                    z=st.kinematics_estimated.position.z_val))
                time.sleep(0.05)

        def journal_until(predicate, leg, limit=15.0):
            """Journal until the vehicle reaches an altitude, so each leg is
            flown to completion and the next one is a PURE phase."""
            end = time.monotonic() + limit
            while time.monotonic() < end:
                z = c.getMultirotorState(
                    vehicle_name="Drone1").kinematics_estimated.position.z_val
                if predicate(z):
                    return
                journal(0.1, leg)
            raise AssertionError(f"leg {leg!r} never completed")

        journal(0.2, "parked")                         # on the ground
        c.enableApiControl(True, "Drone1")
        c.armDisarm(True, "Drone1")
        c.takeoffAsync(vehicle_name="Drone1")
        journal(1.0, "takeoff")                        # takeoff climb
        c.moveToZAsync(-30.0, 6.0, vehicle_name="Drone1")
        journal_until(lambda z: z <= -29.0, "climb")   # commanded climb
        c.moveToPositionAsync(80.0, 0.0, -30.0, 10.0, vehicle_name="Drone1")
        journal(2.0, "cruise")                         # level cruise
        c.moveToZAsync(-6.0, 6.0, vehicle_name="Drone1")
        journal(2.0, "descend")                        # commanded descent
        journal_until(lambda z: z >= -7.0, "descend")  # ...flown to the bottom
        c.landAsync(vehicle_name="Drone1")
        # The command is an ACK, not a state. Between the descend leg reaching
        # its commanded altitude and the land leg being flown there is a real
        # instant where the aircraft is airborne and motionless — a genuine
        # hover, and journalling it as part of the "land" leg made this test
        # fail about one run in three on timing alone. Wait for the sim to
        # actually be flying the landing before journalling it.
        _wait(lambda: c.getMultirotorState(
            vehicle_name="Drone1").kinematics_estimated.linear_velocity.z_val > 0.0,
            "the landing never started")
        journal(1.0, "land")                           # the touchdown itself
        end = time.monotonic() + 20.0
        while (time.monotonic() < end
               and c.getMultirotorState(vehicle_name="Drone1").landed_state != 0):
            journal(0.2, "land")
        journal(0.3, "touchdown")                      # parked again
        return rows

    def test_a_flown_sortie_writes_every_phase_into_the_fuel_journal(self, sim):
        from godseye_uav.safety import FuelModel

        fm = FuelModel(home=(sim.home_geo.geo.latitude,
                             sim.home_geo.geo.longitude, 0.0))
        rows = self._fly_and_journal(sim, fm)
        phases = [r["phase"] for r in rows]

        def leg(name):
            return [r for r in rows if r["leg"] == name]

        assert {"ground", "climb", "cruise", "descend"} <= set(phases), sorted(set(phases))
        assert phases.count("descend") >= 3, "no descent survived into the journal"
        assert phases[0] == "ground" and phases[-1] == "ground"

        # each commanded leg is charged as the phase it actually flew (T5).
        # The takeoff and the landing are the two the sim used to report as
        # 0.00 m/s vertical, so they are the two asserted hardest.
        assert all(r["phase"] == "ground" for r in leg("parked"))
        assert any(r["phase"] == "climb" for r in leg("takeoff")), leg("takeoff")[:3]
        assert all(r["vz"] <= 0.0 for r in leg("takeoff"))
        assert all(r["phase"] == "climb" for r in leg("climb"))
        assert any(r["phase"] == "cruise" for r in leg("cruise"))
        assert any(r["phase"] == "descend" for r in leg("descend"))
        airborne_landing = [r for r in leg("land") if r["phase"] != "ground"]
        assert airborne_landing, "the landing was never sampled in the air"
        assert all(r["phase"] == "descend" and r["vz"] > 0.0
                   for r in airborne_landing), airborne_landing[:3]
        assert all(r["phase"] == "ground" for r in leg("touchdown"))

        # ...and the integrator actually charged that flight to the tank
        assert 0.0 < fm.burned_pct < 5.0
        assert rows[-1]["fuel_pct"] == pytest.approx(fm.fuel_pct, abs=0.001)
        assert rows[-1]["fuel_pct"] < rows[0]["fuel_pct"]
        assert rows[-1]["airframe"] == "quad_suas_electric"

    def test_the_touchdown_is_charged_as_a_descent_not_a_climb(self, sim):
        """The 2.94 m/s touchdown that was charged as a CLIMB, isolated.

        A landing that follows a climb is the exact shape of the bug: the
        stale velocity from the previous leg was still pointing UP.
        """
        import time

        import airsim as _airsim
        from godseye_uav.safety import FuelModel, Phase

        c = _airsim.MultirotorClient(port=sim.port)
        c.enableApiControl(True, "Drone1")
        c.armDisarm(True, "Drone1")
        c.takeoffAsync(vehicle_name="Drone1")
        c.moveToZAsync(-25.0, 5.0, vehicle_name="Drone1")   # climbing hard
        end = time.monotonic() + 10.0
        while time.monotonic() < end:
            st = c.getMultirotorState(vehicle_name="Drone1")
            if st.kinematics_estimated.position.z_val <= -20.0:
                break
            time.sleep(0.05)
        climb_vz = c.getMultirotorState(
            vehicle_name="Drone1").kinematics_estimated.linear_velocity.z_val
        assert climb_vz < -4.0, "never climbed"

        c.landAsync(vehicle_name="Drone1")
        time.sleep(0.3)
        st = c.getMultirotorState(vehicle_name="Drone1")
        v = st.kinematics_estimated.linear_velocity
        speed = (v.x_val ** 2 + v.y_val ** 2 + v.z_val ** 2) ** 0.5
        assert v.z_val > 1.0, f"a landing reported vz={v.z_val}"
        assert FuelModel().classify_phase(speed, v.z_val,
                                          st.landed_state == 0) is Phase.DESCEND


def _true_ned(sim, veh="Drone1"):
    """Sim GROUND TRUTH position — never the degraded fix."""
    return sim._veh(veh).ned


def _settle(client, predicate, timeout=25.0, veh="Drone1"):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate(client.getMultirotorState(vehicle_name=veh)):
            return True
        time.sleep(0.05)
    return False


class TestDegradedGpsDegradesNavigation:
    """M16/M17: a degraded fix is the NAV SOLUTION, not a corrupted read-out.

    MEASURED on the code before this: `sim_set_gps_degradation(denied=True)`
    froze the reported fix and changed nothing else, so a GPS-denied aircraft
    flew every waypoint *perfectly* and every contact it geolocated landed on
    exact ground truth. The tool was therefore a report-only stub: a harness
    that asked for a GPS-denied window got clean navigation and clean
    geolocation and no way to tell.
    """

    def test_a_denied_aircraft_navigates_from_the_fix_it_still_believes(self, sim):
        """Flown 150 m north under denial, then told to go back to where its
        FROZEN fix says it already is, it must not move — and must therefore
        end the mission 150 m from where the operator put it."""
        c = _fly(sim)
        start = _true_ned(sim)
        sim.set_gps_denied(True)

        c.moveToPositionAsync(start.x + 150.0, start.y, -30.0, 12.0,
                              vehicle_name="Drone1")
        assert _settle(c, lambda st: st.kinematics_estimated.position.x_val
                       >= start.x + 149.0, timeout=40.0), "never flew the leg"
        flown = _true_ned(sim)

        # its fix is still the pre-denial one, so "go to the start" is "stay"
        c.moveToPositionAsync(start.x, start.y, -30.0, 12.0, vehicle_name="Drone1")
        import time
        time.sleep(2.5)
        after = _true_ned(sim)
        sim.set_gps_denied(False)

        assert after.x == pytest.approx(flown.x, abs=5.0), (
            f"denied GPS still navigated: moved to {after.x:.1f} m north")
        assert after.x - start.x > 140.0

    def test_a_contact_inherits_the_platform_navigation_error(self, sim):
        """A geolocation is platform position + measured vector, so a wrong
        platform position puts every contact in the wrong place."""
        c = _fly(sim)
        start = _true_ned(sim)
        truth = _place(sim, "truck_1", start.x + 150.0, start.y + 60.0)

        clean = c.simGetDetections("0", 0, vehicle_name="Drone1")
        assert [d.name for d in clean] == ["truck_1"]
        assert clean[0].geo_point.latitude == pytest.approx(truth.latitude, abs=1e-9)

        sim.set_gps_denied(True)
        c.moveToPositionAsync(start.x + 150.0, start.y, -30.0, 10.0,
                              vehicle_name="Drone1")
        assert _settle(c, lambda st: st.kinematics_estimated.position.x_val
                       >= start.x + 149.0)
        dets = c.simGetDetections("0", 0, vehicle_name="Drone1")
        sim.set_gps_denied(False)

        assert [d.name for d in dets] == ["truck_1"]
        off_m = (truth.latitude - dets[0].geo_point.latitude) * 111320.0
        assert off_m == pytest.approx(150.0, abs=20.0), (
            f"contact reported {off_m:.1f} m off truth; the platform was 150 m off")

    def test_a_clean_gps_moves_nothing_at_all(self, sim):
        """The whole mechanism must be exactly zero when nothing is degraded."""
        c = _fly(sim)
        start = _true_ned(sim)
        truth = _place(sim, "truck_1", start.x + 120.0, start.y)
        c.moveToPositionAsync(start.x + 100.0, start.y + 40.0, -30.0, 10.0,
                              vehicle_name="Drone1")
        assert _settle(c, lambda st: (
            abs(st.kinematics_estimated.position.x_val - (start.x + 100.0)) < 0.5
            and abs(st.kinematics_estimated.position.y_val - (start.y + 40.0)) < 0.5))
        here = _true_ned(sim)
        assert here.x == pytest.approx(start.x + 100.0, abs=1.0)
        assert here.y == pytest.approx(start.y + 40.0, abs=1.0)
        d = c.simGetDetections("0", 0, vehicle_name="Drone1")[0]
        assert d.geo_point.latitude == pytest.approx(truth.latitude, abs=1e-12)
        assert d.geo_point.longitude == pytest.approx(truth.longitude, abs=1e-12)


def _geo_err_m(det, truth) -> float:
    """Horizontal metres between a reported contact and its ground truth."""
    dlat = (det.geo_point.latitude - truth.latitude) * 111320.0
    dlon = ((det.geo_point.longitude - truth.longitude) * 111320.0
            * math.cos(math.radians(truth.latitude)))
    return math.hypot(dlat, dlon)


def _rms(values) -> float:
    return math.sqrt(sum(x * x for x in values) / max(len(values), 1))


class TestContactsAreNotReportedAtExactTruth:
    """M17: a real contact sitting on its exact ground-truth coordinate is a
    label, not a measurement.

    MEASURED before this: every detection `geo_point` was the object's stored
    position to the last bit, while a false positive was named `phantom_N`.
    Between the two, separating truth from noise in this sim needed no fusion
    at all — a string prefix, or a lookup against the target table, scored
    100%. `geo_error_m` gives a contact the geolocation error a real sensor
    has: it grows with slant range, and it grows again when the sensor
    conditions the range model already knows about get worse.
    """

    FRAMES = 200

    def _sample(self, c, name, truth):
        out = []
        for _ in range(self.FRAMES):
            for d in c.simGetDetections("0", 0, vehicle_name="Drone1"):
                if d.name == name:
                    out.append(_geo_err_m(d, truth))
        return out

    def test_zero_is_zero_and_stays_bit_exact(self, sim):
        c = _fly(sim)
        truth = _place(sim, "truck_1", 120.0, 0.0)
        errs = self._sample(c, "truck_1", truth)
        assert len(errs) == self.FRAMES
        assert max(errs) == 0.0, "an unconfigured sensor must report truth"

    def test_the_error_grows_with_slant_range(self, sim):
        c = _fly(sim)
        near = _place(sim, "truck_1", 100.0, 0.0)     # ~104 m slant
        far = _place(sim, "truck_2", 460.0, 0.0)      # ~461 m slant
        sim.set_detection_realism(geo_error_m=10.0)
        near_e = self._sample(c, "truck_1", near)
        far_e = self._sample(c, "truck_2", far)
        sim.set_detection_realism(geo_error_m=0.0)

        assert len(near_e) == len(far_e) == self.FRAMES
        assert _rms(near_e) == pytest.approx(10.0 * math.sqrt(2), rel=0.35)
        assert _rms(far_e) > _rms(near_e) * 1.2, (
            f"near {_rms(near_e):.1f} m vs far {_rms(far_e):.1f} m")

    def test_weather_blurs_where_a_contact_is_not_just_whether_it_is_seen(self, sim):
        c = _fly(sim)
        near = _place(sim, "truck_1", 100.0, 0.0)
        sim.set_detection_realism(geo_error_m=10.0)
        clear = self._sample(c, "truck_1", near)
        sim.set_weather(fog=0.8, enabled=True)
        fogged = self._sample(c, "truck_1", near)
        sim.set_weather(fog=0.0, enabled=False)
        sim.set_detection_realism(geo_error_m=0.0)

        assert len(fogged) > 20, "fog dropped every single frame; nothing measured"
        assert _rms(fogged) > _rms(clear) * 2.0, (
            f"clear {_rms(clear):.1f} m vs fogged {_rms(fogged):.1f} m")

    def test_the_reported_position_and_the_relative_vector_stay_consistent(self, sim):
        """The error lives on the MEASURED vector, so `geo_point` and
        `relative_pose` still describe the same point. An error dropped on
        `geo_point` alone would leave a residual any consumer could difference
        out — which is the same self-labelling defect wearing a disguise."""
        c = _fly(sim)
        _place(sim, "truck_1", 150.0, 40.0)
        # Settle first: the two RPCs below must describe the same instant. The
        # let-down captures within 0.5 m of the commanded -30, so settle on
        # "stopped", not on an exact altitude.
        assert _settle(c, lambda st: st.kinematics_estimated.position.z_val
                       <= -29.4 and st.kinematics_estimated.linear_velocity.z_val
                       == 0.0)
        sim.set_detection_realism(geo_error_m=12.0)
        st = c.getMultirotorState(vehicle_name="Drone1")
        d = [x for x in c.simGetDetections("0", 0, vehicle_name="Drone1")
             if x.name == "truck_1"][0]
        sim.set_detection_realism(geo_error_m=0.0)

        from godseye_uav.geo import geodetic_to_ned
        seen = geodetic_to_ned(GeoPoint(d.geo_point.latitude,
                                        d.geo_point.longitude,
                                        d.geo_point.altitude), sim.home_geo.geo)
        p = st.kinematics_estimated.position
        assert d.relative_pose.position.x_val == pytest.approx(seen.x - p.x_val,
                                                               abs=1.0)
        assert d.relative_pose.position.y_val == pytest.approx(seen.y - p.y_val,
                                                               abs=1.0)


class TestAFalsePositiveIsNotSelfLabelling:
    """M17: `phantom_N` made the false-positive rate untestable as noise.

    MEASURED before this: with `false_pos=1.0` every invented contact was
    named `phantom_1`, `phantom_2`, ... — `name.startswith("phantom_")` was a
    perfect classifier, so nothing downstream ever had to cope with a spurious
    track. The phantom also sat at a uniform +/-120 m with a perfectly CENTRED
    `box2D` (the old call passed `off=None`), two more tells a real contact
    never had. A phantom now copies the scene's own naming pattern and is
    placed and boxed exactly like a true contact; the sim keeps the answer out
    of band in `phantom_names()`.
    """

    REAL = ("truck_1", "truck_2", "truck_3")

    def _scene(self, sim):
        c = _fly(sim)
        for i, name in enumerate(self.REAL):
            _place(sim, name, 90.0 + 40.0 * i, -60.0 + 60.0 * i)
        return c

    def test_the_name_alone_cannot_sort_truth_from_noise(self, sim):
        c = self._scene(sim)
        sim.set_detection_realism(false_pos=1.0)
        seen: set[str] = set()
        for _ in range(40):
            seen |= {d.name
                     for d in c.simGetDetections("0", 0, vehicle_name="Drone1")}
        sim.set_detection_realism(false_pos=0.0)

        ghosts = seen - set(self.REAL)
        assert ghosts, "false_pos=1.0 produced no phantom at all"
        assert ghosts <= sim.phantom_names()
        assert not [n for n in ghosts if "phantom" in n.lower()]
        # the real names and the invented ones are the SAME shape, so a
        # pattern fitted to the truth table matches the noise exactly
        pattern = re.compile(r"^truck_\d+$")
        assert all(pattern.match(n) for n in ghosts), sorted(ghosts)
        assert not (ghosts & set(self.REAL)), "a phantom collided with a real target"

    def test_a_phantom_is_placed_and_boxed_like_a_real_contact(self, sim):
        c = self._scene(sim)
        sim.set_detection_realism(false_pos=1.0)
        rng_m, offsets = [], []
        for _ in range(40):
            for d in c.simGetDetections("0", 0, vehicle_name="Drone1"):
                if d.name not in sim.phantom_names():
                    continue
                p = d.relative_pose.position
                rng_m.append(math.sqrt(p.x_val ** 2 + p.y_val ** 2 + p.z_val ** 2))
                offsets.append(abs((d.box2D.min.x_val + d.box2D.max.x_val) / 2.0
                                   - IMG_W / 2.0))
        sim.set_detection_realism(false_pos=0.0)

        assert rng_m, "no phantom observed"
        assert max(rng_m) <= sim.sensor_range_m() + 1.0, (
            "a phantom appeared outside the sensor's own detection range")
        # the old phantom passed off=None to _box2d, so its box was ALWAYS
        # dead-centre no matter where it was; a real contact's never is
        assert max(offsets) > 1.0, "every phantom box was perfectly centred"


class TestTheTwoNoiseKnobsDoNotClassifyEachOther:
    """M17: the name stopped labelling a phantom; the GEOMETRY started.

    The two knobs landed in the same wave and were only ever tested apart.
    `geo_error_m` moves a REAL contact's `relative_pose` off the bearing its
    `box2D` is drawn from — deliberately, because the pixels are where the
    contact really is and it is the range-and-pointing solution behind them
    that is imperfect. The phantom was built from ONE exact vector, so its
    `box2D` and its `relative_pose` agreed to the rounding of the pixel box.

    MEASURED on the code before this, 80 frames with `false_pos=1.0`,
    `geo_error_m=25.0` and a slewed ball: re-projecting `relative_pose`
    through the camera and differencing it against the `box2D` centre flagged
    45 of 45 phantoms and 0 of 237 real contacts — a PERFECT classifier, worst
    phantom residual 0.023 deg against best real residual 0.696 deg, a 30x
    gap. `phantom_names()` was still out of band and the names were still
    unsortable; the consumer just stopped needing either. That is the same
    defect `phantom_N` was, one layer down.
    """

    REAL = ("truck_1", "truck_2", "truck_3")
    FRAMES = 80
    #: A box2D coordinate is rounded to 0.1 px, which at 90 deg over IMG_W is
    #: ~0.035 deg. Anything at or below this floor carries NO error at all.
    ROUNDING_FLOOR_DEG = 0.1

    def _residual_deg(self, sim, d):
        """Degrees between where `box2D` puts a contact and where
        `relative_pose` does, re-projected through the same camera. Zero means
        the two were built from one exact vector."""
        cam = sim.camera("Drone1", "0")
        p = d.relative_pose.position
        off = _frame_offset(sim._vehicles["Drone1"], cam,
                            p.x_val, p.y_val, p.z_val)
        if off is None:  # the errored vector left the frustum; nothing to say
            return None
        px_per_deg = IMG_W / max(1.0, cam.fov_deg)
        cx = (d.box2D.min.x_val + d.box2D.max.x_val) / 2.0
        cy = (d.box2D.min.y_val + d.box2D.max.y_val) / 2.0
        return math.hypot((cx - IMG_W / 2.0) / px_per_deg - off[0],
                          (IMG_H / 2.0 - cy) / px_per_deg - off[1])

    def _settled_scene(self, sim):
        import time
        c = _fly(sim)
        # Out at phantom ranges (a phantom is drawn across the sensor's whole
        # footprint), and inside the slewed ball's frustum, so the two
        # populations are compared over the same geometry rather than at
        # different ends of the range law.
        for i, name in enumerate(self.REAL):
            _place(sim, name, 225.0 + 75.0 * i, 90.0 + 30.0 * i)
        # Stop flying before measuring: `_residual_deg` re-projects through the
        # LIVE attitude, so the aircraft has to be holding one.
        c.cancelLastTask(vehicle_name="Drone1")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            v = c.getMultirotorState(
                vehicle_name="Drone1").kinematics_estimated.linear_velocity
            if (v.x_val, v.y_val, v.z_val) == (0.0, 0.0, 0.0):
                break
            time.sleep(0.05)
        # A slewed ball is what turns the frustum gate — and the off-boresight
        # pixel maths this test reads — on at all (M7).
        c.simSetCameraPose("0", airsim.Pose(
            airsim.Vector3r(0.0, 0.0, 0.0),
            airsim.to_quaternion(math.radians(-10.0), 0.0,
                                 math.radians(10.0))),
            vehicle_name="Drone1")
        time.sleep(0.2)
        return c

    def _sample(self, sim, c):
        phantom, real = [], []
        for _ in range(self.FRAMES):
            for d in c.simGetDetections("0", 0, vehicle_name="Drone1"):
                r = self._residual_deg(sim, d)
                if r is None:
                    continue
                (phantom if d.name in sim.phantom_names() else real).append(r)
        return phantom, real

    def test_box2d_and_relative_pose_do_not_betray_a_phantom(self, sim):
        c = self._settled_scene(sim)
        sim.set_detection_realism(false_pos=1.0, geo_error_m=25.0)
        phantom, real = self._sample(sim, c)
        sim.set_detection_realism(false_pos=0.0, geo_error_m=0.0)

        assert len(phantom) >= 10, f"only {len(phantom)} phantoms observed"
        assert len(real) >= 30, f"only {len(real)} real contacts observed"

        # 1. a phantom carries a REAL geolocation error, not pixel rounding.
        #    Before the fix every phantom sat at 0.02 deg — the floor.
        assert min(phantom) > self.ROUNDING_FLOOR_DEG, (
            f"a phantom's box2D and relative_pose agreed to "
            f"{min(phantom):.4f} deg: it was built from one exact vector "
            f"while every real contact's disagree by the geolocation error")

        # 2. the two populations OVERLAP, so no threshold splits them. Before
        #    the fix max(phantom) was 0.023 and min(real) 0.696.
        assert max(phantom) > min(real), (
            f"phantom residuals (max {max(phantom):.3f} deg) sit entirely "
            f"below the real ones (min {min(real):.3f} deg)")

        # 3. ...stated as the thing that actually matters: the BEST single
        #    threshold over this sample cannot separate noise from truth.
        best_errors = min(
            sum(1 for x in phantom if x >= t) + sum(1 for x in real if x < t)
            for t in sorted(phantom + real))
        assert best_errors > 0, (
            "some threshold on the box2D/relative_pose residual is a PERFECT "
            "classifier for false positives")

    def test_the_residual_is_still_exactly_zero_with_no_geolocation_error(
            self, sim):
        """The tell is the geo error, so with the knob off both populations
        must sit on the rounding floor — a phantom is placed and boxed from
        one vector, and so is a real contact. This is what says the fix added
        an error rather than jittering the box."""
        c = self._settled_scene(sim)
        sim.set_detection_realism(false_pos=1.0)
        phantom, real = self._sample(sim, c)
        sim.set_detection_realism(false_pos=0.0)

        assert phantom and real
        assert max(phantom) <= self.ROUNDING_FLOOR_DEG
        assert max(real) <= self.ROUNDING_FLOOR_DEG


class TestTheFalsePositiveRateDoesNotSwitchItselfOff:
    """M17: `det_false_pos` has to still be true 20 minutes into a sortie.

    A phantom name is minted by rejecting draws that collide with anything
    already spoken for — the scene, the vehicles, and every phantom ever
    minted — and the ledger of past phantoms only ever GREW. So the pattern
    the scene dictates is a finite pool that the sim empties into itself, and
    when 200 consecutive draws collide `_phantom_name` returned "" and
    `simGetDetections` emitted no phantom at all.

    MEASURED on the code before this, scene `truck_1..truck_3` (a plain
    `truck_N` index, 999 names wide) with `det_false_pos=1.0`: frames 0-995
    each carried exactly one phantom and EVERY frame after that carried none,
    permanently — while `environment()` went on publishing
    `det_false_pos: 1.0`. At a 10 Hz detection poll the configured
    false-positive rate silently becomes zero 100 seconds in. That is a knob
    that reports itself as on while doing nothing, which is the same defect
    class as a `wind_ref_mps` of zero.

    The pool here is deliberately SMALL — one zero-padded scene object gives a
    99-name space — so the exhaustion the old code hit after ~1000 frames is
    reached in ~100 and the test stays quick.
    """

    #: `truck_07` fixes the index shape at 2 zero-padded digits, so the whole
    #: name space a phantom may copy is truck_01..truck_99.
    SCENE = "truck_07"
    POOL = 99
    FRAMES = 400

    def test_a_scene_with_a_small_name_space_keeps_producing_phantoms(self, sim):
        c = _fly(sim)
        _place(sim, self.SCENE, 120.0, 40.0)
        sim.set_detection_realism(false_pos=1.0)
        per_block, block = [], 0
        for i in range(self.FRAMES):
            dets = c.simGetDetections("0", 0, vehicle_name="Drone1")
            block += sum(1 for d in dets if d.name in sim.phantom_names())
            if (i + 1) % 100 == 0:
                per_block.append(block)
                block = 0
        sim.set_detection_realism(false_pos=0.0)

        # The pool is 99 wide, so the OLD code's blocks read roughly
        # [98, 0, 0, 0]: it ran dry inside the first hundred frames.
        assert len(per_block) == 4
        assert all(n >= 90 for n in per_block), (
            f"phantoms per 100 frames: {per_block} — at false_pos=1.0 every "
            f"frame should carry one, and a block near zero is the rate "
            f"switching itself off once the name pool is exhausted")
        # ...and the thing the pool existed to protect still holds: a phantom
        # never wears the name of something really in the scene.
        assert self.SCENE not in sim.phantom_names()
        assert len(sim.phantom_names()) <= self.POOL

    def test_the_reused_names_still_copy_the_scenes_shape_and_stay_seeded(
            self, sim):
        """A repeat name is still an unsortable name, and the stream past
        exhaustion still replays from a seed — reuse must not smuggle in an
        unseeded draw."""
        c = _fly(sim)
        _place(sim, self.SCENE, 120.0, 40.0)
        sim.set_detection_realism(false_pos=1.0)

        def run():
            sim.seed(4242)
            return [tuple(d.name for d in
                          c.simGetDetections("0", 0, vehicle_name="Drone1"))
                    for _ in range(self.POOL + 120)]

        first = run()
        assert run() == first, "the post-exhaustion stream is not reproducible"
        sim.set_detection_realism(false_pos=0.0)

        tail = [names for names in first[self.POOL + 20:]]
        assert all(len(n) == 2 for n in tail), (
            "frames past the pool size stopped carrying a phantom")
        shape = re.compile(r"^truck_\d{2}$")
        assert all(shape.match(n) for frame in tail for n in frame), sorted(
            {n for frame in tail for n in frame})
