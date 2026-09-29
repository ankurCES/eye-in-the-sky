"""Server-side simulated-wargame hooks (PLAN §4.5a M14a; WG v2 §3.10, §5.2.6,
§5.2.9, §5.2.12 B5).

What the server itself owns, pinned against the fake AirSim:

* the hook surface B3's engine calls: `vehicles_lost`, `lose_vehicle`,
  `revive_vehicle`, `_spawn_object_record`, `UavBackend.destroy_object`, and
  the fake's `down_vehicle` (landed where it is, disarmed, `collision=True`);
* the `_submit` `vehicle_lost` refusal and `_force_rtb` returning None;
* the `uav://safety/geofence` `doctrine` block, with `isr_only` unchanged;
* `sim_reset` refused during a session;
* the `sim_spawn_target` guards (`duplicate_name`, `scenario_name`,
  `near_scenario_unit`), `sim_move_target` `scenario_name`, and the
  `sim_spawn_order_of_battle` skips;
* the ingest hook: a scenario track is tagged, handed to the engine and never
  observed by the pattern of life;
* the lazy wiring: `srv.wargame`, `srv.wargame_mcp` (never mounted) and
  `recover_on_boot()` after the restart replay.

The engine is a stub here (`StubEngine`): these tests pin the SERVER side of
the contract, whatever `wargame.py` does. No network; B5's ports 53600-53649.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
import types
from contextlib import contextmanager

import airsim
import pytest
from godseye_uav import server as server_mod
from godseye_uav import theaters
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint, canonical_altitude
from godseye_uav.safety import haversine_m
from godseye_uav.server import (
    DOCTRINE_RULE,
    VEHICLE_LOST_STATUS,
    GodseyeUavServer,
    UavBackend,
)
from godseye_uav.store import Store

_PORTS = list(range(53600, 53650))
_PORT = itertools.cycle(_PORTS[os.getpid() % len(_PORTS):]
                        + _PORTS[:os.getpid() % len(_PORTS)])
THEATER = theaters.get("default")
HOME = GeoPoint(THEATER.home_lat, THEATER.home_lon,
                canonical_altitude(THEATER.home_alt_msl_m, THEATER.home_lat,
                                   THEATER.home_lon, datum="msl").alt_hae)
HEAD_ISR_ONLY = ("M14: no kinetic tool exists on this server and no engagement "
                 "recommendation is produced.")
SID = "WG-a1b2c3"
#: A session-unique red object name, as the engine mints them (§5.2.4).
RED_NAME = "aaa_towed_1759072800123001"


def north_east(dn_m: float, de_m: float) -> tuple[float, float]:
    """A point `dn_m` north and `de_m` east of home (flat-earth, < 2 km)."""
    import math
    lat = HOME.latitude + dn_m / 111_320.0
    lon = HOME.longitude + de_m / (111_320.0 * math.cos(math.radians(HOME.latitude)))
    return lat, lon


class StubEngine:
    """The duck-typed engine surface the server reads (§3.10, §5.2.4)."""

    def __init__(self, *, active: bool = True, unit_near: bool = True):
        self.active = active
        self.starting = False
        self.revision = 0
        self.names: set[str] = set()
        self.units: dict[str, tuple[float, float]] = {}
        self.noted: list[str] = []
        if not unit_near:
            self.unit_near = None      # an engine without the method

    def mode_key(self) -> str:
        return f"wargame:{SID}" if self.active else "isr"

    def owns_name(self, name: str) -> bool:
        return self.active and name in self.names

    def note_track(self, track) -> bool:
        self.noted.append(track.track_id)
        return True

    def unit_near(self, lat: float, lon: float, radius_m: float):
        for uid, (ulat, ulon) in sorted(self.units.items()):
            if haversine_m(lat, lon, ulat, ulon) <= radius_m:
                return uid
        return None

    def recover_on_boot(self):
        return None


@contextmanager
def wg_server(tmp_path, **kw):
    """A default-theater server on a fake sim in B5's port range."""
    sim = srv = None
    for _ in range(len(_PORTS)):
        sim = FakeAirSim(home=HOME, port=next(_PORT))
        try:
            sim.start()
            break
        except OSError:
            sim.stop()
            sim = None
    assert sim is not None, "no free port in 53600-53649"
    store = Store(tmp_path)
    try:
        client = airsim.MultirotorClient(port=sim.port)
        client.confirmConnection()
        srv = GodseyeUavServer(UavBackend(client, HOME, sim=sim), store,
                               theater=THEATER, **kw)
        yield srv
    finally:
        if srv is not None:
            srv.stop_monitor()
            srv.tasking.shutdown()
        store.close()
        sim.stop()


def tool(srv, name):
    return srv.mcp._tool_manager._tools[name].fn


def audit(srv, kind: str) -> list[dict]:
    return [r for r in srv.store.audit.read_all() if r.get("kind") == kind]


async def geofence(srv) -> dict:
    items = list(await srv.mcp.read_resource("uav://safety/geofence"))
    return json.loads(items[0].content)


async def on_tasking_loop(srv, coro):
    """Run `coro` where the wargame thread runs it: on the tasking loop."""
    fut = asyncio.run_coroutine_threadsafe(coro, srv.tasking.loop)
    return await asyncio.wrap_future(fut)


async def wait_until(pred, timeout_s: float = 20.0, step_s: float = 0.05):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await pred():
            return True
        await asyncio.sleep(step_s)
    return False


# ------------------------------------------------------------ wiring -----

def test_an_isr_server_has_a_quiet_wargame_and_an_unmounted_registry(tmp_path):
    with wg_server(tmp_path) as srv:
        assert srv.wargame.active is False
        assert srv.wargame.mode_key() == "isr"
        assert srv.vehicles_lost == {}
        assert srv.wargame_mcp_enabled is False
        # Its own registry, never the /mcp one; /mcp carries no wg_ tool.
        assert isinstance(srv.wargame_mcp, server_mod.MCPServer)
        assert srv.wargame_mcp is not srv.mcp
        assert srv.wargame_mcp.name == "godseye-wargame"
        assert not any(n.startswith("wg_") for n in srv.mcp._tool_manager._tools)
        assert srv.wargame_recovery is None


def test_only_the_modules_own_absence_is_tolerated(monkeypatch):
    assert server_mod._optional_module("no_such_wargame_module_xyz") is None
    import importlib

    def broken(name, *a, **k):
        raise ModuleNotFoundError("No module named 'numpyish'", name="numpyish")

    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        server_mod._optional_module("wargame")


def test_wargame_mcp_without_the_tools_module_refuses_to_build(tmp_path):
    if server_mod._optional_module("wargame_tools") is not None:
        pytest.skip("wargame_tools (B4) is merged; the scoped catalog covers the flag")
    with pytest.raises(ValueError, match="wargame_tools"), \
            wg_server(tmp_path, wargame_mcp=True):
        pass


def test_recover_on_boot_runs_after_the_restart_replay(tmp_path, monkeypatch):
    seen: dict = {}

    class Engine(StubEngine):
        def __init__(self, srv):
            super().__init__(active=False)
            self.srv = srv

        def recover_on_boot(self):
            # The replay has already restored tracks, so they can be deleted.
            seen["recovery_exists"] = hasattr(self.srv, "recovery")
            return {"aborted": "WG-old"}

    real = server_mod._optional_module

    def fake(name):
        return (types.SimpleNamespace(WargameEngine=Engine) if name == "wargame"
                else real(name))

    monkeypatch.setattr(server_mod, "_optional_module", fake)
    with wg_server(tmp_path) as srv:
        assert isinstance(srv.wargame, Engine)
        assert seen == {"recovery_exists": True}
        assert srv.wargame_recovery == {"aborted": "WG-old"}


def test_a_failing_boot_recovery_is_reported_and_never_blocks_boot(tmp_path, monkeypatch):
    class Engine(StubEngine):
        def __init__(self, srv):
            super().__init__(active=False)

        def recover_on_boot(self):
            raise OSError("wargame.json unreadable")

    real = server_mod._optional_module
    monkeypatch.setattr(server_mod, "_optional_module",
                        lambda name: (types.SimpleNamespace(WargameEngine=Engine)
                                      if name == "wargame" else real(name)))
    with wg_server(tmp_path) as srv:
        assert srv.wargame_recovery == {"error": "OSError: wargame.json unreadable"}
        assert audit(srv, "wargame_recovery_failed")


# ---------------------------------------------------- geofence doctrine --

def test_the_geofence_doctrine_says_which_mode_and_isr_only_never_changes(tmp_path):
    with wg_server(tmp_path) as srv:
        isr = asyncio.run(geofence(srv))
        assert isr["doctrine"] == {"mode": "isr", "wargame_session": None,
                                   "wargame_mcp": False, "rule": DOCTRINE_RULE}
        assert DOCTRINE_RULE == ("M14a: ISR by default; simulated wargame tools "
                                 "exist only in an operator-approved session.")
        srv.wargame = StubEngine(active=True)
        during = asyncio.run(geofence(srv))
        assert during["doctrine"] == {"mode": "wargame", "wargame_session": SID,
                                      "wargame_mcp": False, "rule": DOCTRINE_RULE}
        # The flag is read, never inferred (a flagged build sets it at boot).
        srv.wargame_mcp_enabled = True
        flagged = asyncio.run(geofence(srv))
        assert flagged["doctrine"]["wargame_mcp"] is True
        assert flagged["doctrine"]["mode"] == "wargame"
        srv.wargame.active = False
        after = asyncio.run(geofence(srv))
        assert after["doctrine"]["mode"] == "isr"
        assert after["doctrine"]["wargame_session"] is None
        for doc in (isr, during, flagged, after):
            assert doc["isr_only"] == HEAD_ISR_ONLY


# ---------------------------------------------------------- sim_reset ----

def test_sim_reset_is_refused_while_a_session_starts_or_runs(tmp_path):
    with wg_server(tmp_path) as srv:
        reset = tool(srv, "sim_reset")
        srv.wargame = StubEngine(active=True)
        out = asyncio.run(reset())
        assert out == {"rejected": True, "error": "wargame_active",
                       "message": "end the wargame first"}
        srv.wargame.active, srv.wargame.starting = False, True
        assert asyncio.run(reset())["error"] == "wargame_active"
        assert srv.sim_resets == []          # nothing was reset
        assert len(audit(srv, "sim_reset_refused")) == 2
        srv.wargame.starting = False
        ok = asyncio.run(reset())
        assert ok["ok"] is True and ok["status"] == "accepted"


# ------------------------------------------- the fake's down_vehicle -----

def test_down_vehicle_lands_it_where_it_is_disarmed_and_collided(tmp_path):
    with wg_server(tmp_path) as srv:
        sim, c = srv.backend.sim, srv.backend.client
        c.enableApiControl(True, vehicle_name="Drone1")
        c.armDisarm(True, vehicle_name="Drone1")
        c.takeoffAsync(vehicle_name="Drone1").join()
        c.moveToPositionAsync(40.0, 25.0, -20.0, 8.0, vehicle_name="Drone1").join()
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            p = c.getMultirotorState(vehicle_name="Drone1").kinematics_estimated.position
            if abs(p.x_val - 40.0) < 1.0 and abs(p.y_val - 25.0) < 1.0:
                break
            time.sleep(0.05)
        before = c.getMultirotorState(vehicle_name="Drone1")
        assert int(before.landed_state) != 0          # flying
        assert c.simGetCollisionInfo(vehicle_name="Drone1").has_collided is False

        out = sim.down_vehicle("Drone1")
        assert out["landed"] is True and out["collision"] is True
        state = c.getMultirotorState(vehicle_name="Drone1")
        pos = state.kinematics_estimated.position
        assert int(state.landed_state) == 0
        assert abs(pos.x_val - 40.0) < 2.0 and abs(pos.y_val - 25.0) < 2.0
        assert pos.z_val == pytest.approx(0.0)
        assert c.simGetCollisionInfo(vehicle_name="Drone1").has_collided is True
        # A late command cannot lift a downed airframe off the ground.
        c.armDisarm(True, vehicle_name="Drone1")
        c.takeoffAsync(vehicle_name="Drone1")
        time.sleep(0.2)
        late = c.getMultirotorState(vehicle_name="Drone1")
        assert int(late.landed_state) == 0
        assert late.kinematics_estimated.position.z_val == pytest.approx(0.0)
        assert c.simGetCollisionInfo(vehicle_name="Drone1").has_collided is True

        parked = sim.park_vehicle("Drone1")
        assert parked["landed"] is True
        assert c.simGetCollisionInfo(vehicle_name="Drone1").has_collided is False
        home = c.getMultirotorState(vehicle_name="Drone1").kinematics_estimated.position
        assert (home.x_val, home.y_val, home.z_val) == pytest.approx((0.0, 0.0, 0.0))
        c.enableApiControl(True, vehicle_name="Drone1")
        c.armDisarm(True, vehicle_name="Drone1")
        c.takeoffAsync(vehicle_name="Drone1").join()
        assert int(c.getMultirotorState(vehicle_name="Drone1").landed_state) != 0
        with pytest.raises(KeyError):
            sim.down_vehicle("NoSuchDrone")


# ------------------------------------------ lose_vehicle / revive -------

def test_a_downed_drone_aborts_flies_nothing_and_is_revived_at_home(tmp_path):
    with wg_server(tmp_path) as srv:
        async def main():
            route = [list(north_east(200, 0)), list(north_east(200, 300)),
                     list(north_east(0, 300))]
            h = await tool(srv, "mission_recon_route")(
                vehicle="Drone1", waypoints=route, alt_agl_m=40.0, speed_mps=8.0)
            assert h.get("status") == "accepted", h
            mid = h["mission_handle"]

            async def airborne():
                t = await srv._telemetry("Drone1")
                return int(t["landed_state"]) != 0 and t["alt_agl_m"] > 10.0
            assert await wait_until(airborne)
            assert srv.missions[mid]["state"] == "executing"

            lost = await on_tasking_loop(srv, srv.lose_vehicle("Drone1", "Red SAM 1"))
            assert lost["lost"] is True and lost["simulated"] is True
            assert lost["by"] == "Red SAM 1" and lost["down_error"] is None
            assert h["task_id"] in lost["cancelled"]
            assert lost["missions_incomplete"] == [mid]
            assert srv.vehicles_lost["Drone1"] == {"by": "Red SAM 1",
                                                   "at_ms": lost["at_ms"]}
            task = srv.tasking.get("Drone1", h["task_id"])
            assert task.state.value == "cancelled"
            assert srv.missions[mid]["state"] == "incomplete"
            assert srv.missions[mid]["status"] == VEHICLE_LOST_STATUS
            assert srv.mission_flags["Drone1"] == VEHICLE_LOST_STATUS
            tele = await srv._telemetry("Drone1")
            assert int(tele["landed_state"]) == 0
            assert srv.backend.client.simGetCollisionInfo(
                vehicle_name="Drone1").has_collided is True

            # Every command is refused, and nothing flies it home either.
            refused = srv._submit("Drone1", "uav_hover", {}, None)
            assert refused == {
                "rejected": True, "error": "vehicle_lost",
                "message": ("Drone1 was lost in the simulated wargame; it "
                            "returns when the wargame ends."),
                "rejected_tool": "uav_hover", "vehicle": "Drone1"}
            takeoff = await tool(srv, "uav_takeoff")(vehicle="Drone1")
            assert takeoff["error"] == "vehicle_lost" and takeoff["rejected"] is True
            assert (await tool(srv, "uav_land")(vehicle="Drone1"))["error"] == "vehicle_lost"
            assert await srv._force_rtb("Drone1", reason="bingo") is None
            assert await srv._execute_lost_link("Drone1", {}) is None
            again = await on_tasking_loop(srv, srv.lose_vehicle("Drone1", "Red AAA 2"))
            assert again["already_lost"] is True and again["by"] == "Red SAM 1"
            [row] = audit(srv, "vehicle_lost_simulated")
            assert row["vehicle"] == "Drone1" and row["simulated"] is True

            back = await srv.revive_vehicle("Drone1")
            assert back["revived"] is True and back["park_error"] is None
            assert back["was_lost"]["by"] == "Red SAM 1"
            assert srv.vehicles_lost == {}
            assert "Drone1" not in srv.mission_flags
            assert srv.backend.client.simGetCollisionInfo(
                vehicle_name="Drone1").has_collided is False
            tele = await srv._telemetry("Drone1")
            assert int(tele["landed_state"]) == 0
            assert haversine_m(tele["lat"], tele["lon"],
                               HOME.latitude, HOME.longitude) < 2.0
            assert audit(srv, "vehicle_revived")
            ok = await tool(srv, "uav_takeoff")(vehicle="Drone1")
            assert ok.get("status") == "accepted", ok
            nothing = await srv.revive_vehicle("Drone1")
            assert nothing["revived"] is False
        asyncio.run(main())


def test_losing_a_drone_cancels_a_forced_rtb_too(tmp_path):
    """The operator-override abort clears an un-cancellable RTB, and its
    de-duplication state goes with it."""
    with wg_server(tmp_path) as srv:
        async def main():
            await tool(srv, "uav_takeoff")(vehicle="Drone1", alt_agl_m=15.0)

            async def airborne():
                t = await srv._telemetry("Drone1")
                return t["alt_agl_m"] > 8.0
            assert await wait_until(airborne)
            h = await on_tasking_loop(srv, srv._force_rtb("Drone1", reason="bingo",
                                                          detail="test"))
            assert h is not None
            await on_tasking_loop(srv, srv.lose_vehicle("Drone1", "Red SAM 1"))
            assert srv.tasking.get("Drone1", h["task_id"]).state.value == "cancelled"
            assert "Drone1" not in srv._rtb_active and "Drone1" not in srv._rtb_task
        asyncio.run(main())


# ----------------------------------- _spawn_object_record + destroy ------

def test_spawn_object_record_files_a_scenario_truth_record(tmp_path):
    with wg_server(tmp_path) as srv:
        async def main():
            lat, lon = north_east(900, 900)
            rec = await srv._spawn_object_record(
                RED_NAME, "aaa_towed", lat, lon, provenance="scenario",
                session_id=SID, side="red", unit_id="red-aaa-1",
                designator="Red AAA 1")
            assert srv.targets[RED_NAME] is rec
            assert rec["provenance"] == "scenario" and rec["session_id"] == SID
            assert rec["side"] == "red" and rec["unit_id"] == "red-aaa-1"
            assert rec["designator"] == "Red AAA 1"
            # Classified exactly, on the ground under the point (T1: one MSL
            # -> HAE conversion), and really in the sim.
            assert rec["ob_class"] == rec["class"] == "aaa_towed"
            assert rec["class_evidence"]["rule"] == "explicit ob_class key"
            assert rec["alt_msl_m"] == pytest.approx(THEATER.home_alt_msl_m)
            glat, glon, galt = srv.backend.sim.object_geo(RED_NAME)
            # (The fake's NED<->geodetic earth models differ by ~2 m at 1 km.)
            assert haversine_m(glat, glon, lat, lon) < 5.0
            assert galt == pytest.approx(rec["alt_hae_m"], abs=0.5)
            [row] = audit(srv, "spawn_target")
            assert row["provenance"] == "scenario" and row["unit_id"] == "red-aaa-1"

            for bad in ({"name": RED_NAME, "ob_class": "aaa_towed"},      # taken
                        {"name": "x_1", "ob_class": "not_a_class"},
                        {"name": "", "ob_class": "aaa_towed"}):
                with pytest.raises(ValueError):
                    await srv._spawn_object_record(bad["name"], bad["ob_class"],
                                                   lat, lon)
            with pytest.raises(ValueError, match="overwrite"):
                await srv._spawn_object_record("x_2", "aaa_towed", lat, lon,
                                               category="other")
            assert srv.backend.sim.object_geo("x_1") is None
            assert srv.backend.sim.object_geo("x_2") is None

            assert await srv.backend.destroy_object(RED_NAME) is True
            assert srv.backend.sim.object_geo(RED_NAME) is None
            assert await srv.backend.destroy_object(RED_NAME) is False
        asyncio.run(main())


# ------------------------------------------- sim_spawn_target guards -----

def test_a_duplicate_name_is_refused_and_the_first_target_kept(tmp_path):
    with wg_server(tmp_path) as srv:
        spawn = tool(srv, "sim_spawn_target")
        a = north_east(300, 0)
        b = north_east(-300, 0)
        first = asyncio.run(spawn(lat=a[0], lon=a[1], ob_class="supply_truck",
                                  name="truck_a"))
        assert first["status"] == "accepted"
        dup = asyncio.run(spawn(lat=b[0], lon=b[1], ob_class="supply_truck",
                                name="truck_a"))
        assert dup == {"rejected": True, "error": "duplicate_name",
                       "message": ("A target named 'truck_a' already exists; "
                                   "give another name or omit it.")}
        assert srv.targets["truck_a"]["lat"] == a[0]
        glat, glon, _ = srv.backend.sim.object_geo("truck_a")
        assert haversine_m(glat, glon, a[0], a[1]) < 5.0
        # An automatic label skips names already taken, rather than
        # overwriting them.
        asyncio.run(spawn(lat=b[0], lon=b[1], ob_class="supply_truck",
                          name="supply_truck_1"))
        auto = asyncio.run(spawn(lat=b[0] + 0.001, lon=b[1], ob_class="supply_truck"))
        assert auto["name"] == "supply_truck_2"


def test_scenario_names_and_positions_are_refused_during_a_session(tmp_path):
    with wg_server(tmp_path) as srv:
        spawn, move = tool(srv, "sim_spawn_target"), tool(srv, "sim_move_target")
        eng = srv.wargame = StubEngine(active=True)
        eng.names.add(RED_NAME)
        red = north_east(1200, 800)
        blue = north_east(-600, -400)
        eng.units = {"red-aaa-1": red, "blue-artillery-1": blue}

        owned = asyncio.run(spawn(lat=red[0] + 0.02, lon=red[1], name=RED_NAME,
                                  ob_class="aaa_towed"))
        assert owned["error"] == "scenario_name" and owned["rejected"] is True
        for unit, (dn, de) in ((red, (1200 + 100, 800)), (blue, (-600, -400 + 120))):
            near = asyncio.run(spawn(lat=north_east(dn, de)[0],
                                     lon=north_east(dn, de)[1],
                                     ob_class="supply_truck"))
            assert near == {"rejected": True, "error": "near_scenario_unit",
                            "message": ("That position is within 150 m of a "
                                        "simulated scenario unit; place it "
                                        "further away.")}
        far = north_east(1200 + 400, 800)
        isr = asyncio.run(spawn(lat=far[0], lon=far[1], ob_class="supply_truck"))
        assert isr["status"] == "accepted"
        moved = asyncio.run(move(target_id=RED_NAME, waypoints=[list(far)]))
        assert moved["error"] == "scenario_name"
        # An ISR object still moves, and outside a session nothing is refused.
        assert asyncio.run(move(target_id=isr["name"],
                                waypoints=[list(north_east(0, 500))]))["ok"] is True
        eng.active = False
        near_red = north_east(1200 + 100, 800)
        assert asyncio.run(spawn(lat=near_red[0], lon=near_red[1],
                                 ob_class="supply_truck"))["status"] == "accepted"
        assert len(audit(srv, "spawn_target_refused")) == 3


def test_red_truth_records_guard_even_without_the_engines_unit_near(tmp_path):
    """The server also checks red units' own truth records at their live sim
    position, so the 150 m clearance holds whichever side answers."""
    with wg_server(tmp_path) as srv:
        srv.wargame = StubEngine(active=True, unit_near=False)
        lat, lon = north_east(1000, -700)
        asyncio.run(srv._spawn_object_record(
            RED_NAME, "aaa_towed", lat, lon, provenance="scenario",
            session_id=SID, side="red", unit_id="red-aaa-1",
            designator="Red AAA 1"))
        near = north_east(1000 + 120, -700)
        assert srv._scenario_unit_near(*near) == "red-aaa-1"
        out = asyncio.run(tool(srv, "sim_spawn_target")(
            lat=near[0], lon=near[1], ob_class="supply_truck"))
        assert out["error"] == "near_scenario_unit"
        # A record of ANOTHER session is not this session's unit.
        srv.targets[RED_NAME]["session_id"] = "WG-other"
        assert srv._scenario_unit_near(*near) is None


# ------------------------------------ sim_spawn_order_of_battle skips ----

class _Site:
    def __init__(self, osm_id, name, lat, lon):
        self.osm_id, self.name, self.lat, self.lon = osm_id, name, lat, lon
        self.category, self.ob_class = "military_base", "c2_node"
        self.ob_source, self.position_source = "test", "test"

    def spawn_request(self):
        return {"ob_class": self.ob_class, "lat": self.lat, "lon": self.lon,
                "name": self.name}

    def alt_provenance(self):
        return {"alt_msl_m": None, "alt_source": "test", "alt_is_real": False}


class _Order:
    real = False

    def __init__(self, sites):
        self.sites = sites
        self.provenance = types.SimpleNamespace(as_dict=lambda: {"source": "test"})

    def by_category(self):
        return {"military_base": len(self.sites)}


def test_mapped_sites_never_take_a_scenario_name_or_crowd_a_unit(tmp_path):
    with wg_server(tmp_path) as srv:
        ob = tool(srv, "sim_spawn_order_of_battle")
        red = north_east(1500, 0)
        near, far = north_east(1500 + 100, 0), north_east(-1500, 0)
        sites = [_Site(1, RED_NAME, *far), _Site(2, "site_near", *near),
                 _Site(3, "site_far", *far)]
        srv.real_order_of_battle = lambda: _Order(sites)
        # ISR mode: nothing is skipped and the result has no `skipped` key.
        isr = asyncio.run(ob())
        assert isr["spawned"] == 3 and "skipped" not in isr
        for name in (RED_NAME, "site_near", "site_far"):
            srv.targets.pop(name)
            srv.backend.sim._objects.pop(name, None)
        eng = srv.wargame = StubEngine(active=True)
        eng.names.add(RED_NAME)
        eng.units = {"red-aaa-1": red}
        out = asyncio.run(ob())
        assert out["spawned"] == 1 and out["refused"] == 0 and out["ok"] is True
        assert [s["name"] for s in out["sites"]] == ["site_far"]
        assert out["skipped"] == [
            {"osm_id": 1, "name": RED_NAME, "category": "military_base",
             "reason": "scenario_name"},
            {"osm_id": 2, "name": "site_near", "category": "military_base",
             "reason": "near_scenario_unit"}]
        assert RED_NAME not in srv.targets and "site_near" not in srv.targets


# ---------------------------------------------------- the ingest hook ----

def test_a_scenario_contact_is_tagged_and_kept_out_of_the_pattern_of_life(tmp_path):
    with wg_server(tmp_path) as srv:
        eng = srv.wargame = StubEngine(active=True)
        eng.names.add(RED_NAME)
        observed: list[str] = []
        real_observe = srv.pol.observe_track

        def spy(track, *a, **k):
            observed.append(track.name)
            return real_observe(track, *a, **k)

        srv.pol.observe_track = spy

        async def main():
            rlat, rlon = north_east(70, 0)
            await srv._spawn_object_record(
                RED_NAME, "aaa_towed", rlat, rlon, provenance="scenario",
                session_id=SID, side="red", unit_id="red-aaa-1",
                designator="Red AAA 1")
            ilat, ilon = north_east(-100, 0)      # > 150 m from the red unit
            out = await tool(srv, "sim_spawn_target")(lat=ilat, lon=ilon,
                                                      ob_class="supply_truck")
            assert out["status"] == "accepted", out
            await tool(srv, "uav_get_detections")(vehicle="Drone1")
            return out["name"]

        isr_name = asyncio.run(main())
        by_name = {t.name: t for t in srv.tracks.tracks()}
        assert set(by_name) >= {RED_NAME, isr_name}
        red, isr = by_name[RED_NAME], by_name[isr_name]
        assert red.scenario is True and isr.scenario is False
        assert eng.noted == [red.track_id]
        assert observed == [isr_name]               # the scenario track skipped
        # The flag is persisted with the scenario track only.
        assert srv.store.tracks.get(red.track_id)["scenario"] is True
        assert "scenario" not in srv.store.tracks.get(isr.track_id)


def test_outside_a_session_every_contact_is_an_isr_contact(tmp_path):
    with wg_server(tmp_path) as srv:
        async def main():
            lat, lon = north_east(70, 0)
            await tool(srv, "sim_spawn_target")(lat=lat, lon=lon,
                                                ob_class="aaa_towed",
                                                name=RED_NAME)
            return await tool(srv, "uav_get_detections")(vehicle="Drone1")

        asyncio.run(main())
        [t] = [t for t in srv.tracks.tracks() if t.name == RED_NAME]
        assert t.scenario is False
        assert "scenario" not in srv.store.tracks.get(t.track_id)


def test_an_engine_hook_that_raises_never_breaks_isr_ingest(tmp_path):
    class Broken(StubEngine):
        def owns_name(self, name):
            raise RuntimeError("engine down")

    with wg_server(tmp_path) as srv:
        srv.wargame = Broken(active=True)

        async def main():
            lat, lon = north_east(70, 0)
            await tool(srv, "sim_spawn_target")(lat=lat, lon=lon,
                                                ob_class="supply_truck")
            return await tool(srv, "uav_get_detections")(vehicle="Drone1")

        scan = asyncio.run(main())
        assert scan["detections"]
        assert all(t.scenario is False for t in srv.tracks.tracks())
        assert len(audit(srv, "wargame_hook_failed")) == 1     # rate-limited


# ------------------------------- ISR texts hold during a session (§6) ----

def test_threat_and_deconflict_isr_only_are_unchanged_during_a_session(tmp_path):
    """WG §6: the threat and deconflict `isr_only` pins hold in a session,
    on a scenario contact too (aircraft never deliver effects)."""
    from godseye_uav import threat

    class Traffic:
        real, contacts = False, []

        def deconflict(self, *a, **k):
            return []

        def as_dict(self):
            return {"count": 0}

    with wg_server(tmp_path) as srv:
        eng = srv.wargame = StubEngine(active=True)
        eng.names.add(RED_NAME)
        srv.real_traffic = lambda: Traffic()

        async def main():
            lat, lon = north_east(70, 0)
            await srv._spawn_object_record(
                RED_NAME, "aaa_towed", lat, lon, provenance="scenario",
                session_id=SID, side="red", unit_id="red-aaa-1",
                designator="Red AAA 1")
            await tool(srv, "uav_get_detections")(vehicle="Drone1")
            [t] = [t for t in srv.tracks.tracks() if t.name == RED_NAME]
            one = await tool(srv, "uav_assess_threat")(vehicle="Drone1",
                                                       track_id=t.track_id)
            area = await tool(srv, "mission_threat_assessment")(vehicle="Drone1",
                                                                dry_run=True)
            dec = await tool(srv, "uav_deconflict_airspace")(vehicle="Drone1")
            return t, one, area, dec

        t, one, area, dec = asyncio.run(main())
        assert t.scenario is True
        assert one["isr_only"] is True
        assert one["authority"] == threat.ISR_AUTHORITY_NOTE
        assert area["isr_only"] == ("M14: sensor-posture advice only; no "
                                    "engagement recommendation is produced")
        assert dec["isr_only"] == ("M14: these are airspace contacts to "
                                   "deconflict against. No engagement "
                                   "recommendation is produced.")
