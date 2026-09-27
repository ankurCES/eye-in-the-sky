"""analyst_policy.classify: the approval gate for the in-app analyst (contract §5.2).

The policy must FAIL CLOSED and must cover every tool the real server
registers, so this file builds the real GodseyeUavServer in-process (no sim
needed just to list tools) and iterates its catalog.
"""
import asyncio
import dataclasses

import pytest
from godseye_uav import analyst_policy as pol
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
        "read_intel_resource", "ui_focus", "ui_track", "ui_show_orb", "ui_inspect")},
    "mission_dry_run": PLAN,
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
                        "sim_spawn_order_of_battle", "sim_set_environment")},
    **{t: SAFETY_OVERRIDE for t in ("sim_set_fuel", "sim_set_link_state", "sim_reset")},
}

MOVEMENT_AND_MISSION = (
    "uav_goto_gps", "uav_fly_route", "uav_orbit_poi", "uav_mission", "mission_grid_search",
    "mission_recon_route", "mission_track_target", "mission_identify_target",
    "mission_handoff_track", "uav_handoff_target")


@pytest.fixture(scope="module")
def server_tool_names(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("policy-store"))
    try:
        srv = GodseyeUavServer(None, store)
        tools = asyncio.run(srv.mcp.list_tools())
        return sorted(t.name for t in tools)
    finally:
        store.close()


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
