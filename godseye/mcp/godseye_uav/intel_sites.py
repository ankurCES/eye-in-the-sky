"""Mapped strategic sites in the intel graph (WG v2 §3.2, §4.1.8, A11).

Sites are CONTEXT (M14, D1, C3): mapped OpenStreetMap features around the
active theater, fetched by `sites.py` (A3) and held on the server as
`srv.sites`. This module turns that `SiteSet` into graph rows:

  * `site` nodes, id `sit:{theater_id}:{osm_type}/{osm_id}`, the top
    `MAX_SITE_NODES` by salience (the overlay feed serves every fetched site);
  * one `in_theater` edge per site, site -> theater. No edge ever points AT a
    site, and a site carries no control or damage attribute (C3);
  * `meta.sites` (`{total, in_graph, omitted, degraded, reason, fetched_at_ms,
    attribution, caveat}`) and the ODbL caveat when sites are drawn;
  * the site inspector body (`site_entity`), which also answers for sites the
    60-node cap left off the graph, because the map can pick any of them.

Every OSM string is untrusted (§0.2, §3.11): names and tag values arrive
cleaned from `sites.py` and are bidi-stripped again here before any client
sees them.

A site set whose bbox does not overlap the theater's is STALE (the few
milliseconds of a theater switch between `srv.theater` and `srv.sites` being
replaced): it is served as no sites with a reason, never under the new id.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from . import sites as _sites
from .intel_graph import (
    NEAR_POI_M,
    TYPE_PREFIX,
    _d,
    _ground_m,
    _node,
    _num,
    _round,
    _s,
    _trunc,
    json_size,
    strip_bidi,
)

#: Site category -> orb sector (§3.2 `SITE_GROUP`; keys as `CATEGORY_GROUP`).
SITE_GROUP: dict[str, str] = {
    "airfield": "air",
    "military_base": "ground-forces",
    "port": "naval",
    "power": "infrastructure", "bridge": "infrastructure", "dam": "infrastructure",
    "fuel": "logistics", "rail_hub": "logistics",
    "comms": "radar-ew",
    "hq_gov": "c2",
    "border_crossing": "civilian", "medical": "civilian",
    "other": "unclassified",
}
#: Graph cap (R16): the most salient sites; the rest are counted.
MAX_SITE_NODES = 60
#: Site salience is the taxonomy weight halved, so a site never outranks a
#: contact of the same weight (§3.2: never above 0.5).
SITE_SALIENCE_FACTOR = 0.5
SITE_SALIENCE_MAX = 0.5
#: Graph `attrs.tags`: at most this many whitelisted tags, each value clipped
#: (the budget rule: "trim site tags first"). The inspector gets them all.
GRAPH_MAX_TAGS = 6
GRAPH_TAG_VALUE_MAX = 60
#: Label cap (§3.2).
LABEL_MAX = 80
#: Inspector "Near": contacts whose last fix is within this of the footprint.
NEAR_SITE_M = 1000.0
#: The fixed words (Appendix B).
MAPPED_WORDS = "Mapped, not verified"
HOW_WE_KNOW = ("Mapped, not verified. Mapping may be incomplete, out of date or wrong; "
               "a missing site is not an absent one.")
STALE_REASON = "the mapped sites are still those of the previous theater; refreshing"
NO_THEATER_REASON = "active theater unknown"
NO_SOURCE_REASON = "no in-process server to read mapped sites from"
SOURCE_WORDS = "OpenStreetMap contributors, ODbL. Fetched via Overpass."


def category_of(site: Any) -> str:
    """The site's category if it is in the closed vocabulary, else `other`."""
    cat = _s(getattr(site, "category", None))
    return cat if cat in _sites.ALL_CATEGORIES else "other"


def category_word(category: str) -> str:
    """Singular copy-deck word ("Military site")."""
    return _sites.SITE_WORDS.get(category, _sites.SITE_WORDS["other"])[0]


def site_salience(site: Any) -> float:
    """`SALIENCE[cat] x 0.5`, never above 0.5."""
    weight = _num(getattr(site, "salience", None))
    if weight is None:
        weight = _sites.SALIENCE.get(category_of(site), _sites.SALIENCE["other"])
    return round(max(0.0, min(SITE_SALIENCE_MAX, weight * SITE_SALIENCE_FACTOR)), 3)


def site_label(site: Any) -> str:
    """The OSM name (bidi-stripped, <= 80 characters) or "Unnamed {category}"."""
    label = _s(getattr(site, "label", None)) or _s(getattr(site, "name", None))
    return _trunc(label or f"Unnamed {category_word(category_of(site)).lower()}", LABEL_MAX)


def site_id(site: Any, theater_id: str) -> str:
    """`sit:{theater_id}:{osm_type}/{osm_id}` (§3.2)."""
    graph_id = getattr(site, "graph_id", None)
    if callable(graph_id):
        return str(graph_id(theater_id))
    return f"{TYPE_PREFIX['site']}:{theater_id}:{site.osm_type}/{site.osm_id}"


def boxes_overlap(a: Sequence[float] | None, b: Sequence[float] | None) -> bool:
    """Two `[s, w, n, e]` boxes share any area (edges count). Unknown -> True."""
    try:
        s1, w1, n1, e1 = (float(v) for v in a)  # type: ignore[union-attr]
        s2, w2, n2, e2 = (float(v) for v in b)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return True
    return s1 <= n2 and s2 <= n1 and w1 <= e2 and w2 <= e1


def current_sites(siteset: Any, theater_bbox: Sequence[float] | None) -> tuple[Any, str | None]:
    """`(siteset, None)` when it belongs to the theater, else `(None, reason)`.

    `siteset` is `srv.sites` (a `sites.SiteSet`); anything without `.sites`
    is no source at all.
    """
    if siteset is None or not hasattr(siteset, "sites"):
        return None, NO_SOURCE_REASON
    if not boxes_overlap(getattr(siteset, "bbox", None), theater_bbox):
        return None, STALE_REASON
    return siteset, None


def fetched_at_ms(siteset: Any) -> int | None:
    value = _num(getattr(siteset, "fetched_at_ms", None)) if siteset is not None else None
    return None if value is None else int(value)


def top_sites(siteset: Any, n: int) -> list[Any]:
    """The `n` most salient sites, in `SiteSet.top` order (deterministic)."""
    top = getattr(siteset, "top", None)
    if callable(top):
        return list(top(n))
    return list(getattr(siteset, "sites", ()))[: max(0, int(n))]


def graph_tags(site: Any) -> dict[str, str]:
    """<= 6 whitelisted tags (whitelist order), values bidi-stripped and clipped.
    `name` is the label already, so it is not repeated here."""
    tags = getattr(site, "tags", None) or {}
    out: dict[str, str] = {}
    for key in _sites.TAG_WHITELIST:
        if key == "name" or key not in tags:
            continue
        if len(out) >= GRAPH_MAX_TAGS:
            break
        out[key] = _trunc(tags[key], GRAPH_TAG_VALUE_MAX)
    return out


def site_node(site: Any, theater_id: str, fetched_ms: int | None) -> dict:
    """One `site` graph node (§3.2). Status is always "ok" (the UI ignores it)."""
    cat = category_of(site)
    bounds = getattr(site, "bounds", None)
    try:
        box = [round(float(v), 6) for v in bounds] if bounds is not None else None
    except (TypeError, ValueError):
        box = None
    if box is not None and (len(box) != 4 or (box[0] == box[2] and box[1] == box[3])):
        box = None                                # a node's bounds are its point
    return _node(
        site_id(site, theater_id), "site", site_label(site),
        subtitle=f"{category_word(cat)}  {MAPPED_WORDS}",
        group=SITE_GROUP.get(cat, SITE_GROUP["other"]),
        salience=site_salience(site), status="ok", ts_ms=None,
        lat=_num(getattr(site, "lat", None)), lon=_num(getattr(site, "lon", None)),
        attrs={"category": cat, "subtype": _trunc(getattr(site, "subtype", ""), 80) or None,
               "osm": {"type": _s(getattr(site, "osm_type", None)) or None,
                       "id": getattr(site, "osm_id", None)},
               "bounds": box, "tags": graph_tags(site),
               "tags_total": int(getattr(site, "tags_total", 0) or 0),
               "protected": True if getattr(site, "protected", False) else None,
               "source": "osm", "register": "mapped", "fetched_at_ms": fetched_ms})


def theater_bbox(b: Any) -> list[float] | None:
    """The active theater's `[s, w, n, e]`: the server's block, else the AO."""
    box = _d(getattr(b.inp, "theater_state", None)).get("bbox")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        return list(box)
    ao = getattr(b, "active_ao", None)
    if not ao:
        return None
    lats = [p[0] for p in ao]
    lons = [p[1] for p in ao]
    return [min(lats), min(lons), max(lats), max(lons)]


def sites_meta(siteset: Any, *, in_graph: int, reason: str | None = None) -> dict:
    """`meta.sites` (§3.2). `siteset` None means none could be read (`reason`)."""
    total = len(getattr(siteset, "sites", ())) if siteset is not None else 0
    degraded = True if siteset is None else bool(getattr(siteset, "degraded", False))
    why = reason if siteset is None else (_s(getattr(siteset, "reason", None)) or None)
    return {"total": total, "in_graph": in_graph, "omitted": max(0, total - in_graph),
            "degraded": degraded, "reason": _trunc(why, 200) if why else None,
            "fetched_at_ms": fetched_at_ms(siteset), "attribution": _sites.ATTRIBUTION,
            "caveat": _sites.SITES_CAVEAT}


def add_site_nodes(b: Any) -> None:
    """Site nodes, `in_theater` edges, `meta.sites` and the caveats, onto the
    `_GraphBuilder` `b` (after its theater nodes exist, before its caveats).

    Sets `b.site_rows` (graph node id -> Site, the drawn ones), `b.site_index`
    (every current site, for the inspector), `b.meta_sites`, `b.sites_current`
    and appends to `b.context_caveats`.
    """
    b.site_rows, b.site_index = {}, {}
    b.sites_current = None
    tid = getattr(b, "active_id", None)
    raw = getattr(b.inp, "sites", None)
    if raw is None:
        b.meta_sites = sites_meta(None, in_graph=0, reason=NO_SOURCE_REASON)
        return
    if not tid:
        b.meta_sites = sites_meta(None, in_graph=0, reason=NO_THEATER_REASON)
        return
    current, reason = current_sites(raw, theater_bbox(b))
    if current is None:
        b.meta_sites = sites_meta(None, in_graph=0, reason=reason)
        return
    b.sites_current = current
    fetched = fetched_at_ms(current)
    thr = f"{TYPE_PREFIX['theater']}:{tid}"
    for site in top_sites(current, len(getattr(current, "sites", ()))):
        b.site_index.setdefault(site_id(site, tid), site)
    for site in top_sites(current, MAX_SITE_NODES):
        node = site_node(site, tid, fetched)
        if node["id"] in b.nodes:
            continue
        b.site_rows[node["id"]] = site
        b._add(node)
        b._edge(node["id"], thr, "in_theater")
    b.meta_sites = sites_meta(current, in_graph=len(b.site_rows))
    if b.site_rows:
        b.context_caveats.append(_sites.SITES_CAVEAT)
    if b.meta_sites["degraded"] and b.meta_sites["reason"] != _sites.GEODATA_OFF_REASON:
        b.context_caveats.append(
            f"Map data feed down ({_trunc(b.meta_sites['reason'] or 'no reason given', 120)})."
            " Sites may be missing, not absent.")


def offgraph_node(b: Any, entity_id: str) -> dict | None:
    """A node for a current site the 60-node cap left off the graph (the map
    draws up to 150 and can pick any of them), or None."""
    eid = str(entity_id or "").strip()
    index = getattr(b, "site_index", None) or {}
    site = index.get(eid)
    if site is None and eid and not eid.startswith(f"{TYPE_PREFIX['site']}:"):
        site = index.get(f"{TYPE_PREFIX['site']}:{eid}")
    if site is None:
        return None
    return site_node(site, b.active_id, fetched_at_ms(getattr(b, "sites_current", None)))


def _rect_m(lat: float, lon: float, bounds: Any) -> float | None:
    try:
        return _sites.rect_distance_m(lat, lon, tuple(float(v) for v in bounds))
    except (TypeError, ValueError):
        return None


def site_entity(b: Any, nid: str, node: dict) -> tuple[dict, dict, dict | None]:
    """Inspector body for a site: `(fields, provenance, raw)`.

    No action, control or damage field: a site is context only (C3). "Near"
    lists contacts within `NEAR_SITE_M` of the footprint and the theater's
    POIs; it is computed here and is never an edge that points at the site.
    """
    site = (getattr(b, "site_rows", None) or {}).get(nid) \
        or (getattr(b, "site_index", None) or {}).get(nid)
    if site is None:
        return {"id": nid}, {"source": SOURCE_WORDS}, None
    cat = category_of(site)
    tags = {k: strip_bidi(str(v)) for k, v in (getattr(site, "tags", None) or {}).items()
            if k in _sites.TAG_WHITELIST}
    lat, lon = _num(getattr(site, "lat", None)), _num(getattr(site, "lon", None))
    bounds = getattr(site, "bounds", None)
    contacts: list[dict] = []
    pois: list[dict] = []
    if lat is not None and lon is not None:
        for c in getattr(b, "kept", ()):
            if c.unlocated:
                continue
            d = _rect_m(c.rep.lat, c.rep.lon, bounds)
            if d is None:
                d = _ground_m(lat, lon, c.rep.lat, c.rep.lon)
            if d <= NEAR_SITE_M:
                contacts.append({"id": c.node_id, "label": _trunc(c.rep.platform, 80),
                                 "distance_m": round(d, 1)})
        for pid, (_tid, p) in (getattr(b, "poi_rows", None) or {}).items():
            plat, plon = _num(_d(p).get("lat")), _num(_d(p).get("lon"))
            if plat is None or plon is None:
                continue
            d = _rect_m(plat, plon, bounds)
            d = _ground_m(lat, lon, plat, plon) if d is None else d
            if d <= NEAR_SITE_M + NEAR_POI_M:
                pois.append({"id": pid, "label": b.nodes.get(pid, {}).get("label"),
                             "distance_m": round(d, 1)})
    contacts.sort(key=lambda r: (r["distance_m"], r["id"]))
    pois.sort(key=lambda r: (r["distance_m"], r["id"]))
    current = getattr(b, "sites_current", None)
    fields = {
        "category": cat, "category_word": category_word(cat),
        "subtype": _trunc(getattr(site, "subtype", ""), 80) or None,
        "name": _trunc(getattr(site, "name", None) or "", 160) or None,
        "name_en": tags.get("name:en") or None,
        "lat": _round(lat, 6), "lon": _round(lon, 6),
        "bounds": node.get("attrs", {}).get("bounds"),
        "osm": node.get("attrs", {}).get("osm"),
        "tags": tags, "tags_total": int(getattr(site, "tags_total", 0) or 0),
        "protected": bool(getattr(site, "protected", False)),
        "register": "mapped", "status_words": MAPPED_WORDS,
        "theater": getattr(b, "active_id", None),
        "in_graph": nid in (getattr(b, "site_rows", None) or {}),
        "near": {"radius_m": NEAR_SITE_M, "contacts": contacts[:50], "pois": pois[:20]},
        "fetched_at_ms": fetched_at_ms(current),
    }
    provenance = {
        "source": SOURCE_WORDS, "attribution": _sites.ATTRIBUTION,
        "how_we_know": HOW_WE_KNOW,
        "classification": (f"first matching taxonomy category ({fields['subtype']})"
                           if fields["subtype"] else "first matching taxonomy category"),
        "fetched_at_ms": fields["fetched_at_ms"],
        "degraded": bool(getattr(current, "degraded", False)) if current is not None else None,
        "reason": _trunc(getattr(current, "reason", None) or "", 200) or None,
        "caveat": _sites.SITES_CAVEAT,
    }
    as_dict = getattr(site, "as_dict", None)
    raw = _stripped(as_dict(max_tags=None)) if callable(as_dict) else None
    return fields, provenance, raw


def _stripped(value: Any) -> Any:
    """Every string in a nested row bidi-stripped (the raw OSM row)."""
    if isinstance(value, str):
        return strip_bidi(value)
    if isinstance(value, dict):
        return {k: _stripped(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stripped(v) for v in value]
    return value


#: Past the graph budget the sites give way first (§3.2 "trim site tags
#: first"): their tags go, then the least salient site nodes, down to this.
MIN_SITE_NODES = 12


def fit_sites_to_budget(b: Any, max_bytes: int) -> None:
    """Hold the built graph `b.graph` under `max_bytes` by trimming SITES
    first: every site node's `tags` go (the inspector still has them all),
    then the least salient site nodes, down to `MIN_SITE_NODES`. Both are
    counted in `meta.sites` (`tags_trimmed`, `trimmed_for_budget`,
    `in_graph`, `omitted`) and a caveat; the overlay still serves every
    site. Nothing else is cut, so a picture over budget for other reasons
    keeps its contacts."""
    rows = list(getattr(b, "site_rows", None) or {})
    if not rows:
        return
    g = b.graph
    size = json_size(g)
    if size <= max_bytes:
        return
    for nid in rows:
        b.nodes[nid]["attrs"].pop("tags", None)
    b.meta_sites["tags_trimmed"] = True
    size = json_size(g)
    if size <= max_bytes:
        return
    # dropping adds a caveat and two meta counters: leave room for them
    target = max_bytes - json_size(_trimmed_caveat(999, max_bytes)) - 48
    drop: list[str] = []
    for nid in reversed(rows):                  # least salient first
        if size <= target or len(rows) - len(drop) <= MIN_SITE_NODES:
            break
        drop.append(nid)
        size -= json_size(b.nodes[nid]) + 1
        size -= sum(json_size(e) + 1 for e in b.edges if e["a"] == nid)
    if not drop:
        return
    gone = set(drop)
    for nid in drop:
        b.nodes.pop(nid, None)
        b.site_rows.pop(nid, None)
    g["nodes"] = [n for n in g["nodes"] if n["id"] not in gone]
    b.edges[:] = [e for e in b.edges if e["a"] not in gone]
    b._edge_keys = {k for k in b._edge_keys if k[0] not in gone}
    counts = g["meta"]["counts"]
    counts["site"] = counts.get("site", 0) - len(drop)
    if counts["site"] <= 0:
        counts.pop("site", None)
    ms = b.meta_sites
    ms["in_graph"] = len(b.site_rows)
    ms["omitted"] = max(0, ms["total"] - ms["in_graph"])
    ms["trimmed_for_budget"] = len(drop)
    g["meta"]["caveats"].append(_trimmed_caveat(len(drop), max_bytes))


def _trimmed_caveat(n: int, max_bytes: int) -> str:
    return (f"{n} mapped site(s) were left off this graph to keep it under "
            f"{max_bytes // 1000} KB; the map shows every fetched site.")
