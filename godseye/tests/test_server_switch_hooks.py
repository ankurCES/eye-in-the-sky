"""Server hooks for runtime theaters and sim speed (WG v2 §3.10, §4.1.6, A6a).

What these tests guard (the A6a acceptance list, §4.3):

* §3.10 hooks exist with their defaults; `airframe=` and `geodata=` kwargs;
  mapped sites come from the cache only; `resolve_real_data("direct")`.
* `set_time_scale` moves the fake and every fuel clock, is audited, and is
  refused under real AirSim; the monitor sleeps `max(0.05, interval / scale)`.
* §4.1.9 #3 (server side): `UavBackend.relocate`/`home_rpc` agree with the
  sim and every origin holder; a failed cross-check refuses every `_submit`.
* §4.1.9 #10: a switch completing while `_gate` awaits telemetry makes the
  submit return `theater_changed` (fly_route, goto, RTH and `_launch_mission`).
* §4.1.9 #11: real data never crosses a switch; `stop_real_data()` never waits.
* §4.1.9 #13 / scale test 8: at x10 the monitor catches a raw 20 m/s dash at
  the fence and commits the forced RTB within 100 m of it.

No test touches the network. Fake sims bind 53100-53149 only (A6a's range).
"""
from __future__ import annotations

import asyncio
import itertools
import math
import os
import pathlib
import threading
import time
from contextlib import contextmanager, suppress
from types import SimpleNamespace

import airsim
import pytest
from godseye_uav import geo_http, sites, theaters
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint, canonical_altitude
from godseye_uav.realdata import HttpResponse
from godseye_uav.safety import DEFAULT_AIRFRAME_ID, SafetyEnvelope, haversine_m
from godseye_uav.server import (
    DEFAULT_TICK_S,
    GEODATA_ENV,
    REAL_DATA_ENV,
    SWITCH_RUNNING_MESSAGE,
    THEATER_CHANGED_MESSAGE,
    GodseyeUavServer,
    UavBackend,
    resolve_geodata,
    resolve_real_data,
)
from godseye_uav.store import Store

_PORTS = list(range(53100, 53150))
_PORT = itertools.cycle(_PORTS[os.getpid() % len(_PORTS):]
                        + _PORTS[:os.getpid() % len(_PORTS)])

THEATER = theaters.get("default")
HOME = GeoPoint(THEATER.home_lat, THEATER.home_lon,
                canonical_altitude(THEATER.home_alt_msl_m, THEATER.home_lat,
                                   THEATER.home_lon, datum="msl").alt_hae)
#: Kherson, far enough that a stale origin copy is kilometres off.
FAR = (46.6354, 32.6169, 50.0)


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
    raise RuntimeError(f"no free port in 53100-53149: {last}")


@contextmanager
def hooks_server(tmp_path, *, with_sim: bool = True, store=None, **kw):
    """A server on a fake sim. `with_sim=False` hides the fake's direct handle,
    which is exactly how a real-AirSim backend looks to the server."""
    sim = _start_sim()
    srv = None
    store = store if store is not None else Store(tmp_path)
    try:
        client = airsim.MultirotorClient(port=sim.port)
        client.confirmConnection()
        backend = UavBackend(client, HOME, sim=sim if with_sim else None)
        kw.setdefault("envelope", SafetyEnvelope(**THEATER.envelope_kwargs()))
        srv = GodseyeUavServer(backend, store, theater=THEATER, watchdog_s=30.0, **kw)
        srv.sim = sim
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


def tool(srv, name):
    return srv.mcp._tool_manager._tools[name].fn


def audits(srv, kind: str) -> list[dict]:
    return [r for r in srv.store.audit.read_all() if r.get("kind") == kind]


def emulate_switch(srv, *, theater=None) -> None:
    """What `theater_switch.switch` does to the hooks (§4.1.3): flag set under
    `_mode_lock`, theater and epoch moved, flag cleared."""
    with srv._mode_lock:
        srv._switching.set()
    try:
        if theater is not None:
            srv.theater = theater
        srv.theater_epoch += 1
        srv.real_world = None
    finally:
        srv._switching.clear()


# ------------------------------------------------------------ §3.10 hooks --

def test_the_switch_hooks_exist_with_their_defaults(tmp_path):
    with hooks_server(tmp_path) as srv:
        assert isinstance(srv._mode_lock, type(threading.Lock()))
        assert isinstance(srv._switching, threading.Event) and not srv._switching.is_set()
        assert srv._ticks_inflight == 0
        assert srv.theater_epoch == 0
        assert srv.theater_set_at_ms is None and srv.theater_set_via is None
        assert srv.theater_previous is None and srv.theater_integrity_error is None
        assert srv.origin_holders == [] and srv.theater_listeners == []
        assert isinstance(srv.boot_recovery_done, threading.Event)
        assert not srv.boot_recovery_done.is_set()   # A6b sets it
        assert srv.airframe_id == DEFAULT_AIRFRAME_ID
        assert srv.time_scale == 1.0
        assert srv.geodata_enabled is False          # in-process default: offline
        assert srv.monitor_period_s == DEFAULT_TICK_S
        assert isinstance(srv.sites, sites.SiteSet)
        assert srv.sites.total == 0 and srv.sites.reason == sites.GEODATA_OFF_REASON
        assert srv.geo_cache.dir == tmp_path / "geodata-cache" / "geodata"


def test_an_in_memory_store_keeps_the_geodata_cache_in_memory(tmp_path):
    with hooks_server(tmp_path, store=Store(":memory:"), geodata=True) as srv:
        assert srv.geo_cache.dir is None
        assert srv.sites.total == 0 and srv.sites.degraded


def test_the_airframe_kwarg_prices_every_new_fuel_model(tmp_path):
    with hooks_server(tmp_path, airframe="group3_fixed_wing") as srv:
        assert srv.airframe_id == "group3_fixed_wing"
        assert srv.monitor_for("Drone9").fuel.airframe.id == "group3_fixed_wing"
    with pytest.raises(ValueError), hooks_server(tmp_path, airframe="no_such_airframe"):
        pass


def test_resolve_geodata_switch():
    assert resolve_geodata(True) is True and resolve_geodata(False) is False
    assert resolve_geodata("on") is True and resolve_geodata("OFF") is False
    with pytest.raises(ValueError):
        resolve_geodata("maybe")
    with pytest.raises(ValueError):
        resolve_geodata(1.5)


def test_the_geodata_env_is_read_when_the_kwarg_is_absent(monkeypatch):
    monkeypatch.delenv(GEODATA_ENV, raising=False)
    assert resolve_geodata(None) is False
    monkeypatch.setenv(GEODATA_ENV, "on")
    assert resolve_geodata(None) is True
    monkeypatch.setenv(GEODATA_ENV, "on please")
    with pytest.raises(ValueError) as exc:
        resolve_geodata(None)
    assert GEODATA_ENV in str(exc.value)


def test_geodata_on_serves_the_theaters_sites_from_the_cache_only(tmp_path):
    """Sites are read from `<store>/geodata-cache` at construction and the
    network is never asked (the egress guard would refuse it anyway)."""
    fixture = (pathlib.Path(__file__).parent / "fixtures" / "geodata"
               / "overpass_sites.json").read_text(encoding="utf-8")
    calls = []

    def overpass(url, timeout_s, data=None):
        calls.append(url)
        return HttpResponse(200, fixture)

    seeded = sites.fetch_sites(THEATER.bbox(), fetch=overpass,
                               cache=geo_http.default_cache(tmp_path / "geodata-cache"))
    assert calls and seeded.total > 0 and not seeded.degraded
    with hooks_server(tmp_path, geodata=True) as srv:
        assert srv.geodata_enabled is True
        assert srv.sites.total == seeded.total and not srv.sites.degraded
        assert srv.sites.counts() == seeded.counts()
    assert len(calls) == 1                     # construction fetched nothing
    with hooks_server(tmp_path, geodata=False) as srv:   # off wins over the cache
        assert srv.sites.total == 0 and srv.sites.reason == sites.GEODATA_OFF_REASON


def test_geodata_on_with_an_empty_cache_is_an_empty_degraded_set(tmp_path):
    with hooks_server(tmp_path, geodata=True) as srv:
        assert srv.sites.total == 0 and srv.sites.degraded
        assert srv.sites.reason != sites.GEODATA_OFF_REASON


def test_real_data_direct_builds_the_direct_client(monkeypatch):
    """§4.1.5 wiring: `"direct"` and `GODSEYE_REAL_DATA=direct` both give
    `default_client(fallback_ground_msl_m=None, direct=True)`."""
    client = resolve_real_data("direct")
    assert client.origin == "direct" and client.terrain is not None
    assert resolve_real_data(" Direct ").origin == "direct"
    monkeypatch.setenv(REAL_DATA_ENV, "direct")
    assert resolve_real_data(None).origin == "direct"
    monkeypatch.setenv(REAL_DATA_ENV, "directly")
    with pytest.raises(ValueError):
        resolve_real_data(None)
    with pytest.raises(ValueError):
        resolve_real_data("gev please")


# ------------------------------------------------------------ sim speed ---

def test_set_time_scale_moves_the_fake_and_every_fuel_clock(tmp_path):
    with hooks_server(tmp_path) as srv:
        before = srv.monitor_for("Drone1").fuel
        out = srv.set_time_scale(5)
        assert out == {"scale": 5.0, "previous": 1.0}
        assert srv.time_scale == 5.0 and srv.sim.time_scale == 5.0
        assert srv.fuel_for("Drone1") is before and before.time_scale == 5.0
        assert srv.monitor_for("Drone7").fuel.time_scale == 5.0   # inherited
        assert srv.sim.environment()["time_scale"] == 5.0
        row = audits(srv, "time_scale_changed")[-1]
        assert row["scale"] == 5.0 and row["previous"] == 1.0
        assert srv.set_time_scale(1.0) == {"scale": 1.0, "previous": 5.0}


@pytest.mark.parametrize("bad", [0.5, 10.5, 0, -2, float("nan"), True, "fast"])
def test_set_time_scale_out_of_range_changes_nothing(tmp_path, bad):
    with hooks_server(tmp_path) as srv:
        srv.set_time_scale(3)
        with pytest.raises(ValueError):
            srv.set_time_scale(bad)
        assert srv.time_scale == 3.0 and srv.sim.time_scale == 3.0
        assert srv.fuel_for("Drone1").time_scale == 3.0


def test_set_time_scale_is_refused_under_real_airsim(tmp_path):
    with hooks_server(tmp_path, with_sim=False) as srv:
        assert srv.backend.sim is None
        with pytest.raises(ValueError) as exc:
            srv.set_time_scale(2)
        assert "real AirSim" in str(exc.value)
        assert srv.time_scale == 1.0 and srv.sim.time_scale == 1.0
        assert not audits(srv, "time_scale_changed")


def test_the_monitor_pass_shrinks_with_sim_speed(tmp_path):
    """§4.1.6: sleep `max(0.05, interval_s / time_scale)`; the measured wall
    period of a pass is published as `monitor_period_s`."""
    with hooks_server(tmp_path) as srv:
        assert srv._monitor_sleep_s(0.5) == 0.5
        srv.set_time_scale(4)
        assert srv._monitor_sleep_s(0.5) == pytest.approx(0.125)
        srv.set_time_scale(10)
        assert srv._monitor_sleep_s(0.5) == pytest.approx(0.05)
        assert srv._monitor_sleep_s(0.2) == pytest.approx(0.05)   # the floor
        srv.boot_recovery_done.set()
        srv.start_monitor(["Drone1"], interval_s=DEFAULT_TICK_S)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and srv.monitor_period_s == DEFAULT_TICK_S:
            time.sleep(0.05)
        srv.stop_monitor()
        assert 0.04 <= srv.monitor_period_s < 0.3, srv.monitor_period_s


# --------------------------------------- §4.1.9 #3: the origin copies -----

class Holder:
    """A minimal origin holder (the bridge's AirSimAdapter shape)."""

    def __init__(self, home: GeoPoint) -> None:
        self.home_geo = home

    def relocate(self, new_home: GeoPoint) -> None:
        self.home_geo = GeoPoint(new_home.latitude, new_home.longitude, new_home.altitude)


def _same(a: GeoPoint, b: GeoPoint) -> bool:
    return (abs(a.latitude - b.latitude) < 1e-9 and abs(a.longitude - b.longitude) < 1e-9
            and abs(a.altitude - b.altitude) < 1e-3)


def test_every_origin_copy_and_get_home_geo_point_agree_after_a_move(tmp_path):
    """Steps 2a-2e: T1 once, then sim, backend and holders move; the sim's own
    `getHomeGeoPoint` (`home_rpc`) agrees with every in-process copy."""
    with hooks_server(tmp_path) as srv:
        holder = Holder(srv.backend.home_geo)
        srv.attach_origin_holder(holder)
        srv.attach_origin_holder(holder)             # idempotent
        assert srv.origin_holders == [holder]

        async def main():
            assert _same(await srv.backend.home_rpc(), srv.backend.home_geo)
            fix = canonical_altitude(FAR[2], FAR[0], FAR[1], datum="msl")
            new_home = GeoPoint(FAR[0], FAR[1], fix.alt_hae)
            srv.sim.relocate_origin(new_home)
            srv.backend.relocate(new_home, fix)
            for h in srv.origin_holders:
                h.relocate(new_home)
            copies = [srv.backend.home_geo, srv.backend.home.geo,
                      srv.backend.home_declared, holder.home_geo,
                      await srv.backend.home_rpc()]
            assert all(_same(c, new_home) for c in copies), copies
            assert srv.backend.home_fix is fix
            tele = await srv.backend.telemetry("Drone1")   # parked at the new home
            assert haversine_m(tele["lat"], tele["lon"], FAR[0], FAR[1]) < 1.0
            assert tele["alt_msl_m"] == pytest.approx(FAR[2], abs=0.5)
        run(main())


def test_a_bad_relocation_leaves_the_backend_origin_in_place(tmp_path):
    with hooks_server(tmp_path) as srv:
        before = srv.backend.home_geo
        fix = srv.backend.home_fix
        for bad in (GeoPoint(float("nan"), 1.0, 0.0), GeoPoint(91.0, 1.0, 0.0),
                    GeoPoint(1.0, 1.0, float("inf"))):
            with pytest.raises(ValueError):
                srv.backend.relocate(bad, fix)
            assert srv.backend.home_geo is before


def test_an_origin_holder_must_have_both_halves(tmp_path):
    with hooks_server(tmp_path) as srv:
        with pytest.raises(TypeError):
            srv.attach_origin_holder(object())
        with pytest.raises(TypeError):
            srv.attach_origin_holder(type("NoHome", (), {"relocate": lambda s, g: None})())
        assert srv.origin_holders == []


GATED = ("uav_goto_gps", "uav_fly_route", "uav_return_to_home", "mission_recon_route")


async def _call(srv, name: str, **extra) -> dict:
    lat, lon = THEATER.home_lat + 0.002, THEATER.home_lon + 0.002
    args = {
        "uav_takeoff": {}, "uav_land": {}, "uav_hover": {},
        "uav_goto_gps": {"lat": lat, "lon": lon, "alt_agl_m": 30.0},
        "uav_fly_route": {"waypoints": [{"lat": lat, "lon": lon, "alt_agl_m": 30.0}]},
        "uav_return_to_home": {},
        "mission_recon_route": {"waypoints": [
            {"lat": THEATER.home_lat, "lon": THEATER.home_lon},
            {"lat": lat, "lon": lon}], "alt_agl_m": 40.0},
    }[name]
    return await tool(srv, name)(vehicle="Drone1", **args, **extra)


def _nothing_queued(srv) -> bool:
    st = srv.tasking.status("Drone1")
    return st["current"] is None and not st["pending"]


def test_a_failed_origin_cross_check_refuses_every_submit_but_land_and_hover(tmp_path):
    """Review A: land and hover never use the origin, so they pass the latch
    (an operator can always land); everything that flies somewhere is refused."""
    with hooks_server(tmp_path) as srv:
        srv.theater_integrity_error = "sim origin 3.1 km from the backend's"

        async def main():
            for name in ("uav_takeoff", *GATED):
                out = await _call(srv, name)
                assert out.get("rejected") is True, (name, out)
                assert out["error"] == "theater_integrity", (name, out)
                assert "3.1 km" in out["message"]
            assert _nothing_queued(srv)
        run(main())
        rows = audits(srv, "submit_refused")
        assert len(rows) == 1 + len(GATED)
        assert {r["error"] for r in rows} == {"theater_integrity"}
        for name in ("uav_land", "uav_hover"):
            assert srv._submit_refusal(None, name) is None
            assert srv._submit_refusal(7, name)["error"] == "theater_changed"


def test_submit_is_refused_while_a_switch_runs(tmp_path):
    with hooks_server(tmp_path) as srv:
        srv._switching.set()
        out = srv._submit("Drone1", "uav_hover", {}, None)
        assert out == {"rejected": True, "error": "theater_changed",
                       "message": SWITCH_RUNNING_MESSAGE,
                       "rejected_tool": "uav_hover", "vehicle": "Drone1"}
        assert _nothing_queued(srv)
        srv._switching.clear()
        assert srv._submit("Drone1", "uav_hover", {}, None)["status"] == "accepted"


def test_gate_epoch_must_be_current(tmp_path):
    with hooks_server(tmp_path) as srv:
        srv.theater_epoch = 4
        stale = srv._submit("Drone1", "uav_hover", {}, None, gate_epoch=3)
        assert stale["error"] == "theater_changed"
        assert stale["message"] == THEATER_CHANGED_MESSAGE
        assert _nothing_queued(srv)
        ok = srv._submit("Drone1", "uav_hover", {}, None, gate_epoch=4)
        assert ok["status"] == "accepted"


# -------------------------------------------- §4.1.9 #10: switch TOCTOU ---

def test_every_gate_records_the_epoch_it_checked_under(tmp_path):
    with hooks_server(tmp_path) as srv:
        srv.theater_epoch = 7

        async def main():
            gate = await srv._gate("Drone1", [{"lat": THEATER.home_lat,
                                               "lon": THEATER.home_lon,
                                               "alt_m": 20.0}], 5.0)
            assert gate["ok"] and gate["theater_epoch"] == 7
            srv._telemetry = _raising_telemetry
            bad = await srv._gate("Drone1", [], 5.0)
            assert bad["ok"] is False and bad["theater_epoch"] == 7
        run(main())


async def _raising_telemetry(vehicle):
    raise RuntimeError("link down")


@pytest.mark.parametrize("name", GATED)
def test_a_switch_completing_during_the_gate_refuses_the_submit(tmp_path, name):
    """A spy `_telemetry` awaits while a switch completes: the plan was checked
    against the old theater, so the submit returns `theater_changed`."""
    with hooks_server(tmp_path) as srv:
        real = srv._telemetry
        switched = []

        async def spy(vehicle):
            if not switched:
                await asyncio.sleep(0.05)
                emulate_switch(srv)
                switched.append(srv.theater_epoch)
            return await real(vehicle)

        srv._telemetry = spy

        async def main():
            out = await _call(srv, name)
            assert switched == [1]
            assert out.get("rejected") is True, out
            assert out["error"] == "theater_changed", out
            assert out["message"] == THEATER_CHANGED_MESSAGE
            assert _nothing_queued(srv)
            # planned again under the new epoch, it goes through
            again = await _call(srv, name, idempotency_key=f"again-{name}")
            assert again.get("status") == "accepted", again
        run(main())
        assert audits(srv, "submit_refused")[-1]["gate_epoch"] == 0


def test_a_dry_run_is_never_refused_by_the_epoch(tmp_path):
    with hooks_server(tmp_path) as srv:
        async def main():
            out = await _call(srv, "mission_recon_route", dry_run=True)
            assert out["dry_run"] is True and out["gate"]["theater_epoch"] == 0
        run(main())


# ----------------------------------------------- no tick straddles a switch -

def test_tick_once_is_skipped_while_a_switch_runs(tmp_path):
    with hooks_server(tmp_path) as srv:
        srv._switching.set()
        assert run(srv.tick_once("Drone1")) == {"skipped": "theater switch"}
        assert "Drone1" not in srv.ticks and srv._ticks_inflight == 0
        assert not list(srv.store.fuel.read_all())
        srv._switching.clear()
        assert run(srv.tick_once("Drone1"))["telemetry"] is True


def test_a_tick_in_flight_finishes_before_the_switch_proceeds(tmp_path):
    """A spy tick sleeping across the switch call finishes first; a tick that
    starts after the flag is set does not run at all."""
    with hooks_server(tmp_path) as srv:
        real = srv._telemetry
        started: list[asyncio.Event] = []

        async def slow(vehicle):
            started[0].set()
            await asyncio.sleep(0.3)
            return await real(vehicle)

        srv._telemetry = slow

        async def main():
            started.append(asyncio.Event())
            tick = asyncio.ensure_future(srv.tick_once("Drone1"))
            await started[0].wait()
            assert srv._ticks_inflight == 1
            with srv._mode_lock:
                srv._switching.set()
            assert await srv.tick_once("Drone2") == {"skipped": "theater switch"}
            assert await srv.wait_ticks_idle(0.05) is False     # still sleeping
            t0 = time.monotonic()
            assert await srv.wait_ticks_idle(5.0) is True
            assert tick.done() and (await tick)["telemetry"] is True
            assert time.monotonic() - t0 < 1.0
            assert srv._ticks_inflight == 0
            srv._switching.clear()

        run(main())


# --------------------------- §4.1.9 #11: real data never crosses a switch --

class SlowReal:
    """A duck-typed real-data client whose hydration blocks until released."""

    origin = "fake"

    def __init__(self) -> None:
        self.terrain = SimpleNamespace(cache_size=lambda: 0, requests=0)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[dict] = []

    def hydrate_theater(self, **kw):
        self.calls.append(kw)
        self.entered.set()
        self.release.wait(10.0)
        return SimpleNamespace(theater_id=kw["theater_id"], n=len(self.calls))


OTHER = theaters.get("iran-isfahan")


def _wait_for(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_a_slow_background_hydration_finishing_after_a_switch_is_dropped(tmp_path):
    slow = SlowReal()
    with hooks_server(tmp_path, real_data=slow) as srv:
        before = (theaters.real_data(THEATER.id), theaters.real_data(OTHER.id))
        try:
            srv.start_real_data(load_ao_terrain=False)
            assert slow.entered.wait(5.0)
            assert slow.calls[0]["theater_id"] == THEATER.id
            emulate_switch(srv, theater=OTHER)
            slow.release.set()
            assert _wait_for(lambda: audits(srv, "real_data_stale_dropped"))
            assert srv.real_world is None
            assert (theaters.real_data(THEATER.id),
                    theaters.real_data(OTHER.id)) == before
            row = audits(srv, "real_data_stale_dropped")[-1]
            assert (row["theater_id"], row["epoch"]) == (THEATER.id, 0)
            assert (row["current_theater_id"], row["current_epoch"]) == (OTHER.id, 1)
            assert row["source"] == "refresher"
        finally:
            slow.release.set()
            srv.stop_real_data()


def test_stop_real_data_never_waits_for_a_hydration_in_flight(tmp_path):
    slow = SlowReal()
    with hooks_server(tmp_path, real_data=slow) as srv:
        try:
            srv.start_real_data(load_ao_terrain=False)
            assert slow.entered.wait(5.0)
            t0 = time.perf_counter()
            srv.stop_real_data()
            assert time.perf_counter() - t0 < 0.05
            assert srv._real_refresher is None
            slow.release.set()
            time.sleep(0.2)
            assert srv.real_world is None          # stopped: never published
        finally:
            slow.release.set()


def test_publish_is_gated_on_the_theater_and_epoch_it_was_started_for(tmp_path):
    slow = SlowReal()
    slow.release.set()
    with hooks_server(tmp_path, real_data=slow) as srv:
        try:
            refresher = srv.start_real_data(interval_s=3600.0, load_ao_terrain=False)
            assert _wait_for(lambda: srv.real_world is not None)
            assert theaters.real_data(THEATER.id) is srv.real_world
            publish = refresher.on_result
            fresh = SimpleNamespace(theater_id=THEATER.id, n=99)
            with srv._mode_lock:
                srv._switching.set()
            publish(fresh)                          # mid-switch: dropped
            assert srv.real_world is not fresh
            srv._switching.clear()
            publish(fresh)                          # same theater and epoch: kept
            assert srv.real_world is fresh
            srv.theater_epoch += 1                  # same theater, new epoch
            publish(SimpleNamespace(theater_id=THEATER.id, n=100))
            assert srv.real_world is fresh
            assert len(audits(srv, "real_data_stale_dropped")) == 2
            # a restarted refresher is the only one, bound to the new epoch
            again = srv.start_real_data(interval_s=3600.0, load_ao_terrain=False)
            assert again is not refresher and srv._real_refresher is again
        finally:
            srv.stop_real_data()
            theaters.clear_hydration(THEATER.id)


def test_a_blocking_hydrate_that_straddles_a_switch_is_refused(tmp_path):
    slow = SlowReal()
    with hooks_server(tmp_path, real_data=slow) as srv:
        before = theaters.real_data(OTHER.id)
        out: dict = {}

        def worker():
            try:
                srv.hydrate_real_data(load_ao_terrain=False)
            except Exception as exc:  # noqa: BLE001 — the assertion reads it
                out["exc"] = exc

        th = threading.Thread(target=worker, daemon=True)
        try:
            th.start()
            assert slow.entered.wait(5.0)
            emulate_switch(srv, theater=OTHER)
            slow.release.set()
            th.join(5.0)
            assert isinstance(out.get("exc"), RuntimeError), out
            assert "theater changed" in str(out["exc"])
            assert srv.real_world is None
            assert theaters.real_data(OTHER.id) is before
            assert audits(srv, "real_data_stale_dropped")[-1]["source"] == "hydrate"
        finally:
            slow.release.set()
            theaters.clear_hydration(THEATER.id)


# ------------------------------------------- §4.1.9 #13: scale test 8 -----

def _box_fence(half_m: float) -> list[tuple[float, float]]:
    lat0, lon0 = THEATER.home_lat, THEATER.home_lon
    dlat = half_m / 111_320.0
    dlon = half_m / (111_320.0 * math.cos(math.radians(lat0)))
    return [(lat0 - dlat, lon0 - dlon), (lat0 - dlat, lon0 + dlon),
            (lat0 + dlat, lon0 + dlon), (lat0 + dlat, lon0 - dlon)]


def test_scale8_at_x10_the_monitor_catches_a_raw_dash_at_the_fence(tmp_path):
    """At x10 a drone commanded straight at the fence at 20 m/s by a raw RPC
    (no gate) covers 200 m per wall second. The monitor, sleeping
    `max(0.05, 0.5 / 10)`, catches it and commits the forced RTB before it is
    `geofence_warn_m` (100 m) outside. At the unscaled 0.5 s it could be ~100 m
    past the fence before the next pass even looked."""
    env = SafetyEnvelope(**{**THEATER.envelope_kwargs(), "geofence": _box_fence(400.0)})
    with hooks_server(tmp_path, envelope=env) as srv:
        commits: list[dict] = []
        real_force_rtb = srv._force_rtb

        async def spy(vehicle, reason, detail="", mission_status=None):
            if reason == "geofence" and not commits:
                v = srv.sim._vehicles[vehicle]
                with srv.sim._lock:
                    n, e, d = v.ned.x, v.ned.y, v.ned.z
                lat, lon, _ = srv.backend.ned_to_llh(n, e, d)
                commits.append({"margin_m": srv.envelope.geofence_margin_m(lat, lon),
                                "period_s": srv.monitor_period_s})
            return await real_force_rtb(vehicle, reason, detail, mission_status)

        srv._force_rtb = spy
        srv.set_time_scale(10)
        srv.boot_recovery_done.set()
        raw = airsim.MultirotorClient(port=srv.sim.port)
        raw.confirmConnection()
        raw.enableApiControl(True)
        raw.armDisarm(True)
        raw.takeoffAsync().join()
        srv.start_monitor(["Drone1"], interval_s=DEFAULT_TICK_S)
        raw.moveToPositionAsync(3000.0, 0.0, -20.0, 20.0)     # due north, 3 km
        assert _wait_for(lambda: commits, timeout_s=30.0), "no forced RTB"
        srv.stop_monitor()
        assert srv.envelope.geofence_warn_m == 100.0
        assert commits[0]["margin_m"] > -srv.envelope.geofence_warn_m, commits
        assert commits[0]["period_s"] < 0.25, commits
        assert srv._rtb_active.get("Drone1") == "geofence"
