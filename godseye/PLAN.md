# godSeye — Agentic UAV Mission Simulation System
## Technical Implementation Plan v1.1 (reviewed: military ISR + systems engineering; adds live-mission command-center view + laptop execution)

**Vision:** God's Eye View (Cesium photorealistic 3D globe) is the C2 UI. Microsoft AirSim is the UAV physics + sensor backend. A Python MCP server exposes mission control to an agentic harness (LLM + `godseye-uav` skill) flying recon / tracking / target-ID / threat-assessment missions on real-world maps with GPS positioning, flight-path mapping, and fuel modeling. **ISR-only: no kinetic tools exist anywhere in the system.**

**Review status:** Incorporates all findings from `REVIEW_MILITARY.md` (M1–M22) and `REVIEW_TECHNICAL.md` (T1–T9). Criticals fixed in design: M1/M2/M3/M7/M13, T1/T2/T7.

---

## 1. Architecture

```
┌────────────────────────────────────────────────────────────┐
│ Agentic Harness (LLM agent + godseye-uav skill)            │
│ mission planning · SALUTE/INTREP reporting · ROE (stricter)│
└─────────────────────┬──────────────────────────────────────┘
                      │ MCP · Streamable HTTP · Bearer auth
┌─────────────────────▼──────────────────────────────────────┐
│ godseye-mcp-server (Python, colocated with sim host)       │
│  tool catalog · per-vehicle FIFO command queue (T2)        │
│  safety envelope: geofence · ceiling · BINGO fuel (M4)     │
│  fuel integrator, tick-reconciled (T5) · track store (M11) │
│  order-of-battle + threat pipeline (M13) · pattern-of-life │
│  (M12) · lost-link plans (M9) · JSONL persistence (T4c)    │
│  ┌────────────────────────────────────────────────────┐   │
│  │ Telemetry Bridge (in-process)                      │   │
│  │  loop A: telemetry ≤10 Hz batched (T3a)            │   │
│  │  loop B: camera 2–5 Hz, subscriber-gated (T3b)     │   │
│  │  loop C: on-demand captures/detections/LOS (T3c)   │   │
│  │  NED→WGS84 + canonical altHae via egm96 (T1)       │   │
│  │  serves /snapshot (REST), /mission-overlay, camera │   │
│  └────────────────────────────────────────────────────┘   │
└───────┬──────────────────────────────────────┬─────────────┘
        │ msgpack-rpc :41451 (loopback only)   │ REST (CORS+token)
┌───────▼───────────────────────┐   ┌──────────▼───────────────┐
│ AirSim / Unreal (GPU host)    │   │ God's Eye View (browser) │
│ multirotor physics · cameras  │   │ src/layers/uav (cloned   │
│ Scene/Depth/Seg/IR · GPS      │   │ from layers/military)    │
│ object spawn/pose · weather   │   │ tracking · cockpit · HUD │
│ time-of-day · multi-vehicle   │   │ detection boxes · PIP    │
│                               │   │ mission overlays         │
│                               │   │ (CustomDataSource, T6)   │
└───────────────────────────────┘   └──────────────────────────┘
```

**Hard rules:**
- MCP server is the **only command path**; GEV UI is read-only (God's Eye).
- msgpack-rpc :41451 binds loopback — the bridge is the only external surface (T4e).
- Streamable HTTP is the only MCP transport in v1 (M21); Bearer token + Origin validation + loopback default (T4e).
- One in-flight command per vehicle, enforced by server FIFO queue + state machine `idle → executing → cancelling → aborting` (T2). Busy commands return `busy` + current handle — harness decides.

---

## 2. AirSim Backend (verified against repo)

`airsim/PythonClient/airsim/client.py` (msgpack-rpc, port 41451, `Common.hpp:22`):

**Flight:** `takeoffAsync` L1121, `landAsync` L1134, `goHomeAsync` L1147, `hoverAsync` L1263, `moveToPositionAsync` L1225, **`moveToGPSAsync(lat,lon,alt,vel)` L1229** (native GPS nav), `moveOnPathAsync` L1221, `moveToZAsync` L1233, `rotateToYawAsync` L1257, `cancelLastTask` L1025, `enableApiControl` L50, `armDisarm` L74. SingleCall guard: one in-flight command per vehicle (T2).

**Sensors:** `simGetImages` L295 (batch Scene+Depth+IR in ONE call → frame-aligned sets, T3), `ImageRequest(camera, ImageType, pixels_as_float, compress)`; `ImageType`: Scene=0, DepthPlanar=1, DepthVis=3, Segmentation=5, Infrared=7 (optical-flow 8/9 cut from v1, M21). Gimbal: `simSetCameraPose`, FOV: `simSetCameraFov` (enables wide→narrow cross-cue, M7). `getGpsData` L874, `getLidarData` L896, `getMultirotorState` L1557, `simGetCollisionInfo` L439, `simTestLineOfSightToPoint` L376.

**Target ID ground truth:** `simSetSegmentationObjectID`, `simAddDetectionFilterMeshName`, `simSetDetectionFilterRadius`, `simGetDetections` L677 → `DetectionInfo[]{name, geo_point, Box2D, Box3D, relative_pose}`.

**Scenario:** `simSpawnObject` L579, `simDestroyObject` L595, `simSetObjectPose` L501, `simAddVehicle` L1083, `simSetTimeOfDay` L224 (celestial clock — sun-angle planning, M6), `simSetWeatherParameter` L253, `simSetWind` L1058 (wind → fuel math, M15), `simPause`/`simContinueForTime`.

**Geo:** `OriginGeopoint` in settings.json (entered as **MSL**, T1); `EarthUtils::nedToGeodetic` (EarthUtils.hpp:291) ported to bridge; `getHomeGeoPoint()` RPC.

**AirSim has NO battery model** (verified) → fuel is 100% server-side (T5).

---

## 3. God's Eye View Integration (verified against repo)

Clone `src/layers/military/` → `src/layers/uav/` (structure confirmed by code read):
- `index.js` `createUavLayer({source, services, resolveAsset})`; `state.js`; `ingestion.js` (`update(viewer,{signal})` → `source.getSnapshot({}, {signal})`, error backoff); `rendering.js` (billboards → 3D GLB on zoom; MQ-9 model already in 3D hangar; IR boost via `Cesium.CustomShader`); `tracking.js` (`_trackFlight`, `gev:awareness-subject-selected`); `lifecycle.js` (`registerPickOwner('uav', …)`).
- App wiring: new `src/app/layers/uavDrones.js`; register `'uav': ['getSnapshot']` in `SOURCE_METHODS` (`src/app/constructCatalog.js`).
- Source: `src/sources/live/uavSim.js` implementing the contract (`src/sources/live/contract.js`: `label` + `getSnapshot` → `{status, source, observedAtMs, stale, freshness, entities}`). **v1 = REST poll only** (T6); WebSocket is v2 behind the same interface.

**Free wins once tracked:** click-to-track, Contacts roster, cockpit view FPV, military HUD telemetry, detection overlay bounding boxes, FLIR/NVG shaders on the PIP camera feed (deferred to Phase 5, M20).

**Mission overlays:** separate `Cesium.CustomDataSource` fed from `/mission-overlay` (GeoJSON: route polyline, waypoints, grid pattern, geofence, threat rings, target markers w/ track IDs) refreshed on mission-state change, NOT through the entity snapshot (T6). Entity property writes capped at 2–5 Hz with client interpolation (T6).

**Datum (T1):** bridge publishes canonical `lat, lon, altHae`; `hae = alt_msl + N(φ,λ)` using `egm96-universal` (already a GEV dependency) evaluated per-sample. GEV sets entity `heightReference` explicitly. ONE conversion point in the bridge — never per-call-site.

---

## 3.1 Live Mission View — Command Center (NEW, v1 requirement)

The GEV browser **is the live mission command center**. All components already exist in the architecture — this section wires them into the operator experience. GEV remains **read-only for flight control** (MCP is the only command path); the view shows the mission as the agentic harness executes it via MCP, in real time.

**Live data feeds (all via existing bridge loops, no new transport):**
| Feed | Rate | Source endpoint |
|---|---|---|
| Drone positions/telemetry on globe (sampled CZML properties, client-interpolated) | 5 Hz poll | `/snapshot` (loop A) |
| Mission overlays: planned route, grid pattern, waypoints, geofence, threat rings, target markers + track IDs | on change | `/mission-overlay` |
| Mission status object: phase, active tool, task progress %, fuel % vs BINGO line, safety/geofence state | 1 Hz | `/snapshot.missions[]` (new field) |
| Sensor video PIP (Scene/IR) of tracked drone | 2–5 Hz JPEG, subscriber-gated | `/camera` (loop B) |
| Contact roster: persistent track IDs, class, confidence, last SALUTE | on change | `/snapshot.contacts[]` (new field) |
| Alarm/audit stream: BINGO warnings, geofence proximity, lost-link, detection events | on event | SSE `/events` (server-sent; trivial in FastAPI, keeps GEV poll-free for alarms) |

**Operator experience (Phase 3 acceptance):**
1. Open GEV → `uav` layer live, drones flying on globe with 5 Hz smooth motion (T6 sampled properties + interpolation).
2. Click drone → tracked: cockpit FPV, HUD (alt HAE, speed, fuel %, ETA-to-BINGO), detection boxes from `simGetDetections` rendered on PIP.
3. As harness executes MCP mission (e.g. `mission_grid_search`): route/grid polyline + coverage area appear in overlay source in real time; progress % + phase label in HUD panel; targets found → numbered track markers (M11) pop on globe + SALUTE card in side panel.
4. `mission_threat_assess` result → threat rings + OB markers updated live; `mission_handoff_track` → second drone's route drawn, contact card shows "TRACKING: UAV-2".
5. All safety events (BINGO, geofence, lost-link plan activation) surface as alarm toasts + HUD banner via SSE.

**Implementation notes:**
- `/snapshot` gains `missions[]` and `contacts[]` sections (bridge already owns mission/track state — just serialize).
- SSE `/events` is the only push channel in v1 (one-way, browser-native `EventSource`, reconnects cleanly); full bidirectional WS stays Phase-6 (T6 v2).
- Command-center layout = existing GEV panels (cockpit + readout + contacts roster); no new UI framework.

**Definition of done (added to Phase 3 exit):** scripted harness mission (recon → ID → orbit) runs headless while operator watches entire mission live in GEV: motion ≤250 ms behind sim, overlays update on phase change, ≥3 alarm types demonstrated.

---

## 4. MCP Server — Tool Catalog v1

Server: Python, colocated with sim host. Long ops (>2 s) return `task_id` immediately + `notifications/progress` (progressToken, T4a). Progress derived server-side from telemetry (distance-to-target vs plan), never from AirSim futures (T2). All tools accept `idempotency_key` (T4b).

### 4.1 Flight control
| Tool | Params | Returns |
|---|---|---|
| `uav_takeoff` | vehicle, alt_m, idempotency_key | task_handle |
| `uav_land` / `uav_return_to_home` | vehicle | task_handle |
| `uav_goto_gps` | vehicle, lat, lon, alt_m, speed_mps | task_handle |
| `uav_fly_route` | vehicle, waypoints[{lat,lon,alt_m}], speed_mps | task_handle + bingo_fuel_pct |
| `uav_orbit_poi` | vehicle, lat, lon, radius_m, alt_m, **direction(cw/ccw)**, laps, camera_track, **sun_side rule** (M3/M6) | task_handle |
| `uav_hover` / `uav_abort` | vehicle | ok (abort = cancelLastTask + hover, clears queue) |
| `uav_set_gimbal` | vehicle, camera, pitch_deg, yaw_deg, track_geo_point? | ok |
| `uav_set_fov` | vehicle, camera, fov_deg (wide↔narrow cross-cue, **M7**) | ok |

### 4.2 Sensors / intel
| Tool | Params | Returns |
|---|---|---|
| `uav_capture_image` | vehicle, camera, type(scene/depth/segmentation/infrared), jpeg_quality | image resource + geo pose + sun angle |
| `uav_get_telemetry` | vehicle | lat/lon/**altHae**, attitude, velocity, fuel_pct, **bingo_fuel_pct**, est_range_km, landed_state, wind |
| `uav_get_detections` | vehicle, camera | DetectionInfo[] + persistent **track_ids** (M11) |
| `uav_los_check` | vehicle, lat, lon, alt_m | LOS bool + first obstacle |
| `uav_list_vehicles` / `uav_list_tracks` | — | vehicles+states / track store (M11) |

### 4.3 Mission primitives (server-composed; every plan passes pre-flight BINGO gate, M4)
| Tool | Params | Returns |
|---|---|---|
| `mission_grid_search` | vehicle, polygon, alt_m, speed, camera, **overlap_pct** (NOT lane_spacing — server derives spacing = swath×(1−overlap), swath = 2·alt·tan(HFOV/2), **M1**), pattern(lawnmower/expanding-square) | mission_handle, derived footprint + lane spacing, waypoints, est_time_s, est_fuel_pct, bingo gate result |
| `mission_recon_route` | vehicle, waypoints, alt_m, camera, **forward_overlap_pct** (distance-triggered captures every N m, **M2**; time interval only as max-rate clamp) | mission_handle |
| `mission_track_target` | vehicle, track_id, alt_m (standoff **server-derived** from threat ring + narrow-FOV pixel density, verified vs `uav_los_check`, **M5**) | mission_handle (server re-path loop) |
| `mission_identify_target` | vehicle, track_id, orbit_first (wide-FOV detect → **cross-cue narrow FOV at reduced slant range**, M7) | mission_handle + {classification, confidence, geo_point, track_history, key_images} |
| `mission_threat_assessment` | vehicle, area_polygon | mission_handle + structured report (see §4.6, M13) |
| `mission_handoff_track` | from_vehicle, to_vehicle, track_id (sensor cue + positive-ID confirm before custody transfer, **M10**) | mission_handle |
| `mission_status` / `mission_cancel` | mission_handle | state, progress_pct, waypoint, eta_s, fuel, bingo_fuel_pct |

### 4.4 Scenario / sim admin
| Tool | Params | Returns |
|---|---|---|
| `sim_spawn_target` | class(vehicle/person/structure — OB library key), lat, lon, heading, mobile_route? | target_id |
| `sim_move_target` | target_id, waypoints, speed | ok |
| `sim_set_time` | datetime / clock_speed (sun-angle planning, M6) | ok |
| `sim_set_weather` | rain/snow/fog/dust, **wind vector (feeds fuel model, M15)** | ok |
| `sim_set_link_state` | vehicle, degraded|lost, duration_s (**lost-link model, M9**) | ok |
| `sim_set_gps_degradation` | vehicle, error_m / denial (**M16**) | ok |
| `sim_reset` | — | ok (does NOT wipe track store / pattern-of-life DB, M12) |

### 4.5 Safety envelope (server-enforced; skill ROE may only be stricter)
- Geofence polygon around AO; ceiling / max speed / min AGL.
- **BINGO fuel doctrine (M4):** pre-flight gate — est. plan fuel + return leg + 20% reserve ≤ capacity, else reject. `bingo_fuel_pct` published in mission state. Reaching BINGO = force-RTB (un-cancellable safety transition, T5); mission flagged "incomplete — fuel".
- **Fuel integrator (T5):** each telemetry tick: recompute phase from *measured* climb/cruise (not commanded), `fuel −= rate(phase, wind)·dt`, persist (JSONL). Dry-run estimates use the SAME integrator over the waypoint plan.
- **Lost-link (M9):** per-mission `lost_link_plan` (hold-orbit / climb-for-LOS / RTB / continue) executed autonomously by server on link loss; LOAL events logged to INTREP.
- Watchdog: future not resolved in timeout → fail handle → hover-recovery (T2). Harness disconnect → lost_link_plan.
- ISR-only: no kinetic tools. Threat assessment outputs `recommended_roe` REMOVED (M14) — system reports; command authority stays with the operator.

### 4.6 Threat assessment — structured pipeline (M13)
Deterministic, not LLM vibes: (a) match each track against **order-of-battle library** (type → capabilities, weapon ranges, mobility); (b) evaluate **intent indicators** (posture, movement toward asset, pattern-of-life deviation from M12 store, emissions if modeled); (c) confidence = confirmed/probable/possible with cited evidence per element; (d) score = capability × intent, each component traceable. Harness narrates; scores come from the model.

### 4.7 Reporting artifacts (M8)
`uav://reports/{mission_id}` — structured **SALUTE** per contact (Size/Activity/Location/Unit/Time/Equipment) + **INTREP** template (mission summary, coverage %, tracks w/ IDs, sensor conditions, LOAL events, gaps). Harness fills fields, never free-form.

### 4.8 Resources & persistence
`uav://{vehicle}/telemetry` · `uav://{vehicle}/camera/{name}/{type}` · `uav://mission/{id}` · `uav://tracks` (M11) · `uav://targets` · `uav://safety/geofence` · `uav://reports/{id}` · `uav://pattern-of-life/{poi}` (M12).
State: append-only JSONL mission log (commands, task transitions, fuel integrals, LOAL) → restart = replay → resume-or-abort-and-RTH (T4c). Track store + pattern-of-life persist across `sim_reset` (M12).

---

## 5. Harness Skill (`godseye-uav`)
- Patterns: area recon (grid w/ footprint-derived lanes), route recon, point surveillance (sun-side orbit), target track + handoff, SAR expanding square, BDA re-look.
- Doctrine: EO day / IR night; altitude↔resolution tradeoff; standoff from threat rings; sun-glare avoidance (M6); **BINGO check before tasking** (M4); wind in fuel math (M15); GPS-degraded fallback = INS/terrain nav (M16).
- Workflow: task → plan → dry-run (fuel/geofence/sun) → execute → monitor progress notifications → SALUTE/INTREP.
- Abort criteria; never overrides server safety. No kinetic reasoning.

---

## 6. Phased Roadmap (exit criteria = named tests)

| Phase | Deliverable | Done when |
|---|---|---|
| **0 — Spike + geo gate** | AirSim headless on GPU host (`-RenderOffscreen`/Xvfb — `-nullrhi` = black frames, T7); Blocks env + OriginGeopoint; EarthUtils NED→WGS84 port; docker-compose + UE/AirSim version matrix pinned by commit (T9); **geo-registration gate test** (T7): scripted marker round-trip ≤5 m horiz / ≤10 m vert; datum test (T1): drone at NED 0 → bridge HAE ≈ expected. Colosseum eval timeboxed 1 day (M22). Real terrain (Cesium-for-Unreal) DEFERRED to Phase 6 (M19) | Geo gate + datum tests pass |
| **1 — Bridge + GEV layer** | 3 bridge loops (T3); `/snapshot` REST; `uav` layer; drone trackable w/ trail, cockpit, HUD | Track a flying drone in browser; fake-AirSim contract tests pass (T8) |
| **2 — MCP core** | Flight tools, per-vehicle FIFO queue + busy-rejection (T2), progress notifications, idempotency, auth (T4e), JSONL persistence, fuel integrator + BINGO gate (T5/M4), geofence | Harness flies GPS route via MCP; restart-recovery test resumes/aborts correctly |
| **3 — Mission primitives** | grid_search (M1 spacing), recon_route (M2 triggers), orbit (M3/M6), track_target (M5), overlays CustomDataSource, camera PIP 2–5 Hz | Grid search flown; coverage math verified vs footprint |
| **4 — Target ID** | sim_spawn_target + OB classes, segmentation IDs, track store (M11), mission_identify_target w/ wide→narrow cross-cue (M7), SALUTE/INTREP artifacts (M8) | Agent identifies spawned target; SALUTE fields complete |
| **5 — Threat assessment + handoff** | OB library + intent pipeline (M13), pattern-of-life store (M12), mission_handoff_track (M10), lost-link plans (M9), FLIR shader PIP polish (M20) | Full recon→ID→assess→handoff mission, structured INTREP |
| **6 — Realism** | Wind (M15), GPS degradation (M16), detection false pos/neg noise (M17), sensor-degradation weather (M18), multi-vehicle, moving convoys, Cesium-for-Unreal real terrain behind feature flag (M19), real vision model optional | Night IR mission tracking moving convoy in GPS-denied window |

## 7. Test strategy (T8)
- **UE-free CI:** fake msgpack-rpc AirSim server → bridge/MCP contract tests; golden-vector geo-math tests (EarthUtils port + egm96); GEV snapshot fixtures; scenario invariant tests (fuel never negative, queue never double-executes); replay log = debrief.
- Per-phase named tests in exit criteria (table above).

## 8. Deployment (T9)

### 8.1 Laptop profile (default dev/demo target — NEW)
**Requirement: runs on one laptop.** Realistic floor: modern gaming laptop or Apple Silicon MBP (M1 Pro+, 16 GB RAM). Target framerate ≥30 fps sim, ≥60 fps GEV.

| Component | Laptop mode |
|---|---|
| **AirSim** | **Windows laptop:** run prebuilt Blocks/Neighborhood binary (`AirSim/Unreal/Environments/Blocks` or downloaded release) — no UE editor/build needed. **macOS:** AirSim Unity build or UE build from source (heavier); alternative = sim in a small cloud GPU box, everything else local (split-host profile below). **Linux laptop w/ NVIDIA:** docker + nvidia-container-toolkit. |
| **Settings for laptop** | `ViewMode: NoDisplay` off, 1280×720 windowed; `simSetTimeOfDay` fixed noon (no dynamic lighting); weather off; 1–2 vehicles max; camera JPEG compress=true, 640×480 @ 3 Hz for PIP; Depth/Segmentation fetched on-demand only. `ClockSpeed: 1.0`. |
| **MCP+bridge** | `uvicorn` process on same laptop, loopback only, token = env var. No container needed for dev. |
| **GEV** | `vite dev` / static build served locally; browser = the command center. Cesium at 720p windowed is light. |
| **Test harness** | Fake-AirSim mode (`--sim=fake`) → entire MCP+GEV stack runs with **zero GPU** for CI, skill dev, and doctrine iteration (T8). |
| **Budgets (enforced in bridge config)** | telemetry 5 Hz, camera 3 Hz max, 1 camera subscriber max, detections on-demand. Sim CPU <40%, GEV JS main-thread <8 ms/frame. |

**One-command demo:** `./scripts/demo_laptop.sh` → starts fake or real AirSim, MCP server, GEV; opens browser; harness skill runs scripted recon mission visible live in command center (acceptance of §3.1).

### 8.2 Split-host (heavy terrain / multi-vehicle)
1. **Sim host:** bare metal/VM w/ GPU (cloud GPU OK); pinned AirSim fork commit + UE version; settings.json mounted. Linux: nvidia-container-toolkit + `-RenderOffscreen`/Xvfb. Windows: bare process (no containers).
2. **MCP+bridge:** one container (shared fuel/mission state); healthcheck = :41451 reachable + `/snapshot` fresh; `sim_state` surfaced in `/snapshot` on sim crash.
3. **GEV:** static/CDN; CORS + Bearer token to reach bridge. Laptop runs only browser + harness client.

## 9. Risks
- Geo-registration (T7) — gated in Phase 0 with hard tolerances.
- Archived AirSim — fork pinned by commit hash; Colosseum timeboxed (M22).
- Camera readback starving physics — bounded by T3 loop budgets + drop-on-backlog.
- Detection GT "cheating" — acceptable v1; false pos/neg noise Phase 6 (M17); real model optional.
- Agent learning bad doctrine — prevented by server-derived spacing/standoff/BINGO (M1–M5) and structured threat pipeline (M13).
