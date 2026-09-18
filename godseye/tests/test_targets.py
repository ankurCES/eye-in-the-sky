"""Target identification + SALUTE/INTREP reporting tests (PLAN Phase 4).

Covers the order-of-battle library (M13a), confidence with cited evidence
(M13c), persistent non-reusable track ids (M11), pattern-of-life (M12) and the
§4.7 SALUTE/INTREP artifacts (M8).
"""
import asyncio
import itertools
import json

import airsim
import pytest

from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint
from godseye_uav.safety import SafetyEnvelope
from godseye_uav.server import GodseyeUavServer, UavBackend
from godseye_uav.store import Store
from godseye_uav.targets import (
    CONFIDENCE_LEVELS,
    OB_LIBRARY,
    UNCLASSIFIED,
    PatternOfLife,
    Track,
    TrackManager,
    assess_confidence,
    classify,
    confidence_at_least,
    intrep_report,
    intrep_summary,
    match_ob,
    mint_origin,
    ob_for_category,
    salute_report,
)
from godseye_uav.threat import indicator_posture

HOME = GeoPoint(47.641468, -122.140165, 93.0)
AO = [(47.63, -122.16), (47.63, -122.12), (47.66, -122.12), (47.66, -122.16)]
_PORT = itertools.count(44000)

#: An 08:00 UTC epoch and a 02:00 UTC epoch, for pattern-of-life hour bins.
H08 = 8 * 3600.0
H02 = 2 * 3600.0


def run(coro):
    return asyncio.run(coro)


def _det(name, lat, lon, alt=93.0, **extra):
    return {"name": name,
            "geo_point": {"latitude": lat, "longitude": lon, "altitude": alt},
            **extra}


def _good_sensor():
    return {"sensor": "scene", "fov_deg": 20.0, "image_px": 640,
            "light": "day", "weather": "clear"}


def _identified_track(tm, name="SA-6_site_1", lat=47.6400, lon=-122.1400,
                      looks=4, t0=1000.0, observer=None):
    """Build a well-observed track: several close-range looks with metadata."""
    observer = observer or {"lat": 47.6405, "lon": -122.1405, "alt_m": 300.0,
                            "vehicle": "Drone1"}
    track = None
    for i in range(looks):
        track = tm.ingest([_det(name, lat + i * 0.00002, lon)],
                          now=t0 + 15.0 * i, sensor=_good_sensor(),
                          observer=observer, frame_id=f"FRAME-{i:03d}")[0]
    return track


# ---- §4.6(a) order-of-battle library (M13a) ----

def test_classify_order_of_battle():
    assert classify("T72_Tank_01") == "armor"
    assert classify("SA-6_SAM_site") == "sam"
    assert classify("Ural_Truck") == "logistics"
    assert classify("radar_array") == "radar"
    assert classify("civilian_sedan") == "civilian"
    # nothing recognisable -> explicitly unknown, never a borrowed envelope
    assert classify("zzz_widget_42") == "unknown"


def test_classification_matches_whole_tokens_not_substrings():
    """M13a: 'tor'/'tel' substrings used to make motorcycles into SAM sites."""
    assert classify("Motorcycle") != "sam"
    assert classify("Tractor_01") != "sam"
    assert classify("Hotel_Building") == "structure"
    assert classify("Satellite_dish") != "sam"
    # ...while real designators still resolve
    assert classify("Tor_M1") == "sam"
    assert classify("TEL_3") == "sam"
    # and the previously-unmatched real-world names now classify
    assert classify("BM-21_Grad") == "artillery"
    assert classify("School_Bus") == "civilian"
    assert classify("ZSU-23-4") == "aaa"


def test_ob_library_rows_carry_capabilities_ranges_and_mobility():
    """PLAN §4.6(a): type -> capabilities, WEAPON RANGES, MOBILITY."""
    assert len(OB_LIBRARY) >= 25, "OB library must cover a real ISR target set"
    categories = {e.category for e in OB_LIBRARY.values()}
    for needed in ("sam", "aaa", "radar", "armor", "artillery", "logistics",
                   "c2", "personnel", "structure"):
        assert needed in categories, needed
    for key, e in OB_LIBRARY.items():
        assert e.capabilities, key
        assert e.signature_cues, key           # why a classification is justified
        assert e.mobility, key
        assert e.typical_unit_size, key
        assert e.typical_unit_count >= 1, key
        assert e.acquisition_range_m >= 0.0, key
        assert e.weapon_range_m >= 0.0, key
        assert 0.0 <= e.threat_weight <= 1.0, key
        assert e.size_m > 0.0, key
    # acquisition range is a DIFFERENT number from weapon range
    sam = OB_LIBRARY["sam_medium_range"]
    assert sam.acquisition_range_m > sam.weapon_range_m > 0.0
    assert sam.weapon_ceiling_m > 0.0
    # artillery reaches a long way on the ground but cannot touch an aircraft
    assert OB_LIBRARY["mlrs"].weapon_range_m == 0.0
    assert OB_LIBRARY["mlrs"].engages_air is False


def test_unclassified_asserts_no_weapons_envelope():
    entry, evidence = match_ob("nondescript_object_7")
    assert entry is UNCLASSIFIED
    assert entry.weapon_range_m == 0.0
    assert evidence["matched"] is None
    assert evidence["specificity"] == 0.0


def test_match_evidence_cites_the_token_that_justified_the_class():
    entry, evidence = match_ob("SA-6_site_1")
    assert entry.key == "sam_medium_range"
    assert evidence["matched"] == "sa-6"
    assert evidence["specificity"] == 1.0          # platform-level designator
    _, generic = match_ob("a_truck")
    assert generic["specificity"] == 0.6           # class-level only


def test_legacy_category_resolves_to_a_representative_row():
    assert ob_for_category("sam").key == "sam_medium_range"
    assert ob_for_category("vehicle").key == "utility_vehicle"
    assert ob_for_category("unknown") is UNCLASSIFIED


# ---- M11 persistent tracks ----

def test_track_manager_persistent_ids_and_correlation():
    tm = TrackManager()
    t1 = tm.ingest([_det("T72_Tank", 47.64, -122.14)], now=1000.0)[0]
    # same object re-detected nearby -> same track id, sightings increment
    t2 = tm.ingest([_det("T72_Tank", 47.64002, -122.14002)], now=1005.0)[0]
    assert t1.track_id == t2.track_id
    assert t2.sightings == 2
    assert t2.speed_mps is not None  # pattern-of-life velocity (M12)


def test_track_manager_separates_distant_contacts():
    tm = TrackManager()
    a = tm.ingest([_det("tank", 47.64, -122.14, 0)])[0]
    b = tm.ingest([_det("tank", 47.65, -122.15, 0)])[0]
    assert a.track_id != b.track_id
    assert len(tm.tracks()) == 2


def test_track_ids_are_not_reused_across_runs():
    """M11: a persistent track id must never silently denote a new object."""
    a = TrackManager()
    b = TrackManager()
    assert a.origin != b.origin, "each run mints its own id prefix"
    ta = a.ingest([_det("tank", 47.64, -122.14)])[0]
    tb = b.ingest([_det("truck", 47.64, -122.14)])[0]
    assert ta.track_id != tb.track_id
    assert ta.uid != tb.uid
    assert ta.track_id.startswith("TRK-")


def test_run_prefixes_do_not_collide_within_one_second():
    """M11: two runs are separated ONLY by the entropy field when they start in
    the same second. At 10 bits that field collided 1 time in 1024, and a
    collided prefix re-issues TRK-<prefix>-0001 for a different object — the
    exact failure M11 exists to prevent (it also made the test above flaky)."""
    same_second = 1_700_000_000.0
    minted = [mint_origin(same_second) for _ in range(2000)]
    assert len(set(minted)) == len(minted), "same-second run prefixes collided"
    assert len({len(m) for m in minted}) == 1, "prefix width must be stable"


def test_colliding_prefixes_would_reissue_a_track_id():
    """Why the entropy width matters, stated as behaviour rather than trust."""
    a = TrackManager(origin="FIXEDPREFIX")
    b = TrackManager(origin="FIXEDPREFIX")
    ta = a.ingest([_det("T72_Tank", 47.64, -122.14)], now=1000.0)[0]
    tb = b.ingest([_det("School_Bus", 33.0, 51.0)], now=1000.0)[0]
    assert ta.track_id == tb.track_id      # same id...
    assert ta.uid != tb.uid                # ...different objects
    assert mint_origin(1.0) != mint_origin(1.0)


def test_track_state_round_trips_for_persistence():
    """M11/§4.8: (de)serialization keeps ids, evidence and history intact."""
    tm = TrackManager()
    t = _identified_track(tm)
    state = tm.to_dict()

    restored = TrackManager.from_dict(state)
    rt = restored.get(t.track_id)
    assert rt is not None
    assert rt.uid == t.uid
    assert rt.sightings == t.sightings
    assert rt.ob_class == t.ob_class
    assert len(rt.observations) == len(t.observations)
    assert rt.history == t.history
    # a restored run keeps its own mint prefix, so nothing is ever re-issued
    fresh = restored.ingest([_det("BTR-80", 47.7, -122.2)])[0]
    assert fresh.track_id != t.track_id
    assert fresh.track_id not in {r["track_id"] for r in state["tracks"]}


def test_sim_reset_preserves_the_track_store():
    """PLAN §4.4 / M12: sim_reset does NOT wipe the track store."""
    tm = TrackManager()
    t = _identified_track(tm)
    out = tm.mark_sim_reset(now=2000.0)
    assert out["tracks_retained"] == 1
    assert tm.get(t.track_id) is not None
    assert tm.sim_epoch == 1


# ---- §4.6(c) confidence with cited evidence (M13c) ----

def test_single_sighting_is_only_possible():
    tm = TrackManager()
    t = tm.ingest([_det("SA-6_site_1", 47.64, -122.14)], now=1000.0,
                  sensor=_good_sensor(),
                  observer={"lat": 47.6405, "lon": -122.1405, "alt_m": 300.0})[0]
    conf = assess_confidence(t, now=1000.0)
    assert conf["level"] == "possible"
    assert conf["sighting_cap"] == "possible"
    assert conf["level"] in CONFIDENCE_LEVELS


def test_confidence_rises_with_real_evidence():
    tm = TrackManager()
    t = _identified_track(tm, looks=6)
    conf = assess_confidence(t, now=1000.0 + 15.0 * 5)
    assert conf["level"] == "confirmed"
    assert conf["score"] > 0.7
    elements = {e["element"] for e in conf["evidence"]}
    # every element of the assessment cites its own evidence (PLAN §4.6(c))
    assert {"independent_sightings", "observation_span_s", "sensor_conditions",
            "pixels_on_target", "time_since_last_fix_s",
            "classification_specificity"} <= elements
    for item in conf["evidence"]:
        assert item["source"], item
    sight = next(e for e in conf["evidence"] if e["element"] == "independent_sightings")
    assert sight["value"] == t.sightings
    assert "fix(es)" in sight["source"]


def test_confidence_degrades_with_conditions_and_staleness():
    good = TrackManager()
    t_good = _identified_track(good, looks=5)
    night_fog = {"sensor": "scene", "fov_deg": 60.0, "image_px": 320,
                 "light": "night", "weather": "fog"}
    bad = TrackManager()
    t_bad = None
    for i in range(5):
        t_bad = bad.ingest([_det("SA-6_site_1", 47.64 + i * 0.00002, -122.14)],
                           now=1000.0 + 15.0 * i, sensor=night_fog,
                           observer={"lat": 47.70, "lon": -122.20, "alt_m": 3000.0})[0]
    at = 1000.0 + 15.0 * 4
    assert assess_confidence(t_bad, at)["score"] < assess_confidence(t_good, at)["score"]
    # and the same track goes stale as time passes without a re-look
    fresh = assess_confidence(t_good, at)["score"]
    stale = assess_confidence(t_good, at + 1200.0)["score"]
    assert stale < fresh


def test_unclassified_contact_cannot_reach_confirmed_on_class_alone():
    tm = TrackManager()
    t = None
    for i in range(6):
        t = tm.ingest([_det("object_9", 47.64, -122.14)], now=1000.0 + i,
                      sensor=_good_sensor(),
                      observer={"lat": 47.6401, "lon": -122.1401, "alt_m": 120.0})[0]
    conf = assess_confidence(t, now=1006.0)
    spec = next(e for e in conf["evidence"]
                if e["element"] == "classification_specificity")
    assert spec["score"] == 0.0
    assert conf["level"] != "confirmed"


def test_confidence_ordering_helper():
    assert confidence_at_least("confirmed", "probable") is True
    assert confidence_at_least("possible", "probable") is False


# ---- location integrity (live-probe regression) ----

def test_location_is_the_contacts_own_geo_point_never_the_observers():
    """A SALUTE Location must be the CONTACT's fix, not the observer's."""
    tm = TrackManager()
    observer = {"lat": 47.0, "lon": -122.0, "alt_m": 900.0, "vehicle": "Drone1"}
    t = tm.ingest([_det("T72_Tank", 47.64, -122.14, 93.0)], now=1000.0,
                  sensor=_good_sensor(), observer=observer)[0]
    assert (t.lat, t.lon) == (47.64, -122.14)
    rep = salute_report(t, observer="Drone1")
    assert rep["location"]["lat"] == 47.64
    assert rep["location"]["lon"] == -122.14
    assert rep["location"]["lat"] != observer["lat"]
    assert rep["location"]["source"] == "contact detection geo_point"
    assert rep["location"]["observer_position_used"] is False
    # the observer position is evidence geometry only
    assert rep["location"]["slant_range_m"] > 1000.0


def test_detection_without_geo_point_never_falls_back_to_the_observer():
    tm = TrackManager()
    observer = {"lat": 47.0, "lon": -122.0, "alt_m": 900.0, "vehicle": "Drone1"}
    updated = tm.ingest([{"name": "ghost"},
                         {"name": "ghost2", "geo_point": {}}],
                        now=1000.0, observer=observer)
    assert updated == []
    assert tm.tracks() == []
    assert len(tm.rejected) == 2
    assert "observer position is never substituted" in tm.rejected[0]["reason"]


# ---- §4.7 SALUTE (M8) ----

def test_salute_report_fields():
    tm = TrackManager()
    t = _identified_track(tm, name="SA-6_SAM", looks=3)
    rep = salute_report(t, observer="Drone1", peers=tm.tracks())
    for key in ("size", "activity", "location", "unit", "time", "equipment"):
        assert key in rep
        assert isinstance(rep[key], dict), key   # structured, never free text
    assert rep["format"] == "SALUTE"
    assert rep["unit"]["category"] == "sam"
    assert rep["unit"]["ob_class"] == "sam_medium_range"
    assert rep["category"] == "sam"              # flat mirror for the roster
    assert rep["observer"] == "Drone1"


def test_salute_every_field_is_complete_and_evidenced():
    tm = TrackManager()
    t = _identified_track(tm, name="T72_Tank_1", looks=4)
    rep = salute_report(t, observer="Drone1", peers=tm.tracks())
    assert rep["size"]["count"] >= 1 and rep["size"]["basis"]
    assert rep["activity"]["code"] and rep["activity"]["basis"]
    assert rep["location"]["fix_time"] == int(t.last_seen)
    assert rep["unit"]["assessment"]                 # unit, not equipment
    assert rep["time"]["iso"].endswith("Z")
    assert rep["time"]["first_seen"] <= rep["time"]["last_seen"]
    eq = rep["equipment"]
    assert eq["detected_as"] == "T72_Tank_1"         # what the sensor saw
    assert eq["platform"] and eq["capabilities"] and eq["signature_cues"]
    assert eq["weapon_range_m"] == OB_LIBRARY["mbt"].weapon_range_m
    assert rep["confidence"]["level"] in CONFIDENCE_LEVELS
    assert rep["confidence"]["evidence"]


def test_salute_size_aggregates_a_co_located_element():
    """M8: a convoy is one element of N, not N 'size 1' contacts."""
    tm = TrackManager()
    for i in range(3):
        tm.ingest([_det("T72_Tank", 47.6400 + i * 0.001, -122.1400)], now=1000.0)
    tracks = tm.tracks()
    rep = salute_report(tracks[0], peers=tracks)
    assert rep["size"]["count"] == 3
    assert len(rep["size"]["members"]) == 3
    # a lone contact elsewhere is not swept into the element
    tm.ingest([_det("T72_Tank", 47.70, -122.20)], now=1000.0)
    lone = tm.tracks()[-1]
    assert salute_report(lone, peers=tm.tracks())["size"]["count"] == 1


def test_fixed_installations_are_never_reported_as_manoeuvring():
    """The 'is this platform even mobile?' test was added to the intent posture
    indicator but not to SALUTE Activity, so the same bunker came back as
    posture=static_installation and activity=manoeuvring in one report."""
    for key in ("bridge", "bunker", "depot_ammo", "depot_fuel", "structure"):
        assert OB_LIBRARY[key].mobility == "fixed", key
        t = Track(track_id="T", name=f"{key}_1", category=OB_LIBRARY[key].category,
                  lat=33.72, lon=51.72, alt_m=0.0, first_seen=0.0, last_seen=10.0,
                  speed_mps=3.0, heading_deg=90.0, ob_class=key)
        act = salute_report(t, now=10.0)["activity"]
        assert act["code"] == "stationary", (key, act)
        assert indicator_posture(t, 10.0)["state"] == "static_installation", key
        assert act["basis"]


def test_civilian_infrastructure_does_not_inherit_a_weapons_envelope():
    """M13a: the token rewrite stopped 'Motorcycle' matching 'tor', but a
    generic single token could still hand civil infrastructure a weapons
    envelope one level up ('Water_Tank' -> main battle tank, 1500 m)."""
    for name in ("Water_Tank", "Septic_Tank", "Fish_Tank", "Ship_Container"):
        entry, ev = match_ob(name)
        assert entry.category == "structure", (name, entry.key)
        assert entry.weapon_range_m == 0.0, (name, entry.weapon_range_m)
        assert ev["matched"] and " " in ev["matched"], (name, ev)
    # the real platforms those keywords exist for still resolve
    assert classify("T72_Tank_01") == "armor"
    assert classify("Patrol_Boat_2") == "naval"


def test_salute_activity_reflects_measured_motion():
    tm = TrackManager()
    tm.ingest([_det("Ural_Truck", 47.6400, -122.1400)], now=1000.0)
    moving = tm.ingest([_det("Ural_Truck", 47.6403, -122.1400)], now=1002.0)[0]
    act = salute_report(moving)["activity"]
    assert act["code"] in ("on_march", "manoeuvring")
    assert act["speed_mps"] and act["speed_mps"] > 1.0


# ---- §4.7 INTREP (M8) ----

def test_intrep_rollup():
    tm = TrackManager()
    tm.ingest([_det("T72", 47.64, -122.14, 0)])
    tm.ingest([_det("SA-6", 47.65, -122.15, 0)])
    rep = intrep_summary(tm.tracks())
    assert rep["format"] == "INTREP"
    assert rep["total_tracks"] == 2
    assert rep["by_category"]["armor"] == 1
    assert rep["by_category"]["sam"] == 1


def test_intrep_has_every_section_the_plan_names():
    """PLAN §4.7: mission summary, coverage %, tracks w/ IDs, sensor
    conditions, LOAL events, gaps."""
    tm = TrackManager()
    t = _identified_track(tm, looks=3)
    rep = intrep_report(
        tm.tracks(), mission_id="MSN-1", now=1000.0 + 30.0,
        mission_summary={"kind": "grid_search", "vehicle": "Drone1",
                         "started": 900, "ended": 1030, "duration_s": 130,
                         "area_name": "AO BRAVO", "status": "complete"},
        coverage={"planned_area_km2": 4.0, "covered_area_km2": 3.4,
                  "coverage_pct": 85.0, "method": "footprint integration"},
        sensor_conditions={"light": "day", "weather": "clear",
                           "visibility_km": 12.0, "wind_mps": 4.0,
                           "gps_quality": "nominal"},
        loal_events=[{"vehicle": "Drone1", "start": 950, "end": 962,
                      "duration_s": 12, "plan": "hold-orbit", "recovered": True}],
    )
    for section in ("mission_summary", "coverage", "contacts",
                    "sensor_conditions", "loal_events", "gaps"):
        assert section in rep, section
    assert rep["mission_id"] == "MSN-1"
    assert rep["mission_summary"]["kind"] == "grid_search"
    assert rep["coverage"]["coverage_pct"] == 85.0
    assert rep["contacts"][0]["track_id"] == t.track_id
    assert rep["contacts"][0]["format"] == "SALUTE"
    assert rep["sensor_conditions"]["sensors_used"] == ["scene"]
    assert rep["loal_events"][0]["duration_s"] == 12
    assert rep["confidence_summary"]["probable"] + \
           rep["confidence_summary"]["confirmed"] >= 1
    kinds = {g["type"] for g in rep["gaps"]}
    assert "area_not_covered" in kinds          # derived from coverage 85%
    assert "link_outage" in kinds               # derived from the LOAL event


def test_intrep_gaps_flag_unidentified_and_low_confidence_contacts():
    tm = TrackManager()
    tm.ingest([_det("object_9", 47.64, -122.14)], now=1000.0)
    rep = intrep_report(tm.tracks(), now=1000.0,
                        coverage={"coverage_pct": 100.0})
    kinds = {g["type"] for g in rep["gaps"]}
    assert "unidentified_contacts" in kinds
    assert "low_confidence_contacts" in kinds
    assert "area_not_covered" not in kinds


def test_intrep_can_carry_the_pattern_of_life_picture():
    tm = TrackManager()
    t = _identified_track(tm, lat=47.6400, lon=-122.1400, looks=3)
    pol = PatternOfLife(min_samples=4)
    pol.define_poi("CROSSROADS", 47.6400, -122.1400, radius_m=300.0)
    for i in range(6):
        pol.observe("TRK-X", "logistics", 47.6400, -122.1400, ts=H08 + i)
    rep = intrep_report(tm.tracks(), now=1045.0, pattern_of_life=pol)
    assert rep["pattern_of_life"]["pois"] == ["CROSSROADS"]
    dev = rep["pattern_of_life"]["deviations"][0]
    assert dev["track_id"] == t.track_id
    assert dev["poi"] == "CROSSROADS"


# ---- M12 pattern-of-life ----

def _seed(pol, poi="DEPOT", n=30, category="logistics", lat=47.64, lon=-122.14):
    pol.define_poi(poi, lat, lon, radius_m=250.0)
    for i in range(n):
        pol.observe(f"TRK-{i:03d}", category, lat, lon, ts=H08 + i)
    return pol


def test_pattern_of_life_builds_a_baseline():
    pol = _seed(PatternOfLife(min_samples=24))
    b = pol.get("DEPOT")
    assert b.total_obs == 30
    assert b.hourly[8] == 30
    assert b.categories["logistics"] == 30
    assert pol.pois_containing(47.64, -122.14) == ["DEPOT"]
    assert pol.pois_containing(47.70, -122.20) == []


def test_pattern_of_life_deviation_is_low_for_normal_activity():
    pol = _seed(PatternOfLife(min_samples=24))
    dev = pol.deviation("DEPOT", category="logistics", ts=H08 + 100)
    assert dev["deviation"] < 0.1
    assert dev["mature"] is True
    elements = {e["element"] for e in dev["evidence"]}
    assert {"hour_of_day_activity", "category_novelty", "baseline_maturity"} <= elements


def test_pattern_of_life_deviation_is_high_for_novel_activity():
    """M13(b): pattern-of-life deviation is an intent indicator."""
    pol = _seed(PatternOfLife(min_samples=24))
    dev = pol.deviation("DEPOT", category="armor", ts=H02)
    assert dev["deviation"] > 0.8
    hour = next(e for e in dev["evidence"] if e["element"] == "hour_of_day_activity")
    assert "UTC hour 02" in hour["source"]
    cat = next(e for e in dev["evidence"] if e["element"] == "category_novelty")
    assert cat["value"] == 0


def test_pattern_of_life_thin_baseline_cannot_manufacture_a_deviation():
    pol = _seed(PatternOfLife(min_samples=24), n=3)
    dev = pol.deviation("DEPOT", category="armor", ts=H02)
    assert dev["mature"] is False
    assert dev["raw_deviation"] > 0.8
    assert dev["deviation"] < 0.2          # scaled down by baseline maturity


def test_pattern_of_life_deviation_for_track_uses_the_containing_poi():
    pol = _seed(PatternOfLife(min_samples=4))
    t = Track(track_id="TRK-1", name="T72_Tank", category="armor",
              lat=47.64, lon=-122.14, alt_m=0.0, first_seen=H02, last_seen=H02,
              ob_class="mbt")
    dev = pol.deviation_for_track(t, now=H02)
    assert dev["poi"] == "DEPOT"
    assert dev["deviation"] > 0.8
    away = Track(track_id="TRK-2", name="T72_Tank", category="armor",
                 lat=47.70, lon=-122.20, alt_m=0.0, first_seen=H02,
                 last_seen=H02, ob_class="mbt")
    assert pol.deviation_for_track(away, now=H02)["deviation"] == 0.0


def test_pattern_of_life_survives_sim_reset_and_round_trips():
    """M12: the pattern-of-life DB survives sim_reset and restart."""
    pol = _seed(PatternOfLife(min_samples=24))
    out = pol.record_sim_reset(ts=H08 + 500)
    assert out["pois_retained"] == 1
    assert pol.get("DEPOT").total_obs == 30       # not wiped

    restored = PatternOfLife.from_dict(pol.to_dict())
    assert restored.get("DEPOT").total_obs == 30
    assert restored.get("DEPOT").hourly[8] == 30
    assert restored.sim_resets == pol.sim_resets
    before = pol.deviation("DEPOT", category="armor", ts=H02)["deviation"]
    assert restored.deviation("DEPOT", category="armor", ts=H02)["deviation"] == before


def test_pattern_of_life_records_dwell_when_a_contact_leaves():
    pol = PatternOfLife(min_samples=4)
    pol.define_poi("GATE", 47.64, -122.14, radius_m=100.0)
    pol.observe("TRK-1", "logistics", 47.64, -122.14, ts=H08)
    pol.observe("TRK-1", "logistics", 47.64, -122.14, ts=H08 + 120)
    pol.observe("TRK-1", "logistics", 47.70, -122.20, ts=H08 + 300)   # departs
    b = pol.get("GATE")
    assert b.visits == 1
    assert b.dwell_samples and b.dwell_samples[0] == pytest.approx(300.0)


# ---- MCP wiring against the fake sim ----

@pytest.fixture
def server(tmp_path):
    port = next(_PORT)
    sim = FakeAirSim(port=port)
    sim.start()
    try:
        client = airsim.MultirotorClient(port=port)
        client.confirmConnection()
        backend = UavBackend(client, HOME)
        envelope = SafetyEnvelope(geofence=AO, home=(HOME.latitude, HOME.longitude, HOME.altitude))
        with Store(tmp_path) as store:
            srv = GodseyeUavServer(backend, store, envelope=envelope, watchdog_s=10.0)
            yield srv
    finally:
        try:
            srv.tasking.shutdown()
        except Exception:
            pass
        sim.stop()


def _fn(srv, name):
    return srv.mcp._tool_manager._tools[name].fn


def test_target_tools_registered(server):
    tools = srv_tools = server.mcp._tool_manager._tools
    for name in ("uav_scan_targets", "uav_identify_target", "uav_target_report",
                 "uav_list_tracks", "sim_spawn_target"):
        assert name in tools, name


def test_spawn_then_scan_creates_track(server):
    async def main():
        # spawn a SAM site near the drone's home, then scan for it
        await _fn(server, "sim_spawn_target")(
            name="SA-6_site_1", mesh="SA-6_SAM", lat=47.6415, lon=-122.1402, alt_m=93.0)
        out = await _fn(server, "uav_scan_targets")(vehicle="Drone1")
        assert out["detections"] >= 1
        assert out["tracks_updated"], out
        rep = out["tracks"][0]
        assert rep["format"] == "SALUTE"
        assert rep["unit"]["category"] == "sam"
        assert rep["confidence"]["level"] in CONFIDENCE_LEVELS
        # identify the same track back
        tid = rep["track_id"]
        ident = await _fn(server, "uav_identify_target")(track_id=tid)
        assert ident["track_id"] == tid
        assert ident["equipment"]["detected_as"] == "SA-6_site_1"
    run(main())


def test_target_report_rollup(server):
    async def main():
        await _fn(server, "sim_spawn_target")(
            name="T72_1", mesh="T72_Tank", lat=47.6415, lon=-122.1402, alt_m=93.0)
        await _fn(server, "uav_scan_targets")(vehicle="Drone1")
        rep = await _fn(server, "uav_target_report")()
        assert rep["format"] == "INTREP"
        assert rep["total_tracks"] >= 1
        assert "gaps" in rep and "sensor_conditions" in rep
        listed = await _fn(server, "uav_list_tracks")()
        assert listed["count"] == rep["total_tracks"]
    run(main())


# ---------------------------------------------------------------------------
# INTREP size (godseye-o8l.6). The INTREP is the artifact a harness actually
# reads; measured live it was 411 KB at 36 tracks, and it grows with a track
# store that persists across runs.
# ---------------------------------------------------------------------------

def _roster(n=25):
    tm = TrackManager()
    obs = {"lat": 33.7230, "lon": 51.7250, "alt_m": 300.0, "vehicle": "Drone1"}
    sensor = {"sensor": "scene", "fov_deg": 20.0, "image_px": 640,
              "light": "day", "weather": "clear"}
    for i in range(n):
        tm.ingest([{"name": f"SA-6_site_{i}",
                    "geo_point": {"latitude": 33.7220 + i * 0.002,
                                  "longitude": 51.7250, "altitude": 0.0}}],
                  observer=obs, sensor=sensor, frame_id=f"f{i}", now=1000.0 + i)
    return tm.tracks()


class TestIntrepSize:
    def test_default_intrep_is_summarised_and_far_smaller(self):
        tracks = _roster()
        full = intrep_report(tracks, now=2000.0, detail="full", top_n=None)
        summary = intrep_report(tracks, now=2000.0)
        assert len(json.dumps(summary)) * 2 < len(json.dumps(full))
        c = summary["contacts"][0]
        # the derivation goes, the verdict stays
        assert "confidence" not in c
        assert c["confidence_level"] in CONFIDENCE_LEVELS
        assert "confidence_score" in c
        # the six SALUTE fields are all still there (M8)
        for k in ("size", "activity", "location", "unit", "time", "equipment"):
            assert k in c, k

    def test_a_truncated_intrep_still_reports_the_whole_picture(self):
        """The counts and gaps must cover every contact, not just the expanded
        ones - otherwise truncation silently shrinks the intelligence picture,
        which is the coverage-overstatement defect wearing a different hat."""
        tracks = _roster()
        out = intrep_report(tracks, now=2000.0, top_n=4)
        assert out["detailed_count"] == 4
        assert out["contacts_omitted_count"] == len(tracks) - 4
        assert out["total_tracks"] == len(tracks)
        assert sum(out["confidence_summary"].values()) == len(tracks)
        assert sum(out["by_category"].values()) == len(tracks)
        assert out["truncation"] and str(len(tracks)) in out["truncation"]
        named = ({c["track_id"] for c in out["contacts"]}
                 | {r["track_id"] for r in out["contacts_omitted"]})
        assert named == {t.track_id for t in tracks}

    def test_full_detail_restores_the_confidence_derivation(self):
        tracks = _roster(3)
        out = intrep_report(tracks, now=2000.0, detail="full")
        assert out["contacts"][0]["confidence"]["evidence"]
        assert out["contacts_omitted_count"] == 0 and out["truncation"] is None

    def test_an_unknown_detail_level_is_refused_not_guessed(self):
        tracks = _roster(2)
        with pytest.raises(ValueError, match="detail="):
            intrep_report(tracks, detail="brief")
        with pytest.raises(ValueError, match="top_n"):
            intrep_report(tracks, top_n=-1)
