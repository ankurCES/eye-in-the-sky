#!/usr/bin/env python3
"""Scripted ISR recon mission driven over the REAL MCP transport (PLAN §8.1).

This is the mission driver `scripts/demo_laptop.sh` runs. It ATTACHES to an
already-running stack (`python -m godseye_uav.launch`, normally started by
`start.sh`) and speaks the same protocol an external agentic harness speaks:
MCP Streamable HTTP at `POST /mcp` with a Bearer token (TOOL_CONTRACT
"Conventions"). It builds no sim, no server and no store of its own — that is
the whole point. The previous version reached into `srv.mcp._tool_manager._tools`
and awaited the tool functions in-process, so it exercised no transport, no
auth and no queue, and nothing it did was visible in the command center
(register finding R7).

Usage:
    # stack already up (start.sh / demo_laptop.sh):
    PYTHONPATH=mcp:<airsim>/PythonClient .venv/bin/python scripts/demo_mission.py \
        --mcp-url http://127.0.0.1:8791/mcp --token dev-token --theater default

The theater MUST match the one the server was launched with: the AO geometry
this script flies comes from `godseye_uav.theaters`, the same single source of
truth `launch.py` builds its geofence from. A mismatch is detected against live
telemetry and fails loudly rather than producing a mystery geofence rejection.

TOOL DRIFT: Wave 2 is adding the TOOL_CONTRACT §4.3 mission primitives
(`mission_grid_search`, `mission_dry_run`, `uav_capture_image`, `uav_los_check`,
`sim_set_time`, ...) alongside the older `uav_mission(kind=...)` dispatcher.
This script is written against TOOL_CONTRACT and discovers what the server
actually offers via `tools/list`, preferring the contract tool and falling back
to the legacy one. Parameter names are read from each tool's `inputSchema`
rather than guessed. Anything genuinely absent is SKIPPED, announced on the
spot, and repeated in the closing summary — never silently ignored.

ISR-only (M14): observe, identify, report. Nothing here prosecutes anything.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

import httpx2
from godseye_uav import theaters
from godseye_uav.geo import canonical_altitude
from godseye_uav.safety import haversine_m
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

DEFAULT_MCP_URL = "http://127.0.0.1:8791/mcp"
DEFAULT_TOKEN = "dev-token"
DEFAULT_VEHICLE = "Drone1"
#: Sim clock the demo asks for when `sim_set_time` exists: local solar noon,
#: so the M6 sun-side rule has a real sun to reason about.
DEMO_SIM_TIME = "2026-06-21 12:00:00"
#: Ceiling on an AUTO-sized monitor window. A plan costed longer than this is
#: watched this long and then reported as unfinished — never silently.
MAX_AUTO_MONITOR_S = 1800.0
#: Slack over the server's own est_time_s: the plan is costed at nominal
#: groundspeed, and the executor pays turn and capture time on top.
AUTO_MONITOR_SLACK = 1.3
AUTO_MONITOR_PAD_S = 30.0
#: Only used when no plan product carried an est_time_s at all.
FALLBACK_MONITOR_S = 120.0


class DemoError(RuntimeError):
    """The demo cannot continue. Always carries what to do about it."""


# ---------------------------------------------------------------------------
# MCP client wrapper
# ---------------------------------------------------------------------------


class Harness:
    """Thin, contract-aware wrapper over one MCP `ClientSession`.

    Knows which tools the server actually published (`tools/list`) and what
    each one's parameters are called (`inputSchema`), so this demo can drive a
    server mid-refactor without guessing and without crashing.
    """

    def __init__(self, session: ClientSession, out=print):
        self.session = session
        self.out = out
        self.schemas: dict[str, dict[str, Any]] = {}
        self.skipped: list[str] = []

    async def discover(self) -> list[str]:
        listing = await self.session.list_tools()
        for tool in listing.tools:
            schema = _attr(tool, "input_schema", "inputSchema") or {}
            self.schemas[tool.name] = schema.get("properties", {}) or {}
        return sorted(self.schemas)

    # -- capability probes ----------------------------------------------
    def first(self, *names: str) -> str | None:
        """First of `names` the server published, preferring contract order."""
        for n in names:
            if n in self.schemas:
                return n
        return None

    def arg(self, tool: str, *candidates: str) -> str | None:
        """Which of `candidates` this tool's schema actually accepts."""
        props = self.schemas.get(tool, {})
        for c in candidates:
            if c in props:
                return c
        return None

    def skip(self, what: str, why: str) -> None:
        line = f"{what}: {why}"
        self.skipped.append(line)
        self.out(f"  SKIP {line}")

    def require(self, *names: str) -> str:
        tool = self.first(*names)
        if tool is None:
            raise DemoError(
                f"server publishes none of {list(names)} — it is not a godSeye "
                f"MCP server, or it failed to register its tools. Published: "
                f"{sorted(self.schemas)}")
        return tool

    # -- calling ---------------------------------------------------------
    async def call(self, tool: str, *, _timeout_s: float = 60.0, **args: Any) -> dict:
        """tools/call over the wire; returns the tool's JSON payload.

        `_timeout_s` is underscored so it can never collide with a tool
        parameter of the same name arriving through `**args`.

        An argument the tool's schema does not declare is an error, not a
        silently dropped keyword: a typo'd parameter that the server ignores
        looks exactly like a feature that quietly does nothing.
        """
        unknown = [k for k in args if k not in self.schemas.get(tool, {})]
        if unknown:
            raise DemoError(
                f"{tool} does not accept {unknown}; its parameters are "
                f"{sorted(self.schemas.get(tool, {}))}. Fix the caller, do not "
                f"drop the argument.")
        result = await self.session.call_tool(tool, args, read_timeout_seconds=_timeout_s)
        return _payload(tool, result)


def _attr(obj: Any, *names: str) -> Any:
    """First attribute of `obj` that exists. The MCP python SDK renamed its
    model fields from camelCase to snake_case; read both rather than pin a
    version and break silently on the other."""
    for n in names:
        value = getattr(obj, n, None)
        if value is not None:
            return value
    return None


def _payload(tool: str, result: Any) -> dict:
    """CallToolResult -> the tool's JSON dict, or a structured error."""
    if _attr(result, "is_error", "isError"):
        text = _first_text(result)
        return {"error": {"code": "tool_error", "message": text or f"{tool} failed",
                          "retryable": False}}
    structured = _attr(result, "structured_content", "structuredContent")
    if isinstance(structured, dict):
        # MCPServer wraps a non-dict return under "result"; unwrap that.
        return structured.get("result", structured) if set(structured) == {"result"} else structured
    text = _first_text(result)
    if text is None:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {"text": text}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _first_text(result: Any) -> str | None:
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    return None


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------


def _printer(out=print):
    def banner(msg: str) -> None:
        out("")
        out(f"=== {msg} ===")
    return banner


def _veh(h: Harness, tool: str, vehicle: str) -> dict[str, Any]:
    """`{"vehicle": ...}` if this tool takes one, `{}` otherwise."""
    key = h.arg(tool, "vehicle", "vehicle_name")
    return {key: vehicle} if key else {}


def _err(payload: dict) -> str | None:
    """The structured error message in a tool payload, if it is one."""
    if not isinstance(payload, dict):
        return None
    e = payload.get("error")
    if isinstance(e, dict):
        return str(e.get("message", e))
    if isinstance(e, str):
        return e
    if payload.get("rejected"):
        return json.dumps(payload.get("gate", payload), default=str)
    return None


# ---------------------------------------------------------------------------
# the mission
# ---------------------------------------------------------------------------


async def run_demo(url: str = DEFAULT_MCP_URL, token: str = DEFAULT_TOKEN,
                   theater_id: str = theaters.DEFAULT_THEATER_ID,
                   *, vehicle: str = DEFAULT_VEHICLE,
                   monitor_s: float | None = None,
                   bridge_url: str | None = None, out=print) -> int:
    """Drive the scripted recon mission over MCP. Returns a process exit code."""
    t = theaters.get(theater_id)
    banner = _printer(out)

    out(f"[demo] MCP  : {url}  (Streamable HTTP + Bearer auth)")
    out(f"[demo] AO   : {t.id} — {t.label} ({t.place})")
    if bridge_url:
        out(f"[demo] watch: {bridge_url}")

    # The MCP Streamable-HTTP transport an external agentic harness uses:
    # Bearer auth on the httpx2 client, POST /mcp, initialize, tools/call.
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                           timeout=120.0) as hc,
        streamable_http_client(url, http_client=hc) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        h = Harness(session, out=out)
        names = await h.discover()
        out(f"[demo] server published {len(names)} tools: {', '.join(names)}")
        return await _fly(h, t, vehicle=vehicle, monitor_s=monitor_s,
                          banner=banner, out=out)


async def _fly(h: Harness, t: theaters.Theater, *, vehicle: str,
               monitor_s: float | None, banner, out) -> int:
    # ---- 0. confirm we are talking about the same AO the server flies in ---
    banner("confirm theater (the AO this script plans in is the server's AO)")
    tele_tool = h.require("uav_get_telemetry")
    tele = await h.call(tele_tool, vehicle=vehicle)
    if _err(tele) or "lat" not in tele:
        raise DemoError(f"{tele_tool} did not return a position: "
                        f"{_err(tele) or tele}")
    off_m = haversine_m(tele["lat"], tele["lon"], t.home_lat, t.home_lon)
    out(f"  {vehicle} at {tele['lat']:.5f},{tele['lon']:.5f} "
        f"alt_hae={tele.get('alt_hae', float('nan')):.1f}m — "
        f"{off_m / 1000:.1f} km from {t.id} home")
    if not t.contains(tele["lat"], tele["lon"]):
        raise DemoError(
            f"{vehicle} is at {tele['lat']:.5f},{tele['lon']:.5f}, which is "
            f"OUTSIDE the '{t.id}' AO this demo plans in ({off_m / 1000:.1f} km "
            f"from its home). The server was launched with a different "
            f"--theater. Re-run with --theater matching the server, or restart "
            f"the server with --theater {t.id}.")
    out(f"  fuel={tele.get('fuel_pct')}%  bingo={tele.get('bingo_fuel_pct')}%")

    # ---- 1. sim clock: give M6 a real sun -------------------------------
    banner("set sim time (M6 sun-side doctrine)")
    set_time = h.first("sim_set_time")
    if set_time is None:
        h.skip("sim_set_time", "not published yet (TOOL_CONTRACT §4.4) — "
                              "sun-side arc selection runs on the server default")
    else:
        arg = h.arg(set_time, "datetime", "datetime_str", "time", "iso")
        if arg is None:
            h.skip("sim_set_time", f"no datetime parameter in {sorted(h.schemas[set_time])}")
        else:
            r = await h.call(set_time, **{arg: DEMO_SIM_TIME})
            out(f"  sim clock -> {DEMO_SIM_TIME} local solar  {_short(r, 80)}")

    # ---- 2. order of battle ---------------------------------------------
    banner("spawn order of battle (ISR-only: objects to observe, M14)")
    await _spawn_order_of_battle(h, t, out)

    # ---- 3. takeoff ------------------------------------------------------
    banner("takeoff")
    takeoff = h.require("uav_takeoff")
    alt_arg = h.arg(takeoff, "alt_agl_m", "alt_m")
    args: dict[str, Any] = {"vehicle": vehicle}
    if alt_arg:
        args[alt_arg] = theaters.DEMO_ALT_M_AGL
    r = await _command(h, takeoff, vehicle, args, out=out)
    if _err(r):
        raise DemoError(f"takeoff rejected: {_err(r)}")
    out(f"  task {r.get('task_id', r.get('task_handle', r))}")

    # ---- 4. plan + dry run ------------------------------------------------
    plan = t.demo_mission()
    polygon = [[lat, lon] for lat, lon in plan["polygon"]]
    banner("dry run the grid search (plan -> dry-run -> execute, PLAN §5)")
    dry = await _dry_run(h, vehicle, polygon, plan, out)

    # ---- 5. execute the grid search ---------------------------------------
    banner("fly the recon grid (M1 spacing derived server-side)")
    handle = await _grid_search(h, vehicle, polygon, plan, out)
    if handle is None:
        return 1

    # ---- 6. monitor, scanning as it flies ---------------------------------
    window, window_note = _monitor_window(monitor_s, dry, handle)
    banner(f"monitor + scan while flying (up to {window:.0f}s — "
           f"watch it live in the command center)")
    out(f"  {window_note}")
    tracks, flight_failure = await _monitor(h, vehicle, handle,
                                            monitor_s=window, out=out)
    if not tracks:
        tracks = await _scan(h, vehicle, out)

    # ---- 7. sensors ------------------------------------------------------
    banner("sensor extras (TOOL_CONTRACT §4.2)")
    await _sensor_extras(h, vehicle, t, tracks, out)

    # ---- 8. threat + reports ----------------------------------------------
    banner("threat assessment (M13 — sensor posture only, never engagement, M14)")
    top = await _assess(h, vehicle, t, tracks, out)

    banner("SALUTE / INTREP reporting (M8)")
    await _report(h, vehicle, top, out)

    # ---- 9. recover ------------------------------------------------------
    banner("return to home + land")
    if not await _idle(h, vehicle, timeout_s=10.0):
        # Still flying the grid. Doctrine: stop the current tasking cleanly
        # before commanding recovery — never stack commands on a busy vehicle
        # (TOOL_CONTRACT "Busy", T2).
        abort = h.first("uav_abort")
        if abort is None:
            h.skip("uav_abort", "not published (TOOL_CONTRACT §4.1) — cannot "
                                "stop the grid, so RTB will be refused as busy")
        else:
            out("  grid still running — uav_abort (cancel + hover) before recovery")
            await h.call(abort, **_veh(h, abort, vehicle))
    rth = h.first("uav_return_to_home")
    if rth is None:
        h.skip("uav_return_to_home", "not published (TOOL_CONTRACT §4.1)")
    else:
        r = await _command(h, rth, vehicle, _veh(h, rth, vehicle), out=out)
        out(f"  RTB {r.get('task_id', r.get('task_handle', _err(r) or r))}")
    land = h.first("uav_land")
    if land is None:
        h.skip("uav_land", "not published (TOOL_CONTRACT §4.1)")
    else:
        r = await _command(h, land, vehicle, _veh(h, land, vehicle),
                           wait_s=30.0, out=out)
        out(f"  land {r.get('task_id', r.get('task_handle', _err(r) or r))}")

    # ---- summary ---------------------------------------------------------
    banner("demo complete" if flight_failure is None
           else "demo finished — THE FLIGHT DID NOT")
    out("  ISR-only (M14): observed, identified and reported — never prosecuted.")
    if dry is None:
        out("  NOTE: no dry-run was possible; the plan was committed unpreviewed.")
    if h.skipped:
        out(f"  {len(h.skipped)} step(s) DEGRADED because the server does not "
            f"publish them yet:")
        for line in h.skipped:
            out(f"    - {line}")
    else:
        out("  every TOOL_CONTRACT step this demo exercises was available.")
    if flight_failure is not None:
        # The recon leg is the product. Reporting "demo complete" over a failed
        # mission task is exactly the kind of green-looking lie this repo has
        # been bitten by, so it exits non-zero.
        out("")
        out(f"  GRID SEARCH TASK FAILED SERVER-SIDE: {flight_failure}")
        out("  The mission leg did not complete. Everything above still ran, but")
        out("  the flight the demo exists to show did not finish. Exit code 3.")
        return 3
    return 0


# ---------------------------------------------------------------------------
# one command per vehicle at a time (TOOL_CONTRACT "Busy", T2)
# ---------------------------------------------------------------------------


async def _queue_state(h: Harness, vehicle: str) -> tuple[str, int] | None:
    """(queue state, pending count) for one vehicle, or None if unknowable."""
    tool = h.first("uav_task_status")
    if tool is not None:
        st = await h.call(tool, vehicle=vehicle)
        if not _err(st):
            return str(st.get("state", "idle")).lower(), int(st.get("queued", 0) or 0)
    tool = h.first("uav_list_vehicles")
    if tool is not None:
        st = await h.call(tool)
        for v in st.get("vehicles") or []:
            if isinstance(v, dict) and v.get("name") == vehicle:
                return str(v.get("state", "idle")).lower(), int(v.get("queued", 0) or 0)
    return None


_SETTLED = {"idle", "done", "completed", "failed", "cancelled", "rejected"}

#: Consecutive failed telemetry samples before the demo gives up. A single
#: dropped sample is a transport hiccup; five in a row means the vehicle is no
#: longer observable and pretending otherwise would be a lie.
_MAX_TELEMETRY_MISSES = 5


async def _idle(h: Harness, vehicle: str, *, timeout_s: float) -> bool:
    """True once the vehicle's queue can accept a new command."""
    deadline = time.monotonic() + timeout_s
    while True:
        qs = await _queue_state(h, vehicle)
        if qs is None:
            return True  # no way to ask; the call itself will report busy
        state, queued = qs
        if state in _SETTLED and queued == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(1.0)


async def _command(h: Harness, tool: str, vehicle: str, args: dict[str, Any], *,
                   wait_s: float = 90.0, out=print) -> dict:
    """Submit a commanding tool, respecting one-in-flight-per-vehicle (T2).

    The server allows exactly one active command per vehicle: a second one is
    refused. So wait for the queue to settle first, and if the server still
    answers `{"status": "busy"}` (the shape TOOL_CONTRACT specifies), wait
    again and retry once. A refusal is reported, never swallowed.
    """
    if not await _idle(h, vehicle, timeout_s=wait_s):
        out(f"  {vehicle} still busy after {wait_s:.0f}s — submitting {tool} anyway")
    result = await h.call(tool, **args)
    if str(result.get("status", "")).lower() == "busy":
        out(f"  server BUSY with {result.get('current')} — waiting, then retrying {tool}")
        await _idle(h, vehicle, timeout_s=wait_s)
        result = await h.call(tool, **args)
    return result


# ---------------------------------------------------------------------------
# mission steps
# ---------------------------------------------------------------------------


async def _spawn_order_of_battle(h: Harness, t: theaters.Theater, out) -> None:
    """Lay down the observable objects, with the altitude datum stated (T1).

    `theaters.demo_targets()` gives ground altitude in MSL. TOOL_CONTRACT §4.4
    asks for `alt_msl_m`, and the shipped tool's own description says `alt_m`
    is "the legacy spelling of the same datum" — MSL, NOT an absolute geodetic
    altitude. So both paths send MSL. The value is still run through the
    canonical converter (T1) purely so the line printed for the operator names
    the geoid separation and the EGM96 source the server will resolve it
    against; nothing is converted here.
    """
    spawn = h.require("sim_spawn_target")
    class_arg = h.arg(spawn, "class", "mesh", "object_class")
    if class_arg is None:
        raise DemoError(f"sim_spawn_target has no class/mesh parameter: "
                        f"{sorted(h.schemas[spawn])}")
    msl_arg = h.arg(spawn, "alt_msl_m")
    legacy_arg = None if msl_arg else h.arg(spawn, "alt_m")
    if msl_arg is None and legacy_arg is None:
        h.skip("sim_spawn_target altitude",
               "tool takes no altitude parameter; targets sit at whatever the "
               "server defaults to (TOOL_CONTRACT §4.4 wants alt_msl_m)")

    for target in t.demo_targets():
        args: dict[str, Any] = {"name": target["name"], class_arg: target["mesh"],
                                "lat": target["lat"], "lon": target["lon"]}
        datum = ""
        if msl_arg:
            args[msl_arg] = target["alt_m"]
            datum = f"{target['alt_m']:.0f} m MSL"
        elif legacy_arg:
            # `alt_m` here is the LEGACY SPELLING of alt_msl_m, not HAE.
            # Converting to HAE before sending would displace every target by
            # the undulation — -22.2 m at the default theater, -33.0 m at
            # indo-pak-loc — i.e. bury or float the whole order of battle.
            fix = canonical_altitude(target["alt_m"], target["lat"], target["lon"],
                                     datum="msl")
            args[legacy_arg] = fix.alt_msl
            datum = (f"{fix.alt_msl:.0f} m MSL via legacy alt_m "
                     f"(N={fix.undulation_m:+.2f} -> {fix.alt_hae:.1f} m HAE "
                     f"server-side, {fix.source})")
        r = await h.call(spawn, **args)
        if _err(r):
            raise DemoError(f"spawn {target['name']} failed: {_err(r)}")
        out(f"  {target['name']:<14s} {target['mesh']:<6s} "
            f"{target['lat']:.5f},{target['lon']:.5f}  {datum}")


async def _dry_run(h: Harness, vehicle: str, polygon: list, plan: dict, out) -> dict | None:
    """TOOL_CONTRACT §4.3 mission_dry_run, or a dry_run flag, or nothing."""
    tool = h.first("mission_dry_run")
    if tool is not None:
        args = _grid_args(h, tool, vehicle, polygon, plan)
        kind_arg = h.arg(tool, "kind", "mission")
        if kind_arg:
            args[kind_arg] = "grid_search"
        r = await h.call(tool, **args)
        if _err(r):
            out(f"  dry run rejected: {_err(r)}")
            return None
        _print_plan(r, out)
        return r

    legacy = h.first("uav_mission")
    if legacy is not None and h.arg(legacy, "dry_run"):
        args = _legacy_mission_args(h, legacy, vehicle, polygon, plan)
        args["dry_run"] = True
        r = await h.call(legacy, **args)
        if _err(r):
            out(f"  dry run rejected: {_err(r)}")
            return None
        _print_plan(r, out)
        return r

    h.skip("mission_dry_run", "not published and uav_mission has no dry_run flag "
                             "(TOOL_CONTRACT §4.3) — cannot preview the plan, so "
                             "the next step commits the flight unpreviewed")
    return None


def _print_plan(r: dict, out) -> None:
    gate = r.get("gate") if isinstance(r.get("gate"), dict) else r
    out(f"  waypoints={r.get('waypoint_count', len(r.get('waypoints') or []))}  "
        f"lane_spacing={r.get('lane_spacing_m', r.get('sweep_spacing_m', '?'))}m  "
        f"est_time={gate.get('est_time_s', '?')}s  "
        f"est_fuel={gate.get('plan_fuel_pct', r.get('est_fuel_pct', '?'))}%  "
        f"bingo_required={gate.get('required_pct', r.get('bingo_fuel_pct', '?'))}%")
    violations = (gate or {}).get("envelope_violations")
    out(f"  gate: {'PASS' if not violations else 'VIOLATIONS ' + str(violations)}")


def _grid_args(h: Harness, tool: str, vehicle: str, polygon: list,
               plan: dict) -> dict[str, Any]:
    """Arguments for a TOOL_CONTRACT §4.3 mission_grid_search-shaped tool."""
    args: dict[str, Any] = {"vehicle": vehicle, "polygon": polygon}
    alt = h.arg(tool, "alt_agl_m", "alt_m")
    if alt:
        args[alt] = plan["alt_m"]
    speed = h.arg(tool, "speed", "speed_mps")
    if speed:
        args[speed] = plan["speed_mps"]
    overlap = h.arg(tool, "overlap_pct")
    if overlap:
        # M1: the caller supplies overlap, never lane spacing.
        args[overlap] = 20.0
    cam = h.arg(tool, "camera")
    if cam:
        args[cam] = "0"
    return args


def _legacy_mission_args(h: Harness, tool: str, vehicle: str, polygon: list,
                         plan: dict) -> dict[str, Any]:
    """Arguments for the legacy `uav_mission(kind=..., params={...})`."""
    args: dict[str, Any] = {"vehicle": vehicle, "kind": "grid_search",
                            "params": {"polygon": polygon, "alt_m": plan["alt_m"]}}
    speed = h.arg(tool, "speed_mps")
    if speed:
        args[speed] = plan["speed_mps"]
    return args


async def _grid_search(h: Harness, vehicle: str, polygon: list, plan: dict,
                       out) -> dict | None:
    tool = h.first("mission_grid_search")
    if tool is not None:
        r = await _command(h, tool, vehicle,
                           _grid_args(h, tool, vehicle, polygon, plan), out=out)
    else:
        legacy = h.first("uav_mission")
        if legacy is None:
            raise DemoError("server publishes neither mission_grid_search nor "
                            "uav_mission — there is no way to fly a grid "
                            "(TOOL_CONTRACT §4.3)")
        h.skip("mission_grid_search",
               "not published yet (TOOL_CONTRACT §4.3) — flying the legacy "
               "uav_mission(kind='grid_search') dispatcher instead")
        r = await _command(h, legacy, vehicle,
                           _legacy_mission_args(h, legacy, vehicle, polygon, plan),
                           out=out)
    if _err(r):
        out(f"  REJECTED: {_err(r)}")
        out("  (the gate result is not an error — re-plan and try again)")
        return None
    out(f"  mission {r.get('mission_handle', r.get('task_id', r.get('task_handle', '?')))}"
        f"  waypoints={r.get('waypoint_count', len(r.get('waypoints') or []))}"
        f"  spacing={r.get('lane_spacing_m', r.get('sweep_spacing_m', '?'))}m"
        f"  bingo={r.get('bingo_fuel_pct', '?')}%")
    return r


def _plan_seconds(*plans: Any) -> float | None:
    """The server's own `est_time_s` out of a dry-run or mission return.

    Both carry it at the top level and inside `gate`; the gate is the M4
    pre-flight product, so read that first.
    """
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        gate = plan.get("gate")
        for src in (gate if isinstance(gate, dict) else None, plan):
            if src is None:
                continue
            value = src.get("est_time_s")
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
    return None


def _monitor_window(requested: float | None, *plans: Any) -> tuple[float, str]:
    """How long to watch the grid fly, and why. Returns (seconds, reason).

    A FIXED default is the wrong shape here. The shipped demo plan is costed by
    the server at ~346 s, so a flat 120 s window meant the one-command demo
    always walked away from its own recon grid at roughly a third of coverage,
    aborted it, and printed "demo complete" — a green-looking lie of exactly
    the kind this repo keeps being bitten by. Nothing server-side stops the leg
    finishing: the T2 watchdog keys on lack of progress, not total duration
    (measured: a grid ran 300 s+ without being failed). Only this window did.
    So when the operator has not asked for a specific window, size it from the
    plan the server just costed, and say so.
    """
    if requested is not None:
        return float(requested), (f"window: {float(requested):.0f}s, as asked "
                                  f"(--monitor-s / MONITOR_S)")
    est = _plan_seconds(*plans)
    if est is None:
        return FALLBACK_MONITOR_S, (
            f"window: no est_time_s in the plan product, so falling back to "
            f"{FALLBACK_MONITOR_S:.0f}s — this may NOT cover the leg")
    window = min(est * AUTO_MONITOR_SLACK + AUTO_MONITOR_PAD_S, MAX_AUTO_MONITOR_S)
    if window < est:
        return window, (
            f"window: the server costed this plan at {est:.0f}s, longer than "
            f"the {MAX_AUTO_MONITOR_S:.0f}s ceiling — the grid WILL be reported "
            f"unfinished")
    return window, (f"window: auto-sized from the server's plan "
                    f"(est_time_s={est:.0f}s -> {window:.0f}s); "
                    f"--monitor-s overrides")


async def _monitor(h: Harness, vehicle: str, handle: dict, *, monitor_s: float,
                   out) -> tuple[list[dict], str | None]:
    """Watch the grid fly AND scan as it goes — contacts appear when the
    sensor actually overflies them, which is the whole point of the grid.

    Returns (contacts seen (M11 persistent track ids), failure reason or None).
    """
    status_tool = h.first("mission_status", "uav_task_status")
    if status_tool is None:
        h.skip("mission_status", "not published (TOOL_CONTRACT §4.3) — progress "
                                "is shown from telemetry only")
    scan_tool = h.first("uav_get_detections", "uav_scan_targets")
    handle_id = (handle.get("mission_handle") or handle.get("task_id")
                 or handle.get("task_handle"))
    tele_tool = h.require("uav_get_telemetry")
    deadline = time.monotonic() + monitor_s
    seen: dict[str, dict] = {}
    finished = False
    outcome = ""
    failure: str | None = None
    misses = 0
    ticks = 0
    while time.monotonic() < deadline and not finished:
        tele = await h.call(tele_tool, vehicle=vehicle)
        if "lat" not in tele:
            # Report every dropped sample. A monitor that hides them is how a
            # dead telemetry path looks like a quiet mission.
            misses += 1
            out(f"  telemetry sample {misses} UNAVAILABLE: {_err(tele) or tele}")
            if misses >= _MAX_TELEMETRY_MISSES:
                raise DemoError(
                    f"{tele_tool} failed {misses} times in a row — the vehicle "
                    f"cannot be monitored, so the mission is not being watched. "
                    f"Check the server log.")
            await asyncio.sleep(3.0)
            continue
        misses = 0
        progress = ""
        if status_tool:
            st = await h.call(status_tool, **_status_args(h, status_tool, vehicle, handle_id))
            state = str(st.get("state", st.get("status", ""))).lower()
            if state:
                progress = f" state={state} progress={st.get('progress_pct', '?')}%"
                if state in _SETTLED:
                    finished = True
                    # A task that ends carrying an `error` did NOT succeed: the
                    # watchdog or the backend failed it. Say so — a monitor that
                    # quietly stops printing progress is how a dead mission
                    # looks like a finished one.
                    task_error = st.get("error")
                    if task_error or state in ("failed", "rejected"):
                        failure = str(task_error or state)
                        outcome = f"FAILED: {failure}"
                    else:
                        outcome = f"finished ({state})"
            else:
                problem = _err(st)
                out(f"  {status_tool} is not answering with a state "
                    f"({problem or st}) — progress from telemetry only")
                status_tool = None
        out(f"  {tele['lat']:.5f},{tele['lon']:.5f} "
            f"alt_hae={tele.get('alt_hae', float('nan')):.0f}m "
            f"spd={tele.get('speed_mps', '?')}m/s "
            f"fuel={tele.get('fuel_pct', '?')}% "
            f"bingo={tele.get('bingo_fuel_pct', '?')}%{progress}"
            f"{' — ' + outcome if outcome else ''}")
        ticks += 1
        # Scan on alternate ticks: the sensor pass is a second RPC round trip
        # per sample and the route executor is using the same link.
        if ticks % 2 == 1 or finished:
            for trk in await _scan_once(h, scan_tool, vehicle, out):
                tid = str(trk.get("track_id", ""))
                if tid and tid not in seen:
                    seen[tid] = trk
                    _print_track(trk, out)
        if not finished:
            await asyncio.sleep(3.0)
    if not finished:
        out(f"  still flying after {monitor_s:.0f}s — carrying on with the "
            f"sensor phase (raise MONITOR_S to watch the whole grid)")
    return list(seen.values()), failure


def _short(value: Any, limit: int = 46) -> str:
    """One readable line out of a nested report field.

    The SALUTE/track payloads nest a rich dict under `unit`, `equipment` and
    `confidence`; dumping them raw makes a console demo unreadable. Prefer the
    human-readable key the report already carries, then truncate.
    """
    if isinstance(value, dict):
        for key in ("text", "level", "platform", "code", "label", "name",
                    "category", "assessment"):
            if key in value:
                return _short(value[key], limit)
        value = json.dumps(value, default=str)
    elif not isinstance(value, str):
        value = json.dumps(value, default=str)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _print_track(trk: dict, out) -> None:
    out(f"    CONTACT {trk.get('track_id', '?')!s:<24s} "
        f"{_short(trk.get('unit', trk.get('category', '?')), 40):<40s} "
        f"conf={_short(trk.get('confidence', '?'), 12)}")


async def _scan_once(h: Harness, tool: str | None, vehicle: str, out) -> list[dict]:
    if tool is None:
        return []
    args: dict[str, Any] = {"vehicle": vehicle}
    cam = h.arg(tool, "camera")
    if cam:
        args[cam] = "0"
    r = await h.call(tool, **args)
    if _err(r):
        out(f"  {tool} error: {_err(r)}")
        return []
    tracks = r.get("tracks")
    if not isinstance(tracks, list):
        tracks = r.get("detections")
    if not isinstance(tracks, list):
        return []
    return [t for t in tracks if isinstance(t, dict)]


def _status_args(h: Harness, tool: str, vehicle: str, handle_id: Any) -> dict[str, Any]:
    args: dict[str, Any] = {}
    hk = h.arg(tool, "mission_handle", "task_id", "handle")
    if hk and handle_id:
        args[hk] = handle_id
    vk = h.arg(tool, "vehicle")
    if vk:
        args[vk] = vehicle
    return args


async def _scan(h: Harness, vehicle: str, out) -> list[dict]:
    """Final sweep for contacts if the monitored flight found none."""
    tool = h.first("uav_get_detections", "uav_scan_targets")
    if tool is None:
        h.skip("uav_get_detections", "not published (TOOL_CONTRACT §4.2) — no "
                                    "contacts, so nothing to identify or assess")
        return []
    for _ in range(3):
        tracks = await _scan_once(h, tool, vehicle, out)
        if tracks:
            for trk in tracks:
                _print_track(trk, out)
            return tracks
        await asyncio.sleep(1.0)
    out("  no contacts (the grid did not overfly anything detectable)")
    return []


async def _sensor_extras(h: Harness, vehicle: str, t: theaters.Theater,
                         tracks: list[dict], out) -> None:
    cap = h.first("uav_capture_image")
    if cap is None:
        h.skip("uav_capture_image", "not published yet (TOOL_CONTRACT §4.2) — "
                                   "no imagery product for the report")
    else:
        args: dict[str, Any] = {"vehicle": vehicle}
        for name, value in (("camera", "0"), ("type", "scene"), ("jpeg_quality", 75)):
            k = h.arg(cap, name)
            if k:
                args[k] = value
        r = await h.call(cap, **args)
        if _err(r):
            out(f"  uav_capture_image error: {_err(r)}")
        else:
            out(f"  image {r.get('resource', r.get('uri', r.get('image_ref', 'captured')))} "
                f"sun={r.get('sun_azimuth_deg', r.get('sun', '?'))}")

    los = h.first("uav_los_check")
    if los is None:
        h.skip("uav_los_check", "not published yet (TOOL_CONTRACT §4.2) — M5 "
                               "standoff cannot be verified, only asserted")
        return
    target = tracks[0] if tracks else None
    lat = float(target["lat"]) if target and "lat" in target else t.center()[0]
    lon = float(target["lon"]) if target and "lon" in target else t.center()[1]
    args = {"vehicle": vehicle, "lat": lat, "lon": lon}
    # The altitude below is the theater's GROUND elevation in metres MSL, so
    # only an MSL spelling may carry it. TOOL_CONTRACT: "Never a bare alt_m" —
    # prefer the datumed name, and take `alt_m` only as the legacy spelling of
    # the same datum. alt_agl_m and alt_hae_m are DIFFERENT datums: filling one
    # of those with an MSL number would move the aimpoint by the ground
    # elevation or by the geoid separation, so they are refused, loudly,
    # rather than silently accepted.
    alt = h.arg(los, "alt_msl_m", "alt_m")
    if alt is None:
        h.skip("uav_los_check altitude",
               f"tool publishes no MSL altitude parameter "
               f"({sorted(h.schemas[los])}); this demo's target altitude is "
               f"MSL and will not be sent down a different datum, so M5 "
               f"standoff stays unverified")
        return
    args[alt] = t.home_alt_msl_m
    r = await h.call(los, **args)
    if _err(r):
        out(f"  uav_los_check error: {_err(r)}")
    else:
        out(f"  LOS to {lat:.5f},{lon:.5f}: {r.get('los')} "
            f"model={r.get('model', '?')} obstacle={r.get('first_obstacle')}")


async def _assess(h: Harness, vehicle: str, t: theaters.Theater,
                  tracks: list[dict], out) -> str | None:
    tool = h.first("mission_threat_assessment", "uav_assess_threat")
    if tool is None:
        h.skip("mission_threat_assessment", "not published (TOOL_CONTRACT §4.3)")
        return tracks[0].get("track_id") if tracks else None
    args: dict[str, Any] = {}
    vk = h.arg(tool, "vehicle")
    if vk:
        args[vk] = vehicle
    # TOOL_CONTRACT §4.3 scopes the assessment to an area. Give it the AO the
    # grid just flew, not an implicit "everything", or the tool has to guess.
    poly = h.arg(tool, "area_polygon", "polygon", "area")
    if poly:
        args[poly] = [[lat, lon] for lat, lon in t.ao]
    r = await h.call(tool, **args)
    if _err(r):
        out(f"  {tool} error: {_err(r)}")
        return tracks[0].get("track_id") if tracks else None
    # The §4.3 mission tool wraps the structured report in `report`/`assessment`
    # alongside the mission handle; the legacy tool returns it flat.
    report = r
    for key in ("report", "assessment"):
        nested = r.get(key)
        if isinstance(nested, dict) and ("assessments" in nested or "count" in nested):
            report = nested
            break
    assessments = report.get("assessments") or report.get("contacts") or []
    out(f"  highest threat: {report.get('highest_threat', '?')}  "
        f"contacts: {report.get('count', len(assessments))}")
    if r.get("survey_note"):
        out(f"  {_short(r['survey_note'], 120)}")
    for a in assessments[:3]:
        out(f"  {a.get('track_id', '?'):<24s} {_short(a.get('category', '?'), 8):<8s} "
            f"{_short(a.get('threat_level', '?'), 8):<8s} "
            f"score={a.get('threat_score', '?')}  "
            f"{_short(a.get('recommendation', ''), 90)}")
    if assessments:
        return assessments[0].get("track_id")
    return tracks[0].get("track_id") if tracks else None


async def _report(h: Harness, vehicle: str, track_id: str | None, out) -> None:
    ident = h.first("mission_identify_target", "uav_identify_target")
    if ident is None:
        h.skip("mission_identify_target", "not published (TOOL_CONTRACT §4.3/§4.2)")
    elif track_id is None:
        out("  no track to identify — nothing was detected this pass")
    else:
        args: dict[str, Any] = {"track_id": track_id}
        vk = h.arg(ident, "vehicle")
        if vk:
            args[vk] = vehicle
        r = await h.call(ident, **args)
        if _err(r):
            # The flown identification pass (M7 wide->narrow cross-cue) can
            # legitimately refuse — no LOS point at the cross-cue altitude, for
            # instance. Report that, then still produce the M8 SALUTE from the
            # track store so the demo's reporting product exists, and say which
            # one the operator is looking at.
            out(f"  {ident} REFUSED the identification pass: {_err(r)}")
            plain = h.first("uav_identify_target") if ident != "uav_identify_target" else None
            if plain is None:
                h.skip(f"{ident}", "refused, and no store-only SALUTE tool to "
                                   "fall back on — no M8 report this pass")
                r = None
            else:
                out(f"  falling back to {plain} (SALUTE from the track store, "
                    f"no new imagery)")
                r = await h.call(plain, track_id=track_id)
                if _err(r):
                    out(f"  {plain} error: {_err(r)}")
                    r = None
        if r:
            out(f"  {r.get('format', 'SALUTE')} {r.get('track_id', track_id)}")
            for field in ("size", "activity", "location", "unit", "time",
                          "equipment", "confidence"):
                if field in r:
                    out(f"    {field:<11s} {_short(r[field], 96)}")

    intrep = h.first("uav_target_report")
    if intrep is None:
        h.skip("uav_target_report", "not published — no INTREP roll-up (M8)")
        return
    r = await h.call(intrep)
    if _err(r):
        out(f"  uav_target_report error: {_err(r)}")
    else:
        tracks = r.get("tracks") or []
        out(f"  {r.get('format', 'INTREP')} as of {r.get('as_of_iso', r.get('as_of', '?'))}"
            f"  tracks={r.get('count', len(tracks))}")
        for key in ("mission_summary", "assessment", "summary", "highest_threat"):
            if key in r:
                out(f"    {key:<16s} {_short(r[key], 110)}")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="godSeye scripted recon mission over the real MCP transport")
    ap.add_argument("--mcp-url", default=DEFAULT_MCP_URL,
                    help="Streamable HTTP endpoint of a RUNNING godseye_uav.launch stack")
    ap.add_argument("--token", default=DEFAULT_TOKEN, help="Bearer token (T4e)")
    ap.add_argument("--theater", default=theaters.DEFAULT_THEATER_ID,
                    choices=theaters.ids(),
                    help="must match the --theater the server was launched with")
    ap.add_argument("--vehicle", default=DEFAULT_VEHICLE)
    ap.add_argument("--monitor-s", type=float, default=None,
                    help="seconds to watch the grid fly before the sensor "
                         "phase. Omit to auto-size from the server's own "
                         "est_time_s, so the leg actually finishes")
    ap.add_argument("--bridge-url", default=None,
                    help="printed so the operator knows where to watch")
    return ap


def _leaves(exc: BaseException) -> Iterable[BaseException]:
    """Flatten an ExceptionGroup — anyio task groups raise these on transport
    failure, and a bare repr of the group hides the useful cause."""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _leaves(sub)
    else:
        yield exc


async def _main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return await run_demo(args.mcp_url, args.token, args.theater,
                              vehicle=args.vehicle, monitor_s=args.monitor_s,
                              bridge_url=args.bridge_url)
    except Exception as exc:  # noqa: BLE001 - reported in full, never swallowed
        causes = list(_leaves(exc))
        for cause in causes:
            print(f"\n[demo] FAILED: {type(cause).__name__}: {cause}", file=sys.stderr)
        if not any(isinstance(c, DemoError) for c in causes):
            print(f"[demo] is the stack up? the MCP endpoint should be "
                  f"{args.mcp_url}; start the whole stack with "
                  f"./scripts/demo_laptop.sh", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(_main(argv))
    except KeyboardInterrupt:
        print("\n[demo] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
