"""The map's context overlay feed, `GET /intel/overlay` (WG v2 §3.3, A11).

Pure over a duck-typed server (`SimpleNamespace`): no sim, no port, no
network. The route runs on an in-memory FastAPI app through TestClient.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from godseye_uav import intel_graph as ig
from godseye_uav import intel_overlay as ov
from godseye_uav import intel_sites, sites, theaters
from test_intel_graph import DEFAULT, NOW_MS, _auth
from test_intel_sites import BIDI, XSS, Ctx, server, site, siteset

PROPS = {"kind", "id", "label", "category", "protected", "register", "salience",
         "labelled", "simulated", "truth"}
BODY = {"type", "rev", "theater", "attribution", "counts", "omitted", "features", "sites"}


def features(body):
    return body["features"]


class TestRev:
    def test_rev_is_epoch_fetched_revision_truth(self):
        assert ov.overlay_rev(3, 1_727_500_000_000, 17, False) == "3:1727500000000:17:0"
        assert ov.overlay_rev(None, None, None, True) == "0:0:0:1"
        assert ov.overlay_rev("x", float("nan"), -1, 0) == "0:0:-1:0"

    def test_rev_moves_with_epoch_sites_engine_and_truth(self):
        srv = server(siteset([site(0)], fetched=111), epoch=1)
        base = ov.current_rev(srv)
        assert base == "1:111:0:0"
        srv.theater_epoch = 2
        assert ov.current_rev(srv) == "2:111:0:0"
        srv.sites = siteset([site(0)], fetched=222)
        assert ov.current_rev(srv) == "2:222:0:0"
        srv.wargame = SimpleNamespace(revision=17)
        assert ov.current_rev(srv) == "2:222:17:0"
        assert ov.current_rev(srv, truth=True) == "2:222:17:1"

    def test_unchanged_answers_without_a_body(self):
        srv = server(siteset([site(k) for k in range(3)]), epoch=4)
        body = ov.build_overlay(srv)
        assert set(body) == BODY and len(features(body)) == 3
        assert ov.build_overlay(srv, rev=body["rev"]) == {"rev": body["rev"],
                                                          "unchanged": True}
        for other in ("", "4:0:0:0", body["rev"] + "x", "4" * 500):
            assert "features" in ov.build_overlay(srv, rev=other)
        # the same rev under truth=1 is another picture
        assert "features" in ov.build_overlay(srv, truth=True, rev=body["rev"])

    def test_parse_truth(self):
        for v, want in (("0", False), ("1", True), ("true", True), ("FALSE", False),
                        (None, False), (True, True)):
            assert ov.parse_truth(v) is want, v
        for v in ("2", "yes", "", "1.0"):
            assert ov.parse_truth(v) is None, v


class TestBody:
    def test_site_features_are_the_spec_shape(self):
        srv = server(siteset([site(0, cat="airfield"), site(1, cat="medical")]), epoch=3)
        body = ov.build_overlay(srv)
        assert body["type"] == "FeatureCollection"
        assert body["theater"] == {"id": "default", "epoch": 3}
        assert body["attribution"] == ["© OpenStreetMap contributors, ODbL"]
        assert body["counts"] == {"site": 2} and body["omitted"] == {}
        f = features(body)[0]
        assert f["type"] == "Feature" and f["id"] == "sit:default:way/1000"
        assert f["geometry"]["type"] == "Point"
        lon, lat = f["geometry"]["coordinates"]            # GeoJSON [lon, lat]
        assert lat == pytest.approx(site(0).lat) and lon == pytest.approx(site(0).lon)
        assert all(len(str(v).split(".")[-1]) <= 6 for v in (lon, lat))
        p = f["properties"]
        assert set(p) == PROPS
        assert p["kind"] == "site" and p["id"] == f["id"] and p["register"] == "mapped"
        assert p["simulated"] is False and p["truth"] is False
        assert p["salience"] == pytest.approx(0.5) and p["protected"] is False
        med = features(body)[1]["properties"]
        assert med["category"] == "medical" and med["protected"] is True
        assert body["sites"] == {"total": 2, "served": 2, "degraded": False, "reason": None,
                                 "fetched_at_ms": NOW_MS - 60_000}
        json.dumps(body, allow_nan=False)

    def test_every_site_is_served_most_salient_first_and_forty_are_labelled(self):
        cats = sites.CATEGORIES
        srv = server(siteset([site(k, cat=cats[k % len(cats)]) for k in range(300)]))
        body = ov.build_overlay(srv)
        fs = features(body)
        assert len(fs) == 300 and body["counts"] == {"site": 300} and body["omitted"] == {}
        sal = [f["properties"]["salience"] for f in fs]
        assert sal == sorted(sal, reverse=True)
        assert [f["properties"]["labelled"] for f in fs] == [True] * 40 + [False] * 260
        assert len({f["id"] for f in fs}) == 300

    def test_more_than_the_cap_is_counted(self):
        srv = server(siteset([site(k) for k in range(ov.MAX_OVERLAY_SITES + 7)]))
        body = ov.build_overlay(srv)
        assert len(features(body)) == ov.MAX_OVERLAY_SITES
        assert body["omitted"] == {"site": 7} and body["sites"]["total"] == 307

    def test_a_maximum_load_body_is_under_400_kb(self):
        items = [sites.Site("relation", 10**12 + k, "military_base", "military=base",
                            "N" * 160, site(k).lat, site(k).lon, site(k).bounds,
                            tags={t: "X" * 160 for t in sites.TAG_WHITELIST}, tags_total=40)
                 for k in range(ov.MAX_OVERLAY_SITES)]
        tid = "dyn-" + "a" * 24 + "-abcdef"
        body = ov.build_overlay(server(siteset(items), theater=SimpleNamespace(
            id=tid, bbox=DEFAULT.bbox)))
        assert len(features(body)) == ov.MAX_OVERLAY_SITES
        assert ov.body_size(body) <= ov.OVERLAY_MAX_BYTES
        assert all(len(f["properties"]["label"]) <= 80 for f in features(body))

    def test_geodata_off_and_no_server(self):
        off = server(sites.empty_set(sites.GEODATA_OFF_REASON, bbox=DEFAULT.bbox()))
        body = ov.build_overlay(off)
        assert features(body) == [] and body["attribution"] == [] and body["counts"] == {}
        assert body["sites"]["degraded"] is True and body["sites"]["reason"] == "map data is off"
        none = ov.build_overlay(None)
        assert none["features"] == [] and none["theater"] == {"id": None, "epoch": None}
        assert none["sites"]["reason"] == intel_sites.NO_SOURCE_REASON
        assert none["rev"] == "0:0:0:0"

    def test_a_switch_in_flight_never_labels_old_sites_with_the_new_id(self):
        """`srv.theater` and the epoch move before `srv.sites` (theater_switch
        step h): a read in between serves no sites, and the next rev differs."""
        far = theaters.get("iran-isfahan")
        srv = server(siteset([site(0)], fetched=111), epoch=1)
        first = ov.build_overlay(srv)
        srv.theater, srv.theater_epoch = far, 2                   # mid-switch
        mid = ov.build_overlay(srv, rev=first["rev"])
        assert features(mid) == [] and mid["theater"] == {"id": "iran-isfahan", "epoch": 2}
        assert mid["sites"]["reason"] == intel_sites.STALE_REASON
        srv.sites = siteset([site(0, lat=far.home_lat, lon=far.home_lon)],
                            bbox=far.bbox(), fetched=222)
        done = ov.build_overlay(srv, rev=mid["rev"])
        assert done["rev"] != mid["rev"]
        assert [f["id"] for f in features(done)] == ["sit:iran-isfahan:way/1000"]

    def test_untrusted_labels_are_text_and_bidi_free(self):
        body = ov.build_overlay(server(siteset([site(0, name=XSS),
                                                site(1, name=BIDI + " port")])))
        labels = [f["properties"]["label"] for f in features(body)]
        assert XSS in labels and "evil port" in labels
        blob = json.dumps(body, ensure_ascii=False)
        assert not any(ch in blob for ch in theaters.BIDI_CONTROLS)

    def test_a_malformed_site_is_skipped_not_fatal(self):
        bad = SimpleNamespace(osm_type="way", osm_id=5, category="dam", lat=None, lon=None,
                              bounds=None, label="x", salience=0.6, protected=False,
                              graph_id=lambda tid: f"sit:{tid}:way/5")
        ss = SimpleNamespace(sites=(bad, site(0)), bbox=DEFAULT.bbox(), fetched_at_ms=1,
                             real=True, reason=None, degraded=False,
                             top=lambda n: [bad, site(0)][:n])
        body = ov.build_overlay(server(ss))
        assert [f["id"] for f in features(body)] == ["sit:default:way/1000"]
        assert body["omitted"] == {"site": 1}


class TestRoute:
    @pytest.fixture()
    def http(self):
        srv = server(siteset([site(k) for k in range(5)]), epoch=6)
        app = FastAPI()
        app.include_router(ig.intel_router(ig.IntelService(Ctx(), srv), _auth("ov")))
        with TestClient(app) as tc:
            yield tc

    H = {"Authorization": "Bearer ov"}  # noqa: RUF012 - a constant header

    def test_bearer_required(self, http):
        assert http.get("/intel/overlay").status_code == 401
        assert http.get("/intel/overlay", headers={"Authorization": "Bearer no"}
                        ).status_code == 401

    def test_body_rev_unchanged_and_truth(self, http):
        r = http.get("/intel/overlay", headers=self.H)
        assert r.status_code == 200
        body = r.json()
        assert body["rev"] == f"6:{NOW_MS - 60_000}:0:0" and len(body["features"]) == 5
        r = http.get(f"/intel/overlay?rev={body['rev']}", headers=self.H)
        assert r.json() == {"rev": body["rev"], "unchanged": True}
        r = http.get(f"/intel/overlay?truth=1&rev={body['rev']}", headers=self.H)
        assert r.json()["rev"].endswith(":1") and "features" in r.json()
        r = http.get("/intel/overlay?truth=maybe", headers=self.H)
        assert r.status_code == 422
        assert r.json() == {"error": "invalid_truth", "allowed": ["0", "1"]}

    def test_graph_meta_rev_is_the_overlay_rev(self, http):
        g = http.get("/intel/graph", headers=self.H).json()
        o = http.get("/intel/overlay", headers=self.H).json()
        assert g["meta"]["overlay_rev"] == o["rev"]
        assert http.get(f"/intel/overlay?rev={g['meta']['overlay_rev']}",
                        headers=self.H).json()["unchanged"] is True
