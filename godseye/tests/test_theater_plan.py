"""Theater proposals (WG v2 §4.1.2, A4): `theater_plan.py`.

The A4 acceptance list for the proposal half: the clamp table (quad 2940 m,
group 3 25000 m, minimum 1500 m); home selection and its fallbacks (the
open-ground fixture); ground primary, fallback, override and `ground_unknown`;
`envelope_caveats`; `args_match`; the preset path; `ProposalBook`; `preview`
fields; `quick_checks`.

No test touches the network: every upstream answer comes from an injected
`fetch` (`FakeFetch`) routed by URL, fed from A3's hand-written fixtures or
inline JSON in the upstream's documented shape.
"""
from __future__ import annotations

import dataclasses
import json
import math
import pathlib
import threading
import urllib.parse
from types import SimpleNamespace

import pytest
from godseye_uav import geocode, realdata, safety, sites, theater_plan, theater_switch, theaters
from godseye_uav.geo_http import DiskCache
from godseye_uav.realdata import HttpResponse
from godseye_uav.safety import LinkState

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "geodata"
QUAD, GROUP3 = "quad_suas_electric", "group3_fixed_wing"
#: Bengaluru centre (E2E A1's coordinates).
LAT, LON = 12.9716, 77.5946
XSS = "<img src=x onerror=alert(1)>"
BIDI = "\u202eevil\u202c"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


#: Mapped sites near Bengaluru centre: two named inside the AO, one named
#: whose orbit ring leaves a 5 km AO, one unnamed, one medical.
BENGALURU_SITES = {"elements": [
    {"type": "way", "id": 101, "center": {"lat": 12.9600, "lon": 77.5850},
     "bounds": {"minlat": 12.9590, "minlon": 77.5840, "maxlat": 12.9610, "maxlon": 77.5860},
     "tags": {"landuse": "military", "name": f"Parade ground {BIDI} {XSS}"}},
    {"type": "node", "id": 102, "lat": 12.9800, "lon": 77.6000,
     "tags": {"power": "substation", "name": "North substation"}},
    {"type": "node", "id": 103, "lat": 12.9936, "lon": 77.6170,
     "tags": {"aeroway": "helipad", "name": "Edge helipad"}},
    {"type": "node", "id": 104, "lat": 12.9650, "lon": 77.6050,
     "tags": {"power": "substation"}},
    {"type": "node", "id": 105, "lat": 12.9700, "lon": 77.5900,
     "tags": {"amenity": "hospital", "name": "City hospital"}},
]}


class FakeFetch:
    """A `geo_http` fetch double. Routes by upstream; records every call."""

    def __init__(self, *, reearth=None, openmeteo=None, open_ground=None, overpass=None):
        self.answers = {"reearth": reearth, "open-meteo": openmeteo,
                        "open_ground": open_ground, "overpass": overpass}
        self.calls: list[tuple[str, str, str]] = []

    @staticmethod
    def kind(url: str, data: bytes | None) -> str:
        if "reearth" in url:
            return "reearth"
        if "open-meteo" in url:
            return "open-meteo"
        body = urllib.parse.unquote_plus((data or b"").decode("utf-8"))
        return "open_ground" if "leisure" in body else "overpass"

    def __call__(self, url, timeout_s, data=None):
        kind = self.kind(url, data)
        self.calls.append((kind, url, urllib.parse.unquote_plus((data or b"").decode("utf-8"))))
        answer = self.answers[kind]
        if answer is None:
            return HttpResponse(503, "unavailable")
        if isinstance(answer, HttpResponse):
            return answer
        return HttpResponse(200, json.dumps(answer))

    def count(self, kind: str) -> int:
        return sum(1 for k, _, _ in self.calls if k == kind)


def happy_fetch(**over) -> FakeFetch:
    kw = {"reearth": fixture("reearth_heights.json"),
          "openmeteo": fixture("openmeteo_elevation.json"),
          "open_ground": fixture("overpass_open_ground.json"),
          "overpass": BENGALURU_SITES}
    kw.update(over)
    return FakeFetch(**kw)


def cache() -> DiskCache:
    return DiskCache(None, "test", 3600.0)


def build(**kw):
    kw.setdefault("lat", LAT)
    kw.setdefault("lon", LON)
    kw.setdefault("label", "Bengaluru centre")
    kw.setdefault("airframe", QUAD)
    return theater_plan.build_proposal(**kw)


# ------------------------------------------------------------- clamp table --

def test_ao_max_half_is_the_clamped_reach_fraction():
    assert theater_plan.ao_max_half_m(QUAD) == pytest.approx(2940.0)
    assert theater_plan.ao_max_half_m(GROUP3) == pytest.approx(25000.0)
    short = dataclasses.replace(safety.QUAD_SUAS_ELECTRIC, endurance_cruise_s=120.0)
    assert safety.reach_radius_m(short) < 1500.0
    assert theater_plan.ao_max_half_m(short) == pytest.approx(1500.0)


@pytest.mark.parametrize("af, requested, geocoded, half, clamped", [
    (QUAD, None, None, 2500.0, False),          # the airframe default
    (GROUP3, None, None, 15000.0, False),
    (QUAD, 50_000.0, None, 2940.0, True),       # over the reach cap
    (GROUP3, 90_000.0, None, 25000.0, True),    # over the 25 km cap
    (QUAD, 300.0, None, 1500.0, True),          # under the 1.5 km minimum
    (QUAD, None, 17_200.0, 2940.0, True),       # a geocoded city box
    (QUAD, None, 2000.0, 2000.0, False),
    (QUAD, 1800.0, 17_200.0, 1800.0, False),    # the request wins over the box
])
def test_ao_half_extent_clamp_table(af, requested, geocoded, half, clamped):
    got, was = theater_plan.ao_half_extent(af, requested_m=requested, geocoded_half_m=geocoded)
    assert got == pytest.approx(half)
    assert was is clamped


def test_a_clamped_request_says_so_and_keeps_the_original_size():
    p = build(half_extent_m=10_000.0, ground_msl_m=920.0)
    assert p.half_extent_m == pytest.approx(2940.0)
    assert p.clamped_from_km == (20.0, 20.0)
    assert any("reduced to 5.9 × 5.9 km" in c and "Quad, small electric" in c
               for c in p.caveats)
    assert p.theater.provenance["ao"]["clamped"] is True
    small = build(half_extent_m=200.0, ground_msl_m=920.0)
    assert small.half_extent_m == pytest.approx(1500.0)
    assert any("enlarged to the 3.0 × 3.0 km minimum" in c for c in small.caveats)


def test_the_e2e_coordinates_give_a_five_km_square():
    p = build(ground_msl_m=920)
    assert isinstance(p, theater_plan.Proposal)
    assert p.clamped_from_km is None and p.half_extent_m == pytest.approx(2500.0)
    assert p.area_km2 == pytest.approx(25.0, abs=0.1)
    s, w, n, e = p.result["theater"]["bbox"]
    assert (s + n) / 2 == pytest.approx(LAT, abs=1e-6)
    assert (w + e) / 2 == pytest.approx(LON, abs=1e-6)
    assert [round(v, 1) for v in p.result["theater"]["ao_km"]] == [5.0, 5.0]


# ------------------------------------------------------- envelope caveats --

def test_envelope_caveats_for_the_quad_ao():
    out = theater_plan.envelope_caveats(2940.0, QUAD)
    assert out[0] == "A long-range air-defence ring (75 km) would cover all of this 5.9 km AO."
    assert "medium-range air-defence (24 km)" in out[1]
    assert "shoulder-launched air-defence (5 km)" in out[1]
    joined = " ".join(out)
    # aircraft and boats are not rings; a 2.5 km gun ring does not cover 5.9 km
    assert "interception" not in joined and "patrol" not in joined
    assert "anti-aircraft gun" not in joined
    assert theater_plan.TERRAIN_MASKING_OFF in out
    assert realdata.MAPPED_DATA_CAVEAT in out
    assert theater_plan.FIXED_WING_CAVEAT not in out


def test_envelope_caveats_for_the_group3_ao_and_masking():
    out = theater_plan.envelope_caveats(25000.0, GROUP3, terrain_masking=True)
    assert out[0] == "A long-range air-defence ring (75 km) would cover all of this 50.0 km AO."
    assert not any(c.startswith("Shorter rings") for c in out)
    assert theater_plan.TERRAIN_MASKING_ON in out
    assert out[-1] == theater_plan.FIXED_WING_CAVEAT


def test_envelope_caveats_is_pure_and_quiet_for_a_huge_ao():
    a = theater_plan.envelope_caveats(60_000.0, GROUP3)
    assert a == theater_plan.envelope_caveats(60_000.0, GROUP3)
    assert not any("ring" in c for c in a)


# ------------------------------------------------------------ ProposalBook --

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_proposal_book_ttl_max_and_ids():
    clock = Clock()
    book = theater_plan.ProposalBook(max=16, ttl_s=1800.0, now=clock)
    p = build(ground_msl_m=920.0)
    assert p.proposal_id.startswith("TP-") and len(p.proposal_id) == 11
    stored = book.put(p)
    assert book.get(p.proposal_id) is stored and stored.expires_at_s == 2800.0
    clock.t += 1799.0
    assert book.get(p.proposal_id) is stored and not stored.expired(clock.t)
    clock.t += 1.0
    assert book.get(p.proposal_id) is None and len(book) == 0
    ids = [book.put(dataclasses.replace(p, proposal_id=f"TP-{i:08x}")).proposal_id
           for i in range(17)]
    assert len(book) == 16
    assert book.get(ids[0]) is None and book.get(ids[-1]) is not None
    assert book.get(None) is None and book.get(123) is None
    with pytest.raises(ValueError):
        theater_plan.ProposalBook(max=0)


# ------------------------------------------------------------------- home --

def test_home_is_the_nearest_named_open_ground_with_room_to_the_edge():
    f = happy_fetch()
    p = build(geodata=True, cache=cache(), fetch=f)
    assert isinstance(p, theater_plan.Proposal), p
    assert p.home["source"] == "overpass-open-ground"
    assert p.home["name"] == "Cubbon Park" and p.home["osm"] == "way/22895320"
    assert p.home["distance_m"] < 0.4 * p.half_extent_m
    assert (p.theater.home_lat, p.theater.home_lon) == (p.home["lat"], p.home["lon"])
    assert p.theater.provenance["home"]["source"] == "overpass-open-ground"
    assert f.count("open_ground") == 1
    # the Overpass query is bounded by 0.4 x half around the centre
    body = next(b for k, _, b in f.calls if k == "open_ground")
    assert f"around:1000,{LAT:.6f},{LON:.6f}" in body


def test_open_ground_too_close_to_the_edge_is_skipped(monkeypatch):
    edge = {"elements": [
        {"type": "way", "id": 9, "bounds": {"minlat": 12.9500, "minlon": 77.5720,
                                            "maxlat": 12.9504, "maxlon": 77.5724},
         "tags": {"leisure": "park", "name": "Corner park"}}]}
    # widen the search so the corner candidate is returned at all
    monkeypatch.setattr(theater_plan, "HOME_SEARCH_FRACTION", 1.5)
    p = build(geodata=True, cache=cache(), fetch=happy_fetch(open_ground=edge))
    assert p.home["source"] == "ao-centre"


def test_home_falls_back_to_the_ao_centre_and_flags_water():
    f = happy_fetch(open_ground={"elements": []}, reearth=None,
                    openmeteo={"elevation": [0.0]})
    p = build(geodata=True, cache=cache(), fetch=f)
    assert p.home["source"] == "ao-centre" and p.home["name"] is None
    assert (p.home["lat"], p.home["lon"]) == pytest.approx(p.theater.center(), abs=1e-6)
    assert theater_plan.HOME_WATER_CAVEAT in p.caveats
    dry = build(geodata=True, cache=cache(), fetch=happy_fetch(open_ground={"elements": []}))
    assert dry.home["source"] == "ao-centre"
    assert theater_plan.HOME_WATER_CAVEAT not in dry.caveats


def test_geodata_off_uses_the_centroid_and_never_fetches():
    f = happy_fetch()
    p = build(geodata=False, fetch=f, ground_msl_m=920.0)
    assert p.home["source"] == "ao-centre" and f.calls == []
    assert p.sites.degraded and p.sites.reason == sites.GEODATA_OFF_REASON
    assert p.theater.pois == ()


def test_an_operator_home_must_be_inside_the_ao():
    p = build(home_lat=12.9800, home_lon=77.6000, ground_msl_m=920.0)
    assert p.home["source"] == "operator"
    assert (p.theater.home_lat, p.theater.home_lon) == (12.98, 77.6)
    out = build(home_lat=13.2, home_lon=77.6, ground_msl_m=920.0)
    assert out["rejected"] is True and out["error"] == "home_outside_ao"
    half = build(home_lat=12.98, ground_msl_m=920.0)
    assert half["error"] == "bad_args"


# ----------------------------------------------------------------- ground --

def test_ground_primary_is_reearth_converted_to_egm96_once():
    f = happy_fetch()
    p = build(geodata=True, cache=cache(), fetch=f)
    g = p.result["ground"]
    assert g["provenance"]["source"] == "reearth"
    assert g["provenance"]["hae_m"] == pytest.approx(838.6)
    assert g["msl_m"] == pytest.approx(925.0, abs=0.5)      # 838.6 - N(-86.4)
    assert p.theater.home_alt_msl_m == g["msl_m"]
    assert g["provenance"]["text"] == theater_plan.GROUND_SOURCE_TEXT["reearth"]
    assert f.count("reearth") == 1 and f.count("open-meteo") == 0


def test_ground_falls_back_to_open_meteo_with_its_caveat():
    f = happy_fetch(reearth=None)
    p = build(geodata=True, cache=cache(), fetch=f)
    assert p.ground_source == "open-meteo" and p.ground_msl_m == 925.0
    assert theater_plan.GROUND_SOURCE_TEXT["open-meteo"] in p.caveats
    assert f.count("reearth") == 1 and f.count("open-meteo") == 1


def test_an_operator_ground_overrides_both_providers():
    f = happy_fetch()
    p = build(geodata=True, cache=cache(), fetch=f, ground_msl_m=901.25)
    assert p.ground_source == "operator" and p.ground_msl_m == 901.25
    assert p.result["ground"]["provenance"]["text"] == "Given in the request (not measured)."
    assert f.count("reearth") == 0 and f.count("open-meteo") == 0


def test_no_ground_anywhere_is_refused():
    out = build(geodata=True, cache=cache(), fetch=happy_fetch(reearth=None, openmeteo=None))
    assert out == {"rejected": True, "error": "ground_unknown",
                   "message": theater_plan.GROUND_UNKNOWN_MESSAGE}
    assert build(geodata=False)["error"] == "ground_unknown"


def test_egress_disabled_without_a_fetch_is_ground_unknown_not_a_socket(monkeypatch):
    monkeypatch.setenv("GODSEYE_NO_EGRESS", "1")
    out = build(geodata=True, cache=cache())
    assert out["error"] == "ground_unknown"


# ------------------------------------------------------------ sites, POIs --

def test_pois_are_named_sites_by_salience_that_fit_the_ao():
    p = build(geodata=True, cache=cache(), fetch=happy_fetch())
    names = [poi.name for poi in p.theater.pois]
    assert len(names) == 3
    assert names[0] == "Parade ground evil <img src=x onerror=alert(1)>"   # bidi stripped
    assert "Edge helipad" not in names          # its orbit ring leaves the AO
    assert "North substation" in names
    assert not theaters.validate([p.theater])
    assert p.result["sites"]["total"] == 5 and p.result["sites"]["degraded"] is False
    assert p.theater.provenance["pois"][0]["source"] == "site"


def test_open_ground_names_fill_the_pois_when_sites_are_down():
    p = build(geodata=True, cache=cache(), fetch=happy_fetch(overpass=None))
    assert p.sites.degraded
    assert any(c.startswith("Mapped sites unavailable") for c in p.caveats)
    assert [poi.name for poi in p.theater.pois][:1] == ["Cubbon Park"]
    assert all(x["source"] == "open-ground" for x in p.theater.provenance["pois"])


# -------------------------------------------------------------- args_match --

def test_args_match_is_exact_within_tolerance():
    p = build(ground_msl_m=920.0)
    args = json.loads(json.dumps(p.set_args))
    assert theater_plan.args_match(p, args) == []
    near = dict(args, ao=[[a + 5e-8, b - 5e-8] for a, b in args["ao"]],
                ground_msl_m=args["ground_msl_m"] + 0.005,
                home_lat=args["home_lat"] + 5e-8)
    assert theater_plan.args_match(p, near) == []
    far = dict(args, ao=[[a + 1e-6, b] for a, b in args["ao"]],
               ground_msl_m=args["ground_msl_m"] + 0.02, label="Elsewhere",
               home_lon=args["home_lon"] + 1e-6)
    assert theater_plan.args_match(p, far) == ["label", "ao", "home_lon", "ground_msl_m"]
    missing = {k: v for k, v in args.items() if k != "airframe"}
    assert theater_plan.args_match(p, missing) == ["airframe"]
    assert "ao" in theater_plan.args_match(p, dict(args, ao=args["ao"][:3]))
    assert "home_lat" in theater_plan.args_match(p, dict(args, home_lat=True))
    assert "proposal_id" in theater_plan.args_match(p, dict(args, proposal_id="TP-x"))
    assert theater_plan.args_match(p, None) == list(theater_plan.SET_ARG_KEYS)


# ------------------------------------------------------------- preset path --

def test_the_preset_path_builds_from_the_table_row():
    f = happy_fetch()
    p = theater_plan.build_proposal(theater_id="iran-isfahan", fetch=f)
    row = theaters.get("iran-isfahan")
    assert p.theater is row and not p.theater.dynamic
    assert p.set_args["theater_id"] == "iran-isfahan"
    assert p.set_args["ground_msl_m"] == row.home_alt_msl_m
    assert p.ground_source == "preset" and p.home["source"] == "preset"
    assert p.geocoder == "Theater table" and p.clamped_from_km is None
    assert p.set_args["ao"] == [[a, b] for a, b in row.ao]
    assert f.calls == []                        # geodata off: nothing fetched
    on = theater_plan.build_proposal(theater_id="iran-isfahan", fetch=f, geodata=True)
    assert f.count("overpass") == 1 and f.count("reearth") == 0
    assert on.sites.total == 5
    out = theater_plan.build_proposal(theater_id="atlantis")
    assert out["rejected"] is True and out["error"] == "unknown_theater"


# ------------------------------------------ geocoded place and provenance --

def photon_fetch(url, timeout_s, data=None):
    assert "photon" in url
    return HttpResponse(200, json.dumps(fixture("photon_kherson.json")))


def test_a_geocoded_place_supplies_centre_box_and_provenance():
    c = cache()
    found = geocode.lookup("Kherson", cache=c, fetch=photon_fetch)
    top = found["candidates"][0]
    p = theater_plan.build_proposal(place_id=top["id"], cache=c, ground_msl_m=50.0,
                                    query="Kherson")
    assert isinstance(p, theater_plan.Proposal), p
    assert p.theater.label == "Kherson"                      # from the cached place
    assert p.center == (46.6354, 32.6169)
    assert p.geocoder == "Photon (OpenStreetMap)" and p.query == "Kherson"
    w, h = geocode.bbox_size_m(tuple(top["bbox"]))
    assert p.clamped_from_km == (round(w / 1000, 1), round(h / 1000, 1))
    assert p.half_extent_m == pytest.approx(2940.0)
    prov = json.loads(json.dumps(p.theater.as_dict()["provenance"]))
    assert set(prov) == {"proposal_id", "center", "ao", "home", "ground", "pois", "sites"}
    assert prov["proposal_id"] == p.proposal_id
    assert prov["center"] == {"lat": 46.6354, "lon": 32.6169, "source": "photon",
                              "place_id": top["id"], "query": "Kherson"}
    assert prov["ao"]["geocoded_half_m"] == pytest.approx(min(w, h) / 2, abs=0.1)
    assert prov["ao"]["reach_m"] == 7350.0 and prov["ao"]["airframe"] == QUAD
    assert prov["ground"]["source"] == "operator" and prov["ground"]["msl_m"] == 50.0
    assert prov["sites"]["attribution"] == sites.ATTRIBUTION


def test_coordinates_without_a_place_are_the_operator_source():
    p = build(ground_msl_m=920.0)
    assert p.theater.provenance["center"]["source"] == "operator"
    assert p.geocoder == "Coordinates"
    by_box = build(ground_msl_m=920.0, bbox=[12.96, 77.58, 12.98, 77.61])
    assert by_box.theater.provenance["ao"]["geocoded_half_m"] == pytest.approx(
        theater_plan.bbox_half_extent_m([12.96, 77.58, 12.98, 77.61]), abs=0.1)
    assert by_box.half_extent_m == pytest.approx(1500.0)       # a 2.2 km box, enlarged


def test_untrusted_label_and_place_are_cleaned_everywhere():
    p = build(label=f"Kherson {BIDI}\n{XSS}", place=f"{XSS} \u2066x\u2069", ground_msl_m=1.0)
    for text in (p.theater.label, p.theater.place, p.set_args["label"],
                 p.result["theater"]["label"], p.result["theater"]["place"]):
        assert not set(text) & theaters.BIDI_CONTROLS and "\n" not in text
    assert p.set_args["label"] == p.theater.label == f"Kherson evil {XSS}"
    assert theaters.DYNAMIC_ID_RE.match(p.theater.id)


@pytest.mark.parametrize("kw, error", [
    ({"lat": None, "lon": None}, "bad_args"),
    ({"lon": None}, "bad_args"),
    ({"lat": "north"}, "bad_args"),
    ({"lat": True}, "bad_args"),
    ({"lat": 91.0}, "bad_args"),
    ({"bbox": [1, 2, 3]}, "bad_args"),
    ({"bbox": [13.0, 77.0, 12.0, 78.0]}, "bad_args"),
    ({"half_extent_m": -5}, "bad_args"),
    ({"airframe": "zeppelin"}, "unknown_airframe"),
    ({"lat": 89.99}, "theater_invalid"),
])
def test_unusable_arguments_are_refused_not_raised(kw, error):
    out = build(ground_msl_m=10.0, **kw)
    assert isinstance(out, dict) and out["rejected"] is True
    assert out["error"] == error and out["message"]


# --------------------------------------------------------- server helpers --

def fake_srv(**over) -> SimpleNamespace:
    """What `preview`/`quick_checks`/`propose` read from a server, and no more."""
    mon = SimpleNamespace(fuel=SimpleNamespace(fuel_pct=94.0,
                                               bingo=SimpleNamespace(tripped=False)),
                          link=SimpleNamespace(state=LinkState.UP))
    queue = SimpleNamespace(active=lambda: None)
    done = threading.Event()
    done.set()
    srv = SimpleNamespace(
        theater=theaters.get("default"), airframe_id=QUAD, monitors={"Drone1": mon},
        _last_tele={"Drone1": {"landed_state": 0}},
        tasking=SimpleNamespace(queue_for=lambda v: queue),
        backend=SimpleNamespace(sim=object()), boot_recovery_done=done,
        theater_listeners=[lambda state: None], geodata_enabled=False, real=None,
        geo_cache=None)
    for key, value in over.items():
        setattr(srv, key, value)
    return srv


def test_propose_stores_in_the_servers_book_and_defaults_the_airframe():
    srv = fake_srv(airframe_id=GROUP3)
    out = theater_plan.propose(srv, {"lat": LAT, "lon": LON, "label": "Wide",
                                     "ground_msl_m": 920.0, "bbox": None})
    assert out["airframe"]["id"] == GROUP3 and out["set_args"]["airframe"] == GROUP3
    assert out["theater"]["ao_km"][0] == pytest.approx(30.0, abs=0.2)   # 15 km default
    stored = srv.theater_proposals.get(out["proposal_id"])
    assert stored is not None and stored.result == out
    assert len(json.dumps(out)) < 8 * 1024
    refused = theater_plan.propose(srv, {"lat": LAT, "lon": LON})
    assert refused["error"] == "ground_unknown" and len(srv.theater_proposals) == 1


PREVIEW_KEYS = {"label", "place", "query", "geocoder", "center", "bbox", "half_extent_m",
                "area_km2", "clamped_from_km", "home", "ground_msl_m", "ground_source",
                "airframe", "previous", "now_after", "resets", "keeps", "sites", "caveats",
                "checks"}
#: A16's REQUIRED_PREVIEW_KEYS: a slip without one of these is Deny-only.
REQUIRED = {"checks", "center", "bbox", "home", "airframe", "ground_msl_m"}


def test_preview_carries_every_field_the_slip_reads():
    srv = fake_srv()
    p = theater_plan.book_of(srv).put(build(ground_msl_m=920.0))
    out = json.loads(json.dumps(theater_plan.preview(srv, p)))
    assert set(out) == PREVIEW_KEYS and REQUIRED <= set(out)
    assert out["center"] == [LAT, LON] and len(out["bbox"]) == 4
    assert out["home"] == {"lat": p.home["lat"], "lon": p.home["lon"], "name": None,
                           "source": "ao-centre", "distance_m": 0.0}
    assert out["ground_msl_m"] == 920.0
    assert out["ground_source"] == "Given in the request (not measured)."
    assert out["airframe"] == {"from": QUAD, "to": QUAD, "label": "Quad, small electric",
                               "reach_m": 7350.0}
    assert out["previous"] == {"id": "default", "label": theaters.get("default").label}
    rows = {r["row"]: r for r in out["now_after"]}
    assert list(rows) == ["Theater", "Airframe", "Fuel", "Map data"]
    assert rows["Theater"]["after"] == "Bengaluru centre"
    assert rows["Fuel"] == {"row": "Fuel", "now": "Drone1 94.0%", "after": "94.0% (kept)"}
    assert rows["Map data"]["now"] == rows["Map data"]["after"] == "Off"
    assert out["resets"] == list(theater_switch.RESETS)
    assert out["keeps"] == list(theater_switch.KEEPS)
    assert out["sites"] == {"total": 0, "degraded": True}
    assert [c["text"] for c in out["checks"]] == [
        "Drone1 on the ground", "Drone1 has no task running", "Drone1 BINGO not latched",
        "Drone1 link up", "Fake simulator", "No wargame running",
        "Restart recovery finished", "Running in the app host", "Proposal still valid"]
    assert all(c["ok"] for c in out["checks"])


def test_preview_says_full_tank_when_the_airframe_changes():
    srv = fake_srv()
    p = theater_plan.book_of(srv).put(build(ground_msl_m=920.0, airframe=GROUP3))
    out = theater_plan.preview(srv, p)
    assert out["airframe"]["from"] == QUAD and out["airframe"]["to"] == GROUP3
    fuel = next(r for r in out["now_after"] if r["row"] == "Fuel")
    assert fuel["after"] == "100% (full tank)"


def _failing(srv, p) -> set[str]:
    return {c["text"] for c in theater_switch.quick_checks(srv, p) if not c["ok"]}


def test_quick_checks_read_every_refusal_from_caches():
    p = build(ground_msl_m=920.0)
    busy = SimpleNamespace(active=lambda: SimpleNamespace(tool="uav_fly_route"))
    latched = SimpleNamespace(fuel=SimpleNamespace(fuel_pct=20.0,
                                                   bingo=SimpleNamespace(tripped=True)),
                              link=SimpleNamespace(state=LinkState.LOAL))
    assert _failing(fake_srv(theater_proposals=theater_plan.ProposalBook()), p) == {
        "Proposal still valid"}
    srv = fake_srv(_last_tele={"Drone1": {"landed_state": 1}},
                   tasking=SimpleNamespace(queue_for=lambda v: busy),
                   monitors={"Drone1": latched}, backend=SimpleNamespace(sim=None),
                   wargame=SimpleNamespace(active=True, starting=False),
                   boot_recovery_done=threading.Event(), theater_listeners=[])
    theater_plan.book_of(srv).put(p)
    assert _failing(srv, p) == {
        "Drone1 on the ground", "Drone1 has no task running", "Drone1 BINGO not latched",
        "Drone1 link up", "Fake simulator", "No wargame running",
        "Restart recovery finished", "Running in the app host"}
    empty = fake_srv(monitors={}, _last_tele={})
    theater_plan.book_of(empty).put(p)
    assert _failing(empty, p) == {"Drone status known"}


def test_a_pending_link_and_an_unticked_drone_are_not_green():
    p = build(ground_msl_m=920.0)
    mon = SimpleNamespace(fuel=SimpleNamespace(fuel_pct=90.0,
                                               bingo=SimpleNamespace(tripped=False)),
                          link=SimpleNamespace(state=LinkState.PENDING))
    srv = fake_srv(monitors={"Drone1": mon, "Drone2": mon}, _last_tele={})
    theater_plan.book_of(srv).put(p)
    assert _failing(srv, p) == {"Drone1 on the ground", "Drone1 link up",
                                "Drone2 on the ground", "Drone2 link up"}
    assert math.isfinite(p.area_km2)
