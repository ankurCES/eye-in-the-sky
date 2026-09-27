# Eye in the Sky

Eye in the Sky is a simulated UAV ISR (intelligence, surveillance, reconnaissance) workstation. It
opens on an **intelligence console**: a 3D orb of every intel entity the sim knows about, a search
and ask bar, a situation rail, an entity inspector and an **AI analyst** that answers from the data
and runs drone missions. Anything the analyst wants to do that moves an aircraft, tasks a sensor or
changes the sim waits for the operator to approve an order slip. The map (God's Eye View on Cesium)
appears only in tracking mode, following one drone.

Everything runs as **one Python process on one origin**: the simulator (AirSim, or a built-in fake
that needs no GPU), the MCP server that is the only command path, the telemetry bridge, the intel
graph, the analyst and the console UI. It can run in a native window, in your browser, headless, or
as a packaged macOS app.

**ISR-only.** There are no kinetic tools anywhere in the system, and none may be added. It observes,
classifies and reports. Threat output is sensor-posture advice only; command authority stays with
the operator.

## Contents

- [Quick start](#quick-start)
- [What you get](#what-you-get)
- [Running modes and options](#running-modes-and-options)
- [Desktop app (macOS)](#desktop-app-macos)
- [Classic dev stack](#classic-dev-stack)
- [Connecting an external agent over MCP](#connecting-an-external-agent-over-mcp)
- [Architecture](#architecture)
- [Safety model](#safety-model)
- [What is real and what is simulated](#what-is-real-and-what-is-simulated)
- [Datum: read before touching altitudes](#datum-read-before-touching-altitudes)
- [Tests](#tests)
- [Layout](#layout)
- [Known limits](#known-limits)
- [Licence and third-party notices](#licence-and-third-party-notices)

## Quick start

Needs Python 3.11 or later, git (to fetch the pinned AirSim client) and, for the UI, Node
(`gods-eye-view/package.json` asks for `>=24.14.0 <25 || >=26 <27`). No GPU and no Unreal.

```bash
./scripts/setup.sh --with-ui                                   # venv, godseye-uav[dev], AirSim client, npm install
godseye/.venv/bin/python -m pip install -e './godseye[app]'    # the analyst and the native window
./eye-in-the-sky                                               # builds the UI if needed, then opens the app
```

`setup.sh` installs only the `dev` extra. The `app` extra adds `claude-agent-sdk` (the analyst; it
bundles the Claude Code CLI) and `pywebview` (the native window). Without it the app still runs: it
opens in your browser instead of a window, and the analyst reports itself unavailable.

`./eye-in-the-sky` runs `npm run build` in `gods-eye-view/` when `dist/` is missing or older than its
sources, then starts `python -m godseye_uav.app`. By default that opens a native window (pywebview,
WKWebView on macOS) when pywebview is installed and a display is available, and your browser
otherwise.

**Analyst sign-in.** The analyst uses the Claude Code CLI. On your own machine, sign in once with
`claude` then `/login`, or set `ANTHROPIC_API_KEY` in the app's environment. A build you give to
anyone else must use API-key authentication, not your Claude login; see
[`godseye/INTEL_CONSOLE.md`](godseye/INTEL_CONSOLE.md#analyst-sign-in-and-anthropics-policy).

## What you get

- **The orb**: vehicles, missions, contacts, units, equipment classes, reports, theater and POIs,
  alarms and feeds, drawn as one sphere. Search with ⌘K, Ctrl+K or `/`; the last result row sends
  your text to the analyst.
- **The situation rail**: theater, sim status, fleet fuel against BINGO, running missions, alarms and
  data caveats. Track and Abort act directly (Abort asks once to confirm).
- **The inspector**: one entity's fields, with measured and assumed values marked, its related
  entities and its actions.
- **The analyst**: a chat that reads the same intel graph, cites entities as chips, dry-runs before
  it flies, and puts every sensor, flight, sim or safety-override call in front of you as an order
  slip. It follows the same doctrine as the `godseye-uav` skill.
- **Tracking mode**: the God's Eye View Cesium map and cockpit view, following one drone. Entered by
  Track, by the analyst, or when a mission you approved launches; Esc or "Back to console" returns
  to the orb.

`?console=off` on the page URL skips the console and loads the plain God's Eye View map application.

The full HTTP and SSE contract, approval classes, order-slip rules and data-honesty rules are in
[`godseye/INTEL_CONSOLE.md`](godseye/INTEL_CONSOLE.md).

## Running modes and options

```bash
./eye-in-the-sky                 # native window if pywebview is installed and a display is available
./eye-in-the-sky --browser       # open the console in the default browser
./eye-in-the-sky --headless      # serve only; open nothing
./eye-in-the-sky --no-build ...  # skip the UI build check
./eye-in-the-sky --help          # every option
```

The launcher passes every other argument to `godseye_uav.app` (also installed as the `godseye-app`
command). The main options:

| Option | Default | Meaning |
|---|---|---|
| `--theater <id>` | `default` (Redmond) | AO preset from `godseye_uav/theaters.py`: `default`, `iran-isfahan`, `iran-natanz`, `iran-fordow`, `indo-pak-loc`, `taiwan-strait`, `ukraine-donbas`, `red-sea-hormuz` |
| `--port <n>` | `8780` | UI, bridge, intel, chat and `/mcp` all share it. If 8780 is taken and you did not pass `--port`, window and browser modes move to a free port. |
| `--mcp-port <n>` | none | Serve the same app on a second port too (the legacy MCP URL, for example 8791) |
| `--host` | `127.0.0.1` | Loopback only: `127.0.0.1`, `localhost` or `::1`. Anything else is refused, because the page carries the token. |
| `--token <tok>` | `$GODSEYE_TOKEN`, else random per launch | Bearer token for the API and MCP. It is never printed. |
| `--store <dir>` | `~/Library/Application Support/EyeInTheSky/store` on macOS | Tracks, missions, audit and fuel journal. Two hosts cannot share one store. |
| `--ui-dir <dir>` | `$GODSEYE_UI_DIR`, else `gods-eye-view/dist` | The built UI |
| `--real`, `--sim-port <n>` | fake sim on a free port | Connect to a real AirSim instead (port 41451 with `--real`) |
| `--no-chat` | analyst on | Turn the analyst off |
| `--model <id>`, `--effort <level>` | `$GODSEYE_CHAT_MODEL` or `claude-opus-5`; `$GODSEYE_CHAT_EFFORT` or the model's default | Analyst model and reasoning effort (`low`, `medium`, `high`, `xhigh`, `max`) |
| `--debug` | off | Web inspector in window mode |
| `--selftest` | off | Packaging check; see the desktop section |

On start the app prints the app URL, the MCP URL, where the token came from (not the token), the
store, whether the UI is built and whether the analyst is available. It writes the MCP URL and token
for external agents to `<store>/../mcp.json` (mode 0600).

## Desktop app (macOS)

```bash
godseye/.venv/bin/python -m pip install -e './godseye[app,desktop]'   # adds PyInstaller
./scripts/build_desktop.sh [--dmg] [--selftest] [--no-cli] [--no-ui-build] [--bake-keys] [--clean]
```

This builds `dist/Eye in the Sky.app` (and `dist/Eye in the Sky.dmg` with `--dmg`). It needs macOS,
the Xcode command line tools (`codesign`, `ditto`, `otool`), npm with `gods-eye-view/node_modules`,
`msgpack` below 1.0 (the AirSim transport needs it) and about 1 GB of free disk.

| Flag | Effect |
|---|---|
| `--dmg` | Also build a compressed disk image with an `/Applications` link |
| `--selftest` | Launch the built app once with `--selftest` (a window opens for a few seconds; temporary store, free port) and fail the build if it fails |
| `--no-cli` | Do not bundle the Claude CLI (225 MB); the analyst then looks for `claude` on the user's machine |
| `--no-ui-build` | Reuse the UI staged by the previous build (`godseye/build/desktop/ui`) |
| `--bake-keys` | Let the UI build read `GOOGLE_MAPS_API_KEY` and `CESIUM_ION_TOKEN`. By default the UI is built without them, and the build fails if either value turns up in the bundle. |
| `--clean` | Drop PyInstaller's cache and work directory first |

How it is put together:

- PyInstaller builds a windowed, onedir app (bundle id `io.eyeinthesky.console`) from
  `godseye/packaging/macos/EyeInTheSky.spec`. The UI is built into `godseye/build/desktop/ui`, never
  into `gods-eye-view/dist`.
- The Agent SDK's bundled Claude CLI is copied to `Contents/Helpers/claude` after PyInstaller, so it
  keeps Anthropic's signature, and the app points the analyst at it. Only the outer app is re-signed,
  ad hoc, then checked with `codesign --verify --deep --strict`.
- Measured on the build machine: 61–66 s for a full build (the DMG adds about 14 s), 329 MB for the
  app (115 MB without the CLI), 165 MB for the DMG.

What the build is and is not:

- It is arm64 only and needs the macOS version the Python it was built with requires (macOS 26 on
  the build machine; `LSMinimumSystemVersion` is read from libpython).
- It is ad-hoc signed and not notarized, so it is meant for the machine that built it. A copy on
  another Mac is blocked by Gatekeeper until you allow it in System Settings → Privacy & Security
  ("Open Anyway"). Distributing it properly needs a Developer ID signature and notarization.
- Launched from Finder, the app has no terminal, so its output goes to
  `~/Library/Application Support/EyeInTheSky/logs/eye-in-the-sky.log` (mode 0600, rotated at 5 MB).
  Its store is `~/Library/Application Support/EyeInTheSky/store` and its harness config
  `~/Library/Application Support/EyeInTheSky/mcp.json`. WebKit keeps page data in
  `~/Library/WebKit/io.eyeinthesky.console` and `~/Library/Caches/io.eyeinthesky.console`.
- A Finder launch does not see variables from your shell profile. To give the packaged app an
  `ANTHROPIC_API_KEY`, start `"dist/Eye in the Sky.app/Contents/MacOS/EyeInTheSky"` from a terminal.
- The UI inside the app is a snapshot of `gods-eye-view/` at build time; rebuild after UI changes.
- Voice input is not available in the app: it depends on God's Eye View's Node dev server.

**Self-test.** `--selftest` opens the window and checks, from inside the page, that the console
mounted, WebGL2 works, `/app/config` answers and the console fetched the intel graph; Python checks
that the UI, the analyst prompt and the geoid grid were packaged. It writes a JSON verdict (to
`--selftest-out PATH`, else stdout; it never contains the token) and exits 0 or 1 within
`--selftest-timeout` seconds (default 60):

```bash
"dist/Eye in the Sky.app/Contents/MacOS/EyeInTheSky" --selftest --selftest-out /tmp/verdict.json
cd godseye && GODSEYE_TEST_DESKTOP_APP=1 .venv/bin/python -m pytest tests/test_app.py -k built_app
```

## Classic dev stack

For UI work with hot reload, or to keep the old ports:

```bash
cd godseye && ./start.sh
```

`start.sh` runs the same single-process host headless (`godseye_uav.app --headless`) on the bridge
port and serves the same app again on the MCP port, then starts the God's Eye View vite dev server:

| What | URL |
|---|---|
| Console, vite dev UI (hot reload) | `http://localhost:4173` |
| Console, built UI (after `npm run build`) | `http://127.0.0.1:8790/` |
| Telemetry bridge | `http://127.0.0.1:8790/snapshot` |
| MCP | `http://127.0.0.1:8791/mcp` (also `http://127.0.0.1:8790/mcp`) |
| Fake AirSim (msgpack-rpc, loopback) | `127.0.0.1:41451` |

Environment: `THEATER` (default from `theaters.py`, which is `default`), `SIM_BACKEND` (`fake` or
`real`), `AIRSIM_PORT` (41451), `BRIDGE_PORT` (8790), `MCP_PORT` (8791), `UI_PORT` (4173), `TOKEN`
(`dev-token`) and `STORE` (`godseye/.godseye/store`). The token is passed to the app as
`GODSEYE_TOKEN`, not on its command line. The dev UI finds the bridge at `http://localhost:8790`
with `dev-token` unless `window.__GODSEYE__`, `localStorage` (`gev.uav.base`, `gev.uav.token`) or
`VITE_UAV_BRIDGE_URL` / `VITE_UAV_BRIDGE_TOKEN` say otherwise.

`godseye/scripts/demo_laptop.sh` boots this stack, waits for it, opens `http://localhost:4173` and
flies a scripted recon mission over MCP. The page opens on the console; press Track on the rail to
watch the drone on the map, or open `http://localhost:4173/?console=off` for the plain map.

## Connecting an external agent over MCP

The MCP server speaks Streamable HTTP with bearer auth at `/mcp` on the app's port. It has 46 tools
and 8 resources (`godseye/TOOL_CONTRACT.md`). The `godseye-uav` skill in
`godseye/.agents/skills/godseye-uav/` is the operating manual for an agent flying through it.

- Against `start.sh`, `godseye/.mcp.json` already points at `http://127.0.0.1:8791/mcp` with
  `Bearer ${GODSEYE_MCP_TOKEN:-dev-token}`.
- Against the app, use the file it writes: `<store>/../mcp.json` (for the desktop default,
  `~/Library/Application Support/EyeInTheSky/mcp.json`). It holds the MCP URL and the per-launch
  token, mode 0600, and works as a Claude Code `--mcp-config` file.

Calls that arrive over `/mcp` do not go through the console's order slips; those are for the in-app
analyst. The server's own safety gates apply to every caller.

From Python with the `mcp` SDK:

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

`godseye/scripts/demo_mission.py` is a complete worked example over the real transport.

## Architecture

```
 python -m godseye_uav.app   (./eye-in-the-sky, godseye-app, the .app, start.sh)
 ┌──────────────────── one FastAPI app, one asyncio loop, 127.0.0.1 only ────────────────────┐
 │  /                  console UI (gods-eye-view build), token injected into index.html       │
 │  /app/config /intel/* /chat/*                     host routes                              │
 │  /mcp               godseye MCP server (46 tools, 8 resources)   <── external agents        │
 │  /health /snapshot /mission-overlay /theaters /events /tracks /control/* /camera/*  bridge │
 │  analyst: one Claude CLI child per chat session, tools called in-process                   │
 └──────────────┬──────────────────────────────────────────────────────────────────────────────┘
                │ msgpack-rpc, loopback only
        fake AirSim (in-process threads)  or  a real AirSim with --real
```

- The MCP server is the only command path. The bridge's `/control/*` routes only forward to it for
  the God's Eye View mission panel and the console's direct Abort.
- The analyst's tools are generated from the real MCP server and called in-process; they carry no
  token on any command line. Its approval policy is in `godseye/mcp/godseye_uav/analyst_policy.py`.
- One in-flight command per vehicle, enforced by a FIFO queue with the state machine
  `idle → executing → cancelling → aborting`.
- Every mutating tool accepts `idempotency_key`; replaying a key returns the original handle.
- Every altitude names its datum: `alt_hae_m`, `alt_msl_m`, `alt_agl_m`. Never a bare `alt_m`.

Detail: [`godseye/INTEL_CONSOLE.md`](godseye/INTEL_CONSOLE.md) (host, chat, approvals),
[`godseye/BRIDGE_CONTRACT.md`](godseye/BRIDGE_CONTRACT.md) (bridge feeds),
[`godseye/TOOL_CONTRACT.md`](godseye/TOOL_CONTRACT.md) (MCP tools).

## Safety model

The server owns safety. No caller can override it, the analyst included; a skill's rules of
engagement may only be stricter.

- **Geofence, ceiling, minimum AGL and maximum speed** are checked in flight, not only at plan time.
- **BINGO fuel**: every plan must pass a pre-flight gate (plan + return leg + 20% reserve ≤ fuel).
  Reaching BINGO forces a return to home that cannot be cancelled, and the mission is flagged
  `incomplete - fuel`.
- **Lost link**: the server runs the mission's lost-link plan on its own (hold, climb for LOS, return
  or continue) and logs the event into the INTREP.
- **Busy**: a second command for a busy vehicle returns `{"status":"busy", "current": <handle>}`.
- **Operator approval** (in-app analyst only): read and dry-run calls run at once; sensor, command,
  sim and safety-override calls wait for an order slip. Only sensor tools can be allowed for a whole
  session.

## What is real and what is simulated

| Layer | Source |
|---|---|
| UAV physics and sensors | Simulated: AirSim multirotor dynamics and cameras, or the built-in fake |
| Detections | Simulated: AirSim ground truth (`simGetDetections`) |
| Terrain | Real (Re:Earth / Mapterhorn, EGM2008) when the real-data layer is on; otherwise AGL is height above the launch datum |
| Weather and wind | Real (Open-Meteo) when the real-data layer is on |
| Air traffic | Real (OpenSky / adsb.lol) when the real-data layer is on; otherwise there is none |
| Mapped installations | OSM / Overpass, context only, when the real-data layer is on |
| Globe imagery (tracking mode) | Google Photorealistic 3D Tiles and Esri World Imagery, under their own keys and terms |
| Geoid | EGM96 (NGA, public domain), shipped as package data |

The real-data layer is off by default (`GODSEYE_REAL_DATA`) and needs God's Eye View's `/api`
providers, which only its vite dev server hosts. Every value that depends on it carries provenance
(`alt_agl_is_real`, `los_is_measured`, `traffic_is_real`, `wind_known`), and the console and the
analyst say when a value was assumed. See `godseye/REAL_DATA_INTEGRATION.md`.

The two upstream projects this builds on:

- [**Microsoft AirSim**](https://github.com/microsoft/AirSim) (MIT), pinned at
  `1ca93f6f77e4e8a39b2b241c1fe2764da4d7dd41`, is the physics and sensor backend, reached over
  msgpack-rpc on loopback. `godseye/mcp/godseye_uav/fake_airsim.py` answers the same API surface
  without Unreal or a GPU; the tests and demos run against it.
- [**God's Eye View**](https://github.com/bilawalsidhu/gods-eye-view) (MIT, © 2026 Bilawal Sidhu) is
  vendored in `gods-eye-view/`. It provides the Cesium globe and cockpit view that tracking mode
  shows. This project's additions there include the UAV layer (`src/layers/uav/`, `src/ui/uav*.js`,
  `src/sources/live/uav*.js`), the intelligence console (`src/console/`) and the tracking port
  (`src/app/trackingPort.js`).

## Datum: read before touching altitudes

Altitude is the highest-risk area in the system. There is exactly one conversion point,
`geo.canonical_altitude()`, and no module may do its own geoid math.

- Origin altitudes are entered as MSL and converted once at ingest.
- `altHae = altMSL + N(φ, λ)`, where `N` is the EGM96 geoid undulation: negative where the geoid is
  below the ellipsoid (−22.21 m at the Redmond origin, +1.58 m near Isfahan).
- If no accurate geoid source can be loaded the code raises rather than degrading silently; a
  degraded datum is shown to the operator.

## Tests

```bash
cd godseye && .venv/bin/python -m pytest tests -q        # Python, against the fake AirSim
cd gods-eye-view && npm test                             # JavaScript (node:test)
cd gods-eye-view && npm run check:boundaries && npm run format:check && npm run build
```

Final runs for this release:

- **Python**: 1,522 tests. The full run gave 1,515 passed, 1 skipped (the opt-in desktop test) and
  6 failed while it shared the machine with a UI build, `npm test` and a live host: two window-mode
  signal tests, two timing tests in `test_server.py` and two in `test_tasking.py`. All six pass on
  their own, and a rerun of those three files passed in full (300 passed, 1 skipped). The full suite
  takes about 40 minutes, most of it `test_server.py`.
- **JavaScript**: 4,883 tests, 4,882 passed, 1 skipped, in about 2 minutes. The run used Node 22,
  so two allocation microbenchmarks calibrated for Node 24 were skipped.

## Layout

| Path | What |
|---|---|
| `eye-in-the-sky` | Launcher: builds the UI if needed, runs `godseye_uav.app` |
| `scripts/setup.sh` | Makes a fresh clone runnable (`--with-ui` also runs `npm install`) |
| `scripts/build_desktop.sh` | Builds the macOS app |
| `godseye/mcp/godseye_uav/app.py`, `host.py` | Entry point and the single-process host |
| `godseye/mcp/godseye_uav/intel_graph.py` | The intel graph behind the orb, search, inspector and analyst |
| `godseye/mcp/godseye_uav/chat.py`, `analyst_policy.py`, `analyst_toolbelt.py`, `analyst_prompt.md` | The analyst: sessions and SSE, approval policy, tools, system prompt |
| `godseye/mcp/godseye_uav/server.py`, `bridge.py` | MCP server and telemetry bridge |
| `godseye/mcp/godseye_uav/` (rest) | Safety envelope, tasking, missions, targets, threat, geo, store, theaters, fake AirSim |
| `godseye/packaging/macos/` | PyInstaller spec, entry point and icon for the app |
| `godseye/tests/` | Python tests; no GPU, no Unreal |
| `godseye/scripts/` | `demo_laptop.sh`, `demo_mission.py`, `ci.sh`, `_airsim_client.sh` |
| `godseye/start.sh` | Classic dev stack |
| `godseye/.agents/skills/godseye-uav/` | The agent skill (symlinked from `godseye/.claude/skills/`) |
| `godseye/INTEL_CONSOLE.md` | Host, HTTP and SSE contract, approvals, analyst, data honesty |
| `godseye/TOOL_CONTRACT.md`, `godseye/BRIDGE_CONTRACT.md` | MCP tool contract, bridge feed contract |
| `godseye/REAL_DATA_INTEGRATION.md` | Which real-world data the sim can consume, and its limits |
| `godseye/PLAN.md`, `godseye/GAP_REGISTER.md`, `godseye/INTEGRATION_FINDINGS.md` | Design, audit and integration history |
| `gods-eye-view/` | God's Eye View (vendored) plus this project's UAV layer and console (`src/console/`) |
| `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md` | Licence and notices |

## Known limits

- The UAV is simulated (AirSim or the fake); only environment data can be real, and that layer is off
  by default.
- God's Eye View's node-only `/api` providers (live aircraft, vessels, CCTV, terrain and others) are
  not served by the app host (`/api/*` answers 404 `not_available_in_app_host`). Use the classic dev
  stack for them.
- Geo-registration is certified to a measured ~900 m radius from the origin; beyond that, re-anchor
  the origin.
- Chat sessions and session grants live in memory and end with the host. The Claude CLI keeps its
  own transcripts under `~/.claude/projects/` (see `godseye/INTEL_CONSOLE.md`).
- Fonts load from Google Fonts; offline, the browser falls back to other fonts.
- The desktop app is ad-hoc signed, not notarized and arm64 only; it needs the macOS version its
  Python was built for (macOS 26 on the build machine).
- OpenSky is licensed for non-commercial research and education; several other feeds need
  attribution. See `gods-eye-view/DATA_SOURCES.md` and `THIRD_PARTY_NOTICES.md`.
- More detail: "Known limits" in `godseye/INTEL_CONSOLE.md` and `godseye/GAP_REGISTER.md`.

## Licence and third-party notices

This project is Apache-2.0 (`LICENSE`, `NOTICE`). Copyright © 2026 Ankur Nair.

It bundles and derives from third-party software and data under their own licences. Read
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) before making this repository public, using it
commercially or giving anyone a build. The main components:

| Component | Licence | Use |
|---|---|---|
| Microsoft AirSim | MIT | Physics and sensor backend; `geo.py` ports AirSim's EarthUtils |
| God's Eye View | MIT (© 2026 Bilawal Sidhu) | Vendored map and cockpit UI |
| Claude Agent SDK (`claude-agent-sdk`) | MIT for the Python package; use governed by Anthropic's Commercial Terms of Service | The analyst; its bundled Claude Code CLI is copied into the desktop app |
| pywebview | BSD 3-Clause | Native window |
| PyInstaller | GPL-2.0-or-later with a bootloader exception | Builds the desktop app (not shipped as a library) |
| Atkinson Hyperlegible Next and Mono | SIL Open Font License 1.1 | Console fonts, loaded from Google Fonts |
| EGM96 geoid grid | NGA, public domain | Package data for `canonical_altitude()` |
| OpenSky Network | Non-commercial research and education | Live air traffic (real-data layer) |
| TeleGeography submarine cables | CC BY-NC-SA 3.0 | Dataset bundled in `gods-eye-view/` |
| Bhote Koshi event data | CC BY-NC 4.0 | Dataset bundled in `gods-eye-view/` |
