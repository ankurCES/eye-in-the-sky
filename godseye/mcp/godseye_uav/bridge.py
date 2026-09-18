"""Telemetry bridge: AirSim msgpack-rpc -> REST/SSE for God's Eye View.

PLAN constraints:
- T1: canonical altitude is altHae; the MSL->HAE conversion happens HERE only.
- T3: three loops. A: telemetry <=10 Hz batched. B: camera 2-5 Hz subscriber
      gated (lazy start/stop). C: mission/intel state, on demand + on change.
- T4e: Bearer token + loopback default; msgpack-rpc stays loopback.
- T6: /snapshot (vehicles) and /mission-overlay (separate CustomDataSource).

Feeds served (BRIDGE_CONTRACT.md):
    GET /health          open; `theater` = the ACTIVE theater (see below)
    GET /snapshot        vehicles[] + missions[] + contacts[] + feeds{}
    GET /snapshot/{name} one vehicle
    GET /mission-overlay GeoJSON FeatureCollection, refreshed on mission change
    GET /events          Server-Sent Events, the alarm lane
    GET /theaters        the theaters.py table, plus `active`
    GET /tracks          MCP track store proxy
    GET /camera/{veh}    subscriber-gated PIP frame
    POST /control/*      MCP proxy for the GEV panel (see "read-only" below)

READ-ONLY (BRIDGE_CONTRACT rule 1). Nothing here commands flight. `/control/*`
forwards a body to the MCP server and returns its answer; the bridge adds no
command of its own, and every other route is a pure read of cached state.

WHERE MISSION AND TRACK STATE COMES FROM
----------------------------------------
PLAN 3.1's implementation note says "the bridge already owns mission/track
state - just serialize". That premise is FALSE. `BridgeState` holds an
`AirSimAdapter` and nothing else: there is no `FuelModel`, no `TrackManager`,
no mission registry on this side of the process. The state the contract asks
for lives on `GodseyeUavServer` (`.missions`, `.ticks`, `.tracks`,
`.envelope`).

Two honest ways to get it, and what was chosen:

  (a) Hold a direct reference to the server object. Zero latency, no second
      process to be up. REJECTED as the primary path: `launch.py` builds the
      bridge app from an `AirSimAdapter` alone and never sees a reason to pass
      the server, and `launch.py`/`server.py` are not this module's to edit.
      The hook exists anyway - `create_app(state_source=...)` takes any
      `MissionFeed`-shaped object (`.poll_once()`, `.intel()`, `.overlay()`,
      `.note_datum()`, `.loop()`, `.stop()`) - so wiring it in-process later is
      a one-line change, and the tests use it.

  (b) Proxy the MCP server over loopback HTTP. CHOSEN, because it is the
      wiring that actually exists today and it keeps the bridge honest about
      being a *reader*: it can only see what a harness could see.

The proxy runs in loop C - a dedicated background thread on its own cadence -
and `/snapshot` serves the cache it fills. No request handler and no telemetry
tick ever waits on the MCP server (T3, BRIDGE_CONTRACT rule 4). When the MCP
server is unreachable, or a tool/resource in TOOL_CONTRACT.md has not been
registered yet, the affected section comes back EMPTY with the reason recorded
in `/snapshot.feeds` - never a plausible-looking default (see "fail visibly").

WHERE AGL COMES FROM (REAL_DATA_INTEGRATION.md)
-----------------------------------------------
`agl` used to be `max(0.0, -ned.z)` computed right here: height above the
LAUNCH DATUM (the NED origin at home), which is true AGL only over ground at
the home elevation. The MCP server now measures AGL against real terrain, so
the two surfaces published DIFFERENT numbers for the same safety-relevant
quantity — over the Fordow ridge the server read -42.2 m (42 m BELOW the crest)
while this bridge put +39.9 m on the operator's HUD, an 82 m disagreement. That
is the same defect class as the geoid-applied-twice altHae split, and it gets
the same fix: ONE source of truth. The bridge already proxies the MCP server for
mission state; `alt_agl_m` now comes from there too, verbatim, and is never
re-derived here.

What a vehicle row carries:

  * `agl` / `alt_agl_m` — the same number in both contracts' spellings, written
    in ONE place (`VehicleSnapshot.set_agl`) so they cannot drift apart;
  * `alt_agl_is_real` / `alt_agl_source` — measured against terrain, or assumed;
    the HUD must be able to tell the operator which;
  * `alt_agl_launch_datum_m` — this bridge's OWN current NED figure, always
    present, so the old number stays visible next to the measured one;
  * `alt_agl_launch_datum_mismatch_m` — set when the server's launch-datum AGL
    disagrees with this bridge's by more than the two can be apart, i.e. the
    two processes are not flying the same frame;
  * `alt_agl_launch_datum_check` — what that comparison actually DID, in words
    and always present. `mismatch_m: None` alone could not tell "compared, and
    they agree" from "never compared";
  * `alt_agl_reason` — why the number is NOT measured, whenever it is not;
  * `alt_agl_at_ms` / `alt_agl_age_ms` — when loop C observed it. Loop A runs at
    10 Hz and loop C at 2 Hz, so a measured AGL is always a little older than
    the position beside it;
  * `alt_agl_measured_age_ms` — how old the MEASUREMENT is: how long since the
    server's own tick last advanced. A server whose monitor loop has stopped
    keeps answering `uav_task_status` with the same tick, so the POLL stays
    fresh while the number is frozen; only this one shows that. `None` is
    never "fresh" — `alt_agl_measured_age_source` says why it is unknown.

The `max(0.0, ...)` floor is gone with the rest: it made an aircraft BELOW the
terrain read as 0 m AGL, hiding exactly the situation the operator most needs.

FAIL VISIBLY (BRIDGE_CONTRACT rule 3)
-------------------------------------
The previous version wrapped `snapshot()` in a bare `except Exception` that
returned `None`, so a geoid error blanked all telemetry with no trace. Now:

  * a degraded geoid keeps telemetry flowing and says so - `datum_degraded`
    on every vehicle row, a `datum_degraded` SSE alarm, and a `sim_state`
    suffix;
  * an altitude that cannot be resolved at all is NOT guessed: the vehicle row
    goes stale, carries `telemetry_error`, and `sim_state` reports it;
  * `fuel_pct`/`bingo_fuel_pct`/`eta_to_bingo_s` default to `None`, never to
    100.0, and `fuel_source` says why they are unknown;
  * a missing MCP tool or resource is reported per feed in `/snapshot.feeds`.

THE ACTIVE THEATER (INTEGRATION_FINDINGS.md UI-1)
-------------------------------------------------
The bridge does not pick a theater and never sees `launch.py`'s `--theater`.
It used to publish nothing about one either, so the command center had two
feeds and neither answered the question: `/theaters` carried the TABLE default
and `/health` had no theater key. Run at `iran-isfahan`, the panel therefore
initialised to Redmond and listed Redmond POIs while the aircraft flew over
Iran.

The authority is the MCP server that is enforcing the envelope, which states it
in the `theater` block of `uav://safety/geofence`. Loop C already read that
resource for the geofence layer; it now also republishes it, in ONE shape,
under `GET /theaters -> active` and `GET /health -> theater`:

    {"known": bool, "id": str|None, "label": str|None,
     "ground_elevation_msl_m": float|None, "ao": [[lat, lon], ...]|None,
     "in_table": bool, "theater_mismatch": obj|None,
     "source": str, "at_ms": int, "reason": str}

`known` is true ONLY when a server actually answered with a theater id. It
never degrades to `theaters.DEFAULT_THEATER_ID`: an unknown theater comes back
`known: false` with `reason` saying why (MCP down, resource unregistered, no
theater block), because a wrong-but-plausible theater is exactly what put the
operator one click from a target 10,000 km away. `in_table` says whether this
bridge's own table has that row, so "somewhere I can draw" and "a theater I
have never heard of" are distinguishable; `theater_mismatch` is the server's
own report that its enforced envelope belongs to a DIFFERENT theater than
everything else it derives from the theater row.

CORS
----
The allowlist is configurable. Set `GODSEYE_BRIDGE_CORS_ORIGINS` to a comma
(or space) separated list of origins, e.g.

    GODSEYE_BRIDGE_CORS_ORIGINS="http://localhost:3000,http://localhost:8080"

`*` allows any origin; because a wildcard and credentialed requests are
mutually exclusive in browsers, `*` turns `allow_credentials` off (the bridge
authenticates with a Bearer header, not cookies, so nothing is lost). The
effective policy is echoed at `GET /health` under `cors`, so a dev server that
cannot reach the bridge can see why without reading this file.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import theaters
from .geo import (
    AltitudeFix,
    GeoidUnavailableError,
    GeoPoint,
    HomeGeoPoint,
    NedPoint,
    canonical_altitude,
    ned_to_geodetic,
)
from .missions import coverage_of_path

#: Fallback home for a bridge built with no theater. `GeoPoint.altitude` is
#: HAE by geo.py's contract, and `AirSimAdapter` reads it that way; a caller
#: holding an AirSim `settings.json` OriginGeopoint (which is entered in MSL)
#: must say so with `home_datum="msl"` rather than let the datum be guessed.
DEFAULT_HOME = GeoPoint(47.641468, -122.140165, 122.0)

#: Vite dev (5173), Vite preview (4173) and the GEV standalone port (5199).
DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:5199", "http://127.0.0.1:5199",
    "http://localhost:5173", "http://127.0.0.1:5173",
    "http://localhost:4173", "http://127.0.0.1:4173",
)
CORS_ENV = "GODSEYE_BRIDGE_CORS_ORIGINS"

#: Severity per alarm kind (BRIDGE_CONTRACT `/events`). This table is the set
#: of kinds the bridge may emit; `Alarm` refuses anything else rather than let
#: an invented kind reach the operator wearing a default severity.
ALARM_SEVERITY: dict[str, str] = {
    "bingo": "critical",
    "geofence_proximity": "warning",
    "geofence_breach": "critical",
    "lost_link": "critical",
    "link_restored": "info",
    "detection": "info",
    "mission_phase": "info",
    "datum_degraded": "warning",
}

#: Contract phases for `missions[].phase`.
MISSION_PHASES = ("planning", "executing", "rtb", "complete", "aborted")

#: Task/mission states as the MCP server spells them -> contract phase.
_PHASE_MAP = {
    "queued": "planning", "planning": "planning", "planned": "planning",
    "running": "executing", "executing": "executing", "active": "executing",
    "done": "complete", "complete": "complete", "completed": "complete",
    "failed": "aborted", "cancelled": "aborted", "canceled": "aborted",
    "aborted": "aborted", "incomplete": "aborted",
}

#: How long the vehicle roster is reused before `listVehicles` is asked again.
ROSTER_TTL_S = 2.0

#: How long a camera subscription survives with no request for its frame.
#: Without this, loop B keeps pulling images for a browser tab that closed
#: minutes ago and the "subscriber-gated" budget (T3) is fiction.
CAMERA_SUB_TTL_S = 12.0

#: Flown-track retention per vehicle (bounded memory; ~30 min at 2 m spacing).
FLOWN_MAX_POINTS = 4000
FLOWN_MIN_STEP_M = 2.0

#: How long the bridge waits before re-reading `uav://safety/geofence` after a
#: read that produced nothing. The resource is static for a run, so it is read
#: once and then retried on a slow clock: a server that registers it late still
#: gets the geofence layer and the ACTIVE theater, and one that never will is
#: not hammered.
GEOFENCE_RETRY_S = 30.0

#: Where the ACTIVE theater comes from. The bridge does not choose a theater and
#: cannot see `launch.py`'s `--theater`; the only authority is the MCP server
#: that is enforcing the envelope, so that is what is cited to the operator.
ACTIVE_THEATER_SOURCE = "mcp:uav://safety/geofence"

#: `alt_agl_source` for an AGL this bridge derived itself: the NED down-offset
#: from the LAUNCH DATUM (the home plane). It is true AGL only over ground at
#: the home elevation. Deliberately a DIFFERENT string from the MCP server's own
#: `synthetic:launch-datum`, so a HUD showing the provenance also says which
#: process produced the number.
AGL_SOURCE_BRIDGE_LAUNCH = "bridge:launch-datum"

#: What the number on the row IS whenever it is not a measured one. Every
#: `alt_agl_reason` ends with this: a reason that says only what the value is
#: NOT leaves the operator reading an unlabelled altitude, which is the defect.
AGL_LAUNCH_DATUM_NOTE = (
    "the value published is height above the LAUNCH DATUM (the home plane), "
    "which equals true AGL only over ground at the home elevation")

#: Why a row's AGL is the launch datum: loop C has not folded a measured value
#: onto it yet.
AGL_UNENRICHED_REASON = (
    "no MCP-measured AGL has been folded onto this row yet; "
    + AGL_LAUNCH_DATUM_NOTE)

#: How far the MCP server's own launch-datum AGL may sit from this bridge's own
#: before the two are declared to be flying different frames. Both derive it
#: from the same NED origin in the same sim, so anything past a few metres means
#: the bridge and the server were handed different homes - and then the measured
#: AGL the bridge is republishing does not belong to the aircraft it is drawing.
#: Published as `alt_agl_launch_datum_mismatch_m`, never silently swallowed.
AGL_FRAME_TOLERANCE_M = 5.0

#: The time assumed to separate the MCP server's AGL sample from the NED sample
#: this bridge compares it with, UNTIL the real cadences have been observed:
#: one server monitor tick (`server.DEFAULT_TICK_S` = 0.5 s) plus one loop-C
#: poll (0.5 s at the default `mission_hz=2.0`). After the first two polls both
#: intervals are measured from the feed itself (`MissionFeed._tick_age`) and
#: this constant is only a floor - the bridge does not keep guessing at a rate
#: it can see.
#:
#: It exists because the frame cross-check compares two heights measured at
#: DIFFERENT MOMENTS. Measured on the real stack (a real MCP server over
#: Streamable HTTP, the real bridge, both on one shared NED origin), an
#: aircraft that had just changed height published
#: `alt_agl_launch_datum_mismatch_m: -39.925` - a 40 m "the two processes are
#: not flying the same frame" alarm raised by nothing but the skew between a
#: 10 Hz loop and a 2 Hz one. Every climb and every RTB let-down would raise
#: it, and an alarm that cries wolf on every descent is an alarm nobody reads
#: when the frames really do disagree.
#:
#: So the tolerance is widened by the height the aircraft could ACTUALLY have
#: travelled in the skew window (its own measured vertical rate x the window).
#: This only ever explains a delta; it never hides one - the explanation is
#: published verbatim in `alt_agl_launch_datum_check`.
AGL_FEED_SKEW_FLOOR_MS = 1000

#: `alt_agl_launch_datum_check` when the two launch-datum figures agree.
AGL_FRAME_AGREED = "agreed"

#: How long a mission stays in `missions[]` after it reaches a terminal phase.
#: The MCP catalog has no "list missions" tool, so the bridge only learns a
#: mission id from the task that is flying it. When that task drains the id
#: would vanish, and the operator would never see `complete` or `aborted` -
#: the two phases that say how the mission ENDED (M4's "incomplete - fuel"
#: rides on one of them).
MISSION_RETAIN_S = 120.0

_M_PER_DEG_LAT = 111_320.0


# ---------------------------------------------------------------------------
# small local geometry (projection only - no doctrine lives here)
# ---------------------------------------------------------------------------

def _m_per_deg(lat: float) -> tuple[float, float]:
    return _M_PER_DEG_LAT, _M_PER_DEG_LAT * math.cos(math.radians(lat))


def _ground_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    m_lat, m_lon = _m_per_deg((lat1 + lat2) / 2.0)
    return math.hypot((lat2 - lat1) * m_lat, (lon2 - lon1) * m_lon)


def circle_ring(lat: float, lon: float, radius_m: float,
                points: int = 48) -> list[list[float]]:
    """A closed GeoJSON ring (lon,lat order) of `radius_m` about a point."""
    if radius_m <= 0.0:
        raise ValueError(f"radius_m must be > 0, got {radius_m!r}")
    m_lat, m_lon = _m_per_deg(lat)
    ring: list[list[float]] = []
    for i in range(points):
        th = 2.0 * math.pi * i / points
        ring.append([lon + radius_m * math.sin(th) / m_lon,
                     lat + radius_m * math.cos(th) / m_lat])
    ring.append(list(ring[0]))
    return ring


def corridor_ring(path: list[tuple[float, float]], width_m: float,
                  cap_points: int = 12) -> list[list[float]]:
    """Ground swept by a `width_m` swath along `path`, as a closed ring.

    This is the ground the sensor actually imaged under the same swath model
    `missions.coverage_of_path` scores with - a polyline buffer, not a
    different coverage theory. Degenerate input (one point, or a path that
    never moves) buffers to a disc, which is the honest answer for a hover.
    """
    if width_m <= 0.0:
        raise ValueError(f"width_m must be > 0, got {width_m!r}")
    pts = [(float(a), float(b)) for a, b in path]
    if not pts:
        raise ValueError("a corridor needs at least one point")
    r = width_m / 2.0
    lat0 = sum(p[0] for p in pts) / len(pts)
    m_lat, m_lon = _m_per_deg(lat0)
    xy = [((p[1] - pts[0][1]) * m_lon, (p[0] - pts[0][0]) * m_lat) for p in pts]
    # collapse repeated vertices so a stationary vehicle is a disc, not a spike
    dedup = [xy[0]]
    for x, y in xy[1:]:
        if math.hypot(x - dedup[-1][0], y - dedup[-1][1]) > 1e-6:
            dedup.append((x, y))
    if len(dedup) < 2:
        return circle_ring(pts[0][0], pts[0][1], r, points=max(8, cap_points * 4))

    def normal(i: int) -> tuple[float, float]:
        ax, ay = dedup[max(0, i - 1)]
        bx, by = dedup[min(len(dedup) - 1, i + 1)]
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy) or 1.0
        return (-dy / n, dx / n)

    left, right = [], []
    for i, (x, y) in enumerate(dedup):
        nx, ny = normal(i)
        left.append((x + nx * r, y + ny * r))
        right.append((x - nx * r, y - ny * r))
    ring_xy = left + list(reversed(right))
    ring = [[pts[0][1] + x / m_lon, pts[0][0] + y / m_lat] for x, y in ring_xy]
    ring.append(list(ring[0]))
    return ring


# ---------------------------------------------------------------------------
# CORS policy (configurable - BRIDGE assignment item 6)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CorsPolicy:
    origins: tuple[str, ...]
    allow_any: bool
    allow_credentials: bool
    source: str

    def as_dict(self) -> dict:
        return {
            "origins": ["*"] if self.allow_any else list(self.origins),
            "allow_any_origin": self.allow_any,
            "allow_credentials": self.allow_credentials,
            "source": self.source,
            "env": CORS_ENV,
            "note": ("set " + CORS_ENV + " to a comma-separated origin list, "
                     "or '*' for any origin (which disables credentials, as "
                     "browsers forbid the combination)"),
        }


def cors_policy(raw: str | None = None, env: dict | None = None) -> CorsPolicy:
    """Resolve the CORS allowlist from `raw`, else the environment, else default.

    A dev server on a port that is not in the list fails every fetch before it
    starts, which is why the list must not be a literal in the source.
    """
    if raw is None:
        raw = (env if env is not None else os.environ).get(CORS_ENV)
    if raw is None or not str(raw).strip():
        return CorsPolicy(DEFAULT_CORS_ORIGINS, False, True, "default")
    parts = [p.strip() for p in str(raw).replace(",", " ").split() if p.strip()]
    if "*" in parts:
        # `*` with credentials is rejected by every browser; the bridge
        # authenticates with a Bearer header, so dropping credentials is safe.
        return CorsPolicy((), True, False, CORS_ENV)
    return CorsPolicy(tuple(parts), False, True, CORS_ENV)


# ---------------------------------------------------------------------------
# alarms + SSE fan-out
# ---------------------------------------------------------------------------

@dataclass
class Alarm:
    """One `/events` payload. Kinds outside ALARM_SEVERITY are refused."""

    kind: str
    message: str
    severity: str = ""
    vehicle: str = ""
    track_id: str = ""
    mission_id: str = ""
    at_ms: int = 0
    detail: dict | None = None

    def __post_init__(self) -> None:
        if self.kind not in ALARM_SEVERITY:
            raise ValueError(
                f"unknown alarm kind {self.kind!r}; the contract defines "
                f"{sorted(ALARM_SEVERITY)}")
        if not self.severity:
            self.severity = ALARM_SEVERITY[self.kind]
        if not self.at_ms:
            self.at_ms = int(time.time() * 1000)

    def payload(self) -> dict:
        out = {"kind": self.kind, "severity": self.severity,
               "message": self.message, "atMs": self.at_ms}
        if self.vehicle:
            out["vehicle"] = self.vehicle
        if self.track_id:
            out["track_id"] = self.track_id
        if self.mission_id:
            out["mission_id"] = self.mission_id
        if self.detail:
            out["detail"] = self.detail
        return out


class Subscriber:
    """One SSE reader. Bounded, so a stalled browser cannot grow the heap."""

    def __init__(self, maxsize: int):
        self.queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._dropped = 0
        self._lock = threading.Lock()

    def offer(self, payload: dict) -> None:
        """Never blocks. A full queue drops its OLDEST event and counts it."""
        while True:
            try:
                self.queue.put_nowait(payload)
                return
            except queue.Full:
                try:
                    self.queue.get_nowait()
                except queue.Empty:  # drained by the reader in between
                    continue
                with self._lock:
                    self._dropped += 1

    def take_dropped(self) -> int:
        with self._lock:
            n, self._dropped = self._dropped, 0
        return n


class EventHub:
    """Publish/subscribe for the alarm lane.

    `publish` is called from loop C (a plain thread) and must never block or
    raise into it: a slow or dead subscriber loses events, the telemetry loop
    does not lose time. Dropped events are surfaced on the next delivery
    (`detail.dropped_since_last`) rather than silently swallowed.
    """

    def __init__(self, max_queue: int = 256, history: int = 100):
        self._lock = threading.Lock()
        self._subs: set[Subscriber] = set()
        self._max_queue = max_queue
        self._history: deque = deque(maxlen=history)
        self.published = 0

    def subscribe(self) -> Subscriber:
        sub = Subscriber(self._max_queue)
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, alarm: Alarm) -> int:
        payload = alarm.payload()
        with self._lock:
            subs = list(self._subs)
            self._history.append(payload)
            self.published += 1
        for sub in subs:
            try:
                sub.offer(payload)
            except Exception:
                # A broken reader loses its subscription, never the publisher's
                # thread: loop C must not be stalled by a dead browser.
                self.unsubscribe(sub)
        return len(subs)

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._history)[-limit:]


# ---------------------------------------------------------------------------
# vehicle telemetry
# ---------------------------------------------------------------------------

@dataclass
class VehicleSnapshot:
    name: str
    latitude: float
    longitude: float
    alt_hae: float
    alt_msl: float
    #: Best available AGL: MEASURED against real terrain when the MCP server
    #: has one, the launch datum when it does not. `alt_agl_is_real` says
    #: which. Written only by `set_agl`. NOT floored at zero - an aircraft
    #: below the terrain under it reads negative, which is the point.
    agl: float
    speed_ms: float
    heading_deg: float
    vx: float
    vy: float
    vz: float
    landed_state: int
    armed: bool
    timestamp_ms: int
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    # --- BRIDGE_CONTRACT vehicles[] additions -------------------------------
    # None, never 100.0: an unknown fuel state that reads as a full tank is the
    # exact defect the contract calls out. `fuel_source` says why it is None.
    fuel_pct: float | None = None
    bingo_fuel_pct: float | None = None
    eta_to_bingo_s: float | None = None
    mission: str = ""
    track_id: str = ""
    datum_degraded: bool = False
    datum_source: str = ""
    fuel_source: str = "unavailable: no MCP mission/fuel state yet"
    # --- AGL provenance (REAL_DATA_INTEGRATION; see "WHERE AGL COMES FROM") --
    #: The same number as `agl`, under the spelling every MCP tool uses
    #: (TOOL_CONTRACT). One writer (`set_agl`), so the two cannot disagree.
    alt_agl_m: float = 0.0
    #: Measured against real terrain (True) or assumed from the launch datum.
    alt_agl_is_real: bool = False
    alt_agl_source: str = AGL_SOURCE_BRIDGE_LAUNCH
    #: Why the AGL is not a measured one. None while it is.
    alt_agl_reason: str | None = AGL_UNENRICHED_REASON
    #: This bridge's OWN height above the launch datum, always current and
    #: always present: the pre-terrain number, kept beside the measured one so
    #: the operator can compare rather than have one quietly replace the other.
    alt_agl_launch_datum_m: float = 0.0
    #: Signed difference between the MCP server's launch-datum AGL and this
    #: bridge's own, published ONLY when it exceeds the frame tolerance - i.e.
    #: when the two processes are not flying the same frame.
    alt_agl_launch_datum_mismatch_m: float | None = None
    #: What ACTUALLY happened to the frame cross-check, always stated. `None`
    #: on `alt_agl_launch_datum_mismatch_m` used to mean either "compared, and
    #: they agree" or "never compared" - a check that had not run was
    #: indistinguishable from a check that had passed, which is the exact shape
    #: of the safety gate that read the wrong altitude key and passed
    #: everything. Written with the mismatch, by `set_launch_datum_check`.
    alt_agl_launch_datum_check: str = (
        "not checked: no MCP-measured AGL has been folded onto this row yet")
    #: When loop C observed the AGL being republished, and how old it was when
    #: this row was built.
    alt_agl_at_ms: int | None = None
    alt_agl_age_ms: int | None = None
    #: The WIDEST this measurement can be - an upper bound on the age of the
    #: server's AGL, not of the poll that fetched it. The two are different
    #: numbers and only this one answers the question the operator is asking.
    #: It is an upper bound on purpose: a freshness figure that could be too
    #: small reads as safety the row does not have.
    #:
    #: A server whose monitor loop has stopped keeps answering
    #: `uav_task_status` with the SAME `last_tick` forever. Loop C's polls all
    #: succeed, so `alt_agl_age_ms` stays near zero while the republished AGL
    #: is arbitrarily old - measured on the real stack: the aircraft flew 600 m
    #: and descended 150 m while `agl` sat at -0.3 m, flagged
    #: `alt_agl_is_real: true`, `alt_agl_reason: null`, `alt_agl_age_ms: 63`.
    #: That is precisely "a number that quietly stops moving".
    #:
    #: `None` is NEVER "fresh": it means the age could not be established, and
    #: `alt_agl_measured_age_source` says why.
    alt_agl_measured_age_ms: int | None = None
    alt_agl_measured_age_source: str = (
        "no MCP-measured AGL has been folded onto this row yet")
    # --- visible degradation ------------------------------------------------
    stale_ms: int = 0
    telemetry_error: str | None = None

    def set_agl(self, value: float, *, is_real: bool, source: str,
                reason: str | None = None, at_ms: int | None = None,
                age_ms: int | None = None,
                measured_age_ms: int | None = None,
                measured_age_source: str) -> None:
        """Write the AGL and its provenance TOGETHER.

        `agl` (BRIDGE_CONTRACT) and `alt_agl_m` (TOOL_CONTRACT) are the same
        quantity under two spellings. `AirSimAdapter.snapshot` seeds both at
        construction and this is the only place either is written afterwards:
        two surfaces disagreeing about one aircraft's height is the defect this
        whole path exists to remove, and it is not going to be reintroduced
        between two fields of the same row.

        A value that is not measured MUST carry a `reason`; asserting that here
        is cheaper than an unlabelled altitude reaching the HUD. The age of the
        MEASUREMENT travels with it for the same reason: an AGL and the answer
        to "how old is it?" must not be settable apart, or the row grows a
        stale number wearing a fresh timestamp.
        """
        if not is_real and not reason:
            raise ValueError(
                "an AGL that is not measured must say why: set_agl(..., "
                f"is_real=False) with no reason for {self.name!r}")
        if measured_age_ms is None and not measured_age_source:
            raise ValueError(
                "an AGL whose measurement age is unknown must say why: "
                f"set_agl(..., measured_age_ms=None) with no "
                f"measured_age_source for {self.name!r}")
        self.agl = self.alt_agl_m = float(value)
        self.alt_agl_is_real = bool(is_real)
        self.alt_agl_source = source
        self.alt_agl_reason = None if is_real else reason
        self.alt_agl_at_ms = at_ms
        self.alt_agl_age_ms = age_ms
        self.alt_agl_measured_age_ms = measured_age_ms
        self.alt_agl_measured_age_source = measured_age_source

    def set_launch_datum_check(self, check: str,
                               mismatch_m: float | None = None) -> None:
        """Write the frame cross-check's OUTCOME and its delta together.

        One writer, because the two are one fact. A `mismatch_m` of `None`
        beside a `check` that says the comparison never ran is an honest
        "unknown"; the same `None` with no `check` beside it is an unrun check
        wearing the appearance of a passed one.
        """
        if not check:
            raise ValueError(
                f"the frame cross-check for {self.name!r} must state its "
                "outcome; an unstated one reads as agreement")
        self.alt_agl_launch_datum_check = check
        self.alt_agl_launch_datum_mismatch_m = mismatch_m


def _attitude_from_quat(q) -> tuple[float, float]:
    """AirSim orientation quaternion -> (pitch_deg, roll_deg). Identity-safe."""
    try:
        w, x, y, z = q.w_val, q.x_val, q.y_val, q.z_val
    except AttributeError:
        return 0.0, 0.0
    # roll (x-axis rotation)
    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    roll = math.degrees(math.atan2(sinr_cosp, cosr_cosp))
    # pitch (y-axis rotation), clamped against gimbal lock
    sinp = 2.0 * (w * y - z * x)
    sinp = max(-1.0, min(1.0, sinp))
    pitch = math.degrees(math.asin(sinp))
    return pitch, roll


class AirSimAdapter:
    """Thin sync wrapper over the in-repo airsim client (loopback only).

    **Datum (T1)**: `home` is a `geo.GeoPoint`, whose altitude is HAE by that
    module's contract — and HAE is what `launch.py` hands this class, having
    already converted the theater's declared MSL ground elevation at its own
    ingest boundary. A caller holding an MSL number instead (an AirSim
    `settings.json` OriginGeopoint, say) passes `home_datum="msl"` and the
    conversion happens ONCE, here, exactly as `server.UavBackend` does it.

    This used to read `ned_to_geodetic(...).altitude` — an HAE quantity, by
    that function's own docstring — as if it were MSL, so the geoid was applied
    a SECOND time on every telemetry sample. The bridge and the MCP server
    therefore published altitudes for the same aircraft that differed by the
    undulation N: +1.5 m at Natanz, -22.2 m at the Redmond origin, -33.2 m at
    indo-pak-loc. Both cannot be the gate-grade datum T1 demands, and the one
    that was wrong was the one on the operator's HUD.
    """

    def __init__(self, ip: str = "127.0.0.1", port: int = 41451,
                 home: GeoPoint = DEFAULT_HOME, client=None,
                 home_datum: str = "hae"):
        self.ip, self.port = ip, port
        if home_datum not in ("hae", "msl"):
            raise ValueError(
                f"home_datum must be 'hae' or 'msl', got {home_datum!r}; an "
                "unlabelled home altitude is how the geoid got applied twice")
        self.home_declared = home
        self.home_datum = home_datum
        self._client = client
        #: rpc_patch hands back a lock BECAUSE msgpack-rpc's single tornado
        #: IOLoop is not re-entrant; loops A (telemetry), B (camera) and C
        #: (roster) plus the /camera request thread all drive this one client.
        #: The lock used to be assigned and never used, which is the same as
        #: not having one.
        self._client_lock = threading.Lock()
        self._client_err: str | None = None
        #: set whenever the geoid had to fall back to the coarse model (T1).
        self.datum_degraded = False
        self.datum_source = ""
        #: THE home conversion, done once. With `home_datum="hae"` there is
        #: nothing to convert, so no geoid is touched at construction and a
        #: bridge still boots where the EGM96 grid is missing.
        self.home_geo = HomeGeoPoint.from_geo(
            home if home_datum == "hae" else
            GeoPoint(home.latitude, home.longitude,
                     self.altitude_fix(home.altitude, home.latitude,
                                       home.longitude, datum="msl").alt_hae))
        #: last snapshot failure, kept so the bridge can report it instead of
        #: returning None into a void (BRIDGE_CONTRACT rule 3).
        self.last_error: str | None = None
        #: set when the vehicle roster is an assumption rather than a reading.
        self.vehicles_fallback: str | None = None
        #: last camera failure, so a 503 can say WHY the PIP is dark.
        self.camera_error: str | None = None

    def _ensure(self):
        if self._client is None:
            try:
                from .rpc_patch import make_client

                self._client, self._client_lock = make_client(ip=self.ip, port=self.port)
                with self._client_lock:
                    self._client.confirmConnection()
                self._client_err = None
            except Exception as e:
                self._client_err = f"{type(e).__name__}: {e}"
                self._client = None
        return self._client

    @property
    def sim_state(self) -> str:
        if self._client_err:
            return f"down: {self._client_err}"
        if not self._client:
            return "connecting"
        if self.datum_degraded:
            # Still flying, still reporting - but on a datum that is NOT
            # gate-grade. `startswith("up")` keeps the field machine-readable.
            return "up: datum_degraded"
        return "up"

    def vehicles(self) -> list[str]:
        c = self._ensure()
        if not c:
            return []
        try:
            with self._client_lock:
                out = [v for v in c.listVehicles()]
            self.vehicles_fallback = None
            return out
        except Exception as e:
            # Older AirSim builds have no listVehicles, so the single-vehicle
            # assumption stays - but it is an ASSUMPTION, and it is now named
            # in `/health` instead of being indistinguishable from a real
            # roster of one. A guess that looks like a measurement is the
            # pattern this file exists to stop.
            self.vehicles_fallback = (
                f"listVehicles unavailable ({type(e).__name__}: {e}); "
                "assuming the single vehicle 'Drone1'")
            return ["Drone1"]

    def altitude_fix(self, alt_m: float, lat: float, lon: float,
                     datum: str = "hae"):
        """THE datum conversion for the telemetry path (T1), applied once.

        `datum` names what `alt_m` IS. The telemetry path passes HAE, because
        `ned_to_geodetic` returns HAE ("NED -> (lat, lon, alt_HAE). The origin
        altitude is HAE, so this is." — `server.UavBackend.ned_to_llh`); the
        constructor passes MSL when the caller declared an MSL home. Calling
        this with the wrong `datum` applies N twice, which is the bug this
        parameter exists to make impossible to write by accident.

        A geoid that will not load is a DEGRADED state, not a dead feed: the
        coarse fallback is taken, flagged on every vehicle row and announced on
        the alarm lane. Silence here is what blanked telemetry before.
        """
        try:
            fix = canonical_altitude(alt_m, lat, lon, datum=datum)
        except GeoidUnavailableError:
            fix = canonical_altitude(alt_m, lat, lon, datum=datum,
                                     allow_approx=True)
            # This reading came off the fallback path. Label it degraded from
            # the control flow, not from what the fix claims about itself: a
            # fix that reports degraded=False here would put an unlabelled
            # approximation on the operator's HUD.
            fix = AltitudeFix(fix.alt_hae, fix.alt_msl, fix.undulation_m,
                              fix.source, True)
        self.datum_degraded = fix.degraded
        self.datum_source = fix.source
        return fix

    def snapshot(self, name: str) -> VehicleSnapshot | None:
        c = self._ensure()
        if not c:
            self.last_error = self._client_err or "no client"
            return None
        try:
            with self._client_lock:
                st = c.getMultirotorState(vehicle_name=name)
            pos = st.kinematics_estimated.position
            vel = st.kinematics_estimated.linear_velocity
            q = st.kinematics_estimated.orientation
            pitch_deg, roll_deg = _attitude_from_quat(q)
            ned = NedPoint(pos.x_val, pos.y_val, pos.z_val)
            gp = ned_to_geodetic(ned, self.home_geo)
            # `gp.altitude` is HAE (GeoPoint's contract, and what
            # ned_to_geodetic documents it returns). Convert once, here, in
            # the direction the datum actually runs (T1).
            fix = self.altitude_fix(gp.altitude, gp.latitude, gp.longitude,
                                    datum="hae")
            speed = math.sqrt(vel.x_val**2 + vel.y_val**2 + vel.z_val**2)
            heading = math.degrees(math.atan2(vel.y_val, vel.x_val)) if speed > 0.3 else 0.0
            self.last_error = None
            # Height above the LAUNCH DATUM, and labelled as such. This used to
            # be published as `agl` full stop, and floored at 0.0 - so an
            # aircraft 42 m BELOW the ridge under it read "0 m AGL" while the
            # MCP server, measuring against real terrain, read -42.2 m. The
            # floor is gone (a negative AGL is the operator's most important
            # reading) and the number itself is only the FALLBACK now:
            # `BridgeState.enrich` replaces it with the server's measured AGL.
            launch_datum_agl = -ned.z
            return VehicleSnapshot(
                name=name,
                latitude=gp.latitude,
                longitude=gp.longitude,
                alt_hae=fix.alt_hae,
                alt_msl=fix.alt_msl,
                agl=launch_datum_agl,
                alt_agl_m=launch_datum_agl,
                alt_agl_launch_datum_m=launch_datum_agl,
                alt_agl_is_real=False,
                alt_agl_source=AGL_SOURCE_BRIDGE_LAUNCH,
                alt_agl_reason=AGL_UNENRICHED_REASON,
                speed_ms=speed,
                heading_deg=heading % 360.0,
                pitch_deg=pitch_deg,
                roll_deg=roll_deg,
                vx=vel.x_val, vy=vel.y_val, vz=vel.z_val,
                landed_state=int(st.landed_state),
                armed=True,
                timestamp_ms=int(time.time() * 1000),
                datum_degraded=fix.degraded,
                datum_source=fix.source,
            )
        except Exception as e:
            # NOT silence. The error is kept, surfaces in `sim_state`, in
            # `/health` and on the stale vehicle row (BRIDGE_CONTRACT rule 3).
            self._client_err = f"{type(e).__name__}: {e}"
            self.last_error = self._client_err
            self._client = None
            return None

    def camera_png(self, name: str, camera: str = "0", image_type: int = 0) -> bytes | None:
        c = self._ensure()
        if not c:
            self.camera_error = self._client_err or "no sim client"
            return None
        try:
            import airsim

            reqs = [airsim.ImageRequest(camera, image_type, False, True)]
            with self._client_lock:
                resp = c.simGetImages(reqs, vehicle_name=name)
            if not resp:
                self.camera_error = f"{name}/{camera}: the sim returned no image"
                return None
            data = resp[0].image_data_uint8
            self.camera_error = None
            if isinstance(data, str):
                return base64.b64decode(data)
            return bytes(data)
        except Exception as e:
            # A dark PIP must say why. The 503 carries this text so an operator
            # is not left guessing whether the sensor or the link is the fault.
            self.camera_error = f"{name}/{camera}: {type(e).__name__}: {e}"
            return None


# ---------------------------------------------------------------------------
# MCP proxy (loop C transport)
# ---------------------------------------------------------------------------

class McpClient:
    """JSON-RPC over the MCP server's Streamable-HTTP endpoint.

    Every method returns `(payload, error)` and NEVER raises: loop C keeps
    running, and the error text lands in `/snapshot.feeds` where an operator
    can read it. A tool or resource that a Wave-3 server has not registered yet
    comes back as an error string, so the affected section degrades to empty
    with a stated reason instead of to a fabricated default.
    """

    def __init__(self, url: str, token: str, timeout: float = 5.0):
        self.url, self.token, self.timeout = url, token, timeout
        self._id = 0
        self._lock = threading.Lock()

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def rpc(self, method: str, params: dict) -> tuple[Any, str | None]:
        payload = {"jsonrpc": "2.0", "id": self._next_id(),
                   "method": method, "params": params}
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.token}",
            }, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read().decode()
        except urllib.error.HTTPError as e:
            return None, f"HTTP {e.code} from {self.url}"
        except Exception as e:
            return None, f"MCP unreachable: {type(e).__name__}: {e}"
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                body = line[5:].strip()
                break
        try:
            out = json.loads(body)
        except Exception:
            return None, f"unparseable MCP response: {body[:160]!r}"
        if isinstance(out, dict) and out.get("error"):
            err = out["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return None, f"{method}: {msg}"
        return out, None

    @staticmethod
    def _text_payload(result: Any, key: str) -> tuple[Any, str | None]:
        res = result.get("result", result) if isinstance(result, dict) else result
        items = res.get(key) if isinstance(res, dict) else None
        if isinstance(items, list) and items and isinstance(items[0], dict):
            text = items[0].get("text")
            if text is not None:
                try:
                    return json.loads(text), None
                except Exception:
                    return {"text": text}, None
        if isinstance(res, dict):
            return res, None
        return None, f"unexpected MCP result shape: {type(res).__name__}"

    @staticmethod
    def _tool_error(name: str, result: Any) -> str | None:
        """`isError` on a tools/call result, as the MCP spec spells failure.

        A tool that raises (or whose arguments do not validate) comes back as
        `{"isError": true, "content": [{"text": "Error executing tool ..."}]}`
        with NO JSON-RPC error - so `rpc()` reports success, the text does not
        parse as JSON, and the old code handed the caller
        `({"text": "Error executing tool ..."}, None)`. A failing tool then read
        as a healthy feed: `/snapshot.feeds` said `ok`, and the mission row it
        built from the error text was blank rather than absent. Only the
        server's own structured `{"error": {...}}` envelope was ever noticed.
        """
        if not isinstance(result, dict) or not result.get("isError"):
            return None
        text = ""
        content = result.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text = str(content[0].get("text") or "")
        return f"{name}: tool error: {' '.join(text.split())[:300] or 'isError'}"

    def call_tool(self, name: str, arguments: dict) -> tuple[Any, str | None]:
        out, err = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if err:
            return None, err
        res = out.get("result", out) if isinstance(out, dict) else out
        tool_err = self._tool_error(name, res)
        if tool_err:
            return None, tool_err
        payload, err = self._text_payload(out, "content")
        if err:
            return None, err
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            e = payload["error"]
            return None, f"{name}: {e.get('code')}: {e.get('message')}"
        return payload, None

    def read_resource(self, uri: str) -> tuple[Any, str | None]:
        out, err = self.rpc("resources/read", {"uri": uri})
        if err:
            return None, err
        return self._text_payload(out, "contents")


# ---------------------------------------------------------------------------
# loop C: mission + contact + safety state
# ---------------------------------------------------------------------------

@dataclass
class FeedStatus:
    ok: bool = False
    error: str | None = None
    at_ms: int = 0
    detail: str = ""

    def as_dict(self) -> dict:
        out = {"ok": self.ok, "atMs": self.at_ms}
        if self.error:
            out["error"] = self.error
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class MissionIntel:
    """The cache `/snapshot` and `/mission-overlay` serve. Never blocks a read."""

    missions: list[dict] = field(default_factory=list)
    contacts: list[dict] = field(default_factory=list)
    per_vehicle: dict[str, dict] = field(default_factory=dict)
    feeds: dict[str, FeedStatus] = field(default_factory=dict)
    at_ms: int = 0

    def feeds_dict(self) -> dict:
        return {k: v.as_dict() for k, v in self.feeds.items()}


def _phase_for(state: str | None, active_tool: str | None,
               force_rtb: bool = False) -> str:
    """Contract phase from the server's own state words. Unknown -> planning.

    An RTB in progress outranks the task state: what the operator needs to see
    is that the aircraft is coming home, not that a task is "running".
    """
    if force_rtb or (active_tool or "") == "uav_return_to_home":
        return "rtb"
    phase = _PHASE_MAP.get(str(state or "").lower(), "planning")
    if phase not in MISSION_PHASES:  # pragma: no cover - guards a table typo
        raise ValueError(
            f"_PHASE_MAP produced {phase!r}, which is not one of the contract "
            f"phases {MISSION_PHASES}; the GEV normalizer would silently show "
            "it as 'unknown'")
    return phase


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _age_words(ms: int | None) -> str:
    """An age for a sentence. `None` is "of unknown age", never "0 s"."""
    return "of an age this bridge could not establish" if ms is None \
        else f"{ms / 1000.0:.2f} s"


def _int_or_none(value: Any) -> int | None:
    """A whole number of milliseconds, or None. `True` is not 1 here: a bool
    arriving where an age belongs is a bug in the source, not a 1 ms age."""
    if isinstance(value, bool):
        return None
    out = _f(value)
    return None if out is None else int(out)


def _agl_from_tick(tick: dict) -> dict:
    """The MCP server's AGL for one tick — or an explicit, reasoned absence.

    The bridge does not derive this number and does not repair it. The server
    measures `alt_agl_m` against real terrain (`resolve_agl`) and states on
    every tick whether that is what happened (`alt_agl_is_real`,
    `alt_agl_source`); this reads those keys verbatim so the two surfaces
    cannot disagree, and returns `alt_agl_m: None` with a reason when the tick
    does not carry them — a tick with no telemetry in it has no altitude at all.

    Three refusals, all of them cases that used to be a shrug:

    * no `alt_agl_m` key — the server never measured on this tick;
    * a non-finite `alt_agl_m` — a NaN that would render as a plausible gauge;
    * `alt_agl_m` with no boolean `alt_agl_is_real` — an altitude whose
      provenance is unstated is NOT published as measured. `tick.get(
      "alt_agl_is_real", False)` would have quietly labelled a measured value
      "assumed"; `.get(..., True)` would have done the far worse opposite.
    """
    row: dict = {
        "alt_agl_m": None,
        "alt_agl_is_real": False,
        "alt_agl_source": None,
        # The server's own launch-datum figure, for the frame cross-check in
        # `BridgeState.enrich`. Absent on a tick without telemetry.
        "alt_agl_launch_datum_m": _f(tick.get("alt_agl_launch_datum_m")),
        "alt_agl_reason": None,
    }
    if "alt_agl_m" not in tick:
        why = ""
        if tick.get("telemetry") is False:
            why = " (that tick carried no telemetry"
            err = tick.get("telemetry_error")
            why += f": {err})" if err else ")"
        row["alt_agl_reason"] = (
            f"the MCP server's last tick carried no alt_agl_m{why}")
        return row
    value = _f(tick.get("alt_agl_m"))
    if value is None:
        row["alt_agl_reason"] = (
            f"the MCP server's last tick carried alt_agl_m="
            f"{tick.get('alt_agl_m')!r}, which is not a finite number")
        return row
    if not isinstance(tick.get("alt_agl_is_real"), bool):
        row["alt_agl_reason"] = (
            "the MCP server's last tick carried an alt_agl_m with no "
            "alt_agl_is_real; an altitude whose provenance is unstated is not "
            "published as measured")
        return row
    row["alt_agl_m"] = value
    row["alt_agl_is_real"] = tick["alt_agl_is_real"]
    row["alt_agl_source"] = str(tick.get("alt_agl_source") or "").strip() or None
    if not row["alt_agl_is_real"]:
        row["alt_agl_reason"] = str(tick.get("alt_agl_reason") or "").strip() or (
            "the MCP server reports alt_agl_is_real=false: this is height above "
            "the LAUNCH DATUM, not above the ground under the aircraft")
    return row


def _salute_text(block: Any, *keys: str) -> str:
    """Flatten one SALUTE sub-report to the contract's string field."""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        for k in keys:
            v = block.get(k)
            if isinstance(v, str) and v:
                return v
    return ""


class MissionFeed:
    """Polls the MCP server for mission/track/safety state and derives alarms.

    Runs on its own thread so neither `/snapshot` nor the telemetry tick ever
    waits on it (T3, BRIDGE_CONTRACT rule 4).

    Alarm edges are derived from the *observed level* of the server's own
    safety state (`bingo.latched`, the geofence violations, the link machine,
    the track roster, the mission phase), not from the tick's transient edge
    list. A level is still true on the next poll, so a condition can never be
    missed by landing between polls; the price is that a condition which both
    raises and clears inside one poll interval is not seen, which is recorded
    here rather than hidden.
    """

    def __init__(self, mcp: McpClient, hub: EventHub, *,
                 vehicles: Callable[[], list[str]],
                 flown: Callable[[str], list[tuple[float, float]]]):
        self.mcp = mcp
        self.hub = hub
        self._vehicles = vehicles
        self._flown = flown
        self._lock = threading.Lock()
        self._intel = MissionIntel()
        self._overlay: dict = _empty_overlay("loop C has not polled yet")
        self._overlay_key: str | None = None
        self._stop = threading.Event()
        # observed levels, for edge derivation
        self._seen_bingo: dict[str, bool] = {}
        self._seen_fence: dict[str, str] = {}
        self._seen_link: dict[str, bool] = {}
        self._seen_phase: dict[str, str] = {}
        self._seen_tracks: set[str] = set()
        self._seen_datum: dict[str, bool] = {}
        #: vehicle -> (the server's own tick stamp, wall-clock ms at which THIS
        #: bridge first saw that stamp). The only way to tell a live feed from
        #: a server whose monitor loop has stopped: the poll succeeds either
        #: way, so a fresh poll is not evidence of a fresh measurement.
        self._tick_seen: dict[str, tuple[float, int]] = {}
        #: vehicle -> the MEASURED interval between the server's own ticks, from
        #: consecutive `t` stamps. Observed, so a server ticking at some other
        #: rate is accounted for instead of assumed at `DEFAULT_TICK_S`.
        self._tick_gap_ms: dict[str, int] = {}
        #: The MEASURED interval between this feed's own polls, and when the
        #: last one started. `mission_hz` is a parameter and the contract's own
        #: rate for this feed is 1 Hz, not the code default of 2 Hz.
        self._poll_gap_ms: int | None = None
        self._last_poll_ms: int | None = None
        # caches refreshed only on change
        self._mission_detail: dict[str, dict] = {}
        self._threat: dict[str, str] = {}
        self._threat_rings: dict[str, dict] = {}
        self._track_key: str | None = None
        self._geofence: dict | None = None
        self._geofence_at = 0.0
        #: why the last `uav://safety/geofence` read produced nothing, and when
        #: the one that succeeded landed. Both are published: an operator asking
        #: "which theater am I flying?" is owed the reason when the answer is
        #: "unknown", not an empty field.
        self._geofence_error: str | None = None
        self._geofence_ms = 0
        self._detail_missing: set[str] = set()
        #: mission_id -> vehicle, so a mission survives its flying task long
        #: enough for the operator to read how it ended (MISSION_RETAIN_S).
        self._seen_missions: dict[str, str] = {}
        self._finished_at: dict[str, float] = {}
        #: alarms that could not be published - counted, never swallowed.
        self.emit_failures = 0
        self.last_emit_error: str | None = None
        #: vehicle -> (burned_pct, elapsed_s) of the last observed fuel record,
        #: so the BINGO ETA is a measured rate rather than a guess.
        self._fuel_prev: dict[str, tuple[float, float]] = {}
        self.polls = 0

    # ---- read side (no I/O) ------------------------------------------------
    def intel(self) -> MissionIntel:
        with self._lock:
            return self._intel

    def overlay(self) -> dict:
        with self._lock:
            return self._overlay

    def active_theater(self) -> dict:
        """The theater the RUNNING MCP server is enforcing, or an honest unknown.

        INTEGRATION_FINDINGS UI-1: the stack was started at `iran-isfahan`, the
        aircraft was at 32.65N 51.67E, and the command center's selector sat on
        `default` listing Redmond POIs — one click from seeding a target
        10,000 km away. Neither feed it could read carried the answer:
        `GET /theaters` served the TABLE default and `GET /health` had no
        theater key at all. The MCP server does know, in the `theater` block of
        `uav://safety/geofence`, so that is what is republished here.

        Non-blocking: a pure read of what loop C last learned. Never falls back
        to `theaters.DEFAULT_THEATER_ID` — "unknown, because X" is a state the
        panel can show as a mismatch, whereas a plausible wrong theater is not.

        KNOWN LIMIT, stated rather than papered over: `uav://safety/geofence`
        is read ONCE and then cached for the life of the bridge (see
        `_refresh_geofence`), so if an MCP server were replaced under a running
        bridge by one flying a DIFFERENT theater, this would keep answering the
        old one. `at_ms` is published for exactly that reason - it is when the
        answer was learned, not when it was served. The shipped topology cannot
        reach the condition (`launch.py` runs the sim, the MCP server and this
        bridge in one process, and the theater is fixed at server construction
        - `sim_reset` does not change it); a deployment that points
        `GODSEYE_MCP_URL` at a separately-restartable server can, and wants a
        periodic re-read here.
        """
        with self._lock:
            doc, err, at_ms = (self._geofence, self._geofence_error,
                               self._geofence_ms)
        if doc is None:
            return theaters.active_unknown(
                f"uav://safety/geofence could not be read: {err}" if err else
                "uav://safety/geofence has not been read yet — loop C has not "
                "polled, or the MCP server is not up",
                source=ACTIVE_THEATER_SOURCE)
        block = doc.get("theater")
        if not isinstance(block, dict):
            return theaters.active_unknown(
                "the server answered uav://safety/geofence but its payload "
                "carried no `theater` block",
                source=ACTIVE_THEATER_SOURCE)
        return theaters.active_from_server(
            block, source=ACTIVE_THEATER_SOURCE, at_ms=at_ms,
            theater_mismatch=doc.get("theater_mismatch"))

    def stop(self) -> None:
        self._stop.set()

    # ---- alarm helpers -----------------------------------------------------
    def _emit(self, alarm: Alarm) -> None:
        """Publish an alarm. The lane never stalls loop C, and never swallows.

        A failure here means an alarm the operator will not see, so it is
        counted and surfaced in `/snapshot.feeds` rather than dropped on the
        floor - a silently missing BINGO warning is the worst bug this file
        could ship.
        """
        try:
            self.hub.publish(alarm)
        except Exception as e:
            self.emit_failures += 1
            self.last_emit_error = f"{type(e).__name__}: {e}"

    def note_datum(self, vehicle: str, degraded: bool, source: str) -> None:
        """Edge-trigger the `datum_degraded` alarm. Called from loop A."""
        if self._seen_datum.get(vehicle) == degraded:
            return
        self._seen_datum[vehicle] = degraded
        if degraded:
            self._emit(Alarm(
                kind="datum_degraded", vehicle=vehicle,
                message=(f"geoid source degraded to {source!r} - altitudes are "
                         "NOT gate-grade"),
                detail={"datum_source": source}))

    # ---- poll --------------------------------------------------------------
    def poll_once(self) -> MissionIntel:
        now_ms = int(time.time() * 1000)
        if self._last_poll_ms is not None:
            self._poll_gap_ms = max(0, now_ms - self._last_poll_ms)
        self._last_poll_ms = now_ms
        feeds: dict[str, FeedStatus] = {}
        per_vehicle: dict[str, dict] = {}
        missions: list[dict] = []
        errors: list[str] = []

        names = list(self._vehicles())
        for name in names:
            st, err = self.mcp.call_tool("uav_task_status", {"vehicle": name})
            if err or not isinstance(st, dict):
                errors.append(f"{name}: {err or 'no payload'}")
                continue
            row, mission = self._vehicle_state(name, st, now_ms)
            per_vehicle[name] = row
            if mission:
                missions.append(mission)
                self._seen_missions[mission["mission_id"]] = mission["vehicle"]
        missions.extend(self._finished_missions(
            {m["mission_id"] for m in missions}, per_vehicle))
        detail = f"mcp:uav_task_status+mission_status @ {self.mcp.url}"
        if self.emit_failures:
            detail += (f"; {self.emit_failures} alarm(s) could not be published "
                       f"({self.last_emit_error})")
        feeds["mission_state"] = FeedStatus(
            ok=bool(per_vehicle) or not names,
            error=("; ".join(errors) or None),
            at_ms=now_ms, detail=detail)

        contacts, cfeed = self._poll_contacts(now_ms)
        feeds["contacts"] = cfeed

        intel = MissionIntel(missions=missions, contacts=contacts,
                             per_vehicle=per_vehicle, feeds=feeds, at_ms=now_ms)
        with self._lock:
            self._intel = intel
            self.polls += 1
        self._refresh_overlay(intel)
        return intel

    def _tick_age(self, name: str, tick: dict, now_ms: int) -> dict:
        """How old the SERVER's measurement is - not how old the poll is.

        `uav_task_status` answers with `self.ticks[vehicle]`, the last verdict
        the server's monitor loop stored. When that loop stops (it swallows a
        per-vehicle `tick_once` failure every pass and keeps going, and
        `stop_monitor` ends it outright) the tool keeps answering, with the
        SAME tick, forever. Loop C's poll succeeds, so `at_ms` and `age_ms` -
        which time the POLL - stay fresh while the AGL they label is frozen.

        The tick carries its own stamp (`t`, the server's `time.monotonic()`).
        It is not comparable to any clock here, but it is comparable to ITSELF:
        the ms at which the bridge first saw the CURRENT value of `t` is the
        newest moment the measurement can be from.

        The number published is the WIDEST the measurement can be, because a
        bound that could be too small is one an operator would read as safety
        it does not have. Three observed terms:

        * how long since this bridge first saw the current `t`;
        * one poll interval - the tick may have been waiting since the poll
          before the one that first carried it. MEASURED between polls, since
          `mission_hz` is a parameter and BRIDGE_CONTRACT's own rate for this
          feed (1 Hz) is not the code's default (2 Hz);
        * the observed gap between the server's ticks - the sample was taken
          up to that long before the tick was stored. MEASURED from consecutive
          `t` stamps, which is the one thing the server's monotonic clock IS
          good for here. Aliased by the poll rate, so it OVERSTATES a server
          ticking faster than loop C polls - the safe direction for a bound
          that is meant to be an upper one.

        Until each interval has been seen twice its half of
        `AGL_FEED_SKEW_FLOOR_MS` stands in, and the source string says so.

        A tick with no usable `t` is not aged to zero - that is the silent
        fallback this exists to remove. It reports `None` and says why.
        """
        stamp = _f(tick.get("t"))
        if stamp is None:
            # The anchor is KEPT. Dropping it would restart the clock at zero
            # if the same frozen stamp came back after a gap - a frozen
            # measurement reading fresh, which is the whole defect.
            raw = tick.get("t", "<absent>")
            return {
                "alt_agl_measured_age_ms": None,
                "alt_agl_measured_age_source": (
                    f"unknown: the MCP server's tick for {name} carries no "
                    f"comparable stamp (t={raw!r}), so a frozen feed cannot be "
                    "told from a live one"),
            }
        seen = self._tick_seen.get(name)
        if seen is None or seen[0] != stamp:
            if seen is not None:
                # Consecutive stamps ARE the server's tick interval. Ignore a
                # non-advancing or absurd gap rather than let it widen the
                # window - a restarted server's monotonic clock jumps.
                gap = (stamp - seen[0]) * 1000.0
                if 0.0 < gap < 60_000.0:
                    self._tick_gap_ms[name] = int(gap)
            self._tick_seen[name] = seen = (stamp, now_ms)
        assumed: list[str] = []
        tick_gap = self._tick_gap_ms.get(name)
        if tick_gap is None:
            assumed.append("the server's tick interval")
            tick_gap = AGL_FEED_SKEW_FLOOR_MS // 2
        poll_gap = self._poll_gap_ms
        if poll_gap is None:
            assumed.append("loop C's poll interval")
            poll_gap = AGL_FEED_SKEW_FLOOR_MS // 2
        since = max(0, now_ms - seen[1])
        note = (f" ({' and '.join(assumed)} not yet measured, assumed at the "
                f"documented rate)" if assumed else "")
        return {
            "alt_agl_measured_age_ms": since + tick_gap + poll_gap,
            "alt_agl_measured_age_source": (
                f"mcp:uav_task_status last_tick t={stamp} first seen by loop C "
                f"{since} ms ago, + {poll_gap} ms poll interval + {tick_gap} ms "
                f"observed gap between server ticks{note}"),
        }

    def _vehicle_state(self, name: str, st: dict,
                       now_ms: int) -> tuple[dict, dict | None]:
        """One `uav_task_status` payload -> vehicle HUD row + mission row."""
        tick = st.get("last_tick") if isinstance(st.get("last_tick"), dict) else {}
        current = st.get("current") if isinstance(st.get("current"), dict) else {}
        bingo = tick.get("bingo") if isinstance(tick.get("bingo"), dict) else {}

        fuel_pct = _f(st.get("fuel_pct"))
        if fuel_pct is None:
            fuel_pct = _f(tick.get("fuel_pct"))
        bingo_pct = _f(bingo.get("bingo_fuel_pct"))
        latched = bool(bingo.get("latched") or st.get("bingo_latched"))
        eta, basis = self._eta_to_bingo(name, tick, fuel_pct, bingo_pct)

        fence, proximity = self._fence_state(tick)
        link_lost = self._link_lost(tick)
        force_rtb = bool(tick.get("force_rtb"))

        mission_id = str(current.get("mission_id") or "")
        active_tool = str(current.get("tool") or "")
        phase = _phase_for(current.get("state") or st.get("state"),
                           active_tool, force_rtb)

        self._alarm_edges(name, latched, fuel_pct, bingo_pct, fence, proximity,
                          link_lost, tick, mission_id, phase)

        row = {
            "fuel_pct": fuel_pct,
            "bingo_fuel_pct": bingo_pct,
            "eta_to_bingo_s": eta,
            "mission": mission_id,
            "track_id": self._track_for(mission_id),
            "fuel_source": basis,
            # The AGL the SERVER measured, so the HUD shows the number the
            # harness flies on (REAL_DATA_INTEGRATION; module docstring), and
            # how old that measurement actually is.
            **_agl_from_tick(tick),
            **self._tick_age(name, tick, now_ms),
        }
        if not mission_id:
            return row, None
        # Pull the mission document the first time this id is seen, so the very
        # first snapshot already carries the tasked polygon (coverage) and the
        # tracked contact - not one poll later.
        self._detail_for(mission_id)
        row["track_id"] = self._track_for(mission_id)

        detail = self._mission_status(mission_id)
        covered, cov_note = self._flown_coverage(mission_id,
                                                 str(detail.get("vehicle") or name))
        mission = {
            "mission_id": mission_id,
            "vehicle": str(detail.get("vehicle") or name),
            "kind": str(detail.get("kind") or ""),
            "phase": phase,
            "active_tool": active_tool,
            "progress_pct": _f(detail.get("progress_pct")
                               if detail.get("progress_pct") is not None
                               else current.get("progress_pct")),
            "waypoint": {
                "index": current.get("waypoint") if current.get("waypoint") is not None
                else detail.get("waypoint"),
                "of": current.get("waypoints_total") or detail.get("waypoints_total"),
            },
            "eta_s": _f(detail.get("eta_s") if detail.get("eta_s") is not None
                        else current.get("eta_s")),
            "fuel_pct": fuel_pct,
            "bingo_fuel_pct": bingo_pct,
            "coverage_pct": covered,
            "coverage_basis": cov_note,
            "safety": {"geofence": fence, "proximity_m": proximity,
                       "bingo_latched": latched},
            "incomplete_reason": (detail.get("mission_status")
                                  or st.get("mission_status") or None),
        }
        return row, mission

    def _eta_to_bingo(self, name: str, tick: dict, fuel_pct: float | None,
                      bingo_pct: float | None) -> tuple[float | None, str]:
        """Seconds of useful mission time left, from the measured burn rate.

        Rate comes from the change in the server's own fuel integral between
        two observed ticks. Without two samples, or with no burn yet, the
        answer is None with the reason attached - never a made-up number.
        """
        rec = tick.get("fuel_record") if isinstance(tick.get("fuel_record"), dict) else {}
        burned, elapsed = _f(rec.get("burned_pct")), _f(rec.get("elapsed_s"))
        if fuel_pct is None or bingo_pct is None:
            return None, "unavailable: MCP reported no fuel/BINGO state"
        last = self._fuel_prev.get(name)
        rate = None
        basis = ""
        if burned is not None and elapsed is not None:
            if last is not None:
                d_burn, d_t = burned - last[0], elapsed - last[1]
                if d_t >= 0.5 and d_burn > 0.0:
                    rate, basis = d_burn / d_t, "measured burn rate between ticks"
            if rate is None and elapsed > 0.5 and burned > 0.0:
                rate, basis = burned / elapsed, "cumulative burn rate since start"
            self._fuel_prev[name] = (burned, elapsed)
        if rate is None or rate <= 0.0:
            return None, "unavailable: no fuel burned yet, ETA undefined"
        # Carry the rate itself: an ETA of days on a parked aircraft is a true
        # extrapolation of an idle burn rate, and an operator can only tell
        # that from the basis. An unexplained number would read as a fault.
        basis = f"{basis} ({rate:.5f} %/s)"
        margin = fuel_pct - bingo_pct
        if margin <= 0.0:
            return 0.0, f"at or past BINGO ({basis})"
        return round(margin / rate, 1), basis

    @staticmethod
    def _link_lost(tick: dict) -> bool:
        """Is the datalink down, per the server's own link machine (M9)?

        `LostLinkMonitor.to_dict()` reports `state` (the LinkState value) and
        `action`, NOT a boolean - reading a `lost` key that is never present
        made this always False, i.e. the lost-link alarm could not fire on real
        data at all. The RTB reason is checked too, so an escalation that has
        already committed the aircraft is never missed.
        """
        link = tick.get("link") if isinstance(tick.get("link"), dict) else {}
        state = str(link.get("state") or "").lower()
        if state in ("loal", "lost", "down"):
            return True
        if link.get("lost") is True:
            return True
        return "lost_link" in (tick.get("rtb_reasons") or [])

    @staticmethod
    def _fence_state(tick: dict) -> tuple[str, float | None]:
        """Geofence word + metres to the edge, from the server's violations."""
        violations = tick.get("violations")
        if not isinstance(violations, list):
            return ("unknown", None)
        state, proximity = "ok", None
        for v in violations:
            if not isinstance(v, dict):
                continue
            kind = v.get("kind")
            if kind == "geofence":
                state, proximity = "breach", _f(v.get("value"))
            elif kind == "geofence_proximity" and state != "breach":
                state, proximity = "proximity", _f(v.get("value"))
        return state, proximity

    def _alarm_edges(self, name: str, latched: bool, fuel_pct: float | None,
                     bingo_pct: float | None, fence: str,
                     proximity: float | None, link_lost: bool, tick: dict,
                     mission_id: str, phase: str) -> None:
        if self._seen_bingo.get(name) != latched:
            self._seen_bingo[name] = latched
            if latched:
                self._emit(Alarm(
                    kind="bingo", vehicle=name, mission_id=mission_id,
                    message=(f"BINGO fuel - forcing RTB (fuel {fuel_pct}% at/below "
                             f"BINGO {bingo_pct}%)"),
                    detail={"fuel_pct": fuel_pct, "bingo_fuel_pct": bingo_pct}))

        if self._seen_fence.get(name) != fence:
            prior = self._seen_fence.get(name)
            self._seen_fence[name] = fence
            if fence == "breach":
                self._emit(Alarm(
                    kind="geofence_breach", vehicle=name, mission_id=mission_id,
                    message=f"geofence BREACH - {abs(proximity or 0.0):.0f} m outside the AO",
                    detail={"margin_m": proximity}))
            elif fence == "proximity" and prior != "breach":
                self._emit(Alarm(
                    kind="geofence_proximity", vehicle=name, mission_id=mission_id,
                    message=f"geofence proximity - {proximity:.0f} m to the AO edge"
                            if proximity is not None else "geofence proximity",
                    detail={"margin_m": proximity}))

        if self._seen_link.get(name) != link_lost:
            self._seen_link[name] = link_lost
            if link_lost:
                link = tick.get("link") if isinstance(tick.get("link"), dict) else {}
                plan = link.get("plan") or (self._mission_detail.get(mission_id, {})
                                            .get("lost_link_plan"))
                named = str(link.get("action") or "")
                if not named and isinstance(plan, dict):
                    named = str(plan.get("behaviour") or plan.get("action")
                                or plan.get("name") or "")
                self._emit(Alarm(
                    kind="lost_link", vehicle=name, mission_id=mission_id,
                    message=("link lost - lost-link plan "
                             f"{named or 'running'} executing"),
                    detail={"lost_link_plan": plan}))
            else:
                self._emit(Alarm(kind="link_restored", vehicle=name,
                                 mission_id=mission_id, message="datalink restored"))

        if mission_id and self._seen_phase.get(mission_id) != phase:
            prior = self._seen_phase.get(mission_id)
            self._seen_phase[mission_id] = phase
            self._emit(Alarm(
                kind="mission_phase", vehicle=name, mission_id=mission_id,
                message=f"{mission_id} {prior or 'new'} -> {phase}",
                detail={"from": prior, "to": phase}))

    def _finished_missions(self, active: set[str],
                           per_vehicle: dict[str, dict]) -> list[dict]:
        """Rows for missions whose flying task has drained (see MISSION_RETAIN_S).

        Their phase comes from `mission_status`, so a mission that ended
        `incomplete - fuel` after a BINGO force-RTB still says so.
        """
        out: list[dict] = []
        now = time.monotonic()
        for mid, vehicle in list(self._seen_missions.items()):
            if mid in active:
                self._finished_at.pop(mid, None)
                continue
            first = self._finished_at.setdefault(mid, now)
            if now - first > MISSION_RETAIN_S:
                self._seen_missions.pop(mid, None)
                self._finished_at.pop(mid, None)
                self._seen_phase.pop(mid, None)
                continue
            detail = self._mission_status(mid)
            if not detail:
                continue
            row = per_vehicle.get(vehicle) or {}
            phase = _phase_for(detail.get("state"), None)
            if self._seen_phase.get(mid) != phase:
                prior = self._seen_phase.get(mid)
                self._seen_phase[mid] = phase
                self._emit(Alarm(
                    kind="mission_phase", vehicle=vehicle, mission_id=mid,
                    message=f"{mid} {prior or 'new'} -> {phase}",
                    detail={"from": prior, "to": phase}))
            covered, cov_note = self._flown_coverage(mid, vehicle)
            out.append({
                "mission_id": mid, "vehicle": vehicle,
                "kind": str(detail.get("kind") or ""), "phase": phase,
                "active_tool": "",
                "progress_pct": _f(detail.get("progress_pct")),
                "waypoint": {"index": detail.get("waypoint"),
                             "of": detail.get("waypoints_total")},
                "eta_s": _f(detail.get("eta_s")),
                "fuel_pct": row.get("fuel_pct"),
                "bingo_fuel_pct": row.get("bingo_fuel_pct"),
                "coverage_pct": covered, "coverage_basis": cov_note,
                "safety": {"geofence": "unknown", "proximity_m": None,
                           "bingo_latched": bool(detail.get("bingo_latched"))},
                "incomplete_reason": detail.get("mission_status") or None,
            })
        return out

    def _mission_status(self, mission_id: str) -> dict:
        out, err = self.mcp.call_tool("mission_status",
                                      {"mission_handle": mission_id})
        if err or not isinstance(out, dict):
            return {}
        return out

    def _detail_for(self, mission_id: str) -> dict:
        """`uav://mission/{id}`, fetched once per mission and cached.

        A resource the Wave-3 server has not registered is remembered as
        missing (and reported on the overlay) rather than re-read every poll.
        """
        if mission_id in self._mission_detail:
            return self._mission_detail[mission_id]
        doc, err = self.mcp.read_resource(f"uav://mission/{mission_id}")
        if err or not isinstance(doc, dict) or not isinstance(doc.get("mission"), dict):
            self._detail_missing.add(mission_id)
            return {}
        self._detail_missing.discard(mission_id)
        self._mission_detail[mission_id] = doc["mission"]
        return doc["mission"]

    def _track_for(self, mission_id: str) -> str:
        meta = (self._mission_detail.get(mission_id) or {}).get("meta")
        if isinstance(meta, dict):
            return str(meta.get("track_id") or "")
        return ""

    # ---- contacts ----------------------------------------------------------
    def _poll_contacts(self, now_ms: int) -> tuple[list[dict], FeedStatus]:
        out, err = self.mcp.call_tool("uav_list_tracks", {})
        if err or not isinstance(out, dict):
            return [], FeedStatus(False, err or "no payload", now_ms,
                                  "mcp:uav_list_tracks")
        rows = out.get("tracks")
        if not isinstance(rows, list):
            return [], FeedStatus(False, "uav_list_tracks returned no tracks[]",
                                  now_ms, "mcp:uav_list_tracks")

        ids = [str(r.get("track_id")) for r in rows if isinstance(r, dict)]
        key = "|".join(sorted(ids))
        # "on change" (BRIDGE_CONTRACT feed table): the threat assessment is
        # only re-run when the roster changes, because uav_assess_threat writes
        # an audit record and pulls telemetry on every call.
        if key != self._track_key:
            self._track_key = key
            self._refresh_threat()
            for tid in ids:
                if tid and tid not in self._seen_tracks:
                    self._seen_tracks.add(tid)
                    row = next((r for r in rows if r.get("track_id") == tid), {})
                    self._emit(Alarm(
                        kind="detection", track_id=tid,
                        message=(f"new contact {tid}: "
                                 f"{row.get('category') or 'unclassified'} "
                                 f"({row.get('confidence_level') or 'unknown'})"),
                        detail={"category": row.get("category"),
                                "confidence": row.get("confidence_level")}))

        contacts = [c for c in (self._contact(r) for r in rows if isinstance(r, dict))
                    if c]
        detail = "mcp:uav_list_tracks"
        if not self._threat and contacts:
            detail += "; threat_level unavailable (uav_assess_threat not served)"
        return contacts, FeedStatus(True, None, now_ms, detail)

    def _refresh_threat(self) -> None:
        names = list(self._vehicles())
        if not names:
            self._threat = {}
            return
        out, err = self.mcp.call_tool("uav_assess_threat", {"vehicle": names[0]})
        if err or not isinstance(out, dict):
            # Not fatal and not faked: contacts keep flowing with
            # threat_level=None and `/snapshot.feeds` says the tool is absent.
            self._threat = {}
            return
        levels: dict[str, str] = {}
        for a in out.get("assessments") or []:
            if not isinstance(a, dict) or not a.get("track_id"):
                continue
            tid = str(a["track_id"])
            levels[tid] = str(a.get("threat_level") or "")
            cap = (a.get("assessment") or {}).get("capability") \
                if isinstance(a.get("assessment"), dict) else {}
            envelope = _f(a.get("envelope_m"))
            acquisition = None
            if isinstance(cap, dict):
                envelope = _f(cap.get("envelope_m")) if envelope is None else envelope
                acquisition = _f(cap.get("acquisition_range_m"))
            # An engagement ring and an acquisition ring are different facts
            # (M14 framing: this is sensor-posture advice, not targeting).
            self._threat_rings[tid] = {"engagement": envelope,
                                       "acquisition": acquisition}
        self._threat = levels

    def _contact(self, row: dict) -> dict | None:
        tid = str(row.get("track_id") or "")
        if not tid:
            return None
        loc = row.get("location") if isinstance(row.get("location"), dict) else {}
        lat, lon = _f(loc.get("lat")), _f(loc.get("lon"))
        if lat is None:
            lat = _f(row.get("lat"))
        if lon is None:
            lon = _f(row.get("lon"))
        seen = row.get("time") if isinstance(row.get("time"), dict) else {}
        last = _f(seen.get("last_seen"))
        if last is None:
            last = _f(seen.get("epoch"))
        alt = _f(loc.get("alt_m"))
        conf = row.get("confidence_level")
        if not isinstance(conf, str):
            block = row.get("confidence")
            conf = block if isinstance(block, str) else (
                block.get("level") if isinstance(block, dict) else None)
        if lat is None or lon is None:
            where = ""
        else:
            where = f"{lat:.5f}, {lon:.5f}" + (f" @ {alt:.0f} m" if alt is not None else "")
        return {
            "track_id": tid,
            "category": str(row.get("category") or ""),
            "confidence": str(conf or ""),
            # The CONTACT's position, never the observer's (BRIDGE_CONTRACT).
            "location": {"lat": lat, "lon": lon, "alt_m": alt},
            "last_seen_ms": int(last * 1000) if last is not None else None,
            "threat_level": self._threat.get(tid),
            "salute": {
                "size": _salute_text(row.get("size"), "text", "element"),
                "activity": _salute_text(row.get("activity"), "text", "code"),
                "location": where,
                "unit": _salute_text(row.get("unit"), "text", "assessment"),
                "time": _salute_text(seen, "iso"),
                "equipment": _salute_text(row.get("equipment"), "text", "platform"),
            },
        }

    # ---- coverage ----------------------------------------------------------
    def _flown_coverage(self, mission_id: str,
                        vehicle: str) -> tuple[float | None, str]:
        """Coverage ACTUALLY FLOWN (M1), scored by missions.coverage_of_path.

        Needs a tasked polygon and a swath, both of which only a grid search
        carries. Anything else reports None with the reason - a route recon has
        no area to be a percentage of, and inventing one would overstate it.
        """
        detail = self._mission_detail.get(mission_id) or {}
        meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
        params = meta.get("_plan_params") if isinstance(meta.get("_plan_params"), dict) else {}
        polygon = params.get("polygon")
        swath = _f(meta.get("swath_m"))
        if not isinstance(polygon, list) or len(polygon) < 3 or not swath:
            return None, ("not area-scored: this mission kind has no tasked "
                          "polygon/swath")
        path = self._flown(vehicle)
        if len(path) < 1:
            return 0.0, "flown: nothing flown yet"
        try:
            cov = coverage_of_path(polygon, path, swath, basis="flown")
        except ValueError as e:
            return None, f"not scored: {e}"
        return round(cov.coverage_pct, 2), cov.method

    # ---- safety resource: the geofence layer AND the active theater ---------
    def _refresh_geofence(self) -> bool:
        """Read `uav://safety/geofence` once, then retry on a slow clock.

        Returns True only when a document was newly stored, so the caller can
        rebuild the overlay on the poll the layer actually arrived.

        A failure is RECORDED, not discarded. The old code did
        `if not err: self._geofence = doc` and dropped `err` on the floor,
        which was survivable while this only fed a map layer and is not now
        that it also answers "which theater am I flying?".
        """
        if self._geofence is not None:
            return False
        now = time.monotonic()
        if now - self._geofence_at <= GEOFENCE_RETRY_S:
            return False
        self._geofence_at = now
        doc, err = self.mcp.read_resource("uav://safety/geofence")
        if err or not isinstance(doc, dict):
            with self._lock:
                self._geofence_error = (
                    err or "uav://safety/geofence returned no JSON object")
            return False
        with self._lock:
            self._geofence = doc
            self._geofence_error = None
            self._geofence_ms = int(time.time() * 1000)
        return True

    # ---- overlay -----------------------------------------------------------
    def _refresh_overlay(self, intel: MissionIntel) -> None:
        """Rebuild only on MISSION-STATE change, never on a fixed tick.

        The key is mission identity, phase, waypoint, progress (to 1%) and the
        track roster. Progress is itself server-derived mission state (T2), so
        keying on it refreshes the flown line and the coverage polygon as the
        mission advances without degenerating into a timer.
        """
        parts = [f"{m['mission_id']}:{m['phase']}:{m['active_tool']}:"
                 f"{(m.get('waypoint') or {}).get('index')}:"
                 f"{round(m.get('progress_pct') or 0.0)}"
                 for m in intel.missions]
        parts.append("tracks=" + (self._track_key or ""))
        key = "|".join(parts)
        # The safety resource carries the geofence layer and the active
        # theater; `_refresh_geofence` owns its clock. Rebuild when the mission
        # state moved, or on the poll the layer first landed.
        arrived = self._refresh_geofence()
        if key == self._overlay_key and not arrived:
            return
        overlay = self.build_overlay(intel)
        with self._lock:
            self._overlay = overlay
            self._overlay_key = key

    def build_overlay(self, intel: MissionIntel) -> dict:
        features: list[dict] = []
        notes: list[str] = []

        fence = (self._geofence or {})
        theater = fence.get("theater") if isinstance(fence.get("theater"), dict) else {}
        # The ENFORCED polygon, not the theater block's decorative copy of it.
        # `geofence` is `SafetyEnvelope.to_dict()["geofence"]` - the boundary
        # the server actually gates every waypoint against - while `theater.ao`
        # is a label that can be (and on the shipped launcher IS) a different
        # theater entirely: a bridge flown at Natanz drew the Redmond AO box,
        # 10,000 km away, so the operator saw margin where there was none.
        ring = fence.get("geofence")
        source = "envelope.geofence"
        if not (isinstance(ring, list) and len(ring) >= 3):
            ring, source = theater.get("ao"), "theater.ao"
        if isinstance(ring, list) and len(ring) >= 3:
            coords = [[float(p[1]), float(p[0])] for p in ring]
            coords.append(list(coords[0]))
            props = {"kind": "geofence", "source": source,
                     "theater": theater.get("id"), "label": theater.get("label")}
            if source != "envelope.geofence":
                # Never let a fallback pass for the enforced boundary.
                props["enforced"] = False
                notes.append(
                    "geofence: uav://safety/geofence served no envelope "
                    "polygon; drawing the theater AO, which the server does "
                    "NOT gate against")
            else:
                props["enforced"] = True
                ao = theater.get("ao")
                if isinstance(ao, list) and len(ao) >= 3 and (
                        [[float(p[0]), float(p[1])] for p in ao]
                        != [[float(p[0]), float(p[1])] for p in ring]):
                    # Both exist and disagree: say so rather than pick quietly.
                    notes.append(
                        "geofence: the enforced envelope and the server's "
                        f"theater block ({theater.get('id')}) disagree; the "
                        "enforced polygon is drawn")
            features.append(_feature("geofence", "Polygon", [coords], props,
                                     fid="geofence"))
        else:
            notes.append("geofence: uav://safety/geofence not served")

        for m in intel.missions:
            features.extend(self._mission_features(m, notes))

        features.extend(self._contact_features(intel.contacts))

        out = {"type": "FeatureCollection", "features": features,
               "generatedAtMs": int(time.time() * 1000),
               "missions": [m["mission_id"] for m in intel.missions]}
        if notes:
            # Foreign members are legal GeoJSON; the renderer ignores them and
            # an operator can see exactly which layer is missing and why.
            out["degraded"] = notes
        return out

    def _mission_features(self, mission: dict, notes: list[str]) -> list[dict]:
        mid = mission["mission_id"]
        vehicle = mission["vehicle"]
        detail = self._mission_detail.get(mid)
        feats: list[dict] = []
        if not detail:
            notes.append(f"{mid}: uav://mission/{mid} not served - no route/grid")
        else:
            wps = detail.get("waypoints")
            if isinstance(wps, list) and len(wps) >= 2:
                line = [[_f(w.get("lon")), _f(w.get("lat"))] for w in wps
                        if isinstance(w, dict)]
                line = [p for p in line if p[0] is not None and p[1] is not None]
                if len(line) >= 2:
                    feats.append(_feature(
                        "route", "LineString", line,
                        {"kind": "route", "mission_id": mid, "vehicle": vehicle,
                         "waypoints": len(line)}, fid=f"route:{mid}"))
            reached = (mission.get("waypoint") or {}).get("index")
            for i, w in enumerate(wps if isinstance(wps, list) else []):
                if not isinstance(w, dict):
                    continue
                lat, lon = _f(w.get("lat")), _f(w.get("lon"))
                if lat is None or lon is None:
                    continue
                feats.append(_feature(
                    "waypoint", "Point", [lon, lat],
                    {"kind": "waypoint", "index": i, "mission_id": mid,
                     "reached": bool(reached is not None and i < int(reached))},
                    fid=f"wp:{mid}:{i}"))
            meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
            params = meta.get("_plan_params") if isinstance(meta.get("_plan_params"), dict) else {}
            poly = params.get("polygon")
            if isinstance(poly, list) and len(poly) >= 3:
                # `grid` is the tasked search area with the DERIVED spacing on
                # it (M1) - the route polyline above already draws the lanes,
                # so drawing them twice would tell the operator nothing new.
                coords = [[float(p[1]), float(p[0])] for p in poly]
                coords.append(list(coords[0]))
                feats.append(_feature(
                    "grid", "Polygon", [coords],
                    {"kind": "grid", "mission_id": mid,
                     "pattern": meta.get("pattern"),
                     "lane_spacing_m": meta.get("lane_spacing_m"),
                     "lane_spacing_derived_m": meta.get("lane_spacing_derived_m"),
                     "lanes": meta.get("lanes"),
                     "lanes_required": meta.get("lanes_required"),
                     "swath_m": meta.get("swath_m"),
                     "truncated": detail.get("truncated")},
                    fid=f"grid:{mid}"))

        path = self._flown(vehicle)
        if len(path) >= 2:
            feats.append(_feature(
                "flown", "LineString", [[lon, lat] for lat, lon in path],
                {"kind": "flown", "mission_id": mid, "vehicle": vehicle,
                 "points": len(path)}, fid=f"flown:{mid}"))
        swath = None
        if detail:
            meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
            swath = _f(meta.get("swath_m"))
        if swath and path:
            try:
                ring = corridor_ring(path, swath)
            except ValueError:
                ring = None
            if ring:
                feats.append(_feature(
                    "coverage", "Polygon", [ring],
                    {"kind": "coverage", "mission_id": mid, "vehicle": vehicle,
                     "swath_m": swath, "basis": "flown",
                     "coverage_pct": mission.get("coverage_pct"),
                     "method": mission.get("coverage_basis")},
                    fid=f"coverage:{mid}"))
        return feats

    def _contact_features(self, contacts: list[dict]) -> list[dict]:
        feats: list[dict] = []
        rings = self._threat_rings
        for c in contacts:
            loc = c.get("location") or {}
            lat, lon = _f(loc.get("lat")), _f(loc.get("lon"))
            if lat is None or lon is None:
                continue
            tid = c["track_id"]
            feats.append(_feature(
                "target", "Point", [lon, lat],
                {"kind": "target", "track_id": tid,
                 "confidence": c.get("confidence"),
                 "category": c.get("category"),
                 "threat_level": c.get("threat_level")}, fid=f"target:{tid}"))
            for name in ("engagement", "acquisition"):
                radius = (rings.get(tid) or {}).get(name)
                if not radius or radius <= 0:
                    continue
                feats.append(_feature(
                    "threat_ring", "Polygon", [circle_ring(lat, lon, radius)],
                    {"kind": "threat_ring", "track_id": tid,
                     "radius_m": radius, "ring": name},
                    fid=f"ring:{name}:{tid}"))
        return feats

    # ---- thread ------------------------------------------------------------
    def loop(self, hz: float = 2.0) -> None:
        dt = 1.0 / max(0.1, hz)
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.poll_once()
            except Exception as e:
                # Keep the last good picture; do NOT blank it. The failure is
                # recorded on the feed so it reads as stale, not as "no
                # missions" (BRIDGE_CONTRACT rule 3).
                with self._lock:
                    self._intel.feeds["loop_c"] = FeedStatus(
                        False, f"{type(e).__name__}: {e}",
                        int(time.time() * 1000),
                        "loop C poll raised; the cached picture below is stale")
            self._stop.wait(max(dt - (time.monotonic() - t0), 0.05))


def _feature(kind: str, geometry: str, coordinates, properties: dict,
             fid: str | None = None) -> dict:
    out = {"type": "Feature",
           "geometry": {"type": geometry, "coordinates": coordinates},
           "properties": properties}
    if fid:
        out["id"] = fid
    return out


def _empty_overlay(reason: str) -> dict:
    """An empty FeatureCollection that SAYS why it is empty."""
    return {"type": "FeatureCollection", "features": [],
            "degraded": [reason], "generatedAtMs": int(time.time() * 1000)}


# ---------------------------------------------------------------------------
# bridge state (loops A + B)
# ---------------------------------------------------------------------------

class BridgeState:
    def __init__(self, adapter: AirSimAdapter, token: str,
                 feed: MissionFeed | None = None, hub: EventHub | None = None):
        self.adapter = adapter
        self.token = token
        self._lock = threading.Lock()
        self._cache: dict[str, VehicleSnapshot] = {}
        self._cam_cache: dict[tuple[str, str, int], tuple[float, bytes]] = {}
        self._cam_subs: dict[tuple[str, str, int], float] = {}
        self._flown: dict[str, deque] = {}
        self._stop = threading.Event()
        self.hub = hub or EventHub()
        self.feed = feed
        self.started_ms = int(time.time() * 1000)
        self._roster: list[str] | None = None
        self._roster_at = 0.0

    # ---- overlay (served from loop C's cache) -------------------------------
    @property
    def mission_overlay(self) -> dict:
        if self.feed is None:
            return _empty_overlay("no mission feed configured on this bridge")
        return self.feed.overlay()

    # ---- flown track -------------------------------------------------------
    def flown(self, vehicle: str) -> list[tuple[float, float]]:
        with self._lock:
            return list(self._flown.get(vehicle) or ())

    def _record_flown(self, snap: VehicleSnapshot) -> None:
        track = self._flown.setdefault(snap.name, deque(maxlen=FLOWN_MAX_POINTS))
        if track:
            lat, lon = track[-1]
            if _ground_m(lat, lon, snap.latitude, snap.longitude) < FLOWN_MIN_STEP_M:
                return
        track.append((snap.latitude, snap.longitude))

    # ---- loop A: telemetry -------------------------------------------------
    def roster(self) -> list[str]:
        """Vehicle names, cached for ROSTER_TTL_S.

        `listVehicles` is a msgpack-rpc round trip, and the roster changes on
        the scale of a whole sim run. Asking for it on every 10 Hz tick spent
        half of loop A's RPC budget re-reading a constant (T3).
        """
        now = time.monotonic()
        if self._roster is None or now - self._roster_at > ROSTER_TTL_S:
            self._roster = self.adapter.vehicles()
            self._roster_at = now
        return self._roster

    def telemetry_loop(self, hz: float = 10.0):
        dt = 1.0 / hz
        while not self._stop.is_set():
            t0 = time.monotonic()
            for name in self.roster():
                self.tick_vehicle(name)
            elapsed = time.monotonic() - t0
            time.sleep(max(dt - elapsed, 0.005))

    def tick_vehicle(self, name: str) -> VehicleSnapshot | None:
        """One telemetry sample, enriched from loop C's cache (no I/O here)."""
        snap = self.adapter.snapshot(name)
        now_ms = int(time.time() * 1000)
        if snap is None:
            # Degradation is state, not silence: keep the last fix, but mark it
            # stale and carry the error (BRIDGE_CONTRACT rule 3).
            with self._lock:
                prior = self._cache.get(name)
                if prior is not None:
                    prior.stale_ms = now_ms - prior.timestamp_ms
                    prior.telemetry_error = self.adapter.last_error
            return None
        self.enrich(snap, now_ms)
        with self._lock:
            self._cache[name] = snap
            self._record_flown(snap)
        if self.feed is not None:
            self.feed.note_datum(name, snap.datum_degraded, snap.datum_source)
        return snap

    def enrich(self, snap: VehicleSnapshot, now_ms: int | None = None
               ) -> VehicleSnapshot:
        """Fold loop C's cached mission/fuel/AGL state onto a vehicle row."""
        if self.feed is None:
            snap.fuel_source = "unavailable: no mission feed configured"
            snap.alt_agl_reason = (
                "no mission feed is configured on this bridge, so no measured "
                f"AGL can be read; {AGL_LAUNCH_DATUM_NOTE}")
            snap.set_launch_datum_check(
                "not checked: no mission feed is configured on this bridge")
            return snap
        intel = self.feed.intel()
        row = intel.per_vehicle.get(snap.name)
        if not row:
            feeds = intel.feeds.get("mission_state")
            why = (feeds.error if feeds and feeds.error
                   else f"MCP reported no state for {snap.name}")
            snap.fuel_source = f"unavailable: {why}"
            snap.alt_agl_reason = (
                f"no measured AGL: {why}; {AGL_LAUNCH_DATUM_NOTE}")
            snap.set_launch_datum_check(f"not checked: {why}")
            return snap
        snap.fuel_pct = row.get("fuel_pct")
        snap.bingo_fuel_pct = row.get("bingo_fuel_pct")
        snap.eta_to_bingo_s = row.get("eta_to_bingo_s")
        snap.mission = row.get("mission") or ""
        snap.track_id = row.get("track_id") or ""
        snap.fuel_source = row.get("fuel_source") or "mcp:uav_task_status"
        self._enrich_agl(snap, row, intel.at_ms,
                         int(time.time() * 1000) if now_ms is None else now_ms)
        return snap

    @staticmethod
    def _enrich_agl(snap: VehicleSnapshot, row: dict, at_ms: int,
                    now_ms: int) -> None:
        """Republish the MCP server's AGL as the row's AGL. ONE source of truth.

        The bridge keeps its own launch-datum figure on the row beside it
        (`alt_agl_launch_datum_m`, current at loop A's 10 Hz) and cross-checks
        the server's against it: both processes derive that number from the same
        NED origin, so a disagreement means they were handed different homes and
        the measured AGL being republished here does not belong to the aircraft
        this bridge is drawing. That is published, not swallowed.

        The AGL's AGE travels with it (`set_agl` will not take one without the
        other) and the frame cross-check states its outcome in words - see
        `_check_launch_datum_frame` for why both of those exist.

        `row` may come from an injected `state_source` (see `create_app`), so
        every key is read defensively - but a missing key means "no measured
        AGL, and here is why", never a substituted number.
        """
        measured_age_ms = _int_or_none(row.get("alt_agl_measured_age_ms"))
        if measured_age_ms is not None and measured_age_ms < 0:
            measured_age_ms = None  # an age before the measurement is not an age
        measured_age_source = (
            str(row.get("alt_agl_measured_age_source") or "").strip()
            or (f"unknown: the mission feed published no measurement age for "
                f"{snap.name}, so a frozen MCP feed cannot be told from a live "
                "one"))
        # Never silently "fresh": if the source claimed an age and it was not a
        # usable integer, say THAT rather than inherit a reason that describes
        # a different failure.
        if measured_age_ms is None and row.get("alt_agl_measured_age_ms") is not None:
            measured_age_source = (
                f"unknown: the mission feed published alt_agl_measured_age_ms="
                f"{row.get('alt_agl_measured_age_ms')!r} for {snap.name}, "
                "which is not a whole number of milliseconds >= 0")
        BridgeState._check_launch_datum_frame(snap, row, measured_age_ms,
                                              max(0, now_ms - at_ms))
        value = row.get("alt_agl_m")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            why = (str(row.get("alt_agl_reason") or "").strip()
                   or f"the mission feed published no alt_agl_m for {snap.name}")
            snap.alt_agl_reason = f"{why}; {AGL_LAUNCH_DATUM_NOTE}"
            return
        if not isinstance(row.get("alt_agl_is_real"), bool):
            # Same refusal as `_agl_from_tick`, repeated here because this row
            # may not have come from it: an unlabelled altitude is not adopted.
            snap.alt_agl_reason = (
                f"the mission feed published an alt_agl_m for {snap.name} with "
                "no alt_agl_is_real; an altitude whose provenance is unstated "
                f"is not published as measured; {AGL_LAUNCH_DATUM_NOTE}")
            return
        is_real = row["alt_agl_is_real"]
        snap.set_agl(
            float(value), is_real=is_real,
            source=(str(row.get("alt_agl_source") or "").strip()
                    or "mcp:uav_task_status (the server named no alt_agl_source)"),
            reason=(None if is_real else
                    (str(row.get("alt_agl_reason") or "").strip()
                     or "the MCP server reports alt_agl_is_real=false")),
            at_ms=at_ms, age_ms=max(0, now_ms - at_ms),
            measured_age_ms=measured_age_ms,
            measured_age_source=measured_age_source)

    @staticmethod
    def _check_launch_datum_frame(snap: VehicleSnapshot, row: dict,
                                  measured_age_ms: int | None,
                                  poll_age_ms: int) -> None:
        """Do the two processes share the NED origin? Always answer in words.

        Republishing the server's AGL is only sound while both derive the
        launch datum from the same home, so the server's own
        `alt_agl_launch_datum_m` is compared with this bridge's. Two things
        that used to be swallowed are now said out loud:

        1. A comparison that could NOT be made (the feed published no
           launch-datum figure, or an unusable one) left
           `alt_agl_launch_datum_mismatch_m: None` - exactly what a comparison
           that PASSED looks like. An unrun check must never wear the face of a
           passed one.

        2. The two heights are sampled at different moments - the server's on
           its 0.5 s monitor tick, this bridge's on loop A's 10 Hz - so an
           aircraft that is simply CLIMBING makes them differ. On the real
           stack that published a 39.9 m "different frames" alarm on a stack
           whose frames were identical. The tolerance is therefore widened by
           the height this aircraft could actually have travelled in the skew
           window (`|vz|` x the window), and the arithmetic is published so a
           reader can check it. A genuine frame offset is a constant hundreds
           of metres and survives that widening; motion does not.
        """
        served = row.get("alt_agl_launch_datum_m")
        if not isinstance(served, (int, float)) or isinstance(served, bool):
            snap.set_launch_datum_check(
                "not checked: the mission feed published no comparable "
                f"alt_agl_launch_datum_m for {snap.name} "
                f"({served!r}), so the two processes cannot be shown to share "
                "a NED origin")
            return
        delta = float(served) - snap.alt_agl_launch_datum_m
        skew_ms = max(poll_age_ms if measured_age_ms is None else measured_age_ms,
                      AGL_FEED_SKEW_FLOOR_MS)
        skew_s = skew_ms / 1000.0
        rate = abs(float(snap.vz))
        allowed = AGL_FRAME_TOLERANCE_M + rate * skew_s
        if abs(delta) <= AGL_FRAME_TOLERANCE_M:
            snap.set_launch_datum_check(AGL_FRAME_AGREED)
        elif abs(delta) <= allowed:
            # NOT "all is well": the skew can hide a small genuine offset, and
            # saying otherwise would be the substituted certainty this file
            # keeps removing. The delta is published in the sentence, and the
            # cover is temporary - the moment the aircraft stops climbing the
            # allowance collapses to AGL_FRAME_TOLERANCE_M and a real offset
            # surfaces. A skew this can hide is bounded by |vz| x the window.
            snap.set_launch_datum_check(
                f"inconclusive: {delta:+.1f} m apart, within the "
                f"{allowed:.1f} m this aircraft's {rate:.1f} m/s vertical rate "
                f"can produce over {skew_s:.2f} s of feed skew - not evidence "
                "of a frame disagreement, and not proof of agreement either")
        else:
            snap.set_launch_datum_check(
                f"MISMATCH: the server's launch-datum AGL is {delta:+.1f} m "
                f"from this bridge's, more than the {allowed:.1f} m that "
                f"{rate:.1f} m/s of vertical rate explains over {skew_s:.2f} s "
                "- either the two processes were handed different homes (and "
                "the measured AGL does not belong to this aircraft) or that "
                f"measurement is {_age_words(measured_age_ms)} old",
                mismatch_m=round(delta, 3))

    def get_snapshot(self, name: str) -> VehicleSnapshot | None:
        with self._lock:
            return self._cache.get(name)

    def all_snapshots(self) -> list[VehicleSnapshot]:
        with self._lock:
            return list(self._cache.values())

    # ---- loop B: camera (subscriber-gated, TTL-expired) --------------------
    def camera_loop(self, hz: float = 3.0):
        dt = 1.0 / hz
        while not self._stop.is_set():
            time.sleep(dt)
            for (veh, cam, it) in self.active_camera_subs():
                png = self.adapter.camera_png(veh, cam, it)
                if png:
                    with self._lock:
                        self._cam_cache[(veh, cam, it)] = (time.time(), png)

    def active_camera_subs(self) -> list[tuple[str, str, int]]:
        """Live subscriptions only. A tab that closed stops costing frames.

        Without the TTL, `subscribe_camera` had no release path at all and
        loop B kept pulling images forever - the "subscriber-gated" half of the
        T3 budget was not real.
        """
        now = time.time()
        with self._lock:
            dead = [k for k, seen in self._cam_subs.items()
                    if now - seen > CAMERA_SUB_TTL_S]
            for k in dead:
                self._cam_subs.pop(k, None)
                self._cam_cache.pop(k, None)
            return list(self._cam_subs)

    def subscribe_camera(self, veh: str, cam: str, it: int):
        with self._lock:
            self._cam_subs[(veh, cam, it)] = time.time()

    def unsubscribe_camera(self, veh: str, cam: str, it: int):
        with self._lock:
            self._cam_subs.pop((veh, cam, it), None)
            self._cam_cache.pop((veh, cam, it), None)

    def get_camera(self, veh: str, cam: str, it: int) -> bytes | None:
        with self._lock:
            entry = self._cam_cache.get((veh, cam, it))
        return entry[1] if entry else None

    def stop(self):
        self._stop.set()
        if self.feed is not None:
            self.feed.stop()


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------

async def event_stream(request: Request, hub: EventHub, *,
                       heartbeat_s: float = 15.0, poll_s: float = 0.05,
                       max_events: int | None = None):
    """SSE generator. Connect/disconnect is non-fatal on both sides.

    The publisher is a plain thread, so events arrive through a thread-safe
    queue that this coroutine drains without ever blocking the event loop.
    A comment heartbeat keeps idle proxies from dropping the stream, and
    `retry:` tells the browser how soon to come back.
    """
    sub = hub.subscribe()
    sent = 0
    try:
        yield ": godseye alarm stream\n\n"
        yield "retry: 3000\n\n"
        last_beat = time.monotonic()
        while True:
            try:
                if await request.is_disconnected():
                    break
            except Exception:
                # A torn-down client is the normal end of an SSE stream, not an
                # error: disconnect is non-fatal on both sides.
                break
            drained = False
            while True:
                try:
                    payload = sub.queue.get_nowait()
                except queue.Empty:
                    break
                dropped = sub.take_dropped()
                if dropped:
                    payload = dict(payload)
                    detail = dict(payload.get("detail") or {})
                    detail["dropped_since_last"] = dropped
                    payload["detail"] = detail
                yield f"event: alarm\ndata: {json.dumps(payload)}\n\n"
                drained = True
                sent += 1
                if max_events is not None and sent >= max_events:
                    return
            now = time.monotonic()
            if drained:
                last_beat = now
            elif now - last_beat >= heartbeat_s:
                yield f": ping {int(time.time())}\n\n"
                last_beat = now
            await asyncio.sleep(poll_s)
    finally:
        hub.unsubscribe(sub)


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

def create_app(adapter: AirSimAdapter | None = None, token: str = "dev-token",
               start_loops: bool = True, *,
               cors: CorsPolicy | None = None,
               mcp_url: str | None = None, mcp_token: str | None = None,
               state_source: MissionFeed | None = None,
               mission_hz: float = 2.0,
               heartbeat_s: float = 15.0) -> FastAPI:
    """Build the bridge app.

    `state_source` is the injection point described in the module docstring:
    pass any `MissionFeed`-shaped object (`.poll_once()`, `.intel()`,
    `.overlay()`, `.note_datum()`, `.active_theater()`, `.loop()`, `.stop()`)
    to source mission and track state some other way - in-process from
    `GodseyeUavServer`, or a fake in a test. Omitted, the bridge proxies the
    MCP server over loopback HTTP.
    """
    adapter = adapter or AirSimAdapter()
    hub = EventHub()
    resolved_mcp_url = mcp_url or os.environ.get(
        "GODSEYE_MCP_URL", "http://127.0.0.1:8791/mcp")
    resolved_mcp_token = mcp_token or os.environ.get("GODSEYE_MCP_TOKEN", token)
    mcp = McpClient(resolved_mcp_url, resolved_mcp_token)

    state = BridgeState(adapter, token, hub=hub)
    feed = state_source or MissionFeed(
        mcp, hub, vehicles=adapter.vehicles, flown=state.flown)
    state.feed = feed

    app = FastAPI(title="godSeye Telemetry Bridge", version="0.2.0")
    app.state.bridge = state
    app.state.hub = hub
    app.state.feed = feed
    app.state.mcp = mcp

    policy = cors or cors_policy()
    app.state.cors = policy
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[] if policy.allow_any else list(policy.origins),
        allow_origin_regex=".*" if policy.allow_any else None,
        allow_credentials=policy.allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def active_theater() -> dict:
        """The running theater, for `/health` and `/theaters` (UI-1).

        Read from `state.feed`, not the local `feed`: the feed is swappable.
        A state source that predates `active_theater()` yields an UNKNOWN block
        naming itself as the cause — the route must not 500, and it must not
        answer `default` either, which is the failure this whole block exists
        to remove.

        What the source hands back is CHECKED here, at the one place both
        routes funnel through, rather than trusted. `state.feed` is explicitly
        any MissionFeed-shaped object, so `active_theater()` is a foreign call:
        a source that returned `{}`, or `known: true` with no id, used to be
        published verbatim by `/health` — and a consumer reading
        `active.id or table.default` lands straight back on Redmond, which is
        UI-1 arriving through the very field that was added to end it. An
        unusable block is downgraded to an honest UNKNOWN naming the source,
        which is the same answer this function already gives for a source that
        has no `active_theater()` at all, and keeps `/health` — an open,
        unauthenticated route — from 500ing on a bad feed.
        """
        source = state.feed
        getter = getattr(source, "active_theater", None)
        if not callable(getter):
            return theaters.active_unknown(
                f"the configured state source ({type(source).__name__}) does "
                "not publish an active theater")
        try:
            return theaters.check_active(getter())
        except Exception as exc:  # foreign code: never let it take a route down
            return theaters.active_unknown(
                f"the configured state source ({type(source).__name__}) "
                f"published an unusable active theater ({exc.__class__.__name__}"
                f": {exc})")

    bearer = HTTPBearer(auto_error=False)

    def auth(cred: HTTPAuthorizationCredentials = Depends(bearer)):
        if cred is None or cred.credentials != token:
            raise HTTPException(status_code=401, detail="unauthorized")
        return True

    def auth_sse(request: Request,
                 cred: HTTPAuthorizationCredentials = Depends(bearer)):
        """SSE auth. `EventSource` cannot set headers, so the loopback bridge
        also accepts `?token=` - the query form the GEV client already uses."""
        if cred is not None and cred.credentials == token:
            return True
        if request.query_params.get("token") == token:
            return True
        raise HTTPException(status_code=401, detail="unauthorized")

    if start_loops:
        threading.Thread(target=state.telemetry_loop, daemon=True).start()
        threading.Thread(target=state.camera_loop, daemon=True).start()
        threading.Thread(target=feed.loop, kwargs={"hz": mission_hz},
                         daemon=True).start()

    @app.get("/health")
    def health():
        return {"ok": True, "sim_state": adapter.sim_state,
                "vehicles": len(state.all_snapshots()),
                # WHERE the aircraft is flying, as the enforcing server says it
                # - not this bridge's table default (UI-1). Same block as
                # `/theaters.active`; `known: false` carries `reason`.
                "theater": active_theater(),
                "datum_degraded": adapter.datum_degraded,
                "datum_source": adapter.datum_source,
                "telemetry_error": adapter.last_error,
                "camera_error": adapter.camera_error,
                "vehicles_fallback": adapter.vehicles_fallback,
                "cors": policy.as_dict(),
                "events": {"subscribers": hub.subscriber_count,
                           "published": hub.published,
                           "kinds": sorted(ALARM_SEVERITY)},
                "mission_feed": {"url": resolved_mcp_url,
                                 "polls": getattr(state.feed, "polls", 0),
                                 **state.feed.intel().feeds_dict()}}

    @app.get("/snapshot")
    def snapshot(_: bool = Depends(auth)):
        # Pure cache read. No MCP call, no camera pull, no geoid lookup - a
        # slow feed can never stall the poll (BRIDGE_CONTRACT rule 4).
        snaps = state.all_snapshots()
        # `state.feed`, not the local: the feed is swappable (see `state_source`
        # in the docstring) and a route that captured the original would keep
        # serving a feed nobody is filling.
        intel = state.feed.intel()
        return {
            "sim_state": adapter.sim_state,
            "observedAtMs": int(time.time() * 1000),
            "count": len(snaps),
            "vehicles": [vars(s) for s in snaps],
            "missions": intel.missions,
            "contacts": intel.contacts,
            "feeds": intel.feeds_dict(),
        }

    @app.get("/snapshot/{name}")
    def snapshot_one(name: str, _: bool = Depends(auth)):
        s = state.get_snapshot(name)
        if not s:
            raise HTTPException(404, "vehicle not found")
        return vars(s)

    @app.get("/mission-overlay")
    def mission_overlay(_: bool = Depends(auth)):
        return state.mission_overlay

    @app.get("/theaters")
    def theater_table(_: bool = Depends(auth)):
        # theaters.py is the single source of truth; this route exists so the
        # browser stops keeping a third hand-typed copy of the table.
        # `active` is the row the RUNNING server chose. Without it the panel
        # can only read `default` - the table's default - and a stack launched
        # at iran-isfahan showed Redmond POIs (INTEGRATION_FINDINGS UI-1).
        return theaters.as_payload(active=active_theater())

    @app.get("/events")
    async def events(request: Request, _: bool = Depends(auth_sse)):
        return StreamingResponse(
            event_stream(request, hub, heartbeat_s=heartbeat_s),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform",
                     "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"})

    @app.post("/camera/subscribe")
    async def cam_sub(req: Request, _: bool = Depends(auth)):
        """Register (or renew) a camera subscription so loop B pulls frames.

        This route used to answer `{"subscribed": true}` without touching the
        subscription table at all - it took no vehicle, read no body, and
        called nothing. A client that asked for a feed was told it had one and
        got no frames; only the lazy subscribe inside `GET /camera/{veh}` ever
        worked. The answer now reports what was actually registered.
        """
        try:
            body = await req.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(400, "subscribe body must be a JSON object")
        veh = str(body.get("vehicle") or "")
        if not veh:
            raise HTTPException(
                400, "subscribe needs a vehicle; a subscription to nothing "
                     "would report success and deliver no frames")
        cam = str(body.get("camera", body.get("cam", "0")))
        try:
            it = int(body.get("type", 0))
        except (TypeError, ValueError):
            raise HTTPException(400, f"type must be an int, got {body.get('type')!r}")
        state.subscribe_camera(veh, cam, it)
        return {"subscribed": True, "ttl_s": CAMERA_SUB_TTL_S,
                "vehicle": veh, "camera": cam, "type": it,
                "active": [{"vehicle": v, "camera": c, "type": t}
                           for v, c, t in state.active_camera_subs()]}

    # -- control proxy: browser UI -> MCP server (God's Eye stays view-only;
    #    the MCP server is the only command path). The bridge forwards a mission
    #    or command to the MCP Streamable-HTTP tool endpoint over loopback.

    def _mcp_call(tool: str, arguments: dict) -> dict:
        out, err = mcp.rpc("tools/call", {"name": tool, "arguments": arguments})
        if err:
            raise HTTPException(502, err)
        return out if isinstance(out, dict) else {"result": out}

    def _unwrap(out: dict, tool: str = "mcp") -> dict:
        # MCP tools/call envelope -> the tool's JSON payload (what the UI expects:
        # {task_id,...} or {rejected:true,error}). Falls back to the raw result.
        res = out.get("result", out) if isinstance(out, dict) else out
        content = res.get("content") if isinstance(res, dict) else None
        # An `isError` result carries prose, not JSON. Without this it reached
        # the panel as {"text": "Error executing tool ..."} with HTTP 200 and
        # no error key - indistinguishable from a tool that simply said little.
        tool_err = McpClient._tool_error(tool, res)
        if tool_err:
            return {"error": tool_err, "isError": True,
                    "text": (content[0].get("text")
                             if isinstance(content, list) and content
                             and isinstance(content[0], dict) else None)}
        if content and isinstance(content, list) and content[0].get("text"):
            try:
                return json.loads(content[0]["text"])
            except Exception:
                return {"text": content[0]["text"]}
        return res if isinstance(res, dict) else {"result": res}

    def _forward(req: Request, call, fixed_tool):
        # Body: {vehicle, kind?, params?, tool?, arguments?}. fixed_tool pins the
        # mission endpoint to uav_mission; command endpoint takes an explicit tool.
        import anyio

        async def _body():
            return await req.json()
        try:
            body = anyio.run(_body)
        except Exception:
            body = {}
        if fixed_tool is not None:
            tool = "uav_mission"
            args = {"vehicle": body.get("vehicle", "Drone1"),
                    "kind": body.get("kind"),
                    "params": body.get("params", {})}
            if body.get("speed_mps") is not None:
                args["speed_mps"] = body["speed_mps"]
        else:
            tool = body.get("tool")
            if not tool:
                raise HTTPException(400, "command needs a tool name")
            args = body.get("arguments", {})
            args.setdefault("vehicle", body.get("vehicle", "Drone1"))
        return _unwrap(call(tool, args), tool)

    @app.post("/control/mission")
    def control_mission(req: Request, _: bool = Depends(auth)):
        return _forward(req, _mcp_call, "uav_mission")

    @app.post("/control/command")
    def control_command(req: Request, _: bool = Depends(auth)):
        return _forward(req, _mcp_call, None)

    @app.get("/control/status/{veh}")
    def control_status(veh: str, _: bool = Depends(auth)):
        return _mcp_call("uav_get_telemetry", {"vehicle": veh})

    @app.get("/tracks")
    def tracks(_: bool = Depends(auth)):
        # Proxy the MCP server's persistent tracks so the GEV UI can render
        # numbered target markers on the globe (M11). Unwrap the MCP envelope.
        return _unwrap(_mcp_call("uav_list_tracks", {}), "uav_list_tracks")

    @app.get("/camera/{veh}")
    def camera(veh: str, cam: str = "0", type: int = 0, _: bool = Depends(auth)):
        # subscribe on first request (lazy start), serve latest frame
        state.subscribe_camera(veh, cam, type)
        png = state.get_camera(veh, cam, type)
        if png is None:
            png = adapter.camera_png(veh, cam, type)  # on-demand (loop C)
        if png is None:
            raise HTTPException(
                503, f"camera unavailable: {adapter.camera_error or 'no frame'}")
        return Response(content=png, media_type="image/png")

    return app
