"""Mapped strategic sites, the keep-out set and open ground (WG v2 §4.1.5, A3).

NOTHING here touches the network: Overpass answers are hand-written fixtures in
Overpass's documented JSON shape (`out tags bb`, `out ids bb`), served by an
injected `fetch`.

What these tests guard:
* one taxonomy drives both the query and the classifier, every category is
  asked for with `out tags bb <cap+1>`, and `bridge=yes` is never asked for;
* node and bounds parsing, dedupe, per-category caps, medical `protected`;
* the keep-out set is uncapped (`out ids bb`) and is only `complete` when
  nothing is in doubt; `blocks()` is exact at the 500 m edge;
* failures are data (`real=False` / `complete=False` plus a reason).
"""
from __future__ import annotations

import json
import math
import pathlib
import re
from urllib.parse import parse_qs

import pytest
from godseye_uav import geo_http, sites
from godseye_uav.geo_http import DiskCache
from godseye_uav.realdata import HttpResponse
from godseye_uav.safety import EARTH_RADIUS_M, haversine_m

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "geodata"
OVERPASS = geo_http.UPSTREAM_URLS["overpass"]
KUMI = geo_http.UPSTREAM_URLS["overpass-kumi"]
BBOX = (46.60, 32.48, 46.68, 32.72)          # (s, w, n, e) around Kherson
BIDI = tuple(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Overpass:
    """A fake Overpass: answers POSTs from a queue (or a function of the query)."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, timeout_s: float, data: bytes | None = None) -> HttpResponse:
        assert data is not None, "Overpass is always a POST with data="
        query = parse_qs(data.decode())["data"][0]
        self.calls.append((url, query))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(answer):
            answer = answer(query)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, HttpResponse):
            return answer
        return HttpResponse(200, json.dumps(answer))


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def test_display_query_asks_every_category_with_cap_plus_one():
    q = sites.overpass_query(BBOX)
    assert q.startswith("[out:json][timeout:25][bbox:46.600000,32.480000,46.680000,32.720000];")
    for category in sites.CATEGORIES:
        block = re.search(rf"/\* {category} \*/ \((.*?)\); out tags bb (\d+);", q)
        assert block, f"{category} missing from the query"
        assert int(block.group(2)) == sites.CAPS[category] + 1
    assert sum(sites.CAPS.values()) == 300
    assert set(sites.CAPS) == set(sites.CATEGORIES) and "other" in sites.ALL_CATEGORIES


def test_bridge_yes_is_never_asked_for_and_never_classified():
    q = sites.overpass_query(BBOX) + "".join(sites.exclusion_queries(BBOX))
    assert '["bridge"' not in q and "bridge=yes" not in q
    assert '["man_made"="bridge"]' in q
    assert sites.classify({"bridge": "yes", "highway": "primary"}) is None
    assert sites.classify({"man_made": "bridge"}) == ("bridge", "man_made=bridge")


@pytest.mark.parametrize("tags,expected", [
    ({"aeroway": "aerodrome"}, "airfield"),
    ({"military": "airfield"}, "airfield"),
    ({"landuse": "military", "aeroway": "aerodrome"}, "airfield"),     # first match wins
    ({"military": "training_area"}, "military_base"),
    ({"harbour": "yes"}, "port"),
    ({"power": "plant"}, "power"),
    ({"power": "line"}, None),
    ({"man_made": "storage_tank", "content": "petroleum"}, "fuel"),
    ({"man_made": "storage_tank", "content": "water"}, None),
    ({"man_made": "tower", "tower:type": "communication"}, "comms"),
    ({"man_made": "tower", "tower:type": "observation"}, None),
    ({"telecom": "data_center"}, "comms"),
    ({"railway": "station", "train": "yes"}, "rail_hub"),
    ({"railway": "station"}, None),
    ({"office": "government"}, "hq_gov"),
    ({"barrier": "border_control"}, "border_crossing"),
    ({"amenity": "hospital"}, "medical"),
    ({"waterway": "dam"}, "dam"),
])
def test_classify_first_matching_category(tags, expected):
    found = sites.classify(tags)
    assert (found[0] if found else None) == expected


def test_invalid_or_oversized_areas_are_refused():
    for bad in [(46.7, 32.4, 46.6, 32.7), (46.6, 32.7, 46.7, 32.4), (10, 10, 13, 11),
                (float("nan"), 1, 2, 3), (1, 2, 3)]:
        with pytest.raises(ValueError):
            sites.overpass_query(bad)
        assert not sites.fetch_sites(bad, cache=None, fetch=Overpass({})).real


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def parsed():
    return sites.parse(load("overpass_sites.json")["elements"])


def test_parse_bounds_nodes_dedupe_and_drops():
    found, capped, dropped = parsed()
    by = {s.osm: s for s in found}
    airport = by["way/24591212"]
    assert [s.osm for s in found].count("way/24591212") == 1            # deduped
    assert airport.bounds == (46.6601, 32.4889, 46.6789, 32.5198)
    assert (airport.lat, airport.lon) == pytest.approx(((46.6601 + 46.6789) / 2,
                                                       (32.4889 + 32.5198) / 2))
    helipad = by["node/5550001"]
    assert helipad.bounds == (46.6402, 32.6021, 46.6402, 32.6021)
    assert helipad.name is None and helipad.label == "Unnamed airfield"
    assert by["relation/7001"].category == "military_base"
    assert by["relation/7001"].subtype == "military=barracks"
    # bridge=yes, the water tank, the subway station and the unlocated way.
    assert {"way/40006", "way/40003", "node/40008", "way/40013"}.isdisjoint(by)
    assert dropped == 4 and capped == {}
    assert by["way/40002"].category == "fuel" and by["node/40004"].category == "comms"


def test_parse_keeps_only_whitelisted_cleaned_tags_and_marks_medical_protected():
    by = {s.osm: s for s in parsed()[0]}
    airport = by["way/24591212"]
    assert set(airport.tags) == {"name", "aeroway", "icao", "iata", "operator"}
    assert airport.tags_total == 8
    assert airport.as_dict()["tags"] == {"name": "Kherson International Airport",
                                         "operator": "Airport authority",
                                         "aeroway": "aerodrome", "icao": "UKOH", "iata": "KHE"}
    port = by["way/30100"]
    assert not any(ch in port.name for ch in BIDI)
    assert "<img src=x onerror=alert(1)>" in port.name
    hospital = by["node/40010"]
    assert hospital.protected and hospital.category in sites.PROTECTED
    assert hospital.as_dict()["protected"] is True and not airport.protected
    assert sites.SITE_EXCLUSION_M == 500.0 and sites.PROTECTED_EXCLUSION_M == 1000.0


def test_parse_caps_each_category_and_keeps_named_sites_first():
    elements = [{"type": "node", "id": i, "lat": 46.61 + i * 1e-4, "lon": 32.6,
                 "tags": {"aeroway": "helipad", **({"name": f"Pad {i}"} if i % 2 else {})}}
                for i in range(1, 32)]
    found, capped, _ = sites.parse(elements)
    assert capped == {"airfield": True}
    assert len(found) == sites.CAPS["airfield"] == 30
    named = [s for s in found if s.name]
    assert len(named) == 16 and found[:16] == named                 # all 16 named kept first


# ---------------------------------------------------------------------------
# fetch_sites / cached_sites / SiteSet
# ---------------------------------------------------------------------------


def test_fetch_sites_posts_one_query_and_caches_24h_at_a_hundredth_of_a_degree():
    cache = geo_http.default_cache(None)
    fake = Overpass(load("overpass_sites.json"))
    got = sites.fetch_sites(BBOX, cache=cache, fetch=fake)
    assert got.real and not got.degraded and got.reason is None
    assert got.total == 14 and got.dropped == 4 and got.fetched_at_ms
    assert fake.calls[0][0] == OVERPASS and fake.calls[0][1] == sites.overpass_query(BBOX)
    nudged = (46.601, 32.481, 46.681, 32.719)       # same key at 0.01 degrees
    again = sites.fetch_sites(nudged, cache=cache, fetch=fake)
    assert len(fake.calls) == 1 and again.total == 14 and again.real
    assert sites.cached_sites(BBOX, cache=cache).total == 14
    refreshed = sites.fetch_sites(BBOX, cache=cache, fetch=fake, refresh=True)
    assert len(fake.calls) == 2 and refreshed.total == 14


def test_overpass_failure_retries_the_mirror_once_then_degrades():
    fake = Overpass(HttpResponse(504, "gateway timeout"), load("overpass_sites.json"))
    got = sites.fetch_sites(BBOX, cache=None, fetch=fake)
    assert [u for u, _ in fake.calls] == [OVERPASS, KUMI] and got.real
    cache = geo_http.default_cache(None)
    dead = Overpass(HttpResponse(429, "busy"), OSError("reset"))
    down = sites.fetch_sites(BBOX, cache=cache, fetch=dead)
    assert [u for u, _ in dead.calls] == [OVERPASS, KUMI]
    assert down.sites == () and not down.real and down.degraded
    assert "HTTP 429" in down.reason and "reset" in down.reason
    assert sites.cached_sites(BBOX, cache=cache).total == 0          # failures never cached


def test_a_remark_means_the_answer_may_be_partial():
    cache = geo_http.default_cache(None)
    got = sites.fetch_sites(BBOX, cache=cache, fetch=Overpass(load("overpass_remark.json")))
    assert got.total == 0 or got.sites                       # whatever parsed is kept...
    assert not got.real and "timed out" in got.reason        # ...but flagged
    assert not sites.cached_sites(BBOX, cache=cache).real


def test_cached_sites_never_fetches_and_says_why_it_is_empty_or_stale():
    class Clock:
        t = 1_000.0

        def __call__(self):
            return self.t

    clock = Clock()
    cache = DiskCache(None, "geodata", 30 * 86_400.0, now=clock)
    miss = sites.cached_sites(BBOX, cache=cache)
    assert miss.sites == () and not miss.real and "not fetched" in miss.reason
    assert "not cached" in sites.cached_sites(BBOX, cache=None).reason
    sites.fetch_sites(BBOX, cache=cache, fetch=Overpass(load("overpass_sites.json")))
    assert sites.cached_sites(BBOX, cache=cache).real
    clock.t += sites.SITES_TTL_S + 1
    stale = sites.cached_sites(BBOX, cache=cache)
    assert stale.total == 14 and not stale.real and "older than 24 h" in stale.reason


def test_siteset_rows_order_filter_near_and_ids():
    got = sites.fetch_sites(BBOX, cache=None, fetch=Overpass(load("overpass_sites.json")))
    body = got.as_dict(limit=40, theater_id="dyn-kherson-abc123")
    assert body["returned"] == body["total"] == 14
    assert body["sites"][0]["salience"] == 1.0
    assert [r["salience"] for r in body["sites"]] == sorted(
        (r["salience"] for r in body["sites"]), reverse=True)
    assert body["sites"][0]["id"].startswith("sit:dyn-kherson-abc123:")
    assert body["counts"]["airfield"] == 2 and body["capped"] == {}
    assert body["attribution"] == "© OpenStreetMap contributors, ODbL"
    assert body["caveat"] == sites.SITES_CAVEAT and body["real"] and not body["degraded"]
    assert all(len(r["tags"]) <= 6 for r in body["sites"])
    only = got.as_dict(category="medical")
    assert [r["osm"] for r in only["sites"]] == ["node/40010"] and "id" not in only["sites"][0]
    near = got.rows(near_lat=46.6360, near_lon=32.6180, limit=3)
    assert near[0]["osm"] == "node/40010" and near[0]["distance_m"] == 0.0
    assert [r["distance_m"] for r in near] == sorted(r["distance_m"] for r in near)
    assert [s.osm for s in got.near(46.6360, 32.6180, 260)] == ["node/40010", "node/40009"]
    assert [s.osm for s in got.near(46.6360, 32.6180, 300)][-1] == "node/40004"
    # Salience first, named before unnamed, then by label.
    assert [s.osm for s in got.top(3)] == ["way/24591212", "relation/7001", "node/5550001"]
    assert all(s.name for s in got.top(5, named_only=True))
    empty = sites.empty_set(sites.GEODATA_OFF_REASON, bbox=BBOX)
    assert empty.as_dict()["reason"] == "map data is off" and empty.degraded


def test_siteset_round_trips_through_the_cache_json():
    got = sites.fetch_sites(BBOX, cache=None, fetch=Overpass(load("overpass_sites.json")))
    back = sites.SiteSet.from_json(json.loads(json.dumps(got.to_json())))
    assert back.sites == got.sites and back.fetched_at_ms == got.fetched_at_ms
    assert back.bbox == got.bbox


# ---------------------------------------------------------------------------
# fetch_exclusion (Phase B keep-out set, built in A3)
# ---------------------------------------------------------------------------


def exclusion_fake(footprints="overpass_exclusion.json",
                   hospitals="overpass_exclusion_hospitals.json") -> Overpass:
    def route(query: str):
        if '(nwr["amenity"="hospital"];)' in query:
            return load(hospitals) if isinstance(hospitals, str) else hospitals
        return load(footprints) if isinstance(footprints, str) else footprints
    return Overpass(route)


def test_exclusion_sends_two_uncapped_ids_bb_queries_over_the_padded_bbox():
    fake = exclusion_fake()
    ex = sites.fetch_exclusion(BBOX, cache=None, fetch=fake)
    assert ex.complete and ex.reason is None
    assert len(ex.rects) == 3 and len(ex.protected) == 1 and ex.element_count == 4
    assert ex.rects[1] == (46.6360, 32.6180, 46.6360, 32.6180)           # node → a point
    footprints, hospitals = (q for _, q in fake.calls)
    for q in (footprints, hospitals):
        assert q.startswith("[out:json][timeout:60][bbox:") and q.endswith("out ids bb;")
        assert "out tags" not in q and not re.search(r"out ids bb \d", q)   # uncapped
        s, w, n, e = (float(v) for v in re.search(r"\[bbox:([^\]]+)\]", q).group(1).split(","))
        assert haversine_m(s, 32.6, BBOX[0], 32.6) == pytest.approx(1000.0, abs=1.0)
        assert haversine_m(n, 32.6, BBOX[2], 32.6) == pytest.approx(1000.0, abs=1.0)
        assert w < BBOX[1] and e > BBOX[3]
    for _, selectors in sites.TAXONOMY:
        for conds in selectors:
            assert f"nwr{sites._selector_ql(conds)};" in footprints
    assert hospitals.count("nwr[") == 1 and '["amenity"="hospital"]' in hospitals
    assert ex.protected_within(46.6360 + 900 / 111_195.0, 32.6180)
    assert not ex.protected_within(46.6360 + 1100 / 111_195.0, 32.6180)
    assert ex.as_dict()["footprints"] == 3 and ex.as_dict()["complete"]


def north_of(lat: float, metres: float) -> float:
    """Latitude `metres` due north along the meridian (the metric safety uses)."""
    return lat + math.degrees(metres / EARTH_RADIUS_M)


def test_blocks_is_exact_at_the_500_m_edge_of_a_ways_bounds():
    ex = sites.fetch_exclusion(BBOX, cache=None, fetch=exclusion_fake())
    s, w, n, e = 46.6601, 32.4889, 46.6789, 32.5198             # way 24591212's bounds
    mid_lat, mid_lon = (s + n) / 2, (w + e) / 2
    assert ex.blocks(north_of(n, 499.0), mid_lon)
    assert not ex.blocks(north_of(n, 501.0), mid_lon)
    assert ex.blocks(mid_lat, mid_lon)                           # inside: distance 0
    east = e + math.degrees(499.0 / (EARTH_RADIUS_M * math.cos(math.radians(mid_lat))))
    assert haversine_m(mid_lat, e, mid_lat, east) == pytest.approx(499.0, abs=0.01)
    assert ex.blocks(mid_lat, east)
    far_east = e + math.degrees(501.0 / (EARTH_RADIUS_M * math.cos(math.radians(mid_lat))))
    assert not ex.blocks(mid_lat, far_east)
    assert ex.blocks(north_of(n, 900.0), mid_lon, margin_m=1000.0)


@pytest.mark.parametrize("fake,why", [
    (lambda: exclusion_fake(footprints="overpass_remark.json"), "remark"),
    (lambda: exclusion_fake(footprints={"elements": [
        {"type": "way", "id": 9}, {"type": "node", "id": 10, "lat": 46.62, "lon": 32.6}]}),
     "no location"),
    (lambda: Overpass(HttpResponse(504, "")), "HTTP 504"),
    (lambda: Overpass({"not_elements": []}), "malformed"),
])
def test_exclusion_is_incomplete_whenever_anything_is_in_doubt(fake, why):
    cache = geo_http.default_cache(None)
    ex = sites.fetch_exclusion(BBOX, cache=cache, fetch=fake())
    assert not ex.complete and why in ex.reason
    again = Overpass(HttpResponse(504, ""))
    sites.fetch_exclusion(BBOX, cache=cache, fetch=again)
    assert again.calls, "an incomplete set must never be served from the cache"


def test_exclusion_is_incomplete_on_the_16_mb_cap_without_retrying(monkeypatch):
    monkeypatch.setattr(geo_http, "MAX_RESPONSE_BYTES", 64)
    fake = exclusion_fake()
    ex = sites.fetch_exclusion(BBOX, cache=None, fetch=fake)
    assert not ex.complete and geo_http.CAP_REASON in ex.reason
    assert KUMI not in [u for u, _ in fake.calls]          # a mirror would hit it too


def test_exclusion_with_geodata_off_is_incomplete_and_fetches_nothing():
    fake = exclusion_fake()
    ex = sites.fetch_exclusion(BBOX, cache=None, fetch=fake, enabled=False)
    assert not ex.complete and ex.reason == "map data is off" and fake.calls == []
    assert not ex.blocks(46.63, 32.6)          # empty, and says so via `complete`


def test_a_complete_exclusion_is_cached():
    cache = geo_http.default_cache(None)
    first = sites.fetch_exclusion(BBOX, cache=cache, fetch=exclusion_fake())
    fake = exclusion_fake()
    second = sites.fetch_exclusion(BBOX, cache=cache, fetch=fake)
    assert fake.calls == [] and second.complete
    assert second.rects == first.rects and second.protected == first.protected


# ---------------------------------------------------------------------------
# open_ground
# ---------------------------------------------------------------------------

CENTRE = (12.97679, 77.59008)       # Bengaluru, the prototype's geocoded centre


def test_open_ground_query_uses_the_three_named_selectors_around_the_centre():
    q = sites.open_ground_query(*CENTRE, 1000.0)
    assert q.startswith("[out:json][timeout:25];")
    at = "(around:1000,12.976790,77.590080)"
    assert f'way["leisure"~"^(park|recreation_ground|pitch)$"]["name"]{at};' in q
    assert f'relation["leisure"~"^(park|recreation_ground|pitch)$"]["name"]{at};' in q
    assert f'way["landuse"~"^(grass|meadow)$"]["name"]{at};' in q
    assert q.endswith("out tags bb 40;")


def test_open_ground_returns_named_candidates_nearest_first_within_the_radius():
    cache = geo_http.default_cache(None)
    fake = Overpass(load("overpass_open_ground.json"))
    got = sites.open_ground(CENTRE, 1000.0, cache=cache, fetch=fake)
    assert [g.name for g in got] == ["MS Building Park", "Cubbon Park"]   # far one dropped
    assert got[0].osm == "way/38872968" and got[0].kind == "leisure=park"
    assert got[0].distance_m < got[1].distance_m <= 1000.0
    assert (got[0].lat, got[0].lon) == pytest.approx(((12.9779 + 12.9790) / 2,
                                                     (77.5868 + 77.5880) / 2))
    assert sites.open_ground(CENTRE, 1000.0, cache=cache, fetch=fake) == got
    assert len(fake.calls) == 1                                        # cached
    assert got[0].as_dict()["osm"] == "way/38872968"


def test_open_ground_failure_or_bad_input_is_an_empty_list():
    assert sites.open_ground(CENTRE, 1000.0, cache=None,
                             fetch=Overpass(HttpResponse(503, ""))) == []
    assert sites.open_ground((95.0, 0.0), 1000.0, cache=None, fetch=Overpass({})) == []
    assert sites.open_ground(CENTRE, 0.0, cache=None, fetch=Overpass({})) == []
