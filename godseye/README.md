# godSeye: UAV ISR simulation, MCP server and analyst host

This directory is the Python half of Eye in the Sky (package `godseye-uav`). It holds the simulated
UAV backend (AirSim, or a built-in fake), the **MCP server** that is the only command path, the
**telemetry bridge**, the **intel graph**, the in-app **AI analyst**, and the **host** that runs all
of them, plus the console UI, as one process on one origin. The repository `README.md` covers
installing and running the app; this file is the view from inside `godseye/`.

**ISR by default.** In ISR mode there are no kinetic tools; the opt-in simulated wargame (M14a,
PLAN.md §4.5a) engages simulated scenario units only. It observes, classifies and reports; command
authority stays with the operator.

```
 python -m godseye_uav.app            (the app; start.sh runs it --headless)
 ┌──────────── one FastAPI app, one asyncio loop, loopback only ────────────┐
 │ /                     console UI (gods-eye-view build)                   │
 │ /app/config /intel/* /chat/*        host routes (host.py, intel_graph,   │
 │                                     chat)                                │
 │ /app/console-claim /wargame/*       simulated wargame (M14a): the        │
 │                                     console key, End (wargame_tools)     │
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
`godseye/.godseye/store`). `start.sh` passes `--geodata on`, and passes `--theater` only when
`THEATER` is set; with it unset the app boots the theater persisted in `$STORE/theater.json`, else
the table default. An unknown `THEATER` is refused before anything binds a port.

`./scripts/demo_laptop.sh` boots that stack and flies a scripted recon mission over MCP. It always
names a table theater (`THEATER`, else the table default), so a theater set from chat is not
restored under it. The browser page opens on the console; Track on the rail shows the drone on the
map, and `?console=off` gives the plain map.

The app's theater, map-data and airframe flags (`app.py`, WG v2 Phase A):

| Flag | Default | Meaning |
|---|---|---|
| `--theater <id>` | the theater persisted in `<store>/theater.json`, else `default` | A theater-table id (`choices` are the static table only). An explicit flag wins over the store and is re-persisted. |
| `--geodata on\|off` | `on` | On-demand map data over the network: place lookup, mapped sites and ground samples for new theaters. `off` keeps the app offline for these: places are given as coordinates, and a new area needs the operator's `ground_msl_m`. |
| `--real-data off\|direct\|gev` | flag absent: `$GODSEYE_REAL_DATA`, unset = off | Real-world data for the safety loop (terrain AGL and LOS, geofence floor, weather). `direct` fetches Re:Earth and Open-Meteo itself; `gev` uses God's Eye View's proxies at `$GODSEYE_GEV_ORIGIN`. An explicit `off` wins over the environment. |
| `--airframe quad_suas_electric\|group3_fixed_wing` | the restored theater's airframe, else `$GODSEYE_AIRFRAME`, else `quad_suas_electric` | The fuel-model profile. An explicit flag wins even over a restored theater's airframe. |
| `--wargame-mcp` | off | Also publish the simulated wargame's ten `wg_*` tools on `/mcp` for external harnesses (M14a). Off, `/mcp` has none: ISR by default. Engagements are still approved in the console only. |

The startup banner says whether the theater was restored from the store (with its epoch), prints a
WARNING line when the persisted theater could not be used (the default is booted instead and the
store's audit trail records `theater_restore_failed`), and adds a line
`map data : on|off (geocoding and mapped sites); real-data hydration on|off`. With `--wargame-mcp`
only, it also prints `wargame  : --wargame-mcp: simulated wg_* tools are also on /mcp; engagements
are still approved in the console only`. The repository's `./eye-in-the-sky` launcher forwards its
arguments, so `./eye-in-the-sky --wargame-mcp` works; `start.sh` passes a fixed argument list and
never sets it.

`godseye_uav.launch` (the older three-listener launcher) is unchanged and still used by tests.

## The in-app analyst

The analyst (`chat.py`) runs the Claude Code CLI through the Claude Agent SDK, one CLI process per
chat session, with no built-in tools and only an in-process MCP server named `godseye`
(`analyst_toolbelt.py`) whose tools are generated from the real server. Every call is classified by
`analyst_policy.py`: reads and dry runs run at once; sensor, command, sim and safety-override calls
wait for the operator's order slip in the console, and a simulated `engagement` asks every time,
with an acknowledgement, from the console that holds the engagement key. Its system prompt is the
base `analyst_prompt.md`, distilled from the skill below, followed by one identity file:
`analyst_prompt_isr.md` by default, or `analyst_prompt_wargame.md` during a simulated wargame
session (all three are package data).

It needs the `app` extra (`pip install -e '.[app]'`, which brings `claude-agent-sdk` and
`pywebview`) and a model provider: the Claude sign-in on this machine by default, or one chosen in
the console's Analyst settings (⌘,): an Anthropic API key, Bedrock, Vertex, Foundry, OpenRouter,
several Anthropic-compatible providers, a local Ollama or LM Studio, or a custom endpoint.
`llm_settings.py` owns those settings, the key store (the macOS Keychain or a 0600 file, never the
browser) and the CLI's environment; `llm_providers.py` is the catalog. Dev and test runs should set
`GODSEYE_LLM_SECRET_STORE=file`. `INTEL_CONSOLE.md` has the full contract: routes, SSE events,
approval classes, session grants, sign-in policy, providers and keys, environment variables, where
transcripts are written, and observed cost; the repository `README.md` lists the providers.

## Theaters anywhere, map data and sim speed

The simulated AO no longer has to be a row of the theater table (WG v2 Phase A). From chat, the
analyst looks a place up (`geo_lookup`, or takes coordinates), sizes an AO for the airframe
(`theater_propose`, which changes nothing), and asks to move the simulation there
(`sim_set_theater`). The operator approves that on a sim slip, every time. The switch parks every
drone, landed, at the new home, and moves the geofence, home and every fuel model's home. It moves
the three copies of the simulation origin together and cross-checks them. The new theater is
persisted in `<store>/theater.json`, so it survives a restart. `sim_set_time_scale` runs the fake
simulator up to ten times faster. A real place is **context only**: mapped strategic sites
(`geo_sites`, from OpenStreetMap) appear on the orb and the map, and are never targets.

- Tools: `TOOL_CONTRACT.md` §4.5 (the five new server tools, their refusals and budgets). The
  default `/mcp` catalog is 51 tools; nothing in Phase A is kinetic.
- Operator flow, slips, persistence, restart rules and audit rows: `INTEL_CONSOLE.md`, "Runtime
  theaters and sim speed".
- Upstreams, caches, the `--geodata` and `--real-data` switches, and attribution:
  `REAL_DATA_INTEGRATION.md` and `../THIRD_PARTY_NOTICES.md` §4.
- The bridge's side (`/snapshot.theater`, the geofence re-read, the flown-track reset):
  `BRIDGE_CONTRACT.md`, "Runtime theaters".

## The simulated wargame (M14a)

ISR is the default (PLAN.md §4.5a). An opt-in, operator-approved **simulated wargame session** adds
scenario forces (simulated units with generic designators such as "Red SAM 1"), notional
engagements between them, red air defence that can down drones, corridors and axes, battle-damage
re-looks and an after-action review. Nothing real is fired, and nothing real can be engaged: a
mapped site, a theater point, a `sim_spawn_*` object or real air traffic is refused by provenance,
and targets near mapped places are refused too. Drones never deliver effects; they fly recce and
re-looks with the ordinary ISR tools. Every wargame output carries `simulated: true`.

- **Code.** `wargame.py` (the engine, its red-adjudication thread and the propose → authorize →
  execute rules), `wargame_tables.py` and `wargame_adjudicate.py` (notional tables and draws),
  `wargame_vectors.py` (corridors and axes), `wargame_spawn.py`, `wargame_bda.py`, `wargame_aar.py`
  (the after-action review and `<store>/wargame.json` crash persistence), `wargame_tools.py` (the ten
  `wg_*` tools and `POST /wargame/session/end`) and `intel_scenario.py` (the wargame in the intel
  graph and on the map). `threat.py`, `missions.py` and `targets.OB_LIBRARY` stay ISR-only.
- **Tools.** `TOOL_CONTRACT.md` §4.6. They live on the server's own registry (`server.wargame_mcp`,
  never mounted). The in-app analyst gets the three entry tools (`wg_session_start`,
  `wg_session_status`, `wg_list_classes`) in ISR mode and all ten during a session. The default
  `/mcp` publishes none; `--wargame-mcp` publishes all ten for external harnesses.
- **Every engagement asks the operator.** `wg_execute_engagement` is class `engagement`: asked on
  every call, never session-grantable, acknowledgement required, never automatic. The chat service
  authorizes a pending engagement only after the console approves it with this launch's console key
  (`POST /app/console-claim`, claimed once) and the acknowledgement, and the engine executes it only
  inside that chat session's console context. `/mcp` and `/control/command` can never confirm one;
  the bridge refuses every `wg_*` tool with 403.
- **Operator flow, the console key, the Blue and Umpire views, the graph and map rows, the audit
  rows and the known limits:** `INTEL_CONSOLE.md`, "Simulated wargame (M14a)".

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
the server's own safety gates apply to every caller. That includes `sim_set_theater`: an `/mcp`
caller can switch the theater with the same refusals, and the theater records `set_via: "mcp"`.
The simulated wargame's `wg_*` tools are on `/mcp` only when the app runs with `--wargame-mcp`, and
even then an `/mcp` caller can never confirm an engagement: `wg_execute_engagement` answers
`engagement_requires_console_approval`. The skill below stays ISR-only and never calls them.

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
- **Theater switch**: refused unless every drone is proven landed, idle, not BINGO-latched and
  linked, no forced RTB is flying, the sim is the built-in fake, the host is the app host and
  restart recovery has finished. While a switch runs, and after a plan whose gate was checked in an
  earlier theater, a submit is refused with `theater_changed`. If the origin copies fail their
  cross-check, every copy is put back at the old origin, the safety monitor enforces nothing, and
  every submit except `uav_land` and `uav_hover` is refused with `theater_integrity` until a
  restart. Once the origin starts to move, a cancelled caller (Stop, a closed chat) cannot leave the
  switch half-done: it runs to its end. What the monitor's caches already disprove (a drone
  airborne or busy, say) is refused before the switch blocks ticks and commands.
- **Restart**: an interrupted route is re-gated against the booted theater before it resumes; one
  planned in another theater is aborted with a forced RTB (`restart_resume_regate_failed`).
- **Simulated wargame** (M14a): a drone red air defence downs is lost until the session ends (its
  task is aborted and every submit for it is refused with `vehicle_lost`); a theater switch and
  `sim_reset` are refused while a session runs; ending the session parks every downed drone at
  home, landed. A restart during a session never resumes it: the next boot deletes its scenario
  tracks and files a partial after-action review.

## Layout

| Path | What |
|---|---|
| `mcp/godseye_uav/app.py`, `host.py` | Entry point (window, browser, headless, self-test) and the single-process host |
| `mcp/godseye_uav/server.py` | MCP server: 51 tools, 8 resources (plus the ten `wg_*` tools under `--wargame-mcp`) |
| `mcp/godseye_uav/bridge.py` | Telemetry bridge |
| `mcp/godseye_uav/intel_graph.py`, `intel_sites.py`, `intel_overlay.py`, `intel_scenario.py` | Intel graph and `/intel/*` routes; mapped-site nodes; the map's `/intel/overlay` feed; the simulated wargame's rows in both |
| `mcp/godseye_uav/theaters.py`, `theater_plan.py`, `theater_switch.py`, `theater_tools.py` | Theater table and dynamic registry; proposals; the switch and `theater.json`; the five theater and sim-speed tools |
| `mcp/godseye_uav/geo_http.py`, `geocode.py`, `sites.py` | Direct map-data upstreams (User-Agent, rate gates, cache, egress switch); Photon/Nominatim lookup; OpenStreetMap sites |
| `mcp/godseye_uav/chat.py`, `analyst_policy.py`, `analyst_toolbelt.py`, `analyst_prompt.md`, `analyst_prompt_isr.md`, `analyst_prompt_wargame.md` | The analyst: service, approval policy, toolbelt, and the system prompt (base plus one identity per mode) |
| `mcp/godseye_uav/wargame.py`, `wargame_tables.py`, `wargame_adjudicate.py`, `wargame_vectors.py`, `wargame_spawn.py`, `wargame_bda.py`, `wargame_aar.py`, `wargame_tools.py` | The opt-in simulated wargame (M14a): engine, notional tables, corridors, placement, battle damage, after-action review, the `wg_*` tools and the End route |
| `mcp/godseye_uav/llm_settings.py`, `llm_providers.py` | The analyst's model providers: `/settings/llm*`, keys, connection checks, the catalog |
| `mcp/godseye_uav/` (rest) | Safety, tasking, missions, targets, threat, geo, store, theaters, real data, fake sim |
| `packaging/macos/` | PyInstaller spec, entry point and icon (built by `../scripts/build_desktop.sh`) |
| `tests/` | Test suite; runs with no GPU and no Unreal |
| `scripts/` | `demo_laptop.sh` (one-command demo), `demo_mission.py`, `ci.sh`, `_airsim_client.sh` |
| `.agents/skills/godseye-uav/` | The harness skill |
| `INTEL_CONSOLE.md` | Host, HTTP and SSE contract, approvals, analyst, the simulated wargame's console side, data honesty |
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
2,488 tests collected when the WG v2 Phase A docs were written. The last timed full run (1,767
tests) took about 40 minutes, most of it `test_server.py`. The analyst and window tests mostly use
fakes for `claude_agent_sdk` and `webview`; the few that need the real SDK skip without it, so the
suite runs without the `app` extra. One test runs the
built desktop app and is opt-in: `GODSEYE_TEST_DESKTOP_APP=1 .venv/bin/python -m pytest
tests/test_app.py -k built_app` after `../scripts/build_desktop.sh`. The provider checks in
`tests/test_llm_settings_live.py` run the real bundled CLI against a local stub behind a deny-all
proxy, with fake keys (`GODSEYE_LIVE_CLI=0` skips them); `GODSEYE_TEST_KEYCHAIN=1` adds a round trip
through a throwaway Keychain.

**No test reaches the internet.** `tests/conftest.py` installs an egress guard for the whole
session: it sets `GODSEYE_NO_EGRESS=1` (subprocess hosts inherit it, and the direct map-data
client refuses before opening a socket), and it makes `socket.socket.connect`, `connect_ex` and
`socket.create_connection` raise `OSError("egress blocked in tests: …")` for any address that is
not local. Local means loopback, `localhost`, the unspecified address and this machine's own
interface address; other host names are refused without a DNS lookup, and a UDP `connect()`
(which sends nothing) is allowed. Every HTTP client in the geodata code is injectable, and the
tests use hand-written fixtures in `tests/fixtures/geodata/`.

A test that really needs the network is marked `@pytest.mark.live_net` (registered in
`pyproject.toml`). It is skipped unless `GODSEYE_LIVE_NET=1`, which lifts both measures for that
test only. A `live_net` test that needs a networked subprocess host must start it inside the test
or a function-scoped fixture: a module-scoped fixture starts before the guard is lifted.

## Datum: read this before touching altitudes

Altitude is the highest-risk area in the system. There is exactly one conversion point,
`geo.canonical_altitude()`, and no module may do its own geoid math.

- Origin altitudes are entered as MSL and converted once at ingest. A runtime theater stores its
  ground as MSL, and each activation (a switch, or the boot) converts it to HAE exactly once; the
  switch then moves the fake sim's, the MCP backend's and the bridge's origin copies to that one
  point and cross-checks them.
- `altHae = altMSL + N(φ, λ)`, where `N` is the EGM96 geoid undulation: negative where the geoid is
  below the ellipsoid (−22.21 m at the Redmond origin, +1.58 m near Isfahan).
- Every altitude field names its datum: `alt_hae_m`, `alt_msl_m`, `alt_agl_m`. Never a bare `alt_m`.
- If an accurate geoid source cannot be loaded the code raises rather than silently degrading; a
  degraded datum is surfaced to the operator, never hidden.

## Known limits

- The UAV is simulated; only the environment data can be real. See `REAL_DATA_INTEGRATION.md`.
- The real-data layer (hydration of the safety loop) is off by default (`--real-data`,
  `GODSEYE_REAL_DATA`). With it off, `alt_agl_m` is height above the launch datum, LOS is geometric
  and there is no live air traffic; every such value says so (`alt_agl_is_real`, `los_is_measured`,
  `traffic_is_real`). `--real-data direct` gives terrain and weather without God's Eye View, but no
  mapped installations or live traffic; `--real-data gev` needs God's Eye View's `/api` providers,
  which only its vite dev server hosts. Map data for new theaters (`--geodata`) is a separate
  switch and is on in the app.
- Geo-registration is certified to a measured ~900 m radius from the origin: the ported AirSim math
  mixes a spherical NED→geodetic with an ellipsoidal geodetic→NED, so horizontal error grows with
  range (worst ≈5 m/km near the equator). Beyond that radius, re-anchor the origin. A chat theater
  is much larger than that: its AO half-extent is 1.5–2.94 km for the quad and up to 25 km for the
  group-3 profile, so positions near the edge of a large AO carry that error.
- The group-3 fixed-wing profile changes the fuel model only; the fake simulator still flies
  multirotor kinematics (hover, 20 m/s cap), and a proposal says so.
- A runtime theater switch needs the built-in fake simulator and the app host. Under a real AirSim
  the origin is fixed by its `settings.json`: switches are refused, and a persisted chat theater is
  not restored at boot.
- Sim speed is not persisted: a restart runs at ×1. Link-loss timers, detections and scans, and the
  analyst's clock stay on wall time at any speed.
- OpenSky is licensed for non-commercial research/education use; several other feeds carry
  attribution requirements. See `../gods-eye-view/DATA_SOURCES.md`.
- The analyst and host have their own list in `INTEL_CONSOLE.md`, "Known limits".
