"""godSeye UAV MCP server (PLAN §4).

Streamable HTTP transport (M21), Bearer token auth (T4e), loopback default.
Tool catalog 4.1 (flight control) + 4.2 (sensors/intel). Every flight tool
runs through the per-vehicle queue (T2) and the safety envelope's pre-flight
BINGO gate (M4); all submissions land in the JSONL store.

What this layer OWNS (Wave 2, stage 1 — wiring the Wave-1 models in):

* **Datum (T1)** — the NED origin is carried as HAE and every altitude the
  server publishes comes out of `geo.canonical_altitude()`, the one conversion
  point. `uav_get_telemetry` returns `alt_hae_m`, `alt_msl_m` and `alt_agl_m`
  with the undulation and its provenance, so the server and the bridge can no
  longer disagree by |N| on the same vehicle at the same instant.
* **Fuel + safety tick loop (T5/M4/R5)** — `tick_once()` reads live telemetry,
  drives `safety.SafetyMonitor` (fuel integrator with the MEASURED phase and
  the headwind resolved from the wind vector, envelope check, link state) and
  persists the integral through `store.log_fuel`. Reaching BINGO pre-empts the
  queue and submits an UN-CANCELLABLE RTB; the mission is flagged
  "incomplete - fuel" and no harness command can clear either.
* **Lost link (M9)** — the per-mission `lost_link_plan` is executed
  autonomously (hold-orbit / climb-for-LOS / RTB / continue) and every LOAL
  event is logged for the INTREP. A harness disconnect triggers the same path.
* **Restart (T4c)** — `store.replay()` runs at construction and its
  resume-or-abort-and-RTH decision is acted on; idempotency keys are re-seeded
  from the journal so a replayed key never re-flies a command.

What this layer OWNS (Wave 2, stage 2 — the full catalog + resources):

* **§4.1/§4.2/§4.3/§4.4 tool catalog** — orbit (with the M3/M6 sun-side rule),
  gimbal + FOV (the M7 cross-cue), imagery, detections with persistent track
  ids, an honest LOS check, the discrete `mission_*` primitives, and the sim
  admin surface. `uav_mission(kind=…)` is now a THIN dispatcher onto the same
  discrete implementations — the doctrine maths stays in `missions.py`.
* **§4.8 resources (R6: the server exposed ZERO)** — all eight `uav://`
  resources, `uav://safety/geofence` first among them: PLAN §4.5 says the
  skill's ROE "may only be stricter" than the server envelope, which no
  harness can honour without being able to READ the envelope.

What this layer OWNS (the REAL WORLD — REAL_DATA_INTEGRATION.md):

`realdata.py` was built, live-verified, and then called from NOWHERE, so the
environment stayed synthetic in four specific ways. Each is wired here:

* **True AGL.** `alt_agl_m` meant height above the LAUNCH DATUM — the NED
  origin — which is real AGL only over ground at the home elevation. Every
  server-side telemetry read now goes through `_telemetry` -> `resolve_agl`,
  which measures against the terrain under the aircraft. The fuel integrator,
  the envelope's min-AGL (the geofence's missing FLOOR), the mission planners,
  the resources and the harness all see the same number.
* **Terrain LOS.** `uav_los_check` answered from a geometric earth-curvature
  horizon with no ground in it at all, so a mountain was invisible to the M5
  standoff check. The sight line is now also cut against bare-earth terrain,
  and the two models compose with AND.
* **Ground truth on the ground.** `sim_spawn_target` defaulted to one
  hand-entered elevation for a whole AO; it now defaults to the measured height
  under the point.
* **Real wind and weather.** The vector pricing every leg's burn (M15) was
  whatever an operator typed; `sim_set_weather(source="real")` takes it from
  the observation. Mapped sites (`sim_spawn_order_of_battle`) and live air
  traffic (`uav_deconflict_airspace`) are wired the same way.

Three rules hold throughout, and are enforced rather than intended: the layer
is OPT-IN (CI and the offline path never need a socket); no telemetry tick or
mission call EVER waits on a fetch (snapped, cached-only reads plus background
prefetch and a resident AO lattice); and a degraded feed falls back to exactly
the old synthetic behaviour while SAYING SO (`*_is_real`, `*_source` and the
feed's `Provenance` ride along on the MCP surface). A silent substitution here
would repeat the bug that made the EGM96 geoid dead code for this project.

ISR-only: no kinetic tools exist here.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer

from . import theaters
from .geo import (
    GeoPoint,
    HomeGeoPoint,
    NedPoint,
    canonical_altitude,
    geodetic_to_ned,
    ned_to_geodetic,
)
from .safety import (
    MISSION_INCOMPLETE_FUEL,
    FuelModel,
    LostLinkBehaviour,
    LostLinkPlan,
    SafetyEnvelope,
    SafetyMonitor,
    haversine_m,
)
from .store import ABORT_RTH, RESUME, Store
# Default number of contacts the intel roll-ups expand. These reports grow with
# the track store, which persists across runs; the full THREATREP measured
# 1.1 MB at 36 tracks, past what the MCP client carries and far past what a
# harness can read. Summary is the default; `detail='full'` opts back in.
from .targets import SUMMARY_TOP_N as INTREP_SUMMARY_TOP_N
from .threat import SUMMARY_TOP_N as THREAT_SUMMARY_TOP_N
from .tasking import (
    PROGRESS_EPS_PCT,
    TERMINAL,
    Task,
    TaskingService,
    VehicleBusyError,
)
from .theaters import Theater

#: How often the safety monitor samples telemetry when it is running.
DEFAULT_TICK_S = 0.5

#: `uav_capture_image` / `uav://{vehicle}/camera/{name}/{type}` image types.
#: Keyed by the contract's spelling (§4.2: scene/depth/segmentation/infrared);
#: the value is AirSim's `ImageType` enum. An unknown name is REFUSED — it is
#: never quietly served as a scene frame.
IMAGE_TYPES: dict[str, int] = {
    "scene": 0,
    "depth": 1,               # DepthPlanar
    "depth_planar": 1,
    "depth_perspective": 2,
    "depth_vis": 3,
    "disparity": 4,
    "segmentation": 5,
    "surface_normals": 6,
    "infrared": 7,
}

#: What each line-of-sight backend ACTUALLY models. `uav_los_check` returns
#: this verbatim (TOOL_CONTRACT §4.2: "State the model in the return"), because
#: a LOS answer the harness cannot characterise is worse than none — M5
#: standoff verification depends on knowing what was and was not ruled out.
LOS_MODELS: dict[str, str] = {
    "sim_los_info": (
        "fake_airsim.line_of_sight: geometric earth-curvature horizon "
        "(k=1.0, no atmospheric refraction) plus declared obstruction "
        "cylinders. Does NOT model terrain, vegetation, buildings that were "
        "not declared as obstructions, or the airframe. A true result means "
        "'not blocked by the horizon or any declared obstruction', never "
        "'verified clear'."),
    "airsim_scene_trace": (
        "AirSim simTestLineOfSightToPoint: an engine ray trace against the "
        "loaded level (terrain and static meshes included). This RPC returns "
        "a bare boolean, so first_obstacle is null even when los is false — "
        "the blocker is not identified."),
    "airsim_scene_trace_between": (
        "AirSim simTestLineOfSightBetweenPoints: an engine ray trace between "
        "two geodetic points. Bare boolean; the blocker is not identified."),
}

#: Weather parameters `sim_set_weather` accepts -> AirSim WeatherParameter enum.
WEATHER_PARAMS: dict[str, int] = {"rain": 0, "snow": 2, "dust": 6, "fog": 7}

#: Datalink states `sim_set_link_state` accepts (M9).
LINK_STATES = ("nominal", "degraded", "lost")

#: Fuel-journal field -> the `FuelModel.from_dict` field it restores (T4c).
#:
#: `Store.log_fuel` writes what `FuelModel.fuel_record()` produces, and that
#: record spells three fields DIFFERENTLY from `FuelModel.to_dict()`, which is
#: what `from_dict` reads. Handing a raw fuel row straight to `from_dict` looks
#: like it works and silently restores `last_phase=ground`,
#: `last_headwind_mps=0.0` and an UN-TRIPPED BINGO latch off its own `.get()`
#: defaults — i.e. it drops exactly the three fields that say what the aircraft
#: was doing and whether it was already committed to RTB. The mapping is
#: explicit so the translation is visible instead of assumed.
FUEL_ROW_TO_STATE: dict[str, str] = {
    "fuel_pct": "fuel_pct",
    "phase": "last_phase",                # fuel_record spells it "phase"
    "burned_pct": "burned_pct",
    "elapsed_s": "elapsed_s",
    "ticks": "ticks",
    "headwind_mps": "last_headwind_mps",  # fuel_record drops the "last_"
    "clamped_ticks": "clamped_ticks",
    "unaccounted_s": "unaccounted_s",
}

# ---------------------------------------------------------------------------
# Real-world data (REAL_DATA_INTEGRATION.md). `realdata.py` was built, live
# verified and then wired to NOTHING: `alt_agl_m` still meant "height above the
# launch datum", `uav_los_check` answered from a geometric horizon with no
# terrain in it at all, `sim_spawn_target` defaulted to the theater's
# hand-entered elevation, and the wind that prices the fuel model was whatever
# the operator typed. These constants are the switch and the vocabulary for
# wiring the consumers.
#
# Three rules govern everything below and are enforced, not merely intended:
#
# 1. OPT-IN / auto-degrading. The layer is OFF unless a client is injected or
#    `GODSEYE_REAL_DATA` is set, so CI, the offline demo and the zero-GPU path
#    never touch a socket.
# 2. NEVER block a telemetry tick or a mission call on a fetch. Every read on a
#    hot path is `allow_network=False` (memory only) and a miss schedules a
#    background prefetch instead of waiting.
# 3. FAIL SOFT, BUT VISIBLE. A degraded feed falls back to exactly the old
#    synthetic behaviour AND says so: `*_is_real`, `*_source` and the feed's
#    own `Provenance` ride along on the MCP surface, so a harness can tell a
#    MEASURED AGL from an ASSUMED one. Silent substitution here is the bug that
#    made the EGM96 geoid dead code for this whole project.
# ---------------------------------------------------------------------------

#: Environment switch for the real-world data layer. Unset means OFF.
REAL_DATA_ENV = "GODSEYE_REAL_DATA"

#: Accepted spellings. Anything else is REFUSED rather than read as "off": an
#: unrecognised value silently disabling the feature is precisely the
#: "validator that accepts a value which disables the feature" failure.
REAL_DATA_TRUE = frozenset({"1", "true", "yes", "on", "enable", "enabled"})
REAL_DATA_FALSE = frozenset({"", "0", "false", "no", "off", "disable", "disabled"})

#: `alt_agl_m` came from MEASURED terrain under the aircraft.
AGL_SOURCE_TERRAIN = "terrain:gev"
#: `alt_agl_m` is the pre-existing height above the LAUNCH DATUM — the NED
#: origin — which is only true AGL over flat ground at the home elevation.
AGL_SOURCE_LAUNCH = "synthetic:launch-datum"

#: Said on every launch-datum AGL so no consumer can mistake it for measured.
AGL_LAUNCH_NOTE = (
    "SYNTHETIC: height above the LAUNCH DATUM (the NED origin), not above real "
    "terrain. Equal to true AGL only over ground at the home elevation. "
    "alt_agl_is_real=false; enable the real-world data layer for measured AGL.")

#: Said on a measured AGL.
AGL_TERRAIN_NOTE = (
    "MEASURED: height above real terrain sampled under the aircraft "
    "(Re:Earth/Mapterhorn bare earth via the God's Eye View terrain proxy). "
    "Bare earth: vegetation and buildings are NOT in it.")

#: Weather sources `sim_set_weather` accepts. 'real' takes every value from the
#: hydrated `WeatherObservation` instead of the caller.
WEATHER_SOURCES = ("operator", "real")

#: Cap on concurrent background terrain prefetches, so a mission that wanders
#: off the hydrated AO cannot turn every telemetry tick into an HTTP request.
TERRAIN_PREFETCH_MAX_INFLIGHT = 4

#: Finest terrain query grid, degrees — ~111 m of latitude, ~91 m of longitude
#: at 35 deg N, one DEM posting and the same order as LOS_SAMPLE_SPACING_M.
#: Every coarser grid is an integer multiple of this one, so the AO lattice a
#: hydration loads and the point a telemetry tick snaps to are always the SAME
#: lattice. They have to be: a query snapped to 0.001 against a lattice loaded
#: at 0.002 misses every other cell, which looks exactly like working.
#:
#: The snap itself is load-bearing, not an optimisation. `TerrainProvider` keys
#: its cache on the exact point (5 dp, matching the upstream proxy's own key),
#: and a flying aircraft is never twice at the same 5-dp point: unsnapped, every
#: telemetry tick misses, fires a prefetch for a coordinate it has already left,
#: and `alt_agl_m` silently stays on the launch datum for the whole sortie.
#: Wiring that reports itself as wired and never measures anything is precisely
#: the defect this wave exists to remove. The snap is REPORTED alongside the
#: value: the height is the ground at the nearest lattice point, not underfoot.
TERRAIN_GRID_DEG = 0.001

#: Points one AO terrain load may ask for. The proxy's own hard cap is 2000 per
#: request (`realdata.TERRAIN_MAX_POINTS`); this leaves headroom and keeps a
#: cold load to a handful of batched requests. The lattice spacing is CHOSEN to
#: fit the theater's AO inside it, never truncated to it — a truncated lattice
#: would leave part of the AO unmeasured while the load reported success.
TERRAIN_AO_MAX_POINTS = 1024

#: Half-width, in lattice cells, of the neighbourhood prefetched on a miss. 1
#: gives a 3x3 = 9-point request, which covers where the aircraft is going next
#: in every direction for the price of one round trip.
TERRAIN_PREFETCH_HALO = 1

#: What `uav_los_check` may do to get terrain. 'cached' never touches the
#: network (default, safe on any path); 'fetch' lets the harness opt into a
#: bounded upstream lookup on a worker thread when it needs a MEASURED answer;
#: 'off' skips terrain entirely.
LOS_TERRAIN_MODES = ("cached", "fetch", "off")


def snap_to_terrain_grid(lat: float, lon: float,
                         grid_deg: float = TERRAIN_GRID_DEG) -> tuple[float, float]:
    """The lattice point a terrain query is asked at. See `TERRAIN_GRID_DEG`.

    The lattice is anchored on the meridian and the equator (multiples of
    `grid_deg`), so it is the same lattice for every caller and every theater —
    nothing has to agree on an origin.
    """
    return (round(round(float(lat) / grid_deg) * grid_deg, 5),
            round(round(float(lon) / grid_deg) * grid_deg, 5))


def terrain_grid_for(bbox: tuple[float, float, float, float],
                     *, min_deg: float = TERRAIN_GRID_DEG,
                     budget: int = TERRAIN_AO_MAX_POINTS) -> float:
    """The finest multiple of `min_deg` whose lattice covers `bbox` in `budget`.

    Coarsening rather than truncating is deliberate: a lattice cut off at the
    request cap covers part of the AO and leaves the rest silently unmeasured.
    """
    south, west, north, east = bbox
    for k in range(1, 1001):
        grid = round(k * min_deg, 6)
        # +3 for the half-cell of overhang at each end plus the endpoint.
        rows = int((north - south) / grid) + 3
        cols = int((east - west) / grid) + 3
        if rows * cols <= budget:
            return grid
    raise ValueError(f"no terrain lattice fits {bbox} into {budget} points")

#: How long past a leg's NOMINAL flight time a SAFETY task flying with the
#: datalink down waits before reporting the arrival unverified (M9). There is
#: no telemetry to converge on while blind, so the sighted 3x backstop would
#: only stall the recovery; the leg is still commanded, it is simply never
#: reported as confirmed.
BLIND_LEG_GRACE_S = 15.0
#: Same, for the touchdown at the end of a blind safety RTB.
BLIND_LAND_S = 20.0

#: Nominal descent rate used ONLY to estimate the eta of the landing phase, so
#: a `uav_return_to_home` handle does not advertise eta 0.4 s while a 30 s
#: descent is still to come. It is an ETA input, never a control input: the
#: descent itself is flown by the sim and confirmed on telemetry.
NOMINAL_DESCENT_MPS = 2.0
#: Default takeoff height, metres AGL above the launch terrain.
DEFAULT_TAKEOFF_AGL_M = 30.0

#: The highest progress an EXECUTOR may report. 100 % is written by the queue,
#: and only as the task becomes terminal (`tasking._execute_one`), so
#: `progress_pct == 100` means "this task is over" and nothing else. Without
#: this, `uav_return_to_home` published progress 100 % / eta 0.4 s the moment
#: the aircraft reached home and then stayed `executing` through the whole
#: descent, so every following command was refused as busy against a handle
#: that claimed to be finished.
EXEC_PROGRESS_CEILING = 99.0
#: Share of an RTB's progress bar that belongs to the CRUISE home; the rest is
#: the descent. Arrival overhead is not completion — the aircraft still has to
#: get down, and the queue is legitimately busy until it does (T2).
RTB_CRUISE_PCT = 88.0
#: Head of a `uav_fly_route`'s progress bar reserved for the auto-takeoff climb
#: when the vehicle starts LANDED. The climb now goes to the route's own first
#: altitude instead of a flat 30 m, so it can outlast the no-progress watchdog;
#: reporting inside a reserved slice keeps the watchdog honest without letting
#: the climb run the whole bar up before a metre of route is flown.
ROUTE_CLIMB_PCT = 5.0

#: How close to home an `uav_return_to_home` has to be for the min-AGL band to
#: be its LANDING rather than an in-flight breach. Matches the `at_home`
#: threshold `_execute`'s RTB branch uses, with margin for the touchdown drift.
RTB_LANDING_RADIUS_M = 50.0

#: How far an explicit envelope's home may sit from the resolved theater's home
#: before the two are reported as a mismatch. Generous: the theaters are
#: hundreds of kilometres apart, and a test envelope that nudges the AO around
#: its own home must not trip it.
THEATER_HOME_TOLERANCE_M = 5000.0

#: Envelope-violation kinds that are *breaches* of the flight envelope rather
#: than the geofence. Audited under a store-recognised safety kind so a restart
#: replay treats them as in-flight safety transitions (T4c).
_BREACH_AUDIT_KIND = {
    "geofence": "geofence",
    "geofence_proximity": "geofence_proximity",
    "ceiling": "envelope_breach",
    "min_agl": "envelope_breach",
    "max_speed": "envelope_breach",
}


class LosModelUnavailableError(RuntimeError):
    """No line-of-sight model could be reached.

    Raised rather than returning `los: True`. TOOL_CONTRACT §4.2 forbids an
    unconditional true, and "assume clear" is exactly the silent fallback that
    would make M5 standoff verification a lie.
    """


def terrain_los_votes(cut: Any) -> bool:
    """Does this terrain cut get a vote in the composed LOS verdict?

    "Clear only if EVERY model that ANSWERED says clear" — and a cut that could
    not read the ground has not answered. `TerrainProvider.line_of_sight`
    returns `los=False, known=False` when no bare-earth height is resident along
    the profile: an honest refusal AT THAT LAYER, but an observation of nothing.
    ANDing it into the verdict turned every COLD profile into a hard block, and
    the profile is deliberately unsnapped (see `terrain_los`) so a cold profile
    is the normal case on any line's first ask.

    A cut votes when it is fully `known`, and ALSO when it found a real measured
    obstruction even though some other sample along the profile was not: a
    partial profile that actually saw the ridge has answered about the ridge,
    and dropping that vote would put the M5 standoff back where it started.
    """
    if cut is None:
        return False
    if cut.known:
        return True
    obstacle = cut.first_obstacle
    return bool(obstacle) and bool(obstacle.get("terrain_is_real"))


def resolve_real_data(spec: Any) -> Any:
    """Turn the `real_data=` constructor switch into a client, or `None`.

    Accepted:

    * a `realdata.RealWorldData` (or anything carrying a `.terrain`) — used
      as-is, which is how the tests inject a recorded, offline fetch;
    * `True` — build the default client on `GODSEYE_GEV_ORIGIN`;
    * `False` — OFF, whatever the environment says;
    * `None` — read `GODSEYE_REAL_DATA`, defaulting to OFF.

    An environment value that is neither truthy nor falsey raises. It would
    otherwise read as "off", which is the same class of defect as a validator
    that accepts a value which quietly disables the feature: the operator sets
    `GODSEYE_REAL_DATA=on please`, sees no error, and flies a whole sortie on
    synthetic terrain believing it is measured.
    """
    if spec is False:
        return None
    if spec is not None and spec is not True:
        if not hasattr(spec, "terrain"):
            raise ValueError(
                f"real_data={spec!r} is neither a RealWorldData client nor a "
                "bool; it must carry a .terrain provider")
        return spec
    if spec is None:
        import os

        raw = os.environ.get(REAL_DATA_ENV)
        if raw is None:
            return None
        value = raw.strip().lower()
        if value in REAL_DATA_FALSE:
            return None
        if value not in REAL_DATA_TRUE:
            raise ValueError(
                f"{REAL_DATA_ENV}={raw!r} is not a recognised switch; use one "
                f"of {sorted(REAL_DATA_TRUE)} or {sorted(REAL_DATA_FALSE - {''})}. "
                "It is refused rather than read as 'off', because a typo that "
                "silently disables real-world data is indistinguishable from "
                "having it.")
    from . import realdata

    # No instance-wide fallback ground plane: with none, an unknown terrain
    # height comes back None and FLAGGED instead of a plausible-looking number,
    # and the AGL consumer falls back to the launch datum *visibly*.
    return realdata.default_client(fallback_ground_msl_m=None)


def error(code: str, message: str, retryable: bool = False, **extra: Any) -> dict:
    """The structured error envelope every tool returns (TOOL_CONTRACT).

    Safety REJECTIONS are not errors — they come back as a gate result so the
    harness can re-plan. This is for malformed input and unreachable backends.
    """
    return {"error": {"code": code, "message": message, "retryable": retryable,
                      **extra}}


def overlap_fraction(value: float, param: str) -> float:
    """`overlap_pct` at the MCP boundary is a PERCENT; convert it once, here.

    The tool parameter is named `overlap_pct`, so 20 means 20% — that is what
    a harness reading TOOL_CONTRACT §4.3 sends. `missions.lane_spacing_m` takes
    a FRACTION, so the conversion has to happen somewhere; it happens exactly
    here, and the plan reports both numbers.

    The ambiguous band (0, 1) is REFUSED rather than guessed: 0.20 could mean
    20% or 0.2%, and getting it wrong changes the derived lane spacing by 100x
    without anyone noticing, which is precisely the class of defect M1 exists
    to stop.
    """
    v = float(value)
    if not (0.0 <= v < 100.0):
        raise ValueError(
            f"{param}={value!r} must be a percentage in [0, 100) — 20 means "
            "20% overlap.")
    if 0.0 < v < 1.0:
        raise ValueError(
            f"{param}={value!r} is ambiguous: read as a percentage that is "
            f"{v}% overlap, which is almost certainly not what was meant. "
            f"Pass {param}=20 for 20% overlap. The server will not guess "
            "which unit you meant (M1).")
    return v / 100.0


def latlon_polygon(value: Any, param: str) -> list[tuple[float, float]]:
    """Normalize a polygon to `[(lat, lon), ...]` at the MCP boundary.

    The mission planners take a vertex as `{"lat","lon"}` OR `[lat, lon]`
    (`missions._pt`), so that is the shape a harness reading TOOL_CONTRACT §4.3
    sends. `safety.point_in_polygon` understands ONLY the pair form: given a
    dict it unpacks the KEYS, so `polygon[0]` became the strings
    ("lat", "lon"). The threat tools passed the caller's polygon straight
    through, which meant a dict polygon either

      * silently "scoped" the THREATREP against a fence of two strings —
        `scoped_by_polygon: true`, `scoping_error: null`, `area_polygon`
        echoed back as [["lat","lon"], ...] — whenever the track store was
        empty, or
      * raised a bare TypeError out of the tool (an opaque "Error executing
        tool", not the contract's structured error) as soon as it held one.

    Normalizing once, here, and refusing a malformed vertex loudly is the fix.
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{param} must be a list of vertices, got {type(value).__name__}")
    out: list[tuple[float, float]] = []
    for i, p in enumerate(value):
        try:
            if isinstance(p, dict):
                lat, lon = float(p["lat"]), float(p["lon"])
            elif isinstance(p, (list, tuple)) and len(p) >= 2:
                lat, lon = float(p[0]), float(p[1])
            else:
                raise TypeError(type(p).__name__)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ValueError(
                f"{param}[{i}]={p!r} is not a vertex: pass {{'lat':..,'lon':..}} "
                f"or [lat, lon]. ({type(exc).__name__}: {exc})") from exc
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError(
                f"{param}[{i}]=({lat}, {lon}) is not a lat/lon pair — check the "
                "vertex order; the server will not guess.")
        out.append((lat, lon))
    return out


def _one_altitude(field: str, **spellings: float | None) -> float:
    """Resolve exactly one explicitly-datumed altitude argument.

    TOOL_CONTRACT: "never a bare alt_m". Tools keep the legacy `alt_m` spelling
    working so the shipped GEV panel and the existing journals stay valid, but
    when both are supplied and DISAGREE the call is refused — the server does
    not pick one silently, which is precisely how R3's 17.4 m datum
    disagreement survived for so long.
    """
    given = {k: float(v) for k, v in spellings.items() if v is not None}
    if not given:
        raise ValueError(
            f"{field}: supply one of {sorted(spellings)} — an altitude with no "
            "datum in its name is not accepted (TOOL_CONTRACT).")
    values = set(round(v, 6) for v in given.values())
    if len(values) > 1:
        raise ValueError(
            f"{field}: {given} disagree. Supply exactly one; the server will "
            "not choose an altitude for you.")
    return next(iter(given.values()))


class StaticBearerVerifier(TokenVerifier):
    """Static Bearer token check (T4e). Rejects anything else."""

    def __init__(self, token: str):
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != self._token:
            return None
        return AccessToken(token=token, client_id="godseye-harness", scopes=["uav"], expires_at=None)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a msgpack struct that may be an object or a dict.

    Real AirSim deserializes `DetectionInfo` into objects; the fake answers
    with plain dicts over the wire. Both must work, and a missing field comes
    back as `default` rather than being invented.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        if name in obj:
            return obj[name]
        key = name.encode()
        return obj.get(key, default)
    return getattr(obj, name, default)


def _xy(vec: Any) -> tuple[float, float] | None:
    x, y = _get(vec, "x_val"), _get(vec, "y_val")
    if x is None or y is None:
        return None
    return float(x), float(y)


def _xyz(vec: Any) -> tuple[float, float, float] | None:
    x, y, z = _get(vec, "x_val"), _get(vec, "y_val"), _get(vec, "z_val")
    if x is None or y is None or z is None:
        return None
    return float(x), float(y), float(z)


def _euler_deg(q: Any) -> dict | None:
    """Quaternion -> {roll_deg, pitch_deg, yaw_deg}. The ONE place this runs."""
    if q is None:
        return None
    w = _get(q, "w_val")
    x, y, z = _get(q, "x_val"), _get(q, "y_val"), _get(q, "z_val")
    if w is None or x is None or y is None or z is None:
        return None
    w, x, y, z = float(w), float(x), float(y), float(z)
    roll = math.degrees(math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y)))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2.0 * (w * y - z * x)))))
    yaw = math.degrees(math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z)))
    return {"roll_deg": round(roll, 3), "pitch_deg": round(pitch, 3),
            "yaw_deg": round(yaw, 3)}


def _box_pixels(box2d: Any) -> tuple[dict | None, float | None]:
    """Detection bounding box -> (serializable box, pixels on target).

    PLAN M7 needs pixels-on-target *measured*, not guessed from slant range.
    A detection with no box returns `None` for both — the caller must see that
    the measurement is absent instead of being handed a fabricated number.
    """
    lo = _xy(_get(box2d, "min"))
    hi = _xy(_get(box2d, "max"))
    if lo is None or hi is None:
        return None, None
    w, h = abs(hi[0] - lo[0]), abs(hi[1] - lo[1])
    box = {"min": {"x": round(lo[0], 2), "y": round(lo[1], 2)},
           "max": {"x": round(hi[0], 2), "y": round(hi[1], 2)},
           "width_px": round(w, 2), "height_px": round(h, 2)}
    return box, round(max(w, h), 2)


class UavBackend:
    """Thin async wrapper over the AirSim python client (real or fake).

    Blocking client calls run in a thread so the event loop stays free; the
    queue (T2) is what serializes per-vehicle work, not the client.

    **Datum (T1)**: `home` is a `geo.GeoPoint`, whose altitude is HAE by the
    module contract. A caller holding the theater's declared MSL elevation
    passes `home_datum="msl"` and the conversion happens ONCE, here, through
    `canonical_altitude`. Everything after that — NED math, telemetry, waypoint
    altitudes — is HAE, and `alt_msl_m` is derived back out at publication.
    """

    def __init__(self, client: Any, home: GeoPoint, sim: Any = None,
                 home_datum: str = "hae", allow_approx_datum: bool = False):
        self.client = client
        self.home_declared = home
        self.home_fix = canonical_altitude(
            home.altitude, home.latitude, home.longitude,
            datum=home_datum, allow_approx=allow_approx_datum)
        self.home_geo = GeoPoint(home.latitude, home.longitude, self.home_fix.alt_hae)
        # Per-thread isolated client to dodge tornado-4.5 "IOLoop is already
        # running" when RPCs run inside asyncio.to_thread (see rpc_patch).
        self._local = threading.local()
        self.home = HomeGeoPoint.from_geo(self.home_geo)
        # Optional direct handle to a FakeAirSim for sim-only realism hooks
        # (GPS denial M16 / sensor noise M17). Real AirSim exposes these via
        # sensor settings instead; wind (M15) works over RPC on both.
        self.sim = sim
        #: Last wind vector commanded through `sim_set_environment` (M15). Used
        #: only when the sim cannot be asked for its own — the source is always
        #: reported alongside the value, never silently substituted.
        self.commanded_wind_ne: tuple[float, float] | None = None

    # ---- datum-aware conversions (T1) ----
    def llh_to_ned(self, lat: float, lon: float, alt_hae: float) -> tuple[float, float, float]:
        ned = geodetic_to_ned(GeoPoint(lat, lon, alt_hae), self.home_geo)
        return ned.x, ned.y, ned.z

    def llh_agl_to_ned(self, lat: float, lon: float, alt_agl_m: float) -> tuple[float, float, float]:
        """Waypoint altitudes are AGL above the launch datum; the origin is
        already HAE (T1), so an AGL waypoint is simply home_hae + agl."""
        abs_alt = self.home_geo.altitude + float(alt_agl_m)
        return self.llh_to_ned(lat, lon, abs_alt)

    def ned_to_llh(self, n: float, e: float, d: float) -> tuple[float, float, float]:
        """NED -> (lat, lon, alt_HAE). The origin altitude is HAE, so this is."""
        geo = ned_to_geodetic(NedPoint(n, e, d), self.home)
        return geo.latitude, geo.longitude, geo.altitude

    def _thread_client(self):
        c = getattr(self._local, "client", None)
        if c is None:
            from .rpc_patch import make_client
            addr = getattr(self.client, "client", None)
            ip = getattr(getattr(addr, "_address", None), "host", "127.0.0.1")
            port = getattr(getattr(addr, "_address", None), "port", 41451)
            c, _ = make_client(ip=ip, port=port)
            self._local.client = c
        return c

    async def _call(self, fn, *args, **kwargs):
        """Run a blocking RPC on the calling thread's own isolated client.

        The method name is re-resolved against the per-thread client INSIDE the
        worker thread. The previous form tested `hasattr(self._thread_client
        .__class__, name)` — `self._thread_client` is a bound method, so that
        was always False and every RPC went through the shared main-thread
        client. That is invisible while calls are serialized and fatal the
        moment two loops call at once ("IOLoop is already running"), which is
        exactly what the safety monitor + the queue worker now do.
        """
        name = getattr(fn, "__name__", None)

        def _run():
            if name:
                bound = getattr(self._thread_client(), name, None)
                if bound is not None:
                    return bound(*args, **kwargs)
            return fn(*args, **kwargs)

        return await asyncio.to_thread(_run)

    async def _raw_call(self, method: str, *args):
        """Call an RPC by name (fake-only extensions the typed client lacks)."""
        c = self._thread_client()
        rpc = getattr(c, "client", None)
        if rpc is None:
            raise RuntimeError("no msgpack-rpc channel on the AirSim client")
        return await asyncio.to_thread(rpc.call, method, *args)

    async def list_vehicles(self) -> list[str]:
        """The sim's vehicle roster. A failure RAISES — it is never invented.

        This used to be `except Exception: return ["Drone1"]`. Every failure —
        an RPC error, a lost datalink, a sim that never came up — reported one
        hardcoded aircraft, to `uav_list_vehicles` AND to the safety monitor's
        vehicle list. A harness was then told a fleet of four was a fleet of
        one, and the monitor ticked fuel and geofence for a name it had made up
        while the real airframes went unwatched. Callers must handle the
        exception and surface the degradation; none of them may guess a roster.
        """
        vehicles = await self._call(self.client.listVehicles)
        return list(vehicles)

    async def telemetry(self, vehicle: str) -> dict:
        """Live telemetry with BOTH datums resolved at the one conversion point."""
        state = await self._call(self.client.getMultirotorState, vehicle_name=vehicle)
        kin = state.kinematics_estimated
        pos = kin.position
        gps = await self._call(self.client.getGpsData, vehicle_name=vehicle)
        lat, lon, alt_hae = self.ned_to_llh(pos.x_val, pos.y_val, pos.z_val)
        fix = canonical_altitude(alt_hae, lat, lon, datum="hae")
        vel = kin.linear_velocity
        speed = math.sqrt(vel.x_val**2 + vel.y_val**2 + vel.z_val**2)
        # AGL above the launch datum (flat-world sim; terrain relief is M19).
        # `+ 0.0` normalizes the -0.0 that negating a zero NED z produces.
        alt_agl = -float(pos.z_val) + 0.0
        from .safety import track_deg_from_velocity
        return {
            "vehicle": vehicle,
            "lat": lat,
            "lon": lon,
            # T1: three explicit datums, never a bare alt_m (TOOL_CONTRACT §4.2).
            "alt_hae_m": round(fix.alt_hae, 3),
            "alt_msl_m": round(fix.alt_msl, 3),
            "alt_agl_m": round(alt_agl, 3),
            "undulation_m": round(fix.undulation_m, 3),
            "datum_source": fix.source,
            "datum_degraded": fix.degraded,
            # Same value as alt_hae_m; kept because the bridge/GEV layer reads
            # this key. It is the canonical altitude now, not the old raw one.
            "alt_hae": round(fix.alt_hae, 3),
            "ned": [pos.x_val, pos.y_val, pos.z_val],
            "speed_mps": round(speed, 2),
            "vx_mps": round(vel.x_val, 3),
            "vy_mps": round(vel.y_val, 3),
            "vz_mps": round(vel.z_val, 2),
            "track_deg": track_deg_from_velocity(vel.x_val, vel.y_val),
            "landed_state": int(state.landed_state),
            "attitude": self._attitude(kin),
            "gps": {"lat": gps.gnss.geo_point.latitude, "lon": gps.gnss.geo_point.longitude},
        }

    @staticmethod
    def _attitude(kin: Any) -> dict:
        q = _get(kin, "orientation")
        w = _get(q, "w_val", 1.0)
        x, y, z = _get(q, "x_val", 0.0), _get(q, "y_val", 0.0), _get(q, "z_val", 0.0)
        roll = math.degrees(math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y)))
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2.0 * (w * y - z * x)))))
        yaw = math.degrees(math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z)))
        return {"roll_deg": round(roll, 2), "pitch_deg": round(pitch, 2),
                "yaw_deg": round(yaw % 360.0, 2)}

    async def wind_ne(self) -> tuple[tuple[float, float], str]:
        """Current NED wind (north, east) m/s and where the number came from (M15)."""
        if self.sim is not None:
            try:
                w = self.sim.wind()
                return (float(w.x), float(w.y)), "sim"
            except Exception:
                pass
        try:
            env = await self._raw_call("simGetEnvironment")
            wind = _get(env, "wind") or {}
            return (float(_get(wind, "north", 0.0)), float(_get(wind, "east", 0.0))), "sim_rpc"
        except Exception:
            pass
        if self.commanded_wind_ne is not None:
            return self.commanded_wind_ne, "commanded"
        return (0.0, 0.0), "unset"

    async def environment(self) -> dict | None:
        """Sim environment (sun, weather, wind) for the INTREP, or None."""
        if self.sim is not None:
            try:
                return self.sim.environment()
            except Exception:
                return None
        try:
            return await self._raw_call("simGetEnvironment")
        except Exception:
            return None

    async def camera_fov_deg(self, vehicle: str, camera: str = "0") -> float | None:
        try:
            info = await self._call(self.client.simGetCameraInfo, camera, vehicle)
        except Exception:
            return None
        fov = _get(info, "fov")
        return float(fov) if fov is not None else None

    async def link_state(self, vehicle: str) -> str | None:
        """Datalink health as the sim reports it (M9), or None if unavailable."""
        if self.sim is not None:
            try:
                return str(self.sim.link_state(vehicle)["state"])
            except Exception:
                return None
        try:
            st = await self._raw_call("simGetLinkState", vehicle)
            return str(_get(st, "state"))
        except Exception:
            return None

    # ---- flight ----
    async def takeoff(self, vehicle: str, alt_agl_m: float = 3.0, timeout_s: float = 60.0,
                      task: Task | None = None, *,
                      pct_lo: float = 0.0, pct_hi: float = 100.0) -> None:
        await self._call(self.client.enableApiControl, True, vehicle)
        await self._call(self.client.armDisarm, True, vehicle)
        await self._call(self.client.takeoffAsync, vehicle_name=vehicle)
        # Command-ack (T2): futures may resolve instantly, so confirm the climb
        # on telemetry. NED z is negative-up; target = -alt_agl_m.
        target_z = -max(3.0, float(alt_agl_m))
        if alt_agl_m > 3.0:
            await self._call(self.client.moveToZAsync, target_z, 3.0, vehicle_name=vehicle)
        start = time.monotonic()
        z0 = (await self.telemetry(vehicle))["ned"][2]
        span = abs(target_z - z0) or 1.0
        # Only a forward step larger than `PROGRESS_EPS_PCT` resets the
        # no-progress watchdog, so a tall climb reported every poll moves the
        # bar by less than the epsilon each time and reads as STALLED while it
        # is climbing perfectly well. Report on material steps instead: the
        # band still carries ~1/eps of them, which is far more than a climb
        # needs, and every one of them is a real reset.
        last_reported = pct_lo - PROGRESS_EPS_PCT
        while time.monotonic() - start < timeout_s:
            t = await self.telemetry(vehicle)
            z = t["ned"][2]
            if task is not None:
                # Capped below 100: only the queue publishes 100 %, and only
                # as the task becomes terminal (EXEC_PROGRESS_CEILING).
                # `pct_lo`/`pct_hi` map the climb onto a band, exactly as
                # `land` does, so an auto-takeoff that is the HEAD of a longer
                # command reports inside a reserved slice instead of running
                # the whole bar to 99 % before the route has flown a metre.
                pct = min(pct_lo + (pct_hi - pct_lo) * min(1.0, abs(z - z0) / span),
                          EXEC_PROGRESS_CEILING)
                if pct > last_reported + PROGRESS_EPS_PCT:
                    last_reported = pct
                    await task.report_progress(
                        pct, note=f"climb {-z:.0f}/{-target_z:.0f} m AGL")
            if z <= target_z + 1.0:
                return
            await asyncio.sleep(0.2)
        raise TimeoutError(f"takeoff did not reach {-target_z:.0f} m AGL in {timeout_s:.0f}s")

    async def land(self, vehicle: str, timeout_s: float = 120.0,
                   task: Task | None = None, *, blind_ok: bool = False,
                   blind_timeout_s: float | None = None,
                   pct_lo: float = 0.0, pct_hi: float = 100.0) -> dict:
        """Land, confirming the descent on telemetry.

        `pct_lo`/`pct_hi` map the descent onto a band of the task's progress,
        so a landing that is the TAIL of a longer command (the RTB) reports
        from where the route left off instead of restarting the bar at zero —
        and only reaches 100 % when the aircraft is actually down.

        `blind_ok` is for a SAFETY transition only (M9): the airframe is still
        commandable with the datalink down — only the sensing is gone — so the
        landing is commanded and the return says the touchdown was never
        OBSERVED rather than raising and abandoning the aircraft mid-air.
        """
        await self._call(self.client.landAsync, vehicle_name=vehicle)
        # Command-ack on telemetry, same doctrine as takeoff: the fake's future
        # resolves instantly and a real one blocks the executor thread.
        if blind_timeout_s is None:
            blind_timeout_s = BLIND_LAND_S
        start = time.monotonic()
        blind_err: str | None = None
        try:
            z0 = (await self.telemetry(vehicle))["ned"][2]
        except Exception as exc:  # noqa: BLE001 — re-raised unless blind_ok
            if not blind_ok:
                raise
            blind_err = f"{type(exc).__name__}: {exc}"
            z0 = None
        span = abs(z0 or 0.0) or 1.0
        while time.monotonic() - start < timeout_s:
            try:
                t = await self.telemetry(vehicle)
            except Exception as exc:  # noqa: BLE001
                if not blind_ok:
                    raise
                blind_err = f"{type(exc).__name__}: {exc}"
                if time.monotonic() - start > blind_timeout_s:
                    break
                await asyncio.sleep(0.25)
                continue
            if z0 is None:
                z0 = t["ned"][2]
                span = abs(z0) or 1.0
            if task is not None:
                frac = min(1.0, max(0.0, (abs(z0) - abs(t["ned"][2])) / span))
                await task.report_progress(
                    min(pct_lo + (pct_hi - pct_lo) * frac, EXEC_PROGRESS_CEILING),
                    note="descent",
                    eta_s=max(0.0, abs(t["ned"][2])) / NOMINAL_DESCENT_MPS)
            if int(t.get("landed_state", 0)) == 0 or t["ned"][2] >= -0.5:
                if task is not None:
                    # Not 100: the queue writes that, and only once the task is
                    # actually terminal (EXEC_PROGRESS_CEILING).
                    await task.report_progress(
                        min(pct_hi, EXEC_PROGRESS_CEILING), note="touchdown",
                        eta_s=0.0)
                return {"landed": True, "verified": True}
            await asyncio.sleep(0.2)
        if blind_err is not None:
            return {"landed": False, "verified": False,
                    "reason": f"landing commanded, touchdown never observed: {blind_err}"}
        raise TimeoutError(f"land did not complete in {timeout_s:.0f}s")

    async def goto_ned(self, vehicle: str, n: float, e: float, d: float, speed: float,
                       on_progress: Callable[[float, float], Awaitable[None]] | None = None,
                       timeout_s: float | None = None, *, blind_ok: bool = False,
                       start_ned: tuple[float, float, float] | None = None) -> dict:
        # Command-ack (T2): the fake sim's futures resolve instantly, and real
        # AirSim futures block the executor thread. Progress must come from
        # telemetry, so wait for the position to converge on the target NED.
        await self._call(self.client.moveToPositionAsync, n, e, d, speed, vehicle_name=vehicle)
        return await self._wait_arrival(vehicle, n, e, d, on_progress=on_progress,
                                        timeout_s=timeout_s, speed=speed,
                                        blind_ok=blind_ok, start_ned=start_ned)

    async def _wait_arrival(self, vehicle: str, n: float, e: float, d: float,
                            tol_m: float = 5.0, timeout_s: float | None = None,
                            speed: float = 10.0,
                            on_progress: Callable[[float, float], Awaitable[None]] | None = None,
                            *, blind_ok: bool = False,
                            start_ned: tuple[float, float, float] | None = None) -> dict:
        """Poll telemetry until the leg closes.

        `timeout_s` is derived from the leg the caller actually planned (3x the
        nominal flight time + a minute) instead of the old flat 180 s, which
        killed any leg longer than 1.8 km. The queue's no-progress watchdog is
        what catches a stuck vehicle (T2); this is only a backstop.

        `blind_ok` (SAFETY tasks only, M9): a lost datalink removes telemetry
        but NOT the ability to command the airframe. Raising here failed the
        lost-link RTB 0.6 ms after it started — under exactly the condition the
        RTB exists for. With `blind_ok` the leg is flown to its deadline and
        the return reports `verified: False`; arrival is never CLAIMED.
        """
        start = time.monotonic()
        blind_err: str | None = None
        if start_ned is None:
            try:
                start_ned = tuple((await self.telemetry(vehicle))["ned"])
            except Exception as exc:  # noqa: BLE001 — re-raised unless blind_ok
                if not blind_ok:
                    raise
                blind_err = f"{type(exc).__name__}: {exc}"
        leg_m = math.dist(tuple(start_ned), (n, e, d)) if start_ned is not None else 0.0
        if timeout_s is None:
            timeout_s = max(120.0, 3.0 * leg_m / max(1.0, speed) + 60.0)
        while True:
            try:
                t = await self.telemetry(vehicle)
            except Exception as exc:  # noqa: BLE001
                if not blind_ok:
                    raise
                blind_err = f"{type(exc).__name__}: {exc}"
                # Blind, so there is nothing to converge on. Wait the leg's
                # NOMINAL flight time plus a grace, not the sighted backstop:
                # holding a blind safety task open for the full 3x window would
                # stall the recovery behind it.
                blind_deadline = min(timeout_s,
                                     leg_m / max(1.0, speed) + BLIND_LEG_GRACE_S)
                if time.monotonic() - start > blind_deadline:
                    return {"arrived": False, "verified": False, "leg_m": leg_m,
                            "reason": ("leg commanded, arrival never observed: "
                                       f"{blind_err}")}
                await asyncio.sleep(0.25)
                continue
            x, y, z = t["ned"]
            dist = math.dist((x, y, z), (n, e, d))
            if on_progress is not None:
                await on_progress(dist, leg_m or dist)
            if dist <= tol_m:
                return {"arrived": True, "verified": True, "leg_m": leg_m,
                        "blind_recovered": blind_err}
            if time.monotonic() - start > timeout_s:
                raise TimeoutError(
                    f"arrival timeout after {timeout_s:.0f}s (dist {dist:.1f} m of {leg_m:.1f} m)")
            await asyncio.sleep(0.25)

    async def hover(self, vehicle: str) -> None:
        fut = await self._call(self.client.hoverAsync, vehicle_name=vehicle)
        await self._call(fut.join)

    async def cancel_last(self, vehicle: str) -> None:
        await self._call(self.client.cancelLastTask, vehicle_name=vehicle)

    async def get_detections(self, vehicle: str, camera: str = "0",
                             image_type: int = 0) -> list[dict]:
        """Raw ground-truth detections (DetectionInfo) within the filter radius.

        The 2-D box and the relative pose are KEPT: pixels-on-target is the
        measurement the M7 cross-cue and the confidence model need, and
        throwing the box away forced it to be guessed from slant range.
        """
        dets = await self._call(
            self.client.simGetDetections, camera, image_type, vehicle)
        out = []
        for d in dets or []:
            gp = _get(d, "geo_point")
            box, pixels = _box_pixels(_get(d, "box2D"))
            rel = _xyz(_get(_get(d, "relative_pose"), "position"))
            out.append({
                "name": _get(d, "name", "unknown"),
                "geo_point": {"latitude": _get(gp, "latitude"),
                              "longitude": _get(gp, "longitude"),
                              "altitude": _get(gp, "altitude")},
                "box2D": box,
                "pixels_on_target": pixels,
                "relative_pose_ned": (list(rel) if rel else None),
                "slant_range_m": (round(math.dist((0.0, 0.0, 0.0), rel), 2) if rel else None),
            })
        return out

    async def spawn_object(self, name: str, mesh: str, lat: float, lon: float,
                           alt_hae_m: float, heading_deg: float = 0.0) -> str:
        from airsim import Pose, Vector3r, to_quaternion
        n, e, d = self.llh_to_ned(lat, lon, alt_hae_m)
        pose = Pose(Vector3r(n, e, d),
                    to_quaternion(0.0, 0.0, math.radians(float(heading_deg))))
        return await self._call(self.client.simSpawnObject, name, mesh, pose,
                                Vector3r(1, 1, 1), False, True)

    # ---- camera: gimbal + FOV (M7 wide->narrow cross-cue) ----
    async def set_camera_pose(self, vehicle: str, camera: str, pitch_deg: float,
                              yaw_deg: float, roll_deg: float = 0.0) -> dict:
        """Slew the gimbal. Angles are BODY-relative degrees, pitch +up."""
        from airsim import Pose, Vector3r, to_quaternion
        pose = Pose(Vector3r(0.0, 0.0, 0.0),
                    to_quaternion(math.radians(pitch_deg), math.radians(roll_deg),
                                  math.radians(yaw_deg)))
        await self._call(self.client.simSetCameraPose, camera, pose, vehicle)
        return await self.camera_state(vehicle, camera)

    async def set_camera_fov(self, vehicle: str, camera: str, fov_deg: float) -> dict:
        """Set the horizontal FOV and READ IT BACK — a commanded FOV the sim
        did not accept would silently invalidate every M1/M5/M7 footprint
        number derived from it."""
        await self._call(self.client.simSetCameraFov, camera, float(fov_deg), vehicle)
        return await self.camera_state(vehicle, camera)

    async def camera_state(self, vehicle: str, camera: str = "0") -> dict:
        """Live camera pose + FOV as the sim reports it (never as commanded)."""
        info = await self._call(self.client.simGetCameraInfo, camera, vehicle)
        euler = _euler_deg(_get(_get(info, "pose"), "orientation")) or {}
        fov = _get(info, "fov")
        return {"camera": camera, "vehicle": vehicle,
                "fov_deg": (round(float(fov), 4) if fov is not None else None),
                "pitch_deg": euler.get("pitch_deg"), "yaw_deg": euler.get("yaw_deg"),
                "roll_deg": euler.get("roll_deg"), "source": "simGetCameraInfo"}

    # ---- imagery (§4.2) ----
    async def capture(self, vehicle: str, camera: str, image_type: int) -> dict:
        """One frame + the camera pose the sim actually used for it."""
        from airsim import ImageRequest
        req = ImageRequest(camera, int(image_type), False, True)
        responses = await self._call(self.client.simGetImages, [req], vehicle)
        if not responses:
            raise RuntimeError(
                f"simGetImages returned no frame for {vehicle}/{camera} "
                f"type={image_type}")
        r = responses[0]
        raw = _get(r, "image_data_uint8")
        if isinstance(raw, str):        # the fake base64s a compressed frame
            data = base64.b64decode(raw)
        elif isinstance(raw, (bytes, bytearray)):
            data = bytes(raw)
        elif isinstance(raw, list):
            data = bytes(raw)
        else:
            raise RuntimeError(
                f"simGetImages returned {type(raw).__name__} image data for "
                f"{vehicle}/{camera}; no pixels were produced")
        if not data:
            raise RuntimeError(
                f"simGetImages returned an EMPTY frame for {vehicle}/{camera}")
        return {"png": data, "width": int(_get(r, "width", 0) or 0),
                "height": int(_get(r, "height", 0) or 0),
                "camera_position_ned": _xyz(_get(r, "camera_position")),
                "camera_orientation": _get(r, "camera_orientation"),
                "time_stamp": _get(r, "time_stamp")}

    # ---- line of sight (§4.2, M5) ----
    def _sync_rpc(self, method: str, *args) -> Any:
        """Blocking raw RPC on the CALLING thread's own client.

        The mission planners take a synchronous `los_check` callable (M5), so
        the planner runs in a worker thread and reaches the sim through this.
        """
        c = self._thread_client()
        rpc = getattr(c, "client", None)
        if rpc is None:
            raise RuntimeError("no msgpack-rpc channel on the AirSim client")
        return rpc.call(method, *args)

    @staticmethod
    def _geo_arg(lat: float, lon: float, alt_hae_m: float) -> dict:
        return {"latitude": float(lat), "longitude": float(lon),
                "altitude": float(alt_hae_m)}

    def los_to_point_sync(self, vehicle: str, lat: float, lon: float,
                          alt_hae_m: float) -> dict:
        """Vehicle -> point LOS. Never returns an unconditional true."""
        point = self._geo_arg(lat, lon, alt_hae_m)
        failures: list[str] = []
        try:
            info = self._sync_rpc("simGetLineOfSightInfo", point, vehicle)
            return {"los": bool(_get(info, "los")),
                    "first_obstacle": _get(info, "first_obstacle"),
                    "first_obstacle_modelled": True,
                    "model": LOS_MODELS["sim_los_info"],
                    "source": "simGetLineOfSightInfo"}
        except Exception as exc:  # noqa: BLE001 — recorded, then the next model
            failures.append(f"simGetLineOfSightInfo: {type(exc).__name__}: {exc}")
        try:
            los = self._thread_client().client.call(
                "simTestLineOfSightToPoint", point, vehicle)
            return {"los": bool(los), "first_obstacle": None,
                    "first_obstacle_modelled": False,
                    "model": LOS_MODELS["airsim_scene_trace"],
                    "source": "simTestLineOfSightToPoint"}
        except Exception as exc:  # noqa: BLE001 — reported, never assumed clear
            failures.append(f"simTestLineOfSightToPoint: {type(exc).__name__}: {exc}")
        raise LosModelUnavailableError(
            "no line-of-sight model answered for "
            f"{vehicle} -> {lat:.6f},{lon:.6f}: {'; '.join(failures)}")

    def los_between_sync(self, a: tuple[float, float, float],
                         b: tuple[float, float, float]) -> dict:
        """Point -> point LOS (altitudes HAE). Used by the M5 ring verifier."""
        if self.sim is not None:
            try:
                los, obstacle = self.sim.line_of_sight(GeoPoint(*a), GeoPoint(*b))
                return {"los": bool(los), "first_obstacle": obstacle,
                        "first_obstacle_modelled": True,
                        "model": LOS_MODELS["sim_los_info"],
                        "source": "fake_airsim.line_of_sight"}
            except Exception as exc:  # noqa: BLE001 — fall through to the RPC
                failures = [f"fake_airsim.line_of_sight: {type(exc).__name__}: {exc}"]
        else:
            failures = []
        try:
            los = self._sync_rpc("simTestLineOfSightBetweenPoints",
                                 self._geo_arg(*a), self._geo_arg(*b))
            return {"los": bool(los), "first_obstacle": None,
                    "first_obstacle_modelled": False,
                    "model": LOS_MODELS["airsim_scene_trace_between"],
                    "source": "simTestLineOfSightBetweenPoints"}
        except Exception as exc:  # noqa: BLE001 — reported, never assumed clear
            failures.append(f"simTestLineOfSightBetweenPoints: "
                            f"{type(exc).__name__}: {exc}")
        raise LosModelUnavailableError(
            f"no line-of-sight model answered between {a} and {b}: "
            f"{'; '.join(failures)}")

    async def los_to_point(self, vehicle: str, lat: float, lon: float,
                           alt_hae_m: float) -> dict:
        return await asyncio.to_thread(self.los_to_point_sync, vehicle, lat, lon,
                                       alt_hae_m)

    # ---- sim admin (§4.4) ----
    async def sun(self, vehicle: str = "Drone1") -> dict | None:
        """Sun azimuth/elevation at the vehicle (M6), or None if unmodelled."""
        try:
            st = await self._raw_call("simGetSunPosition", vehicle)
        except Exception:  # noqa: BLE001 — the caller reports the absence
            env = await self.environment()
            if not env or env.get("sun_azimuth_deg") is None:
                return None
            return {"azimuth_deg": env.get("sun_azimuth_deg"),
                    "elevation_deg": env.get("sun_elevation_deg"),
                    "is_day": env.get("is_day"), "sim_time": env.get("sim_time"),
                    "source": "simGetEnvironment"}
        return {"azimuth_deg": _get(st, "azimuth_deg"),
                "elevation_deg": _get(st, "elevation_deg"),
                "is_day": _get(st, "is_day"), "sim_time": _get(st, "sim_time"),
                "source": "simGetSunPosition"}

    async def set_time(self, datetime_str: str | None, clock_speed: float,
                       enabled: bool = True) -> None:
        await self._call(self.client.simSetTimeOfDay, bool(enabled),
                         datetime_str or "", False, float(clock_speed), 60, True)

    async def set_weather(self, values: dict[str, float]) -> None:
        await self._call(self.client.simEnableWeather, True)
        for key, val in values.items():
            await self._call(self.client.simSetWeatherParameter,
                             WEATHER_PARAMS[key], float(val))

    async def set_wind(self, north: float, east: float, down: float = 0.0) -> None:
        from airsim import Vector3r
        await self._call(self.client.simSetWind, Vector3r(north, east, down))
        self.commanded_wind_ne = (float(north), float(east))

    async def set_link_state(self, vehicle: str, state: str,
                            duration_s: float = 0.0) -> dict:
        return await self._raw_call("simSetLinkState", vehicle, state,
                                    float(duration_s))

    async def set_object_route(self, name: str, waypoints: list[dict],
                               speed_mps: float, loop: bool = False) -> None:
        await self._raw_call("simSetObjectRoute", name, waypoints,
                             float(speed_mps), bool(loop))

    async def object_pose(self, name: str) -> tuple[float, float, float] | None:
        """Spawned object position as (lat, lon, alt_hae) or None if gone."""
        pose = await self._call(self.client.simGetObjectPose, name)
        ned = _xyz(_get(pose, "position"))
        if ned is None or any(math.isnan(v) for v in ned):
            return None
        return self.ned_to_llh(*ned)

    async def reset(self) -> None:
        await self._call(self.client.reset)


class GodseyeUavServer:
    """Wires safety + tasking + store + backend into the MCP tool catalog."""

    def __init__(
        self,
        backend: UavBackend,
        store: Store,
        envelope: SafetyEnvelope | None = None,
        fuels: dict[str, FuelModel] | None = None,
        token: str = "dev-token",
        watchdog_s: float = 120.0,
        theater: Theater | str | None = None,
        lost_link_plan: LostLinkPlan | dict | None = None,
        real_data: Any = None,
    ):
        self.backend = backend
        self.store = store
        # Theater is the single source of truth for home + AO (E/M20): an
        # explicit envelope still wins, but the fallback is a real theater
        # rather than an unconstrained, home-less SafetyEnvelope.
        self.theater = theater if isinstance(theater, Theater) else theaters.get(theater)
        self.envelope = envelope or SafetyEnvelope(**self.theater.envelope_kwargs())
        #: Set when an explicit `envelope` was built for a DIFFERENT theater
        #: than the one this server resolved (`launch.py` passes `envelope=`
        #: but not `theater=`, so `--theater iran-isfahan` gets the Isfahan
        #: geofence and the Redmond theater). Everything keyed off
        #: `self.theater` is then wrong for the AO actually being flown:
        #: `sim_spawn_target`'s default ground elevation above all, which
        #: buries a target ~1.4 km underground in the mountain theaters — the
        #: exact §4.4 defect the contract calls out. Reported loudly rather
        #: than silently mis-configured; `uav://safety/geofence` carries it.
        self.theater_mismatch: dict | None = None
        if envelope is not None and envelope.home is not None:
            off_m = haversine_m(envelope.home[0], envelope.home[1],
                                self.theater.home_lat, self.theater.home_lon)
            if off_m > THEATER_HOME_TOLERANCE_M:
                self.theater_mismatch = {
                    "theater_id": self.theater.id,
                    "theater_home": [self.theater.home_lat, self.theater.home_lon],
                    "theater_ground_elevation_msl_m": self.theater.home_alt_msl_m,
                    "envelope_home": list(envelope.home),
                    "offset_m": round(off_m, 1),
                    "impact": ("sim_spawn_target's default ground elevation, the "
                               "seeded pattern-of-life POIs and the INTREP area "
                               "name all come from the theater, not the "
                               "envelope. Pass theater= alongside envelope=."),
                }
                self.store.log_audit(
                    "theater_mismatch",
                    f"envelope home is {off_m / 1000.0:.1f} km from theater "
                    f"{self.theater.id!r}'s home",
                    **self.theater_mismatch)
        self.lost_link_plan = (lost_link_plan if isinstance(lost_link_plan, LostLinkPlan)
                               else LostLinkPlan.from_dict(lost_link_plan))
        self.monitors: dict[str, SafetyMonitor] = {}
        for name, fm in (fuels or {}).items():
            self.monitors[name] = SafetyMonitor(envelope=self.envelope, fuel=fm,
                                                link=self._new_link_monitor())
        # watchdog_s is now a NO-PROGRESS window, not a cap on total duration.
        self.tasking = TaskingService(watchdog_s=watchdog_s)
        self.tasking.set_executor(self._execute)
        self.tasking.set_recovery(self._watchdog_recovery)
        # T4c: the ONE writer of a task's terminal journal row. Registered on
        # the service (not a queue) so a vehicle that first appears mid-run is
        # covered too.
        self.tasking.add_terminal_hook(self._journal_terminal_task)
        #: Non-None when the sim could not be asked for its vehicle roster.
        #: A degradation the harness can read, never a hardcoded fleet.
        self.vehicle_roster_error: str | None = None
        from .targets import PatternOfLife, TrackManager
        self.tracks = TrackManager()
        # M12: one pattern-of-life store, seeded with the theater's POIs, so
        # intent indicator 3 of 4 stops reporting "no_store" forever.
        self.pol = PatternOfLife()
        for poi in self.theater.pois:
            self.pol.define_poi(poi.name, poi.lat, poi.lon)
        self.missions: dict[str, dict] = {}
        #: mission_id -> the handle exactly as it was first returned (T4b).
        #: A replayed idempotency_key is answered from here, so the harness
        #: gets back the handle the mission is actually filed under.
        self._mission_handles: dict[str, dict] = {}
        self.mission_flags: dict[str, str] = {}
        #: M5 re-path loop state, keyed by mission id: the LIVE `MissionPlan`
        #: the vehicle is flying plus the LOS policy it was planned under, so
        #: the executor can re-centre the orbit on a contact that moves
        #: (PLAN §4.3 "server re-path loop"). Populated only for
        #: `track_target`; dropped when the mission's task goes terminal.
        self._repath: dict[str, dict] = {}
        self.loal_events: list[dict] = []
        self.ticks: dict[str, dict] = {}
        #: Sim ground truth behind `sim_spawn_target` (uav://targets, §4.8).
        #: This is what was PLACED — never confused with `self.tracks`, which
        #: is what the sensor actually derived (M11).
        self.targets: dict[str, dict] = {}
        #: Generated reports, keyed by report id (uav://reports/{id}, §4.7).
        self.reports: dict[str, dict] = {}
        #: Last captured frame per (vehicle, camera, type)
        #: (uav://{vehicle}/camera/{name}/{type}, §4.8).
        self.frames: dict[tuple[str, str, str], dict] = {}
        #: task_id -> live M2 capture tally, so a mission's imagery collection
        #: is observable WHILE it flies and not only from the finished result.
        self._capture_progress: dict[str, dict] = {}
        #: T4b for mutating tools that do NOT go through the task queue
        #: (gimbal, FOV, sim admin): key -> the ORIGINAL result.
        self._idem_results: dict[str, dict] = {}
        self._idem_lock = threading.Lock()
        self.sim_resets: list[dict] = []
        self._last_tele: dict[str, dict] = {}
        #: Altitude-limit kinds currently raised against MEASURED terrain per
        #: vehicle. Edge state, so `_merge_terrain_floor` journals the 0->1 and
        #: 1->0 transitions rather than one row per 0.5 s tick.
        self._terrain_floor_active: dict[str, set[str]] = {}
        self._missed_ticks: dict[str, int] = {}
        #: What `_restore_persisted_state` put back at boot (T4c). Set by
        #: `_boot_replay`; initialised here so the safety resource can be read
        #: even if construction never reaches the replay.
        self.boot_restored: dict = {}
        self._harness_down: set[str] = set()
        self._harness_down_all = False
        self._rtb_active: dict[str, str] = {}
        #: The safety task each vehicle was last committed to. `_force_rtb`
        #: de-duplicates on this being ALIVE, never on the reason alone.
        self._rtb_task: dict[str, Task] = {}
        self._safety_seq = 0
        self._monitor_task: Any = None
        self._monitor_stop = threading.Event()
        self._monitor_errors: list[str] = []
        # ---- real-world data (REAL_DATA_INTEGRATION.md) ----
        #: The ingestion client, or None when the layer is OFF. Resolved before
        #: the tools are registered so their descriptions can say which it is.
        #: A bad `GODSEYE_REAL_DATA` raises HERE, at construction, rather than
        #: leaving a server that quietly flies on synthetic terrain.
        self.real: Any = resolve_real_data(real_data)
        #: Last hydration for this server's theater (`realdata.TheaterRealData`).
        #: Never fetched on a hot path; `hydrate_real_data()` and the background
        #: refresher are the only writers.
        self.real_world: Any = None
        #: Why the last hydration failed, if it did. Surfaced, never swallowed.
        self.real_data_error: str | None = None
        #: Terrain points a background prefetch is already chasing, and how many
        #: prefetch threads are running (the cap is on threads, not points).
        self._terrain_inflight: set[str] = set()
        self._terrain_prefetches = 0
        self._terrain_lock = threading.Lock()
        #: The ONE terrain lattice: what `terrain_at` snaps to and what an AO
        #: load fetches. Fitted to this theater's AO so the whole area fits
        #: inside one bounded load; `prefetch_ao_terrain` may coarsen it, never
        #: refine it past the fit.
        self._fitted_terrain_grid_deg = terrain_grid_for(self.theater.bbox())
        self.terrain_grid_deg = self._fitted_terrain_grid_deg
        self._real_refresher: Any = None
        #: The real weather this server pushed into the sim, if it did (M15/M18).
        self.real_weather_applied: dict | None = None
        self.mcp = MCPServer(
            "godseye-uav",
            token_verifier=StaticBearerVerifier(token),
            auth=AuthSettings(
                issuer_url="http://127.0.0.1:8791",
                resource_server_url="http://127.0.0.1:8791",
                required_scopes=["uav"],
            ),
        )
        self._register_tools()
        self._register_resources()
        # T4c: restart = replay -> resume-or-abort-and-RTH. Runs at boot so the
        # decision exists before the first command is accepted.
        self.recovery = self._boot_replay()

    # ---- per-vehicle safety state ----
    def _new_link_monitor(self):
        from .safety import LostLinkMonitor
        return LostLinkMonitor(plan=LostLinkPlan.from_dict(self.lost_link_plan.to_dict()))

    def monitor_for(self, vehicle: str) -> SafetyMonitor:
        mon = self.monitors.get(vehicle)
        if mon is None:
            fm = FuelModel()
            fm.home = self.envelope.home
            mon = SafetyMonitor(envelope=self.envelope, fuel=fm,
                                link=self._new_link_monitor())
            self.monitors[vehicle] = mon
        return mon

    def fuel_for(self, vehicle: str) -> FuelModel:
        return self.monitor_for(vehicle).fuel

    @property
    def fuels(self) -> dict[str, FuelModel]:
        return {v: m.fuel for v, m in self.monitors.items()}

    # ---------------------------------------------------------------- #
    # Real-world data (REAL_DATA_INTEGRATION.md)                        #
    # ---------------------------------------------------------------- #

    @property
    def real_data_enabled(self) -> bool:
        """True when an ingestion client exists. Says nothing about whether any
        feed actually answered — read `real_data_status()` for that."""
        return self.real is not None

    def real_data_status(self) -> dict:
        """What this server actually knows about the real world, and what it
        does not. Always present on the MCP surface: an ABSENT flag reads as
        "fine", and that is the failure mode this project keeps repeating."""
        out: dict[str, Any] = {
            "enabled": self.real_data_enabled,
            "hydrated": self.real_world is not None,
            "theater": self.theater.id,
            "error": self.real_data_error,
            "refreshing": self._real_refresher is not None,
            "weather_applied_to_sim": self.real_weather_applied,
            "env_switch": REAL_DATA_ENV,
        }
        if not self.real_data_enabled:
            out["note"] = (
                "the real-world data layer is OFF, so alt_agl_m is height "
                "above the LAUNCH DATUM, uav_los_check has no terrain in it, "
                "sim_spawn_target defaults to the theater's hand-entered "
                f"ground elevation and the wind is the sim's. Set {REAL_DATA_ENV}=1 "
                "or call sim_hydrate_real_data on a server built with a client.")
            return out
        out["origin"] = getattr(self.real, "origin", None)
        out["terrain_points_cached"] = self.real.terrain.cache_size()
        out["terrain_requests"] = self.real.terrain.requests
        out["terrain_grid_deg"] = self.terrain_grid_deg
        out["terrain_grid_m"] = round(self.terrain_grid_deg * 111_320.0, 1)
        out["terrain_grid_note"] = (
            "every terrain read on a hot path is snapped to this lattice, and "
            "an AO load fetches exactly this lattice; a value's "
            "terrain_snap_offset_m says how far the reported ground is from the "
            "point asked about")
        if self.real_world is None:
            out["note"] = ("enabled but never hydrated: every feed still reads "
                           "as synthetic. Call sim_hydrate_real_data.")
            return out
        out["degraded_feeds"] = self.real_world.degraded_feeds
        out["real"] = self.real_world.real
        out["hydrated_at_ms"] = self.real_world.hydrated_at_ms
        out["feeds"] = {name: p.as_dict()
                        for name, p in self.real_world.feeds.items()}
        out["attribution"] = self.real_world.attributions()
        out["terrain_delta_m"] = self.real_world.terrain_delta_m()
        return out

    # -- terrain reads; NONE of these may touch the network --------------
    def terrain_at(self, lat: float, lon: float) -> Any:
        """Cached-only ground sample near a point, or None when the layer is OFF.

        Memory only (`allow_network=False`), so this is safe on a telemetry tick
        and inside a mission call. The point is snapped to `TERRAIN_GRID_DEG`
        first (see that constant — without the snap a flying aircraft misses the
        cache on every single tick and AGL never becomes measured at all), and a
        miss schedules a BACKGROUND prefetch of the surrounding lattice and
        returns the flagged fallback now.
        """
        if self.real is None:
            return None
        slat, slon = snap_to_terrain_grid(lat, lon, self.terrain_grid_deg)
        sample = self.real.terrain.cached(slat, slon)
        if not sample.real:
            self.prefetch_terrain(self._terrain_halo(slat, slon))
        return sample

    def _terrain_halo(self, slat: float, slon: float) -> list[tuple[float, float]]:
        """The lattice cell and its neighbours — where the aircraft goes next."""
        span = range(-TERRAIN_PREFETCH_HALO, TERRAIN_PREFETCH_HALO + 1)
        return [(round(slat + i * self.terrain_grid_deg, 5),
                 round(slon + j * self.terrain_grid_deg, 5))
                for i in span for j in span]

    def prefetch_terrain(self, points: Any) -> None:
        """Warm the terrain cache for `points` on a daemon thread. Never blocks.

        De-duplicated on the provider's own cache key, and the number of
        CONCURRENT prefetches is capped, so a mission flying off the hydrated AO
        cannot turn every 0.25 s telemetry tick into an outstanding request.
        The provider batches the points of one call into as few requests as the
        proxy's cap allows.
        """
        if self.real is None:
            return
        keyed: dict[str, tuple[float, float]] = {}
        for lat, lon in points:
            keyed.setdefault(self.real.terrain._key(lat, lon), (float(lat), float(lon)))
        with self._terrain_lock:
            if self._terrain_prefetches >= TERRAIN_PREFETCH_MAX_INFLIGHT:
                return
            wanted = [p for k, p in keyed.items() if k not in self._terrain_inflight]
            if not wanted:
                return
            keys = [self.real.terrain._key(lat, lon) for lat, lon in wanted]
            self._terrain_inflight.update(keys)
            self._terrain_prefetches += 1

        def run() -> None:
            try:
                self.real.terrain.heights(wanted)
            except Exception as exc:  # noqa: BLE001 — a daemon must not die silently
                self.real_data_error = f"terrain prefetch: {type(exc).__name__}: {exc}"
            finally:
                with self._terrain_lock:
                    self._terrain_inflight.difference_update(keys)
                    self._terrain_prefetches -= 1

        threading.Thread(target=run, name="godseye-terrain-prefetch",
                         daemon=True).start()

    def ao_terrain_lattice(self) -> list[tuple[float, float]]:
        """Every lattice point covering the theater's AO, at `terrain_grid_deg`.

        Built by snapping the bbox corners onto the SAME lattice `terrain_at`
        snaps to, then walking whole cells and overhanging by one at each end —
        so a point anywhere inside the AO snaps onto a point in this list, which
        is the whole reason the hot path can be cache-only.
        """
        south, west, north, east = self.theater.bbox()
        grid = self.terrain_grid_deg
        s_lat, s_lon = snap_to_terrain_grid(south, west, grid)
        n_lat, n_lon = snap_to_terrain_grid(north, east, grid)
        rows = round((n_lat - s_lat) / grid) + 1
        cols = round((n_lon - s_lon) / grid) + 1
        return [(round(s_lat + i * grid, 5), round(s_lon + j * grid, 5))
                for i in range(-1, rows + 1) for j in range(-1, cols + 1)]

    def prefetch_ao_terrain(self, spacing_m: float | None = None) -> dict:
        """Load the DEM for the whole AO, so every later read is a cache hit.
        BLOCKS — a startup / between-missions call, run on a worker thread.

        This is what makes the non-blocking hot path actually deliver MEASURED
        values: with the AO lattice resident, a telemetry tick's snapped point is
        already in memory and `alt_agl_m` is real from the first sample instead
        of after a prefetch the aircraft has already outrun.

        `spacing_m` may COARSEN the lattice (fewer points, a cheaper cold load);
        it can never make it finer than the theater's own fitted grid, and
        whatever it chooses becomes `terrain_grid_deg` so the snap follows it.
        A lattice and a snap on different grids miss every other cell, which
        looks exactly like working.
        """
        if self.real is None:
            raise RuntimeError("the real-world data layer is OFF on this server")
        if spacing_m is not None:
            asked = round(max(1.0, float(spacing_m)) / 111_320.0 / TERRAIN_GRID_DEG)
            self.terrain_grid_deg = max(
                self._fitted_terrain_grid_deg,
                round(max(1, asked) * TERRAIN_GRID_DEG, 6))
        points = self.ao_terrain_lattice()
        samples = self.real.terrain.heights(points)
        measured = sum(1 for s in samples if s.real)
        out = {
            "requested": len(points), "measured": measured,
            "grid_deg": self.terrain_grid_deg,
            "spacing_m": round(self.terrain_grid_deg * 111_320.0, 1),
            "fitted_grid_deg": self._fitted_terrain_grid_deg,
            "bbox": list(self.theater.bbox()),
            "cache_size": self.real.terrain.cache_size(),
        }
        if measured < len(points):
            out["note"] = (
                f"{len(points) - measured} of {len(points)} AO lattice points "
                "have no measured height; AGL and LOS over those cells fall "
                "back to the launch datum and say so")
        return out

    def resolve_agl(self, lat: float, lon: float, alt_hae_m: float,
                    launch_agl_m: float) -> dict:
        """MEASURED AGL when real terrain is available, the launch datum when not.

        This is the fix for the oldest lie in the codebase: "AGL" meant height
        above the takeoff point, which is true AGL only over ground at the home
        elevation. The fallback is bit-for-bit the old behaviour — the point is
        that the caller can now SEE which one it got, on every single value:

        * `alt_agl_m` — the best available AGL;
        * `alt_agl_launch_datum_m` — the old number, ALWAYS present, so nothing
          that depended on it loses it and the two can be compared;
        * `alt_agl_is_real` / `alt_agl_source` / `alt_agl_note` — which it is;
        * `terrain` — the full `TerrainSample` with its `Provenance`, whenever
          a terrain read was attempted at all.

        This is `realdata.TerrainProvider.agl()`'s arithmetic — vehicle HAE minus
        ground HAE — done here rather than by calling it, for two reasons that
        both matter on a 0.25 s tick: the point must be SNAPPED to the terrain
        lattice first (`terrain_at`), and `alt_hae_m` has already been through
        `geo.canonical_altitude()` in `backend.telemetry`, so calling `agl()`
        would re-run the geoid on every sample for an identical answer.
        `AglFix.agl_m` is None when terrain is unknown, and that None is never
        turned into a plausible number here either: it selects the launch datum
        and says so.
        """
        launch = round(float(launch_agl_m), 3)
        out: dict[str, Any] = {
            "alt_agl_m": launch,
            "alt_agl_launch_datum_m": launch,
            "alt_agl_is_real": False,
            "alt_agl_source": AGL_SOURCE_LAUNCH,
            "alt_agl_note": AGL_LAUNCH_NOTE,
        }
        sample = self.terrain_at(lat, lon)
        if sample is None:
            out["alt_agl_reason"] = (
                "the real-world data layer is OFF on this server "
                f"({REAL_DATA_ENV} unset and no client injected)")
            return out
        out["terrain"] = sample.as_dict()
        out["terrain_snapped_to"] = [sample.lat, sample.lon]
        out["terrain_snap_offset_m"] = round(
            haversine_m(float(lat), float(lon), sample.lat, sample.lon), 1)
        if not sample.real or sample.hae_m is None:
            out["alt_agl_reason"] = sample.provenance.reason
            return out
        out.update({
            "alt_agl_m": round(float(alt_hae_m) - float(sample.hae_m), 3),
            "alt_agl_is_real": True,
            "alt_agl_source": AGL_SOURCE_TERRAIN,
            "alt_agl_note": AGL_TERRAIN_NOTE,
            "terrain_hae_m": round(float(sample.hae_m), 2),
            "terrain_msl_m": (None if sample.msl_m is None
                              else round(float(sample.msl_m), 2)),
            "agl_minus_launch_datum_m": round(
                (float(alt_hae_m) - float(sample.hae_m)) - launch, 3),
        })
        return out

    async def _telemetry(self, vehicle: str) -> dict:
        """`backend.telemetry` with the AGL resolved against real terrain.

        Every server-side read of telemetry goes through here, so `alt_agl_m`
        means the same thing to the fuel integrator, the envelope check, the
        mission planners, the resources and the harness. The backend itself
        still publishes the raw launch-datum number and knows nothing about
        terrain — there is one wiring point, and this is it.

        `alt_agl_m` is now MEASURED height above the ground under the aircraft.
        That is a different physical quantity from the one every COMMANDED and
        PRICED altitude in this system is expressed in — see `commanded_agl`,
        which is what those consumers must read.
        """
        tele = await self.backend.telemetry(vehicle)
        tele.update(self.resolve_agl(tele["lat"], tele["lon"], tele["alt_hae_m"],
                                     tele["alt_agl_m"]))
        return tele

    @staticmethod
    def commanded_agl(tele: dict) -> float:
        """The altitude datum every COMMAND and every fuel figure is written in.

        Measured AGL (`alt_agl_m`) answers "how high am I above the ground I am
        over?". It is the right number for a terrain floor and for a sensor
        footprint, and it is the WRONG number for anything that commands or
        prices an altitude, because all of those are expressed against the
        LAUNCH DATUM (the NED origin at home):

        * `UavBackend.llh_agl_to_ned` turns a waypoint's `alt_agl_m` into
          `home_hae + alt`. Feeding it a terrain-relative figure moves the
          aircraft to a completely different height.
        * `SafetyEnvelope`'s `ceiling_m_agl` / `min_agl_m` are documented as
          heights above the launch datum, and `FuelModel.bingo_fuel_pct` lets
          the return leg down to 0 *over home* — "height above home ground" is
          precisely the launch datum, never the ground under the aircraft.

        Measured over the Fordow ridge, the two are 82 m apart and of opposite
        sign: the safety RTB's cruise altitude read `max(min_agl + 2, -42.2)`
        = 5 m and commanded a cruise 77 m UNDERGROUND, on the one task whose
        entire purpose is to bring the aircraft home. Indexed, never `.get`:
        `resolve_agl` always writes this key, and a substituted default here
        would be the same silent datum swap in a new disguise.
        """
        return float(tele["alt_agl_launch_datum_m"])

    def ground_msl_at(self, lat: float, lon: float) -> dict:
        """Ground elevation under a point: measured terrain, else the theater's
        hand-entered elevation — never 0, and never silently one for the other.

        `sim_spawn_target`'s default. A 0 default buried every target ~1550 m
        underground in the mountain theaters (TOOL_CONTRACT §4.4); the theater
        fallback an earlier wave wired fixed that but is still a single
        hand-entered number for a whole AO, which is wrong by hundreds of
        metres across real relief.
        """
        sample = self.terrain_at(lat, lon)
        if sample is not None and sample.real and sample.msl_m is not None:
            return {"alt_msl_m": round(float(sample.msl_m), 2),
                    "alt_source": AGL_SOURCE_TERRAIN, "alt_is_real": True,
                    "terrain": sample.as_dict()}
        out = {"alt_msl_m": float(self.theater.home_alt_msl_m),
               "alt_source": f"theater:{self.theater.id}", "alt_is_real": False,
               "alt_reason": (
                   "no measured terrain height at this point; falling back to "
                   f"the theater's hand-entered ground elevation "
                   f"({self.theater.home_alt_msl_m:.1f} m MSL at its HOME point), "
                   "which is a single number for the whole AO")}
        if sample is not None:
            out["terrain"] = sample.as_dict()
        return out

    def terrain_los(self, observer: tuple[float, float, float],
                    target: tuple[float, float, float], *,
                    allow_network: bool = False) -> Any:
        """Terrain-aware LOS, or None when the layer is OFF.

        Default `allow_network=False`: a LOS check is a mission call and must not
        sit on a socket, so the answer comes from memory and says `known=False`
        when the profile is not resident. The profile points are then prefetched
        in the background, so the next check over the same ground is measured.
        A harness that needs a measured answer NOW opts in explicitly
        (`uav_los_check(terrain="fetch")`), which runs on a worker thread.

        Unlike a telemetry tick this is not snapped to the lattice: the upstream
        proxy answers arbitrary points, and a LOS profile sampled at exactly
        90 m is more useful than one quantised to the cache grid.
        """
        if self.real is None:
            return None
        result = self.real.terrain.line_of_sight(observer, target, datum="hae",
                                                 allow_network=allow_network)
        if not result.known and not allow_network:
            self.prefetch_terrain(
                [(s.lat, s.lon)
                 for s in self.real.terrain.profile((observer[0], observer[1]),
                                                    (target[0], target[1]),
                                                    allow_network=False)])
        return result

    def terrain_floor(self) -> dict | None:
        """The geofence FLOOR: the lowest altitude that clears the highest ground
        in the AO by the envelope's own min-AGL. None when the layer is OFF.

        The envelope had no floor at all: `min_agl_m` was measured against the
        launch datum, so a geofence over a 300 m ridge let the aircraft fly into
        it while reading a comfortable 60 m "AGL". With terrain wired, the
        min-AGL check in `tick_once` is measured against the ground actually
        under the aircraft, and THIS is the same limit expressed as an absolute
        altitude so a planner can respect it before taking off.
        """
        if self.real is None:
            return None
        fix = self.real.terrain.floor(self.theater.terrain_sample_points(),
                                      clearance_agl_m=self.envelope.min_agl_m,
                                      allow_network=False)
        out = fix.as_dict()
        out["enforced_as"] = (
            "the envelope's min_agl_m is checked against MEASURED terrain under "
            "the aircraft on every tick when alt_agl_is_real is true; this floor "
            "is the same limit as an absolute altitude over the whole AO, for "
            "planning before the aircraft is anywhere."
            if fix.real else
            "ADVISORY ONLY: this floor rests on synthetic ground, so it is not "
            "a terrain floor. The min-AGL check falls back to the launch datum.")
        return out

    def real_weather(self) -> Any:
        """The hydrated `WeatherObservation`, or None. Memory only."""
        return None if self.real_world is None else self.real_world.weather

    def real_traffic(self) -> Any:
        """The hydrated `AirspaceTraffic`, or None. Memory only."""
        return None if self.real_world is None else self.real_world.traffic

    def real_order_of_battle(self) -> Any:
        """The hydrated `OrderOfBattle`, or None. Memory only."""
        return None if self.real_world is None else self.real_world.order_of_battle

    def hydrate_real_data(self, *, allow_network: bool = True,
                          load_ao_terrain: bool = True,
                          terrain_grid_m: float | None = None) -> dict:
        """Read every feed for this server's theater. BLOCKS — never call this
        from a telemetry tick or inside the event loop; the tool that exposes it
        runs it on a worker thread.

        Nothing in the static theater table is rewritten: a measured ground that
        disagrees with the hand-entered one is REPORTED (`terrain_delta_m`), not
        applied under a running geofence.
        """
        if self.real is None:
            raise RuntimeError(
                "the real-world data layer is OFF on this server; construct it "
                f"with real_data=True / a client, or set {REAL_DATA_ENV}=1")
        try:
            self.real_world = self.theater.hydrate(
                self.real, allow_network=allow_network,
                floor_clearance_agl_m=self.envelope.min_agl_m)
            self.real_data_error = None
        except Exception as exc:  # noqa: BLE001 — recorded and surfaced
            self.real_data_error = f"{type(exc).__name__}: {exc}"
            raise
        status = self.real_data_status()
        if load_ao_terrain and allow_network:
            # Resident DEM for the AO. Without it the hot path's cached-only
            # reads miss and every AGL falls back to the launch datum: the
            # wiring would be there and would never measure anything.
            status["ao_terrain"] = self.prefetch_ao_terrain(terrain_grid_m)
        self.store.log_audit(
            "real_data", f"hydrated {self.theater.id}",
            degraded_feeds=self.real_world.degraded_feeds,
            terrain_delta_m=self.real_world.terrain_delta_m(),
            terrain_points_cached=self.real.terrain.cache_size())
        return status

    async def apply_real_weather(self) -> dict:
        """Push the hydrated real weather into the sim (M15 wind + M18 obscurants).

        This is the wiring the fuel model needs: `FuelModel` prices each leg
        against `wind_ne`, which comes from the sim, which until now only ever
        held what an operator typed. `WeatherObservation.sim_weather_payload()`
        emits exactly `sim_set_weather`'s parameter names, and the wind it
        carries is the NED air-mass vector derived from the real observation.

        REFUSED, never faked, when the weather was not actually observed: a
        fabricated dead calm is indistinguishable from a measured one, which is
        why `WeatherObservation.wind_known` exists.
        """
        obs = self.real_weather()
        if obs is None:
            return error("real_weather_unavailable",
                         "no hydrated weather for this theater; call "
                         "sim_hydrate_real_data first", retryable=True)
        if not obs.real or not obs.wind_known:
            return error(
                "real_weather_degraded",
                "the weather feed is degraded, so its wind is an assumption, "
                f"not an observation ({obs.provenance.reason}). It is refused "
                "rather than applied as if it were measured.",
                retryable=True, provenance=obs.provenance.as_dict())
        payload = obs.sim_weather_payload()
        # EVERY obscurant, zeros included. Sending only the non-zero ones would
        # leave an operator's earlier fog sitting in the sim while this reported
        # that the weather now matched a clear observation — the same "the
        # substitution is invisible" failure the flags exist to prevent.
        values = {k: payload[k] for k in ("rain", "snow", "fog", "dust")}
        await self.backend.set_weather(values)
        await self.backend.set_wind(payload["wind_north_mps"],
                                    payload["wind_east_mps"],
                                    payload["wind_down_mps"])
        applied = {
            "source": "real",
            "weather_is_real": True,
            "weather": values,
            "wind_ne_mps": [payload["wind_north_mps"], payload["wind_east_mps"]],
            "wind_speed_mps": obs.wind_speed_mps,
            "wind_from_deg": obs.wind_from_deg,
            "condition": obs.condition,
            "sensor_factor": obs.sensor_factor,
            "observed_at_ms": obs.observed_at_ms,
            "provenance": obs.provenance.as_dict(),
            "effects": ("M15: this wind is what the fuel integrator prices every "
                        "leg's headwind against; M18: the condition shortens "
                        "detection range."),
        }
        self.real_weather_applied = applied
        self.store.log_audit("real_weather", f"{obs.condition} from real feed",
                             wind_ne_mps=applied["wind_ne_mps"],
                             condition=obs.condition,
                             source=obs.provenance.source)
        return applied

    def start_real_data(self, *, interval_s: float = 300.0,
                        load_ao_terrain: bool = True) -> Any:
        """Hydrate now on a daemon thread, then keep refreshing off every hot path."""
        if self.real is None:
            return None
        from .realdata import BackgroundRefresher

        def publish(result: Any) -> None:
            self.real_world = result
            theaters.set_real_data(self.theater.id, result)
            # The AO lattice has to be loaded on THIS path too. Without it the
            # background route hydrates the four feeds and leaves every hot-path
            # terrain read a cache miss — real data that is present and never
            # consulted, which is the defect this whole wave is about. It runs
            # on the refresher's own daemon thread, so it blocks nothing.
            if load_ao_terrain:
                try:
                    self.prefetch_ao_terrain()
                except Exception as exc:  # noqa: BLE001 — recorded, loop survives
                    self.real_data_error = (
                        f"AO terrain load: {type(exc).__name__}: {exc}")

        refresher = BackgroundRefresher(
            self.real, interval_s=interval_s, on_result=publish,
            theater_id=self.theater.id, home_lat=self.theater.home_lat,
            home_lon=self.theater.home_lon, bbox=self.theater.bbox(),
            static_home_msl_m=self.theater.home_alt_msl_m,
            floor_points=self.theater.terrain_sample_points(),
            floor_clearance_agl_m=self.envelope.min_agl_m)
        self._real_refresher = refresher.start()
        return self._real_refresher

    def stop_real_data(self) -> None:
        refresher, self._real_refresher = self._real_refresher, None
        if refresher is not None:
            refresher.stop()

    # ---- restart recovery (T4c) ----
    def _boot_replay(self):
        """Replay the journal and act on resume-or-abort-and-RTH."""
        report = self.store.replay(record=True)
        # T4b across a restart: a key whose task finished must return the
        # ORIGINAL handle, not fly the command again.
        for row in self.store.tasks.read_all():
            key, tid = row.get("idempotency_key"), row.get("task_id")
            vehicle = row.get("vehicle")
            if not (key and tid and vehicle):
                continue
            if row.get("event") in ("completed", "done", "failed", "cancelled", "aborted"):
                self.tasking.seed_idempotency(vehicle, key, row)
        self.boot_actions: list[dict] = []
        for rec in report.interrupted:
            if not rec.vehicle:
                continue
            action = {"vehicle": rec.vehicle, "decision": rec.decision, "reason": rec.reason,
                      "kind": rec.kind, "id": rec.id, "tool": rec.tool, "params": rec.params}
            self.boot_actions.append(action)
        if self.boot_actions:
            self.store.log_audit(
                "restart_recovery",
                f"{len(self.boot_actions)} interrupted item(s) recovered at boot",
                actions=self.boot_actions)
        # The LOAD half of restart recovery. Without it the decisions above are
        # taken for an aircraft that came back with a full tank (see
        # `_restore_persisted_state`).
        self.boot_restored = self._restore_persisted_state(report)
        return report

    # ---- restart recovery: the LOAD half (T4c / M11 / M12) ----
    def _restore_persisted_state(self, report) -> dict:
        """Rebuild the fuel integrators, the track store and the
        pattern-of-life baselines from the journals this store already holds.

        The store side of all three has always worked: `store.log_fuel` writes
        every tick, `_persist_intel` writes every track and every touched POI
        baseline. The LOAD side was written (`FuelModel.from_dict`,
        `TrackManager.load_state`, `PatternOfLife.load_state`) and then called
        from nowhere, so a restart rebuilt all three BARE:

        * the fuel integrator came back at 100 % with an UN-TRIPPED BINGO
          latch, so `apply_boot_recovery`'s RESUME handed the aircraft that was
          at 22 % and already committed to RTB a full tank and flew it on;
        * `tracks.jsonl` and `pattern_of_life.jsonl` were written and then
          orphaned, so the M11 persistent track id and the M12 pattern-of-life
          baseline — the whole point of both — did not survive a restart.

        Everything restored here is REPORTED: what came back, what the journal
        did not carry, and every record that would not parse. A restore that
        quietly dropped half the state would be worse than not restoring at
        all, because the numbers would look live.
        """
        restored = {"fuel": self._restore_fuel(report), **self._restore_intel()}
        # `_restore_fuel` returns a row per vehicle it was ASKED about, and a
        # row whose `restored` is False is a vehicle back on a full tank.
        # Counting those as restored would report the exact state this whole
        # function exists to make impossible as a success.
        fuel_ok = [r for r in restored["fuel"] if r.get("restored")]
        fuel_bad = [r["vehicle"] for r in restored["fuel"] if not r.get("restored")]
        restored["fuel_not_restored"] = fuel_bad
        summary = (f"{len(fuel_ok)} fuel integrator(s), "
                   f"{restored['tracks']['restored']} track(s), "
                   f"{restored['pattern_of_life']['restored']} "
                   "pattern-of-life baseline(s) restored from the journals")
        if fuel_bad:
            summary += (f" — {len(fuel_bad)} FUEL CLOCK(S) NOT RESTORED "
                        f"({', '.join(fuel_bad)}): those vehicles are on a "
                        "fresh integrator, i.e. a full tank")
        if any(restored[k].get("dropped") for k in ("tracks", "pattern_of_life")):
            summary += " — SOME PERSISTED RECORDS DID NOT LOAD, see dropped"
        if restored["fuel"] or restored["tracks"]["persisted"] \
                or restored["pattern_of_life"]["persisted"]:
            self.store.log_audit("restart_state_restored", summary, **restored)
        return restored

    def _restore_fuel(self, report) -> list[dict]:
        """Put each vehicle's persisted fuel integral back on its monitor.

        `report.fuel` is the LAST fuel row per vehicle (`Store.fuel_state`), and
        the fuel journal is run-scoped: `store.reset()` rotates it, so a row
        here belongs to the run that was interrupted.

        Configuration (airframe, rate table, capacity, home, RTB speed) comes
        from the model this process constructed; the flown STATE comes from the
        row. The one exception is the airframe id, which travels with the
        state: replaying a burn under a different energy model would re-price
        every second of it, so a row that names an airframe wins and brings its
        own rate table with it.

        A row that will not rebuild is REPORTED and skipped, never swallowed
        and never fatal — same treatment `_restore_intel` gives a track record
        that will not parse. See `_unrestored` for why both halves of that
        matter.
        """
        out: list[dict] = []
        for vehicle, row in sorted((report.fuel or {}).items()):
            if not row:
                continue
            if row.get("fuel_pct") is None:
                # The row exists and does not say how much fuel was left. That
                # is not "full": it is unknown, and the vehicle is about to be
                # resumed or aborted on the basis of it.
                out.append(self._unrestored(
                    vehicle, row, "the persisted fuel row carries no fuel_pct",
                    absent=["fuel_pct"]))
                continue
            # `monitor_for` MINTS the monitor when the vehicle is known only to
            # the journal, which is the restart case that matters: the fleet is
            # not hardcoded anywhere.
            mon = self.monitor_for(vehicle)
            state = mon.fuel.to_dict()
            notes: list[str] = []
            absent: list[str] = []
            for row_key, state_key in FUEL_ROW_TO_STATE.items():
                if row.get(row_key) is None:
                    absent.append(row_key)
                else:
                    state[state_key] = row[row_key]
            airframe = row.get("airframe")
            if airframe is None:
                absent.append("airframe")
                notes.append(
                    "the row does not say which airframe produced this burn "
                    f"(pre-airframe journal); the configured {state.get('airframe')!r} "
                    "energy model is kept and the recovered burn rate may not "
                    "be the one that was flown")
            elif airframe != state.get("airframe"):
                notes.append(
                    f"journal airframe {airframe!r} != configured "
                    f"{state.get('airframe')!r}; the JOURNAL's model wins, so "
                    "the recovered burn is priced by the airframe that flew it")
                state["airframe"] = airframe
                # Drop this process's derived numbers so `from_dict` rebuilds
                # them from the airframe that actually flew.
                for k in ("rates", "airframe_profile", "capacity_s_cruise"):
                    state.pop(k, None)
            latched = row.get("bingo_latched")
            if latched is None:
                absent.append("bingo_latched")
                # NOT read as "not latched": the row simply does not say. The
                # latch is left as constructed and re-tested against the
                # restored fuel on the first tick — and it is reported here,
                # because a silent False is the shape that flies an aircraft
                # that was already committed to RTB back out to the target.
                notes.append(
                    "the row carries no bingo_latched flag, so the latch was "
                    "NOT restored from it; it is re-tested against the restored "
                    "fuel on the first tick")
            else:
                bingo = dict(state.get("bingo") or {})
                bingo["tripped"] = bool(latched)
                if latched:
                    detail = self._latched_bingo_detail(vehicle)
                    if detail:
                        bingo.update(detail)
                        notes.append(
                            "the latch detail was recovered from the audit "
                            "trail; tripped_at is the dead process's monotonic "
                            "clock and is not comparable to this one's")
                    else:
                        notes.append(
                            "the latch is restored TRIPPED from the fuel "
                            "journal, but no audit row carried its detail, so "
                            "fuel_pct_at_trip / position are unknown")
                state["bingo"] = bingo
            try:
                fm = FuelModel.from_dict(state)
            except (ValueError, KeyError, TypeError) as exc:
                # The reachable case is an airframe id the journal names and
                # this build no longer has: `get_airframe` raises rather than
                # fall back to another energy model (deliberately — a fuel
                # clock priced by the wrong airframe is a wrong BINGO line
                # stated with confidence). Letting that escape takes the WHOLE
                # server down in `__init__` over one row, for every vehicle,
                # with nothing written to the audit trail.
                out.append(self._unrestored(
                    vehicle, row,
                    f"the persisted fuel row would not rebuild: "
                    f"{type(exc).__name__}: {exc}",
                    absent=absent, notes=notes))
                continue
            mon.fuel = fm
            out.append({
                "vehicle": vehicle,
                "restored": True,
                "fuel_pct": round(fm.fuel_pct, 3),
                "phase": fm.last_phase.value,
                "ticks": fm.ticks,
                "elapsed_s": round(fm.elapsed_s, 2),
                "airframe": fm.airframe.id,
                "bingo_latched": fm.bingo.tripped,
                "fields_absent_from_row": absent,
                "notes": notes,
            })
        return out

    def _unrestored(self, vehicle: str, row: dict, why: str, *,
                    absent: list[str] | None = None,
                    notes: list[str] | None = None) -> dict:
        """One vehicle whose persisted fuel clock could NOT be put back.

        Two failure shapes are possible and both are wrong to hide:

        * raising out of `__init__` — one unreadable row then makes the whole
          server un-bootable for every vehicle, and the audit trail, the place
          a restart failure has to be legible from, never gets written;
        * returning quietly — the vehicle comes back on a FRESH integrator,
          i.e. a full tank and an un-tripped BINGO latch, which is exactly the
          state `_restore_persisted_state` exists to stop `apply_boot_recovery`
          from resuming on.

        So it is neither: the vehicle keeps the integrator it was constructed
        with, and that fact is the return value, an audit row, and part of the
        `restart_recovery` block on `uav://safety/geofence`.
        """
        mon = self.monitor_for(vehicle)
        self.store.log_audit(
            "restart_state_dropped",
            f"{vehicle}'s fuel clock is NOT restored: {why}. It is on a FRESH "
            "integrator (full tank, un-tripped BINGO latch) and the boot "
            "recovery decision for it is taken on that",
            vehicle=vehicle, row_fuel_pct=row.get("fuel_pct"),
            row_airframe=row.get("airframe"),
            row_bingo_latched=row.get("bingo_latched"), reason=why)
        return {
            "vehicle": vehicle,
            "restored": False,
            "reason": why,
            "row_fuel_pct": row.get("fuel_pct"),
            "row_airframe": row.get("airframe"),
            "row_bingo_latched": row.get("bingo_latched"),
            "fuel_pct": round(mon.fuel.fuel_pct, 3),
            "bingo_latched": mon.fuel.bingo.tripped,
            "airframe": mon.fuel.airframe.id,
            "fields_absent_from_row": list(absent or []),
            "notes": list(notes or []) + [
                ("the fuel clock was NOT restored; fuel_pct/bingo_latched "
                 "above are this process's FRESH integrator, not the flown "
                 "state")],
        }

    def _latched_bingo_detail(self, vehicle: str) -> dict:
        """The full BINGO latch as it was journaled, from the audit trail.

        `force_rtb` writes `bingo=fm.bingo.to_dict()`, so when the latch tripped
        in the previous process its fuel/position detail is on disk. The fuel
        journal only carries the boolean.
        """
        for rec in reversed(list(self.store.audit.read_all())):
            if rec.get("vehicle") != vehicle:
                continue
            b = rec.get("bingo")
            if isinstance(b, dict) and b.get("tripped"):
                return {k: b[k] for k in
                        ("tripped_at", "fuel_pct_at_trip", "bingo_pct_at_trip",
                         "position", "clear_attempts") if k in b}
        return {}

    def _restore_intel(self) -> dict:
        """Reload the M11 track store and the M12 pattern-of-life baselines.

        Both survive `sim_reset` by design (PLAN §4.4), so they are reloaded
        unconditionally — there is no "this run" to scope them to.

        `load_state` skips a record it cannot parse, which is the right
        behaviour for a log but is invisible from its return value. Every
        persisted id is checked against what is actually in memory afterwards,
        so a dropped track is a REPORTED gap and not a track that quietly
        stopped existing.
        """
        track_rows = self.store.tracks.values()
        # The record store keys on the track, so the MANAGER's own sim epoch is
        # not persisted — only each track's copy of it. Taking the highest one
        # back stops a fresh manager from reporting epoch 0 over tracks stamped
        # 2, and from re-issuing an epoch number the run has already used.
        epoch = max((int(r.get("sim_epoch") or 0) for r in track_rows), default=0)
        self.tracks.load_state({"tracks": track_rows, "sim_epoch": epoch})
        dropped_tracks = [str(r.get("track_id"))
                          for r in track_rows
                          if r.get("track_id") is None
                          or self.tracks.get(str(r["track_id"])) is None]

        poi_rows = self.store.pattern_of_life.values()
        self.pol.load_state({"pois": poi_rows})
        dropped_pois = [str(r.get("poi"))
                        for r in poi_rows
                        if r.get("poi") is None or self.pol.get(str(r["poi"])) is None]
        for name in (dropped_tracks, dropped_pois):
            if name:
                self.store.log_audit(
                    "restart_state_dropped",
                    f"{len(name)} persisted intel record(s) would not parse and "
                    "are NOT in this process's stores", ids=name)
        return {
            "tracks": {"persisted": len(track_rows),
                       "restored": len(track_rows) - len(dropped_tracks),
                       "dropped": dropped_tracks},
            "pattern_of_life": {"persisted": len(poi_rows),
                                "restored": len(poi_rows) - len(dropped_pois),
                                "dropped": dropped_pois},
        }

    async def apply_boot_recovery(self) -> list[dict]:
        """Execute the boot decisions: RTH what is unsafe, resume what is not."""
        applied: list[dict] = []
        for vehicle in self.recovery.vehicles():
            decision = self.recovery.decision_for(vehicle)
            reasons = self.recovery.reasons_for(vehicle)
            if decision == ABORT_RTH:
                handle = await self._force_rtb(
                    vehicle, reason="restart_recovery",
                    detail="; ".join(reasons) or "interrupted work at restart")
                applied.append({"vehicle": vehicle, "decision": ABORT_RTH,
                                "task": handle, "reasons": reasons})
                continue
            for act in self.boot_actions:
                if act["vehicle"] != vehicle or act["decision"] != RESUME:
                    continue
                if not act.get("tool") or not act.get("params"):
                    continue
                task = self.tasking.submit(vehicle, act["tool"], dict(act["params"]),
                                           idempotency_key=f"resume:{act['id']}",
                                           allow_queue=True)
                self.store.log_task(task.handle(), "resumed", params=task.params,
                                    recovered_from=act["id"])
                applied.append({"vehicle": vehicle, "decision": RESUME,
                                "task": task.handle(), "reasons": reasons})
        return applied

    # ---- queue executor (T2): runs inside the per-vehicle worker ----
    async def _execute(self, task, ctx) -> dict:
        v = task.vehicle
        p = task.params
        tool = task.tool
        # M9: a SAFETY transition may be flown with the datalink down — that is
        # the case it exists for. Only a safety task is allowed to fly blind,
        # and it always REPORTS that it did.
        blind_ok = bool(getattr(task, "safety", False))
        self.store.log_task(task.handle(), "started",
                            params=self._journal_payload(p))
        try:
            if tool == "uav_takeoff":
                climb_to = _one_altitude("uav_takeoff altitude",
                                         alt_agl_m=p.get("alt_agl_m"),
                                         alt_m=p.get("alt_m"))
                await self.backend.takeoff(v, alt_agl_m=climb_to, task=task)
                result = {"ok": True, "alt_agl_m": climb_to, "alt_m": climb_to}
            elif tool == "uav_land":
                await self.backend.land(v, task=task)
                result = {"ok": True}
            elif tool == "uav_return_to_home":
                home = self.envelope.home
                if home is None:
                    raise ValueError("no home configured in safety envelope")
                tele, tele_src, tele_err = await self._tele_or_last(v, blind_ok=blind_ok)
                at_home = haversine_m(tele["lat"], tele["lon"], home[0], home[1]) <= 30.0
                landed = int(tele.get("landed_state", 0)) == 0
                if landed and at_home and tele_src == "live":
                    # Already down, already home: an RTB that takes off again in
                    # order to land again is not a recovery, it is a hazard.
                    # Only on a LIVE fix: a stale "landed at home" must not talk
                    # the server out of recovering an airborne aircraft.
                    await task.report_progress(EXEC_PROGRESS_CEILING,
                                               note="already at home, landed",
                                               eta_s=0.0)
                    result = {"ok": True, "home": list(home), "reason": p.get("reason"),
                              "no_op": "already at home and landed"}
                else:
                    # COMMANDED altitude -> the launch datum (`commanded_agl`).
                    # With the terrain-measured AGL here, an RTB started over
                    # high ground read a NEGATIVE height, `max()` chose the
                    # envelope floor, and the un-cancellable force-RTB flew home
                    # at `min_agl + 2` above HOME — 77 m underground over the
                    # Fordow ridge, on the one task that exists to save the
                    # aircraft.
                    cruise = float(p.get("alt_m")
                                   or max(self.envelope.min_agl_m + 2.0,
                                          self.commanded_agl(tele)))
                    if landed and tele_src == "live":
                        await self._ensure_airborne(v, cruise)
                    # An RTB is FLY-then-LAND. Flying it 0..100 made the handle
                    # read progress 100 % / eta 0.4 s the moment the aircraft
                    # reached home, and then sit in `executing` for the whole
                    # descent — so every following command was refused as busy
                    # while the handle claimed to be finished. The route gets
                    # the first band, the descent the rest, and 100 % now means
                    # the task is over.
                    descent_eta_s = cruise / NOMINAL_DESCENT_MPS
                    legs = await self._fly_legs(task, v, [{"lat": home[0], "lon": home[1],
                                                           "alt_m": cruise}],
                                                p.get("speed_mps", 10.0),
                                                blind_ok=blind_ok,
                                                pct_lo=0.0, pct_hi=RTB_CRUISE_PCT,
                                                tail_eta_s=descent_eta_s)
                    touchdown = await self.backend.land(
                        v, task=task, blind_ok=blind_ok,
                        pct_lo=RTB_CRUISE_PCT, pct_hi=100.0)
                    result = {"ok": True, "home": list(home), "reason": p.get("reason"),
                              "position_source": tele_src, "touchdown": touchdown,
                              **legs}
                    if tele_err:
                        result["telemetry_error"] = tele_err
            elif tool == "uav_goto_gps":
                goto_alt = _one_altitude("uav_goto_gps altitude",
                                         alt_agl_m=p.get("alt_agl_m"),
                                         alt_m=p.get("alt_m"))
                await self._ensure_airborne(v, goto_alt, blind_ok=blind_ok)
                legs = await self._fly_legs(task, v, [{"lat": p["lat"], "lon": p["lon"],
                                                       "alt_agl_m": goto_alt}],
                                            p.get("speed_mps", 10.0), blind_ok=blind_ok)
                result = {"ok": True, "at": [p["lat"], p["lon"]], **legs}
            elif tool == "uav_fly_route":
                # The auto-takeoff climbs to the FIRST waypoint's altitude, and
                # it must read the same spelling the leg will be flown at.
                # `wp.get("alt_m", 30.0)` ignored `alt_agl_m` — the spelling
                # TOOL_CONTRACT makes canonical — so a contract-conformant
                # route was climbed to a silent 30 m default instead of the
                # altitude it asked for (measured: a 120 m route took off to
                # 30 m). Resolved through the one resolver, so a waypoint with
                # NEITHER spelling is refused here exactly as `_fly_legs`
                # refuses it, rather than defaulted.
                first = p["waypoints"][0] if p["waypoints"] else None
                if first is None:
                    raise ValueError("route has no waypoints")
                first_alt = _one_altitude("uav_fly_route waypoint 1 altitude",
                                          alt_agl_m=first.get("alt_agl_m"),
                                          alt_m=first.get("alt_m"))
                climbed = await self._ensure_airborne(
                    v, first_alt, blind_ok=blind_ok, task=task,
                    pct_lo=0.0, pct_hi=ROUTE_CLIMB_PCT)
                # M2: the mission's capture schedule rides with the route and
                # is fired here, at the distances it was computed for.
                legs = await self._fly_legs(task, v, p["waypoints"],
                                            p.get("speed_mps", 10.0),
                                            blind_ok=blind_ok,
                                            captures=p.get("captures"),
                                            pct_lo=ROUTE_CLIMB_PCT if climbed else 0.0,
                                            pct_hi=100.0,
                                            repath=self._repath_route(task))
                result = {"ok": True, "legs": len(p["waypoints"]), **legs}
                loop = self._repath.get(task.mission_id or "")
                if loop is not None:
                    result["repaths"] = list(loop["repaths"])
            elif tool == "uav_hover":
                await self.backend.hover(v)
                result = {"ok": True}
            elif tool == "uav_abort":
                await self.backend.cancel_last(v)
                await self.backend.hover(v)
                result = {"ok": True, "aborted": True}
            else:
                raise ValueError(f"no executor for tool {tool}")
        except asyncio.CancelledError:
            # Release the blocking RPC in the sim so the executor thread can
            # unwind (real AirSim + fake both honor cancelLastTask).
            try:
                await self.backend.cancel_last(v)
            except Exception:
                pass
            # The terminal task/mission rows are NOT written here: at this point
            # the queue has not yet decided the final state, so a row written
            # from inside the executor claims "executing" forever. The queue's
            # terminal hook (`_journal_terminal_task`) is the single writer, and
            # it also covers the outcomes this block never sees — a watchdog
            # kill, a safety pre-emption, an abort before the task ever ran.
            raise
        except Exception as exc:
            self.store.log_audit("task_failed", str(exc), vehicle=v, tool=tool)
            raise
        return result

    async def _tele_or_last(self, vehicle: str, *,
                            blind_ok: bool) -> tuple[dict, str, str | None]:
        """Live telemetry, or the monitor's last known fix for a SAFETY task.

        A lost datalink is exactly the condition under which the lost-link plan
        must fly, and it is exactly the condition that makes `telemetry()`
        raise. Falling back is allowed ONLY for a safety transition, only to a
        fix the monitor actually recorded, and the source is always returned so
        the caller reports it rather than passing a stale fix off as live.
        """
        try:
            return await self._telemetry(vehicle), "live", None
        except Exception as exc:  # noqa: BLE001 — re-raised unless blind_ok
            err = f"{type(exc).__name__}: {exc}"
            last = self._last_tele.get(vehicle)
            if not blind_ok or last is None:
                raise
            self.store.log_audit(
                "telemetry_degraded",
                f"flying on the last known fix: {err}", vehicle=vehicle,
                fix_lat=last.get("lat"), fix_lon=last.get("lon"),
                fix_alt_agl_m=last.get("alt_agl_m"))
            return last, "last_known_fix", err

    def _capture_schedule(self, captures: list[dict] | None) -> list[dict]:
        """Validate and order an M2 capture schedule for execution.

        Every entry must carry the fields the trigger needs. A missing field is
        REFUSED rather than defaulted: `.get("camera", "0")` on a schedule built
        for camera "1" would silently image the wrong sensor and the INTREP
        would still say the captures were taken.
        """
        out: list[dict] = []
        for i, c in enumerate(captures or []):
            missing = [k for k in ("along_track_m", "camera") if c.get(k) is None]
            if missing:
                raise ValueError(
                    f"capture schedule entry {i} is missing {missing}; the "
                    "trigger point cannot be fired without them and no default "
                    "is substituted")
            out.append({"along_track_m": float(c["along_track_m"]),
                        "camera": str(c["camera"]),
                        "type": str(c.get("type") or "scene"),
                        "lat": c.get("lat"), "lon": c.get("lon"),
                        "leg": c.get("leg")})
        out.sort(key=lambda c: c["along_track_m"])
        return out

    def _repath_route(self, task):
        """The M5 server re-path loop (PLAN §4.3), as a `_fly_legs` hook.

        `missions.repath_needed` / `missions.repath_track` were written, unit
        tested, and called from nowhere — `missions.py` even carries the note
        "call repath_track() when repath_needed() is true (M5)" addressed to a
        caller that did not exist. So over MCP, the only interface a harness
        has, a `track_target` mission orbited the contact's FIRST fix for the
        whole flight: it did not track a moving contact, it orbited a memory of
        one.

        Returns None for any route that is not a live `track_target` mission (a
        lawnmower has no contact to follow). For one that is, the hook is
        consulted between legs and answers with a RE-CENTRED ring once the
        contact has moved past the plan's own `reacquire_move_m` tolerance.

        Three refusals, all journaled rather than silent:
          * the track has left the store -> the ring is not re-centred;
          * the re-plan does not verify (LOS blocked at the new fix, plan too
            large) -> the aircraft finishes the ring it is on rather than
            closing on an unverified standoff (M5/M14);
          * a re-path is never applied to a route carrying an M2 capture
            schedule (`_fly_legs` raises) — the trigger distances were measured
            against the route being replaced.

        BOUNDS. A re-path replaces the REMAINING route, so a contact that keeps
        moving keeps the route from running out: the mission then ends when the
        contact settles (the ring completes), when the M4 BINGO gate commits
        the vehicle to RTB, or when the operator cancels. Those are real bounds,
        not silent ones — but `Task.report_progress` is monotonic, so an
        indefinitely-followed contact drives progress to its high-water mark and
        the no-progress watchdog, not the doctrine, is what would time it out.
        "What progress means for an open-ended track" is a decision this defect
        fix does not make; it is named in the handoff.
        """
        mission_id = getattr(task, "mission_id", None)
        if not mission_id or mission_id not in self._repath:
            return None

        async def repath(leg_index: int) -> list[dict] | None:
            loop = self._repath.get(mission_id)
            if loop is None:
                return None
            now = time.monotonic()
            last = loop["checked_at"]
            if last is not None and now - last < loop["interval_s"]:
                return None
            loop["checked_at"] = now
            plan = loop["plan"]
            track = self.tracks.get(loop["track_id"])
            if track is None:
                self.store.log_audit(
                    "repath_skipped",
                    f"track {loop['track_id']} is no longer in the track store; "
                    "the orbit is NOT re-centred",
                    vehicle=loop["vehicle"], mission_id=mission_id)
                return None
            from .missions import repath_needed, repath_track
            if not repath_needed(plan, track):
                return None
            # The planner is synchronous and its LOS check reaches the sim, so
            # it runs off the executor's loop exactly as the original plan did.
            # The LOS policy is the mission's own: a re-path is verified the
            # way the plan it replaces was, never more loosely.
            new, err = await asyncio.to_thread(
                self._plan_or_error, repath_track, plan, track,
                los_check=(None if loop["allow_unverified"]
                           else self._los_check(loop["vehicle"])),
                allow_unverified=loop["allow_unverified"])
            if err:
                detail = (err.get("error") or {}).get("message") or str(err)
                self.store.log_audit(
                    "repath_refused",
                    f"track {loop['track_id']} moved, but the re-planned "
                    f"standoff did not verify: {detail}",
                    vehicle=loop["vehicle"], mission_id=mission_id,
                    track_id=loop["track_id"], **err)
                return None
            self._stamp_phase_fov(new)
            entry = {"track_id": loop["track_id"],
                     "moved_m": new.meta.get("repath_moved_m"),
                     "from_poi": [round(c, 6) for c in plan.meta["poi"]],
                     "poi": [round(c, 6) for c in new.meta["poi"]],
                     "standoff_m": new.meta["standoff_m"],
                     "waypoints": len(new.waypoints),
                     "leg_index": leg_index}
            loop["plan"] = new
            loop["repaths"].append(entry)
            rec = self.missions.get(mission_id)
            if rec is not None:
                # `mission_status` and `uav://mission/{id}` must describe the
                # ring the aircraft is ACTUALLY flying, not the one it was
                # launched on.
                rec["meta"] = dict(new.meta)
                rec["waypoints"] = new.to_route()
                rec["repaths"] = list(loop["repaths"])
            self.store.log_mission(mission_id, "repath", vehicle=loop["vehicle"],
                                   **entry)
            self.store.log_audit(
                "repath",
                f"track {loop['track_id']} moved {entry['moved_m']} m; the "
                f"{float(entry['standoff_m']):.0f} m standoff ring is "
                "re-centred on its current fix (M5)",
                vehicle=loop["vehicle"], mission_id=mission_id, **entry)
            return new.to_route()

        return repath

    async def _fly_legs(self, task, vehicle: str, waypoints: list[dict],
                        speed_mps: float, *, blind_ok: bool = False,
                        captures: list[dict] | None = None,
                        pct_lo: float = 0.0, pct_hi: float = 100.0,
                        tail_eta_s: float = 0.0,
                        repath: Callable[[int], Awaitable[list[dict] | None]]
                        | None = None) -> dict:
        """Fly a route, deriving progress_pct from telemetry (T4a/T2).

        Progress is distance-closed over the whole plan, so a long healthy leg
        keeps the watchdog fed while a vehicle that stops closing does not.

        `pct_lo`/`pct_hi` map that distance onto a BAND of the task's progress.
        A command with work left after the last waypoint (an RTB still has to
        land) flies its route in 0..pct_hi, so `progress_pct == 100` keeps
        meaning "this task is finished" rather than "the aircraft arrived and
        is now doing something else for the next 30 s". `tail_eta_s` is added
        to the reported eta for that same reason.

        `captures` is the M2 distance-triggered schedule. It is EXECUTED here:
        each trigger fires a real capture as the aircraft passes its along-track
        distance, so a recon route collects the imagery it planned instead of
        merely reporting how many frames it would have taken.

        `repath` is the M5 server re-path loop (`_repath_route`). It is asked,
        before each leg, whether the route it is flying is still the right one;
        a non-empty answer REPLACES every waypoint from here on and the
        remaining distance is re-measured from where the aircraft actually is,
        so `progress_pct` keeps meaning "of the route I am now flying".
        Progress stays monotonic (`tasking.Task.report_progress` takes the max
        — a re-plan must not walk the bar backwards).

        Returns what the flight could and could NOT be confirmed to have done:
        `legs_unverified` is non-empty when a safety task flew a leg with the
        datalink down, so the caller never reports an arrival it did not see.
        """
        if not waypoints:
            # An empty route reports no progress, so the watchdog would kill it
            # 120 s later with a confusing "no progress" error. Fail loudly now.
            raise ValueError("route has no waypoints")
        if not (0.0 <= pct_lo < pct_hi <= 100.0):
            raise ValueError(
                f"progress band ({pct_lo}, {pct_hi}) is not a rising band in 0..100")
        tele, tele_src, tele_err = await self._tele_or_last(vehicle, blind_ok=blind_ok)
        unverified: list[dict] = []
        #: Waypoints whose leg actually CLOSED. `task.waypoint` is the leg being
        #: flown, which is one ahead of this — crediting it as imaged ground
        #: reports an area as cleared while the aircraft is still crossing it.
        reached = 0
        start_ned = tuple(tele["ned"]) if tele.get("ned") else None
        lat, lon = tele["lat"], tele["lon"]
        legs_m: list[float] = []
        for wp in waypoints:
            d = haversine_m(lat, lon, float(wp["lat"]), float(wp["lon"]))
            legs_m.append(d)
            lat, lon = float(wp["lat"]), float(wp["lon"])
        total_m = sum(legs_m) or 1.0
        done_m = 0.0
        n = len(waypoints)
        commanded_fov: float | None = None
        schedule = self._capture_schedule(captures)
        # M2 ORIGIN: `missions.capture_points` measures `along_track_m` from
        # route waypoint 1, but the distance this loop accumulates is measured
        # from wherever the AIRCRAFT happens to be — and the ingress leg to
        # waypoint 1 is part of that. Comparing the two directly fired every
        # trigger `ingress_m` early: measured over the wire, a 936 m ingress
        # onto a 161 m strip took all five frames ~935 m off the tasked route
        # and the INTREP still read "5 of 5 collected, 0 missed". The schedule
        # is anchored on waypoint 1 here, so a trigger fires at the GROUND
        # POINT it was planned for.
        schedule_origin_m = legs_m[0] if schedule else 0.0
        pending = list(schedule)
        taken: list[dict] = []
        cap_failed: list[dict] = []
        tid = getattr(task, "id", None)
        if schedule and tid:
            # Live tally so `mission_status` can report collection DURING the
            # run, not only once the task's result exists.
            self._capture_progress[tid] = {
                "captures_planned": len(schedule), "captures_taken": 0,
                "captures_missed": 0}

        def tally() -> None:
            if schedule and tid:
                self._capture_progress[tid] = {
                    "captures_planned": len(schedule),
                    "captures_taken": len(taken),
                    "captures_missed": len(cap_failed)}

        async def fire_due(along_m: float) -> None:
            """Fire every capture whose trigger POINT has been passed (M2).

            `along_m` is distance flown from the aircraft's start; the
            schedule's `along_track_m` is distance from route waypoint 1. The
            two are related by `schedule_origin_m`, and conflating them images
            the ingress leg instead of the route.
            """
            while pending and (schedule_origin_m + pending[0]["along_track_m"]
                               <= along_m + 1e-9):
                c = pending.pop(0)
                try:
                    meta = await self._capture_frame(vehicle, c["camera"], c["type"])
                except Exception as exc:  # noqa: BLE001 — recorded as a MISS
                    detail = f"{type(exc).__name__}: {exc}"
                    cap_failed.append({"along_track_m": c["along_track_m"],
                                       "camera": c["camera"], "type": c["type"],
                                       "error": detail})
                    self.store.log_audit(
                        "capture_missed",
                        f"scheduled capture at {c['along_track_m']:.0f} m "
                        f"along track did not fire: {detail}",
                        vehicle=vehicle, camera=c["camera"],
                        along_track_m=c["along_track_m"],
                        task_id=tid)
                    tally()
                    continue
                taken.append({"along_track_m": c["along_track_m"],
                              "planned_lat": c["lat"], "planned_lon": c["lon"],
                              "camera": c["camera"], "type": c["type"],
                              "resource": meta["resource"],
                              "frame_id": meta["frame_id"],
                              "geo_pose": meta.get("geo_pose"),
                              "captured_at": meta["captured_at"]})
                tally()

        i = 0
        repaths = 0
        while i < len(waypoints):
            # M5: is the route still the right route? Asked BEFORE the leg is
            # commanded, so a contact that has moved is followed on this leg
            # rather than after the stale ring has been flown to the end.
            if repath is not None:
                new_wps = await repath(i)
                if new_wps:
                    if pending:
                        # Every trigger distance in the schedule was measured
                        # against the route being replaced. Firing them against
                        # a different route images ground nobody tasked, and
                        # the tally would still read "n of n collected".
                        raise ValueError(
                            "a re-path was returned for a route carrying an M2 "
                            f"capture schedule ({len(pending)} trigger(s) still "
                            "pending); the trigger distances belong to the route "
                            "being replaced and are not transferable")
                    waypoints = list(waypoints[:i]) + [dict(w) for w in new_wps]
                    n = len(waypoints)
                    repaths += 1
                    # Re-measure the remaining route from where the aircraft
                    # actually IS, not from the waypoint it was last sent to:
                    # the re-path may have happened mid-ring.
                    here, _src, _err = await self._tele_or_last(
                        vehicle, blind_ok=blind_ok)
                    cur_lat, cur_lon = here["lat"], here["lon"]
                    tail: list[float] = []
                    for wp2 in waypoints[i:]:
                        d = haversine_m(cur_lat, cur_lon,
                                        float(wp2["lat"]), float(wp2["lon"]))
                        tail.append(d)
                        cur_lat, cur_lon = float(wp2["lat"]), float(wp2["lon"])
                    legs_m = legs_m[:i] + tail
                    total_m = (done_m + sum(tail)) or 1.0
                    # The NED start hint is only read on leg 0; re-seed it from
                    # the fix we just took rather than dropping it, so a
                    # re-path before the first leg keeps its progress datum.
                    start_ned = (tuple(here["ned"])
                                 if i == 0 and here.get("ned") else None)
            wp = waypoints[i]
            leg_len = legs_m[i]
            idx = i

            # M7: a plan phase that changes the field of view commands it here,
            # at the waypoint where the phase starts. A failure is FATAL to the
            # leg rather than logged and flown: the plan's pixels-on-target
            # figures were computed for this field, and flying the pass at the
            # previous one would report an identification that never happened.
            fov = wp.get("fov_deg")
            if fov is not None and fov != commanded_fov:
                cam_state = await self.backend.set_camera_fov(
                    vehicle, str(wp.get("camera", "0")), float(fov))
                got = cam_state.get("fov_deg")
                if got is None or abs(float(got) - float(fov)) > 0.1:
                    raise RuntimeError(
                        f"uav_set_fov({fov:.2f} deg) read back {got!r} on "
                        f"{vehicle}/{wp.get('camera', '0')}: the "
                        f"{wp.get('phase')} phase cannot be flown at the field "
                        "its pixel-density maths assumed (M7)")
                commanded_fov = float(fov)
                self.store.log_audit(
                    "sensor_fov", f"{wp.get('phase')} phase at {fov:.2f} deg",
                    vehicle=vehicle, camera=str(wp.get("camera", "0")),
                    fov_deg=float(fov), waypoint=i + 1,
                    task_id=getattr(task, "id", None))

            # `_total`/`_n` are bound HERE, like `_done`/`_idx`/`_len`: a
            # re-path rewrites both between legs, and this closure must report
            # against the route the leg it belongs to is being flown on.
            async def on_progress(remaining_m: float, leg_m: float,
                                  _done=done_m, _idx=idx, _len=leg_len,
                                  _total=total_m, _n=n) -> None:
                flown = max(0.0, min(_len, _len - remaining_m))
                along = _done + flown
                if pending:
                    await fire_due(along)
                pct = min(pct_lo + (pct_hi - pct_lo) * (along / _total),
                          EXEC_PROGRESS_CEILING)
                remaining_total = max(0.0, _total - along)
                await task.report_progress(
                    pct, waypoint=_idx + 1, waypoints_total=_n,
                    eta_s=remaining_total / max(0.5, speed_mps) + tail_eta_s,
                    note=f"leg {_idx + 1}/{_n}, {remaining_m:.0f} m to waypoint")

            # `alt_agl_m` is the contract spelling; `alt_m` is the legacy one
            # for the same AGL datum. A waypoint with NEITHER used to default
            # to 0 m AGL — i.e. fly the leg at ground level — so it is refused.
            try:
                wp_alt = _one_altitude(
                    f"waypoint {i + 1} altitude",
                    alt_agl_m=wp.get("alt_agl_m"), alt_m=wp.get("alt_m"))
            except ValueError as exc:
                raise ValueError(
                    f"route waypoint {i + 1} of {n}: {exc}") from exc
            ned = self.backend.llh_agl_to_ned(float(wp["lat"]), float(wp["lon"]),
                                              float(wp_alt))
            arrival = await self.backend.goto_ned(
                vehicle, ned[0], ned[1], ned[2], speed_mps,
                on_progress=on_progress, blind_ok=blind_ok,
                start_ned=start_ned if i == 0 else None)
            if isinstance(arrival, dict) and not arrival.get("verified", True):
                unverified.append({"waypoint": i + 1, "lat": float(wp["lat"]),
                                   "lon": float(wp["lon"]),
                                   "reason": arrival.get("reason")})
                self.store.log_audit(
                    "leg_unverified", arrival.get("reason") or "arrival not observed",
                    vehicle=vehicle, waypoint=i + 1,
                    task_id=getattr(task, "id", None))
            start_ned = None
            reached = i + 1
            done_m += leg_len
            # The leg closed, so its triggers are behind the aircraft even if
            # the poll loop never sampled at exactly that distance. A blind
            # leg reports no progress at all, so this is the ONLY place its
            # captures can fire.
            if pending:
                await fire_due(done_m)
            await task.report_progress(
                min(pct_lo + (pct_hi - pct_lo) * (done_m / total_m),
                    EXEC_PROGRESS_CEILING),
                waypoint=i + 1, waypoints_total=n,
                eta_s=max(0.0, total_m - done_m) / max(0.5, speed_mps) + tail_eta_s)
            i += 1
        if pending:
            # The last waypoint is behind the aircraft, so every trigger that
            # lies ON the planned route has been overflown — even the final
            # one, which the flown distance can miss by centimetres because the
            # plan measures arc length flat-earth and this loop measures it
            # with haversine. `missions.route_length_m` is the planner's OWN
            # metric, so comparing the schedule against it reconciles the two
            # conventions exactly instead of guessing a tolerance.
            from .missions import route_length_m
            planned_route_m = route_length_m(
                [(float(w["lat"]), float(w["lon"])) for w in waypoints])
            await fire_due(schedule_origin_m + planned_route_m)
            # Beyond the planned route: each is a FRAME THAT DOES NOT EXIST, so
            # it is counted as a gap with its reason. Dropping them would let a
            # mission report `planned=20, taken=18, missed=0` — the exact
            # silent shape the capture tally exists to make impossible.
            for c in pending:
                reason = (
                    f"trigger at {c['along_track_m']:.1f} m along track lies "
                    f"beyond the {planned_route_m:.1f} m route that was flown; "
                    "no frame was taken")
                cap_failed.append({"along_track_m": c["along_track_m"],
                                   "camera": c["camera"], "type": c["type"],
                                   "error": reason})
                self.store.log_audit("capture_missed", reason, vehicle=vehicle,
                                     camera=c["camera"],
                                     along_track_m=c["along_track_m"],
                                     task_id=tid)
            pending.clear()
            tally()
        out: dict = {"legs_unverified": unverified,
                     "position_source": tele_src,
                     "waypoints_reached": reached,
                     "waypoints_total": n}
        if repath is not None:
            # How many times the route the aircraft was flying was replaced
            # under it (M5). Zero is a real answer: the contact held still.
            out["repath_count"] = repaths
        if tele_err:
            out["telemetry_error"] = tele_err
        if schedule:
            # M2: what the schedule PLANNED against what the aircraft actually
            # collected. `captures_taken < captures_planned` is a real gap in
            # the imagery and the INTREP reports it as one.
            out.update({
                "captures_planned": len(schedule),
                "captures_taken": len(taken),
                "captures_missed": len(cap_failed),
                "captures": taken,
                "capture_failures": cap_failed,
                "capture_trigger": "distance",
            })
        return out

    async def _ensure_airborne(self, vehicle: str, alt_m: float, *,
                               blind_ok: bool = False,
                               task: Task | None = None,
                               pct_lo: float = 0.0,
                               pct_hi: float = 100.0) -> bool:
        """Movement tools auto-takeoff when landed (missions submit fly_route
        directly; doctrine: a mission is self-contained).

        Returns True when it actually commanded a climb, so the caller knows
        whether the head band was used.

        `task` + the band matter now that the climb honours the ROUTE's
        altitude rather than a flat 30 m: a climb to a mission ceiling can take
        longer than the no-progress watchdog window, and a climb that reports
        nothing is indistinguishable from a stalled one. It reports inside
        `pct_lo..pct_hi` so the watchdog sees the aircraft moving without the
        climb consuming the whole progress bar.

        With the datalink down there is no fix to decide on, and `takeoff`
        itself confirms the climb on telemetry it cannot read — so a blind
        safety task skips this rather than failing on it. The vehicle it is
        recovering is airborne by construction.
        """
        try:
            t = await self._telemetry(vehicle)
        except Exception:  # noqa: BLE001 — re-raised unless this is blind safety
            if not blind_ok:
                raise
            self.store.log_audit(
                "telemetry_degraded",
                "airborne check skipped: no telemetry (safety task flying blind)",
                vehicle=vehicle)
            return False
        if int(t.get("landed_state", 0)) == 0:  # 0 = landed
            await self.backend.takeoff(vehicle, alt_agl_m=max(3.0, float(alt_m)),
                                       task=task, pct_lo=pct_lo, pct_hi=pct_hi)
            return True
        return False

    async def _watchdog_recovery(self, task, reason: str) -> str:
        """T2: a watchdog timeout fails the handle AND recovers to hover."""
        v = task.vehicle
        try:
            await self.backend.cancel_last(v)
            await self.backend.hover(v)
            out = "hover"
        except Exception as exc:  # noqa: BLE001 — recorded on the handle
            out = f"hover failed: {type(exc).__name__}: {exc}"
        # A watchdog kill of an UN-CANCELLABLE SAFETY transition (the BINGO
        # force-RTB) is a different event from killing a harness task: the
        # aircraft is on its way home on the fuel it has left, and the only
        # thing that brings it back is `_force_rtb` re-committing on the next
        # monitor tick. Stamping it on the row so the journal distinguishes
        # them — reading `watchdog_timeout` rows and finding a dead RTB is how
        # this was caught at all.
        self.store.log_audit("watchdog_timeout", reason, vehicle=v, tool=task.tool,
                             task_id=task.id, recovery=out,
                             safety=bool(getattr(task, "safety", False)),
                             uncancellable=bool(getattr(task, "uncancellable", False)),
                             progress_pct=round(task.progress_pct, 1))
        return out

    # ---- safety gate shared by movement tools (M4) ----
    async def _gate(self, vehicle: str, waypoints: list[dict], speed: float) -> dict:
        """Pre-flight BINGO + geofence gate, from where the vehicle ACTUALLY is.

        Starting every plan at `envelope.home` omitted the ingress leg and the
        whole M15 headwind penalty; both are priced here.
        """
        fm = self.fuel_for(vehicle)
        try:
            tele = await self._telemetry(vehicle)
        except Exception as exc:  # no silent fallback: an ungateable plan is rejected
            gate = {"ok": False, "error": f"telemetry unavailable: {type(exc).__name__}: {exc}",
                    "envelope_violations": ["telemetry_unavailable"], "required_pct": None}
            self.store.log_audit("preflight_reject", "gate could not read telemetry",
                                 vehicle=vehicle, gate=gate)
            return gate
        start = (tele["lat"], tele["lon"])
        wind_ne, wind_source = await self.backend.wind_ne()
        # PRICED altitude -> the launch datum (`commanded_agl`). The waypoints
        # this is integrated against carry launch-datum altitudes, and the
        # return leg lets down to 0 over HOME; starting that integration from a
        # terrain-relative height mixes two datums inside one estimate.
        start_agl = self.commanded_agl(tele)
        gate = fm.preflight_gate(waypoints, start, speed,
                                 start_alt_m=start_agl, wind_ne=wind_ne)
        violations = self.envelope.check_route(waypoints) + self.envelope.check_speed(speed)
        gate["envelope_violations"] = violations
        gate["start"] = [round(start[0], 6), round(start[1], 6)]
        gate["start_alt_agl_m"] = start_agl
        gate["start_alt_agl_datum"] = AGL_SOURCE_LAUNCH
        gate["start_alt_agl_measured_m"] = tele["alt_agl_m"]
        gate["wind_ne_mps"] = [round(wind_ne[0], 2), round(wind_ne[1], 2)]
        gate["wind_source"] = wind_source
        gate["bingo_fuel_pct"] = round(
            fm.bingo_fuel_pct(start, start_agl, wind_ne=wind_ne), 2)
        gate["ok"] = gate["ok"] and not violations
        if not gate["ok"]:
            self.store.log_audit("preflight_reject", "pre-flight gate rejected plan",
                                 vehicle=vehicle, gate=gate)
        return gate

    def _submit(self, vehicle: str, tool: str, params: dict,
                idempotency_key: str | None, **kw) -> dict:
        """Submit through the queue, honouring the busy contract (T2)."""
        try:
            task = self.tasking.submit(vehicle, tool, params,
                                       idempotency_key=idempotency_key, **kw)
        except VehicleBusyError as exc:
            self.store.log_audit("busy", f"{tool} rejected: vehicle busy",
                                 vehicle=vehicle, tool=tool,
                                 current_task_id=exc.current.id)
            return {"status": "busy", "current": exc.current.handle(),
                    "rejected_tool": tool, "vehicle": vehicle,
                    "mission_status": self.mission_flags.get(vehicle)}
        handle = task.handle()
        if task.replays:
            # T4b: a replayed key returns the ORIGINAL handle and re-executes
            # nothing. Say so, so the caller can tell it from a fresh submit.
            handle["status"] = "duplicate"
            handle["idempotent_replay"] = task.replays
            return handle
        handle["status"] = "accepted"
        self.store.log_task(handle, "submitted",
                            params=self._journal_payload(params),
                            idempotency_key=idempotency_key)
        return handle

    #: Payload keys whose full contents are deliberately kept OUT of the task
    #: journal, replaced by a count and a pointer. A capture schedule may hold
    #: `missions.MAX_PLAN_CAPTURES` (20 000) entries — roughly 2 MB per row,
    #: written on submit, on start and again on the terminal transition. This
    #: project has already lost a run to 405 MB of capture dicts. Nothing is
    #: lost: every frame that fires is recorded individually in the audit trail
    #: (`capture_image` / `capture_missed`), and the elision says so rather than
    #: quietly shortening the record.
    _JOURNAL_ELIDE = {"captures": "capture schedule",
                      "capture_failures": "capture failures"}

    @classmethod
    def _journal_payload(cls, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        out: dict = {}
        for k, v in payload.items():
            if k in cls._JOURNAL_ELIDE and isinstance(v, list):
                out[f"{k}_n"] = len(v)
                out[f"{k}_elided"] = (
                    f"{cls._JOURNAL_ELIDE[k]}: {len(v)} entries not written to "
                    "this journal (size); each one is in the audit trail")
            else:
                out[k] = v
        return out

    #: tasking.TaskState -> the journal event name `store.TERMINAL_TASK_EVENTS`
    #: recognises. Anything missing from this map would journal an event the
    #: replay does not treat as terminal, so it is asserted, never defaulted.
    _TERMINAL_EVENTS = {"done": "completed", "failed": "failed",
                        "cancelled": "cancelled"}

    def _journal_terminal_task(self, task: Task) -> None:
        """Write the row that says how a task actually ENDED (T4c).

        Fired by the queue once the task is terminal, so `state` in the row is
        the real terminal state. Before this existed the journal was written
        from inside the executor, where `task.state` was still "executing" —
        every row in a real `tasks.jsonl` read queued/executing/executing, no
        task ever reached `done` or `failed` on disk, and a task cancelled
        before it started got no closing row at all. `Store.replay()` then
        could not tell a completed mission from an abandoned one, which
        silently defeated the whole restart-recovery decision.
        """
        state = task.state.value
        event = self._TERMINAL_EVENTS.get(state)
        if event is None:  # pragma: no cover — a new TaskState must be mapped
            # The queue reports the raised error on `task.terminal_errors`,
            # which nobody polls, so the audit trail is told first: a state the
            # journal cannot name is a hole in restart recovery, not a detail.
            msg = (f"task {task.id} finalized in unmapped state {state!r}; add "
                   "it to _TERMINAL_EVENTS or the restart replay will read it "
                   "as work still in flight")
            self.store.log_audit("terminal_state_unmapped", msg,
                                 vehicle=task.vehicle, task_id=task.id,
                                 tool=task.tool, state=state)
            raise ValueError(msg)
        self.store.log_task(task.handle(), event,
                            params=self._journal_payload(task.params),
                            result=self._journal_payload(task.result),
                            error=task.error,
                            recovery=task.recovery,
                            finished_at=task.finished_at)
        if task.mission_id:
            rec = self.missions.get(task.mission_id)
            if rec is not None:
                rec["state"] = state
                rec["ended"] = task.finished_at
            # M5: the flight is over, so the re-path loop for it is over. The
            # re-paths it performed stay on the mission record and in the
            # journal; only the live plan is dropped.
            self._repath.pop(task.mission_id, None)
            self.store.log_mission(task.mission_id, event,
                                   vehicle=task.vehicle, task_id=task.id,
                                   status=self.mission_flags.get(task.vehicle),
                                   error=task.error)

    # ---- telemetry + safety tick loop (T5/M4/M9) ----
    async def tick_once(self, vehicle: str, now: float | None = None) -> dict:
        """One telemetry sample -> fuel integral, envelope, link, enforcement.

        This is the loop R5 said did not exist: without it `fuel_pct` is a
        constant 100.0 and BINGO can never fire.
        """
        mon = self.monitor_for(vehicle)
        link_up, tele, link_err = True, None, None
        try:
            tele = await self._telemetry(vehicle)
        except Exception as exc:  # a lost datalink is exactly this (M9)
            link_up = False
            link_err = f"{type(exc).__name__}: {exc}"
        sim_link = await self.backend.link_state(vehicle)
        degraded = sim_link == "degraded"
        if sim_link == "lost":
            link_up = False
        harness_down = self._harness_down_all or vehicle in self._harness_down
        if harness_down:
            link_up = False
        if tele is not None:
            self._last_tele[vehicle] = tele
        src = tele if tele is not None else self._last_tele.get(vehicle)
        wind_ne, wind_source = await self.backend.wind_ne()
        if tele is None or src is None:
            # No measurement this tick: the fuel integrator is NOT advanced on
            # a guess. The link machine still runs — that is what reacts.
            self._missed_ticks[vehicle] = self._missed_ticks.get(vehicle, 0) + 1
            event = mon.link.observe(link_up, degraded=degraded, now=now,
                                     bingo_latched=mon.fuel.bingo.tripped)
            verdict = {
                "t": time.monotonic() if now is None else now,
                "vehicle": vehicle, "telemetry": False,
                "telemetry_error": link_err,
                "harness_disconnected": harness_down,
                "missed_ticks": self._missed_ticks[vehicle],
                "fuel_pct": round(mon.fuel.fuel_pct, 3),
                "link": mon.link.to_dict(now), "link_event": event,
                "alarms": [], "violations": [], "breaches": [],
                "force_rtb": mon.link.action is LostLinkBehaviour.RTB,
                "rtb_reasons": (["lost_link"] if mon.link.action is LostLinkBehaviour.RTB else []),
                "wind_source": wind_source,
                "mission_status": self.mission_flags.get(vehicle),
            }
        else:
            landed_now = int(src.get("landed_state", 0)) == 0
            # `SafetyMonitor.tick` takes ONE altitude and hands it to BOTH the
            # fuel model and the envelope, which want DIFFERENT quantities. The
            # fuel model's return leg lets down to 0 over HOME, so it is priced
            # on the launch datum (`commanded_agl`) — that is what goes in. The
            # terrain floor is a different question and is asked separately,
            # below, against the ground actually under the aircraft.
            verdict = mon.tick(
                lat=src["lat"], lon=src["lon"],
                alt_agl_m=self.commanded_agl(src),
                speed_mps=src["speed_mps"], vz_mps=src["vz_mps"],
                landed=landed_now,
                vx_mps=src.get("vx_mps"), vy_mps=src.get("vy_mps"),
                wind_ne=wind_ne, link_up=link_up, link_degraded=degraded, now=now)
            verdict["vehicle"] = vehicle
            verdict["telemetry"] = True
            verdict["harness_disconnected"] = harness_down
            verdict["wind_source"] = wind_source
            verdict["alt_agl_m"] = src["alt_agl_m"]
            # Whether the envelope's min-AGL was just checked against MEASURED
            # terrain or against the launch datum. `_telemetry` resolved it;
            # this carries the answer onto every tick record so a breach can
            # never be read as terrain-verified when it was not.
            verdict["alt_agl_is_real"] = src.get("alt_agl_is_real", False)
            verdict["alt_agl_source"] = src.get("alt_agl_source", AGL_SOURCE_LAUNCH)
            verdict["alt_agl_launch_datum_m"] = self.commanded_agl(src)
            self._merge_terrain_floor(vehicle, verdict, src, landed=landed_now)
            # T5: persist the integral so a restart can recover the fuel clock.
            self.store.log_fuel(vehicle, bingo_fuel_pct=verdict["bingo"]["bingo_fuel_pct"],
                                wind_source=wind_source, **verdict["fuel_record"])
        self.ticks[vehicle] = verdict
        await self._audit_tick(vehicle, verdict)
        await self._enforce(vehicle, verdict)
        return verdict

    def _merge_terrain_floor(self, vehicle: str, verdict: dict, src: dict, *,
                             landed: bool) -> None:
        """Check the ALTITUDE limits a second time, against MEASURED ground.

        The envelope's `min_agl_m` / `ceiling_m_agl` are documented as heights
        above the launch datum, and that is the datum `mon.tick` was just given
        — it has to be, because the same number prices the fuel model's let-down
        to HOME. But "am I about to fly into the hill?" is a question about the
        ground under the aircraft, and the launch datum cannot answer it: over a
        95 m ridge the aircraft reads a comfortable 40 m while it is 42 m below
        the crest.

        So the altitude limits are asked a SECOND time here, against the
        measured AGL, and any kind the datum pass did not already raise is
        merged in, tagged `datum: "terrain"`. Nothing is silently replaced: the
        launch-datum verdict is untouched, `terrain_floor_violations` lists what
        only the measured pass saw, and `terrain_floor_checked` says whether the
        question could be asked at all — an absent flag reads as "fine".

        Only altitude kinds are merged. The geofence is a lat/lon test and gives
        the same answer on either datum; merging it would double-count the
        breach that commits the vehicle to an RTB.
        """
        verdict["terrain_floor_checked"] = False
        verdict["terrain_floor_violations"] = []
        active = self._terrain_floor_active.setdefault(vehicle, set())
        if not src.get("alt_agl_is_real"):
            verdict["terrain_floor_reason"] = (
                src.get("alt_agl_reason")
                or "no measured terrain under the aircraft on this tick; the "
                   "altitude limits were checked against the launch datum only")
            active.clear()
            return
        measured = float(src["alt_agl_m"])
        datum_agl = self.commanded_agl(src)
        verdict["terrain_floor_checked"] = True
        verdict["terrain_agl_m"] = measured
        verdict["terrain_hae_m"] = src.get("terrain_hae_m")
        if landed:
            active.clear()
            return
        seen = {v["kind"] for v in verdict.get("violations") or []}
        extra = [v for v in self.envelope.check_position(
            src["lat"], src["lon"], measured, landed=False)
            if v.kind in ("ceiling", "min_agl") and v.kind not in seen]
        # Edge-triggered audit, like `SafetyMonitor._alarm_edges`: the monitor
        # ticks twice a second and an aircraft sits over high ground for whole
        # minutes, so logging every tick would bury the journal.
        now_kinds = {v.kind for v in extra}
        for gone in sorted(active - now_kinds):
            self.store.log_audit(
                "envelope_transition",
                f"{gone} against measured terrain cleared "
                f"({measured:.1f} m AGL)", vehicle=vehicle, alarm=gone,
                datum="terrain", state="clear",
                measured_agl_m=round(measured, 2))
        active.intersection_update(now_kinds)
        if not extra:
            return
        rows = []
        for v in extra:
            row = v.to_dict()
            row["datum"] = "terrain"
            row["measured_agl_m"] = round(measured, 2)
            row["launch_datum_agl_m"] = round(datum_agl, 2)
            row["terrain_hae_m"] = src.get("terrain_hae_m")
            rows.append(row)
            if v.kind in active:
                continue
            active.add(v.kind)
            self.store.log_audit(
                _BREACH_AUDIT_KIND.get(v.kind, v.kind),
                f"{v.message} against MEASURED terrain "
                f"({measured:.1f} m AGL over ground at "
                f"{src.get('terrain_hae_m')} m HAE); the launch datum reads "
                f"{datum_agl:.1f} m and sees nothing",
                vehicle=vehicle, alarm=v.kind, datum="terrain", state="raised",
                measured_agl_m=round(measured, 2),
                launch_datum_agl_m=round(datum_agl, 2))
        verdict["terrain_floor_violations"] = rows
        verdict["violations"] = list(verdict.get("violations") or []) + rows
        verdict["breaches"] = list(verdict.get("breaches") or []) + [
            v.kind for v in extra if v.is_breach]

    def _vertical_transition(self, vehicle: str,
                             verdict: dict | None = None) -> str | None:
        """The tool that legitimately takes the vehicle through the min-AGL band.

        `uav_takeoff` and `uav_land` are transitions by definition.
        `uav_return_to_home` ENDS in a landing, and it is the most common
        landing in this system — every BINGO RTB, every lost-link RTB, every
        restart RTH finishes with one. Its touchdown was audited as an in-flight
        `envelope_breach`, a kind `store.SAFETY_AUDIT_KINDS` treats as a safety
        event, so the next restart decided ABORT_RTH and flew yet another RTB
        off the evidence of the previous one landing normally. That is exactly
        the replay-misreading the `envelope_transition` kind exists to stop.

        An RTB counts only OVER HOME, where its landing happens: an RTB that
        crosses the AO at 2 m AGL is still a breach and is still audited as
        one. Position is the discriminator rather than the measured vertical
        phase because `vz_mps` is not a reliable descent signal here — the sim
        reports 0.0 through a takeoff and a landing alike, so the phase during
        a touchdown comes back `hover` or even `climb`.
        """
        cur = self.tasking.queue_for(vehicle).current
        if cur is None:
            return None
        if cur.tool in ("uav_takeoff", "uav_land"):
            return cur.tool
        if cur.tool == "uav_return_to_home":
            rec = (verdict or {}).get("fuel_record") or {}
            home = self.envelope.home
            lat, lon = rec.get("lat"), rec.get("lon")
            if lat is None:
                last = self._last_tele.get(vehicle) or {}
                lat, lon = last.get("lat"), last.get("lon")
            if home is None or lat is None:
                return None
            if haversine_m(lat, lon, home[0], home[1]) <= RTB_LANDING_RADIUS_M:
                return cur.tool
        return None

    async def _audit_tick(self, vehicle: str, verdict: dict) -> None:
        transition = self._vertical_transition(vehicle, verdict)
        suppressed: list[str] = []
        for alarm in verdict.get("alarms") or []:
            raw = alarm.get("kind", "alarm")
            kind = "bingo" if raw == "bingo" else _BREACH_AUDIT_KIND.get(raw, raw)
            if raw == "min_agl" and transition is not None:
                # A takeoff or a landing flies through the min-AGL band by
                # definition. Still logged — under a kind a restart replay does
                # not read as an in-flight safety breach — never dropped.
                kind = "envelope_transition"
                suppressed.append(raw)
            message = alarm.get("message") or f"{raw} {alarm.get('state')}"
            self.store.log_audit(kind, f"{message} [{alarm.get('state')}]",
                                 vehicle=vehicle, alarm=raw,
                                 transition=transition,
                                 **{k: v for k, v in alarm.items()
                                    if k not in ("kind", "message")})
        if suppressed:
            verdict["transition_suppressed"] = suppressed
            verdict["transition_tool"] = transition
        event = verdict.get("link_event")
        if event:
            # `state` is log_link_event's own positional (the link transition);
            # the monitor's own "state" field rides along under another name so
            # the two cannot collide.
            fields = {k: v for k, v in event.items() if k != "state"}
            self.store.log_link_event(vehicle, event.get("event", "link"),
                                      link_machine_state=event.get("state"), **fields)
            self.loal_events.append({"vehicle": vehicle, **event})

    async def _enforce(self, vehicle: str, verdict: dict) -> None:
        """Act on the verdict: BINGO force-RTB, geofence breach, lost link."""
        reasons = list(verdict.get("rtb_reasons") or [])
        event = verdict.get("link_event") or {}
        if event.get("event") in ("loal_declared", "loal_escalated"):
            await self._execute_lost_link(vehicle, verdict)
            return
        if verdict.get("bingo", {}).get("latched") or "bingo" in reasons:
            await self._force_rtb(vehicle, reason="bingo",
                                  detail=f"fuel {verdict.get('fuel_pct')}% at/below BINGO "
                                         f"{verdict.get('bingo', {}).get('bingo_fuel_pct')}%",
                                  mission_status=MISSION_INCOMPLETE_FUEL)
        elif "geofence" in reasons:
            await self._force_rtb(vehicle, reason="geofence",
                                  detail="geofence breach in flight")

    async def _force_rtb(self, vehicle: str, reason: str, detail: str = "",
                         mission_status: str | None = None) -> dict | None:
        """Pre-empt the queue and commit the vehicle to an un-cancellable RTB.

        M4/T5: the harness cannot cancel or abort this, and it cannot clear the
        mission flag either.

        The de-duplication keys on a safety task that is STILL FLYING, not on
        the reason alone. `_rtb_active[vehicle] = reason` was never cleared, so
        once a vehicle had been force-RTB'd for a reason, every later force-RTB
        for that same reason returned None — no task, no audit row, nothing.
        That turned a failed safety RTB (a lost-link RTB dies the moment it
        asks for telemetry, because a lost link is exactly what removes it)
        into a vehicle that could never be recovered again, silently.
        """
        prev = self._rtb_task.get(vehicle)
        if self._rtb_active.get(vehicle) == reason:
            if prev is not None and prev.state not in TERMINAL:
                return None  # already committed to this exact transition
            # The previous transition is over — completed, cancelled or FAILED.
            # Re-commit, and say in the journal why a second one was needed.
            self.store.log_audit(
                "force_rtb_resubmit",
                f"previous {reason} RTB ended {prev.state.value if prev else 'unknown'}"
                f"{f' ({prev.error})' if prev is not None and prev.error else ''}; "
                "re-committing",
                vehicle=vehicle, reason=reason,
                previous_task_id=(prev.id if prev is not None else None))
        self._rtb_active[vehicle] = reason
        self._safety_seq += 1
        if mission_status:
            self.mission_flags[vehicle] = mission_status
        fm = self.fuel_for(vehicle)
        self.store.log_audit("force_rtb", detail or reason, vehicle=vehicle,
                             reason=reason, fuel_pct=round(fm.fuel_pct, 3),
                             mission_status=self.mission_flags.get(vehicle),
                             bingo=fm.bingo.to_dict())
        for mid, rec in self.missions.items():
            if rec.get("vehicle") == vehicle and rec.get("state") == "executing":
                rec["state"] = "incomplete"
                rec["status"] = self.mission_flags.get(vehicle) or reason
                self.store.log_mission(mid, "incomplete", vehicle=vehicle,
                                       status=rec["status"], reason=reason)
        task = self.tasking.submit(
            vehicle, "uav_return_to_home",
            {"speed_mps": fm.rtb_speed_mps, "reason": reason, "detail": detail},
            idempotency_key=f"safety-rtb:{vehicle}:{reason}:{self._safety_seq}",
            privileged=True, uncancellable=True)
        self._rtb_task[vehicle] = task
        handle = task.handle()
        self.store.log_task(handle, "submitted", params=task.params,
                            safety_reason=reason)
        self.store.sync()
        return handle

    async def _execute_lost_link(self, vehicle: str, verdict: dict) -> dict | None:
        """Fly the per-mission lost-link plan autonomously (M9)."""
        mon = self.monitor_for(vehicle)
        action = mon.link.action
        if action is None:
            return None
        tele = self._last_tele.get(vehicle)
        plan = mon.link.plan
        self.store.log_audit("lost_link", f"executing {action.value}", vehicle=vehicle,
                             behaviour=action.value, plan=plan.to_dict())
        if action is LostLinkBehaviour.CONTINUE:
            return {"action": action.value, "task": None}
        if action is LostLinkBehaviour.RTB:
            handle = await self._force_rtb(vehicle, reason="lost_link",
                                           detail="lost-link plan: RTB")
            return {"action": action.value, "task": handle}
        if tele is None:
            self.store.log_audit("lost_link", "no last position; falling back to RTB",
                                 vehicle=vehicle, behaviour=action.value)
            handle = await self._force_rtb(vehicle, reason="lost_link",
                                           detail="lost-link with no known position")
            return {"action": "rtb", "task": handle, "downgraded_from": action.value}
        self._safety_seq += 1
        if action is LostLinkBehaviour.CLIMB_FOR_LOS:
            params = {"lat": tele["lat"], "lon": tele["lon"],
                      "alt_m": min(plan.climb_to_m, self.envelope.ceiling_m_agl),
                      "speed_mps": 8.0, "reason": "lost_link_climb"}
            tool = "uav_goto_gps"
        else:  # HOLD_ORBIT
            from .missions import plan_mission
            # COMMANDED altitude -> the launch datum (`commanded_agl`); the
            # measured AGL here parks a blind aircraft at the envelope floor
            # above HOME, which over high ground is inside the hill.
            alt = plan.orbit_alt_m if plan.orbit_alt_m is not None else max(
                self.envelope.min_agl_m + 2.0, self.commanded_agl(tele))
            mission = plan_mission("orbit_poi", vehicle, lat=tele["lat"], lon=tele["lon"],
                                   alt_m=alt, radius_m=plan.orbit_radius_m)
            params = {"waypoints": mission.to_route(), "speed_mps": mission.speed_mps,
                      "reason": "lost_link_hold_orbit"}
            tool = "uav_fly_route"
        task = self.tasking.submit(
            vehicle, tool, params,
            idempotency_key=f"safety-loal:{vehicle}:{action.value}:{self._safety_seq}",
            privileged=True, uncancellable=True)
        self.store.log_task(task.handle(), "submitted", params=params,
                            safety_reason=f"lost_link:{action.value}")
        self.store.sync()
        return {"action": action.value, "task": task.handle()}

    # ---- harness disconnect (PLAN §4.5) ----
    def harness_disconnected(self, vehicle: str | None = None, down: bool = True) -> dict:
        """Declare the control-station link down -> fly the lost-link plan.

        NOT WIRED TO THE TRANSPORT, and this docstring says so rather than
        implying otherwise. Everything downstream of this call works — the flag
        reaches `tick_once`, `LostLinkMonitor.observe` declares LOAL after the
        dwell, and `_execute_lost_link` flies the plan (proved by
        `test_harness_disconnect_triggers_the_lost_link_plan`). What does not
        exist is a PRODUCTION CALLER: only that test calls this, so a real
        control station that drops its connection is never treated as link
        loss, and PLAN §4.5's "Harness disconnect -> lost_link_plan" is
        delivered only from the vehicle-link side (`sim_set_link_state`).

        What was checked, against mcp 2.2.0:

        * `serve()` runs the transport with `stateless_http=True`, and
          `StreamableHTTPSessionManager._handle_stateless_request` builds a
          FRESH transport per HTTP request and terminates it in a `finally`.
          There are no sessions in that mode, so there is no session lifecycle
          to hook: every request "disconnects" on completion, and an idle
          harness is indistinguishable from a departed one.
        * In STATEFUL mode the manager does track sessions, but it exposes no
          callback for their end: `_server_instances`, `_session_owners` and
          `_discard_session` are all private, and a grep of the installed SDK
          for a server-side session/disconnect hook finds only the CLIENT-side
          `on_session_created` in `mcp/client/sse.py`.
        * `ServerMiddleware` (`mcp.server.context`) wraps one request each; it
          never sees a connection end.
        * The SDK's only disconnect watcher, `_streamable_http_modern.
          watch_disconnect`, cancels the in-flight request when the ASGI
          connection drops. It is per-request and internal.

        Wiring it would mean a transport change (stateful sessions) plus a
        dependency on private SDK internals, or a harness-liveness TIMEOUT —
        and a timeout is a doctrine decision (how long silence means the
        station is gone) that would fly un-cancellable RTBs on its own. Neither
        belongs in a defect fix; both are named in the handoff.
        """
        if vehicle is None:
            self._harness_down_all = bool(down)
        elif down:
            self._harness_down.add(vehicle)
        else:
            self._harness_down.discard(vehicle)
        self.store.log_audit("lost_link", f"harness link {'lost' if down else 'restored'}",
                             vehicle=vehicle, scope="harness")
        return {"harness_down": self._harness_down_all or bool(self._harness_down),
                "vehicles": sorted(self._harness_down)}

    # ---- monitor loop ----
    def start_monitor(self, vehicles: list[str] | None = None,
                      interval_s: float = DEFAULT_TICK_S) -> None:
        """Start the telemetry/safety tick loop on the tasking loop thread."""
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._monitor_stop.clear()
        loop = self.tasking.loop
        self._monitor_task = asyncio.run_coroutine_threadsafe(
            self._monitor_loop(vehicles, interval_s), loop)

    def stop_monitor(self) -> None:
        self._monitor_stop.set()
        # The real-data refresher is a daemon, but a test that builds dozens of
        # servers should not leave dozens of them ticking at the upstream.
        self.stop_real_data()

    async def _monitor_loop(self, vehicles: list[str] | None, interval_s: float) -> None:
        try:
            await self.apply_boot_recovery()
        except Exception as exc:  # noqa: BLE001 — recorded, loop still runs
            self._monitor_errors.append(f"boot_recovery: {type(exc).__name__}: {exc}")
            self.store.log_audit("restart_recovery_failed", str(exc))
        names: list[str] = list(vehicles) if vehicles else []
        roster_confirmed = bool(vehicles)
        while not self._monitor_stop.is_set():
            if not roster_confirmed:
                # The roster is the sim's to answer, and a failure used to come
                # back as the literal ["Drone1"] — so the safety loop ticked
                # fuel, geofence and link for a name nobody had confirmed while
                # the real fleet went unmonitored. Retry every pass instead,
                # and say loudly that the roster is unknown.
                try:
                    names = list(await self.backend.list_vehicles())
                    roster_confirmed = True
                    self.vehicle_roster_error = None
                except Exception as exc:  # noqa: BLE001 — surfaced, never faked
                    detail = f"{type(exc).__name__}: {exc}"
                    if self.vehicle_roster_error != detail:
                        self._monitor_errors.append(f"list_vehicles: {detail}")
                        self.store.log_audit("vehicle_roster_unavailable", detail)
                    self.vehicle_roster_error = detail
                    # Monitor what this server has actually commanded. That is
                    # a KNOWN set, not a guessed one, and it is reported as a
                    # degradation rather than passed off as the fleet.
                    names = sorted(set(self.tasking.vehicles())
                                   | set(self._last_tele))
                    if not names:
                        await asyncio.sleep(interval_s)
                        continue
            for v in names:
                try:
                    await self.tick_once(v)
                except Exception as exc:  # noqa: BLE001 — surfaced, never silent
                    self._monitor_errors.append(f"{v}: {type(exc).__name__}: {exc}")
                    self.store.log_audit("tick_failed", str(exc), vehicle=v)
            await asyncio.sleep(interval_s)

    # ---- intel helpers ----
    async def _sensor_conditions(self, vehicle: str, camera: str = "0") -> dict:
        """What the sensor actually saw through (M18/INTREP §4.7)."""
        env = await self.backend.environment()
        fov = await self.backend.camera_fov_deg(vehicle, camera)
        weather = (env or {}).get("weather") or {}
        light = None
        if env is not None:
            light = "day" if env.get("is_day") else "night"
        out = {"sensor": "scene", "camera": camera, "fov_deg": fov,
               "image_px": 640, "light": light,
               "weather": _dominant_weather(weather),
               "visibility_km": None, "sun_elevation_deg": (env or {}).get("sun_elevation_deg"),
               "sun_azimuth_deg": (env or {}).get("sun_azimuth_deg"),
               "wind_mps": None, "gps_quality": ("denied" if (env or {}).get("gps_denied")
                                                 else "nominal" if env else None),
               "source": "sim_environment" if env else "unavailable"}
        wind_ne, wind_source = await self.backend.wind_ne()
        out["wind_mps"] = round(math.hypot(*wind_ne), 2)
        out["wind_source"] = wind_source
        return {k: v for k, v in out.items() if v is not None or k in
                ("light", "weather", "visibility_km", "gps_quality")}

    def _persist_intel(self, tracks: list) -> None:
        for t in tracks:
            self.store.tracks.put(t.to_dict())
            for poi in self.pol.pois_containing(t.lat, t.lon):
                base = self.pol.get(poi)
                if base is not None:
                    self.store.pattern_of_life.put(base.to_dict())

    # ---- idempotency for tools that do NOT go through the queue (T4b) ----
    def _idem_replay(self, tool: str, key: str | None) -> dict | None:
        """The ORIGINAL result for a replayed key, or None for a fresh call.

        TOOL_CONTRACT: "Replaying a key returns the ORIGINAL handle and must
        not re-execute. A key accepted-and-ignored is a defect."
        """
        if not key:
            return None
        with self._idem_lock:
            prior = self._idem_results.get(f"{tool}:{key}")
        if prior is None:
            return None
        out = dict(prior)
        out["status"] = "duplicate"
        out["idempotent_replay"] = True
        return out

    def _idem_record(self, tool: str, key: str | None, result: dict) -> dict:
        if key and not result.get("error"):
            with self._idem_lock:
                self._idem_results[f"{tool}:{key}"] = dict(result)
            result = {**result, "idempotency_key": key}
        return result

    # ---- sun (M6) ----
    async def sun_state(self, vehicle: str) -> dict | None:
        return await self.backend.sun(vehicle)

    # ---- altitude datum resolution (T1) ----
    def _target_altitudes(self, lat: float, lon: float, *,
                          alt_msl_m: float | None = None,
                          alt_agl_m: float | None = None,
                          alt_hae_m: float | None = None,
                          alt_m: float | None = None) -> dict:
        """One geodetic point in all three datums, converted exactly once.

        Exactly ONE spelling may be supplied. `alt_m` is the legacy spelling
        and is documented as MSL; supplying it together with a disagreeing
        explicit value is refused rather than silently resolved.
        """
        if alt_m is not None:
            # Legacy spelling, documented as MSL. It may stand in for
            # alt_msl_m; it may never silently override a datumed value.
            if alt_agl_m is not None or alt_hae_m is not None:
                raise ValueError(
                    "alt_m (legacy, MSL) was supplied alongside an explicit "
                    "alt_agl_m/alt_hae_m; drop alt_m rather than have the "
                    "server choose a datum for you")
            alt_msl_m = _one_altitude("target altitude", alt_msl_m=alt_msl_m,
                                      alt_m=alt_m)
        given = [k for k, v in (("alt_msl_m", alt_msl_m), ("alt_agl_m", alt_agl_m),
                                ("alt_hae_m", alt_hae_m)) if v is not None]
        if len(given) != 1:
            raise ValueError(
                "give exactly one of alt_msl_m / alt_agl_m / alt_hae_m for the "
                f"target altitude (got {given or 'none'}) — an altitude with "
                "no datum is not accepted (TOOL_CONTRACT)")
        # The ground this point's AGL is measured against: MEASURED terrain at
        # (lat, lon) when the real-world layer has it, otherwise the launch
        # datum. Resolved once, used in both directions, and reported — an AGL
        # target over a 300 m ridge used to be silently 300 m too low.
        ground = self.terrain_at(lat, lon)
        ground_hae = (float(ground.hae_m)
                      if ground is not None and ground.real and ground.hae_m is not None
                      else None)
        datum_hae = (ground_hae if ground_hae is not None
                     else self.backend.home_geo.altitude)
        if alt_agl_m is not None:
            fix = canonical_altitude(datum_hae + float(alt_agl_m), lat, lon,
                                     datum="hae")
        elif alt_msl_m is not None:
            fix = canonical_altitude(float(alt_msl_m), lat, lon, datum="msl")
        else:
            fix = canonical_altitude(float(alt_hae_m), lat, lon, datum="hae")
        out = {"alt_hae_m": round(fix.alt_hae, 3),
               "alt_msl_m": round(fix.alt_msl, 3),
               "alt_agl_m": round(fix.alt_hae - datum_hae, 3),
               "alt_agl_launch_datum_m": round(
                   fix.alt_hae - self.backend.home_geo.altitude, 3),
               "alt_agl_is_real": ground_hae is not None,
               "alt_agl_source": (AGL_SOURCE_TERRAIN if ground_hae is not None
                                  else AGL_SOURCE_LAUNCH),
               "alt_agl_note": (AGL_TERRAIN_NOTE if ground_hae is not None
                                else AGL_LAUNCH_NOTE),
               "undulation_m": round(fix.undulation_m, 3),
               "datum_source": fix.source, "datum_given": given[0]}
        if ground is not None:
            out["terrain"] = ground.as_dict()
        return out

    async def _aim_at(self, vehicle: str, point: dict) -> dict:
        """Gimbal angles that put a geodetic point on the boresight.

        Body-relative, so the vehicle's live heading is subtracted — a nose
        camera on an east-bound drone needs a different yaw than the same look
        angle on a north-bound one.
        """
        from .safety import bearing_deg
        target = self._target_altitudes(
            float(point["lat"]), float(point["lon"]),
            alt_msl_m=point.get("alt_msl_m"), alt_agl_m=point.get("alt_agl_m"),
            alt_hae_m=point.get("alt_hae_m"), alt_m=point.get("alt_m"))
        tele = await self._telemetry(vehicle)
        ground = haversine_m(tele["lat"], tele["lon"], float(point["lat"]),
                             float(point["lon"]))
        drop = tele["alt_hae_m"] - target["alt_hae_m"]
        brg = bearing_deg(tele["lat"], tele["lon"], float(point["lat"]),
                          float(point["lon"]))
        heading = float((tele.get("attitude") or {}).get("yaw_deg") or 0.0)
        pitch = -math.degrees(math.atan2(drop, max(0.1, ground)))
        return {"pitch_deg": round(pitch, 2),
                "yaw_deg": round((brg - heading + 180.0) % 360.0 - 180.0, 2),
                "bearing_to_target_deg": round(brg, 2),
                "vehicle_heading_deg": round(heading, 2),
                "ground_range_m": round(ground, 1),
                "slant_range_m": round(math.hypot(ground, drop), 1),
                "height_above_target_m": round(drop, 1),
                "target": {"lat": point["lat"], "lon": point["lon"], **target}}

    # ---- imagery (§4.2 + uav://{vehicle}/camera/{name}/{type}) ----
    async def _capture_frame(self, vehicle: str, camera: str, type_name: str,
                             jpeg_quality: int | None = None) -> dict:
        """Capture, cache for the resource, and return the frame's metadata."""
        key = (type_name or "scene").lower()
        if key not in IMAGE_TYPES:
            raise ValueError(
                f"unknown image type {type_name!r}; known types: "
                f"{sorted(IMAGE_TYPES)}")
        shot = await self.backend.capture(vehicle, camera, IMAGE_TYPES[key])
        png: bytes = shot["png"]
        tele = await self._telemetry(vehicle)
        cam_ned = shot.get("camera_position_ned")
        if cam_ned is not None:
            clat, clon, chae = self.backend.ned_to_llh(*cam_ned)
            fix = canonical_altitude(chae, clat, clon, datum="hae")
            geo_pose = {
                "lat": round(clat, 7), "lon": round(clon, 7),
                "alt_hae_m": round(fix.alt_hae, 3),
                "alt_msl_m": round(fix.alt_msl, 3),
                # Camera AGL goes through the same resolver as everything else,
                # so a frame's metadata cannot say "60 m AGL" on the launch datum
                # while the aircraft is in fact 40 m below a ridge.
                **self.resolve_agl(
                    clat, clon, fix.alt_hae,
                    fix.alt_hae - self.backend.home_geo.altitude),
                "undulation_m": round(fix.undulation_m, 3),
                "datum_source": fix.source,
                "camera_ned": [round(v, 3) for v in cam_ned],
                "source": "simGetImages camera_position",
            }
        else:
            geo_pose = {"source": "unavailable",
                        "note": ("this sim returned no camera_position with the "
                                 "frame; the vehicle position is NOT substituted "
                                 "for the camera's")}
        geo_pose["boresight"] = _euler_deg(shot.get("camera_orientation"))
        sun = await self.backend.sun(vehicle)
        cam_state = await self.backend.camera_state(vehicle, camera)
        resource = f"uav://{vehicle}/camera/{camera}/{key}"
        meta = {
            "resource": resource, "uri": resource,
            "frame_id": f"{vehicle}:{camera}:{key}:{int(time.time() * 1000)}",
            "vehicle": vehicle, "camera": camera, "type": key,
            "image_type": IMAGE_TYPES[key],
            "mime_type": "image/png", "encoding": "png",
            "bytes": len(png), "sha256": hashlib.sha256(png).hexdigest(),
            "width": shot.get("width"), "height": shot.get("height"),
            "jpeg_quality_requested": jpeg_quality,
            "jpeg_quality_applied": False,
            "encoding_note": (
                "AirSim's ImageRequest carries no JPEG quality field and the "
                "frame comes back PNG-compressed, so jpeg_quality was NOT "
                "applied. It is reported rather than silently accepted."),
            "geo_pose": geo_pose,
            "vehicle_pose": {"lat": tele["lat"], "lon": tele["lon"],
                             "alt_hae_m": tele["alt_hae_m"],
                             "alt_msl_m": tele["alt_msl_m"],
                             "alt_agl_m": tele["alt_agl_m"],
                             "attitude": tele["attitude"]},
            "camera_state": cam_state,
            "sun": sun,
            "sun_azimuth_deg": (sun or {}).get("azimuth_deg"),
            "sun_elevation_deg": (sun or {}).get("elevation_deg"),
            "captured_at": time.time(),
            "status": "accepted",
        }
        self.frames[(vehicle, camera, key)] = {"png": png, "meta": meta}
        self.store.log_audit("capture_image", f"{vehicle}/{camera}/{key}",
                             vehicle=vehicle, camera=camera, image_type=key,
                             bytes=len(png), resource=resource,
                             sun_elevation_deg=(sun or {}).get("elevation_deg"))
        return meta

    # ---- M3/M6: the orbit ring and the sun-side rule ----
    def _orbit_ring(self, lat: float, lon: float, alt_agl_m: float, radius_m: float,
                    *, direction: str, laps: int, points: int,
                    sun: dict | None, sun_side: bool) -> tuple[list[dict], dict]:
        """Ring waypoints plus the sun-side decision, stated.

        M6: the sensor must look with the sun BEHIND it, so the aircraft flies
        the arc on the sun's side of the contact — the bearing from the POI to
        the aircraft is the sun's azimuth. `missions.orbit_waypoints` starts
        the ring at that bearing; this records WHY, and what was actually
        chosen when the sun is below the horizon (there is no sun side at
        night, and pretending otherwise would be a fabricated justification).
        """
        from .missions import orbit_waypoints
        az = None if sun is None else sun.get("azimuth_deg")
        el = None if sun is None else sun.get("elevation_deg")
        applied = False
        if not sun_side:
            reason = ("sun_side=False: the caller pinned the arc; the ring "
                      "starts due north of the POI")
        elif az is None:
            reason = ("no sun model is available from this sim, so no sun-side "
                      "arc could be chosen — the ring starts due north of the "
                      "POI and glare is NOT managed")
        elif el is not None and float(el) <= 0.0:
            reason = (f"sun elevation {float(el):.1f} deg is at or below the "
                      "horizon: there is no sun side to favour, so the ring "
                      "starts due north of the POI")
        else:
            applied = True
            reason = (f"sun azimuth {float(az):.1f} deg, elevation "
                      f"{float(el):.1f} deg: the ring starts at bearing "
                      f"{float(az):.1f} deg from the POI, putting the aircraft "
                      "between the sun and the contact so the sun is behind "
                      "the sensor and the target is front-lit (M6)")
        ring = orbit_waypoints(lat, lon, alt_agl_m, radius_m, points=points,
                               sun_azimuth_deg=(float(az) if applied else None))
        direction = (direction or "cw").lower()
        if direction not in ("cw", "ccw"):
            raise ValueError(f"direction must be 'cw' or 'ccw', got {direction!r}")
        if direction == "ccw":
            # The ring is built on INCREASING bearing from the POI, which is
            # clockwise seen from above. Reversing the tail keeps the entry
            # point (and therefore the sun-side arc) and flies it the other way.
            ring = [ring[0]] + list(reversed(ring[1:]))
        laps = max(1, int(laps))
        waypoints = [dict(wp) for _ in range(laps) for wp in ring]
        report = {
            "applied": applied,
            "reason": reason,
            "sun_azimuth_deg": (round(float(az), 2) if az is not None else None),
            "sun_elevation_deg": (round(float(el), 2) if el is not None else None),
            "sun_source": (sun or {}).get("source"),
            "arc_start_bearing_from_poi_deg": ring[0].get("bearing_from_poi_deg"),
            "sensor_look_bearing_deg": (
                round((float(ring[0].get("bearing_from_poi_deg", 0.0)) + 180.0) % 360.0, 1)),
        }
        return waypoints, report

    def _camera_track_pose(self, alt_agl_m: float, radius_m: float,
                           direction: str) -> dict:
        """Gimbal angles that keep an orbiting sensor on the POI (M3).

        On a circular orbit flown nose-tangent, the POI sits exactly 90 deg off
        the nose — to the RIGHT for a clockwise ring, to the LEFT for
        counter-clockwise — at the depression angle the orbit geometry sets.
        These are derived, not guessed.
        """
        depression = math.degrees(math.atan2(float(alt_agl_m), max(1.0, float(radius_m))))
        return {"pitch_deg": round(-depression, 2),
                "yaw_deg": (90.0 if direction == "cw" else -90.0),
                "depression_deg": round(depression, 2),
                "slant_range_m": round(math.hypot(alt_agl_m, radius_m), 1),
                "basis": ("nose-tangent orbit: the POI is 90 deg off the nose "
                          f"({'starboard' if direction == 'cw' else 'port'}) at "
                          f"{depression:.1f} deg depression")}

    # ---- M5: the synchronous LOS closure the mission planners require ----
    def _los_check(self, vehicle: str):
        """A `missions.LosCheck` bound to this sim.

        `missions.track_target_plan` REFUSES to plan without one (the M5
        standoff must be verified, not asserted). It is synchronous, so the
        planner runs in a worker thread and this reaches the sim from there.
        """
        home_hae = self.backend.home_geo.altitude

        def check(lat: float, lon: float, alt_agl_m: float,
                  tlat: float, tlon: float, talt_m: float) -> dict:
            # The ring altitude is AGL: over real terrain it is measured from
            # the ground under the ring point, not from the launch datum.
            ground = self.terrain_at(float(lat), float(lon))
            datum = (float(ground.hae_m)
                     if ground is not None and ground.real and ground.hae_m is not None
                     else home_hae)
            observer = (float(lat), float(lon), datum + float(alt_agl_m))
            target = (float(tlat), float(tlon), float(talt_m))
            out = self.backend.los_between_sync(observer, target)
            terrain = self.terrain_los(observer, target)
            measured = terrain is not None and terrain.known
            out["los_is_measured"] = measured
            if terrain is None:
                return out
            out["terrain"] = terrain.as_dict()
            out["terrain_known"] = bool(terrain.known)
            if not terrain_los_votes(terrain):
                # An UNKNOWN cut has not answered, so it does not get a vote.
                # `TerrainProvider.line_of_sight` returns `los=False` when no
                # height is resident along the profile — an honest refusal AT
                # THAT LAYER — and this closure is memory-only by design, while
                # the profile is deliberately NOT snapped to the terrain lattice
                # (see `terrain_los`). So on a COLD profile every sight line
                # came back False: measured over the wire, a flat, clear 800 m
                # line over the Fordow valley floor answered los=False with
                # sim_los=True, terrain.known=False and NO obstacle, and the
                # same line answered True three seconds later once the
                # background prefetch landed. That refuses an M5-verified
                # mission over clear ground, non-deterministically, and it is
                # the opposite of the layer-OFF behaviour, which returns the
                # sim's answer flagged `los_is_measured: false`. The prefetch
                # `terrain_los` already scheduled makes the next call measured.
                out["los_terrain_note"] = (
                    "terrain was NOT consulted on this check: no bare-earth "
                    "height is resident along this profile and the check is "
                    "memory-only. The verdict is the sim's geometric horizon "
                    "alone (los_is_measured=false); the profile has been "
                    "prefetched, so re-asking gives a measured cut.")
                return out
            out["sim_los"] = out["los"]
            # AND, for the same reason the tool composes this way: a ridge the
            # sim's horizon model cannot see still blocks the M5 standoff.
            out["los"] = bool(out["los"]) and bool(terrain.los)
            if not terrain.los and terrain.first_obstacle is not None:
                out["first_obstacle"] = terrain.first_obstacle
                out["first_obstacle_modelled"] = True
                out["model"] = f"{out['model']}  ||  TERRAIN: {terrain.model}"
            return out

        return check

    @staticmethod
    def _stamp_phase_fov(plan) -> None:
        """Carry each phase's `fov_deg` onto its own waypoints.

        This is what makes the M7 cross-cue actually FLY: `_fly_legs` commands
        `uav_set_fov` when the field changes between phases, so the narrow-FOV
        identification pass is flown at the field its pixel-density maths
        assumed rather than at whatever the camera happened to be left on.
        """
        for phase in plan.phases or []:
            span = phase.get("waypoint_span")
            fov = phase.get("fov_deg")
            if not span or fov is None:
                continue
            for wp in plan.waypoints[int(span[0]):int(span[1])]:
                wp["fov_deg"] = float(fov)
                wp["phase"] = phase.get("name")
                if phase.get("camera"):
                    wp["camera"] = phase["camera"]
        if plan.phases:
            return
        # A single-phase plan (grid, recon, orbit, track) costs its swath,
        # lane spacing and capture interval at its camera's WIDE field. If the
        # sensor is left on whatever field the last command set, the coverage
        # the plan reports is not the coverage that was imaged — the M1 defect
        # in a different disguise. So the plan's own field is commanded too.
        cam = plan.meta.get("camera")
        hfov = cam.get("hfov_deg") if isinstance(cam, dict) else None
        if hfov is None:
            return
        for wp in plan.waypoints:
            wp.setdefault("fov_deg", float(hfov))
            wp.setdefault("camera", str(cam.get("camera", "0")))
            wp.setdefault("phase", plan.kind)

    # ---- M4: the plan product every mission passes through ----
    async def _plan_product(self, vehicle: str, plan) -> dict:
        """`missions.dry_run` priced from the vehicle's LIVE position and wind.

        One costing path for every mission tool AND for `mission_dry_run`, so
        the preview and the flight can never disagree.
        """
        from .missions import dry_run
        fm = self.fuel_for(vehicle)
        self._stamp_phase_fov(plan)
        try:
            tele = await self._telemetry(vehicle)
        except Exception as exc:  # no silent fallback: an ungateable plan is rejected
            gate = {"ok": False, "required_pct": None, "est_time_s": None,
                    "error": f"telemetry unavailable: {type(exc).__name__}: {exc}",
                    "envelope_violations": ["telemetry_unavailable"]}
            self.store.log_audit("preflight_reject", "gate could not read telemetry",
                                 vehicle=vehicle, gate=gate)
            return {**plan.to_dict(), "mission_kind": plan.kind, "executed": False,
                    "gate": gate}
        wind_ne, wind_source = await self.backend.wind_ne()
        # PRICED altitude -> the launch datum, the datum the plan's own
        # waypoint altitudes are in (`commanded_agl`).
        start_agl = self.commanded_agl(tele)
        product = dry_run(plan, fm, self.envelope, start=(tele["lat"], tele["lon"]),
                          start_alt_agl_m=start_agl, wind_ne=wind_ne)
        gate = product["gate"]
        gate["start"] = [round(tele["lat"], 6), round(tele["lon"], 6)]
        gate["start_alt_agl_m"] = start_agl
        gate["start_alt_agl_datum"] = AGL_SOURCE_LAUNCH
        gate["start_alt_agl_measured_m"] = tele["alt_agl_m"]
        gate["wind_ne_mps"] = [round(wind_ne[0], 2), round(wind_ne[1], 2)]
        gate["wind_source"] = wind_source
        gate["bingo_fuel_pct"] = product["bingo_fuel_pct"]
        product["wind_source"] = wind_source
        if not gate["ok"]:
            self.store.log_audit("preflight_reject", "pre-flight gate rejected plan",
                                 vehicle=vehicle, gate=gate,
                                 mission_kind=plan.kind)
        return product

    async def _launch_mission(self, vehicle: str, plan, *,
                              idempotency_key: str | None = None,
                              speed_mps: float | None = None,
                              dry_run: bool = False,
                              lost_link_plan: dict | None = None,
                              extra: dict | None = None,
                              repath: dict | None = None) -> dict:
        """Gate -> queue -> register -> journal. Shared by every mission tool.

        `repath` arms the M5 server re-path loop for this mission (see
        `_repath_route`); only `track_target` passes one.
        """
        if speed_mps is not None:
            plan.speed_mps = float(speed_mps)
        if lost_link_plan is not None:
            try:
                self.monitor_for(vehicle).link.plan = LostLinkPlan.from_dict(lost_link_plan)
            except ValueError as exc:
                return error("invalid_lost_link_plan", str(exc))
        product = await self._plan_product(vehicle, plan)
        gate = product["gate"]
        if dry_run:
            # mission_dry_run: the plan product only. Nothing is queued, the
            # vehicle is not commanded, and the gate verdict rides along so the
            # harness can decide (PLAN §5: task -> plan -> dry-run -> execute).
            return {"executed": False, "dry_run": True, "vehicle": vehicle,
                    "kind": plan.kind, **product, **(extra or {})}
        if not gate["ok"]:
            # A safety rejection is NOT an error (TOOL_CONTRACT): the gate
            # result comes back so the harness can re-plan.
            return {"rejected": True, "kind": plan.kind, "gate": gate,
                    "plan": product, **(extra or {})}
        mission_id = f"MSN-{uuid.uuid4().hex[:8]}"
        if repath is not None:
            # M5: armed BEFORE the task is queued, not after `_submit` returns.
            # The queue worker can be inside `_fly_legs` while this function is
            # still building the handle, and `_repath_route` resolves the loop
            # once, at that moment: a loop armed a few milliseconds later would
            # silently never run for the whole flight.
            self._repath[mission_id] = {**repath, "checked_at": None,
                                        "repaths": []}
        handle = self._submit(vehicle, "uav_fly_route",
                              {"waypoints": plan.to_route(),
                               "speed_mps": plan.speed_mps,
                               "captures": plan.captures},
                              idempotency_key, mission_id=mission_id)
        if handle.get("status") in ("busy", "duplicate"):
            # Nothing was queued under THIS mission id (busy flew nothing; a
            # duplicate belongs to the original mission), so the loop armed
            # above has no flight to follow.
            self._repath.pop(mission_id, None)
        if handle.get("status") == "busy":
            return handle
        if handle.get("status") == "duplicate":
            # T4b, TOOL_CONTRACT: "Replaying a key returns the ORIGINAL handle."
            # The queue already de-duplicated the TASK, but the mission id was
            # minted fresh above, so the replay used to hand back a brand-new
            # MSN- the mission was never filed under: mission_status on it said
            # "unknown mission", and the INTREP for it did not exist. The
            # original id travels on the original task, which is what came back.
            original_id = handle.get("mission_id")
            if not original_id:
                return {**error("idempotency_key_reused_across_tools",
                                f"idempotency_key {idempotency_key!r} already "
                                f"belongs to task {handle.get('task_id')} "
                                f"({handle.get('tool')}), which is not a "
                                "mission; use a fresh key", retryable=False),
                        "current": handle}
            prior = self._mission_handles.get(original_id)
            if prior is None:
                # Known to the queue but not to this process's registry — a
                # key re-seeded from the journal after a restart. Say so;
                # do NOT rebuild a handle from the new plan and pass it off
                # as the original.
                return {**handle, "mission_handle": original_id,
                        "mission_id": original_id,
                        "original_handle_available": False,
                        "reason": ("this key was replayed against a mission "
                                   "filed before this process started; the "
                                   "original mission_handle is returned but "
                                   "its plan product is not in memory. Read "
                                   f"uav://mission/{original_id}.")}
            return {**prior, "status": "duplicate",
                    "idempotent_replay": handle.get("idempotent_replay"),
                    "original_handle_available": True}
        handle.update({
            "mission_handle": mission_id, "mission_id": mission_id,
            "kind": plan.kind, "doctrine": plan.meta.get("doctrine"),
            "waypoint_count": len(plan.waypoints),
            "waypoints": plan.to_route(),
            "speed_mps": plan.speed_mps,
            "alt_agl_m": plan.alt_agl_m,
            "bingo_fuel_pct": gate["bingo_fuel_pct"],
            "plan_required_pct": gate["required_pct"],
            "est_time_s": gate["est_time_s"],
            "est_fuel_pct": product.get("est_fuel_pct"),
            "gate": gate,
            "coverage": product.get("coverage"),
            "phases": plan.phases,
            "warnings": list(product.get("warnings") or []),
            "truncated": plan.truncated,
            "truncation_reason": plan.truncation_reason,
        })
        for key in ("sweep_spacing_m", "lane_spacing_m", "lane_spacing_derived_m",
                    "lanes", "lanes_required", "swath_m", "footprint_m",
                    "capture_interval_m", "standoff_m", "standoff", "los",
                    "camera", "overlap_pct", "forward_overlap_pct", "pattern",
                    "track_id", "radius_m", "poi",
                    # M2: mission_recon_route's description promises the
                    # trigger distance, the widened interval when the rate
                    # clamp binds, and the overlap actually achieved. None of
                    # them reached the wire, so the caller could not tell what
                    # the schedule it was handed a count of would actually do.
                    "capture_every_m", "capture_interval_requested_m",
                    "capture_interval_s_at_speed", "max_capture_rate_hz",
                    "capture_rate_clamped", "capture_trigger",
                    "forward_overlap_achieved_pct", "along_track_swath_m"):
            if key in plan.meta:
                handle[key] = plan.meta[key]
        if plan.captures:
            handle["capture_count"] = len(plan.captures)
            # Seeded HERE, not when the route starts: the aircraft climbs
            # before the first leg, and a harness polling mission_status in
            # that window must still be told how many frames are scheduled.
            self._capture_progress[handle["task_id"]] = {
                "captures_planned": len(plan.captures), "captures_taken": 0,
                "captures_missed": 0}
        handle.update(extra or {})
        self.missions[mission_id] = {
            "mission_id": mission_id, "vehicle": vehicle, "kind": plan.kind,
            "task_id": handle["task_id"], "state": "executing",
            "started": time.time(), "waypoints": plan.to_route(),
            "speed_mps": plan.speed_mps, "meta": dict(plan.meta), "gate": gate,
            "coverage": product.get("coverage"),
            "phases": plan.phases,
            "warnings": list(product.get("warnings") or []),
            "truncated": plan.truncated,
            "truncation_reason": plan.truncation_reason,
            "lost_link_plan": self.monitor_for(vehicle).link.plan.to_dict(),
        }
        self.store.log_mission(mission_id, "submitted", vehicle=vehicle,
                               kind=plan.kind, task_id=handle["task_id"],
                               waypoints=len(plan.waypoints),
                               est_time_s=gate["est_time_s"],
                               bingo_fuel_pct=gate["bingo_fuel_pct"])
        # The handle exactly as the caller first received it, so a replayed
        # idempotency_key can be answered with the ORIGINAL rather than a
        # re-derived look-alike (T4b).
        self._mission_handles[mission_id] = dict(handle)
        return handle

    # ---- §4.3 discrete mission primitives (uav_mission forwards to these) ----
    async def _dispatch_mission(self, kind: str, vehicle: str, params: dict, *,
                                speed_mps: float | None = None,
                                idempotency_key: str | None = None,
                                dry_run: bool = False) -> dict:
        """The one dispatch table. `uav_mission` and `mission_dry_run` share it."""
        kw = dict(params or {})
        link_plan = kw.pop("lost_link_plan", None)
        kind = (kind or "").lower()
        # Legacy spellings the GEV panel still sends. `alt_m` is accepted but
        # ALWAYS means metres AGL above the launch datum, which is what the
        # planners take; the canonical spelling is alt_agl_m (TOOL_CONTRACT).
        alt = kw.pop("alt_agl_m", None)
        if alt is None:
            alt = kw.pop("alt_m", None)
        else:
            kw.pop("alt_m", None)
        camera = kw.pop("camera", "0")
        speed = speed_mps if speed_mps is not None else kw.pop("speed_mps", None)
        kw.pop("speed_mps", None)
        common = {"lost_link_plan": link_plan, "dry_run": dry_run,
                  "idempotency_key": idempotency_key, "camera": camera}
        try:
            if kind == "grid_search":
                return await self._mission_grid_search(
                    vehicle, polygon=kw.get("polygon") or [],
                    alt_agl_m=(60.0 if alt is None else float(alt)),
                    overlap_pct=float(kw.get("overlap_pct",
                                             kw.get("overlap", 20.0))),
                    pattern=kw.get("pattern", "lawnmower"),
                    speed_mps=(8.0 if speed is None else float(speed)),
                    max_lanes=kw.get("max_lanes"), legs=int(kw.get("legs", 12)),
                    **common)
            if kind == "recon_route":
                return await self._mission_recon_route(
                    vehicle, waypoints=kw.get("waypoints") or [],
                    alt_agl_m=(60.0 if alt is None else float(alt)),
                    forward_overlap_pct=float(
                        kw.get("forward_overlap_pct", kw.get("overlap", 20.0))),
                    speed_mps=(10.0 if speed is None else float(speed)),
                    max_capture_rate_hz=float(kw.get("max_capture_rate_hz", 1.0)),
                    **common)
            if kind == "track_target":
                return await self._mission_track_target(
                    vehicle, track_id=kw.get("track_id"), track=kw.get("track"),
                    alt_agl_m=(120.0 if alt is None else float(alt)),
                    speed_mps=(12.0 if speed is None else float(speed)),
                    points=int(kw.get("points", 12)),
                    allow_unverified=bool(kw.get("allow_unverified", False)),
                    **common)
            if kind in ("identify", "identify_target"):
                # `max_id_alt_agl_m` is the remedy the M7 LOS refusal names.
                # `kw.get(...)` with NO default is deliberate: absent means
                # "planner default" (the detect altitude), and a value the
                # caller did send is never quietly replaced. A non-numeric one
                # raises here and comes back as `invalid_mission_params`.
                id_ceiling = kw.get("max_id_alt_agl_m")
                return await self._mission_identify_target(
                    vehicle, track_id=kw.get("track_id"), track=kw.get("track"),
                    alt_agl_m=(120.0 if alt is None else float(alt)),
                    orbit_first=bool(kw.get("orbit_first", True)),
                    speed_mps=(10.0 if speed is None else float(speed)),
                    max_id_alt_agl_m=(None if id_ceiling is None
                                      else float(id_ceiling)),
                    allow_unverified=bool(kw.get("allow_unverified", False)),
                    **common)
            if kind == "threat_assessment":
                return await self._mission_threat_assessment(
                    vehicle, area_polygon=kw.get("area_polygon"),
                    defended=kw.get("defended"),
                    survey=bool(kw.get("survey", False)),
                    alt_agl_m=(60.0 if alt is None else float(alt)),
                    overlap_pct=float(kw.get("overlap_pct", 20.0)),
                    camera=camera,
                    speed_mps=(8.0 if speed is None else float(speed)),
                    dry_run=dry_run, idempotency_key=idempotency_key)
            if kind == "orbit_poi":
                return await self._mission_orbit_poi(
                    vehicle, lat=float(kw["lat"]), lon=float(kw["lon"]),
                    radius_m=float(kw.get("radius_m", 80.0)),
                    alt_agl_m=(60.0 if alt is None else float(alt)),
                    direction=kw.get("direction", "cw"),
                    laps=int(kw.get("laps", 1)), points=int(kw.get("points", 12)),
                    camera_track=bool(kw.get("camera_track", True)),
                    sun_side=bool(kw.get("sun_side", True)),
                    speed_mps=(10.0 if speed is None else float(speed)),
                    **common)
            if kind == "assess":
                # Legacy point-assess kept for the GEV panel; the doctrine path
                # for a known contact is mission_identify_target (M7).
                from .missions import plan_mission
                plan = plan_mission("assess", vehicle, lat=kw["lat"], lon=kw["lon"],
                                    radius_m=kw.get("radius_m", 50.0),
                                    alt_agl_m=(45.0 if alt is None else float(alt)),
                                    camera=camera,
                                    speed_mps=(8.0 if speed is None else float(speed)))
                return await self._launch_mission(
                    vehicle, plan, idempotency_key=idempotency_key,
                    dry_run=dry_run, lost_link_plan=link_plan)
        except KeyError as exc:
            return error("missing_parameter",
                         f"mission kind {kind!r} needs {exc}")
        except (ValueError, TypeError) as exc:
            return error("invalid_mission_params", str(exc))
        return error("unknown_mission_kind",
                     f"unknown mission kind {kind!r}; known kinds: grid_search, "
                     "recon_route, track_target, identify, threat_assessment, "
                     "orbit_poi, assess")

    def _plan_or_error(self, fn, *args, **kw):
        """Run a planner, turning its refusals into structured errors.

        Every one of these refusals is deliberate in `missions.py` (a plan too
        large, an overlap given as a percent, a masked ring). They are surfaced
        with their own code — never swallowed into a default plan.
        """
        from .missions import LosBlockedError, PlanTooLargeError
        from .missions import LosUnavailableError as PlanLosUnavailable
        try:
            return fn(*args, **kw), None
        except PlanTooLargeError as exc:
            return None, error("plan_too_large", str(exc))
        except LosBlockedError as exc:
            return None, error("los_blocked", str(exc), retryable=True)
        except (PlanLosUnavailable, LosModelUnavailableError) as exc:
            return None, error("los_unavailable", str(exc), retryable=True)
        except (ValueError, KeyError, TypeError) as exc:
            return None, error("invalid_mission_params", str(exc))

    async def _mission_grid_search(self, vehicle: str, *, polygon: list,
                                   alt_agl_m: float, overlap_pct: float,
                                   pattern: str = "lawnmower", camera: str = "0",
                                   speed_mps: float = 8.0,
                                   max_lanes: int | None = None, legs: int = 12,
                                   lost_link_plan: dict | None = None,
                                   dry_run: bool = False,
                                   idempotency_key: str | None = None) -> dict:
        from .missions import grid_search_plan
        try:
            fraction = overlap_fraction(overlap_pct, "overlap_pct")
        except ValueError as exc:
            return error("invalid_mission_params", str(exc))
        plan, err = self._plan_or_error(
            grid_search_plan, vehicle, polygon, float(alt_agl_m),
            overlap_pct=fraction, camera_name=camera, pattern=pattern,
            speed_mps=float(speed_mps), max_lanes=max_lanes, legs=int(legs))
        if err:
            return err
        return await self._launch_mission(
            vehicle, plan, idempotency_key=idempotency_key, dry_run=dry_run,
            lost_link_plan=lost_link_plan,
            extra={"overlap_pct": float(overlap_pct),
                   "overlap_fraction": fraction})

    async def _mission_recon_route(self, vehicle: str, *, waypoints: list,
                                   alt_agl_m: float, forward_overlap_pct: float,
                                   camera: str = "0", speed_mps: float = 10.0,
                                   max_capture_rate_hz: float = 1.0,
                                   lost_link_plan: dict | None = None,
                                   dry_run: bool = False,
                                   idempotency_key: str | None = None) -> dict:
        from .missions import recon_route_plan
        try:
            fraction = overlap_fraction(forward_overlap_pct, "forward_overlap_pct")
        except ValueError as exc:
            return error("invalid_mission_params", str(exc))
        plan, err = self._plan_or_error(
            recon_route_plan, vehicle, waypoints, float(alt_agl_m),
            forward_overlap_pct=fraction, camera_name=camera,
            speed_mps=float(speed_mps),
            max_capture_rate_hz=float(max_capture_rate_hz))
        if err:
            return err
        return await self._launch_mission(
            vehicle, plan, idempotency_key=idempotency_key, dry_run=dry_run,
            lost_link_plan=lost_link_plan,
            extra={"forward_overlap_pct": float(forward_overlap_pct),
                   "forward_overlap_fraction": fraction})

    def _resolve_track(self, track_id: str | None, track=None):
        if track is not None:
            return track, None
        if not track_id:
            return None, error(
                "missing_parameter",
                "track_id is required: the standoff is derived from the "
                "contact's order-of-battle class, not from a bare lat/lon (M5)")
        found = self.tracks.get(track_id)
        if found is None:
            return None, error("unknown_track", f"no track {track_id!r} in the "
                                                "store; scan first (M11)")
        return found, None

    async def _mission_track_target(self, vehicle: str, *,
                                    track_id: str | None = None, track=None,
                                    alt_agl_m: float = 120.0, camera: str = "0",
                                    speed_mps: float = 12.0, points: int = 12,
                                    allow_unverified: bool = False,
                                    lost_link_plan: dict | None = None,
                                    dry_run: bool = False,
                                    idempotency_key: str | None = None) -> dict:
        from .missions import track_target_plan
        trk, err = self._resolve_track(track_id, track)
        if err:
            return err
        sun = await self.backend.sun(vehicle)
        # The planner's los_check is synchronous (M5), so it runs in a worker
        # thread with its own RPC client.
        plan, err = await asyncio.to_thread(
            self._plan_or_error, track_target_plan, vehicle, trk,
            alt_agl_m=float(alt_agl_m), camera_name=camera,
            speed_mps=float(speed_mps), points=int(points),
            los_check=(None if allow_unverified else self._los_check(vehicle)),
            allow_unverified=bool(allow_unverified),
            sun_azimuth_deg=(sun or {}).get("azimuth_deg"))
        if err:
            return err
        # M5: arm the server re-path loop for this mission. `missions.py`
        # carried the note "call repath_track() when repath_needed() is true"
        # addressed to a caller that did not exist, so over MCP a moving
        # contact was orbited at its FIRST fix for the whole mission — i.e.
        # mission_track_target did not track. The executor consults this while
        # it flies (`_repath_route`); the LOS policy travels with it so a
        # re-path is verified exactly the way the plan it replaces was.
        return await self._launch_mission(
            vehicle, plan, idempotency_key=idempotency_key, dry_run=dry_run,
            lost_link_plan=lost_link_plan,
            repath={"plan": plan, "vehicle": vehicle,
                    "track_id": trk.track_id,
                    "allow_unverified": bool(allow_unverified),
                    "interval_s": float(plan.meta["repath"]["interval_s"])},
            extra={"track_id": trk.track_id,
                   "sun": sun,
                   "standoff_basis": plan.meta["standoff"]["basis"]})

    async def _mission_identify_target(self, vehicle: str, *,
                                       track_id: str | None = None, track=None,
                                       alt_agl_m: float = 120.0,
                                       orbit_first: bool = True,
                                       camera: str = "0", speed_mps: float = 10.0,
                                       max_id_alt_agl_m: float | None = None,
                                       allow_unverified: bool = False,
                                       lost_link_plan: dict | None = None,
                                       dry_run: bool = False,
                                       idempotency_key: str | None = None) -> dict:
        from .missions import identify_plan
        trk, err = self._resolve_track(track_id, track)
        if err:
            return err
        sun = await self.backend.sun(vehicle)
        # M7: `max_id_alt_agl_m` is the ceiling the ID-altitude search is
        # allowed to climb to, and the LOS refusal names it as the operator's
        # FIRST remedy ("raise max_id_alt_agl_m above N m AGL") — the ring
        # itself is not negotiable under ISR doctrine (M14). It was accepted by
        # `identify_plan` but reachable from no tool, so over MCP — the only
        # interface a harness has — the refusal named a lever that did not
        # exist. It is forwarded, not defaulted: None means "let the planner
        # use the detect altitude", which is the planner's own documented
        # default, and any other value is passed through verbatim.
        plan, err = await asyncio.to_thread(
            self._plan_or_error, identify_plan, vehicle, trk,
            alt_agl_m=float(alt_agl_m), camera_name=camera,
            speed_mps=float(speed_mps), orbit_first=bool(orbit_first),
            max_id_alt_agl_m=(None if max_id_alt_agl_m is None
                              else float(max_id_alt_agl_m)),
            los_check=(None if allow_unverified else self._los_check(vehicle)),
            allow_unverified=bool(allow_unverified),
            sun_azimuth_deg=(sun or {}).get("azimuth_deg"))
        if err:
            return err
        return await self._launch_mission(
            vehicle, plan, idempotency_key=idempotency_key, dry_run=dry_run,
            lost_link_plan=lost_link_plan,
            extra={"track_id": trk.track_id, "sun": sun,
                   **self._identify_product(trk, vehicle)})

    def _identify_product(self, track, vehicle: str) -> dict:
        """§4.3's identify return: classification, confidence, geo_point,
        track_history, key_images."""
        from .targets import assess_confidence
        ob = track.ob
        images = [meta["resource"] for (veh, _cam, _typ), meta
                  in self.frames.items() if veh == vehicle]
        return {
            "classification": {
                "ob_class": ob.key, "name": ob.name, "category": ob.category,
                "role": ob.role, "detected_as": track.name,
                "match_evidence": track.match_evidence,
            },
            "confidence": assess_confidence(track),
            "geo_point": {"lat": round(track.lat, 6), "lon": round(track.lon, 6),
                          "alt_m": round(float(track.alt_m or 0.0), 1),
                          "datum": "the sim's geodetic altitude for the contact"},
            "track_history": [{"ts": ts, "lat": round(la, 6), "lon": round(lo, 6)}
                              for ts, la, lo in list(track.history)[-50:]],
            "key_images": images,
            "key_images_note": (
                None if images else
                "no imagery has been captured on this vehicle yet — call "
                "uav_capture_image, or read uav://{vehicle}/camera/{name}/{type}"),
        }

    async def _mission_orbit_poi(self, vehicle: str, *, lat: float, lon: float,
                                 radius_m: float, alt_agl_m: float,
                                 direction: str = "cw", laps: int = 1,
                                 points: int = 12, camera_track: bool = True,
                                 sun_side: bool = True, camera: str = "0",
                                 speed_mps: float = 10.0,
                                 lost_link_plan: dict | None = None,
                                 dry_run: bool = False,
                                 idempotency_key: str | None = None) -> dict:
        """§4.1 uav_orbit_poi: a standoff ring with the M3/M6 sun-side rule."""
        from .missions import MissionPlan, camera as resolve_camera
        try:
            cam = resolve_camera(camera)
            sun = await self.backend.sun(vehicle)
            ring, sun_report = self._orbit_ring(
                float(lat), float(lon), float(alt_agl_m), float(radius_m),
                direction=direction, laps=laps, points=int(points), sun=sun,
                sun_side=bool(sun_side))
        except (ValueError, KeyError) as exc:
            return error("invalid_mission_params", str(exc))
        gimbal = self._camera_track_pose(float(alt_agl_m), float(radius_m),
                                         (direction or "cw").lower())
        plan = MissionPlan("orbit_poi", vehicle, ring, float(alt_agl_m),
                           float(speed_mps),
                           {"doctrine": "orbit_poi", "radius_m": float(radius_m),
                            "poi": [float(lat), float(lon)],
                            "direction": (direction or "cw").lower(),
                            "laps": max(1, int(laps)), "points": int(points),
                            "camera": cam.to_dict(), "sun_side": sun_report,
                            "camera_track": gimbal})
        out = await self._launch_mission(
            vehicle, plan, idempotency_key=idempotency_key, dry_run=dry_run,
            lost_link_plan=lost_link_plan,
            extra={"direction": (direction or "cw").lower(),
                   "laps": max(1, int(laps)), "sun_side": sun_report,
                   "sun": sun})
        if camera_track and not dry_run and not out.get("rejected") \
                and not out.get("error") and out.get("status") != "busy":
            try:
                state = await self.backend.set_camera_pose(
                    vehicle, camera, gimbal["pitch_deg"], gimbal["yaw_deg"])
                out["camera_track"] = {"applied": True, **gimbal, "camera_state": state}
            except Exception as exc:  # noqa: BLE001 — reported, never claimed
                out["camera_track"] = {
                    "applied": False, **gimbal,
                    "error": f"gimbal slew failed: {type(exc).__name__}: {exc}"}
        else:
            out["camera_track"] = {"applied": False, **gimbal,
                                   "reason": ("camera_track=False" if not camera_track
                                              else "plan not executed")}
        return out

    async def _mission_threat_assessment(self, vehicle: str, *,
                                         area_polygon: list | None = None,
                                         defended: list | None = None,
                                         survey: bool = False,
                                         alt_agl_m: float = 60.0,
                                         overlap_pct: float = 20.0,
                                         camera: str = "0",
                                         speed_mps: float = 8.0,
                                         dry_run: bool = False,
                                         detail: str = "summary",
                                         top_n: int | None = THREAT_SUMMARY_TOP_N,
                                         idempotency_key: str | None = None) -> dict:
        from .threat import assess_area
        try:
            tele = await self._telemetry(vehicle)
        except Exception as exc:  # noqa: BLE001 — surfaced, never defaulted
            return error("telemetry_unavailable",
                         f"{type(exc).__name__}: {exc}", retryable=True)
        observer = self._observer(tele)
        try:
            polygon = latlon_polygon(
                area_polygon if area_polygon is not None else self.envelope.geofence,
                "area_polygon")
        except ValueError as exc:
            return error("bad_polygon", str(exc))
        try:
            report = assess_area(self.tracks.tracks(), observer, defended, self.pol,
                                 area_polygon=polygon, detail=detail, top_n=top_n)
        except ValueError as exc:
            return error("invalid_parameter", str(exc))
        mission_id = f"MSN-{uuid.uuid4().hex[:8]}"
        # `assessment` used to be a SECOND COPY of the same report. JSON cannot
        # alias, so every THREATREP was serialised twice - half of the 1.1 MB
        # measured on the wire at 36 tracks. It is now a pointer, not a copy.
        out: dict = {"report": report,
                     "assessment": "see `report` — this key was a duplicate copy "
                                   "of it and is kept only as a pointer",
                     "area_polygon": [list(p) for p in (polygon or [])],
                     "isr_only": "M14: sensor-posture advice only; no "
                                 "engagement recommendation is produced"}
        survey_handle = None
        if survey:
            survey_handle = await self._mission_grid_search(
                vehicle, polygon=polygon or [], alt_agl_m=alt_agl_m,
                overlap_pct=overlap_pct, camera=camera, speed_mps=speed_mps,
                dry_run=dry_run, idempotency_key=idempotency_key)
            out["survey"] = survey_handle
            if survey_handle.get("mission_handle"):
                mission_id = survey_handle["mission_handle"]
        else:
            out["survey"] = None
            out["survey_note"] = (
                "survey=False: nothing was flown. The assessment is computed "
                "from the tracks already in the store — pass survey=True to "
                "fly a grid over area_polygon first.")
        if survey_handle is None or not survey_handle.get("mission_handle"):
            # Register it anyway so mission_status / uav://mission/{id} resolve.
            self.missions[mission_id] = {
                "mission_id": mission_id, "vehicle": vehicle,
                "kind": "threat_assessment", "task_id": None,
                "state": "complete", "started": time.time(),
                "waypoints": [], "speed_mps": speed_mps,
                "meta": {"doctrine": "threat_assessment",
                         "area_polygon": [list(p) for p in (polygon or [])]},
                "gate": None, "report_id": mission_id,
            }
            self.store.log_mission(mission_id, "completed", vehicle=vehicle,
                                   kind="threat_assessment",
                                   count=report.get("count"),
                                   highest_threat=report.get("highest_threat"))
        report["report_id"] = mission_id
        report["resource"] = f"uav://reports/{mission_id}"
        self.reports[mission_id] = report
        out["mission_handle"] = mission_id
        out["mission_id"] = mission_id
        out["report_id"] = mission_id
        out["resource"] = f"uav://reports/{mission_id}"
        self.store.log_audit("threat_assess_area",
                             f"highest {report.get('highest_threat')}",
                             vehicle=vehicle, count=report.get("count"),
                             mission_id=mission_id)
        return out

    async def _mission_handoff_track(self, *, track_id: str, from_vehicle: str,
                                     to_vehicle: str, alt_agl_m: float = 60.0,
                                     dry_run: bool = False,
                                     idempotency_key: str | None = None) -> dict:
        from .threat import plan_handoff
        trk, err = self._resolve_track(track_id)
        if err:
            return err
        try:
            tele = await self._telemetry(to_vehicle)
        except Exception as exc:  # noqa: BLE001 — a receiver we cannot see is a refusal
            return error("telemetry_unavailable",
                         f"receiver {to_vehicle}: {type(exc).__name__}: {exc}",
                         retryable=True)
        fm = self.fuel_for(to_vehicle)
        ho = plan_handoff(trk, from_vehicle, to_vehicle, fm.fuel_pct,
                          {"lat": tele["lat"], "lon": tele["lon"]})
        self.store.log_audit("handoff", f"{track_id} {from_vehicle}->{to_vehicle} "
                             f"{'accepted' if ho.accepted else 'rejected'}",
                             track_id=track_id, accepted=ho.accepted,
                             reason=ho.reason)
        if not ho.accepted:
            return {"accepted": False, "reason": ho.reason, "track_id": track_id,
                    "from_vehicle": from_vehicle, "to_vehicle": to_vehicle}
        out = await self._mission_track_target(
            to_vehicle, track_id=track_id, alt_agl_m=alt_agl_m,
            allow_unverified=True, dry_run=dry_run,
            idempotency_key=idempotency_key)
        if out.get("error") or out.get("rejected") or out.get("status") == "busy":
            return {"accepted": False, "reason": "receiver could not be tasked",
                    "track_id": track_id, "to_vehicle": to_vehicle,
                    "detail": out}
        out.update({"accepted": True, "track_id": track_id,
                    "from_vehicle": from_vehicle, "to_vehicle": to_vehicle,
                    "standoff": ho.reason})
        return out

    async def _mission_cancel(self, mission_handle: str,
                              idempotency_key: str | None = None) -> dict:
        replay = self._idem_replay("mission_cancel", idempotency_key)
        if replay is not None:
            return replay
        rec = self.missions.get(mission_handle)
        if rec is None:
            return error("unknown_mission", f"no mission {mission_handle!r}")
        vehicle = rec["vehicle"]
        q = self.tasking.queue_for(vehicle)
        cur = q.current
        if cur is not None and cur.uncancellable and cur.state.value not in (
                "done", "failed", "cancelled"):
            # M4/T5: the vehicle is committed to a safety transition. Reporting
            # "cancelled" here would tell the harness the mission stopped while
            # the aircraft keeps flying the RTB.
            reason = f"{cur.tool} is an un-cancellable safety transition"
            self.store.log_audit("abort_refused", reason, vehicle=vehicle,
                                 mission_id=mission_handle)
            return {"cancelled": False, "refused": True, "reason": reason,
                    "current": cur.handle(), "mission_handle": mission_handle,
                    "mission_status": self.mission_flags.get(vehicle)}
        task = (self.tasking.get(vehicle, rec["task_id"])
                if rec.get("task_id") else None)
        if task is None:
            rec["state"] = "cancelled"
            out = {"cancelled": True, "mission_handle": mission_handle,
                   "note": "the mission had no flying task to cancel"}
            self.store.log_mission(mission_handle, "cancelled", vehicle=vehicle)
            return self._idem_record("mission_cancel", idempotency_key, out)
        if task.state.value in ("done", "failed", "cancelled"):
            return {"cancelled": False, "mission_handle": mission_handle,
                    "reason": f"already {task.state.value}",
                    "state": task.state.value}
        res = await q.abort(reason=f"mission_cancel {mission_handle}")
        if res.get("refused"):
            self.store.log_audit("abort_refused", res["reason"], vehicle=vehicle,
                                 mission_id=mission_handle)
            return {"cancelled": False, "refused": True, "reason": res["reason"],
                    "current": res["current"], "mission_handle": mission_handle,
                    "mission_status": self.mission_flags.get(vehicle)}
        rec["state"] = "cancelled"
        self.store.log_mission(mission_handle, "cancelled", vehicle=vehicle)
        self.store.log_audit("mission_cancel", f"{mission_handle} cancelled",
                             vehicle=vehicle, mission_id=mission_handle)
        out = {"cancelled": True, "mission_handle": mission_handle,
               "also_cleared": res.get("cancelled"),
               "current": res.get("current")}
        return self._idem_record("mission_cancel", idempotency_key, out)

    def _mission_state(self, mission_id: str) -> dict:
        """§4.3 mission_status: state, progress_pct, waypoint, eta_s, fuel."""
        rec = self.missions.get(mission_id)
        if rec is None:
            return error("unknown_mission", f"no mission {mission_id!r}")
        vehicle = rec["vehicle"]
        task = (self.tasking.get(vehicle, rec["task_id"])
                if rec.get("task_id") else None)
        fm = self.fuel_for(vehicle)
        tick = self.ticks.get(vehicle) or {}
        out = {
            "mission_handle": mission_id, "mission_id": mission_id,
            "vehicle": vehicle, "kind": rec["kind"], "task_id": rec["task_id"],
            "state": (task.state.value if task else rec["state"]),
            "progress_pct": (round(task.progress_pct, 1) if task else None),
            "waypoint": (task.waypoint if task else None),
            "waypoints_total": ((task.waypoints_total if task else None)
                                or len(rec.get("waypoints") or [])),
            "eta_s": (task.eta_s if task else None),
            "error": (task.error if task else None),
            "fuel_pct": round(fm.fuel_pct, 2),
            "bingo_fuel_pct": (tick.get("bingo") or {}).get("bingo_fuel_pct"),
            "bingo_latched": fm.bingo.tripped,
            "mission_status": rec.get("status") or self.mission_flags.get(vehicle),
            "coverage": rec.get("coverage"),
            "coverage_basis": (rec.get("coverage") or {}).get("basis"),
            "coverage_note": (
                "`coverage` here is the PLANNED coverage of this mission's "
                "plan. The coverage actually IMAGED is reported by "
                "uav_target_report with basis='flown' — the two are different "
                "numbers and must not be substituted for one another."),
            "warnings": rec.get("warnings") or [],
            "truncated": rec.get("truncated"),
            "lost_link_plan": rec.get("lost_link_plan"),
        }
        if rec["kind"] == "track_target":
            # M5: what the re-path loop has done to this mission. Present on
            # every track_target — an empty list means "the contact has not
            # moved far enough to re-centre the ring", which is a different
            # statement from "this mission does not re-path", and the loop's
            # own liveness says which.
            out["repaths"] = list(rec.get("repaths") or [])
            out["repath_count"] = len(out["repaths"])
            out["repath_loop_active"] = mission_id in self._repath
            out["repath_tolerance_m"] = (
                (rec.get("meta") or {}).get("repath", {}).get("reacquire_move_m"))
            out["track_poi"] = (rec.get("meta") or {}).get("poi")
            # A re-path rewrites `waypoints`/`meta` on the mission record to
            # the ring actually being flown, but `gate` — the M4 pre-flight
            # verdict, and the `bingo_fuel_pct` this call falls back to — was
            # computed for the ring the mission LAUNCHED on and is NOT re-run.
            # (The in-flight authority over a re-centred ring is the M4 BINGO
            # line, evaluated every tick and un-cancellable.) Half a record
            # updated and half not, with nothing saying which, is how a stale
            # number gets read as a current one.
            out["gate_covers_current_route"] = not out["repaths"]
        live = self._capture_progress.get(rec.get("task_id") or "")
        if live is not None:
            out.update(live)
            out["capture_trigger"] = "distance"
        elif task is not None:
            out.update(self._capture_tally(task))
        if out["bingo_fuel_pct"] is None:
            out.pop("bingo_fuel_pct")
            out["bingo_fuel_pct"] = (rec.get("gate") or {}).get("bingo_fuel_pct")
        return out

    # ---- shared detection ingest (§4.2 + M11/M12) ----
    async def _ingest_frame(self, vehicle: str, camera: str) -> dict:
        """One sensor frame -> persistent tracks. The single ingest path used
        by both `uav_get_detections` and `uav_scan_targets`."""
        dets = await self.backend.get_detections(vehicle, camera)
        tele = await self._telemetry(vehicle)
        observer = {"lat": tele["lat"], "lon": tele["lon"],
                    "alt_m": tele["alt_hae_m"], "vehicle": vehicle}
        sensor = await self._sensor_conditions(vehicle, camera)
        frame_id = f"{vehicle}:{camera}:{int(time.time() * 1000)}"
        before = len(self.tracks.rejected)
        updated = self.tracks.ingest(dets, sensor=sensor, observer=observer,
                                     frame_id=frame_id)
        for t in updated:
            self.pol.observe_track(t)
        self._persist_intel(updated)
        return {"detections": dets, "tracks": updated, "observer": observer,
                "sensor": sensor, "frame_id": frame_id,
                "rejected_this_frame": len(self.tracks.rejected) - before}

    @staticmethod
    def _observer(tele: dict) -> dict:
        """The observer dict the intel and threat layers consume.

        `observer["alt_m"]` MUST be HAE, because both consumers difference it
        against `Track.alt_m`, which is the detection's geo_point altitude —
        HAE (`threat._assess`: `dz = obs_alt - track.alt_m`;
        `targets._observation`: the same). `_ingest_frame` already passed HAE;
        the two threat entry points passed `alt_agl_m` under the same key, so
        the slant range they reported was short by the GROUND ELEVATION. At
        the Redmond origin that is ~100 m (a contact 111 m away was reported
        at 113 m instead of 137 m); in the mountain theaters, whose home sits
        at ~1550 m MSL, it puts the observing UAV more than a kilometre
        underground and collapses every engagement-envelope test.

        One key, one datum — the R3 defect class, in a second disguise. The
        explicitly-named spellings ride along so no consumer has to infer it.
        """
        return {"lat": tele["lat"], "lon": tele["lon"],
                "alt_m": tele["alt_hae_m"],
                "alt_hae_m": tele["alt_hae_m"],
                "alt_msl_m": tele.get("alt_msl_m"),
                "alt_agl_m": tele.get("alt_agl_m"),
                "datum": "hae"}

    def _track_for(self, track_id: str):
        return self.tracks.get(track_id)

    # ---- sim ground truth (§4.4 + uav://targets) ----
    async def _move_target(self, target_id: str, waypoints: list,
                           speed_mps: float, loop: bool) -> dict:
        """Drive a spawned object along geodetic waypoints (M17)."""
        if target_id not in self.targets:
            known = sorted(self.targets)
            return error("unknown_target",
                         f"no spawned target {target_id!r}; spawned: {known}")
        if not waypoints:
            return error("invalid_parameter",
                         "a route needs at least one waypoint")
        route: list[dict] = []
        for i, wp in enumerate(waypoints):
            if isinstance(wp, (list, tuple)):
                wp = {"lat": wp[0], "lon": wp[1],
                      **({"alt_msl_m": wp[2]} if len(wp) > 2 else {})}
            alt_kwargs = {k: wp[k] for k in
                          ("alt_msl_m", "alt_agl_m", "alt_hae_m", "alt_m")
                          if wp.get(k) is not None}
            if not alt_kwargs:
                # A waypoint with no altitude keeps the target on the ground it
                # was placed on, rather than defaulting to 0 (which is ~1550 m
                # underground in the shipped theaters).
                alt_kwargs = {"alt_msl_m": self.targets[target_id]["alt_msl_m"]}
            try:
                alt = self._target_altitudes(float(wp["lat"]), float(wp["lon"]),
                                             **alt_kwargs)
            except (ValueError, KeyError) as exc:
                return error("invalid_parameter", f"waypoint {i}: {exc}")
            route.append({"latitude": float(wp["lat"]), "longitude": float(wp["lon"]),
                          "altitude": alt["alt_hae_m"]})
        try:
            await self.backend.set_object_route(target_id, route, speed_mps, loop)
        except Exception as exc:  # noqa: BLE001 — fake-only RPC on real AirSim
            return error("move_target_unsupported",
                         f"this sim has no simSetObjectRoute: "
                         f"{type(exc).__name__}: {exc}")
        record = {"target_id": target_id, "waypoints": len(route),
                  "speed_mps": speed_mps, "loop": loop,
                  "route": [{"lat": r["latitude"], "lon": r["longitude"],
                             "alt_hae_m": r["altitude"]} for r in route]}
        self.targets[target_id]["route"] = record
        self.store.log_audit("move_target", f"{target_id} on a {len(route)}-point route",
                             target_id=target_id, waypoints=len(route),
                             speed_mps=speed_mps, loop=loop)
        return {"ok": True, "status": "accepted", **record}

    # ---- §4.7 report registry (uav://reports/{id}) ----
    async def _build_intrep(self, mission_id: str | None = None,
                            vehicle: str | None = None,
                            since: float | None = None,
                            detail: str = "summary",
                            top_n: int | None = INTREP_SUMMARY_TOP_N) -> dict:
        from .targets import intrep_report
        mission = self.missions.get(mission_id) if mission_id else None
        if mission is None and self.missions and mission_id is None:
            mission = max(self.missions.values(), key=lambda m: m["started"])
        veh = vehicle or (mission or {}).get("vehicle") or "UAV"
        sensors = await self._sensor_conditions(veh)
        summary, coverage = self._mission_sections(mission)
        loal = [e for e in self.loal_events
                if vehicle is None or e.get("vehicle") == veh]
        rep = intrep_report(
            self.tracks.tracks(), since=since,
            mission_id=(mission or {}).get("mission_id"),
            mission_summary=summary, coverage=coverage,
            sensor_conditions={"light": sensors.get("light"),
                               "weather": sensors.get("weather"),
                               "wind_mps": sensors.get("wind_mps"),
                               "gps_quality": sensors.get("gps_quality"),
                               "sensors_used": []},
            loal_events=loal, observer=veh,
            pattern_of_life=self.pol, detail=detail, top_n=top_n)
        report_id = (mission or {}).get("mission_id") or "latest"
        rep["report_id"] = report_id
        rep["resource"] = f"uav://reports/{report_id}"
        self.reports[report_id] = rep
        self.reports["latest"] = rep
        return rep

    def _register_tools(self) -> None:
        mcp = self.mcp

        @mcp.tool(name="uav_takeoff", description=(
            "Arm and take off. DATUM: `alt_agl_m` is metres ABOVE THE LAUNCH "
            "TERRAIN (AGL) — it is neither MSL nor HAE, and the contract name "
            "for this parameter is alt_agl_m (TOOL_CONTRACT §4.1: never a bare "
            "alt_m). `alt_m` stays accepted as a DOCUMENTED LEGACY ALIAS for "
            "the same AGL datum, for callers written against the old schema; "
            "pass one or the other, and if both are passed they must agree or "
            f"the call is refused. Default {DEFAULT_TAKEOFF_AGL_M:.0f} m AGL. "
            "Returns a task_handle (carrying alt_agl_m), or {status:'busy', "
            "current:<handle>} if the vehicle already has work (T2)."))
        async def uav_takeoff(vehicle: str, alt_agl_m: float | None = None,
                              alt_m: float | None = None,
                              idempotency_key: str | None = None) -> dict:
            try:
                alt = (_one_altitude("uav_takeoff altitude",
                                     alt_agl_m=alt_agl_m, alt_m=alt_m)
                       if (alt_agl_m is not None or alt_m is not None)
                       else DEFAULT_TAKEOFF_AGL_M)
            except ValueError as exc:
                return error("invalid_parameter", str(exc))
            alt_m = alt
            gate_v = self.envelope.check_point(*self._home_latlon(vehicle), float(alt_m))
            if gate_v:
                self.store.log_audit("preflight_reject", "takeoff outside envelope",
                                     vehicle=vehicle, violations=gate_v)
                return {"rejected": True, "gate": {"ok": False, "envelope_violations": gate_v}}
            # The task params keep the internal `alt_m` key (the executor and
            # every journal written so far use it) but the DATUMED spelling is
            # written alongside it and published on the handle, so nothing a
            # caller or a replay reads is a bare altitude.
            handle = self._submit(vehicle, "uav_takeoff",
                                  {"alt_m": alt_m, "alt_agl_m": alt_m},
                                  idempotency_key)
            handle["alt_agl_m"] = float(alt_m)
            return handle

        @mcp.tool(name="uav_land", description="Land at current position.")
        async def uav_land(vehicle: str, idempotency_key: str | None = None) -> dict:
            return self._submit(vehicle, "uav_land", {}, idempotency_key)

        @mcp.tool(name="uav_return_to_home", description=(
            "Fly back to envelope home and land. Gated like any other movement "
            "(M4); the server's own BINGO RTB is un-cancellable and bypasses the gate."))
        async def uav_return_to_home(vehicle: str, speed_mps: float = 10.0,
                                     idempotency_key: str | None = None) -> dict:
            home = self.envelope.home
            if home is None:
                return {"rejected": True, "error": "no home configured in safety envelope"}
            gate = await self._gate(vehicle, [{"lat": home[0], "lon": home[1], "alt_m": 0.0}],
                                    speed_mps)
            if not gate["ok"] and gate.get("envelope_violations"):
                return {"rejected": True, "gate": gate}
            handle = self._submit(vehicle, "uav_return_to_home",
                                  {"speed_mps": speed_mps}, idempotency_key)
            handle["bingo_fuel_pct"] = gate.get("bingo_fuel_pct")
            return handle

        @mcp.tool(name="uav_goto_gps", description=(
            "Go to lat/lon and hold. DATUM: `alt_agl_m` is metres ABOVE THE "
            "LAUNCH TERRAIN (AGL) — neither MSL nor HAE — and is the contract "
            "name for this parameter (TOOL_CONTRACT §4.1: never a bare alt_m). "
            "`alt_m` stays accepted as a DOCUMENTED LEGACY ALIAS for the same "
            "AGL datum; pass one or the other, and if both are passed they "
            "must agree or the call is refused. Pre-flight BINGO + geofence "
            "gate from the vehicle's CURRENT position, with wind (M15)."))
        async def uav_goto_gps(vehicle: str, lat: float, lon: float,
                               alt_agl_m: float | None = None,
                               alt_m: float | None = None,
                               speed_mps: float = 10.0,
                               idempotency_key: str | None = None) -> dict:
            try:
                alt = _one_altitude("uav_goto_gps altitude",
                                    alt_agl_m=alt_agl_m, alt_m=alt_m)
            except ValueError as exc:
                return error("invalid_parameter", str(exc))
            alt_m = alt
            gate = await self._gate(vehicle, [{"lat": lat, "lon": lon, "alt_m": alt_m}], speed_mps)
            if not gate["ok"]:
                return {"rejected": True, "gate": gate}
            handle = self._submit(vehicle, "uav_goto_gps",
                                  {"lat": lat, "lon": lon, "alt_m": alt_m,
                                   "alt_agl_m": alt_m, "speed_mps": speed_mps},
                                  idempotency_key)
            handle["alt_agl_m"] = float(alt_m)
            handle["bingo_fuel_pct"] = gate["bingo_fuel_pct"]
            handle["plan_required_pct"] = gate["required_pct"]
            return handle

        @mcp.tool(name="uav_fly_route", description=(
            "Fly an ordered waypoint route. Each waypoint is "
            "{lat, lon, alt_agl_m} — metres ABOVE THE LAUNCH TERRAIN, neither "
            "MSL nor HAE (TOOL_CONTRACT §4.1: never a bare alt_m). `alt_m` is "
            "accepted per-waypoint as a DOCUMENTED LEGACY ALIAS for the same "
            "AGL datum; a waypoint carrying NEITHER is refused rather than "
            "flown at 0 m AGL, and one carrying both must have them agree. "
            "Pre-flight BINGO + geofence gate from the vehicle's current "
            "position, with wind (M15)."))
        async def uav_fly_route(vehicle: str, waypoints: list[dict],
                                speed_mps: float = 10.0,
                                idempotency_key: str | None = None) -> dict:
            gate = await self._gate(vehicle, waypoints, speed_mps)
            if not gate["ok"]:
                return {"rejected": True, "gate": gate}
            handle = self._submit(vehicle, "uav_fly_route",
                                  {"waypoints": waypoints, "speed_mps": speed_mps},
                                  idempotency_key)
            handle["bingo_fuel_pct"] = gate["bingo_fuel_pct"]
            handle["plan_required_pct"] = gate["required_pct"]
            return handle

        @mcp.tool(name="uav_hover", description="Hover in place.")
        async def uav_hover(vehicle: str, idempotency_key: str | None = None) -> dict:
            return self._submit(vehicle, "uav_hover", {}, idempotency_key)

        @mcp.tool(name="uav_abort", description=(
            "Cancel current task + clear queue + hover (T2). REFUSED while an "
            "un-cancellable safety transition (BINGO force-RTB, M4) is flying."))
        async def uav_abort(vehicle: str) -> dict:
            q = self.tasking.queue_for(vehicle)
            fm = self.fuel_for(vehicle)
            if fm.bingo.tripped:
                # Counted, refused: a harness command can never clear BINGO (T5).
                fm.bingo.clear(operator_override=False)
            res = await q.abort()
            if res.get("refused"):
                self.store.log_audit("abort_refused", res["reason"], vehicle=vehicle,
                                     clear_attempts=fm.bingo.clear_attempts)
                return {"aborted": False, "refused": True, "reason": res["reason"],
                        "current": res["current"],
                        "mission_status": self.mission_flags.get(vehicle),
                        "bingo": fm.bingo.to_dict()}
            self.store.log_audit("abort", "uav_abort: queue cleared, hover", vehicle=vehicle)
            out = self._submit(vehicle, "uav_hover", {}, None, allow_queue=True)
            out["aborted"] = True
            out["cancelled"] = res.get("cancelled")
            out["mission_status"] = self.mission_flags.get(vehicle)
            return out

        @mcp.tool(name="uav_orbit_poi", description=(
            "Fly a standoff orbit around a point of interest (M3). alt_agl_m "
            "is metres above the launch datum. direction is cw|ccw. The SUN-"
            "SIDE RULE (M6) is applied unless sun_side=False: the ring starts "
            "at the sun's azimuth from the POI so the aircraft sits between "
            "the sun and the contact, keeping the sun BEHIND the sensor — the "
            "return states which arc was chosen and why, including when there "
            "is no sun side to favour. camera_track slews the gimbal to the "
            "POI for the orbit geometry."))
        async def uav_orbit_poi(vehicle: str, lat: float, lon: float,
                                radius_m: float = 150.0, alt_agl_m: float = 60.0,
                                direction: str = "cw", laps: int = 1,
                                points: int = 12, camera_track: bool = True,
                                sun_side: bool = True, camera: str = "0",
                                speed_mps: float = 10.0,
                                dry_run: bool = False,
                                idempotency_key: str | None = None) -> dict:
            return await self._mission_orbit_poi(
                vehicle, lat=lat, lon=lon, radius_m=radius_m,
                alt_agl_m=alt_agl_m, direction=direction, laps=laps,
                points=points, camera_track=camera_track, sun_side=sun_side,
                camera=camera, speed_mps=speed_mps, dry_run=dry_run,
                idempotency_key=idempotency_key)

        @mcp.tool(name="uav_set_gimbal", description=(
            "Slew the sensor gimbal. pitch_deg/yaw_deg/roll_deg are BODY-"
            "relative degrees (pitch +up, so nadir is -90; yaw + clockwise "
            "from the nose). Pass track_geo_point {lat, lon, and one of "
            "alt_msl_m|alt_agl_m|alt_hae_m} instead and the server derives the "
            "angles from live telemetry. The pose is READ BACK from the sim "
            "and returned — never merely echoed."))
        async def uav_set_gimbal(vehicle: str, camera: str = "0",
                                 pitch_deg: float | None = None,
                                 yaw_deg: float | None = None,
                                 roll_deg: float = 0.0,
                                 track_geo_point: dict | None = None,
                                 idempotency_key: str | None = None) -> dict:
            replay = self._idem_replay("uav_set_gimbal", idempotency_key)
            if replay is not None:
                return replay
            derivation = None
            if track_geo_point is not None:
                try:
                    aim = await self._aim_at(vehicle, track_geo_point)
                except (ValueError, KeyError) as exc:
                    return error("invalid_parameter", str(exc))
                except Exception as exc:  # noqa: BLE001 — surfaced, not defaulted
                    return error("telemetry_unavailable",
                                 f"{type(exc).__name__}: {exc}", retryable=True)
                pitch_deg, yaw_deg = aim["pitch_deg"], aim["yaw_deg"]
                derivation = aim
            if pitch_deg is None or yaw_deg is None:
                return error("missing_parameter",
                             "supply pitch_deg and yaw_deg, or track_geo_point")
            try:
                state = await self.backend.set_camera_pose(
                    vehicle, camera, float(pitch_deg), float(yaw_deg),
                    float(roll_deg))
            except Exception as exc:  # noqa: BLE001 — a failed slew is reported
                return error("gimbal_failed", f"{type(exc).__name__}: {exc}",
                             retryable=True)
            self.store.log_audit("gimbal", f"{camera} pitch={pitch_deg} yaw={yaw_deg}",
                                 vehicle=vehicle, camera=camera,
                                 pitch_deg=pitch_deg, yaw_deg=yaw_deg)
            out = {"ok": True, "vehicle": vehicle, "camera": camera,
                   "commanded": {"pitch_deg": float(pitch_deg),
                                 "yaw_deg": float(yaw_deg),
                                 "roll_deg": float(roll_deg)},
                   "camera_state": state, "derived_from": derivation,
                   "status": "accepted"}
            return self._idem_record("uav_set_gimbal", idempotency_key, out)

        @mcp.tool(name="uav_set_fov", description=(
            "Set the camera's horizontal field of view in degrees. This is "
            "what makes the M7 wide->narrow cross-cue possible: a narrower "
            "field puts more pixels on the contact at the same slant range. "
            "The FOV is READ BACK from the sim and the resulting ground swath "
            "at the vehicle's current alt_agl_m is returned with it."))
        async def uav_set_fov(vehicle: str, fov_deg: float, camera: str = "0",
                              idempotency_key: str | None = None) -> dict:
            replay = self._idem_replay("uav_set_fov", idempotency_key)
            if replay is not None:
                return replay
            if not (0.0 < float(fov_deg) < 180.0):
                return error("invalid_parameter",
                             f"fov_deg must be in (0, 180), got {fov_deg!r}")
            try:
                state = await self.backend.set_camera_fov(vehicle, camera,
                                                          float(fov_deg))
            except Exception as exc:  # noqa: BLE001 — a failed command is reported
                return error("fov_failed", f"{type(exc).__name__}: {exc}",
                             retryable=True)
            got = state.get("fov_deg")
            if got is None or abs(float(got) - float(fov_deg)) > 0.1:
                return error("fov_not_applied",
                             f"commanded {fov_deg} deg, sim reports {got!r}; "
                             "every footprint derived from this FOV would be "
                             "wrong, so the command is reported as failed")
            out = {"ok": True, "vehicle": vehicle, "camera": camera,
                   "fov_deg": float(got), "status": "accepted",
                   "camera_state": state}
            try:
                tele = await self._telemetry(vehicle)
                from .missions import footprint_m, ground_sample_distance_m
                agl = max(0.1, float(tele["alt_agl_m"]))
                w, h = footprint_m(agl, float(got), float(got) * 0.75)
                out["alt_agl_m"] = tele["alt_agl_m"]
                out["swath_m"] = round(w, 2)
                out["footprint_m"] = [round(w, 2), round(h, 2)]
                out["gsd_m_per_px_at_nadir"] = round(
                    ground_sample_distance_m(agl, float(got), 640), 4)
            except Exception as exc:  # noqa: BLE001 — the FOV still applied
                out["footprint_error"] = f"{type(exc).__name__}: {exc}"
            self.store.log_audit("sensor_fov", f"{camera} -> {got} deg",
                                 vehicle=vehicle, camera=camera, fov_deg=got)
            return self._idem_record("uav_set_fov", idempotency_key, out)

        @mcp.tool(name="uav_capture_image", description=(
            "Capture one frame. type = scene | depth | segmentation | infrared "
            "(plus AirSim's depth_planar/depth_perspective/depth_vis/"
            "disparity/surface_normals). Returns the resource ref "
            "uav://{vehicle}/camera/{camera}/{type} that serves the bytes, the "
            "geo pose of the CAMERA (lat/lon with alt_hae_m + alt_msl_m + "
            "alt_agl_m, one conversion point) and the sun angle at capture "
            "time (M6)."))
        async def uav_capture_image(vehicle: str, camera: str = "0",
                                    type: str = "scene",
                                    jpeg_quality: int | None = None,
                                    idempotency_key: str | None = None) -> dict:
            replay = self._idem_replay("uav_capture_image", idempotency_key)
            if replay is not None:
                return replay
            try:
                meta = await self._capture_frame(vehicle, camera, type,
                                                 jpeg_quality=jpeg_quality)
            except ValueError as exc:
                return error("invalid_parameter", str(exc))
            except Exception as exc:  # noqa: BLE001 — no pixels is not a blank frame
                return error("capture_failed",
                             f"{vehicle}/{camera}/{type}: "
                             f"{exc.__class__.__name__}: {exc}", retryable=True)
            return self._idem_record("uav_capture_image", idempotency_key, meta)

        @mcp.tool(name="uav_get_detections", description=(
            "Raw DetectionInfo[] for one camera, each correlated into the "
            "persistent track store so every contact carries its track_id "
            "(M11) and its own confidence with cited evidence. Positions are "
            "the contact's own geo_point — a detection with none is REJECTED "
            "and reported, never back-filled from the observer."))
        async def uav_get_detections(vehicle: str, camera: str = "0") -> dict:
            frame = await self._ingest_frame(vehicle, camera)
            from .targets import assess_confidence
            accepted = iter(frame["tracks"])
            out: list[dict] = []
            for det in frame["detections"]:
                gp = det.get("geo_point") or {}
                if gp.get("latitude") is None or gp.get("longitude") is None:
                    out.append({**det, "track_id": None, "confidence": None,
                                "rejected": ("no geo_point on the detection; "
                                             "the observer position is never "
                                             "substituted for a contact")})
                    continue
                t = next(accepted, None)
                row = {**det, "track_id": (t.track_id if t else None)}
                if t is not None:
                    conf = assess_confidence(t)
                    row["confidence"] = {"level": conf["level"],
                                         "score": conf["score"],
                                         "evidence": conf.get("evidence")}
                    row["ob_class"] = t.ob_class
                    row["category"] = t.category
                    row["sightings"] = len(t.observations)
                out.append(row)
            return {"vehicle": vehicle, "camera": camera,
                    "frame_id": frame["frame_id"], "observer": frame["observer"],
                    "sensor": frame["sensor"], "count": len(out),
                    "rejected_this_frame": frame["rejected_this_frame"],
                    "detections": out}

        @mcp.tool(name="uav_los_check", description=(
            "Line of sight from the vehicle to a point. Give the target "
            "altitude as exactly one of alt_msl_m, alt_agl_m or alt_hae_m. "
            "Returns {los, first_obstacle, model} where `model` states what "
            "was ACTUALLY modelled — this never answers an unconditional true, "
            "and when no LOS model can be reached it returns a structured "
            "error rather than assuming the sight line is clear (M5). When the "
            "real-world data layer is on, the sight line is ALSO cut against "
            "MEASURED bare-earth terrain along the great circle, and "
            "`los_is_measured` says whether the answer you are trusting rests "
            "on real terrain or on the sim's geometric horizon."))
        async def uav_los_check(vehicle: str, lat: float, lon: float,
                                alt_msl_m: float | None = None,
                                alt_agl_m: float | None = None,
                                alt_hae_m: float | None = None,
                                alt_m: float | None = None,
                                terrain: str = "cached") -> dict:
            if terrain not in LOS_TERRAIN_MODES:
                return error("invalid_parameter",
                             f"terrain={terrain!r} is not one of "
                             f"{list(LOS_TERRAIN_MODES)}")
            try:
                target = self._target_altitudes(lat, lon, alt_msl_m=alt_msl_m,
                                                alt_agl_m=alt_agl_m,
                                                alt_hae_m=alt_hae_m, alt_m=alt_m)
            except ValueError as exc:
                return error("invalid_parameter", str(exc))
            # The observer is needed BEFORE the terrain profile can be cut, so
            # telemetry is read first and its failure no longer merely annotates
            # the answer — without a position there is no sight line to check.
            observer: dict = {}
            tele = None
            try:
                tele = await self._telemetry(vehicle)
                observer = {"lat": tele["lat"], "lon": tele["lon"],
                            "alt_hae_m": tele["alt_hae_m"],
                            "alt_agl_m": tele["alt_agl_m"],
                            "alt_agl_is_real": tele["alt_agl_is_real"],
                            "alt_agl_source": tele["alt_agl_source"]}
            except Exception as exc:  # noqa: BLE001 — the sim verdict still stands
                observer = {"error": f"{type(exc).__name__}: {exc}"}
            cut: Any = None
            if tele is not None and terrain != "off":
                observer_llh = (tele["lat"], tele["lon"], tele["alt_hae_m"])
                target_llh = (float(lat), float(lon), float(target["alt_hae_m"]))
                try:
                    if terrain == "fetch":
                        # Explicit opt-in: the caller has asked to WAIT for a
                        # measured answer. On a worker thread, so the telemetry
                        # tick loop on the event loop keeps running.
                        cut = await asyncio.to_thread(
                            self.terrain_los, observer_llh, target_llh,
                            allow_network=True)
                    else:
                        cut = self.terrain_los(observer_llh, target_llh)
                except Exception as exc:  # noqa: BLE001 — never assumed clear
                    cut = None
                    self.real_data_error = f"terrain LOS: {type(exc).__name__}: {exc}"
            res: dict | None = None
            sim_failure: str | None = None
            try:
                res = await self.backend.los_to_point(vehicle, lat, lon,
                                                      target["alt_hae_m"])
            except LosModelUnavailableError as exc:
                sim_failure = str(exc)
            except Exception as exc:  # noqa: BLE001 — surfaced, never assumed
                sim_failure = f"{type(exc).__name__}: {exc}"
            measured = cut is not None and cut.known
            if res is None and not terrain_los_votes(cut):
                # Neither model answered on evidence. No boolean is returned.
                return error(
                    "los_unavailable",
                    sim_failure or "no line-of-sight model answered",
                    retryable=True, los=None,
                    terrain=(None if cut is None else cut.as_dict()),
                    note=("no boolean is returned: an unverifiable sight line "
                          "must not read as clear"))
            if res is None:
                # The sim model is gone but terrain ANSWERED (fully measured,
                # or partial with a real obstruction in it). That is a better
                # answer than the one that was lost, not a fallback.
                res = {"los": cut.los, "first_obstacle": cut.first_obstacle,
                       "first_obstacle_modelled": True,
                       "model": cut.model, "source": "realdata.TerrainProvider",
                       "sim_model_error": sim_failure}
            elif terrain_los_votes(cut):
                # Both answered. A sight line is clear only if EVERY model that
                # answered says so — a terrain block the sim cannot see is still
                # a block, and the sim's declared obstruction cylinders are not
                # in the bare-earth DEM. Composition is AND, never "prefer one".
                res["sim_los"] = res["los"]
                res["sim_model"] = res["model"]
                res["los"] = bool(res["los"]) and bool(cut.los)
                if not cut.los and cut.first_obstacle is not None:
                    res["first_obstacle"] = cut.first_obstacle
                    res["first_obstacle_modelled"] = True
                res["model"] = (
                    f"{res['sim_model']}  ||  TERRAIN: {cut.model}  ||  "
                    "COMPOSED: los is true only when BOTH models say clear")
            elif cut is not None:
                # "EVERY model that ANSWERED" — and an UNKNOWN cut has not.
                # `TerrainProvider.line_of_sight` returns `los=False` when no
                # height is resident along the profile, which is the honest
                # refusal at that layer but is NOT an observation of terrain.
                # ANDing it in turned every cold profile into a hard block:
                # measured over the wire, a flat, clear 800 m line over the
                # Fordow valley floor came back los=False / sim_los=True /
                # first_obstacle=None, with `los_basis` telling the operator the
                # answer rested on the sim's horizon — which had said CLEAR. And
                # the layer being OFF returns that same sim verdict as `true`,
                # so switching real data ON inverted the answer. The profile is
                # prefetched by `terrain_los`, so the next ask is measured.
                res["terrain_known"] = False
                res["terrain_not_consulted"] = (
                    "no bare-earth height is resident along this profile, so "
                    "terrain did not vote. It is NOT counted as an obstruction: "
                    "an unread model is not a block. Re-ask with "
                    "terrain='fetch' for a measured cut now, or again in a "
                    "moment — the profile has been prefetched.")
            if cut is not None:
                res["terrain"] = cut.as_dict()
            res["terrain_mode"] = terrain
            res["los_is_measured"] = measured
            res["los_basis"] = (
                "MEASURED: cut against real bare-earth terrain along the great "
                "circle. Vegetation and buildings are not modelled."
                if measured else
                "ASSUMED: no real terrain was available on this call, so the "
                "answer rests on the sim's geometric horizon and declared "
                "obstructions only. Do not read it as terrain-verified (M5 "
                "standoff); re-ask with terrain='fetch' for a measured cut.")
            if tele is not None:
                ground = haversine_m(tele["lat"], tele["lon"], lat, lon)
                res["ground_range_m"] = round(ground, 1)
                res["slant_range_m"] = round(
                    math.hypot(ground, tele["alt_hae_m"] - target["alt_hae_m"]), 1)
            res["vehicle"] = vehicle
            res["observer"] = observer
            res["target"] = {"lat": lat, "lon": lon, **target}
            return res

        @mcp.tool(name="uav_get_telemetry", description=(
            "Live telemetry: lat/lon, alt_hae_m + alt_msl_m + alt_agl_m (T1, one "
            "conversion point), attitude, velocity, wind, fuel_pct, bingo_fuel_pct, "
            "landed_state, link state and queue status. `alt_agl_m` is height "
            "above MEASURED terrain when the real-world data layer has the "
            "ground under the aircraft and height above the LAUNCH DATUM when it "
            "does not: `alt_agl_is_real` says which, `alt_agl_source` names it, "
            "`alt_agl_launch_datum_m` always carries the launch-datum figure "
            "alongside, and `terrain` carries the sample's own provenance. Do "
            "not treat an AGL as measured without checking that flag."))
        async def uav_get_telemetry(vehicle: str) -> dict:
            return await self._telemetry_payload(vehicle)

        @mcp.tool(name="uav_list_vehicles", description=(
            "List vehicles and queue states. `roster_source` says where the "
            "names came from: 'sim' is the authoritative roster. If the sim "
            "cannot be asked, this returns a structured error with "
            "roster_source='unavailable' and, separately, the vehicles this "
            "SERVER has commanded — it never substitutes a guessed roster."))
        async def uav_list_vehicles() -> dict:
            try:
                names = await self.backend.list_vehicles()
            except Exception as exc:  # noqa: BLE001 — surfaced, never faked
                detail = f"{type(exc).__name__}: {exc}"
                self.vehicle_roster_error = detail
                self.store.log_audit("vehicle_roster_unavailable", detail)
                known = sorted(set(self.tasking.vehicles())
                               | set(self._last_tele))
                return {
                    **error("vehicle_roster_unavailable",
                            f"the sim could not be asked for its vehicle "
                            f"roster: {detail}", retryable=True),
                    "roster_source": "unavailable",
                    "degraded": True,
                    "vehicles": None,
                    "vehicles_this_server_has_commanded": [
                        {"name": n, **self.tasking.status(n)} for n in known],
                    "note": ("these are names this server has already worked "
                             "with, NOT the sim's roster; vehicles it has "
                             "never touched are missing from it"),
                }
            self.vehicle_roster_error = None
            return {"vehicles": [{"name": n, **self.tasking.status(n)} for n in names],
                    "roster_source": "sim", "degraded": False}

        @mcp.tool(name="uav_mission", description=(
            "THIN DISPATCHER (TOOL_CONTRACT §4.3) kept so the GEV control panel "
            "keeps working: forwards to the discrete mission_* tools. kind = "
            "recon_route | grid_search | orbit_poi | track_target | identify | "
            "assess. params: recon_route{waypoints, alt_agl_m}, "
            "grid_search{polygon, alt_agl_m, overlap_pct (percent)}, "
            "orbit_poi/assess{lat, lon, radius_m?, alt_agl_m}, "
            "track_target{track_id, alt_agl_m}, "
            "identify{track_id, alt_agl_m, max_id_alt_agl_m? (the ID-search "
            "ceiling a los_blocked refusal tells the operator to raise)}, "
            "lost_link_plan{behaviour: hold_orbit|climb_for_los|rtb|continue} (M9). "
            "Altitudes are metres AGL above the launch datum. dry_run=True "
            "returns the plan product and executes nothing."))
        async def uav_mission(vehicle: str, kind: str, params: dict = {},
                              speed_mps: float | None = None,
                              idempotency_key: str | None = None,
                              dry_run: bool = False) -> dict:
            return await self._dispatch_mission(
                kind, vehicle, dict(params or {}), speed_mps=speed_mps,
                idempotency_key=idempotency_key, dry_run=dry_run)

        # ---------------------------------------------------------- §4.3 ----
        @mcp.tool(name="mission_grid_search", description=(
            "Area search (M1). Lane spacing is SERVER-DERIVED: swath = "
            "2*alt_agl_m*tan(HFOV/2), spacing = swath*(1-overlap_pct). The "
            "caller supplies overlap_pct as a PERCENT (20 means 20% overlap), "
            "never a lane spacing. alt_agl_m is metres above the launch datum. Returns the "
            "mission handle, the footprint, the lane spacing ACTUALLY FLOWN, "
            "the waypoints, est_time_s, est_fuel_pct and the M4 gate."))
        async def mission_grid_search(vehicle: str, polygon: list,
                                      alt_agl_m: float = 60.0,
                                      overlap_pct: float = 20.0,
                                      pattern: str = "lawnmower",
                                      camera: str = "0",
                                      speed_mps: float = 8.0,
                                      max_lanes: int | None = None,
                                      legs: int = 12,
                                      lost_link_plan: dict | None = None,
                                      dry_run: bool = False,
                                      idempotency_key: str | None = None) -> dict:
            return await self._mission_grid_search(
                vehicle, polygon=polygon, alt_agl_m=alt_agl_m,
                overlap_pct=overlap_pct, pattern=pattern, camera=camera,
                speed_mps=speed_mps, max_lanes=max_lanes, legs=legs,
                lost_link_plan=lost_link_plan, dry_run=dry_run,
                idempotency_key=idempotency_key)

        @mcp.tool(name="mission_recon_route", description=(
            "Route recon with DISTANCE-triggered captures (M2): one capture "
            "every along_track_swath*(1-forward_overlap_pct) metres. "
            "max_capture_rate_hz is a max-rate CLAMP only and, when it binds, "
            "the return reports the widened interval and the overlap actually "
            "achieved. alt_agl_m is metres above the launch datum; "
            "forward_overlap_pct is a PERCENT (20 means 20% forward overlap)."))
        async def mission_recon_route(vehicle: str, waypoints: list,
                                      alt_agl_m: float = 60.0,
                                      forward_overlap_pct: float = 20.0,
                                      camera: str = "0",
                                      speed_mps: float = 10.0,
                                      max_capture_rate_hz: float = 1.0,
                                      lost_link_plan: dict | None = None,
                                      dry_run: bool = False,
                                      idempotency_key: str | None = None) -> dict:
            return await self._mission_recon_route(
                vehicle, waypoints=waypoints, alt_agl_m=alt_agl_m,
                forward_overlap_pct=forward_overlap_pct, camera=camera,
                speed_mps=speed_mps, max_capture_rate_hz=max_capture_rate_hz,
                lost_link_plan=lost_link_plan, dry_run=dry_run,
                idempotency_key=idempotency_key)

        @mcp.tool(name="mission_track_target", description=(
            "Follow a track at a SERVER-DERIVED standoff (M5): the contact's "
            "order-of-battle threat ring, cross-checked against the narrow-FOV "
            "pixel density needed for ID, then VERIFIED with the same "
            "line-of-sight model uav_los_check reports. A caller-supplied "
            "radius is not accepted. alt_agl_m is metres above the launch "
            "datum. Masked ring points are dropped and reported."))
        async def mission_track_target(vehicle: str, track_id: str,
                                       alt_agl_m: float = 120.0,
                                       camera: str = "0",
                                       speed_mps: float = 12.0,
                                       points: int = 12,
                                       allow_unverified: bool = False,
                                       lost_link_plan: dict | None = None,
                                       dry_run: bool = False,
                                       idempotency_key: str | None = None) -> dict:
            return await self._mission_track_target(
                vehicle, track_id=track_id, alt_agl_m=alt_agl_m, camera=camera,
                speed_mps=speed_mps, points=points,
                allow_unverified=allow_unverified, lost_link_plan=lost_link_plan,
                dry_run=dry_run, idempotency_key=idempotency_key)

        @mcp.tool(name="mission_identify_target", description=(
            "Wide-FOV detect then CROSS-CUE to narrow FOV at a reduced slant "
            "range (M7). Two phases, each carrying the fov_deg the server "
            "drives uav_set_fov to; the threat ring is a hard floor in both "
            "(ISR-only, M14). alt_agl_m is metres above the launch datum. "
            "max_id_alt_agl_m is the CEILING the ID-altitude search may climb "
            "to (default: the detect altitude) and is the remedy a "
            "los_blocked refusal names: raise it above the band the refusal "
            "reports and the masked ID ring is re-flown higher. The standoff "
            "is never traded for it (M14). "
            "Returns the mission handle plus {classification, confidence, "
            "geo_point, track_history, key_images}."))
        async def mission_identify_target(vehicle: str, track_id: str,
                                          alt_agl_m: float = 120.0,
                                          orbit_first: bool = True,
                                          camera: str = "0",
                                          speed_mps: float = 10.0,
                                          max_id_alt_agl_m: float | None = None,
                                          allow_unverified: bool = False,
                                          lost_link_plan: dict | None = None,
                                          dry_run: bool = False,
                                          idempotency_key: str | None = None) -> dict:
            return await self._mission_identify_target(
                vehicle, track_id=track_id, alt_agl_m=alt_agl_m,
                orbit_first=orbit_first, camera=camera, speed_mps=speed_mps,
                max_id_alt_agl_m=max_id_alt_agl_m,
                allow_unverified=allow_unverified, lost_link_plan=lost_link_plan,
                dry_run=dry_run, idempotency_key=idempotency_key)

        @mcp.tool(name="mission_threat_assessment", description=(
            "Deterministic order-of-battle threat assessment over an area "
            "(M13, PLAN §4.6): capability from the OB library, intent from "
            "posture/movement/pattern-of-life/emissions, confidence with cited "
            "evidence. ISR-only (M14): sensor-posture advice only, no "
            "engagement recommendation. Optionally queues a recon of the area "
            "(survey=True) so the assessment is flown, not just computed. "
            "SIZE: contacts are SUMMARISED by default — every flat, traceable "
            "score component is kept and the expandable sub-assessments are "
            "dropped — and the top `top_n` by threat score are returned; the "
            "rest are compact rows in `omitted`, named in `truncation`. Pass "
            "detail='full' for the cited evidence, or top_n=null for no cap. "
            "Both can be very large: this grows with the track store, which "
            "persists across runs."))
        async def mission_threat_assessment(vehicle: str,
                                            area_polygon: list | None = None,
                                            defended: list | None = None,
                                            survey: bool = False,
                                            alt_agl_m: float = 60.0,
                                            overlap_pct: float = 20.0,
                                            camera: str = "0",
                                            speed_mps: float = 8.0,
                                            dry_run: bool = False,
                                            detail: str = "summary",
                                            top_n: int | None = THREAT_SUMMARY_TOP_N,
                                            idempotency_key: str | None = None) -> dict:
            return await self._mission_threat_assessment(
                vehicle, area_polygon=area_polygon, defended=defended,
                survey=survey, alt_agl_m=alt_agl_m, overlap_pct=overlap_pct,
                camera=camera, speed_mps=speed_mps, dry_run=dry_run,
                detail=detail, top_n=top_n,
                idempotency_key=idempotency_key)

        @mcp.tool(name="mission_handoff_track", description=(
            "Hand custody of a track to another UAV (M10): the receiver must "
            "have the fuel and the range, and the standoff it is sent to is "
            "derived from the contact's weapon envelope (M5). On accept, an "
            "orbit mission is queued for the receiver."))
        async def mission_handoff_track(track_id: str, from_vehicle: str,
                                        to_vehicle: str,
                                        alt_agl_m: float = 60.0,
                                        dry_run: bool = False,
                                        idempotency_key: str | None = None) -> dict:
            return await self._mission_handoff_track(
                track_id=track_id, from_vehicle=from_vehicle,
                to_vehicle=to_vehicle, alt_agl_m=alt_agl_m, dry_run=dry_run,
                idempotency_key=idempotency_key)

        @mcp.tool(name="mission_status", description=(
            "State of one mission: state, progress_pct (server-derived from "
            "telemetry), waypoint, eta_s, fuel_pct and bingo_fuel_pct."))
        async def mission_status(mission_handle: str) -> dict:
            return self._mission_state(mission_handle)

        @mcp.tool(name="mission_cancel", description=(
            "Cancel a mission. REFUSED while an un-cancellable safety "
            "transition (BINGO force-RTB, lost-link RTB) is flying — a harness "
            "cannot cancel its way out of a safety commitment (M4/T5)."))
        async def mission_cancel(mission_handle: str,
                                 idempotency_key: str | None = None) -> dict:
            return await self._mission_cancel(mission_handle, idempotency_key)

        @mcp.tool(name="mission_dry_run", description=(
            "Plan product ONLY — nothing is queued and the vehicle is not "
            "commanded (PLAN §5: task -> plan -> dry-run -> execute -> "
            "monitor). Same parameters as the mission tools: kind = "
            "grid_search | recon_route | track_target | identify | orbit_poi | "
            "assess, plus that kind's params. Returns waypoints, est_time_s, "
            "est_fuel_pct and the M4 gate verdict."))
        async def mission_dry_run(vehicle: str, kind: str = "grid_search",
                                  params: dict | None = None,
                                  polygon: list | None = None,
                                  waypoints: list | None = None,
                                  track_id: str | None = None,
                                  alt_agl_m: float | None = None,
                                  overlap_pct: float | None = None,
                                  camera: str | None = None,
                                  speed_mps: float | None = None) -> dict:
            kw = dict(params or {})
            for key, val in (("polygon", polygon), ("waypoints", waypoints),
                             ("track_id", track_id), ("alt_agl_m", alt_agl_m),
                             ("overlap_pct", overlap_pct), ("camera", camera)):
                if val is not None:
                    kw[key] = val
            return await self._dispatch_mission(kind, vehicle, kw,
                                                speed_mps=speed_mps, dry_run=True)

        @mcp.tool(name="uav_scan_targets", description=(
            "Pull ground-truth detections, correlate into persistent numbered "
            "tracks (M11) with measured pixels-on-target, classify by "
            "order-of-battle, fold into the pattern-of-life store (M12). "
            "Returns SALUTE reports (M8) with element aggregation."))
        async def uav_scan_targets(vehicle: str, camera: str = "0") -> dict:
            frame = await self._ingest_frame(vehicle, camera)
            updated = frame["tracks"]
            from .targets import salute_report
            peers = self.tracks.tracks()
            for t in updated:
                self.store.log_audit("target_track", f"{t.track_id} {t.category}",
                                     vehicle=vehicle, track_id=t.track_id,
                                     lat=t.lat, lon=t.lon)
            return {"vehicle": vehicle, "detections": len(frame["detections"]),
                    "frame_id": frame["frame_id"], "observer": frame["observer"],
                    "sensor": frame["sensor"],
                    "rejected_detections": len(self.tracks.rejected),
                    "tracks_updated": [t.track_id for t in updated],
                    "tracks": [salute_report(t, observer=vehicle, peers=peers)
                               for t in updated]}

        @mcp.tool(name="uav_identify_target", description=(
            "Identify + report one track as a SALUTE report (M8) with element "
            "aggregation against the whole track store."))
        async def uav_identify_target(track_id: str) -> dict:
            from .targets import salute_report
            t = self.tracks.get(track_id)
            if not t:
                return {"error": f"unknown track {track_id}"}
            rep = salute_report(t, peers=self.tracks.tracks())
            self.store.log_audit("identify", f"{track_id} identified",
                                 track_id=track_id, category=t.category,
                                 confidence=rep["confidence_level"])
            return rep

        @mcp.tool(name="uav_target_report", description=(
            "Full INTREP (PLAN §4.7, M8): mission summary, coverage %, contacts "
            "with track ids, sensor conditions, LOAL events and derived gaps. "
            "SIZE: contacts are SUMMARISED by default (all six SALUTE fields, "
            "minus each contact's confidence derivation) and the top `top_n` are "
            "expanded; the rest are compact rows in `contacts_omitted` and named "
            "in `truncation`. The counts, confidence_summary and gaps always "
            "cover EVERY contact. Pass detail='full' for the evidence, or "
            "top_n=null for no cap — both can be very large, because the track "
            "store persists across runs."))
        async def uav_target_report(since: float | None = None,
                                    mission_id: str | None = None,
                                    vehicle: str | None = None,
                                    detail: str = "summary",
                                    top_n: int | None = INTREP_SUMMARY_TOP_N) -> dict:
            try:
                return await self._build_intrep(
                    mission_id=mission_id, vehicle=vehicle, since=since,
                    detail=detail, top_n=top_n)
            except ValueError as exc:
                return error("invalid_parameter", str(exc))

        @mcp.tool(name="uav_list_tracks", description="All persistent tracks (M11).")
        async def uav_list_tracks() -> dict:
            from .targets import salute_report
            peers = self.tracks.tracks()
            return {"count": len(peers),
                    "tracks": [salute_report(t, peers=peers) for t in peers]}

        @mcp.tool(name="sim_spawn_target", description=(
            "Sim admin: place a ground-truth target. TOOL_CONTRACT §4.4 calls "
            "this field `class`; the PARAMETER is spelled `ob_class` because "
            "`class` is a Python keyword and cannot be a parameter name, and "
            "the RETURN publishes it under BOTH `class` and `ob_class` so "
            "either spelling reads correctly. It is an order-of-battle library "
            "key (uav_list_ob_classes lists them) and is validated — an "
            "unknown key is refused, never spawned as an "
            "unclassified blob. `mesh`/`name` remain accepted for the legacy "
            "path. The altitude is MSL (alt_msl_m; `alt_m` is the legacy "
            "spelling of the same datum) and DEFAULTS TO THE THEATER GROUND "
            "ELEVATION, never 0 — a 0 default buries targets ~1550 m "
            "underground in the shipped theaters. Pass mobile_route "
            "[{lat, lon, alt_msl_m?}] to drive it (see sim_move_target)."))
        async def sim_spawn_target(lat: float, lon: float,
                                   ob_class: str | None = None,
                                   mesh: str | None = None,
                                   name: str | None = None,
                                   alt_msl_m: float | None = None,
                                   alt_m: float | None = None,
                                   heading_deg: float = 0.0,
                                   mobile_route: list | None = None,
                                   speed_mps: float = 8.0,
                                   loop: bool = False,
                                   idempotency_key: str | None = None) -> dict:
            replay = self._idem_replay("sim_spawn_target", idempotency_key)
            if replay is not None:
                return replay
            from .targets import OB_LIBRARY, match_ob
            if ob_class is not None and ob_class not in OB_LIBRARY:
                return error("unknown_ob_class",
                             f"{ob_class!r} is not an order-of-battle library "
                             f"key; known keys: {sorted(OB_LIBRARY)}")
            label = name or ob_class or mesh
            if not label:
                return error("missing_parameter",
                             "give ob_class (preferred), or name/mesh for the "
                             "legacy path")
            if name is None:
                self._target_seq = getattr(self, "_target_seq", 0) + 1
                label = f"{ob_class or mesh}_{self._target_seq}"
            asset = mesh or ob_class or label
            # The default is the ground under THIS point — measured terrain when
            # the real-world layer has it, the theater's hand-entered elevation
            # when it does not, and never 0 (§4.4). Which one it was travels
            # with the target: a site sitting on an assumed plane 200 m off the
            # real hillside is a fact the operator has to be able to see.
            ground = self.ground_msl_at(float(lat), float(lon))
            try:
                explicit = (alt_msl_m is not None or alt_m is not None)
                alt_msl = (float(_one_altitude("sim_spawn_target altitude",
                                               alt_msl_m=alt_msl_m, alt_m=alt_m))
                           if explicit else float(ground["alt_msl_m"]))
            except ValueError as exc:
                return error("invalid_parameter", str(exc))
            alt_provenance = ({"alt_source": "caller", "alt_is_real": False,
                               "alt_reason": ("the caller supplied the altitude; "
                                              "the server did not measure it"),
                               "terrain_default_msl_m": ground["alt_msl_m"],
                               "terrain_default_source": ground["alt_source"]}
                              if explicit else
                              {k: v for k, v in ground.items() if k != "terrain"})
            fix = canonical_altitude(alt_msl, lat, lon, datum="msl")
            spawned = await self.backend.spawn_object(
                label, asset, lat, lon, fix.alt_hae, heading_deg=heading_deg)
            entry, evidence = match_ob(ob_class or label)
            record = {
                "target_id": label, "name": label, "mesh": asset,
                # §4.4 names this slot `class`; `ob_class` is the same value
                # under the spelling the parameter had to use.
                "class": entry.key,
                "ob_class": entry.key, "category": entry.category,
                "class_evidence": evidence,
                "lat": lat, "lon": lon,
                "alt_msl_m": alt_msl, "alt_hae_m": round(fix.alt_hae, 3),
                "undulation_m": round(fix.undulation_m, 3),
                "datum_source": fix.source,
                "heading_deg": float(heading_deg),
                "spawned_at": time.time(), "route": None,
                **alt_provenance,
            }
            self.targets[label] = record
            self.store.log_audit("spawn_target", f"{label} @ {lat},{lon}",
                                 name=label, mesh=asset, ob_class=entry.key,
                                 alt_msl_m=alt_msl,
                                 alt_hae_m=round(fix.alt_hae, 3),
                                 alt_source=alt_provenance["alt_source"],
                                 alt_is_real=alt_provenance["alt_is_real"])
            out = {"spawned": spawned, "target_id": label, "name": label,
                   # §4.4 spelling first, legacy alias alongside it.
                   "class": entry.key,
                   "ob_class": entry.key, "category": entry.category,
                   "class_evidence": evidence,
                   "alt_msl_m": alt_msl, "alt_hae_m": round(fix.alt_hae, 3),
                   "heading_deg": float(heading_deg),
                   **alt_provenance,
                   "resource": "uav://targets", "status": "accepted"}
            if mobile_route:
                moved = await self._move_target(label, mobile_route,
                                                float(speed_mps), bool(loop))
                out["mobile_route"] = moved
                if moved.get("error"):
                    return moved
            return self._idem_record("sim_spawn_target", idempotency_key, out)

        @mcp.tool(name="sim_move_target", description=(
            "Drive a spawned target along waypoints [{lat, lon, alt_msl_m?}] "
            "at speed_mps (M17). The object then carries real motion, so "
            "Track.update derives a genuine course and speed instead of a "
            "stationary fix."))
        async def sim_move_target(target_id: str, waypoints: list,
                                  speed_mps: float = 8.0, loop: bool = False,
                                  idempotency_key: str | None = None) -> dict:
            replay = self._idem_replay("sim_move_target", idempotency_key)
            if replay is not None:
                return replay
            out = await self._move_target(target_id, waypoints, float(speed_mps),
                                          bool(loop))
            if out.get("error"):
                return out
            return self._idem_record("sim_move_target", idempotency_key, out)

        @mcp.tool(name="sim_set_time", description=(
            "Set the sim clock (M6). `datetime` is 'YYYY-MM-DD HH:MM:SS' LOCAL "
            "SOLAR time at the theater home, so 12:00:00 is midday in every "
            "theater; clock_speed is the celestial multiplier (0 freezes the "
            "sun). Returns the resulting SUN POSITION, which is what the "
            "sun-side orbit rule in uav_orbit_poi plans against."))
        async def sim_set_time(datetime: str | None = None,
                               clock_speed: float = 1.0, enabled: bool = True,
                               vehicle: str = "Drone1",
                               idempotency_key: str | None = None) -> dict:
            replay = self._idem_replay("sim_set_time", idempotency_key)
            if replay is not None:
                return replay
            try:
                await self.backend.set_time(datetime, float(clock_speed),
                                            bool(enabled))
            except Exception as exc:  # noqa: BLE001 — reported, never ignored
                return error("sim_set_time_failed", f"{type(exc).__name__}: {exc}")
            sun = await self.backend.sun(vehicle)
            env = await self.backend.environment()
            out = {"ok": True, "status": "accepted", "requested": datetime,
                   "clock_speed": float(clock_speed), "enabled": bool(enabled),
                   "sim_time": (env or {}).get("sim_time"), "sun": sun}
            if sun is None:
                out["sun_note"] = ("this sim publishes no sun model, so the "
                                   "M6 sun-side orbit rule cannot be applied")
            self.store.log_audit("sim_time", f"time={datetime} x{clock_speed}",
                                 sim_time=(env or {}).get("sim_time"),
                                 sun_elevation_deg=(sun or {}).get("elevation_deg"))
            return self._idem_record("sim_set_time", idempotency_key, out)

        @mcp.tool(name="sim_set_weather", description=(
            "Set obscurants (rain/snow/fog/dust, each 0..1) and the wind "
            "vector in m/s NED. Weather degrades the sensor (M18: shorter "
            "detection range, more false negatives); the wind feeds the fuel "
            "model through the headwind component on each leg (M15). "
            "source='real' takes EVERY value from the theater's actual current "
            "weather instead of the caller (Open-Meteo via the God's Eye View "
            "proxy, hydrated by sim_hydrate_real_data) and is REFUSED — never "
            "silently downgraded to a fabricated dead calm — when that "
            "observation is missing or degraded."))
        async def sim_set_weather(rain: float | None = None,
                                  snow: float | None = None,
                                  fog: float | None = None,
                                  dust: float | None = None,
                                  wind_north_mps: float | None = None,
                                  wind_east_mps: float | None = None,
                                  wind_down_mps: float | None = None,
                                  source: str = "operator",
                                  idempotency_key: str | None = None) -> dict:
            replay = self._idem_replay("sim_set_weather", idempotency_key)
            if replay is not None:
                return replay
            if source not in WEATHER_SOURCES:
                return error("invalid_parameter",
                             f"source={source!r} is not one of "
                             f"{list(WEATHER_SOURCES)}")
            if source == "real":
                supplied = [k for k, v in
                            (("rain", rain), ("snow", snow), ("fog", fog),
                             ("dust", dust), ("wind_north_mps", wind_north_mps),
                             ("wind_east_mps", wind_east_mps),
                             ("wind_down_mps", wind_down_mps)) if v is not None]
                if supplied:
                    return error(
                        "invalid_parameter",
                        f"source='real' takes every value from the observation, "
                        f"but {supplied} were also supplied; drop them rather "
                        "than have the server decide which wins")
                applied = await self.apply_real_weather()
                if applied.get("error"):
                    return applied
                env = await self.backend.environment()
                wind_ne, wind_source = await self.backend.wind_ne()
                out = {"ok": True, "status": "accepted", "source": "real",
                       "weather_is_real": True,
                       "weather": applied["weather"],
                       "wind_ne_mps": [round(wind_ne[0], 2), round(wind_ne[1], 2)],
                       "wind_source": wind_source,
                       "real_weather": applied,
                       "sim_weather": (env or {}).get("weather"),
                       "detection_range_m": (env or {}).get("detection_range_m"),
                       "effects": applied["effects"]}
                return self._idem_record("sim_set_weather", idempotency_key, out)
            values = {k: float(v) for k, v in
                      (("rain", rain), ("snow", snow), ("fog", fog), ("dust", dust))
                      if v is not None}
            for key, val in values.items():
                if not (0.0 <= val <= 1.0):
                    return error("invalid_parameter",
                                 f"{key}={val!r} must be in [0, 1]")
            try:
                if values:
                    await self.backend.set_weather(values)
                if (wind_north_mps is not None or wind_east_mps is not None
                        or wind_down_mps is not None):
                    await self.backend.set_wind(float(wind_north_mps or 0.0),
                                                float(wind_east_mps or 0.0),
                                                float(wind_down_mps or 0.0))
            except Exception as exc:  # noqa: BLE001 — reported, never ignored
                return error("sim_set_weather_failed",
                             f"{type(exc).__name__}: {exc}")
            env = await self.backend.environment()
            wind_ne, wind_source = await self.backend.wind_ne()
            out = {"ok": True, "status": "accepted", "source": "operator",
                   "weather": values,
                   "wind_ne_mps": [round(wind_ne[0], 2), round(wind_ne[1], 2)],
                   "wind_source": wind_source,
                   "weather_is_real": False,
                   "weather_note": ("OPERATOR-SUPPLIED weather, not an "
                                    "observation of the real theater; call with "
                                    "source='real' for the measured one"),
                   "sim_weather": (env or {}).get("weather"),
                   "detection_range_m": (env or {}).get("detection_range_m"),
                   "effects": ("M18: obscurants shorten detection range and "
                               "raise the false-negative rate; M15: the wind "
                               "vector is priced into every leg's burn")}
            self.store.log_audit("environment", f"weather {values}", **out["weather"],
                                 wind_ne_mps=out["wind_ne_mps"])
            return self._idem_record("sim_set_weather", idempotency_key, out)

        # ---- real-world data (REAL_DATA_INTEGRATION.md) ----

        @mcp.tool(name="uav_real_data_status", description=(
            "What this server actually KNOWS about the real world and what it "
            "is assuming: whether the real-data layer is on, which feeds "
            "(terrain / installations / air traffic / weather) are measured and "
            "which are degraded and why, the required upstream attribution, and "
            "the terrain floor under the geofence. Read this before trusting an "
            "alt_agl_m or a uav_los_check: both fall back to the launch datum "
            "and a geometric horizon, visibly, when terrain is unavailable."))
        async def uav_real_data_status(include_feeds: bool = False) -> dict:
            out = self.real_data_status()
            out["geofence_floor"] = self.terrain_floor()
            if include_feeds and self.real_world is not None:
                out["real_data"] = self.real_world.as_dict()
            elif include_feeds:
                out["real_data"] = None
            return out

        @mcp.tool(name="sim_hydrate_real_data", description=(
            "Ingest the real world for this theater: bare-earth terrain, mapped "
            "military sites, live air traffic and current weather. BLOCKS on "
            "the network, so it runs on a worker thread and is a startup / "
            "between-missions call, never a per-tick one. Nothing in the static "
            "theater table is rewritten — a measured ground that disagrees with "
            "the hand-entered one is REPORTED as terrain_delta_m, not applied "
            "under a running geofence. It also loads the DEM for the whole AO "
            "(load_ao_terrain, coarsened by terrain_grid_m), which is what lets "
            "every later telemetry tick and mission call read MEASURED terrain "
            "from memory without touching the network. apply_weather=true "
            "additionally pushes the measured wind and obscurants into the sim, "
            "which is what makes the fuel model burn against real wind (M15)."))
        async def sim_hydrate_real_data(apply_weather: bool = False,
                                        allow_network: bool = True,
                                        background: bool = False,
                                        load_ao_terrain: bool = True,
                                        terrain_grid_m: float | None = None,
                                        idempotency_key: str | None = None) -> dict:
            replay = self._idem_replay("sim_hydrate_real_data", idempotency_key)
            if replay is not None:
                return replay
            if not self.real_data_enabled:
                return error(
                    "real_data_disabled",
                    "the real-world data layer is OFF on this server. Build it "
                    f"with real_data=True (or a client), or set {REAL_DATA_ENV}=1. "
                    "It is opt-in so CI and the offline path never need a network.",
                    retryable=False, status=self.real_data_status())
            if background:
                self.start_real_data()
                return self._idem_record(
                    "sim_hydrate_real_data", idempotency_key,
                    {"ok": True, "status": "accepted", "background": True,
                     "note": ("hydration and refresh run on a daemon thread; poll "
                              "uav_real_data_status for the result"),
                     "real_data": self.real_data_status()})
            try:
                status = await asyncio.to_thread(self.hydrate_real_data,
                                                 allow_network=allow_network,
                                                 load_ao_terrain=load_ao_terrain,
                                                 terrain_grid_m=terrain_grid_m)
            except Exception as exc:  # noqa: BLE001 — surfaced, never swallowed
                return error("real_data_hydration_failed",
                             f"{type(exc).__name__}: {exc}", retryable=True,
                             status=self.real_data_status())
            out: dict = {"ok": True, "status": "accepted", "background": False,
                         "real_data": status,
                         "geofence_floor": self.terrain_floor()}
            if apply_weather:
                out["weather"] = await self.apply_real_weather()
            return self._idem_record("sim_hydrate_real_data", idempotency_key, out)

        @mcp.tool(name="uav_deconflict_airspace", description=(
            "Real aircraft inside the separation box around the vehicle (or "
            "around an explicit point), nearest first — the ADS-B/OpenSky "
            "picture for the AO, not targets (M14 ISR-only). A contact whose "
            "altitude is unknown counts as NOT vertically separated, which is "
            "the only safe reading. An EMPTY list is an empty FEED, never a "
            "guarantee of clear airspace: check `traffic_is_real`."))
        async def uav_deconflict_airspace(vehicle: str = "Drone1",
                                          lat: float | None = None,
                                          lon: float | None = None,
                                          alt_hae_m: float | None = None,
                                          horizontal_m: float | None = None,
                                          vertical_m: float | None = None) -> dict:
            traffic = self.real_traffic()
            if traffic is None:
                return error(
                    "real_traffic_unavailable",
                    "no hydrated air-traffic picture for this theater; call "
                    "sim_hydrate_real_data first", retryable=True,
                    status=self.real_data_status())
            at_lat, at_lon, at_alt = lat, lon, alt_hae_m
            observer: dict = {"source": "explicit"}
            if at_lat is None or at_lon is None:
                try:
                    tele = await self._telemetry(vehicle)
                except Exception as exc:  # noqa: BLE001 — reported, never guessed
                    return error("telemetry_unavailable",
                                 f"{type(exc).__name__}: {exc}", retryable=True)
                at_lat, at_lon = tele["lat"], tele["lon"]
                at_alt = tele["alt_hae_m"] if at_alt is None else at_alt
                observer = {"source": "vehicle", "vehicle": vehicle,
                            "lat": at_lat, "lon": at_lon,
                            "alt_hae_m": tele["alt_hae_m"]}
            from .realdata import DECONFLICT_HORIZONTAL_M, DECONFLICT_VERTICAL_M
            # A separation box of 0 (or a negative one) is not a tight box: it
            # is the deconfliction switched OFF, and it comes back as the same
            # `count: 0, traffic_is_real: true` a genuinely clear sky does. A
            # parameter whose value silently disables the check it configures is
            # refused rather than honoured; `horizontal_m` filters contacts by
            # `horizontal > box`, so 0 excludes every contact that is not at the
            # exact same coordinate, and `vertical_m` = 0 declares every contact
            # with any height difference at all "separated".
            for name, raw in (("horizontal_m", horizontal_m),
                              ("vertical_m", vertical_m)):
                if raw is None:
                    continue
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    return error("invalid_parameter",
                                 f"{name}={raw!r} is not a number")
                if math.isnan(val) or val <= 0.0:
                    return error(
                        "invalid_parameter",
                        f"{name}={raw!r} must be > 0. A separation box of "
                        "zero or less reports an empty conflict list for every "
                        "contact in the feed, which is indistinguishable from "
                        "clear airspace; omit the parameter for the default "
                        f"({DECONFLICT_HORIZONTAL_M:.0f} m x "
                        f"{DECONFLICT_VERTICAL_M:.0f} m).")
            box_h = (DECONFLICT_HORIZONTAL_M if horizontal_m is None
                     else float(horizontal_m))
            box_v = (DECONFLICT_VERTICAL_M if vertical_m is None
                     else float(vertical_m))
            conflicts = traffic.deconflict(float(at_lat), float(at_lon),
                                           None if at_alt is None else float(at_alt),
                                           horizontal_m=box_h, vertical_m=box_v)
            return {
                "vehicle": vehicle, "observer": observer,
                "separation_box": {"horizontal_m": box_h, "vertical_m": box_v},
                "conflicts": conflicts, "count": len(conflicts),
                "traffic_is_real": traffic.real,
                "contacts_in_feed": len(traffic.contacts),
                "traffic": traffic.as_dict(),
                "isr_only": ("M14: these are airspace contacts to deconflict "
                             "against. No engagement recommendation is produced."),
            }

        @mcp.tool(name="sim_spawn_order_of_battle", description=(
            "Spawn ground truth from the theater's MAPPED military sites "
            "(OpenStreetMap/Overpass via the God's Eye View proxy), each at the "
            "measured terrain height under it. This is MAPPED, INCOMPLETE, "
            "UNVERIFIED data used as ISR context (M14) — it is not an "
            "authoritative order of battle and every spawned site says so. "
            "Requires sim_hydrate_real_data first."))
        async def sim_spawn_order_of_battle(limit: int | None = None,
                                            categories: list | None = None,
                                            idempotency_key: str | None = None) -> dict:
            replay = self._idem_replay("sim_spawn_order_of_battle", idempotency_key)
            if replay is not None:
                return replay
            order = self.real_order_of_battle()
            if order is None:
                return error(
                    "real_ob_unavailable",
                    "no hydrated order of battle for this theater; call "
                    "sim_hydrate_real_data first", retryable=True,
                    status=self.real_data_status())
            from .realdata import MAPPED_DATA_CAVEAT
            # `limit=0` spawned nothing and answered `ok: true, spawned: 0` —
            # the same shape a theater with no mapped sites returns. Omit the
            # parameter for "all of them"; a cap of zero is refused rather than
            # silently turning the tool into a no-op that reports success.
            if limit is not None:
                try:
                    lim = int(limit)
                except (TypeError, ValueError):
                    return error("invalid_parameter",
                                 f"limit={limit!r} is not an integer")
                if lim < 1:
                    return error(
                        "invalid_parameter",
                        f"limit={limit!r} must be >= 1. A cap of zero or less "
                        "spawns nothing and reports success, which reads the "
                        "same as an empty order of battle; omit limit to spawn "
                        "every mapped site.")
            wanted = None if not categories else {str(c) for c in categories}
            spawned: list[dict] = []
            refused: list[dict] = []
            for site in order.sites:
                if wanted is not None and site.category not in wanted:
                    continue
                if limit is not None and len(spawned) >= int(limit):
                    break
                request = site.spawn_request()
                out = await sim_spawn_target(**request)
                row = {"osm_id": site.osm_id, "name": site.name,
                       "category": site.category, "ob_class": site.ob_class,
                       "ob_source": site.ob_source,
                       "position_source": site.position_source,
                       "alt_provenance": site.alt_provenance(), "result": out}
                (refused if out.get("error") else spawned).append(row)
            result = {
                "ok": not refused, "status": "accepted",
                "spawned": len(spawned), "refused": len(refused),
                "sites": spawned, "refusals": refused,
                "by_category": order.by_category(),
                "ob_is_real": order.real,
                "mapped_data": True, "authoritative": False,
                "caveat": MAPPED_DATA_CAVEAT,
                "provenance": order.provenance.as_dict(),
            }
            return self._idem_record("sim_spawn_order_of_battle",
                                     idempotency_key, result)

        @mcp.tool(name="sim_set_link_state", description=(
            "Force a vehicle's datalink to nominal | degraded | lost for "
            "duration_s seconds (0 = until cleared). This is what exercises "
            "the lost-link plan (M9): degraded serves stale telemetry, lost "
            "takes telemetry, imagery and contacts with it."))
        async def sim_set_link_state(vehicle: str, state: str = "lost",
                                     duration_s: float = 0.0,
                                     idempotency_key: str | None = None) -> dict:
            replay = self._idem_replay("sim_set_link_state", idempotency_key)
            if replay is not None:
                return replay
            if state not in LINK_STATES:
                return error("invalid_parameter",
                             f"state must be one of {list(LINK_STATES)}, "
                             f"got {state!r}")
            try:
                res = await self.backend.set_link_state(vehicle, state,
                                                        float(duration_s))
            except Exception as exc:  # noqa: BLE001 — fake-only RPC on real AirSim
                return error("link_state_unsupported",
                             f"this sim has no simSetLinkState: "
                             f"{type(exc).__name__}: {exc}")
            out = {"ok": True, "status": "accepted", "vehicle": vehicle,
                   "link": {k: _get(res, k) for k in
                            ("vehicle", "state", "remaining_s")},
                   "lost_link_plan": self.monitor_for(vehicle).link.plan.to_dict()}
            self.store.log_audit("sim_link_state", f"{vehicle} -> {state}",
                                 vehicle=vehicle, state=state,
                                 duration_s=float(duration_s))
            return self._idem_record("sim_set_link_state", idempotency_key, out)

        @mcp.tool(name="sim_set_gps_degradation", description=(
            "Degrade the GPS (M16/M17): error_m adds gaussian jitter to the "
            "reported fix, denied=True FREEZES it at the last good position. "
            "This is a sim-wide setting in the UE-free path, not per vehicle — "
            "the return says so rather than implying per-vehicle isolation."))
        async def sim_set_gps_degradation(vehicle: str | None = None,
                                          error_m: float | None = None,
                                          denied: bool | None = None,
                                          idempotency_key: str | None = None) -> dict:
            replay = self._idem_replay("sim_set_gps_degradation", idempotency_key)
            if replay is not None:
                return replay
            sim = getattr(self.backend, "sim", None)
            if sim is None:
                return error("gps_degradation_unsupported",
                             "GPS denial/noise is a fake-sim hook; on real "
                             "AirSim it is configured through sensor settings, "
                             "so no degradation was applied")
            if error_m is None and denied is None:
                return error("missing_parameter",
                             "give error_m and/or denied")
            state: dict[str, Any] = {}
            if denied is not None:
                sim.set_gps_denied(bool(denied))
                state["gps_denied"] = bool(denied)
            if error_m is not None:
                if float(error_m) < 0.0:
                    return error("invalid_parameter", "error_m must be >= 0")
                sim.set_gps_noise(float(error_m))
                state["gps_noise_m"] = float(error_m)
            out = {"ok": True, "status": "accepted", **state,
                   "scope": "sim-wide (all vehicles)", "vehicle_requested": vehicle}
            self.store.log_audit("gps_degradation", f"{state}", **state,
                                 vehicle=vehicle)
            return self._idem_record("sim_set_gps_degradation", idempotency_key, out)

        @mcp.tool(name="sim_reset", description=(
            "Reset the simulated vehicles to their start state. The TRACK "
            "STORE and the PATTERN-OF-LIFE baselines SURVIVE (M12) — the "
            "return reports how many of each were retained, and the sim epoch "
            "is bumped so tracks from before the reset stay distinguishable."))
        async def sim_reset(idempotency_key: str | None = None) -> dict:
            replay = self._idem_replay("sim_reset", idempotency_key)
            if replay is not None:
                return replay
            try:
                await self.backend.reset()
            except Exception as exc:  # noqa: BLE001 — reported, never ignored
                return error("sim_reset_failed", f"{type(exc).__name__}: {exc}")
            tracks = self.tracks.mark_sim_reset()
            pol = self.pol.record_sim_reset()
            for t in self.tracks.tracks():
                self.store.tracks.put(t.to_dict())
            record = {"ok": True, "status": "accepted",
                      "tracks_retained": tracks["tracks_retained"],
                      "sim_epoch": tracks["sim_epoch"],
                      "pois_retained": pol["pois_retained"],
                      "sim_resets": pol["sim_resets"],
                      "targets_retained": len(self.targets),
                      "persistence": ("M12: sim_reset does NOT wipe the track "
                                      "store or the pattern-of-life baselines"),
                      # Terrain does not move and mapped sites do not move: the
                      # ingested world is a property of the PLACE, not of the
                      # sim run, so a reset keeps it. Said explicitly because
                      # "did my AGL just go back to the launch datum?" must not
                      # be a question the harness has to guess at.
                      "real_data_retained": self.real_data_status(),
                      "at": tracks["at"]}
            self.sim_resets.append(record)
            self.store.log_audit("sim_reset", "vehicles reset; intel retained",
                                 tracks_retained=record["tracks_retained"],
                                 pois_retained=record["pois_retained"],
                                 sim_epoch=record["sim_epoch"])
            return self._idem_record("sim_reset", idempotency_key, record)

        @mcp.tool(name="uav_list_ob_classes", description=(
            "The order-of-battle library keys sim_spawn_target and the threat "
            "model use (PLAN §4.6a): key, name, category, role, engagement "
            "envelope and typical unit size."))
        async def uav_list_ob_classes() -> dict:
            from .targets import OB_LIBRARY
            return {"count": len(OB_LIBRARY),
                    "classes": [c.to_dict() for c in OB_LIBRARY.values()]}

        @mcp.tool(name="uav_assess_threat", description=(
            "Deterministic order-of-battle threat assessment of tracks (M13): "
            "envelope math vs the observer, intent from movement AND the "
            "pattern-of-life store (M12). ISR-only (M14). Optionally pass "
            "defended assets [{lat,lon,name}] and an area_polygon to scope it."))
        async def uav_assess_threat(vehicle: str, track_id: str | None = None,
                                    defended: list[dict] | None = None,
                                    area_polygon: list | None = None) -> dict:
            from .threat import assess_area, assess_track
            tele = await self._telemetry(vehicle)
            observer = self._observer(tele)
            if track_id:
                t = self.tracks.get(track_id)
                if not t:
                    return {"error": f"unknown track {track_id}"}
                out = assess_track(t, observer, defended, self.pol)
                self.store.log_audit("threat_assess", f"{track_id} {out['threat_level']}",
                                     track_id=track_id, level=out["threat_level"],
                                     score=out.get("threat_score"))
                return out
            try:
                polygon = latlon_polygon(
                    area_polygon if area_polygon is not None else self.envelope.geofence,
                    "area_polygon")
            except ValueError as exc:
                return error("bad_polygon", str(exc))
            out = assess_area(self.tracks.tracks(), observer, defended, self.pol,
                              area_polygon=polygon)
            self.store.log_audit("threat_assess_area",
                                 f"highest {out['highest_threat']}",
                                 vehicle=vehicle, count=out["count"])
            return out

        @mcp.tool(name="uav_handoff_target", description=(
            "Coordinate a track handoff to another UAV (M10): receiver must have "
            "fuel + be in range; standoff derived from the weapon envelope (M5). "
            "On accept, queues an orbit mission for the receiver."))
        async def uav_handoff_target(track_id: str, from_vehicle: str,
                                     to_vehicle: str,
                                     idempotency_key: str | None = None) -> dict:
            # Same implementation as mission_handoff_track (§4.3); this name is
            # kept because the GEV panel calls it.
            return await self._mission_handoff_track(
                track_id=track_id, from_vehicle=from_vehicle,
                to_vehicle=to_vehicle, idempotency_key=idempotency_key)

        @mcp.tool(name="sim_set_environment", description=(
            "Set simulation realism (PLAN Phase 6): wind_north/east/down m/s (M15), "
            "gps_denied bool to freeze the GPS fix (M16), gps_noise_m gaussian jitter (M17)."))
        async def sim_set_environment(wind_north: float = 0.0, wind_east: float = 0.0,
                                      wind_down: float = 0.0, gps_denied: bool | None = None,
                                      gps_noise_m: float | None = None,
                                      det_false_neg: float | None = None,
                                      det_false_pos: float | None = None,
                                      idempotency_key: str | None = None) -> dict:
            replay = self._idem_replay("sim_set_environment", idempotency_key)
            if replay is not None:
                return replay
            client = self.backend.client
            loop = asyncio.get_running_loop()
            import airsim as _airsim
            await loop.run_in_executor(
                None, lambda: client.simSetWind(
                    _airsim.Vector3r(wind_north, wind_east, wind_down)))
            # Remember what we commanded so the fuel model still has a wind
            # vector when the sim cannot be asked for its own (M15). The source
            # is always reported with the value.
            self.backend.commanded_wind_ne = (float(wind_north), float(wind_east))
            state = {"wind_north": wind_north, "wind_east": wind_east, "wind_down": wind_down}
            # GPS denial / noise are fake-sim realism hooks (real AirSim handles
            # them via sensor settings); reach the fake directly when present.
            sim = getattr(self.backend, "sim", None)
            if sim is not None:
                if gps_denied is not None:
                    sim.set_gps_denied(gps_denied)
                    state["gps_denied"] = gps_denied
                if gps_noise_m is not None:
                    sim.set_gps_noise(gps_noise_m)
                    state["gps_noise_m"] = gps_noise_m
                if det_false_neg is not None or det_false_pos is not None:
                    sim.set_detection_realism(det_false_neg or 0.0, det_false_pos or 0.0)
                    state["det_false_neg"] = det_false_neg
                    state["det_false_pos"] = det_false_pos
            self.store.log_audit("environment", f"wind N{wind_north} E{wind_east}", **state)
            return self._idem_record("sim_set_environment", idempotency_key,
                                     {"ok": True, "status": "accepted", **state})

        @mcp.tool(name="uav_task_status", description=(
            "Status of one vehicle's queue or a task_id. A task_id is resolvable "
            "for its whole lifetime, queued window included (T4a). Progress is "
            "derived server-side from telemetry and is also pushed over "
            "notifications/progress when the caller supplies a progressToken."))
        async def uav_task_status(vehicle: str, task_id: str | None = None,
                                  ctx: Context | None = None) -> dict:
            q = self.tasking.queue_for(vehicle)
            fm = self.fuel_for(vehicle)
            if task_id:
                t = q.get(task_id)
                if not t:
                    return {"error": "unknown task_id", "task_id": task_id}
                out = t.handle()
                if ctx is not None:
                    await ctx.report_progress(t.progress_pct, 100.0,
                                              t.progress_note or t.state.value)
                    out["progress_notified"] = True
                out["recovery"] = t.recovery
                out["fuel_pct"] = round(fm.fuel_pct, 2)
                out["bingo_latched"] = fm.bingo.tripped
                out["mission_status"] = self.mission_flags.get(vehicle)
                out["mission"] = self.missions.get(t.mission_id or "")
                return out
            st = self.tasking.status(vehicle)
            st["fuel_pct"] = round(fm.fuel_pct, 2)
            st["bingo_latched"] = fm.bingo.tripped
            st["mission_status"] = self.mission_flags.get(vehicle)
            st["last_tick"] = self.ticks.get(vehicle)
            if ctx is not None and q.current is not None:
                await ctx.report_progress(q.current.progress_pct, 100.0,
                                          q.current.progress_note or q.current.state.value)
                st["progress_notified"] = True
            return st

    # ---- §4.8 resources (R6: the server exposed ZERO of these) ----
    def _register_resources(self) -> None:
        mcp = self.mcp

        @mcp.resource("uav://{vehicle}/telemetry", mime_type="application/json",
                      name="telemetry", description=(
                          "Live telemetry for one vehicle: lat/lon with "
                          "alt_hae_m + alt_msl_m + alt_agl_m (T1, one "
                          "conversion point), attitude, velocity, wind, "
                          "fuel_pct, bingo_fuel_pct, link and queue state."))
        async def telemetry_resource(vehicle: str) -> dict:
            return await self._telemetry_payload(vehicle)

        @mcp.resource("uav://{vehicle}/camera/{name}/{type}",
                      mime_type="image/png", name="camera_frame",
                      description=(
                          "The most recent frame from one camera as PNG bytes; "
                          "a fresh frame is captured if none has been taken "
                          "yet. type = scene | depth | segmentation | infrared. "
                          "The frame's geo pose and sun angle come back from "
                          "uav_capture_image."))
        async def camera_resource(vehicle: str, name: str, type: str) -> bytes:
            key = (type or "scene").lower()
            if key not in IMAGE_TYPES:
                raise ValueError(
                    f"unknown image type {type!r}; known types: "
                    f"{sorted(IMAGE_TYPES)}")
            cached = self.frames.get((vehicle, name, key))
            if cached is None:
                await self._capture_frame(vehicle, name, key)
                cached = self.frames[(vehicle, name, key)]
            return cached["png"]

        @mcp.resource("uav://mission/{id}", mime_type="application/json",
                      name="mission", description=(
                          "One mission: the plan that was gated, the doctrine "
                          "it was derived from, its live state/progress and "
                          "its journal entries."))
        async def mission_resource(id: str) -> dict:
            rec = self.missions.get(id)
            if rec is None:
                raise ValueError(
                    f"no mission {id!r}; known missions: {sorted(self.missions)}")
            return {"mission": {k: v for k, v in rec.items()},
                    "status": self._mission_state(id),
                    "journal": self.store.mission_events(id)}

        @mcp.resource("uav://tracks", mime_type="application/json",
                      name="tracks", description=(
                          "The persistent track store (M11): every contact "
                          "with its durable track_id, order-of-battle class "
                          "and confidence. Survives sim_reset (M12)."))
        async def tracks_resource() -> dict:
            from .targets import assess_confidence
            tracks = self.tracks.tracks()
            return {
                "count": len(tracks),
                "sim_epoch": self.tracks.sim_epoch,
                "origin": self.tracks.origin,
                "rejected_detections": len(self.tracks.rejected),
                "tracks": [{**t.to_dict(),
                            "confidence": assess_confidence(t)} for t in tracks],
            }

        @mcp.resource("uav://targets", mime_type="application/json",
                      name="targets", description=(
                          "Sim GROUND TRUTH: what sim_spawn_target actually "
                          "placed, with its order-of-battle class and its "
                          "route if it is moving. This is deliberately NOT the "
                          "track store — uav://tracks is what the sensor "
                          "derived, this is what is really there."))
        async def targets_resource() -> dict:
            live = []
            for name, rec in self.targets.items():
                row = dict(rec)
                try:
                    pos = await self.backend.object_pose(name)
                except Exception as exc:  # noqa: BLE001 — reported per target
                    pos, row["position_error"] = None, f"{type(exc).__name__}: {exc}"
                if pos is not None:
                    fix = canonical_altitude(pos[2], pos[0], pos[1], datum="hae")
                    row["current"] = {"lat": round(pos[0], 7),
                                      "lon": round(pos[1], 7),
                                      "alt_hae_m": round(fix.alt_hae, 3),
                                      "alt_msl_m": round(fix.alt_msl, 3)}
                else:
                    row["current"] = None
                    row.setdefault("position_note",
                                   "the sim no longer holds this object")
                live.append(row)
            return {"count": len(live), "targets": live,
                    "note": ("ground truth placed into the sim; the ISR-derived "
                             "picture is uav://tracks")}

        @mcp.resource("uav://safety/geofence", mime_type="application/json",
                      name="safety_envelope", description=(
                          "The server-enforced safety envelope: geofence "
                          "polygon, ceiling, min AGL, max speed, home and the "
                          "lost-link plan. PLAN §4.5 says a skill's ROE 'may "
                          "only be stricter' than this, which is impossible to "
                          "honour without being able to read it."))
        async def geofence_resource() -> dict:
            env = self.envelope.to_dict()
            home = self.envelope.home
            home_block = None
            if home:
                fix = canonical_altitude(home[2], home[0], home[1], datum="msl")
                home_block = {"lat": home[0], "lon": home[1],
                              "alt_msl_m": home[2],
                              "alt_hae_m": round(fix.alt_hae, 3),
                              "undulation_m": round(fix.undulation_m, 3),
                              "datum_source": fix.source}
            floor = self.terrain_floor()
            return {
                **env,
                "units": {
                    "altitudes": (
                        "metres AGL, MEASURED above real terrain under the "
                        "aircraft wherever the terrain feed has the ground and "
                        "above the LAUNCH DATUM where it does not — every "
                        "altitude carries alt_agl_is_real saying which it is"
                        if self.real_data_enabled else
                        "metres AGL above the LAUNCH DATUM (the NED origin) — "
                        "no terrain model is in force, so this is true AGL only "
                        "over ground at the home elevation"),
                    "terrain_available": self.real_data_enabled,
                    "agl_is_measured": "per value: read alt_agl_is_real",
                    "speeds": "metres per second, ground speed"},
                # The floor the envelope's min_agl_m implies over the whole AO.
                # Without terrain there was no floor at all: min_agl was checked
                # against the launch datum, so a geofence over a ridge let the
                # aircraft fly into it while reading a comfortable AGL.
                "terrain_floor": floor,
                "real_data": self.real_data_status(),
                "home_datums": home_block,
                "theater": {"id": self.theater.id, "label": self.theater.label,
                            "ao": self.theater.ao_list(),
                            "ground_elevation_msl_m": self.theater.home_alt_msl_m},
                # None unless the enforced envelope was built for a different
                # theater than the one everything theater-derived comes from.
                "theater_mismatch": self.theater_mismatch,
                # T4c: what this process RECOVERED at boot — the per-vehicle
                # resume/abort decision and, under `restored`, the fuel
                # integrators, tracks and pattern-of-life baselines that were
                # read back off the journals. A restart that silently came back
                # with a full tank and an un-tripped BINGO latch is
                # indistinguishable from a fresh launch unless this is readable.
                "restart_recovery": {
                    "decisions": {v: self.recovery.decision_for(v)
                                  for v in self.recovery.vehicles()},
                    "restored": self.boot_restored,
                },
                "lost_link_plan": self.lost_link_plan.to_dict(),
                "bingo": {vehicle: (tick.get("bingo") or {})
                          for vehicle, tick in self.ticks.items()},
                "mission_flags": dict(self.mission_flags),
                "roe": ("PLAN §4.5: a harness/skill ROE may only be STRICTER "
                        "than this envelope. The server enforces these limits "
                        "regardless of what the harness asks for; a BINGO "
                        "force-RTB is un-cancellable (M4/T5)."),
                "isr_only": ("M14: no kinetic tool exists on this server and "
                             "no engagement recommendation is produced."),
            }

        @mcp.resource("uav://reports/{id}", mime_type="application/json",
                      name="report", description=(
                          "A structured report (PLAN §4.7): the INTREP for a "
                          "mission id, or a THREATREP produced by "
                          "mission_threat_assessment. Use 'latest' for the most "
                          "recent INTREP."))
        async def report_resource(id: str) -> dict:
            if id in self.reports:
                return self.reports[id]
            if id in self.missions:
                return await self._build_intrep(mission_id=id)
            raise ValueError(
                f"no report {id!r}; generated reports: {sorted(self.reports)}; "
                f"missions: {sorted(self.missions)}")

        @mcp.resource("uav://pattern-of-life/{poi}", mime_type="application/json",
                      name="pattern_of_life", description=(
                          "The observed baseline at one point of interest "
                          "(M12) and the current deviation from it — the third "
                          "of the four threat intent indicators. Use 'all' for "
                          "every POI."))
        async def pattern_of_life_resource(poi: str) -> dict:
            if poi in ("all", "*"):
                return {"count": len(self.pol.pois()),
                        "min_samples": self.pol.min_samples,
                        "sim_resets": len(self.pol.sim_resets),
                        "pois": [self.pol.get(p).to_dict() for p in self.pol.pois()]}
            base = self.pol.get(poi)
            if base is None:
                raise ValueError(
                    f"no pattern-of-life POI {poi!r}; known: {sorted(self.pol.pois())}")
            return {"poi": poi, "baseline": base.to_dict(),
                    "deviation": self.pol.deviation(poi),
                    "sim_resets": len(self.pol.sim_resets),
                    "persistence": "M12: baselines survive sim_reset"}

    @staticmethod
    def _range_estimate(fm, tele: dict, wind_ne: tuple[float, float]) -> dict:
        """`est_range_km` (TOOL_CONTRACT §4.2), with what it assumes stated.

        Usable fuel is what is left ABOVE the BINGO line, burned at the cruise
        rate with the current headwind — the same `_burn` the integrator uses,
        so the number cannot drift away from the fuel clock.
        """
        from .safety import Phase, headwind_component_mps
        head = headwind_component_mps(wind_ne[0], wind_ne[1], tele.get("track_deg"))
        rate = fm._burn(Phase.CRUISE, 1.0, max(0.0, head))
        usable = max(0.0, tele["fuel_pct"] - tele["bingo_fuel_pct"])
        if rate <= 0.0:
            return {"est_range_km": None, "est_endurance_s": None,
                    "est_range_basis": "cruise burn rate is zero; range is undefined"}
        seconds = usable / rate
        return {
            "est_range_km": round(seconds * fm.rtb_speed_mps / 1000.0, 2),
            "est_endurance_s": round(seconds, 1),
            "usable_fuel_pct": round(usable, 2),
            "est_range_basis": (
                f"{usable:.1f}% usable fuel (above the BINGO line) at the "
                f"cruise burn rate with a {head:.1f} m/s headwind, flown at "
                f"{fm.rtb_speed_mps:.0f} m/s"),
        }

    async def _telemetry_payload(self, vehicle: str) -> dict:
        """The telemetry body shared by the tool and the resource."""
        tele = await self._telemetry(vehicle)
        mon = self.monitor_for(vehicle)
        fm = mon.fuel
        wind_ne, wind_source = await self.backend.wind_ne()
        tele["fuel_pct"] = round(fm.fuel_pct, 2)
        # BINGO is priced from the LAUNCH-DATUM AGL (`commanded_agl`): the
        # return leg lets down to 0 over HOME, so the let-down height is height
        # above home ground, never height above the ground under the aircraft.
        # Handing it HAE inflates the burn by the elevation of home, and handing
        # it the terrain-measured AGL makes the published line disagree with the
        # pre-flight gate that is contractually supposed to match it.
        tele["bingo_fuel_pct"] = round(
            fm.bingo_fuel_pct((tele["lat"], tele["lon"]),
                              self.commanded_agl(tele), wind_ne=wind_ne), 2)
        tele["fuel_ticks"] = fm.ticks
        tele["wind_ne_mps"] = [round(wind_ne[0], 2), round(wind_ne[1], 2)]
        tele["wind_source"] = wind_source
        tele.update(self._range_estimate(fm, tele, wind_ne))
        tele["link"] = mon.link.to_dict()
        tele["mission_status"] = self.mission_flags.get(vehicle)
        tele["bingo"] = fm.bingo.to_dict()
        tele["queue"] = self.tasking.status(vehicle)
        tele["resource"] = f"uav://{vehicle}/telemetry"
        return tele

    # ---- small helpers used by the tools ----
    def _home_latlon(self, vehicle: str) -> tuple[float, float]:
        tele = self._last_tele.get(vehicle)
        if tele:
            return tele["lat"], tele["lon"]
        home = self.envelope.home
        if home:
            return home[0], home[1]
        return self.backend.home_geo.latitude, self.backend.home_geo.longitude

    def _mission_sections(self, mission: dict | None) -> tuple[dict, dict]:
        """INTREP mission-summary + coverage sections from the mission registry.

        The coverage slot is SENSOR coverage of the tasked area, produced by
        `missions.coverage_of_path` — the one coverage producer in the system
        (PLAN §4.7) — from the part of the route the aircraft actually flew.

        It used to be `waypoints_flown / waypoints_planned`, which is mission
        PROGRESS, not imaged ground. The two disagree badly (a grid reported
        11.15 % from `mission_grid_search` and a different figure from
        `uav_target_report` for the same mission), and reporting progress as
        coverage is the most damaging error an ISR report can make: the
        consumer reads it as ground cleared and stops looking there. Waypoint
        progress is still published, under its own honest name.
        """
        if not mission:
            return ({"status": "no mission executed", "narrative": None},
                    {"coverage_pct": None,
                     "method": "no mission registered; coverage unknown"})
        task = self.tasking.get(mission["vehicle"], mission["task_id"])
        route = list(mission.get("waypoints") or [])
        planned = len(route) or 1
        flown, flown_basis = self._waypoints_reached(task)
        summary = {
            "mission_id": mission["mission_id"], "kind": mission["kind"],
            "vehicle": mission["vehicle"],
            "started": int(mission["started"]),
            "ended": (int(task.finished_at) if task and task.finished_at else None),
            "duration_s": (round(task.finished_at - mission["started"], 1)
                           if task and task.finished_at else
                           round(time.time() - mission["started"], 1)),
            "area_name": self.theater.label,
            "status": mission.get("status") or (task.state.value if task else mission["state"]),
            "narrative": None,
        }
        summary.update(self._capture_tally(task))
        coverage = self._flown_coverage_section(mission, route, flown, planned)
        coverage.update({
            "waypoints_planned": planned,
            "waypoints_flown": min(flown, planned),
            "waypoints_flown_basis": flown_basis,
            "waypoint_progress_pct": round(100.0 * min(flown, planned) / planned, 1),
            "sweep_spacing_m": (mission.get("meta") or {}).get("sweep_spacing_m"),
        })
        return summary, coverage

    @staticmethod
    def _waypoints_reached(task) -> tuple[int, str]:
        """Waypoints whose leg actually CLOSED, and where that number came from.

        `task.waypoint` is the leg being flown, which is one AHEAD of the last
        confirmed arrival. Scoring coverage on it credits the aircraft with
        imaging ground it is still crossing — the same "reported as cleared
        when it was not" error the coverage slot exists to avoid — so the
        in-flight leg is not counted until it closes.
        """
        if task is None:
            return 0, "no task for this mission"
        res = getattr(task, "result", None) or {}
        if res.get("waypoints_reached") is not None:
            return int(res["waypoints_reached"]), "legs confirmed closed by the flight"
        return (max(0, (task.waypoint or 0) - 1),
                "legs confirmed closed so far; the leg in progress is not credited")

    @staticmethod
    def _capture_tally(task) -> dict:
        """How many of the M2 planned captures were actually taken (§4.7)."""
        res = getattr(task, "result", None) or {}
        if "captures_planned" not in res:
            return {}
        return {"captures_planned": res["captures_planned"],
                "captures_taken": res["captures_taken"],
                "captures_missed": res.get("captures_missed", 0),
                "capture_trigger": res.get("capture_trigger")}

    def _flown_coverage_section(self, mission: dict, route: list,
                                flown: int, planned: int) -> dict:
        """Sensor coverage of the tasked AO by the route flown so far.

        Returns `coverage_pct: None` WITH the reason when the mission has no
        tasked polygon to score against (a recon route, an orbit, a track
        follow): an area-coverage number for a mission that never had an area
        would be fiction, and the old waypoint ratio was exactly that fiction
        wearing the coverage slot's name.
        """
        from .missions import coverage_of_path

        meta = mission.get("meta") or {}
        polygon = (meta.get("_plan_params") or {}).get("polygon")
        planned_cov = mission.get("coverage") or {}
        if not polygon:
            return {
                "planned_area_km2": None, "covered_area_km2": None,
                "coverage_pct": None,
                "method": (f"a {mission['kind']!r} mission is not tasked with an "
                           "area, so there is no ground-coverage figure to "
                           "report; see waypoint_progress_pct for how much of "
                           "the route was flown"),
                "basis": "not_applicable",
                "planned_coverage_pct": planned_cov.get("coverage_pct"),
            }
        swath = meta.get("swath_m")
        if not swath:
            return {
                "planned_area_km2": None, "covered_area_km2": None,
                "coverage_pct": None,
                "method": ("the mission plan carries no sensor swath, so the "
                           "imaged ground cannot be computed; it is NOT "
                           "substituted with waypoint progress"),
                "basis": "unknown_swath",
                "planned_coverage_pct": planned_cov.get("coverage_pct"),
            }
        path = [(float(w["lat"]), float(w["lon"]))
                for w in route[:max(0, min(flown, planned))]]
        if len(path) < 2:
            return {
                "planned_area_km2": planned_cov.get("planned_area_km2"),
                "covered_area_km2": 0.0, "coverage_pct": 0.0,
                "method": (f"{len(path)} of {planned} waypoints confirmed flown "
                           "— no leg has been completed, so no ground has been "
                           "imaged"),
                "basis": "flown",
                "swath_m": float(swath),
                "planned_coverage_pct": planned_cov.get("coverage_pct"),
            }
        try:
            cov = coverage_of_path(polygon, path, float(swath), basis="flown")
        except ValueError as exc:
            return {
                "planned_area_km2": None, "covered_area_km2": None,
                "coverage_pct": None,
                "method": f"coverage could not be computed: {exc}",
                "basis": "error",
                "planned_coverage_pct": planned_cov.get("coverage_pct"),
            }
        out = cov.to_dict()
        out["planned_coverage_pct"] = planned_cov.get("coverage_pct")
        out["method"] = (
            f"sensor coverage of the tasked AO by the {len(path)} of {planned} "
            f"waypoints confirmed flown — {out['method']}")
        return out

    async def serve(self, host: str = "127.0.0.1", port: int = 8791) -> None:
        self.start_monitor()
        try:
            await self.mcp.run_streamable_http_async(
                host=host, port=port, streamable_http_path="/mcp", stateless_http=True)
        finally:
            self.stop_monitor()


def _dominant_weather(weather: dict) -> str | None:
    """Turn the sim's weather dict into the one word the INTREP reports."""
    if not weather:
        return None
    worst, value = None, 0.0
    for key in ("rain", "snow", "fog", "dust"):
        v = float(weather.get(key) or 0.0)
        if v > value:
            worst, value = key, v
    if worst is None or value <= 0.0:
        return "clear"
    return f"{worst}:{value:.2f}"
