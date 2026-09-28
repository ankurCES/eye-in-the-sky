"""Place lookup for runtime theaters: coordinates, then the theater table, then
Photon, then Nominatim (WG v2 §4.1.5, C9).

Why this order. Coordinates need no network. A preset row already carries a
checked home, AO and ground height. Photon (komoot, keyless) is the primary
geocoder because it answers in the same ~0.8 s as Nominatim without the public
Nominatim instance's strict 1 req/s, no-bulk policy; Nominatim is the fallback
when Photon fails or finds nothing.

Shapes that differ between the two, and are the classic inversion bugs:

* Photon: GeoJSON `coordinates [lon, lat]`, `properties.extent [W, N, E, S]`.
* Nominatim `jsonv2`: string `lat`/`lon`, `boundingbox ["S", "N", "W", "E"]`.

Both become `Place.bbox = (s, w, n, e)` — the one bbox order in godSeye (R4).

Everything a geocoder returns is untrusted text (§0.2): names are bidi- and
control-stripped to 160 characters here, before anything else sees them.
`lookup` never raises; a failure is an empty candidate list with a reason.
Results are cached 30 days (`GEOCODE_TTL_S`); every `Place` is also cached by
id so `theater_propose(place_id=…)` can recover its box without a new request.
"""
from __future__ import annotations

import math
import re
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import geo_http
from .geo_http import CallBudget, DiskCache, GeoFetch, clean_text
from .realdata import RealDataUnavailable
from .safety import haversine_m

ATTRIBUTION = ("Geocoding: Photon by komoot / Nominatim; "
               "data © OpenStreetMap contributors, ODbL")

#: Geocoder results do not move on a mission timescale.
GEOCODE_TTL_S = 30 * 86_400.0
#: Per-request timeout for either geocoder, seconds.
GEOCODE_TIMEOUT_S = 8.0
#: Untrusted names are cut to this many characters.
NAME_MAX = 160
#: `geo_lookup` bounds (§3.7).
QUERY_MIN, QUERY_MAX = 2, 200
LIMIT_MIN, LIMIT_MAX = 1, 10
#: Bumped when the cached candidate shape changes.
CACHE_VERSION = "g1"

#: `source` → the human geocoder name the theater block shows (§3.2).
GEOCODER_LABEL: Mapping[str, str] = {
    "photon": "Photon (OpenStreetMap)",
    "nominatim": "Nominatim (OpenStreetMap)",
    "coordinates": "Coordinates",
    "preset": "Theater table",
}

#: The reason `lookup(enabled=False)` gives for a place name (R25).
GEODATA_OFF_REASON = "map data is off; give coordinates"

_OSM_TYPE = {"N": "node", "W": "way", "R": "relation",
             "node": "node", "way": "way", "relation": "relation"}


@dataclass(frozen=True)
class Place:
    """One geocoder candidate. `bbox` is `(s, w, n, e)` or None."""

    id: str                      # "photon:relation/7902476", "coords:12.97160,77.59460"
    name: str
    label: str
    lat: float
    lon: float
    source: str                  # photon | nominatim | coordinates | preset
    bbox: tuple[float, float, float, float] | None = None
    kind: str | None = None      # the geocoder's place type (city, park, …)
    osm: str | None = None       # "relation/7902476"
    theater_id: str | None = None  # preset rows only

    @property
    def geocoder(self) -> str | None:
        return GEOCODER_LABEL.get(self.source)

    def size_m(self) -> tuple[float, float] | None:
        """(width_m, height_m) of the bbox, or None."""
        return None if self.bbox is None else bbox_size_m(self.bbox)

    def geocoded_half_m(self) -> float | None:
        """Half the shorter side of the bbox in metres (theater_plan's
        `geocoded_half`), or None without a box."""
        size = self.size_m()
        return None if size is None else min(size) / 2.0

    def as_dict(self) -> dict[str, Any]:
        size = self.size_m()
        return {
            "id": self.id, "name": self.name, "label": self.label,
            "lat": round(self.lat, 7), "lon": round(self.lon, 7),
            "bbox": None if self.bbox is None else [round(v, 7) for v in self.bbox],
            "size_km": None if size is None else [round(size[0] / 1000.0, 1),
                                                  round(size[1] / 1000.0, 1)],
            "source": self.source, "geocoder": self.geocoder,
            "kind": self.kind, "osm": self.osm, "theater_id": self.theater_id,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Place:
        bbox = d.get("bbox")
        return cls(
            id=str(d["id"]), name=clean_text(d.get("name"), NAME_MAX),
            label=clean_text(d.get("label"), NAME_MAX),
            lat=float(d["lat"]), lon=float(d["lon"]), source=str(d["source"]),
            bbox=None if not bbox else _valid_bbox(*bbox),
            kind=d.get("kind"), osm=d.get("osm"), theater_id=d.get("theater_id"))


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _valid_point(lat: Any, lon: Any) -> tuple[float, float] | None:
    la, lo = _finite(lat), _finite(lon)
    if la is None or lo is None or abs(la) > 90.0 or abs(lo) > 180.0:
        return None
    return (la, lo)


def _valid_bbox(s: Any, w: Any, n: Any, e: Any) -> tuple[float, float, float, float] | None:
    """`(s, w, n, e)` when it is a real, non-wrapping box; None otherwise.
    Latitudes are re-ordered (a swapped pair is still one box); a west edge
    east of the east edge is an antimeridian wrap, dropped like GEV does."""
    vals = [_finite(v) for v in (s, w, n, e)]
    if any(v is None for v in vals):
        return None
    s_, w_, n_, e_ = vals
    s_, n_ = min(s_, n_), max(s_, n_)
    if abs(s_) > 90 or abs(n_) > 90 or abs(w_) > 180 or abs(e_) > 180 or w_ > e_:
        return None
    return (s_, w_, n_, e_)


def bbox_size_m(bbox: tuple[float, float, float, float] | list[float]
                ) -> tuple[float, float]:
    """(width_m, height_m) of an `[s, w, n, e]` box, measured at its middle."""
    s, w, n, e = (float(v) for v in bbox)
    mid_lat, mid_lon = (s + n) / 2.0, (w + e) / 2.0
    return (haversine_m(mid_lat, w, mid_lat, e), haversine_m(s, mid_lon, n, mid_lon))


def parse_coords(text: str) -> tuple[float, float] | None:
    """`(lat, lon)` from "12.9716, 77.5946", "12.9716 77.5946",
    "12.9716N 77.5946E" or "12°58'N"-free decimal forms; None otherwise."""
    m = _COORDS.match(str(text or ""))
    if not m:
        return None
    lat, lat_h, lon, lon_h = float(m.group(1)), m.group(2), float(m.group(3)), m.group(4)
    if lat_h:
        lat = -abs(lat) if lat_h in "Ss" else abs(lat)
    if lon_h:
        lon = -abs(lon) if lon_h in "Ww" else abs(lon)
    return _valid_point(lat, lon)


_NUM = r"[-+]?\d{1,3}(?:\.\d+)?"
_COORDS = re.compile(
    rf"^\s*\(?\s*({_NUM})\s*°?\s*([NnSs])?\s*[,;\s]\s*({_NUM})\s*°?\s*([EeWw])?\s*\)?\s*$")


def _coords_place(lat: float, lon: float) -> Place:
    text = f"{lat:.5f}, {lon:.5f}"
    return Place(id=f"coords:{lat:.5f},{lon:.5f}", name=text, label=text,
                 lat=lat, lon=lon, source="coordinates")


def _osm_ref(osm_type: Any, osm_id: Any) -> str | None:
    kind = _OSM_TYPE.get(str(osm_type))
    if kind is None or isinstance(osm_id, bool):
        return None
    try:
        return f"{kind}/{int(osm_id)}"
    except (TypeError, ValueError):
        return None


def parse_photon(payload: Any, *, limit: int = LIMIT_MAX) -> list[Place]:
    """Photon GeoJSON → Places. Skips features without a usable point."""
    features = payload.get("features") if isinstance(payload, Mapping) else None
    if not isinstance(features, list):
        raise RealDataUnavailable("photon", "malformed response (no features array)")
    out: list[Place] = []
    for feature in features:
        if not isinstance(feature, Mapping):
            continue
        geometry = feature.get("geometry")
        coords = geometry.get("coordinates") if isinstance(geometry, Mapping) else None
        if not (isinstance(coords, list) and len(coords) >= 2
                and geometry.get("type") == "Point"):
            continue
        point = _valid_point(coords[1], coords[0])          # GeoJSON is [lon, lat]
        if point is None:
            continue
        props = feature.get("properties")
        props = props if isinstance(props, Mapping) else {}
        osm = _osm_ref(props.get("osm_type"), props.get("osm_id"))
        extent = props.get("extent")                          # [W, N, E, S]
        bbox = (_valid_bbox(extent[3], extent[0], extent[1], extent[2])
                if isinstance(extent, list) and len(extent) == 4 else None)
        name = clean_text(props.get("name"), NAME_MAX) or clean_text(
            props.get("city") or props.get("state") or props.get("country"), NAME_MAX)
        parts = [name]
        for key in ("district", "city", "state", "country"):
            part = clean_text(props.get(key), NAME_MAX)
            if part and part not in parts:
                parts.append(part)
        name = name or f"{point[0]:.5f}, {point[1]:.5f}"
        out.append(Place(
            id=f"photon:{osm}" if osm else f"photon:{point[0]:.5f},{point[1]:.5f}",
            name=name, label=clean_text(", ".join(p for p in parts if p), NAME_MAX) or name,
            lat=point[0], lon=point[1], source="photon", bbox=bbox, osm=osm,
            kind=clean_text(props.get("type") or props.get("osm_value"), 40) or None))
    return _dedupe(out, limit)


def parse_nominatim(payload: Any, *, limit: int = LIMIT_MAX) -> list[Place]:
    """Nominatim `jsonv2` list → Places. Skips rows without a usable point."""
    if not isinstance(payload, list):
        raise RealDataUnavailable("nominatim", "malformed response (not a list)")
    out: list[Place] = []
    for row in payload:
        if not isinstance(row, Mapping):
            continue
        point = _valid_point(row.get("lat"), row.get("lon"))   # strings in jsonv2
        if point is None:
            continue
        osm = _osm_ref(row.get("osm_type"), row.get("osm_id"))
        bb = row.get("boundingbox")                              # ["S", "N", "W", "E"]
        bbox = (_valid_bbox(bb[0], bb[2], bb[1], bb[3])
                if isinstance(bb, list) and len(bb) == 4 else None)
        display = clean_text(row.get("display_name"), NAME_MAX)
        name = (clean_text(row.get("name"), NAME_MAX) or display.split(",")[0].strip()
                or f"{point[0]:.5f}, {point[1]:.5f}")
        out.append(Place(
            id=f"nominatim:{osm}" if osm else f"nominatim:{point[0]:.5f},{point[1]:.5f}",
            name=name, label=display or name, lat=point[0], lon=point[1],
            source="nominatim", bbox=bbox, osm=osm,
            kind=clean_text(row.get("addresstype") or row.get("type"), 40) or None))
    return _dedupe(out, limit)


def _dedupe(places: list[Place], limit: int) -> list[Place]:
    seen: set[str] = set()
    out: list[Place] = []
    for place in places:
        if place.id in seen:
            continue
        seen.add(place.id)
        out.append(place)
        if len(out) >= limit:
            break
    return out


def preset_places(query: str) -> list[Place]:
    """Theater-table rows whose id, label or place name the query names
    exactly (case-insensitive); no substring guessing."""
    from . import theaters

    wanted = _norm(query)
    if not wanted:
        return []
    return [_preset_place(t) for t in theaters.THEATERS.values() if wanted in _preset_keys(t)]


def _norm(text: str) -> str:
    return " ".join(str(text).casefold().split()).strip(" .,;")


def _preset_keys(t: Any) -> set[str]:
    """id; label and place without parentheticals; their first comma part;
    the part after an em dash ("Iran — Isfahan" → "isfahan")."""
    keys = {_norm(t.id)}
    for text in (t.label, t.place):
        base = re.sub(r"\s*\([^)]*\)", "", text).strip()
        keys.add(_norm(base))
        keys.add(_norm(base.split(",")[0]))
        if "—" in base:
            keys.add(_norm(base.split("—")[-1]))
    keys.discard("")
    return keys


def _preset_place(t: Any) -> Place:
    lat, lon = t.center()
    return Place(id=f"preset:{t.id}", name=clean_text(t.label, NAME_MAX),
                 label=clean_text(t.place, NAME_MAX), lat=lat, lon=lon, source="preset",
                 bbox=tuple(t.bbox()), kind="theater", theater_id=t.id)


def _place_key(place_id: str) -> str:
    return f"place:{CACHE_VERSION}:{place_id}"


def cached_place(place_id: str, *, cache: DiskCache | None) -> Place | None:
    """A Place previously returned by `lookup`, by id, from the cache only.
    Coordinates and preset ids are rebuilt without the cache."""
    pid = str(place_id or "")
    if pid.startswith("coords:"):
        lat, _, lon = pid[len("coords:"):].partition(",")
        point = _valid_point(lat, lon)
        return None if point is None else _coords_place(*point)
    if pid.startswith("preset:"):
        from . import theaters

        t = theaters.THEATERS.get(pid[len("preset:"):])
        return None if t is None else _preset_place(t)
    if cache is None:
        return None
    try:
        row = cache.get(_place_key(pid), ttl_s=GEOCODE_TTL_S)
        return None if not isinstance(row, Mapping) else Place.from_dict(row)
    except (KeyError, TypeError, ValueError):
        return None


def lookup(query: str, *, limit: int = 5, cache: DiskCache | None = None,
           fetch: GeoFetch | None = None, enabled: bool = True,
           budget: CallBudget | None = None) -> dict[str, Any]:
    """`{candidates: [Place.as_dict], provenance}`. Never raises.

    `enabled=False` (geodata off) answers coordinates only. `budget` is taken
    only when a network request is actually needed; when it is exhausted the
    result is empty with `provenance.rate_limited = True`.
    """
    q = clean_text(query, QUERY_MAX)
    try:
        lim = max(LIMIT_MIN, min(int(limit), LIMIT_MAX))
    except (TypeError, ValueError):
        lim = 5
    prov: dict[str, Any] = {
        "query": q, "real": False, "source": "none", "geocoder": None, "cached": False,
        "attribution": None, "fetched_at_ms": None, "reason": None,
        "rate_limited": False, "fallback_reason": None, "tried": []}
    try:
        places = _lookup(q, lim, cache, fetch, enabled, budget, prov)
    except Exception as exc:  # noqa: BLE001 — lookup never raises; the reason is data
        places = []
        prov.update(real=False, reason=f"geocoding failed: {type(exc).__name__}: {exc}")
    if places:
        prov["geocoder"] = GEOCODER_LABEL.get(prov["source"])
    return {"candidates": [p.as_dict() for p in places], "provenance": prov}


def _lookup(q: str, lim: int, cache: DiskCache | None, fetch: GeoFetch | None,
            enabled: bool, budget: CallBudget | None, prov: dict[str, Any]) -> list[Place]:
    if len(q) < QUERY_MIN:
        prov["reason"] = f"the query must be {QUERY_MIN} to {QUERY_MAX} characters"
        return []
    point = parse_coords(q)
    if point is not None:
        prov.update(real=True, source="coordinates")
        return [_coords_place(*point)]
    if not enabled:
        prov["reason"] = GEODATA_OFF_REASON
        return []
    presets = preset_places(q)
    if presets:
        prov.update(real=True, source="preset")
        return presets[:lim]
    key = f"geocode:{CACHE_VERSION}:{_norm(q)}:{lim}"
    hit = cache.get(key, ttl_s=GEOCODE_TTL_S) if cache is not None else None
    if isinstance(hit, Mapping) and hit.get("candidates"):
        prov.update(real=True, source=str(hit.get("source")), cached=True,
                    attribution=ATTRIBUTION, fetched_at_ms=hit.get("fetched_at_ms"))
        return [Place.from_dict(row) for row in hit["candidates"]]
    if budget is not None and not budget.take():
        prov.update(rate_limited=True,
                    reason="too many new place lookups; try again in a few minutes")
        return []
    reasons: list[str] = []
    for source, url in (
            ("photon", geo_http.UPSTREAM_URLS["photon"] + "?"
             + urllib.parse.urlencode({"q": q, "limit": lim})),
            ("nominatim", geo_http.UPSTREAM_URLS["nominatim"] + "?"
             + urllib.parse.urlencode({"format": "jsonv2", "q": q, "limit": lim}))):
        prov["tried"].append(source)
        parser = parse_photon if source == "photon" else parse_nominatim
        try:
            places = parser(geo_http.fetch_json(source, url, timeout_s=GEOCODE_TIMEOUT_S,
                                                fetch=fetch), limit=lim)
        except RealDataUnavailable as exc:
            reasons.append(f"{source}: {exc.reason}")
            continue
        if not places:
            reasons.append(f"{source}: no match")
            continue
        at_ms = int(time.time() * 1000)
        prov.update(real=True, source=source, attribution=ATTRIBUTION, fetched_at_ms=at_ms,
                    reason=None, fallback_reason="; ".join(reasons) or None)
        if cache is not None:
            cache.put(key, {"source": source, "fetched_at_ms": at_ms,
                            "candidates": [p.as_dict() for p in places]})
            for place in places:
                cache.put(_place_key(place.id), place.as_dict())
        return places
    prov["reason"] = ("no place found for this query" if all(
        r.endswith("no match") for r in reasons) else "geocoders unavailable") + (
        f" ({'; '.join(reasons)})")
    return []
