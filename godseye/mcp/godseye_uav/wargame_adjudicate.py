"""Notional adjudication for the simulated wargame (M14a, PLAN.md §4.5a; WG spec §5.2.3).

Pure functions over `wargame_tables` classes: probability-of-detection and
probability-of-effect curves, the outcome draw, unit state changes and the
battle-damage-assessment tiers. Abstract and notional (D1): every number is a
play-balance parameter from `wargame_tables`, never weapon data.

`dz` is the SHOOTER-RELATIVE height: the observer's `alt_hae_m` minus the unit's
`alt_hae_m` (V21). An aircraft below the unit, or above its ceiling, is never
engaged by `p_kill_air`.

Randomness only comes in through the stream a caller passes. The engine owns two
seeded streams (`make_streams`): `red` for `step()` and `blue` for strikes and
package draws, each counting its draws, so blue outcomes depend only on the seed
and the order of strikes.
"""
from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any

from .wargame_tables import (
    CLASSES,
    CONF_FACTOR,
    DAMAGED_PK_FACTOR,
    DAMAGED_RANGE_FACTOR,
    EFFECT_SPLIT,
    P_CAP,
    RADAR_CUE_BONUS,
    WgClass,
)

OUTCOMES = ("missed", "suppressed", "damaged", "destroyed")
UNIT_STATES = ("active", "suppressed", "damaged", "destroyed")
BDA_STATES = ("none", "no_change", "damaged", "destroyed_probable", "destroyed_confirmed")
FAST_SPEED_MPS = 15.0          # faster than this: p_kill_air x 0.9
HIGH_BAND_FRAC = 0.8           # dz above this fraction of the ceiling: p_kill_air x 0.5


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def _cap(p: float) -> float:
    return _clamp(p, 0.0, P_CAP)


def env_factors(env: Mapping[str, Any] | None) -> tuple[float, float]:
    """`(visibility_factor, light_factor)` from `sim.environment()`, each in [0, 1]
    and 1.0 when missing. Reads a top-level `visibility_factor` first, else the one
    nested in `environment()["weather"]`."""
    env = env if isinstance(env, Mapping) else {}
    vis = env.get("visibility_factor")
    if vis is None and isinstance(env.get("weather"), Mapping):
        vis = env["weather"].get("visibility_factor")
    light = env.get("light_factor")
    return (_clamp(1.0 if vis is None else vis), _clamp(1.0 if light is None else light))


def p_detect(cls: WgClass, slant: float, env: Mapping[str, Any] | None, *,
             cue: bool = False) -> float:
    """`min(P_CAP, p_detect_ref x clamp(1 - (slant/detection_range_m)^2, 0, 1)
    x (vis x light if optical else 1) + (RADAR_CUE_BONUS if cue else 0))`.
    0 beyond the detection range (a cue never lets a unit see past its own)."""
    rng_m = cls.detection_range_m
    if rng_m <= 0.0 or slant < 0.0 or slant > rng_m:
        return 0.0
    p = cls.p_detect_ref * _clamp(1.0 - (slant / rng_m) ** 2)
    if cls.optical:
        vis, light = env_factors(env)
        p *= vis * light
    return _cap(p + (RADAR_CUE_BONUS if cue else 0.0))


def threat_range(cls: WgClass, damaged: bool) -> float:
    """R: the class's air threat range, x DAMAGED_RANGE_FACTOR when damaged."""
    return cls.threat_range_m * (DAMAGED_RANGE_FACTOR if damaged else 1.0)


def p_kill_air(cls: WgClass, slant: float, dz: float, speed: float,
               damaged: bool) -> float:
    """`min(P_CAP, pk_air x (1 - 0.6 (slant/R)^2) x (0.5 if dz > 0.8 ceiling)
    x (0.9 if speed > 15) x (DAMAGED_PK_FACTOR if damaged))`; 0 unless
    `0 <= dz <= ceiling` and `slant <= R`. `dz` is observer minus unit HAE."""
    r = threat_range(cls, damaged)
    ceiling = cls.threat_ceiling_m
    if r <= 0.0 or ceiling <= 0.0 or not (0.0 <= dz <= ceiling) or not (0.0 <= slant <= r):
        return 0.0
    p = cls.pk_air * (1.0 - 0.6 * (slant / r) ** 2)
    if dz > HIGH_BAND_FRAC * ceiling:
        p *= 0.5
    if speed > FAST_SPEED_MPS:
        p *= 0.9
    if damaged:
        p *= DAMAGED_PK_FACTOR
    return _cap(p)


def p_ground(cls: WgClass, dist: float, target_hardness: float, damaged: bool) -> float:
    """`min(P_CAP, pk_ground x (1 - 0.6 (dist/ground_range_m)^2) x target_hardness
    x (DAMAGED_PK_FACTOR if damaged))`; 0 beyond `ground_range_m`
    (x DAMAGED_RANGE_FACTOR when damaged)."""
    g = cls.ground_range_m
    if g <= 0.0 or dist < 0.0 or dist > g * (DAMAGED_RANGE_FACTOR if damaged else 1.0):
        return 0.0
    p = cls.pk_ground * (1.0 - 0.6 * (dist / g) ** 2) * float(target_hardness)
    return _cap(p * (DAMAGED_PK_FACTOR if damaged else 1.0))


def effect_bands(effect: float) -> dict[str, float]:
    """`{effect, destroyed, damaged, suppressed}`: `effect` split by EFFECT_SPLIT,
    rounded to 2 dp. The rounded numbers are both shown and drawn against."""
    e = _cap(effect)
    out = {"effect": round(e, 2)}
    for k in ("destroyed", "damaged", "suppressed"):
        out[k] = round(EFFECT_SPLIT[k] * e, 2)
    return out


def strike_probabilities(shooter: WgClass, hardness: float, range_m: float,
                         confidence: str) -> dict[str, float]:
    """Notional strike effect: `min(P_CAP, pk_strike x (1 - 0.4 (range/strike_range)^2
    if strike_range else 1) x hardness x CONF_FACTOR[confidence])`, split by
    `effect_bands`. An unknown confidence (below "probable") gives 0."""
    sr = shooter.strike_range_m
    fall = _clamp(1.0 - 0.4 * (float(range_m) / sr) ** 2) if sr else 1.0
    eff = shooter.pk_strike * fall * float(hardness) * CONF_FACTOR.get(confidence, 0.0)
    return effect_bands(eff)


def draw_outcome(rng: Any, p: Mapping[str, float]) -> tuple[str, float]:
    """One draw `u = rng.random()`: `u < destroyed` destroyed; `< destroyed +
    damaged` damaged; `< effect` suppressed; else missed."""
    u = rng.random()
    d = float(p.get("destroyed", 0.0))
    dm = d + float(p.get("damaged", 0.0))
    if u < d:
        return "destroyed", u
    if u < dm:
        return "damaged", u
    if u < float(p.get("effect", 0.0)):
        return "suppressed", u
    return "missed", u


def apply_outcome(unit: Any, outcome: str, now_s: float) -> str:
    """Apply an outcome to a unit (duck-typed: `wg_class`, `state`, `damaged`,
    `state_until_s`, `ammo`) and return its new state. Destroyed is terminal.

    suppressed: `state="suppressed"` until `now_s + suppress_s`;
    damaged: a second damage destroys, else `damaged=True`, `state="damaged"`;
    destroyed: `state="destroyed"`, `ammo=0`; missed: nothing.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome!r}")
    if unit.state == "destroyed" or outcome == "missed":
        return unit.state
    if outcome == "suppressed":
        unit.state = "suppressed"
        unit.state_until_s = float(now_s) + CLASSES[unit.wg_class].suppress_s
        return unit.state
    if outcome == "damaged" and not unit.damaged:
        unit.damaged = True
        unit.state = "damaged"
        unit.state_until_s = None
        return unit.state
    unit.state = "destroyed"
    unit.ammo = 0
    unit.state_until_s = None
    return unit.state


def release_suppression(unit: Any, now_s: float) -> bool:
    """When suppression lapses (`state_until_s <= now_s`), the state returns to
    "damaged" if `unit.damaged` else "active". True when it changed."""
    until = unit.state_until_s
    if unit.state != "suppressed" or until is None or until > now_s:
        return False
    unit.state = "damaged" if unit.damaged else "active"
    unit.state_until_s = None
    return True


def bda_state(truth_state: str, damaged: bool, looks: int) -> str:
    """BDA tier (R8): none with no look; a destroyed unit is probable after one
    look and confirmed after two; else damaged or no_change."""
    if looks <= 0:
        return "none"
    if truth_state == "destroyed":
        return "destroyed_probable" if looks == 1 else "destroyed_confirmed"
    return "damaged" if damaged else "no_change"


def stream_seed(seed: Any, name: str) -> str:
    """The seed string of stream `name`: `f"{seed}:{name}"`."""
    return f"{seed}:{name}"


class RngStream:
    """A seeded random stream that numbers its draws.

    `random.Random(f"{seed}:{name}")` underneath (string seeds hash with SHA-512,
    so the sequence is the same in every process). `draw` is the number of draws
    taken so far; after a draw it is that draw's number (1-based), which the
    engine records on the engagement.
    """

    def __init__(self, seed: Any, name: str) -> None:
        self.seed = seed
        self.name = name
        self.draw = 0
        self._rng = random.Random(stream_seed(seed, name))

    def random(self) -> float:
        self.draw += 1
        return self._rng.random()

    def __repr__(self) -> str:
        return f"RngStream(seed={self.seed!r}, name={self.name!r}, draw={self.draw})"


def make_streams(seed: Any) -> tuple[RngStream, RngStream]:
    """`(rng_red, rng_blue)`: `red` drives `step()`, `blue` strikes and package draws."""
    return RngStream(seed, "red"), RngStream(seed, "blue")
