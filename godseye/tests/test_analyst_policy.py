"""analyst_policy.classify: the approval gate for the in-app analyst (contract §5.2).

The policy must FAIL CLOSED and must cover every tool the real server
registers, so this file builds the real GodseyeUavServer in-process (no sim
needed just to list tools) and iterates its catalog.
"""
import asyncio
import dataclasses
import math
import pathlib
import re

import pytest
from godseye_uav import analyst_policy as pol
from godseye_uav import theater_tools
from godseye_uav.analyst_policy import (
    COMMAND,
    DRY_RUN_TOOLS,
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
        "geo_lookup", "geo_sites", "ui_show_map")},
    "mission_dry_run": PLAN,
    "theater_propose": PLAN,
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
                        "sim_set_theater", "sim_set_time_scale")},
    **{t: SAFETY_OVERRIDE for t in ("sim_set_fuel", "sim_set_link_state", "sim_reset")},
}

MOVEMENT_AND_MISSION = (
    "uav_goto_gps", "uav_fly_route", "uav_orbit_poi", "uav_mission", "mission_grid_search",
    "mission_recon_route", "mission_track_target", "mission_identify_target",
    "mission_handoff_track", "uav_handoff_target")


@pytest.fixture(scope="module")
def server_tools(tmp_path_factory):
    """``{name: Tool}`` for every tool the real server registers."""
    store = Store(tmp_path_factory.mktemp("policy-store"))
    try:
        srv = GodseyeUavServer(None, store)
        tools = asyncio.run(srv.mcp.list_tools())
        return {t.name: t for t in tools}
    finally:
        store.close()


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
        assert d.acknowledge is (klass == SAFETY_OVERRIDE), name
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


def test_command_sim_and_override_are_never_session_approvable():
    for name, klass in EXPECTED_CLASS.items():
        if klass in (COMMAND, SIM, SAFETY_OVERRIDE):
            for args in ({}, {"vehicle": "Drone1"}, {"vehicle": "Drone1", "survey": True}):
                d = classify(name, args)
                assert d.allow_session is False and d.auto is False, name


# --------------------------------------------------------- static allowlist --

def test_static_auto_tools_hold_only_arg_independent_reads():
    forbidden = {n for n, k in EXPECTED_CLASS.items() if k in (SENSOR, COMMAND, SIM,
                                                               SAFETY_OVERRIDE)}
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
    if d.klass in (COMMAND, SENSOR, SIM, SAFETY_OVERRIDE):
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

def test_only_safety_overrides_ask_for_an_acknowledgement():
    assert pol.ACKNOWLEDGE_CLASSES == {SAFETY_OVERRIDE}
    for name, klass in EXPECTED_CLASS.items():
        assert classify(name, {"vehicle": "Drone1"}).acknowledge is (klass == SAFETY_OVERRIDE)
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

#: The eleven chip prefixes of Phase A (WG spec §3.1: `sit` joins the ten).
PROMPT_PREFIXES = ["veh", "msn", "trk", "unit", "ob", "rpt", "thr", "poi", "sit", "alarm", "feed"]


@pytest.fixture(scope="module")
def prompt_text():
    from importlib.resources import files

    return files("godseye_uav").joinpath("analyst_prompt.md").read_text(encoding="utf-8")


def test_prompt_keeps_the_isr_only_identity_where_b7_splits_it(prompt_text):
    lines = prompt_text.splitlines()
    # WG spec §5.1: B7 splits `analyst_prompt.md:9-14` out as the ISR identity.
    assert lines[8] == "## Identity: ISR only"
    assert lines[10].startswith("This system observes, classifies and reports.")
    assert lines[13].endswith("never an engagement recommendation.")
    assert lines[14] == "" and lines[15] == "## How the console works"
    assert prompt_text.count("## Identity") == 1
    assert "No engagement recommendations." in prompt_text


def test_prompt_prefix_list_has_the_eleven_prefixes(prompt_text):
    flat = " ".join(prompt_text.split())
    m = re.search(r"The prefixes are exactly these eleven \(`([a-z ]+)`\)", flat)
    assert m, "the prefix sentence moved or changed"
    assert m.group(1).split() == PROMPT_PREFIXES
    used = set(re.findall(r"\[\[([a-z]+):", prompt_text)) - {"type"}
    assert used == set(PROMPT_PREFIXES)


def test_prompt_teaches_the_theater_workflow(prompt_text):
    flat = " ".join(prompt_text.split())
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
        "it stays ISR only, and a real place is context, never a target.",
    ):
        assert needle in flat, needle


def test_prompt_names_every_phase_a_tool_and_their_approval_class(prompt_text):
    for name in PHASE_A_TITLES:
        assert f"`{name}`" in prompt_text, name
    flat = " ".join(prompt_text.split())
    reads = flat[flat.index("**Reads run at once.**"):flat.index("**Everything else waits")]
    for name in ("geo_lookup", "geo_sites", "theater_propose"):
        assert name in reads and EXPECTED_CLASS[name] in (READ, PLAN)
    asks = flat[flat.index("**Everything else waits"):flat.index("**Never claim")]
    for name in ("sim_set_theater", "sim_set_time_scale"):
        assert name in asks and EXPECTED_CLASS[name] == SIM


def test_prompt_stays_isr_in_phase_a(prompt_text):
    assert "wg_" not in prompt_text
    assert "ISR only" in prompt_text
    bidi = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))}
    assert not bidi & set(prompt_text)
