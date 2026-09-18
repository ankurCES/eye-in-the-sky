# Telemetry bridge ↔ command-center contract (PLAN §3.1)

The bridge is the **read-only** feed layer. GEV never commands flight — the MCP server is the only
command path. This document is the contract between `mcp/godseye_uav/bridge.py` and the GEV `uav` layer.

**Measured baseline (before Wave 3):** `/snapshot` emits only `{sim_state, observedAtMs, count, vehicles}`;
`/mission-overlay` returns a hardcoded empty FeatureCollection that nothing ever writes to;
`/events` returns **404**. Three of the six §3.1 feeds do not exist.

## Feeds

| Feed | Endpoint | Rate | Status |
|---|---|---|---|
| Vehicle telemetry | `GET /snapshot` → `vehicles[]` | 5 Hz poll | exists |
| **Mission state** | `GET /snapshot` → `missions[]` | 1 Hz | **MISSING** |
| **Contact roster** | `GET /snapshot` → `contacts[]` | on change | **MISSING** |
| Mission overlays | `GET /mission-overlay` (GeoJSON) | on change | **EMPTY — never populated** |
| Sensor PIP | `GET /camera/{vehicle}` (JPEG) | 2–5 Hz, subscriber-gated | exists |
| **Alarms** | `GET /events` (SSE) | on event | **MISSING (404)** |

All endpoints except `/health` require `Authorization: Bearer <token>`.

## `/snapshot`

```jsonc
{
  "sim_state": "up",
  "observedAtMs": 1789658270821,
  "count": 1,
  "vehicles": [ { /* existing shape — see below for required additions */ } ],
  "missions": [ /* NEW */ ],
  "contacts": [ /* NEW */ ]
}
```

### `vehicles[]` — required additions

The existing shape is kept (the GEV source adapter maps `vehicles` → `records`). It must additionally
carry, because the HUD needs them and they are currently defaults that are never populated:

| Field | Meaning |
|---|---|
| `fuel_pct` | live, **decreasing** — was pinned at 100.0 |
| `bingo_fuel_pct` | the BINGO line for the current position |
| `eta_to_bingo_s` | seconds of useful mission time left |
| `mission` | active mission id, or `""` — was always `""` |
| `track_id` | contact currently being tracked, or `""` |
| `alt_hae`, `alt_msl`, `agl` | all three, geoid applied **exactly once** (T1) |
| `datum_degraded` | true if the geoid source degraded — never hide this |

### `missions[]` — NEW (§3.1 feed row 3)

```jsonc
{
  "mission_id": "msn-0007",
  "vehicle": "Drone1",
  "kind": "grid_search",
  "phase": "executing",          // planning|executing|rtb|complete|aborted
  "active_tool": "uav_fly_route",
  "progress_pct": 43.5,           // server-derived from telemetry (T2), not a stub
  "waypoint": { "index": 6, "of": 14 },
  "eta_s": 480,
  "fuel_pct": 62.1,
  "bingo_fuel_pct": 24.8,
  "coverage_pct": 41.0,           // actually flown, never planned
  "safety": { "geofence": "ok", "proximity_m": 310, "bingo_latched": false },
  "incomplete_reason": null       // e.g. "incomplete - fuel" (M4)
}
```

### `contacts[]` — NEW (§3.1 feed row 5)

```jsonc
{
  "track_id": "TRK-…-0003",
  "category": "sam_medium_range",
  "confidence": "probable",       // confirmed|probable|possible
  "location": { "lat": 33.7241, "lon": 51.7238, "alt_m": 1548.0 },
  "last_seen_ms": 1789658270000,
  "threat_level": "high",
  "salute": { "size": "…", "activity": "…", "location": "…",
              "unit": "…", "time": "…", "equipment": "…" }
}
```

`location` is the **contact's** position, never the observer's.

## `/mission-overlay` — GeoJSON FeatureCollection

Rendered into a **separate `Cesium.CustomDataSource`**, never through the entity snapshot (T6).
Every feature carries `properties.kind` so the renderer can style it:

| `kind` | Geometry | Notes |
|---|---|---|
| `route` | LineString | planned route |
| `flown` | LineString | actually flown track |
| `waypoint` | Point | `properties.index`, `properties.reached` |
| `grid` | LineString/Polygon | the lawnmower or expanding-square pattern |
| `coverage` | Polygon | ground actually imaged — drives the honest coverage % |
| `geofence` | Polygon | the AO boundary |
| `threat_ring` | Polygon | `properties.track_id`, `properties.radius_m`, `properties.ring` (`engagement`\|`acquisition`) |
| `target` | Point | `properties.track_id`, `properties.confidence` |

Refresh on mission-state change, not on a fixed tick.

## `/events` — Server-Sent Events

One-way push, browser-native `EventSource`, reconnects cleanly. The only push channel in v1
(full bidirectional WebSocket stays Phase 6). Each event:

```
event: alarm
data: {"kind":"bingo","severity":"critical","vehicle":"Drone1","message":"BINGO fuel — forcing RTB","atMs":…}
```

Required `kind`s — PLAN §3.1 demands **at least three alarm types demonstrated**:

| kind | severity | Fires when |
|---|---|---|
| `bingo` | critical | BINGO reached; RTB forced and latched |
| `geofence_proximity` | warning | inside the geofence margin |
| `geofence_breach` | critical | outside the AO |
| `lost_link` | critical | link lost; names the lost-link plan that ran |
| `link_restored` | info | link back |
| `detection` | info | new contact promoted to a track |
| `mission_phase` | info | phase transition |
| `datum_degraded` | warning | geoid source degraded — the operator must know |

Send a comment heartbeat (`: ping`) periodically so idle proxies do not drop the stream, and make
disconnect/reconnect non-fatal for both sides.

## `/theaters` — the theater table and the ACTIVE theater

`GET /theaters` serves `theaters.as_payload()` verbatim (schema `godseye.theaters/v1`), so the browser
keeps no table of its own.

It also publishes **which theater the server is actually flying**. This exists because of a defect found
by running the stack and the UI together (`INTEGRATION_FINDINGS.md`, UI-1): the server was running
`iran-isfahan` while the panel showed Redmond POIs, because nothing told it otherwise. The table's
`default` is *not* the running theater and must never be read as one.

```jsonc
// GET /theaters -> "active"      GET /health -> "theater"   (same block, both endpoints)
{
  "known": true,                  // authority. false => do NOT adopt, whatever the id looks like
  "id": "iran-isfahan",
  "label": "Iran — Isfahan",
  "reason": null,                 // REQUIRED when known=false: e.g. "MCP server is not up"
  "theater_mismatch": null,       // set when the server's envelope disagrees with its theater row
  "source": "mcp:uav://safety/geofence",
  "at_ms": 1789680963833
}
```

Consumer rules:

- `known: false` **wins over a plausible id** — never adopt an unknown theater, and show the `reason`.
- A running theater the client's table does not carry is **reported**, never resolved to the default.
- The block being absent is today's legacy case: degrade quietly, keep working, say "not published".
- A selector that disagrees with the running theater must be **visible**, not silently corrected.

## Rules

1. **Read-only.** The bridge exposes no flight command path. `/control/*` proxies to MCP and exists only
   for the GEV panel — it must not become a second command authority.
2. **T6 write cap.** Entity property writes capped at 2–5 Hz with client-side interpolation. The GEV
   layer currently uses `ConstantPositionProperty`, so motion *jumps* at the poll rate — use sampled
   position properties so 5 Hz data renders as smooth motion.
3. **Fail visibly.** `bridge.py:139` wraps `snapshot()` in a bare `except Exception` returning `None`,
   which would silently blank telemetry on a datum error. Degradation must surface as state
   (`sim_state`, `datum_degraded`, an SSE alarm), never as silence.
4. **Never block the poll on a slow feed.** Camera and any real-world data lookups must not stall
   `/snapshot`.
