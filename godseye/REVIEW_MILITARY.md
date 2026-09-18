# MILITARY ISR REVIEW — godseye PLAN_DRAFT v0.1
**Reviewer:** UAS/ISR operations planner (ret.) — doctrine check only, no repo exploration.
**Verdict:** Architecture is sound (one command path, server-enforced safety, ground-truth detections). Doctrine is amateur hour in three places: grid spacing, orbit geometry, threat assessment. Fix before Phase 3, or the sim teaches the agent bad habits you'll never untrain.

## 1. DOCTRINAL ERRORS

**M1 — CRITICAL — Lane spacing is a free knob, not derived from sensor footprint.** `mission_grid_search` takes `lane_spacing_m` with no tie to FOV/alt. Real spacing = cross-track swath × (1 − required overlap); swath = 2 × alt × tan(HFOV/2). At 100 m AGL with 60° HFOV that's ~115 m swath → 90 m lanes at 20% overlap, not vibes.
*Fix:* Server computes lane spacing from alt + camera FOV + overlap requirement; reject or auto-correct user-supplied spacing; return derived footprint in the mission plan.

**M2 — CRITICAL — Time-based capture triggers.** `mission_recon_route` uses `capture_interval_s`; capture rate must scale with ground speed or coverage collapses when speed changes.
*Fix:* Trigger captures by along-track distance (forward overlap = footprint length × (1 − overlap)), i.e. capture every N meters; keep time interval only as a max-rate clamp.

**M3 — CRITICAL — Orbit geometry under-specified.** `uav_orbit_poi` has radius/alt but no orbit direction (CW/CCW), no sun-side rule, no threat-ring logic. Direction matters: orbit so the sensor faces the sun-lit side (EO) and so egress is away from the threat. Standoff must come from the threat ring, not operator taste.
*Fix:* Orbit tool requires `direction`, and server computes standoff ≥ nearest threat-ring radius + margin, cross-checked against narrow-FOV resolution at that slant range; reject plans inside a threat ring without explicit waiver flag.

**M4 — MAJOR — No BINGO gate before tasking.** Fuel model has a 20% reserve and auto-RTH on low fuel — reactive, not preventive. Real planning declares BINGO (fuel to return + reserve) before wheels-up and treats it as a hard abort line: reaching BINGO = RTB, mission incomplete is reported as such.
*Fix:* Every mission plan (grid/orbit/track) must pass a pre-flight check: est. fuel for full plan + return leg + reserve ≤ capacity; publish `bingo_fuel_pct` in mission state; server force-RTB at BINGO and flags the mission "incomplete — fuel."

**M5 — MAJOR — `mission_track_target` standoff is free.** Same defect as M3: standoff must be set by threat ring outer bound and by narrow-FOV pixel-density requirement, then verified against terrain masking via `uav_los_check`.
*Fix:* Server derives standoff from threat profile + required sensor resolution; log the derivation in the mission plan.

**M6 — MAJOR — Sun angle is absent from capture doctrine.** `sim_set_time` exists but nothing computes solar elevation/azimuth vs sensor azimuth; EO capture looking into low sun is worthless, IR at low sun may be flooded.
*Fix:* Planning step computes sun position (time/lat/lon) and biases orbit direction / capture azimuth away from sun; flag "sun-glare risk" on waypoints.

## 2. MISSING CAPABILITIES

**M7 — CRITICAL — No sensor cross-cue (wide→narrow FOV).** No FOV/zoom tool in the catalog; `uav_set_gimbal` does pointing only. Detect on wide, cross-cue to narrow for ID is the core detect-to-identify workflow and it's impossible with these tools.
*Fix:* Add `uav_set_fov` (or wide/narrow camera swap) + a `mission_identify_target` step that wides first, detects, then cues narrow at reduced slant range.

**M8 — MAJOR — No SALUTE/INTREP artifact schema.** "INTREP format" is a bullet in the skill section; the MCP catalog returns raw detections and free-text reports. Reports must be structured artifacts: SALUTE per contact (Size/Activity/Location/Unit/Time/Equipment) and an INTREP with mission summary, coverage, confidence, and gaps.
*Fix:* Define `uav://reports/{mission}` artifact with SALUTE entries + INTREP template; harness fills fields, never free-form.

**M9 — MAJOR — Lost-link behavior undefined.** Watchdog on harness disconnect exists, but the sim never models datalink loss itself, and there's no action-on-loss-of-link doctrine (hold orbit, climb for LOS, return, or continue per plan).
*Fix:* Add a sim-level link model (configurable link loss regions/duration) + a per-mission `lost_link_plan` the server executes autonomously; log LOAL events in the INTREP.

**M10 — MAJOR — No target handoff between vehicles.** Multi-vehicle "lead/wingman" is listed, but there is no handoff tool: cue second platform's sensor onto the track, confirm continuity, transfer track custody. Handoff is how real ISR sustains a track.
*Fix:* Add `mission_handoff_track(from, to, track_id)` with sensor cue + positive-ID confirmation before custody transfer; track numbering persists across handoff.

**M11 — MAJOR — No stable track numbering / contact management.** Detections are per-frame `DetectionInfo[]` with no track IDs; without persistent track numbers you can't hand off, report SALUTE coherently, or measure pattern of life.
*Fix:* Server-side tracker: assign persistent track IDs (GTN-style), maintain track state (first/last seen, classification, confidence), expose via `uav://tracks`.

**M12 — MAJOR — No pattern-of-life storage.** `sim_reset` wipes the world; scenario history evaporates. Threat assessment without pattern of life is guessing.
*Fix:* Persistent per-POI pattern-of-life store (activity by time-of-day, routes, dwell times, deviations); seed it from scenario runs; feed it to M13's assessment.

## 3. THREAT ASSESSMENT — STRUCTURED, NOT VIBES

**M13 — CRITICAL — `mission_threat_assessment` returns "threat_scores[] + recommended_roe" with no defined method.** That is an LLM horoscope. Threat assessment is a process: (a) match each contact against an order-of-battle library (type → capabilities, weapons ranges, mobility); (b) evaluate intent indicators (posture, movement toward friendly/asset, pattern-of-life deviation, emissions if modeled); (c) assign confidence (confirmed/probable/possible) with cited evidence per element; (d) score = capability × intent, each component traced to evidence.
*Fix:* Make threat assessment a deterministic pipeline over the OB library + intent indicators + pattern-of-life store (M12); harness may narrate, but scores and confidence must come from the structured model with evidence references.

**M14 — MAJOR — "recommended_roe" inverts authority.** ROE is commander-given constraint; an assessment primitive recommending ROE teaches the agent the wrong chain of command.
*Fix:* Replace with `roe_check`: evaluate the current/intended action against configured ROE (from MCP config, read-only), returning allowed/restricted/required-escalation; agent never generates ROE.

## 4. REALISM — WHAT'S WORTH SIMULATING

**M15 — MAJOR — Wind is in the tool list but not in doctrine or fuel math.** Crosswind changes orbit hold, ground speed, lane fidelity, and burn rate; a fuel integrator that ignores wind lies to the planner.
*Fix:* Feed wind into the fuel/burn model and into orbit/grid execution (crab compensation); include wind in the pre-flight BINGO check (M4).

**M16 — MAJOR — No GPS degradation model.** GPS is assumed perfect; real ISR planning handles degraded GPS (drift, denial) and the geofence must degrade safely with it.
*Fix:* Add configurable GPS noise/denial; geofence checks on fused position with growth margin during degradation; log GPS quality in telemetry and INTREP.

**M17 — MINOR — Detection noise must include false positives/negatives, not just jitter.** Jittered ground truth trains an oracle-obsessed agent.
*Fix:* Noise model = positional jitter + missed detections + false alarms (rate by sensor type, range, weather, sun angle); validate the pipeline still detects/tracks under it.

**M18 — MINOR — Simulate sensor noise and weather effects on detection, not on physics purity.** Full weather physics fidelity is waste for an ISR trainer; what matters is its effect on the sensor picture and fuel.
*Fix:* Keep weather as sensor-degradation knobs + fuel multiplier; don't chase aerodynamic realism.

## 5. CUT / DEFER

**M19 — MAJOR — Defer Cesium-for-Unreal real-world terrain out of Phase 0.** It's the biggest schedule risk for zero doctrinal value; GPS math works on Blocks. Fly doctrine on abstract terrain; geo-registration is a later fidelity item.
*Fix:* Phase 0 = Blocks + OriginGeopoint + EarthUtils port; real terrain becomes Phase 6+ behind a feature flag.

**M20 — MINOR — Defer FLIR/NVG shaders, cockpit PIP polish, and share links until the mission pipeline is proven.** UI sugar in front of working ISR primitives is backwards.
*Fix:* Phase 3 delivers camera frames + detections as data; visual treatments land after Phase 4.

**M21 — MINOR — Cut optical-flow image types and dual transport (stdio+HTTP) from v1.** Neither serves a mission; HTTP covers the stated deployment.
*Fix:* Drop ImageType 8/9 from the catalog; HTTP-only transport, stdio later if a dev workflow needs it.

**M22 — MINOR — Timebox or drop the Colosseum evaluation.** Archived AirSim pinned in a fork is acceptable for an ISR-only trainer; migration is rework you can defer until something breaks.
*Fix:* Pin the fork, record the Colosseum decision in one spike day max, move on.

**Bottom line:** Fix M1–M3, M7, M13 before writing mission primitives — everything else can ride along. The plan's instincts (server owns safety, ground-truth detections, ISR-only) are right; the doctrine layer is where it will fail the mission if you don't tighten it now.
