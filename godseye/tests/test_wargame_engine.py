"""The simulated wargame engine against the fake AirSim (PLAN §4.5a M14a;
WG v2 §5.2.4-§5.2.9, key unit tests of B3).

Every session here is a SIMULATION between scenario units the engine placed;
effects are notional table draws. What is pinned:

* every `start` refusal, and the `starting` race with a theater switch;
* the spawn rules through the engine, designators, session-unique names and
  exact classification, `no_room` leaving nothing behind;
* a seeded `step()` event sequence, a drone lost in an envelope (queue aborted,
  `vehicle_lost`, no forced RTB), red ground fire, the 20 ms step budget;
* the provenance gate (mapped-site, `sim_spawn_target`, pre-spawn and
  far-from-truth tracks refused and ignored by threats, axes and `step()`);
* the real-site gate on corridors and after `authorize`;
* propose -> authorize -> execute: only the console session executes, once;
* BDA tiers and fog; `end` cleanup and the AAR (never `latest`); same seed,
  same outcomes; no real-system token in any output.

The engine's thread is off (`run_thread = False`); tests drive `step()`. No
network (geodata off); B3's ports 53300-53349.
"""
from __future__ import annotations

import asyncio
import gc
import itertools
import math
import os
import statistics
import threading
import time
from contextlib import contextmanager

import airsim
import pytest
from godseye_uav import intel_graph, sites, theater_switch, theaters
from godseye_uav import wargame as wg
from godseye_uav import wargame_spawn as sp
from godseye_uav import wargame_tables as T
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint, canonical_altitude
from godseye_uav.safety import haversine_m
from godseye_uav.server import GodseyeUavServer, UavBackend
from godseye_uav.store import Store
from godseye_uav.targets import Track, match_ob
from support.wg_tokens import assert_no_real_system_tokens

_PORTS = list(range(53300, 53350))
_PORT = itertools.cycle(_PORTS[os.getpid() % len(_PORTS):]
                        + _PORTS[:os.getpid() % len(_PORTS)])
THEATER = theaters.get("default")
HOME = GeoPoint(THEATER.home_lat, THEATER.home_lon,
                canonical_altitude(THEATER.home_alt_msl_m, THEATER.home_lat,
                                   THEATER.home_lon, datum="msl").alt_hae)
SEED = 4417


def ne(dn_m: float, de_m: float) -> tuple[float, float]:
    """A point `dn_m` north and `de_m` east of home (flat earth, < 3 km)."""
    return (HOME.latitude + dn_m / 111_320.0,
            HOME.longitude + de_m / (111_320.0 * math.cos(math.radians(HOME.latitude))))


P_RED = ne(1600, 1000)          # >= 1 km from home, > 500 m from every theater point
P_BLUE = ne(-900, -900)


@contextmanager
def wg_server(tmp_path, *, store_path=None, **kw):
    """A default-theater server on a fake sim, engine thread off."""
    sim = srv = None
    for _ in range(len(_PORTS)):
        sim = FakeAirSim(home=HOME, port=next(_PORT))
        try:
            sim.start()
            break
        except OSError:
            sim.stop()
            sim = None
    assert sim is not None, "no free port in 53300-53349"
    store = Store(store_path or tmp_path)
    try:
        client = airsim.MultirotorClient(port=sim.port)
        client.confirmConnection()
        srv = GodseyeUavServer(UavBackend(client, HOME, sim=sim), store,
                               theater=kw.pop("theater", THEATER), **kw)
        srv.wargame.run_thread = False
        yield srv
    finally:
        if srv is not None:
            srv.wargame.close()
            srv.stop_monitor()
            srv.tasking.shutdown()
        store.close()
        sim.stop()


def run(coro):
    return asyncio.run(coro)


def tool(srv, name):
    return srv.mcp._tool_manager._tools[name].fn


def audit(srv, kind: str) -> list[dict]:
    srv.wargame.flush()
    return [r for r in srv.store.audit.read_all() if r.get("kind") == kind]


def start(srv, **kw) -> dict:
    out = run(srv.wargame.start(**{"seed": SEED, **kw}))
    assert "session_id" in out, out
    return out


def spawn(srv, side: str, wg_class: str, at: tuple[float, float], **kw) -> dict:
    out = run(srv.wargame.spawn(side, wg_class, *at, **kw))
    assert "units" in out, out
    return out["units"][0]


def session(srv):
    return srv.wargame._session


def unit(srv, uid: str):
    return session(srv).units[uid]


def sense(srv, name: str, *, times: int = 3, at: tuple[float, float] | None = None,
          vehicle: str = "Drone1"):
    """Fold `times` detections of object `name` into a track, as `_ingest_frame`
    does: the scenario hook first, the pattern of life for everything else."""
    if at is None:
        lat, lon, alt = srv.backend.sim.object_geo(name)
    else:
        (lat, lon), alt = at, HOME.altitude
    track = None
    for _ in range(times):
        det = {"name": name, "geo_point": {"latitude": lat, "longitude": lon, "altitude": alt},
               "slant_range_m": 400.0, "pixels_on_target": 60}
        updated = srv.tracks.ingest(
            [det], sensor={"sensor": "scene", "fov_deg": 60},
            observer={"lat": lat, "lon": lon + 0.003, "alt_m": alt + 150.0, "vehicle": vehicle})
        for t in updated:
            if not srv._note_scenario_track(t):
                srv.pol.observe_track(t)
            track = t
        time.sleep(0.002)
    return track


def over(u, *, up: float = 300.0, east_m: float = 300.0, landed: int = 1,
         speed: float = 8.0) -> dict:
    """A telemetry row for a drone `east_m` east of unit `u`, `up` m above it."""
    lon = u.lon + east_m / (111_320.0 * math.cos(math.radians(u.lat)))
    return {"lat": u.lat, "lon": lon, "alt_hae_m": u.alt_hae_m + up,
            "landed_state": landed, "speed_mps": speed}


def red_blue(srv, red_class: str = "ad_gun"):
    """Start a session with one red unit at P_RED and one blue artillery at P_BLUE."""
    start(srv, red_engages=False)
    red = spawn(srv, "red", red_class, P_RED)
    blue = spawn(srv, "blue", "blue_artillery", P_BLUE)
    return red, blue


def proposed(srv, red_class: str = "ad_gun"):
    """A session with a sensed red unit and a proposed strike on it."""
    red, blue = red_blue(srv, red_class)
    name = unit(srv, red["unit_id"]).object_name
    track = sense(srv, name)
    out = run(srv.wargame.propose_strike(blue["unit_id"], track.track_id))
    assert "pending_id" in out, out
    return red, blue, track, out


def excl(*rects, complete=True, protected=()):
    return sites.Exclusion(rects=tuple(tuple(r) for r in rects),
                           protected=tuple(tuple(r) for r in protected), complete=complete,
                           reason=None if complete else "Overpass: timeout",
                           fetched_at_ms=0, element_count=len(rects))


def rect_east_of(point, gap_m: float, size_m: float = 100.0):
    """A footprint whose west edge is `gap_m` east of `point`."""
    k = 111_320.0 * math.cos(math.radians(point[0]))
    return (point[0] - size_m / 2 / 111_320.0, point[1] + gap_m / k,
            point[0] + size_m / 2 / 111_320.0, point[1] + (gap_m + size_m) / k)


# ------------------------------------------------------------ ISR mode -----

def test_isr_mode_has_a_quiet_engine_and_no_thread(tmp_path):
    with wg_server(tmp_path) as srv:
        eng = srv.wargame
        assert isinstance(eng, wg.WargameEngine)
        assert eng.active is False and eng.starting is False and eng.last is None
        assert eng.mode_key() == "isr" and eng.owns_name("aaa_towed_1") is False
        assert srv.wargame_recovery is None
        assert eng.graph_rows(truth=True) == {
            "nodes": [], "edges": [], "meta": {"wargame": {"active": False, "last": None}}}
        assert eng.overlay_features(truth=True) == []
        assert eng.status()["active"] is False
        assert eng.unit_near(HOME.latitude, HOME.longitude, 5000.0) is None
        assert not any(t.name.startswith("godseye-wargame") for t in threading.enumerate())
        for coro in (eng.spawn("red", "ad_gun", *P_RED), eng.generate("air_defence_belt"),
                     eng.propose_strike("blue-artillery-1", "TRK-x"),
                     eng.execute("WG-x-E1", "blue-artillery-1", "TRK-x"),
                     eng.plan_corridor("Drone1", "TRK-x"), eng.end(reason="operator")):
            out = run(coro)
            assert out["error"] == "wargame_inactive" and out["rejected"] is True
            assert out["message"] == wg.INACTIVE_MESSAGE
        assert not (tmp_path / "wargame.json").exists()
        assert audit(srv, "wargame_session_started") == []


# ------------------------------------------------------ start refusals -----

def test_every_start_refusal(tmp_path, monkeypatch):
    with wg_server(tmp_path) as srv:
        eng = srv.wargame

        def refused(code, **kw):
            out = run(eng.start(**kw))
            assert out["error"] == code, out
            assert eng.active is False and eng.starting is False
            return out

        sim = srv.backend.sim
        srv.backend.sim = None
        assert refused("wargame_requires_fake_sim")["message"] == \
            "The wargame needs the fake simulator."
        srv.backend.sim = sim
        srv._switching.set()
        assert refused("theater_switching")["message"] == "A theater switch is running."
        srv._switching.clear()
        srv.theater_integrity_error = "origin moved 12 m"
        assert refused("theater_integrity")["message"] == "origin moved 12 m"
        srv.theater_integrity_error = None
        srv.theater = theaters.get("iran-natanz")
        assert refused("theater_not_cleared")["message"] == \
            "This theater isn't cleared for the wargame."
        # A dynamic theater needs a COMPLETE exclusion set.
        dyn = theaters.make_dynamic(label="Test field", place="Test", center=ne(0, 0),
                                    half_extent_m=2940.0, home=ne(0, 0),
                                    home_alt_msl_m=THEATER.home_alt_msl_m, provenance=None)
        srv.theater = dyn
        monkeypatch.setattr(sites, "fetch_exclusion", lambda *a, **k: excl(complete=False))
        assert refused("exclusion_incomplete")["message"] == (
            "Mapped places here couldn't all be loaded, so the wargame can't keep clear of "
            "them; try again when map data is available.")
        # The same degraded set is fine on `default` (stock synthetic origin).
        srv.theater = THEATER
        out = start(srv)
        again = run(eng.start())
        assert again["error"] == "wargame_active"
        assert again["message"] == "A wargame session is already running."
        assert eng.active is True and eng.session_id == out["session_id"]


def test_start_and_a_theater_switch_interleaved_have_exactly_one_winner(tmp_path, monkeypatch):
    class SlowLock:
        """`_mode_lock`, held a little longer, so the two callers really collide."""

        def __init__(self):
            self._lock = threading.Lock()

        def __enter__(self):
            self._lock.acquire()
            time.sleep(0.03)
            return self

        def __exit__(self, *exc):
            self._lock.release()

    monkeypatch.setattr(theater_switch, "cached_refusals", lambda srv: [])
    with wg_server(tmp_path) as srv:
        srv._mode_lock = SlowLock()
        srv.boot_recovery_done.set()
        start_done = threading.Event()

        async def hold(timeout_s):
            # The switch holds `_switching` until the start attempt has answered.
            await asyncio.to_thread(start_done.wait, 5.0)
            return False
        srv.wait_ticks_idle = hold
        proposal = type("P", (), {"theater": THEATER})()
        winners = set()
        for first in ("start", "switch", "start", "switch"):
            start_done.clear()
            results: dict = {}

            def do_start(results=results):
                try:
                    results["start"] = run(srv.wargame.start(seed=SEED))
                finally:
                    start_done.set()

            def do_switch(results=results):
                results["switch"] = run(theater_switch.switch(srv, proposal, via="mcp"))

            order = [do_start, do_switch] if first == "start" else [do_switch, do_start]
            threads = [threading.Thread(target=f) for f in order]
            for th in threads:
                th.start()
                time.sleep(0.005)
            for th in threads:
                th.join(15.0)
            started = "session_id" in results["start"]
            switched = results["switch"]["reasons"] != [theater_switch.MSG_WARGAME]
            assert started != switched, results            # exactly one winner
            if started:
                winners.add("start")
                assert results["switch"]["message"] == theater_switch.MSG_WARGAME
                assert run(srv.wargame.end(reason="test"))["ended"] is True
            else:
                winners.add("switch")
                assert results["start"]["error"] == "theater_switching"
                assert srv.wargame.active is False
            assert srv.wargame.starting is False and not srv._switching.is_set()
        assert winners == {"start", "switch"}


def test_a_session_starts_persists_and_says_what_fits(tmp_path):
    with wg_server(tmp_path) as srv:
        out = start(srv, red_engages=False, reveal_red=True)
        assert out["session_id"].startswith("WG-") and len(out["session_id"]) == 9
        assert out["seed"] == SEED and out["engine"] == out["table_version"] == "wg-notional/1"
        assert out["theater_id"] == "default" and out["red_engages"] is False
        assert out["reveal_red"] is True and out["simulated"] is True
        assert out["note"] == T.NOTIONAL_NOTE
        assert out["ao"] == T.ao_geometry_bbox(THEATER.bbox())
        assert out["classes_that_fit"] == ["ad_gun"]         # 3.2 km half-diagonal
        # geodata off: the default theater runs, and says what it could not load.
        assert any("Mapped places weren't loaded" in c for c in out["caveats"])
        assert wg.TERRAIN_OFF_CAVEAT in out["caveats"]
        assert srv.wargame.mode_key() == f"wargame:{out['session_id']}"
        import json
        doc = json.loads((tmp_path / "wargame.json").read_text())
        assert doc == {"schema": "godseye.wargame-session/v1", "session_id": out["session_id"],
                       "num": session(srv).num, "started_at_ms": out["started_at_ms"],
                       "seed": SEED, "theater_id": "default", "object_names": [],
                       "track_ids": []}
        [row] = audit(srv, "wargame_session_started")
        assert row["session_id"] == out["session_id"] and row["seed"] == SEED
        assert run(srv.wargame.start())["error"] == "wargame_active"
        # A random seed when none is given; a non-number is refused.
        assert run(srv.wargame.end(reason="test"))["ended"] is True
        assert run(srv.wargame.start(seed="abc"))["error"] == "invalid_parameter"
        assert isinstance(start(srv, seed=None)["seed"], int)
        assert session(srv).num != doc["num"]


# --------------------------------------------------------------- spawn -----

def test_designators_session_unique_names_and_exact_classification(tmp_path):
    with wg_server(tmp_path) as srv:
        start(srv)
        num = session(srv).num
        a = spawn(srv, "red", "ad_gun", P_RED)
        b = spawn(srv, "red", "ad_gun", ne(1600, -1000))
        blue = spawn(srv, "blue", "blue_artillery", P_BLUE)
        assert (a["unit_id"], a["designator"]) == ("red-aaa-1", "Red AD guns 1")
        assert (b["unit_id"], b["designator"]) == ("red-aaa-2", "Red AD guns 2")
        assert (blue["unit_id"], blue["designator"]) == ("blue-artillery-1", "Blue artillery 1")
        names = [unit(srv, u).object_name for u in ("red-aaa-1", "red-aaa-2")]
        assert names == [f"aaa_towed_{num}001", f"aaa_towed_{num}002"]
        assert unit(srv, "blue-artillery-1").object_name is None      # registry only
        for uid, name in zip(("red-aaa-1", "red-aaa-2"), names, strict=True):
            rec = srv.targets[name]
            assert rec["ob_class"] == "aaa_towed" and match_ob(name)[0].key == "aaa_towed"
            assert rec["provenance"] == "scenario" and rec["session_id"] == session(srv).session_id
            assert (rec["side"], rec["unit_id"]) == ("red", uid)
            assert srv.backend.sim.object_geo(name) is not None
            assert srv.wargame.owns_name(name) is True
        track = sense(srv, names[0])
        assert track.ob_class == "aaa_towed" and track.scenario is True
        assert session(srv).track_ids == [track.track_id]
        assert a["kind_label"] == "Air-defence guns" and a["register"] == "scenario"
        assert a["provenance"] == "scenario" and a["simulated"] is True
        assert srv.wargame.unit_near(*P_RED, 50.0) == "red-aaa-1"
        import json
        doc = json.loads((tmp_path / "wargame.json").read_text())
        assert doc["object_names"] == names and doc["track_ids"] == [track.track_id]


def test_the_covers_caveat_rides_on_a_unit_whose_envelope_covers_the_ao(tmp_path):
    with wg_server(tmp_path) as srv:
        start(srv)
        out = run(srv.wargame.spawn("red", "ad_short", *P_RED))
        assert out["units"][0]["caveats"][0] == T.COVERS_CAVEAT
        assert T.COVERS_CAVEAT in out["caveats"]
        gun = spawn(srv, "red", "ad_gun", ne(1600, -1000))
        assert T.COVERS_CAVEAT not in gun["caveats"]
        assert T.COVERS_CAVEAT in srv.wargame.status(truth=True)["caveats"]


def test_spawn_rules_through_the_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(sites, "fetch_exclusion",
                        lambda *a, **k: excl(rect_east_of(P_RED, 400.0),
                                             rect_east_of(ne(-1500, 1500), 600.0)))
    with wg_server(tmp_path) as srv:
        start(srv)
        eng = srv.wargame

        def code(side, cls, at, **kw):
            return run(eng.spawn(side, cls, *at, **kw)).get("error")

        assert code("red", "ad_gun", P_RED) == "near_real_site"            # edge at 400 m
        assert code("red", "ad_gun", ne(-1500, 1500)) is None               # edge at 600 m
        poi = THEATER.pois[0]
        assert code("blue", "blue_mech", (poi.lat + 0.003, poi.lon)) == "near_theater_point"
        assert code("red", "ad_gun", ne(900, -1200)) is None
        assert code("red", "ad_gun", ne(0, -900)) == "too_close_home"
        assert code("blue", "blue_mech", ne(-100, -900)) is None            # blue may
        assert code("red", "ad_gun", ne(1050, -1200)) == "too_close_unit"
        truck = run(tool(srv, "sim_spawn_target")(lat=ne(-1500, -1500)[0],
                                                  lon=ne(-1500, -1500)[1],
                                                  ob_class="supply_truck", name="truck_a"))
        assert truck["status"] == "accepted", truck
        assert code("blue", "blue_mech", ne(-1400, -1500)) == "near_existing_object"
        assert code("red", "ad_gun", ne(5000, 0)) == "outside_ao"
        assert code("red", "not_a_class", P_RED) == "unknown_class"
        assert code("blue", "ad_gun", P_RED) == "side_mismatch"
        assert code("red", "ad_gun", ne(1600, -300), count=7) == "invalid_parameter"
        assert code("red", "armour_company", ne(1800, -1500),
                    objective_id="frc:red-aaa-1") == "unknown_objective"
        monkeypatch.setattr(sp, "MAX_UNITS", len(session(srv).units) + 1)
        assert code("red", "ad_gun", ne(-1800, 1800), count=2) == "max_units"
        # Messages never name a place.
        for c in ("near_real_site", "near_theater_point", "too_close_home"):
            assert sp.MESSAGES[c] in {m["message"] for m in (sp.refusal(c),)}
        assert len(session(srv).units) == 3


def test_no_room_refuses_the_whole_scenario_and_spawns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sites, "fetch_exclusion",
                        lambda *a, **k: excl(tuple(THEATER.bbox())))
    with wg_server(tmp_path) as srv:
        start(srv)
        before = dict(srv.targets)
        out = run(srv.wargame.generate("air_defence_belt", "high"))
        assert out == sp.refusal("no_room")
        assert session(srv).units == {} and srv.targets == before
        assert session(srv).object_names == [] and srv.backend.sim.objects() == {}
        assert run(srv.wargame.spawn("blue", "blue_mech", *P_BLUE))["error"] == "near_real_site"


def test_a_template_places_all_units_with_objectives_routes_and_fog(tmp_path):
    with wg_server(tmp_path) as srv:
        start(srv)
        out = run(srv.wargame.generate("mech_advance", "medium"))
        s = session(srv)
        assert out["blue_placed"] == 3 and out["red_placed"] == 4
        # Blue view: the answer lists blue units only and names no red class.
        assert {u["side"] for u in out["units"]} == {"blue"} and out["ad_class"] is None
        point = next(u for u in s.units.values() if u.slot == "point")
        assert point.wg_class == "blue_defended_point"
        movers = [u for u in s.units.values() if u.side == "red" and u.slot in
                  ("armour", "infantry")]
        assert len(movers) == 3 and all(u.objective == point.unit_id for u in movers)
        for u in movers:
            stand = 0.8 * max(T.CLASSES[u.wg_class].ground_range_m, 500.0)
            if haversine_m(u.lat, u.lon, point.lat, point.lon) > stand:
                assert srv.targets[u.object_name]["route"] is not None
                assert haversine_m(*u.route_to, point.lat, point.lon) == \
                    pytest.approx(stand, abs=2.0)
        # Every unit obeys the spawn rules; red keeps 1 km from home.
        for u in s.units.values():
            if u.side == "red":
                assert haversine_m(u.lat, u.lon, HOME.latitude, HOME.longitude) >= 1000.0
            assert all(haversine_m(u.lat, u.lon, p.lat, p.lon) >= 500.0 for p in THEATER.pois)
        srv.wargame.step(10.0, {})                       # axes are recomputed per pass
        truth = srv.wargame.graph_rows(truth=True)
        axes = {(e["a"], e["b"]) for e in truth["edges"] if e["kind"] == "axis"}
        assert axes == {(f"frc:{u.unit_id}", f"frc:{point.unit_id}") for u in movers}
        blue_view = srv.wargame.graph_rows(truth=False)
        assert not any(n["type"] == "vector" and n["attrs"]["kind"] == "axis"
                       for n in blue_view["nodes"])
        assert not any(e["kind"] == "axis" for e in blue_view["edges"])
        run(srv.wargame.end(reason="test"))
        revealed = start(srv, reveal_red=True)
        assert revealed["reveal_red"] is True
        out2 = run(srv.wargame.generate("air_defence_belt", "low"))
        assert {u["side"] for u in out2["units"]} == {"red", "blue"}
        assert out2["ad_class"] == "ad_gun"          # the only class that fits 3.2 km
        assert T.COVERS_CAVEAT not in out2["caveats"]


# ---------------------------------------------------------------- step -----

CLEAR = {"light_factor": 1.0, "weather": {"visibility_factor": 1.0}}


def red_shots(srv) -> list[tuple]:
    return [(e.attacker, e.target, e.outcome, e.draw, e.p_notional["effect"])
            for e in session(srv).engagements if e.kind == "red_shot"]


def scripted_air_defence(srv, monkeypatch, passes: int = 12, seed: int = 7) -> list[tuple]:
    """A seeded red-air-defence run: a SAM, AD guns and a radar against two drones."""
    monkeypatch.setattr(srv.wargame, "_environment", lambda: CLEAR)
    start(srv, red_engages=True, seed=seed)
    sam = unit(srv, spawn(srv, "red", "ad_short", P_RED)["unit_id"])
    gun = unit(srv, spawn(srv, "red", "ad_gun", ne(1700, -1200))["unit_id"])
    spawn(srv, "red", "radar_early_warning", ne(-1600, 1500))
    tele = {"Drone2": over(gun, up=250.0, east_m=1700.0),
            "Drone1": over(sam, up=600.0, east_m=6000.0),
            "Drone3": over(sam, landed=0)}                    # on the ground: ignored
    for i in range(passes):
        srv.wargame.step(100.0 + 6.0 * i, tele)
    return red_shots(srv)


#: The exact event sequence of `scripted_air_defence` with seed 7 (pinned).
EXPECTED_SHOTS = [
    ("frc:red-aaa-1", "veh:Drone2", "missed", 4, 0.17),
    ("frc:red-sam-1", "veh:Drone1", "missed", 6, 0.33),
    ("frc:red-aaa-1", "veh:Drone2", "destroyed", 16, 0.17),
    ("frc:red-sam-1", "veh:Drone1", "missed", 18, 0.33),
    ("frc:red-sam-1", "veh:Drone1", "missed", 23, 0.33),
    ("frc:red-sam-1", "veh:Drone1", "destroyed", 28, 0.33),
]


def test_a_seeded_step_sequence_is_exact(tmp_path, monkeypatch):
    with wg_server(tmp_path) as srv:
        assert scripted_air_defence(srv, monkeypatch) == EXPECTED_SHOTS
        s = session(srv)
        assert s.rng_red.draw == 28 and s.rng_blue.draw == 0      # blue never drawn
        assert set(s.lost) == {"Drone1", "Drone2"}                 # Drone3 never engaged
        assert s.lost["Drone1"]["by"] == "Red SAM 1"
        # Cycles: guns every 5 s, the SAM every 12 s; one shot per unit per pass.
        sam = unit(srv, "red-sam-1")
        assert sam.ammo == 8 - 4 and sam.next_shot_s > 100.0
        kinds = [e["kind"] for e in srv.wargame.status(truth=True, events=100)["events"]]
        assert kinds.count("red_shot") == 6
        assert all(e.phase == "adjudicated" and e.stream == "red" for e in s.engagements)
        consequences = [e.consequence for e in s.engagements]
        assert consequences == ["none", "none", "own_loss", "none", "none", "own_loss"]
        run(srv.wargame._await_losses())
        assert set(srv.vehicles_lost) == {"Drone1", "Drone2"}
        # Same seed, fresh server: the same sequence.
    with wg_server(tmp_path / "again") as srv:
        assert scripted_air_defence(srv, monkeypatch) == EXPECTED_SHOTS


def test_a_drone_in_a_short_range_envelope_is_lost_and_flies_nothing(tmp_path, monkeypatch):
    with wg_server(tmp_path) as srv:
        monkeypatch.setattr(srv.wargame, "_environment", lambda: CLEAR)

        async def main():
            await srv.wargame.start(seed=SEED, red_engages=True)
            out = await srv.wargame.spawn("red", "ad_short", *P_RED)
            sam = srv.wargame._session.units[out["units"][0]["unit_id"]]
            route = [list(ne(200, 0)), list(ne(200, 300)), list(ne(0, 300))]
            h = await tool(srv, "mission_recon_route")(
                vehicle="Drone1", waypoints=route, alt_agl_m=40.0, speed_mps=8.0)
            assert h.get("status") == "accepted", h
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                t = await srv._telemetry("Drone1")
                if int(t["landed_state"]) != 0:
                    break
                await asyncio.sleep(0.05)
            # The umpire's picture: Drone1 inside the SAM envelope.
            tele = {"Drone1": over(sam, up=300.0, east_m=800.0)}
            for i in range(20):
                srv.wargame.step(1000.0 + 13.0 * i, tele)
                if "Drone1" in srv.wargame._session.lost:
                    break
            assert srv.wargame._session.lost["Drone1"]["by"] == "Red SAM 1"
            await srv.wargame._await_losses()
            assert srv.vehicles_lost["Drone1"]["by"] == "Red SAM 1"
            task = srv.tasking.get("Drone1", h["task_id"])
            assert task.state.value == "cancelled"                  # the queue was aborted
            refused = srv._submit("Drone1", "uav_hover", {}, None)
            assert refused["error"] == "vehicle_lost"
            assert await srv._force_rtb("Drone1", reason="bingo") is None
            assert (await tool(srv, "uav_takeoff")(vehicle="Drone1"))["error"] == "vehicle_lost"
            # A downed drone is never engaged again.
            before = len(red_shots(srv))
            srv.wargame.step(2000.0, tele)
            assert len(red_shots(srv)) == before
            # Blue view: the loss is shown, the shooter is not.
            blue = srv.wargame.status(truth=False, events=100)
            shot = [e for e in blue["events"] if e["kind"] == "red_shot"][-1]
            assert shot["text"].startswith(wg.RED_AD_HIDDEN) and shot["outcome"] == "destroyed"
            assert blue["vehicles_lost"][0]["by"] == wg.RED_AD_HIDDEN
            ended = await srv.wargame.end(reason="test")
            assert ended["revived"] == ["Drone1"] and srv.vehicles_lost == {}
        run(main())


def test_red_ground_fire_hits_the_nearest_seen_blue_unit(tmp_path, monkeypatch):
    with wg_server(tmp_path) as srv:
        start(srv, red_engages=True, seed=7)
        armour = spawn(srv, "red", "armour_company", ne(1500, -1500))
        near = spawn(srv, "blue", "blue_mech", ne(-100, -1500))       # 1.6 km: in range
        spawn(srv, "blue", "blue_strike_air", ne(-300, -1800))       # never a target
        spawn(srv, "blue", "blue_artillery", ne(-1800, 1800))        # far and unseen
        shots = []
        for i in range(10):
            srv.wargame.step(500.0 + 61.0 * i, {})
            shots = [e for e in session(srv).engagements if e.kind == "red_ground"]
        assert shots and {e.target for e in shots} == {f"frc:{near['unit_id']}"}
        assert all(e.attacker == f"frc:{armour['unit_id']}" for e in shots)
        for e in shots:
            want = ("own_loss" if e.outcome == "destroyed" else "own_damage"
                    if e.outcome in ("damaged", "suppressed") else "none")
            assert e.consequence == want and e.p_notional["effect"] > 0
        b = unit(srv, near["unit_id"])
        if any(e.outcome == "destroyed" for e in shots):
            assert b.state == "destroyed"
        assert unit(srv, armour["unit_id"]).ammo == 20 - len(shots)
        blue = srv.wargame.status(truth=False, events=100)
        texts = [e["text"] for e in blue["events"] if e["kind"] == "red_ground"]
        assert texts and all(t.startswith(wg.RED_GROUND_HIDDEN) for t in texts)
        rows = [e for e in blue["engagements"] if e["kind"] == "red_ground"]
        assert all(r["attacker"] is None and r["inputs"] == [] for r in rows)
        # red_engages=false: nobody fires.
        run(srv.wargame.end(reason="test"))
        start(srv, red_engages=False)
        spawn(srv, "red", "armour_company", ne(1500, -1500))
        spawn(srv, "blue", "blue_mech", ne(-100, -1500))
        for i in range(5):
            srv.wargame.step(500.0 + 61.0 * i, {})
        assert session(srv).engagements == []


def test_step_stays_within_20_ms_at_60_units_and_4_drones(tmp_path):
    with wg_server(tmp_path) as srv:
        start(srv, red_engages=True)
        eng = srv.wargame
        grid = [ne(dn, de) for dn in range(-2000, 2001, 250) for de in range(-2000, 2001, 250)]
        classes = itertools.cycle(["ad_gun", "ad_short", "radar_early_warning", "armour_company",
                                   "mech_infantry", "command_post"])
        for p in grid:
            if len(session(srv).units) >= 54:
                break
            run(eng.spawn("red", next(classes), *p))
        for p in reversed(grid):
            if len(session(srv).units) >= 60:
                break
            run(eng.spawn("blue", "blue_mech", *p))
        assert len(session(srv).units) == 60
        eng.flush()
        reds = [u for u in session(srv).units.values() if u.side == "red"]
        tele = {f"Drone{i + 1}": over(reds[i * 7], up=200.0, east_m=500.0) for i in range(4)}
        assert srv.tasking.loop.is_running()      # as in the app: the monitor runs on it
        with eng._lock:
            for u in reds:                    # every unit needs a new horizon this pass
                u.horizon_at = None
        times, own = [], []
        gc.collect()
        gc.disable()                   # a full collection in this big test process is
        try:                           # not the engine's time (it pauses every thread)
            for i in range(12):
                t0 = time.perf_counter()
                eng.step(100.0 + 0.25 * i, tele)
                times.append((time.perf_counter() - t0) * 1000.0)
                own.append(eng.step_ms)
        finally:
            gc.enable()
        assert red_shots(srv)                     # the drones really were engaged
        # The engine's own measure of each pass, horizon submission included.
        assert own[0] <= wg.STEP_BUDGET_MS and max(own) <= wg.STEP_BUDGET_MS, own
        assert statistics.median(times) <= wg.STEP_BUDGET_MS, times
        assert audit(srv, "wargame_step_slow") == []
        eng.flush()
        assert all(u.horizon is not None for u in reds
                   if T.CLASSES[u.wg_class].role in ("air", "sensor"))


# ------------------------------------------------------ provenance gate -----

class _Site:
    """A mapped site as `sim_spawn_order_of_battle` spawns it (fake real data)."""

    def __init__(self, osm_id, name, lat, lon, ob_class="sam_short_range"):
        self.osm_id, self.name, self.lat, self.lon = osm_id, name, lat, lon
        self.category, self.ob_class = "military_base", ob_class
        self.ob_source, self.position_source = "test", "test"

    def spawn_request(self):
        return {"ob_class": self.ob_class, "lat": self.lat, "lon": self.lon, "name": self.name}

    def alt_provenance(self):
        return {"alt_msl_m": None, "alt_source": "test", "alt_is_real": False}


class _Order:
    real = False

    def __init__(self, sites_):
        self.sites = sites_
        self.provenance = type("P", (), {"as_dict": staticmethod(lambda: {"source": "test"})})()

    def by_category(self):
        return {"military_base": len(self.sites)}


def refused_as_not_scenario(srv, blue_id: str, track_id: str) -> None:
    for out in (run(srv.wargame.propose_strike(blue_id, track_id)),
                run(srv.wargame.plan_corridor("Drone1", track_id))):
        assert out == {"rejected": True, "error": "not_a_scenario_unit",
                       "message": wg.NOT_SCENARIO_MESSAGE, "simulated": True}
    with pytest.raises(wg.WargameRefused) as exc:
        srv.wargame.gate_track(track_id)
    assert exc.value.code == "not_a_scenario_unit"


def test_real_data_and_other_sim_objects_never_pass_the_gate(tmp_path, monkeypatch):
    with wg_server(tmp_path) as srv:
        monkeypatch.setattr(srv.wargame, "_environment", lambda: CLEAR)
        start(srv, red_engages=True)
        blue = spawn(srv, "blue", "blue_rocket", P_BLUE)
        # A mapped site spawned from (fake) real data as a short-range SAM.
        osm_at = ne(1500, -1500)
        srv.real_order_of_battle = lambda: _Order([_Site(9, "sam_short_range_9001", *osm_at)])
        ob = run(tool(srv, "sim_spawn_order_of_battle")())
        assert ob["spawned"] == 1, ob
        osm = sense(srv, "sam_short_range_9001")
        assert osm.ob_class == "sam_short_range" and osm.scenario is False
        refused_as_not_scenario(srv, blue["unit_id"], osm.track_id)
        # A `sim_spawn_target` object.
        tgt_at = ne(-1500, 1500)
        out = run(tool(srv, "sim_spawn_target")(lat=tgt_at[0], lon=tgt_at[1],
                                                 ob_class="sam_short_range",
                                                 name="sam_short_range_1"))
        assert out["status"] == "accepted", out
        other = sense(srv, "sam_short_range_1")
        refused_as_not_scenario(srv, blue["unit_id"], other.track_id)
        # Neither is a threat, an axis end or a shooter: nothing on the engine side.
        with srv.wargame._lock:
            assert srv.wargame._sensed_threats(session(srv)) == []
            assert srv.wargame._truth_threats(session(srv)) == []
        assert srv.wargame.graph_rows(truth=True)["nodes"][0]["id"] == f"frc:{blue['unit_id']}"
        tele = {"Drone1": {"lat": osm_at[0], "lon": osm_at[1] + 0.004,
                           "alt_hae_m": HOME.altitude + 300.0, "landed_state": 1,
                           "speed_mps": 8.0}}
        for i in range(10):
            srv.wargame.step(100.0 + 13.0 * i, tele)
        assert session(srv).engagements == [] and session(srv).lost == {}
        assert all(v["kind"] != "axis" for v in session(srv).vectors.values())
        # A real scenario unit's track does pass, and is a sensed threat.
        red = spawn(srv, "red", "ad_gun", P_RED)
        good = sense(srv, unit(srv, red["unit_id"]).object_name)
        u, t = srv.wargame.gate_track(f"trk:{good.track_id}")
        assert (u.unit_id, t.track_id) == (red["unit_id"], good.track_id)
        with srv.wargame._lock:
            [threat] = srv.wargame._sensed_threats(session(srv))
        assert threat[3].key == "ad_gun" and threat[4] is False


def test_the_provenance_gate_answers_before_the_confidence_check(tmp_path):
    """A `sim_spawn_target` object seen once ("possible") is refused as not a
    scenario unit, not as low confidence (B17, E2E B2): D1's answer comes first."""
    with wg_server(tmp_path) as srv:
        start(srv, red_engages=False)
        blue = spawn(srv, "blue", "blue_rocket", P_BLUE)
        tgt_at = ne(-1500, 1500)
        out = run(tool(srv, "sim_spawn_target")(lat=tgt_at[0], lon=tgt_at[1],
                                                 ob_class="sam_short_range",
                                                 name="sam_short_range_1"))
        assert out["status"] == "accepted", out
        once = sense(srv, "sam_short_range_1", times=1)
        assert run(srv.wargame.propose_strike(blue["unit_id"], once.track_id))["error"] == \
            "not_a_scenario_unit"


def test_a_track_seen_before_the_spawn_or_far_from_truth_is_refused(tmp_path):
    with wg_server(tmp_path) as srv:
        red, blue = red_blue(srv)
        u = unit(srv, red["unit_id"])
        # A persisted track carrying the unit's name, first seen before it existed.
        early = Track(track_id="TRK-early-0001", name=u.object_name, category="aaa",
                      lat=u.lat, lon=u.lon, alt_m=u.alt_hae_m, first_seen=u.spawned_at - 10.0,
                      last_seen=time.time(), sightings=5, ob_class="aaa_towed")
        srv.tracks.add(early)
        session(srv).track_ids.append(early.track_id)
        refused_as_not_scenario(srv, blue["unit_id"], early.track_id)
        # A track 400 m from the unit's truth.
        far_lat = u.lat + 400.0 / 111_320.0
        far = Track(track_id="TRK-far-0001", name=u.object_name, category="aaa",
                    lat=far_lat, lon=u.lon, alt_m=u.alt_hae_m, first_seen=time.time(),
                    last_seen=time.time(), sightings=5, ob_class="aaa_towed")
        srv.tracks.add(far)
        refused_as_not_scenario(srv, blue["unit_id"], far.track_id)
        # Unknown ids and a blue designator are no tracks at all.
        refused_as_not_scenario(srv, blue["unit_id"], "TRK-nope-0001")
        refused_as_not_scenario(srv, blue["unit_id"], blue["unit_id"])


def test_a_corridor_to_a_track_near_a_mapped_place_is_refused(tmp_path):
    with wg_server(tmp_path) as srv:
        red, _ = red_blue(srv)
        u = unit(srv, red["unit_id"])
        track = sense(srv, u.object_name)
        ok = run(srv.wargame.plan_corridor("Drone1", track.track_id))
        assert "waypoints" in ok, ok
        session(srv).exclusion = excl(rect_east_of((u.lat, u.lon), 400.0))
        out = run(srv.wargame.plan_corridor("Drone1", track.track_id))
        assert out == sp.refusal("target_near_real_site")
        session(srv).exclusion = excl(protected=(rect_east_of((u.lat, u.lon), 900.0),))
        assert run(srv.wargame.plan_corridor("Drone1", track.track_id))["error"] == \
            "target_protected"


# ------------------------------------------------------------- strikes -----

def test_propose_answers_with_generic_labels_and_execute_args(tmp_path):
    with wg_server(tmp_path) as srv:
        _red, blue, track, out = proposed(srv)
        pid = out["pending_id"]
        assert pid == f"{session(srv).session_id}-E1"
        assert out["execute_args"] == {"pending_id": pid, "shooter_id": blue["unit_id"],
                                       "target_track_id": track.track_id}
        assert out["target"] == {"track_id": track.track_id, "label": "Air-defence guns",
                                 "perceived_class": "aaa_towed", "confidence": "probable"}
        assert out["shooter"]["designator"] == "Blue artillery 1"
        p = out["p_estimate"]
        want = wg.strike_probabilities(T.CLASSES["blue_artillery"],
                                       T.estimate_hardness("aaa_towed"), out["range_m"],
                                       "probable")
        assert {k: p[k] for k in want} == want and p["package_survive"] is None
        assert out["corridor"] is None and out["simulated"] is True
        assert out["expires_at_ms"] - session(srv).pendings[pid].created_ms == 600_000
        e = session(srv).engagements[-1]
        assert (e.kind, e.phase, e.target, e.attacker) == (
            "blue_strike", "proposed", f"trk:{track.track_id}", f"frc:{blue['unit_id']}")
        assert e.inputs[0].startswith("Range ") and e.inputs[0].endswith("of 20.0 km")
        assert e.inputs[1] == "Perceived as air-defence guns, probable"
        prev = srv.wargame.preview(pid)
        assert prev["verb_kind"] == "engagement" and prev["kind"] == "blue_strike"
        assert [c["ok"] for c in prev["checks"]] == [True] * 7
        assert [c["text"] for c in prev["checks"]] == [
            "Target is a simulated scenario unit",
            "More than 500 m from any mapped place or theater point",
            "Not within 1 km of a protected place", "Shooter active with ammunition",
            "In range", "Engagement still waiting for approval", "Wargame session active"]
        assert prev["target"]["label"] == "Air-defence guns" and prev["target"]["scenario"]
        assert prev["target"]["graph_id"] == f"trk:{track.track_id}"
        assert prev["attacker"] == {"id": f"frc:{blue['unit_id']}", "label": "Blue artillery 1",
                                    "wg_class": "blue_artillery"}
        assert set(prev) >= {"checks", "target", "attacker", "p_notional"}
        assert srv.wargame.preview("WG-nope-E9") == {}


def test_propose_refusals_in_order(tmp_path):
    with wg_server(tmp_path) as srv:
        red, blue = red_blue(srv)
        u = unit(srv, red["unit_id"])
        eng = srv.wargame
        once = sense(srv, u.object_name, times=1)                 # one look: "possible"
        assert run(eng.propose_strike(blue["unit_id"], once.track_id))["error"] == \
            "insufficient_confidence"
        track = sense(srv, u.object_name, times=3)
        assert run(eng.propose_strike("blue-nope-1", track.track_id))["error"] == \
            "shooter_unavailable"
        assert run(eng.propose_strike(red["unit_id"], track.track_id))["error"] == \
            "shooter_unavailable"
        point = spawn(srv, "blue", "blue_defended_point", ne(-900, 900))
        assert run(eng.propose_strike(point["unit_id"], track.track_id))["error"] == \
            "shooter_unavailable"
        mech = spawn(srv, "blue", "blue_mech", ne(-1500, -1500))          # 2.5 km reach
        assert run(eng.propose_strike(mech["unit_id"], track.track_id))["error"] == \
            "out_of_range"
        unit(srv, blue["unit_id"]).ammo = 0
        assert run(eng.propose_strike(blue["unit_id"], track.track_id))["error"] == \
            "shooter_unavailable"
        unit(srv, blue["unit_id"]).ammo = 12
        for _ in range(wg.MAX_PENDING):
            assert "pending_id" in run(eng.propose_strike(blue["unit_id"], track.track_id))
        assert run(eng.propose_strike(blue["unit_id"], track.track_id))["error"] == \
            "pending_limit"


def test_only_the_console_session_that_approved_executes_and_only_once(tmp_path):
    with wg_server(tmp_path) as srv:
        _red, blue, _track, out = proposed(srv)
        eng, args = srv.wargame, out["execute_args"]
        refusal = {"rejected": True, "error": "engagement_requires_console_approval",
                   "message": wg.ENGINE_MESSAGES["engagement_requires_console_approval"],
                   "simulated": True}
        # Not authorized yet: refused, even inside a console call.
        with wg.console_call("chat-A"):
            assert run(eng.execute(**args)) == refusal
        # Authorize refusals leave it proposed.
        with pytest.raises(wg.WargameRefused) as exc:
            eng.authorize(args["pending_id"], "apr-1", chat_session="chat-A",
                          args={**args, "target_track_id": "TRK-other"})
        assert exc.value.code == "engagement_args_mismatch"
        with pytest.raises(wg.WargameRefused) as exc:
            eng.authorize(args["pending_id"], "apr-1", chat_session="", args=args)
        assert exc.value.code == "engagement_requires_console_approval"
        with pytest.raises(wg.WargameRefused) as exc:
            eng.authorize("WG-x-E9", "apr-1", chat_session="chat-A", args=args)
        assert exc.value.code == "unknown_pending"
        assert session(srv).pendings[args["pending_id"]].state == "proposed"
        eng.authorize(args["pending_id"], "apr-1", chat_session="chat-A",
                      args={**args, "idempotency_key": "k1"})
        with pytest.raises(wg.WargameRefused) as exc:        # one authorization
            eng.authorize(args["pending_id"], "apr-2", chat_session="chat-A", args=args)
        assert exc.value.code == "engagement_not_pending"
        assert run(eng.execute(**args)) == refusal                        # /mcp: no context
        with wg.console_call("chat-B"):
            assert run(eng.execute(**args)) == refusal                    # another session
        with wg.console_call("chat-A"):
            assert run(eng.execute(**{**args, "shooter_id": "blue-x-1"})) == refusal
        rows = audit(srv, "engagement_confirm_refused")
        assert [r["reason"] for r in rows] == [
            "not authorized", "not from the approving console session",
            "not from the approving console session", "arguments differ"]
        draws = session(srv).rng_blue.draw
        assert draws == 0 and unit(srv, blue["unit_id"]).ammo == 12
        with wg.console_call("chat-A"):
            first = run(eng.execute(**args))
            assert first["executed"] is True and first["outcome"] is None      # fog
            assert first["outcome_note"] == wg.OUTCOME_NOTE
            again = run(eng.execute(**args))                               # replay
        assert again == first
        assert session(srv).rng_blue.draw == 1 and unit(srv, blue["unit_id"]).ammo == 11
        e = session(srv).engagements[-1]
        assert e.phase == "adjudicated" and e.draw == 1 and e.approval_id == "apr-1"
        assert e.outcome in ("missed", "suppressed", "damaged", "destroyed")
        with wg.console_call("chat-B"):
            assert run(eng.execute(**args)) == refusal                  # consumed, other caller


def test_an_expired_or_denied_engagement_never_fires(tmp_path):
    with wg_server(tmp_path) as srv:
        _red, blue, track, out = proposed(srv)
        eng, args = srv.wargame, out["execute_args"]
        eng.authorize(args["pending_id"], "apr-1", chat_session="chat-A", args=args)
        session(srv).pendings[args["pending_id"]].expires_at_ms = wg._now_ms() - 1
        with wg.console_call("chat-A"):
            assert run(eng.execute(**args))["error"] == "engagement_requires_console_approval"
        eng.step(10.0, {})                                      # housekeeping expires it
        assert session(srv).pendings[args["pending_id"]].state == "expired"
        assert session(srv).engagements[-1].phase == "expired"
        second = run(eng.propose_strike(blue["unit_id"], track.track_id))
        eng.deny(second["pending_id"])
        assert session(srv).pendings[second["pending_id"]].state == "denied"
        with pytest.raises(wg.WargameRefused):
            eng.authorize(second["pending_id"], "apr-2", chat_session="chat-A",
                          args=second["execute_args"])
        third = run(eng.propose_strike(blue["unit_id"], track.track_id))
        session(srv).pendings[third["pending_id"]].expires_at_ms = wg._now_ms() - 1
        with pytest.raises(wg.WargameRefused) as exc:
            eng.authorize(third["pending_id"], "apr-3", chat_session="chat-A",
                          args=third["execute_args"])
        assert exc.value.code == "engagement_expired"
        assert session(srv).rng_blue.draw == 0


def test_a_unit_driven_onto_a_mapped_place_after_authorize_is_not_struck(tmp_path, monkeypatch):
    gap = 700.0
    monkeypatch.setattr(sites, "fetch_exclusion",
                        lambda *a, **k: excl(rect_east_of(P_RED, gap, 200.0)))
    with wg_server(tmp_path) as srv:
        red, blue, _track, out = proposed(srv)
        eng, args = srv.wargame, out["execute_args"]
        eng.authorize(args["pending_id"], "apr-1", chat_session="chat-A", args=args)
        u = unit(srv, red["unit_id"])
        k = 111_320.0 * math.cos(math.radians(u.lat))
        dest = (u.lat, u.lon + 250.0 / k, u.alt_hae_m)       # 450 m from the footprint
        srv.backend.sim.set_object_route(u.object_name, [dest], speed_mps=200.0)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            lat, lon, _ = srv.backend.sim.object_geo(u.object_name)
            if haversine_m(lat, lon, dest[0], dest[1]) < 5.0:
                break
            time.sleep(0.05)
        prev = eng.preview(args["pending_id"])
        assert prev["checks"][0]["ok"] is True and prev["checks"][1]["ok"] is False
        with wg.console_call("chat-A"):
            got = run(eng.execute(**args))
        assert got == sp.refusal("target_near_real_site")
        assert session(srv).pendings[args["pending_id"]].state == "expired"
        assert session(srv).engagements[-1].phase == "expired"
        assert session(srv).rng_blue.draw == 0 and unit(srv, blue["unit_id"]).ammo == 12


def test_a_virtual_strike_package_plans_a_corridor_and_can_be_lost(tmp_path):
    with wg_server(tmp_path) as srv:
        start(srv, red_engages=False, reveal_red=True, seed=99)
        red = spawn(srv, "red", "ad_gun", P_RED)
        pkg = spawn(srv, "blue", "blue_strike_air", ne(-900, -900))
        assert pkg["strike_range_m"] is None                        # the whole AO
        track = sense(srv, unit(srv, red["unit_id"]).object_name)
        out = run(srv.wargame.propose_strike(pkg["unit_id"], track.track_id))
        assert "pending_id" in out, out
        cor = out["corridor"]
        assert cor["id"].startswith("vec:cor-") and cor["threat_basis"] == "truth"
        assert 0.0 < out["p_estimate"]["package_survive"] <= 1.0
        prev = srv.wargame.preview(out["pending_id"])
        assert prev["verb_kind"] == "strike" and prev["vector"]["id"] == cor["id"]
        assert out["execute_args"]["shooter_id"] == pkg["unit_id"]
        vec = session(srv).vectors[cor["id"][4:]]
        assert vec["alt_agl_m"] == T.PACKAGE_ALT_AGL_M and vec["speed_mps"] == \
            T.PACKAGE_SPEED_MPS and vec["from"] == f"frc:{pkg['unit_id']}"
        srv.wargame.authorize(out["pending_id"], "apr-1", chat_session="c",
                              args=out["execute_args"])
        with wg.console_call("c"):
            done = run(srv.wargame.execute(**out["execute_args"]))
        assert done["executed"] is True and done["outcome"] is not None    # reveal_red
        e = session(srv).engagements[-1]
        assert e.inputs[-1].startswith("Package survival") or e.inputs[-2].startswith(
            "Package survival")
        assert session(srv).rng_blue.draw in (1, 2)
        if done["package_lost"]:
            assert e.consequence == "own_loss" and e.outcome == "missed"
            assert unit(srv, pkg["unit_id"]).state == "destroyed"
        assert vec["proposed"] is False


def test_bda_tiers_and_the_fog_on_a_blue_strike(tmp_path):
    with wg_server(tmp_path) as srv:
        red, _blue, _track, out = proposed(srv)
        eng, args = srv.wargame, out["execute_args"]
        eng.authorize(args["pending_id"], "apr-1", chat_session="c", args=args)
        with wg.console_call("c"):
            run(eng.execute(**args))
        e = session(srv).engagements[-1]
        u = unit(srv, red["unit_id"])

        def blue_row():
            return next(n for n in eng.graph_rows(truth=False)["nodes"]
                        if n["id"] == f"eng:{e.engagement_id}")["attrs"]

        eng.step(50.0, {})
        assert e.bda == {"state": "none", "looks": 0, "last_look_ms": None}
        assert blue_row()["outcome"] is None and blue_row()["outcome_hidden"] is True
        truth = next(n for n in eng.graph_rows(truth=True)["nodes"]
                     if n["id"] == f"eng:{e.engagement_id}")["attrs"]
        assert truth["outcome"] == e.outcome and truth["outcome_hidden"] is False
        sense(srv, u.object_name, times=1)
        eng.step(51.0, {})
        assert e.bda["looks"] == 1
        assert e.bda["state"] == wg.bda.bda_state(u.state, u.damaged, 1)
        assert blue_row()["outcome"] == e.outcome and blue_row()["outcome_hidden"] is False
        sense(srv, u.object_name, times=1)
        eng.step(52.0, {})
        assert e.bda["looks"] == 2
        assert e.bda["state"] == wg.bda.bda_state(u.state, u.damaged, 2)
        # Force the tiers: destroyed is probable after 1 look, confirmed after 2.
        u.state, u.damaged = "destroyed", True
        e.bda = None
        eng.step(53.0, {})
        assert e.bda["state"] == "destroyed_confirmed"
        kinds = [x["kind"] for x in eng.status(truth=False, events=100)["events"]]
        assert "bda_assessed" in kinds
        # Looks only count on gated tracks: a stranger's detections do not.
        before = e.bda["looks"]
        run(tool(srv, "sim_spawn_target")(lat=ne(-1500, 1500)[0], lon=ne(-1500, 1500)[1],
                                          ob_class="aaa_towed", name="aaa_towed_77"))
        sense(srv, "aaa_towed_77", times=2)
        eng.step(54.0, {})
        assert e.bda["looks"] == before


def test_a_corridor_plan_is_a_dry_run_ready_recon_route(tmp_path):
    with wg_server(tmp_path) as srv:
        red, _ = red_blue(srv)
        u = unit(srv, red["unit_id"])
        track = sense(srv, u.object_name)
        out = run(srv.wargame.plan_corridor("Drone1", track.track_id, relook=True,
                                            relook_radius_m=400.0))
        assert "waypoints" in out, out
        assert out["threat_basis"] == "sensed" and out["threats_considered"] == 1
        assert out["terrain_masking"] == {"source": "flat (real terrain not loaded)",
                                          "coverage_pct": 0.0}
        assert wg.TERRAIN_OFF_CAVEAT in out["caveats"] and wg.SENSED_CAVEAT in out["caveats"]
        assert out["relook_points"] > 0 and len(out["waypoints"]) <= 40 + 16
        assert out["recon_args"] == {"vehicle": "Drone1", "waypoints": out["waypoints"],
                                     "alt_agl_m": 60.0, "dry_run": True}
        assert out["vector_id"].startswith("vec:cor-") and out["simulated"] is True
        first = out["waypoints"][0]
        assert haversine_m(first["lat"], first["lon"], HOME.latitude, HOME.longitude) < 5.0
        dry = run(tool(srv, "mission_recon_route")(**out["recon_args"]))
        assert dry["dry_run"] is True and dry["executed"] is False
        assert dry["gate"]["ok"] is True, dry["gate"]          # geofence + fuel
        for bad in ({"relook_radius_m": 100.0}, {"alt_agl_m": -5.0}):
            assert run(srv.wargame.plan_corridor("Drone1", track.track_id, **bad))["error"] \
                == "invalid_parameter"
        assert run(srv.wargame.plan_corridor("NoSuchDrone", track.track_id))["error"] == \
            "vehicle_unknown"
        session(srv).lost["Drone1"] = {"by": "Red AD guns 1"}
        assert run(srv.wargame.plan_corridor("Drone1", track.track_id))["error"] == \
            "vehicle_lost"


# ------------------------------------------------------ end and the AAR -----

def scripted_session(srv, monkeypatch, *, seed: int = SEED) -> dict:
    """Start, place both sides, sense, strike once, let red fire, end. Returns
    every engine output (for the token check) and the AAR."""
    monkeypatch.setattr(srv.wargame, "_environment", lambda: CLEAR)
    eng, outs = srv.wargame, {}
    outs["start"] = run(eng.start(seed=seed, red_engages=True))
    outs["spawn_red"] = run(eng.spawn("red", "ad_short", *P_RED))
    outs["spawn_blue"] = run(eng.spawn("blue", "blue_artillery", *P_BLUE))
    outs["generate"] = run(eng.generate("mech_advance", "low"))
    red = unit(srv, outs["spawn_red"]["units"][0]["unit_id"])
    track = sense(srv, red.object_name)
    outs["corridor"] = run(eng.plan_corridor("Drone1", track.track_id, relook=True))
    outs["propose"] = run(eng.propose_strike(outs["spawn_blue"]["units"][0]["unit_id"],
                                             track.track_id))
    args = outs["propose"]["execute_args"]
    outs["preview"] = eng.preview(args["pending_id"])
    eng.authorize(args["pending_id"], "apr-1", chat_session="c", args=args)
    with wg.console_call("c"):
        outs["execute"] = run(eng.execute(**args))
    tele = {"Drone1": over(red, up=300.0, east_m=3000.0)}
    for i in range(8):
        eng.step(100.0 + 61.0 * i, tele)
    sense(srv, red.object_name, times=2)
    eng.step(700.0, tele)
    run(eng._await_losses())
    for truth in (False, True):
        outs[f"status_{truth}"] = eng.status(truth=truth, events=100)
        outs[f"forces_{truth}"] = eng.list_forces(truth=truth)
        outs[f"graph_{truth}"] = eng.graph_rows(truth=truth)
        outs[f"overlay_{truth}"] = eng.overlay_features(truth=truth)
    outs["end"] = run(eng.end(reason="operator"))
    outs["aar"] = srv.reports[outs["end"]["aar_id"]]
    return outs


def test_end_removes_everything_revives_drones_and_files_the_aar(tmp_path, monkeypatch):
    with wg_server(tmp_path) as srv:
        srv.reports["latest"] = latest = {"report_id": "latest", "marker": 1}
        outs = scripted_session(srv, monkeypatch)
        sid = outs["start"]["session_id"]
        end = outs["end"]
        assert end["ended"] is True and end["aar_id"] == f"aar-{sid}"
        assert end["resource"] == f"uav://reports/aar-{sid}"
        assert srv.reports["latest"] is latest                   # never `latest`
        aar = outs["aar"]
        assert aar["report_type"] == "AAR" and aar["incomplete"] is False
        assert aar["session_id"] == sid and aar["theater_id"] == "default"
        assert aar["seed"] == SEED and aar["engine"] == aar["table_version"] == "wg-notional/1"
        assert aar["simulated"] is True and aar["note"] == T.NOTIONAL_NOTE
        assert aar["markdown"].startswith("# After-action review (simulated)")
        for section in ("## Summary", "## Timeline", "## Sorties",
                        "## Battle damage accuracy", "## Replay"):
            assert section in aar["markdown"]
        assert len(aar["markdown"].encode()) <= 40_000
        assert THEATER.place not in aar["markdown"] and "Redmond" not in aar["markdown"]
        assert [b["engagement_id"] for b in aar["bda_accuracy"]] == \
            [outs["execute"]["engagement_id"]]
        assert aar["track_ids"] and all(srv.tracks.get(t) is None for t in aar["track_ids"])
        assert all(srv.store.tracks.get(t) is None for t in aar["track_ids"])
        # Scenario objects are gone from the sim and from the truth records.
        assert not any(r.get("provenance") == "scenario" for r in srv.targets.values())
        assert srv.backend.sim.objects() == {}
        assert srv.vehicles_lost == {} and not (tmp_path / "wargame.json").exists()
        eng = srv.wargame
        assert eng.active is False and eng.mode_key() == "isr"
        assert eng.last == {"session_id": sid, "aar_id": f"aar-{sid}",
                            "ended_at_ms": aar["ended_at_ms"]}
        assert eng.owns_name("anything") is False
        [row] = audit(srv, "wargame_session_ended")
        assert row["session_id"] == sid and row["track_ids"] == aar["track_ids"]
        assert eng.graph_rows(truth=True)["meta"]["wargame"] == {"active": False,
                                                                  "last": eng.last}
        assert run(eng.end(reason="again"))["error"] == "wargame_inactive"


def test_the_same_seed_gives_the_same_aar_outcomes(tmp_path, monkeypatch):
    def outcomes(aar):
        return [(e["kind"], e["side"], e["outcome"]) for e in aar["timeline"]
                if e["kind"] in ("red_shot", "red_ground", "blue_strike_executed",
                                 "package_lost", "bda_assessed")]

    with wg_server(tmp_path) as srv:
        first = outcomes(scripted_session(srv, monkeypatch)["aar"])
        second = outcomes(scripted_session(srv, monkeypatch)["aar"])
    assert first and first == second
    with wg_server(tmp_path / "other") as srv:
        assert outcomes(scripted_session(srv, monkeypatch)["aar"]) == first


def test_no_real_system_token_in_any_engine_output_or_the_aar(tmp_path, monkeypatch):
    with wg_server(tmp_path) as srv:
        outs = scripted_session(srv, monkeypatch)
        assert_no_real_system_tokens(outs)
        assert_no_real_system_tokens(outs["aar"]["markdown"])
        assert_no_real_system_tokens(wg.ENGINE_MESSAGES)
        for key, out in outs.items():
            if key.startswith("overlay"):
                assert all(f["properties"]["simulated"] is True for f in out), key
            elif isinstance(out, dict):
                assert out.get("simulated") is True or "nodes" in out, key
        for n in outs["graph_True"]["nodes"]:
            assert n["attrs"]["simulated"] is True
            assert "Redmond" not in str(n) and THEATER.place not in str(n)


def test_isr_reports_on_a_scenario_contact_name_no_real_system(tmp_path):
    """D1 (B17 relabel): during a session the ISR tools describe a simulated
    unit by its generic label; the OB library's real-system names and
    calibres stay out of SALUTE, INTREP and threat output."""
    with wg_server(tmp_path) as srv:
        start(srv, red_engages=False)
        red = spawn(srv, "red", "ad_gun", P_RED)
        trk = sense(srv, unit(srv, red["unit_id"]).object_name)
        assert trk.scenario is True and trk.ob_class == "aaa_towed"
        outs = {
            "list": run(tool(srv, "uav_list_tracks")()),
            "intrep": run(tool(srv, "uav_target_report")(detail="full")),
            "one": run(tool(srv, "uav_assess_threat")(vehicle="Drone1",
                                                      track_id=trk.track_id)),
            "area": run(tool(srv, "uav_assess_threat")(vehicle="Drone1")),
        }
        for key, out in outs.items():
            assert trk.track_id in str(out), key
            assert_no_real_system_tokens(out)
        [row] = outs["list"]["tracks"]
        assert row["equipment"]["platform"] == f"{T.label_for_ob('aaa_towed')} (notional)"
        assert row["scenario"] is True and row["ob_class"] == "aaa_towed"


# ------------------------------------------------- graph rows and overlay -----

def test_graph_rows_follow_the_contract_and_the_fog(tmp_path, monkeypatch):
    assert wg.CATEGORY_GROUP == intel_graph.CATEGORY_GROUP
    with wg_server(tmp_path) as srv:
        monkeypatch.setattr(srv.wargame, "_environment", lambda: CLEAR)
        start(srv, red_engages=True)
        red = spawn(srv, "red", "ad_short", P_RED)
        blue = spawn(srv, "blue", "blue_artillery", P_BLUE)
        u = unit(srv, red["unit_id"])
        track = sense(srv, u.object_name)
        tele = {"Drone1": over(u, up=300.0, east_m=2000.0)}
        for i in range(12):
            srv.wargame.step(100.0 + 13.0 * i, tele)
        eng = srv.wargame
        truth, blue_view = eng.graph_rows(truth=True), eng.graph_rows(truth=False)
        fr = next(n for n in truth["nodes"] if n["id"] == f"frc:{red['unit_id']}")
        assert fr["type"] == "force" and fr["label"] == "Red SAM 1"
        assert fr["group"] == "air-defense" and fr["salience"] == 0.6
        assert fr["subtitle"] == "Red  Surface-to-air, short range  Active  Scenario"
        assert fr["attrs"]["provenance"] == "scenario" and fr["attrs"]["side"] == "red"
        assert fr["attrs"]["correlated"] == [f"trk:{track.track_id}"]
        assert fr["attrs"]["threat_range_m"] == 8000.0 and fr["status"] in ("critical", "warn")
        fb = next(n for n in blue_view["nodes"] if n["id"] == f"frc:{blue['unit_id']}")
        assert fb["group"] == "ground-forces" and fb["status"] == "ok"
        assert "correlated" not in fb["attrs"]
        assert not any(n["id"] == f"frc:{red['unit_id']}" for n in blue_view["nodes"])
        edges = {(e["a"], e["b"], e["kind"]) for e in truth["edges"]}
        assert (f"trk:{track.track_id}", f"frc:{red['unit_id']}", "correlates") in edges
        assert not any(e["kind"] in ("correlates", "threatens", "axis")
                       for e in blue_view["edges"])
        shots = [n for n in blue_view["nodes"] if n["type"] == "engagement"
                 and n["attrs"]["kind"] == "red_shot"]
        assert shots
        for n in shots:
            assert n["attrs"]["attacker"] is None
            assert n["attrs"]["attacker_label"] == wg.RED_AD_HIDDEN
            assert n["attrs"]["p_notional"] is None and n["group"] == "fleet"
            assert n["label"] == "Simulated shot on Drone1"
            assert not any(e["a"] == n["id"] and e["kind"] == "launched_by"
                           for e in blue_view["edges"])
        own_loss = [n for n in shots if n["attrs"]["consequence"] == "own_loss"]
        assert all(n["status"] == "critical" and n["attrs"]["outcome"] == "destroyed"
                   for n in own_loss)
        meta = blue_view["meta"]["wargame"]
        assert meta["active"] is True and meta["engine"] == "wg-notional/1"
        assert meta["counts"]["red"] == {"seen": 1} and meta["truth_view"] is False
        assert truth["meta"]["wargame"]["counts"]["red"]["units"] == 1
        assert blue_view["meta"]["caveats"] == [wg.SESSION_CAVEAT]
        for n in truth["nodes"]:
            assert set(n) == {"id", "type", "label", "subtitle", "group", "salience",
                              "status", "ts_ms", "lat", "lon", "attrs"}
            assert n["status"] in ("ok", "warn", "critical", "stale")
        assert len(truth["nodes"]) <= wg.GRAPH_FORCES + wg.GRAPH_ENGAGEMENTS + wg.GRAPH_VECTORS


def test_overlay_features_are_geojson_and_fogged(tmp_path):
    with wg_server(tmp_path) as srv:
        start(srv)
        red = spawn(srv, "red", "ad_gun", P_RED)
        spawn(srv, "red", "radar_early_warning", ne(-1600, 1500))
        spawn(srv, "blue", "blue_artillery", P_BLUE)
        srv.wargame.flush()
        truth = srv.wargame.overlay_features(truth=True)
        blue = srv.wargame.overlay_features(truth=False)
        kinds = sorted({f["properties"]["kind"] for f in truth})
        assert kinds == ["force", "force_envelope"]
        env = [f for f in truth if f["properties"]["kind"] == "force_envelope"]
        assert {(f["properties"]["force"], f["properties"]["ring"]) for f in env} == {
            (f"frc:{red['unit_id']}", "threat"), (f"frc:{red['unit_id']}", "detection"),
            ("frc:red-radar-1", "detection")}
        for f in env:
            ring = f["geometry"]["coordinates"][0]
            assert f["geometry"]["type"] == "Polygon" and ring[0] == ring[-1]
            assert len(ring) >= 49                     # a masked fan or a 48-point circle
        for f in truth:
            p = f["properties"]
            assert p["simulated"] is True and p["register"] == "scenario"
            assert p["truth"] is True and p["id"] == f["id"]
        assert {f["properties"]["side"] for f in blue} == {"blue"}
        assert all(f["properties"]["kind"] == "force" for f in blue)
        assert all(f["properties"]["truth"] is False for f in blue)


def test_the_servers_guards_see_the_real_engine(tmp_path):
    """B5's `sim_spawn_target` / `sim_move_target` guards and the ingest hook,
    against this engine rather than a stub."""
    with wg_server(tmp_path) as srv:
        red, _ = red_blue(srv)
        u = unit(srv, red["unit_id"])
        near = run(tool(srv, "sim_spawn_target")(lat=u.lat + 100 / 111_320.0, lon=u.lon,
                                                 ob_class="supply_truck"))
        assert near["error"] == "near_scenario_unit"
        taken = run(tool(srv, "sim_spawn_target")(lat=ne(-1500, 1500)[0],
                                                  lon=ne(-1500, 1500)[1],
                                                  ob_class="aaa_towed", name=u.object_name))
        assert taken["error"] == "scenario_name"
        moved = run(tool(srv, "sim_move_target")(target_id=u.object_name,
                                                 waypoints=[list(ne(1500, 800))]))
        assert moved["error"] == "scenario_name"
        # The ingest hook hands the engine each scenario track once, persisted.
        track = sense(srv, u.object_name)
        assert srv.wargame.note_track(track) is False             # already known
        assert session(srv).track_ids == [track.track_id]


def test_the_wargame_thread_steps_on_sim_time_and_stops_at_the_end(tmp_path, monkeypatch):
    with wg_server(tmp_path) as srv:
        eng = srv.wargame
        eng.run_thread = True
        monkeypatch.setattr(eng, "_environment", lambda: CLEAR)
        start(srv, red_engages=True, seed=7)
        gun = unit(srv, spawn(srv, "red", "ad_gun", P_RED)["unit_id"])
        srv._last_tele["Drone9"] = over(gun, up=150.0, east_m=400.0)
        [th] = [t for t in threading.enumerate() if t.name == "godseye-wargame"]
        assert th.daemon is True
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not red_shots(srv):
            time.sleep(0.05)
        assert red_shots(srv), "the thread never engaged"
        assert eng.step_ms > 0.0 and eng.errors == 0
        # Its audit rows are written off the thread, and they do arrive.
        assert any(r.get("event", {}).get("kind") == "red_shot"
                   for r in audit(srv, "wargame_event"))
        # A failing pass is counted and audited once, and the loop lives on.
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise RuntimeError("pass failed")
        monkeypatch.setattr(eng, "_pass", boom)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(calls) < 3:
            time.sleep(0.05)
        assert len(calls) >= 3 and eng.errors >= 3
        assert len(audit(srv, "wargame_step_failed")) == 1
        monkeypatch.undo()
        srv._last_tele.pop("Drone9", None)
        run(eng.end(reason="test"))
        assert not th.is_alive()
        assert not any(t.name == "godseye-wargame" for t in threading.enumerate())


# ---------------------------------------- provenance split (D1, review fix) -----

def _isr_object(srv, name: str = "isr_obj_1", at: tuple[float, float] | None = None) -> dict:
    """A non-scenario sim object, spawned legally far from every unit."""
    lat, lon = at or ne(-1500, 1500)
    out = run(tool(srv, "sim_spawn_target")(lat=lat, lon=lon, ob_class="sam_short_range",
                                             name=name))
    assert out["status"] == "accepted", out
    return out


def test_a_non_scenario_objects_detections_never_feed_a_scenario_track(tmp_path):
    # Review B (boundary, high): an ISR object 50 m from a red unit had its
    # detections folded into the scenario track (same id, the fix moved 52 m),
    # and six of them alone qualified a simulated strike.
    with wg_server(tmp_path) as srv:
        red, blue = red_blue(srv)
        u = unit(srv, red["unit_id"])
        strack = sense(srv, u.object_name, times=1)             # one real look
        first = run(srv.wargame.propose_strike(blue["unit_id"], strack.track_id))
        assert first.get("error") == "insufficient_confidence", first
        before = (strack.lat, strack.lon, strack.sightings, len(strack.observations))
        _isr_object(srv)
        near = (u.lat + 50.0 / 111_320.0, u.lon)                # as if it drove there
        other = sense(srv, "isr_obj_1", times=6, at=near)
        assert other.track_id != strack.track_id and other.name == "isr_obj_1"
        assert other.scenario is False and other.track_id not in session(srv).track_ids
        assert (strack.lat, strack.lon, strack.sightings, len(strack.observations)) == before
        out = run(srv.wargame.propose_strike(blue["unit_id"], strack.track_id))
        assert out.get("error") == "insufficient_confidence" and "pending_id" not in out, out
        refused_as_not_scenario(srv, blue["unit_id"], other.track_id)
        # ...and the unit's own looks still build its own track
        again = sense(srv, u.object_name, times=3)
        assert again.track_id == strack.track_id and again.scenario is True


def test_sim_move_target_keeps_other_objects_clear_of_scenario_units(tmp_path):
    with wg_server(tmp_path) as srv:
        _isr_object(srv)
        mv = tool(srv, "sim_move_target")
        red, _ = red_blue(srv)
        u = unit(srv, red["unit_id"])
        # P_RED = ne(1600, 1000): waypoints relative to it
        near = (u.lat + 50.0 / 111_320.0, u.lon)

        def refused(**kw):
            out = run(mv(target_id="isr_obj_1", speed_mps=8.0, **kw))
            assert out["rejected"] is True and out["error"] == "near_scenario_unit", out
            assert "150 m" in out["message"]

        refused(waypoints=[{"lat": near[0], "lon": near[1]}])          # onto the unit
        refused(waypoints=[list(ne(1600, 400)), list(ne(1600, 1600))])  # a leg through it
        ring = [dict(zip(("lat", "lon"), ne(*p))) for p in
                ((1000, 1600), (2200, 1600), (2200, 400))]              # each leg >= 600 m off
        refused(waypoints=ring, loop=True)                              # the closing leg
        assert len(audit(srv, "move_target_refused")) == 3
        assert srv.targets["isr_obj_1"].get("route") is None, "a refused route was applied"
        ok = run(mv(target_id="isr_obj_1", waypoints=ring, speed_mps=8.0, loop=False))
        assert ok["status"] == "accepted", ok
        # sim_spawn_target's mobile_route is held to the same rule, before spawning
        far = ne(-1500, 2500)
        out = run(tool(srv, "sim_spawn_target")(
            lat=far[0], lon=far[1], ob_class="sam_short_range", name="isr_obj_2",
            mobile_route=[list(ne(1600, 400)), list(ne(1600, 1600))]))
        assert out["error"] == "near_scenario_unit", out
        assert "isr_obj_2" not in srv.targets
        # the engine drives its own red units through `_move_target`, unguarded
        assert run(srv._move_target(u.object_name, [list(ne(2200, 1000))], 8.0, False))["ok"]
        # outside a session (ISR) the same move is accepted, unchanged
        run(srv.wargame.end(reason="test"))
        out = run(mv(target_id="isr_obj_1", waypoints=[{"lat": near[0], "lon": near[1]}],
                     speed_mps=8.0))
        assert out["status"] == "accepted", out


@pytest.mark.parametrize("tid", ["iran-natanz", "iran-fordow"])
def test_a_session_never_starts_in_a_dynamic_ao_over_a_refused_preset(tmp_path, monkeypatch,
                                                                     tid):
    # Review B (boundary, medium): a dynamic AO on the preset's home was
    # cleared, and with complete map data a session started over the facility.
    import dataclasses
    pre = theaters.get(tid)
    with wg_server(tmp_path) as srv:
        dyn = theaters.make_dynamic(label="Field AO", place="Field",
                                    center=(pre.home_lat, pre.home_lon), half_extent_m=2200.0,
                                    home=(pre.home_lat, pre.home_lon),
                                    home_alt_msl_m=pre.home_alt_msl_m, provenance=None)
        assert dyn.wargame_ok is False
        d = 150.0 / 111_320.0
        monkeypatch.setattr(sites, "fetch_exclusion", lambda *a, **k: excl(
            (pre.home_lat - d, pre.home_lon - d, pre.home_lat + d, pre.home_lon + d)))
        # ...and a row forced to True is still refused (engine defence in depth)
        for t in (dyn, dataclasses.replace(dyn, wargame_ok=True)):
            srv.theater = t
            out = run(srv.wargame.start(seed=1))
            assert out.get("error") == "theater_not_cleared", out
            assert "session_id" not in out and srv.wargame.active is False
            assert srv.wargame.starting is False
