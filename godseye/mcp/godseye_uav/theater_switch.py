"""Runtime theater switch: every copy of the NED origin moved atomically (WG v2 §4.1.3).

A theater is not one value. The simulation origin lives in THREE places — the
fake simulator (`FakeAirSim.home_geo`), the MCP backend (`UavBackend.home_geo`
and friends) and every origin holder the host attached (the bridge's
`AirSimAdapter`) — and the geofence, home, fuel homes, POIs, terrain lattice,
real data and mapped sites all derive from it. The working prototype
(`relocate_proto.py`) measured what a partial move does: a copy left behind
draws the drone 13,000 km away. So `switch()`:

* runs on the tasking loop, with `_switching` set under `_mode_lock` so
  `_submit` refuses and `tick_once` skips for its whole length, and only
  after every in-flight tick has drained (`wait_ticks_idle`);
* refuses unless every drone is PROVEN landed, idle, un-latched and linked,
  on the fake simulator, in the app host, after restart recovery (what the
  caches already disprove is refused before the flag is raised);
* converts the theater's MSL ground to HAE ONCE (T1), moves the three copies
  with no await in between, and cross-checks them against each other AND the
  simulator's own `getHomeGeoPoint`. A mismatch puts every copy back at the
  old origin and sets `srv.theater_integrity_error`, after which every
  `_submit` but land and hover refuses and the monitor enforces nothing;
* once the origin starts to move, runs to its end even when its caller is
  cancelled (Stop, a closed chat): it is never left half-moved;
* moves everything derived from the origin, bumps `theater_epoch`, persists
  `<store>/theater.json` atomically, and only then tells the listeners.

Duck-types `srv`; never imports `server.py` (§0.2).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import safety, theaters
from .geo import GeoidUnavailableError, GeoPoint, canonical_altitude

_LOG = logging.getLogger(__name__)

SCHEMA = "godseye.theater-state/v1"
STATE_FILE = "theater.json"
#: Cross-check tolerances (step 2e): degrees and metres.
ORIGIN_DEG_TOL = 1e-9
ORIGIN_ALT_TOL_M = 1e-3
#: How long the switch waits for an in-flight safety tick.
TICK_DRAIN_S = 5.0

#: What a switch resets and what it keeps (§4.1.3); the slip shows both.
RESETS: tuple[str, ...] = (
    "Vehicle positions (every drone is parked at the new home, landed)",
    "Geofence, home and every fuel model's home",
    "Alarms in progress",
    "Per-tick caches and real data for the old area",
    "Mapped sites (replaced by the new area's)",
)
KEEPS: tuple[str, ...] = (
    "Fuel level and BINGO latch (same airframe)",
    "Contacts and tracks",
    "Reports and pattern of life",
    "Mission history and the audit trail",
    "Lost-link plan and sim speed",
    "Scene objects in the old area",
)

#: Step 0 and 1 refusals, and the `refusals()` table, verbatim.
MSG_ALREADY_SWITCHING = "a theater switch is already running"
MSG_WARGAME = "end the wargame session first"
MSG_RECOVERY = "restart recovery still running"
MSG_TICK = "a safety tick did not finish; try again"
MSG_REAL_AIRSIM = ("real AirSim: the origin is fixed by settings.json; runtime theater "
                   "change is not supported")
MSG_NO_HOST = "runtime theater change needs the app host"
MSG_ROSTER = "vehicle roster unavailable"
MSG_RTB = "a forced RTB is flying"
MSG_GEOID = "the EGM96 geoid is unavailable"


class SwitchRefused(Exception):
    """The switch did not start: `reasons` are one sentence each."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = [str(r) for r in reasons]
        super().__init__("; ".join(self.reasons))

    def as_result(self) -> dict[str, Any]:
        """`{rejected, error: "switch_refused", message, reasons}` (§3.1)."""
        return {"rejected": True, "error": "switch_refused",
                "message": "; ".join(self.reasons), "reasons": list(self.reasons)}


class TheaterIntegrityError(RuntimeError):
    """The origin copies disagree after a move (step 2e)."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _flag(value: Any) -> bool:
    """A bool, an Event (`is_set`) or a zero-argument callable, read as a bool."""
    is_set = getattr(value, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if callable(value):
        try:
            return bool(value())
        except Exception:  # noqa: BLE001 — unreadable counts as busy (safe side)
            return True
    return bool(value)


def wargame_busy(srv: Any) -> bool:
    """True while a wargame session is starting or active (Phase B, B5).
    Phase A servers have no `srv.wargame`, which reads as not busy."""
    wg = getattr(srv, "wargame", None)
    if wg is None:
        return False
    return any(_flag(getattr(wg, name, False)) for name in ("starting", "active"))


def _link_down(mon: Any) -> bool:
    """The monitor's link is pending or declared lost (M9)."""
    state = getattr(getattr(mon, "link", None), "state", None)
    return str(getattr(state, "value", state)) in ("pending", "loal")


def _rtb_flying(srv: Any) -> bool:
    from .tasking import TERMINAL

    return any(task is not None and task.state not in TERMINAL
               for task in dict(getattr(srv, "_rtb_task", {}) or {}).values())


async def refusals(srv: Any, t: theaters.Theater) -> list[str]:
    """Why `t` cannot be activated now: one sentence each; empty = go.

    Everything the move needs PROVEN, not assumed: every drone the simulator
    lists is asked for its telemetry (a drone that cannot answer cannot be
    proven landed), plus the queue, the BINGO latch and the link.
    """
    backend = srv.backend
    if getattr(backend, "sim", None) is None:
        return [MSG_REAL_AIRSIM]
    why: list[str] = []
    if not getattr(srv, "theater_listeners", None):
        why.append(MSG_NO_HOST)
    try:
        names = [str(v) for v in await backend.list_vehicles()]
    except Exception:  # noqa: BLE001 — the roster is unknown, so nothing is proven
        why.append(MSG_ROSTER)
        names = []
    monitors = dict(getattr(srv, "monitors", {}) or {})
    for v in names:
        try:
            tele = await backend.telemetry(v)
            landed = int(tele["landed_state"])
        except Exception:  # noqa: BLE001
            why.append(f"{v}: telemetry unavailable, cannot prove it is landed")
            landed = None
        if landed is not None and landed != 0:
            why.append(f"{v}: airborne (landed_state={landed})")
        act = srv.tasking.queue_for(v).active()
        if act is not None:
            why.append(f"{v}: busy ({act.tool} {getattr(act.state, 'value', act.state)})")
        mon = monitors.get(v)
        if mon is not None and mon.fuel.bingo.tripped:
            why.append(f"{v}: BINGO latched; refuel with sim_set_fuel first")
        link = None
        try:
            link = await backend.link_state(v)
        except Exception:  # noqa: BLE001 — the monitor's view still counts
            link = None
        if (mon is not None and _link_down(mon)) or link == "lost":
            why.append(f"{v}: link lost")
    if _rtb_flying(srv):
        why.append(MSG_RTB)
    why.extend(theaters.validate([t]))
    try:
        canonical_altitude(t.home_alt_msl_m, t.home_lat, t.home_lon, datum="msl")
    except GeoidUnavailableError:
        why.append(MSG_GEOID)
    return why


def cached_refusals(srv: Any) -> list[str]:
    """The refusals the caches already PROVE, read before `_switching` is raised.

    Review A: a refused switch used to raise `_switching` first, so a caller
    retrying `sim_set_theater` while a drone flew kept `tick_once` skipping and
    `_submit` refusing the operator's own land and return-home. Only positive
    evidence refuses here (a drone whose last sample is missing proves nothing
    either way); `refusals()`, under the flag, stays the authoritative gate.
    Same sentences as `refusals()`. No RPC, no await.
    """
    backend = getattr(srv, "backend", None)
    if getattr(backend, "sim", None) is None:
        return [MSG_REAL_AIRSIM]
    why: list[str] = []
    if not getattr(srv, "theater_listeners", None):
        why.append(MSG_NO_HOST)
    done = getattr(srv, "boot_recovery_done", None)
    if done is None or not _flag(done):
        why.append(MSG_RECOVERY)
    monitors = dict(getattr(srv, "monitors", {}) or {})
    last = dict(getattr(srv, "_last_tele", {}) or {})
    for v in sorted(set(monitors) | set(last)):
        tele = last.get(v)
        landed = tele.get("landed_state") if isinstance(tele, Mapping) else None
        try:
            if landed is not None and int(landed) != 0:
                why.append(f"{v}: airborne (landed_state={int(landed)})")
        except (TypeError, ValueError):
            pass                                  # unreadable: the RPC check decides
        try:
            act = srv.tasking.queue_for(v).active()
        except Exception:  # noqa: BLE001 — unknown here; the RPC-side check decides
            act = None
        if act is not None:
            why.append(f"{v}: busy ({act.tool} {getattr(act.state, 'value', act.state)})")
        mon = monitors.get(v)
        if mon is not None and mon.fuel.bingo.tripped:
            why.append(f"{v}: BINGO latched; refuel with sim_set_fuel first")
        if mon is not None and _link_down(mon):
            why.append(f"{v}: link lost")
    if _rtb_flying(srv):
        why.append(MSG_RTB)
    return why


def quick_checks(srv: Any, p: Any) -> list[dict[str, Any]]:
    """The `refusals` list read from caches only (no RPC), plus "Proposal
    still valid": the slip's checks (§3.6). Any `ok: false` blocks the slip."""
    checks: list[dict[str, Any]] = []
    monitors = dict(getattr(srv, "monitors", {}) or {})
    last = dict(getattr(srv, "_last_tele", {}) or {})
    names = sorted(set(monitors) | set(last))
    if not names:
        checks.append({"text": "Drone status known", "ok": False})
    for v in names:
        tele = last.get(v) or {}
        landed = tele.get("landed_state") if isinstance(tele, Mapping) else None
        checks.append({"text": f"{v} on the ground",
                       "ok": landed is not None and int(landed) == 0})
        try:
            idle = srv.tasking.queue_for(v).active() is None
        except Exception:  # noqa: BLE001
            idle = False
        checks.append({"text": f"{v} has no task running", "ok": idle})
        mon = monitors.get(v)
        checks.append({"text": f"{v} BINGO not latched",
                       "ok": mon is None or not mon.fuel.bingo.tripped})
        checks.append({"text": f"{v} link up", "ok": mon is None or not _link_down(mon)})
    checks.append({"text": "Fake simulator",
                   "ok": getattr(getattr(srv, "backend", None), "sim", None) is not None})
    checks.append({"text": "No wargame running", "ok": not wargame_busy(srv)})
    done = getattr(srv, "boot_recovery_done", None)
    checks.append({"text": "Restart recovery finished", "ok": done is not None and _flag(done)})
    checks.append({"text": "Running in the app host",
                   "ok": bool(getattr(srv, "theater_listeners", None))})
    book = getattr(srv, "theater_proposals", None)
    pid = getattr(p, "proposal_id", None)
    valid = (book.get(pid) is not None) if book is not None else not p.expired()
    checks.append({"text": "Proposal still valid", "ok": bool(valid)})
    return checks


def _geo_of(value: Any) -> Any:
    """A GeoPoint from a GeoPoint or a HomeGeoPoint (`.geo`)."""
    return getattr(value, "geo", value)


def _same(a: Any, b: Any) -> bool:
    try:
        return (abs(float(a.latitude) - float(b.latitude)) < ORIGIN_DEG_TOL
                and abs(float(a.longitude) - float(b.longitude)) < ORIGIN_DEG_TOL
                and abs(float(a.altitude) - float(b.altitude)) < ORIGIN_ALT_TOL_M)
    except (AttributeError, TypeError, ValueError):
        return False


def _move_origin(srv: Any, new_home: GeoPoint, fix: Any) -> None:
    """Steps 2b-2d: the three copies, with no await in between."""
    srv.backend.sim.relocate_origin(new_home)          # b. the fake's copy
    srv.backend.relocate(new_home, fix)                # c. the MCP backend's copy
    for holder in list(srv.origin_holders):            # d. every attached holder
        holder.relocate(new_home)


async def _cross_check(srv: Any, new_home: GeoPoint) -> None:
    """Step 2e: every copy equals `new_home`, and so does the simulator's own
    `getHomeGeoPoint` (copies agreeing with each other prove nothing if the
    simulator is somewhere else). Raises TheaterIntegrityError."""
    copies = [("simulator", _geo_of(srv.backend.sim.home_geo)),
              ("MCP backend", _geo_of(srv.backend.home_geo))]
    copies += [(type(h).__name__, _geo_of(getattr(h, "home_geo", None)))
               for h in srv.origin_holders]
    bad = [name for name, geo in copies if not _same(geo, new_home)]
    try:
        rpc = await srv.backend.home_rpc()
    except Exception as exc:  # noqa: BLE001 — an unreadable origin is not a proven one
        bad.append(f"getHomeGeoPoint ({type(exc).__name__}: {exc})")
    else:
        if not _same(rpc, new_home):
            bad.append("getHomeGeoPoint")
    if bad:
        raise TheaterIntegrityError(
            f"origin copies disagree after the move: {', '.join(bad)} not at "
            f"{new_home.latitude:.7f}, {new_home.longitude:.7f}, {new_home.altitude:.3f} m HAE")


_BACKEND_ORIGIN_FIELDS = ("home_declared", "home_fix", "home_geo", "home")


def _origin_snapshot(srv: Any) -> dict[str, Any]:
    """Every copy of the origin as it is BEFORE step 2b, for `_roll_back`."""
    backend = srv.backend
    return {"sim": _geo_of(backend.sim.home_geo),
            "backend": {k: getattr(backend, k) for k in _BACKEND_ORIGIN_FIELDS
                        if hasattr(backend, k)},
            "holders": [(h, _geo_of(getattr(h, "home_geo", None)))
                        for h in list(srv.origin_holders)]}


def _roll_back(srv: Any, snap: Mapping[str, Any]) -> list[str]:
    """Put every origin copy back where `snap` found it (review A).

    The drones were proven landed before the move, so the fake re-parks them
    at the old home, landed; the monitor then measures them against the old
    fence and fuel home it never stopped using. No await, so a cancellation
    cannot land half-way. Each copy is tried on its own and then CHECKED by
    position (a holder that never moved is back by definition). Returns the
    copies that are not at the old origin: empty means every copy is back.
    """
    backend, old = srv.backend, snap["sim"]
    try:
        backend.sim.relocate_origin(old)
    except Exception as exc:  # noqa: BLE001 — checked by position below
        _LOG.error("theater rollback: the simulator's origin: %s", exc)
    saved = snap["backend"]
    try:
        backend.relocate(_geo_of(saved["home_geo"]), saved.get("home_fix"))
        for name, value in saved.items():       # exactly what it was, declared datum too
            setattr(backend, name, value)
    except Exception as exc:  # noqa: BLE001
        _LOG.error("theater rollback: the MCP backend's origin: %s", exc)
    for holder, geo in snap["holders"]:
        if geo is None:
            continue
        try:
            holder.relocate(geo)
        except Exception as exc:  # noqa: BLE001
            _LOG.error("theater rollback: %s: %s", type(holder).__name__, exc)
    stranded = []
    if not _same(_geo_of(backend.sim.home_geo), old):
        stranded.append("simulator")
    if not _same(_geo_of(getattr(backend, "home_geo", None)), _geo_of(saved.get("home_geo"))):
        stranded.append("MCP backend")
    stranded += [type(h).__name__ for h, geo in snap["holders"]
                 if geo is not None and not _same(_geo_of(getattr(h, "home_geo", None)), geo)]
    return stranded


def _integrity_failed(srv: Any, t: theaters.Theater, exc: BaseException,
                      stranded: list[str] | None = None) -> dict[str, Any]:
    """Latch `theater_integrity_error` (every `_submit` but land and hover
    refuses, and the monitor enforces nothing) and audit it. `stranded` is
    `_roll_back`'s answer, or None when nothing was rolled back."""
    msg = str(exc) if isinstance(exc, TheaterIntegrityError) else (
        f"the theater switch to {t.id} failed part-way ({type(exc).__name__}: {exc})")
    if stranded is None:
        msg += "; the origin copies were not put back"
    elif stranded:
        msg += (f"; {', '.join(stranded)} not put back at the old origin, so the "
                "safety monitor enforces nothing until a restart")
    else:
        msg += ("; every origin copy was put back at the old origin and the drones "
                "are parked at the old home, landed")
    srv.theater_integrity_error = msg
    srv.store.log_audit("theater_integrity", msg, theater_id=t.id,
                        epoch=getattr(srv, "theater_epoch", None),
                        rolled_back=stranded is not None and not stranded,
                        stranded=stranded)
    _LOG.error("theater integrity: %s", msg)
    return {"rejected": True, "error": "theater_integrity",
            "message": f"The simulation origin failed its cross-check; restart the host: {msg}"}


def _server_fn(srv: Any, name: str) -> Any:
    """A module-level helper of the server's own module, looked up at call
    time (this module never imports `server.py`, §0.2)."""
    return getattr(sys.modules.get(type(srv).__module__), name, None)


def _apply(srv: Any, p: Any, t: theaters.Theater, via: str) -> bool:
    """Steps 2f-2i minus persistence: everything derived from the origin.
    No await. Returns whether the airframe changed."""
    from .safety import FuelModel

    old = srv.theater
    # f. IN PLACE: every monitor holds a reference to this envelope.
    srv.envelope.geofence = t.ao_list()
    srv.envelope.home = t.home
    # g. fuel follows the airframe; alarms in progress are about the old area.
    changed = p.airframe != srv.airframe_id
    monitors = dict(srv.monitors)
    if changed:
        previous_af, srv.airframe_id = srv.airframe_id, p.airframe
        for v, mon in monitors.items():
            mon.fuel = FuelModel(airframe=p.airframe, home=t.home, time_scale=srv.time_scale)
            srv.store.log_fuel(v, 100.0, "ground", airframe=p.airframe,
                               reason="airframe_changed")
        srv.store.log_audit("airframe_changed",
                            f"airframe {previous_af} -> {p.airframe}; full tank for every drone",
                            airframe=p.airframe, previous=previous_af,
                            vehicles=sorted(monitors), theater_id=t.id)
    else:
        for mon in monitors.values():
            mon.fuel.home = t.home
    for mon in monitors.values():
        mon._active_alarms.clear()
        mon._breach_since.clear()
    # i (first half). Registered before any reader can see the new id.
    if t.dynamic:
        theaters.register_dynamic(t)
    # h. the server's own theater state.
    srv.theater = t
    srv.theater_mismatch = None
    srv.theater_epoch = int(srv.theater_epoch) + 1
    srv.theater_set_at_ms = _now_ms()
    srv.theater_previous = {"id": old.id, "label": old.label}
    srv.theater_set_via = via
    for poi in t.pois:                                # old baselines kept (M12)
        srv.pol.define_poi(poi.name, poi.lat, poi.lon)
    grid_for = _server_fn(srv, "terrain_grid_for")
    if grid_for is not None:
        srv._fitted_terrain_grid_deg = grid_for(t.bbox())
        srv.terrain_grid_deg = srv._fitted_terrain_grid_deg
    srv.stop_real_data()                              # non-blocking (§4.1.9 #11)
    srv.real_world = None
    for name in ("_last_tele", "ticks", "_terrain_floor_active", "mission_flags",
                 "_repath", "_missed_ticks"):
        cache = getattr(srv, name, None)
        if cache is not None:
            cache.clear()
    srv.tracks.mark_sim_reset()                       # as sim_reset does
    for track in srv.tracks.tracks():
        srv.store.tracks.put(track.to_dict())
    srv.sites = p.sites
    return changed


def _notify(srv: Any, state: Mapping[str, Any]) -> None:
    """Step 4: listeners are told after the switch; one failing never
    stops the rest and never fails the switch."""
    for cb in list(getattr(srv, "theater_listeners", []) or []):
        try:
            cb(dict(state))
        except Exception as exc:  # noqa: BLE001 — logged and audited, never raised
            _LOG.warning("theater listener %r failed: %s", cb, exc)
            srv.store.log_audit("theater_listener_failed", f"{type(exc).__name__}: {exc}",
                                listener=getattr(cb, "__qualname__", repr(cb))[:120])


async def _run_to_end(coro: Any) -> Any:
    """Await `coro` in a task of its own that a cancellation of the CALLER
    cannot stop half-way (review A, critical).

    Stop, or closing the chat, cancels the in-process tool call; that cancels
    `switch()` at its only await after the origin moved (`getHomeGeoPoint`),
    which used to leave the origin in the new area and the theater, fence and
    fuel homes in the old one. Here the step runs on to its end (committed,
    or rolled back and latched) and only then is the cancellation re-raised.
    If the step's own task is cancelled (the tasking loop shutting down),
    `_activate` rolls back first and this propagates that cancellation.
    """
    task = asyncio.ensure_future(coro)
    interrupted = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            interrupted = True
    if interrupted:
        if not task.cancelled() and task.exception() is not None:
            _LOG.error("theater switch finished after its caller left: %r", task.exception())
        raise asyncio.CancelledError()
    return task.result()


async def switch(srv: Any, p: Any, via: str = "mcp") -> dict[str, Any]:
    """Activate proposal `p` (§4.1.3). Runs ON THE TASKING LOOP.

    Returns the result dict, or a refusal: `switch_refused` (nothing moved)
    or `theater_integrity` (the origin copies disagreed; they are put back,
    and every `_submit` but land and hover refuses until a restart). `via` is
    "console" or "mcp". Once the origin starts to move the switch can no
    longer be cancelled half-way (`_run_to_end`).
    """
    t = p.theater
    early = cached_refusals(srv)                  # before the flag (review A)
    with srv._mode_lock:
        if srv._switching.is_set():
            return SwitchRefused([MSG_ALREADY_SWITCHING]).as_result()
        if wargame_busy(srv):
            return SwitchRefused([MSG_WARGAME]).as_result()
        if not early:
            srv._switching.set()
    if early:
        srv.store.log_audit("theater_switch_refused", "; ".join(early), theater_id=t.id,
                            reasons=early, via=via, cached=True)
        return SwitchRefused(early).as_result()
    try:
        try:
            if not srv.boot_recovery_done.is_set():
                raise SwitchRefused([MSG_RECOVERY])
            if not await srv.wait_ticks_idle(TICK_DRAIN_S):
                raise SwitchRefused([MSG_TICK])
            reasons = await refusals(srv, t)
            if reasons:
                raise SwitchRefused(reasons)
            # a. T1: the ONE MSL->HAE conversion for this activation.
            try:
                fix = canonical_altitude(t.home_alt_msl_m, t.home_lat, t.home_lon,
                                         datum="msl")
            except GeoidUnavailableError:
                raise SwitchRefused([MSG_GEOID]) from None
        except SwitchRefused as exc:
            srv.store.log_audit("theater_switch_refused", str(exc), theater_id=t.id,
                                reasons=exc.reasons, via=via)
            return exc.as_result()
        return await _run_to_end(_activate(srv, p, t, fix, via))
    finally:
        srv._switching.clear()


async def _activate(srv: Any, p: Any, t: theaters.Theater, fix: Any,
                    via: str) -> dict[str, Any]:
    """Steps 2b-4: move, cross-check, apply, persist, tell. The only await is
    `getHomeGeoPoint` in the cross-check; a failure there, or ANY exception or
    cancellation before `_apply` starts, puts every origin copy back
    (`_roll_back`) and latches `theater_integrity_error`."""
    new_home = GeoPoint(t.home_lat, t.home_lon, fix.alt_hae)
    old = srv.theater
    snap = _origin_snapshot(srv)
    applying = False
    try:
        _move_origin(srv, new_home, fix)
        await _cross_check(srv, new_home)
        applying = True
        changed = _apply(srv, p, t, via)
    except BaseException as exc:  # a half-move is never left behind
        # Past `_apply`'s start the server's own theater may already be the
        # new one, so the copies stay; the latch stops `_submit` and the
        # monitor's enforcement either way.
        stranded = None if applying else _roll_back(srv, snap)
        out = _integrity_failed(srv, t, exc, stranded)
        if not isinstance(exc, Exception):
            raise                                 # a cancellation, after the rollback
        return out
    state = state_of(srv, via)
    where = persist(srv.store, state)
    datum = {"home_hae_m": round(fix.alt_hae, 3), "undulation_m": round(fix.undulation_m, 3),
             "source": fix.source, "degraded": fix.degraded}
    srv.store.log_audit("theater_changed", f"theater {old.id} -> {t.id}",
                        old=old.id, new=t.id, epoch=srv.theater_epoch, via=via,
                        airframe=srv.airframe_id, datum=datum, dynamic=t.dynamic,
                        persisted=None if where is None else str(where))
    real = getattr(srv, "real", None) is not None
    if real:
        srv.start_real_data()                     # background, epoch-captured
    _notify(srv, state)
    return {
        "ok": True, "status": "accepted",
        "theater": {"id": t.id, "label": t.label, "epoch": srv.theater_epoch,
                    "dynamic": t.dynamic},
        "previous": {"id": old.id, "label": old.label},
        "home": {"lat": t.home_lat, "lon": t.home_lon, "alt_msl_m": t.home_alt_msl_m,
                 "alt_hae_m": datum["home_hae_m"], "undulation_m": datum["undulation_m"],
                 "datum_source": fix.source},
        "airframe": {"id": srv.airframe_id, "changed": changed},
        "fuel": "full tank (airframe changed)" if changed else "kept",
        "reset": list(RESETS), "kept": list(KEEPS),
        "sites_loaded": int(getattr(p.sites, "total", 0)),
        "real_data": "reloading in background" if real else "off",
    }


def state_of(srv: Any, via: str | None = None) -> dict[str, Any]:
    """The persisted document for `srv`'s running theater (§4.1.3).

    A dynamic theater carries its full row (`Theater.as_dict()` minus the
    volatile `real_data` block); a preset is stored by id only, so the table
    wins on reload. `via` overrides `srv.theater_set_via` (the host's boot
    persist passes "boot"). `time_scale` is deliberately absent: a restart
    runs at x1.
    """
    t = srv.theater
    row = None
    if t.dynamic:
        row = t.as_dict()
        row.pop("real_data", None)
    set_at = getattr(srv, "theater_set_at_ms", None)
    return {"schema": SCHEMA, "theater_id": t.id, "theater": row,
            "airframe": getattr(srv, "airframe_id", None),
            "epoch": int(getattr(srv, "theater_epoch", 0) or 0),
            "set_at_ms": set_at if set_at is not None else _now_ms(),
            "set_via": via or getattr(srv, "theater_set_via", None),
            "previous": getattr(srv, "theater_previous", None)}


def state_path(store_or_dir: Any) -> Path | None:
    """`<store>/theater.json` for a `Store` (None when in memory) or a directory."""
    if store_or_dir is None:
        return None
    if hasattr(store_or_dir, "in_memory") or hasattr(store_or_dir, "journal_paths"):
        root = getattr(store_or_dir, "root", None)
        return None if root is None else Path(root) / STATE_FILE
    text = os.fspath(store_or_dir)
    if not text or text == ":memory:":
        return None
    return Path(text) / STATE_FILE


def persist(store_or_dir: Any, state: Mapping[str, Any]) -> Path | None:
    """Write `state` to `<store>/theater.json` atomically (tmp file, then
    rename). Never raises: returns the path, or None (in-memory store, or a
    write failure, which is logged)."""
    path = state_path(store_or_dir)
    if path is None:
        return None
    tmp: str | None = None
    try:
        text = json.dumps(dict(state), indent=2, sort_keys=True, allow_nan=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".theater-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)              # a reader sees the old file or the new one
        tmp = None
        return path
    except (OSError, TypeError, ValueError) as exc:
        _LOG.warning("theater state not persisted to %s: %s", path, exc)
        return None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _load(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": f"{path.name} is unreadable ({type(exc).__name__}: {exc})"}
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return {"error": f"{path.name} is not a {SCHEMA} document"}
    tid = doc.get("theater_id")
    if not isinstance(tid, str) or not tid:
        return {"error": f"{path.name} names no theater"}
    if tid.startswith(theaters.DYNAMIC_PREFIX):
        row = doc.get("theater")
        if not isinstance(row, Mapping):
            return {"error": f"{path.name}: dynamic theater {tid[:60]!r} has no row"}
        try:
            t = theaters.from_dict(row)
            if t.id != tid:
                return {"error": f"{path.name}: row id {t.id[:60]!r} is not {tid[:60]!r}"}
            theaters.register_dynamic(t)
        except (TypeError, ValueError) as exc:
            return {"error": f"{path.name}: dynamic theater {tid[:60]!r} is invalid ({exc})"}
    elif tid not in theaters.THEATERS:
        return {"error": f"{path.name}: unknown theater {tid[:60]!r}"}
    airframe = doc.get("airframe")
    if airframe is not None:
        try:
            airframe = safety.get_airframe(airframe).id
        except (TypeError, ValueError):
            return {"error": f"{path.name}: unknown airframe {str(airframe)[:40]!r}"}
    epoch = doc.get("epoch", 0)
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        return {"error": f"{path.name}: epoch must be a non-negative integer"}
    return {"theater_id": tid, "airframe": airframe, "epoch": epoch}


def load_persisted(store_dir: Any) -> dict[str, Any] | None:
    """Read `<store>/theater.json` at boot (§4.1.4). Never raises.

    Registers a persisted dynamic theater BEFORE returning, so the host's
    `resolve_theater(tid)` finds it. Returns `{theater_id, airframe, epoch}`,
    `{error}` (corrupt or unknown: the host falls back to the default theater
    and audits `theater_restore_failed`), or None when nothing was persisted.
    """
    try:
        path = state_path(store_dir)
        if path is None or not path.exists():
            return None
        return _load(path)
    except Exception as exc:  # noqa: BLE001 — boot must never be blocked by this file
        return {"error": f"{STATE_FILE}: {type(exc).__name__}: {exc}"}
