"""Phase 5: deterministic threat assessment (M13) + handoff (M10).

Pins PLAN §4.6: (a) OB match, (b) all four intent indicators, (c) confidence
with cited evidence, (d) score = capability x intent with every component
traceable. Plus M14: the only advisory is sensor posture / self-protection.
"""
import itertools
import json
import re
import sys, pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "mcp"))

from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.server import GodseyeUavServer as GodseyeServer
from godseye_uav.threat import (
    ISR_AUTHORITY_NOTE,
    _level_for,
    assess_area,
    assess_capability,
    assess_intent,
    assess_track,
    plan_handoff,
    standoff_m,
)
from godseye_uav.targets import (
    OB_LIBRARY,
    PatternOfLife,
    Track,
    TrackManager,
    classify,
)

_port = itertools.count(45000)

H08 = 8 * 3600.0
H02 = 2 * 3600.0


def _free():
    return next(_port)


def _track(cat="sam", lat=33.7220, lon=51.7250, speed=0.0, heading=None):
    return Track(track_id="TRK-001", name=f"test_{cat}", category=classify(f"test_{cat}"),
                 lat=lat, lon=lon, alt_m=0.0, first_seen=0.0, last_seen=0.0,
                 speed_mps=speed, heading_deg=heading)


def _observed_track(name="SA-6_site_1", lat=33.7220, lon=51.7250, looks=4,
                    t0=1000.0, observer=None, tm=None):
    """A track built from real detections, so it carries citable evidence."""
    tm = tm or TrackManager()
    observer = observer or {"lat": lat + 0.001, "lon": lon, "alt_m": 300.0,
                            "vehicle": "Drone1"}
    sensor = {"sensor": "scene", "fov_deg": 20.0, "image_px": 640,
              "light": "day", "weather": "clear"}
    track = None
    for i in range(looks):
        track = tm.ingest(
            [{"name": name,
              "geo_point": {"latitude": lat, "longitude": lon, "altitude": 0.0}}],
            now=t0 + 15.0 * i, sensor=sensor, observer=observer,
            frame_id=f"FRAME-{i:03d}")[0]
    return track


class TestThreat:
    def test_sam_in_envelope_is_high(self):
        # observer ~110 m from a SAM site (envelope 24 km) → dangerous
        t = _track("sam")
        obs = {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0}
        out = assess_track(t, obs)
        assert out["in_envelope"] is True
        assert out["threat_level"] in ("high", "critical")
        assert out["category"] == "sam"
        assert "envelope" in out["rationale"]

    def test_personnel_far_is_low(self):
        t = _track("personnel")
        obs = {"lat": 33.8000, "lon": 51.8000, "alt_m": 100.0}  # ~11 km away
        out = assess_track(t, obs)
        assert out["in_envelope"] is False
        assert out["threat_level"] in ("none", "low", "moderate")

    def test_intent_raises_score(self):
        # truck heading north straight at a defended asset to its north
        asset = [{"lat": 33.7400, "lon": 51.7250, "name": "FOB"}]
        t_still = _track("vehicle", speed=0.0)
        t_closing = _track("vehicle", speed=15.0, heading=0.0)  # heading north
        obs = {"lat": 33.7000, "lon": 51.7000, "alt_m": 100.0}
        s_still = assess_track(t_still, obs, asset)["threat_score"]
        s_close = assess_track(t_closing, obs, asset)["threat_score"]
        assert s_close > s_still

    def test_area_rollup_orders_by_score(self):
        tracks = [_track("personnel"), _track("sam", lon=51.7260)]
        obs = {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0}
        out = assess_area(tracks, obs)
        assert out["format"] == "THREATREP"
        assert out["count"] == 2
        # SAM should outrank personnel
        assert out["assessments"][0]["category"] == "sam"
        assert out["highest_threat"] in ("high", "critical")

    def test_isr_only_recommendation(self):
        # M14: recommendations never say "engage" / "prosecute" / "strike"
        t = _track("sam")
        obs = {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0}
        rec = assess_track(t, obs)["recommendation"].lower()
        for banned in ("engage", "prosecute", "strike", "attack", "destroy"):
            assert banned not in rec


class TestScoreTraceability:
    """PLAN §4.6(d): score = capability x intent, each component traceable."""

    def test_score_is_the_product_of_capability_and_intent(self):
        t = _track("sam")
        obs = {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0}
        out = assess_track(t, obs)
        assert out["threat_score"] == pytest.approx(
            out["capability"] * out["intent"], abs=2e-3)
        score = out["assessment"]["score"]
        assert score["formula"] == "capability x intent (PLAN §4.6(d))"
        assert score["capability"] == out["capability"]
        assert score["intent"] == out["intent"]

    def test_zero_intent_collapses_the_score(self):
        # multiplicative by construction: no capability survives zero intent
        assert _level_for(1.0 * 0.0) == "none"

    def test_low_intent_keeps_a_capable_in_envelope_contact_below_critical(self):
        """The doctrinal error M13 exists to prevent: under the old additive
        sum a static, non-emitting gun with the observer inside its envelope
        scored 'critical' on capability alone."""
        t = Track(track_id="TRK-A", name="ZU-23_position", category="aaa",
                  lat=33.7220, lon=51.7250, alt_m=0.0, first_seen=0.0,
                  last_seen=0.0, speed_mps=0.0, ob_class="aaa_towed")
        obs = {"lat": 33.7240, "lon": 51.7250, "alt_m": 100.0}   # ~220 m: inside
        out = assess_track(t, obs)
        assert out["in_envelope"] is True
        assert out["capability"] > 0.5            # genuinely capable
        assert out["intent"] < 0.55               # but nothing indicates intent
        assert out["threat_level"] not in ("critical",)

    def test_every_score_component_is_a_number_not_prose(self):
        t = _track("sam")
        obs = {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0}
        out = assess_track(t, obs)
        for key in ("capability", "capability_weight", "envelope_factor",
                    "intent", "threat_score", "confidence_score"):
            assert isinstance(out[key], (int, float)), key
        cap = out["assessment"]["capability"]
        assert cap["value"] == pytest.approx(
            cap["capability_weight"] * (0.35 + 0.65 * cap["engagement_geometry"]),
            abs=1e-3)

    def test_evidence_cites_a_source_for_every_element(self):
        t = _observed_track()
        out = assess_track(t, {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0},
                           now=1045.0)
        assert out["evidence"], "assessment must carry structured evidence"
        for item in out["evidence"]:
            assert item["component"], item
            assert item["element"], item
            assert item["source"], item
        components = {i["component"] for i in out["evidence"]}
        assert "capability" in components
        assert "confidence" in components
        assert any(c.startswith("intent.") for c in components)
        # the OB row that drove capability is named
        assert out["ob_class"] == "sam_medium_range"
        assert any(i["element"] == "ob_class" for i in out["evidence"])


class TestIntentIndicators:
    """PLAN §4.6(b): posture, movement toward asset, POL deviation, emissions."""

    def test_all_four_indicators_are_evaluated(self):
        t = _track("sam")
        out = assess_intent(t, {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0},
                            None, None, now=1000.0)
        names = [i["indicator"] for i in out["indicators"]]
        assert names == ["posture", "movement_toward_asset",
                         "pattern_of_life_deviation", "emissions"]
        assert out["indicator_count"] == 4
        for ind in out["indicators"]:
            assert ind["evidence"], ind["indicator"]
            assert 0.0 <= ind["value"] <= 1.0
            assert ind["contribution"] == pytest.approx(ind["value"] * ind["weight"])

    def test_posture_distinguishes_emplaced_air_defence_from_a_halted_truck(self):
        sam = assess_intent(_track("sam"), None, None, None, 1000.0)["indicators"][0]
        assert sam["state"] == "emplaced_ready"
        truck = Track(track_id="T", name="Ural_Truck", category="logistics",
                      lat=33.72, lon=51.72, alt_m=0.0, first_seen=0.0,
                      last_seen=0.0, speed_mps=0.0, ob_class="supply_truck")
        halted = assess_intent(truck, None, None, None, 1000.0)["indicators"][0]
        assert halted["state"] == "halted"
        assert sam["value"] > halted["value"]

    def test_posture_flags_an_air_defence_system_on_the_move_as_displacing(self):
        moving = Track(track_id="T", name="Tor_M1", category="sam",
                       lat=33.72, lon=51.72, alt_m=0.0, first_seen=0.0,
                       last_seen=0.0, speed_mps=9.0, heading_deg=90.0,
                       ob_class="sam_short_range")
        ind = assess_intent(moving, None, None, None, 1000.0)["indicators"][0]
        assert ind["state"] == "displacing"

    def test_movement_indicator_cites_the_asset_range_and_cone(self):
        asset = [{"lat": 33.7400, "lon": 51.7250, "name": "FOB"}]
        t = _track("vehicle", speed=15.0, heading=0.0)
        ind = assess_intent(t, None, asset, None, 1000.0)["indicators"][1]
        assert ind["value"] > 0.5
        assert "FOB" in ind["state"]
        elements = {e["element"] for e in ind["evidence"]}
        assert {"asset", "range_to_asset_m", "heading_offset_deg"} <= elements

    def test_movement_indicator_is_zero_when_heading_away(self):
        asset = [{"lat": 33.7400, "lon": 51.7250, "name": "FOB"}]
        t = _track("vehicle", speed=15.0, heading=180.0)  # driving south, away
        ind = assess_intent(t, None, asset, None, 1000.0)["indicators"][1]
        assert ind["value"] == 0.0

    def test_pattern_of_life_deviation_feeds_intent(self):
        """M12 store -> §4.6(b) intent indicator."""
        pol = PatternOfLife(min_samples=10)
        pol.define_poi("DEPOT", 33.7220, 51.7250, radius_m=300.0)
        for i in range(20):
            pol.observe(f"TRK-{i}", "logistics", 33.7220, 51.7250, ts=H08 + i)
        armour = Track(track_id="TRK-X", name="T72_Tank", category="armor",
                       lat=33.7220, lon=51.7250, alt_m=0.0, first_seen=H02,
                       last_seen=H02, speed_mps=0.0, ob_class="mbt")
        without = assess_intent(armour, None, None, None, H02)
        with_pol = assess_intent(armour, None, None, pol, H02)
        pol_ind = with_pol["indicators"][2]
        assert pol_ind["value"] > 0.8
        assert "DEPOT" in pol_ind["state"]
        assert with_pol["value"] > without["value"]
        assert without["indicators"][2]["state"] == "no_store"

    def test_emissions_indicator_separates_observed_from_inferred(self):
        inferred = assess_intent(_track("sam"), None, None, None, 1000.0)["indicators"][3]
        assert inferred["state"] == "emitter class, no ESM observation"
        assert inferred["evidence"][0].get("inferred") is True

        tm = TrackManager()
        t = None
        for i in range(2):
            t = tm.ingest([{"name": "SA-6_site_1",
                            "geo_point": {"latitude": 33.722, "longitude": 51.725,
                                          "altitude": 0.0},
                            "emitter_active": True}],
                          now=1000.0 + i, frame_id=f"F{i}")[0]
        observed = assess_intent(t, None, None, None, 1002.0)["indicators"][3]
        assert observed["state"] == "emission observed"
        assert observed["value"] > inferred["value"]
        assert observed["evidence"][0].get("inferred") is not True

    def test_non_emitter_scores_zero_emissions(self):
        truck = Track(track_id="T", name="Ural_Truck", category="logistics",
                      lat=33.72, lon=51.72, alt_m=0.0, first_seen=0.0,
                      last_seen=0.0, ob_class="supply_truck")
        ind = assess_intent(truck, None, None, None, 1000.0)["indicators"][3]
        assert ind["value"] == 0.0
        assert ind["state"] == "non-emitter"


class TestCapability:
    def test_capability_uses_the_ob_row_not_a_coarse_weight(self):
        cap = assess_capability(_track("sam"),
                                {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0})
        ob = OB_LIBRARY["sam_medium_range"]
        assert cap["envelope_m"] == ob.weapon_range_m
        assert cap["acquisition_range_m"] == ob.acquisition_range_m
        assert cap["mobility"] == ob.mobility
        assert cap["envelope_asserted"] is True

    def test_observer_above_the_engagement_ceiling_reduces_capability(self):
        t = Track(track_id="T", name="ZSU-23-4", category="aaa", lat=33.722,
                  lon=51.725, alt_m=0.0, first_seen=0.0, last_seen=0.0,
                  ob_class="aaa_self_propelled")   # ceiling 1500 m
        low = assess_capability(t, {"lat": 33.7222, "lon": 51.725, "alt_m": 500.0})
        high = assess_capability(t, {"lat": 33.7222, "lon": 51.725, "alt_m": 6000.0})
        assert low["in_envelope"] is True
        assert high["in_envelope"] is False
        assert high["above_weapon_ceiling"] is True
        assert high["value"] < low["value"]

    def test_unclassified_contact_gets_a_prudent_standoff_not_a_borrowed_envelope(self):
        t = Track(track_id="T", name="object_9", category="unknown", lat=33.72,
                  lon=51.72, alt_m=0.0, first_seen=0.0, last_seen=0.0)
        cap = assess_capability(t, {"lat": 33.7202, "lon": 51.72, "alt_m": 100.0})
        assert cap["envelope_asserted"] is False
        assert cap["envelope_m"] == 0.0
        assert cap["prudent_standoff_m"] == 1500.0

    def test_climbing_above_the_ceiling_never_raises_capability(self):
        """M13d regression: the above-ceiling branch scaled an UNCLAMPED
        weapon_range/observer_range ratio by 0.25, so for any row whose
        weapon_range/ceiling exceeds 4 (ifv, apc, patrol_boat) the 'penalty'
        came out larger than being inside the envelope — climbing above the
        weapon ceiling RAISED the assessed threat."""
        for key, e in OB_LIBRARY.items():
            if not (e.weapon_range_m > 0.0 and e.weapon_ceiling_m > 0.0):
                continue
            t = Track(track_id="T", name=key, category=e.category, lat=33.72,
                      lon=51.72, alt_m=0.0, first_seen=0.0, last_seen=0.0,
                      ob_class=key)
            below = assess_capability(
                t, {"lat": 33.72, "lon": 51.72, "alt_m": e.weapon_ceiling_m * 0.9})
            above = assess_capability(
                t, {"lat": 33.72, "lon": 51.72, "alt_m": e.weapon_ceiling_m * 1.01})
            assert below["in_envelope"] is True, key
            assert above["above_weapon_ceiling"] is True, key
            assert above["value"] < below["value"], key

    def test_engagement_geometry_is_a_bounded_fraction(self):
        """engagement_geometry is documented as a 0..1 fraction, so capability
        can never exceed the library's own threat_weight for that row."""
        for key, e in OB_LIBRARY.items():
            t = Track(track_id="T", name=key, category=e.category, lat=33.72,
                      lon=51.72, alt_m=0.0, first_seen=0.0, last_seen=0.0,
                      ob_class=key)
            for alt in (0.0, 120.0, 600.0, 3000.0, 30000.0):
                cap = assess_capability(t, {"lat": 33.72, "lon": 51.72,
                                            "alt_m": alt})
                assert 0.0 <= cap["engagement_geometry"] <= 1.0, (key, alt)
                assert cap["value"] <= e.threat_weight + 1e-9, (key, alt)

    def test_standoff_is_derived_from_the_engagement_envelope(self):
        """M5: the server derives standoff, the harness never picks it."""
        assert standoff_m(_track("sam")) == pytest.approx(
            OB_LIBRARY["sam_medium_range"].weapon_range_m * 1.1)
        manpads = Track(track_id="T", name="Igla_team", category="sam",
                        lat=33.72, lon=51.72, alt_m=0.0, first_seen=0.0,
                        last_seen=0.0, ob_class="manpads")
        assert standoff_m(manpads) == pytest.approx(5000.0 * 1.1)


class TestIsrOnly:
    """M14: godSeye reports; command authority stays with the operator."""

    # Whole-word kinetic verbs. Note that the NOUNS 'engagement envelope',
    # 'engagement ceiling' and 'engagement_geometry' are allowed: they describe
    # what a contact can do TO the UAV, which is exactly the self-protection
    # information ISR is supposed to produce.
    BANNED = (r"\bengage\b", r"\bengaging\b", r"\bprosecut", r"\bstrike\b",
              r"\battack\b", r"\bdestroy", r"\bkill\b", r"\bneutrali[sz]e\b",
              r"\bfire mission\b", r"\bweapon release\b", r"\bopen fire\b")

    def _strings(self, obj):
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, dict):
            for k, v in obj.items():
                yield from self._strings(k)
                yield from self._strings(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                yield from self._strings(v)

    def test_no_kinetic_language_anywhere_in_an_assessment(self):
        t = _observed_track()
        out = assess_track(t, {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0},
                           [{"lat": 33.74, "lon": 51.725, "name": "FOB"}],
                           now=1045.0)
        for s in self._strings(out):
            low = s.lower()
            for banned in self.BANNED:
                assert re.search(banned, low) is None, f"{banned!r} matched {s!r}"

    def test_no_kinetic_language_for_any_order_of_battle_class(self):
        """The single-track test above only exercises sam_medium_range. The
        library's role and capability prose is echoed verbatim into every
        SALUTE/INTREP/THREATREP, so the M14 guard has to hold for EVERY row:
        'air superiority / strike' and 'electronic attack' slipped through."""
        obs = {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0}
        for key, e in OB_LIBRARY.items():
            t = Track(track_id="TRK-1", name=f"{key}_1", category=e.category,
                      lat=33.7220, lon=51.7250, alt_m=0.0, first_seen=0.0,
                      last_seen=0.0, speed_mps=0.0, ob_class=key)
            out = assess_track(t, obs, now=10.0)
            for s in self._strings(out):
                low = s.lower()
                for banned in self.BANNED:
                    assert re.search(banned, low) is None, f"{key}: {banned!r} in {s!r}"

    def test_advisory_is_named_and_scoped_as_sensor_posture(self):
        t = _track("sam")
        out = assess_track(t, {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0})
        posture = out["sensor_posture"]
        assert posture["code"] in ("increase_standoff", "maintain_standoff",
                                   "routine_isr")
        assert posture["advisory"].startswith("SENSOR POSTURE:")
        assert posture["scope"] == "sensor employment and aircraft self-protection only"
        assert posture["standoff_m"] > 0.0
        assert out["isr_only"] is True
        assert out["authority"] == ISR_AUTHORITY_NOTE
        # the legacy key is a pure alias, not a second opinion
        assert out["recommendation"] == posture["advisory"]

    def test_threatrep_states_its_authority(self):
        out = assess_area([_track("sam")],
                          {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0})
        assert out["isr_only"] is True
        assert "operator" in out["authority"]


class TestAreaScoping:
    def test_area_polygon_scopes_the_rollup(self):
        sector = [(33.720, 51.720), (33.720, 51.730),
                  (33.730, 51.730), (33.730, 51.720)]
        inside = _track("sam", lat=33.7220, lon=51.7250)
        outside = _track("sam", lat=33.9000, lon=51.9000)
        obs = {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0}
        out = assess_area([inside, outside], obs, area_polygon=sector)
        assert out["count"] == 1
        assert out["tracks_out_of_area"] == 1
        assert out["scoped_by_polygon"] is True
        assert out["scoping_error"] is None
        assert assess_area([inside, outside], obs)["count"] == 2

    def test_degenerate_polygon_is_reported_not_silently_ignored(self):
        """safety.point_in_polygon treats a <3-vertex polygon as 'no geofence'
        and passes every point, so the roll-up used to claim
        scoped_by_polygon=True while scoping nothing at all."""
        inside = _track("sam", lat=33.7220, lon=51.7250)
        outside = _track("sam", lat=33.9000, lon=51.9000)
        obs = {"lat": 33.7230, "lon": 51.7250, "alt_m": 100.0}
        for bad in ([(33.72, 51.72)], [(33.72, 51.72), (33.73, 51.73)]):
            out = assess_area([inside, outside], obs, area_polygon=bad)
            assert out["count"] == 2                   # nothing was scoped out
            assert out["scoped_by_polygon"] is False   # ...and it says so
            assert "at least 3" in out["scoping_error"]


class TestHandoff:
    def test_low_fuel_receiver_rejected(self):
        t = _observed_track()
        ho = plan_handoff(t, "Drone1", "Drone2", to_fuel_pct=20.0, now=1045.0)
        assert ho.accepted is False
        assert "fuel" in ho.reason.lower()

    def test_in_range_receiver_accepted(self):
        t = _observed_track()
        ho = plan_handoff(t, "Drone1", "Drone2", to_fuel_pct=80.0,
                          to_pos={"lat": 33.7240, "lon": 51.7260}, now=1045.0)
        assert ho.accepted is True
        assert "standoff" in ho.reason
        assert ho.standoff_m > 0.0
        assert ho.evidence

    def test_far_receiver_rejected(self):
        t = _observed_track()
        ho = plan_handoff(t, "Drone1", "Drone2", to_fuel_pct=80.0,
                          to_pos={"lat": 36.5000, "lon": 54.5000}, now=1045.0)
        assert ho.accepted is False
        assert "handoff range" in ho.reason

    def test_custody_transfer_requires_a_positive_id(self):
        """M10: you do not hand over custody of a 'possible'."""
        weak = _observed_track(looks=1)
        ho = plan_handoff(weak, "Drone1", "Drone2", to_fuel_pct=80.0,
                          to_pos={"lat": 33.7240, "lon": 51.7260}, now=1000.0)
        assert ho.accepted is False
        assert "positive ID" in ho.reason
        assert ho.confidence == "possible"


def test_assess_threat_tool(tmp_path):
    import asyncio, airsim
    from godseye_uav.server import UavBackend
    from godseye_uav.safety import SafetyEnvelope
    from godseye_uav.store import Store
    from godseye_uav.geo import GeoPoint
    HOME = GeoPoint(47.641468, -122.140165, 93.0)
    AO = [(47.63, -122.16), (47.63, -122.12), (47.66, -122.12), (47.66, -122.16)]
    port = _free()
    sim = FakeAirSim(port=port)
    sim.start()
    srv = None
    try:
        client = airsim.MultirotorClient(port=port)
        client.confirmConnection()
        backend = UavBackend(client, HOME)
        envelope = SafetyEnvelope(geofence=AO, home=(HOME.latitude, HOME.longitude, HOME.altitude))
        with Store(tmp_path) as store:
            srv = GodseyeServer(backend, store, envelope=envelope, watchdog_s=10.0)
            fn = srv.mcp._tool_manager._tools["uav_assess_threat"].fn
            scan = srv.mcp._tool_manager._tools["uav_scan_targets"].fn
            spawn = srv.mcp._tool_manager._tools["sim_spawn_target"].fn
            # place a SAM site ~120 m east of the drone's home
            asyncio.run(spawn(name="SA-6_site_1", mesh="sam", lat=47.641468, lon=-122.1384, alt_m=93.0))
            out = asyncio.run(scan(vehicle="Drone1"))
            tid = out["tracks"][0]["track_id"]
            res = asyncio.run(fn(vehicle="Drone1", track_id=tid))
            assert res["threat_level"] in ("high", "critical")
            assert res["in_envelope"] is True
            assert res["ob_class"] == "sam_medium_range"
            assert res["sensor_posture"]["scope"].startswith("sensor employment")
            res_all = asyncio.run(fn(vehicle="Drone1"))
            assert res_all["format"] == "THREATREP"
    finally:
        if srv is not None:
            try:
                srv.tasking.shutdown()
            except Exception:
                pass
        sim.stop()


# ---------------------------------------------------------------------------
# Report size (godseye-o8l.6): the consumer is an LLM harness, and the roll-up
# grows with a track store that persists across runs. Measured live at 36
# tracks the full THREATREP was 1.1 MB - past what the MCP python client will
# carry, and far past what a harness can read in a context window.
# ---------------------------------------------------------------------------

def _many_tracks(n=25):
    tm = TrackManager()
    obs = {"lat": 33.7230, "lon": 51.7250, "alt_m": 300.0, "vehicle": "Drone1"}
    sensor = {"sensor": "scene", "fov_deg": 20.0, "image_px": 640,
              "light": "day", "weather": "clear"}
    for i in range(n):
        tm.ingest([{"name": f"SA-6_site_{i}",
                    "geo_point": {"latitude": 33.7220 + i * 0.002,
                                  "longitude": 51.7250, "altitude": 0.0}}],
                  observer=obs, sensor=sensor, frame_id=f"f{i}", now=1000.0 + i)
    return tm.tracks(), obs


class TestThreatrepSize:
    def test_default_rollup_is_summarised_and_far_smaller(self):
        tracks, obs = _many_tracks()
        full = assess_area(tracks, obs, now=2000.0, detail="full", top_n=None)
        summary = assess_area(tracks, obs, now=2000.0)
        assert len(json.dumps(summary)) * 4 < len(json.dumps(full)), (
            "the default roll-up must be dramatically smaller than the full one"
        )
        entry = summary["assessments"][0]
        # The expandable sub-objects go...
        assert "assessment" not in entry
        assert "evidence" not in entry
        # ...but every flat, traceable score component survives (PLAN 4.6(d)).
        for k in ("threat_score", "threat_level", "capability", "intent",
                  "confidence_level", "confidence_score", "envelope_m",
                  "observer_range_m", "sensor_posture"):
            assert k in entry, k
        # ISR authority is stated once on the report, not on every contact.
        assert summary["authority"]
        assert "authority" not in entry

    def test_truncation_is_explicit_and_nothing_vanishes(self):
        tracks, obs = _many_tracks()
        out = assess_area(tracks, obs, now=2000.0, top_n=5)
        assert out["count"] == len(tracks)
        assert out["detailed_count"] == 5
        assert out["omitted_count"] == len(tracks) - 5
        assert out["truncation"] and str(out["omitted_count"]) in out["truncation"]
        # every omitted contact is still NAMED - a shortened ISR report that
        # quietly loses contacts is the coverage-overstatement defect again
        seen = ({a["track_id"] for a in out["assessments"]}
                | {r["track_id"] for r in out["omitted"]})
        assert seen == {t.track_id for t in tracks}
        for row in out["omitted"]:
            assert {"track_id", "threat_level", "threat_score"} <= set(row)

    def test_full_detail_restores_the_evidence(self):
        tracks, obs = _many_tracks(3)
        out = assess_area(tracks, obs, now=2000.0, detail="full")
        entry = out["assessments"][0]
        assert entry["assessment"]["capability"]["evidence"]
        assert entry["evidence"]
        assert out["omitted_count"] == 0 and out["truncation"] is None

    def test_an_unknown_detail_level_is_refused_not_guessed(self):
        tracks, obs = _many_tracks(2)
        with pytest.raises(ValueError, match="detail="):
            assess_area(tracks, obs, detail="verbose")
        with pytest.raises(ValueError, match="top_n"):
            assess_area(tracks, obs, top_n=-1)
