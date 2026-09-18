# Real-world data integration — verified surface

The user requirement is that the simulation be *realistic* and **use actual data**. God's Eye View
already ingests a large amount of real-world data.

**Status: wired.** `mcp/godseye_uav/realdata.py` ingests it and the MCP server consumes it. What
changed, with the measurements that prove it:

| Was | Is |
|---|---|
| "AGL" meant *height above the launch point* | measured against real terrain. Over the Fordow ridge the aircraft reads `alt_agl_m = -42.2` (42 m **below** the ridge) where the old number said `+39.9` — an 82 m disagreement |
| `uav_los_check` used a geometric horizon; a mountain was invisible | sight line cut against bare-earth terrain: `los=False` while the old model said clear, first obstacle at 89.5 m blocking by 37.3 m |
| min-AGL / ceiling measured from the launch datum | terrain-relative — the ridge case raises `min_agl` where the old figure raised nothing |
| geofence had no floor | `uav://safety/geofence` publishes a `terrain_floor` from the highest ground in the AO |
| targets spawned at one hand-entered elevation for the whole AO | each target defaults to the measured ground under it |
| fuel wind (M15) was operator-typed | `sim_set_weather(source='real')` drives it from the observation; 15 m/s from 090 flying east gives `last_headwind_mps +15.0` |
| no real order of battle, no traffic | `sim_spawn_order_of_battle` (mapped installations) and `uav_deconflict_airspace` (live traffic) |

Theaters are anchored to real places and their declared ground elevations are now pinned against
measured terrain by a test — two were wrong by **647 m** (Fordow) and **286 m** (Natanz) and are fixed.

Everything below was verified by reading the GEV source in this working tree, not assumed.

## What GEV actually provides

| Capability | Where | Contract | Mission use |
|---|---|---|---|
| **Real terrain elevation** | `gods-eye-view/server/providers/terrain.js:159` | `GET /api/terrain/heights?points=lon,lat;lon,lat;…` → ellipsoidal heights. Disk+memory cached, single-flight, serves stale on upstream error. `MAX_POINTS` cap per request. Upstream: Re:Earth Terrain / Mapterhorn (CC BY 4.0), geoid EGM2008 (NGA, public domain). | True **AGL** instead of height-above-takeoff; terrain-aware **LOS** (`uav_los_check`); a geofence *floor*; terrain-following route planning; honest masking of contacts behind ridgelines. |
| **Real military installations** | `gods-eye-view/server/providers/military-installations.js`, `src/data/militaryInstallations.js`, cached under `.gev-cache/military-installations/` | mapped-site catalog + `searchNearby` | Seed the **order-of-battle** for a theater from real mapped installations instead of hand-spawned meshes; gives every theater a plausible target set anchored to real places. |
| **Real live air traffic** | `gods-eye-view/server/providers/aircraft/`, `src/sources/live/aircraft.js` | OpenSky (primary), `adsb.lol` point API fallback `api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{radius}` (ODbL) | **Deconfliction**: real aircraft in the AO become airspace contacts the UAV must avoid; a realistic reason for altitude blocks and hold orders. |
| **Real weather + wind** | Open-Meteo (CC BY 4.0), used today for cockpit local info | current conditions per lat/lon | Drive `sim_set_weather` and the **wind vector that feeds the fuel model (M15)** from the theater's *actual current weather*, and sensor degradation (M18) from real visibility/cloud. |
| **Photoreal 3D globe / imagery** | Google Photorealistic 3D Tiles; Esri World Imagery keyless fallback | tile services | The operator sees the mission over real imagery of the real place — this is the "realistic environment" the C2 view already delivers. |

## Integration rules

1. **Terrain is the highest-value item.** It converts three currently-fake quantities (AGL, LOS,
   geofence floor) into real ones. Everything else is additive.
2. **Never block a mission on a network fetch.** Every real-data feed must fail soft to the current
   synthetic behaviour, and the degraded state must be *visible* (a flag in the snapshot / a logged
   warning), never a silent substitution — that is the same failure mode as the dead EGM96 path.
3. **Cache aggressively and respect provider terms.** Several sources carry attribution requirements
   and rate limits (Nominatim 1 req/s; adsb.lol/OSM ODbL attribution; Open-Meteo CC BY 4.0;
   OpenSky is **non-commercial research/education only**). Reuse GEV's existing proxy+cache layers
   rather than calling upstreams directly from the Python side, so the existing attribution and
   rate-limit handling is not bypassed.
4. **Theaters become real places.** Each theater in `theaters.py` should be able to hydrate its OB from
   real installations near its home point and its weather from the real forecast at that point.
5. **ISR-only.** Real installation data is used for *observation and reporting* context. No targeting
   for strike — the system reports, the operator decides (M14).

## Honest limits to document

- The UAV itself remains simulated (AirSim or the built-in fake); only the *environment* is real.
- OpenSky/adsb.lol coverage is uneven — some theaters will have little or no real air traffic.
- Real installation data is *mapped* data (OSM/Overpass, Google Places) and is incomplete; it is
  context, not an authoritative order of battle. Say so in any report that uses it.
