"""The simulated wargame in the intel graph and on the map (M14a; WG v2 §3.2,
§3.3, §5.2.12 B10; key unit tests of B10).

Every row here is a SIMULATION: scenario units the wargame placed, notional
adjudications, `simulated: true`. What is pinned:

* rows per view: the Blue view hides red truth (red forces, red axes,
  `correlated`, the truth-only edges, a red attacker, an outcome before
  battle damage assessment); the Umpire view (`truth`) shows it;
* the caps (60 forces, the newest 24 engagements and 12 vectors) and
  `meta.wargame` (inactive, active, Blue-view counts, pending, omitted, error);
* the byte budget at maximum load and the overlay kinds (and their caps);
* scenario contacts carry generic labels, never an order-of-battle name, and
  stay out of the equipment and unit roll-ups; their inspector withholds OB text;
* the after-action review's `format` and `markdown`;
* `assert_no_real_system_tokens` over graph, entity and overlay rows;
* no wargame edge touches a real place; the analyst's reads never see truth.

The engine is the real `WargameEngine` over a duck-typed server with an
injected session (thread off): no sim, no port, no network.
"""
from __future__ import annotations

import math
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from godseye_uav import intel_graph as ig
from godseye_uav import intel_overlay as ov
from godseye_uav import intel_scenario as isc
from godseye_uav import targets, theater_tools
from godseye_uav import wargame as wg
from godseye_uav import wargame_aar as aar
from godseye_uav import wargame_tables as T
from godseye_uav.targets import OB_LIBRARY, Track, salute_report
from support.wg_tokens import assert_no_real_system_tokens, find_real_system_tokens
from test_intel_graph import DEFAULT, NOW_MS, _auth, live_inputs, vehicle_row

SID = "WG-abc123"
WALL_S = time.time()
XSS = "<img src=x onerror=alert(1)>"
BIDI = "\u202eevil\u202c"
WG_EDGES = {"attacks", "launched_by", "along", "axis", "ingress", "threatens", "correlates"}


def at(dn_m: float, de_m: float) -> tuple[float, float]:
    """A point `dn_m` north and `de_m` east of the default home (flat, < 3 km)."""
    return (DEFAULT.home_lat + dn_m / 111_320.0,
            DEFAULT.home_lon + de_m / (111_320.0 * math.cos(math.radians(DEFAULT.home_lat))))


class Tracks:
    """`srv.tracks`: what the engine reads (`get`)."""

    def __init__(self):
        self.by_id: dict[str, Track] = {}

    def get(self, tid):
        return self.by_id.get(tid)


class Scn:
    """A running session on the real engine, built by hand (no sim, no port)."""

    def __init__(self, *, reveal_red: bool = False):
        self.srv = SimpleNamespace(
            tracks=Tracks(), targets={}, _last_tele={}, time_scale=1.0, backend=None,
            vehicles_lost={}, theater=DEFAULT, theater_epoch=2, theater_mismatch=None,
            sites=None, airframe_id="quad_suas_electric")
        self.eng = wg.WargameEngine(self.srv, run_thread=False)
        self.srv.wargame = self.eng
        self.s = wg.Session(
            session_id=SID, num=1, started_at_ms=int(WALL_S * 1000) - 600_000, seed=4417,
            red_engages=True, reveal_red=reveal_red, theater_id="default", theater_epoch=2,
            exclusion=None, lattice=SimpleNamespace(undulation_m=0.0, covered=False,
                                                    cell_m=150.0),
            ao={"center": tuple(DEFAULT.center()), "half_extent_m": 2940.0,
                "half_diagonal_m": 4158.0},
            units={}, pendings={}, engagements=[], events=[], object_names=[], track_ids=[],
            lost={}, vectors={}, caveats=[wg.SESSION_CAVEAT])
        self.eng._session = self.s
        self.count: dict[tuple[str, str], int] = {}
        self.seq = 0

    # ---- units and contacts ------------------------------------------------
    def unit(self, side: str, wg_class: str, dn: float, de: float, *, objective=None,
             state: str = "active", designator: str | None = None) -> wg.Unit:
        cls = T.CLASSES[wg_class]
        n = self.count[(side, cls.prefix_slug)] = self.count.get((side, cls.prefix_slug), 0) + 1
        lat, lon = at(dn, de)
        obj = f"{cls.ob_key}_1{len(self.s.object_names) + 1:03d}" if side == "red" else None
        u = wg.Unit(unit_id=T.unit_id(side, cls.prefix_slug, n), side=side, wg_class=wg_class,
                    designator=designator or T.designator(side, cls.prefix_slug, n),
                    lat=lat, lon=lon, alt_hae_m=0.0, alt_msl_m=10.0, object_name=obj,
                    spawned_at=WALL_S - 500, state=state, ammo=cls.ammo,
                    objective=objective.unit_id if objective else None)
        self.s.units[u.unit_id] = u
        if obj:
            self.s.object_names.append(obj)
            self.eng._names = frozenset(self.s.object_names)
            self.srv.targets[obj] = {"provenance": "scenario", "session_id": SID,
                                     "side": "red", "unit_id": u.unit_id}
        return u

    def track(self, u: wg.Unit, *, off_m: float = 30.0, first_seen: float | None = None,
              confidence_sightings: int = 3) -> Track:
        """A sensed track of red unit `u` (passes the provenance gate)."""
        k = len(self.srv.tracks.by_id) + 1
        cls = T.CLASSES[u.wg_class]
        t = Track(track_id=f"TRK-SCNSCNSCNSCN-{k:04d}", name=u.object_name,
                  category=OB_LIBRARY[cls.ob_key].category, lat=u.lat + off_m / 111_320.0,
                  lon=u.lon, alt_m=10.0,
                  first_seen=WALL_S - 400 if first_seen is None else first_seen,
                  last_seen=WALL_S - 10, sightings=confidence_sightings,
                  ob_class=cls.ob_key, scenario=True, origin_run="SCNSCNSCNSCN")
        self.srv.tracks.by_id[t.track_id] = t
        self.s.track_ids.append(t.track_id)
        return t

    def rows(self) -> list[dict]:
        """The bridge's `uav_list_tracks` rows (the real SALUTE shape)."""
        tracks = list(self.srv.tracks.by_id.values())
        return [salute_report(t, peers=tracks, now=WALL_S) for t in tracks]

    # ---- engagements and vectors ---------------------------------------------
    def _eid(self) -> str:
        self.seq += 1
        return f"{SID}-E{self.seq}"

    def strike(self, shooter: wg.Unit, u: wg.Unit, t: Track, *, phase="adjudicated",
               outcome="damaged", looks=0, at_ms=None) -> wg.Engagement:
        at_ms = at_ms or int(WALL_S * 1000) - 60_000 + self.seq
        eid = self._eid()
        e = wg.Engagement(
            engagement_id=eid, kind="blue_strike", phase=phase,
            attacker=f"frc:{shooter.unit_id}", target=f"trk:{t.track_id}",
            target_label=T.label_for_ob(t.ob_class), vector=None,
            p_notional={"effect": 0.62, "destroyed": 0.2, "damaged": 0.25, "suppressed": 0.17},
            inputs=["Range 3.2 km of 20.0 km", "Perceived as air-defence guns, probable"],
            outcome=None if phase != "adjudicated" else outcome,
            consequence="red_effect" if phase == "adjudicated" else "none", seed=4417,
            draw=self.seq, proposed_at_ms=at_ms - 5000,
            adjudicated_at_ms=at_ms if phase == "adjudicated" else None,
            fired_at_ms=at_ms if phase == "adjudicated" else None,
            bda={"state": "damaged" if looks else "none", "looks": looks,
                 "last_look_ms": at_ms + 1000 if looks else None},
            unit_id=u.unit_id, attacker_label=shooter.designator, track_id=t.track_id,
            target_point=(t.lat, t.lon), attacker_point=(shooter.lat, shooter.lon),
            stream="blue")
        self.s.engagements.append(e)
        if phase in ("proposed", "authorized"):
            args = {"pending_id": eid, "shooter_id": shooter.unit_id,
                    "target_track_id": t.track_id}
            self.s.pendings[eid] = wg.Pending(
                pending_id=eid, shooter_id=shooter.unit_id, target_track_id=t.track_id,
                unit_id=u.unit_id, args=args, created_ms=at_ms,
                expires_at_ms=at_ms + 600_000, state=phase)
        return e

    def shot(self, red: wg.Unit, vehicle: str = "Drone1", *, outcome="destroyed",
             at_ms=None) -> wg.Engagement:
        """A red air-defence shot at a drone; a kill downs it (vehicles_lost)."""
        at_ms = at_ms or int(WALL_S * 1000) - 30_000 + self.seq
        e = wg.Engagement(
            engagement_id=self._eid(), kind="red_shot", phase="adjudicated",
            attacker=f"frc:{red.unit_id}", target=f"veh:{vehicle}", target_label=vehicle,
            vector=None, p_notional={"effect": 0.3, "destroyed": 0.3, "damaged": 0.0,
                                     "suppressed": 0.0},
            inputs=["Slant range 1.2 km of 2.0 km", "60 m above the unit, ceiling 1000 m"],
            outcome=outcome, consequence="own_loss" if outcome == "destroyed" else "none",
            seed=4417, draw=self.seq, proposed_at_ms=at_ms, adjudicated_at_ms=at_ms,
            unit_id=red.unit_id, attacker_label=red.designator,
            target_point=at(1500, 900), attacker_point=(red.lat, red.lon), stream="red")
        self.s.engagements.append(e)
        if outcome == "destroyed":
            self.s.lost[vehicle] = {"by": red.designator, "at_ms": at_ms}
            self.srv.vehicles_lost[vehicle] = {"by": red.designator, "at_ms": at_ms}
        return e

    def ground(self, red: wg.Unit, blue: wg.Unit, *, outcome="suppressed") -> wg.Engagement:
        at_ms = int(WALL_S * 1000) - 20_000 + self.seq
        e = wg.Engagement(
            engagement_id=self._eid(), kind="red_ground", phase="adjudicated",
            attacker=f"frc:{red.unit_id}", target=f"frc:{blue.unit_id}",
            target_label=blue.designator, vector=None,
            p_notional={"effect": 0.2, "destroyed": 0.06, "damaged": 0.08, "suppressed": 0.06},
            inputs=["Range 1.1 km of 3.0 km"], outcome=outcome,
            consequence="own_damage" if outcome in ("damaged", "suppressed") else "none",
            seed=4417, draw=self.seq, proposed_at_ms=at_ms, adjudicated_at_ms=at_ms,
            unit_id=red.unit_id, attacker_label=red.designator,
            target_point=(blue.lat, blue.lon), attacker_point=(red.lat, red.lon), stream="red")
        self.s.engagements.append(e)
        return e

    def corridor(self, t: Track, vehicle: str = "Drone1", *, points: int = 40,
                 legs: int = 12) -> dict:
        """A planned corridor to track `t`, in `_store_corridor`'s shape."""
        self.s.cor_seq += 1
        a, b = at(-200, -200), (t.lat, t.lon)
        path = [[a[0] + (b[0] - a[0]) * k / (points - 1), a[1] + (b[1] - a[1]) * k / (points - 1)]
                for k in range(points)]
        vec = {"id": f"cor-{self.s.cor_seq}", "kind": "corridor", "side": "blue",
               "from": f"veh:{vehicle}", "from_label": vehicle, "to": f"trk:{t.track_id}",
               "to_label": T.label_for_ob(t.ob_class), "from_point": list(a),
               "to_point": list(b), "bearing_deg": 41.5, "length_m": 2412.3, "alt_band": "low",
               "alt_agl_m": 60.0, "corridor_m": 150.0, "speed_mps": 10.0, "eta_s": 241.2,
               "exposure_s": 42.0, "p_survive": 0.91,
               "straight": {"exposure_s": 160.0, "p_survive": 0.7, "length_m": 2000.0},
               "delta_exposure_s": 118.0, "delta_length_m": 412.3,
               "legs": [{"exposure": ("low", "moderate", "high")[k % 3], "exposure_s": 3.5 * k,
                         "length_m": 201.0} for k in range(legs)],
               "threat_basis": "sensed", "threats_considered": 1, "proposed": True,
               "caveats": [wg.SENSED_CAVEAT, wg.TERRAIN_OFF_CAVEAT], "path": path,
               "created_ms": int(WALL_S * 1000) - 10_000 + self.s.cor_seq, "simulated": True}
        self.s.vectors[vec["id"]] = vec
        return vec

    def axes(self) -> None:
        self.eng._update_axes(self.s)

    def fly(self, vehicle: str, u: wg.Unit, *, agl: float = 60.0) -> None:
        """Put `vehicle` airborne over unit `u` (inside its envelope)."""
        self.srv._last_tele[vehicle] = {"lat": u.lat, "lon": u.lon, "alt_hae_m": agl,
                                        "landed_state": 1, "speed_mps": 8.0}

    # ---- the picture -----------------------------------------------------------
    def inputs(self, *, truth: bool = False, tracks=None, **over) -> ig.GraphInputs:
        rows = self.rows() if tracks is None else tracks
        return live_inputs(tracks=rows, contacts=[], truth=truth,
                           wargame=isc.gather(self.srv, truth=truth), **over)

    def graph(self, *, truth: bool = False, scope: str = "theater", **over) -> dict:
        return ig.build_graph(self.inputs(truth=truth, **over), scope=scope)


def standard(*, reveal_red: bool = False) -> tuple[Scn, dict]:
    """One of everything: red guns sensed and struck, a red SAM downing Drone1,
    red armour advancing on a blue depot and firing at blue mech, a corridor,
    a waiting strike."""
    sc = Scn(reveal_red=reveal_red)
    guns = sc.unit("red", "ad_gun", 1600, 1000)
    sam = sc.unit("red", "ad_short", 1800, -1200)
    depot = sc.unit("blue", "blue_defended_point", -600, -300)
    armour = sc.unit("red", "armour_company", 2200, 200, objective=depot)
    arty = sc.unit("blue", "blue_artillery", -900, -900)
    mech = sc.unit("blue", "blue_mech", -300, 600)
    t_guns = sc.track(guns)
    t_sam = sc.track(sam)
    hit = sc.strike(arty, guns, t_guns, outcome="damaged")
    waiting = sc.strike(arty, sam, t_sam, phase="proposed")
    shot = sc.shot(sam, "Drone1")
    ground = sc.ground(armour, mech)
    cor = sc.corridor(t_guns)
    sc.axes()
    sc.fly("Drone2", guns)
    return sc, {"guns": guns, "sam": sam, "depot": depot, "armour": armour, "arty": arty,
                "mech": mech, "t_guns": t_guns, "t_sam": t_sam, "hit": hit,
                "waiting": waiting, "shot": shot, "ground": ground, "cor": cor}


def nodes(g, ntype=None) -> list[dict]:
    return [n for n in g["nodes"] if ntype is None or n["type"] == ntype]


def node(g, nid) -> dict | None:
    return next((n for n in g["nodes"] if n["id"] == nid), None)


def edges(g, kind=None) -> list[tuple[str, str, str]]:
    return [(e["a"], e["b"], e["kind"]) for e in g["edges"] if kind is None or e["kind"] == kind]


def two_drones(**over):
    return {"vehicles": [vehicle_row("Drone1", landed_state=0),
                         vehicle_row("Drone2", lat=at(1600, 1000)[0], lon=at(1600, 1000)[1])],
            **over}


# ===========================================================================
# rows per view
# ===========================================================================

class TestViews:
    def test_blue_view_hides_red_truth(self):
        sc, x = standard()
        g = sc.graph(**two_drones())
        forces = nodes(g, "force")
        assert forces and {n["attrs"]["side"] for n in forces} == {"blue"}
        assert not any("correlated" in n["attrs"] for n in forces)
        assert not edges(g, "axis") and not edges(g, "threatens") and not edges(g, "correlates")
        assert not any(n["attrs"]["kind"] == "axis" for n in nodes(g, "vector"))
        shot = node(g, f"eng:{x['shot'].engagement_id}")["attrs"]
        assert shot["attacker"] is None
        assert shot["attacker_label"] == "Red air defence (not identified)"
        assert shot["p_notional"] is None and shot["inputs"] == []
        ground = node(g, f"eng:{x['ground'].engagement_id}")["attrs"]
        assert ground["attacker"] is None
        assert ground["attacker_label"] == "Red ground forces (not identified)"
        hit = node(g, f"eng:{x['hit'].engagement_id}")["attrs"]
        assert hit["outcome_hidden"] is True and hit["outcome"] is None   # before BDA
        assert not any(e[1].startswith("frc:red-") for e in edges(g, "launched_by"))
        assert g["meta"]["wargame"]["truth_view"] is False
        assert g["meta"]["wargame"]["counts"]["red"] == {"seen": 2}

    def test_umpire_view_shows_truth(self):
        sc, x = standard()
        g = sc.graph(truth=True, **two_drones())
        reds = {n["id"] for n in nodes(g, "force") if n["attrs"]["side"] == "red"}
        assert reds == {"frc:red-aaa-1", "frc:red-sam-1", "frc:red-armour-1"}
        guns = node(g, "frc:red-aaa-1")["attrs"]
        assert guns["correlated"] == [f"trk:{x['t_guns'].track_id}"]
        assert (f"trk:{x['t_guns'].track_id}", "frc:red-aaa-1", "correlates") in edges(g)
        assert ("frc:red-armour-1", "frc:blue-depot-1", "axis") in edges(g)
        assert ("frc:red-aaa-1", "veh:Drone2", "threatens") in edges(g)
        shot = node(g, f"eng:{x['shot'].engagement_id}")["attrs"]
        assert shot["attacker"] == "frc:red-sam-1" and shot["attacker_label"] == "Red SAM 1"
        assert (f"eng:{x['shot'].engagement_id}", "frc:red-sam-1", "launched_by") in edges(g)
        hit = node(g, f"eng:{x['hit'].engagement_id}")["attrs"]
        assert hit["outcome"] == "damaged" and hit["outcome_hidden"] is False
        assert g["meta"]["wargame"]["truth_view"] is True
        assert g["meta"]["wargame"]["counts"]["red"]["units"] == 3

    def test_a_revealed_session_shows_red_but_never_the_truth_only_rows(self):
        sc, _x = standard(reveal_red=True)
        g = sc.graph(**two_drones())
        assert {n["attrs"]["side"] for n in nodes(g, "force")} == {"red", "blue"}
        assert not any("correlated" in n["attrs"] for n in nodes(g, "force"))
        assert not {k for _a, _b, k in edges(g)} & isc.TRUTH_EDGES
        assert g["meta"]["wargame"]["truth_view"] is True

    def test_the_outcome_shows_in_blue_view_after_a_look(self):
        sc = Scn()
        guns, arty = sc.unit("red", "ad_gun", 1600, 1000), sc.unit("blue", "blue_artillery",
                                                                    -900, -900)
        e = sc.strike(arty, guns, sc.track(guns), outcome="destroyed", looks=1)
        a = node(sc.graph(), f"eng:{e.engagement_id}")["attrs"]
        assert a["outcome_hidden"] is False and a["outcome"] == "destroyed"
        assert a["bda"]["looks"] == 1

    def test_the_filter_holds_even_when_the_engine_does_not(self):
        """Fog is re-applied here: an engine row that leaks red into the Blue
        view (or truth-only rows) is dropped or masked all the same."""
        sc, x = standard()
        leaky = isc.gather(sc.srv, truth=True)
        g = ig.build_graph(live_inputs(tracks=sc.rows(), contacts=[], truth=False,
                                       wargame=leaky, **two_drones()))
        assert {n["attrs"]["side"] for n in nodes(g, "force")} == {"blue"}
        assert not {k for _a, _b, k in edges(g)} & isc.TRUTH_EDGES
        shot = node(g, f"eng:{x['shot'].engagement_id}")["attrs"]
        assert shot["attacker"] is None and shot["p_notional"] is None
        hit = node(g, f"eng:{x['hit'].engagement_id}")["attrs"]
        assert hit["outcome"] is None and hit["outcome_hidden"] is True
        assert not any(n["attrs"].get("kind") == "axis" for n in nodes(g, "vector"))

    def test_every_wargame_row_is_simulated_and_scenario(self):
        sc, _x = standard()
        for truth in (False, True):
            g = sc.graph(truth=truth, **two_drones())
            rows = [n for n in g["nodes"] if n["type"] in isc.WARGAME_TYPES]
            assert rows and all(n["attrs"]["simulated"] is True for n in rows)
            assert all(n["attrs"]["provenance"] == "scenario" for n in nodes(g, "force"))
            assert g["meta"]["wargame"]["simulated"] is True
            assert isc.SESSION_CAVEAT in g["meta"]["caveats"]

    def test_edges_reference_nodes_and_only_known_kinds(self):
        sc, _x = standard()
        for truth in (False, True):
            g = sc.graph(truth=truth, **two_drones())
            ids = {n["id"] for n in g["nodes"]}
            for a, b, kind in edges(g):
                assert a in ids and b in ids
            wg_kinds = {k for _a, _b, k in edges(g)} & WG_EDGES
            assert wg_kinds <= isc.EDGE_KINDS
            for n in g["nodes"]:
                assert ig.PREFIX_TYPE[n["id"].split(":", 1)[0]] == n["type"]
            ig.json_size(g)

    def test_no_wargame_edge_touches_a_real_place(self):
        sc, x = standard()
        wg_rows = isc.gather(sc.srv, truth=True)
        poi = "poi:default:North Field"
        wg_rows["edges"] += [{"a": "frc:red-aaa-1", "b": poi, "kind": "threatens"},
                             {"a": f"eng:{x['hit'].engagement_id}", "b": "thr:default",
                              "kind": "attacks"},
                             {"a": "frc:red-aaa-1", "b": "sit:default:way/1", "kind": "axis"}]
        g = ig.build_graph(live_inputs(tracks=sc.rows(), contacts=[], truth=True,
                                       wargame=wg_rows))
        assert poi in {n["id"] for n in g["nodes"]}
        for a, b, kind in edges(g):
            if kind in WG_EDGES:
                assert not b.startswith(isc.REAL_PREFIXES) and not a.startswith(
                    isc.REAL_PREFIXES), (a, b, kind)


# ===========================================================================
# caps and meta.wargame
# ===========================================================================

class FakeEngine:
    """An engine answering fixed rows: for caps, errors and the inactive case."""

    def __init__(self, rows=None, feats=None, *, revision=5, owns=(), boom=False):
        self.rows, self.feats, self.revision = rows, feats or [], revision
        self.owns, self.boom = set(owns), boom

    def graph_rows(self, *, truth):
        if self.boom:
            raise RuntimeError("engine down")
        return self.rows

    def overlay_features(self, *, truth):
        if self.boom:
            raise RuntimeError("engine down")
        return self.feats

    def owns_name(self, name):
        return name in self.owns


def active_meta(**over):
    return {"active": True, "session_id": SID, "started_at_ms": NOW_MS - 1000, "seed": 1,
            "engine": T.TABLE_VERSION, "time_scale": 1.0, "red_engages": True,
            "reveal_red": False, "truth_view": False, "revision": 9, "pending": [],
            "counts": {"blue": {"units": 1}, "red": {"units": 4, "seen": 1}},
            "caveats": [wg.SESSION_CAVEAT], "step_ms": 1.5, "errors": 0, "simulated": True,
            **over}


def row(nid, ntype, ts=None, **attrs):
    return {"id": nid, "type": ntype, "label": nid, "subtitle": "", "group": "air",
            "salience": 0.5, "status": "ok", "ts_ms": ts, "lat": DEFAULT.home_lat,
            "lon": DEFAULT.home_lon, "attrs": {"simulated": True, **attrs}}


def fake_graph(rows, meta=None, *, truth=False, edges_=()):
    eng = FakeEngine({"nodes": rows, "edges": list(edges_),
                      "meta": {"wargame": meta or active_meta(), "caveats": []}})
    srv = SimpleNamespace(wargame=eng)
    return ig.build_graph(live_inputs(truth=truth, wargame=isc.gather(srv, truth=truth)))


class TestCapsAndMeta:
    def test_caps_keep_sixty_forces_and_the_newest_engagements_and_vectors(self):
        rows = [row(f"frc:blue-mech-{k}", "force", side="blue", provenance="scenario")
                for k in range(70)]
        rows += [row(f"eng:{SID}-E{k}", "engagement", ts=NOW_MS - 100_000 + k,
                     kind="blue_strike", bda={"looks": 1}) for k in range(30)]
        rows += [row(f"vec:cor-{k}", "vector", ts=NOW_MS - 100_000 + k, kind="corridor",
                     side="blue") for k in range(15)]
        g = fake_graph(rows)
        assert len(nodes(g, "force")) == 60
        assert [n["id"] for n in nodes(g, "force")][:2] == ["frc:blue-mech-0", "frc:blue-mech-1"]
        engs = [n["id"] for n in nodes(g, "engagement")]
        assert engs == [f"eng:{SID}-E{k}" for k in range(6, 30)]           # newest 24
        assert [n["id"] for n in nodes(g, "vector")] == [f"vec:cor-{k}" for k in range(3, 15)]
        assert g["meta"]["wargame"]["omitted"] == {"force": 10, "engagement": 6, "vector": 3}
        assert g["meta"]["counts"]["force"] == 60

    def test_only_scenario_forces_enter(self):
        g = fake_graph([row("frc:blue-mech-1", "force", side="blue", provenance="scenario"),
                        row("frc:blue-mech-2", "force", side="blue", provenance="osm"),
                        row("frc:blue-mech-3", "force", side="blue"),
                        row("sit:default:way/1", "force", side="blue", provenance="scenario"),
                        row("frc:blue-mech-4", "site", side="blue", provenance="scenario")])
        assert [n["id"] for n in g["nodes"] if n["type"] in isc.WARGAME_TYPES] == [
            "frc:blue-mech-1"]

    def test_inactive_meta_is_exactly_active_false_and_last(self):
        g = ig.build_graph(live_inputs())
        assert g["meta"]["wargame"] == {"active": False, "last": None}
        last = {"session_id": SID, "aar_id": f"aar-{SID}", "ended_at_ms": NOW_MS, "x": 1}
        g = fake_graph([], {"active": False, "last": last})
        assert g["meta"]["wargame"] == {"active": False, "last": {
            "session_id": SID, "aar_id": f"aar-{SID}", "ended_at_ms": NOW_MS}}
        assert not [n for n in g["nodes"] if n["type"] in isc.WARGAME_TYPES]
        assert isc.SESSION_CAVEAT not in g["meta"]["caveats"]

    def test_active_meta_keys_and_blue_view_counts(self):
        g = fake_graph([], active_meta(pending=[f"eng:{SID}-E2", "bogus"]))
        m = g["meta"]["wargame"]
        assert list(m) == list(isc.META_KEYS)
        assert m["counts"]["red"] == {"seen": 1} and m["pending"] == [f"eng:{SID}-E2"]
        g = fake_graph([], active_meta(), truth=True)
        assert g["meta"]["wargame"]["counts"]["red"] == {"units": 4, "seen": 1}
        assert g["meta"]["wargame"]["truth_view"] is True

    def test_a_failing_engine_is_said_never_guessed(self):
        srv = SimpleNamespace(wargame=FakeEngine(boom=True))
        g = ig.build_graph(live_inputs(wargame=isc.gather(srv)))
        m = g["meta"]["wargame"]
        assert m["active"] is False and m["last"] is None
        assert m["error"] == "the simulated wargame engine could not be read (RuntimeError)"
        assert any("wargame rows are missing, not absent" in c for c in g["meta"]["caveats"])

    def test_meta_overlay_rev_carries_the_engine_revision_and_view(self):
        g = fake_graph([], active_meta(revision=9))
        assert g["meta"]["overlay_rev"].split(":")[2:] == ["9", "0"]
        g = fake_graph([], active_meta(revision=9), truth=True)
        assert g["meta"]["overlay_rev"].endswith(":9:1")


class TestVehicleLoss:
    def test_a_downed_drone_is_marked_and_its_attacker_masked_in_blue_view(self):
        sc, x = standard()
        v = node(sc.graph(), "veh:Drone1")["attrs"]
        assert v["wargame_state"] == "lost"
        assert v["wargame_lost_at_ms"] == x["shot"].adjudicated_at_ms
        assert v["wargame_lost_by"] is None
        v = node(sc.graph(truth=True), "veh:Drone1")["attrs"]
        assert v["wargame_lost_by"] == "frc:red-sam-1"

    def test_the_attacker_is_found_by_designator_when_the_shot_left_the_graph(self):
        sc = Scn()
        sam = sc.unit("red", "ad_short", 1800, -1200)
        sc.shot(sam, "Drone1")
        sc.s.engagements.clear()                     # the shot aged out of the newest 24
        v = node(sc.graph(truth=True), "veh:Drone1")["attrs"]
        assert v["wargame_state"] == "lost" and v["wargame_lost_by"] == "frc:red-sam-1"

    def test_no_loss_no_attrs(self):
        sc = Scn()
        sc.unit("blue", "blue_artillery", -900, -900)
        v = node(sc.graph(truth=True), "veh:Drone1")["attrs"]
        assert "wargame_state" not in v and "wargame_lost_by" not in v


# ===========================================================================
# scenario contacts: generic labels, no OB text
# ===========================================================================

RED_OB_CLASSES = [c.ob_key for c in T.CLASSES.values() if c.side == "red"]


def service(sc: Scn, ctx_rows=None) -> ig.IntelService:
    rows = sc.rows() if ctx_rows is None else ctx_rows

    class Ctx:
        def snapshot(self):
            return {"vehicles": [vehicle_row("Drone1"), vehicle_row("Drone2")],
                    "missions": [], "contacts": [], "feeds": {}}

        def track_rows(self):
            return rows

    s = ig.IntelService(Ctx(), sc.srv)
    s.cache_ttl_s = 0.0
    return s


class TestScenarioContacts:
    @pytest.mark.parametrize("wg_class", [c.key for c in T.CLASSES.values() if c.side == "red"])
    def test_a_scenario_contact_reads_generic_everywhere(self, wg_class):
        sc = Scn()
        u = sc.unit("red", wg_class, 1600, 1000)
        t = sc.track(u)
        label = T.label_for_ob(t.ob_class)
        g = sc.graph(truth=True)
        n = node(g, f"trk:{t.track_id}")
        assert n["label"] == label == T.CLASSES[wg_class].label
        assert n["attrs"]["scenario"] is True and n["attrs"]["platform"] == label
        assert n["attrs"]["simulated"] is True
        assert n["subtitle"].endswith(targets.SCENARIO_CONTACT_NOTE)
        assert f"ob:{t.ob_class}" not in {x["id"] for x in g["nodes"]}   # no OB roll-up
        assert not nodes(g, "unit") and not edges(g, "is_a")
        ob_name = OB_LIBRARY[t.ob_class].name
        assert ob_name not in str(g)
        assert_no_real_system_tokens(g)
        svc = service(sc)
        ent = svc.entity(f"trk:{t.track_id}")
        assert ent["fields"]["platform"] == label and ent["fields"]["scenario"] is True
        assert "raw" not in ent and ob_name not in str(ent)
        assert ent["provenance"]["order_of_battle"] == isc.OB_WITHHELD
        assert ent["fields"]["salute"]["location"]["lat"] == pytest.approx(t.lat, abs=1e-6)
        assert_no_real_system_tokens(ent)
        top = svc.overview()["contacts"]["top"]
        assert [c["label"] for c in top] == [label]

    def test_two_scenario_contacts_side_by_side_are_never_an_ob_element(self):
        sc = Scn()
        a = sc.unit("red", "ad_gun", 1600, 1000)
        b = sc.unit("red", "ad_gun", 1600, 1250)          # 250 m: inside the 500 m element
        sc.track(a)
        sc.track(b)
        g = sc.graph(truth=True)
        assert not nodes(g, "unit") and not nodes(g, "equipment")
        assert "x towed" not in str(g).lower()

    def test_rows_the_bridge_polled_before_the_flag_are_marked_by_name(self):
        sc = Scn()
        t = sc.track(sc.unit("red", "ad_gun", 1600, 1000))
        stale = [{k: v for k, v in r.items() if k not in ("scenario", "simulated")}
                 for r in sc.rows()]
        assert "scenario" not in stale[0]
        g = service(sc, stale).graph()
        assert node(g, f"trk:{t.track_id}")["label"] == "Air-defence guns"
        marked = isc.mark_scenario_rows(stale, sc.srv)
        assert marked[0]["scenario"] is True and "scenario" not in stale[0]   # a copy

    def test_isr_tracks_are_untouched(self):
        sc = Scn()
        isr = Track(track_id="TRK-ISRISRISRISR-0001", name="SA-6_site_1", category="sam",
                    lat=at(500, 500)[0], lon=at(500, 500)[1], alt_m=10.0,
                    first_seen=WALL_S - 100, last_seen=WALL_S - 5, ob_class="sam_medium_range")
        g = sc.graph(tracks=[salute_report(isr, now=WALL_S)])
        n = node(g, f"trk:{isr.track_id}")
        assert n["label"] == OB_LIBRARY["sam_medium_range"].name
        assert "scenario" not in n["attrs"] and "platform" not in n["attrs"]
        assert node(g, "ob:sam_medium_range") is not None
        assert isc.mark_scenario_rows([{"equipment_name": "SA-6_site_1"}], sc.srv) == [
            {"equipment_name": "SA-6_site_1"}]


# ===========================================================================
# the after-action review
# ===========================================================================

def an_aar(**over) -> dict:
    return aar.build_aar(
        session_id=SID, theater_id="default", started_at_ms=NOW_MS - 900_000,
        ended_at_ms=NOW_MS, seed=4417, time_scale=4.0, red_engages=True, reveal_red=False,
        units=[{"side": "red", "state": "destroyed"}, {"side": "blue", "state": "active"}],
        events=[{"t_ms": NOW_MS - 1000, "sim_s": 12.0, "kind": "blue_strike_executed",
                 "side": "blue", "text": "Blue artillery 1 struck Air-defence guns.",
                 "outcome": "destroyed", "register": "scenario", "simulated": True}],
        sorties=[{"vehicle": "Drone1", "missions": 2, "lost": False}], bda_accuracy=[],
        track_ids=["TRK-SCNSCNSCNSCN-0001"], **over)


class TestAfterActionReview:
    def test_report_node_format_and_entity_markdown(self):
        rep = an_aar()
        g = ig.build_graph(live_inputs(reports={f"aar-{SID}": rep}))
        n = node(g, f"rpt:aar-{SID}")
        assert n["type"] == "report" and n["attrs"]["format"] == "AAR"
        assert n["label"] == f"After-action review (simulated) {SID}"
        assert n["attrs"]["simulated"] is True and n["ts_ms"] == NOW_MS
        assert n["status"] == "ok" and "incomplete" not in n["attrs"]
        sc = Scn()
        sc.srv.reports = {f"aar-{SID}": rep}
        ent = service(sc).entity(f"aar-{SID}")
        assert ent["id"] == f"rpt:aar-{SID}"
        f = ent["fields"]
        assert f["markdown"] == rep["markdown"] and f["report_type"] == "AAR"
        assert f["header"]["format"] == "AAR" and "markdown" not in f["header"]
        assert f["title"] == aar.AAR_TITLE and f["track_ids"] == rep["track_ids"]
        assert ent["provenance"]["resource"] == f"uav://reports/aar-{SID}"
        assert "raw" not in ent
        assert_no_real_system_tokens(ent)

    def test_an_incomplete_review_warns(self):
        rep = an_aar(incomplete=True)
        n = node(ig.build_graph(live_inputs(reports={f"aar-{SID}": rep})), f"rpt:aar-{SID}")
        assert n["status"] == "warn" and n["attrs"]["incomplete"] is True
        assert n["subtitle"].endswith("incomplete")

    def test_other_reports_are_unchanged(self):
        g = ig.build_graph(live_inputs(reports={"latest": {
            "format": "INTREP", "report_id": "R1", "gaps": [], "total_tracks": 3}}))
        n = node(g, "rpt:R1")
        assert n["label"] == "INTREP R1" and n["attrs"]["format"] == "INTREP"


# ===========================================================================
# budget, tokens
# ===========================================================================

def max_load() -> Scn:
    """60 forces (30 red, 30 blue), 24 engagements, 12 corridors of 40 points."""
    sc = Scn()
    classes = ["ad_gun", "ad_short", "ad_manportable", "radar_early_warning",
               "armour_company", "artillery_battery"]
    reds = [sc.unit("red", classes[k % 6], 1200 + 60 * (k // 6), -1500 + 500 * (k % 6))
            for k in range(30)]
    blues = [sc.unit("blue", ("blue_artillery", "blue_mech", "blue_rocket")[k % 3],
                     -1500 + 60 * (k // 6), -1500 + 500 * (k % 6)) for k in range(30)]
    tracks = [sc.track(u) for u in reds[:24]]
    for k in range(22):
        sc.strike(blues[k], reds[k], tracks[k], outcome="damaged", looks=k % 2)
    for k in (22, 23):
        sc.strike(blues[k], reds[k], tracks[k], phase="proposed")
    for k in range(12):
        sc.corridor(tracks[k], legs=20)
    return sc


class TestBudget:
    @staticmethod
    def _picture(runs):
        from test_intel_graph import TestBudgets

        ss = TestBudgets._max_sites()
        state = theater_tools.theater_state(SimpleNamespace(
            theater=DEFAULT, theater_epoch=1, sites=ss, airframe_id="group3_fixed_wing"))
        return {**TestBudgets._picture(runs), "theater_state": state, "sites": ss}

    @pytest.mark.parametrize("truth", [False, True])
    def test_maximum_load_fits_in_150_kb(self, truth):
        """§3.2: 100 tracks, 60 worst-case sites, 60 forces, 24 engagements and
        12 vectors. Sites give way first, then the wargame's long lists,
        `threatens` edges, nulls, subtitles and the oldest settled rows; every
        cut is counted and waiting engagements and forces always stay."""
        sc = max_load()
        g = ig.build_graph(live_inputs(**self._picture(1), truth=truth,
                                       wargame=isc.gather(sc.srv, truth=truth)))
        size = ig.json_size(g)
        assert size <= ig.GRAPH_TARGET_BYTES, f"{size} B ({'umpire' if truth else 'blue'})"
        assert len(nodes(g, "track")) == 100
        assert len(nodes(g, "force")) == (60 if truth else 30)
        pending = {f"eng:{SID}-E23", f"eng:{SID}-E24"}
        assert pending <= {n["id"] for n in nodes(g, "engagement")}
        assert set(g["meta"]["wargame"]["pending"]) == pending
        trimmed = g["meta"]["wargame"]["trimmed_for_budget"]
        assert trimmed["lists"] >= 12 and g["meta"]["sites"]["tags_trimmed"] is True
        shown = len(nodes(g, "engagement"))
        assert shown + trimmed.get("engagement", 0) == 24
        assert g["meta"]["counts"]["engagement"] == shown
        if trimmed.get("engagement"):
            assert any("left off this graph" in c for c in g["meta"]["caveats"])
        # the ISR contacts beside them are real-class OB data; the wargame's are not
        assert_no_real_system_tokens([n for n in g["nodes"] if n["type"] in isc.WARGAME_TYPES])

    def test_the_pessimistic_ten_run_picture_trims_in_order_and_says_so(self):
        """Phase A's pessimistic picture (every contact re-tracked in 10 runs)
        already fills ~130 KB; with 60 forces in the Umpire view it cannot fit
        without dropping forces, which never give way. It is trimmed as far as
        the rules allow and every cut is counted (the Blue view fits)."""
        sc = max_load()
        blue = ig.build_graph(live_inputs(**self._picture(10),
                                          wargame=isc.gather(sc.srv, truth=False)))
        assert ig.json_size(blue) <= ig.GRAPH_TARGET_BYTES
        g = ig.build_graph(live_inputs(**self._picture(10), truth=True,
                                       wargame=isc.gather(sc.srv, truth=True)))
        trimmed = g["meta"]["wargame"]["trimmed_for_budget"]
        assert set(trimmed) == {"lists", "threatens", "nulls", "subtitles", "engagement",
                                "vector"}
        assert len(nodes(g, "force")) == 60
        assert len(nodes(g, "engagement")) == isc.MIN_ENGAGEMENTS
        assert len(nodes(g, "vector")) == isc.MIN_VECTORS
        assert len(edges(g, "threatens")) == isc.THREATENS_KEEP
        assert ig.json_size(g) <= 1.15 * ig.GRAPH_TARGET_BYTES   # the known overshoot

    def test_the_inspector_is_never_thinned_by_the_budget(self):
        sc = max_load()
        pic = self._picture(10)
        svc = service(sc)
        svc.gather = lambda *, truth=False: live_inputs(
            **pic, truth=truth, wargame=isc.gather(sc.srv, truth=truth))
        g = svc.graph(truth=True)
        assert g["meta"]["wargame"]["trimmed_for_budget"]["subtitles"] > 0
        f = svc.entity("frc:red-sam-1", truth=True)["fields"]
        assert f["subtitle"] and "strike_range_m" in f and len(f["caveats"]) >= 1
        gone = f"eng:{SID}-E1"                           # the oldest settled strike
        assert node(g, gone) is None
        ent = svc.entity(gone, truth=True)
        assert ent is not None and ent["fields"]["kind"] == "blue_strike"
        assert ent["fields"]["inputs"] == ["Range 3.2 km of 20.0 km",
                                           "Perceived as air-defence guns, probable"]


class TestNoRealSystemTokens:
    def test_graph_engagement_and_vector_attrs(self):
        sc, _x = standard()
        for truth in (False, True):
            g = sc.graph(truth=truth, **two_drones())
            rows = [n for n in g["nodes"] if n["type"] in isc.WARGAME_TYPES]
            assert {n["type"] for n in rows} == set(isc.WARGAME_TYPES)
            for n in rows:
                assert_no_real_system_tokens(n["attrs"])
            assert_no_real_system_tokens(g)

    def test_entities_and_overlay(self):
        sc, x = standard()
        svc = service(sc)
        for nid in ("frc:red-sam-1", "frc:blue-artillery-1", f"eng:{x['hit'].engagement_id}",
                    f"vec:{x['cor']['id']}", "vec:axis-red-armour-1",
                    f"trk:{x['t_sam'].track_id}"):
            ent = svc.entity(nid, truth=True)
            assert ent is not None, nid
            assert_no_real_system_tokens(ent)
        for truth in (False, True):
            assert_no_real_system_tokens(ov.build_overlay(sc.srv, truth=truth))

    def test_the_helper_would_catch_an_ob_name(self):
        """The check is live: the ISR row of the same object names the system."""
        row = salute_report(Track(track_id="TRK-X-1", name="n", category="aaa", lat=1.0,
                                  lon=1.0, alt_m=0.0, first_seen=1.0, last_seen=2.0,
                                  ob_class="aaa_towed"), now=3.0)
        assert find_real_system_tokens(row)


# ===========================================================================
# the overlay
# ===========================================================================

def kinds(body) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in body["features"]:
        out[f["properties"]["kind"]] = out.get(f["properties"]["kind"], 0) + 1
    return out


class TestOverlay:
    def test_blue_and_umpire_kinds(self):
        sc, x = standard()
        blue = ov.build_overlay(sc.srv)
        assert kinds(blue) == {"force": 3, "vector": 1, "engagement": 4}
        assert blue["counts"] == kinds(blue) and blue["omitted"] == {}
        assert {f["properties"]["side"] for f in blue["features"]
                if f["properties"]["kind"] == "force"} == {"blue"}
        umpire = ov.build_overlay(sc.srv, truth=True)
        k = kinds(umpire)
        assert k["force"] == 6 and k["vector"] == 2 and k["engagement"] == 4
        assert k["force_envelope"] == 4                 # guns and SAM: threat + detection
        for f in umpire["features"]:
            p = f["properties"]
            assert p["simulated"] is True and p["truth"] is True
            assert p["register"] == "scenario" and p["id"] == f["id"]
            assert {"kind", "id", "label", "register", "simulated", "truth"} <= set(p)
        env = next(f for f in umpire["features"] if f["properties"]["kind"] == "force_envelope")
        assert env["geometry"]["type"] == "Polygon" and env["properties"]["force"].startswith(
            "frc:red-")
        cor = next(f for f in umpire["features"] if f["properties"].get("kind_detail")
                   == "corridor")
        assert cor["geometry"]["type"] == "LineString" and len(cor["geometry"]["coordinates"]) == 40
        shot = next(f for f in blue["features"] if f["id"] == f"eng:{x['shot'].engagement_id}")
        assert shot["properties"]["from"] is None                  # masked in blue view

    def test_rev_carries_the_engine_revision_and_unchanged_answers(self):
        sc, _x = standard()
        sc.eng.revision = 17
        body = ov.build_overlay(sc.srv, truth=True)
        assert body["rev"].split(":")[2:] == ["17", "1"]
        assert ov.build_overlay(sc.srv, truth=True, rev=body["rev"]) == {
            "rev": body["rev"], "unchanged": True}
        sc.eng.revision = 18
        assert "features" in ov.build_overlay(sc.srv, truth=True, rev=body["rev"])
        g = sc.graph(truth=True)
        assert g["meta"]["overlay_rev"].split(":")[2:] == ["18", "1"]

    def test_caps_per_kind_and_coordinates(self):
        def feat(fid, kind, **p):
            geom = ({"type": "Polygon", "coordinates": [[[1.123456789, 2.0], [1.0, 2.1],
                                                          [1.1, 2.2], [1.123456789, 2.0]]]}
                    if kind == "force_envelope" else
                    {"type": "LineString", "coordinates": [[1.0, 2.0], [1.1, 2.1]]}
                    if kind == "vector" else {"type": "Point", "coordinates": [1.0, 2.0]})
            return {"type": "Feature", "id": fid, "geometry": geom,
                    "properties": {"kind": kind, "id": fid, "label": fid, **p}}

        feats = [feat(f"frc:blue-mech-{k}", "force", side="blue") for k in range(70)]
        feats += [feat(f"env:red-sam-{k}:threat", "force_envelope") for k in range(90)]
        feats += [feat(f"vec:axis-red-armour-{k}", "vector", kind_detail="axis")
                  for k in range(25)]
        feats += [feat(f"vec:cor-{k}", "vector", kind_detail="corridor") for k in range(9)]
        feats += [feat(f"eng:{SID}-E{k}", "engagement") for k in range(30)]
        feats += [feat("sit:default:way/1", "site"), feat("x", "weird"), {"nope": 1},
                  {**feat("frc:bad", "force"), "geometry": {"type": "Point",
                                                            "coordinates": [float("nan"), 1]}}]
        srv = SimpleNamespace(wargame=FakeEngine(feats=feats))
        got, counts, omitted = isc.overlay_features(srv, truth=True)
        assert counts == {"force": 60, "force_envelope": 80, "vector": 26, "engagement": 24}
        assert omitted == {"force": 10, "force_envelope": 10, "vector": 8, "engagement": 6}
        ids = [f["id"] for f in got]
        assert f"eng:{SID}-E29" in ids and f"eng:{SID}-E5" not in ids     # the newest 24
        assert "vec:cor-8" in ids and "vec:cor-2" not in ids               # the newest 6
        env = next(f for f in got if f["properties"]["kind"] == "force_envelope")
        assert env["geometry"]["coordinates"][0][0] == [1.123457, 2.0]
        assert all(f["properties"]["simulated"] is True for f in got)

    def test_maximum_load_body_under_400_kb(self):
        from test_intel_graph import TestBudgets

        sc = max_load()
        sc.srv.sites = TestBudgets._max_sites(300)
        for truth in (False, True):
            body = ov.build_overlay(sc.srv, truth=truth)
            assert ov.body_size(body) <= ov.OVERLAY_MAX_BYTES
            assert body["counts"]["site"] == 300

    def test_a_failing_engine_leaves_the_sites(self):
        from test_intel_sites import server, site, siteset

        srv = server(siteset([site(0), site(1)]), wargame=FakeEngine(boom=True))
        body = ov.build_overlay(srv)
        assert body["counts"] == {"site": 2}
        assert body["wargame"] == {"error": f"{isc.ENGINE_ERROR} (RuntimeError)"}
        srv = server(siteset([site(0)]), wargame=SimpleNamespace(revision=3))
        assert set(ov.build_overlay(srv)) == {"type", "rev", "theater", "attribution", "counts",
                                              "omitted", "features", "sites"}

    def test_bidi_is_stripped_from_labels(self):
        sc = Scn()
        u = sc.unit("blue", "blue_artillery", -900, -900, designator=f"{XSS}{BIDI}")
        body = ov.build_overlay(sc.srv)
        f = next(f for f in body["features"] if f["id"] == f"frc:{u.unit_id}")
        assert f["properties"]["label"] == f"{XSS}evil"
        g = sc.graph()
        assert node(g, f"frc:{u.unit_id}")["label"] == f"{XSS}evil"


# ===========================================================================
# the service, the routes, ids and the ISR picture
# ===========================================================================

class TestServiceAndRoutes:
    def test_graph_is_cached_per_scope_and_view(self):
        sc, _x = standard()
        svc = service(sc)
        svc.cache_ttl_s = 60.0
        blue, umpire = svc.graph(), svc.graph(truth=True)
        assert blue is svc.graph() and umpire is svc.graph("theater", truth=True)
        assert blue is not umpire
        assert set(svc._cache) == {("theater", False), ("theater", True)}
        assert not any(n["attrs"].get("side") == "red" for n in nodes(blue, "force"))
        assert any(n["attrs"].get("side") == "red" for n in nodes(umpire, "force"))
        svc.invalidate()
        assert svc._cache == {}

    def test_a_session_end_drops_the_cached_graph_at_once(self):
        """B17 (E2E B1): a graph gathered while a session ran is never served
        after it ended, even inside the cache TTL, nor the other way round."""
        sc, _x = standard()
        svc = service(sc)
        svc.cache_ttl_s = 60.0
        during = svc.graph(truth=True)
        assert nodes(during, "force") and during is svc.graph(truth=True)
        session, sc.eng._session = sc.eng._session, None
        after = svc.graph(truth=True)
        assert after is not during and nodes(after, "force") == []
        assert after["meta"]["wargame"]["active"] is False
        assert after is svc.graph(truth=True)
        sc.eng._session = session
        assert nodes(svc.graph(truth=True), "force")

    def test_an_unreadable_mode_key_reads_as_isr(self):
        sc, _x = standard()
        svc = service(sc)

        def broken():
            raise RuntimeError("engine gone")

        sc.eng.mode_key = broken
        assert svc._mode_key() == "isr"
        assert nodes(svc.graph(truth=True), "force")
        svc.server = None
        assert svc._mode_key() == "isr"

    def test_the_analysts_reads_never_see_truth(self):
        """overview, search and entity without `truth` (the toolbelt's calls)."""
        sc, x = standard()
        svc = service(sc)
        assert svc.entity("frc:red-sam-1") is None
        assert svc.entity("red-sam-1") is None
        assert svc.entity("frc:red-sam-1", truth=True)["fields"]["side"] == "red"
        assert not [r for r in svc.search("red sam", types=["force"])]
        assert [r["id"] for r in svc.search("Blue artillery", types=["force"])] == [
            "frc:blue-artillery-1"]
        shot = svc.entity(f"eng:{x['shot'].engagement_id}")
        assert shot["fields"]["attacker"] is None
        assert shot["fields"]["attacker_label"] == "Red air defence (not identified)"
        assert shot["provenance"]["view"].startswith("blue")
        assert "Red SAM" not in str(svc.overview())

    def test_bare_ids_resolve_and_entities_carry_provenance(self):
        sc, x = standard()
        svc = service(sc)
        for bare, nid in (("blue-artillery-1", "frc:blue-artillery-1"),
                          (x["hit"].engagement_id, f"eng:{x['hit'].engagement_id}"),
                          (x["cor"]["id"], f"vec:{x['cor']['id']}")):
            ent = svc.entity(bare)
            assert ent["id"] == nid and ent["fields"]["simulated"] is True
            assert ent["provenance"]["register"] == "scenario"
            assert ent["provenance"]["session_id"] == SID
            assert "raw" not in ent
        rel = svc.entity(f"eng:{x['hit'].engagement_id}")["related"]
        assert {(r["id"], r["kind"]) for r in rel} >= {
            (f"trk:{x['t_guns'].track_id}", "attacks"), ("frc:blue-artillery-1", "launched_by")}

    def test_routes_take_truth(self):
        sc, _x = standard()
        app = FastAPI()
        app.include_router(ig.intel_router(service(sc), _auth("wg")))
        h = {"Authorization": "Bearer wg"}
        with TestClient(app) as tc:
            blue = tc.get("/intel/graph", headers=h).json()
            umpire = tc.get("/intel/graph?truth=1", headers=h).json()
            assert blue["meta"]["wargame"]["truth_view"] is False
            assert umpire["meta"]["wargame"]["truth_view"] is True
            assert node(umpire, "frc:red-sam-1") and not node(blue, "frc:red-sam-1")
            assert tc.get("/intel/entity/frc:red-sam-1", headers=h).status_code == 404
            r = tc.get("/intel/entity/frc:red-sam-1?truth=1", headers=h)
            assert r.status_code == 200 and r.json()["fields"]["side"] == "red"
            for path in ("/intel/graph?truth=2", "/intel/entity/frc:x?truth=yes"):
                r = tc.get(path, headers=h)
                assert r.status_code == 422
                assert r.json() == {"error": "invalid_truth", "allowed": ["0", "1"]}
            assert tc.get("/intel/graph?truth=1").status_code == 401

    def test_the_isr_pin_meta_without_a_session(self):
        srv = SimpleNamespace(theater=DEFAULT, theater_epoch=0, sites=None,
                              wargame=wg.WargameEngine(SimpleNamespace(), run_thread=False))
        svc = ig.IntelService(SimpleNamespace(), srv)
        g = svc.graph()
        assert g["meta"]["wargame"] == {"active": False, "last": None}
        assert not [n for n in g["nodes"] if n["type"] in isc.WARGAME_TYPES]
        assert isc.SESSION_CAVEAT not in g["meta"]["caveats"]


class TestContracts:
    def test_prefixes(self):
        assert {ig.TYPE_PREFIX[t] for t in isc.WARGAME_TYPES} == {"frc", "eng", "vec"}
        assert ig.PREFIX_TYPE["frc"] == "force" and ig.PREFIX_TYPE["eng"] == "engagement"
        assert ig.PREFIX_TYPE["vec"] == "vector"
        assert len(ig.TYPE_PREFIX) == 14 == len(ig.PREFIX_TYPE)

    def test_constants_match_the_engine_and_the_track_store(self):
        assert isc.SESSION_CAVEAT == wg.SESSION_CAVEAT
        assert isc.MASKED_ATTACKER == {"red_shot": wg.RED_AD_HIDDEN,
                                       "red_ground": wg.RED_GROUND_HIDDEN}
        assert isc.SCENARIO_NOTE == targets.SCENARIO_CONTACT_NOTE
        assert isc.MAX_NODES == {"force": wg.GRAPH_FORCES, "engagement": wg.GRAPH_ENGAGEMENTS,
                                 "vector": wg.GRAPH_VECTORS}
        assert isc.OVERLAY_CAPS == {"force": 60, "force_envelope": wg.OVERLAY_ENVELOPES,
                                    "axis": wg.OVERLAY_AXES, "corridor": wg.OVERLAY_CORRIDORS,
                                    "engagement": wg.OVERLAY_ENGAGEMENTS}
        assert set(isc.META_KEYS) == set(wg.WargameEngine(SimpleNamespace())._meta(
            Scn().s, True))
