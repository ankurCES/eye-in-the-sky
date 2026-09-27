"""Intel graph (CONTRACT §4), its service/router, and the §6 bridge additions.

Fixtures mirror the LIVE payloads captured from a running stack (39 tracks:
the three Redmond ground-truth objects re-tracked in 10 runs each, plus three
contacts each left over in iran-isfahan, indo-pak-loc and red-sea-hormuz; a
parked Drone1 at 0 % fuel; the real SALUTE row shape from
`targets.salute_report`). The honesty rules are asserted against exactly the
store shape that motivated them.

Families:
  * pure `build_graph` - schema invariants, dedupe, theater scoping, threat
    honesty, timestamps, units, alarms, reports, caveats, size budgets;
  * `search_nodes` ranking;
  * bridge §6 - omitted-row threat fold, the summary THREATREP shape, locked
    copying accessors, numbered alarm history, `app.state.godseye`;
  * `IntelService` + `intel_router` over a real `create_app` bridge (fake MCP
    transport; no sim, no port 41451);
  * one end-to-end run against a REAL in-process `GodseyeUavServer` on a
    FakeAirSim in this module's port range (52400-52449).
"""
import asyncio
import json
import math
import time

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient
from godseye_uav import intel_graph as ig
from godseye_uav import theaters
from godseye_uav.bridge import (
    AirSimAdapter,
    Alarm,
    EventHub,
    McpClient,
    MissionFeed,
    VehicleSnapshot,
    create_app,
)
from godseye_uav.targets import OB_LIBRARY, Track
from godseye_uav.threat import assess_area

NOW_S = 1_790_487_743          # the live capture's wall clock
NOW_MS = NOW_S * 1000
#: This module's own port range (CONTRACT §0: 52100-52999, never 8790/8791/41451).
E2E_SIM_PORTS = range(52400, 52450)


def _nothing_listens(port: int) -> bool:
    """True when no loopback listener answers on ``port``. The range above is
    shared with other live checks, and a wildcard bind can SHADOW-share a port
    someone holds on 127.0.0.1 (macOS), so a successful bind proves nothing:
    the test's clients would then talk to the other process."""
    import socket

    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _unused_port() -> int:
    """A loopback port nothing listens on (ephemeral: never 8790/8791/41451)."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


#: Where bridge_app's adapter points: nothing is listening there.
UNUSED_SIM_PORT = _unused_port()

DEFAULT = theaters.get("default")
REDMOND_OBJECTS = [  # (equipment_name, ob_class, lat, lon) — live positions
    ("SA-6_site_1", "sam_medium_range", 47.642276, -122.138963),
    ("T72_column_1", "mbt", 47.640750, -122.138696),
    ("radar_1", "radar_acquisition", 47.642455, -122.141100),
]
FOREIGN = {  # theater -> [(equipment_name, ob_class, lat, lon)]
    "indo-pak-loc": [("SA-6_site_1", "sam_medium_range", 34.08081, 74.82098),
                     ("T72_column_1", "mbt", 34.07928, 74.82120),
                     ("radar_1", "radar_acquisition", 34.08098, 74.81924)],
    "red-sea-hormuz": [("SA-6_site_1", "sam_medium_range", 26.55081, 56.25090),
                       ("T72_column_1", "mbt", 26.54929, 56.25111),
                       ("radar_1", "radar_acquisition", 26.55098, 56.24930)],
    "iran-isfahan": [("command_post_1", "c2_node", 32.65580, 51.67096),
                     ("D30_howitzer_1", "towed_howitzer", 32.65428, 51.67118),
                     ("comms_relay_1", "comms_relay", 32.65598, 51.66925)],
}
RUNS = [f"R{i:02d}QFVTP02XV" for i in range(10)]   # 12-char run prefixes


def salute(track_id, equipment_name, ob_class, lat, lon, *, first_seen, last_seen,
           sightings=3, confidence="probable", score=0.41, members=None):
    """One `uav_list_tracks` row in the real `targets.salute_report` shape."""
    ob = OB_LIBRARY[ob_class]
    return {
        "format": "SALUTE", "track_id": track_id, "uid": track_id.lower(),
        "size": {"count": len(members or [track_id]), "element": ob.typical_unit_size,
                 "text": f"{len(members or [track_id])} x {ob.name}",
                 "members": list(members or [track_id]),
                 "basis": "same-category track(s) within 500 m"},
        "activity": {"code": "observed", "text": "observed, motion not yet resolved",
                     "speed_mps": None, "heading_deg": None, "dwell_s": 9.0,
                     "basis": "single fix"},
        "location": {"lat": lat, "lon": lon, "alt_m": 77.6,
                     "source": "contact detection geo_point",
                     "observer_position_used": False, "fix_time": int(last_seen),
                     "slant_range_m": 172.3},
        "unit": {"category": ob.category, "ob_class": ob_class,
                 "assessment": ob.typical_unit_size, "role": ob.role,
                 "text": f"{ob.name} — {ob.typical_unit_size}", "confidence": confidence},
        "time": {"epoch": int(last_seen), "iso": "2026-09-17T17:10:40Z",
                 "first_seen": int(first_seen), "last_seen": int(last_seen),
                 "age_s": round(NOW_S - last_seen, 1)},
        "equipment": {"platform": ob.name, "detected_as": equipment_name,
                      "capabilities": list(ob.capabilities),
                      "weapon_range_m": ob.weapon_range_m,
                      "weapon_ceiling_m": ob.weapon_ceiling_m,
                      "acquisition_range_m": ob.acquisition_range_m,
                      "mobility": ob.mobility, "signature_cues": list(ob.signature_cues),
                      "text": f"{ob.name} ({equipment_name})"},
        "confidence": {"level": confidence, "score": score, "score_tier": confidence,
                       "sighting_cap": "confirmed", "classification_cap": "confirmed",
                       "thresholds": {"confirmed": 0.7, "probable": 0.4},
                       "evidence": [
                           {"element": "independent_sightings", "value": sightings,
                            "source": f"track {track_id}: {sightings} correlated fix(es)",
                            "unit": "fixes", "weight": 0.24, "score": 0.4,
                            "contribution": 0.096},
                           {"element": "classification_specificity", "value": ob_class,
                            "source": f"order-of-battle keyword matched in {equipment_name}",
                            "weight": 0.16, "score": 1.0, "contribution": 0.16}]},
        "category": ob.category, "ob_class": ob_class, "confidence_level": confidence,
        "equipment_name": equipment_name, "lat": lat, "lon": lon, "observer": "UAV",
        "sightings": sightings, "speed_mps": None, "heading_deg": None,
    }


def contact(row, threat=None):
    """The compact `/snapshot.contacts[]` row the bridge builds from `row`."""
    return {"track_id": row["track_id"], "category": row["category"],
            "confidence": row["confidence_level"],
            "location": {"lat": row["lat"], "lon": row["lon"], "alt_m": 77.6},
            "last_seen_ms": row["time"]["last_seen"] * 1000, "threat_level": threat,
            "salute": {"size": row["size"]["text"], "activity": row["activity"]["text"],
                       "location": f"{row['lat']:.5f}, {row['lon']:.5f}", "unit": "",
                       "time": row["time"]["iso"], "equipment": row["equipment"]["text"]}}


def live_store():
    """The 39-track live store: 10 runs x 3 Redmond objects + 3 x 3 foreign."""
    rows = []
    for i, run in enumerate(RUNS):
        t0 = 1_789_665_031 + i * 900
        for seq, (name, ob, lat, lon) in enumerate(REDMOND_OBJECTS, start=1):
            rows.append(salute(f"TRK-{run}-{seq:04d}", name, ob, lat, lon,
                               first_seen=t0, last_seen=t0 + 9, sightings=3 + i))
    for k, (_theater, objs) in enumerate(sorted(FOREIGN.items())):
        run = f"F{k:02d}ABCDEFGHJ"
        for seq, (name, ob, lat, lon) in enumerate(objs, start=1):
            rows.append(salute(f"TRK-{run}-{seq:04d}", name, ob, lat, lon,
                               first_seen=1_790_016_381, last_seen=1_790_016_478,
                               sightings=17))
    return rows


def active_block(tid="default"):
    t = theaters.get(tid)
    return theaters.active_from_server(
        {"id": t.id, "label": t.label, "ao": [list(p) for p in t.ao],
         "ground_elevation_msl_m": t.home_alt_msl_m},
        source="mcp:uav://safety/geofence", at_ms=NOW_MS - 60_000)


def vehicle_row(name="Drone1", *, fuel=72.0, bingo=25.0, lat=None, lon=None,
                landed_state=1, mission="", track_id="", agl_real=False):
    snap = VehicleSnapshot(
        name=name, latitude=DEFAULT.home_lat if lat is None else lat,
        longitude=DEFAULT.home_lon if lon is None else lon, alt_hae=150.0, alt_msl=172.0,
        agl=50.0, speed_ms=8.0, heading_deg=90.0, vx=0.0, vy=8.0, vz=0.0,
        landed_state=landed_state, armed=True, timestamp_ms=NOW_MS - 100,
        fuel_pct=fuel, bingo_fuel_pct=bingo, eta_to_bingo_s=1200.0, mission=mission,
        track_id=track_id, fuel_source="measured burn rate between ticks (0.02 %/s)",
        alt_agl_m=50.0, alt_agl_is_real=agl_real,
        alt_agl_source="synthetic:launch-datum",
        alt_agl_reason=None if agl_real else "height above the LAUNCH DATUM")
    return dict(vars(snap))


def live_inputs(**over):
    rows = live_store()
    # The bridge's fold assessed the Redmond SAMs critical and nothing else yet.
    contacts = [contact(r, "critical" if r["ob_class"] == "sam_medium_range"
                        and r["lat"] > 40 else None) for r in rows]
    kw = {
        "vehicles": [vehicle_row()], "missions": [], "contacts": contacts, "tracks": rows,
        "theaters": theaters.as_payload()["theaters"], "active_theater": active_block(),
        "alarms": [], "mission_details": {}, "per_vehicle": {},
        "feeds": {"mission_state": {"ok": True, "atMs": NOW_MS - 500},
                  "contacts": {"ok": True, "atMs": NOW_MS - 500,
                               "detail": "mcp:uav_list_tracks"}},
        "real_data": {"enabled": False, "hydrated": False, "theater": "default"},
        "sim_state": "up", "now_ms": NOW_MS}
    kw.update(over)
    return ig.GraphInputs(**kw)


NODE_KEYS = {"id", "type", "label", "subtitle", "group", "salience", "status", "ts_ms",
             "lat", "lon", "attrs"}
STATUSES = {"ok", "warn", "critical", "stale", "unknown"}
EDGE_KINDS = {"flying", "tracking", "operating_in", "observes", "target", "member_of",
              "is_a", "in_theater", "near", "reports_on", "about"}


def assert_well_formed(g):
    assert g["schema"] == "godseye.intel-graph/v1"
    assert g["scope"] in ig.SCOPES
    ids = [n["id"] for n in g["nodes"]]
    assert len(ids) == len(set(ids)), "duplicate node ids"
    for n in g["nodes"]:
        assert set(n) == NODE_KEYS, n
        assert n["status"] in STATUSES, n
        assert 0.0 <= n["salience"] <= 1.0
        prefix = n["id"].split(":", 1)[0]
        assert ig.PREFIX_TYPE[prefix] == n["type"], n["id"]
        for k in ("lat", "lon"):
            assert n[k] is None or math.isfinite(n[k])
        assert n["ts_ms"] is None or isinstance(n["ts_ms"], int)
    node_ids = set(ids)
    for e in g["edges"]:
        assert set(e) == {"a", "b", "kind"}
        assert e["kind"] in EDGE_KINDS, e
        assert e["a"] in node_ids and e["b"] in node_ids, e
    # what the routes will actually serialize (Starlette refuses NaN)
    json.dumps(g, allow_nan=False)
    assert g["meta"]["counts"] == {t: sum(1 for n in g["nodes"] if n["type"] == t)
                                   for t in {n["type"] for n in g["nodes"]}}


# ===========================================================================
# build_graph (pure)
# ===========================================================================

class TestSchema:
    @pytest.mark.parametrize("scope", ig.SCOPES)
    def test_live_store_is_well_formed(self, scope):
        assert_well_formed(ig.build_graph(live_inputs(), scope=scope))

    def test_empty_inputs_are_well_formed(self):
        g = ig.build_graph(ig.GraphInputs(now_ms=NOW_MS))
        assert_well_formed(g)
        assert g["theater"]["known"] is False
        assert any("Active theater unknown" in c for c in g["meta"]["caveats"])

    def test_bad_scope_is_refused(self):
        with pytest.raises(ValueError):
            ig.build_graph(live_inputs(), scope="everything")

    def test_is_deterministic(self):
        a = ig.build_graph(live_inputs(), scope="all")
        b = ig.build_graph(live_inputs(), scope="all")
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


class TestDuplicates:
    def test_ten_runs_of_the_same_objects_collapse_to_three_contacts(self):
        g = ig.build_graph(live_inputs())
        tracks = [n for n in g["nodes"] if n["type"] == "track"]
        assert len(tracks) == 3, [n["label"] for n in tracks]
        newest = RUNS[-1]
        for n in tracks:
            assert newest in n["id"], "the representative must be the freshest run"
            assert n["attrs"]["duplicate_count"] == 9
            # newest first, capped: the store grows by one per run, forever
            assert len(n["attrs"]["duplicates"]) == ig.MAX_DUPLICATE_IDS
            assert n["attrs"]["duplicates"][0].split("-")[1] == RUNS[-2]
            assert all(d.split("-")[1] != newest for d in n["attrs"]["duplicates"])
            assert "10 runs" in n["subtitle"]
        m = g["meta"]
        assert m["duplicates_collapsed"] == 27
        assert m["tracks_total"] == 39
        assert m["tracks_total"] == len(tracks) + m["duplicates_collapsed"] + \
            m["out_of_theater"] + m["tracks_omitted"]
        assert any("27 duplicate track(s)" in c and "3 contact(s)" in c
                   for c in m["caveats"])

    def test_same_run_tracks_are_never_merged(self):
        a = salute("TRK-AAAAAAAAAAAA-0001", "T72_column_1", "mbt", 47.64, -122.14,
                   first_seen=NOW_S - 60, last_seen=NOW_S - 50)
        b = salute("TRK-AAAAAAAAAAAA-0002", "T72_column_1", "mbt", 47.64003, -122.14,
                   first_seen=NOW_S - 60, last_seen=NOW_S - 50)
        g = ig.build_graph(live_inputs(tracks=[a, b], contacts=[]))
        assert sum(1 for n in g["nodes"] if n["type"] == "track") == 2
        assert g["meta"]["duplicates_collapsed"] == 0

    @pytest.mark.parametrize("dlat,name,ob", [
        (0.0005, "T72_column_1", "mbt"),          # ~55 m apart: a different object
        (0.0, "BMP_column_1", "ifv"),             # same place, different equipment
    ])
    def test_distance_and_equipment_both_matter(self, dlat, name, ob):
        a = salute("TRK-AAAAAAAAAAAA-0001", "T72_column_1", "mbt", 47.64, -122.14,
                   first_seen=NOW_S - 600, last_seen=NOW_S - 590)
        b = salute("TRK-BBBBBBBBBBBB-0001", name, ob, 47.64 + dlat, -122.14,
                   first_seen=NOW_S - 60, last_seen=NOW_S - 50)
        g = ig.build_graph(live_inputs(tracks=[a, b], contacts=[]))
        assert sum(1 for n in g["nodes"] if n["type"] == "track") == 2

    def test_a_threat_level_on_any_run_survives_the_collapse(self):
        rows = live_store()
        old = rows[0]            # run 0's SA-6: the only assessed copy
        contacts = [contact(r, "critical" if r is old else None) for r in rows]
        b = ig._GraphBuilder(live_inputs(contacts=contacts), "theater")
        g = b.build()
        sam = next(n for n in g["nodes"] if n["attrs"].get("ob_class") == "sam_medium_range")
        assert sam["attrs"]["threat"] == "critical" and sam["status"] == "critical"
        assert b.cluster_by_node[sam["id"]].threat_from == old["track_id"]

    def test_duplicate_ids_resolve_to_their_contact(self):
        b = ig._GraphBuilder(live_inputs(), "theater")
        b.build()
        first_run = f"TRK-{RUNS[0]}-0001"
        assert b.resolve(first_run) == f"trk:TRK-{RUNS[-1]}-0001"
        assert b.resolve(f"trk:{first_run}") == f"trk:TRK-{RUNS[-1]}-0001"


class TestTheaterScope:
    def test_other_theaters_are_hidden_and_counted(self):
        g = ig.build_graph(live_inputs())
        assert g["theater"] == {"id": "default", "label": DEFAULT.label,
                                "place": DEFAULT.place, "known": True}
        assert g["meta"]["out_of_theater"] == 9
        assert g["meta"]["out_of_theater_contacts"] == 9
        assert g["meta"]["scoped_to_theater"] is True
        assert all(n["attrs"].get("theater") == "default"
                   for n in g["nodes"] if n["type"] == "track")
        assert [n["id"] for n in g["nodes"] if n["type"] == "theater"] == ["thr:default"]
        assert any("hidden in theater scope" in c for c in g["meta"]["caveats"])

    def test_scope_all_shows_them_labelled(self):
        g = ig.build_graph(live_inputs(), scope="all")
        tracks = [n for n in g["nodes"] if n["type"] == "track"]
        assert len(tracks) == 12
        by_theater = {}
        for n in tracks:
            by_theater.setdefault(n["attrs"]["theater"], 0)
            by_theater[n["attrs"]["theater"]] += 1
        assert by_theater == {"default": 3, "indo-pak-loc": 3, "red-sea-hormuz": 3,
                              "iran-isfahan": 3}
        assert sum(1 for n in tracks if n["attrs"].get("out_of_theater")) == 9
        assert g["meta"]["out_of_theater"] == 0
        assert len([n for n in g["nodes"] if n["type"] == "theater"]) == len(
            theaters.all_theaters())
        edges = {(e["a"], e["b"]) for e in g["edges"] if e["kind"] == "in_theater"}
        isfahan = next(n["id"] for n in tracks if n["attrs"]["theater"] == "iran-isfahan")
        assert (isfahan, "thr:iran-isfahan") in edges

    def test_a_contact_just_outside_the_ao_stays_flagged(self):
        south = min(p[0] for p in DEFAULT.ao)
        near = salute("TRK-NNNNNNNNNNNN-0001", "SA-6_site_2", "sam_medium_range",
                      south - 0.045, -122.14, first_seen=NOW_S - 60, last_seen=NOW_S - 50)
        far = salute("TRK-FFFFFFFFFFFF-0001", "SA-6_site_3", "sam_medium_range",
                     south - 0.5, -122.14, first_seen=NOW_S - 60, last_seen=NOW_S - 50)
        g = ig.build_graph(live_inputs(tracks=[near, far], contacts=[]))
        nodes = {n["id"]: n for n in g["nodes"] if n["type"] == "track"}
        assert set(nodes) == {"trk:TRK-NNNNNNNNNNNN-0001"}   # ~5 km out: kept
        assert nodes["trk:TRK-NNNNNNNNNNNN-0001"]["attrs"]["outside_ao"] is True
        assert g["meta"]["out_of_theater"] == 1               # ~55 km out: hidden

    def test_an_unknown_theater_is_never_guessed(self):
        unknown = theaters.active_unknown("uav://safety/geofence has not been read yet")
        g = ig.build_graph(live_inputs(active_theater=unknown))
        assert g["theater"]["known"] is False and g["theater"]["id"] is None
        assert g["meta"]["scoped_to_theater"] is False
        assert sum(1 for n in g["nodes"] if n["type"] == "track") == 12
        assert not [n for n in g["nodes"] if n["type"] == "theater"]
        assert any("Active theater unknown (uav://safety/geofence has not been read yet)"
                   in c for c in g["meta"]["caveats"])
        assert next(n for n in g["nodes"] if n["id"] == "feed:theater")["status"] == "warn"

    def test_vehicle_operating_in_only_when_inside(self):
        away = vehicle_row(name="Drone2", lat=10.0, lon=10.0)
        g = ig.build_graph(live_inputs(vehicles=[vehicle_row(), away]))
        edges = {(e["a"], e["b"], e["kind"]) for e in g["edges"]}
        assert ("veh:Drone1", "thr:default", "operating_in") in edges
        assert not any(a == "veh:Drone2" and k == "operating_in" for a, _b, k in edges)


class TestThreatHonesty:
    def test_unassessed_is_unknown_never_none(self):
        g = ig.build_graph(live_inputs())
        tracks = {n["attrs"]["ob_class"]: n for n in g["nodes"] if n["type"] == "track"}
        for ob in ("mbt", "radar_acquisition"):
            assert tracks[ob]["attrs"]["threat"] == "not assessed"
            assert tracks[ob]["status"] == "unknown"
        assert g["meta"]["threat_assessed"] == 1 and g["meta"]["threat_unassessed"] == 2
        assert any("'not assessed', never 'none'" in c for c in g["meta"]["caveats"])

    def test_a_real_none_is_shown_as_none(self):
        row = salute("TRK-AAAAAAAAAAAA-0001", "Ural_truck_1", "supply_truck", 47.64, -122.14,
                     first_seen=NOW_S - 60, last_seen=NOW_S - 50)
        g = ig.build_graph(live_inputs(tracks=[row], contacts=[contact(row, "none")]))
        n = next(n for n in g["nodes"] if n["type"] == "track")
        assert n["attrs"]["threat"] == "none" and n["status"] == "ok"

    @pytest.mark.parametrize("level,status", [("critical", "critical"), ("high", "warn"),
                                              ("moderate", "warn"), ("low", "ok")])
    def test_status_follows_the_level(self, level, status):
        row = salute("TRK-AAAAAAAAAAAA-0001", "SA-6_site_1", "sam_medium_range", 47.64,
                     -122.14, first_seen=NOW_S - 60, last_seen=NOW_S - 50)
        g = ig.build_graph(live_inputs(tracks=[row], contacts=[contact(row, level)]))
        assert next(n for n in g["nodes"] if n["type"] == "track")["status"] == status

    def test_stale_custody_is_marked(self):
        g = ig.build_graph(live_inputs())
        assert g["meta"]["stale_contacts"] == 3
        assert all(n["attrs"].get("stale") for n in g["nodes"] if n["type"] == "track")
        assert any("not re-fixed in over 15 min" in c for c in g["meta"]["caveats"])


MISSION_ROW = {"mission_id": "MSN-1a2b3c4d", "vehicle": "Drone1", "kind": "track_target",
               "phase": "executing", "active_tool": "uav_fly_route", "progress_pct": 43.5,
               "waypoint": {"index": 6, "of": 14}, "eta_s": 480.0, "fuel_pct": 72.0,
               "bingo_fuel_pct": 25.0, "coverage_pct": None,
               "coverage_basis": "not area-scored", "incomplete_reason": None,
               "safety": {"geofence": "ok", "proximity_m": None, "bingo_latched": False}}
GRID_ROW = {**MISSION_ROW, "mission_id": "MSN-99887766", "kind": "grid_search",
            "phase": "complete", "vehicle": "Drone2"}


class TestMissions:
    def test_no_fabricated_timestamps(self):
        g = ig.build_graph(live_inputs(missions=[MISSION_ROW]))
        m = next(n for n in g["nodes"] if n["type"] == "mission")
        assert m["ts_ms"] is None
        assert any("never an invented one" in c for c in g["meta"]["caveats"])

    def test_a_recorded_start_is_used(self):
        detail = {"mission_id": "MSN-1a2b3c4d", "started": NOW_S - 300.5, "meta": {}}
        g = ig.build_graph(live_inputs(missions=[MISSION_ROW],
                                       mission_details={"MSN-1a2b3c4d": detail}))
        m = next(n for n in g["nodes"] if n["type"] == "mission")
        assert m["ts_ms"] == int((NOW_S - 300.5) * 1000)
        assert m["attrs"]["ts_basis"].startswith("mission start")

    def test_edges_flying_target_tracking_observes(self):
        first_run_sam = f"TRK-{RUNS[0]}-0001"     # the mission names a DUPLICATE id
        detail_track = {"mission_id": "MSN-1a2b3c4d", "meta": {"track_id": first_run_sam}}
        poly = [[47.640, -122.142], [47.640, -122.138], [47.643, -122.138],
                [47.643, -122.142]]
        detail_grid = {"mission_id": "MSN-99887766",
                       "meta": {"_plan_params": {"polygon": poly}, "swath_m": 84.0}}
        g = ig.build_graph(live_inputs(
            vehicles=[vehicle_row(mission="MSN-1a2b3c4d", track_id=first_run_sam),
                      vehicle_row(name="Drone2")],
            missions=[MISSION_ROW, GRID_ROW],
            mission_details={"MSN-1a2b3c4d": detail_track, "MSN-99887766": detail_grid}))
        edges = {(e["a"], e["b"], e["kind"]) for e in g["edges"]}
        rep = f"trk:TRK-{RUNS[-1]}-0001"
        assert ("veh:Drone1", "msn:MSN-1a2b3c4d", "flying") in edges
        assert ("msn:MSN-1a2b3c4d", rep, "target") in edges
        assert ("veh:Drone1", rep, "tracking") in edges
        # GRID_ROW is COMPLETE: `flying` is present tense, so no edge (review
        # finding: every finished mission kept a `flying` link to its drone).
        assert ("veh:Drone2", "msn:MSN-99887766", "flying") not in edges
        observed = {b for a, b, k in edges if a == "msn:MSN-99887766" and k == "observes"}
        assert observed == {rep, f"trk:TRK-{RUNS[-1]}-0002", f"trk:TRK-{RUNS[-1]}-0003"}
        grid = next(n for n in g["nodes"] if n["id"] == "msn:MSN-99887766")
        assert grid["label"] == "Grid search · Drone2"
        assert grid["status"] == "ok"

    def test_only_a_mission_in_progress_is_flying(self):
        """Drone1 finished MSN-16e970d8 and is flying MSN-b912c9a0 (live shapes):
        only the running one is `flying`; a finished or aborted one is history."""
        done = {**MISSION_ROW, "mission_id": "MSN-16e970d8", "kind": "recon_route",
                "phase": "complete", "progress_pct": 100.0}
        aborted = {**MISSION_ROW, "mission_id": "MSN-0badc0de", "phase": "aborted"}
        rtb = {**MISSION_ROW, "mission_id": "MSN-0000abcd", "vehicle": "Drone2", "phase": "rtb"}
        running = {**MISSION_ROW, "mission_id": "MSN-b912c9a0", "kind": "recon_route"}
        g = ig.build_graph(live_inputs(
            vehicles=[vehicle_row(mission="MSN-b912c9a0"), vehicle_row(name="Drone2")],
            missions=[done, aborted, rtb, running]))
        flying = {(e["a"], e["b"]) for e in g["edges"] if e["kind"] == "flying"}
        assert flying == {("veh:Drone1", "msn:MSN-b912c9a0"),
                          ("veh:Drone2", "msn:MSN-0000abcd")}
        for mid in ("MSN-16e970d8", "MSN-0badc0de"):         # the nodes stay
            assert any(n["id"] == f"msn:{mid}" for n in g["nodes"])

    @pytest.mark.parametrize("waypoint,expected", [
        ({"index": 6, "of": 14}, {"index": 6, "of": 14}),
        ({"index": None, "of": 14}, {"of": 14}),       # only what is known
        ({"index": 3, "of": None}, {"index": 3}),
        ({"index": None, "of": None}, None),
        (None, None),
    ])
    def test_waypoint_position_when_known(self, waypoint, expected):
        g = ig.build_graph(live_inputs(missions=[{**MISSION_ROW, "waypoint": waypoint}]))
        m = next(n for n in g["nodes"] if n["type"] == "mission")
        assert m["attrs"].get("waypoint") == expected

    @pytest.mark.parametrize("patch,status", [
        ({"safety": {"geofence": "breach", "bingo_latched": False}}, "critical"),
        ({"safety": {"geofence": "ok", "bingo_latched": True}}, "critical"),
        ({"phase": "rtb"}, "warn"),
        ({"phase": "aborted", "incomplete_reason": "incomplete - fuel"}, "warn"),
    ])
    def test_mission_status(self, patch, status):
        g = ig.build_graph(live_inputs(missions=[{**MISSION_ROW, **patch}]))
        assert next(n for n in g["nodes"] if n["type"] == "mission")["status"] == status


class TestUnitsAndClasses:
    def test_units_count_deduplicated_contacts(self):
        rows = []
        for i, run in enumerate(RUNS):       # a 3-TEL battery, re-tracked in 10 runs
            for seq, dlat in enumerate((0.0, 0.0015, 0.003), start=1):
                rows.append(salute(f"TRK-{run}-{seq:04d}", f"SA-6_TEL_{seq}",
                                   "sam_medium_range", 47.640 + dlat, -122.14,
                                   first_seen=NOW_S - 900 + i, last_seen=NOW_S - 800 + i))
        g = ig.build_graph(live_inputs(tracks=rows, contacts=[]))
        units = [n for n in g["nodes"] if n["type"] == "unit"]
        assert len(units) == 1
        u = units[0]
        assert u["attrs"]["members"] == 3, "duplicates inflated the unit"
        assert u["id"] == f"unit:sam:TRK-{RUNS[-1]}-0001"
        assert u["label"].startswith("3 x medium-range SAM battery")
        assert "partial element (3 of a nominal 5)" == u["subtitle"]
        members = {e["a"] for e in g["edges"] if e["kind"] == "member_of"}
        assert len(members) == 3

    def test_a_mixed_class_unit_reads_its_category_as_an_acronym(self):
        rows = [salute("TRK-aa01-0001", "SA-6_TEL", "sam_medium_range", 47.640, -122.14,
                       first_seen=NOW_S - 900, last_seen=NOW_S - 800),
                salute("TRK-aa01-0002", "SA-15_TLAR", "sam_short_range", 47.641, -122.14,
                       first_seen=NOW_S - 900, last_seen=NOW_S - 800)]
        g = ig.build_graph(live_inputs(tracks=rows, contacts=[]))
        u = next(n for n in g["nodes"] if n["type"] == "unit")
        assert u["label"] == "SAM element (2 contacts)"      # never "Sam element"
        assert u["attrs"]["category"] == "sam"               # the machine key stays raw

    @pytest.mark.parametrize("word,human", [
        ("sam", "SAM"), ("c2", "C2"), ("aaa", "AAA"), ("ew", "EW"), ("mlrs", "MLRS"),
        ("orbit_poi", "Orbit POI"), ("geofence_breach", "Geofence breach"),
        ("real_data", "Real data"), ("", ""), (None, "")])
    def test_humanize_keeps_acronyms(self, word, human):
        assert ig._humanize(word) == human

    def test_isfahan_c2_pair_is_a_unit_in_scope_all(self):
        g = ig.build_graph(live_inputs(), scope="all")
        units = [n for n in g["nodes"] if n["type"] == "unit"]
        assert [u["attrs"]["category"] for u in units] == ["c2"]
        assert units[0]["attrs"]["members"] == 2

    def test_equipment_class_nodes(self):
        g = ig.build_graph(live_inputs())
        ob = {n["id"]: n for n in g["nodes"] if n["type"] == "equipment"}
        assert set(ob) == {"ob:sam_medium_range", "ob:mbt", "ob:radar_acquisition"}
        assert ob["ob:sam_medium_range"]["attrs"]["weapon_range_m"] == 24000.0
        assert ob["ob:sam_medium_range"]["group"] == "air-defense"
        assert sum(1 for e in g["edges"] if e["kind"] == "is_a") == 3

    def test_near_poi_edges(self):
        g = ig.build_graph(live_inputs())
        near = {(e["a"], e["b"]) for e in g["edges"] if e["kind"] == "near"}
        assert (f"trk:TRK-{RUNS[-1]}-0003", "poi:default:North Field") in near
        assert {e["a"] for e in g["edges"] if e["kind"] == "in_theater"
                and e["a"].startswith("poi:")} == {
            f"poi:default:{p.name}" for p in DEFAULT.pois}


class TestAlarmsReportsFeeds:
    def test_alarms_keep_the_newest_fifty_with_stable_ids(self):
        alarms = [{"kind": "detection", "severity": "info", "message": f"m{i}",
                   "atMs": NOW_MS - 1000 + i, "track_id": f"TRK-{RUNS[0]}-0001",
                   "seq": 1000 + i} for i in range(70)]
        alarms[-1] = {"kind": "bingo", "severity": "critical", "message": "BINGO",
                      "atMs": NOW_MS, "vehicle": "Drone1", "mission_id": "MSN-1a2b3c4d",
                      "seq": 1069}
        g = ig.build_graph(live_inputs(alarms=alarms, missions=[MISSION_ROW]))
        nodes = [n for n in g["nodes"] if n["type"] == "alarm"]
        assert len(nodes) == ig.MAX_ALARM_NODES
        assert nodes[0]["id"] == "alarm:1020" and nodes[-1]["id"] == "alarm:1069"
        assert nodes[-1]["status"] == "critical" and nodes[-1]["ts_ms"] == NOW_MS
        about = {(e["a"], e["b"]) for e in g["edges"] if e["kind"] == "about"}
        assert ("alarm:1069", "veh:Drone1") in about
        assert ("alarm:1069", "msn:MSN-1a2b3c4d") in about
        # an alarm about a duplicate id points at the contact it folded into
        assert ("alarm:1020", f"trk:TRK-{RUNS[-1]}-0001") in about

    def test_alarms_without_seq_get_positional_ids(self):
        alarms = [{"kind": "detection", "severity": "info", "message": str(i),
                   "atMs": NOW_MS} for i in range(3)]
        g = ig.build_graph(live_inputs(alarms=alarms))
        assert [n["id"] for n in g["nodes"] if n["type"] == "alarm"] == [
            "alarm:1", "alarm:2", "alarm:3"]

    def test_reports_and_reports_on(self):
        threatrep = {"format": "THREATREP", "count": 3, "highest_threat": "critical",
                     "report_id": "MSN-11112222",
                     "assessments": [{"track_id": f"TRK-{RUNS[0]}-0001",
                                      "threat_level": "critical"}],
                     "omitted": [{"track_id": f"TRK-{RUNS[3]}-0002", "threat_level": "low"},
                                 {"track_id": "TRK-F00ABCDEFGHJ-0001",    # out of theater
                                  "threat_level": "low"}]}
        intrep = {"format": "INTREP", "report_id": "latest", "as_of": NOW_S - 10,
                  "total_tracks": 39, "gaps": [{"type": "custody_lapsed"}],
                  "contacts": [{"track_id": f"TRK-{RUNS[-1]}-0003"}],
                  "contacts_omitted": []}
        g = ig.build_graph(live_inputs(reports={"MSN-11112222": threatrep,
                                                "latest": intrep}))
        reps = {n["id"]: n for n in g["nodes"] if n["type"] == "report"}
        assert set(reps) == {"rpt:MSN-11112222", "rpt:latest"}
        assert reps["rpt:MSN-11112222"]["status"] == "critical"
        assert reps["rpt:MSN-11112222"]["ts_ms"] is None      # THREATREP carries none
        assert reps["rpt:latest"]["ts_ms"] == (NOW_S - 10) * 1000
        on = {(e["a"], e["b"]) for e in g["edges"] if e["kind"] == "reports_on"}
        assert on == {("rpt:MSN-11112222", f"trk:TRK-{RUNS[-1]}-0001"),
                      ("rpt:MSN-11112222", f"trk:TRK-{RUNS[-1]}-0002"),
                      ("rpt:latest", f"trk:TRK-{RUNS[-1]}-0003")}

    def test_latest_alias_is_not_a_second_report(self):
        intrep = {"format": "INTREP", "report_id": "MSN-AAAA0000", "as_of": NOW_S}
        g = ig.build_graph(live_inputs(reports={"MSN-AAAA0000": intrep, "latest": intrep}))
        assert [n["id"] for n in g["nodes"] if n["type"] == "report"] == ["rpt:MSN-AAAA0000"]

    def test_real_data_off_and_feed_down_caveats(self):
        feeds = {"mission_state": {"ok": True, "atMs": NOW_MS},
                 "contacts": {"ok": False, "atMs": NOW_MS,
                              "error": "MCP unreachable: ConnectionRefusedError"}}
        g = ig.build_graph(live_inputs(feeds=feeds))
        cav = g["meta"]["caveats"]
        assert cav[0].startswith("Real-data layer is off: AGL is height above the launch "
                                 "datum")
        assert any(c.startswith("Feed contacts is down (MCP unreachable")
                   and "not a negative finding" in c for c in cav)
        feed = next(n for n in g["nodes"] if n["id"] == "feed:contacts")
        assert feed["status"] == "critical"
        assert g["meta"]["feeds"]["contacts"]["ok"] is False
        assert next(n for n in g["nodes"] if n["id"] == "feed:real_data")["status"] == "warn"

    def test_real_data_hydrated_names_assumed_agl(self):
        rd = {"enabled": True, "hydrated": True, "degraded_feeds": {"traffic": "timeout"}}
        g = ig.build_graph(live_inputs(real_data=rd))
        cav = g["meta"]["caveats"]
        assert any("Real-data feeds degraded (traffic)" in c for c in cav)
        assert any(c.startswith("AGL for Drone1 is height above the launch datum")
                   for c in cav)

    def test_real_data_falls_back_to_the_safety_resource(self):
        g = ig.build_graph(live_inputs(real_data=None,
                                       geofence={"real_data": {"enabled": False}}))
        assert g["meta"]["caveats"][0].startswith("Real-data layer is off")


class TestVehicles:
    @pytest.mark.parametrize("over,status", [
        ({}, "ok"),
        ({"fuel": 30.0, "bingo": 25.0}, "warn"),
        ({"fuel": 20.0, "bingo": 25.0}, "critical"),
        ({"fuel": None}, "unknown"),
    ])
    def test_fuel_vs_bingo(self, over, status):
        g = ig.build_graph(live_inputs(vehicles=[vehicle_row(**over)]))
        v = next(n for n in g["nodes"] if n["type"] == "vehicle")
        assert v["status"] == status

    def test_latched_bingo_from_the_server_is_critical(self):
        g = ig.build_graph(live_inputs(vehicle_status={
            "Drone1": {"bingo": {"tripped": True}, "link": {"state": "up"}}}))
        v = next(n for n in g["nodes"] if n["type"] == "vehicle")
        assert v["status"] == "critical" and v["attrs"]["bingo_latched"] is True
        assert "BINGO latched" in v["subtitle"] and v["attrs"]["link"] == "up"

    def test_lost_link_plan_when_known(self):
        g = ig.build_graph(live_inputs())
        assert "lost_link" not in next(n for n in g["nodes"] if n["type"] == "vehicle")["attrs"]
        default = {"behaviour": "rtb", "declare_after_s": 5.0, "restore_after_s": 2.0,
                   "orbit_radius_m": 200.0, "orbit_alt_m": None, "climb_to_m": 120.0,
                   "escalate_to_rtb_after_s": 300.0}
        g = ig.build_graph(live_inputs(geofence={"lost_link_plan": default}))
        ll = next(n for n in g["nodes"] if n["type"] == "vehicle")["attrs"]["lost_link"]
        assert ll == {"behaviour": "rtb", "declare_after_s": 5.0,
                      "escalate_to_rtb_after_s": 300.0, "climb_to_m": 120.0,
                      "source": "server default (uav://safety/geofence, as of boot)"}
        live = {**default, "behaviour": "hold_orbit"}
        g = ig.build_graph(live_inputs(geofence={"lost_link_plan": default}, vehicle_status={
            "Drone1": {"link": {"state": "up", "plan": live}}}))
        ll = next(n for n in g["nodes"] if n["type"] == "vehicle")["attrs"]["lost_link"]
        assert ll["behaviour"] == "hold_orbit" and ll["source"] == "vehicle"

    @pytest.mark.parametrize("state,status,since", [
        ("up", "ok", None),
        ("degraded", "warn", None),
        ("pending", "warn", NOW_MS - 3_000),      # down, dwell not expired yet
        ("loal", "critical", NOW_MS - 42_000),    # declared lost
    ])
    def test_link_state_and_since_when_the_link_went_down(self, state, status, since):
        link = {"state": state, "down_for_s": 0.0}
        if since is not None:
            link["down_since_ms"] = since
        g = ig.build_graph(live_inputs(vehicle_status={"Drone1": {"link": link}}))
        v = next(n for n in g["nodes"] if n["type"] == "vehicle")
        assert v["attrs"]["link"] == state and v["status"] == status
        assert v["attrs"].get("link_lost_since_ms") == since
        assert g["meta"]["feeds"]["sim"]["ok"] is True   # a vehicle link is not the sim

    def test_a_stray_down_since_on_an_up_link_is_not_a_loss(self):
        g = ig.build_graph(live_inputs(vehicle_status={
            "Drone1": {"link": {"state": "up", "down_since_ms": NOW_MS - 1}}}))
        v = next(n for n in g["nodes"] if n["type"] == "vehicle")
        assert "link_lost_since_ms" not in v["attrs"]

    def test_live_parked_drone_at_zero_fuel(self):
        row = vehicle_row(fuel=0.0, bingo=20.0, landed_state=0)
        g = ig.build_graph(live_inputs(vehicles=[row]))
        v = next(n for n in g["nodes"] if n["type"] == "vehicle")
        assert v["status"] == "critical"
        assert v["subtitle"] == "fuel 0% / BINGO 20% · landed"
        assert v["attrs"]["margin_pct"] == -20.0 and v["attrs"]["agl_is_real"] is False

    def test_a_vehicle_known_only_to_the_mission_feed(self):
        g = ig.build_graph(live_inputs(vehicles=[], per_vehicle={
            "Drone3": {"fuel_pct": 50.0, "bingo_fuel_pct": 20.0, "mission": ""}}))
        v = next(n for n in g["nodes"] if n["id"] == "veh:Drone3")
        assert v["status"] == "unknown" and v["lat"] is None


class TestBudgets:
    def test_one_hundred_tracks_fit_in_150_kb(self):
        rows, contacts = [], []
        south, west = DEFAULT.bounds()[0], DEFAULT.bounds()[1]
        classes = list(OB_LIBRARY)
        for k in range(100):
            ob = classes[k % len(classes)]
            lat = south + 0.002 + 0.0035 * (k // 10)
            lon = west + 0.002 + 0.0055 * (k % 10)
            for r, run in enumerate(RUNS):         # each re-tracked in 10 runs
                row = salute(f"TRK-{run}-{k:04d}", f"{ob}_{k}", ob, lat, lon,
                             first_seen=NOW_S - 5000 + r, last_seen=NOW_S - 4000 + r,
                             members=[f"TRK-{x}-{k:04d}" for x in RUNS])
                rows.append(row)
                contacts.append(contact(row, ["critical", "high", None, "low"][k % 4]))
        alarms = [{"kind": "detection", "severity": "info", "message": "new contact " * 5,
                   "atMs": NOW_MS, "track_id": f"TRK-{RUNS[0]}-{i:04d}", "seq": i + 1}
                  for i in range(100)]
        vehicles = [vehicle_row(name=f"Drone{i}") for i in range(1, 4)]
        missions = [{**MISSION_ROW, "mission_id": f"MSN-0000000{i}"} for i in range(5)]
        g = ig.build_graph(live_inputs(tracks=rows, contacts=contacts, alarms=alarms,
                                       vehicles=vehicles, missions=missions), scope="all")
        assert_well_formed(g)
        assert sum(1 for n in g["nodes"] if n["type"] == "track") == 100
        assert g["meta"]["duplicates_collapsed"] == 900
        size = ig.json_size(g)
        assert size <= ig.GRAPH_TARGET_BYTES, f"{size} B for 100 tracks"

    def test_track_nodes_are_capped_and_counted(self):
        rows = [salute(f"TRK-CAPCAPCAPCAP-{k:04d}", f"truck_{k}", "supply_truck",
                       DEFAULT.home_lat + 0.0003 * (k % 60),
                       DEFAULT.home_lon + 0.0004 * (k // 60),
                       first_seen=NOW_S - 100, last_seen=NOW_S - 50)
                for k in range(ig.MAX_TRACK_NODES + 25)]
        g = ig.build_graph(live_inputs(tracks=rows, contacts=[]))
        assert sum(1 for n in g["nodes"] if n["type"] == "track") == ig.MAX_TRACK_NODES
        assert g["meta"]["tracks_omitted"] == 25
        assert any("counted but not drawn" in c for c in g["meta"]["caveats"])

    def test_fit_to_budget_says_what_it_cut(self):
        big = {"id": "x", "fields": {"waypoints": [{"lat": i, "lon": i} for i in range(5000)],
                                     "note": "n" * 10},
               "raw": {"blob": "z" * 90_000}}
        out = ig.fit_to_budget(big, 20_000)
        assert ig.json_size(out) <= 20_000
        assert "raw" not in out and out["_truncated"]["dropped"] == ["raw"]
        marker = out["fields"]["waypoints"][-1]
        assert marker["_truncated"] is True
        assert marker["_omitted"] + len(out["fields"]["waypoints"]) - 1 == 5000

    def test_fit_to_budget_counts_its_own_marker(self):
        """The `_truncated` report is part of the response: the result WITH it
        must be within max_bytes (it used to be added after the size check, so
        a body trimmed to the byte limit came back ~30-80 bytes over)."""
        for n in (40, 200, 900):
            doc = {"id": "x", "fields": {"rows": [{"i": i, "pad": "p" * 30}
                                                  for i in range(n)]},
                   "raw": {"blob": "z" * 3_000}}
            size = ig.json_size(doc)
            for budget in (size - 1, size - 3_000, size - 3_050, size // 2, 1_500):
                if budget < 600:
                    continue
                out = ig.fit_to_budget(doc, budget)
                assert ig.json_size(out) <= budget, (n, budget)
                assert "_truncated" in out

    def test_fit_to_budget_scrubs_non_finite_numbers(self):
        out = ig.fit_to_budget({"a": float("nan"), "b": [float("inf"), 1.0]}, 1000)
        assert out == {"a": None, "b": [None, 1.0]}


# ===========================================================================
# search
# ===========================================================================

class TestSearch:
    @pytest.fixture()
    def built(self):
        b = ig._GraphBuilder(live_inputs(missions=[MISSION_ROW]), "all")
        b.build()
        return b

    def test_exact_id_beats_prefix_beats_tokens(self, built):
        rep = f"TRK-{RUNS[-1]}-0001"
        hits = ig.search_nodes(built.graph, rep, aliases=built.alias)
        assert hits[0]["id"] == f"trk:{rep}" and hits[0]["score"] >= 1000
        hits = ig.search_nodes(built.graph, f"trk:TRK-{RUNS[-1]}", aliases=built.alias)
        assert {h["id"] for h in hits[:3]} == {f"trk:TRK-{RUNS[-1]}-000{i}" for i in (1, 2, 3)}
        assert all(h["score"] >= 800 for h in hits[:3])

    def test_a_duplicate_id_finds_its_contact(self, built):
        hits = ig.search_nodes(built.graph, f"TRK-{RUNS[2]}-0002", aliases=built.alias)
        assert hits[0]["id"] == f"trk:TRK-{RUNS[-1]}-0002"

    def test_tokens_are_case_insensitive_and_all_must_match(self, built):
        hits = ig.search_nodes(built.graph, "SAM battery", aliases=built.alias)
        assert hits and all("sam" in h["label"].lower() for h in hits)
        # in-theater first, foreign copies after, flagged
        assert hits[0]["id"] == f"trk:TRK-{RUNS[-1]}-0001" or hits[0]["type"] != "track"
        tracks = [h for h in hits if h["type"] == "track"]
        assert not tracks[0].get("out_of_theater")
        assert any(h.get("out_of_theater") for h in tracks)

    def test_other_theaters_and_their_pois_are_flagged_out_of_theater(self, built):
        """Active theater is Redmond; 'donbas' finds Ukraine's POIs and theater,
        and every one of them says it is outside the active AO."""
        g = built.graph
        byid = {n["id"]: n for n in g["nodes"]}
        assert not byid["thr:default"]["attrs"].get("out_of_theater")
        assert all(not n["attrs"].get("out_of_theater") for n in g["nodes"]
                   if n["type"] == "poi" and n["attrs"].get("theater") == "default")
        foreign = [n for n in g["nodes"] if n["type"] in ("poi", "theater")
                   and n["id"] not in ("thr:default",)
                   and not n["id"].startswith("poi:default:")]
        assert foreign and all(n["attrs"].get("out_of_theater") is True for n in foreign)
        hits = ig.search_nodes(g, "donbas", aliases=built.alias)
        donbas = [h for h in hits if "ukraine-donbas" in h["id"]]
        assert donbas and all(h.get("out_of_theater") for h in donbas), hits

    def test_type_filter_takes_names_and_prefixes(self, built):
        for types in (["track"], ["trk"], ["tracks"], ["contacts"]):
            hits = ig.search_nodes(built.graph, "sa-6", types=types)
            assert hits and {h["type"] for h in hits} == {"track"}, types
        hits = ig.search_nodes(built.graph, "drone1", types=["vehicle", "msn"])
        assert {h["type"] for h in hits} <= {"vehicle", "mission"}
        assert hits[0]["id"] == "veh:Drone1"

    def test_any_token_fallback_and_limits(self, built):
        hits = ig.search_nodes(built.graph, "radar zzzqqq")
        assert hits and any("radar" in h["label"].lower() for h in hits)
        assert ig.search_nodes(built.graph, "") == []
        assert ig.search_nodes(built.graph, "   ") == []
        assert len(ig.search_nodes(built.graph, "field", limit=2)) == 2
        assert len(ig.search_nodes(built.graph, "t", limit=10_000)) <= 100

    def test_attrs_and_subtitles_are_searched(self, built):
        hits = ig.search_nodes(built.graph, "T72_column_1")
        assert hits[0]["type"] == "track"
        hits = ig.search_nodes(built.graph, "executing")
        assert hits[0]["id"] == "msn:MSN-1a2b3c4d"


# ===========================================================================
# bridge §6 additions
# ===========================================================================

class FakeTransport(McpClient):
    """Loop C's seam, stubbed (same approach as tests/test_bridge.py)."""

    def __init__(self, *, tracks=(), threat=None, mission_doc=None, geofence=None,
                 current=None, fail_tracks=False):
        super().__init__("fake://mcp", "t")
        self.tracks, self.threat, self.mission_doc = list(tracks), threat, mission_doc
        self.geofence, self.current, self.fail_tracks = geofence, current, fail_tracks
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append(name)
        if name == "uav_task_status":
            tick = {"t": 1.0, "fuel_pct": 72.0, "violations": [], "link": {"state": "up"},
                    "bingo": {"bingo_fuel_pct": 25.0, "latched": False},
                    "alt_agl_m": 50.0, "alt_agl_is_real": False,
                    "alt_agl_source": "synthetic:launch-datum"}
            return {"vehicle": arguments["vehicle"], "state": "running",
                    "current": self.current, "fuel_pct": 72.0, "bingo_latched": False,
                    "mission_status": None, "last_tick": tick}, None
        if name == "mission_status":
            return ({"mission_handle": self.current["mission_id"], "vehicle": "Drone1",
                     "kind": "track_target", "state": "running", "progress_pct": 43.5}
                    if self.current else {}), None
        if name == "uav_list_tracks":
            if self.fail_tracks:
                return None, "uav_list_tracks: MCP unreachable"
            return {"count": len(self.tracks), "tracks": self.tracks}, None
        if name == "uav_assess_threat":
            return (self.threat, None) if self.threat else (None, "no assessment")
        return None, f"{name}: not stubbed"

    def read_resource(self, uri):
        if uri.startswith("uav://mission/") and self.mission_doc:
            return {"mission": self.mission_doc}, None
        if uri == "uav://safety/geofence" and self.geofence:
            return self.geofence, None
        return None, f"{uri}: not served"


def real_summary_threatrep(rows, top_n):
    """What `uav_assess_threat` really returns: threat.assess_area, summary shape."""
    tracks = [Track(track_id=r["track_id"], name=r["equipment_name"],
                    category=r["category"], ob_class=r["ob_class"], lat=r["lat"],
                    lon=r["lon"], alt_m=77.6, first_seen=r["time"]["first_seen"],
                    last_seen=r["time"]["last_seen"], sightings=r["sightings"])
              for r in rows]
    rep = assess_area(tracks, observer={"lat": DEFAULT.home_lat, "lon": DEFAULT.home_lon,
                                        "alt_m": 150.0},
                      area_polygon=list(DEFAULT.ao), detail="summary", top_n=top_n,
                      now=NOW_S)
    return json.loads(json.dumps(rep))     # over the wire


def fed(transport, vehicles=("Drone1",)):
    hub = EventHub()
    return MissionFeed(transport, hub, vehicles=lambda: list(vehicles),
                       flown=lambda v: []), hub


class TestBridgeThreatFold:
    def test_omitted_rows_get_their_threat_level(self):
        rows = live_store()[:30]                       # the 30 Redmond tracks
        rep = real_summary_threatrep(rows, top_n=10)
        assert len(rep["assessments"]) == 10 and rep["omitted_count"] == 20
        feed, _ = fed(FakeTransport(tracks=rows, threat=rep))
        contacts = feed.poll_once().contacts
        assert len(contacts) == 30
        missing = [c["track_id"] for c in contacts if not c["threat_level"]]
        assert not missing, f"{len(missing)} assessed contacts read as unassessed"
        omitted = {r["track_id"]: r["threat_level"] for r in rep["omitted"]}
        for c in contacts:
            if c["track_id"] in omitted:
                assert c["threat_level"] == omitted[c["track_id"]]

    def test_rings_come_from_the_summary_and_the_track_row(self):
        rows = live_store()[:3]
        rep = real_summary_threatrep(rows, top_n=10)
        assert "assessment" not in rep["assessments"][0]  # the summary shape
        feed, _ = fed(FakeTransport(tracks=rows, threat=rep))
        feed.poll_once()
        rings = feed.threat_rings()
        sam = rows[0]["track_id"]
        assert rings[sam]["engagement"] == OB_LIBRARY["sam_medium_range"].weapon_range_m
        assert rings[sam]["acquisition"] == OB_LIBRARY["sam_medium_range"].acquisition_range_m

    def test_test_bridge_fixture_is_the_real_summary_shape(self):
        """tests/test_bridge.py's THREATREP must not drift back to the full
        nested shape that hid the live defects."""
        from test_bridge import THREATREP
        real = real_summary_threatrep(live_store()[:12], top_n=10)
        assert set(THREATREP) == set(real)
        assert set(THREATREP["assessments"][0]) == set(real["assessments"][0])
        assert set(real["omitted"][0]) >= {"track_id", "threat_level"}


class TestBridgeAccessors:
    def _feed(self, **kw):
        rows = live_store()[:3]
        doc = {"mission_id": "MSN-1a2b3c4d", "vehicle": "Drone1", "kind": "track_target",
               "started": NOW_S - 30, "waypoints": [{"lat": 1, "lon": 2}],
               "meta": {"track_id": rows[0]["track_id"]}}
        fence = {"geofence": [list(p) for p in DEFAULT.ao],
                 "theater": {"id": "default", "label": DEFAULT.label,
                             "ao": [list(p) for p in DEFAULT.ao],
                             "ground_elevation_msl_m": 122.0},
                 "real_data": {"enabled": False, "hydrated": False}}
        current = {"task_id": "t1", "tool": "uav_fly_route", "state": "running",
                   "mission_id": "MSN-1a2b3c4d", "progress_pct": 10.0}
        t = FakeTransport(tracks=rows, threat=real_summary_threatrep(rows, 10),
                          mission_doc=doc, geofence=fence, current=current, **kw)
        feed, hub = fed(t)
        feed._geofence_at = -1e9      # read the safety resource on the first poll
        feed.poll_once()
        return feed, hub, t

    def test_accessors_return_copies(self):
        feed, _, _ = self._feed()
        md = feed.mission_details()
        assert md["MSN-1a2b3c4d"]["started"] == NOW_S - 30
        md["MSN-1a2b3c4d"]["started"] = 0
        md.clear()
        assert feed.mission_details()["MSN-1a2b3c4d"]["started"] == NOW_S - 30
        rings = feed.threat_rings()
        rings.clear()
        assert feed.threat_rings()
        doc = feed.geofence_doc()
        doc["theater"]["id"] = "tampered"
        assert feed.geofence_doc()["theater"]["id"] == "default"
        assert feed.active_theater()["id"] == "default"
        rows = feed.track_rows()
        assert len(rows) == 3 and rows[0]["format"] == "SALUTE"
        rows.clear()
        assert len(feed.track_rows()) == 3

    def test_track_rows_empty_with_the_contacts_when_the_tool_fails(self):
        feed, _, t = self._feed()
        t.fail_tracks = True
        intel = feed.poll_once()
        assert intel.contacts == [] and feed.track_rows() == []
        assert intel.feeds["contacts"].ok is False

    def test_accessors_before_any_poll(self):
        feed, _ = fed(FakeTransport())
        assert feed.mission_details() == {} and feed.threat_rings() == {}
        assert feed.geofence_doc() is None and feed.track_rows() == []


class TestEventHubNumbering:
    def test_sequence_numbers_survive_the_history_window(self):
        hub = EventHub(history=100)
        for i in range(130):
            hub.publish(Alarm(kind="detection", message=f"m{i}", track_id=f"T{i}"))
        got = hub.recent_numbered(5)
        assert [s for s, _ in got] == [126, 127, 128, 129, 130]
        assert got[-1][1]["message"] == "m129"
        assert hub.recent_numbered(500)[0][0] == 31
        assert hub.recent_numbered(0) == [] and hub.recent_numbered(-3) == []


def bridge_app(transport=None, feed_cls=MissionFeed):
    """A real create_app bridge, loops off, adapter on an unused port (never
    41451), loop C fed by `transport`."""
    app = create_app(adapter=AirSimAdapter(ip="127.0.0.1", port=UNUSED_SIM_PORT),
                     token="intel-test-token", start_loops=False)
    if transport is not None:
        feed = feed_cls(transport, app.state.hub, vehicles=lambda: ["Drone1"],
                        flown=app.state.bridge.flown)
        feed._geofence_at = -1e9
        app.state.bridge.feed = feed
        app.state.feed = feed
    return app


class TestGodseyeContext:
    def test_namespace_is_attached_without_new_routes(self):
        app = bridge_app()
        ctx = app.state.godseye
        for name in ("state", "hub", "feed", "mcp", "adapter", "active_theater", "token",
                     "snapshot", "theaters", "intel", "track_rows", "mission_details",
                     "threat_rings", "geofence_doc", "recent_events", "sim_state"):
            assert hasattr(ctx, name), name
        assert ctx.token == "intel-test-token"
        assert ctx.hub is app.state.hub and ctx.state is app.state.bridge
        paths = {getattr(r, "path", "") for r in app.routes}
        assert not any(p.startswith(("/intel", "/chat", "/app")) for p in paths)
        post = {r.path for r in app.routes if "POST" in (getattr(r, "methods", None) or ())}
        assert post == {"/control/mission", "/control/command", "/camera/subscribe"}
        # the token never rides in a payload the namespace produces
        assert "intel-test-token" not in json.dumps(ctx.snapshot(), default=str)
        assert "intel-test-token" not in json.dumps(ctx.theaters(), default=str)

    def test_callables_read_the_current_feed(self):
        rows = live_store()[:3]
        app = bridge_app(FakeTransport(tracks=rows))
        app.state.bridge.feed.poll_once()
        ctx = app.state.godseye
        assert len(ctx.track_rows()) == 3
        assert len(ctx.snapshot()["contacts"]) == 3

        class Legacy:     # a state source that predates the §6 accessors
            polls = 0

            def intel(self):
                from godseye_uav.bridge import MissionIntel
                return MissionIntel()

        app.state.bridge.feed = Legacy()
        assert ctx.track_rows() == [] and ctx.mission_details() == {}
        assert ctx.geofence_doc() is None and ctx.threat_rings() == {}
        assert ctx.snapshot()["contacts"] == []

    def test_recent_events_carry_seq(self):
        app = bridge_app()
        for i in range(3):
            app.state.hub.publish(Alarm(kind="mission_phase", message=str(i),
                                        mission_id="MSN-1"))
        ev = app.state.godseye.recent_events(2)
        assert [e["seq"] for e in ev] == [2, 3] and ev[-1]["kind"] == "mission_phase"
        assert len(app.state.godseye.recent_events(10_000)) == 3


# ===========================================================================
# IntelService + router over a real bridge
# ===========================================================================

@pytest.fixture()
def service():
    rows = live_store()
    rep = real_summary_threatrep(rows, top_n=10)
    doc = {"mission_id": "MSN-1a2b3c4d", "vehicle": "Drone1", "kind": "track_target",
           "task_id": "t1", "state": "executing", "started": NOW_S - 120.0,
           "waypoints": [{"lat": 47.64 + i * 1e-5, "lon": -122.14, "alt_agl_m": 60.0}
                         for i in range(4000)],
           "meta": {"track_id": rows[0]["track_id"]}, "gate": {"ok": True},
           "coverage": {"coverage_pct": None}, "phases": [], "warnings": []}
    fence = {"geofence": [list(p) for p in DEFAULT.ao], "ceiling_m_agl": 120.0,
             "min_agl_m": 3.0, "max_speed_mps": 20.0, "roe": "stricter only",
             "isr_only": True,
             "theater": {"id": "default", "label": DEFAULT.label,
                         "ao": [list(p) for p in DEFAULT.ao], "ground_elevation_msl_m": 122.0},
             "real_data": {"enabled": False, "hydrated": False}}
    current = {"task_id": "t1", "tool": "uav_fly_route", "state": "running",
               "mission_id": "MSN-1a2b3c4d", "progress_pct": 43.5}
    app = bridge_app(FakeTransport(tracks=rows, threat=rep, mission_doc=doc, geofence=fence,
                                   current=current))
    app.state.bridge.feed.poll_once()
    snap = VehicleSnapshot(**{k: v for k, v in vehicle_row(mission="MSN-1a2b3c4d").items()})
    app.state.bridge.enrich(snap)
    with app.state.bridge._lock:
        app.state.bridge._cache["Drone1"] = snap
    for i in range(120):
        app.state.hub.publish(Alarm(kind="detection", message=f"new contact {i}",
                                    track_id=rows[i % len(rows)]["track_id"]))
    svc = ig.IntelService(app.state.godseye)
    svc.clock = lambda: NOW_S
    return svc, app


class TestIntelService:
    def test_graph_from_the_bridge(self, service):
        svc, _ = service
        g = svc.graph()
        assert_well_formed(g)
        m = g["meta"]
        assert m["tracks_total"] == 39 and m["out_of_theater"] == 9
        assert m["duplicates_collapsed"] == 27
        assert m["threat_assessed"] == 3, "the omitted fold should assess every contact"
        types = m["counts"]
        assert types["vehicle"] == 1 and types["mission"] == 1 and types["alarm"] == 50
        mission = next(n for n in g["nodes"] if n["type"] == "mission")
        assert mission["ts_ms"] == int((NOW_S - 120.0) * 1000)
        edges = {(e["a"], e["b"], e["kind"]) for e in g["edges"]}
        assert ("msn:MSN-1a2b3c4d", f"trk:TRK-{RUNS[-1]}-0001", "target") in edges
        assert svc.graph() is g, "cached within the TTL"
        svc.cache_ttl_s = 0.0
        assert svc.graph() is not g

    def test_every_entity_fits_and_resolves(self, service):
        svc, _ = service
        g = svc.graph(scope="all")
        for n in g["nodes"]:
            e = svc.entity(n["id"])
            assert e is not None and e["id"] == n["id"] and e["type"] == n["type"], n["id"]
            assert ig.json_size(e) <= ig.ENTITY_MAX_BYTES, n["id"]
            json.dumps(e, allow_nan=False)
            assert isinstance(e["fields"], dict) and isinstance(e["provenance"], dict)
        assert svc.entity("trk:does-not-exist") is None
        assert svc.entity("") is None

    def test_track_entity_is_full_salute_with_provenance(self, service):
        svc, _ = service
        dup = f"TRK-{RUNS[0]}-0001"
        e = svc.entity(dup)
        assert e["id"] == f"trk:TRK-{RUNS[-1]}-0001" and e["requested_id"] == dup
        f = e["fields"]
        assert set(f["salute"]) == {"size", "activity", "location", "unit", "time",
                                    "equipment"}
        assert len(f["duplicates"]) == 9 and f["custody_lapsed"] is True
        assert f["threat"] in ig.THREAT_RANK
        assert f["threat_rings_m"]["engagement"] == 24000.0
        p = e["provenance"]
        assert p["location_source"] == "contact detection geo_point"
        assert p["observer_position_used"] is False
        assert "WHICH vehicle" in p["observer"]
        assert p["confidence_basis"][0]["element"] == "independent_sightings"
        assert "not a current position" in p["position"]
        assert e["raw"]["format"] == "SALUTE"
        kinds = {r["kind"] for r in e["related"]}
        assert {"is_a", "in_theater", "target"} <= kinds

    def test_mission_entity_caps_waypoints(self, service):
        svc, _ = service
        e = svc.entity("msn:MSN-1a2b3c4d")
        f = e["fields"]
        assert f["waypoints"]["count"] == 4000 and len(f["waypoints"]["first"]) == 5
        assert f["started_ms"] == int((NOW_S - 120.0) * 1000)
        assert f["gate"] == {"ok": True} and f["meta"]["track_id"] == f"TRK-{RUNS[0]}-0001"

    def test_vehicle_theater_poi_entities(self, service):
        svc, _ = service
        v = svc.entity("veh:Drone1")["fields"]
        assert v["fuel"]["fuel_pct"] == 72.0 and v["fuel"]["margin_pct"] == 47.0
        assert v["agl"]["alt_agl_is_real"] is False
        t = svc.entity("thr:default")
        assert t["fields"]["active"] is True
        assert t["fields"]["envelope"]["ceiling_m_agl"] == 120.0
        assert len(t["fields"]["pois"]) == 3
        p = svc.entity("poi:default:North Field")
        assert p["fields"]["near_contacts"] == [f"trk:TRK-{RUNS[-1]}-0003"]

    def test_entity_budget_is_a_parameter(self, service):
        svc, _ = service
        e = svc.entity("msn:MSN-1a2b3c4d", max_bytes=2_000)
        assert ig.json_size(e) <= 2_000 and "_truncated" in e

    def test_overview_is_small_and_honest(self, service):
        svc, _ = service
        o = svc.overview()
        assert ig.json_size(o) <= ig.OVERVIEW_MAX_BYTES
        assert o["theater"]["id"] == "default"
        # graph ids the analyst can put in [[type:id]] chips as-is
        assert o["theater"]["graph_id"] == "thr:default"
        assert o["vehicles"][0]["id"] == "veh:Drone1"
        assert "graph_id" not in svc.graph()["theater"]   # the cached graph is not mutated
        assert o["contacts"]["in_theater"] == 3
        assert o["contacts"]["duplicates_collapsed"] == 27
        assert o["contacts"]["out_of_theater"] == 9
        assert "not assessed" not in o["contacts"]["by_threat"]
        assert o["vehicles"][0]["name"] == "Drone1"
        assert o["missions"][0]["id"] == "msn:MSN-1a2b3c4d"
        assert len(o["alarms"]) <= 5 and o["caveats"]
        assert o["caveats"][0].startswith("Real-data layer is off")

    def test_overview_stays_under_budget_with_a_big_picture(self):
        rows = [salute(f"TRK-BIGBIGBIGBIG-{k:04d}", f"thing_{k}", ob,
                       DEFAULT.home_lat + 0.0004 * (k % 50), DEFAULT.home_lon
                       + 0.0006 * (k // 50), first_seen=NOW_S - 100, last_seen=NOW_S - 50)
                for k, ob in enumerate(list(OB_LIBRARY) * 12)]

        class Ctx:
            def snapshot(self):
                return {"vehicles": [vehicle_row(name=f"Drone{i}") for i in range(20)],
                        "missions": [{**MISSION_ROW, "mission_id": f"MSN-{i:08d}",
                                      "vehicle": f"Drone{i}"} for i in range(20)],
                        "contacts": [], "feeds": {}, "sim_state": "up"}

            def track_rows(self):
                return rows

            def theaters(self):
                return {"theaters": theaters.as_payload()["theaters"],
                        "active": active_block()}

            def recent_events(self, n):
                return [{"kind": "detection", "severity": "info", "message": "x" * 300,
                         "atMs": NOW_MS, "seq": i} for i in range(n)]

        svc = ig.IntelService(Ctx())
        o = svc.overview()
        assert ig.json_size(o) <= ig.OVERVIEW_MAX_BYTES
        assert o["vehicles_omitted"] == 12 and o["missions_omitted"] == 12

    def test_search_and_recent_events(self, service):
        svc, app = service
        hits = svc.search("sam", types=["track"])
        assert hits[0]["id"] == f"trk:TRK-{RUNS[-1]}-0001"
        assert any(h.get("out_of_theater") for h in hits)
        ev = svc.recent_events(500)
        # loop C's own poll published alarms before the 120 added here
        published = app.state.hub.published
        assert published >= 120
        assert len(ev) == 100 and ev[-1]["seq"] == published
        assert [e["seq"] for e in ev] == list(range(published - 99, published + 1))

    def test_a_failing_source_is_a_caveat(self):
        class Broken:
            def snapshot(self):
                raise RuntimeError("bridge state torn down")

        svc = ig.IntelService(Broken())
        g = svc.graph()
        assert_well_formed(g)
        assert any("Intel source snapshot could not be read (RuntimeError: bridge state "
                   "torn down)" in c for c in g["meta"]["caveats"])


class TestInProcessTheater:
    """Before loop C's first read of uav://safety/geofence the bridge's active
    theater is honestly unknown; with the in-process server present the
    service asks the server (the authority that resource reports) directly."""

    class Ctx:
        def snapshot(self):
            return {"vehicles": [], "missions": [], "contacts": [], "feeds": {}}

        def theaters(self):
            return {"theaters": theaters.as_payload()["theaters"],
                    "active": theaters.active_unknown(
                        "uav://safety/geofence has not been read yet")}

    def test_server_theater_fills_an_unknown_bridge_block(self):
        from types import SimpleNamespace

        srv = SimpleNamespace(theater=DEFAULT, theater_mismatch=None)
        g = ig.IntelService(self.Ctx(), srv).graph()
        assert g["theater"] == {"id": "default", "label": DEFAULT.label,
                                "place": DEFAULT.place, "known": True}
        assert g["meta"]["scoped_to_theater"] is True
        assert not any("Active theater unknown" in c for c in g["meta"]["caveats"])
        o = ig.IntelService(self.Ctx(), srv).overview()
        assert o["theater"]["id"] == "default" and o["theater"]["known"] is True

    def test_a_server_without_a_theater_is_not_guessed(self):
        from types import SimpleNamespace

        g = ig.IntelService(self.Ctx(), SimpleNamespace()).graph()
        assert g["theater"]["known"] is False and g["theater"]["id"] is None
        assert any("Active theater unknown" in c for c in g["meta"]["caveats"])

    def test_a_known_bridge_block_wins(self):
        from types import SimpleNamespace

        other = theaters.get("iran-isfahan")
        srv = SimpleNamespace(theater=other, theater_mismatch=None)

        class Known(self.Ctx):
            def theaters(self):
                return {"theaters": theaters.as_payload()["theaters"],
                        "active": active_block()}

        g = ig.IntelService(Known(), srv).graph()
        assert g["theater"]["id"] == "default"

    def test_block_matches_the_servers_geofence_resource(self):
        block = ig._server_active_theater(
            __import__("types").SimpleNamespace(theater=DEFAULT, theater_mismatch=None),
            NOW_MS)
        assert block["known"] is True and block["id"] == "default"
        assert block["source"] == ig.SERVER_THEATER_SOURCE and block["at_ms"] == NOW_MS
        assert block["ao"] == [[a, b] for a, b in DEFAULT.ao_list()]
        assert block["reason"] == ""


def _auth(token):
    bearer = HTTPBearer(auto_error=False)

    def check(cred: HTTPAuthorizationCredentials = Depends(bearer)):  # noqa: B008
        if cred is None or cred.credentials != token:
            raise HTTPException(status_code=401, detail="unauthorized")
        return True
    return check


class TestRouter:
    @pytest.fixture()
    def http(self, service):
        svc, bridge = service
        app = FastAPI()
        app.include_router(ig.intel_router(svc, _auth("rt")))
        with TestClient(app) as tc:
            tc.published = bridge.state.hub.published
            yield tc

    H = {"Authorization": "Bearer rt"}  # noqa: RUF012 - a constant header

    def test_auth_required(self, http):
        for path in ("/intel/graph", "/intel/entity/veh:Drone1", "/intel/events/recent"):
            assert http.get(path).status_code == 401
            assert http.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401

    def test_graph_and_scope(self, http):
        r = http.get("/intel/graph", headers=self.H)
        assert r.status_code == 200 and r.json()["scope"] == "theater"
        assert http.get("/intel/graph?scope=all", headers=self.H).json()["scope"] == "all"
        r = http.get("/intel/graph?scope=galaxy", headers=self.H)
        assert r.status_code == 422 and r.json()["error"] == "invalid_scope"

    def test_entity(self, http):
        r = http.get("/intel/entity/poi:default:North%20Field", headers=self.H)
        assert r.status_code == 200 and r.json()["type"] == "poi"
        r = http.get(f"/intel/entity/trk:TRK-{RUNS[0]}-0002", headers=self.H)
        assert r.status_code == 200 and r.json()["id"] == f"trk:TRK-{RUNS[-1]}-0002"
        assert len(r.content) <= ig.ENTITY_MAX_BYTES
        r = http.get("/intel/entity/trk:nope", headers=self.H)
        assert r.status_code == 404
        assert r.json() == {"error": "unknown_entity", "id": "trk:nope"}  # CONTRACT §3 shape

    def test_events_recent_is_clamped(self, http):
        r = http.get("/intel/events/recent?limit=5000", headers=self.H)
        assert r.status_code == 200 and len(r.json()["events"]) == 100
        r = http.get("/intel/events/recent?limit=3", headers=self.H)
        n = http.published
        assert [e["seq"] for e in r.json()["events"]] == [n - 2, n - 1, n]


# ===========================================================================
# end to end against a REAL in-process server
# ===========================================================================

class InProcessTransport(McpClient):
    """Loop C's transport, answered by a real GodseyeUavServer in-process."""

    def __init__(self, srv):
        super().__init__("inproc://mcp", "t")
        self.srv = srv
        self.threat_payloads = []

    def call_tool(self, name, arguments):
        fn = self.srv.mcp._tool_manager._tools[name].fn
        out = asyncio.run(fn(**arguments))
        out = json.loads(json.dumps(out, default=str))
        if isinstance(out, dict) and isinstance(out.get("error"), dict):
            return None, f"{name}: {out['error'].get('code')}"
        if name == "uav_assess_threat":
            self.threat_payloads.append(out)
        return out, None

    def read_resource(self, uri):
        async def read():
            items = list(await self.srv.mcp.read_resource(uri))
            return json.loads(items[0].content)
        try:
            return asyncio.run(read()), None
        except Exception as exc:  # noqa: BLE001 - mirrors McpClient: never raises
            return None, f"{uri}: {exc}"


@pytest.fixture()
def real_server(tmp_path):
    import airsim
    from godseye_uav.fake_airsim import FakeAirSim
    from godseye_uav.geo import GeoPoint
    from godseye_uav.host import honour_msgpack_bind_host
    from godseye_uav.safety import SafetyEnvelope
    from godseye_uav.server import GodseyeUavServer, UavBackend
    from godseye_uav.store import Store

    home = GeoPoint(DEFAULT.home_lat, DEFAULT.home_lon, 93.0)
    sim, last = None, None
    honour_msgpack_bind_host()   # the sim binds 127.0.0.1, so a held port fails
    for port in E2E_SIM_PORTS:
        if not _nothing_listens(port):
            continue
        cand = FakeAirSim(home=home, port=port)
        try:
            cand.start()
            sim = cand
            break
        except OSError as exc:
            last = exc
    if sim is None:
        raise RuntimeError(f"no free sim port in {E2E_SIM_PORTS}: {last}")
    store = Store(tmp_path)
    srv = None
    try:
        client = airsim.MultirotorClient(port=sim.port)
        client.confirmConnection()
        srv = GodseyeUavServer(UavBackend(client, home, sim=sim), store,
                               envelope=SafetyEnvelope(geofence=list(DEFAULT.ao),
                                                       home=(home.latitude, home.longitude,
                                                             home.altitude)))
        yield srv, home
    finally:
        if srv is not None:
            srv.stop_monitor()
            srv.tasking.shutdown()
        store.close()
        sim.stop()


def test_real_server_shapes_flow_through_to_the_graph(real_server):
    srv, home = real_server
    tools = srv.mcp._tool_manager._tools

    async def seed():
        for i in range(12):    # a 4 x 3 grid ~90 x 90 m apart: 12 separate tracks
            out = await tools["sim_spawn_target"].fn(
                lat=home.latitude + 0.0006 + 0.0008 * (i % 4),
                lon=home.longitude + 0.0012 * (i // 4 - 1),
                ob_class="supply_truck" if i % 2 else "mbt")
            assert out.get("error") is None, out
        for _ in range(3):
            await tools["uav_get_detections"].fn(vehicle="Drone1")
            await asyncio.sleep(0.05)
        await srv.tick_once("Drone1")
    asyncio.run(seed())
    assert len(srv.tracks.tracks()) == 12

    transport = InProcessTransport(srv)
    app = bridge_app(transport)
    intel = app.state.bridge.feed.poll_once()
    # the REAL summary THREATREP expanded 10 and listed the rest in `omitted`...
    rep = transport.threat_payloads[-1]
    assert rep["detail"] == "summary" and rep["omitted_count"] == 2
    # ...and the bridge now folds both, so every contact carries a level
    assert len(intel.contacts) == 12
    assert all(c["threat_level"] for c in intel.contacts)

    svc = ig.IntelService(app.state.godseye, srv)
    g = svc.graph()
    assert_well_formed(g)
    assert g["theater"]["id"] == "default" and g["theater"]["known"] is True
    tracks = [n for n in g["nodes"] if n["type"] == "track"]
    assert len(tracks) == 12
    assert all(n["attrs"]["threat"] != "not assessed" for n in tracks)
    assert {n["attrs"]["ob_class"] for n in tracks} == {"mbt", "supply_truck"}
    assert g["meta"]["duplicates_collapsed"] == 0
    assert g["meta"]["caveats"][0].startswith("Real-data layer is off")
    veh = next(n for n in g["nodes"] if n["type"] == "vehicle")
    assert veh["attrs"]["link"] and veh["attrs"]["bingo_latched"] is False
    assert veh["attrs"]["lost_link"]["behaviour"] == "rtb"
    assert veh["attrs"]["lost_link"]["source"] == "vehicle"

    e = svc.entity(tracks[0]["id"])
    assert ig.json_size(e) <= ig.ENTITY_MAX_BYTES
    td = e["fields"]["threat_detail"]
    assert td["isr_only"] is True and "sensor_posture" in td
    assert e["provenance"]["source"].startswith("uav_list_tracks SALUTE row")
    assert svc.entity("veh:Drone1")["fields"]["link"]["state"]
    assert ig.json_size(svc.overview()) <= ig.OVERVIEW_MAX_BYTES
    assert svc.search("truck", types=["track"])
    # the whole thing never reached for the live stack's ports
    assert all(p not in (8790, 8791, 41451) for p in (UNUSED_SIM_PORT, *E2E_SIM_PORTS))
    started = time.monotonic()
    svc.cache_ttl_s = 0.0
    svc.graph()
    assert time.monotonic() - started < 1.0


def test_a_simulated_link_loss_is_the_vehicle_not_the_sim_feed(real_server):
    """sim_set_link_state(lost) is ONE vehicle's datalink. The sim host still
    answers, so feed:sim stays up; the vehicle node carries the link machine's
    state and when the link went down (attrs.link_lost_since_ms)."""
    srv, home = real_server
    sim = srv.backend.sim
    tools = srv.mcp._tool_manager._tools
    app = create_app(adapter=AirSimAdapter(ip="127.0.0.1", port=sim.port, home=home),
                     token="intel-test-token", start_loops=False)
    feed = MissionFeed(InProcessTransport(srv), app.state.hub, vehicles=lambda: ["Drone1"],
                       flown=app.state.bridge.flown)
    feed._geofence_at = -1e9
    app.state.bridge.feed = app.state.feed = feed
    state = app.state.bridge
    svc = ig.IntelService(app.state.godseye, srv)
    svc.cache_ttl_s = 0.0

    def nodes():
        feed.poll_once()
        return {n["id"]: n for n in svc.graph()["nodes"]}

    asyncio.run(srv.tick_once("Drone1"))
    assert state.tick_vehicle("Drone1") is not None
    before = nodes()
    assert before["feed:sim"]["status"] == "ok"
    assert before["veh:Drone1"]["attrs"]["link"] == "up"
    assert "link_lost_since_ms" not in before["veh:Drone1"]["attrs"]

    lost_at_ms = int(time.time() * 1000)
    out = asyncio.run(tools["sim_set_link_state"].fn(vehicle="Drone1", state="lost"))
    assert out["ok"] is True, out
    try:
        asyncio.run(srv.tick_once("Drone1"))          # the link machine sees it: pending
        assert state.tick_vehicle("Drone1") is None   # telemetry refused for THIS vehicle
        after = nodes()
        assert after["feed:sim"]["status"] == "ok", after["feed:sim"]
        assert after["feed:sim"]["attrs"]["ok"] is True
        veh = after["veh:Drone1"]
        assert veh["attrs"]["link"] == "pending"
        since = veh["attrs"]["link_lost_since_ms"]
        assert lost_at_ms - 50 <= since <= int(time.time() * 1000) + 50
        assert veh["status"] == "critical"            # stale telemetry with the reason
        entity = svc.entity("veh:Drone1")
        assert entity["fields"]["link_lost_since_ms"] == since
        assert "datalink lost" in entity["provenance"]["telemetry_error"]
    finally:
        asyncio.run(tools["sim_set_link_state"].fn(vehicle="Drone1", state="nominal"))
    assert state.tick_vehicle("Drone1") is not None
    assert nodes()["feed:sim"]["status"] == "ok"


def test_bidi_controls_are_stripped_from_node_text():
    """An OSM name or `detected_as` carrying U+202E/U+2066 must not reach the
    orb, search or inspector, where it could reverse or spoof a label."""
    n = ig._node("x:1", "poi", "Evil\u202eleiF htroN", subtitle="\u2066SAM\u2069 site",
                 attrs={"detected_as": "T-72\u202d", "count": 3})
    assert n["label"] == "EvilleiF htroN"
    assert n["subtitle"] == "SAM site"
    assert n["attrs"] == {"detected_as": "T-72", "count": 3}
    assert ig.strip_bidi("a\u202ab\u202cc\u2067d\u2068e") == "abcde"
