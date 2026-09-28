"""A2 (WG spec §4.1.6): airframe reach and the fuel clock at sim speed.

Scale tests 3 and 7 of §4.1.6, `reach_radius_m` (quad 7350 m, group 3
352,800 m), the two new `Airframe` fields, and `FuelModel.time_scale`. Scale
test 3 flies the fake on an injected clock, so nothing here waits on wall time
or opens a socket.
"""
from __future__ import annotations

import math

import pytest
from godseye_uav import safety
from godseye_uav.fake_airsim import FakeAirSim, _Task
from godseye_uav.geo import NedPoint
from godseye_uav.safety import (
    GROUP3_FIXED_WING,
    MAX_TICK_DT_S,
    QUAD_SUAS_ELECTRIC,
    RESERVE_PCT,
    Airframe,
    FuelModel,
    Phase,
    reach_radius_m,
)

HOME = (47.641468, -122.140165, 122.0)


def _airframe(**kw) -> Airframe:
    base = {"id": "bench", "summary": "", "source": "", "endurance_cruise_s": 600.0,
            "phase_multipliers": {p: 1.0 for p in Phase}}
    base.update(kw)
    return Airframe(**base)


# ---------------------------------------------------------------------------
# reach_radius_m and the AO clamp it feeds (C7).
# ---------------------------------------------------------------------------
def test_reach_radius_of_the_shipped_airframes():
    assert reach_radius_m("quad_suas_electric") == pytest.approx(7350.0, abs=1e-9)
    assert reach_radius_m(QUAD_SUAS_ELECTRIC) == pytest.approx(7350.0, abs=1e-9)
    assert reach_radius_m("group3_fixed_wing") == pytest.approx(352_800.0, abs=1e-6)
    assert reach_radius_m(GROUP3_FIXED_WING) == pytest.approx(352_800.0, abs=1e-6)


def test_reach_radius_default_follows_the_process_airframe(monkeypatch):
    monkeypatch.delenv(safety.AIRFRAME_ENV_VAR, raising=False)
    assert reach_radius_m(None) == pytest.approx(7350.0)
    monkeypatch.setenv(safety.AIRFRAME_ENV_VAR, "group3_fixed_wing")
    assert reach_radius_m(None) == pytest.approx(352_800.0)


def test_reach_radius_formula_and_its_knobs():
    af = _airframe(endurance_cruise_s=1000.0, cruise_speed_mps=12.0)
    assert RESERVE_PCT == 20.0 and safety.REACH_MARGIN_PCT == 10.0
    assert reach_radius_m(af) == pytest.approx(0.5 * 0.70 * 1000.0 * 12.0)
    assert reach_radius_m(af, margin_pct=0.0) == pytest.approx(0.5 * 0.80 * 12_000.0)
    assert reach_radius_m(af, reserve_pct=0.0, margin_pct=0.0) == pytest.approx(6000.0)


@pytest.mark.parametrize("kw", [{"reserve_pct": 60.0, "margin_pct": 40.0},
                                {"reserve_pct": -1.0}, {"margin_pct": -5.0},
                                {"reserve_pct": math.nan}, {"margin_pct": math.inf}])
def test_reach_radius_refuses_a_reserve_that_leaves_no_tank(kw):
    with pytest.raises(ValueError):
        reach_radius_m("quad_suas_electric", **kw)


def test_reach_radius_refuses_an_unknown_airframe():
    with pytest.raises(ValueError, match="unknown airframe"):
        reach_radius_m("hexacopter_9000")


def test_the_ao_clamp_the_proposal_derives_from_reach():
    """C7: half-extent max = clamp(0.4 x reach, 1500, 25000)."""
    def ao_max_half(af):
        return min(max(0.4 * reach_radius_m(af), 1500.0), 25_000.0)

    assert ao_max_half("quad_suas_electric") == pytest.approx(2940.0)
    assert ao_max_half("group3_fixed_wing") == 25_000.0


# ---------------------------------------------------------------------------
# Airframe.label / cruise_speed_mps.
# ---------------------------------------------------------------------------
def test_shipped_airframes_carry_label_and_cruise_speed_in_to_dict():
    q, g = QUAD_SUAS_ELECTRIC.to_dict(), GROUP3_FIXED_WING.to_dict()
    assert (q["label"], q["cruise_speed_mps"]) == ("Quad, small electric", 10.0)
    assert (g["label"], g["cruise_speed_mps"]) == ("Fixed-wing, group 3", 20.0)
    # the existing keys are all still there
    for key in ("id", "summary", "source", "endurance_cruise_s", "phase_multipliers",
                "cruise_pct_per_s", "climb_rate_mps", "descend_rate_mps"):
        assert key in q


def test_a_custom_airframe_defaults_its_label_to_its_id_and_cruise_to_10():
    af = _airframe()
    assert af.label == "bench" and af.cruise_speed_mps == 10.0
    assert _airframe(label="Bench quad").to_dict()["label"] == "Bench quad"


@pytest.mark.parametrize("bad", [0.0, -3.0, math.nan, math.inf, "fast", None])
def test_a_cruise_speed_that_breaks_the_reach_is_refused(bad):
    with pytest.raises(ValueError, match="cruise_speed_mps"):
        _airframe(cruise_speed_mps=bad)


def test_cruise_speed_is_part_of_equality_and_the_label_is_display_only():
    a, b = _airframe(cruise_speed_mps=10), _airframe(cruise_speed_mps=10.0)
    assert a == b and hash(a) == hash(b)
    assert _airframe(cruise_speed_mps=12.0) != a  # it changes the reach
    x, y = _airframe(label="x"), _airframe(label="y")
    assert x == y and hash(x) == hash(y)  # same energy model, other name


# ---------------------------------------------------------------------------
# FuelModel.time_scale (scale test 7 and the field's contract).
# ---------------------------------------------------------------------------
def test_scale7_a_60s_wall_gap_at_x10_charges_300_sim_seconds():
    fm = FuelModel(time_scale=10.0)
    fm.tick(10.0, 0.0, False, now=0.0)          # primes the clock
    fm.tick(10.0, 0.0, False, now=60.0)         # a 60 s stall
    assert MAX_TICK_DT_S == 30.0
    assert fm.clamped_ticks == 1
    assert fm.unaccounted_s == pytest.approx(30.0)   # WALL seconds dropped
    assert fm.elapsed_s == pytest.approx(300.0)      # SIM seconds charged
    cruise = QUAD_SUAS_ELECTRIC.cruise_rate_pct_per_s
    assert fm.burned_pct == pytest.approx(300.0 * cruise)
    assert fm.fuel_pct == pytest.approx(100.0 - 300.0 * cruise)


def test_the_default_scale_is_1_and_burns_exactly_as_before():
    legacy, scaled = FuelModel(), FuelModel(time_scale=1.0)
    assert legacy.time_scale == 1.0
    for t in (0.0, 0.5, 1.0, 40.0, 40.5):
        legacy.tick(10.0, 0.0, False, now=t)
        scaled.tick(10.0, 0.0, False, now=t)
    assert legacy.fuel_pct == scaled.fuel_pct
    assert legacy.elapsed_s == pytest.approx(30.0 + 1.0 + 0.5)
    assert legacy.fuel_pct == pytest.approx(
        100.0 - 31.5 * QUAD_SUAS_ELECTRIC.cruise_rate_pct_per_s)


def test_x10_charges_ten_sim_seconds_per_wall_second_in_every_phase():
    for speed, vz, landed, phase in ((10.0, 0.0, False, Phase.CRUISE),
                                     (0.0, 0.0, False, Phase.HOVER),
                                     (0.0, -2.0, False, Phase.CLIMB),
                                     (0.0, 0.0, True, Phase.GROUND)):
        fm = FuelModel(time_scale=10.0)
        fm.tick(speed, vz, landed, now=0.0)
        fm.tick(speed, vz, landed, now=1.0)
        assert fm.elapsed_s == pytest.approx(10.0)
        assert fm.burned_pct == pytest.approx(10.0 * fm.rates[phase]), phase


def test_the_scale_can_change_on_a_live_model():
    fm = FuelModel()
    fm.tick(10.0, 0.0, False, now=0.0)
    fm.tick(10.0, 0.0, False, now=1.0)
    fm.time_scale = 4          # what the server does to a running monitor
    assert fm.time_scale == 4.0 and isinstance(fm.time_scale, float)
    fm.tick(10.0, 0.0, False, now=2.0)
    assert fm.elapsed_s == pytest.approx(5.0)


@pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf, True, "x", None])
def test_a_scale_that_stops_or_poisons_the_fuel_clock_is_refused(bad):
    with pytest.raises(ValueError, match="time_scale"):
        FuelModel(time_scale=bad)
    fm = FuelModel()
    with pytest.raises(ValueError, match="time_scale"):
        fm.time_scale = bad
    assert fm.time_scale == 1.0


def test_time_scale_is_journaled_per_row_but_never_persisted_as_state():
    fm = FuelModel(airframe="group3_fixed_wing", home=HOME, time_scale=10.0)
    fm.tick(12.0, 0.0, False, now=0.0)
    fm.tick(12.0, 0.0, False, now=2.0)
    assert fm.fuel_record()["time_scale"] == 10.0
    assert fm.fuel_record(t=1.0)["airframe"] == "group3_fixed_wing"
    state = fm.to_dict()
    assert "time_scale" not in state
    back = FuelModel.from_dict(state)
    assert back.time_scale == 1.0, "a restart runs at x1"
    assert back.fuel_pct == fm.fuel_pct and back.elapsed_s == fm.elapsed_s


def test_bingo_and_the_preflight_gate_do_not_depend_on_sim_speed():
    route = [{"lat": 47.6500, "lon": -122.1400, "alt_m": 60.0},
             {"lat": 47.6500, "lon": -122.1300, "alt_m": 60.0}]
    slow, fast = FuelModel(home=HOME), FuelModel(home=HOME, time_scale=10.0)
    assert slow.preflight_gate(route, HOME[:2], 10.0) == fast.preflight_gate(route, HOME[:2], 10.0)
    at = (47.6500, -122.1300)
    assert slow.bingo_fuel_pct(at, 60.0) == fast.bingo_fuel_pct(at, 60.0)
    slow.fuel_pct = fast.fuel_pct = 21.0
    assert slow.check_bingo(at, 60.0) == fast.check_bingo(at, 60.0)


# ---------------------------------------------------------------------------
# Scale test 3: the same sim-distance route at x1 and x10 burns the same fuel.
# ---------------------------------------------------------------------------
class _Clock:
    def __init__(self) -> None:
        self.t = 500.0

    def __call__(self) -> float:
        return self.t


def _fly_route(scale: float, fuel_scale: float | None = None) -> tuple[float, float, float]:
    """Fly a climb + 300 m + 200 m route in the fake at `scale`, fuel-ticked the
    way the server does it: the monitor sleeps max(0.05, 0.5 / scale) wall s.

    Returns (burned_pct, sim seconds charged, wall seconds taken).
    """
    clock = _Clock()
    sim = FakeAirSim(clock=clock)
    sim.set_time_scale(scale)
    fm = FuelModel(home=HOME, time_scale=scale if fuel_scale is None else fuel_scale)
    v = sim._vehicles["Drone1"]
    legs = [NedPoint(0.0, 0.0, -30.0), NedPoint(300.0, 0.0, -30.0),
            NedPoint(300.0, 200.0, -30.0)]
    with sim._lock:
        v.armed, v.landed = True, False
        v.task = _Task("move_pos", legs.pop(0), 3.0)
    period = max(0.05, 0.5 / scale)       # server._monitor_loop at this scale
    physics = 0.02
    t0, next_tick = clock.t, clock.t
    fm.tick(0.0, 0.0, False, now=clock.t)
    while clock.t - t0 < 600.0:
        clock.t += physics
        sim._integrate()
        if v.task.done and legs:
            with sim._lock:
                v.task = _Task("move_pos", legs.pop(0), 10.0)
        if clock.t >= next_tick + period - 1e-9:
            next_tick = clock.t
            fm.tick(math.hypot(v.vel.x, v.vel.y), v.vel.z, v.landed, now=clock.t)
            if v.task.done and not legs:
                break
    assert v.task.done and not legs, "the route never finished"
    assert v.ned.x == pytest.approx(300.0, abs=0.6) and v.ned.y == pytest.approx(200.0, abs=0.6)
    return fm.burned_pct, fm.elapsed_s, clock.t - t0


def test_scale3_the_same_route_at_x1_and_x10_burns_within_5_percent():
    burn1, sim1, wall1 = _fly_route(1.0)
    burn10, sim10, wall10 = _fly_route(10.0)
    assert wall10 == pytest.approx(wall1 / 10.0, rel=0.1)   # it really ran faster
    assert sim10 == pytest.approx(sim1, rel=0.05)
    assert burn10 == pytest.approx(burn1, rel=0.05)
    # ...and the burn is the route's: 10 s of climb + 50 s of cruise, roughly
    rates = QUAD_SUAS_ELECTRIC.rates_pct_per_s()
    expect = 10.0 * rates[Phase.CLIMB] + 50.0 * rates[Phase.CRUISE]
    assert burn1 == pytest.approx(expect, rel=0.1)


def test_scale3_an_unscaled_fuel_clock_under_x10_would_under_burn_tenfold():
    """Why the coupling exists: the tank must be charged the SIM seconds flown."""
    burn1, _, _ = _fly_route(1.0)
    burn_uncoupled, _, _ = _fly_route(10.0, fuel_scale=1.0)
    assert burn_uncoupled == pytest.approx(burn1 / 10.0, rel=0.1)
