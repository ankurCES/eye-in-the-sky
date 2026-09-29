"""The simulated wargame engine (M14a, PLAN.md §4.5a; WG v2 §5.2.4-§5.2.9).

An opt-in, operator-approved SIMULATION. Its forces are scenario units the
wargame itself places (provenance `scenario`, generic designators such as
"Red SAM 1"); effects are abstract notional adjudications from
`wargame_tables` / `wargame_adjudicate` (no weaponeering, D1); every output is
stamped `simulated: true`. Drones never deliver effects: they fly recce and
battle-damage re-looks, and red air defence may down them.

Hard rules this module enforces (D1, D2, C3, V4):

- **Provenance gate** (`gate_track`): the ONLY way a track enters a wargame
  computation (sensed threats, corridor targets, strikes, BDA looks). A track
  passes only if its sim object is a scenario unit of THIS session, spawned
  before the track was first seen, within 300 m of the unit's truth. Mapped
  sites, `sim_spawn_*` objects, phantoms and real traffic always fail.
- **Real-site gate**: no target within 500 m of a mapped place or theater point,
  or within 1 km of a protected place, re-checked at propose, authorize and
  execute, on the unit's truth position AND the track position.
- **Propose -> authorize -> execute**: `authorize` is called only by the chat
  service after a console approval; `execute` also needs
  `CONSOLE_CALL == the authorizing chat session`. `/mcp` and
  `/control/command` never have that context, so they can never confirm.
- Threats, envelopes, axes and `step()` shooters come only from engine units.

Threads (C6): red adjudication runs in the daemon thread `godseye-wargame`
(`step()` every `WG_TICK_S` wall seconds, `rng_red` only). It reads memory only
(`srv._last_tele`, the fake sim's locked state) and shares no lock with the
safety tick. A drone loss is handed to the tasking loop with
`run_coroutine_threadsafe(srv.lose_vehicle(...))`. Heavy geometry (lattice,
horizons, exposure, A*) never runs under `self._lock`: snapshot, compute in a
worker, commit. `srv` is duck-typed; this module never imports `server.py`.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import secrets
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from . import sites as _sites
from . import theaters as _theaters
from . import wargame_aar as aar
from . import wargame_bda as bda
from .safety import bearing_deg, haversine_m
from .targets import OB_LIBRARY, assess_confidence, confidence_at_least
from .wargame_adjudicate import (
    apply_outcome,
    draw_outcome,
    effect_bands,
    make_streams,
    p_detect,
    p_ground,
    p_kill_air,
    release_suppression,
    strike_probabilities,
    threat_range,
)
from .wargame_spawn import (
    AO_MARGIN_M,
    HOME_EXCLUSION_M,
    MESSAGES,
    OBJECT_CLEARANCE_M,
    UNIT_SPACING_M,
    World,
    object_name,
    plan_positions,
    refusal,
    relative_position,
    site_gate,
)
from .wargame_tables import (
    CLASSES,
    DAMAGED_CYCLE_FACTOR,
    DAMAGED_RANGE_FACTOR,
    INTENSITIES,
    NOTIONAL_NOTE,
    PACKAGE_ALT_AGL_M,
    PACKAGE_SPEED_MPS,
    TABLE_VERSION,
    TEMPLATES,
    ao_geometry_bbox,
    class_caveats,
    classes_that_fit,
    designator,
    destination,
    estimate_hardness,
    label_for_ob,
    red_class_for_ob,
    template_positions,
    unit_id,
)
from .wargame_vectors import (
    NoRoute,
    build_lattice,
    coverage_fan,
    exposure_grid,
    ground_hae_m,
    horizon,
    plan,
    red_axes,
    ring_points,
    summarize,
    visible,
)

__all__ = ["CONSOLE_CALL", "Engagement", "Pending", "Session", "Unit", "WargameEngine",
           "WargameRefused", "console_call"]

WG_TICK_S = 0.25                      # wall seconds between step() passes
PENDING_TTL_S = 600.0                 # wall; matches the approval timeout
MAX_UNITS, MAX_PENDING, MAX_EVENTS = 60, 8, 2000
TRACK_TRUTH_MAX_M, HORIZON_MOVE_M = 300.0, 25.0
STEP_BUDGET_MS = 20.0
#: Re-exported spawn-rule constants (§5.2.4 lists them here).
SPAWN_RULES_M = {"home": HOME_EXCLUSION_M, "spacing": UNIT_SPACING_M,
                 "object": OBJECT_CLEARANCE_M, "ao_margin": AO_MARGIN_M}
ENGINE = TABLE_VERSION

#: Set to the chat session id by the toolbelt proxy (`console_call`) around a
#: console-approved `wg_*` call. `/mcp` and `/control/command` never set it.
CONSOLE_CALL: ContextVar[str | None] = ContextVar("wg_console_call", default=None)

INACTIVE_MESSAGE = "Start a simulated wargame session first (wg_session_start)."
NOT_SCENARIO_MESSAGE = ("Only simulated scenario units can be engaged. Mapped real sites, "
                        "real air traffic and other sim objects are context only (M14a).")
SESSION_CAVEAT = ("Scenario forces and engagements are simulated; outcomes are notional "
                  "adjudications.")
OUTCOME_NOTE = "Unknown until a re-look (battle damage assessment)."
RED_AD_HIDDEN = "Red air defence (not identified)"
RED_GROUND_HIDDEN = "Red ground forces (not identified)"
MASKING_PENDING_CAVEAT = "Terrain masking not yet computed for this unit."
TERRAIN_OFF_CAVEAT = ("Terrain masking is off: real terrain isn't loaded (start the app with "
                      "--real-data direct).")
NO_AVOID_CAVEAT = ("No route avoids the envelope here: the planned route is within 5 % of the "
                   "straight route's exposure.")
SENSED_CAVEAT = "Planned against sensed contacts only; threats not yet seen are not in it."
EXCLUSION_OFF_CAVEAT = ("Mapped places weren't loaded ({reason}); scenario units are kept "
                        "clear of theater points and home only.")

#: Refusal sentences of the engine itself (spawn and gate ones are in wargame_spawn).
ENGINE_MESSAGES: dict[str, str] = {
    "wargame_active": "A wargame session is already running.",
    "wargame_requires_fake_sim": "The wargame needs the fake simulator.",
    "theater_switching": "A theater switch is running.",
    "theater_not_cleared": "This theater isn't cleared for the wargame.",
    "exclusion_incomplete": ("Mapped places here couldn't all be loaded, so the wargame can't "
                             "keep clear of them; try again when map data is available."),
    "wargame_inactive": INACTIVE_MESSAGE,
    "wargame_ending": "The wargame is ending; wait for it to finish.",
    "not_a_scenario_unit": NOT_SCENARIO_MESSAGE,
    "shooter_unavailable": ("The shooter must be a blue scenario shooter that is active or "
                            "damaged and has ammunition left."),
    "insufficient_confidence": ("The contact must be identified with at least probable "
                                "confidence before a simulated strike."),
    "out_of_range": "The target is beyond the shooter's notional range.",
    "pending_limit": "At most 8 simulated engagements can wait for approval at once.",
    "unknown_pending": "No simulated engagement with that id is waiting.",
    "engagement_not_pending": "That simulated engagement is no longer waiting for approval.",
    "engagement_expired": "That simulated engagement expired; propose it again.",
    "engagement_args_mismatch": ("The request doesn't match the proposed engagement; call "
                                 "wg_execute_engagement with execute_args exactly."),
    "engagement_requires_console_approval": ("A simulated engagement runs only after the "
                                             "operator approves it in the console."),
    "vehicle_unknown": "No vehicle by that name is in the simulator.",
    "vehicle_lost": ("That drone was lost in the simulated wargame; it returns when the "
                     "wargame ends."),
    "unknown_template": ("Unknown scenario template; use air_defence_belt, mech_advance or "
                         "strike_exercise."),
    "unknown_objective": ("The objective must be a scenario unit of the other side that isn't "
                          "destroyed."),
    "spawn_busy": "Another scenario placement is running; try again when it finishes.",
    "spawn_failed": "The simulator couldn't place the scenario units, so none were placed.",
    "invalid_parameter": "A parameter is out of range.",
}

GRAPH_ENGAGEMENTS, GRAPH_VECTORS, GRAPH_FORCES = 24, 12, 60
OVERLAY_ENVELOPES, OVERLAY_AXES, OVERLAY_CORRIDORS, OVERLAY_ENGAGEMENTS = 80, 20, 6, 24
RED_FIRE_WINDOW_MS = 60_000
POS_BUMP_M, POS_BUMP_S = 25.0, 5.0
STEP_AUDIT_EVERY_S = 60.0
ENVELOPE_REF_AGL_M = 60.0
DEFAULT_DRONE_SPEED_MPS = 10.0        # `mission_recon_route`'s default speed
LOSS_WAIT_S = 5.0
RELOOK_MIN_M, RELOOK_MAX_M = 150.0, 1500.0
MAX_THREATS = 40
CIRCLE_POINTS = 48

#: `intel_graph.CATEGORY_GROUP` (a test holds them equal): OB category -> sector.
CATEGORY_GROUP = {
    "sam": "air-defense", "aaa": "air-defense", "radar": "radar-ew",
    "c2": "c2", "armor": "ground-forces", "artillery": "ground-forces",
    "personnel": "ground-forces", "logistics": "logistics",
    "vehicle": "logistics", "structure": "infrastructure", "naval": "naval",
    "aircraft": "air", "civilian": "civilian",
}
#: Blue force sectors (§3.2).
BLUE_GROUP = {"blue_artillery": "ground-forces", "blue_rocket": "ground-forces",
              "blue_mech": "ground-forces", "blue_strike_air": "air",
              "blue_defended_point": "logistics"}
STATE_WORD = {"active": "Active", "suppressed": "Suppressed", "damaged": "Damaged",
              "destroyed": "Destroyed"}
KIND_WORD = {"blue_strike": "strike", "red_shot": "shot", "red_ground": "ground fire"}
PHASE_WORD = {"proposed": "Waiting for you", "authorized": "Authorized",
              "adjudicated": "Adjudicated", "denied": "Denied", "expired": "Expired"}


@contextlib.contextmanager
def console_call(chat_session_id: str) -> Iterator[None]:
    """Mark the enclosed call as made from the console's approval path (§3.8)."""
    token = CONSOLE_CALL.set(str(chat_session_id) if chat_session_id else None)
    try:
        yield
    finally:
        CONSOLE_CALL.reset(token)


class WargameRefused(Exception):
    """A refusal with a code (§3.1). `authorize` raises it; the chat service
    denies the approval with its message."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or ENGINE_MESSAGES.get(code) or MESSAGES.get(code) or code
        super().__init__(self.message)

    def as_result(self) -> dict:
        return refusal(self.code, self.message)


def _refused(code: str, message: str | None = None) -> dict:
    return WargameRefused(code, message).as_result()


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Unit:
    """One scenario unit. Red units are sim objects (`object_name`); blue units
    are registry-only. `spawned_at` is wall `time.time()`; `*_s` are SIM seconds."""

    unit_id: str
    side: str
    wg_class: str
    designator: str
    lat: float
    lon: float
    alt_hae_m: float
    alt_msl_m: float
    object_name: str | None
    spawned_at: float
    state: str = "active"
    damaged: bool = False
    state_until_s: float | None = None
    ammo: int = 0
    next_shot_s: float = 0.0
    last_fired_ms: int | None = None
    objective: str | None = None
    horizon: Any = None
    horizon_at: tuple[float, float] | None = None
    in_envelope: set = field(default_factory=set)
    detecting: set = field(default_factory=set)
    slot: str | None = None
    horizon_pending: bool = False
    fans: dict | None = None
    route_to: tuple[float, float] | None = None
    pos_rev: tuple[float, float, float] | None = None   # (lat, lon, wall s) at last bump


@dataclass
class Pending:
    """A proposed blue strike waiting for the console (§5.2.7)."""

    pending_id: str
    shooter_id: str
    target_track_id: str
    unit_id: str
    args: dict
    created_ms: int
    expires_at_ms: int
    state: str = "proposed"        # proposed|authorized|consumed|denied|expired
    p_estimate: dict | None = None
    range_m: float = 0.0
    corridor: dict | None = None
    vector_id: str | None = None
    authorized: dict | None = None  # {approval_id, chat_session, at_ms}
    level: str = "probable"
    result: dict | None = None      # the execute answer, for an idempotent replay


@dataclass
class Engagement:
    """One engagement row: a blue strike, a red shot at a drone, or red ground fire."""

    engagement_id: str
    kind: str
    phase: str
    attacker: str | None
    target: str
    target_label: str
    vector: str | None
    p_notional: dict | None
    inputs: list[str]
    outcome: str | None
    consequence: str
    seed: int
    draw: int | None
    proposed_at_ms: int
    adjudicated_at_ms: int | None
    approval_id: str | None = None
    fired_at_ms: int | None = None
    bda: dict | None = None
    unit_id: str | None = None      # the red unit struck (blue_strike) or firing (red)
    authorized_at_ms: int | None = None  # the console approval (blue strikes; §5.3.6)
    attacker_label: str = ""
    track_id: str | None = None
    target_point: tuple[float, float] | None = None
    attacker_point: tuple[float, float] | None = None
    stream: str | None = None       # "red" | "blue"


@dataclass
class Session:
    session_id: str
    num: int
    started_at_ms: int
    seed: int
    red_engages: bool
    reveal_red: bool
    theater_id: str
    theater_epoch: int
    exclusion: Any
    lattice: Any
    ao: dict
    units: dict
    pendings: dict
    engagements: list
    events: list
    object_names: list
    track_ids: list
    lost: dict
    vectors: dict
    rng_red: Any = None
    rng_blue: Any = None
    seq: int = 0                    # engagement ids: pending strikes and red events share it
    red_seq: int = 0                # red object names
    cor_seq: int = 0                # corridor vector ids
    counters: dict = field(default_factory=dict)   # (side, prefix) -> n
    caveats: list = field(default_factory=list)


def _strip(value: Any, prefix: str) -> str:
    """An id with its graph prefix (`frc:`, `trk:`, `eng:`) removed."""
    text = str(value if value is not None else "").strip()
    return text.removeprefix(prefix)


def _exec_args(pending_id: Any, shooter_id: Any, target_track_id: Any) -> dict:
    return {"pending_id": _strip(pending_id, "eng:"), "shooter_id": _strip(shooter_id, "frc:"),
            "target_track_id": _strip(target_track_id, "trk:")}


def _args_equal(recorded: dict, args: Any) -> bool:
    """The three engagement args equal the pending record (extra keys such as
    `idempotency_key` are not compared)."""
    if not isinstance(args, dict):
        return False
    try:
        got = _exec_args(args["pending_id"], args["shooter_id"], args["target_track_id"])
    except KeyError:
        return False
    return got == recorded


def _km(m: float) -> str:
    return f"{float(m) / 1000.0:.1f} km"


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def _circle(lat: float, lon: float, radius_m: float, n: int = CIRCLE_POINTS) -> list[list[float]]:
    return [[round(p[0], 7), round(p[1], 7)]
            for p in (destination(lat, lon, radius_m, 360.0 * i / n) for i in range(n))]


def _compute_horizon(lat: float, lon: float, eye_msl: float, lattice: Any, cls: Any) -> dict:
    """A unit's terrain horizon and its envelope fans (worker thread, §5.2.4)."""
    rng = max(cls.detection_range_m, cls.threat_range_m, 1.0)
    h = horizon(lat, lon, eye_msl, lattice, rng)
    ref = h.eye_msl_m + ENVELOPE_REF_AGL_M
    fans = {}
    if cls.threat_range_m > 0:
        fans["threat"] = coverage_fan(h, cls.threat_range_m, ref)
    if cls.detection_range_m > 0:
        fans["detection"] = coverage_fan(h, cls.detection_range_m, ref)
    return {"horizon": h, "fans": fans}


def _compute_corridor(lattice: Any, threats: list, start: tuple, goal: tuple, fence: list,
                      alt: float, speed: float, env: Any) -> dict:
    """Horizons for sensed threats, the exposure grid, the planned and straight
    routes (worker thread, §5.2.8). Raises `NoRoute`."""
    ready = []
    for lat, lon, hae, cls, damaged, hz in threats:
        if hz is None:
            hz = horizon(lat, lon, lattice.ground_at(lat, lon), lattice,
                         max(cls.detection_range_m, cls.threat_range_m, 1.0))
        ready.append((lat, lon, hae, cls, damaged, hz))
    exp = exposure_grid(lattice, ready, alt, speed, env=env)
    route = plan(lattice, exp, start, goal, fence, alt_agl_m=alt, speed_mps=speed)
    return {"route": route, "planned": summarize(route.path, exp, speed),
            "straight": summarize([start, goal], exp, speed), "threats": len(ready)}


def _evaluate_path(lattice: Any, threats: list, path: list, alt: float, speed: float,
                   env: Any) -> dict:
    """A stored path flown against TRUTH threats (worker thread, §5.2.7 step 4)."""
    exp = exposure_grid(lattice, threats, alt, speed, env=env)
    return summarize(path, exp, speed)


class WargameEngine:
    """The simulated wargame (§5.2.4). One per server; at most one session."""

    def __init__(self, srv: Any, *, run_thread: bool = True):
        self.srv = srv
        #: False keeps `start()` from launching the `godseye-wargame` thread
        #: (tests drive `step()` directly).
        self.run_thread = bool(run_thread)
        self._lock = threading.Lock()
        self._session: Session | None = None
        self._names: frozenset[str] = frozenset()
        self.starting = False
        self.last: dict | None = None
        self.revision = 0
        self.errors = 0
        self.step_ms = 0.0
        self._ending = False
        self._spawning = False
        self._last_num = 0
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._hpool: ThreadPoolExecutor | None = None
        self._iopool: ThreadPoolExecutor | None = None
        self._pool_lock = threading.Lock()
        self._loss_futures: list = []
        self._audit_at: dict[str, float] = {}
        #: `deferred` is set while a thread runs `step()`: its audit rows go
        #: through the IO worker (no fsync inside the 20 ms pass, §0.2).
        self._tls = threading.local()

    # ------------------------------------------------------------ state --
    @property
    def active(self) -> bool:
        return self._session is not None

    @property
    def session_id(self) -> str | None:
        s = self._session
        return None if s is None else s.session_id

    def mode_key(self) -> str:
        s = self._session
        return "isr" if s is None else f"wargame:{s.session_id}"

    def owns_name(self, name: str) -> bool:
        """True for a sim object name of the running session (lock-free read)."""
        return str(name) in self._names

    def unit_near(self, lat: float, lon: float, radius_m: float) -> str | None:
        """A scenario unit of the running session within `radius_m`, else None."""
        with self._lock:
            s = self._session
            if s is None:
                return None
            for u in sorted(s.units.values(), key=lambda x: x.unit_id):
                if haversine_m(float(lat), float(lon), u.lat, u.lon) <= float(radius_m):
                    return u.unit_id
        return None

    def note_track(self, track: Any) -> bool:
        """The ingest hook (§5.2.9): remember a scenario track of this session so
        it is deleted at the end. True when it was new (and persisted)."""
        with self._lock:
            s = self._session
            if s is None or getattr(track, "name", None) not in self._names:
                return False
            tid = str(getattr(track, "track_id", "") or "")
            if not tid or tid in s.track_ids:
                return False
            s.track_ids.append(tid)
            self.revision += 1
            record = self._record(s)
        self._write_record(record)
        return True

    def _active(self) -> Session | None:
        """The running session, or None while none runs or it is ending."""
        s = self._session
        return None if s is None or self._ending else s

    def _sim(self) -> Any:
        return getattr(getattr(self.srv, "backend", None), "sim", None)

    def _sim_now(self) -> float | None:
        sim = self._sim()
        try:
            return float(sim.sim_elapsed_s()) if sim is not None else None
        except Exception:  # noqa: BLE001 — a missing clock reads as unknown
            return None

    def _environment(self) -> dict | None:
        sim = self._sim()
        try:
            return sim.environment() if sim is not None else None
        except Exception:  # noqa: BLE001 — clear conditions when unreadable
            return None

    def _time_scale(self) -> float:
        try:
            return float(getattr(self.srv, "time_scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            return 1.0

    # --------------------------------------------------- audit and files --
    def _pool(self, name: str) -> ThreadPoolExecutor:
        with self._pool_lock:
            if name == "horizon":
                if self._hpool is None:
                    self._hpool = ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="godseye-wargame-horizon")
                return self._hpool
            if self._iopool is None:
                self._iopool = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="godseye-wargame-io")
            return self._iopool

    def _audit(self, kind: str, message: str, **fields: Any) -> None:
        """Audit a row. From the wargame thread it goes through the IO worker
        (no blocking I/O on that thread, §0.2); elsewhere it is written now."""
        store = getattr(self.srv, "store", None)
        if store is None:
            return
        if threading.current_thread() is self._thread or getattr(self._tls, "deferred", False):
            self._pool("io").submit(store.log_audit, kind, message, **fields)
        else:
            store.log_audit(kind, message, **fields)

    def _audit_limited(self, kind: str, message: str, **fields: Any) -> None:
        """At most one row of `kind` per `STEP_AUDIT_EVERY_S`."""
        now = time.monotonic()
        if now - self._audit_at.get(kind, -math.inf) >= STEP_AUDIT_EVERY_S:
            self._audit_at[kind] = now
            self._audit(kind, message, **fields)

    def flush(self, timeout_s: float = 10.0) -> bool:
        """Wait until queued audit rows and horizon jobs are done (tests, `end`)."""
        ok = True
        for name, pool in (("horizon", self._hpool), ("io", self._iopool)):
            if pool is None:
                continue
            try:
                pool.submit(lambda: None).result(timeout=timeout_s)
            except Exception:  # noqa: BLE001 — reported as not flushed
                ok = False
        return ok

    def _path(self) -> Any:
        return aar.session_path(getattr(self.srv, "store", None))

    @staticmethod
    def _record(s: Session) -> dict:
        return aar.session_record(
            session_id=s.session_id, num=s.num, started_at_ms=s.started_at_ms, seed=s.seed,
            theater_id=s.theater_id, object_names=s.object_names, track_ids=s.track_ids)

    def _write_record(self, record: dict) -> None:
        """Persist `wargame.json` (§5.2.9); a failure is audited, never raised."""
        try:
            aar.write_session(self._path(), record)
        except OSError as exc:
            self._audit_limited("wargame_persist_failed", f"{type(exc).__name__}: {exc}",
                                session_id=record.get("session_id"))

    # ------------------------------------------------- crash recovery --
    def recover_on_boot(self) -> dict | None:
        """A session a crash interrupted (§5.2.9): delete its tracks, file a
        partial AAR (`incomplete: true`), audit, remove `wargame.json`. None
        when there is nothing to recover. Called by the server after the
        restart replay restored tracks, so they can be deleted again."""
        srv = self.srv
        path = self._path()
        rec = aar.read_session(path)
        if rec is None:
            return None
        sid = str(rec.get("session_id") or "unknown")
        names = {str(n) for n in rec.get("object_names") or ()}
        ids = [str(t) for t in rec.get("track_ids") or ()]
        tracks = getattr(srv, "tracks", None)
        if tracks is not None:
            ids += [t.track_id for t in tracks.tracks()
                    if t.name in names and t.track_id not in ids]
        store = getattr(srv, "store", None)
        if store is not None:
            ids += [rid for rid, row in store.tracks.all().items()
                    if isinstance(row, dict) and row.get("name") in names and rid not in ids]
        removed = tracks.remove(ids) if tracks is not None else []
        if store is not None:
            for tid in ids:
                store.tracks.delete(tid)
        pol = getattr(srv, "pol", None)
        if pol is not None and ids:
            pol.forget(ids)
        for name in names:
            getattr(srv, "targets", {}).pop(name, None)
        ended = _now_ms()
        rows = list(store.audit.read_all()) if store is not None else []
        report = aar.partial_aar({**rec, "session_id": sid, "track_ids": ids}, rows,
                                 ended_at_ms=ended)
        srv.reports[report["id"]] = report
        with contextlib.suppress(TypeError, ValueError):
            self._last_num = max(self._last_num, int(rec.get("num") or 0))
        self._audit("wargame_session_aborted_by_restart",
                    f"simulated wargame {sid} was interrupted by a restart",
                    session_id=sid, track_ids=ids, aar_id=report["id"],
                    corrupt=rec.get("corrupt"), simulated=True)
        aar.remove_session(path)
        self.last = {"session_id": sid, "aar_id": report["id"], "ended_at_ms": ended}
        return {"aborted": sid, "aar_id": report["id"], "track_ids": ids,
                "removed": removed, "simulated": True}

    # ---------------------------------------------------------- start --
    def _start_refusal(self) -> dict | None:
        """The §5.2.4 start refusals, checked under `srv._mode_lock`."""
        srv = self.srv
        if self._session is not None or self.starting:
            return _refused("wargame_active")
        if self._sim() is None:
            return _refused("wargame_requires_fake_sim")
        if srv._switching.is_set():
            return _refused("theater_switching")
        if getattr(srv, "theater_integrity_error", None):
            return _refused("theater_integrity", str(srv.theater_integrity_error))
        if getattr(srv.theater, "wargame_ok", False) is not True:
            return _refused("theater_not_cleared")
        # Defence in depth (M14a, D1): whatever built the row, an AO on or near
        # a preset theater the table refuses is not cleared.
        if not _theaters.wargame_clear_of_presets(getattr(srv.theater, "ao", None) or ()):
            return _refused("theater_not_cleared")
        return None

    def _ground_fn(self):
        terrain_at = getattr(self.srv, "terrain_at", None)

        def ground(lat: float, lon: float) -> float | None:
            s = terrain_at(lat, lon) if terrain_at is not None else None
            if s is not None and getattr(s, "real", False) and s.msl_m is not None:
                return float(s.msl_m)
            return None
        return ground

    async def start(self, *, seed: Any = None, red_engages: bool = True,
                    reveal_red: bool = False) -> dict:
        """Start a session (§5.2.4). Returns the session summary or a refusal."""
        srv = self.srv
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            try:
                seed = int(str(seed).strip())
            except ValueError:
                return _refused("invalid_parameter", "seed must be a whole number.")
        with srv._mode_lock:
            refused = self._start_refusal()
            if refused is not None:
                return refused
            self.starting = True
        try:
            t = srv.theater
            bbox = t.bbox()
            exclusion = await asyncio.to_thread(
                _sites.fetch_exclusion, bbox, cache=getattr(srv, "geo_cache", None),
                fetch=getattr(srv, "geo_fetch", None),
                enabled=bool(getattr(srv, "geodata_enabled", False)))
            if not exclusion.complete and t.id != "default":
                return _refused("exclusion_incomplete")
            ao = {"bbox": list(bbox), "home_msl_m": float(t.home_alt_msl_m),
                  "home_hae_m": float(t.home_alt_hae_m())}
            lattice = await asyncio.to_thread(build_lattice, ao, self._ground_fn())
            return self._commit_start(t, exclusion, lattice, ao, seed, red_engages, reveal_red)
        finally:
            self.starting = False

    def _commit_start(self, t: Any, exclusion: Any, lattice: Any, ao: dict, seed: Any,
                      red_engages: bool, reveal_red: bool) -> dict:
        srv = self.srv
        geo = ao_geometry_bbox(t.bbox())
        with srv._mode_lock:
            if srv._switching.is_set():
                return _refused("theater_switching")
            now = _now_ms()
            num = now if now > self._last_num else self._last_num + 1
            self._last_num = num
            seed = int(seed) if seed is not None else secrets.randbelow(100_000)
            rng_red, rng_blue = make_streams(seed)
            caveats = [SESSION_CAVEAT]
            if not exclusion.complete:
                caveats.append(EXCLUSION_OFF_CAVEAT.format(
                    reason=exclusion.reason or "not available"))
            if not lattice.covered:
                caveats.append(TERRAIN_OFF_CAVEAT)
            ao.update({"center": tuple(t.center()), "home": (t.home_lat, t.home_lon),
                       "polygon": [tuple(p) for p in t.ao_list()],
                       "pois": [(p.lat, p.lon) for p in t.pois], **geo})
            s = Session(
                session_id=f"WG-{secrets.token_hex(3)}", num=num, started_at_ms=now,
                seed=seed, red_engages=bool(red_engages), reveal_red=bool(reveal_red),
                theater_id=t.id, theater_epoch=int(getattr(srv, "theater_epoch", 0) or 0),
                exclusion=exclusion, lattice=lattice, ao=ao, units={}, pendings={},
                engagements=[], events=[], object_names=[], track_ids=[], lost={},
                vectors={}, rng_red=rng_red, rng_blue=rng_blue, caveats=caveats)
            with self._lock:
                self._session, self._names = s, frozenset()
                self.revision += 1
                self._event(s, "session_started", "blue",
                            f"Simulated wargame {s.session_id} started (seed {seed}).")
            self._write_record(self._record(s))
            self._audit("wargame_session_started", f"simulated wargame {s.session_id} started",
                        session_id=s.session_id, seed=seed, theater_id=t.id,
                        red_engages=s.red_engages, reveal_red=s.reveal_red, simulated=True)
            self._start_thread(s)
        return {"session_id": s.session_id, "started_at_ms": s.started_at_ms, "seed": seed,
                "engine": ENGINE, "table_version": TABLE_VERSION,
                "red_engages": s.red_engages, "reveal_red": s.reveal_red,
                "theater_id": t.id, "ao": dict(geo), "classes_that_fit": classes_that_fit(geo),
                "caveats": list(caveats), "simulated": True, "note": NOTIONAL_NOTE}

    # --------------------------------------------------------- thread --
    def _start_thread(self, s: Session) -> None:
        if not self.run_thread:
            return
        stop = threading.Event()
        th = threading.Thread(target=self._loop, args=(stop,), name="godseye-wargame",
                              daemon=True)
        self._stop, self._thread = stop, th
        th.start()

    def _loop(self, stop: threading.Event) -> None:
        while not stop.wait(WG_TICK_S):
            now = self._sim_now()
            if now is None:
                continue
            try:
                self.step(now, dict(getattr(self.srv, "_last_tele", {}) or {}))
            except Exception as exc:  # noqa: BLE001 — counted and audited, loop lives
                self.errors += 1
                self._audit_limited("wargame_step_failed", f"{type(exc).__name__}: {exc}",
                                    session_id=self.session_id)

    def _stop_thread(self, timeout_s: float = 3.0) -> None:
        stop, th = self._stop, self._thread
        if stop is not None:
            stop.set()
        if th is not None and th is not threading.current_thread():
            th.join(timeout_s)
        self._stop = self._thread = None

    def close(self) -> None:
        """Stop the thread and the worker pools (process or test teardown). A
        running session stays in memory; the pools are rebuilt on demand."""
        self._stop_thread()
        with self._pool_lock:
            pools, self._hpool, self._iopool = (self._hpool, self._iopool), None, None
        for pool in pools:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)

    # --------------------------------------------------------- events --
    def _event(self, s: Session, kind: str, side: str, text: str, *,
               outcome: str | None = None, blue_text: str | None = None,
               blue_hidden: bool = False, blue_outcome_hidden: bool = False,
               sim_s: float | None = None, **fields: Any) -> dict:
        """Append one event (≤ MAX_EVENTS kept) and audit it as `wargame_event`.
        Caller holds `self._lock`. `blue_*` say how blue view shows it (fog)."""
        if sim_s is None:
            sim_s = self._sim_now()
        ev = {"t_ms": _now_ms(), "sim_s": None if sim_s is None else round(sim_s, 1),
              "kind": kind, "side": side, "text": text, "outcome": outcome,
              "register": "scenario", "simulated": True}
        s.events.append({**ev, "_blue_text": blue_text, "_blue_hidden": bool(blue_hidden),
                         "_blue_outcome_hidden": bool(blue_outcome_hidden)})
        if len(s.events) > MAX_EVENTS:
            del s.events[:len(s.events) - MAX_EVENTS]
        self._audit("wargame_event", text, session_id=s.session_id, event=ev, **fields)
        return ev

    @staticmethod
    def _event_view(e: dict, truth: bool) -> dict | None:
        out = aar.public_event(e)
        if truth:
            return out
        if e.get("_blue_hidden"):
            return None
        if e.get("_blue_text"):
            out["text"] = e["_blue_text"]
        if e.get("_blue_outcome_hidden"):
            out["outcome"] = None
        return out

    def _next_eid(self, s: Session) -> str:
        s.seq += 1
        return f"{s.session_id}-E{s.seq}"

    def _eng(self, s: Session, engagement_id: str) -> Engagement | None:
        for e in reversed(s.engagements):
            if e.engagement_id == engagement_id:
                return e
        return None

    # ------------------------------------------------------------ end --
    async def end(self, *, reason: str = "operator") -> dict:
        """End the session (§5.2.9): stop the thread, file the AAR (never
        `latest`), remove scenario objects and tracks, revive downed drones,
        audit, delete `wargame.json`. `{ended, aar_id, resource}`."""
        srv = self.srv
        with srv._mode_lock, self._lock:
            s = self._session
            if s is None:
                return _refused("wargame_inactive")
            if self._ending:
                return _refused("wargame_ending")
            self._ending = True
        try:
            await asyncio.to_thread(self._stop_thread)
            await self._await_losses()
            ended = _now_ms()
            with self._lock:
                for p in s.pendings.values():
                    if p.state in ("proposed", "authorized"):
                        p.state = "expired"
                        e = self._eng(s, p.pending_id)
                        if e is not None:
                            e.phase = "expired"
                self._event(s, "session_ended", "blue",
                            f"Simulated wargame {s.session_id} ended ({reason}).")
                units = [self._unit_row(s, u, truth=True) for u in self._units_sorted(s)]
                events = list(s.events)
                accuracy = [self._accuracy(s, e) for e in s.engagements
                            if e.kind == "blue_strike" and e.fired_at_ms is not None]
                names = list(s.object_names)
                owned = set(names)
                tids = list(s.track_ids)
                lost = sorted(set(s.lost) | set(getattr(srv, "vehicles_lost", {}) or {}))
            tids += [t.track_id for t in srv.tracks.tracks()
                     if t.name in owned and t.track_id not in tids]
            report = aar.build_aar(
                session_id=s.session_id, theater_id=s.theater_id,
                started_at_ms=s.started_at_ms, ended_at_ms=ended, seed=s.seed,
                time_scale=self._time_scale(), red_engages=s.red_engages,
                reveal_red=s.reveal_red, units=units, events=events,
                sorties=self._sorties(s, lost), bda_accuracy=accuracy, track_ids=tids,
                reason=reason)
            srv.reports[report["id"]] = report          # never "latest" (V21)
            destroy_errors = []
            for name in names:
                try:
                    await srv.backend.destroy_object(name)
                except Exception as exc:  # noqa: BLE001 — reported in the audit row
                    destroy_errors.append(f"{name}: {type(exc).__name__}")
                srv.targets.pop(name, None)
            removed = srv.tracks.remove(tids)
            for tid in tids:
                srv.store.tracks.delete(tid)
            srv.pol.forget(tids)
            revived = []
            for v in lost:
                with contextlib.suppress(Exception):
                    out = await srv.revive_vehicle(v)
                    if out.get("revived"):
                        revived.append(v)
            await asyncio.to_thread(self.flush)
            self._audit("wargame_session_ended", f"simulated wargame {s.session_id} ended",
                        session_id=s.session_id, reason=reason, aar_id=report["id"],
                        track_ids=tids, tracks_removed=removed, objects=names,
                        destroy_errors=destroy_errors, revived=revived, simulated=True)
            aar.remove_session(self._path())
            with srv._mode_lock, self._lock:
                self._session, self._names = None, frozenset()
                self.last = {"session_id": s.session_id, "aar_id": report["id"],
                             "ended_at_ms": ended}
                self.revision += 1
            return {"ended": True, "session_id": s.session_id, "aar_id": report["id"],
                    "resource": report["resource"], "revived": revived,
                    "simulated": True, "note": NOTIONAL_NOTE}
        finally:
            self._ending = False

    async def _await_losses(self) -> None:
        """Let drone losses already handed to the tasking loop finish, so a
        drone is never revived before it was downed."""
        futures, self._loss_futures = list(self._loss_futures), []
        for f in futures:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.wrap_future(f), LOSS_WAIT_S)

    def _sorties(self, s: Session, lost: list[str]) -> list[dict]:
        """Per drone: missions flown during the session, and whether it was lost."""
        start_s = s.started_at_ms / 1000.0
        per: dict[str, list[dict]] = {}
        for m in list(getattr(self.srv, "missions", {}).values()):
            if not isinstance(m, dict) or float(m.get("started") or 0.0) < start_s:
                continue
            per.setdefault(str(m.get("vehicle")), []).append(
                {"mission_id": m.get("mission_id"), "kind": m.get("kind"),
                 "state": m.get("state")})
        names = set(per) | set(lost) | set(getattr(self.srv, "_last_tele", {}) or {})
        return [{"vehicle": v, "missions": per.get(v, []), "missions_flown": len(per.get(v, [])),
                 "lost": v in lost, "lost_by": (s.lost.get(v) or {}).get("by"),
                 "simulated": True} for v in sorted(names)]

    # ----------------------------------------------------------- step --
    def step(self, now_s: float, tele: dict[str, dict]) -> None:
        """One adjudication pass (§5.2.5), `rng_red` only. `now_s` is SIM seconds
        (`sim.sim_elapsed_s()`); `tele` is `srv._last_tele`. Holds `self._lock`
        for the pass; heavy work is only submitted, never run, here."""
        t0 = time.perf_counter()
        sim = self._sim()
        env = self._environment() if self._session is not None else None
        losses: list[tuple[str, str]] = []
        jobs: list[tuple] = []
        self._tls.deferred = True
        try:
            self._pass(now_s, tele, sim, env, losses, jobs)
        finally:
            self._tls.deferred = False
        for vehicle, cause in losses:
            self._schedule_loss(vehicle, cause)
        # One submission per pass, last: it is in the budget, the computation is not.
        self._submit_horizons(jobs)
        ms = (time.perf_counter() - t0) * 1000.0
        self.step_ms = round(ms, 3)
        if ms > STEP_BUDGET_MS:
            self._audit_limited("wargame_step_slow", f"step took {ms:.1f} ms",
                                session_id=self.session_id, step_ms=round(ms, 1))

    def _pass(self, now_s: float, tele: dict, sim: Any, env: Any, losses: list,
              jobs: list) -> None:
        with self._lock:
            s = self._session
            if s is None or self._ending:
                return
            changed = self._housekeeping(s, now_s)
            changed |= self._truth_positions(s, sim, now_s, jobs)
            if s.red_engages:
                changed |= self._red_air(s, now_s, tele or {}, env, losses)
                changed |= self._red_ground(s, now_s)
            changed |= self._update_axes(s)
            changed |= self._bda_pass(s)
            if changed:
                self.revision += 1

    def _schedule_loss(self, vehicle: str, cause: str) -> None:
        """Down `vehicle` on the tasking loop (C6); the future is kept for `end`."""
        srv = self.srv
        try:
            fut = asyncio.run_coroutine_threadsafe(srv.lose_vehicle(vehicle, cause),
                                                   srv.tasking.loop)
        except Exception as exc:  # noqa: BLE001 — audited; the drone stays in `lost`
            self._audit_limited("wargame_loss_failed", f"{type(exc).__name__}: {exc}",
                                vehicle=vehicle)
            return
        self._loss_futures.append(fut)

    def _housekeeping(self, s: Session, now_s: float) -> bool:
        changed = False
        now = _now_ms()
        for p in s.pendings.values():
            if p.state in ("proposed", "authorized") and now > p.expires_at_ms:
                self._expire(s, p, "timed out")
                changed = True
        for u in s.units.values():
            if release_suppression(u, now_s):
                changed = True
        return changed

    def _expire(self, s: Session, p: Pending, why: str) -> None:
        p.state = "expired"
        e = self._eng(s, p.pending_id)
        if e is not None:
            e.phase = "expired"
        self._event(s, "blue_strike_expired", "blue",
                    f"Simulated engagement {p.pending_id} expired ({why}).")

    def _truth(self, s: Session, u: Unit, sim: Any, now_s: float | None = None) -> bool:
        """Refresh a red unit's truth position from the fake sim (memory only)."""
        if u.side != "red" or not u.object_name or sim is None:
            return False
        pos = sim.object_geo(u.object_name)
        if pos is None:
            if u.state == "destroyed":
                return False
            u.state, u.ammo, u.state_until_s = "destroyed", 0, None
            u.in_envelope.clear()
            u.detecting.clear()
            self._event(s, "object_removed", "red",
                        f"{u.designator} was removed from the simulation (object removed).",
                        outcome="destroyed", blue_hidden=True, sim_s=now_s)
            return True
        lat, lon, hae = float(pos[0]), float(pos[1]), float(pos[2])
        u.lat, u.lon, u.alt_hae_m = lat, lon, hae
        u.alt_msl_m = hae - float(getattr(s.lattice, "undulation_m", 0.0) or 0.0)
        return False

    def _moved(self, u: Unit) -> bool:
        """Revision rule (§5.2.4): a position counts after 25 m, or 5 s of drift."""
        now = time.time()
        if u.pos_rev is None:
            u.pos_rev = (u.lat, u.lon, now)
            return False
        d = haversine_m(u.pos_rev[0], u.pos_rev[1], u.lat, u.lon)
        if d >= POS_BUMP_M or (d > 0.5 and now - u.pos_rev[2] >= POS_BUMP_S):
            u.pos_rev = (u.lat, u.lon, now)
            return True
        return False

    def _truth_positions(self, s: Session, sim: Any, now_s: float, jobs: list) -> bool:
        changed = False
        for u in s.units.values():
            if u.side != "red":
                continue
            changed |= self._truth(s, u, sim, now_s)
            changed |= self._moved(u)
            if u.state != "destroyed" and self._needs_horizon(u):
                self._queue_horizon(s, u, jobs)
        return changed

    @staticmethod
    def _needs_horizon(u: Unit) -> bool:
        cls = CLASSES[u.wg_class]
        if u.side != "red" or cls.role not in ("air", "sensor") or u.horizon_pending:
            return False
        if u.horizon_at is None:
            return True
        return haversine_m(u.horizon_at[0], u.horizon_at[1], u.lat, u.lon) > HORIZON_MOVE_M

    @staticmethod
    def _queue_horizon(s: Session, u: Unit, jobs: list) -> None:
        """Queue a horizon job (§5.2.4). Caller holds the lock; the jobs of one
        pass are submitted together after it is released (`_submit_horizons`)."""
        u.horizon_pending = True
        jobs.append((s.session_id, u.unit_id, (u.lat, u.lon),
                     (u.lat, u.lon, u.alt_msl_m, s.lattice, CLASSES[u.wg_class])))

    def _submit_horizons(self, jobs: list) -> None:
        if jobs:
            self._pool("horizon").submit(self._horizon_batch, list(jobs))

    def _horizon_batch(self, jobs: list) -> None:
        for sid, uid, at, args in jobs:
            self._horizon_job(sid, uid, at, args)
            time.sleep(0)                   # let the wargame thread in between jobs

    def _horizon_job(self, sid: str, uid: str, at: tuple[float, float], args: tuple) -> None:
        """Runs on the horizon worker: compute, then commit under the lock (never
        a done-callback, which could run on the submitting thread, lock held)."""
        try:
            out = _compute_horizon(*args)
        except Exception as exc:  # noqa: BLE001 — counted; the unit keeps clear LOS
            out = None
            self.errors += 1
            self._audit_limited("wargame_horizon_failed", f"{type(exc).__name__}: {exc}",
                                session_id=sid, unit_id=uid)
        with self._lock:
            s = self._session
            u = s.units.get(uid) if s is not None and s.session_id == sid else None
            if u is None:
                return
            u.horizon_pending = False
            if out is not None:
                u.horizon, u.fans, u.horizon_at = out["horizon"], out["fans"], at
                self.revision += 1

    def _units_sorted(self, s: Session, side: str | None = None) -> list[Unit]:
        return [u for u in sorted(s.units.values(), key=lambda x: x.unit_id)
                if side is None or u.side == side]

    @staticmethod
    def _drone(t: dict, und: float) -> tuple[float, float, float, float, float] | None:
        """(lat, lon, alt_hae, alt_msl on the lattice datum, speed) of a telemetry row."""
        try:
            lat, lon, hae = float(t["lat"]), float(t["lon"]), float(t["alt_hae_m"])
        except (KeyError, TypeError, ValueError):
            return None
        return lat, lon, hae, hae - und, float(t.get("speed_mps") or 0.0)

    def _red_air(self, s: Session, now_s: float, tele: dict, env: Any,
                 losses: list[tuple[str, str]]) -> bool:
        """Red air defence against airborne drones (§5.2.5 step 3)."""
        und = float(getattr(s.lattice, "undulation_m", 0.0) or 0.0)
        lost_srv = set(getattr(self.srv, "vehicles_lost", {}) or {})
        pos: dict[str, tuple] = {}
        for v in sorted(tele):
            t = tele[v]
            if not isinstance(t, dict) or int(t.get("landed_state") or 0) == 0:
                continue
            if v in s.lost or v in lost_srv:
                continue
            p = self._drone(t, und)
            if p is not None:
                pos[v] = p
        air, changed = [], False
        for u in self._units_sorted(s, "red"):
            if CLASSES[u.wg_class].role != "air":
                continue
            if u.state in ("active", "damaged"):
                air.append(u)
            elif u.in_envelope or u.detecting:      # suppressed or destroyed: blind
                u.in_envelope.clear()
                u.detecting.clear()
                changed = True
        if not pos:
            for u in air:
                if u.in_envelope or u.detecting:
                    u.in_envelope.clear()
                    u.detecting.clear()
                    changed = True
            return changed
        cued: dict[str, list[Unit]] = {}
        for r in self._units_sorted(s, "red"):
            rc = CLASSES[r.wg_class]
            if rc.role != "sensor" or r.state != "active":
                continue
            for v, (lat, lon, hae, msl, _spd) in pos.items():
                slant = math.hypot(haversine_m(r.lat, r.lon, lat, lon), hae - r.alt_hae_m)
                if slant > rc.detection_range_m or not visible(r.horizon, lat, lon, msl):
                    continue
                if s.rng_red.random() < p_detect(rc, slant, env):
                    cued.setdefault(v, []).append(r)
        now_ms = _now_ms()
        for u in air:
            before = (frozenset(u.in_envelope), frozenset(u.detecting))
            u.in_envelope.clear()
            u.detecting.clear()
            cls = CLASSES[u.wg_class]
            reach = threat_range(cls, u.damaged)
            fired = False
            for v, (lat, lon, hae, msl, spd) in pos.items():
                if v in s.lost:
                    continue
                dz = hae - u.alt_hae_m
                if not 0.0 <= dz <= cls.threat_ceiling_m:
                    continue
                slant = math.hypot(haversine_m(u.lat, u.lon, lat, lon), dz)
                if slant > cls.detection_range_m or not visible(u.horizon, lat, lon, msl):
                    continue
                cue = any(haversine_m(r.lat, r.lon, u.lat, u.lon)
                          <= CLASSES[r.wg_class].detection_range_m for r in cued.get(v, ()))
                if s.rng_red.random() >= p_detect(cls, slant, env, cue=cue):
                    continue
                u.detecting.add(v)
                if slant > reach:
                    continue
                u.in_envelope.add(v)
                if fired or u.ammo <= 0 or now_s < u.next_shot_s:
                    continue
                fired = True
                self._red_shot(s, u, v, (lat, lon), slant, dz, spd, now_s, now_ms, losses)
            changed |= (frozenset(u.in_envelope), frozenset(u.detecting)) != before or fired
        return changed

    def _red_shot(self, s: Session, u: Unit, v: str, at: tuple[float, float], slant: float,
                  dz: float, speed: float, now_s: float, now_ms: int,
                  losses: list[tuple[str, str]]) -> None:
        cls = CLASSES[u.wg_class]
        pk = round(p_kill_air(cls, slant, dz, speed, u.damaged), 2)
        k = s.rng_red.random()
        outcome = "destroyed" if k < pk else "missed"
        u.ammo -= 1
        u.next_shot_s = now_s + cls.cycle_s * (DAMAGED_CYCLE_FACTOR if u.damaged else 1.0)
        u.last_fired_ms = now_ms
        eid = self._next_eid(s)
        reach = threat_range(cls, u.damaged)
        s.engagements.append(Engagement(
            engagement_id=eid, kind="red_shot", phase="adjudicated",
            attacker=f"frc:{u.unit_id}", target=f"veh:{v}", target_label=v, vector=None,
            p_notional={"effect": pk, "destroyed": pk, "damaged": 0.0, "suppressed": 0.0},
            inputs=[f"Slant range {_km(slant)} of {_km(reach)}",
                    f"{dz:.0f} m above the unit, ceiling {cls.threat_ceiling_m:.0f} m"],
            outcome=outcome, consequence="own_loss" if outcome == "destroyed" else "none",
            seed=s.seed, draw=s.rng_red.draw, proposed_at_ms=now_ms,
            adjudicated_at_ms=now_ms, unit_id=u.unit_id, attacker_label=u.designator,
            target_point=at, attacker_point=(u.lat, u.lon), stream="red"))
        word = "downed it" if outcome == "destroyed" else "missed"
        self._event(s, "red_shot", "red",
                    f"{u.designator} fired at {v}: {word} (simulated).", outcome=outcome,
                    blue_text=f"{RED_AD_HIDDEN} fired at {v}: {word} (simulated).",
                    sim_s=now_s, engagement_id=eid)
        if outcome == "destroyed":
            s.lost[v] = {"by": u.designator, "at_ms": now_ms, "engagement_id": eid}
            losses.append((v, u.designator))

    def _red_ground(self, s: Session, now_s: float) -> bool:
        """Red ground and indirect fire on blue scenario units (§5.2.5 step 4)."""
        blues = [b for b in self._units_sorted(s, "blue") if b.wg_class != "blue_strike_air"]
        if not any(b.state != "destroyed" for b in blues):
            return False
        reds = self._units_sorted(s, "red")
        spotters = [r for r in reds if r.state != "destroyed"
                    and CLASSES[r.wg_class].detection_range_m > 0]
        changed = False
        for u in reds:
            cls = CLASSES[u.wg_class]
            if cls.role not in ("ground", "indirect") or u.state not in ("active", "damaged"):
                continue
            if u.ammo <= 0 or now_s < u.next_shot_s:
                continue
            limit = cls.ground_range_m * (DAMAGED_RANGE_FACTOR if u.damaged else 1.0)
            cands = []
            for b in blues:
                if b.state == "destroyed":
                    continue
                d = haversine_m(u.lat, u.lon, b.lat, b.lon)
                if d > limit:
                    continue
                if cls.role == "ground":
                    seen = d <= cls.detection_range_m
                else:
                    seen = any(haversine_m(r.lat, r.lon, b.lat, b.lon)
                               <= CLASSES[r.wg_class].detection_range_m for r in spotters)
                if seen:
                    cands.append((d, b.unit_id, b))
            if not cands:
                continue
            d, _, b = min(cands, key=lambda c: (c[0], c[1]))
            self._ground_shot(s, u, b, d, now_s)
            changed = True
        return changed

    def _ground_shot(self, s: Session, u: Unit, b: Unit, d: float, now_s: float) -> None:
        cls = CLASSES[u.wg_class]
        bands = effect_bands(p_ground(cls, d, CLASSES[b.wg_class].hardness, u.damaged))
        drawn, _ = draw_outcome(s.rng_red, bands)
        state = apply_outcome(b, drawn, now_s)
        outcome = "destroyed" if drawn != "missed" and state == "destroyed" else drawn
        u.ammo -= 1
        u.next_shot_s = now_s + cls.cycle_s * (DAMAGED_CYCLE_FACTOR if u.damaged else 1.0)
        now_ms = _now_ms()
        u.last_fired_ms = now_ms
        eid = self._next_eid(s)
        consequence = ("own_loss" if outcome == "destroyed" else
                       "own_damage" if outcome in ("damaged", "suppressed") else "none")
        s.engagements.append(Engagement(
            engagement_id=eid, kind="red_ground", phase="adjudicated",
            attacker=f"frc:{u.unit_id}", target=f"frc:{b.unit_id}", target_label=b.designator,
            vector=None, p_notional=bands,
            inputs=[f"Range {_km(d)} of {_km(cls.ground_range_m)}",
                    f"Target hardness {CLASSES[b.wg_class].hardness:.2f} (notional)"],
            outcome=outcome, consequence=consequence, seed=s.seed, draw=s.rng_red.draw,
            proposed_at_ms=now_ms, adjudicated_at_ms=now_ms, unit_id=u.unit_id,
            attacker_label=u.designator, target_point=(b.lat, b.lon),
            attacker_point=(u.lat, u.lon), stream="red"))
        self._event(s, "red_ground", "red",
                    f"{u.designator} fired on {b.designator}: {outcome} (simulated).",
                    outcome=outcome,
                    blue_text=f"{RED_GROUND_HIDDEN} fired on {b.designator}: {outcome} "
                              "(simulated).", sim_s=now_s, engagement_id=eid)

    def _update_axes(self, s: Session) -> bool:
        """Red axes of advance toward blue objectives (§5.2.5 step 5)."""
        rows = {f"axis-{a['unit_id']}": a for a in red_axes(list(s.units.values()), s.units)}
        changed = False
        for vid in [k for k in s.vectors if k.startswith("axis-") and k not in rows]:
            del s.vectors[vid]
            changed = True
        for vid, a in rows.items():
            old = s.vectors.get(vid)
            if old is None or haversine_m(*old["from"], *a["from"]) >= POS_BUMP_M \
                    or old.get("objective_id") != a.get("objective_id"):
                s.vectors[vid] = {**a, "side": "red", "proposed": False,
                                  "created_ms": (old or {}).get("created_ms") or _now_ms()}
                changed = True
        return changed

    def _bda_pass(self, s: Session) -> bool:
        """Recount looks for every fired blue strike (§5.2.9); a tier change is an event."""
        changed = False
        for e in s.engagements:
            if e.kind != "blue_strike" or e.fired_at_ms is None or not e.unit_id:
                continue
            u = s.units.get(e.unit_id)
            if u is None:
                continue
            new = bda.assess(u.state, u.damaged, self._gated_tracks(s, u), e.fired_at_ms)
            old = e.bda or dict(bda.NO_BDA)
            if new == old:
                continue
            e.bda = new
            changed = True
            if bda.tier_changed(old, new):
                self._event(s, "bda_assessed", "blue", bda.bda_text(e.target_label, new),
                            outcome=new["state"], engagement_id=e.engagement_id)
        return changed

    # ---------------------------------------------------------- gates --
    def _unit_for_object(self, s: Session, name: str) -> Unit | None:
        for u in s.units.values():
            if u.object_name == name and u.side == "red":
                return u
        return None

    def _track_ok(self, s: Session, t: Any, u: Unit | None = None) -> Unit | None:
        """Provenance gate conditions 2-5 for one track (§5.2.6); its unit or None."""
        rec = getattr(self.srv, "targets", {}).get(getattr(t, "name", None))
        if not isinstance(rec, dict) or rec.get("provenance") != "scenario" \
                or rec.get("session_id") != s.session_id:
            return None
        unit = self._unit_for_object(s, t.name)
        if unit is None or (u is not None and unit is not u):
            return None
        try:
            if float(t.first_seen) < float(unit.spawned_at):
                return None
            if haversine_m(float(t.lat), float(t.lon), unit.lat, unit.lon) > TRACK_TRUTH_MAX_M:
                return None
        except (TypeError, ValueError):
            return None
        return unit

    def _gated_tracks(self, s: Session, u: Unit) -> list[Any]:
        """The session's tracks that pass the gate for unit `u` (BDA looks)."""
        out = []
        for tid in list(s.track_ids):
            t = self.srv.tracks.get(tid)
            if t is not None and self._track_ok(s, t, u) is not None:
                out.append(t)
        return out

    def gate_track(self, track_id: Any) -> tuple[Unit, Any]:
        """The provenance gate (§5.2.6): `(unit, track)` or `WargameRefused`."""
        with self._lock:
            s = self._active()
            if s is None:
                raise WargameRefused("wargame_inactive")
            return self._gate(s, track_id)

    def _gate(self, s: Session, track_id: Any, *, refresh: bool = True) -> tuple[Unit, Any]:
        t = self.srv.tracks.get(_strip(track_id, "trk:"))
        if t is None:
            raise WargameRefused("not_a_scenario_unit")
        rec = self.srv.targets.get(t.name)
        unit = self._unit_for_object(s, t.name) if isinstance(rec, dict) else None
        if refresh and unit is not None:
            self._truth(s, unit, self._sim())
        if self._track_ok(s, t) is None:
            raise WargameRefused("not_a_scenario_unit")
        return unit, t

    def _site_check(self, s: Session, points: list[tuple[float, float]]) -> None:
        """The real-site gate on each point (truth and track)."""
        for lat, lon in points:
            code = site_gate(s.exclusion, s.ao.get("pois") or (), lat, lon)
            if code is not None:
                raise WargameRefused(code)

    @staticmethod
    def _shooter_ok(shooter: Unit | None) -> bool:
        if shooter is None or shooter.side != "blue":
            return False
        return (CLASSES[shooter.wg_class].role == "shooter"
                and shooter.state in ("active", "damaged") and shooter.ammo > 0)

    @staticmethod
    def _in_range(shooter: Unit, lat: float, lon: float) -> tuple[bool, float, float | None]:
        """(ok, range_m, limit_m|None) — None is the whole AO."""
        d = haversine_m(shooter.lat, shooter.lon, lat, lon)
        sr = CLASSES[shooter.wg_class].strike_range_m
        if sr is None:
            return True, d, None
        limit = sr * (DAMAGED_RANGE_FACTOR if shooter.damaged else 1.0)
        return d <= limit, d, limit

    def _recheck(self, s: Session, p: Pending) -> tuple[Unit, Unit, Any, float]:
        """Checks 2 and 4-6 again (§5.2.7): `(shooter, unit, track, range_m)`."""
        shooter = s.units.get(p.shooter_id)
        if not self._shooter_ok(shooter):
            raise WargameRefused("shooter_unavailable")
        unit, track = self._gate(s, p.target_track_id)
        self._site_check(s, [(unit.lat, unit.lon), (float(track.lat), float(track.lon))])
        ok, d, _ = self._in_range(shooter, float(track.lat), float(track.lon))
        if not ok:
            raise WargameRefused("out_of_range")
        return shooter, unit, track, d

    @staticmethod
    def _confidence(track: Any) -> str:
        try:
            return str(assess_confidence(track)["level"])
        except Exception:  # noqa: BLE001 — an unassessable contact is not identified
            return "possible"

    def _accuracy(self, s: Session, e: Engagement) -> dict:
        u = s.units.get(e.unit_id or "")
        return bda.accuracy_row(e.engagement_id, e.target_label, e.bda, e.outcome,
                                u.state if u else "unknown", bool(u and u.damaged))

    # ---------------------------------------------------------- spawn --
    def _world(self, s: Session) -> World:
        """The spawn-rule snapshot (§5.2.6). Caller holds the lock."""
        srv, sim = self.srv, self._sim()
        objects = []
        for name, rec in list(getattr(srv, "targets", {}).items()):
            if name in self._names or not isinstance(rec, dict):
                continue
            pos = sim.object_geo(name) if sim is not None else None
            lat, lon = (pos[0], pos[1]) if pos else (rec.get("lat"), rec.get("lon"))
            if lat is not None and lon is not None:
                objects.append((float(lat), float(lon)))
        for t in srv.tracks.tracks():
            if t.name not in self._names:
                objects.append((float(t.lat), float(t.lon)))
        fence = tuple(tuple(p) for p in (getattr(srv.envelope, "geofence", None) or ()))
        return World(ao=tuple(s.ao["polygon"]), geofence=fence, exclusion=s.exclusion,
                     pois=tuple(s.ao.get("pois") or ()), home=tuple(s.ao["home"]),
                     units=tuple((u.lat, u.lon) for u in s.units.values()),
                     objects=tuple(objects), n_units=len(s.units))

    def _reserve(self, s: Session, rows: list[dict]) -> list[Unit]:
        """Ids, designators and object names for `rows` (under the lock). Red
        names join `owns_name` before anything is spawned."""
        out = []
        for r in rows:
            cls = CLASSES[r["wg_class"]]
            key = (cls.side, cls.prefix_slug)
            s.counters[key] = n = s.counters.get(key, 0) + 1
            name = None
            if cls.side == "red":
                s.red_seq += 1
                name = object_name(cls.ob_key, s.num, s.red_seq)
                s.object_names.append(name)
            out.append(Unit(unit_id=unit_id(cls.side, cls.prefix_slug, n), side=cls.side,
                            wg_class=cls.key, designator=designator(cls.side, cls.prefix_slug, n),
                            lat=float(r["lat"]), lon=float(r["lon"]), alt_hae_m=0.0,
                            alt_msl_m=0.0, object_name=name, spawned_at=0.0, ammo=cls.ammo,
                            objective=r.get("objective"), slot=r.get("slot")))
        self._names = frozenset(s.object_names)
        return out

    async def _place(self, s: Session, rows: list[dict], snapshot: tuple) -> dict | list[Unit]:
        """Reserve, spawn red objects, register every unit; all or nothing."""
        with self._lock:
            units = self._reserve(s, rows)
            if any(r.get("objective_slot") for r in rows):
                first = {}
                for u in units:
                    first.setdefault(u.slot, u.unit_id)
                for u, r in zip(units, rows, strict=True):
                    if r.get("objective_slot"):
                        u.objective = first.get(r["objective_slot"])
        srv, spawned = self.srv, []
        und = float(getattr(s.lattice, "undulation_m", 0.0) or 0.0)
        try:
            for u in units:
                u.spawned_at = time.time()       # before the object can be seen (gate 4)
                if u.object_name:
                    rec = await srv._spawn_object_record(
                        u.object_name, CLASSES[u.wg_class].ob_key, u.lat, u.lon,
                        provenance="scenario", session_id=s.session_id, side="red",
                        unit_id=u.unit_id, designator=u.designator)
                    spawned.append(u.object_name)
                    u.lat, u.lon = float(rec["lat"]), float(rec["lon"])
                    u.alt_msl_m, u.alt_hae_m = float(rec["alt_msl_m"]), float(rec["alt_hae_m"])
                else:
                    g = float(srv.ground_msl_at(u.lat, u.lon)["alt_msl_m"])
                    u.alt_msl_m, u.alt_hae_m = g, g + und
        except Exception as exc:  # noqa: BLE001 — rolled back and reported
            await self._unplace(s, spawned, snapshot)
            self._audit("wargame_spawn_failed", f"{type(exc).__name__}: {exc}",
                        session_id=s.session_id)
            return _refused("spawn_failed")
        if self._active() is not s:                 # the session ended meanwhile
            await self._unplace(s, spawned, snapshot)
            return _refused("wargame_inactive")
        jobs: list[tuple] = []
        with self._lock:
            for u in units:
                u.pos_rev = (u.lat, u.lon, time.time())
                s.units[u.unit_id] = u
                if self._needs_horizon(u):
                    self._queue_horizon(s, u, jobs)
                where = relative_position(s.ao["center"], u.lat, u.lon)
                self._event(s, "unit_placed", u.side,
                            f"{u.designator} ({CLASSES[u.wg_class].label}) placed {where}.",
                            blue_hidden=u.side == "red", unit_id=u.unit_id)
            self.revision += 1
            record = self._record(s)
        self._submit_horizons(jobs)
        self._write_record(record)
        for u in units:
            await self._route_to_objective(s, u)
        return units

    async def _unplace(self, s: Session, spawned: list[str], snapshot: tuple) -> None:
        """Undo a partial placement: remove its objects, restore the counters."""
        srv = self.srv
        for name in spawned:
            with contextlib.suppress(Exception):
                await srv.backend.destroy_object(name)
            srv.targets.pop(name, None)
        with self._lock:
            s.counters, s.red_seq, names = snapshot
            s.object_names[:] = names
            if self._session is s:
                self._names = frozenset(names)

    async def _route_to_objective(self, s: Session, u: Unit) -> None:
        """A mobile red unit with an objective drives to `0.8 x max(ground range,
        500 m)` short of it (§5.2.6)."""
        cls = CLASSES[u.wg_class]
        obj = s.units.get(u.objective or "")
        if u.side != "red" or not u.object_name or obj is None or cls.speed_mps <= 0:
            return
        stand = 0.8 * max(cls.ground_range_m, 500.0)
        if haversine_m(u.lat, u.lon, obj.lat, obj.lon) <= stand:
            return
        stop = destination(obj.lat, obj.lon, stand, bearing_deg(obj.lat, obj.lon, u.lat, u.lon))
        try:
            out = await self.srv._move_target(u.object_name, [{"lat": stop[0], "lon": stop[1]}],
                                              cls.speed_mps, False)
        except Exception as exc:  # noqa: BLE001 — the unit stays where it is
            out = {"error": f"{type(exc).__name__}: {exc}"}
        if isinstance(out, dict) and out.get("ok"):
            u.route_to = stop
        else:
            self._audit("wargame_route_failed", str(out), session_id=s.session_id,
                        unit_id=u.unit_id)

    def _snapshot(self, s: Session) -> tuple:
        return (dict(s.counters), s.red_seq, list(s.object_names))

    async def spawn(self, side: str, wg_class: str, lat: float, lon: float, *,
                    count: int = 1, objective_id: str | None = None) -> dict:
        """Add `count` scenario units of `wg_class` at (lat, lon) (§5.2.6). The first
        must sit exactly there; the rest follow the spiral around it."""
        cls = CLASSES.get(str(wg_class))
        if cls is None:
            return _refused("unknown_class")
        if cls.side != side:
            return _refused("side_mismatch")
        try:
            lat, lon, n = float(lat), float(lon), int(count)
        except (TypeError, ValueError):
            return _refused("invalid_parameter", "lat, lon and count must be numbers.")
        if isinstance(count, bool) or not 1 <= n <= 6 or not (
                math.isfinite(lat) and math.isfinite(lon)):
            return _refused("invalid_parameter", "count must be 1 to 6 at a finite position.")
        with self._lock:
            s = self._active()
            if s is None:
                return _refused("wargame_inactive")
            if self._spawning:
                return _refused("spawn_busy")
            obj = None
            if objective_id:
                obj = s.units.get(_strip(objective_id, "frc:"))
                if obj is None or obj.side == side or obj.state == "destroyed":
                    return _refused("unknown_objective")
            positions, code = plan_positions(self._world(s), [(side, lat, lon)] * n,
                                             move_first=False)
            if positions is None:
                return _refused(code)
            rows = [{"wg_class": cls.key, "lat": p[0], "lon": p[1],
                     "objective": obj.unit_id if obj else None} for p in positions]
            snap = self._snapshot(s)
            self._spawning = True
        try:
            units = await self._place(s, rows, snap)
        finally:
            self._spawning = False
        if isinstance(units, dict):
            return units
        with self._lock:
            out = [self._unit_row(s, u, truth=True) for u in units]
        caveats = sorted({c for r in out for c in r["caveats"]})
        return {"units": out, "caveats": caveats, "simulated": True, "note": NOTIONAL_NOTE}

    async def generate(self, template: str, intensity: str = "medium",
                       ad_class: str | None = None) -> dict:
        """Place a whole scenario template (§5.2.2), all or nothing. Red units are
        fog-filtered from the answer unless the session reveals red."""
        if template not in TEMPLATES:
            return _refused("unknown_template")
        if intensity not in INTENSITIES:
            return _refused("invalid_parameter", "intensity must be low, medium or high.")
        with self._lock:
            s = self._active()
            if s is None:
                return _refused("wargame_inactive")
            if self._spawning:
                return _refused("spawn_busy")
            geo = {k: s.ao[k] for k in ("half_extent_m", "half_diagonal_m")}
            try:
                planned = template_positions(template, intensity, s.ao["center"], s.ao["home"],
                                             geo, ad_class=ad_class or None)
            except ValueError:
                return _refused("unknown_class")
            reqs = [(p["side"], p["lat"], p["lon"]) for p in planned]
            positions, code = plan_positions(self._world(s), reqs)
            if positions is None:
                return _refused(code)
            rows = [{"wg_class": p["wg_class"], "lat": q[0], "lon": q[1], "slot": p["slot"],
                     "objective_slot": p["objective"]} for p, q in zip(planned, positions,
                                                                     strict=True)]
            moved = sum(1 for (_, la, lo), q in zip(reqs, positions, strict=True)
                        if haversine_m(la, lo, q[0], q[1]) > 1.0)
            snap = self._snapshot(s)
            self._spawning = True
        try:
            units = await self._place(s, rows, snap)
        finally:
            self._spawning = False
        if isinstance(units, dict):
            return units
        with self._lock:
            show_red = s.reveal_red
            out = [self._unit_row(s, u, truth=True) for u in units
                   if u.side == "blue" or show_red]
            ad = next((u.wg_class for u in units
                       if u.side == "red" and CLASSES[u.wg_class].role == "air"), None)
            self._event(s, "scenario_generated", "blue",
                        f"Scenario {template} ({intensity}) placed: "
                        f"{sum(u.side == 'blue' for u in units)} blue units.")
        # An envelope that covers the AO is said even in blue view (D5 honesty);
        # the caveat names no unit.
        caveats = sorted({c for u in units for c in class_caveats(CLASSES[u.wg_class], geo)})
        return {"template": template, "intensity": intensity,
                "ad_class": ad if show_red else None, "units": out,
                "blue_placed": sum(u.side == "blue" for u in units),
                "red_placed": sum(u.side == "red" for u in units), "moved_by_spiral": moved,
                "caveats": caveats, "simulated": True, "note": NOTIONAL_NOTE}

    # ------------------------------------------------ threat basis --
    def _sensed_threats(self, s: Session) -> list[tuple]:
        """Gated tracks perceived as a red air-defence class, at the TRACK position
        with that class's table numbers (§5.2.8, R18). Nothing else qualifies."""
        out = []
        for tid in list(s.track_ids):
            t = self.srv.tracks.get(tid)
            if t is None or self._track_ok(s, t) is None:
                continue
            cls = red_class_for_ob(getattr(t, "ob_class", None))
            if cls is None or cls.role != "air":
                continue
            lat, lon = float(t.lat), float(t.lon)
            out.append((lat, lon, ground_hae_m(s.lattice, lat, lon), cls, False, None))
        return out

    def _truth_threats(self, s: Session) -> list[tuple]:
        """Engine red air units able to fire, at truth, with their horizons."""
        return [(u.lat, u.lon, u.alt_hae_m, CLASSES[u.wg_class], u.damaged, u.horizon)
                for u in self._units_sorted(s, "red")
                if CLASSES[u.wg_class].role == "air" and u.state in ("active", "damaged")]

    def _threat_inputs(self, s: Session, start: tuple, goal: tuple) -> tuple[list, str]:
        """`(threats nearest the leg first, <= 40; basis)`."""
        from .wargame_spawn import edge_distance_m
        basis = "truth" if s.reveal_red else "sensed"
        threats = self._truth_threats(s) if basis == "truth" else self._sensed_threats(s)
        threats.sort(key=lambda th: edge_distance_m(th[0], th[1], [start, goal]))
        return threats[:MAX_THREATS], basis

    # --------------------------------------------------------- strikes --
    async def propose_strike(self, shooter_id: str, target_track_id: str) -> dict:
        """Propose a simulated blue strike on a sensed scenario contact (§5.2.7)."""
        now = _now_ms()
        with self._lock:
            s = self._active()
            if s is None:
                return _refused("wargame_inactive")
            sid, tid = _strip(shooter_id, "frc:"), _strip(target_track_id, "trk:")
            shooter = s.units.get(sid)
            try:
                if not self._shooter_ok(shooter):
                    raise WargameRefused("shooter_unavailable")
                track = self.srv.tracks.get(tid)
                if track is None:
                    raise WargameRefused("not_a_scenario_unit")
                # The provenance gate first (D1): anything that is not a scenario
                # unit is refused as such, whatever the sensor's confidence.
                unit, track = self._gate(s, tid)
                level = self._confidence(track)
                if not confidence_at_least(level, "probable"):
                    raise WargameRefused("insufficient_confidence")
                self._site_check(s, [(unit.lat, unit.lon), (float(track.lat), float(track.lon))])
                ok, rng_m, limit = self._in_range(shooter, float(track.lat), float(track.lon))
                if not ok:
                    raise WargameRefused("out_of_range")
                if sum(p.state in ("proposed", "authorized")
                       for p in s.pendings.values()) >= MAX_PENDING:
                    raise WargameRefused("pending_limit")
            except WargameRefused as exc:
                return exc.as_result()
            cls = CLASSES[shooter.wg_class]
            p_est = dict(strike_probabilities(cls, estimate_hardness(track.ob_class), rng_m, level))
            p_est["package_survive"] = None
            label = label_for_ob(track.ob_class)
            eid = self._next_eid(s)
            goal = (float(track.lat), float(track.lon))
            corridor_in = None
            if cls.key == "blue_strike_air":
                start = tuple(s.ao["home"])
                threats, basis = self._threat_inputs(s, start, goal)
                corridor_in = (s.lattice, threats, start, goal,
                               [tuple(p) for p in self.srv.envelope.geofence], basis)
        corridor = None
        if corridor_in is not None:
            lattice, threats, start, goal, fence, basis = corridor_in
            try:
                comp = await asyncio.to_thread(_compute_corridor, lattice, threats, start, goal,
                                               fence, PACKAGE_ALT_AGL_M, PACKAGE_SPEED_MPS,
                                               self._environment())
            except NoRoute as exc:
                return {**exc.as_result(), "simulated": True}
            p_est["package_survive"] = comp["planned"]["p_survive"]
        with self._lock:
            if self._active() is not s:
                return _refused("wargame_inactive")
            vid = None
            if corridor_in is not None:
                corridor = self._store_corridor(
                    s, comp, frm=f"frc:{shooter.unit_id}", frm_label=shooter.designator,
                    to=f"trk:{track.track_id}", to_label=label, alt=PACKAGE_ALT_AGL_M,
                    speed=PACKAGE_SPEED_MPS, basis=basis, proposed=True)
                vid = corridor["id"]
            args = _exec_args(eid, shooter.unit_id, track.track_id)
            p = Pending(pending_id=eid, shooter_id=shooter.unit_id, target_track_id=track.track_id,
                        unit_id=unit.unit_id, args=args, created_ms=now,
                        expires_at_ms=now + int(PENDING_TTL_S * 1000), p_estimate=p_est,
                        range_m=round(rng_m, 1), corridor=corridor, vector_id=vid, level=level)
            s.pendings[eid] = p
            inputs = [f"Range {_km(rng_m)} of {_km(limit)}" if limit is not None
                      else f"Range {_km(rng_m)} (whole AO)",
                      f"Perceived as {_lower_first(label)}, {level}"]
            s.engagements.append(Engagement(
                engagement_id=eid, kind="blue_strike", phase="proposed",
                attacker=f"frc:{shooter.unit_id}", target=f"trk:{track.track_id}",
                target_label=label, vector=f"vec:{vid}" if vid else None,
                p_notional=dict(p_est), inputs=inputs, outcome=None, consequence="none",
                seed=s.seed, draw=None, proposed_at_ms=now, adjudicated_at_ms=None,
                bda=dict(bda.NO_BDA), unit_id=unit.unit_id, attacker_label=shooter.designator,
                track_id=track.track_id, target_point=goal,
                attacker_point=(shooter.lat, shooter.lon), stream="blue"))
            self._event(s, "blue_strike_proposed", "blue",
                        f"{shooter.designator} proposed a simulated strike on {label} "
                        f"({eid}).", engagement_id=eid)
            self.revision += 1
            caveats = [NOTIONAL_NOTE, ("The estimate uses the perceived class; the umpire "
                                       "adjudicates against the true unit."),
                       *((corridor or {}).get("caveats") or [])]
            return {"pending_id": eid,
                    "shooter": {"id": f"frc:{shooter.unit_id}", "unit_id": shooter.unit_id,
                                "designator": shooter.designator},
                    "target": {"track_id": track.track_id, "label": label,
                               "perceived_class": track.ob_class, "confidence": level},
                    "range_m": p.range_m, "p_estimate": dict(p_est),
                    "corridor": self._corridor_brief(corridor), "expires_at_ms": p.expires_at_ms,
                    "execute_args": dict(args), "caveats": caveats, "simulated": True,
                    "note": NOTIONAL_NOTE}

    def _store_corridor(self, s: Session, comp: dict, *, frm: str, frm_label: str, to: str,
                        to_label: str, alt: float, speed: float, basis: str,
                        proposed: bool, extra_points: list | None = None) -> dict:
        """File `vec:cor-{n}` (§3.2 vector attrs). Caller holds the lock."""
        planned, straight = comp["planned"], comp["straight"]
        path = [list(p) for p in comp["route"].path]
        s.cor_seq += 1
        vid = f"cor-{s.cor_seq}"
        caveats = [SENSED_CAVEAT] if basis == "sensed" else []
        if straight["exposure_s"] > 0 and planned["exposure_s"] >= 0.95 * straight["exposure_s"]:
            caveats.append(NO_AVOID_CAVEAT)
        if not s.lattice.covered:
            caveats.append(TERRAIN_OFF_CAVEAT)
        a, b = path[0], path[-1]
        vec = {"id": vid, "kind": "corridor", "side": "blue", "from": frm, "from_label": frm_label,
               "to": to, "to_label": to_label, "from_point": [round(a[0], 7), round(a[1], 7)],
               "to_point": [round(b[0], 7), round(b[1], 7)],
               "bearing_deg": round(bearing_deg(a[0], a[1], b[0], b[1]), 1),
               "length_m": planned["length_m"], "alt_band": "low" if alt < 150.0 else "medium",
               "alt_agl_m": float(alt), "corridor_m": round(float(s.lattice.cell_m), 1),
               "speed_mps": float(speed), "eta_s": planned["eta_s"],
               "exposure_s": planned["exposure_s"], "p_survive": planned["p_survive"],
               "straight": {"exposure_s": straight["exposure_s"],
                            "p_survive": straight["p_survive"],
                            "length_m": straight["length_m"]},
               "delta_exposure_s": round(straight["exposure_s"] - planned["exposure_s"], 1),
               "delta_length_m": round(planned["length_m"] - straight["length_m"], 1),
               "legs": planned["legs"], "threat_basis": basis,
               "threats_considered": comp["threats"], "proposed": bool(proposed),
               "caveats": caveats, "path": path + [list(p) for p in extra_points or ()],
               "created_ms": _now_ms(), "simulated": True}
        s.vectors[vid] = vec
        return vec

    @staticmethod
    def _corridor_brief(vec: dict | None) -> dict | None:
        if not vec:
            return None
        return {"id": f"vec:{vec['id']}", "exposure_s": vec["exposure_s"],
                "p_survive": vec["p_survive"], "delta_exposure_s": vec["delta_exposure_s"],
                "delta_length_m": vec["delta_length_m"], "length_m": vec["length_m"],
                "threat_basis": vec["threat_basis"], "caveats": list(vec["caveats"])}

    def preview(self, pending_id: str) -> dict:
        """The `engagement` preview the console's slip shows (§5.2.7). Every gate is
        re-run against the current state; a failing one is `ok: false`. `{}` for
        an unknown id (the slip is then Deny-only)."""
        with self._lock:
            s = self._session
            p = s.pendings.get(_strip(pending_id, "eng:")) if s is not None else None
            if p is None:
                return {}
            e = self._eng(s, p.pending_id)
            shooter = s.units.get(p.shooter_id)
            track = self.srv.tracks.get(p.target_track_id)
            try:
                unit, _ = self._gate(s, p.target_track_id)
                scenario = True
            except WargameRefused:
                unit, scenario = None, False
            points = [] if track is None else [(float(track.lat), float(track.lon))]
            if unit is not None:
                points.insert(0, (unit.lat, unit.lon))
            codes = {site_gate(s.exclusion, s.ao.get("pois") or (), la, lo) for la, lo in points}
            in_rng = (shooter is not None and track is not None
                      and self._in_range(shooter, float(track.lat), float(track.lon))[0])
            waiting = p.state == "proposed" and _now_ms() <= p.expires_at_ms
            checks = [
                {"text": "Target is a simulated scenario unit", "ok": scenario},
                {"text": "More than 500 m from any mapped place or theater point",
                 "ok": bool(points) and "target_near_real_site" not in codes},
                {"text": "Not within 1 km of a protected place",
                 "ok": bool(points) and "target_protected" not in codes},
                {"text": "Shooter active with ammunition", "ok": self._shooter_ok(shooter)},
                {"text": "In range", "ok": bool(in_rng)},
                {"text": "Engagement still waiting for approval", "ok": waiting},
                {"text": "Wargame session active", "ok": not self._ending}]
            vec = s.vectors.get(p.vector_id or "")
            protected = bool(points) and s.exclusion is not None and any(
                s.exclusion.protected_within(la, lo) for la, lo in points)
            target = {"track_id": p.target_track_id, "graph_id": f"trk:{p.target_track_id}",
                      "label": e.target_label if e else "Contact",
                      "perceived_class": getattr(track, "ob_class", None),
                      "confidence": self._confidence(track) if track is not None else None,
                      "sightings": getattr(track, "sightings", None),
                      "last_seen_ms": (int(float(track.last_seen) * 1000)
                                       if track is not None else None),
                      "lat": getattr(track, "lat", None), "lon": getattr(track, "lon", None),
                      "scenario": scenario, "protected": protected}
            cls = CLASSES[shooter.wg_class] if shooter else None
            return {"id": p.pending_id, "kind": "blue_strike",
                    "verb_kind": "strike" if cls and cls.key == "blue_strike_air"
                    else "engagement",
                    "attacker": {"id": f"frc:{p.shooter_id}",
                                 "label": shooter.designator if shooter else p.shooter_id,
                                 "wg_class": shooter.wg_class if shooter else None},
                    "target": target, "vector": self._corridor_brief(vec),
                    "p_notional": dict(p.p_estimate or {}), "inputs": list(e.inputs if e else []),
                    "range_m": p.range_m, "seed": s.seed, "engine": ENGINE,
                    "caveats": [NOTIONAL_NOTE] + list((vec or {}).get("caveats") or []),
                    "expires_at_ms": p.expires_at_ms, "checks": checks, "simulated": True,
                    "note": NOTIONAL_NOTE}

    def authorize(self, pending_id: str, approval_id: str, *, chat_session: str,
                  args: Any) -> None:
        """Called ONLY by the chat service after a console approval (§3.8 step 4).
        Re-runs checks 2 and 4-6; raises `WargameRefused` when anything fails."""
        now = _now_ms()
        with self._lock:
            s = self._active()
            if s is None:
                raise WargameRefused("wargame_inactive")
            p = s.pendings.get(_strip(pending_id, "eng:"))
            if p is None:
                raise WargameRefused("unknown_pending")
            if p.state != "proposed":
                raise WargameRefused("engagement_not_pending")
            if now > p.expires_at_ms:
                self._expire(s, p, "timed out")
                self.revision += 1
                raise WargameRefused("engagement_expired")
            if not _args_equal(p.args, args):
                raise WargameRefused("engagement_args_mismatch")
            if not isinstance(chat_session, str) or not chat_session.strip():
                raise WargameRefused("engagement_requires_console_approval")
            self._recheck(s, p)
            p.state = "authorized"
            p.authorized = {"approval_id": approval_id, "chat_session": chat_session,
                            "at_ms": now}
            e = self._eng(s, p.pending_id)
            if e is not None:
                e.phase, e.approval_id = "authorized", approval_id
                e.authorized_at_ms = now
            self._event(s, "blue_strike_authorized", "blue",
                        f"The operator approved simulated engagement {p.pending_id}.",
                        engagement_id=p.pending_id, approval_id=approval_id)
            self.revision += 1

    def deny(self, pending_id: str) -> None:
        """The operator denied it (or the approval failed): phase `denied`."""
        with self._lock:
            s = self._session
            p = s.pendings.get(_strip(pending_id, "eng:")) if s is not None else None
            if p is None or p.state not in ("proposed", "authorized"):
                return
            p.state = "denied"
            e = self._eng(s, p.pending_id)
            if e is not None:
                e.phase = "denied"
            self._event(s, "blue_strike_denied", "blue",
                        f"Simulated engagement {p.pending_id} was denied.",
                        engagement_id=p.pending_id)
            self.revision += 1

    def _confirm_refused(self, pending_id: str, why: str) -> dict:
        self._audit("engagement_confirm_refused",
                    f"simulated engagement {pending_id} refused: {why}",
                    pending_id=pending_id, reason=why,
                    console_call=CONSOLE_CALL.get() is not None, simulated=True)
        return _refused("engagement_requires_console_approval")

    async def execute(self, pending_id: str, shooter_id: str, target_track_id: str) -> dict:
        """Roll the one simulated outcome of an AUTHORIZED engagement (§5.2.7).
        Needs `CONSOLE_CALL == the authorizing chat session`; one-shot."""
        caller = CONSOLE_CALL.get()
        now = _now_ms()
        args = _exec_args(pending_id, shooter_id, target_track_id)
        with self._lock:
            s = self._active()
            if s is None:
                return _refused("wargame_inactive")
            p = s.pendings.get(args["pending_id"])
            if p is None:
                return self._confirm_refused(args["pending_id"], "no such engagement")
            auth = p.authorized or {}
            same = caller is not None and caller == auth.get("chat_session")
            if p.state == "consumed" and p.result is not None and same and p.args == args:
                return dict(p.result)                       # idempotent replay: no new draw
            why = ("not authorized" if p.state != "authorized" else
                   "not from the approving console session" if not same else
                   "expired" if now > p.expires_at_ms else
                   "arguments differ" if p.args != args else None)
            if why is not None:
                return self._confirm_refused(p.pending_id, why)
            try:
                shooter, unit, track, _ = self._recheck(s, p)
            except WargameRefused as exc:
                self._expire(s, p, exc.code)
                self.revision += 1
                return exc.as_result()
            p.state = "consumed"
            shooter.ammo = max(0, shooter.ammo - 1)
            shooter.last_fired_ms = now
            e = self._eng(s, p.pending_id)
            e.fired_at_ms = now
            package = CLASSES[shooter.wg_class].key == "blue_strike_air"
            vec = s.vectors.get(p.vector_id or "")
            job = None
            if package and vec is not None:
                job = (s.lattice, self._truth_threats(s), [tuple(q) for q in vec["path"]],
                       float(vec["alt_agl_m"]), float(vec["speed_mps"]))
        truth_survive = None
        if job is not None:
            try:
                out = await asyncio.to_thread(_evaluate_path, *job, self._environment())
                truth_survive = float(out["p_survive"])
            except Exception as exc:  # noqa: BLE001 — the package then flies unhindered
                self._audit("wargame_package_eval_failed", f"{type(exc).__name__}: {exc}",
                            session_id=s.session_id, pending_id=p.pending_id)
        return await self._adjudicate(s, p, e, shooter, unit, track, truth_survive, vec)

    async def _adjudicate(self, s: Session, p: Pending, e: Engagement, shooter: Unit,
                          unit: Unit, track: Any, truth_survive: float | None,
                          vec: dict | None) -> dict:
        """Steps 4-7 of `execute` (§5.2.7): the umpire uses the TRUE class and
        position; `rng_blue` only."""
        halt = None
        with self._lock:
            now = _now_ms()
            sim_now = self._sim_now() or 0.0
            cls = CLASSES[shooter.wg_class]
            inputs, outcome, draw, consequence, lost = list(e.inputs), "missed", None, "none", False
            if truth_survive is not None:
                u = s.rng_blue.random()
                draw = s.rng_blue.draw
                inputs.append(f"Package survival on the way in about {truth_survive:.2f} "
                              "(umpire)")
                if u >= truth_survive:
                    lost, consequence = True, "own_loss"
                    shooter.state, shooter.ammo, shooter.state_until_s = "destroyed", 0, None
                    self._event(s, "package_lost", "blue",
                                f"{shooter.designator} was lost on the way in (simulated).",
                                outcome="destroyed", engagement_id=e.engagement_id)
            if not lost:
                self._truth(s, unit, self._sim())
                gap = haversine_m(float(track.lat), float(track.lon), unit.lat, unit.lon)
                if gap > TRACK_TRUTH_MAX_M:
                    inputs.append("Target not at the reported location")
                else:
                    rng_m = haversine_m(shooter.lat, shooter.lon, unit.lat, unit.lon)
                    probs = strike_probabilities(cls, CLASSES[unit.wg_class].hardness, rng_m,
                                                 p.level)
                    drawn, _ = draw_outcome(s.rng_blue, probs)
                    draw = s.rng_blue.draw
                    state = apply_outcome(unit, drawn, sim_now)
                    outcome = "destroyed" if drawn != "missed" and state == "destroyed" else drawn
                    consequence = "red_effect" if outcome != "missed" else "none"
                    if state == "destroyed" and unit.object_name and unit.route_to:
                        halt = (unit.object_name, unit.lat, unit.lon)
                        unit.route_to = None
            e.phase, e.outcome, e.consequence, e.draw = "adjudicated", outcome, consequence, draw
            e.adjudicated_at_ms, e.inputs = now, inputs
            if vec is not None:
                vec["proposed"] = False
            self._event(
                s, "blue_strike_executed", "blue",
                f"{shooter.designator} struck {unit.designator}: {outcome} (simulated).",
                outcome=outcome, blue_outcome_hidden=True,
                blue_text=(f"{shooter.designator} fired a simulated strike on "
                           f"{e.target_label}; the outcome is unknown until a re-look."),
                engagement_id=e.engagement_id, draw=draw)
            result = {"executed": True, "engagement_id": e.engagement_id,
                      "fired_at_ms": e.fired_at_ms,
                      "outcome": outcome if s.reveal_red else None,
                      "outcome_note": OUTCOME_NOTE, "package_lost": lost,
                      "simulated": True, "note": NOTIONAL_NOTE}
            p.result = dict(result)
            self.revision += 1
        if halt is not None:
            with contextlib.suppress(Exception):
                await self.srv._move_target(halt[0], [{"lat": halt[1], "lon": halt[2]}],
                                            0.0, False)
        return result

    # -------------------------------------------------------- corridor --
    async def _vehicle_known(self, vehicle: str) -> bool:
        srv = self.srv
        if vehicle in (getattr(srv, "_last_tele", {}) or {}) or \
                vehicle in (getattr(srv, "monitors", {}) or {}):
            return True
        try:
            return vehicle in set(await srv.backend.list_vehicles())
        except Exception:  # noqa: BLE001 — an unreadable roster knows nobody
            return False

    async def plan_corridor(self, vehicle: str, target_track_id: str, *,
                            alt_agl_m: float = 60.0, relook: bool = False,
                            relook_radius_m: float = 400.0) -> dict:
        """A least-exposure corridor from `vehicle` to a gated scenario contact
        (§5.2.8), dry-run-ready as `recon_args`. Drones only look (C11)."""
        try:
            alt, radius = float(alt_agl_m), float(relook_radius_m)
        except (TypeError, ValueError):
            return _refused("invalid_parameter", "alt_agl_m and relook_radius_m are numbers.")
        if not (math.isfinite(alt) and 0.0 < alt <= 5000.0):
            return _refused("invalid_parameter", "alt_agl_m must be above 0 m.")
        if not RELOOK_MIN_M <= radius <= RELOOK_MAX_M:
            return _refused("invalid_parameter", "relook_radius_m must be 150 to 1500 m.")
        vehicle = str(vehicle)
        known = await self._vehicle_known(vehicle)
        srv = self.srv
        with self._lock:
            s = self._active()
            if s is None:
                return _refused("wargame_inactive")
            if not known:
                return _refused("vehicle_unknown")
            if vehicle in s.lost or vehicle in (getattr(srv, "vehicles_lost", {}) or {}):
                return _refused("vehicle_lost")
            try:
                _, track = self._gate(s, target_track_id)
                self._site_check(s, [(float(track.lat), float(track.lon))])
            except WargameRefused as exc:
                return exc.as_result()
            t = (getattr(srv, "_last_tele", {}) or {}).get(vehicle)
            start = ((float(t["lat"]), float(t["lon"])) if isinstance(t, dict) and "lat" in t
                     else tuple(s.ao["home"]))
            goal = (float(track.lat), float(track.lon))
            threats, basis = self._threat_inputs(s, start, goal)
            fence = [tuple(q) for q in srv.envelope.geofence]
            lattice, excl, pois = s.lattice, s.exclusion, tuple(s.ao.get("pois") or ())
            label = label_for_ob(track.ob_class)
        try:
            comp = await asyncio.to_thread(_compute_corridor, lattice, threats, start, goal,
                                           fence, alt, DEFAULT_DRONE_SPEED_MPS,
                                           self._environment())
        except NoRoute as exc:
            return {**exc.as_result(), "simulated": True}
        ring = []
        if relook:
            ring = ring_points(goal[0], goal[1], radius, geofence=fence,
                               blocked=lambda la, lo: site_gate(excl, pois, la, lo) is not None)
        with self._lock:
            if self._active() is not s:
                return _refused("wargame_inactive")
            vec = self._store_corridor(s, comp, frm=f"veh:{vehicle}", frm_label=vehicle,
                                       to=f"trk:{track.track_id}", to_label=label, alt=alt,
                                       speed=DEFAULT_DRONE_SPEED_MPS, basis=basis,
                                       proposed=True, extra_points=ring)
            self.revision += 1
        waypoints = [{"lat": round(q[0], 7), "lon": round(q[1], 7)} for q in vec["path"]]
        return {"waypoints": waypoints, "alt_agl_m": alt, "length_m": vec["length_m"],
                "eta_s": vec["eta_s"], "exposure_s": vec["exposure_s"],
                "p_survive": vec["p_survive"], "straight": dict(vec["straight"]),
                "delta_exposure_s": vec["delta_exposure_s"],
                "delta_length_m": vec["delta_length_m"], "legs": vec["legs"],
                "threat_basis": basis, "threats_considered": vec["threats_considered"],
                "terrain_masking": {"source": lattice.terrain_source,
                                    "coverage_pct": lattice.coverage_pct},
                "relook_points": len(ring), "caveats": list(vec["caveats"]),
                "recon_args": {"vehicle": vehicle, "waypoints": waypoints, "alt_agl_m": alt,
                               "dry_run": True},
                "vector_id": f"vec:{vec['id']}", "target": {"track_id": track.track_id,
                                                           "label": label},
                "simulated": True, "note": NOTIONAL_NOTE}

    # ----------------------------------------------------------- views --
    def _correlated(self, s: Session, u: Unit) -> list[str]:
        return [f"trk:{t.track_id}" for t in self._gated_tracks(s, u)]

    def _unit_row(self, s: Session, u: Unit, *, truth: bool) -> dict:
        """A force row for tool answers (§3.2 force attrs). Caller holds the lock."""
        cls = CLASSES[u.wg_class]
        geo = {k: s.ao[k] for k in ("half_extent_m", "half_diagonal_m")}
        caveats = list(class_caveats(cls, geo))
        if u.side == "red" and cls.role in ("air", "sensor") and u.horizon is None:
            caveats.append(MASKING_PENDING_CAVEAT)
        row = {"unit_id": u.unit_id, "id": f"frc:{u.unit_id}", "designator": u.designator,
               "side": u.side, "wg_class": u.wg_class, "kind_label": cls.label,
               "ob_class": cls.ob_key, "state": u.state, "damaged": u.damaged,
               "ammo": u.ammo, "lat": round(u.lat, 7), "lon": round(u.lon, 7),
               "alt_msl_m": round(u.alt_msl_m, 1),
               "position": relative_position(s.ao["center"], u.lat, u.lon),
               "mobile": cls.mobile, "objective": f"frc:{u.objective}" if u.objective else None,
               "threat_range_m": cls.threat_range_m or None,
               "threat_ceiling_m": cls.threat_ceiling_m or None,
               "detection_range_m": cls.detection_range_m or None,
               "strike_range_m": (None if cls.side == "red" or cls.role != "shooter"
                                  else cls.strike_range_m),
               "provenance": "scenario", "register": "scenario", "caveats": caveats,
               "simulated": True}
        if truth and u.side == "red":
            row["correlated"] = self._correlated(s, u)
        return row

    def _seen(self, s: Session) -> int:
        """Red units with at least one gated track (what blue has sensed)."""
        return sum(1 for u in self._units_sorted(s, "red") if self._gated_tracks(s, u))

    def _counts(self, s: Session, show_red: bool) -> dict:
        c = aar.count_units(self._unit_row(s, u, truth=False) for u in s.units.values())
        return {"blue": c["blue"],
                "red": {**c["red"], "seen": self._seen(s)} if show_red
                else {"seen": self._seen(s)}}

    def _engagement_row(self, s: Session, e: Engagement, show_red: bool) -> dict:
        """§3.2 engagement attrs, fog-filtered for blue view."""
        red_attacker = e.kind in ("red_shot", "red_ground")
        hide_attacker = red_attacker and not show_red
        hide_outcome = (e.kind == "blue_strike" and not show_red
                        and not bda.outcome_visible(e.bda))
        masked = RED_AD_HIDDEN if e.kind == "red_shot" else RED_GROUND_HIDDEN
        return {"id": f"eng:{e.engagement_id}", "engagement_id": e.engagement_id,
                "kind": e.kind, "phase": e.phase,
                "attacker": None if hide_attacker else e.attacker,
                "attacker_label": masked if hide_attacker else e.attacker_label,
                "target": e.target, "target_label": e.target_label, "vector": e.vector,
                "p_notional": None if hide_attacker else (dict(e.p_notional)
                                                          if e.p_notional else None),
                "inputs": [] if hide_attacker else list(e.inputs),
                "outcome": None if hide_outcome else e.outcome, "outcome_hidden": hide_outcome,
                "consequence": e.consequence,
                "bda": dict(e.bda) if e.kind == "blue_strike" and e.bda else None,
                "approval_id": e.approval_id, "seed": e.seed, "draw": e.draw, "engine": ENGINE,
                "proposed_at_ms": e.proposed_at_ms, "adjudicated_at_ms": e.adjudicated_at_ms,
                "fired_at_ms": e.fired_at_ms, "authorized_at_ms": e.authorized_at_ms,
                "simulated": True}

    def status(self, *, truth: bool = False, events: int = 20) -> dict:
        """Session status for `wg_session_status`, fog-filtered unless `truth` or
        the session reveals red (§5.2.4)."""
        try:
            n = max(0, min(100, int(events)))
        except (TypeError, ValueError):
            n = 20
        with self._lock:
            s = self._session
            if s is None:
                return {"active": False, "starting": self.starting, "last": self.last,
                        "simulated": True, "note": NOTIONAL_NOTE}
            show_red = bool(truth) or s.reveal_red
            units = [self._unit_row(s, u, truth=show_red) for u in self._units_sorted(s)
                     if u.side == "blue" or show_red]
            evs = [v for v in (self._event_view(e, show_red) for e in s.events) if v]
            engs = [self._engagement_row(s, e, show_red)
                    for e in s.engagements[-GRAPH_ENGAGEMENTS:]]
            pending = [{"pending_id": p.pending_id, "shooter_id": p.shooter_id,
                        "target_track_id": p.target_track_id, "state": p.state,
                        "expires_at_ms": p.expires_at_ms, "p_estimate": dict(p.p_estimate or {})}
                       for p in s.pendings.values() if p.state in ("proposed", "authorized")]
            caveats = sorted({*s.caveats, *(c for r in units for c in r["caveats"])})
            return {"active": True, "session_id": s.session_id,
                    "started_at_ms": s.started_at_ms, "seed": s.seed, "engine": ENGINE,
                    "table_version": TABLE_VERSION, "time_scale": self._time_scale(),
                    "red_engages": s.red_engages, "reveal_red": s.reveal_red,
                    "truth_view": show_red, "theater_id": s.theater_id,
                    "ao": {k: s.ao[k] for k in ("half_extent_m", "half_diagonal_m")},
                    "counts": self._counts(s, show_red), "units": units, "pending": pending,
                    "engagements": engs,
                    "vehicles_lost": [{"vehicle": v, "at_ms": r.get("at_ms"),
                                       "by": r.get("by") if show_red else RED_AD_HIDDEN}
                                      for v, r in sorted(s.lost.items())],
                    "events": evs[-n:] if n else [], "caveats": caveats,
                    "revision": self.revision, "step_ms": self.step_ms, "errors": self.errors,
                    "simulated": True, "note": NOTIONAL_NOTE}

    def list_forces(self, *, side: str | None = None, truth: bool = False) -> dict:
        """Scenario forces, fog-filtered (red only in truth view or when revealed)."""
        with self._lock:
            s = self._session
            if s is None:
                return _refused("wargame_inactive")
            show_red = bool(truth) or s.reveal_red
            rows = [self._unit_row(s, u, truth=show_red) for u in self._units_sorted(s, side)
                    if u.side == "blue" or show_red]
            return {"forces": rows, "counts": self._counts(s, show_red), "truth_view": show_red,
                    "simulated": True, "note": NOTIONAL_NOTE}

    # ----------------------------------------------------- graph rows --
    def _airborne(self, s: Session) -> dict[str, tuple]:
        und = float(getattr(s.lattice, "undulation_m", 0.0) or 0.0)
        out = {}
        for v, t in sorted((getattr(self.srv, "_last_tele", {}) or {}).items()):
            if isinstance(t, dict) and int(t.get("landed_state") or 0) != 0 and v not in s.lost:
                p = self._drone(t, und)
                if p is not None:
                    out[v] = p
        return out

    def _inside(self, u: Unit, drone: tuple, *, detection: bool) -> bool:
        """Geometric: the drone is inside the unit's threat (or detection) envelope."""
        cls = CLASSES[u.wg_class]
        lat, lon, hae, msl, _ = drone
        dz = hae - u.alt_hae_m
        slant = math.hypot(haversine_m(u.lat, u.lon, lat, lon), dz)
        if detection:
            return cls.detection_range_m > 0 and slant <= cls.detection_range_m
        return (cls.threat_range_m > 0 and 0.0 <= dz <= cls.threat_ceiling_m
                and slant <= threat_range(cls, u.damaged) and visible(u.horizon, lat, lon, msl))

    def _force_status(self, s: Session, u: Unit, drones: dict, now_ms: int) -> str:
        """§3.2 force status (a force is never `ok` because it is green: D8)."""
        if u.side == "blue":
            return {"active": "ok", "destroyed": "critical"}.get(u.state, "warn")
        if u.state == "destroyed":
            return "stale"
        if any(self._inside(u, d, detection=False) for d in drones.values()) or (
                u.last_fired_ms is not None and now_ms - u.last_fired_ms <= RED_FIRE_WINDOW_MS):
            return "critical"
        det = CLASSES[u.wg_class].detection_range_m
        if any(self._inside(u, d, detection=True) for d in drones.values()) or any(
                b.state != "destroyed" and haversine_m(u.lat, u.lon, b.lat, b.lon) <= det
                for b in self._units_sorted(s, "blue")):
            return "warn"
        return "ok"

    @staticmethod
    def _group(u: Unit) -> str:
        if u.side == "blue":
            return BLUE_GROUP.get(u.wg_class, "unclassified")
        ob = OB_LIBRARY.get(CLASSES[u.wg_class].ob_key or "")
        return CATEGORY_GROUP.get(ob.category, "unclassified") if ob else "unclassified"

    def _until_ms(self, u: Unit, now_ms: int) -> int | None:
        if u.state_until_s is None:
            return None
        sim_now = self._sim_now()
        if sim_now is None:
            return None
        return int(now_ms + max(0.0, u.state_until_s - sim_now) / self._time_scale() * 1000.0)

    def _force_node(self, s: Session, u: Unit, status: str, truth: bool, now_ms: int) -> dict:
        row = self._unit_row(s, u, truth=truth)
        attrs = {k: row[k] for k in ("side", "provenance", "register", "wg_class", "ob_class",
                                     "kind_label", "state", "ammo", "threat_range_m",
                                     "threat_ceiling_m", "detection_range_m", "strike_range_m",
                                     "mobile", "objective", "caveats", "simulated")}
        attrs["state_until_ms"] = self._until_ms(u, now_ms)
        if truth and u.side == "red":
            attrs["correlated"] = row["correlated"]
        side = "Red" if u.side == "red" else "Blue"
        return {"id": row["id"], "type": "force", "label": u.designator,
                "subtitle": (f"{side}  {row['kind_label']}  {STATE_WORD.get(u.state, u.state)}"
                             "  Scenario"),
                "group": self._group(u), "salience": 0.6 if u.side == "red" else 0.5,
                "status": status, "ts_ms": int(u.spawned_at * 1000), "lat": round(u.lat, 6),
                "lon": round(u.lon, 6), "attrs": attrs}

    def _target_group(self, s: Session, e: Engagement) -> str:
        if e.kind == "red_shot":
            return "fleet"
        if e.kind == "red_ground":
            b = s.units.get(_strip(e.target, "frc:"))
            return self._group(b) if b else "unclassified"
        t = self.srv.tracks.get(e.track_id or "")
        ob = OB_LIBRARY.get(getattr(t, "ob_class", "") or "")
        return CATEGORY_GROUP.get(ob.category, "unclassified") if ob else "unclassified"

    def _engagement_node(self, s: Session, e: Engagement, show_red: bool) -> dict:
        row = self._engagement_row(s, e, show_red)
        pending = e.phase in ("proposed", "authorized")
        status = ("critical" if e.consequence == "own_loss" else
                  "warn" if e.consequence == "own_damage" or pending else "ok")
        attrs = {k: v for k, v in row.items() if k not in ("id", "engagement_id")}
        pt = e.target_point or (None, None)
        return {"id": row["id"], "type": "engagement",
                "label": f"Simulated {KIND_WORD.get(e.kind, 'engagement')} on {e.target_label}",
                "subtitle": f"{PHASE_WORD.get(e.phase, e.phase)}  Simulated",
                "group": self._target_group(s, e), "salience": 0.8 if pending else 0.5,
                "status": status, "ts_ms": e.adjudicated_at_ms or e.proposed_at_ms,
                "lat": pt[0], "lon": pt[1], "attrs": attrs}

    def _vector_node(self, s: Session, v: dict, status: str, group: str) -> dict:
        axis = v["kind"] == "axis"
        if axis:
            u = s.units.get(v["unit_id"])
            o = s.units.get(v.get("objective_id") or "")
            frm, to = f"frc:{v['unit_id']}", f"frc:{v['objective_id']}"
            label = (f"Red axis from {u.designator if u else v['unit_id']} to "
                     f"{o.designator if o else v.get('objective_id')}")
        else:
            frm, to = v["from"], v["to"]
            label = f"Planned corridor from {v['from_label']} to {v['to_label']}"
        straight = v.get("straight")
        attrs = {"kind": v["kind"], "side": v["side"], "from": frm, "to": to,
                 "to_point": list(v.get("to_point") or v.get("to")),
                 "bearing_deg": v.get("bearing_deg"), "length_m": v.get("length_m"),
                 "alt_band": v.get("alt_band"), "corridor_m": v.get("corridor_m"),
                 "speed_mps": v.get("speed_mps"), "eta_s": v.get("eta_s"),
                 "exposure_s": v.get("exposure_s"), "p_survive": v.get("p_survive"),
                 "straight": ({"exposure_s": straight["exposure_s"],
                               "p_survive": straight["p_survive"]} if straight else None),
                 "delta_exposure_s": v.get("delta_exposure_s"),
                 "delta_length_m": v.get("delta_length_m"),
                 "legs": [{"exposure": g["exposure"], "exposure_s": g["exposure_s"],
                           "length_m": g["length_m"]} for g in v.get("legs") or ()],
                 "threat_basis": v.get("threat_basis") or ("truth" if axis else None),
                 "proposed": bool(v.get("proposed")), "caveats": list(v.get("caveats") or ()),
                 "simulated": True}
        a = v.get("from_point") or v.get("from")
        return {"id": f"vec:{v['id']}", "type": "vector", "label": label,
                "subtitle": ("Red axis" if axis else "Planned corridor") + "  Simulated",
                "group": group, "salience": 0.4, "status": status,
                "ts_ms": v.get("created_ms"), "lat": round(a[0], 6), "lon": round(a[1], 6),
                "attrs": attrs}

    def _vectors_for(self, s: Session, show_red: bool, cap_axes: int,
                     cap_corridors: int) -> list[dict]:
        axes = [v for v in s.vectors.values() if v["kind"] == "axis"] if show_red else []
        cors = [v for v in s.vectors.values() if v["kind"] == "corridor"]
        def key(v: dict) -> int:
            return int(v.get("created_ms") or 0)
        return sorted(axes, key=key)[-cap_axes:] + sorted(cors, key=key)[-cap_corridors:]

    def graph_rows(self, *, truth: bool) -> dict:
        """Wargame nodes, edges and `meta.wargame` for the intel graph (§3.2).
        Red nodes only in truth view or when revealed; truth-only edges only in
        truth view. Nothing here derives from real data (D1)."""
        with self._lock:
            s = self._session
            if s is None:
                return {"nodes": [], "edges": [],
                        "meta": {"wargame": {"active": False, "last": self.last}}}
            show_red = bool(truth) or s.reveal_red
            now = _now_ms()
            drones = self._airborne(s)
            nodes, edges, status = [], [], {}
            for u in [u for u in self._units_sorted(s)
                      if u.side == "blue" or show_red][:GRAPH_FORCES]:
                status[u.unit_id] = self._force_status(s, u, drones, now)
                nodes.append(self._force_node(s, u, status[u.unit_id], bool(truth), now))
            vecs = self._vectors_for(s, show_red, GRAPH_VECTORS, GRAPH_VECTORS)
            vecs = sorted(vecs, key=lambda v: v.get("created_ms") or 0)[-GRAPH_VECTORS:]
            for v in vecs:
                if v["kind"] == "axis":
                    u = s.units.get(v["unit_id"])
                    node = self._vector_node(s, v, status.get(v["unit_id"], "ok"),
                                             self._group(u) if u else "unclassified")
                    if truth:
                        edges.append({"a": node["attrs"]["from"], "b": node["attrs"]["to"],
                                      "kind": "axis"})
                else:
                    frm_unit = s.units.get(_strip(v["from"], "frc:"))
                    group = self._group(frm_unit) if frm_unit else "fleet"
                    node = self._vector_node(s, v, "ok", group)
                    edges.append({"a": v["from"], "b": v["to"], "kind": "ingress"})
                nodes.append(node)
            shown = {n["id"] for n in nodes}
            for e in s.engagements[-GRAPH_ENGAGEMENTS:]:
                node = self._engagement_node(s, e, show_red)
                nodes.append(node)
                edges.append({"a": node["id"], "b": e.target, "kind": "attacks"})
                if node["attrs"]["attacker"]:
                    edges.append({"a": node["id"], "b": e.attacker, "kind": "launched_by"})
                if e.vector and e.vector in shown:
                    edges.append({"a": node["id"], "b": e.vector, "kind": "along"})
            if truth:
                edges += self._truth_edges(s, drones)
            return {"nodes": nodes, "edges": edges,
                    "meta": {"wargame": self._meta(s, show_red), "caveats": [SESSION_CAVEAT]}}

    def _truth_edges(self, s: Session, drones: dict) -> list[dict]:
        out = []
        blues = [b for b in self._units_sorted(s, "blue") if b.state != "destroyed"]
        for u in self._units_sorted(s, "red"):
            if u.state == "destroyed":
                continue
            cls = CLASSES[u.wg_class]
            for v, d in drones.items():
                if self._inside(u, d, detection=False):
                    out.append({"a": f"frc:{u.unit_id}", "b": f"veh:{v}", "kind": "threatens"})
            reach = cls.ground_range_m * (DAMAGED_RANGE_FACTOR if u.damaged else 1.0)
            for b in blues:
                if reach > 0 and haversine_m(u.lat, u.lon, b.lat, b.lon) <= reach:
                    out.append({"a": f"frc:{u.unit_id}", "b": f"frc:{b.unit_id}",
                                "kind": "threatens"})
            for tid in self._correlated(s, u):
                out.append({"a": tid, "b": f"frc:{u.unit_id}", "kind": "correlates"})
        return out

    def _meta(self, s: Session, show_red: bool) -> dict:
        """`graph.meta.wargame` while a session runs (§3.2)."""
        return {"active": True, "session_id": s.session_id, "started_at_ms": s.started_at_ms,
                "seed": s.seed, "engine": ENGINE, "time_scale": self._time_scale(),
                "red_engages": s.red_engages, "reveal_red": s.reveal_red,
                "truth_view": show_red, "revision": self.revision,
                "pending": [f"eng:{p.pending_id}" for p in s.pendings.values()
                            if p.state in ("proposed", "authorized")],
                "counts": self._counts(s, show_red), "caveats": list(s.caveats),
                "step_ms": self.step_ms, "errors": self.errors, "simulated": True}

    # --------------------------------------------------------- overlay --
    @staticmethod
    def _feature(fid: str, geometry: dict, props: dict) -> dict:
        return {"type": "Feature", "id": fid, "geometry": geometry, "properties": props}

    @staticmethod
    def _pt(lat: float, lon: float) -> dict:
        return {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}

    @staticmethod
    def _ring(points: list) -> dict:
        ring = [[round(p[1], 6), round(p[0], 6)] for p in points]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        return {"type": "Polygon", "coordinates": [ring]}

    def overlay_features(self, *, truth: bool) -> list[dict]:
        """GeoJSON features for `/intel/overlay` (§3.3): `force`, `force_envelope`
        (terrain-masked fan, else a 48-point circle; red air defence and radar
        only), `vector` and `engagement`. Red only in truth view or revealed."""
        with self._lock:
            s = self._session
            if s is None:
                return []
            show_red = bool(truth) or s.reveal_red
            now = _now_ms()
            drones = self._airborne(s)
            base = {"register": "scenario", "simulated": True, "truth": bool(truth)}
            feats, envelopes = [], []
            for u in self._units_sorted(s):
                if u.side == "red" and not show_red:
                    continue
                st = self._force_status(s, u, drones, now)
                fid = f"frc:{u.unit_id}"
                feats.append(self._feature(fid, self._pt(u.lat, u.lon), {
                    **base, "kind": "force", "id": fid, "label": u.designator, "side": u.side,
                    "wg_class": u.wg_class, "state": u.state, "status": st}))
                cls = CLASSES[u.wg_class]
                if u.side != "red" or u.state == "destroyed" or cls.role not in ("air", "sensor"):
                    continue
                for ring, radius in (("threat", threat_range(cls, u.damaged)),
                                     ("detection", cls.detection_range_m)):
                    if radius <= 0:
                        continue
                    pts = (u.fans or {}).get(ring) if not u.damaged or ring != "threat" \
                        else None
                    pts = pts or _circle(u.lat, u.lon, radius)
                    eid = f"env:{u.unit_id}:{ring}"
                    envelopes.append(self._feature(eid, self._ring(pts), {
                        **base, "kind": "force_envelope", "id": eid, "label": u.designator,
                        "side": "red", "force": fid, "ring": ring, "radius_m": radius,
                        "status": st}))
            feats += envelopes[:OVERLAY_ENVELOPES]
            for v in self._vectors_for(s, show_red, OVERLAY_AXES, OVERLAY_CORRIDORS):
                pts = v.get("path") or [v["from"], v["to"]]
                fid = f"vec:{v['id']}"
                axis = v["kind"] == "axis"
                st = self._force_status(s, s.units[v["unit_id"]], drones, now) \
                    if axis and v["unit_id"] in s.units else "ok"
                feats.append(self._feature(fid, {"type": "LineString", "coordinates": [
                    [round(p[1], 6), round(p[0], 6)] for p in pts]}, {
                    **base, "kind": "vector", "id": fid, "side": v["side"],
                    "label": "Red axis" if axis else "Planned corridor",
                    "kind_detail": v["kind"], "status": st, "alt_band": v.get("alt_band"),
                    "corridor_m": v.get("corridor_m"),
                    "legs": [{"exposure": g["exposure"], "exposure_s": g["exposure_s"],
                              "length_m": g["length_m"]} for g in v.get("legs") or ()],
                    "exposure_s": v.get("exposure_s"), "p_survive": v.get("p_survive"),
                    "proposed": bool(v.get("proposed"))}))
            for e in s.engagements[-OVERLAY_ENGAGEMENTS:]:
                if e.target_point is None:
                    continue
                row = self._engagement_row(s, e, show_red)
                fid = row["id"]
                frm = (None if row["attacker"] is None or e.attacker_point is None
                       else [round(e.attacker_point[1], 6), round(e.attacker_point[0], 6)])
                feats.append(self._feature(fid, self._pt(*e.target_point), {
                    **base, "kind": "engagement", "id": fid,
                    "label": f"Simulated {KIND_WORD.get(e.kind, 'engagement')} on "
                             f"{e.target_label}",
                    "side": "blue" if e.kind == "blue_strike" else "red",
                    "phase": e.phase, "kind_detail": e.kind, "outcome": row["outcome"],
                    "consequence": e.consequence,
                    "p_notional": (row["p_notional"] or {}).get("effect"),
                    "bda_state": (row["bda"] or {}).get("state"), "from": frm}))
            return feats
