"""Fake AirSim msgpack-rpc server — UE-free CI path (PLAN T8).

Implements the exact wire surface the real AirSim client calls (verified
against airsim/PythonClient/airsim/client.py). A simple point-mass
multirotor model integrates kinematics so flight commands produce plausible
telemetry. Serves on :41451 so the REAL airsim Python client can connect.

Used for: bridge contract tests, MCP tool tests, laptop zero-GPU demo mode.

Fidelity model (what this fake does and does NOT model)
------------------------------------------------------
* **Wire decoding (R1/R2):** msgpack-rpc hands the server raw bytes for every
  string, so wire *dict keys* arrive as bytes. Every incoming argument is
  normalized once at the dispatch boundary (:class:`_WireDispatcher`); handlers
  only ever see str keys. Before this, `pose.get("position", <zeros>)` silently
  took its default and every spawned object landed on the home origin.
* **Kinematics / velocity:** `kinematics_estimated.linear_velocity` is the
  vehicle's GROUND velocity in NED — **+z is DOWN**, so a climb reports a
  NEGATIVE z and a descent a POSITIVE one, matching real AirSim. The
  invariant `_integrate` holds is that *the velocity the sim reports is the
  motion the sim applies*: every branch computes an air-relative velocity,
  adds the wind (M15), reports the sum and integrates exactly that vector.
  Both halves of that were broken in turn — first the maneuver branches
  reported 0.0 (or whatever the PREVIOUS maneuver had been doing), so a
  flight's fuel journal contained zero `descend` samples and a 2.94 m/s
  touchdown classified as a climb; then hard-zeroing them lied the other way,
  reporting (0,0,0) for a vehicle the same tick was drifting 14.9 m/s
  downwind, which `safety.FuelModel.classify_phase` read as a HOVER with no
  track, so `track_deg_from_velocity` returned None and the M15 headwind
  penalty was charged as 0.0 for the whole loiter.
* **Wind on a commanded leg (M15):** a waypoint leg is flown as a wind
  triangle. `velocity` is the commanded GROUND speed (AirSim's world-frame
  `moveToPositionAsync`, a copter's `WPNAV_SPEED`), so the autopilot CRABS —
  the nose points along the AIR vector, which is visible in the reported
  attitude, while the ground track holds the bearing to the waypoint.
  :data:`MAX_SPEED` is the airspeed envelope: wind is free while the required
  airspeed fits inside it, and past that the airframe is flat out and the
  ground speed — hence the leg time — collapses to
  `W.u + sqrt(MAX_SPEED^2 - crosswind^2)`, which is the BINGO case of
  fighting home on nothing. Before this the vehicle was simply blown off the
  commanded track (measured: ~0.6x the range-to-go, ~90 m off a 150 m
  crosswind ingress) while reporting the commanded vector as its velocity.
  When the crosswind alone exceeds the envelope the track cannot be held at
  all; the sim flies the best available air vector and reports the vehicle
  being pushed off track rather than faking progress
  (:meth:`FakeAirSim.wind_limited`).
* **Navigation under GPS degradation (M16/M17):** the degraded fix is the
  vehicle's NAV SOLUTION, not just a corrupted read-out. A commanded position
  — `moveToPosition`, `moveToGPS`, the altitude in `moveToZ` — is interpreted
  in that degraded frame, so the aircraft stops where its ESTIMATE matches
  the waypoint and its TRUTH is off by the navigation error (which, under
  denial, is however far it has flown since the fix froze). Contact
  geolocations in `simGetDetections` inherit the same platform error. With no
  degradation configured the error is exactly zero and nothing moves.
* **Detection noise (M17):** a false positive must be indistinguishable from a
  real contact, or a harness "fusing" the feed is really just reading a label.
  Phantoms are therefore named from the scene's OWN naming pattern (never a
  `phantom_*` giveaway), placed inside the sensor's real range and frustum,
  boxed with the same off-boresight pixel maths as a true contact, AND given
  the same geolocation error; the sim keeps the ground truth out of band in
  :meth:`FakeAirSim.phantom_names`. Real contacts carry a geolocation error
  that grows with slant range and with weather/darkness
  (`set_detection_realism(geo_error_m=...)`), so an exact ground-truth
  position is not itself the tell. The two knobs have to be indistinguishable
  TOGETHER, not only apart: a phantom built from one exact vector had a
  `box2D` and a `relative_pose` that agreed to the pixel rounding while every
  real contact's disagreed by the geolocation error, which re-projected
  through the camera was a perfect classifier. And a phantom NAME is reused
  once the scene's pattern runs out of unused ones, because refusing to emit
  a phantom turned a configured false-positive rate silently into zero
  partway through a sortie. Both knobs are OFF (0.0) until configured, like
  every other noise knob here.
* **Sensor (M7/M18):** cameras carry a per-vehicle gimbal pose + FOV. FOV sets
  the effective detection range (narrow FOV = more pixels per metre = detect
  further) and the reported `box2D` pixel size. A *frustum* gate applies only
  once a camera has been explicitly slewed with `simSetCameraPose`; until then
  the sensor is treated as a slewable ball in field-of-regard mode (anything
  within range is acquirable). No occlusion, no resolution-limited PSF.
* **Sun (M6):** a NOAA low-precision solar model drives azimuth/elevation from
  the sim clock. No atmospheric refraction, no terrain shadowing.
* **Weather (M18):** rain/snow/fog/dust are obscurants that shrink detection
  range and raise the false-negative rate. No per-pixel scattering.
* **LOS:** geometric earth-curvature horizon plus a configurable obstruction
  set (vertical cylinders). There is NO terrain model — see
  :meth:`FakeAirSim.line_of_sight` for exactly what is and is not modelled.
* **Link (M9):** degraded = stale telemetry, lost = telemetry RPCs error.
* **Randomness:** all sim noise draws from an injectable/seedable
  `random.Random` (`FakeAirSim(seed=...)` / :meth:`FakeAirSim.seed`).

ISR only: this module models sensing, never weapons or targeting-for-strike.
"""
from __future__ import annotations

import base64
import functools
import math
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

import msgpack  # noqa: F401  (imported for parity with the real server deps)
from msgpackrpc import Server, Address  # type: ignore

from .geo import GeoPoint, HomeGeoPoint, NedPoint, geodetic_to_ned, ned_to_geodetic

DEFAULT_HOME = GeoPoint(47.641468, -122.140165, 122.0)  # AirSim default origin
GRAVITY = 9.80665
CRUISE_ACCEL = 6.0  # m/s^2 toward target
MAX_SPEED = 20.0
# Vertical rates, as MAGNITUDES. NED sign is applied where they are flown:
# a climb is -z (up), a descent +z (down). See _integrate.
LAND_RATE = 2.0
TAKEOFF_RATE = 2.0
#: Largest dt one physics tick may fly. The 50 Hz loop normally hands over
#: ~0.02 s; anything larger is the PROCESS being starved (a loaded machine, a
#: suspended laptop, a wall-clock jump), and flying it whole would teleport the
#: aircraft. The excess is therefore dropped — but it is COUNTED and published
#: (`environment()["sim_tick_clamped"]` / `["sim_time_lost_s"]`), because the
#: aircraft then covers less ground than the wall clock says while
#: `safety.FuelModel` goes on burning against that same wall clock. A starved
#: run under-flies and over-burns, which is the difference between an RTB that
#: reaches home and one that does not; losing it silently made a loaded
#: machine look like a short-legged airframe. `safety.MAX_TICK_DT_S` is the
#: same clamp on the fuel side and has always counted its own remainder.
MAX_TICK_DT_S = 0.1

IMG_W, IMG_H = 256, 144  # fake frame size (kept stable: bridge + tests rely on it)
DEFAULT_FOV_DEG = 90.0  # AirSim's default camera horizontal FOV
REF_FOV_DEG = 90.0  # FOV at which DETECT_RANGE_M is the plain filter radius
DETECT_RANGE_M = 500.0  # base detection filter radius (clear, day, REF_FOV)
FOV_RANGE_GAIN_LIMITS = (0.5, 4.0)  # clamp on REF_FOV/fov range scaling
NOMINAL_TARGET_SIZE_M = 4.0  # assumed contact size for box2D pixel math
#: Slant range at which `set_detection_realism(geo_error_m=X)` means exactly X
#: metres of 1-sigma geolocation error, in clear daylight. Error scales
#: linearly with slant range beyond it (ranging + boresight-angle error both
#: project further the further out the contact is) and inversely with the
#: sensor conditions `sensor_range_m` already models.
DETECT_GEO_ERROR_REF_M = 300.0
#: Fallback vocabulary for naming a false positive when the scene holds no
#: real object to copy a name from. A phantom drawn from the same words a
#: mission's own targets use is what makes it unsortable from truth; with an
#: EMPTY scene there is nothing to copy, so these plausible scenery nouns are
#: the best available — and the limitation is named here rather than hidden.
PHANTOM_STEMS = ("truck", "pickup", "van", "sedan", "bus", "trailer",
                 "container", "tent", "generator", "skiff", "mast", "shelter")
EARTH_R_M = 6378137.0
LOS_MIN_EYE_M = 1.0  # sensor height floor for the LOS horizon (no terrain model)
DEGRADED_HOLD_S = 2.0  # M9: a degraded link refreshes telemetry this slowly
DEFAULT_SIM_TIME = "2026-06-21 12:00:00"  # local solar time at home => daylight
LINK_STATES = ("nominal", "degraded", "lost")

# WeatherParameter enum (airsim/types.py:70) -> our state key.
WEATHER_PARAMS = {
    0: "rain", 1: "roadwetness", 2: "snow", 3: "roadsnow",
    4: "mapleleaf", 5: "roadleaf", 6: "dust", 7: "fog", 8: "enabled",
}
# Obscurant weights: how strongly each degrades an EO/IR sensor (M18).
OBSCURANT_WEIGHTS = {"fog": 1.0, "dust": 0.8, "snow": 0.6, "rain": 0.4}


@functools.lru_cache(maxsize=64)
def _png_solid(rgb: tuple[int, int, int], w: int = IMG_W, h: int = IMG_H) -> bytes:
    """Minimal solid-color PNG (no PIL dependency)."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # RGB8
    row = b"\x00" + bytes(rgb) * w
    raw = row * h
    idat = zlib.compress(raw, 6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# wire normalization (verified gap R1/R2)
# ---------------------------------------------------------------------------
def _wire(x: Any) -> Any:
    """Recursively decode one msgpack-rpc payload: bytes -> str (R1/R2).

    msgpack-rpc unpacks with ``raw=True``, so *every* string on the wire —
    including dict KEYS — arrives as bytes. Handlers that did
    ``pose.get("position", <default>)`` therefore took the default forever
    (proven: every spawned target landed on the home origin; simSetWind over
    RPC was a no-op). Applied once at the dispatch boundary so no handler can
    reintroduce the bug. Incoming requests carry no binary blobs; a value that
    is not valid UTF-8 is left as bytes rather than corrupted.
    """
    if isinstance(x, (bytes, bytearray)):
        try:
            return bytes(x).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(x)
    if isinstance(x, dict):
        return {_wire(k): _wire(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_wire(v) for v in x]
    return x


class _WireDispatcher:
    """msgpackrpc dispatch boundary that normalizes every incoming arg (R1/R2).

    msgpackrpc resolves handlers with ``hasattr``/``getattr`` on the dispatcher,
    so proxying here covers the whole RPC surface at once — present and future
    handlers alike.
    """

    def __init__(self, handlers: Any) -> None:
        self._handlers = handlers

    def __getattr__(self, name: str) -> Any:
        fn = getattr(self._handlers, name)  # AttributeError -> NoMethodError
        if not callable(fn):
            return fn

        @functools.wraps(fn)
        def call(*args: Any) -> Any:
            return fn(*[_wire(a) for a in args])

        return call


@dataclass
class _Task:
    kind: str  # takeoff|land|move_gps|move_pos|hover|none
    target_ned: NedPoint | None = None
    speed: float = 5.0
    done: bool = False
    cancelled: bool = False


@dataclass
class _Camera:
    """Per-vehicle camera: gimbal pose + FOV (M7 wide->narrow cross-cue)."""

    name: str
    fov_deg: float = DEFAULT_FOV_DEG
    pitch_deg: float = 0.0  # body-relative gimbal, +up (nadir = -90)
    yaw_deg: float = 0.0  # body-relative gimbal, + clockwise from nose
    roll_deg: float = 0.0
    offset: NedPoint = field(default_factory=lambda: NedPoint(0.0, 0.0, 0.0))
    slewed: bool = False  # True once simSetCameraPose was commanded


@dataclass
class _ObjectRoute:
    """M17 Phase 6: a spawned object following waypoints at a speed.

    Waypoints and the running position are held in NED. Integrating in NED
    (rather than geodetic round-tripping every tick) matters: geodetic_to_ned
    and ned_to_geodetic use different earth models, so a per-tick round trip
    accumulates metres of drift per second.
    """

    waypoints: list[NedPoint]
    speed_mps: float
    loop: bool = False
    idx: int = 0
    done: bool = False
    pos: NedPoint | None = None  # None = resync from the object's stored geo


@dataclass
class _LinkState:
    """M9 datalink state with an optional expiry (monotonic seconds)."""

    state: str = "nominal"
    until: float | None = None


@dataclass
class _Obstruction:
    """LOS blocker: a vertical cylinder (no terrain model exists)."""

    name: str
    geo: GeoPoint  # altitude = top of the obstruction (altHae metres)
    radius_m: float


@dataclass
class _Vehicle:
    name: str
    ned: NedPoint = field(default_factory=lambda: NedPoint(0.0, 0.0, 0.0))
    #: GROUND velocity (air-relative velocity + wind). This is what
    #: `linear_velocity` reports and what `_integrate` actually flies.
    vel: NedPoint = field(default_factory=lambda: NedPoint(0.0, 0.0, 0.0))
    #: AIR-relative velocity — what the props are doing. The nose points along
    #: this, which is what makes a crab angle visible in wind.
    air_vel: NedPoint = field(default_factory=lambda: NedPoint(0.0, 0.0, 0.0))
    #: True while the crosswind exceeds the commanded airspeed, so the
    #: commanded ground track cannot be held at all (M15). Read it with
    #: :meth:`FakeAirSim.wind_limited`; the telemetry shows the same thing as
    #: a vehicle visibly being pushed off track.
    wind_limited: bool = False
    armed: bool = False
    api_control: bool = False
    landed: bool = True
    task: _Task = field(default_factory=lambda: _Task("none"))
    collision: bool = False
    # Smoothed attitude (deg) so the cockpit chase-cam banks into turns
    # instead of snapping to the instantaneous velocity vector each tick.
    heading_deg: float = 0.0
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    cameras: dict[str, _Camera] = field(default_factory=dict)


class FakeAirSim:
    """One-msgpack-rpc-server fake for the multirotor surface."""

    def __init__(self, home: GeoPoint = DEFAULT_HOME, port: int = 41451,
                 rng: random.Random | None = None, seed: int | None = None):
        self.home_geo = HomeGeoPoint.from_geo(home)
        self.port = port
        self._vehicles: dict[str, _Vehicle] = {"Drone1": _Vehicle("Drone1")}
        self._objects: dict[str, GeoPoint] = {}
        self._object_routes: dict[str, _ObjectRoute] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._abort = threading.Event()  # set by cancelLastTask; clears on next task
        self._server: Server | None = None
        self._last_tick = time.monotonic()
        # Physics time the MAX_TICK_DT_S clamp dropped, and how often. Read
        # them out of `environment()`; they are the only record that the
        # aircraft flew less than the wall clock the fuel model charged.
        self._tick_clamped = 0
        self._tick_lost_s = 0.0
        # M17: one injectable RNG for every sim noise draw, so a test can seed
        # the whole fake instead of monkeypatching the random module.
        self._rng = rng if rng is not None else random.Random(seed)
        # -- realism (PLAN Phase 6) -------------------------------------
        # M15 wind: steady NED wind vector (m/s) that drifts a flying vehicle.
        self._wind = NedPoint(0.0, 0.0, 0.0)
        # M16 GPS denial: when True, geodetic reads freeze at the last good fix
        # (the vehicle still dead-reckons in NED — position truth keeps moving).
        self._gps_denied = False
        self._last_good_geo: dict[str, GeoPoint] = {}
        # M17 sensor noise: metres of gaussian jitter added to reported GPS.
        self._gps_noise_m = 0.0
        # M17 detection realism: probability of dropping a real contact
        # (false negative) and of inventing a phantom contact (false positive).
        self._det_fn_rate = 0.0
        self._det_fp_rate = 0.0
        # M17 geolocation error: 1-sigma metres at DETECT_GEO_ERROR_REF_M in
        # clear daylight, scaled by slant range and sensor conditions. 0.0 =
        # contacts are reported at exact ground truth (the old behaviour).
        self._det_geo_error_m = 0.0
        # Ground truth for the phantoms minted so far. It lives HERE and never
        # on the wire: the whole point of the naming change is that a consumer
        # of simGetDetections cannot tell a false positive from a real one, so
        # a test that needs to know has to ask the sim, not the contact.
        self._fp_names: set[str] = set()
        # M18 weather: obscurant intensities 0..1 keyed by WEATHER_PARAMS.
        self._weather: dict[str, float] = {k: 0.0 for k in WEATHER_PARAMS.values()}
        self._weather["enabled"] = 0.0
        # M6 sim clock: epoch is LOCAL SOLAR time at the home longitude.
        self._sim_epoch = _parse_datetime(DEFAULT_SIM_TIME)
        self._sim_epoch_at = time.monotonic()
        self._clock_speed = 1.0
        self._tod_enabled = True
        # M9 datalink per vehicle + the stale-telemetry cache a degraded link serves.
        self._links: dict[str, _LinkState] = {}
        self._stale_cache: dict[str, tuple[float, Any]] = {}
        # LOS obstruction set (there is no terrain model — see line_of_sight).
        self._obstructions: list[_Obstruction] = []

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        srv = Server(_WireDispatcher(self._dispatch()))
        self._server = srv
        srv.listen(Address("127.0.0.1", self.port))
        self._stop.clear()
        # The clock starts when the loop does, not when the object was built:
        # the gap between construction and `start()` is not starved physics
        # and must not be charged as lost sim time.
        self._last_tick = time.monotonic()
        self._phys = threading.Thread(target=self._physics_loop, daemon=True)
        self._phys.start()
        self._t = threading.Thread(target=srv.start, daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.stop()
                self._server.close()
            except Exception:
                pass

    def _physics_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.02)  # 50 Hz physics
            self._integrate()

    # -- physics ---------------------------------------------------------
    def _integrate(self) -> None:
        with self._lock:
            now = time.monotonic()
            raw = now - self._last_tick
            dt = min(raw, MAX_TICK_DT_S)
            if raw > MAX_TICK_DT_S:
                # See MAX_TICK_DT_S: the excess is dropped, never silently.
                self._tick_clamped += 1
                self._tick_lost_s += raw - MAX_TICK_DT_S
            self._last_tick = now
            for v in self._vehicles.values():
                self._step_vehicle(v, dt)
            self._advance_objects(dt)

    def _step_vehicle(self, v: _Vehicle, dt: float) -> None:
        """Advance ONE vehicle by `dt`, holding the invariant below.

        THE INVARIANT: `v.vel` is the velocity this tick actually moved the
        vehicle — air-relative velocity plus wind, integrated as one vector.
        Both halves of that have been violated in turn. First the maneuver
        branches reported a stale or zero `vel` while the position moved
        (a 2.94 m/s touchdown read as a CLIMB to
        `safety.FuelModel.classify_phase`). Then the fix for THAT hard-zeroed
        `vel` on the idle branch while the same tick drifted the aircraft
        downwind — measured 14.9 m/s of ground track reported as
        (0.00, 0.00, 0.00), which classifies as a HOVER, returns no track from
        `track_deg_from_velocity`, and so charges a 0.0 m/s headwind for the
        entire loiter: the M15 fuel coupling with nothing to work against.
        Computing one ground velocity per tick and integrating exactly it is
        what makes both of those unrepresentable rather than merely fixed.
        """
        t = v.task
        # M15: the air mass moves over the ground at `wind`, and it carries
        # any AIRBORNE, ARMED vehicle with it — parked or unarmed, nothing.
        drift = (self._wind if (not v.landed and v.armed)
                 else NedPoint(0.0, 0.0, 0.0))
        if t.done or not v.armed:
            # Nothing is being flown, so nothing is moving UNDER POWER — but
            # the wind still is, and `vel` is the one place that motion is
            # reported. On the ground, unarmed, or in still air the drift is
            # exactly zero, so this is the stale-vector fix unchanged.
            # (Attitude is deliberately NOT touched: a parked vehicle holds
            # the heading it stopped on, and a vehicle drifting WITH the air
            # mass has no relative wind to weathervane into.)
            v.air_vel = NedPoint(0.0, 0.0, 0.0)
            v.wind_limited = False
            v.vel = drift
            v.ned = NedPoint(v.ned.x + drift.x * dt,
                             v.ned.y + drift.y * dt,
                             v.ned.z + drift.z * dt)
            return

        air = NedPoint(0.0, 0.0, 0.0)
        v.wind_limited = False
        if t.kind == "takeoff":
            v.landed = False
            # NED: +z is DOWN, so a CLIMB is a NEGATIVE z velocity.
            air = NedPoint(0.0, 0.0, -TAKEOFF_RATE)
        elif t.kind == "land":
            air = NedPoint(0.0, 0.0, LAND_RATE)  # descending = +z
        elif t.kind == "hover":
            # A commanded hover is a real state, not the absence of one: it
            # holds position against the AIR, so its air-relative velocity is
            # zero and its GROUND velocity is the wind.
            air = NedPoint(0.0, 0.0, 0.0)
        elif t.kind in ("move_pos", "move_gps") and t.target_ned is not None:
            air = self._leg_air_velocity(v, t, drift, dt)

        # One ground velocity; report it and fly exactly it.
        v.air_vel = air
        v.vel = NedPoint(air.x + drift.x, air.y + drift.y, air.z + drift.z)
        v.ned = NedPoint(v.ned.x + v.vel.x * dt,
                         v.ned.y + v.vel.y * dt,
                         v.ned.z + v.vel.z * dt)

        if t.kind == "takeoff" and v.ned.z <= -3.0:  # 3 m AGL
            t.done = True
        elif t.kind == "land" and v.ned.z >= 0.0:
            # Touchdown: the only place motion is cut rather than integrated,
            # because the ground is what stops it.
            v.ned = NedPoint(v.ned.x, v.ned.y, 0.0)
            v.landed = True
            t.done = True
            v.air_vel = v.vel = NedPoint(0.0, 0.0, 0.0)
        self._update_attitude(v, dt)

    def _leg_air_velocity(self, v: _Vehicle, t: "_Task", drift: NedPoint,
                          dt: float) -> NedPoint:
        """The AIR-relative velocity that flies this leg's wind triangle (M15).

        `t.speed` is the commanded GROUND speed — that is what
        `moveToPositionAsync`'s `velocity` means for a multirotor here and in
        AirSim (and what `WPNAV_SPEED` means on a real copter): the autopilot
        controls position in the NED frame and treats wind as a disturbance to
        reject. So it CRABS — it points the air vector off the bearing by
        exactly the wind — and the ground velocity stays `speed` along the
        track. :data:`MAX_SPEED` is the other half of that: it is the
        airframe's AIRSPEED envelope, and the wind is only free while
        ``|G*u - W| <= MAX_SPEED``. Past that the airframe is flat out and the
        ground speed collapses to ``W.u + sqrt(MAX_SPEED^2 - |W_perp|^2)`` —
        which is where a headwind finally does lengthen the leg, and it is the
        BINGO case: fighting home with nothing left in the envelope. Returning
        the air vector (rather than moving the vehicle here) keeps the
        single-integration invariant in `_step_vehicle`.

        Before this, a leg flew at the commanded speed along the DIRECT vector
        and the wind was added to position on top of it: the vehicle was blown
        off the commanded track (measured: it equilibrates about 0.6x the
        range-to-go off a crosswind leg, ~90 m out on a 150 m ingress), the
        nose never crabbed, and `linear_velocity` reported the commanded
        vector rather than the motion.
        """
        dx = t.target_ned.x - v.ned.x  # type: ignore[union-attr]
        dy = t.target_ned.y - v.ned.y  # type: ignore[union-attr]
        dz = t.target_ned.z - v.ned.z  # type: ignore[union-attr]
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dist < 0.5:  # arrived: hold against the air, drift with it
            t.done = True
            return NedPoint(0.0, 0.0, 0.0)
        ux, uy, uz = dx / dist, dy / dist, dz / dist
        want = min(t.speed, MAX_SPEED)  # commanded GROUND speed
        w_par = drift.x * ux + drift.y * uy + drift.z * uz
        px = drift.x - w_par * ux
        py = drift.y - w_par * uy
        pz = drift.z - w_par * uz
        cross = math.sqrt(px * px + py * py + pz * pz)
        if cross >= MAX_SPEED:
            # The crosswind alone exceeds the whole airspeed envelope: there
            # is NO heading that holds this track. Fly the air vector straight
            # into the crosswind — the most of it that can be cancelled — and
            # let the telemetry show the vehicle being pushed off track. The
            # alternative (pretending the commanded track is still being
            # flown) is the class of silent lie this file keeps closing.
            v.wind_limited = True
            return NedPoint(-MAX_SPEED * px / cross, -MAX_SPEED * py / cross,
                            -MAX_SPEED * pz / cross)
        # The fastest ground speed this track can be held at, flat out. It can
        # go NEGATIVE: a headwind past the airspeed envelope blows the vehicle
        # backwards down the track, which is what actually happens and what
        # BINGO exists for.
        gmax = w_par + math.sqrt(MAX_SPEED * MAX_SPEED - cross * cross)
        gspeed = min(want, gmax)
        if gspeed < want:
            v.wind_limited = True  # the commanded ground speed is not available
        gx, gy, gz = gspeed * ux, gspeed * uy, gspeed * uz
        if gspeed * dt > dist:  # would overshoot: land exactly on the waypoint
            gx, gy, gz = dx / dt, dy / dt, dz / dt
        return NedPoint(gx - drift.x, gy - drift.y, gz - drift.z)

    def _advance_objects(self, dt: float) -> None:
        """M17 Phase 6: walk routed objects (convoys) along their waypoints.

        Called from the physics tick with ``self._lock`` held. Motion is a
        straight line in the local NED frame at the commanded ground speed —
        no vehicle dynamics, no road network, no terrain following.
        """
        for name, route in list(self._object_routes.items()):
            gp = self._objects.get(name)
            if gp is None or route.done or not route.waypoints:
                continue
            if route.pos is None:  # first tick, or the object was teleported
                route.pos = geodetic_to_ned(gp, self.home_geo.geo)
            cur = route.pos
            tgt = route.waypoints[route.idx]
            dx, dy, dz = tgt.x - cur.x, tgt.y - cur.y, tgt.z - cur.z
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            step = route.speed_mps * dt
            if dist <= max(step, 0.25):
                route.pos = tgt
                route.idx += 1
                if route.idx >= len(route.waypoints):
                    if route.loop:
                        route.idx = 0
                    else:
                        route.idx = len(route.waypoints) - 1
                        route.done = True
            else:
                ux, uy, uz = dx / dist, dy / dist, dz / dist
                route.pos = NedPoint(cur.x + ux * step, cur.y + uy * step,
                                     cur.z + uz * step)
            self._objects[name] = ned_to_geodetic(route.pos, self.home_geo)

    def _update_attitude(self, v: _Vehicle, dt: float) -> None:
        """Derive a smoothed body attitude from the velocity vector.

        Heading follows the AIR-relative velocity — the nose points where the
        props are pushing, not where the ground track goes — so in a crosswind
        the reported heading differs from the track by the crab angle (M15).
        In still air the two vectors are identical and this is unchanged.
        Pitch follows the vertical component and roll banks into turns
        (coordinated-turn proxy) so the cockpit chase-cam reads like a real
        flight sim rather than a fixed camera. Applied to every vehicle each
        tick (hover decays to level).
        """
        vx, vy, vz = v.air_vel.x, v.air_vel.y, v.air_vel.z
        gs = math.hypot(vx, vy)
        if gs > 0.3:
            target_heading = math.degrees(math.atan2(vy, vx)) % 360.0
        else:
            target_heading = v.heading_deg  # hold last heading in a hover
        # Pitch tracks the FLIGHT-PATH angle, which only exists when the
        # vehicle is going somewhere horizontally. A multirotor climbs and
        # lands LEVEL: deriving pitch from a purely vertical velocity would
        # stand a taking-off quad on its tail (atan2 -> 90 deg, clamped to 30)
        # the moment takeoff started reporting a real climb rate.
        target_pitch = -math.degrees(math.atan2(vz, gs)) if gs > 0.3 else 0.0
        # Bank angle from turn rate: phi = atan(omega * v / g), clamped.
        dh = ((target_heading - v.heading_deg + 540.0) % 360.0) - 180.0
        turn_rate = dh / dt if dt > 1e-6 else 0.0  # deg/s
        target_roll = math.degrees(
            math.atan(math.radians(max(-90.0, min(90.0, turn_rate))) * max(gs, 0.0) / 9.81)
        ) if gs > 0.5 else 0.0
        target_roll = max(-35.0, min(35.0, target_roll))
        # First-order smoothing toward targets (~5 Hz attitude response).
        k = min(1.0, dt * 5.0)
        v.heading_deg = (v.heading_deg + dh * k) % 360.0
        v.pitch_deg += (max(-30.0, min(30.0, target_pitch)) - v.pitch_deg) * k
        v.roll_deg += (target_roll - v.roll_deg) * k

    # -- realism helpers (PLAN Phase 6) ---------------------------------
    def seed(self, value: int) -> None:
        """M17: reseed every sim noise draw (detection FN/FP, GPS jitter).

        The phantom-name ledger is reset with it. That ledger is noise STATE,
        not scenario state: `_phantom_name` rejects a name it has already
        minted, so leaving it populated across a reseed changes how many draws
        each name costs and the "same seed, same stream" property quietly
        stops holding — measured as two runs of the same seeded stream
        diverging at the third frame.
        """
        with self._lock:
            self._rng.seed(value)
            self._fp_names.clear()

    def set_wind(self, north: float, east: float, down: float = 0.0) -> None:
        """M15: steady NED wind (m/s)."""
        with self._lock:
            self._wind = NedPoint(north, east, down)

    def wind(self) -> NedPoint:
        """M15: the steady NED wind currently set."""
        return self._wind

    def wind_limited(self, vehicle: str = "Drone1") -> bool:
        """M15 ground truth: is the wind beating the airspeed envelope?

        True while the commanded GROUND VELOCITY is not available to the
        airframe — either the commanded speed cannot be held against the
        along-track wind, or (worse) no heading holds the track at all and the
        vehicle is being pushed off it. See :meth:`_leg_air_velocity`. The
        same fact is visible in telemetry — the reported ground speed is below
        what was commanded, or has a large across-track component and the
        range to the waypoint stops closing — this is just the name for it.
        """
        with self._lock:
            return bool(self._veh(vehicle).wind_limited)

    def set_gps_denied(self, denied: bool) -> None:
        """M16: freeze/unfreeze the reported GPS fix."""
        with self._lock:
            self._gps_denied = bool(denied)

    def set_gps_noise(self, metres: float) -> None:
        """M17: gaussian jitter (m) added to reported GPS."""
        with self._lock:
            self._gps_noise_m = max(0.0, float(metres))

    def set_detection_realism(self, false_neg: float = 0.0, false_pos: float = 0.0,
                              geo_error_m: float | None = None) -> None:
        """M17: per-frame probability of missed (FN) and phantom (FP) contacts.

        `geo_error_m` is the 1-sigma GEOLOCATION error on a REAL contact, in
        metres at :data:`DETECT_GEO_ERROR_REF_M` slant range in clear
        daylight; it grows with slant range and with anything that degrades
        the sensor (weather, darkness). It exists because a feed in which
        every true contact sits on its exact ground-truth coordinate is a feed
        where truth and noise are told apart by a lookup, not by fusion.
        `None` leaves it as it is; 0.0 turns it off, which is the default and
        the historical behaviour.
        """
        with self._lock:
            self._det_fn_rate = min(1.0, max(0.0, float(false_neg)))
            self._det_fp_rate = min(1.0, max(0.0, float(false_pos)))
            if geo_error_m is not None:
                self._det_geo_error_m = max(0.0, float(geo_error_m))

    def phantom_names(self) -> set[str]:
        """Sim GROUND TRUTH: every false-positive name minted so far (M17).

        A phantom is named from the scene's own naming pattern precisely so a
        consumer of `simGetDetections` CANNOT sort truth from noise — which
        means a test or a scoring harness has to get that answer from the sim
        instead of from the contact. This is that channel; nothing here ever
        reaches the wire.
        """
        with self._lock:
            return set(self._fp_names)

    def _detection_geo_sigma(self, dist_m: float, image_type: int = 0) -> float:
        """1-sigma geolocation error (m) for a contact at `dist_m` slant range.

        Linear in slant range past the reference (both the ranging error and
        the boresight-angle error project further the further out the contact
        is) and inversely proportional to the same sensor-condition factor
        `sensor_range_m` already applies, so fog and darkness blur WHERE a
        contact is as well as whether it is seen at all.
        """
        if self._det_geo_error_m <= 0.0:
            return 0.0
        env = self.visibility_factor()
        if int(image_type) != 7:  # EO needs light; thermal does not
            env *= self.light_factor()
        span = max(dist_m, DETECT_GEO_ERROR_REF_M) / DETECT_GEO_ERROR_REF_M
        return self._det_geo_error_m * span / max(0.05, env)

    def _geo_error_draw(self, dist_m: float,
                        image_type: int = 0) -> tuple[float, float, float]:
        """One NED geolocation-error sample (m) for a contact at `dist_m`.

        Draws nothing at all when the knob is off, so a seeded RNG stream is
        byte-identical to the stream before this existed.
        """
        sigma = self._detection_geo_sigma(dist_m, image_type)
        if sigma <= 0.0:
            return (0.0, 0.0, 0.0)
        return (self._rng.gauss(0.0, sigma), self._rng.gauss(0.0, sigma),
                self._rng.gauss(0.0, sigma * 0.5))

    def _name_shape(self, name: str) -> tuple[str, str | None, int]:
        """Split a scene name into (stem, separator, pad-width).

        `("truck", "_", 0)` for `truck_1` — a plain integer index, so any
        integer matches its shape. `("truck", "_", 2)` for `truck_07`, where
        the leading zero says the index is zero-padded to a fixed width.
        `("bridge", None, 0)` for a name carrying no index at all. The shape
        is what a phantom copies.
        """
        i = len(name)
        while i > 0 and name[i - 1].isdigit():
            i -= 1
        if i == len(name) or i == 0:
            return (name, None, 0)
        digits = name[i:]
        sep = ""
        if name[i - 1] in "_-.":
            sep = name[i - 1]
            i -= 1
        return (name[:i], sep, len(digits) if digits.startswith("0") else 0)

    def _phantom_name(self) -> str:
        """Mint a false-positive name that copies the scene's own pattern.

        A phantom called `phantom_7` is not a false positive, it is a labelled
        one: any consumer sorts it out with `startswith`, so the FP rate never
        tested whether anything downstream could actually handle a spurious
        contact. The name is therefore built from a REAL object's stem and
        index shape wherever the scene has one — `truck_1, truck_2` gets a
        `truck_9`, and nothing about the string separates them.

        Limits, stated rather than hidden: with an EMPTY scene there is no
        pattern to copy and a name comes from :data:`PHANTOM_STEMS`; and a
        scene whose names carry no index (`bridge`, `depot`) can only be
        matched in SHAPE, not vocabulary, since reusing the one word present
        would collide with the real object.
        """
        # Names that may NEVER be worn by a phantom: a real scene object would
        # make the contact a duplicate of a true one, and a vehicle is not
        # scenery. Our OWN past names are merely preferred-against — see below.
        real = set(self._objects) | set(self._vehicles)
        taken = real | self._fp_names
        shapes = [self._name_shape(n) for n in self._objects]
        indexed = [s for s in shapes if s[1] is not None]
        for _ in range(200):
            if indexed:
                stem, sep, pad = indexed[self._rng.randrange(len(indexed))]
                n = self._rng.randrange(1, 10 ** pad if pad else 1000)
                cand = f"{stem}{sep}{n:0{pad}d}" if pad else f"{stem}{sep}{n}"
            else:
                stem = PHANTOM_STEMS[self._rng.randrange(len(PHANTOM_STEMS))]
                cand = (stem if shapes
                        else f"{stem}_{self._rng.randrange(1, 100)}")
            if cand not in taken:
                self._fp_names.add(cand)
                return cand
        # Every name this pattern can make is already spoken for. REUSING one
        # of our own is the honest outcome; returning "" was not.
        #
        # The ledger only ever grew, and a plain `truck_N` pattern is 999
        # names wide, so a scene of `truck_1..truck_3` ran out after 996
        # phantoms — MEASURED: with `det_false_pos=1.0`, frames 0-995 each
        # carried a phantom and every frame after that carried NONE, forever,
        # while `environment()` went on publishing `det_false_pos: 1.0`. At a
        # 10 Hz detection poll that is a false-positive rate that switches
        # itself off 100 seconds into a sortie without saying so — a
        # configured knob reporting itself as on while doing nothing, which is
        # the failure shape this whole file exists to keep closing.
        #
        # A repeat name gives nothing away: it is already indistinguishable
        # from a real one, it is already in `phantom_names()`, and a spurious
        # contact that RECURS is more like a real false track than one that
        # never appears twice. Sorted, because set iteration order depends on
        # per-process string hash randomization and the draw has to be
        # reproducible from a seed.
        reusable = sorted(self._fp_names - real)
        if reusable:
            return reusable[self._rng.randrange(len(reusable))]
        # Nothing has ever been minted and nothing can be: emit no phantom.
        return ""

    def _phantom_contact(self, v: _Vehicle, cam: _Camera, max_range: float,
                         nav: NedPoint, image_type: int = 0) -> dict | None:
        """A false-positive contact, or None if one could not be placed (M17).

        Placed the way a real one would be seen: inside the sensor's effective
        range, past the same frustum gate, boxed with the same off-boresight
        pixel maths, and — the part that has to track every change made to the
        REAL branch — carrying the same geolocation error. Returns None rather
        than emitting a contact that breaks any of those, because an FP the
        sensor model itself could not have produced is another way of
        labelling it.

        THE GEOLOCATION ERROR IS NOT OPTIONAL HERE. `geo_error_m` moves a real
        contact's `relative_pose` off the bearing its `box2D` is drawn from
        (the pixels are where the contact really is; the range-and-pointing
        solution behind them is not). A phantom built from one exact vector
        has `box2D` and `relative_pose` agreeing to the rounding of the pixel
        box, so re-projecting `relative_pose` through the camera and
        differencing it against the box centre was a PERFECT classifier the
        moment both knobs were on together: measured over 80 frames with
        `geo_error_m=25` and a slewed ball, 45 of 45 phantoms flagged and 0 of
        237 real contacts misflagged, the worst phantom residual 0.023 deg
        against a best real residual of 0.696 deg. The name stopped labelling
        a phantom and the geometry started. Drawing the error here from the
        same `_geo_error_draw` closes it — and draws nothing at all when the
        knob is off, so a seeded stream is unchanged.

        One tell remains and is named rather than papered over: a phantom is
        always at ground level. Every target this sim spawns is too, so it
        separates nothing today — but a scene holding an AIRBORNE object would
        make altitude a discriminator again.
        """
        rng = self._rng
        for _ in range(8):
            bearing = rng.uniform(0.0, 2.0 * math.pi)
            # sqrt() so the draw is uniform over the AREA the sensor covers,
            # not bunched toward the vehicle.
            reach = max_range * math.sqrt(rng.uniform(0.01, 1.0))
            pn = v.ned.x + reach * math.cos(bearing)
            pe = v.ned.y + reach * math.sin(bearing)
            dx, dy, dz = pn - v.ned.x, pe - v.ned.y, -v.ned.z  # ground level
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            if dist > max_range:
                continue
            off = _frame_offset(v, cam, dx, dy, dz)
            if cam.slewed and off is None:
                continue
            name = self._phantom_name()
            if not name:
                return None
            # Same error, same order of operations as the real branch: it
            # lands on the MEASURED relative vector, and the absolute position
            # is derived from that vector, so the two stay consistent with
            # each other and inconsistent with `box2D` by exactly as much as a
            # real contact's are.
            ex, ey, ez = self._geo_error_draw(dist, image_type)
            rel = NedPoint(dx + ex, dy + ey, dz + ez)
            pgp = ned_to_geodetic(
                NedPoint(v.ned.x + nav.x + rel.x, v.ned.y + nav.y + rel.y,
                         v.ned.z + nav.z + rel.z), self.home_geo)
            return {
                "name": name,
                "geo_point": {"latitude": pgp.latitude,
                              "longitude": pgp.longitude,
                              "altitude": pgp.altitude},
                "box2D": _box2d(cam.fov_deg, dist, off),
                "box3D": {"min": _v3(NedPoint(-2, -2, -2)),
                          "max": _v3(NedPoint(2, 2, 2))},
                "relative_pose": {
                    "position": _v3(rel),
                    "orientation": _q_identity(),
                },
            }
        return None

    def _nav_error_ned(self, v: _Vehicle) -> NedPoint:
        """The vehicle's NAVIGATION error: (where it thinks it is) - (truth).

        Degrading GPS only on the read-out makes a GPS-denied aircraft fly
        *perfectly* while reporting a frozen fix, which is the opposite of
        what denial does. The degraded fix IS the nav solution, so this offset
        is what every commanded position is interpreted through
        (:meth:`_nav_target`) and what every geolocated contact inherits.
        Exactly zero unless denial or noise is configured.
        """
        if not self._gps_denied and self._gps_noise_m <= 0.0:
            return NedPoint(0.0, 0.0, 0.0)
        rep = geodetic_to_ned(self._reported_geo(v), self.home_geo.geo)
        return NedPoint(rep.x - v.ned.x, rep.y - v.ned.y, rep.z - v.ned.z)

    def _nav_target(self, v: _Vehicle, wanted: NedPoint) -> NedPoint:
        """Commanded position (in the vehicle's ESTIMATE frame) -> true NED.

        The autopilot flies until its own estimate reads `wanted`, and its
        estimate is truth + nav error, so it comes to rest that error short of
        the real point. Captured once, when the command is accepted.
        """
        err = self._nav_error_ned(v)
        return NedPoint(wanted.x - err.x, wanted.y - err.y, wanted.z - err.z)

    # -- M18 weather -----------------------------------------------------
    def set_weather(self, rain: float | None = None, snow: float | None = None,
                    fog: float | None = None, dust: float | None = None,
                    enabled: bool | None = None) -> dict:
        """M18: set obscurant intensities (0..1). Returns the new weather state.

        Obscurants shrink the effective detection range and raise the
        false-negative rate; they do not alter flight dynamics here (the fuel
        multiplier lives in safety.FuelModel).
        """
        with self._lock:
            for key, val in (("rain", rain), ("snow", snow),
                             ("fog", fog), ("dust", dust)):
                if val is not None:
                    self._weather[key] = min(1.0, max(0.0, float(val)))
            if enabled is not None:
                self._weather["enabled"] = 1.0 if enabled else 0.0
            elif any(self._weather[k] > 0.0 for k in OBSCURANT_WEIGHTS):
                self._weather["enabled"] = 1.0
        return self.weather()

    def weather(self) -> dict:
        """M18: current weather + the derived visibility factor (0.05..1.0)."""
        w = dict(self._weather)
        w["obscurant"] = self._obscurant()
        w["visibility_factor"] = self.visibility_factor()
        return w

    def _obscurant(self) -> float:
        """Combined obscurant load 0..1 from the weighted intensities."""
        if not self._weather.get("enabled"):
            return 0.0
        return min(1.0, sum(self._weather.get(k, 0.0) * wt
                            for k, wt in OBSCURANT_WEIGHTS.items()))

    def visibility_factor(self) -> float:
        """M18: sensor range multiplier from weather (1.0 clear, 0.05 worst)."""
        return max(0.05, 1.0 - 0.9 * self._obscurant())

    # -- M6 time of day / sun --------------------------------------------
    def set_time(self, datetime_str: str | None = None,
                 clock_speed: float | None = None,
                 enabled: bool | None = None) -> datetime:
        """M6: set the sim clock. ``datetime_str`` is LOCAL SOLAR time at home.

        Interpreting it as local solar time (rather than UTC) means "12:00:00"
        is midday in every theater, which is what sun-side orbit planning
        actually wants. ``clock_speed`` is AirSim's celestial clock multiplier
        (0.0 freezes the sun). Returns the new sim time.
        """
        with self._lock:
            if datetime_str:
                self._sim_epoch = _parse_datetime(datetime_str)
            if clock_speed is not None:
                self._clock_speed = max(0.0, float(clock_speed))
            if enabled is not None:
                self._tod_enabled = bool(enabled)
                if not enabled:
                    self._sim_epoch = _parse_datetime(DEFAULT_SIM_TIME)
            self._sim_epoch_at = time.monotonic()
            return self._sim_epoch

    def sim_time(self) -> datetime:
        """M6: current sim datetime (local solar time at home), clock-advanced."""
        elapsed = (time.monotonic() - self._sim_epoch_at) * self._clock_speed
        return self._sim_epoch + timedelta(seconds=elapsed)

    def sun_position(self, lat: float | None = None,
                     lon: float | None = None) -> tuple[float, float]:
        """M6: (azimuth_deg from true north, elevation_deg) of the sun.

        NOAA low-precision solar equations (~0.1 deg). No refraction, no
        parallax, no terrain shadowing.
        """
        lat = self.home_geo.geo.latitude if lat is None else lat
        lon = self.home_geo.geo.longitude if lon is None else lon
        # The clock is local solar time at HOME; convert to UTC for the model.
        utc = self.sim_time() - timedelta(hours=self.home_geo.geo.longitude / 15.0)
        return _solar_position(utc, lat, lon)

    def light_factor(self, lat: float | None = None,
                     lon: float | None = None) -> float:
        """M6: EO illumination multiplier, 1.0 in daylight down to 0.2 at night.

        Ramps linearly between civil twilight (-6 deg) and 10 deg elevation.
        Thermal/IR sensors ignore this (see :meth:`sensor_range_m`).
        """
        _, elev = self.sun_position(lat, lon)
        if elev >= 10.0:
            return 1.0
        if elev <= -6.0:
            return 0.2
        return 0.2 + 0.8 * (elev + 6.0) / 16.0

    # -- M7 camera gimbal + FOV -------------------------------------------
    def camera(self, vehicle: str = "Drone1", camera: str = "0") -> _Camera:
        """Per-vehicle camera state (created on first reference)."""
        v = self._veh(vehicle)
        return v.cameras.setdefault(str(camera), _Camera(str(camera)))

    def set_camera_fov(self, vehicle: str, camera: str, fov_deg: float) -> _Camera:
        """M7: set a camera's horizontal FOV (narrow = the identify cue)."""
        cam = self.camera(vehicle, camera)
        cam.fov_deg = min(170.0, max(1.0, float(fov_deg)))
        return cam

    def set_camera_pose(self, vehicle: str, camera: str, pitch_deg: float = 0.0,
                        yaw_deg: float = 0.0, roll_deg: float = 0.0,
                        offset: NedPoint | None = None) -> _Camera:
        """M7/gimbal: slew a camera. Angles are body-relative (nadir = -90 pitch).

        Slewing a camera switches it out of field-of-regard mode: from then on
        contacts must fall inside the frustum to be detected.
        """
        cam = self.camera(vehicle, camera)
        cam.pitch_deg = float(pitch_deg)
        cam.yaw_deg = float(yaw_deg)
        cam.roll_deg = float(roll_deg)
        if offset is not None:
            cam.offset = offset
        cam.slewed = True
        return cam

    def sensor_range_m(self, vehicle: str = "Drone1", camera: str = "0",
                       image_type: int = 0) -> float:
        """Effective detection range: base * FOV gain * weather * illumination.

        Narrowing the FOV puts more pixels on each metre of target, so the
        sensor resolves contacts further out (M7); weather obscurants and
        darkness pull the range back in (M18/M6). ImageType 7 (Infrared) is
        thermal and ignores illumination.
        """
        cam = self.camera(vehicle, camera)
        lo, hi = FOV_RANGE_GAIN_LIMITS
        fov_gain = min(hi, max(lo, REF_FOV_DEG / max(1.0, cam.fov_deg)))
        env = self.visibility_factor()
        if int(image_type) != 7:  # EO needs light; thermal does not
            env *= self.light_factor()
        return DETECT_RANGE_M * fov_gain * env

    # -- M9 datalink -------------------------------------------------------
    def set_link_state(self, vehicle: str, state: str = "nominal",
                       duration_s: float = 0.0) -> dict:
        """M9: force the datalink to degraded|lost for ``duration_s`` (0 = until cleared).

        degraded = telemetry goes stale (refreshes every DEGRADED_HOLD_S);
        lost = telemetry RPCs raise, which is what a lost-link plan must react to.
        """
        st = str(state).lower()
        if st not in LINK_STATES:
            raise ValueError(f"link state must be one of {LINK_STATES}, got {state!r}")
        until = time.monotonic() + float(duration_s) if duration_s and duration_s > 0 else None
        with self._lock:
            self._links[vehicle or "Drone1"] = _LinkState(st, until)
        return self.link_state(vehicle)

    def link_state(self, vehicle: str = "Drone1") -> dict:
        """M9: {state, remaining_s} for a vehicle's datalink (auto-recovers)."""
        name = vehicle or "Drone1"
        link = self._links.get(name)
        if link is None:
            return {"vehicle": name, "state": "nominal", "remaining_s": 0.0}
        if link.until is not None and time.monotonic() >= link.until:
            self._links.pop(name, None)
            return {"vehicle": name, "state": "nominal", "remaining_s": 0.0}
        remaining = 0.0 if link.until is None else max(0.0, link.until - time.monotonic())
        return {"vehicle": name, "state": link.state, "remaining_s": round(remaining, 2)}

    def _link_gate(self, vehicle: str) -> str:
        return str(self.link_state(vehicle)["state"])

    def _stale(self, key: str, build: Callable[[], Any]) -> Any:
        """M9 degraded link: serve the cached payload until it ages out."""
        now = time.monotonic()
        hit = self._stale_cache.get(key)
        if hit is not None and now - hit[0] < DEGRADED_HOLD_S:
            return hit[1]
        val = build()
        self._stale_cache[key] = (now, val)
        return val

    # -- line of sight -----------------------------------------------------
    def add_obstruction(self, lat: float, lon: float, height_m: float,
                        radius_m: float = 50.0, name: str = "") -> _Obstruction:
        """Add a LOS blocker: a vertical cylinder of ``height_m`` altHae."""
        ob = _Obstruction(name or f"obstruction_{len(self._obstructions) + 1}",
                          GeoPoint(lat, lon, float(height_m)), float(radius_m))
        with self._lock:
            self._obstructions.append(ob)
        return ob

    def clear_obstructions(self) -> None:
        """Remove every LOS blocker."""
        with self._lock:
            self._obstructions.clear()

    def obstructions(self) -> list[dict]:
        """Current LOS blockers as plain dicts."""
        return [{"name": o.name, "lat": o.geo.latitude, "lon": o.geo.longitude,
                 "height_m": o.geo.altitude, "radius_m": o.radius_m}
                for o in self._obstructions]

    def line_of_sight(self, observer: GeoPoint,
                      target: GeoPoint) -> tuple[bool, dict | None]:
        """Honest LOS test -> (has_los, first_obstacle).

        MODELS: (a) the geometric earth-curvature horizon, with both endpoint
        heights taken above the home altitude datum and no atmospheric
        refraction (k = 1.0, i.e. deliberately conservative — a real 4/3-earth
        radio horizon is ~15% further); (b) an explicit obstruction set of
        vertical cylinders added with :meth:`add_obstruction`, blocking when
        the sight line passes inside the cylinder below its top.

        DOES NOT MODEL: terrain (there is no DEM in the UE-free path, so flat
        ground at the home altitude is assumed), vegetation, buildings that
        were not added as obstructions, atmospheric attenuation, or the
        vehicle's own airframe. A True result therefore means "not blocked by
        the horizon or by any declared obstruction", never "verified clear".
        Endpoints below LOS_MIN_EYE_M of the datum are raised to it, so a
        landed vehicle has a sensor height rather than a zero-length horizon.
        """
        datum = self.home_geo.geo.altitude
        h1 = max(LOS_MIN_EYE_M, observer.altitude - datum)
        h2 = max(LOS_MIN_EYE_M, target.altitude - datum)
        a = geodetic_to_ned(observer, self.home_geo.geo)
        b = geodetic_to_ned(target, self.home_geo.geo)
        ground = math.hypot(b.x - a.x, b.y - a.y)
        horizon = math.sqrt(2.0 * EARTH_R_M * h1) + math.sqrt(2.0 * EARTH_R_M * h2)
        if ground > horizon:
            return False, {"type": "horizon", "name": "earth_curvature",
                           "range_m": round(horizon, 1),
                           "ground_range_m": round(ground, 1)}
        for ob in list(self._obstructions):
            c = geodetic_to_ned(ob.geo, self.home_geo.geo)
            # closest approach of segment a->b to the cylinder axis (2D)
            vx, vy = b.x - a.x, b.y - a.y
            seg2 = vx * vx + vy * vy
            t = 0.0 if seg2 <= 1e-9 else ((c.x - a.x) * vx + (c.y - a.y) * vy) / seg2
            t = max(0.0, min(1.0, t))
            px, py = a.x + vx * t, a.y + vy * t
            if math.hypot(c.x - px, c.y - py) > ob.radius_m:
                continue
            ray_alt = observer.altitude + (target.altitude - observer.altitude) * t
            if ray_alt < ob.geo.altitude:
                return False, {"type": "obstruction", "name": ob.name,
                               "lat": ob.geo.latitude, "lon": ob.geo.longitude,
                               "height_m": ob.geo.altitude,
                               "ground_range_m": round(ground * t, 1)}
        return True, None

    # -- objects / convoys --------------------------------------------------
    def set_object_route(self, name: str, waypoints: list, speed_mps: float = 8.0,
                         loop: bool = False) -> _ObjectRoute:
        """M17 Phase 6: drive a spawned object along ``waypoints`` at a speed.

        Waypoints are GeoPoint or (lat, lon, alt) tuples / {latitude,...} dicts.
        The physics loop advances the object, so detections of it carry real
        motion and Track.update can derive speed/heading.
        """
        if name not in self._objects:
            raise KeyError(f"no spawned object named {name!r}")
        pts = [geodetic_to_ned(_as_geopoint(w, self.home_geo.geo.altitude),
                               self.home_geo.geo) for w in waypoints]
        route = _ObjectRoute(pts, max(0.0, float(speed_mps)), bool(loop))
        with self._lock:
            self._object_routes[name] = route
        return route

    def object_route(self, name: str) -> dict | None:
        """Route state for a moving object, or None if it is static."""
        r = self._object_routes.get(name)
        if r is None:
            return None
        return {"name": name, "speed_mps": r.speed_mps, "loop": r.loop,
                "waypoint_index": r.idx, "waypoints": len(r.waypoints),
                "done": r.done}

    def objects(self) -> dict[str, GeoPoint]:
        """Spawned scenario objects (targets), keyed by name."""
        return dict(self._objects)

    def _resync_route(self, name: str) -> None:
        """A teleport supersedes a route's running NED position."""
        route = self._object_routes.get(name)
        if route is not None:
            route.pos = None

    def environment(self) -> dict:
        """One-call environment summary (INTREP 'sensor conditions', PLAN 4.7)."""
        az, el = self.sun_position()
        return {
            "sim_time": self.sim_time().strftime("%Y-%m-%d %H:%M:%S"),
            "clock_speed": self._clock_speed,
            "sun_azimuth_deg": round(az, 2),
            "sun_elevation_deg": round(el, 2),
            "is_day": el > 0.0,
            "light_factor": round(self.light_factor(), 3),
            "weather": self.weather(),
            "wind": {"north": self._wind.x, "east": self._wind.y, "down": self._wind.z},
            "gps_denied": self._gps_denied,
            "gps_noise_m": self._gps_noise_m,
            "detection_range_m": round(self.sensor_range_m(), 1),
            # Every noise knob published, so "no noise" is a stated condition
            # rather than something a consumer has to infer from clean data.
            "det_false_neg": self._det_fn_rate,
            "det_false_pos": self._det_fp_rate,
            "det_geo_error_m": self._det_geo_error_m,
            "det_geo_error_sigma_m": round(
                self._detection_geo_sigma(self.sensor_range_m()), 2),
            # Physics time the MAX_TICK_DT_S clamp dropped. Non-zero means the
            # process was starved and every vehicle flew LESS than the wall
            # clock `safety.FuelModel` burned against — so a sortie flown here
            # is short-legged by roughly this many seconds of cruise. It is
            # published rather than merely clamped because a fuel clock that
            # outruns the airframe is exactly how an RTB fails to reach home.
            "sim_tick_clamped": self._tick_clamped,
            "sim_time_lost_s": round(self._tick_lost_s, 3),
        }

    def _reported_geo(self, v: _Vehicle) -> GeoPoint:
        """True NED -> reported geodetic with denial (M16) + noise (M17)."""
        true_gp = ned_to_geodetic(v.ned, self.home_geo)
        if self._gps_denied:
            # GPS fix frozen: return the last good fix captured before denial.
            return self._last_good_geo.get(v.name, true_gp)
        self._last_good_geo[v.name] = true_gp
        if self._gps_noise_m > 0.0:
            m_lat = 111320.0
            m_lon = 111320.0 * math.cos(math.radians(true_gp.latitude))
            true_gp = GeoPoint(
                true_gp.latitude + self._rng.gauss(0, self._gps_noise_m) / m_lat,
                true_gp.longitude + self._rng.gauss(0, self._gps_noise_m) / m_lon,
                true_gp.altitude + self._rng.gauss(0, self._gps_noise_m * 0.5),
            )
        return true_gp

    def _true_geo(self, v: _Vehicle) -> GeoPoint:
        """Ground-truth geodetic position (never GPS-denied or jittered)."""
        return ned_to_geodetic(v.ned, self.home_geo)

    # -- RPC dispatch ----------------------------------------------------
    def _dispatch(self):
        d = self

        class H:  # handlers; names become the wire methods
            # NOTE: every argument reaching these handlers has already been
            # bytes-decoded by _WireDispatcher (R1/R2) — read str keys freely.

            # session / admin
            def ping(self):
                return True

            def getServerVersion(self):
                return 1  # int, matching real AirSim ServerVersion

            def getMinRequiredServerVersion(self):
                return 1

            def getMinRequiredClientVersion(self):
                return 1

            def reset(self):
                with d._lock:
                    for v in d._vehicles.values():
                        v.ned = NedPoint(0, 0, 0)
                        v.vel = NedPoint(0, 0, 0)
                        v.armed = False
                        v.landed = True
                        v.task = _Task("none")
                    d._links.clear()
                    d._stale_cache.clear()

            def getSettingsString(self):
                return '{"SettingsVersion": 2, "SimMode": "Multirotor", "fake": true}'

            def enableApiControl(self, enable: bool, vehicle_name: str = ""):
                v = d._veh(vehicle_name)
                v.api_control = bool(enable)

            def isApiControlEnabled(self, vehicle_name: str = ""):
                return d._veh(vehicle_name).api_control

            def armDisarm(self, arm: bool, vehicle_name: str = ""):
                v = d._veh(vehicle_name)
                v.armed = bool(arm)
                return v.armed

            def getHomeGeoPoint(self, vehicle_name: str = ""):
                g = d.home_geo.geo
                return {
                    "latitude": g.latitude,
                    "longitude": g.longitude,
                    "altitude": g.altitude,
                }

            def listVehicles(self):
                # Vehicles only: spawned scenario objects are NOT vehicles, and
                # returning them here minted phantom drones on the globe.
                # Filtering on _objects (not just refusing in _veh) closes the
                # ordering hole: reading a name as a vehicle BEFORE it was
                # spawned as an object left the phantom in _vehicles forever.
                return [n for n in d._vehicles if n not in d._objects]

            # flight — handlers block until the maneuver completes, matching
            # real AirSim where call_async(...).join() waits for completion.
            def takeoff(self, timeout_sec: float = 20.0, vehicle_name: str = ""):
                v = d._veh(vehicle_name)
                v.landed = False  # airborne the moment takeoff is commanded
                d._start_task(v, _Task("takeoff"))
                return True

            def land(self, timeout_sec: float = 60.0, vehicle_name: str = ""):
                v = d._veh(vehicle_name)
                d._start_task(v, _Task("land"))
                return True

            def hover(self, vehicle_name: str = ""):
                d._veh(vehicle_name).task = _Task("hover", done=False)
                return True

            def moveToPosition(self, x, y, z, velocity, timeout_sec=3e38,
                               drivetrain=0, yaw_mode=None, lookahead=-1,
                               adaptive_lookahead=1, vehicle_name=""):
                v = d._veh(vehicle_name)
                # M16/M17: a commanded position is in the vehicle's own NAV
                # frame, and under GPS degradation that frame is offset from
                # truth. See _nav_target.
                d._start_task(v, _Task(
                    "move_pos", d._nav_target(v, NedPoint(x, y, z)),
                    float(velocity)
                ))
                return True

            def moveToGPS(self, latitude, longitude, altitude, velocity,
                          timeout_sec=3e38, drivetrain=0, yaw_mode=None,
                          lookahead=-1, adaptive_lookahead=1, vehicle_name=""):
                # AirSim moveToGPSAsync: altitude is MSL; home alt is the
                # reference. Convert target to NED about home.
                target_geo = GeoPoint(latitude, longitude, altitude)
                ned = geodetic_to_ned(target_geo, d.home_geo.geo)
                v = d._veh(vehicle_name)
                d._start_task(v, _Task("move_gps", d._nav_target(v, ned),
                                       float(velocity)))
                return True

            def moveToZ(self, z, velocity, timeout_sec=3e38, yaw_mode=None,
                        lookahead=-1, adaptive_lookahead=1, vehicle_name=""):
                v = d._veh(vehicle_name)
                # Only the ALTITUDE is commanded here, so only the altitude
                # goes through the nav frame: the horizontal target is "where
                # I am", which is the same point in either frame. Passing the
                # truth x/y through _nav_target would have shifted the vehicle
                # sideways on a pure climb command.
                tgt = d._nav_target(v, NedPoint(v.ned.x, v.ned.y, z))
                d._start_task(v, _Task(
                    "move_pos", NedPoint(v.ned.x, v.ned.y, tgt.z),
                    float(velocity)))
                return True

            def cancelLastTask(self, vehicle_name: str = ""):
                v = d._veh(vehicle_name)
                v.task.cancelled = True
                d._abort.set()  # unblock any in-flight _wait_task handler
                v.task = _Task("none", done=True)
                return True

            # state
            def getMultirotorState(self, vehicle_name: str = ""):
                v = d._veh(vehicle_name)
                gate = d._link_gate(v.name)
                if gate == "lost":  # M9: no telemetry reaches the operator
                    raise RuntimeError(f"datalink lost: no telemetry from {v.name}")
                build = lambda: d._multirotor_state(v)  # noqa: E731
                if gate == "degraded":
                    return d._stale(f"state:{v.name}", build)
                return build()

            def getGpsData(self, vehicle_name: str = "", sensor_name: str = ""):
                v = d._veh(vehicle_name)
                gate = d._link_gate(v.name)
                if gate == "lost":
                    raise RuntimeError(f"datalink lost: no GPS from {v.name}")
                build = lambda: d._gps_data(v)  # noqa: E731
                if gate == "degraded":
                    return d._stale(f"gps:{v.name}", build)
                return build()

            def simGetCollisionInfo(self, vehicle_name: str = ""):
                return {"has_collided": d._veh(vehicle_name).collision,
                        "time_stamp": int(time.time() * 1e9)}

            # images
            def simGetImages(self, requests, vehicle_name: str = "",
                             external: bool = False):
                v = d._veh(vehicle_name)
                # M9: a lost datalink takes the SENSOR feed with it. Gating only
                # getMultirotorState/getGpsData left full-motion video and
                # contacts streaming through a "lost" link, so a lost-link plan
                # was never actually deprived of anything.
                if d._link_gate(v.name) == "lost":
                    raise RuntimeError(f"datalink lost: no imagery from {v.name}")
                out = []
                for req in requests or []:
                    it = int(req.get("image_type", 0))
                    cam_name = str(req.get("camera_name", "0"))
                    cam = d.camera(v.name, cam_name)
                    png = _png_solid(d._frame_rgb(it))
                    as_float = bool(req.get("pixels_as_float", False))
                    compress = bool(req.get("compress", True))
                    if as_float:
                        data8: Any = []
                        dataf = [float(-v.ned.z)] * (IMG_W * IMG_H)
                    else:
                        data8 = (base64.b64encode(png).decode() if compress
                                 else list(png))
                        dataf = []
                    # M7: the mount offset is BODY-frame, so it has to be
                    # rotated by the vehicle attitude before it is added to a
                    # NED position — a nose-mounted camera on an east-bound
                    # drone is 2 m east, not 2 m north.
                    moff = _body_offset_to_ned(cam.offset, v.roll_deg,
                                               v.pitch_deg, v.heading_deg)
                    cam_ned = NedPoint(v.ned.x + moff.x,
                                       v.ned.y + moff.y,
                                       v.ned.z + moff.z)
                    out.append({
                        "image_data_uint8": data8,
                        "image_data_float": dataf,
                        "camera_name": cam_name,
                        "image_type": it,
                        "pixels_as_float": as_float,
                        "compress": compress,
                        "width": IMG_W,
                        "height": IMG_H,
                        "time_stamp": int(time.time() * 1e9),
                        "camera_position": _v3(cam_ned),
                        # Body attitude COMPOSED with the gimbal (quaternion
                        # product). Summing Euler angles is not composition and
                        # gives the wrong boresight off the level.
                        "camera_orientation": _q_mul(
                            _q_from_euler(v.roll_deg, v.pitch_deg, v.heading_deg),
                            _q_from_euler(cam.roll_deg, cam.pitch_deg,
                                          cam.yaw_deg)),
                    })
                return out

            def simGetCameraInfo(self, camera_name: str = "0",
                                 vehicle_name: str = "", external: bool = False):
                v = d._veh(vehicle_name)
                cam = d.camera(v.name, str(camera_name))
                return {
                    "pose": {"position": _v3(cam.offset),
                             "orientation": _q_from_euler(cam.roll_deg,
                                                          cam.pitch_deg,
                                                          cam.yaw_deg)},
                    "fov": cam.fov_deg,
                    "proj_mat": {"matrix": _proj_matrix(cam.fov_deg)},
                }

            def simSetCameraPose(self, camera_name, pose, vehicle_name: str = "",
                                 external: bool = False):
                v = d._veh(vehicle_name)
                roll, pitch, yaw = _euler_from_q(pose.get("orientation") or {})
                d.set_camera_pose(v.name, str(camera_name), pitch_deg=pitch,
                                  yaw_deg=yaw, roll_deg=roll,
                                  offset=_pos_of(pose))
                return True

            def simSetCameraFov(self, camera_name, fov_degrees,
                                vehicle_name: str = "", external: bool = False):
                v = d._veh(vehicle_name)
                d.set_camera_fov(v.name, str(camera_name), float(fov_degrees))
                return True

            # detections / scenario
            def simGetDetections(self, camera_name: str = "", image_type: int = 0,
                                 vehicle_name: str = "", external: bool = False):
                v = d._veh(vehicle_name)
                if d._link_gate(v.name) == "lost":  # M9: see simGetImages
                    raise RuntimeError(f"datalink lost: no contacts from {v.name}")
                cam = d.camera(v.name, str(camera_name or "0"))
                rng = d._rng
                res = []
                max_range = d.sensor_range_m(v.name, cam.name, int(image_type))
                fn_rate = d._effective_fn_rate()
                # M16/M17: a geolocation is the PLATFORM position plus the
                # measured relative vector, so the platform's navigation error
                # lands on every contact. Degrading only the vehicle's own fix
                # left the contacts it reported pinned to exact truth.
                nav = d._nav_error_ned(v)
                for name, gp in list(d._objects.items()):
                    ned = geodetic_to_ned(gp, d.home_geo.geo)
                    dx = ned.x - v.ned.x
                    dy = ned.y - v.ned.y
                    dz = ned.z - v.ned.z
                    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                    if dist > max_range:
                        continue
                    off = _frame_offset(v, cam, dx, dy, dz)
                    if cam.slewed and off is None:
                        continue  # M7: outside the slewed camera's frustum
                    # M17 false negative: drop a real contact this frame.
                    if fn_rate > 0.0 and rng.random() < fn_rate:
                        continue
                    # M17 geolocation error. The RELATIVE vector is what the
                    # sensor measures, so the error goes there and the
                    # absolute position is derived from it — the two stay
                    # consistent, which an error applied only to `geo_point`
                    # would not (the residual would be the tell). `box2D` is
                    # deliberately computed from the TRUE bearing: the pixels
                    # are where the contact really is; it is the RANGE and
                    # pointing solution behind them that is imperfect.
                    ex, ey, ez = d._geo_error_draw(dist, int(image_type))
                    rel = NedPoint(dx + ex, dy + ey, dz + ez)
                    if ex or ey or ez or nav.x or nav.y or nav.z:
                        rgp = ned_to_geodetic(
                            NedPoint(v.ned.x + nav.x + rel.x,
                                     v.ned.y + nav.y + rel.y,
                                     v.ned.z + nav.z + rel.z), d.home_geo)
                    else:
                        # No error to add, so report the object's stored
                        # position as-is. Re-deriving it would put it through
                        # a geodetic->NED->geodetic round trip, and those two
                        # conversions use different earth models: measured
                        # ~0.7 m of pure conversion drift. A perfect sensor
                        # must report the exact truth, not truth plus a
                        # rounding artefact of this function.
                        rgp = gp
                    res.append({
                        "name": name,
                        "geo_point": {
                            "latitude": rgp.latitude,
                            "longitude": rgp.longitude,
                            "altitude": rgp.altitude,
                        },
                        "box2D": _box2d(cam.fov_deg, dist, off),
                        "box3D": {"min": _v3(NedPoint(-2, -2, -2)),
                                  "max": _v3(NedPoint(2, 2, 2))},
                        "relative_pose": {
                            "position": _v3(rel),
                            "orientation": _q_identity(),
                        },
                    })
                # M17 false positive: invent a phantom contact. It has to be
                # indistinguishable from the real ones ABOVE — same naming
                # pattern, inside the same sensor range and frustum, boxed by
                # the same off-boresight maths. `phantom_N` at a uniform
                # +/-120 m with a perfectly centred box was a contact any
                # consumer could reject on the name alone, which made the
                # false-positive rate unfalsifiable as a test of fusion.
                if d._det_fp_rate > 0.0 and rng.random() < d._det_fp_rate:
                    ghost = d._phantom_contact(v, cam, max_range, nav,
                                               int(image_type))
                    if ghost is not None:
                        res.append(ghost)
                return res

            def simSpawnObject(self, object_name, mesh_name, pose, scale,
                               physics_enabled=False, is_blueprint=False):
                object_name = _s(object_name)
                ned = _pos_of(pose)
                d._objects[object_name] = ned_to_geodetic(ned, d.home_geo)
                d._resync_route(object_name)
                return object_name

            def simDestroyObject(self, object_name):
                name = _s(object_name)
                d._object_routes.pop(name, None)
                return d._objects.pop(name, None) is not None

            def simSetObjectPose(self, object_name, pose, teleport=True):
                object_name = _s(object_name)
                ned = _pos_of(pose)
                if object_name in d._objects:
                    d._objects[object_name] = ned_to_geodetic(ned, d.home_geo)
                    d._resync_route(object_name)
                    return True
                return False

            def simGetObjectPose(self, object_name):
                gp = d._objects.get(_s(object_name))
                if gp is None:
                    return {"position": _v3(NedPoint(math.nan, math.nan, math.nan)),
                            "orientation": _q_identity()}
                return {"position": _v3(geodetic_to_ned(gp, d.home_geo.geo)),
                        "orientation": _q_identity()}

            def simListSceneObjects(self, name_regex=".*"):
                return list(d._objects.keys())

            def simEnableWeather(self, enable):
                d.set_weather(enabled=bool(enable))
                return True

            def simSetWeatherParameter(self, param, value):
                # M18: store it — a no-op stub made weather unexercisable.
                key = WEATHER_PARAMS.get(int(param))
                if key is None:
                    return False
                if key == "enabled":
                    d.set_weather(enabled=bool(value))
                else:
                    with d._lock:
                        d._weather[key] = min(1.0, max(0.0, float(value)))
                        d._weather["enabled"] = 1.0
                return True

            def simSetTimeOfDay(self, is_enabled, start_datetime="",
                                is_start_datetime_dst=False,
                                celestial_clock_speed=1.0, update_interval_secs=60,
                                move_sun=True):
                # M6: store it — the sun position drives sun-side orbit doctrine.
                d.set_time(start_datetime or None,
                           float(celestial_clock_speed) if move_sun else 0.0,
                           bool(is_enabled))
                return True

            def simSetWind(self, wind):
                # wind is a Vector3r dict {x_val, y_val, z_val} in NED m/s
                d.set_wind(float(wind.get("x_val", 0.0)),
                           float(wind.get("y_val", 0.0)),
                           float(wind.get("z_val", 0.0)))
                return True

            def simPause(self, is_paused):
                return True

            def simContinueForTime(self, seconds):
                return True

            def simTestLineOfSightToPoint(self, point, vehicle_name=""):
                v = d._veh(vehicle_name)
                los, _ = d.line_of_sight(d._true_geo(v), _as_geopoint(point))
                return los

            def simTestLineOfSightBetweenPoints(self, point1, point2):
                los, _ = d.line_of_sight(_as_geopoint(point1), _as_geopoint(point2))
                return los

            # -- fake-only extensions (NOT part of AirSim's RPC surface) -----
            # Callers must treat these as unavailable when --real is used.
            def simGetLineOfSightInfo(self, point, vehicle_name=""):
                """LOS + the first obstacle (uav_los_check's second return)."""
                v = d._veh(vehicle_name)
                los, obstacle = d.line_of_sight(d._true_geo(v), _as_geopoint(point))
                return {"los": los, "first_obstacle": obstacle}

            def simSetLinkState(self, vehicle_name="", state="nominal", duration_s=0.0):
                """M9: force degraded|lost datalink for a duration."""
                return d.set_link_state(vehicle_name, state, float(duration_s))

            def simGetLinkState(self, vehicle_name=""):
                """M9: current datalink state for a vehicle."""
                return d.link_state(vehicle_name)

            def simGetSunPosition(self, vehicle_name=""):
                """M6: sun azimuth/elevation at the vehicle's position."""
                v = d._veh(vehicle_name)
                gp = d._true_geo(v)
                az, el = d.sun_position(gp.latitude, gp.longitude)
                return {"azimuth_deg": az, "elevation_deg": el,
                        "sim_time": d.sim_time().strftime("%Y-%m-%d %H:%M:%S"),
                        "is_day": el > 0.0}

            def simGetEnvironment(self):
                """Weather + sun + wind + sensor range (INTREP sensor conditions)."""
                return d.environment()

            def simSetObjectRoute(self, object_name, waypoints, speed=8.0, loop=False):
                """M17: drive a spawned object along geodetic waypoints."""
                d.set_object_route(_s(object_name), waypoints or [],
                                   float(speed), bool(loop))
                return True

        return H()

    # -- state builders (shared by the RPC handlers) ----------------------
    def _multirotor_state(self, v: _Vehicle) -> dict:
        gp = self._reported_geo(v)
        return {
            "kinematics_estimated": {
                "position": _v3(v.ned),
                "orientation": _q_from_euler(v.roll_deg, v.pitch_deg, v.heading_deg),
                "linear_velocity": _v3(v.vel),
                "angular_velocity": _v3(NedPoint(0, 0, 0)),
                "linear_acceleration": _v3(NedPoint(0, 0, 0)),
                "angular_acceleration": _v3(NedPoint(0, 0, 0)),
            },
            "gps_location": {
                "latitude": gp.latitude,
                "longitude": gp.longitude,
                "altitude": gp.altitude,
            },
            "timestamp": int(time.time() * 1e9),
            "landed_state": 0 if v.landed else 2,
            "rc_data": {"timestamp": 0, "is_initialized": True,
                        "is_valid": True, "switches": 0, "vendor_id": ""},
            "ready": True,
            "ready_message": "",
            "can_arm": True,
        }

    def _gps_data(self, v: _Vehicle) -> dict:
        gp = self._reported_geo(v)
        return {
            "time_stamp": int(time.time() * 1e9),
            "gnss": {
                "geo_point": {
                    "latitude": gp.latitude,
                    "longitude": gp.longitude,
                    "altitude": gp.altitude,
                },
                "eph": 1.0,
                "epv": 1.5,
                "velocity": _v3(v.vel),
                "fix_type": 3,
                "satellites_visible": 12,
            },
            "is_valid": True,
        }

    def _effective_fn_rate(self) -> float:
        """M17+M18: the configured false-negative rate, raised by obscurants."""
        weather_fn = 0.6 * self._obscurant()
        return min(1.0, 1.0 - (1.0 - self._det_fn_rate) * (1.0 - weather_fn))

    def _frame_rgb(self, image_type: int) -> tuple[int, int, int]:
        """Flat frame colour per ImageType, dimmed by night + weather (M6/M18)."""
        base = {
            0: (90, 140, 90),   # Scene: greenish terrain
            1: (40, 40, 40),    # DepthPlanar
            3: (60, 60, 80),    # DepthVis
            5: (0, 0, 0),       # Segmentation (black)
            7: (120, 120, 120),  # Infrared: gray
        }.get(int(image_type), (50, 50, 50))
        if int(image_type) == 7:  # thermal: unaffected by light, hazed by weather
            k = 0.5 + 0.5 * self.visibility_factor()
        else:
            k = self.light_factor() * (0.4 + 0.6 * self.visibility_factor())
        return tuple(max(0, min(255, int(c * k))) for c in base)  # type: ignore[return-value]

    def _start_task(self, v: _Vehicle, task: "_Task") -> None:
        self._abort.clear()  # a fresh maneuver supersedes any prior cancel
        v.task = task

    def _wait_task(self, v: _Vehicle, timeout_sec: float) -> None:
        # Real AirSim holds the RPC response until the maneuver finishes.
        # Unblock on completion, cancelLastTask, or sim.stop() so neither an
        # abort nor a teardown hangs a handler thread.
        t = v.task
        deadline = time.monotonic() + min(float(timeout_sec), 120.0)
        while (not t.done and not t.cancelled and not self._stop.is_set()
               and not self._abort.is_set()
               and time.monotonic() < deadline):
            time.sleep(0.01)

    def _veh(self, name) -> _Vehicle:
        # msgpack-rpc delivers str args as bytes (raw=True); normalize.
        if isinstance(name, (bytes, bytearray)):
            name = name.decode("utf-8", "replace")
        name = name or "Drone1"
        if name in self._objects:
            # A spawned target is scenery, not an airframe: creating a vehicle
            # for it minted phantom drones on the globe.
            raise ValueError(f"{name!r} is a spawned object, not a vehicle")
        return self._vehicles.setdefault(name, _Vehicle(name))


def _s(x) -> str:
    # msgpack-rpc delivers str args as bytes (raw=True); normalize.
    if isinstance(x, (bytes, bytearray)):
        return x.decode("utf-8", "replace")
    return x


def _v3(p: NedPoint) -> dict:
    return {"x_val": p.x, "y_val": p.y, "z_val": p.z}


def _v2(x: float, y: float) -> dict:
    return {"x_val": x, "y_val": y}


def _q_identity() -> dict:
    return {"w_val": 1.0, "x_val": 0.0, "y_val": 0.0, "z_val": 0.0}


def _q_from_euler(roll_deg: float, pitch_deg: float, yaw_deg: float) -> dict:
    """NED body attitude (roll/pitch/yaw, deg) -> AirSim quaternion dict."""
    r, p, y = (math.radians(a) * 0.5 for a in (roll_deg, pitch_deg, yaw_deg))
    cr, sr, cp, sp, cy, sy = (math.cos(r), math.sin(r), math.cos(p),
                              math.sin(p), math.cos(y), math.sin(y))
    return {
        "w_val": cr * cp * cy + sr * sp * sy,
        "x_val": sr * cp * cy - cr * sp * sy,
        "y_val": cr * sp * cy + sr * cp * sy,
        "z_val": cr * cp * sy - sr * sp * cy,
    }


def _euler_from_q(q: dict) -> tuple[float, float, float]:
    """AirSim quaternion dict -> (roll, pitch, yaw) degrees."""
    w = float(q.get("w_val", 1.0))
    x = float(q.get("x_val", 0.0))
    y = float(q.get("y_val", 0.0))
    z = float(q.get("z_val", 0.0))
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    sinp = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
    pitch = math.asin(sinp)
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)


def _pos_of(pose: Any) -> NedPoint:
    """Read a Pose's position (R1: keys arrive bytes-decoded, defaults are real)."""
    p = (pose or {}).get("position") or {}
    return NedPoint(float(p.get("x_val", 0.0) or 0.0),
                    float(p.get("y_val", 0.0) or 0.0),
                    float(p.get("z_val", 0.0) or 0.0))


def _as_geopoint(pt: Any, default_alt: float = 0.0) -> GeoPoint:
    """Coerce a wire GeoPoint dict / tuple / GeoPoint into a GeoPoint."""
    if isinstance(pt, GeoPoint):
        return pt
    if isinstance(pt, dict):
        return GeoPoint(float(pt.get("latitude", pt.get("lat", 0.0))),
                        float(pt.get("longitude", pt.get("lon", 0.0))),
                        float(pt.get("altitude", pt.get("alt", default_alt))))
    seq = list(pt)
    return GeoPoint(float(seq[0]), float(seq[1]),
                    float(seq[2]) if len(seq) > 2 else default_alt)


def _rot_body_to_ned(roll_deg: float, pitch_deg: float,
                     yaw_deg: float) -> list[list[float]]:
    """Body->NED direction-cosine matrix (aerospace Z-Y-X, pitch positive up).

    Column 0 is the body x-axis (boresight/nose) expressed in NED, column 1 the
    y-axis (right wing), column 2 the z-axis (belly). Adding Euler angles is
    NOT rotation composition, which is why the camera boresight and the
    reported camera_orientation both go through real matrices/quaternions.
    """
    cr, sr = math.cos(math.radians(roll_deg)), math.sin(math.radians(roll_deg))
    cp, sp = math.cos(math.radians(pitch_deg)), math.sin(math.radians(pitch_deg))
    cy, sy = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    return [
        [cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy],
        [cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy],
        [-sp, sr * cp, cr * cp],
    ]


def _rot_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    """3x3 matrix product."""
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def _body_offset_to_ned(offset: NedPoint, roll_deg: float, pitch_deg: float,
                        yaw_deg: float) -> NedPoint:
    """Rotate a body-frame mount offset into NED (M7 camera_position)."""
    r = _rot_body_to_ned(roll_deg, pitch_deg, yaw_deg)
    o = (offset.x, offset.y, offset.z)
    return NedPoint(*[sum(r[i][k] * o[k] for k in range(3)) for i in range(3)])


def _q_mul(a: dict, b: dict) -> dict:
    """Hamilton product a*b of two AirSim quaternion dicts (a then b applied)."""
    aw, ax, ay, az = (a["w_val"], a["x_val"], a["y_val"], a["z_val"])
    bw, bx, by, bz = (b["w_val"], b["x_val"], b["y_val"], b["z_val"])
    return {
        "w_val": aw * bw - ax * bx - ay * by - az * bz,
        "x_val": aw * bx + ax * bw + ay * bz - az * by,
        "y_val": aw * by - ax * bz + ay * bw + az * bx,
        "z_val": aw * bz + ax * by - ay * bx + az * bw,
    }


def _vfov_deg(hfov_deg: float) -> float:
    """Vertical FOV from the horizontal FOV at the fake's frame aspect."""
    half = math.radians(max(1.0, hfov_deg)) * 0.5
    return 2.0 * math.degrees(math.atan(math.tan(half) * IMG_H / IMG_W))


def _frame_offset(v: _Vehicle, cam: _Camera, dx: float, dy: float,
                  dz: float) -> tuple[float, float] | None:
    """(daz, del) degrees off the camera boresight, or None if out of frame.

    The contact vector is rotated OUT of NED into the camera frame (x
    boresight, y right, z down) and gated against the real rectilinear
    frustum. Comparing world-frame azimuth/elevation against the boresight's
    instead (the obvious separable test) is only valid for a level boresight:
    with the ball slewed to nadir, world azimuth is degenerate, and a measured
    7 of 8 ground contacts 11 deg off the boresight were rejected on azimuth
    alone. A slewed camera is the only time this gate runs (M7), so the
    separable form was wrong exactly when it mattered.
    """
    rot = _rot_mul(_rot_body_to_ned(v.roll_deg, v.pitch_deg, v.heading_deg),
                   _rot_body_to_ned(cam.roll_deg, cam.pitch_deg, cam.yaw_deg))
    # camera-frame components = columns of the body->NED rotation dotted with d
    fwd = rot[0][0] * dx + rot[1][0] * dy + rot[2][0] * dz
    right = rot[0][1] * dx + rot[1][1] * dy + rot[2][1] * dz
    down = rot[0][2] * dx + rot[1][2] * dy + rot[2][2] * dz
    if fwd <= 1e-9:  # behind the image plane
        return None
    tan_h = math.tan(math.radians(max(1.0, cam.fov_deg)) / 2.0)
    tan_v = math.tan(math.radians(_vfov_deg(cam.fov_deg)) / 2.0)
    if abs(right) > fwd * tan_h or abs(down) > fwd * tan_v:
        return None
    return math.degrees(math.atan2(right, fwd)), -math.degrees(math.atan2(down, fwd))


def _box2d(fov_deg: float, dist_m: float, off: tuple[float, float] | None) -> dict:
    """Pixel bounding box: narrower FOV = more pixels on the same contact (M7)."""
    px_per_deg = IMG_W / max(1.0, fov_deg)
    ang = 2.0 * math.degrees(math.atan2(NOMINAL_TARGET_SIZE_M / 2.0, max(dist_m, 1.0)))
    half = max(2.0, min(IMG_W / 2.0, ang * px_per_deg / 2.0))
    cx = IMG_W / 2.0 + (off[0] * px_per_deg if off else 0.0)
    cy = IMG_H / 2.0 - (off[1] * px_per_deg if off else 0.0)
    return {"min": _v2(round(cx - half, 1), round(cy - half, 1)),
            "max": _v2(round(cx + half, 1), round(cy + half, 1))}


def _proj_matrix(fov_deg: float) -> list[list[float]]:
    """Perspective projection matrix for simGetCameraInfo (row-major)."""
    f = 1.0 / math.tan(math.radians(max(1.0, fov_deg)) / 2.0)
    aspect = IMG_W / IMG_H
    return [[f, 0.0, 0.0, 0.0], [0.0, f * aspect, 0.0, 0.0],
            [0.0, 0.0, 1.0, -1.0], [0.0, 0.0, 1.0, 0.0]]


def _parse_datetime(text: str) -> datetime:
    """Parse AirSim's '%Y-%m-%d %H:%M:%S' (ISO 'T' also accepted)."""
    s = str(text).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable sim datetime {text!r}")


def _solar_position(utc: datetime, lat_deg: float,
                    lon_deg: float) -> tuple[float, float]:
    """NOAA low-precision solar position -> (azimuth from N, elevation) deg."""
    doy = utc.timetuple().tm_yday
    hour = utc.hour + utc.minute / 60.0 + utc.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (doy - 1 + (hour - 12.0) / 24.0)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma)
                       - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma)
                       - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    time_offset = eqtime + 4.0 * lon_deg  # minutes; utc => timezone 0
    tst = hour * 60.0 + time_offset
    ha = math.radians(tst / 4.0 - 180.0)
    lat = math.radians(lat_deg)
    cos_zen = (math.sin(lat) * math.sin(decl)
               + math.cos(lat) * math.cos(decl) * math.cos(ha))
    cos_zen = max(-1.0, min(1.0, cos_zen))
    zenith = math.acos(cos_zen)
    elevation = 90.0 - math.degrees(zenith)
    sin_zen = math.sin(zenith)
    if abs(sin_zen) < 1e-9:
        return 180.0, elevation
    cos_az = (math.sin(lat) * cos_zen - math.sin(decl)) / (math.cos(lat) * sin_zen)
    az = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
    # acos gives the angle either side of due south; the hour angle picks the
    # side (negative = morning, sun east of south).
    azimuth = (180.0 + az) % 360.0 if ha > 0 else (180.0 - az) % 360.0
    return azimuth, elevation


if __name__ == "__main__":
    s = FakeAirSim()
    s.start()
    print(f"fake-airsim listening on 127.0.0.1:{s.port}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        s.stop()
