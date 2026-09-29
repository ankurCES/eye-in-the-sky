"""Simulated wargame tools, their registry and the end route (M14a, PLAN.md
§4.5a; WG v2 §3.7, §3.8, §5.2.10; unit B4).

Ten `wg_*` tools, added by `register(mcp, srv)` to:

* `srv.wargame_mcp` (`MCPServer("godseye-wargame")`, never mounted). The
  analyst's toolbelt proxies it (B8), inside `wargame.console_call(session)`.
* `srv.mcp` as well, but only under `--wargame-mcp` (D3). The default `/mcp`
  catalog has no `wg_*` tool. `register` is called once per registry.

What every tool guarantees:

* **Simulated.** Every result, refusals included, carries `simulated: true`
  and `note: NOTIONAL_NOTE` (§3.1). Descriptions start "SIMULATION (M14a):"
  and name no order-of-battle class.
* **Session scoped.** Every tool except the entry tools (`ENTRY_TOOLS`:
  `wg_session_start`, `wg_session_status`, `wg_list_classes`) refuses
  `wargame_inactive` when no session is active, before anything else runs
  (only a replayed `idempotency_key` is answered first).
* **Scenario units only** (D1). The engine's provenance gate and real-site
  gate decide what can be planned against or engaged. This module reads no
  real data and passes the engine nothing but the caller's arguments.
* **Console-only confirm** (D2, §3.8). `wg_execute_engagement` goes straight
  to `engine.execute`, which needs the console's authorization AND
  `wargame.CONSOLE_CALL` equal to the authorizing chat session. A `/mcp` call
  never carries that context, so it is refused
  `engagement_requires_console_approval`.
* **Idempotent** mutating tools (TOOL_CONTRACT): a replayed `idempotency_key`
  returns the original result without running again, and a refusal is never
  recorded. `wg_execute_engagement` keys are scoped to the console session
  that made the call, so no other caller can read a recorded outcome.
* **Budgets** (§3.7): `wg_plan_corridor` and `wg_propose_strike` each spend
  one call of their own `CallBudget(20, 600 s)` per request that reaches the
  engine; past it they refuse `rate_limited`.

`wargame_router(srv, auth)` serves bearer `POST /wargame/session/end` (§3.5):
200 `{ok, aar_id, ...}`, or 409 `{error: "wargame_inactive"}` with no session.

This module duck-types `srv` (the `GodseyeUavServer`) and never imports
`server.py` (§0.2).
"""
from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Mapping
from typing import Any, Literal

from . import geo_http
from . import wargame_tables as _tables
from .wargame import CONSOLE_CALL, INACTIVE_MESSAGE, WargameRefused
from .wargame_tables import NOTIONAL_NOTE

_LOG = logging.getLogger(__name__)

#: The ten tools (§3.7), in registration order.
TOOL_NAMES: tuple[str, ...] = (
    "wg_session_start", "wg_session_status", "wg_list_classes", "wg_session_end",
    "wg_generate_scenario", "wg_spawn_force", "wg_list_forces", "wg_plan_corridor",
    "wg_propose_strike", "wg_execute_engagement")

#: The only `wg_*` tools the ISR toolbelt carries (§3.7; B7 mirrors it as
#: `analyst_policy.WG_ENTRY_TOOLS`). They work with no session.
ENTRY_TOOLS: frozenset[str] = frozenset(
    {"wg_session_start", "wg_session_status", "wg_list_classes"})

#: Approval class per tool (§3.7, §5.2.11); `analyst_policy` (B7) decides.
TOOL_CLASSES: Mapping[str, str] = {
    "wg_session_start": "sim", "wg_session_status": "read", "wg_list_classes": "read",
    "wg_session_end": "sim", "wg_generate_scenario": "sim", "wg_spawn_force": "sim",
    "wg_list_forces": "read", "wg_plan_corridor": "plan", "wg_propose_strike": "plan",
    "wg_execute_engagement": "engagement",
}
#: The tools that change state; each takes `idempotency_key`.
MUTATING: frozenset[str] = frozenset(
    name for name, klass in TOOL_CLASSES.items() if klass in ("sim", "engagement"))

#: §3.7 titles; `analyst_policy._TITLES` and `format.js TOOL_TITLES` match.
TITLES: Mapping[str, str] = {
    "wg_session_start": "Start a simulated wargame",
    "wg_session_status": "Wargame status",
    "wg_list_classes": "List wargame classes",
    "wg_session_end": "End the wargame",
    "wg_generate_scenario": "Generate a scenario",
    "wg_spawn_force": "Add simulated forces",
    "wg_list_forces": "List forces",
    "wg_plan_corridor": "Plan a corridor",
    "wg_propose_strike": "Propose a simulated strike",
    "wg_execute_engagement": "Execute a simulated engagement",
}

#: `(calls, window_s)` budgets (§3.7).
BUDGETS: Mapping[str, tuple[int, float]] = {
    "wg_plan_corridor": (20, 600.0),
    "wg_propose_strike": (20, 600.0),
}

#: Argument bounds (§3.7).
EVENTS_DEFAULT, EVENTS_MAX = 20, 100
COUNT_MAX = 6
SIDES: tuple[str, ...] = ("red", "blue")

#: `end(reason=...)` per caller: the console route, the analyst's approved
#: call (it carries `CONSOLE_CALL`), or a direct `/mcp` call.
END_REASON_ROUTE, END_REASON_CONSOLE, END_REASON_MCP = "operator", "analyst", "mcp"

RATE_LIMITED_MESSAGE = ("Too many {what} in the last {minutes} minutes; "
                        "try again in a few minutes.")
FAILED_MESSAGE = "The simulated wargame couldn't complete that ({kind}); nothing else changed."

#: Codes the end route answers 409 for (§3.5); anything else is a 500.
END_CONFLICT_CODES: frozenset[str] = frozenset({"wargame_inactive", "wargame_ending"})

Side = Literal["red", "blue"]
Template = Literal["air_defence_belt", "mech_advance", "strike_exercise"]
Intensity = Literal["low", "medium", "high"]

#: Tool descriptions (§5.2.10): each starts "SIMULATION (M14a):", names no
#: order-of-battle class and no real system, and states every height's datum.
DESCRIPTIONS: Mapping[str, str] = {
    "wg_session_start": (
        "SIMULATION (M14a): start an opt-in simulated wargame session in the running "
        "theater; the operator approves every start. Its forces are simulated scenario "
        "units only, and real places are context, never targets. `seed` (a whole number) "
        "makes the notional draws repeatable; `red_engages=false` keeps red forces from "
        "firing; `reveal_red=true` shows red truth in wargame answers. Returns "
        "{session_id, started_at_ms, seed, engine, table_version, red_engages, reveal_red, "
        "theater_id, ao: {half_extent_m, half_diagonal_m}, classes_that_fit, caveats}. "
        "Refused while a session runs (wargame_active), under real AirSim, during a "
        "theater switch, in a theater not cleared for the wargame, and when mapped places "
        "couldn't all be loaded. Nothing real is fired."),
    "wg_session_status": (
        "SIMULATION (M14a): the simulated wargame's status. With no session: "
        "{active: false, starting, last}. In a session: counts, blue units, pending and "
        "recent engagements, drones lost and the last `events` events (0-100, default "
        "20). Fog of war applies: red units stay hidden unless the session reveals red, "
        "and a strike's outcome stays hidden until a re-look assesses it. Changes nothing."),
    "wg_list_classes": (
        "SIMULATION (M14a): the notional wargame classes, red and blue, with generic "
        "labels and round play-balance numbers (ranges in metres, heights above the unit, "
        "times in sim seconds), plus fits_ao (its envelope is smaller than the running "
        "AO) and covers_ao (its envelope reaches every AO corner). Not weapon data. Use "
        "the `key` values as wg_class. Changes nothing."),
    "wg_session_end": (
        "SIMULATION (M14a): end the simulated wargame. Scenario units, their contacts and "
        "waiting engagements are removed; drones the wargame downed are restored at home, "
        "landed; aircraft keep their current tasks. The after-action review is filed as "
        "uav://reports/aar-<session id>, never as the latest report. Returns {ended, "
        "session_id, aar_id, resource, revived}."),
    "wg_generate_scenario": (
        "SIMULATION (M14a): place a whole scenario of simulated units, all or nothing. "
        "`template`: air_defence_belt, mech_advance or strike_exercise; `intensity`: "
        "low, medium (default) or high; `ad_class`: an optional red air-defence class key "
        "from wg_list_classes (default: the largest that fits the AO). Units are placed "
        "relative to the AO centre and home, at least 500 m from mapped places and "
        "theater points, 1 km from home for red, and 200 m apart. Only blue units are "
        "listed unless the session reveals red (red_placed counts them). Refused no_room "
        "when the area has no room."),
    "wg_spawn_force": (
        "SIMULATION (M14a): add `count` (1-6, default 1) simulated units of one class "
        "(`wg_class`, a key from wg_list_classes) for `side` (red or blue) at `lat`/`lon`; "
        "the first sits exactly there and the rest spiral round it. `objective_id` (the "
        "frc: id of a unit of the other side) sends a mobile unit toward it. Refused "
        "outside the AO, within 500 m of a mapped place or theater point, within 1 km of "
        "home (red), within 200 m of another unit and within 150 m of another sim object. "
        "Returns {units: [{unit_id, id, designator, side, wg_class, state, lat, lon, "
        "alt_msl_m (metres above mean sea level), position, ...}], caveats}."),
    "wg_list_forces": (
        "SIMULATION (M14a): the simulated scenario forces, optionally of one `side` (red "
        "or blue). Fog of war applies: red units are listed only when the session reveals "
        "red. Heights are alt_msl_m (metres above mean sea level). Changes nothing."),
    "wg_plan_corridor": (
        "SIMULATION (M14a): plan a least-exposure recce or re-look corridor for drone "
        "`vehicle` to `target_track_id` (required): a sensed contact of a scenario unit of "
        "this session. Mapped sites, other sim objects and real traffic are refused, as is "
        "a contact within 500 m of a mapped place or theater point. One altitude, "
        "`alt_agl_m` (metres above ground level, default 60). Planned against sensed "
        "threats only unless the session reveals red. `relook=true` adds points on a ring "
        "of `relook_radius_m` (150-1500 m, default 400) round the contact. Returns "
        "waypoints, length_m, eta_s, exposure_s, a notional p_survive, caveats and "
        "`recon_args`: fly them with mission_recon_route, dry run first. Drones only look; "
        "they never deliver effects. At most 20 plans per 10 minutes."),
    "wg_propose_strike": (
        "SIMULATION (M14a): propose a simulated strike by blue scenario shooter "
        "`shooter_id` (frc: id or unit id) on `target_track_id`, a sensed contact "
        "identified with at least probable confidence. Only scenario units of this session "
        "can be engaged: mapped sites, theater points, other sim objects and real traffic "
        "are refused, as is any target within 500 m of a mapped place or theater point or "
        "out of the shooter's notional range. Nothing is fired. Returns the pending "
        "engagement, a notional estimate from the perceived class, and `execute_args`: "
        "pass them unchanged to wg_execute_engagement, which the operator approves. It "
        "expires after 10 minutes. At most 20 proposals per 10 minutes."),
    "wg_execute_engagement": (
        "SIMULATION (M14a): roll the one simulated outcome of a proposed engagement. Pass "
        "wg_propose_strike's execute_args exactly: pending_id, shooter_id, "
        "target_track_id. It runs only after the operator approves it in the console; any "
        "other call is refused (engagement_requires_console_approval), and every gate is "
        "checked again. Nothing real is fired. The outcome stands until the wargame ends; "
        "unless red is revealed it stays hidden until a re-look (battle damage "
        "assessment). Returns {executed, engagement_id, fired_at_ms, outcome, "
        "outcome_note}."),
}


# ---------------------------------------------------------------- public --

def register(mcp: Any, srv: Any) -> None:
    """Add the ten `wg_*` tools to `mcp` (§5.2.10). Called once for
    `srv.wargame_mcp` and, under `--wargame-mcp`, once more for `srv.mcp`;
    the budgets (`srv.wargame_budgets`) are created once and shared.

    Every tool reads `srv.wargame` when it is CALLED, never here, so an
    engine replaced after construction is the one that answers."""
    if getattr(srv, "wargame_budgets", None) is None:
        srv.wargame_budgets = new_budgets()

    def tool(name: str) -> Any:
        return mcp.tool(name=name, title=TITLES[name], description=DESCRIPTIONS[name])

    @tool("wg_session_start")
    async def _wg_session_start(seed: int | None = None, red_engages: bool = True,
                                reveal_red: bool = False,
                                idempotency_key: str | None = None) -> dict:
        return await session_start(srv, seed=seed, red_engages=red_engages,
                                   reveal_red=reveal_red, idempotency_key=idempotency_key)

    @tool("wg_session_status")
    async def _wg_session_status(events: int = EVENTS_DEFAULT) -> dict:
        return await session_status(srv, events=events)

    @tool("wg_list_classes")
    async def _wg_list_classes() -> dict:
        return await list_classes(srv)

    @tool("wg_session_end")
    async def _wg_session_end(idempotency_key: str | None = None) -> dict:
        return await session_end(srv, idempotency_key=idempotency_key)

    @tool("wg_generate_scenario")
    async def _wg_generate_scenario(template: Template, intensity: Intensity = "medium",
                                    ad_class: str | None = None,
                                    idempotency_key: str | None = None) -> dict:
        return await generate_scenario(srv, template, intensity=intensity, ad_class=ad_class,
                                       idempotency_key=idempotency_key)

    @tool("wg_spawn_force")
    async def _wg_spawn_force(side: Side, wg_class: str, lat: float, lon: float,
                              count: int = 1, objective_id: str | None = None,
                              idempotency_key: str | None = None) -> dict:
        return await spawn_force(srv, side, wg_class, lat, lon, count=count,
                                 objective_id=objective_id, idempotency_key=idempotency_key)

    @tool("wg_list_forces")
    async def _wg_list_forces(side: Side | None = None) -> dict:
        return await list_forces(srv, side=side)

    @tool("wg_plan_corridor")
    async def _wg_plan_corridor(vehicle: str, target_track_id: str, alt_agl_m: float = 60.0,
                                relook: bool = False,
                                relook_radius_m: float = 400.0) -> dict:
        return await plan_corridor(srv, vehicle, target_track_id, alt_agl_m=alt_agl_m,
                                   relook=relook, relook_radius_m=relook_radius_m)

    @tool("wg_propose_strike")
    async def _wg_propose_strike(shooter_id: str, target_track_id: str) -> dict:
        return await propose_strike(srv, shooter_id, target_track_id)

    @tool("wg_execute_engagement")
    async def _wg_execute_engagement(pending_id: str, shooter_id: str, target_track_id: str,
                                     idempotency_key: str | None = None) -> dict:
        return await execute_engagement(srv, pending_id, shooter_id, target_track_id,
                                        idempotency_key=idempotency_key)


def wargame_router(srv: Any, auth: Any) -> Any:
    """Bearer `POST /wargame/session/end` (§3.5): `srv.wargame.end(reason=
    "operator")` -> 200 `{ok, aar_id, session_id, resource, revived,
    simulated}`; no session -> 409 `{error: "wargame_inactive", message}`
    (also 409 while an end is already running). A request body is not read:
    the AAR records `operator`, never caller text.

    `auth` is a FastAPI dependency (the host's bearer check)."""
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/wargame", dependencies=[Depends(auth)])

    @router.post("/session/end")
    async def wargame_session_end() -> Any:
        out = await end_session_for_route(srv)
        if out.get("rejected"):
            code = str(out.get("error"))
            return JSONResponse(
                {"error": code, "message": out.get("message"), "simulated": True},
                status_code=409 if code in END_CONFLICT_CODES else 500)
        return {"ok": True, "aar_id": out.get("aar_id"), "session_id": out.get("session_id"),
                "resource": out.get("resource"), "revived": list(out.get("revived") or []),
                "simulated": True}

    return router


# ------------------------------------------------------------ tool bodies --

async def session_start(srv: Any, *, seed: Any = None, red_engages: Any = True,
                        reveal_red: Any = False, idempotency_key: str | None = None) -> dict:
    """`wg_session_start` (entry tool): `engine.start(...)`, the §5.2.10 summary."""
    replay = srv._idem_replay("wg_session_start", idempotency_key)
    if replay is not None:
        return stamp(replay)
    if not isinstance(red_engages, bool) or not isinstance(reveal_red, bool):
        return refusal("invalid_parameter", "red_engages and reveal_red must be true or false.")
    out = await _engine(srv.wargame.start(seed=seed, red_engages=red_engages,
                                          reveal_red=reveal_red))
    return srv._idem_record("wg_session_start", idempotency_key, out)


async def session_status(srv: Any, *, events: Any = EVENTS_DEFAULT) -> dict:
    """`wg_session_status` (entry tool): the blue-view status (`truth=False`)."""
    n = _int_in(events, 0, EVENTS_MAX)
    if n is None:
        return refusal("invalid_parameter",
                       f"events must be a whole number from 0 to {EVENTS_MAX}.")
    return _sync(lambda: srv.wargame.status(truth=False, events=n))


async def list_classes(srv: Any) -> dict:
    """`wg_list_classes` (entry tool): every class with `fits_ao`/`covers_ao`
    for the running theater's AO (§3.7)."""
    def rows() -> dict:
        geo = _tables.ao_geometry(srv)
        return {"classes": _tables.class_rows(geo), "theater_id": srv.theater.id,
                "ao": dict(geo), "classes_that_fit": _tables.classes_that_fit(geo),
                "table_version": _tables.TABLE_VERSION}
    return _sync(rows)


async def session_end(srv: Any, *, idempotency_key: str | None = None) -> dict:
    """`wg_session_end`: `engine.end(reason=...)` -> `{ended, aar_id, resource}`."""
    replay = srv._idem_replay("wg_session_end", idempotency_key)
    if replay is not None:
        return stamp(replay)
    inactive = _inactive(srv)
    if inactive is not None:
        return inactive
    reason = END_REASON_CONSOLE if CONSOLE_CALL.get() else END_REASON_MCP
    out = await _engine(srv.wargame.end(reason=reason))
    return srv._idem_record("wg_session_end", idempotency_key, out)


async def end_session_for_route(srv: Any) -> dict:
    """The route's end (§3.5): the same refusals, reason `operator`."""
    inactive = _inactive(srv)
    if inactive is not None:
        return inactive
    return await _engine(srv.wargame.end(reason=END_REASON_ROUTE))


async def generate_scenario(srv: Any, template: Any, *, intensity: Any = "medium",
                            ad_class: Any = None, idempotency_key: str | None = None) -> dict:
    """`wg_generate_scenario`: `engine.generate(...)`, fog-filtered."""
    replay = srv._idem_replay("wg_generate_scenario", idempotency_key)
    if replay is not None:
        return stamp(replay)
    inactive = _inactive(srv)
    if inactive is not None:
        return inactive
    out = await _engine(srv.wargame.generate(str(template), str(intensity),
                                             str(ad_class) if ad_class else None))
    return srv._idem_record("wg_generate_scenario", idempotency_key, out)


async def spawn_force(srv: Any, side: Any, wg_class: Any, lat: Any, lon: Any, *,
                      count: Any = 1, objective_id: Any = None,
                      idempotency_key: str | None = None) -> dict:
    """`wg_spawn_force`: `engine.spawn(...)` -> `{units, caveats}`."""
    replay = srv._idem_replay("wg_spawn_force", idempotency_key)
    if replay is not None:
        return stamp(replay)
    inactive = _inactive(srv)
    if inactive is not None:
        return inactive
    if side not in SIDES:
        return refusal("invalid_parameter", "side must be red or blue.")
    n = _int_in(count, 1, COUNT_MAX)
    if n is None:
        return refusal("invalid_parameter", f"count must be a whole number from 1 to {COUNT_MAX}.")
    out = await _engine(srv.wargame.spawn(side, str(wg_class), lat, lon, count=n,
                                          objective_id=str(objective_id) if objective_id
                                          else None))
    return srv._idem_record("wg_spawn_force", idempotency_key, out)


async def list_forces(srv: Any, *, side: Any = None) -> dict:
    """`wg_list_forces`: blue view (`truth=False`)."""
    inactive = _inactive(srv)
    if inactive is not None:
        return inactive
    if side is not None and side not in SIDES:
        return refusal("invalid_parameter", "side must be red, blue or omitted.")
    return _sync(lambda: srv.wargame.list_forces(side=side, truth=False))


async def plan_corridor(srv: Any, vehicle: Any, target_track_id: Any, *,
                        alt_agl_m: Any = 60.0, relook: Any = False,
                        relook_radius_m: Any = 400.0) -> dict:
    """`wg_plan_corridor` (§5.2.8): a gated scenario contact is required."""
    inactive = _inactive(srv)
    if inactive is not None:
        return inactive
    if not _text_arg(vehicle) or not _text_arg(target_track_id):
        return refusal("invalid_parameter", "vehicle and target_track_id are required.")
    if not isinstance(relook, bool):
        return refusal("invalid_parameter", "relook must be true or false.")
    if not _budget(srv, "wg_plan_corridor").take():
        return _rate_limited("wg_plan_corridor", "corridor plans")
    return await _engine(srv.wargame.plan_corridor(
        str(vehicle), str(target_track_id), alt_agl_m=alt_agl_m, relook=relook,
        relook_radius_m=relook_radius_m))


async def propose_strike(srv: Any, shooter_id: Any, target_track_id: Any) -> dict:
    """`wg_propose_strike` (§5.2.7): a pending engagement and `execute_args`."""
    inactive = _inactive(srv)
    if inactive is not None:
        return inactive
    if not _text_arg(shooter_id) or not _text_arg(target_track_id):
        return refusal("invalid_parameter", "shooter_id and target_track_id are required.")
    if not _budget(srv, "wg_propose_strike").take():
        return _rate_limited("wg_propose_strike", "strike proposals")
    return await _engine(srv.wargame.propose_strike(str(shooter_id), str(target_track_id)))


async def execute_engagement(srv: Any, pending_id: Any, shooter_id: Any, target_track_id: Any,
                             *, idempotency_key: str | None = None) -> dict:
    """`wg_execute_engagement` (§3.8): `engine.execute(...)`, which refuses
    unless the console authorized it and `CONSOLE_CALL` is that chat session.
    Keys are recorded per console session; a call without one records none."""
    console = CONSOLE_CALL.get()
    key_tool = f"wg_execute_engagement@{console}" if console else None
    if key_tool is not None:
        replay = srv._idem_replay(key_tool, idempotency_key)
        if replay is not None:
            return stamp(replay)
    inactive = _inactive(srv)
    if inactive is not None:
        return inactive
    out = await _engine(srv.wargame.execute(str(pending_id), str(shooter_id),
                                            str(target_track_id)))
    return srv._idem_record(key_tool, idempotency_key, out) if key_tool is not None else out


# --------------------------------------------------------------- helpers --

def stamp(out: Any) -> dict:
    """A wargame tool result: a copy with `simulated: true` and `note` (§3.1)."""
    body = dict(out) if isinstance(out, Mapping) else {"result": out}
    body["simulated"] = True
    body.setdefault("note", NOTIONAL_NOTE)
    return body


def refusal(code: str, message: str, **extra: Any) -> dict:
    """`{rejected: true, error, message}` (§3.1), stamped; the UI shows
    `message` verbatim, as text."""
    return stamp({"rejected": True, "error": code, "message": message, **extra})


def _inactive(srv: Any) -> dict | None:
    """The `wargame_inactive` refusal when no session is active, else None."""
    if bool(getattr(getattr(srv, "wargame", None), "active", False)):
        return None
    return refusal("wargame_inactive", INACTIVE_MESSAGE)


def _failed(exc: BaseException) -> dict:
    _LOG.exception("simulated wargame call failed")
    return refusal("wargame_failed", FAILED_MESSAGE.format(kind=type(exc).__name__))


async def _engine(call: Awaitable[Any]) -> dict:
    """Await one engine call; a raised `WargameRefused` is its refusal, any
    other error a `wargame_failed` refusal. The answer is stamped."""
    try:
        out = await call
    except WargameRefused as exc:
        return stamp(exc.as_result())
    except Exception as exc:  # noqa: BLE001 — reported, never swallowed
        return _failed(exc)
    return stamp(out)


def _sync(fn: Any) -> dict:
    """A synchronous engine read, as `_engine` answers it."""
    try:
        return stamp(fn())
    except WargameRefused as exc:
        return stamp(exc.as_result())
    except Exception as exc:  # noqa: BLE001 — reported, never swallowed
        return _failed(exc)


def _int_in(value: Any, lo: int, hi: int) -> int | None:
    """A whole number in [lo, hi] (bools and fractions refused), or None."""
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n) or n != int(n) or not lo <= n <= hi:
        return None
    return int(n)


def _text_arg(value: Any) -> bool:
    """A non-empty id or name."""
    return isinstance(value, str) and bool(value.strip())


def new_budgets() -> dict[str, geo_http.CallBudget]:
    """Fresh per-server budgets (§3.7)."""
    return {tool: geo_http.CallBudget(calls, window_s)
            for tool, (calls, window_s) in BUDGETS.items()}


def _budget(srv: Any, tool: str) -> geo_http.CallBudget:
    budgets = getattr(srv, "wargame_budgets", None)
    if budgets is None:
        budgets = srv.wargame_budgets = new_budgets()
    return budgets[tool]


def _rate_limited(tool: str, what: str) -> dict:
    calls, window_s = BUDGETS[tool]
    return refusal("rate_limited", RATE_LIMITED_MESSAGE.format(
        what=what, minutes=int(window_s // 60)), budget={"calls": calls, "window_s": window_s})
