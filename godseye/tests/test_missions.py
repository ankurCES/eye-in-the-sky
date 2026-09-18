"""Mission doctrine tests (PLAN Phase 3: M1/M2/M4/M5/M7 + the coverage producer).

Every test here pins a behaviour the shipped code did NOT have:

  M1  the grid plan reports the lane spacing and the coverage it ACTUALLY
      flies, never the uncapped sensor-derived spacing it did not.
  M2  recon captures are distance-triggered; a time interval is only a clamp.
  M5  track standoff is derived from the contact's order-of-battle weapon
      envelope and the pixel density an ID needs, then LOS-verified.
  M7  identify cross-cues wide FOV -> narrow FOV at a reduced slant range.
  M4  every plan is priced by the pre-flight gate, and `mission_dry_run`
      produces the whole plan product without executing anything.
"""
import asyncio
import itertools
import math
import time

import airsim
import pytest
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint
from godseye_uav.missions import (
    CAMERAS,
    DETECT_PIXELS,
    IDENTIFY_PIXELS,
    LosBlockedError,
    LosUnavailableError,
    PlanTooLargeError,
    camera,
    capture_interval_m,
    capture_points,
    coverage_of_path,
    dry_run,
    expanding_square_waypoints,
    flown_coverage,
    footprint_m,
    grid_search_plan,
    grid_sweep_spacing_m,
    id_altitude_band,
    identify_plan,
    lane_spacing_m,
    lawnmower_waypoints,
    max_slant_for_pixels,
    mission_coverage,
    orbit_waypoints,
    pixels_on_target,
    plan_mission,
    polygon_area_m2,
    recon_route_plan,
    repath_needed,
    repath_track,
    route_length_m,
    standoff_for_track,
    swath_m,
    track_target_plan,
)
from godseye_uav.safety import FuelModel, SafetyEnvelope, haversine_m
from godseye_uav.server import GodseyeUavServer, UavBackend
from godseye_uav.store import Store
from godseye_uav.targets import Track
from godseye_uav.threat import standoff_m as threat_standoff_m

HOME = GeoPoint(47.641468, -122.140165, 93.0)
AO = [(47.63, -122.16), (47.63, -122.12), (47.66, -122.12), (47.66, -122.16)]
SMALL_BOX = [(47.6412, -122.1404), (47.6412, -122.1398),
             (47.6418, -122.1398), (47.6418, -122.1404)]
_PORT = itertools.count(47300)   # this agent's assigned port range


def run(coro):
    return asyncio.run(coro)


def _track(ob_class="mbt", lat=47.6430, lon=-122.1400, **kw):
    now = time.time()
    return Track(track_id=kw.pop("track_id", "T-0001"),
                 name=kw.pop("name", "contact"),
                 category=kw.pop("category", "armor"),
                 lat=lat, lon=lon, alt_m=kw.pop("alt_m", 0.0),
                 first_seen=now, last_seen=now, ob_class=ob_class, **kw)


def _los_clear(*_args):
    return {"los": True, "first_obstacle": None, "model": "test-stub"}


def _env():
    return SafetyEnvelope(geofence=AO, home=(HOME.latitude, HOME.longitude,
                                             HOME.altitude))


def _fuel():
    return FuelModel(home=(HOME.latitude, HOME.longitude, HOME.altitude))


# =========================================================================
# Sensor footprint maths (M1 basis)
# =========================================================================

def test_footprint_scales_with_altitude():
    w60, _ = footprint_m(60.0)
    w120, _ = footprint_m(120.0)
    assert w120 == pytest.approx(2 * w60, rel=1e-6)


def test_grid_spacing_derived_from_footprint():
    alt = 60.0
    w, _ = footprint_m(alt)
    assert grid_sweep_spacing_m(alt) == pytest.approx(w * 0.8, rel=1e-6)
    assert lane_spacing_m(alt, overlap_pct=0.2) == pytest.approx(w * 0.8, rel=1e-6)


def test_swath_is_the_plan_formula():
    """M1: swath = 2*alt*tan(HFOV/2) — the only source of lane spacing."""
    alt, hfov = 75.0, 60.0
    assert swath_m(alt, hfov) == pytest.approx(
        2 * alt * math.tan(math.radians(hfov) / 2), rel=1e-9)


def test_zero_altitude_has_no_footprint():
    with pytest.raises(ValueError):
        footprint_m(0.0)


def test_percent_style_overlap_is_refused_not_coerced():
    """20 must not be silently read as 0.20 (or as 2000% overlap)."""
    with pytest.raises(ValueError, match="fraction"):
        lane_spacing_m(60.0, overlap_pct=20)
    with pytest.raises(ValueError):
        grid_search_plan("D", SMALL_BOX, 60.0, overlap_pct=20)


def test_unknown_camera_is_refused_never_defaulted():
    with pytest.raises(ValueError, match="unknown camera"):
        camera("thermal_9000")
    assert camera(None).name == "0"
    assert camera("eo_narrow").narrow_hfov_deg < CAMERAS["eo_narrow"].hfov_deg


# =========================================================================
# M1 — grid search tells the truth about coverage
# =========================================================================

def test_lawnmower_covers_polygon_and_alternates():
    poly = [(47.640, -122.142), (47.640, -122.138),
            (47.643, -122.138), (47.643, -122.142)]
    wps = lawnmower_waypoints(poly, 60.0)
    assert len(wps) >= 4
    assert wps[0]["lat"] == pytest.approx(wps[1]["lat"])
    assert (wps[1]["lon"] > wps[0]["lon"]) != (wps[3]["lon"] > wps[2]["lon"])
    for wp in wps:
        assert 47.640 - 1e-6 <= wp["lat"] <= 47.643 + 1e-6


def test_waypoints_state_their_altitude_datum():
    """TOOL_CONTRACT: never a bare alt. Every waypoint carries alt_agl_m."""
    for wp in lawnmower_waypoints(SMALL_BOX, 45.0):
        assert wp["alt_agl_m"] == 45.0
        assert wp["alt_m"] == wp["alt_agl_m"]


def test_grid_search_is_not_silently_capped():
    """The shipped code capped at 12 lanes. A full plan must be a full plan."""
    plan = grid_search_plan("D", AO, 60.0)
    assert plan.truncated is False
    assert plan.meta["lanes"] == plan.meta["lanes_required"] > 12
    assert plan.coverage.coverage_pct > 99.0


def test_grid_search_reports_the_spacing_it_actually_flies():
    """M1 headline: a truncated plan must NOT report the uncapped spacing.

    The shipped code capped at 12 lanes and reported the sensor-derived
    55.4 m spacing while flying 303.6 m lanes — a ~4.4x overstatement.
    """
    derived = lane_spacing_m(60.0, overlap_pct=0.2)
    assert derived == pytest.approx(55.43, abs=0.1)

    plan = grid_search_plan("D", AO, 60.0, max_lanes=12)
    assert plan.meta["lanes"] == 12
    assert plan.meta["lanes_required"] > 12
    # what the aircraft actually flies, not what the sensor would like
    assert plan.meta["lane_spacing_m"] == pytest.approx(303.6, abs=1.0)
    assert plan.meta["lane_spacing_derived_m"] == pytest.approx(derived, abs=0.1)
    # the legacy key can no longer disagree with the flown geometry
    assert plan.meta["sweep_spacing_m"] == plan.meta["lane_spacing_m"]

    # and the flown spacing is derivable from the waypoints themselves
    lats = sorted({round(w["lat"], 9) for w in plan.waypoints})
    step_m = (lats[1] - lats[0]) * 111320.0
    assert step_m == pytest.approx(plan.meta["lane_spacing_m"], rel=0.02)


def test_reported_coverage_equals_coverage_actually_flown():
    """The bug that mattered: ~77% of the AO went unimaged while the tool
    reported full-overlap coverage. Coverage is now recomputed from the
    plan's own waypoints and must agree with what is reported."""
    plan = grid_search_plan("D", AO, 60.0, max_lanes=12)
    cov = plan.coverage.to_dict()

    assert cov["coverage_pct"] < 30.0, "12 lanes over a 3.3 km AO cannot cover it"
    assert cov["uncovered_km2"] > 0.0
    # independently recompute from the waypoints that will actually be flown
    recomputed = coverage_of_path(AO, plan.waypoints, swath_m(60.0))
    assert cov["coverage_pct"] == pytest.approx(recomputed.coverage_pct, abs=0.5)

    # geometric sanity: 12 swaths of ~69 m spread over a ~3340 m span
    expected = 100.0 * (11 * swath_m(60.0) + swath_m(60.0)) / 3339.6
    assert cov["coverage_pct"] == pytest.approx(expected, abs=3.0)


def test_truncation_is_reported_with_a_reason():
    plan = grid_search_plan("D", AO, 60.0, max_lanes=12)
    assert plan.truncated is True
    assert "truncated" in plan.truncation_reason
    assert "303.6" in plan.truncation_reason or "303" in plan.truncation_reason
    assert any(w.startswith("coverage_thinned") for w in plan.warnings)


def test_estimates_match_the_route_actually_planned():
    """T5/M1: est_time_s and est_fuel_pct price the flown plan, truncation
    included — not the full plan the caller asked for."""
    env, poly = _env(), AO
    capped = grid_search_plan("D", poly, 60.0, max_lanes=12)
    full = grid_search_plan("D", poly, 60.0)
    p_capped = dry_run(capped, _fuel(), env, start=(HOME.latitude, HOME.longitude))
    p_full = dry_run(full, _fuel(), env, start=(HOME.latitude, HOME.longitude))

    assert p_capped["est_time_s"] < p_full["est_time_s"] / 3
    assert p_capped["est_fuel_pct"] < p_full["est_fuel_pct"] / 3
    # the very same integrator the in-flight tick uses (T5)
    independent = _fuel().preflight_gate(
        capped.waypoints, (HOME.latitude, HOME.longitude), capped.speed_mps)
    assert p_capped["est_fuel_pct"] == independent["plan_fuel_pct"]
    assert p_capped["est_time_s"] == independent["est_time_s"]


def test_oversized_grid_is_refused_not_capped():
    """No silent fallback: an unflyable plan raises instead of shrinking."""
    with pytest.raises(PlanTooLargeError, match="NOT silently capped"):
        grid_search_plan("D", AO, 3.0, overlap_pct=0.8)


def test_expanding_square_pattern_available():
    plan = grid_search_plan("D", SMALL_BOX, 60.0, pattern="expanding-square",
                            legs=8)
    assert plan.meta["pattern"] == "expanding-square"
    assert len(plan.waypoints) == 9
    assert plan.coverage is not None


def test_unknown_grid_pattern_is_refused():
    with pytest.raises(ValueError, match="unknown grid pattern"):
        grid_search_plan("D", SMALL_BOX, 60.0, pattern="spiral")


def test_expanding_square_legs_grow_by_lane_spacing():
    step = lane_spacing_m(60.0, overlap_pct=0.2)
    wps = expanding_square_waypoints((47.64, -122.14), 60.0, legs=4)
    m_lat = 111320.0
    m_lon = m_lat * math.cos(math.radians(47.64))
    lengths = [math.hypot((b["lat"] - a["lat"]) * m_lat,
                          (b["lon"] - a["lon"]) * m_lon)
               for a, b in itertools.pairwise(wps)]
    assert lengths[0] == pytest.approx(step, rel=0.02)
    assert lengths[2] == pytest.approx(2 * step, rel=0.02)


# =========================================================================
# The coverage producer the INTREP needs (PLAN §4.7)
# =========================================================================

def test_polygon_area_matches_geometry():
    # ~0.0006 deg lat x 0.0006 deg lon box near 47.64
    box = [(47.6412, -122.1404), (47.6412, -122.1398),
           (47.6418, -122.1398), (47.6418, -122.1404)]
    h = 0.0006 * 111320.0
    w = 0.0006 * 111320.0 * math.cos(math.radians(47.6415))
    assert polygon_area_m2(box) == pytest.approx(h * w, rel=0.01)


def test_coverage_of_full_plan_is_complete_and_partial_plan_is_not():
    full = grid_search_plan("D", SMALL_BOX, 40.0)
    assert coverage_of_path(SMALL_BOX, full.waypoints,
                            swath_m(40.0)).coverage_pct > 99.0
    half = full.waypoints[: len(full.waypoints) // 2]
    partial = coverage_of_path(SMALL_BOX, half, swath_m(40.0))
    assert 30.0 < partial.coverage_pct < 75.0


def test_flown_coverage_reports_what_was_flown_not_what_was_planned():
    """The INTREP's empty 'coverage %' slot: a producer that takes the
    telemetry trail and returns the §4.7 coverage section."""
    plan = grid_search_plan("D", SMALL_BOX, 40.0)
    flown = plan.waypoints[: len(plan.waypoints) // 2]   # aborted half way
    cov = flown_coverage(SMALL_BOX, flown, 40.0)
    assert cov.basis == "flown"
    d = cov.to_dict()
    assert set(d) >= {"planned_area_km2", "covered_area_km2", "coverage_pct",
                      "method"}
    assert d["coverage_pct"] < plan.coverage.coverage_pct
    assert "swath sweep" in d["method"] and "sampled on a" in d["method"]


def test_mission_coverage_prefers_the_flown_track():
    plan = grid_search_plan("D", SMALL_BOX, 40.0)
    planned = mission_coverage(plan)
    flown = mission_coverage(plan, flown_path=plan.waypoints[:4])
    assert planned["basis"] == "planned"
    assert flown["basis"] == "flown"
    assert flown["coverage_pct"] < planned["coverage_pct"]


def test_coverage_slot_fits_the_intrep_template():
    from godseye_uav.targets import intrep_report
    plan = grid_search_plan("D", SMALL_BOX, 40.0, max_lanes=2)
    rep = intrep_report([], coverage=plan.coverage.to_dict())
    assert rep["coverage"]["coverage_pct"] == plan.coverage.to_dict()["coverage_pct"]
    # a thin plan must surface as a collection gap, not as silent success
    assert any(g["type"] == "area_not_covered" for g in rep["gaps"])


def test_mission_coverage_refuses_to_pass_planned_off_as_flown():
    """A flown trail handed to a plan with no tasked polygon was silently
    ignored and the PLANNED figure returned under `basis: 'planned'` — the
    caller asked what was imaged and got what was intended."""
    plan = recon_route_plan("D", ROUTE, 60.0)
    with pytest.raises(ValueError, match="no tasked polygon"):
        mission_coverage(plan, flown_path=plan.waypoints)
    # with the AO named explicitly it answers, and answers about the trail
    scored = mission_coverage(plan, flown_path=plan.waypoints, polygon=AO)
    assert scored["basis"] == "flown"


def test_zero_sampling_cell_is_refused_not_re_derived():
    """`if cell_m else` read a literal 0 as 'not supplied'."""
    with pytest.raises(ValueError, match="cell_m must be > 0"):
        coverage_of_path(SMALL_BOX, lawnmower_waypoints(SMALL_BOX, 40.0),
                         swath_m(40.0), cell_m=0)


def test_degenerate_polygon_raises_rather_than_reporting_zero():
    with pytest.raises(ValueError):
        coverage_of_path([(47.64, -122.14), (47.64, -122.14)],
                         [{"lat": 47.64, "lon": -122.14}], 60.0)


# =========================================================================
# M2 — recon route: distance-triggered captures
# =========================================================================

ROUTE = [(47.6415, -122.1405), (47.6450, -122.1405), (47.6450, -122.1350)]


def test_recon_route_has_a_capture_plan_at_all():
    """The shipped recon_route returned the caller's waypoints verbatim: no
    overlap parameter, no captures, no ISR."""
    plan = recon_route_plan("D", ROUTE, 60.0, forward_overlap_pct=0.2)
    assert plan.captures, "a recon route that images nothing is not ISR"
    assert plan.meta["capture_trigger"] == "distance"


def test_capture_interval_is_the_along_track_footprint_rule():
    """M2: every along_track_swath*(1-forward_overlap_pct) metres."""
    _, along = footprint_m(60.0)
    assert capture_interval_m(60.0, forward_overlap_pct=0.2) == pytest.approx(
        along * 0.8, rel=1e-9)
    plan = recon_route_plan("D", ROUTE, 60.0, forward_overlap_pct=0.2)
    assert plan.meta["capture_every_m"] == pytest.approx(along * 0.8, abs=0.05)


def test_captures_are_spaced_by_distance_along_the_route():
    plan = recon_route_plan("D", ROUTE, 60.0, forward_overlap_pct=0.2)
    caps = plan.captures
    m_lat = 111320.0
    m_lon = m_lat * math.cos(math.radians(47.643))
    for a, b in itertools.pairwise(caps):
        step = math.hypot((b["lat"] - a["lat"]) * m_lat,
                          (b["lon"] - a["lon"]) * m_lon)
        # equal along-track spacing; the corner leg is the only chord shortcut
        assert step <= plan.meta["capture_every_m"] + 0.05
    assert caps[-1]["along_track_m"] > caps[0]["along_track_m"]


def test_higher_forward_overlap_triggers_more_captures():
    sparse = recon_route_plan("D", ROUTE, 60.0, forward_overlap_pct=0.1)
    dense = recon_route_plan("D", ROUTE, 60.0, forward_overlap_pct=0.8)
    assert len(dense.captures) > 3 * len(sparse.captures)


def test_time_interval_only_ever_clamps_the_rate():
    """M2: 'a time interval may only act as a max-rate clamp'."""
    fast = recon_route_plan("D", ROUTE, 60.0, forward_overlap_pct=0.2,
                            speed_mps=18.0, max_capture_rate_hz=0.25)
    assert fast.meta["capture_rate_clamped"] is True
    assert fast.meta["capture_every_m"] > fast.meta["capture_interval_requested_m"]
    # the overlap actually achieved is reported, gaps and all — never assumed
    assert fast.meta["forward_overlap_achieved_pct"] < 0.2
    assert any(w.startswith("capture_rate_clamped") for w in fast.warnings)
    assert any(w.startswith("forward_coverage_gap") for w in fast.warnings)

    # a generous rate clamp must not move the distance trigger at all
    slow = recon_route_plan("D", ROUTE, 60.0, forward_overlap_pct=0.2,
                            speed_mps=8.0, max_capture_rate_hz=5.0)
    assert slow.meta["capture_rate_clamped"] is False
    assert slow.meta["capture_every_m"] == slow.meta["capture_interval_requested_m"]


def test_capture_schedule_is_sized_before_it_is_built():
    """The capture list was unbounded: MAX_PLAN_WAYPOINTS guards the waypoint
    list only, and a 2-waypoint route is legal by waypoint count at ANY length.

    Measured on the shipped code over the live MCP server: one
    `uav_mission(kind='recon_route')` call with a 1000 km route and
    forward_overlap_pct=0.99 materialised 1,001,881 capture dicts and 405 MB of
    RSS before the geofence gate threw the plan away. It is now refused up
    front, like an oversized grid — never truncated, never silently built.
    """
    long_route = [(47.0, -122.0), (47.9, -122.0)]          # ~100 km
    with pytest.raises(PlanTooLargeError, match="NOT silently capped"):
        recon_route_plan("D", long_route, 60.0, forward_overlap_pct=0.99,
                         max_capture_rate_hz=1000.0, speed_mps=0.5)
    # and the refusal names both numbers so the caller can act on it
    with pytest.raises(PlanTooLargeError) as exc:
        capture_points(long_route, 1.0)
    assert "captures" in str(exc.value) and "km route" in str(exc.value)

    # a schedule inside the limit is still built normally
    ok = recon_route_plan("D", long_route, 60.0, forward_overlap_pct=0.2,
                          speed_mps=10.0)
    assert 0 < len(ok.captures) <= 20_000


def test_route_length_is_measured_not_guessed():
    length = route_length_m([(47.0, -122.0), (47.09, -122.0)])
    assert length == pytest.approx(0.09 * 111320.0, rel=0.01)
    assert route_length_m([(47.0, -122.0)]) == 0.0


def test_capture_points_start_at_the_route_start():
    caps = capture_points([{"lat": 47.64, "lon": -122.14},
                           {"lat": 47.65, "lon": -122.14}], 100.0)
    assert caps[0]["along_track_m"] == 0.0
    assert caps[1]["along_track_m"] == pytest.approx(100.0)


def test_recon_route_needs_two_waypoints():
    with pytest.raises(ValueError):
        recon_route_plan("D", [(47.64, -122.14)], 60.0)


# =========================================================================
# M5 — track target: server-derived standoff, LOS-verified
# =========================================================================

def test_standoff_comes_from_the_threat_ring_not_the_caller():
    tk = _track("mbt")
    so = standoff_for_track(tk, alt_agl_m=120.0)
    assert so["standoff_m"] == pytest.approx(threat_standoff_m(tk), abs=0.1)
    assert so["standoff_m"] == pytest.approx(tk.ob.weapon_range_m * 1.1, abs=0.1)
    assert so["ob_class"] == "mbt"
    assert "engagement envelope" in so["basis"]


def test_standoff_scales_with_the_contacts_weapon_envelope():
    near = standoff_for_track(_track("infantry_squad", category="personnel"),
                              alt_agl_m=120.0)
    far = standoff_for_track(_track("sam_long_range", category="sam"),
                             alt_agl_m=120.0)
    assert far["standoff_m"] > 50 * near["standoff_m"]


def test_standoff_reports_when_identification_is_not_achievable():
    """An S-300's 82.5 km ring is far outside the sensor's ID range. The plan
    must SAY so, not quietly close inside the envelope for a better picture."""
    so = standoff_for_track(_track("sam_long_range", category="sam"),
                            alt_agl_m=120.0)
    assert so["id_achievable"] is False
    assert so["limiting_factor"] == "sensor_resolution"
    assert so["expected_pixels_on_target"] < so["min_pixels_on_target"]
    assert so["standoff_m"] >= so["threat_ring_m"]   # never traded away


def test_pixel_density_maths_matches_the_intel_chain():
    """The planner and targets.Observation.resolution_score must agree."""
    from godseye_uav.targets import Observation
    cam = camera("0")
    obs = Observation(ts=0.0, lat=0.0, lon=0.0, slant_range_m=1200.0,
                      fov_deg=cam.narrow_hfov_deg, image_px=cam.image_px_w)
    _, _, px = obs.resolution_score(9.5)
    assert px == pytest.approx(
        pixels_on_target(9.5, 1200.0, cam.narrow_hfov_deg, cam.image_px_w), rel=1e-9)


def test_an_ob_row_with_no_size_cannot_buy_a_pixel_density_standoff():
    """`max_slant_for_pixels` refuses a size-less OB row by design — but the
    callers defeated that with `ob.size_m if ob.size_m > 0 else 1.0`, so the
    standoff, expected_pixels_on_target and id_achievable came back computed
    against a nominal 1 m target and were reported as if measured."""
    import dataclasses

    from godseye_uav.targets import OB_LIBRARY

    sizeless = dataclasses.replace(OB_LIBRARY["mbt"], size_m=0.0)

    class _StubTrack:
        track_id = "T-SIZELESS"
        lat, lon, alt_m = 47.6430, -122.1400, 0.0
        ob = sizeless

    with pytest.raises(ValueError, match="carries no size_m"):
        standoff_for_track(_StubTrack(), alt_agl_m=120.0)
    with pytest.raises(ValueError, match="carries no size_m"):
        identify_plan("D", _StubTrack(), alt_agl_m=400.0, los_check=_los_clear)


def test_max_slant_for_pixels_inverts_the_pixel_maths():
    r = max_slant_for_pixels(9.5, fov_deg=5.0, image_px=640,
                             min_pixels=IDENTIFY_PIXELS)
    assert pixels_on_target(9.5, r, 5.0, 640) == pytest.approx(IDENTIFY_PIXELS,
                                                               rel=1e-9)


def test_track_target_refuses_a_caller_supplied_radius():
    """M5: 'Never a caller-supplied radius.'"""
    with pytest.raises(ValueError, match="threat ring"):
        plan_mission("track_target", "D", lat=47.643, lon=-122.14, radius_m=80.0)


def test_track_target_requires_a_los_check():
    """The standoff must be VERIFIED, not asserted."""
    with pytest.raises(LosUnavailableError, match="los_check"):
        track_target_plan("D", _track("mbt"))


def test_track_target_plan_is_los_verified():
    plan = track_target_plan("D", _track("mbt"), alt_agl_m=120.0,
                             los_check=_los_clear)
    assert plan.meta["los"]["verified"] is True
    assert plan.meta["los"]["points_checked"] == len(plan.waypoints)
    assert plan.meta["standoff_m"] == plan.meta["standoff"]["threat_ring_m"]
    assert plan.meta["track_id"] == "T-0001"


def test_unverified_standoff_is_stamped_not_silent():
    plan = track_target_plan("D", _track("mbt"), los_check=None,
                             allow_unverified=True)
    assert plan.meta["los"]["verified"] is False
    assert any(w.startswith("los_unverified") for w in plan.warnings)


def test_masked_ring_points_are_dropped_and_reported():
    tk = _track("mbt")

    def half_blocked(f_lat, f_lon, f_alt, t_lat, t_lon, t_alt):
        return {"los": f_lon < tk.lon, "model": "test-stub"}

    plan = track_target_plan("D", tk, alt_agl_m=120.0, los_check=half_blocked)
    assert 0 < len(plan.waypoints) < plan.meta["los"]["points_checked"]
    assert plan.meta["los"]["blocked"]
    assert any(w.startswith("los_partial") for w in plan.warnings)


def test_fully_masked_track_is_refused():
    with pytest.raises(LosBlockedError, match="no line of sight"):
        track_target_plan("D", _track("mbt"),
                          los_check=lambda *a: {"los": False})


def test_malformed_los_result_is_refused():
    with pytest.raises(ValueError, match="'los' boolean"):
        track_target_plan("D", _track("mbt"), los_check=lambda *a: {"ok": True})


def test_repath_loop_follows_a_moving_contact():
    """M5: 'the ring is computed once so a moving contact is lost.'"""
    tk = _track("mbt")
    plan = track_target_plan("D", tk, alt_agl_m=120.0, los_check=_los_clear)
    assert repath_needed(plan, tk) is False
    tk.update(tk.lat + 0.01, tk.lon, 0.0, time.time())   # ~1.1 km north
    assert repath_needed(plan, tk) is True
    new = repath_track(plan, tk, los_check=_los_clear)
    assert new.meta["poi"] == [tk.lat, tk.lon]
    assert new.meta["standoff_m"] == plan.meta["standoff_m"]
    assert new.meta["repath_moved_m"] > 1000.0
    assert repath_needed(new, tk) is False


# =========================================================================
# M7 — identify: wide detect -> narrow cross-cue at reduced slant range
# =========================================================================

def test_identify_has_two_phases_with_different_fovs():
    """The shipped 'assess' was a fixed 6-point ring with no FOV change."""
    plan = identify_plan("D", _track("radar_acquisition", category="radar"),
                         alt_agl_m=400.0, los_check=_los_clear)
    names = [p["name"] for p in plan.phases]
    assert names == ["detect", "identify"]
    wide, narrow = plan.phases
    assert wide["fov_deg"] > narrow["fov_deg"]
    assert wide["fov_deg"] == camera("0").hfov_deg
    assert narrow["fov_deg"] == camera("0").narrow_hfov_deg


def test_identify_cross_cue_reduces_slant_range():
    """M7: 'cross-cue to narrow FOV at reduced slant range for the ID pass.'"""
    plan = identify_plan("D", _track("radar_acquisition", category="radar"),
                         alt_agl_m=400.0, los_check=_los_clear)
    cc = plan.meta["cross_cue"]
    assert cc["id_slant_m"] < cc["detect_slant_m"]
    assert cc["slant_reduction_m"] > 0.0
    assert cc["id_px"] > cc["detect_px"]
    assert plan.meta["id_achievable"] is True
    assert plan.phases[1]["expected_pixels_on_target"] >= IDENTIFY_PIXELS


def test_identify_never_closes_inside_the_threat_ring():
    plan = identify_plan("D", _track("mbt"), alt_agl_m=400.0,
                         los_check=_los_clear)
    ring = plan.meta["threat_ring_m"]
    for phase in plan.phases:
        assert phase["standoff_m"] >= ring - 1e-6


def test_identify_says_so_when_the_id_is_impossible():
    plan = identify_plan("D", _track("sam_long_range", category="sam"),
                         alt_agl_m=400.0, los_check=_los_clear)
    assert plan.meta["id_achievable"] is False
    assert plan.phases[1]["achievable"] is False
    assert any(w.startswith("id_not_achievable") for w in plan.warnings)
    # and it still holds the standoff rather than diving for resolution (M14)
    assert plan.phases[1]["standoff_m"] >= plan.meta["threat_ring_m"]


def test_identify_phase_waypoint_spans_index_the_route():
    plan = identify_plan("D", _track("radar_acquisition", category="radar"),
                         alt_agl_m=400.0, los_check=_los_clear,
                         detect_points=8, id_points=6)
    d, i = plan.phases
    assert d["waypoint_span"] == [0, 8]
    assert i["waypoint_span"] == [8, 14]
    assert len(plan.waypoints) == 14
    for wp in plan.waypoints[8:]:
        assert wp["alt_agl_m"] == pytest.approx(i["alt_agl_m"], abs=0.1)


def test_identify_orbit_first_false_skips_the_detect_ring():
    plan = identify_plan("D", _track("mbt"), alt_agl_m=200.0,
                         los_check=_los_clear, orbit_first=False)
    assert plan.phases[0]["waypoint_span"] == [0, 1]
    assert plan.meta["orbit_first"] is False


def test_unverified_identify_does_not_claim_every_point_is_masked():
    """allow_unverified stamped `clear_indices: []` — 'no point has line of
    sight' — on a plan whose every point is flown. Unverified means unfiltered,
    exactly as track_target_plan already recorded it."""
    plan = identify_plan("D", _track("mbt"), alt_agl_m=400.0,
                         allow_unverified=True)
    los = plan.meta["los"]
    assert los["verified"] is False
    assert len(los["clear_indices"]) == len(plan.waypoints)


def test_identify_requires_los_like_track_target():
    with pytest.raises(LosUnavailableError):
        identify_plan("D", _track("mbt"))


def test_identify_detect_phase_uses_detection_pixel_floor():
    plan = identify_plan("D", _track("radar_acquisition", category="radar"),
                         alt_agl_m=400.0, los_check=_los_clear)
    assert plan.phases[0]["min_pixels"] == DETECT_PIXELS
    assert plan.phases[1]["min_pixels"] == IDENTIFY_PIXELS


def test_sensor_limited_detect_standoff_does_not_cry_wolf():
    """`detect_ground` is derived so the slant lands exactly ON the pixel
    floor when the sensor is the binding constraint, so `detect_px <
    min_detect_pixels` fired at 2.9999999999999996 px on EVERY such plan. A
    marginal warning on every plan trains the operator to ignore the real
    one. (Measured on an `mbt`: size 9.5 m lands just under the floor in
    binary floating point, size 12.0 m lands exactly on it.)"""
    plan = identify_plan("D", _track("mbt"), alt_agl_m=60.0,
                         los_check=_los_clear)
    detect = plan.phases[0]
    # the sensor, not the threat ring, set this standoff
    assert detect["standoff_m"] > plan.meta["threat_ring_m"]
    assert detect["expected_pixels_on_target"] == pytest.approx(DETECT_PIXELS,
                                                                rel=1e-6)
    assert not [w for w in plan.warnings if w.startswith("detect_marginal")], \
        plan.warnings


def test_detect_marginal_still_fires_when_the_ring_really_is_too_far():
    """...and the warning is not simply gone: a threat ring beyond the wide
    field's reach still says so."""
    plan = identify_plan("D", _track("sam_long_range", category="sam"),
                         alt_agl_m=400.0, los_check=_los_clear)
    assert plan.phases[0]["expected_pixels_on_target"] < DETECT_PIXELS
    assert any(w.startswith("detect_marginal") for w in plan.warnings)


# -------------------------------------------------------------------------
# M7 — the ID altitude is SEARCHED, not assumed
# -------------------------------------------------------------------------

def _los_above(min_alt_agl_m: float):
    """A LOS stub that models the one thing the flat sim actually models: a
    higher eye sees further. Everything at or above `min_alt_agl_m` is clear."""
    def check(f_lat, f_lon, f_alt, t_lat, t_lon, t_alt):
        clear = f_alt >= min_alt_agl_m
        return {"los": clear, "model": "test-stub",
                "first_obstacle": (None if clear else
                                   {"type": "horizon", "name": "earth_curvature",
                                    "range_m": 23134.0, "ground_range_m": 26451.0})}
    return check


def test_id_altitude_band_starts_at_the_pixel_optimal_and_climbs():
    band = id_altitude_band(30.0, 30.0, 60.0, steps=4)
    assert band[0] == pytest.approx(30.0)
    assert band[-1] == pytest.approx(60.0)
    assert band == sorted(band)
    # a preferred altitude already at the ceiling is the whole band
    assert id_altitude_band(60.0, 30.0, 60.0, steps=6) == [60.0]
    # and the floor is honoured even if a caller asks for less
    assert id_altitude_band(5.0, 30.0, 60.0, steps=3)[0] == pytest.approx(30.0)


def test_identify_climbs_the_id_ring_when_the_pixel_optimal_altitude_is_masked():
    """THE defect: the ID pass descended to the 30 m floor, which collapses the
    geometric horizon, and then refused because the contact was below it — on a
    contact the 60 m detect ring could see perfectly well."""
    plan = identify_plan("D", _track("sam_medium_range", category="sam"),
                         alt_agl_m=60.0, los_check=_los_above(42.0))
    cc = plan.meta["cross_cue"]
    assert cc["id_alt_pixel_optimal_m"] == pytest.approx(30.0)
    assert cc["id_alt_agl_m"] >= 42.0        # climbed to where it can see
    assert cc["id_alt_agl_m"] <= 60.0        # and no higher than it had to
    assert plan.phases[1]["waypoint_span"][1] > plan.phases[1]["waypoint_span"][0]
    assert any(w.startswith("id_alt_raised_for_los") for w in plan.warnings)
    # every ID waypoint is flown at the altitude the plan says it is
    lo, hi = plan.phases[1]["waypoint_span"]
    for wp in plan.waypoints[lo:hi]:
        assert wp["alt_agl_m"] == pytest.approx(cc["id_alt_agl_m"], abs=0.1)


def test_identify_records_every_altitude_it_tried():
    plan = identify_plan("D", _track("sam_medium_range", category="sam"),
                         alt_agl_m=60.0, los_check=_los_above(42.0))
    search = plan.meta["cross_cue"]["id_alt_search"]
    assert [row["alt_agl_m"] for row in search] == sorted(
        row["alt_agl_m"] for row in search)
    assert search[0]["points_clear"] == 0
    assert search[0]["blocked_by"] and "earth_curvature" in search[0]["blocked_by"]
    assert search[-1]["points_clear"] == search[-1]["points_checked"]
    assert search[-1]["blocked_by"] is None


def test_identify_keeps_the_pixel_optimal_altitude_when_it_is_clear():
    """The search must not climb for its own sake: a clear low pass is the best
    picture and is what gets flown, in ONE candidate."""
    plan = identify_plan("D", _track("sam_medium_range", category="sam"),
                         alt_agl_m=60.0, los_check=_los_clear)
    cc = plan.meta["cross_cue"]
    assert cc["id_alt_agl_m"] == pytest.approx(cc["id_alt_pixel_optimal_m"])
    assert len(cc["id_alt_search"]) == 1
    assert not any(w.startswith("id_alt_raised_for_los") for w in plan.warnings)


def test_identify_refusal_names_the_band_it_searched():
    """A bare 'no line-of-sight point at the cross-cue altitude' is a refusal
    the operator cannot act on. Name the band, the standoff and the blocker."""
    with pytest.raises(LosBlockedError) as exc:
        identify_plan("D", _track("sam_medium_range", category="sam",
                                  track_id="TRK-AB12-0007"),
                      alt_agl_m=60.0, los_check=_los_above(90.0))
    msg = str(exc.value)
    assert "TRK-AB12-0007" in msg
    assert "searched" in msg and "30" in msg and "60 m AGL" in msg
    assert "earth_curvature" in msg               # what actually blocked it
    assert "26400" in msg                         # the standoff it was held at
    assert "max_id_alt_agl_m" in msg              # a lever the caller has
    assert "M14" in msg                           # why the ring is not the lever


def test_identify_refusal_distinguishes_a_wholly_masked_contact():
    """When even the detect ring is masked, say THAT — it is a different
    problem from an ID ring that is merely too low."""
    with pytest.raises(LosBlockedError) as exc:
        identify_plan("D", _track("mbt"), alt_agl_m=60.0,
                      los_check=lambda *a: {"los": False})
    msg = str(exc.value)
    assert "no line of sight to track" in msg
    assert "detect ring" in msg
    assert "searched" in msg


def test_identify_altitude_ceiling_is_a_caller_lever():
    """`max_id_alt_agl_m` is the lever the refusal names, so it has to work."""
    plan = identify_plan("D", _track("sam_medium_range", category="sam"),
                         alt_agl_m=60.0, max_id_alt_agl_m=110.0,
                         los_check=_los_above(90.0))
    assert plan.meta["cross_cue"]["id_alt_agl_m"] >= 90.0
    assert plan.meta["cross_cue"]["id_alt_band_m"] == [30.0, 110.0]


def test_identify_reports_a_ceiling_the_min_agl_floor_overrode():
    """`id_altitude_band` clamps a ceiling below the floor UP to the floor.

    That is the right resolution — the min-AGL floor is the safety limit — but
    doing it silently hands back a plan whose identify ring is flown ABOVE the
    ceiling the caller asked for, with nothing in the plan saying so. A caller
    who set `max_id_alt_agl_m` as an airspace lid would read the returned
    `id_alt_agl_m` as honouring it.
    """
    plan = identify_plan("D", _track("mbt"), alt_agl_m=200.0,
                         max_id_alt_agl_m=20.0, los_check=_los_clear)
    warn = [w for w in plan.warnings if w.startswith("id_alt_ceiling_below_floor")]
    assert warn, plan.warnings
    assert "max_id_alt_agl_m" in warn[0]
    assert "20 m AGL" in warn[0] and "30 m AGL" in warn[0]
    # the plan really is flown at the floor, above the ceiling that was asked for
    assert plan.meta["cross_cue"]["id_alt_agl_m"] == pytest.approx(30.0)
    lo, hi = plan.phases[1]["waypoint_span"]
    assert all(wp["alt_agl_m"] == pytest.approx(30.0)
               for wp in plan.waypoints[lo:hi])
    # ...and a ceiling that is merely the (low) detect altitude says which one
    low = identify_plan("D", _track("mbt"), alt_agl_m=20.0, los_check=_los_clear)
    assert any("the detect altitude" in w for w in low.warnings
               if w.startswith("id_alt_ceiling_below_floor")), low.warnings
    # a normal plan is NOT given this warning
    ok = identify_plan("D", _track("mbt"), alt_agl_m=200.0, los_check=_los_clear)
    assert not [w for w in ok.warnings
                if w.startswith("id_alt_ceiling_below_floor")]


def test_identify_los_indices_still_index_the_combined_route():
    """The two rings are checked separately now; `meta['los']` must keep
    indexing detect+identify or clear_indices stops matching waypoints."""
    plan = identify_plan("D", _track("radar_acquisition", category="radar"),
                         alt_agl_m=60.0, los_check=_los_clear,
                         detect_points=8, id_points=6)
    los = plan.meta["los"]
    assert los["verified"] is True
    assert los["points_checked"] == 14
    assert los["clear_indices"] == list(range(14))
    assert los["arc_pct"] == pytest.approx(100.0)
    assert len(plan.waypoints) == 14


def test_identify_partial_mask_drops_masked_points_in_both_rings():
    tk = _track("radar_acquisition", category="radar")

    def half_blocked(f_lat, f_lon, f_alt, t_lat, t_lon, t_alt):
        return {"los": f_lon < tk.lon, "model": "test-stub"}

    plan = identify_plan("D", tk, alt_agl_m=60.0, los_check=half_blocked,
                         detect_points=8, id_points=6)
    los = plan.meta["los"]
    assert los["points_checked"] == 14
    assert 0 < len(plan.waypoints) < 14
    assert len(plan.waypoints) == len(los["clear_indices"])
    assert max(e["index"] for e in los["blocked"]) < 14
    assert any(w.startswith("los_partial") for w in plan.warnings)
    # both phases lost points, and the spans still index the flown route
    d, i = plan.phases
    assert d["waypoint_span"] == [0, len(plan.waypoints) - (i["waypoint_span"][1]
                                                            - i["waypoint_span"][0])]
    assert i["waypoint_span"][1] == len(plan.waypoints)


# =========================================================================
# M4 — the pre-flight gate and mission_dry_run
# =========================================================================

def test_dry_run_produces_the_plan_product_without_executing():
    """PLAN §5: task -> plan -> dry-run -> execute. The dry run was impossible."""
    plan = grid_search_plan("D", SMALL_BOX, 40.0)
    product = dry_run(plan, _fuel(), _env(), start=(HOME.latitude, HOME.longitude))
    assert product["executed"] is False
    for key in ("waypoints", "est_time_s", "est_fuel_pct", "gate", "coverage",
                "bingo_fuel_pct", "required_pct"):
        assert key in product, key
    assert product["gate"]["ok"] is True
    assert len(product["waypoints"]) == len(plan.waypoints)


def test_dry_run_gate_rejects_a_plan_outside_the_geofence():
    plan = grid_search_plan("D", [(35.0, 51.0), (35.0, 51.01),
                                  (35.01, 51.01), (35.01, 51.0)], 60.0)
    product = dry_run(plan, _fuel(), _env(), start=(HOME.latitude, HOME.longitude))
    assert product["gate"]["ok"] is False
    assert product["gate"]["envelope_violations"]


def test_dry_run_gate_rejects_a_plan_it_cannot_fuel():
    fm = _fuel()
    fm.fuel_pct = 25.0
    product = dry_run(grid_search_plan("D", AO, 60.0), fm, _env(),
                      start=(HOME.latitude, HOME.longitude))
    assert product["gate"]["ok"] is False
    assert product["gate"]["required_pct"] > product["gate"]["available_pct"]


def test_dry_run_gate_rejects_an_over_speed_plan():
    plan = grid_search_plan("D", SMALL_BOX, 40.0, speed_mps=45.0)
    product = dry_run(plan, _fuel(), _env(), start=(HOME.latitude, HOME.longitude))
    assert product["gate"]["ok"] is False
    assert any("max_speed" in v for v in product["gate"]["envelope_violations"])


def test_bingo_fuel_pct_means_the_return_leg_line_not_the_plan_cost():
    """The two contradictory meanings of bingo_fuel_pct: one name, one meaning."""
    fm = _fuel()
    plan = grid_search_plan("D", AO, 60.0)
    product = dry_run(plan, fm, _env(), start=(HOME.latitude, HOME.longitude))
    assert product["bingo_fuel_pct"] == pytest.approx(
        fm.bingo_fuel_pct((HOME.latitude, HOME.longitude), 0.0), abs=0.01)
    assert product["required_pct"] == product["gate"]["required_pct"]
    assert product["required_pct"] > product["bingo_fuel_pct"]
    assert product["est_fuel_pct"] == product["gate"]["plan_fuel_pct"]


def test_dry_run_flags_a_gate_priced_from_home_instead_of_the_vehicle():
    product = dry_run(grid_search_plan("D", SMALL_BOX, 40.0), _fuel(), _env())
    assert any(w.startswith("gate_start_assumed_home") for w in product["warnings"])


def test_dry_run_without_home_or_start_refuses_rather_than_guessing():
    env = SafetyEnvelope(geofence=AO)   # no home configured
    with pytest.raises(ValueError, match="start position"):
        dry_run(grid_search_plan("D", SMALL_BOX, 40.0), FuelModel(), env)


def test_dry_run_carries_plan_warnings_through_to_the_product():
    plan = grid_search_plan("D", AO, 60.0, max_lanes=12)
    product = dry_run(plan, _fuel(), _env(), start=(HOME.latitude, HOME.longitude))
    assert product["truncated"] is True
    assert any(w.startswith("coverage_thinned") for w in product["warnings"])
    assert product["coverage"]["coverage_pct"] < 30.0


# =========================================================================
# A geofence rejection on a SERVER-derived radius has to say which of the two
# is too small (the register finding "the derived orbit is outside the AO and
# the mission can never be flown", reported as 14 bare wpN:geofence strings).
# =========================================================================

#: The shipped default theater's AO: ~1.1 x 0.75 km around the Redmond origin.
TIGHT_AO = [(47.636468, -122.145165), (47.636468, -122.135165),
            (47.646468, -122.135165), (47.646468, -122.145165)]


def _tight_env():
    return SafetyEnvelope(geofence=TIGHT_AO,
                          home=(47.641468, -122.140165, 122.0))


def test_geofence_rejection_names_a_threat_ring_wider_than_the_ao():
    tk = _track("mbt", lat=47.6415, lon=-122.1402)
    plan = identify_plan("D", tk, alt_agl_m=60.0, los_check=_los_clear)
    product = dry_run(plan, _fuel(), _tight_env(), start=(47.641468, -122.140165))
    assert product["gate"]["ok"] is False
    detail = product["gate"]["standoff_vs_ao"]
    assert detail["limiting_factor"] == "threat_ring"
    assert detail["threat_ring_m"] == pytest.approx(1650.0, abs=1.0)
    assert detail["ao_reach_m"] < 1000.0
    assert detail["shortfall_m"] > 600.0
    assert detail["ob_class"] == "mbt"
    assert detail["waypoints_rejected"] == len(plan.waypoints)
    assert detail["summary"] in product["warnings"]
    assert "M14" in detail["summary"]


def test_geofence_rejection_names_the_detect_ring_when_the_threat_ring_fits():
    """A 300 m threat ring fits the default AO; the 2.2 km wide-field detect
    ring does not. Saying 'threat ring too big' there would be a lie."""
    tk = _track("radar_acquisition", category="radar", lat=47.6415,
                lon=-122.1402)
    plan = identify_plan("D", tk, alt_agl_m=60.0, los_check=_los_clear)
    product = dry_run(plan, _fuel(), _tight_env(), start=(47.641468, -122.140165))
    detail = product["gate"]["standoff_vs_ao"]
    assert detail["limiting_factor"] == "sensor_standoff"
    assert detail["threat_ring_m"] == pytest.approx(300.0, abs=1.0)
    assert detail["threat_ring_m"] < detail["ao_reach_m"] < \
        detail["plan_max_standoff_m"]
    assert "min_detect_pixels" in detail["summary"]


def test_standoff_vs_ao_stays_silent_when_the_rings_fit():
    """No invented explanation: a plan rejected for some OTHER reason must not
    be handed a standoff story."""
    plan = identify_plan("D", _track("mortar", category="artillery",
                                     lat=47.6415, lon=-122.1402),
                         alt_agl_m=60.0, los_check=_los_clear)
    env = _env()        # the roomy test AO
    product = dry_run(plan, _fuel(), env, start=(HOME.latitude, HOME.longitude))
    assert product["gate"]["envelope_violations"] == []
    assert "standoff_vs_ao" not in product["gate"]
    # ...and a plan with no ring at all never gets one either
    far = grid_search_plan("D", [(35.0, 51.0), (35.0, 51.01),
                                 (35.01, 51.01), (35.01, 51.0)], 60.0)
    bad = dry_run(far, _fuel(), env, start=(HOME.latitude, HOME.longitude))
    assert bad["gate"]["envelope_violations"]
    assert "standoff_vs_ao" not in bad["gate"]


def test_standoff_vs_ao_invents_nothing_for_a_long_thin_ao():
    """The case the "stays silent" test above does NOT reach.

    `standoff_vs_ao` measures the AO's reach as the distance to its FURTHEST
    vertex, so a long thin AO reads as roomy while its narrow axis still throws
    the orbit out. Delete the `widest_m <= reach_m ...  return None` guard and
    this plan — whose 1650 m threat ring and 1648 m orbit both sit well inside
    a 2495 m reach — is handed `limiting_factor: sensor_standoff`, a summary
    reading "the plan's widest ring is 1648 m ... while the AO reaches only
    2495 m", and `shortfall_m: -846.4`. A confident, self-contradicting story
    on a plan that is rejected for a completely different reason is worse than
    the bare `wpN:geofence` list this function exists to replace.
    """
    lat, lon = 47.6415, -122.1402
    dlat, dlon = 100.0 / 111320.0, 2500.0 / (111320.0 * 0.675)
    thin = [(lat - dlat, lon - dlon), (lat - dlat, lon + dlon),
            (lat + dlat, lon + dlon), (lat + dlat, lon - dlon)]
    env = SafetyEnvelope(geofence=thin, home=(lat, lon, 122.0))
    plan = track_target_plan("D", _track("mbt", lat=lat, lon=lon),
                             alt_agl_m=60.0, los_check=_los_clear)
    ring = plan.meta["standoff"]["threat_ring_m"]
    reach = max(haversine_m(lat, lon, a, b) for a, b in thin)
    widest = max(haversine_m(lat, lon, w["lat"], w["lon"])
                 for w in plan.waypoints)
    assert ring <= reach and widest <= reach, "the rings DO fit this AO"
    product = dry_run(plan, _fuel(), env, start=(lat, lon))
    assert product["gate"]["envelope_violations"], "…and it is still rejected"
    assert "standoff_vs_ao" not in product["gate"]
    assert not [w for w in product["warnings"]
                if w.startswith("standoff_exceeds_ao")]


# =========================================================================
# The measured reproduction: the shipped default theater + the REAL LOS model
# =========================================================================

def _theater_los(t):
    """`missions.LosCheck` over the real `fake_airsim.line_of_sight` horizon
    model for a theater — no stub, no port bound."""
    from godseye_uav import launch

    home = launch.home_geopoint(t)
    sim = FakeAirSim(home=home)

    def check(f_lat, f_lon, f_alt, t_lat, t_lon, t_alt):
        los, obstacle = sim.line_of_sight(
            GeoPoint(f_lat, f_lon, home.altitude + f_alt),
            GeoPoint(t_lat, t_lon, t_alt))
        return {"los": bool(los), "first_obstacle": obstacle}

    return check, home


def test_identify_no_longer_refuses_a_sam_in_the_shipped_default_theater():
    """Measured before the fix: `mission_identify_target` refused reproducibly
    with 'the identification pass on track TRK-... has no line-of-sight point
    at the cross-cue altitude'. The detect ring at 60 m AGL saw the contact
    (horizon 32.0 km > 26.4 km ring); the ID ring at the 30 m floor did not
    (horizon 23.1 km). Same theater, same contact, real LOS model."""
    from godseye_uav import theaters

    t = theaters.get("default")
    los_check, home = _theater_los(t)
    lat, lon = t.demo_targets()[0]["lat"], t.demo_targets()[0]["lon"]
    tk = _track("sam_medium_range", category="sam", lat=lat, lon=lon,
                alt_m=home.altitude)      # contact on the ground, as detected

    plan = identify_plan("D", tk, alt_agl_m=60.0, los_check=los_check)
    cc = plan.meta["cross_cue"]
    assert cc["id_alt_pixel_optimal_m"] == pytest.approx(30.0)
    assert cc["id_alt_agl_m"] > 30.0
    assert plan.meta["los"]["points_clear"] == len(plan.waypoints) > 0
    # the mission still cannot be FLOWN here — and now says exactly why
    product = dry_run(plan, _fuel(), _tight_env(), start=(t.home_lat, t.home_lon))
    assert product["gate"]["ok"] is False
    assert product["gate"]["standoff_vs_ao"]["limiting_factor"] == "threat_ring"


def test_identify_passes_the_whole_gate_in_a_normal_theater():
    """...and against a contact whose doctrine fits a real AO, the identify
    pass plans, clears LOS and passes the M4 gate — end to end."""
    from godseye_uav import launch, theaters

    t = theaters.get("ukraine-donbas")
    los_check, home = _theater_los(t)
    lat, lon = t.center()
    tk = _track("c2_node", category="c2", lat=lat, lon=lon, alt_m=home.altitude)

    plan = identify_plan("D", tk, alt_agl_m=60.0, los_check=los_check)
    env = launch.build_envelope(t)
    product = dry_run(plan, FuelModel(home=env.home),
                      env, start=(t.home_lat, t.home_lon))
    assert product["gate"]["envelope_violations"] == []
    assert product["gate"]["ok"] is True, product["gate"]
    assert "standoff_vs_ao" not in product["gate"]
    assert [p["name"] for p in plan.phases] == ["detect", "identify"]
    assert plan.meta["id_achievable"] is True


def test_dry_run_prices_every_mission_kind():
    env, fm_start = _env(), (HOME.latitude, HOME.longitude)
    tk = _track("mbt", lat=47.6425, lon=-122.1402)
    plans = [
        grid_search_plan("D", SMALL_BOX, 40.0),
        recon_route_plan("D", ROUTE, 60.0),
        track_target_plan("D", tk, alt_agl_m=100.0, los_check=_los_clear),
        identify_plan("D", tk, alt_agl_m=100.0, los_check=_los_clear),
    ]
    for plan in plans:
        product = dry_run(plan, _fuel(), env, start=fm_start)
        assert product["est_fuel_pct"] >= 0.0, plan.kind
        assert product["est_time_s"] > 0.0, plan.kind
        assert "ok" in product["gate"], plan.kind


# =========================================================================
# Legacy dispatcher + ISR-only
# =========================================================================

def test_orbit_ring_radius_and_count():
    wps = orbit_waypoints(47.641, -122.140, 60.0, 100.0, points=12)
    assert len(wps) == 12
    m_lat = 111320.0
    m_lon = 111320.0 * math.cos(math.radians(47.641))
    for wp in wps:
        dn = (wp["lat"] - 47.641) * m_lat
        de = (wp["lon"] + 122.140) * m_lon
        assert math.hypot(dn, de) == pytest.approx(100.0, rel=0.02)


def test_plan_mission_kinds():
    assert plan_mission("grid_search", "D", polygon=AO, alt_m=60).kind == "grid_search"
    assert plan_mission("orbit_poi", "D", lat=47.64, lon=-122.14).meta["doctrine"] == "orbit_poi"
    assert plan_mission("assess", "D", lat=47.64, lon=-122.14).meta["doctrine"] == "assess"
    with pytest.raises(ValueError):
        plan_mission("kinetic_strike", "D")  # ISR-only (M14)


def test_plan_mission_recon_route_now_carries_captures():
    plan = plan_mission("recon_route", "D", waypoints=ROUTE, alt_m=60.0)
    assert plan.captures
    assert plan.meta["capture_every_m"] > 0.0


def test_plan_mission_identify_routes_to_the_cross_cue():
    plan = plan_mission("identify", "D", track=_track("mbt"), alt_m=200.0,
                        allow_unverified=True)
    assert [p["name"] for p in plan.phases] == ["detect", "identify"]


def test_no_kinetic_capability_in_the_module():
    """M14: ISR-only. No engagement primitive may be defined here.

    Checked against defined names rather than prose, so the module's own
    "no engagement capability" disclaimer does not trip the test.
    """
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "mcp" / "godseye_uav" / "missions.py"
    tree = ast.parse(src.read_text())
    names = {n.name.lower() for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    for name in names:
        for banned in ("weapon", "engage", "strike", "kinetic", "fire",
                       "prosecut", "kill", "attack", "target_list"):
            assert banned not in name, f"{name} looks kinetic (M14)"


# =========================================================================
# MCP tool wiring (the legacy uav_mission dispatcher keeps working)
# =========================================================================

@pytest.fixture
def server(tmp_path):
    port = next(_PORT)
    sim = FakeAirSim(port=port)
    sim.start()
    srv = None
    try:
        client = airsim.MultirotorClient(port=port)
        client.confirmConnection()
        backend = UavBackend(client, HOME)
        envelope = SafetyEnvelope(geofence=AO, home=(HOME.latitude, HOME.longitude,
                                                     HOME.altitude))
        with Store(tmp_path) as store:
            srv = GodseyeUavServer(backend, store, envelope=envelope, watchdog_s=10.0)
            yield srv
    finally:
        try:
            srv.tasking.shutdown()
        except Exception:
            pass
        sim.stop()


def _tools(srv):
    return srv.mcp._tool_manager._tools


def test_uav_mission_tool_registered(server):
    assert "uav_mission" in _tools(server)


def test_grid_search_mission_submits_route(server):
    async def main():
        h = await _tools(server)["uav_mission"].fn(
            vehicle="Drone1", kind="grid_search",
            params={"polygon": SMALL_BOX, "alt_m": 40})
        assert h.get("rejected") is not True, h
        assert h["kind"] == "grid_search"
        assert h["waypoint_count"] >= 4
        assert "sweep_spacing_m" in h
        assert h["state"] in ("queued", "executing")
    run(main())


def test_orbit_mission_gated_by_geofence(server):
    async def main():
        h = await _tools(server)["uav_mission"].fn(
            vehicle="Drone1", kind="orbit_poi",
            params={"lat": 35.0, "lon": 51.0, "radius_m": 80})
        assert h.get("rejected") is True
        assert h["gate"]["ok"] is False
    run(main())


def test_assess_mission_inside_ao_accepted(server):
    async def main():
        h = await _tools(server)["uav_mission"].fn(
            vehicle="Drone1", kind="assess",
            params={"lat": 47.641, "lon": -122.140, "radius_m": 50})
        assert h.get("rejected") is not True, h
        assert h["doctrine"] == "assess"
    run(main())


def test_legacy_grid_mission_reports_flown_spacing_through_the_tool(server):
    """The tool's sweep_spacing_m must now be the spacing actually flown."""
    async def main():
        h = await _tools(server)["uav_mission"].fn(
            vehicle="Drone1", kind="grid_search",
            params={"polygon": SMALL_BOX, "alt_m": 40, "max_lanes": 2})
        assert h.get("rejected") is not True, h
        plan = grid_search_plan("Drone1", SMALL_BOX, 40.0, max_lanes=2)
        assert h["sweep_spacing_m"] == plan.meta["lane_spacing_m"]
        assert h["sweep_spacing_m"] > plan.meta["lane_spacing_derived_m"]
    run(main())
