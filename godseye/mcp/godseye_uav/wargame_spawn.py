"""Spawn rules, placement and the real-site gate for the simulated wargame (M14a, WG v2 §5.2.6).

PLAN.md §4.5a: scenario units are simulated and placed by the wargame only, kept
clear of every real place. A position is valid only when ALL of these hold:

- inside the AO and the geofence, at least `AO_MARGIN_M` from either edge;
- not within `SITE_EXCLUSION_M` (500 m) of a mapped footprint (`exclusion.blocks`);
- at least 500 m from every theater point (POI);
- red only: at least `HOME_EXCLUSION_M` (1 km) from home;
- at least `UNIT_SPACING_M` (200 m) from every other scenario unit;
- at least `OBJECT_CLEARANCE_M` (150 m) from every other sim object and track.

A refused position is moved by a deterministic spiral (radius `k x 150 m`,
k = 1..8, bearings 0-330 deg in 30 deg steps; the first valid point wins). When
any unit of a request finds no valid point the whole request is refused with
`no_room`; the exclusions are never relaxed.

`site_gate` is the real-site half of the strike and corridor gates (§5.2.6).
Messages never name a place. Pure: no I/O, no clock, no randomness; the engine
(`wargame.py`) snapshots a `World` under its lock and calls in here.
"""
from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .safety import haversine_m, point_in_polygon
from .wargame_tables import destination

#: Mapped footprints and theater points: scenario units and targets keep clear.
SITE_EXCLUSION_M = 500.0
#: A protected place (medical): no target within this distance.
PROTECTED_EXCLUSION_M = 1000.0
HOME_EXCLUSION_M, UNIT_SPACING_M, OBJECT_CLEARANCE_M = 1000.0, 200.0, 150.0
AO_MARGIN_M = 150.0
MAX_UNITS = 60
#: The deterministic spiral (§5.2.2 "Placing a scenario").
SPIRAL_STEP_M = 150.0
SPIRAL_RINGS = 8
SPIRAL_BEARINGS: tuple[float, ...] = tuple(float(b) for b in range(0, 360, 30))

_M_PER_DEG = 111_320.0

#: Refusal sentences (§3.1: one sentence, shown verbatim; never a place name).
MESSAGES: dict[str, str] = {
    "outside_ao": "That position is outside the AO or within 150 m of its edge.",
    "near_real_site": "That position is within 500 m of a mapped place.",
    "near_theater_point": "That position is within 500 m of a theater point.",
    "too_close_home": "Red units are kept at least 1 km from home.",
    "too_close_unit": "That position is within 200 m of another scenario unit.",
    "near_existing_object": "That position is within 150 m of another sim object or contact.",
    "max_units": "The wargame holds at most 60 scenario units.",
    "unknown_class": "That isn't a wargame class; list them with wg_list_classes.",
    "side_mismatch": "That class belongs to the other side.",
    "no_room": ("There isn't room here for this scenario away from mapped places, theater "
                "points and home; try a smaller intensity or a larger AO."),
    "target_near_real_site": "The target is within 500 m of a mapped place or theater point.",
    "target_protected": "The target is within 1 km of a protected place.",
}

#: Codes `position_refusal` can return, in the order they are checked.
POSITION_CODES = ("outside_ao", "near_real_site", "near_theater_point", "too_close_home",
                  "too_close_unit", "near_existing_object")


def refusal(code: str, message: str | None = None, **extra: Any) -> dict:
    """`{rejected, error, message}` (§3.1), stamped simulated (D1)."""
    return {"rejected": True, "error": code, "message": message or MESSAGES[code],
            **extra, "simulated": True}


LatLon = tuple[float, float]


@dataclass(frozen=True)
class World:
    """What a position is checked against: a snapshot the engine takes under its lock.

    `ao` and `geofence` are `[(lat, lon), ...]` polygons (the geofence may be
    empty: then only the AO applies). `exclusion` is a `sites.Exclusion` (or
    anything with `blocks(lat, lon, margin_m)`), None meaning no footprints.
    `units` are the current scenario unit positions; `objects` every other sim
    object and track position. `n_units` counts the session's units.
    """

    ao: tuple[LatLon, ...]
    geofence: tuple[LatLon, ...] = ()
    exclusion: Any = None
    pois: tuple[LatLon, ...] = ()
    home: LatLon | None = None
    units: tuple[LatLon, ...] = ()
    objects: tuple[LatLon, ...] = ()
    n_units: int = 0
    notes: dict = field(default_factory=dict, compare=False)

    def with_unit(self, lat: float, lon: float) -> World:
        """This world with one more scenario unit at (lat, lon)."""
        return replace(self, units=(*self.units, (float(lat), float(lon))),
                       n_units=self.n_units + 1)


def _xy(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """Local metres east/north of (lat0, lon0); exact enough at AO scale."""
    return ((lon - lon0) * _M_PER_DEG * math.cos(math.radians(lat0)),
            (lat - lat0) * _M_PER_DEG)


def edge_distance_m(lat: float, lon: float, polygon: Sequence[Sequence[float]]) -> float:
    """Metres from (lat, lon) to the nearest edge of `polygon` (inf when < 2 vertices)."""
    pts = [(float(p[0]), float(p[1])) for p in polygon]
    if len(pts) < 2:
        return math.inf
    best = math.inf
    for (alat, alon), (blat, blon) in zip(pts, pts[1:] + pts[:1], strict=True):
        ax, ay = _xy(lat, lon, alat, alon)
        bx, by = _xy(lat, lon, blat, blon)
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 <= 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def inside_with_margin(lat: float, lon: float, polygon: Sequence[Sequence[float]],
                       margin_m: float = AO_MARGIN_M) -> bool:
    """Inside `polygon` and at least `margin_m` from its edge."""
    poly = [(float(p[0]), float(p[1])) for p in polygon]
    if len(poly) < 3 or not point_in_polygon(lat, lon, poly):
        return False
    return edge_distance_m(lat, lon, poly) >= margin_m


def _near_any(lat: float, lon: float, points: Sequence[LatLon], radius_m: float) -> bool:
    return any(haversine_m(lat, lon, p[0], p[1]) < radius_m for p in points)


def _blocks(exclusion: Any, lat: float, lon: float, margin_m: float) -> bool:
    if exclusion is None:
        return False
    return bool(exclusion.blocks(lat, lon, margin_m))


def position_refusal(world: World, side: str, lat: float, lon: float) -> str | None:
    """The first spawn rule (lat, lon) breaks for a unit of `side`, else None."""
    lat, lon = float(lat), float(lon)
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return "outside_ao"
    if not inside_with_margin(lat, lon, world.ao):
        return "outside_ao"
    if len(world.geofence) >= 3 and not inside_with_margin(lat, lon, world.geofence):
        return "outside_ao"
    if _blocks(world.exclusion, lat, lon, SITE_EXCLUSION_M):
        return "near_real_site"
    if _near_any(lat, lon, world.pois, SITE_EXCLUSION_M):
        return "near_theater_point"
    if side == "red" and world.home is not None and \
            haversine_m(lat, lon, world.home[0], world.home[1]) < HOME_EXCLUSION_M:
        return "too_close_home"
    if _near_any(lat, lon, world.units, UNIT_SPACING_M):
        return "too_close_unit"
    if _near_any(lat, lon, world.objects, OBJECT_CLEARANCE_M):
        return "near_existing_object"
    return None


def spiral(lat: float, lon: float) -> Iterator[LatLon]:
    """The deterministic spiral around (lat, lon): rings of `k x 150 m`, k = 1..8,
    each walked 0-330 deg in 30 deg steps (§5.2.2)."""
    for k in range(1, SPIRAL_RINGS + 1):
        for b in SPIRAL_BEARINGS:
            yield destination(lat, lon, k * SPIRAL_STEP_M, b)


def place(world: World, side: str, lat: float, lon: float, *,
          move: bool = True) -> tuple[LatLon | None, str | None]:
    """`((lat, lon), None)` for a valid point, else `(None, code)`.

    With `move` the spiral is walked and the first valid point wins (its code
    is `no_room` when none is); without it the point itself must be valid.
    """
    code = position_refusal(world, side, lat, lon)
    if code is None:
        return (float(lat), float(lon)), None
    if not move:
        return None, code
    for p in spiral(float(lat), float(lon)):
        if position_refusal(world, side, p[0], p[1]) is None:
            return p, None
    return None, "no_room"


def plan_positions(world: World, requests: Sequence[tuple[str, float, float]], *,
                   move_first: bool = True) -> tuple[list[LatLon] | None, str | None]:
    """Place every `(side, lat, lon)` request, in order, before anything is spawned.

    Each accepted point joins the world as a unit, so later requests keep the
    200 m spacing from it. Returns `(positions, None)`, or `(None, code)` when
    a request has no valid point: `max_units`, the first request's own code
    when `move_first` is False, else `no_room`. Nothing is partly placed.
    """
    if world.n_units + len(requests) > MAX_UNITS:
        return None, "max_units"
    out: list[LatLon] = []
    w = world
    for i, (side, lat, lon) in enumerate(requests):
        p, code = place(w, side, lat, lon, move=(move_first or i > 0))
        if p is None:
            return None, code
        out.append(p)
        w = w.with_unit(*p)
    return out, None


def moved_count(requests: Sequence[tuple[str, float, float]], positions: Sequence[LatLon],
                tolerance_m: float = 1.0) -> int:
    """How many placed positions the spiral moved off their nominal point."""
    return sum(1 for (_, lat, lon), p in zip(requests, positions, strict=True)
               if haversine_m(lat, lon, p[0], p[1]) > tolerance_m)


def site_gate(exclusion: Any, pois: Sequence[LatLon], lat: float, lon: float) -> str | None:
    """The real-site gate on one point (§5.2.6), or None when it passes.

    `target_near_real_site` within 500 m of a mapped footprint or a theater
    point; `target_protected` within 1 km of a protected place.
    """
    lat, lon = float(lat), float(lon)
    if _blocks(exclusion, lat, lon, SITE_EXCLUSION_M) or \
            _near_any(lat, lon, pois, SITE_EXCLUSION_M):
        return "target_near_real_site"
    if exclusion is not None and exclusion.protected_within(lat, lon, PROTECTED_EXCLUSION_M):
        return "target_protected"
    return None


def object_name(ob_key: str, num: int, seq: int) -> str:
    """Session-unique sim object name `{ob_key}_{num}{seq:03d}` (§5.2.4). It
    classifies back to `ob_key` through `targets.match_ob` (D7 #6)."""
    return f"{ob_key}_{int(num)}{int(seq):03d}"


_COMPASS = ("north", "north-east", "east", "south-east", "south", "south-west", "west",
            "north-west")


def compass_word(bearing: float) -> str:
    """The 8-point compass word for a bearing in degrees true."""
    return _COMPASS[int(((float(bearing) % 360.0) + 22.5) // 45.0) % 8]


def distance_words(metres: float) -> str:
    """'350 m' below 1 km, else '2.1 km'."""
    m = max(0.0, float(metres))
    return f"{m:.0f} m" if m < 1000.0 else f"{m / 1000.0:.1f} km"


def relative_position(center: LatLon, lat: float, lon: float) -> str:
    """AO-relative text, e.g. '2.1 km north-east of the AO centre' (§3.1: no place
    names in wargame output)."""
    from .safety import bearing_deg

    d = haversine_m(center[0], center[1], float(lat), float(lon))
    if d < 100.0:
        return "at the AO centre"
    word = compass_word(bearing_deg(center[0], center[1], float(lat), float(lon)))
    return f"{distance_words(d)} {word} of the AO centre"
