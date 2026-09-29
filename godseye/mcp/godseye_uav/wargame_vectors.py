"""Simulated wargame vectors: terrain masking, exposure and corridors (M14a, WG v2 §5.2.8).

Pure geometry for the opt-in simulated wargame (PLAN.md §4.5a). Nothing here
touches `srv`, the network, the store or a clock; the engine (`wargame.py`)
snapshots its inputs under its lock and calls these functions in a worker
thread (§5.2.4), so every function is deterministic for its arguments.

What lives here, all at ONE altitude per plan (R19: 2D A*):

- `Lattice` / `build_lattice`: an AO ground grid, `cell_m = clamp(extent/48,
  100, 1500)`. A cell with no real ground sample falls back to home MSL and is
  counted as uncovered (`coverage_pct`).
- `horizon` / `visible` / `coverage_fan`: per-azimuth cumulative terrain
  elevation profiles, so a line-of-sight check is O(1).
- `exposure_grid`: a hazard rate per cell, the sum of `p_detect * pk / cycle_s`
  over threats in range, inside the ceiling band and visible.
- `plan` / `summarize`: exposure-weighted A* inside the geofence (a hard mask),
  string-pulled to at most 40 waypoints; `p_survive = exp(-H)`.
- `red_axes`: straight surface axes from red scenario units to their blue
  objectives (R10: red `frc` -> blue `frc` only).

D1 (M14a): inputs are simulated scenario units only. Threats are the engine's
red units or tracks that already passed the provenance gate (§5.2.6); axes run
only between engine units. Mapped sites, real traffic and other sim objects
never reach this module, and it names no place: outputs are coordinates and
notional numbers. The probabilities come from `wargame_adjudicate` (B1), the
single source of the notional tables; tests may inject a `kernel`.
"""
from __future__ import annotations

import heapq
import math
from array import array
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from types import SimpleNamespace
from typing import Any

from . import safety

#: Lattice sizing (§5.2.8): at most 48 cells a side, 100 m to 1500 m each.
MAX_CELLS = 48
CELL_MIN_M, CELL_MAX_M = 100.0, 1500.0
#: Horizon azimuth bins and coverage-fan points (§5.2.8 defaults).
HORIZON_BINS = 180
FAN_POINTS = 72
#: A sensor sits at least this far above the lattice ground under it, so a
#: unit whose sim altitude disagrees with the terrain grid is not buried.
EYE_AGL_M = 5.0
#: Standard-atmosphere effective earth radius (4/3) for line of sight.
REFRACTION_K = 4.0 / 3.0
EARTH_RADIUS_M = safety.EARTH_RADIUS_M
_RE_EFF_2 = 2.0 * REFRACTION_K * EARTH_RADIUS_M
_M_PER_DEG = math.pi / 180.0 * EARTH_RADIUS_M
#: Planner defaults (§5.2.8).
W_HAZARD = 50_000.0
MAX_EXPANSIONS = 60_000
MAX_WAYPOINTS = 40
SHORTCUT_TOLERANCE = 1.05
#: Planned legs keep this clear of the fence edge (the default
#: `SafetyEnvelope.geofence_warn_m`), so a corridor never flies the drone into
#: a proximity warning it did not start in.
FENCE_BUFFER_M = 100.0
#: Consecutive rejected shortcuts before string-pulling moves on.
_PULL_PATIENCE = 6
#: Threat basis cap (§5.2.8: at most 40 threats, nearest first).
MAX_THREATS = 40
#: Leg exposure buckets (§3.9): low = 0 s exposed, moderate = p_survive >= .95.
LEG_MODERATE_P = 0.95
#: Re-look ring points (§5.2.8 `relook`).
RING_POINTS = 16
#: `terrain_masking.source` words (§5.2.8 output).
TERRAIN_REAL = "real terrain"
TERRAIN_FLAT = "flat (real terrain not loaded)"
ALT_BAND_SURFACE = "surface"

_NEIGHBOURS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


class NoRoute(Exception):
    """The planner found no route; `code` is a refusal code (§3.1)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_result(self) -> dict:
        return {"rejected": True, "error": self.code, "message": self.message}


# ---------------------------------------------------------------- lattice ----
@dataclass(frozen=True, eq=False)
class Lattice:
    """AO ground grid in a local equirectangular frame anchored at (south, west).

    Row `i` runs north from `south`, column `j` east from `west`; `ground_msl`
    is row-major. `undulation_m` is HAE minus MSL at the AO (one value: the
    geoid varies by metres across a 50 km AO), so threat and drone heights can
    be compared in HAE the way `p_kill_air`'s `dz` wants (§5.2.3).
    """

    south: float
    west: float
    north: float
    east: float
    cell_m: float
    nx: int
    ny: int
    ground_msl: tuple[float, ...]
    covered: int
    home_msl_m: float
    undulation_m: float
    kx: float  # metres per degree of longitude at the AO's mid-latitude
    ky: float  # metres per degree of latitude

    @property
    def cells(self) -> int:
        return self.nx * self.ny

    @property
    def coverage_pct(self) -> float:
        """Share of cells with a real ground sample (feeds `terrain_masking`)."""
        return round(100.0 * self.covered / self.cells, 1)

    @property
    def terrain_source(self) -> str:
        return TERRAIN_REAL if self.covered else TERRAIN_FLAT

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return ((lon - self.west) * self.kx, (lat - self.south) * self.ky)

    def latlon(self, x: float, y: float) -> tuple[float, float]:
        return (self.south + y / self.ky, self.west + x / self.kx)

    def _index_xy(self, x: float, y: float) -> int:
        j = min(self.nx - 1, max(0, math.floor(x / self.cell_m)))
        i = min(self.ny - 1, max(0, math.floor(y / self.cell_m)))
        return i * self.nx + j

    def cell_of(self, lat: float, lon: float) -> int:
        """Row-major index of the cell under a point, clamped to the grid."""
        return self._index_xy(*self.xy(lat, lon))

    def center_xy(self, index: int) -> tuple[float, float]:
        i, j = divmod(index, self.nx)
        return ((j + 0.5) * self.cell_m, (i + 0.5) * self.cell_m)

    def center(self, index: int) -> tuple[float, float]:
        return self.latlon(*self.center_xy(index))

    def ground_at(self, lat: float, lon: float) -> float:
        """Ground MSL of the (clamped) cell under a point."""
        return self.ground_msl[self.cell_of(lat, lon)]

    def as_dict(self) -> dict:
        return {"bbox": [self.south, self.west, self.north, self.east],
                "cell_m": round(self.cell_m, 1), "nx": self.nx, "ny": self.ny,
                "coverage_pct": self.coverage_pct, "source": self.terrain_source}


def _ao_fields(ao: Any) -> tuple[float, float, float, float, float, float]:
    """(s, w, n, e, home_msl_m, undulation_m) from a mapping or a Theater.

    A mapping carries `bbox` `[s, w, n, e]` (§3.1), `home_msl_m` and either
    `undulation_m` or `home_hae_m`; a Theater-like object gives `bbox()` and
    `home_alt_msl_m`. A missing undulation is 0 (heights stay MSL-consistent).
    """
    if isinstance(ao, Mapping):
        s, w, n, e = (float(v) for v in ao["bbox"])
        home_msl = float(ao.get("home_msl_m", ao.get("home_alt_msl_m", 0.0)))
        if ao.get("undulation_m") is not None:
            und = float(ao["undulation_m"])
        elif ao.get("home_hae_m") is not None:
            und = float(ao["home_hae_m"]) - home_msl
        else:
            und = 0.0
    else:
        s, w, n, e = (float(v) for v in ao.bbox())
        home_msl, und = float(ao.home_alt_msl_m), 0.0
    if not all(math.isfinite(v) for v in (s, w, n, e, home_msl, und)):
        raise ValueError("AO fields must be finite")
    if not (s < n and w < e):
        raise ValueError("AO bbox must be [south, west, north, east] with south < north, west < east")
    return s, w, n, e, home_msl, und


def build_lattice(ao: Any, ground_fn: Callable[[float, float], float | None] | None,
                  max_cells: int = MAX_CELLS) -> Lattice:
    """Sample the AO ground once per cell centre (§5.2.8).

    `cell_m = clamp(max_extent / max_cells, 100, 1500)`; an AO wider than
    `max_cells * 1500` m (beyond the 25 km half-extent cap, C7) grows the cell
    instead of the grid, so the planner budget holds. `ground_fn(lat, lon)`
    returns MSL metres or None; None or a non-finite value is home MSL and
    counts as uncovered. BLOCKING when `ground_fn` is: call in a worker thread.
    """
    s, w, n, e, home_msl, und = _ao_fields(ao)
    if max_cells < 1:
        raise ValueError("max_cells must be at least 1")
    kx = _M_PER_DEG * math.cos(math.radians((s + n) / 2.0))
    ky = _M_PER_DEG
    width, height = (e - w) * kx, (n - s) * ky
    extent = max(width, height)
    cell = min(CELL_MAX_M, max(CELL_MIN_M, extent / max_cells))
    if math.ceil(extent / cell - 1e-9) > max_cells:
        cell = extent / max_cells
    nx = max(1, math.ceil(width / cell - 1e-9))
    ny = max(1, math.ceil(height / cell - 1e-9))
    ground: list[float] = []
    covered = 0
    for i in range(ny):
        lat = s + (i + 0.5) * cell / ky
        for j in range(nx):
            g = ground_fn(lat, w + (j + 0.5) * cell / kx) if ground_fn is not None else None
            if g is not None and isinstance(g, (int, float)) and math.isfinite(g):
                ground.append(float(g))
                covered += 1
            else:
                ground.append(home_msl)
    return Lattice(south=s, west=w, north=n, east=e, cell_m=cell, nx=nx, ny=ny,
                   ground_msl=tuple(ground), covered=covered, home_msl_m=home_msl,
                   undulation_m=und, kx=kx, ky=ky)


def ground_hae_m(lattice: Lattice, lat: float, lon: float) -> float:
    """Lattice ground under a point in HAE (to place a sensed threat, §5.2.8)."""
    return lattice.ground_at(lat, lon) + lattice.undulation_m


# -------------------------------------------------------------- visibility ----
@dataclass(frozen=True, eq=False)
class Horizon:
    """Cumulative max terrain elevation (tan) per azimuth bin and range step."""

    lat: float
    lon: float
    eye_msl_m: float
    range_m: float
    step_m: float
    bins: int
    steps: int
    ex: float
    ey: float
    kx: float
    ky: float
    west: float
    south: float
    prof: array


def horizon(lat: float, lon: float, eye_msl: float, lattice: Lattice, range_m: float,
            bins: int = HORIZON_BINS) -> Horizon:
    """Terrain horizon of a sensor at (lat, lon, eye_msl) out to `range_m`.

    Rays step one cell length at a time; `prof[b, k]` is the steepest terrain
    elevation (tan, with 4/3-earth curvature) seen along bin `b` at steps
    1..k, so `visible` is one lookup. The eye is lifted to at least
    `EYE_AGL_M` above the lattice ground under it. Samples off the lattice
    carry no terrain. CPU work (~bins x steps): run it off the event loop.
    """
    if bins < 1 or not math.isfinite(range_m) or range_m <= 0:
        raise ValueError("horizon needs bins >= 1 and a positive finite range")
    eye = max(float(eye_msl), lattice.ground_at(lat, lon) + EYE_AGL_M)
    ex, ey = lattice.xy(lat, lon)
    step = lattice.cell_m
    x_max, y_max = lattice.nx * step, lattice.ny * step
    reach = max(math.hypot(cx - ex, cy - ey)
                for cx in (0.0, x_max) for cy in (0.0, y_max))
    steps = max(1, math.ceil(min(range_m, reach) / step))
    ground, nx = lattice.ground_msl, lattice.nx
    width = steps + 1
    prof = array("d", [-math.inf]) * (bins * width)
    for b in range(bins):
        az = math.radians((b + 0.5) * 360.0 / bins)
        ux, uy = math.sin(az), math.cos(az)
        best = -math.inf
        base = b * width
        for k in range(1, width):
            d = k * step
            x, y = ex + d * ux, ey + d * uy
            if 0.0 <= x < x_max and 0.0 <= y < y_max:
                g = ground[int(y / step) * nx + int(x / step)]
                best = max(best, (g - eye - d * d / _RE_EFF_2) / d)
            prof[base + k] = best
    return Horizon(lat=lat, lon=lon, eye_msl_m=eye, range_m=float(range_m), step_m=step,
                   bins=bins, steps=steps, ex=ex, ey=ey, kx=lattice.kx, ky=lattice.ky,
                   west=lattice.west, south=lattice.south, prof=prof)


def _masking_tan(h: Horizon, dx: float, dy: float, d: float) -> float:
    """Steepest terrain tan strictly nearer than `d` (half a step of slack)."""
    az = math.degrees(math.atan2(dx, dy)) % 360.0
    b = min(h.bins - 1, int(az * h.bins / 360.0))
    k = min(h.steps, max(0, int(d / h.step_m - 0.5)))
    return h.prof[b * (h.steps + 1) + k]


def visible(h: Horizon | None, lat: float, lon: float, alt_msl: float) -> bool:
    """Line of sight from the horizon's eye to a point at `alt_msl`; O(1).

    True when `h is None` (no horizon yet: clear line of sight, §5.2.4).
    Terrain within half a step of the target never masks it.
    """
    if h is None:
        return True
    dx = (lon - h.west) * h.kx - h.ex
    dy = (lat - h.south) * h.ky - h.ey
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return True
    t = (alt_msl - h.eye_msl_m - d * d / _RE_EFF_2) / d
    return t >= _masking_tan(h, dx, dy, d) - 1e-9


def coverage_fan(h: Horizon | None, range_m: float, ref_alt_msl: float, n: int = FAN_POINTS,
                 *, center: tuple[float, float] | None = None) -> list[list[float]]:
    """`n` [lat, lon] points bounding where a target at `ref_alt_msl` is seen.

    Each azimuth ends at `range_m` or at the first masked step, whichever is
    nearer. With no horizon it is the plain ring around `center`, or `[]`.
    """
    if n < 3 or range_m <= 0:
        raise ValueError("coverage_fan needs n >= 3 and a positive range")
    if h is None:
        if center is None:
            return []
        return [[round(p[0], 7), round(p[1], 7)]
                for p in (destination(center[0], center[1], range_m, 360.0 * i / n)
                          for i in range(n))]
    out: list[list[float]] = []
    for i in range(n):
        az = math.radians(360.0 * i / n)
        ux, uy = math.sin(az), math.cos(az)
        r, d = range_m, h.step_m
        while d < range_m:
            t = (ref_alt_msl - h.eye_msl_m - d * d / _RE_EFF_2) / d
            if t < _masking_tan(h, ux * d, uy * d, d) - 1e-9:
                r = max(0.0, d - h.step_m)
                break
            d += h.step_m
        x, y = h.ex + r * ux, h.ey + r * uy
        out.append([round(h.south + y / h.ky, 7), round(h.west + x / h.kx, 7)])
    return out


# ---------------------------------------------------------------- exposure ----
@dataclass(frozen=True, eq=False)
class Exposure:
    """Hazard rate (per second of flight) per lattice cell at one altitude."""

    lattice: Lattice
    rate: tuple[float, ...]
    alt_agl_m: float
    speed_mps: float
    threats: int
    threats_dropped: int = 0

    def rate_at(self, lat: float, lon: float) -> float:
        return self.rate[self.lattice.cell_of(lat, lon)]

    def rate_xy(self, x: float, y: float) -> float:
        return self.rate[self.lattice._index_xy(x, y)]

    @property
    def max_rate(self) -> float:
        return max(self.rate) if self.rate else 0.0


def _default_kernel() -> SimpleNamespace:
    """B1's notional tables: the one source of p_detect and p_kill_air (§5.2.3)."""
    from . import wargame_adjudicate as adj
    from . import wargame_tables as tables

    return SimpleNamespace(p_detect=adj.p_detect, p_kill_air=adj.p_kill_air,
                           DAMAGED_CYCLE_FACTOR=tables.DAMAGED_CYCLE_FACTOR,
                           DAMAGED_RANGE_FACTOR=tables.DAMAGED_RANGE_FACTOR)


def exposure_grid(lattice: Lattice, threats: Iterable[Sequence[Any]], alt_agl_m: float,
                  speed_mps: float, *, env: Mapping[str, float] | None = None,
                  kernel: Any = None) -> Exposure:
    """Hazard rate per cell for a drone at `alt_agl_m` over the lattice ground.

    Each threat is `(lat, lon, alt_hae_m, WgClass, damaged, horizon)`. A cell
    gains `p_detect * p_kill_air / cycle_s` from a threat only when the slant
    range is within `R` (the damaged range when damaged), `0 <= dz <= ceiling`
    with `dz` = drone HAE minus unit HAE, and the cell is `visible` from the
    threat. `cycle_s` doubles when damaged. Only the first `MAX_THREATS` are
    used (the caller orders them nearest first); the rest are counted.
    `kernel` defaults to `wargame_adjudicate` (B1); `env` defaults to clear.
    """
    if not (math.isfinite(alt_agl_m) and math.isfinite(speed_mps) and speed_mps > 0):
        raise ValueError("exposure_grid needs a finite altitude and a positive speed")
    k = kernel if kernel is not None else _default_kernel()
    env = dict(env or {})
    listed = list(threats)
    used, dropped = listed[:MAX_THREATS], max(0, len(listed) - MAX_THREATS)
    rate = [0.0] * lattice.cells
    cell = lattice.cell_m
    nx, ny, und = lattice.nx, lattice.ny, lattice.undulation_m
    drone_msl = [g + alt_agl_m for g in lattice.ground_msl]
    centers = [lattice.center(c) for c in range(lattice.cells)]
    for lat, lon, alt_hae, cls, damaged, hz in used:
        damaged = bool(damaged)
        r = float(cls.threat_range_m) * (k.DAMAGED_RANGE_FACTOR if damaged else 1.0)
        cycle = float(cls.cycle_s) * (k.DAMAGED_CYCLE_FACTOR if damaged else 1.0)
        ceiling = float(cls.threat_ceiling_m)
        if r <= 0 or cycle <= 0 or ceiling < 0:
            continue
        tx, ty = lattice.xy(lat, lon)
        i0, i1 = max(0, int((ty - r) // cell)), min(ny - 1, int((ty + r) // cell))
        j0, j1 = max(0, int((tx - r) // cell)), min(nx - 1, int((tx + r) // cell))
        for i in range(i0, i1 + 1):
            cy = (i + 0.5) * cell - ty
            for j in range(j0, j1 + 1):
                cx = (j + 0.5) * cell - tx
                h2 = cx * cx + cy * cy
                if h2 > r * r:
                    continue
                c = i * nx + j
                dz = drone_msl[c] + und - alt_hae
                if dz < 0 or dz > ceiling:
                    continue
                slant = math.sqrt(h2 + dz * dz)
                if slant > r:
                    continue
                if hz is not None and not visible(hz, *centers[c], drone_msl[c]):
                    continue
                pk = k.p_kill_air(cls, slant, dz, speed_mps, damaged)
                if pk <= 0:
                    continue
                rate[c] += k.p_detect(cls, slant, env) * pk / cycle
    return Exposure(lattice=lattice, rate=tuple(rate), alt_agl_m=float(alt_agl_m),
                    speed_mps=float(speed_mps), threats=len(used), threats_dropped=dropped)


def _segment_xy(exposure: Exposure, ax: float, ay: float, bx: float, by: float,
                speed: float) -> tuple[float, float]:
    """(integrated hazard, exposed seconds) flying a->b at `speed` (lattice XY).

    Samples every quarter cell at segment midpoints; the field is piecewise
    constant per cell, so this is the same sum the planner minimises.
    """
    length = math.hypot(bx - ax, by - ay)
    if length <= 0:
        return 0.0, 0.0
    n = max(1, math.ceil(length / (exposure.lattice.cell_m / 4.0)))
    dt = length / n / speed
    hazard = exposed = 0.0
    for s in range(n):
        t = (s + 0.5) / n
        r = exposure.rate_xy(ax + (bx - ax) * t, ay + (by - ay) * t)
        if r > 0:
            hazard += r * dt
            exposed += dt
    return hazard, exposed


def _point(p: Any) -> tuple[float, float]:
    """(lat, lon) from `(lat, lon)`, `[lat, lon]` or `{lat, lon}`."""
    if isinstance(p, Mapping):
        return float(p["lat"]), float(p["lon"])
    return float(p[0]), float(p[1])


def _leg_bucket(exposed_s: float, p_survive: float) -> str:
    if exposed_s <= 0:
        return "low"
    return "moderate" if p_survive >= LEG_MODERATE_P else "high"


def summarize(path: Sequence[Any], exposure: Exposure, speed: float) -> dict:
    """Length, time, exposure and notional survival of a path (§5.2.8, §3.9).

    `p_survive = exp(-H)` with `H` the integrated hazard; each leg (consecutive
    points) is bucketed low (0 s exposed), moderate (leg p_survive >= 0.95) or
    high. Lengths are great-circle metres; hazard is sampled in lattice XY.
    """
    if not (math.isfinite(speed) and speed > 0):
        raise ValueError("summarize needs a positive speed")
    pts = [_point(p) for p in path]
    lat_ = exposure.lattice
    legs: list[dict] = []
    total_len = total_h = total_exp = 0.0
    for a, b in pairwise(pts):
        length = safety.haversine_m(a[0], a[1], b[0], b[1])
        h, exp_s = _segment_xy(exposure, *lat_.xy(*a), *lat_.xy(*b), speed)
        p = math.exp(-h)
        legs.append({"from": [round(a[0], 7), round(a[1], 7)],
                     "to": [round(b[0], 7), round(b[1], 7)],
                     "length_m": round(length, 1), "eta_s": round(length / speed, 1),
                     "exposure_s": round(exp_s, 1), "p_survive": round(p, 4),
                     "exposure": _leg_bucket(exp_s, p)})
        total_len += length
        total_h += h
        total_exp += exp_s
    return {"length_m": round(total_len, 1), "eta_s": round(total_len / speed, 1),
            "exposure_s": round(total_exp, 1), "hazard": round(total_h, 6),
            "p_survive": round(math.exp(-total_h), 4), "legs": legs, "simulated": True}


# ----------------------------------------------------------------- planner ----
@dataclass(frozen=True)
class PlannedRoute:
    """A planned corridor: `path` runs from start to goal, both included."""

    path: tuple[tuple[float, float], ...]
    expansions: int
    raw_points: int
    decimated: int = 0  # points dropped past the 1.05x pull to meet MAX_WAYPOINTS

    def waypoints(self) -> list[dict]:
        return [{"lat": round(p[0], 7), "lon": round(p[1], 7)} for p in self.path]


def _point_seg_dist(px: float, py: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / seg2))
    return math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy))


def _orient(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _proper_cross(a, b, c, d) -> bool:
    """Segments ab and cd cross at a point interior to both."""
    return (_orient(a, b, c) * _orient(a, b, d) < 0) and (_orient(c, d, a) * _orient(c, d, b) < 0)


class _Fence:
    """The geofence in lattice XY: membership, edge margins and segment tests.

    Point membership is `safety.point_in_polygon` (the server's own test);
    segments are straight in lattice XY, the frame the drone flies in. A leg
    keeps `FENCE_BUFFER_M` from every fence edge, except that a leg may stay
    as close as its own endpoint already is (a drone parked near the fence).
    """

    def __init__(self, lattice: Lattice, fence_ll: list[tuple[float, float]]):
        self.ll = fence_ll
        self.active = len(fence_ll) >= 3
        poly = [lattice.xy(lat, lon) for lat, lon in fence_ll] if self.active else []
        self.edges = [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]

    def contains_ll(self, lat: float, lon: float) -> bool:
        return not self.active or safety.point_in_polygon(lat, lon, self.ll)

    def _contains_xy(self, x: float, y: float) -> bool:
        inside = False
        for (ax, ay), (bx, by) in self.edges:
            if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
                inside = not inside
        return inside

    def margin(self, x: float, y: float) -> float:
        if not self.active:
            return math.inf
        return min(_point_seg_dist(x, y, a, b) for a, b in self.edges)

    def segment_ok(self, a: tuple[float, float], b: tuple[float, float],
                   ma: float, mb: float) -> bool:
        if not self.active:
            return True
        need = min(FENCE_BUFFER_M, ma, mb)
        if ma + mb >= math.hypot(b[0] - a[0], b[1] - a[1]) + 2.0 * need:
            return True  # every point is within an endpoint's clear disc
        for c, d in self.edges:
            if _proper_cross(a, b, c, d):
                return False
            if need > 0 and min(_point_seg_dist(*a, c, d), _point_seg_dist(*b, c, d),
                                _point_seg_dist(*c, a, b), _point_seg_dist(*d, a, b)) < need - 1e-6:
                return False
        return self._contains_xy((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def _pull(raw: list[tuple[float, float]], margins: list[float], fence: _Fence,
          exposure: Exposure, speed: float, tol: float) -> list[int]:
    """String-pull `raw` (XY): accept a shortcut when its hazard is at most
    `tol` x the stretch it replaces and it stays inside the fence; returns the
    kept indices. Gives up on an anchor after `_PULL_PATIENCE` misses."""
    seg = [_segment_xy(exposure, *raw[k], *raw[k + 1], speed)[0] for k in range(len(raw) - 1)]
    prefix = [0.0]
    for h in seg:
        prefix.append(prefix[-1] + h)
    kept, i = [0], 0
    while i < len(raw) - 1:
        best, misses = i + 1, 0
        for j in range(i + 2, len(raw)):
            if fence.segment_ok(raw[i], raw[j], margins[i], margins[j]):
                h = _segment_xy(exposure, *raw[i], *raw[j], speed)[0]
                if h <= tol * (prefix[j] - prefix[i]) + 1e-9:
                    best, misses = j, 0
                    continue
            misses += 1
            if misses >= _PULL_PATIENCE:
                break
        kept.append(best)
        i = best
    return kept


def _decimate(kept: list[int], raw: list[tuple[float, float]], margins: list[float],
              fence: _Fence, exposure: Exposure, speed: float, limit: int) -> list[int]:
    """Drop interior points until at most `limit` remain, each time the one
    whose removal adds the least hazard while the new leg stays in the fence."""
    kept = list(kept)

    def hz(a: int, b: int) -> float:
        return _segment_xy(exposure, *raw[a], *raw[b], speed)[0]

    while len(kept) > limit:
        best: tuple[float, int] | None = None
        for k in range(1, len(kept) - 1):
            a, m, b = kept[k - 1], kept[k], kept[k + 1]
            if not fence.segment_ok(raw[a], raw[b], margins[a], margins[b]):
                continue
            added = hz(a, b) - hz(a, m) - hz(m, b)
            if best is None or added < best[0] - 1e-12:
                best = (added, k)
        if best is None:
            raise NoRoute("route_too_complex",
                          f"The corridor needs more than {limit} waypoints here.")
        del kept[best[1]]
    return kept


def plan(lattice: Lattice, exposure: Exposure, start: Sequence[float], goal: Sequence[float],
         geofence: Sequence[Sequence[float]] | None, *, alt_agl_m: float, speed_mps: float,
         w_hazard: float = W_HAZARD, max_expansions: int = MAX_EXPANSIONS) -> PlannedRoute:
    """Exposure-weighted 8-connected A* from `start` to `goal` (§5.2.8, R19).

    Edge cost is `L + w_hazard * H` (`H` = integrated hazard on the edge). The
    geofence is a hard mask: a cell is usable only when its centre is inside
    and `FENCE_BUFFER_M` clear of the edge, and every edge must stay inside.
    Start and goal are linked to nearby usable cells (and to each other when
    the straight leg is inside). The path is string-pulled (a shortcut is kept
    when its hazard is at most 1.05x the stretch it replaces); if more than
    `MAX_WAYPOINTS` points remain, the points whose removal adds the least
    hazard are dropped. Raises `NoRoute` (start or target outside the fence,
    no route, budget, too complex).
    """
    if not (math.isfinite(speed_mps) and speed_mps > 0):
        raise ValueError("plan needs a positive speed")
    if exposure.lattice is not lattice:
        raise ValueError("the exposure grid was built on a different lattice")
    if abs(exposure.alt_agl_m - float(alt_agl_m)) > 1e-6:
        raise ValueError("the exposure grid was built for a different altitude")
    s_ll, g_ll = _point(start), _point(goal)
    fence = _Fence(lattice, [_point(v) for v in (geofence or ())])
    if not fence.contains_ll(*s_ll):
        raise NoRoute("start_outside_fence", "The start point is outside the geofence.")
    if not fence.contains_ll(*g_ll):
        raise NoRoute("target_outside_fence", "The target is outside the geofence.")
    n, nx, ny, cell = lattice.cells, lattice.nx, lattice.ny, lattice.cell_m
    start_n, goal_n = n, n + 1
    xy = [lattice.center_xy(c) for c in range(n)] + [lattice.xy(*s_ll), lattice.xy(*g_ll)]
    margin = [fence.margin(*p) for p in xy]
    usable = bytearray(n + 2)
    for c in range(n):
        usable[c] = margin[c] >= FENCE_BUFFER_M and fence.contains_ll(*lattice.center(c))
    rate, gx, gy = exposure.rate, xy[goal_n][0], xy[goal_n][1]

    def leg_cost(u: int, v: int) -> float:
        length = math.hypot(xy[v][0] - xy[u][0], xy[v][1] - xy[u][1])
        return length + w_hazard * _segment_xy(exposure, *xy[u], *xy[v], speed_mps)[0]

    def links(node: int) -> dict[int, float]:
        """Usable cells around a start/goal point, widening the ring to 3."""
        i0, j0 = divmod(lattice._index_xy(*xy[node]), nx)
        for radius in (1, 2, 3):
            found: dict[int, float] = {}
            for i in range(max(0, i0 - radius), min(ny, i0 + radius + 1)):
                for j in range(max(0, j0 - radius), min(nx, j0 + radius + 1)):
                    c = i * nx + j
                    if usable[c] and fence.segment_ok(xy[node], xy[c], margin[node], margin[c]):
                        found[c] = leg_cost(node, c)
            if found:
                return found
        return {}

    start_links, goal_links = links(start_n), links(goal_n)
    if fence.segment_ok(xy[start_n], xy[goal_n], margin[start_n], margin[goal_n]):
        start_links[goal_n] = leg_cost(start_n, goal_n)
    g_score = [math.inf] * (n + 2)
    parent = [-1] * (n + 2)
    closed = bytearray(n + 2)
    g_score[start_n] = 0.0
    heap: list[tuple[float, int, int]] = [(math.hypot(xy[start_n][0] - gx, xy[start_n][1] - gy),
                                           0, start_n)]
    pushes = expansions = 0
    diag = cell * math.sqrt(2.0)
    while heap:
        _, _, u = heapq.heappop(heap)
        if closed[u]:
            continue
        if u == goal_n:
            break
        closed[u] = 1
        expansions += 1
        if expansions > max_expansions:
            raise NoRoute("plan_budget", "The corridor search ran out of budget; try again.")
        if u == start_n:
            edges = list(start_links.items())
        else:
            i, j = divmod(u, nx)
            edges = []
            for di, dj in _NEIGHBOURS:
                ii, jj = i + di, j + dj
                if not (0 <= ii < ny and 0 <= jj < nx):
                    continue
                v = ii * nx + jj
                if not usable[v] or closed[v]:
                    continue
                if not fence.segment_ok(xy[u], xy[v], margin[u], margin[v]):
                    continue
                length = diag if di and dj else cell
                edges.append((v, length * (1.0 + w_hazard * (rate[u] + rate[v]) * 0.5
                                           / speed_mps)))
            if u in goal_links:
                edges.append((goal_n, goal_links[u]))
        for v, cost in edges:
            ng = g_score[u] + cost
            if ng < g_score[v]:
                g_score[v], parent[v] = ng, u
                pushes += 1
                heapq.heappush(heap, (ng + math.hypot(xy[v][0] - gx, xy[v][1] - gy), pushes, v))
    if parent[goal_n] < 0:
        raise NoRoute("no_route", "No route to the target stays inside the geofence.")
    nodes = [goal_n]
    while nodes[-1] != start_n:
        nodes.append(parent[nodes[-1]])
    nodes.reverse()
    raw = [xy[v] for v in nodes]
    margins = [margin[v] for v in nodes]
    pulled = _pull(raw, margins, fence, exposure, speed_mps, SHORTCUT_TOLERANCE)
    kept = _decimate(pulled, raw, margins, fence, exposure, speed_mps, MAX_WAYPOINTS)
    path = [s_ll] + [lattice.latlon(*raw[k]) for k in kept[1:-1]] + [g_ll]
    return PlannedRoute(path=tuple(path), expansions=expansions, raw_points=len(raw),
                        decimated=len(pulled) - len(kept))


# -------------------------------------------------------------------- axes ----
def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from an engine `Unit` (attributes) or a plain mapping."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def red_axes(units: Any, objectives: Mapping[str, Any], *,
             classes: Mapping[str, Any] | None = None) -> list[dict]:
    """Surface axes of advance from red scenario units to their objectives.

    One row per red unit that is not destroyed, has an `objective`, and whose
    class moves (`speed_mps > 0`). The objective must be a blue engine unit
    that is not destroyed (R10: red `frc` -> blue `frc` only), so an axis can
    never point at a coordinate from mapped or real data (D1). `units` may be a
    mapping or an iterable of `Unit`s; `objectives` maps unit id -> `Unit`.
    `classes` defaults to `wargame_tables.CLASSES` (B1). Rows are ordered by
    `unit_id`; `eta_s = length_m / speed_mps`.
    """
    if classes is None:
        from .wargame_tables import CLASSES as classes
    pool = units.values() if isinstance(units, Mapping) else units
    out: list[dict] = []
    for unit in sorted(pool, key=lambda u: str(_field(u, "unit_id"))):
        if _field(unit, "side") != "red" or _field(unit, "state") == "destroyed":
            continue
        objective_id = _field(unit, "objective")
        target = objectives.get(objective_id) if objective_id else None
        if target is None or _field(target, "side") != "blue" \
                or _field(target, "state") == "destroyed":
            continue
        cls = classes.get(_field(unit, "wg_class"))
        speed = float(getattr(cls, "speed_mps", 0.0) or 0.0)
        if speed <= 0:
            continue
        a = (float(_field(unit, "lat")), float(_field(unit, "lon")))
        b = (float(_field(target, "lat")), float(_field(target, "lon")))
        length = safety.haversine_m(a[0], a[1], b[0], b[1])
        unit_id = str(_field(unit, "unit_id"))
        out.append({"id": f"axis-{unit_id}", "kind": "axis", "unit_id": unit_id,
                    "objective_id": str(objective_id),
                    "from": [round(a[0], 7), round(a[1], 7)],
                    "to": [round(b[0], 7), round(b[1], 7)],
                    "bearing_deg": round(safety.bearing_deg(a[0], a[1], b[0], b[1]), 1),
                    "length_m": round(length, 1), "speed_mps": speed,
                    "eta_s": round(length / speed, 1), "alt_band": ALT_BAND_SURFACE,
                    "simulated": True})
    return out


def destination(lat: float, lon: float, distance_m: float,
                bearing_deg: float) -> tuple[float, float]:
    """Great-circle point `distance_m` from (lat, lon) on `bearing_deg` true."""
    d = distance_m / EARTH_RADIUS_M
    th = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(max(-1.0, min(1.0, math.sin(p1) * math.cos(d)
                                 + math.cos(p1) * math.sin(d) * math.cos(th))))
    l2 = l1 + math.atan2(math.sin(th) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def ring_points(lat: float, lon: float, radius_m: float, n: int = RING_POINTS, *,
                geofence: Sequence[Sequence[float]] | None = None,
                blocked: Callable[[float, float], bool] | None = None) -> list[tuple[float, float]]:
    """The `relook` ring (§5.2.8): `n` points at `radius_m`, clockwise from
    north, minus those outside the geofence or where `blocked(lat, lon)` (the
    engine passes its exclusion test)."""
    fence = [_point(v) for v in (geofence or ())]
    out: list[tuple[float, float]] = []
    for i in range(n):
        p = destination(lat, lon, radius_m, 360.0 * i / n)
        if len(fence) >= 3 and not safety.point_in_polygon(p[0], p[1], fence):
            continue
        if blocked is not None and blocked(p[0], p[1]):
            continue
        out.append((round(p[0], 7), round(p[1], 7)))
    return out
