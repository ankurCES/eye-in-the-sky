"""Safety envelope tests: geofence, ceiling, BINGO fuel gate (PLAN §4.5)."""
import math

import pytest

from godseye_uav.safety import (
    AIRFRAMES,
    AIRFRAME_ENV_VAR,
    BREACH_DOCTRINE,
    DEFAULT_AIRFRAME_ID,
    ENVELOPE_VIOLATION_KINDS,
    GROUP3_FIXED_WING,
    MAX_TICK_DT_S,
    MISSION_INCOMPLETE_FUEL,
    QUAD_SUAS_ELECTRIC,
    UNDECLARED_DOCTRINE,
    Airframe,
    FuelModel,
    LinkState,
    LostLinkBehaviour,
    LostLinkMonitor,
    LostLinkPlan,
    Phase,
    RESERVE_PCT,
    SafetyEnvelope,
    SafetyMonitor,
    Violation,
    bearing_deg,
    default_airframe,
    distance_to_polygon_edge_m,
    doctrine_for,
    get_airframe,
    haversine_m,
    headwind_component_mps,
    point_in_polygon,
    track_deg_from_velocity,
)

# ~1km square around Bellevue downtown
SQUARE = [(47.610, -122.205), (47.610, -122.190), (47.620, -122.190), (47.620, -122.205)]


def test_haversine_known_distance():
    # 1 degree latitude ≈ 111.2 km
    d = haversine_m(47.0, -122.0, 48.0, -122.0)
    assert 110_000 < d < 112_500


def test_point_in_polygon_inside_outside():
    assert point_in_polygon(47.615, -122.1975, SQUARE) is True
    assert point_in_polygon(47.605, -122.1975, SQUARE) is False
    assert point_in_polygon(47.615, -122.210, SQUARE) is False


def test_point_in_polygon_empty_is_unconstrained():
    assert point_in_polygon(0.0, 0.0, []) is True


def test_distance_to_edge():
    # center of ~1km square → ~500m to nearest edge
    d = distance_to_polygon_edge_m(47.615, -122.1975, SQUARE)
    assert 400 < d < 650


def test_envelope_check_point():
    env = SafetyEnvelope(geofence=SQUARE, ceiling_m_agl=120.0, min_agl_m=3.0)
    assert env.check_point(47.615, -122.1975, 50.0) == []
    assert "geofence" in env.check_point(47.605, -122.1975, 50.0)
    assert any("ceiling" in v for v in env.check_point(47.615, -122.1975, 150.0))
    assert any("min_agl" in v for v in env.check_point(47.615, -122.1975, 1.0))


def test_envelope_check_route_reports_waypoint_index():
    env = SafetyEnvelope(geofence=SQUARE)
    route = [
        {"lat": 47.615, "lon": -122.1975, "alt_m": 50},
        {"lat": 47.605, "lon": -122.1975, "alt_m": 50},  # outside
    ]
    violations = env.check_route(route)
    assert len(violations) == 1
    assert violations[0].startswith("wp1:")


def test_check_route_reads_the_contract_altitude_spelling():
    """A route written to the DOCUMENTED contract must be gated.

    Regression: check_route read only ``alt_agl``/``alt_m`` and defaulted to 0,
    while every tool description tells a harness to send ``alt_agl_m``
    ("never a bare alt_m"). So the one spelling the contract mandates was the
    one spelling that bypassed the envelope: a 500 m waypoint passed a 120 m
    ceiling and returned "ok". Mission-planned routes happened to emit both
    spellings, which is why nothing caught it.
    """
    env = SafetyEnvelope(geofence=SQUARE, ceiling_m_agl=120.0, min_agl_m=3.0)
    inside = {"lat": 47.615, "lon": -122.1975}

    # Every accepted spelling must gate identically - no privileged alias.
    for key in ("alt_agl_m", "alt_agl", "alt_m"):
        over = env.check_route([{**inside, key: 500.0}])
        assert any("ceiling" in v for v in over), f"{key}=500 bypassed the ceiling"
        under = env.check_route([{**inside, key: 1.0}])
        assert any("min_agl" in v for v in under), f"{key}=1 bypassed the floor"
        assert env.check_route([{**inside, key: 60.0}]) == [], f"{key}=60 should pass"

    # A waypoint with no altitude is a violation, never a silent 0 that would
    # sail through both limits.
    missing = env.check_route([inside])
    assert len(missing) == 1 and "no_altitude" in missing[0]


def test_geofence_margin_sign():
    env = SafetyEnvelope(geofence=SQUARE)
    assert env.geofence_margin_m(47.615, -122.1975) > 0
    assert env.geofence_margin_m(47.605, -122.1975) < 0


def test_fuel_phase_classification():
    fm = FuelModel()
    assert fm.classify_phase(0, 0, True) == Phase.GROUND
    assert fm.classify_phase(0, -2.0, False) == Phase.CLIMB
    assert fm.classify_phase(0, 2.0, False) == Phase.DESCEND
    assert fm.classify_phase(0.2, 0, False) == Phase.HOVER
    assert fm.classify_phase(10, 0, False) == Phase.CRUISE


def test_fuel_tick_burns_and_clamps():
    fm = FuelModel()
    fm.tick(0, 0, True, now=1000.0)  # prime
    fm.tick(10, 0, False, now=1010.0)  # 10s cruise
    assert fm.fuel_pct < 100.0
    burn = 100.0 - fm.fuel_pct
    # The default airframe is a 35-min-endurance sUAS multirotor, so cruise
    # burns 100%/2100 s = 0.0476 %/s -> ~0.476% over 10 s. The band is tight
    # on purpose: this is the number that decides whether BINGO is reachable.
    assert burn == pytest.approx(10.0 * 100.0 / fm.capacity_s_cruise, rel=1e-9)
    assert 0.4 < burn < 0.6
    # never below zero
    fm.fuel_pct = 0.01
    fm.tick(10, 0, False, now=10000.0)
    assert fm.fuel_pct == 0.0


def test_fuel_wind_penalty():
    calm = FuelModel()
    windy = FuelModel()
    est_calm = calm.estimate_route([{"lat": 47.62, "lon": -122.19, "alt_m": 50}], (47.61, -122.20), 10.0, headwind_mps=0.0)
    est_wind = windy.estimate_route([{"lat": 47.62, "lon": -122.19, "alt_m": 50}], (47.61, -122.20), 10.0, headwind_mps=15.0)
    assert est_wind["fuel_pct"] > est_calm["fuel_pct"]


def test_preflight_gate_accepts_short_route():
    fm = FuelModel()
    fm.home = (47.615, -122.1975, 0.0)
    gate = fm.preflight_gate(
        [{"lat": 47.616, "lon": -122.198, "alt_m": 30}],
        (47.615, -122.1975),
        10.0,
    )
    assert gate["ok"] is True
    # reserve (20%) dominates a short hop; plan+return burn is only a few %
    assert gate["required_pct"] < 30.0
    assert gate["plan_fuel_pct"] < 5.0
    assert gate["est_time_s"] > 0


def test_preflight_gate_rejects_exhausting_route():
    fm = FuelModel()
    fm.home = (47.615, -122.1975, 0.0)
    fm.fuel_pct = 30.0
    # ~40 km out-and-back at 10 m/s → way over 30% - reserve
    gate = fm.preflight_gate(
        [{"lat": 47.9, "lon": -122.2, "alt_m": 100}],
        (47.615, -122.1975),
        10.0,
    )
    assert gate["ok"] is False
    assert gate["required_pct"] > gate["available_pct"]


def test_bingo_fuel_includes_reserve():
    fm = FuelModel()
    fm.home = (47.615, -122.1975, 0.0)
    bingo = fm.bingo_fuel_pct((47.617, -122.199), 50.0)
    assert bingo > RESERVE_PCT
    # farther away → higher bingo
    bingo_far = fm.bingo_fuel_pct((47.7, -122.3), 50.0)
    assert bingo_far > bingo


# --------------------------------------------------------------------------
# Fuel integrator robustness for a real tick loop (T5, gap R5)
# --------------------------------------------------------------------------

CENTER = (47.615, -122.1975)
HOME3 = (47.615, -122.1975, 0.0)


def test_fuel_home_is_a_real_field_not_a_monkey_patch():
    """T5/M4: FuelModel() must be usable without server.py patching .home."""
    fm = FuelModel(home=HOME3)
    assert fm.home == HOME3
    # and with no home the BINGO line degrades to the bare reserve
    assert FuelModel().bingo_fuel_pct(CENTER, 50.0) == RESERVE_PCT


def test_fuel_tick_first_call_only_primes_the_clock():
    """T5: the first tick has no dt, so nothing may burn."""
    fm = FuelModel()
    assert fm.tick(18.0, 0.0, False, now=500.0) == 100.0
    assert fm.fuel_pct == 100.0
    assert fm.ticks == 0


def test_fuel_tick_ignores_repeated_and_out_of_order_timestamps():
    """T5: a resent or late telemetry sample must not double-charge fuel."""
    fm = FuelModel()
    fm.tick(10.0, 0.0, False, now=1000.0)
    fm.tick(10.0, 0.0, False, now=1010.0)
    one_tick = 100.0 - fm.fuel_pct
    assert one_tick > 0.0
    fm.tick(10.0, 0.0, False, now=1010.0)  # repeated
    fm.tick(10.0, 0.0, False, now=1005.0)  # out of order
    assert 100.0 - fm.fuel_pct == one_tick
    # the clock never rewound: the next in-order tick charges exactly 10 s again
    fm.tick(10.0, 0.0, False, now=1020.0)
    assert abs((100.0 - fm.fuel_pct) - 2 * one_tick) < 1e-12


def test_fuel_tick_clamps_a_clock_jump():
    """T5: a suspended process must not drain the tank in one tick."""
    jumped = FuelModel()
    jumped.tick(10.0, 0.0, False, now=0.0)
    jumped.tick(10.0, 0.0, False, now=1_000_000.0)
    bounded = FuelModel()
    bounded.tick(10.0, 0.0, False, now=0.0)
    bounded.tick(10.0, 0.0, False, now=MAX_TICK_DT_S)
    assert abs(jumped.fuel_pct - bounded.fuel_pct) < 1e-12
    # a 1 000 000 s jump may cost at most MAX_TICK_DT_S of cruise, not the tank
    assert jumped.fuel_pct > 100.0 - MAX_TICK_DT_S * 100.0 / bounded.capacity_s_cruise - 1e-9
    assert jumped.fuel_pct > 98.0


def test_fuel_tick_tracks_burn_totals_for_the_journal():
    fm = FuelModel()
    fm.tick(0.0, 0.0, True, now=0.0)
    fm.tick(12.0, 0.0, False, now=20.0)
    rec = fm.fuel_record(vehicle_note="x")
    assert rec["phase"] == Phase.CRUISE.value
    assert rec["burned_pct"] > 0.0
    assert abs(fm.burned_pct - (100.0 - fm.fuel_pct)) < 1e-12
    assert rec["elapsed_s"] == 20.0 and rec["ticks"] == 1
    assert rec["vehicle_note"] == "x"


def test_fuel_state_roundtrips_for_restart_replay():
    fm = FuelModel(home=HOME3)
    fm.tick(0.0, 0.0, True, now=0.0)
    fm.tick(12.0, 0.0, False, now=25.0)
    fm.bingo.trip(fuel_pct=fm.fuel_pct, bingo_pct=21.0, at=CENTER, now=25.0)
    back = FuelModel.from_dict(fm.to_dict())
    assert abs(back.fuel_pct - fm.fuel_pct) < 1e-12
    assert back.home == HOME3
    assert back.bingo.tripped is True
    assert back.rates[Phase.CRUISE] == fm.rates[Phase.CRUISE]


# --------------------------------------------------------------------------
# Wind → fuel coupling (M15)
# --------------------------------------------------------------------------

def test_headwind_component_from_wind_vector():
    """M15: NED wind vector + ground track → headwind component."""
    # air mass moving south at 10 m/s; flying north is a 10 m/s headwind
    assert abs(headwind_component_mps(-10.0, 0.0, 0.0) - 10.0) < 1e-9
    # flying south with it = tailwind
    assert abs(headwind_component_mps(-10.0, 0.0, 180.0) + 10.0) < 1e-9
    # pure crosswind contributes nothing to the burn
    assert abs(headwind_component_mps(-10.0, 0.0, 90.0)) < 1e-9
    # no track (hover) → no headwind
    assert headwind_component_mps(-10.0, 0.0, None) == 0.0


def test_track_deg_from_velocity():
    assert abs(track_deg_from_velocity(10.0, 0.0) - 0.0) < 1e-9
    assert abs(track_deg_from_velocity(0.0, 10.0) - 90.0) < 1e-9
    assert track_deg_from_velocity(0.05, 0.0) is None  # hovering


def test_tick_wind_vector_increases_the_burn():
    """M15: the live integrator must burn more into a headwind."""
    calm, windy = FuelModel(), FuelModel()
    for now in (0.0, 20.0):
        calm.tick(15.0, 0.0, False, now=now, wind_ne=(0.0, 0.0), track_deg=0.0)
        windy.tick(15.0, 0.0, False, now=now, wind_ne=(-15.0, 0.0), track_deg=0.0)
    assert windy.fuel_pct < calm.fuel_pct
    assert windy.last_headwind_mps > 14.0


def test_estimate_route_resolves_headwind_per_leg():
    """M15: out-and-back — headwind on the way out, tailwind coming home."""
    fm = FuelModel()
    out_and_back = [
        {"lat": 47.625, "lon": -122.1975, "alt_m": 50},
        {"lat": 47.615, "lon": -122.1975, "alt_m": 50},
    ]
    est = fm.estimate_route(out_and_back, CENTER, 12.0, start_alt_m=50.0,
                            wind_ne=(-12.0, 0.0))
    assert est["legs"][0]["headwind_mps"] > 11.0
    assert est["legs"][1]["headwind_mps"] < -11.0
    # equal-length legs, but the upwind one costs more
    assert est["legs"][0]["fuel_pct"] > est["legs"][1]["fuel_pct"]


def test_bearing_deg_cardinals():
    assert abs(bearing_deg(47.61, -122.20, 47.62, -122.20)) < 0.5
    assert abs(bearing_deg(47.61, -122.20, 47.61, -122.19) - 90.0) < 0.5


# --------------------------------------------------------------------------
# T5: one integrator, two uses
# --------------------------------------------------------------------------

def test_preflight_and_inflight_share_one_integrator():
    """T5: "dry-run estimates use the SAME integrator" — prove it numerically."""
    plan = FuelModel(home=HOME3)
    est = plan.estimate_route([{"lat": 47.6160, "lon": -122.1975, "alt_m": 50.0}],
                              CENTER, 10.0, start_alt_m=50.0)
    live = FuelModel(home=HOME3)
    live.tick(10.0, 0.0, False, now=0.0)
    live.tick(10.0, 0.0, False, now=est["time_s"])
    assert abs((100.0 - live.fuel_pct) - est["fuel_pct"]) < 1e-9


def test_preflight_and_inflight_agree_under_wind():
    """T5 + M15: the wind penalty must be identical on both paths."""
    plan = FuelModel()
    est = plan.estimate_route([{"lat": 47.6160, "lon": -122.1975, "alt_m": 50.0}],
                              CENTER, 10.0, start_alt_m=50.0, wind_ne=(-15.0, 0.0))
    live = FuelModel()
    live.tick(10.0, 0.0, False, now=0.0, wind_ne=(-15.0, 0.0), track_deg=0.0)
    live.tick(10.0, 0.0, False, now=est["time_s"], wind_ne=(-15.0, 0.0), track_deg=0.0)
    assert abs((100.0 - live.fuel_pct) - est["fuel_pct"]) < 1e-9


def test_preflight_gate_warns_when_no_home_is_configured():
    """M4: a gate that silently dropped the return leg must say so."""
    fm = FuelModel()  # no home
    gate = fm.preflight_gate([{"lat": 47.616, "lon": -122.198, "alt_m": 30}],
                             CENTER, 10.0)
    assert gate["home_configured"] is False
    assert gate["return_fuel_pct"] == 0.0
    assert any("no_home_configured" in w for w in gate["warnings"])


def test_preflight_gate_refuses_new_plans_once_bingo_is_latched():
    """M4: a vehicle committed to RTB may not be re-tasked."""
    fm = FuelModel(home=HOME3)
    fm.bingo.trip(fuel_pct=20.0, bingo_pct=21.0, at=CENTER, now=1.0)
    gate = fm.preflight_gate([{"lat": 47.616, "lon": -122.198, "alt_m": 30}],
                             CENTER, 10.0)
    assert gate["ok"] is False
    assert gate["bingo_latched"] is True


# --------------------------------------------------------------------------
# BINGO force-RTB latch (M4 / T5)
# --------------------------------------------------------------------------

def test_bingo_latch_is_uncancellable_by_the_harness():
    """T5: reaching BINGO is an un-cancellable safety transition."""
    fm = FuelModel(home=HOME3)
    fm.fuel_pct = 20.02  # just under the near-home BINGO line
    st = fm.check_bingo((47.6155, -122.1975), 50.0)
    assert st["tripped_now"] is True and st["latched"] is True
    assert st["force_rtb"] is True
    assert st["mission_status"] == MISSION_INCOMPLETE_FUEL
    # a harness cancel cannot clear it, even after a (fictional) refuel
    assert fm.bingo.clear() is False
    fm.fuel_pct = 100.0
    again = fm.check_bingo((47.6155, -122.1975), 50.0)
    assert again["below_bingo"] is False
    assert again["latched"] is True and again["force_rtb"] is True
    assert again["tripped_now"] is False  # the edge fires exactly once
    assert fm.bingo.clear_attempts == 1
    # only an explicit operator override may reset it
    assert fm.bingo.clear(operator_override=True) is True
    assert fm.check_bingo((47.6155, -122.1975), 50.0)["latched"] is False


def test_simulated_flight_burns_fuel_and_trips_bingo():
    """R5/M4: fly a telemetry sequence — fuel must fall and BINGO must latch.

    Fails on pre-fix code: tick() had no production caller, fuel_pct sat at
    100% forever and no BINGO latch existed.
    """
    env = SafetyEnvelope(geofence=SQUARE, home=HOME3, ceiling_m_agl=120.0)
    mon = SafetyMonitor(envelope=env)
    mon.fuel.fuel_pct = 20.4  # airborne late in the sortie
    assert mon.fuel.home == HOME3  # picked up from the envelope

    fuels, verdicts = [], []
    for i in range(12):
        v = mon.tick(lat=47.6155, lon=-122.1975, alt_agl_m=60.0, speed_mps=14.0,
                     vz_mps=0.0, landed=False, track_deg=0.0,
                     wind_ne=(-5.0, 0.0), now=float(i) * 20.0)
        fuels.append(v["fuel_pct"])
        verdicts.append(v)

    assert fuels[-1] < fuels[0] < 100.0, "fuel must actually decrease in flight"
    assert all(b <= a for a, b in zip(fuels, fuels[1:])), "fuel must be monotonic"
    assert mon.fuel.burned_pct > 0.0

    forced = [v for v in verdicts if v["force_rtb"]]
    assert forced, "BINGO must force RTB before the tank runs dry"
    first = forced[0]
    assert "bingo" in first["rtb_reasons"]
    assert first["uncancellable"] is True
    assert first["mission_status"] == MISSION_INCOMPLETE_FUEL
    # the latch holds for every later tick, and the alarm edge fires once
    assert all(v["force_rtb"] for v in verdicts[verdicts.index(first):])
    raised = [a for v in verdicts for a in v["alarms"]
              if a["kind"] == "bingo" and a["state"] == "raised"]
    assert len(raised) == 1
    # the journal hook carries what store.log_fuel needs (PLAN §4.5 JSONL)
    rec = verdicts[-1]["fuel_record"]
    assert rec["fuel_pct"] == verdicts[-1]["fuel_pct"]
    assert rec["phase"] == Phase.CRUISE.value and rec["bingo_latched"] is True


# --------------------------------------------------------------------------
# In-flight envelope breach detection (M4 / PLAN §3.1)
# --------------------------------------------------------------------------

def test_check_state_flags_geofence_breach_and_proximity_separately():
    env = SafetyEnvelope(geofence=SQUARE, geofence_warn_m=100.0)
    assert env.check_state(47.615, -122.1975, 50.0, speed_mps=10.0) == []
    near = env.check_state(47.6105, -122.1975, 50.0, speed_mps=10.0)
    assert [v.kind for v in near] == ["geofence_proximity"]
    assert near[0].severity == "warning" and near[0].value > 0
    out = env.check_state(47.605, -122.1975, 50.0, speed_mps=10.0)
    assert [v.kind for v in out] == ["geofence"]
    assert out[0].severity == "breach" and out[0].value < 0


def test_check_state_enforces_max_speed_and_ceiling():
    env = SafetyEnvelope(geofence=SQUARE, ceiling_m_agl=120.0, max_speed_mps=20.0)
    fast = env.check_state(47.615, -122.1975, 50.0, speed_mps=35.0)
    assert [v.kind for v in fast] == ["max_speed"]
    high = env.check_state(47.615, -122.1975, 400.0, speed_mps=10.0)
    assert [v.kind for v in high] == ["ceiling"]
    assert env.check_speed(35.0) and env.check_speed(10.0) == []


def test_check_position_min_agl_covers_zero_in_flight_only():
    env = SafetyEnvelope(geofence=SQUARE, min_agl_m=3.0)
    flying = env.check_position(47.615, -122.1975, 0.0, landed=False)
    assert [v.kind for v in flying] == ["min_agl"]
    assert env.check_position(47.615, -122.1975, 0.0, landed=True) == []


def test_monitor_geofence_breach_forces_rtb_and_raises_one_alarm():
    """A drone blown out of the AO on wind must be caught in flight."""
    mon = SafetyMonitor(envelope=SafetyEnvelope(geofence=SQUARE, home=HOME3))
    inside = mon.tick(lat=47.615, lon=-122.1975, alt_agl_m=60.0, speed_mps=12.0,
                      vz_mps=0.0, landed=False, now=0.0)
    assert inside["force_rtb"] is False and inside["alarms"] == []
    drifted = mon.tick(lat=47.605, lon=-122.1975, alt_agl_m=60.0, speed_mps=12.0,
                       vz_mps=0.0, landed=False, now=5.0)
    assert "geofence" in drifted["breaches"]
    assert drifted["force_rtb"] is True and "geofence" in drifted["rtb_reasons"]
    assert any(a["kind"] == "geofence" and a["state"] == "raised" for a in drifted["alarms"])
    back = mon.tick(lat=47.615, lon=-122.1975, alt_agl_m=60.0, speed_mps=12.0,
                    vz_mps=0.0, landed=False, now=10.0)
    assert any(a["kind"] == "geofence" and a["state"] == "cleared" for a in back["alarms"])
    assert back["force_rtb"] is False


# --------------------------------------------------------------------------
# Lost link (M9)
# --------------------------------------------------------------------------

def test_lost_link_plan_parses_the_four_behaviours():
    for name in ("hold_orbit", "climb_for_los", "rtb", "continue"):
        assert LostLinkPlan.from_dict({"behaviour": name}).behaviour.value == name
    assert LostLinkPlan.from_dict(None).behaviour is LostLinkBehaviour.RTB
    try:
        LostLinkPlan.from_dict({"behaviour": "self_destruct"})
    except ValueError as exc:
        assert "unknown lost_link behaviour" in str(exc)
    else:
        raise AssertionError("unknown behaviour must be rejected")
    assert LostLinkPlan.from_dict({"behaviour": "rtb"}).to_dict()["behaviour"] == "rtb"


def test_lost_link_declares_after_dwell_then_restores():
    mon = LostLinkMonitor(plan=LostLinkPlan(behaviour=LostLinkBehaviour.HOLD_ORBIT,
                                            declare_after_s=5.0, restore_after_s=2.0))
    assert mon.observe(False, now=0.0) is None      # dwell starts
    assert mon.state is LinkState.PENDING
    assert mon.observe(False, now=3.0) is None      # not yet
    ev = mon.observe(False, now=6.0)
    assert ev is not None and ev["event"] == "loal_declared"
    assert mon.state is LinkState.LOAL and mon.lost is True
    assert mon.action is LostLinkBehaviour.HOLD_ORBIT
    assert mon.observe(True, now=7.0) is None       # restore dwell
    back = mon.observe(True, now=9.0)
    assert back is not None and back["event"] == "link_restored"
    assert back["duration_s"] == 7.0  # link lost at t=0, back at t=7
    assert mon.state is LinkState.UP and mon.action is None
    assert [e["event"] for e in mon.intrep_events()] == ["loal_declared", "link_restored"]


def test_lost_link_brief_dropout_never_declares_loal():
    mon = LostLinkMonitor(plan=LostLinkPlan(declare_after_s=5.0, restore_after_s=1.0))
    assert mon.observe(False, now=0.0) is None
    assert mon.observe(True, now=1.0) is None
    assert mon.observe(True, now=3.0) is None
    assert mon.state is LinkState.UP
    assert mon.intrep_events() == []
    assert mon.loal_count == 0


def test_lost_link_escalates_to_rtb_when_the_link_stays_dark():
    mon = LostLinkMonitor(plan=LostLinkPlan(behaviour=LostLinkBehaviour.CONTINUE,
                                            declare_after_s=5.0,
                                            escalate_to_rtb_after_s=60.0))
    mon.observe(False, now=0.0)
    mon.observe(False, now=6.0)
    assert mon.action is LostLinkBehaviour.CONTINUE
    assert mon.observe(False, now=30.0) is None
    ev = mon.observe(False, now=61.0)
    assert ev is not None and ev["event"] == "loal_escalated"
    assert mon.action is LostLinkBehaviour.RTB


def test_lost_link_bingo_overrides_the_planned_behaviour():
    """M4 beats M9: a fuel-committed vehicle comes home regardless of plan."""
    mon = LostLinkMonitor(plan=LostLinkPlan(behaviour=LostLinkBehaviour.CLIMB_FOR_LOS,
                                            declare_after_s=2.0))
    mon.observe(False, now=0.0, bingo_latched=True)
    ev = mon.observe(False, now=3.0, bingo_latched=True)
    assert ev["event"] == "loal_declared" and ev["bingo_override"] is True
    assert mon.action is LostLinkBehaviour.RTB


def test_monitor_lost_link_surfaces_alarm_and_rtb():
    mon = SafetyMonitor(envelope=SafetyEnvelope(geofence=SQUARE, home=HOME3),
                        link=LostLinkMonitor(plan=LostLinkPlan(
                            behaviour=LostLinkBehaviour.RTB, declare_after_s=2.0)))
    kw = dict(lat=47.615, lon=-122.1975, alt_agl_m=60.0, speed_mps=12.0,
              vz_mps=0.0, landed=False)
    mon.tick(link_up=False, now=0.0, **kw)
    v = mon.tick(link_up=False, now=3.0, **kw)
    assert v["link_event"]["event"] == "loal_declared"
    assert v["link"]["state"] == LinkState.LOAL.value
    assert v["force_rtb"] is True and "lost_link" in v["rtb_reasons"]
    assert any(a["kind"] == "lost_link" and a["state"] == "raised" for a in v["alarms"])
    assert v["uncancellable"] is False  # only BINGO is un-cancellable


# --------------------------------------------------------------------------
# Adversarial follow-ups: each of these fails on the *previous* "fixed" code,
# where a mutation survived the suite (T5 / M15 / M4).
# --------------------------------------------------------------------------

FAR_N = (47.700, -122.1975)  # ~9.5 km due north of HOME3


def test_bingo_line_prices_the_leg_home_not_the_leg_being_flown():
    """M15/M4: a tailwind outbound is a headwind home — the abort line must say so.

    SafetyMonitor used to resolve one scalar headwind from the *current track*
    and hand it to check_bingo, so flying downwind read as "no wind penalty"
    on the return leg and under-estimated BINGO in the one case that matters.
    """
    mon = SafetyMonitor(envelope=SafetyEnvelope(home=HOME3))
    v = mon.tick(lat=FAR_N[0], lon=FAR_N[1], alt_agl_m=500.0, speed_mps=15.0,
                 vz_mps=0.0, landed=False, track_deg=0.0,
                 wind_ne=(15.0, 0.0), now=0.0)
    assert v["headwind_mps"] < -14.0, "outbound leg is a tailwind"

    calm = FuelModel(home=HOME3)
    truth = FuelModel(home=HOME3)
    # the return leg is southbound into that same air mass = a real headwind
    assert truth.bingo_fuel_pct(FAR_N, 500.0, wind_ne=(15.0, 0.0)) > \
        calm.bingo_fuel_pct(FAR_N, 500.0)
    assert abs(v["bingo"]["bingo_fuel_pct"]
               - truth.bingo_fuel_pct(FAR_N, 500.0, wind_ne=(15.0, 0.0))) < 0.01


def test_estimate_route_reports_where_the_plan_ends():
    """T5: the integrator, not the caller, owns the plan's final altitude."""
    fm = FuelModel()
    est = fm.estimate_route([{"lat": 47.63, "lon": -122.1975, "alt_m": 400.0},
                             {"lat": 47.64, "lon": -122.1975}],  # inherits 400
                            CENTER, 10.0, start_alt_m=0.0)
    assert est["end_alt_m"] == 400.0


def test_preflight_return_leg_lets_down_from_where_the_plan_ends():
    """T5: a final waypoint with no alt_m must not erase the let-down burn.

    preflight_gate re-derived the return altitude from waypoints[-1] and
    silently fell back to start_alt_m, so this route was charged a return leg
    from 0 m while the identical route with an explicit alt_m paid the full
    400 m descent.
    """
    fm = FuelModel(home=HOME3)
    explicit = [{"lat": 47.63, "lon": -122.1975, "alt_m": 400.0},
                {"lat": 47.64, "lon": -122.1975, "alt_m": 400.0}]
    implicit = [{"lat": 47.63, "lon": -122.1975, "alt_m": 400.0},
                {"lat": 47.64, "lon": -122.1975}]  # same flight, alt inherited
    g_e = fm.preflight_gate(explicit, CENTER, 10.0)
    g_i = fm.preflight_gate(implicit, CENTER, 10.0)
    assert g_e["return_fuel_pct"] == g_i["return_fuel_pct"]
    # and the let-down is really in there: a 400 m plan costs more to come
    # home from than a ground-level one
    low = fm.preflight_gate([{"lat": 47.64, "lon": -122.1975, "alt_m": 0.0}],
                            CENTER, 10.0)
    assert g_e["return_fuel_pct"] > low["return_fuel_pct"]


def test_bingo_line_and_preflight_return_leg_use_the_same_rtb_speed():
    """T5 "one integrator": the abort line and the gate must not disagree.

    bingo_fuel_pct hard-coded 10 m/s while preflight_gate's return leg used the
    mission speed, so the two agreed only by coincidence at speed 10.
    """
    for rtb_speed in (10.0, 25.0):
        fm = FuelModel(home=HOME3, rtb_speed_mps=rtb_speed)
        wp = {"lat": 47.64, "lon": -122.1975, "alt_m": 300.0}
        for mission_speed in (5.0, 18.0):
            gate = fm.preflight_gate([wp], CENTER, mission_speed)
            line = fm.bingo_fuel_pct((wp["lat"], wp["lon"]), wp["alt_m"])
            assert abs(line - (gate["return_fuel_pct"] + RESERVE_PCT)) < 0.01, (
                f"rtb={rtb_speed} mission={mission_speed}: "
                f"bingo {line} vs gate {gate['return_fuel_pct'] + RESERVE_PCT}")


def test_tick_counts_the_burn_the_dt_clamp_dropped():
    """T5: clamping a long dt protects the tank but must not hide the loss."""
    fm = FuelModel()
    fm.tick(12.0, 0.0, False, now=0.0)
    fm.tick(12.0, 0.0, False, now=300.0)  # 300 s sample, 30 s charged
    assert fm.clamped_ticks == 1
    assert abs(fm.unaccounted_s - (300.0 - MAX_TICK_DT_S)) < 1e-9
    rec = fm.fuel_record()
    assert rec["clamped_ticks"] == 1 and rec["unaccounted_s"] > 0.0
    # a healthy loop reports nothing
    ok = FuelModel()
    ok.tick(12.0, 0.0, False, now=0.0)
    ok.tick(12.0, 0.0, False, now=1.0)
    assert ok.fuel_record()["clamped_ticks"] == 0
    assert ok.fuel_record()["unaccounted_s"] == 0.0


def test_repeated_sample_refreshes_the_reported_headwind():
    """T5: a no-burn tick must still report the wind it was handed."""
    fm = FuelModel()
    fm.tick(12.0, 0.0, False, now=0.0, wind_ne=(-15.0, 0.0), track_deg=0.0)
    fm.tick(12.0, 0.0, False, now=10.0, wind_ne=(-15.0, 0.0), track_deg=0.0)
    assert fm.last_headwind_mps > 14.0
    fm.tick(12.0, 0.0, False, now=10.0, wind_ne=(0.0, 0.0), track_deg=0.0)
    assert fm.fuel_record()["headwind_mps"] == 0.0, "stale wind in the journal"


def test_fuel_state_roundtrip_carries_the_new_bookkeeping():
    """T4c: rtb speed and the clamp counters must survive a restart replay."""
    fm = FuelModel(home=HOME3, rtb_speed_mps=17.0)
    fm.tick(12.0, 0.0, False, now=0.0)
    fm.tick(12.0, 0.0, False, now=400.0)
    back = FuelModel.from_dict(fm.to_dict())
    assert back.rtb_speed_mps == 17.0
    assert back.clamped_ticks == fm.clamped_ticks == 1
    assert abs(back.unaccounted_s - fm.unaccounted_s) < 1e-9


# --------------------------------------------------------------------------
# Airframe energy model: BINGO has to be REACHABLE or the whole §4.5 safety
# doctrine is unexercised. Every test below fails on the 14-hour burn model.
# --------------------------------------------------------------------------

#: An AO with no fence, so a sortie that leaves the 1 km SQUARE is not also a
#: geofence breach — these tests are about fuel, and only about fuel.
OPEN_AO = SafetyEnvelope(home=HOME3, ceiling_m_agl=150.0, max_speed_mps=20.0)


def test_default_airframe_endurance_is_credible_for_the_class():
    """M4: a normalized 0..100% tank only means something via the burn rate.

    Measured on the shipped stack before this: cruise burned 0.0021 %/s and
    `uav_get_telemetry` published est_endurance_s = 40320 — 11.2 HOURS for a
    vehicle the sim flies at 8-15 m/s under a 20 m/s cap.
    """
    fm = FuelModel()
    assert fm.airframe.id == DEFAULT_AIRFRAME_ID == "quad_suas_electric"
    minutes = fm.endurance_s(Phase.CRUISE) / 60.0
    assert 20.0 <= minutes <= 45.0, f"{minutes:.1f} min is not this airframe"
    # the phase ordering a multirotor actually has: climbing is the most
    # expensive, hovering costs more than cruising, the ground is nearly free
    assert fm.rates[Phase.CLIMB] > fm.rates[Phase.HOVER] > fm.rates[Phase.CRUISE]
    assert fm.rates[Phase.CRUISE] > fm.rates[Phase.DESCEND] > fm.rates[Phase.GROUND]
    assert fm.rates[Phase.GROUND] < 0.1 * fm.rates[Phase.CRUISE]
    # and the choice is documented where an operator will look for it
    assert "multirotor" in fm.airframe.summary.lower()
    assert fm.airframe.source and "endurance" in fm.airframe.source.lower()


def test_bingo_is_reachable_inside_one_sortie_not_nine_hours_away():
    """The defect in one number: minutes of flight from full tank to BINGO."""
    fm = FuelModel(home=HOME3)
    line = fm.bingo_fuel_pct((47.6375, -122.1975), 100.0)  # ~2.5 km out
    to_bingo_min = (100.0 - line) / fm.rates[Phase.CRUISE] / 60.0
    assert line > RESERVE_PCT  # the return leg is really priced in
    assert 10.0 < to_bingo_min < 45.0, (
        f"{to_bingo_min:.0f} min of cruise to reach BINGO — the pre-flight "
        "gate and the force-RTB are both untestable at that range")


def _sortie(monitor, *, minutes=60.0, dt=5.0, out_lat=47.6375, alt=100.0):
    """Fly a mission-length ISR profile and return every tick verdict.

    Climb out, transit ~2.5 km north, loiter on station. Stops the moment the
    monitor commits the vehicle, so the returned list ends on the trip.
    """
    verdicts = []
    t = 0.0
    while t <= minutes * 60.0:
        if t < 35.0:          # climb to altitude at 3 m/s
            lat, agl, vz, speed = HOME3[0], min(alt, 3.0 * t), -3.0, 2.0
        elif t < 200.0:       # transit outbound at 15 m/s
            frac = (t - 35.0) / 165.0
            lat, agl, vz, speed = (HOME3[0] + (out_lat - HOME3[0]) * frac,
                                   alt, 0.0, 15.0)
        else:                 # on station: a 12 m/s orbit
            lat, agl, vz, speed = out_lat, alt, 0.0, 12.0
        v = monitor.tick(lat=lat, lon=HOME3[1], alt_agl_m=agl, speed_mps=speed,
                         vz_mps=vz, landed=False, track_deg=0.0, now=t)
        verdicts.append(v)
        if v["force_rtb"]:
            break
        t += dt
    return verdicts


def test_a_mission_length_sortie_actually_trips_and_latches_bingo():
    """PLAN §4.5's most important behaviour, exercised by a FLIGHT.

    Every previous BINGO test set `fuel_pct` by hand. With a 14-hour burn
    model nothing else could: this profile (climb, 2.5 km transit, loiter)
    burns 5% of the tank in an hour there, so it never trips, and the
    un-cancellable force-RTB shipped untested against a real fuel clock.
    """
    mon = SafetyMonitor(envelope=OPEN_AO)
    assert mon.fuel.fuel_pct == 100.0  # a full tank, not a rigged one

    flight = _sortie(mon)
    trip = flight[-1]
    assert trip["force_rtb"] is True, (
        f"no BINGO in {flight[-1]['t'] / 60.0:.0f} min of flight "
        f"(fuel {flight[-1]['fuel_pct']}%)")
    minutes = trip["t"] / 60.0
    assert 10.0 < minutes < 50.0, f"BINGO at {minutes:.1f} min"

    # it is BINGO that committed it, it latched, and the mission is flagged
    assert trip["rtb_reasons"] == ["bingo"]
    assert trip["bingo"]["tripped_now"] is True
    assert trip["uncancellable"] is True
    assert trip["mission_status"] == MISSION_INCOMPLETE_FUEL
    assert mon.fuel.bingo.tripped_at == trip["t"]

    # it tripped with fuel to GET home, which is the point of the line
    assert trip["fuel_pct"] > RESERVE_PCT
    assert trip["bingo"]["margin_pct"] <= 0.0
    assert all(v["fuel_pct"] > 0.0 for v in flight), "ran the tank dry first"
    assert flight[0]["fuel_pct"] > flight[-1]["fuel_pct"]

    # the alarm edge fires exactly once, and the latch survives the RTB
    raised = [a for v in flight for a in v["alarms"]
              if a["kind"] == "bingo" and a["state"] == "raised"]
    assert len(raised) == 1
    home_leg = [mon.tick(lat=HOME3[0], lon=HOME3[1], alt_agl_m=100.0,
                         speed_mps=10.0, vz_mps=0.0, landed=False,
                         track_deg=180.0, now=trip["t"] + 10.0 * i)
                for i in range(1, 13)]
    assert all(v["force_rtb"] and v["uncancellable"] for v in home_leg)
    assert all(v["bingo"]["tripped_now"] is False for v in home_leg)
    assert mon.fuel.bingo.clear() is False  # still un-cancellable at home


def test_the_slower_airframe_does_not_trip_on_the_same_sortie():
    """The profile is not rigged: the SAME flight on a 14 h ISR platform is
    nowhere near BINGO. The trip above is the airframe, not the arithmetic."""
    mon = SafetyMonitor(envelope=OPEN_AO,
                        fuel=FuelModel(airframe=GROUP3_FIXED_WING, home=HOME3))
    flight = _sortie(mon)
    assert not any(v["force_rtb"] for v in flight)
    assert flight[-1]["fuel_pct"] > 90.0
    assert flight[-1]["t"] >= 3600.0  # it really flew the whole hour


def test_airframe_profiles_are_named_tunable_and_loud_about_a_bad_one():
    """"Named and tunable, not magic numbers" — and no silent fallback."""
    assert set(AIRFRAMES) >= {"quad_suas_electric", "group3_fixed_wing"}
    assert get_airframe("group3_fixed_wing") is GROUP3_FIXED_WING
    assert get_airframe(QUAD_SUAS_ELECTRIC) is QUAD_SUAS_ELECTRIC
    assert get_airframe(None) is default_airframe()

    with pytest.raises(ValueError, match="unknown airframe"):
        get_airframe("mystery_jet")
    with pytest.raises(ValueError, match="quad_suas_electric"):
        FuelModel(airframe="mystery_jet")  # lists what it does know

    # a caller can calibrate one from measured numbers without touching safety.py
    tuned = Airframe(id="bench", summary="bench-calibrated", source="flight test",
                     endurance_cruise_s=18.0 * 60.0,
                     phase_multipliers={Phase.GROUND: 0.04, Phase.CLIMB: 1.7,
                                        Phase.CRUISE: 1.0, Phase.DESCEND: 0.7,
                                        Phase.HOVER: 1.25},
                     wind_penalty_per_mps=0.05, wind_ref_mps=12.0)
    fm = FuelModel(airframe=tuned)
    assert fm.capacity_s_cruise == 1080.0
    assert fm.rates[Phase.CRUISE] == pytest.approx(100.0 / 1080.0)
    assert fm.endurance_s() / 60.0 == pytest.approx(18.0)
    # its wind penalty is the airframe's, not a module constant
    calm = fm._burn(Phase.CRUISE, 1.0, 0.0)
    assert fm._burn(Phase.CRUISE, 1.0, 8.0) / calm == pytest.approx(1.0 + 0.05 * 8.0)
    # ...clamped by the profile's own ceiling, and by its own reference wind
    assert fm._burn(Phase.CRUISE, 1.0, 40.0) / calm == pytest.approx(
        1.0 + fm.airframe.wind_penalty_max)
    assert fm._burn(Phase.CRUISE, 1.0, 12.0) == fm._burn(Phase.CRUISE, 1.0, 99.0)

    # a profile that cannot be integrated is refused at construction
    with pytest.raises(ValueError, match="endurance_cruise_s"):
        Airframe(id="x", summary="", source="", endurance_cruise_s=0.0,
                 phase_multipliers=dict(QUAD_SUAS_ELECTRIC.phase_multipliers))
    with pytest.raises(ValueError, match="missing"):
        Airframe(id="x", summary="", source="", endurance_cruise_s=600.0,
                 phase_multipliers={Phase.CRUISE: 1.0})
    with pytest.raises(ValueError, match="CRUISE must be exactly 1.0"):
        Airframe(id="x", summary="", source="", endurance_cruise_s=600.0,
                 phase_multipliers=dict(QUAD_SUAS_ELECTRIC.phase_multipliers,
                                        **{Phase.CRUISE: 2.0}))
    with pytest.raises(ValueError, match="missing phases"):
        FuelModel(rates={Phase.CRUISE: 0.1})


def test_the_airframe_is_selectable_by_environment_and_never_guessed(monkeypatch):
    """server.py builds `FuelModel()` itself, so an operator needs SOME way in."""
    monkeypatch.setenv(AIRFRAME_ENV_VAR, "group3_fixed_wing")
    assert FuelModel().airframe is GROUP3_FIXED_WING
    monkeypatch.setenv(AIRFRAME_ENV_VAR, "not_an_airframe")
    with pytest.raises(ValueError, match="unknown airframe"):
        FuelModel()
    monkeypatch.delenv(AIRFRAME_ENV_VAR)
    assert FuelModel().airframe is QUAD_SUAS_ELECTRIC


def test_the_airframe_travels_with_the_persisted_fuel_state():
    """T4c: a journal replayed under another energy model re-prices every burn."""
    fm = FuelModel(airframe="group3_fixed_wing", home=HOME3)
    fm.tick(12.0, 0.0, False, now=0.0)
    fm.tick(12.0, 0.0, False, now=25.0)
    assert fm.fuel_record()["airframe"] == "group3_fixed_wing"
    back = FuelModel.from_dict(fm.to_dict())
    assert back.airframe is GROUP3_FIXED_WING
    assert back.capacity_s_cruise == fm.capacity_s_cruise
    assert back.rates[Phase.CRUISE] == fm.rates[Phase.CRUISE]
    assert back._burn(Phase.CRUISE, 10.0, 8.0) == fm._burn(Phase.CRUISE, 10.0, 8.0)
    # the profile itself is published, so the numbers are auditable
    prof = fm.to_dict()["airframe_profile"]
    assert prof["endurance_cruise_s"] == 14.0 * 3600.0
    assert prof["phase_multipliers"]["cruise"] == 1.0


def test_one_integrator_still_holds_on_a_non_default_airframe():
    """T5 is a property of the model, not of the numbers that were tuned."""
    for airframe in ("quad_suas_electric", "group3_fixed_wing"):
        plan = FuelModel(airframe=airframe, home=HOME3)
        est = plan.estimate_route([{"lat": 47.6160, "lon": -122.1975, "alt_m": 50.0}],
                                  CENTER, 10.0, start_alt_m=50.0)
        live = FuelModel(airframe=airframe, home=HOME3)
        live.tick(10.0, 0.0, False, now=0.0)
        live.tick(10.0, 0.0, False, now=est["time_s"])
        assert abs((100.0 - live.fuel_pct) - est["fuel_pct"]) < 1e-9, airframe
    # and the dry run lets down at the AIRFRAME's descent rate, not a constant
    quad = FuelModel(airframe="quad_suas_electric")
    wing = FuelModel(airframe="group3_fixed_wing")
    down = [{"lat": CENTER[0], "lon": CENTER[1], "alt_m": 0.0}]
    assert (quad.estimate_route(down, CENTER, 10.0, start_alt_m=300.0)["time_s"]
            != wing.estimate_route(down, CENTER, 10.0, start_alt_m=300.0)["time_s"])


# --------------------------------------------------------------------------
# Breach doctrine (§4.5): which finding COMMITS the vehicle, stated out loud.
# --------------------------------------------------------------------------

def test_the_breach_doctrine_is_published_with_the_envelope():
    """A skill whose ROE 'may only be stricter' has to be able to READ it."""
    doctrine = SafetyEnvelope(geofence=SQUARE, home=HOME3).to_dict()["breach_doctrine"]
    assert doctrine == BREACH_DOCTRINE
    assert doctrine["geofence"] == "force_rtb"
    assert doctrine["bingo"] == "force_rtb_uncancellable"
    assert doctrine["ceiling"] == doctrine["max_speed"] == "alarm_only"
    assert doctrine["min_agl"] == "alarm_only"
    assert doctrine["geofence_proximity"] == "warning"


def test_only_the_geofence_breach_commits_the_vehicle_and_the_field_says_so():
    """The old field was called `breach_forces_rtb` but gated on ONE kind.

    The asymmetry is correct — an RTB is the remedy for being outside the AO,
    and is not the remedy for being too high or too fast — but it has to be
    intentional. Flipping the doctrine switch must change exactly that case.
    """
    env = SafetyEnvelope(geofence=SQUARE, home=HOME3, ceiling_m_agl=120.0,
                         max_speed_mps=20.0)
    mon = SafetyMonitor(envelope=env)
    hot = mon.tick(lat=47.615, lon=-122.1975, alt_agl_m=400.0, speed_mps=35.0,
                   vz_mps=0.0, landed=False, now=0.0)
    assert set(hot["breaches"]) == {"ceiling", "max_speed"}
    assert hot["force_rtb"] is False and hot["rtb_reasons"] == []
    assert [a["kind"] for a in hot["alarms"] if a["state"] == "raised"] != []
    assert hot["breach_doctrine"]["ceiling"] == "alarm_only"

    out = mon.tick(lat=47.605, lon=-122.1975, alt_agl_m=60.0, speed_mps=10.0,
                   vz_mps=0.0, landed=False, now=5.0)
    assert out["rtb_reasons"] == ["geofence"]

    off = SafetyMonitor(envelope=env, geofence_breach_forces_rtb=False)
    still_out = off.tick(lat=47.605, lon=-122.1975, alt_agl_m=60.0, speed_mps=10.0,
                         vz_mps=0.0, landed=False, now=0.0)
    assert "geofence" in still_out["breaches"]  # still SEEN and alarmed
    assert still_out["force_rtb"] is False      # doctrine is what commits it
    assert any(a["kind"] == "geofence" for a in still_out["alarms"])


def test_an_unrecovered_ceiling_breach_is_surfaced_as_sustained_state():
    """Alarm-only must not mean 'one edge and then silence'.

    A ceiling excursion the vehicle never corrects is a real safety condition;
    it is published with how long it has stood so the operator (M14: command
    authority is theirs) can act, rather than being escalated into an RTB the
    enforcement layer would not fly.
    """
    mon = SafetyMonitor(envelope=SafetyEnvelope(geofence=SQUARE, home=HOME3,
                                                ceiling_m_agl=120.0),
                        sustained_breach_s=30.0)
    kw = dict(lat=47.615, lon=-122.1975, speed_mps=10.0, vz_mps=0.0, landed=False)
    first = mon.tick(alt_agl_m=400.0, now=0.0, **kw)
    assert first["breaches"] == ["ceiling"] and first["sustained_breaches"] == []
    assert mon.tick(alt_agl_m=400.0, now=20.0, **kw)["sustained_breaches"] == []

    stuck = mon.tick(alt_agl_m=400.0, now=45.0, **kw)["sustained_breaches"]
    assert [s["kind"] for s in stuck] == ["ceiling"]
    assert stuck[0]["for_s"] == 45.0 and stuck[0]["since"] == 0.0
    assert stuck[0]["value"] == 400.0 and stuck[0]["limit"] == 120.0
    assert stuck[0]["doctrine"] == "alarm_only"
    # ...and it is still not an RTB
    assert mon.tick(alt_agl_m=400.0, now=60.0, **kw)["force_rtb"] is False

    # recovering clears the clock: a NEW excursion starts counting from scratch
    assert mon.tick(alt_agl_m=60.0, now=70.0, **kw)["sustained_breaches"] == []
    again = mon.tick(alt_agl_m=400.0, now=75.0, **kw)
    assert again["sustained_breaches"] == []
    assert mon.tick(alt_agl_m=400.0, now=100.0, **kw)["sustained_breaches"] == []
    assert mon.tick(alt_agl_m=400.0, now=110.0, **kw)["sustained_breaches"][0]["for_s"] == 35.0


# --------------------------------------------------------------------------
# The two silent fallbacks the airframe/doctrine work left behind. Both were
# reproduced at runtime on the shipped code before these tests existed.
# --------------------------------------------------------------------------

def _wind_kwargs(**over):
    """A valid `Airframe` kwargs set, so each test varies ONE wind field."""
    return dict(
        id="probe", summary="wind-field probe", source="test",
        endurance_cruise_s=2100.0,
        phase_multipliers={Phase.GROUND: 0.05, Phase.CLIMB: 1.5,
                           Phase.CRUISE: 1.0, Phase.DESCEND: 0.8,
                           Phase.HOVER: 1.17},
        **over)


def test_a_profile_that_would_disable_the_m15_headwind_penalty_is_refused():
    """M15/M4: the wind fields are part of the same fuel clock as the rest.

    `__post_init__` refused a bad endurance, a missing phase multiplier and a
    non-positive vertical rate — and then accepted three wind values that make
    the penalty dead code. Measured on the shipped profile validator:
    `wind_ref_mps=0.0` constructed fine and a 25 m/s headwind then burned
    EXACTLY the calm rate, which is the bare-except-geoid failure shape: a
    safety input that is present, wired, and has no effect.
    """
    # a headwind that costs nothing, because the reference wind is zero
    with pytest.raises(ValueError, match="wind_ref_mps"):
        Airframe(**_wind_kwargs(wind_ref_mps=0.0))
    with pytest.raises(ValueError, match="wind_ref_mps"):
        Airframe(**_wind_kwargs(wind_ref_mps=-5.0))
    # ...or because the ceiling clamps every penalty back to zero
    with pytest.raises(ValueError, match="wind_penalty_max"):
        Airframe(**_wind_kwargs(wind_penalty_max=0.0))
    # the error names the profile, so an operator knows WHICH one is wrong
    with pytest.raises(ValueError, match="probe"):
        Airframe(**_wind_kwargs(wind_ref_mps=0.0))

    # and the same refusal reaches a caller who builds it through FuelModel
    with pytest.raises(ValueError, match="wind_penalty_max"):
        FuelModel(airframe=Airframe(**_wind_kwargs(wind_penalty_max=-1.0)))


def test_a_negative_wind_penalty_cannot_make_a_headwind_cheaper_than_calm_air():
    """The BINGO line prices the leg HOME, into whatever wind is out there.

    Measured before the guard: `wind_penalty_per_mps=-0.05` built without
    complaint and a 10 m/s headwind then burned 0.0238 %/s against 0.0476 %/s
    in calm air — half price for flying into the wind. The abort line would be
    under-estimated in exactly the downwind case the module says it exists for.
    """
    with pytest.raises(ValueError, match="wind_penalty_per_mps"):
        Airframe(**_wind_kwargs(wind_penalty_per_mps=-0.05))

    # the property the guard protects, on every shipped profile
    for airframe in AIRFRAMES.values():
        fm = FuelModel(airframe=airframe)
        calm = fm._burn(Phase.CRUISE, 1.0, 0.0)
        assert fm._burn(Phase.CRUISE, 1.0, 10.0) > calm, airframe.id
        assert fm._burn(Phase.CRUISE, 1.0, airframe.wind_ref_mps) > calm, airframe.id
    # zero penalty per m/s is still a legitimate (if unusual) calibration
    assert Airframe(**_wind_kwargs(wind_penalty_per_mps=0.0)).wind_penalty_per_mps == 0.0


def test_an_unclassified_finding_is_never_published_as_alarm_only():
    """`BREACH_DOCTRINE.get(kind, "alarm_only")` answered a safety question.

    `alarm_only` is not a neutral default — the published table defines it as
    "seen, and deliberately does not commit the vehicle". Measured on the
    shipped monitor: a `no_fly_zone` breach came back under
    `sustained_breaches` with `doctrine: alarm_only`, i.e. the system stated a
    decision nobody had made. It must read as undeclared instead, and be named.
    """
    class RestrictedAirspace(SafetyEnvelope):
        def check_state(self, lat, lon, alt_agl_m, *, speed_mps=None,
                        landed=False):
            return [Violation("no_fly_zone", "breach", "restricted airspace",
                              1.0, 0.0)]

    assert "no_fly_zone" not in BREACH_DOCTRINE
    assert doctrine_for("no_fly_zone") == UNDECLARED_DOCTRINE != "alarm_only"
    assert doctrine_for("geofence") == "force_rtb"

    mon = SafetyMonitor(envelope=RestrictedAirspace(home=HOME3),
                        sustained_breach_s=30.0)
    mon.tick(lat=HOME3[0], lon=HOME3[1], alt_agl_m=50.0, speed_mps=5.0,
             vz_mps=0.0, landed=False, now=0.0)
    v = mon.tick(lat=HOME3[0], lon=HOME3[1], alt_agl_m=50.0, speed_mps=5.0,
                 vz_mps=0.0, landed=False, now=40.0)

    assert v["breaches"] == ["no_fly_zone"]
    assert [s["doctrine"] for s in v["sustained_breaches"]] == [UNDECLARED_DOCTRINE]
    # ...and it is NAMED, so the gap is visible rather than inferred
    assert v["undeclared_doctrine"] == ["no_fly_zone"]
    # it still does not manufacture an RTB the enforcement layer would not fly
    assert v["force_rtb"] is False and v["rtb_reasons"] == []


def test_every_kind_this_envelope_emits_has_a_declared_doctrine():
    """The guard that keeps `undeclared` unreachable for our own findings.

    ENVELOPE_VIOLATION_KINDS is the set `SafetyEnvelope` can produce; each one
    must have a row, so adding a limit without deciding what it commits the
    vehicle to fails at import instead of at 400 ft.
    """
    assert ENVELOPE_VIOLATION_KINDS <= set(BREACH_DOCTRINE)
    for kind in ENVELOPE_VIOLATION_KINDS:
        assert doctrine_for(kind) != UNDECLARED_DOCTRINE

    # the set is the real one: drive the envelope over every limit it has and
    # confirm nothing comes back that the table has not classified.
    env = SafetyEnvelope(geofence=SQUARE, home=HOME3, ceiling_m_agl=120.0,
                         min_agl_m=10.0, max_speed_mps=20.0,
                         geofence_warn_m=100.0)
    seen = set()
    for lat, lon, agl, spd in [
            (47.6150, -122.1975, 400.0, 5.0),    # ceiling
            (47.6150, -122.1975, 2.0, 5.0),      # min_agl
            (47.6150, -122.1975, 60.0, 35.0),    # max_speed
            (47.5000, -122.1975, 60.0, 5.0),     # geofence breach
            (47.6105, -122.1975, 60.0, 5.0)]:    # geofence proximity
        seen |= {v.kind for v in env.check_state(lat, lon, agl,
                                                 speed_mps=spd, landed=False)}
    assert seen == ENVELOPE_VIOLATION_KINDS, seen ^ ENVELOPE_VIOLATION_KINDS


def test_a_shipped_profile_cannot_be_re_priced_behind_the_validator():
    """`frozen=True` froze the fields, not the dict one of them held.

    `AIRFRAMES` entries are module-level singletons that every later
    `FuelModel()` reads. Measured before this: mutating
    `FuelModel().airframe.phase_multipliers` changed
    `QUAD_SUAS_ELECTRIC.phase_multipliers` for the whole process — setting a
    GROUND multiplier to 99.0 (or CRUISE to anything but 1.0, which
    `__post_init__` refuses at construction) with no error anywhere.
    """
    fm = FuelModel()
    assert fm.airframe is QUAD_SUAS_ELECTRIC
    before = dict(QUAD_SUAS_ELECTRIC.phase_multipliers)

    with pytest.raises(TypeError):
        fm.airframe.phase_multipliers[Phase.GROUND] = 99.0
    with pytest.raises(TypeError):        # the one __post_init__ refuses
        QUAD_SUAS_ELECTRIC.phase_multipliers[Phase.CRUISE] = 2.0
    assert dict(QUAD_SUAS_ELECTRIC.phase_multipliers) == before
    assert FuelModel().rates == fm.rates

    # a caller's own dict is copied in, so mutating it after the fact cannot
    # reach the profile either
    mine = {Phase.GROUND: 0.05, Phase.CLIMB: 1.5, Phase.CRUISE: 1.0,
            Phase.DESCEND: 0.8, Phase.HOVER: 1.17}
    af = Airframe(id="mine", summary="s", source="t", endurance_cruise_s=1200.0,
                  phase_multipliers=mine)
    mine[Phase.CRUISE] = 7.0
    assert af.phase_multipliers[Phase.CRUISE] == 1.0
    # ...and it still reads like a mapping everywhere it is used
    assert dict(af.phase_multipliers)[Phase.HOVER] == 1.17
    assert af.to_dict()["phase_multipliers"]["cruise"] == 1.0
    assert Phase.DESCEND in af.phase_multipliers


def test_a_frozen_airframe_is_actually_hashable_and_usable_as_a_key():
    """`frozen=True` promised hashability and the mapping field revoked it.

    MEASURED on the code before this: `hash(QUAD_SUAS_ELECTRIC)` raised
    `TypeError: unhashable type: 'dict'`. The dataclass-generated `__hash__`
    hashes the tuple of FIELDS, and one field is a mapping — so every profile
    the module ships was unhashable, and the second half of what `frozen=True`
    is for (a value that can key a cache or join a set) never existed. Closing
    the mutation hole with `MappingProxyType` did not fix this: a mapping
    proxy is no more hashable than the dict it wraps.
    """
    assert isinstance(hash(QUAD_SUAS_ELECTRIC), int)
    # every shipped profile, not just the default
    assert len({af for af in AIRFRAMES.values()}) == len(set(AIRFRAMES))

    # equal profiles must hash equal, or a dict keyed on one would miss
    twin = Airframe(id=QUAD_SUAS_ELECTRIC.id,
                    summary=QUAD_SUAS_ELECTRIC.summary,
                    source=QUAD_SUAS_ELECTRIC.source,
                    endurance_cruise_s=QUAD_SUAS_ELECTRIC.endurance_cruise_s,
                    phase_multipliers=dict(QUAD_SUAS_ELECTRIC.phase_multipliers))
    assert twin == QUAD_SUAS_ELECTRIC
    assert hash(twin) == hash(QUAD_SUAS_ELECTRIC)
    assert {QUAD_SUAS_ELECTRIC: "burn-table"}[twin] == "burn-table"

    # ...and a profile that differs only inside the mapping is a DIFFERENT key,
    # which is the whole reason the mapping has to be part of the hash
    other = Airframe(id=QUAD_SUAS_ELECTRIC.id,
                     summary=QUAD_SUAS_ELECTRIC.summary,
                     source=QUAD_SUAS_ELECTRIC.source,
                     endurance_cruise_s=QUAD_SUAS_ELECTRIC.endurance_cruise_s,
                     phase_multipliers={**QUAD_SUAS_ELECTRIC.phase_multipliers,
                                        Phase.HOVER: 1.40})
    assert other != QUAD_SUAS_ELECTRIC
    assert len({QUAD_SUAS_ELECTRIC, twin, other}) == 2


def _profile(**over):
    """A valid, integrable profile, overridden one field at a time."""
    kw = dict(id="probe", summary="fuel-clock probe", source="test",
              endurance_cruise_s=2100.0,
              phase_multipliers={Phase.GROUND: 0.05, Phase.CLIMB: 1.5,
                                 Phase.CRUISE: 1.0, Phase.DESCEND: 0.8,
                                 Phase.HOVER: 1.17})
    kw.update(over)
    return kw


def test_a_profile_that_stops_the_fuel_clock_is_refused_not_flown():
    """M4/T5: the validator's sign tests had an OFF SWITCH on the wrong side.

    `__post_init__` refused a zero endurance, a negative multiplier and three
    dead wind fields — and then accepted the infinities and NaNs that disable
    the same clock without failing any of them. MEASURED on the code before
    this guard:

      * `endurance_cruise_s=float("inf")` satisfied `> 0`, so
        `cruise_rate_pct_per_s` was `100.0/inf == 0.0` and EVERY phase burned
        `0.0 %/s`. A `FuelModel` on that profile flew 3.3 HOURS of cruise into
        a 15 m/s headwind and reported `fuel_pct=100.0`, `burned_pct=0.0`,
        `bingo.tripped=False` over 400 ticks. BINGO — the one gate that turns
        a sortie around — was unreachable, silently.
      * `nan` passes BOTH `> 0.0` and `< 0.0` as False, so it slipped past
        every sign test in the class: a NaN phase multiplier, and a NaN
        `wind_penalty_per_mps` that then collapses through
        `min(wind_penalty_max, nan)` to a CONSTANT maximum penalty which no
        longer varies with the headwind at all.
      * a `0.0` multiplier on a FLIGHT phase is the same switch scoped to one
        phase: a loiter with infinite endurance.

    The assertions below are on the OUTCOME the guard buys — a fuel clock that
    actually moves — not merely on the raise.
    """
    # the exact value that flew for 3.3 h on 0.00% of fuel
    with pytest.raises(ValueError, match="endurance_cruise_s"):
        Airframe(**_profile(endurance_cruise_s=float("inf")))
    with pytest.raises(ValueError, match="endurance_cruise_s"):
        Airframe(**_profile(endurance_cruise_s=float("nan")))
    # a NaN multiplier on any phase, and a flight phase that burns nothing
    with pytest.raises(ValueError, match="finite"):
        Airframe(**_profile(phase_multipliers={
            Phase.GROUND: 0.05, Phase.CLIMB: 1.5, Phase.CRUISE: 1.0,
            Phase.DESCEND: 0.8, Phase.HOVER: float("nan")}))
    with pytest.raises(ValueError, match="zero phase multiplier"):
        Airframe(**_profile(phase_multipliers={
            Phase.GROUND: 0.05, Phase.CLIMB: 1.5, Phase.CRUISE: 1.0,
            Phase.DESCEND: 0.8, Phase.HOVER: 0.0}))
    # ...but GROUND may still be zero: an engine stopped on the ramp is real
    assert Airframe(**_profile(phase_multipliers={
        Phase.GROUND: 0.0, Phase.CLIMB: 1.5, Phase.CRUISE: 1.0,
        Phase.DESCEND: 0.8, Phase.HOVER: 1.17})).rates_pct_per_s()[Phase.GROUND] == 0.0
    # the three wind fields, in the form that passes a bare sign test
    with pytest.raises(ValueError, match="wind_penalty_per_mps"):
        Airframe(**_profile(wind_penalty_per_mps=float("nan")))
    with pytest.raises(ValueError, match="wind_ref_mps"):
        Airframe(**_profile(wind_ref_mps=float("inf")))
    with pytest.raises(ValueError, match="wind_penalty_max"):
        Airframe(**_profile(wind_penalty_max=float("inf")))
    with pytest.raises(ValueError, match="vertical rates"):
        Airframe(**_profile(climb_rate_mps=float("inf")))

    # ...and `FuelModel(rates=...)` is not a way around any of it. That
    # argument replaces the whole burn table without the profile validator
    # ever seeing it, so every value refused above had to be refused here too:
    # a table of zeros makes `endurance_s()` infinite and `_burn()` return
    # 0.0, which is the same stopped clock reached by another door.
    zeros = {p: 0.0 for p in Phase}
    with pytest.raises(ValueError, match="stop the fuel clock"):
        FuelModel(rates=zeros)
    with pytest.raises(ValueError, match="stop the fuel clock"):
        FuelModel(rates={**QUAD_SUAS_ELECTRIC.rates_pct_per_s(),
                         Phase.HOVER: 0.0})
    with pytest.raises(ValueError, match="finite"):
        FuelModel(rates={**QUAD_SUAS_ELECTRIC.rates_pct_per_s(),
                         Phase.CRUISE: float("nan")})
    # a GROUND rate of zero is still allowed, and still integrates
    ok = FuelModel(rates={**QUAD_SUAS_ELECTRIC.rates_pct_per_s(),
                          Phase.GROUND: 0.0})
    assert ok.rates[Phase.GROUND] == 0.0
    assert ok.rates[Phase.CRUISE] > 0.0

    # THE OUTCOME: on every profile that survives, the clock moves and the
    # gate is reachable. This is the property the raises above protect.
    for airframe in list(AIRFRAMES.values()) + [Airframe(**_profile())]:
        fm = FuelModel(airframe=airframe, home=(0.0, 0.0, 0.0))
        for phase in Phase:
            rate = fm.rates[phase]
            assert math.isfinite(rate), f"{airframe.id}/{phase.value}"
            if phase is not Phase.GROUND:
                assert rate > 0.0, f"{airframe.id}/{phase.value} burns nothing"
        now = 0.0
        fm.tick(15.0, 0.0, False, now=now)
        start = fm.fuel_pct
        while fm.fuel_pct > 0.0 and now < airframe.endurance_cruise_s * 4.0:
            now += MAX_TICK_DT_S
            fm.tick(15.0, 0.0, False, headwind_mps=15.0, now=now)
        assert fm.fuel_pct < start, airframe.id
        assert fm.fuel_pct == 0.0, (
            f"{airframe.id}: the tank never emptied in "
            f"{now / 3600.0:.1f} h — the fuel clock is stopped")


def test_a_profile_keyed_by_plain_strings_is_normalized_not_left_to_explode():
    """`Phase` is a `str` Enum, so the presence check could not see the bug.

    `Phase.CRUISE in {"cruise": 1.0}` is True — a `str` Enum member hashes and
    compares as its value — so a profile written with plain-string keys passed
    `phase_multipliers is missing ...`, passed the CRUISE == 1.0 check, and
    passed the sign checks. MEASURED on the code before this: the profile then
    built fine and `hash(af)` and `af.to_dict()` BOTH raised
    `AttributeError: 'str' object has no attribute 'value'`, because those two
    are the only methods that read `p.value` off a key. The new `__hash__` is
    what made this reachable from a cache or a set, so the mapping is
    normalized once at construction instead.
    """
    strkeyed = Airframe(**_profile(phase_multipliers={
        "ground": 0.05, "climb": 1.5, "cruise": 1.0,
        "descend": 0.8, "hover": 1.17}))
    enumkeyed = Airframe(**_profile())

    # both of these used to raise AttributeError
    assert isinstance(hash(strkeyed), int)
    assert strkeyed.to_dict()["phase_multipliers"]["cruise"] == 1.0
    # and the two spellings are ONE value, so they key a cache identically
    assert strkeyed == enumkeyed
    assert hash(strkeyed) == hash(enumkeyed)
    assert len({strkeyed, enumkeyed}) == 1
    assert {enumkeyed: "burn-table"}[strkeyed] == "burn-table"
    # the stored mapping has exactly one key type, whatever went in
    assert all(isinstance(k, Phase) for k in strkeyed.phase_multipliers)
    assert strkeyed.rates_pct_per_s()[Phase.HOVER] == pytest.approx(
        enumkeyed.rates_pct_per_s()[Phase.HOVER])

    # a key that is not a phase at all is refused BY NAME rather than carried
    # into the mapping to fail later
    with pytest.raises(ValueError, match="unknown phase key"):
        Airframe(**_profile(phase_multipliers={
            Phase.GROUND: 0.05, Phase.CLIMB: 1.5, Phase.CRUISE: 1.0,
            Phase.DESCEND: 0.8, Phase.HOVER: 1.17, "warp": 2.0}))
