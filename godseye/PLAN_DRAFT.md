# godSeye — Agentic UAV Mission Simulation System
## Technical Implementation Plan (DRAFT v0.1 — pending expert review)

**Vision:** God's Eye View (Cesium photorealistic 3D globe, live tracking, military HUD, FLIR/NVG shaders, detection overlay) becomes the command-and-control UI. Microsoft AirSim becomes the UAV physics + sensor simulation backend. An MCP (Model Context Protocol) server exposes mission control to an agentic harness (LLM agent + skills) that plans and flies recon / tracking / target-identification / threat-assessment missions on real-world maps with GPS positioning, flight-path mapping, and fuel modeling.

---

## 1. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Agentic Harness (LLM agent, godseye-uav skill)                  │
│   mission planning · ROE checks · BDA · target ID reasoning     │
└──────────────────────┬──────────────────────────────────────────┘
                       │ MCP (tools/resources/notifications)
┌──────────────────────▼──────────────────────────────────────────┐
│ godseye-mcp-server (Node.js)                                    │
│   tool catalog · safety envelope · mission state machine        │
│   target-ID pipeline orchestration · fuel/battery model         │
└───────┬──────────────────────────────────────┬──────────────────┘
        │ msgpack-rpc :41451                   │ HTTP/WebSocket
┌───────▼───────────────────────┐   ┌──────────▼──────────────────┐
│ AirSim (Unreal Engine)        │   │ Telemetry Bridge (in MCP    │
│ multirotor physics · cameras  │   │ server or standalone Node)  │
│ seg/IR/depth · lidar · GPS    │   │ polls AirSim @ 10 Hz,       │
│ object spawn/pose · weather   │   │ serves snapshot + stream    │
└───────────────────────────────┘   └──────────┬──────────────────┘
                                               │ getSnapshot() contract
                                    ┌──────────▼──────────────────┐
                                    │ God's Eye View (browser)    │
                                    │ new layer: src/layers/uav   │
                                    │ (cloned from layers/military)│
                                    │ 3D drone models · tracking  │
                                    │ cockpit feed · HUD · FLIR   │
                                    │ mission overlays (routes,   │
                                    │ grids, geofence, targets)   │
                                    └─────────────────────────────┘
```

**Key decisions:**
- **Fork, not upstream:** work in a `godseye` fork of gods-eye-view; layers are modules, so the UAV layer is additive and mergeable.
- **AirSim runs headless on the sim host** (Linux/Windows with UE; `settings.json` with `OriginGeopoint` placed at the mission AO). Real-world terrain in UE via Cesium-for-Unreal plugin **or** use a georeferenced custom UE environment. Bridge converts NED ↔ WGS84 with the same math as `EarthUtils::nedToGeodetic` (AirLib `EarthUtils.hpp:291`) so the web globe and the sim agree on lat/lon.
- **MCP server owns ALL control authority.** The web UI is read-only for sim state (a "God's Eye"); only the harness (or a human operator console later) issues commands. This keeps one command path, auditable.

---

## 2. AirSim Backend (verified against repo)

From `airsim/PythonClient/airsim/client.py` + `AirLib` (verified by research agent + direct grep):

**Flight control (MultirotorClient, msgpack-rpc port 41451):**
- `takeoffAsync`, `landAsync`, `goHomeAsync`, `hoverAsync`
- `moveToPositionAsync(x,y,z,velocity)` — NED
- `moveToGPSAsync(lat,lon,alt,velocity)` — **native GPS waypoint nav** (client.py:1229)
- `moveOnPathAsync(path, velocity)` — multi-waypoint routes
- `moveByVelocityAsync`, `rotateToYawAsync`, `cancelLastTask`
- `enableApiControl`, `armDisarm`, `simPause`, `simContinueForTime`

**Sensors / imagery:**
- `simGetImages([ImageRequest(camera, image_type, pixels_as_float, compress)])` — one call returns aligned Scene/Depth/Segmentation/IR frames
- `ImageType`: Scene=0, DepthPlanar=1, DepthPerspective=2, DepthVis=3, Segmentation=5, Infrared=7, OpticalFlow=8/9
- Gimbal: `simSetCameraPose(camera_name, pose)` — full EO/IR gimbal simulation
- `getGpsData`, `getImuData`, `getBarometerData`, `getMagnetometerData`, `getLidarData`, `getDistanceSensorData`
- `simGetGroundTruthKinematics` — truth telemetry
- `getMultirotorState` → `MultirotorState` (kinematics, GPS, RC, landed state, timestamp)

**Target ID ground truth (critical):**
- `simSetSegmentationObjectID(mesh, id)` + `seg_rgbs.txt` — assign class IDs to target meshes
- `simAddDetectionFilterMeshName`, `simSetDetectionFilterRadius`, `simGetDetections(camera, image_type)` → `DetectionInfo[]` {name, geo_point, Box2D, Box3D, relative_pose} — **AirSim gives us detector ground truth for free**; our vision model's output is validated against it (and during training-free operation, DetectionInfo can simulate a model with configurable error/noise)

**World / scenario control:**
- `simSpawnObject`, `simDestroyObject`, `simSetObjectPose`, `simListSceneObjects` — dynamic target placement, moving convoys (tick poses from bridge)
- `simSetTimeOfDay` (celestial clock), `simEnableWeather`, `simSetWeatherParameter`, `simSetWind`
- `simTestLineOfSightToPoint` — sensor LOS checks for masking/terrain analysis
- Multi-vehicle: `simAddVehicle`, `listVehicles`, `settings.json` "Vehicles" block — multi-ship flights (lead/wingman)

**Geo-referencing:**
- `OriginGeopoint` in settings.json (default 47.641468,-122.140165); `EarthUtils::nedToGeodetic` converts local NED → WGS84. Bridge must replicate this to publish drone position to the globe. `getHomeGeoPoint()` RPC available.

**Known quirks (from research):** Python `BarometerData.altitude` typing bug (types.py:429) — bridge reads raw msgpack fields, unaffected. Async APIs return futures — server must track/cancel tasks.

---

## 3. God's Eye View Integration (verified against repo)

From `gods-eye-view` (research agent extraction of `src/layers/military/` + app wiring):

**Layer anatomy (clone `src/layers/military/` → `src/layers/uav/`):**
- `index.js` — `createUavLayer({source, services, resolveAsset})`
- `state.js` — entity state, tracked entity, detection objects, IR shader state
- `ingestion.js` — `update(viewer,{signal})` → `source.getSnapshot({}, {signal})`, error backoff
- `rendering.js` — billboards → 3D GLB models on zoom (`_modelSpec`, `_ensureModel`, IR boost via `Cesium.CustomShader` UNLIT) — reuse for MQ-9/quad models (3D hangar already has MQ-9)
- `tracking.js` — `_trackFlight`, click handler, publishes `gev:awareness-subject-selected`, feeds cockpit/voice context
- `lifecycle.js` — `registerPickOwner('uav', …)`, init/enable/destroy
- App wiring: new `src/app/layers/uavDrones.js` injecting services; register `'uav': ['getSnapshot']` in `SOURCE_METHODS` (`src/app/constructCatalog.js`)

**Source contract:** `src/sources/live/contract.js` — object with `label` + `getSnapshot(query,{signal})` returning `{status, source, observedAtMs, stale, freshness, entities}`. New `src/sources/live/uavSim.js` polls/streams from telemetry bridge (WebSocket with REST fallback).

**Free wins once tracked:** click-to-track, Contacts roster (250 km proximity), cockpit view (`src/ui/cockpit*.js` binds to any tracked entity — drone FPV reuse), detection overlay bounding boxes (feeds on `_detectionObjects`), military HUD telemetry, FLIR/NVG GLSL shaders (`src/bloom.js`, `ui/effects.js`) applied to drone camera viewport, share links with tracked drone.

**Mission overlays (new):** `src/layers/uav/missionOverlay.js` — Cesium entities for planned route polyline + waypoints, grid-search pattern, geofence cylinder, target markers with classification labels, fuel/ETA readout in HUD. Rendered from mission state published by bridge.

**Drone camera feed:** new UI surface — PIP window showing sim camera (JPEG stream from bridge) with optional FLIR shader treatment, detection boxes drawn from `simGetDetections` and/or vision-model output.

---

## 4. MCP Server — `godseye-mcp`

Node.js MCP server (official `@modelcontextprotocol/sdk`).

### 4.1 Transport
- **Streamable HTTP** (primary): harness may run on a different machine than the sim host; supports SSE notifications for long missions.
- **stdio** (secondary): same-host dev.

### 4.2 Tool catalog (v1)

**Flight control**
| Tool | Params | Returns |
|---|---|---|
| `uav_takeoff` | vehicle, altitude_m | task_handle |
| `uav_land` | vehicle | task_handle |
| `uav_goto_gps` | vehicle, lat, lon, alt_m, speed_mps | task_handle |
| `uav_fly_route` | vehicle, waypoints[{lat,lon,alt_m}], speed_mps | task_handle |
| `uav_orbit_poi` | vehicle, lat, lon, radius_m, alt_m, laps, camera_track=true | task_handle |
| `uav_hover` | vehicle | ok |
| `uav_return_to_home` | vehicle | task_handle |
| `uav_abort` | vehicle | ok (cancelLastTask + hover) |
| `uav_set_gimbal` | vehicle, camera, pitch_deg, yaw_deg, track_geo_point? | ok |

**Sensors / intel**
| Tool | Params | Returns |
|---|---|---|
| `uav_capture_image` | vehicle, camera, type(scene/depth/segmentation/infrared), jpeg_quality | image resource + geo pose |
| `uav_get_telemetry` | vehicle | position(lat/lon/alt), attitude, velocity, battery_pct, fuel_remaining_s, range_km, landed_state |
| `uav_get_detections` | vehicle, camera | DetectionInfo[] (name, geo_point, box2d/box3d) |
| `uav_los_check` | vehicle, lat, lon, alt_m | line-of-sight bool + first obstacle |
| `uav_list_vehicles` | — | vehicles + states |

**Mission primitives (server-composed)**
| Tool | Params | Returns |
|---|---|---|
| `mission_grid_search` | vehicle, polygon[[lat,lon]], lane_spacing_m, alt_m, speed, pattern(lawnmower/expanding-square) | mission_handle, waypoints, est_time_s, est_fuel_pct |
| `mission_recon_route` | vehicle, waypoints, alt_m, capture_interval_s, cameras | mission_handle |
| `mission_track_target` | vehicle, target_id, standoff_m, alt_m | mission_handle (server loop: read target pose → re-path) |
| `mission_identify_target` | vehicle, target_id, orbit_first=true | mission_handle + final {classification, confidence, images, geo_point} |
| `mission_threat_assessment` | vehicle, area_polygon | mission_handle + report {contacts[], threat_scores[], recommended_roe} |
| `mission_status` | mission_handle | state, progress_pct, current_waypoint, eta_s, fuel |
| `mission_cancel` | mission_handle | ok |

**Scenario / sim admin**
| Tool | Params | Returns |
|---|---|---|
| `sim_spawn_target` | asset(vehicle/person/structure class), lat, lon, heading, mobile_route? | target_id |
| `sim_move_target` | target_id, waypoints, speed | ok |
| `sim_set_time` | datetime or clock_speed | ok |
| `sim_set_weather` | rain, snow, fog, dust, wind vector | ok |
| `sim_reset` | — | ok |

### 4.3 Resources
- `uav://{vehicle}/telemetry` (stream, 10 Hz)
- `uav://{vehicle}/camera/{name}/{type}` (latest frame)
- `uav://mission/{id}` (mission state JSON)
- `uav://targets` (scenario target list)
- `uav://safety/geofence`

### 4.4 Safety envelope (server-enforced, cannot be bypassed by prompts)
- Geofence polygon (default: AO box around OriginGeopoint); commands outside rejected pre-flight.
- Ceiling / max speed / min altitude AGL (depth + terrain check).
- **Fuel/battery model:** server-side integrator — capacity (e.g. 35 min), burn rate by phase (hover/climb/cruise), reserve margin 20%; mission planner rejects plans exceeding `fuel_budget - reserve`; in-flight low-fuel triggers auto-RTH notification.
- Kill switch: `uav_abort` + sim-level `reset` always available; watchdog if harness disconnects mid-mission → auto-hover then RTH.
- ROE gate (config): `mission_identify_target` allowed autonomously; any simulated "kinetic" action is **out of scope by design** — this system is ISR-only. No weapons tools exist in the catalog.

### 4.5 State model
- `TaskHandle` (thin wrapper over AirSim async future): id, vehicle, state(running/done/failed/cancelled).
- `Mission` (server-composed): id, type, plan(waypoints), progress, events[], artifacts(images, detections, report).
- Notifications: `notifications/message` for waypoint reached, detection found, fuel warnings, mission complete.

### 4.6 Target identification pipeline
1. Orbit/capture: multi-angle Scene + IR + Segmentation frames.
2. Detection: v1 = AirSim `simGetDetections` ground truth with configurable noise (simulates a model); v2 = real vision model (ONNX/YOLO or VLM API) run server-side on Scene frames, validated against segmentation ground truth.
3. Classification: class ID ↔ segmentation table; confidence from view diversity.
4. Output artifact: {classification, confidence, geo_point, track_history, key_images} → harness for reasoning (threat assessment, BDA narrative).

---

## 5. Telemetry Bridge (AirSim → Globe)

- Node service (can live inside MCP server process): msgpack-rpc client polling `getMultirotorState` + `simGetVehiclePose` per vehicle @ **10 Hz**, NED→WGS84 via EarthUtils port, plus target poses for `sim_spawn_target` mobiles.
- Exposes: `GET /snapshot` (getSnapshot contract: entities[{id, lat, lon, altMsl, heading, speed, class:'uav-sim', trail}]) and `WS /stream` (10 Hz diffs), `GET /camera/{vehicle}/{name}.jpg` (transcoded sim frames @ 2–5 Hz), `GET /mission-overlay` (routes/grids/geofence as GeoJSON).
- GEV side: `src/sources/live/uavSim.js` consumes `/snapshot`; WebSocket upgrade for low latency; geoid: GEV already has `egm96-universal` — MSL conversion consistent.

---

## 6. Harness Skill (`godseye-uav` skill for the agent)

- Mission planning patterns: area recon (grid), route recon, point surveillance (orbit), target track, SAR expanding square, BDA re-look.
- Doctrine: sensor selection (EO day / IR night), altitude vs resolution tradeoff, standoff vs terrain masking (`uav_los_check`), fuel-aware planning (always plan return leg), time-on-station math.
- Workflow: receive task → plan → dry-run fuel/geofence validation → execute → monitor notifications → analyze captures → report (INTREP format).
- Constraints: ISR-only; no kinetic; ROE from MCP config; abort criteria.

---

## 7. Phased Roadmap

| Phase | Deliverable | Done when |
|---|---|---|
| **0 — Spike** | AirSim running headless; Python script: takeoff → moveToGPSAsync → orbit → capture 4 image types → land; EarthUtils NED→WGS84 port validated vs `getGpsData` | Script output matches sim telemetry <1 m |
| **1 — Bridge + GEV layer** | `uav` layer cloned from military; bridge /snapshot; drone visible+trackable on globe with trail, cockpit, HUD | Track a flying drone in GEV from browser |
| **2 — MCP core** | MCP server: flight tools, telemetry resource, safety envelope, fuel model, task handles | Harness takes off, flies GPS route, lands via MCP |
| **3 — Mission primitives** | grid_search, recon_route, orbit, track_target; mission overlays on globe; camera PIP with FLIR shader | Grid search flown + captured imagery in UI |
| **4 — Target ID** | sim_spawn_target, segmentation IDs, detections, mission_identify_target, multi-angle capture, report artifacts | Agent identifies spawned target, report correct |
| **5 — Threat assessment + doctrine skill** | threat_assessment primitive, skill with patterns/ROE, INTREP output | Full recon→identify→assess mission run by agent |
| **6 — Realism** | weather/time-of-day scenarios, multi-vehicle, moving targets, noise model on detections, optional real vision model | Night IR mission with moving convoy tracked |

## 8. Risks / Open Questions
- **Unreal world fidelity vs real maps:** GEV globe is photoreal via Google/Cesium tiles; AirSim needs Cesium-for-Unreal (or prebuilt env) for matching real-world terrain — Phase 0 must validate geo-registration quality. Fallback: abstract training environments (Blocks) with GPS math intact.
- **AirSim maintenance status:** repo is archived by Microsoft; fork pinned. Colosseum (AirSim successor) evaluated as drop-in alternative in Phase 0.
- **msgpack-rpc from Node:** no mature Node msgpack-rpc client for AirSim — options: (a) thin Python sidecar (`PythonClient`) wrapped by MCP server over local HTTP, (b) Node msgpack-rpc lib. Recommend (a) — Python client is first-party.
- **Detection ground truth vs realism:** using `simGetDetections` directly is "cheating" — acceptable for v1; noise model in Phase 6.
- Determinism/replay: log all RPC + telemetry for mission replay/debrief.
