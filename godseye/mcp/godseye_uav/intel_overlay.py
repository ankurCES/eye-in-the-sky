"""The map's context overlay feed: `GET /intel/overlay` (WG v2 §3.3, R2, A11).

A GeoJSON FeatureCollection of everything the map draws as context around
the active theater. Phase A serves `site` features: EVERY fetched site
(<= `MAX_OVERLAY_SITES`), most salient first, the top `LABELLED_SITES`
flagged `labelled`. Phase B (B10) adds the simulated wargame's `force`,
`force_envelope`, `vector` and `engagement` features from the engine
(`intel_scenario.overlay_features`: red only in the Umpire view `truth=1` or
a revealed session, capped per §3.3, every one `simulated: true`); its
`revision` is part of `rev`. A wargame read that fails leaves the sites
standing and says so in `wargame.error`.

The map polls every 3 s with the last `rev` it drew:

    rev = f"{epoch}:{sites.fetched_at_ms or 0}:{engine.revision or 0}:{int(truth)}"

and an unchanged `rev` answers `{"rev": ..., "unchanged": true}` without a
body. `rev` changes on a theater switch (epoch), a sites refresh
(fetched_at_ms) and any wargame change (revision), so nothing else needs to
be compared. `meta.overlay_rev` on `/intel/graph` is the same string.

Pure reads of the in-process server (duck-typed `srv`; this module never
imports `server.py`). Every label is untrusted OSM text: bidi-stripped and
capped here, rendered as text by the client (§3.11).
"""
from __future__ import annotations

import math
from typing import Any

from . import intel_scenario, intel_sites
from . import sites as _sites
from .intel_graph import _num, json_size

#: Every fetched site is served (R16); the taxonomy caps sum to this.
MAX_OVERLAY_SITES = 300
#: The map labels only these (the most salient).
LABELLED_SITES = 40
#: A maximum-load body stays under this (§3.3).
OVERLAY_MAX_BYTES = 400_000
#: GeoJSON coordinates are rounded to this many decimals (~0.1 m).
COORD_DECIMALS = 6
#: A `rev` longer than this is not one we issued; it never matches.
REV_MAX_LEN = 128
TRUTH_VALUES = {"0": False, "1": True, "false": False, "true": True}


def overlay_rev(epoch: Any, sites_fetched_at_ms: Any, engine_revision: Any,
                truth: bool) -> str:
    """The §3.3 revision string. Missing or broken parts read as 0."""
    return f"{_int0(epoch)}:{_int0(sites_fetched_at_ms)}:{_int0(engine_revision)}:{int(bool(truth))}"


def parse_truth(value: Any) -> bool | None:
    """`truth` query value -> bool; None when it is not one of 0/1/false/true."""
    if isinstance(value, bool):
        return value
    return TRUTH_VALUES.get(str(value if value is not None else "0").strip().lower())


def engine_revision(srv: Any) -> int:
    """The wargame engine's revision (Phase B), 0 when there is none."""
    return _int0(getattr(getattr(srv, "wargame", None), "revision", 0))


def server_sites(srv: Any) -> tuple[Any, str | None, Any]:
    """`(siteset or None, reason, theater)` read once from the server.

    The theater is read FIRST: a switch replaces `srv.theater` before
    `srv.sites`, and `intel_sites.current_sites` refuses a set whose bbox
    does not overlap the theater's, so a read in between serves no sites
    rather than old ones under the new id.
    """
    theater = getattr(srv, "theater", None)
    raw = getattr(srv, "sites", None)
    try:
        bbox = theater.bbox() if theater is not None else None
    except Exception:  # noqa: BLE001 - a theater without a box is not fatal
        bbox = None
    current, reason = intel_sites.current_sites(raw, bbox)
    return current, reason, theater


def site_feature(site: Any, theater_id: str, *, labelled: bool) -> dict | None:
    """One `site` Feature (§3.3), or None for a site with no usable point."""
    lat, lon = _num(getattr(site, "lat", None)), _num(getattr(site, "lon", None))
    if lat is None or lon is None:
        return None
    sid = intel_sites.site_id(site, theater_id)
    return {"type": "Feature", "id": sid,
            "geometry": {"type": "Point",
                         "coordinates": [round(lon, COORD_DECIMALS), round(lat, COORD_DECIMALS)]},
            "properties": {"kind": "site", "id": sid, "label": intel_sites.site_label(site),
                           "category": intel_sites.category_of(site),
                           "protected": bool(getattr(site, "protected", False)),
                           "register": "mapped", "salience": intel_sites.site_salience(site),
                           "labelled": bool(labelled), "simulated": False, "truth": False}}


def _int0(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0
    return int(out) if math.isfinite(out) else 0


def current_rev(srv: Any, *, truth: bool = False) -> str:
    """The rev `build_overlay` would answer with right now."""
    current, _reason, _theater = server_sites(srv)
    return overlay_rev(getattr(srv, "theater_epoch", 0),
                       intel_sites.fetched_at_ms(current) if current is not None else 0,
                       engine_revision(srv), truth)


def build_overlay(srv: Any, *, truth: bool = False, rev: str | None = None) -> dict:
    """The overlay body for `srv` (§3.3), or `{rev, unchanged: true}` when
    `rev` is the current revision. `srv` None (no in-process server) is an
    empty collection that says so. Never raises on a malformed site."""
    truth = bool(truth)
    if srv is None:
        body = _collection(overlay_rev(0, 0, 0, truth), None, None, [], {}, {})
        body["sites"] = _sites_block(None, 0, 0, intel_sites.NO_SOURCE_REASON)
        return body
    current, reason, theater = server_sites(srv)
    epoch = getattr(srv, "theater_epoch", 0)
    fetched = intel_sites.fetched_at_ms(current) if current is not None else None
    now_rev = overlay_rev(epoch, fetched or 0, engine_revision(srv), truth)
    if isinstance(rev, str) and len(rev) <= REV_MAX_LEN and rev == now_rev:
        return {"rev": now_rev, "unchanged": True}
    tid = str(getattr(theater, "id", "") or "") or None
    features: list[dict] = []
    omitted: dict[str, int] = {}
    total = 0
    if current is not None and tid:
        ordered = intel_sites.top_sites(current, len(getattr(current, "sites", ())))
        total = len(ordered)
        seen: set[str] = set()
        for rank, site in enumerate(ordered[:MAX_OVERLAY_SITES]):
            try:
                feat = site_feature(site, tid, labelled=rank < LABELLED_SITES)
            except (AttributeError, TypeError, ValueError):
                feat = None
            if feat is None or feat["id"] in seen:
                continue
            seen.add(feat["id"])
            features.append(feat)
        if total > len(features):
            omitted["site"] = total - len(features)
    counts = {"site": len(features)} if features else {}
    served_sites = len(features)
    error = None
    try:
        wg_features, wg_counts, wg_omitted = intel_scenario.overlay_features(srv, truth=truth)
    except Exception as exc:  # noqa: BLE001 - the sites still serve; the body says so
        wg_features, wg_counts, wg_omitted = [], {}, {}
        error = f"{intel_scenario.ENGINE_ERROR} ({type(exc).__name__})"
    features.extend(wg_features)
    counts.update(wg_counts)
    omitted.update(wg_omitted)
    body = _collection(now_rev, tid, _int_or_none(epoch), features, counts, omitted)
    body["sites"] = _sites_block(current, total, served_sites, reason)
    if error:
        body["wargame"] = {"error": error}
    return body


def _sites_block(current: Any, total: int, served: int, reason: str | None) -> dict:
    """`{total, served, degraded, reason, fetched_at_ms}`: what the dock and
    the "map data feed down" line need, next to the features."""
    meta = intel_sites.sites_meta(current, in_graph=served, reason=reason)
    return {"total": total, "served": served, "degraded": meta["degraded"],
            "reason": meta["reason"], "fetched_at_ms": meta["fetched_at_ms"]}


def _collection(rev: str, theater_id: str | None, epoch: int | None, features: list[dict],
                counts: dict, omitted: dict) -> dict:
    return {"type": "FeatureCollection", "rev": rev,
            "theater": {"id": theater_id, "epoch": epoch},
            "attribution": [_sites.ATTRIBUTION] if any(
                f["properties"]["kind"] == "site" for f in features) else [],
            "counts": counts, "omitted": omitted, "features": features}


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return int(out) if math.isfinite(out) else None


def body_size(body: dict) -> int:
    """Compact JSON bytes (what the route puts on the wire)."""
    return json_size(body)
