"""Approval policy for the in-app analyst (contract §5.2). Pure; no I/O.

Every tool call the analyst makes is classified here, by tool name AND
arguments, into one of six classes:

  read             auto   -- reads state; may write an audit row, never moves anything
  plan             auto   -- a dry run: nothing is queued and nothing moves
  sensor           ask    -- tasks or reads a sensor; writes intel (sightings, POL);
                             the operator may allow a sensor TOOL for the session
  command          ask    -- moves an aircraft or changes its mission; EVERY call asks
  sim              ask    -- changes the simulated world; EVERY call asks
  safety_override  ask    -- overrides a safety mechanism; EVERY call asks

Session grants follow contract v1.1 §10.2: per tool, sensor class only.  (v1
also let ``sim`` be granted for a session; v1.1 withdrew that.)

The policy FAILS CLOSED: a tool it does not know is a ``command`` (approval
required, no "allow for session").  Two argument rules need care because the
server silently ignores arguments a tool does not declare:

* ``dry_run: true`` only turns a call into a ``plan`` for tools that actually
  honour ``dry_run`` (``DRY_RUN_TOOLS``).  ``mission_cancel(dry_run=True)`` is
  still a real cancel, so it stays a ``command``.
* A ``lost_link_plan`` (top level, or inside ``params``) is applied to the
  vehicle's LIVE lost-link plan before the server looks at ``dry_run``
  (server.py ``_launch_mission``), so any call carrying one is a ``command``.
  ``params`` is read the way the server reads it: FastMCP also accepts it as
  a JSON STRING and parses it into the dict, so a string is parsed here too,
  and one that cannot be parsed but mentions ``lost_link_plan`` fails closed.
  (Today the SDK's schema check rejects a string ``params`` before the call
  reaches the server; the policy must not depend on that.)

``STATIC_AUTO_TOOLS`` is the set that may go into the SDK's ``allowed_tools``
(which bypasses ``can_use_tool`` entirely), so it holds only tools whose
classification is ``read`` whatever their arguments.  Arg-dependent tools --
including ``mission_dry_run``, which becomes a ``command`` when its ``params``
carry a ``lost_link_plan`` -- are routed through ``can_use_tool`` and
auto-allowed there when ``classify(...).auto`` is true.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

READ = "read"
PLAN = "plan"
SENSOR = "sensor"
COMMAND = "command"
SIM = "sim"
SAFETY_OVERRIDE = "safety_override"

CLASSES = (READ, PLAN, SENSOR, COMMAND, SIM, SAFETY_OVERRIDE)
AUTO_CLASSES = frozenset({READ, PLAN})
#: Classes whose calls an operator may allow for the rest of the session (per tool).
SESSION_CLASSES = frozenset({SENSOR})

#: The in-process SDK server's name; the CLI exposes its tools as
#: ``mcp__godseye__<tool>``.
SDK_SERVER_NAME = "godseye"
TOOL_PREFIX = f"mcp__{SDK_SERVER_NAME}__"


@dataclass(frozen=True)
class Decision:
    klass: str
    auto: bool
    allow_session: bool
    title: str
    summary: str
    consequences: tuple[str, ...]


# --------------------------------------------------------------------------
# The explicit table.  Every tool the godseye server registers must appear in
# exactly one of these sets (tests/test_analyst_policy.py iterates the real
# server's tool list), plus the toolbelt's curated tools.
# --------------------------------------------------------------------------

SERVER_READ_TOOLS = frozenset({
    "uav_get_telemetry", "uav_list_vehicles", "uav_task_status", "mission_status",
    "uav_los_check", "uav_target_report", "uav_identify_target", "uav_assess_threat",
    "uav_list_ob_classes", "uav_real_data_status", "uav_deconflict_airspace",
    # Excluded from the toolbelt (the intel tools replace it), classified anyway
    # so the table covers every server tool.
    "uav_list_tracks",
})

#: The toolbelt's curated in-process tools (analyst_toolbelt.py).
CURATED_TOOLS = frozenset({
    "intel_overview", "intel_search", "intel_entity", "read_intel_resource",
    "ui_focus", "ui_track", "ui_show_orb", "ui_inspect",
})

PLAN_TOOLS = frozenset({"mission_dry_run"})

SENSOR_TOOLS = frozenset({
    "uav_get_detections", "uav_scan_targets", "uav_capture_image",
    "uav_set_gimbal", "uav_set_fov",
})

COMMAND_TOOLS = frozenset({
    "uav_takeoff", "uav_land", "uav_return_to_home", "uav_goto_gps", "uav_fly_route",
    "uav_hover", "uav_orbit_poi", "uav_mission", "mission_grid_search",
    "mission_recon_route", "mission_track_target", "mission_identify_target",
    "mission_threat_assessment", "mission_handoff_track", "uav_handoff_target",
    "mission_cancel", "uav_abort",
})

SIM_TOOLS = frozenset({
    "sim_set_time", "sim_set_weather", "sim_spawn_target", "sim_move_target",
    "sim_set_gps_degradation", "sim_hydrate_real_data", "sim_spawn_order_of_battle",
    # Legacy and excluded from the toolbelt (it zeroes the wind), classified anyway.
    "sim_set_environment",
})

SAFETY_OVERRIDE_TOOLS = frozenset({"sim_set_fuel", "sim_set_link_state", "sim_reset"})

#: Tools whose server implementation honours ``dry_run``.  Only these can be a
#: ``plan``; the server ignores undeclared arguments, so ``dry_run`` on any
#: other tool is NOT a dry run.
DRY_RUN_TOOLS = frozenset({
    "uav_mission", "uav_orbit_poi", "mission_grid_search", "mission_recon_route",
    "mission_track_target", "mission_identify_target", "mission_threat_assessment",
    "mission_handoff_track",
})

READ_TOOLS = SERVER_READ_TOOLS | CURATED_TOOLS

KNOWN_TOOLS = (READ_TOOLS | PLAN_TOOLS | SENSOR_TOOLS | COMMAND_TOOLS | SIM_TOOLS
               | SAFETY_OVERRIDE_TOOLS)

#: Bare names safe to list in ``allowed_tools``: read whatever the arguments.
STATIC_AUTO_TOOLS: frozenset[str] = frozenset(READ_TOOLS)

#: Command tools that put an aircraft in the air (or keep it flying) when they
#: succeed; an approved one switches the console into tracking mode.
_FLYING_TOOLS = frozenset({
    "uav_takeoff", "uav_goto_gps", "uav_fly_route", "uav_orbit_poi", "uav_mission",
    "uav_return_to_home", "mission_grid_search", "mission_recon_route",
    "mission_track_target", "mission_identify_target", "mission_handoff_track",
    "uav_handoff_target",
})

_MISSION_KIND = {
    "grid_search": "grid_search", "recon_route": "recon_route",
    "track_target": "track_target", "track": "track_target",
    "identify": "identify_target", "identify_target": "identify_target",
    "orbit_poi": "orbit_poi", "threat_assessment": "threat_assessment",
    # Legacy GEV point-assess: a FLIGHT (plan_mission("assess") -> _launch_mission),
    # not the threat assessment.
    "assess": "assess",
}

_TOOL_KIND = {
    "mission_grid_search": "grid_search", "mission_recon_route": "recon_route",
    "mission_track_target": "track_target", "mission_identify_target": "identify_target",
    "mission_threat_assessment": "threat_assessment", "uav_orbit_poi": "orbit_poi",
    "mission_handoff_track": "handoff", "uav_handoff_target": "handoff",
}

_KIND_LABEL = {
    "grid_search": "grid search", "recon_route": "route recon",
    "track_target": "contact track", "identify_target": "contact identification",
    "orbit_poi": "orbit", "threat_assessment": "threat assessment",
    "handoff": "track handoff", "assess": "point assessment",
}

_TITLES = {
    "uav_get_telemetry": "Read telemetry",
    "uav_list_vehicles": "List vehicles",
    "uav_task_status": "Check task status",
    "mission_status": "Check mission status",
    "uav_los_check": "Check line of sight",
    "uav_target_report": "Build intelligence report",
    "uav_identify_target": "Read contact report",
    "uav_assess_threat": "Assess threat",
    "uav_list_ob_classes": "List equipment classes",
    "uav_real_data_status": "Check real-data status",
    "uav_deconflict_airspace": "Check airspace",
    "uav_list_tracks": "List all tracks",
    "intel_overview": "Read situation overview",
    "intel_search": "Search intel",
    "intel_entity": "Read entity",
    "read_intel_resource": "Read server resource",
    "ui_focus": "Highlight in orb",
    "ui_track": "Show drone on map",
    "ui_show_orb": "Return to orb",
    "ui_inspect": "Open inspector",
    "uav_get_detections": "Collect detections",
    "uav_scan_targets": "Scan for targets",
    "uav_capture_image": "Capture image",
    "uav_set_gimbal": "Point camera",
    "uav_set_fov": "Set field of view",
    "uav_takeoff": "Take off",
    "uav_land": "Land",
    "uav_return_to_home": "Return to home",
    "uav_goto_gps": "Fly to point",
    "uav_fly_route": "Fly route",
    "uav_hover": "Hover",
    "uav_orbit_poi": "Orbit point",
    "mission_grid_search": "Grid search",
    "mission_recon_route": "Route recon",
    "mission_track_target": "Track contact",
    "mission_identify_target": "Identify contact",
    "mission_threat_assessment": "Threat assessment",
    "mission_handoff_track": "Hand off track",
    "uav_handoff_target": "Hand off track",
    "mission_cancel": "Cancel mission",
    "uav_abort": "Abort",
    "sim_set_time": "Set sim time",
    "sim_set_weather": "Set sim weather",
    "sim_spawn_target": "Spawn sim target",
    "sim_move_target": "Move sim target",
    "sim_set_gps_degradation": "Degrade GPS",
    "sim_hydrate_real_data": "Load real-world data",
    "sim_spawn_order_of_battle": "Spawn order of battle",
    "sim_set_environment": "Set sim environment",
    "sim_set_fuel": "Refuel",
    "sim_set_link_state": "Set link state",
    "sim_reset": "Reset simulation",
}

TAKEOFF_NOTE = "Takes off first if the aircraft is on the ground."
GATE_NOTE = ("The server's fuel, BINGO and geofence gate runs first and may "
             "reject it.")
SAFETY_TRANSITION_NOTE = "Refused while a BINGO or lost-link return is flying."
PLAN_NOTE = "Plan only: nothing is queued and the aircraft does not move."


def bare_name(tool: str) -> str:
    """``mcp__godseye__uav_takeoff`` -> ``uav_takeoff``.

    Only the godseye prefix is stripped: a tool from any other MCP server keeps
    its full name, is unknown to this table, and therefore fails closed.
    """
    tool = str(tool or "")
    if tool.startswith(TOOL_PREFIX):
        return tool[len(TOOL_PREFIX):]
    return tool


def mission_kind(tool: str, args: dict | None) -> str | None:
    """Canonical mission kind for a tool call (``grid_search``, ``orbit_poi``...).

    Used to pair a dry run with the live call it previews.  ``None`` when the
    call is not a mission.
    """
    name = bare_name(tool)
    a = args if isinstance(args, dict) else {}
    if name in ("uav_mission", "mission_dry_run"):
        default = "grid_search" if name == "mission_dry_run" else None
        kind = a.get("kind", default)
        # the server lower-cases the kind before dispatching it
        return _MISSION_KIND.get(str(kind).lower()) if kind is not None else None
    return _TOOL_KIND.get(name)


def mission_vehicle(tool: str, args: dict | None) -> str | None:
    """The aircraft a mission/movement call commits (the receiver on handoff)."""
    a = args if isinstance(args, dict) else {}
    name = bare_name(tool)
    if name in ("mission_handoff_track", "uav_handoff_target"):
        v = a.get("to_vehicle")
    else:
        v = a.get("vehicle")
    return str(v) if isinstance(v, str) and v else None


def auto_track_vehicle(tool: str, args: dict | None) -> str | None:
    """Vehicle to follow after this call SUCCEEDS, or None.

    Only calls that put an aircraft in flight qualify; a threat assessment
    without ``survey`` flies nothing, and a dry run is never tracked.
    """
    name = bare_name(tool)
    a = args if isinstance(args, dict) else {}
    if a.get("dry_run") is True and name in DRY_RUN_TOOLS:
        return None
    if mission_kind(name, a) == "threat_assessment":
        return mission_vehicle(name, a) if _survey(a) else None
    if name not in _FLYING_TOOLS:
        return None
    return mission_vehicle(name, a)


_TRUE_STRINGS = frozenset({"1", "true", "t", "yes", "y", "on"})


def _survey(args: dict) -> bool:
    """Does this threat assessment fly a survey first?  Read the way the server does.

    ``mission_threat_assessment(survey=...)`` goes through pydantic's lax bool
    (``"true"``/``1`` count); ``uav_mission`` passes ``params["survey"]`` through
    plain ``bool()``, so ANY non-empty value -- even the string ``"false"`` --
    flies.  Erring toward "it flies" keeps the approval card honest.
    """
    top = args.get("survey")
    if top is True or (isinstance(top, (int, float)) and not isinstance(top, bool)
                       and top == 1):
        return True
    if isinstance(top, str) and top.strip().lower() in _TRUE_STRINGS:
        return True
    return bool(_params(args).get("survey", False))


def _parse_params(value: Any) -> dict | None:
    """``params`` as the server will see it, or None when it cannot be read."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value)
        except (ValueError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _params(args: dict) -> dict:
    """The call's ``params`` as a dict (a JSON string is parsed); ``{}`` otherwise."""
    return _parse_params(args.get("params")) or {}


def _has_lost_link_plan(args: dict) -> bool:
    if args.get("lost_link_plan") is not None:
        return True
    raw = args.get("params")
    if raw is None:
        return False
    params = _parse_params(raw)
    if params is None:
        # A params we cannot read the way the server would: a mention is enough.
        return "lost_link_plan" in str(raw)
    return params.get("lost_link_plan") is not None


# ------------------------------------------------------------------ summary --

def _num(value: Any, digits: int = 0) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if digits == 0:
        return f"{value:.0f}"
    return f"{value:.{digits}f}"


def _point(args: dict) -> str | None:
    lat, lon = args.get("lat"), args.get("lon")
    la, lo = _num(lat, 4), _num(lon, 4)
    if la is None or lo is None:
        return None
    return f"{la}, {lo}"


def _count(value: Any, noun: str, plural: str | None = None) -> str | None:
    if isinstance(value, list):
        return f"{len(value)} {noun if len(value) == 1 else (plural or noun + 's')}"
    return None


def _summary(name: str, args: dict, *, dry: bool) -> str:
    merged = {**_params(args), **args}
    parts: list[str] = []
    if name in ("mission_handoff_track", "uav_handoff_target"):
        frm, to = merged.get("from_vehicle"), merged.get("to_vehicle")
        if frm or to:
            parts.append(f"{frm or '?'} → {to or '?'}")
    elif isinstance(merged.get("vehicle"), str):
        parts.append(merged["vehicle"])
    kind = mission_kind(name, merged)
    if name in ("uav_mission", "mission_dry_run") and kind:
        parts.append(_KIND_LABEL.get(kind, kind))
    if isinstance(merged.get("track_id"), str):
        parts.append(f"track {merged['track_id']}")
    if isinstance(merged.get("mission_handle"), str):
        parts.append(merged["mission_handle"])
    for key in ("polygon", "area_polygon"):
        if isinstance(merged.get(key), list):
            parts.append(f"polygon of {len(merged[key])} points")
    if name == "mission_threat_assessment" and merged.get("area_polygon") is None:
        parts.append("whole AO")
    wp = _count(merged.get("waypoints"), "waypoint")
    if wp:
        parts.append(wp)
    pt = _point(merged)
    if pt:
        parts.append(pt)
    alt = _num(merged.get("alt_agl_m"))
    if alt is None:
        alt = _num(merged.get("alt_m"))
    if alt is not None:
        parts.append(f"{alt} m AGL")
    if _num(merged.get("radius_m")) is not None:
        parts.append(f"{_num(merged['radius_m'])} m radius")
    if _num(merged.get("laps")) is not None:
        parts.append(f"{_num(merged['laps'])} laps")
    if _num(merged.get("overlap_pct")) is not None:
        parts.append(f"{_num(merged['overlap_pct'])}% overlap")
    if _num(merged.get("forward_overlap_pct")) is not None:
        parts.append(f"{_num(merged['forward_overlap_pct'])}% forward overlap")
    if _num(merged.get("speed_mps")) is not None:
        parts.append(f"{_num(merged['speed_mps'])} m/s")
    if _num(merged.get("fov_deg")) is not None:
        parts.append(f"{_num(merged['fov_deg'])}° FOV")
    if name == "sim_set_fuel":
        pct = _num(merged.get("fuel_pct"))
        parts.append(f"fuel to {pct if pct is not None else '100'}%")
    if name == "sim_set_link_state":
        parts.append(f"link {merged.get('state') or 'lost'}")
    if _survey(args):
        parts.append("with survey flight")
    if isinstance(merged.get("query"), str):
        parts.append(f"“{merged['query'][:60]}”")
    for key in ("id", "uri"):
        if isinstance(merged.get(key), str):
            parts.append(merged[key][:80])
    if isinstance(merged.get("ids"), list):
        parts.append(_count(merged["ids"], "entity", "entities") or "")
    if _has_lost_link_plan(args):
        parts.append("replaces lost-link plan")
    if dry:
        parts.append("dry run")
    parts = [p for p in parts if p]
    if not parts and args:
        keys = ", ".join(sorted(str(k) for k in args)[:6])
        return f"Arguments: {keys}"
    return " · ".join(parts) or "No arguments"


# ------------------------------------------------------------- consequences --

def _v(args: dict, key: str = "vehicle") -> str:
    """Vehicle name for the start of a sentence."""
    v = args.get(key)
    return v if isinstance(v, str) and v else "The aircraft"


def _who(args: dict, key: str = "vehicle") -> str:
    """Vehicle name for the middle of a sentence."""
    v = args.get(key)
    return v if isinstance(v, str) and v else "the aircraft"


def _alt(args: dict, default: str) -> str:
    alt = _num(args.get("alt_agl_m"))
    if alt is None:
        alt = _num(args.get("alt_m"))
    return alt if alt is not None else default


def _mission_consequences(name: str, args: dict) -> list[str]:
    merged = {**_params(args), **args}
    kind = mission_kind(name, merged)
    v = _v(merged)
    label = _KIND_LABEL.get(kind or "", "mission")
    out = [f"{v} flies the {label} mission.", TAKEOFF_NOTE, GATE_NOTE]
    if kind == "recon_route":
        out.append("Camera captures fire along the route.")
    elif kind == "identify_target":
        out.append("The camera field of view changes during the pass.")
    elif kind == "track_target":
        out.append("The aircraft follows the contact at a server-derived standoff.")
    elif kind == "orbit_poi":
        out.append("The camera slews to track the point.")
    elif kind == "threat_assessment":
        out[0] = f"{v} flies a grid-search survey of the area, then the threat report is built."
    return out


def _consequences(name: str, args: dict, klass: str, dry: bool) -> list[str]:
    a = args
    if dry:
        out = [PLAN_NOTE]
        if mission_kind(name, a) == "threat_assessment":
            out.append("Registers a mission record and stores the report.")
        if _has_lost_link_plan(a):
            out.append(f"Replaces {_who(a)}'s live lost-link plan, even on a dry run.")
        return out
    if name == "uav_takeoff":
        return [f"Arms {_who(a)} and climbs to {_alt(a, '30')} m AGL.",
                ("Only the envelope is checked (geofence, ceiling, minimum AGL); "
                 "there is no fuel gate on takeoff.")]
    if name == "uav_land":
        return [f"{_v(a)} descends and lands where it is now."]
    if name == "uav_return_to_home":
        return [f"{_v(a)} flies to the home point and lands.",
                "Takes off first if it is on the ground away from home.",
                "Only envelope violations block it; a fuel shortfall does not."]
    if name == "uav_goto_gps":
        where = _point(a) or "the point"
        return [f"{_v(a)} flies to {where} at {_alt(a, '?')} m AGL and holds there.",
                TAKEOFF_NOTE, GATE_NOTE]
    if name == "uav_fly_route":
        n = len(a["waypoints"]) if isinstance(a.get("waypoints"), list) else "the"
        return [f"{_v(a)} flies {n} waypoints, then holds at the last one.",
                TAKEOFF_NOTE, GATE_NOTE]
    if name == "uav_hover":
        return [f"{_v(a)} holds its position.",
                "Refused as busy if the aircraft already has a task; abort interrupts it."]
    if name in ("uav_orbit_poi", "uav_mission", "mission_grid_search", "mission_recon_route",
                "mission_track_target", "mission_identify_target") and not (
            name == "uav_mission" and mission_kind(name, a) == "threat_assessment"):
        out = _mission_consequences(name, a)
    elif name == "mission_threat_assessment" or (
            name == "uav_mission" and mission_kind(name, a) == "threat_assessment"):
        if _survey(a):
            out = _mission_consequences(name, a)
        else:
            out = [("Builds a threat report from the tracks already in the store; "
                    "nothing flies."),
                   "Registers a mission record and stores the report."]
    elif name in ("mission_handoff_track", "uav_handoff_target"):
        out = [f"{_v(a, 'to_vehicle')} takes custody of the track and flies a track mission.",
               TAKEOFF_NOTE,
               "The receiver's line-of-sight check is skipped on handoff."]
    elif name == "mission_cancel":
        out = ["Cancels the mission's task and clears that aircraft's queue.",
               "The aircraft stops where it is; it does not return home by itself.",
               SAFETY_TRANSITION_NOTE]
    elif name == "uav_abort":
        out = [f"Cancels {_who(a)}'s current task, clears its queue and hovers in place.",
               SAFETY_TRANSITION_NOTE,
               "A latched BINGO stays latched."]
    elif name in ("uav_get_detections", "uav_scan_targets"):
        out = [(f"Reads {_who(a)}'s sensor and updates the contact store: sightings and "
                "pattern-of-life counts go up."),
               "Repeated scans can raise a contact's confidence."]
    elif name == "uav_capture_image":
        out = ["Captures one camera frame; the aircraft and gimbal do not move."]
    elif name == "uav_set_gimbal":
        out = [f"Slews {_who(a)}'s camera gimbal."]
    elif name == "uav_set_fov":
        fov = _num(a.get("fov_deg"))
        out = [f"Changes {_who(a)}'s camera field of view"
               + (f" to {fov}°." if fov is not None else ".")]
    elif name == "sim_set_time":
        out = ["Changes the sim clock, which moves the sun."]
    elif name == "sim_set_weather":
        out = ["Changes the sim weather and wind; detection range and fuel burn change with it."]
    elif name == "sim_spawn_target":
        out = ["Places a new ground-truth object in the sim."]
    elif name == "sim_move_target":
        out = ["Sends a sim target along a new route."]
    elif name == "sim_set_gps_degradation":
        out = ["Degrades GPS for every vehicle in the sim, not just one."]
    elif name == "sim_hydrate_real_data":
        out = ["Loads real terrain, weather and traffic for the theater; it may use the network.",
               "Pushes real weather into the sim only when apply_weather is set."]
    elif name == "sim_spawn_order_of_battle":
        out = ["Spawns sim targets at mapped real-world sites; they are not authoritative."]
    elif name == "sim_set_environment":
        out = ["Legacy tool: it always overwrites the sim wind, which defaults to zero."]
    elif name == "sim_set_fuel":
        pct = _num(a.get("fuel_pct"))
        out = [(f"Sets {_who(a)}'s fuel to {pct if pct is not None else '100'}% and clears "
                "the BINGO latch."),
               "This overrides a safety latch. A return already flying is not cancelled."]
    elif name == "sim_set_link_state":
        state = a.get("state") or "lost"
        if state == "lost":
            out = [(f"Drops {_who(a)}'s link. After a few seconds the lost-link plan runs; "
                    "by default that is a return to base that cannot be cancelled.")]
        elif state == "degraded":
            out = [f"Degrades {_who(a)}'s link."]
        else:
            out = [f"Restores {_who(a)}'s link to nominal."]
    elif name == "sim_reset":
        out = [("Resets the sim: vehicles return to their start state and in-flight "
                "tasks are dropped."),
               "Tracks, pattern of life, fuel and the BINGO latch are kept."]
    elif klass == COMMAND and name not in KNOWN_TOOLS:
        out = ["Unknown tool: its effect is not known, so it is treated as a command."]
    else:
        out = []
    if _has_lost_link_plan(a):
        out.append(f"Replaces {_who(a)}'s live lost-link plan.")
    return out


def _title(name: str, args: dict, klass: str, dry: bool) -> str:
    if name in ("uav_mission", "mission_dry_run"):
        kind = mission_kind(name, args)
        label = _KIND_LABEL.get(kind or "", "mission")
        if klass == PLAN:
            return f"Plan {label}"
        return label[:1].upper() + label[1:]
    base = _TITLES.get(name)
    if base is None:
        return f"Run {name or 'unknown tool'}"
    if dry:
        kind = mission_kind(name, args)
        return f"Plan {_KIND_LABEL.get(kind or '', base.lower())}"
    if name == "mission_threat_assessment" and _survey(args):
        return "Threat assessment with survey flight"
    return base


#: Bidi embedding/override/isolate controls. Tool arguments are model-written,
#: so one could reverse or spoof the approval text; they are stripped from it.
_BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069]")


def _nobidi(text: str) -> str:
    return _BIDI_RE.sub("", text)


def _klass(name: str, args: dict) -> tuple[str, bool]:
    """(class, is_dry_run) for a bare tool name."""
    dry = args.get("dry_run") is True and name in DRY_RUN_TOOLS
    if name in READ_TOOLS:
        return READ, False
    if name in PLAN_TOOLS:
        return (COMMAND if _has_lost_link_plan(args) else PLAN), True
    if dry:
        return (COMMAND if _has_lost_link_plan(args) else PLAN), True
    if name in SENSOR_TOOLS:
        return SENSOR, False
    if name in COMMAND_TOOLS:
        return COMMAND, False
    if name in SIM_TOOLS:
        return SIM, False
    if name in SAFETY_OVERRIDE_TOOLS:
        return SAFETY_OVERRIDE, False
    return COMMAND, False  # fail closed


def classify(tool: str, args: dict | None) -> Decision:
    """Classify one tool call.  Never raises; unknown input fails closed."""
    try:
        name = bare_name(tool)
        a = args if isinstance(args, dict) else {}
        klass, dry = _klass(name, a)
        if klass == COMMAND and name in PLAN_TOOLS | DRY_RUN_TOOLS and dry:
            # A dry run carrying a lost_link_plan: it plans, but it also
            # rewrites the live lost-link plan, so it asks like a command.
            title = _title(name, a, PLAN, True)
        else:
            title = _title(name, a, klass, dry)
        return Decision(
            klass=klass,
            auto=klass in AUTO_CLASSES,
            allow_session=klass in SESSION_CLASSES,
            title=_nobidi(title),
            summary=_nobidi(_summary(name, a, dry=dry)),
            consequences=tuple(_nobidi(c) for c in _consequences(name, a, klass, dry)),
        )
    except Exception:  # noqa: BLE001 -- a policy bug must never auto-approve
        return Decision(klass=COMMAND, auto=False, allow_session=False,
                        title=_nobidi(f"Run {bare_name(str(tool))}"),
                        summary="Unparsed arguments",
                        consequences=(("The call could not be classified, so it is "
                                       "treated as a command."),))
