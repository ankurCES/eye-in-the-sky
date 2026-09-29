"""Notional adjudication — WG spec §5.2.3, unit B1 (M14a, PLAN.md §4.5a).

B1's acceptance (§5.4): `p_kill_air` is monotone in range, 0 outside the ceiling
band and uses the shooter-relative `dz`; the damaged halving; the
`apply_outcome` chain; the `bda_state` table; both RNG streams reproducible.
Pure: no server, no ports, no network.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from itertools import pairwise

import pytest
from godseye_uav import wargame_adjudicate as A
from godseye_uav.wargame_tables import CLASSES, P_CAP, RADAR_CUE_BONUS
from support.wg_tokens import assert_no_real_system_tokens

AD_GUN, AD_SHORT, AD_MEDIUM = CLASSES["ad_gun"], CLASSES["ad_short"], CLASSES["ad_medium"]
CLEAR = {"visibility_factor": 1.0, "light_factor": 1.0}


@dataclass
class _Unit:
    """The fields `apply_outcome` touches on the engine's `Unit`."""

    wg_class: str
    state: str = "active"
    damaged: bool = False
    state_until_s: float | None = None
    ammo: int = 10


class _FixedRng:
    def __init__(self, *values: float) -> None:
        self.values = list(values)

    def random(self) -> float:
        return self.values.pop(0)


# ---------------------------------------------------------------- p_detect
def test_p_detect_curve_and_range_limit():
    assert A.p_detect(AD_SHORT, 0.0, CLEAR) == pytest.approx(AD_SHORT.p_detect_ref)
    half = A.p_detect(AD_SHORT, 7500.0, CLEAR)
    assert half == pytest.approx(0.80 * (1 - 0.25))
    samples = [A.p_detect(AD_SHORT, s, CLEAR) for s in range(0, 15001, 500)]
    assert all(a >= b for a, b in pairwise(samples))
    assert A.p_detect(AD_SHORT, 15000.0, CLEAR) == 0.0
    assert A.p_detect(AD_SHORT, 15001.0, CLEAR, cue=True) == 0.0   # a cue can't see past range
    assert A.p_detect(CLASSES["supply_depot"], 0.0, CLEAR) == 0.0  # no detection range


def test_p_detect_optical_only_scales_with_visibility_and_light():
    dim = {"visibility_factor": 0.5, "light_factor": 0.4}
    assert A.p_detect(AD_GUN, 0.0, dim) == pytest.approx(0.70 * 0.5 * 0.4)
    assert A.p_detect(AD_SHORT, 0.0, dim) == pytest.approx(0.80)   # radar: not optical
    nested = {"light_factor": 0.4, "weather": {"visibility_factor": 0.5}}  # sim.environment()
    assert A.p_detect(AD_GUN, 0.0, nested) == pytest.approx(0.70 * 0.5 * 0.4)
    for missing in (None, {}):
        assert A.p_detect(AD_GUN, 0.0, missing) == pytest.approx(0.70)
    assert A.env_factors({"visibility_factor": 3.0, "light_factor": -1.0}) == (1.0, 0.0)


def test_p_detect_radar_cue_bonus_and_cap():
    base = A.p_detect(AD_GUN, 1500.0, CLEAR)
    assert A.p_detect(AD_GUN, 1500.0, CLEAR, cue=True) == pytest.approx(base + RADAR_CUE_BONUS)
    assert A.p_detect(CLASSES["ad_long"], 0.0, CLEAR, cue=True) == P_CAP


# ---------------------------------------------------------------- p_kill_air
def test_p_kill_air_is_monotone_in_range_and_zero_beyond_it():
    dz = 100.0
    samples = [A.p_kill_air(AD_SHORT, s, dz, 10.0, False) for s in range(0, 8001, 250)]
    assert samples[0] == pytest.approx(AD_SHORT.pk_air)
    assert all(a > b for a, b in pairwise(samples))
    assert samples[-1] == pytest.approx(0.50 * 0.4)                  # 1 - 0.6 at R
    assert A.p_kill_air(AD_SHORT, 8000.5, dz, 10.0, False) == 0.0


def test_p_kill_air_is_zero_outside_the_ceiling_band():
    ceiling = AD_GUN.threat_ceiling_m
    assert A.p_kill_air(AD_GUN, 500.0, -1.0, 10.0, False) == 0.0           # below the unit
    assert A.p_kill_air(AD_GUN, 500.0, ceiling + 0.1, 10.0, False) == 0.0  # above ceiling
    assert A.p_kill_air(AD_GUN, 500.0, 0.0, 10.0, False) > 0.0
    low = A.p_kill_air(AD_GUN, 500.0, 0.8 * ceiling, 10.0, False)
    high = A.p_kill_air(AD_GUN, 500.0, 0.8 * ceiling + 1.0, 10.0, False)
    assert high == pytest.approx(low * 0.5)                                # top band halves
    assert A.p_kill_air(AD_GUN, 500.0, ceiling, 10.0, False) == pytest.approx(high)
    assert A.p_kill_air(CLASSES["radar_early_warning"], 0.0, 10.0, 0.0, False) == 0.0


def test_p_kill_air_uses_the_shooter_relative_dz():
    """The same aircraft at 1100 m HAE: out of reach of guns at 0 m HAE (dz 1100 >
    ceiling 1000), in reach of guns on a 500 m hill (dz 600)."""
    drone_hae = 1100.0
    assert A.p_kill_air(AD_GUN, 800.0, drone_hae - 0.0, 10.0, False) == 0.0
    assert A.p_kill_air(AD_GUN, 800.0, drone_hae - 500.0, 10.0, False) > 0.0
    # An aircraft below a unit on a ridge is never engaged.
    assert A.p_kill_air(AD_GUN, 800.0, 300.0 - 400.0, 10.0, False) == 0.0


def test_p_kill_air_speed_and_damage_factors():
    base = A.p_kill_air(AD_MEDIUM, 4000.0, 500.0, 10.0, False)
    assert A.p_kill_air(AD_MEDIUM, 4000.0, 500.0, 15.0, False) == pytest.approx(base)
    assert A.p_kill_air(AD_MEDIUM, 4000.0, 500.0, 15.1, False) == pytest.approx(base * 0.9)
    # Damaged: R shrinks to 0.8 R, and the probability is halved.
    r_d = A.threat_range(AD_MEDIUM, True)
    assert r_d == pytest.approx(0.8 * AD_MEDIUM.threat_range_m)
    assert A.p_kill_air(AD_MEDIUM, 0.0, 500.0, 10.0, True) == pytest.approx(
        A.p_kill_air(AD_MEDIUM, 0.0, 500.0, 10.0, False) * 0.5)
    between = 0.9 * AD_MEDIUM.threat_range_m
    assert A.p_kill_air(AD_MEDIUM, between, 500.0, 10.0, False) > 0.0
    assert A.p_kill_air(AD_MEDIUM, between, 500.0, 10.0, True) == 0.0
    damaged = A.p_kill_air(AD_MEDIUM, 4000.0, 500.0, 10.0, True)
    assert damaged == pytest.approx(0.55 * (1 - 0.6 * (4000 / r_d) ** 2) * 0.5)


# ---------------------------------------------------------------- p_ground
def test_p_ground_formula_range_and_damage():
    armour = CLASSES["armour_company"]
    assert A.p_ground(armour, 0.0, 1.0, False) == pytest.approx(0.35)
    assert A.p_ground(armour, 1500.0, 0.7, False) == pytest.approx(0.35 * (1 - 0.15) * 0.7)
    assert A.p_ground(armour, 3000.0, 1.0, False) == pytest.approx(0.35 * 0.4)
    assert A.p_ground(armour, 3001.0, 1.0, False) == 0.0
    assert A.p_ground(armour, 2500.0, 1.0, True) == 0.0            # damaged: 0.8 x range
    assert A.p_ground(armour, 1000.0, 1.0, True) == pytest.approx(
        A.p_ground(armour, 1000.0, 1.0, False) * 0.5)
    samples = [A.p_ground(armour, d, 1.0, False) for d in range(0, 3001, 250)]
    assert all(a > b for a, b in pairwise(samples))
    assert A.p_ground(CLASSES["ad_gun"], 0.0, 1.0, False) == 0.0    # no ground role


# ---------------------------------------------------------------- strikes
def test_strike_probabilities_split_and_range_fall_off():
    arty = CLASSES["blue_artillery"]
    p = A.strike_probabilities(arty, 0.85, 10000.0, "probable")
    effect = 0.50 * (1 - 0.4 * 0.25) * 0.85 * 0.85
    assert p == {"effect": round(effect, 2), "destroyed": round(0.32 * effect, 2),
                 "damaged": round(0.40 * effect, 2), "suppressed": round(0.28 * effect, 2)}
    assert A.strike_probabilities(arty, 1.0, 0.0, "confirmed")["effect"] == 0.5
    far = A.strike_probabilities(arty, 1.0, 20000.0, "confirmed")["effect"]
    assert far == pytest.approx(0.30)                                  # 1 - 0.4 at max range
    package = CLASSES["blue_strike_air"]                               # None: whole AO
    assert A.strike_probabilities(package, 1.0, 1e6, "confirmed")["effect"] == 0.65
    assert A.strike_probabilities(arty, 1.0, 0.0, "possible")["effect"] == 0.0
    assert A.strike_probabilities(CLASSES["blue_defended_point"], 1.0, 0.0,
                                  "confirmed")["effect"] == 0.0
    assert A.effect_bands(2.0)["effect"] == P_CAP


def test_draw_outcome_bands():
    p = {"effect": 0.5, "destroyed": 0.16, "damaged": 0.2, "suppressed": 0.14}
    for u, want in ((0.0, "destroyed"), (0.159, "destroyed"), (0.16, "damaged"),
                    (0.359, "damaged"), (0.36, "suppressed"), (0.499, "suppressed"),
                    (0.5, "missed"), (0.99, "missed")):
        assert A.draw_outcome(_FixedRng(u), p) == (want, u)
    assert A.draw_outcome(_FixedRng(0.0), {"effect": 0.0, "destroyed": 0.0, "damaged": 0.0,
                                           "suppressed": 0.0}) == ("missed", 0.0)


# ---------------------------------------------------------------- unit state
def test_apply_outcome_chain():
    unit = _Unit("ad_short")
    assert A.apply_outcome(unit, "missed", 10.0) == "active"
    assert A.apply_outcome(unit, "suppressed", 10.0) == "suppressed"
    assert unit.state_until_s == 10.0 + CLASSES["ad_short"].suppress_s
    assert not A.release_suppression(unit, unit.state_until_s - 0.1)
    assert A.release_suppression(unit, unit.state_until_s)
    assert (unit.state, unit.state_until_s) == ("active", None)
    assert A.apply_outcome(unit, "damaged", 20.0) == "damaged" and unit.damaged
    assert unit.ammo == 10
    assert A.apply_outcome(unit, "suppressed", 30.0) == "suppressed"
    assert A.release_suppression(unit, 30.0 + CLASSES["ad_short"].suppress_s)
    assert unit.state == "damaged"                                   # lapses back to damaged
    assert A.apply_outcome(unit, "damaged", 40.0) == "destroyed"     # second damage destroys
    assert (unit.ammo, unit.state_until_s) == (0, None)
    for outcome in ("suppressed", "damaged", "missed"):              # destroyed is terminal
        assert A.apply_outcome(unit, outcome, 50.0) == "destroyed"
    assert not A.release_suppression(unit, 1e9)


def test_apply_outcome_destroyed_and_unknown():
    unit = _Unit("blue_artillery", state="suppressed", state_until_s=99.0)
    assert A.apply_outcome(unit, "destroyed", 1.0) == "destroyed"
    assert (unit.ammo, unit.state_until_s, unit.damaged) == (0, None, False)
    blue = _Unit("blue_mech")
    A.apply_outcome(blue, "suppressed", 5.0)
    assert blue.state_until_s == 5.0 + 180.0                         # blue suppression holds
    with pytest.raises(ValueError):
        A.apply_outcome(_Unit("ad_gun"), "vaporised", 0.0)


def test_bda_state_table():
    assert A.bda_state("destroyed", False, 0) == "none"
    assert A.bda_state("active", True, 0) == "none"
    assert A.bda_state("destroyed", False, 1) == "destroyed_probable"
    assert A.bda_state("destroyed", True, 2) == "destroyed_confirmed"
    assert A.bda_state("destroyed", True, 5) == "destroyed_confirmed"
    assert A.bda_state("damaged", True, 1) == "damaged"
    assert A.bda_state("suppressed", True, 3) == "damaged"
    assert A.bda_state("suppressed", False, 1) == "no_change"
    assert A.bda_state("active", False, 2) == "no_change"
    assert {A.bda_state(s, d, n) for s in A.UNIT_STATES for d in (False, True)
            for n in (0, 1, 2)} == set(A.BDA_STATES)


# ---------------------------------------------------------------- RNG streams
def test_both_rng_streams_are_reproducible_and_independent():
    red, blue = A.make_streams(4417)
    red2, blue2 = A.make_streams(4417)
    seq_red = [red.random() for _ in range(50)]
    assert seq_red == [red2.random() for _ in range(50)]
    assert [blue.random() for _ in range(20)] == [blue2.random() for _ in range(20)]
    ref = random.Random("4417:red")
    assert seq_red == [ref.random() for _ in range(50)]
    assert A.stream_seed(4417, "blue") == "4417:blue"
    assert A.make_streams(4417)[1].random() == random.Random("4417:blue").random()
    assert A.make_streams(4417)[0].random() != A.make_streams(4417)[1].random()
    assert A.make_streams(4418)[1].random() != A.make_streams(4417)[1].random()
    # Blue outcomes depend only on the seed and the order of strikes: any number of
    # red draws in between leaves the blue sequence unchanged.
    _red3, b3 = A.make_streams(4417)
    quiet = [b3.random() for _ in range(5)]
    r4, b4 = A.make_streams(4417)
    noisy = []
    for i in range(5):
        for _ in range(i * 7 + 3):
            r4.random()
        noisy.append(b4.random())
    assert quiet == noisy
    assert (r4.draw, b4.draw) == (sum(i * 7 + 3 for i in range(5)), 5)


def test_draw_counter_numbers_each_draw():
    red, blue = A.make_streams("seed-x")
    assert (red.draw, blue.draw) == (0, 0)
    outcome, _u = A.draw_outcome(blue, A.strike_probabilities(
        CLASSES["blue_rocket"], 0.85, 12000.0, "confirmed"))
    assert blue.draw == 1 and red.draw == 0 and outcome in A.OUTCOMES
    A.draw_outcome(blue, {"effect": 0.5, "destroyed": 0.2, "damaged": 0.2, "suppressed": 0.1})
    assert blue.draw == 2
    assert "draw=2" in repr(blue)


def test_seeded_strike_sequence_is_exact():
    """The same seed and order of strikes gives the same outcomes and draw numbers."""
    def run(seed):
        _, blue = A.make_streams(seed)
        out = []
        for shooter, rng_m in (("blue_artillery", 3200.0), ("blue_rocket", 15000.0),
                               ("blue_strike_air", 9000.0), ("blue_artillery", 18000.0)):
            p = A.strike_probabilities(CLASSES[shooter], CLASSES["ad_gun"].hardness,
                                       rng_m, "probable")
            outcome, u = A.draw_outcome(blue, p)
            out.append((outcome, round(u, 12), blue.draw))
        return out
    assert run(4417) == run(4417)
    assert [d for *_, d in run(4417)] == [1, 2, 3, 4]


def test_no_real_system_tokens_in_adjudication_vocabulary():
    assert_no_real_system_tokens({
        "outcomes": A.OUTCOMES, "states": A.UNIT_STATES, "bda": A.BDA_STATES,
        "bands": A.strike_probabilities(CLASSES["blue_artillery"], 0.85, 1000.0, "confirmed"),
        "docs": [A.__doc__, A.p_kill_air.__doc__, A.apply_outcome.__doc__],
    })
