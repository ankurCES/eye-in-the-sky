"""Bridge contract tests against FakeAirSim (UE-free, T8).

Two families here:

  * the original adapter/telemetry/camera tests, unchanged in intent;
  * BRIDGE_CONTRACT.md Wave-3 feeds — `missions[]`, `contacts[]`, the GeoJSON
    `/mission-overlay`, the SSE `/events` alarm lane, `/theaters`, configurable
    CORS and visible degradation.

The Wave-3 feeds are driven through a FAKE MCP transport rather than a live
MCP server: the bridge's only route to mission/track state is the loopback
JSON-RPC proxy (see the bridge module docstring), so faking the transport is
faking exactly the seam under test, and it lets a test assert what the bridge
does when a tool or resource is NOT registered yet — which is the state the
Wave-3 MCP server is in while it is being written.
"""
import asyncio
import json
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

# The AirSim PythonClient is put on sys.path by tests/conftest.py, which
# resolves it RELATIVE to the repo (sibling checkout, ci.sh's vendored copy,
# or $GODSEYE_AIRSIM_PYTHONCLIENT) and raises a message naming everything it
# searched when it cannot find one. This module used to carry an absolute
# path into one developer's home directory, which no clone or CI runner
# could satisfy.

from godseye_uav import bridge as bridge_mod
from godseye_uav.bridge import (
    AGL_FEED_SKEW_FLOOR_MS,
    AGL_FRAME_AGREED,
    AGL_FRAME_TOLERANCE_M,
    AGL_SOURCE_BRIDGE_LAUNCH,
    ALARM_SEVERITY,
    DEFAULT_CORS_ORIGINS,
    AirSimAdapter,
    Alarm,
    EventHub,
    McpClient,
    MissionFeed,
    MissionIntel,
    VehicleSnapshot,
    circle_ring,
    corridor_ring,
    cors_policy,
    create_app,
)
from godseye_uav.fake_airsim import FakeAirSim

# Ports in this module's assigned range (48000-48099) so a concurrent run of
# another test module cannot collide with the FakeAirSim this file boots.
# `PORT` is claimed at fixture time: a socket left in TIME_WAIT by a previous
# run must not error the whole module, so a few ports are tried in turn.
SIM_PORTS = range(48001, 48012)
LIVE_PORTS = range(48021, 48030)
PORT = SIM_PORTS.start
TOKEN = "test-token"


def _free_port(candidates):
    import socket

    for port in candidates:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError(f"no free port in {candidates}")


@pytest.fixture(scope="module")
def sim():
    global PORT
    last = None
    for port in SIM_PORTS:
        s = FakeAirSim(port=port)
        try:
            s.start()
        except OSError as e:
            last = e
            continue
        PORT = port
        time.sleep(0.3)
        yield s
        s.stop()
        return
    raise RuntimeError(f"no free sim port in {SIM_PORTS}: {last}")


@pytest.fixture()
def client(sim):
    adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
    app = create_app(adapter=adapter, token=TOKEN, start_loops=False)
    with TestClient(app) as tc:
        yield tc


H = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# a fake MCP transport: the bridge's only seam to mission/track state
# ---------------------------------------------------------------------------

def _wp(lat, lon, alt=60.0):
    return {"lat": lat, "lon": lon, "alt_m": alt}


GRID_POLYGON = [[47.6400, -122.1420], [47.6400, -122.1380],
                [47.6430, -122.1380], [47.6430, -122.1420]]

ROUTE = [_wp(47.6405, -122.1415), _wp(47.6405, -122.1385),
         _wp(47.6420, -122.1385), _wp(47.6420, -122.1415)]


class FakeMcp(McpClient):
    """MCP transport stub. `absent` names tools/resources that are NOT served."""

    def __init__(self, *, absent=(), tick=None, tracks=None, mission_state=None,
                 current=None, mission_doc=None, geofence=None, threat=None):
        super().__init__("fake://mcp", "t")
        self.absent = set(absent)
        self.tick = tick if tick is not None else self.default_tick()
        self.tracks = tracks if tracks is not None else []
        self.mission_state = mission_state or {}
        self.current = current
        self.mission_doc = mission_doc
        self.geofence = geofence
        self.threat = threat
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def default_tick(**over):
        tick = {
            "t": 100.0, "telemetry": True, "fuel_pct": 72.0,
            "bingo": {"fuel_pct": 72.0, "bingo_fuel_pct": 25.0,
                      "margin_pct": 47.0, "below_bingo": False,
                      "latched": False},
            "violations": [], "breaches": [], "alarms": [],
            "link": {"lost": False, "degraded": False},
            "force_rtb": False, "rtb_reasons": [],
            "fuel_record": {"burned_pct": 28.0, "elapsed_s": 140.0,
                            "phase": "cruise"},
        }
        tick.update(over)
        return tick

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name in self.absent:
            return None, f"{name}: Unknown tool: {name}"
        if name == "uav_task_status":
            out = {"vehicle": arguments["vehicle"], "state": "running",
                   "current": self.current, "queued": 0, "pending": [],
                   "fuel_pct": self.tick.get("fuel_pct"),
                   "bingo_latched": self.tick["bingo"]["latched"],
                   "mission_status": None, "last_tick": self.tick}
            return out, None
        if name == "mission_status":
            return dict(self.mission_state), None
        if name == "uav_list_tracks":
            return {"count": len(self.tracks), "tracks": self.tracks}, None
        if name == "uav_assess_threat":
            if self.threat is None:
                return None, "uav_assess_threat: no assessment"
            return self.threat, None
        return None, f"{name}: not stubbed"

    def read_resource(self, uri):
        self.calls.append(("resource", {"uri": uri}))
        if uri in self.absent:
            return None, f"{uri}: Unknown resource"
        if uri.startswith("uav://mission/"):
            if self.mission_doc is None:
                return None, f"{uri}: no mission"
            return {"mission": self.mission_doc}, None
        if uri == "uav://safety/geofence":
            if self.geofence is None:
                return None, "uav://safety/geofence: not served"
            return self.geofence, None
        return None, f"{uri}: not stubbed"


def salute_row(track_id, lat, lon, *, category="sam_medium_range",
               confidence="probable", last_seen=1_700_000_000.0):
    return {
        "format": "SALUTE", "track_id": track_id,
        "size": {"count": 2, "element": "battery", "text": "2 x SA-6"},
        "activity": {"code": "emplaced", "text": "emplaced in position"},
        "location": {"lat": lat, "lon": lon, "alt_m": 1548.0},
        "unit": {"category": category, "text": "SA-6 — battery"},
        "time": {"epoch": int(last_seen), "iso": "2023-11-14T22:13:20Z",
                 "last_seen": last_seen},
        "equipment": {"platform": "SA-6", "text": "SA-6 (sam)",
                      "weapon_range_m": 24000.0, "acquisition_range_m": 60000.0},
        "category": category, "confidence_level": confidence,
        "lat": lat, "lon": lon,
    }


MISSION_DOC = {
    "mission_id": "msn-0007", "vehicle": "Drone1", "kind": "grid_search",
    "task_id": "tsk-1", "state": "executing", "waypoints": ROUTE,
    "meta": {"pattern": "lawnmower", "swath_m": 84.0, "lane_spacing_m": 67.2,
             "lane_spacing_derived_m": 67.2, "lanes": 5, "lanes_required": 5,
             "track_id": "TRK-ABC-0003",
             "_plan_params": {"polygon": GRID_POLYGON, "alt_agl_m": 60.0}},
    "truncated": False,
    "lost_link_plan": {"action": "rtb", "name": "loal-rtb"},
}

CURRENT = {"task_id": "tsk-1", "tool": "uav_fly_route", "vehicle": "Drone1",
           "state": "running", "progress_pct": 43.5, "waypoint": 6,
           "waypoints_total": 14, "eta_s": 480.0, "mission_id": "msn-0007",
           "uncancellable": False}

MISSION_STATE = {"mission_id": "msn-0007", "vehicle": "Drone1",
                 "kind": "grid_search", "state": "running",
                 "progress_pct": 43.5, "waypoint": 6, "waypoints_total": 14,
                 "eta_s": 480.0, "fuel_pct": 72.0, "bingo_fuel_pct": 25.0,
                 "mission_status": None}

GEOFENCE = {"geofence": [[47.636, -122.145], [47.636, -122.135],
                         [47.647, -122.135], [47.647, -122.145]],
            "theater": {"id": "default", "label": "Redmond (AirSim default)",
                        "ao": [[47.636, -122.145], [47.636, -122.135],
                               [47.647, -122.135], [47.647, -122.145]]}}

THREATREP = {"format": "THREATREP", "count": 1, "highest_threat": "high",
             "assessments": [{"track_id": "TRK-ABC-0003", "threat_level": "high",
                              "envelope_m": 24000.0,
                              "assessment": {"capability": {
                                  "envelope_m": 24000.0,
                                  "acquisition_range_m": 60000.0}}}]}


def full_mcp(**over):
    kw = {"current": CURRENT, "mission_state": MISSION_STATE,
          "mission_doc": MISSION_DOC, "geofence": GEOFENCE, "threat": THREATREP,
          "tracks": [salute_row("TRK-ABC-0003", 47.6412, -122.1400)]}
    kw.update(over)
    return FakeMcp(**kw)


def wired(mcp, *, flown=None, vehicles=("Drone1",)):
    """A MissionFeed on a fake transport, plus its hub."""
    hub = EventHub()
    feed = MissionFeed(mcp, hub, vehicles=lambda: list(vehicles),
                       flown=lambda v: list(flown or []))
    return feed, hub


@pytest.fixture()
def fed_client(sim):
    """A bridge whose loop-C state source is the fake MCP transport."""
    adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
    mcp = full_mcp()
    state_holder = {}
    app = create_app(adapter=adapter, token=TOKEN, start_loops=False)
    feed = MissionFeed(mcp, app.state.hub,
                       vehicles=adapter.vehicles,
                       flown=app.state.bridge.flown)
    app.state.bridge.feed = feed
    app.state.feed = feed
    state_holder["mcp"] = mcp
    app.state.fake_mcp = mcp
    with TestClient(app) as tc:
        yield tc


# ===========================================================================
# original coverage
# ===========================================================================

class TestAuth:
    def test_health_open(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_snapshot_requires_auth(self, client):
        assert client.get("/snapshot").status_code == 401

    def test_snapshot_with_auth(self, client):
        r = client.get("/snapshot", headers=H)
        assert r.status_code == 200


class TestSnapshot:
    def test_snapshot_shape_and_althae(self, client):
        adapter = client.app.state.bridge.adapter
        snap = adapter.snapshot("Drone1")
        assert snap is not None
        # T1: altHae = altMSL + N. N is the EGM96 geoid undulation, which is
        # NEGATIVE in the Pacific Northwest. -22.21 m at the Redmond origin is
        # the published EGM96 value, used here as an EXTERNAL oracle: pinning
        # the bridge to the converter's own output would be tautological, and
        # this assertion previously pinned -28.8 -- the output of a broken
        # latitude-only approximation -- which kept the dead-geoid bug green.
        assert abs((snap.alt_hae - snap.alt_msl) - (-22.21)) < 1.0
        r = client.get("/snapshot", headers=H)
        body = r.json()
        assert body["sim_state"].startswith("up")
        assert "vehicles" in body

    def test_t1_datum_at_ned_origin(self, client):
        """PLAN Phase 0 datum gate (T1): a drone at NED 0 must report the
        origin's own altitude, in both datums, with the geoid applied exactly
        once.

        This pins the datum CONVENTION as well as the arithmetic.
        `geo.GeoPoint.altitude` is HAE ("altitude = altHae (ellipsoid)"), and
        `ned_to_geodetic` returns a GeoPoint, so at NED 0 alt_hae is the
        origin altitude VERBATIM and alt_msl is that minus N. The test
        previously asserted the opposite convention -- that the origin
        altitude was MSL -- which is how a second application of the geoid on
        the telemetry path stayed green for a whole wave.
        """
        from godseye_uav.bridge import DEFAULT_HOME
        from godseye_uav.geo import canonical_altitude

        adapter = client.app.state.bridge.adapter
        snap = adapter.snapshot("Drone1")
        assert snap is not None

        origin_hae = DEFAULT_HOME.altitude
        fix = canonical_altitude(
            origin_hae, DEFAULT_HOME.latitude, DEFAULT_HOME.longitude, datum="hae"
        )
        # The drone sits at the origin before any takeoff, so NED z ~ 0.
        assert abs(snap.alt_hae - origin_hae) < 1.0, (
            f"alt_hae {snap.alt_hae} should be the origin HAE {origin_hae}"
        )
        assert abs(snap.alt_msl - fix.alt_msl) < 1.0, (
            f"alt_msl {snap.alt_msl} should be {fix.alt_msl} "
            f"(= {origin_hae} HAE - N {fix.undulation_m:.3f})"
        )
        # And the geoid must have been applied exactly once, not twice.
        assert abs((snap.alt_hae - snap.alt_msl) - fix.undulation_m) < 0.5
        assert fix.degraded is False, "datum gate must not pass on a degraded geoid"

    def test_bridge_and_mcp_server_agree_on_the_same_aircraft(self, sim):
        """T1 cross-check: the HUD altitude and the MCP server's own telemetry
        for ONE aircraft, from ONE home, must be the SAME number.

        `launch.py` converts the theater's declared MSL ground elevation to HAE
        at its ingest boundary and hands that one `GeoPoint` to BOTH
        `server.UavBackend` and `bridge.AirSimAdapter`. The bridge used to read
        that HAE altitude as MSL and add N a second time, so the two published
        altitudes for the same aircraft at the same instant differed by the
        undulation: +1.5 m at Natanz, -22.2 m at Redmond, -33.2 m at
        indo-pak-loc. Both cannot be gate-grade.

        Fails on the old code by |N|.
        """
        from godseye_uav import theaters
        from godseye_uav.bridge import AirSimAdapter
        from godseye_uav.geo import GeoPoint, canonical_altitude

        t = theaters.get("iran-natanz")
        # exactly what launch.home_geopoint() does: MSL in, HAE out, once.
        home_fix = canonical_altitude(t.home_alt_msl_m, t.home_lat, t.home_lon,
                                      datum="msl")
        home = GeoPoint(t.home_lat, t.home_lon, home_fix.alt_hae)

        adapter = AirSimAdapter(ip="127.0.0.1", port=PORT, home=home)
        snap = adapter.snapshot("Drone1")
        assert snap is not None

        # At NED 0 the aircraft is on the theater's own ground, so its
        # orthometric height IS the theater's declared ground elevation.
        assert abs(snap.alt_msl - t.home_alt_msl_m) < 0.5, (
            f"alt_msl {snap.alt_msl:.3f} should be the theater's declared "
            f"ground elevation {t.home_alt_msl_m} m MSL, not that plus N "
            f"({home_fix.undulation_m:+.3f} m) applied a second time")
        assert abs(snap.alt_hae - home_fix.alt_hae) < 0.5, (
            f"alt_hae {snap.alt_hae:.3f} should be the home HAE "
            f"{home_fix.alt_hae:.3f}")

    def test_an_msl_home_is_converted_once_at_the_boundary(self, sim):
        """A caller holding an MSL number says so, and N is applied ONCE.

        `home_datum` exists so an unlabelled home altitude cannot silently
        become a double conversion again.
        """
        from godseye_uav.bridge import AirSimAdapter
        from godseye_uav.geo import GeoPoint, canonical_altitude

        home_msl = GeoPoint(33.7243, 51.7286, 1580.0)
        fix = canonical_altitude(1580.0, 33.7243, 51.7286, datum="msl")
        adapter = AirSimAdapter(ip="127.0.0.1", port=PORT, home=home_msl,
                                home_datum="msl")
        snap = adapter.snapshot("Drone1")
        assert snap is not None
        assert abs(snap.alt_msl - 1580.0) < 0.5, snap.alt_msl
        assert abs(snap.alt_hae - fix.alt_hae) < 0.5, snap.alt_hae
        with pytest.raises(ValueError, match="home_datum"):
            AirSimAdapter(ip="127.0.0.1", port=PORT, home=home_msl,
                          home_datum="agl")

    def test_snapshot_via_loop(self, client):
        state = client.app.state.bridge
        t = threading.Thread(target=state.telemetry_loop, kwargs={"hz": 20},
                             daemon=True)
        t.start()
        time.sleep(0.4)
        r = client.get("/snapshot", headers=H)
        assert r.json()["count"] >= 1
        veh = r.json()["vehicles"][0]
        for k in ("latitude", "longitude", "alt_hae", "speed_ms", "landed_state"):
            assert k in veh
        state.stop()


class TestCamera:
    def test_camera_on_demand(self, client):
        r = client.get("/camera/Drone1", headers=H)
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:4] == b"\x89PNG"

    def test_camera_subscriber_gated(self, client):
        state = client.app.state.bridge
        r = client.get("/camera/Drone1?type=7", headers=H)  # Infrared
        assert r.status_code == 200
        assert ("Drone1", "0", 7) in state._cam_subs  # lazy subscribe

    def test_camera_subscription_expires_so_loop_b_releases(self, client):
        """T3 loop-B budget: a browser tab that closed must stop costing frames.

        Fails on the old code, where `subscribe_camera` added to a `set` that
        nothing ever removed from — loop B pulled images forever.
        """
        state = client.app.state.bridge
        client.get("/camera/Drone1?type=5", headers=H)
        key = ("Drone1", "0", 5)
        assert key in state.active_camera_subs()
        with state._lock:
            state._cam_subs[key] = time.time() - bridge_mod.CAMERA_SUB_TTL_S - 1.0
        assert key not in state.active_camera_subs()
        assert key not in state._cam_subs, "the expired subscription must be dropped"

    def test_post_camera_subscribe_actually_subscribes(self, client):
        """`POST /camera/subscribe` must register the subscription it claims.

        Fails on the old code, whose handler read no body, took no vehicle and
        called nothing at all — it returned `{"subscribed": true}` to every
        caller and loop B pulled not one frame for them. A client that asked
        for a feed was told it had one; only the lazy subscribe inside
        `GET /camera/{veh}` ever worked.
        """
        state = client.app.state.bridge
        key = ("Drone2", "3", 7)
        assert key not in state.active_camera_subs()
        r = client.post("/camera/subscribe", headers=H,
                        json={"vehicle": "Drone2", "camera": "3", "type": 7})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["subscribed"] is True
        assert (body["vehicle"], body["camera"], body["type"]) == key
        assert key in state.active_camera_subs(), (
            "the route answered 'subscribed' without subscribing anything")
        assert {"vehicle": "Drone2", "camera": "3", "type": 7} in body["active"]

    def test_post_camera_subscribe_refuses_a_subscription_to_nothing(self, client):
        """No vehicle -> 400, not a cheerful `{"subscribed": true}`."""
        r = client.post("/camera/subscribe", headers=H, json={})
        assert r.status_code == 400
        assert "vehicle" in r.json()["detail"]
        assert client.post("/camera/subscribe", headers=H).status_code == 400

    def test_camera_rpcs_are_serialized_with_the_telemetry_loop(self, sim):
        """rpc_patch hands back a LOCK because one msgpack-rpc client on one
        tornado IOLoop cannot be driven by two threads at once.

        Loops A (telemetry), B (camera) and C (roster) plus the `/camera`
        request thread all share `AirSimAdapter._client`. The lock was
        assigned in `_ensure` and then used nowhere, which is the same as not
        having one. Fails on the old code: concurrency reaches 2.
        """
        import threading as _t

        adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
        adapter._ensure()
        live = {"now": 0, "max": 0}
        guard = _t.Lock()

        def watched(original):
            def call(*a, **kw):
                with guard:
                    live["now"] += 1
                    live["max"] = max(live["max"], live["now"])
                try:
                    time.sleep(0.02)
                    return original(*a, **kw)
                finally:
                    with guard:
                        live["now"] -= 1
            return call

        c = adapter._ensure()
        c.getMultirotorState = watched(c.getMultirotorState)
        c.listVehicles = watched(c.listVehicles)

        threads = [_t.Thread(target=adapter.snapshot, args=("Drone1",))
                   for _ in range(4)]
        threads += [_t.Thread(target=adapter.vehicles) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)
        assert live["max"] == 1, (
            f"{live['max']} concurrent msgpack-rpc calls on one shared client; "
            "rpc_patch's lock is not being taken")

    def test_loop_b_pulls_frames_only_for_live_subscriptions(self, client):
        state = client.app.state.bridge
        pulled = []
        state.adapter.camera_png = lambda v, c, t: pulled.append((v, c, t)) or b"x"
        th = threading.Thread(target=state.camera_loop, kwargs={"hz": 40},
                              daemon=True)
        th.start()
        time.sleep(0.2)
        assert pulled == [], "loop B must be idle with no subscriber"
        state.subscribe_camera("Drone1", "0", 0)
        time.sleep(0.2)
        assert pulled, "a live subscriber must be served"
        state.unsubscribe_camera("Drone1", "0", 0)
        time.sleep(0.15)
        pulled.clear()
        time.sleep(0.2)
        state.stop()
        assert pulled == [], "an unsubscribed camera must stop costing frames"


class TestLoopBudgets:
    """T3: three loops, each on its own budget, none blocking another."""

    def test_loop_a_never_touches_the_mcp_server(self, fed_client):
        """The telemetry tick reads loop C's CACHE. If it called the MCP server
        instead, a slow or dead MCP would drag the 5 Hz poll down with it."""
        state = fed_client.app.state.bridge
        mcp = fed_client.app.state.fake_mcp
        fed_client.app.state.feed.poll_once()
        mcp.calls.clear()
        for _ in range(5):
            state.tick_vehicle("Drone1")
        assert mcp.calls == [], f"loop A did MCP I/O: {mcp.calls}"
        # ...and it still picked the cached mission state up.
        assert state.get_snapshot("Drone1").fuel_pct == 72.0

    def test_loop_a_holds_its_rate(self, client):
        """<=10 Hz (T3). A loop that free-runs starves the sim's physics."""
        state = client.app.state.bridge
        ticks = []
        state.adapter.snapshot = lambda name: ticks.append(time.monotonic())
        th = threading.Thread(target=state.telemetry_loop, kwargs={"hz": 10.0},
                              daemon=True)
        th.start()
        time.sleep(0.9)
        state.stop()
        time.sleep(0.15)
        assert 4 <= len(ticks) <= 14, f"{len(ticks)} ticks in ~0.9 s at 10 Hz"

    def test_loop_a_does_not_re_read_the_roster_every_tick(self, client):
        """`listVehicles` is an RPC round trip; at 10 Hz it was half of loop
        A's budget spent re-reading a constant."""
        state = client.app.state.bridge
        calls = []
        state.adapter.vehicles = lambda: calls.append(1) or ["Drone1"]
        for _ in range(20):
            state.roster()
        assert len(calls) == 1, f"{len(calls)} roster reads for 20 ticks"
        state._roster_at -= bridge_mod.ROSTER_TTL_S + 1.0
        state.roster()
        assert len(calls) == 2, "the roster must still refresh after its TTL"

    def test_loop_c_runs_on_its_own_cadence(self):
        feed, _ = wired(full_mcp())
        th = threading.Thread(target=feed.loop, kwargs={"hz": 20.0}, daemon=True)
        th.start()
        time.sleep(0.6)
        feed.stop()
        time.sleep(0.2)
        assert feed.polls >= 2, feed.polls
        assert feed.intel().missions


class TestOverlay:
    def test_mission_overlay_geojson(self, client):
        r = client.get("/mission-overlay", headers=H)
        assert r.status_code == 200
        assert r.json()["type"] == "FeatureCollection"


# ===========================================================================
# BRIDGE_CONTRACT: CORS is configurable (assignment item 6)
# ===========================================================================

class TestCors:
    def test_default_allowlist_unchanged(self):
        p = cors_policy(None, env={})
        assert p.origins == DEFAULT_CORS_ORIGINS
        assert p.allow_credentials is True
        assert p.source == "default"

    def test_env_overrides_the_allowlist(self):
        p = cors_policy(None, env={"GODSEYE_BRIDGE_CORS_ORIGINS":
                                   "http://localhost:3000, http://foo.test:9"})
        assert p.origins == ("http://localhost:3000", "http://foo.test:9")
        assert p.allow_any is False

    def test_wildcard_disables_credentials(self):
        # `*` + credentials is rejected by every browser; the bridge uses a
        # Bearer header, so the wildcard must drop credentials, not lie.
        p = cors_policy("*")
        assert p.allow_any is True
        assert p.allow_credentials is False

    def test_a_dev_server_on_a_new_port_is_allowed(self, sim):
        """The old code hard-coded three origins; anything else failed every
        fetch before it started. This fails on the old bridge."""
        app = create_app(adapter=AirSimAdapter(ip="127.0.0.1", port=PORT),
                         token=TOKEN, start_loops=False,
                         cors=cors_policy("http://localhost:31337"))
        with TestClient(app) as tc:
            r = tc.get("/health", headers={"Origin": "http://localhost:31337"})
            assert r.headers.get("access-control-allow-origin") == \
                "http://localhost:31337"

    def test_health_publishes_the_effective_policy(self, client):
        cors = client.get("/health").json()["cors"]
        assert cors["env"] == "GODSEYE_BRIDGE_CORS_ORIGINS"
        assert "http://localhost:5173" in cors["origins"]


# ===========================================================================
# BRIDGE_CONTRACT: GET /theaters (assignment item 5)
# ===========================================================================

class TestTheaters:
    def test_theaters_served_from_the_single_source(self, client):
        from godseye_uav import theaters

        r = client.get("/theaters", headers=H)
        assert r.status_code == 200, "the route did not exist before Wave 3"
        body = r.json()
        table = theaters.as_payload()
        # Everything except `active` is the table verbatim; `active` is the one
        # part the table cannot know, so it is compared separately below.
        assert {k: v for k, v in body.items() if k != "active"} == \
               {k: v for k, v in table.items() if k != "active"}
        assert body["schema"] == "godseye.theaters/v1"
        assert body["alt_datum"] == "MSL"
        assert {t["id"] for t in body["theaters"]} == set(theaters.ids())

    def test_theaters_requires_auth(self, client):
        assert client.get("/theaters").status_code == 401


# ===========================================================================
# INTEGRATION_FINDINGS UI-1: the bridge must publish the ACTIVE theater
#
# Verified live: `launch.py --theater iran-isfahan`, the aircraft correctly at
# 32.6546/51.668 - and the command center's selector sat on `default`, listing
# REDMOND POIs, one click from seeding a target 10,000 km away. The panel was
# not at fault: `GET /theaters` served the TABLE default and `GET /health` had
# no theater key at all. Neither feed could answer the question.
# ===========================================================================

ISFAHAN_GEOFENCE = {
    "geofence": [[32.63, 51.63], [32.63, 51.71], [32.68, 51.71], [32.68, 51.63]],
    "theater": {"id": "iran-isfahan", "label": "Iran — Isfahan",
                "ground_elevation_msl_m": 1570.0,
                "ao": [[32.63, 51.63], [32.63, 51.71],
                       [32.68, 51.71], [32.68, 51.63]]},
    "theater_mismatch": None,
}


class TestActiveTheater:
    @staticmethod
    def _client(mcp, *, poll=True):
        """A bridge whose loop C reads `mcp`, polled once by hand."""
        adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
        app = create_app(adapter=adapter, token=TOKEN, start_loops=False)
        feed = MissionFeed(mcp, app.state.hub, vehicles=adapter.vehicles,
                           flown=app.state.bridge.flown)
        app.state.bridge.feed = feed
        app.state.feed = feed
        if poll:
            feed.poll_once()
        return TestClient(app), feed

    def test_health_and_theaters_publish_the_running_theater_not_the_default(
            self, sim):
        """The UI-1 regression, asserted as the OUTCOME an operator reads.

        On the pre-fix bridge `/health` had no `theater` key and `/theaters`
        had no `active` key, so this raises KeyError rather than merely
        reporting the wrong place.
        """
        from godseye_uav import theaters

        tc, _feed = self._client(full_mcp(geofence=ISFAHAN_GEOFENCE))
        with tc as client:
            health = client.get("/health").json()
            table = client.get("/theaters", headers=H).json()

        for where, active in (("/health", health["theater"]),
                              ("/theaters", table["active"])):
            assert active["known"] is True, where
            assert active["id"] == "iran-isfahan", where
            assert active["label"] == "Iran — Isfahan", where
            assert active["ground_elevation_msl_m"] == 1570.0, where
            assert active["in_table"] is True, where
            assert active["source"] == "mcp:uav://safety/geofence", where
            assert active["at_ms"] > 0, where
            assert active["reason"] == "", where
        # ONE shape, so the panel has one thing to adopt.
        assert health["theater"] == table["active"]
        # ...and it is emphatically not the table default, which is what the
        # panel was reduced to reading.
        assert table["default"] == theaters.DEFAULT_THEATER_ID == "default"
        assert table["active"]["id"] != table["default"]

    def test_an_unknown_active_theater_never_degrades_to_the_table_default(
            self, sim):
        """A silent fallback here is the whole defect: an aircraft over Iran
        shown as being at Redmond is worse than an aircraft shown as nowhere.
        With `uav://safety/geofence` unserved the answer must be UNKNOWN, and
        must say why."""
        from godseye_uav import theaters

        mcp = full_mcp(geofence=None, absent={"uav://safety/geofence"})
        tc, _feed = self._client(mcp)
        with tc as client:
            active = client.get("/health").json()["theater"]
            assert client.get("/theaters", headers=H).json()["active"] == active

        assert active["known"] is False
        assert active["id"] is None
        assert active["id"] != theaters.DEFAULT_THEATER_ID
        assert active["label"] is None
        assert active["ground_elevation_msl_m"] is None
        assert active["in_table"] is False
        assert "uav://safety/geofence" in active["reason"]
        assert "Unknown resource" in active["reason"], (
            "the reason must carry the upstream error, not just 'unknown' - "
            "the old code discarded it entirely")

    def test_before_the_first_poll_the_answer_is_unknown_and_says_so(self, sim):
        """A bridge that has not yet talked to the MCP server knows nothing.
        It must not fill the gap, and it must not 500."""
        tc, _feed = self._client(full_mcp(geofence=ISFAHAN_GEOFENCE), poll=False)
        with tc as client:
            active = client.get("/health").json()["theater"]
        assert active["known"] is False and active["id"] is None
        assert "has not been read yet" in active["reason"]

    def test_the_servers_theater_mismatch_is_carried_to_the_operator(self, sim):
        """`GodseyeUavServer.theater_mismatch` is its own report that the
        ENFORCED envelope belongs to a different theater than everything else
        it derives from the theater row - which buries targets ~1.4 km
        underground. It has to survive the trip to the HUD."""
        mismatch = {"theater_id": "default", "offset_km": 9_800.0,
                    "consequence": "targets spawn at the wrong ground"}
        geofence = dict(ISFAHAN_GEOFENCE, theater_mismatch=mismatch)
        tc, _feed = self._client(full_mcp(geofence=geofence))
        with tc as client:
            active = client.get("/health").json()["theater"]
        assert active["known"] is True and active["id"] == "iran-isfahan"
        assert active["theater_mismatch"] == mismatch

    def test_the_published_theater_is_not_a_live_alias_into_the_feeds_cache(
            self, sim):
        """`/health` returns `active_theater()` DIRECTLY — it is the one route
        that never goes through `as_payload`'s deep copy. `active_theater()`
        built the block with `theater_mismatch=doc.get("theater_mismatch")`,
        where `doc` is the feed's OWN cached `uav://safety/geofence`, so the
        value handed out of the route was a live reference into `MissionFeed`
        state that the feed reads back under its lock.

        The table payload was already deep-copied against exactly this; the
        constructor that feeds it was not, so the copy was applied one level
        below the leak. Asserts the outcome: editing what the route returned
        must not edit what the feed then serves.

        FAILS ON THE PRE-FIX CODE: the second read came back tampered.
        """
        mismatch = {"theater_id": "default", "offset_km": 9_800.0}
        geofence = dict(ISFAHAN_GEOFENCE, theater_mismatch=mismatch)
        tc, feed = self._client(full_mcp(geofence=geofence))

        first = feed.active_theater()
        first["theater_mismatch"]["offset_km"] = 0.0
        first["theater_mismatch"]["theater_id"] = "tampered"

        second = feed.active_theater()
        assert second["theater_mismatch"] == {"theater_id": "default",
                                              "offset_km": 9_800.0}, (
            "the published block aliased the feed's cached geofence document, "
            "so a consumer editing its copy silently rewrote what every later "
            "reader of /health and /theaters is told")
        with tc as client:
            served = client.get("/health").json()["theater"]
        assert served["theater_mismatch"] == mismatch

    def test_a_theater_the_bridge_has_never_heard_of_is_flagged_not_dropped(
            self, sim):
        """The operator still needs to know where the aircraft is; the panel
        still needs to know it cannot draw the AO from its own table."""
        geofence = {"geofence": ISFAHAN_GEOFENCE["geofence"],
                    "theater": {"id": "forward-op-17", "label": "FOB 17",
                                "ground_elevation_msl_m": 640.0}}
        tc, _feed = self._client(full_mcp(geofence=geofence))
        with tc as client:
            active = client.get("/theaters", headers=H).json()["active"]
        assert active["known"] is True and active["id"] == "forward-op-17"
        assert active["in_table"] is False
        assert active["ao"] is None

    def test_a_theaterless_geofence_payload_is_unknown_not_guessed(self, sim):
        """The resource answered, but with no theater block. That is still
        'unknown', and the reason has to distinguish it from a dead server."""
        geofence = {"geofence": ISFAHAN_GEOFENCE["geofence"]}
        tc, _feed = self._client(full_mcp(geofence=geofence))
        with tc as client:
            active = client.get("/health").json()["theater"]
        assert active["known"] is False and active["id"] is None
        assert "no `theater` block" in active["reason"]

    def test_health_survives_a_state_source_that_cannot_answer(self, sim):
        """`create_app(state_source=...)` takes any MissionFeed-shaped object.
        One built before `active_theater()` existed must degrade visibly, not
        take `/health` down with it."""
        class OldFeed:
            polls = 0

            def intel(self):
                return MissionIntel()

            def overlay(self):
                return {"type": "FeatureCollection", "features": []}

            def note_datum(self, *_a):
                pass

            def poll_once(self):
                return MissionIntel()

            def loop(self, **_kw):
                pass

            def stop(self):
                pass

        app = create_app(adapter=AirSimAdapter(ip="127.0.0.1", port=PORT),
                         token=TOKEN, start_loops=False, state_source=OldFeed())
        with TestClient(app) as client:
            r = client.get("/health")
            assert r.status_code == 200
            active = r.json()["theater"]
        assert active["known"] is False and active["id"] is None
        assert "OldFeed" in active["reason"]

    def test_a_state_source_that_answers_with_rubbish_is_not_published(self, sim):
        """The sibling hole of the test above. A source with NO
        `active_theater()` degraded honestly; a source that HAS one and returns
        junk was trusted, and `/health` published it verbatim — `{}` or
        `known: true` with no id. Every consumer resolves the theater as
        `active.id or table.default`, so an idless block lands back on the
        TABLE default: UI-1 arriving through the field added to end it.

        `/health` is open and unauthenticated, so the answer is a visible
        UNKNOWN naming the source, not a 500 and not the junk.

        FAILS ON THE PRE-FIX CODE: `/health` served the rubbish block back.
        """
        class BadFeed:
            polls = 0

            def __init__(self, answer):
                self._answer = answer

            def active_theater(self):
                if isinstance(self._answer, Exception):
                    raise self._answer
                return self._answer

            def intel(self):
                return MissionIntel()

            def overlay(self):
                return {"type": "FeatureCollection", "features": []}

            def note_datum(self, *_a):
                pass

            def poll_once(self):
                return MissionIntel()

            def loop(self, **_kw):
                pass

            def stop(self):
                pass

        from godseye_uav import theaters

        for answer in ({},
                       {"known": True},
                       {"known": True, "id": None, "label": None,
                        "ground_elevation_msl_m": None, "ao": None,
                        "in_table": False, "theater_mismatch": None,
                        "source": "x", "at_ms": 1, "reason": ""},
                       RuntimeError("feed exploded")):
            app = create_app(adapter=AirSimAdapter(ip="127.0.0.1", port=PORT),
                             token=TOKEN, start_loops=False,
                             state_source=BadFeed(answer))
            with TestClient(app) as client:
                r = client.get("/health")
                assert r.status_code == 200, (answer, r.status_code)
                active = r.json()["theater"]
                table = client.get("/theaters", headers=H)
                assert table.status_code == 200, (answer, table.status_code)

            assert active["known"] is False, f"published {answer!r} as known"
            assert active["id"] is None
            assert "BadFeed" in active["reason"]
            # the whole point: it must not resolve to the table default
            assert (active["id"] or theaters.DEFAULT_THEATER_ID) == \
                theaters.DEFAULT_THEATER_ID
            assert table.json()["active"] == active

    def test_the_geofence_resource_is_read_once_and_retried_on_a_slow_clock(
            self, sim):
        """The active theater rides on the same cached read as the geofence
        layer: loop C must not re-read it on every 2 Hz poll."""
        mcp = full_mcp(geofence=ISFAHAN_GEOFENCE)
        tc, feed = self._client(mcp, poll=False)
        with tc:
            for _ in range(5):
                feed.poll_once()
        reads = [c for c in mcp.calls
                 if c[0] == "resource" and c[1]["uri"] == "uav://safety/geofence"]
        assert len(reads) == 1, reads
        assert feed.active_theater()["id"] == "iran-isfahan"


# ===========================================================================
# BRIDGE_CONTRACT: /snapshot.missions[] and .contacts[]
# ===========================================================================

class TestMissionsFeed:
    def test_missions_row_matches_the_contract(self):
        mcp = full_mcp()
        feed, _hub = wired(mcp, flown=[(47.6405, -122.1415), (47.6405, -122.1395)])
        intel = feed.poll_once()
        assert len(intel.missions) == 1, "missions[] did not exist before Wave 3"
        m = intel.missions[0]
        assert m["mission_id"] == "msn-0007"
        assert m["vehicle"] == "Drone1"
        assert m["kind"] == "grid_search"
        assert m["phase"] == "executing"
        assert m["active_tool"] == "uav_fly_route"
        assert m["progress_pct"] == 43.5
        assert m["waypoint"] == {"index": 6, "of": 14}
        assert m["eta_s"] == 480.0
        assert m["fuel_pct"] == 72.0
        assert m["bingo_fuel_pct"] == 25.0
        assert m["safety"] == {"geofence": "ok", "proximity_m": None,
                               "bingo_latched": False}
        assert m["incomplete_reason"] is None
        assert set(m) >= {"mission_id", "vehicle", "kind", "phase", "active_tool",
                          "progress_pct", "waypoint", "eta_s", "fuel_pct",
                          "bingo_fuel_pct", "coverage_pct", "safety",
                          "incomplete_reason"}

    def test_phase_is_rtb_while_the_forced_rtb_flies(self):
        mcp = full_mcp(current={**CURRENT, "tool": "uav_return_to_home"})
        feed, _ = wired(mcp)
        assert feed.poll_once().missions[0]["phase"] == "rtb"

    def test_a_bingo_latch_reads_as_rtb_before_the_rtb_task_starts(self):
        """The aircraft is committed the moment BINGO latches; the HUD must say
        so without waiting for uav_return_to_home to be the current tool."""
        tick = FakeMcp.default_tick(force_rtb=True, rtb_reasons=["bingo"])
        feed, _ = wired(full_mcp(tick=tick))
        assert feed.poll_once().missions[0]["phase"] == "rtb"

    def test_every_server_state_word_maps_to_a_contract_phase(self):
        """A state the map does not know must not silently become a phase the
        GEV normalizer shows as 'unknown'."""
        for state in ("queued", "running", "done", "failed", "cancelled",
                      "incomplete", "planning", None, "", "who-knows"):
            assert bridge_mod._phase_for(state, None) in bridge_mod.MISSION_PHASES
        assert bridge_mod._phase_for("done", None) == "complete"
        assert bridge_mod._phase_for("incomplete", None) == "aborted"
        assert bridge_mod._phase_for("who-knows", None) == "planning"

    def test_geofence_proximity_surfaces_in_mission_safety(self):
        tick = FakeMcp.default_tick(violations=[
            {"kind": "geofence_proximity", "severity": "warning",
             "message": "geofence_proximity(310m<500m)", "value": 310.0,
             "limit": 500.0}])
        feed, _ = wired(full_mcp(tick=tick))
        safety = feed.poll_once().missions[0]["safety"]
        assert safety["geofence"] == "proximity"
        assert safety["proximity_m"] == 310.0

    def test_bingo_latch_surfaces_and_marks_incomplete(self):
        tick = FakeMcp.default_tick(
            fuel_pct=24.0,
            bingo={"fuel_pct": 24.0, "bingo_fuel_pct": 25.0, "margin_pct": -1.0,
                   "below_bingo": True, "latched": True},
            force_rtb=True, rtb_reasons=["bingo"])
        mcp = full_mcp(tick=tick,
                       mission_state={**MISSION_STATE,
                                      "mission_status": "incomplete - fuel"})
        feed, _ = wired(mcp)
        m = feed.poll_once().missions[0]
        assert m["safety"]["bingo_latched"] is True
        assert m["incomplete_reason"] == "incomplete - fuel"

    def test_coverage_is_flown_not_planned(self):
        """M1: coverage_pct is the ground ACTUALLY imaged. A mission that has
        flown nothing reports 0%, not the planner's number."""
        nothing, _ = wired(full_mcp(), flown=[])
        assert nothing.poll_once().missions[0]["coverage_pct"] == 0.0

        # Two full sweeps of the polygon cover strictly more than none of it.
        sweep = [(47.6405, -122.1420), (47.6405, -122.1380),
                 (47.6415, -122.1380), (47.6415, -122.1420),
                 (47.6425, -122.1420), (47.6425, -122.1380)]
        flying, _ = wired(full_mcp(), flown=sweep)
        covered = flying.poll_once().missions[0]["coverage_pct"]
        assert covered > 50.0, covered
        assert covered <= 100.0

    def test_coverage_is_none_with_a_reason_when_not_area_scored(self):
        """NO SILENT FALLBACK: a route recon has no tasked polygon, so its
        coverage is unknown — never 0%, never 100%."""
        doc = {**MISSION_DOC, "kind": "recon_route",
               "meta": {"doctrine": "route-recon"}}
        feed, _ = wired(full_mcp(mission_doc=doc,
                                 mission_state={**MISSION_STATE,
                                                "kind": "recon_route"}),
                        flown=[(47.641, -122.140), (47.642, -122.139)])
        feed.poll_once()
        m = feed.poll_once().missions[0]
        assert m["coverage_pct"] is None
        assert "no tasked polygon" in m["coverage_basis"]

    def test_no_mission_means_no_mission_row(self):
        feed, _ = wired(full_mcp(current=None))
        assert feed.poll_once().missions == []

    def test_a_finished_mission_still_reports_how_it_ended(self):
        """The MCP catalog has no "list missions" tool, so a mission id is only
        learned from the task flying it. When that task drains the row must NOT
        vanish - `complete`/`aborted` and M4's "incomplete - fuel" are exactly
        the states an operator needs after the fact.

        Caught on the live stack: a finished grid search left `missions[]`
        empty, so the command centre showed no end state at all.
        """
        mcp = full_mcp()
        feed, _ = wired(mcp)
        assert feed.poll_once().missions[0]["phase"] == "executing"
        mcp.current = None                                   # queue drained
        mcp.mission_state = {**MISSION_STATE, "state": "done",
                             "progress_pct": 100.0}
        rows = feed.poll_once().missions
        assert [r["mission_id"] for r in rows] == ["msn-0007"]
        assert rows[0]["phase"] == "complete"
        assert rows[0]["progress_pct"] == 100.0

    def test_a_bingo_aborted_mission_keeps_its_incomplete_reason(self):
        mcp = full_mcp()
        feed, _ = wired(mcp)
        feed.poll_once()
        mcp.current = None
        mcp.mission_state = {**MISSION_STATE, "state": "incomplete",
                             "mission_status": "incomplete - fuel",
                             "bingo_latched": True}
        row = feed.poll_once().missions[0]
        assert row["phase"] == "aborted"
        assert row["incomplete_reason"] == "incomplete - fuel"
        assert row["safety"]["bingo_latched"] is True

    def test_a_finished_mission_is_eventually_dropped(self):
        mcp = full_mcp()
        feed, _ = wired(mcp)
        feed.poll_once()
        mcp.current = None
        mcp.mission_state = {**MISSION_STATE, "state": "done"}
        assert feed.poll_once().missions
        feed._finished_at["msn-0007"] -= bridge_mod.MISSION_RETAIN_S + 1.0
        assert feed.poll_once().missions == []


class TestContactsFeed:
    def test_confidence_survives_the_nested_report_shape(self):
        """`salute_report` mirrors the level flat AND nests the whole
        assessment under `confidence`. Both shapes must resolve, and a nested
        dict must never be str()'d into the field."""
        row = salute_row("TRK-X-1", 47.64, -122.14)
        row.pop("confidence_level")
        row["confidence"] = {"level": "confirmed", "score": 0.91,
                             "evidence": [{"k": "v"}]}
        feed, _ = wired(full_mcp(tracks=[row]))
        assert feed.poll_once().contacts[0]["confidence"] == "confirmed"

    def test_contacts_row_matches_the_contract(self):
        feed, _ = wired(full_mcp())
        intel = feed.poll_once()
        assert len(intel.contacts) == 1, "contacts[] did not exist before Wave 3"
        c = intel.contacts[0]
        assert c["track_id"] == "TRK-ABC-0003"
        assert c["category"] == "sam_medium_range"
        assert c["confidence"] == "probable"
        assert c["location"] == {"lat": 47.6412, "lon": -122.1400, "alt_m": 1548.0}
        assert c["last_seen_ms"] == 1_700_000_000_000
        assert c["threat_level"] == "high"
        assert set(c["salute"]) == {"size", "activity", "location", "unit",
                                    "time", "equipment"}
        assert all(isinstance(v, str) for v in c["salute"].values())
        assert c["salute"]["size"] == "2 x SA-6"
        assert c["salute"]["activity"] == "emplaced in position"

    def test_contact_location_is_the_contact_not_the_observer(self):
        """BRIDGE_CONTRACT: `location` is the CONTACT's position. The observer
        drone sits at the Redmond origin; the track does not."""
        feed, _ = wired(full_mcp())
        c = feed.poll_once().contacts[0]
        assert c["location"]["lat"] == pytest.approx(47.6412)
        assert c["location"]["lat"] != pytest.approx(47.641468)
        assert c["salute"]["location"].startswith("47.64120, -122.14000")

    def test_threat_level_is_none_not_faked_when_the_tool_is_absent(self):
        """NO SILENT FALLBACK: without uav_assess_threat the level is unknown
        and the feed says so — it never invents 'low'."""
        feed, _ = wired(full_mcp(absent={"uav_assess_threat"}))
        intel = feed.poll_once()
        assert intel.contacts[0]["threat_level"] is None
        assert "threat_level unavailable" in intel.feeds["contacts"].detail

    def test_threat_assessment_reruns_only_when_the_roster_changes(self):
        """contacts[] is an ON CHANGE feed: uav_assess_threat writes an audit
        record per call, so polling must not re-run it every tick."""
        mcp = full_mcp()
        feed, _ = wired(mcp)
        for _ in range(3):
            feed.poll_once()
        assert sum(1 for n, _ in mcp.calls if n == "uav_assess_threat") == 1
        mcp.tracks = mcp.tracks + [salute_row("TRK-ABC-0004", 47.643, -122.139)]
        feed.poll_once()
        assert sum(1 for n, _ in mcp.calls if n == "uav_assess_threat") == 2


class TestSnapshotEnvelope:
    def test_snapshot_carries_missions_and_contacts(self, fed_client):
        body = fed_client.get("/snapshot", headers=H).json()
        assert "missions" in body and "contacts" in body
        fed_client.app.state.feed.poll_once()
        body = fed_client.get("/snapshot", headers=H).json()
        assert [m["mission_id"] for m in body["missions"]] == ["msn-0007"]
        assert [c["track_id"] for c in body["contacts"]] == ["TRK-ABC-0003"]

    def test_vehicles_carry_the_populated_hud_fields(self, fed_client):
        state = fed_client.app.state.bridge
        fed_client.app.state.feed.poll_once()
        state.tick_vehicle("Drone1")
        veh = fed_client.get("/snapshot", headers=H).json()["vehicles"][0]
        # These five were dataclass defaults that nothing ever wrote.
        assert veh["fuel_pct"] == 72.0
        assert veh["bingo_fuel_pct"] == 25.0
        assert veh["eta_to_bingo_s"] is not None and veh["eta_to_bingo_s"] > 0
        assert veh["mission"] == "msn-0007"
        assert veh["track_id"] == "TRK-ABC-0003"
        # ...and all three altitudes plus the datum flag.
        for k in ("alt_hae", "alt_msl", "agl"):
            assert isinstance(veh[k], float)
        assert veh["datum_degraded"] is False
        assert veh["datum_source"]

    def test_fuel_defaults_to_unknown_never_to_a_full_tank(self, client):
        """The old dataclass pinned fuel_pct at 100.0, so an unwired bridge
        reported a full tank forever. Unknown must read as unknown."""
        state = client.app.state.bridge
        state.tick_vehicle("Drone1")
        veh = client.get("/snapshot", headers=H).json()["vehicles"][0]
        assert veh["fuel_pct"] is None
        assert veh["bingo_fuel_pct"] is None
        assert veh["eta_to_bingo_s"] is None
        assert veh["fuel_source"].startswith("unavailable")

    def test_eta_to_bingo_is_derived_from_the_measured_burn(self):
        feed, _ = wired(full_mcp())
        feed.poll_once()
        row = feed.intel().per_vehicle["Drone1"]
        # 28% burned over 140 s = 0.2 %/s; margin 72-25 = 47% -> 235 s.
        assert row["eta_to_bingo_s"] == pytest.approx(235.0, rel=0.02)
        assert "burn rate" in row["fuel_source"]

    def test_eta_is_none_before_any_fuel_is_burned(self):
        tick = FakeMcp.default_tick(
            fuel_record={"burned_pct": 0.0, "elapsed_s": 0.0, "phase": "idle"})
        feed, _ = wired(full_mcp(tick=tick))
        feed.poll_once()
        row = feed.intel().per_vehicle["Drone1"]
        assert row["eta_to_bingo_s"] is None
        assert "no fuel burned yet" in row["fuel_source"]

    def test_absent_mcp_tool_degrades_visibly_not_silently(self, sim):
        """TOOL_CONTRACT tools land in another agent's server.py. Until they do,
        the section must be EMPTY WITH A REASON, never a plausible default."""
        feed, _ = wired(FakeMcp(absent={"uav_task_status", "uav_list_tracks"}))
        intel = feed.poll_once()
        assert intel.missions == [] and intel.contacts == []
        assert intel.feeds["mission_state"].ok is False
        assert "Unknown tool" in intel.feeds["mission_state"].error
        assert intel.feeds["contacts"].ok is False

    def test_snapshot_reports_feed_health(self, fed_client):
        fed_client.app.state.feed.poll_once()
        feeds = fed_client.get("/snapshot", headers=H).json()["feeds"]
        assert feeds["mission_state"]["ok"] is True
        assert "uav_task_status" in feeds["mission_state"]["detail"]

    def test_snapshot_never_calls_the_mcp_server(self, fed_client):
        """BRIDGE_CONTRACT rule 4: a slow feed must not stall the poll."""
        mcp = fed_client.app.state.fake_mcp
        mcp.calls.clear()
        fed_client.get("/snapshot", headers=H)
        assert mcp.calls == [], f"/snapshot did I/O: {mcp.calls}"


# ===========================================================================
# BRIDGE_CONTRACT: /mission-overlay GeoJSON
# ===========================================================================

class TestMissionOverlay:
    def _overlay(self, **over):
        flown = over.pop("flown", [(47.6405, -122.1420), (47.6405, -122.1380),
                                   (47.6415, -122.1380), (47.6415, -122.1420)])
        feed, _ = wired(full_mcp(**over), flown=flown)
        feed.poll_once()
        return feed.overlay()

    def test_every_contract_kind_is_emitted(self):
        fc = self._overlay()
        assert fc["type"] == "FeatureCollection"
        kinds = {f["properties"]["kind"] for f in fc["features"]}
        # The old endpoint returned a hardcoded empty FeatureCollection.
        assert kinds == {"route", "flown", "waypoint", "grid", "coverage",
                         "geofence", "threat_ring", "target"}, kinds
        assert all("kind" in f["properties"] for f in fc["features"])

    def test_geometry_types_and_lon_lat_order(self):
        fc = self._overlay()
        by_kind = {}
        for f in fc["features"]:
            by_kind.setdefault(f["properties"]["kind"], []).append(f)
        assert by_kind["route"][0]["geometry"]["type"] == "LineString"
        assert by_kind["flown"][0]["geometry"]["type"] == "LineString"
        assert by_kind["waypoint"][0]["geometry"]["type"] == "Point"
        assert by_kind["grid"][0]["geometry"]["type"] == "Polygon"
        assert by_kind["coverage"][0]["geometry"]["type"] == "Polygon"
        assert by_kind["geofence"][0]["geometry"]["type"] == "Polygon"
        assert by_kind["threat_ring"][0]["geometry"]["type"] == "Polygon"
        assert by_kind["target"][0]["geometry"]["type"] == "Point"
        # GeoJSON is [lon, lat]; Redmond is lon ~ -122, lat ~ +47.
        lon, lat = by_kind["target"][0]["geometry"]["coordinates"]
        assert -123 < lon < -121 and 47 < lat < 48

    def test_waypoints_carry_index_and_reached(self):
        fc = self._overlay()
        wps = sorted((f for f in fc["features"]
                      if f["properties"]["kind"] == "waypoint"),
                     key=lambda f: f["properties"]["index"])
        assert [w["properties"]["index"] for w in wps] == [0, 1, 2, 3]
        # the task is at waypoint 6 of 14, so every one of these 4 is behind it
        assert all(w["properties"]["reached"] for w in wps)

    def test_waypoint_reached_tracks_the_task(self):
        fc = self._overlay(current={**CURRENT, "waypoint": 2})
        reached = {f["properties"]["index"]: f["properties"]["reached"]
                   for f in fc["features"] if f["properties"]["kind"] == "waypoint"}
        assert reached == {0: True, 1: True, 2: False, 3: False}

    def test_threat_rings_separate_engagement_from_acquisition(self):
        fc = self._overlay()
        rings = {f["properties"]["ring"]: f["properties"]
                 for f in fc["features"]
                 if f["properties"]["kind"] == "threat_ring"}
        assert set(rings) == {"engagement", "acquisition"}
        assert rings["engagement"]["radius_m"] == 24000.0
        assert rings["acquisition"]["radius_m"] == 60000.0
        assert rings["engagement"]["radius_m"] < rings["acquisition"]["radius_m"]
        assert all(r["track_id"] == "TRK-ABC-0003" for r in rings.values())

    def test_target_carries_track_id_and_confidence(self):
        fc = self._overlay()
        target = next(f for f in fc["features"]
                      if f["properties"]["kind"] == "target")
        assert target["properties"]["track_id"] == "TRK-ABC-0003"
        assert target["properties"]["confidence"] == "probable"

    def test_grid_carries_the_server_derived_spacing(self):
        grid = next(f for f in self._overlay()["features"]
                    if f["properties"]["kind"] == "grid")
        assert grid["properties"]["pattern"] == "lawnmower"
        assert grid["properties"]["lane_spacing_m"] == 67.2
        assert grid["properties"]["swath_m"] == 84.0

    def test_coverage_polygon_is_the_flown_corridor(self):
        cov = next(f for f in self._overlay()["features"]
                   if f["properties"]["kind"] == "coverage")
        assert cov["properties"]["basis"] == "flown"
        assert cov["properties"]["swath_m"] == 84.0
        ring = cov["geometry"]["coordinates"][0]
        assert ring[0] == ring[-1], "a GeoJSON ring must close"
        assert len(ring) >= 8

    def test_overlay_refreshes_on_mission_change_not_on_a_tick(self):
        mcp = full_mcp()
        feed, _ = wired(mcp, flown=[(47.6405, -122.1420), (47.6405, -122.1380)])
        feed.poll_once()
        first = feed.overlay()
        for _ in range(3):
            feed.poll_once()
        assert feed.overlay() is first, "an unchanged mission must not rebuild"
        mcp.current = {**CURRENT, "waypoint": 9}
        feed.poll_once()
        assert feed.overlay() is not first, "a waypoint advance must rebuild"

    def test_geofence_layer_is_the_ENFORCED_polygon(self):
        """The AO drawn on the globe must be the boundary the server gates on.

        `uav://safety/geofence` carries two polygons: `geofence`, which is
        `SafetyEnvelope.to_dict()["geofence"]` and is what every waypoint is
        actually checked against, and a `theater` block that is a LABEL. The
        overlay preferred `theater.ao`.

        On the shipped launcher those two are different theaters — `launch.py`
        builds the envelope from `--theater` but never passes the theater to
        `GodseyeUavServer`, so the resource reports the Natanz envelope beside
        a `theater` block that still says Redmond. Booted at `iran-natanz` the
        bridge drew an AO box in Washington State, ~10,000 km from the
        aircraft, so the operator saw margin where there was none.

        Fails on the old code, which draws the Redmond ring.
        """
        enforced = [[33.705, 51.700], [33.705, 51.760],
                    [33.745, 51.760], [33.745, 51.700]]
        decorative = [[47.636, -122.145], [47.636, -122.135],
                      [47.647, -122.135], [47.647, -122.145]]
        fc = self._overlay(geofence={
            "geofence": enforced,
            "theater": {"id": "default", "label": "Redmond (AirSim default)",
                        "ao": decorative}})
        fence = next(f for f in fc["features"]
                     if f["properties"]["kind"] == "geofence")
        ring = fence["geometry"]["coordinates"][0]
        assert ring[0] == ring[-1], "a GeoJSON ring must close"
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        assert min(lons) > 51.0 and max(lons) < 52.0, (
            f"the drawn AO is at lon {lons[0]}, not the ENFORCED envelope")
        assert min(lats) > 33.0 and max(lats) < 34.0, lats
        assert fence["properties"]["source"] == "envelope.geofence"
        assert fence["properties"]["enforced"] is True
        # ...and the disagreement is reported, never resolved in silence.
        assert any("disagree" in note for note in fc["degraded"]), fc.get("degraded")

    def test_a_theater_ao_fallback_is_labelled_as_not_enforced(self):
        """No envelope polygon -> the theater AO may stand in, but it must not
        pass for the boundary the server gates on."""
        fc = self._overlay(geofence={
            "theater": {"id": "default", "label": "Redmond (AirSim default)",
                        "ao": [[47.636, -122.145], [47.636, -122.135],
                               [47.647, -122.135], [47.647, -122.145]]}})
        fence = next(f for f in fc["features"]
                     if f["properties"]["kind"] == "geofence")
        assert fence["properties"]["source"] == "theater.ao"
        assert fence["properties"]["enforced"] is False
        assert any("does NOT gate" in note for note in fc["degraded"])

    def test_missing_resource_degrades_visibly(self):
        """The overlay must SAY which layer it could not draw."""
        fc = self._overlay(absent={"uav://safety/geofence"}, geofence=None)
        kinds = {f["properties"]["kind"] for f in fc["features"]}
        assert "geofence" not in kinds
        assert any("geofence" in note for note in fc["degraded"])

    def test_empty_overlay_states_its_reason(self, client):
        fc = client.get("/mission-overlay", headers=H).json()
        assert fc["features"] == []
        assert fc["degraded"], "an empty overlay must say why it is empty"

    def test_overlay_served_over_http(self, fed_client):
        fed_client.app.state.bridge.tick_vehicle("Drone1")
        fed_client.app.state.feed.poll_once()
        fc = fed_client.get("/mission-overlay", headers=H).json()
        assert fc["type"] == "FeatureCollection"
        assert {f["properties"]["kind"] for f in fc["features"]} >= {
            "route", "waypoint", "grid", "geofence", "target", "threat_ring"}


class TestOverlayGeometry:
    def test_circle_ring_closes_and_has_the_right_radius(self):
        ring = circle_ring(47.64, -122.14, 1000.0, points=36)
        assert ring[0] == ring[-1]
        assert len(ring) == 37
        lon, lat = ring[0]
        assert bridge_mod._ground_m(47.64, -122.14, lat, lon) == \
            pytest.approx(1000.0, rel=0.02)

    def test_circle_ring_refuses_a_nonpositive_radius(self):
        with pytest.raises(ValueError):
            circle_ring(47.6, -122.1, 0.0)

    def test_corridor_width_matches_the_swath(self):
        path = [(47.6400, -122.1400), (47.6400, -122.1300)]
        ring = corridor_ring(path, 200.0)
        assert ring[0] == ring[-1]
        lats = [p[1] for p in ring]
        span = bridge_mod._ground_m(min(lats), -122.14, max(lats), -122.14)
        assert span == pytest.approx(200.0, rel=0.05)

    def test_a_hover_buffers_to_a_disc_not_a_spike(self):
        ring = corridor_ring([(47.64, -122.14), (47.64, -122.14)], 100.0)
        assert len(ring) > 8
        assert ring[0] == ring[-1]


# ===========================================================================
# BRIDGE_CONTRACT: SSE /events — the alarm lane
# ===========================================================================

class TestAlarmModel:
    def test_every_contract_kind_has_a_severity(self):
        assert set(ALARM_SEVERITY) == {
            "bingo", "geofence_proximity", "geofence_breach", "lost_link",
            "link_restored", "detection", "mission_phase", "datum_degraded"}
        assert ALARM_SEVERITY["bingo"] == "critical"
        assert ALARM_SEVERITY["geofence_proximity"] == "warning"
        assert ALARM_SEVERITY["link_restored"] == "info"

    def test_an_invented_kind_is_refused(self):
        """No silent fallback: an unknown kind must not reach the operator
        wearing a default severity."""
        with pytest.raises(ValueError):
            Alarm(kind="everything_is_fine", message="hi")

    def test_payload_uses_the_contract_field_names(self):
        p = Alarm(kind="bingo", vehicle="Drone1",
                  message="BINGO fuel — forcing RTB").payload()
        assert p["kind"] == "bingo"
        assert p["severity"] == "critical"
        assert p["vehicle"] == "Drone1"
        assert isinstance(p["atMs"], int) and p["atMs"] > 0


class TestEventHub:
    def test_multiple_subscribers_all_receive(self):
        hub = EventHub()
        a, b = hub.subscribe(), hub.subscribe()
        assert hub.publish(Alarm(kind="detection", message="x")) == 2
        assert a.queue.get_nowait()["kind"] == "detection"
        assert b.queue.get_nowait()["kind"] == "detection"

    def test_a_dead_subscriber_never_blocks_the_publisher(self):
        """A stalled browser must cost events, not the telemetry loop."""
        hub = EventHub(max_queue=4)
        slow = hub.subscribe()
        for i in range(50):
            hub.publish(Alarm(kind="detection", message=f"m{i}"))
        assert slow.queue.qsize() == 4
        assert slow.take_dropped() == 46, "drops must be counted, not hidden"

    def test_unsubscribe_stops_delivery(self):
        hub = EventHub()
        sub = hub.subscribe()
        hub.unsubscribe(sub)
        assert hub.publish(Alarm(kind="detection", message="x")) == 0


class TestAlarmDerivation:
    def _drain(self, hub, sub):
        out = []
        while True:
            try:
                out.append(sub.queue.get_nowait())
            except Exception:
                return out

    def test_bingo_fires_once_on_the_latch_edge(self):
        mcp = full_mcp()
        feed, hub = wired(mcp)
        sub = hub.subscribe()
        feed.poll_once()
        assert not [a for a in self._drain(hub, sub) if a["kind"] == "bingo"]
        mcp.tick = FakeMcp.default_tick(
            fuel_pct=24.0,
            bingo={"fuel_pct": 24.0, "bingo_fuel_pct": 25.0, "margin_pct": -1.0,
                   "below_bingo": True, "latched": True})
        feed.poll_once()
        feed.poll_once()
        bingos = [a for a in self._drain(hub, sub) if a["kind"] == "bingo"]
        assert len(bingos) == 1, "the latch edge fires once, not every poll"
        assert bingos[0]["severity"] == "critical"
        assert bingos[0]["vehicle"] == "Drone1"

    def test_geofence_proximity_then_breach(self):
        mcp = full_mcp()
        feed, hub = wired(mcp)
        sub = hub.subscribe()
        feed.poll_once()
        self._drain(hub, sub)
        mcp.tick = FakeMcp.default_tick(violations=[
            {"kind": "geofence_proximity", "severity": "warning",
             "message": "close", "value": 120.0, "limit": 500.0}])
        feed.poll_once()
        mcp.tick = FakeMcp.default_tick(violations=[
            {"kind": "geofence", "severity": "breach", "message": "out",
             "value": -40.0, "limit": 0.0}])
        feed.poll_once()
        got = [a for a in self._drain(hub, sub) if a["kind"].startswith("geofence")]
        assert [a["kind"] for a in got] == ["geofence_proximity", "geofence_breach"]
        assert got[0]["severity"] == "warning"
        assert got[1]["severity"] == "critical"

    def test_lost_link_reads_the_real_link_machine_shape(self):
        """`LostLinkMonitor.to_dict()` reports `state`/`action`, never a `lost`
        boolean. Reading a `lost` key meant the alarm could not fire on real
        data at all - caught only by driving the live stack."""
        mcp = full_mcp()
        feed, hub = wired(mcp)
        sub = hub.subscribe()
        feed.poll_once()          # primes the mission detail cache
        feed.poll_once()
        self._drain(hub, sub)
        mcp.tick = FakeMcp.default_tick(
            link={"state": "loal", "action": "rtb", "down_for_s": 6.0,
                  "plan": {"behaviour": "rtb", "declare_after_s": 5.0}},
            force_rtb=True, rtb_reasons=["lost_link"])
        feed.poll_once()
        mcp.tick = FakeMcp.default_tick(
            link={"state": "up", "action": None, "down_for_s": 0.0})
        feed.poll_once()
        got = [a for a in self._drain(hub, sub)
               if a["kind"] in ("lost_link", "link_restored")]
        assert [a["kind"] for a in got] == ["lost_link", "link_restored"]
        assert got[0]["severity"] == "critical"
        # M9: the alarm must name the lost-link plan that ran.
        assert "rtb" in got[0]["message"]
        assert got[0]["detail"]["lost_link_plan"]["behaviour"] == "rtb"
        assert got[1]["severity"] == "info"

    def test_lost_link_also_fires_from_the_pending_rtb_reason(self):
        mcp = full_mcp()
        feed, hub = wired(mcp)
        sub = hub.subscribe()
        feed.poll_once()
        self._drain(hub, sub)
        mcp.tick = FakeMcp.default_tick(link={"state": "pending"},
                                        rtb_reasons=["lost_link"])
        feed.poll_once()
        assert [a["kind"] for a in self._drain(hub, sub)
                if a["kind"] == "lost_link"] == ["lost_link"]

    def test_detection_fires_once_per_new_track(self):
        mcp = full_mcp()
        feed, hub = wired(mcp)
        sub = hub.subscribe()
        feed.poll_once()
        first = [a for a in self._drain(hub, sub) if a["kind"] == "detection"]
        assert [a["track_id"] for a in first] == ["TRK-ABC-0003"]
        feed.poll_once()
        assert not [a for a in self._drain(hub, sub) if a["kind"] == "detection"]
        mcp.tracks = mcp.tracks + [salute_row("TRK-ABC-0009", 47.6435, -122.1390)]
        feed.poll_once()
        again = [a for a in self._drain(hub, sub) if a["kind"] == "detection"]
        assert [a["track_id"] for a in again] == ["TRK-ABC-0009"]

    def test_mission_phase_fires_on_transition(self):
        mcp = full_mcp()
        feed, hub = wired(mcp)
        sub = hub.subscribe()
        feed.poll_once()
        got = [a for a in self._drain(hub, sub) if a["kind"] == "mission_phase"]
        assert got and got[0]["detail"]["to"] == "executing"
        mcp.current = {**CURRENT, "tool": "uav_return_to_home"}
        feed.poll_once()
        got = [a for a in self._drain(hub, sub) if a["kind"] == "mission_phase"]
        assert [a["detail"]["to"] for a in got] == ["rtb"]

    def test_datum_degraded_fires_from_the_telemetry_path(self):
        feed, hub = wired(full_mcp())
        sub = hub.subscribe()
        feed.note_datum("Drone1", False, "egm96-grid")
        assert not self._drain(hub, sub)
        feed.note_datum("Drone1", True, "coarse-approximation")
        feed.note_datum("Drone1", True, "coarse-approximation")
        got = self._drain(hub, sub)
        assert [a["kind"] for a in got] == ["datum_degraded"]
        assert got[0]["severity"] == "warning"
        assert "coarse-approximation" in got[0]["message"]

    def test_all_eight_kinds_are_reachable(self):
        """PLAN §3.1 demands ≥3 alarm types demonstrated; the contract lists 8."""
        mcp = full_mcp()
        feed, hub = wired(mcp)
        sub = hub.subscribe()
        seen = set()
        feed.poll_once()                                     # detection+phase
        feed.poll_once()
        mcp.tick = FakeMcp.default_tick(violations=[
            {"kind": "geofence_proximity", "value": 90.0, "limit": 500.0}])
        feed.poll_once()                                     # proximity
        mcp.tick = FakeMcp.default_tick(violations=[
            {"kind": "geofence", "value": -5.0, "limit": 0.0}])
        feed.poll_once()                                     # breach
        mcp.tick = FakeMcp.default_tick(link={"state": "loal", "action": "rtb"})
        feed.poll_once()                                     # lost_link
        mcp.tick = FakeMcp.default_tick(link={"state": "up"})
        feed.poll_once()                                     # link_restored
        mcp.tick = FakeMcp.default_tick(
            bingo={"bingo_fuel_pct": 25.0, "latched": True})
        mcp.current = {**CURRENT, "tool": "uav_return_to_home"}
        feed.poll_once()                                     # bingo + rtb phase
        feed.note_datum("Drone1", True, "coarse")            # datum_degraded
        for a in self._drain(hub, sub):
            seen.add(a["kind"])
        assert seen == set(ALARM_SEVERITY), sorted(set(ALARM_SEVERITY) - seen)


class _FakeRequest:
    """A Request stub whose client hangs up after `alive` polls.

    `on_poll(n)` runs inside the generator's own loop, which is how a test
    publishes AFTER the stream has subscribed - a late subscriber getting no
    replay of older alarms is correct SSE behaviour, not a bug.
    """

    def __init__(self, alive=4, on_poll=None):
        self._left = alive
        self._on_poll = on_poll
        self._n = 0

    async def is_disconnected(self):
        if self._on_poll is not None:
            self._on_poll(self._n)
        self._n += 1
        self._left -= 1
        return self._left < 0


def drain_stream(hub, *, alive=4, heartbeat_s=0.0, poll_s=0.0, on_poll=None):
    async def run():
        out = []
        async for chunk in bridge_mod.event_stream(
                _FakeRequest(alive, on_poll), hub,
                heartbeat_s=heartbeat_s, poll_s=poll_s):
            out.append(chunk)
        return out
    return asyncio.run(run())


class TestSseGenerator:
    def test_preamble_carries_a_retry_hint(self):
        chunks = drain_stream(EventHub(), alive=1)
        assert chunks[0].startswith(": ")
        assert any(c.startswith("retry:") for c in chunks)

    def test_heartbeat_keeps_an_idle_stream_open(self):
        """A comment heartbeat is what stops an idle proxy dropping the feed."""
        chunks = drain_stream(EventHub(), alive=4, heartbeat_s=0.0)
        pings = [c for c in chunks if c.startswith(": ping")]
        assert pings, chunks
        assert all(c.endswith("\n\n") for c in pings)

    def test_an_alarm_is_framed_as_the_contract_spells_it(self):
        hub = EventHub()

        def publish(n):
            if n == 0:
                hub.publish(Alarm(kind="bingo", vehicle="Drone1",
                                  message="BINGO fuel - forcing RTB"))

        frame = next(c for c in drain_stream(hub, alive=3, on_poll=publish)
                     if c.startswith("event: alarm"))
        head, body = frame.split("\n", 1)
        assert head == "event: alarm"
        assert frame.endswith("\n\n"), "an SSE frame ends with a blank line"
        data = json.loads(body.split("data:", 1)[1].strip())
        assert data == {"kind": "bingo", "severity": "critical",
                        "vehicle": "Drone1",
                        "message": "BINGO fuel - forcing RTB",
                        "atMs": data["atMs"]}

    def test_dropped_events_are_announced_not_hidden(self):
        """A slow reader loses events - it must never lose them QUIETLY."""
        hub = EventHub(max_queue=2)

        def flood(n):
            if n == 0:
                for i in range(10):
                    hub.publish(Alarm(kind="detection", message=f"m{i}"))

        frames = drain_stream(hub, alive=2, on_poll=flood)
        payloads = [json.loads(f.split("data:", 1)[1].strip())
                    for f in frames if f.startswith("event: alarm")]
        assert len(payloads) == 2, frames
        assert payloads[0]["detail"]["dropped_since_last"] == 8

    def test_disconnect_releases_the_slot(self):
        hub = EventHub()
        assert hub.subscriber_count == 0
        drain_stream(hub, alive=2)
        assert hub.subscriber_count == 0, "a closed stream must release its slot"

    def test_publishing_with_no_subscribers_is_harmless(self, client):
        hub = client.app.state.hub
        assert hub.publish(Alarm(kind="detection", message="nobody home")) == 0
        assert hub.recent()[-1]["kind"] == "detection"

    def test_health_reports_the_alarm_lane(self, client):
        ev = client.get("/health").json()["events"]
        assert sorted(ev["kinds"]) == sorted(ALARM_SEVERITY)
        assert ev["subscribers"] == 0


class TestSseEndpointAuth:
    def test_events_requires_auth(self, client):
        assert client.get("/events").status_code == 401

    def test_events_rejects_a_wrong_query_token(self, client):
        assert client.get("/events?token=nope").status_code == 401


# ---------------------------------------------------------------------------
# real HTTP: an SSE endpoint that has only ever been called in-process is not
# proven. `TestClient` cannot hold an open stream (its portal blocks on a
# generator that is still alive), so these run against a real uvicorn server.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def live(sim):
    import uvicorn

    port = _free_port(LIVE_PORTS)
    adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
    app = create_app(adapter=adapter, token=TOKEN, start_loops=False,
                     heartbeat_s=0.4)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 15.0
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not come up"
    yield app, f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(timeout=10)


def read_sse(url, *, want, timeout=12.0, headers=None):
    """Read an SSE stream until a COMPLETE frame containing `want` arrives.

    SSE frames are terminated by a blank line, so stopping at the first line
    that matches would cut a `data:` payload off its `event:` header.
    """
    import httpx

    buf = ""
    deadline = time.time() + timeout
    with httpx.stream("GET", url, headers=headers or {},
                      timeout=httpx.Timeout(timeout)) as r:
        assert r.status_code == 200, r.status_code
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            buf += line + "\n"
            if want in buf and buf.endswith("\n\n"):
                return buf
            if time.time() > deadline:
                break
    pytest.fail(f"{want!r} never arrived complete; got {buf!r}")


class TestSseOverRealHttp:
    def test_eventsource_style_query_token_is_accepted(self, live):
        """`EventSource` cannot set an Authorization header, which is why the
        GEV client sends `?token=`. A 401/404 here means no alarms at all."""
        _app, base = live
        body = read_sse(f"{base}/events?token={TOKEN}", want="retry:")
        assert "retry:" in body

    def test_heartbeat_arrives_over_the_wire(self, live):
        _app, base = live
        assert ": ping" in read_sse(f"{base}/events", want=": ping", headers=H)

    def test_an_alarm_reaches_a_live_browser(self, live):
        app, base = live
        hub = app.state.hub

        def publish_soon():
            time.sleep(0.6)
            hub.publish(Alarm(kind="bingo", vehicle="Drone1",
                              message="BINGO fuel - forcing RTB"))

        threading.Thread(target=publish_soon, daemon=True).start()
        body = read_sse(f"{base}/events", want="event: alarm", headers=H)
        data = json.loads(body.split("event: alarm", 1)[1]
                          .split("data:", 1)[1].split("\n", 1)[0])
        assert data["kind"] == "bingo"
        assert data["severity"] == "critical"
        assert data["vehicle"] == "Drone1"

    def test_two_browsers_both_get_the_alarm(self, live):
        """Multiple concurrent subscribers, and neither blocks the other."""
        app, base = live
        hub = app.state.hub
        got: list[str] = []
        errs: list[BaseException] = []

        def reader():
            try:
                body = read_sse(f"{base}/events", want="event: alarm", headers=H)
                got.append(body)
            except BaseException as e:
                errs.append(e)

        threads = [threading.Thread(target=reader, daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        deadline = time.time() + 6.0
        while hub.subscriber_count < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert hub.subscriber_count == 2, hub.subscriber_count
        hub.publish(Alarm(kind="geofence_proximity", vehicle="Drone1",
                          message="geofence proximity - 310 m to the AO edge"))
        for t in threads:
            t.join(timeout=12)
        assert not errs, errs
        assert len(got) == 2
        assert all("geofence_proximity" in b for b in got)

    def test_client_disconnect_releases_the_subscriber(self, live):
        app, base = live
        hub = app.state.hub
        read_sse(f"{base}/events", want="retry:", headers=H)
        deadline = time.time() + 8.0
        while hub.subscriber_count and time.time() < deadline:
            time.sleep(0.1)
        assert hub.subscriber_count == 0, "a hung-up browser must free its slot"

    def test_snapshot_still_answers_while_a_stream_is_open(self, live):
        """A held-open SSE connection must not stall the telemetry poll."""
        import httpx

        app, base = live
        app.state.bridge.tick_vehicle("Drone1")
        with httpx.stream("GET", f"{base}/events", headers=H,
                          timeout=httpx.Timeout(10.0)) as r:
            next(r.iter_lines())
            t0 = time.monotonic()
            body = httpx.get(f"{base}/snapshot", headers=H, timeout=5.0)
            assert body.status_code == 200
            assert time.monotonic() - t0 < 2.0
            assert body.json()["count"] >= 1


# ===========================================================================
# BRIDGE_CONTRACT rule 3: fail visibly
# ===========================================================================

class _BrokenGeoidAdapter(AirSimAdapter):
    """An adapter whose geoid will not load — the exact case bridge.py:139's
    bare `except Exception` turned into silence."""

    def altitude_fix(self, alt_m, lat, lon, datum="hae"):
        from godseye_uav.geo import canonical_altitude

        fix = canonical_altitude(alt_m, lat, lon, datum=datum,
                                 allow_approx=True)
        object.__setattr__(fix, "degraded", True)
        self.datum_degraded = True
        self.datum_source = "coarse-approximation"
        return type(fix)(fix.alt_hae, fix.alt_msl, fix.undulation_m,
                         "coarse-approximation", True)


class TestMcpToolErrorsAreNotSwallowed:
    """A tool that FAILS must not read as a healthy feed.

    MCP reports a tool failure as `{"isError": true, "content":[{"text": ...}]}`
    inside a SUCCESSFUL JSON-RPC response. The bridge's client only ever
    noticed the server's own structured `{"error": {...}}` envelope, so a
    raising tool — or one called with arguments that do not validate — came
    back as `({"text": "Error executing tool ..."}, None)`: `poll_once` then
    built a mission row out of prose and `/snapshot.feeds` reported `ok`.

    This is not hypothetical: `uav_goto_gps` takes `alt_m`, and a call written
    against TOOL_CONTRACT's `alt_agl_m` fails validation exactly this way on
    the live server.
    """

    @staticmethod
    def _client(result):
        c = McpClient("http://127.0.0.1:1/mcp", "t")
        c.rpc = lambda method, params: ({"jsonrpc": "2.0", "id": 1,
                                         "result": result}, None)
        return c

    def test_is_error_becomes_an_error_not_a_payload(self):
        out, err = self._client({
            "content": [{"type": "text",
                         "text": "Error executing tool uav_goto_gps: 1 "
                                 "validation error for uav_goto_gpsArguments\n"
                                 "alt_m\n  Field required"}],
            "isError": True,
        }).call_tool("uav_goto_gps", {"vehicle": "Drone1"})
        assert out is None, "a failing tool must not hand back a payload"
        assert err and "uav_goto_gps" in err
        assert "tool error" in err and "validation error" in err

    def test_a_healthy_tool_still_parses(self):
        out, err = self._client({
            "content": [{"type": "text", "text": '{"count": 0, "tracks": []}'}],
            "isError": False,
        }).call_tool("uav_list_tracks", {})
        assert err is None
        assert out == {"count": 0, "tracks": []}

    def test_a_failing_tool_marks_the_feed_not_ok(self):
        """The end-to-end consequence: `/snapshot.feeds` must say it failed."""
        class Failing(FakeMcp):
            def call_tool(self, name, arguments):
                if name == "uav_task_status":
                    return McpClient.call_tool(
                        TestMcpToolErrorsAreNotSwallowed._client({
                            "content": [{"type": "text",
                                         "text": "Error executing tool "
                                                 "uav_task_status: boom"}],
                            "isError": True}),
                        name, arguments)
                return super().call_tool(name, arguments)

        feed, _ = wired(Failing(current=CURRENT, mission_state=MISSION_STATE,
                                mission_doc=MISSION_DOC, geofence=GEOFENCE,
                                threat=THREATREP, tracks=[]))
        intel = feed.poll_once()
        assert intel.missions == []
        assert intel.feeds["mission_state"].ok is False, (
            "a tool that raised was reported as a healthy feed")
        assert "boom" in (intel.feeds["mission_state"].error or "")

    def test_the_control_proxy_does_not_return_an_error_as_success(self, client):
        """`/control/command` must not hand the panel prose with no error key."""
        app_mcp = client.app.state.mcp
        app_mcp.rpc = lambda method, params: (
            {"jsonrpc": "2.0", "id": 1,
             "result": {"content": [{"type": "text",
                                     "text": "Error executing tool uav_hover: boom"}],
                        "isError": True}}, None)
        body = client.post("/control/command", headers=H,
                           json={"tool": "uav_hover", "vehicle": "Drone1"}).json()
        assert body.get("isError") is True, body
        assert "uav_hover" in body.get("error", ""), body


class TestFailVisibly:
    def test_the_real_fallback_path_labels_itself_degraded(self, sim, monkeypatch):
        """The accurate EGM96 source refuses -> the coarse fallback is taken.

        The reading must be labelled degraded from the CONTROL FLOW. Taking the
        label from what the fallback fix reports about itself puts an
        unlabelled approximation on the operator's HUD — which is the whole
        failure mode T1 exists to prevent.
        """
        from godseye_uav import geo

        real = geo.canonical_altitude

        def refuse_the_accurate_source(alt, lat, lon, *, datum="msl",
                                       allow_approx=False):
            if not allow_approx:
                raise geo.GeoidUnavailableError("EGM96 unavailable (simulated)")
            # Deliberately hands back a fix that claims it is NOT degraded.
            return real(alt, lat, lon, datum=datum, allow_approx=True)

        monkeypatch.setattr(bridge_mod, "canonical_altitude",
                            refuse_the_accurate_source)
        adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
        snap = adapter.snapshot("Drone1")
        assert snap is not None, "a degraded datum must not blank the vehicle"
        assert snap.datum_degraded is True
        assert adapter.datum_degraded is True
        assert adapter.sim_state.startswith("up")
        assert "datum_degraded" in adapter.sim_state

    def test_a_degraded_datum_does_not_blank_telemetry(self, sim):
        """Old behaviour: the geoid raises -> bare except -> snapshot() returns
        None -> every vehicle disappears with no trace. New: it still flies,
        and every channel says the datum is degraded."""
        adapter = _BrokenGeoidAdapter(ip="127.0.0.1", port=PORT)
        app = create_app(adapter=adapter, token=TOKEN, start_loops=False)
        with TestClient(app) as tc:
            hub = app.state.hub
            sub = hub.subscribe()
            app.state.bridge.tick_vehicle("Drone1")
            body = tc.get("/snapshot", headers=H).json()
            assert body["count"] == 1, "telemetry must not be blanked"
            veh = body["vehicles"][0]
            assert veh["datum_degraded"] is True
            assert veh["datum_source"] == "coarse-approximation"
            assert body["sim_state"].startswith("up")
            assert "datum_degraded" in body["sim_state"]
            assert tc.get("/health").json()["datum_degraded"] is True
            assert sub.queue.get_nowait()["kind"] == "datum_degraded"

    def test_a_snapshot_failure_marks_the_row_stale_with_the_error(self, sim):
        adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
        app = create_app(adapter=adapter, token=TOKEN, start_loops=False)
        with TestClient(app) as tc:
            state = app.state.bridge
            state.tick_vehicle("Drone1")
            assert tc.get("/snapshot", headers=H).json()["count"] == 1

            def boom(name):
                adapter.last_error = "RuntimeError: rpc died"

            adapter.snapshot = boom
            time.sleep(0.02)
            state.tick_vehicle("Drone1")
            veh = tc.get("/snapshot", headers=H).json()["vehicles"][0]
            assert veh["telemetry_error"] == "RuntimeError: rpc died"
            assert veh["stale_ms"] > 0, "a stale fix must announce its age"

    def test_loop_c_failure_keeps_the_last_picture_and_says_so(self):
        mcp = full_mcp()
        feed, _ = wired(mcp)
        feed.poll_once()
        assert feed.intel().missions

        def explode():
            raise RuntimeError("transport gone")

        feed.poll_once = explode
        t = threading.Thread(target=feed.loop, kwargs={"hz": 40}, daemon=True)
        t.start()
        deadline = time.time() + 5.0
        while "loop_c" not in feed.intel().feeds and time.time() < deadline:
            time.sleep(0.02)
        feed.stop()
        assert feed.intel().missions, "a failed poll must not blank the picture"
        assert feed.intel().feeds["loop_c"].ok is False
        assert "transport gone" in feed.intel().feeds["loop_c"].error

    def test_an_unpublishable_alarm_is_counted_not_swallowed(self):
        """A BINGO warning the operator never sees is the worst possible bug,
        so a failed publish is surfaced on the feed rather than dropped."""
        mcp = full_mcp()
        feed, hub = wired(mcp)

        def broken(_alarm):
            raise RuntimeError("hub exploded")

        hub.publish = broken
        intel = feed.poll_once()
        assert feed.emit_failures > 0
        assert "could not be published" in intel.feeds["mission_state"].detail
        assert "hub exploded" in intel.feeds["mission_state"].detail

    def test_an_assumed_vehicle_roster_says_it_is_an_assumption(self, sim):
        """`listVehicles` failing fell back to ['Drone1'] indistinguishably
        from a real roster of one. The guess must be labelled."""
        adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
        assert adapter.vehicles() == ["Drone1"]
        assert adapter.vehicles_fallback is None

        client_obj = adapter._ensure()

        def no_such_method():
            raise AttributeError("listVehicles")

        client_obj.listVehicles = no_such_method
        assert adapter.vehicles() == ["Drone1"]
        assert adapter.vehicles_fallback
        assert "listVehicles unavailable" in adapter.vehicles_fallback

    def test_a_dark_pip_says_why(self, sim):
        adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
        app = create_app(adapter=adapter, token=TOKEN, start_loops=False)
        with TestClient(app) as tc:
            def no_images(*a, **k):
                raise RuntimeError("sensor offline")

            adapter._ensure().simGetImages = no_images
            r = tc.get("/camera/Drone1?type=3", headers=H)
            assert r.status_code == 503
            assert "sensor offline" in r.json()["detail"], r.json()
            assert "sensor offline" in tc.get("/health").json()["camera_error"]


# ===========================================================================
# read-only guarantee (BRIDGE_CONTRACT rule 1 / GEV rule 5)
# ===========================================================================

class TestReadOnly:
    def test_no_feed_route_mutates(self, client):
        """GEV is read-only for flight control: every feed added in Wave 3 is
        GET-only, and nothing but /control/* can reach an MCP tool."""
        feeds = ["/snapshot", "/snapshot/Drone1", "/mission-overlay",
                 "/theaters", "/events", "/tracks", "/camera/Drone1"]
        routes = {r.path: r.methods for r in client.app.routes
                  if hasattr(r, "methods")}
        for path in feeds:
            template = path if path in routes else {
                "/snapshot/Drone1": "/snapshot/{name}",
                "/camera/Drone1": "/camera/{veh}"}[path]
            assert routes[template] <= {"GET", "HEAD"}, (template, routes[template])

    def test_control_routes_are_the_only_post(self, client):
        posts = {r.path for r in client.app.routes
                 if hasattr(r, "methods") and "POST" in r.methods}
        assert posts == {"/control/mission", "/control/command",
                         "/camera/subscribe"}


# ===========================================================================
# AGL: the operator must be shown the number the harness flies on
# (REAL_DATA_INTEGRATION.md; BRIDGE_CONTRACT vehicles[] / "fail visibly")
#
# The MCP server measures `alt_agl_m` against real terrain. `bridge.py` used to
# compute its own `max(0.0, -ned.z)` — height above the HOME PLANE — and THAT
# was the number reaching /snapshot, the GEV HUD and the operator. Over the
# Fordow ridge the two disagree by 82 m and in opposite signs: the server knows
# the aircraft is 42.2 m BELOW the crest while the HUD reads a comfortable
# +39.9 m. Two surfaces publishing different answers for one safety-relevant
# quantity is the defect class that started this effort (the altHae/geoid
# split), so the fix is the same: ONE source of truth, with provenance.
# ===========================================================================

def _terrain_tick(**over):
    """A tick as the MCP server writes one when it MEASURED AGL.

    `alt_agl_m` is the Fordow figure from REAL_DATA_INTEGRATION.md. The
    launch-datum figure is 0.0 because the fixture's aircraft is parked at the
    NED origin, which is what this bridge's own number reads — the frame
    cross-check compares those two, and it is exercised on its own below.
    """
    fields = {"alt_agl_m": -42.2, "alt_agl_is_real": True,
              "alt_agl_source": "terrain:gev", "alt_agl_launch_datum_m": 0.0}
    fields.update(over)
    return FakeMcp.default_tick(**fields)


def _agl_rows(tick):
    """A bridge on a fake MCP transport serving `tick`, polled and ticked once.

    Returns the vehicle row as BOTH feeds publish it: `/snapshot.vehicles[0]`
    and `/snapshot/{name}`.
    """
    adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
    app = create_app(adapter=adapter, token=TOKEN, start_loops=False)
    feed = MissionFeed(full_mcp(tick=tick), app.state.hub,
                       vehicles=adapter.vehicles, flown=app.state.bridge.flown)
    app.state.bridge.feed = feed
    app.state.feed = feed
    with TestClient(app) as tc:
        feed.poll_once()
        app.state.bridge.tick_vehicle("Drone1")
        return (tc.get("/snapshot", headers=H).json()["vehicles"][0],
                tc.get("/snapshot/Drone1", headers=H).json())


class TestMeasuredAgl:
    def test_snapshot_serves_the_servers_measured_agl_not_the_home_plane(self, sim):
        """THE defect: /snapshot published the bridge's own launch-datum AGL.

        FAILS ON THE OLD CODE: `agl` was `max(0.0, -ned.z)` = 0.0 for this
        parked aircraft, while the server — the authority, the thing the
        harness flies on — measured -42.2 m against the terrain under it.
        """
        veh, one = _agl_rows(_terrain_tick())
        assert veh["agl"] == pytest.approx(-42.2), (
            "the HUD is still being served the bridge's own home-plane number")
        # Both spellings, one writer: they can never disagree.
        assert veh["alt_agl_m"] == veh["agl"]
        # ...and the operator can see it is MEASURED, not assumed.
        assert veh["alt_agl_is_real"] is True
        assert veh["alt_agl_source"] == "terrain:gev"
        assert veh["alt_agl_reason"] is None
        # The old number is kept alongside, not thrown away.
        assert veh["alt_agl_launch_datum_m"] == pytest.approx(0.0, abs=0.5)
        assert veh["alt_agl_launch_datum_mismatch_m"] is None
        # /snapshot/{name} serves the same row, not a second opinion.
        assert one["agl"] == veh["agl"]
        assert one["alt_agl_source"] == veh["alt_agl_source"]

    def test_an_aircraft_below_its_launch_datum_is_not_floored_to_zero(
            self, client, sim):
        """`max(0.0, ...)` was itself a silent floor: an aircraft BELOW the
        plane it is measured against read 0 m AGL — hiding precisely the
        situation the operator most needs to see.

        FAILS ON THE OLD CODE: 12 m below the home plane reported as 0.0.
        """
        from godseye_uav.geo import NedPoint

        veh_obj = sim._vehicles["Drone1"]
        was = veh_obj.ned
        try:
            veh_obj.ned = NedPoint(0.0, 0.0, 12.0)  # NED z is DOWN-positive
            snap = client.app.state.bridge.adapter.snapshot("Drone1")
            assert snap is not None
            assert snap.agl == pytest.approx(-12.0, abs=0.1)
            assert snap.alt_agl_launch_datum_m == pytest.approx(-12.0, abs=0.1)
            client.app.state.bridge.tick_vehicle("Drone1")
            row = client.get("/snapshot", headers=H).json()["vehicles"][0]
            assert row["agl"] == pytest.approx(-12.0, abs=0.1)
        finally:
            veh_obj.ned = was

    def test_no_measured_agl_keeps_the_launch_datum_and_says_why(self, sim):
        """A tick with no AGL in it must degrade VISIBLY: the launch datum,
        labelled as the launch datum, with the reason on the row."""
        veh, _one = _agl_rows(FakeMcp.default_tick())
        assert veh["agl"] == pytest.approx(0.0, abs=0.5)
        assert veh["alt_agl_is_real"] is False
        assert veh["alt_agl_source"] == AGL_SOURCE_BRIDGE_LAUNCH
        assert "no alt_agl_m" in veh["alt_agl_reason"]
        assert "LAUNCH DATUM" in veh["alt_agl_reason"]

    def test_an_agl_with_no_provenance_is_not_published_as_measured(self, sim):
        """`tick.get("alt_agl_is_real", False)` would have quietly labelled a
        measured value "assumed"; the `True` default would have done the far
        worse opposite. An unstated provenance is a refusal, with a reason."""
        tick = FakeMcp.default_tick(alt_agl_m=-42.2)  # no alt_agl_is_real
        veh, _one = _agl_rows(tick)
        assert veh["alt_agl_is_real"] is False
        assert veh["agl"] == pytest.approx(0.0, abs=0.5), (
            "an unlabelled altitude was adopted as the HUD's AGL")
        assert "no alt_agl_is_real" in veh["alt_agl_reason"]

    def test_a_tick_with_no_telemetry_says_that_is_why(self, sim):
        """The server writes a tick with no altitude at all when the link is
        down. The row must name that, not merely go quiet."""
        tick = FakeMcp.default_tick(telemetry=False,
                                    telemetry_error="TimeoutError: rpc")
        veh, _one = _agl_rows(tick)
        assert veh["alt_agl_is_real"] is False
        assert "no telemetry" in veh["alt_agl_reason"]
        assert "TimeoutError: rpc" in veh["alt_agl_reason"]

    def test_an_unmeasured_server_agl_is_still_the_one_published(self, sim):
        """One source of truth means ALWAYS the server's number — including
        when the server says it is NOT measured. Its reason rides along, so the
        HUD can say "assumed" instead of implying terrain was consulted."""
        tick = _terrain_tick(
            alt_agl_m=3.5, alt_agl_is_real=False,
            alt_agl_source="synthetic:launch-datum",
            alt_agl_launch_datum_m=3.5,
            alt_agl_reason="the real-world data layer is OFF on this server")
        veh, _one = _agl_rows(tick)
        assert veh["agl"] == pytest.approx(3.5)
        assert veh["alt_agl_is_real"] is False
        assert veh["alt_agl_source"] == "synthetic:launch-datum"
        assert veh["alt_agl_reason"] == (
            "the real-world data layer is OFF on this server")
        assert veh["alt_agl_launch_datum_mismatch_m"] is None

    def test_a_server_flying_a_different_frame_is_reported(self, sim):
        """Republishing the server's AGL is only sound while both processes
        share the NED origin. When the server's own launch-datum AGL disagrees
        with this bridge's, they were handed different homes — and the measured
        number does not belong to the aircraft being drawn. Say so."""
        veh, _one = _agl_rows(_terrain_tick(alt_agl_launch_datum_m=300.0))
        assert veh["alt_agl_launch_datum_mismatch_m"] == pytest.approx(
            300.0, abs=0.5)
        assert abs(veh["alt_agl_launch_datum_mismatch_m"]) > AGL_FRAME_TOLERANCE_M
        # The measured value is still served — flagged, never swallowed.
        assert veh["agl"] == pytest.approx(-42.2)
        assert veh["alt_agl_is_real"] is True

    def test_the_measured_agl_carries_its_age(self, sim):
        """Loop A runs at 10 Hz and loop C at 2 Hz, so the measured AGL is
        always a little older than the position beside it. A stalled MCP feed
        has to show as AGE, not as a number that quietly stops moving."""
        adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
        app = create_app(adapter=adapter, token=TOKEN, start_loops=False)
        feed = MissionFeed(full_mcp(tick=_terrain_tick()), app.state.hub,
                           vehicles=adapter.vehicles,
                           flown=app.state.bridge.flown)
        app.state.bridge.feed = feed
        app.state.feed = feed
        with TestClient(app) as tc:
            feed.poll_once()
            app.state.bridge.tick_vehicle("Drone1")
            first = tc.get("/snapshot", headers=H).json()["vehicles"][0]
            assert first["alt_agl_at_ms"] == feed.intel().at_ms
            assert 0 <= first["alt_agl_age_ms"] < 2000
            # loop C stops; loop A keeps running. The AGL must visibly age.
            time.sleep(0.12)
            app.state.bridge.tick_vehicle("Drone1")
            later = tc.get("/snapshot", headers=H).json()["vehicles"][0]
            assert later["agl"] == first["agl"]
            assert later["alt_agl_age_ms"] >= first["alt_agl_age_ms"] + 90

    def test_an_unwired_bridge_says_it_has_no_measured_agl(self, client):
        """No mission feed at all: the row still flies, still carries the
        launch datum, and names why the number is not a measured one."""
        state = client.app.state.bridge
        state.feed = None
        snap = state.tick_vehicle("Drone1")
        assert snap is not None, "an unwired feed must not blank telemetry"
        assert snap.alt_agl_is_real is False
        assert "no mission feed" in snap.alt_agl_reason
        assert "LAUNCH DATUM" in snap.alt_agl_reason
        assert isinstance(snap.agl, float)
        assert snap.alt_agl_launch_datum_check.startswith("not checked")

    def test_a_non_finite_agl_is_refused_by_name(self, sim):
        """A NaN altitude renders as a plausible gauge and compares False
        against every threshold, so it must be refused BY NAME, not passed
        through and not quietly turned into something else.

        FAILS ON `value = float(tick["alt_agl_m"])`: NaN is a float, so it was
        adopted and `agl` came back NaN behind `alt_agl_is_real: true`.
        """
        veh, _one = _agl_rows(_terrain_tick(alt_agl_m=float("nan")))
        assert veh["agl"] == pytest.approx(0.0, abs=0.5), (
            f"a NaN reached the HUD as {veh['agl']!r}")
        assert veh["agl"] == veh["agl"], "the HUD's agl is NaN"
        assert veh["alt_agl_is_real"] is False
        assert "not a finite number" in veh["alt_agl_reason"]
        assert "nan" in veh["alt_agl_reason"].lower()

    def test_an_agl_that_is_not_measured_cannot_be_written_without_a_reason(self):
        """The guard that keeps an unlabelled altitude off the HUD. Asserting
        it at the writer is cheaper than finding it on an operator's screen.

        FAILS ON THE OLD CODE: `set_agl` took the write and the row published a
        synthetic AGL with `alt_agl_reason: None` — indistinguishable from a
        terrain-measured one.
        """
        snap = _bare_snapshot()
        with pytest.raises(ValueError, match="must say why"):
            snap.set_agl(3.5, is_real=False, source="synthetic:launch-datum",
                         reason=None, measured_age_source="x")
        assert snap.agl == pytest.approx(40.0), "the bad write went through"

    def test_a_state_source_row_with_no_provenance_is_not_adopted(self, sim):
        """`create_app(state_source=...)` is a documented injection point, so
        `_enrich_agl` cannot assume its row came from `_agl_from_tick`. A row
        carrying an `alt_agl_m` with no boolean `alt_agl_is_real` is refused
        there too — the check is not redundant, it is the only one on this path.

        FAILS ON THE OLD CODE: the second refusal was absent and the unlabelled
        -42.2 became the HUD's AGL.
        """
        veh = _row_from_state_source(
            {"alt_agl_m": -42.2, "alt_agl_source": "terrain:gev",
             "alt_agl_launch_datum_m": 0.0})
        assert veh["alt_agl_is_real"] is False
        assert veh["agl"] == pytest.approx(0.0, abs=0.5), (
            "an unlabelled altitude was adopted as the HUD's AGL")
        assert "no alt_agl_is_real" in veh["alt_agl_reason"]


def _bare_snapshot(**over):
    """A VehicleSnapshot at 40 m over its launch datum, nothing enriched."""
    fields = {
        "name": "Drone1", "latitude": 34.8765, "longitude": 50.9958,
        "alt_hae": 943.0, "alt_msl": 943.0, "agl": 40.0, "alt_agl_m": 40.0,
        "alt_agl_launch_datum_m": 40.0, "speed_ms": 0.0, "heading_deg": 0.0,
        "vx": 0.0, "vy": 0.0, "vz": 0.0, "landed_state": 1, "armed": True,
        "timestamp_ms": 0}
    fields.update(over)
    return VehicleSnapshot(**fields)


def _climbing_snapshot(*, served_launch_datum_m, vz=-12.0, age_ms=1000):
    """`BridgeState.enrich`'s AGL fold, run on an aircraft with a real vertical
    rate, against a row shaped exactly as `MissionFeed._vehicle_state` builds
    one. The fake sim's integrator owns `vel` (it recomputes it from the task
    every step), so a climb is staged on the snapshot rather than on the sim.
    """
    from godseye_uav.bridge import BridgeState, _agl_from_tick

    snap = _bare_snapshot(vz=vz)
    row = dict(_agl_from_tick(
        _terrain_tick(alt_agl_launch_datum_m=served_launch_datum_m)))
    row["alt_agl_measured_age_ms"] = age_ms
    row["alt_agl_measured_age_source"] = "test: staged measurement age"
    BridgeState._enrich_agl(snap, row, at_ms=1_000_000, now_ms=1_000_100)
    return snap


def _row_from_state_source(row, *, vehicle="Drone1", at_ms=None):
    """`/snapshot.vehicles[0]` for a bridge whose loop-C state source is an
    injected MissionFeed-shaped object publishing `row` verbatim.

    This is the seam `create_app(state_source=...)` documents, and the only way
    to reach `_enrich_agl`'s own defences: a row from `_agl_from_tick` has
    already been through the identical checks upstream.
    """
    stamp = int(time.time() * 1000) if at_ms is None else at_ms

    class Source:
        polls = 0

        def intel(self):
            return MissionIntel(per_vehicle={vehicle: dict(row)}, at_ms=stamp)

        def overlay(self):
            return {"type": "FeatureCollection", "features": []}

        def note_datum(self, *_a):
            pass

        def poll_once(self):
            return self.intel()

        def loop(self, **_kw):
            pass

        def stop(self):
            pass

    app = create_app(adapter=AirSimAdapter(ip="127.0.0.1", port=PORT),
                     token=TOKEN, start_loops=False, state_source=Source())
    with TestClient(app) as tc:
        app.state.bridge.tick_vehicle(vehicle)
        return tc.get("/snapshot", headers=H).json()["vehicles"][0]


# ===========================================================================
# HOW OLD IS THAT AGL, AND IS IT EVEN THIS AIRCRAFT'S?
#
# Republishing the MCP server's AGL removed one defect (two surfaces, two
# numbers) and opened two more, both found by RUNNING the stack — a real MCP
# server over Streamable HTTP, the real bridge, one shared NED origin:
#
#  1. `alt_agl_launch_datum_mismatch_m: -39.925` on a stack whose two processes
#     shared a home exactly. The frame cross-check compares two heights sampled
#     at DIFFERENT MOMENTS (the server's 0.5 s monitor tick against loop A's
#     10 Hz), so an aircraft that is merely climbing trips it. Every takeoff and
#     every RTB let-down would raise a "different frames" alarm, and an alarm
#     that cries wolf on every descent is one nobody reads when it matters.
#
#  2. `agl: -0.3, alt_agl_is_real: true, alt_agl_reason: null,
#     alt_agl_age_ms: 63` — for an aircraft that had since flown 600 m and
#     descended 150 m. The server's monitor loop had stopped; `uav_task_status`
#     kept answering with the SAME `last_tick`, so every poll succeeded and the
#     age — which times the POLL — stayed at 63 ms while the number it labelled
#     was frozen. That is exactly the "number that quietly stops moving" the
#     age field was added to prevent.
#
# Plus the silent third: a cross-check that could not run published the same
# `mismatch_m: None` as one that ran and passed.
# ===========================================================================


class TestAglFreshnessAndFrame:
    def test_a_frame_check_that_could_not_run_does_not_read_as_agreement(self):
        """`mismatch_m: None` meant BOTH "compared, they agree" and "never
        compared". A gate that has not run must never wear the face of one that
        passed — that is the shape of the safety gate that read the wrong
        altitude key and passed everything.

        FAILS ON THE OLD CODE: the row published `alt_agl_launch_datum_mismatch_m:
        None` and nothing else, so no consumer could tell the two apart.
        """
        veh = _row_from_state_source(
            {"alt_agl_m": -42.2, "alt_agl_is_real": True,
             "alt_agl_source": "terrain:gev"})  # no launch-datum figure at all
        assert veh["agl"] == pytest.approx(-42.2), "the measured AGL is still served"
        assert veh["alt_agl_launch_datum_mismatch_m"] is None
        check = veh["alt_agl_launch_datum_check"]
        assert check.startswith("not checked"), check
        assert "alt_agl_launch_datum_m" in check
        assert check != AGL_FRAME_AGREED

    def test_a_frame_check_that_ran_and_passed_says_so(self):
        """The other half: agreement must be stated, not inferred from a null."""
        veh = _row_from_state_source(
            {"alt_agl_m": -42.2, "alt_agl_is_real": True,
             "alt_agl_source": "terrain:gev", "alt_agl_launch_datum_m": 0.0})
        assert veh["alt_agl_launch_datum_check"] == AGL_FRAME_AGREED
        assert veh["alt_agl_launch_datum_mismatch_m"] is None

    def test_an_unusable_launch_datum_figure_is_named_not_skipped(self):
        """A non-numeric launch datum used to fall through the isinstance guard
        and leave the check silently unrun."""
        veh = _row_from_state_source(
            {"alt_agl_m": -42.2, "alt_agl_is_real": True,
             "alt_agl_source": "terrain:gev",
             "alt_agl_launch_datum_m": "903.01 m"})
        assert veh["alt_agl_launch_datum_check"].startswith("not checked")
        assert "903.01 m" in veh["alt_agl_launch_datum_check"]

    def test_a_climbing_aircraft_is_not_reported_as_a_different_frame(self, sim):
        """MEASURED ON THE REAL STACK: a 39.9 m "the two processes are not
        flying the same frame" alarm, raised by nothing but the skew between a
        2 Hz feed and a 10 Hz one while the aircraft changed height.

        The aircraft here is climbing at 12 m/s and the server's launch-datum
        figure is 9 m below this bridge's — three quarters of a second of
        climb. That is motion, and the row must say so instead of alarming.

        FAILS ON THE OLD CODE: a bare `abs(delta) > AGL_FRAME_TOLERANCE_M`
        published `alt_agl_launch_datum_mismatch_m: -9.0`.
        """
        snap = _climbing_snapshot(served_launch_datum_m=31.0)
        assert snap.alt_agl_launch_datum_mismatch_m is None, (
            "a climb was published as a frame disagreement")
        check = snap.alt_agl_launch_datum_check
        assert check.startswith("inconclusive"), check
        assert "not proof of agreement either" in check, check
        assert "12.0 m/s" in check
        # ...and the measured AGL is still the one the operator is shown.
        assert snap.agl == pytest.approx(-42.2)
        assert snap.alt_agl_is_real is True

    def test_a_real_frame_offset_is_still_reported_while_climbing(self):
        """The widening must never HIDE a disagreement. A 300 m offset is not
        something a 12 m/s climb can account for in a second of skew."""
        snap = _climbing_snapshot(served_launch_datum_m=300.0)
        assert snap.alt_agl_launch_datum_mismatch_m == pytest.approx(260.0, abs=0.5)
        assert snap.alt_agl_launch_datum_check.startswith("MISMATCH")
        assert snap.agl == pytest.approx(-42.2), (
            "the measured value must still be served, flagged")

    def test_the_widening_collapses_the_moment_the_climb_stops(self):
        """Why a real offset cannot hide behind the allowance for long: the
        allowance is proportional to vertical rate, so it vanishes in the hover
        or cruise that every sortie contains."""
        climbing = _climbing_snapshot(served_launch_datum_m=31.0)
        assert climbing.alt_agl_launch_datum_mismatch_m is None
        level = _climbing_snapshot(served_launch_datum_m=31.0, vz=0.0)
        assert level.alt_agl_launch_datum_mismatch_m == pytest.approx(
            -9.0, abs=0.5), "the same 9 m gap must surface once level"
        assert level.alt_agl_launch_datum_check.startswith("MISMATCH")

    def test_a_stationary_aircraft_gets_no_motion_allowance(self, sim):
        """With no vertical rate there is nothing for motion to explain, so the
        tolerance is the plain frame tolerance and a 9 m gap is a MISMATCH."""
        veh, _one = _agl_rows(_terrain_tick(alt_agl_launch_datum_m=9.0))
        assert veh["vz"] == pytest.approx(0.0, abs=0.01)
        assert veh["alt_agl_launch_datum_mismatch_m"] == pytest.approx(9.0, abs=0.5)
        assert veh["alt_agl_launch_datum_check"].startswith("MISMATCH")

    def test_a_frozen_mcp_tick_ages_even_while_the_poll_stays_fresh(self, sim):
        """MEASURED ON THE REAL STACK: the server's monitor loop stopped, the
        aircraft flew 600 m and descended 150 m, and `/snapshot` kept serving
        `agl: -0.3` behind `alt_agl_is_real: true` and `alt_agl_age_ms: 63`.

        `uav_task_status` answers from `self.ticks[vehicle]`, so it keeps
        handing back the SAME tick forever when the loop behind it dies. Every
        poll succeeds — so poll age is no evidence of measurement age.

        FAILS ON THE OLD CODE: there was no measurement age at all; the only
        age on the row was the poll's, which stayed near zero indefinitely.
        """
        adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
        app = create_app(adapter=adapter, token=TOKEN, start_loops=False)
        mcp = full_mcp(tick=_terrain_tick(t=100.0))
        feed = MissionFeed(mcp, app.state.hub, vehicles=adapter.vehicles,
                           flown=app.state.bridge.flown)
        app.state.bridge.feed = feed
        app.state.feed = feed
        with TestClient(app) as tc:
            # Two polls first, so the sampling intervals are MEASURED rather
            # than assumed and the only thing moving afterwards is the age.
            feed.poll_once()
            time.sleep(0.15)
            feed.poll_once()
            app.state.bridge.tick_vehicle("Drone1")
            first = tc.get("/snapshot", headers=H).json()["vehicles"][0]
            # The server freezes: same tick, same `t`, every poll succeeding.
            for _ in range(3):
                time.sleep(0.15)
                feed.poll_once()
                app.state.bridge.tick_vehicle("Drone1")
            later = tc.get("/snapshot", headers=H).json()["vehicles"][0]
            assert later["agl"] == first["agl"], "the fixture did not freeze"
            assert later["alt_agl_age_ms"] < 200, (
                "the POLL is still fresh — that is the trap")
            grew = (later["alt_agl_measured_age_ms"]
                    - first["alt_agl_measured_age_ms"])
            assert grew >= 400, (
                f"a frozen measurement aged by only {grew} ms while the poll "
                f"stayed at {later['alt_agl_age_ms']} ms")
            assert "first seen by loop C" in later["alt_agl_measured_age_source"]

    def test_a_tick_that_advances_resets_the_measurement_age(self, sim):
        """The complement: a live server must not read as stale. The age is an
        upper bound, so it never goes to zero — it drops back to the sampling
        slack (one poll interval + one server tick) the moment `t` moves."""
        adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
        app = create_app(adapter=adapter, token=TOKEN, start_loops=False)
        mcp = full_mcp(tick=_terrain_tick(t=100.0))
        feed = MissionFeed(mcp, app.state.hub, vehicles=adapter.vehicles,
                           flown=app.state.bridge.flown)
        app.state.bridge.feed = feed
        app.state.feed = feed
        with TestClient(app) as tc:
            feed.poll_once()
            time.sleep(0.6)
            feed.poll_once()                    # still frozen: age climbing
            app.state.bridge.tick_vehicle("Drone1")
            stale = tc.get("/snapshot", headers=H).json()["vehicles"][0]
            # 0.9 s on, deliberately NOT the assumed half-floor, so a source
            # string reading 500 ms would prove the gap was never measured.
            mcp.tick = _terrain_tick(t=100.9)   # the monitor ticked again
            feed.poll_once()
            app.state.bridge.tick_vehicle("Drone1")
            veh = tc.get("/snapshot", headers=H).json()["vehicles"][0]
        assert veh["alt_agl_measured_age_ms"] < stale["alt_agl_measured_age_ms"], (
            "a freshly advanced tick was still reported as stale")
        assert veh["alt_agl_measured_age_ms"] < 2500
        assert "t=100.9" in veh["alt_agl_measured_age_source"]
        # ...and the 0.9 s between the two `t` stamps is MEASURED from them,
        # not assumed from `DEFAULT_TICK_S`.
        assert "900 ms observed gap between server ticks" in (
            veh["alt_agl_measured_age_source"])
        assert "not yet measured" not in veh["alt_agl_measured_age_source"]

    def test_a_tick_with_no_comparable_stamp_says_the_age_is_unknown(self, sim):
        """No silent zero. A tick the bridge cannot age is reported as
        UNKNOWN with the reason, never as a fresh one."""
        veh, _one = _agl_rows(_terrain_tick(t=None))
        assert veh["alt_agl_measured_age_ms"] is None
        assert veh["alt_agl_measured_age_source"].startswith("unknown")
        assert "frozen feed" in veh["alt_agl_measured_age_source"]
        # ...and the measured AGL is still published, flagged rather than lost.
        assert veh["agl"] == pytest.approx(-42.2)

    def test_a_stamp_that_vanishes_and_returns_does_not_restart_the_clock(self, sim):
        """A frozen tick that briefly loses its stamp must not come back
        looking fresh. The first-seen anchor is keyed on the stamp VALUE, so
        the same stale `t` keeps the same anchor across the gap."""
        adapter = AirSimAdapter(ip="127.0.0.1", port=PORT)
        app = create_app(adapter=adapter, token=TOKEN, start_loops=False)
        mcp = full_mcp(tick=_terrain_tick(t=100.0))
        feed = MissionFeed(mcp, app.state.hub, vehicles=adapter.vehicles,
                           flown=app.state.bridge.flown)
        app.state.bridge.feed = feed
        app.state.feed = feed
        with TestClient(app) as tc:
            feed.poll_once()
            time.sleep(0.2)
            feed.poll_once()
            time.sleep(0.5)
            mcp.tick = _terrain_tick(t=None)     # one tick with no stamp
            feed.poll_once()
            mcp.tick = _terrain_tick(t=100.0)    # ...and the SAME stale tick back
            feed.poll_once()
            app.state.bridge.tick_vehicle("Drone1")
            veh = tc.get("/snapshot", headers=H).json()["vehicles"][0]
        assert veh["alt_agl_measured_age_ms"] >= 700, (
            f"the clock restarted: {veh['alt_agl_measured_age_ms']} ms for a "
            "measurement that has been frozen throughout")

    def test_a_state_source_that_states_no_measurement_age_is_not_read_as_fresh(self):
        """`_enrich_agl`'s own defence, for a row that did not come from
        `_agl_from_tick`: an absent age is unknown, never zero."""
        veh = _row_from_state_source(
            {"alt_agl_m": -42.2, "alt_agl_is_real": True,
             "alt_agl_source": "terrain:gev", "alt_agl_launch_datum_m": 0.0})
        assert veh["alt_agl_measured_age_ms"] is None
        assert veh["alt_agl_measured_age_source"].startswith("unknown")
        assert "no measurement age" in veh["alt_agl_measured_age_source"]

    def test_an_unusable_measurement_age_is_named_not_defaulted(self):
        """A source that DID publish an age, but not a usable one, must be told
        apart from one that published none — different bug, different fix."""
        veh = _row_from_state_source(
            {"alt_agl_m": -42.2, "alt_agl_is_real": True,
             "alt_agl_source": "terrain:gev", "alt_agl_launch_datum_m": 0.0,
             "alt_agl_measured_age_ms": "recent"})
        assert veh["alt_agl_measured_age_ms"] is None
        assert "not a whole number of milliseconds >= 0" in (
            veh["alt_agl_measured_age_source"])

    def test_a_negative_measurement_age_is_refused(self):
        """An age that runs backwards is not an age. It must not be published
        as one, and it must not widen the frame check's skew window."""
        veh = _row_from_state_source(
            {"alt_agl_m": -42.2, "alt_agl_is_real": True,
             "alt_agl_source": "terrain:gev", "alt_agl_launch_datum_m": 0.0,
             "alt_agl_measured_age_ms": -5000})
        assert veh["alt_agl_measured_age_ms"] is None
        assert "-5000" in veh["alt_agl_measured_age_source"]

    def test_the_skew_floor_covers_one_server_tick_and_one_poll(self):
        """The widening window is the deployed cadence, not a magic number:
        `server.DEFAULT_TICK_S` + one loop-C poll at the default `mission_hz`."""
        from godseye_uav.server import DEFAULT_TICK_S

        assert AGL_FEED_SKEW_FLOOR_MS == pytest.approx(
            (DEFAULT_TICK_S + 1.0 / 2.0) * 1000.0)


# ===========================================================================
# END TO END over REAL terrain (ports 51100-51199)
#
# The real MCP server, the real terrain layer, the real bridge: the aircraft is
# put where the MEASURED AGL and the LAUNCH-DATUM AGL disagree by 82 m, and
# /snapshot is read to see which of the two the operator is shown.
#
# The DEM is REAL: postings copied verbatim from God's Eye View's own on-disk
# terrain cache (`.gev-cache/terrain-heights.json`, upstream Re:Earth /
# Mapterhorn bare-earth ELLIPSOIDAL heights), so this needs neither a network
# nor a dev server. Only the two HTTP hops are stood in for: the terrain proxy
# (a recorded fetch) and the bridge's JSON-RPC hop to the MCP server (an
# in-process transport that calls the server's OWN registered tools and returns
# their real payloads).
# ===========================================================================

#: The Fordow meridian profile, 34.8725N - 34.8893N at 50.9958E.
#:   home  34.8853 N — recorded ground 903.01 m HAE (the valley floor)
#:   ridge 34.8765 N — recorded ground 997.96 m HAE (+95 m)
_DEM_FORDOW = (
    (34.8725, 50.9958, 961.11), (34.8733, 50.9958, 957.77),
    (34.8741, 50.9958, 956.95), (34.8749, 50.9958, 966.01),
    (34.8757, 50.9958, 985.18), (34.8765, 50.9958, 997.96),
    (34.8773, 50.9958, 976.93), (34.8781, 50.9958, 960.18),
    (34.8789, 50.9958, 961.7), (34.8797, 50.9958, 956.63),
    (34.8805, 50.9958, 937.44), (34.8813, 50.9958, 944.03),
    (34.8821, 50.9958, 944.13), (34.8829, 50.9958, 925.57),
    (34.8837, 50.9958, 914.63), (34.8845, 50.9958, 906.21),
    (34.8849, 50.9958, 904.14), (34.8853, 50.9958, 903.01),
    (34.8861, 50.9958, 903.31), (34.8869, 50.9958, 893.12),
    (34.8877, 50.9958, 889.17), (34.8885, 50.9958, 885.8),
    (34.8893, 50.9958, 882.49),
)

#: How far the DEM fixture will reach for a posting. Beyond it the fixture
#: answers with NO height, so the provider takes its FLAGGED fallback rather
#: than inventing a measurement for unmapped ground.
_DEM_REACH_M = 1500.0

FORDOW_HOME_LAT, FORDOW_HOME_LON, FORDOW_HOME_HAE = 34.8853, 50.9958, 903.01
FORDOW_RIDGE = (34.8765, 50.9958)
#: This module's second port range, for the real-terrain stack.
REAL_PORTS = range(51100, 51120)


def _dem_height(dem, lat, lon):
    """Nearest recorded posting within `_DEM_REACH_M`, else None."""
    from godseye_uav.safety import haversine_m

    best, best_m = None, None
    for p_lat, p_lon, hae in dem:
        d = haversine_m(lat, lon, p_lat, p_lon)
        if best_m is None or d < best_m:
            best, best_m = hae, d
    return best if best_m is not None and best_m <= _DEM_REACH_M else None


def _recorded_terrain_fetch(dem):
    """An offline stand-in for the God's Eye View terrain proxy. Only
    `/api/terrain/heights` answers; everything else 404s, which is what a dead
    upstream looks like."""
    from urllib.parse import parse_qs, urlparse

    from godseye_uav.realdata import HttpResponse

    def fetch(url, timeout_s):
        parsed = urlparse(url)
        if parsed.path == "/api/terrain/heights":
            results = []
            for pair in parse_qs(parsed.query)["points"][0].split(";"):
                p_lon, p_lat = (float(v) for v in pair.split(","))
                hae = _dem_height(dem, p_lat, p_lon)
                results.append({"lon": p_lon, "lat": p_lat, "ellipsoid": hae,
                                "geoid": None, "elevation": hae})
            return HttpResponse(200, json.dumps({"results": results}), {})
        return HttpResponse(404, json.dumps({"error": "not in this fixture"}), {})

    return fetch


class InProcessMcp(McpClient):
    """The bridge's MCP seam, wired to a REAL `GodseyeUavServer` in-process.

    Only the loopback HTTP hop is replaced: every payload is the server's own
    tool output, so what the bridge folds onto a vehicle row here is exactly
    what it folds on over the wire.
    """

    def __init__(self, srv):
        super().__init__("inproc://godseye-uav", "t")
        self.srv = srv
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        entry = self.srv.mcp._tool_manager._tools.get(name)
        if entry is None:
            return None, f"{name}: Unknown tool: {name}"
        try:
            out = asyncio.run(entry.fn(**arguments))
        except Exception as exc:  # surfaced as a feed error, exactly like HTTP
            return None, f"{name}: {type(exc).__name__}: {exc}"
        return out, None

    def read_resource(self, uri):
        self.calls.append(("resource", {"uri": uri}))
        return None, f"{uri}: not served by the in-process transport"


@pytest.fixture()
def fordow_stack(tmp_path):
    """FakeAirSim at Fordow + a real MCP server with the real-world data layer
    on and offline, its AO terrain resident. Yields (server, sim, home)."""
    import airsim
    from godseye_uav.geo import GeoPoint
    from godseye_uav.realdata import RealWorldData
    from godseye_uav.server import GodseyeUavServer, UavBackend
    from godseye_uav.store import Store

    home = GeoPoint(FORDOW_HOME_LAT, FORDOW_HOME_LON, FORDOW_HOME_HAE)
    sim, srv, store, last = None, None, None, None
    for port in REAL_PORTS:
        candidate = FakeAirSim(home=home, port=port)
        try:
            candidate.start()
        except OSError as exc:
            last = exc
            try:
                candidate.stop()
            except Exception:
                pass
            continue
        sim = candidate
        break
    if sim is None:
        raise RuntimeError(f"no free port in {REAL_PORTS}: {last}")
    try:
        c = airsim.MultirotorClient(port=sim.port)
        c.confirmConnection()
        store = Store(tmp_path)
        srv = GodseyeUavServer(
            UavBackend(c, home, sim=sim), store, theater="iran-fordow",
            watchdog_s=90.0,
            real_data=RealWorldData(origin="http://recorded",
                                    fetch=_recorded_terrain_fetch(_DEM_FORDOW),
                                    fallback_ground_msl_m=None))
        srv.sim = sim
        loaded = srv.prefetch_ao_terrain()
        assert loaded["measured"] > 0, (
            f"the recorded DEM measured nothing: {loaded}")
        yield srv, sim, home
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


class TestRealTerrainEndToEnd:
    def test_the_operator_is_shown_the_agl_the_harness_flies_on(
            self, fordow_stack):
        """THE proof, over real terrain, through the real server.

        The aircraft is held 40 m above the LAUNCH DATUM over the Fordow ridge,
        whose recorded ground is ~82 m above the home point. The MCP server —
        the authority, the thing the safety envelope and the harness read —
        measures -42 m AGL: the aircraft is BELOW the crest. The old bridge put
        +40 m on the operator's HUD, and with the `max(0.0, ...)` floor it
        could not have shown the negative number even if it had one.

        FAILS ON THE OLD CODE: `/snapshot.vehicles[0].agl` was +40.0, i.e. it
        equalled `alt_agl_launch_datum_m` and disagreed with the server's own
        `alt_agl_m` by the full 82 m.
        """
        from godseye_uav.geo import GeoPoint, geodetic_to_ned
        from godseye_uav.server import AGL_SOURCE_TERRAIN

        srv, sim, home = fordow_stack

        # Hold the aircraft 40 m above the launch datum, over the ridge.
        target = GeoPoint(FORDOW_RIDGE[0], FORDOW_RIDGE[1], home.altitude + 40.0)
        drone = sim._vehicles["Drone1"]
        drone.ned = geodetic_to_ned(target, home)
        drone.landed = False

        # --- what the SERVER knows (the number every gate is checked on) ---
        verdict = asyncio.run(srv.tick_once("Drone1"))
        assert verdict["alt_agl_is_real"] is True, verdict.get(
            "terrain_floor_reason")
        assert verdict["alt_agl_source"] == AGL_SOURCE_TERRAIN
        assert verdict["alt_agl_launch_datum_m"] == pytest.approx(40.0, abs=1.0)
        assert verdict["alt_agl_m"] < 0.0, (
            "the fixture did not put the aircraft below the ridge")
        disagreement = (verdict["alt_agl_launch_datum_m"]
                        - verdict["alt_agl_m"])
        assert disagreement > 75.0, disagreement

        # --- what the OPERATOR is shown ---
        adapter = AirSimAdapter(ip="127.0.0.1", port=sim.port, home=home)
        app = create_app(adapter=adapter, token=TOKEN, start_loops=False)
        mcp = InProcessMcp(srv)
        feed = MissionFeed(mcp, app.state.hub, vehicles=adapter.vehicles,
                           flown=app.state.bridge.flown)
        app.state.bridge.feed = feed
        app.state.feed = feed
        with TestClient(app) as tc:
            feed.poll_once()
            app.state.bridge.tick_vehicle("Drone1")
            veh = tc.get("/snapshot", headers=H).json()["vehicles"][0]

        # ONE source of truth: the HUD's number IS the server's number.
        assert veh["agl"] == pytest.approx(verdict["alt_agl_m"], abs=0.05), (
            f"the operator is shown {veh['agl']} while the server flies on "
            f"{verdict['alt_agl_m']}")
        assert veh["alt_agl_m"] == veh["agl"]
        assert veh["agl"] < 0.0, "a below-terrain aircraft must read negative"
        # ...carrying the provenance the HUD needs (BRIDGE_CONTRACT).
        assert veh["alt_agl_is_real"] is True
        assert veh["alt_agl_source"] == AGL_SOURCE_TERRAIN
        assert veh["alt_agl_reason"] is None
        # ...with the launch-datum figure alongside, and the two frames agreed.
        assert veh["alt_agl_launch_datum_m"] == pytest.approx(40.0, abs=1.0)
        assert veh["alt_agl_launch_datum_mismatch_m"] is None
        assert (veh["alt_agl_launch_datum_m"] - veh["agl"]) > 75.0
        # The altitudes the two processes publish for this aircraft agree too.
        assert veh["alt_hae"] == pytest.approx(
            home.altitude + 40.0, abs=1.0)
        # The ground under it is a REAL recorded posting, not a fabrication.
        sample = srv.terrain_at(FORDOW_RIDGE[0], FORDOW_RIDGE[1])
        assert sample.real is True
        assert verdict["terrain_hae_m"] == pytest.approx(
            _dem_height(_DEM_FORDOW, sample.lat, sample.lon), abs=0.01)

    def test_off_the_mapped_ground_the_bridge_says_so(self, fordow_stack):
        """Fail soft, but VISIBLE: where the DEM has nothing, the row falls
        back to the launch datum, labels it, and carries the server's reason —
        the same degradation the server publishes, not a quiet substitution."""
        from godseye_uav.geo import GeoPoint, geodetic_to_ned

        srv, sim, home = fordow_stack
        # 3.5 km east of the recorded meridian: inside the AO, off the DEM.
        off_map = GeoPoint(34.8853, 51.0340, home.altitude + 40.0)
        drone = sim._vehicles["Drone1"]
        drone.ned = geodetic_to_ned(off_map, home)
        drone.landed = False

        verdict = asyncio.run(srv.tick_once("Drone1"))
        assert verdict["alt_agl_is_real"] is False

        adapter = AirSimAdapter(ip="127.0.0.1", port=sim.port, home=home)
        app = create_app(adapter=adapter, token=TOKEN, start_loops=False)
        feed = MissionFeed(InProcessMcp(srv), app.state.hub,
                           vehicles=adapter.vehicles,
                           flown=app.state.bridge.flown)
        app.state.bridge.feed = feed
        app.state.feed = feed
        with TestClient(app) as tc:
            feed.poll_once()
            app.state.bridge.tick_vehicle("Drone1")
            veh = tc.get("/snapshot", headers=H).json()["vehicles"][0]

        assert veh["alt_agl_is_real"] is False
        assert veh["agl"] == pytest.approx(veh["alt_agl_launch_datum_m"], abs=1.0)
        assert veh["alt_agl_reason"], "a synthetic AGL with no reason"
        assert veh["alt_agl_launch_datum_mismatch_m"] is None
