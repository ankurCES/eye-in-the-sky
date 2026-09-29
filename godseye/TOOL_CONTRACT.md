# godSeye MCP tool contract (authoritative, v1)

This is the contract Wave-2 implementers and the `godseye-uav` skill both build against.
It is PLAN.md §4.1–§4.8 made concrete. Where the shipped code deviates, this document wins.

**Baseline measured before the work started:** 19 tools, **0 resources**, 0 prompts.
**Shipped now:** 51 tools (46 + the five runtime-theater tools of §4.5), 8 resources (3 concrete + 5 templates) — verified over the real transport.
The ten simulated wargame tools of §4.6 (M14a) are not in that count: they are on `/mcp` only when the
host runs with `--wargame-mcp`.

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
- **ISR by default (M14, M14a)**: no kinetic tool exists in the default catalog. Threat output
  carries no engagement recommendation; sensor-posture advice only. Simulated `wg_*` tools (M14a)
  are published only under `--wargame-mcp`. Command authority stays with the operator.

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

**Simulated wargame guards (M14a, WG v2 §5.2.6).** These sim tools keep the wargame's scenario units
apart from every other object. Their refusals are `{"rejected": true, "error": "<code>", "message":
"<one sentence>"}` and change nothing:

- `sim_spawn_target`: `scenario_name` (the name belongs to a scenario unit), `duplicate_name` (a
  target of that name already exists; it used to be overwritten), and during a session
  `near_scenario_unit` (within 150 m of a scenario unit, or a `mobile_route` that passes within
  150 m of one). An automatic label skips names already taken.
- `sim_move_target`: `scenario_name`. Only the wargame moves its own units. During a session also
  `near_scenario_unit`: a route whose legs (from where the object is now, and back to the first
  waypoint when `loop`) pass within 150 m of a scenario unit.
- Detections of any other object never join a scenario unit's track, however close they are: a
  scenario contact associates only with detections of its own object (D1).
- `sim_spawn_order_of_battle`: a mapped site that would take a scenario unit's name or land within
  150 m of one is skipped, not spawned. The result then carries `skipped: [{osm_id, name, category,
  reason}]`; the key is absent when nothing was skipped. A second run refuses every site it already
  spawned as `duplicate_name`.
- `sim_reset`: `wargame_active` ("end the wargame first") while a session is starting or running.
- Any command for a drone the wargame downed: `vehicle_lost` until the wargame ends.

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

## §4.5 Runtime theaters and sim speed (WG v2 Phase A)

SIMULATION administration: put the simulated AO over any real place, and run the fake simulator
faster. Registered by `mcp/godseye_uav/theater_tools.py`. A real place is **context only (M14)**:
geocoder answers and mapped sites are never targets, and a missing site is not an absent one.

| Tool | Class | Params | Returns |
|---|---|---|---|
| `geo_lookup` | read | query (2–200 characters), limit=5 (1–10) | `{candidates: [{id, name, label, lat, lon, bbox: [s,w,n,e], size_km, source, geocoder, kind, osm, theater_id}], provenance}`. Order: coordinates → theater table → Photon → Nominatim; cached 30 days. No heights. Map data off (`--geodata off`): coordinates only, otherwise `candidates: []` with `provenance.reason` "map data is off; give coordinates". Uncached lookups: 30 per 10 min, else `rate_limited`. |
| `geo_sites` | read | category? (airfield, military_base, port, power, fuel, comms, bridge, rail_hub, hq_gov, border_crossing, medical, dam, other), limit=40 (1–60), refresh=false, near_lat?, near_lon? | `{sites: [{id: "sit:{theater}:{osm}", name, label, category, subtype, lat, lon, bounds, protected, salience, tags, …, distance_m?}], returned, total, counts, capped, real, degraded, reason, fetched_at_ms, bbox, attribution, caveat, note, theater: {id, epoch}, refresh?, hint?}`. `refresh=true` refetches the running theater's area in a worker thread (6 per 10 min, else `rate_limited`); a degraded or empty answer never replaces a non-degraded set of the same theater epoch. |
| `theater_propose` | plan | theater_id?, lat?, lon?, place_id?, label? (≤60), place?, bbox? [s,w,n,e], half_extent_m?, airframe?, home_lat?, home_lon?, **ground_msl_m?**, query? (the `geo_lookup` query, shown to the operator) | `{proposal_id, simulated_world: true, theater: {id, label, place, ao, bbox, ao_km, area_km2, home: [lat, lon, msl], pois, dynamic}, airframe: {id, label, summary, reach_m, ao_max_half_m}, ground: {msl_m, provenance}, sites, caveats, set_args}`. Changes nothing. Half-extent `clamp(requested or geocoded or default, 1500, clamp(0.4·reach, 1500, 25000))`. Ground is metres **MSL** (Re:Earth → EGM96, else Open-Meteo, else the operator's `ground_msl_m`). 10 per 10 min with map data on. Refusals: `ground_unknown`, `theater_invalid`, `rate_limited`, `proposal_timeout` (60 s). Proposals live 30 min (16 at most). |
| `sim_set_theater` | **sim** (operator approval every time; never session-grantable) | proposal_id, theater_id, label, ao [[lat, lon]] (3–12), home_lat, home_lon, **ground_msl_m**, airframe, idempotency_key | `{ok, status: "accepted", theater: {id, label, epoch, dynamic}, previous, home: {lat, lon, alt_msl_m, alt_hae_m, undulation_m, datum_source}, airframe: {id, changed}, fuel, reset, kept, sites_loaded, real_data}`; `{ok, status: "unchanged"}` when the theater and airframe are already running. |
| `sim_set_time_scale` | **sim** | scale (1–10), idempotency_key | `{ok, status: "accepted", scale, previous, caveats}` |

**Theater change protocol.** `geo_lookup` (or coordinates) → `theater_propose` → `sim_set_theater(**set_args)`
exactly. The handler checks, in order: idempotency replay; the proposal exists (`proposal_expired`); every
argument matches it to 1e-7° and 0.01 m, the AO vertex by vertex (`proposal_mismatch` with `fields`); same
theater row (AO, home, ground, label, POIs) and airframe (`unchanged`; a corrected home or ground under the
same id is a switch). The switch then runs on the tasking loop: every drone is parked, landed,
at the new home; the geofence, home and fuel homes move; the theater's MSL ground is converted to HAE once
(T1); contacts, reports and the audit trail are kept; fuel is kept unless the airframe changes (full tank).

**Refusals.** These five tools refuse with `{"rejected": true, "error": "<code>", "message": "<one
sentence>"}` (WG v2 §3.1; the console shows `message` verbatim, as text) and change nothing. Every tool:
`invalid_parameter` for malformed arguments; the three budgeted ones `rate_limited`. `sim_set_theater`
also: `proposal_expired`, `proposal_mismatch` (with `fields`), `switch_refused` (with `reasons`, one
sentence each) while any drone is airborne, busy, BINGO-latched or has lost its link, while a forced RTB
flies, under real AirSim, outside the app host ("runtime theater change needs the app host"), before
restart recovery finishes, or while another switch runs; `theater_integrity` when the origin copies fail
their cross-check (every copy is put back at the old origin; after it every command but `uav_land` and
`uav_hover` is refused and the monitor enforces nothing until a restart); `theater_switch_failed` for an
unexpected error. `sim_set_time_scale` also: `time_scale_refused` under real AirSim. A refusal is never
recorded against an `idempotency_key`.

**Who asked.** `set_via` is `console` when the in-app analyst made the call (the operator approved its
slip), `mcp` for a direct `/mcp` call, `boot` for the restored theater. The chat slip's previews come from
`theater_tools.approval_preview`; the graph's theater block from `theater_tools.theater_state`.

**Sim speed.** The fake simulator only (physics, fuel and the sun run `scale`× faster). The returned
caveats say what stays on wall time: link-loss timers, detections and scans, the analyst's clock, the
safety checks ("Safety checks run every 0.5 sim-seconds.") and, above ×3, camera captures (coverage gaps).
`time_scale` is not persisted; a restart runs at ×1.

## §4.6 Simulated wargame (M14a; not on /mcp unless --wargame-mcp)

SIMULATION only (PLAN.md §4.5a). Registered by `mcp/godseye_uav/wargame_tools.py` on the wargame's own
registry (`godseye-wargame`, never mounted); the in-app analyst reaches it through its toolbelt (the
three entry tools in ISR mode, all ten during a session). The default `/mcp` publishes none of them.
`--wargame-mcp` is an explicit opt-in for external harnesses that also publishes all ten on `/mcp`.
`/control/command` never forwards a `wg_*` tool (403 `wargame_tools_not_forwarded`).

- **Scenario units only.** The forces are simulated units the wargame places (provenance `scenario`,
  generic designators such as "Red SAM 1", ids `frc:{side}-{prefix}-{n}`). Nothing else can be
  planned against or engaged: a mapped site, a theater point, a `sim_spawn_*` object, a phantom or
  real air traffic is refused `not_a_scenario_unit`. So is a target within 500 m of a mapped place
  or theater point (`target_near_real_site`) or within 1 km of a protected place
  (`target_protected`), checked on the unit's true position and on the track's.
- **Notional.** Effects are play-balance table draws, with no real system names and no
  weaponeering. Every result, refusals included, carries `simulated: true` and `note` ("Notional
  simulation parameters chosen for play balance. Not weapon data."). Wargame text names no real
  place: positions are AO-relative and the theater is named by `theater_id` only.
- **Drones never deliver effects.** They fly recce and battle-damage re-looks with the ordinary ISR
  tools (`mission_recon_route`, `uav_scan_targets`). Shooters are blue scenario units.
- **Session scoped.** Every tool except the entry tools (`wg_session_start`, `wg_session_status`,
  `wg_list_classes`) refuses `wargame_inactive` ("Start a simulated wargame session first
  (wg_session_start).") when no session runs, before anything else is checked (only a replayed
  `idempotency_key` is answered first).
- **Fog of war.** Answers are the blue view: red units are listed only when the session was started
  with `reveal_red`, and a strike's outcome stays hidden until a re-look assesses it.

| Tool | Class | Params | Returns |
|---|---|---|---|
| `wg_session_start` | **sim** (operator approval every time; never session-grantable) | seed?, red_engages=true, reveal_red=false, idempotency_key | `{session_id, started_at_ms, seed, engine, table_version, red_engages, reveal_red, theater_id, ao: {half_extent_m, half_diagonal_m}, classes_that_fit, caveats}`. Refusals: `wargame_active`, `wargame_requires_fake_sim`, `theater_switching`, `theater_integrity`, `theater_not_cleared` (every preset except `default`, and a chat theater whose AO, grown by 5 km, overlaps one of those presets), `exclusion_incomplete` (a chat theater whose mapped places couldn't all be loaded), `invalid_parameter`. |
| `wg_session_status` | read | events=20 (0–100) | No session: `{active: false, starting, last}`. In a session: `{active, session_id, started_at_ms, seed, engine, table_version, time_scale, red_engages, reveal_red, truth_view, theater_id, ao, counts, units, pending, engagements, vehicles_lost, events, caveats, revision, step_ms, errors}`, blue view. |
| `wg_list_classes` | read | — | `{classes: [{key, side, ob_key, prefix_slug, label, role, threat_range_m, threat_ceiling_m (height above the unit), detection_range_m, strike_range_m, cycle_s, ammo, …, fits_ao, covers_ao, caveats}], theater_id, ao, classes_that_fit, table_version}` for the running AO. Works with no session. |
| `wg_session_end` | **sim** | idempotency_key | `{ended, session_id, aar_id, resource, revived}`. Scenario objects, their tracks and waiting engagements are removed; drones the wargame downed are parked at home, landed; aircraft keep their tasks. The after-action review is filed as `uav://reports/aar-<session id>`, never as `latest`. Refusals: `wargame_inactive`, `wargame_ending`. |
| `wg_generate_scenario` | **sim** | template (air_defence_belt, mech_advance, strike_exercise), intensity=medium (low, medium, high), ad_class?, idempotency_key | `{template, intensity, ad_class, units, blue_placed, red_placed, moved_by_spiral, caveats}`, all or nothing; red units are listed only when revealed. Refusals: `no_room`, `unknown_template`, `unknown_class`, `max_units`, `spawn_busy`, `spawn_failed`. |
| `wg_spawn_force` | **sim** | side (red, blue), wg_class, lat, lon, count=1 (1–6), objective_id?, idempotency_key | `{units: [{unit_id, id, designator, side, wg_class, kind_label, state, damaged, ammo, lat, lon, alt_msl_m, position, mobile, objective, threat_range_m, threat_ceiling_m, detection_range_m, strike_range_m, provenance: "scenario", caveats}], caveats}`. The first unit sits at lat/lon, the rest spiral round it. Refusals: `outside_ao`, `near_real_site`, `near_theater_point` (500 m), `too_close_home` (red, 1 km), `too_close_unit` (200 m), `near_existing_object` (150 m), `max_units` (60), `unknown_class`, `side_mismatch`, `unknown_objective`, `spawn_busy`, `spawn_failed`. |
| `wg_list_forces` | read | side? (red, blue) | `{forces, counts, truth_view}`, blue view. |
| `wg_plan_corridor` | plan | vehicle, **target_track_id** (required; omitting it is a validation error), alt_agl_m=60, relook=false, relook_radius_m=400 (150–1500) | `{waypoints, alt_agl_m, length_m, eta_s, exposure_s, p_survive, straight, delta_exposure_s, delta_length_m, legs, threat_basis, threats_considered, terrain_masking, relook_points, caveats, recon_args, vector_id, target}`. One altitude (metres AGL); planned against gated sensed threats unless red is revealed. `recon_args` go to `mission_recon_route` unchanged (dry run first), through the normal gate and slip. 20 per 10 min, else `rate_limited`. Refusals: `not_a_scenario_unit`, `target_near_real_site`, `target_protected`, `vehicle_unknown`, `vehicle_lost`, `no_route`, `start_outside_fence`, `target_outside_fence`, `route_too_complex`, `plan_budget`. |
| `wg_propose_strike` | plan | shooter_id, target_track_id | `{pending_id, shooter: {id, unit_id, designator}, target: {track_id, label, perceived_class, confidence}, range_m, p_estimate, corridor, expires_at_ms, execute_args, caveats}`. Nothing is fired; the pending engagement expires after 10 min. 20 per 10 min, else `rate_limited`. Refusals: `shooter_unavailable`, `not_a_scenario_unit`, `insufficient_confidence` (below probable), `target_near_real_site`, `target_protected`, `out_of_range`, `pending_limit` (8 waiting). |
| `wg_execute_engagement` | **engagement** (asks every call; never session-grantable; acknowledgement required; never automatic) | pending_id, shooter_id, target_track_id, idempotency_key | `{executed: true, engagement_id, fired_at_ms, outcome, outcome_note}`; `outcome` is null unless red is revealed ("Unknown until a re-look (battle damage assessment)."). Refusals: `engagement_requires_console_approval`, or the gate refusal when a re-check fails (the engagement then expires). |

**Engagement protocol (WG v2 §3.8).** `wg_propose_strike` returns `execute_args`; the analyst calls
`wg_execute_engagement(**execute_args)` exactly. The console's engagement slip asks the operator, who
approves with the acknowledgement; only then does the chat service authorize the pending engagement,
and the analyst's toolbelt runs the call inside that chat session's console context. The engine
executes only an authorized engagement, only from that console session, only with the same arguments,
only before it expires, and only once, re-running every gate first. `/mcp` (even under
`--wargame-mcp`) and `/control/command` never have that context, so they can never confirm.

**Refusals and keys.** Refusals are `{"rejected": true, "error": "<code>", "message": "<one
sentence>", "simulated": true, "note": …}`; the console shows `message` verbatim, as text. Malformed
arguments give `invalid_parameter`; an unexpected engine error gives `wargame_failed`. A replayed
`idempotency_key` returns the original result and runs nothing again; a refusal is never recorded.
`wg_execute_engagement` keys are recorded per console session, so no other caller can replay one.

**Ending.** `wg_session_end`, or the bearer route `POST /wargame/session/end` (no body is read): 200
`{ok: true, aar_id, session_id, resource, revived, simulated: true}`, or 409 `{error:
"wargame_inactive" | "wargame_ending", message}`. The AAR records who ended it: `operator` (the
route), `analyst` (the approved analyst call) or `mcp` (a direct `/mcp` call).

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
It also carries `doctrine: {mode: "isr"|"wargame", wargame_session, wargame_mcp, rule}` (M14a):
`mode` is `wargame` only while a simulated wargame session runs, `wargame_mcp` says whether `/mcp`
publishes the `wg_*` tools, and `rule` is "M14a: ISR by default; simulated wargame tools exist only
in an operator-approved session." `isr_only` is unchanged.

## Watchdog (T2)

The task watchdog keys on *lack of progress*, not total duration (`tasking.py`): a task is failed only
when nothing has reported forward movement for its watchdog window (120 s by default), and the
recovery hook then hovers the vehicle. A ~1100 s AO crossing that keeps closing on its target
survives. (The original flat 120 s cap on total duration failed every realistic mission leg.)
