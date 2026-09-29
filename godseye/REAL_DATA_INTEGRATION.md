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

The GEV table below was verified by reading the GEV source in this working tree, not assumed. The
sections on the two switches and the direct upstreams were checked against `geo_http.py`,
`geocode.py`, `sites.py`, `realdata.py`, `theater_plan.py` and `theater_tools.py`.

## What GEV actually provides

| Capability | Where | Contract | Mission use |
|---|---|---|---|
| **Real terrain elevation** | `gods-eye-view/server/providers/terrain.js:159` | `GET /api/terrain/heights?points=lon,lat;lon,lat;…` → ellipsoidal heights. Disk+memory cached, single-flight, serves stale on upstream error. `MAX_POINTS` cap per request. Upstream: Re:Earth Terrain / Mapterhorn (CC BY 4.0), geoid EGM2008 (NGA, public domain). | True **AGL** instead of height-above-takeoff; terrain-aware **LOS** (`uav_los_check`); a geofence *floor*; terrain-following route planning; honest masking of contacts behind ridgelines. |
| **Real military installations** | `gods-eye-view/server/providers/military-installations.js`, `src/data/militaryInstallations.js`, cached under `.gev-cache/military-installations/` | mapped-site catalog + `searchNearby` | Seed the **order-of-battle** for a theater from real mapped installations instead of hand-spawned meshes; gives every theater a plausible target set anchored to real places. |
| **Real live air traffic** | `gods-eye-view/server/providers/aircraft/`, `src/sources/live/aircraft.js` | OpenSky (primary), `adsb.lol` point API fallback `api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{radius}` (ODbL) | **Deconfliction**: real aircraft in the AO become airspace contacts the UAV must avoid; a realistic reason for altitude blocks and hold orders. |
| **Real weather + wind** | Open-Meteo (CC BY 4.0), used today for cockpit local info | current conditions per lat/lon | Drive `sim_set_weather` and the **wind vector that feeds the fuel model (M15)** from the theater's *actual current weather*, and sensor degradation (M18) from real visibility/cloud. |
| **Photoreal 3D globe / imagery** | Google Photorealistic 3D Tiles; Esri World Imagery keyless fallback | tile services | The operator sees the mission over real imagery of the real place — this is the "realistic environment" the C2 view already delivers. |

## Two switches: map data and hydration (WG v2 Phase A)

Since runtime theaters (WG v2 Phase A, D6, C9, R25) there are two independent switches, and the
Python side can reach the public upstreams itself (the **direct** path) as well as through GEV's
proxies.

| Switch | What it covers | Default |
|---|---|---|
| **Map data**: `app.py --geodata on\|off`, `srv.geodata_enabled` | On-demand data for new theaters: place lookup (`geo_lookup`), mapped strategic sites (`geo_sites`, the orb's site band, the map's context overlay), and the open-ground home search and ground sample in `theater_propose` | **on** in the app (`--geodata on`, also passed by `start.sh`). Off for a default `HostConfig()`. The legacy launcher and a bare `GodseyeUavServer` read `GODSEYE_GEODATA` (unset = off), so in-process tests stay offline |
| **Hydration**: `app.py --real-data off\|direct\|gev`, `srv.real` | The safety loop's real-world layer described above: terrain AGL, terrain LOS, the geofence floor, target ground, real weather | **off**. Without the flag, `GODSEYE_REAL_DATA` decides (unset = off); an explicit `--real-data off` wins over the environment |

Hydration modes:

- `direct` (or `GODSEYE_REAL_DATA=direct`): terrain from Re:Earth and weather from Open-Meteo,
  fetched by `realdata.py` itself through `geo_http.py`. Mapped installations and live air
  traffic stay empty in this mode, with the visible reasons "direct mode: mapped installations are
  not hydrated into an order of battle; mapped sites are context from geo_sites (sites.py)" and
  "direct mode: live air traffic needs the God's Eye View proxy (--real-data gev)".
- `gev` (or `GODSEYE_REAL_DATA=1`): GEV's proxies at `GODSEYE_GEV_ORIGIN` (default
  `http://localhost:5199`), which only its vite dev server hosts. Everything in the table above.

With map data off: `geo_lookup` accepts coordinates only, sites are empty with the reason "map data
is off", a proposal's home is the AO centre, and a proposal for a new area needs the operator's
`ground_msl_m` (a theater-table row keeps its own).

### Direct upstreams

`geo_http.py` is the only place the direct path reaches the network. It lists these URLs and no
others:

| Upstream | URL | Used for | Minimum spacing |
|---|---|---|---|
| Photon (komoot) | `https://photon.komoot.io/api/` | place lookup, tried first | 1.0 s |
| Nominatim (OpenStreetMap) | `https://nominatim.openstreetmap.org/search` (`format=jsonv2`) | place lookup when Photon fails or finds nothing | 1.1 s |
| Overpass | `https://overpass-api.de/api/interpreter` (POST) | mapped sites and open ground for a home | 5.0 s |
| Overpass mirror | `https://overpass.kumi.systems/api/interpreter` | one retry after a failed Overpass request (not after an egress refusal or the size cap) | 5.0 s |
| Re:Earth | `https://terrain.reearth.land/heights.json` | ground height for a proposal; direct terrain hydration (5-decimal `lon,lat` points, 64 per request) | 0.25 s |
| Open-Meteo | `https://api.open-meteo.com/v1/forecast` and `/v1/elevation` | direct weather; the ground-height fallback | 0.5 s |

What `geo_http.py` does for every request:

- It sends one identifying User-Agent, `EyeInTheSky/<version> (+godseye; contact: …)`, where the
  contact is `GODSEYE_GEO_CONTACT`, else "unset". Nominatim's and Overpass's usage policies ask for
  a way to reach the operator of a client, so set it before real use.
- It holds one process-wide rate gate per upstream (the spacing above) and caps a response body at
  16 MB.
- `GODSEYE_NO_EGRESS=1` (or true, yes, on) makes it refuse before opening a socket, with the
  reason "egress disabled". The test suite sets it.
- Every failure becomes data (`real: false` plus a reason), never an exception into a tool and
  never silence.

Around it:

- **Cache.** `<store>/geodata-cache/` (memory only for an in-memory store): atomic JSON files and
  an in-memory layer. Place lookups are kept 30 days and site fetches 24 hours (keyed on the box at
  0.01° and the taxonomy version).
- **Tool budgets.** Uncached `geo_lookup` calls: 30 per 10 minutes. `geo_sites(refresh=true)`: 6
  per 10 minutes. `theater_propose` with map data on: 10 per 10 minutes. Past a budget the tool
  answers `rate_limited`.
- **What leaves the machine.** The text of a place search goes to Photon, and to Nominatim on a
  fallback. The theater's box goes to Overpass. Single points go to Re:Earth and Open-Meteo.
  Coordinates typed as coordinates, and names that match a theater-table row, are answered locally.
- **Off the loops.** Proposals run in a worker thread under a 60 s limit; a sites refresh runs in a
  worker thread; nothing on the tasking or monitor loop waits on these upstreams.

### Ground height for a new theater (T1, C10)

1. Primary: Re:Earth's ellipsoidal height, converted once through
   `geo.canonical_altitude(datum="hae")` to EGM96 sea level.
2. Fallback: Open-Meteo's `/v1/elevation` (Copernicus DEM), used as sea level. The proposal and
   the theater carry the sentence "Copernicus DEM via Open-Meteo (EGM2008), used as sea level; the
   geoid difference is not corrected."
3. The operator's `ground_msl_m` overrides both. With no answer at all the proposal is refused
   (`ground_unknown`: "No ground elevation could be measured here; give ground_msl_m.").

The theater stores MSL. Each activation converts MSL to HAE exactly once: the switch (§4.1.3 step
2a) or, at boot, `launch.home_geopoint`.

### Mapped strategic sites (`sites.py`)

One Overpass query per theater box, one `out tags bb <cap+1>` block per category; the first
matching category wins, and a category that fills its cap is flagged `capped`.

| Category | Cap | Category | Cap |
|---|---|---|---|
| airfield | 30 | bridge (`man_made=bridge`, never `bridge=yes`) | 20 |
| military_base | 40 | rail_hub | 20 |
| port | 20 | hq_gov | 30 |
| power | 40 | border_crossing | 10 |
| fuel | 20 | medical (**protected**) | 30 |
| comms | 30 | dam | 10 |

- An element that matches no category is dropped, not labelled `other`. Names and tag values are
  bidi- and control-stripped; only whitelisted tags are kept (`sites.TAG_WHITELIST`).
- Sites are **context only** (M14, D1, C3). They never enter `srv.targets`; a site node has no
  action, control or damage field; no graph edge points at a site; the site inspector has no
  engagement action. A missing site is not an absent one.
- Caps downstream: the intel graph carries the 60 most salient sites (fewer when the 150 KB graph
  budget needs it; `meta.sites` counts the rest), `/intel/overlay` serves up to 300 and labels the
  top 40, and the map draws at most 150.
- `sites.fetch_exclusion` (uncapped footprints plus hospitals as protected) is built for the Phase B
  wargame's placement gate; nothing in Phase A calls it.

## Integration rules

1. **Terrain is the highest-value item.** It converts three currently-fake quantities (AGL, LOS,
   geofence floor) into real ones. Everything else is additive.
2. **Never block a mission on a network fetch.** Every real-data feed must fail soft to the current
   synthetic behaviour, and the degraded state must be *visible* (a flag in the snapshot / a logged
   warning), never a silent substitution — that is the same failure mode as the dead EGM96 path.
3. **Cache aggressively and respect provider terms.** Several sources carry attribution requirements
   and rate limits (Nominatim 1 req/s; adsb.lol/OSM ODbL attribution; Open-Meteo CC BY 4.0;
   OpenSky is **non-commercial research/education only**). The `gev` path reuses GEV's proxy and
   cache layers. The direct path (WG v2 D6) calls the upstreams from Python, so `geo_http.py`
   carries that handling once: identifying User-Agent, per-upstream spacing, cache and call
   budgets. No other module may call these upstreams, and every answer carries its attribution
   (`geocode.ATTRIBUTION`, `sites.ATTRIBUTION`, `realdata.ATTRIBUTION`; see
   `../THIRD_PARTY_NOTICES.md` §4).
4. **Theaters become real places.** Each theater in `theaters.py` should be able to hydrate its OB from
   real installations near its home point and its weather from the real forecast at that point.
   Since Phase A any place can be the theater at runtime (`theater_propose`, then an approved
   `sim_set_theater`), and its mapped sites come with it.
5. **ISR-only.** Real installation data is used for *observation and reporting* context. No targeting
   for strike — the system reports, the operator decides (M14). Mapped sites are never targets and
   never enter the target list. Under M14a it is never a wargame target either.

## Honest limits to document

- The UAV itself remains simulated (AirSim or the built-in fake); only the *environment* is real.
- OpenSky/adsb.lol coverage is uneven — some theaters will have little or no real air traffic.
- Real installation data is *mapped* data (OSM/Overpass, Google Places) and is incomplete; it is
  context, not an authoritative order of battle. Say so in any report that uses it. Every graph
  that shows sites carries "Sites are mapped OpenStreetMap data (ODbL), not an order of battle.",
  and a degraded fetch adds "Map data feed down (…). Sites may be missing, not absent."
- Photon, Nominatim and Overpass are shared public services. Their fair-use limits are set by their
  operators and can change; the spacing and budgets here were chosen from the spec, not measured
  against those limits. Unverified here: the current published limits of each service.
- The Open-Meteo ground fallback is EGM2008-referenced and used as EGM96 sea level without
  correcting the geoid difference; the theater says so when it was used.
- Direct hydration has no mapped installations and no live air traffic (see the reasons above).
- A mapped site's position is its OSM bounds centre, not a surveyed point.
