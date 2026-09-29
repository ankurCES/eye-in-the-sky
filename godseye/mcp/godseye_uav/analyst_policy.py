"""Approval policy for the in-app analyst (contract §5.2). Pure; no I/O.

Every tool call the analyst makes is classified here, by tool name AND
arguments, into one of seven classes:

  read             auto   -- reads state; may write an audit row, never moves anything
  plan             auto   -- a dry run: nothing is queued and nothing moves
  sensor           ask    -- tasks or reads a sensor; writes intel (sightings, POL);
                             the operator may allow a sensor TOOL for the session
  command          ask    -- moves an aircraft or changes its mission; EVERY call asks
  sim              ask    -- changes the simulated world; EVERY call asks
  safety_override  ask    -- overrides a safety mechanism; EVERY call asks
  engagement       ask    -- rolls one simulated engagement outcome (M14a); EVERY call
                             asks, with an acknowledgement, from the console only

Session grants follow contract v1.1 §10.2: per tool, sensor class only.  (v1
also let ``sim`` be granted for a session; v1.1 withdrew that.)

``Decision.acknowledge`` marks the classes whose approval card also asks the
operator to acknowledge what they are approving (``ACKNOWLEDGE_CLASSES``:
``safety_override`` and ``engagement``; WG spec §3.6).  The chat service sends
it to the console as ``acknowledge_required``.

Simulated wargame (M14a, PLAN.md §4.5a; WG spec §3.7, §5.2.11, D2): the
``wg_*`` tools live on their own registry (``wargame_tools``).  Status, forces
and classes are ``read``; corridor plans and strike proposals are ``plan``
(nothing moves and nothing is adjudicated; a proposal only records what the
engagement will ask the operator to approve); session start and end,
scenario generation and force spawns are ``sim``.  ``wg_execute_engagement``
is the one ``engagement`` tool: it is checked FIRST, before any other rule, so
no argument can turn it into anything else, and it is never automatic and
never session-grantable.  The policy only labels it; the server enforces the
console-claimed approval path (``wargame.authorize``/``execute``).
``WG_ENTRY_TOOLS`` are the only ``wg_*`` tools the ISR toolbelt carries.

Runtime theaters (WG spec §3.7, A8): ``geo_lookup`` and ``geo_sites`` are
``read``, ``theater_propose`` is ``plan`` (it changes nothing; the server
drops undeclared arguments, so neither ``dry_run`` nor a ``lost_link_plan``
changes that), and ``sim_set_theater`` and ``sim_set_time_scale`` are ``sim``:
every call asks and none is session-grantable.  Their consequences are built
from the call's arguments only.

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
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

READ = "read"
PLAN = "plan"
SENSOR = "sensor"
COMMAND = "command"
SIM = "sim"
SAFETY_OVERRIDE = "safety_override"
#: A simulated engagement (M14a, D2): asks on every call, needs an
#: acknowledgement, is never session-grantable and never automatic.
ENGAGEMENT = "engagement"

CLASSES = (READ, PLAN, SENSOR, COMMAND, SIM, SAFETY_OVERRIDE, ENGAGEMENT)
AUTO_CLASSES = frozenset({READ, PLAN})
#: Classes whose calls an operator may allow for the rest of the session (per tool).
SESSION_CLASSES = frozenset({SENSOR})
#: Classes whose approval needs an explicit acknowledgement (WG spec §3.6).
ACKNOWLEDGE_CLASSES = frozenset({SAFETY_OVERRIDE, ENGAGEMENT})

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
    #: True when approving also needs an acknowledgement (``ACKNOWLEDGE_CLASSES``).
    acknowledge: bool = False


# --------------------------------------------------------------------------
# The explicit table.  Every tool the godseye server registers must appear in
# exactly one of these sets (tests/test_analyst_policy.py iterates the real
# server's tool list), plus the toolbelt's curated tools.
# --------------------------------------------------------------------------

#: The simulated wargame's tools (M14a; `wargame_tools.TOOL_NAMES`), by class
#: (WG spec §3.7, §5.2.11).  They are listed here and in the class sets below.
WG_READ_TOOLS = frozenset({"wg_session_status", "wg_list_forces", "wg_list_classes"})
WG_PLAN_TOOLS = frozenset({"wg_plan_corridor", "wg_propose_strike"})
WG_SIM_TOOLS = frozenset({"wg_session_start", "wg_session_end", "wg_generate_scenario",
                          "wg_spawn_force"})
#: The engagement class (D2).  Checked before every other rule in `_klass`.
ENGAGEMENT_TOOLS = frozenset({"wg_execute_engagement"})
WG_TOOLS = WG_READ_TOOLS | WG_PLAN_TOOLS | WG_SIM_TOOLS | ENGAGEMENT_TOOLS
#: The only `wg_*` tools the ISR toolbelt carries (§3.7; `wargame_tools.ENTRY_TOOLS`).
WG_ENTRY_TOOLS = frozenset({"wg_session_start", "wg_session_status", "wg_list_classes"})

SERVER_READ_TOOLS = frozenset({
    "uav_get_telemetry", "uav_list_vehicles", "uav_task_status", "mission_status",
    "uav_los_check", "uav_target_report", "uav_identify_target", "uav_assess_threat",
    "uav_list_ob_classes", "uav_real_data_status", "uav_deconflict_airspace",
    # Excluded from the toolbelt (the intel tools replace it), classified anyway
    # so the table covers every server tool.
    "uav_list_tracks",
    # Runtime theaters (WG spec §3.7): a place lookup and the mapped sites.
    "geo_lookup", "geo_sites",
})

#: The toolbelt's curated in-process tools (analyst_toolbelt.py).
CURATED_TOOLS = frozenset({
    "intel_overview", "intel_search", "intel_entity", "read_intel_resource",
    "ui_focus", "ui_track", "ui_show_orb", "ui_inspect", "ui_show_map",
})

#: Plan tools: they change nothing, so they run at once.
PLAN_TOOLS = frozenset({"mission_dry_run", "theater_propose"}) | WG_PLAN_TOOLS
#: The plan tools that read a mission ``params`` -- and so a ``lost_link_plan``,
#: which the server applies to the LIVE plan even on a dry run.
_MISSION_PLAN_TOOLS = frozenset({"mission_dry_run"})

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
    # Runtime theaters (WG spec §3.7, D2): never session-grantable.
    "sim_set_theater", "sim_set_time_scale",
}) | WG_SIM_TOOLS

SAFETY_OVERRIDE_TOOLS = frozenset({"sim_set_fuel", "sim_set_link_state", "sim_reset"})

#: Tools whose server implementation honours ``dry_run``.  Only these can be a
#: ``plan``; the server ignores undeclared arguments, so ``dry_run`` on any
#: other tool is NOT a dry run.
DRY_RUN_TOOLS = frozenset({
    "uav_mission", "uav_orbit_poi", "mission_grid_search", "mission_recon_route",
    "mission_track_target", "mission_identify_target", "mission_threat_assessment",
    "mission_handoff_track",
})

READ_TOOLS = SERVER_READ_TOOLS | CURATED_TOOLS | WG_READ_TOOLS

#: The runtime-theater tools (`theater_tools.TOOL_NAMES`).  None of them declares
#: a ``lost_link_plan``, so one passed to them changes nothing.
THEATER_TOOLS = frozenset({"geo_lookup", "geo_sites", "theater_propose",
                           "sim_set_theater", "sim_set_time_scale"})

KNOWN_TOOLS = (READ_TOOLS | PLAN_TOOLS | SENSOR_TOOLS | COMMAND_TOOLS | SIM_TOOLS
               | SAFETY_OVERRIDE_TOOLS | ENGAGEMENT_TOOLS)

#: Tools that declare no ``lost_link_plan`` (the server drops it), so the card
#: never claims one replaces the live plan.
_NO_LOST_LINK_TOOLS = THEATER_TOOLS | WG_TOOLS

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
    # WG spec §3.7; theater_tools.TITLES and format.js TOOL_TITLES match.
    "geo_lookup": "Look up a place",
    "geo_sites": "List mapped sites",
    "theater_propose": "Propose a theater",
    "sim_set_theater": "Set the theater",
    "sim_set_time_scale": "Set sim speed",
    "ui_show_map": "Show on the map",
    # Simulated wargame (M14a, WG spec §3.7); wargame_tools.TITLES matches.
    "wg_session_start": "Start a simulated wargame",
    "wg_session_status": "Wargame status",
    "wg_session_end": "End the wargame",
    "wg_generate_scenario": "Generate a scenario",
    "wg_spawn_force": "Add simulated forces",
    "wg_list_forces": "List forces",
    "wg_list_classes": "List wargame classes",
    "wg_plan_corridor": "Plan a corridor",
    "wg_propose_strike": "Propose a simulated strike",
    "wg_execute_engagement": "Execute a simulated engagement",
}

TAKEOFF_NOTE = "Takes off first if the aircraft is on the ground."
GATE_NOTE = ("The server's fuel, BINGO and geofence gate runs first and may "
             "reject it.")
SAFETY_TRANSITION_NOTE = "Refused while a BINGO or lost-link return is flying."
PLAN_NOTE = "Plan only: nothing is queued and the aircraft does not move."

#: `sim_set_theater` consequences that do not depend on the call (WG spec §4.3 A8).
THEATER_FUEL_NOTE = ("Fuel level and the BINGO latch are kept unless the airframe changes, "
                     "which gives a full tank.")
THEATER_REFUSAL_NOTE = ("Refused if any drone is airborne, busy, BINGO-latched or has lost "
                        "its link, and under real AirSim.")
THEATER_KEEPS_NOTE = ("Contacts, reports and the audit trail are kept; alarms in progress and "
                      "the old area's real data are cleared.")
#: `sim_set_time_scale` consequences (WG spec §4.3 A8).
TIME_SCALE_CLOCK_NOTE = ("Safety checks and camera captures stay on a real-time clock, so they "
                         "happen less often per simulated second.")
TIME_SCALE_NORMAL_NOTE = ("Safety checks and camera captures run at their normal rate per "
                          "simulated second.")

#: Simulated wargame consequences (WG spec §5.2.11), from the arguments only.
WG_START_NOTE = ("Starts a simulated wargame session (M14a). The analyst gets the wargame "
                 "tools; every engagement will still ask you first.")
WG_NOTHING_REAL_NOTE = ("Nothing real is fired. Scenario units are simulated and kept away "
                        "from mapped real places.")
WG_RED_ENGAGES_NOTE = ("Red air defence may down drones automatically; a downed drone's "
                       "current task is aborted and it stays down until the wargame ends.")
WG_RED_HOLDS_NOTE = "Red forces won't fire in this session."
WG_START_REFUSAL_NOTE = ("Refused under real AirSim, during a theater switch, and in theaters "
                         "not cleared for the wargame.")
WG_END_NOTES = (
    ("Ends the simulated wargame: scenario units, their contacts and waiting engagements "
     "are removed."),
    "Downed drones are restored at home, landed. The after-action review is kept as a report.",
    "Aircraft keep their current tasks.",
)
WG_GENERATE_SPACING_NOTE = ("Units are kept at least 500 m from mapped places and theater "
                            "points and, for red, 1 km from home.")
WG_GENERATE_REFUSAL_NOTE = "Refused if the area has no room for them."
WG_SPAWN_REFUSAL_NOTE = ("Refused within 500 m of a mapped place or theater point, within 1 km "
                         "of home (red), or within 200 m of another unit.")
WG_ENGAGEMENT_NOTES = (
    "Rolls one simulated outcome for this engagement against a scenario unit.",
    "Nothing real is fired.",
    "The outcome stands for the rest of this wargame; only ending the wargame clears it.",
    "In blue view the outcome stays hidden until a re-look assesses damage.",
)
WG_PROPOSE_NOTE = ("Plan only: records a proposed simulated strike; the engagement itself "
                   "asks you separately.")
#: Template keys (`wargame_tables.TEMPLATES`) as words for the approval card.
_WG_TEMPLATE_WORDS = {"air_defence_belt": "air-defence belt", "mech_advance": "mechanised advance",
                      "strike_exercise": "strike exercise"}
#: Longest model-written wargame id or key an approval text quotes.
_WG_ID_MAX = 40


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


# ----------------------------------------------------------------- theaters --

#: Metres per degree of latitude; the same constant `theaters.py` builds a
#: dynamic AO with, so a {W} x {H} read back from the AO bounds is exact.
_M_PER_DEG = 111_320.0
#: Longest theater label an approval sentence quotes (WG spec §3.7: <= 60).
_LABEL_MAX = 60


def _finite(value: Any) -> float | None:
    """``value`` as a finite float, or None (bools are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _clean_label(value: Any, limit: int = _LABEL_MAX) -> str | None:
    """A model-written theater label as one short line of plain text, or None.

    Control and format characters (bidi controls included) are dropped and
    whitespace is collapsed; the console renders the result as text only.
    """
    if not isinstance(value, str):
        return None
    kept = "".join(" " if ch.isspace() else ch for ch in value
                   if ch.isspace() or not unicodedata.category(ch).startswith("C"))
    text = " ".join(kept.split())
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text or None


def _theater_name(args: dict) -> str | None:
    """The label a theater call names: ``label``, else ``theater_id``."""
    return _clean_label(args.get("label")) or _clean_label(args.get("theater_id"))


def _ao_bounds(ao: Any) -> tuple[float, float, float, float] | None:
    """``(s, w, n, e)`` of an ``ao`` list of ``[lat, lon]`` vertices, or None."""
    if not isinstance(ao, (list, tuple)) or len(ao) < 3:
        return None
    lats: list[float] = []
    lons: list[float] = []
    for vertex in ao:
        if not isinstance(vertex, (list, tuple)) or len(vertex) != 2:
            return None
        lat, lon = _finite(vertex[0]), _finite(vertex[1])
        if lat is None or lon is None or abs(lat) > 90.0 or abs(lon) > 180.0:
            return None
        lats.append(lat)
        lons.append(lon)
    return min(lats), min(lons), max(lats), max(lons)


def _ao_size(bounds: tuple[float, float, float, float]) -> tuple[str, str, str, str]:
    """``(W km, H km, centre lat, centre lon)`` of AO bounds, as display text."""
    s, w, n, e = bounds
    mid = (s + n) / 2.0
    width_km = (e - w) * _M_PER_DEG * math.cos(math.radians(mid)) / 1000.0
    height_km = (n - s) * _M_PER_DEG / 1000.0
    return (f"{width_km:.1f}", f"{height_km:.1f}", f"{mid:.5f}", f"{(w + e) / 2.0:.5f}")


def _coords(lat: Any, lon: Any, digits: int = 5) -> str | None:
    la, lo = _finite(lat), _finite(lon)
    if la is None or lo is None:
        return None
    return f"{la:.{digits}f}, {lo:.{digits}f}"


def _scale_text(value: Any) -> str | None:
    """A sim speed as short text (``10``, ``2.5``), or None when not a number."""
    s = _finite(value)
    return None if s is None else f"{s:g}"


def _theater_summary(name: str, args: dict) -> str | None:
    """The one-line digest for a theater tool, or None to use the generic one."""
    parts: list[str | None] = []
    if name == "sim_set_theater":
        parts.append(_theater_name(args))
        bounds = _ao_bounds(args.get("ao"))
        if bounds is not None:
            w_km, h_km, _, _ = _ao_size(bounds)
            parts.append(f"{w_km} × {h_km} km")
    elif name == "sim_set_time_scale":
        scale = _scale_text(args.get("scale"))
        parts.append(f"×{scale}" if scale is not None else None)
    elif name == "theater_propose":
        parts.append(_theater_name(args) or _clean_label(args.get("place")))
        parts.append(_coords(args.get("lat"), args.get("lon"), 4))
        query = _clean_label(args.get("query"))
        parts.append(f"“{query}”" if query else None)
        half = _num(_finite(args.get("half_extent_m")))
        parts.append(f"{half} m half-extent" if half is not None else None)
        parts.append(_clean_label(args.get("airframe"), 40))
    elif name == "geo_sites":
        parts.append(_clean_label(args.get("category"), 40))
        near = _coords(args.get("near_lat"), args.get("near_lon"), 4)
        parts.append(f"near {near}" if near else None)
        parts.append("refresh" if args.get("refresh") is True else None)
    else:
        return None
    text = " · ".join(p for p in parts if p)
    return text[:200] or None


# ------------------------------------------------------------------ wargame --

def _wg_text(value: Any) -> str | None:
    """A model-written wargame id, key or word as one short line of plain text."""
    return _clean_label(value, _WG_ID_MAX)


def _wg_class_label(key: Any) -> str | None:
    """The generic label of a wargame class key (``wargame_tables.CLASSES``),
    lower-cased for the middle of a sentence; an unknown key is shown as text.

    Lazy import, so the policy stays importable (and pure) on its own; a
    missing table only costs the nicer wording.
    """
    text = _wg_text(key)
    if text is None:
        return None
    try:
        from .wargame_tables import CLASSES
        label = getattr(CLASSES.get(text), "label", None)
    except Exception:  # noqa: BLE001 -- wording only; never fails the card
        label = None
    if not isinstance(label, str) or not label:
        return text
    return label[:1].lower() + label[1:]


def _wg_template(value: Any) -> str | None:
    text = _wg_text(value)
    return _WG_TEMPLATE_WORDS.get(text or "", text)


def _wg_count(value: Any) -> str | None:
    """``count`` as text; the tool's default is 1."""
    if value is None:
        return "1"
    n = _finite(value)
    return None if n is None else f"{n:g}"


def _wg_summary(name: str, args: dict) -> str | None:
    """The one-line digest for a ``wg_*`` tool, or None to use the generic one."""
    parts: list[str | None] = []
    if name == "wg_session_start":
        seed = _num(_finite(args.get("seed")))
        parts.append(f"seed {seed}" if seed is not None else None)
        parts.append("red holds fire" if args.get("red_engages") is False else "red may fire")
        parts.append("red revealed to the planner" if args.get("reveal_red") is True else None)
    elif name == "wg_session_status":
        events = _num(_finite(args.get("events")))
        parts.append(f"{events} events" if events is not None else None)
    elif name == "wg_list_forces":
        parts.append(_wg_text(args.get("side")))
    elif name == "wg_generate_scenario":
        parts.append(_wg_template(args.get("template")))
        parts.append(_wg_text(args.get("intensity")))
        parts.append(_wg_class_label(args.get("ad_class")))
    elif name == "wg_spawn_force":
        parts.append(_wg_text(args.get("side")))
        parts.append(_wg_class_label(args.get("wg_class")))
        count = _wg_count(args.get("count")) if "count" in args else None
        parts.append(f"×{count}" if count else None)
        parts.append(_coords(args.get("lat"), args.get("lon"), 4))
    elif name in ("wg_plan_corridor", "wg_propose_strike", "wg_execute_engagement"):
        who = _wg_text(args.get("vehicle" if name == "wg_plan_corridor" else "shooter_id"))
        track = _wg_text(args.get("target_track_id"))
        if who or track:
            parts.append(f"{who or '?'} → track {track or '?'}")
        if name == "wg_plan_corridor":
            alt = _num(_finite(args.get("alt_agl_m")))
            parts.append(f"{alt} m AGL" if alt is not None else None)
            if args.get("relook") is True:
                radius = _num(_finite(args.get("relook_radius_m")))
                parts.append(f"re-look, {radius} m radius" if radius else "re-look")
        elif name == "wg_execute_engagement":
            parts.append(_wg_text(args.get("pending_id")))
    else:
        return None
    text = " · ".join(p for p in parts if p)
    return text[:200] or None


def _wg_consequences(name: str, args: dict) -> list[str]:
    """WG spec §5.2.11: from the arguments only.  "Red holds fire" only for a
    literal ``red_engages: false``; anything else warns that red may fire."""
    if name == "wg_session_start":
        red = WG_RED_HOLDS_NOTE if args.get("red_engages") is False else WG_RED_ENGAGES_NOTE
        return [WG_START_NOTE, WG_NOTHING_REAL_NOTE, red, WG_START_REFUSAL_NOTE]
    if name == "wg_session_end":
        return list(WG_END_NOTES)
    if name == "wg_generate_scenario":
        template = _wg_template(args.get("template")) or "chosen"
        intensity = _wg_text(args.get("intensity")) or "medium"
        return [f"Places simulated scenario units for the {template} template ({intensity}).",
                WG_GENERATE_SPACING_NOTE, WG_GENERATE_REFUSAL_NOTE]
    if name == "wg_spawn_force":
        who = " ".join(p for p in (_wg_count(args.get("count")), "simulated",
                                   _wg_text(args.get("side")),
                                   _wg_class_label(args.get("wg_class")) or "units") if p)
        where = _coords(args.get("lat"), args.get("lon"), 4) or "the given point"
        return [f"Adds {who} near {where}.", WG_SPAWN_REFUSAL_NOTE]
    if name in ENGAGEMENT_TOOLS:
        return list(WG_ENGAGEMENT_NOTES)
    if name == "wg_plan_corridor":
        return [PLAN_NOTE]
    if name == "wg_propose_strike":
        return [WG_PROPOSE_NOTE]
    return []


def _summary(name: str, args: dict, *, dry: bool) -> str:
    theater = _theater_summary(name, args)
    if theater:
        return theater
    wargame = _wg_summary(name, args)
    if wargame:
        return wargame
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
    if _has_lost_link_plan(args) and name not in _NO_LOST_LINK_TOOLS:
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


def _set_theater_consequences(args: dict) -> list[str]:
    """WG spec §4.3 A8: from the arguments only; {W} x {H} from the AO bounds."""
    label = _theater_name(args)
    bounds = _ao_bounds(args.get("ao"))
    if bounds is not None:
        w_km, h_km, lat, lon = _ao_size(bounds)
        area = f"a {w_km} × {h_km} km area around {lat}, {lon}"
        first = (f"Moves the simulation to {label}: {area}." if label
                 else f"Moves the simulation to {area}.")
    else:
        first = (f"Moves the simulation to {label}." if label
                 else "Moves the simulation to a new theater.")
    home = _coords(args.get("home_lat"), args.get("home_lon"))
    where = f"the new home ({home})" if home else "the new home"
    return [first,
            f"Every drone is parked, landed, at {where}; the old area's geofence stops applying.",
            THEATER_FUEL_NOTE, THEATER_REFUSAL_NOTE, THEATER_KEEPS_NOTE]


def _time_scale_consequences(args: dict) -> list[str]:
    """WG spec §4.3 A8.  At x1 the "faster" sentences would be false, so the
    card says the simulator returns to normal speed instead."""
    scale = _scale_text(args.get("scale"))
    if _finite(args.get("scale")) == 1.0:
        return ["Runs the fake simulator at normal speed (physics, fuel, sun).",
                TIME_SCALE_NORMAL_NOTE]
    speed = f"{scale}× faster" if scale is not None else "at a new speed"
    return [(f"Runs the fake simulator {speed} (physics, fuel, sun). "
             "Link-loss timers stay in wall-clock seconds."),
            TIME_SCALE_CLOCK_NOTE]


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
    elif name == "sim_set_theater":
        out = _set_theater_consequences(a)
    elif name == "sim_set_time_scale":
        out = _time_scale_consequences(a)
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
    elif name in WG_TOOLS:
        out = _wg_consequences(name, a)
    elif klass == COMMAND and name not in KNOWN_TOOLS:
        out = ["Unknown tool: its effect is not known, so it is treated as a command."]
    else:
        out = []
    if _has_lost_link_plan(a) and name not in _NO_LOST_LINK_TOOLS:
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
    if name in ENGAGEMENT_TOOLS:
        # First, before every other rule: no argument makes an engagement anything else.
        return ENGAGEMENT, False
    dry = args.get("dry_run") is True and name in DRY_RUN_TOOLS
    if name in READ_TOOLS:
        return READ, False
    if name in _MISSION_PLAN_TOOLS:
        return (COMMAND if _has_lost_link_plan(args) else PLAN), True
    if name in PLAN_TOOLS:
        # Not a mission dry run: it declares no lost_link_plan or dry_run, and
        # the server drops undeclared arguments.
        return PLAN, False
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
            acknowledge=klass in ACKNOWLEDGE_CLASSES,
        )
    except Exception:  # noqa: BLE001 -- a policy bug must never auto-approve
        name = bare_name(str(tool))
        if name in ENGAGEMENT_TOOLS:
            # Fail closed means the STRICTER class: an engagement keeps its
            # acknowledgement and the console-only approval path (D2).
            return Decision(klass=ENGAGEMENT, auto=False, allow_session=False,
                            title=_TITLES[name], summary="Unparsed arguments",
                            consequences=WG_ENGAGEMENT_NOTES, acknowledge=True)
        return Decision(klass=COMMAND, auto=False, allow_session=False,
                        title=_nobidi(f"Run {name}"),
                        summary="Unparsed arguments",
                        consequences=(("The call could not be classified, so it is "
                                       "treated as a command."),))
