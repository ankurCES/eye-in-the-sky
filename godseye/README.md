# godSeye — agentic UAV ISR mission simulation

An agentic harness (an LLM + the `godseye-uav` skill) flies reconnaissance, target-identification and
threat-assessment missions over real-world maps. **God's Eye View** (Cesium photorealistic globe) is the
command center the operator watches; **AirSim** (or a built-in fake) is the UAV physics and sensor
backend; a **Python MCP server** is the only command path.

**ISR-only.** There are no kinetic tools anywhere in the system. It observes, classifies and reports;
command authority stays with the operator.

```
  agentic harness (LLM + godseye-uav skill)
            │  MCP · Streamable HTTP · Bearer
  ┌─────────▼──────────────────────────────┐
  │ godseye-mcp-server        :8791/mcp    │  safety envelope · FIFO queue · fuel/BINGO
  │  └─ telemetry bridge      :8790        │  /snapshot /mission-overlay /camera /events
  └────┬──────────────────────────┬────────┘
       │ msgpack-rpc :41451       │ REST (read-only)
  ┌────▼─────────────┐   ┌────────▼──────────────┐
  │ AirSim  or  fake │   │ God's Eye View  :4173 │
  └──────────────────┘   └───────────────────────┘
```

## Quick start (no GPU required)

```bash
./scripts/demo_laptop.sh
```

This boots the fake AirSim, the MCP server, the telemetry bridge and the GEV UI, then flies a scripted
recon mission you can watch live. Everything runs on loopback.

Services only, no demo:

```bash
./start.sh
```

| Service | URL | Notes |
|---|---|---|
| MCP server | `http://127.0.0.1:8791/mcp` | the only command path; `Authorization: Bearer <token>` |
| Telemetry bridge | `http://127.0.0.1:8790` | read-only feeds |
| God's Eye View | `http://localhost:4173` | the operator's command center |

Environment: `THEATER`, `SIM_BACKEND` (`fake`\|`real`), `AIRSIM_PORT`, `BRIDGE_PORT`, `MCP_PORT`,
`UI_PORT`, `TOKEN`.

## Connecting a harness

The server speaks MCP over Streamable HTTP with Bearer auth. `.mcp.json` in this repo wires it up for
Claude Code and any client that reads that format:

```json
{
  "mcpServers": {
    "godseye-uav": {
      "type": "http",
      "url": "http://127.0.0.1:8791/mcp",
      "headers": { "Authorization": "Bearer ${GODSEYE_MCP_TOKEN:-dev-token}" }
    }
  }
}
```

From Python:

```python
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

async with httpx2.AsyncClient(headers={"Authorization": "Bearer dev-token"}) as hc:
    async with streamable_http_client("http://127.0.0.1:8791/mcp", http_client=hc) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print([t.name for t in (await s.list_tools()).tools])
```

Note this SDK's `streamable_http_client` takes `http_client=` (not `headers=`) and yields two values.

`scripts/demo_mission.py` is a complete worked example that drives the real transport.

## The skill

`.agents/skills/godseye-uav/` (symlinked to `.claude/skills/` for Claude Code) is the operating manual
the harness loads: mission patterns, sensor and altitude doctrine, the safety contract, and the
SALUTE/INTREP reporting templates. Its core rule is the workflow:

> **task → plan → dry-run → execute → monitor → report**

The dry-run is not optional — it returns the waypoints, fuel estimate and the BINGO gate result
*without flying anything*, and a rejected gate means re-plan, never work around.

## Safety model

The server owns safety and the harness cannot override it — a skill's ROE may only be *stricter*.

- **Geofence / ceiling / min-AGL / max-speed**, checked in flight, not only at plan time.
- **BINGO fuel**: every plan must pass a pre-flight gate (plan + return leg + 20% reserve ≤ capacity).
  Reaching BINGO forces an RTB that **cannot be cancelled**, and the mission is flagged
  `incomplete - fuel`.
- **Lost link**: the server autonomously executes the mission's lost-link plan (hold-orbit /
  climb-for-LOS / RTB / continue) and logs the LOAL event into the INTREP.

## Layout

| Path | What |
|---|---|
| `mcp/godseye_uav/` | MCP server, telemetry bridge, safety, missions, intel, geo, fake sim |
| `tests/` | UE-free test suite — runs with zero GPU |
| `scripts/` | `demo_laptop.sh` (one-command demo), `demo_mission.py`, `ci.sh` |
| `.agents/skills/godseye-uav/` | the harness skill |
| `PLAN.md` | the product plan (findings referenced as M1–M22 / T1–T9) |
| `TOOL_CONTRACT.md` | authoritative MCP tool + resource contract |
| `BRIDGE_CONTRACT.md` | bridge ↔ command-center feed contract |
| `REAL_DATA_INTEGRATION.md` | which real-world data the sim consumes, and its limits |
| `GAP_REGISTER.md` | the verified gap register from the plan-vs-code audit |

## Tests

```bash
PYTHONPATH="$PWD/mcp:$PWD/../airsim/PythonClient" .venv/bin/python -m pytest tests -q
```

The whole suite runs against the built-in fake AirSim — **no Unreal, no GPU**. That is deliberate: CI,
skill development and doctrine iteration all have to work on a laptop.

## Datum — read this before touching altitudes

Altitude is the highest-risk area in the system. There is exactly **one** conversion point,
`geo.canonical_altitude()`, and no module may do its own geoid math.

- Origin altitudes are entered as **MSL** and converted once at ingest.
- `altHae = altMSL + N(φ, λ)`, where `N` is the EGM96 geoid undulation — **negative** where the geoid is
  below the ellipsoid (−22.21 m at the Redmond origin, +1.58 m near Isfahan).
- Every altitude field names its datum: `alt_hae_m`, `alt_msl_m`, `alt_agl_m`. Never a bare `alt_m`.
- If an accurate geoid source cannot be loaded the code **raises** rather than silently degrading; a
  degraded datum is surfaced to the operator, never hidden.

## Known limits

- The UAV is simulated; only the *environment* data is real. See `REAL_DATA_INTEGRATION.md`.
- `alt_agl_m` is height above the launch datum in the flat-world fake sim; terrain-relative AGL
  arrives with the real-terrain work.
- Geo-registration is certified to a measured ~900 m radius from the origin: the ported AirSim math
  mixes a spherical NED→geodetic with an ellipsoidal geodetic→NED, so horizontal error grows with
  range (worst ≈5 m/km near the equator). Beyond that radius, re-anchor the origin.
- OpenSky is licensed for non-commercial research/education use; several other feeds carry attribution
  requirements. See `gods-eye-view/DATA_SOURCES.md`.
