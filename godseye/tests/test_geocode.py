"""geo_http plumbing and the Photon → Nominatim geocoder (WG v2 §4.1.5, A3).

NOTHING here touches the network. Every upstream answer is a hand-written
fixture in the upstream's documented shape (tests/fixtures/geodata/), served by
an injected `fetch`. The one path that exercises the REAL client replaces
`geo_http.http_fetch` itself, so even a regression cannot open a socket.

What these tests guard:
* the two inverted bbox orders (Photon `extent [W,N,E,S]`, Nominatim
  `boundingbox ["S","N","W","E"]`) both land on `(s, w, n, e)`;
* the order coordinates → theater table → Photon → Nominatim, and the
  fallback on a Photon failure or an empty answer;
* untrusted names lose their bidi controls but keep their literal text;
* politeness: the gate spaces real requests, `GODSEYE_NO_EGRESS=1` refuses
  before any socket, `CallBudget` refuses a flood of uncached lookups.
"""
from __future__ import annotations

import json
import pathlib
import socket
from urllib.parse import parse_qs, urlparse

import pytest
from godseye_uav import geo_http, geocode
from godseye_uav.geo_http import CallBudget, DiskCache
from godseye_uav.realdata import HttpResponse, RateGate, RealDataUnavailable

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "geodata"
BIDI = ("\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
        "\u2066", "\u2067", "\u2068", "\u2069")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeUpstream:
    """Routes a URL prefix to a canned body; records every call."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, bytes | None]] = []

    def __call__(self, url: str, timeout_s: float, data: bytes | None = None) -> HttpResponse:
        self.calls.append((url, data))
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                if isinstance(answer, BaseException):
                    raise answer
                if isinstance(answer, HttpResponse):
                    return answer
                return HttpResponse(200, answer if isinstance(answer, str) else json.dumps(answer))
        raise AssertionError(f"unexpected upstream call {url}")

    def hosts(self) -> list[str]:
        return [urlparse(url).netloc for url, _ in self.calls]


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


PHOTON = geo_http.UPSTREAM_URLS["photon"]
NOMINATIM = geo_http.UPSTREAM_URLS["nominatim"]


# ---------------------------------------------------------------------------
# geo_http: gate, egress, fetch_json
# ---------------------------------------------------------------------------


def test_gate_is_one_process_wide_rate_gate_per_upstream():
    assert geo_http.gate("photon") is geo_http.gate("photon")
    assert geo_http.gate("photon") is not geo_http.gate("nominatim")
    for upstream, spacing in geo_http.MIN_SPACING_S.items():
        assert isinstance(geo_http.gate(upstream), RateGate)
        assert geo_http.gate(upstream).min_interval_s == spacing
    assert geo_http.MIN_SPACING_S["nominatim"] == 1.1
    assert geo_http.MIN_SPACING_S["overpass"] == 5.0
    with pytest.raises(ValueError):
        geo_http.gate("example.com")


def test_rate_gate_waits_the_spacing_on_an_injected_clock():
    clock = Clock()
    gate = RateGate(1.1, now=clock, sleep=clock.sleep)
    assert gate.wait() == 0.0
    assert gate.wait() == pytest.approx(1.1)
    clock.t += 5.0
    assert gate.wait() == 0.0
    assert clock.slept == [pytest.approx(1.1)]


def test_real_client_path_waits_on_the_upstream_gate(monkeypatch):
    monkeypatch.delenv(geo_http.NO_EGRESS_ENV, raising=False)
    clock = Clock()
    monkeypatch.setitem(geo_http._GATES, "nominatim",
                        RateGate(1.1, now=clock, sleep=clock.sleep))
    seen = []

    def fake_http(url, timeout_s, data=None):
        seen.append((url, data))
        return HttpResponse(200, "[]")

    monkeypatch.setattr(geo_http, "http_fetch", fake_http)
    for _ in range(2):
        assert geo_http.fetch_json("nominatim", NOMINATIM + "?q=x", timeout_s=1.0) == []
    assert len(seen) == 2
    assert clock.slept == [pytest.approx(1.1)]


def test_no_egress_refuses_before_the_gate_or_any_socket(monkeypatch):
    monkeypatch.setenv(geo_http.NO_EGRESS_ENV, "1")

    def boom(*a, **k):
        raise AssertionError("a socket was opened under GODSEYE_NO_EGRESS=1")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(geo_http, "gate", boom)
    with pytest.raises(RealDataUnavailable) as err:
        geo_http.fetch_json("photon", PHOTON + "?q=Kherson", timeout_s=1.0)
    assert err.value.reason == geo_http.EGRESS_DISABLED == "egress disabled"
    with pytest.raises(RealDataUnavailable):
        geo_http.http_fetch(PHOTON, 1.0)


def test_an_injected_fetch_is_a_double_and_skips_gate_and_egress(monkeypatch):
    monkeypatch.setenv(geo_http.NO_EGRESS_ENV, "1")
    monkeypatch.setattr(geo_http, "gate", lambda u: pytest.fail("gated a test double"))
    fake = FakeUpstream({PHOTON: fixture("photon_empty.json")})
    assert geo_http.fetch_json("photon", PHOTON, timeout_s=1.0, fetch=fake)["features"] == []


def test_fetch_json_posts_a_form_body_and_turns_every_failure_into_data(monkeypatch):
    url = geo_http.UPSTREAM_URLS["overpass"]
    fake = FakeUpstream({url: {"elements": []}})
    geo_http.fetch_json("overpass", url, timeout_s=1.0, data={"data": "[out:json];"}, fetch=fake)
    assert parse_qs(fake.calls[0][1].decode())["data"] == ["[out:json];"]

    def answer(resp):
        return lambda u, t, data=None: resp

    cases = [(HttpResponse(503, "busy"), "HTTP 503"),
             (HttpResponse(429, "slow down"), "HTTP 429 (rate limited)"),
             (HttpResponse(200, "<html>"), "unparsable response")]
    for resp, reason in cases:
        with pytest.raises(RealDataUnavailable) as err:
            geo_http.fetch_json("photon", PHOTON, timeout_s=1.0, fetch=answer(resp))
        assert err.value.feed == "photon" and err.value.reason.startswith(reason)

    def raises(u, t, data=None):
        raise OSError("connection reset")

    with pytest.raises(RealDataUnavailable, match="OSError: connection reset"):
        geo_http.fetch_json("photon", PHOTON, timeout_s=1.0, fetch=raises)
    monkeypatch.setattr(geo_http, "MAX_RESPONSE_BYTES", 10)
    with pytest.raises(RealDataUnavailable) as err:
        geo_http.fetch_json("photon", PHOTON, timeout_s=1.0,
                            fetch=answer(HttpResponse(200, json.dumps({"x": "y" * 20}))))
    assert err.value.reason == geo_http.CAP_REASON


def test_user_agent_identifies_the_app_and_the_contact(monkeypatch):
    monkeypatch.delenv(geo_http.CONTACT_ENV, raising=False)
    assert geo_http.user_agent().startswith("EyeInTheSky/")
    assert geo_http.user_agent().endswith("(+godseye; contact: unset)")
    monkeypatch.setenv(geo_http.CONTACT_ENV, "ops@example.invalid")
    assert "contact: ops@example.invalid" in geo_http.user_agent()


def test_clean_text_strips_bidi_and_controls_but_keeps_literal_markup():
    raw = "A\u202eevil\u202c\n<img src=x onerror=alert(1)>\x00\u2066z\u2069"
    out = geo_http.clean_text(raw)
    assert not any(ch in out for ch in BIDI) and "\x00" not in out and "\n" not in out
    assert "<img src=x onerror=alert(1)>" in out
    assert out.startswith("Aevil ")
    assert len(geo_http.clean_text("x" * 500)) == 160
    assert geo_http.clean_text(None) == ""


# ---------------------------------------------------------------------------
# DiskCache and CallBudget
# ---------------------------------------------------------------------------


def test_disk_cache_ttl_uses_the_injected_clock_and_a_per_read_override():
    clock = Clock()
    cache = DiskCache(None, "geodata", 60.0, now=clock)
    cache.put("k", {"v": [1, 2]})
    assert cache.get("k") == {"v": [1, 2]}
    assert cache.get_entry("k") == (1000.0, {"v": [1, 2]})
    clock.t += 59.0
    assert cache.get("k") == {"v": [1, 2]}
    clock.t += 1.0
    assert cache.get("k") is None                       # expired at exactly the TTL
    assert cache.get("k", ttl_s=3600.0) == {"v": [1, 2]}  # a longer-lived reader
    assert cache.get("missing") is None


def test_disk_cache_writes_atomically_and_survives_a_new_instance(tmp_path, monkeypatch):
    clock = Clock()
    cache = DiskCache(tmp_path, "geodata", 3600.0, now=clock)
    cache.put("sites:tx1:1.00,2.00,3.00,4.00", {"n": 1})
    path = cache.path_for("sites:tx1:1.00,2.00,3.00,4.00")
    assert path.parent == tmp_path / "geodata" and path.exists()
    assert json.loads(path.read_text())["value"] == {"n": 1}
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".tmp-")]
    fresh = DiskCache(tmp_path, "geodata", 3600.0, now=clock)
    assert fresh.get("sites:tx1:1.00,2.00,3.00,4.00") == {"n": 1}

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(geo_http.os, "replace", failing_replace)
    cache.put("sites:tx1:1.00,2.00,3.00,4.00", {"n": 2})     # never raises
    assert json.loads(path.read_text())["value"] == {"n": 1}  # old file intact
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".tmp-")]
    assert cache.get("sites:tx1:1.00,2.00,3.00,4.00") == {"n": 2}   # memory still serves


def test_disk_cache_ignores_a_file_that_answers_for_another_key(tmp_path):
    cache = DiskCache(tmp_path, "geodata", 3600.0)
    cache.put("a", 1)
    doc = json.loads(cache.path_for("a").read_text())
    doc["key"] = "b"
    cache.path_for("a").write_text(json.dumps(doc))
    assert DiskCache(tmp_path, "geodata", 3600.0).get("a") is None


def test_memory_only_cache_writes_no_files(tmp_path):
    cache = geo_http.default_cache(None)
    cache.put("x", 1)
    assert cache.get("x") == 1 and cache.path_for("x") is None
    assert list(tmp_path.iterdir()) == []


def test_call_budget_is_a_sliding_window():
    clock = Clock()
    budget = CallBudget(2, 600.0, now=clock)
    assert budget.take() and budget.take()
    assert not budget.take() and budget.remaining() == 0
    clock.t += 599.0
    assert not budget.take()
    clock.t += 1.0                    # both earlier takes left the window
    assert budget.take() and budget.remaining() == 1
    with pytest.raises(ValueError):
        CallBudget(0, 10.0)


# ---------------------------------------------------------------------------
# Parsing: the two bbox orders
# ---------------------------------------------------------------------------

KHERSON_BBOX = (46.5835, 32.4829, 46.7254, 32.7412)   # (s, w, n, e)


def test_photon_parses_lon_lat_and_the_w_n_e_s_extent():
    places = geocode.parse_photon(json.loads(fixture("photon_kherson.json")))
    assert [p.id for p in places] == ["photon:relation/1573441", "photon:node/337010207"]
    city = places[0]
    assert (city.lat, city.lon) == (46.6354, 32.6169)
    assert city.bbox == KHERSON_BBOX
    assert city.label == "Kherson, Kherson Oblast, Ukraine"
    assert city.kind == "city" and city.osm == "relation/1573441"
    assert places[1].bbox is None
    d = city.as_dict()
    assert d["bbox"] == list(KHERSON_BBOX) and d["geocoder"] == "Photon (OpenStreetMap)"
    assert d["size_km"][0] == pytest.approx(19.8, abs=0.2)
    assert d["size_km"][1] == pytest.approx(15.8, abs=0.2)
    assert city.geocoded_half_m() == pytest.approx(min(city.size_m()) / 2.0)


def test_nominatim_parses_string_coords_and_the_s_n_w_e_boundingbox():
    places = geocode.parse_nominatim(json.loads(fixture("nominatim_kherson.json")))
    assert [p.id for p in places] == ["nominatim:relation/1573441"]   # broken row skipped
    city = places[0]
    assert (city.lat, city.lon) == (46.6354, 32.6169)
    assert city.bbox == KHERSON_BBOX                                 # same box as Photon's
    assert city.name == "Kherson" and city.kind == "city"
    assert city.label.startswith("Kherson, Kherson Urban Hromada")


def test_malformed_payloads_raise_real_data_unavailable():
    with pytest.raises(RealDataUnavailable):
        geocode.parse_photon([])
    with pytest.raises(RealDataUnavailable):
        geocode.parse_nominatim({"error": "x"})


def test_untrusted_names_are_bidi_stripped_but_keep_their_literal_text():
    places = geocode.parse_photon(json.loads(fixture("photon_kherson.json")))
    station = places[1].as_dict()
    for text in (station["name"], station["label"]):
        assert not any(ch in text for ch in BIDI)
        assert "<img src=x onerror=alert(1)>" in text
        assert len(text) <= geocode.NAME_MAX


# ---------------------------------------------------------------------------
# lookup(): coordinates → theater table → Photon → Nominatim
# ---------------------------------------------------------------------------


def test_photon_answers_first_and_is_attributed():
    fake = FakeUpstream({PHOTON: fixture("photon_kherson.json")})
    out = geocode.lookup("Kherson", limit=5, fetch=fake)
    assert fake.hosts() == ["photon.komoot.io"]
    url = urlparse(fake.calls[0][0])
    assert f"{url.scheme}://{url.netloc}{url.path}" == PHOTON
    assert parse_qs(url.query) == {"q": ["Kherson"], "limit": ["5"]}
    assert out["candidates"][0]["id"] == "photon:relation/1573441"
    prov = out["provenance"]
    assert prov["real"] and prov["source"] == "photon" and not prov["cached"]
    assert prov["geocoder"] == "Photon (OpenStreetMap)"
    assert prov["attribution"] == geocode.ATTRIBUTION
    assert "OpenStreetMap contributors, ODbL" in geocode.ATTRIBUTION


@pytest.mark.parametrize("photon", [HttpResponse(503, "down"), OSError("reset"),
                                    fixture("photon_empty.json")])
def test_nominatim_is_the_fallback_on_photon_failure_or_empty(photon):
    fake = FakeUpstream({PHOTON: photon, NOMINATIM: fixture("nominatim_kherson.json")})
    out = geocode.lookup("Kherson", fetch=fake)
    assert fake.hosts() == ["photon.komoot.io", "nominatim.openstreetmap.org"]
    query = parse_qs(urlparse(fake.calls[1][0]).query)
    assert query["format"] == ["jsonv2"] and query["q"] == ["Kherson"]
    assert [c["id"] for c in out["candidates"]] == ["nominatim:relation/1573441"]
    assert out["candidates"][0]["bbox"] == list(KHERSON_BBOX)
    prov = out["provenance"]
    assert prov["source"] == "nominatim" and prov["geocoder"] == "Nominatim (OpenStreetMap)"
    assert prov["fallback_reason"].startswith("photon:")


def test_both_geocoders_down_is_an_empty_answer_with_a_reason_never_a_raise():
    fake = FakeUpstream({PHOTON: HttpResponse(502, ""), NOMINATIM: ZeroDivisionError("x")})
    out = geocode.lookup("Kherson", fetch=fake)
    assert out["candidates"] == []
    assert not out["provenance"]["real"]
    assert "geocoders unavailable" in out["provenance"]["reason"]
    both_empty = FakeUpstream({PHOTON: fixture("photon_empty.json"), NOMINATIM: "[]"})
    assert geocode.lookup("Nowhere", fetch=both_empty)["provenance"]["reason"].startswith(
        "no place found")


@pytest.mark.parametrize("text,expected", [
    ("12.97160, 77.59460", (12.9716, 77.5946)),
    ("12.9716 77.5946", (12.9716, 77.5946)),
    ("(47.6415, -122.1402)", (47.6415, -122.1402)),
    ("33.72N 51.72E", (33.72, 51.72)),
    ("33.72S; 51.72W", (-33.72, -51.72)),
    ("91, 10", None), ("10, 181", None), ("Kherson", None), ("12", None),
])
def test_parse_coords(text, expected):
    assert geocode.parse_coords(text) == expected


def test_coordinates_need_no_network_and_work_with_geodata_off():
    fake = FakeUpstream({})
    for enabled in (True, False):
        out = geocode.lookup("12.97160, 77.59460", fetch=fake, enabled=enabled)
        assert out["candidates"] == [geocode.Place(
            id="coords:12.97160,77.59460", name="12.97160, 77.59460",
            label="12.97160, 77.59460", lat=12.9716, lon=77.5946,
            source="coordinates").as_dict()]
        assert out["provenance"]["geocoder"] == "Coordinates"
    assert fake.calls == []


def test_geodata_off_refuses_place_names_without_a_fetch():
    fake = FakeUpstream({})
    out = geocode.lookup("Kherson", fetch=fake, enabled=False)
    assert out == {"candidates": [], "provenance": out["provenance"]}
    assert out["provenance"]["reason"] == "map data is off; give coordinates"
    assert not out["provenance"]["real"] and fake.calls == []


def test_theater_table_rows_answer_before_the_network():
    fake = FakeUpstream({})
    out = geocode.lookup("Isfahan", fetch=fake)
    assert [c["id"] for c in out["candidates"]] == ["preset:iran-isfahan"]
    row = out["candidates"][0]
    assert row["theater_id"] == "iran-isfahan" and row["source"] == "preset"
    assert out["provenance"]["geocoder"] == "Theater table"
    assert geocode.lookup("redmond", fetch=fake)["candidates"][0]["theater_id"] == "default"
    assert fake.calls == []
    # Exact names only: "Washington" is in default's place string but is not a preset.
    assert geocode.preset_places("Washington") == []
    assert geocode.preset_places("Iran") == []


def test_results_are_cached_by_query_and_each_place_by_id(tmp_path):
    cache = geo_http.default_cache(tmp_path)
    fake = FakeUpstream({PHOTON: fixture("photon_kherson.json")})
    first = geocode.lookup("Kherson", cache=cache, fetch=fake)
    second = geocode.lookup("  kherson ", cache=cache, fetch=fake)
    assert len(fake.calls) == 1
    assert second["provenance"]["cached"] and second["candidates"] == first["candidates"]
    place = geocode.cached_place("photon:relation/1573441", cache=cache)
    assert place is not None and place.bbox == KHERSON_BBOX and place.source == "photon"
    assert geocode.cached_place("photon:relation/1", cache=cache) is None
    assert geocode.cached_place("photon:relation/1573441", cache=None) is None
    assert geocode.cached_place("coords:12.97160,77.59460", cache=None).lat == 12.9716
    assert geocode.cached_place("preset:default", cache=None).theater_id == "default"


def test_the_budget_is_spent_only_on_uncached_network_lookups():
    cache = geo_http.default_cache(None)
    budget = CallBudget(1, 600.0)
    fake = FakeUpstream({PHOTON: fixture("photon_kherson.json")})
    assert geocode.lookup("Kherson", cache=cache, fetch=fake, budget=budget)["candidates"]
    again = geocode.lookup("Kherson", cache=cache, fetch=fake, budget=budget)
    assert again["provenance"]["cached"]                         # cache hit: no budget
    geocode.lookup("12.9716, 77.5946", fetch=fake, budget=budget)  # coordinates: no budget
    limited = geocode.lookup("Mykolaiv", cache=cache, fetch=fake, budget=budget)
    assert limited["candidates"] == [] and limited["provenance"]["rate_limited"]
    assert len(fake.calls) == 1


def test_query_and_limit_are_clamped():
    fake = FakeUpstream({PHOTON: fixture("photon_kherson.json")})
    out = geocode.lookup("Kherson", limit=99, fetch=fake)
    assert parse_qs(urlparse(fake.calls[0][0]).query)["limit"] == ["10"]
    assert out["provenance"]["query"] == "Kherson"
    assert geocode.lookup("x", fetch=fake)["provenance"]["reason"].startswith("the query must")
