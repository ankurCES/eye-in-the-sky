# godSeye MCP tool contract (authoritative, v1)

This is the contract Wave-2 implementers and the `godseye-uav` skill both build against.
It is PLAN.md §4.1–§4.8 made concrete. Where the shipped code deviates, this document wins.

**Baseline measured before the work started:** 19 tools, **0 resources**, 0 prompts.
**Shipped now:** 46 tools, 8 resources (3 concrete + 5 templates) — verified over the real transport.

## Conventions (apply to every tool)

- **Transport**: Streamable HTTP at `POST /mcp`, Bearer token, loopback by default (M21, T4e).
- **Altitude**: every altitude parameter and return value MUST state its datum in the tool description.
  Use `alt_agl_m` for height above *terrain*, `alt_msl_m` for orthometric, `alt_hae_m` for ellipsoidal.
  Never a bare `alt_m`. There is exactly ONE conversion point (T1) — call the canonical converter in
  `geo.py`; no module may do its own geoid math.
- **Long operations** (>2 s) return a `task_handle` immediately and report progress via
  `notifications/progress` with the caller's `progressToken` (T4a). Progress is derived **server-side
  from telemetry** (distance-to-target vs plan), never from AirSim futures (T2).
- **Idempotency**: every mutating tool accepts `idempotency_key`. Replaying a key returns the ORIGINAL
  handle and must not re-execute (T4b). A key accepted-and-ignored is a defect.
- **Busy**: one in-flight command per vehicle. A second command returns `{"status":"busy", "current": <handle>}`
  — the harness decides what to do (T2). Never silently queue behind an unbounded backlog.
- **Errors** are structured: `{"error": {"code": "...", "message": "...", "retryable": bool}}`.
  Safety rejections are NOT errors — they return the gate result so the harness can re-plan.
- **ISR-only (M14)**: no kinetic tool exists. Threat output carries no engagement recommendation;
  sensor-posture advice only. Command authority stays with the operator.

## §4.1 Flight control

| Tool | Params | Returns |
|---|---|---|
| `uav_takeoff` | vehicle, alt_agl_m, idempotency_key | task_handle |
| `uav_land` | vehicle, idempotency_key | task_handle |
| `uav_return_to_home` | vehicle, speed_mps, idempotency_key | task_handle |
| `uav_goto_gps` | vehicle, lat, lon, alt_agl_m, speed_mps, idempotency_key | task_handle |
| `uav_fly_route` | vehicle, waypoints[{lat,lon,alt_agl_m}], speed_mps, idempotency_key | task_handle + bingo_fuel_pct |
| `uav_orbit_poi` | vehicle, lat, lon, radius_m, alt_agl_m, **direction** (cw/ccw), laps, camera_track, **sun_side** (M3/M6) | task_handle |
| `uav_hover` | vehicle | ok |
| `uav_abort` | vehicle | ok — `cancelLastTask` + hover + clear queue |
| `uav_set_gimbal` | vehicle, camera, pitch_deg, yaw_deg, track_geo_point? | ok |
| `uav_set_fov` | vehicle, camera, fov_deg | ok — **enables the M7 wide→narrow cross-cue** |

`uav_orbit_poi` MUST implement the sun-side rule (M6): given time of day and sun azimuth, choose the
orbit arc that keeps the sun behind the sensor, and say in the return which arc it chose and why.

## §4.2 Sensors / intel

| Tool | Params | Returns |
|---|---|---|
| `uav_capture_image` | vehicle, camera, type (scene/depth/segmentation/infrared), jpeg_quality | image resource ref + geo pose + **sun angle** |
| `uav_get_telemetry` | vehicle | lat, lon, **alt_hae_m**, alt_msl_m, alt_agl_m, attitude, velocity, fuel_pct, **bingo_fuel_pct**, est_range_km, landed_state, wind |
| `uav_get_detections` | vehicle, camera | DetectionInfo[] + persistent **track_ids** (M11) + per-contact confidence |
| `uav_los_check` | vehicle, lat, lon, alt_m | `{los: bool, first_obstacle: {...}|null, model: "<what was actually modelled>"}` |
| `uav_list_vehicles` | — | vehicles + queue states |
| `uav_list_tracks` | — | track store (M11) |

`uav_los_check` must never return unconditional `true`. State the model in the return: a LOS check the
harness cannot trust is worse than none, because doctrine depends on it (M5 standoff verification).

## §4.3 Mission primitives

Discrete tools (PLAN §4.3). `uav_mission(kind=…)` is kept ONLY as a thin dispatcher so the existing GEV
control panel keeps working — it must forward to these, not reimplement them.

| Tool | Params | Returns |
|---|---|---|
| `mission_grid_search` | vehicle, polygon, alt_agl_m, speed, camera, **overlap_pct**, pattern (lawnmower/expanding-square) | mission_handle, derived footprint, **lane spacing actually flown**, waypoints, est_time_s, est_fuel_pct, bingo gate |
| `mission_recon_route` | vehicle, waypoints, alt_agl_m, camera, **forward_overlap_pct** | mission_handle |
| `mission_track_target` | vehicle, track_id, alt_agl_m | mission_handle (server re-path loop) |
| `mission_identify_target` | vehicle, track_id, orbit_first | mission_handle + {classification, confidence, geo_point, track_history, key_images} |
| `mission_threat_assessment` | vehicle, area_polygon | mission_handle + structured report (§4.6) |
| `mission_handoff_track` | from_vehicle, to_vehicle, track_id | mission_handle |
| `mission_status` | mission_handle | state, **progress_pct**, waypoint, eta_s, fuel, bingo_fuel_pct |
| `mission_cancel` | mission_handle | ok |
| `mission_dry_run` | same params as any mission | plan product only: waypoints, est_time_s, est_fuel_pct, gate result — **executes nothing** |

Doctrine that must be *server-derived*, never taken from the caller (M1–M7):
- **Overlap units**: `overlap_pct` / `forward_overlap_pct` are **percents at the MCP boundary**
  (`20` = 20%), converted to a fraction exactly once at that boundary. The ambiguous open band
  `(0, 1)` is **refused, never guessed** — `0.20` could mean 0.2% or 20%, and silently coercing it
  would corrupt every derived lane spacing.
- **M1 grid spacing**: `swath = 2·alt_agl·tan(HFOV/2)`, `spacing = swath·(1−overlap_fraction)`. The caller
  supplies `overlap_pct`, never `lane_spacing`. **If the plan is capped or truncated for any reason, the
  return must report the coverage ACTUALLY achieved.** (An earlier version capped at 12 lanes and
  reported the uncapped spacing, overstating coverage by ~4×; a truncated plan now reports the
  spacing and coverage it actually flies, and why it was truncated.)
- **M2 recon captures**: distance-triggered every `swath·(1−forward_overlap_pct)` metres. A time
  interval may only act as a max-rate clamp.
- **M5 track standoff**: derived from the track's threat ring + the narrow-FOV pixel density needed for
  ID, then **verified with `uav_los_check`**. Never a caller-supplied radius.
- **M7 identify**: wide FOV to detect → cross-cue to narrow FOV at reduced slant range.
- **M4 BINGO gate**: every mission plan passes the pre-flight gate before anything is queued.

`mission_dry_run` exists because the skill's workflow is *task → plan → dry-run → execute → monitor*
(PLAN §5) and that is impossible today.

## §4.4 Scenario / sim admin

| Tool | Params | Returns |
|---|---|---|
| `sim_spawn_target` | **class** (OB library key), lat, lon, **alt_msl_m** (default = terrain height, never 0), heading, mobile_route? | target_id |
| `sim_move_target` | target_id, waypoints, speed | ok |
| `sim_set_time` | datetime / clock_speed | ok — drives the real sun position (M6) |
| `sim_set_weather` | rain/snow/fog/dust, **wind vector** | ok — wind feeds the fuel model (M15), weather degrades sensors (M18) |
| `sim_set_link_state` | vehicle, degraded/lost, duration_s | ok — exercises the lost-link plan (M9) |
| `sim_set_gps_degradation` | vehicle, error_m / denial | ok (M16) |
| `sim_reset` | — | ok — **must NOT wipe the track store or pattern-of-life** (M12) |
| `sim_set_fuel` | vehicle, fuel_pct (0–100, default 100), idempotency_key | **SAFETY OVERRIDE** (operator approval only): sets the fuel clock to `fuel_pct` (a new tank: burn integral restarts) and clears the BINGO latch with `operator_override`. Returns `fuel_pct`, `fuel_pct_before`, the **current** `bingo_fuel_pct` line (priced from position like `uav_get_telemetry`, with `bingo_fuel_pct_basis`; last tick's line, labelled, if telemetry is down), `margin_pct`, `bingo_tripped`, `bingo_was_latched`, `relatch_expected` (fuel at/below the line re-latches on the next tick), `safety_task_in_flight` (a forced RTB already flying is **not** cancelled) and `note`. Unknown vehicle → `unknown_vehicle`; out-of-range/non-finite fuel → `invalid_parameter`. Journaled to `fuel.jsonl` (survives a restart) and audited as `fuel_reset`. `mission_status` is left as a record. |

`sim_spawn_target`'s altitude defaults to the ground under the point: measured terrain when the
real-data layer has it, else the running theater's ground elevation (the return says which). An
earlier default of `0.0` buried targets ~1550 m underground in the shipped theaters.

## Also shipped (not in the tables above)

These tools are registered by the server and listed by `tools/list`; their descriptions there are
authoritative.

| Tool | Params | What it does |
|---|---|---|
| `uav_task_status` | vehicle, task_id? | One vehicle's queue or one task; progress derived server-side from telemetry |
| `uav_mission` | vehicle, kind, params?, speed_mps?, idempotency_key, dry_run? | Thin dispatcher for the GEV panel; forwards to the discrete mission tools (kinds `recon_route`, `grid_search`, `orbit_poi`, `track_target`, `identify`, `assess`) |
| `uav_scan_targets` | vehicle, camera? | Pull detections, correlate into persistent tracks with measured pixels on target, classify, update pattern-of-life; returns SALUTE reports |
| `uav_identify_target` | track_id | SALUTE report for one track, with element aggregation over the track store (no sensor tasking) |
| `uav_assess_threat` | vehicle, track_id?, defended?, area_polygon? | Deterministic order-of-battle threat assessment; ISR-only |
| `uav_list_ob_classes` | — | Order-of-battle library keys, with category, role, envelope and unit size |
| `uav_deconflict_airspace` | vehicle? (default Drone1), lat?, lon?, alt_hae_m?, horizontal_m?, vertical_m? | Real aircraft inside the separation box, nearest first; an empty list is an empty feed, not clear airspace |
| `uav_real_data_status` | include_feeds? | What the server measures and what it assumes, per real-data feed |
| `uav_handoff_target` | track_id, from_vehicle, to_vehicle, idempotency_key | The GEV panel's handoff; same implementation as `mission_handoff_track` without `dry_run` and altitude |
| `sim_hydrate_real_data` | apply_weather?, allow_network?, background?, load_ao_terrain?, terrain_grid_m?, idempotency_key | Ingest terrain, mapped sites, air traffic and weather for the theater (network; between missions) |
| `sim_spawn_order_of_battle` | limit?, categories?, idempotency_key | Spawn ground truth from mapped military sites (context only, not authoritative); needs `sim_hydrate_real_data` first |
| `sim_set_environment` | wind_north/east/down? (default 0), gps_denied?, gps_noise_m?, det_false_neg?, det_false_pos?, idempotency_key | Legacy realism switch (wind, GPS, detection error); omitting the wind sets it to zero |

The in-app analyst does not see `uav_list_tracks`, `sim_set_environment` or `uav_handoff_target`;
its approval class for every tool is in `mcp/godseye_uav/analyst_policy.py` (see
`INTEL_CONSOLE.md`).

## Report size — summary by default, full traces on request

The consumer is an LLM harness, and the intel reports grow with the track store, **which persists
across runs**. Measured live at 36 tracks: `mission_threat_assessment` returned **1.1 MB** — past what
the MCP python client will carry, and far past what a harness can read in a context window.

So both roll-ups take `detail` and `top_n`:

| | `detail="summary"` (default) | `detail="full"` |
|---|---|---|
| `mission_threat_assessment` | flat traceable score components (capability, intent, confidence, envelope, observer range) + sensor posture | adds the `assessment` sub-objects and their cited `evidence` |
| `uav_target_report` (INTREP) | all six SALUTE fields + `confidence_level`/`confidence_score` | adds each contact's `confidence` derivation and evidence |

`top_n` caps how many contacts are **expanded** (default 10; `None` for no cap). Contacts past it are
never dropped — they appear as compact rows in `omitted` / `contacts_omitted` and are named in a
`truncation` string. The INTREP's counts, `confidence_summary` and `gaps` are always computed over
**every** contact, never only the expanded ones: a truncated report that also shrank its gap analysis
would be the coverage-overstatement defect (M1) in another costume.

An unrecognised `detail` value is **refused**, not guessed — the wrong guess silently changes what an
ISR report contains.

## §4.8 Resources (all 8 shipped: 3 concrete, 5 templates)

`uav://{vehicle}/telemetry` · `uav://{vehicle}/camera/{name}/{type}` · `uav://mission/{id}` ·
`uav://tracks` · `uav://targets` · `uav://safety/geofence` · `uav://reports/{id}` ·
`uav://pattern-of-life/{poi}`

`uav://safety/geofence` matters most for the skill: PLAN §4.5 says "skill ROE may only be stricter",
which the skill cannot honour without being able to read the envelope it must stay inside.

## Watchdog (T2)

The task watchdog keys on *lack of progress*, not total duration (`tasking.py`): a task is failed only
when nothing has reported forward movement for its watchdog window (120 s by default), and the
recovery hook then hovers the vehicle. A ~1100 s AO crossing that keeps closing on its target
survives. (The original flat 120 s cap on total duration failed every realistic mission leg.)
