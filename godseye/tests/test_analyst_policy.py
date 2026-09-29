"""analyst_policy.classify: the approval gate for the in-app analyst (contract §5.2).

The policy must FAIL CLOSED and must cover every tool the real server
registers, so this file builds the real GodseyeUavServer in-process (no sim
needed just to list tools) and iterates its catalog -- and its never-mounted
simulated wargame registry (`srv.wargame_mcp`, M14a; WG spec §5.2.11, §6).
"""
import asyncio
import dataclasses
import math
import pathlib
import re

import pytest
from godseye_uav import analyst_policy as pol
from godseye_uav import theater_tools
from godseye_uav import wargame_tools as wargame_registry
from godseye_uav.analyst_policy import (
    COMMAND,
    DRY_RUN_TOOLS,
    ENGAGEMENT,
    KNOWN_TOOLS,
    PLAN,
    READ,
    SAFETY_OVERRIDE,
    SENSOR,
    SIM,
    STATIC_AUTO_TOOLS,
    TOOL_PREFIX,
    Decision,
    auto_track_vehicle,
    bare_name,
    classify,
    mission_kind,
    mission_vehicle,
)
from godseye_uav.server import GodseyeUavServer
from godseye_uav.store import Store

#: The one tool the host-side port adds (PY-INTEL, contract §9); it may not be
#: registered yet in this checkout, but the policy must already know it.
PORTED_TOOLS = {"sim_set_fuel"}

EXPECTED_CLASS = {
    # read
    **{t: READ for t in (
        "uav_get_telemetry", "uav_list_vehicles", "uav_task_status", "mission_status",
        "uav_los_check", "uav_target_report", "uav_identify_target", "uav_assess_threat",
        "uav_list_ob_classes", "uav_real_data_status", "uav_deconflict_airspace",
        "uav_list_tracks", "intel_overview", "intel_search", "intel_entity",
        "read_intel_resource", "ui_focus", "ui_track", "ui_show_orb", "ui_inspect",
        # runtime theaters (WG spec §3.7, A8)
        "geo_lookup", "geo_sites", "ui_show_map",
        # simulated wargame (M14a, WG spec §5.2.11, B7)
        "wg_session_status", "wg_list_forces", "wg_list_classes")},
    "mission_dry_run": PLAN,
    "theater_propose": PLAN,
    "wg_plan_corridor": PLAN,
    "wg_propose_strike": PLAN,
    **{t: SENSOR for t in ("uav_get_detections", "uav_scan_targets", "uav_capture_image",
                           "uav_set_gimbal", "uav_set_fov")},
    **{t: COMMAND for t in (
        "uav_takeoff", "uav_land", "uav_return_to_home", "uav_goto_gps", "uav_fly_route",
        "uav_hover", "uav_orbit_poi", "uav_mission", "mission_grid_search",
        "mission_recon_route", "mission_track_target", "mission_identify_target",
        "mission_threat_assessment", "mission_handoff_track", "uav_handoff_target",
        "mission_cancel", "uav_abort")},
    **{t: SIM for t in ("sim_set_time", "sim_set_weather", "sim_spawn_target",
                        "sim_move_target", "sim_set_gps_degradation", "sim_hydrate_real_data",
                        "sim_spawn_order_of_battle", "sim_set_environment",
                        "sim_set_theater", "sim_set_time_scale",
                        "wg_session_start", "wg_session_end", "wg_generate_scenario",
                        "wg_spawn_force")},
    **{t: SAFETY_OVERRIDE for t in ("sim_set_fuel", "sim_set_link_state", "sim_reset")},
    "wg_execute_engagement": ENGAGEMENT,
}

MOVEMENT_AND_MISSION = (
    "uav_goto_gps", "uav_fly_route", "uav_orbit_poi", "uav_mission", "mission_grid_search",
    "mission_recon_route", "mission_track_target", "mission_identify_target",
    "mission_handoff_track", "uav_handoff_target")


@pytest.fixture(scope="module")
def catalogs(tmp_path_factory):
    """``({name: Tool}, {name: Tool})``: every tool the real server registers on
    /mcp, and every tool of its never-mounted wargame registry (M14a)."""
    store = Store(tmp_path_factory.mktemp("policy-store"))
    try:
        srv = GodseyeUavServer(None, store)
        tools = asyncio.run(srv.mcp.list_tools())
        wargame = asyncio.run(srv.wargame_mcp.list_tools())
        return {t.name: t for t in tools}, {t.name: t for t in wargame}
    finally:
        store.close()


@pytest.fixture(scope="module")
def server_tools(catalogs):
    """``{name: Tool}`` for every tool the real server registers."""
    return catalogs[0]


@pytest.fixture(scope="module")
def wargame_tools(catalogs):
    """``{name: Tool}`` for every tool on `srv.wargame_mcp`."""
    return catalogs[1]


@pytest.fixture(scope="module")
def server_tool_names(server_tools):
    return sorted(server_tools)


# ------------------------------------------------------------- coverage --

def test_every_real_server_tool_has_an_explicit_classification(server_tool_names):
    assert len(server_tool_names) >= 45
    missing = [t for t in server_tool_names if t not in KNOWN_TOOLS]
    assert not missing, f"tools the policy does not classify explicitly: {missing}"
    for name in server_tool_names:
        assert name in EXPECTED_CLASS, f"test table lacks {name}"
        assert classify(name, {}).klass == EXPECTED_CLASS[name], name


def test_ported_and_curated_tools_are_classified():
    for name in PORTED_TOOLS | pol.CURATED_TOOLS:
        assert name in KNOWN_TOOLS
        assert classify(name, {}).klass == EXPECTED_CLASS[name]


def test_expected_table_matches_policy_exactly():
    for name, klass in EXPECTED_CLASS.items():
        d = classify(name, {})
        assert d.klass == klass, name
        assert d.auto is (klass in (READ, PLAN)), name
        assert d.allow_session is (klass == SENSOR), name
        assert d.acknowledge is (klass in (SAFETY_OVERRIDE, ENGAGEMENT)), name
    assert set(EXPECTED_CLASS) == set(KNOWN_TOOLS)


# ------------------------------------------------------------ fail closed --

@pytest.mark.parametrize("tool", ["Bash", "Read", "WebFetch", "uav_teleport", "intel_delete",
                                  "ui_launch", "", "mcp__other__uav_get_telemetry",
                                  "mcp__godseye-uav__uav_list_vehicles", "mcp__godseye__"])
def test_unknown_tools_fail_closed_as_command(tool):
    d = classify(tool, {"vehicle": "Drone1"})
    assert d.klass == COMMAND
    assert d.auto is False
    assert d.allow_session is False
    assert d.title and d.consequences


@pytest.mark.parametrize("args", [None, [], "x", 5, {"dry_run": "true"}])
def test_odd_args_never_raise_and_never_widen(args):
    d = classify("mission_grid_search", args)
    assert d.klass == COMMAND and d.auto is False
    assert classify("uav_get_telemetry", args).klass == READ


def test_classify_never_raises_even_on_internal_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(pol, "_summary", boom)
    d = classify("uav_get_telemetry", {})
    assert d.klass == COMMAND and d.auto is False and d.allow_session is False


def test_prefix_is_stripped_only_for_the_godseye_server():
    assert bare_name(f"{TOOL_PREFIX}uav_takeoff") == "uav_takeoff"
    assert bare_name("mcp__other__uav_takeoff") == "mcp__other__uav_takeoff"
    assert classify(f"{TOOL_PREFIX}uav_get_telemetry", {}).klass == READ
    assert classify(f"{TOOL_PREFIX}mission_dry_run", {"vehicle": "Drone1"}).klass == PLAN


# --------------------------------------------------------------- dry run --

@pytest.mark.parametrize("tool", sorted(DRY_RUN_TOOLS))
def test_dry_run_true_is_a_plan(tool):
    d = classify(tool, {"vehicle": "Drone1", "from_vehicle": "Drone1",
                        "to_vehicle": "Drone2", "dry_run": True})
    assert d.klass == PLAN and d.auto is True and d.allow_session is False
    assert d.title.startswith("Plan ")
    assert "dry run" in d.summary
    assert any("nothing is queued" in c for c in d.consequences)


@pytest.mark.parametrize("tool", sorted(DRY_RUN_TOOLS | {"mission_dry_run"}))
@pytest.mark.parametrize("where", ["top", "params"])
def test_lost_link_plan_makes_even_a_dry_run_a_command(tool, where):
    plan = {"behaviour": "hold_orbit"}
    args = {"vehicle": "Drone1", "dry_run": True}
    if where == "top":
        args["lost_link_plan"] = plan
    else:
        args["params"] = {"lost_link_plan": plan}
    d = classify(tool, args)
    assert d.klass == COMMAND and d.auto is False and d.allow_session is False
    assert any("lost-link plan" in c for c in d.consequences)
    assert not any("Takes off" in c for c in d.consequences)  # still a dry run: nothing flies


def test_null_lost_link_plan_does_not_escalate():
    d = classify("mission_grid_search", {"vehicle": "Drone1", "dry_run": True,
                                         "lost_link_plan": None})
    assert d.klass == PLAN


@pytest.mark.parametrize("tool", ["mission_cancel", "uav_takeoff", "uav_goto_gps", "uav_abort",
                                  "uav_handoff_target", "sim_reset", "uav_scan_targets",
                                  "sim_set_fuel"])
def test_dry_run_on_a_tool_that_ignores_it_is_not_a_plan(tool):
    # The server silently drops undeclared arguments, so mission_cancel(dry_run=True)
    # would really cancel.  The policy must not believe the flag.
    real = classify(tool, {"vehicle": "Drone1"}).klass
    d = classify(tool, {"vehicle": "Drone1", "mission_handle": "MSN-1", "dry_run": True})
    assert d.klass == real and d.auto is False


def test_dry_run_must_be_literally_true():
    for value in ("true", 1, "yes", None, False):
        assert classify("mission_grid_search", {"vehicle": "D", "dry_run": value}).klass \
            == COMMAND


def test_mission_dry_run_is_a_plan_without_lost_link():
    d = classify("mission_dry_run", {"vehicle": "Drone1", "kind": "recon_route"})
    assert d.klass == PLAN and d.auto
    assert d.title == "Plan route recon"


# ---------------------------------------------------------- session scope --

def test_only_sensor_tools_may_be_allowed_for_the_session():
    # Contract v1.1 §10.2: grants are per tool and sensor-only (v1 also allowed sim).
    for name, klass in EXPECTED_CLASS.items():
        if klass == SENSOR:
            d = classify(name, {"vehicle": "Drone1"})
            assert d.allow_session is True and d.auto is False, name


def test_command_sim_override_and_engagement_are_never_session_approvable():
    for name, klass in EXPECTED_CLASS.items():
        if klass in (COMMAND, SIM, SAFETY_OVERRIDE, ENGAGEMENT):
            for args in ({}, {"vehicle": "Drone1"}, {"vehicle": "Drone1", "survey": True}):
                d = classify(name, args)
                assert d.allow_session is False and d.auto is False, name


# --------------------------------------------------------- static allowlist --

def test_static_auto_tools_hold_only_arg_independent_reads():
    forbidden = {n for n, k in EXPECTED_CLASS.items() if k in (PLAN, SENSOR, COMMAND, SIM,
                                                               SAFETY_OVERRIDE, ENGAGEMENT)}
    assert not (STATIC_AUTO_TOOLS & forbidden)
    assert "mission_dry_run" not in STATIC_AUTO_TOOLS  # arg-dependent: lost_link_plan
    assert not (STATIC_AUTO_TOOLS & DRY_RUN_TOOLS)
    adversarial = [{}, {"dry_run": True}, {"dry_run": False}, {"survey": True},
                   {"lost_link_plan": {"behaviour": "rtb"}},
                   {"params": {"lost_link_plan": {}}}, {"state": "lost"}]
    for name in STATIC_AUTO_TOOLS:
        for args in adversarial:
            d = classify(name, args)
            assert d.klass == READ and d.auto is True, (name, args)
    assert all(not n.startswith("mcp__") for n in STATIC_AUTO_TOOLS)


# ----------------------------------------------------------- human text --

def _sentence_case(text: str) -> bool:
    return bool(text) and text[0].isupper() and not text.isupper()


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASS))
def test_titles_summaries_and_consequences_are_human(name):
    d = classify(name, {"vehicle": "Drone1", "lat": 47.64, "lon": -122.14, "alt_agl_m": 60,
                        "from_vehicle": "Drone1", "to_vehicle": "Drone2",
                        "track_id": "TRK-ab12-0001", "mission_handle": "MSN-1a2b3c4d"})
    assert isinstance(d, Decision)
    assert dataclasses.is_dataclass(d)
    assert _sentence_case(d.title), d.title
    assert not d.title.endswith("."), d.title
    assert len(d.title) <= 48, d.title
    assert d.summary and len(d.summary) <= 200
    assert "_" not in d.title, d.title
    assert isinstance(d.consequences, tuple)
    if d.klass in (COMMAND, SENSOR, SIM, SAFETY_OVERRIDE, ENGAGEMENT):
        assert d.consequences, name
    for c in d.consequences:
        assert _sentence_case(c) and c.endswith("."), c
        assert len(c) <= 160, c


def test_decision_is_frozen():
    d = classify("uav_land", {"vehicle": "Drone1"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.klass = READ  # type: ignore[misc]


@pytest.mark.parametrize("tool", MOVEMENT_AND_MISSION)
def test_movement_and_mission_tools_say_they_take_off(tool):
    d = classify(tool, {"vehicle": "Drone1", "to_vehicle": "Drone2", "kind": "grid_search"})
    assert d.klass == COMMAND
    assert any("take" in c.lower() and "off" in c.lower() for c in d.consequences), d


def test_survey_threat_assessment_flies_and_plain_one_does_not():
    flying = classify("mission_threat_assessment", {"vehicle": "Drone1", "survey": True})
    assert any("Takes off" in c for c in flying.consequences)
    assert "survey" in flying.title
    still = classify("mission_threat_assessment", {"vehicle": "Drone1"})
    assert still.klass == COMMAND  # contract §5.2: not dry -> command
    assert any("nothing flies" in c for c in still.consequences)


def test_rth_and_takeoff_wording():
    rth = classify("uav_return_to_home", {"vehicle": "Drone1"})
    assert any("home point and lands" in c for c in rth.consequences)
    assert any("fuel shortfall does not" in c for c in rth.consequences)
    up = classify("uav_takeoff", {"vehicle": "Drone1", "alt_agl_m": 40})
    assert any("40 m AGL" in c for c in up.consequences)
    assert any("no fuel gate" in c for c in up.consequences)


def test_abort_says_it_hovers_and_is_refused_during_safety_rtb():
    d = classify("uav_abort", {"vehicle": "Drone2"})
    text = " ".join(d.consequences)
    assert "hovers in place" in text and "Drone2" in text
    assert "Refused while a BINGO" in text
    cancel = " ".join(classify("mission_cancel", {"mission_handle": "MSN-1"}).consequences)
    assert "does not return home" in cancel


def test_refuel_says_it_clears_the_bingo_latch():
    d = classify("sim_set_fuel", {"vehicle": "Drone1", "fuel_pct": 80})
    assert d.klass == SAFETY_OVERRIDE and d.allow_session is False
    assert any("clears the BINGO latch" in c and "80%" in c for c in d.consequences)


def test_link_loss_says_the_lost_link_plan_runs_default_rtb():
    d = classify("sim_set_link_state", {"vehicle": "Drone1"})  # state defaults to lost
    text = " ".join(d.consequences)
    assert "lost-link plan runs" in text and "return to base" in text
    assert "restores" in " ".join(
        classify("sim_set_link_state", {"vehicle": "Drone1", "state": "nominal"}).consequences
    ).lower()


def test_sim_reset_keeps_tracks_and_bingo():
    text = " ".join(classify("sim_reset", {}).consequences)
    assert "in-flight tasks are dropped" in text and "BINGO latch are kept" in text


def test_summary_is_a_one_line_human_args_digest():
    d = classify("mission_grid_search", {"vehicle": "Drone1", "alt_agl_m": 60,
                                         "polygon": [[1, 2], [3, 4], [5, 6], [1, 2]],
                                         "overlap_pct": 30, "dry_run": True})
    assert d.summary == "Drone1 · polygon of 4 points · 60 m AGL · 30% overlap · dry run"
    assert "\n" not in d.summary
    assert classify("mission_handoff_track", {"from_vehicle": "Drone1", "to_vehicle": "Drone2",
                                              "track_id": "TRK-x-0001"}).summary \
        == "Drone1 → Drone2 · track TRK-x-0001"


# ------------------------------------------------------- mission helpers --

def test_mission_kind_pairs_dry_runs_with_live_calls():
    assert mission_kind("mission_dry_run", {}) == "grid_search"
    assert mission_kind("mission_dry_run", {"kind": "identify"}) == "identify_target"
    assert mission_kind("uav_mission", {"kind": "orbit_poi"}) == "orbit_poi"
    assert mission_kind("uav_mission", {"kind": "Grid_Search"}) == "grid_search"
    assert mission_kind("mission_dry_run", {"kind": "assess"}) == "assess"
    assert mission_kind("uav_mission", {}) is None
    assert mission_kind("mission_identify_target", {}) == "identify_target"
    assert mission_kind("uav_orbit_poi", {}) == "orbit_poi"
    assert mission_kind("uav_takeoff", {}) is None
    assert mission_vehicle("mission_handoff_track", {"from_vehicle": "A", "to_vehicle": "B"}) == "B"
    assert mission_vehicle("mission_grid_search", {"vehicle": "Drone1"}) == "Drone1"


def test_auto_track_only_for_calls_that_fly():
    assert auto_track_vehicle("mission_grid_search", {"vehicle": "Drone1"}) == "Drone1"
    assert auto_track_vehicle(f"{TOOL_PREFIX}uav_goto_gps", {"vehicle": "Drone2"}) == "Drone2"
    assert auto_track_vehicle("mission_handoff_track", {"to_vehicle": "Drone2"}) == "Drone2"
    assert auto_track_vehicle("mission_grid_search", {"vehicle": "D", "dry_run": True}) is None
    assert auto_track_vehicle("mission_threat_assessment", {"vehicle": "D"}) is None
    assert auto_track_vehicle("mission_threat_assessment", {"vehicle": "D", "survey": True}) == "D"
    for tool in ("uav_land", "uav_hover", "uav_abort", "mission_cancel", "sim_set_fuel",
                 "uav_get_telemetry"):
        assert auto_track_vehicle(tool, {"vehicle": "D"}) is None, tool
    # the legacy dispatcher: a threat assessment without a survey flies nothing, while
    # the legacy point "assess" kind is a flight
    assert auto_track_vehicle("uav_mission", {"vehicle": "D",
                                              "kind": "threat_assessment"}) is None
    assert auto_track_vehicle("uav_mission", {"vehicle": "D", "kind": "assess"}) == "D"
    assert auto_track_vehicle("uav_mission", {"vehicle": "D", "kind": "threat_assessment",
                                              "params": {"survey": "false"}}) == "D"
    assert auto_track_vehicle("mission_threat_assessment", {"vehicle": "D",
                                                            "survey": "true"}) == "D"
    assert auto_track_vehicle("uav_mission", {"vehicle": "D", "kind": "threat_assessment",
                                              "params": {"survey": True}}) == "D"
    assert auto_track_vehicle("uav_mission", {"vehicle": "D", "kind": "grid_search"}) == "D"


def test_legacy_dispatcher_threat_assessment_wording():
    point = classify("uav_mission", {"vehicle": "Drone1", "kind": "ASSESS"})
    assert point.title == "Point assessment"
    assert any("Takes off" in c for c in point.consequences)
    plain = classify("uav_mission", {"vehicle": "Drone1", "kind": "threat_assessment"})
    assert plain.klass == COMMAND
    assert any("nothing flies" in c for c in plain.consequences)
    flying = classify("uav_mission", {"vehicle": "Drone1", "kind": "threat_assessment",
                                      "params": {"survey": True}})
    assert any("Takes off" in c for c in flying.consequences)
    assert "with survey flight" in flying.summary


def test_focus_summary_pluralizes_entities():
    """Seen live: the ui_focus tool row read "3 entitys"."""
    assert classify("ui_focus", {"ids": ["veh:D", "trk:T-1", "trk:T-2"]}).summary == "3 entities"
    assert classify("ui_focus", {"ids": ["veh:D"]}).summary == "1 entity"


# ------------------------------------------------- params as a JSON string --

_LLP = {"behaviour": "continue", "escalate_to_rtb_after_s": 1e9}
_POLY = [[47.63, -122.16], [47.63, -122.12], [47.66, -122.12]]


@pytest.mark.parametrize("tool,extra", [("mission_dry_run", {"kind": "grid_search"}),
                                        ("uav_mission", {"kind": "grid_search",
                                                         "dry_run": True})])
def test_a_lost_link_plan_inside_json_string_params_is_a_command(tool, extra, tmp_path):
    """Review finding: FastMCP accepts ``params`` as a JSON STRING and parses it,
    and the server rewrites the LIVE lost-link plan before it looks at dry_run.
    The policy classified that call as an auto-approved plan (it only looked
    inside a dict ``params``). Checked against the real server: the same args
    really do rewrite the plan, and classify() asks for approval."""
    import json

    args = {"vehicle": "Drone1", **extra,
            "params": json.dumps({"polygon": _POLY, "lost_link_plan": _LLP})}
    d = classify(tool, args)
    assert d.klass == COMMAND and d.auto is False and d.allow_session is False
    assert any("lost-link plan" in c for c in d.consequences)
    assert "replaces lost-link plan" in d.summary

    store = Store(tmp_path / "store")
    try:
        srv = GodseyeUavServer(None, store)

        async def call() -> tuple[dict, dict]:
            before = srv.monitor_for("Drone1").link.plan.to_dict()
            await srv.mcp.call_tool(tool, dict(args))
            return before, srv.monitor_for("Drone1").link.plan.to_dict()

        before, after = asyncio.run(call())
    finally:
        store.close()
    assert before["behaviour"] == "rtb" and after["behaviour"] == "continue"  # the premise


def test_string_params_without_a_lost_link_plan_stay_a_plan():
    import json

    d = classify("mission_dry_run", {"vehicle": "Drone1", "kind": "grid_search",
                                     "params": json.dumps({"polygon": _POLY})})
    assert d.klass == PLAN and d.auto is True


@pytest.mark.parametrize("raw", ['{"lost_link_plan": {"behaviour": "continue"', "[1, 2]",
                                 "lost_link_plan=continue"])
def test_unreadable_string_params_mentioning_a_lost_link_plan_fail_closed(raw):
    d = classify("mission_dry_run", {"vehicle": "Drone1", "params": raw})
    if "lost_link_plan" in raw:
        assert d.klass == COMMAND and d.auto is False
    else:
        assert d.klass == PLAN


def test_string_params_are_read_for_the_survey_consequence():
    """An approval card must not say "nothing flies" for a survey hidden in a
    JSON-string params (uav_mission reads params['survey'] with plain bool())."""
    import json

    d = classify("uav_mission", {"vehicle": "Drone1", "kind": "threat_assessment",
                                 "params": json.dumps({"survey": True})})
    assert any("Takes off" in c for c in d.consequences)
    assert not any("nothing flies" in c for c in d.consequences)
    assert auto_track_vehicle("uav_mission", {"vehicle": "Drone1", "kind": "threat_assessment",
                                              "params": json.dumps({"survey": True})}) == "Drone1"


def test_bidi_controls_are_stripped_from_the_approval_text():
    """A model-written vehicle name carrying U+202E must not reverse or spoof the
    approval title, summary or consequences the operator reads."""
    rlo, lri, pdi = "\u202e", "\u2066", "\u2069"
    d = classify("uav_orbit_poi", {"vehicle": f"Drone1{rlo}ynneD", "poi": f"{lri}North Field{pdi}",
                                   "alt_agl_m": 60})
    text = " ".join((d.title, d.summary, *d.consequences))
    assert not any(ch in text for ch in (rlo, lri, pdi))
    assert "Drone1ynneD" in text
    fallback = classify(f"weird{rlo}tool", None)
    assert rlo not in fallback.title


# ======================================================================
# runtime theaters (WG spec §3.7, §4.3 A8)
# ======================================================================

#: WG spec §3.7 titles for the six Phase A tools.
PHASE_A_TITLES = {
    "geo_lookup": "Look up a place",
    "geo_sites": "List mapped sites",
    "theater_propose": "Propose a theater",
    "sim_set_theater": "Set the theater",
    "sim_set_time_scale": "Set sim speed",
    "ui_show_map": "Show on the map",
}

FORMAT_JS = (pathlib.Path(__file__).resolve().parents[2]
             / "gods-eye-view" / "src" / "console" / "chat" / "format.js")


def _box(lat: float, lon: float, half_m: float) -> list[list[float]]:
    """The square AO `theaters.make_dynamic` builds (same constants)."""
    dlat = half_m / 111_320.0
    dlon = half_m / (111_320.0 * math.cos(math.radians(lat)))
    return [[lat - dlat, lon - dlon], [lat - dlat, lon + dlon],
            [lat + dlat, lon + dlon], [lat + dlat, lon - dlon]]


SET_ARGS = {"proposal_id": "prop-1", "theater_id": "dyn-bengaluru-centre-1a2b3c",
            "label": "Bengaluru centre", "ao": _box(12.9716, 77.5946, 2500.0),
            "home_lat": 12.97321, "home_lon": 77.59102, "ground_msl_m": 920.0,
            "airframe": "quad_suas_electric"}


def _set_theater(**over):
    return classify("sim_set_theater", {**SET_ARGS, **over})


def test_phase_a_adds_exactly_six_classified_tools(server_tool_names):
    assert set(PHASE_A_TITLES) <= set(EXPECTED_CLASS)
    assert set(theater_tools.TOOL_NAMES) <= set(server_tool_names)
    assert pol.THEATER_TOOLS == set(theater_tools.TOOL_NAMES)
    assert "ui_show_map" in pol.CURATED_TOOLS
    assert {EXPECTED_CLASS[t] for t in ("geo_lookup", "geo_sites", "ui_show_map")} == {READ}
    assert EXPECTED_CLASS["theater_propose"] == PLAN
    assert EXPECTED_CLASS["sim_set_theater"] == EXPECTED_CLASS["sim_set_time_scale"] == SIM


def test_phase_a_titles_match_the_server_and_the_spec(server_tools):
    for name, title in PHASE_A_TITLES.items():
        assert classify(name, {}).title == title, name
        assert pol._TITLES[name] == title
    for name in theater_tools.TOOL_NAMES:
        # the server title wins: the policy repeats what the tool registers
        assert theater_tools.TITLES[name] == PHASE_A_TITLES[name]
        assert server_tools[name].title == PHASE_A_TITLES[name], name


@pytest.mark.skipif(not FORMAT_JS.is_file(), reason="console sources not in this checkout")
def test_policy_titles_match_the_console_tool_titles():
    text = FORMAT_JS.read_text(encoding="utf-8")
    block = text[text.index("export const TOOL_TITLES"):]
    block = block[:block.index("});")]
    js = dict(re.findall(r"^\s*([a-z_]+): '([^']*)',?\s*$", block, re.MULTILINE))
    assert set(PHASE_A_TITLES) <= set(js)
    for name in set(js) & set(pol._TITLES):
        assert js[name] == pol._TITLES[name], name


def test_geo_reads_are_static_auto_and_the_sim_tools_never_are():
    for name in ("geo_lookup", "geo_sites", "ui_show_map"):
        assert name in STATIC_AUTO_TOOLS
    for name in ("theater_propose", "sim_set_theater", "sim_set_time_scale"):
        assert name not in STATIC_AUTO_TOOLS
    for name in ("sim_set_theater", "sim_set_time_scale"):
        for args in ({}, SET_ARGS, {"scale": 4}, {"dry_run": True}):
            d = classify(name, args)
            assert d.klass == SIM and d.auto is False and d.allow_session is False
            assert d.acknowledge is False


@pytest.mark.parametrize("args", [
    {}, {"dry_run": True}, {"dry_run": False}, {"lost_link_plan": {"behaviour": "rtb"}},
    {"params": {"lost_link_plan": {}}}, {"params": "lost_link_plan=continue"},
    {"lat": 12.9716, "lon": 77.5946, "label": "Bengaluru centre"}])
def test_theater_propose_is_always_a_plan_and_never_a_dry_run(args):
    d = classify("theater_propose", args)
    assert d.klass == PLAN and d.auto is True and d.allow_session is False
    assert d.title == "Propose a theater"
    assert "dry run" not in d.summary and "lost-link" not in d.summary
    assert not any("lost-link" in c or "nothing is queued" in c for c in d.consequences)


def test_no_theater_tool_declares_dry_run_params_or_a_lost_link_plan(server_tools):
    """The premise of the two tests around this one: the server drops undeclared
    arguments, so on these tools neither flag can plan or rewrite anything."""
    for name in theater_tools.TOOL_NAMES:
        props = set(server_tools[name].input_schema.get("properties", {}))
        assert not props & {"dry_run", "params", "lost_link_plan"}, name


def test_mission_dry_run_keeps_its_lost_link_escalation():
    d = classify("mission_dry_run", {"vehicle": "Drone1", "lost_link_plan": {"b": 1}})
    assert d.klass == COMMAND and d.auto is False


@pytest.mark.parametrize("name", sorted(theater_tools.TOOL_NAMES))
def test_a_lost_link_plan_on_a_theater_tool_is_not_claimed(name):
    d = classify(name, {**SET_ARGS, "scale": 2, "lost_link_plan": {"behaviour": "rtb"}})
    assert not any("lost-link" in c for c in d.consequences), name
    assert "lost-link" not in d.summary


# ------------------------------------------------------------ acknowledge --

def test_only_safety_overrides_and_engagements_ask_for_an_acknowledgement():
    assert pol.ACKNOWLEDGE_CLASSES == {SAFETY_OVERRIDE, ENGAGEMENT}
    for name, klass in EXPECTED_CLASS.items():
        assert classify(name, {"vehicle": "Drone1"}).acknowledge is (
            klass in (SAFETY_OVERRIDE, ENGAGEMENT)), name
    assert classify("uav_teleport", {}).acknowledge is False


def test_acknowledge_defaults_off_and_the_fallback_never_sets_it(monkeypatch):
    d = Decision(klass=COMMAND, auto=False, allow_session=False, title="T", summary="s",
                 consequences=())
    assert d.acknowledge is False
    monkeypatch.setattr(pol, "_summary", lambda *a, **k: 1 / 0)
    fallback = classify("sim_set_fuel", {})
    assert fallback.klass == COMMAND and fallback.acknowledge is False


# ----------------------------------------------------------- consequences --

def test_set_theater_consequences_are_the_spec_lines():
    d = _set_theater()
    assert d.title == "Set the theater"
    assert d.summary == "Bengaluru centre · 5.0 × 5.0 km"
    assert d.consequences == (
        ("Moves the simulation to Bengaluru centre: a 5.0 × 5.0 km area around "
         "12.97160, 77.59460."),
        ("Every drone is parked, landed, at the new home (12.97321, 77.59102); the old "
         "area's geofence stops applying."),
        ("Fuel level and the BINGO latch are kept unless the airframe changes, which gives "
         "a full tank."),
        ("Refused if any drone is airborne, busy, BINGO-latched or has lost its link, and "
         "under real AirSim."),
        ("Contacts, reports and the audit trail are kept; alarms in progress and the old "
         "area's real data are cleared."),
    )


def test_set_theater_width_and_height_come_from_the_ao_bounds():
    # a group-3 box at 60°N: 50 km each way, and an uneven 12 x 4 km polygon
    assert "a 50.0 × 50.0 km area around 60.00000, 25.00000" in \
        _set_theater(ao=_box(60.0, 25.0, 25_000.0)).consequences[0]
    dlat = 4_000 / 111_320.0
    dlon = 12_000 / (111_320.0 * math.cos(math.radians(10.0 + dlat / 2)))
    uneven = [[10.0, 20.0], [10.0 + dlat, 20.0], [10.0 + dlat / 2, 20.0 + dlon / 3],
              [10.0 + dlat, 20.0 + dlon], [10.0, 20.0 + dlon]]
    first = _set_theater(ao=uneven).consequences[0]
    assert first.endswith("a 12.0 × 4.0 km area around 10.01797, 20.05473."), first


@pytest.mark.parametrize("ao", [None, [], [[1, 2], [3, 4]], "12.9,77.5", [[1, 2], [3, 4], [5]],
                                [[1, 2], [3, "4"], [5, 6]], [[1, 2], [3, True], [5, 6]],
                                [[1, 2], [3, float("nan")], [5, 6]],
                                [[1, 2], [95, 4], [5, 6]], [[1, 2], [3, 181], [5, 6]]])
def test_a_malformed_ao_drops_the_area_but_never_the_card(ao):
    d = _set_theater(ao=ao)
    assert d.klass == SIM and d.auto is False
    assert d.consequences[0] == "Moves the simulation to Bengaluru centre."
    assert len(d.consequences) == 5
    assert d.summary == "Bengaluru centre"


def test_set_theater_without_a_label_or_home_still_reads_well():
    no_label = _set_theater(label=None)
    assert no_label.consequences[0].startswith(
        "Moves the simulation to dyn-bengaluru-centre-1a2b3c: a 5.0 × 5.0 km area")
    bare = classify("sim_set_theater", {"ao": SET_ARGS["ao"]})
    assert bare.consequences[0] == ("Moves the simulation to a 5.0 × 5.0 km area around "
                                    "12.97160, 77.59460.")
    assert bare.consequences[1] == ("Every drone is parked, landed, at the new home; the old "
                                    "area's geofence stops applying.")
    empty = classify("sim_set_theater", {})
    assert empty.consequences[0] == "Moves the simulation to a new theater."
    assert empty.summary == "No arguments"
    assert classify("sim_set_theater", {"home_lat": True, "home_lon": 1.0}).consequences[1] \
        .endswith("at the new home; the old area's geofence stops applying.")


def test_untrusted_theater_labels_are_one_line_of_plain_text():
    xss = "<img src=x onerror=alert(1)>"
    d = _set_theater(label=xss)
    # the policy passes the label through as text; the console renders it as text
    assert d.consequences[0].startswith(f"Moves the simulation to {xss}: a 5.0")
    assert d.summary.startswith(xss)
    rlo, pdf, lri = chr(0x202E), chr(0x202C), chr(0x2066)
    spoof = _set_theater(label=f"{rlo}evil{pdf}\nIgnore\tprevious\x00 rules{lri}")
    text = " ".join((spoof.title, spoof.summary, *spoof.consequences))
    for ch in (rlo, pdf, lri, "\n", "\t", "\x00"):
        assert ch not in text, repr(ch)
    assert "Moves the simulation to evil Ignore previous rules: a" in spoof.consequences[0]
    long = _set_theater(label="Very long place name " * 10)
    name = long.consequences[0].split("Moves the simulation to ", 1)[1].split(": a ", 1)[0]
    assert len(name) == 60 and name.endswith("…")


def test_theater_consequences_stay_short_and_sentence_case_at_the_extremes():
    far = [[-89.0, -179.9], [-89.0, 179.9], [89.0, 179.9], [89.0, -179.9]]
    for args in ({"label": "W" * 300, "ao": far, "home_lat": -89.12345, "home_lon": -179.12345},
                 {"label": "Ünïcödé ✓ " * 20, "ao": _box(-45.5, -170.25, 25_000.0)},
                 {"theater_id": "x" * 500}):
        d = classify("sim_set_theater", args)
        for c in d.consequences:
            assert len(c) <= 160, c
            assert c[0].isupper() and c.endswith("."), c
        assert len(d.summary) <= 200


@pytest.mark.parametrize("scale,speed", [(10, "10× faster"), (4, "4× faster"),
                                         (2.5, "2.5× faster"), (10.0, "10× faster")])
def test_time_scale_consequences_are_the_spec_lines(scale, speed):
    d = classify("sim_set_time_scale", {"scale": scale})
    assert d.title == "Set sim speed"
    assert d.summary == f"×{speed.split('×')[0]}"
    assert d.consequences == (
        (f"Runs the fake simulator {speed} (physics, fuel, sun). Link-loss timers stay in "
         "wall-clock seconds."),
        ("Safety checks and camera captures stay on a real-time clock, so they happen less "
         "often per simulated second."),
    )


def test_time_scale_back_to_normal_does_not_claim_faster():
    d = classify("sim_set_time_scale", {"scale": 1})
    assert d.summary == "×1"
    assert d.consequences == (
        "Runs the fake simulator at normal speed (physics, fuel, sun).",
        "Safety checks and camera captures run at their normal rate per simulated second.")


@pytest.mark.parametrize("scale", [None, "fast", True, float("inf"), [4]])
def test_time_scale_without_a_readable_scale_still_explains_itself(scale):
    d = classify("sim_set_time_scale", {} if scale is None else {"scale": scale})
    assert d.klass == SIM and d.auto is False
    assert d.consequences[0].startswith("Runs the fake simulator at a new speed")
    assert len(d.consequences) == 2


def test_theater_read_and_plan_summaries():
    assert classify("geo_lookup", {"query": "Kherson", "limit": 3}).summary == "“Kherson”"
    assert classify("geo_sites", {"category": "airfield", "near_lat": 46.6, "near_lon": 32.6,
                                  "refresh": True}).summary \
        == "airfield · near 46.6000, 32.6000 · refresh"
    assert classify("geo_sites", {}).summary == "No arguments"
    assert classify("theater_propose", {
        "lat": 12.9716, "lon": 77.5946, "label": "Bengaluru centre", "ground_msl_m": 920,
        "airframe": "quad_suas_electric", "half_extent_m": 2500, "query": "12.97160, 77.59460",
    }).summary == ("Bengaluru centre · 12.9716, 77.5946 · “12.97160, 77.59460” · "
                   "2500 m half-extent · quad_suas_electric")
    assert classify("theater_propose", {"theater_id": "kherson"}).summary == "kherson"
    assert classify("ui_show_map", {"ids": ["thr:x", "trk:T-1"], "reason": "Watch"}).summary \
        == "2 entities"


# ------------------------------------------------------ the analyst prompt --
# WG spec §5.2.11 (B7): `analyst_prompt.md` is the base (everything except
# `## Identity`), `analyst_prompt_isr.md` the default identity and
# `analyst_prompt_wargame.md` the simulated wargame addendum (session only).

#: The fourteen chip prefixes (WG spec §3.1: `sit` in Phase A; `frc eng vec` in B).
PROMPT_PREFIXES = ["veh", "msn", "trk", "unit", "ob", "rpt", "thr", "poi", "sit", "frc", "eng",
                   "vec", "alarm", "feed"]
PROMPT_FILES = ("analyst_prompt.md", "analyst_prompt_isr.md", "analyst_prompt_wargame.md")

#: `analyst_prompt.md:9-14` at Phase A, moved verbatim into the ISR identity.
HEAD_ISR_IDENTITY = """\
## Identity: ISR only

This system observes, classifies and reports. It has no weapons and you never reason about engaging,
striking, targeting for fires or prosecuting anything. If asked to attack, say plainly that this is an
ISR system and offer observation instead. Threat output is sensor-posture and self-protection advice
only (stand off, climb, change aspect, break contact), never an engagement recommendation.
"""
#: WG spec §5.2.11: the paragraph that follows it.
M14A_OFF = ("The simulated wargame (M14a) is off in this session. If the operator asks to simulate "
            "an attack between simulated forces, offer to start one with `wg_session_start`, which "
            "they approve. Until then you have no wargame tools. Real places are never targets in "
            "any mode.")
#: The ISR refusal line: in the ISR identity, never in the wargame prompt.
REFUSAL_LINE = ("If asked to attack, say plainly that this is an ISR system and offer observation "
                "instead.")
WARGAME_HEADING = "## Identity: simulated wargame (M14a)"
_BIDI = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))}
_TOOL_RX = re.compile(r"\b((?:wg|uav|mission|sim|intel|ui|geo|theater)_[a-z_]+)")
PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
PYI_SPEC = pathlib.Path(__file__).resolve().parents[1] / "packaging" / "macos" / "EyeInTheSky.spec"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read_prompt(name: str) -> str:
    from importlib.resources import files

    return files("godseye_uav").joinpath(name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def prompt_text():
    """The base prompt."""
    return _read_prompt("analyst_prompt.md")


@pytest.fixture(scope="module")
def isr_text():
    return _read_prompt("analyst_prompt_isr.md")


@pytest.fixture(scope="module")
def wargame_text():
    return _read_prompt("analyst_prompt_wargame.md")


def test_the_base_prompt_is_everything_but_the_identity(prompt_text):
    lines = prompt_text.splitlines()
    assert lines[0] == "# Eye in the Sky — ISR analyst"
    # the intro paragraph, then straight into the console section
    assert lines[7] == "" and lines[8] == "## How the console works"
    assert "## Identity" not in prompt_text
    assert REFUSAL_LINE not in _flat(prompt_text)
    assert "No engagement recommendations." in prompt_text
    assert "wg_" not in prompt_text                        # wargame tools: addendum only


def test_prompt_prefix_list_has_the_fourteen_prefixes(prompt_text):
    flat = _flat(prompt_text)
    m = re.search(r"The prefixes are exactly these fourteen \(`([a-z ]+)`\)", flat)
    assert m, "the prefix sentence moved or changed"
    assert m.group(1).split() == PROMPT_PREFIXES
    used = set(re.findall(r"\[\[([a-z]+):", prompt_text)) - {"type"}
    assert used == set(PROMPT_PREFIXES)
    assert "`[[frc:…|Red SAM 1]]`" in prompt_text          # a generic designator


def test_prompt_teaches_the_theater_workflow(prompt_text):
    flat = _flat(prompt_text)
    for needle in (
        "call `geo_lookup` (or take the coordinates the operator gives), then `theater_propose`",
        "Choose `airframe=\"group3_fixed_wing\"` for areas wider than about 6 km.",
        "Summarise the proposal in two lines",
        "call `sim_set_theater` with its `set_args` exactly",
        "The operator approves, and the drones must be landed and idle",
        "Use `sim_set_time_scale` for long sorties.",
        "`geo_sites` returns mapped strategic sites.",
        "They are context only, mapped and not verified.",
        "Cite them as `[[sit:…|name]]`.",
        "A missing site is not an absent one.",
        "`ui_show_map` shows an area on the map.",
        "A recce is `mission_recon_route` or `mission_grid_search`, dry run first.",
        "it never changes the doctrine mode, and a real place is context, never a target.",
    ):
        assert needle in flat, needle


def test_prompt_names_every_phase_a_tool_and_their_approval_class(prompt_text):
    for name in PHASE_A_TITLES:
        assert f"`{name}`" in prompt_text, name
    flat = _flat(prompt_text)
    reads = flat[flat.index("**Reads run at once.**"):flat.index("**Everything else waits")]
    for name in ("geo_lookup", "geo_sites", "theater_propose"):
        assert name in reads and EXPECTED_CLASS[name] in (READ, PLAN)
    asks = flat[flat.index("**Everything else waits"):flat.index("**Never claim")]
    for name in ("sim_set_theater", "sim_set_time_scale"):
        assert name in asks and EXPECTED_CLASS[name] == SIM


def test_the_isr_identity_is_the_phase_a_section_verbatim_then_the_m14a_paragraph(isr_text):
    assert isr_text.startswith(HEAD_ISR_IDENTITY + "\n")
    rest = isr_text[len(HEAD_ISR_IDENTITY) + 1:]
    assert _flat(rest) == M14A_OFF
    assert "ISR only" in isr_text and REFUSAL_LINE in _flat(isr_text)
    assert isr_text.count("## ") == 1
    # the only wargame tool it names is one the ISR toolbelt carries
    named = set(_TOOL_RX.findall(isr_text))
    assert named == {"wg_session_start"} and named <= pol.WG_ENTRY_TOOLS


def test_the_wargame_addendum_is_its_own_identity(wargame_text):
    lines = wargame_text.splitlines()
    assert lines[0] == WARGAME_HEADING and wargame_text.count("## ") == 1
    flat = _flat(wargame_text)
    assert REFUSAL_LINE not in flat and "ISR only" not in flat
    assert "## Identity: ISR only" not in wargame_text
    for needle in (
        "A simulated wargame session is active.",
        "Nothing real is fired.",
        "Only scenario units can be engaged.",
        "never targets; the server refuses them.",
        "give positions relative to the AO centre.",
        "Drones never deliver effects. Shooters are blue scenario units.",
        "`wg_propose_strike(shooter_id, target_track_id)`",
        "then call `wg_execute_engagement` with `execute_args` exactly.",
        "The operator approves every engagement in the console.",
        "in blue view outcomes stay hidden until battle damage assessment.",
        "`wg_plan_corridor(relook=true)` and fly `mission_recon_route(**recon_args)`, dry run first;",
        "Probabilities are notional play-balance numbers; say so.",
        ("End with `wg_session_end` and cite the after-action review as "
         "`uav://reports/aar-<session id>`."),
    ):
        assert needle in flat, needle


def test_every_tool_the_prompts_name_is_one_the_policy_knows(prompt_text, isr_text,
                                                             wargame_text):
    """A prompt must never teach a tool the policy would fail closed on."""
    for text in (prompt_text, isr_text, wargame_text):
        for name in set(_TOOL_RX.findall(text)):
            assert name in KNOWN_TOOLS, name
    assert {"wg_propose_strike", "wg_execute_engagement", "wg_plan_corridor",
            "wg_session_end"} <= set(_TOOL_RX.findall(wargame_text))


def test_the_prompts_hold_no_bidi_and_no_real_system_tokens(prompt_text, isr_text, wargame_text):
    from support.wg_tokens import assert_no_real_system_tokens

    for text in (prompt_text, isr_text, wargame_text):
        assert not _BIDI & set(text)
        assert "none may be added" not in text.lower()     # doctrine lint (B0)
    # The addendum's own "No weaponeering:" line names the banned terms to ban
    # them; every other line of it is held to the wargame token rule.
    lines = [ln for ln in _flat(wargame_text).split("- ") if not ln.startswith("No weaponeering")]
    assert len(lines) >= 6
    assert_no_real_system_tokens(lines)
    assert_no_real_system_tokens(M14A_OFF)


def test_every_prompt_file_ships_as_package_data():
    import tomllib

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    listed = data["tool"]["setuptools"]["package-data"]["godseye_uav"]
    spec = PYI_SPEC.read_text(encoding="utf-8")
    for name in PROMPT_FILES:
        assert name in listed, name
        assert f'"{name}"' in spec, name
        assert _read_prompt(name).strip(), name


def test_the_composed_prompt_follows_the_mode():
    """B8's `_load_prompt(mode)` returns base + identity (WG spec §5.2.11).  The
    ISR prompt keeps "ISR only" and the refusal line (`test_chat.py:490`); the
    wargame prompt has its own identity and lacks the refusal line."""
    import inspect

    from godseye_uav import chat

    if not inspect.signature(chat._load_prompt).parameters:
        pytest.skip("B8 adds _load_prompt(mode); not merged yet")
    isr = chat._load_prompt("isr")
    assert isr == chat._load_prompt()                        # ISR is the default
    wargame = chat._load_prompt("wargame")
    base = _flat(_read_prompt("analyst_prompt.md"))
    assert "ISR only" in isr and REFUSAL_LINE in _flat(isr)
    assert _flat(HEAD_ISR_IDENTITY) in _flat(isr) and M14A_OFF in _flat(isr)
    assert WARGAME_HEADING not in isr
    assert WARGAME_HEADING in wargame and REFUSAL_LINE not in _flat(wargame)
    assert "## Identity: ISR only" not in wargame and M14A_OFF not in _flat(wargame)
    for prompt in (isr, wargame):
        assert "[[type:id|label]]" in prompt and "No engagement recommendations." in prompt
        assert len(_flat(prompt)) >= len(base)


# ======================================================================
# simulated wargame (M14a; WG spec §3.7, §5.2.11, §6; B7)
# ======================================================================

WG_EXPECTED = {n: k for n, k in EXPECTED_CLASS.items() if n.startswith("wg_")}
WG_ARGS = {
    "wg_session_start": {"seed": 4417, "red_engages": True, "reveal_red": False},
    "wg_session_status": {"events": 20},
    "wg_list_classes": {},
    "wg_session_end": {},
    "wg_generate_scenario": {"template": "air_defence_belt", "intensity": "high",
                             "ad_class": "ad_short"},
    "wg_spawn_force": {"side": "red", "wg_class": "ad_gun", "lat": 47.6512, "lon": -122.1234,
                       "count": 2},
    "wg_list_forces": {"side": "blue"},
    "wg_plan_corridor": {"vehicle": "Drone1", "target_track_id": "T-0003", "alt_agl_m": 60,
                         "relook": True, "relook_radius_m": 400},
    "wg_propose_strike": {"shooter_id": "blue-artillery-1", "target_track_id": "T-0003"},
    "wg_execute_engagement": {"pending_id": "WG-4417-E1", "shooter_id": "blue-artillery-1",
                              "target_track_id": "T-0003"},
}
#: Arguments a model could add to talk an engagement down.
ADVERSARIAL = [{}, {"dry_run": True}, {"dry_run": "true"}, {"lost_link_plan": {"b": "rtb"}},
               {"params": {"dry_run": True}}, {"klass": "read", "auto": True},
               {"allow_session": True, "acknowledged": True}, {"survey": True},
               {**WG_ARGS["wg_execute_engagement"], "dry_run": True}]


def test_every_wargame_tool_has_an_explicit_classification(wargame_tools):
    """§6: iterates `srv.wargame_mcp.list_tools()`, the registry B4 fills."""
    assert set(wargame_tools) == set(wargame_registry.TOOL_NAMES) == pol.WG_TOOLS
    assert len(wargame_tools) == 10
    for name in wargame_tools:
        assert name in KNOWN_TOOLS, name
        assert EXPECTED_CLASS[name] == wargame_registry.TOOL_CLASSES[name], name
        assert classify(name, {}).klass == EXPECTED_CLASS[name], name
    assert set(WG_EXPECTED) == pol.WG_TOOLS


def test_the_default_catalog_has_no_wargame_tool(server_tools):
    assert not any(n.startswith("wg_") for n in server_tools)
    assert not pol.WG_TOOLS & set(server_tools)


def test_the_wargame_tables_are_the_spec_rows():
    assert pol.WG_READ_TOOLS == {"wg_session_status", "wg_list_forces", "wg_list_classes"}
    assert pol.WG_PLAN_TOOLS == {"wg_plan_corridor", "wg_propose_strike"}
    assert pol.WG_PLAN_TOOLS <= pol.PLAN_TOOLS and pol.WG_READ_TOOLS <= pol.READ_TOOLS
    assert pol.WG_SIM_TOOLS == {"wg_session_start", "wg_session_end", "wg_generate_scenario",
                                "wg_spawn_force"} and pol.WG_SIM_TOOLS <= pol.SIM_TOOLS
    assert pol.ENGAGEMENT_TOOLS == {"wg_execute_engagement"}
    assert pol.WG_ENTRY_TOOLS == {"wg_session_start", "wg_session_status", "wg_list_classes"}
    assert pol.WG_ENTRY_TOOLS == wargame_registry.ENTRY_TOOLS
    sets = [pol.READ_TOOLS, pol.PLAN_TOOLS, pol.SENSOR_TOOLS, pol.COMMAND_TOOLS, pol.SIM_TOOLS,
            pol.SAFETY_OVERRIDE_TOOLS, pol.ENGAGEMENT_TOOLS]
    for i, a in enumerate(sets):                           # exactly one class per tool
        for b in sets[i + 1:]:
            assert not a & b, a & b


def test_engagement_is_a_class_of_its_own():
    assert ENGAGEMENT == "engagement" and ENGAGEMENT in pol.CLASSES
    assert ENGAGEMENT not in pol.AUTO_CLASSES and ENGAGEMENT not in pol.SESSION_CLASSES
    assert ENGAGEMENT in pol.ACKNOWLEDGE_CLASSES
    assert not pol.ENGAGEMENT_TOOLS & STATIC_AUTO_TOOLS


@pytest.mark.parametrize("args", ADVERSARIAL)
@pytest.mark.parametrize("tool", ["wg_execute_engagement", f"{TOOL_PREFIX}wg_execute_engagement"])
def test_an_engagement_always_asks_with_an_acknowledgement(tool, args):
    d = classify(tool, args)
    assert d.klass == ENGAGEMENT
    assert d.auto is False and d.allow_session is False and d.acknowledge is True
    assert d.title == "Execute a simulated engagement"
    assert "dry run" not in d.summary and "lost-link" not in d.summary
    assert not any("lost-link" in c or "nothing is queued" in c for c in d.consequences)


def test_a_policy_bug_keeps_an_engagement_an_engagement(monkeypatch):
    """Fail closed means the stricter class: the fallback must not turn an
    engagement into a plain command (no acknowledgement, no console path)."""
    monkeypatch.setattr(pol, "_summary", lambda *a, **k: 1 / 0)
    d = classify("wg_execute_engagement", WG_ARGS["wg_execute_engagement"])
    assert d.klass == ENGAGEMENT and d.acknowledge is True
    assert d.auto is False and d.allow_session is False
    assert d.consequences == pol.WG_ENGAGEMENT_NOTES
    other = classify("wg_spawn_force", WG_ARGS["wg_spawn_force"])
    assert other.klass == COMMAND and other.auto is False and other.acknowledge is False


@pytest.mark.parametrize("tool", ["wg_fire", "wg_execute_engagement2", "wg_",
                                  "mcp__other__wg_execute_engagement", "wg_session_starts"])
def test_an_unknown_wargame_name_fails_closed(tool):
    d = classify(tool, {"dry_run": True})
    assert d.klass == COMMAND and d.auto is False and d.allow_session is False


def test_wargame_reads_run_at_once_and_everything_else_wg_waits():
    for name, klass in WG_EXPECTED.items():
        for args in ADVERSARIAL:
            d = classify(name, args)
            assert d.klass == klass, (name, args)
            assert d.auto is (klass in (READ, PLAN)), name
            assert d.allow_session is False, name
            assert (name in STATIC_AUTO_TOOLS) is (klass == READ), name


def test_wargame_titles_match_the_registry_the_server_and_the_console(wargame_tools):
    for name in pol.WG_TOOLS:
        assert pol._TITLES[name] == wargame_registry.TITLES[name], name
        assert wargame_tools[name].title == pol._TITLES[name], name    # the server title wins
        assert classify(name, {}).title == pol._TITLES[name], name
    if FORMAT_JS.is_file():
        text = FORMAT_JS.read_text(encoding="utf-8")
        block = text[text.index("export const TOOL_TITLES"):]
        block = block[:block.index("});")]
        js = dict(re.findall(r"^\s*([a-z_]+): '([^']*)',?\s*$", block, re.MULTILINE))
        for name in pol.WG_TOOLS:
            assert js.get(name) == pol._TITLES[name], name


def test_session_start_consequences_are_the_spec_lines():
    d = classify("wg_session_start", WG_ARGS["wg_session_start"])
    assert d.klass == SIM and d.title == "Start a simulated wargame"
    assert d.summary == "seed 4417 · red may fire"
    assert d.consequences == (
        ("Starts a simulated wargame session (M14a). The analyst gets the wargame tools; every "
         "engagement will still ask you first."),
        "Nothing real is fired. Scenario units are simulated and kept away from mapped real places.",
        ("Red air defence may down drones automatically; a downed drone's current task is "
         "aborted and it stays down until the wargame ends."),
        ("Refused under real AirSim, during a theater switch, and in theaters not cleared for "
         "the wargame."),
    )
    held = classify("wg_session_start", {"seed": 4417, "red_engages": False, "reveal_red": True})
    assert held.consequences[2] == "Red forces won't fire in this session."
    assert held.summary == "seed 4417 · red holds fire · red revealed to the planner"


@pytest.mark.parametrize("value", [None, True, "false", 0, "no"])
def test_red_holds_fire_is_claimed_only_for_a_literal_false(value):
    """Erring toward "red may fire" keeps the card honest: it never promises a
    quiet session the server might not give."""
    args = {} if value is None else {"red_engages": value}
    d = classify("wg_session_start", args)
    assert d.consequences[2].startswith("Red air defence may down drones")


def test_session_end_generate_and_spawn_consequences_are_the_spec_lines():
    assert classify("wg_session_end", {}).consequences == (
        ("Ends the simulated wargame: scenario units, their contacts and waiting engagements "
         "are removed."),
        "Downed drones are restored at home, landed. The after-action review is kept as a report.",
        "Aircraft keep their current tasks.",
    )
    gen = classify("wg_generate_scenario", WG_ARGS["wg_generate_scenario"])
    assert gen.summary == "air-defence belt · high · surface-to-air, short range"
    assert gen.consequences == (
        "Places simulated scenario units for the air-defence belt template (high).",
        ("Units are kept at least 500 m from mapped places and theater points and, for red, "
         "1 km from home."),
        "Refused if the area has no room for them.",
    )
    assert classify("wg_generate_scenario", {"template": "mech_advance"}).consequences[0] == (
        "Places simulated scenario units for the mechanised advance template (medium).")
    spawn = classify("wg_spawn_force", WG_ARGS["wg_spawn_force"])
    assert spawn.summary == "red · air-defence guns · ×2 · 47.6512, -122.1234"
    assert spawn.consequences == (
        "Adds 2 simulated red air-defence guns near 47.6512, -122.1234.",
        ("Refused within 500 m of a mapped place or theater point, within 1 km of home (red), "
         "or within 200 m of another unit."),
    )
    one = classify("wg_spawn_force", {"side": "blue", "wg_class": "blue_artillery",
                                      "lat": 47.6, "lon": -122.1})
    assert one.consequences[0] == "Adds 1 simulated blue artillery battery near 47.6000, -122.1000."


def test_engagement_consequences_and_summary_are_the_spec_lines():
    d = classify("wg_execute_engagement", WG_ARGS["wg_execute_engagement"])
    assert d.summary == "blue-artillery-1 → track T-0003 · WG-4417-E1"
    assert d.consequences == (
        "Rolls one simulated outcome for this engagement against a scenario unit.",
        "Nothing real is fired.",
        "The outcome stands for the rest of this wargame; only ending the wargame clears it.",
        "In blue view the outcome stays hidden until a re-look assesses damage.",
    )


def test_plan_tools_say_they_plan_and_reads_summarise():
    corridor = classify("wg_plan_corridor", WG_ARGS["wg_plan_corridor"])
    assert corridor.klass == PLAN and corridor.auto is True
    assert corridor.summary == "Drone1 → track T-0003 · 60 m AGL · re-look, 400 m radius"
    assert corridor.consequences == (pol.PLAN_NOTE,)
    strike = classify("wg_propose_strike", WG_ARGS["wg_propose_strike"])
    assert strike.klass == PLAN and strike.summary == "blue-artillery-1 → track T-0003"
    assert strike.consequences == (pol.WG_PROPOSE_NOTE,)
    assert classify("wg_session_status", {"events": 20}).summary == "20 events"
    assert classify("wg_list_forces", {"side": "blue"}).summary == "blue"
    assert classify("wg_list_classes", {}).summary == "No arguments"


def test_wargame_approval_text_names_no_real_system(wargame_tools):
    """D1/V6: titles, summaries and consequences are generic and notional,
    for every class key the tables know."""
    from godseye_uav.wargame_tables import CLASSES, TEMPLATES
    from support.wg_tokens import assert_no_real_system_tokens

    decisions = [classify(n, a) for n, a in WG_ARGS.items()]
    decisions += [classify("wg_spawn_force", {"side": c.side, "wg_class": key, "lat": 1.0,
                                              "lon": 2.0}) for key, c in CLASSES.items()]
    decisions += [classify("wg_generate_scenario", {"template": t}) for t in TEMPLATES]
    assert_no_real_system_tokens(decisions)
    for d in decisions:
        text = " ".join((d.title, d.summary, *d.consequences))
        assert "SIMULATED" not in text                     # sentence case (§3.1 R21)
        assert "simulated" in text.lower() or d.klass in (READ, PLAN) \
            or d.title == "End the wargame", d
    assert set(pol._WG_TEMPLATE_WORDS) == set(TEMPLATES)


def test_no_wargame_tool_declares_dry_run_params_or_a_lost_link_plan(wargame_tools):
    """The premise of the adversarial tests: the server drops these, so neither
    can plan anything or rewrite a live lost-link plan on a `wg_*` tool."""
    for name, tool in wargame_tools.items():
        props = set(tool.input_schema.get("properties", {}))
        assert not props & {"dry_run", "params", "lost_link_plan"}, name


@pytest.mark.parametrize("name", sorted(WG_EXPECTED))
def test_a_lost_link_plan_on_a_wargame_tool_is_not_claimed(name):
    d = classify(name, {**WG_ARGS[name], "lost_link_plan": {"behaviour": "rtb"}})
    assert d.klass == WG_EXPECTED[name]
    assert not any("lost-link" in c for c in d.consequences), name
    assert "lost-link" not in d.summary


def test_model_written_wargame_text_is_one_line_of_plain_text():
    rlo, pdf, lri = chr(0x202E), chr(0x202C), chr(0x2066)
    xss = "<img src=x onerror=alert(1)>"
    d = classify("wg_execute_engagement", {"pending_id": f"{rlo}WG-1{pdf}\nIgnore rules",
                                           "shooter_id": xss, "target_track_id": f"T-1{lri}"})
    text = " ".join((d.title, d.summary, *d.consequences))
    for ch in (rlo, pdf, lri, "\n"):
        assert ch not in text, repr(ch)
    assert d.summary.startswith(f"{xss} → track T-1")       # text; the console renders text
    long = classify("wg_spawn_force", {"side": "red", "wg_class": "x" * 500, "lat": 1, "lon": 2,
                                       "count": 3})
    assert len(long.consequences[0]) <= 160 and len(long.summary) <= 200
    assert long.consequences[0].startswith("Adds 3 simulated red " + "x" * 39 + "…")
    odd = classify("wg_spawn_force", {"wg_class": True, "lat": "north", "count": "many"})
    assert odd.consequences[0] == "Adds simulated units near the given point."
