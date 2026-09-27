# godSeye: UAV ISR simulation, MCP server and analyst host

This directory is the Python half of Eye in the Sky (package `godseye-uav`). It holds the simulated
UAV backend (AirSim, or a built-in fake), the **MCP server** that is the only command path, the
**telemetry bridge**, the **intel graph**, the in-app **AI analyst**, and the **host** that runs all
of them, plus the console UI, as one process on one origin. The repository `README.md` covers
installing and running the app; this file is the view from inside `godseye/`.

**ISR-only.** There are no kinetic tools anywhere in the system. It observes, classifies and
reports; command authority stays with the operator.

```
 python -m godseye_uav.app            (the app; start.sh runs it --headless)
 ┌──────────── one FastAPI app, one asyncio loop, loopback only ────────────┐
 │ /                     console UI (gods-eye-view build)                   │
 │ /app/config /intel/* /chat/*        host routes (host.py, intel_graph,   │
 │                                     chat)                                │
 │ /mcp                  MCP server (server.py), bearer auth                │
 │ /health /snapshot /events ...       telemetry bridge (bridge.py)         │
 └────────────┬──────────────────────────────────────────────────────────────┘
              │ msgpack-rpc, loopback
      fake AirSim (fake_airsim.py)  or  real AirSim (--real)
```

## Running it

From the repository root, `./eye-in-the-sky` (see the root `README.md`). From here:

```bash
./start.sh                               # the classic dev stack: host on :8790 and :8791, vite on :4173
.venv/bin/python -m godseye_uav.app --help
```

`start.sh` starts `godseye_uav.app --headless` with the bridge port (8790) as the app port and the
MCP port (8791) as a second listener for the same app, then the God's Eye View vite dev server:

| Service | URL | Notes |
|---|---|---|
| MCP server | `http://127.0.0.1:8791/mcp` | the only command path; `Authorization: Bearer <token>`. Also at `:8790/mcp`. |
| Telemetry bridge | `http://127.0.0.1:8790` | read feeds; see `BRIDGE_CONTRACT.md` |
| Console (built UI) | `http://127.0.0.1:8790/` | needs `npm run build` in `gods-eye-view/` |
| Console (vite dev UI) | `http://localhost:4173` | hot reload; talks to `:8790` with `dev-token` |

Environment: `THEATER`, `SIM_BACKEND` (`fake`\|`real`), `AIRSIM_PORT`, `BRIDGE_PORT`, `MCP_PORT`,
`UI_PORT`, `TOKEN` (default `dev-token`, passed to the app as `GODSEYE_TOKEN`) and `STORE` (default
`godseye/.godseye/store`).

`./scripts/demo_laptop.sh` boots that stack and flies a scripted recon mission over MCP. The browser
page opens on the console; Track on the rail shows the drone on the map, and `?console=off` gives the
plain map.

`godseye_uav.launch` (the older three-listener launcher) is unchanged and still used by tests.

## The in-app analyst

The analyst (`chat.py`) runs the Claude Code CLI through the Claude Agent SDK, one CLI process per
chat session, with no built-in tools and only an in-process MCP server named `godseye`
(`analyst_toolbelt.py`) whose tools are generated from the real server. Every call is classified by
`analyst_policy.py`: reads and dry runs run at once; sensor, command, sim and safety-override calls
wait for the operator's order slip in the console. Its system prompt is `analyst_prompt.md`,
distilled from the skill below.

It needs the `app` extra (`pip install -e '.[app]'`, which brings `claude-agent-sdk` and
`pywebview`) and a Claude sign-in or `ANTHROPIC_API_KEY`. `INTEL_CONSOLE.md` has the full contract:
routes, SSE events, approval classes, session grants, sign-in policy, environment variables, where
transcripts are written, and observed cost.

## Connecting a harness

The server speaks MCP over Streamable HTTP with bearer auth. `.mcp.json` in this directory wires it
up for Claude Code and any client that reads that format, against `start.sh`:

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

Against the app (port 8780, a random token per launch), use `<store>/../mcp.json`, which the app
writes with mode 0600 and which works as a Claude Code `--mcp-config` file.

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

Calls over `/mcp` do not pass through the console's order slips (those are for the in-app analyst);
the server's own safety gates apply to every caller.

## The skill

`.agents/skills/godseye-uav/` (symlinked to `.claude/skills/` for Claude Code) is the operating manual
the harness loads: mission patterns, sensor and altitude doctrine, the safety contract, and the
SALUTE/INTREP reporting templates. Its core rule is the workflow:

> **task → plan → dry-run → execute → monitor → report**

The dry run is not optional. It returns the waypoints, fuel estimate and the BINGO gate result
without flying anything, and a rejected gate means re-plan, never work around.

## Safety model

The server owns safety and no caller can override it; a skill's rules of engagement may only be
stricter.

- **Geofence / ceiling / min-AGL / max-speed**, checked in flight, not only at plan time.
- **BINGO fuel**: every plan must pass a pre-flight gate (plan + return leg + 20% reserve ≤ fuel).
  Reaching BINGO forces an RTB that cannot be cancelled, and the mission is flagged
  `incomplete - fuel`.
- **Lost link**: the server runs the mission's lost-link plan on its own (hold-orbit /
  climb-for-LOS / RTB / continue) and logs the LOAL event into the INTREP.
- **Busy**: one in-flight command per vehicle; a second returns `{"status":"busy", "current": …}`.

## Layout

| Path | What |
|---|---|
| `mcp/godseye_uav/app.py`, `host.py` | Entry point (window, browser, headless, self-test) and the single-process host |
| `mcp/godseye_uav/server.py` | MCP server: 46 tools, 8 resources |
| `mcp/godseye_uav/bridge.py` | Telemetry bridge |
| `mcp/godseye_uav/intel_graph.py` | Intel graph and `/intel/*` routes |
| `mcp/godseye_uav/chat.py`, `analyst_policy.py`, `analyst_toolbelt.py`, `analyst_prompt.md` | The analyst |
| `mcp/godseye_uav/` (rest) | Safety, tasking, missions, targets, threat, geo, store, theaters, real data, fake sim |
| `packaging/macos/` | PyInstaller spec, entry point and icon (built by `../scripts/build_desktop.sh`) |
| `tests/` | Test suite; runs with no GPU and no Unreal |
| `scripts/` | `demo_laptop.sh` (one-command demo), `demo_mission.py`, `ci.sh`, `_airsim_client.sh` |
| `.agents/skills/godseye-uav/` | The harness skill |
| `INTEL_CONSOLE.md` | Host, HTTP and SSE contract, approvals, analyst, data honesty |
| `TOOL_CONTRACT.md` | MCP tool and resource contract |
| `BRIDGE_CONTRACT.md` | Bridge feed contract and the in-process accessors |
| `REAL_DATA_INTEGRATION.md` | Which real-world data the sim can consume, and its limits |
| `PLAN.md`, `GAP_REGISTER.md`, `INTEGRATION_FINDINGS.md` | Plan (findings M1–M22 / T1–T9), audit and integration history |

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

`tests/conftest.py` puts `mcp/` and the AirSim PythonClient (`$GODSEYE_AIRSIM_PYTHONCLIENT`,
`../airsim/PythonClient` or `.godseye/vendor/airsim/PythonClient`, which `scripts/setup.sh` fetches)
on the path. The whole suite runs against the built-in fake AirSim with no Unreal and no GPU:
1,522 tests, about 40 minutes, most of it `test_server.py`. The analyst and window tests mostly use
fakes for `claude_agent_sdk` and `webview`; the few that need the real SDK skip without it, so the
suite runs without the `app` extra. One test runs the
built desktop app and is opt-in: `GODSEYE_TEST_DESKTOP_APP=1 .venv/bin/python -m pytest
tests/test_app.py -k built_app` after `../scripts/build_desktop.sh`.

## Datum: read this before touching altitudes

Altitude is the highest-risk area in the system. There is exactly one conversion point,
`geo.canonical_altitude()`, and no module may do its own geoid math.

- Origin altitudes are entered as MSL and converted once at ingest.
- `altHae = altMSL + N(φ, λ)`, where `N` is the EGM96 geoid undulation: negative where the geoid is
  below the ellipsoid (−22.21 m at the Redmond origin, +1.58 m near Isfahan).
- Every altitude field names its datum: `alt_hae_m`, `alt_msl_m`, `alt_agl_m`. Never a bare `alt_m`.
- If an accurate geoid source cannot be loaded the code raises rather than silently degrading; a
  degraded datum is surfaced to the operator, never hidden.

## Known limits

- The UAV is simulated; only the environment data can be real. See `REAL_DATA_INTEGRATION.md`.
- The real-data layer is off by default (`GODSEYE_REAL_DATA`). With it off, `alt_agl_m` is height
  above the launch datum, LOS is geometric and there is no live air traffic; every such value says
  so (`alt_agl_is_real`, `los_is_measured`, `traffic_is_real`). Turning it on needs God's Eye View's
  `/api` providers, which only its vite dev server hosts.
- Geo-registration is certified to a measured ~900 m radius from the origin: the ported AirSim math
  mixes a spherical NED→geodetic with an ellipsoidal geodetic→NED, so horizontal error grows with
  range (worst ≈5 m/km near the equator). Beyond that radius, re-anchor the origin.
- OpenSky is licensed for non-commercial research/education use; several other feeds carry
  attribution requirements. See `../gods-eye-view/DATA_SOURCES.md`.
- The analyst and host have their own list in `INTEL_CONSOLE.md`, "Known limits".
