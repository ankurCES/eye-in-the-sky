"""Theater proposals: an exact, checked AO before anything moves (WG v2 §4.1.2).

`theater_propose` turns "a place" into a proposal the operator approves and
`sim_set_theater` executes VERBATIM (§3.8): the tool refuses any argument that
does not match the stored proposal (`args_match`), so the approval card the
policy renders from the args is guaranteed to be what runs.

What a proposal decides, in order:

1. Reach. `safety.reach_radius_m(af)` (quad 7350 m, group 3 352.8 km) caps the
   AO half-extent at `clamp(0.4 x reach, 1500, 25000)` (D5, C7).
2. Half-extent. The requested value, else half the shorter side of the
   geocoded box, else `DEFAULT_HALF[af]`, clamped to that cap; a clamp is a
   caveat and `clamped_from_km`.
3. Home. The operator's (inside the AO), else the nearest named open ground
   within `0.4 x half` of the centre, else the AO centre.
4. Ground (T1). Re:Earth ellipsoidal height through `canonical_altitude`
   (EGM96 MSL), else Open-Meteo used as MSL (caveated), else the operator's
   `ground_msl_m`, else refused (`ground_unknown`). The theater stores MSL;
   `theater_switch` converts it to HAE exactly once per activation.
5. Sites and POIs: mapped context only (M14), never targets.

BLOCKING: the only callers are `asyncio.to_thread` under `wait_for(..., 60)`.
Duck-types `srv`; never imports `server.py` (§0.2). Untrusted text (labels,
places, POI names) is cleaned by `theaters.make_dynamic` and `geo_http`.
"""
from __future__ import annotations

import json
import logging
import math
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from . import geocode, realdata, safety, sites, theaters
from .geo import GeoidUnavailableError, canonical_altitude
from .geo_http import DiskCache, GeoFetch

_LOG = logging.getLogger(__name__)

#: AO half-extent bounds (C7): never smaller than 1.5 km, never wider than 25 km.
AO_MIN_HALF_M = 1500.0
AO_CAP_HALF_M = 25_000.0
#: The AO half-extent is at most this fraction of the airframe's reach.
AO_REACH_FRACTION = 0.4
#: Half-extent when neither the operator nor a geocoded box gives one.
DEFAULT_HALF: Mapping[str, float] = {"quad_suas_electric": 2500.0,
                                     "group3_fixed_wing": 15_000.0}
#: Open-ground homes are searched within this fraction of the half-extent.
HOME_SEARCH_FRACTION = 0.4
#: A home keeps this much room to the AO edge (the demo box plus 100 m).
HOME_CLEARANCE_M = theaters.DEMO_BOX_HALF_M + 100.0
#: POIs a dynamic theater carries (named sites first, then open ground).
POI_MAX = 3
#: Ground at or below this at an AO-centre home suggests open water.
WATER_GROUND_M = 1.0
#: `ProposalBook` bounds (§4.1.2 step 9).
PROPOSAL_MAX = 16
PROPOSAL_TTL_S = 1800.0
PROPOSAL_PREFIX = "TP-"
#: `args_match` tolerances: degrees and metres.
DEG_TOL = 1e-7
M_TOL = 0.01
#: Exactly what `sim_set_theater` must receive (§3.7).
SET_ARG_KEYS: tuple[str, ...] = ("proposal_id", "theater_id", "label", "ao", "home_lat",
                                 "home_lon", "ground_msl_m", "airframe")

#: `ground_source` sentences (§3.2), keyed by the provenance `ground.source`.
#: `operator` means `ground_msl_m` was passed to `theater_propose`, which the
#: analyst may have chosen itself, so it is not attributed to the operator
#: (review A; the §3.2 literal was "Set by the operator.").
GROUND_SOURCE_TEXT: Mapping[str, str] = {
    "reearth": ("Re:Earth terrain (ellipsoidal height), converted to sea level "
                "(EGM96) once."),
    "open-meteo": ("Copernicus DEM via Open-Meteo (EGM2008), used as sea level; the "
                   "geoid difference is not corrected."),
    "operator": "Given in the request (not measured).",
    "preset": "Theater table (checked against terrain to 50 m).",
}
#: `center.source` -> the geocoder name a slip and the theater block show.
GEOCODER_TEXT: Mapping[str, str] = {
    "photon": "Photon (OpenStreetMap)",
    "nominatim": "Nominatim (OpenStreetMap)",
    "coordinates": "Coordinates",
    "operator": "Coordinates",
    "preset": "Theater table",
}

HOME_WATER_CAVEAT = "Home is the AO centre; the ground there is at sea level (over water?)."
TERRAIN_MASKING_OFF = "Terrain masking is off."
TERRAIN_MASKING_ON = "Terrain masking is on."
FIXED_WING_CAVEAT = ("Fuel model only; the fake still flies multirotor kinematics "
                     "(hover, 20 m/s cap).")
GROUND_UNKNOWN_MESSAGE = ("No ground elevation could be measured here; give "
                          "ground_msl_m.")

#: OB categories whose `weapon_range_m` is a ground air-defence ring.
AIR_DEFENCE_CATEGORIES = frozenset({"sam", "aaa"})
#: ISR wording for the air-defence classes the envelope caveat names (M14:
#: a ring is something to stand off from, never something to attack).
_RING_WORDS: Mapping[str, str] = {
    "sam_long_range": "long-range air-defence",
    "sam_medium_range": "medium-range air-defence",
    "sam_short_range": "short-range air-defence",
    "spaag_missile": "gun and missile air-defence",
    "manpads": "shoulder-launched air-defence",
    "aaa_self_propelled": "self-propelled anti-aircraft gun",
    "aaa_towed": "towed anti-aircraft gun",
}


def refusal(error: str, message: str, **extra: Any) -> dict[str, Any]:
    """`{rejected, error, message}` (§3.1), plus any extra keys."""
    return {"rejected": True, "error": error, "message": message, **extra}


@dataclass(frozen=True, eq=False)
class Proposal:
    """One stored proposal. `theater` is what `sim_set_theater` activates."""

    proposal_id: str
    theater: theaters.Theater
    airframe: str
    sites: Any                                  # sites.SiteSet
    set_args: Mapping[str, Any]
    result: Mapping[str, Any]                   # the `theater_propose` body
    center: tuple[float, float]
    half_extent_m: float
    area_km2: float
    clamped_from_km: tuple[float, float] | None
    geocoder: str | None
    query: str | None
    home: Mapping[str, Any]                     # {lat, lon, name, source, distance_m, osm}
    ground_msl_m: float
    ground_source: str                          # a GROUND_SOURCE_TEXT key
    caveats: tuple[str, ...] = ()
    created_at_ms: int = 0
    expires_at_s: float = math.inf              # on the book's clock
    reach_m: float = 0.0
    ao_max_half_m: float = 0.0

    @property
    def id(self) -> str:
        return self.proposal_id

    def expired(self, now_s: float | None = None) -> bool:
        return (time.monotonic() if now_s is None else float(now_s)) >= self.expires_at_s


class ProposalBook:
    """At most `max` proposals, each valid `ttl_s` seconds. Thread-safe."""

    def __init__(self, max: int = PROPOSAL_MAX, ttl_s: float = PROPOSAL_TTL_S, *,
                 now: Callable[[], float] = time.monotonic) -> None:
        if int(max) < 1 or not float(ttl_s) > 0:
            raise ValueError("ProposalBook needs max >= 1 and ttl_s > 0")
        self.max = int(max)
        self.ttl_s = float(ttl_s)
        self._now = now
        self._rows: OrderedDict[str, Proposal] = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        for pid in [k for k, p in self._rows.items() if p.expired(now)]:
            del self._rows[pid]

    def put(self, p: Proposal) -> Proposal:
        """Store `p`, stamped to expire `ttl_s` from now; evicts the oldest
        beyond `max`. Returns the stored (stamped) proposal."""
        with self._lock:
            now = self._now()
            self._expire(now)
            stored = replace(p, expires_at_s=now + self.ttl_s)
            self._rows[stored.proposal_id] = stored
            self._rows.move_to_end(stored.proposal_id)
            while len(self._rows) > self.max:
                self._rows.popitem(last=False)
            return stored

    def get(self, proposal_id: Any) -> Proposal | None:
        """The proposal, or None when unknown, evicted or expired."""
        if not isinstance(proposal_id, str):
            return None
        with self._lock:
            self._expire(self._now())
            return self._rows.get(proposal_id)

    def __len__(self) -> int:
        with self._lock:
            self._expire(self._now())
            return len(self._rows)


def new_proposal_id() -> str:
    """`TP-<8 hex>`."""
    return f"{PROPOSAL_PREFIX}{secrets.token_hex(4)}"


def ao_max_half_m(af: Any) -> float:
    """`clamp(0.4 x reach, 1500, 25000)`: quad 2940 m, group 3 25000 m (C7)."""
    reach = safety.reach_radius_m(af)
    return min(AO_CAP_HALF_M, max(AO_MIN_HALF_M, AO_REACH_FRACTION * reach))


def ao_half_extent(af: Any, *, requested_m: float | None = None,
                   geocoded_half_m: float | None = None) -> tuple[float, bool]:
    """`(half, clamped)`: the requested half, else the geocoded half, else the
    airframe's default, clamped to `[1500, ao_max_half_m(af)]`."""
    prof = safety.get_airframe(af)
    want = next((float(v) for v in (requested_m, geocoded_half_m)
                 if v is not None and math.isfinite(float(v)) and float(v) > 0),
                DEFAULT_HALF.get(prof.id, DEFAULT_HALF[safety.DEFAULT_AIRFRAME_ID]))
    half = min(ao_max_half_m(prof), max(AO_MIN_HALF_M, want))
    return half, abs(half - want) > 1e-6


def _km(m: float) -> str:
    return f"{m / 1000.0:.1f}"


def envelope_caveats(half_m: float, af: Any, *, terrain_masking: bool = False) -> list[str]:
    """ISR caveats about the AO's scale against reference air-defence rings.

    Pure: the OB numbers are `targets.OB_LIBRARY` reference data (M14 wording:
    rings are something to stand off from). A square AO of half-extent `h`
    has half-diagonal `h x sqrt(2)`; a ring wider than that, centred on the
    AO, covers all of it. Only ground air-defence classes (`sam`, `aaa`)
    count: aircraft and boats are not rings.
    """
    from .targets import OB_LIBRARY

    prof = safety.get_airframe(af)
    size = _km(2.0 * float(half_m))
    half_diag = float(half_m) * math.sqrt(2.0)
    rings = sorted(((ob.weapon_range_m, _RING_WORDS.get(key, ob.role))
                    for key, ob in OB_LIBRARY.items()
                    if ob.category in AIR_DEFENCE_CATEGORIES and ob.engages_air
                    and ob.weapon_range_m > half_diag),
                   key=lambda r: -r[0])
    out: list[str] = []
    if rings:
        rng, word = rings[0]
        out.append(f"A {word} ring ({rng / 1000.0:g} km) would cover all of this "
                   f"{size} km AO.")
        if len(rings) > 1:
            rest = ", ".join(f"{w} ({r / 1000.0:g} km)" for r, w in rings[1:])
            out.append(f"Shorter rings that would also cover it: {rest}.")
    out.append(TERRAIN_MASKING_ON if terrain_masking else TERRAIN_MASKING_OFF)
    out.append(realdata.MAPPED_DATA_CAVEAT)
    if prof.id == safety.GROUP3_FIXED_WING.id:
        out.append(FIXED_WING_CAVEAT)
    return out


def ao_area_km2(ao: Sequence[Sequence[float]]) -> float:
    """Area of a lat/lon polygon, km² (local equirectangular shoelace)."""
    pts = [(float(p[0]), float(p[1])) for p in ao]
    if len(pts) < 3:
        return 0.0
    lat0 = sum(p[0] for p in pts) / len(pts)
    kx = 111_320.0 * math.cos(math.radians(lat0))
    xy = [(lon * kx, lat * 111_320.0) for lat, lon in pts]
    twice = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]))
    return abs(twice) / 2.0 / 1e6


def bbox_half_extent_m(bbox: Sequence[float]) -> float:
    """`min(half-width, half-height)` of an `[s, w, n, e]` box, metres."""
    width, height = geocode.bbox_size_m(tuple(float(v) for v in bbox))
    return min(width, height) / 2.0


def _close(a: Any, b: Any, tol: float) -> bool:
    try:
        if isinstance(a, bool) or isinstance(b, bool):
            return False
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return math.isfinite(fa) and math.isfinite(fb) and abs(fa - fb) <= tol


def _ao_close(a: Any, b: Any) -> bool:
    try:
        if len(a) != len(b):
            return False
        return all(len(u) == 2 and len(v) == 2 and _close(u[0], v[0], DEG_TOL)
                   and _close(u[1], v[1], DEG_TOL) for u, v in zip(a, b))
    except TypeError:
        return False


def args_match(p: Proposal, args: Mapping[str, Any]) -> list[str]:
    """Field names of `args` that differ from `p.set_args` (empty = exact).

    Strings compare exactly; coordinates to 1e-7 degrees; `ground_msl_m` to
    0.01 m; the AO vertex by vertex. A missing field is a mismatch.
    """
    want = p.set_args
    bad: list[str] = []
    for key in SET_ARG_KEYS:
        have = args.get(key) if isinstance(args, Mapping) else None
        if key == "ao":
            ok = _ao_close(have, want[key])
        elif key in ("home_lat", "home_lon"):
            ok = _close(have, want[key], DEG_TOL)
        elif key == "ground_msl_m":
            ok = _close(have, want[key], M_TOL)
        else:
            ok = isinstance(have, str) and have == want[key]
        if not ok:
            bad.append(key)
    return bad


# ---------------------------------------------------------------------------
# Building a proposal
# ---------------------------------------------------------------------------


class _Bad(ValueError):
    """An argument problem, surfaced as `{rejected, error, message}`."""

    def __init__(self, error: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.result = refusal(error, message, **extra)


def _num(value: Any, what: str) -> float | None:
    """None stays None; anything else must be a finite number (not a bool)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise _Bad("bad_args", f"{what} must be a number.")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise _Bad("bad_args", f"{what} must be a number.") from None
    if not math.isfinite(out):
        raise _Bad("bad_args", f"{what} must be finite.")
    return out


def _point(lat: Any, lon: Any, what: str) -> tuple[float, float] | None:
    la, lo = _num(lat, f"{what} latitude"), _num(lon, f"{what} longitude")
    if la is None and lo is None:
        return None
    if la is None or lo is None:
        raise _Bad("bad_args", f"Give both the {what} latitude and longitude.")
    if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
        raise _Bad("bad_args", f"The {what} ({la}, {lo}) is not a valid latitude and longitude.")
    return round(la, 7), round(lo, 7)


def _bbox_arg(bbox: Any) -> tuple[float, float, float, float] | None:
    """An `[s, w, n, e]` box that is real and does not wrap, or None."""
    if bbox is None:
        return None
    try:
        s, w, n, e = (_num(v, "bbox") for v in bbox)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, _Bad):
            raise
        raise _Bad("bad_args", "bbox must be [south, west, north, east].") from None
    if None in (s, w, n, e) or not (-90 <= s < n <= 90 and -180 <= w < e <= 180):
        raise _Bad("bad_args", "bbox must be [south, west, north, east] with south < north "
                               "and west < east.")
    return (s, w, n, e)


def _airframe(airframe: Any) -> safety.Airframe:
    try:
        return safety.get_airframe(airframe)
    except ValueError:
        raise _Bad("unknown_airframe",
                   f"Unknown airframe {str(airframe)[:40]!r}; known: "
                   f"{', '.join(sorted(safety.AIRFRAMES))}.") from None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hae(msl: float, lat: float, lon: float) -> float | None:
    try:
        return round(canonical_altitude(msl, lat, lon, datum="msl").alt_hae, 3)
    except (GeoidUnavailableError, ValueError):
        return None


def _ground(lat: float, lon: float, *, operator_msl: float | None, geodata: bool,
            fetch: GeoFetch | None) -> dict[str, Any] | None:
    """`{msl_m, source, hae_m, fetched_at_ms}` at a point, or None (T1).

    Operator first (it overrides both providers), then Re:Earth's ellipsoidal
    height through the one conversion point, then Open-Meteo used as MSL.
    Geodata off: only the operator's value exists.
    """
    if operator_msl is not None:
        return {"msl_m": round(operator_msl, 2), "source": "operator",
                "hae_m": _hae(operator_msl, lat, lon), "fetched_at_ms": None}
    if not geodata:
        return None
    try:
        sample = realdata.TerrainProvider(direct=True, fetch=fetch).heights([(lat, lon)])[0]
        if (sample.real and sample.msl_m is not None and math.isfinite(sample.msl_m)
                and sample.hae_m is not None):
            return {"msl_m": round(sample.msl_m, 2), "source": "reearth",
                    "hae_m": round(sample.hae_m, 3),
                    "fetched_at_ms": sample.provenance.retrieved_at_ms or _now_ms()}
        _LOG.info("Re:Earth ground unavailable at %.5f,%.5f: %s", lat, lon,
                  sample.provenance.reason)
    except Exception as exc:  # noqa: BLE001 — a provider bug must not kill the proposal
        _LOG.warning("Re:Earth ground lookup failed at %.5f,%.5f: %s", lat, lon, exc)
    try:
        msl = realdata.open_meteo_elevation(lat, lon, fetch=fetch)
    except realdata.RealDataUnavailable as exc:
        _LOG.info("Open-Meteo ground unavailable at %.5f,%.5f: %s", lat, lon, exc.reason)
        return None
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("Open-Meteo ground lookup failed at %.5f,%.5f: %s", lat, lon, exc)
        return None
    return {"msl_m": round(msl, 2), "source": "open-meteo", "hae_m": _hae(msl, lat, lon),
            "fetched_at_ms": _now_ms()}


def _clear_of_edges(probe: theaters.Theater, lat: float, lon: float) -> bool:
    """True when a `HOME_CLEARANCE_M` box around the point stays in the AO."""
    return all(probe.contains(*probe.point_at(dn * HOME_CLEARANCE_M, de * HOME_CLEARANCE_M,
                                              ref=(lat, lon)))
               for dn, de in ((-1, -1), (-1, 1), (1, 1), (1, -1)))


def _ring_inside(probe: theaters.Theater, lat: float, lon: float) -> bool:
    """The `validate()` POI rule: the point and its orbit ring are in the AO."""
    return probe.contains(lat, lon) and all(
        probe.contains(a, b) for a, b in probe.ring(lat, lon, probe.orbit_radius_m))


def _pois(probe: theaters.Theater, site_set: Any, ground: Sequence[Any]) -> list[dict]:
    """Up to `POI_MAX` named sites by salience, then open-ground names, each
    kept only when its orbit ring stays inside the AO (§4.1.2 step 6)."""
    picked: list[dict] = []
    seen: set[str] = set()
    pool = [(s.name, s.lat, s.lon, "site", s.osm) for s in site_set.top(60, named_only=True)]
    pool += [(g.name, g.lat, g.lon, "open-ground", g.osm) for g in ground]
    for name, lat, lon, source, osm in pool:
        name = theaters.clean_text(name, limit=theaters.LABEL_MAX)
        if len(picked) >= POI_MAX or not name or name.casefold() in seen:
            continue
        if _ring_inside(probe, lat, lon):
            seen.add(name.casefold())
            picked.append({"name": name, "lat": round(float(lat), 7),
                           "lon": round(float(lon), 7), "source": source, "osm": osm})
    return picked


def _sites_block(site_set: Any) -> dict[str, Any]:
    return {"counts": site_set.counts(),
            "capped": {k: True for k, v in dict(site_set.capped).items() if v},
            "total": site_set.total, "attribution": sites.ATTRIBUTION,
            "caveat": sites.SITES_CAVEAT, "degraded": site_set.degraded,
            "reason": site_set.reason}


def _finish(*, t: theaters.Theater, prof: safety.Airframe, site_set: Any,
            center: tuple[float, float], half: float,
            clamped_from_km: tuple[float, float] | None, geocoder: str | None,
            query: str | None, home: Mapping[str, Any], ground: Mapping[str, Any],
            caveats: Sequence[str], proposal_id: str) -> Proposal:
    """`set_args`, the `theater_propose` body and the stored `Proposal`."""
    bbox = [round(v, 7) for v in t.bbox()]
    width, height = geocode.bbox_size_m(t.bbox())
    area = round(ao_area_km2(t.ao), 2)
    reach, max_half = safety.reach_radius_m(prof), ao_max_half_m(prof)
    set_args = {"proposal_id": proposal_id, "theater_id": t.id, "label": t.label,
                "ao": [[lat, lon] for lat, lon in t.ao], "home_lat": t.home_lat,
                "home_lon": t.home_lon, "ground_msl_m": t.home_alt_msl_m,
                "airframe": prof.id}
    source = str(ground["source"])
    result = {
        "proposal_id": proposal_id, "simulated_world": True,
        "theater": {"id": t.id, "label": t.label, "place": t.place,
                    "ao": set_args["ao"], "bbox": bbox,
                    "ao_km": [round(width / 1000.0, 2), round(height / 1000.0, 2)],
                    "area_km2": area, "home": [t.home_lat, t.home_lon, t.home_alt_msl_m],
                    "pois": [p.as_dict() for p in t.pois], "dynamic": t.dynamic},
        "airframe": {"id": prof.id, "label": prof.label or prof.id, "summary": prof.summary,
                     "reach_m": round(reach, 1), "ao_max_half_m": round(max_half, 1)},
        "ground": {"msl_m": t.home_alt_msl_m,
                   "provenance": {"source": source, "text": GROUND_SOURCE_TEXT.get(source),
                                  "hae_m": ground.get("hae_m"),
                                  "fetched_at_ms": ground.get("fetched_at_ms")}},
        "sites": _sites_block(site_set),
        "caveats": list(caveats),
        "set_args": set_args,
    }
    return Proposal(
        proposal_id=proposal_id, theater=t, airframe=prof.id, sites=site_set,
        set_args=set_args, result=result, center=(round(center[0], 7), round(center[1], 7)),
        half_extent_m=round(half, 1), area_km2=area, clamped_from_km=clamped_from_km,
        geocoder=geocoder, query=query, home=dict(home), ground_msl_m=t.home_alt_msl_m,
        ground_source=source, caveats=tuple(caveats), created_at_ms=_now_ms(),
        reach_m=round(reach, 1), ao_max_half_m=round(max_half, 1))


def from_preset(theater_id: str, *, airframe: Any = None, geodata: bool = False,
                cache: DiskCache | None = None, fetch: GeoFetch | None = None,
                terrain_masking: bool = False,
                proposal_id: str | None = None) -> Proposal | dict[str, Any]:
    """A proposal for a known theater: a table row (the table wins) or an
    already-registered dynamic one. The only network use is the optional
    sites fetch (geodata on)."""
    try:
        prof = _airframe(airframe)
    except _Bad as exc:
        return exc.result
    if not theaters.is_known(theater_id):
        return refusal("unknown_theater",
                       f"Unknown theater {str(theater_id)[:60]!r}; give coordinates or a "
                       "theater id from the table.")
    t = theaters.get(theater_id)
    bbox = t.bbox()
    site_set = (sites.fetch_sites(bbox, cache=cache, fetch=fetch) if geodata
                else sites.empty_set(sites.GEODATA_OFF_REASON, bbox=bbox))
    prov = t.provenance if isinstance(t.provenance, Mapping) else {}
    center_src = (prov.get("center") or {}).get("source") if prov else "preset"
    home_prov = (prov.get("home") or {}) if prov else {}
    ground_prov = (prov.get("ground") or {}) if prov else {}
    center = t.center()
    home = {"lat": t.home_lat, "lon": t.home_lon, "name": home_prov.get("name"),
            "source": home_prov.get("source") or "preset", "osm": home_prov.get("osm"),
            "distance_m": round(safety.haversine_m(center[0], center[1],
                                                   t.home_lat, t.home_lon), 1)}
    ground = {"msl_m": t.home_alt_msl_m,
              "source": ground_prov.get("source") or "preset",
              "hae_m": ground_prov.get("hae_m", _hae(t.home_alt_msl_m, t.home_lat, t.home_lon)),
              "fetched_at_ms": ground_prov.get("fetched_at_ms")}
    caveats = []
    if geodata and site_set.degraded:
        caveats.append(f"Mapped sites unavailable ({site_set.reason}).")
    half = bbox_half_extent_m(bbox)
    caveats += envelope_caveats(half, prof, terrain_masking=terrain_masking)
    return _finish(t=t, prof=prof, site_set=site_set, center=center, half=half,
                   clamped_from_km=None, geocoder=GEOCODER_TEXT.get(center_src or "preset"),
                   query=(prov.get("center") or {}).get("query") if prov else None,
                   home=home, ground=ground, caveats=caveats,
                   proposal_id=proposal_id or new_proposal_id())


def _clamp_caveat(prof: safety.Airframe, want_km: tuple[float, float], half: float) -> str:
    size = _km(2.0 * half)
    if 2.0 * half < min(want_km) * 1000.0:
        return (f"The {want_km[0]:.1f} × {want_km[1]:.1f} km area was reduced to "
                f"{size} × {size} km to stay within the {prof.label or prof.id} reach.")
    return f"The area was enlarged to the {size} × {size} km minimum."


def build_proposal(*, theater_id: str | None = None, lat: Any = None, lon: Any = None,
                   place_id: str | None = None, label: str | None = None,
                   place: str | None = None, bbox: Sequence[Any] | None = None,
                   half_extent_m: Any = None, airframe: Any = None,
                   home_lat: Any = None, home_lon: Any = None, ground_msl_m: Any = None,
                   query: str | None = None, geodata: bool = False,
                   cache: DiskCache | None = None, fetch: GeoFetch | None = None,
                   terrain_masking: bool = False,
                   proposal_id: str | None = None) -> Proposal | dict[str, Any]:
    """Build (not store) a proposal; a refusal dict when it cannot (§4.1.2).

    BLOCKING. `theater_id` takes the preset path. Otherwise the centre is
    `lat`/`lon`, else the cached `place_id` entry. Network use (geodata on
    only): open ground, one Re:Earth sample (Open-Meteo as fallback) and the
    sites fetch, all through `fetch` when injected.
    """
    pid = proposal_id or new_proposal_id()
    if theater_id:
        return from_preset(theater_id, airframe=airframe, geodata=geodata, cache=cache,
                           fetch=fetch, terrain_masking=terrain_masking, proposal_id=pid)
    try:
        return _build_dynamic(
            lat=lat, lon=lon, place_id=place_id, label=label, place=place, bbox=bbox,
            half_extent_m=half_extent_m, airframe=airframe, home_lat=home_lat,
            home_lon=home_lon, ground_msl_m=ground_msl_m, query=query, geodata=geodata,
            cache=cache, fetch=fetch, terrain_masking=terrain_masking, pid=pid)
    except _Bad as exc:
        return exc.result


def _build_dynamic(*, lat: Any, lon: Any, place_id: Any, label: Any, place: Any,
                   bbox: Any, half_extent_m: Any, airframe: Any, home_lat: Any,
                   home_lon: Any, ground_msl_m: Any, query: Any, geodata: bool,
                   cache: DiskCache | None, fetch: GeoFetch | None,
                   terrain_masking: bool, pid: str) -> Proposal | dict[str, Any]:
    prof = _airframe(airframe)
    cached = (geocode.cached_place(str(place_id), cache=cache)
              if isinstance(place_id, str) and place_id else None)
    center = _point(lat, lon, "centre")
    if center is None and cached is not None:
        center = (round(cached.lat, 7), round(cached.lon, 7))
    if center is None:
        raise _Bad("bad_args", "Give lat and lon, a place_id from geo_lookup, or a theater_id.")
    box = _bbox_arg(bbox) or (cached.bbox if cached is not None else None)
    geocoded = bbox_half_extent_m(box) if box is not None else None
    requested = _num(half_extent_m, "half_extent_m")
    if requested is not None and requested <= 0:
        raise _Bad("bad_args", "half_extent_m must be positive.")
    half, clamped = ao_half_extent(prof, requested_m=requested, geocoded_half_m=geocoded)
    clamped_from_km = None
    caveats: list[str] = []
    if clamped:
        if requested is not None:
            clamped_from_km = (round(2 * requested / 1000.0, 1),) * 2
        elif box is not None:
            w, h = geocode.bbox_size_m(box)
            clamped_from_km = (round(w / 1000.0, 1), round(h / 1000.0, 1))
        if clamped_from_km is not None:
            caveats.append(_clamp_caveat(prof, clamped_from_km, half))
    name = theaters.clean_text(label, limit=theaters.LABEL_MAX)
    if not name:
        name = (cached.name if cached is not None and cached.name
                else f"Area {center[0]:.4f}, {center[1]:.4f}")
    where = (theaters.clean_text(place, limit=theaters.PLACE_MAX)
             or (cached.label if cached is not None else "")
             or f"{center[0]:.5f}, {center[1]:.5f}")
    try:
        probe = theaters.make_dynamic(label=name, place=where, center=center,
                                      half_extent_m=half, home=center,
                                      home_alt_msl_m=0.0, provenance=None)
    except ValueError as exc:
        return refusal("theater_invalid", f"This area cannot be a theater: {exc}.",
                       problems=[str(exc)])
    # ---- home ----
    ground_pool: list[Any] = []
    op_home = _point(home_lat, home_lon, "home")
    if op_home is not None:
        if not probe.contains(*op_home):
            return refusal("home_outside_ao", "The home point is outside the proposed area.")
        home = {"lat": op_home[0], "lon": op_home[1], "name": None, "source": "operator",
                "osm": None}
    else:
        home = None
        if geodata:
            ground_pool = sites.open_ground(center, HOME_SEARCH_FRACTION * half,
                                            cache=cache, fetch=fetch)
            for g in ground_pool:
                if probe.contains(g.lat, g.lon) and _clear_of_edges(probe, g.lat, g.lon):
                    home = {"lat": round(g.lat, 7), "lon": round(g.lon, 7), "name": g.name,
                            "source": "overpass-open-ground", "osm": g.osm}
                    break
        if home is None:
            c = probe.center()
            home = {"lat": round(c[0], 7), "lon": round(c[1], 7), "name": None,
                    "source": "ao-centre", "osm": None}
    home["distance_m"] = round(safety.haversine_m(center[0], center[1],
                                                  home["lat"], home["lon"]), 1)
    # ---- ground (T1) ----
    ground = _ground(home["lat"], home["lon"],
                     operator_msl=_num(ground_msl_m, "ground_msl_m"),
                     geodata=geodata, fetch=fetch)
    if ground is None:
        return refusal("ground_unknown", GROUND_UNKNOWN_MESSAGE)
    if home["source"] == "ao-centre" and ground["msl_m"] <= WATER_GROUND_M:
        caveats.append(HOME_WATER_CAVEAT)
    if ground["source"] == "open-meteo":
        caveats.append(GROUND_SOURCE_TEXT["open-meteo"])
    # ---- sites and POIs ----
    site_set = (sites.fetch_sites(probe.bbox(), cache=cache, fetch=fetch) if geodata
                else sites.empty_set(sites.GEODATA_OFF_REASON, bbox=probe.bbox()))
    if geodata and site_set.degraded:
        caveats.append(f"Mapped sites unavailable ({site_set.reason}).")
    pois = _pois(probe, site_set, ground_pool)
    source = cached.source if cached is not None else "operator"
    provenance = {
        "proposal_id": pid,
        "center": {"lat": center[0], "lon": center[1], "source": source,
                   "place_id": cached.id if cached is not None else None,
                   "query": theaters.clean_text(query, limit=200) or None},
        "ao": {"half_extent_m": round(half, 1), "requested_m": requested,
               "geocoded_half_m": None if geocoded is None else round(geocoded, 1),
               "clamped": clamped, "airframe": prof.id,
               "reach_m": round(safety.reach_radius_m(prof), 1)},
        "home": {"source": home["source"], "name": home["name"], "osm": home["osm"],
                 "distance_m": home["distance_m"]},
        "ground": {"msl_m": ground["msl_m"], "source": ground["source"],
                   "hae_m": ground["hae_m"], "fetched_at_ms": ground["fetched_at_ms"]},
        "pois": [{"name": p["name"], "source": p["source"], "osm": p["osm"]} for p in pois],
        "sites": {"total": site_set.total, "degraded": site_set.degraded,
                  "reason": site_set.reason, "fetched_at_ms": site_set.fetched_at_ms,
                  "attribution": sites.ATTRIBUTION},
    }
    try:
        t = theaters.make_dynamic(
            label=name, place=where, center=center, half_extent_m=half,
            home=(home["lat"], home["lon"]), home_alt_msl_m=ground["msl_m"],
            pois=[(p["name"], p["lat"], p["lon"]) for p in pois], provenance=provenance)
    except ValueError as exc:
        return refusal("theater_invalid", f"This area cannot be a theater: {exc}.",
                       problems=[str(exc)])
    problems = theaters.validate([t])
    if problems:
        return refusal("theater_invalid", "The proposed theater failed its checks.",
                       problems=problems)
    caveats += envelope_caveats(half, prof, terrain_masking=terrain_masking)
    return _finish(t=t, prof=prof, site_set=site_set, center=center, half=half,
                   clamped_from_km=clamped_from_km, geocoder=GEOCODER_TEXT.get(source),
                   query=provenance["center"]["query"], home=home, ground=ground,
                   caveats=caveats, proposal_id=pid)


#: `theater_propose` arguments `propose` forwards (§3.7), plus `query`.
PROPOSE_ARG_KEYS: tuple[str, ...] = (
    "theater_id", "lat", "lon", "place_id", "label", "place", "bbox", "half_extent_m",
    "airframe", "home_lat", "home_lon", "ground_msl_m", "query")


def book_of(srv: Any) -> ProposalBook:
    """`srv.theater_proposals`, created on first use (A7's `register` makes it)."""
    book = getattr(srv, "theater_proposals", None)
    if book is None:
        book = ProposalBook()
        srv.theater_proposals = book
    return book


def propose(srv: Any, args: Mapping[str, Any], *,
            fetch: GeoFetch | None = None) -> dict[str, Any]:
    """`theater_propose` against a server: build, store, return the body.

    BLOCKING (call it through `asyncio.to_thread`). Reads `srv.geodata_enabled`,
    `srv.geo_cache`, `srv.airframe_id` (the default airframe) and `srv.real`
    (terrain masking). A refusal dict is returned as is and stores nothing.
    """
    kw = {k: args[k] for k in PROPOSE_ARG_KEYS if k in args and args[k] is not None}
    kw.setdefault("airframe", getattr(srv, "airframe_id", None))
    out = build_proposal(**kw, geodata=bool(getattr(srv, "geodata_enabled", False)),
                         cache=getattr(srv, "geo_cache", None), fetch=fetch,
                         terrain_masking=getattr(srv, "real", None) is not None)
    if isinstance(out, dict):
        return out
    stored = book_of(srv).put(out)
    # A deep, JSON-proven copy: a caller decorating its reply cannot reach
    # the stored proposal.
    return json.loads(json.dumps(stored.result, allow_nan=False))


def _airframe_label(af_id: Any) -> str:
    try:
        prof = safety.get_airframe(af_id)
    except ValueError:
        return str(af_id)
    return prof.label or prof.id


def _fuel_rows(srv: Any, changed: bool) -> dict[str, Any]:
    monitors = dict(getattr(srv, "monitors", {}) or {})
    levels = [(v, float(m.fuel.fuel_pct)) for v, m in sorted(monitors.items())]
    now = ", ".join(f"{v} {pct:.1f}%" for v, pct in levels) or "No drones yet"
    if changed:
        after = "100% (full tank)"
    elif len(levels) == 1:
        after = f"{levels[0][1]:.1f}% (kept)"
    else:
        after = "Kept"
    return {"row": "Fuel", "now": now, "after": after}


def preview(srv: Any, proposal: Proposal) -> dict[str, Any]:
    """`theater_preview` (§3.6) for the sim slip: caches only, no network, no RPC.

    Callers wrap it (`theater_tools.approval_preview` returns `{}` on error),
    but nothing here is expected to raise for a live server.
    """
    from . import theater_switch

    p = proposal
    t = p.theater
    cur = getattr(srv, "theater", None)
    cur_af = getattr(srv, "airframe_id", None)
    changed = cur_af != p.airframe
    geodata = "On" if getattr(srv, "geodata_enabled", False) else "Off"
    home = {k: p.home.get(k) for k in ("lat", "lon", "name", "source", "distance_m")}
    return {
        "label": t.label, "place": t.place, "query": p.query, "geocoder": p.geocoder,
        "center": [p.center[0], p.center[1]],
        "bbox": [round(v, 7) for v in t.bbox()],
        "half_extent_m": p.half_extent_m, "area_km2": p.area_km2,
        "clamped_from_km": None if p.clamped_from_km is None else list(p.clamped_from_km),
        "home": home, "ground_msl_m": p.ground_msl_m,
        "ground_source": GROUND_SOURCE_TEXT.get(p.ground_source, p.ground_source),
        "airframe": {"from": cur_af, "to": p.airframe, "label": _airframe_label(p.airframe),
                     "reach_m": p.reach_m},
        "previous": (None if cur is None else {"id": cur.id, "label": cur.label}),
        "now_after": [
            {"row": "Theater", "now": getattr(cur, "label", "Unknown"), "after": t.label},
            {"row": "Airframe", "now": _airframe_label(cur_af),
             "after": _airframe_label(p.airframe)},
            _fuel_rows(srv, changed),
            {"row": "Map data", "now": geodata, "after": geodata},
        ],
        "resets": list(theater_switch.RESETS), "keeps": list(theater_switch.KEEPS),
        "sites": {"total": p.sites.total, "degraded": p.sites.degraded},
        "caveats": list(p.caveats),
        "checks": theater_switch.quick_checks(srv, p),
    }
