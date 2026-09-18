"""Contract test: REAL airsim PythonClient against FakeAirSim server.

Proves the fake speaks the real wire protocol (T8 UE-free CI path)."""
import itertools
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

from godseye_uav.fake_airsim import FakeAirSim  # noqa: E402
from godseye_uav.geo import (  # noqa: E402
    GeoPoint, NedPoint, geodetic_to_ned, ned_to_geodetic)

import airsim  # noqa: E402  (the in-repo PythonClient)

# Ports 46110-46139 are this module's band. Cycling it (rather than binding one
# fixed port) keeps a concurrent run of this same file from colliding, and a
# fresh sim per wire-regression test keeps their scenario state independent.
_WIRE_PORT = itertools.cycle(range(46110, 46140))


def _start_sim(**kw) -> FakeAirSim:
    """Start a sim on a free port in this band, stepping past one a concurrent
    run of this file already holds (several agents share this repo's tests)."""
    err = None
    for port in itertools.islice(_WIRE_PORT, 30):
        s = FakeAirSim(port=port, **kw)
        try:
            s.start()
        except OSError as exc:
            err = exc
            s.stop()
            continue
        return s
    raise AssertionError(f"no free port in 46110-46139: {err}")


@pytest.fixture(scope="module")
def server():
    s = _start_sim()
    time.sleep(0.3)
    yield s
    s.stop()


@pytest.fixture()
def client(server):
    c = airsim.MultirotorClient(ip="127.0.0.1", port=server.port)
    c.confirmConnection()
    return c


class TestWire:
    def test_ping(self, client):
        assert client.ping() is True

    def test_home_geo_point(self, client):
        hp = client.getHomeGeoPoint()
        assert abs(hp.latitude - 47.641468) < 1e-6
        assert abs(hp.longitude - (-122.140165)) < 1e-6

    def test_api_control_and_arm(self, client):
        client.enableApiControl(True)
        assert client.isApiControlEnabled() is True
        assert client.armDisarm(True) is True


class TestFlight:
    def test_takeoff_then_move_gps(self, client):
        client.enableApiControl(True)
        client.armDisarm(True)
        client.takeoffAsync().join()
        time.sleep(2.0)  # fake takeoff rate 2 m/s to 3 m AGL
        st0 = client.getMultirotorState()
        alt0 = st0.gps_location.altitude
        client.moveToGPSAsync(47.641900, -122.139500, alt0, 15.0).join()
        # wait for arrival (50 m at up to 15 m/s)
        deadline = time.time() + 8
        st1 = st0
        while time.time() < deadline:
            time.sleep(0.3)
            st1 = client.getMultirotorState()
            if (abs(st1.gps_location.latitude - 47.641900) < 5e-4
                    and abs(st1.gps_location.longitude - (-122.139500)) < 5e-4):
                break
        assert abs(st1.gps_location.latitude - 47.641900) < 5e-4
        assert abs(st1.gps_location.longitude - (-122.139500)) < 5e-4

    def test_hover_and_cancel(self, client):
        client.enableApiControl(True)
        client.armDisarm(True)
        client.hoverAsync()
        client.cancelLastTask()

    def test_land(self, client):
        client.enableApiControl(True)
        client.armDisarm(True)
        client.landAsync().join()
        deadline = time.time() + 6
        st = client.getMultirotorState()
        while time.time() < deadline and st.landed_state != 0:
            time.sleep(0.3)
            st = client.getMultirotorState()
        assert st.landed_state == 0


class TestSensors:
    def test_gps_data(self, client):
        gd = client.getGpsData()
        assert gd.is_valid is True
        assert abs(gd.gnss.geo_point.latitude - 47.641468) < 1e-2

    def test_images_batch(self, client):
        reqs = [
            airsim.ImageRequest("0", airsim.ImageType.Scene, False, True),
            airsim.ImageRequest("0", airsim.ImageType.Infrared, False, True),
        ]
        resps = client.simGetImages(reqs)
        assert len(resps) == 2
        for r in resps:
            assert r.width == 256 and r.height == 144
            data = r.image_data_uint8
            assert data is not None and len(data) > 0

    def test_collision_info(self, client):
        ci = client.simGetCollisionInfo()
        assert ci.has_collided is False


class TestScenario:
    def test_spawn_and_detect(self, client):
        pose = airsim.Pose()
        pose.position = airsim.Vector3r(50.0, 30.0, 0.0)
        name = client.simSpawnObject("tank1", "TANK", pose, airsim.Vector3r(1, 1, 1))
        assert name == "tank1"
        client.enableApiControl(True)
        client.armDisarm(True)
        client.takeoffAsync().join()
        time.sleep(0.3)
        dets = client.simGetDetections("0", airsim.ImageType.Scene)
        names = [d.name for d in dets]
        assert "tank1" in names
        d = next(x for x in dets if x.name == "tank1")
        assert abs(d.geo_point.latitude - 47.641468) < 5e-3

    def test_list_vehicles(self, client):
        vs = client.listVehicles()
        assert "Drone1" in vs


# ---------------------------------------------------------------------------
# Wire-decoding regressions + the RPCs the tool catalog needs (fresh sim each).
# ---------------------------------------------------------------------------


@pytest.fixture()
def wire_sim():
    s = _start_sim(seed=11)
    time.sleep(0.2)
    yield s
    s.stop()


@pytest.fixture()
def wire(wire_sim):
    c = airsim.MultirotorClient(ip="127.0.0.1", port=wire_sim.port)
    c.confirmConnection()
    return c


def _horiz_m(a: GeoPoint, b: GeoPoint) -> float:
    lat = math.radians((a.latitude + b.latitude) / 2.0)
    dy = math.radians(b.latitude - a.latitude) * 6378137.0
    dx = math.radians(b.longitude - a.longitude) * 6378137.0 * math.cos(lat)
    return math.hypot(dx, dy)


def _rotate_by_q(q, v: tuple[float, float, float]) -> tuple[float, float, float]:
    """Rotate a vector by an AirSim quaternion (independent of the sim's math)."""
    w, ux, uy, uz = q.w_val, q.x_val, q.y_val, q.z_val
    cx = uy * v[2] - uz * v[1] + w * v[0]
    cy = uz * v[0] - ux * v[2] + w * v[1]
    cz = ux * v[1] - uy * v[0] + w * v[2]
    return (v[0] + 2.0 * (uy * cz - uz * cy),
            v[1] + 2.0 * (uz * cx - ux * cz),
            v[2] + 2.0 * (ux * cy - uy * cx))


def _spawn(client, sim, name: str, target: GeoPoint):
    ned = geodetic_to_ned(target, sim.home_geo.geo)
    return client.simSpawnObject(
        name, "Cylinder", airsim.Pose(airsim.Vector3r(ned.x, ned.y, ned.z)),
        airsim.Vector3r(1, 1, 1))


class TestWireDictKeys:
    """R1/R2: msgpack-rpc delivers dict keys as BYTES.

    Every handler that read a wire dict with str keys AND a default silently
    took the default forever. These cases all fail on the pre-fix code.
    """

    def test_spawn_lands_on_the_requested_geodetic_point(self, wire, wire_sim):
        # Pre-fix: pose.get("position", {zeros}) -> every target on the home
        # origin (measured 579 m error on a 33.724/51.724 request).
        home = wire_sim.home_geo.geo
        target = GeoPoint(home.latitude + 0.0045, home.longitude + 0.0045,
                          home.altitude)
        _spawn(wire, wire_sim, "r1_marker", target)
        got = wire_sim.objects()["r1_marker"]
        assert _horiz_m(target, home) > 400.0  # the request is far from home
        assert _horiz_m(target, got) < 5.0, (target, got)

    def test_set_object_pose_moves_to_the_requested_point(self, wire, wire_sim):
        home = wire_sim.home_geo.geo
        _spawn(wire, wire_sim, "r1_mover", home)
        moved = GeoPoint(home.latitude + 0.002, home.longitude, home.altitude)
        ned = geodetic_to_ned(moved, home)
        assert wire.simSetObjectPose(
            "r1_mover", airsim.Pose(airsim.Vector3r(ned.x, ned.y, ned.z))) is True
        assert _horiz_m(moved, wire_sim.objects()["r1_mover"]) < 5.0

    def test_wind_over_rpc_reaches_the_sim(self, wire, wire_sim):
        # Pre-fix: wind.get("x_val", 0.0) -> simSetWind was a no-op (M15 dead).
        wire.simSetWind(airsim.Vector3r(12.0, 5.0, -1.0))
        w = wire_sim.wind()
        assert (w.x, w.y, w.z) == (12.0, 5.0, -1.0)

    def test_image_request_fields_are_honoured(self, wire, wire_sim):
        # Pre-fix: req.get("image_type"/"camera_name"/"compress") all defaulted.
        reqs = [airsim.ImageRequest("3", airsim.ImageType.Infrared, False, False)]
        r = wire.simGetImages(reqs)[0]
        assert r.camera_name == "3"
        assert r.image_type == airsim.ImageType.Infrared
        assert isinstance(r.image_data_uint8, list)  # compress=False honoured
        assert bytes(r.image_data_uint8)[:4] == b"\x89PNG"

    def test_spawned_object_is_not_a_vehicle(self, wire, wire_sim):
        _spawn(wire, wire_sim, "r1_target", wire_sim.home_geo.geo)
        assert wire.listVehicles() == ["Drone1"]  # no phantom drone on the globe

    def test_a_name_read_as_a_vehicle_before_it_is_spawned_is_still_not_listed(
            self, wire, wire_sim):
        """Refusing the name in _veh() only closes the door AFTER the spawn.

        Reading the name as a vehicle first minted the phantom, and nothing
        ever removed it — listVehicles kept parking a cyan drone on home.
        """
        wire.getMultirotorState(vehicle_name="convoy_9")  # mints the phantom
        assert "convoy_9" in wire_sim._vehicles
        _spawn(wire, wire_sim, "convoy_9", wire_sim.home_geo.geo)
        assert wire.listVehicles() == ["Drone1"]


class TestCameraControl:
    """M7: gimbal + FOV must exist AND change what the sensor reports."""

    def test_fov_is_stored_and_reported(self, wire, wire_sim):
        wire.simSetCameraFov("0", 20.0)
        info = wire.simGetCameraInfo("0")
        assert abs(info.fov - 20.0) < 1e-6
        assert abs(wire_sim.camera("Drone1", "0").fov_deg - 20.0) < 1e-6

    def test_narrow_fov_extends_detection_range(self, wire, wire_sim):
        wide = wire_sim.sensor_range_m("Drone1", "0", 0)
        wire.simSetCameraFov("0", 30.0)
        assert wire_sim.sensor_range_m("Drone1", "0", 0) > wide * 2.5

    def test_narrow_fov_puts_more_pixels_on_the_contact(self, wire, wire_sim):
        home = wire_sim.home_geo.geo
        _spawn(wire, wire_sim, "cue_target",
               GeoPoint(home.latitude + 0.0009, home.longitude, home.altitude))

        def box_width():
            d = next(x for x in wire.simGetDetections("0", 0) if x.name == "cue_target")
            return d.box2D.max.x_val - d.box2D.min.x_val

        wire.simSetCameraFov("0", 90.0)
        wide = box_width()
        wire.simSetCameraFov("0", 15.0)
        assert box_width() > wide * 2.0  # cross-cue: higher pixel density

    def test_gimbal_pose_is_stored_and_gates_the_frame(self, wire, wire_sim):
        home = wire_sim.home_geo.geo
        _spawn(wire, wire_sim, "north_target",
               GeoPoint(home.latitude + 0.0009, home.longitude, home.altitude))
        assert any(d.name == "north_target" for d in wire.simGetDetections("0", 0))
        # slew the ball hard south: the northern contact leaves the frame
        wire.simSetCameraPose("0", airsim.Pose(
            airsim.Vector3r(0, 0, 0), airsim.to_quaternion(0.0, 0.0, math.pi)))
        cam = wire_sim.camera("Drone1", "0")
        assert cam.slewed is True and abs(abs(cam.yaw_deg) - 180.0) < 1e-3
        assert not any(d.name == "north_target" for d in wire.simGetDetections("0", 0))

    def test_a_slewed_camera_still_sees_what_it_is_pointed_at(self, wire, wire_sim):
        """M7: the gate must be a real frustum, not a world-frame az/el box.

        Comparing world azimuth against the boresight azimuth is degenerate
        once the ball is slewed off level — which is the only time the gate
        runs. With the camera at nadir, 7 of 8 ground contacts 11 deg off the
        boresight were rejected on azimuth alone.
        """
        v = wire_sim._veh("Drone1")
        v.ned = NedPoint(0.0, 0.0, -100.0)  # 100 m AGL, level, heading north
        for brg in range(0, 360, 45):
            n = 20.0 * math.cos(math.radians(brg))
            e = 20.0 * math.sin(math.radians(brg))
            wire_sim._objects[f"ring_{brg:03d}"] = ned_to_geodetic(
                NedPoint(n, e, 0.0), wire_sim.home_geo)
        # nadir slew: every ring contact is 11.3 deg off boresight, well inside
        # the 90 deg horizontal / 58.7 deg vertical frame.
        wire.simSetCameraPose("0", airsim.Pose(
            airsim.Vector3r(0, 0, 0), airsim.to_quaternion(-math.pi / 2, 0, 0)))
        seen = {d.name for d in wire.simGetDetections("0", 0)}
        assert len([n for n in seen if n.startswith("ring_")]) == 8, sorted(seen)
        # and a contact genuinely outside the nadir frame stays out (56 deg off)
        wire_sim._objects["far_side"] = ned_to_geodetic(
            NedPoint(150.0, 0.0, 0.0), wire_sim.home_geo)
        assert not any(d.name == "far_side" for d in wire.simGetDetections("0", 0))

    def test_camera_position_rotates_the_mount_offset_into_ned(self, wire, wire_sim):
        """M7: a mount offset is BODY-frame; adding it raw to a NED position
        puts a nose camera 2 m north of an east-bound drone."""
        v = wire_sim._veh("Drone1")
        v.ned, v.heading_deg = NedPoint(0.0, 0.0, -100.0), 90.0  # heading east
        wire_sim.set_camera_pose("Drone1", "0", offset=NedPoint(2.0, 0.0, -0.5))
        pos = wire.simGetImages([airsim.ImageRequest("0", 0, False, True)])[0]\
            .camera_position
        assert abs(pos.x_val - 0.0) < 1e-6, pos.x_val
        assert abs(pos.y_val - 2.0) < 1e-6, pos.y_val  # 2 m EAST, not north
        assert abs(pos.z_val - (-100.5)) < 1e-6

    def test_camera_orientation_composes_body_attitude_with_the_gimbal(
            self, wire, wire_sim):
        """M7: rotations compose by quaternion product, not by adding Euler
        angles — a 30 deg roll tilts a nadir ball 30 deg off vertical."""
        v = wire_sim._veh("Drone1")
        v.roll_deg, v.pitch_deg, v.heading_deg = 30.0, 0.0, 0.0
        wire_sim.set_camera_pose("Drone1", "0", pitch_deg=-90.0)  # nadir gimbal
        q = wire.simGetImages([airsim.ImageRequest("0", 0, False, True)])[0]\
            .camera_orientation
        bore = _rotate_by_q(q, (1.0, 0.0, 0.0))  # camera x-axis in NED
        # belly of a right-rolled airframe tilts toward -east
        assert abs(bore[0] - 0.0) < 1e-9, bore
        assert abs(bore[1] - (-0.5)) < 1e-6, bore  # Euler-sum would give 0.0
        assert abs(bore[2] - math.cos(math.radians(30.0))) < 1e-6, bore


class TestLineOfSight:
    """An LOS check that always returns True is worse than none."""

    def test_los_to_a_nearby_point_is_true(self, wire, wire_sim):
        home = wire_sim.home_geo.geo
        p = airsim.GeoPoint()
        p.latitude, p.longitude, p.altitude = (home.latitude + 0.001,
                                               home.longitude, home.altitude)
        assert wire.simTestLineOfSightToPoint(p) is True

    def test_los_beyond_the_horizon_is_false(self, wire, wire_sim):
        home = wire_sim.home_geo.geo
        p = airsim.GeoPoint()
        p.latitude, p.longitude, p.altitude = (home.latitude + 1.0,
                                               home.longitude, home.altitude)
        assert wire.simTestLineOfSightToPoint(p) is False  # ~111 km away

    def test_obstruction_blocks_and_is_named(self, wire, wire_sim):
        home = wire_sim.home_geo.geo
        lat, lon = home.latitude + 0.004, home.longitude
        wire_sim.add_obstruction(home.latitude + 0.002, lon,
                                 home.altitude + 200.0, 150.0, "ridge")
        p = airsim.GeoPoint()
        p.latitude, p.longitude, p.altitude = lat, lon, home.altitude
        assert wire.simTestLineOfSightToPoint(p) is False
        info = wire.client.call("simGetLineOfSightInfo",
                                {"latitude": lat, "longitude": lon,
                                 "altitude": home.altitude}, "")
        assert info["los"] is False
        assert info["first_obstacle"]["name"] == "ridge"
        wire_sim.clear_obstructions()
        assert wire.simTestLineOfSightToPoint(p) is True

    def test_between_points_respects_the_horizon(self, wire, wire_sim):
        home = wire_sim.home_geo.geo
        a = {"latitude": home.latitude, "longitude": home.longitude,
             "altitude": home.altitude + 2000.0}
        b = {"latitude": home.latitude + 0.05, "longitude": home.longitude,
             "altitude": home.altitude}
        assert wire.client.call("simTestLineOfSightBetweenPoints", a, b) is True
        a_low = dict(a, altitude=home.altitude)
        assert wire.client.call("simTestLineOfSightBetweenPoints", a_low,
                                dict(b, latitude=home.latitude + 0.5)) is False


class TestVerticalVelocity:
    """NED +z is DOWN: a climb reports -z, a descent +z.

    PROVEN on the shipped fake before this: `linear_velocity.z_val` was 0.00
    through takeoffs and landings, and whatever the PREVIOUS maneuver had been
    doing after it finished. A full live flight's 1978 fuel rows therefore
    contained ZERO `descend` samples and a 2.94 m/s touchdown was classified
    as a CLIMB by safety.FuelModel.classify_phase, which is the phase model
    the whole fuel integrator is built on (T5).
    """

    @staticmethod
    def _sample(client, fm, label, out, n=3, dt=0.08):
        from godseye_uav.safety import Phase  # noqa: F401  (values compared below)
        for _ in range(n):
            st = client.getMultirotorState()
            vel = st.kinematics_estimated.linear_velocity
            speed = math.sqrt(vel.x_val ** 2 + vel.y_val ** 2 + vel.z_val ** 2)
            out.append({
                "at": label,
                "phase": fm.classify_phase(speed, vel.z_val,
                                           st.landed_state == 0).value,
                "vz": vel.z_val,
                "z": st.kinematics_estimated.position.z_val,
            })
            time.sleep(dt)

    @staticmethod
    def _wait_z(client, predicate, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            z = client.getMultirotorState().kinematics_estimated.position.z_val
            if predicate(z):
                return z
            time.sleep(0.05)
        raise AssertionError("vehicle never reached the commanded altitude")

    def test_a_full_profile_reports_every_vertical_phase_in_order(self, wire):
        from godseye_uav.safety import FuelModel

        fm = FuelModel()
        rows: list[dict] = []
        self._sample(wire, fm, "parked", rows, n=2)

        wire.enableApiControl(True)
        wire.armDisarm(True)
        wire.takeoffAsync()
        time.sleep(0.15)  # one 50 Hz physics tick, as after every command here
        self._sample(wire, fm, "takeoff", rows)

        wire.moveToZAsync(-25.0, 5.0)
        time.sleep(0.15)
        self._sample(wire, fm, "climb", rows)
        self._wait_z(wire, lambda z: z <= -24.0)

        wire.moveToPositionAsync(60.0, 0.0, -25.0, 8.0)
        time.sleep(0.15)
        self._sample(wire, fm, "cruise", rows)

        wire.moveToZAsync(-8.0, 3.0)
        time.sleep(0.15)
        self._sample(wire, fm, "descend", rows)
        self._wait_z(wire, lambda z: z >= -9.0)

        wire.landAsync()
        time.sleep(0.15)
        self._sample(wire, fm, "land", rows)
        end = time.time() + 15.0
        while time.time() < end and wire.getMultirotorState().landed_state != 0:
            time.sleep(0.05)
        self._sample(wire, fm, "touchdown", rows, n=2)

        def at(label):
            return [r for r in rows if r["at"] == label]

        # every commanded phase is the phase the MEASUREMENT implies (T5)
        assert all(r["phase"] == "ground" and r["vz"] == 0.0 for r in at("parked"))
        assert any(r["phase"] == "climb" for r in at("takeoff")), at("takeoff")
        assert all(r["vz"] < -1.0 for r in at("takeoff")), at("takeoff")
        assert all(r["phase"] == "climb" and r["vz"] < -4.0 for r in at("climb"))
        assert all(r["phase"] == "cruise" and abs(r["vz"]) < 0.5 for r in at("cruise"))
        assert all(r["phase"] == "descend" and r["vz"] > 2.0 for r in at("descend"))
        # the touchdown itself: a descent, and never mistaken for a climb
        assert any(r["phase"] == "descend" for r in at("land")), at("land")
        assert all(r["vz"] > 1.0 for r in at("land")), at("land")
        assert not any(r["phase"] == "climb" for r in at("land"))
        assert all(r["phase"] == "ground" and r["vz"] == 0.0 for r in at("touchdown"))

        # ...and they happened in the order a sortie flies them
        order = [r["phase"] for r in rows]
        assert set(order) == {"ground", "climb", "cruise", "descend"}
        assert (order.index("climb") < order.index("cruise")
                < order.index("descend") < len(order) - 1)
        assert order[-1] == "ground"

    def test_a_finished_or_cancelled_maneuver_stops_reporting_its_velocity(self, wire):
        """The stale-velocity half of the bug, on its own.

        `cancelLastTask` marked the task done and the integrator then skipped
        the vehicle entirely, so a cancelled 5 m/s climb kept reporting a
        5 m/s climb for the rest of the flight — including through the landing
        that followed it.
        """
        from godseye_uav.safety import FuelModel

        fm = FuelModel()
        wire.enableApiControl(True)
        wire.armDisarm(True)
        wire.takeoffAsync()
        wire.moveToZAsync(-60.0, 5.0)
        time.sleep(0.4)
        climbing = wire.getMultirotorState().kinematics_estimated.linear_velocity
        assert climbing.z_val < -4.0, "never actually climbed"

        wire.cancelLastTask()
        time.sleep(0.2)
        vel = wire.getMultirotorState().kinematics_estimated.linear_velocity
        assert vel.z_val == 0.0, f"stale climb rate after cancel: {vel.z_val}"
        speed = math.sqrt(vel.x_val ** 2 + vel.y_val ** 2 + vel.z_val ** 2)
        assert fm.classify_phase(speed, vel.z_val, False) is not None
        assert fm.classify_phase(speed, vel.z_val, False).value == "hover"

        # a commanded hover is the same: a held position, not a frozen vector
        wire.hoverAsync()
        time.sleep(0.2)
        held = wire.getMultirotorState().kinematics_estimated.linear_velocity
        assert (held.x_val, held.y_val, held.z_val) == (0.0, 0.0, 0.0)

    def test_a_vertical_climb_keeps_the_airframe_level(self, wire):
        """A multirotor climbs LEVEL; only a flight path pitches it.

        Attitude here is derived from the velocity vector, so the moment a
        pure vertical maneuver started reporting a real climb rate the old
        rule (flight-path angle from the 3D speed) stood the airframe on its
        tail at the 30 deg clamp — pointing the chase-cam and the reported
        boresight at the sky for every takeoff and every let-down.
        """
        wire.enableApiControl(True)
        wire.armDisarm(True)
        wire.takeoffAsync()
        time.sleep(0.5)
        wire.moveToZAsync(-40.0, 5.0)
        time.sleep(0.6)
        st = wire.getMultirotorState()
        assert st.kinematics_estimated.linear_velocity.z_val < -4.0
        pitch, roll, _ = airsim.to_eularian_angles(
            st.kinematics_estimated.orientation)
        assert abs(math.degrees(pitch)) < 5.0, math.degrees(pitch)
        assert abs(math.degrees(roll)) < 5.0

        # ...and a climbing TRANSIT still pitches, because that one is real
        wire.moveToPositionAsync(200.0, 0.0, -80.0, 12.0)
        time.sleep(0.8)
        pitch, _, _ = airsim.to_eularian_angles(
            wire.getMultirotorState().kinematics_estimated.orientation)
        assert math.degrees(pitch) > 5.0, math.degrees(pitch)


class TestWindDriftIsReportedAsVelocity:
    """M15: the velocity the sim reports must be the motion the sim applies.

    That invariant is what the vertical-velocity work above established, and
    the wind path still broke it in the other direction. `_integrate` drifts
    any airborne, armed vehicle at the wind speed and then reported a hard
    (0.00, 0.00, 0.00) for it — measured at 14.9 m/s of ground track against a
    reported 0.00 m/s. Downstream that is not a small error:
    `safety.FuelModel.classify_phase` sees HOVER instead of a moving aircraft,
    `track_deg_from_velocity` returns None, and the M15 headwind penalty the
    fuel model exists to apply is charged as 0.0 for the entire loiter.
    """

    @staticmethod
    def _airborne(wire):
        wire.enableApiControl(True)
        wire.armDisarm(True)
        wire.takeoffAsync()
        wire.moveToZAsync(-60.0, 6.0)
        end = time.time() + 15.0
        while time.time() < end:
            st = wire.getMultirotorState()
            if st.kinematics_estimated.position.z_val <= -55.0:
                return
            time.sleep(0.05)
        raise AssertionError("never reached altitude")

    def test_an_unattended_airborne_vehicle_reports_the_wind_that_moves_it(
            self, wire, wire_sim):
        from godseye_uav.safety import (FuelModel, headwind_component_mps,
                                        track_deg_from_velocity)

        self._airborne(wire)
        wire.cancelLastTask()          # no task: the wind is the only mover
        time.sleep(0.15)
        calm = wire.getMultirotorState().kinematics_estimated.linear_velocity
        assert (calm.x_val, calm.y_val, calm.z_val) == (0.0, 0.0, 0.0), \
            "still air must still report a dead-stop, as before"

        wire_sim.set_wind(15.0, 0.0, 0.0)   # 15 m/s from the south
        time.sleep(0.15)
        p0 = wire.getMultirotorState().kinematics_estimated.position
        t0 = time.monotonic()
        time.sleep(1.0)
        st = wire.getMultirotorState()
        p1 = st.kinematics_estimated.position
        vel = st.kinematics_estimated.linear_velocity
        measured = math.hypot(p1.x_val - p0.x_val,
                              p1.y_val - p0.y_val) / (time.monotonic() - t0)

        # the sim moved it, so the sim reports it moving — and by how much
        assert measured > 10.0, f"the wind never drifted it ({measured:.1f} m/s)"
        assert vel.x_val == pytest.approx(15.0, abs=0.01)
        assert (vel.y_val, vel.z_val) == (0.0, 0.0)
        speed = math.sqrt(vel.x_val ** 2 + vel.y_val ** 2 + vel.z_val ** 2)
        assert speed == pytest.approx(measured, rel=0.15)

        # ...so the fuel model can see it. The track is the whole point: with a
        # hard-zero velocity `track_deg_from_velocity` returned None, and
        # `SafetyMonitor.tick` then resolved EVERY wind to a 0.0 m/s headwind —
        # the M15 coupling had no bearing to work against. Now it does, and it
        # correctly reads a pure tailwind, because a vehicle being blown
        # downwind is by definition going with the air mass.
        track = track_deg_from_velocity(vel.x_val, vel.y_val)
        assert track is not None and track == pytest.approx(0.0, abs=1.0)
        assert headwind_component_mps(15.0, 0.0, track) == pytest.approx(-15.0, abs=0.1)
        # ...and the same resolution charges a real PENALTY on the leg home,
        # which is the number BINGO is priced from (M4/M15).
        assert headwind_component_mps(15.0, 0.0, 180.0) == pytest.approx(15.0, abs=0.1)

        fm = FuelModel()
        # 15 m/s of ground track is not a hover, and it used to be charged as one
        assert fm.classify_phase(speed, vel.z_val,
                                 st.landed_state == 0).value == "cruise"
        wire_sim.set_wind(0.0, 0.0, 0.0)

    def test_a_commanded_hover_in_wind_reports_the_same_drift(self, wire, wire_sim):
        self._airborne(wire)
        wire.hoverAsync()
        time.sleep(0.15)
        still = wire.getMultirotorState().kinematics_estimated.linear_velocity
        assert (still.x_val, still.y_val, still.z_val) == (0.0, 0.0, 0.0)

        wire_sim.set_wind(0.0, -9.0, 0.0)   # 9 m/s toward the west
        time.sleep(0.25)
        vel = wire.getMultirotorState().kinematics_estimated.linear_velocity
        assert vel.y_val == pytest.approx(-9.0, abs=0.01)
        assert (vel.x_val, vel.z_val) == (0.0, 0.0)
        wire_sim.set_wind(0.0, 0.0, 0.0)
        time.sleep(0.15)
        back = wire.getMultirotorState().kinematics_estimated.linear_velocity
        assert (back.x_val, back.y_val, back.z_val) == (0.0, 0.0, 0.0)

    def test_a_landed_or_unarmed_vehicle_is_never_drifted_or_reported_moving(
            self, wire, wire_sim):
        """The drift gate is `airborne AND armed`; `vel` must honour the same
        gate, or a parked airframe would report the weather as ground speed."""
        wire_sim.set_wind(12.0, 12.0, 0.0)
        time.sleep(0.25)
        st = wire.getMultirotorState()
        vel = st.kinematics_estimated.linear_velocity
        assert (vel.x_val, vel.y_val, vel.z_val) == (0.0, 0.0, 0.0)
        assert st.landed_state == 0  # still on the ground
        parked = st.kinematics_estimated.position
        time.sleep(0.25)
        now = wire.getMultirotorState().kinematics_estimated.position
        assert (now.x_val, now.y_val) == (parked.x_val, parked.y_val)

        # airborne but disarmed: neither drifted nor reported moving
        wire_sim.set_wind(0.0, 0.0, 0.0)
        self._airborne(wire)
        wire.cancelLastTask()
        wire_sim.set_wind(12.0, 12.0, 0.0)
        wire.armDisarm(False)
        time.sleep(0.25)
        vel = wire.getMultirotorState().kinematics_estimated.linear_velocity
        assert (vel.x_val, vel.y_val, vel.z_val) == (0.0, 0.0, 0.0)
        wire_sim.set_wind(0.0, 0.0, 0.0)


class TestWindOnACommandedLeg:
    """M15 Phase 6: wind has to reach a vehicle that is being FLOWN.

    The drift work before this covered the vehicle nobody was commanding — an
    idle or hovering aircraft now reports the wind that moves it. A WAYPOINT
    leg still did not: `_integrate` added the wind to POSITION and then moved
    the vehicle at the full commanded speed along the direct vector, reporting
    that commanded vector as `linear_velocity`. Measured on that code, with a
    9 m/s crosswind on a 15 m/s leg due north: the reported velocity was
    (15.0, 0.0, 0.0) while the aircraft was actually making about (15, 9, 0)
    over the ground and swinging tens of metres off the commanded track — the
    same "reported velocity is not the motion" defect, on the one branch that
    matters for a mission.

    A leg is now flown as a wind triangle. `velocity` is the commanded GROUND
    speed — AirSim's world-frame `moveToPositionAsync`, a copter's
    `WPNAV_SPEED` — so the autopilot crabs and holds both the track and the
    ground speed, and what the wind spends is AIRSPEED out of the `MAX_SPEED`
    envelope. Once that envelope is gone the ground speed, and the leg time
    with it, is the wind's to set.
    """

    SPEED = 15.0             # commanded GROUND speed
    CROSSWIND = 9.0          # pure east, on a leg flown due north
    # the airspeed that costs: sqrt(15^2 + 9^2) = 17.5, inside MAX_SPEED (20)
    CRAB_DEG = 30.96         # atan2(9, 15)

    @staticmethod
    def _airborne(wire, z=-60.0):
        """Climb AND settle: a leg commanded while still climbing carries a
        vertical component that muddies every horizontal number below."""
        wire.enableApiControl(True)
        wire.armDisarm(True)
        wire.takeoffAsync()
        wire.moveToZAsync(z, 6.0)
        end = time.time() + 20.0
        while time.time() < end:
            k = wire.getMultirotorState().kinematics_estimated
            if k.position.z_val <= z + 0.5 and k.linear_velocity.z_val == 0.0:
                return
            time.sleep(0.05)
        raise AssertionError("never reached altitude")

    def test_a_crosswind_leg_reports_the_ground_velocity_it_actually_flies(
            self, wire, wire_sim):
        self._airborne(wire)
        wire_sim.set_wind(0.0, self.CROSSWIND, 0.0)
        wire.moveToPositionAsync(900.0, 0.0, -60.0, self.SPEED)
        time.sleep(1.0)  # let the crab settle

        p0 = wire.getMultirotorState().kinematics_estimated.position
        t0 = time.monotonic()
        time.sleep(1.0)
        st = wire.getMultirotorState()
        p1 = st.kinematics_estimated.position
        vel = st.kinematics_estimated.linear_velocity
        dt = time.monotonic() - t0
        moved = ((p1.x_val - p0.x_val) / dt, (p1.y_val - p0.y_val) / dt)
        limited = wire_sim.wind_limited()
        wire_sim.set_wind(0.0, 0.0, 0.0)

        # 1. the invariant: what is reported is what was flown. The old code
        #    reported (15, 0, 0) while actually making about (15, 9, 0).
        assert vel.x_val == pytest.approx(moved[0], abs=0.6)
        assert vel.y_val == pytest.approx(moved[1], abs=0.6)
        # 2. the crab holds the TRACK: no across-track ground velocity
        assert abs(vel.y_val) < 1.0, f"not crabbing: {vel.y_val:.2f} m/s east"
        assert abs(moved[1]) < 1.0, f"blown {moved[1]:.2f} m/s east of track"
        # 3. the commanded GROUND speed survives, because 17.5 m/s of airspeed
        #    still fits inside the envelope — the wind is paid in airspeed here
        assert vel.x_val == pytest.approx(self.SPEED, abs=0.5)
        assert limited is False

    def test_the_commanded_track_is_held_instead_of_being_blown_off_it(
            self, wire, wire_sim):
        """Old code equilibrated ~0.6 * (range to go) off track: 90 m east of
        a 150 m leg commanded due north, and 180 m out on a 300 m one."""
        self._airborne(wire)
        wire_sim.set_wind(0.0, self.CROSSWIND, 0.0)
        wire.moveToPositionAsync(150.0, 0.0, -60.0, self.SPEED)
        worst, end = 0.0, time.time() + 30.0
        p = wire.getMultirotorState().kinematics_estimated.position
        while time.time() < end:
            p = wire.getMultirotorState().kinematics_estimated.position
            worst = max(worst, abs(p.y_val))
            if p.x_val >= 149.0:
                break
            time.sleep(0.05)
        wire_sim.set_wind(0.0, 0.0, 0.0)
        assert p.x_val >= 145.0, f"never flew the leg (north {p.x_val:.1f} m)"
        assert worst < 6.0, f"blown {worst:.1f} m off a track it should hold"

    def test_the_nose_crabs_into_the_wind_while_the_track_stays_put(
            self, wire, wire_sim):
        """The crab angle is a real attitude, not a bookkeeping trick: to make
        15 m/s north over the ground in a 9 m/s easterly, the airframe points
        atan2(9, 15) = 31 deg left of the track and flies 17.5 m/s through the
        air. The old code's nose followed the (wind-free) commanded vector, so
        the crab was 0 until the vehicle had already been blown off track."""
        self._airborne(wire)
        wire_sim.set_wind(0.0, self.CROSSWIND, 0.0)
        wire.moveToPositionAsync(900.0, 0.0, -60.0, self.SPEED)
        time.sleep(2.0)
        st = wire.getMultirotorState()
        _, _, yaw = airsim.to_eularian_angles(st.kinematics_estimated.orientation)
        heading = math.degrees(yaw) % 360.0
        vel = st.kinematics_estimated.linear_velocity
        track = math.degrees(math.atan2(vel.y_val, vel.x_val)) % 360.0
        wire_sim.set_wind(0.0, 0.0, 0.0)

        crab = (track - heading + 540.0) % 360.0 - 180.0
        assert crab == pytest.approx(self.CRAB_DEG, abs=6.0), (
            f"heading {heading:.1f} vs track {track:.1f}")

    def test_a_headwind_past_the_envelope_sets_the_ground_speed_and_leg_time(
            self, wire, wire_sim):
        """This is where wind finally buys leg time, and it is the BINGO case.

        15 m/s of ground speed into a 12 m/s headwind wants 27 m/s of
        airspeed; the airframe has 20. So it flies flat out and makes
        20 - 12 = 8 m/s over the ground — nearly half the commanded speed, and
        the leg takes nearly twice as long. The old code reported the
        commanded 15.0 m/s while actually making 15 - 12 = 3 m/s, so every
        consumer of `linear_velocity` — the fuel phase model included — was
        told the aircraft was cruising when it was barely holding station.
        """
        self._airborne(wire)
        wire_sim.set_wind(-12.0, 0.0, 0.0)   # 12 m/s from the north
        wire.moveToPositionAsync(900.0, 0.0, -60.0, self.SPEED)
        time.sleep(1.0)
        p0 = wire.getMultirotorState().kinematics_estimated.position
        t0 = time.monotonic()
        time.sleep(1.0)
        st = wire.getMultirotorState()
        p1 = st.kinematics_estimated.position
        vel = st.kinematics_estimated.linear_velocity
        dt = time.monotonic() - t0
        north = (p1.x_val - p0.x_val) / dt
        limited = wire_sim.wind_limited()
        wire_sim.set_wind(0.0, 0.0, 0.0)

        assert limited is True, "the envelope ran out and nothing said so"
        assert vel.x_val == pytest.approx(north, abs=0.6)   # the invariant
        assert vel.x_val == pytest.approx(8.0, abs=1.0), "not flying flat out"
        assert vel.x_val < self.SPEED - 3.0

    def test_a_crosswind_beyond_the_envelope_is_flown_and_reported_honestly(
            self, wire, wire_sim):
        """No heading holds the track, so the sim must not pretend one does.

        Old code flew the commanded 18 m/s straight at the waypoint and let
        the full 25 m/s crosswind add on top. Now the whole 20 m/s envelope is
        spent opposing the crosswind, which is all it can do: the across-track
        rate drops to 25 - 20 = 5 m/s and there is no along-track progress at
        all to report.
        """
        self._airborne(wire)
        wire_sim.set_wind(0.0, 25.0, 0.0)
        wire.moveToPositionAsync(900.0, 0.0, -60.0, 18.0)
        time.sleep(0.8)
        p0 = wire.getMultirotorState().kinematics_estimated.position
        t0 = time.monotonic()
        time.sleep(1.0)
        p1 = wire.getMultirotorState().kinematics_estimated.position
        dt = time.monotonic() - t0
        east = (p1.y_val - p0.y_val) / dt
        north = (p1.x_val - p0.x_val) / dt
        limited = wire_sim.wind_limited()
        wire_sim.set_wind(0.0, 0.0, 0.0)

        assert limited is True
        assert east == pytest.approx(5.0, abs=1.5), "the airspeed bought nothing"
        assert abs(north) < 2.0, f"faked {north:.1f} m/s of progress up-track"

    def test_still_air_is_untouched_by_all_of_it(self, wire, wire_sim):
        """The wind triangle degenerates to the old straight leg at W = 0."""
        self._airborne(wire)
        wire.moveToPositionAsync(600.0, 0.0, -60.0, self.SPEED)
        time.sleep(1.0)
        st = wire.getMultirotorState()
        vel = st.kinematics_estimated.linear_velocity
        assert vel.x_val == pytest.approx(self.SPEED, abs=0.1)
        assert vel.y_val == 0.0
        assert abs(vel.z_val) < 0.05  # residual let-down onto the commanded z
        assert wire_sim.wind_limited() is False


class TestStarvedPhysicsTimeIsCountedNotLostSilently:
    """The physics clamp drops sim time; the FUEL clock does not (T5).

    `_integrate` flies `min(now - last_tick, MAX_TICK_DT_S)`. The clamp is
    right — a suspended laptop or a loaded machine must not teleport the
    aircraft — but the remainder it drops is a real decision, and it was
    dropped in silence. `safety.FuelModel` applies the SAME kind of clamp to
    the same wall clock and has always published what it dropped
    (`clamped_ticks`, `unaccounted_s`), because the two halves disagree: the
    aircraft covers only the clamped time while the tank is charged the whole
    wall-clock interval.

    So a starved process under-flies and over-burns, and the sortie silently
    gets shorter legs than the airframe has. On an 8 km RTB priced at the
    cruise rate that is the difference between reaching home and not. This
    test asserts the CONSEQUENCE is measurable, not merely that a counter
    exists: the metres the clamp cost and the seconds it dropped have to
    agree, and they have to be readable from `environment()`.
    """

    def test_the_clamp_publishes_the_sim_time_it_dropped(self):
        from godseye_uav.fake_airsim import MAX_TICK_DT_S, _Task

        sim = _start_sim()
        try:
            env = sim.environment()
            assert env["sim_tick_clamped"] == 0
            assert env["sim_time_lost_s"] == 0.0

            v = sim._vehicles["Drone1"]
            with sim._lock:
                v.ned = NedPoint(0.0, 0.0, -60.0)
                v.armed, v.landed = True, False
                # a leg long enough that nothing else ends it
                v.task = _Task("move_pos", NedPoint(100_000.0, 0.0, -60.0), 10.0)
                sim._last_tick = time.monotonic() - 3.0   # a 3 s stall
                north0 = v.ned.x
            sim._integrate()
            with sim._lock:
                flown = v.ned.x - north0
            env = sim.environment()

            # 1. the stall is counted, and by how much
            assert env["sim_tick_clamped"] == 1
            assert env["sim_time_lost_s"] == pytest.approx(3.0 - MAX_TICK_DT_S,
                                                           abs=0.2)
            # 2. ...and the number means what it says: the vehicle flew only
            #    the clamped slice of a 10 m/s leg, not the 30 m the wall
            #    clock passed. The published loss accounts for the difference.
            assert flown == pytest.approx(10.0 * MAX_TICK_DT_S, abs=0.2)
            missing_m = 10.0 * 3.0 - flown
            assert missing_m == pytest.approx(10.0 * env["sim_time_lost_s"],
                                              rel=0.1), (
                f"{missing_m:.1f} m of leg went missing but the sim published "
                f"{env['sim_time_lost_s']:.2f} s of lost time")
        finally:
            sim.stop()

    def test_an_unstarved_run_never_reports_lost_time(self):
        """The counter must be zero on a healthy loop, or it is noise nobody
        can act on. The clock is started by `start()`, so the gap between
        constructing the sim and starting it is not charged as a stall."""
        sim = _start_sim()
        try:
            time.sleep(0.6)  # ~30 healthy 50 Hz ticks
            env = sim.environment()
            assert env["sim_tick_clamped"] == 0, (
                f"a quiet 50 Hz loop reported {env['sim_tick_clamped']} "
                f"starved ticks ({env['sim_time_lost_s']} s)")
            assert env["sim_time_lost_s"] == 0.0
        finally:
            sim.stop()
