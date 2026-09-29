"""Simulated wargame rows in the intel graph and on the map (M14a, PLAN.md
§4.5a; WG v2 §3.2, §3.3, §5.2.12 B10).

The wargame engine (`wargame.WargameEngine`, B3) owns every scenario row: it
builds the `force`, `engagement` and `vector` nodes, their edges and
`meta.wargame` (`graph_rows(truth=)`), and the map's `force`,
`force_envelope`, `vector` and `engagement` features
(`overlay_features(truth=)`). This module places them in the ONE intel
picture and holds the picture's own rules on top of the engine's:

  * VIEWS. `truth` is the console's Umpire view (`/intel/graph?truth=1`). In
    the Blue view (the default, and ALWAYS the analyst's) red forces and red
    axes are absent unless the session reveals red, the truth-only edges
    (`axis`, `threatens`, `correlates`) and `correlated[]` never appear, a red
    attacker is masked and a hidden outcome stays hidden. The engine filters
    first; this module filters again, so no view rests on one check.
  * CAPS (§3.2): forces 60, the newest 24 engagements, the newest 12
    vectors; whatever is cut is counted in `meta.wargame.omitted`. Past the
    150 KB budget (after the sites) the rows give way in a fixed, counted
    order (`fit_to_budget`); forces and waiting engagements never do, and the
    inspector reads the rows as placed.
  * NOTHING REAL (D1, C3). Only engine rows with provenance `scenario` enter,
    and no wargame edge touches a site, a POI or a theater. Scenario contacts
    carry the generic `wargame_tables.label_for_ob` label, never an
    order-of-battle system name; they stay out of the OB `equipment` and
    SALUTE `unit` roll-ups, and their inspector body withholds OB text.
  * `simulated: true` on every row, and the session caveat in `meta.caveats`.
  * The after-action review (`report_type == "AAR"`) is a report node with
    `attrs.format = "AAR"`; its entity carries `fields.markdown`.

Pure over its inputs apart from `gather` / `overlay_features`, which read the
in-process server (`srv` duck-typed; this module never imports `server.py`).
"""
from __future__ import annotations

import copy
import math
from typing import Any

from .intel_graph import (
    TYPE_PREFIX,
    _clamp01,
    _copy_mapping,
    _d,
    _int,
    _l,
    _num,
    _round,
    _s,
    _trunc,
    json_size,
    strip_bidi,
)
from .targets import OB_LIBRARY
from .wargame_aar import AAR_TITLE, REPORT_TYPE
from .wargame_tables import NOTIONAL_NOTE, label_for_ob

WARGAME_TYPES = ("force", "engagement", "vector")
#: Graph caps (§3.2): forces as the engine orders them, the newest rest.
MAX_NODES = {"force": 60, "engagement": 24, "vector": 12}
#: Wargame edge kinds (§3.2); the truth-only ones never reach a Blue view.
EDGE_KINDS = frozenset({"attacks", "launched_by", "along", "axis", "ingress", "threatens",
                        "correlates"})
TRUTH_EDGES = frozenset({"axis", "threatens", "correlates"})
#: Real places: no wargame edge may touch one (C3).
REAL_PREFIXES = (f"{TYPE_PREFIX['site']}:", f"{TYPE_PREFIX['poi']}:",
                 f"{TYPE_PREFIX['theater']}:")
#: Overlay caps (§3.3): the key is the kind, or `vector.kind_detail`.
OVERLAY_CAPS = {"force": 60, "force_envelope": 80, "axis": 20, "corridor": 6,
                "engagement": 24}
OVERLAY_KINDS = ("force", "force_envelope", "vector", "engagement")
GEOMETRIES = ("Point", "LineString", "Polygon")
COORD_DECIMALS = 6
STATUSES = ("ok", "warn", "critical", "stale", "unknown")
#: `wargame.SESSION_CAVEAT` (a test holds them equal): `meta.caveats` in a session.
SESSION_CAVEAT = ("Scenario forces and engagements are simulated; outcomes are notional "
                  "adjudications.")
#: A red attacker in the Blue view (`wargame.RED_AD_HIDDEN` / `RED_GROUND_HIDDEN`).
MASKED_ATTACKER = {"red_shot": "Red air defence (not identified)",
                   "red_ground": "Red ground forces (not identified)"}
#: `targets.SCENARIO_CONTACT_NOTE`: the scenario track subtitle suffix (§3.2).
SCENARIO_NOTE = "Scenario contact (simulated)"
#: `meta.wargame` keys while a session runs (§3.2), in that order.
META_KEYS = ("active", "session_id", "started_at_ms", "seed", "engine", "time_scale",
             "red_engages", "reveal_red", "truth_view", "revision", "pending", "counts",
             "caveats", "step_ms", "errors", "simulated")
LAST_KEYS = ("session_id", "aar_id", "ended_at_ms")
#: Keys that carry order-of-battle reference data (SALUTE equipment, the
#: bridge's threat rings, the live threat envelope), dropped at any depth for a
#: scenario contact: its numbers and cues describe a real system class.
OB_REFERENCE_KEYS = frozenset({"capabilities", "signature_cues", "weapon_range_m",
                               "weapon_min_range_m", "weapon_ceiling_m",
                               "acquisition_range_m", "threat_rings_m", "envelope_m"})
ENGINE_SOURCE = ("simulated wargame engine (M14a): placed and adjudicated by the wargame, "
                 "not observed by a sensor")
OB_WITHHELD = ("withheld for a scenario contact (M14a): generic label only, no "
               "order-of-battle system text")
ENGINE_ERROR = "the simulated wargame engine could not be read"


# ---------------------------------------------------------------------------
# reading the server
# ---------------------------------------------------------------------------

def engine_of(srv: Any) -> Any:
    """`srv.wargame` (Phase B), or None."""
    return getattr(srv, "wargame", None) if srv is not None else None


def _int0(value: Any) -> int:
    out = _int(value)
    return 0 if out is None else out


def gather(srv: Any, *, truth: bool = False) -> dict | None:
    """The engine's graph rows for one view and what the graph reads beside
    them: `{nodes, edges, meta, revision, vehicles_lost}`. None when the
    server has no engine; `{error}` when it could not be read (never raises:
    a wargame read must not take the ISR picture down with it)."""
    eng = engine_of(srv)
    rows_fn = getattr(eng, "graph_rows", None)
    if not callable(rows_fn):
        return None
    try:
        rows = _d(rows_fn(truth=bool(truth)))
        meta = _d(rows.get("meta"))
        revision = _int(_d(meta.get("wargame")).get("revision"))
        lost = _copy_mapping(getattr(srv, "vehicles_lost", None))
    except Exception as exc:  # noqa: BLE001 - said in meta.wargame and a caveat
        return {"error": f"{ENGINE_ERROR} ({type(exc).__name__})"}
    return {"nodes": _l(rows.get("nodes")), "edges": _l(rows.get("edges")), "meta": meta,
            "revision": _int0(getattr(eng, "revision", 0)) if revision is None else revision,
            "vehicles_lost": {str(k): dict(v) for k, v in lost.items() if isinstance(v, dict)}}


def mark_scenario_rows(rows: list[dict], srv: Any) -> list[dict]:
    """Track rows whose sim object the running session owns, marked
    `scenario: true` (as a copy) even when the bridge polled the row before
    the ingest hook flagged its track. Other rows are returned untouched."""
    owns = getattr(engine_of(srv), "owns_name", None)
    if not callable(owns):
        return rows
    out = []
    for r in rows:
        name = _s(r.get("equipment_name")) or _s(_d(r.get("equipment")).get("detected_as"))
        if r.get("scenario") is not True and name:
            try:
                owned = owns(name) is True
            except Exception:  # noqa: BLE001 - unknown is "not owned"
                owned = False
            if owned:
                r = {**r, "scenario": True, "simulated": True}
        out.append(r)
    return out


def scenario_label(ob_class: Any) -> str:
    """The generic label a scenario contact shows (§3.2), never an OB name."""
    return label_for_ob(_s(ob_class))


# ---------------------------------------------------------------------------
# graph rows
# ---------------------------------------------------------------------------

def clean_value(value: Any) -> Any:
    """Every string in a nested row bidi-stripped; non-finite floats -> None.
    Nulls are kept: a wargame row's `null` is data (a hidden attacker)."""
    if isinstance(value, str):
        return strip_bidi(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): clean_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [clean_value(v) for v in value]
    return value


def _ref(value: Any, alias: dict) -> str:
    """A graph id an engine row names, through the builder's track aliases (a
    scenario track folded into another contact resolves to that contact)."""
    rid = _s(value)
    return alias.get(rid, rid) if rid.startswith("trk:") else rid


def clean_node(raw: Any, *, truth: bool, alias: dict | None = None) -> dict | None:
    """One engine row as a graph node (the `intel_graph._node` shape), or None
    when it is not a scenario row this module may place."""
    if not isinstance(raw, dict):
        return None
    ntype, nid = raw.get("type"), _s(raw.get("id"))
    if ntype not in WARGAME_TYPES or not nid.startswith(f"{TYPE_PREFIX[ntype]}:"):
        return None
    attrs = clean_value(_d(raw.get("attrs")))
    if ntype == "force" and attrs.get("provenance") != "scenario":
        return None                                  # only scenario units (D1)
    alias = alias or {}
    attrs["simulated"] = True
    if truth and ntype == "force":
        attrs["correlated"] = [_ref(t, alias) for t in _l(attrs.get("correlated"))]
    else:
        attrs.pop("correlated", None)               # truth only (§3.2)
    if ntype == "engagement":
        attrs["target"] = _ref(attrs.get("target"), alias) or None
    status = raw.get("status") if raw.get("status") in STATUSES else "unknown"
    return {"id": nid, "type": ntype, "label": _trunc(raw.get("label"), 80) or nid,
            "subtitle": _trunc(raw.get("subtitle"), 140),
            "group": _s(raw.get("group")) or "unclassified",
            "salience": round(_clamp01(_num(raw.get("salience")) or 0.0), 3),
            "status": status, "ts_ms": _int(raw.get("ts_ms")),
            "lat": _round(_num(raw.get("lat")), 6), "lon": _round(_num(raw.get("lon")), 6),
            "attrs": attrs}


def fog(node: dict, *, show_red: bool) -> dict | None:
    """The Blue-view filter over one cleaned node (§3.2 Fog), or None when the
    view may not show it. `show_red` is the Umpire view or a revealed session."""
    a = node["attrs"]
    if node["type"] == "engagement" and not show_red and a.get("kind") == "blue_strike" \
            and _int0(_d(a.get("bda")).get("looks")) < 1:
        a["outcome_hidden"] = True                  # unknown until a re-look (§3.2)
    if node["type"] == "engagement" and a.get("outcome_hidden") is True:
        a["outcome"] = None
    if show_red:
        return node
    if node["type"] == "force":
        return node if a.get("side") == "blue" else None
    if node["type"] == "vector":
        return node if a.get("kind") == "corridor" and a.get("side") != "red" else None
    if a.get("kind") in MASKED_ATTACKER:
        a.update(attacker=None, attacker_label=MASKED_ATTACKER[a["kind"]], p_notional=None,
                 inputs=[])
    return node


def select(rows: list, *, truth: bool, show_red: bool,
           alias: dict | None = None) -> tuple[list[dict], dict[str, int]]:
    """The nodes one view shows, capped (§3.2), and what the caps cut."""
    by_type: dict[str, list[dict]] = {t: [] for t in WARGAME_TYPES}
    seen: set[str] = set()
    for raw in rows:
        node = clean_node(raw, truth=truth, alias=alias)
        node = fog(node, show_red=show_red) if node is not None else None
        if node is None or node["id"] in seen:
            continue
        seen.add(node["id"])
        by_type[node["type"]].append(node)
    out: list[dict] = []
    omitted: dict[str, int] = {}
    for ntype in WARGAME_TYPES:
        rows_t = by_type[ntype]
        cap = MAX_NODES[ntype]
        if ntype != "force":                        # the newest, oldest first
            rows_t = sorted(rows_t, key=lambda n: (n["ts_ms"] or 0, n["id"]))[-cap:] \
                if len(rows_t) > cap else rows_t
        else:
            rows_t = rows_t[:cap]
        if len(by_type[ntype]) > len(rows_t):
            omitted[ntype] = len(by_type[ntype]) - len(rows_t)
        out.extend(rows_t)
    return out, omitted


def add_nodes(b: Any) -> None:
    """Scenario nodes and edges, vehicle loss attrs, `meta.wargame` and the
    session caveats onto the `_GraphBuilder` `b`. Runs after `b`'s own nodes
    and edges so every endpoint a wargame edge names can be checked; sets
    `b.wargame_rows` (node id -> node), `b.wargame_full` (the same rows as
    placed, before any budget trim: the inspector reads these) and
    `b.meta_wargame`."""
    wg = _d(getattr(b.inp, "wargame", None))
    truth = getattr(b.inp, "truth", False) is True
    b.wargame_rows, b.wargame_full = {}, {}
    meta = _d(_d(wg.get("meta")).get("wargame"))
    if meta.get("active") is not True:
        b.meta_wargame = inactive_meta(meta, error=_s(wg.get("error")) or None)
        if wg.get("error"):
            err = _s(wg["error"])
            b.context_caveats.append(_trunc(f"{err[:1].upper()}{err[1:]}; wargame rows are "
                                            "missing, not absent.", 300))
        _vehicle_losses(b, _d(wg.get("vehicles_lost")), show_red=truth)
        return
    show_red = truth or meta.get("reveal_red") is True
    nodes, omitted = select(_l(wg.get("nodes")), truth=truth, show_red=show_red,
                            alias=b.alias)
    for node in nodes:
        if node["id"] not in b.nodes:
            b._add(node)
            b.wargame_rows[node["id"]] = node
            b.wargame_full[node["id"]] = copy.deepcopy(node)
    # `ingress` joins a shown corridor's own ends (a drone and a contact)
    ingress = {(_ref(n["attrs"].get("from"), b.alias), _ref(n["attrs"].get("to"), b.alias))
               for n in nodes if n["type"] == "vector" and n["attrs"].get("kind") == "corridor"}
    for e in _l(wg.get("edges")):
        kind = _d(e).get("kind")
        if kind not in EDGE_KINDS or (kind in TRUTH_EDGES and not truth):
            continue
        a, z = _ref(e.get("a"), b.alias), _ref(e.get("b"), b.alias)
        if a.startswith(REAL_PREFIXES) or z.startswith(REAL_PREFIXES):
            continue                                # never an edge at a real place
        if kind == "ingress":
            ok = (a, z) in ingress
        else:
            ok = a in b.wargame_rows or z in b.wargame_rows
        if ok:
            b._edge(a, z, kind)
    _vehicle_losses(b, _d(wg.get("vehicles_lost")), show_red=show_red)
    b.meta_wargame = active_meta(meta, show_red=show_red, omitted=omitted)
    for c in [SESSION_CAVEAT, *_l(_d(wg.get("meta")).get("caveats"))]:
        if isinstance(c, str) and c and _trunc(c, 300) not in b.context_caveats:
            b.context_caveats.append(_trunc(c, 300))


def _vehicle_losses(b: Any, lost: dict, *, show_red: bool) -> None:
    """Vehicle attrs for a drone the wargame downed (§3.2): `wargame_state`,
    `wargame_lost_at_ms` and `wargame_lost_by`, the downing unit's `frc:` id
    in a view that shows red, else null (the Blue view never names it)."""
    if not lost:
        return
    shooter: dict[str, tuple[int, str]] = {}
    for n in b.wargame_rows.values():
        a = n["attrs"]
        if n["type"] == "engagement" and a.get("kind") == "red_shot" \
                and a.get("consequence") == "own_loss" and _s(a.get("attacker")):
            at = _int0(a.get("adjudicated_at_ms"))
            if at >= shooter.get(_s(a.get("target")), (-1, ""))[0]:
                shooter[_s(a.get("target"))] = (at, a["attacker"])
    by_label = {n["label"]: nid for nid, n in b.wargame_rows.items() if n["type"] == "force"}
    for name, rec in sorted(lost.items()):
        node = b.nodes.get(f"{TYPE_PREFIX['vehicle']}:{name}")
        if node is None:
            continue
        by = None
        if show_red:
            by = shooter.get(node["id"], (0, None))[1] or by_label.get(_s(rec.get("by")))
        node["attrs"].update(wargame_state="lost", wargame_lost_by=by,
                             wargame_lost_at_ms=_int(rec.get("at_ms")))


def inactive_meta(meta: dict, *, error: str | None = None) -> dict:
    """`meta.wargame` with no session: `{active: false, last}` (§3.2)."""
    last = meta.get("last")
    out = {"active": False,
           "last": {k: last.get(k) for k in LAST_KEYS} if isinstance(last, dict) else None}
    if error:
        out["error"] = _trunc(error, 200)
    return out


def active_meta(meta: dict, *, show_red: bool, omitted: dict | None = None) -> dict:
    """`meta.wargame` while a session runs (§3.2). The Blue view's red counts
    carry only `seen`."""
    out = {k: clean_value(meta.get(k)) for k in META_KEYS if k in meta}
    out.update(active=True, truth_view=bool(show_red), simulated=True)
    counts = _d(out.get("counts"))
    if not show_red:
        counts = {**counts, "red": {"seen": _int0(_d(counts.get("red")).get("seen"))}}
    out["counts"] = counts
    out["pending"] = [p for p in _l(out.get("pending"))
                      if isinstance(p, str) and p.startswith(f"{TYPE_PREFIX['engagement']}:")]
    out["caveats"] = [_trunc(c, 300) for c in _l(out.get("caveats")) if isinstance(c, str)]
    if omitted:
        out["omitted"] = dict(omitted)
    return out


#: Past the graph budget, once the sites gave way (§3.2), the wargame's rows
#: give way in this order, each step counted in `meta.wargame.trimmed_for_budget`:
#: long lists shrink to `TRIM_LIST_TO` entries (`{key}_total` beside them); the
#: truth-only `threatens` edges past `THREATENS_KEEP` (those at a drone kept
#: first); null attrs other than `KEEP_NULL` (a missing field hides its row in
#: the UI); the wargame subtitles (the console composes them from the attrs,
#: B13 `wargameText`); then the oldest SETTLED engagements and vectors, down to
#: `MIN_ENGAGEMENTS` / `MIN_VECTORS`, with a caveat. Forces and waiting
#: engagements are never dropped.
TRIM_LIST_TO = 3
TRIM_KEYS = ("legs", "inputs", "caveats", "correlated")
THREATENS_KEEP = 24
KEEP_NULL = frozenset({"attacker", "outcome", "to", "p_notional", "vector", "objective"})
MIN_ENGAGEMENTS, MIN_VECTORS = 4, 2
PENDING_PHASES = ("proposed", "authorized")


def _trim_lists(rows: dict) -> int:
    cut = 0
    for node in rows.values():
        a = node["attrs"]
        for key in TRIM_KEYS:
            lst = a.get(key)
            if isinstance(lst, list) and len(lst) > TRIM_LIST_TO:
                a[f"{key}_total"] = len(lst)
                a[key] = lst[:TRIM_LIST_TO]
                cut += 1
    return cut


def _trim_threatens(b: Any) -> int:
    thr = [e for e in b.edges if e["kind"] == "threatens"]
    if len(thr) <= THREATENS_KEEP:
        return 0
    keep = sorted(thr, key=lambda e: (not e["b"].startswith("veh:"), e["a"], e["b"]))
    gone = {id(e) for e in keep[THREATENS_KEEP:]}
    b.edges[:] = [e for e in b.edges if id(e) not in gone]
    b._edge_keys = {(e["a"], e["b"], e["kind"]) for e in b.edges}
    return len(gone)


def _trim_nulls(rows: dict) -> int:
    cut = 0
    for node in rows.values():
        a = node["attrs"]
        for key in [k for k, v in a.items() if v is None and k not in KEEP_NULL]:
            del a[key]
            cut += 1
    return cut


def _trim_subtitles(rows: dict) -> int:
    cut = 0
    for node in rows.values():
        if node["subtitle"]:
            node["subtitle"] = ""
            cut += 1
    return cut


def _drop_settled(b: Any, size: int, max_bytes: int) -> dict[str, int]:
    """Drop the oldest settled engagements, then vectors, until the graph fits."""
    pending = set(_l(_d(getattr(b, "meta_wargame", None)).get("pending")))
    rows = b.wargame_rows

    def settled(ntype: str) -> list[dict]:
        out = [n for n in rows.values() if n["type"] == ntype and n["id"] not in pending
               and n["attrs"].get("phase") not in PENDING_PHASES]
        return sorted(out, key=lambda n: (n["ts_ms"] or 0, n["id"]))

    target = max_bytes - 400                      # room for the caveat and the counts
    gone: dict[str, str] = {}
    for ntype, floor in (("engagement", MIN_ENGAGEMENTS), ("vector", MIN_VECTORS)):
        cands = settled(ntype)
        total = sum(1 for n in rows.values() if n["type"] == ntype)
        for n in cands:
            if size <= target or total <= floor:
                break
            gone[n["id"]] = ntype
            total -= 1
            size -= json_size(n) + 1
            size -= sum(json_size(e) + 1 for e in b.edges if n["id"] in (e["a"], e["b"]))
    if not gone:
        return {}
    for nid in gone:
        b.nodes.pop(nid, None)
        rows.pop(nid, None)
    g = b.graph
    g["nodes"] = [n for n in g["nodes"] if n["id"] not in gone]
    b.edges[:] = [e for e in b.edges if e["a"] not in gone and e["b"] not in gone]
    b._edge_keys = {(e["a"], e["b"], e["kind"]) for e in b.edges}
    dropped: dict[str, int] = {}
    for ntype in gone.values():
        dropped[ntype] = dropped.get(ntype, 0) + 1
    counts = g["meta"]["counts"]
    for ntype, n in dropped.items():
        counts[ntype] = counts.get(ntype, 0) - n
        if counts[ntype] <= 0:
            counts.pop(ntype, None)
    g["meta"]["caveats"].append(
        f"{sum(dropped.values())} settled simulated engagement(s) or vector(s) were left off "
        f"this graph to keep it under {max_bytes // 1000} KB; wg_session_status lists them.")
    return dropped


def fit_to_budget(b: Any, max_bytes: int) -> None:
    """Hold `b.graph` under `max_bytes` once the sites have given way, trimming
    the wargame rows in the order above and saying what was cut in
    `meta.wargame.trimmed_for_budget`. Never silent; never a force or a
    waiting engagement."""
    rows = getattr(b, "wargame_rows", None) or {}
    if not rows or json_size(b.graph) <= max_bytes:
        return
    done: dict[str, int] = {}
    for key, step in (("lists", lambda: _trim_lists(rows)),
                      ("threatens", lambda: _trim_threatens(b)),
                      ("nulls", lambda: _trim_nulls(rows)),
                      ("subtitles", lambda: _trim_subtitles(rows))):
        n = step()
        if n:
            done[key] = n
        if json_size(b.graph) <= max_bytes:
            break
    size = json_size(b.graph)
    if size > max_bytes:
        done.update(_drop_settled(b, size, max_bytes))
    if done and isinstance(getattr(b, "meta_wargame", None), dict):
        b.meta_wargame["trimmed_for_budget"] = done


# ---------------------------------------------------------------------------
# inspector bodies
# ---------------------------------------------------------------------------

def wargame_entity(b: Any, nid: str, node: dict) -> tuple[dict, dict, None]:
    """Inspector body for a force, an engagement or a vector: `(fields,
    provenance, raw)`. The node's own rows in its view; no "near" rows and no
    place names (§5.3.4), and no raw engine record. Read from the row as
    placed, so a budget trim of the graph never thins the inspector."""
    node = (getattr(b, "wargame_full", None) or {}).get(nid, node)
    fields = {"id": nid, "type": node["type"], "label": node["label"],
              "subtitle": node["subtitle"], "lat": node["lat"], "lon": node["lon"],
              **node["attrs"]}
    meta = _d(getattr(b, "meta_wargame", None))
    provenance = {"source": ENGINE_SOURCE, "register": "scenario",
                  "session_id": meta.get("session_id"), "engine": meta.get("engine"),
                  "view": ("umpire view: red units where the scenario put them"
                           if getattr(b.inp, "truth", False) else
                           "blue view: red units only as sensor contacts, unless the "
                           "session reveals them"),
                  "numbers": NOTIONAL_NOTE, "simulated": True}
    return fields, provenance, None


def offgraph_node(b: Any, entity_id: str) -> dict | None:
    """A wargame row the byte budget left off the graph (still in this view),
    by graph id or bare id, or None."""
    full = getattr(b, "wargame_full", None) or {}
    eid = str(entity_id or "").strip()
    for cand in (eid, *(f"{TYPE_PREFIX[t]}:{eid}" for t in WARGAME_TYPES)):
        if cand in full and cand not in b.nodes:
            return full[cand]
    return None


def _generic(value: Any, names: tuple[str, ...], label: str) -> Any:
    """`value` with every OB system name replaced by the generic `label` and
    the OB reference keys dropped (a scenario contact's SALUTE, threat)."""
    if isinstance(value, str):
        for name in names:
            value = value.replace(name, label)
        return strip_bidi(value)
    if isinstance(value, dict):
        return {k: _generic(v, names, label) for k, v in value.items()
                if k not in OB_REFERENCE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_generic(v, names, label) for v in value]
    return value


def scrub_track_entity(fields: dict, provenance: dict, ob_class: Any
                       ) -> tuple[dict, dict, None]:
    """A scenario contact's inspector body (D1): the generic label in place of
    the OB system name everywhere, no OB reference data (capabilities, ranges,
    cues) and no raw row. The position, times, confidence and sightings stay:
    they are what the sensor reported."""
    label = scenario_label(ob_class)
    ob = OB_LIBRARY.get(_s(ob_class))
    names = tuple(n for n in ((ob.name,) if ob else ()) if n)
    out = _generic(fields, names, label)
    out.update(platform=label, scenario=True, simulated=True)
    prov = _generic(provenance, names, label)
    prov.update(scenario=f"{SCENARIO_NOTE}: a wargame scenario unit seen by a sensor",
                order_of_battle=OB_WITHHELD)
    return out, prov, None


# ---------------------------------------------------------------------------
# the after-action review
# ---------------------------------------------------------------------------

def is_aar(rep: Any) -> bool:
    return isinstance(rep, dict) and (rep.get("report_type") == REPORT_TYPE
                                      or rep.get("format") == REPORT_TYPE)


def aar_node(rep: dict, rid: str) -> dict:
    """`{label, subtitle, status, ts_ms, attrs}` of an AAR's report node."""
    counts, le = _d(rep.get("counts")), _d(rep.get("loss_exchange"))
    parts = [f"{_int0(counts.get('events'))} events",
             f"{_int0(le.get('red_destroyed'))} red destroyed",
             f"{_int0(le.get('blue_losses'))} blue losses"]
    incomplete = rep.get("incomplete") is True
    if incomplete:
        parts.append("incomplete")
    session = _s(rep.get("session_id"))
    return {"label": f"{AAR_TITLE} {session}".strip(), "subtitle": "  ".join(parts),
            "status": "warn" if incomplete else "ok", "ts_ms": _int(rep.get("ended_at_ms")),
            "attrs": {"format": REPORT_TYPE, "report_id": rid, "session_id": session or None,
                      "theater_id": _s(rep.get("theater_id")) or None,
                      "started_at_ms": _int(rep.get("started_at_ms")),
                      "ended_at_ms": _int(rep.get("ended_at_ms")),
                      "count": _int(counts.get("events")),
                      "incomplete": True if incomplete else None, "simulated": True}}


def aar_entity(rep: dict) -> tuple[dict, dict, None]:
    """The AAR's inspector body: `fields.markdown` (the read view renders it)
    beside the counts, sorties and BDA accuracy. The timeline is in the
    markdown; the full record is `uav://reports/{id}`."""
    head = {k: v for k, v in rep.items() if not isinstance(v, (list, dict)) and k != "markdown"}
    head["format"] = REPORT_TYPE
    rid = _s(rep.get("report_id")) or _s(rep.get("id"))
    track_ids = [t for t in _l(rep.get("track_ids")) if isinstance(t, str)]
    fields = {"header": clean_value(head), "report_type": REPORT_TYPE, "format": REPORT_TYPE,
              "title": AAR_TITLE,
              "markdown": strip_bidi(rep["markdown"]) if isinstance(rep.get("markdown"), str)
              else None,
              "incomplete": rep.get("incomplete") is True,
              "counts": clean_value(rep.get("counts")),
              "loss_exchange": clean_value(rep.get("loss_exchange")),
              "sorties": clean_value(_l(rep.get("sorties"))),
              "bda_accuracy": clean_value(_l(rep.get("bda_accuracy"))),
              "timeline_events": len(_l(rep.get("timeline"))),
              "track_ids": track_ids[:50], "track_ids_total": len(track_ids),
              "simulated": True}
    provenance = {"source": "in-process report store: the simulated wargame's after-action "
                            "review (M14a)",
                  "resource": _s(rep.get("resource")) or f"uav://reports/{rid}",
                  "detail": "notional adjudications between scenario units; the tracks it "
                            "lists were deleted when the session ended",
                  "note": _s(rep.get("note")) or NOTIONAL_NOTE}
    return fields, provenance, None


# ---------------------------------------------------------------------------
# the map overlay (§3.3)
# ---------------------------------------------------------------------------

def _coords(value: Any) -> Any:
    """GeoJSON coordinates rounded to `COORD_DECIMALS`; None when not numeric."""
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                         for v in value):
            if not all(math.isfinite(float(v)) for v in value):
                return None
            return [round(float(v), COORD_DECIMALS) for v in value]
        out = [_coords(v) for v in value]
        return None if any(v is None for v in out) else out
    return None


def clean_feature(raw: Any, *, truth: bool) -> dict | None:
    """One engine feature as an overlay feature, or None when it is not a
    wargame kind with a usable geometry."""
    if not isinstance(raw, dict):
        return None
    props, geom = _d(raw.get("properties")), _d(raw.get("geometry"))
    kind, fid = props.get("kind"), _s(props.get("id")) or _s(raw.get("id"))
    coords = _coords(geom.get("coordinates"))
    if kind not in OVERLAY_KINDS or not fid or geom.get("type") not in GEOMETRIES \
            or coords is None:
        return None
    p = clean_value(props)
    p.update(kind=kind, id=fid, label=_trunc(p.get("label"), 80) or fid,
             register="scenario", simulated=True, truth=bool(truth))
    return {"type": "Feature", "id": fid,
            "geometry": {"type": geom["type"], "coordinates": coords}, "properties": p}


def overlay_features(srv: Any, *, truth: bool = False
                     ) -> tuple[list[dict], dict[str, int], dict[str, int]]:
    """The wargame's map features for one view (§3.3), cleaned and capped:
    `(features, counts, omitted)`, keyed by kind. Empty with no session. The
    engine fog-filters by view; this adds the caps and the text rules."""
    fn = getattr(engine_of(srv), "overlay_features", None)
    if not callable(fn):
        return [], {}, {}
    buckets: dict[str, list[dict]] = {k: [] for k in OVERLAY_CAPS}
    for raw in _l(fn(truth=bool(truth))):
        feat = clean_feature(raw, truth=truth)
        if feat is None:
            continue
        p = feat["properties"]
        key = p.get("kind_detail") if p["kind"] == "vector" else p["kind"]
        if key in buckets:
            buckets[key].append(feat)
    features: list[dict] = []
    counts: dict[str, int] = {}
    omitted: dict[str, int] = {}
    for key, cap in OVERLAY_CAPS.items():
        rows = buckets[key]
        kept = rows[:cap] if key in ("force", "force_envelope") else rows[-cap:]
        kind = "vector" if key in ("axis", "corridor") else key
        if kept:
            counts[kind] = counts.get(kind, 0) + len(kept)
        if len(rows) > len(kept):
            omitted[kind] = omitted.get(kind, 0) + len(rows) - len(kept)
        features.extend(kept)
    return features, counts, omitted
