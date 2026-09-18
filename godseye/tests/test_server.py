"""MCP server tool-catalog tests against the fake AirSim (PLAN §4.1/4.2).

Wave-2 stage-1 wiring contracts pinned here:
  * T1 — one datum conversion point: telemetry carries alt_hae_m / alt_msl_m /
    alt_agl_m and the BINGO line is priced from AGL, not from HAE.
  * T5/M4/R5 — the fuel integrator is actually ticked from live telemetry with
    the MEASURED phase and the resolved headwind, the integral is persisted,
    and reaching BINGO forces an UN-CANCELLABLE RTB.
  * M9 — the lost-link plan runs autonomously, on a sim link drop and on a
    harness disconnect.
  * T2/T4a/T4b — busy rejection, pollable queued ids, real idempotency, and
    progress derived server-side from telemetry.
  * T4c — restart replay is acted on at boot.
"""
import asyncio
import base64
import hashlib
import itertools
import json
import math
import os
import time
from contextlib import contextmanager

import airsim
import pytest
from godseye_uav import theaters
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint, canonical_altitude
from godseye_uav.safety import (
    MISSION_INCOMPLETE_FUEL,
    FuelModel,
    SafetyEnvelope,
    haversine_m,
    headwind_component_mps,
)
from godseye_uav.server import (
    AGL_SOURCE_LAUNCH,
    AGL_SOURCE_TERRAIN,
    EXEC_PROGRESS_CEILING,
    REAL_DATA_ENV,
    RTB_CRUISE_PCT,
    GodseyeUavServer,
    StaticBearerVerifier,
    UavBackend,
    resolve_real_data,
)
from godseye_uav.store import Store

# HOME.altitude is HAE (geo.GeoPoint's documented datum, T1).
HOME = GeoPoint(47.641468, -122.140165, 93.0)
AO = [(47.63, -122.16), (47.63, -122.12), (47.66, -122.12), (47.66, -122.16)]
TIGHT_AO = [(47.70, -122.30), (47.70, -122.28), (47.72, -122.28), (47.72, -122.30)]

# Unique port per test so a lingering sim/handler from one test never blocks
# the next test's msgpack listen (root cause of the full-suite hang).
# Wave-2 CORE stage-1 owns 47000-47099. The start offset is process-derived and
# a busy port is skipped, so two concurrent pytest runs do not fight over it.
_PORTS = list(range(47000, 47100))
_PORT = itertools.cycle(_PORTS[os.getpid() % len(_PORTS):] + _PORTS[:os.getpid() % len(_PORTS)])

#: The restart-recovery / M5 re-path tests at the end of this file own
#: 51000-51099, so the sims they start cannot contend with the 47000-range
#: sims every other test in this file uses — including a concurrent run.
_RESTART_PORTS = list(range(51000, 51100))
_RESTART_PORT = itertools.cycle(
    _RESTART_PORTS[os.getpid() % len(_RESTART_PORTS):]
    + _RESTART_PORTS[:os.getpid() % len(_RESTART_PORTS)])


def _start_sim(ports: list[int] | None = None, nxt=None) -> FakeAirSim:
    """Start a FakeAirSim on the first free port in the assigned range.

    The sim is given the SAME home as `UavBackend`, so the sim's own geodetic
    frame and the server's agree. They diverged by 29 m before (the fake's
    DEFAULT_HOME is 122 m, HOME is 93 m), which is invisible for NED-derived
    telemetry but silently wrong for anything expressed in absolute geodetic
    altitude — line of sight and obstruction geometry above all.

    `ports`/`nxt` select a different range and its own round-robin cursor.
    """
    ports = _PORTS if ports is None else ports
    nxt = _PORT if nxt is None else nxt
    last = None
    for _ in range(len(ports)):
        sim = FakeAirSim(home=HOME, port=next(nxt))
        try:
            sim.start()
            return sim
        except OSError as exc:  # port taken by another run — try the next one
            last = exc
            try:
                sim.stop()
            except Exception:
                pass
    raise RuntimeError(f"no free port in {ports[0]}-{ports[-1]}: {last}")


@contextmanager
def build_server(tmp_path, *, envelope=..., watchdog_s=30.0, store=None,
                 ports=None, nxt=None, **kw):
    sim = _start_sim(ports, nxt)  # avoid the live dev sim on :41451
    port = sim.port
    srv = None
    owns_store = store is None
    try:
        client = airsim.MultirotorClient(port=port)
        client.confirmConnection()
        backend = UavBackend(client, HOME, sim=sim)
        if envelope is ...:
            envelope = SafetyEnvelope(
                geofence=AO, home=(HOME.latitude, HOME.longitude, HOME.altitude))
        store = store if store is not None else Store(tmp_path)
        srv = GodseyeUavServer(backend, store, envelope=envelope,
                               watchdog_s=watchdog_s, **kw)
        srv.sim = sim
        yield srv
    finally:
        if srv is not None:
            srv.stop_monitor()
            try:
                srv.tasking.shutdown()
            except Exception:
                pass
        if owns_store and store is not None:
            store.close()
        sim.stop()


@pytest.fixture
def server(tmp_path):
    with build_server(tmp_path) as srv:
        yield srv


def run(coro):
    return asyncio.run(coro)


def tool(srv, name):
    return srv.mcp._tool_manager._tools[name].fn


async def wait_state(srv, vehicle, task_id, states=("done", "failed", "cancelled"),
                     timeout_s=30.0):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        t = srv.tasking.queue_for(vehicle).get(task_id)
        if t and t.state.value in states:
            return t
        await asyncio.sleep(0.1)
    return srv.tasking.queue_for(vehicle).get(task_id)


async def takeoff_to(srv, vehicle, alt_m, timeout_s=40.0):
    h = srv._submit(vehicle, "uav_takeoff", {"alt_m": alt_m}, None)
    t = await wait_state(srv, vehicle, h["task_id"], timeout_s=timeout_s)
    assert t.state.value == "done", t.error
    return t


# ---------------------------------------------------------------- auth -----

def test_bearer_verifier():
    v = StaticBearerVerifier("secret")
    assert run(v.verify_token("secret")) is not None
    assert run(v.verify_token("wrong")) is None


# ------------------------------------------------------- T1: the datum -----

def test_telemetry_publishes_all_three_datums_from_one_conversion(server):
    """T1: alt_hae_m / alt_msl_m / alt_agl_m, geoid applied exactly once.

    The old server published the raw NED altitude under the name `alt_hae`
    with no geoid at all, so it disagreed with the bridge by |N| (R3).
    """
    async def main():
        tele = await tool(server, "uav_get_telemetry")(vehicle="Drone1")
        for key in ("alt_hae_m", "alt_msl_m", "alt_agl_m", "undulation_m",
                    "datum_source"):
            assert key in tele, key
        # External oracle: EGM96 N at the Redmond origin is -22.21 m.
        assert abs(tele["undulation_m"] - (-22.21)) < 1.0
        assert tele["datum_degraded"] is False
        # h = H + N, applied exactly once.
        assert abs((tele["alt_hae_m"] - tele["alt_msl_m"]) - tele["undulation_m"]) < 0.01
        fix = canonical_altitude(tele["alt_hae_m"], tele["lat"], tele["lon"], datum="hae")
        assert abs(tele["alt_msl_m"] - fix.alt_msl) < 0.01
        # The vehicle is at the NED origin, so HAE is the origin's own HAE.
        assert abs(tele["alt_hae_m"] - HOME.altitude) < 1.0
        assert abs(tele["alt_agl_m"] - (-tele["ned"][2])) < 0.01
    run(main())


def test_bingo_line_is_priced_from_agl_not_hae(server):
    """The Wave-1 handoff: server.py passed alt_hae where the parameter is AGL."""
    async def main():
        await takeoff_to(server, "Drone1", 30.0)
        tele = await tool(server, "uav_get_telemetry")(vehicle="Drone1")
        fm = server.fuel_for("Drone1")
        wind_ne, _ = await server.backend.wind_ne()
        from_agl = fm.bingo_fuel_pct((tele["lat"], tele["lon"]), tele["alt_agl_m"],
                                     wind_ne=wind_ne)
        from_hae = fm.bingo_fuel_pct((tele["lat"], tele["lon"]), tele["alt_hae_m"],
                                     wind_ne=wind_ne)
        assert abs(tele["bingo_fuel_pct"] - round(from_agl, 2)) < 0.01
        assert from_hae > from_agl  # the old call inflated the let-down burn
    run(main())


# --------------------------------------- T5/M4/R5: the fuel + safety loop ---

def test_tick_loop_burns_fuel_and_persists_the_integral(server):
    """R5: without this loop fuel_pct is a constant 100.0 and BINGO never fires."""
    async def main():
        fm = server.fuel_for("Drone1")
        assert fm.fuel_pct == 100.0 and fm.ticks == 0
        for _ in range(6):
            v = await server.tick_once("Drone1")
            await asyncio.sleep(0.15)
        assert fm.ticks >= 4, fm.ticks
        assert fm.fuel_pct < 100.0, "the integrator never advanced"
        assert fm.burned_pct > 0.0
        assert v["telemetry"] is True
        rows = [r for r in server.store.fuel.read_all() if r.get("vehicle") == "Drone1"]
        assert len(rows) >= 4
        assert rows[-1]["phase"] in ("ground", "hover", "cruise", "climb", "descend")
        assert rows[-1]["bingo_fuel_pct"] >= 20.0
        assert rows[-1]["fuel_pct"] == pytest.approx(fm.fuel_pct, abs=0.01)
    run(main())


def test_tick_uses_the_measured_phase_not_the_commanded_one(server):
    """T5: 'recompute phase from MEASURED climb/cruise (not commanded)'."""
    async def main():
        server._submit("Drone1", "uav_takeoff", {"alt_m": 60}, None)
        seen = set()
        end = time.monotonic() + 25.0
        while time.monotonic() < end and "climb" not in seen:
            v = await server.tick_once("Drone1")
            tele = await server.backend.telemetry("Drone1")
            # the phase the integrator charged is the one the MEASUREMENT implies
            expected = FuelModel().classify_phase(
                tele["speed_mps"], tele["vz_mps"],
                int(tele["landed_state"]) == 0).value
            seen.add(v["phase"])
            assert v["phase"] in (expected, "climb", "hover", "cruise", "ground")
            await asyncio.sleep(0.1)
        assert "climb" in seen, f"never measured a climb phase: {seen}"
    run(main())


def test_tick_resolves_headwind_from_the_wind_vector(server):
    """M15: the wind vector reaches the burn rate through the ground track."""
    async def main():
        await takeoff_to(server, "Drone1", 15.0)
        north = server._submit("Drone1", "uav_goto_gps",
                               {"lat": HOME.latitude + 0.004, "lon": HOME.longitude,
                                "alt_m": 15.0, "speed_mps": 10.0}, None)
        server.sim.set_wind(-8.0, 0.0, 0.0)  # air mass moving south = headwind north
        head, track = 0.0, None
        end = time.monotonic() + 20.0
        while time.monotonic() < end:
            tele = await server.backend.telemetry("Drone1")
            if tele["speed_mps"] > 3.0 and tele["track_deg"] is not None:
                v = await server.tick_once("Drone1")
                head, track = v["headwind_mps"], tele["track_deg"]
                break
            await asyncio.sleep(0.1)
        # "vehicle never moved" on its own cannot be diagnosed after the fact:
        # the leg not starting, the leg failing and the poll loop being starved
        # all look identical. Say which one it was.
        if track is None:
            leg = server.tasking.queue_for("Drone1").get(north.get("task_id"))
            raise AssertionError(
                f"vehicle never moved: submit returned {north.get('status')!r}, "
                f"leg is {leg.state.value if leg else 'unknown'} "
                f"at {leg.progress_pct if leg else '?'}% "
                f"(error={leg.error if leg else None!r})")
        assert head == pytest.approx(headwind_component_mps(-8.0, 0.0, track), abs=1.5)
        assert head > 4.0, f"headwind {head} on track {track}"
        await server.tasking.queue_for("Drone1").abort()
        assert north["state"] in ("queued", "executing")
    run(main())


def test_bingo_forces_an_uncancellable_rtb_that_the_harness_cannot_clear(server):
    """M4/T5: force-RTB, mission flagged 'incomplete - fuel', harness refused."""
    async def main():
        await takeoff_to(server, "Drone1", 60.0)
        fm = server.fuel_for("Drone1")
        fm.fuel_pct = 12.0  # below any plausible return-leg + 20% reserve
        v = await server.tick_once("Drone1")
        assert v["bingo"]["below_bingo"] is True
        assert v["bingo"]["tripped_now"] is True
        assert v["force_rtb"] is True and "bingo" in v["rtb_reasons"]
        assert v["mission_status"] == MISSION_INCOMPLETE_FUEL
        assert server.mission_flags["Drone1"] == MISSION_INCOMPLETE_FUEL

        q = server.tasking.queue_for("Drone1")
        end = time.monotonic() + 10.0
        while time.monotonic() < end and (
                q.current is None or q.current.tool != "uav_return_to_home"):
            await asyncio.sleep(0.1)
        assert q.current is not None and q.current.tool == "uav_return_to_home"
        assert q.current.uncancellable is True and q.current.safety is True

        # the harness cannot abort it, and cannot clear the flag or the latch
        out = await tool(server, "uav_abort")(vehicle="Drone1")
        assert out["aborted"] is False and out["refused"] is True
        assert out["mission_status"] == MISSION_INCOMPLETE_FUEL
        assert out["bingo"]["clear_attempts"] >= 1
        assert fm.bingo.tripped is True
        assert server.mission_flags["Drone1"] == MISSION_INCOMPLETE_FUEL
        assert q.current.tool == "uav_return_to_home"

        # and a new harness plan is refused by the gate while BINGO is latched
        plan = await tool(server, "uav_goto_gps")(
            vehicle="Drone1", lat=47.645, lon=-122.145, alt_m=40.0)
        assert plan.get("rejected") is True or plan.get("status") == "busy"

        kinds = [r["kind"] for r in server.store.audit.read_all()]
        assert "bingo" in kinds and "force_rtb" in kinds
        assert "abort_refused" in kinds
    run(main())


def test_geofence_breach_in_flight_forces_rtb(tmp_path):
    """M4: the envelope was only checked at plan submission; now it is flown."""
    fence = SafetyEnvelope(geofence=TIGHT_AO,  # deliberately excludes home
                           home=(HOME.latitude, HOME.longitude, HOME.altitude))
    with build_server(tmp_path, envelope=fence) as srv:
        async def main():
            v = await srv.tick_once("Drone1")
            kinds = [x["kind"] for x in v["violations"]]
            assert "geofence" in kinds
            assert "geofence" in v["breaches"]
            assert v["force_rtb"] is True and "geofence" in v["rtb_reasons"]
            q = srv.tasking.queue_for("Drone1")
            end = time.monotonic() + 10.0
            while time.monotonic() < end and q.current is None:
                await asyncio.sleep(0.1)
            assert q.current is not None
            assert q.current.tool == "uav_return_to_home"
            assert q.current.uncancellable is True
            kinds = [r["kind"] for r in srv.store.audit.read_all()]
            assert "geofence" in kinds and "force_rtb" in kinds
        run(main())


def test_geofence_proximity_is_a_warning_not_a_breach(tmp_path):
    """PLAN §3.1 lists geofence PROXIMITY as its own alarm, distinct from a breach."""
    near = SafetyEnvelope(geofence=AO, geofence_warn_m=100_000.0,
                          home=(HOME.latitude, HOME.longitude, HOME.altitude))
    with build_server(tmp_path, envelope=near) as srv:
        async def main():
            v = await srv.tick_once("Drone1")
            by_kind = {x["kind"]: x for x in v["violations"]}
            assert "geofence_proximity" in by_kind
            assert by_kind["geofence_proximity"]["severity"] == "warning"
            assert "geofence" not in by_kind          # inside the fence
            assert v["breaches"] == []
            assert v["force_rtb"] is False            # a warning never forces RTB
            assert any(a["kind"] == "geofence_proximity" for a in v["alarms"])
        run(main())


def test_ceiling_breach_is_detected_in_flight(tmp_path):
    low = SafetyEnvelope(geofence=AO, ceiling_m_agl=10.0,
                         home=(HOME.latitude, HOME.longitude, HOME.altitude))
    with build_server(tmp_path, envelope=low) as srv:
        async def main():
            srv._submit("Drone1", "uav_takeoff", {"alt_m": 40}, None)
            end = time.monotonic() + 30.0
            breached = None
            while time.monotonic() < end:
                v = await srv.tick_once("Drone1")
                if "ceiling" in v["breaches"]:
                    breached = v
                    break
                await asyncio.sleep(0.15)
            assert breached is not None, "ceiling breach never detected in flight"
            assert breached["force_rtb"] is False  # ceiling alone does not RTB
            rows = [r for r in srv.store.audit.read_all()]
            kinds = [r["kind"] for r in rows]
            assert "envelope_breach" in kinds
            # a takeoff legitimately flies through the min-AGL band: that is
            # logged as a transition, never as an in-flight envelope breach
            # (a restart replay must not read every takeoff as unsafe)
            assert all(r.get("alarm") != "min_agl"
                       for r in rows if r["kind"] == "envelope_breach")
            assert any(r["kind"] == "envelope_transition" and r.get("alarm") == "min_agl"
                       for r in rows)
        run(main())


def test_an_envelope_from_another_theater_is_reported_not_swallowed(tmp_path):
    """`launch.py` passes `envelope=` but not `theater=`.

    Adversarial finding. Everything theater-derived — `sim_spawn_target`'s
    DEFAULT ground elevation, the seeded pattern-of-life POIs, the INTREP
    `area_name`, the `theater` block on uav://safety/geofence — comes from
    `self.theater`, which falls back to the DEFAULT (Redmond, 122 m MSL) when
    no theater is passed. Launch with `--theater indo-pak-loc` and the fence
    is the Kashmir AO while targets spawn at 122 m MSL on ~1550 m ground: the
    §4.4 "buried ~1550 m underground, undetectable" defect, reintroduced via
    the launcher. The server cannot fix the launcher, but it must not sit
    silently mis-configured.
    """
    other = theaters.get("indo-pak-loc")
    env = SafetyEnvelope(**other.envelope_kwargs())
    with build_server(tmp_path, envelope=env) as srv:
        assert srv.theater.id != other.id  # the mis-wiring being detected
        mm = srv.theater_mismatch
        assert mm is not None, "an envelope from another theater was swallowed"
        assert mm["offset_m"] > 5000.0
        assert mm["theater_ground_elevation_msl_m"] == srv.theater.home_alt_msl_m
        assert "theater_mismatch" in [r["kind"] for r in srv.store.audit.read_all()]

        async def main():
            fence = await read_json(srv, "uav://safety/geofence")
            assert fence["theater_mismatch"] is not None
            assert fence["theater_mismatch"]["envelope_home"][0] == env.home[0]
        run(main())


def test_a_matching_theater_reports_no_mismatch(server):
    """The normal path stays quiet: no false alarm for the ordinary server."""
    assert server.theater_mismatch is None

    async def main():
        fence = await read_json(server, "uav://safety/geofence")
        assert fence["theater_mismatch"] is None
    run(main())


def test_an_rtb_touchdown_is_a_transition_not_an_in_flight_breach(tmp_path):
    """The `envelope_transition` carve-out missed the commonest landing there is.

    Adversarial finding. `_vertical_transition` recognised only uav_takeoff and
    uav_land, but every BINGO RTB, every lost-link RTB and every restart RTH
    lands inside `uav_return_to_home`. Its touchdown was therefore audited as
    `envelope_breach`, which `store.SAFETY_AUDIT_KINDS` treats as a safety
    event — so the NEXT restart read a normal landing as an in-flight breach
    and decided ABORT_RTH, flying another RTB. Proven on a live run: two
    `min_agl(2.1m<3m)` rows with `transition: null` right after a BINGO RTB.
    """
    with build_server(tmp_path) as srv:
        async def main():
            await takeoff_to(srv, "Drone1", 25.0)
            h = await srv._force_rtb("Drone1", reason="bingo", detail="test")
            # Tick THROUGHOUT, the way the monitor does: the min-AGL alarm
            # fires during the descent, while the RTB is still the current task.
            q = srv.tasking.queue_for("Drone1")
            end = time.monotonic() + 60.0
            while time.monotonic() < end:
                await srv.tick_once("Drone1")
                t = q.get(h["task_id"])
                if t and t.state.value in ("done", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.05)
            assert t.state.value == "done", t.error
            rows = srv.store.audit.read_all()
            min_agl = [r for r in rows if r.get("alarm") == "min_agl"]
            assert min_agl, "the RTB landing never entered the min-AGL band"
            breaches = [r for r in min_agl if r["kind"] == "envelope_breach"]
            assert not breaches, (
                "an RTB touchdown was audited as an in-flight envelope breach; "
                "a restart replay reads that as a safety event and re-RTBs: "
                f"{breaches}")
            assert any(r["kind"] == "envelope_transition"
                       and r.get("transition") == "uav_return_to_home"
                       for r in min_agl)
            # and store.replay must not now treat the flight as unsafe
            assert not any(r["kind"] in ("envelope_breach",) for r in rows)
        run(main())


def test_takeoff_above_the_ceiling_is_rejected(tmp_path):
    low = SafetyEnvelope(geofence=AO, ceiling_m_agl=50.0,
                         home=(HOME.latitude, HOME.longitude, HOME.altitude))
    with build_server(tmp_path, envelope=low) as srv:
        async def main():
            out = await tool(srv, "uav_takeoff")(vehicle="Drone1", alt_m=400.0)
            assert out["rejected"] is True
            assert any("ceiling" in v for v in out["gate"]["envelope_violations"])
        run(main())


# ------------------------------------------------------------- M9 link -----

def test_lost_link_declares_loal_and_flies_the_plan(tmp_path):
    with build_server(tmp_path, lost_link_plan={"behaviour": "hold_orbit",
                                                "declare_after_s": 0.2,
                                                "restore_after_s": 0.2}) as srv:
        async def main():
            await takeoff_to(srv, "Drone1", 20.0)
            await srv.tick_once("Drone1")           # prime with a good sample
            srv.sim.set_link_state("Drone1", "lost")
            event = None
            end = time.monotonic() + 10.0
            while time.monotonic() < end:
                v = await srv.tick_once("Drone1")
                if v.get("link_event"):
                    event = v["link_event"]
                    break
                await asyncio.sleep(0.15)
            assert event is not None and event["event"] == "loal_declared"
            assert event["behaviour"] == "hold_orbit"
            assert srv.loal_events and srv.loal_events[-1]["vehicle"] == "Drone1"
            q = srv.tasking.queue_for("Drone1")
            end = time.monotonic() + 10.0
            while time.monotonic() < end and q.current is None:
                await asyncio.sleep(0.1)
            assert q.current is not None
            assert q.current.params.get("reason") == "lost_link_hold_orbit"
            assert q.current.uncancellable is True
            kinds = [r["kind"] for r in srv.store.audit.read_all()]
            assert "loal" in kinds and "lost_link" in kinds
        run(main())


def test_harness_disconnect_triggers_the_lost_link_plan(tmp_path):
    """PLAN §4.5: 'Harness disconnect -> lost_link_plan'."""
    with build_server(tmp_path, lost_link_plan={"behaviour": "rtb",
                                                "declare_after_s": 0.2}) as srv:
        async def main():
            await takeoff_to(srv, "Drone1", 20.0)
            await srv.tick_once("Drone1")
            srv.harness_disconnected("Drone1")
            event = None
            end = time.monotonic() + 10.0
            while time.monotonic() < end:
                v = await srv.tick_once("Drone1")
                assert v["harness_disconnected"] is True
                if v.get("link_event"):
                    event = v["link_event"]
                    break
                await asyncio.sleep(0.15)
            assert event is not None and event["event"] == "loal_declared"
            q = srv.tasking.queue_for("Drone1")
            end = time.monotonic() + 10.0
            while time.monotonic() < end and q.current is None:
                await asyncio.sleep(0.1)
            assert q.current is not None
            assert q.current.tool == "uav_return_to_home"
            assert q.current.uncancellable is True
            assert q.current.params["reason"] == "lost_link"
        run(main())


def test_lost_link_rtb_survives_the_lost_link_that_caused_it(tmp_path):
    """M9: the force-RTB must FLY with the datalink down, not die on it.

    Adversarial finding. The existing M9 tests assert only that a task was
    submitted (`q.current is not None`). Over the wire the submitted task was
    marked `started` and `failed` 0.6 ms later with
    "datalink lost: no telemetry from Drone1", because `_execute`'s
    uav_return_to_home branch opened with `await backend.telemetry(v)` — the
    one RPC a lost link removes. The aircraft was left airborne, LOAL, with no
    recovery flying and no further attempt.

    The airframe stays commandable with the link down (only sensing is gone),
    so the RTB must fly on the monitor's last known fix and REPORT that the
    arrival was never observed.
    """
    import godseye_uav.server as server_mod
    old = (server_mod.BLIND_LEG_GRACE_S, server_mod.BLIND_LAND_S)
    server_mod.BLIND_LEG_GRACE_S, server_mod.BLIND_LAND_S = 1.5, 1.5
    with build_server(tmp_path, lost_link_plan={"behaviour": "rtb",
                                                "declare_after_s": 0.2}) as srv:
        async def main():
            await takeoff_to(srv, "Drone1", 20.0)
            await srv.tick_once("Drone1")           # prime the last-known fix
            srv.sim.set_link_state("Drone1", "lost")
            end = time.monotonic() + 10.0
            while time.monotonic() < end:
                v = await srv.tick_once("Drone1")
                if (v.get("link_event") or {}).get("event") == "loal_declared":
                    break
                await asyncio.sleep(0.15)
            q = srv.tasking.queue_for("Drone1")
            end = time.monotonic() + 10.0
            while time.monotonic() < end and q.current is None:
                await asyncio.sleep(0.1)
            assert q.current is not None, "no lost-link RTB was submitted at all"
            task = q.current
            assert task.tool == "uav_return_to_home" and task.uncancellable

            # The old code failed here within milliseconds.
            end = time.monotonic() + 20.0
            while time.monotonic() < end and task.state.value not in (
                    "done", "failed", "cancelled"):
                await asyncio.sleep(0.1)
            assert task.state.value != "failed", (
                f"the lost-link RTB died on the lost link: {task.error}")
            assert task.state.value == "done", task.state.value
            # It flew blind, and it says so rather than claiming a landing.
            assert task.result["position_source"] == "last_known_fix"
            assert task.result["legs_unverified"], task.result
            assert task.result["touchdown"]["verified"] is False
            kinds = [r["kind"] for r in srv.store.audit.read_all()]
            assert "telemetry_degraded" in kinds
            assert "leg_unverified" in kinds
        try:
            run(main())
        finally:
            server_mod.BLIND_LEG_GRACE_S, server_mod.BLIND_LAND_S = old


def test_a_second_force_rtb_is_never_silently_suppressed(tmp_path):
    """`_rtb_active` was set and never cleared, so the SECOND force-RTB for a
    reason was a no-op: no task, no audit row, nothing.

    Combined with the lost-link RTB dying on its own trigger condition, that
    left an airborne vehicle permanently unrecoverable — and silently, which
    is the failure mode this codebase's hard rules exist to stop.
    """
    with build_server(tmp_path) as srv:
        async def main():
            await takeoff_to(srv, "Drone1", 20.0)
            first = await srv._force_rtb("Drone1", reason="lost_link", detail="one")
            assert first is not None
            q = srv.tasking.queue_for("Drone1")
            t1 = q.get(first["task_id"])
            end = time.monotonic() + 30.0
            while time.monotonic() < end and t1.state.value not in (
                    "done", "failed", "cancelled"):
                await asyncio.sleep(0.1)
            assert t1.state.value in ("done", "failed", "cancelled")

            # While it was still flying, a repeat is correctly a no-op...
            # ...but once it is OVER, the vehicle must be committable again.
            second = await srv._force_rtb("Drone1", reason="lost_link", detail="two")
            assert second is not None, (
                "the second force-RTB for the same reason returned None and "
                "logged nothing")
            assert second["task_id"] != first["task_id"]
            assert second["uncancellable"] is True
            kinds = [r["kind"] for r in srv.store.audit.read_all()]
            assert "force_rtb_resubmit" in kinds
        run(main())


def test_repeat_force_rtb_is_a_no_op_while_the_first_still_flies(tmp_path):
    """The de-duplication that DOES belong: one live safety transition."""
    with build_server(tmp_path) as srv:
        async def main():
            await takeoff_to(srv, "Drone1", 20.0)
            first = await srv._force_rtb("Drone1", reason="bingo", detail="one")
            assert first is not None
            t1 = srv.tasking.queue_for("Drone1").get(first["task_id"])
            assert t1.state.value not in ("done", "failed", "cancelled")
            assert await srv._force_rtb("Drone1", reason="bingo", detail="two") is None
        run(main())


def test_lost_link_climb_for_los_is_flown_autonomously(tmp_path):
    """M9's second behaviour: climb for line-of-sight, capped by the ceiling."""
    with build_server(tmp_path, lost_link_plan={"behaviour": "climb_for_los",
                                                "declare_after_s": 0.2,
                                                "climb_to_m": 500.0}) as srv:
        async def main():
            await takeoff_to(srv, "Drone1", 15.0)
            await srv.tick_once("Drone1")
            srv.sim.set_link_state("Drone1", "lost")
            end = time.monotonic() + 10.0
            while time.monotonic() < end:
                v = await srv.tick_once("Drone1")
                if (v.get("link_event") or {}).get("event") == "loal_declared":
                    break
                await asyncio.sleep(0.15)
            q = srv.tasking.queue_for("Drone1")
            end = time.monotonic() + 10.0
            while time.monotonic() < end and q.current is None:
                await asyncio.sleep(0.1)
            assert q.current is not None
            assert q.current.tool == "uav_goto_gps"
            assert q.current.params["reason"] == "lost_link_climb"
            # never above the envelope ceiling, even when the plan asks for more
            assert q.current.params["alt_m"] == srv.envelope.ceiling_m_agl
            assert q.current.uncancellable is True
        run(main())


def test_link_restored_is_logged_for_the_intrep(tmp_path):
    with build_server(tmp_path, lost_link_plan={"behaviour": "continue",
                                                "declare_after_s": 0.2,
                                                "restore_after_s": 0.2}) as srv:
        async def main():
            await srv.tick_once("Drone1")
            srv.sim.set_link_state("Drone1", "lost")
            end = time.monotonic() + 8.0
            while time.monotonic() < end:
                v = await srv.tick_once("Drone1")
                if (v.get("link_event") or {}).get("event") == "loal_declared":
                    break
                await asyncio.sleep(0.1)
            srv.sim.set_link_state("Drone1", "nominal")
            restored = None
            end = time.monotonic() + 8.0
            while time.monotonic() < end:
                v = await srv.tick_once("Drone1")
                if (v.get("link_event") or {}).get("event") == "link_restored":
                    restored = v["link_event"]
                    break
                await asyncio.sleep(0.15)
            assert restored is not None
            rep = await tool(srv, "uav_target_report")()
            events = [e["event"] for e in rep["loal_events"]]
            assert "loal_declared" in events and "link_restored" in events
        run(main())


# ----------------------------------------------- T2 / T4a / T4b: queue -----

def test_second_command_returns_busy_with_the_current_handle(server):
    async def main():
        first = await tool(server, "uav_takeoff")(vehicle="Drone1", alt_m=40.0)
        assert first["status"] == "accepted"
        second = await tool(server, "uav_hover")(vehicle="Drone1")
        assert second["status"] == "busy"
        assert second["current"]["task_id"] == first["task_id"]
        assert second["rejected_tool"] == "uav_hover"
        await server.tasking.queue_for("Drone1").abort()
    run(main())


def test_queued_task_is_pollable_before_it_starts(server):
    async def main():
        h = server._submit("Drone1", "uav_takeoff", {"alt_m": 20}, None)
        # no sleep: the worker has not dequeued it yet
        st = await tool(server, "uav_task_status")(vehicle="Drone1",
                                                   task_id=h["task_id"])
        assert st.get("error") is None
        assert st["task_id"] == h["task_id"]
        assert st["state"] in ("queued", "executing")
        await server.tasking.queue_for("Drone1").abort()
    run(main())


def test_idempotency_key_deduplicates_through_the_tool(server):
    async def main():
        a = await tool(server, "uav_takeoff")(vehicle="Drone1", alt_m=20.0,
                                              idempotency_key="key-1")
        b = await tool(server, "uav_takeoff")(vehicle="Drone1", alt_m=20.0,
                                              idempotency_key="key-1")
        assert b["task_id"] == a["task_id"]
        assert b["status"] == "duplicate" and b["idempotent_replay"] == 1
        submitted = [r for r in server.store.tasks.read_all()
                     if r.get("event") == "submitted" and r.get("tool") == "uav_takeoff"]
        assert len(submitted) == 1, "a replayed key re-executed"
        assert submitted[0]["idempotency_key"] == "key-1"
        # The journalled params carry the DATUMED spelling as well as the
        # legacy one, and they agree — a replayed command must not be
        # reconstructable only from a bare altitude (TOOL_CONTRACT).
        assert submitted[0]["params"] == {"alt_m": 20.0, "alt_agl_m": 20.0}
        await server.tasking.queue_for("Drone1").abort()
    run(main())


def test_progress_pct_is_derived_from_telemetry(server):
    """T4a: progress_pct was hardcoded 0.0 forever."""
    async def main():
        await takeoff_to(server, "Drone1", 12.0)
        h = await tool(server, "uav_goto_gps")(
            vehicle="Drone1", lat=HOME.latitude + 0.003, lon=HOME.longitude,
            alt_m=12.0, speed_mps=10.0)
        assert h["status"] == "accepted"
        task = server.tasking.queue_for("Drone1").get(h["task_id"])
        end = time.monotonic() + 30.0
        while time.monotonic() < end and task.progress_pct < 1.0:
            await asyncio.sleep(0.2)
        assert 1.0 <= task.progress_pct <= 100.0
        assert task.waypoint == 1 and task.waypoints_total == 1
        assert task.eta_s is not None and task.eta_s > 0.0
        st = await tool(server, "uav_task_status")(vehicle="Drone1",
                                                   task_id=h["task_id"])
        assert st["progress_pct"] > 0.0
        await server.tasking.queue_for("Drone1").abort()
    run(main())


def test_progress_notifications_use_the_callers_token(server):
    """T4a: notifications/progress carries the caller's progressToken."""
    class FakeCtx:
        def __init__(self):
            self.reports = []

        async def report_progress(self, progress, total=None, message=None):
            self.reports.append((progress, total, message))

    async def main():
        h = server._submit("Drone1", "uav_takeoff", {"alt_m": 25}, None)
        ctx = FakeCtx()
        end = time.monotonic() + 20.0
        while time.monotonic() < end:
            st = await tool(server, "uav_task_status")(
                vehicle="Drone1", task_id=h["task_id"], ctx=ctx)
            assert st.get("progress_notified") is True
            if ctx.reports and ctx.reports[-1][0] > 0.0:
                break
            await asyncio.sleep(0.2)
        assert ctx.reports and ctx.reports[-1][1] == 100.0
        assert ctx.reports[-1][0] > 0.0
        await server.tasking.queue_for("Drone1").abort()
    run(main())


def test_watchdog_is_no_longer_a_flat_cap_on_duration(server):
    """T2: a leg far longer than the watchdog window survives on progress.

    The watchdog is 3 s here; the leg takes >10 s. On the old flat
    total-duration watchdog this task died at t=3 s.
    """
    async def main():
        server.tasking.watchdog_s = 3.0
        server.tasking.queue_for("Drone1").watchdog_s = 3.0
        await takeoff_to(server, "Drone1", 12.0)
        h = await tool(server, "uav_goto_gps")(
            vehicle="Drone1", lat=HOME.latitude + 0.0012, lon=HOME.longitude,
            alt_m=12.0, speed_mps=10.0)
        t = await wait_state(server, "Drone1", h["task_id"], timeout_s=60.0)
        assert t.state.value == "done", t.error
        assert t.finished_at - t.started_at > 5.0
        assert t.progress_pct == 100.0
    run(main())


def test_watchdog_timeout_recovers_to_hover(server):
    """T2: 'fail handle -> hover-recovery' — nothing commanded hover before."""
    async def main():
        server.tasking.queue_for("Drone1").watchdog_s = 0.5
        h = server._submit("Drone1", "uav_goto_gps",
                           {"lat": HOME.latitude + 0.02, "lon": HOME.longitude,
                            "alt_m": 30.0, "speed_mps": 0.6}, None)
        t = await wait_state(server, "Drone1", h["task_id"], timeout_s=40.0)
        assert t.state.value == "failed"
        assert "watchdog" in t.error
        assert t.recovery == "hover"
        kinds = [r["kind"] for r in server.store.audit.read_all()]
        assert "watchdog_timeout" in kinds
    run(main())


# -------------------------------------------------- gate / envelope (M4) ---

def test_goto_gps_gate_rejects_outside_geofence(server):
    async def main():
        gate = await server._gate("Drone1", [{"lat": 47.70, "lon": -122.30, "alt_m": 50}], 10.0)
        assert gate["ok"] is False
        assert gate["envelope_violations"]
    run(main())


def test_goto_gps_gate_accepts_inside(server):
    async def main():
        gate = await server._gate("Drone1", [{"lat": 47.645, "lon": -122.145, "alt_m": 50}], 10.0)
        assert gate["ok"] is True
        assert gate["required_pct"] is not None
        assert gate["bingo_fuel_pct"] >= 20.0
    run(main())


def test_gate_enforces_max_speed(server):
    """M4 lists 'ceiling / max speed / min AGL'; only the first two were checked."""
    async def main():
        fast = await server._gate(
            "Drone1", [{"lat": 47.645, "lon": -122.145, "alt_m": 50}],
            server.envelope.max_speed_mps + 25.0)
        assert fast["ok"] is False
        assert any("max_speed" in v for v in fast["envelope_violations"])
        out = await tool(server, "uav_goto_gps")(
            vehicle="Drone1", lat=47.645, lon=-122.145, alt_m=50.0,
            speed_mps=server.envelope.max_speed_mps + 25.0)
        assert out["rejected"] is True
    run(main())


def test_gate_starts_from_live_telemetry_and_prices_the_wind(server):
    """Wave-1 handoff: _gate started every plan at envelope.home with no wind,
    omitting the ingress leg and the whole M15 penalty."""
    async def main():
        await takeoff_to(server, "Drone1", 12.0)
        far = server._submit("Drone1", "uav_goto_gps",
                             {"lat": HOME.latitude + 0.01, "lon": HOME.longitude,
                              "alt_m": 12.0, "speed_mps": 10.0}, None)
        end = time.monotonic() + 20.0
        while time.monotonic() < end:
            tele = await server.backend.telemetry("Drone1")
            if abs(tele["lat"] - HOME.latitude) > 0.002:
                break
            await asyncio.sleep(0.2)
        await server.tasking.queue_for("Drone1").abort()
        await asyncio.sleep(0.3)
        tele = await server.backend.telemetry("Drone1")
        # a long in-AO tour, so the M15 penalty is bigger than the rounding
        route = [{"lat": 47.655, "lon": -122.150, "alt_m": 40.0},
                 {"lat": 47.635, "lon": -122.150, "alt_m": 40.0},
                 {"lat": 47.655, "lon": -122.130, "alt_m": 40.0},
                 {"lat": 47.635, "lon": -122.130, "alt_m": 40.0}]
        calm = await server._gate("Drone1", route, 10.0)
        assert calm["ok"] is True
        assert calm["start"][0] == pytest.approx(tele["lat"], abs=1e-4)
        assert abs(calm["start"][0] - HOME.latitude) > 0.001  # NOT home
        assert calm["start_alt_agl_m"] > 5.0
        server.sim.set_wind(-15.0, 0.0, 0.0)
        windy = await server._gate("Drone1", route, 10.0)
        assert windy["wind_source"] in ("sim", "sim_rpc")
        assert windy["wind_ne_mps"][0] == pytest.approx(-15.0, abs=0.1)
        assert windy["required_pct"] > calm["required_pct"], "wind never reached the gate"
        assert windy["bingo_fuel_pct"] >= calm["bingo_fuel_pct"]
        assert any(leg["headwind_mps"] > 5.0
                   for leg in server.fuel_for("Drone1").estimate_route(
                       route, tuple(calm["start"]), 10.0,
                       wind_ne=(-15.0, 0.0))["legs"])
        assert far["state"] in ("queued", "executing", "cancelled")
    run(main())


# ------------------------------------------------- store + theater (E) -----

def test_theater_supplies_envelope_and_pattern_of_life_pois(tmp_path):
    with build_server(tmp_path, envelope=None, theater="iran-isfahan") as srv:
        t = theaters.get("iran-isfahan")
        assert srv.theater.id == "iran-isfahan"
        assert srv.envelope.geofence == t.ao_list()
        assert srv.envelope.home == t.home
        assert srv.fuel_for("Drone1").home == t.home
        assert sorted(srv.pol.pois()) == sorted(p.name for p in t.pois)


def test_submission_logs_params_and_idempotency_key(server):
    server._submit("Drone1", "uav_hover", {}, "hov-1")
    rows = [r for r in server.store.tasks.read_all() if r.get("event") == "submitted"]
    assert any(r.get("tool") == "uav_hover" and r.get("idempotency_key") == "hov-1"
               and r.get("params") == {} for r in rows)


def test_uav_mission_registers_and_journals_a_mission(server):
    async def main():
        box = [(47.6412, -122.1404), (47.6412, -122.1398),
               (47.6418, -122.1398), (47.6418, -122.1404)]
        h = await tool(server, "uav_mission")(
            vehicle="Drone1", kind="grid_search",
            params={"polygon": box, "alt_m": 40})
        assert h.get("rejected") is not True, h
        mid = h["mission_id"]
        assert mid in server.missions
        rows = [r for r in server.store.missions.read_all() if r["mission_id"] == mid]
        assert rows and rows[0]["event"] == "submitted"
        assert rows[0]["kind"] == "grid_search"
        assert rows[0]["bingo_fuel_pct"] >= 20.0
        await server.tasking.queue_for("Drone1").abort()
    run(main())


def test_boot_replay_recovers_an_interrupted_task_to_rth(tmp_path):
    """T4c: restart = replay -> resume-or-abort-and-RTH, acted on."""
    with Store(tmp_path) as seed:
        seed.log_task({"task_id": "old-1", "tool": "uav_fly_route",
                       "vehicle": "Drone1", "state": "executing"}, "started",
                      params={"waypoints": [{"lat": 47.645, "lon": -122.145,
                                             "alt_m": 30}], "speed_mps": 10.0})
    with build_server(tmp_path) as srv:
        assert srv.recovery.decision_for("Drone1") == "abort_and_rth"
        assert any("fuel" in r for r in srv.recovery.reasons_for("Drone1"))

        async def main():
            applied = await srv.apply_boot_recovery()
            assert applied and applied[0]["decision"] == "abort_and_rth"
            q = srv.tasking.queue_for("Drone1")
            end = time.monotonic() + 10.0
            while time.monotonic() < end and q.current is None:
                await asyncio.sleep(0.1)
            assert q.current is not None
            assert q.current.tool == "uav_return_to_home"
            assert q.current.uncancellable is True
            kinds = [r["kind"] for r in srv.store.audit.read_all()]
            assert "restart_recovery" in kinds and "force_rtb" in kinds
        run(main())


def test_boot_replay_resumes_when_the_reserve_is_provably_intact(tmp_path):
    with Store(tmp_path) as seed:
        seed.log_task({"task_id": "old-2", "tool": "uav_hover",
                       "vehicle": "Drone1", "state": "executing"}, "started",
                      params={"reason": "interrupted mission"})
        seed.log_fuel("Drone1", 88.0, "cruise", bingo_fuel_pct=25.0)
    with build_server(tmp_path) as srv:
        assert srv.recovery.decision_for("Drone1") == "resume"

        async def main():
            applied = await srv.apply_boot_recovery()
            assert applied and applied[0]["decision"] == "resume"
            assert applied[0]["task"]["tool"] == "uav_hover"
            rows = [r for r in srv.store.tasks.read_all() if r.get("event") == "resumed"]
            assert rows and rows[0]["recovered_from"] == "old-2"
        run(main())


def test_boot_replay_reseeds_idempotency_keys(tmp_path):
    """T4b must survive a restart: a replayed key returns the ORIGINAL handle."""
    with Store(tmp_path) as seed:
        seed.log_task({"task_id": "done-7", "tool": "uav_takeoff",
                       "vehicle": "Drone1", "state": "done"}, "submitted",
                      params={"alt_m": 20}, idempotency_key="restart-key")
        seed.log_task({"task_id": "done-7", "tool": "uav_takeoff",
                       "vehicle": "Drone1", "state": "done"}, "completed",
                      idempotency_key="restart-key")
        seed.log_fuel("Drone1", 90.0, "ground", bingo_fuel_pct=25.0)
    with build_server(tmp_path) as srv:
        async def main():
            again = await tool(srv, "uav_takeoff")(vehicle="Drone1", alt_m=20.0,
                                                   idempotency_key="restart-key")
            assert again["task_id"] == "done-7"
            assert again["status"] == "duplicate"
            assert srv.tasking.queue_for("Drone1").pending() == 0
        run(main())


# ------------------------------------------------ intel wiring (M8-M13) ----

def test_scan_passes_observer_sensor_and_frame_into_the_track_manager(server):
    async def main():
        await tool(server, "sim_spawn_target")(
            name="SA-6_site_1", mesh="sam", lat=47.6417, lon=-122.1402)
        out = await tool(server, "uav_scan_targets")(vehicle="Drone1")
        assert out["detections"] >= 1
        assert out["observer"]["vehicle"] == "Drone1"
        assert out["sensor"]["fov_deg"] == pytest.approx(90.0)
        assert out["frame_id"].startswith("Drone1:0:")
        track = server.tracks.get(out["tracks_updated"][0])
        obs = track.observations[-1]
        assert obs.frame_id == out["frame_id"]
        assert obs.observer == "Drone1"
        # pixels-on-target MEASURED from the detection box, not guessed
        assert obs.pixels_on_target is not None and obs.pixels_on_target > 0
        assert obs.slant_range_m is not None
        assert obs.fov_deg == pytest.approx(90.0)
        rep = out["tracks"][0]
        # the confidence model cites the MEASURED box, not a slant-range guess
        assert any("measured pixels" in json.dumps(e)
                   for e in rep["confidence"]["evidence"]), rep["confidence"]["evidence"]
    run(main())


def test_salute_element_aggregation_uses_peers(server):
    """M8: the peers argument was never passed, so aggregation never fired."""
    async def main():
        for i, (dlat, dlon) in enumerate(((0.0004, 0.0), (0.0016, 0.0))):
            await tool(server, "sim_spawn_target")(
                name=f"T72_tank_{i}", mesh="tank",
                lat=HOME.latitude + dlat, lon=HOME.longitude + dlon)
        out = await tool(server, "uav_scan_targets")(vehicle="Drone1")
        sizes = [t["size"]["count"] for t in out["tracks"]]
        assert max(sizes) >= 2, [t["size"] for t in out["tracks"]]
        assert any(len(t["size"]["members"]) >= 2 for t in out["tracks"])
        assert any("x" in t["size"]["text"] for t in out["tracks"])
    run(main())


def test_threat_assessment_consumes_the_pattern_of_life_store(server):
    """M12/M13: indicator 3 of 4 reported 'no_store' forever — no PoL existed."""
    async def main():
        await tool(server, "sim_spawn_target")(
            name="SA-6_site_2", mesh="sam", lat=47.6417, lon=-122.1402)
        out = await tool(server, "uav_scan_targets")(vehicle="Drone1")
        tid = out["tracks_updated"][0]
        assert server.pol.pois(), "no pattern-of-life POIs defined"
        res = await tool(server, "uav_assess_threat")(vehicle="Drone1", track_id=tid)
        pol_ind = next(i for i in res["assessment"]["intent"]["indicators"]
                       if i["indicator"] == "pattern_of_life_deviation")
        assert pol_ind["state"] != "no_store"
        # persisted (M12): the store survives a restart
        assert server.store.tracks.get(tid) is not None
        area = await tool(server, "uav_assess_threat")(vehicle="Drone1")
        assert area["format"] == "THREATREP"
        assert area["scoped_by_polygon"] is True
    run(main())


def test_intrep_has_every_required_section(server):
    """M8/§4.7: the report was a track-count roll-up with no mission context."""
    async def main():
        box = [(47.6412, -122.1404), (47.6412, -122.1398),
               (47.6418, -122.1398), (47.6418, -122.1404)]
        h = await tool(server, "uav_mission")(
            vehicle="Drone1", kind="grid_search",
            params={"polygon": box, "alt_m": 40})
        assert h.get("rejected") is not True, h
        await tool(server, "sim_spawn_target")(
            name="radar_1", mesh="radar", lat=47.6415, lon=-122.1402)
        await asyncio.sleep(0.5)
        await tool(server, "uav_scan_targets")(vehicle="Drone1")
        rep = await tool(server, "uav_target_report")()
        assert rep["format"] == "INTREP"
        assert rep["mission_id"] == h["mission_id"]
        assert rep["mission_summary"]["kind"] == "grid_search"
        assert rep["mission_summary"]["vehicle"] == "Drone1"
        assert rep["mission_summary"]["duration_s"] is not None
        assert rep["coverage"]["coverage_pct"] is not None
        assert rep["coverage"]["waypoints_planned"] >= 4
        assert rep["sensor_conditions"]["light"] in ("day", "night")
        assert isinstance(rep["loal_events"], list)
        assert rep["pattern_of_life"] is not None
        assert rep["total_tracks"] >= 1
        await server.tasking.queue_for("Drone1").abort()
    run(main())


def test_detections_keep_the_box_and_relative_pose(server):
    async def main():
        await tool(server, "sim_spawn_target")(
            name="T72_tank_9", mesh="tank", lat=47.6416, lon=-122.1402)
        dets = await server.backend.get_detections("Drone1")
        assert dets
        d = dets[0]
        assert d["box2D"] is not None
        assert d["box2D"]["width_px"] > 0 and d["box2D"]["height_px"] > 0
        assert d["pixels_on_target"] == pytest.approx(
            max(d["box2D"]["width_px"], d["box2D"]["height_px"]))
        assert d["relative_pose_ned"] is not None
        assert d["slant_range_m"] == pytest.approx(
            math.dist((0, 0, 0), d["relative_pose_ned"]), abs=0.01)
    run(main())


def test_spawn_target_defaults_to_theater_ground_not_zero(server):
    async def main():
        out = await tool(server, "sim_spawn_target")(
            name="bunker_1", mesh="structure", lat=47.6415, lon=-122.1402)
        assert out["alt_msl_m"] == server.theater.home_alt_msl_m
        assert out["alt_msl_m"] != 0.0
        assert out["alt_hae_m"] == pytest.approx(
            canonical_altitude(out["alt_msl_m"], 47.6415, -122.1402,
                               datum="msl").alt_hae, abs=0.01)
    run(main())


# -------------------------------------------------------- legacy paths -----

def test_takeoff_then_land_via_queue(server):
    async def main():
        h = server._submit("Drone1", "uav_takeoff", {"alt_m": 20}, "tk-1")
        assert h["state"] in ("queued", "executing")
        t = await wait_state(server, "Drone1", h["task_id"], timeout_s=40.0)
        assert t.state.value == "done", t.error
        tele = await server.backend.telemetry("Drone1")
        assert tele["alt_agl_m"] > 15.0, tele
        assert tele["alt_hae_m"] > HOME.altitude + 15.0
        h2 = server._submit("Drone1", "uav_land", {}, None)
        t2 = await wait_state(server, "Drone1", h2["task_id"], timeout_s=60.0)
        assert t2.state.value == "done", t2.error
    run(main())


def test_abort_clears_and_hovers(server):
    async def main():
        server._submit("Drone1", "uav_fly_route", {"waypoints": [
            {"lat": 47.64148, "lon": -122.14018, "alt_m": 30},
            {"lat": 47.64150, "lon": -122.14020, "alt_m": 30},
        ], "speed_mps": 15.0}, None)
        await asyncio.sleep(0.5)
        out = await tool(server, "uav_abort")(vehicle="Drone1")
        assert out["aborted"] is True
        await asyncio.sleep(0.3)
        q = server.tasking.queue_for("Drone1")
        assert q.pending() <= 1  # only the hover the abort queued
        assert any(r.get("kind") == "abort" for r in server.store.audit.read_all())
    run(main())


def test_rtb_while_landed_at_home_is_a_no_op(server):
    """An RTB that takes off in order to land again is a hazard, not a recovery."""
    async def main():
        h = server._submit("Drone1", "uav_return_to_home", {"speed_mps": 10.0}, None)
        t = await wait_state(server, "Drone1", h["task_id"], timeout_s=30.0)
        assert t.state.value == "done", t.error
        assert t.result.get("no_op") == "already at home and landed"
        tele = await server.backend.telemetry("Drone1")
        assert tele["alt_agl_m"] < 1.0, "the vehicle left the ground"
    run(main())


def test_empty_route_fails_loudly_instead_of_stalling_the_watchdog(server):
    async def main():
        h = server._submit("Drone1", "uav_fly_route",
                           {"waypoints": [], "speed_mps": 10.0}, None)
        t = await wait_state(server, "Drone1", h["task_id"], timeout_s=30.0)
        assert t.state.value == "failed"
        assert "no waypoints" in t.error
    run(main())


def test_telemetry_includes_fuel_and_bingo(server):
    async def main():
        tele = await tool(server, "uav_get_telemetry")(vehicle="Drone1")
        assert 0 < tele["fuel_pct"] <= 100
        assert tele["bingo_fuel_pct"] >= 20.0
        assert abs(tele["lat"] - HOME.latitude) < 0.01
        assert tele["queue"]["vehicle"] == "Drone1"
        assert tele["link"]["state"] in ("up", "degraded", "pending", "loal")
        assert tele["wind_source"] in ("sim", "sim_rpc", "commanded", "unset")
    run(main())


# ==========================================================================
# Wave-2 CORE stage 2 — the full §4.1-§4.4 tool catalog and the §4.8
# resources. Measured baseline before this stage: 19 tools, 0 resources (R6).
# ==========================================================================

#: Streamable-HTTP ports for this stage (sims keep the 47000-47099 cycle).
_HTTP_PORTS = list(range(47100, 47150))

#: Every tool TOOL_CONTRACT §4.1-§4.4 names. None of the starred ones existed.
CONTRACT_TOOLS = {
    # §4.1 flight control
    "uav_takeoff", "uav_land", "uav_return_to_home", "uav_goto_gps",
    "uav_fly_route", "uav_orbit_poi", "uav_hover", "uav_abort",
    "uav_set_gimbal", "uav_set_fov",
    # §4.2 sensors / intel
    "uav_capture_image", "uav_get_telemetry", "uav_get_detections",
    "uav_los_check", "uav_list_vehicles", "uav_list_tracks",
    # §4.3 mission primitives
    "mission_grid_search", "mission_recon_route", "mission_track_target",
    "mission_identify_target", "mission_threat_assessment",
    "mission_handoff_track", "mission_status", "mission_cancel",
    "mission_dry_run",
    # §4.4 scenario / sim admin
    "sim_spawn_target", "sim_move_target", "sim_set_time", "sim_set_weather",
    "sim_set_link_state", "sim_set_gps_degradation", "sim_reset",
}

#: PLAN §4.8 / TOOL_CONTRACT §4.8. All eight were missing (R6).
CONTRACT_RESOURCES = {
    "uav://{vehicle}/telemetry", "uav://{vehicle}/camera/{name}/{type}",
    "uav://mission/{id}", "uav://tracks", "uav://targets",
    "uav://safety/geofence", "uav://reports/{id}",
    "uav://pattern-of-life/{poi}",
}


def schema_of(srv, name) -> dict:
    return srv.mcp._tool_manager._tools[name].parameters.get("properties", {})


async def read_resource(srv, uri):
    items = list(await srv.mcp.read_resource(uri))
    assert items, f"{uri} produced no content"
    return items[0]


async def read_json(srv, uri) -> dict:
    return json.loads((await read_resource(srv, uri)).content)


async def spawn_and_scan(srv, *, ob_class="supply_truck", dlat=0.0006, dlon=0.0,
                         scans=1):
    """Ground truth + N sensor frames -> a real persistent track.

    `scans` matters: M8 confidence is capped by the number of sightings, so a
    single frame can only ever be 'possible'.
    """
    out = await tool(srv, "sim_spawn_target")(
        lat=HOME.latitude + dlat, lon=HOME.longitude + dlon, ob_class=ob_class)
    assert out.get("error") is None, out
    ids: list[str] = []
    for _ in range(scans):
        scan = await tool(srv, "uav_get_detections")(vehicle="Drone1")
        ids = [d["track_id"] for d in scan["detections"] if d.get("track_id")]
        assert ids, scan
        await asyncio.sleep(0.05)
    return out["target_id"], ids[0]


# ------------------------------------------------- catalog coverage (R6) ---

def test_every_contract_tool_is_published(server):
    """TOOL_CONTRACT §4.1-§4.4. Measured baseline: 19 tools, 14 of these absent."""
    published = set(server.mcp._tool_manager._tools)
    missing = sorted(CONTRACT_TOOLS - published)
    assert not missing, f"tools named by TOOL_CONTRACT but not published: {missing}"


def test_no_kinetic_tool_exists(server):
    """M14: ISR-only. The catalog grew a lot; it must not have grown teeth."""
    banned = ("fire", "strike", "engage", "weapon", "launch_missile", "attack",
              "prosecute", "designate_for_strike", "release")
    for name in server.mcp._tool_manager._tools:
        assert not any(b in name.lower() for b in banned), name


def test_every_altitude_parameter_names_its_datum(server):
    """TOOL_CONTRACT: 'Never a bare alt_m'.

    The legacy `alt_m` spelling survives on the tools the GEV panel already
    calls, but only where the tool DESCRIPTION states which datum it is; every
    other altitude parameter has the datum in its own name.
    """
    offenders = []
    for name, spec in server.mcp._tool_manager._tools.items():
        desc = (spec.description or "").lower()
        for param in spec.parameters.get("properties", {}):
            if not param.startswith("alt") and not param.endswith("_m"):
                continue
            if not param.startswith("alt"):
                continue
            if param in ("alt_agl_m", "alt_msl_m", "alt_hae_m"):
                continue
            if param == "alt_m" and ("agl" in desc or "msl" in desc):
                continue
            offenders.append(f"{name}.{param}")
    assert not offenders, f"altitude parameters with no datum: {offenders}"


def test_every_mutating_tool_accepts_an_idempotency_key(server):
    """TOOL_CONTRACT: 'every mutating tool accepts idempotency_key'."""
    read_only = {"uav_get_telemetry", "uav_list_vehicles", "uav_list_tracks",
                 "uav_list_ob_classes", "uav_target_report", "uav_task_status",
                 "uav_identify_target", "uav_get_detections", "uav_los_check",
                 "uav_assess_threat", "uav_scan_targets", "mission_status",
                 "mission_dry_run", "uav_abort",
                 # Real-world data reads: they observe, they change nothing.
                 "uav_real_data_status", "uav_deconflict_airspace"}
    missing = [n for n in server.mcp._tool_manager._tools
               if n not in read_only and "idempotency_key" not in schema_of(server, n)]
    assert not missing, f"mutating tools with no idempotency_key: {missing}"


# --------------------------------------------------------- §4.8 resources --

def test_all_eight_contract_resources_are_published(server):
    """R6: 'the server exposes 0 resources' — measured over the wire."""
    async def main():
        static = {str(r.uri) for r in await server.mcp.list_resources()}
        templates = {t.uri_template for t in await server.mcp.list_resource_templates()}
        published = static | templates
        missing = sorted(CONTRACT_RESOURCES - published)
        assert not missing, f"PLAN §4.8 resources still missing: {missing}"
    run(main())


def test_geofence_resource_publishes_the_envelope_the_server_enforces(server):
    """PLAN §4.5: a skill's ROE 'may only be stricter' — impossible to honour
    without being able to READ the envelope. This is the resource that matters
    most, and it did not exist."""
    async def main():
        env = await read_json(server, "uav://safety/geofence")
        assert [tuple(p) for p in env["geofence"]] == list(AO)
        assert env["ceiling_m_agl"] == server.envelope.ceiling_m_agl
        assert env["min_agl_m"] == server.envelope.min_agl_m
        assert env["max_speed_mps"] == server.envelope.max_speed_mps
        assert env["home_datums"]["lat"] == pytest.approx(HOME.latitude)
        # both datums for home, from the one conversion point (T1)
        assert env["home_datums"]["alt_hae_m"] == pytest.approx(
            canonical_altitude(HOME.altitude, HOME.latitude, HOME.longitude,
                               datum="msl").alt_hae, abs=0.01)
        assert "stricter" in env["roe"].lower()
        assert "M14" in env["isr_only"]
        assert env["lost_link_plan"]["behaviour"]

        # and the published fence is the ENFORCED one: a point the resource
        # says is outside really is rejected by the gate.
        outside = await tool(server, "uav_goto_gps")(
            vehicle="Drone1", lat=47.70, lon=-122.30, alt_m=50.0)
        assert outside["rejected"] is True
        assert any("geofence" in v for v in outside["gate"]["envelope_violations"])
    run(main())


def test_telemetry_resource_is_the_same_payload_as_the_tool(server):
    async def main():
        res = await read_json(server, "uav://Drone1/telemetry")
        tele = await tool(server, "uav_get_telemetry")(vehicle="Drone1")
        for key in ("alt_hae_m", "alt_msl_m", "alt_agl_m", "undulation_m",
                    "datum_source", "bingo_fuel_pct"):
            assert key in res, key
        assert res["resource"] == "uav://Drone1/telemetry"
        assert res["lat"] == pytest.approx(tele["lat"], abs=1e-6)
        assert res["bingo_fuel_pct"] == pytest.approx(tele["bingo_fuel_pct"], abs=0.5)
    run(main())


def test_camera_resource_serves_real_png_bytes(server):
    async def main():
        item = await read_resource(server, "uav://Drone1/camera/0/scene")
        assert isinstance(item.content, bytes)
        assert item.content[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
        assert item.mime_type == "image/png"
        # the tool and the resource describe the SAME cached frame
        meta = await tool(server, "uav_capture_image")(vehicle="Drone1")
        again = await read_resource(server, "uav://Drone1/camera/0/scene")
        assert meta["sha256"] == hashlib.sha256(again.content).hexdigest()
    run(main())


def test_camera_resource_refuses_an_unknown_image_type(server):
    async def main():
        with pytest.raises(Exception) as exc:
            await server.mcp.read_resource("uav://Drone1/camera/0/thermal_x")
        assert "thermal_x" in str(exc.value) or "unknown image type" in str(exc.value)
    run(main())


def test_targets_resource_is_ground_truth_and_tracks_is_what_was_derived(server):
    """§4.8 lists both, and they are deliberately different things."""
    async def main():
        target_id, track_id = await spawn_and_scan(server)
        targets = await read_json(server, "uav://targets")
        assert targets["count"] == 1
        row = targets["targets"][0]
        assert row["target_id"] == target_id
        assert row["ob_class"] == "supply_truck"
        assert row["alt_msl_m"] == server.theater.home_alt_msl_m
        assert row["current"]["lat"] == pytest.approx(row["lat"], abs=1e-4)

        tracks = await read_json(server, "uav://tracks")
        assert tracks["count"] >= 1
        ids = [t["track_id"] for t in tracks["tracks"]]
        assert track_id in ids
        assert tracks["tracks"][0]["confidence"]["level"] in (
            "possible", "probable", "confirmed")
        # a track id is NOT a target id: one is derived, the other is truth
        assert track_id != target_id
    run(main())


def test_mission_resource_carries_the_plan_the_status_and_the_journal(server):
    async def main():
        box = [(47.6412, -122.1404), (47.6412, -122.1398),
               (47.6418, -122.1398), (47.6418, -122.1404)]
        h = await tool(server, "mission_grid_search")(
            vehicle="Drone1", polygon=box, alt_agl_m=40.0, overlap_pct=20.0)
        assert h.get("rejected") is not True, h
        mid = h["mission_handle"]
        res = await read_json(server, f"uav://mission/{mid}")
        assert res["mission"]["kind"] == "grid_search"
        assert res["mission"]["meta"]["lane_spacing_m"] > 0
        assert res["status"]["mission_handle"] == mid
        assert res["status"]["state"] in ("queued", "executing", "done")
        assert res["journal"] and res["journal"][0]["event"] == "submitted"
        await server.tasking.queue_for("Drone1").abort()
    run(main())


def test_mission_resource_fails_loudly_on_an_unknown_id(server):
    async def main():
        with pytest.raises(Exception):
            await server.mcp.read_resource("uav://mission/MSN-nope")
    run(main())


def test_reports_resource_serves_the_intrep(server):
    async def main():
        await spawn_and_scan(server)
        rep = await tool(server, "uav_target_report")()
        assert rep["format"] == "INTREP"
        res = await read_json(server, f"uav://reports/{rep['report_id']}")
        assert res["format"] == "INTREP"
        assert res["total_tracks"] == rep["total_tracks"]
        latest = await read_json(server, "uav://reports/latest")
        assert latest["report_id"] == rep["report_id"]
    run(main())


def test_pattern_of_life_resource_exposes_the_m12_store(server):
    async def main():
        allp = await read_json(server, "uav://pattern-of-life/all")
        assert allp["count"] == len(server.pol.pois())
        poi = server.pol.pois()[0]
        one = await read_json(server, f"uav://pattern-of-life/{poi}")
        assert one["poi"] == poi
        assert one["baseline"]["poi"] == poi
        assert "deviation" in one
        with pytest.raises(Exception):
            await server.mcp.read_resource("uav://pattern-of-life/not-a-poi")
    run(main())


# ------------------------------------------- §4.1 orbit / gimbal / FOV -----

def test_orbit_poi_flies_the_sun_side_arc_and_says_why(server):
    """M3/M6: 'choose the orbit arc that keeps the sun behind the sensor, and
    say in the return which arc it chose and why'."""
    async def main():
        clock = await tool(server, "sim_set_time")(
            datetime="2026-06-21 06:00:00", clock_speed=0.0)
        az = clock["sun"]["azimuth_deg"]
        assert clock["sun"]["elevation_deg"] > 0.0
        out = await tool(server, "uav_orbit_poi")(
            vehicle="Drone1", lat=47.6430, lon=-122.1402, radius_m=150.0,
            alt_agl_m=50.0, dry_run=True)
        side = out["sun_side"]
        assert side["applied"] is True
        assert side["arc_start_bearing_from_poi_deg"] == pytest.approx(az, abs=1.0)
        assert out["waypoints"][0]["bearing_from_poi_deg"] == pytest.approx(az, abs=1.0)
        # the sensor looks back across the POI, i.e. AWAY from the sun
        assert side["sensor_look_bearing_deg"] == pytest.approx((az + 180.0) % 360.0,
                                                                abs=1.0)
        assert "sun" in side["reason"] and "behind the sensor" in side["reason"]

        # a different sun -> a different arc, so this is really derived
        clock2 = await tool(server, "sim_set_time")(
            datetime="2026-06-21 12:00:00", clock_speed=0.0)
        out2 = await tool(server, "uav_orbit_poi")(
            vehicle="Drone1", lat=47.6430, lon=-122.1402, radius_m=150.0,
            alt_agl_m=50.0, dry_run=True)
        assert out2["sun_side"]["arc_start_bearing_from_poi_deg"] == pytest.approx(
            clock2["sun"]["azimuth_deg"], abs=1.0)
        assert abs(out2["sun_side"]["arc_start_bearing_from_poi_deg"]
                   - side["arc_start_bearing_from_poi_deg"]) > 45.0
    run(main())


def test_orbit_poi_refuses_to_invent_a_sun_side_at_night(server):
    """No silent fallback: below the horizon there IS no sun side, and the
    return says so instead of justifying an arbitrary arc with 'sun-side'."""
    async def main():
        clock = await tool(server, "sim_set_time")(
            datetime="2026-06-21 00:30:00", clock_speed=0.0)
        assert clock["sun"]["elevation_deg"] < 0.0
        out = await tool(server, "uav_orbit_poi")(
            vehicle="Drone1", lat=47.6430, lon=-122.1402, radius_m=150.0,
            alt_agl_m=50.0, dry_run=True)
        side = out["sun_side"]
        assert side["applied"] is False
        assert "horizon" in side["reason"]
        assert side["sun_elevation_deg"] < 0.0
        assert out["waypoints"][0]["bearing_from_poi_deg"] == pytest.approx(0.0, abs=0.1)
    run(main())


def test_orbit_direction_cw_and_ccw_fly_opposite_arcs(server):
    async def main():
        kw = dict(vehicle="Drone1", lat=47.6430, lon=-122.1402, radius_m=150.0,
                  alt_agl_m=50.0, points=12, sun_side=False, dry_run=True)
        cw = await tool(server, "uav_orbit_poi")(direction="cw", **kw)
        ccw = await tool(server, "uav_orbit_poi")(direction="ccw", **kw)
        assert cw["direction"] == "cw" and ccw["direction"] == "ccw"
        assert cw["waypoints"][0] == ccw["waypoints"][0]      # same entry point
        assert cw["waypoints"][1]["bearing_from_poi_deg"] == pytest.approx(30.0, abs=0.5)
        assert ccw["waypoints"][1]["bearing_from_poi_deg"] == pytest.approx(330.0, abs=0.5)
        bad = await tool(server, "uav_orbit_poi")(direction="widdershins", **kw)
        assert bad["error"]["code"] == "invalid_mission_params"
    run(main())


def test_orbit_laps_repeat_the_ring(server):
    async def main():
        out = await tool(server, "uav_orbit_poi")(
            vehicle="Drone1", lat=47.6430, lon=-122.1402, radius_m=150.0,
            alt_agl_m=50.0, points=8, laps=3, dry_run=True)
        assert out["laps"] == 3
        assert out["waypoint_count"] == 24
        assert out["waypoints"][0]["lat"] == pytest.approx(out["waypoints"][8]["lat"])
    run(main())


def test_orbit_camera_track_slews_the_gimbal_onto_the_poi(server):
    """M3: the sensor must actually be looking at the thing being orbited."""
    async def main():
        out = await tool(server, "uav_orbit_poi")(
            vehicle="Drone1", lat=47.6430, lon=-122.1402, radius_m=100.0,
            alt_agl_m=100.0, direction="cw", camera_track=True)
        assert out.get("rejected") is not True, out
        track = out["camera_track"]
        assert track["applied"] is True
        # 100 m up, 100 m out -> 45 deg depression, POI 90 deg to starboard
        assert track["depression_deg"] == pytest.approx(45.0, abs=0.5)
        assert track["pitch_deg"] == pytest.approx(-45.0, abs=0.5)
        assert track["yaw_deg"] == 90.0
        # read back from the SIM, not echoed
        assert track["camera_state"]["pitch_deg"] == pytest.approx(-45.0, abs=1.0)
        assert server.sim.camera("Drone1", "0").pitch_deg == pytest.approx(-45.0, abs=1.0)
        await server.tasking.queue_for("Drone1").abort()
    run(main())


def test_set_gimbal_derives_its_angles_from_a_geo_point(server):
    async def main():
        await takeoff_to(server, "Drone1", 60.0)
        out = await tool(server, "uav_set_gimbal")(
            vehicle="Drone1", camera="0",
            track_geo_point={"lat": HOME.latitude + 0.0009,
                             "lon": HOME.longitude, "alt_agl_m": 0.0})
        assert out["ok"] is True
        d = out["derived_from"]
        assert d["ground_range_m"] == pytest.approx(100.0, abs=15.0)
        assert d["height_above_target_m"] == pytest.approx(60.0, abs=3.0)
        assert d["bearing_to_target_deg"] == pytest.approx(0.0, abs=2.0)
        assert d["pitch_deg"] == pytest.approx(
            -math.degrees(math.atan2(d["height_above_target_m"],
                                     d["ground_range_m"])), abs=0.1)
        assert d["target"]["alt_msl_m"] == pytest.approx(
            canonical_altitude(HOME.altitude, HOME.latitude, HOME.longitude,
                               datum="hae").alt_msl, abs=0.5)
        assert out["camera_state"]["pitch_deg"] == pytest.approx(d["pitch_deg"], abs=1.0)
    run(main())


def test_set_gimbal_needs_angles_or_a_point(server):
    async def main():
        out = await tool(server, "uav_set_gimbal")(vehicle="Drone1")
        assert out["error"]["code"] == "missing_parameter"
    run(main())


def test_set_fov_is_read_back_and_drives_the_footprint(server):
    """M7: without this the wide->narrow cross-cue cannot happen at all."""
    async def main():
        await takeoff_to(server, "Drone1", 60.0)
        wide = await tool(server, "uav_set_fov")(vehicle="Drone1", fov_deg=90.0)
        narrow = await tool(server, "uav_set_fov")(vehicle="Drone1", fov_deg=20.0)
        assert wide["fov_deg"] == 90.0 and narrow["fov_deg"] == 20.0
        # read back from the sim, and the sim really changed
        assert server.sim.camera("Drone1", "0").fov_deg == 20.0
        assert narrow["camera_state"]["source"] == "simGetCameraInfo"
        # a narrower field is a narrower swath and a finer ground sample
        assert narrow["swath_m"] < wide["swath_m"]
        assert narrow["gsd_m_per_px_at_nadir"] < wide["gsd_m_per_px_at_nadir"]
        assert narrow["swath_m"] == pytest.approx(
            2 * narrow["alt_agl_m"] * math.tan(math.radians(20.0) / 2), abs=0.5)
        bad = await tool(server, "uav_set_fov")(vehicle="Drone1", fov_deg=400.0)
        assert bad["error"]["code"] == "invalid_parameter"
        assert server.sim.camera("Drone1", "0").fov_deg == 20.0
    run(main())


def test_set_fov_idempotency_key_does_not_re_execute(server):
    """T4b: 'a key accepted-and-ignored is a defect'."""
    async def main():
        first = await tool(server, "uav_set_fov")(vehicle="Drone1", fov_deg=35.0,
                                                  idempotency_key="fov-1")
        assert first["status"] == "accepted"
        await tool(server, "uav_set_fov")(vehicle="Drone1", fov_deg=70.0)
        assert server.sim.camera("Drone1", "0").fov_deg == 70.0
        replay = await tool(server, "uav_set_fov")(vehicle="Drone1", fov_deg=35.0,
                                                   idempotency_key="fov-1")
        assert replay["status"] == "duplicate"
        assert replay["idempotent_replay"] is True
        assert replay["fov_deg"] == 35.0            # the ORIGINAL result
        assert server.sim.camera("Drone1", "0").fov_deg == 70.0, "the replay re-executed"
    run(main())


# -------------------------------------------- §4.2 imagery / LOS / dets ----

def test_capture_image_returns_a_resource_ref_geo_pose_and_sun_angle(server):
    async def main():
        await takeoff_to(server, "Drone1", 45.0)
        out = await tool(server, "uav_capture_image")(vehicle="Drone1", camera="0",
                                                      type="scene", jpeg_quality=75)
        assert out["resource"] == "uav://Drone1/camera/0/scene"
        assert out["bytes"] > 0 and out["width"] > 0 and out["height"] > 0
        pose = out["geo_pose"]
        # all three datums, from the one conversion point (T1)
        assert abs((pose["alt_hae_m"] - pose["alt_msl_m"]) - pose["undulation_m"]) < 0.01
        assert pose["alt_agl_m"] == pytest.approx(45.0, abs=3.0)
        assert pose["lat"] == pytest.approx(HOME.latitude, abs=1e-3)
        assert out["sun_elevation_deg"] is not None
        assert out["sun"]["source"] in ("simGetSunPosition", "simGetEnvironment")
        # jpeg_quality is REPORTED as not applied, not silently swallowed
        assert out["jpeg_quality_requested"] == 75
        assert out["jpeg_quality_applied"] is False
        assert "NOT applied" in out["encoding_note"]
        bad = await tool(server, "uav_capture_image")(vehicle="Drone1", type="lidar")
        assert bad["error"]["code"] == "invalid_parameter"
    run(main())


def test_get_detections_carries_persistent_track_ids_and_confidence(server):
    """§4.2: 'DetectionInfo[] + persistent track_ids (M11) + per-contact
    confidence'. The only scan tool before this returned a COUNT."""
    async def main():
        await tool(server, "sim_spawn_target")(
            lat=HOME.latitude + 0.0005, lon=HOME.longitude, ob_class="mbt")
        first = await tool(server, "uav_get_detections")(vehicle="Drone1")
        assert first["count"] >= 1
        d = first["detections"][0]
        assert d["track_id"] and d["track_id"].startswith("TRK-")
        assert d["confidence"]["level"] in ("possible", "probable", "confirmed")
        assert d["confidence"]["evidence"]
        assert d["ob_class"] == "mbt"
        assert d["box2D"]["width_px"] > 0
        assert d["pixels_on_target"] > 0
        assert d["slant_range_m"] > 0
        # the SAME contact keeps the SAME id on the next frame (M11)
        second = await tool(server, "uav_get_detections")(vehicle="Drone1")
        assert second["detections"][0]["track_id"] == d["track_id"]
        assert second["detections"][0]["sightings"] > d["sightings"]
    run(main())


def test_los_check_reports_the_model_and_is_blocked_by_an_obstruction(server):
    """§4.2: 'must never return unconditional true; report the model used'."""
    async def main():
        far_lat = HOME.latitude + 0.0027          # ~300 m north
        clear = await tool(server, "uav_los_check")(
            vehicle="Drone1", lat=far_lat, lon=HOME.longitude, alt_agl_m=0.0)
        assert clear["los"] is True
        assert "earth-curvature horizon" in clear["model"]
        assert "Does NOT model terrain" in clear["model"]
        assert clear["first_obstacle"] is None
        assert clear["ground_range_m"] == pytest.approx(300.0, abs=25.0)
        assert clear["target"]["alt_hae_m"] == pytest.approx(HOME.altitude, abs=0.01)

        # a declared obstruction between the two really blocks it
        server.sim.add_obstruction(HOME.latitude + 0.00135, HOME.longitude,
                                   HOME.altitude + 200.0, radius_m=60.0,
                                   name="ridge_1")
        blocked = await tool(server, "uav_los_check")(
            vehicle="Drone1", lat=far_lat, lon=HOME.longitude, alt_agl_m=0.0)
        assert blocked["los"] is False, "LOS answered true through a 200 m ridge"
        assert blocked["first_obstacle"]["name"] == "ridge_1"
        assert blocked["first_obstacle"]["type"] == "obstruction"
        assert blocked["first_obstacle_modelled"] is True
        server.sim.clear_obstructions()
    run(main())


def test_los_check_refuses_an_altitude_with_no_datum(server):
    async def main():
        none_given = await tool(server, "uav_los_check")(
            vehicle="Drone1", lat=47.6430, lon=-122.1402)
        assert none_given["error"]["code"] == "invalid_parameter"
        assert "alt_msl_m" in none_given["error"]["message"]
        two_given = await tool(server, "uav_los_check")(
            vehicle="Drone1", lat=47.6430, lon=-122.1402,
            alt_msl_m=70.0, alt_agl_m=10.0)
        assert two_given["error"]["code"] == "invalid_parameter"
    run(main())


# ------------------------------------------------ §4.3 mission primitives --

def test_mission_grid_search_derives_lane_spacing_from_overlap(server):
    """M1: spacing = swath*(1-overlap), swath = 2*alt*tan(HFOV/2); the caller
    supplies overlap, never a lane spacing."""
    async def main():
        from godseye_uav.missions import camera as cam_of
        from godseye_uav.missions import lane_spacing_m, swath_m
        box = [(47.6410, -122.1410), (47.6410, -122.1390),
               (47.6425, -122.1390), (47.6425, -122.1410)]
        out = await tool(server, "mission_grid_search")(
            vehicle="Drone1", polygon=box, alt_agl_m=50.0, overlap_pct=25.0,
            dry_run=True)
        hfov = cam_of("0").hfov_deg
        derived = lane_spacing_m(50.0, hfov_deg=hfov, overlap_pct=0.25)
        assert out["swath_m"] == pytest.approx(swath_m(50.0, hfov), abs=0.1)
        assert out["lane_spacing_derived_m"] == pytest.approx(derived, abs=0.1)
        # the spacing REPORTED is the one actually flown: the lanes are spread
        # evenly over the AO span, which can only tighten it, never loosen it
        assert 0.0 < out["lane_spacing_m"] <= derived + 0.1
        assert out["overlap_fraction"] == 0.25
        assert out["overlap_pct"] == 25.0
        assert "lane_spacing" not in schema_of(server, "mission_grid_search")
        assert out["executed"] is False
        assert out["coverage"]["coverage_pct"] > 0
        assert out["gate"]["ok"] is True
        assert out["est_time_s"] > 0 and out["est_fuel_pct"] > 0
    run(main())


def test_mission_grid_search_refuses_an_ambiguous_overlap(server):
    """An overlap of 0.20 could be 20% or 0.2% — a 100x error in the derived
    spacing. It is refused, not guessed (M1)."""
    async def main():
        box = [(47.6410, -122.1410), (47.6410, -122.1390),
               (47.6425, -122.1390), (47.6425, -122.1410)]
        out = await tool(server, "mission_grid_search")(
            vehicle="Drone1", polygon=box, alt_agl_m=50.0, overlap_pct=0.20,
            dry_run=True)
        assert out["error"]["code"] == "invalid_mission_params"
        assert "ambiguous" in out["error"]["message"]
        assert "20" in out["error"]["message"]
    run(main())


def test_mission_grid_search_reports_the_coverage_it_actually_achieves(server):
    """M1: 'if the plan is capped or truncated, report the coverage ACTUALLY
    achieved' — the old code reported the uncapped spacing."""
    async def main():
        box = [(47.6360, -122.1500), (47.6360, -122.1300),
               (47.6560, -122.1300), (47.6560, -122.1500)]
        out = await tool(server, "mission_grid_search")(
            vehicle="Drone1", polygon=box, alt_agl_m=40.0, overlap_pct=20.0,
            max_lanes=3, dry_run=True)
        assert out["truncated"] is True
        assert out["lane_spacing_m"] > out["lane_spacing_derived_m"]
        assert "coverage_thinned" in " ".join(out["warnings"])
        assert out["coverage"]["coverage_pct"] < 100.0
    run(main())


def test_mission_dry_run_executes_nothing(server):
    """PLAN §5: task -> plan -> dry-run -> execute. The dry run must not fly."""
    async def main():
        box = [(47.6412, -122.1404), (47.6412, -122.1398),
               (47.6418, -122.1398), (47.6418, -122.1404)]
        before = len(server.missions)
        out = await tool(server, "mission_dry_run")(
            vehicle="Drone1", kind="grid_search", polygon=box, alt_agl_m=40.0)
        assert out["executed"] is False and out["dry_run"] is True
        assert out["waypoints"] and out["gate"]["ok"] is True
        assert out["est_time_s"] > 0
        assert len(server.missions) == before, "the dry run registered a mission"
        assert server.tasking.queue_for("Drone1").current is None
        assert server.tasking.queue_for("Drone1").pending() == 0
    run(main())


def test_mission_status_and_cancel(server):
    async def main():
        box = [(47.6412, -122.1404), (47.6412, -122.1398),
               (47.6418, -122.1398), (47.6418, -122.1404)]
        h = await tool(server, "mission_grid_search")(
            vehicle="Drone1", polygon=box, alt_agl_m=40.0, overlap_pct=20.0)
        mid = h["mission_handle"]
        st = await tool(server, "mission_status")(mission_handle=mid)
        assert st["mission_handle"] == mid
        assert st["state"] in ("queued", "executing")
        assert st["progress_pct"] is not None
        assert st["waypoints_total"] == h["waypoint_count"]
        assert st["fuel_pct"] > 0 and st["bingo_fuel_pct"] >= 20.0
        cancelled = await tool(server, "mission_cancel")(mission_handle=mid)
        assert cancelled["cancelled"] is True
        await asyncio.sleep(0.4)
        after = await tool(server, "mission_status")(mission_handle=mid)
        assert after["state"] in ("cancelled", "failed", "done")
        unknown = await tool(server, "mission_status")(mission_handle="MSN-nope")
        assert unknown["error"]["code"] == "unknown_mission"
    run(main())


def test_mission_cancel_is_refused_during_a_bingo_rtb(server):
    """M4/T5: the harness cannot cancel its way out of a safety commitment."""
    async def main():
        await takeoff_to(server, "Drone1", 40.0)
        box = [(47.6412, -122.1404), (47.6412, -122.1398),
               (47.6418, -122.1398), (47.6418, -122.1404)]
        h = await tool(server, "mission_grid_search")(
            vehicle="Drone1", polygon=box, alt_agl_m=40.0, overlap_pct=20.0)
        mid = h["mission_handle"]
        server.fuel_for("Drone1").fuel_pct = 10.0
        await server.tick_once("Drone1")
        q = server.tasking.queue_for("Drone1")
        end = time.monotonic() + 10.0
        while time.monotonic() < end and (
                q.current is None or q.current.tool != "uav_return_to_home"):
            await asyncio.sleep(0.1)
        assert q.current is not None and q.current.tool == "uav_return_to_home"
        out = await tool(server, "mission_cancel")(mission_handle=mid)
        assert out["cancelled"] is False and out["refused"] is True
        assert "un-cancellable" in out["reason"]
        assert out["mission_status"] == MISSION_INCOMPLETE_FUEL
        assert q.current.tool == "uav_return_to_home"
        assert any(r["kind"] == "abort_refused" and r.get("mission_id") == mid
                   for r in server.store.audit.read_all())
    run(main())


def test_mission_track_target_derives_standoff_and_verifies_los(server):
    """M5: standoff from the threat ring + pixel density, VERIFIED with the
    same LOS model uav_los_check reports. Never a caller-supplied radius."""
    async def main():
        _, track_id = await spawn_and_scan(server, ob_class="supply_truck")
        out = await tool(server, "mission_track_target")(
            vehicle="Drone1", track_id=track_id, alt_agl_m=100.0, dry_run=True)
        assert out.get("error") is None, out
        assert "radius_m" not in schema_of(server, "mission_track_target")
        so = out["standoff"]
        assert so["standoff_m"] == pytest.approx(300.0, abs=1.0)   # MIN_STANDOFF_M
        assert so["threat_ring_m"] == pytest.approx(300.0, abs=1.0)
        assert "engagement envelope" in so["basis"]
        los = out["los"]
        assert los["verified"] is True
        assert los["points_checked"] == 12 and los["points_clear"] == 12
        assert out["track_id"] == track_id
        missing = await tool(server, "mission_track_target")(
            vehicle="Drone1", track_id="TRK-nope", dry_run=True)
        assert missing["error"]["code"] == "unknown_track"
    run(main())


def test_mission_track_target_drops_the_masked_arc(server):
    """A ring point with no line of sight is dropped and reported, not flown."""
    async def main():
        _, track_id = await spawn_and_scan(server, ob_class="supply_truck")
        t = server.tracks.get(track_id)
        # a ridge 200 m north of the contact masks only the northern arc of a
        # 300 m standoff ring — the rays from the other bearings miss it
        server.sim.add_obstruction(t.lat + 0.0018, t.lon, HOME.altitude + 150.0,
                                   radius_m=60.0, name="ridge_n")
        out = await tool(server, "mission_track_target")(
            vehicle="Drone1", track_id=track_id, alt_agl_m=60.0, dry_run=True)
        server.sim.clear_obstructions()
        assert out.get("error") is None, out
        los = out["los"]
        assert los["blocked"], "the ridge masked nothing"
        assert los["points_clear"] < los["points_checked"]
        assert out["waypoint_count"] == los["points_clear"]
        assert any("los_partial" in w for w in out["warnings"])
    run(main())


def test_mission_identify_target_cross_cues_wide_to_narrow(server):
    """M7: wide FOV to detect, then narrow FOV at a REDUCED slant range."""
    async def main():
        _, track_id = await spawn_and_scan(server, ob_class="supply_truck")
        out = await tool(server, "mission_identify_target")(
            vehicle="Drone1", track_id=track_id, alt_agl_m=120.0, dry_run=True)
        assert out.get("error") is None, out
        detect, ident = out["phases"]
        assert detect["name"] == "detect" and ident["name"] == "identify"
        assert ident["fov_deg"] < detect["fov_deg"], "no FOV cross-cue"
        assert ident["slant_range_m"] < detect["slant_range_m"], "no slant reduction"
        assert ident["expected_pixels_on_target"] > detect["expected_pixels_on_target"]
        # the phase FOV is stamped on the waypoints so the executor can fly it
        fovs = {wp.get("fov_deg") for wp in out["waypoints"]}
        assert fovs == {detect["fov_deg"], ident["fov_deg"]}
        # §4.3's identify product
        assert out["classification"]["ob_class"] == "supply_truck"
        assert out["confidence"]["level"] in ("possible", "probable", "confirmed")
        assert out["geo_point"]["lat"] == pytest.approx(
            server.tracks.get(track_id).lat, abs=1e-6)
        assert isinstance(out["track_history"], list) and out["track_history"]
        assert isinstance(out["key_images"], list)
    run(main())


def test_the_executor_actually_commands_the_cross_cue_fov(server):
    """M7 end to end: a phase FOV on a waypoint is COMMANDED in flight.

    Without this the identify pass flies at whatever field the camera happened
    to be left on, while the plan reports pixels it never achieved.
    """
    async def main():
        await takeoff_to(server, "Drone1", 20.0)
        await tool(server, "uav_set_fov")(vehicle="Drone1", fov_deg=90.0)
        h = server._submit("Drone1", "uav_fly_route", {"waypoints": [
            {"lat": HOME.latitude + 0.0003, "lon": HOME.longitude,
             "alt_m": 20.0, "fov_deg": 18.0, "camera": "0", "phase": "identify"},
        ], "speed_mps": 8.0}, None)
        t = await wait_state(server, "Drone1", h["task_id"], timeout_s=40.0)
        assert t.state.value == "done", t.error
        assert server.sim.camera("Drone1", "0").fov_deg == pytest.approx(18.0)
        kinds = [r for r in server.store.audit.read_all() if r["kind"] == "sensor_fov"]
        assert any(r.get("fov_deg") == 18.0 and r.get("waypoint") == 1 for r in kinds)
    run(main())


def test_mission_threat_assessment_is_isr_only(server):
    """M13 + M14: a structured assessment with no engagement recommendation."""
    async def main():
        _, track_id = await spawn_and_scan(server, ob_class="mbt")
        out = await tool(server, "mission_threat_assessment")(vehicle="Drone1")
        assert out["report"]["format"] == "THREATREP"
        assert out["mission_handle"].startswith("MSN-")
        assert out["survey"] is None and "nothing was flown" in out["survey_note"]
        assert "M14" in out["isr_only"]
        blob = json.dumps(out).lower()
        assert "recommended_roe" not in blob
        assert "engage" not in blob or "no engagement" in blob
        # it is readable back as a report resource and as a mission
        rep = await read_json(server, out["resource"])
        assert rep["format"] == "THREATREP"
        st = await tool(server, "mission_status")(mission_handle=out["mission_handle"])
        assert st["kind"] == "threat_assessment"
        assert track_id in json.dumps(out["report"])
    run(main())


def test_threat_observer_altitude_is_the_same_datum_as_the_track(server):
    """T1 again: `observer['alt_m']` is differenced against `Track.alt_m`.

    Adversarial finding. `_ingest_frame` passed the observer's HAE under
    `alt_m`; `mission_threat_assessment` and `uav_assess_threat` passed
    `alt_agl_m` under the SAME key, which `threat._assess` differences against
    the track's geo_point altitude (HAE) to get the slant range. The reported
    `observer_range_m` was therefore short by the ground elevation — ~100 m at
    the Redmond origin, ~1550 m in the mountain theaters, where it places the
    observing UAV a kilometre underground and breaks every weapon-envelope
    comparison M13 is built on.
    """
    async def main():
        await takeoff_to(server, "Drone1", 80.0)
        _, track_id = await spawn_and_scan(server, ob_class="mbt", dlat=0.0009)
        tele = await tool(server, "uav_get_telemetry")(vehicle="Drone1")
        trk = server.tracks.get(track_id)

        out = await tool(server, "uav_assess_threat")(
            vehicle="Drone1", track_id=track_id)
        ground = haversine_m(tele["lat"], tele["lon"], trk.lat, trk.lon)
        truth = math.hypot(ground, tele["alt_hae_m"] - trk.alt_m)
        wrong = math.hypot(ground, tele["alt_agl_m"] - trk.alt_m)
        # the two must be far enough apart for this to be a real assertion
        assert abs(truth - wrong) > 20.0, (truth, wrong)
        assert abs(out["observer_range_m"] - truth) < 1.0, (
            out["observer_range_m"], truth, wrong)

        area = await tool(server, "mission_threat_assessment")(vehicle="Drone1")
        got = [a for a in area["report"]["assessments"] if a["track_id"] == track_id]
        assert got and abs(got[0]["observer_range_m"] - truth) < 1.0
    run(main())


def test_threat_area_polygon_accepts_the_same_vertex_shape_the_planners_do(server):
    """§4.3: a vertex is {'lat','lon'} OR [lat, lon] everywhere else.

    Adversarial finding. `mission_threat_assessment` / `uav_assess_threat`
    handed the caller's polygon straight to `safety.point_in_polygon`, which
    understands only the pair form and unpacks a dict into its KEYS. Over the
    wire that meant, with an EMPTY track store, a THREATREP that claimed
    `scoped_by_polygon: true`, `scoping_error: null` and echoed the fence back
    as [["lat","lon"], ...] — a scoped assessment against two strings — and,
    the moment the store held one track, an opaque "Error executing tool
    mission_threat_assessment" instead of the contract's structured error.
    """
    async def main():
        _, track_id = await spawn_and_scan(server, ob_class="mbt")
        assert server.tracks.tracks(), "the defect only bites with a track present"
        lat, lon = HOME.latitude, HOME.longitude
        dicts = [{"lat": lat - 0.01, "lon": lon - 0.01},
                 {"lat": lat + 0.01, "lon": lon - 0.01},
                 {"lat": lat + 0.01, "lon": lon + 0.01},
                 {"lat": lat - 0.01, "lon": lon + 0.01}]
        pairs = [[p["lat"], p["lon"]] for p in dicts]

        out = await tool(server, "mission_threat_assessment")(
            vehicle="Drone1", area_polygon=dicts)
        assert "error" not in out, out
        # the echoed fence is NUMBERS, not the dict keys
        assert out["area_polygon"] == pairs
        assert out["report"]["area_polygon"] == pairs
        assert out["report"]["count"] == len(server.tracks.tracks())

        ref = await tool(server, "mission_threat_assessment")(
            vehicle="Drone1", area_polygon=pairs)
        assert ref["report"]["count"] == out["report"]["count"]

        direct = await tool(server, "uav_assess_threat")(
            vehicle="Drone1", area_polygon=dicts)
        assert "error" not in direct, direct
        assert direct["area_polygon"] == pairs

        # and a genuinely malformed vertex is a STRUCTURED refusal, not a 500
        bad = await tool(server, "uav_assess_threat")(
            vehicle="Drone1", area_polygon=[{"lat": lat}, {"lon": lon}, [1]])
        assert bad["error"]["code"] == "bad_polygon"
        assert "area_polygon[0]" in bad["error"]["message"]
    run(main())


def test_mission_handoff_track_moves_custody_with_a_derived_standoff(server):
    """M10 + M5: the receiver is sent to the contact's threat-ring standoff."""
    async def main():
        # M10 needs a positive ID before custody moves: one frame is only
        # 'possible', so the contact is looked at until it is at least probable
        _, track_id = await spawn_and_scan(server, ob_class="supply_truck", scans=3)
        out = await tool(server, "mission_handoff_track")(
            track_id=track_id, from_vehicle="Drone1", to_vehicle="Drone2",
            alt_agl_m=80.0)
        assert out.get("accepted") is True, out
        assert out["to_vehicle"] == "Drone2"
        assert out["standoff_m"] == pytest.approx(300.0, abs=1.0)
        assert out["task_id"]
        q = server.tasking.queue_for("Drone2")
        assert q.get(out["task_id"]) is not None    # pollable from the instant
        end = time.monotonic() + 10.0
        while time.monotonic() < end and q.current is None:
            await asyncio.sleep(0.1)
        assert q.current is not None and q.current.id == out["task_id"]
        await q.abort()
    run(main())


def test_uav_mission_is_a_thin_dispatcher_onto_the_discrete_tools(server):
    """TOOL_CONTRACT §4.3: 'it must forward to these, not reimplement them'."""
    async def main():
        box = [(47.6410, -122.1410), (47.6410, -122.1390),
               (47.6425, -122.1390), (47.6425, -122.1410)]
        direct = await tool(server, "mission_grid_search")(
            vehicle="Drone1", polygon=box, alt_agl_m=50.0, overlap_pct=25.0,
            dry_run=True)
        via = await tool(server, "uav_mission")(
            vehicle="Drone1", kind="grid_search",
            params={"polygon": box, "alt_agl_m": 50.0, "overlap_pct": 25.0},
            dry_run=True)
        assert via["waypoints"] == direct["waypoints"]
        assert via["lane_spacing_m"] == direct["lane_spacing_m"]
        assert via["coverage"] == direct["coverage"]
        bad = await tool(server, "uav_mission")(vehicle="Drone1", kind="bombard",
                                                params={})
        assert bad["error"]["code"] == "unknown_mission_kind"
    run(main())


# --------------------------------------------------------- §4.4 sim admin --

def test_sim_spawn_target_validates_the_ob_class(server):
    async def main():
        good = await tool(server, "sim_spawn_target")(
            lat=HOME.latitude + 0.0004, lon=HOME.longitude, ob_class="sam_medium_range")
        assert good["ob_class"] == "sam_medium_range"
        assert good["category"] == "sam"
        assert good["class_evidence"]["rule"] == "explicit ob_class key"
        bad = await tool(server, "sim_spawn_target")(
            lat=HOME.latitude, lon=HOME.longitude, ob_class="death_star")
        assert bad["error"]["code"] == "unknown_ob_class"
        assert "sam_medium_range" in bad["error"]["message"]
        assert "death_star" not in server.targets
    run(main())


def test_sim_spawn_target_altitude_defaults_to_terrain_and_states_its_datum(server):
    async def main():
        out = await tool(server, "sim_spawn_target")(
            lat=47.6415, lon=-122.1402, ob_class="bunker")
        assert out["alt_msl_m"] == server.theater.home_alt_msl_m != 0.0
        assert "alt_msl_m" in schema_of(server, "sim_spawn_target")
        explicit = await tool(server, "sim_spawn_target")(
            lat=47.6416, lon=-122.1402, ob_class="bunker", alt_msl_m=150.0)
        assert explicit["alt_msl_m"] == 150.0
        assert explicit["alt_hae_m"] == pytest.approx(
            canonical_altitude(150.0, 47.6416, -122.1402, datum="msl").alt_hae,
            abs=0.01)
        clash = await tool(server, "sim_spawn_target")(
            lat=47.6417, lon=-122.1402, ob_class="bunker",
            alt_msl_m=150.0, alt_m=90.0)
        assert clash["error"]["code"] == "invalid_parameter"
    run(main())


def test_sim_spawn_target_idempotency_key_spawns_once(server):
    async def main():
        a = await tool(server, "sim_spawn_target")(
            lat=HOME.latitude + 0.0004, lon=HOME.longitude, ob_class="mbt",
            idempotency_key="spawn-1")
        b = await tool(server, "sim_spawn_target")(
            lat=HOME.latitude + 0.0004, lon=HOME.longitude, ob_class="mbt",
            idempotency_key="spawn-1")
        assert b["status"] == "duplicate"
        assert b["target_id"] == a["target_id"]
        rows = [r for r in server.store.audit.read_all() if r["kind"] == "spawn_target"]
        assert len(rows) == 1, "a replayed key spawned a second object"
        assert len(server.targets) == 1
    run(main())


def test_sim_move_target_gives_the_contact_real_motion(server):
    async def main():
        target_id, _ = await spawn_and_scan(server, ob_class="supply_truck")
        start = await server.backend.object_pose(target_id)
        out = await tool(server, "sim_move_target")(
            target_id=target_id, speed_mps=12.0,
            waypoints=[{"lat": HOME.latitude + 0.0025, "lon": HOME.longitude}])
        assert out["ok"] is True and out["waypoints"] == 1
        # a waypoint with no altitude keeps the target on its own ground
        assert out["route"][0]["alt_hae_m"] == pytest.approx(
            server.targets[target_id]["alt_hae_m"], abs=0.01)
        moved = None
        end = time.monotonic() + 10.0
        while time.monotonic() < end:
            pos = await server.backend.object_pose(target_id)
            if pos and abs(pos[0] - start[0]) > 1e-5:
                moved = pos
                break
            await asyncio.sleep(0.3)
        assert moved is not None, "the target never moved"
        unknown = await tool(server, "sim_move_target")(
            target_id="ghost", waypoints=[{"lat": 47.64, "lon": -122.14}])
        assert unknown["error"]["code"] == "unknown_target"
    run(main())


def test_sim_set_time_moves_the_real_sun(server):
    """M6: the sun position is what the sun-side orbit rule plans against."""
    async def main():
        noon = await tool(server, "sim_set_time")(
            datetime="2026-06-21 12:00:00", clock_speed=0.0)
        dawn = await tool(server, "sim_set_time")(
            datetime="2026-06-21 06:00:00", clock_speed=0.0)
        assert noon["sun"]["elevation_deg"] > dawn["sun"]["elevation_deg"] > 0.0
        assert abs(noon["sun"]["azimuth_deg"] - dawn["sun"]["azimuth_deg"]) > 45.0
        assert noon["sim_time"].startswith("2026-06-21 12:00")
        # and the sensor-conditions block the INTREP reads follows it
        night = await tool(server, "sim_set_time")(
            datetime="2026-06-21 00:30:00", clock_speed=0.0)
        assert night["sun"]["elevation_deg"] < 0.0
        cond = await server._sensor_conditions("Drone1")
        assert cond["light"] == "night"
    run(main())


def test_sim_set_weather_degrades_the_sensor_and_reaches_the_fuel_model(server):
    """M18 (weather degrades sensors) + M15 (wind feeds the fuel model)."""
    async def main():
        before = (await server.backend.environment())["detection_range_m"]
        out = await tool(server, "sim_set_weather")(
            fog=0.8, wind_north_mps=-12.0, wind_east_mps=0.0)
        assert out["ok"] is True
        assert out["sim_weather"]["fog"] == pytest.approx(0.8)
        assert out["detection_range_m"] < before, "fog did not shorten the sensor"
        assert out["wind_ne_mps"] == [-12.0, 0.0]
        assert out["wind_source"] in ("sim", "sim_rpc")
        # the wind really reaches the M4 gate's fuel estimate
        route = [{"lat": 47.6550, "lon": -122.1500, "alt_m": 40.0},
                 {"lat": 47.6350, "lon": -122.1500, "alt_m": 40.0}]
        windy = await server._gate("Drone1", route, 10.0)
        assert windy["wind_ne_mps"] == [-12.0, 0.0]
        await tool(server, "sim_set_weather")(fog=0.0, wind_north_mps=0.0)
        calm = await server._gate("Drone1", route, 10.0)
        assert windy["required_pct"] > calm["required_pct"]
        bad = await tool(server, "sim_set_weather")(rain=7.0)
        assert bad["error"]["code"] == "invalid_parameter"
    run(main())


def test_sim_set_link_state_drives_the_lost_link_plan(tmp_path):
    """M9 through the TOOL, not by reaching into the fake."""
    with build_server(tmp_path, lost_link_plan={"behaviour": "rtb",
                                                "declare_after_s": 0.2}) as srv:
        async def main():
            await takeoff_to(srv, "Drone1", 20.0)
            await srv.tick_once("Drone1")
            out = await tool(srv, "sim_set_link_state")(vehicle="Drone1",
                                                        state="lost")
            assert out["ok"] is True and out["link"]["state"] == "lost"
            assert out["lost_link_plan"]["behaviour"] == "rtb"
            event = None
            end = time.monotonic() + 10.0
            while time.monotonic() < end:
                v = await srv.tick_once("Drone1")
                if v.get("link_event"):
                    event = v["link_event"]
                    break
                await asyncio.sleep(0.15)
            assert event is not None and event["event"] == "loal_declared"
            bad = await tool(srv, "sim_set_link_state")(vehicle="Drone1",
                                                         state="flaky")
            assert bad["error"]["code"] == "invalid_parameter"
        run(main())


def test_sim_set_gps_degradation(server):
    """M16/M17, and it says out loud that it is sim-wide, not per vehicle."""
    async def main():
        out = await tool(server, "sim_set_gps_degradation")(
            vehicle="Drone1", error_m=25.0, denied=True)
        assert out["ok"] is True
        assert out["gps_noise_m"] == 25.0 and out["gps_denied"] is True
        assert "sim-wide" in out["scope"]
        env = await server.backend.environment()
        assert env["gps_noise_m"] == 25.0 and env["gps_denied"] is True
        cond = await server._sensor_conditions("Drone1")
        assert cond["gps_quality"] == "denied"
        await tool(server, "sim_set_gps_degradation")(denied=False, error_m=0.0)
        nothing = await tool(server, "sim_set_gps_degradation")(vehicle="Drone1")
        assert nothing["error"]["code"] == "missing_parameter"
    run(main())


def test_sim_reset_keeps_the_track_store_and_the_pattern_of_life(server):
    """M12: 'sim_reset must NOT wipe the track store or pattern-of-life'."""
    async def main():
        _, track_id = await spawn_and_scan(server, ob_class="mbt")
        await takeoff_to(server, "Drone1", 25.0)
        pois_before = len(server.pol.pois())
        epoch_before = server.tracks.sim_epoch

        out = await tool(server, "sim_reset")()
        assert out["ok"] is True
        assert out["tracks_retained"] >= 1
        assert out["pois_retained"] == pois_before
        assert out["sim_epoch"] == epoch_before + 1
        assert "M12" in out["persistence"]

        # the intel survived...
        assert server.tracks.get(track_id) is not None
        listed = await read_json(server, "uav://tracks")
        assert track_id in [t["track_id"] for t in listed["tracks"]]
        assert server.store.tracks.get(track_id) is not None
        assert len(server.pol.pois()) == pois_before
        # ...and the vehicle really was reset
        tele = await server.backend.telemetry("Drone1")
        assert tele["alt_agl_m"] == pytest.approx(0.0, abs=0.5)
    run(main())


# ------------------------------------------- the real Streamable HTTP wire --

@contextmanager
def http_server(tmp_path):
    """The MCP server on a real loopback socket, spoken to over POST /mcp."""
    import threading as _threading
    import urllib.request

    sim = _start_sim()
    token = "stage2-token"
    client = airsim.MultirotorClient(port=sim.port)
    client.confirmConnection()
    backend = UavBackend(client, HOME, sim=sim)
    store = Store(tmp_path)
    srv = GodseyeUavServer(
        backend, store, token=token, watchdog_s=30.0,
        envelope=SafetyEnvelope(geofence=AO,
                                home=(HOME.latitude, HOME.longitude, HOME.altitude)))

    def answers(url: str) -> bool:
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test_server", "version": "1"}},
        }).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=2.0) as r:
                return r.status == 200
        except Exception:
            return False

    url = loop = thread = None
    try:
        for port in _HTTP_PORTS:
            loop = asyncio.new_event_loop()

            def run_loop(loop=loop, port=port):
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(
                        srv.mcp.run_streamable_http_async(
                            host="127.0.0.1", port=port,
                            streamable_http_path="/mcp", stateless_http=True))
                except (RuntimeError, asyncio.CancelledError, SystemExit):
                    pass

            thread = _threading.Thread(target=run_loop, daemon=True)
            thread.start()
            candidate = f"http://127.0.0.1:{port}/mcp"
            end = time.monotonic() + 15.0
            while time.monotonic() < end:
                if answers(candidate):
                    url = candidate
                    break
                if not thread.is_alive():
                    break
                time.sleep(0.2)
            if url:
                break
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5.0)
        assert url is not None, f"no bindable MCP port in {_HTTP_PORTS[0]}-{_HTTP_PORTS[-1]}"
        yield {"url": url, "token": token, "server": srv}
    finally:
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5.0)
        srv.stop_monitor()
        try:
            srv.tasking.shutdown()
        except Exception:
            pass
        store.close()
        sim.stop()
        if loop is not None and not loop.is_running():
            loop.close()


def test_catalog_and_resources_over_the_real_streamable_http_transport(tmp_path):
    """R6 was measured 'over the wire' — so this re-measures it the same way.

    tools/list, resources/list, resources/templates/list, resources/read and a
    tools/call, all through POST /mcp with Bearer auth. No server internals.
    """
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    with http_server(tmp_path) as stack:
        async def main():
            async with httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {stack['token']}"},
                    timeout=30.0) as hc:
                async with streamable_http_client(stack["url"], http_client=hc) as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()

                        tools = {t.name for t in (await session.list_tools()).tools}
                        missing = sorted(CONTRACT_TOOLS - tools)
                        assert not missing, f"not published over the wire: {missing}"

                        static = {str(r_.uri) for r_ in
                                  (await session.list_resources()).resources}
                        templates = {t.uri_template for t in
                                     (await session.list_resource_templates()
                                      ).resource_templates}
                        assert not sorted(CONTRACT_RESOURCES - (static | templates))

                        # the envelope a skill's ROE must stay inside, read over MCP
                        fence = json.loads(
                            (await session.read_resource("uav://safety/geofence")
                             ).contents[0].text)
                        assert [tuple(p) for p in fence["geofence"]] == list(AO)
                        assert "stricter" in fence["roe"].lower()

                        # a mutating tool call, then its ground truth as a resource
                        res = await session.call_tool("sim_spawn_target", {
                            "lat": HOME.latitude + 0.0005,
                            "lon": HOME.longitude, "ob_class": "mbt"})
                        spawned = json.loads(res.content[0].text)
                        assert spawned["ob_class"] == "mbt"
                        targets = json.loads(
                            (await session.read_resource("uav://targets")
                             ).contents[0].text)
                        assert targets["count"] == 1

                        # and an image resource really returns PNG bytes
                        img = (await session.read_resource(
                            "uav://Drone1/camera/0/scene")).contents[0]
                        assert base64.b64decode(img.blob)[:8] == b"\x89PNG\r\n\x1a\n"

                        tele = json.loads(
                            (await session.read_resource("uav://Drone1/telemetry")
                             ).contents[0].text)
                        assert tele["alt_hae_m"] == pytest.approx(HOME.altitude, abs=1.0)
                        assert tele["alt_msl_m"] != tele["alt_hae_m"]
        asyncio.run(main())


def test_the_wire_still_refuses_a_bad_bearer_token(tmp_path):
    """T4e: the new surface did not open a hole."""
    import urllib.error
    import urllib.request

    with http_server(tmp_path) as stack:
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}).encode()
        req = urllib.request.Request(stack["url"], data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer not-the-token"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5.0)
        assert exc.value.code == 401


def test_telemetry_publishes_est_range_km_with_its_basis(server):
    """TOOL_CONTRACT §4.2 names est_range_km; it was never published.

    It is derived from the SAME burn model the fuel integrator uses, so it can
    never drift away from the fuel clock, and the return says what it assumed.
    """
    from godseye_uav.safety import Phase

    async def main():
        tele = await tool(server, "uav_get_telemetry")(vehicle="Drone1")
        fm = server.fuel_for("Drone1")
        assert tele["est_range_km"] > 0.0
        assert tele["usable_fuel_pct"] == pytest.approx(
            tele["fuel_pct"] - tele["bingo_fuel_pct"], abs=0.01)
        expect_s = tele["usable_fuel_pct"] / fm._burn(Phase.CRUISE, 1.0, 0.0)
        assert tele["est_endurance_s"] == pytest.approx(expect_s, rel=0.01)
        assert tele["est_range_km"] == pytest.approx(
            expect_s * fm.rtb_speed_mps / 1000.0, rel=0.01)
        assert "BINGO line" in tele["est_range_basis"]
        # burning fuel shortens it — it is not a constant
        fm.fuel_pct = 60.0
        later = await tool(server, "uav_get_telemetry")(vehicle="Drone1")
        assert later["est_range_km"] < tele["est_range_km"]
    run(main())


def test_capture_image_reports_the_boresight_the_frame_was_taken_on(server):
    """A geo pose without the look angle cannot be geo-referenced.

    The published boresight is the WORLD look direction — the body attitude
    composed with the gimbal — so on a level, north-facing vehicle it equals
    the commanded gimbal, and it moves when either of the two moves.
    """
    async def main():
        level = await tool(server, "uav_capture_image")(vehicle="Drone1")
        assert level["geo_pose"]["boresight"]["pitch_deg"] == pytest.approx(0.0, abs=1.0)
        await tool(server, "uav_set_gimbal")(vehicle="Drone1", camera="0",
                                             pitch_deg=-35.0, yaw_deg=15.0)
        out = await tool(server, "uav_capture_image")(vehicle="Drone1")
        bore = out["geo_pose"]["boresight"]
        assert bore is not None
        assert bore["pitch_deg"] == pytest.approx(-35.0, abs=1.5)
        assert bore["yaw_deg"] == pytest.approx(15.0, abs=1.5)
        # and it is the SENSOR's look angle, not the airframe's
        assert out["vehicle_pose"]["attitude"]["pitch_deg"] == pytest.approx(0.0, abs=1.0)
    run(main())


def test_a_grid_mission_flies_at_the_fov_its_swath_was_costed_at(server):
    """M1 in a different disguise: the plan's swath, lane spacing and coverage
    are all computed at the camera's wide field. If the sensor is left on
    whatever field the last command set, the reported coverage is not the
    coverage that was imaged. The plan's own field is commanded in flight.
    """
    async def main():
        from godseye_uav.missions import camera as cam_of
        hfov = cam_of("0").hfov_deg
        await takeoff_to(server, "Drone1", 25.0)
        # leave the sensor on a field the plan did NOT assume
        await tool(server, "uav_set_fov")(vehicle="Drone1", fov_deg=90.0)
        assert server.sim.camera("Drone1", "0").fov_deg == 90.0
        box = [(47.6412, -122.1404), (47.6412, -122.1398),
               (47.6418, -122.1398), (47.6418, -122.1404)]
        h = await tool(server, "mission_grid_search")(
            vehicle="Drone1", polygon=box, alt_agl_m=25.0, overlap_pct=20.0,
            speed_mps=10.0)
        assert h.get("rejected") is not True, h
        assert all(wp["fov_deg"] == hfov for wp in h["waypoints"])
        end = time.monotonic() + 30.0
        while time.monotonic() < end:
            if server.sim.camera("Drone1", "0").fov_deg == pytest.approx(hfov):
                break
            await asyncio.sleep(0.2)
        assert server.sim.camera("Drone1", "0").fov_deg == pytest.approx(hfov), \
            "the grid flew at a field its swath was never costed at"
        await server.tasking.queue_for("Drone1").abort()
    run(main())


# ===========================================================================
# Wave-4: TOOL_CONTRACT spellings, terminal states, coverage truth, captures.
# Every defect below was proven at runtime before it was fixed.
# ===========================================================================

_W4_PORTS = list(range(49100, 49150))


# ------------------------------------ (1) contract spellings at the boundary

def test_flight_tools_publish_the_contract_altitude_spelling(server):
    """TOOL_CONTRACT §4.1: 'Never a bare alt_m.'

    `uav_takeoff` and `uav_goto_gps` — the two most-used flight tools —
    published only `alt_m`, so a harness written from the contract could not
    call either of them. The datumed name is now the published one and the
    legacy spelling survives as a documented alias.
    """
    async def main():
        for name in ("uav_takeoff", "uav_goto_gps"):
            props = schema_of(server, name)
            assert "alt_agl_m" in props, f"{name} does not publish alt_agl_m"
            assert "alt_m" in props, f"{name} dropped the legacy alias"
            desc = server.mcp._tool_manager._tools[name].description
            assert "AGL" in desc and "alt_agl_m" in desc
            # the datum is stated, not implied
            assert "MSL" in desc and "HAE" in desc

        # the contract spelling actually flies
        h = await tool(server, "uav_takeoff")(vehicle="Drone1", alt_agl_m=12.0)
        assert h["status"] == "accepted"
        assert h["alt_agl_m"] == 12.0
        t = await wait_state(server, "Drone1", h["task_id"])
        assert t.state.value == "done", t.error
        tele = await server.backend.telemetry("Drone1")
        assert tele["alt_agl_m"] == pytest.approx(12.0, abs=3.0)
    run(main())


def test_the_legacy_altitude_alias_may_never_silently_disagree(server):
    """Both spellings, different values: refused, never one silently chosen."""
    async def main():
        out = await tool(server, "uav_takeoff")(
            vehicle="Drone1", alt_agl_m=20.0, alt_m=50.0)
        assert out["error"]["code"] == "invalid_parameter"
        assert "disagree" in out["error"]["message"]
        # and an altitude with no datum at all is still refused on goto
        bare = await tool(server, "uav_goto_gps")(
            vehicle="Drone1", lat=47.645, lon=-122.145)
        assert bare["error"]["code"] == "invalid_parameter"
        assert server.tasking.queue_for("Drone1").active() is None
    run(main())


def test_a_route_waypoint_with_no_altitude_is_refused_not_flown_at_ground(server):
    """`wp.get("alt_m", 0.0)` flew the leg at 0 m AGL — into the terrain."""
    async def main():
        await takeoff_to(server, "Drone1", 20.0)
        h = server._submit("Drone1", "uav_fly_route",
                           {"waypoints": [{"lat": 47.6425, "lon": -122.1402}],
                            "speed_mps": 8.0}, None)
        t = await wait_state(server, "Drone1", h["task_id"])
        assert t.state.value == "failed"
        assert "waypoint 1" in (t.error or "")
        # the same waypoint with the CONTRACT spelling is accepted
        h2 = server._submit("Drone1", "uav_fly_route",
                            {"waypoints": [{"lat": 47.6425, "lon": -122.1402,
                                            "alt_agl_m": 20.0}],
                             "speed_mps": 8.0}, None)
        t2 = await wait_state(server, "Drone1", h2["task_id"], timeout_s=60.0)
        assert t2.state.value == "done", t2.error
    run(main())


def test_sim_spawn_target_publishes_the_contract_class_slot(server):
    """§4.4 names the slot `class`; the tool published only mesh/ob_class."""
    async def main():
        out = await tool(server, "sim_spawn_target")(
            lat=HOME.latitude + 0.0004, lon=HOME.longitude, ob_class="mbt")
        assert out["class"] == "mbt"
        assert out["ob_class"] == out["class"], "the alias must not drift"
        listed = await read_json(server, "uav://targets")
        assert listed["targets"][0]["class"] == "mbt"
        # the parameter name is reconciled in the description, not left to guess
        desc = server.mcp._tool_manager._tools["sim_spawn_target"].description
        assert "`class`" in desc and "ob_class" in desc
    run(main())


# ----------------------------- (2) the roster is never invented on failure

def _break_vehicle_listing(srv):
    """Make the sim's roster RPC fail the way a lost link does."""
    def boom(*a, **kw):
        raise ConnectionError("msgpack-rpc: connection reset")
    srv.backend.client.listVehicles = boom


def test_list_vehicles_surfaces_the_failure_instead_of_inventing_a_fleet(server):
    """The bug class that cost this project the most.

    `except Exception: return ["Drone1"]` reported one hardcoded aircraft for
    ANY failure — a lost link included — to both uav_list_vehicles and the
    safety monitor's vehicle list.
    """
    async def main():
        good = await tool(server, "uav_list_vehicles")()
        assert good["roster_source"] == "sim" and good["degraded"] is False
        names = [v["name"] for v in good["vehicles"]]

        _break_vehicle_listing(server)
        out = await tool(server, "uav_list_vehicles")()
        assert out["error"]["code"] == "vehicle_roster_unavailable"
        assert out["error"]["retryable"] is True
        assert out["degraded"] is True
        assert out["roster_source"] == "unavailable"
        assert out["vehicles"] is None, "a failed roster is not a roster"
        # it did NOT pass a guess off as the fleet
        assert out.get("vehicles") != names
        assert "ConnectionError" in out["error"]["message"]
        assert server.vehicle_roster_error is not None
        kinds = [r["kind"] for r in server.store.audit_tail(50)]
        assert "vehicle_roster_unavailable" in kinds

        # the backend itself raises rather than answering with a fiction
        with pytest.raises(ConnectionError):
            await server.backend.list_vehicles()
    run(main())


def test_the_monitor_does_not_tick_a_vehicle_it_made_up(tmp_path):
    """The monitor took its vehicle list from the same silent fallback, so a
    roster failure had it burning fuel and checking geofence for a name nobody
    confirmed while the real airframes went unwatched."""
    with build_server(tmp_path) as srv:
        async def main():
            _break_vehicle_listing(srv)
            task = asyncio.ensure_future(srv._monitor_loop(None, 0.05))
            await asyncio.sleep(0.6)
            srv._monitor_stop.set()
            await asyncio.wait_for(task, timeout=5.0)
            assert srv.vehicle_roster_error is not None
            assert any("list_vehicles" in e for e in srv._monitor_errors)
            # nothing was ticked, because nothing was known
            assert srv.fuel_for("Drone1").ticks == 0
            assert srv.ticks == {}
        run(main())


# -------------------------- (3) a replayed mission key keeps its ORIGINAL id

def test_replaying_a_mission_key_returns_the_original_mission_handle(server):
    """TOOL_CONTRACT: 'Replaying a key returns the ORIGINAL handle.'

    The task was correctly de-duplicated, but the mission id was minted before
    the submit, so the replay handed back a fresh MSN- the mission had never
    been filed under: mission_status on it said unknown, and no INTREP existed.
    """
    async def main():
        box = [(47.6412, -122.1404), (47.6412, -122.1398),
               (47.6418, -122.1398), (47.6418, -122.1404)]
        a = await tool(server, "mission_grid_search")(
            vehicle="Drone1", polygon=box, alt_agl_m=40.0, overlap_pct=20.0,
            idempotency_key="msn-key-1")
        assert a.get("rejected") is not True, a
        b = await tool(server, "mission_grid_search")(
            vehicle="Drone1", polygon=box, alt_agl_m=40.0, overlap_pct=20.0,
            idempotency_key="msn-key-1")
        assert b["status"] == "duplicate"
        assert b["task_id"] == a["task_id"]
        assert b["mission_handle"] == a["mission_handle"], (
            "a replayed key handed back a handle the mission was never filed "
            "under")
        assert b["mission_id"] == a["mission_id"]
        assert b["original_handle_available"] is True
        # and the handle it returns actually resolves
        st = await tool(server, "mission_status")(mission_handle=b["mission_handle"])
        assert st.get("error") is None, st
        assert st["mission_id"] == a["mission_handle"]
        assert b["mission_handle"] in server.missions
        await server.tasking.queue_for("Drone1").abort()
    run(main())


def test_a_mission_key_reused_by_a_non_mission_tool_is_refused(server):
    """It must not answer with a mission handle it does not have."""
    async def main():
        await tool(server, "uav_takeoff")(vehicle="Drone1", alt_agl_m=20.0,
                                          idempotency_key="shared-key")
        box = [(47.6412, -122.1404), (47.6412, -122.1398),
               (47.6418, -122.1398), (47.6418, -122.1404)]
        out = await tool(server, "mission_grid_search")(
            vehicle="Drone1", polygon=box, alt_agl_m=40.0, overlap_pct=20.0,
            idempotency_key="shared-key")
        assert out["error"]["code"] == "idempotency_key_reused_across_tools"
        assert "mission_handle" not in out
        await server.tasking.queue_for("Drone1").abort()
    run(main())


# --------------------- (4) tasks reach a TERMINAL state in the journal

def _task_rows(srv, task_id):
    return [r for r in srv.store.tasks.read_all() if r.get("task_id") == task_id]


def test_the_task_journal_records_the_real_terminal_state(server):
    """T4c, proven at runtime: `.godseye/store/tasks.jsonl` read
    queued/executing/executing for every task and no row ever carried a
    terminal `state`, because the outcome was journalled from inside the
    executor — before the queue had decided it."""
    async def main():
        h = await tool(server, "uav_takeoff")(vehicle="Drone1", alt_agl_m=15.0)
        t = await wait_state(server, "Drone1", h["task_id"])
        assert t.state.value == "done", t.error
        rows = _task_rows(server, h["task_id"])
        states = [r.get("state") for r in rows]
        assert "done" in states, (
            f"no row carries a terminal state; got {states}")
        closing = [r for r in rows if r.get("event") == "completed"]
        assert len(closing) == 1, "the terminal row must be written exactly once"
        assert closing[0]["state"] == "done", (
            "the closing row still claims the task is executing")
        assert closing[0]["progress_pct"] == 100.0
        assert closing[0]["result"]["ok"] is True
        assert closing[0]["finished_at"] is not None
    run(main())


def test_a_failed_task_is_journalled_as_failed_with_its_terminal_state(server):
    async def main():
        h = server._submit("Drone1", "uav_fly_route",
                           {"waypoints": [{"lat": 47.6425, "lon": -122.1402}],
                            "speed_mps": 8.0}, None)
        t = await wait_state(server, "Drone1", h["task_id"])
        assert t.state.value == "failed"
        rows = _task_rows(server, h["task_id"])
        closing = [r for r in rows if r.get("event") == "failed"]
        assert closing and closing[0]["state"] == "failed"
        assert closing[0]["error"]
    run(main())


def test_a_task_cancelled_before_it_flew_is_closed_so_replay_ignores_it(server):
    """A queued task dropped by abort got NO closing row at all, so every
    restart replay saw it as work still in flight and recovered to RTH."""
    async def main():
        await takeoff_to(server, "Drone1", 15.0)
        flying = server._submit("Drone1", "uav_fly_route",
                                {"waypoints": [{"lat": 47.6480, "lon": -122.1500,
                                                "alt_agl_m": 40.0}],
                                 "speed_mps": 3.0}, None)
        queued = server.tasking.submit(
            "Drone1", "uav_hover", {}, allow_queue=True)
        await asyncio.sleep(0.3)
        assert queued.state.value == "queued", queued.state
        await server.tasking.queue_for("Drone1").abort()
        await wait_state(server, "Drone1", flying["task_id"])
        await asyncio.sleep(0.3)

        for tid in (flying["task_id"], queued.id):
            rows = _task_rows(server, tid)
            assert rows, f"{tid} has no journal rows at all"
            assert any(r.get("event") == "cancelled" and r.get("state") == "cancelled"
                       for r in rows), (
                f"{tid} never reached a terminal row: "
                f"{[(r.get('event'), r.get('state')) for r in rows]}")

        rep = server.store.replay()
        interrupted = {r.id for r in rep.tasks}
        assert queued.id not in interrupted, (
            "a task that was cancelled still replays as work in flight")
        assert flying["task_id"] not in interrupted
    run(main())


def test_return_to_home_never_claims_100_percent_while_it_is_still_flying(server):
    """Proven at runtime: RTH hit progress_pct 100.0 / eta_s 0.4 and then sat
    in `executing` for 30 s+, so the next command was refused as busy against a
    handle that said it had finished."""
    async def main():
        await takeoff_to(server, "Drone1", 15.0)
        h = await tool(server, "uav_return_to_home")(vehicle="Drone1", speed_mps=8.0)
        assert h["status"] == "accepted", h
        q = server.tasking.queue_for("Drone1")
        task = q.get(h["task_id"])

        worst = 0.0            # highest progress seen while NOT terminal
        eta_at_worst = None
        end = time.monotonic() + 90.0
        while time.monotonic() < end:
            state, pct = task.state.value, task.progress_pct
            if state in ("done", "failed", "cancelled"):
                break
            if pct > worst:
                worst, eta_at_worst = pct, task.eta_s
            await asyncio.sleep(0.05)

        assert task.state.value == "done", task.error
        assert worst < 100.0, (
            f"progress hit {worst}% (eta {eta_at_worst}s) while the task was "
            "still executing — 100% must mean the task is over")
        # ...and it does reach 100 exactly when it terminates
        assert task.progress_pct == 100.0
        assert task.result["touchdown"]["landed"] is True

        # the aircraft really is down, and the queue really is free
        tele = await server.backend.telemetry("Drone1")
        assert tele["alt_agl_m"] == pytest.approx(0.0, abs=1.5)
        nxt = await tool(server, "uav_takeoff")(vehicle="Drone1", alt_agl_m=10.0)
        assert nxt.get("status") == "accepted", nxt
        await wait_state(server, "Drone1", nxt["task_id"])
    run(main())


def test_an_executor_never_publishes_100_percent_before_the_queue_does(server):
    """The invariant that makes progress_pct readable: 100% is written by the
    queue as the task turns terminal, and by nothing else."""
    async def main():
        await takeoff_to(server, "Drone1", 12.0)
        h = server._submit("Drone1", "uav_fly_route",
                           {"waypoints": [{"lat": 47.6430, "lon": -122.1420,
                                           "alt_agl_m": 25.0}],
                            "speed_mps": 6.0}, None)
        task = server.tasking.queue_for("Drone1").get(h["task_id"])

        # EVERY report is recorded, not sampled: a 100% published for one poll
        # interval is exactly the defect, and polling could miss it.
        reports: list[tuple[str, float]] = []

        async def watch(t):
            reports.append((t.state.value, t.progress_pct))

        task.add_progress_hook(watch)
        t = await wait_state(server, "Drone1", h["task_id"], timeout_s=120.0)
        assert t.state.value == "done", t.error

        early_100 = [r for r in reports
                     if r[1] >= 100.0 and r[0] not in ("done", "failed", "cancelled")]
        assert not early_100, (
            f"an executor published 100% while the task was still running: "
            f"{early_100}")
        assert reports[-1] == ("done", 100.0), reports[-1]
        assert max(p for s, p in reports if s == "executing") <= 99.0
    run(main())


# --------------- (5) coverage is SENSOR coverage, not waypoint progress

def test_intrep_coverage_is_imaged_ground_not_waypoint_progress(server):
    """Proven at runtime: one mission reported coverage_pct 11.15 from
    mission_grid_search and a different figure from uav_target_report, because
    the INTREP slot was waypoints-flown/waypoints-planned. Reporting progress
    as imaged coverage is the most damaging error an ISR report can make: the
    consumer believes ground was cleared when it was not."""
    from godseye_uav.missions import coverage_of_path

    async def main():
        box = [(47.6405, -122.1425), (47.6405, -122.1385),
               (47.6432, -122.1385), (47.6432, -122.1425)]
        h = await tool(server, "mission_grid_search")(
            vehicle="Drone1", polygon=box, alt_agl_m=60.0, overlap_pct=20.0,
            speed_mps=20.0)
        assert h.get("rejected") is not True, h
        assert h["coverage"]["basis"] == "planned"

        task = server.tasking.queue_for("Drone1").get(h["task_id"])
        end = time.monotonic() + 180.0
        while time.monotonic() < end and (task.waypoint or 0) < 3:
            if task.state.value in ("done", "failed", "cancelled"):
                break
            await asyncio.sleep(0.1)
        # freeze the flight so the recomputation below is like-for-like
        await server.tasking.queue_for("Drone1").abort()
        await wait_state(server, "Drone1", h["task_id"], timeout_s=30.0)
        rep = await tool(server, "uav_target_report")(mission_id=h["mission_handle"])
        cov = rep["coverage"]
        # only legs that actually CLOSED are credited: the leg the aircraft was
        # crossing when it was cancelled is not reported as imaged ground
        flown = cov["waypoints_flown"]
        assert flown >= 2, f"the grid never closed a leg (waypoint={task.waypoint})"
        assert flown < (task.waypoint or 0), (
            "the in-progress leg was credited as flown")
        assert "not credited" in cov["waypoints_flown_basis"]
        assert cov["basis"] == "flown", cov
        assert cov["coverage_pct"] is not None

        # it is the real producer's number, on the route actually flown
        expect = coverage_of_path(
            box, [(w["lat"], w["lon"]) for w in h["waypoints"][:flown]],
            float(h["swath_m"]), basis="flown")
        assert cov["coverage_pct"] == pytest.approx(expect.coverage_pct, abs=0.01)
        assert cov["swath_m"] == pytest.approx(float(h["swath_m"]), abs=0.01)
        assert cov["covered_area_km2"] > 0.0

        # waypoint progress still exists — under its own honest name, and it is
        # NOT what the coverage slot reports
        assert cov["waypoints_flown"] == flown
        assert cov["waypoint_progress_pct"] == pytest.approx(
            100.0 * flown / cov["waypoints_planned"], abs=0.1)
        assert cov["coverage_pct"] != pytest.approx(
            cov["waypoint_progress_pct"], abs=0.01), (
            "the coverage slot is still reporting waypoint progress")
        # planned vs flown are both present and distinguishable
        assert cov["planned_coverage_pct"] == pytest.approx(
            h["coverage"]["coverage_pct"], abs=0.01)
        # ...and mission_status labels ITS number as the planned one, so the
        # two producers can never be read as the same quantity
        st = await tool(server, "mission_status")(mission_handle=h["mission_handle"])
        assert st["coverage_basis"] == "planned"
        assert "must not be substituted" in st["coverage_note"]
        assert st["coverage"]["coverage_pct"] == pytest.approx(
            cov["planned_coverage_pct"], abs=0.01)
    run(main())


def test_coverage_is_refused_not_faked_for_a_mission_with_no_area(server):
    """An orbit has no tasked polygon. A coverage percentage for it would be
    fiction — which is exactly what the waypoint ratio was."""
    async def main():
        h = await tool(server, "uav_orbit_poi")(
            vehicle="Drone1", lat=HOME.latitude + 0.0006, lon=HOME.longitude,
            radius_m=80.0, alt_agl_m=45.0)
        assert h.get("rejected") is not True, h
        await asyncio.sleep(1.0)
        rep = await tool(server, "uav_target_report")(mission_id=h["mission_handle"])
        cov = rep["coverage"]
        assert cov["coverage_pct"] is None
        assert cov["basis"] == "not_applicable"
        assert "not tasked with an area" in cov["method"]
        assert cov["waypoint_progress_pct"] is not None
        await server.tasking.queue_for("Drone1").abort()
    run(main())


# ------------------- (6) the M2 capture schedule is actually EXECUTED

def test_a_recon_route_actually_collects_its_planned_captures(server):
    """M2: plan.captures reached the wire as `capture_count` and nothing ever
    walked the schedule, so a recon route planned imagery it never took."""
    async def main():
        route = [{"lat": HOME.latitude, "lon": HOME.longitude},
                 {"lat": HOME.latitude + 0.0030, "lon": HOME.longitude},
                 {"lat": HOME.latitude + 0.0030, "lon": HOME.longitude + 0.0030}]
        h = await tool(server, "mission_recon_route")(
            vehicle="Drone1", waypoints=route, alt_agl_m=60.0,
            forward_overlap_pct=20.0, speed_mps=14.0)
        assert h.get("rejected") is not True, h
        planned = h["capture_count"]
        assert planned >= 2, h

        frames_before = len(server.frames)
        t = await wait_state(server, "Drone1", h["task_id"], timeout_s=240.0)
        assert t.state.value == "done", t.error

        res = t.result
        assert res["captures_planned"] == planned
        assert res["captures_taken"] >= 2, res
        assert res["captures_taken"] + res["captures_missed"] == planned
        assert res["capture_trigger"] == "distance"

        # real frames, not bookkeeping: each carries retrievable PNG bytes
        assert len(server.frames) > frames_before
        shot = res["captures"][0]
        assert shot["resource"].startswith("uav://Drone1/camera/")
        item = await read_resource(server, shot["resource"])
        assert item.content[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"

        # the triggers really are distance-ordered along the route
        along = [c["along_track_m"] for c in res["captures"]]
        assert along == sorted(along)
        assert along[0] == 0.0
        # the trigger distance the tool documents actually reaches the wire
        interval = h["capture_every_m"]
        assert h["capture_trigger"] == "distance"
        assert along[1] == pytest.approx(interval, abs=0.5)

        # and they were audited as they fired
        kinds = [r["kind"] for r in server.store.audit_tail(200)]
        assert kinds.count("capture_image") >= res["captures_taken"]

        # the INTREP can say how many of the planned captures were taken
        rep = await tool(server, "uav_target_report")(mission_id=h["mission_handle"])
        assert rep["mission_summary"]["captures_planned"] == planned
        assert rep["mission_summary"]["captures_taken"] == res["captures_taken"]
        assert rep["mission_summary"]["captures_missed"] == res["captures_missed"]
    run(main())


def test_a_capture_that_fails_is_reported_as_a_gap_not_silently_dropped(server):
    """A missed frame is a hole in the imagery; the INTREP must show it."""
    async def main():
        route = [{"lat": HOME.latitude, "lon": HOME.longitude},
                 {"lat": HOME.latitude + 0.0022, "lon": HOME.longitude}]
        h = await tool(server, "mission_recon_route")(
            vehicle="Drone1", waypoints=route, alt_agl_m=60.0,
            forward_overlap_pct=20.0, speed_mps=14.0)
        assert h.get("rejected") is not True, h
        planned = h["capture_count"]

        real = server.backend.capture

        async def flaky(vehicle, camera, image_type):
            raise RuntimeError("sensor bus timeout")

        server.backend.capture = flaky
        try:
            t = await wait_state(server, "Drone1", h["task_id"], timeout_s=240.0)
        finally:
            server.backend.capture = real
        assert t.state.value == "done", t.error
        res = t.result
        assert res["captures_taken"] == 0
        assert res["captures_missed"] == planned
        assert "sensor bus timeout" in res["capture_failures"][0]["error"]
        kinds = [r["kind"] for r in server.store.audit_tail(200)]
        assert "capture_missed" in kinds
        rep = await tool(server, "uav_target_report")(mission_id=h["mission_handle"])
        assert rep["mission_summary"]["captures_taken"] == 0
        assert rep["mission_summary"]["captures_planned"] == planned
    run(main())


# ---------------------------------------------------------------------------
# Wave-4 runtime proof: every fix above, re-measured over the REAL Streamable
# HTTP transport (POST /mcp, Bearer auth, MCP ClientSession) rather than
# through server internals. Ports 49100-49199.
# ---------------------------------------------------------------------------

@contextmanager
def w4_http_server(tmp_path):
    """The MCP server on a real loopback socket in the Wave-4 port range."""
    import threading as _threading
    import urllib.request

    sim = _start_sim()
    token = "wave4-token"
    client = airsim.MultirotorClient(port=sim.port)
    client.confirmConnection()
    backend = UavBackend(client, HOME, sim=sim)
    store = Store(tmp_path)
    srv = GodseyeUavServer(
        backend, store, token=token, watchdog_s=60.0,
        envelope=SafetyEnvelope(geofence=AO,
                                home=(HOME.latitude, HOME.longitude, HOME.altitude)))

    def answers(url: str) -> bool:
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "wave4", "version": "1"}},
        }).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=2.0) as r:
                return r.status == 200
        except Exception:
            return False

    url = loop = thread = None
    try:
        for port in _W4_PORTS:
            loop = asyncio.new_event_loop()

            def run_loop(loop=loop, port=port):
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(
                        srv.mcp.run_streamable_http_async(
                            host="127.0.0.1", port=port,
                            streamable_http_path="/mcp", stateless_http=True))
                except (RuntimeError, asyncio.CancelledError, SystemExit):
                    pass

            thread = _threading.Thread(target=run_loop, daemon=True)
            thread.start()
            candidate = f"http://127.0.0.1:{port}/mcp"
            end = time.monotonic() + 15.0
            while time.monotonic() < end:
                if answers(candidate):
                    url = candidate
                    break
                if not thread.is_alive():
                    break
                time.sleep(0.2)
            if url:
                break
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5.0)
        assert url is not None, f"no bindable MCP port in {_W4_PORTS[0]}-{_W4_PORTS[-1]}"
        yield {"url": url, "token": token, "server": srv}
    finally:
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5.0)
        srv.stop_monitor()
        try:
            srv.tasking.shutdown()
        except Exception:
            pass
        store.close()
        sim.stop()
        if loop is not None and not loop.is_running():
            loop.close()


def test_wave4_contract_fixes_hold_over_the_real_streamable_http_wire(tmp_path):
    """The whole assignment, re-proven over the wire a harness actually speaks.

    Nothing here touches srv.mcp._tool_manager: the schemas come from
    tools/list, the calls from tools/call and the imagery from resources/read.
    """
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from godseye_uav.missions import coverage_of_path

    with w4_http_server(tmp_path) as stack:
        srv = stack["server"]

        async def main():
            async with httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {stack['token']}"},
                    timeout=120.0) as hc:
                async with streamable_http_client(stack["url"], http_client=hc) as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()

                        async def call(name, **args):
                            res = await session.call_tool(name, args)
                            return json.loads(res.content[0].text)

                        # ---- (1) the contract spellings, as published --------
                        listed = {t.name: t for t in (await session.list_tools()).tools}
                        for name in ("uav_takeoff", "uav_goto_gps"):
                            props = listed[name].input_schema["properties"]
                            assert "alt_agl_m" in props, f"{name} over the wire"
                            assert "alt_m" in props
                            assert "AGL" in listed[name].description

                        spawn = await call("sim_spawn_target",
                                           lat=HOME.latitude + 0.0005,
                                           lon=HOME.longitude, ob_class="mbt")
                        assert spawn["class"] == "mbt" == spawn["ob_class"]

                        # ---- (2) the roster is real, and says so -------------
                        veh = await call("uav_list_vehicles")
                        assert veh["roster_source"] == "sim"
                        assert veh["degraded"] is False
                        assert [v["name"] for v in veh["vehicles"]]

                        # ---- takeoff with the CONTRACT spelling --------------
                        up = await call("uav_takeoff", vehicle="Drone1",
                                        alt_agl_m=25.0)
                        assert up["status"] == "accepted"
                        assert up["alt_agl_m"] == 25.0
                        t_up = await wait_state(srv, "Drone1", up["task_id"],
                                                timeout_s=90.0)
                        assert t_up.state.value == "done", t_up.error

                        # ---- (4) terminal state, in the journal -------------
                        rows = [r for r in srv.store.tasks.read_all()
                                if r.get("task_id") == up["task_id"]]
                        closing = [r for r in rows if r.get("event") == "completed"]
                        assert closing and closing[0]["state"] == "done"

                        # ---- (6) captures really fire on a recon route ------
                        route = [{"lat": HOME.latitude, "lon": HOME.longitude},
                                 {"lat": HOME.latitude + 0.0026,
                                  "lon": HOME.longitude}]
                        recon = await call("mission_recon_route",
                                           vehicle="Drone1", waypoints=route,
                                           alt_agl_m=60.0,
                                           forward_overlap_pct=20.0,
                                           speed_mps=16.0,
                                           idempotency_key="w4-recon")
                        assert recon.get("rejected") is not True, recon
                        assert recon["capture_count"] >= 2
                        assert recon["capture_trigger"] == "distance"

                        # ---- (3) the replay returns the ORIGINAL handle -----
                        again = await call("mission_recon_route",
                                           vehicle="Drone1", waypoints=route,
                                           alt_agl_m=60.0,
                                           forward_overlap_pct=20.0,
                                           speed_mps=16.0,
                                           idempotency_key="w4-recon")
                        assert again["status"] == "duplicate"
                        assert again["mission_handle"] == recon["mission_handle"]
                        assert again["task_id"] == recon["task_id"]
                        st = await call("mission_status",
                                        mission_handle=again["mission_handle"])
                        assert st.get("error") is None, st

                        t_recon = await wait_state(srv, "Drone1",
                                                   recon["task_id"], timeout_s=300.0)
                        assert t_recon.state.value == "done", t_recon.error
                        taken = t_recon.result["captures_taken"]
                        assert taken >= 2, t_recon.result

                        # the frames are retrievable as MCP resources
                        ref = t_recon.result["captures"][0]["resource"]
                        img = (await session.read_resource(ref)).contents[0]
                        assert base64.b64decode(img.blob)[:8] == b"\x89PNG\r\n\x1a\n"

                        # ---- (5) INTREP coverage is imaged ground -----------
                        box = [(47.6405, -122.1425), (47.6405, -122.1385),
                               (47.6432, -122.1385), (47.6432, -122.1425)]
                        grid = await call("mission_grid_search",
                                          vehicle="Drone1", polygon=box,
                                          alt_agl_m=60.0, overlap_pct=20.0,
                                          speed_mps=20.0)
                        assert grid.get("rejected") is not True, grid
                        gtask = srv.tasking.queue_for("Drone1").get(grid["task_id"])
                        end = time.monotonic() + 180.0
                        while time.monotonic() < end and (gtask.waypoint or 0) < 3:
                            if gtask.state.value in ("done", "failed", "cancelled"):
                                break
                            await asyncio.sleep(0.2)
                        await srv.tasking.queue_for("Drone1").abort()
                        await wait_state(srv, "Drone1", grid["task_id"], timeout_s=60.0)
                        rep = await call("uav_target_report",
                                         mission_id=grid["mission_handle"])
                        cov = rep["coverage"]
                        flown = cov["waypoints_flown"]
                        assert flown >= 2, f"grid closed {flown} legs"
                        assert cov["basis"] == "flown"
                        expect = coverage_of_path(
                            box,
                            [(wp["lat"], wp["lon"])
                             for wp in grid["waypoints"][:flown]],
                            float(grid["swath_m"]), basis="flown")
                        assert cov["coverage_pct"] == pytest.approx(
                            expect.coverage_pct, abs=0.01)
                        assert cov["coverage_pct"] != pytest.approx(
                            cov["waypoint_progress_pct"], abs=0.01)
                        assert rep["mission_summary"]["mission_id"] == \
                            grid["mission_handle"]
        asyncio.run(main())


def test_the_capture_schedule_is_never_written_into_the_task_journal(server):
    """A 20 000-entry schedule (missions.MAX_PLAN_CAPTURES) would be ~2 MB per
    row, written three times per task. This project has already lost a run to
    405 MB of capture dicts. The counts are journalled and the elision is
    stated; every frame that fires is in the audit trail."""
    async def main():
        route = [{"lat": HOME.latitude, "lon": HOME.longitude},
                 {"lat": HOME.latitude + 0.0022, "lon": HOME.longitude}]
        h = await tool(server, "mission_recon_route")(
            vehicle="Drone1", waypoints=route, alt_agl_m=60.0,
            forward_overlap_pct=20.0, speed_mps=16.0)
        assert h.get("rejected") is not True, h
        t = await wait_state(server, "Drone1", h["task_id"], timeout_s=240.0)
        assert t.state.value == "done", t.error

        rows = _task_rows(server, h["task_id"])
        assert rows
        for r in rows:
            for payload in (r.get("params") or {}, r.get("result") or {}):
                assert "captures" not in payload, (
                    f"the {r['event']} row carries the full capture list")
            if r["event"] == "submitted":
                assert r["params"]["captures_n"] == h["capture_count"]
                assert "audit trail" in r["params"]["captures_elided"]
        closing = [r for r in rows if r["event"] == "completed"][0]
        assert closing["result"]["captures_n"] == t.result["captures_taken"]
        # the in-memory result still carries the frames in full
        assert len(t.result["captures"]) == t.result["captures_taken"]
    run(main())


def test_mission_status_reports_capture_collection_while_it_flies(server):
    """A harness monitoring a recon mission must be able to see the imagery
    being collected, not just a plan's capture_count and then, eventually, a
    result. It must also not mistake the PLANNED coverage for imaged ground."""
    async def main():
        route = [{"lat": HOME.latitude, "lon": HOME.longitude},
                 {"lat": HOME.latitude + 0.0030, "lon": HOME.longitude}]
        h = await tool(server, "mission_recon_route")(
            vehicle="Drone1", waypoints=route, alt_agl_m=60.0,
            forward_overlap_pct=20.0, speed_mps=12.0)
        assert h.get("rejected") is not True, h
        planned = h["capture_count"]

        seen_mid_flight = None
        end = time.monotonic() + 240.0
        while time.monotonic() < end:
            st = await tool(server, "mission_status")(
                mission_handle=h["mission_handle"])
            assert st["captures_planned"] == planned
            assert st["capture_trigger"] == "distance"
            if st["state"] in ("done", "failed", "cancelled"):
                break
            if st["captures_taken"] > 0 and seen_mid_flight is None:
                seen_mid_flight = st["captures_taken"]
            await asyncio.sleep(0.2)

        t = server.tasking.queue_for("Drone1").get(h["task_id"])
        assert t.state.value == "done", t.error
        assert seen_mid_flight is not None, (
            "capture collection was never observable while the mission flew")
        assert seen_mid_flight <= t.result["captures_taken"]

        final = await tool(server, "mission_status")(
            mission_handle=h["mission_handle"])
        assert final["captures_taken"] == t.result["captures_taken"]
        # planned vs imaged coverage are labelled and kept apart. A recon
        # route has no tasked area at all, so it carries no planned coverage
        # to be mistaken for imaged ground.
        assert final["coverage"] is None
        assert final["coverage_basis"] is None
        assert "must not be substituted" in final["coverage_note"]
    run(main())


# ---------------------------------------------------------------------------
# Wave-4 adversarial verification: three defects the wave-4 fixes themselves
# left behind, each proven at runtime over the stack before being closed here.
# ---------------------------------------------------------------------------

def test_fly_route_auto_takeoff_reads_the_contract_altitude_spelling(server):
    """Wave 4 made `alt_agl_m` the canonical waypoint spelling and taught
    `_fly_legs` to resolve it — but `uav_fly_route`'s auto-takeoff still read
    `wp.get("alt_m", 30.0)`. Measured over the wire: a landed vehicle given a
    120 m `alt_agl_m` route climbed to the silent 30.0 m default.

    A `.get()` default standing in for a datum the caller DID supply is the
    bug class this project has been bitten by most.
    """
    async def main():
        tele = await server.backend.telemetry("Drone1")
        assert int(tele["landed_state"]) == 0, "the vehicle must start landed"

        climbs = []
        real = server.backend.takeoff

        async def spy(vehicle, alt_agl_m=3.0, **kw):
            climbs.append(float(alt_agl_m))
            return await real(vehicle, alt_agl_m=alt_agl_m, **kw)

        server.backend.takeoff = spy
        try:
            h = server._submit("Drone1", "uav_fly_route",
                               {"waypoints": [{"lat": HOME.latitude + 0.0012,
                                               "lon": HOME.longitude + 0.0012,
                                               "alt_agl_m": 120.0}],
                                "speed_mps": 14.0}, None)
            t = await wait_state(server, "Drone1", h["task_id"], timeout_s=180.0)
        finally:
            server.backend.takeoff = real
        assert t.state.value == "done", t.error
        assert climbs, "the landed vehicle was never taken off"
        assert climbs[0] == pytest.approx(120.0, abs=0.01), (
            f"auto-takeoff climbed to {climbs[0]} m for a 120 m alt_agl_m "
            "route — the contract spelling was ignored for a silent default")

    run(main())


def test_fly_route_auto_takeoff_refuses_a_waypoint_with_no_datum(server):
    """The same resolver, so the auto-takeoff cannot invent an altitude for a
    waypoint that `_fly_legs` would refuse to fly."""
    async def main():
        h = server._submit("Drone1", "uav_fly_route",
                           {"waypoints": [{"lat": HOME.latitude + 0.0008,
                                           "lon": HOME.longitude}],
                            "speed_mps": 10.0}, None)
        t = await wait_state(server, "Drone1", h["task_id"], timeout_s=60.0)
        assert t.state.value == "failed", t.state
        assert "altitude" in (t.error or "")
        tele = await server.backend.telemetry("Drone1")
        assert int(tele["landed_state"]) == 0, (
            "the vehicle took off for a route it was never going to fly")
    run(main())


def test_recon_captures_fire_at_the_ground_points_they_were_planned_for(server):
    """M2 along-track has TWO origins and wave 4 conflated them.

    `missions.capture_points` measures `along_track_m` from route waypoint 1;
    `_fly_legs` accumulates distance from wherever the AIRCRAFT is, ingress leg
    included. Measured over the wire: a 936 m ingress onto a 161 m strip took
    all five frames ~935 m away from the tasked route, and the INTREP reported
    "5 of 5 collected, 0 missed" — imagery of the wrong ground, certified
    complete.
    """
    async def main():
        await takeoff_to(server, "Drone1", 60.0)
        # Park the aircraft far from waypoint 1 so the ingress leg dominates.
        far = server._submit("Drone1", "uav_goto_gps",
                             {"lat": HOME.latitude - 0.0060,
                              "lon": HOME.longitude - 0.0060,
                              "alt_agl_m": 60.0, "speed_mps": 25.0}, None)
        t0 = await wait_state(server, "Drone1", far["task_id"], timeout_s=180.0)
        assert t0.state.value == "done", t0.error
        here = await server.backend.telemetry("Drone1")

        route = [{"lat": HOME.latitude + 0.0010, "lon": HOME.longitude + 0.0010},
                 {"lat": HOME.latitude + 0.0022, "lon": HOME.longitude + 0.0022}]
        ingress_m = haversine_m(here["lat"], here["lon"],
                                route[0]["lat"], route[0]["lon"])
        strip_m = haversine_m(route[0]["lat"], route[0]["lon"],
                              route[1]["lat"], route[1]["lon"])
        assert ingress_m > strip_m, (
            f"ingress {ingress_m:.0f} m must dominate the {strip_m:.0f} m strip")

        h = await tool(server, "mission_recon_route")(
            vehicle="Drone1", waypoints=route, alt_agl_m=60.0,
            forward_overlap_pct=30.0, speed_mps=16.0)
        assert h.get("rejected") is not True, h
        t = await wait_state(server, "Drone1", h["task_id"], timeout_s=300.0)
        assert t.state.value == "done", t.error
        res = t.result
        assert res["captures_taken"] >= 2, res

        # every frame is where the schedule said it would be, not one ingress
        # leg short of it
        offsets = []
        for c in res["captures"]:
            pose = c.get("geo_pose") or {}
            assert pose.get("lat") is not None, c
            offsets.append(haversine_m(c["planned_lat"], c["planned_lon"],
                                       pose["lat"], pose["lon"]))
        assert max(offsets) < 60.0, (
            f"worst frame was taken {max(offsets):.0f} m from the point the M2 "
            f"schedule planned it for (ingress was {ingress_m:.0f} m)")
        # ...and nothing was lost reconciling the two origins
        assert res["captures_taken"] + res["captures_missed"] == res["captures_planned"]
    run(main())


def test_a_capture_beyond_the_flown_route_is_a_gap_not_a_silent_drop(server):
    """A trigger the aircraft never reaches must be counted, not dropped.

    `captures_planned - captures_taken - captures_missed` had no reason to be
    zero: leftovers simply stayed in the pending list, so a route could report
    `planned=20, taken=18, missed=0` and the INTREP would call it complete.
    """
    async def main():
        await takeoff_to(server, "Drone1", 50.0)
        route = [{"lat": HOME.latitude, "lon": HOME.longitude, "alt_agl_m": 50.0},
                 {"lat": HOME.latitude + 0.0015, "lon": HOME.longitude,
                  "alt_agl_m": 50.0}]
        flown_m = haversine_m(route[0]["lat"], route[0]["lon"],
                              route[1]["lat"], route[1]["lon"])
        # one trigger on the route, one well past its end
        h = server._submit("Drone1", "uav_fly_route",
                           {"waypoints": route, "speed_mps": 14.0,
                            "captures": [
                                {"along_track_m": 10.0, "camera": "0",
                                 "type": "scene", "lat": route[0]["lat"],
                                 "lon": route[0]["lon"]},
                                {"along_track_m": flown_m + 500.0, "camera": "0",
                                 "type": "scene", "lat": route[1]["lat"],
                                 "lon": route[1]["lon"]}]}, None)
        t = await wait_state(server, "Drone1", h["task_id"], timeout_s=180.0)
        assert t.state.value == "done", t.error
        res = t.result
        assert res["captures_planned"] == 2
        assert res["captures_taken"] == 1
        assert res["captures_missed"] == 1, (
            f"the unreachable trigger was dropped instead of reported: {res}")
        assert res["captures_taken"] + res["captures_missed"] == res["captures_planned"]
        gap = res["capture_failures"][0]
        assert "beyond" in gap["error"] and "no frame was taken" in gap["error"]
        kinds = [r["kind"] for r in server.store.audit_tail(200)]
        assert "capture_missed" in kinds
    run(main())


def test_the_rtb_cruise_leg_leaves_the_descent_its_own_progress_band(server):
    """The 88/12 split is what makes RTB progress mean anything.

    `EXEC_PROGRESS_CEILING` alone only guarantees < 100 %: with the cruise band
    running 0..100 the aircraft would reach home at 99 % and then sit there for
    the whole descent — the same "says it is finished while it is still
    flying" defect, one percentage point quieter. This pins the split itself.
    """
    async def main():
        await takeoff_to(server, "Drone1", 40.0)
        # fly out so the RTB has a real cruise leg to report against
        out = server._submit("Drone1", "uav_goto_gps",
                             {"lat": HOME.latitude + 0.0035,
                              "lon": HOME.longitude + 0.0035,
                              "alt_agl_m": 40.0, "speed_mps": 25.0}, None)
        t0 = await wait_state(server, "Drone1", out["task_id"], timeout_s=180.0)
        assert t0.state.value == "done", t0.error

        h = await tool(server, "uav_return_to_home")(vehicle="Drone1", speed_mps=10.0)
        assert h["status"] == "accepted", h
        task = server.tasking.queue_for("Drone1").get(h["task_id"])

        notes: list[tuple[str, float]] = []

        async def watch(t):
            notes.append((t.progress_note or "", t.progress_pct))

        task.add_progress_hook(watch)
        t = await wait_state(server, "Drone1", h["task_id"], timeout_s=240.0)
        assert t.state.value == "done", t.error

        cruise = [p for note, p in notes if note.startswith("leg ")]
        descent = [p for note, p in notes if note in ("descent", "touchdown")]
        assert cruise and descent, notes[:6]
        assert max(cruise) <= RTB_CRUISE_PCT + 1e-6, (
            f"the cruise leg ran to {max(cruise)}% and left the descent no "
            f"band of its own (ceiling is {RTB_CRUISE_PCT})")
        assert max(descent) > RTB_CRUISE_PCT, (
            "the descent never reported above the cruise band")
        assert max(descent) <= EXEC_PROGRESS_CEILING
        assert task.progress_pct == 100.0
        assert t.result["touchdown"]["landed"] is True
    run(main())


# ------------------------------------------- watchdog vs mission scale -----

#: A geofence big enough to hold a real ISR leg. `AO` is ~3 km across, which is
#: why no test ever flew far enough to trip the watchdog defect below.
WIDE_AO = [(47.50, -122.30), (47.50, -122.00), (47.85, -122.00), (47.85, -122.30)]


def test_a_mission_scale_leg_is_not_killed_by_the_watchdog_while_it_flies(tmp_path):
    """The watchdog was killing tasks that were flying perfectly.

    `Task.report_progress` reset the no-progress clock only when a SINGLE
    report jumped more than `PROGRESS_EPS_PCT`. Telemetry arrives every 0.25 s,
    so one report on a 16.2 km leg at 12 m/s is worth 0.0185 pp — under the
    epsilon. The clock never reset and the task died at exactly `watchdog_s`
    while the aircraft was covering its commanded 12 m/s: measured over the
    wire, a 16.2 km recon route that PASSED the M4 gate failed with "watchdog
    timeout: no progress for 120.0s ... progress 8.9%" after flying 1 445 m in
    121 s.

    Flown here against a 4 s window for 14 s — three and a half windows — on a
    real route through the real executor. The leg is far too long to finish in
    a test; that it is STILL FLYING is the whole point.
    """
    async def main():
        with build_server(tmp_path, watchdog_s=4.0,
                          envelope=SafetyEnvelope(
                              geofence=WIDE_AO,
                              home=(HOME.latitude, HOME.longitude,
                                    HOME.altitude))) as server:
            await takeoff_to(server, "Drone1", 60.0)
            start = await server.backend.telemetry("Drone1")
            # 16.2 km due north of home: 0.1457 deg of latitude.
            route = [{"lat": HOME.latitude + 0.1457, "lon": HOME.longitude,
                      "alt_agl_m": 60.0}]
            leg_m = haversine_m(start["lat"], start["lon"],
                                route[0]["lat"], route[0]["lon"])
            assert leg_m > 15_000.0, f"leg is only {leg_m:.0f} m"
            h = server._submit("Drone1", "uav_fly_route",
                               {"waypoints": route, "speed_mps": 12.0}, None)
            task = server.tasking.queue_for("Drone1").get(h["task_id"])

            deadline = time.monotonic() + 14.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.25)
                assert task.state.value != "failed", (
                    f"the watchdog killed a healthy {leg_m / 1000:.1f} km leg "
                    f"at {task.progress_pct:.1f}%: {task.error}")
            assert task.state.value == "executing", task.state.value

            # it was not surviving by standing still: it really is closing
            now = await server.backend.telemetry("Drone1")
            flown_m = haversine_m(start["lat"], start["lon"],
                                  now["lat"], now["lon"])
            assert flown_m > 80.0, f"only {flown_m:.0f} m flown in 14 s"
            assert 0.0 < task.progress_pct < 5.0, task.progress_pct
            # ...and every individual report was below the epsilon, which is
            # what the old code could not cope with
            step_pct = 100.0 * (12.0 * 0.25) / leg_m
            assert step_pct < 0.05, step_pct

            await server.tasking.queue_for("Drone1").abort(reason="test over")
            await wait_state(server, "Drone1", h["task_id"], timeout_s=30.0)
    run(main())


def test_a_watchdog_kill_of_a_safety_task_is_journalled_as_one(server):
    """A watchdog kill of the un-cancellable force-RTB is not a routine one.

    It is the aircraft's ride home being taken away, and only `_force_rtb`
    re-committing on the next monitor tick brings it back. The audit row has to
    say which kind of task died — reading `watchdog_timeout` rows and finding a
    dead RTB is how this class of bug is caught at all.
    """
    async def main():
        from godseye_uav.tasking import Task

        safety = Task(tool="uav_return_to_home", params={"reason": "bingo"},
                      vehicle="Drone1", safety=True, uncancellable=True)
        await safety.report_progress(31.4)
        assert await server._watchdog_recovery(safety, "watchdog timeout: test")

        harness = Task(tool="uav_fly_route", params={}, vehicle="Drone1")
        assert await server._watchdog_recovery(harness, "watchdog timeout: test")

        rows = [r for r in server.store.audit.read_all()
                if r["kind"] == "watchdog_timeout"]
        by_task = {r["task_id"]: r for r in rows}
        assert by_task[safety.id]["safety"] is True
        assert by_task[safety.id]["uncancellable"] is True
        assert by_task[safety.id]["progress_pct"] == 31.4
        assert by_task[harness.id]["safety"] is False
        assert by_task[harness.id]["uncancellable"] is False
    run(main())


# ------------------------------------------ M7: the identify-ceiling lever --

def test_identify_exposes_the_altitude_ceiling_its_refusal_names(server):
    """M7: `max_id_alt_agl_m` is the remedy the LOS refusal tells the operator
    to use — and over MCP, the only interface a harness has, it was unreachable.

    `identify_plan` has always taken it; `mission_identify_target` and the
    `uav_mission(kind="identify")` dispatcher neither exposed nor forwarded it,
    so the refusal named a lever that did not exist on the wire.
    """
    async def main():
        _, track_id = await spawn_and_scan(server, ob_class="supply_truck")

        # Default: the ID-altitude search may climb no higher than the detect
        # altitude, so the band tops out there.
        base = await tool(server, "mission_identify_target")(
            vehicle="Drone1", track_id=track_id, alt_agl_m=120.0, dry_run=True)
        assert base.get("error") is None, base
        band_lo, band_hi = base["cross_cue"]["id_alt_band_m"]
        assert band_hi == pytest.approx(base["cross_cue"]["detect_alt_agl_m"])

        # Raising the ceiling is what the refusal asks for, and it must reach
        # the planner: the band the ID pass may search actually widens.
        raised = await tool(server, "mission_identify_target")(
            vehicle="Drone1", track_id=track_id, alt_agl_m=120.0,
            max_id_alt_agl_m=band_hi + 150.0, dry_run=True)
        assert raised.get("error") is None, raised
        assert raised["cross_cue"]["id_alt_band_m"] == [
            pytest.approx(band_lo), pytest.approx(band_hi + 150.0)]

        # ...and the same lever through the thin dispatcher the panel uses
        via_dispatch = await tool(server, "uav_mission")(
            vehicle="Drone1", kind="identify",
            params={"track_id": track_id, "alt_agl_m": 120.0,
                    "max_id_alt_agl_m": band_hi + 150.0},
            dry_run=True)
        assert via_dispatch.get("error") is None, via_dispatch
        assert via_dispatch["cross_cue"]["id_alt_band_m"] == \
            raised["cross_cue"]["id_alt_band_m"]

        # A ceiling BELOW the min-AGL floor is overridden by the floor, and the
        # plan says so rather than pretending the lid was honoured.
        lidded = await tool(server, "mission_identify_target")(
            vehicle="Drone1", track_id=track_id, alt_agl_m=200.0,
            max_id_alt_agl_m=20.0, dry_run=True)
        assert lidded.get("error") is None, lidded
        assert any(w.startswith("id_alt_ceiling_below_floor")
                   and "max_id_alt_agl_m" in w for w in lidded["warnings"]), \
            lidded["warnings"]

        # Junk is refused, not silently ignored (it would disable the lever).
        bad = await tool(server, "uav_mission")(
            vehicle="Drone1", kind="identify",
            params={"track_id": track_id, "max_id_alt_agl_m": "as high as it takes"},
            dry_run=True)
        assert bad["error"]["code"] == "invalid_mission_params", bad
        assert "as high as it takes" in bad["error"]["message"]
    run(main())


def test_the_identify_refusal_names_a_parameter_the_wire_actually_has(server):
    """The refusal text and the tool surface must not disagree.

    `identify_plan`'s los_blocked remedy is "raise max_id_alt_agl_m above N m
    AGL". If the MCP tool has no such parameter the operator is told to turn a
    knob that is not on the panel.
    """
    published = server.mcp._tool_manager._tools
    for name in ("mission_identify_target",):
        params = published[name].fn.__code__.co_varnames
        assert "max_id_alt_agl_m" in params, (
            f"{name} does not expose the remedy its own refusal names")
    assert "max_id_alt_agl_m" in published["uav_mission"].description


# ==========================================================================
# Real-world data wired into the server (REAL_DATA_INTEGRATION.md)
#
# `realdata.py` was built, live-verified, and then called from NOWHERE. The
# consequences were all silent: `alt_agl_m` meant height above the LAUNCH
# DATUM, `uav_los_check` answered from a geometric earth-curvature horizon
# with no terrain in it, `sim_spawn_target` defaulted to one hand-entered
# elevation for a whole AO, and the wind pricing the fuel model (M15) was
# whatever an operator typed. These tests pin the wiring — and every one of
# them asserts the OUTCOME (a sight line actually blocked, an AGL that
# actually differs from the launch datum), never that a call was made.
#
# The terrain is REAL. `_DEM_FORDOW` and `_DEM_ISFAHAN` below are heights
# recorded verbatim from God's Eye View's own on-disk terrain cache
# (`.gev-cache/terrain-heights.json`, upstream Re:Earth / Mapterhorn
# ellipsoidal heights), so these tests need neither a network nor a dev
# server. `_RECORDED_ISFAHAN_SITES` is likewise a real recorded
# `/api/military-installations` payload (OpenStreetMap/Overpass, ODbL).
#
# The weather and traffic payloads are NOT recordings — neither feed is in
# that cache. They are hand-built in the documented upstream response shape,
# and are labelled as such here so nothing in this file claims to be a
# measurement that is not one.
# ==========================================================================
#: 185 postings
_DEM_FORDOW = (
    (34.8557, 50.9958, 1080.19), (34.8565, 50.9958, 1076.0), (34.8573, 50.9958,
    1056.08), (34.8581, 50.9958, 1053.58), (34.8589, 50.9958, 1046.14), (34.8597,
    50.9958, 1048.22), (34.8605, 50.9958, 1034.53), (34.8613, 50.9958, 1030.56),
    (34.8621, 50.9958, 1018.69), (34.8629, 50.9958, 1015.93), (34.8637, 50.9958,
    1012.33), (34.8645, 50.9958, 1008.86), (34.865, 50.965, 985.12), (34.865, 50.98,
    988.57), (34.865, 50.995, 1004.95), (34.865, 51.01, 1036.2), (34.865, 51.025,
    947.45), (34.8653, 50.9958, 1005.69), (34.8661, 50.9958, 999.05), (34.8669,
    50.9958, 992.09), (34.8677, 50.9958, 986.37), (34.8685, 50.9958, 981.67),
    (34.8693, 50.9958, 976.94), (34.8701, 50.9958, 972.36), (34.87048, 50.97079,
    960.07), (34.8709, 50.9958, 968.29), (34.87095, 50.97159, 956.66), (34.87143,
    50.97238, 952.64), (34.8717, 50.9958, 964.45), (34.87191, 50.97317, 948.77),
    (34.87238, 50.97397, 946.21), (34.8725, 50.9958, 961.11), (34.87286, 50.97476,
    944.39), (34.8733, 50.9958, 957.77), (34.87333, 50.97555, 943.27), (34.87381,
    50.97635, 942.52), (34.8741, 50.9958, 956.95), (34.87429, 50.97714, 941.31),
    (34.87476, 50.97793, 939.64), (34.8749, 50.9958, 966.01), (34.875, 50.965,
    940.86), (34.875, 50.98, 938.79), (34.875, 50.995, 963.17), (34.875, 51.01,
    1030.99), (34.875, 51.025, 904.15), (34.87524, 50.97873, 937.94), (34.8757,
    50.9958, 985.18), (34.87572, 50.97952, 936.23), (34.87619, 50.98031, 934.49),
    (34.8765, 50.9958, 997.96), (34.87667, 50.98111, 933.36), (34.87714, 50.9819,
    932.19), (34.8773, 50.9958, 976.93), (34.87762, 50.98269, 930.83), (34.8781,
    50.98349, 929.27), (34.8781, 50.9958, 960.18), (34.87857, 50.98428, 928.1),
    (34.8789, 50.9958, 961.7), (34.87905, 50.98508, 928.13), (34.87953, 50.98587,
    941.79), (34.8797, 50.9958, 956.63), (34.88, 50.98666, 947.85), (34.88048,
    50.98746, 928.69), (34.8805, 50.9958, 937.44), (34.88095, 50.98825, 924.29),
    (34.8813, 50.9958, 944.03), (34.88143, 50.98904, 919.41), (34.88191, 50.98984,
    925.12), (34.8821, 50.9958, 944.13), (34.88238, 50.99063, 916.98), (34.88286,
    50.99142, 928.02), (34.8829, 50.9958, 925.57), (34.88334, 50.99222, 934.67),
    (34.8837, 50.9958, 914.63), (34.88381, 50.99301, 917.7), (34.88429, 50.9938,
    912.85), (34.8845, 50.9958, 906.21), (34.88476, 50.9946, 914.4), (34.8849,
    50.9958, 904.14), (34.885, 50.965, 967.58), (34.885, 50.98, 907.14), (34.885,
    50.995, 908.27), (34.885, 51.01, 950.87), (34.885, 51.025, 866.66), (34.88524,
    50.99539, 902.66), (34.8853, 50.9958, 903.01), (34.88532, 50.99663, 914.58),
    (34.88572, 50.99619, 904.78), (34.88573, 50.99747, 904.52), (34.8861, 50.9958,
    903.31), (34.88615, 50.9983, 901.91), (34.88619, 50.99698, 900.84), (34.88657,
    50.99913, 898.98), (34.88667, 50.99777, 894.22), (34.8869, 50.9958, 893.12),
    (34.88698, 50.99997, 898.23), (34.88715, 50.99857, 890.49), (34.8874, 51.0008,
    890.92), (34.88762, 50.99936, 890.85), (34.8877, 50.9958, 889.17), (34.88782,
    51.00163, 895.4), (34.8881, 51.00015, 885.22), (34.88823, 51.00247, 879.48),
    (34.8885, 50.9958, 885.8), (34.88857, 51.00095, 879.86), (34.88865, 51.0033,
    876.36), (34.88905, 51.00174, 874.67), (34.88907, 51.00413, 875.58), (34.8893,
    50.9958, 882.49), (34.88948, 51.00497, 873.28), (34.88953, 51.00254, 872.3),
    (34.8899, 51.0058, 870.33), (34.89, 51.00333, 871.95), (34.8901, 50.9958,
    879.62), (34.89032, 51.00663, 867.26), (34.89048, 51.00412, 869.78), (34.89073,
    51.00747, 864.36), (34.8909, 50.9958, 876.81), (34.89095, 51.00492, 867.61),
    (34.89115, 51.0083, 861.44), (34.89143, 51.00571, 864.93), (34.89157, 51.00913,
    859.03), (34.8917, 50.9958, 873.97), (34.89191, 51.0065, 862.5), (34.89198,
    51.00997, 856.94), (34.89238, 51.0073, 859.49), (34.8924, 51.0108, 855.65),
    (34.8925, 50.9958, 872.1), (34.89282, 51.01163, 854.45), (34.89286, 51.00809,
    856.99), (34.89323, 51.01247, 853.56), (34.8933, 50.9958, 869.29), (34.89334,
    51.00889, 854.11), (34.89365, 51.0133, 852.5), (34.89381, 51.00968, 851.98),
    (34.89407, 51.01413, 851.5), (34.8941, 50.9958, 866.96), (34.89429, 51.01047,
    850.73), (34.89448, 51.01497, 850.54), (34.89476, 51.01127, 848.56), (34.8949,
    50.9958, 865.11), (34.895, 50.965, 875.35), (34.895, 50.98, 875.81), (34.895,
    50.995, 865.14), (34.895, 51.01, 848.92), (34.895, 51.025, 842.17), (34.89524,
    51.01206, 847.36), (34.8957, 50.9958, 862.74), (34.89572, 51.01285, 846.15),
    (34.89619, 51.01365, 845.39), (34.8965, 50.9958, 859.94), (34.89667, 51.01444,
    843.77), (34.89714, 51.01524, 842.6), (34.8973, 50.9958, 858.09), (34.89762,
    51.01603, 841.4), (34.8981, 50.9958, 855.96), (34.8981, 51.01682, 839.69),
    (34.89857, 51.01762, 838.02), (34.8989, 50.9958, 853.47), (34.89905, 51.01841,
    836.31), (34.89952, 51.01921, 834.53), (34.8997, 50.9958, 851.57), (34.9005,
    50.9958, 849.22), (34.9013, 50.9958, 846.9), (34.9021, 50.9958, 844.56),
    (34.9029, 50.9958, 842.72), (34.9037, 50.9958, 840.85), (34.9045, 50.9958,
    838.98), (34.905, 50.965, 852.26), (34.905, 50.98, 847.03), (34.905, 50.995,
    837.77), (34.905, 51.01, 827.22), (34.905, 51.025, 819.67), (34.9053, 50.9958,
    836.98), (34.9061, 50.9958, 834.86), (34.9069, 50.9958, 833.02), (34.9077,
    50.9958, 831.47), (34.9085, 50.9958, 829.82), (34.9093, 50.9958, 827.99),
    (34.9101, 50.9958, 826.16), (34.9109, 50.9958, 824.48), (34.9117, 50.9958,
    822.99), (34.9125, 50.9958, 821.47), (34.9133, 50.9958, 819.8), (34.9141,
    50.9958, 818.43)
)

#: 26 postings
_DEM_ISFAHAN = (
    (32.63, 51.63, 1581.55), (32.63, 51.65, 1581.35), (32.63, 51.67, 1579.6),
    (32.63, 51.69, 1576.68), (32.63, 51.71, 1563.77), (32.6425, 51.63, 1578.93),
    (32.6425, 51.65, 1570.86), (32.6425, 51.67, 1567.97), (32.6425, 51.69, 1571.78),
    (32.6425, 51.71, 1572.68), (32.6546, 51.668, 1579.43), (32.655, 51.63, 1582.02),
    (32.655, 51.65, 1581.01), (32.655, 51.67, 1583.58), (32.655, 51.69, 1571.93),
    (32.655, 51.71, 1570.56), (32.6675, 51.63, 1581.06), (32.6675, 51.65, 1580.81),
    (32.6675, 51.67, 1575.9), (32.6675, 51.69, 1570.15), (32.6675, 51.71, 1569.24),
    (32.68, 51.63, 1577.9), (32.68, 51.65, 1577.04), (32.68, 51.67, 1573.5), (32.68,
    51.69, 1567.45), (32.68, 51.71, 1569.02)
)


#: Real recorded `/api/military-installations` elements for the Isfahan AO
#: (OpenStreetMap / Overpass, ODbL), trimmed from
#: `.gev-cache/military-installations/`. MAPPED data — incomplete, unverified,
#: ISR context only (M14).
_RECORDED_ISFAHAN_SITES = {
    "elements": [
        {"type": "node", "id": 297227347, "lat": 32.6209518, "lon": 51.6875522,
         "tags": {"aeroway": "aerodrome", "landuse": "military", "icao": "OIFP",
                  "military": "airfield", "name:en": "Badr Air Base"}},
        {"type": "way", "id": 196702675,
         "bounds": {"minlat": 32.6164696, "minlon": 51.65141,
                    "maxlat": 32.6223165, "maxlon": 51.6579187},
         "tags": {"landuse": "military",
                  "name:en": "Esfahan Artillery Learning Centre"}},
        {"type": "way", "id": 591216430,
         "bounds": {"minlat": 32.5982978, "minlon": 51.6234922,
                    "maxlat": 32.6027171, "maxlon": 51.6265086},
         "tags": {"landuse": "military", "military": "training_area"}},
        {"type": "way", "id": 942730477,
         "bounds": {"minlat": 32.6032742, "minlon": 51.6449501,
                    "maxlat": 32.6096656, "maxlon": 51.6500412},
         "tags": {"landuse": "military", "military": "range"}},
    ],
}

#: NOT a recording: the documented `/api/weather-effects` response shape with a
#: 15 m/s wind FROM 090. Flying east into it must cost fuel; flying west must
#: not. That direction convention is the whole point of the fixture.
_WEATHER_E15 = {
    "weather": {"windKph": 54.0, "windDirectionDeg": 90.0, "weatherCode": 0.0,
                "visibilityM": 24000.0, "temperatureC": 27.0,
                "cloudCoverPct": 5.0, "precipitationMm": 0.0,
                "observedAt": "2026-09-17T09:00:00Z"},
}

#: NOT a recording: one OpenSky state vector in the documented `/states/all`
#: field order, placed ~3 km from the Fordow home at 940 m HAE — inside the
#: default 9260 m x 300 m deconfliction box around a UAV at that altitude.
_TRAFFIC_ONE_CLOSE = {
    "states": [
        ["4ca7b5", "IRA1234 ", "Iran", 1789625000, 1789625000,
         50.9958, 34.9120, 900.0, False, 128.0, 181.0, 0.0, None, 940.0,
         "7421", False, 0, 0],
    ],
}

#: How far the DEM fixture will reach for a posting. Beyond this it answers
#: with NO height, so the provider takes its flagged fallback: a fixture that
#: served the nearest posting from any distance would be inventing measurements
#: for unmapped ground, which is the failure mode these tests exist to catch.
#: The two recorded products differ in resolution (Fordow ~89 m along the
#: profile, Isfahan a ~1.4 km lattice), so the reach covers the coarser one.
_DEM_REACH_M = 1500.0

_REAL_PORTS = list(range(50100, 50200))
_REAL_PORT = itertools.cycle(
    _REAL_PORTS[os.getpid() % len(_REAL_PORTS):]
    + _REAL_PORTS[:os.getpid() % len(_REAL_PORTS)])

#: The Fordow test geometry, all of it read off `_DEM_FORDOW`:
#:   home   34.8853 N — recorded ground 903.01 m HAE
#:   ridge  34.8765 N — recorded ground 997.96 m HAE  (+95 m, blocks the view)
#:   south  34.8709 N — recorded ground 968.29 m HAE  (beyond the ridge)
FORDOW_HOME = GeoPoint(34.8853, 50.9958, 903.01)
FORDOW_RIDGE = (34.8765, 50.9958)
FORDOW_BEYOND_RIDGE = (34.8709, 50.9958)
ISFAHAN_HOME = GeoPoint(32.6546, 51.668, 1579.43)


def _dem_height(dem, lat, lon):
    """Nearest recorded posting within `_DEM_REACH_M`, else None."""
    best, best_m = None, None
    for p_lat, p_lon, hae in dem:
        d = haversine_m(lat, lon, p_lat, p_lon)
        if best_m is None or d < best_m:
            best, best_m = hae, d
    return best if best_m is not None and best_m <= _DEM_REACH_M else None


def recorded_gev_fetch(dem=(), *, weather=None, installations=None, traffic=None,
                       calls=None):
    """An offline stand-in for the God's Eye View proxies.

    Only the feeds passed in answer; everything else returns 404, which is what
    a dead upstream looks like and is exactly what the degradation flags have to
    survive. `calls` collects every URL so a test can prove a hot path did NOT
    reach for the network.
    """
    from urllib.parse import parse_qs, urlparse

    from godseye_uav.realdata import HttpResponse

    def fetch(url, timeout_s):
        if calls is not None:
            calls.append(url)
        parsed = urlparse(url)
        if parsed.path == "/api/terrain/heights":
            results = []
            for pair in parse_qs(parsed.query)["points"][0].split(";"):
                p_lon, p_lat = (float(v) for v in pair.split(","))
                hae = _dem_height(dem, p_lat, p_lon)
                results.append({"lon": p_lon, "lat": p_lat, "ellipsoid": hae,
                                "geoid": None, "elevation": hae})
            return HttpResponse(200, json.dumps({"results": results}), {})
        if parsed.path == "/api/weather-effects" and weather is not None:
            return HttpResponse(200, json.dumps(weather), {})
        if parsed.path == "/api/military-installations" and installations is not None:
            return HttpResponse(200, json.dumps(installations), {})
        if parsed.path == "/api/opensky" and traffic is not None:
            return HttpResponse(200, json.dumps(traffic),
                                {"x-godseye-source": "opensky"})
        return HttpResponse(404, json.dumps({"error": "feed not in this fixture"}), {})

    return fetch


def real_data_client(dem=(), **kw):
    from godseye_uav.realdata import RealWorldData

    return RealWorldData(origin="http://recorded",
                         fetch=recorded_gev_fetch(dem, **kw),
                         fallback_ground_msl_m=None)


@contextmanager
def real_world_server(tmp_path, *, home, theater_id, client, watchdog_s=90.0,
                      **kw):
    """A server on a REAL place, with the real-data layer on and offline.

    Its own port range (50100-50199) and its own sim home, because the point of
    these tests is terrain relief and the Redmond home has almost none.
    """
    last = None
    sim = None
    for _ in range(len(_REAL_PORTS)):
        sim = FakeAirSim(home=home, port=next(_REAL_PORT))
        try:
            sim.start()
            break
        except OSError as exc:
            last = exc
            try:
                sim.stop()
            except Exception:
                pass
            sim = None
    if sim is None:
        raise RuntimeError(f"no free port in 50100-50199: {last}")
    srv = store = None
    theaters.clear_hydration(theater_id)
    try:
        c = airsim.MultirotorClient(port=sim.port)
        c.confirmConnection()
        backend = UavBackend(c, home, sim=sim)
        store = Store(tmp_path)
        srv = GodseyeUavServer(backend, store, theater=theater_id,
                               watchdog_s=watchdog_s, real_data=client, **kw)
        srv.sim = sim
        yield srv
    finally:
        if srv is not None:
            srv.stop_monitor()
            try:
                srv.tasking.shutdown()
            except Exception:
                pass
        if store is not None:
            store.close()
        sim.stop()
        theaters.clear_hydration(theater_id)


@pytest.fixture
def fordow(tmp_path):
    """Fordow, terrain hydrated from the recorded DEM. Nothing else answers."""
    with real_world_server(tmp_path, home=FORDOW_HOME, theater_id="iran-fordow",
                           client=real_data_client(_DEM_FORDOW)) as srv:
        srv.hydrate_real_data()
        yield srv


# ------------------------------------------------- the switch is honest ----

def test_real_data_is_off_by_default_and_says_so(server):
    """CI, the offline demo and the zero-GPU path must never need a network.

    And the OFF state has to be legible: a harness reading `alt_agl_m` with no
    way to tell it is the launch datum is the whole bug.
    """
    assert server.real_data_enabled is False
    async def main():
        status = await tool(server, "uav_real_data_status")()
        assert status["enabled"] is False
        assert "LAUNCH DATUM" in status["note"]
        tele = await tool(server, "uav_get_telemetry")(vehicle="Drone1")
        assert tele["alt_agl_is_real"] is False
        assert tele["alt_agl_source"] == AGL_SOURCE_LAUNCH
        assert tele["alt_agl_m"] == tele["alt_agl_launch_datum_m"]
        hydrate = await tool(server, "sim_hydrate_real_data")()
        assert hydrate["error"]["code"] == "real_data_disabled"
    run(main())


def test_an_unrecognised_real_data_switch_is_refused_not_read_as_off(monkeypatch):
    """A typo that silently disables real data is indistinguishable from not
    having it. `GODSEYE_REAL_DATA=on please` must fail loudly."""
    monkeypatch.setenv(REAL_DATA_ENV, "on please")
    with pytest.raises(ValueError) as exc:
        resolve_real_data(None)
    assert REAL_DATA_ENV in str(exc.value)
    monkeypatch.setenv(REAL_DATA_ENV, "off")
    assert resolve_real_data(None) is None
    monkeypatch.delenv(REAL_DATA_ENV)
    assert resolve_real_data(None) is None


# --------------------------------------------- AGL measured against terrain ---

def test_agl_is_measured_against_real_terrain_not_the_launch_datum(fordow):
    """THE requirement: "AGL" has meant height above the takeoff point for this
    project's whole life, which is true AGL only over ground at home elevation.

    Flown for real over the Fordow ridge, whose recorded ground is 95 m ABOVE
    the home point: the aircraft holds 40 m on the launch datum and is in fact
    BELOW the ridge crest. Every number below is checked against the recorded
    DEM, and the launch-datum figure is kept alongside so the two can be
    compared rather than one quietly replacing the other.

    FAILS ON THE OLD CODE: `alt_agl_m` was `-ned.z`, so it equalled
    `alt_agl_launch_datum_m` exactly and the delta assertion is 0.
    """
    async def main():
        on_deck = await tool(fordow, "uav_get_telemetry")(vehicle="Drone1")
        assert on_deck["alt_agl_is_real"] is True, on_deck.get("alt_agl_reason")
        assert on_deck["alt_agl_source"] == AGL_SOURCE_TERRAIN
        # On deck the ground under the aircraft is the recorded posting the
        # home point snaps to — ~903 m HAE, the valley floor at Fordow.
        assert on_deck["terrain_hae_m"] == pytest.approx(
            _dem_height(_DEM_FORDOW, *on_deck["terrain_snapped_to"]), abs=0.01)
        assert on_deck["terrain_hae_m"] == pytest.approx(903.0, abs=5.0)
        assert on_deck["terrain_snap_offset_m"] < 250.0

        await takeoff_to(fordow, "Drone1", 40.0)
        h = fordow._submit("Drone1", "uav_goto_gps",
                           {"lat": FORDOW_RIDGE[0], "lon": FORDOW_RIDGE[1],
                            "alt_m": 40.0, "speed_mps": 25.0}, None)
        t = await wait_state(fordow, "Drone1", h["task_id"], timeout_s=180.0)
        assert t.state.value == "done", t.error

        tele = await tool(fordow, "uav_get_telemetry")(vehicle="Drone1")
        assert tele["alt_agl_is_real"] is True, tele.get("alt_agl_reason")
        assert tele["alt_agl_source"] == AGL_SOURCE_TERRAIN
        assert tele["alt_agl_launch_datum_m"] == pytest.approx(40.0, abs=3.0)
        # The AGL is the aircraft's HAE minus the MEASURED ground, exactly.
        assert tele["alt_agl_m"] == pytest.approx(
            tele["alt_hae_m"] - tele["terrain_hae_m"], abs=0.01)
        # ... and that ground is a real recorded posting, not a fabrication.
        assert tele["terrain_hae_m"] == pytest.approx(
            _dem_height(_DEM_FORDOW, *tele["terrain_snapped_to"]), abs=0.01)
        assert tele["terrain"]["provenance"]["real"] is True
        # The ridge is ~95 m above home, so the two AGLs must disagree hugely,
        # and the truth is that the aircraft is BELOW the crest.
        delta = tele["alt_agl_m"] - tele["alt_agl_launch_datum_m"]
        assert delta < -25.0, (
            f"terrain-measured AGL {tele['alt_agl_m']} barely differs from the "
            f"launch datum {tele['alt_agl_launch_datum_m']}; terrain is not wired")
        assert tele["alt_agl_m"] < 0.0

        # And the envelope's min-AGL is now checked against that, which is what
        # a geofence "floor" actually is.
        verdict = await fordow.tick_once("Drone1")
        assert verdict["alt_agl_is_real"] is True
        assert verdict["alt_agl_source"] == AGL_SOURCE_TERRAIN
        assert verdict["alt_agl_m"] < 0.0
        assert "min_agl" in verdict["breaches"], verdict["violations"]
        # On the launch datum the same instant looks like a comfortable 40 m
        # and raises nothing at all. That is the whole difference.
        assert not fordow.envelope.check_position(
            tele["lat"], tele["lon"],
            verdict["alt_agl_launch_datum_m"], landed=False)
    run(main())


def test_terrain_agl_degrades_visibly_off_the_mapped_ground(fordow):
    """Fail soft, but VISIBLE. Over ground the DEM does not cover, the value
    falls back to exactly the old launch-datum behaviour AND says so, with the
    provider's own reason attached. A silent substitution here is the bug that
    made the EGM96 geoid dead code for this entire project."""
    off_map = fordow.resolve_agl(34.8853, 51.0400, 1000.0, 97.0)
    assert off_map["alt_agl_is_real"] is False
    assert off_map["alt_agl_m"] == 97.0 == off_map["alt_agl_launch_datum_m"]
    assert off_map["alt_agl_source"] == AGL_SOURCE_LAUNCH
    assert "LAUNCH DATUM" in off_map["alt_agl_note"]
    assert off_map["alt_agl_reason"], "a synthetic value with no reason"
    assert off_map["terrain"]["provenance"]["degraded"] is True


# ------------------------------------------------------- LOS through terrain --

def test_los_check_is_genuinely_blocked_by_real_terrain(fordow):
    """TOOL_CONTRACT §4.2: the LOS check must never be an unconditional true,
    and M5 standoff depends on it. Until now it answered from a geometric
    earth-curvature horizon with NO terrain in it, so a mountain was invisible.

    The Fordow ridge (recorded ground 997.96 m HAE) sits between the home point
    (903.01) and a point 1.6 km south (968.29). At 40 m AGL the sight line is
    cut by real ground; the sim's own horizon model says clear, so the block can
    only come from terrain.

    FAILS ON THE OLD CODE: no terrain is consulted, `los` is the sim's True.
    """
    async def main():
        await takeoff_to(fordow, "Drone1", 40.0)
        blocked = await tool(fordow, "uav_los_check")(
            vehicle="Drone1", lat=FORDOW_BEYOND_RIDGE[0],
            lon=FORDOW_BEYOND_RIDGE[1], alt_agl_m=5.0, terrain="fetch")
        assert blocked.get("error") is None, blocked
        assert blocked["los_is_measured"] is True, blocked.get("terrain")
        # The sim alone saw nothing in the way. Terrain is what blocks it.
        assert blocked["sim_los"] is True
        assert blocked["los"] is False, "a 95 m ridge of real ground was flown through"
        obstacle = blocked["first_obstacle"]
        assert obstacle["terrain_is_real"] is True
        assert obstacle["terrain_hae_m"] == pytest.approx(
            _dem_height(_DEM_FORDOW, obstacle["lat"], obstacle["lon"]), abs=0.01)
        assert obstacle["obstruction_m"] > 0.0
        assert 0.0 < obstacle["range_m"] < blocked["ground_range_m"]
        assert "bare-earth" in blocked["model"]
        assert "MEASURED" in blocked["los_basis"]

        # Climb over it and the SAME line clears — the block is the geometry,
        # not a check that always says no.
        h = fordow._submit("Drone1", "uav_goto_gps",
                           {"lat": FORDOW_HOME.latitude, "lon": FORDOW_HOME.longitude,
                            "alt_m": 260.0, "speed_mps": 12.0}, None)
        t = await wait_state(fordow, "Drone1", h["task_id"], timeout_s=180.0)
        assert t.state.value == "done", t.error
        over = await tool(fordow, "uav_los_check")(
            vehicle="Drone1", lat=FORDOW_BEYOND_RIDGE[0],
            lon=FORDOW_BEYOND_RIDGE[1], alt_agl_m=5.0, terrain="fetch")
        assert over["los_is_measured"] is True
        assert over["los"] is True, over["first_obstacle"]
        assert over["terrain"]["min_clearance_m"] > 0.0
    run(main())


def test_los_says_when_it_is_not_measured_rather_than_implying_it_is(server):
    """With no terrain layer the old answer still stands — but it must not read
    as terrain-verified. A LOS a harness cannot characterise is worse than none."""
    async def main():
        res = await tool(server, "uav_los_check")(
            vehicle="Drone1", lat=HOME.latitude + 0.0027, lon=HOME.longitude,
            alt_agl_m=0.0)
        assert res["los"] is True
        assert res["los_is_measured"] is False
        assert "ASSUMED" in res["los_basis"]
        assert "terrain" not in res
        bad = await tool(server, "uav_los_check")(
            vehicle="Drone1", lat=HOME.latitude, lon=HOME.longitude,
            alt_agl_m=0.0, terrain="guess")
        assert bad["error"]["code"] == "invalid_parameter"
    run(main())


def test_the_hot_path_never_waits_on_a_terrain_fetch(tmp_path):
    """Design rule 1: NEVER block a telemetry tick or a mission call on a fetch.

    The upstream here takes 3 s per request — a cold terrain proxy really is
    that slow; TERRAIN_TIMEOUT_S is 25 s. A telemetry read and a default LOS
    check must both come back immediately anyway, with the degradation visible,
    and only an explicit terrain='fetch' may wait.
    """
    import time as _time

    slow = recorded_gev_fetch(_DEM_FORDOW)

    def slow_fetch(url, timeout_s):
        _time.sleep(3.0)
        return slow(url, timeout_s)

    from godseye_uav.realdata import RealWorldData
    client = RealWorldData(origin="http://recorded", fetch=slow_fetch,
                           fallback_ground_msl_m=None)
    with real_world_server(tmp_path, home=FORDOW_HOME, theater_id="iran-fordow",
                           client=client) as srv:
        async def main():
            start = _time.monotonic()
            tele = await tool(srv, "uav_get_telemetry")(vehicle="Drone1")
            los = await tool(srv, "uav_los_check")(
                vehicle="Drone1", lat=FORDOW_BEYOND_RIDGE[0],
                lon=FORDOW_BEYOND_RIDGE[1], alt_agl_m=5.0)
            elapsed = _time.monotonic() - start
            assert elapsed < 2.0, (
                f"a telemetry read plus a LOS check took {elapsed:.1f}s — they "
                "waited on the terrain upstream")
            # Both degraded to the old behaviour, and both SAY so.
            assert tele["alt_agl_is_real"] is False
            assert tele["alt_agl_m"] == tele["alt_agl_launch_datum_m"]
            assert los["terrain_mode"] == "cached"
            assert los["los_is_measured"] is False
            assert "ASSUMED" in los["los_basis"]
            # "Degraded to the OLD behaviour" is the load-bearing half, and
            # checking only the flags let the opposite pass: a profile that
            # could not be READ came back `los=False` and was indistinguishable
            # from a measured ridge. The old behaviour is the SIM's verdict.
            assert los["terrain_known"] is False
            assert los["los"] is True, (
                "an unread terrain profile was counted as an obstruction; a "
                "degraded feed must fall back to the sim's answer, flagged, "
                "not fabricate a block")
            assert los.get("terrain_not_consulted"), los
        run(main())


# ------------------------------------------- §4.4 spawn on measured ground ---

def test_spawn_target_defaults_to_measured_terrain_not_one_number_for_the_AO(fordow):
    """§4.4: the altitude default 'never 0'. An earlier wave replaced 0 with the
    theater's hand-entered ground elevation, which is one number for a whole AO
    and is 95 m wrong on the ridge — enough to bury or float a target.

    FAILS ON THE OLD CODE: every spawn came back at the theater elevation.
    """
    async def main():
        theater_elev = fordow.theater.home_alt_msl_m
        on_ridge = await tool(fordow, "sim_spawn_target")(
            lat=FORDOW_RIDGE[0], lon=FORDOW_RIDGE[1], ob_class="sam_medium_range")
        assert on_ridge.get("error") is None, on_ridge
        assert on_ridge["alt_is_real"] is True, on_ridge.get("alt_reason")
        assert on_ridge["alt_source"] == AGL_SOURCE_TERRAIN
        # It stands on the measured ground, which is nowhere near the single
        # theater number that used to be used for every point in the AO.
        assert abs(on_ridge["alt_msl_m"] - theater_elev) > 50.0, on_ridge
        ground = fordow.terrain_at(*FORDOW_RIDGE)
        assert on_ridge["alt_msl_m"] == pytest.approx(ground.msl_m, abs=0.01)
        assert fordow.targets[on_ridge["target_id"]]["alt_is_real"] is True

        # Off the mapped ground it degrades to the theater elevation, visibly.
        off_map = await tool(fordow, "sim_spawn_target")(
            lat=34.8853, lon=51.0400, ob_class="sam_medium_range")
        assert off_map["alt_is_real"] is False
        assert off_map["alt_msl_m"] == pytest.approx(theater_elev, abs=0.01)
        assert off_map["alt_reason"]

        # A caller-supplied altitude is still the caller's, and is not dressed
        # up as measured just because terrain happened to be available.
        explicit = await tool(fordow, "sim_spawn_target")(
            lat=FORDOW_RIDGE[0], lon=FORDOW_RIDGE[1], ob_class="sam_medium_range",
            alt_msl_m=1234.0)
        assert explicit["alt_msl_m"] == 1234.0
        assert explicit["alt_is_real"] is False
        assert explicit["alt_source"] == "caller"
        assert explicit["terrain_default_msl_m"] == pytest.approx(
            ground.msl_m, abs=0.01)
    run(main())


# ------------------------------------------------- M15: real wind -> fuel ----

def test_real_weather_supplies_the_wind_the_fuel_model_burns_against(tmp_path):
    """M15. The fuel integrator prices every leg against a headwind derived from
    the sim's wind vector, and that vector was only ever what an operator typed.

    Here it comes from the observation: 15 m/s FROM 090. Flying east into it,
    the integrator must actually see +15 m/s of headwind, and the range estimate
    must be shorter than the same aircraft in still air.

    FAILS ON THE OLD CODE: `sim_set_weather` had no `source` parameter at all.
    """
    client = real_data_client(_DEM_FORDOW, weather=_WEATHER_E15)
    with real_world_server(tmp_path, home=FORDOW_HOME, theater_id="iran-fordow",
                           client=client) as srv:
        async def main():
            srv.hydrate_real_data()
            obs = srv.real_weather()
            assert obs.real is True and obs.wind_known is True, obs.provenance

            applied = await tool(srv, "sim_set_weather")(source="real")
            assert applied.get("error") is None, applied
            assert applied["source"] == "real"
            # Wind FROM 090 is air moving WEST: (north, east) = (0, -15).
            assert applied["wind_ne_mps"] == pytest.approx([0.0, -15.0], abs=0.05)
            assert applied["wind_source"] == "sim", (
                "the real wind never reached the sim, so the fuel model cannot "
                "see it")
            assert applied["real_weather"]["provenance"]["real"] is True

            # Now fly EAST into it and watch the integrator.
            await takeoff_to(srv, "Drone1", 60.0)
            h = srv._submit("Drone1", "uav_goto_gps",
                            {"lat": FORDOW_HOME.latitude,
                             "lon": FORDOW_HOME.longitude + 0.0040,
                             "alt_m": 60.0, "speed_mps": 12.0}, None)
            fm = srv.fuel_for("Drone1")
            heads: list[float] = []
            deadline = time.monotonic() + 120.0
            while time.monotonic() < deadline:
                t = srv.tasking.queue_for("Drone1").get(h["task_id"])
                verdict = await srv.tick_once("Drone1")
                if verdict.get("telemetry"):
                    heads.append(fm.last_headwind_mps)
                if t and t.state.value in ("done", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.2)
            assert heads, "the fuel integrator never ticked"
            assert max(heads) == pytest.approx(15.0, abs=1.5), (
                f"flying east into a 15 m/s easterly, the integrator saw "
                f"{max(heads)} m/s of headwind")

            tele = await tool(srv, "uav_get_telemetry")(vehicle="Drone1")
            assert tele["wind_ne_mps"] == pytest.approx([0.0, -15.0], abs=0.05)
            ranges = [srv._range_estimate(fm, tele, (0.0, -15.0)),
                      srv._range_estimate(fm, tele, (0.0, 0.0))]
            assert ranges[0]["est_range_km"] < ranges[1]["est_range_km"], (
                "the real headwind did not shorten the range estimate")
        run(main())


def test_applying_real_weather_clears_obscurants_the_observation_does_not_have(tmp_path):
    """Applying a CLEAR observation must leave the sim clear.

    Sending only the non-zero obscurants leaves an operator's earlier fog
    sitting in the sim while the call reports that the weather now matches a
    clear real observation — the substitution is invisible, which is the whole
    failure mode. `_WEATHER_E15` is clear, dry and 24 km visibility.

    FAILS ON THE OLD CODE: it filtered the payload to `> 0.0` and the fog stayed.
    """
    client = real_data_client(_DEM_FORDOW, weather=_WEATHER_E15)
    with real_world_server(tmp_path, home=FORDOW_HOME, theater_id="iran-fordow",
                           client=client) as srv:
        async def main():
            srv.hydrate_real_data()
            fogged = await tool(srv, "sim_set_weather")(fog=0.8)
            assert fogged.get("error") is None, fogged
            assert srv.sim.environment()["weather"]["fog"] == pytest.approx(0.8)
            degraded_range = srv.sim.environment()["detection_range_m"]

            real = await tool(srv, "sim_set_weather")(source="real")
            assert real.get("error") is None, real
            assert real["weather_is_real"] is True
            assert real["weather"]["fog"] == 0.0, real["weather"]
            assert srv.sim.environment()["weather"]["fog"] == 0.0, (
                "the sim is still fogged while the call reported a clear "
                "real observation")
            assert srv.sim.environment()["detection_range_m"] > degraded_range
        run(main())


def test_real_weather_is_refused_rather_than_faked_as_a_dead_calm(tmp_path):
    """A fabricated calm is indistinguishable from a measured one. With no
    weather feed, source='real' must fail loudly instead of applying (0,0)."""
    with real_world_server(tmp_path, home=FORDOW_HOME, theater_id="iran-fordow",
                           client=real_data_client(_DEM_FORDOW)) as srv:
        async def main():
            srv.hydrate_real_data()          # terrain answers, weather 404s
            obs = srv.real_weather()
            assert obs.real is False and obs.wind_ne == (0.0, 0.0)
            out = await tool(srv, "sim_set_weather")(source="real")
            assert out["error"]["code"] == "real_weather_degraded", out
            assert srv.real_weather_applied is None
            wind_ne, _ = await srv.backend.wind_ne()
            assert wind_ne == (0.0, 0.0)     # untouched, not "applied as calm"
            # Mixing the two sources is refused rather than silently resolved.
            mixed = await tool(srv, "sim_set_weather")(source="real", fog=0.5)
            assert mixed["error"]["code"] == "invalid_parameter"
            assert "fog" in mixed["error"]["message"]
            bad = await tool(srv, "sim_set_weather")(source="whatever")
            assert bad["error"]["code"] == "invalid_parameter"
        run(main())


# -------------------------------------------------- real air traffic (M14) ---

def test_deconfliction_uses_the_real_air_picture(tmp_path):
    """Real aircraft in the AO become airspace contacts. ISR only: they are
    traffic to avoid, never targets."""
    client = real_data_client(_DEM_FORDOW, traffic=_TRAFFIC_ONE_CLOSE)
    with real_world_server(tmp_path, home=FORDOW_HOME, theater_id="iran-fordow",
                           client=client) as srv:
        async def main():
            srv.hydrate_real_data()
            out = await tool(srv, "uav_deconflict_airspace")(vehicle="Drone1")
            assert out.get("error") is None, out
            assert out["traffic_is_real"] is True
            assert out["count"] >= 1, out
            row = out["conflicts"][0]
            assert row["contact"]["icao24"] == "4ca7b5"
            assert row["contact"]["callsign"] == "IRA1234"
            assert row["horizontal_m"] < out["separation_box"]["horizontal_m"]
            assert "M14" in out["isr_only"]
            # A contact with NO usable altitude counts as not separated:
            # unknown vertical separation is never read as clearance.
            blind = srv.real_traffic().deconflict(
                FORDOW_HOME.latitude, FORDOW_HOME.longitude, None)
            assert blind and blind[0]["vertical_m"] is None
            assert "cannot be asserted" in blind[0]["basis"]
        run(main())


def test_an_empty_traffic_feed_is_never_reported_as_a_clear_sky(tmp_path):
    with real_world_server(tmp_path, home=FORDOW_HOME, theater_id="iran-fordow",
                           client=real_data_client(_DEM_FORDOW)) as srv:
        async def main():
            srv.hydrate_real_data()          # traffic 404s
            out = await tool(srv, "uav_deconflict_airspace")(vehicle="Drone1")
            assert out["count"] == 0
            assert out["traffic_is_real"] is False
            assert out["traffic"]["provenance"]["degraded"] is True
            assert "not a clear sky" in out["traffic"]["provenance"]["reason"]
        run(main())


# ------------------------------------- a real order of battle binds to §4.4 ---

def test_the_mapped_order_of_battle_actually_spawns(tmp_path):
    """`OrderOfBattle.spawn_requests()` used to emit `class` — a Python keyword
    that `sim_spawn_target(**request)` can never bind — so this path could not
    have worked at all. Here it is, splatted into the real tool.

    Every spawned site stays labelled MAPPED and NON-AUTHORITATIVE (M14).
    """
    client = real_data_client(_DEM_ISFAHAN, installations=_RECORDED_ISFAHAN_SITES)
    with real_world_server(tmp_path, home=ISFAHAN_HOME, theater_id="iran-isfahan",
                           client=client) as srv:
        async def main():
            srv.hydrate_real_data()
            order = srv.real_order_of_battle()
            assert order.real is True and order.sites, order.provenance
            out = await tool(srv, "sim_spawn_order_of_battle")()
            assert out["refused"] == 0, out["refusals"]
            assert out["spawned"] == len(order.sites)
            assert out["authoritative"] is False and out["mapped_data"] is True
            assert "not an order of battle" in out["caveat"].lower()

            from godseye_uav.targets import OB_LIBRARY
            for row in out["sites"]:
                spawn = row["result"]
                assert spawn.get("error") is None, spawn
                assert spawn["ob_class"] in OB_LIBRARY
                # Nobody is buried at 0 m in a theater that sits at ~1580 m.
                assert spawn["alt_msl_m"] > 1000.0, spawn
                # Ground truth really was placed, at the site's own coordinates.
                placed = srv.targets[spawn["target_id"]]
                assert placed["lat"] == pytest.approx(
                    next(s.lat for s in order.sites if s.name == row["name"]))
            names = {row["name"] for row in out["sites"]}
            assert "Badr Air Base" in names, names
            # A site whose position came from a mapped AREA says so rather than
            # presenting a centroid as a fix.
            assert {row["position_source"] for row in out["sites"]} & {
                "bounds-midpoint", "geometry-centroid", "center"}
            # `limit=0` spawned nothing and answered ok/spawned=0 — the same
            # shape a theater with NO mapped sites returns, so a harness could
            # not tell the two apart. A cap that disables the tool is refused.
            for bad in (0, -3):
                res = await tool(srv, "sim_spawn_order_of_battle")(limit=bad)
                assert res.get("error"), (
                    f"limit={bad} was accepted and returned "
                    f"ok={res.get('ok')} spawned={res.get('spawned')}")
                assert res["error"]["code"] == "invalid_parameter"
            one = await tool(srv, "sim_spawn_order_of_battle")(limit=1)
            assert one.get("error") is None and one["spawned"] == 1, one
        run(main())


# ------------------------------------------------ the floor under the fence ---

def test_the_geofence_resource_publishes_a_terrain_floor(fordow):
    """The envelope had no floor: min_agl was measured from the launch datum, so
    a fence over a ridge let the aircraft fly into it while reading a
    comfortable AGL. The skill's ROE 'may only be stricter' — it cannot be,
    without being able to read this.

    FAILS ON THE OLD CODE: the resource had no terrain_floor key at all.
    """
    async def main():
        fence = await read_json(fordow, "uav://safety/geofence")
        floor = fence["terrain_floor"]
        assert floor is not None, fence.keys()
        assert floor["clearance_agl_m"] == fordow.envelope.min_agl_m
        highest = floor["highest_terrain"]
        assert highest["terrain_hae_m"] == pytest.approx(
            _dem_height(_DEM_FORDOW, highest["lat"], highest["lon"]), abs=0.01)
        assert floor["floor_hae_m"] == pytest.approx(
            highest["terrain_hae_m"] + fordow.envelope.min_agl_m, abs=0.01)
        # The AO's high ground really is well above the home point, which is
        # exactly the relief a launch-datum min_agl cannot see.
        assert highest["terrain_hae_m"] - FORDOW_HOME.altitude > 100.0
        assert floor["provenance"]["real"] is True
        assert "MEASURED terrain" in floor["enforced_as"]
        assert fence["units"]["terrain_available"] is True
        assert fence["real_data"]["enabled"] is True
        assert fence["real_data"]["hydrated"] is True
    run(main())


def test_the_geofence_resource_admits_when_there_is_no_terrain_floor(server):
    """With the layer off there is NO floor, and the resource says so rather
    than omitting the key — an absent flag reads as 'fine'."""
    async def main():
        fence = await read_json(server, "uav://safety/geofence")
        assert "terrain_floor" in fence
        assert fence["terrain_floor"] is None
        assert fence["units"]["terrain_available"] is False
        assert "LAUNCH DATUM" in fence["units"]["altitudes"]
        assert fence["real_data"]["enabled"] is False
    run(main())


def test_real_data_status_tells_a_harness_what_is_measured(fordow):
    """The harness has to be able to tell a measured AGL from an assumed one
    WITHOUT inspecting every value. This is that summary."""
    async def main():
        status = await tool(fordow, "uav_real_data_status")(include_feeds=True)
        assert status["enabled"] is True and status["hydrated"] is True
        assert status["terrain_points_cached"] > 0
        # Terrain answered; the other three upstreams did not, and each is named.
        assert "terrain" not in status["degraded_feeds"]
        for feed in ("weather", "traffic", "installations"):
            assert feed in status["degraded_feeds"], status["degraded_feeds"]
            assert status["feeds"][feed]["reason"]
        assert status["real"] is False
        assert any("Re:Earth" in a for a in status["attribution"]), status
        assert status["real_data"]["schema"].startswith("godseye.realdata/")
        # The measured ground really does disagree with the static table, and
        # that is REPORTED, never applied under the running geofence.
        assert status["terrain_delta_m"] is not None
        assert fordow.theater.home_alt_msl_m == 903.0, (
            "the static table was rewritten by hydration")
    run(main())


def test_the_background_refresh_loads_the_AO_terrain_too(tmp_path):
    """The background route is the one a running server actually takes, and it
    has to leave the same thing behind as the blocking one. Hydrating the four
    feeds without loading the AO lattice means every hot-path terrain read is a
    cache miss — real data present and never consulted, which is the whole
    defect this wiring exists to remove.

    FAILS ON THE OLD CODE: the background publish only stored the hydration.
    """
    with real_world_server(tmp_path, home=FORDOW_HOME, theater_id="iran-fordow",
                           client=real_data_client(_DEM_FORDOW)) as srv:
        assert srv.real.terrain.cache_size() == 0
        srv.start_real_data(interval_s=3600.0)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if srv.real_world is not None and srv.real.terrain.cache_size() > 100:
                break
            time.sleep(0.1)
        srv.stop_real_data()
        assert srv.real_world is not None, "the background hydration never ran"
        assert srv.real.terrain.cache_size() > 100, (
            "the background route hydrated the feeds but left the AO terrain "
            "unloaded, so every telemetry tick would miss the cache")
        before = srv.real.terrain.requests
        sample = srv.terrain_at(FORDOW_HOME.latitude, FORDOW_HOME.longitude)
        assert sample.real is True
        assert srv.real.terrain.requests == before


def test_hydration_loads_the_AO_terrain_so_the_hot_path_can_stay_offline(fordow):
    """The non-blocking rule only delivers measured values if the DEM is already
    resident: a cached-only read of a point nobody fetched is a flagged
    fallback. Hydration is what makes the hot path work."""
    resident = fordow.real.terrain.cache_size()
    assert resident > 100, (
        f"only {resident} terrain points resident; every telemetry tick will "
        "miss and AGL will never become measured")
    before = fordow.real.terrain.requests
    # A memory-only read of a resident point: measured, and no request made.
    sample = fordow.terrain_at(FORDOW_HOME.latitude, FORDOW_HOME.longitude)
    assert sample.real is True
    assert fordow.real.terrain.requests == before


# ==========================================================================
# WAVE-5 ADVERSARIAL: the terrain-AGL rewiring put a TERRAIN-relative number
# into fields whose datum is the LAUNCH DATUM. `alt_agl_m` now answers "how
# high am I above the ground I am over?"; every COMMANDED and every PRICED
# altitude in this system answers "how high am I above HOME?" — that is what
# `UavBackend.llh_agl_to_ned` writes, what `SafetyEnvelope`'s limits are
# documented in, and what `FuelModel.bingo_fuel_pct` lets down to 0 over home.
# Over the Fordow ridge the two are 82 m apart and of OPPOSITE SIGN.
# ==========================================================================

async def _park_over_the_ridge(srv, alt_agl_m=40.0):
    """Fly to the Fordow ridge and return the telemetry there."""
    await takeoff_to(srv, "Drone1", alt_agl_m)
    h = srv._submit("Drone1", "uav_goto_gps",
                    {"lat": FORDOW_RIDGE[0], "lon": FORDOW_RIDGE[1],
                     "alt_m": alt_agl_m, "speed_mps": 25.0}, None)
    t = await wait_state(srv, "Drone1", h["task_id"], timeout_s=180.0)
    assert t.state.value == "done", t.error
    tele = await srv._telemetry("Drone1")
    # the premise: the two datums genuinely disagree, and by a lot
    assert tele["alt_agl_is_real"] is True, tele.get("alt_agl_reason")
    assert tele["alt_agl_m"] < -20.0, tele["alt_agl_m"]
    assert tele["alt_agl_launch_datum_m"] > 30.0, tele["alt_agl_launch_datum_m"]
    return tele


def test_the_safety_rtb_cruise_altitude_is_on_the_datum_it_is_commanded_in(fordow):
    """THE defect the terrain rewiring introduced, on the one un-cancellable task.

    `uav_return_to_home` with no explicit `alt_m` holds the height it is
    already at: `max(min_agl + 2, <current AGL>)`. That altitude is handed to
    `llh_agl_to_ned`, which reads it as height above the LAUNCH DATUM. Once
    `<current AGL>` became the TERRAIN-measured value it went NEGATIVE over
    high ground, `max()` chose the envelope floor, and the BINGO force-RTB —
    the task whose entire purpose is to bring the aircraft home — commanded a
    cruise at 5 m above HOME. Over the Fordow ridge that is 908.0 m HAE under
    985.2 m of rock: 77 m underground, for the whole run home.

    FAILS ON THE OLD CODE: the commanded cruise is `min_agl_m + 2` = 5.0 m.
    """
    async def main():
        tele = await _park_over_the_ridge(fordow)
        datum_agl = tele["alt_agl_launch_datum_m"]
        home_hae = fordow.backend.home_geo.altitude

        h = fordow._submit("Drone1", "uav_return_to_home",
                           {"reason": "test", "speed_mps": 12.0}, None,
                           privileged=True, uncancellable=True)
        # Sample the cruise. Only while the aircraft is still well out from
        # home: the RTB ENDS in a landing, and that descent is legitimate.
        lowest = None
        deadline = time.monotonic() + 150.0
        task = fordow.tasking.queue_for("Drone1").get(h["task_id"])
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            t = await fordow._telemetry("Drone1")
            out_m = haversine_m(t["lat"], t["lon"],
                                FORDOW_HOME.latitude, FORDOW_HOME.longitude)
            if out_m > 200.0:
                d = t["alt_agl_launch_datum_m"]
                lowest = d if lowest is None else min(lowest, d)
            if task.state.value in ("done", "failed", "cancelled"):
                break
        assert lowest is not None, "never sampled the cruise"
        assert lowest > datum_agl - 10.0, (
            f"the force-RTB descended to {lowest:.1f} m above the launch datum "
            f"({home_hae + lowest:.1f} m HAE) while cruising out from home; it "
            f"started at {datum_agl:.1f} m and the ground under it is "
            f"{tele['terrain_hae_m']} m HAE. The commanded cruise was taken "
            "from the TERRAIN-measured AGL, which is negative here, so max() "
            f"chose the envelope floor {fordow.envelope.min_agl_m + 2.0:.0f} m.")
        assert lowest > fordow.envelope.min_agl_m + 12.0, lowest
    run(main())


def test_the_preflight_gate_prices_the_plan_in_the_datum_its_waypoints_carry(fordow):
    """M4: the gate integrates a plan whose waypoint altitudes are LAUNCH-DATUM
    AGL, starting from the vehicle's current altitude. Handing it the
    terrain-measured height mixes two datums inside one estimate, so every
    leg's climb/descent is mis-priced — and the published BINGO line stops
    agreeing with the gate, which `FuelModel.preflight_gate` documents as a
    contract ("the gate and the abort line agree").

    Measured over the Fordow ridge: required_pct 47.07 against 45.12 and
    est_time_s 300.6 against 273.3 on a 2.2 km plan, purely from the datum mix.

    FAILS ON THE OLD CODE: `gate["start_alt_agl_m"]` is the negative measured
    AGL, and the gate's own numbers move with it.
    """
    async def main():
        tele = await _park_over_the_ridge(fordow)
        route = [{"lat": FORDOW_HOME.latitude + 0.02,
                  "lon": FORDOW_HOME.longitude, "alt_agl_m": 60.0}]
        gate = await fordow._gate("Drone1", route, 12.0)
        assert gate["start_alt_agl_m"] == pytest.approx(
            tele["alt_agl_launch_datum_m"], abs=2.0), (
            "the gate started its integration from the terrain-measured AGL, "
            "not from the datum its waypoints are written in")
        assert gate["start_alt_agl_datum"] == AGL_SOURCE_LAUNCH
        # the measured figure is KEPT, so nothing is hidden by the correction
        assert gate["start_alt_agl_measured_m"] == pytest.approx(
            tele["alt_agl_m"], abs=2.0)
        assert gate["start_alt_agl_measured_m"] < 0.0 < gate["start_alt_agl_m"]

        # and the abort line the gate quotes is the one the telemetry publishes
        payload = await tool(fordow, "uav_get_telemetry")(vehicle="Drone1")
        assert payload["bingo_fuel_pct"] == pytest.approx(
            gate["bingo_fuel_pct"], abs=0.05), (
            f"the gate quotes BINGO {gate['bingo_fuel_pct']} while telemetry "
            f"publishes {payload['bingo_fuel_pct']}: two datums, two lines")
    run(main())


def test_the_terrain_floor_is_still_raised_when_the_launch_datum_sees_nothing(fordow):
    """The datum correction must not cost the terrain floor that motivated it.

    The envelope's limits are checked TWICE: once on the launch datum (inside
    the monitor, because the same number prices the fuel model's let-down to
    home) and once against the ground actually under the aircraft. Over the
    ridge the launch datum reads a comfortable 40 m and raises nothing at all;
    the measured pass raises min_agl, tags it `datum: "terrain"`, and the tick
    still carries it in `breaches`.
    """
    async def main():
        tele = await _park_over_the_ridge(fordow)
        verdict = await fordow.tick_once("Drone1")
        assert verdict["terrain_floor_checked"] is True
        assert verdict["alt_agl_m"] < 0.0
        assert "min_agl" in verdict["breaches"], verdict["violations"]
        rows = verdict["terrain_floor_violations"]
        assert rows and rows[0]["kind"] == "min_agl"
        assert rows[0]["datum"] == "terrain"
        assert rows[0]["launch_datum_agl_m"] > 30.0
        # the launch-datum pass really did see nothing — that is the point
        assert not fordow.envelope.check_position(
            tele["lat"], tele["lon"], verdict["alt_agl_launch_datum_m"],
            landed=False)
        # ...and it is audited, so it is not only in the live verdict
        kinds = [(r["kind"], r.get("datum")) for r in fordow.store.audit_tail(200)]
        assert ("envelope_breach", "terrain") in kinds, kinds[-10:]
    run(main())


def test_the_bingo_line_is_not_moved_by_the_ground_under_the_aircraft(fordow):
    """`bingo_fuel_pct` prices the let-down to 0 AT HOME, so its altitude is
    height above HOME ground — the launch datum — whatever is underneath.

    FAILS ON THE OLD CODE: the tick priced the line from the terrain AGL, so
    the same aircraft at the same height had two different abort lines
    depending on what it happened to be flying over.
    """
    async def main():
        tele = await _park_over_the_ridge(fordow)
        fm = fordow.fuel_for("Drone1")
        wind_ne, _ = await fordow.backend.wind_ne()
        expected = fm.bingo_fuel_pct((tele["lat"], tele["lon"]),
                                     tele["alt_agl_launch_datum_m"],
                                     wind_ne=wind_ne)
        from_terrain = fm.bingo_fuel_pct((tele["lat"], tele["lon"]),
                                         tele["alt_agl_m"], wind_ne=wind_ne)
        assert abs(expected - from_terrain) > 0.1, (
            "the two datums price the same line identically here, so this test "
            "cannot discriminate")
        verdict = await fordow.tick_once("Drone1")
        assert verdict["bingo"]["bingo_fuel_pct"] == pytest.approx(expected, abs=0.05)
        assert verdict["bingo"]["bingo_fuel_pct"] != pytest.approx(
            from_terrain, abs=0.05)
    run(main())


def test_a_zero_separation_box_is_refused_not_read_as_clear_airspace(tmp_path):
    """A parameter whose value silently disables the check it configures.

    `uav_deconflict_airspace(horizontal_m=0)` filtered on `horizontal > 0`, so
    every contact in the feed was dropped and the tool answered `count: 0,
    traffic_is_real: true` — byte for byte what genuinely clear airspace looks
    like. `vertical_m=0` does the same from the other side: any height
    difference at all then counts as separated.

    FAILS ON THE OLD CODE: both calls return 0 conflicts and no error.
    """
    with real_world_server(tmp_path, home=FORDOW_HOME, theater_id="iran-fordow",
                           client=real_data_client(_DEM_FORDOW,
                                                   traffic=_TRAFFIC_ONE_CLOSE)) as srv:
        async def main():
            srv.hydrate_real_data()
            call = tool(srv, "uav_deconflict_airspace")
            base = await call(vehicle="Drone1")
            assert base.get("error") is None, base
            assert base["traffic_is_real"] is True
            assert base["count"] >= 1, (
                "the fixture has to produce a conflict for this to discriminate")
            for kwargs in ({"horizontal_m": 0.0}, {"vertical_m": 0.0},
                           {"horizontal_m": -1.0}, {"vertical_m": -50.0}):
                res = await call(vehicle="Drone1", **kwargs)
                assert res.get("error"), (
                    f"{kwargs} was accepted and answered "
                    f"count={res.get('count')} traffic_is_real="
                    f"{res.get('traffic_is_real')} — a disabled check wearing "
                    "the shape of clear airspace")
                assert res["error"]["code"] == "invalid_parameter"
            # a real, tighter box is still honoured
            tight = await call(vehicle="Drone1", horizontal_m=1.0, vertical_m=1.0)
            assert tight.get("error") is None, tight
            assert tight["separation_box"] == {"horizontal_m": 1.0,
                                               "vertical_m": 1.0}
        run(main())


def test_an_unread_terrain_profile_is_not_counted_as_an_obstruction(fordow):
    """"Clear only if EVERY model that ANSWERED says clear" — and an UNKNOWN
    terrain cut has not answered.

    `TerrainProvider.line_of_sight` returns `los=False` when no bare-earth
    height is resident along the profile. That is the honest refusal AT THAT
    LAYER. But the LOS profile is deliberately NOT snapped to the terrain
    lattice, and the default `terrain="cached"` mode is memory-only, so a COLD
    profile is unknown for EVERY sight line the first time it is asked.
    ANDing that False into the verdict made the tool answer "blocked" on clear
    ground, with `first_obstacle: None` and a `los_basis` telling the operator
    the answer rested on the sim's geometric horizon — which had said CLEAR.

    Worse, it inverted with the switch: with the real-data layer OFF the same
    call returns `los: True` flagged `los_is_measured: false`. Turning real
    data on must not turn a clear line into a refusal.

    FAILS ON THE OLD CODE: `cold["los"]` is False on a flat, clear 250 m line.
    """
    async def main():
        await takeoff_to(fordow, "Drone1", 120.0)
        near = (FORDOW_HOME.latitude + 0.0022, FORDOW_HOME.longitude)
        cold = await tool(fordow, "uav_los_check")(
            vehicle="Drone1", lat=near[0], lon=near[1], alt_agl_m=5.0)
        assert cold.get("error") is None, cold
        # the premise: the cut really is unknown, and it really does carry the
        # conservative False that used to be ANDed in
        assert cold["los_is_measured"] is False
        assert cold["terrain_known"] is False
        assert cold["terrain"]["known"] is False
        assert cold["terrain"]["los"] is False
        assert cold["los"] is True, (
            "an unread terrain profile was counted as a vote and blocked a "
            f"clear sight line: {cold.get('first_obstacle')!r}")
        assert cold["first_obstacle"] is None
        assert "ASSUMED" in cold["los_basis"]
        assert "did not vote" in cold["terrain_not_consulted"]

        # ...and the SAME line, actually measured, is clear — so this is not a
        # check that has simply been switched off.
        hot = await tool(fordow, "uav_los_check")(
            vehicle="Drone1", lat=near[0], lon=near[1], alt_agl_m=5.0,
            terrain="fetch")
        assert hot["los_is_measured"] is True
        assert hot["los"] is True, hot.get("first_obstacle")
    run(main())


def test_the_mission_planners_los_closure_does_not_refuse_on_an_unread_profile(fordow):
    """The same defect where it costs a mission: `_los_check` is what
    `mission_track_target` and `mission_identify_target` verify the M5 standoff
    with, and neither tool exposes a `terrain` parameter, so the closure is
    always the memory-only path.

    On a COLD profile it returned `los: False` for a flat, clear 800 m line
    over the Fordow valley floor — so the FIRST attempt at an M5-verified
    mission was refused over clear ground, and the same call a few seconds
    later (once the background prefetch landed) succeeded. Non-deterministic
    refusal is worse than either answer.

    FAILS ON THE OLD CODE: the cold call returns los=False.
    """
    async def main():
        await takeoff_to(fordow, "Drone1", 120.0)
        check = fordow._los_check("Drone1")
        olat = FORDOW_HOME.latitude
        olon = FORDOW_HOME.longitude - 0.0045
        tlat, tlon = FORDOW_HOME.latitude, FORDOW_HOME.longitude + 0.0045
        ground = _dem_height(_DEM_FORDOW, tlat, tlon)
        assert ground is not None
        cold = await asyncio.to_thread(check, olat, olon, 120.0, tlat, tlon,
                                       ground + 2.0)
        assert cold["los_is_measured"] is False
        assert cold["terrain_known"] is False
        assert cold["terrain"]["los"] is False      # the unknown cut's refusal
        assert cold["los"] is True, (
            "the planner's LOS closure refused a clear line because the "
            "terrain profile had not been read yet")
        assert "prefetched" in cold["los_terrain_note"]

        # the prefetch it scheduled makes the next ask measured, and the
        # measured answer for this line is also clear
        for _ in range(60):
            await asyncio.sleep(0.25)
            hot = await asyncio.to_thread(check, olat, olon, 120.0, tlat, tlon,
                                          ground + 2.0)
            if hot["los_is_measured"]:
                break
        assert hot["los_is_measured"] is True, "the profile was never prefetched"
        assert hot["los"] is True, hot.get("first_obstacle")
    run(main())


def test_a_partial_terrain_profile_that_saw_a_real_ridge_still_votes():
    """The boundary of "a model that has not answered gets no vote".

    `LosResult.known` is False whenever ANY sample along the profile was
    synthetic — including a profile that read the ground perfectly well for the
    first kilometre and found a ridge there. Dropping that vote because a later
    sample was missing would put the M5 standoff straight back where it was:
    flying through a mountain the DEM actually measured.

    So the rule is `known`, OR a first obstacle that is itself real. A cut with
    no measured heights at all (the cold-profile case) still gets no vote, and
    neither does a "block" invented on a synthetic fallback ground plane.
    """
    from godseye_uav.server import terrain_los_votes

    class Cut:
        def __init__(self, known, obstacle):
            self.known = known
            self.first_obstacle = obstacle

    real_rock = {"lat": 34.8757, "lon": 50.9958, "terrain_hae_m": 985.18,
                 "obstruction_m": 37.76, "terrain_is_real": True}
    invented = {"lat": 34.8757, "lon": 50.9958, "terrain_hae_m": 0.0,
                "obstruction_m": 1.0, "terrain_is_real": False}

    assert terrain_los_votes(None) is False
    assert terrain_los_votes(Cut(True, None)) is True        # fully measured
    assert terrain_los_votes(Cut(True, real_rock)) is True
    assert terrain_los_votes(Cut(False, None)) is False      # the cold profile
    assert terrain_los_votes(Cut(False, real_rock)) is True  # partial, but SAW it
    assert terrain_los_votes(Cut(False, invented)) is False  # not measured ground


# =========================================================================
# T4c — restart recovery: the LOAD half, and the M5 server re-path loop.
#
# Three deserializers were written for restart recovery and had ZERO
# production call sites — `FuelModel.from_dict`, `TrackManager.load_state`,
# `PatternOfLife.load_state` — so `_boot_replay` replayed the task and mission
# journals and rebuilt the fuel integrator, the track store and the
# pattern-of-life store BARE. The consequences are the two tests below.
#
# `tests/test_store.py::test_intel_stores_survive_a_restart` passes on that
# broken code: it round-trips a hand-authored dict through `Store` and never
# constructs a `TrackManager` or a server. Everything here goes through the
# real boot path — a real server, on a real store, after a real flight.
#
# These tests own ports 51000-51099.
# =========================================================================

def _restart_server(tmp_path, **kw):
    """`build_server` on this section's own port range."""
    return build_server(tmp_path, ports=_RESTART_PORTS, nxt=_RESTART_PORT, **kw)


def test_restart_restores_the_fuel_clock_and_the_bingo_latch(tmp_path):
    """T4c: a RESUME after a restart used to hand the vehicle a FULL TANK.

    The aircraft that was at 22 % and LATCHED came back at 100 % and
    un-latched, and `apply_boot_recovery` flew it on. The store side already
    worked — every tick is in `fuel.jsonl` — it was the load side that was
    never called.
    """
    with _restart_server(tmp_path) as srv:
        async def burn():
            await takeoff_to(srv, "Drone1", 60.0)
            for _ in range(6):
                await srv.tick_once("Drone1")
                await asyncio.sleep(0.12)
            fm = srv.fuel_for("Drone1")
            # Below any plausible return leg + reserve: BINGO latches for real,
            # through `_enforce`, exactly as it does in flight.
            fm.fuel_pct = 12.0
            v = await srv.tick_once("Drone1")
            assert v["bingo"]["tripped_now"] is True
            return fm
        flown = run(burn())
        assert flown.bingo.tripped is True
        assert flown.fuel_pct < 100.0 and flown.ticks >= 4
        before = {"fuel_pct": flown.fuel_pct, "phase": flown.last_phase.value,
                  "ticks": flown.ticks, "burned_pct": flown.burned_pct,
                  "elapsed_s": flown.elapsed_s, "airframe": flown.airframe.id,
                  "headwind": flown.last_headwind_mps}
        persisted = srv.store.fuel_state("Drone1")
        assert persisted["fuel_pct"] == pytest.approx(before["fuel_pct"], abs=0.001)
        assert persisted["bingo_latched"] is True

    # --- the process dies; a new one comes up on the SAME store ---
    with _restart_server(tmp_path) as srv2:
        fm = srv2.fuel_for("Drone1")
        assert fm.fuel_pct == pytest.approx(persisted["fuel_pct"], abs=0.001), (
            "the fuel clock reset on restart: the vehicle came back with "
            f"{fm.fuel_pct}% instead of the persisted {persisted['fuel_pct']}%")
        assert fm.fuel_pct < 100.0
        assert fm.bingo.tripped is True, (
            "the BINGO latch came back CLEARED: a vehicle committed to RTB "
            "would be flown on by the boot recovery")
        # The three fields whose journal spelling differs from `from_dict`'s.
        # Feeding the raw row to `from_dict` restores ground / 0.0 / untripped
        # off its own `.get()` defaults and looks like it worked.
        assert fm.last_phase.value == before["phase"]
        assert fm.last_headwind_mps == pytest.approx(before["headwind"], abs=0.001)
        assert fm.ticks == before["ticks"]
        assert fm.burned_pct == pytest.approx(before["burned_pct"], abs=0.001)
        assert fm.elapsed_s == pytest.approx(before["elapsed_s"], abs=0.01)
        assert fm.airframe.id == before["airframe"]
        # The latch detail came off the `force_rtb` audit row, not invented.
        assert fm.bingo.fuel_pct_at_trip is not None
        assert fm.bingo.bingo_pct_at_trip is not None

        row = next(r for r in srv2.boot_restored["fuel"] if r["vehicle"] == "Drone1")
        assert row["bingo_latched"] is True
        assert row["fuel_pct"] == pytest.approx(persisted["fuel_pct"], abs=0.001)
        assert row["fields_absent_from_row"] == []
        kinds = [r["kind"] for r in srv2.store.audit.read_all()]
        assert "restart_state_restored" in kinds


def test_restart_restores_the_track_store_and_the_pattern_of_life_baseline(tmp_path):
    """M11/M12: `tracks.jsonl` and `pattern_of_life.jsonl` were orphaned.

    A persistent track id that does not survive a restart is not persistent,
    and a pattern-of-life baseline that starts empty on every launch has no
    baseline. Both stores were WRITTEN on every ingest and read back by
    nobody.
    """
    poi_lat, poi_lon = HOME.latitude + 0.0006, HOME.longitude
    with _restart_server(tmp_path) as srv:
        async def collect():
            # A POI the contact is standing in, so the M12 baseline actually
            # accrues observations through the real ingest path.
            srv.pol.define_poi("TEST-JUNCTION", poi_lat, poi_lon)
            _tid, track_id = await spawn_and_scan(srv, ob_class="supply_truck",
                                                  scans=3)
            return track_id
        track_id = run(collect())
        base = srv.pol.get("TEST-JUNCTION")
        assert base is not None and base.total_obs >= 1
        before = {"track_id": track_id,
                  "name": srv.tracks.get(track_id).name,
                  "category": srv.tracks.get(track_id).category,
                  "sightings": len(srv.tracks.get(track_id).observations),
                  "total_obs": base.total_obs}
        # the store side (already working) — this is what the load side ignored
        assert srv.store.tracks.get(track_id) is not None
        assert srv.store.pattern_of_life.get("TEST-JUNCTION") is not None

    with _restart_server(tmp_path) as srv2:
        recovered = srv2.tracks.get(track_id)
        assert recovered is not None, (
            f"track {track_id} did not survive the restart: the M11 persistent "
            "id refers to nothing in the new process")
        assert recovered.track_id == before["track_id"]
        assert recovered.name == before["name"]
        assert recovered.category == before["category"]
        assert len(recovered.observations) == before["sightings"]
        # a new id is still minted under this process's own prefix
        assert srv2.tracks.new_track_id() != before["track_id"]

        pol = srv2.pol.get("TEST-JUNCTION")
        assert pol is not None, (
            "the M12 pattern-of-life baseline did not survive the restart, so "
            "the deviation indicator starts from zero on every launch")
        assert pol.total_obs == before["total_obs"]
        # the theater's own seeded POIs are still there alongside it
        assert set(srv2.pol.pois()) >= {"TEST-JUNCTION"}

        restored = srv2.boot_restored
        assert restored["tracks"]["restored"] >= 1
        assert restored["tracks"]["dropped"] == []
        assert restored["pattern_of_life"]["restored"] >= 1
        assert restored["pattern_of_life"]["dropped"] == []


def test_restart_does_not_silently_default_a_partial_fuel_row(tmp_path):
    """No silent fallback: a row that does not say is not read as "fine".

    A hand-seeded row (the shape `test_boot_replay_*` writes) carries neither
    the airframe nor `bingo_latched`. The latch is NOT restored from it — and
    the gap is REPORTED rather than resolved into an un-tripped latch, which is
    the shape that flies a committed aircraft back out to the target.
    """
    with Store(tmp_path) as seed:
        seed.log_fuel("Drone1", 63.5, "cruise", bingo_fuel_pct=25.0)
    with _restart_server(tmp_path) as srv:
        fm = srv.fuel_for("Drone1")
        assert fm.fuel_pct == pytest.approx(63.5, abs=0.001)
        assert fm.last_phase.value == "cruise"
        assert fm.bingo.tripped is False
        row = next(r for r in srv.boot_restored["fuel"] if r["vehicle"] == "Drone1")
        assert set(row["fields_absent_from_row"]) >= {"bingo_latched", "airframe",
                                                      "ticks", "burned_pct"}
        assert any("bingo_latched" in n for n in row["notes"]), row["notes"]
        assert any("airframe" in n for n in row["notes"]), row["notes"]


def test_the_server_repaths_a_track_target_mission_onto_a_moving_contact(tmp_path):
    """M5 / PLAN §4.3 "server re-path loop".

    `missions.repath_needed` / `missions.repath_track` were unit tested and
    called from nowhere — `grep repath server.py` returned zero hits, and
    `missions.py` carried the note "call repath_track() when repath_needed() is
    true (M5)" addressed to a caller that did not exist. So over MCP a moving
    contact was orbited at its FIRST fix for the whole flight: the mission was
    named track_target and did not track.

    The contact is moved on the live track the way a sensor update moves it
    (`Track.update`), 1 km north — far outside the 300 m standoff ring the
    mission was launched on, so "the aircraft is on the ring around the NEW
    fix" cannot be satisfied by the old route.
    """
    with _restart_server(tmp_path, watchdog_s=120.0) as srv:
        async def main():
            _target, track_id = await spawn_and_scan(srv, ob_class="supply_truck")
            trk = srv.tracks.get(track_id)
            old_lat, old_lon = trk.lat, trk.lon
            out = await tool(srv, "mission_track_target")(
                vehicle="Drone1", track_id=track_id, alt_agl_m=60.0,
                speed_mps=18.0, points=4)
            assert out.get("error") is None, out
            assert out.get("rejected") is not True, out
            mid = out["mission_id"]
            standoff = float(out["standoff_m"])
            assert mid in srv._repath, "the re-path loop was never armed"
            # The plan's own cadence is 15 s, which outlasts this flight; the
            # decision is asked at every leg boundary instead. `repath_needed`
            # still decides, and it is still the plan's own tolerance.
            srv._repath[mid]["interval_s"] = 0.0
            assert srv._repath[mid]["plan"].meta["poi"] == [old_lat, old_lon]

            # the contact drives ~1 km north, 33x the plan's own re-acquire
            # tolerance for a 300 m ring
            new_lat, new_lon = old_lat + 0.009, old_lon
            trk.update(new_lat, new_lon, trk.alt_m, time.time())

            reps: list = []
            reached_new_ring = False
            end = time.monotonic() + 200.0
            while time.monotonic() < end:
                task = srv.tasking.queue_for("Drone1").get(out["task_id"])
                if task is None or task.state.value in ("done", "failed", "cancelled"):
                    break
                reps = srv.missions[mid].get("repaths") or []
                if reps and (task.waypoint or 0) >= reps[0]["leg_index"] + 2:
                    reached_new_ring = True
                    break
                await asyncio.sleep(0.25)

            assert reps, (
                "the contact moved 1 km and the orbit was never re-centred — "
                "mission_track_target flew the ring around a fix the contact "
                "had left")
            assert reached_new_ring, (
                "the re-planned ring was recorded but no leg of it closed")
            assert reps[0]["moved_m"] > 900.0
            assert reps[0]["poi"] == [round(new_lat, 6), round(new_lon, 6)]
            assert reps[0]["standoff_m"] == pytest.approx(standoff, abs=1.0)

            # THE OUTCOME: the aircraft is flying the ring around the contact's
            # CURRENT fix, not the one it was tasked on.
            tele = await tool(srv, "uav_get_telemetry")(vehicle="Drone1")
            d_new = haversine_m(tele["lat"], tele["lon"], new_lat, new_lon)
            d_old = haversine_m(tele["lat"], tele["lon"], old_lat, old_lon)
            assert abs(d_new - standoff) < 90.0, (
                f"the aircraft is {d_new:.0f} m from the contact's current fix, "
                f"not on its {standoff:.0f} m standoff ring")
            assert d_old > standoff + 300.0, (
                f"the aircraft is still {d_old:.0f} m from the ABANDONED fix")

            # and the server says so, on the wire and in the journals
            st = await tool(srv, "mission_status")(mission_handle=mid)
            assert st["repath_count"] >= 1
            # The record's `waypoints`/`meta` were rewritten to the ring being
            # flown, but `gate` — the M4 pre-flight verdict, and the
            # `bingo_fuel_pct` mission_status falls back to — still describes
            # the ring this mission LAUNCHED on. Half a record updated and half
            # not, with nothing saying which, is how a stale number gets read
            # as a current one.
            assert st["gate_covers_current_route"] is False
            assert st["track_poi"] == [new_lat, new_lon]
            assert st["repath_tolerance_m"] == pytest.approx(
                max(25.0, standoff * 0.10), abs=0.1)
            assert any(r["kind"] == "repath" and r.get("mission_id") == mid
                       for r in srv.store.audit.read_all())
            assert any(r.get("event") == "repath" and r.get("mission_id") == mid
                       for r in srv.store.missions.read_all())
            await srv.tasking.queue_for("Drone1").abort()
        run(main())


def test_a_still_contact_is_never_repathed(tmp_path):
    """The loop is armed and answers NO while the contact holds its fix.

    A re-path loop that fires on a stationary contact would re-plan (and
    re-verify LOS on) every leg boundary of every track_target mission for no
    reason — and `repath_needed`'s tolerance would be decorative.
    """
    with _restart_server(tmp_path, watchdog_s=120.0) as srv:
        async def main():
            _target, track_id = await spawn_and_scan(srv, ob_class="supply_truck")
            out = await tool(srv, "mission_track_target")(
                vehicle="Drone1", track_id=track_id, alt_agl_m=60.0,
                speed_mps=18.0, points=4)
            assert out.get("error") is None, out
            mid = out["mission_id"]
            srv._repath[mid]["interval_s"] = 0.0     # ask at every leg
            end = time.monotonic() + 90.0
            while time.monotonic() < end:
                task = srv.tasking.queue_for("Drone1").get(out["task_id"])
                if task is None or task.state.value in ("done", "failed", "cancelled"):
                    break
                if (task.waypoint or 0) >= 2:
                    break
                await asyncio.sleep(0.25)
            assert not (srv.missions[mid].get("repaths") or []), (
                "a contact that never moved was re-pathed")
            st = await tool(srv, "mission_status")(mission_handle=mid)
            assert st["repath_count"] == 0
            assert st["repath_loop_active"] is True
            await srv.tasking.queue_for("Drone1").abort()
        run(main())


# =========================================================================
# Adversarial pass over the T4c / M5 delivery. Every claim below was
# REPORTED as delivered with no test that discriminates it, or names a
# behaviour the delivered code does not have. Same port range (51000-51099).
# =========================================================================

#: A full fuel row as `FuelModel.fuel_record()` spells it, for a restart whose
#: previous process is not re-flown for every test.
def _seed_fuel_row(tmp_path, **over):
    row = {"airframe": "quad_suas_electric", "burned_pct": 78.5,
           "elapsed_s": 912.0, "ticks": 1824, "headwind_mps": 3.2,
           "bingo_latched": True, "clamped_ticks": 0, "unaccounted_s": 0.0,
           "bingo_fuel_pct": 25.0}
    row.update(over)
    fuel_pct = row.pop("fuel_pct", 21.5)
    phase = row.pop("phase", "cruise")
    with Store(tmp_path) as seed:
        seed.log_fuel("Drone1", fuel_pct, phase, **row)
    return {"fuel_pct": fuel_pct, "phase": phase, **row}


def test_the_safety_resource_publishes_what_the_restart_put_back(tmp_path):
    """`uav://safety/geofence` must say what came back, not just what was decided.

    A restart that came back BARE — full tank, un-tripped latch, empty track
    store — is indistinguishable over MCP from a fresh launch unless the
    recovered state is readable. The `restart_recovery.restored` block is the
    only place it is.
    """
    seeded = _seed_fuel_row(tmp_path)
    with _restart_server(tmp_path) as srv:
        fence = run(read_json(srv, "uav://safety/geofence"))
        rr = fence.get("restart_recovery")
        assert isinstance(rr, dict), (
            "uav://safety/geofence does not publish restart_recovery, so a "
            "restart that recovered nothing reads exactly like a fresh launch")
        assert "decisions" in rr and isinstance(rr["decisions"], dict)
        restored = rr["restored"]
        row = next(r for r in restored["fuel"] if r["vehicle"] == "Drone1")
        assert row["restored"] is True
        assert row["fuel_pct"] == pytest.approx(seeded["fuel_pct"], abs=0.01)
        assert row["bingo_latched"] is True
        assert row["phase"] == seeded["phase"]
        # the intel halves are reported even when there is nothing to report
        assert set(restored["tracks"]) >= {"persisted", "restored", "dropped"}
        assert set(restored["pattern_of_life"]) >= {"persisted", "restored",
                                                    "dropped"}
        # and the live integrator really is the recovered one
        assert srv.fuel_for("Drone1").fuel_pct == pytest.approx(
            seeded["fuel_pct"], abs=0.01)


def test_a_fuel_row_naming_an_unknown_airframe_does_not_kill_the_boot(tmp_path):
    """A journal row this build cannot rebuild must be REPORTED, not fatal.

    `get_airframe` raises on an unknown id by design (a fuel clock priced by
    the wrong airframe is a wrong BINGO line stated with confidence). Feeding
    the journal straight into `FuelModel.from_dict` therefore turned one stale
    row — a profile retired between builds — into a server that will not
    CONSTRUCT at all, for every vehicle, with nothing in the audit trail.

    Neither is acceptable, and neither is the other silent shape: coming back
    at 100 % with an un-tripped latch and saying nothing is the exact state
    restart recovery exists to stop `apply_boot_recovery` resuming on.
    """
    _seed_fuel_row(tmp_path, airframe="retired_profile_v1")
    with _restart_server(tmp_path) as srv:          # must not raise
        row = next(r for r in srv.boot_restored["fuel"] if r["vehicle"] == "Drone1")
        assert row["restored"] is False
        assert "retired_profile_v1" in row["reason"], row["reason"]
        assert row["row_fuel_pct"] == pytest.approx(21.5, abs=0.01)
        assert row["row_bingo_latched"] is True
        # ...and it does NOT claim the flown state was recovered
        assert row["fuel_pct"] == 100.0
        assert row["bingo_latched"] is False
        assert any("NOT restored" in n for n in row["notes"]), row["notes"]
        assert srv.boot_restored["fuel_not_restored"] == ["Drone1"]
        dropped = [r for r in srv.store.audit.read_all()
                   if r["kind"] == "restart_state_dropped"
                   and r.get("vehicle") == "Drone1"]
        assert dropped, ("the fuel clock was not restored and the audit trail "
                         "does not say so")
        assert dropped[0]["row_airframe"] == "retired_profile_v1"
        summary = [r for r in srv.store.audit.read_all()
                   if r["kind"] == "restart_state_restored"]
        assert summary and "NOT RESTORED" in summary[-1]["message"], (
            "the summary counts an un-restored vehicle as restored")


def test_a_journal_that_names_another_airframe_reprices_the_recovered_burn(tmp_path):
    """from_dict's documented rule, applied by the restore: the JOURNAL wins.

    Replaying a burn under a different energy model re-prices every second of
    it, so the airframe travels with the state and brings its own rate table.
    Keeping this process's configured profile would recover the right fuel
    percentage on the wrong burn curve — and the BINGO line derived from it
    would be wrong, quietly.
    """
    from godseye_uav.safety import AIRFRAMES, DEFAULT_AIRFRAME_ID, get_airframe
    other = next(i for i in sorted(AIRFRAMES) if i != DEFAULT_AIRFRAME_ID)
    _seed_fuel_row(tmp_path, airframe=other)
    with _restart_server(tmp_path) as srv:
        fm = srv.fuel_for("Drone1")
        assert fm.airframe.id == other, (
            f"the recovered burn is priced by {fm.airframe.id!r}, but the "
            f"journal says it was flown by {other!r}")
        profile = get_airframe(other)
        assert fm.rates == profile.rates_pct_per_s(), (
            "the fuel percentage came back but the BURN TABLE did not: the "
            "recovered clock is priced by the wrong energy model")
        assert fm.capacity_s_cruise == pytest.approx(profile.endurance_cruise_s)
        # the two profiles really do differ, so the assertions above bite
        default = get_airframe(DEFAULT_AIRFRAME_ID)
        assert default.endurance_cruise_s != profile.endurance_cruise_s
        assert default.rates_pct_per_s() != profile.rates_pct_per_s()
        row = next(r for r in srv.boot_restored["fuel"] if r["vehicle"] == "Drone1")
        assert row["airframe"] == other
        assert any("JOURNAL" in n for n in row["notes"]), row["notes"]
        assert "airframe" not in row["fields_absent_from_row"]


def _repath_mission(srv, track_id, **kw):
    """Launch a track_target and make the loop decide at every leg boundary."""
    async def go():
        out = await tool(srv, "mission_track_target")(
            vehicle="Drone1", track_id=track_id, alt_agl_m=60.0,
            speed_mps=18.0, points=4, **kw)
        assert out.get("error") is None and out.get("rejected") is not True, out
        srv._repath[out["mission_id"]]["interval_s"] = 0.0
        return out
    return go


def test_the_repath_is_refused_when_the_replanned_standoff_does_not_verify(tmp_path):
    """M5/M14: the contact moved behind a ridge. Do NOT close on it.

    The re-plan is verified under the mission's OWN line-of-sight policy. When
    it does not verify, the aircraft finishes the ring it is on — it does not
    trade the verified standoff for an unverified one, and the refusal is
    journaled rather than dropped.
    """
    with _restart_server(tmp_path, watchdog_s=120.0) as srv:
        async def main():
            _t, track_id = await spawn_and_scan(srv, ob_class="supply_truck")
            trk = srv.tracks.get(track_id)
            old = [trk.lat, trk.lon]
            out = await _repath_mission(srv, track_id)()
            mid = out["mission_id"]
            # a mast on the contact's NEW fix masks every point of any ring
            # around it, so the re-plan cannot verify from anywhere
            new_lat, new_lon = old[0] + 0.0035, old[1]
            srv.sim.add_obstruction(new_lat, new_lon, HOME.altitude + 400.0,
                                    radius_m=80.0, name="mast")
            trk.update(new_lat, new_lon, trk.alt_m, time.time())

            refused = None
            end = time.monotonic() + 180.0
            while time.monotonic() < end:
                task = srv.tasking.queue_for("Drone1").get(out["task_id"])
                refused = [r for r in srv.store.audit.read_all()
                           if r["kind"] == "repath_refused"
                           and r.get("mission_id") == mid]
                if refused:
                    break
                if task is None or task.state.value in ("done", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.25)
            assert refused, (
                "the contact moved behind a mast and the re-path was neither "
                "applied nor refused in the journal")
            assert refused[0]["error"]["code"] == "los_blocked", refused[0]
            st = await tool(srv, "mission_status")(mission_handle=mid)
            assert st["repath_count"] == 0, (
                "an UNVERIFIED standoff was flown at a contact that had moved "
                "behind a mast")
            assert st["track_poi"] == old, (
                "the ring was re-centred anyway; track_poi moved")
            assert st["gate_covers_current_route"] is True
            assert not (srv.missions[mid].get("repaths") or [])
            await srv.tasking.queue_for("Drone1").abort()
        run(main())


def test_the_repath_is_skipped_when_the_track_leaves_the_store(tmp_path):
    """The contact is gone from the store: say so, do not re-centre on nothing."""
    with _restart_server(tmp_path, watchdog_s=120.0) as srv:
        async def main():
            _t, track_id = await spawn_and_scan(srv, ob_class="supply_truck")
            out = await _repath_mission(srv, track_id)()
            mid = out["mission_id"]
            srv.tracks._tracks.pop(track_id)
            assert srv.tracks.get(track_id) is None

            skipped = None
            end = time.monotonic() + 180.0
            while time.monotonic() < end:
                task = srv.tasking.queue_for("Drone1").get(out["task_id"])
                skipped = [r for r in srv.store.audit.read_all()
                           if r["kind"] == "repath_skipped"
                           and r.get("mission_id") == mid]
                if skipped:
                    break
                if task is None or task.state.value in ("done", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.25)
            assert skipped, (
                "the track left the store mid-mission and the re-path loop "
                "said nothing about it")
            assert track_id in skipped[0]["message"]
            st = await tool(srv, "mission_status")(mission_handle=mid)
            assert st["repath_count"] == 0
            await srv.tasking.queue_for("Drone1").abort()
        run(main())


class _StubTask:
    """The three attributes `_fly_legs` touches on a task."""

    id = "T-repath-captures"
    mission_id = None
    waypoint = None

    async def report_progress(self, pct, **kw):
        return None


def test_a_repath_is_refused_for_a_route_carrying_pending_captures(tmp_path):
    """M2 trigger distances belong to the route being replaced.

    Firing them against a substituted route images ground nobody tasked, and
    the tally would still read "n of n collected" — the exact silent shape the
    capture accounting exists to make impossible. So the flight fails loudly
    and NO frame is taken against the wrong route.
    """
    with _restart_server(tmp_path, watchdog_s=120.0) as srv:
        async def main():
            await takeoff_to(srv, "Drone1", 40.0)

            async def repath(_leg):
                return [{"lat": HOME.latitude + 0.004,
                         "lon": HOME.longitude - 0.004, "alt_agl_m": 40.0}]

            wps = [{"lat": HOME.latitude + 0.0008 * k, "lon": HOME.longitude,
                    "alt_agl_m": 40.0} for k in (1, 2, 3)]
            caps = [{"along_track_m": 40.0, "camera": "0", "type": "scene"}]
            shots_before = len([r for r in srv.store.audit.read_all()
                                if r["kind"] == "capture_image"])
            with pytest.raises(ValueError, match="not transferable"):
                await srv._fly_legs(_StubTask(), "Drone1", wps, 12.0,
                                    captures=caps, repath=repath)
            shots_after = [r for r in srv.store.audit.read_all()
                           if r["kind"] == "capture_image"]
            assert len(shots_after) == shots_before, (
                "a scheduled capture fired against the SUBSTITUTED route")
            tally = srv._capture_progress.get(_StubTask.id) or {}
            assert tally.get("captures_taken", 0) == 0
        run(main())


def test_a_restored_bingo_latch_still_commits_the_vehicle_to_rtb(tmp_path):
    """The OUTCOME of restoring the latch, not the fact that it is restored.

    BINGO is un-clearable by doctrine: once the aircraft is committed it stays
    committed even if the line later falls below it — the headwind eases, the
    aircraft descends, the return leg gets cheaper. So the case that separates
    "the latch came back" from "the fuel came back" is a vehicle whose
    RECOVERED fuel is comfortably ABOVE the line: a fresh latch would not trip
    on it, and without the restored one the restart hands the harness a
    vehicle that is free to fly on.

    `_enforce` reads `bingo.latched`, not the 0->1 edge, so a correctly
    restored latch re-commits the vehicle on the very first tick.
    """
    _seed_fuel_row(tmp_path, fuel_pct=40.0, bingo_latched=True)
    with _restart_server(tmp_path) as srv:
        fm = srv.fuel_for("Drone1")
        assert fm.bingo.tripped is True
        async def main():
            tele = await tool(srv, "uav_get_telemetry")(vehicle="Drone1")
            line = tele["bingo_fuel_pct"]
            # the discriminator: a FRESH latch would not have tripped here
            assert fm.fuel_pct > line + 5.0, (
                f"fuel {fm.fuel_pct}% is not comfortably above the "
                f"{line}% line; this test would pass without the latch")
            await srv.tick_once("Drone1")
            await asyncio.sleep(0.2)
        run(main())
        rtb = [r for r in srv.store.audit.read_all()
               if r["kind"] == "force_rtb" and r.get("vehicle") == "Drone1"]
        assert rtb, (
            "the vehicle came back committed to RTB and the first tick flew it "
            "on: no force_rtb was commanded")
        assert rtb[-1]["reason"] == "bingo", rtb[-1]
        assert srv.mission_flags.get("Drone1") == MISSION_INCOMPLETE_FUEL
        assert srv.fuel_for("Drone1").bingo.tripped is True
