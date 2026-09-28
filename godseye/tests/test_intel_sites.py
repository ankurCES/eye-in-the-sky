"""Mapped sites in the intel graph, and the §3.2 theater block (WG v2 A11).

Pure: every graph here is `build_graph` over hand-made inputs, or an
`IntelService` over a duck-typed server (`SimpleNamespace`), so nothing binds
a port or touches the network. Sites are built as `sites.Site` rows, exactly
what `srv.sites` holds after A3's `fetch_sites`.

Families:
  * site nodes: id grammar, group, salience, label, subtitle, attrs, cap,
    `in_theater` edges (and none pointing AT a site), `meta.sites`, caveats;
  * stale / missing / degraded site sets;
  * the theater block keys and the active `thr:` node, vehicle airframe;
  * the in-process server wins, `invalidate()`;
  * the site inspector (on and off the graph), search, untrusted text;
  * the byte budget at maximum load (sites give way first).
"""
import json
from types import SimpleNamespace

import pytest
from godseye_uav import intel_graph as ig
from godseye_uav import intel_overlay, intel_sites, sites, theater_tools, theaters
from test_intel_graph import (
    DEFAULT,
    NOW_MS,
    NOW_S,
    RUNS,
    assert_well_formed,
    contact,
    live_inputs,
    salute,
    vehicle_row,
)

XSS = "<img src=x onerror=alert(1)>"
BIDI = "\u202eevil\u202c"
S, W, N, E = DEFAULT.bbox()


def site(k=0, *, cat="airfield", name="default", osm_type="way", lat=None, lon=None,
         tags=None, tags_total=None, point=False, subtype=None):
    """One `sites.Site` inside the default AO (a 9 x 9 lattice)."""
    lat = S + (N - S) * ((k % 9) + 0.5) / 9 if lat is None else lat
    lon = W + (E - W) * ((k // 9 % 9) + 0.5) / 9 if lon is None else lon
    bounds = (lat, lon, lat, lon) if point else (lat - 5e-4, lon - 5e-4, lat + 5e-4, lon + 5e-4)
    name = f"Site {k}" if name == "default" else name
    tags = {"name": name, "operator": "Regional authority", "icao": "KXYZ"} \
        if tags is None else tags
    return sites.Site(osm_type, 1000 + k, cat, subtype or f"{cat}=yes", name, lat, lon, bounds,
                      tags=tags, tags_total=len(tags) + 2 if tags_total is None else tags_total)


def siteset(items, *, real=True, reason=None, bbox=None, fetched=NOW_MS - 60_000):
    return sites.SiteSet(tuple(items), real, reason, fetched, {},
                         DEFAULT.bbox() if bbox is None else bbox, 0)


def server(ss=None, *, theater=DEFAULT, epoch=2, **kw):
    """A duck-typed in-process server: what theater_state and the overlay read."""
    return SimpleNamespace(theater=theater, theater_mismatch=None, theater_epoch=epoch,
                           sites=siteset([site(0)]) if ss is None else ss,
                           airframe_id="quad_suas_electric", time_scale=1.0, **kw)


def graph(ss, *, scope="theater", srv=None, **over):
    srv = srv or server(ss)
    return ig.build_graph(live_inputs(theater_state=theater_tools.theater_state(srv),
                                      sites=ss, **over), scope=scope)


def site_nodes(g):
    return [n for n in g["nodes"] if n["type"] == "site"]


class Ctx:
    """The bridge side of IntelService: an unknown bridge theater."""

    def snapshot(self):
        return {"vehicles": [vehicle_row()], "missions": [], "contacts": [], "feeds": {}}

    def theaters(self):
        return {"theaters": theaters.as_payload()["theaters"],
                "active": theaters.active_unknown("not read yet")}


# ===========================================================================
# site nodes
# ===========================================================================

class TestSiteNodes:
    @pytest.mark.parametrize("scope", ig.SCOPES)
    def test_site_nodes_are_well_formed(self, scope):
        g = graph(siteset([site(k, cat=c) for k, c in enumerate(sites.ALL_CATEGORIES)]),
                  scope=scope)
        assert_well_formed(g)
        nodes = site_nodes(g)
        assert len(nodes) == len(sites.ALL_CATEGORIES)
        assert ig.TYPE_PREFIX["site"] == "sit" and ig.PREFIX_TYPE["sit"] == "site"
        for n in nodes:
            cat = n["attrs"]["category"]
            assert n["id"] == f"sit:default:way/{1000 + sites.ALL_CATEGORIES.index(cat)}"
            assert n["group"] == intel_sites.SITE_GROUP[cat]
            assert n["status"] == "ok" and n["ts_ms"] is None
            assert n["salience"] == pytest.approx(sites.SALIENCE[cat] * 0.5)
            assert n["salience"] <= 0.5
            word = sites.SITE_WORDS[cat][0]
            assert n["subtitle"] == f"{word}  Mapped, not verified"
            assert "·" not in n["subtitle"]

    def test_site_group_covers_the_closed_vocabulary(self):
        assert set(intel_sites.SITE_GROUP) == set(sites.ALL_CATEGORIES)
        sectors = set(ig.CATEGORY_GROUP.values()) | {"unclassified"}
        assert set(intel_sites.SITE_GROUP.values()) <= sectors
        assert intel_sites.SITE_GROUP["medical"] == "civilian"
        assert intel_sites.SITE_GROUP["hq_gov"] == "c2"
        assert intel_sites.SITE_GROUP["comms"] == "radar-ew"

    def test_attrs_are_the_spec_set_and_carry_no_control_or_damage(self):
        g = graph(siteset([site(0, cat="airfield"), site(1, cat="medical", point=True)]))
        byid = {n["id"]: n for n in site_nodes(g)}
        air = byid["sit:default:way/1000"]["attrs"]
        assert set(air) == {"category", "subtype", "osm", "bounds", "tags", "tags_total",
                            "source", "register", "fetched_at_ms"}
        assert air["osm"] == {"type": "way", "id": 1000}
        assert air["source"] == "osm" and air["register"] == "mapped"
        assert air["fetched_at_ms"] == NOW_MS - 60_000
        s, w, n, e = air["bounds"]
        assert s < n and w < e                         # [s, w, n, e]
        assert air["tags"] == {"operator": "Regional authority", "icao": "KXYZ"}
        assert air["tags_total"] == 5
        med = byid["sit:default:way/1001"]["attrs"]
        assert med["protected"] is True and "bounds" not in med   # a point has no box
        forbidden = {"control", "controls", "damage", "state", "side", "engageable",
                     "actions", "strike", "target"}
        for n in site_nodes(g):
            assert not forbidden & set(n["attrs"]), n["attrs"]

    def test_tags_are_whitelisted_capped_and_clipped(self):
        tags = {k: "v" * 200 for k in sites.TAG_WHITELIST}
        tags["website"] = "https://example.invalid/"
        n = site_nodes(graph(siteset([site(0, tags=tags)])))[0]["attrs"]
        assert len(n["tags"]) == intel_sites.GRAPH_MAX_TAGS
        assert "name" not in n["tags"] and "website" not in n["tags"]
        assert list(n["tags"]) == [k for k in sites.TAG_WHITELIST if k != "name"][:6]
        assert all(len(v) <= intel_sites.GRAPH_TAG_VALUE_MAX for v in n["tags"].values())

    def test_labels_are_names_or_unnamed_words(self):
        g = graph(siteset([site(0, name="N" * 200), site(1, cat="power", name=None,
                                                          tags={})]))
        byid = {n["id"]: n for n in site_nodes(g)}
        assert len(byid["sit:default:way/1000"]["label"]) == 80
        assert byid["sit:default:way/1000"]["label"].endswith("…")
        assert byid["sit:default:way/1001"]["label"] == "Unnamed power site"

    def test_in_theater_edges_and_none_point_at_a_site(self):
        g = graph(siteset([site(k) for k in range(5)]))
        ids = {n["id"] for n in site_nodes(g)}
        edges = [e for e in g["edges"] if e["a"] in ids or e["b"] in ids]
        assert len(edges) == 5
        assert all(e["kind"] == "in_theater" and e["b"] == "thr:default" for e in edges)
        assert not any(e["b"] in ids for e in g["edges"])

    def test_the_cap_keeps_the_most_salient_sixty(self):
        items = [site(k, cat="medical") for k in range(40)] \
            + [site(100 + k, cat="military_base") for k in range(40)]
        g = graph(siteset(items))
        nodes = site_nodes(g)
        assert len(nodes) == intel_sites.MAX_SITE_NODES == 60
        cats = [n["attrs"]["category"] for n in nodes]
        assert cats.count("military_base") == 40 and cats.count("medical") == 20
        m = g["meta"]["sites"]
        assert (m["total"], m["in_graph"], m["omitted"]) == (80, 60, 20)
        assert g["meta"]["counts"]["site"] == 60


# ===========================================================================
# meta.sites, caveats, degraded / stale / missing sets
# ===========================================================================

META_SITES_KEYS = {"total", "in_graph", "omitted", "degraded", "reason", "fetched_at_ms",
                   "attribution", "caveat"}


class TestMetaSites:
    def test_meta_sites_and_the_odbl_caveat(self):
        g = graph(siteset([site(k) for k in range(3)]))
        m = g["meta"]["sites"]
        assert set(m) == META_SITES_KEYS
        assert m == {"total": 3, "in_graph": 3, "omitted": 0, "degraded": False,
                     "reason": None, "fetched_at_ms": NOW_MS - 60_000,
                     "attribution": "© OpenStreetMap contributors, ODbL",
                     "caveat": sites.SITES_CAVEAT}
        assert sites.SITES_CAVEAT == ("Sites are mapped OpenStreetMap data (ODbL), not an "
                                      "order of battle.")
        assert sites.SITES_CAVEAT in g["meta"]["caveats"]
        # the real-data line still leads (the analyst's overview reads it first)
        assert g["meta"]["caveats"][0].startswith("Real-data layer is off")

    def test_geodata_off_is_an_empty_degraded_set_without_a_caveat(self):
        off = sites.empty_set(sites.GEODATA_OFF_REASON, bbox=DEFAULT.bbox())
        g = graph(off)
        assert site_nodes(g) == []
        m = g["meta"]["sites"]
        assert m["total"] == 0 and m["in_graph"] == 0 and m["degraded"] is True
        assert m["reason"] == "map data is off"
        assert not any("OpenStreetMap" in c or "Map data" in c for c in g["meta"]["caveats"])

    def test_a_degraded_feed_keeps_its_sites_and_says_so(self):
        g = graph(siteset([site(0)], real=False, reason="Overpass: remark: runtime error"))
        assert len(site_nodes(g)) == 1
        assert g["meta"]["sites"]["degraded"] is True
        assert any(c.startswith("Map data feed down (Overpass: remark: runtime error).")
                   and c.endswith("Sites may be missing, not absent.")
                   for c in g["meta"]["caveats"])

    def test_no_server_means_no_sites_and_a_reason(self):
        g = ig.build_graph(live_inputs())
        assert site_nodes(g) == []
        assert g["meta"]["sites"]["reason"] == intel_sites.NO_SOURCE_REASON
        assert g["meta"]["sites"]["total"] == 0
        assert g["meta"]["theater_epoch"] is None
        assert g["meta"]["overlay_rev"] == "0:0:0:0"

    def test_an_unknown_theater_draws_no_sites(self):
        g = ig.build_graph(live_inputs(active_theater=theaters.active_unknown("x"),
                                       sites=siteset([site(0)])))
        assert site_nodes(g) == []
        assert g["meta"]["sites"]["reason"] == intel_sites.NO_THEATER_REASON

    def test_a_stale_set_from_the_previous_theater_is_not_served_under_the_new_id(self):
        far = theaters.get("iran-isfahan")
        old = siteset([site(0, lat=far.home_lat, lon=far.home_lon)], bbox=far.bbox())
        g = graph(old)
        assert site_nodes(g) == []
        assert g["meta"]["sites"]["reason"] == intel_sites.STALE_REASON
        assert g["meta"]["overlay_rev"] == "2:0:0:0"

    def test_boxes_overlap(self):
        assert intel_sites.boxes_overlap([0, 0, 1, 1], [1, 1, 2, 2])     # touching
        assert not intel_sites.boxes_overlap([0, 0, 1, 1], [1.1, 0, 2, 1])
        assert intel_sites.boxes_overlap(None, [0, 0, 1, 1])             # unknown: trust
        assert intel_sites.boxes_overlap([0, 0, 1, 1], "garbage")


# ===========================================================================
# theater block, active node, airframe, epoch and rev
# ===========================================================================

class TestTheaterBlock:
    def test_theater_block_keys(self):
        g = graph(siteset([site(0)]))
        block = g["theater"]
        assert set(block) == set(theater_tools.STATE_KEYS)
        assert block["id"] == "default" and block["known"] is True
        assert block["epoch"] == 2 and block["dynamic"] is False
        assert block["source"] == "preset" and block["state"] == "active"
        assert block["bbox"] == [pytest.approx(v) for v in DEFAULT.bbox()]
        assert set(block["home"]) == {"lat", "lon", "alt_msl_m", "name", "source"}
        assert block["airframe"]["id"] == "quad_suas_electric"
        assert g["meta"]["theater_epoch"] == 2

    def test_the_active_theater_node_carries_the_block(self):
        g = graph(siteset([site(0)]))
        thr = next(n for n in g["nodes"] if n["id"] == "thr:default")
        a = thr["attrs"]
        assert a["active"] is True and a["epoch"] == 2 and a["state"] == "active"
        assert a["home"] == g["theater"]["home"] and a["airframe"] == g["theater"]["airframe"]
        assert a["ground_source"] == g["theater"]["ground_source"]
        inactive = [n for n in g["nodes"] if n["type"] == "theater" and n is not thr]
        assert all("epoch" not in n["attrs"] for n in inactive)

    def test_vehicle_carries_the_airframe(self):
        srv = server(siteset([]))
        srv.airframe_id = "group3_fixed_wing"
        g = graph(siteset([]), srv=srv)
        veh = next(n for n in g["nodes"] if n["type"] == "vehicle")
        assert veh["attrs"]["airframe"] == {"id": "group3_fixed_wing",
                                            "label": theater_tools.airframe_block(
                                                "group3_fixed_wing")["label"]}
        assert "airframe" not in next(n for n in ig.build_graph(live_inputs())["nodes"]
                                      if n["type"] == "vehicle")["attrs"]

    def test_without_a_server_the_bridge_geofence_gives_epoch_and_dynamic(self):
        fence = {"theater": {"id": "default", "label": DEFAULT.label, "epoch": 5,
                             "dynamic": False}}
        g = ig.build_graph(live_inputs(geofence=fence))
        assert g["theater"]["epoch"] == 5 and g["theater"]["dynamic"] is False
        assert g["meta"]["theater_epoch"] == 5
        assert set(g["theater"]) == {"id", "label", "place", "known", "epoch", "dynamic"}
        other = {"theater": {"id": "iran-isfahan", "epoch": 9}}
        assert "epoch" not in ig.build_graph(live_inputs(geofence=other))["theater"]

    def test_overlay_rev_matches_the_overlay_feed(self):
        ss = siteset([site(0)], fetched=1_790_000_000_123)
        srv = server(ss, epoch=7)
        g = graph(ss, srv=srv)
        assert g["meta"]["overlay_rev"] == "7:1790000000123:0:0"
        assert g["meta"]["overlay_rev"] == intel_overlay.current_rev(srv)
        assert intel_overlay.build_overlay(srv, rev=g["meta"]["overlay_rev"]) == {
            "rev": "7:1790000000123:0:0", "unchanged": True}


# ===========================================================================
# IntelService: the in-process server wins, invalidate(), inspector, search
# ===========================================================================

class TestService:
    def test_the_in_process_server_wins_and_supplies_state_and_sites(self):
        class Known(Ctx):
            def theaters(self):
                return {"theaters": theaters.as_payload()["theaters"],
                        "active": theaters.active_from_server(
                            {"id": "iran-isfahan", "label": "stale bridge copy",
                             "ao": [list(p) for p in theaters.get("iran-isfahan").ao],
                             "ground_elevation_msl_m": 1570.0},
                            source="bridge", at_ms=NOW_MS)}

        srv = server(siteset([site(k) for k in range(4)]), epoch=3)
        svc = ig.IntelService(Known(), srv)
        g = svc.graph()
        assert g["theater"]["id"] == "default" and g["theater"]["epoch"] == 3
        assert len(site_nodes(g)) == 4
        assert all(n["id"].startswith("sit:default:") for n in site_nodes(g))

    def test_the_theater_node_time_is_when_it_was_set(self):
        srv = server(siteset([]), epoch=1)
        svc = ig.IntelService(Ctx(), srv)
        svc.clock = lambda: NOW_S
        thr = next(n for n in svc.graph()["nodes"] if n["id"] == "thr:default")
        assert thr["ts_ms"] == NOW_MS                   # never set: learned now
        srv.theater_set_at_ms = NOW_MS - 90_000
        svc.invalidate()
        thr = next(n for n in svc.graph()["nodes"] if n["id"] == "thr:default")
        assert thr["ts_ms"] == NOW_MS - 90_000 and thr["attrs"]["set_at_ms"] == NOW_MS - 90_000

    def test_invalidate_drops_the_cache(self):
        srv = server(siteset([site(0)]), epoch=1)
        svc = ig.IntelService(Ctx(), srv)
        g = svc.graph()
        assert svc.graph() is g                         # cached within the TTL
        srv.theater_epoch = 2
        srv.sites = siteset([site(0), site(1)], fetched=NOW_MS)
        svc.invalidate()
        g2 = svc.graph()
        assert g2 is not g and g2["theater"]["epoch"] == 2 and len(site_nodes(g2)) == 2
        assert svc._cache and svc.graph(scope="all") is not None
        svc.invalidate()
        assert svc._cache == {}

    def test_site_entity_is_context_only(self):
        rows = [salute(f"TRK-{RUNS[0]}-0001", "T72_column_1", "mbt", 47.6405, -122.1390,
                       first_seen=NOW_S - 100, last_seen=NOW_S - 50)]

        class WithTracks(Ctx):
            def track_rows(self):
                return rows

            def snapshot(self):
                return {**super().snapshot(), "contacts": [contact(r) for r in rows]}

        near = site(0, lat=47.6410, lon=-122.1395, name="North base",
                    tags={"name": "North base", "name:en": "North base (en)",
                          "military": "base", "operator": "x", "icao": "KXYZ",
                          "landuse": "military", "barrier": "b", "office": "o"})
        srv = server(siteset([near, site(1, cat="medical")]))
        svc = ig.IntelService(WithTracks(), srv)
        e = svc.entity("sit:default:way/1000")
        f = e["fields"]
        assert e["type"] == "site" and f["category"] == "airfield"
        assert f["category_word"] == "Airfield" and f["register"] == "mapped"
        assert f["name"] == "North base" and f["name_en"] == "North base (en)"
        assert len(f["tags"]) == 8 and f["in_graph"] is True     # every tag here
        assert [c["id"] for c in f["near"]["contacts"]] == [f"trk:TRK-{RUNS[0]}-0001"]
        p = e["provenance"]
        assert p["source"] == "OpenStreetMap contributors, ODbL. Fetched via Overpass."
        assert p["how_we_know"] == intel_sites.HOW_WE_KNOW
        assert {r["kind"] for r in e["related"]} == {"in_theater"}
        blob = json.dumps(e).lower()
        for word in ("engage", "strike", "attack", "damage"):
            assert word not in blob, word
        assert svc.entity("sit:default:way/1001")["fields"]["protected"] is True

    def test_a_site_past_the_cap_still_resolves_for_the_map(self):
        items = [site(k, cat="military_base") for k in range(60)] + [site(60, cat="dam")]
        svc = ig.IntelService(Ctx(), server(siteset(items)))
        assert "sit:default:way/1060" not in {n["id"] for n in svc.graph("all")["nodes"]}
        e = svc.entity("sit:default:way/1060")
        assert e is not None and e["type"] == "site" and e["fields"]["in_graph"] is False
        assert svc.entity("default:way/1060")["id"] == "sit:default:way/1060"   # bare id
        assert svc.entity("default:way/1001")["id"] == "sit:default:way/1001"
        assert svc.entity("sit:default:way/999999") is None
        assert svc.entity("sit:other:way/1001") is None

    def test_search_finds_sites(self):
        svc = ig.IntelService(Ctx(), server(siteset([
            site(0, name="Kherson International Airport",
                 tags={"name": "x", "icao": "UKOH"}),
            site(1, cat="power", name="Riverside substation")])))
        hits = svc.search("kherson", types=["site"])
        assert hits and hits[0]["id"] == "sit:default:way/1000"
        assert hits[0]["subtitle"] == "Airfield  Mapped, not verified"
        for types in (["sites"], ["sit"], ["places"]):
            assert {h["type"] for h in svc.search("substation", types=types)} == {"site"}
        assert svc.search("UKOH")[0]["id"] == "sit:default:way/1000"      # a tag
        places = svc.search("default", types=["places"])
        assert {h["type"] for h in places} <= {"theater", "poi", "site"}


# ===========================================================================
# untrusted text (§3.11)
# ===========================================================================

def test_untrusted_names_and_tags_are_text_and_bidi_free():
    srv = server(siteset([site(0, name=XSS, tags={"name": XSS, "operator": BIDI}),
                          site(1, name=BIDI + " base")]))
    svc = ig.IntelService(Ctx(), srv)
    g = svc.graph()
    byid = {n["id"]: n for n in site_nodes(g)}
    assert byid["sit:default:way/1000"]["label"] == XSS          # literal text, as sent
    assert byid["sit:default:way/1000"]["attrs"]["tags"] == {"operator": "evil"}
    assert byid["sit:default:way/1001"]["label"] == "evil base"
    e = svc.entity("sit:default:way/1000")
    assert e["label"] == XSS and e["fields"]["tags"]["operator"] == "evil"
    blob = json.dumps({"g": g, "e": e}, ensure_ascii=False)
    for ch in theaters.BIDI_CONTROLS:
        assert ch not in blob


# ===========================================================================
# sites give way first when the graph is over budget (§3.2)
# ===========================================================================

class TestSitesGiveWayFirst:
    @staticmethod
    def built(n=60):
        items = [site(k, cat=sites.CATEGORIES[k % 12], name=f"Site number {k:03d}",
                      tags={k2: "value " * 5 for k2 in sites.TAG_WHITELIST})
                 for k in range(n)]
        srv = server(siteset(items))
        b = ig._GraphBuilder(live_inputs(theater_state=theater_tools.theater_state(srv),
                                         sites=srv.sites), "theater")
        b.build()
        return b

    def test_under_budget_nothing_is_trimmed(self):
        b = self.built()
        assert ig.json_size(b.graph) < ig.GRAPH_TARGET_BYTES
        assert "tags_trimmed" not in b.graph["meta"]["sites"]
        assert all(n["attrs"].get("tags") for n in site_nodes(b.graph))

    def test_tags_go_first(self):
        b = self.built()
        size = ig.json_size(b.graph)
        intel_sites.fit_sites_to_budget(b, size - 500)
        assert ig.json_size(b.graph) <= size - 500
        assert b.graph["meta"]["sites"]["tags_trimmed"] is True
        assert "trimmed_for_budget" not in b.graph["meta"]["sites"]
        assert len(site_nodes(b.graph)) == 60
        assert not any("tags" in n["attrs"] for n in site_nodes(b.graph))
        assert all(n["attrs"]["tags_total"] for n in site_nodes(b.graph))

    def test_then_the_least_salient_sites_are_counted_off(self):
        b = self.built()
        budget = ig.json_size(b.graph) - 20_000
        intel_sites.fit_sites_to_budget(b, budget)
        g = b.graph
        assert ig.json_size(g) <= budget
        assert_well_formed(g)
        kept = site_nodes(g)
        m = g["meta"]["sites"]
        assert m["in_graph"] == len(kept) == g["meta"]["counts"]["site"]
        assert m["omitted"] == 60 - len(kept) == m["trimmed_for_budget"] > 0
        least = min(n["salience"] for n in kept)
        dropped = set(b.site_index) - {n["id"] for n in kept}
        assert all(intel_sites.site_salience(b.site_index[i]) <= least for i in dropped)
        assert any(c.startswith(f"{m['trimmed_for_budget']} mapped site(s) were left off")
                   for c in g["meta"]["caveats"])
        assert not any(e["a"] in dropped for e in g["edges"])

    def test_the_floor_keeps_one_row_of_context(self):
        b = self.built()
        intel_sites.fit_sites_to_budget(b, 1_000)
        assert len(site_nodes(b.graph)) == intel_sites.MIN_SITE_NODES
        assert b.graph["meta"]["sites"]["in_graph"] == intel_sites.MIN_SITE_NODES


# ===========================================================================
# the theater inspector's "How we know" (review A, ui)
# ===========================================================================

def test_a_chat_theaters_how_we_know_says_chat_and_names_the_epoch():
    """It used to say a chat theater came from the static table with
    hand-entered anchors, and that the envelope was read once at boot."""
    t = theaters.register_dynamic(theaters.make_dynamic(
        label="Bengaluru centre", place="Bengaluru", center=(12.9716, 77.5946),
        half_extent_m=2500.0, home=(12.9716, 77.5946), home_alt_msl_m=920.0,
        provenance={"proposal_id": "TP-00000001"}))

    def fence_ctx(theater, epoch, dynamic):
        class WithFence(Ctx):
            def geofence_doc(self):
                return {"geofence": [list(p) for p in theater.ao], "home": list(theater.home),
                        "theater": {"id": theater.id, "epoch": epoch, "dynamic": dynamic}}
        return WithFence()

    srv = server(siteset([]), theater=t, epoch=4, theater_set_at_ms=NOW_MS - 60_000)
    p = ig.IntelService(fence_ctx(t, 4, True), srv).entity(f"thr:{t.id}")["provenance"]
    blob = json.dumps(p)
    assert p["table"] == "set from chat (not in the static table)"
    for stale in ("static-table", "static table)", "hand-entered", "read once at boot"):
        assert stale not in blob.replace("not in the static table", ""), stale
    assert p["real_data"]["hydrated"] is False and "chat proposal" in p["real_data"]["note"]
    assert p["envelope_source"] == "uav://safety/geofence (theater epoch 4)"
    preset = ig.IntelService(fence_ctx(DEFAULT, 0, False), server(siteset([]), epoch=0))
    q = preset.entity("thr:default")["provenance"]
    assert q["table"] == "theaters.py static table"
    assert q["real_data"]["source"] == "static-table"
    assert q["envelope_source"] == "uav://safety/geofence (theater epoch 0)"
