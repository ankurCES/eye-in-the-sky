"""analyst_toolbelt: the in-process `godseye` SDK server the analyst talks to (§5.1).

Proxied tools are generated from a REAL GodseyeUavServer (FakeAirSim backend on
ports 52100-52199, temp store) and called in-process.  The Agent SDK is
replaced by a minimal fake so this file runs without claude-agent-sdk; one test
uses the real SDK (skipped when it is not installed) end to end over an
in-memory MCP client.
"""
import asyncio
import contextlib
import itertools
import json
import os
import threading
from contextlib import contextmanager

import airsim
import pytest
from godseye_uav import analyst_toolbelt as tb
from godseye_uav.analyst_policy import (
    COMMAND,
    CURATED_TOOLS,
    READ,
    SENSOR,
    SIM,
    STATIC_AUTO_TOOLS,
    TOOL_PREFIX,
    classify,
)
from godseye_uav.analyst_toolbelt import (
    EXCLUDED_TOOLS,
    RESULT_CHAR_LIMIT,
    build_toolbelt,
    dumps_compact,
    extract_entities,
    result_text,
    shrink,
)
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint
from godseye_uav.host import honour_msgpack_bind_host
from godseye_uav.safety import SafetyEnvelope
from godseye_uav.server import GodseyeUavServer, UavBackend
from godseye_uav.store import Store

HOME = GeoPoint(47.641468, -122.140165, 93.0)
AO = [(47.63, -122.16), (47.63, -122.12), (47.66, -122.12), (47.66, -122.16)]

_PORTS = list(range(52100, 52200))
_PORT = itertools.cycle(_PORTS[os.getpid() % len(_PORTS):] + _PORTS[:os.getpid() % len(_PORTS)])


def _nothing_listens(port: int) -> bool:
    """No loopback listener on ``port``. These ranges are shared with live
    checks, and a wildcard bind can SHADOW-share a port someone else holds on
    127.0.0.1 (macOS): the test's clients would then fly the OTHER sim."""
    import socket

    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0

CURATED = ["intel_overview", "intel_search", "intel_entity", "read_intel_resource",
           "ui_focus", "ui_track", "ui_show_orb", "ui_inspect"]


# ------------------------------------------------------------------ fakes --

class FakeTool:
    def __init__(self, name, description, input_schema, handler):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler


class FakeSdk:
    """The two SDK entry points the toolbelt uses."""

    @staticmethod
    def tool(name, description, input_schema, annotations=None):
        def deco(fn):
            return FakeTool(name, description, input_schema, fn)
        return deco

    @staticmethod
    def create_sdk_mcp_server(name, version="1.0.0", tools=None):
        return {"type": "sdk", "name": name, "version": version, "tools": list(tools or [])}


class StubIntel:
    def __init__(self, entity_size=100):
        self.calls = []
        self.entity_size = entity_size

    def overview(self):
        self.calls.append(("overview",))
        return {"theater": {"id": "default"}, "vehicles": [{"id": "veh:Drone1"}]}

    def search(self, query, types=None, limit=25):
        self.calls.append(("search", query, types, limit))
        return [{"id": f"trk:TRK-a-{i:04d}", "type": "track", "label": f"Contact {i}"}
                for i in range(40)]

    async def entity(self, entity_id):  # async on purpose: both shapes are accepted
        self.calls.append(("entity", entity_id))
        if entity_id == "veh:nope":
            return None
        return {"id": entity_id, "type": "vehicle",
                "fields": {"history": [{"i": i, "pad": "x" * 40}
                                       for i in range(self.entity_size)]}}


def _tools(belt):
    return {t.name: t for t in belt.tools}


def _payload(result):
    return json.loads(result_text(result))


def _start_sim() -> FakeAirSim:
    last = None
    honour_msgpack_bind_host()   # the sim binds 127.0.0.1, so a held port fails
    for _ in range(len(_PORTS)):
        port = next(_PORT)
        if not _nothing_listens(port):
            continue
        sim = FakeAirSim(home=HOME, port=port)
        try:
            sim.start()
            return sim
        except OSError as exc:
            last = exc
            with contextlib.suppress(Exception):
                sim.stop()
    raise RuntimeError(f"no free port in 52100-52199: {last}")


@contextmanager
def real_server(tmp_path):
    sim = _start_sim()
    store = Store(tmp_path)
    srv = None
    try:
        client = airsim.MultirotorClient(port=sim.port)
        client.confirmConnection()
        backend = UavBackend(client, HOME, sim=sim)
        envelope = SafetyEnvelope(geofence=AO,
                                  home=(HOME.latitude, HOME.longitude, HOME.altitude))
        srv = GodseyeUavServer(backend, store, envelope=envelope, watchdog_s=30.0)
        yield srv
    finally:
        if srv is not None:
            srv.stop_monitor()
            with contextlib.suppress(Exception):
                srv.tasking.shutdown()
        store.close()
        sim.stop()


@pytest.fixture
def server(tmp_path):
    with real_server(tmp_path) as srv:
        yield srv


@pytest.fixture
def listing_server(tmp_path):
    """A real server with no backend: enough to list tools and read static resources."""
    store = Store(tmp_path)
    try:
        yield GodseyeUavServer(None, store)
    finally:
        store.close()


def build(server, intel=None, emitted=None):
    sink = emitted if emitted is not None else []
    return asyncio.run(build_toolbelt(server, intel, sink.append, FakeSdk))


# --------------------------------------------------------------- catalog --

def test_every_server_tool_is_proxied_except_the_excluded(listing_server):
    belt = build(listing_server)
    server_names = {t.name for t in asyncio.run(listing_server.mcp.list_tools())}
    names = set(belt.tool_names)
    assert names == (server_names - set(EXCLUDED_TOOLS)) | set(CURATED)
    for gone in ("uav_list_tracks", "sim_set_environment"):
        assert gone in server_names and gone not in names
        assert gone in belt.excluded
        assert f"{TOOL_PREFIX}{gone}" in belt.disallowed_tools
    assert belt.server_config["name"] == "godseye"
    assert belt.server_config["type"] == "sdk"


def test_the_handoff_alias_is_excluded_and_its_superset_kept(listing_server):
    """uav_handoff_target is the GEV panel's alias of mission_handoff_track
    (one implementation). The analyst keeps the superset, which can dry-run."""
    belt = build(listing_server)
    infos = {t.name: t for t in asyncio.run(listing_server.mcp.list_tools())}
    alias = set(infos["uav_handoff_target"].input_schema["properties"])
    full = set(infos["mission_handoff_track"].input_schema["properties"])
    assert alias <= full and {"dry_run", "alt_agl_m"} <= full - alias
    assert "uav_handoff_target" not in belt.tool_names
    assert "mission_handoff_track" in belt.tool_names
    assert f"{TOOL_PREFIX}uav_handoff_target" in belt.disallowed_tools


def test_allowed_tools_are_prefixed_static_reads_only(listing_server):
    belt = build(listing_server)
    assert belt.allowed_tools, "read tools must be auto-approved"
    for full in belt.allowed_tools:
        assert full.startswith(TOOL_PREFIX)
        name = full[len(TOOL_PREFIX):]
        assert name in STATIC_AUTO_TOOLS
        assert classify(name, {}).klass == READ
    names = {n[len(TOOL_PREFIX):] for n in belt.allowed_tools}
    for name in belt.tool_names:
        klass = classify(name, {}).klass
        if klass in (COMMAND, SENSOR, SIM) or name.startswith("sim_"):
            assert name not in names, name
    assert "mission_dry_run" not in names
    assert set(CURATED) <= names


def test_curated_tools_are_all_read_class():
    for name in CURATED:
        assert classify(name, {}).klass == READ


def test_proxied_schemas_and_descriptions_come_from_the_server(listing_server):
    belt = _tools(build(listing_server))
    infos = {t.name: t for t in asyncio.run(listing_server.mcp.list_tools())}
    for name, info in infos.items():
        if name in EXCLUDED_TOOLS:
            continue
        tool = belt[name]
        assert tool.input_schema["properties"] == info.input_schema.get("properties", {})
        assert tool.input_schema["type"] == "object"
        assert tool.description.startswith(info.description.strip()[:40])
    assert "approve" in belt["mission_grid_search"].description
    assert "dry_run=true" in belt["mission_grid_search"].description
    assert "approve" not in belt["uav_get_telemetry"].description
    assert "detail defaults to 'summary'" in belt["uav_target_report"].description


def test_without_a_server_only_curated_tools_exist():
    belt = asyncio.run(build_toolbelt(None, None, lambda d: None, FakeSdk))
    assert belt.tool_names == CURATED


# -------------------------------------------------------------- proxying --

def test_proxied_call_runs_in_process_against_the_real_server(server):
    async def main():
        belt = _tools(await build_toolbelt(server, None, lambda d: None, FakeSdk))
        res = await belt["uav_get_telemetry"].handler({"vehicle": "Drone1"})
        assert not res.get("is_error")
        text = result_text(res)
        assert "\n" not in text  # compact JSON, not the server's indent=2
        body = json.loads(text)
        assert body["vehicle"] == "Drone1"
        assert "fuel_pct" in body and "bingo_fuel_pct" in body
        vehicles = _payload(await belt["uav_list_vehicles"].handler({}))
        assert "vehicles" in vehicles
    asyncio.run(main())


def test_heavy_report_tools_get_summary_defaults(listing_server):
    seen = []
    real = listing_server.mcp.call_tool

    async def spy(name, args, context=None):
        seen.append((name, dict(args)))
        return await real(name, args)

    listing_server.mcp.call_tool = spy
    belt = _tools(build(listing_server))

    async def main():
        await belt["uav_target_report"].handler({})
        await belt["uav_target_report"].handler({"detail": "full", "top_n": 3})
        await belt["mission_threat_assessment"].handler({"vehicle": "Drone1"})
        await belt["uav_list_ob_classes"].handler({})
    asyncio.run(main())
    assert seen[0] == ("uav_target_report", {"detail": "summary", "top_n": 10})
    assert seen[1] == ("uav_target_report", {"detail": "full", "top_n": 3})
    assert seen[2][1]["detail"] == "summary" and seen[2][1]["top_n"] == 10
    assert seen[3] == ("uav_list_ob_classes", {})  # tools without the knobs are untouched


def test_tool_failures_come_back_as_error_results_not_exceptions(listing_server):
    belt = _tools(build(listing_server))

    async def main():
        # argument validation failure inside the server (ToolError)
        bad = await belt["uav_goto_gps"].handler({"vehicle": "Drone1", "lat": "north"})
        assert bad["is_error"] is True
        err = _payload(bad)["error"]
        assert err["code"] == "tool_failed" and "uav_goto_gps" in err["message"]
        # a crash inside the tool (no backend -> UnexpectedToolError)
        crash = await belt["uav_get_telemetry"].handler({"vehicle": "Drone1"})
        assert crash["is_error"] is True
        assert "uav_get_telemetry" in _payload(crash)["error"]["message"]
    asyncio.run(main())


def test_structured_server_errors_pass_through_as_data(listing_server):
    belt = _tools(build(listing_server))
    res = asyncio.run(belt["uav_identify_target"].handler({"track_id": "TRK-nope-0001"}))
    assert not res.get("is_error")
    assert "unknown track" in _payload(res)["error"]


def test_large_mission_plan_is_capped_with_explicit_markers(server):
    big = [[47.631, -122.159], [47.631, -122.121], [47.659, -122.121], [47.659, -122.159]]

    async def main():
        belt = _tools(await build_toolbelt(server, None, lambda d: None, FakeSdk))
        res = await belt["mission_dry_run"].handler({
            "vehicle": "Drone1", "kind": "grid_search", "polygon": big,
            "alt_agl_m": 30, "overlap_pct": 60})
        text = result_text(res)
        body = json.loads(text)
        assert len(text) <= RESULT_CHAR_LIMIT
        assert body.get("dry_run") is True
        assert "gate" in body and "ok" in body["gate"]  # scalars survive shaping
        if body.get("_truncated"):
            markers = [x for v in body.values() if isinstance(v, list) for x in v
                       if isinstance(x, dict) and x.get("_truncated")]
            assert markers and all(m["_omitted"] > 0 for m in markers)
        return body
    body = asyncio.run(main())
    assert body.get("_truncated") is True, "a 60%-overlap grid of the AO should exceed the cap"


# ---------------------------------------------------------------- shaping --

def test_small_payloads_are_untouched():
    obj = {"a": [1, 2, 3], "b": "x"}
    assert shrink(obj) == (obj, False)


def test_shrink_halves_the_largest_list_and_counts_what_it_dropped():
    obj = {"gate": {"ok": True}, "waypoints": [{"lat": i, "lon": i, "alt": 60}
                                               for i in range(5000)],
           "small": list(range(5))}
    out, truncated = shrink(obj, 6000)
    assert truncated is True
    assert len(dumps_compact(out)) <= 6000
    assert out["_truncated"] is True and "cut to fit" in out["_note"]
    assert out["gate"] == {"ok": True} and out["small"] == list(range(5))
    wps = out["waypoints"]
    marker = wps[-1]
    assert marker["_truncated"] is True
    assert marker["_omitted"] == 5000 - (len(wps) - 1)
    assert "omitted" in marker["_note"]
    assert wps[0] == {"lat": 0, "lon": 0, "alt": 60}


def test_shrink_handles_nested_lists_strings_and_top_level_lists():
    nested = {"report": {"contacts": [{"id": i, "history": list(range(300))}
                                      for i in range(60)]}}
    out, truncated = shrink(nested, 5000)
    assert truncated and len(dumps_compact(out)) <= 5000

    long_text = {"note": "y" * 50_000}
    out, truncated = shrink(long_text, 3000)
    assert truncated and len(dumps_compact(out)) <= 3000
    assert "[truncated" in out["note"]

    top = list(range(20_000))
    out, truncated = shrink(top, 2000)
    assert truncated and len(dumps_compact(out)) <= 2000
    assert out["result"][-1]["_omitted"] > 0

    wide = {f"k{i}": "v" * 20 for i in range(2000)}
    out, truncated = shrink(wide, 3000)
    assert truncated and len(dumps_compact(out)) <= 3000
    assert out["_omitted_keys"]


def test_shrink_is_never_silent():
    for obj in ({"x": list(range(10_000))}, {"s": "z" * 40_000}, list(range(9000))):
        out, truncated = shrink(obj, 1500)
        assert truncated is True
        assert "_truncated" in dumps_compact(out)


def test_shrink_never_lets_a_payload_marker_hide_its_own_cut():
    """Review finding: IntelService reports what it cut as a DICT under
    ``_truncated`` ({"dropped": [...]}); the toolbelt's own cut built
    ``{"_truncated": True, **work}`` and the payload's dict overwrote the True,
    so a result the toolbelt had cut read as not truncated."""
    payload = {"_truncated": {"dropped": ["raw"]}, "_note": "raw was dropped",
               "big": ["x" * 100] * 400}
    out, truncated = shrink(payload, 20_000)
    assert truncated is True
    assert out["_truncated"] is True                       # our flag wins
    assert out["_truncation"] == {"dropped": ["raw"]}      # the payload's report is kept
    assert "raw was dropped" in out["_note"]
    assert len(dumps_compact(out)) <= 20_000
    # a payload that already fits is returned untouched, marker and all
    small = {"_truncated": {"dropped": ["raw"]}, "id": "rpt:latest"}
    assert shrink(small, 20_000) == (small, False)


def test_toolbelt_carries_each_proxied_tools_schema_defaults(listing_server):
    """The approval card compares a dry run with the live call on EFFECTIVE
    plans, so the toolbelt publishes each tool's own defaults."""
    belt = build(listing_server)
    assert belt.defaults["uav_orbit_poi"]["radius_m"] == 150.0
    assert belt.defaults["mission_grid_search"]["overlap_pct"] == 20.0
    assert "vehicle" not in belt.defaults["mission_grid_search"]      # required: no default
    assert set(belt.defaults) == {n for n in belt.tool_names if n not in CURATED_TOOLS}
    assert tb.schema_defaults({"properties": {"a": {"default": 1}, "b": {}}}) == {"a": 1}
    assert tb.schema_defaults(None) == {}


# ---------------------------------------------------------------- curated --

def test_intel_tools_call_the_intel_service():
    intel = StubIntel()
    belt = _tools(build(None, intel))

    async def main():
        ov = _payload(await belt["intel_overview"].handler({}))
        assert ov["theater"]["id"] == "default"
        hits = _payload(await belt["intel_search"].handler(
            {"query": "sam", "types": ["track"], "limit": 99}))
        assert hits["count"] == 25 and len(hits["results"]) == 25
        ent = _payload(await belt["intel_entity"].handler({"id": "veh:Drone1"}))
        assert ent["id"] == "veh:Drone1"
        missing = await belt["intel_entity"].handler({"id": "veh:nope"})
        assert missing["is_error"] and _payload(missing)["error"]["code"] == "unknown_entity"
    asyncio.run(main())
    assert ("search", "sam", ["track"], 25) in intel.calls


def test_intel_entity_is_capped_at_20k():
    belt = _tools(build(None, StubIntel(entity_size=5000)))
    res = asyncio.run(belt["intel_entity"].handler({"id": "veh:Drone1"}))
    text = result_text(res)
    assert len(text) <= tb.ENTITY_CHAR_LIMIT
    assert json.loads(text)["_truncated"] is True


class ThreadRecordingIntel:
    """A sync reader shaped like IntelService (entity takes max_bytes)."""

    def __init__(self):
        self.threads = []
        self.max_bytes = []

    def overview(self):
        self.threads.append(threading.get_ident())
        return {"theater": {"id": "default"}}

    def search(self, query, types=None, limit=25):
        self.threads.append(threading.get_ident())
        return []

    def entity(self, entity_id, *, max_bytes=60_000):
        self.threads.append(threading.get_ident())
        self.max_bytes.append(max_bytes)
        return {"id": entity_id, "type": "vehicle", "fields": {}}


def test_sync_intel_readers_run_off_the_event_loop_and_get_the_entity_budget():
    """IntelService builds the graph synchronously; the toolbelt runs on the
    host loop (SSE + in-process MCP), so sync readers go to a worker thread,
    and intel_entity asks the service for its 20 KB budget (explicit
    `_truncated` markers) instead of relying on a character cut."""
    intel = ThreadRecordingIntel()
    belt = _tools(build(None, intel))

    async def main():
        loop_thread = threading.get_ident()
        await belt["intel_overview"].handler({})
        await belt["intel_search"].handler({"query": "x"})
        await belt["intel_entity"].handler({"id": "veh:Drone1"})
        return loop_thread
    loop_thread = asyncio.run(main())
    assert len(intel.threads) == 3
    assert loop_thread not in intel.threads
    assert intel.max_bytes == [tb.ENTITY_CHAR_LIMIT]


def test_intel_entity_uses_the_real_services_byte_budget(monkeypatch):
    """Against the real IntelService.entity: the toolbelt's budget is passed
    through, so an oversized entity is trimmed by the service's own
    fit_to_budget (a `_truncated` report naming max_bytes), not cut blind."""
    from types import SimpleNamespace

    from godseye_uav.intel_graph import IntelService

    rows = [{"track_id": f"TRK-r{i:03d}-0001", "platform": "Supply truck",
             "category": "logistics", "ob_class": "supply_truck",
             "location": {"lat": 47.64 + i * 0.001, "lon": -122.14},
             "notes": "n" * 400} for i in range(60)]
    ctx = SimpleNamespace(
        snapshot=lambda: {"vehicles": [], "missions": [], "contacts": [], "feeds": {}},
        track_rows=lambda: rows,
        theaters=lambda: {"theaters": [], "active": {"known": False}},
        recent_events=lambda limit: [], mission_details=dict, threat_rings=dict,
        geofence_doc=lambda: None)
    svc = IntelService(ctx)
    belt = _tools(build(None, svc))
    res = asyncio.run(belt["intel_entity"].handler({"id": "veh:none"}))
    assert res["is_error"] and _payload(res)["error"]["code"] == "unknown_entity"
    # The equipment class relates to all 60 contacts: a long `related` list.
    eid = "ob:supply_truck"
    full = svc.entity(eid)
    assert full is not None and len(full["related"]) >= 50
    size = len(dumps_compact(full).encode())
    budget = size // 2
    monkeypatch.setattr(tb, "ENTITY_CHAR_LIMIT", budget)
    res = asyncio.run(belt["intel_entity"].handler({"id": eid}))
    text = result_text(res)
    body = json.loads(text)
    assert len(text.encode()) <= budget
    assert body["id"] == eid
    assert body["related"][-1]["_truncated"] is True
    assert isinstance(body["_truncated"], dict) and body["_truncated"]["max_bytes"] == budget


def test_intel_tools_report_a_missing_service():
    belt = _tools(build(None, None))
    for name, args in (("intel_overview", {}), ("intel_search", {"query": "x"}),
                       ("intel_entity", {"id": "veh:D"})):
        res = asyncio.run(belt[name].handler(args))
        assert res["is_error"] and _payload(res)["error"]["code"] == "intel_unavailable"


def test_ui_tools_emit_directives():
    emitted = []
    belt = _tools(build(None, None, emitted))

    async def main():
        for name, args in (("ui_focus", {"ids": ["veh:Drone1", "trk:TRK-a-0001"], "note": "hi"}),
                           ("ui_track", {"vehicle": "Drone1", "reason": "watch"}),
                           ("ui_show_orb", {}),
                           ("ui_inspect", {"id": "msn:MSN-1a2b3c4d"})):
            res = await belt[name].handler(args)
            assert _payload(res) == {"ok": True}
    asyncio.run(main())
    assert emitted == [
        {"action": "focus", "ids": ["veh:Drone1", "trk:TRK-a-0001"], "note": "hi"},
        {"action": "track", "vehicle": "Drone1", "reason": "watch"},
        {"action": "orb"},
        {"action": "inspect", "id": "msn:MSN-1a2b3c4d"},
    ]


def test_ui_emit_failure_is_an_error_result():
    def boom(_):
        raise ValueError("no")
    belt = _tools(asyncio.run(build_toolbelt(None, None, boom, FakeSdk)))
    res = asyncio.run(belt["ui_show_orb"].handler({}))
    assert res["is_error"]


# -------------------------------------------------------------- resources --

@pytest.mark.parametrize("uri", [
    "uav://targets", "uav://tracks", "uav://Drone1/camera/0/scene",
    "uav://mission/../targets", "uav://mission/a/b", "file:///etc/passwd",
    "uav://reports/latest?x=1", "uav://safety/geofence/extra", "http://127.0.0.1:8790/snapshot",
    "uav://pattern-of-life/*", " uav://targets",
])
def test_resource_reads_outside_the_allowlist_are_refused(listing_server, uri):
    belt = _tools(build(listing_server))
    res = asyncio.run(belt["read_intel_resource"].handler({"uri": uri}))
    assert res["is_error"]
    assert _payload(res)["error"]["code"] == "resource_not_allowed"


@pytest.mark.parametrize("uri", [
    "uav://mission/MSN-1a2b3c4d", "uav://reports/latest", "uav://reports/MSN-1a2b3c4d",
    "uav://pattern-of-life/all", "uav://pattern-of-life/North Field",
    "uav://safety/geofence", "uav://Drone1/telemetry",
])
def test_allowlisted_uris_match(uri):
    assert any(rx.match(uri) for rx in tb.RESOURCE_ALLOWLIST)


def test_allowlisted_resource_is_read_in_process(server):
    async def main():
        belt = _tools(await build_toolbelt(server, None, lambda d: None, FakeSdk))
        geo = await belt["read_intel_resource"].handler({"uri": "uav://safety/geofence"})
        assert not geo.get("is_error")
        assert "isr_only" in _payload(geo) or "envelope" in _payload(geo)
        tele = await belt["read_intel_resource"].handler({"uri": "uav://Drone1/telemetry"})
        assert _payload(tele)["vehicle"] == "Drone1"
        gone = await belt["read_intel_resource"].handler({"uri": "uav://mission/MSN-00000000"})
        assert gone["is_error"] and _payload(gone)["error"]["code"] == "resource_failed"
    asyncio.run(main())


# --------------------------------------------------------------- entities --

def test_extract_entities_finds_graph_ids():
    payload = {"vehicle": "Drone1", "mission_id": "MSN-1a2b3c4d",
               "tracks": [{"track_id": "TRK-a-0001"}, {"track_id": "TRK-a-0002"}],
               "results": [{"id": "unit:air-defense:TRK-a-0001"}, {"id": "not-an-id"}],
               "mission_handle": "not-a-mission"}
    assert extract_entities(payload) == ["veh:Drone1", "msn:MSN-1a2b3c4d", "trk:TRK-a-0001",
                                         "trk:TRK-a-0002", "unit:air-defense:TRK-a-0001"]
    many = {"tracks": [{"track_id": f"TRK-a-{i:04d}"} for i in range(100)]}
    assert len(extract_entities(many)) == 20
    assert extract_entities("text") == []


# --------------------------------------------------------------- real SDK --

def test_real_sdk_serves_the_toolbelt_end_to_end(server):
    sdk = pytest.importorskip("claude_agent_sdk")
    jsonschema = pytest.importorskip("jsonschema")
    from mcp import Client

    emitted = []

    async def main():
        belt = await build_toolbelt(server, StubIntel(), emitted.append, sdk)
        assert belt.server_config["type"] == "sdk" and belt.server_config["name"] == "godseye"
        for t in belt.tools:
            schema = sdk._build_input_schema(t)  # what the SDK puts on the wire
            assert schema is t.input_schema, t.name  # full JSON Schema passed through
            jsonschema.Draft202012Validator.check_schema(schema)
        async with Client(belt.server_config["instance"]) as client:
            listed = {t.name for t in (await client.list_tools()).tools}
            assert listed == set(belt.tool_names)
            res = await client.call_tool("uav_get_telemetry", {"vehicle": "Drone1"})
            assert not res.is_error
            assert json.loads(res.content[0].text)["vehicle"] == "Drone1"
            bad = await client.call_tool("uav_get_telemetry", {})
            assert bad.is_error  # schema validation by the SDK: vehicle is required
            await client.call_tool("ui_track", {"vehicle": "Drone1", "reason": "x"})
    asyncio.run(main())
    assert emitted == [{"action": "track", "vehicle": "Drone1", "reason": "x"}]
