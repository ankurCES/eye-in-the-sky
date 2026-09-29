"""Phase A's ISR surface changes, pinned (WG v2 §4.1.11, A6a).

Phase A is ISR-neutral: these are the ONLY changes it may make to surfaces an
ISR harness already reads, and each one is pinned here as an exact set or a
literal, so a later unit cannot widen an ISR surface by accident:

* `/mcp` tools: exactly the 46 at HEAD `a6763a9` plus the five Phase A tools;
* every existing tool description is byte-identical to HEAD;
* `uav://safety/geofence`: top-level keys unchanged; `theater` adds only
  `epoch` and `dynamic`; `isr_only` unchanged;
* gate dicts add only `theater_epoch`; `_submit` adds only the refusals
  `theater_changed` and `theater_integrity`;
* `Airframe.to_dict()` adds `label`/`cruise_speed_mps`, `environment()` and
  `fuel_record()` add `time_scale` (A2's additions, pinned here at A2's ask);
* `threat.ISR_AUTHORITY_NOTE`, `realdata.MAPPED_DATA_CAVEAT` and the
  `chat.py` stub prompt are the HEAD literals.

Phase B (WG v2 §5.0, B5): in ISR mode (no session, no `--wargame-mcp`) Phase B
changes ONLY these surfaces, each pinned below:

* `uav://safety/geofence` adds `doctrine` (mode "isr"); `isr_only` unchanged;
* `_submit` adds only the `vehicle_lost` refusal (same five keys);
* `sim_spawn_target` refuses a duplicate label (`duplicate_name`);
* `/intel/graph` meta adds `wargame:{active:false,last}` (B10);
* the analyst toolbelt adds only the 3 `WG_ENTRY_TOOLS` (B7/B8);
* the analyst prompt is base + ISR identity, which keeps "ISR only" (B7);
* `/mcp` and every ISR text above are unchanged (the Phase A pins hold).
Rows for units not merged yet skip on that unit's own absence.

No network; the fake sim binds A6a's range (53100-53149), and B5's Phase B
rows bind 53680-53699.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import os
from contextlib import contextmanager

import airsim
import pytest
from godseye_uav import chat, realdata, theaters, threat
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint, canonical_altitude
from godseye_uav.safety import AIRFRAMES, FuelModel
from godseye_uav.server import GodseyeUavServer, UavBackend
from godseye_uav.store import Store

_PORTS = list(range(53100, 53150))
_PORT = itertools.cycle(_PORTS[(os.getpid() + 25) % len(_PORTS):]
                        + _PORTS[:(os.getpid() + 25) % len(_PORTS)])
THEATER = theaters.get("default")
HOME = GeoPoint(THEATER.home_lat, THEATER.home_lon,
                canonical_altitude(THEATER.home_alt_msl_m, THEATER.home_lat,
                                   THEATER.home_lon, datum="msl").alt_hae)

#: The `/mcp` catalog at HEAD a6763a9 (46 tools).
HEAD_TOOLS = frozenset({
    "mission_cancel", "mission_dry_run", "mission_grid_search",
    "mission_handoff_track", "mission_identify_target", "mission_recon_route",
    "mission_status", "mission_threat_assessment", "mission_track_target",
    "sim_hydrate_real_data", "sim_move_target", "sim_reset",
    "sim_set_environment", "sim_set_fuel", "sim_set_gps_degradation",
    "sim_set_link_state", "sim_set_time", "sim_set_weather",
    "sim_spawn_order_of_battle", "sim_spawn_target", "uav_abort",
    "uav_assess_threat", "uav_capture_image", "uav_deconflict_airspace",
    "uav_fly_route", "uav_get_detections", "uav_get_telemetry", "uav_goto_gps",
    "uav_handoff_target", "uav_hover", "uav_identify_target", "uav_land",
    "uav_list_ob_classes", "uav_list_tracks", "uav_list_vehicles",
    "uav_los_check", "uav_mission", "uav_orbit_poi", "uav_real_data_status",
    "uav_return_to_home", "uav_scan_targets", "uav_set_fov", "uav_set_gimbal",
    "uav_takeoff", "uav_target_report", "uav_task_status",
})

#: The five tools Phase A adds (A7 registers them through `theater_tools`).
PHASE_A_TOOLS = frozenset({"geo_lookup", "geo_sites", "theater_propose",
                           "sim_set_theater", "sim_set_time_scale"})

#: sha256(description)[:16] of every HEAD tool. Descriptions do not depend on
#: the server's configuration (checked with real data, theater, airframe and
#: geodata all changed).
HEAD_DESCRIPTION_SHA256 = {
    "mission_cancel": "d7c88587b1e4459d",
    "mission_dry_run": "dbd6f2611d3effce",
    "mission_grid_search": "430453e9ed110419",
    "mission_handoff_track": "724e4b5bb2bb536f",
    "mission_identify_target": "52cac12446ce12e8",
    "mission_recon_route": "205bcd251b812d8d",
    "mission_status": "7c09cff6f28f444f",
    "mission_threat_assessment": "eca634cc53c11a64",
    "mission_track_target": "906c124f0f62be32",
    "sim_hydrate_real_data": "4a1369609dbdf4ff",
    "sim_move_target": "9683fe591e53e1d7",
    "sim_reset": "c8841be37155b6b5",
    "sim_set_environment": "0df68969d1b8d067",
    "sim_set_fuel": "dd9e86a68e696b1f",
    "sim_set_gps_degradation": "08d47d4e4b91d70f",
    "sim_set_link_state": "cbff7189e35e6628",
    "sim_set_time": "a96583aa55ffab6b",
    "sim_set_weather": "b8316498619fc9c3",
    "sim_spawn_order_of_battle": "5ebf87682f927adb",
    "sim_spawn_target": "1829e01daf268259",
    "uav_abort": "97fbf140d7dc9859",
    "uav_assess_threat": "d006f0cb57509b94",
    "uav_capture_image": "7e70750151617601",
    "uav_deconflict_airspace": "29667feb4323e4fd",
    "uav_fly_route": "68fc639f2c4432d9",
    "uav_get_detections": "adc8ba0d7ca71927",
    "uav_get_telemetry": "8d322217c0603c40",
    "uav_goto_gps": "f07ca72a5eff69f5",
    "uav_handoff_target": "4f82570ae33d0e47",
    "uav_hover": "3e5b9091aa094890",
    "uav_identify_target": "6392858077893b56",
    "uav_land": "847ffa8947030d9f",
    "uav_list_ob_classes": "fc74651d042cb276",
    "uav_list_tracks": "0dd5189dff80bd27",
    "uav_list_vehicles": "beb00a0cc8aa72b4",
    "uav_los_check": "150e3793717a0b0d",
    "uav_mission": "d8616578eed0e77c",
    "uav_orbit_poi": "91906875e6f234af",
    "uav_real_data_status": "9fe8ce8b6eb2adf5",
    "uav_return_to_home": "37e20485f6b89bde",
    "uav_scan_targets": "a5647f005febfa2d",
    "uav_set_fov": "3fd41fddfaf703b0",
    "uav_set_gimbal": "60e5148e7197c34f",
    "uav_takeoff": "56af89fac846d888",
    "uav_target_report": "ed9a84dbf012f7d5",
    "uav_task_status": "30a1e3ab3de1ffc7",
}

#: `uav://safety/geofence` top-level keys at HEAD (unchanged in Phase A).
HEAD_GEOFENCE_KEYS = frozenset({
    "bingo", "breach_doctrine", "ceiling_m_agl", "geofence", "geofence_warn_m",
    "home", "home_datums", "isr_only", "lost_link_plan", "max_speed_mps",
    "min_agl_m", "mission_flags", "real_data", "restart_recovery", "roe",
    "terrain_floor", "theater", "theater_mismatch", "units"})
HEAD_GEOFENCE_THEATER_KEYS = frozenset({"id", "label", "ao", "ground_elevation_msl_m"})
HEAD_ISR_ONLY = ("M14: no kinetic tool exists on this server and no engagement "
                 "recommendation is produced.")

#: A pre-flight gate dict (`_gate`, and a mission product's `gate`) at HEAD.
HEAD_GATE_KEYS = frozenset({
    "available_pct", "bingo_fuel_pct", "bingo_latched", "envelope_violations",
    "est_distance_m", "est_time_s", "home_configured", "ok", "plan_fuel_pct",
    "required_pct", "reserve_pct", "return_fuel_pct", "start",
    "start_alt_agl_datum", "start_alt_agl_m", "start_alt_agl_measured_m",
    "warnings", "wind_ne_mps", "wind_source"})

#: The fake's `environment()`, `Airframe.to_dict()` and `fuel_record()` at HEAD.
HEAD_ENVIRONMENT_KEYS = frozenset({
    "clock_speed", "det_false_neg", "det_false_pos", "det_geo_error_m",
    "det_geo_error_sigma_m", "detection_range_m", "gps_denied", "gps_noise_m",
    "is_day", "light_factor", "sim_tick_clamped", "sim_time", "sim_time_lost_s",
    "sun_azimuth_deg", "sun_elevation_deg", "weather", "wind"})
HEAD_AIRFRAME_KEYS = frozenset({
    "climb_rate_mps", "cruise_pct_per_s", "descend_rate_mps",
    "endurance_cruise_min", "endurance_cruise_s", "endurance_hover_min", "id",
    "phase_multipliers", "source", "summary", "wind_penalty_max",
    "wind_penalty_per_mps", "wind_ref_mps"})
HEAD_FUEL_RECORD_KEYS = frozenset({
    "airframe", "bingo_latched", "burned_pct", "clamped_ticks", "elapsed_s",
    "fuel_pct", "headwind_mps", "phase", "ticks", "unaccounted_s"})

HEAD_ISR_AUTHORITY_NOTE = (
    "ISR-only: this is a sensor-posture and self-protection advisory. "
    "godSeye has no engagement capability and confers no engagement authority; "
    "command decisions remain with the operator.")
HEAD_MAPPED_DATA_CAVEAT = (
    "MAPPED DATA, NOT AN ORDER OF BATTLE: sites come from OpenStreetMap/Overpass "
    "mapped features. The coverage is incomplete, the tagging is unverified, and "
    "nothing here is confirmed by observation. Use as ISR context only (M14); do "
    "not report it as a confirmed order of battle.")
HEAD_CHAT_STUB_PROMPT = (
    "You are the ISR analyst inside the Eye in the Sky console. ISR only: observe, "
    "classify and report; never plan or recommend engagement. The operator approves "
    "every command in the UI; never claim a command ran until its result says so.")


#: B5's Phase B rows (WG §5.4 test ports 53600-53699).
_B5_PORTS = list(range(53680, 53700))
_B5_PORT = itertools.cycle(_B5_PORTS[os.getpid() % len(_B5_PORTS):]
                           + _B5_PORTS[:os.getpid() % len(_B5_PORTS)])


@contextmanager
def isr_server(tmp_path, ports=None, port_cycle=None):
    """A default ISR server (no real data, no geodata) on a fake sim."""
    ports = _PORTS if ports is None else ports
    port_cycle = _PORT if port_cycle is None else port_cycle
    sim = srv = None
    for _ in range(len(ports)):
        sim = FakeAirSim(home=HOME, port=next(port_cycle))
        try:
            sim.start()
            break
        except OSError:
            sim.stop()
            sim = None
    assert sim is not None, f"no free port in {ports[0]}-{ports[-1]}"
    store = Store(tmp_path)
    try:
        client = airsim.MultirotorClient(port=sim.port)
        client.confirmConnection()
        srv = GodseyeUavServer(UavBackend(client, HOME, sim=sim), store, theater=THEATER)
        yield srv
    finally:
        if srv is not None:
            srv.stop_monitor()
            srv.tasking.shutdown()
        store.close()
        sim.stop()


def _tools(srv) -> dict:
    return srv.mcp._tool_manager._tools


# ----------------------------------------------------------- /mcp tools ---

def test_the_catalog_is_heads_46_plus_only_phase_a_tools(tmp_path):
    with isr_server(tmp_path) as srv:
        names = set(_tools(srv))
    assert len(HEAD_TOOLS) == 46 and len(PHASE_A_TOOLS) == 5
    assert names - PHASE_A_TOOLS == HEAD_TOOLS
    assert not any(n.startswith("wg_") for n in names)    # Phase B only


def test_the_catalog_is_exactly_51_once_theater_tools_is_merged(tmp_path):
    pytest.importorskip("godseye_uav.theater_tools",
                        reason="A7 registers the Phase A tools; not merged yet")
    with isr_server(tmp_path) as srv:
        assert set(_tools(srv)) == HEAD_TOOLS | PHASE_A_TOOLS


def test_every_head_tool_description_is_byte_identical(tmp_path):
    assert set(HEAD_DESCRIPTION_SHA256) == HEAD_TOOLS
    with isr_server(tmp_path) as srv:
        tools = _tools(srv)
        changed = sorted(
            n for n in HEAD_TOOLS
            if hashlib.sha256((tools[n].description or "").encode("utf-8"))
            .hexdigest()[:16] != HEAD_DESCRIPTION_SHA256[n])
    assert not changed, f"ISR tool descriptions changed: {changed}"


# ------------------------------------------------- uav://safety/geofence --

def test_the_geofence_adds_only_the_theater_epoch_and_dynamic_flag(tmp_path):
    async def read(srv):
        items = list(await srv.mcp.read_resource("uav://safety/geofence"))
        return json.loads(items[0].content)

    with isr_server(tmp_path) as srv:
        doc = asyncio.run(read(srv))
        # Phase B adds exactly `doctrine` at the top level (pinned below).
        assert set(doc) == HEAD_GEOFENCE_KEYS | {"doctrine"}
        assert set(doc["theater"]) == HEAD_GEOFENCE_THEATER_KEYS | {"epoch", "dynamic"}
        assert doc["theater"]["epoch"] == 0 and doc["theater"]["dynamic"] is False
        assert doc["theater"]["id"] == THEATER.id
        assert doc["isr_only"] == HEAD_ISR_ONLY
        srv.theater_epoch = 3
        assert asyncio.run(read(srv))["theater"]["epoch"] == 3


# ------------------------------------------------- gates and _submit -----

def test_gate_dicts_add_only_the_theater_epoch(tmp_path):
    wp = {"lat": THEATER.home_lat + 0.001, "lon": THEATER.home_lon, "alt_m": 20.0}
    route = [{"lat": THEATER.home_lat, "lon": THEATER.home_lon},
             {"lat": THEATER.home_lat + 0.002, "lon": THEATER.home_lon + 0.001}]

    async def main(srv):
        gate = await srv._gate("Drone1", [wp], 5.0)
        dry = await _tools(srv)["mission_recon_route"].fn(
            vehicle="Drone1", waypoints=route, dry_run=True)
        return gate, dry["gate"]

    with isr_server(tmp_path) as srv:
        gate, dry = asyncio.run(main(srv))
    assert set(gate) == HEAD_GATE_KEYS | {"theater_epoch"}
    assert set(dry) == HEAD_GATE_KEYS | {"theater_epoch"}


def test_submit_adds_only_the_two_theater_refusals(tmp_path):
    shape = {"rejected", "error", "message", "rejected_tool", "vehicle"}
    with isr_server(tmp_path) as srv:
        srv._switching.set()
        switching = srv._submit("Drone1", "uav_hover", {}, None)
        srv._switching.clear()
        stale = srv._submit("Drone1", "uav_hover", {}, None, gate_epoch=99)
        srv.theater_integrity_error = "copies disagree"
        # Land and hover pass the integrity latch (review A); a return home,
        # which flies to the origin, does not.
        broken = srv._submit("Drone1", "uav_return_to_home", {}, None)
    assert {switching["error"], stale["error"], broken["error"]} == {
        "theater_changed", "theater_integrity"}
    assert set(switching) == set(stale) == set(broken) == shape


# ------------------------------------------------ A2's scale surfaces -----

def test_scale_adds_only_label_cruise_speed_and_time_scale(tmp_path):
    for af in AIRFRAMES.values():
        assert set(af.to_dict()) == HEAD_AIRFRAME_KEYS | {"label", "cruise_speed_mps"}
    assert set(FuelModel().fuel_record()) == HEAD_FUEL_RECORD_KEYS | {"time_scale"}
    sim = FakeAirSim(home=HOME)        # never started: environment() is local
    assert set(sim.environment()) == HEAD_ENVIRONMENT_KEYS | {"time_scale"}


# --------------------------------------------------- byte-identical text --

def test_the_isr_authority_and_mapped_data_texts_are_the_head_literals():
    assert threat.ISR_AUTHORITY_NOTE == HEAD_ISR_AUTHORITY_NOTE
    assert realdata.MAPPED_DATA_CAVEAT == HEAD_MAPPED_DATA_CAVEAT


def test_the_chat_stub_prompt_is_the_head_literal(monkeypatch, tmp_path):
    """`chat._load_prompt` falls back to this stub when analyst_prompt.md is
    missing both as package data and beside the module."""
    import importlib.resources

    def missing(_package):
        raise FileNotFoundError("no package data")

    monkeypatch.setattr(importlib.resources, "files", missing)
    monkeypatch.setattr(chat, "__file__", str(tmp_path / "chat.py"))
    assert chat._load_prompt() == HEAD_CHAT_STUB_PROMPT


# =========================================================================
# Phase B rows (WG v2 §5.0; B5). ISR mode: no session, no --wargame-mcp.
# =========================================================================

HEAD_DOCTRINE = {
    "mode": "isr", "wargame_session": None, "wargame_mcp": False,
    "rule": ("M14a: ISR by default; simulated wargame tools exist only in an "
             "operator-approved session.")}
#: The `## Identity` section of analyst_prompt.md at Phase A (B7 moves it,
#: verbatim, into analyst_prompt_isr.md).
HEAD_ISR_IDENTITY = (
    "## Identity: ISR only\n\nThis system observes, classifies and reports. It has "
    "no weapons and you never reason about engaging,\nstriking, targeting for fires "
    "or prosecuting anything. If asked to attack, say plainly that this is an\nISR "
    "system and offer observation instead. Threat output is sensor-posture and "
    "self-protection advice\nonly (stand off, climb, change aspect, break contact), "
    "never an engagement recommendation.")
M14A_OFF_PARAGRAPH = (
    "The simulated wargame (M14a) is off in this session. If the operator asks to "
    "simulate an attack between simulated forces, offer to start one with "
    "`wg_session_start`, which they approve. Until then you have no wargame tools. "
    "Real places are never targets in any mode.")
WG_ENTRY_TOOLS = frozenset({"wg_session_start", "wg_session_status", "wg_list_classes"})


def _squash(text: str) -> str:
    return " ".join(text.split())


def test_phase_b_geofence_doctrine_is_isr_and_isr_only_is_unchanged(tmp_path):
    async def read(srv):
        items = list(await srv.mcp.read_resource("uav://safety/geofence"))
        return json.loads(items[0].content)

    with isr_server(tmp_path, _B5_PORTS, _B5_PORT) as srv:
        doc = asyncio.run(read(srv))
    assert doc["doctrine"] == HEAD_DOCTRINE
    assert doc["isr_only"] == HEAD_ISR_ONLY
    assert set(doc) - HEAD_GEOFENCE_KEYS == {"doctrine"}


def test_phase_b_submit_adds_only_the_vehicle_lost_refusal(tmp_path):
    shape = {"rejected", "error", "message", "rejected_tool", "vehicle"}
    with isr_server(tmp_path, _B5_PORTS, _B5_PORT) as srv:
        assert srv.vehicles_lost == {}                 # nothing lost in ISR mode
        srv.vehicles_lost["Drone1"] = {"by": "Red SAM 1", "at_ms": 0}
        lost = srv._submit("Drone1", "uav_hover", {}, None)
        other = srv._submit("Drone2", "uav_hover", {}, None)
    assert set(lost) == shape and lost["error"] == "vehicle_lost"
    assert other.get("status") == "accepted"


def test_phase_b_sim_spawn_target_refuses_a_duplicate_label(tmp_path):
    lat, lon = THEATER.home_lat + 0.002, THEATER.home_lon
    with isr_server(tmp_path, _B5_PORTS, _B5_PORT) as srv:
        spawn = srv.mcp._tool_manager._tools["sim_spawn_target"].fn
        first = asyncio.run(spawn(lat=lat, lon=lon, ob_class="supply_truck", name="t1"))
        again = asyncio.run(spawn(lat=lat + 0.002, lon=lon, ob_class="supply_truck",
                                  name="t1"))
        assert first["status"] == "accepted"
        assert again["error"] == "duplicate_name" and again["rejected"] is True
        assert set(again) == {"rejected", "error", "message"}
        assert srv.targets["t1"]["lat"] == lat            # never overwritten


def test_phase_b_mcp_has_no_wargame_tool_and_its_own_registry_is_unmounted(tmp_path):
    with isr_server(tmp_path, _B5_PORTS, _B5_PORT) as srv:
        assert not any(n.startswith("wg_") for n in _tools(srv))
        assert srv.wargame_mcp is not srv.mcp and srv.wargame_mcp_enabled is False
        assert srv.wargame.mode_key() == "isr"


def test_phase_b_intel_graph_meta_says_the_wargame_is_off(tmp_path):
    pytest.importorskip("godseye_uav.intel_scenario",
                        reason="B10 adds meta.wargame; not merged yet")
    from godseye_uav.intel_graph import IntelService

    class Ctx:
        def snapshot(self):
            return {"vehicles": [], "missions": [], "contacts": [], "feeds": {}}

        def theaters(self):
            return {"theaters": theaters.as_payload()["theaters"],
                    "active": theaters.active_unknown("not read yet")}

    with isr_server(tmp_path, _B5_PORTS, _B5_PORT) as srv:
        meta = IntelService(Ctx(), srv).graph()["meta"]
    assert meta["wargame"] == {"active": False, "last": None}


def test_phase_b_toolbelt_entry_tools_are_the_three(tmp_path):
    from godseye_uav import analyst_policy
    if not hasattr(analyst_policy, "WG_ENTRY_TOOLS"):
        pytest.skip("B7 adds WG_ENTRY_TOOLS; not merged yet")
    assert frozenset(analyst_policy.WG_ENTRY_TOOLS) == WG_ENTRY_TOOLS


def test_phase_b_the_isr_prompt_keeps_its_identity(tmp_path):
    import inspect
    import pathlib

    here = pathlib.Path(chat.__file__).with_name("analyst_prompt_isr.md")
    if not here.is_file():
        pytest.skip("B7 splits the analyst prompt; not merged yet")
    isr = here.read_text(encoding="utf-8")
    base = here.with_name("analyst_prompt.md").read_text(encoding="utf-8")
    assert _squash(isr).startswith(_squash(HEAD_ISR_IDENTITY))
    assert _squash(M14A_OFF_PARAGRAPH) in _squash(isr)
    assert "## Identity" not in base and "No engagement recommendations." in base
    params = inspect.signature(chat._load_prompt).parameters
    prompt = chat._load_prompt("isr") if params else chat._load_prompt()
    assert "ISR only" in prompt and _squash(HEAD_ISR_IDENTITY) in _squash(prompt)
