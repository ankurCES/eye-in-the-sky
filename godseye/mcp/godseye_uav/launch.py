"""One-command harness launch (PLAN Phase 3.5): fake AirSim + MCP server +
telemetry bridge, so a harness (or the GEV mission-control panel) can fly a
UAV mission and watch it live.

Usage:
    PYTHONPATH=mcp:<airsim>/PythonClient python -m godseye_uav.launch \
        [--theater indo-pak-loc] [--sim-port 41451] [--mcp-port 8791] \
        [--bridge-port 8790] [--real]

The MCP server holds the safety envelope (geofence = theater AO, home =
theater origin). The bridge exposes /snapshot (GEV UAV layer) and /control/*
(the mission-control panel). Pass --real to connect to a real AirSim instead
of the built-in fake.

THEATERS (single source of truth): this module used to carry its own hand-typed
`THEATERS` dict, which had drifted from the two other copies — its
"iran-isfahan" was in fact Natanz, 118.6 km and -20 m away from the canonical
entry, so the out-of-the-box demo mission was geofence-rejected. The table now
lives in `theaters.py` and nowhere else; `--theater` choices come from
`theaters.ids()`. The resolved row is handed to the server as `theater=`, not
just as a geofence — see `build_server`, which explains what was silently wrong
(targets spawned ~1.4 km underground) while only the envelope was wired.

ALTITUDE DATUM (T1): a theater's `home_alt_msl_m` is an MSL *ground elevation*
(the datum AirSim's settings.json OriginGeopoint is entered in), while
`geo.GeoPoint.altitude` is HAE. The two differ by the EGM96 undulation N — up
to -33.2 m at indo-pak-loc, -30.3 m at red-sea-hormuz, -22.2 m at the default
Redmond origin. `home_geopoint()` below is the ONE place this launcher
converts, via `geo.canonical_altitude(..., datum="msl")`; every downstream
consumer (FakeAirSim, UavBackend, the bridge) receives HAE and converts no
further. `SafetyEnvelope.home` keeps MSL — that field is documented as
`lat, lon, alt_msl` and the fuel model's return-leg math is written against it.
"""
from __future__ import annotations

import argparse
import asyncio
import threading

import uvicorn

from . import theaters
from .bridge import AirSimAdapter, create_app
from .fake_airsim import FakeAirSim
from .geo import GeoPoint, canonical_altitude
from .safety import SafetyEnvelope
from .server import GodseyeUavServer, UavBackend
from .store import Store
from .theaters import Theater


def resolve_theater(theater_id: str | None) -> Theater:
    """Look up a theater in the ONE table (`theaters.py`).

    Raises KeyError naming the known ids — an unknown theater is never
    silently swapped for the default.
    """
    return theaters.get(theater_id)


def home_altitude_fix(t: Theater):
    """The T1 conversion for this theater's home, with provenance.

    Returns the full `AltitudeFix` (alt_hae, alt_msl, undulation_m, source,
    degraded) so the launcher can print which EGM96 source answered. Raises
    `geo.GeoidUnavailableError` if no accurate source loads — the launcher
    refuses to boot on a degraded datum rather than fly on one (T1).
    """
    return canonical_altitude(t.home_alt_msl_m, t.home_lat, t.home_lon,
                              datum="msl")


def home_geopoint(t: Theater) -> GeoPoint:
    """Theater home as a `GeoPoint`, whose altitude field is HAE (T1).

    This is the ingest boundary: MSL in, HAE out, exactly once.
    """
    return GeoPoint(t.home_lat, t.home_lon, home_altitude_fix(t).alt_hae)


def build_envelope(t: Theater) -> SafetyEnvelope:
    """Safety envelope for this theater: geofence = AO, home = MSL triple."""
    return SafetyEnvelope(**t.envelope_kwargs())


def build_server(t: Theater, backend: UavBackend, store: Store, *,
                 token: str = "dev-token", **kwargs) -> GodseyeUavServer:
    """The MCP server for THIS theater — geofence and theater from one row.

    THEATER WIRING (the defect this function exists to close): the launcher
    used to call `GodseyeUavServer(backend, store, envelope=envelope, ...)`
    with no `theater=`, so the server resolved `theaters.get(None)` — the
    DEFAULT (Redmond) theater — for everything that is not the geofence. The
    drone and the AO were correctly the requested theater's while:

      * `sim_spawn_target`'s default altitude was Redmond's 122 m ground
        elevation. Under `--theater iran-isfahan`, whose ground is 1570 m MSL,
        every target spawned without an explicit altitude sat ~1448 m
        UNDERGROUND and could never be detected.
      * the M12 pattern-of-life store was seeded with Redmond's POIs, so every
        POI-relative assessment was about a place 11,000 km away.
      * the INTREP's `area_name` named the wrong theater.

    `GodseyeUavServer` reports that condition as `theater_mismatch`; because
    the envelope and the theater are both derived from `t` here, this launcher
    cannot trip it. The wiring is asserted at boot by `main()` rather than
    assumed (`tests/test_launch.py`).
    """
    return GodseyeUavServer(backend, store, envelope=build_envelope(t),
                            theater=t, token=token, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="godSeye UAV harness launcher")
    ap.add_argument("--theater", default=theaters.DEFAULT_THEATER_ID,
                    choices=theaters.ids(),
                    help="AO preset from godseye_uav.theaters (single source of truth)")
    ap.add_argument("--sim-port", type=int, default=41451)
    ap.add_argument("--mcp-port", type=int, default=8791)
    ap.add_argument("--bridge-port", type=int, default=8790)
    ap.add_argument("--token", default="dev-token")
    ap.add_argument("--real", action="store_true",
                    help="connect to a real AirSim on --sim-port instead of the fake")
    ap.add_argument("--db", default=".godseye/store")
    return ap


def main() -> None:
    args = build_parser().parse_args()

    t = resolve_theater(args.theater)
    fix = home_altitude_fix(t)
    home = GeoPoint(t.home_lat, t.home_lon, fix.alt_hae)

    print(f"[launch] theater={t.id} ({t.place}) — {t.label}")
    print(f"[launch] home={t.home_lat:.6f},{t.home_lon:.6f} "
          f"alt_msl={fix.alt_msl:.1f}m -> alt_hae={fix.alt_hae:.1f}m "
          f"(N={fix.undulation_m:+.3f}m via {fix.source}) [T1: converted once, here]")
    if fix.degraded:  # pragma: no cover - canonical_altitude raises by default
        print("[launch] WARNING: DEGRADED datum — altitudes are NOT gate-grade")

    sim = None
    if not args.real:
        sim = FakeAirSim(home=home, port=args.sim_port)
        sim.start()
        print(f"[launch] fake AirSim on :{args.sim_port} home_hae={home.altitude:.1f}m")

    import airsim  # in-repo client via PYTHONPATH

    client = airsim.MultirotorClient(port=args.sim_port)
    client.confirmConnection()
    backend = UavBackend(client, home, sim=sim)
    store = Store(args.db)
    mcp_server = build_server(t, backend, store, token=args.token)
    if mcp_server.theater_mismatch is not None:  # pragma: no cover - wiring guard
        raise SystemExit(
            f"[launch] FATAL: server resolved theater "
            f"{mcp_server.theater.id!r} for a {t.id!r} envelope: "
            f"{mcp_server.theater_mismatch}")
    print(f"[launch] server theater={mcp_server.theater.id} "
          f"ground={t.home_alt_msl_m:.0f} m MSL "
          f"(sim_spawn_target's default altitude), "
          f"{len(t.pois)} pattern-of-life POIs seeded")

    # telemetry bridge (GEV UAV layer + mission-control panel). Point its
    # /control proxy at the MCP server we are about to serve.
    import os
    os.environ.setdefault("GODSEYE_MCP_URL", f"http://127.0.0.1:{args.mcp_port}/mcp")
    os.environ.setdefault("GODSEYE_MCP_TOKEN", args.token)
    # Bridge builds its own per-thread isolated-IOLoop client (tornado fix);
    # do not inject the main-thread client here.
    adapter = AirSimAdapter(port=args.sim_port, home=home)
    bridge_app = create_app(adapter=adapter, token=args.token)
    bridge = uvicorn.Server(uvicorn.Config(
        bridge_app, host="127.0.0.1", port=args.bridge_port, log_level="warning"))
    threading.Thread(target=bridge.run, daemon=True).start()
    print(f"[launch] telemetry bridge on http://127.0.0.1:{args.bridge_port} "
          f"(/snapshot, /control/*)")

    print(f"[launch] MCP server on http://127.0.0.1:{args.mcp_port}/mcp "
          f"theater={t.id} — Ctrl-C to stop")
    try:
        asyncio.run(mcp_server.serve(host="127.0.0.1", port=args.mcp_port))
    except KeyboardInterrupt:
        pass
    finally:
        mcp_server.tasking.shutdown()
        if sim is not None:
            sim.stop()


if __name__ == "__main__":
    main()
