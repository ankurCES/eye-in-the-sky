# Telemetry bridge contract (PLAN §3.1)

The bridge (`mcp/godseye_uav/bridge.py`, `create_app`) is the **read-only** feed layer between the
simulator and MCP server on one side and the UIs on the other: God's Eye View's `uav` layer (the map
in tracking mode) reads it over HTTP, and the intel graph behind the console reads its caches
in-process. It never commands flight; the MCP server is the only command path. Where this file and
the code disagree, the code wins.

## Where it runs

- **App host** (`godseye_uav.app` / `host.py`; `./eye-in-the-sky`, the desktop app, `start.sh`):
  `create_app` builds the one FastAPI app of the process. The host mounts `/mcp` on the same app and
  passes it `mcp_url=http://127.0.0.1:<port>/mcp` and the host's token, so the mission feed below
  polls MCP over loopback on the same port. Port 8780 for the app, 8790 (and 8791) under `start.sh`.
- **Legacy launcher** (`godseye_uav.launch`, used by tests): the bridge runs as its own listener on
  8790 and finds MCP from `GODSEYE_MCP_URL` / `GODSEYE_MCP_TOKEN` (default
  `http://127.0.0.1:8791/mcp`, the bridge token).

Every route except `/health` needs `Authorization: Bearer <token>`. `/events` also accepts
`?token=`, because `EventSource` cannot set headers.

## Routes

| Route | Response |
|---|---|
| `GET /health` | Open. `{ok, sim_state, vehicles, theater, datum_degraded, datum_source, telemetry_error, camera_error, vehicles_fallback, cors, events:{subscribers, published, kinds}, mission_feed:{url, polls, …feeds}}` |
| `GET /snapshot` | `{sim_state, observedAtMs, count, vehicles[], missions[], contacts[], feeds{}, theater{id, epoch}}`, a pure cache read |
| `GET /snapshot/{name}` | One vehicle row, or 404 |
| `GET /mission-overlay` | GeoJSON FeatureCollection |
| `GET /theaters` | `theaters.as_payload()` (schema `godseye.theaters/v1`) plus `active` |
| `GET /events` | Server-Sent Events, the alarm lane |
| `GET /tracks` | The MCP server's `uav_list_tracks`, unwrapped |
| `GET /camera/{veh}?cam=0&type=0` | Latest PNG frame; subscribes on first request; 503 when no frame |
| `POST /camera/subscribe` | Body `{vehicle, camera?, type?}` → `{subscribed, ttl_s, vehicle, camera, type, active[]}` |
| `POST /control/mission` | Forwards `{vehicle, kind, params, speed_mps?}` to `uav_mission` |
| `POST /control/command` | Forwards `{tool, arguments, vehicle}` to that MCP tool |
| `GET /control/status/{veh}` | `uav_get_telemetry` for that vehicle |

`/control/*` exists for God's Eye View's mission panel and the console's direct Abort
(`POST /control/command {tool:"uav_abort", vehicle}`). It adds no command of its own, and the
analyst never uses it. `tests/test_bridge.py` pins the exact POST route set (`/control/mission`,
`/control/command`, `/camera/subscribe`), so new routes are never added inside `create_app`.

## `/snapshot`

```jsonc
{
  "sim_state": "up",              // "connecting" | "up" | "up: datum_degraded" | "down: <error>"
  "observedAtMs": 1789658270821,
  "count": 1,
  "vehicles": [ /* rows, below */ ],
  "missions": [ /* rows, below */ ],
  "contacts": [ /* rows, below */ ],
  "feeds": { "mission_state": {"ok": true, "atMs": 0, "detail": "…"}, "contacts": {…} },
  "theater": {"id": "dyn-bengaluru-centre-1a2b3c", "epoch": 1}   // WG v2 §3.4
}
```

`sim_state` goes `down` only when the sim host does not answer (a transport error or timeout). When
the sim answers with an error for one vehicle, for example a datalink it reports lost after
`sim_set_link_state`, the client is kept, `sim_state` stays `up`, and that vehicle's row goes stale
with the error (`bridge.sim_answered`). `feeds` reports each loop-C feed (`mission_state`,
`contacts`, and `loop_c` when a poll raised) as `{ok, atMs, error?, detail?}`; a missing MCP tool or
resource shows up there, never as a plausible default.

`theater` is the running theater's id and epoch, both read from one cached `uav://safety/geofence`
document (`MissionFeed.theater_ref()`), so a new id is never paired with an old epoch. `epoch` is
the server's `theater_epoch`: 0 at a fresh boot, one more after each runtime switch, and kept
across a restart that restores the same theater. It is `null` when the server publishes no plain
integer (an older server, or a bool or string), when the theater is unknown, and for up to one
loop-C poll (about 0.5 s) after a switch while the geofence is re-read. Clients reset trails and
overlays on an id change or on a change between two non-null epochs, so one switch causes one reset.
The active block's keys (`ACTIVE_KEYS`) are unchanged.

### `vehicles[]`

| Field | Meaning |
|---|---|
| `name`, `latitude`, `longitude` | Position |
| `alt_hae`, `alt_msl` | Geoid applied exactly once (T1) |
| `agl`, `alt_agl_m` | The same number in two spellings. From the MCP server's tick (measured against terrain when the real-data layer is on), else height above the launch datum |
| `alt_agl_is_real`, `alt_agl_source`, `alt_agl_reason` | Whether that AGL was measured, and why not |
| `alt_agl_launch_datum_m`, `alt_agl_launch_datum_mismatch_m`, `alt_agl_launch_datum_check` | The bridge's own launch-datum figure and the frame cross-check, stated in words |
| `alt_agl_at_ms`, `alt_agl_age_ms`, `alt_agl_measured_age_ms`, `alt_agl_measured_age_source` | When the AGL was observed, and how old the server's measurement is |
| `speed_ms`, `heading_deg`, `pitch_deg`, `roll_deg`, `vx`, `vy`, `vz` | Kinematics |
| `landed_state`, `armed`, `timestamp_ms` | State |
| `fuel_pct`, `bingo_fuel_pct`, `eta_to_bingo_s`, `fuel_source` | From the server's fuel model; `null` with a reason in `fuel_source` when unknown, never 100 |
| `mission`, `track_id` | Active mission id and tracked contact, or `""` |
| `datum_degraded`, `datum_source` | True if the geoid source degraded; never hidden |
| `stale_ms`, `telemetry_error` | Set when the last sample failed: the last fix is kept, marked stale, with the error |

### `missions[]`

```jsonc
{
  "mission_id": "MSN-1a2b3c4d",
  "vehicle": "Drone1",
  "kind": "grid_search",
  "phase": "executing",            // planning | executing | rtb | complete | aborted
  "active_tool": "uav_fly_route",
  "progress_pct": 43.5,            // server-derived from telemetry (T2)
  "waypoint": {"index": 6, "of": 14},   // either value may be null when the server does not say
  "eta_s": 480,
  "fuel_pct": 62.1,
  "bingo_fuel_pct": 24.8,
  "coverage_pct": 41.0,            // actually flown; null (with the reason) unless a grid search
  "coverage_basis": "…",
  "safety": {"geofence": "ok", "proximity_m": 310, "bingo_latched": false},
  "incomplete_reason": null        // e.g. "incomplete - fuel" (M4)
}
```

A mission whose flying task has drained stays in the list with its final phase from
`mission_status` (so `complete`, `aborted` or `incomplete - fuel` is visible) for 120 s
(`MISSION_RETAIN_S`), then leaves it.

### `contacts[]`

```jsonc
{
  "track_id": "TRK-…",
  "category": "sam_medium_range",
  "confidence": "probable",        // confirmed | probable | possible
  "location": {"lat": 33.7241, "lon": 51.7238, "alt_m": 1548.0},
  "last_seen_ms": 1789658270000,
  "threat_level": "high",          // null when the server has not assessed this contact
  "salute": {"size": "…", "activity": "…", "location": "…", "unit": "…", "time": "…", "equipment": "…"}
}
```

`location` is the contact's position, never the observer's.

**Threat levels and rings.** Loop C calls `uav_assess_threat` for the area. Its summary answer
expands the top 10 contacts in `assessments` and lists every other assessed contact as a compact row
in `omitted`; both lists are folded into `threat_level`, so a contact the server assessed never
reads as unassessed. A contact in neither list (outside the assessed area) keeps `null`. Engagement
rings come from the expanded rows' `envelope_m`. Acquisition rings come from the assessment's
capability block when present, else from the track's own SALUTE row
(`equipment.acquisition_range_m`, the figure the threat model cites).

## `/mission-overlay`

A GeoJSON FeatureCollection, rendered into its own `Cesium.CustomDataSource` (T6), refreshed on
mission-state change. Every feature carries `properties.kind`:

| `kind` | Geometry | Notes |
|---|---|---|
| `route` | LineString | planned route |
| `waypoint` | Point | `properties.index` |
| `grid` | LineString/Polygon | lawnmower or expanding-square pattern |
| `flown` | LineString | the track actually flown. A fix more than 2 km (`FLOWN_JUMP_M`) from the last vertex starts a new track instead of drawing the jump; a theater switch clears every track (`reset_flown`) |
| `coverage` | Polygon | ground actually imaged; `properties.coverage_pct` |
| `geofence` | Polygon | the AO; `properties.enforced: false` when it came from the theater table rather than the server's envelope. Rebuilt once the geofence is re-read after a theater switch |
| `target` | Point | `properties.track_id`, `confidence`, `category`, `threat_level` |
| `threat_ring` | Polygon | `properties.track_id`, `radius_m`, `ring` (`engagement` \| `acquisition`) |

## `/events`

One-way SSE, reconnect-safe. The stream opens with a comment and `retry: 3000`, sends a comment
heartbeat (`: ping <unix seconds>`) when idle (every 15 s by default), and each alarm as:

```
event: alarm
data: {"kind":"bingo","severity":"critical","message":"…","atMs":…,"vehicle":"Drone1","mission_id":"…","detail":{…}}
```

`vehicle`, `track_id`, `mission_id` and `detail` appear only when set. A subscriber that fell behind
gets `detail.dropped_since_last`. The kinds are fixed (`ALARM_SEVERITY`, pinned by
`tests/test_bridge.py`); an unknown kind is refused rather than sent:

| kind | severity | Fires when |
|---|---|---|
| `bingo` | critical | BINGO reached; RTB forced and latched |
| `geofence_proximity` | warning | inside the geofence margin |
| `geofence_breach` | critical | outside the AO |
| `lost_link` | critical | link lost; names the lost-link plan that ran |
| `link_restored` | info | the link came back after a `lost_link` (never on a healthy vehicle's first poll) |
| `detection` | info | new contact promoted to a track |
| `mission_phase` | info | phase transition (`detail.from`, `detail.to`) |
| `datum_degraded` | warning | geoid source degraded |

## `/theaters` and the active theater

`GET /theaters` serves the theater table verbatim, so the browser keeps no table of its own, plus
`active`: the theater the running MCP server is enforcing, read from the `theater` block of
`uav://safety/geofence`. `GET /health` carries the same block as `theater`:

```jsonc
{
  "known": true,              // false => do NOT adopt it, whatever the id looks like
  "id": "iran-isfahan", "label": "Iran — Isfahan",   // or a chat theater's "dyn-…" id
  "ground_elevation_msl_m": 1570.0, "ao": [[lat, lon], …],
  "in_table": true,           // this bridge's table has the row
  "theater_mismatch": null,   // the server's own report that its envelope belongs to another theater
  "source": "mcp:uav://safety/geofence", "at_ms": 1789680963833,
  "reason": "…"               // why, when known is false
}
```

The table's `default` is never the running theater. `known: false` wins over a plausible id; a
selector that disagrees with the running theater is shown, not silently corrected
(`INTEGRATION_FINDINGS.md`, UI-1).

### Runtime theaters (WG v2 Phase A)

The theater can change while the app runs: the analyst proposes one and the operator approves
`sim_set_theater` (`INTEL_CONSOLE.md`, "Runtime theaters and sim speed"). The bridge follows:

- **The table grows.** A theater set from chat has a `dyn-…` id and lives in the process's dynamic
  registry (`theaters.register_dynamic`). `/theaters` lists it after the static rows, and the
  active block's `in_table` is true for it. The geofence document's `theater` block gains `epoch`
  (an integer) and `dynamic` (a bool); the rest of that document is unchanged.
- **Re-reading the geofence.** Loop C reads `uav://safety/geofence` at once when nothing is cached,
  retries every 30 s (`GEOFENCE_RETRY_S`) while no read has succeeded, and re-reads a good document
  every 30 s (`GEOFENCE_REREAD_S`). The slow re-read catches a theater change no listener reported,
  such as a separately restarted MCP server behind `GODSEYE_MCP_URL`. A change is the pair
  `(theater.id, theater.epoch)` changing, so an airframe-only switch (same id, new epoch) counts. A
  failed re-read keeps the last good document.
- **`MissionFeed.invalidate_geofence()`** drops the cached document and the overlay key under the
  feed lock. It does no I/O and never raises. Until the next read lands, `active_theater()` is
  `known: false` with the reason "the theater changed; uav://safety/geofence is being re-read"
  (or the upstream error if that read fails). A read that was already in flight when the
  invalidate happened is thrown away, because it may describe the old theater.
- **`BridgeState.reset_flown()`** clears every vehicle's flown track.
- **`AirSimAdapter.home_geo`** is now a read-only property that returns a `geo.GeoPoint` (HAE);
  it used to be a `HomeGeoPoint` attribute. **`AirSimAdapter.relocate(new_home: GeoPoint)`** moves
  the adapter's origin copy: it sets `home_declared` and `home_datum = "hae"` and swaps the cached
  projection in one assignment, so a telemetry sample in flight uses the old origin or the new one,
  never a mix. A non-finite or out-of-range point raises `ValueError` and keeps the old origin.
  Nothing is converted here: the switch converts the theater's MSL ground to HAE once (T1).
- **The host wires it.** `host.py` registers the adapter with `server.attach_origin_holder(adapter)`,
  so a switch moves it together with the fake simulator's and the MCP backend's copies and
  cross-checks all three against the simulator's own `getHomeGeoPoint`. It also adds a theater
  listener that runs, after every switch, `feed.invalidate_geofence()`, then
  `app.state.bridge.reset_flown()`, then the intel graph's cache invalidation. The feed is read
  when the listener runs, because it can be swapped. Every step runs even if an earlier one
  fails; the failures are raised together afterwards, and the switch records a
  `theater_listener_failed` audit row but still completes.
- **The legacy launcher** (`launch.py`) attaches no origin holder and adds no listener, so a
  runtime switch is refused there with "runtime theater change needs the app host".

## In-process access: `app.state.godseye`

Readers outside `create_app` (the host's intel graph and analyst) must not add routes inside it and
should not call the bridge over HTTP from the same process. `create_app` therefore publishes one
read-only namespace before it returns:

| Attribute | What |
|---|---|
| `state`, `hub`, `feed`, `mcp`, `adapter` | `BridgeState`, `EventHub`, the mission feed, the MCP client, the `AirSimAdapter` |
| `token` | The token this app checks, so a host can build its auth from it; never serialized |
| `active_theater()` | The active-theater block above |
| `snapshot()` | The `/snapshot` body |
| `theaters()` | The `/theaters` body |
| `sim_state()` | `adapter.sim_state` |
| `current_feed()`, `intel()` | The current feed and its cached `MissionIntel` |
| `track_rows()` | Raw `uav_list_tracks` rows behind `contacts[]` |
| `mission_details()` | `mission_id` → the cached `uav://mission/{id}` document |
| `threat_rings()` | `track_id` → `{engagement, acquisition}` radii in metres (or null) |
| `geofence_doc()` | The cached `uav://safety/geofence` document, or null |
| `recent_events(limit=50)` | The newest alarm payloads with their `seq` (limit clamped to 0–100) |

The callables read `state.feed` at call time, because the feed is swappable
(`create_app(state_source=…)`). `MissionFeed`'s own accessors (`mission_details()`,
`threat_rings()`, `geofence_doc()`, `track_rows()`) take the feed lock and return copies; callers
treat the nested values as read-only. A feed without an accessor yields an empty answer, not an
error.

`create_app` also leaves the `BridgeState` on `app.state.bridge` (the same object as
`app.state.godseye.state`). The host's theater listener reaches `bridge.feed.invalidate_geofence()`
and `bridge.reset_flown()` through it. Those two calls and `adapter.relocate()` (made by the switch
itself) are how the bridge follows an approved theater switch; none of them commands flight.

## Routes the host adds

`host.py` adds these to the same app from outside `create_app` (details in `INTEL_CONSOLE.md`):
`/app/config`, `/intel/graph`, `/intel/entity/{id}`, `/intel/events/recent`, `/intel/overlay`,
`/chat/*`, `/settings/llm*`, `/mcp`, `/api/*` (404 `not_available_in_app_host`), and the built UI at
`/` with the token injected into `index.html`. It also wraps the app in a loopback `Host`-header check and strips the bridge's CORS
headers from the token-bearing pages.

## CORS

The allowlist defaults to ports 4173, 5173 and 5199 on `localhost` and `127.0.0.1`.
`GODSEYE_BRIDGE_CORS_ORIGINS` replaces it (comma- or space-separated); `*` allows any origin and turns
credentials off. `GET /health` echoes the effective policy under `cors`.

## Rules

1. **Read-only.** The bridge exposes no flight command of its own. `/control/*` forwards to MCP and
   must not become a second command authority.
2. **T6 write cap.** Entity property writes are capped at 2–5 Hz, with client-side interpolation so
   5 Hz data renders as smooth motion.
3. **Fail visibly.** Degradation is state (`sim_state`, `datum_degraded`, `telemetry_error`,
   `feeds`, an SSE alarm), never silence and never a plausible default.
4. **Never block the poll on a slow feed.** `/snapshot` reads caches only; MCP, camera and
   real-world lookups run on their own loops (T3: telemetry ≤ 10 Hz, camera 2–5 Hz subscriber-gated,
   mission and intel state at 2 Hz).
