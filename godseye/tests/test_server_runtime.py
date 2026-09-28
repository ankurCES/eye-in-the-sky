"""Restart recovery and the monitor roster across runtime theaters (WG v2
§4.1.4, §4.1.9 #1 #9 #12; unit A6b).

What these tests guard (the A6b acceptance list, §4.3):

* D7 #1 — the boot RESUME re-gate. (a) An interrupted `uav_fly_route` that is
  out of the envelope is NOT resubmitted: `_force_rtb(restart_recovery)` and a
  `restart_resume_regate_failed` row instead. (b) A row planned in
  `dyn-x` and booted at `default` gives ABORT_RTH ("planned in X, booted in
  Y"). (c) An in-envelope route resumes, submitted with the gate's epoch.
  (d) A legacy row (no `theater`) is re-gated, never aborted for a mismatch.
* §4.1.4 journal: task rows (`_submit`, `_force_rtb`, the resume) carry
  `theater` and `theater_epoch`; fuel rows carry `airframe` too; the replay
  hands the theater to every boot action.
* §4.1.4 fuel restore: a row flown by another airframe starts a full tank
  and is audited `fuel_restore_airframe_mismatch`; a matching row and a
  legacy row are restored.
* D7 #12 — `boot_recovery_done` is set when recovery finishes (also when it
  raises), and an `/mcp` `sim_set_theater` before that is refused with
  "restart recovery still running", then accepted.
* D7 #9 — the monitor re-lists the roster every 20 passes: a Drone2 created
  after boot is ticked within 20 passes.

No test touches the network. Fake sims bind 53150-53199 only (A6b's range).
"""
from __future__ import annotations

import asyncio
import itertools
import os
import shutil
import threading
import time
from contextlib import contextmanager, suppress

import airsim
import pytest
from godseye_uav import theaters
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint, canonical_altitude
from godseye_uav.safety import DEFAULT_AIRFRAME_ID, SafetyEnvelope
from godseye_uav.server import (
    RESUME_NO_GATE_TOOLS,
    ROSTER_REFRESH_PASSES,
    THEATER_CHANGED_MESSAGE,
    GodseyeUavServer,
    UavBackend,
)
from godseye_uav.store import ABORT_RTH, RESUME, Store

_PORTS = list(range(53150, 53200))
_PORT = itertools.cycle(_PORTS[os.getpid() % len(_PORTS):]
                        + _PORTS[:os.getpid() % len(_PORTS)])

THEATER = theaters.get("default")
GROUP3 = "group3_fixed_wing"

#: Inside the default (Redmond) AO, a few hundred metres from home.
IN_AO_ROUTE = [{"lat": 47.6430, "lon": -122.1390, "alt_agl_m": 40.0},
               {"lat": 47.6445, "lon": -122.1375, "alt_agl_m": 40.0}]
#: 6.5 km north of home: outside the default AO's north edge (47.6615).
OUT_OF_AO_ROUTE = [{"lat": 47.7000, "lon": -122.1400, "alt_agl_m": 40.0}]


def home_of(t: theaters.Theater) -> GeoPoint:
    """The theater's home in HAE (T1), as the host builds it."""
    fix = canonical_altitude(t.home_alt_msl_m, t.home_lat, t.home_lon, datum="msl")
    return GeoPoint(t.home_lat, t.home_lon, fix.alt_hae)


def _start_sim(home: GeoPoint) -> FakeAirSim:
    last = None
    for _ in range(len(_PORTS)):
        sim = FakeAirSim(home=home, port=next(_PORT))
        try:
            sim.start()
            return sim
        except OSError as exc:  # taken by a concurrent run: try the next one
            last = exc
            with suppress(Exception):
                sim.stop()
    raise RuntimeError(f"no free port in 53150-53199: {last}")


@contextmanager
def runtime_server(root, *, theater=THEATER, **kw):
    """A server on a fake sim whose origin is `theater`'s home, over the
    store at `root` (which a test may have seeded, as a crashed run would)."""
    home = home_of(theater)
    sim = _start_sim(home)
    store = Store(root)
    srv = None
    try:
        client = airsim.MultirotorClient(port=sim.port)
        client.confirmConnection()
        backend = UavBackend(client, home, sim=sim)
        kw.setdefault("envelope", SafetyEnvelope(**theater.envelope_kwargs()))
        srv = GodseyeUavServer(backend, store, theater=theater, watchdog_s=30.0, **kw)
        srv.sim = sim
        yield srv
    finally:
        if srv is not None:
            srv.stop_monitor()
            with suppress(Exception):
                srv.tasking.shutdown()
        store.close()
        sim.stop()


def seed_task(root, task_id: str, tool: str, params: dict, *, vehicle="Drone1",
              theater: str | None = "default", epoch: int | None = 0,
              fuel_pct: float = 88.0, airframe: str | None = DEFAULT_AIRFRAME_ID) -> None:
    """Journal an interrupted task the way a crashed process leaves it: a
    submitted and a started row and a fresh fuel row, and no closing row.
    `theater=None` writes a legacy row (no theater fields)."""
    stamp = {} if theater is None else {"theater": theater, "theater_epoch": epoch}
    fuel = {} if airframe is None else {"airframe": airframe}
    handle = {"task_id": task_id, "tool": tool, "vehicle": vehicle}
    with Store(root) as seed:
        seed.log_task({**handle, "state": "queued"}, "submitted", params=params,
                      idempotency_key=None, **stamp)
        seed.log_task({**handle, "state": "executing"}, "started", params=params)
        seed.log_fuel(vehicle, fuel_pct, "cruise", bingo_fuel_pct=25.0, **fuel)


def run(coro):
    return asyncio.run(coro)


def tool(srv, name):
    return srv.mcp._tool_manager._tools[name].fn


def audits(srv, kind: str) -> list[dict]:
    return [r for r in srv.store.audit.read_all() if r.get("kind") == kind]


def task_rows(srv, event: str | None = None) -> list[dict]:
    return [r for r in srv.store.tasks.read_all()
            if event is None or r.get("event") == event]


def wait_until(pred, timeout: float = 15.0, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def queued_tools(srv, vehicle="Drone1") -> list[str]:
    """The tool of every task the vehicle's queue holds (current first)."""
    q = srv.tasking.queue_for(vehicle)
    tools = [t.tool for t in q.pending_tasks()]
    return ([q.current.tool] if q.current is not None else []) + tools


async def abort_all(srv, vehicle="Drone1") -> None:
    with suppress(Exception):
        await srv.tasking.queue_for(vehicle).abort()


# ------------------------------------------------ D7 #1: the boot re-gate --

def test_a_out_of_envelope_interrupted_route_is_not_resubmitted(tmp_path):
    """(a) The journal alone says RESUME; the re-gate against the envelope
    says no. Nothing re-flies the route: a forced RTB and the audit row."""
    seed_task(tmp_path, "old-a", "uav_fly_route",
              {"waypoints": OUT_OF_AO_ROUTE, "speed_mps": 8.0})
    with runtime_server(tmp_path) as srv:
        assert srv.recovery.decision_for("Drone1") == RESUME
        assert srv.boot_actions[0]["theater"] == "default"

        async def main():
            applied = await srv.apply_boot_recovery()
            assert len(applied) == 1 and applied[0]["decision"] == ABORT_RTH, applied
            assert "geofence" in applied[0]["regate_failed"], applied
            rtb = srv._rtb_task["Drone1"]
            assert rtb.tool == "uav_return_to_home" and rtb.uncancellable is True
            assert rtb.params["reason"] == "restart_recovery"
            await abort_all(srv)
        run(main())

        assert srv.boot_recovery_done.is_set()
        submitted = task_rows(srv, "submitted")
        assert [r["tool"] for r in submitted if r["task_id"] != "old-a"] \
            == ["uav_return_to_home"], "the out-of-envelope route was resubmitted"
        assert task_rows(srv, "resumed") == []
        failed = audits(srv, "restart_resume_regate_failed")
        assert len(failed) == 1
        assert failed[0]["vehicle"] == "Drone1" and failed[0]["recovered_from"] == "old-a"
        assert failed[0]["booted_theater"] == "default"
        assert "geofence" in failed[0]["reason"]
        assert any(r.get("reason") == "restart_recovery" for r in audits(srv, "force_rtb"))
        assert audits(srv, "restart_resume_regated") == []
        # The decision on record is the one taken (uav://safety/geofence).
        assert srv.recovery.decision_for("Drone1") == ABORT_RTH


def test_b_a_row_planned_in_another_theater_aborts_to_rth(tmp_path):
    """(b) `theater:"dyn-x"` booted at `default`: ABORT_RTH, never re-gated,
    even though the route would pass the default envelope."""
    seed_task(tmp_path, "old-b", "uav_fly_route",
              {"waypoints": IN_AO_ROUTE, "speed_mps": 8.0}, theater="dyn-x", epoch=3)
    with runtime_server(tmp_path) as srv:
        assert srv.boot_actions[0]["theater"] == "dyn-x"
        gated: list = []
        real_gate = srv._gate

        async def spy_gate(*a, **kw):
            gated.append(a)
            return await real_gate(*a, **kw)
        srv._gate = spy_gate

        async def main():
            applied = await srv.apply_boot_recovery()
            assert [a["decision"] for a in applied] == [ABORT_RTH]
            assert applied[0]["regate_failed"] == "planned in dyn-x, booted in default"
            assert srv._rtb_task["Drone1"].params["reason"] == "restart_recovery"
            await abort_all(srv)
        run(main())

        assert gated == [], "a row from another theater was gated, not aborted"
        assert task_rows(srv, "resumed") == []
        failed = audits(srv, "restart_resume_regate_failed")[-1]
        assert failed["planned_theater"] == "dyn-x" and failed["booted_theater"] == "default"
        assert failed["reason"] == "planned in dyn-x, booted in default"
        assert failed["gate"] is None
        assert srv.recovery.decision_for("Drone1") == ABORT_RTH
        assert any("planned in dyn-x" in r for r in srv.recovery.reasons_for("Drone1"))


def test_c_an_in_envelope_route_resumes_with_the_gate_epoch(tmp_path):
    """(c) Same theater, route inside the envelope: resubmitted through
    `_submit` (journaled with the theater) and audited as re-gated."""
    seed_task(tmp_path, "old-c", "uav_fly_route",
              {"waypoints": IN_AO_ROUTE, "speed_mps": 8.0})
    with runtime_server(tmp_path) as srv:
        seen: list[dict] = []
        real_submit = srv._submit

        def spy_submit(*a, **kw):
            seen.append(kw)
            return real_submit(*a, **kw)
        srv._submit = spy_submit

        async def main():
            applied = await srv.apply_boot_recovery()
            assert [a["decision"] for a in applied] == [RESUME], applied
            handle = applied[0]["task"]
            assert handle["tool"] == "uav_fly_route" and handle["status"] == "accepted"
            assert "uav_fly_route" in queued_tools(srv)
            await abort_all(srv)
            return handle
        handle = run(main())

        assert seen and seen[0]["gate_epoch"] == 0 and seen[0]["allow_queue"] is True
        resumed = task_rows(srv, "resumed")
        assert len(resumed) == 1 and resumed[0]["recovered_from"] == "old-c"
        assert resumed[0]["task_id"] == handle["task_id"]
        assert resumed[0]["theater"] == "default" and resumed[0]["theater_epoch"] == 0
        sub = [r for r in task_rows(srv, "submitted") if r["task_id"] == handle["task_id"]]
        assert sub and sub[0]["theater"] == "default" and sub[0]["theater_epoch"] == 0
        assert sub[0]["idempotency_key"] == "resume:old-c"
        ok = audits(srv, "restart_resume_regated")
        assert len(ok) == 1 and ok[0]["recovered_from"] == "old-c" and ok[0]["gated"] is True
        assert ok[0]["theater"] == "default" and ok[0]["planned_theater"] == "default"
        assert audits(srv, "restart_resume_regate_failed") == []
        assert srv.recovery.decision_for("Drone1") == RESUME
        assert srv.boot_recovery_done.is_set()


def test_d_a_legacy_row_is_re_gated_but_never_aborted_for_its_theater(tmp_path):
    """(d) A row with no `theater` predates the field: it is not assumed to
    be from another theater. Booted at Isfahan with a route inside the
    Isfahan AO, it resumes after the gate."""
    isfahan = theaters.get("iran-isfahan")
    route = [{"lat": 32.6560, "lon": 51.6700, "alt_agl_m": 50.0}]
    seed_task(tmp_path, "old-d", "uav_fly_route", {"waypoints": route, "speed_mps": 8.0},
              theater=None, airframe=None)
    with runtime_server(tmp_path, theater=isfahan) as srv:
        assert srv.boot_actions[0]["theater"] is None

        async def main():
            applied = await srv.apply_boot_recovery()
            assert [a["decision"] for a in applied] == [RESUME], applied
            await abort_all(srv)
        run(main())

        ok = audits(srv, "restart_resume_regated")
        assert len(ok) == 1 and ok[0]["planned_theater"] is None and ok[0]["gated"] is True
        assert ok[0]["theater"] == "iran-isfahan"
        assert audits(srv, "restart_resume_regate_failed") == []
        resumed = task_rows(srv, "resumed")
        assert resumed and resumed[0]["theater"] == "iran-isfahan"


def test_d_a_legacy_row_outside_the_envelope_is_aborted_by_the_gate(tmp_path):
    """(d) Re-gated means gated: a legacy row is aborted by the envelope,
    and the reason is the gate, not a theater mismatch."""
    seed_task(tmp_path, "old-d2", "uav_fly_route",
              {"waypoints": OUT_OF_AO_ROUTE, "speed_mps": 8.0}, theater=None)
    with runtime_server(tmp_path) as srv:
        async def main():
            applied = await srv.apply_boot_recovery()
            assert [a["decision"] for a in applied] == [ABORT_RTH]
            await abort_all(srv)
        run(main())
        failed = audits(srv, "restart_resume_regate_failed")
        assert len(failed) == 1 and failed[0]["planned_theater"] is None
        assert "geofence" in failed[0]["reason"] and "planned in" not in failed[0]["reason"]
        assert failed[0]["gate"]["ok"] is False
        assert task_rows(srv, "resumed") == []


def test_a_resume_whose_epoch_moved_during_the_gate_is_aborted(tmp_path):
    """The re-gate submits with `gate_epoch`: if the epoch moved while the
    gate awaited telemetry, `_submit` refuses and the vehicle RTBs. (A real
    switch cannot run before recovery finishes; the epoch is moved by hand
    to prove the guard is wired.)"""
    seed_task(tmp_path, "old-e", "uav_goto_gps",
              {"lat": 47.6430, "lon": -122.1390, "alt_m": 40.0, "alt_agl_m": 40.0,
               "speed_mps": 8.0})
    with runtime_server(tmp_path) as srv:
        real_gate = srv._gate

        async def gate_then_switch(*a, **kw):
            gate = await real_gate(*a, **kw)
            srv.theater_epoch += 1          # a switch completed meanwhile
            return gate
        srv._gate = gate_then_switch

        async def main():
            applied = await srv.apply_boot_recovery()
            assert [a["decision"] for a in applied] == [ABORT_RTH], applied
            assert applied[0]["regate_failed"] == THEATER_CHANGED_MESSAGE
            await abort_all(srv)
        run(main())
        assert [r["error"] for r in audits(srv, "submit_refused")] == ["theater_changed"]
        assert task_rows(srv, "resumed") == []
        assert audits(srv, "restart_resume_regate_failed")[-1]["gate"]["theater_epoch"] == 0


@pytest.mark.parametrize("tool_name", sorted(RESUME_NO_GATE_TOOLS))
def test_takeoff_land_and_hover_resume_without_a_route_gate(tmp_path, tool_name):
    params = {"alt_m": 20.0, "alt_agl_m": 20.0} if tool_name == "uav_takeoff" else {
        "reason": "interrupted"}
    seed_task(tmp_path, "old-f", tool_name, params)
    with runtime_server(tmp_path) as srv:
        gated: list = []
        real_gate = srv._gate

        async def spy_gate(*a, **kw):
            gated.append(a)
            return await real_gate(*a, **kw)
        srv._gate = spy_gate

        async def main():
            applied = await srv.apply_boot_recovery()
            assert [a["decision"] for a in applied] == [RESUME], applied
            assert applied[0]["task"]["tool"] == tool_name
            await abort_all(srv)
        run(main())
        assert gated == []
        ok = audits(srv, "restart_resume_regated")
        assert len(ok) == 1 and ok[0]["gated"] is False


def test_a_command_that_cannot_be_re_gated_is_aborted(tmp_path):
    seed_task(tmp_path, "old-g", "uav_set_gimbal", {"pitch_deg": -45.0})
    with runtime_server(tmp_path) as srv:
        async def main():
            applied = await srv.apply_boot_recovery()
            assert [a["decision"] for a in applied] == [ABORT_RTH]
            assert "not re-gateable" in applied[0]["regate_failed"]
            await abort_all(srv)
        run(main())
        assert "not re-gateable" in audits(srv, "restart_resume_regate_failed")[-1]["reason"]


def test_resume_route_per_tool(tmp_path):
    """`_resume_route(tool, params)` (§4.1.4): route and speed per tool."""
    with runtime_server(tmp_path) as srv:
        wps, speed = srv._resume_route("uav_fly_route",
                                       {"waypoints": IN_AO_ROUTE, "speed_mps": 7.0})
        assert wps == IN_AO_ROUTE and speed == 7.0
        wps, speed = srv._resume_route("uav_goto_gps", {"lat": 47.643, "lon": -122.139,
                                                        "alt_agl_m": 35.0})
        assert wps == [{"lat": 47.643, "lon": -122.139, "alt_m": 35.0}] and speed == 10.0
        home = srv.envelope.home
        wps, _ = srv._resume_route("uav_return_to_home", {"speed_mps": 12.0})
        assert wps == [{"lat": home[0], "lon": home[1], "alt_m": 0.0}]
        for name in ("uav_takeoff", "uav_land", "uav_hover"):
            assert srv._resume_route(name, {})[0] == []
        for name, params in (("uav_fly_route", {"waypoints": []}),
                             ("uav_goto_gps", {"lat": 47.6}),
                             ("uav_fly_route", {"waypoints": IN_AO_ROUTE,
                                                "speed_mps": "fast"}),
                             ("uav_orbit_poi", {"lat": 47.6, "lon": -122.1}),
                             ("uav_hover", ["not", "a", "dict"])):
            with pytest.raises(ValueError, match="re-gateable"):
                srv._resume_route(name, params)


# ------------------------------------------ §4.1.4: the journal's theater --

def test_task_rows_carry_the_theater_and_its_epoch(tmp_path):
    """`_submit` and `_force_rtb` rows name the theater and epoch they were
    written in, and follow a switch."""
    with runtime_server(tmp_path) as srv:
        async def main():
            h1 = srv._submit("Drone1", "uav_hover", {}, "hover-1")
            rtb = await srv._force_rtb("Drone1", reason="geofence", detail="test")
            srv.theater = theaters.get("iran-isfahan")     # what a switch sets
            srv.theater_epoch = 1
            h2 = srv._submit("Drone1", "uav_hover", {}, "hover-2", allow_queue=True)
            await abort_all(srv)
            return h1, rtb, h2
        h1, rtb, h2 = run(main())
        by_id = {r["task_id"]: r for r in task_rows(srv, "submitted")}
        assert by_id[h1["task_id"]]["theater"] == "default"
        assert by_id[h1["task_id"]]["theater_epoch"] == 0
        assert by_id[rtb["task_id"]]["theater"] == "default"
        assert by_id[rtb["task_id"]]["safety_reason"] == "geofence"
        assert by_id[h2["task_id"]]["theater"] == "iran-isfahan"
        assert by_id[h2["task_id"]]["theater_epoch"] == 1


def test_fuel_rows_carry_the_airframe_and_the_theater(tmp_path):
    """Every fuel row (safety tick and operator refuel) names the airframe
    that priced the burn, and the theater."""
    with runtime_server(tmp_path, airframe=GROUP3) as srv:
        async def main():
            await srv.tick_once("Drone1")
            srv.theater_epoch = 2
            out = await srv._set_fuel("Drone1", 80.0)
            assert out["ok"] is True, out
        run(main())
        rows = [r for r in srv.store.fuel.read_all() if r.get("vehicle") == "Drone1"]
        assert len(rows) == 2
        tick, refuel = rows
        for row in rows:
            assert row["airframe"] == GROUP3 == srv.airframe_id
            assert row["theater"] == "default"
        assert tick["theater_epoch"] == 0 and refuel["theater_epoch"] == 2
        assert refuel["reason"] == "operator_fuel_reset" and refuel["fuel_pct"] == 80.0


def test_a_crashed_run_hands_its_theater_to_the_next_boot(tmp_path):
    """End to end: a route in flight when the process dies is journaled with
    its theater; the next boot's action carries it and re-gates it."""
    a, b = tmp_path / "a", tmp_path / "b"
    with runtime_server(a) as srv:
        async def fly():
            await srv.tick_once("Drone1")
            h = await tool(srv, "uav_fly_route")(vehicle="Drone1", waypoints=IN_AO_ROUTE,
                                                 speed_mps=8.0)
            assert h.get("status") == "accepted", h
            return h
        handle = run(fly())
        assert wait_until(lambda: srv.tasking.queue_for("Drone1").current is not None)
        srv.store.sync()
        b.mkdir()
        for name in ("tasks", "fuel", "audit", "missions"):
            shutil.copy(a / f"{name}.jsonl", b / f"{name}.jsonl")   # the crash image
    with runtime_server(b) as srv2:
        acts = [x for x in srv2.boot_actions if x["id"] == handle["task_id"]]
        assert acts and acts[0]["theater"] == "default"
        assert acts[0]["decision"] == RESUME

        async def main():
            applied = await srv2.apply_boot_recovery()
            assert [x["decision"] for x in applied] == [RESUME], applied
            await abort_all(srv2)
        run(main())
        assert audits(srv2, "restart_resume_regated")[-1]["recovered_from"] == handle["task_id"]


# --------------------------------------- §4.1.4: fuel restore by airframe --

def seed_fuel(root, *, airframe: str | None, fuel_pct: float = 55.0) -> None:
    extra = {} if airframe is None else {"airframe": airframe}
    with Store(root) as seed:
        seed.log_fuel("Drone1", fuel_pct, "cruise", bingo_fuel_pct=25.0,
                      bingo_latched=False, burned_pct=45.0, elapsed_s=600.0,
                      ticks=1200, **extra)


def test_a_fuel_row_from_another_airframe_starts_a_full_tank(tmp_path):
    seed_fuel(tmp_path, airframe=GROUP3)
    with runtime_server(tmp_path) as srv:
        assert srv.airframe_id == DEFAULT_AIRFRAME_ID
        fm = srv.fuel_for("Drone1")
        assert fm.fuel_pct == 100.0 and fm.airframe.id == DEFAULT_AIRFRAME_ID
        row = next(r for r in srv.boot_restored["fuel"] if r["vehicle"] == "Drone1")
        assert row["restored"] is False and row["row_airframe"] == GROUP3
        assert GROUP3 in row["reason"] and DEFAULT_AIRFRAME_ID in row["reason"]
        assert srv.boot_restored["fuel_not_restored"] == ["Drone1"]
        mism = audits(srv, "fuel_restore_airframe_mismatch")
        assert len(mism) == 1
        assert mism[0]["vehicle"] == "Drone1" and mism[0]["row_airframe"] == GROUP3
        assert mism[0]["airframe"] == DEFAULT_AIRFRAME_ID
        assert mism[0]["row_fuel_pct"] == 55.0


def test_a_fuel_row_from_the_booted_airframe_is_restored(tmp_path):
    seed_fuel(tmp_path, airframe=GROUP3)
    with runtime_server(tmp_path, airframe=GROUP3) as srv:
        fm = srv.fuel_for("Drone1")
        assert fm.airframe.id == GROUP3 and fm.fuel_pct == pytest.approx(55.0)
        row = next(r for r in srv.boot_restored["fuel"] if r["vehicle"] == "Drone1")
        assert row["restored"] is True and row["airframe"] == GROUP3
        assert audits(srv, "fuel_restore_airframe_mismatch") == []


def test_a_legacy_fuel_row_without_an_airframe_is_restored_as_before(tmp_path):
    seed_fuel(tmp_path, airframe=None)
    with runtime_server(tmp_path, airframe=GROUP3) as srv:
        fm = srv.fuel_for("Drone1")
        assert fm.fuel_pct == pytest.approx(55.0) and fm.airframe.id == GROUP3
        row = next(r for r in srv.boot_restored["fuel"] if r["vehicle"] == "Drone1")
        assert row["restored"] is True and "airframe" in row["fields_absent_from_row"]
        assert audits(srv, "fuel_restore_airframe_mismatch") == []


# ------------------------------- D7 #12: boot_recovery_done gates a switch --

def test_recovery_with_nothing_to_do_still_marks_itself_done(tmp_path):
    with runtime_server(tmp_path) as srv:
        assert not srv.boot_recovery_done.is_set()
        assert run(srv.apply_boot_recovery()) == []
        assert srv.boot_recovery_done.is_set()


def test_recovery_that_raises_is_still_marked_done(tmp_path):
    """The `finally`: a recovery that failed is over, and says so."""
    seed_task(tmp_path, "old-h", "uav_hover", {"reason": "x"}, fuel_pct=5.0)
    with runtime_server(tmp_path) as srv:
        assert srv.recovery.decision_for("Drone1") == ABORT_RTH

        async def broken_rtb(*a, **kw):
            raise RuntimeError("queue gone")
        srv._force_rtb = broken_rtb
        with pytest.raises(RuntimeError, match="queue gone"):
            run(srv.apply_boot_recovery())
        assert srv.boot_recovery_done.is_set()


def test_the_monitor_marks_recovery_done_when_recovery_raises(tmp_path):
    with runtime_server(tmp_path) as srv:
        async def boom():
            raise RuntimeError("journal unreadable")
        srv.apply_boot_recovery = boom
        srv.start_monitor(interval_s=0.05)
        assert wait_until(srv.boot_recovery_done.is_set)
        rows = audits(srv, "restart_recovery_failed")
        assert rows and "journal unreadable" in rows[-1]["message"]


def _set_args(srv) -> dict:
    """A Bengaluru proposal through the `/mcp` tool (geodata off: no network)."""
    out = run(tool(srv, "theater_propose")(
        lat=12.9716, lon=77.5946, label="Bengaluru centre", ground_msl_m=920.0,
        airframe=DEFAULT_AIRFRAME_ID))
    assert isinstance(out.get("set_args"), dict), out
    return out["set_args"]


def _ready_for_switch(srv) -> bool:
    """Recovery done, the monitor has seen Drone1 landed, nothing flying."""
    q = srv.tasking.queue_for("Drone1")
    rtb = srv._rtb_task.get("Drone1")
    return (srv.boot_recovery_done.is_set() and "Drone1" in srv.ticks
            and q.active() is None
            and (rtb is None or rtb.state.value in ("done", "failed", "cancelled")))


def test_mcp_set_theater_is_refused_until_restart_recovery_finishes(tmp_path):
    """D7 #12: before the monitor has run recovery, `/mcp` `sim_set_theater`
    is refused with "restart recovery still running"; after it, accepted."""
    with runtime_server(tmp_path) as srv:
        events: list[dict] = []
        srv.theater_listeners.append(events.append)
        args = _set_args(srv)
        early = run(tool(srv, "sim_set_theater")(**args))
        assert early["rejected"] is True and early["error"] == "switch_refused", early
        assert early["reasons"] == ["restart recovery still running"]
        assert srv.theater.id == "default" and srv.theater_epoch == 0 and events == []

        srv.start_monitor(interval_s=0.05)
        assert wait_until(lambda: _ready_for_switch(srv))
        out = run(tool(srv, "sim_set_theater")(**args))
        assert out.get("status") == "accepted", out
        assert out["theater"]["id"] == args["theater_id"] and out["theater"]["epoch"] == 1
        assert srv.theater.id == args["theater_id"] and len(events) == 1


def test_a_switch_during_a_running_recovery_is_refused(tmp_path):
    """D7 #12 while recovery is IN PROGRESS (its re-gate awaiting telemetry):
    refused; once the re-gate has aborted the route and the RTB is over,
    the same proposal is accepted."""
    seed_task(tmp_path, "old-i", "uav_fly_route",
              {"waypoints": OUT_OF_AO_ROUTE, "speed_mps": 8.0})
    with runtime_server(tmp_path) as srv:
        srv.theater_listeners.append(lambda state: None)
        entered, release = threading.Event(), threading.Event()
        real_gate = srv._gate

        async def slow_gate(*a, **kw):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return await real_gate(*a, **kw)
        srv._gate = slow_gate
        args = _set_args(srv)
        srv.start_monitor(interval_s=0.05)
        assert entered.wait(10.0), "recovery never reached its re-gate"
        during = run(tool(srv, "sim_set_theater")(**args))
        assert during["reasons"] == ["restart recovery still running"], during
        assert not srv.boot_recovery_done.is_set()
        release.set()
        assert wait_until(lambda: _ready_for_switch(srv), timeout=30.0)
        assert audits(srv, "restart_resume_regate_failed")
        out = run(tool(srv, "sim_set_theater")(**args))
        assert out.get("status") == "accepted", out


# ------------------------------------------ D7 #9: monitor roster refresh --

def _count_ticks(srv) -> list[tuple[str, int]]:
    """Spy on `tick_once`: (vehicle, monitor pass) per tick."""
    ticks: list[tuple[str, int]] = []
    real_tick = srv.tick_once

    async def spy(vehicle, now=None):
        ticks.append((vehicle, srv._monitor_passes))
        return await real_tick(vehicle, now)
    srv.tick_once = spy
    return ticks


def test_a_drone_created_after_boot_is_ticked_within_20_passes(tmp_path):
    with runtime_server(tmp_path) as srv:
        ticks = _count_ticks(srv)
        srv.start_monitor(interval_s=0.05)
        assert wait_until(lambda: len(ticks) >= 3)
        assert {v for v, _ in ticks} == {"Drone1"}
        with srv.sim._lock:
            srv.sim._veh("Drone2")                  # a drone the sim adds later
        created_at = srv._monitor_passes
        assert wait_until(lambda: any(v == "Drone2" for v, _ in ticks), timeout=30.0)
        first = next(p for v, p in ticks if v == "Drone2")
        assert first - created_at <= ROSTER_REFRESH_PASSES, (created_at, first)
        grew = audits(srv, "vehicle_roster_refreshed")
        assert grew and grew[-1]["added"] == ["Drone2"]
        assert grew[-1]["monitored"] == ["Drone1", "Drone2"]
        # Drone1 is still ticked after the refresh (a union, not a replace).
        assert wait_until(lambda: any(v == "Drone1" and p > first for v, p in ticks))


def test_an_explicit_monitor_roster_is_not_widened(tmp_path):
    """`start_monitor(["Drone1"])` monitors what the caller named."""
    with runtime_server(tmp_path) as srv:
        ticks = _count_ticks(srv)
        with srv.sim._lock:
            srv.sim._veh("Drone2")
        srv.start_monitor(["Drone1"], interval_s=0.05)
        assert wait_until(lambda: srv._monitor_passes > 2 * ROSTER_REFRESH_PASSES + 1,
                          timeout=30.0)
        assert {v for v, _ in ticks} == {"Drone1"}
        assert audits(srv, "vehicle_roster_refreshed") == []


def test_a_failed_roster_refresh_keeps_every_known_name(tmp_path):
    with runtime_server(tmp_path) as srv:
        real = srv.backend.list_vehicles

        async def boom():
            raise ConnectionError("sim went away")
        srv.backend.list_vehicles = boom
        assert run(srv._refresh_roster(["Drone1", "Drone2"])) == ["Drone1", "Drone2"]
        assert "ConnectionError" in srv.vehicle_roster_error
        assert audits(srv, "vehicle_roster_unavailable")[-1]["refresh"] is True
        srv.backend.list_vehicles = real
        # A name the sim no longer lists stays monitored; nothing new joined.
        assert run(srv._refresh_roster(["Drone1", "Ghost"])) == ["Drone1", "Ghost"]
        assert srv.vehicle_roster_error is None
        assert audits(srv, "vehicle_roster_refreshed") == []
