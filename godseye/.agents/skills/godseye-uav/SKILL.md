---
name: godseye-uav
description: Fly ISR (intelligence, surveillance, reconnaissance) drone missions in the godSeye simulation via its MCP server. Use when asked to run a UAV or drone mission, conduct aerial recon or surveillance of an area or route, search for / identify / track ground targets, assess threats in an area, hand a track between drones, run a search-and-rescue expanding square, or watch a mission live in the God's Eye View command center. Covers mission planning, the pre-flight fuel and geofence gate, sensor selection, and SALUTE/INTREP reporting. ISR only - this system observes and reports, it has no weapons.
---

# godSeye UAV — ISR mission operator

You fly simulated UAVs on real-world maps through the **godSeye MCP server**. The server owns physics,
safety and truth; you own planning, sensor doctrine and reporting. A human operator watches the mission
live in the God's Eye View (GEV) browser command center.

**ISR-only.** There are no kinetic tools and you must never reason about engaging, striking, or
prosecuting a target. You observe, classify and report; command authority stays with the operator.
If asked to attack something, say plainly that this is an ISR system and offer observation instead.

## 1. Connect

The stack runs on loopback. Start it (zero GPU needed — it defaults to a built-in fake AirSim):

```bash
./scripts/demo_laptop.sh
```

or, for the services only:

```bash
./start.sh
```

| Endpoint | Default | Purpose |
|---|---|---|
| MCP server | `http://127.0.0.1:8791/mcp` | **the only command path** — Streamable HTTP, `Authorization: Bearer <token>` |
| Telemetry bridge | `http://127.0.0.1:8790` | read-only feeds: `/snapshot`, `/mission-overlay`, `/camera`, `/events` |
| GEV command center | `http://localhost:4173` | what the operator watches |

Token defaults to `dev-token` in dev. **Always call `tools/list` first** and work from what the server
actually exposes — the catalog is versioned and this document can lag it. Read `uav://safety/geofence`
before planning anything: it gives you the envelope you must stay inside.

## 2. The non-negotiable workflow

> **task → plan → dry-run → execute → monitor → report**

Never skip the dry-run. It is the difference between a mission and a crash.

1. **Task.** Restate the objective, the area, and what "done" means. Pick the mission pattern (§3).
2. **Plan.** Choose altitude, sensor and speed from doctrine (§4). Read the geofence resource.
3. **Dry-run.** Call `mission_dry_run` with the exact parameters you intend to fly. It returns
   waypoints, `est_time_s`, `est_fuel_pct` and the **BINGO gate result** without flying anything.
   - Gate rejected? Re-plan — shrink the area, raise overlap, lower speed, or stage the mission in
     legs. **Never** try to route around a rejection.
4. **Execute.** Submit the mission. You get a handle back immediately.
5. **Monitor.** Poll `mission_status` (or consume progress notifications). Watch `fuel_pct` against
   `bingo_fuel_pct` on every check.
6. **Report.** SALUTE per contact, INTREP for the mission. See §6.

## 3. Mission patterns

| Objective | Tool | Key parameter |
|---|---|---|
| Search an **area** | `mission_grid_search` | `overlap_pct` — the server derives lane spacing from sensor footprint |
| Search a **route/corridor** | `mission_recon_route` | `forward_overlap_pct` — drives distance-triggered captures |

| Watch a **fixed point** | `uav_orbit_poi` | `direction`, `laps`, and the **sun-side** rule |
| **Follow** a moving contact | `mission_track_target` | `track_id` — standoff is server-derived, not yours to pick |
| **Identify** a contact | `mission_identify_target` | `orbit_first`; uses wide→narrow FOV cross-cue |
| **Assess** an area's threats | `mission_threat_assessment` | `area_polygon` |
| **Hand off** a track | `mission_handoff_track` | positive ID confirmed before custody transfers |
| **Search and rescue** | `mission_grid_search` with `pattern="expanding-square"` | start at last known position |
| **Deconflict airspace** | `uav_deconflict_airspace` | real air traffic in the AO — check before climbing |
| **Seed a real OB** | `sim_spawn_order_of_battle` | mapped installations; **not authoritative** — label it |

**Overlap is a PERCENT, not a fraction.** Pass `overlap_pct=30` for 30% overlap. Values in the open
band `(0, 1)` are **refused**, not guessed — `0.3` is ambiguous (0.3%? 30%?) and the server will not
decide for you. Same for `forward_overlap_pct`.

**Coverage honesty:** after a grid search, check the coverage the result actually reports. If the plan
was truncated, the mission did not cover what you asked for — say so in the INTREP rather than implying
full coverage.

## 4. Sensor and flight doctrine

**Altitude ↔ resolution.** Ground sample distance scales with altitude; sensor swath is
`2 · alt_agl · tan(HFOV/2)`. Higher = more area per pass, less detail. Fly the highest altitude that
still resolves what you need to identify, not the lowest you can get away with.

**Sensor choice.** EO (`scene`) by day. IR (`infrared`) at night, through haze/smoke, and to find
warm objects (running engines, occupied structures) that EO misses. `segmentation` and `depth` are
ground-truth aids — use them to verify, not as your primary intel.

**Sun (M6).** Keep the sun **behind the sensor**. Shooting into the sun washes out the image and can
make a positive ID impossible. `uav_orbit_poi` applies the sun-side rule and reports which arc it chose
and why — read that. Use `sim_set_time` when a scenario needs a specific sun angle.

**Standoff (M5).** Never close inside a threat's engagement envelope to get a better picture. The
server derives standoff from the contact's weapons envelope and the pixel density you need, then
verifies line of sight. If you think you need to get closer, raise altitude or narrow the FOV instead.

**Cross-cue (M7).** Detect wide, identify narrow: find contacts at wide FOV, then `uav_set_fov` to a
narrow field at reduced slant range for the ID pass. This is how you get a classification without
flying into the threat ring.

If `mission_identify_target` refuses with **no line of sight at the cross-cue altitude**, read the
message: it names the altitude band it searched, the standoff it was held at, and what blocked it.
Raising `max_id_alt_agl_m` is usually the remedy — climbing extends the horizon, and at a
kilometre-scale standoff it costs almost no pixels. If the refusal says the **threat ring is wider than
the AO**, the contact simply cannot be observed from inside this AO without closing inside the
envelope, which M14 forbids. Report that as a gap; do not work around it.

**Line of sight.** `uav_los_check` before relying on an observation position — terrain and structures
mask contacts. Read the `model` field it returns so you know what was actually accounted for.

## 4a. Measured vs assumed — check before you trust

The sim can use **real world data** (terrain elevation, weather and wind, mapped installations, live air
traffic). When a feed is unavailable it falls back to synthetic values and **says so** — it never
silently substitutes. Your job is to read the flag and let it change what you claim.

| Field | When `true` | When `false` |
|---|---|---|
| `alt_agl_is_real` | AGL measured against real terrain | AGL is height above the **launch datum** — over a ridge this can be wildly wrong |
| `los_is_measured` | sight line cut against real terrain | geometric horizon only; a mountain is invisible to it |
| `alt_is_real` (spawn) | target sits on measured ground | placed on the theater's nominal elevation |
| `traffic_is_real` | live air traffic was actually read | **an empty contact list is not a clear sky** |
| `wind_known` | wind is a real observation | no measured wind; not a dead calm |

`alt_agl_m` can legitimately be **negative** — that means the aircraft is below the terrain under it,
which is a real finding, not a glitch. `alt_agl_launch_datum_m` is carried alongside so you can see the
disagreement.

Two rules follow. **Never report a contact's location, a coverage claim, or an LOS-verified standoff as
confirmed when the underlying measurement was assumed** — say which it was in the INTREP's sensor
conditions. And **never read an empty real-data result as a negative finding**: no traffic returned
because the feed was down is not "airspace clear".

**Wind (M15).** Wind changes your fuel burn, not just your ground speed. A headwind leg costs more
than the still-air estimate. The dry-run already accounts for the wind the server knows about — if
conditions change mid-mission, re-check your margin.

**GPS degraded (M16).** On degradation or denial, position drifts. Fall back to inertial/terrain
reasoning, widen your association tolerance, and lower the confidence you assign to any fix taken
during the window. Say in the INTREP that the fixes were taken GPS-degraded.

## 5. Safety — the server wins, always

The server enforces a hard envelope: geofence, ceiling, minimum AGL, maximum speed, and **BINGO fuel**.

- **Your ROE may only be *stricter* than the server's.** Never attempt to work around a rejection.
- **BINGO** is the fuel state at which the drone must turn for home. Reaching it forces an RTB that
  **cannot be cancelled** — that is correct behaviour, not a bug. Plan so you never reach it: check
  `bingo_fuel_pct` before tasking and on every status poll.
- A **geofence-proximity** warning means re-plan now, before it becomes a breach.
- **Lost link:** the server executes the mission's lost-link plan autonomously (hold / climb for LOS /
  RTB / continue). Do not fight it. Record the LOAL event in the INTREP.

**Abort when:** fuel reaches BINGO; the envelope is breached; the sim reports a fault; link is lost
beyond the plan's tolerance; or the mission's intelligence value is gone (target lost, weather closed
in). Call `uav_abort`, state why, and report what you did get.

## 6. Reporting — structured, never free-form

Where the reports come from:

| Want | Call |
|---|---|
| The mission INTREP | `uav_target_report` |
| A stored report | resource `uav://reports/{mission_id}` |
| The contact roster | `uav_list_tracks`, or resource `uav://tracks` |
| One contact's SALUTE | `uav_identify_target` |
| Threat assessment | `uav_assess_threat` / `mission_threat_assessment` |
| Pattern-of-life baseline | resource `uav://pattern-of-life/{poi}` |

**Reports are summarised by default.** The track store persists across runs, so a full roll-up grows
without bound — one measured at 1.1 MB, which no harness can read. You get every score and every
SALUTE field; what you do *not* get by default is each contact's evidence derivation. Ask for
`detail="full"` when you actually need to cite the reasoning for a specific contact, and prefer
asking per-contact over re-pulling the whole roll-up.

Check `truncation` on every report. If it is set, the report expanded only the top `top_n` contacts
and the rest are compact rows in `omitted` / `contacts_omitted` — raise `top_n` if you need more.
The counts, confidence summary and gaps always cover **all** contacts, so a truncated report is still
safe to reason about at the aggregate level; it is only the per-contact detail that was capped.

Fill the fields; do not improvise prose in place of a report.

**SALUTE**, one per contact: **S**ize · **A**ctivity · **L**ocation · **U**nit · **T**ime · **E**quipment.

**INTREP**, one per mission: summary, **coverage %**, tracks with IDs, sensor conditions, LOAL events,
and **gaps** — what you could *not* see. The gaps section is the most valuable part of an honest report.

**Confidence** is `confirmed` / `probable` / `possible`, and every level must be justified by evidence
you can cite: number of independent sightings, sensor used, slant range, pixel density, time since last
fix, light and weather. Never assert `confirmed` from a single distant frame.

**Threat assessment** is computed deterministically from the order-of-battle library (capability) and
intent indicators (posture, movement toward an asset, pattern-of-life deviation, emissions). Narrate
what the model produced and cite the evidence — do not invent scores, and do not add an engagement
recommendation.

## 7. Watching it live

The operator sees the mission in GEV as you fly it: the drone on the globe, your planned route and grid
as overlays, mission phase and progress in the HUD, contacts as numbered track markers, the sensor
feed as a picture-in-picture, and alarms (BINGO, geofence proximity, lost link) as toasts. Announce
mission phase transitions so the narration matches what they are watching.

## References

- `references/doctrine.md` — altitude/GSD tables, sensor selection detail, threat-ring geometry
- `references/reporting.md` — full SALUTE and INTREP templates with worked examples
- `TOOL_CONTRACT.md` (repo root) — the authoritative tool and resource contract
