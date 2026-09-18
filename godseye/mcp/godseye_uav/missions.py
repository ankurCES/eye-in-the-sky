"""Mission doctrine: the geometry and the reasoning behind every mission plan.

This module is the *doctrine layer* (PLAN §4.3, M1/M2/M5/M7 + the M4 gate). It
is pure: it computes plans, prices them through the SAME fuel integrator the
flight loop uses (T5), and hands back a plan product. It never talks to AirSim
and never queues anything — `server.py` owns submission.

Doctrine that is *server-derived*, never taken from the caller:

  M1 grid search   `swath = 2*alt_agl*tan(HFOV/2)`, `spacing = swath*(1-overlap_pct)`.
                   The caller supplies `overlap_pct`; `lane_spacing` is NEVER a
                   caller knob. A plan that is truncated reports the spacing and
                   the coverage it ACTUALLY flies, plus why it was truncated.
  M2 recon route   captures are distance-triggered every
                   `along_track_swath*(1-forward_overlap_pct)` metres. A time
                   interval is permitted ONLY as a max-rate clamp, and when the
                   clamp binds the degraded overlap actually achieved is reported.
  M5 track target  standoff = the track's threat ring (order-of-battle weapon
                   envelope) floored by the narrow-FOV pixel density needed to
                   identify, then VERIFIED with a line-of-sight check.
  M7 identify      wide FOV to detect -> cross-cue to narrow FOV at a REDUCED
                   slant range for the identification pass.
  M4 gate          every plan is priced by `safety.FuelModel.preflight_gate`
                   before anything is queued; `dry_run()` produces the whole
                   plan product (waypoints, est_time_s, est_fuel_pct, coverage,
                   gate result) while executing nothing.

Coverage (§4.7 INTREP "coverage %") is produced by `coverage_of_path`, which
sweeps the sensor swath along a path — planned OR actually flown — and clips it
to the tasked polygon. It is the only coverage producer in the system.

ISR-ONLY (M14). Nothing here selects, prosecutes or recommends engagement.

Entry points the MCP layer calls:
    grid_search_plan / recon_route_plan / track_target_plan / identify_plan
    dry_run(plan, fuel, envelope, ...)   -> plan product + M4 gate, no execution
    coverage_of_path(polygon, path, swath_m) -> the INTREP coverage slot
    repath_track(plan, track, ...)       -> M5 server re-path loop
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from .safety import FuelModel, SafetyEnvelope, haversine_m

# ---------------------------------------------------------------------------
# Sensor model
# ---------------------------------------------------------------------------

#: Default ISR gimbal, wide field. Footprint drives grid spacing and capture
#: triggers (M1/M2) — never free knobs.
DEFAULT_HFOV_DEG = 60.0
DEFAULT_VFOV_DEG = 45.0
DEFAULT_OVERLAP = 0.20  # 20% side/forward overlap for recon coverage
DEFAULT_OVERLAP_PCT = DEFAULT_OVERLAP

#: Johnson-criteria discrimination levels, in pixels across the target's
#: longest dimension. These set how close the ID pass has to get (M5/M7).
DETECT_PIXELS = 3.0
RECOGNIZE_PIXELS = 8.0
IDENTIFY_PIXELS = 13.0

#: A plan larger than this is refused outright rather than silently capped —
#: silently capping is exactly the M1 defect this module exists to remove.
MAX_PLAN_WAYPOINTS = 4000

#: The same rule for the M2 capture schedule. `MAX_PLAN_WAYPOINTS` bounds the
#: waypoint list but NOT the capture list, and the two are independent: a
#: 2-waypoint route with a 1 m capture interval is a legal plan by waypoint
#: count and an unbounded one by capture count. Measured: a 2-waypoint,
#: 1000 km route at `forward_overlap_pct=0.99` materialised 1,001,881 capture
#: dicts and 405 MB of RSS inside a single `uav_mission` call, before the
#: geofence gate threw the plan away. Refused, never truncated.
MAX_PLAN_CAPTURES = 20_000

#: Floor for the identification pass's altitude (AGL). Descending to reduce
#: slant range stops here; the safety envelope's min_agl_m may be stricter.
MIN_ID_ALT_AGL_M = 30.0

#: How many altitudes the M7 cross-cue tries before it refuses (see
#: `id_altitude_band`). The pixel-optimal altitude is always the first.
ID_ALT_SEARCH_STEPS = 6


class PlanTooLargeError(ValueError):
    """The derived plan exceeds MAX_PLAN_WAYPOINTS. Raised, never truncated."""


class LosUnavailableError(RuntimeError):
    """M5 standoff could not be LOS-verified because no checker was supplied."""


class LosBlockedError(RuntimeError):
    """Every candidate observation point is masked — no usable arc exists."""


@dataclass(frozen=True)
class Camera:
    """A named sensor. The harness names a camera; the server owns the optics.

    `hfov_deg`/`vfov_deg` are the WIDE field used for search and detection.
    `narrow_hfov_deg` is what `uav_set_fov` is driven to for the M7 ID pass.
    `image_px_w` matches `targets.Observation.image_px` so the pixel-density
    maths here and the confidence maths in the intel chain agree.
    """

    name: str
    hfov_deg: float
    vfov_deg: float
    narrow_hfov_deg: float
    image_px_w: int = 640
    image_px_h: int = 480

    def to_dict(self) -> dict:
        return {
            "camera": self.name,
            "hfov_deg": self.hfov_deg,
            "vfov_deg": self.vfov_deg,
            "narrow_hfov_deg": self.narrow_hfov_deg,
            "image_px_w": self.image_px_w,
            "image_px_h": self.image_px_h,
        }


#: Named sensors. "0" is AirSim's default camera id and is the wide ISR gimbal.
CAMERAS: dict[str, Camera] = {
    "0": Camera("0", DEFAULT_HFOV_DEG, DEFAULT_VFOV_DEG, 5.0),
    "eo_wide": Camera("eo_wide", DEFAULT_HFOV_DEG, DEFAULT_VFOV_DEG, 5.0),
    "eo_narrow": Camera("eo_narrow", 12.0, 9.0, 2.0),
    "ir": Camera("ir", 45.0, 34.0, 6.0, image_px_w=640, image_px_h=512),
}
DEFAULT_CAMERA = "0"


def camera(name: str | Camera | None = None) -> Camera:
    """Resolve a camera name. An unknown name is refused, never defaulted."""
    if isinstance(name, Camera):
        return name
    if name is None:
        return CAMERAS[DEFAULT_CAMERA]
    key = str(name)
    if key not in CAMERAS:
        raise ValueError(
            f"unknown camera {key!r}; known cameras: {sorted(CAMERAS)}")
    return CAMERAS[key]


def _overlap_fraction(value: float, param: str) -> float:
    """Overlap is a FRACTION in [0, 1). 20 (meaning "20%") is refused loudly.

    An overlap silently coerced from 20 to 0.2 — or worse, used as 20 — would
    change the derived lane spacing by 100x without anyone noticing, which is
    the class of defect M1 is about.
    """
    v = float(value)
    if not (0.0 <= v < 1.0):
        raise ValueError(
            f"{param}={value!r} must be a fraction in [0, 1) — 0.20 means 20% "
            "overlap. Percent-style values (20) are refused so the derived "
            "spacing cannot be silently wrong (M1).")
    return v


# ---------------------------------------------------------------------------
# Footprint / resolution maths (M1, M2, M5, M7)
# ---------------------------------------------------------------------------

def footprint_m(alt_agl_m: float, hfov_deg: float = DEFAULT_HFOV_DEG,
                vfov_deg: float = DEFAULT_VFOV_DEG) -> tuple[float, float]:
    """Ground swath (cross-track width, along-track length) in metres at AGL."""
    if alt_agl_m <= 0.0:
        raise ValueError(f"alt_agl_m must be > 0 to have a footprint, got {alt_agl_m}")
    w = 2.0 * alt_agl_m * math.tan(math.radians(hfov_deg) / 2.0)
    h = 2.0 * alt_agl_m * math.tan(math.radians(vfov_deg) / 2.0)
    return w, h


def swath_m(alt_agl_m: float, hfov_deg: float = DEFAULT_HFOV_DEG) -> float:
    """Cross-track sensor swath: `2*alt_agl*tan(HFOV/2)` (M1)."""
    return footprint_m(alt_agl_m, hfov_deg)[0]


def lane_spacing_m(alt_agl_m: float, *, hfov_deg: float = DEFAULT_HFOV_DEG,
                   overlap_pct: float = DEFAULT_OVERLAP) -> float:
    """Sensor-derived lane spacing: `swath*(1-overlap_pct)` (M1).

    This is the ONLY place a lane spacing may come from. The caller supplies
    the overlap; a caller-supplied `lane_spacing` does not exist.
    """
    ov = _overlap_fraction(overlap_pct, "overlap_pct")
    return max(1.0, swath_m(alt_agl_m, hfov_deg) * (1.0 - ov))


def grid_sweep_spacing_m(alt_agl_m: float, hfov_deg: float = DEFAULT_HFOV_DEG,
                         overlap: float = DEFAULT_OVERLAP) -> float:
    """Back-compatible alias of `lane_spacing_m` (positional signature)."""
    return lane_spacing_m(alt_agl_m, hfov_deg=hfov_deg, overlap_pct=overlap)


def capture_interval_m(alt_agl_m: float, *, vfov_deg: float = DEFAULT_VFOV_DEG,
                       forward_overlap_pct: float = DEFAULT_OVERLAP) -> float:
    """Distance between captures: `along_track_swath*(1-forward_overlap)` (M2)."""
    ov = _overlap_fraction(forward_overlap_pct, "forward_overlap_pct")
    _, along = footprint_m(alt_agl_m, DEFAULT_HFOV_DEG, vfov_deg)
    return max(1.0, along * (1.0 - ov))


def ground_sample_distance_m(slant_range_m: float, fov_deg: float,
                             image_px: int) -> float:
    """Metres per pixel at a slant range for a given FOV and frame width."""
    if slant_range_m <= 0.0 or image_px <= 0:
        raise ValueError("slant_range_m and image_px must be > 0")
    return 2.0 * slant_range_m * math.tan(math.radians(fov_deg) / 2.0) / image_px


def pixels_on_target(target_size_m: float, slant_range_m: float,
                     fov_deg: float, image_px: int) -> float:
    """Pixels across the target's longest dimension.

    Identical maths to `targets.Observation.resolution_score`, so a plan's
    predicted pixel count and the intel chain's confidence maths agree.
    """
    gsd = ground_sample_distance_m(slant_range_m, fov_deg, image_px)
    return target_size_m / gsd


def max_slant_for_pixels(target_size_m: float, *, fov_deg: float,
                         image_px: int, min_pixels: float) -> float:
    """Furthest slant range at which `min_pixels` still fall on the target."""
    if min_pixels <= 0.0:
        raise ValueError("min_pixels must be > 0")
    if target_size_m <= 0.0:
        raise ValueError(
            "target_size_m must be > 0; an order-of-battle row with no size "
            "cannot support a pixel-density standoff (M5)")
    return (target_size_m * image_px) / (2.0 * min_pixels
                                         * math.tan(math.radians(fov_deg) / 2.0))


# ---------------------------------------------------------------------------
# Local planar helpers
# ---------------------------------------------------------------------------

def _pt(p) -> tuple[float, float]:
    """Accept a vertex as (lat, lon) tuple/list or {'lat','lon'} dict (JSON)."""
    if isinstance(p, Mapping):
        return float(p["lat"]), float(p["lon"])
    return float(p[0]), float(p[1])


def _centroid(polygon: Sequence) -> tuple[float, float]:
    pts = [_pt(p) for p in polygon]
    lat = sum(p[0] for p in pts) / len(pts)
    lon = sum(p[1] for p in pts) / len(pts)
    return lat, lon


def _m_per_deg(lat: float) -> tuple[float, float]:
    """Metres per degree (lat, lon) at a reference latitude."""
    m_lat = 111320.0
    m_lon = 111320.0 * math.cos(math.radians(lat))
    return m_lat, max(1e-6, m_lon)


def _wp(lat: float, lon: float, alt_agl_m: float, **extra) -> dict:
    """A waypoint. `alt_m` is kept for `safety`/`server`; `alt_agl_m` states
    the datum explicitly (TOOL_CONTRACT: never a bare altitude)."""
    return {"lat": lat, "lon": lon, "alt_m": alt_agl_m,
            "alt_agl_m": alt_agl_m, **extra}


def _dist_seg_m(px: float, py: float, ax: float, ay: float,
                bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _ring_contains(xs: list[float], ys: list[float], px: float, py: float) -> bool:
    inside = False
    n = len(xs)
    for i in range(n):
        j = (i + 1) % n
        if (ys[i] > py) != (ys[j] > py):
            x_cross = xs[i] + (py - ys[i]) * (xs[j] - xs[i]) / (ys[j] - ys[i])
            if px < x_cross:
                inside = not inside
    return inside


# ---------------------------------------------------------------------------
# Coverage (PLAN §4.7 "coverage %", the INTREP slot)
# ---------------------------------------------------------------------------

@dataclass
class CoverageEstimate:
    """How much of the tasked polygon a path's sensor swath actually images.

    Serializes straight into the INTREP `coverage` section
    (`targets.intrep_report`): planned_area_km2, covered_area_km2,
    coverage_pct, method.
    """

    planned_area_km2: float
    covered_area_km2: float
    coverage_pct: float
    method: str
    swath_m: float
    cell_m: float
    basis: str = "planned"   # "planned" (from the plan) | "flown" (from telemetry)

    @property
    def uncovered_km2(self) -> float:
        return max(0.0, self.planned_area_km2 - self.covered_area_km2)

    def to_dict(self) -> dict:
        return {
            "planned_area_km2": round(self.planned_area_km2, 6),
            "covered_area_km2": round(self.covered_area_km2, 6),
            "coverage_pct": round(self.coverage_pct, 2),
            "uncovered_km2": round(self.uncovered_km2, 6),
            "method": self.method,
            "swath_m": round(self.swath_m, 2),
            "cell_m": round(self.cell_m, 2),
            "basis": self.basis,
        }


def polygon_area_m2(polygon: Sequence) -> float:
    """Shoelace area of a lat/lon ring, projected to local metres."""
    pts = [_pt(p) for p in polygon]
    if len(pts) < 3:
        raise ValueError("a polygon needs >= 3 vertices to have an area")
    lat0, _ = _centroid(pts)
    m_lat, m_lon = _m_per_deg(lat0)
    xs = [(p[1] - pts[0][1]) * m_lon for p in pts]
    ys = [(p[0] - pts[0][0]) * m_lat for p in pts]
    acc = 0.0
    n = len(pts)
    for i in range(n):
        j = (i + 1) % n
        acc += xs[i] * ys[j] - xs[j] * ys[i]
    return abs(acc) / 2.0


def coverage_of_path(polygon: Sequence, path: Sequence, swath_width_m: float,
                     *, cell_m: float | None = None, max_cells: int = 250_000,
                     basis: str = "planned") -> CoverageEstimate:
    """Fraction of `polygon` imaged by sweeping `swath_width_m` along `path`.

    The single coverage producer in the system (PLAN §4.7). Works on a planned
    route or on a flown track from telemetry — pass `basis="flown"` for the
    latter, which is what the INTREP should carry after the fact.

    The polygon is sampled on a regular grid in local metres; a cell counts as
    imaged when its centre lies within `swath_width_m/2` of the path. The cell
    size and the method are reported so the number can be audited rather than
    trusted.
    """
    pts = [_pt(p) for p in polygon]
    if len(pts) < 3:
        raise ValueError("coverage needs a polygon of >= 3 vertices")
    if swath_width_m <= 0.0:
        raise ValueError("swath_width_m must be > 0")
    track = [_pt(p) for p in path]

    lat0, lon0 = _centroid(pts)
    m_lat, m_lon = _m_per_deg(lat0)
    xs = [(p[1] - lon0) * m_lon for p in pts]
    ys = [(p[0] - lat0) * m_lat for p in pts]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    w = max(1e-6, x_max - x_min)
    h = max(1e-6, y_max - y_min)

    if cell_m is None:
        cell = swath_width_m / 4.0
    else:
        cell = float(cell_m)
        if cell <= 0.0:
            # `if cell_m else` silently re-derived the default from a 0 the
            # caller meant literally; a zero sampling cell is an error.
            raise ValueError(f"cell_m must be > 0 when supplied, got {cell_m!r}")
    cell = max(cell, math.sqrt((w * h) / max(1, max_cells)), 0.5)
    nx = max(1, math.ceil(w / cell))
    ny = max(1, math.ceil(h / cell))

    inside: set[tuple[int, int]] = set()
    for i in range(nx):
        cx = x_min + (i + 0.5) * cell
        for j in range(ny):
            cy = y_min + (j + 0.5) * cell
            if _ring_contains(xs, ys, cx, cy):
                inside.add((i, j))
    if not inside:
        raise ValueError(
            "polygon sampled to zero cells — it is degenerate or smaller than "
            f"the {cell:.1f} m sampling cell; coverage cannot be reported")

    r = swath_width_m / 2.0
    tx = [(p[1] - lon0) * m_lon for p in track]
    ty = [(p[0] - lat0) * m_lat for p in track]
    covered: set[tuple[int, int]] = set()
    segments = max(0, len(track) - 1)
    for k in range(max(1, len(track))):
        if segments:
            if k >= segments:
                break
            ax, ay, bx, by = tx[k], ty[k], tx[k + 1], ty[k + 1]
        elif track:
            ax = bx = tx[0]
            ay = by = ty[0]
        else:
            break
        i0 = max(0, int((min(ax, bx) - r - x_min) / cell))
        i1 = min(nx - 1, int((max(ax, bx) + r - x_min) / cell))
        j0 = max(0, int((min(ay, by) - r - y_min) / cell))
        j1 = min(ny - 1, int((max(ay, by) + r - y_min) / cell))
        for i in range(i0, i1 + 1):
            cx = x_min + (i + 0.5) * cell
            for j in range(j0, j1 + 1):
                if (i, j) not in inside or (i, j) in covered:
                    continue
                cy = y_min + (j + 0.5) * cell
                if _dist_seg_m(cx, cy, ax, ay, bx, by) <= r:
                    covered.add((i, j))

    cell_area = cell * cell
    planned = len(inside) * cell_area
    covered_area = len(covered) * cell_area
    pct = 100.0 * len(covered) / len(inside)
    method = (f"swath sweep: {swath_width_m:.1f} m swath along "
              f"{max(0, len(track) - 1)} leg(s), sampled on a {cell:.1f} m grid "
              f"({len(inside)} cells in the AO, {len(covered)} imaged)")
    return CoverageEstimate(
        planned_area_km2=planned / 1e6,
        covered_area_km2=covered_area / 1e6,
        coverage_pct=pct,
        method=method,
        swath_m=swath_width_m,
        cell_m=cell,
        basis=basis,
    )


def flown_coverage(polygon: Sequence, flown_path: Sequence, alt_agl_m: float,
                   *, camera_name: str | Camera = DEFAULT_CAMERA,
                   cell_m: float | None = None) -> CoverageEstimate:
    """Coverage actually achieved by a flown track (the INTREP producer).

    `flown_path` is the telemetry breadcrumb trail; `alt_agl_m` the altitude it
    was flown at. Returns the same `CoverageEstimate` shape as the planner, so
    planned-vs-flown is a like-for-like comparison.
    """
    cam = camera(camera_name)
    return coverage_of_path(polygon, flown_path, swath_m(alt_agl_m, cam.hfov_deg),
                            cell_m=cell_m, basis="flown")


# ---------------------------------------------------------------------------
# Waypoint generators
# ---------------------------------------------------------------------------

def lawnmower_waypoints(polygon: Sequence, alt_agl_m: float,
                        hfov_deg: float = DEFAULT_HFOV_DEG,
                        overlap: float = DEFAULT_OVERLAP,
                        *, max_lanes: int | None = None) -> list[dict]:
    """Boustrophedon coverage of a lat/lon polygon's bounding box (M1).

    Sweeps run E-W, stepping N-S by the footprint-derived spacing. `max_lanes`
    truncates the plan — the caller that truncates is responsible for reporting
    the spacing and coverage actually flown; `grid_search_plan` does that.
    """
    geom = _lawnmower_geometry(polygon, alt_agl_m, hfov_deg, overlap,
                              max_lanes=max_lanes)
    return geom["waypoints"]


def _lawnmower_geometry(polygon: Sequence, alt_agl_m: float, hfov_deg: float,
                        overlap: float, *, max_lanes: int | None) -> dict:
    """Lanes + the spacing that is genuinely flown (never the derived one)."""
    pts = [_pt(p) for p in polygon]
    if len(pts) < 3:
        raise ValueError("grid_search needs a polygon of >=3 vertices")
    clat, _ = _centroid(pts)
    m_lat, _ = _m_per_deg(clat)
    derived = lane_spacing_m(alt_agl_m, hfov_deg=hfov_deg, overlap_pct=overlap)

    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)

    span_m = (lat_max - lat_min) * m_lat
    full_lanes = max(1, math.ceil(span_m / derived) + 1)
    lanes = full_lanes
    truncated = False
    if max_lanes is not None:
        if int(max_lanes) < 1:
            raise ValueError("max_lanes must be >= 1 when supplied")
        lanes = min(full_lanes, int(max_lanes))
        truncated = lanes < full_lanes
    if lanes * 2 > MAX_PLAN_WAYPOINTS:
        raise PlanTooLargeError(
            f"lawnmower over this AO needs {lanes} lanes ({lanes * 2} waypoints) "
            f"at {alt_agl_m:.0f} m AGL with {derived:.1f} m spacing, over the "
            f"{MAX_PLAN_WAYPOINTS}-waypoint limit. Raise alt_agl_m, reduce "
            "overlap_pct, or split the AO — the plan is NOT silently capped.")

    dlat = (lat_max - lat_min) / (lanes - 1) if lanes > 1 else 0.0
    # The spacing the aircraft genuinely flies. When the plan is truncated the
    # lanes are spread across the whole box, so this is WIDER than the derived
    # sensor spacing and coverage is correspondingly thinner (M1). A one-lane
    # plan has no inter-lane spacing; the derived figure stands and `coverage`
    # carries the real answer.
    actual = dlat * m_lat if lanes > 1 else derived

    wps: list[dict] = []
    for i in range(lanes):
        lat = lat_min + i * dlat
        a, b = (lon_min, lon_max) if i % 2 == 0 else (lon_max, lon_min)
        wps.append(_wp(lat, a, alt_agl_m))
        wps.append(_wp(lat, b, alt_agl_m))
    return {
        "waypoints": wps, "lanes": lanes, "full_lanes": full_lanes,
        "derived_spacing_m": derived, "actual_spacing_m": actual,
        "truncated": truncated, "span_m": span_m,
        "bbox": (lat_min, lon_min, lat_max, lon_max),
    }


def expanding_square_waypoints(center: tuple[float, float], alt_agl_m: float,
                               *, hfov_deg: float = DEFAULT_HFOV_DEG,
                               overlap: float = DEFAULT_OVERLAP,
                               legs: int = 12) -> list[dict]:
    """SAR expanding square from a datum (PLAN §5), leg growth = lane spacing."""
    if legs < 2:
        raise ValueError("expanding-square needs >= 2 legs")
    if legs > MAX_PLAN_WAYPOINTS:
        raise PlanTooLargeError(f"{legs} legs exceeds {MAX_PLAN_WAYPOINTS}")
    step = lane_spacing_m(alt_agl_m, hfov_deg=hfov_deg, overlap_pct=overlap)
    lat, lon = float(center[0]), float(center[1])
    m_lat, m_lon = _m_per_deg(lat)
    headings = ((0.0, 1.0), (-1.0, 0.0), (0.0, -1.0), (1.0, 0.0))  # E, S, W, N
    wps = [_wp(lat, lon, alt_agl_m)]
    n_off = e_off = 0.0
    for i in range(legs):
        dn, de = headings[i % 4]
        length = step * (i // 2 + 1)
        n_off += dn * length
        e_off += de * length
        wps.append(_wp(lat + n_off / m_lat, lon + e_off / m_lon, alt_agl_m))
    return wps


def orbit_waypoints(lat: float, lon: float, alt_agl_m: float, radius_m: float,
                    points: int = 12, sun_azimuth_deg: float | None = None,
                    *, start_deg: float | None = None) -> list[dict]:
    """Standoff orbit ring around a POI (M3). When a sun azimuth is known the
    ring starts on the sun side so the gimbal looks away from glare (M6)."""
    if radius_m <= 0.0:
        raise ValueError("orbit radius_m must be > 0")
    if points < 3:
        raise ValueError("an orbit ring needs >= 3 points")
    m_lat, m_lon = _m_per_deg(lat)
    start = 0.0
    if start_deg is not None:
        start = math.radians(start_deg)
    elif sun_azimuth_deg is not None:
        start = math.radians(sun_azimuth_deg)
    wps: list[dict] = []
    for i in range(points):
        a = start + (2.0 * math.pi * i / points)
        wps.append(_wp(lat + radius_m * math.cos(a) / m_lat,
                       lon + radius_m * math.sin(a) / m_lon,
                       alt_agl_m, bearing_from_poi_deg=round(math.degrees(a) % 360.0, 1)))
    return wps


# ---------------------------------------------------------------------------
# The plan object
# ---------------------------------------------------------------------------

@dataclass
class MissionPlan:
    """A costed, doctrine-derived plan. Nothing here has been executed."""

    kind: str
    vehicle: str
    waypoints: list[dict] = field(default_factory=list)
    alt_m: float = 60.0
    speed_mps: float = 10.0
    meta: dict = field(default_factory=dict)
    coverage: CoverageEstimate | None = None
    captures: list[dict] = field(default_factory=list)
    phases: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False
    truncation_reason: str | None = None

    @property
    def alt_agl_m(self) -> float:
        """Altitude datum, stated (TOOL_CONTRACT: never a bare altitude)."""
        return self.alt_m

    def to_route(self) -> list[dict]:
        return self.waypoints

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "vehicle": self.vehicle,
            "alt_agl_m": self.alt_m,
            "speed_mps": self.speed_mps,
            "waypoints": self.waypoints,
            "waypoint_count": len(self.waypoints),
            "coverage": self.coverage.to_dict() if self.coverage else None,
            "captures": self.captures,
            "capture_count": len(self.captures),
            "phases": self.phases,
            "truncated": self.truncated,
            "truncation_reason": self.truncation_reason,
            "warnings": list(self.warnings),
            **{k: v for k, v in self.meta.items() if not k.startswith("_")},
        }


# ---------------------------------------------------------------------------
# M1 — grid search
# ---------------------------------------------------------------------------

def grid_search_plan(vehicle: str, polygon: Sequence, alt_agl_m: float, *,
                     overlap_pct: float = DEFAULT_OVERLAP,
                     camera_name: str | Camera = DEFAULT_CAMERA,
                     pattern: str = "lawnmower",
                     speed_mps: float = 8.0,
                     max_lanes: int | None = None,
                     legs: int = 12,
                     coverage_cell_m: float | None = None) -> MissionPlan:
    """Area search with sensor-derived lane spacing (M1).

    `overlap_pct` is the only coverage knob the caller has; the spacing comes
    from `swath*(1-overlap_pct)`. When `max_lanes` truncates the plan, the
    returned `lane_spacing_m` is the spacing ACTUALLY FLOWN and `coverage` is
    the fraction of the AO that spacing genuinely images — the plan never
    reports the uncapped spacing it did not fly.
    """
    cam = camera(camera_name)
    ov = _overlap_fraction(overlap_pct, "overlap_pct")
    pattern = (pattern or "lawnmower").lower()
    sw = swath_m(alt_agl_m, cam.hfov_deg)
    warnings: list[str] = []

    if pattern == "lawnmower":
        geom = _lawnmower_geometry(polygon, alt_agl_m, cam.hfov_deg, ov,
                                   max_lanes=max_lanes)
        wps = geom["waypoints"]
        actual_spacing = geom["actual_spacing_m"]
        truncated = geom["truncated"]
        reason = None
        if truncated:
            reason = (
                f"max_lanes={max_lanes} truncated the plan from "
                f"{geom['full_lanes']} lanes to {geom['lanes']}; the lanes were "
                f"spread over the whole {geom['span_m']:.0f} m span, so the "
                f"spacing actually flown is {actual_spacing:.1f} m against a "
                f"{geom['derived_spacing_m']:.1f} m sensor-derived spacing "
                f"({sw:.1f} m swath)")
            warnings.append("coverage_thinned: " + reason)
    elif pattern in ("expanding-square", "expanding_square"):
        geom = None
        wps = expanding_square_waypoints(_centroid(polygon), alt_agl_m,
                                         hfov_deg=cam.hfov_deg, overlap=ov,
                                         legs=legs)
        actual_spacing = lane_spacing_m(alt_agl_m, hfov_deg=cam.hfov_deg,
                                        overlap_pct=ov)
        truncated = False
        reason = None
    else:
        raise ValueError(
            f"unknown grid pattern {pattern!r}; expected 'lawnmower' or "
            "'expanding-square'")

    if len(wps) > MAX_PLAN_WAYPOINTS:
        raise PlanTooLargeError(
            f"grid plan is {len(wps)} waypoints, over the "
            f"{MAX_PLAN_WAYPOINTS} limit; it is refused, not capped")

    cov = coverage_of_path(polygon, wps, sw, cell_m=coverage_cell_m,
                           basis="planned")
    if cov.coverage_pct < 99.0 and not truncated:
        warnings.append(
            f"planned coverage is {cov.coverage_pct:.1f}% of the AO — the "
            "pattern does not image the whole tasked area")

    meta = {
        "doctrine": "grid-search",
        "pattern": pattern,
        "camera": cam.to_dict(),
        "overlap_pct": ov,
        "swath_m": round(sw, 2),
        "footprint_m": [round(v, 2) for v in footprint_m(alt_agl_m, cam.hfov_deg,
                                                         cam.vfov_deg)],
        "lane_spacing_m": round(actual_spacing, 2),
        "lane_spacing_derived_m": round(lane_spacing_m(
            alt_agl_m, hfov_deg=cam.hfov_deg, overlap_pct=ov), 2),
        "lanes": (geom["lanes"] if geom else None),
        "lanes_required": (geom["full_lanes"] if geom else None),
        # Retained for the legacy uav_mission return; it is the SAME number as
        # lane_spacing_m so the old key can no longer disagree with the plan.
        "sweep_spacing_m": round(actual_spacing, 2),
        "_plan_params": {
            "polygon": [list(_pt(p)) for p in polygon], "alt_agl_m": alt_agl_m,
            "overlap_pct": ov, "camera_name": cam.name, "pattern": pattern,
            "speed_mps": speed_mps, "max_lanes": max_lanes, "legs": legs,
        },
    }
    return MissionPlan("grid_search", vehicle, wps, alt_agl_m, speed_mps, meta,
                       coverage=cov, warnings=warnings, truncated=truncated,
                       truncation_reason=reason)


# ---------------------------------------------------------------------------
# M2 — recon route with distance-triggered captures
# ---------------------------------------------------------------------------

def route_length_m(waypoints: Sequence) -> float:
    """Arc length of a lat/lon route in local metres."""
    pts = [_pt(p) for p in waypoints]
    if len(pts) < 2:
        return 0.0
    lat0, _ = _centroid(pts)
    m_lat, m_lon = _m_per_deg(lat0)
    return sum(math.hypot((b[0] - a[0]) * m_lat, (b[1] - a[1]) * m_lon)
               for a, b in zip(pts, pts[1:]))


def capture_points(waypoints: Sequence, interval_m: float) -> list[dict]:
    """Geo points at which a capture fires, every `interval_m` along the route.

    Distance-triggered, per M2 — the trigger is arc length flown, not a clock.

    The schedule is SIZED BEFORE IT IS BUILT: a route long enough (or an
    interval short enough) to exceed `MAX_PLAN_CAPTURES` is refused, not
    truncated and not quietly materialised. Building it first and discarding it
    later is how one `uav_mission` call turned into 405 MB of capture dicts.
    """
    if interval_m <= 0.0:
        raise ValueError("capture interval_m must be > 0")
    pts = [_pt(p) for p in waypoints]
    if len(pts) < 2:
        raise ValueError("a recon route needs >= 2 waypoints to trigger captures")
    total_m = route_length_m(pts)
    n_captures = 1 + int(total_m // interval_m)
    if n_captures > MAX_PLAN_CAPTURES:
        raise PlanTooLargeError(
            f"a capture every {interval_m:.2f} m along a {total_m / 1000.0:.1f} km "
            f"route is {n_captures} captures, over the {MAX_PLAN_CAPTURES} "
            "limit. Raise alt_agl_m, lower forward_overlap_pct, lower "
            "max_capture_rate_hz, or split the route — the schedule is NOT "
            "silently capped.")
    lat0, _ = _centroid(pts)
    m_lat, m_lon = _m_per_deg(lat0)
    out = [{"lat": pts[0][0], "lon": pts[0][1], "along_track_m": 0.0, "leg": 0}]
    travelled = 0.0
    next_trigger = interval_m
    for k in range(len(pts) - 1):
        a, b = pts[k], pts[k + 1]
        dn = (b[0] - a[0]) * m_lat
        de = (b[1] - a[1]) * m_lon
        leg = math.hypot(dn, de)
        if leg <= 0.0:
            continue
        while travelled + leg >= next_trigger:
            f = (next_trigger - travelled) / leg
            out.append({"lat": a[0] + (b[0] - a[0]) * f,
                        "lon": a[1] + (b[1] - a[1]) * f,
                        "along_track_m": round(next_trigger, 2), "leg": k})
            next_trigger += interval_m
        travelled += leg
    return out


def recon_route_plan(vehicle: str, waypoints: Sequence, alt_agl_m: float, *,
                     forward_overlap_pct: float = DEFAULT_OVERLAP,
                     camera_name: str | Camera = DEFAULT_CAMERA,
                     speed_mps: float = 10.0,
                     max_capture_rate_hz: float = 1.0) -> MissionPlan:
    """Route recon with distance-triggered captures (M2).

    Captures fire every `along_track_swath*(1-forward_overlap_pct)` metres.
    `max_capture_rate_hz` is a MAX-RATE CLAMP only: it can only slow the
    trigger down. When it binds, the plan reports the widened interval and the
    forward overlap actually achieved, which may be negative (gaps in the
    strip) — it is never silently accepted as if the requested overlap held.
    """
    cam = camera(camera_name)
    ov = _overlap_fraction(forward_overlap_pct, "forward_overlap_pct")
    route = [_wp(*_pt(p), float(
        p.get("alt_agl_m", p.get("alt_m", alt_agl_m)) if isinstance(p, Mapping)
        else alt_agl_m)) for p in waypoints]
    if len(route) < 2:
        raise ValueError("recon_route needs >= 2 waypoints")
    if len(route) > MAX_PLAN_WAYPOINTS:
        raise PlanTooLargeError(f"recon route is {len(route)} waypoints")

    _, along = footprint_m(alt_agl_m, cam.hfov_deg, cam.vfov_deg)
    want_m = capture_interval_m(alt_agl_m, vfov_deg=cam.vfov_deg,
                                forward_overlap_pct=ov)
    warnings: list[str] = []
    off_alt = sorted({w["alt_agl_m"] for w in route
                      if abs(w["alt_agl_m"] - float(alt_agl_m)) > 1e-6})
    if off_alt:
        warnings.append(
            f"mixed_altitude_route: the capture interval is derived at "
            f"{alt_agl_m:.0f} m AGL, but legs are flown at {off_alt} m — the "
            "forward overlap on those legs is not the requested one")

    if max_capture_rate_hz <= 0.0:
        raise ValueError("max_capture_rate_hz must be > 0")
    min_period_s = 1.0 / float(max_capture_rate_hz)
    clamp_m = min_period_s * max(0.1, float(speed_mps))
    interval = want_m
    clamped = False
    if clamp_m > want_m:
        clamped = True
        interval = clamp_m
        warnings.append(
            f"capture_rate_clamped: {forward_overlap_pct:.0%} forward overlap "
            f"wants a capture every {want_m:.1f} m, but the "
            f"{max_capture_rate_hz:.2f} Hz max rate at {speed_mps:.1f} m/s "
            f"allows one only every {clamp_m:.1f} m")
    achieved_overlap = 1.0 - interval / along
    if achieved_overlap < 0.0:
        warnings.append(
            f"forward_coverage_gap: consecutive frames are {interval:.1f} m "
            f"apart with a {along:.1f} m along-track footprint — "
            f"{-achieved_overlap:.0%} of the strip between frames is unimaged")

    caps = capture_points(route, interval)
    for c in caps:
        c["camera"] = cam.name
        c["fov_deg"] = cam.hfov_deg
        c["alt_agl_m"] = alt_agl_m

    sw = swath_m(alt_agl_m, cam.hfov_deg)
    meta = {
        "doctrine": "route-recon",
        "camera": cam.to_dict(),
        "forward_overlap_pct": ov,
        "forward_overlap_achieved_pct": round(achieved_overlap, 4),
        "capture_every_m": round(interval, 2),
        "capture_interval_requested_m": round(want_m, 2),
        "capture_interval_s_at_speed": round(interval / max(0.1, speed_mps), 2),
        "max_capture_rate_hz": float(max_capture_rate_hz),
        "capture_rate_clamped": clamped,
        "capture_trigger": "distance",
        "along_track_swath_m": round(along, 2),
        "swath_m": round(sw, 2),
        "_plan_params": {
            "waypoints": [[w["lat"], w["lon"]] for w in route],
            "alt_agl_m": alt_agl_m, "forward_overlap_pct": ov,
            "camera_name": cam.name, "speed_mps": speed_mps,
            "max_capture_rate_hz": max_capture_rate_hz,
        },
    }
    return MissionPlan("recon_route", vehicle, route, alt_agl_m, speed_mps, meta,
                       captures=caps, warnings=warnings)


# ---------------------------------------------------------------------------
# M5 — track target: server-derived standoff, LOS-verified
# ---------------------------------------------------------------------------

#: `los_check(from_lat, from_lon, from_alt_agl_m, to_lat, to_lon, to_alt_agl_m)`
#: -> mapping carrying at least `{"los": bool}`. `server.py` adapts
#: `uav_los_check` / `simTestLineOfSightBetweenPoints` to this shape.
LosCheck = Callable[[float, float, float, float, float, float], Mapping]


def _target_size_m(ob) -> float:
    """The contact's physical size, or a loud refusal (M5/M7).

    Every pixel-density number in this module — the ID standoff, the detect
    standoff, `id_achievable` — is `target_size_m / gsd`. An order-of-battle row
    with no size cannot support any of them, so it is refused here rather than
    silently replaced by a nominal 1 m target whose numbers would be reported
    as if measured.
    """
    size = float(getattr(ob, "size_m", 0.0) or 0.0)
    if size <= 0.0:
        raise ValueError(
            f"order-of-battle class {getattr(ob, 'key', '?')!r} carries no "
            "size_m; the M5/M7 pixel-density standoff cannot be derived for it. "
            "Fix the OB row — a substituted nominal size would be reported as a "
            "measured pixels-on-target figure.")
    return size


def _require_los(result: Mapping, where: str) -> bool:
    if not isinstance(result, Mapping) or "los" not in result:
        raise ValueError(
            f"los_check returned {result!r} for {where}; it must be a mapping "
            "with a 'los' boolean. A LOS check the planner cannot read is "
            "worse than none (TOOL_CONTRACT §4.2).")
    return bool(result["los"])


def standoff_for_track(track, *, alt_agl_m: float,
                       camera_name: str | Camera = DEFAULT_CAMERA,
                       min_pixels_on_target: float = IDENTIFY_PIXELS,
                       use_narrow_fov: bool = True) -> dict:
    """Server-derived observation standoff for a track (M5).

    Two independent constraints:
      * the threat ring — `threat.standoff_m(track)`, i.e. the contact's
        order-of-battle weapon envelope plus a 10% margin. This is a FLOOR and
        is never traded away for a better picture (ISR-only, M14).
      * pixel density — the furthest slant range at which `min_pixels_on_target`
        still fall across the contact, at the FOV the ID pass will use.

    When the threat ring puts the contact beyond the sensor's identification
    range the result says so (`id_achievable: False`, `limiting_factor`) rather
    than quietly closing inside the ring or quietly claiming an ID is possible.
    """
    from .threat import standoff_m as threat_standoff_m

    cam = camera(camera_name)
    fov = cam.narrow_hfov_deg if use_narrow_fov else cam.hfov_deg
    ob = track.ob
    # No silent fallback: substituting a 1 m target for an OB row with no size
    # made `max_slant_for_pixels`'s own "a row with no size cannot support a
    # pixel-density standoff (M5)" error unreachable, and returned an
    # id_achievable verdict computed against a target that does not exist.
    size = _target_size_m(ob)
    ring = float(threat_standoff_m(track))
    id_max_slant = max_slant_for_pixels(
        size, fov_deg=fov,
        image_px=cam.image_px_w, min_pixels=min_pixels_on_target)

    ground = ring
    slant = math.hypot(ground, float(alt_agl_m))
    px = pixels_on_target(size, slant, fov, cam.image_px_w)
    achievable = slant <= id_max_slant
    if achievable:
        limiting = "threat_ring"
    else:
        limiting = "sensor_resolution"
    return {
        "track_id": getattr(track, "track_id", None),
        "ob_class": ob.key,
        "target_size_m": ob.size_m,
        "weapon_range_m": ob.weapon_range_m,
        "weapon_ceiling_m": ob.weapon_ceiling_m,
        "threat_ring_m": round(ring, 1),
        "standoff_m": round(ground, 1),
        "alt_agl_m": float(alt_agl_m),
        "slant_range_m": round(slant, 1),
        "fov_deg": fov,
        "image_px": cam.image_px_w,
        "min_pixels_on_target": float(min_pixels_on_target),
        "expected_pixels_on_target": round(px, 2),
        "id_max_slant_m": round(id_max_slant, 1),
        "id_achievable": achievable,
        "limiting_factor": limiting,
        "basis": (
            f"standoff floored by the {ob.name} engagement envelope "
            f"({ob.weapon_range_m:.0f} m + 10% = {ring:.0f} m); at "
            f"{alt_agl_m:.0f} m AGL that is a {slant:.0f} m slant, giving "
            f"{px:.1f} px across a {ob.size_m:.1f} m target at {fov:.1f} deg "
            f"FOV (need {min_pixels_on_target:.0f} px, identification reaches "
            f"{id_max_slant:.0f} m)"),
        "above_weapon_ceiling": (ob.weapon_ceiling_m > 0.0
                                 and float(alt_agl_m) > ob.weapon_ceiling_m),
    }


def verify_orbit_los(ring: Sequence[dict], target: tuple[float, float, float],
                     los_check: LosCheck) -> dict:
    """Run the LOS check from every ring point to the contact (M5)."""
    clear: list[int] = []
    blocked: list[dict] = []
    for i, wp in enumerate(ring):
        res = los_check(wp["lat"], wp["lon"], wp["alt_m"],
                        target[0], target[1], target[2])
        if _require_los(res, f"ring point {i}"):
            clear.append(i)
        else:
            blocked.append({"index": i, "lat": wp["lat"], "lon": wp["lon"],
                            "detail": {k: v for k, v in res.items() if k != "los"}})
    return {
        "verified": True,
        "points_checked": len(ring),
        "points_clear": len(clear),
        "clear_indices": clear,
        "blocked": blocked,
        "arc_pct": round(100.0 * len(clear) / len(ring), 1) if ring else 0.0,
    }


def id_altitude_band(preferred_m: float, floor_m: float, ceiling_m: float,
                     steps: int = ID_ALT_SEARCH_STEPS) -> list[float]:
    """Altitudes the cross-cue ID pass may be flown at, best picture first.

    ASCENDING, starting at the pixel-optimal altitude. Two facts make that the
    right order:

      * at a fixed standoff, a LOWER altitude is a shorter slant range and so
        MORE pixels on the target — the first entry is the best picture the
        geometry allows;
      * a HIGHER eye sees further — the geometric horizon grows as
        `sqrt(2*R*h)` — so climbing is the only lever left once the preferred
        altitude is masked, given that closing the standoff is forbidden (M14).

    So "first candidate with line of sight" is also "the most pixels on target
    among the candidates that can actually see the contact".
    """
    lo = max(float(floor_m), 0.0)
    hi = max(lo, float(ceiling_m))
    start = min(max(float(preferred_m), lo), hi)
    n = max(1, int(steps))
    if hi - start <= 1e-6 or n == 1:
        return [round(start, 3)]
    out = [start + (hi - start) * i / (n - 1) for i in range(n)]
    seen: list[float] = []
    for alt in out:
        alt = round(alt, 3)
        if not seen or abs(alt - seen[-1]) > 1e-6:
            seen.append(alt)
    return seen


def _blocker_summary(blocked: Sequence[Mapping]) -> str:
    """One readable line per distinct obstruction in a `verify_orbit_los` run.

    A refusal that only says "no line of sight" tells the operator nothing they
    can act on. This names WHAT blocked, how many points it took, and the
    numbers the LOS model reported, so "the horizon reached 23134 m against a
    26368 m ground range" is visible instead of inferred.
    """
    kinds: dict[str, dict] = {}
    for entry in blocked:
        detail = dict(entry.get("detail") or {})
        obstacle = detail.get("first_obstacle") or {}
        if not isinstance(obstacle, Mapping):
            obstacle = {}
        name = (obstacle.get("name") or obstacle.get("type")
                or detail.get("model") or "unmodelled")
        row = kinds.setdefault(str(name), {"count": 0, "reach_m": None,
                                           "range_m": None})
        row["count"] += 1
        reach = obstacle.get("range_m")
        rng = obstacle.get("ground_range_m")
        if reach is not None:
            row["reach_m"] = float(reach)
        if rng is not None:
            row["range_m"] = max(row["range_m"] or 0.0, float(rng))
    parts = []
    for name, row in sorted(kinds.items()):
        text = f"{name} blocked {row['count']}"
        if row["reach_m"] is not None and row["range_m"] is not None:
            text += (f" (LOS model reach {row['reach_m']:.0f} m vs a "
                     f"{row['range_m']:.0f} m ground range)")
        parts.append(text)
    return "; ".join(parts) or "the LOS model reported no obstacle detail"


def _merge_los(detect_los: Mapping, id_los: Mapping) -> dict:
    """One `verify_orbit_los`-shaped result over `detect ring + id ring`.

    The two rings are checked separately (the ID ring is re-checked at each
    candidate altitude), but the plan's `meta["los"]` must keep indexing the
    combined waypoint list the way it always did, or `clear_indices` stops
    lining up with `waypoints`.
    """
    offset = int(detect_los["points_checked"])
    checked = offset + int(id_los["points_checked"])
    clear = ([int(i) for i in detect_los["clear_indices"]]
             + [int(i) + offset for i in id_los["clear_indices"]])
    blocked = list(detect_los["blocked"]) + [
        {**entry, "index": int(entry["index"]) + offset}
        for entry in id_los["blocked"]]
    return {
        "verified": True,
        "points_checked": checked,
        "points_clear": len(clear),
        "clear_indices": clear,
        "blocked": blocked,
        "arc_pct": round(100.0 * len(clear) / checked, 1) if checked else 0.0,
    }


def track_target_plan(vehicle: str, track, *, alt_agl_m: float = 120.0,
                      camera_name: str | Camera = DEFAULT_CAMERA,
                      speed_mps: float = 12.0,
                      min_pixels_on_target: float = IDENTIFY_PIXELS,
                      points: int = 12,
                      los_check: LosCheck | None = None,
                      allow_unverified: bool = False,
                      sun_azimuth_deg: float | None = None,
                      repath_interval_s: float = 15.0) -> MissionPlan:
    """Follow a contact at a server-derived, LOS-verified standoff (M5).

    The caller supplies the track, NOT a radius. The standoff is the contact's
    threat ring (`standoff_for_track`); the narrow-FOV pixel density needed for
    an ID is evaluated against that ring and REPORTED — resolution never buys
    its way inside the envelope (M14). Every point on the orbit ring is then
    LOS-checked against the contact: points with no line of sight are dropped
    and reported, and if nothing has line of sight the plan is refused
    (`LosBlockedError`).

    `los_check` is mandatory. Passing `allow_unverified=True` instead is an
    explicit, recorded decision: the plan comes back stamped
    `los.verified: False` with a warning, never silently.
    """
    track_id = getattr(track, "track_id", None)
    if track_id is None:
        raise ValueError(
            "track_target needs a targets.Track (M5 standoff is derived from "
            "its order-of-battle class); a bare lat/lon has no threat ring")
    so = standoff_for_track(track, alt_agl_m=alt_agl_m, camera_name=camera_name,
                            min_pixels_on_target=min_pixels_on_target)
    cam = camera(camera_name)
    warnings: list[str] = []
    if not so["id_achievable"]:
        warnings.append(
            f"id_not_achievable_at_standoff: {so['basis']}. Standoff holds "
            "(ISR-only: the threat ring is never traded for resolution); "
            "identification needs a narrower FOV or a different sensor.")

    ring = orbit_waypoints(track.lat, track.lon, alt_agl_m, so["standoff_m"],
                           points=points, sun_azimuth_deg=sun_azimuth_deg)

    if los_check is not None:
        los = verify_orbit_los(ring, (track.lat, track.lon, float(track.alt_m or 0.0)),
                               los_check)
        if not los["clear_indices"]:
            raise LosBlockedError(
                f"no line of sight to track {track_id} from any of "
                f"{los['points_checked']} points on the {so['standoff_m']:.0f} m "
                f"standoff ring at {alt_agl_m:.0f} m AGL — climb, move the "
                "standoff, or retask")
        if los["blocked"]:
            warnings.append(
                f"los_partial: {len(los['blocked'])} of {los['points_checked']} "
                f"ring points are masked; the plan flies the {los['arc_pct']:.0f}% "
                "of the arc that has line of sight")
        ring = [ring[i] for i in los["clear_indices"]]
    elif allow_unverified:
        los = {"verified": False, "points_checked": 0, "points_clear": 0,
               "clear_indices": list(range(len(ring))), "blocked": [],
               "arc_pct": None}
        warnings.append(
            "los_unverified: the M5 standoff was accepted without a "
            "line-of-sight check (allow_unverified=True). Terrain masking of "
            "the contact has NOT been ruled out.")
    else:
        raise LosUnavailableError(
            "track_target requires a los_check callable to verify the M5 "
            "standoff (TOOL_CONTRACT: 'standoff ... then verified with "
            "uav_los_check'). Pass allow_unverified=True to plan without it — "
            "the plan is then stamped los.verified=False.")

    meta = {
        "doctrine": "track_target",
        "track_id": track_id,
        "poi": [track.lat, track.lon],
        "camera": cam.to_dict(),
        "standoff": so,
        "standoff_m": so["standoff_m"],
        "radius_m": so["standoff_m"],   # legacy key; same server-derived number
        "los": los,
        "repath": {
            "interval_s": float(repath_interval_s),
            "reacquire_move_m": round(max(25.0, so["standoff_m"] * 0.10), 1),
            "note": "call repath_track() when repath_needed() is true (M5)",
        },
        "_plan_params": {
            "alt_agl_m": alt_agl_m, "camera_name": cam.name,
            "speed_mps": speed_mps, "min_pixels_on_target": min_pixels_on_target,
            "points": points, "sun_azimuth_deg": sun_azimuth_deg,
            "repath_interval_s": repath_interval_s,
        },
    }
    return MissionPlan("track_target", vehicle, ring, alt_agl_m, speed_mps, meta,
                       warnings=warnings)


def repath_needed(plan: MissionPlan, track, *, tolerance_m: float | None = None) -> bool:
    """True when the contact has moved far enough to invalidate the orbit (M5)."""
    if plan.kind != "track_target":
        raise ValueError(f"repath_needed is for track_target plans, not {plan.kind!r}")
    poi = plan.meta["poi"]
    m_lat, m_lon = _m_per_deg(poi[0])
    moved = math.hypot((track.lat - poi[0]) * m_lat, (track.lon - poi[1]) * m_lon)
    tol = (float(tolerance_m) if tolerance_m is not None
           else float(plan.meta["repath"]["reacquire_move_m"]))
    return moved > tol


def repath_track(plan: MissionPlan, track, *, los_check: LosCheck | None = None,
                 allow_unverified: bool = False, **overrides) -> MissionPlan:
    """Re-derive the orbit around the contact's current fix (M5 re-path loop)."""
    if plan.kind != "track_target":
        raise ValueError(f"repath_track is for track_target plans, not {plan.kind!r}")
    params = dict(plan.meta["_plan_params"])
    params.update(overrides)
    new = track_target_plan(plan.vehicle, track, los_check=los_check,
                            allow_unverified=allow_unverified, **params)
    new.meta["repath_of"] = plan.meta.get("track_id")
    new.meta["repath_moved_m"] = round(math.hypot(
        (track.lat - plan.meta["poi"][0]) * _m_per_deg(plan.meta["poi"][0])[0],
        (track.lon - plan.meta["poi"][1]) * _m_per_deg(plan.meta["poi"][0])[1]), 1)
    return new


# ---------------------------------------------------------------------------
# M7 — identify: wide detect -> narrow cross-cue at reduced slant range
# ---------------------------------------------------------------------------

def identify_plan(vehicle: str, track, *, alt_agl_m: float = 120.0,
                  camera_name: str | Camera = DEFAULT_CAMERA,
                  speed_mps: float = 10.0,
                  orbit_first: bool = True,
                  detect_points: int = 8,
                  id_points: int = 6,
                  min_id_pixels: float = IDENTIFY_PIXELS,
                  min_detect_pixels: float = DETECT_PIXELS,
                  min_alt_agl_m: float = MIN_ID_ALT_AGL_M,
                  max_id_alt_agl_m: float | None = None,
                  id_alt_steps: int = ID_ALT_SEARCH_STEPS,
                  los_check: LosCheck | None = None,
                  allow_unverified: bool = False,
                  sun_azimuth_deg: float | None = None) -> MissionPlan:
    """Wide-FOV detect, then cross-cue to narrow FOV at reduced slant (M7).

    Two phases, each carrying the `fov_deg` the server drives `uav_set_fov` to:

      1. `detect` — WIDE FOV, flown at the furthest standoff that still puts
         `min_detect_pixels` on the contact (never closer than the threat
         ring). Search happens at the sensor's reach, not on top of the target.
      2. `identify` — NARROW FOV at a REDUCED slant range: the aircraft closes
         to the threat-ring standoff and descends to the altitude at which
         `min_id_pixels` fall on the contact, floored by `min_alt_agl_m`.

    The threat ring is a hard floor in both phases: resolution is never bought
    by flying inside a contact's engagement envelope (ISR-only, M14). When even
    the floor altitude cannot reach the needed pixel count the plan says
    `id_achievable: False` and carries a warning — it does not pretend.

    THE ID ALTITUDE IS SEARCHED, NOT ASSUMED. The pixel-optimal ID altitude is
    the LOWEST one (shortest slant at a fixed standoff), and the code used to
    fly exactly that and refuse if it was masked. At a large threat ring that is
    geometrically backwards: descending to the 30 m floor buys almost no pixels
    when the ground range is kilometres, while it collapses the geometric
    horizon. Measured in the default theater: an SA-6-class contact's 26.4 km
    ring is visible from the 60 m detect ring (horizon 32.0 km) and INVISIBLE
    from the 30 m ID ring (horizon 23.1 km), so every identify pass refused with
    "no line-of-sight point at the cross-cue altitude" — a bare refusal on a
    contact that was in plain sight one ring up. The ID pass now walks
    `id_altitude_band()` from the pixel-optimal altitude up to
    `max_id_alt_agl_m` (default: the detect altitude) and flies the FIRST
    altitude with a sight line — the best picture among the ones that can
    actually see. If the whole band is masked the refusal names the band, the
    standoff, how many points were checked and what blocked them, because an
    operator cannot act on "no line of sight".
    """
    track_id = getattr(track, "track_id", None)
    if track_id is None:
        raise ValueError("identify needs a targets.Track (M7 cross-cue is "
                         "derived from its order-of-battle class)")
    cam = camera(camera_name)
    ob = track.ob
    size = _target_size_m(ob)
    warnings: list[str] = []

    # M5 floor: the contact's engagement envelope. Neither phase goes inside it.
    ring = standoff_for_track(track, alt_agl_m=alt_agl_m, camera_name=cam,
                              min_pixels_on_target=min_id_pixels)
    ring_m = ring["threat_ring_m"]

    # Phase 1: detect from as far out as the wide field allows.
    detect_alt = float(alt_agl_m)
    wide_max_slant = max_slant_for_pixels(size, fov_deg=cam.hfov_deg,
                                          image_px=cam.image_px_w,
                                          min_pixels=min_detect_pixels)
    detect_ground = max(ring_m, math.sqrt(max(0.0, wide_max_slant ** 2
                                              - detect_alt ** 2)))
    detect_slant = math.hypot(detect_ground, detect_alt)
    detect_px = pixels_on_target(size, detect_slant, cam.hfov_deg, cam.image_px_w)
    # `detect_ground` is derived so that the slant lands exactly ON the pixel
    # floor when the sensor (not the threat ring) is the binding constraint, so
    # a bare `<` fired this warning on EVERY such plan at 2.9999999996 px. A
    # marginal-detection warning that cries wolf on every plan trains the
    # operator to ignore the one that means something; the tolerance is one part
    # in a billion, far below any real geometry.
    if detect_px < min_detect_pixels * (1.0 - 1e-9):
        warnings.append(
            f"detect_marginal: the wide {cam.hfov_deg:.0f} deg field delivers "
            f"{detect_px:.1f} px on a {size:.1f} m contact at the "
            f"{detect_ground:.0f} m detect standoff (threat ring {ring_m:.0f} m) "
            f"— below the {min_detect_pixels:.0f} px detection floor")

    # Phase 2: cross-cue. Narrow the field AND reduce slant range by closing to
    # the threat ring and descending (M7).
    id_ground = ring_m
    id_max_slant = max_slant_for_pixels(size, fov_deg=cam.narrow_hfov_deg,
                                        image_px=cam.image_px_w,
                                        min_pixels=min_id_pixels)
    if id_max_slant > id_ground:
        id_alt_best = min(detect_alt, max(float(min_alt_agl_m),
                                          math.sqrt(id_max_slant ** 2
                                                    - id_ground ** 2)))
    else:
        id_alt_best = float(min_alt_agl_m)
    ceiling = float(detect_alt if max_id_alt_agl_m is None else max_id_alt_agl_m)
    if ceiling < float(min_alt_agl_m):
        # The two altitude constraints contradict each other. `id_altitude_band`
        # resolves that by clamping the ceiling UP to the floor — silently, so a
        # caller who set `max_id_alt_agl_m` as an airspace lid (or who flew the
        # detect pass below the min-AGL floor) would be handed an ID ring flown
        # ABOVE the lid with nothing in the plan saying so. The floor still
        # wins — it is the safety limit — but the override is reported.
        warnings.append(
            f"id_alt_ceiling_below_floor: the ID-pass ceiling {ceiling:.0f} m "
            "AGL ("
            + ("the detect altitude" if max_id_alt_agl_m is None
               else "max_id_alt_agl_m")
            + f") is below the {float(min_alt_agl_m):.0f} m AGL minimum-altitude "
              "floor, so the floor wins: the identify ring is flown at "
              f"{float(min_alt_agl_m):.0f} m AGL, ABOVE the ceiling asked for")
    band = id_altitude_band(id_alt_best, min_alt_agl_m, ceiling, id_alt_steps)
    id_alt = band[0]

    def _id_geometry(alt: float) -> tuple[float, float]:
        slant = math.hypot(id_ground, float(alt))
        return slant, pixels_on_target(size, slant, cam.narrow_hfov_deg,
                                       cam.image_px_w)

    ground = id_ground
    detect_wps = (orbit_waypoints(track.lat, track.lon, detect_alt, detect_ground,
                                  points=detect_points,
                                  sun_azimuth_deg=sun_azimuth_deg)
                  if orbit_first else
                  orbit_waypoints(track.lat, track.lon, detect_alt, detect_ground,
                                  points=3, sun_azimuth_deg=sun_azimuth_deg)[:1])

    def _id_ring(alt: float) -> list[dict]:
        return orbit_waypoints(track.lat, track.lon, alt, id_ground,
                               points=id_points, sun_azimuth_deg=sun_azimuth_deg)

    id_wps = _id_ring(id_alt)
    #: One row per altitude the cross-cue tried — the evidence behind both the
    #: chosen altitude and any refusal.
    alt_search: list[dict] = []

    target = (track.lat, track.lon, float(track.alt_m or 0.0))
    if los_check is not None:
        detect_los = verify_orbit_los(detect_wps, target, los_check)
        chosen_los = None
        for candidate in band:
            ring_wps = _id_ring(candidate)
            cand_los = verify_orbit_los(ring_wps, target, los_check)
            alt_search.append({
                "alt_agl_m": round(candidate, 1),
                "slant_range_m": round(_id_geometry(candidate)[0], 1),
                "expected_pixels_on_target": round(_id_geometry(candidate)[1], 2),
                "points_checked": cand_los["points_checked"],
                "points_clear": cand_los["points_clear"],
                "blocked_by": (_blocker_summary(cand_los["blocked"])
                               if cand_los["blocked"] else None),
            })
            if cand_los["clear_indices"]:
                id_alt, id_wps, chosen_los = candidate, ring_wps, cand_los
                break
        if chosen_los is None:
            searched = (f"{len(band)} altitude(s) from {band[0]:.0f} to "
                        f"{band[-1]:.0f} m AGL, {id_points} ring points each")
            # The band is monotone — a higher eye always reaches further — so
            # the two ends bound it. Naming all six would repeat one fact.
            ends = [alt_search[0]] if len(alt_search) == 1 else [alt_search[0],
                                                                alt_search[-1]]
            why = "; ".join(f"at {row['alt_agl_m']:.0f} m AGL {row['blocked_by']}"
                            for row in ends)
            remedy = (
                f"The {id_ground:.0f} m standoff is the {ob.name} engagement "
                f"envelope ({ob.weapon_range_m:.0f} m + 10%) and ISR doctrine "
                "(M14) does not trade it for a picture, so the levers are: "
                f"raise max_id_alt_agl_m above {band[-1]:.0f} m AGL, observe "
                "from an AO large enough to hold that ring, or retask.")
            if not detect_los["clear_indices"]:
                raise LosBlockedError(
                    f"no line of sight to track {track_id} from any point of "
                    f"the identify plan: the {detect_ground:.0f} m detect ring "
                    f"at {detect_alt:.0f} m AGL is masked "
                    f"({_blocker_summary(detect_los['blocked'])}) and so is "
                    f"every cross-cue altitude — searched {searched}. {why}. "
                    f"{remedy}")
            raise LosBlockedError(
                f"the identification pass on track {track_id} has no "
                f"line-of-sight point at any cross-cue altitude: searched "
                f"{searched} on the {id_ground:.0f} m threat-ring standoff, "
                f"none clear. {why}. The detect ring at {detect_alt:.0f} m AGL "
                f"has {detect_los['points_clear']} of "
                f"{detect_los['points_checked']} points clear. {remedy}")
        if id_alt > band[0] + 1e-6:
            warnings.append(
                f"id_alt_raised_for_los: the pixel-optimal ID altitude "
                f"{band[0]:.0f} m AGL had no sight line to the contact at the "
                f"{id_ground:.0f} m standoff ({alt_search[0]['blocked_by']}); "
                f"climbed to {id_alt:.0f} m AGL, trading "
                f"{alt_search[0]['expected_pixels_on_target']:.1f} px for "
                f"{alt_search[-1]['expected_pixels_on_target']:.1f} px and a "
                "usable sight line")
        los = _merge_los(detect_los, chosen_los)
        if los["blocked"]:
            warnings.append(
                f"los_partial: {len(los['blocked'])} of {los['points_checked']} "
                "identify points are masked and were dropped")
        keep_d = set(detect_los["clear_indices"])
        keep_i = set(chosen_los["clear_indices"])
        detect_wps = [w for i, w in enumerate(detect_wps) if i in keep_d]
        id_wps = [w for i, w in enumerate(id_wps) if i in keep_i]
    elif allow_unverified:
        # `clear_indices: []` said "no point has line of sight" while every
        # point was still flown — the opposite of the plan. Unverified means
        # unfiltered, exactly as in track_target_plan.
        los = {"verified": False, "points_checked": 0, "points_clear": 0,
               "clear_indices": list(range(len(detect_wps) + len(id_wps))),
               "blocked": [], "arc_pct": None}
        warnings.append("los_unverified: identify plan accepted without a "
                        "line-of-sight check (allow_unverified=True)")
    else:
        raise LosUnavailableError(
            "identify requires a los_check callable to verify the cross-cue "
            "geometry (M5/M7); pass allow_unverified=True to plan without it")

    # Pixel maths is reported for the altitude actually chosen above, never for
    # the one the search started from.
    id_slant, id_px = _id_geometry(id_alt)
    id_ok = id_slant <= id_max_slant
    if not id_ok:
        warnings.append(
            f"id_not_achievable: even at the {id_alt:.0f} m AGL ID pass the "
            f"{id_ground:.0f} m threat-ring standoff leaves a {id_slant:.0f} m "
            f"slant, past the {id_max_slant:.0f} m identification range of the "
            f"{cam.narrow_hfov_deg:.1f} deg field ({id_px:.1f} px of "
            f"{min_id_pixels:.0f} needed). The standoff holds (M14).")
    if id_slant >= detect_slant:
        warnings.append(
            "cross_cue_no_slant_reduction: the ID pass could not close the "
            f"slant range below the detect pass ({id_slant:.0f} m vs "
            f"{detect_slant:.0f} m) — the threat ring pins both phases at the "
            "same geometry, so the cue is FOV-only")

    phases = [
        {"name": "detect", "fov_deg": cam.hfov_deg, "camera": cam.name,
         "alt_agl_m": round(detect_alt, 1), "standoff_m": round(detect_ground, 1),
         "slant_range_m": round(detect_slant, 1),
         "expected_pixels_on_target": round(detect_px, 2),
         "min_pixels": float(min_detect_pixels),
         "waypoint_span": [0, len(detect_wps)],
         "purpose": "wide-field detection at the sensor's reach, outside the "
                    "threat ring"},
        {"name": "identify", "fov_deg": cam.narrow_hfov_deg, "camera": cam.name,
         "alt_agl_m": round(id_alt, 1), "standoff_m": round(id_ground, 1),
         "slant_range_m": round(id_slant, 1),
         "expected_pixels_on_target": round(id_px, 2),
         "min_pixels": float(min_id_pixels),
         "id_max_slant_m": round(id_max_slant, 1),
         "achievable": id_ok,
         "waypoint_span": [len(detect_wps), len(detect_wps) + len(id_wps)],
         "purpose": "narrow-field cross-cue at reduced slant range (M7)"},
    ]
    meta = {
        "doctrine": "identify",
        "track_id": track_id,
        "poi": [track.lat, track.lon],
        "camera": cam.to_dict(),
        "orbit_first": bool(orbit_first),
        "standoff_m": round(ground, 1),
        "threat_ring_m": ring_m,
        "ob_class": ob.key,
        "cross_cue": {
            "wide_fov_deg": cam.hfov_deg, "narrow_fov_deg": cam.narrow_hfov_deg,
            "detect_standoff_m": round(detect_ground, 1),
            "id_standoff_m": round(id_ground, 1),
            "detect_alt_agl_m": round(detect_alt, 1),
            "id_alt_agl_m": round(id_alt, 1),
            "detect_slant_m": round(detect_slant, 1),
            "id_slant_m": round(id_slant, 1),
            "slant_reduction_m": round(detect_slant - id_slant, 1),
            "detect_px": round(detect_px, 2),
            "id_px": round(id_px, 2),
            # The altitude band the ID pass was allowed to use, the altitude it
            # would have flown on pixel density alone, and what every candidate
            # LOS check actually returned. A refusal AND a silent climb are both
            # explained by this block rather than having to be inferred.
            "id_alt_band_m": [round(band[0], 1), round(band[-1], 1)],
            "id_alt_pixel_optimal_m": round(band[0], 1),
            "id_alt_search": alt_search,
        },
        "id_achievable": id_ok,
        "los": los,
        "_plan_params": {
            "alt_agl_m": alt_agl_m, "camera_name": cam.name,
            "speed_mps": speed_mps, "orbit_first": orbit_first,
        },
    }
    return MissionPlan("identify", vehicle, detect_wps + id_wps, alt_agl_m,
                       speed_mps, meta, phases=phases, warnings=warnings)


# ---------------------------------------------------------------------------
# M4 — the pre-flight gate and the dry-run plan product
# ---------------------------------------------------------------------------

def standoff_vs_ao(plan: MissionPlan, envelope: SafetyEnvelope) -> dict | None:
    """Why a doctrine-derived standoff plan cannot fit in this AO, or None.

    The M5 threat ring is not a knob: `standoff_for_track` derives it from the
    contact's engagement envelope and M14 forbids trading it for a picture. So
    when the ring is wider than the AO, the geofence rejects every waypoint and
    the operator is handed `wp0:geofence, wp1:geofence, ...` with nothing saying
    WHY — measured in the default theater, whose AO reaches ~700 m from the
    contact while an `mbt`'s ring is 1650 m and an SA-6-class ring is 26.4 km.
    That is a theater/doctrine mismatch, not a planning bug, and it is named
    here so the operator can act on it.

    Returns None when the plan carries no threat ring, when there is no
    geofence, or when the ring does fit (in which case the violations have some
    other cause and inventing this explanation would be a lie).
    """
    meta = plan.meta or {}
    poi = meta.get("poi")
    if not poi or not envelope.geofence or not plan.waypoints:
        return None
    standoff = meta.get("standoff")
    standoff = standoff if isinstance(standoff, Mapping) else {}
    ring = meta.get("threat_ring_m", standoff.get("threat_ring_m"))
    reach_m = max(haversine_m(poi[0], poi[1], lat, lon)
                  for lat, lon in envelope.geofence)
    widest_m = max(haversine_m(poi[0], poi[1], wp["lat"], wp["lon"])
                   for wp in plan.waypoints)
    if widest_m <= reach_m and (ring is None or float(ring) <= reach_m):
        return None            # the rings fit; the violations have another cause
    ob_class = meta.get("ob_class") or standoff.get("ob_class")
    # Count GEOFENCE violations only: `check_route` also reports ceiling and
    # min-AGL on the same waypoint, and claiming those are "outside the AO"
    # would be the same kind of confident wrong answer this function exists to
    # replace.
    outside = sum(1 for v in envelope.check_route(plan.to_route())
                  if "geofence" in v)
    if not outside:
        return None
    where = (f"the AO reaches only {reach_m:.0f} m from the contact at "
             f"{poi[0]:.5f},{poi[1]:.5f}, so {outside} of "
             f"{len(plan.waypoints)} waypoints are outside the geofence.")
    if ring is not None and float(ring) > reach_m:
        ring = float(ring)
        limiting = "threat_ring"
        summary = (
            f"standoff_exceeds_ao: the M5 threat ring for this contact is "
            f"{ring:.0f} m"
            + (f" (order-of-battle class {ob_class!r})" if ob_class else "")
            + f" and {where} ISR doctrine (M14) holds the threat ring rather "
              "than closing inside a contact's engagement envelope for a better "
              "picture, so this contact cannot be observed from this AO at all: "
              f"task it from an AO reaching at least {ring:.0f} m around the "
              "contact, or retask.")
        shortfall = ring - reach_m
    else:
        limiting = "sensor_standoff"
        summary = (
            f"standoff_exceeds_ao: the plan's widest ring is {widest_m:.0f} m "
            "from the contact — the wide-field detect standoff, i.e. the range "
            "at which the detection pixel floor is still met — while "
            + where
            + (f" The {float(ring):.0f} m threat ring itself would fit."
               if ring is not None else "")
            + " Fly this contact from a larger AO, or raise min_detect_pixels "
              "so the detect ring is pulled in to a shorter, higher-resolution "
              "standoff.")
        shortfall = widest_m - reach_m
    return {
        "limiting_factor": limiting,
        "threat_ring_m": None if ring is None else round(float(ring), 1),
        "plan_max_standoff_m": round(widest_m, 1),
        "ao_reach_m": round(reach_m, 1),
        "shortfall_m": round(shortfall, 1),
        "ob_class": ob_class,
        "waypoints_rejected": outside,
        "waypoints": len(plan.waypoints),
        "summary": summary,
    }


def dry_run(plan: MissionPlan, fuel: FuelModel, envelope: SafetyEnvelope, *,
            start: tuple[float, float] | None = None,
            start_alt_agl_m: float = 0.0,
            wind_ne: tuple[float, float] | None = None) -> dict:
    """The plan product: waypoints, est_time_s, est_fuel_pct, gate. Executes nothing.

    This is `mission_dry_run` (TOOL_CONTRACT §4.3) and it is also the M4 gate
    every mission must pass before anything is queued: the caller submits only
    when `product["gate"]["ok"]` is true.

    Fuel and time come from `FuelModel.preflight_gate` over `plan.waypoints` —
    the SAME integrator the in-flight tick uses (T5) — so the estimate prices
    exactly the route that will be flown, truncation included.

    `start` should be the vehicle's LIVE position. Omitting it falls back to
    the envelope home and records `gate_start_assumed_home` in the warnings, so
    a missing ingress leg is visible rather than silent.
    """
    if not isinstance(plan, MissionPlan):
        raise TypeError(f"dry_run needs a MissionPlan, got {type(plan).__name__}")
    warnings = list(plan.warnings)
    if start is None:
        if envelope.home is None:
            raise ValueError(
                "dry_run needs a start position: pass the vehicle's live "
                "position, or configure envelope.home. Defaulting to the first "
                "waypoint would hide the whole ingress leg from the M4 gate.")
        start = (envelope.home[0], envelope.home[1])
        warnings.append(
            "gate_start_assumed_home: the gate was priced from home, not from "
            "the vehicle's live position; the ingress leg may be understated")

    route = plan.to_route()
    gate = fuel.preflight_gate(route, start, plan.speed_mps,
                               start_alt_m=float(start_alt_agl_m),
                               wind_ne=wind_ne)
    violations = envelope.check_route(route)
    speed_violations = envelope.check_speed(plan.speed_mps)
    gate["envelope_violations"] = violations + speed_violations
    gate["ok"] = bool(gate["ok"]) and not violations and not speed_violations
    # A geofence rejection on a plan whose radius the SERVER derived is not
    # something the operator can re-plan their way out of; say which of the two
    # is too small rather than listing every waypoint as "geofence".
    if violations:
        detail = standoff_vs_ao(plan, envelope)
        if detail is not None:
            gate["standoff_vs_ao"] = detail
            warnings.append(detail["summary"])

    bingo_line = fuel.bingo_fuel_pct(start, float(start_alt_agl_m),
                                     wind_ne=wind_ne)
    product = plan.to_dict()
    product.update({
        "mission_kind": plan.kind,
        "executed": False,
        "gate": gate,
        # est_* describe the route ACTUALLY planned above, truncation included.
        "est_time_s": gate["est_time_s"],
        "est_distance_m": gate["est_distance_m"],
        "est_fuel_pct": gate["plan_fuel_pct"],
        "required_pct": gate["required_pct"],
        # Reserved for the return-leg + reserve line, never the plan's cost
        # (the two meanings were previously the same key).
        "bingo_fuel_pct": round(bingo_line, 2),
        "fuel_pct": round(fuel.fuel_pct, 2),
        "gate_start": [start[0], start[1]],
        "warnings": warnings,
    })
    return product


def gate_plan(plan: MissionPlan, fuel: FuelModel, envelope: SafetyEnvelope,
              **kw) -> dict:
    """Just the M4 gate verdict for a plan (thin view over `dry_run`)."""
    return dry_run(plan, fuel, envelope, **kw)["gate"]


def mission_coverage(plan: MissionPlan, flown_path: Sequence | None = None,
                     polygon: Sequence | None = None) -> dict | None:
    """Coverage for the INTREP (§4.7): planned, or actually flown if given.

    Pass the telemetry breadcrumb trail as `flown_path` after the mission and
    the returned slot describes what was genuinely imaged, not what was planned.
    """
    poly = polygon
    if poly is None:
        params = plan.meta.get("_plan_params") or {}
        poly = params.get("polygon")
    if flown_path is None:
        return plan.coverage.to_dict() if plan.coverage else None
    if poly is None:
        # A flown trail was supplied and silently ignored, handing the caller
        # the PLANNED figure under the name of the flown one — the exact
        # substitution this module exists to stop.
        raise ValueError(
            f"a {plan.kind!r} plan carries no tasked polygon, so a flown track "
            "cannot be scored against an area. Pass polygon=<the tasked AO> "
            "explicitly; the planned coverage will NOT be returned in its place.")
    sw = float(plan.meta.get("swath_m") or swath_m(plan.alt_m))
    return coverage_of_path(poly, flown_path, sw, basis="flown").to_dict()


# ---------------------------------------------------------------------------
# Legacy dispatcher (kept so the GEV control panel's uav_mission keeps working)
# ---------------------------------------------------------------------------

def plan_mission(kind: str, vehicle: str, **kw) -> MissionPlan:
    """Build a MissionPlan for the requested kind (legacy kwarg spelling).

    `uav_mission` forwards here. New code should call the discrete planners
    (`grid_search_plan`, `recon_route_plan`, `track_target_plan`,
    `identify_plan`) — they expose the doctrine each mission derives.
    """
    kind = (kind or "").lower()
    alt = float(kw.get("alt_agl_m", kw.get("alt_m", 60.0)))

    if kind == "recon_route":
        wps = kw.get("waypoints") or []
        if not wps:
            raise ValueError("recon_route needs waypoints")
        return recon_route_plan(
            vehicle, wps, alt,
            forward_overlap_pct=kw.get("forward_overlap_pct",
                                       kw.get("overlap", DEFAULT_OVERLAP)),
            camera_name=kw.get("camera", DEFAULT_CAMERA),
            speed_mps=float(kw.get("speed_mps", 10.0)),
            max_capture_rate_hz=float(kw.get("max_capture_rate_hz", 1.0)))

    if kind == "grid_search":
        return grid_search_plan(
            vehicle, kw.get("polygon") or [], alt,
            overlap_pct=kw.get("overlap_pct", kw.get("overlap", DEFAULT_OVERLAP)),
            camera_name=kw.get("camera", DEFAULT_CAMERA),
            pattern=kw.get("pattern", "lawnmower"),
            speed_mps=float(kw.get("speed_mps", 8.0)),
            max_lanes=kw.get("max_lanes"),
            legs=int(kw.get("legs", 12)))

    if kind == "orbit_poi":
        lat, lon = float(kw["lat"]), float(kw["lon"])
        radius = float(kw.get("radius_m", 80.0))
        wps = orbit_waypoints(lat, lon, alt, radius,
                              points=int(kw.get("points", 12)),
                              sun_azimuth_deg=kw.get("sun_azimuth_deg"))
        return MissionPlan(kind, vehicle, wps, alt, float(kw.get("speed_mps", 10.0)),
                           {"doctrine": "orbit_poi", "radius_m": radius,
                            "poi": [lat, lon]})

    if kind == "track_target":
        track = kw.get("track")
        if track is None:
            raise ValueError(
                "track_target is derived from a track, not a radius (M5): pass "
                "track=<targets.Track> (or call track_target_plan directly). A "
                "caller-supplied radius_m is no longer accepted — the standoff "
                "comes from the contact's threat ring.")
        return track_target_plan(
            vehicle, track, alt_agl_m=alt,
            camera_name=kw.get("camera", DEFAULT_CAMERA),
            speed_mps=float(kw.get("speed_mps", 12.0)),
            los_check=kw.get("los_check"),
            allow_unverified=bool(kw.get("allow_unverified", False)),
            sun_azimuth_deg=kw.get("sun_azimuth_deg"))

    if kind in ("identify", "identify_target"):
        track = kw.get("track")
        if track is None:
            raise ValueError("identify needs track=<targets.Track> (M7)")
        return identify_plan(
            vehicle, track, alt_agl_m=alt,
            camera_name=kw.get("camera", DEFAULT_CAMERA),
            speed_mps=float(kw.get("speed_mps", 10.0)),
            orbit_first=bool(kw.get("orbit_first", True)),
            los_check=kw.get("los_check"),
            allow_unverified=bool(kw.get("allow_unverified", False)),
            sun_azimuth_deg=kw.get("sun_azimuth_deg"))

    if kind == "assess":
        # Legacy point-assess: a tight narrow-FOV ring at a known point. Kept
        # for the GEV panel; the doctrine path for a known contact is
        # identify_plan, which cross-cues wide->narrow (M7).
        lat, lon = float(kw["lat"]), float(kw["lon"])
        alt = float(kw.get("alt_agl_m", kw.get("alt_m", 45.0)))
        cam = camera(kw.get("camera", DEFAULT_CAMERA))
        radius = float(kw.get("radius_m", 50.0))
        wps = orbit_waypoints(lat, lon, alt, radius, points=6,
                              sun_azimuth_deg=kw.get("sun_azimuth_deg"))
        slant = math.hypot(radius, alt)
        return MissionPlan(kind, vehicle, wps, alt, float(kw.get("speed_mps", 8.0)),
                           {"doctrine": "assess", "poi": [lat, lon],
                            "radius_m": radius, "camera": cam.to_dict(),
                            "slant_range_m": round(slant, 1)},
                           phases=[{"name": "assess", "fov_deg": cam.narrow_hfov_deg,
                                    "camera": cam.name, "alt_agl_m": alt,
                                    "standoff_m": radius,
                                    "slant_range_m": round(slant, 1),
                                    "waypoint_span": [0, len(wps)],
                                    "purpose": "narrow-field look at a known point"}])

    raise ValueError(f"unknown mission kind: {kind}")
