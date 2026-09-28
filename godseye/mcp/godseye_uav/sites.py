"""Mapped strategic sites for a theater, from Overpass (WG v2 §4.1.5, D6, C3).

Sites are CONTEXT (M14): mapped OpenStreetMap features, not an order of
battle, never targets. Nothing here feeds `srv.targets`, and nothing here has
an attack action. Three products:

* `fetch_sites(bbox)` — the display set: one Overpass request with one
  `out tags bb <cap+1>` block per taxonomy category, classified here (first
  matching category wins), capped per category, cached 24 h.
* `fetch_exclusion(bbox)` — the Phase B keep-out set: every taxonomy footprint
  over the bbox expanded by 1 km, UNCAPPED (`out ids bb`), plus hospitals as
  `protected`. Any doubt (a failed fetch, a `remark`, the 16 MB cap, an element
  with no location, geodata off) makes it `complete=False`, which is the safe
  side: the wargame refuses rather than place a unit near an unseen site.
* `open_ground(center, radius_m)` — named parks and grass near a centre, the
  candidate homes `theater_plan` chooses from.

Every OSM string is untrusted (§0.2): names and tag values are bidi- and
control-stripped here, and only whitelisted tags are kept.

Taxonomy is data (`TAXONOMY`): the same tuples generate the Overpass QL and
classify the answer, so the query and the parser cannot drift. `bridge=yes`
is deliberately absent — it tags every road bridge and swamped a live Isfahan
probe (241 of 300 elements); only `man_made=bridge` counts.
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import geo_http
from .geo_http import DiskCache, GeoFetch, clean_text
from .realdata import RealDataUnavailable
from .safety import EARTH_RADIUS_M, haversine_m

_LOG = logging.getLogger(__name__)

#: Part of every cache key: a taxonomy change never serves an old parse.
TAXONOMY_VERSION = "tx1"
SITES_TTL_S = 86_400.0
ATTRIBUTION = "© OpenStreetMap contributors, ODbL"
SITES_CAVEAT = "Sites are mapped OpenStreetMap data (ODbL), not an order of battle."
#: `SiteSet.reason` when geodata is off (R25).
GEODATA_OFF_REASON = "map data is off"

SITE_EXCLUSION_M = 500.0
PROTECTED_EXCLUSION_M = 1000.0
PROTECTED = frozenset({"medical"})

#: Overpass QL `[timeout:]` values and the HTTP timeouts wrapped around them.
DISPLAY_QL_TIMEOUT_S = 25
EXCLUSION_QL_TIMEOUT_S = 60
DISPLAY_HTTP_TIMEOUT_S = 30.0
EXCLUSION_HTTP_TIMEOUT_S = 65.0
OPEN_GROUND_CAP = 40
#: The exclusion query covers the bbox grown by this much on every side.
EXCLUSION_PAD_M = 1000.0
#: Refuse areas wider than this (Overpass cost; the AO cap is 25 km).
MAX_BBOX_SPAN_DEG = 2.0

#: A condition is `(key, op, value)`, op "=" (exact) or "~" (regex).
Cond = tuple[str, str, str]

#: category → selectors; a selector is a tuple of conditions that must all hold.
TAXONOMY: tuple[tuple[str, tuple[tuple[Cond, ...], ...]], ...] = (
    ("airfield", ((("aeroway", "~", "^(aerodrome|heliport|helipad)$"),),
                  (("military", "=", "airfield"),))),
    ("military_base", ((("military", "~", "^(base|barracks|naval_base|range|training_area)$"),),
                       (("landuse", "=", "military"),))),
    ("port", ((("landuse", "=", "port"),), (("harbour", "=", "yes"),),
              (("amenity", "=", "ferry_terminal"),))),
    ("power", ((("power", "~", "^(plant|substation)$"),),)),
    ("fuel", ((("industrial", "~", "^(oil|refinery|fuel_depot)$"),),
              (("man_made", "=", "storage_tank"),
               ("content", "~", "^(oil|fuel|petroleum|gas)$")))),
    ("comms", ((("man_made", "=", "communications_tower"),),
               (("man_made", "~", "^(mast|tower)$"), ("tower:type", "=", "communication")),
               (("telecom", "~", "^(exchange|data_center)$"),))),
    ("bridge", ((("man_made", "=", "bridge"),),)),
    ("rail_hub", ((("railway", "=", "yard"),), (("railway", "=", "station"), ("train", "=", "yes")))),
    ("hq_gov", ((("office", "=", "government"),), (("amenity", "=", "townhall"),))),
    ("border_crossing", ((("barrier", "=", "border_control"),),)),
    ("medical", ((("amenity", "=", "hospital"),),)),
    ("dam", ((("waterway", "=", "dam"),),)),
)
CATEGORIES: tuple[str, ...] = tuple(cat for cat, _ in TAXONOMY)
#: The closed vocabulary (§3.9): the taxonomy plus `other`.
ALL_CATEGORIES: tuple[str, ...] = CATEGORIES + ("other",)

CAPS: Mapping[str, int] = {
    "airfield": 30, "military_base": 40, "port": 20, "power": 40, "fuel": 20,
    "comms": 30, "bridge": 20, "rail_hub": 20, "hq_gov": 30, "border_crossing": 10,
    "medical": 30, "dam": 10,
}
SALIENCE: Mapping[str, float] = {
    "military_base": 1.0, "airfield": 1.0,
    "port": 0.8, "power": 0.8, "fuel": 0.8,
    "dam": 0.6, "comms": 0.6, "rail_hub": 0.6, "hq_gov": 0.6,
    "bridge": 0.5, "border_crossing": 0.5,
    "medical": 0.4,
    "other": 0.3,
}
#: Copy deck (Appendix B): category → (singular, plural).
SITE_WORDS: Mapping[str, tuple[str, str]] = {
    "airfield": ("Airfield", "Airfields"), "military_base": ("Military site", "Military sites"),
    "port": ("Port", "Ports"), "power": ("Power", "Power"), "fuel": ("Fuel", "Fuel"),
    "comms": ("Comms", "Comms"), "bridge": ("Bridge", "Bridges"),
    "rail_hub": ("Rail hub", "Rail hubs"), "hq_gov": ("Government", "Government"),
    "border_crossing": ("Border crossing", "Border crossings"),
    "medical": ("Medical, protected", "Medical"), "dam": ("Dam", "Dams"),
    "other": ("Mapped site", "Mapped sites"),
}
#: Label for a site with no name ("Unnamed military site").
_UNNAMED: Mapping[str, str] = {
    "airfield": "airfield", "military_base": "military site", "port": "port",
    "power": "power site", "fuel": "fuel site", "comms": "comms site", "bridge": "bridge",
    "rail_hub": "rail hub", "hq_gov": "government site", "border_crossing": "border crossing",
    "medical": "hospital", "dam": "dam", "other": "mapped site",
}
TAG_WHITELIST: tuple[str, ...] = (
    "name", "name:en", "operator", "military", "aeroway", "icao", "iata", "power",
    "plant:source", "man_made", "landuse", "harbour", "industrial", "railway", "office",
    "amenity", "barrier", "waterway", "tower:type", "telecom",
)


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    """One mapped feature. `bounds` is `(s, w, n, e)`; a node's is a point."""

    osm_type: str                # node | way | relation
    osm_id: int
    category: str
    subtype: str                 # the tag that classified it, "aeroway=aerodrome"
    name: str | None
    lat: float
    lon: float
    bounds: tuple[float, float, float, float]
    tags: Mapping[str, str] = field(default_factory=dict, hash=False)  # whitelisted, cleaned
    tags_total: int = 0

    @property
    def osm(self) -> str:
        return f"{self.osm_type}/{self.osm_id}"

    @property
    def protected(self) -> bool:
        return self.category in PROTECTED

    @property
    def salience(self) -> float:
        return SALIENCE.get(self.category, SALIENCE["other"])

    @property
    def label(self) -> str:
        return self.name or f"Unnamed {_UNNAMED.get(self.category, 'mapped site')}"

    def graph_id(self, theater_id: str) -> str:
        """`sit:{theater_id}:{osm_type}/{osm_id}` (§3.2)."""
        return f"sit:{theater_id}:{self.osm}"

    def as_dict(self, *, max_tags: int | None = 6) -> dict[str, Any]:
        keys = [k for k in TAG_WHITELIST if k in self.tags]
        if max_tags is not None:
            keys = keys[:max(0, int(max_tags))]
        return {
            "osm": self.osm, "osm_type": self.osm_type, "osm_id": self.osm_id,
            "name": self.name, "label": self.label, "category": self.category,
            "subtype": self.subtype, "lat": round(self.lat, 6), "lon": round(self.lon, 6),
            "bounds": [round(v, 6) for v in self.bounds], "protected": self.protected,
            "salience": self.salience, "tags": {k: self.tags[k] for k in keys},
            "tags_total": self.tags_total, "source": "osm", "register": "mapped",
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Site:
        tags = d.get("tags") if isinstance(d.get("tags"), Mapping) else {}
        return cls(
            osm_type=str(d["osm_type"]), osm_id=int(d["osm_id"]),
            category=str(d["category"]), subtype=str(d.get("subtype") or ""),
            name=clean_text(d.get("name")) or None, lat=float(d["lat"]), lon=float(d["lon"]),
            bounds=tuple(float(v) for v in d["bounds"]),
            tags={str(k): clean_text(v) for k, v in tags.items() if k in TAG_WHITELIST},
            tags_total=int(d.get("tags_total") or 0))

    def to_json(self) -> dict[str, Any]:
        """Lossless row for the cache: every whitelisted tag, unrounded position."""
        return {**self.as_dict(max_tags=None), "lat": self.lat, "lon": self.lon,
                "bounds": list(self.bounds)}


@dataclass(frozen=True)
class SiteSet:
    """The mapped sites of one area, with the provenance a UI must show."""

    sites: tuple[Site, ...]
    real: bool
    reason: str | None = None
    fetched_at_ms: int | None = None
    capped: Mapping[str, bool] = field(default_factory=dict, hash=False)
    bbox: tuple[float, float, float, float] | None = None
    dropped: int = 0

    attribution = ATTRIBUTION
    caveat = SITES_CAVEAT

    @property
    def degraded(self) -> bool:
        return not self.real

    @property
    def total(self) -> int:
        return len(self.sites)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for site in self.sites:
            out[site.category] = out.get(site.category, 0) + 1
        return out

    def top(self, n: int, *, named_only: bool = False) -> list[Site]:
        """The `n` most salient sites (named first within a salience)."""
        pool = [s for s in self.sites if s.name or not named_only]
        pool.sort(key=lambda s: (-s.salience, s.name is None, s.label.casefold(), s.osm))
        return pool[:max(0, int(n))]

    def near(self, lat: float, lon: float, radius_m: float) -> list[Site]:
        """Sites whose footprint lies within `radius_m` of a point, nearest first."""
        scored = [(rect_distance_m(lat, lon, s.bounds), s) for s in self.sites]
        return [s for d, s in sorted(scored, key=lambda p: (p[0], p[1].osm))
                if d <= float(radius_m)]

    def rows(self, *, category: str | None = None, limit: int = 40,
             near_lat: float | None = None, near_lon: float | None = None,
             theater_id: str | None = None) -> list[dict[str, Any]]:
        pool = [s for s in self.sites if category is None or s.category == category]
        near = near_lat is not None and near_lon is not None
        if near:
            dist = {s.osm: rect_distance_m(float(near_lat), float(near_lon), s.bounds)
                    for s in pool}
            pool.sort(key=lambda s: (dist[s.osm], s.osm))
        else:
            pool.sort(key=lambda s: (-s.salience, s.name is None, s.label.casefold(), s.osm))
        out = []
        for site in pool[:max(0, int(limit))]:
            row = site.as_dict()
            if theater_id:
                row = {"id": site.graph_id(theater_id), **row}
            if near:
                row["distance_m"] = round(dist[site.osm], 1)
            out.append(row)
        return out

    def as_dict(self, *, category: str | None = None, limit: int = 40,
                near_lat: float | None = None, near_lon: float | None = None,
                theater_id: str | None = None) -> dict[str, Any]:
        """The `geo_sites` result body (§3.7)."""
        rows = self.rows(category=category, limit=limit, near_lat=near_lat,
                         near_lon=near_lon, theater_id=theater_id)
        return {
            "sites": rows, "returned": len(rows), "total": self.total,
            "counts": self.counts(), "capped": {k: True for k, v in self.capped.items() if v},
            "category": category, "real": self.real, "degraded": self.degraded,
            "reason": self.reason, "fetched_at_ms": self.fetched_at_ms,
            "bbox": None if self.bbox is None else [round(v, 6) for v in self.bbox],
            "attribution": ATTRIBUTION, "caveat": SITES_CAVEAT,
            "note": "Context only: mapped, not verified. A missing site is not an absent one.",
        }

    def to_json(self) -> dict[str, Any]:
        return {"v": TAXONOMY_VERSION, "fetched_at_ms": self.fetched_at_ms,
                "capped": {k: True for k, v in self.capped.items() if v},
                "bbox": None if self.bbox is None else list(self.bbox),
                "dropped": self.dropped, "sites": [s.to_json() for s in self.sites]}

    @classmethod
    def from_json(cls, d: Mapping[str, Any], *, real: bool = True,
                  reason: str | None = None) -> SiteSet:
        bbox = d.get("bbox")
        return cls(sites=tuple(Site.from_dict(r) for r in d.get("sites") or ()),
                   real=real, reason=reason, fetched_at_ms=d.get("fetched_at_ms"),
                   capped=dict(d.get("capped") or {}),
                   bbox=None if not bbox else tuple(float(v) for v in bbox),
                   dropped=int(d.get("dropped") or 0))


def empty_set(reason: str, *, bbox: Sequence[float] | None = None) -> SiteSet:
    """An empty, degraded set carrying why (e.g. `GEODATA_OFF_REASON`)."""
    box = _check_bbox(bbox) if bbox is not None else None
    return SiteSet(sites=(), real=False, reason=str(reason) or "no mapped sites", bbox=box)


def rect_distance_m(lat: float, lon: float, rect: Sequence[float]) -> float:
    """Metres from a point to an `(s, w, n, e)` rectangle; 0 inside it.
    The nearest rectangle point is the clamped point (exact enough at AO scale)."""
    s, w, n, e = rect
    return haversine_m(lat, lon, min(max(lat, s), n), min(max(lon, w), e))


def _check_bbox(bbox: Sequence[float] | None) -> tuple[float, float, float, float] | None:
    """`(s, w, n, e)` if valid and not larger than `MAX_BBOX_SPAN_DEG`; else None."""
    try:
        s, w, n, e = (float(v) for v in bbox)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (s, w, n, e)):
        return None
    if not (-90 <= s < n <= 90 and -180 <= w < e <= 180):
        return None
    if n - s > MAX_BBOX_SPAN_DEG or e - w > MAX_BBOX_SPAN_DEG:
        return None
    return (s, w, n, e)


@dataclass(frozen=True)
class Exclusion:
    """Keep-out footprints (Phase B). Rectangles are `(s, w, n, e)`."""

    rects: tuple[tuple[float, float, float, float], ...]
    protected: tuple[tuple[float, float, float, float], ...]
    complete: bool
    reason: str | None
    fetched_at_ms: int | None
    element_count: int

    def blocks(self, lat: float, lon: float, margin_m: float = SITE_EXCLUSION_M) -> bool:
        """True when the point is within `margin_m` of any footprint (0 inside)."""
        return _within(self.rects, lat, lon, margin_m)

    def protected_within(self, lat: float, lon: float,
                         margin_m: float = PROTECTED_EXCLUSION_M) -> bool:
        return _within(self.protected, lat, lon, margin_m)

    def as_dict(self) -> dict[str, Any]:
        return {"complete": self.complete, "reason": self.reason,
                "fetched_at_ms": self.fetched_at_ms, "element_count": self.element_count,
                "footprints": len(self.rects), "protected": len(self.protected),
                "margin_m": SITE_EXCLUSION_M, "protected_margin_m": PROTECTED_EXCLUSION_M}

    def to_json(self) -> dict[str, Any]:
        return {"v": TAXONOMY_VERSION, "rects": [list(r) for r in self.rects],
                "protected": [list(r) for r in self.protected],
                "fetched_at_ms": self.fetched_at_ms, "element_count": self.element_count}


def _within(rects: Sequence[Sequence[float]], lat: float, lon: float, margin_m: float) -> bool:
    margin = max(0.0, float(margin_m))
    # Cheap degree pre-filter (generous by 10 %), then the metric check.
    dlat = margin * 1.1 / 110_574.0
    dlon = margin * 1.1 / max(1.0, 111_320.0 * math.cos(math.radians(lat)))
    for s, w, n, e in rects:
        if lat < s - dlat or lat > n + dlat or lon < w - dlon or lon > e + dlon:
            continue
        if rect_distance_m(lat, lon, (s, w, n, e)) <= margin:
            return True
    return False


@dataclass(frozen=True)
class OpenGround:
    """A named open-ground candidate home (park, pitch, grass)."""

    name: str
    lat: float
    lon: float
    osm: str
    kind: str                    # "leisure=park"
    distance_m: float
    bounds: tuple[float, float, float, float]

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "lat": round(self.lat, 7), "lon": round(self.lon, 7),
                "osm": self.osm, "kind": self.kind, "distance_m": round(self.distance_m, 1),
                "bounds": [round(v, 7) for v in self.bounds]}


# ---------------------------------------------------------------------------
# Query, parse, fetch
# ---------------------------------------------------------------------------


def classify(tags: Mapping[str, str]) -> tuple[str, str] | None:
    """`(category, subtype)` of the first matching taxonomy selector, or None."""
    for category, selectors in TAXONOMY:
        for conds in selectors:
            if all(_holds(tags, c) for c in conds):
                key = conds[0][0]
                return category, f"{key}={clean_text(tags.get(key), 60)}"
    return None


def _holds(tags: Mapping[str, str], cond: Cond) -> bool:
    key, op, value = cond
    got = tags.get(key)
    if not isinstance(got, str):
        return False
    return got == value if op == "=" else _regex(value).fullmatch(got) is not None


_REGEX_CACHE: dict[str, Any] = {}


def _regex(pattern: str) -> Any:
    import re

    found = _REGEX_CACHE.get(pattern)
    if found is None:
        found = _REGEX_CACHE[pattern] = re.compile(pattern)
    return found


def _selector_ql(conds: Sequence[Cond]) -> str:
    return "".join(f'["{k}"="{v}"]' if op == "=" else f'["{k}"~"{v}"]' for k, op, v in conds)


def _bbox_ql(box: Sequence[float]) -> str:
    return ",".join(f"{v:.6f}" for v in box)


def overpass_query(bbox: Sequence[float]) -> str:
    """The display query: one `out tags bb <cap+1>` block per category."""
    box = _check_bbox(bbox)
    if box is None:
        raise ValueError(f"invalid bbox {bbox!r}: need [s,w,n,e], s<n, w<e, "
                         f"at most {MAX_BBOX_SPAN_DEG} degrees a side")
    lines = [f"[out:json][timeout:{DISPLAY_QL_TIMEOUT_S}][bbox:{_bbox_ql(box)}];"]
    for category, selectors in TAXONOMY:
        union = " ".join(f"nwr{_selector_ql(conds)};" for conds in selectors)
        lines.append(f"/* {category} */ ({union}); out tags bb {CAPS[category] + 1};")
    return "\n".join(lines)


def _pad(box: Sequence[float], pad_m: float) -> tuple[float, float, float, float]:
    s, w, n, e = box
    # Spherical metres, the metric `blocks()` measures with; the longitude pad
    # uses the more poleward edge so it is never short.
    dlat = math.degrees(pad_m / EARTH_RADIUS_M)
    dlon = math.degrees(pad_m / max(1.0, EARTH_RADIUS_M * math.cos(
        math.radians(max(abs(s), abs(n))))))
    return (max(-90.0, s - dlat), max(-180.0, w - dlon),
            min(90.0, n + dlat), min(180.0, e + dlon))


def exclusion_queries(bbox: Sequence[float]) -> tuple[str, str]:
    """(footprints, hospitals) queries over `bbox` grown by `EXCLUSION_PAD_M`."""
    box = _check_bbox(bbox)
    if box is None:
        raise ValueError(f"invalid bbox {bbox!r}")
    padded = _bbox_ql(_pad(box, EXCLUSION_PAD_M))
    head = f"[out:json][timeout:{EXCLUSION_QL_TIMEOUT_S}][bbox:{padded}];"
    every = " ".join(f"nwr{_selector_ql(conds)};"
                     for _, selectors in TAXONOMY for conds in selectors)
    footprints = f"{head}\n({every});\nout ids bb;"
    hospitals = f'{head}\n(nwr["amenity"="hospital"];);\nout ids bb;'
    return footprints, hospitals


def open_ground_query(lat: float, lon: float, radius_m: float) -> str:
    r = max(1, round(radius_m))
    at = f"around:{r},{lat:.6f},{lon:.6f}"
    return (f"[out:json][timeout:{DISPLAY_QL_TIMEOUT_S}];\n"
            f'( way["leisure"~"^(park|recreation_ground|pitch)$"]["name"]({at});\n'
            f'  relation["leisure"~"^(park|recreation_ground|pitch)$"]["name"]({at});\n'
            f'  way["landuse"~"^(grass|meadow)$"]["name"]({at}); );\n'
            f"out tags bb {OPEN_GROUND_CAP};")


def parse(elements: Iterable[Any]) -> tuple[list[Site], dict[str, bool], int]:
    """`(sites, capped, dropped)`: classified, deduplicated, capped per
    category (named first); unlocated or unclassifiable elements are dropped."""
    per_cat: dict[str, list[Site]] = {c: [] for c in CATEGORIES}
    seen: set[tuple[str, int]] = set()
    dropped = 0
    for el in elements:
        ref = _element_ref(el)
        if ref is None:
            dropped += 1
            continue
        if ref in seen:
            continue                                   # dedupe: not a drop
        seen.add(ref)
        raw = el.get("tags")
        tags = ({str(k): str(v) for k, v in raw.items() if isinstance(v, (str, int, float))}
                if isinstance(raw, Mapping) else {})
        found = classify(tags)
        located = _element_location(el)
        if found is None or located is None:
            dropped += 1                               # never placed at (0, 0)
            continue
        (lat, lon), bounds = located
        per_cat[found[0]].append(Site(
            osm_type=ref[0], osm_id=ref[1], category=found[0], subtype=found[1],
            name=clean_text(tags.get("name") or tags.get("name:en")) or None,
            lat=lat, lon=lon, bounds=bounds,
            tags={k: clean_text(tags[k]) for k in TAG_WHITELIST if k in tags},
            tags_total=len(tags)))
    sites: list[Site] = []
    capped: dict[str, bool] = {}
    for category in CATEGORIES:
        rows = sorted(per_cat[category], key=lambda s: s.name is None)   # stable
        if len(rows) > CAPS[category]:
            capped[category] = True
        sites.extend(rows[:CAPS[category]])
    return sites, capped, dropped


def _element_ref(el: Any) -> tuple[str, int] | None:
    if not isinstance(el, Mapping):
        return None
    kind, oid = el.get("type"), el.get("id")
    if kind not in ("node", "way", "relation") or isinstance(oid, bool) or not isinstance(
            oid, int):
        return None
    return (kind, oid)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _element_location(el: Any) -> tuple[tuple[float, float],
                                        tuple[float, float, float, float]] | None:
    """`((lat, lon), (s, w, n, e))` from a node's coordinates or an element's
    `bounds` (centre = bounds midpoint); None when it has neither."""
    if not isinstance(el, Mapping):
        return None
    lat, lon = _num(el.get("lat")), _num(el.get("lon"))
    if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
        return (lat, lon), (lat, lon, lat, lon)
    b = el.get("bounds")
    if isinstance(b, Mapping):
        s, w = _num(b.get("minlat")), _num(b.get("minlon"))
        n, e = _num(b.get("maxlat")), _num(b.get("maxlon"))
        if None not in (s, w, n, e) and -90 <= s <= n <= 90 and -180 <= w <= e <= 180:
            return ((s + n) / 2.0, (w + e) / 2.0), (s, w, n, e)
    return None


def _box_key(prefix: str, box: Sequence[float]) -> str:
    return f"{prefix}:{TAXONOMY_VERSION}:" + ",".join(f"{v:.2f}" for v in box)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _overpass(query: str, *, timeout_s: float, fetch: GeoFetch | None) -> Any:
    """POST one query to Overpass, retrying once on the kumi.systems mirror."""
    reasons: list[str] = []
    for upstream in ("overpass", "overpass-kumi"):
        try:
            return geo_http.fetch_json(upstream, geo_http.UPSTREAM_URLS[upstream],
                                       timeout_s=timeout_s, data={"data": query}, fetch=fetch)
        except RealDataUnavailable as exc:
            reasons.append(f"{upstream}: {exc.reason}")
            if exc.reason in (geo_http.EGRESS_DISABLED, geo_http.CAP_REASON):
                break                                  # a mirror would answer the same
    raise RealDataUnavailable("overpass", "; ".join(reasons))


def fetch_sites(bbox: Sequence[float], *, cache: DiskCache | None,
                fetch: GeoFetch | None = None, refresh: bool = False) -> SiteSet:
    """Mapped sites in `bbox`; cached 24 h. Never raises: a failure is an
    empty set with `real=False` and the reason. `refresh=True` skips the cache
    read (`geo_sites(refresh=true)`); a successful answer still updates it."""
    box = _check_bbox(bbox)
    if box is None:
        return empty_set(f"invalid area: need [s,w,n,e] at most {MAX_BBOX_SPAN_DEG} degrees")
    key = _box_key("sites", box)
    try:
        hit = (cache.get(key, ttl_s=SITES_TTL_S)
               if cache is not None and not refresh else None)
        if isinstance(hit, Mapping):
            return SiteSet.from_json(hit)
    except (KeyError, TypeError, ValueError) as exc:
        _LOG.warning("sites cache entry unreadable (%s); refetching", exc)
    try:
        payload = _overpass(overpass_query(box), timeout_s=DISPLAY_HTTP_TIMEOUT_S, fetch=fetch)
    except RealDataUnavailable as exc:
        return SiteSet(sites=(), real=False, bbox=box,
                       reason=f"map data feed down (Overpass: {exc.reason})")
    elements = payload.get("elements") if isinstance(payload, Mapping) else None
    if not isinstance(elements, list):
        return SiteSet(sites=(), real=False, bbox=box,
                       reason="map data feed down (malformed Overpass response)")
    sites, capped, dropped = parse(elements)
    remark = clean_text(payload.get("remark"), 200)
    result = SiteSet(
        sites=tuple(sites), real=not remark, fetched_at_ms=_now_ms(), capped=capped,
        bbox=box, dropped=dropped,
        reason=f"Overpass reported a problem ({remark}); sites may be missing" if remark
        else None)
    if cache is not None and result.real:
        cache.put(key, result.to_json())
    return result


def cached_sites(bbox: Sequence[float], *, cache: DiskCache | None) -> SiteSet:
    """`fetch_sites` from the cache only (no network). A stale entry is served
    degraded; a miss is an empty degraded set."""
    box = _check_bbox(bbox)
    if box is None:
        return empty_set("invalid area")
    if cache is None:
        return empty_set("mapped sites are not cached for this area", bbox=box)
    key = _box_key("sites", box)
    try:
        entry = cache.get_entry(key, ttl_s=math.inf)
        if entry is None:
            return empty_set("mapped sites are not fetched for this area yet", bbox=box)
        if cache.get_entry(key, ttl_s=SITES_TTL_S) is not None:
            return SiteSet.from_json(entry[1])
        return SiteSet.from_json(
            entry[1], real=False,
            reason="cached mapped sites are older than 24 h; refresh with geo_sites")
    except (KeyError, TypeError, ValueError, AttributeError):
        return empty_set("the cached mapped sites for this area are unreadable", bbox=box)


def fetch_exclusion(bbox: Sequence[float], *, cache: DiskCache | None,
                    fetch: GeoFetch | None = None, enabled: bool = True) -> Exclusion:
    """The keep-out set for `bbox` (+1 km). Never raises."""
    if not enabled:
        return Exclusion((), (), False, GEODATA_OFF_REASON, None, 0)
    box = _check_bbox(bbox)
    if box is None:
        return Exclusion((), (), False, "invalid area", None, 0)
    key = _box_key("excl", box)
    try:
        hit = cache.get(key, ttl_s=SITES_TTL_S) if cache is not None else None
        if isinstance(hit, Mapping):
            return Exclusion(
                rects=tuple(tuple(float(v) for v in r) for r in hit["rects"]),
                protected=tuple(tuple(float(v) for v in r) for r in hit["protected"]),
                complete=True, reason=None, fetched_at_ms=hit.get("fetched_at_ms"),
                element_count=int(hit.get("element_count") or 0))
    except (KeyError, TypeError, ValueError) as exc:
        _LOG.warning("exclusion cache entry unreadable (%s); refetching", exc)
    rects: list[tuple[float, float, float, float]] = []
    protected: list[tuple[float, float, float, float]] = []
    problems: list[str] = []
    count = unlocated = 0
    for query, bucket in zip(exclusion_queries(box), (rects, protected)):
        try:
            payload = _overpass(query, timeout_s=EXCLUSION_HTTP_TIMEOUT_S, fetch=fetch)
        except RealDataUnavailable as exc:
            problems.append(exc.reason)
            continue
        elements = payload.get("elements") if isinstance(payload, Mapping) else None
        if not isinstance(elements, list):
            problems.append("malformed Overpass response")
            continue
        remark = clean_text(payload.get("remark"), 200)
        if remark:
            problems.append(f"Overpass remark: {remark}")
        for el in elements:
            count += 1
            located = _element_location(el)
            if located is None:
                unlocated += 1
                continue
            bucket.append(located[1])
    if unlocated:
        problems.append(f"{unlocated} mapped element(s) had no location")
    result = Exclusion(rects=tuple(rects), protected=tuple(protected), complete=not problems,
                       reason="; ".join(problems) or None, fetched_at_ms=_now_ms(),
                       element_count=count)
    if cache is not None and result.complete:
        cache.put(key, result.to_json())
    return result


def open_ground(center: Sequence[float], radius_m: float, *, cache: DiskCache | None,
                fetch: GeoFetch | None = None) -> list[OpenGround]:
    """Named open ground within `radius_m` of `center` (lat, lon), nearest
    first. Never raises: a failure is an empty list."""
    try:
        lat, lon, radius = float(center[0]), float(center[1]), float(radius_m)
    except (TypeError, ValueError, IndexError):
        return []
    if not (math.isfinite(lat) and math.isfinite(lon) and math.isfinite(radius)
            and abs(lat) <= 90 and abs(lon) <= 180 and radius > 0):
        return []
    key = f"og:{TAXONOMY_VERSION}:{lat:.4f},{lon:.4f},{round(radius)}"
    rows = cache.get(key, ttl_s=SITES_TTL_S) if cache is not None else None
    if not isinstance(rows, list):
        try:
            payload = _overpass(open_ground_query(lat, lon, radius),
                                timeout_s=DISPLAY_HTTP_TIMEOUT_S, fetch=fetch)
        except RealDataUnavailable as exc:
            _LOG.info("open ground unavailable near %.4f,%.4f: %s", lat, lon, exc.reason)
            return []
        elements = payload.get("elements") if isinstance(payload, Mapping) else None
        if not isinstance(elements, list):
            return []
        rows = []
        for el in elements:
            ref, located = _element_ref(el), _element_location(el)
            tags = el.get("tags") if isinstance(el, Mapping) else None
            name = clean_text(tags.get("name")) if isinstance(tags, Mapping) else ""
            if ref is None or located is None or not name:
                continue
            kind = next((f"{k}={clean_text(tags[k], 40)}" for k in ("leisure", "landuse")
                         if isinstance(tags.get(k), str)), "")
            rows.append({"name": name, "lat": located[0][0], "lon": located[0][1],
                         "osm": f"{ref[0]}/{ref[1]}", "kind": kind,
                         "bounds": list(located[1])})
        if cache is not None and not payload.get("remark"):
            cache.put(key, rows)
    out: list[OpenGround] = []
    for row in rows:
        try:
            d = haversine_m(lat, lon, float(row["lat"]), float(row["lon"]))
            if d <= radius:
                out.append(OpenGround(
                    name=clean_text(row["name"]), lat=float(row["lat"]),
                    lon=float(row["lon"]), osm=str(row["osm"]), kind=str(row.get("kind", "")),
                    distance_m=d, bounds=tuple(float(v) for v in row["bounds"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda g: (g.distance_m, g.osm))
    return out
