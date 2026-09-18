"""Harness entrypoint tests: theater wiring, datum-at-ingest, real MCP demo.

Three register findings are guarded here, each of which the pre-Wave-2 code
fails:

1. `launch.py` carried its OWN hand-typed THEATERS dict (a third copy), whose
   "iran-isfahan" was actually Natanz — 118.6 km and -20 m from the canonical
   entry (taiwan-strait 43.9 km, ukraine-donbas 3.7 km / -50 m). Measured
   against that table, the canonical demo mission is geofence-rejected at every
   one of its 14 waypoints for iran-isfahan and taiwan-strait, and the old
   `demo_mission.py` could not even be asked for the launcher's default
   theater: its own choices were iran-isfahan/indo-pak-loc/"redmond".
2. `launch.py` fed theater home altitudes, which are MSL ground elevations,
   straight into `GeoPoint(...)`, whose altitude field is HAE (T1). At the
   shipped theaters that is a -33.2 m .. +14.6 m lie about where the ground is.
3. `scripts/demo_mission.py` reached into `srv.mcp._tool_manager._tools` and
   awaited the tool functions in-process (R7), so it exercised no transport, no
   Bearer auth and no queue, and was invisible in the command center.

The MCP tests here stand up a real `GodseyeUavServer` on loopback and drive it
over Streamable HTTP, because that is the only way to prove (3).
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import airsim
import httpx2
import pytest
from godseye_uav import launch, theaters
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint, canonical_altitude
from godseye_uav.missions import plan_mission
from godseye_uav.safety import haversine_m
from godseye_uav.server import GodseyeUavServer, UavBackend
from godseye_uav.store import Store
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

REPO = Path(__file__).resolve().parents[1]
DEMO_PY = REPO / "scripts" / "demo_mission.py"
DEMO_SH = REPO / "scripts" / "demo_laptop.sh"
START_SH = REPO / "start.sh"

# Ports assigned to this agent (47500-47599). Taken by binding for real and
# stepping to the next on failure, so a listener left over from an earlier run
# (or taken between a probe and the bind) is walked past instead of crashing.
# The start offset is rotated by pid so two concurrent runs do not both begin
# at the bottom of the range and collide on every attempt.
def _rotate(ports: list[int]) -> list[int]:
    n = os.getpid() % len(ports)
    return ports[n:] + ports[:n]


_SIM_RANGE = _rotate(list(range(47550, 47570)))
_MCP_RANGE = _rotate(list(range(47570, 47600)))

# Unique per process. Several agents run this suite against the same machine,
# so a sibling pytest can already be serving THIS module's fixture on a port in
# the range. With a shared token its server answers our readiness probe 200, we
# adopt its port, and the test then dies with ConnectError when that run ends.
# A per-process token makes a foreign server answer 401, so the probe rejects
# it and we move to the next port.
TOKEN = f"test-token-launch-{os.getpid()}"


# ---------------------------------------------------------------------------
# (a) theaters.py is the single source of truth for the launcher
# ---------------------------------------------------------------------------


def test_launcher_keeps_no_theater_table_of_its_own():
    """The third copy is gone: ids come from `theaters.ids()`."""
    assert not hasattr(launch, "THEATERS"), (
        "launch.py still defines its own THEATERS dict — that is the drift "
        "theaters.py exists to retire")
    action = {a.dest: a for a in launch.build_parser()._actions}["theater"]
    assert list(action.choices) == theaters.ids()
    assert action.default == theaters.DEFAULT_THEATER_ID


def test_launcher_resolves_the_canonical_isfahan_not_natanz():
    """The old launcher's 'iran-isfahan' home was Natanz, 118.6 km away."""
    t = launch.resolve_theater("iran-isfahan")
    assert t is theaters.get("iran-isfahan")
    assert haversine_m(t.home_lat, t.home_lon, 32.6546, 51.6680) < 5_000
    # ... and the place the old table pointed at is now its own theater.
    natanz = launch.resolve_theater("iran-natanz")
    apart = haversine_m(t.home_lat, t.home_lon, natanz.home_lat, natanz.home_lon)
    assert apart > 100_000


def test_launcher_rejects_an_unknown_theater_loudly():
    with pytest.raises(KeyError) as exc:
        launch.resolve_theater("atlantis")
    assert "atlantis" in str(exc.value)
    assert "iran-isfahan" in str(exc.value)  # names what IS available


def test_launcher_covers_every_theater_in_the_table():
    for tid in theaters.ids():
        assert launch.resolve_theater(tid).id == tid


# ---------------------------------------------------------------------------
# (b) T1: MSL -> HAE converted exactly once, at ingest
# ---------------------------------------------------------------------------


def test_home_geopoint_altitude_is_hae_not_the_stored_msl():
    """`GeoPoint.altitude` is HAE; the theater stores MSL. Convert at ingest."""
    for tid in theaters.ids():
        t = theaters.get(tid)
        home = launch.home_geopoint(t)
        assert isinstance(home, GeoPoint)
        assert home.latitude == t.home_lat and home.longitude == t.home_lon
        expected = canonical_altitude(t.home_alt_msl_m, t.home_lat, t.home_lon,
                                      datum="msl").alt_hae
        assert home.altitude == pytest.approx(expected, abs=1e-9)


def test_home_altitude_fix_matches_the_measured_undulations():
    """Regression values from the shipped EGM96 grid — a silent datum change
    (or a degraded source) moves these."""
    measured = {
        "default": -22.208,
        "indo-pak-loc": -33.031,
        "taiwan-strait": 14.562,
        "ukraine-donbas": 14.488,
        "red-sea-hormuz": -30.312,
        "iran-natanz": 1.524,
    }
    for tid, n in measured.items():
        t = theaters.get(tid)
        fix = launch.home_altitude_fix(t)
        assert fix.undulation_m == pytest.approx(n, abs=0.05), tid
        assert fix.alt_hae == pytest.approx(fix.alt_msl + fix.undulation_m, abs=1e-6)
        assert fix.alt_msl == t.home_alt_msl_m
        assert not fix.degraded          # never fly on the coarse fallback (T1)
        assert "egm96" in fix.source     # a real geoid source answered


def test_home_geopoint_differs_from_msl_where_the_geoid_does():
    """The pre-fix code passed MSL through as if it were HAE. At Redmond that
    is a 22.2 m error — bigger than the PLAN T7 +/-10 m vertical gate."""
    t = theaters.get("default")
    assert abs(launch.home_geopoint(t).altitude - t.home_alt_msl_m) > 10.0


def test_envelope_home_stays_msl_and_geofence_is_the_ao():
    """`SafetyEnvelope.home` is documented `lat, lon, alt_msl` — no conversion
    there, or the fuel model's return leg starts from the wrong ground."""
    for tid in theaters.ids():
        t = theaters.get(tid)
        env = launch.build_envelope(t)
        assert env.home == t.home
        assert env.home[2] == t.home_alt_msl_m
        assert env.geofence == t.ao_list()


# ---------------------------------------------------------------------------
# the verified consequence: the demo mission is no longer geofence-rejected
# ---------------------------------------------------------------------------


def test_every_theater_demo_mission_passes_the_launchers_geofence():
    """The register finding was that the out-of-the-box demo mission is
    geofence-rejected, so the demo cannot run. Measured against the OLD
    launcher table that is true of iran-isfahan (start.sh's old default) and
    taiwan-strait, at all 14 waypoints. Check the launcher's OWN envelope
    against the demo's OWN plan, for every theater, so no future edit to
    either side can drift them apart again."""
    for tid in theaters.ids():
        t = theaters.get(tid)
        env = launch.build_envelope(t)
        demo = t.demo_mission()
        plan = plan_mission("grid_search", "Drone1", polygon=demo["polygon"],
                            alt_m=demo["alt_m"], speed_mps=demo["speed_mps"])
        assert env.check_route(plan.to_route()) == [], tid
        assert env.check_speed(demo["speed_mps"]) == [], tid


# ---------------------------------------------------------------------------
# (c) the demo drives the REAL MCP transport
# ---------------------------------------------------------------------------


def _load_demo():
    spec = importlib.util.spec_from_file_location("godseye_demo_mission", DEMO_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo_mission = _load_demo()


def test_demo_does_not_touch_private_server_internals():
    """R7: the old demo called `srv.mcp._tool_manager._tools[...].fn(...)`.

    Checked on the parsed AST, not the text, so the explanation of what was
    removed can still be written down in the module docstring.
    """
    tree = ast.parse(DEMO_PY.read_text(encoding="utf-8"))
    private = sorted({
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.startswith("_tool")
    })
    assert private == [], f"demo still reaches into server internals: {private}"
    src = DEMO_PY.read_text(encoding="utf-8")
    assert "streamable_http_client" in src
    assert "ClientSession" in src


def _start_sim(home) -> tuple[FakeAirSim, int]:
    """Start a FakeAirSim on the first port in this agent's range that binds.

    Probing with a throwaway socket first is not enough — that is a TOCTOU:
    the port can be taken between the probe and the real bind, and tornado
    then raises `OSError: [Errno 48] Address already in use` out of a fixture
    that looks unrelated. Bind for real and step to the next port on failure,
    the same way tests/test_fake_airsim.py does.
    """
    failures: list[str] = []
    for port in _SIM_RANGE:
        sim = FakeAirSim(home=home, port=port)
        try:
            sim.start()
        except OSError as exc:
            failures.append(f":{port} {exc}")
            continue
        return sim, port
    raise AssertionError(
        f"no bindable sim port in {min(_SIM_RANGE)}..{max(_SIM_RANGE)} -> {failures}")


def _mcp_answers(url: str, token: str) -> bool:
    """True once POST /mcp completes an MCP initialize with this token."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test_launch", "version": "1"}},
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    })
    try:
        with urllib.request.urlopen(req, timeout=2.0) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - not up yet
        return False


@pytest.fixture(scope="module")
def live_stack(tmp_path_factory):
    """A real fake-AirSim + MCP server on loopback, spoken to over HTTP."""
    t = theaters.get("default")
    home = launch.home_geopoint(t)

    sim, sim_port = _start_sim(home)
    client = airsim.MultirotorClient(port=sim_port)
    client.confirmConnection()
    backend = UavBackend(client, home, sim=sim)
    store = Store(tmp_path_factory.mktemp("store"))
    srv = GodseyeUavServer(backend, store, envelope=launch.build_envelope(t),
                           token=TOKEN, watchdog_s=30.0)

    # uvicorn reports a bind clash as SystemExit(3) inside its own thread, so
    # the only reliable readiness signal is a real MCP initialize answering
    # 200. Try successive ports until one does.
    url = loop = thread = None
    for mcp_port in _MCP_RANGE:
        loop = asyncio.new_event_loop()

        def run(loop=loop, mcp_port=mcp_port):
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(srv.serve(host="127.0.0.1", port=mcp_port))
            except (RuntimeError, asyncio.CancelledError, SystemExit):
                pass

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        candidate = f"http://127.0.0.1:{mcp_port}/mcp"
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if _mcp_answers(candidate, TOKEN):
                url = candidate
                break
            if not thread.is_alive():   # uvicorn gave up on this port
                break
            time.sleep(0.25)
        if url:
            break
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5.0)
    if url is None:  # pragma: no cover - no usable port is a hard error
        sim.stop()
        raise AssertionError(
            f"MCP server never answered on any port in "
            f"{min(_MCP_RANGE)}..{max(_MCP_RANGE)}")

    try:
        yield {"url": url, "token": TOKEN, "theater": t.id}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5.0)
        srv.tasking.shutdown()
        store.close()
        sim.stop()
        # Release the listening socket for the next run; a half-closed uvicorn
        # is exactly what made the fixed-port version of this fixture flaky.
        if not loop.is_running():
            loop.close()


def test_demo_flies_over_streamable_http_with_bearer_auth(live_stack):
    """End to end: tools/list + tools/call over POST /mcp, no internals."""
    lines: list[str] = []
    rc = asyncio.run(demo_mission.run_demo(
        live_stack["url"], live_stack["token"], live_stack["theater"],
        monitor_s=4.0, out=lines.append))
    text = "\n".join(lines)
    # 0 = clean run; 3 = every step ran but the server failed the flight task.
    assert rc in (0, 3), text
    assert "Streamable HTTP + Bearer auth" in text
    assert "server published" in text and "uav_get_telemetry" in text
    # T1: the spawn laydown always states the datum it sent. On the contract
    # tool (`alt_msl_m`) it is MSL straight through; on the legacy `alt_m`,
    # which is absolute geodetic, it shows the MSL -> HAE conversion and which
    # EGM96 source produced it. Either way the datum is never left implicit.
    assert "m MSL" in text, text
    if "m MSL -> " in text:
        assert "HAE" in text and "egm96" in text, text
    assert "=== takeoff ===" in text
    assert "ISR-only (M14)" in text


def test_demo_refuses_a_theater_the_server_is_not_flying(live_stack, capsys):
    """A theater mismatch must fail loudly, not as a mystery geofence reject.

    Driven through `main()` — the entry point demo_laptop.sh calls — so the
    exit code and the operator-facing message are both what ships.
    """
    rc = demo_mission.main([
        "--mcp-url", live_stack["url"], "--token", live_stack["token"],
        "--theater", "iran-isfahan", "--monitor-s", "1",
    ])
    assert rc == 2
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "OUTSIDE" in text
    assert "iran-isfahan" in text
    assert "=== takeoff ===" not in text  # nothing was commanded


def test_demo_needs_the_bearer_token(live_stack, capsys):
    """T4e: auth is really on the wire, not bypassed by an in-process shim."""
    rc = demo_mission.main([
        "--mcp-url", live_stack["url"], "--token", "not-the-token",
        "--theater", live_stack["theater"], "--monitor-s", "1",
    ])
    assert rc == 2
    captured = capsys.readouterr()
    assert "FAILED" in captured.err
    assert "=== takeoff ===" not in captured.out


# ---------------------------------------------------------------------------
# (d)/(e) the one-command demo script and start.sh
# ---------------------------------------------------------------------------


def test_demo_laptop_script_exists_and_is_runnable():
    """PLAN §8.1 promises ./scripts/demo_laptop.sh; it did not exist."""
    assert DEMO_SH.is_file(), "scripts/demo_laptop.sh is missing (PLAN §8.1)"
    assert DEMO_SH.stat().st_mode & 0o111, "demo_laptop.sh is not executable"
    subprocess.run(["bash", "-n", str(DEMO_SH)], check=True)


def test_demo_laptop_polls_real_health_endpoints_and_runs_the_mission():
    src = DEMO_SH.read_text(encoding="utf-8")
    assert "/health" in src                     # bridge readiness, not a sleep
    assert "/mcp" in src                        # MCP readiness
    assert "scripts/demo_mission.py" in src     # it actually flies the mission
    assert "open" in src and "xdg-open" in src  # opens the command center
    for var in ("THEATER", "SIM_BACKEND", "AIRSIM_PORT", "BRIDGE_PORT",
                "MCP_PORT", "UI_PORT", "TOKEN"):
        assert var in src, f"demo_laptop.sh ignores the {var} override"


def test_start_sh_takes_its_theater_from_the_table():
    """start.sh used to hardcode THEATER=iran-isfahan — a fourth copy of the
    theater knowledge, and one that named the wrong place."""
    src = START_SH.read_text(encoding="utf-8")
    assert 'THEATER:-iran-isfahan' not in src
    assert "theaters.DEFAULT_THEATER_ID" in src
    assert "godseye_uav import theaters" in src
    subprocess.run(["bash", "-n", str(START_SH)], check=True)


# ---------------------------------------------------------------------------
# start.sh self-checks the theater table before it binds anything
# ---------------------------------------------------------------------------


def _start_sh_theater_block() -> str:
    """The python start.sh runs to resolve and check its theater.

    Executed for real below rather than grepped: a test that only asserts
    `"validate" in src` passes on a script that imports the function and never
    calls it, which is exactly the bug class here.
    """
    src = START_SH.read_text(encoding="utf-8")
    match = re.search(r"<<'PYTHEATER'\n(.*?)\nPYTHEATER", src, re.DOTALL)
    assert match, "start.sh no longer has a PYTHEATER block"
    return match.group(1)


def _run_theater_block(theater_id: str) -> None:
    sys_argv = ["-", theater_id]
    old = sys.argv
    sys.argv = sys_argv
    try:
        # Running the SHIPPED block is the whole point — a grep for "validate"
        # would pass on a script that imports it and never calls it.
        exec(compile(_start_sh_theater_block(), "start.sh:PYTHEATER", "exec"),  # noqa: S102
             {"__name__": "__main__"})
    finally:
        sys.argv = old


def test_start_sh_refuses_to_boot_on_an_inconsistent_theater_table(monkeypatch,
                                                                   capsys):
    """Only demo_laptop.sh validated the table, so `./start.sh` on its own
    would happily boot on a table whose home sits outside its own AO or whose
    demo box the geofence rejects — and only fail mid-mission."""
    monkeypatch.setattr(theaters, "validate",
                        lambda *a, **k: ["default: home 1,2 outside its AO"])
    with pytest.raises(SystemExit) as exc:
        _run_theater_block(theaters.DEFAULT_THEATER_ID)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "INCONSISTENT" in err
    assert "outside its AO" in err       # names the actual problem, not "failed"


def test_start_sh_boots_on_the_real_table_and_prints_the_theater(capsys):
    """The guard must not be a brick wall: the shipped table is consistent."""
    assert theaters.validate() == []
    _run_theater_block("iran-isfahan")
    out = capsys.readouterr().out
    assert "iran-isfahan" in out
    assert "Isfahan" in out


# ---------------------------------------------------------------------------
# (f) the theater is threaded into the SERVER, not just into the geofence
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _wiring_backend(tmp_path_factory):
    """A backend + store the wiring tests can build servers on top of."""
    t = theaters.get("default")
    sim, _port = _start_sim(launch.home_geopoint(t))
    client = airsim.MultirotorClient(port=_port)
    client.confirmConnection()
    backend = UavBackend(client, launch.home_geopoint(t), sim=sim)
    store = Store(tmp_path_factory.mktemp("wiring-store"))
    try:
        yield backend, store
    finally:
        store.close()
        sim.stop()


def test_launcher_threads_the_theater_into_the_server(_wiring_backend):
    """The launcher passed `envelope=` and NOT `theater=`, so the server fell
    back to `theaters.get(None)` — Redmond — for everything that is not the
    geofence. Under `--theater iran-isfahan` that made `sim_spawn_target`
    default to Redmond's 122 m ground in an AO whose ground is 1570 m: every
    target spawned without an explicit altitude sat ~1448 m underground.
    """
    backend, store = _wiring_backend
    t = theaters.get("iran-isfahan")
    srv = launch.build_server(t, backend, store, token=TOKEN)
    try:
        assert srv.theater is t
        assert srv.theater_mismatch is None
        # the three things keyed off the theater rather than the envelope
        assert srv.theater.home_alt_msl_m == pytest.approx(1570.0)
        assert srv.envelope.geofence == t.ao_list()
        assert sorted(srv.pol.pois()) == sorted(p.name for p in t.pois)
    finally:
        srv.tasking.shutdown()


def test_envelope_only_wiring_is_the_defect_the_server_now_reports(_wiring_backend):
    """Pin the failure mode itself, so a future edit that drops `theater=`
    cannot pass unnoticed: built the old way, the server resolves REDMOND."""
    backend, store = _wiring_backend
    t = theaters.get("iran-isfahan")
    old_way = GodseyeUavServer(backend, store, envelope=launch.build_envelope(t),
                               token=TOKEN)
    try:
        assert old_way.theater.id == theaters.DEFAULT_THEATER_ID
        assert old_way.theater.home_alt_msl_m == pytest.approx(122.0)
        assert old_way.theater_mismatch is not None
        assert old_way.theater_mismatch["theater_id"] == "default"
        assert old_way.theater_mismatch["offset_m"] > 1000.0
    finally:
        old_way.tasking.shutdown()


# ---------------------------------------------------------------------------
# (g) the booted stack: `python -m godseye_uav.launch --theater ...` for real
# ---------------------------------------------------------------------------

_BOOT_BASE = _rotate(list(range(49200, 49290)))


def _boot_env() -> dict:
    """Child env with the in-repo airsim client and package on PYTHONPATH."""
    env = dict(os.environ)
    paths = [str(REPO / "mcp"), str(Path(airsim.__file__).resolve().parents[1])]
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(paths + ([existing] if existing else []))
    return env


@pytest.fixture(scope="module")
def booted_stack(tmp_path_factory):
    """The REAL launcher, in its own process, on a NON-default theater.

    Nothing here imports the server: the point is to exercise the shipped
    `python -m godseye_uav.launch` wiring end to end, the way `start.sh` runs
    it, and then ask the running server — over MCP — where it puts a target.
    """
    theater_id = "iran-isfahan"
    db = tmp_path_factory.mktemp("boot-store")
    proc = url = None
    failures: list[str] = []
    for i in range(0, len(_BOOT_BASE) - 2, 3):
        sim_port, mcp_port, bridge_port = _BOOT_BASE[i:i + 3]
        proc = subprocess.Popen(
            [sys.executable, "-m", "godseye_uav.launch",
             "--theater", theater_id, "--sim-port", str(sim_port),
             "--mcp-port", str(mcp_port), "--bridge-port", str(bridge_port),
             "--token", TOKEN, "--db", str(db)],
            cwd=str(REPO), env=_boot_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        candidate = f"http://127.0.0.1:{mcp_port}/mcp"
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if _mcp_answers(candidate, TOKEN):
                url = candidate
                break
            if proc.poll() is not None:      # died on a bound port; next triple
                failures.append(f":{sim_port}/{mcp_port} exited {proc.returncode}")
                break
            time.sleep(0.25)
        if url:
            break
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            proc.kill()
    if url is None:  # pragma: no cover - no usable port triple is a hard error
        raise AssertionError(
            f"`python -m godseye_uav.launch` never came up in "
            f"{min(_BOOT_BASE)}..{max(_BOOT_BASE)} -> {failures}")
    try:
        yield {"url": url, "token": TOKEN, "theater": theater_id}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15.0)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


async def _call(url: str, token: str, tool: str, args: dict) -> dict:
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                           timeout=60.0) as hc,
        streamable_http_client(url, http_client=hc) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(tool, args, read_timeout_seconds=60.0)
        # The MCP python SDK renamed its model fields camelCase -> snake_case;
        # read whichever this version has rather than pin one and break on the
        # other (`demo_mission.py` does the same).
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)
        assert not is_error, result
        payload = (getattr(result, "structured_content", None)
                   or getattr(result, "structuredContent", None))
        if not payload:
            text = next((b.text for b in (result.content or [])
                         if getattr(b, "text", None)), None)
            assert text, f"{tool} returned no readable content: {result}"
            payload = json.loads(text)
        return payload.get("result", payload) if set(payload) == {"result"} else payload


async def _resource(url: str, token: str, uri: str) -> dict:
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                           timeout=60.0) as hc,
        streamable_http_client(url, http_client=hc) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.read_resource(uri)
        return json.loads(result.contents[0].text)


def test_booted_stack_spawns_targets_on_the_theaters_ground_not_redmonds(
        booted_stack):
    """THE measured consequence of the missing `theater=`.

    A `sim_spawn_target` with no altitude used to come back at 122.0 m MSL —
    Redmond's ground — while flying an AO whose ground is 1570 m MSL, burying
    the target ~1448 m underground where no sensor can ever see it. Asked of a
    REAL booted stack over MCP, not of an in-process object.
    """
    t = theaters.get(booted_stack["theater"])
    lat, lon = t.center()
    out = asyncio.run(_call(booted_stack["url"], booted_stack["token"],
                            "sim_spawn_target",
                            {"lat": lat, "lon": lon, "ob_class": "mbt"}))
    assert "error" not in out, out
    assert out["alt_msl_m"] == pytest.approx(t.home_alt_msl_m, abs=0.01)
    assert out["alt_msl_m"] > 1000.0                 # Isfahan, not Redmond
    assert abs(out["alt_msl_m"] - 122.0) > 1000.0    # the exact old defect


def test_booted_stack_reports_no_theater_mismatch(booted_stack):
    """`uav://safety/geofence` carries the mismatch flag. The launcher must
    leave it clear: an audited 'theater_mismatch' on every boot would be the
    defect still present, merely announced."""
    t = theaters.get(booted_stack["theater"])
    out = asyncio.run(_resource(booted_stack["url"], booted_stack["token"],
                                "uav://safety/geofence"))
    assert out["theater"]["id"] == t.id
    assert out["theater"]["ground_elevation_msl_m"] == pytest.approx(1570.0)
    assert out["theater_mismatch"] is None
    assert [list(v) for v in out["theater"]["ao"]] == [list(v) for v
                                                       in t.ao_list()]
    # the geofence the server enforces is that same AO, not Redmond's
    assert [list(v) for v in out["geofence"]] == [list(v) for v in t.ao_list()]
