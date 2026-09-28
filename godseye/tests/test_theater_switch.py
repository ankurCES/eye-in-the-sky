"""Runtime theater switch (WG v2 §4.1.3, §4.1.9 #2, #3, #12; A4): `theater_switch.py`.

The A4 acceptance list for the switch half: success; every refusal
(airborne, busy, BINGO, link lost, real AirSim, no app host, restart recovery
still running, and the rest of the table); integrity (a stale origin holder
sets `theater_integrity_error` and every `_submit` is refused); the envelope
keeps its identity and `fuel.home` follows; a route in the new AO is accepted
and one back to the old AO is refused (`wp0:geofence`); no tick straddles a
switch; and the persistence round trip.

Real server, real fake simulator (msgpack-RPC on 53000-53099, A4's range),
real bridge `AirSimAdapter` as the origin holder (it never connects: only
its origin copy is exercised). No test touches the network.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import itertools
import json
import os
import threading
import time
from contextlib import contextmanager, suppress
from types import SimpleNamespace

import airsim
import pytest
from godseye_uav import server as server_mod
from godseye_uav import theater_plan, theater_switch, theaters
from godseye_uav.bridge import AirSimAdapter
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoidUnavailableError, GeoPoint, canonical_altitude
from godseye_uav.realdata import HttpResponse
from godseye_uav.safety import LinkState, SafetyEnvelope, haversine_m
from godseye_uav.server import GodseyeUavServer, UavBackend
from godseye_uav.store import Store
from godseye_uav.tasking import TaskState

_PORTS = list(range(53000, 53100))
_PORT = itertools.cycle(_PORTS[os.getpid() % len(_PORTS):]
                        + _PORTS[:os.getpid() % len(_PORTS)])

THEATER = theaters.get("default")
HOME = GeoPoint(THEATER.home_lat, THEATER.home_lon,
                canonical_altitude(THEATER.home_alt_msl_m, THEATER.home_lat,
                                   THEATER.home_lon, datum="msl").alt_hae)
LAT, LON = 12.9716, 77.5946
QUAD, GROUP3 = "quad_suas_electric", "group3_fixed_wing"


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
    raise RuntimeError(f"no free port in 53000-53099: {last}")


@contextmanager
def rig(tmp_path, *, with_sim=True, listener=True, recovered=True, holder=True, **kw):
    """A server as the app host wires it: fake sim, bridge adapter attached as
    an origin holder, a theater listener, restart recovery finished."""
    sim = _start_sim()
    store = Store(tmp_path)
    srv = None
    try:
        client = airsim.MultirotorClient(port=sim.port)
        client.confirmConnection()
        backend = UavBackend(client, HOME, sim=sim if with_sim else None)
        srv = GodseyeUavServer(backend, store, theater=THEATER, watchdog_s=30.0,
                               envelope=SafetyEnvelope(**THEATER.envelope_kwargs()), **kw)
        adapter = AirSimAdapter(port=sim.port, home=HOME)
        if holder:
            srv.attach_origin_holder(adapter)
        events: list[dict] = []
        if listener:
            srv.theater_listeners.append(events.append)
        if recovered:
            srv.boot_recovery_done.set()
        srv.monitor_for("Drone1")
        yield SimpleNamespace(srv=srv, sim=sim, adapter=adapter, events=events,
                              store=store, client=client)
    finally:
        if srv is not None:
            srv.stop_monitor()
            with suppress(Exception):
                srv.tasking.shutdown()
        store.close()
        sim.stop()


def run(coro):
    return asyncio.run(coro)


def on_loop(srv, coro, timeout=30.0):
    """Run `coro` on the tasking loop, as `theater_tools` does."""
    return asyncio.run_coroutine_threadsafe(coro, srv.tasking.loop).result(timeout)


def do_switch(srv, p, via="mcp"):
    return on_loop(srv, theater_switch.switch(srv, p, via))


def tool(srv, name):
    return srv.mcp._tool_manager._tools[name].fn


def audits(srv, kind: str) -> list[dict]:
    return [r for r in srv.store.audit.read_all() if r.get("kind") == kind]


def proposal(srv, **kw) -> theater_plan.Proposal:
    kw.setdefault("lat", LAT)
    kw.setdefault("lon", LON)
    kw.setdefault("label", "Bengaluru centre")
    kw.setdefault("ground_msl_m", 920.0)
    kw.setdefault("airframe", QUAD)
    p = theater_plan.build_proposal(**kw)
    assert isinstance(p, theater_plan.Proposal), p
    return theater_plan.book_of(srv).put(p)


def origin_of(value):
    return getattr(value, "geo", value)


def assert_at(geo, want, tol_deg=1e-9, tol_m=1e-3):
    g = origin_of(geo)
    assert abs(g.latitude - want.latitude) < tol_deg
    assert abs(g.longitude - want.longitude) < tol_deg
    assert abs(g.altitude - want.altitude) < tol_m


# ------------------------------------------------------------------ success --

def test_a_switch_moves_every_origin_copy_and_everything_derived(tmp_path):
    with rig(tmp_path) as r:
        srv = r.srv
        envelope, mon = srv.envelope, srv.monitor_for("Drone1")
        mon.fuel.fuel_pct = 77.0
        mon._active_alarms.add("geofence")
        mon._breach_since["geofence"] = 1.0
        srv._last_tele["Drone1"] = {"landed_state": 0}
        srv.ticks["Drone1"] = {"x": 1}
        epoch0 = srv.tracks.sim_epoch
        p = proposal(srv)
        out = do_switch(srv, p, via="console")
        t = p.theater
        assert out["ok"] is True and out["status"] == "accepted", out
        assert out["theater"] == {"id": t.id, "label": "Bengaluru centre", "epoch": 1,
                                  "dynamic": True}
        assert out["previous"] == {"id": "default", "label": THEATER.label}
        fix = canonical_altitude(920.0, t.home_lat, t.home_lon, datum="msl")
        assert out["home"] == {"lat": t.home_lat, "lon": t.home_lon, "alt_msl_m": 920.0,
                               "alt_hae_m": round(fix.alt_hae, 3),
                               "undulation_m": round(fix.undulation_m, 3),
                               "datum_source": fix.source}
        assert out["airframe"] == {"id": QUAD, "changed": False} and out["fuel"] == "kept"
        assert out["reset"] == list(theater_switch.RESETS)
        assert out["kept"] == list(theater_switch.KEEPS)
        assert out["real_data"] == "off" and out["sites_loaded"] == 0
        # every copy of the origin, and the simulator's own answer, agree (T1: HAE)
        new_home = GeoPoint(t.home_lat, t.home_lon, fix.alt_hae)
        assert_at(r.sim.home_geo, new_home)
        assert_at(srv.backend.home_geo, new_home)
        assert_at(srv.backend.home, new_home)
        assert_at(r.adapter.home_geo, new_home)
        assert_at(run(srv.backend.home_rpc()), new_home)
        rpc = r.client.getHomeGeoPoint()                      # over msgpack-RPC
        assert abs(rpc.latitude - t.home_lat) < 1e-9
        assert abs(rpc.longitude - t.home_lon) < 1e-9
        assert srv.theater_integrity_error is None
        # the envelope is moved IN PLACE: monitors hold the reference
        assert srv.envelope is envelope and mon.envelope is envelope
        assert envelope.geofence == t.ao_list() and envelope.home == t.home
        # same airframe: level kept, home follows; alarms in progress cleared
        assert srv.monitor_for("Drone1") is mon
        assert mon.fuel.fuel_pct == 77.0 and mon.fuel.home == t.home
        assert not mon._active_alarms and not mon._breach_since
        # server state
        assert srv.theater is t and srv.theater_epoch == 1
        assert srv.theater_set_via == "console" and srv.theater_mismatch is None
        assert srv.theater_previous == {"id": "default", "label": THEATER.label}
        assert abs(srv.theater_set_at_ms - time.time() * 1000) < 60_000
        assert theaters.get(t.id) is not None and theaters.is_known(t.id)
        assert srv.sites is p.sites
        assert srv.terrain_grid_deg == server_mod.terrain_grid_for(t.bbox())
        assert srv._last_tele == {} and srv.ticks == {}
        assert srv.tracks.sim_epoch == epoch0 + 1
        assert srv.real_world is None
        assert not srv._switching.is_set()
        # the drone is parked, landed, at the new home
        tele = run(srv.backend.telemetry("Drone1"))
        assert tele["landed_state"] == 0
        assert haversine_m(tele["lat"], tele["lon"], t.home_lat, t.home_lon) < 1.0
        # listeners told, state persisted, audited
        assert [e["theater_id"] for e in r.events] == [t.id]
        doc = json.loads((tmp_path / "theater.json").read_text())
        assert doc["schema"] == theater_switch.SCHEMA and doc["epoch"] == 1
        assert doc["set_via"] == "console" and doc["theater"]["id"] == t.id
        row = audits(srv, "theater_changed")[-1]
        assert row["old"] == "default" and row["new"] == t.id and row["epoch"] == 1
        assert row["datum"]["home_hae_m"] == round(fix.alt_hae, 3)


def test_after_a_switch_the_new_ao_flies_and_the_old_one_is_refused(tmp_path):
    with rig(tmp_path) as r:
        srv = r.srv
        p = proposal(srv)
        assert do_switch(srv, p)["ok"]
        back = run(tool(srv, "uav_goto_gps")(vehicle="Drone1", lat=THEATER.home_lat,
                                             lon=THEATER.home_lon, alt_agl_m=60.0))
        assert back["rejected"] is True
        assert any(v.startswith("wp0:") and "geofence" in v
                   for v in back["gate"]["envelope_violations"])
        n, _ = p.theater.point_at(500.0, 0.0)
        go = run(tool(srv, "uav_goto_gps")(vehicle="Drone1", lat=n, lon=p.theater.home_lon,
                                           alt_agl_m=60.0))
        assert go.get("status") == "accepted", go


# ----------------------------------------------------------------- refusals --

def _airborne(r, mp):
    r.client.takeoffAsync(vehicle_name="Drone1").join()


def _busy(r, mp):
    task = SimpleNamespace(tool="uav_fly_route", state=TaskState.EXECUTING)
    mp.setattr(r.srv.tasking, "queue_for", lambda v: SimpleNamespace(active=lambda: task))


def _bingo(r, mp):
    r.srv.monitor_for("Drone1").fuel.bingo.tripped = True


def _link_lost(r, mp):
    r.srv.monitor_for("Drone1").link.state = LinkState.LOAL


def _roster(r, mp):
    async def boom():
        raise RuntimeError("rpc down")
    mp.setattr(r.srv.backend, "list_vehicles", boom)


def _telemetry(r, mp):
    async def boom(vehicle):
        raise RuntimeError("datalink lost")
    mp.setattr(r.srv.backend, "telemetry", boom)


def _rtb(r, mp):
    r.srv._rtb_task["Drone1"] = SimpleNamespace(state=TaskState.EXECUTING)


def _geoid(r, mp):
    def boom(*a, **k):
        raise GeoidUnavailableError("no EGM96 grid")
    mp.setattr(theater_switch, "canonical_altitude", boom)


def _tick(r, mp):
    async def never(timeout_s):
        return False
    mp.setattr(r.srv, "wait_ticks_idle", never)


def _wargame(r, mp):
    r.srv.wargame = SimpleNamespace(active=False, starting=threading.Event())
    r.srv.wargame.starting.set()


REFUSALS = [
    ("airborne", {}, _airborne, "Drone1: airborne (landed_state="),
    ("busy", {}, _busy, "Drone1: busy (uav_fly_route executing)"),
    ("bingo", {}, _bingo, "Drone1: BINGO latched; refuel with sim_set_fuel first"),
    ("link", {}, _link_lost, "Drone1: link lost"),
    ("roster", {}, _roster, theater_switch.MSG_ROSTER),
    ("telemetry", {}, _telemetry, "Drone1: telemetry unavailable, cannot prove it is landed"),
    ("rtb", {}, _rtb, theater_switch.MSG_RTB),
    ("geoid", {}, _geoid, theater_switch.MSG_GEOID),
    ("real", {"with_sim": False}, None, theater_switch.MSG_REAL_AIRSIM),
    ("no_host", {"listener": False}, None, theater_switch.MSG_NO_HOST),
    ("recovery", {"recovered": False}, None, theater_switch.MSG_RECOVERY),
    ("tick", {}, _tick, theater_switch.MSG_TICK),
    ("wargame", {}, _wargame, theater_switch.MSG_WARGAME),
]


@pytest.mark.parametrize("name, rig_kw, breaker, reason", REFUSALS,
                         ids=[x[0] for x in REFUSALS])
def test_each_refusal_moves_nothing(tmp_path, monkeypatch, name, rig_kw, breaker, reason):
    with rig(tmp_path, **rig_kw) as r:
        srv = r.srv
        if breaker is not None:
            breaker(r, monkeypatch)
        p = proposal(srv)
        envelope = list(srv.envelope.geofence)
        out = do_switch(srv, p)
        assert out["rejected"] is True and out["error"] == "switch_refused", out
        assert any(x.startswith(reason) for x in out["reasons"]), out
        assert out["message"] == "; ".join(out["reasons"])
        assert srv.theater is THEATER and srv.theater_epoch == 0
        assert srv.envelope.geofence == envelope
        assert_at(r.sim.home_geo, HOME)
        assert_at(r.adapter.home_geo, HOME)
        assert srv.theater_integrity_error is None and not srv._switching.is_set()
        assert not (tmp_path / "theater.json").exists() and r.events == []
        if name not in ("wargame",):
            assert audits(srv, "theater_switch_refused")[-1]["reasons"] == out["reasons"]


def test_restart_recovery_gates_the_switch_until_it_finishes(tmp_path):
    with rig(tmp_path, recovered=False) as r:
        p = proposal(r.srv)
        assert do_switch(r.srv, p)["reasons"] == [theater_switch.MSG_RECOVERY]
        r.srv.boot_recovery_done.set()
        assert do_switch(r.srv, p)["status"] == "accepted"


def test_a_switch_already_running_is_refused_and_not_cleared(tmp_path):
    with rig(tmp_path) as r:
        r.srv._switching.set()
        out = do_switch(r.srv, proposal(r.srv))
        assert out["reasons"] == [theater_switch.MSG_ALREADY_SWITCHING]
        assert r.srv._switching.is_set()          # the other switch still owns it
        r.srv._switching.clear()


def test_validate_problems_are_refused_verbatim(tmp_path):
    with rig(tmp_path) as r:
        p = proposal(r.srv)
        bad_t = dataclasses.replace(p.theater, home_lat=p.theater.home_lat + 1.0)
        out = do_switch(r.srv, dataclasses.replace(p, theater=bad_t))
        assert out["reasons"] == theaters.validate([bad_t]) != []


# ---------------------------------------------------------------- integrity --

class StaleHolder:
    """An origin holder that does not move: the defect step 2e exists for."""

    def __init__(self, home):
        self.home_geo = home

    def relocate(self, new_home):
        pass


class BrokenHolder(StaleHolder):
    def relocate(self, new_home):
        raise RuntimeError("adapter wedged")


@pytest.mark.parametrize("holder_cls", [StaleHolder, BrokenHolder])
def test_a_stale_origin_copy_refuses_every_submit(tmp_path, holder_cls):
    with rig(tmp_path) as r:
        srv = r.srv
        srv.attach_origin_holder(holder_cls(HOME))
        out = do_switch(srv, proposal(srv))
        assert out["rejected"] is True and out["error"] == "theater_integrity", out
        assert srv.theater_integrity_error
        assert audits(srv, "theater_integrity")
        assert not srv._switching.is_set()
        # land and hover are exempt (they never use the origin); see below
        refused = srv._submit("Drone1", "uav_takeoff", {"alt_agl_m": 10.0}, None)
        assert refused["rejected"] is True and refused["error"] == "theater_integrity"
        assert r.events == [] and not (tmp_path / "theater.json").exists()


def test_the_simulators_own_answer_is_part_of_the_cross_check(tmp_path, monkeypatch):
    with rig(tmp_path) as r:
        async def elsewhere():
            return HOME
        monkeypatch.setattr(r.srv.backend, "home_rpc", elsewhere)
        out = do_switch(r.srv, proposal(r.srv))
        assert out["error"] == "theater_integrity"
        assert "getHomeGeoPoint" in r.srv.theater_integrity_error


# ------------------------------------------------------------ tick ordering --

def test_no_tick_straddles_a_switch(tmp_path, monkeypatch):
    with rig(tmp_path) as r:
        srv, order = r.srv, []
        body = srv._tick_once_body

        async def slow_body(vehicle, now):
            order.append("tick start")
            await asyncio.sleep(0.4)            # a tick in flight on the OLD geometry
            out = await body(vehicle, now)
            order.append("tick end")
            return out
        monkeypatch.setattr(srv, "_tick_once_body", slow_body)
        relocate = r.sim.relocate_origin

        def spy_relocate(geo):
            order.append("origin moved")
            return relocate(geo)
        monkeypatch.setattr(r.sim, "relocate_origin", spy_relocate)
        refusals, skipped = theater_switch.refusals, []

        async def spy_refusals(srv_, t):
            skipped.append(await srv_.tick_once("Drone1"))   # a tick DURING the switch
            return await refusals(srv_, t)
        monkeypatch.setattr(theater_switch, "refusals", spy_refusals)
        tick = asyncio.run_coroutine_threadsafe(srv.tick_once("Drone1"), srv.tasking.loop)
        deadline = time.monotonic() + 5.0
        while srv._ticks_inflight == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert srv._ticks_inflight == 1
        out = do_switch(srv, proposal(srv))
        assert out["status"] == "accepted", out
        tick.result(10)
        assert order == ["tick start", "tick end", "origin moved"]
        assert skipped == [{"skipped": "theater switch"}]


# ----------------------------------------------------------------- airframe --

def test_an_airframe_change_is_a_fresh_full_tank_at_the_current_sim_speed(tmp_path):
    with rig(tmp_path) as r:
        srv = r.srv
        srv.set_time_scale(4.0)
        old = srv.monitor_for("Drone1")
        old.fuel.fuel_pct = 41.0
        out = do_switch(srv, proposal(srv, airframe=GROUP3))
        assert out["airframe"] == {"id": GROUP3, "changed": True}
        assert out["fuel"] == "full tank (airframe changed)"
        fuel = srv.monitor_for("Drone1").fuel
        assert srv.airframe_id == GROUP3 and fuel.airframe.id == GROUP3
        assert fuel.fuel_pct == 100.0 and fuel.time_scale == 4.0
        assert fuel.home == srv.theater.home and not fuel.bingo.tripped
        assert srv.monitor_for("Drone1") is old             # the monitor survives
        rows = [f for f in srv.store.fuel.read_all()
                if f.get("reason") == "airframe_changed"]
        assert rows and rows[-1]["airframe"] == GROUP3 and rows[-1]["fuel_pct"] == 100.0
        assert audits(srv, "airframe_changed")[-1]["previous"] == QUAD
        doc = json.loads((tmp_path / "theater.json").read_text())
        assert doc["airframe"] == GROUP3 and "time_scale" not in doc


# ------------------------------------------- real data, listeners, POIs --

def test_real_data_restarts_after_the_epoch_moves(tmp_path, monkeypatch):
    with rig(tmp_path) as r:
        srv, seen = r.srv, []
        srv.real = SimpleNamespace()
        monkeypatch.setattr(srv, "stop_real_data", lambda: seen.append(("stop",)))
        monkeypatch.setattr(srv, "start_real_data",
                            lambda: seen.append(("start", srv.theater.id, srv.theater_epoch)))
        p = proposal(srv)
        out = do_switch(srv, p)
        assert out["real_data"] == "reloading in background"
        assert seen == [("stop",), ("start", p.theater.id, 1)]


def test_a_failing_listener_never_fails_the_switch(tmp_path):
    with rig(tmp_path) as r:
        def boom(state):
            raise RuntimeError("listener bug")
        r.srv.theater_listeners.insert(0, boom)
        out = do_switch(r.srv, proposal(r.srv))
        assert out["status"] == "accepted"
        assert len(r.events) == 1 and audits(r.srv, "theater_listener_failed")


def _sites_fetch(url, timeout_s, data=None):
    return HttpResponse(200, json.dumps({"elements": [
        {"type": "node", "id": 7, "lat": 12.9800, "lon": 77.6000,
         "tags": {"power": "substation", "name": "North substation"}}]}))


def test_the_new_theaters_pois_are_defined_and_old_baselines_kept(tmp_path):
    with rig(tmp_path) as r:
        before = set(r.srv.pol.pois())
        p = proposal(r.srv, geodata=True, fetch=_sites_fetch)
        assert [poi.name for poi in p.theater.pois] == ["North substation"]
        assert do_switch(r.srv, p)["sites_loaded"] == 1
        assert set(r.srv.pol.pois()) == before | {"North substation"}
        assert r.srv.sites.total == 1


def test_switching_to_a_preset_stores_it_by_id(tmp_path):
    with rig(tmp_path) as r:
        p = theater_plan.book_of(r.srv).put(
            theater_plan.build_proposal(theater_id="iran-isfahan"))
        out = do_switch(r.srv, p)
        assert out["theater"]["dynamic"] is False and r.srv.theater.id == "iran-isfahan"
        doc = json.loads((tmp_path / "theater.json").read_text())
        assert doc["theater_id"] == "iran-isfahan" and doc["theater"] is None


# -------------------------------------------------------------- persistence --

def test_persistence_round_trip_registers_the_dynamic_theater(tmp_path):
    with rig(tmp_path) as r:
        p = proposal(r.srv, airframe=GROUP3)
        assert do_switch(r.srv, p)["ok"]
        state = theater_switch.state_of(r.srv)
    theaters.clear_dynamic()
    assert not theaters.is_known(p.theater.id)
    got = theater_switch.load_persisted(tmp_path)
    assert got == {"theater_id": p.theater.id, "airframe": GROUP3, "epoch": 1}
    back = theaters.get(p.theater.id)
    assert back == p.theater and back.dynamic
    assert json.loads(json.dumps(back.as_dict()["provenance"])) == json.loads(
        json.dumps(p.theater.as_dict()["provenance"]))
    assert state["previous"] == {"id": "default", "label": THEATER.label}
    assert "real_data" not in state["theater"]
    again = theater_switch.load_persisted(Store(tmp_path))
    assert again == got                        # a Store works as well as a path
    assert [f.name for f in tmp_path.iterdir() if f.name.startswith(".theater-")] == []


def _write(tmp_path, doc) -> None:
    text = doc if isinstance(doc, str) else json.dumps(doc)
    (tmp_path / "theater.json").write_text(text, encoding="utf-8")


def _good_dynamic() -> dict:
    t = theaters.make_dynamic(label="Test area", place="Somewhere", center=(LAT, LON),
                              half_extent_m=2000.0, home=(LAT, LON), home_alt_msl_m=900.0,
                              provenance={"proposal_id": "TP-00000000"})
    row = t.as_dict()
    return {"schema": theater_switch.SCHEMA, "theater_id": t.id, "theater": row,
            "airframe": QUAD, "epoch": 3, "set_at_ms": 1, "set_via": "console",
            "previous": {"id": "default", "label": "x"}}


@pytest.mark.parametrize("mutate, fragment", [
    (lambda d: "{not json", "unreadable"),
    (lambda d: {**d, "schema": "other/v9"}, "not a godseye.theater-state/v1"),
    (lambda d: {**d, "theater_id": ""}, "names no theater"),
    (lambda d: {**d, "theater_id": "atlantis", "theater": None}, "unknown theater"),
    (lambda d: {**d, "theater": None}, "has no row"),
    (lambda d: {**d, "theater": {**d["theater"], "id": "dyn-other-000000"}}, "is not"),
    (lambda d: {**d, "theater": {**d["theater"], "ao": [[1, 2]]}}, "is invalid"),
    (lambda d: {**d, "airframe": "zeppelin"}, "unknown airframe"),
    (lambda d: {**d, "epoch": -1}, "epoch"),
    (lambda d: {**d, "epoch": True}, "epoch"),
    (lambda d: [1, 2, 3], "not a godseye.theater-state/v1"),
])
def test_a_corrupt_state_file_is_an_error_never_an_exception(tmp_path, mutate, fragment):
    _write(tmp_path, mutate(_good_dynamic()))
    got = theater_switch.load_persisted(tmp_path)
    assert set(got) == {"error"} and fragment in got["error"], got


def test_load_persisted_happy_paths_and_absences(tmp_path):
    assert theater_switch.load_persisted(tmp_path) is None
    assert theater_switch.load_persisted(Store(":memory:")) is None
    assert theater_switch.load_persisted(None) is None
    doc = _good_dynamic()
    _write(tmp_path, doc)
    assert theater_switch.load_persisted(tmp_path) == {
        "theater_id": doc["theater_id"], "airframe": QUAD, "epoch": 3}
    assert theaters.is_known(doc["theater_id"])
    _write(tmp_path, {**doc, "theater_id": "iran-natanz", "theater": None, "airframe": None})
    assert theater_switch.load_persisted(tmp_path) == {
        "theater_id": "iran-natanz", "airframe": None, "epoch": 3}


def test_persist_is_atomic_and_never_raises(tmp_path):
    state = {"schema": theater_switch.SCHEMA, "theater_id": "default", "epoch": 0}
    path = theater_switch.persist(tmp_path, state)
    assert path == tmp_path / "theater.json"
    assert json.loads(path.read_text()) == state
    assert theater_switch.persist(tmp_path, {**state, "epoch": 5}) == path
    assert json.loads(path.read_text())["epoch"] == 5
    assert theater_switch.persist(Store(":memory:"), state) is None
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert theater_switch.persist(blocker, state) is None      # not a directory
    assert theater_switch.persist(tmp_path, {"bad": float("nan")}) is None
    assert json.loads(path.read_text())["epoch"] == 5          # the old file survives
    assert sorted(f.name for f in tmp_path.iterdir()) == ["file", "theater.json"]


# ------------------------------- cancellation and integrity (review A, safety) --

def _slow_home_rpc(srv, monkeypatch, delay_s=0.5):
    """`getHomeGeoPoint` slowed down: the one await after the origin moves."""
    real, entered = srv.backend.home_rpc, threading.Event()

    async def slow():
        entered.set()
        await asyncio.sleep(delay_s)
        return await real()
    monkeypatch.setattr(srv.backend, "home_rpc", slow)
    return entered


def _switch_over(srv, timeout=15.0):
    deadline = time.monotonic() + timeout
    while srv._switching.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not srv._switching.is_set(), "the switch never finished"


def assert_consistent(r, theater, holders=True):
    """Every origin copy, the fence and the fuel home agree with `srv.theater`."""
    srv = r.srv
    fix = canonical_altitude(theater.home_alt_msl_m, theater.home_lat, theater.home_lon,
                             datum="msl")
    home = GeoPoint(theater.home_lat, theater.home_lon, fix.alt_hae)
    assert srv.theater == theater
    assert_at(r.sim.home_geo, home)
    assert_at(srv.backend.home_geo, home)
    assert_at(srv.backend.home, home)
    if holders:
        assert_at(r.adapter.home_geo, home)
    assert tuple(srv.envelope.home) == theater.home
    assert srv.envelope.geofence == theater.ao_list()
    assert tuple(srv.fuel_for("Drone1").home) == theater.home


def test_a_caller_cancelled_during_the_cross_check_never_half_moves_the_origin(
        tmp_path, monkeypatch):
    """Stop, or closing the chat, cancels the in-process tool call while the
    switch awaits getHomeGeoPoint. The switch runs to its end on the tasking
    loop; it never leaves the origin moved and the theater, fence and fuel
    homes old (review A, critical)."""
    with rig(tmp_path) as r:
        srv = r.srv
        entered = _slow_home_rpc(srv, monkeypatch)
        p = proposal(srv)
        args = json.loads(json.dumps(dict(p.set_args)))

        async def caller():
            task = asyncio.ensure_future(tool(srv, "sim_set_theater")(**args))
            assert await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            return task.cancelled()

        assert run(caller()) is True
        _switch_over(srv)
        assert srv.theater_integrity_error is None
        assert_consistent(r, p.theater)
        assert srv.theater_epoch == 1 and [e["theater_id"] for e in r.events] == [p.theater.id]
        assert srv._submit_refusal(None) is None


def test_cancelling_the_switch_itself_still_runs_the_move_to_its_end(tmp_path, monkeypatch):
    with rig(tmp_path) as r:
        srv = r.srv
        entered = _slow_home_rpc(srv, monkeypatch)
        p = proposal(srv)
        fut = asyncio.run_coroutine_threadsafe(theater_switch.switch(srv, p, "console"),
                                               srv.tasking.loop)
        assert entered.wait(10)
        fut.cancel()
        _switch_over(srv)
        assert fut.cancelled()
        assert srv.theater_integrity_error is None
        assert_consistent(r, p.theater)
        assert audits(srv, "theater_changed")[-1]["new"] == p.theater.id


def test_a_cancellation_inside_the_move_rolls_every_copy_back_and_latches(
        tmp_path, monkeypatch):
    """The move itself cancelled (the tasking loop shutting down): every copy
    goes back to the old origin, the latch is set, and the cancellation still
    propagates."""
    with rig(tmp_path) as r:
        srv = r.srv

        async def cancelled():
            raise asyncio.CancelledError()
        monkeypatch.setattr(srv.backend, "home_rpc", cancelled)
        with pytest.raises(concurrent.futures.CancelledError):
            do_switch(srv, proposal(srv))
        assert not srv._switching.is_set()
        assert srv.theater_integrity_error and "put back" in srv.theater_integrity_error
        assert_consistent(r, THEATER)
        assert srv.theater_epoch == 0 and audits(srv, "theater_integrity")


def _blip(r, mp):
    real, calls = r.srv.backend.home_rpc, []

    async def blip():
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("getHomeGeoPoint timed out")
        return await real()
    mp.setattr(r.srv.backend, "home_rpc", blip)


INTEGRITY_BREAKERS = [
    ("stale", lambda r, mp: r.srv.attach_origin_holder(StaleHolder(HOME))),
    ("broken", lambda r, mp: r.srv.attach_origin_holder(BrokenHolder(HOME))),
    ("rpc_blip", _blip),
]


@pytest.mark.parametrize("name, breaker", INTEGRITY_BREAKERS,
                         ids=[x[0] for x in INTEGRITY_BREAKERS])
def test_after_an_integrity_failure_the_copies_are_put_back_and_nothing_flies(
        tmp_path, monkeypatch, name, breaker):
    """Review A (high): after a failed cross-check the next safety tick used to
    measure the parked drone against the old fuel home (13,000 km away) and fly
    an uncancellable BINGO RTB. The copies go back to the old origin (the
    drones were proven landed), and no forced RTB follows."""
    with rig(tmp_path) as r:
        srv = r.srv
        breaker(r, monkeypatch)
        out = do_switch(srv, proposal(srv))
        assert out["error"] == "theater_integrity", out
        assert "put back" in srv.theater_integrity_error
        assert_consistent(r, THEATER)
        for _ in range(3):
            verdict = on_loop(srv, srv.tick_once("Drone1"))
            assert not verdict.get("rtb_reasons"), verdict
        assert audits(srv, "force_rtb") == [] and srv._rtb_task.get("Drone1") is None
        assert not srv.fuel_for("Drone1").bingo.tripped
        tele = run(srv.backend.telemetry("Drone1"))
        assert tele["landed_state"] == 0
        assert haversine_m(tele["lat"], tele["lon"], THEATER.home_lat, THEATER.home_lon) < 1.0


def test_when_a_copy_cannot_be_put_back_enforcement_is_suspended_not_flown(
        tmp_path, monkeypatch):
    """The belt to the rollback's braces: the MCP backend's copy refuses to go
    back, so the monitor sees the parked drone 13,000 km from the old fuel
    home. The verdict says so, and nothing is flown on it."""
    with rig(tmp_path) as r:
        srv = r.srv
        relocate, moves = srv.backend.relocate, []

        def once(geo, fix):
            moves.append(geo)
            if len(moves) > 1:
                raise RuntimeError("backend wedged")
            return relocate(geo, fix)
        monkeypatch.setattr(srv.backend, "relocate", once)

        async def elsewhere():
            return HOME
        monkeypatch.setattr(srv.backend, "home_rpc", elsewhere)
        out = do_switch(srv, proposal(srv))
        assert out["error"] == "theater_integrity"
        assert "MCP backend" in srv.theater_integrity_error
        assert "not put back" in srv.theater_integrity_error
        verdict = on_loop(srv, srv.tick_once("Drone1"))
        assert verdict["enforcement_suspended"] == "theater_integrity", verdict
        assert audits(srv, "force_rtb") == [] and srv._rtb_task.get("Drone1") is None
        on_loop(srv, srv.tick_once("Drone1"))
        assert len(audits(srv, "enforcement_suspended")) == 1     # edge, not every tick


def test_land_and_hover_pass_the_integrity_refusal_and_nothing_else_does(tmp_path):
    """Land and hover never use the origin, so an operator can always use
    them; every command that does (a route, a return home) is still refused."""
    with rig(tmp_path) as r:
        srv = r.srv
        srv.attach_origin_holder(StaleHolder(HOME))
        assert do_switch(srv, proposal(srv))["error"] == "theater_integrity"
        for name in ("uav_land", "uav_hover"):
            assert srv._submit_refusal(None, tool=name) is None
        for name in ("uav_takeoff", "uav_return_to_home", "uav_goto_gps", "uav_fly_route"):
            assert srv._submit_refusal(None, tool=name)["error"] == "theater_integrity"
        assert srv._submit_refusal(None)["error"] == "theater_integrity"
        land = srv._submit("Drone1", "uav_land", {}, None)
        assert land["status"] == "accepted", land
        rth = run(tool(srv, "uav_return_to_home")(vehicle="Drone1"))
        assert rth["rejected"] is True and rth["error"] == "theater_integrity"


class SpyEvent(threading.Event):
    """`_switching`, counting how often a switch raised it."""

    def __init__(self):
        super().__init__()
        self.raised = 0

    def set(self):
        self.raised += 1
        super().set()


def _cached_airborne(r, mp):
    r.srv._last_tele["Drone1"] = {"landed_state": 1}


CACHED_REFUSALS = [
    ("airborne", {}, _cached_airborne, "Drone1: airborne (landed_state=1)"),
    ("busy", {}, _busy, "Drone1: busy (uav_fly_route executing)"),
    ("bingo", {}, _bingo, "Drone1: BINGO latched; refuel with sim_set_fuel first"),
    ("link", {}, _link_lost, "Drone1: link lost"),
    ("rtb", {}, _rtb, theater_switch.MSG_RTB),
    ("recovery", {"recovered": False}, None, theater_switch.MSG_RECOVERY),
    ("real", {"with_sim": False}, None, theater_switch.MSG_REAL_AIRSIM),
    ("no_host", {"listener": False}, None, theater_switch.MSG_NO_HOST),
]


@pytest.mark.parametrize("name, rig_kw, breaker, reason", CACHED_REFUSALS,
                         ids=[x[0] for x in CACHED_REFUSALS])
def test_a_switch_the_caches_already_refuse_never_raises_the_switching_flag(
        tmp_path, monkeypatch, name, rig_kw, breaker, reason):
    """Review A (medium): a refused switch used to raise `_switching` first, so
    a caller retrying while a drone flew skipped safety ticks and refused the
    operator's own commands. What the caches already prove is refused before
    the flag; the RPC-proven refusals stay the authoritative gate."""
    with rig(tmp_path, **rig_kw) as r:
        srv = r.srv
        srv._switching = spy = SpyEvent()
        if breaker is not None:
            breaker(r, monkeypatch)
        out = do_switch(srv, proposal(srv))
        assert out["error"] == "switch_refused" and reason in out["reasons"], out
        assert spy.raised == 0 and not spy.is_set()
        assert audits(srv, "theater_switch_refused")[-1]["reasons"] == out["reasons"]
        assert srv.theater is THEATER and srv.theater_epoch == 0
        assert_at(r.sim.home_geo, HOME)


def test_the_rpc_proven_refusals_still_gate_what_the_caches_miss(tmp_path):
    """An empty cache proves nothing either way: the RPC check still runs, under
    the flag, and still refuses an airborne drone."""
    with rig(tmp_path) as r:
        srv = r.srv
        srv._switching = spy = SpyEvent()
        r.client.takeoffAsync(vehicle_name="Drone1").join()
        assert "Drone1" not in srv._last_tele
        out = do_switch(srv, proposal(srv))
        assert any(x.startswith("Drone1: airborne") for x in out["reasons"]), out
        assert spy.raised == 1 and not spy.is_set()
