"""Intel graph: every intel entity the console holds, as ONE honest picture.

CONTRACT §4. The intelligence console's orb, its search bar, the entity
inspector and the in-app analyst all read the same graph, so the rules that
keep it honest live here once:

  * DUPLICATES ARE COLLAPSED. The track store persists across runs, so the same
    ground-truth object re-tracked in ten runs is ten tracks (live: 10 x each
    Redmond object, and SALUTE sizes reading "10 x SAM battery"). Tracks with
    the same equipment name / OB class within `DUPLICATE_RADIUS_M` from
    DIFFERENT origin runs become one node, the freshest one, carrying
    `attrs.duplicates`; `meta.duplicates_collapsed` counts what was folded.
    Two tracks from the SAME run are never merged - within one run the
    tracker already decided they are different objects.
  * THE DEFAULT SCOPE IS THE ACTIVE THEATER. The store also spans theaters
    (live: Isfahan, Hormuz and LoC contacts while flying Redmond). Scope
    "theater" keeps contacts inside the active AO (plus `AO_MARGIN_M`, flagged
    `outside_ao`, because a SAM just outside the box still covers it) and
    counts the rest in `meta.out_of_theater`. An UNKNOWN active theater is
    never guessed: nothing is scoped and a caveat says so.
  * NO ASSESSMENT IS NOT "NONE". A contact with no threat level has
    `status: "unknown"` and `attrs.threat: "not assessed"`. `none` is a real
    model output and is only ever shown when the model said it.
  * NO FABRICATED TIMESTAMPS. Mission rows carry none; a mission node gets
    `ts_ms` only from its recorded start (`uav://mission/{id}.started`),
    otherwise `null`. Reports without `as_of` get `null`.
  * CAVEATS TRAVEL WITH THE DATA (`meta.caveats`): the real-data layer being
    off (AGL above the launch datum, geometric LOS), unknown theater, feeds
    down (an empty list is not a negative finding), stale custody, the scope
    and dedupe arithmetic.

`build_graph` is PURE (inputs in, dict out) so it is exhaustively testable
without a sim. `IntelService` gathers the inputs from the bridge's in-process
state (`app.state.godseye`, bridge.py `_godseye_context`) and, when given one,
the `GodseyeUavServer` (reports, pattern-of-life, link/latch state, live
per-track threat detail). It never calls MCP over HTTP and never commands
anything: every method here is a read.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .safety import point_in_polygon
from .targets import ELEMENT_RADIUS_M, OB_LIBRARY

SCHEMA = "godseye.intel-graph/v1"
SCOPES = ("theater", "all")

#: Same object re-tracked in another run: same equipment within this radius.
DUPLICATE_RADIUS_M = 25.0
#: `near` edge: a contact within this distance of a theater POI. The
#: pattern-of-life store uses the same 250 m radius for its POIs.
NEAR_POI_M = 250.0
#: A contact this far OUTSIDE the active AO is still "in theater" (flagged
#: `outside_ao`): an air-defence site just past the box still reaches into it,
#: while every other theater is thousands of kilometres away.
AO_MARGIN_M = 10_000.0
#: Custody lapses after this long without a fix (the INTREP gaps rule).
CUSTODY_LAPSE_S = 900.0
#: Mission phases in which a vehicle is flying it (the `flying` edge is
#: present tense; `complete`/`aborted` missions are history).
ACTIVE_MISSION_PHASES = frozenset({"planning", "executing", "rtb"})
#: Alarm nodes kept (the newest). The hub itself keeps 100.
MAX_ALARM_NODES = 50
#: Track nodes kept per graph. Past it the least salient are counted, not drawn.
MAX_TRACK_NODES = 400
#: Collapsed-duplicate ids listed on a graph node (newest first). The store
#: gains one more per run, forever; `attrs.duplicate_count` has the total and
#: `IntelService.entity` lists them all.
MAX_DUPLICATE_IDS = 5
#: Edges one report or one mission may contribute (reports_on / observes).
MAX_EDGES_PER_SOURCE = 100
#: Byte budgets (compact JSON).
ENTITY_MAX_BYTES = 60_000
OVERVIEW_MAX_BYTES = 6_000
GRAPH_TARGET_BYTES = 150_000
#: How long IntelService reuses a built graph (the UI polls every 2 s and the
#: analyst may ask several questions in one turn).
CACHE_TTL_S = 0.5

NOT_ASSESSED = "not assessed"
THREAT_RANK = {"critical": 5, "high": 4, "moderate": 3, "low": 2, "none": 1}
CONFIDENCE_RANK = {"confirmed": 3, "probable": 2, "possible": 1}

#: Node type -> id prefix (CONTRACT §4 "Node types and ids").
TYPE_PREFIX = {
    "vehicle": "veh", "mission": "msn", "track": "trk", "unit": "unit",
    "equipment": "ob", "report": "rpt", "theater": "thr", "poi": "poi",
    "alarm": "alarm", "feed": "feed",
}
PREFIX_TYPE = {v: k for k, v in TYPE_PREFIX.items()}

#: OB category -> orb cluster group.
CATEGORY_GROUP = {
    "sam": "air-defense", "aaa": "air-defense", "radar": "radar-ew",
    "c2": "c2", "armor": "ground-forces", "artillery": "ground-forces",
    "personnel": "ground-forces", "logistics": "logistics",
    "vehicle": "logistics", "structure": "infrastructure", "naval": "naval",
    "aircraft": "air", "civilian": "civilian",
}

_M_PER_DEG_LAT = 111_320.0
_TERMINAL_TASK_STATES = ("done", "failed", "cancelled")


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------

@dataclass
class GraphInputs:
    """Everything `build_graph` reads. Every field defaults to empty.

    The shapes are the bridge's own (bridge.py): `vehicles` are `/snapshot`
    vehicle rows (`vars(VehicleSnapshot)`), `missions` / `contacts` are
    `/snapshot.missions[]` / `.contacts[]`, `tracks` are the raw
    `uav_list_tracks` SALUTE rows behind the contacts, `theaters` is
    `/theaters.theaters[]`, `active_theater` its `active` block, `alarms` are
    `/events` payloads (oldest first, each with `seq` when known),
    `mission_details` maps mission_id -> `uav://mission/{id}.mission`,
    `per_vehicle` is `MissionIntel.per_vehicle`, `feeds` is
    `MissionIntel.feeds_dict()` and `reports` is report_id -> INTREP/THREATREP.
    """

    vehicles: list[dict] = field(default_factory=list)
    missions: list[dict] = field(default_factory=list)
    contacts: list[dict] = field(default_factory=list)
    tracks: list[dict] = field(default_factory=list)
    theaters: list[dict] = field(default_factory=list)
    active_theater: dict = field(default_factory=dict)
    alarms: list[dict] = field(default_factory=list)
    mission_details: dict = field(default_factory=dict)
    per_vehicle: dict = field(default_factory=dict)
    feeds: dict = field(default_factory=dict)
    reports: dict = field(default_factory=dict)
    # ---- extensions ----
    #: `uav://safety/geofence` (envelope, theater block, real_data as of boot).
    geofence: dict | None = None
    #: `GodseyeUavServer.real_data_status()` when in-process, else None.
    real_data: dict | None = None
    #: vehicle -> {link:{state,action,...}, bingo:{tripped,...}, queue:{...}}
    vehicle_status: dict = field(default_factory=dict)
    #: track_id -> {engagement, acquisition} (bridge threat rings)
    threat_rings: dict = field(default_factory=dict)
    #: the bridge's `sim_state` string ("up", "down: ...", ...)
    sim_state: str | None = None
    #: sources that could not be read while gathering, as sentences
    source_errors: list[str] = field(default_factory=list)
    #: wall clock for staleness; None = now. Tests pin it.
    now_ms: int | None = None


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(value: Any) -> int | None:
    out = _num(value)
    return None if out is None else int(out)


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    return value if isinstance(value, list) else []


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


#: Bidi embedding/override/isolate controls (U+202A-202E, U+2066-2069). A name
#: carrying one could render reversed or spoof a neighbouring label, so display
#: text is stripped of them before it reaches any client.
_BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069]")


def strip_bidi(text: str) -> str:
    return _BIDI_RE.sub("", text)


def _trunc(text: Any, n: int) -> str:
    s = strip_bidi(str(text or ""))
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _round(value: float | None, nd: int) -> float | None:
    return None if value is None else round(value, nd)


def json_size(obj: Any) -> int:
    """Bytes of `obj` as compact UTF-8 JSON (what the routes put on the wire)."""
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False,
                          default=str).encode())


def _ground_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    m_lon = _M_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.hypot((lat2 - lat1) * _M_PER_DEG_LAT, (lon2 - lon1) * m_lon)


def _poly(value: Any) -> list[tuple[float, float]] | None:
    """[[lat, lon], ...] (or [{lat, lon}]) with >= 3 usable vertices, else None."""
    pts: list[tuple[float, float]] = []
    for p in _l(value):
        if isinstance(p, dict):
            lat, lon = _num(p.get("lat")), _num(p.get("lon"))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            lat, lon = _num(p[0]), _num(p[1])
        else:
            continue
        if lat is not None and lon is not None:
            pts.append((lat, lon))
    return pts if len(pts) >= 3 else None


def _dist_to_polygon_m(lat: float, lon: float, poly: list[tuple[float, float]]) -> float:
    """0 inside (boundary included), else metres to the nearest edge."""
    if point_in_polygon(lat, lon, poly):
        return 0.0
    m_lon = _M_PER_DEG_LAT * math.cos(math.radians(lat))
    best = math.inf
    n = len(poly)
    for i in range(n):
        ax = (poly[i][1] - lon) * m_lon
        ay = (poly[i][0] - lat) * _M_PER_DEG_LAT
        bx = (poly[(i + 1) % n][1] - lon) * m_lon
        by = (poly[(i + 1) % n][0] - lat) * _M_PER_DEG_LAT
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def _area_membership(lat: float | None, lon: float | None,
                     poly: list[tuple[float, float]] | None,
                     margin_m: float = AO_MARGIN_M) -> tuple[bool, bool]:
    """(in the area incl. margin, only via the margin)."""
    if lat is None or lon is None or poly is None:
        return False, False
    d = _dist_to_polygon_m(lat, lon, poly)
    if d == 0.0:
        return True, False
    return (d <= margin_m), (d <= margin_m)


def _centroid(poly: list[tuple[float, float]]) -> tuple[float, float]:
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))


def _origin_run(track_id: str, row: dict | None = None) -> str:
    """`TRK-<run prefix>-<seq>` -> run prefix (targets.TrackManager ids)."""
    if row and _s(row.get("origin_run")):
        return _s(row.get("origin_run"))
    parts = track_id.split("-")
    if len(parts) >= 3 and parts[0].upper() == "TRK":
        return "-".join(parts[1:-1])
    return ""


#: Words that read as acronyms, never as "Sam" or "C2 element" -> "C2".
ACRONYMS = {w: w.upper() for w in (
    "sam", "aaa", "ew", "c2", "mlrs", "ifv", "mbt", "apc", "manpads", "uav", "isr",
    "gps", "poi", "ao", "los")}


def _humanize(word: str) -> str:
    """`snake_case` -> sentence case, with military/system acronyms upper-cased."""
    parts = [ACRONYMS.get(w.lower(), w) for w in str(word or "").replace("_", " ").split()]
    s = " ".join(parts)
    return s[:1].upper() + s[1:] if s else ""


def _threat_word(level: str | None) -> str:
    return level if level else NOT_ASSESSED


def _threat_status(level: str | None, stale: bool) -> str:
    if not level:
        return "unknown"
    if level == "critical":
        return "critical"
    if level in ("high", "moderate"):
        return "warn"
    return "stale" if stale else "ok"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _node(nid: str, ntype: str, label: str, *, subtitle: str = "", group: str = "",
          salience: float = 0.3, status: str = "unknown", ts_ms: int | None = None,
          lat: float | None = None, lon: float | None = None,
          attrs: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "label": _trunc(label, 80) or nid,
            "subtitle": _trunc(subtitle, 140), "group": group or ntype,
            "salience": round(_clamp01(salience), 3), "status": status,
            "ts_ms": ts_ms,
            "lat": _round(lat, 6), "lon": _round(lon, 6),
            "attrs": {k: strip_bidi(v) if isinstance(v, str) else v
                      for k, v in (attrs or {}).items() if v is not None}}


# ---------------------------------------------------------------------------
# tracks: normalise, dedupe, scope
# ---------------------------------------------------------------------------

@dataclass
class _Track:
    id: str
    run: str
    category: str
    ob_class: str
    equipment_name: str
    platform: str
    lat: float | None
    lon: float | None
    alt_m: float | None
    first_seen: float | None
    last_seen: float | None
    confidence: str
    confidence_score: float | None
    sightings: int | None
    threat: str | None
    row: dict
    compact: bool


@dataclass
class _Cluster:
    rep: _Track
    members: list[_Track]
    runs: set[str]
    key: tuple[str, str]
    threat: str | None = None
    threat_from: str | None = None
    theater: str | None = None
    in_active: bool = False
    outside_ao: bool = False
    unlocated: bool = False
    stale: bool = False
    salience: float = 0.0
    unit_id: str | None = None

    @property
    def node_id(self) -> str:
        return f"trk:{self.rep.id}"

    @property
    def duplicate_ids(self) -> list[str]:
        return [m.id for m in self.members if m is not self.rep]


def _track_from(row: dict | None, contact: dict | None) -> _Track | None:
    src = row if isinstance(row, dict) else (contact if isinstance(contact, dict) else {})
    tid = _s(src.get("track_id"))
    if not tid:
        return None
    contact = contact if isinstance(contact, dict) else {}
    loc = _d(src.get("location"))
    lat, lon = _num(loc.get("lat")), _num(loc.get("lon"))
    if lat is None or lon is None:
        lat, lon = _num(src.get("lat")), _num(src.get("lon"))
    if (lat is None or lon is None) and contact:
        cloc = _d(contact.get("location"))
        lat, lon = _num(cloc.get("lat")), _num(cloc.get("lon"))
    tm = _d(src.get("time")) if row else {}
    last = _num(tm.get("last_seen"))
    if last is None:
        last = _num(tm.get("epoch"))
    if last is None:
        ms = _num(contact.get("last_seen_ms") if contact else src.get("last_seen_ms"))
        last = None if ms is None else ms / 1000.0
    unit, equip = _d(src.get("unit")), _d(src.get("equipment"))
    conf = src.get("confidence")
    ob_class = _s(src.get("ob_class")) or _s(unit.get("ob_class"))
    category = (_s(src.get("category")) or _s(unit.get("category"))
                or _s(contact.get("category")))
    platform = _s(equip.get("platform"))
    if not platform and ob_class in OB_LIBRARY:
        platform = OB_LIBRARY[ob_class].name
    if not platform:
        platform = _s(_d(contact.get("salute")).get("equipment")) or _humanize(category)
    confidence = (_s(src.get("confidence_level"))
                  or (_s(conf.get("level")) if isinstance(conf, dict) else _s(conf))
                  or _s(contact.get("confidence")))
    threat = _s(contact.get("threat_level")) or _s(src.get("threat_level"))
    sightings = _int(src.get("sightings"))
    return _Track(
        id=tid, run=_origin_run(tid, row), category=category, ob_class=ob_class,
        equipment_name=_s(src.get("equipment_name")) or _s(equip.get("detected_as")),
        platform=platform, lat=lat, lon=lon,
        alt_m=_num(loc.get("alt_m")), first_seen=_num(tm.get("first_seen")),
        last_seen=last, confidence=confidence.lower(),
        confidence_score=_num(conf.get("score")) if isinstance(conf, dict) else None,
        sightings=sightings, threat=threat.lower() or None,
        row=src, compact=row is None)


def _normalise_tracks(rows: Iterable[dict], contacts: Iterable[dict]) -> list[_Track]:
    by_id = {str(c.get("track_id")): c for c in contacts
             if isinstance(c, dict) and c.get("track_id")}
    out: list[_Track] = []
    seen: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = _track_from(r, by_id.get(str(r.get("track_id"))))
        if t and t.id not in seen:
            out.append(t)
            seen.add(t.id)
    # Contacts with no raw row (a state source that publishes only the compact
    # rows): still intel, just less of it.
    for tid, c in by_id.items():
        if tid not in seen:
            t = _track_from(None, c)
            if t:
                out.append(t)
                seen.add(t.id)
    return out


def _dedupe(tracks: list[_Track]) -> list[_Cluster]:
    """Collapse the same object re-tracked in different runs (module docstring)."""
    order = sorted(tracks, key=lambda t: (-(t.last_seen or 0.0), -(t.sightings or 0), t.id))
    clusters: list[_Cluster] = []
    by_key: dict[tuple[str, str], list[_Cluster]] = {}
    for t in order:
        key = (t.equipment_name, t.ob_class)
        mergeable = (t.lat is not None and t.lon is not None and t.run
                     and (t.equipment_name or t.ob_class not in ("", "unclassified")))
        placed = False
        if mergeable:
            for c in by_key.get(key, []):
                if t.run in c.runs or c.rep.lat is None or c.rep.lon is None:
                    continue
                if _ground_m(c.rep.lat, c.rep.lon, t.lat, t.lon) <= DUPLICATE_RADIUS_M:
                    c.members.append(t)
                    c.runs.add(t.run)
                    placed = True
                    break
        if not placed:
            c = _Cluster(rep=t, members=[t], runs={t.run}, key=key)
            clusters.append(c)
            if mergeable:
                by_key.setdefault(key, []).append(c)
    for c in clusters:
        assessed = [(THREAT_RANK.get(m.threat or "", 0), m) for m in c.members if m.threat]
        if assessed:
            best = max(assessed, key=lambda x: (x[0], x[1] is c.rep))[1]
            c.threat = best.threat
            c.threat_from = None if best is c.rep else best.id
    return clusters


# ---------------------------------------------------------------------------
# the builder
# ---------------------------------------------------------------------------

class _GraphBuilder:
    """One pass over GraphInputs. Keeps its indexes so `IntelService.entity`
    can resolve ids, aliases and raw rows against exactly the graph it served."""

    def __init__(self, inputs: GraphInputs, scope: str = "theater"):
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")
        self.inp = inputs
        self.scope = scope
        self.now_ms = int(inputs.now_ms if inputs.now_ms is not None else time.time() * 1000)
        self.now_s = self.now_ms / 1000.0
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._edge_keys: set[tuple[str, str, str]] = set()
        self.alias: dict[str, str] = {}
        self.clusters: list[_Cluster] = []
        self.kept: list[_Cluster] = []
        self.cluster_by_node: dict[str, _Cluster] = {}
        self.units: dict[str, list[_Cluster]] = {}
        self.theater_rows: dict[str, dict] = {}
        self.poi_rows: dict[str, tuple[str, dict]] = {}
        self.vehicle_rows: dict[str, dict] = {}
        self.mission_rows: dict[str, dict] = {}
        self.report_rows: dict[str, dict] = {}
        self.alarm_rows: dict[str, dict] = {}
        self.feed_rows: dict[str, dict] = {}
        self.caveats: list[str] = []
        self.meta: dict = {}
        self.active: dict = {}
        self.active_id: str | None = None
        self.active_ao: list[tuple[float, float]] | None = None
        self.scoped = False
        self.tracks_total = 0
        self.tracks_omitted = 0
        self.out_of_theater_raw = 0
        self.out_of_theater_contacts = 0
        self.graph: dict = {}

    # ---- entry -------------------------------------------------------------
    def build(self) -> dict:
        self._theaters()
        self._tracks()
        self._vehicle_nodes()
        self._mission_nodes()
        self._track_nodes()
        self._theater_nodes()
        self._report_nodes()
        self._alarm_nodes()
        self._feed_nodes()
        self._edges()
        self._caveats()
        counts: dict[str, int] = {}
        for n in self.nodes.values():
            counts[n["type"]] = counts.get(n["type"], 0) + 1
        in_scope = self.kept
        assessed = sum(1 for c in in_scope if c.threat)
        self.meta = {
            "counts": counts,
            "tracks_total": self.tracks_total,
            "out_of_theater": self.out_of_theater_raw,
            "out_of_theater_contacts": self.out_of_theater_contacts,
            "duplicates_collapsed": sum(len(c.members) - 1 for c in in_scope),
            "tracks_omitted": self.tracks_omitted,
            "threat_assessed": assessed,
            "threat_unassessed": len(in_scope) - assessed,
            "stale_contacts": sum(1 for c in in_scope if c.stale),
            "scoped_to_theater": self.scoped,
            "ao_margin_m": AO_MARGIN_M if self.scoped else None,
            "duplicate_radius_m": DUPLICATE_RADIUS_M,
            "caveats": self.caveats,
            "feeds": {name[len("feed:"):]: {k: v for k, v in (
                ("ok", row.get("ok")), ("status", self.nodes[name]["status"]),
                ("error", row.get("error")), ("at_ms", row.get("at_ms"))) if v is not None}
                for name, row in self.feed_rows.items()},
        }
        self.graph = {
            "schema": SCHEMA,
            "generated_at_ms": self.now_ms,
            "scope": self.scope,
            "theater": self._theater_block(),
            "nodes": list(self.nodes.values()),
            "edges": self.edges,
            "meta": self.meta,
        }
        return self.graph

    def resolve(self, entity_id: str) -> str | None:
        """A node id, a bare id or a collapsed duplicate's id -> the node id."""
        eid = str(entity_id or "").strip()
        if eid in self.nodes:
            return eid
        if eid in self.alias:
            return self.alias[eid]
        if ":" not in eid:
            for prefix in ("trk", "msn", "veh", "rpt", "thr", "ob", "feed", "alarm"):
                cand = f"{prefix}:{eid}"
                if cand in self.nodes:
                    return cand
                if cand in self.alias:
                    return self.alias[cand]
        return None

    # ---- theaters ------------------------------------------------------------
    def _theaters(self) -> None:
        for t in self.inp.theaters:
            if isinstance(t, dict) and _s(t.get("id")):
                self.theater_rows[t["id"]] = t
        self.active = _d(self.inp.active_theater)
        if self.active.get("known") and _s(self.active.get("id")):
            self.active_id = self.active["id"]
            row = self.theater_rows.get(self.active_id, {})
            self.active_ao = (_poly(self.active.get("ao")) or _poly(row.get("ao"))
                              or _poly(_d(self.inp.geofence).get("geofence")))

    def _theater_of(self, lat: float | None, lon: float | None) -> str | None:
        if lat is None or lon is None:
            return None
        if self.active_id and self.active_ao and _area_membership(lat, lon, self.active_ao)[0]:
            return self.active_id
        for tid, row in self.theater_rows.items():
            if _area_membership(lat, lon, _poly(row.get("ao")))[0]:
                return tid
        return None

    def _theater_block(self) -> dict:
        if not self.active_id:
            return {"id": None, "label": None, "place": None, "known": False,
                    "reason": _s(self.active.get("reason")) or "no active theater was published"}
        row = self.theater_rows.get(self.active_id, {})
        return {"id": self.active_id,
                "label": _s(self.active.get("label")) or _s(row.get("label")) or self.active_id,
                "place": _s(row.get("place")) or None, "known": True}

    # ---- tracks --------------------------------------------------------------
    def _tracks(self) -> None:
        tracks = _normalise_tracks(self.inp.tracks, self.inp.contacts)
        self.tracks_total = len(tracks)
        self.clusters = _dedupe(tracks)
        self.scoped = self.scope == "theater" and self.active_ao is not None
        for c in self.clusters:
            t = c.rep
            c.unlocated = t.lat is None or t.lon is None
            c.theater = self._theater_of(t.lat, t.lon)
            if self.active_ao is not None:
                c.in_active, c.outside_ao = _area_membership(t.lat, t.lon, self.active_ao)
            age = None if t.last_seen is None else self.now_s - t.last_seen
            c.stale = age is not None and age > CUSTODY_LAPSE_S
            c.salience = self._track_salience(c)
            for m in c.members:
                self.alias[m.id] = c.node_id
                self.alias[f"trk:{m.id}"] = c.node_id
        kept = [c for c in self.clusters
                if not self.scoped or c.in_active or c.unlocated]
        dropped = [c for c in self.clusters if c not in kept] if self.scoped else []
        self.out_of_theater_contacts = len(dropped)
        self.out_of_theater_raw = sum(len(c.members) for c in dropped)
        for c in dropped:
            for m in c.members:
                self.alias.pop(m.id, None)
                self.alias.pop(f"trk:{m.id}", None)
        if len(kept) > MAX_TRACK_NODES:
            kept.sort(key=lambda c: (-c.salience, c.rep.id))
            for c in kept[MAX_TRACK_NODES:]:
                self.tracks_omitted += len(c.members)
                for m in c.members:
                    self.alias.pop(m.id, None)
                    self.alias.pop(f"trk:{m.id}", None)
            kept = kept[:MAX_TRACK_NODES]
        self.kept = kept
        self.cluster_by_node = {c.node_id: c for c in kept}
        self._units()

    def _track_salience(self, c: _Cluster) -> float:
        threat = {"critical": 1.0, "high": 0.85, "moderate": 0.65, "low": 0.45,
                  "none": 0.3}.get(c.threat or "", 0.5)
        conf = {"confirmed": 1.0, "probable": 0.85, "possible": 0.65}.get(c.rep.confidence, 0.6)
        fresh = 0.7 if c.stale else 1.0
        return _clamp01(0.1 + 0.9 * threat * conf * fresh)

    def _units(self) -> None:
        """Co-located same-category contacts (SALUTE 'Size', ELEMENT_RADIUS_M),
        recomputed on the DEDUPED picture so a battery seen in ten runs is one
        battery, not ten."""
        located = [c for c in self.kept if not c.unlocated and c.rep.category]
        parent = list(range(len(located)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, a in enumerate(located):
            for j in range(i + 1, len(located)):
                b = located[j]
                if a.rep.category != b.rep.category:
                    continue
                if _ground_m(a.rep.lat, a.rep.lon, b.rep.lat, b.rep.lon) <= ELEMENT_RADIUS_M:
                    parent[find(i)] = find(j)
        groups: dict[int, list[_Cluster]] = {}
        for i, c in enumerate(located):
            groups.setdefault(find(i), []).append(c)
        for members in groups.values():
            if len(members) < 2:
                continue
            uid = f"unit:{members[0].rep.category}:{min(m.rep.id for m in members)}"
            self.units[uid] = sorted(members, key=lambda m: m.rep.id)
            for m in members:
                m.unit_id = uid

    # ---- nodes -----------------------------------------------------------------
    def _add(self, node: dict) -> None:
        self.nodes[node["id"]] = node

    def _vehicle_nodes(self) -> None:
        rows: dict[str, dict] = {}
        for name, pv in _d(self.inp.per_vehicle).items():
            if isinstance(pv, dict):
                rows[str(name)] = dict(pv)
        for v in self.inp.vehicles:
            if isinstance(v, dict) and _s(v.get("name")):
                rows[v["name"]] = {**rows.get(v["name"], {}), **v, "_telemetry": True}
        for name in _d(self.inp.vehicle_status):
            rows.setdefault(str(name), {})
        for name in sorted(rows):
            row = rows[name]
            self.vehicle_rows[name] = row
            status_x = _d(_d(self.inp.vehicle_status).get(name))
            fuel = _num(row.get("fuel_pct"))
            bingo = _num(row.get("bingo_fuel_pct"))
            margin = None if fuel is None or bingo is None else fuel - bingo
            latched = self._bingo_latched(name, row)
            link_x = _d(status_x.get("link"))
            link = _s(link_x.get("state")) or None
            # When the vehicle's datalink went down, per the server's link
            # machine: `pending` (down, dwell not expired) and `loal` (declared
            # lost). A sim-host outage is `feed:sim`, never this.
            link_lost_since = _int(link_x.get("down_since_ms")) \
                if link in ("pending", "loal", "lost") else None
            landed_state = _int(row.get("landed_state"))
            landed = None if landed_state is None else landed_state == 0
            lat, lon = _num(row.get("latitude")), _num(row.get("longitude"))
            stale_ms = _int(row.get("stale_ms")) or 0
            agl_real = row.get("alt_agl_is_real") if isinstance(
                row.get("alt_agl_is_real"), bool) else None
            lost_link = self._lost_link(status_x)
            if latched or (margin is not None and margin <= 0) or link in ("loal", "lost") \
                    or row.get("telemetry_error") or stale_ms > 5000:
                status = "critical"
            elif fuel is None or not row.get("_telemetry"):
                status = "unknown"
            elif (margin is not None and margin < 10.0) or link in ("degraded", "pending") \
                    or row.get("datum_degraded") \
                    or row.get("alt_agl_launch_datum_mismatch_m") is not None:
                status = "warn"
            else:
                status = "ok"
            parts = []
            if fuel is not None:
                parts.append(f"fuel {fuel:.0f}%" + (f" / BINGO {bingo:.0f}%" if bingo is not None
                                                     else ""))
            else:
                parts.append("fuel unknown")
            if latched:
                parts.append("BINGO latched")
            if landed is not None:
                parts.append("landed" if landed else "airborne")
            if _s(row.get("mission")):
                parts.append(row["mission"])
            self._add(_node(
                f"veh:{name}", "vehicle", name, subtitle=" · ".join(parts), group="fleet",
                salience={"critical": 1.0, "warn": 0.9, "ok": 0.8}.get(status, 0.7),
                status=status, ts_ms=_int(row.get("timestamp_ms")), lat=lat, lon=lon,
                attrs={"fuel_pct": _round(fuel, 1), "bingo_fuel_pct": _round(bingo, 1),
                       "margin_pct": _round(margin, 1),
                       "eta_to_bingo_s": _round(_num(row.get("eta_to_bingo_s")), 0),
                       "bingo_latched": latched, "landed": landed,
                       "agl_m": _round(_num(row.get("alt_agl_m")), 1),
                       "agl_is_real": agl_real, "link": link,
                       "link_lost_since_ms": link_lost_since,
                       "mission": _s(row.get("mission")) or None,
                       "track_id": _s(row.get("track_id")) or None,
                       "stale_ms": stale_ms or None,
                       "datum_degraded": True if row.get("datum_degraded") else None,
                       "lost_link": lost_link}))

    def _lost_link(self, status_x: dict) -> dict | None:
        """The lost-link plan this vehicle would fly (CONTRACT §10.4), or None.

        The vehicle's own LIVE plan (a mission may have replaced it) when the
        server is in-process, else the server's default from the safety
        resource. `source` says which; unknown stays absent, never assumed.
        """
        plan = _d(_d(status_x.get("link")).get("plan"))
        source = "vehicle"
        if not plan:
            plan = _d(_d(self.inp.geofence).get("lost_link_plan"))
            source = "server default (uav://safety/geofence, as of boot)"
        if not _s(plan.get("behaviour")):
            return None
        out = {k: plan.get(k) for k in ("behaviour", "declare_after_s",
                                         "escalate_to_rtb_after_s", "climb_to_m")}
        out["source"] = source
        return out

    def _bingo_latched(self, name: str, row: dict) -> bool | None:
        b = _d(_d(_d(self.inp.vehicle_status).get(name)).get("bingo"))
        if isinstance(b.get("tripped"), bool):
            return b["tripped"]
        if isinstance(row.get("bingo_latched"), bool):
            return row["bingo_latched"]
        for m in self.inp.missions:
            if isinstance(m, dict) and m.get("vehicle") == name and m.get("phase") not in (
                    "complete", "aborted"):
                latched = _d(m.get("safety")).get("bingo_latched")
                if isinstance(latched, bool):
                    return latched
        return None

    def _mission_nodes(self) -> None:
        for m in self.inp.missions:
            if not isinstance(m, dict) or not _s(m.get("mission_id")):
                continue
            mid = m["mission_id"]
            self.mission_rows[mid] = m
            detail = _d(_d(self.inp.mission_details).get(mid))
            phase = _s(m.get("phase")) or "unknown"
            safety = _d(m.get("safety"))
            if safety.get("geofence") == "breach" or safety.get("bingo_latched") is True:
                status = "critical"
            elif phase in ("aborted", "rtb") or safety.get("geofence") == "proximity" \
                    or _s(m.get("incomplete_reason")):
                status = "warn"
            elif phase in ("planning", "executing", "complete"):
                status = "ok"
            else:
                status = "unknown"
            progress = _num(m.get("progress_pct"))
            kind = _s(m.get("kind")) or _s(detail.get("kind")) or "mission"
            vehicle = _s(m.get("vehicle"))
            started = _num(detail.get("started"))
            # 1-based, from the flying task (bridge row `waypoint {index, of}`);
            # only the halves that are known, never a guessed position.
            wp = _d(m.get("waypoint"))
            waypoint = {k: v for k, v in (("index", _int(wp.get("index"))),
                                          ("of", _int(wp.get("of")))) if v is not None}
            sub = phase + (f" · {progress:.0f}%" if progress is not None else "")
            if _s(m.get("incomplete_reason")):
                sub += f" · {m['incomplete_reason']}"
            self._add(_node(
                f"msn:{mid}", "mission", f"{_humanize(kind)} · {vehicle or mid}",
                subtitle=sub, group="missions",
                salience=0.9 if phase in ("executing", "rtb", "planning") else 0.6,
                status=status,
                ts_ms=int(started * 1000) if started is not None else None,
                attrs={"mission_id": mid, "kind": kind, "phase": phase,
                       "vehicle": vehicle or None, "progress_pct": _round(progress, 1),
                       "eta_s": _round(_num(m.get("eta_s")), 0),
                       "waypoint": waypoint or None,
                       "coverage_pct": _round(_num(m.get("coverage_pct")), 1),
                       "geofence": _s(safety.get("geofence")) or None,
                       "incomplete_reason": _s(m.get("incomplete_reason")) or None,
                       "ts_basis": "mission start (uav://mission)" if started is not None
                       else None}))

    def _track_nodes(self) -> None:
        ob_seen: dict[str, list[_Cluster]] = {}
        for c in self.kept:
            t = c.rep
            age = None if t.last_seen is None else max(0.0, self.now_s - t.last_seen)
            dup = c.duplicate_ids
            sub = [t.confidence or "unrated"]
            if t.sightings is not None:
                sub.append(f"{t.sightings} sighting{'s' if t.sightings != 1 else ''}")
            if dup:
                sub.append(f"{len(c.members)} runs")
            if c.stale:
                sub.append("custody lapsed")
            if t.equipment_name:
                sub.insert(0, t.equipment_name)
            self._add(_node(
                c.node_id, "track", t.platform or t.id, subtitle=" · ".join(sub),
                group=CATEGORY_GROUP.get(t.category, t.category or "unclassified"),
                salience=c.salience, status=_threat_status(c.threat, c.stale),
                ts_ms=int(t.last_seen * 1000) if t.last_seen is not None else None,
                lat=t.lat, lon=t.lon,
                attrs={"category": t.category or None, "ob_class": t.ob_class or None,
                       "confidence": t.confidence or None,
                       "threat": _threat_word(c.threat),
                       "sightings": t.sightings,
                       "age_s": None if age is None else int(age),
                       "stale": True if c.stale else None,
                       "duplicates": dup[:MAX_DUPLICATE_IDS] or None,
                       "duplicate_count": len(dup) or None,
                       "unit": c.unit_id,
                       "theater": c.theater,
                       "outside_ao": True if c.outside_ao else None,
                       "out_of_theater": (True if self.active_id and c.theater != self.active_id
                                          and not c.unlocated else None),
                       "unlocated": True if c.unlocated else None}))
            if t.ob_class and t.ob_class != "unclassified":
                ob_seen.setdefault(t.ob_class, []).append(c)
        for uid, members in self.units.items():
            reps = [m.rep for m in members]
            classes = {r.ob_class for r in reps}
            ob = OB_LIBRARY.get(next(iter(classes))) if len(classes) == 1 else None
            cat = reps[0].category
            label = (f"{len(reps)} x {ob.name}" if ob else
                     f"{_humanize(cat)} element ({len(reps)} contacts)")
            nominal = ob.typical_unit_count if ob else None
            sub = (f"partial element ({len(reps)} of a nominal {nominal})"
                   if nominal and len(reps) < nominal else
                   (ob.typical_unit_size if ob else f"{len(reps)} co-located contacts"))
            levels = [m.threat for m in members if m.threat]
            top = max(levels, key=lambda lv: THREAT_RANK.get(lv, 0)) if levels else None
            lat = sum(r.lat for r in reps) / len(reps)
            lon = sum(r.lon for r in reps) / len(reps)
            seen = [r.last_seen for r in reps if r.last_seen is not None]
            stale = all(m.stale for m in members)
            self._add(_node(
                uid, "unit", label, subtitle=sub,
                group=CATEGORY_GROUP.get(cat, cat or "unclassified"),
                salience=max(m.salience for m in members),
                status=_threat_status(top, stale),
                ts_ms=int(max(seen) * 1000) if seen else None, lat=lat, lon=lon,
                attrs={"category": cat, "members": len(reps),
                       "threat": _threat_word(top), "nominal": nominal,
                       "basis": f"same-category contacts within {ELEMENT_RADIUS_M:.0f} m, "
                                "after duplicate collapse"}))
        for ob_class, clusters in sorted(ob_seen.items()):
            ob = OB_LIBRARY.get(ob_class)
            reps = [c.rep for c in clusters]
            self._add(_node(
                f"ob:{ob_class}", "equipment",
                ob.name if ob else (reps[0].platform or ob_class),
                subtitle=f"{len(reps)} contact{'s' if len(reps) != 1 else ''}"
                         + (f" · {ob.role}" if ob else ""),
                group=CATEGORY_GROUP.get(ob.category if ob else reps[0].category,
                                         "unclassified"),
                salience=0.3 + 0.5 * (ob.threat_weight if ob else 0.3), status="ok",
                attrs={"ob_class": ob_class, "category": ob.category if ob else reps[0].category,
                       "contacts": len(reps),
                       "weapon_range_m": ob.weapon_range_m if ob else None,
                       "acquisition_range_m": ob.acquisition_range_m if ob else None}))

    def _theater_nodes(self) -> None:
        if self.scope == "all":
            ids = list(self.theater_rows)
            if self.active_id and self.active_id not in ids:
                ids.insert(0, self.active_id)
        else:
            ids = [self.active_id] if self.active_id else []
        mismatch = self.active.get("theater_mismatch")
        for tid in ids:
            row = _d(self.theater_rows.get(tid))
            ao = (self.active_ao if tid == self.active_id else None) or _poly(row.get("ao"))
            lat, lon = _centroid(ao) if ao else (None, None)
            active = tid == self.active_id
            # Another theater's node and POIs are outside the active AO: say
            # so, so search labels and demotes them like out-of-theater tracks.
            outside = True if (self.active_id and not active) else None
            rd = _d(row.get("real_data"))
            status = ("warn" if active and mismatch else "ok") if active else "ok"
            label = (_s(self.active.get("label")) if active else "") or _s(row.get("label")) or tid
            self._add(_node(
                f"thr:{tid}", "theater", label,
                subtitle=(_s(row.get("place")) + (" · active" if active else "")).strip(" ·"),
                group="theater", salience=0.95 if active else 0.25, status=status,
                ts_ms=_int(self.active.get("at_ms")) if active else None, lat=lat, lon=lon,
                attrs={"active": active, "place": _s(row.get("place")) or None,
                       "in_table": bool(row), "real_data": rd.get("hydrated"),
                       "theater_mismatch": True if active and mismatch else None,
                       "out_of_theater": outside}))
            for p in _l(row.get("pois")):
                name = _s(_d(p).get("name"))
                plat, plon = _num(_d(p).get("lat")), _num(_d(p).get("lon"))
                if not name:
                    continue
                pid = f"poi:{tid}:{name}"
                self.poi_rows[pid] = (tid, p)
                self._add(_node(pid, "poi", name, subtitle=label, group="theater",
                                salience=0.5 if active else 0.15, status="ok",
                                lat=plat, lon=plon,
                                attrs={"theater": tid, "out_of_theater": outside}))

    def _report_nodes(self) -> None:
        reports = _d(self.inp.reports)
        seen_objs: list[int] = []
        named = {k: v for k, v in reports.items() if k != "latest"}
        items = list(named.items())
        latest = reports.get("latest")
        if isinstance(latest, dict) and not any(
                v is latest or (_s(v.get("report_id")) and v.get("report_id")
                                == latest.get("report_id"))
                for v in named.values() if isinstance(v, dict)):
            items.append(("latest", latest))
        for key, rep in items:
            if not isinstance(rep, dict) or id(rep) in seen_objs:
                continue
            seen_objs.append(id(rep))
            rid = _s(rep.get("report_id")) or str(key)
            nid = f"rpt:{rid}"
            self.report_rows[nid] = rep
            fmt = _s(rep.get("format")) or "REPORT"
            as_of = _num(rep.get("as_of"))
            if fmt == "THREATREP":
                top = _s(rep.get("highest_threat")) or None
                sub = f"highest {top or 'n/a'} · {_int(rep.get('count')) or 0} contacts"
                status = {"critical": "critical", "high": "warn"}.get(top or "", "ok")
            else:
                gaps = len(_l(rep.get("gaps")))
                n = _int(rep.get("total_tracks"))
                sub = (f"{n} contacts · " if n is not None else "") + f"{gaps} gaps"
                status = "warn" if gaps else "ok"
            self._add(_node(
                nid, "report", f"{fmt} {rid}", subtitle=sub, group="reports",
                salience=0.6, status=status,
                ts_ms=int(as_of * 1000) if as_of is not None else None,
                attrs={"format": fmt, "report_id": rid,
                       "highest_threat": _s(rep.get("highest_threat")) or None,
                       "count": _int(rep.get("count") if rep.get("count") is not None
                                     else rep.get("total_tracks")),
                       "gaps": len(_l(rep.get("gaps"))) if "gaps" in rep else None,
                       "detail": _s(rep.get("detail")) or None}))

    def _alarm_nodes(self) -> None:
        alarms = [a for a in self.inp.alarms if isinstance(a, dict)]
        base = len(alarms)
        for i, a in enumerate(alarms[-MAX_ALARM_NODES:]):
            seq = _int(a.get("seq"))
            if seq is None:
                seq = base - min(base, MAX_ALARM_NODES) + i + 1
            nid = f"alarm:{seq}"
            self.alarm_rows[nid] = a
            sev = _s(a.get("severity"))
            kind = _s(a.get("kind")) or "alarm"
            self._add(_node(
                nid, "alarm", _humanize(kind), subtitle=_s(a.get("message")),
                group="alarms",
                salience={"critical": 0.9, "warning": 0.7}.get(sev, 0.4),
                status={"critical": "critical", "warning": "warn", "info": "ok"}.get(sev,
                                                                                    "unknown"),
                ts_ms=_int(a.get("atMs")),
                attrs={"seq": seq, "kind": kind, "severity": sev or None,
                       "vehicle": _s(a.get("vehicle")) or None,
                       "track_id": _s(a.get("track_id")) or None,
                       "mission_id": _s(a.get("mission_id")) or None}))

    def _feed_nodes(self) -> None:
        feeds: dict[str, dict] = {}
        for name, f in _d(self.inp.feeds).items():
            f = _d(f)
            feeds[str(name)] = {"ok": bool(f.get("ok")), "error": _s(f.get("error")) or None,
                                "at_ms": _int(f.get("atMs") if "atMs" in f else f.get("at_ms")),
                                "detail": _s(f.get("detail")) or None}
        if self.inp.sim_state is not None:
            sim = str(self.inp.sim_state)
            feeds["sim"] = {"ok": sim.startswith("up"),
                            "error": None if sim.startswith("up") else sim,
                            "detail": sim, "at_ms": None,
                            "warn": sim != "up" and sim.startswith("up")}
        rd = self._real_data()
        if rd is not None:
            feeds["real_data"] = {"ok": bool(rd.get("hydrated")), "error": None,
                                  "detail": self._real_data_words(rd), "at_ms": None,
                                  "warn": not rd.get("hydrated") or bool(rd.get("degraded_feeds"))}
        # Unknown for up to GEOFENCE_RETRY_S after boot, so a warning, not a fault.
        feeds["theater"] = {"ok": bool(self.active_id), "at_ms": _int(self.active.get("at_ms")),
                            "error": None if self.active_id else (
                                _s(self.active.get("reason")) or "active theater unknown"),
                            "detail": f"active theater {self.active_id}" if self.active_id
                            else None,
                            "warn": not self.active_id}
        for name, f in feeds.items():
            nid = f"feed:{name}"
            self.feed_rows[nid] = f
            status = "critical" if not f["ok"] and not f.get("warn") else (
                "warn" if f.get("warn") else "ok")
            self._add(_node(nid, "feed", _humanize(name),
                            subtitle=f.get("error") or f.get("detail") or "",
                            group="feeds", salience=0.7 if status != "ok" else 0.2,
                            status=status, ts_ms=f.get("at_ms"),
                            attrs={"ok": f["ok"], "error": _trunc(f.get("error"), 200) or None}))

    def _real_data(self) -> dict | None:
        if isinstance(self.inp.real_data, dict):
            return self.inp.real_data
        rd = _d(self.inp.geofence).get("real_data")
        return rd if isinstance(rd, dict) else None

    @staticmethod
    def _real_data_words(rd: dict) -> str:
        if not rd.get("enabled", rd.get("hydrated")):
            return "off: AGL is height above the launch datum, LOS is geometric"
        if not rd.get("hydrated"):
            return "enabled, not hydrated: every feed still reads as synthetic"
        degraded = _d(rd.get("degraded_feeds"))
        return ("hydrated" + (f"; degraded: {', '.join(sorted(degraded))}" if degraded else ""))

    # ---- edges -----------------------------------------------------------------
    def _edge(self, a: str | None, b: str | None, kind: str) -> None:
        if not a or not b or a == b or a not in self.nodes or b not in self.nodes:
            return
        key = (a, b, kind)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        self.edges.append({"a": a, "b": b, "kind": kind})

    def _trk(self, track_id: Any) -> str | None:
        tid = _s(track_id)
        return self.alias.get(tid) if tid else None

    def _edges(self) -> None:
        # vehicles
        for name, row in self.vehicle_rows.items():
            vid = f"veh:{name}"
            mid = _s(row.get("mission"))
            if mid:
                self._edge(vid, f"msn:{mid}", "flying")
            self._edge(vid, self._trk(row.get("track_id")), "tracking")
            lat, lon = _num(row.get("latitude")), _num(row.get("longitude"))
            where = self._theater_of(lat, lon)
            if where and (self.scope == "all" or where == self.active_id):
                self._edge(vid, f"thr:{where}", "operating_in")
        # missions
        for mid, m in self.mission_rows.items():
            nid = f"msn:{mid}"
            vehicle = _s(m.get("vehicle"))
            # `flying` is present tense: only a mission still in progress. A
            # completed or aborted mission kept a `flying` link to its drone
            # forever (the vehicle side, from row.mission, was already gated).
            if vehicle and m.get("phase") in ACTIVE_MISSION_PHASES:
                self._edge(f"veh:{vehicle}", nid, "flying")
            detail = _d(_d(self.inp.mission_details).get(mid))
            meta = _d(detail.get("meta"))
            target = self._trk(meta.get("track_id"))
            self._edge(nid, target, "target")
            if target and vehicle and m.get("phase") in ("planning", "executing"):
                self._edge(f"veh:{vehicle}", target, "tracking")
            area = _poly(_d(meta.get("_plan_params")).get("polygon")) \
                or _poly(meta.get("area_polygon"))
            if area:
                n = 0
                for c in self.kept:
                    if n >= MAX_EDGES_PER_SOURCE:
                        break
                    if not c.unlocated and point_in_polygon(c.rep.lat, c.rep.lon, area) \
                            and c.node_id != target:
                        self._edge(nid, c.node_id, "observes")
                        n += 1
        # tracks
        for c in self.kept:
            nid = c.node_id
            self._edge(nid, c.unit_id, "member_of")
            if c.rep.ob_class:
                self._edge(nid, f"ob:{c.rep.ob_class}", "is_a")
            if c.theater:
                self._edge(nid, f"thr:{c.theater}", "in_theater")
            if c.unlocated:
                continue
            for pid, (_tid, p) in self.poi_rows.items():
                plat, plon = _num(_d(p).get("lat")), _num(_d(p).get("lon"))
                if plat is not None and plon is not None and _ground_m(
                        c.rep.lat, c.rep.lon, plat, plon) <= NEAR_POI_M:
                    self._edge(nid, pid, "near")
        for pid, (tid, _p) in self.poi_rows.items():
            self._edge(pid, f"thr:{tid}", "in_theater")
        # reports
        for nid, rep in self.report_rows.items():
            n = 0
            for key in ("assessments", "omitted", "contacts", "contacts_omitted"):
                for row in _l(rep.get(key)):
                    if n >= MAX_EDGES_PER_SOURCE:
                        break
                    target = self._trk(_d(row).get("track_id"))
                    if target and (nid, target, "reports_on") not in self._edge_keys:
                        self._edge(nid, target, "reports_on")
                        n += 1
        # alarms
        for nid, a in self.alarm_rows.items():
            if _s(a.get("vehicle")):
                self._edge(nid, f"veh:{a['vehicle']}", "about")
            self._edge(nid, self._trk(a.get("track_id")), "about")
            if _s(a.get("mission_id")):
                self._edge(nid, f"msn:{a['mission_id']}", "about")

    # ---- caveats ---------------------------------------------------------------
    def _caveats(self) -> None:
        out: list[str] = []
        rd = self._real_data()
        if rd is not None and not rd.get("enabled", rd.get("hydrated")):
            out.append("Real-data layer is off: AGL is height above the launch datum (not "
                       "terrain clearance), LOS is geometric with no terrain, and there is no "
                       "live air traffic.")
        elif rd is not None and not rd.get("hydrated"):
            out.append("Real-data layer is enabled but not hydrated: every feed still reads as "
                       "synthetic.")
        elif rd is not None and _d(rd.get("degraded_feeds")):
            out.append("Real-data feeds degraded ("
                       + ", ".join(sorted(_d(rd.get("degraded_feeds"))))
                       + "): treat those values as assumed.")
        assumed = sorted(n for n, r in self.vehicle_rows.items()
                         if r.get("alt_agl_is_real") is False)
        if assumed and (rd is None or rd.get("hydrated")):
            out.append(f"AGL for {', '.join(assumed)} is height above the launch datum, not "
                       "measured terrain clearance.")
        degraded = sorted(n for n, r in self.vehicle_rows.items() if r.get("datum_degraded"))
        if degraded:
            out.append(f"Geoid degraded for {', '.join(degraded)}: altitudes are not gate-grade.")
        if not self.active_id:
            out.append("Active theater unknown ("
                       + (_s(self.active.get("reason")) or "not published")
                       + "); contacts are not scoped to an AO.")
        elif self.active_ao is None:
            out.append(f"Active theater {self.active_id} has no AO polygon; contacts are not "
                       "scoped.")
        if self.active.get("theater_mismatch"):
            out.append("The server reports a theater mismatch: its enforced envelope belongs to "
                       "a different theater than its theater row.")
        if self.out_of_theater_raw:
            out.append(f"{self.out_of_theater_raw} track(s) ({self.out_of_theater_contacts} "
                       "contact(s)) from other theaters' runs are hidden in theater scope; "
                       "scope=all shows them.")
        collapsed = sum(len(c.members) - 1 for c in self.kept)
        if collapsed:
            merged = sum(1 for c in self.kept if len(c.members) > 1)
            out.append(f"{collapsed} duplicate track(s) from repeated runs were collapsed into "
                       f"{merged} contact(s) (same equipment within {DUPLICATE_RADIUS_M:.0f} m, "
                       "different runs); counts and unit sizes use the collapsed picture.")
        unassessed = sum(1 for c in self.kept if not c.threat)
        if unassessed:
            out.append(f"{unassessed} of {len(self.kept)} contact(s) have no threat assessment "
                       "(outside the assessed area or none served): shown as 'not assessed', "
                       "never 'none'.")
        if any(c.threat for c in self.kept):
            out.append("Threat levels are model outputs (capability x intent) for sensor "
                       "posture and self-protection only; ISR-only, no engagement "
                       "recommendation.")
        stale = sum(1 for c in self.kept if c.stale)
        if stale:
            out.append(f"{stale} contact(s) not re-fixed in over "
                       f"{CUSTODY_LAPSE_S / 60:.0f} min: positions are last known, not current.")
        if self.tracks_omitted:
            out.append(f"{self.tracks_omitted} lower-salience track(s) past the "
                       f"{MAX_TRACK_NODES}-node cap are counted but not drawn.")
        if any(_num(_d(_d(self.inp.mission_details).get(mid)).get("started")) is None
               for mid in self.mission_rows):
            out.append("Mission rows carry no timestamps; a mission shows only its recorded "
                       "start time, never an invented one.")
        for name, f in self.feed_rows.items():
            if not f.get("ok") and not f.get("warn") and name != "feed:theater":
                out.append(f"Feed {name[5:]} is down ({_trunc(f.get('error'), 120)}); "
                           "an absent entity there is not a negative finding.")
        for err in self.inp.source_errors:
            out.append(_trunc(err, 200))
        dedup: list[str] = []
        for c in out:
            if c not in dedup:
                dedup.append(c)
        self.caveats = dedup[:14]


def build_graph(inputs: GraphInputs, *, scope: str = "theater") -> dict:
    """The intel graph (schema `godseye.intel-graph/v1`, CONTRACT §4). Pure."""
    return _GraphBuilder(inputs, scope).build()


# ---------------------------------------------------------------------------
# shrinking to a byte budget (never silent)
# ---------------------------------------------------------------------------

def _finite(node: Any) -> Any:
    """Non-finite floats -> None. Starlette's JSONResponse refuses NaN/inf, so
    one bad number in a raw row would turn an inspector read into a 500."""
    if isinstance(node, float):
        return node if math.isfinite(node) else None
    if isinstance(node, list):
        return [_finite(v) for v in node]
    if isinstance(node, dict):
        return {k: _finite(v) for k, v in node.items()}
    return node


def fit_to_budget(obj: dict, max_bytes: int, *, drop_first: tuple[str, ...] = ("raw",)) -> dict:
    """Shrink `obj` under `max_bytes` of compact JSON, saying what was cut.

    First drops the optional top-level keys in `drop_first`, then repeatedly
    halves the longest list anywhere in the document (with an explicit
    `_truncated` marker), then clips long strings. A response is never
    shortened silently: `_truncated` lists what was removed.
    """
    out = _finite(json.loads(json.dumps(obj, default=str)))
    if json_size(out) <= max_bytes:
        return out
    # The `_truncated` report is added after cutting; reserve room for it so
    # the RESULT (marker included) is within `max_bytes`, not just the body.
    report = {"dropped": list(drop_first), "lists_shortened": 999,
              "max_bytes": max_bytes}
    reserve = json_size({"_truncated": report}) + 1
    budget = max(0, max_bytes - reserve)
    dropped: list[str] = []
    for key in drop_first:
        if key in out:
            out.pop(key)
            dropped.append(key)
            if json_size(out) <= budget:
                out["_truncated"] = {"dropped": dropped}
                return out

    def longest(node: Any, path: tuple = ()) -> tuple[int, tuple]:
        best = (0, ())
        if isinstance(node, list):
            if len(node) > 1:
                best = (len(node), path)
            for i, v in enumerate(node):
                cand = longest(v, path + (i,))
                best = max(best, cand, key=lambda x: x[0])
        elif isinstance(node, dict):
            for k, v in node.items():
                cand = longest(v, path + (k,))
                best = max(best, cand, key=lambda x: x[0])
        return best

    shortened = 0
    for _ in range(200):
        if json_size(out) <= budget:
            break
        n, path = longest(out)
        if n <= 2:
            break
        parent = out
        for p in path[:-1]:
            parent = parent[p]
        lst = parent[path[-1]]
        real = [x for x in lst if not (isinstance(x, dict) and x.get("_truncated"))]
        prior = sum(x.get("_omitted", 0) for x in lst if isinstance(x, dict)
                    and x.get("_truncated"))
        keep = max(1, len(real) // 2)
        parent[path[-1]] = real[:keep] + [{"_truncated": True,
                                           "_omitted": len(real) - keep + prior,
                                           "_note": "list shortened to fit the response budget"}]
        shortened += 1
    if json_size(out) > budget:
        def clip(node: Any) -> Any:
            if isinstance(node, str) and len(node) > 200:
                return node[:199] + "…"
            if isinstance(node, list):
                return [clip(v) for v in node]
            if isinstance(node, dict):
                return {k: clip(v) for k, v in node.items()}
            return node
        out = clip(out)
    out["_truncated"] = {"dropped": dropped, "lists_shortened": shortened,
                         "max_bytes": max_bytes}
    return out


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"[\w:.\-/]+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(str(text or "").lower()) if t]


def _normalise_types(types: Iterable[str] | None) -> set[str] | None:
    if not types:
        return None
    out: set[str] = set()
    for t in types:
        t = str(t or "").strip().lower()
        if t in TYPE_PREFIX:
            out.add(t)
        elif t in PREFIX_TYPE:
            out.add(PREFIX_TYPE[t])
        elif t.rstrip("s") in TYPE_PREFIX:
            out.add(t.rstrip("s"))
        elif t in ("contact", "contacts", "target", "targets"):
            out.add("track")
        elif t in ("ob", "ob_class", "class"):
            out.add("equipment")
    return out or None


def search_nodes(graph: dict, query: str, types: Iterable[str] | None = None,
                 limit: int = 25, *, aliases: dict[str, str] | None = None) -> list[dict]:
    """Case-insensitive token search over label / id / type / subtitle / attrs.

    Ranked: exact id > id prefix > exact label > label prefix > token matches
    (label word-start > label > id > type > subtitle > attrs). Every token must
    match; only when no node matches them all are any-token matches returned.
    Out-of-theater nodes rank after in-theater ones.
    """
    q = str(query or "").strip().lower()
    wanted = _normalise_types(types)
    limit = max(1, min(int(limit or 25), 100))
    if not q:
        return []
    toks = _tokens(q)
    alias_target = (aliases or {}).get(query.strip()) or (aliases or {}).get(q)
    scored: list[tuple[float, float, str, dict, bool]] = []
    for n in graph.get("nodes", []):
        if wanted and n.get("type") not in wanted:
            continue
        nid = str(n.get("id", "")).lower()
        bare = nid.split(":", 1)[1] if ":" in nid else nid
        label = str(n.get("label", "")).lower()
        sub = str(n.get("subtitle", "")).lower()
        ntype = str(n.get("type", "")).lower()
        attrs = n.get("attrs") or {}
        attr_text = " ".join(
            " ".join(map(str, v)) if isinstance(v, list) else str(v)
            for v in attrs.values()).lower()
        score = 0.0
        if q == nid or q == bare or (alias_target and alias_target == n.get("id")):
            score = 1000.0
        elif nid.startswith(q) or bare.startswith(q):
            score = 800.0
        elif q == label:
            score = 700.0
        elif label.startswith(q):
            score = 600.0
        matched = 0
        tok_score = 0.0
        label_words = _tokens(label)
        for t in toks:
            s = 0.0
            if any(w.startswith(t) for w in label_words):
                s = 30.0
            elif t in label:
                s = 20.0
            elif t in nid:
                s = 15.0
            elif t in ntype or (len(t) >= 3 and ntype.startswith(t.rstrip("s"))):
                s = 12.0
            elif t in sub:
                s = 10.0
            elif t in attr_text:
                s = 5.0
            if s:
                matched += 1
                tok_score += s
        all_tokens = bool(toks) and matched == len(toks)
        if score == 0.0 and matched == 0:
            continue
        strong = score >= 600.0 or all_tokens
        if score == 0.0:
            score = (100.0 if all_tokens else 0.0) + tok_score
        else:
            score += tok_score
        if attrs.get("out_of_theater"):
            score -= 50.0
        scored.append((score, float(n.get("salience") or 0.0), str(n.get("label", "")), n,
                       strong))
    # Every token must match; any-token matches only when nothing matched all.
    if any(row[4] for row in scored):
        scored = [row for row in scored if row[4]]
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    out = []
    for score, _sal, _label, n, _strong in scored[:limit]:
        row = {"id": n["id"], "type": n["type"], "label": n["label"],
               "subtitle": n.get("subtitle", ""), "status": n.get("status"),
               "score": round(score, 1)}
        if (n.get("attrs") or {}).get("out_of_theater"):
            row["out_of_theater"] = True
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------

#: `active.source` when the theater came from the in-process server rather
#: than the bridge's copy of uav://safety/geofence.
SERVER_THEATER_SOURCE = "in-process server (theater block of uav://safety/geofence)"


def _server_active_theater(srv: Any, now_ms: int | None) -> dict | None:
    """The active-theater block from the in-process server's own theater
    (the `theater` block its uav://safety/geofence resource publishes), or
    None when the server has none. Never falls back to the table default."""
    t = getattr(srv, "theater", None)
    if t is None or not _s(getattr(t, "id", None)):
        return None
    from . import theaters

    ao = t.ao_list() if callable(getattr(t, "ao_list", None)) else None
    block = {"id": t.id, "label": getattr(t, "label", None), "ao": ao,
             "ground_elevation_msl_m": getattr(t, "home_alt_msl_m", None)}
    return theaters.active_from_server(
        block, source=SERVER_THEATER_SOURCE,
        at_ms=int(now_ms if now_ms is not None else time.time() * 1000),
        theater_mismatch=getattr(srv, "theater_mismatch", None))


def _call(ctx: Any, name: str, *args: Any) -> Any:
    fn = getattr(ctx, name, None)
    return fn(*args) if callable(fn) else None


def _copy_mapping(mapping: Any, tries: int = 3) -> dict:
    """`dict(m)` of a dict another thread may be growing, retried."""
    for _ in range(tries):
        try:
            return dict(mapping or {})
        except RuntimeError:  # changed size during iteration
            continue
    return {}


class IntelService:
    """The in-process intel reader behind `/intel/*` and the analyst's tools.

    `bridge_ctx` is `bridge_app.state.godseye` (bridge.py `_godseye_context`);
    `server` is the in-process `GodseyeUavServer`, or None. Nothing here makes
    an MCP call or commands anything.
    """

    def __init__(self, bridge_ctx: Any, server: Any = None):
        self.ctx = bridge_ctx
        self.server = server
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, _GraphBuilder]] = {}
        self.cache_ttl_s = CACHE_TTL_S
        self.clock: Callable[[], float] = time.time

    # ---- gathering -------------------------------------------------------------
    def gather(self) -> GraphInputs:
        """Snapshot every source once. A failing source is an empty section
        plus a sentence in `source_errors` - never a crash, never a guess."""
        inp = GraphInputs(now_ms=int(self.clock() * 1000))
        errors = inp.source_errors
        ctx = self.ctx

        def attempt(label: str, fn: Callable[[], Any]) -> Any:
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - a source down is a caveat
                errors.append(f"Intel source {label} could not be read "
                              f"({type(exc).__name__}: {_trunc(exc, 120)}).")
                return None

        snap = attempt("snapshot", lambda: _call(ctx, "snapshot"))
        if isinstance(snap, dict):
            inp.vehicles = [dict(v) for v in _l(snap.get("vehicles")) if isinstance(v, dict)]
            inp.missions = [m for m in _l(snap.get("missions")) if isinstance(m, dict)]
            inp.contacts = [c for c in _l(snap.get("contacts")) if isinstance(c, dict)]
            inp.feeds = _d(snap.get("feeds"))
            inp.sim_state = snap.get("sim_state") if isinstance(snap.get("sim_state"),
                                                                str) else None
        intel = attempt("mission feed", lambda: _call(ctx, "intel"))
        if intel is not None:
            inp.per_vehicle = _copy_mapping(getattr(intel, "per_vehicle", None))
            if not inp.feeds and hasattr(intel, "feeds_dict"):
                inp.feeds = attempt("feed status", intel.feeds_dict) or {}
        inp.tracks = [r for r in _l(attempt("track rows", lambda: _call(ctx, "track_rows")))
                      if isinstance(r, dict)]
        thr = attempt("theaters", lambda: _call(ctx, "theaters"))
        if isinstance(thr, dict):
            inp.theaters = [t for t in _l(thr.get("theaters")) if isinstance(t, dict)]
            inp.active_theater = _d(thr.get("active"))
        elif callable(getattr(ctx, "active_theater", None)):
            inp.active_theater = _d(attempt("active theater", ctx.active_theater))
        inp.alarms = _l(attempt("alarms", lambda: _call(ctx, "recent_events", 100)))
        inp.mission_details = _d(attempt("mission details",
                                         lambda: _call(ctx, "mission_details")))
        inp.threat_rings = _d(attempt("threat rings", lambda: _call(ctx, "threat_rings")))
        geo = attempt("safety resource", lambda: _call(ctx, "geofence_doc"))
        inp.geofence = geo if isinstance(geo, dict) else None
        if self.server is not None:
            self._gather_server(inp, attempt)
        return inp

    def _gather_server(self, inp: GraphInputs, attempt: Callable) -> None:
        srv = self.server
        if not _d(inp.active_theater).get("known"):
            # The bridge learns the theater from loop C's first read of
            # uav://safety/geofence, so right after boot (and in any host whose
            # loops are not running) it is honestly unknown. The in-process
            # server IS the authority that resource reports, so ask it
            # directly - same block, same checks, a source that says so.
            active = attempt("in-process theater",
                             lambda: _server_active_theater(srv, inp.now_ms))
            if isinstance(active, dict) and active.get("known"):
                inp.active_theater = active
        reports = attempt("reports", lambda: _copy_mapping(getattr(srv, "reports", None)))
        inp.reports = reports if isinstance(reports, dict) else {}
        rd = attempt("real-data status", srv.real_data_status) \
            if callable(getattr(srv, "real_data_status", None)) else None
        inp.real_data = rd if isinstance(rd, dict) else None

        def status() -> dict:
            out: dict[str, dict] = {}
            monitors = _copy_mapping(getattr(srv, "monitors", None))
            for name, mon in monitors.items():
                row: dict[str, Any] = {}
                link = getattr(mon, "link", None)
                if link is not None and callable(getattr(link, "to_dict", None)):
                    ld = link.to_dict()
                    row["link"] = {k: ld.get(k) for k in ("state", "action", "down_for_s",
                                                          "loal_count", "plan")}
                    down_since = _num(getattr(link, "down_since", None))
                    if down_since is not None:
                        # The link machine's clock is time.monotonic(); the
                        # graph speaks wall-clock ms.
                        row["link"]["down_since_ms"] = round(
                            (time.time() - time.monotonic() + down_since) * 1000)
                fuel = getattr(mon, "fuel", None)
                if fuel is not None:
                    row["bingo"] = fuel.bingo.to_dict()
                tasking = getattr(srv, "tasking", None)
                # Only queues that already exist: `status()` -> `queue_for()`
                # MINTS a queue (and lazily starts the tasking thread) for an
                # unknown name, and a read must not do that.
                known = (tasking.vehicles() if tasking is not None
                         and callable(getattr(tasking, "vehicles", None)) else [])
                if name in known and callable(getattr(tasking, "status", None)):
                    q = tasking.status(name)
                    row["queue"] = {"state": q.get("state"), "queued": q.get("queued"),
                                    "current": q.get("current")}
                row["mission_status"] = _d(getattr(srv, "mission_flags", None)).get(name)
                out[str(name)] = row
            return out

        vs = attempt("vehicle safety state", status)
        inp.vehicle_status = vs if isinstance(vs, dict) else {}

    # ---- graph -----------------------------------------------------------------
    def _builder(self, scope: str) -> _GraphBuilder:
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(scope)
            if hit and now - hit[0] <= self.cache_ttl_s:
                return hit[1]
        b = _GraphBuilder(self.gather(), scope)
        b.build()
        with self._lock:
            self._cache[scope] = (now, b)
        return b

    def graph(self, scope: str = "theater") -> dict:
        return self._builder(scope).graph

    # ---- search ----------------------------------------------------------------
    def search(self, query: str, types: list[str] | None = None,
               limit: int = 25) -> list[dict]:
        b = self._builder("all")
        return search_nodes(b.graph, query, types, limit, aliases=b.alias)

    # ---- events ----------------------------------------------------------------
    def recent_events(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        out = _call(self.ctx, "recent_events", limit)
        if isinstance(out, list):
            return out
        hub = getattr(self.ctx, "hub", None)
        if hub is not None and callable(getattr(hub, "recent", None)):
            return list(hub.recent(limit))
        return []

    # ---- overview --------------------------------------------------------------
    def overview(self) -> dict:
        """Compact situation for the analyst (<= OVERVIEW_MAX_BYTES)."""
        b = self._builder("theater")
        g = b.graph
        nodes = g["nodes"]
        vehicles = [{"id": n["id"], "name": n["label"], "status": n["status"],
                     **{k: n["attrs"].get(k) for k in (
                         "fuel_pct", "bingo_fuel_pct", "margin_pct", "bingo_latched",
                         "landed", "mission", "link", "agl_is_real")
                        if n["attrs"].get(k) is not None}}
                    for n in nodes if n["type"] == "vehicle"]
        missions = [{"id": n["id"], "kind": n["attrs"].get("kind"),
                     "vehicle": n["attrs"].get("vehicle"), "phase": n["attrs"].get("phase"),
                     "progress_pct": n["attrs"].get("progress_pct"), "status": n["status"]}
                    for n in nodes if n["type"] == "mission"]
        by_cat: dict[str, int] = {}
        by_conf: dict[str, int] = {}
        by_threat: dict[str, int] = {}
        for c in b.kept:
            by_cat[c.rep.category or "unclassified"] = by_cat.get(
                c.rep.category or "unclassified", 0) + 1
            by_conf[c.rep.confidence or "unrated"] = by_conf.get(
                c.rep.confidence or "unrated", 0) + 1
            w = _threat_word(c.threat)
            by_threat[w] = by_threat.get(w, 0) + 1
        top = sorted(b.kept, key=lambda c: (-c.salience, c.rep.id))[:5]
        alarms = [{"seq": n["attrs"].get("seq"), "kind": n["attrs"].get("kind"),
                   "severity": n["attrs"].get("severity"),
                   "message": _trunc(n["subtitle"], 100), "atMs": n["ts_ms"]}
                  for n in nodes if n["type"] == "alarm"][-5:]
        meta = g["meta"]
        # Graph ids for [[type:id]] chips, next to the bare theater id: live,
        # the analyst wrote `[[theater:default]]` (a chip nothing resolves).
        theater = dict(g["theater"])
        if theater.get("known") and theater.get("id"):
            theater["graph_id"] = f"{TYPE_PREFIX['theater']}:{theater['id']}"
        out = {
            "generated_at_ms": g["generated_at_ms"],
            "theater": theater,
            "sim": b.inp.sim_state,
            "vehicles": vehicles,
            "missions": missions,
            "contacts": {
                "in_theater": len(b.kept), "by_category": by_cat,
                "by_confidence": by_conf, "by_threat": by_threat,
                "stale": meta["stale_contacts"],
                "duplicates_collapsed": meta["duplicates_collapsed"],
                "out_of_theater": meta["out_of_theater"],
                "top": [{"id": c.node_id, "label": _trunc(c.rep.platform, 60),
                         "threat": _threat_word(c.threat),
                         "confidence": c.rep.confidence or None} for c in top],
            },
            "alarms": alarms,
            "feeds_down": sorted(k for k, v in meta["feeds"].items()
                                 if v.get("status") == "critical"),
            "feeds_degraded": sorted(k for k, v in meta["feeds"].items()
                                     if v.get("status") == "warn"),
            "caveats": [_trunc(c, 220) for c in meta["caveats"]],
        }
        for trim in range(8):
            if json_size(out) <= OVERVIEW_MAX_BYTES:
                return out
            if trim == 0:
                out["caveats"] = out["caveats"][:6]
            elif trim == 1:
                out["contacts"]["top"] = out["contacts"]["top"][:3]
                out["alarms"] = out["alarms"][-3:]
            elif trim == 2:
                out["caveats"] = [_trunc(c, 120) for c in out["caveats"][:4]]
            elif trim == 3:
                cats = sorted(out["contacts"]["by_category"].items(), key=lambda x: -x[1])
                out["contacts"]["by_category"] = dict(cats[:8])
            elif trim == 4:
                if len(out["vehicles"]) > 8:
                    out["vehicles_omitted"] = len(out["vehicles"]) - 8
                    out["vehicles"] = out["vehicles"][:8]
                if len(out["missions"]) > 8:
                    out["missions_omitted"] = len(out["missions"]) - 8
                    out["missions"] = out["missions"][:8]
            else:
                return fit_to_budget(out, OVERVIEW_MAX_BYTES)
        return out

    # ---- entity ----------------------------------------------------------------
    def entity(self, entity_id: str, *, max_bytes: int = ENTITY_MAX_BYTES) -> dict | None:
        """Full detail for one graph entity (<= `max_bytes`), or None if unknown.

        Resolved against the scope="all" graph, so an out-of-theater contact
        or a collapsed duplicate's id is still inspectable (a duplicate id
        resolves to the contact it was folded into, and says so).
        """
        b = self._builder("all")
        nid = b.resolve(entity_id)
        if nid is None:
            return None
        node = b.nodes[nid]
        builder = {
            "track": self._track_entity, "vehicle": self._vehicle_entity,
            "mission": self._mission_entity, "theater": self._theater_entity,
            "poi": self._poi_entity, "unit": self._unit_entity,
            "equipment": self._equipment_entity, "report": self._report_entity,
            "alarm": self._alarm_entity, "feed": self._feed_entity,
        }[node["type"]]
        fields, provenance, raw = builder(b, nid, node)
        related = []
        for e in b.edges:
            if e["a"] == nid or e["b"] == nid:
                other = e["b"] if e["a"] == nid else e["a"]
                o = b.nodes.get(other, {})
                related.append({"id": other, "type": o.get("type"), "label": o.get("label"),
                                "kind": e["kind"], "dir": "out" if e["a"] == nid else "in"})
        out = {"id": nid, "type": node["type"], "label": node["label"],
               "subtitle": node["subtitle"], "status": node["status"],
               "requested_id": entity_id if entity_id != nid else None,
               "fields": fields, "provenance": provenance,
               "related": related[:80],
               "caveats": b.caveats[:6]}
        if len(related) > 80:
            out["related_omitted"] = len(related) - 80
        if raw is not None:
            out["raw"] = raw
        out = {k: v for k, v in out.items() if v is not None}
        return fit_to_budget(out, max_bytes)

    # per-type detail: each returns (fields, provenance, raw|None)
    def _track_entity(self, b: _GraphBuilder, nid: str, node: dict):
        c = b.cluster_by_node[nid]
        t = c.rep
        row = t.row
        age = None if t.last_seen is None else max(0.0, b.now_s - t.last_seen)
        loc = _d(row.get("location"))
        rings = _d(b.inp.threat_rings.get(t.id))
        fields = {
            "track_id": t.id, "platform": t.platform, "category": t.category or None,
            "ob_class": t.ob_class or None, "equipment_name": t.equipment_name or None,
            "confidence": t.confidence or None, "confidence_score": t.confidence_score,
            "sightings": t.sightings, "threat": _threat_word(c.threat),
            "threat_from_duplicate": c.threat_from,
            "lat": t.lat, "lon": t.lon, "alt_m": t.alt_m,
            "first_seen_ms": int(t.first_seen * 1000) if t.first_seen is not None else None,
            "last_seen_ms": int(t.last_seen * 1000) if t.last_seen is not None else None,
            "age_s": None if age is None else int(age), "custody_lapsed": c.stale,
            "theater": c.theater, "in_active_theater": c.in_active or None,
            "outside_ao": c.outside_ao or None, "unit": c.unit_id,
            "origin_run": t.run or None,
            "duplicates": [{"track_id": m.id, "run": m.run, "sightings": m.sightings,
                            "last_seen_ms": int(m.last_seen * 1000) if m.last_seen else None,
                            "confidence": m.confidence or None,
                            "threat": m.threat or NOT_ASSESSED}
                           for m in c.members if m is not t],
            "threat_rings_m": {k: v for k, v in rings.items() if v} or None,
            "salute": {k: row.get(k) for k in ("size", "activity", "location", "unit", "time",
                                               "equipment") if k in row} or (
                _d(row.get("salute")) or None),
        }
        detail = self._live_threat(t.id)
        if detail:
            fields["threat_detail"] = detail
        conf = _d(row.get("confidence"))
        provenance = {
            "source": "compact /snapshot contact row" if t.compact
            else "uav_list_tracks SALUTE row (M11 persistent track store)",
            "location_source": _s(loc.get("source")) or None,
            "observer_position_used": loc.get("observer_position_used"),
            "observer": (f"{row.get('observer')!s} - the store does not record WHICH "
                         "vehicle observed it") if row.get("observer") else None,
            "position": ("last fix, custody lapsed - not a current position" if c.stale
                         else "last fix"),
            "confidence_basis": [
                {k: e.get(k) for k in ("element", "value", "score", "contribution")}
                for e in _l(conf.get("evidence")) if isinstance(e, dict)] or None,
            "threat_basis": (
                "bridge area assessment (uav_assess_threat summary THREATREP, run when the "
                "track roster last changed, against the first vehicle's position then)"
                if c.threat else "not assessed: outside the assessed area or no assessment "
                "served - this is not a 'none' finding"),
            "duplicates_basis": (f"{len(c.members) - 1} track(s) of the same equipment "
                                 f"within {DUPLICATE_RADIUS_M:.0f} m from other runs were "
                                 "folded into this one (the freshest)") if len(c.members) > 1
            else None,
        }
        return fields, provenance, row

    def _live_threat(self, track_id: str) -> dict | None:
        """Per-track threat math NOW, when the server is in-process (pure read)."""
        srv = self.server
        tracks = getattr(srv, "tracks", None) if srv is not None else None
        if tracks is None or not callable(getattr(tracks, "get", None)):
            return None
        try:
            from .threat import assess_track
            track = tracks.get(track_id)
            if track is None:
                return None
            teles = _copy_mapping(getattr(srv, "_last_tele", None))
            vehicle = min(teles) if teles else None
            observer = srv._observer(teles[vehicle]) if vehicle else None
            a = assess_track(track, observer, None, getattr(srv, "pol", None))
        except Exception:  # noqa: BLE001 - an optional enrichment, never fatal
            return None
        keep = ("threat_score", "threat_level", "capability", "intent", "in_envelope",
                "envelope_m", "observer_range_m", "rationale", "sensor_posture")
        out = {k: a.get(k) for k in keep}
        out["basis"] = (f"computed for this view against {vehicle}'s last telemetry"
                        if vehicle else "computed for this view with no observer position")
        out["isr_only"] = True
        return out

    def _vehicle_entity(self, b: _GraphBuilder, nid: str, node: dict):
        name = nid[len("veh:"):]
        row = {k: v for k, v in b.vehicle_rows.get(name, {}).items() if k != "_telemetry"}
        status_x = _d(b.inp.vehicle_status.get(name))
        fuel, bingo = _num(row.get("fuel_pct")), _num(row.get("bingo_fuel_pct"))
        fields = {
            "name": name,
            "position": {"lat": _num(row.get("latitude")), "lon": _num(row.get("longitude")),
                         "alt_hae_m": _num(row.get("alt_hae")),
                         "alt_msl_m": _num(row.get("alt_msl")),
                         "speed_mps": _num(row.get("speed_ms")),
                         "heading_deg": _num(row.get("heading_deg")),
                         "landed": node["attrs"].get("landed")},
            "fuel": {"fuel_pct": fuel, "bingo_fuel_pct": bingo,
                     "margin_pct": None if fuel is None or bingo is None else round(
                         fuel - bingo, 2),
                     "eta_to_bingo_s": _num(row.get("eta_to_bingo_s")),
                     "bingo_latched": node["attrs"].get("bingo_latched"),
                     "bingo_latch": status_x.get("bingo"),
                     "fuel_source": row.get("fuel_source")},
            "agl": {"alt_agl_m": _num(row.get("alt_agl_m")),
                    "alt_agl_is_real": row.get("alt_agl_is_real"),
                    "alt_agl_source": row.get("alt_agl_source"),
                    "alt_agl_reason": row.get("alt_agl_reason"),
                    "alt_agl_launch_datum_m": _num(row.get("alt_agl_launch_datum_m")),
                    "launch_datum_check": row.get("alt_agl_launch_datum_check"),
                    "measured_age_ms": row.get("alt_agl_measured_age_ms")},
            "link": status_x.get("link"),
            "link_lost_since_ms": node["attrs"].get("link_lost_since_ms"),
            "queue": status_x.get("queue"),
            "mission": row.get("mission") or None,
            "track_id": row.get("track_id") or None,
            "mission_status": status_x.get("mission_status"),
        }
        provenance = {
            "telemetry": "bridge loop A (10 Hz) enriched from loop C" if row.get(
                "timestamp_ms") else "no telemetry row: mission-feed state only",
            "fuel_source": row.get("fuel_source"),
            "agl_is_measured": row.get("alt_agl_is_real"),
            "agl_note": None if row.get("alt_agl_is_real") else (
                "AGL is height above the LAUNCH DATUM, not terrain clearance"),
            "datum_source": row.get("datum_source"),
            "datum_degraded": row.get("datum_degraded"),
            "telemetry_error": row.get("telemetry_error"),
            "stale_ms": row.get("stale_ms"),
            "link_source": "in-process safety monitor" if status_x.get("link") else
            "unknown: no in-process server to read the link machine from",
        }
        return fields, provenance, row

    def _mission_entity(self, b: _GraphBuilder, nid: str, node: dict):
        mid = nid[len("msn:"):]
        row = b.mission_rows.get(mid, {})
        detail = dict(_d(_d(b.inp.mission_details).get(mid)))
        wps = _l(detail.pop("waypoints", None))
        meta = dict(_d(detail.get("meta")))
        started = _num(detail.get("started"))
        fields = {
            **row,
            "started_ms": int(started * 1000) if started is not None else None,
            "task_id": detail.get("task_id"), "state": detail.get("state"),
            "speed_mps": detail.get("speed_mps"),
            "gate": detail.get("gate"), "coverage": detail.get("coverage"),
            "phases": detail.get("phases"), "warnings": detail.get("warnings"),
            "truncated": detail.get("truncated"),
            "lost_link_plan": detail.get("lost_link_plan"),
            "meta": meta or None,
            "waypoints": {"count": len(wps), "first": wps[:5],
                          "last": wps[-1:] if len(wps) > 5 else []} if wps else None,
        }
        provenance = {
            "row": "bridge mission feed (uav_task_status + mission_status)",
            "detail": "uav://mission/{id} (cached once per mission)" if detail
            else "uav://mission/{id} not cached: no route, gate or coverage detail",
            "timestamps": ("start from uav://mission/{id}.started" if started is not None
                           else "mission rows carry no timestamps; none is shown"),
            "coverage_basis": row.get("coverage_basis"),
        }
        return fields, provenance, None

    def _theater_entity(self, b: _GraphBuilder, nid: str, node: dict):
        tid = nid[len("thr:"):]
        row = dict(_d(b.theater_rows.get(tid)))
        active = tid == b.active_id
        fence = _d(b.inp.geofence) if active else {}
        envelope = {k: fence.get(k) for k in (
            "geofence", "ceiling_m_agl", "min_agl_m", "max_speed_mps", "geofence_warn_m",
            "home", "home_datums", "roe", "isr_only", "mission_flags", "bingo",
            "restart_recovery", "lost_link_plan") if k in fence}
        fields = {"id": tid, **row, "active": active,
                  "active_block": b.active if active else None,
                  "envelope": envelope or None}
        provenance = {
            "table": "theaters.py static table" if row else "not in this build's table",
            "active_source": _s(b.active.get("source")) or None if active else None,
            "active_learned_at_ms": _int(b.active.get("at_ms")) if active else None,
            "real_data": row.get("real_data"),
            "envelope_source": "uav://safety/geofence (read once at boot)" if envelope
            else None,
        }
        return fields, provenance, None

    def _poi_entity(self, b: _GraphBuilder, nid: str, node: dict):
        tid, p = b.poi_rows[nid]
        name = _s(_d(p).get("name"))
        near = [e["a"] for e in b.edges if e["b"] == nid and e["kind"] == "near"]
        fields = {"name": name, "theater": tid, "lat": _num(_d(p).get("lat")),
                  "lon": _num(_d(p).get("lon")), "radius_m": NEAR_POI_M,
                  "near_contacts": near}
        pol = getattr(self.server, "pol", None) if self.server is not None else None
        if pol is not None and callable(getattr(pol, "get", None)):
            try:
                base = pol.get(name)
                if base is not None:
                    fields["pattern_of_life"] = {"baseline": base.to_dict(),
                                                 "deviation": pol.deviation(name)}
            except Exception:  # noqa: BLE001 - optional enrichment
                fields["pattern_of_life"] = None
        provenance = {"source": "theaters.py POI table",
                      "pattern_of_life": "in-process pattern-of-life store (M12)"
                      if fields.get("pattern_of_life") else
                      "not available: no baseline for this POI or no in-process server"}
        return fields, provenance, None

    def _unit_entity(self, b: _GraphBuilder, nid: str, node: dict):
        members = b.units.get(nid, [])
        fields = {**node["attrs"],
                  "members": [{"id": m.node_id, "label": m.rep.platform,
                               "confidence": m.rep.confidence or None,
                               "threat": _threat_word(m.threat), "lat": m.rep.lat,
                               "lon": m.rep.lon, "duplicates_folded": len(m.members) - 1}
                              for m in members]}
        provenance = {"basis": node["attrs"].get("basis"),
                      "note": "derived in this graph from the deduplicated contacts; the "
                              "SALUTE 'size' field on the raw rows counts duplicates"}
        return fields, provenance, None

    def _equipment_entity(self, b: _GraphBuilder, nid: str, node: dict):
        ob_class = nid[len("ob:"):]
        ob = OB_LIBRARY.get(ob_class)
        tracks = [c.node_id for c in b.kept if c.rep.ob_class == ob_class]
        fields = {"ob_class": ob_class, "library": ob.to_dict() if ob else None,
                  "contacts": tracks}
        provenance = {"source": "targets.OB_LIBRARY (order-of-battle reference data)"
                      if ob else "class not in this build's OB library"}
        return fields, provenance, None

    def _report_entity(self, b: _GraphBuilder, nid: str, node: dict):
        rep = b.report_rows[nid]
        fmt = _s(rep.get("format"))
        head = {k: v for k, v in rep.items() if not isinstance(v, (list, dict))}
        fields: dict[str, Any] = {"header": head}
        if fmt == "THREATREP":
            fields["assessments"] = [
                {k: _d(a).get(k) for k in ("track_id", "ob_class", "threat_level",
                                           "threat_score", "in_envelope", "envelope_m",
                                           "observer_range_m")}
                | {"sensor_posture": _d(_d(a).get("sensor_posture")).get("code")}
                for a in _l(rep.get("assessments"))]
            fields["omitted"] = _l(rep.get("omitted"))
            fields["area_polygon"] = rep.get("area_polygon")
        else:
            fields["mission_summary"] = rep.get("mission_summary")
            fields["coverage"] = rep.get("coverage")
            fields["gaps"] = rep.get("gaps")
            fields["confidence_summary"] = rep.get("confidence_summary")
            fields["by_category"] = rep.get("by_category")
            fields["contacts"] = [
                {"track_id": _d(c).get("track_id"), "ob_class": _d(c).get("ob_class"),
                 "confidence_level": _d(c).get("confidence_level"),
                 "size": _d(_d(c).get("size")).get("text"),
                 "activity": _d(_d(c).get("activity")).get("text")}
                for c in _l(rep.get("contacts"))]
            fields["contacts_omitted"] = _l(rep.get("contacts_omitted"))
            fields["sensor_conditions"] = rep.get("sensor_conditions")
            fields["loal_events"] = rep.get("loal_events")
        provenance = {"source": "in-process report store (uav://reports/{id})",
                      "detail": rep.get("detail"), "truncation": rep.get("truncation")}
        return fields, provenance, rep

    def _alarm_entity(self, b: _GraphBuilder, nid: str, node: dict):
        a = b.alarm_rows[nid]
        return dict(a), {"source": "bridge alarm lane (edge-derived from server levels)",
                         "note": "a condition that raises and clears inside one poll is "
                                 "not seen"}, None

    def _feed_entity(self, b: _GraphBuilder, nid: str, node: dict):
        fields = dict(b.feed_rows[nid])
        fields.pop("warn", None)
        if nid == "feed:real_data":
            fields["real_data"] = b._real_data()
            source = ("in-process server real_data_status()" if b.inp.real_data is not None
                      else "uav://safety/geofence real_data block (as of boot)")
        elif nid == "feed:theater":
            fields["active_block"] = b.active
            source = "bridge active theater (uav://safety/geofence theater block)"
        elif nid == "feed:sim":
            source = "bridge AirSim adapter state"
        else:
            source = "bridge loop C feed status (/snapshot.feeds)"
        return fields, {"source": source}, None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def intel_router(service: IntelService, auth: Callable) -> Any:
    """GET /intel/graph, /intel/entity/{id}, /intel/events/recent (CONTRACT §3).

    `auth` is a FastAPI dependency (the host's bearer check). Handlers are
    sync `def`s so graph building runs in the threadpool, never on the loop
    that serves SSE and the in-process MCP server.
    """
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/intel", dependencies=[Depends(auth)])

    # Error bodies are top-level `{error, ...}` (CONTRACT §3), the same shape
    # the chat routes answer with - not FastAPI's `{detail: ...}` wrapper.
    @router.get("/graph")
    def intel_graph(scope: str = "theater"):
        if scope not in SCOPES:
            return JSONResponse({"error": "invalid_scope", "scope": scope,
                                 "allowed": list(SCOPES)}, status_code=422)
        return service.graph(scope)

    @router.get("/entity/{entity_id:path}")
    def intel_entity(entity_id: str):
        out = service.entity(entity_id)
        if out is None:
            return JSONResponse({"error": "unknown_entity", "id": entity_id},
                                status_code=404)
        return out

    @router.get("/events/recent")
    def intel_events(limit: int = 50):
        return {"events": service.recent_events(max(1, min(int(limit), 100)))}

    return router
