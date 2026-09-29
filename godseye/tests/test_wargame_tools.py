"""The simulated wargame tools, their registry and the end route (PLAN §4.5a
M14a; WG v2 §3.7, §3.8, §5.2.10; key unit tests of B4).

What is pinned:

* the catalog: ten `wg_*` tools with the §3.7 titles, classes and arguments;
  every mutating one takes `idempotency_key`; descriptions start
  "SIMULATION (M14a):", name no order-of-battle class and carry no
  real-system token; `wg_plan_corridor` without `target_track_id` is a
  validation error;
* `wargame_inactive` on every non-entry tool with no session, before the
  engine is reached; the entry tools answer with none;
* `simulated: true` (and the notional note) on every output, refusals too;
* the flag: default `/mcp` has no `wg_*`; `--wargame-mcp` publishes all ten
  there as well, sharing one set of budgets;
* the `CallBudget`s on `wg_plan_corridor` and `wg_propose_strike`;
* a scripted session through the tools: real data and other sim objects are
  refused, `/mcp` can never confirm, only the authorizing console session
  executes (once), and no output carries a real-system token;
* `POST /wargame/session/end`: 200, then 409.

Pure tests use a stub engine; the rest run a default-theater server on the
fake AirSim with the engine thread off. No network (geodata off); B4's ports
53400-53499.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import math
import os
import re
import time
import types
import typing
from contextlib import contextmanager
from pathlib import Path

import airsim
import pytest
from godseye_uav import theaters
from godseye_uav import wargame as wg
from godseye_uav import wargame_tables as T
from godseye_uav import wargame_tools as wt
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint, canonical_altitude
from godseye_uav.server import GodseyeUavServer, UavBackend
from godseye_uav.store import Store
from godseye_uav.targets import OB_LIBRARY
from mcp.server.mcpserver import MCPServer
from support.wg_tokens import assert_no_real_system_tokens

_PORTS = list(range(53400, 53500))
_PORT = itertools.cycle(_PORTS[os.getpid() % len(_PORTS):]
                        + _PORTS[:os.getpid() % len(_PORTS)])
THEATER = theaters.get("default")
HOME = GeoPoint(THEATER.home_lat, THEATER.home_lon,
                canonical_altitude(THEATER.home_alt_msl_m, THEATER.home_lat,
                                   THEATER.home_lon, datum="msl").alt_hae)
SEED = 4417
CONTRACT = Path(__file__).resolve().parents[1] / "TOOL_CONTRACT.md"

#: §3.7, verbatim.
TITLES = {
    "wg_session_start": "Start a simulated wargame", "wg_session_status": "Wargame status",
    "wg_session_end": "End the wargame", "wg_generate_scenario": "Generate a scenario",
    "wg_spawn_force": "Add simulated forces", "wg_list_forces": "List forces",
    "wg_list_classes": "List wargame classes", "wg_plan_corridor": "Plan a corridor",
    "wg_propose_strike": "Propose a simulated strike",
    "wg_execute_engagement": "Execute a simulated engagement",
}
#: §3.7 arguments per tool (mutating tools also take `idempotency_key`).
ARGS = {
    "wg_session_start": {"seed", "red_engages", "reveal_red", "idempotency_key"},
    "wg_session_status": {"events"},
    "wg_list_classes": set(),
    "wg_session_end": {"idempotency_key"},
    "wg_generate_scenario": {"template", "intensity", "ad_class", "idempotency_key"},
    "wg_spawn_force": {"side", "wg_class", "lat", "lon", "count", "objective_id",
                       "idempotency_key"},
    "wg_list_forces": {"side"},
    "wg_plan_corridor": {"vehicle", "target_track_id", "alt_agl_m", "relook",
                         "relook_radius_m"},
    "wg_propose_strike": {"shooter_id", "target_track_id"},
    "wg_execute_engagement": {"pending_id", "shooter_id", "target_track_id",
                              "idempotency_key"},
}
ENTRY = {"wg_session_start", "wg_session_status", "wg_list_classes"}
MUTATING = {"wg_session_start", "wg_session_end", "wg_generate_scenario", "wg_spawn_force",
            "wg_execute_engagement"}
#: Arguments that get each non-entry tool past validation, so the only
#: refusal left is the missing session.
VALID = {
    "wg_session_end": {},
    "wg_generate_scenario": {"template": "air_defence_belt"},
    "wg_spawn_force": {"side": "red", "wg_class": "ad_gun", "lat": 47.65, "lon": -122.13},
    "wg_list_forces": {},
    "wg_plan_corridor": {"vehicle": "Drone1", "target_track_id": "T-0001"},
    "wg_propose_strike": {"shooter_id": "blue-artillery-1", "target_track_id": "T-0001"},
    "wg_execute_engagement": {"pending_id": "WG-000000-E1", "shooter_id": "blue-artillery-1",
                              "target_track_id": "T-0001"},
}


def run(coro):
    return asyncio.run(coro)


def ne(dn_m: float, de_m: float) -> tuple[float, float]:
    """A point `dn_m` north and `de_m` east of home (flat earth, < 3 km)."""
    return (HOME.latitude + dn_m / 111_320.0,
            HOME.longitude + de_m / (111_320.0 * math.cos(math.radians(HOME.latitude))))


P_RED = ne(1600, 1000)          # >= 1 km from home, > 500 m from every theater point
P_BLUE = ne(-900, -900)


def fn(registry: MCPServer, name: str):
    return registry._tool_manager._tools[name].fn


def over_mcp(registry: MCPServer, name: str, args: dict) -> dict:
    """A call through the MCP layer (argument validation included)."""
    out = run(registry.call_tool(name, args))
    return json.loads(out.content[0].text)


def assert_stamped(out: dict) -> None:
    assert out.get("simulated") is True, out
    assert out.get("note") == T.NOTIONAL_NOTE, out


class StubEngine:
    """The engine surface the tools call, recording each call."""

    def __init__(self, *, active: bool = False):
        self.active = active
        self.calls: list[tuple[str, tuple, dict]] = []
        self.raise_on: dict[str, BaseException] = {}

    def _note(self, name: str, *a, **kw) -> dict:
        self.calls.append((name, a, kw))
        if name in self.raise_on:
            raise self.raise_on[name]
        return {"called": name}

    async def start(self, **kw):
        return self._note("start", **kw)

    async def end(self, **kw):
        return {**self._note("end", **kw), "ended": True, "aar_id": "aar-WG-1",
                "session_id": "WG-1", "resource": "uav://reports/aar-WG-1", "revived": []}

    async def generate(self, *a):
        return self._note("generate", *a)

    async def spawn(self, *a, **kw):
        return self._note("spawn", *a, **kw)

    async def plan_corridor(self, *a, **kw):
        return self._note("plan_corridor", *a, **kw)

    async def propose_strike(self, *a):
        return self._note("propose_strike", *a)

    async def execute(self, *a):
        # What the real engine checks: the console context of THIS call.
        return {**self._note("execute", *a, console=wg.CONSOLE_CALL.get()), "executed": True}

    def status(self, **kw):
        return self._note("status", **kw)

    def list_forces(self, **kw):
        return self._note("list_forces", **kw)


class StubServer:
    """What the tools read from `srv`: the engine, the theater, idempotency."""

    def __init__(self, engine: StubEngine):
        self.wargame = engine
        self.theater = THEATER
        self._idem: dict[str, dict] = {}

    def _idem_replay(self, tool, key):
        prior = self._idem.get(f"{tool}:{key}") if key else None
        return None if prior is None else {**prior, "status": "duplicate",
                                           "idempotent_replay": True}

    def _idem_record(self, tool, key, result):
        if key and not result.get("error"):
            self._idem[f"{tool}:{key}"] = dict(result)
            result = {**result, "idempotency_key": key}
        return result


def stub(*, active: bool = False) -> tuple[StubServer, MCPServer]:
    srv = StubServer(StubEngine(active=active))
    registry = MCPServer("godseye-wargame")
    wt.register(registry, srv)
    return srv, registry


# ------------------------------------------------------------ catalog -----

def test_the_catalog_matches_the_contract():
    assert set(wt.TOOL_NAMES) == set(TITLES) and len(wt.TOOL_NAMES) == 10
    assert dict(wt.TITLES) == TITLES
    assert set(wt.DESCRIPTIONS) == set(wt.TOOL_CLASSES) == set(TITLES)
    assert wt.ENTRY_TOOLS == ENTRY
    assert wt.MUTATING == MUTATING
    assert {k for k, v in wt.TOOL_CLASSES.items() if v == "engagement"} == {
        "wg_execute_engagement"}
    assert {k for k, v in wt.TOOL_CLASSES.items() if v == "plan"} == {
        "wg_plan_corridor", "wg_propose_strike"}
    assert set(wt.TOOL_CLASSES.values()) <= {"sim", "read", "plan", "engagement"}
    # The schema enums are the engine's own vocabularies.
    assert set(typing.get_args(wt.Template)) == set(T.TEMPLATES)
    assert typing.get_args(wt.Intensity) == T.INTENSITIES
    assert typing.get_args(wt.Side) == T.SIDES
    assert dict(wt.BUDGETS) == {"wg_plan_corridor": (20, 600.0),
                                "wg_propose_strike": (20, 600.0)}


def test_descriptions_are_simulation_only_and_name_no_real_system():
    for name, text in wt.DESCRIPTIONS.items():
        assert text.startswith("SIMULATION (M14a):"), name
        for key, ob in OB_LIBRARY.items():
            assert not re.search(rf"\b{re.escape(key)}\b", text), (name, key)
            assert ob.name.lower() not in text.lower(), (name, ob.name)
    assert_no_real_system_tokens(dict(wt.DESCRIPTIONS))
    assert_no_real_system_tokens(dict(wt.TITLES))


def test_register_publishes_every_tool_with_its_arguments_and_shares_the_budgets():
    srv = StubServer(StubEngine())
    one, two = MCPServer("a"), MCPServer("b")
    wt.register(one, srv)
    budgets = srv.wargame_budgets
    wt.register(two, srv)                  # --wargame-mcp: once more, for /mcp
    assert srv.wargame_budgets is budgets
    for registry in (one, two):
        tools = {t.name: t for t in run(registry.list_tools())}
        assert set(tools) == set(TITLES)
        for name, t in tools.items():
            props = set(t.input_schema.get("properties", {}))
            assert props == ARGS[name], name
            assert ("idempotency_key" in props) is (name in MUTATING), name
            assert t.title == TITLES[name] and t.description == wt.DESCRIPTIONS[name]
        assert tools["wg_plan_corridor"].input_schema["required"] == [
            "vehicle", "target_track_id"]
        assert tools["wg_generate_scenario"].input_schema["properties"]["template"][
            "enum"] == list(T.TEMPLATES)


def test_a_corridor_without_a_target_track_is_a_validation_error():
    _, registry = stub(active=True)
    with pytest.raises(Exception, match="target_track_id"):
        run(registry.call_tool("wg_plan_corridor", {"vehicle": "Drone1"}))
    with pytest.raises(Exception, match="side"):
        run(registry.call_tool("wg_spawn_force", {"side": "green", "wg_class": "ad_gun",
                                                  "lat": 1.0, "lon": 2.0}))


# ---------------------------------------------------- session scoping -----

def test_every_non_entry_tool_refuses_without_a_session_before_the_engine():
    srv, registry = stub(active=False)
    for name in set(TITLES) - ENTRY:
        for out in (run(fn(registry, name)(**VALID[name])),
                    over_mcp(registry, name, VALID[name])):
            assert out == {"rejected": True, "error": "wargame_inactive",
                           "message": wg.INACTIVE_MESSAGE, "simulated": True,
                           "note": T.NOTIONAL_NOTE}, (name, out)
    assert srv.wargame.calls == []
    # The entry tools answer without a session.
    assert run(fn(registry, "wg_session_status")())["called"] == "status"
    assert run(fn(registry, "wg_session_start")())["called"] == "start"
    classes = run(fn(registry, "wg_list_classes")())
    assert_stamped(classes)
    assert len(classes["classes"]) == len(T.CLASSES) == 16
    assert classes["theater_id"] == "default"
    assert all({"fits_ao", "covers_ao", "caveats"} <= set(r) for r in classes["classes"])
    assert classes["classes_that_fit"] == T.classes_that_fit(classes["ao"])


def test_every_answer_is_stamped_and_the_views_are_blue():
    srv, registry = stub(active=True)
    for name in TITLES:
        out = run(fn(registry, name)(**VALID.get(name, {})))
        assert_stamped(out)
    kw = {c[0]: c[2] for c in srv.wargame.calls}
    assert kw["status"] == {"truth": False, "events": 20}
    assert kw["list_forces"] == {"side": None, "truth": False}
    # Engine errors become stamped refusals; nothing escapes as a crash.
    srv.wargame.raise_on["plan_corridor"] = RuntimeError("boom")
    srv.wargame.raise_on["propose_strike"] = wg.WargameRefused("out_of_range")
    out = run(fn(registry, "wg_plan_corridor")(**VALID["wg_plan_corridor"]))
    assert out["rejected"] is True and out["error"] == "wargame_failed"
    assert "boom" not in out["message"] and "RuntimeError" in out["message"]
    assert_stamped(out)
    out = run(fn(registry, "wg_propose_strike")(**VALID["wg_propose_strike"]))
    assert out["error"] == "out_of_range" and out["message"] == wg.ENGINE_MESSAGES[
        "out_of_range"]
    assert_stamped(out)


def test_bad_arguments_are_refused_before_the_engine():
    srv, registry = stub(active=True)
    bad = [("wg_session_status", {"events": 101}), ("wg_session_status", {"events": -1}),
           ("wg_session_status", {"events": True}),
           ("wg_session_start", {"red_engages": "yes"}),
           ("wg_spawn_force", {**VALID["wg_spawn_force"], "count": 7}),
           ("wg_spawn_force", {**VALID["wg_spawn_force"], "count": 1.5}),
           ("wg_spawn_force", {**VALID["wg_spawn_force"], "side": "green"}),
           ("wg_list_forces", {"side": "green"}),
           ("wg_plan_corridor", {"vehicle": "Drone1", "target_track_id": " "}),
           ("wg_plan_corridor", {**VALID["wg_plan_corridor"], "relook": "no"}),
           ("wg_propose_strike", {"shooter_id": "", "target_track_id": "T-1"})]
    for name, args in bad:
        out = run(fn(registry, name)(**args))
        assert out["error"] == "invalid_parameter", (name, args, out)
        assert_stamped(out)
    assert srv.wargame.calls == []


def test_the_end_reason_follows_the_caller():
    srv, registry = stub(active=True)
    run(fn(registry, "wg_session_end")())
    with wg.console_call("chat-1"):
        run(fn(registry, "wg_session_end")())
    run(wt.end_session_for_route(srv))
    assert [c[2]["reason"] for c in srv.wargame.calls] == ["mcp", "analyst", "operator"]


def test_mutating_tools_replay_their_idempotency_key():
    srv, registry = stub(active=True)
    for name in ("wg_session_start", "wg_session_end", "wg_generate_scenario",
                 "wg_spawn_force"):
        first = run(fn(registry, name)(**VALID.get(name, {}), idempotency_key=f"k-{name}"))
        again = run(fn(registry, name)(**VALID.get(name, {}), idempotency_key=f"k-{name}"))
        assert again["idempotent_replay"] is True and again["called"] == first["called"]
        assert_stamped(again)
    assert [c[0] for c in srv.wargame.calls] == ["start", "end", "generate", "spawn"]
    # A refusal is never recorded: the next call with the key runs.
    srv.wargame.active = False
    assert run(fn(registry, "wg_spawn_force")(
        **VALID["wg_spawn_force"], idempotency_key="r"))["error"] == "wargame_inactive"
    srv.wargame.active = True
    assert run(fn(registry, "wg_spawn_force")(
        **VALID["wg_spawn_force"], idempotency_key="r"))["called"] == "spawn"


def test_execute_keys_are_scoped_to_the_console_session():
    srv, registry = stub(active=True)
    ex = fn(registry, "wg_execute_engagement")
    args = {**VALID["wg_execute_engagement"], "idempotency_key": "x"}
    run(ex(**args))                                   # no console: nothing recorded
    with wg.console_call("chat-1"):
        run(ex(**args))
        replay = run(ex(**args))
    assert replay["idempotent_replay"] is True
    with wg.console_call("chat-2"):
        other = run(ex(**args))                       # another session: not a replay
    assert "idempotent_replay" not in other
    assert "idempotent_replay" not in run(ex(**args))
    assert [c[2]["console"] for c in srv.wargame.calls] == [None, "chat-1", "chat-2", None]

    # The toolbelt's path (B8): `call_tool` inside `console_call` carries the
    # context to the engine; the same call outside it carries none.
    async def via_mcp(console: str | None) -> None:
        if console is None:
            await registry.call_tool("wg_execute_engagement", VALID["wg_execute_engagement"])
            return
        with wg.console_call(console):
            await registry.call_tool("wg_execute_engagement", VALID["wg_execute_engagement"])

    run(via_mcp("chat-9"))
    run(via_mcp(None))
    assert [c[2]["console"] for c in srv.wargame.calls[-2:]] == ["chat-9", None]


def test_the_corridor_and_strike_budgets():
    srv, registry = stub(active=True)
    for name in ("wg_plan_corridor", "wg_propose_strike"):
        for _ in range(20):
            assert run(fn(registry, name)(**VALID[name]))["called"]
        out = run(fn(registry, name)(**VALID[name]))
        assert out["rejected"] is True and out["error"] == "rate_limited", out
        assert out["budget"] == {"calls": 20, "window_s": 600.0}
        assert out["message"].startswith("Too many ") and "10 minutes" in out["message"]
        assert_stamped(out)
    assert len(srv.wargame.calls) == 40
    # One budget per server, shared by both registries (--wargame-mcp).
    other = MCPServer("godseye-uav")
    wt.register(other, srv)
    assert run(fn(other, "wg_propose_strike")(**VALID["wg_propose_strike"]))[
        "error"] == "rate_limited"
    # A call refused before the engine (no session) spends nothing.
    fresh, reg = stub(active=False)
    for _ in range(25):
        run(fn(reg, "wg_plan_corridor")(**VALID["wg_plan_corridor"]))
    assert fresh.wargame_budgets["wg_plan_corridor"].remaining() == 20


# ------------------------------------------------------- real server -----

@contextmanager
def wg_server(tmp_path, **kw):
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
    assert sim is not None, "no free port in 53400-53499"
    store = Store(tmp_path)
    try:
        client = airsim.MultirotorClient(port=sim.port)
        client.confirmConnection()
        srv = GodseyeUavServer(UavBackend(client, HOME, sim=sim), store, theater=THEATER, **kw)
        srv.wargame.run_thread = False
        yield srv
    finally:
        if srv is not None:
            srv.wargame.close()
            srv.stop_monitor()
            srv.tasking.shutdown()
        store.close()
        sim.stop()


def sense(srv, name: str, *, times: int = 3):
    """Fold `times` detections of sim object `name` into a track, as
    `_ingest_frame` does: the scenario hook first, the pattern of life else."""
    lat, lon, alt = srv.backend.sim.object_geo(name)
    track = None
    for _ in range(times):
        det = {"name": name, "geo_point": {"latitude": lat, "longitude": lon, "altitude": alt},
               "slant_range_m": 400.0, "pixels_on_target": 60}
        for t in srv.tracks.ingest(
                [det], sensor={"sensor": "scene", "fov_deg": 60},
                observer={"lat": lat, "lon": lon + 0.003, "alt_m": alt + 150.0,
                          "vehicle": "Drone1"}):
            if not srv._note_scenario_track(t):
                srv.pol.observe_track(t)
            track = t
        time.sleep(0.002)
    return track


class _Site:
    """A mapped site as `sim_spawn_order_of_battle` spawns it (fake real data)."""

    def __init__(self, osm_id, name, lat, lon):
        self.osm_id, self.name, self.lat, self.lon = osm_id, name, lat, lon
        self.category, self.ob_class = "military_base", "sam_short_range"
        self.ob_source, self.position_source = "test", "test"

    def spawn_request(self):
        return {"ob_class": self.ob_class, "lat": self.lat, "lon": self.lon, "name": self.name}

    def alt_provenance(self):
        return {"alt_msl_m": None, "alt_source": "test", "alt_is_real": False}


class _Order:
    real = False

    def __init__(self, sites_):
        self.sites = sites_
        self.provenance = types.SimpleNamespace(as_dict=lambda: {"source": "test"})

    def by_category(self):
        return {"military_base": len(self.sites)}


def test_the_default_mcp_has_no_wargame_tool_and_the_flag_publishes_all(tmp_path):
    with wg_server(tmp_path / "isr") as srv:
        assert not any(n.startswith("wg_") for n in srv.mcp._tool_manager._tools)
        assert set(srv.wargame_mcp._tool_manager._tools) == set(TITLES)
    with wg_server(tmp_path / "flag", wargame_mcp=True) as srv:
        published = srv.mcp._tool_manager._tools
        assert {n for n in published if n.startswith("wg_")} == set(TITLES)
        assert set(srv.wargame_mcp._tool_manager._tools) == set(TITLES)
        # The /mcp copies are the same tools: scoped, stamped, no session yet.
        for name in set(TITLES) - ENTRY:
            out = over_mcp(srv.mcp, name, VALID[name])
            assert out["error"] == "wargame_inactive", (name, out)
            assert_stamped(out)
        status = over_mcp(srv.mcp, "wg_session_status", {})
        assert status["active"] is False and status["last"] is None
        assert_stamped(status)
        classes = over_mcp(srv.mcp, "wg_list_classes", {})
        assert len(classes["classes"]) == 16 and classes["theater_id"] == "default"
        assert srv.wargame.active is False


def test_tool_contract_documents_every_wargame_tool_and_its_arguments():
    doc = CONTRACT.read_text()
    rows = {line.split("|")[1].strip().strip("`"): line
            for line in doc.splitlines() if line.startswith("| `wg_")}
    for name, args in ARGS.items():
        assert name in rows, f"{name} is not in TOOL_CONTRACT.md"
        missing = [a for a in args if a not in rows[name]]
        assert not missing, f"{name}: undocumented arguments {missing}"
        assert wt.TOOL_CLASSES[name] in rows[name], name
    for code in ("wargame_inactive", "engagement_requires_console_approval",
                 "not_a_scenario_unit", "rate_limited", "duplicate_name", "scenario_name",
                 "near_scenario_unit", "wargame_active", "POST /wargame/session/end"):
        assert code in doc, code
    assert "51 tools" in doc                        # the default catalog is unchanged


def test_a_scripted_session_through_the_tools(tmp_path):
    """Start, spawn, sense, plan, propose, confirm (console only), end. Real
    data and other sim objects are refused; `/mcp` never confirms; every output
    is stamped and free of real-system tokens."""
    outputs: list[dict] = []

    def keep(out: dict) -> dict:
        assert_stamped(out)
        outputs.append(out)
        return out

    with wg_server(tmp_path, wargame_mcp=True) as srv:
        reg = srv.wargame_mcp

        def call(name: str, **kw) -> dict:
            return keep(run(fn(reg, name)(**kw)))

        started = call("wg_session_start", seed=SEED, red_engages=False, idempotency_key="s1")
        assert {"session_id", "started_at_ms", "seed", "engine", "table_version",
                "red_engages", "reveal_red", "theater_id", "ao", "classes_that_fit",
                "caveats"} <= set(started)
        assert started["seed"] == SEED and started["theater_id"] == "default"
        again = call("wg_session_start", seed=SEED, red_engages=False, idempotency_key="s1")
        assert again["session_id"] == started["session_id"] and again["idempotent_replay"]
        assert call("wg_session_start")["error"] == "wargame_active"
        call("wg_list_classes")
        spawn_red = {"side": "red", "wg_class": "ad_gun", "lat": P_RED[0], "lon": P_RED[1]}
        red = call("wg_spawn_force", **spawn_red, idempotency_key="r1")["units"][0]
        blue = call("wg_spawn_force", side="blue", wg_class="blue_rocket", lat=P_BLUE[0],
                    lon=P_BLUE[1])["units"][0]
        assert (red["unit_id"], red["designator"]) == ("red-aaa-1", "Red AD guns 1")
        assert (blue["unit_id"], blue["designator"]) == ("blue-rockets-1", "Blue rockets 1")
        assert red["provenance"] == "scenario"
        replay = call("wg_spawn_force", **spawn_red, idempotency_key="r1")
        assert replay["idempotent_replay"] and replay["units"][0]["unit_id"] == "red-aaa-1"
        assert len(srv.wargame._session.units) == 2
        # Fog of war: the analyst's view lists blue only.
        assert [f["side"] for f in call("wg_list_forces")["forces"]] == ["blue"]
        assert call("wg_list_forces", side="red")["forces"] == []
        # A mapped site and a `sim_spawn_target` object are context, never targets.
        osm_at, tgt_at = ne(1500, -1500), ne(-1500, 1500)
        srv.real_order_of_battle = lambda: _Order([_Site(9, "sam_short_range_9001", *osm_at)])
        assert run(fn(srv.mcp, "sim_spawn_order_of_battle")())["spawned"] == 1
        assert run(fn(srv.mcp, "sim_spawn_target")(
            lat=tgt_at[0], lon=tgt_at[1], ob_class="sam_short_range",
            name="sam_short_range_1"))["status"] == "accepted"
        for name in ("sam_short_range_9001", "sam_short_range_1"):
            other = sense(srv, name)
            assert other.ob_class == "sam_short_range" and other.scenario is False
            for out in (call("wg_propose_strike", shooter_id=blue["unit_id"],
                             target_track_id=other.track_id),
                        call("wg_plan_corridor", vehicle="Drone1",
                             target_track_id=other.track_id)):
                assert out["error"] == "not_a_scenario_unit", out
                assert out["message"] == wg.NOT_SCENARIO_MESSAGE
        # The scenario unit's own track passes.
        track = sense(srv, srv.wargame._session.units[red["unit_id"]].object_name)
        corridor = call("wg_plan_corridor", vehicle="Drone1", target_track_id=track.track_id,
                        relook=True)
        assert corridor["waypoints"] and corridor["recon_args"]["dry_run"] is True
        proposal = call("wg_propose_strike", shooter_id=blue["id"],
                        target_track_id=f"trk:{track.track_id}")
        args = proposal["execute_args"]
        assert args == {"pending_id": proposal["pending_id"], "shooter_id": blue["unit_id"],
                        "target_track_id": track.track_id}
        # Nothing confirms without the console: not /mcp, not the bare tool,
        # not /mcp after the console authorized it, not another chat session.
        assert keep(over_mcp(srv.mcp, "wg_execute_engagement", args))["error"] == \
            "engagement_requires_console_approval"
        assert call("wg_execute_engagement", **args)["error"] == \
            "engagement_requires_console_approval"
        srv.wargame.authorize(args["pending_id"], "appr-1", chat_session="chat-1", args=args)
        assert keep(over_mcp(srv.mcp, "wg_execute_engagement", args))["error"] == \
            "engagement_requires_console_approval"
        with wg.console_call("chat-2"):
            assert call("wg_execute_engagement", **args)["error"] == \
                "engagement_requires_console_approval"
        with wg.console_call("chat-1"):
            fired = call("wg_execute_engagement", **args, idempotency_key="x1")
            replay = call("wg_execute_engagement", **args, idempotency_key="x1")
            once_more = call("wg_execute_engagement", **args)
        assert fired["executed"] is True and fired["outcome"] is None       # blue view
        assert fired["outcome_note"] == wg.OUTCOME_NOTE
        assert replay["idempotent_replay"] and replay["engagement_id"] == fired["engagement_id"]
        assert once_more["engagement_id"] == fired["engagement_id"]
        assert sum(1 for e in srv.wargame._session.engagements if e.fired_at_ms) == 1
        call("wg_session_status", events=100)
        ended = call("wg_session_end", idempotency_key="e1")
        assert ended["ended"] is True and ended["resource"] == f"uav://reports/{ended['aar_id']}"
        assert call("wg_session_end", idempotency_key="e1")["idempotent_replay"]
        assert srv.wargame.active is False
        for name in set(TITLES) - ENTRY:
            assert call(name, **VALID[name])["error"] == "wargame_inactive", name
        aar = srv.reports[ended["aar_id"]]
        assert aar["reason"] == "mcp"
    assert_no_real_system_tokens(outputs)
    assert_no_real_system_tokens(aar)


def test_the_end_route_answers_200_then_409(tmp_path):
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient

    def auth(authorization: str | None = Header(default=None)) -> bool:
        if authorization != "Bearer t0ken":
            raise HTTPException(status_code=401)
        return True

    with wg_server(tmp_path) as srv:
        app = FastAPI()
        app.include_router(wt.wargame_router(srv, auth))
        bearer = {"Authorization": "Bearer t0ken"}
        with TestClient(app) as tc:
            assert tc.post("/wargame/session/end").status_code == 401
            r = tc.post("/wargame/session/end", headers=bearer)
            assert r.status_code == 409
            assert r.json() == {"error": "wargame_inactive", "message": wg.INACTIVE_MESSAGE,
                                "simulated": True}
            started = run(fn(srv.wargame_mcp, "wg_session_start")(seed=SEED, red_engages=False))
            r = tc.post("/wargame/session/end", headers=bearer,
                        json={"reason": "<img src=x onerror=alert(1)>"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body == {"ok": True, "aar_id": f"aar-{started['session_id']}",
                            "session_id": started["session_id"],
                            "resource": f"uav://reports/aar-{started['session_id']}",
                            "revived": [], "simulated": True}
            # The AAR records the route, never caller text.
            assert srv.reports[body["aar_id"]]["reason"] == "operator"
            assert srv.wargame.active is False
            r = tc.post("/wargame/session/end", headers=bearer)
            assert r.status_code == 409 and r.json()["error"] == "wargame_inactive"
