# Integration findings — stack + browser, run together

The bridge feeds and the GEV command-center UI were built in parallel against `BRIDGE_CONTRACT.md` but
had never been run **together** until this pass. These findings come from booting the real stack
(`launch.py --theater iran-isfahan`, bridge :49603) plus the GEV dev server and driving the actual page.

## What works (verified in a real browser)

- The UAV mission-control panel renders over **real Esri satellite imagery of Isfahan**
  (MGRS 39S WS 6264 1319 / 32°39'16.56"N 051°40'04.80"E) — the environment really is the real place.
- The panel's theater list is sourced from the bridge, not a hardcoded copy: the page prints
  `theaters: bridge · godseye.theaters/v1`. The fourth duplicate table is retired.
- Selecting a theater re-seeds its POIs correctly (Redmond → *North/South/East Field*;
  Iran — Isfahan → *Isfahan North/Center/South*).
- `/snapshot` carries `missions[]`, `contacts[]` and `feeds{}`; `/theaters` and SSE `/events` both
  answer (`: godseye alarm stream`, `retry: 3000`).
- HUD cells are present and populated: PROGRESS, COVERAGE (FLOWN), FUEL vs BINGO, ETA TO BINGO,
  MISSION ETA, WAYPOINT, GEOFENCE, ALT MSL, AGL, CONTACTS.
- `/health` self-describes usefully: `datum_source: egm96-grid:us_nga_egm96_15.tif`,
  `datum_degraded: false`, the CORS policy and how to change it, the 8 SSE alarm kinds, and the
  mission-feed's MCP URL and poll count.

## Defects found

### UI-1 — nothing publishes the ACTIVE theater, so the operator can be looking at the wrong place

The server was running `--theater iran-isfahan` and the drone was correctly at `32.6546, 51.668`, but
the panel's theater selector initialised to **`default` (Redmond)** and listed **Redmond POIs**. The
operator is one click from seeding a target ~10,000 km from the aircraft.

The UI is not at fault — it has no way to know. Verified:

- `GET /theaters` returns `default: "default"` — that is the *table's* default, not what is running.
- `GET /health` has **no theater key at all** (`ok, sim_state, vehicles, datum_degraded, datum_source,
  telemetry_error, camera_error, vehicles_fallback, cors, events, mission_feed`).

**Fix:** the bridge must publish the active theater (the MCP server knows it — `uav://safety/geofence`
already carries a `theater` block), and the panel must adopt it on load instead of defaulting. A
mismatch between the selector and the running theater should be visible, not silent.

### UI-2 — ETA TO BINGO reads `11:05:25`

A direct symptom of the unrealistic fuel burn rate (~0.0021 %/s → 11.2 h endurance). Being fixed in the
physics stream; noted here because it is what the operator actually sees on the HUD, and an
11-hour BINGO makes the whole fuel-margin readout decorative.

## Note on method

Both defects were invisible to unit tests and to each component's own verifier, because each side was
correct against the contract in isolation. They only appear when the two halves run together against a
server configured for a non-default theater. Worth keeping a periodic
"boot it and look at it" check in the loop — `scripts/demo_laptop.sh` is the natural home for it.
