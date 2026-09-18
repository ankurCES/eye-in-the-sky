"""Safety envelope: geofence, ceiling/speed/AGL limits, BINGO fuel, lost link.

Server-enforced (PLAN §4.5). The skill's ROE may only be stricter, never
looser. All geometry is WGS84 degrees + metres; the polygon test is a
ray-cast in a local equirectangular projection (good to <1 m inside a
tactical AO of tens of km).

Fuel doctrine (M4/T5)
---------------------
  - One integrator, two uses: live ticks reconcile against *measured*
    phase; dry-run estimates run the SAME integrator over a waypoint plan.
  - Pre-flight gate: est. plan fuel + return leg + 20% reserve <= capacity.
  - Reaching BINGO in flight = force-RTB (un-cancellable safety transition).

Airframe energy model — what this vehicle IS (M4/T5)
----------------------------------------------------
Fuel is normalized to 0..100%, so the ONLY thing that gives the percentage a
meaning is the burn rate. The rate therefore comes from a named, tunable
:class:`Airframe` profile instead of magic constants: a real airframe is
calibrated against its own flight-test data, and a minimal model cannot guess
that for you. Every constant below is a field of a profile you can replace.

**Default profile `quad_suas_electric`: a Group-1/2 electric multirotor sUAS
carrying an EO/IR gimbal** — the airframe AirSim actually simulates (the
SimpleFlight quadrotor), flown here at 8-15 m/s under a 20 m/s limit.
Published endurance for that class sits in the 30-45 min band: Skydio X10
~40 min, DJI Matrice 30T 41 min max / 36 min hover, Parrot ANAFI USA 32 min,
Teal Golden Eagle ~30 min. The profile is calibrated to **35 min (2100 s) of
cruise endurance**, with hover ~30 min: a multirotor's induced power falls in
forward flight (translational lift), so cruise at the best-endurance speed
draws roughly 0.85x hover power. Climb adds m*g*V_climb/eta on top of that,
descent recovers part of it, and "ground" is avionics + datalink + payload
only. Those four ratios are the profile's `phase_multipliers`; see
:data:`QUAD_SUAS_ELECTRIC` for the arithmetic behind each one.

Why a credible number matters: at the 14-hour figure this module used to
carry, cruise burned ~0.0021 %/s and a 20% BINGO line was ~9 flight-hours
away. Every mission this system actually flies gated against a practically
full tank, and the un-cancellable force-RTB of PLAN §4.5 — the single most
important safety behaviour here — was exercised by nothing except a unit test
that set `fuel_pct` by hand. With the default profile a 35-minute sortie
reaches BINGO, which is what makes the doctrine testable end to end.

:data:`GROUP3_FIXED_WING` (ScanEagle-class, 14 h) is kept for theatre-scale
sorties a multirotor genuinely cannot fly. Select a profile explicitly with
``FuelModel(airframe="group3_fixed_wing")``, or set the ``GODSEYE_AIRFRAME``
environment variable for a process that builds its own FuelModels. An unknown
id raises: there is no silent fallback to the default.

Envelope breach doctrine — which finding commits the vehicle (§4.5)
-------------------------------------------------------------------
Deliberately narrower than the old field name `breach_forces_rtb` implied.
:data:`BREACH_DOCTRINE` is the table, it is published on
``uav://safety/geofence``, and `SafetyMonitor.tick` implements exactly it:

  * ``geofence``   -> **force RTB**. Outside the AO is outside the airspace
    the mission was cleared for. The vehicle cannot fix that where it is, and
    the correction (fly back) IS the RTB.
  * ``ceiling`` / ``max_speed`` -> **alarm, never RTB**. Both are recoverable
    in place: the fix is to descend or to slow down. An RTB flies at speed and
    at altitude, so it cannot correct either, and committing to one over a
    gust would convert a self-correcting excursion into a lost mission. If the
    excursion does NOT clear, that is surfaced as `sustained_breaches` on the
    tick verdict (with how long it has been standing) rather than silently
    tolerated — command authority stays with the operator (M14).
  * ``min_agl``    -> **alarm, never RTB**. Every takeoff and every landing
    flies through the band by definition; the server already audits those as
    `envelope_transition`. An RTB ends in a landing, so it cannot be the
    remedy for being low.
  * ``geofence_proximity`` -> **warning**. Still inside the fence.
  * BINGO fuel -> force RTB, and un-cancellable (the only un-cancellable one).

`rtb_reasons` therefore carries only `bingo`, `geofence` and `lost_link` —
the three transitions the enforcement layer (`server._enforce`) actually
flies. The model does not manufacture an RTB nothing will execute.

A finding with no row in the table is published as
:data:`UNDECLARED_DOCTRINE` and listed under `undeclared_doctrine` on the
tick verdict — never as `alarm_only`, which is itself a decision ("seen, and
deliberately does not commit the vehicle") and would be stated on a kind
nobody classified. :data:`ENVELOPE_VIOLATION_KINDS` is checked against the
table at import, so this can only ever describe a kind some other layer
invented, not one this envelope emits.

This module is the *model* only: it owns the math, the state machines and
the serialization hooks. The tasking/server layer drives it from the
telemetry tick loop (`SafetyMonitor.tick`) and executes what it decides.

Entry points for a telemetry tick loop:
  - `SafetyMonitor.tick(...)`  — one call per telemetry sample; returns fuel,
    BINGO state, structured envelope violations, link state, alarm edges and
    the `force_rtb` decision.
  - `FuelModel.tick(...)`      — the integrator alone (T5).
  - `SafetyEnvelope.check_state(...)` — in-flight envelope check (M4/§3.1).
  - `LostLinkMonitor.observe(...)`    — link state machine (M9).
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

EARTH_RADIUS_M = 6_371_008.8
RESERVE_PCT = 20.0

# Largest dt a single integrator tick may charge (T5). A suspended process,
# a resumed laptop or a wall-clock jump must not drain the tank in one step.
MAX_TICK_DT_S = 30.0

# Mission flag required by PLAN §4.5 when BINGO forces RTB (M4).
MISSION_INCOMPLETE_FUEL = "incomplete - fuel"


class Phase(str, Enum):
    GROUND = "ground"
    CLIMB = "climb"
    CRUISE = "cruise"
    DESCEND = "descend"
    HOVER = "hover"


# Phases whose burn a headwind penalises: the vehicle is making way against
# the air mass. A descent is gravity-assisted and a hover has no ground track.
WIND_SENSITIVE_PHASES = (Phase.CRUISE, Phase.CLIMB)


@dataclass(frozen=True)
class Airframe:
    """A named, tunable energy model for ONE airframe class (M4/T5).

    Fuel is a normalized 0..100%, so `endurance_cruise_s` is what gives the
    percentage a physical meaning, and `phase_multipliers` is how the other
    flight phases relate to cruise. Both are *calibration inputs* — replace
    them with numbers measured on the real vehicle rather than trusting the
    defaults, which are published-spec figures for the airframe class.

    Fields:
      `endurance_cruise_s`  seconds of cruise a full tank buys (100% -> 0%).
      `phase_multipliers`   burn rate per :class:`Phase` as a multiple of the
                            cruise rate. CRUISE must be 1.0 by construction.
      `wind_penalty_per_mps` / `wind_ref_mps` / `wind_penalty_max`
                            M15 headwind penalty: rate *= 1 + min(max,
                            per_mps * min(headwind, ref)). All three are
                            validated: a zero reference or ceiling would make
                            the penalty dead code and a negative rate would
                            make a headwind cheaper than calm air, and both
                            are wrong silently rather than loudly.
      `climb_rate_mps` / `descend_rate_mps`
                            the vertical rates a DRY RUN assumes, so the
                            pre-flight gate prices the same let-down the
                            in-flight integrator will measure (T5).
    """

    id: str
    summary: str
    source: str
    endurance_cruise_s: float
    phase_multipliers: dict
    wind_penalty_per_mps: float = 0.0333
    wind_ref_mps: float = 15.0
    wind_penalty_max: float = 0.5
    climb_rate_mps: float = 3.0
    descend_rate_mps: float = 2.0

    def __post_init__(self) -> None:
        # `frozen=True` freezes the FIELDS, not the dict one of them holds, and
        # the shipped profiles are module-level singletons every later
        # `FuelModel()` reads. A caller who mutated `QUAD_SUAS_ELECTRIC
        # .phase_multipliers` would re-price the burn table for the whole
        # process while bypassing every check below — including the one that
        # refuses a CRUISE multiplier other than 1.0. Take a read-only copy so
        # the validation this method performs stays true afterwards.
        #
        # The copy also NORMALIZES the keys to `Phase`. `Phase` is a `str` Enum,
        # so `Phase.CRUISE in {"cruise": 1.0}` is True and a profile written
        # with plain-string keys sailed through every check below — and then
        # `__hash__` and `to_dict`, which both read `p.value`, died on
        # `AttributeError: 'str' object has no attribute 'value'` the first
        # time anything published or cached the profile. Converting here means
        # a key is either a real phase or is refused by name, and the mapping
        # every other method reads has exactly one key type.
        table: dict[Phase, float] = {}
        for key, mult in dict(self.phase_multipliers).items():
            try:
                phase = key if isinstance(key, Phase) else Phase(key)
            except ValueError:
                raise ValueError(
                    f"{self.id}: phase_multipliers has an unknown phase key "
                    f"{key!r}; the phases are {[p.value for p in Phase]}"
                ) from None
            try:
                table[phase] = float(mult)
            except (TypeError, ValueError):
                raise ValueError(
                    f"{self.id}: phase multiplier for {phase.value} must be a "
                    f"number, got {mult!r}") from None
        object.__setattr__(self, "phase_multipliers", MappingProxyType(table))
        # A mis-specified profile is a silently wrong fuel clock; refuse it.
        #
        # EVERY numeric field below is checked for FINITENESS as well as sign,
        # because on this dataclass the infinities are not degenerate inputs —
        # they are the off switch. `endurance_cruise_s=inf` satisfies `> 0`,
        # makes `cruise_rate_pct_per_s` exactly 0.0, and therefore makes EVERY
        # phase burn 0.0 %/s: measured, 3.3 hours of cruise into a 15 m/s
        # headwind burned 0.00% and `BingoLatch` never tripped. NaN is worse
        # still, because `nan > 0.0` and `nan < 0.0` are BOTH False, so a NaN
        # slips past a bare sign test in either direction. A profile that
        # disables the fuel clock is the same defect as a `wind_ref_mps` of
        # zero, one field further up.
        if not (math.isfinite(self.endurance_cruise_s)
                and self.endurance_cruise_s > 0.0):
            raise ValueError(f"{self.id}: endurance_cruise_s must be a finite "
                             f"number > 0, got {self.endurance_cruise_s!r}; an "
                             "infinite or NaN endurance makes every burn rate "
                             "0.0 %/s and BINGO unreachable")
        missing = [p.value for p in Phase if p not in self.phase_multipliers]
        if missing:
            raise ValueError(f"{self.id}: phase_multipliers is missing {missing}")
        if self.phase_multipliers[Phase.CRUISE] != 1.0:
            raise ValueError(f"{self.id}: multipliers are relative to CRUISE, "
                             "so CRUISE must be exactly 1.0")
        bad = [p.value for p, m in self.phase_multipliers.items() if m < 0.0]
        if bad:
            raise ValueError(f"{self.id}: negative phase multiplier for {bad}")
        unreal = [p.value for p, m in self.phase_multipliers.items()
                  if not math.isfinite(m)]
        if unreal:
            raise ValueError(f"{self.id}: phase multiplier must be a finite "
                             f"number for {unreal}")
        # A flight phase that burns NOTHING is the same off switch scoped to
        # one phase: a 0.0 HOVER multiplier gives a loiter infinite endurance,
        # which is not a calibration anyone measures. GROUND is the one
        # exception — a fixed wing with the engine stopped really does burn
        # nothing on the ramp — so it may be 0.0 and nothing else may.
        dead = [p.value for p, m in self.phase_multipliers.items()
                if m == 0.0 and p is not Phase.GROUND]
        if dead:
            raise ValueError(f"{self.id}: a zero phase multiplier makes the "
                             f"fuel clock stop in that phase; {dead} must be "
                             "> 0 (only GROUND may burn nothing)")
        for name, rate in (("climb_rate_mps", self.climb_rate_mps),
                           ("descend_rate_mps", self.descend_rate_mps)):
            if not (math.isfinite(rate) and rate > 0.0):
                raise ValueError(f"{self.id}: vertical rates must be finite "
                                 f"and > 0, got {name}={rate!r}")
        # The M15 headwind fields are part of the same fuel clock and were the
        # half this validator did not cover. Every one of them has a value that
        # silently DISABLES or INVERTS the penalty rather than failing:
        #   wind_ref_mps <= 0   -> min(headwind, ref) is 0, so no headwind ever
        #                          costs anything (M15 becomes dead code, the
        #                          same shape as the bare-except geoid bug);
        #   wind_penalty_max <= 0 -> the min() clamps every penalty back to 0;
        #   wind_penalty_per_mps < 0 -> a headwind burns LESS than calm air, so
        #                          the BINGO line under-prices the return leg in
        #                          exactly the downwind case it exists to catch.
        # None of those is a profile anyone means to write, so refuse them here
        # instead of flying a fuel clock that is quietly wrong.
        # ...and, like the endurance above, each of them has an INFINITE or NaN
        # value that passes a bare sign test while removing the thing it names:
        # `wind_ref_mps=inf` uncaps the reference wind, `wind_penalty_max=inf`
        # removes the ceiling, and a NaN `wind_penalty_per_mps` slips past
        # `< 0.0` entirely and then collapses through `min()` to a CONSTANT
        # maximum penalty that no longer varies with the headwind at all —
        # M15 present, wired, and with no coupling left in it.
        if not (math.isfinite(self.wind_ref_mps) and self.wind_ref_mps > 0.0):
            raise ValueError(
                f"{self.id}: wind_ref_mps must be a finite number > 0 (got "
                f"{self.wind_ref_mps!r}); at or below 0 no headwind is ever "
                "charged and the M15 penalty is dead code, and an infinite or "
                "NaN reference removes the cap it exists to impose")
        if not (math.isfinite(self.wind_penalty_max)
                and self.wind_penalty_max > 0.0):
            raise ValueError(
                f"{self.id}: wind_penalty_max must be a finite number > 0 (got "
                f"{self.wind_penalty_max!r}); at or below 0 every headwind "
                "penalty is clamped away, and an infinite or NaN ceiling "
                "clamps nothing")
        if not (math.isfinite(self.wind_penalty_per_mps)
                and self.wind_penalty_per_mps >= 0.0):
            raise ValueError(
                f"{self.id}: wind_penalty_per_mps must be a finite number >= 0 "
                f"(got {self.wind_penalty_per_mps!r}); a negative rate makes a "
                "headwind cheaper than calm air, and a NaN rate passes every "
                "sign test and then charges a constant maximum penalty that "
                "does not vary with the wind")

    def __hash__(self) -> int:
        """Hash the profile's CONTENT, which `frozen=True` alone could not.

        `@dataclass(frozen=True)` generates a `__hash__` over the tuple of
        fields, and one of those fields is a mapping — so the generated hash
        raised `TypeError: unhashable type: 'dict'` on every profile, the
        shipped singletons included. `frozen=True` was therefore cosmetic in
        both directions: the dict was still mutable (fixed above with a
        `MappingProxyType` copy) *and* the immutability it was supposed to buy
        — usability as a dict key or set member, e.g. caching a burn table per
        profile — was never actually available. `MappingProxyType` is no more
        hashable than the `dict` it wraps, so the mapping is hashed as its
        sorted items here. Equality is unchanged (the generated `__eq__`
        compares the mappings by value), so equal profiles still hash equal.
        """
        return hash((
            self.id, self.summary, self.source, self.endurance_cruise_s,
            tuple(sorted((p.value, float(m))
                         for p, m in self.phase_multipliers.items())),
            self.wind_penalty_per_mps, self.wind_ref_mps,
            self.wind_penalty_max, self.climb_rate_mps, self.descend_rate_mps,
        ))

    @property
    def cruise_rate_pct_per_s(self) -> float:
        """Cruise burn as a fraction of capacity per second."""
        return 100.0 / self.endurance_cruise_s

    def rates_pct_per_s(self) -> dict:
        """The per-phase burn table a :class:`FuelModel` integrates."""
        cruise = self.cruise_rate_pct_per_s
        return {p: cruise * float(m) for p, m in self.phase_multipliers.items()}

    def endurance_s(self, phase: "Phase" = Phase.CRUISE,
                    fuel_pct: float = 100.0) -> float:
        """Seconds `fuel_pct` of tank lasts if flown entirely in `phase`."""
        rate = self.rates_pct_per_s()[phase]
        return float("inf") if rate <= 0.0 else fuel_pct / rate

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "summary": self.summary,
            "source": self.source,
            "endurance_cruise_s": self.endurance_cruise_s,
            "endurance_cruise_min": round(self.endurance_cruise_s / 60.0, 1),
            "endurance_hover_min": round(self.endurance_s(Phase.HOVER) / 60.0, 1),
            "phase_multipliers": {p.value: m for p, m in self.phase_multipliers.items()},
            "cruise_pct_per_s": round(self.cruise_rate_pct_per_s, 6),
            "wind_penalty_per_mps": self.wind_penalty_per_mps,
            "wind_ref_mps": self.wind_ref_mps,
            "wind_penalty_max": self.wind_penalty_max,
            "climb_rate_mps": self.climb_rate_mps,
            "descend_rate_mps": self.descend_rate_mps,
        }


#: Default. The airframe AirSim actually simulates: a Group-1/2 electric
#: multirotor sUAS with an EO/IR gimbal, flown here at 8-15 m/s (20 m/s cap).
#:
#: 35 min cruise endurance is the mid-point of the published band for that
#: class. Multipliers, and where each comes from:
#:   HOVER   1.17 — hover endurance ~30 min against 35 min cruise: a
#:                  multirotor's induced power falls with translational lift,
#:                  so best-endurance cruise draws ~0.85x hover power.
#:   CLIMB   1.50 — hover power plus m*g*V_climb/eta. For a ~6 kg airframe at
#:                  3 m/s and eta~0.7 that is ~250 W on ~765 W of cruise power.
#:   DESCEND 0.80 — a controlled 2 m/s descent recovers part of the induced
#:                  power but must stay out of vortex-ring state, so it is not
#:                  free: ~0.75x hover.
#:   GROUND  0.05 — rotors stopped: avionics, datalink and the gimbal only
#:                  (~30-40 W against ~765 W in cruise).
QUAD_SUAS_ELECTRIC = Airframe(
    id="quad_suas_electric",
    summary=("Group-1/2 electric multirotor sUAS with an EO/IR gimbal "
             "(the class AirSim's SimpleFlight multirotor models); "
             "35 min cruise endurance, ~30 min hover."),
    source=("Published manufacturer endurance for the class: Skydio X10 "
            "~40 min, DJI Matrice 30T 41 min max / 36 min hover, Parrot "
            "ANAFI USA 32 min, Teal Golden Eagle ~30 min. Calibrate against "
            "your own flight-test data before flying a real airframe."),
    endurance_cruise_s=35.0 * 60.0,
    phase_multipliers={
        Phase.GROUND: 0.05,
        Phase.CLIMB: 1.50,
        Phase.CRUISE: 1.0,
        Phase.DESCEND: 0.80,
        Phase.HOVER: 1.17,
    },
    climb_rate_mps=3.0,
    descend_rate_mps=2.0,
)

#: A Group-3 catapult-launched fixed-wing ISR UAV (ScanEagle-class). Kept so a
#: theatre-scale sortie a multirotor cannot fly is still expressible: the
#: pre-flight gate then passes a 100+ km plan instead of correctly refusing it
#: for the quad. A fixed wing loiters more efficiently than it cruises, glides
#: down at idle, and burns nothing on the ground with the engine stopped.
GROUP3_FIXED_WING = Airframe(
    id="group3_fixed_wing",
    summary=("Group-3 catapult-launched fixed-wing ISR UAV (ScanEagle-class); "
             "14 h endurance for theatre-scale sorties."),
    source=("Insitu ScanEagle-class published endurance (>14 h). Loiter is "
            "flown at best-endurance speed, below cruise power."),
    endurance_cruise_s=14.0 * 3600.0,
    phase_multipliers={
        Phase.GROUND: 0.02,
        Phase.CLIMB: 1.60,
        Phase.CRUISE: 1.0,
        Phase.DESCEND: 0.50,
        Phase.HOVER: 0.90,  # an orbit at loiter speed, not a true hover
    },
    climb_rate_mps=2.5,
    descend_rate_mps=3.0,
)

AIRFRAMES: dict[str, Airframe] = {
    a.id: a for a in (QUAD_SUAS_ELECTRIC, GROUP3_FIXED_WING)
}
DEFAULT_AIRFRAME_ID = QUAD_SUAS_ELECTRIC.id
#: Process-level override for code that constructs `FuelModel()` itself
#: (server.py does). Unknown ids raise — never a silent fallback.
AIRFRAME_ENV_VAR = "GODSEYE_AIRFRAME"


def get_airframe(airframe: "Airframe | str | None") -> Airframe:
    """Resolve an :class:`Airframe`, an id, or None (= the default profile).

    An unknown id raises with the known ids listed. A fuel clock that quietly
    fell back to another airframe would be a wrong BINGO line stated with full
    confidence, which is the failure mode this whole module exists to avoid.
    """
    if airframe is None:
        return default_airframe()
    if isinstance(airframe, Airframe):
        return airframe
    try:
        return AIRFRAMES[str(airframe)]
    except KeyError:
        raise ValueError(
            f"unknown airframe {airframe!r}; known profiles: "
            f"{sorted(AIRFRAMES)}") from None


def default_airframe() -> Airframe:
    """The profile a bare `FuelModel()` gets, honouring `GODSEYE_AIRFRAME`."""
    override = os.environ.get(AIRFRAME_ENV_VAR)
    if override:
        return get_airframe(override.strip())
    return AIRFRAMES[DEFAULT_AIRFRAME_ID]


# Legacy aliases: the DEFAULT profile's numbers, kept because they were the
# module's public spelling. The live values always come from the airframe a
# FuelModel was built with (`fm.airframe`), never from these.
ENDURANCE_CRUISE_S = QUAD_SUAS_ELECTRIC.endurance_cruise_s
DEFAULT_RATES_PCT_PER_S = QUAD_SUAS_ELECTRIC.rates_pct_per_s()
WIND_PENALTY_PER_MPS = QUAD_SUAS_ELECTRIC.wind_penalty_per_mps
WIND_REF_MPS = QUAD_SUAS_ELECTRIC.wind_ref_mps
CLIMB_RATE_MPS = QUAD_SUAS_ELECTRIC.climb_rate_mps
DESCEND_RATE_MPS = QUAD_SUAS_ELECTRIC.descend_rate_mps

#: Which envelope finding commits the vehicle to what (PLAN §4.5). Published
#: on `uav://safety/geofence`; `SafetyMonitor.tick` implements exactly this.
BREACH_DOCTRINE = {
    "geofence": "force_rtb",
    "ceiling": "alarm_only",
    "max_speed": "alarm_only",
    "min_agl": "alarm_only",
    "geofence_proximity": "warning",
    "bingo": "force_rtb_uncancellable",
}

#: What a finding with NO entry in :data:`BREACH_DOCTRINE` is published as.
#:
#: The lookup used to default to `"alarm_only"`, which is not a neutral value:
#: it is the doctrine that explicitly means "seen, and deliberately does not
#: commit the vehicle". Publishing it for a kind nobody classified states a
#: safety decision that was never made — a `.get()` default answering a
#: question about what the aircraft does next. `undeclared` cannot be misread
#: as a decision, and `SafetyMonitor.tick` reports it separately as well.
UNDECLARED_DOCTRINE = "undeclared"

#: Every `Violation.kind` `SafetyEnvelope` can emit. `BREACH_DOCTRINE` must
#: cover all of them; the guard below fails at IMPORT if a new kind is added
#: without deciding what it commits the vehicle to, so `UNDECLARED_DOCTRINE`
#: stays reachable only for a kind some other layer invented.
ENVELOPE_VIOLATION_KINDS = frozenset({
    "geofence", "geofence_proximity", "ceiling", "min_agl", "max_speed",
})

_undeclared = sorted(ENVELOPE_VIOLATION_KINDS - set(BREACH_DOCTRINE))
if _undeclared:  # pragma: no cover - a guard against a future edit
    raise RuntimeError(
        f"BREACH_DOCTRINE has no entry for envelope violation(s) "
        f"{_undeclared}: decide whether each one commits the vehicle before "
        "the tick loop has to publish an answer")
del _undeclared


def doctrine_for(kind: str) -> str:
    """The published doctrine for one finding, never guessed (§4.5).

    Returns :data:`UNDECLARED_DOCTRINE` for a kind that has none, rather than
    the permissive `alarm_only` the old `.get()` default supplied.
    """
    return BREACH_DOCTRINE.get(kind, UNDECLARED_DOCTRINE)


def _to_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    lat_r, lat0_r = math.radians(lat), math.radians(lat0)
    x = math.radians(lon - lon0) * EARTH_RADIUS_M * math.cos((lat_r + lat0_r) / 2)
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing 1->2 in degrees true (0 = north)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return math.degrees(math.atan2(y, x)) % 360.0


def track_deg_from_velocity(vx_mps: float, vy_mps: float) -> float | None:
    """Ground track (deg true) from NED horizontal velocity, None if hovering."""
    if math.hypot(vx_mps, vy_mps) < 0.5:
        return None
    return math.degrees(math.atan2(vy_mps, vx_mps)) % 360.0


def headwind_component_mps(wind_n: float, wind_e: float, track_deg: float | None) -> float:
    """Headwind component of an NED wind vector along a ground track (M15).

    `wind_n/wind_e` are the air-mass velocity in NED m/s — the convention the
    sim uses, where an airborne vehicle drifts *with* the vector. `track_deg`
    is the vehicle's ground track (0 = north, 90 = east); None (hovering, no
    track) yields 0. Returns +ve for a headwind, -ve for a tailwind.
    PLAN §4.4: the wind vector "feeds fuel model, M15".
    """
    if track_deg is None:
        return 0.0
    t = math.radians(track_deg)
    along = wind_n * math.cos(t) + wind_e * math.sin(t)
    return -along if along else 0.0  # never hand the journal a "-0.0"


def _on_segment(px: float, py: float, ax: float, ay: float,
                bx: float, by: float, tol_m: float = 0.5) -> bool:
    """True if P lies on segment AB within tol_m perpendicular metres.

    Coordinates are local-XY metres, so the cross product magnitude scales
    with segment length x perpendicular distance; divide by length to get a
    true metric distance and compare against a real tolerance.
    """
    dx, dy = bx - ax, by - ay
    seg_len = math.hypot(dx, dy)
    if seg_len < 1e-9:
        return math.hypot(px - ax, py - ay) <= tol_m
    dist = abs(dx * (py - ay) - dy * (px - ax)) / seg_len
    if dist > tol_m:
        return False
    dot = (px - ax) * (px - bx) + (py - ay) * (py - by)
    return dot <= 0.0


def point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray-cast in local equirectangular XY. polygon = [(lat, lon), ...].

    Boundary points (on an edge or vertex) count as INSIDE so mission plans
    that trace the AO boundary are not spuriously rejected by the geofence.
    """
    if len(polygon) < 3:
        return True  # no geofence configured = unconstrained
    lat0, lon0 = polygon[0]
    px, py = _to_xy(lat, lon, lat0, lon0)
    inside = False
    n = len(polygon)
    for i in range(n):
        ax, ay = _to_xy(*polygon[i], lat0, lon0)
        bx, by = _to_xy(*polygon[(i + 1) % n], lat0, lon0)
        if _on_segment(px, py, ax, ay, bx, by):
            return True  # boundary counts as inside
        if (ay > py) != (by > py):
            x_cross = ax + (py - ay) * (bx - ax) / (by - ay)
            if px < x_cross:
                inside = not inside
    return inside


def distance_to_polygon_edge_m(lat: float, lon: float, polygon: list[tuple[float, float]]) -> float:
    """Approximate min distance to any polygon edge (metres)."""
    if len(polygon) < 3:
        return float("inf")
    lat0, lon0 = polygon[0]
    px, py = _to_xy(lat, lon, lat0, lon0)
    best = float("inf")
    n = len(polygon)
    for i in range(n):
        ax, ay = _to_xy(*polygon[i], lat0, lon0)
        bx, by = _to_xy(*polygon[(i + 1) % n], lat0, lon0)
        dx, dy = bx - ax, by - ay
        seg_len2 = dx * dx + dy * dy
        if seg_len2 == 0:
            d = math.hypot(px - ax, py - ay)
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        best = min(best, d)
    return best


@dataclass
class Violation:
    """One structured envelope finding (M4, PLAN §3.1 alarm types).

    `kind` is stable and machine-readable; `severity` separates a *breach*
    (outside the envelope now) from a *warning* (geofence proximity, i.e.
    still inside but within the configured margin).
    """

    kind: str
    severity: str  # "breach" | "warning"
    message: str
    value: float = 0.0
    limit: float = 0.0

    @property
    def is_breach(self) -> bool:
        return self.severity == "breach"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "value": round(self.value, 3),
            "limit": round(self.limit, 3),
        }

    def __str__(self) -> str:  # legacy string form used by check_point/check_route
        return self.message


@dataclass
class SafetyEnvelope:
    """Configured limits for one AO. Defaults are permissive-but-sane.

    Altitudes are height above the launch datum ("AGL" in a flat-world sim;
    terrain relief is Phase 6 / M19), speeds are ground speed in m/s.
    """

    geofence: list[tuple[float, float]] = field(default_factory=list)
    ceiling_m_agl: float = 120.0
    max_speed_mps: float = 20.0
    min_agl_m: float = 3.0
    home: tuple[float, float, float] | None = None  # lat, lon, alt_msl
    # Inside-the-fence distance at which proximity is *warned* (PLAN §3.1
    # "geofence proximity" alarm), distinct from a breach.
    geofence_warn_m: float = 100.0

    def check_point(self, lat: float, lon: float, alt_agl: float) -> list[str]:
        """Return list of violations (empty = inside envelope). Plan-time."""
        violations: list[str] = []
        if self.geofence and not point_in_polygon(lat, lon, self.geofence):
            violations.append("geofence")
        if alt_agl > self.ceiling_m_agl:
            violations.append(f"ceiling({alt_agl:.0f}m>{self.ceiling_m_agl:.0f}m)")
        if 0 < alt_agl < self.min_agl_m:
            violations.append(f"min_agl({alt_agl:.1f}m<{self.min_agl_m:.0f}m)")
        return violations

    #: Waypoint altitude spellings, contract spelling FIRST (TOOL_CONTRACT
    #: "never a bare alt_m"). The older spellings stay readable so existing
    #: callers keep working, but `alt_agl_m` is what every tool description
    #: tells a harness to send.
    WAYPOINT_ALT_KEYS = ("alt_agl_m", "alt_agl", "alt_m")

    def check_route(self, waypoints: list[dict]) -> list[str]:
        """Plan-time envelope check over a route (M4).

        A waypoint carrying NO recognised altitude is a violation, never a
        silent 0. The previous version read only ``alt_agl``/``alt_m`` and
        defaulted to 0, so a route written to the documented contract -
        ``{lat, lon, alt_agl_m}`` - had every altitude read as zero and sailed
        through both the ceiling and the min-AGL floor. That is a safety gate
        that passes everything, which is worse than no gate at all because it
        reports "ok".
        """
        violations: list[str] = []
        for i, wp in enumerate(waypoints):
            alt = next((wp[k] for k in self.WAYPOINT_ALT_KEYS if wp.get(k) is not None), None)
            if alt is None:
                violations.append(
                    f"wp{i}:no_altitude(expected one of {'/'.join(self.WAYPOINT_ALT_KEYS)})")
                continue
            v = self.check_point(float(wp["lat"]), float(wp["lon"]), float(alt))
            if v:
                violations.append(f"wp{i}:{','.join(v)}")
        return violations

    def check_speed(self, speed_mps: float) -> list[str]:
        """Plan-time speed limit check (M4: "ceiling / max speed / min AGL")."""
        if speed_mps > self.max_speed_mps:
            return [f"max_speed({speed_mps:.1f}>{self.max_speed_mps:.0f}mps)"]
        return []

    def geofence_margin_m(self, lat: float, lon: float) -> float:
        if not self.geofence:
            return float("inf")
        d = distance_to_polygon_edge_m(lat, lon, self.geofence)
        return d if point_in_polygon(lat, lon, self.geofence) else -d

    # ---- in-flight (per-tick) checks -----------------------------------
    def check_position(self, lat: float, lon: float, alt_agl_m: float,
                       *, landed: bool = False) -> list[Violation]:
        """Per-tick position check: geofence breach, proximity, ceiling, AGL.

        A drone drifting out of the AO on wind (M15) is caught here; the
        proximity warning fires while still inside, at `geofence_warn_m`.
        """
        out: list[Violation] = []
        if self.geofence:
            margin = self.geofence_margin_m(lat, lon)
            if margin < 0.0:
                out.append(Violation("geofence", "breach", "geofence", margin, 0.0))
            elif margin <= self.geofence_warn_m:
                out.append(Violation(
                    "geofence_proximity", "warning",
                    f"geofence_proximity({margin:.0f}m<{self.geofence_warn_m:.0f}m)",
                    margin, self.geofence_warn_m,
                ))
        if landed:
            return out  # a vehicle on the ground owes no altitude limits
        if alt_agl_m > self.ceiling_m_agl:
            out.append(Violation(
                "ceiling", "breach",
                f"ceiling({alt_agl_m:.0f}m>{self.ceiling_m_agl:.0f}m)",
                alt_agl_m, self.ceiling_m_agl,
            ))
        elif alt_agl_m < self.min_agl_m:
            # In flight, 0 m and below are violations too (the plan-time
            # check tolerates 0 because a waypoint may omit its altitude).
            out.append(Violation(
                "min_agl", "breach",
                f"min_agl({alt_agl_m:.1f}m<{self.min_agl_m:.0f}m)",
                alt_agl_m, self.min_agl_m,
            ))
        return out

    def check_state(self, lat: float, lon: float, alt_agl_m: float,
                    *, speed_mps: float | None = None,
                    landed: bool = False) -> list[Violation]:
        """Full per-tick envelope check: position limits plus ground speed."""
        out = self.check_position(lat, lon, alt_agl_m, landed=landed)
        if speed_mps is not None and not landed and speed_mps > self.max_speed_mps:
            out.append(Violation(
                "max_speed", "breach",
                f"max_speed({speed_mps:.1f}>{self.max_speed_mps:.0f}mps)",
                speed_mps, self.max_speed_mps,
            ))
        return out

    def to_dict(self) -> dict:
        """Serializable envelope (uav://safety/geofence, §4.8)."""
        return {
            "geofence": [list(p) for p in self.geofence],
            "ceiling_m_agl": self.ceiling_m_agl,
            "max_speed_mps": self.max_speed_mps,
            "min_agl_m": self.min_agl_m,
            "geofence_warn_m": self.geofence_warn_m,
            "home": list(self.home) if self.home else None,
            # What each finding actually COMMITS the vehicle to. A skill whose
            # ROE may only be stricter has to be able to read this, not infer
            # it from a field called `breach_forces_rtb` (§4.5).
            "breach_doctrine": dict(BREACH_DOCTRINE),
        }


@dataclass
class BingoLatch:
    """Un-cancellable BINGO latch (M4/T5).

    Once tripped the vehicle is committed to RTB. `clear()` refuses harness
    commands (`mission_cancel`, `uav_abort`) — only an explicit operator
    override may reset it, and every refused attempt is counted so the
    audit log can show the harness tried.
    """

    tripped: bool = False
    tripped_at: float | None = None
    fuel_pct_at_trip: float | None = None
    bingo_pct_at_trip: float | None = None
    position: tuple[float, float] | None = None
    clear_attempts: int = 0

    def trip(self, *, fuel_pct: float, bingo_pct: float,
             at: tuple[float, float] | None = None, now: float | None = None) -> bool:
        """Latch BINGO. Returns True only on the 0->1 edge (event to log)."""
        if self.tripped:
            return False
        self.tripped = True
        self.tripped_at = time.monotonic() if now is None else float(now)
        self.fuel_pct_at_trip = float(fuel_pct)
        self.bingo_pct_at_trip = float(bingo_pct)
        self.position = tuple(at) if at is not None else None  # type: ignore[assignment]
        return True

    def clear(self, *, operator_override: bool = False) -> bool:
        """Clear the latch. Harness commands can never clear it (T5)."""
        if not operator_override:
            self.clear_attempts += 1
            return False
        self.tripped = False
        self.tripped_at = None
        self.fuel_pct_at_trip = None
        self.bingo_pct_at_trip = None
        self.position = None
        return True

    def to_dict(self) -> dict:
        return {
            "tripped": self.tripped,
            "tripped_at": self.tripped_at,
            "fuel_pct_at_trip": self.fuel_pct_at_trip,
            "bingo_pct_at_trip": self.bingo_pct_at_trip,
            "position": list(self.position) if self.position else None,
            "clear_attempts": self.clear_attempts,
        }


@dataclass
class FuelModel:
    """Tick-reconciled fuel integrator (T5). Capacity is normalized to 100%.

    The burn rates come from an :class:`Airframe` profile — `airframe=` takes
    a profile, its id, or None for :func:`default_airframe` (honouring
    ``GODSEYE_AIRFRAME``). `capacity_s_cruise` and `rates` are derived from it
    unless a caller passes them explicitly, so "what this vehicle is" is one
    decision made in one place instead of four magic numbers (M4/T5).
    """

    #: `Airframe`, an id in :data:`AIRFRAMES`, or None for the default.
    airframe: "Airframe | str | None" = None
    capacity_s_cruise: float | None = None  # cruise endurance (s)
    fuel_pct: float = 100.0
    rates: dict | None = None
    home: tuple[float, float, float] | None = None  # lat, lon, alt (M4 return leg)
    # Integrator bookkeeping — the fuel-state serialization hook (§4.5 JSONL).
    burned_pct: float = 0.0
    elapsed_s: float = 0.0
    ticks: int = 0
    last_phase: Phase = Phase.GROUND
    last_headwind_mps: float = 0.0
    # Speed the RTB leg is actually flown at (server's uav_return_to_home
    # defaults to 10 m/s). Both the BINGO line and the pre-flight gate's
    # return leg use it, so the two agree instead of one hard-coding 10.0
    # while the other silently used the mission speed (T5).
    rtb_speed_mps: float = 10.0
    # Burn the MAX_TICK_DT_S clamp dropped (T5). A slow or stalled tick loop
    # under-charges fuel; counting it here makes that visible in the journal
    # instead of silently losing the burn.
    clamped_ticks: int = 0
    unaccounted_s: float = 0.0
    bingo: BingoLatch = field(default_factory=BingoLatch)
    _last_ts: float | None = None
    _last_alt: float | None = None

    def __post_init__(self) -> None:
        self.airframe = get_airframe(self.airframe)
        if self.rates is None:
            self.rates = self.airframe.rates_pct_per_s()
        else:
            missing = [p.value for p in Phase if p not in self.rates]
            if missing:  # a missing phase is a KeyError mid-flight, not here
                raise ValueError(f"fuel rates are missing phases {missing}")
            # `rates=` BYPASSES the whole `Airframe` validator, so every value
            # it refuses has to be refused again here or the bypass is the way
            # in. A non-finite rate poisons the tank (`max(0.0, nan)` is 0.0,
            # so the first tick empties it) and a zero rate on a FLIGHT phase
            # stops the clock in that phase exactly as an infinite endurance
            # stops it everywhere: `endurance_s` returns `inf`, `_burn`
            # returns 0.0, and BINGO is unreachable while `fuel_pct` sits at
            # 100. GROUND may be zero, for the same reason it may be zero on a
            # profile — an engine stopped on the ramp burns nothing.
            unusable = [p.value for p in Phase
                        if not math.isfinite(self.rates[p])
                        or self.rates[p] < 0.0
                        or (self.rates[p] == 0.0 and p is not Phase.GROUND)]
            if unusable:
                raise ValueError(
                    f"fuel rates must be finite and > 0 for every flight "
                    f"phase (GROUND may be 0); {unusable} would stop the fuel "
                    "clock and make BINGO unreachable")
        if self.capacity_s_cruise is None:
            self.capacity_s_cruise = self.airframe.endurance_cruise_s

    def endurance_s(self, phase: Phase = Phase.CRUISE) -> float:
        """Seconds the CURRENT fuel lasts if the rest is flown in `phase`.

        The number `uav_get_telemetry` publishes as `est_endurance_s` is this
        one measured from the BINGO line rather than from empty; both read the
        same rate table, so they cannot drift apart (T5).
        """
        rate = self.rates[phase]
        return float("inf") if rate <= 0.0 else self.fuel_pct / rate

    def classify_phase(self, speed_mps: float, vz_mps: float, landed: bool) -> Phase:
        if landed:
            return Phase.GROUND
        if vz_mps < -0.8:
            return Phase.CLIMB
        if vz_mps > 0.8:
            return Phase.DESCEND
        if speed_mps < 1.0:
            return Phase.HOVER
        return Phase.CRUISE

    def _burn(self, phase: Phase, dt_s: float, headwind_mps: float = 0.0) -> float:
        """Burn for `dt_s` in `phase`, with the M15 headwind penalty applied.

        The penalty constants belong to the airframe profile, not to the
        module: a fixed wing and a multirotor do not pay the same price for
        the same headwind.
        """
        rate = self.rates[phase]
        af = self.airframe
        if phase in WIND_SENSITIVE_PHASES and headwind_mps > 0:
            rate *= 1.0 + min(af.wind_penalty_max,
                              af.wind_penalty_per_mps * min(headwind_mps,
                                                            af.wind_ref_mps))
        return rate * dt_s

    def tick(self, speed_mps: float, vz_mps: float, landed: bool,
             headwind_mps: float = 0.0, now: float | None = None,
             *, wind_ne: tuple[float, float] | None = None,
             track_deg: float | None = None) -> float:
        """Reconcile one telemetry tick. Returns current fuel_pct (T5).

        Robust for a real tick loop: the first call only primes the clock,
        repeated/out-of-order timestamps burn nothing and never rewind the
        clock, a clock jump is clamped to MAX_TICK_DT_S, and fuel never goes
        negative. Supply `wind_ne` + `track_deg` to couple wind into the burn
        (M15) instead of pre-resolving `headwind_mps` yourself.
        """
        now = time.monotonic() if now is None else float(now)
        if wind_ne is not None:
            headwind_mps = headwind_component_mps(wind_ne[0], wind_ne[1], track_deg)
        phase = self.classify_phase(speed_mps, vz_mps, landed)
        if self._last_ts is None:
            # First call: no dt exists yet, so nothing can have burned.
            self._last_ts = now
            self.last_phase = phase
            self.last_headwind_mps = headwind_mps
            return self.fuel_pct
        dt = now - self._last_ts
        if dt <= 0.0:
            # Repeated or out-of-order sample: never double-charge, and never
            # move the clock backwards (the next in-order tick would then
            # charge the same interval twice).
            self.last_phase = phase
            self.last_headwind_mps = headwind_mps
            return self.fuel_pct
        if dt > MAX_TICK_DT_S:  # suspended process / wall-clock jump / slow loop
            self.clamped_ticks += 1
            self.unaccounted_s += dt - MAX_TICK_DT_S
            dt = MAX_TICK_DT_S
        before = self.fuel_pct
        self.fuel_pct = max(0.0, before - self._burn(phase, dt, headwind_mps))
        self.burned_pct += before - self.fuel_pct
        self.elapsed_s += dt
        self.ticks += 1
        self._last_ts = now
        self.last_phase = phase
        self.last_headwind_mps = headwind_mps
        return self.fuel_pct

    def estimate_route(self, waypoints: list[dict], start: tuple[float, float],
                       speed_mps: float, headwind_mps: float = 0.0,
                       *, start_alt_m: float = 0.0,
                       wind_ne: tuple[float, float] | None = None) -> dict:
        """Dry-run the SAME integrator over a waypoint plan (M4 pre-flight).

        waypoints: [{lat, lon, alt_m}] (alt_m = target AGL/alt for the leg).
        `wind_ne` resolves the headwind per leg from the vehicle's ground
        track (M15); a scalar `headwind_mps` applies to every leg.
        Returns {fuel_pct, time_s, distance_m, end_alt_m, legs}. `end_alt_m`
        is where the plan leaves the vehicle — the altitude a return leg must
        let down from, so the caller cannot re-derive it and get it wrong (T5).
        """
        total_fuel = 0.0
        total_time = 0.0
        total_dist = 0.0
        lat, lon = start
        alt = float(start_alt_m)
        legs = []
        for wp in waypoints:
            tlat, tlon = float(wp["lat"]), float(wp["lon"])
            talt = float(wp.get("alt_m", alt))
            dist = haversine_m(lat, lon, tlat, tlon)
            if wind_ne is None:
                hw = headwind_mps
            else:
                track = bearing_deg(lat, lon, tlat, tlon) if dist > 1.0 else None
                hw = headwind_component_mps(wind_ne[0], wind_ne[1], track)
            climb = max(0.0, talt - alt)
            descend = max(0.0, alt - talt)
            # Vertical legs at the AIRFRAME's climb/descent rates, so the
            # dry run prices the let-down the vehicle will actually fly (T5).
            t_climb = climb / self.airframe.climb_rate_mps
            t_desc = descend / self.airframe.descend_rate_mps
            t_cruise = dist / max(0.5, speed_mps)
            fuel = (
                self._burn(Phase.CLIMB, t_climb, hw)
                + self._burn(Phase.DESCEND, t_desc)
                + self._burn(Phase.CRUISE, t_cruise, hw)
            )
            total_fuel += fuel
            total_time += t_climb + t_desc + t_cruise
            total_dist += dist
            legs.append({"to": [tlat, tlon], "distance_m": dist, "fuel_pct": fuel,
                         "headwind_mps": round(hw, 2)})
            lat, lon, alt = tlat, tlon, talt
        return {"fuel_pct": total_fuel, "time_s": total_time, "distance_m": total_dist,
                "end_alt_m": alt, "legs": legs}

    def bingo_fuel_pct(self, at: tuple[float, float], alt_m: float,
                       headwind_mps: float = 0.0,
                       *, wind_ne: tuple[float, float] | None = None,
                       speed_mps: float | None = None) -> float:
        """Fuel level at which the vehicle must RTB now: return leg + reserve.

        Runs the same integrator as the pre-flight gate (T5), descending from
        `alt_m`, so the abort line includes the let-down from cruise height.

        `alt_m` is height above the launch datum (AGL), the same datum as
        `SafetyEnvelope.ceiling_m_agl` and a waypoint's `alt_m`: the return leg
        lets down to 0 over home. Handing it an MSL/HAE altitude inflates the
        let-down burn by the elevation of home (T1 datum contract).
        `speed_mps` defaults to `self.rtb_speed_mps` — the speed the RTB leg is
        flown at — rather than a hard-coded constant.
        """
        if self.home is None:
            return RESERVE_PCT
        est = self.estimate_route(
            [{"lat": self.home[0], "lon": self.home[1], "alt_m": 0.0}],
            at, self.rtb_speed_mps if speed_mps is None else speed_mps,
            headwind_mps, start_alt_m=alt_m, wind_ne=wind_ne,
        )
        return est["fuel_pct"] + RESERVE_PCT

    def check_bingo(self, at: tuple[float, float], alt_m: float,
                    *, headwind_mps: float = 0.0,
                    wind_ne: tuple[float, float] | None = None,
                    speed_mps: float | None = None,
                    now: float | None = None) -> dict:
        """Per-tick BINGO evaluation (M4). Latches force-RTB; never un-latches.

        `tripped_now` is the 0->1 edge — the moment the tasking layer must
        pre-empt the queue, submit the un-cancellable RTB and flag the
        mission MISSION_INCOMPLETE_FUEL.
        """
        line = self.bingo_fuel_pct(at, alt_m, headwind_mps, wind_ne=wind_ne,
                                   speed_mps=speed_mps)
        below = self.fuel_pct <= line
        tripped_now = False
        if below:
            tripped_now = self.bingo.trip(fuel_pct=self.fuel_pct, bingo_pct=line,
                                          at=at, now=now)
        return {
            "fuel_pct": round(self.fuel_pct, 3),
            "bingo_fuel_pct": round(line, 3),
            "margin_pct": round(self.fuel_pct - line, 3),
            "below_bingo": below,
            "tripped_now": tripped_now,
            "latched": self.bingo.tripped,
            "force_rtb": self.bingo.tripped,
            "mission_status": MISSION_INCOMPLETE_FUEL if self.bingo.tripped else None,
        }

    def preflight_gate(self, waypoints: list[dict], start: tuple[float, float],
                       speed_mps: float, headwind_mps: float = 0.0,
                       *, start_alt_m: float = 0.0,
                       wind_ne: tuple[float, float] | None = None) -> dict:
        """M4 gate: plan fuel + return-from-final-wp + reserve <= current fuel.

        Uses `estimate_route` — the same integrator the live tick uses (T5),
        including the let-down from the altitude the plan actually ends at.
        The return leg is flown at `self.rtb_speed_mps`, the same speed the
        in-flight BINGO line assumes, so the gate and the abort line agree.
        """
        plan = self.estimate_route(waypoints, start, speed_mps, headwind_mps,
                                   start_alt_m=start_alt_m, wind_ne=wind_ne)
        warnings: list[str] = []
        if waypoints:
            last = waypoints[-1]
            # Where the plan leaves the vehicle, carried out of the integrator
            # itself. Re-deriving it from waypoints[-1] silently fell back to
            # the *start* altitude whenever the final waypoint omitted alt_m,
            # so the gate charged a let-down the flight never flies (T5).
            last_alt = float(plan["end_alt_m"])
            ret = self.estimate_route(
                [{"lat": self.home[0], "lon": self.home[1], "alt_m": 0.0}] if self.home else [],
                (float(last["lat"]), float(last["lon"])),
                self.rtb_speed_mps,
                headwind_mps,
                start_alt_m=last_alt,
                wind_ne=wind_ne,
            )
        else:
            ret = {"fuel_pct": 0.0}
        if self.home is None:
            warnings.append("no_home_configured: return leg omitted from the M4 gate")
        required = plan["fuel_pct"] + ret["fuel_pct"] + RESERVE_PCT
        ok = required <= self.fuel_pct and not self.bingo.tripped
        if self.bingo.tripped:
            warnings.append("bingo_latched: vehicle is committed to RTB (M4)")
        return {
            "ok": ok,
            "plan_fuel_pct": round(plan["fuel_pct"], 2),
            "return_fuel_pct": round(ret["fuel_pct"], 2),
            "reserve_pct": RESERVE_PCT,
            "required_pct": round(required, 2),
            "available_pct": round(self.fuel_pct, 2),
            "est_time_s": round(plan["time_s"], 1),
            "est_distance_m": round(plan["distance_m"], 1),
            "home_configured": self.home is not None,
            "bingo_latched": self.bingo.tripped,
            "warnings": warnings,
        }

    # ---- serialization hooks (PLAN §4.5 "persist (JSONL)", §4.8 replay) ----
    def fuel_record(self, **fields) -> dict:
        """One JSONL-ready fuel integral row for Store.log_fuel (T5/T4c).

        Caller supplies the vehicle: `store.log_fuel(v, **fm.fuel_record())`
        expects `fuel_pct` and `phase`, which are both included here.
        """
        return {
            "fuel_pct": round(self.fuel_pct, 3),
            "phase": self.last_phase.value,
            # Which energy model produced this row. A journal replayed against
            # a different airframe would silently re-price every burn.
            "airframe": self.airframe.id,
            "burned_pct": round(self.burned_pct, 3),
            "elapsed_s": round(self.elapsed_s, 2),
            "ticks": self.ticks,
            "headwind_mps": round(self.last_headwind_mps, 2),
            "bingo_latched": self.bingo.tripped,
            # Non-zero means the tick loop was slower than MAX_TICK_DT_S and
            # this much burn was dropped on the floor — a silent under-read of
            # fuel, so the journal has to carry it (T5).
            "clamped_ticks": self.clamped_ticks,
            "unaccounted_s": round(self.unaccounted_s, 2),
            **fields,
        }

    def to_dict(self) -> dict:
        """Full integrator state, round-trippable by `from_dict` (T4c replay)."""
        return {
            "airframe": self.airframe.id,
            "airframe_profile": self.airframe.to_dict(),
            "capacity_s_cruise": self.capacity_s_cruise,
            "fuel_pct": self.fuel_pct,
            "rates": {p.value: r for p, r in self.rates.items()},
            "home": list(self.home) if self.home else None,
            "burned_pct": self.burned_pct,
            "elapsed_s": self.elapsed_s,
            "ticks": self.ticks,
            "last_phase": self.last_phase.value,
            "last_headwind_mps": self.last_headwind_mps,
            "rtb_speed_mps": self.rtb_speed_mps,
            "clamped_ticks": self.clamped_ticks,
            "unaccounted_s": self.unaccounted_s,
            "bingo": self.bingo.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FuelModel":
        """Restore a persisted integrator (restart replay, PLAN §4.8).

        The airframe id travels with the state: replaying a journal under a
        different energy model would re-price every burn in it. A record
        written before airframes existed carries no id and restores on this
        process's default profile — but its own `rates` table, if present,
        still wins, so the recovered burn rate is the one that was flown.
        """
        fm = cls(
            airframe=d.get("airframe"),
            capacity_s_cruise=(float(d["capacity_s_cruise"])
                               if d.get("capacity_s_cruise") is not None else None),
            fuel_pct=float(d.get("fuel_pct", 100.0)),
            home=tuple(d["home"]) if d.get("home") else None,  # type: ignore[arg-type]
            burned_pct=float(d.get("burned_pct", 0.0)),
            elapsed_s=float(d.get("elapsed_s", 0.0)),
            ticks=int(d.get("ticks", 0)),
            last_phase=Phase(d.get("last_phase", Phase.GROUND.value)),
            last_headwind_mps=float(d.get("last_headwind_mps", 0.0)),
            rtb_speed_mps=float(d.get("rtb_speed_mps", 10.0)),
            clamped_ticks=int(d.get("clamped_ticks", 0)),
            unaccounted_s=float(d.get("unaccounted_s", 0.0)),
        )
        if d.get("rates"):
            fm.rates = {Phase(k): float(v) for k, v in d["rates"].items()}
        b = d.get("bingo") or {}
        fm.bingo = BingoLatch(
            tripped=bool(b.get("tripped", False)),
            tripped_at=b.get("tripped_at"),
            fuel_pct_at_trip=b.get("fuel_pct_at_trip"),
            bingo_pct_at_trip=b.get("bingo_pct_at_trip"),
            position=tuple(b["position"]) if b.get("position") else None,  # type: ignore[arg-type]
            clear_attempts=int(b.get("clear_attempts", 0)),
        )
        return fm


# ---------------------------------------------------------------------------
# Lost link (M9)
# ---------------------------------------------------------------------------

class LostLinkBehaviour(str, Enum):
    """The four planned behaviours of PLAN §4.5 (M9)."""

    HOLD_ORBIT = "hold_orbit"
    CLIMB_FOR_LOS = "climb_for_los"
    RTB = "rtb"
    CONTINUE = "continue"


class LinkState(str, Enum):
    UP = "up"
    DEGRADED = "degraded"
    PENDING = "pending"  # link down, dwell timer not yet expired
    LOAL = "loal"        # loss-of-all-link declared; behaviour executing


@dataclass
class LostLinkPlan:
    """Per-mission lost-link plan (M9), carried on the mission record."""

    behaviour: LostLinkBehaviour = LostLinkBehaviour.RTB
    declare_after_s: float = 5.0        # dwell before declaring LOAL
    restore_after_s: float = 2.0        # dwell of good link before "restored"
    orbit_radius_m: float = 200.0
    orbit_alt_m: float | None = None
    climb_to_m: float = 120.0           # CLIMB_FOR_LOS target (AGL)
    escalate_to_rtb_after_s: float = 300.0  # any behaviour -> RTB if still dark

    @classmethod
    def from_dict(cls, d: dict | None) -> "LostLinkPlan":
        """Parse a harness-supplied plan; unknown behaviour is rejected (M9)."""
        if not d:
            return cls()
        b = d.get("behaviour", LostLinkBehaviour.RTB.value)
        try:
            behaviour = LostLinkBehaviour(b)
        except ValueError as exc:
            raise ValueError(
                f"unknown lost_link behaviour {b!r}; expected one of "
                f"{[x.value for x in LostLinkBehaviour]}"
            ) from exc
        return cls(
            behaviour=behaviour,
            declare_after_s=float(d.get("declare_after_s", 5.0)),
            restore_after_s=float(d.get("restore_after_s", 2.0)),
            orbit_radius_m=float(d.get("orbit_radius_m", 200.0)),
            orbit_alt_m=(float(d["orbit_alt_m"]) if d.get("orbit_alt_m") is not None else None),
            climb_to_m=float(d.get("climb_to_m", 120.0)),
            escalate_to_rtb_after_s=float(d.get("escalate_to_rtb_after_s", 300.0)),
        )

    def to_dict(self) -> dict:
        return {
            "behaviour": self.behaviour.value,
            "declare_after_s": self.declare_after_s,
            "restore_after_s": self.restore_after_s,
            "orbit_radius_m": self.orbit_radius_m,
            "orbit_alt_m": self.orbit_alt_m,
            "climb_to_m": self.climb_to_m,
            "escalate_to_rtb_after_s": self.escalate_to_rtb_after_s,
        }


@dataclass
class LostLinkMonitor:
    """Link-loss state machine + LOAL event log (M9).

    The tasking layer calls `observe()` once per telemetry tick with the
    link health it measured (or with `link_up=False` on harness disconnect,
    PLAN §4.5) and executes `action` from the returned event. Events are
    retained for the INTREP's "LOAL events" section (M8/§4.7).
    """

    plan: LostLinkPlan = field(default_factory=LostLinkPlan)
    state: LinkState = LinkState.UP
    events: list[dict] = field(default_factory=list)
    action: LostLinkBehaviour | None = None
    down_since: float | None = None
    up_since: float | None = None
    loal_count: int = 0

    def _emit(self, event: str, now: float, **fields) -> dict:
        rec = {"event": event, "t": round(now, 3), "state": self.state.value,
               "behaviour": self.plan.behaviour.value,
               "action": self.action.value if self.action else None, **fields}
        self.events.append(rec)
        return rec

    def observe(self, link_up: bool, *, degraded: bool = False,
                now: float | None = None, bingo_latched: bool = False) -> dict | None:
        """Feed one link sample. Returns an event dict on a transition (M9).

        Transitions: `loal_declared` (dwell expired -> fly the plan),
        `loal_escalated` (still dark past escalate_to_rtb_after_s -> RTB),
        `link_restored` (link back for restore_after_s -> hand control back).
        A latched BINGO overrides the plan: safety always flies RTB (M4).
        """
        now = time.monotonic() if now is None else float(now)
        if link_up:
            if self.state in (LinkState.PENDING, LinkState.LOAL):
                if self.up_since is None:
                    self.up_since = now
                if now - self.up_since < self.plan.restore_after_s:
                    return None
                was = self.state
                # Outage length = link back minus link lost (not dwell expiry).
                dur = (round(self.up_since - self.down_since, 3)
                       if self.down_since is not None else 0.0)
                self.state = LinkState.UP
                self.down_since = None
                self.up_since = None
                prev_action, self.action = self.action, None
                if was is LinkState.LOAL:
                    return self._emit("link_restored", now, duration_s=dur,
                                      resumed_from=prev_action.value if prev_action else None)
                return None  # PENDING -> UP: dwell never expired, no LOAL to log
            self.state = LinkState.DEGRADED if degraded else LinkState.UP
            self.up_since = None
            self.down_since = None
            return None

        # link is down
        self.up_since = None
        if self.down_since is None:
            self.down_since = now
            self.state = LinkState.PENDING
            return None
        down_for = now - self.down_since
        if self.state is LinkState.PENDING:
            if down_for < self.plan.declare_after_s:
                return None
            self.state = LinkState.LOAL
            self.loal_count += 1
            self.action = (LostLinkBehaviour.RTB if bingo_latched else self.plan.behaviour)
            return self._emit("loal_declared", now, down_for_s=round(down_for, 3),
                              bingo_override=bool(bingo_latched))
        # already in LOAL: escalate to RTB if the link stays dark too long
        if (self.action is not LostLinkBehaviour.RTB
                and down_for >= self.plan.escalate_to_rtb_after_s):
            self.action = LostLinkBehaviour.RTB
            return self._emit("loal_escalated", now, down_for_s=round(down_for, 3),
                              reason="escalate_to_rtb_after_s")
        if bingo_latched and self.action is not LostLinkBehaviour.RTB:
            self.action = LostLinkBehaviour.RTB
            return self._emit("loal_escalated", now, down_for_s=round(down_for, 3),
                              reason="bingo")
        return None

    @property
    def lost(self) -> bool:
        return self.state is LinkState.LOAL

    def to_dict(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else float(now)
        return {
            "state": self.state.value,
            "plan": self.plan.to_dict(),
            "action": self.action.value if self.action else None,
            "down_for_s": round(now - self.down_since, 2) if self.down_since is not None else 0.0,
            "loal_count": self.loal_count,
        }

    def intrep_events(self) -> list[dict]:
        """LOAL events for the INTREP template (M8, PLAN §4.7)."""
        return list(self.events)


# ---------------------------------------------------------------------------
# Per-tick monitor (the API the telemetry loop calls)
# ---------------------------------------------------------------------------

@dataclass
class SafetyMonitor:
    """One vehicle's live safety state: fuel + envelope + link (M4/M9/M15).

    Designed for a telemetry tick loop: call `tick()` per sample, act on
    `force_rtb`, publish `alarms` to the SSE channel (§3.1) and append
    `fuel_record` to the JSONL fuel journal (§4.5).

    Breach doctrine (:data:`BREACH_DOCTRINE`, and the module docstring for the
    reasoning): a GEOFENCE breach forces RTB; a ceiling, max-speed or min-AGL
    breach raises an alarm and does NOT. That asymmetry is deliberate — the
    geofence is the only one an RTB is the remedy for — and the field is named
    for the one case it governs rather than the generic `breach_forces_rtb`,
    which read as though every breach committed the vehicle. A ceiling or
    speed excursion that will not clear is published as `sustained_breaches`
    on the verdict, with how long it has stood, so the operator sees an
    un-recovered envelope violation instead of one alarm edge and silence.
    """

    envelope: SafetyEnvelope = field(default_factory=SafetyEnvelope)
    fuel: FuelModel = field(default_factory=FuelModel)
    link: LostLinkMonitor = field(default_factory=LostLinkMonitor)
    #: Doctrine switch for the one breach that DOES commit the vehicle.
    geofence_breach_forces_rtb: bool = True
    #: A breach still standing after this long is reported as sustained.
    sustained_breach_s: float = 30.0
    _active_alarms: set = field(default_factory=set)
    _breach_since: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.fuel.home is None and self.envelope.home is not None:
            self.fuel.home = self.envelope.home

    def _alarm_edges(self, active: dict[str, dict], now: float) -> list[dict]:
        """Emit raised/cleared edges only, so the alarm stream is not a spam."""
        out: list[dict] = []
        for kind, payload in active.items():
            if kind not in self._active_alarms:
                self._active_alarms.add(kind)
                out.append({"kind": kind, "state": "raised", "t": round(now, 3), **payload})
        for kind in sorted(self._active_alarms - set(active)):
            self._active_alarms.discard(kind)
            out.append({"kind": kind, "state": "cleared", "t": round(now, 3)})
        return out

    def _sustained(self, breaches: list[Violation], now: float) -> list[dict]:
        """Breaches that have stood longer than `sustained_breach_s`.

        A ceiling or over-speed excursion is alarm-only by doctrine because it
        is recoverable in place — but one that never recovers must not vanish
        into a single alarm edge. This is the state that says so: `for_s` is
        how long the vehicle has been outside that limit, continuously.
        """
        kinds = {v.kind: v for v in breaches}
        for gone in [k for k in self._breach_since if k not in kinds]:
            self._breach_since.pop(gone, None)
        out: list[dict] = []
        for kind, v in kinds.items():
            since = self._breach_since.setdefault(kind, now)
            for_s = now - since
            if for_s >= self.sustained_breach_s:
                out.append({
                    "kind": kind,
                    "since": round(since, 3),
                    "for_s": round(for_s, 2),
                    "value": round(v.value, 3),
                    "limit": round(v.limit, 3),
                    "doctrine": doctrine_for(kind),
                })
        return out

    def tick(self, *, lat: float, lon: float, alt_agl_m: float, speed_mps: float,
             vz_mps: float, landed: bool, vx_mps: float | None = None,
             vy_mps: float | None = None, track_deg: float | None = None,
             wind_ne: tuple[float, float] = (0.0, 0.0), link_up: bool = True,
             link_degraded: bool = False, now: float | None = None) -> dict:
        """One telemetry sample -> full safety verdict.

        `vz_mps` is NED (positive = descending), matching the bridge snapshot
        and the sim's own `linear_velocity.z_val`. Supply either `track_deg`
        or `vx_mps`/`vy_mps` so wind can be resolved into a headwind
        component (M15).

        `rtb_reasons` carries only what the enforcement layer flies: `bingo`
        (un-cancellable), `geofence`, `lost_link`. Ceiling/speed/min-AGL
        breaches are alarm-only by doctrine — see :data:`BREACH_DOCTRINE` —
        and surface under `sustained_breaches` if they do not clear.
        """
        now = time.monotonic() if now is None else float(now)
        if track_deg is None and vx_mps is not None and vy_mps is not None:
            track_deg = track_deg_from_velocity(vx_mps, vy_mps)
        headwind = headwind_component_mps(wind_ne[0], wind_ne[1], track_deg)

        fuel_pct = self.fuel.tick(speed_mps, vz_mps, landed, headwind, now)
        # The BINGO line must price the leg *home*, not the leg being flown:
        # hand check_bingo the wind vector so the headwind is resolved from the
        # bearing to home (M15). Passing the current track's scalar headwind
        # made a tailwind read as "no wind penalty" on the return leg, i.e. the
        # abort line was under-estimated in exactly the downwind case where the
        # vehicle has to fight its way back (M4).
        bingo = self.fuel.check_bingo((lat, lon), alt_agl_m, wind_ne=wind_ne, now=now)
        violations = self.envelope.check_state(lat, lon, alt_agl_m,
                                               speed_mps=speed_mps, landed=landed)
        link_event = self.link.observe(link_up, degraded=link_degraded, now=now,
                                       bingo_latched=self.fuel.bingo.tripped)

        active: dict[str, dict] = {v.kind: v.to_dict() for v in violations}
        if bingo["latched"]:
            active["bingo"] = {"message": MISSION_INCOMPLETE_FUEL,
                               "fuel_pct": bingo["fuel_pct"],
                               "bingo_fuel_pct": bingo["bingo_fuel_pct"]}
        if self.link.lost:
            active["lost_link"] = {"message": f"lost_link:{self.link.action.value if self.link.action else ''}"}
        alarms = self._alarm_edges(active, now)

        breaches = [v for v in violations if v.is_breach]
        sustained = self._sustained(breaches, now)
        # A finding whose doctrine nobody wrote down is a question, not a
        # verdict. It is named here so an operator sees "nobody decided what
        # this commits the vehicle to" instead of reading the old `.get()`
        # default and concluding the system had decided it was harmless.
        undeclared = sorted({v.kind for v in violations
                             if v.kind not in BREACH_DOCTRINE})
        reasons: list[str] = []
        if bingo["latched"]:
            reasons.append("bingo")
        # BREACH_DOCTRINE: the geofence is the only envelope breach an RTB is
        # the remedy for, so it is the only one that commits the vehicle.
        # Ceiling / max-speed / min-AGL are alarm-only by design and are
        # escalated to the operator as `sustained_breaches`, not to the
        # tasking layer as an RTB it would not fly (§4.5, M14).
        if self.geofence_breach_forces_rtb and any(v.kind == "geofence"
                                                   for v in breaches):
            reasons.append("geofence")
        if self.link.action is LostLinkBehaviour.RTB:
            reasons.append("lost_link")
        return {
            "t": round(now, 3),
            "fuel_pct": round(fuel_pct, 3),
            "phase": self.fuel.last_phase.value,
            "headwind_mps": round(headwind, 2),
            "bingo": bingo,
            "violations": [v.to_dict() for v in violations],
            "breaches": [v.kind for v in breaches],
            "sustained_breaches": sustained,
            "breach_doctrine": dict(BREACH_DOCTRINE),
            "undeclared_doctrine": undeclared,
            "alarms": alarms,
            "link": self.link.to_dict(now),
            "link_event": link_event,
            "force_rtb": bool(reasons),
            "rtb_reasons": reasons,
            "uncancellable": bool(bingo["latched"]),
            "mission_status": bingo["mission_status"],
            "fuel_record": self.fuel.fuel_record(t=round(now, 3), lat=lat, lon=lon),
        }
