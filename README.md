# Eye in the Sky — Agentic UAV ISR Mission Simulation

**An AI-powered drone simulation platform.** An LLM agent harness flies reconnaissance, target-identification, and threat-assessment missions over **real-world terrain and data**, while an operator watches live on a photorealistic 3D globe. The simulation fuses three open-source giants: **Microsoft AirSim** for UAV physics and sensors, **Bilawal Sidhu's God's Eye View** for the photorealistic Cesium 3D command center, and a **Python MCP server** as the AI agent's command path.

**ISR-only.** There are no kinetic tools anywhere in the system, and none may be added. It observes, classifies, and reports; command authority stays with the operator.

---

## Table of Contents

- [What this repo does](#what-this-repo-does)
- [How God's Eye View (Bilawal Sidhu) enhances the engine](#how-gods-eye-view-bilawal-sidhu-enhances-the-engine)
- [How Microsoft AirSim drives the UAV](#how-microsoft-airsim-drives-the-uav)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Connecting an AI agent](#connecting-an-ai-agent)
- [The skill — how the AI flies](#the-skill--how-the-ai-flies)
- [Safety model](#safety-model)
- [Realism — what's real, what's simulated](#realism--whats-real-whats-simulated)
- [Layout](#layout)
- [Tests](#tests)
- [Datum — read before touching altitudes](#datum--read-before-touching-altitudes)
- [Known limits](#known-limits)
- [Licence and third-party notices](#licence-and-third-party-notices)

---

## What this repo does

`eye-in-the-sky` is a complete UAV ISR (Intelligence, Surveillance, Reconnaissance) simulation platform. It enables an AI agent — any LLM loaded with the `godseye-uav` skill — to plan and execute reconnaissance missions over real-world geography. The AI agent:

1. **Receives a mission task** from a human operator (e.g., "search this polygon for armored vehicles").
2. **Plans the flight** — altitude, sensor, overlap, speed — derived from doctrine, not guesswork.
3. **Dry-runs the plan** against the server's safety gates (geofence, BINGO fuel, min-AGL, ceiling, max-speed) *without moving the aircraft*.
4. **Executes the mission** if the gate passes — the server flies the UAV, captures imagery and telemetry, detects and tracks contacts, and assesses threats.
5. **Reports** in SALUTE per-contact and INTREP mission-summary format — with evidence, confidence levels, and honest gap analysis.

The entire mission is **visible live** in the God's Eye View browser command center: the drone on a photorealistic 3D globe, the planned route, the grid pattern, tracked contacts as numbered markers, a live sensor picture-in-picture, and HUD instruments showing fuel, progress, BINGO margins, and altitude. Alarms (geofence proximity, BINGO, lost link) fire as SSE toasts.

---

## How God's Eye View (Bilawal Sidhu) enhances the engine

[**God's Eye View**](https://github.com/bilawalsidhu/gods-eye-view) (MIT, Copyright © 2026 Bilawal Sidhu) is an open-source Cesium-based photorealistic 3D globe command center. It is **vendored in full** under `gods-eye-view/` and serves as the **operator's window into every mission**.

| Capability God's Eye View provides | How it enhances the drone sim |
|---|---|
| **Photorealistic 3D globe** (Google Photorealistic 3D Tiles + Esri World Imagery) | The operator sees the drone flying over the *actual terrain* of the theater — Isfahan, Donbas, Redmond — not a stylized map. Every ridgeline, building, and road is real. |
| **Real terrain elevation** (Re:Earth/Mapterhorn, EGM2008 geoid) | Feeds the sim's AGL computation, terrain-aware line-of-sight checks, geofence floor, and terrain-following routes. An aircraft over the Fordow ridge reads `alt_agl_m = −42.2` — 42 m *below* the ridge — not a flat-earth guess. |
| **Live air traffic** (OpenSky/adsb.lol) | Real aircraft in the AO become airspace contacts for deconfliction — the AI agent must check airspace before climbing. |
| **Real weather and wind** (Open-Meteo) | Drives the wind vector fed into the fuel model and sensor degradation (visibility, cloud). The difference between a headwind and a tailwind is fuel, and fuel is safety. |
| **Real mapped installations** (OSM/Overpass) | Seeds the order-of-battle for each theater — plausible target sets anchored to real places, not hand-placed meshes. |
| **CustomDataSource mission overlays** | Routes, flown tracks, grid patterns, coverage polygons, threat rings, and geofence boundaries are rendered as native Cesium geometry on the globe — not as HTML divs floating over it. |
| **Picture-in-picture sensor feed** | The UAV's camera stream appears as a live PIP in the command center — what the drone sees, the operator sees. |
| **HUD and mission panel** | Progress, coverage (flown), fuel vs BINGO, ETA, waypoint index, GEOFENCE status, altitude (MSL + AGL), contact count — all live. |

The **UAV layer** (`gods-eye-view/src/layers/uav/`) is this project's integration — ~32 files, ~9,200 lines — that bridges God's Eye View to the simulation. It polls the telemetry bridge (`/snapshot`), renders mission overlays via `Cesium.CustomDataSource`, drives the HUD from live telemetry, and manages track lifecycles and rendering. Roughly 70 integration lines are hooked into God's Eye View's existing catalog, state, and shell — the upstream project is otherwise unmodified.

**The result:** what was a general-purpose situational-awareness globe becomes a dedicated UAV command center where the operator can watch an AI agent fly a mission, see the same sensor feed the AI sees, and make the final call on every contact.

---

## How Microsoft AirSim drives the UAV

[**Microsoft AirSim**](https://github.com/microsoft/AirSim) (MIT) is the UAV physics and sensor backend, pinned at commit `1ca93f6f77e4e8a39b2b241c1fe2764da4d7dd41`. AirSim provides:

| AirSim capability | How it drives the sim |
|---|---|
| **Multirotor flight dynamics** | `takeoffAsync`, `landAsync`, `goHomeAsync`, `hoverAsync`, `moveToGPSAsync` (native GPS navigation), `moveOnPathAsync`, `rotateToYawAsync` — all the flight primitives the MCP server commands. |
| **Sensor suite** | `simGetImages` for batch Scene + Depth + Segmentation + Infrared in one call (frame-aligned sets). Gimbal control via `simSetCameraPose`. FOV control via `simSetCameraFov` (enables the wide→narrow cross-cue — detect on wide, identify on narrow). |
| **GPS and telemetry** | `getGpsData`, `getMultirotorState` (attitude, velocity, landed state), `getLidarData`, `simTestLineOfSightToPoint`. |
| **Target detection ground truth** | `simSetSegmentationObjectID`, `simGetDetections` → `DetectionInfo[]` with name, geo_point, bounding boxes, and relative pose. |
| **Environment control** | `simSetTimeOfDay` (celestial clock for sun-angle planning), `simSetWeatherParameter`, `simSetWind` (wind vector → fuel model), object spawn/destroy/pose. |
| **Collision detection** | `simGetCollisionInfo` for crash detection. |

The Python client communicates over **msgpack-rpc** on port 41451 (loopback only). The MCP server's `UavBackend` wraps these calls in a safety-enforced, idempotent, FIFO-queued layer — the AI agent never talks to AirSim directly.

### Built-in fake AirSim (no GPU / no Unreal required)

`godseye/mcp/godseye_uav/fake_airsim.py` is a complete msgpack-rpc server (`:41451`) that impersonates AirSim's API surface with zero dependencies on Unreal Engine or a GPU. Every test, the CI suite, and laptop demo missions run against it. It answers `getGpsData`, `getMultirotorState`, `simGetImages` (generated frames), `simGetDetections`, `simTestLineOfSightToPoint`, and the full flight-command set — enough to exercise every MCP tool and every mission pattern without hardware.

---

## Architecture

```
  agentic harness  (LLM + the godseye-uav skill)
            │  MCP · Streamable HTTP · Bearer
  ┌─────────▼──────────────────────────────┐
  │ godseye-mcp-server        :8791/mcp    │  45 tools · 8 resources
  │   safety envelope · FIFO queue · fuel  │  geofence · BINGO · lost-link
  │   └─ telemetry bridge     :8790        │  /snapshot /mission-overlay /camera /events
  └────┬──────────────────────────┬────────┘
       │ msgpack-rpc :41451       │ REST + SSE (read-only)
  ┌────▼─────────────┐   ┌────────▼──────────────┐
  │ AirSim  or  the  │   │ God's Eye View  :4173 │  the command center
  │ built-in fake    │   │ (Cesium 3D globe)     │
  └──────────────────┘   └───────────────────────┘
```

**Hard rules:**
- The MCP server is the **only command path**. The telemetry bridge and God's Eye View are read-only.
- msgpack-rpc binds loopback only — the bridge is the only external surface.
- One in-flight command per vehicle, enforced by a server FIFO queue with state machine `idle → executing → cancelling → aborting`.
- Every altitude carries a named datum (`alt_hae_m`, `alt_msl_m`, `alt_agl_m`). Never a bare `alt_m`.
- Every mutating tool accepts `idempotency_key`. Replaying a key returns the original handle.

---

## Quick start

**No GPU required.** Everything runs on a laptop against the built-in fake AirSim.

### One-command demo

```bash
./scripts/demo_laptop.sh
```

This boots the fake sim, the MCP server, the telemetry bridge, and the God's Eye View UI, then flies a scripted reconnaissance mission you can watch live at <http://localhost:4173>.

### Services only (no automatic mission)

```bash
cd godseye && ./start.sh
```

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `THEATER` | `iran-isfahan` | Theater ID from `godseye_uav/theaters.py` |
| `SIM_BACKEND` | `fake` | `fake` (no GPU) or `real` (connect to AirSim/Unreal) |
| `AIRSIM_PORT` | `41451` | msgpack-rpc port |
| `BRIDGE_PORT` | `8790` | Telemetry bridge REST/SSE port |
| `MCP_PORT` | `8791` | MCP server port |
| `UI_PORT` | `4173` | God's Eye View dev server port |
| `TOKEN` | `dev-token` | Bearer token for MCP and bridge |

### Full setup from a clean clone

```bash
./scripts/setup.sh --with-ui
cd godseye && ./scripts/demo_laptop.sh
```

`setup.sh` creates the Python venv, installs the package with all dependencies, fetches the pinned AirSim PythonClient, and runs `npm install` in the God's Eye View directory.

---

## Connecting an AI agent

The MCP server speaks MCP over Streamable HTTP with Bearer auth. `.mcp.json` configures it for any MCP-compatible client (Claude Code, Cursor, Codex, etc.):

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

From Python with `mcp` SDK:

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

---

## The skill — how the AI flies

`godseye/.agents/skills/godseye-uav/SKILL.md` is the operating manual the AI agent loads. It defines:

- **Mission patterns**: grid search (lawnmower / expanding-square), route recon, track-and-follow, target identification (wide→narrow FOV cross-cue), threat assessment, track handoff, airspace deconfliction.
- **Sensor and flight doctrine**: altitude ↔ resolution trade-off, when to use EO vs IR, sun-side rule (keep the sun behind the sensor), standoff distances (server-derived from threat envelopes), cross-cue workflow.
- **Safety contract**: the non-negotiable workflow, geofence/margin/min-AGL rules, BINGO and lost-link behaviour, and the rule that a harness's ROE may only be *stricter* than the server's.
- **Reporting templates**: SALUTE (Size, Activity, Location, Unit, Time, Equipment) per contact; INTREP (Intelligence Report) per mission with confidence justifications, gap analysis, and an honest coverage statement.

The non-negotiable workflow:

> **task → plan → dry-run → execute → monitor → report**

The dry-run is the gate: it returns waypoints, `est_time_s`, `est_fuel_pct`, and the BINGO gate result *without flying anything*. A rejected gate means re-plan — never work around it.

---

## Safety model

The server owns safety. The AI agent cannot override it; a skill's ROE may only be *stricter*.

- **Geofence / ceiling / min-AGL / max-speed**, checked in flight, not only at plan time.
- **BINGO fuel**: every plan must pass a pre-flight gate (plan + return leg + 20% reserve ≤ capacity). Reaching BINGO forces an RTB that **cannot be cancelled**, and the mission is flagged `incomplete - fuel`.
- **Lost link**: the server autonomously executes the mission's lost-link plan (hold-orbit / climb-for-LOS / RTB / continue) and logs the LOAL event into the INTREP.
- **Busy queue**: one in-flight command per vehicle. A second command returns `{"status":"busy", "current": <handle>}` — the harness decides, not the server.

---

## Realism — what's real, what's simulated

| Layer | Real | Simulated |
|---|---|---|
| **UAV physics** | — | AirSim multirotor dynamics (or fake sim) |
| **Sensors** | — | AirSim Scene/Depth/Seg/IR cameras + gimbal |
| **Terrain** | Re:Earth/Mapterhorn; EGM2008 geoid | — |
| **Weather/wind** | Open-Meteo (current conditions at theater) | — |
| **Air traffic** | OpenSky/adsb.lol (live aircraft in AO) | — |
| **Mapped installations** | OSM/Overpass (context, not authoritative) | — |
| **Globe imagery** | Google Photorealistic 3D Tiles; Esri World Imagery | — |
| **Geoid** | EGM96 (NGA, public domain) — shipped as package data | — |
| **Detections** | — | AirSim ground truth (`simGetDetections`) |
| **Order of battle** | Seeded from real installations; doctrine-derived threat rings | Class capabilities from `OB_LIBRARY` |

Every value that comes from real data carries provenance: `alt_agl_is_real`, `los_is_measured`, `traffic_is_real`, `wind_known`. A degraded feed is surfaced as a visible flag — never silently substituted.

---

## Layout

| Path | What |
|---|---|
| `godseye/mcp/godseye_uav/` | MCP server, telemetry bridge, safety envelope, mission doctrine, intel pipeline, geo math, fake AirSim |
| `godseye/tests/` | UE-free test suite (~1,000 tests) — runs with zero GPU |
| `godseye/scripts/` | `demo_laptop.sh`, `demo_mission.py`, `ci.sh`, `_airsim_client.sh` |
| `godseye/.agents/skills/godseye-uav/` | **The harness skill** — mission patterns, sensor doctrine, safety contract, SALUTE/INTREP templates |
| `godseye/.claude/skills/godseye-uav/` | Symlink for Claude Code compatibility |
| `godseye/PLAN.md` | The design document with numbered findings (M1–M22 military, T1–T9 technical) |
| `godseye/TOOL_CONTRACT.md` | Authoritative 45-tool + 8-resource MCP contract |
| `godseye/BRIDGE_CONTRACT.md` | Telemetry bridge ↔ God's Eye View feed contract |
| `godseye/REAL_DATA_INTEGRATION.md` | What real-world data the sim consumes, and its limits |
| `godseye/GAP_REGISTER.md` | Verified gap register from the plan-vs-code audit |
| `godseye/INTEGRATION_FINDINGS.md` | Defects found by running stack + browser together |
| `gods-eye-view/` | **Bilawal Sidhu's God's Eye View** (MIT, vendored) + this project's UAV layer (`src/layers/uav/`) |
| `scripts/setup.sh` | Makes a fresh clone runnable |
| `LICENSE` | Apache-2.0 |
| `THIRD_PARTY_NOTICES.md` | Required notices for AirSim, God's Eye View, datasets, and EGM96 grid |

---

## Tests

```bash
# Python: ~1,000 tests against the fake AirSim (no GPU, no Unreal)
cd godseye && PYTHONPATH="$PWD/mcp:$PWD/../airsim/PythonClient" .venv/bin/python -m pytest tests -q

# JavaScript: ~4,200 tests for the God's Eye View frontend
cd gods-eye-view && npm test
```

---

## Datum — read before touching altitudes

Altitude is the highest-risk area in the system. There is exactly **one** conversion point, `geo.canonical_altitude()`, and no module may do its own geoid math.

- Origin altitudes are entered as **MSL** and converted once at ingest.
- `altHae = altMSL + N(φ, λ)`, where `N` is the EGM96 geoid undulation — **negative** where the geoid is below the ellipsoid (−22.21 m at the Redmond origin, +1.58 m near Isfahan).
- Every altitude field names its datum: `alt_hae_m`, `alt_msl_m`, `alt_agl_m`. Never a bare `alt_m`.
- If an accurate geoid source cannot be loaded the code **raises** rather than silently degrading; a degraded datum is surfaced to the operator, never hidden.

---

## Known limits

- The UAV itself is simulated (AirSim or the built-in fake); only the *environment* data is real.
- Geo-registration is certified to a measured ~900 m radius from the origin. Beyond that, re-anchor the origin.
- OpenSky is licensed for non-commercial research/education use; several other feeds carry attribution requirements. See `gods-eye-view/DATA_SOURCES.md` and `THIRD_PARTY_NOTICES.md`.
- The task watchdog is a flat 120 s duration — too short for realistic mission legs (an AO crossing takes ~1,100 s). See `GAP_REGISTER.md` for all known defects.
- Coverage of the MCP tool catalog is tracked in `GAP_REGISTER.md` — 19 tools at baseline, 45 planned; not all are implemented yet.

---

## Licence and third-party notices

**This project** is Apache-2.0 (`LICENSE`, `NOTICE`). Copyright © 2026 Ankur Nair.

**It bundles and derives from third-party software and data under their own licences.** Read [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) in full before making this repository public or using it commercially. Key third-party components:

| Component | Licence | Usage |
|---|---|---|
| Microsoft AirSim | MIT | UAV physics/sensor backend; `geo.py` is a line-by-line port of AirSim's EarthUtils |
| God's Eye View | MIT (© 2026 Bilawal Sidhu) | Full vendored command-center UI + this project's UAV layer |
| EGM96 geoid grid | NGA public domain | Shipped as package data for `canonical_altitude()` |
| OpenSky Network | Non‑commercial research/education | Live air traffic |
| TeleGeography submarine cables | CC BY‑NC‑SA 3.0 | Bundled dataset in `gods-eye-view/` |
| Bhote Koshi event data | CC BY‑NC 4.0 | Bundled dataset in `gods-eye-view/` |

**ISR-only.** This project observes, classifies and reports. It contains no kinetic capability and none may be added. Threat assessment produces sensor-posture advice only; command authority stays with the operator.