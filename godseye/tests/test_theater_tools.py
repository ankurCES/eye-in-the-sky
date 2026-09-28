"""The runtime-theater and sim-speed tools (WG v2 §3.7, §3.10, §4.1.6; A7).

What these tests guard (the A7 acceptance list, §4.3):

* each tool's happy path with an injected geodata client (`srv.geo_fetch`);
* the call budgets give `rate_limited`;
* `sim_set_theater`: `proposal_mismatch`, `proposal_expired`, `unchanged`,
  idempotent replay, and `set_via` from `CALL_VIA`;
* descriptions say SIMULATION, and the theater tools name the MSL datum;
* with geodata off the fetch spy is never called;
* `theater_state()` keys; `approval_preview` for both sim tools, and `{}` on
  a raising proposal book;
* the §4.1.6 sim-speed caveats, verbatim and in order.

No test touches the network: every geodata request goes to `GeoWire`, and the
fake sims bind A7's range (53200-53299) only.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import pathlib
import threading
from contextlib import contextmanager, suppress
from types import SimpleNamespace
from typing import ClassVar

import airsim
import pytest
from godseye_uav import geo_http, sites, theater_tools, theaters
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint, canonical_altitude
from godseye_uav.realdata import HttpResponse
from godseye_uav.safety import SafetyEnvelope
from godseye_uav.server import (
    DEFAULT_TICK_S,
    MONITOR_MIN_SLEEP_S,
    GodseyeUavServer,
    UavBackend,
)
from godseye_uav.store import Store

_PORTS = list(range(53200, 53300))
_PORT = itertools.cycle(_PORTS[os.getpid() % len(_PORTS):]
                        + _PORTS[:os.getpid() % len(_PORTS)])

THEATER = theaters.get("default")
HOME = GeoPoint(THEATER.home_lat, THEATER.home_lon,
                canonical_altitude(THEATER.home_alt_msl_m, THEATER.home_lat,
                                   THEATER.home_lon, datum="msl").alt_hae)
FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "geodata"
#: Bengaluru centre (E2E A1): a chat theater far from the Redmond default.
BLR = (12.9716, 77.5946)
XSS = "<img src=x onerror=alert(1)>"
BIDI = "\u202eevil\u202c"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class GeoWire:
    """The injected geodata client: answers by upstream host, records every
    call. A route's answer is a JSON body, an `HttpResponse`, an exception, or
    a callable `(url, data) -> answer`."""

    HOSTS: ClassVar[dict[str, str]] = {
        "photon": "photon.komoot.io", "nominatim": "nominatim.openstreetmap.org",
        "overpass": "overpass", "reearth": "terrain.reearth.land",
        "open-meteo": "open-meteo.com"}

    def __init__(self, **routes):
        self.routes = routes
        self.calls: list[tuple[str, bytes | None]] = []
        self.threads: set[str] = set()
        self._lock = threading.Lock()

    def __call__(self, url, timeout_s, data=None):
        with self._lock:
            self.calls.append((url, data))
            self.threads.add(threading.current_thread().name)
        for upstream, answer in self.routes.items():
            if self.HOSTS[upstream.replace("_", "-")] in url:
                if callable(answer) and not isinstance(answer, HttpResponse):
                    answer = answer(url, data)
                if isinstance(answer, BaseException):
                    raise answer
                if isinstance(answer, HttpResponse):
                    return answer
                return HttpResponse(200, json.dumps(answer))
        return HttpResponse(503, "no route in this test")

    def hits(self, upstream: str) -> int:
        host = self.HOSTS[upstream]
        return sum(1 for url, _ in self.calls if host in url)


def overpass(url, data):
    """Overpass double: the open-ground query gets parks, anything else sites."""
    return load("overpass_open_ground.json" if data and b"leisure" in data
                else "overpass_sites.json")


def online_wire() -> GeoWire:
    """Every upstream a proposal with map data on may ask, answered offline."""
    return GeoWire(photon=load("photon_kherson.json"), overpass=overpass,
                   reearth=load("reearth_heights.json"),
                   open_meteo=load("openmeteo_elevation.json"))


def _start_sim() -> FakeAirSim:
    last = None
    for _ in range(len(_PORTS)):
        sim = FakeAirSim(home=HOME, port=next(_PORT))
        try:
            sim.start()
            return sim
        except OSError as exc:  # taken by a concurrent run: try the next one
            last = exc
            with suppress(Exception):
                sim.stop()
    raise RuntimeError(f"no free port in 53200-53299: {last}")


@contextmanager
def tools_server(tmp_path, *, with_sim=True, geodata=False, listener=True,
                 wire: GeoWire | None = None, recovered=True, **kw):
    """A server on a fake sim, as the app host builds it: a theater listener
    attached and restart recovery finished (A6b sets that flag in the app)."""
    sim = _start_sim()
    srv = None
    store = Store(tmp_path / "store")
    try:
        client = airsim.MultirotorClient(port=sim.port)
        client.confirmConnection()
        backend = UavBackend(client, HOME, sim=sim if with_sim else None)
        srv = GodseyeUavServer(backend, store, theater=THEATER, watchdog_s=30.0,
                               envelope=SafetyEnvelope(**THEATER.envelope_kwargs()),
                               geodata=geodata, **kw)
        srv.geo_fetch = wire if wire is not None else GeoWire()
        srv.sim = sim
        srv.heard = []
        if listener:
            srv.theater_listeners.append(srv.heard.append)
        if recovered:
            srv.boot_recovery_done.set()
        yield srv
    finally:
        if srv is not None:
            srv.stop_monitor()
            with suppress(Exception):
                srv.tasking.shutdown()
        store.close()
        sim.stop()


def run(coro):
    return asyncio.run(coro)


def call(srv, name, **args):
    """Call a registered tool's function in-process (no transport)."""
    return run(srv.mcp._tool_manager._tools[name].fn(**args))


def audits(srv, kind: str) -> list[dict]:
    return [r for r in srv.store.audit.read_all() if r.get("kind") == kind]


def blr_proposal(srv, **extra) -> dict:
    """A chat proposal over Bengaluru centre from coordinates (no network)."""
    args = {"lat": BLR[0], "lon": BLR[1], "label": "Bengaluru centre",
            "ground_msl_m": 920.0, "airframe": "quad_suas_electric", **extra}
    out = call(srv, "theater_propose", **args)
    assert not out.get("rejected") and "error" not in out, out
    return out


# ------------------------------------------------------------ catalog --

def test_register_adds_exactly_the_five_tools_with_titles_and_a_proposal_book(tmp_path):
    with tools_server(tmp_path) as srv:
        tools = srv.mcp._tool_manager._tools
        assert set(theater_tools.TOOL_NAMES) <= set(tools)
        assert len(tools) == 51 and not any(n.startswith("wg_") for n in tools)
        for name in theater_tools.TOOL_NAMES:
            assert tools[name].title == theater_tools.TITLES[name]
        assert type(srv.theater_proposals).__name__ == "ProposalBook"
        assert srv.theater_proposals.max == 16 and srv.theater_proposals.ttl_s == 1800.0
        assert {k: (b.calls, b.window_s) for k, b in srv.theater_budgets.items()} == {
            "geo_lookup": (30, 600.0), "geo_sites": (6, 600.0),
            "theater_propose": (10, 600.0)}


def test_descriptions_say_simulation_and_the_theater_tools_name_msl(tmp_path):
    with tools_server(tmp_path) as srv:
        tools = srv.mcp._tool_manager._tools
        for name in theater_tools.TOOL_NAMES:
            assert "SIMULATION" in tools[name].description, name
        for name in ("geo_lookup", "theater_propose", "sim_set_theater"):
            assert "MSL" in tools[name].description, name


def test_only_the_sim_tools_take_an_idempotency_key(tmp_path):
    with tools_server(tmp_path) as srv:
        props = {n: set(srv.mcp._tool_manager._tools[n].parameters.get("properties", {}))
                 for n in theater_tools.TOOL_NAMES}
    assert "idempotency_key" in props["sim_set_theater"]
    assert "idempotency_key" in props["sim_set_time_scale"]
    assert not any("idempotency_key" in props[n]
                   for n in ("geo_lookup", "geo_sites", "theater_propose"))
    assert props["sim_set_theater"] == set(theater_tools.SET_ARG_KEYS) | {"idempotency_key"}
    assert props["theater_propose"] == {
        "theater_id", "lat", "lon", "place_id", "label", "place", "bbox",
        "half_extent_m", "airframe", "home_lat", "home_lon", "ground_msl_m", "query"}


def test_the_mirrored_constants_match_the_server_and_the_plan():
    from godseye_uav import theater_plan

    assert theater_tools.DEFAULT_TICK_S == DEFAULT_TICK_S
    assert theater_tools.MONITOR_MIN_SLEEP_S == MONITOR_MIN_SLEEP_S
    assert theater_tools.SET_ARG_KEYS == theater_plan.SET_ARG_KEYS
    assert (theater_tools.PROPOSAL_MAX, theater_tools.PROPOSAL_TTL_S) == (
        theater_plan.PROPOSAL_MAX, theater_plan.PROPOSAL_TTL_S)


# --------------------------------------------------------- geo_lookup --

def test_geo_lookup_answers_from_photon_through_the_injected_client(tmp_path):
    wire = GeoWire(photon=load("photon_kherson.json"))
    with tools_server(tmp_path, geodata=True, wire=wire) as srv:
        out = call(srv, "geo_lookup", query="Kherson", limit=3)
        again = call(srv, "geo_lookup", query="Kherson", limit=3)
    first = out["candidates"][0]
    assert first["name"] == "Kherson" and first["geocoder"] == "Photon (OpenStreetMap)"
    s, w, n, e = first["bbox"]
    assert s < n and w < e                         # Photon [W,N,E,S] became [s,w,n,e]
    assert out["provenance"]["real"] is True and out["provenance"]["source"] == "photon"
    assert "ODbL" in out["provenance"]["attribution"]
    assert again["provenance"]["cached"] is True
    assert wire.hits("photon") == 1                # the second answer came from the cache
    assert threading.main_thread().name not in wire.threads   # off the event loop


def test_geo_lookup_names_come_back_as_clean_text(tmp_path):
    feature = load("photon_kherson.json")["features"][0]
    feature = {**feature, "properties": {**feature["properties"], "name": XSS + BIDI}}
    wire = GeoWire(photon={"type": "FeatureCollection", "features": [feature]})
    with tools_server(tmp_path, geodata=True, wire=wire) as srv:
        out = call(srv, "geo_lookup", query="Kherson")
    first = out["candidates"][0]
    assert first["name"] == XSS + "evil"           # the text survives, the bidi does not
    assert not any(ch in first["label"] for ch in "\u202a\u202b\u202c\u202d\u202e")


def test_geo_lookup_takes_coordinates_without_any_request(tmp_path):
    wire = GeoWire()
    with tools_server(tmp_path, geodata=True, wire=wire) as srv:
        out = call(srv, "geo_lookup", query="12.97160, 77.59460")
    assert [c["source"] for c in out["candidates"]] == ["coordinates"]
    assert out["candidates"][0]["lat"] == pytest.approx(12.9716)
    assert wire.calls == []


def test_with_geodata_off_a_place_name_is_refused_and_nothing_is_fetched(tmp_path):
    wire = GeoWire(photon=load("photon_kherson.json"))
    with tools_server(tmp_path, geodata=False, wire=wire) as srv:
        name = call(srv, "geo_lookup", query="Kherson")
        coords = call(srv, "geo_lookup", query="46.6354 32.6169")
    assert name["candidates"] == []
    assert name["provenance"]["real"] is False
    assert name["provenance"]["reason"] == "map data is off; give coordinates"
    assert coords["candidates"][0]["source"] == "coordinates"
    assert wire.calls == []


def test_geo_lookup_budget_gives_rate_limited_for_uncached_lookups_only(tmp_path):
    wire = GeoWire(photon=load("photon_kherson.json"))
    with tools_server(tmp_path, geodata=True, wire=wire) as srv:
        srv.theater_budgets["geo_lookup"] = geo_http.CallBudget(1, 600.0)
        assert call(srv, "geo_lookup", query="Kherson")["candidates"]
        cached = call(srv, "geo_lookup", query="Kherson")
        limited = call(srv, "geo_lookup", query="Mykolaiv")
        coords = call(srv, "geo_lookup", query="46.6, 32.6")
    assert cached["candidates"] and not cached.get("rejected")
    assert limited["rejected"] is True and limited["error"] == "rate_limited"
    assert limited["candidates"] == [] and "try again" in limited["message"]
    assert coords["candidates"]                    # coordinates never spend the budget
    assert wire.hits("photon") == 1


@pytest.mark.parametrize("args", [{"query": "K"}, {"query": "x" * 201},
                                  {"query": "Kherson", "limit": 0},
                                  {"query": "Kherson", "limit": 11}])
def test_geo_lookup_refuses_out_of_range_arguments(tmp_path, args):
    with tools_server(tmp_path, geodata=True) as srv:
        out = call(srv, "geo_lookup", **args)
        assert out["rejected"] is True and out["error"] == "invalid_parameter"
        assert srv.geo_fetch.calls == []


# ---------------------------------------------------------- geo_sites --

def test_geo_sites_serves_the_cache_only_and_says_how_to_refresh(tmp_path):
    wire = GeoWire(overpass=load("overpass_sites.json"))
    with tools_server(tmp_path, geodata=True, wire=wire) as srv:
        out = call(srv, "geo_sites")
    assert out["sites"] == [] and out["degraded"] is True
    assert "refresh=true" in out["hint"]
    assert out["theater"] == {"id": "default", "epoch": 0}
    assert out["attribution"] == sites.ATTRIBUTION
    assert wire.calls == []


def test_geo_sites_refresh_loads_the_area_and_rows_carry_their_chip_ids(tmp_path):
    wire = GeoWire(overpass=load("overpass_sites.json"))
    with tools_server(tmp_path, geodata=True, wire=wire) as srv:
        out = call(srv, "geo_sites", refresh=True)
        airfields = call(srv, "geo_sites", category="airfield", limit=2)
        near = call(srv, "geo_sites", near_lat=46.67, near_lon=32.50, limit=3)
        loaded = srv.sites
    assert out["refresh"]["done"] is True and out["refresh"]["replaced"] is True
    assert out["real"] is True and out["total"] == loaded.total > 0
    assert all(r["id"].startswith("sit:default:") for r in out["sites"])
    assert threading.main_thread().name not in wire.threads
    assert out["note"].startswith("Context only")
    assert {r["category"] for r in airfields["sites"]} == {"airfield"}
    assert len(airfields["sites"]) <= 2
    dist = [r["distance_m"] for r in near["sites"]]
    assert dist == sorted(dist)
    assert wire.hits("overpass") == 1


def test_a_failed_refresh_never_replaces_good_sites(tmp_path):
    wire = GeoWire(overpass=load("overpass_sites.json"))
    with tools_server(tmp_path, geodata=True, wire=wire) as srv:
        call(srv, "geo_sites", refresh=True)
        good = srv.sites
        wire.routes["overpass"] = HttpResponse(504, "gateway timeout")
        out = call(srv, "geo_sites", refresh=True)
        assert srv.sites is good
    assert out["refresh"] == {"done": True, "replaced": False, "real": False, "total": 0,
                              "reason": out["refresh"]["reason"]}
    assert "map data feed down" in out["refresh"]["reason"]
    assert out["real"] is True and out["total"] == good.total


def test_a_refresh_that_straddles_a_theater_switch_is_dropped(tmp_path):
    holder = {}

    def switch_mid_fetch(url, data):
        holder["srv"].theater_epoch += 1         # a switch lands while Overpass answers
        return load("overpass_sites.json")

    with tools_server(tmp_path, geodata=True, wire=GeoWire(overpass=switch_mid_fetch)) as srv:
        holder["srv"] = srv
        before = srv.sites
        out = call(srv, "geo_sites", refresh=True)
        assert srv.sites is before
    assert out["refresh"]["done"] is False and "dropped" in out["refresh"]["reason"]


def test_geo_sites_refresh_budget_gives_rate_limited(tmp_path):
    wire = GeoWire(overpass=load("overpass_sites.json"))
    with tools_server(tmp_path, geodata=True, wire=wire) as srv:
        srv.theater_budgets["geo_sites"] = geo_http.CallBudget(1, 600.0)
        assert call(srv, "geo_sites", refresh=True)["refresh"]["done"] is True
        limited = call(srv, "geo_sites", refresh=True)
        plain = call(srv, "geo_sites")
    assert limited["rejected"] is True and limited["error"] == "rate_limited"
    assert plain["total"] > 0                      # reading the cache is never limited
    assert wire.hits("overpass") == 1


def test_with_geodata_off_a_sites_refresh_fetches_nothing(tmp_path):
    wire = GeoWire(overpass=load("overpass_sites.json"))
    with tools_server(tmp_path, geodata=False, wire=wire) as srv:
        out = call(srv, "geo_sites", refresh=True)
    assert out["refresh"] == {"done": False, "reason": sites.GEODATA_OFF_REASON}
    assert out["reason"] == sites.GEODATA_OFF_REASON and out["sites"] == []
    assert "hint" not in out
    assert wire.calls == []


@pytest.mark.parametrize("args", [{"category": "missile_site"}, {"limit": 61},
                                  {"limit": 0}, {"near_lat": 46.6},
                                  {"near_lat": 95.0, "near_lon": 32.0}])
def test_geo_sites_refuses_out_of_range_arguments(tmp_path, args):
    with tools_server(tmp_path, geodata=True) as srv:
        out = call(srv, "geo_sites", **args)
    assert out["rejected"] is True and out["error"] == "invalid_parameter"


# ---------------------------------------------------- sim speed (§4.1.6) --

CAVEATS_X1 = [
    "Link-loss timers stay in wall-clock seconds.",
    "Detections and scans run in real time.",
    "The analyst's clock is wall time.",
    "Safety checks run every 0.5 sim-seconds.",
]
GAP_LINE = ("Camera captures are rate-limited in real time; coverage gaps are likely "
            "above ×3.")


def test_the_time_scale_caveats_are_verbatim_and_in_order():
    assert theater_tools.capture_gap_scale() == 3
    assert theater_tools.time_scale_caveats(None, 1) == CAVEATS_X1
    assert theater_tools.time_scale_caveats(None, 3) == CAVEATS_X1
    assert theater_tools.time_scale_caveats(None, 4) == CAVEATS_X1 + [GAP_LINE]
    assert theater_tools.time_scale_caveats(None, 10) == CAVEATS_X1 + [GAP_LINE]
    assert theater_tools.safety_period_sim_s(10) == 0.5


def test_sim_set_time_scale_speeds_the_fake_and_every_fuel_clock(tmp_path):
    with tools_server(tmp_path) as srv:
        srv.monitor_for("Drone1")
        out = call(srv, "sim_set_time_scale", scale=10)
        assert srv.time_scale == 10.0 and srv.sim.time_scale == 10.0
        assert srv.fuel_for("Drone1").time_scale == 10.0
        assert audits(srv, "time_scale_changed")
    assert out["ok"] is True and out["status"] == "accepted"
    assert (out["scale"], out["previous"]) == (10.0, 1.0)
    assert out["caveats"] == CAVEATS_X1 + [GAP_LINE]


def test_sim_set_time_scale_is_idempotent(tmp_path):
    with tools_server(tmp_path) as srv:
        first = call(srv, "sim_set_time_scale", scale=4, idempotency_key="k-speed")
        call(srv, "sim_set_time_scale", scale=2)
        replay = call(srv, "sim_set_time_scale", scale=4, idempotency_key="k-speed")
        assert srv.time_scale == 2.0                # the replay did not run again
        assert len(audits(srv, "time_scale_changed")) == 2
    assert replay["idempotent_replay"] is True and replay["status"] == "duplicate"
    assert (replay["scale"], replay["previous"]) == (first["scale"], first["previous"])


@pytest.mark.parametrize("scale", [0.5, 10.5, 0, True, "fast", float("nan")])
def test_sim_set_time_scale_refuses_out_of_range(tmp_path, scale):
    with tools_server(tmp_path) as srv:
        out = call(srv, "sim_set_time_scale", scale=scale)
        assert srv.time_scale == 1.0 and srv.sim.time_scale == 1.0
    assert out["rejected"] is True and out["error"] == "invalid_parameter"


def test_sim_set_time_scale_is_refused_under_real_airsim(tmp_path):
    with tools_server(tmp_path, with_sim=False) as srv:
        out = call(srv, "sim_set_time_scale", scale=4)
        assert srv.time_scale == 1.0
    assert out["rejected"] is True and out["error"] == "time_scale_refused"
    assert out["message"].startswith("real AirSim")


def test_the_time_scale_preview_carries_from_to_checks_and_caveats(tmp_path):
    with tools_server(tmp_path) as srv:
        ok = theater_tools.approval_preview(srv, "sim_set_time_scale", {"scale": 10})
        bad = theater_tools.approval_preview(srv, "sim_set_time_scale", {"scale": 12})
    with tools_server(tmp_path / "real", with_sim=False) as real:
        airsim_ = theater_tools.approval_preview(real, "sim_set_time_scale", {"scale": 4})
    assert ok == {"from": 1.0, "to": 10.0,
                  "checks": [{"text": "Fake simulator", "ok": True}],
                  "caveats": CAVEATS_X1 + [GAP_LINE]}
    assert bad["to"] == 12.0 and bad["caveats"] == []
    assert {"text": "Speed between ×1 and ×10", "ok": False} in bad["checks"]
    assert airsim_["checks"] == [{"text": "Fake simulator", "ok": False}]


# ------------------------------------------------------ theater_state --

def test_theater_state_for_a_preset_has_every_block_key(tmp_path):
    with tools_server(tmp_path) as srv:
        state = theater_tools.theater_state(srv)
        srv._switching.set()
        switching = theater_tools.theater_state(srv)["state"]
        srv._switching.clear()
    assert list(state) == list(theater_tools.STATE_KEYS)
    assert state["id"] == "default" and state["known"] is True
    assert (state["epoch"], state["dynamic"], state["source"], state["state"]) == (
        0, False, "preset", "active")
    assert switching == "switching"
    s, w, n, e = state["bbox"]
    assert s < state["center"][0] < n and w < state["center"][1] < e
    assert state["half_extent_m"] > 0 and state["area_km2"] > 0
    assert state["home"]["source"] == "preset" and state["home"]["name"] is None
    assert state["home"]["alt_msl_m"] == pytest.approx(THEATER.home_alt_msl_m)
    assert state["ground_msl_m"] == pytest.approx(THEATER.home_alt_msl_m)
    assert state["ground_source"] == "Theater table (checked against terrain to 50 m)."
    assert state["airframe"] == {"id": "quad_suas_electric",
                                 "label": "Quad, small electric", "reach_m": 7350}
    assert state["time_scale"] == 1.0 and state["geocoder"] == "Theater table"
    assert state["query"] is None and state["previous"] is None
    assert state["set_via"] is None and state["integrity_error"] is None


def _chat_theater(**prov) -> theaters.Theater:
    provenance = {
        "proposal_id": "TP-0000abcd",
        "center": {"lat": BLR[0], "lon": BLR[1], "source": "photon",
                   "place_id": "photon:relation/1", "query": "Bengaluru " + BIDI},
        "ao": {"half_extent_m": 2500.0, "requested_m": None, "clamped": False},
        "home": {"source": "overpass-open-ground", "name": XSS + BIDI, "distance_m": 345},
        "ground": {"msl_m": 920.0, "source": "reearth"}, **prov}
    return theaters.make_dynamic(
        label="Bengaluru centre", place="Bengaluru, India", center=BLR,
        half_extent_m=2500.0, home=BLR, home_alt_msl_m=920.0, provenance=provenance)


def test_theater_state_for_a_chat_theater_reads_its_provenance_as_clean_text():
    t = _chat_theater()
    srv = SimpleNamespace(theater=t, theater_epoch=2, airframe_id="group3_fixed_wing",
                          time_scale=4.0, theater_set_at_ms=1234, theater_set_via="console",
                          theater_previous={"id": "default", "label": "Redmond"},
                          theater_integrity_error=None, _switching=threading.Event())
    state = theater_tools.theater_state(srv)
    assert (state["source"], state["dynamic"], state["epoch"]) == ("chat", True, 2)
    assert state["half_extent_m"] == 2500.0
    assert state["area_km2"] == pytest.approx(25.0, abs=0.01)
    assert state["home"]["name"] == XSS + "evil"   # bidi stripped, text kept as text
    assert state["home"]["source"] == "overpass-open-ground"
    assert state["query"] == "Bengaluru evil"
    assert state["geocoder"] == "Photon (OpenStreetMap)"
    assert state["ground_source"].startswith("Re:Earth terrain")
    assert state["airframe"]["reach_m"] == 352800
    assert (state["time_scale"], state["set_via"], state["set_at_ms"]) == (4.0, "console", 1234)
    assert state["previous"] == {"id": "default", "label": "Redmond"}


def test_theater_state_never_raises_on_a_bare_server():
    state = theater_tools.theater_state(SimpleNamespace())
    assert list(state) == list(theater_tools.STATE_KEYS)
    assert state["known"] is False and state["state"] == "active" and state["epoch"] == 0
    broken = SimpleNamespace(theater=SimpleNamespace(id="x", label="X", ao=None),
                             airframe_id="no_such_airframe")
    out = theater_tools.theater_state(broken)
    assert out["id"] == "x" and out["bbox"] is None and out["airframe"] is None


# ---------------------------------------------------- theater_propose --

def test_theater_propose_from_coordinates_with_geodata_off_fetches_nothing(tmp_path):
    wire = online_wire()
    with tools_server(tmp_path, geodata=False, wire=wire) as srv:
        out = blr_proposal(srv)
        stored = srv.theater_proposals.get(out["proposal_id"])
        assert srv.theater.id == "default"         # a proposal changes nothing
    assert wire.calls == []
    assert out["proposal_id"].startswith("TP-") and stored is not None
    assert out["simulated_world"] is True
    assert set(out["set_args"]) == set(theater_tools.SET_ARG_KEYS)
    assert out["set_args"]["theater_id"].startswith("dyn-bengaluru-centre-")
    assert out["set_args"]["ground_msl_m"] == 920.0
    assert out["theater"]["area_km2"] == pytest.approx(25.0, abs=0.05)
    assert out["sites"]["reason"] == sites.GEODATA_OFF_REASON


def test_theater_propose_with_geodata_on_uses_only_the_injected_client(tmp_path):
    wire = online_wire()
    with tools_server(tmp_path, geodata=True, wire=wire) as srv:
        place = call(srv, "geo_lookup", query="Kherson")["candidates"][0]
        out = call(srv, "theater_propose", lat=place["lat"], lon=place["lon"],
                   bbox=place["bbox"], place_id=place["id"], label="Kherson",
                   query="Kherson")
        preview = theater_tools.approval_preview(srv, "sim_set_theater", out["set_args"])
    assert out["ground"]["provenance"]["source"] == "reearth"
    assert out["sites"]["total"] > 0 and out["sites"]["degraded"] is False
    assert preview["geocoder"] == "Photon (OpenStreetMap)" and preview["query"] == "Kherson"
    assert preview["clamped_from_km"] is not None   # a city box is wider than quad reach
    assert wire.hits("photon") == 1 and wire.hits("overpass") >= 1
    assert wire.hits("reearth") == 1


def test_theater_propose_budget_gives_rate_limited_only_with_geodata_on(tmp_path):
    with tools_server(tmp_path, geodata=True, wire=online_wire()) as srv:
        srv.theater_budgets["theater_propose"] = geo_http.CallBudget(1, 600.0)
        blr_proposal(srv)
        limited = call(srv, "theater_propose", lat=BLR[0], lon=BLR[1], ground_msl_m=920.0)
    assert limited["rejected"] is True and limited["error"] == "rate_limited"
    with tools_server(tmp_path / "off", geodata=False) as srv:
        srv.theater_budgets["theater_propose"] = geo_http.CallBudget(1, 600.0)
        blr_proposal(srv)
        blr_proposal(srv)                           # nothing leaves the host: no budget


def test_theater_propose_passes_the_plan_refusals_through(tmp_path):
    with tools_server(tmp_path, geodata=False) as srv:
        out = call(srv, "theater_propose", lat=BLR[0], lon=BLR[1])
        assert len(srv.theater_proposals) == 0
    assert out["rejected"] is True and out["error"] == "ground_unknown"
    assert out["message"] == "No ground elevation could be measured here; give ground_msl_m."


@pytest.mark.parametrize("args", [{"label": "x" * 61}, {"bbox": [1, 2, 3]},
                                  {"bbox": [1, 2, 3, "north"]}, {"query": 7}])
def test_theater_propose_refuses_malformed_arguments(tmp_path, args):
    with tools_server(tmp_path) as srv:
        out = call(srv, "theater_propose", lat=BLR[0], lon=BLR[1], ground_msl_m=920.0,
                   **args)
    assert out["rejected"] is True and out["error"] == "invalid_parameter"


def test_a_slow_proposal_is_refused_after_the_timeout(tmp_path, monkeypatch):
    import time as _time

    def slow(srv, args, *, fetch=None):
        _time.sleep(0.5)
        return {"never": True}

    with tools_server(tmp_path) as srv:
        monkeypatch.setattr(theater_tools, "PROPOSE_TIMEOUT_S", 0.05)
        monkeypatch.setattr(theater_tools, "_plan", lambda: SimpleNamespace(propose=slow))
        out = call(srv, "theater_propose", lat=BLR[0], lon=BLR[1], ground_msl_m=920.0)
    assert out["rejected"] is True and out["error"] == "proposal_timeout"


# ---------------------------------------------------- sim_set_theater --

def test_sim_set_theater_switches_on_the_tasking_loop_and_tells_the_listeners(tmp_path):
    with tools_server(tmp_path) as srv:
        p = blr_proposal(srv)
        out = call(srv, "sim_set_theater", **p["set_args"])
        state = theater_tools.theater_state(srv)
        assert srv.theater.id == p["set_args"]["theater_id"]
        assert len(srv.heard) == 1 and srv.heard[0]["theater_id"] == srv.theater.id
        assert audits(srv, "theater_changed")
    assert out["ok"] is True and out["status"] == "accepted"
    assert out["theater"]["epoch"] == 1 and out["theater"]["dynamic"] is True
    assert out["previous"]["id"] == "default"
    assert out["home"]["alt_msl_m"] == 920.0
    assert state["epoch"] == 1 and state["set_via"] == "mcp" and state["source"] == "chat"


def test_sim_set_theater_from_the_console_is_set_via_console(tmp_path):
    async def as_console(srv, set_args):
        token = theater_tools.CALL_VIA.set("console")
        try:
            return await srv.mcp._tool_manager._tools["sim_set_theater"].fn(**set_args)
        finally:
            theater_tools.CALL_VIA.reset(token)

    with tools_server(tmp_path) as srv:
        p = blr_proposal(srv)
        out = run(as_console(srv, p["set_args"]))
        assert out["status"] == "accepted"
        assert srv.theater_set_via == "console"
        assert theater_tools.theater_state(srv)["set_via"] == "console"
    assert theater_tools.CALL_VIA.get() == "mcp"


def test_call_via_survives_the_mcp_call_path_the_toolbelt_proxy_uses(tmp_path):
    """A9's proxy sets CALL_VIA around `server.mcp.call_tool`; the value must
    reach the handler through the SDK's argument validation."""
    async def proxy(srv, set_args):
        token = theater_tools.CALL_VIA.set("console")
        try:
            return await srv.mcp.call_tool("sim_set_theater", set_args)
        finally:
            theater_tools.CALL_VIA.reset(token)

    with tools_server(tmp_path) as srv:
        p = blr_proposal(srv)
        run(proxy(srv, dict(p["set_args"])))
        assert srv.theater.id == p["set_args"]["theater_id"]
        assert srv.theater_set_via == "console"


def test_sim_set_theater_is_idempotent_and_a_repeat_is_unchanged(tmp_path):
    with tools_server(tmp_path) as srv:
        p = blr_proposal(srv)
        first = call(srv, "sim_set_theater", idempotency_key="k-thr", **p["set_args"])
        replay = call(srv, "sim_set_theater", idempotency_key="k-thr", **p["set_args"])
        again = call(srv, "sim_set_theater", **p["set_args"])
        assert srv.theater_epoch == 1 and len(srv.heard) == 1
        assert len(audits(srv, "theater_changed")) == 1
    assert replay["idempotent_replay"] is True and replay["status"] == "duplicate"
    assert replay["theater"] == first["theater"]
    assert again["ok"] is True and again["status"] == "unchanged"
    assert again["theater"]["epoch"] == 1 and again["airframe"]["changed"] is False


def test_the_running_preset_with_the_same_airframe_is_unchanged(tmp_path):
    with tools_server(tmp_path) as srv:
        p = call(srv, "theater_propose", theater_id="default")
        out = call(srv, "sim_set_theater", **p["set_args"])
        assert srv.theater_epoch == 0 and srv.heard == []
    assert out["status"] == "unchanged" and out["theater"]["id"] == "default"


def test_an_unknown_or_expired_proposal_is_refused(tmp_path):
    from godseye_uav import theater_plan

    clock = [0.0]
    with tools_server(tmp_path) as srv:
        srv.theater_proposals = theater_plan.ProposalBook(now=lambda: clock[0])
        p = blr_proposal(srv)
        unknown = call(srv, "sim_set_theater", **{**p["set_args"], "proposal_id": "TP-00000000"})
        clock[0] += 1801.0
        expired = call(srv, "sim_set_theater", **p["set_args"])
        assert srv.theater.id == "default" and srv.theater_epoch == 0
    for out in (unknown, expired):
        assert out["rejected"] is True and out["error"] == "proposal_expired"
        assert "theater_propose" in out["message"]


@pytest.mark.parametrize("change, field", [
    (lambda a: {**a, "label": "Somewhere else"}, "label"),
    (lambda a: {**a, "airframe": "group3_fixed_wing"}, "airframe"),
    (lambda a: {**a, "home_lat": a["home_lat"] + 1e-6}, "home_lat"),
    (lambda a: {**a, "ground_msl_m": a["ground_msl_m"] + 0.02}, "ground_msl_m"),
    (lambda a: {**a, "ao": [[a["ao"][0][0] + 1e-6, a["ao"][0][1]], *a["ao"][1:]]}, "ao"),
    (lambda a: {**a, "theater_id": "default"}, "theater_id"),
])
def test_arguments_that_differ_from_the_proposal_are_refused(tmp_path, change, field):
    with tools_server(tmp_path) as srv:
        p = blr_proposal(srv)
        out = call(srv, "sim_set_theater", **change(dict(p["set_args"])))
        assert srv.theater.id == "default" and srv.theater_epoch == 0
    assert out["rejected"] is True and out["error"] == "proposal_mismatch"
    assert out["fields"] == [field] and field in out["message"]


def test_arguments_within_the_tolerances_still_match(tmp_path):
    with tools_server(tmp_path) as srv:
        a = dict(blr_proposal(srv)["set_args"])
        a.update(home_lat=a["home_lat"] + 5e-8, ground_msl_m=a["ground_msl_m"] + 0.005)
        out = call(srv, "sim_set_theater", **a)
    assert out["status"] == "accepted"


@pytest.mark.parametrize("bad", [{"ao": [[1.0, 2.0], [3.0, 4.0]]},
                                 {"ao": [[1.0, 2.0]] * 13},
                                 {"ao": [[1.0, "x"], [1.0, 2.0], [3.0, 4.0]]},
                                 {"home_lat": float("nan")}, {"label": ""}])
def test_malformed_set_args_are_refused_before_any_lookup(tmp_path, bad):
    with tools_server(tmp_path) as srv:
        a = dict(blr_proposal(srv)["set_args"])
        out = run(theater_tools.sim_set_theater(srv, {**a, **bad}))
    assert out["rejected"] is True and out["error"] == "invalid_parameter"


def test_a_switch_refusal_changes_nothing_and_names_the_reason(tmp_path):
    with tools_server(tmp_path, listener=False) as srv:
        p = blr_proposal(srv)
        out = call(srv, "sim_set_theater", **p["set_args"])
        assert srv.theater.id == "default" and srv.theater_epoch == 0
        assert not srv._switching.is_set()
    assert out["rejected"] is True and out["error"] == "switch_refused"
    assert "runtime theater change needs the app host" in out["reasons"]
    with tools_server(tmp_path / "boot", recovered=False) as srv:
        out = call(srv, "sim_set_theater", **blr_proposal(srv)["set_args"])
    assert out["error"] == "switch_refused"
    assert out["reasons"] == ["restart recovery still running"]


def test_the_handler_hops_to_the_tasking_loop_with_the_caller_via(tmp_path, monkeypatch):
    from godseye_uav import theater_switch

    seen = {}

    async def spy_switch(srv, p, via):
        seen.update(thread=threading.current_thread().name, via=via, pid=p.proposal_id)
        return {"ok": True, "status": "accepted", "theater": {"id": p.theater.id}}

    async def as_console(srv, a):
        token = theater_tools.CALL_VIA.set("console")
        try:
            return await theater_tools.sim_set_theater(srv, a)
        finally:
            theater_tools.CALL_VIA.reset(token)

    fake = SimpleNamespace(switch=spy_switch, SwitchRefused=theater_switch.SwitchRefused)
    with tools_server(tmp_path) as srv:
        p = blr_proposal(srv)
        monkeypatch.setattr(theater_tools, "_switch", lambda: fake)
        out = run(as_console(srv, p["set_args"]))
    assert out["status"] == "accepted"
    assert seen == {"thread": "godseye-tasking", "via": "console",
                    "pid": p["proposal_id"]}


def test_a_crashing_switch_is_reported_not_raised(tmp_path, monkeypatch):
    from godseye_uav import theater_switch

    async def boom(srv, p, via):
        raise RuntimeError("simulated failure")

    fake = SimpleNamespace(switch=boom, SwitchRefused=theater_switch.SwitchRefused)
    with tools_server(tmp_path) as srv:
        p = blr_proposal(srv)
        monkeypatch.setattr(theater_tools, "_switch", lambda: fake)
        out = call(srv, "sim_set_theater", idempotency_key="k-boom", **p["set_args"])
        assert srv._idem_replay("sim_set_theater", "k-boom") is None   # not recorded
    assert out["rejected"] is True and out["error"] == "theater_switch_failed"
    assert "RuntimeError: simulated failure" in out["message"]


# --------------------------------------------------- approval_preview --

#: §3.6: a `theater_preview` without any of these makes the slip Deny-only.
PREVIEW_REQUIRED = {"checks", "center", "bbox", "home", "airframe", "ground_msl_m"}


def test_the_theater_preview_has_every_required_key(tmp_path):
    with tools_server(tmp_path) as srv:
        srv.monitor_for("Drone1")
        p = blr_proposal(srv)
        out = theater_tools.approval_preview(srv, "sim_set_theater", p["set_args"])
    assert PREVIEW_REQUIRED <= set(out)
    assert out["center"] == [BLR[0], BLR[1]] and out["ground_msl_m"] == 920.0
    assert out["airframe"]["to"] == "quad_suas_electric"
    assert {"text": "Proposal still valid", "ok": True} in out["checks"]
    assert "mismatch" not in out


def test_a_mismatched_request_fails_the_proposal_check(tmp_path):
    with tools_server(tmp_path) as srv:
        a = dict(blr_proposal(srv)["set_args"])
        out = theater_tools.approval_preview(srv, "sim_set_theater", {**a, "label": "Else"})
    assert {"text": "Proposal still valid", "ok": False} in out["checks"]
    assert [c["text"] for c in out["checks"]].count("Proposal still valid") == 1
    assert out["mismatch"] == ["label"]


def test_the_preview_is_empty_when_it_cannot_be_built(tmp_path):
    class Raising:
        def get(self, _pid):
            raise RuntimeError("book unreadable")

    with tools_server(tmp_path) as srv:
        a = dict(blr_proposal(srv)["set_args"])
        unknown = theater_tools.approval_preview(
            srv, "sim_set_theater", {**a, "proposal_id": "TP-00000000"})
        other = theater_tools.approval_preview(srv, "uav_takeoff", {"vehicle": "Drone1"})
        none = theater_tools.approval_preview(srv, "sim_set_theater", None)
        srv.theater_proposals = Raising()
        raising = theater_tools.approval_preview(srv, "sim_set_theater", a)
    assert unknown == {} and other == {} and none == {} and raising == {}
    assert theater_tools.approval_preview(SimpleNamespace(), "sim_set_time_scale",
                                          {"scale": 2})["checks"] == [
        {"text": "Fake simulator", "ok": False}]


def test_after_a_switch_the_sites_and_their_chip_ids_are_the_new_theaters(tmp_path):
    with tools_server(tmp_path, geodata=True, wire=online_wire()) as srv:
        place = call(srv, "geo_lookup", query="Kherson")["candidates"][0]
        p = call(srv, "theater_propose", lat=place["lat"], lon=place["lon"],
                 bbox=place["bbox"], place_id=place["id"], label="Kherson")
        out = call(srv, "sim_set_theater", **p["set_args"])
        rows = call(srv, "geo_sites", limit=60)
    tid = out["theater"]["id"]
    assert rows["theater"] == {"id": tid, "epoch": 1}
    assert rows["total"] == p["sites"]["total"] > 0
    assert all(r["id"].startswith(f"sit:{tid}:") for r in rows["sites"])


# ------------------------------------------------------ TOOL_CONTRACT --

def test_tool_contract_documents_every_phase_a_tool_and_its_parameters(tmp_path):
    doc = (pathlib.Path(__file__).parents[1] / "TOOL_CONTRACT.md").read_text()
    rows = {line.split("|")[1].strip().strip("`"): line
            for line in doc.splitlines() if line.startswith("| `")}
    with tools_server(tmp_path) as srv:
        for name in theater_tools.TOOL_NAMES:
            assert name in rows, f"{name} is not in TOOL_CONTRACT.md"
            params = srv.mcp._tool_manager._tools[name].parameters.get("properties", {})
            missing = [p for p in params if p not in rows[name]]
            assert not missing, f"{name}: undocumented parameters {missing}"
    assert "51 tools" in doc


def test_a_home_or_ground_correction_under_the_same_id_is_switched_not_dropped(tmp_path):
    """Review A (medium): the dynamic id hashes only the label and the AO, so a
    corrected ground or home over the same area keeps the id. An approved
    correction must still move the origin; only an identical row is unchanged."""
    with tools_server(tmp_path) as srv:
        first = blr_proposal(srv, ground_msl_m=0.0)
        assert call(srv, "sim_set_theater", **first["set_args"])["status"] == "accepted"
        fix = blr_proposal(srv, ground_msl_m=920.0, home_lat=12.975, home_lon=77.60)
        assert fix["set_args"]["theater_id"] == first["set_args"]["theater_id"]
        out = call(srv, "sim_set_theater", **fix["set_args"])
        assert out["status"] == "accepted", out
        assert out["theater"]["epoch"] == 2 and srv.theater_epoch == 2
        assert srv.theater.home_alt_msl_m == 920.0
        assert (srv.theater.home_lat, srv.theater.home_lon) == (12.975, 77.60)
        want = canonical_altitude(920.0, 12.975, 77.60, datum="msl").alt_hae
        assert srv.backend.home_geo.altitude == pytest.approx(want, abs=1e-3)
        assert tuple(srv.envelope.home) == srv.theater.home
        same = blr_proposal(srv, ground_msl_m=920.0, home_lat=12.975, home_lon=77.60)
        again = call(srv, "sim_set_theater", **same["set_args"])
        assert again["status"] == "unchanged" and srv.theater_epoch == 2
        relabel = blr_proposal(srv, ground_msl_m=920.0, home_lat=12.975, home_lon=77.60,
                               label="Bengaluru Centre")
        assert relabel["set_args"]["theater_id"] == first["set_args"]["theater_id"]
        assert call(srv, "sim_set_theater", **relabel["set_args"])["status"] == "accepted"
        assert srv.theater.label == "Bengaluru Centre" and srv.theater_epoch == 3
