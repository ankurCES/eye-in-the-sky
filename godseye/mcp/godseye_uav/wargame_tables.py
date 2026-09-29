"""Notional class tables for the simulated wargame (M14a, PLAN.md §4.5a; WG spec §5.2.2).

Every number here is a round play-balance parameter for a SIMULATION. None of it
is weapon data: there are no real system names, no munition specifications, no
blast radii, no fuzing and no aimpoints (D1). Labels and designators are generic
("Red SAM 1", "Air-defence guns"). `tests/support/wg_tokens.py` enforces that on
every label and note.

A red class carries an `ob_key`: the order-of-battle class of the sim object it
is spawned as, used ONLY so the ISR pipeline classifies the resulting track. Blue
classes are registry-only (`ob_key=None`): they never exist in the sim scene and
are never sensed.

Templates place units relative to the AO centre and home only, never to a site
or any mapped place (§5.2.2 "Templates"). This module computes nominal positions;
the spawn rules and the deterministic spiral are `wargame_spawn.py`'s (B3).

Pure: no I/O, no clock, no randomness. `srv` is duck-typed and only
`ao_geometry(srv)` reads it (`srv.theater.bbox()`).
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .safety import EARTH_RADIUS_M, bearing_deg, haversine_m
from .targets import OB_LIBRARY

TABLE_VERSION = "wg-notional/1"
NOTIONAL_NOTE = "Notional simulation parameters chosen for play balance. Not weapon data."
P_CAP = 0.95
DAMAGED_PK_FACTOR, DAMAGED_CYCLE_FACTOR, DAMAGED_RANGE_FACTOR = 0.5, 2.0, 0.8
RADAR_CUE_BONUS = 0.2
CONF_FACTOR = {"confirmed": 1.0, "probable": 0.85}
EFFECT_SPLIT = {"destroyed": 0.32, "damaged": 0.40, "suppressed": 0.28}
PACKAGE_ALT_AGL_M, PACKAGE_SPEED_MPS = 150.0, 100.0

SIDES = ("red", "blue")
ROLES = ("air", "sensor", "ground", "indirect", "none", "shooter", "objective")

#: Per-unit caveat for a class whose threat envelope reaches every AO corner.
COVERS_CAVEAT = "Its envelope covers the whole AO; no route avoids it."


@dataclass(frozen=True)
class WgClass:
    """One notional wargame class. Distances in metres, times in SIM seconds."""

    key: str
    side: str
    ob_key: str | None
    prefix_slug: str
    label: str
    role: str                     # one of ROLES
    threat_range_m: float = 0.0   # vs aircraft
    threat_ceiling_m: float = 0.0  # height above the unit
    detection_range_m: float = 0.0
    p_detect_ref: float = 0.0
    pk_air: float = 0.0
    ground_range_m: float = 0.0   # red vs blue units
    pk_ground: float = 0.0
    strike_range_m: float | None = 0.0   # blue shooters; None = whole AO
    pk_strike: float = 0.0
    cycle_s: float = 0.0          # sim seconds between shots
    ammo: int = 0
    suppress_s: float = 0.0       # sim seconds
    optical: bool = False
    speed_mps: float = 0.0
    hardness: float = 1.0         # multiplies effect against this unit

    @property
    def mobile(self) -> bool:
        return self.speed_mps > 0.0

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready row (generic label only; `ob_key` is a sim class key)."""
        return asdict(self)


def _red(key: str, ob_key: str, prefix: str, label: str, role: str, *,
         threat: float = 0.0, ceiling: float = 0.0, detect: float = 0.0,
         p_det: float = 0.0, pk_air: float = 0.0, ground: float = 0.0,
         pk_ground: float = 0.0, cycle: float = 0.0, ammo: int = 0,
         suppress: float = 0.0, optical: bool = False, speed: float = 0.0,
         hardness: float = 1.0) -> WgClass:
    return WgClass(
        key=key, side="red", ob_key=ob_key, prefix_slug=prefix, label=label, role=role,
        threat_range_m=float(threat), threat_ceiling_m=float(ceiling),
        detection_range_m=float(detect), p_detect_ref=float(p_det), pk_air=float(pk_air),
        ground_range_m=float(ground), pk_ground=float(pk_ground), strike_range_m=0.0,
        cycle_s=float(cycle), ammo=int(ammo), suppress_s=float(suppress),
        optical=bool(optical), speed_mps=float(speed), hardness=float(hardness))


def _blue(key: str, prefix: str, label: str, role: str, *,
          strike: float | None = 0.0, pk_strike: float = 0.0, cycle: float = 0.0,
          ammo: int = 0, suppress: float = 0.0, hardness: float = 1.0,
          speed: float = 0.0) -> WgClass:
    return WgClass(
        key=key, side="blue", ob_key=None, prefix_slug=prefix, label=label, role=role,
        strike_range_m=None if strike is None else float(strike),
        pk_strike=float(pk_strike), cycle_s=float(cycle), ammo=int(ammo),
        suppress_s=float(suppress), hardness=float(hardness), speed_mps=float(speed))


#: The 16 notional classes (11 red, 5 blue), WG spec §5.2.2 tables.
#: Blue `suppress_s` is not in the spec's blue table; it is set here (180 s, the
#: red ground value; 300 s for the depot, as for the red depot) so a "suppressed"
#: outcome from red ground fire lasts longer than one pass. The virtual strike
#: package is never a red ground-fire target, so it keeps 0.
CLASSES: dict[str, WgClass] = {c.key: c for c in (
    # ---- red (sim objects; `ob_key` classifies the ISR track) ----
    _red("ad_long", "sam_long_range", "sam", "Surface-to-air, long range", "air",
         threat=40000, ceiling=15000, detect=80000, p_det=.90, pk_air=.60,
         cycle=30, ammo=8, suppress=300, hardness=.85),
    _red("ad_medium", "sam_medium_range", "sam", "Surface-to-air, medium range", "air",
         threat=20000, ceiling=8000, detect=40000, p_det=.85, pk_air=.55,
         cycle=20, ammo=12, suppress=240, hardness=.85),
    _red("ad_short", "sam_short_range", "sam", "Surface-to-air, short range", "air",
         threat=8000, ceiling=4000, detect=15000, p_det=.80, pk_air=.50,
         cycle=12, ammo=8, suppress=180, speed=6, hardness=.85),
    _red("ad_gun", "aaa_towed", "aaa", "Air-defence guns", "air",
         threat=2000, ceiling=1000, detect=3000, p_det=.70, pk_air=.30,
         cycle=5, ammo=40, suppress=120, optical=True, hardness=.85),
    _red("ad_manportable", "manpads", "manpads", "Portable surface-to-air", "air",
         threat=4000, ceiling=2500, detect=5000, p_det=.60, pk_air=.40,
         cycle=60, ammo=4, suppress=120, optical=True, speed=1.5, hardness=.9),
    _red("radar_early_warning", "radar_acquisition", "radar", "Early-warning radar", "sensor",
         detect=60000, p_det=.90, suppress=300, hardness=.9),
    _red("armour_company", "mbt", "armour", "Armour company", "ground",
         detect=5000, p_det=.80, ground=3000, pk_ground=.35, cycle=60, ammo=20,
         suppress=180, optical=True, speed=8, hardness=.7),
    _red("mech_infantry", "ifv", "infantry", "Mechanised infantry", "ground",
         detect=4000, p_det=.80, ground=2000, pk_ground=.25, cycle=60, ammo=20,
         suppress=180, optical=True, speed=6, hardness=.8),
    _red("artillery_battery", "spg", "artillery", "Artillery battery", "indirect",
         detect=2000, p_det=.70, ground=15000, pk_ground=.30, cycle=120, ammo=12,
         suppress=240, optical=True, hardness=.85),
    _red("command_post", "c2_node", "cp", "Command post", "none",
         detect=2000, p_det=.70, suppress=300, optical=True, hardness=.9),
    _red("supply_depot", "depot_ammo", "depot", "Supply depot", "none",
         suppress=300, hardness=1.0),
    # ---- blue (registry-only; never in the sim scene, never sensed) ----
    _blue("blue_artillery", "artillery", "Artillery battery", "shooter",
          strike=20000, pk_strike=.50, cycle=120, ammo=12, suppress=180, hardness=1.0),
    _blue("blue_rocket", "rockets", "Rocket battery", "shooter",
          strike=40000, pk_strike=.60, cycle=300, ammo=4, suppress=180, hardness=1.0),
    _blue("blue_strike_air", "strike", "Strike package (virtual)", "shooter",
          strike=None, pk_strike=.65, cycle=600, ammo=2),
    _blue("blue_mech", "mech", "Mechanised company", "shooter",
          strike=2500, pk_strike=.35, cycle=60, ammo=20, suppress=180, hardness=.7, speed=6),
    _blue("blue_defended_point", "depot", "Defended depot", "objective",
          suppress=300, hardness=.8),
)}

# --------------------------------------------------------------------------
# Designators and generic labels
# --------------------------------------------------------------------------
#: Designator word per `prefix_slug` (WG spec §5.2.2).
WORD: dict[str, str] = {
    "sam": "SAM", "aaa": "AD guns", "manpads": "portable SAM", "radar": "radar",
    "armour": "armour", "infantry": "infantry", "artillery": "artillery",
    "cp": "command post", "depot": "depot", "rockets": "rockets",
    "strike": "strike package", "mech": "mech company",
}

#: Generic label per OB category, for tracks whose class no red class claims.
CATEGORY_LABEL: dict[str, str] = {
    "sam": "Air-defence contact", "aaa": "Air-defence contact",
    "radar": "Radar contact", "armor": "Armour contact",
    "artillery": "Artillery contact", "c2": "Command contact",
    "logistics": "Logistics or structure", "structure": "Logistics or structure",
}
CONTACT_LABEL = "Contact"

#: Notional hardness estimated from the PERCEIVED class (blue's estimate).
CATEGORY_HARDNESS: dict[str, float] = {
    "sam": .85, "aaa": .85, "radar": .9, "armor": .7, "artillery": .85, "c2": .9,
}

_BY_OB_KEY: dict[str, WgClass] = {
    c.ob_key: c for c in CLASSES.values() if c.side == "red" and c.ob_key}


def designator(side: str, prefix_slug: str, n: int) -> str:
    """Designator such as "Red SAM 1"; `n` counts per `(side, prefix_slug)`."""
    return f"{'Red' if side == 'red' else 'Blue'} {WORD[prefix_slug]} {int(n)}"


def unit_id(side: str, prefix_slug: str, n: int) -> str:
    """`{side}-{prefix_slug}-{n}`; the graph id is `frc:{unit_id}` (R5)."""
    return f"{side}-{prefix_slug}-{int(n)}"


def red_class_for_ob(ob_key: str | None) -> WgClass | None:
    """The red class spawned as sim class `ob_key`, if any."""
    return _BY_OB_KEY.get(ob_key or "")


def label_for_ob(ob_key: str | None) -> str:
    """Generic label for a perceived OB class: never an OB name (§5.2.2)."""
    cls = red_class_for_ob(ob_key)
    if cls is not None:
        return cls.label
    ob = OB_LIBRARY.get(ob_key or "")
    if ob is not None:
        return CATEGORY_LABEL.get(ob.category, CONTACT_LABEL)
    return CONTACT_LABEL


def estimate_hardness(ob_key: str | None) -> float:
    """Blue's notional hardness estimate from the perceived OB category."""
    ob = OB_LIBRARY.get(ob_key or "")
    return CATEGORY_HARDNESS.get(ob.category, 1.0) if ob is not None else 1.0


# --------------------------------------------------------------------------
# AO fit
# --------------------------------------------------------------------------
def ao_geometry_bbox(bbox: Sequence[float]) -> dict[str, float]:
    """`{half_extent_m, half_diagonal_m}` of an `[s, w, n, e]` box.

    Width and height are measured at the box's middle, as
    `geocode.bbox_size_m` does; `half_extent_m` is `min(half-width,
    half-height)`, as `theater_plan.bbox_half_extent_m` and the graph's
    theater block report it (§3.2).
    """
    s, w, n, e = (float(v) for v in bbox)
    mid_lat, mid_lon = (s + n) / 2.0, (w + e) / 2.0
    width = haversine_m(mid_lat, w, mid_lat, e)
    height = haversine_m(s, mid_lon, n, mid_lon)
    return {"half_extent_m": round(min(width, height) / 2.0, 1),
            "half_diagonal_m": round(math.hypot(width, height) / 2.0, 1)}


def ao_geometry(srv: Any) -> dict[str, float]:
    """AO geometry of the active theater (`srv.theater.bbox()`, metres)."""
    return ao_geometry_bbox(srv.theater.bbox())


def _half_diagonal(geo: Any) -> float:
    if isinstance(geo, Mapping):
        return float(geo["half_diagonal_m"])
    return float(geo.half_diagonal_m)


def _half_extent(geo: Any) -> float:
    if isinstance(geo, Mapping):
        return float(geo["half_extent_m"])
    return float(geo.half_extent_m)


def covers_ao(cls: WgClass, geo: Any) -> bool:
    """The class's air threat envelope reaches every AO corner."""
    return cls.threat_range_m >= _half_diagonal(geo)


def fits_ao(cls: WgClass, geo: Any) -> bool:
    """The class has an air threat envelope smaller than the AO's half-diagonal."""
    return 0.0 < cls.threat_range_m < _half_diagonal(geo)


def class_caveats(cls: WgClass, geo: Any) -> list[str]:
    """Per-unit caveats (§5.2.6): the covers caveat when it applies."""
    return [COVERS_CAVEAT] if covers_ao(cls, geo) else []


def class_rows(geo: Any | None = None) -> list[dict[str, Any]]:
    """Every class as a row, with `fits_ao`/`covers_ao`/`caveats` when `geo` is given."""
    rows = []
    for cls in CLASSES.values():
        row = cls.as_dict()
        if geo is not None:
            row["fits_ao"] = fits_ao(cls, geo)
            row["covers_ao"] = covers_ao(cls, geo)
            row["caveats"] = class_caveats(cls, geo)
        rows.append(row)
    return rows


def classes_that_fit(geo: Any) -> list[str]:
    """Keys of the classes whose envelope fits the AO (`wg_session_start`)."""
    return [c.key for c in CLASSES.values() if fits_ao(c, geo)]


# --------------------------------------------------------------------------
# Templates (§5.2.2): placement relative to AO centre C and home H only
# --------------------------------------------------------------------------
AD_PREF: tuple[str, ...] = ("ad_medium", "ad_short", "ad_manportable", "ad_gun")
AD_FALLBACK = "ad_gun"
INTENSITIES: tuple[str, ...] = ("low", "medium", "high")
ANCHORS: tuple[str, ...] = ("far", "near", "C", "H")
FAR_FRAC, NEAR_FRAC = 0.55, 0.35      # anchor distances, fractions of the half-extent
RING_STEP = 0.1                       # added to r per full cycle of offsets
SAME_POINT_M = 200.0                  # H and C closer than this: bearing 0


@dataclass(frozen=True)
class TemplateSlot:
    """One template row. `classes` is one key, or a preference list (AD choice)."""

    slot: str
    side: str
    classes: tuple[str, ...]
    counts: tuple[int, int, int]      # low / medium / high
    anchor: str                       # one of ANCHORS
    r: float                          # fraction of the half-extent from the anchor
    offsets_deg: tuple[float, ...]    # added to the bearing H -> C, cycled
    objective: str | None = None      # slot name of this slot's objective


def _slot(slot, side, classes, counts, anchor, r, offsets, objective=None) -> TemplateSlot:
    if isinstance(classes, str):
        classes = (classes,)
    return TemplateSlot(slot, side, tuple(classes), tuple(counts), anchor, float(r),
                        tuple(float(o) for o in offsets), objective)


TEMPLATES: dict[str, tuple[TemplateSlot, ...]] = {
    "air_defence_belt": (
        _slot("ad", "red", AD_PREF, (1, 2, 3), "far", .25, (0, -60, 60)),
        _slot("radar", "red", "radar_early_warning", (0, 1, 1), "far", .45, (0,)),
        _slot("guns", "red", "ad_gun", (1, 1, 2), "C", .30, (-90, 90)),
        _slot("fires", "blue", "blue_artillery", (1, 1, 1), "near", .20, (180,)),
    ),
    "mech_advance": (
        _slot("armour", "red", "armour_company", (1, 2, 3), "far", .20, (0, -40, 40), "point"),
        _slot("infantry", "red", "mech_infantry", (1, 1, 2), "far", .30, (-20, 20), "point"),
        _slot("cover", "red", AD_PREF[1:], (1, 1, 1), "far", .40, (0,)),
        _slot("point", "blue", "blue_defended_point", (1, 1, 1), "near", .25, (0,)),
        _slot("mech", "blue", "blue_mech", (1, 1, 2), "near", .35, (-30, 30)),
        _slot("fires", "blue", "blue_artillery", (1, 1, 1), "near", .20, (180,)),
    ),
    "strike_exercise": (
        _slot("sam", "red", AD_PREF, (1, 1, 2), "far", .20, (0, 90)),
        _slot("radar", "red", "radar_early_warning", (1, 1, 1), "far", .35, (45,)),
        _slot("cp", "red", "command_post", (1, 1, 1), "far", .30, (-45,)),
        _slot("depot", "red", "supply_depot", (1, 1, 2), "far", .45, (-90, 90)),
        _slot("fires", "blue", "blue_artillery", (1, 1, 1), "near", .20, (180,)),
        _slot("rockets", "blue", "blue_rocket", (0, 1, 1), "near", .30, (150,)),
        _slot("strike", "blue", "blue_strike_air", (1, 1, 1), "H", 0, (0,)),
    ),
}


def is_ad_class(key: str | None) -> bool:
    """A red class with an air threat envelope (valid for `ad_class`)."""
    cls = CLASSES.get(key or "")
    return cls is not None and cls.side == "red" and cls.role == "air"


def choose_ad_class(geo: Any, prefs: Sequence[str] = AD_PREF,
                    ad_class: str | None = None) -> str:
    """`ad_class` when given, else the first of `prefs` that fits the AO, else
    `ad_gun` (whose units then carry the covers caveat). ValueError on an
    `ad_class` that is not a red air-defence class."""
    if ad_class is not None:
        if not is_ad_class(ad_class):
            raise ValueError(f"unknown air-defence class: {ad_class!r}")
        return ad_class
    for key in prefs:
        if fits_ao(CLASSES[key], geo):
            return key
    return AD_FALLBACK


@dataclass(frozen=True)
class PlannedUnit:
    """One unit a template asks for, before any position is checked."""

    slot: str
    index: int                        # 0-based within the slot
    side: str
    wg_class: str
    anchor: str
    r: float
    offset_deg: float
    objective: str | None


def expand_template(template: str, intensity: str, geo: Any, *,
                    ad_class: str | None = None) -> list[PlannedUnit]:
    """The units of `template` at `intensity`, in table order. Offsets cycle and
    each full cycle adds `RING_STEP` to `r`. KeyError on an unknown template,
    ValueError on an unknown intensity or `ad_class`."""
    if intensity not in INTENSITIES:
        raise ValueError(f"unknown intensity: {intensity!r}")
    level = INTENSITIES.index(intensity)
    out: list[PlannedUnit] = []
    for s in TEMPLATES[template]:
        key = s.classes[0] if len(s.classes) == 1 else choose_ad_class(
            geo, s.classes, ad_class)
        k = len(s.offsets_deg)
        for i in range(s.counts[level]):
            out.append(PlannedUnit(s.slot, i, s.side, key, s.anchor,
                                   round(s.r + RING_STEP * (i // k), 6),
                                   s.offsets_deg[i % k], s.objective))
    return out


def destination(lat: float, lon: float, distance_m: float,
                bearing: float) -> tuple[float, float]:
    """Point `distance_m` from (lat, lon) on initial bearing `bearing` (deg true).

    Great circle on the same sphere as `safety.haversine_m`, so
    `haversine_m(p, destination(p, d, b)) == d` to rounding.
    """
    d = float(distance_m) / EARTH_RADIUS_M
    b = math.radians(float(bearing))
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return (math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0)


def template_frame(center: Sequence[float], home: Sequence[float],
                   half_extent_m: float) -> dict[str, Any]:
    """`beta` (bearing H -> C, 0 when they are < 200 m apart) and the anchor points."""
    c = (float(center[0]), float(center[1]))
    h = (float(home[0]), float(home[1]))
    half = float(half_extent_m)
    beta = 0.0 if haversine_m(*h, *c) < SAME_POINT_M else bearing_deg(*h, *c)
    return {"beta": beta,
            "far": destination(*c, FAR_FRAC * half, beta),
            "near": destination(*h, NEAR_FRAC * half, beta),
            "C": c, "H": h}


def template_positions(template: str, intensity: str, center: Sequence[float],
                       home: Sequence[float], geo: Any, *,
                       ad_class: str | None = None) -> list[dict[str, Any]]:
    """Nominal position of every unit `template` asks for (§5.2.2): each at
    `destination(anchor, r * half, beta + offset)`. Positions are NOT checked
    here; `wargame_spawn` applies the spawn rules and the spiral (B3)."""
    half = _half_extent(geo)
    frame = template_frame(center, home, half)
    out = []
    for u in expand_template(template, intensity, geo, ad_class=ad_class):
        a_lat, a_lon = frame[u.anchor]
        lat, lon = destination(a_lat, a_lon, u.r * half, frame["beta"] + u.offset_deg)
        out.append({"slot": u.slot, "index": u.index, "side": u.side,
                    "wg_class": u.wg_class, "anchor": u.anchor, "objective": u.objective,
                    "lat": lat, "lon": lon})
    return out
