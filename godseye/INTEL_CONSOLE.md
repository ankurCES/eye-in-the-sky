# Intelligence console and in-app analyst

This is the reference for the Eye in the Sky app: the single-process host, its HTTP and SSE contract,
the analyst's approval policy, and the data-honesty rules the console follows. Everything here was
checked against the code: `host.py`, `app.py`, `chat.py`, `analyst_policy.py`, `analyst_toolbelt.py`,
`intel_graph.py`, `intel_sites.py`, `intel_overlay.py`, `theater_tools.py`, `theater_plan.py`,
`theater_switch.py`, `llm_settings.py` and `llm_providers.py` in `mcp/godseye_uav/`, and
`gods-eye-view/src/console/` (plus `src/app/trackingPort.js` and `src/layers/uav/context*.js` for the
map). Where this file and the code disagree, the code wins.

**ISR-only.** The analyst observes, classifies and reports. Nothing it can call is kinetic, and threat
output is sensor-posture advice only. Every action that moves an aircraft, tasks a sensor or changes
the sim waits for the operator. That includes moving the simulation to another place: a real place
is context only, and its mapped sites are never targets.

## Contents

- [What the operator sees](#what-the-operator-sees)
- [Architecture: one process, one origin](#architecture-one-process-one-origin)
- [HTTP contract](#http-contract)
- [Chat stream (SSE)](#chat-stream-sse)
- [Approval classes](#approval-classes)
- [Session grants](#session-grants)
- [Order-slip rules](#order-slip-rules)
- [Tracking handoff](#tracking-handoff)
- [Map overview and mapped sites](#map-overview-and-mapped-sites)
- [The analyst's toolbelt](#the-analysts-toolbelt)
- [Runtime theaters and sim speed](#runtime-theaters-and-sim-speed)
- [Analyst sign-in and Anthropic's policy](#analyst-sign-in-and-anthropics-policy)
- [Analyst providers and keys](#analyst-providers-and-keys)
- [Settings routes](#settings-routes)
- [Environment variables](#environment-variables)
- [Where things are written](#where-things-are-written)
- [Observed cost](#observed-cost)
- [Data-honesty rules](#data-honesty-rules)
- [Known limits](#known-limits)

## What the operator sees

The app opens on an intelligence console, not a map:

- an **orb**: a 3D sphere of every intel entity (vehicles, missions, contacts, units, equipment
  classes, reports, theater and POIs, mapped sites, alarms, feeds), drawn with Canvas2D, with an
  Orb | List | Map view toggle;
- a **search and ask bar** (⌘K, Ctrl+K or `/`) that ranks graph nodes and ends with an
  "Ask the analyst" row;
- the **analyst chat**, which answers from the data and runs missions through the godseye tools;
- a **situation rail**: theater (how and when it was set, area, home, sim speed), sim status, fleet
  fuel against BINGO, running missions, alarms and data caveats;
- an **entity inspector** with measured and assumed values marked, related entities and actions
  (Ask about this, Focus, Track, Abort);
- **analyst settings** (⌘, or Ctrl+, or "Analyst settings…" in the analyst's menu): which model
  provider the analyst uses, its model and its key. The analyst's header names the model and the
  provider ("claude-opus-5 via Claude login (this Mac)").

The Cesium map from God's Eye View (GEV) appears in two modes only: **tracking mode**, following one
drone in the cockpit view, and **map overview**, which frames an area (the theater, sites, drones)
with GEV's chrome hidden (see [Map overview and mapped sites](#map-overview-and-mapped-sites)).
`?console=off` on the page URL skips the console and loads the plain GEV map application, first-run
launcher included (`gods-eye-view/src/main.js`).

## Architecture: one process, one origin

```
 python -m godseye_uav.app            (./eye-in-the-sky, godseye-app, the .app, start.sh --headless)
 ┌───────────────────────────────── one FastAPI app, one asyncio loop ─────────────────────────────┐
 │ 127.0.0.1:8780  (start.sh: 127.0.0.1:8790, and the same app again on :8791)                      │
 │                                                                                                   │
 │  /                 built console UI (gods-eye-view/dist), window.__GODSEYE__ injected            │
 │  /app/config  /intel/*  /chat/*  /settings/llm*    host routers, added outside create_app        │
 │  /mcp              GodseyeUavServer, Streamable HTTP + bearer       <── external MCP harnesses   │
 │  /health /snapshot /mission-overlay /theaters /events /tracks /control/* /camera/*   bridge      │
 │  /api/*            404 {"error":"not_available_in_app_host"}                                      │
 │                                                                                                   │
 │  IntelService ── reads app.state.godseye (bridge caches) + the server object, never over HTTP    │
 │  LlmSettings  ── the analyst's provider, its key (Keychain or 0600 file), the CLI's env          │
 │  ChatService  ── one actor task per chat session ── ClaudeSDKClient ──┐                          │
 │                  in-process SDK MCP server "godseye" (the toolbelt)   │ stdin/stdout              │
 │                  └─ server.mcp.call_tool(...) on the same loop        │                          │
 │  bridge mission feed ── polls /mcp over loopback HTTP (same port)     │                          │
 └────────────┬──────────────────────────────────────────────────────────┼──────────────────────────┘
              │ msgpack-rpc, loopback only                                ▼
      FakeAirSim (in-process threads)                        claude CLI child process ──► the chosen provider
      or a real AirSim with --real
```

- **Window mode** (`--window`, the default when pywebview imports and a display is available): the
  pywebview window (WKWebView on macOS) owns the main thread and loads `http://127.0.0.1:<port>/`; the
  host runs on a worker thread with its own event loop.
- **Browser mode** (`--browser`) and **headless mode** (`--headless`) run the host on the main thread.
  `start.sh` uses `--headless`.
- The host binds loopback only (`127.0.0.1`, `localhost` or `::1`; anything else is refused) and
  rejects any other `Host` header with 400, because the index page carries the token.
- The fake sim is patched to bind loopback only, and the host refuses to start if it cannot verify
  that.
- Two hosts cannot share a store: the store directory is locked (`<store>/.host.lock`).
- If `intel_graph`, `chat` or `llm_settings` fails to load or start, the host still boots and answers
  that module's routes with 503 (see below).

## HTTP contract

Auth is `Authorization: Bearer <token>` unless noted. SSE routes also accept `?token=` because
`EventSource` cannot set headers. Error bodies are top-level `{"error": ..., ...}`. A request body
that fails validation gets FastAPI's standard 422 (`{"detail": [...]}`).

| Route | Auth | Response |
|---|---|---|
| `GET /` and `GET /index.html` | none | The built UI with `<script>window.__GODSEYE__={"bridgeUrl":"","token":"…"}</script>` inserted before the first module script. Sent with `no-store`, `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'` and `Cross-Origin-Resource-Policy: same-origin`; CORS headers are stripped. If the UI is not built, a page that says how to build it. |
| `GET /app/config` | none | `{app:"eye-in-the-sky", version, theater:{id,label,epoch}, chat:{available, model, reason?, provider?:{id,label}}, mcp_path:"/mcp", ui:"built"\|"missing"}`. `theater` is read from the running server, so it follows a runtime switch; `epoch` is its `theater_epoch`. Never the token, and never the provider's host, base URL or key state (this route has no auth). |
| `GET /intel/graph?scope=theater\|all` | bearer | The intel graph (below). Bad scope: 422 `{error:"invalid_scope", scope, allowed}`. |
| `GET /intel/overlay?truth=0\|1&rev=` | bearer | The map's context features (below). `{rev, unchanged:true}` when `rev` is still current. Any other `truth` value: 422 `{error:"invalid_truth", allowed:["0","1"]}`. |
| `GET /intel/entity/{id}` | bearer | `{id, type, label, subtitle, status, requested_id?, fields, provenance, related:[{id,type,label,kind,dir}], related_omitted?, caveats, raw?}`, at most 60,000 bytes. Accepts a collapsed duplicate's id or a bare id. Unknown: 404 `{error:"unknown_entity", id}`. |
| `GET /intel/events/recent?limit=50` | bearer | `{events:[alarm payload + seq]}`, limit clamped to 1–100. |
| `GET /chat/status` | bearer | `{available, model, reason?, hint?, effort?, provider:{id, label, kind, model_family, host, key_source, configured}, cost_basis:"anthropic_list"\|"unreliable", settings_rev}`. Never reads a key and never carries one. `reason` is `disabled`, `sdk_missing` or `cli_missing` (the analyst can't run at all), or a provider reason (below). |
| `POST /chat/sessions` | bearer | `{session_id}`. The CLI is not started until the first message. With 8 sessions open, the least recently active idle one is closed; if none is idle, 409 `{error:"busy", message}`. |
| `POST /chat/sessions/{sid}/messages` | bearer | Body `{text (1–8000 chars), context?:{focused_ids:[graph id]}}` → 202 `{turn_id}`. 404 `{error:"unknown_session", session_id}`; 409 `{error:"busy", turn_id}` while a turn runs; 503 `{error:"unavailable", reason, hint?}`; 422 `{error:"invalid", message}`. Up to 20 valid `focused_ids` are prefixed to the prompt as `[[id]]` references. |
| `GET /chat/sessions/{sid}/stream` | bearer or `?token=` | `text/event-stream` (next section). Resume with the `Last-Event-ID` header or `?last_event_id=`. 404 for an unknown session. |
| `POST /chat/sessions/{sid}/approvals/{approval_id}` | bearer | Body `{decision:"approve"\|"deny"\|"approve_session", note? (≤ 2000)}` → `{ok:true}`. 404 `{error:"unknown_approval", approval_id}` (unknown, expired or already decided); 422 `{error:"not_allowed", message}` for `approve_session` on a class that cannot be granted. |
| `GET /chat/sessions/{sid}/grants` | bearer | `{grants:[{tool, since_ms}]}`. 404 `{error:"unknown_session", session_id}`. |
| `DELETE /chat/sessions/{sid}/grants/{tool}` | bearer | `{ok:true}`; idempotent for a known session, 404 `unknown_session` otherwise. |
| `POST /chat/sessions/{sid}/interrupt` | bearer | `{ok:true}`. Pending approvals resolve as `cancelled`. 404 for an unknown session. |
| `DELETE /chat/sessions/{sid}` | bearer | `{ok:true}`. Stops the session's CLI. 404 for an unknown session. |
| `GET /settings/llm`, `PUT /settings/llm`, `POST /settings/llm/test`, `DELETE /settings/llm/providers/{id}/key` | bearer, header only | The analyst's model-provider settings; see [Settings routes](#settings-routes). Same origin only; a key goes in and never comes back out. |
| `/mcp` | bearer | The godseye MCP server (51 tools, 8 resources; see `TOOL_CONTRACT.md`). A 401 names `http://127.0.0.1:<port>/.well-known/oauth-protected-resource` as its resource metadata; that address is not served (404). |
| `/api/{path}` | none | 404 `{error:"not_available_in_app_host"}`: GEV's node-only providers exist only under its vite dev server. |

When a module could not be loaded: `/intel/*` answers 503 `{error:"intel_unavailable"}`;
`GET /chat/status` answers `{available:false, reason:"sdk_missing"|"error", hint, model}` and the
other `/chat/*` routes 503 `{error:"chat_unavailable"}`. If `llm_settings` fails to load or its
routes can't be built, `/settings/*` answers 503 `{error:"settings_unavailable"}` and the analyst
runs on the Claude login. The bridge routes are unchanged; see `BRIDGE_CONTRACT.md`. The chat never
uses `/control/*`.

**`/chat/status` and the provider** (when the settings module loaded):

- `provider.key_source` is `login`, `environment`, `keychain`, `file`, `memory`, `cloud` (the cloud
  credential chain) or `none`. `provider.host` is where requests go (`api.anthropic.com` for the
  Claude login and an Anthropic key, the endpoint's host for a URL provider, the regional Bedrock,
  Vertex or Foundry host).
- The provider reasons make `available` false with the hint "Open analyst settings.":
  `provider_not_configured`, `provider_key_missing`, `settings_error`, and `provider_auth` (the
  provider rejected the key; its hint is "Open analyst settings to replace the key.", and it clears
  when the settings change or a later turn on the same settings succeeds).
- A provider reason does not block `POST …/messages`; only `disabled`, `sdk_missing` and
  `cli_missing` answer 503 there. With `provider_not_configured`, `provider_key_missing` or
  `settings_error` the turn starts, no CLI is spawned, and it ends with an `error` event whose `code`
  is `config`. With `provider_auth` the turn runs, so the provider can take the key after all.
- `model` is the active provider's model; `--model` and `GODSEYE_CHAT_MODEL` apply only to the two
  Claude-from-Anthropic kinds, and `effort` is shown only for the Claude kinds (login, API key,
  Bedrock, Vertex, Foundry).

### The intel graph

`GET /intel/graph` returns schema `godseye.intel-graph/v1`:

```jsonc
{
  "schema": "godseye.intel-graph/v1", "generated_at_ms": 0, "scope": "theater",
  "theater": {"id": "default", "label": "Redmond (AirSim default)", "place": "…", "known": true},
  "nodes": [{"id": "trk:TRK-…", "type": "track", "label": "…", "subtitle": "…", "group": "air-defense",
             "salience": 0.0, "status": "ok|warn|critical|stale|unknown", "ts_ms": 0, "lat": 0, "lon": 0,
             "attrs": {}}],
  "edges": [{"a": "veh:Drone1", "b": "msn:MSN-…", "kind": "flying"}],
  "meta": {"counts": {}, "tracks_total": 0, "out_of_theater": 0, "out_of_theater_contacts": 0,
           "duplicates_collapsed": 0, "tracks_omitted": 0, "threat_assessed": 0, "threat_unassessed": 0,
           "stale_contacts": 0, "scoped_to_theater": true, "ao_margin_m": 10000, "duplicate_radius_m": 25,
           "caveats": ["…"], "feeds": {"<name>": {"ok": true, "status": "ok", "error": "…", "at_ms": 0}},
           "theater_epoch": 1, "overlay_rev": "1:1727500000000:0:0",
           "sites": {"total": 41, "in_graph": 41, "omitted": 0, "degraded": false, "reason": null,
                     "fetched_at_ms": 0, "attribution": "© OpenStreetMap contributors, ODbL",
                     "caveat": "Sites are mapped OpenStreetMap data (ODbL), not an order of battle."}}
}
```

When the active theater is unknown, `theater` is `{id:null, label:null, place:null, known:false, reason}`.

**The theater block** (WG v2 §3.2). With the in-process server present (the app host), the block
always comes from `theater_tools.theater_state(server)`, even when the bridge still reports the old
theater. The same keys are on the active `thr:` node's `attrs`. Past `{id, label, place, known}` it
carries:

```jsonc
{"epoch": 1, "dynamic": true, "source": "chat",          // "preset" for a table theater
 "state": "active",                                      // "switching" during a switch
 "bbox": [s, w, n, e], "center": [lat, lon], "half_extent_m": 2500.0, "area_km2": 25.0,
 "home": {"lat": 0, "lon": 0, "alt_msl_m": 920.0, "name": "…|null",
          "source": "overpass-open-ground|ao-centre|operator|preset"},
 "ground_msl_m": 920.0, "ground_source": "Given in the request (not measured).",
 "airframe": {"id": "quad_suas_electric", "label": "Quad, small electric", "reach_m": 7350},
 "time_scale": 1.0, "geocoder": "Photon (OpenStreetMap)|Nominatim (OpenStreetMap)|Coordinates|Theater table|null",
 "query": "…|null", "set_at_ms": 0, "set_via": "console|mcp|boot|null",
 "previous": {"id": "default", "label": "Redmond (AirSim default)"}, "integrity_error": null}
```

`ground_source` is one of four sentences (`theater_plan.GROUND_SOURCE_TEXT`): Re:Earth converted to
EGM96 once; Copernicus DEM via Open-Meteo used as sea level without the geoid correction; set by the
operator; or the theater table's value. Without an in-process server the block carries only
`epoch` and `dynamic`, from the bridge's copy of the geofence document. `meta.theater_epoch` repeats
the epoch; `meta.overlay_rev` is the overlay feed's current `rev`.

| Node type | Id | Notes |
|---|---|---|
| vehicle | `veh:{name}` | `attrs`: `fuel_pct`, `bingo_fuel_pct`, `margin_pct`, `eta_to_bingo_s`, `bingo_latched`, `landed`, `agl_m`, `agl_is_real`, `link`, `link_lost_since_ms`, `mission`, `track_id`, `stale_ms`, `datum_degraded`, `lost_link {behaviour, declare_after_s, escalate_to_rtb_after_s, climb_to_m, source}`, `airframe {id, label}` |
| mission | `msn:{mission_id}` | `attrs`: `kind`, `phase`, `vehicle`, `progress_pct`, `eta_s`, `waypoint {index?, of?}`, `coverage_pct`, `geofence`, `incomplete_reason`, `ts_basis` |
| track (contact) | `trk:{track_id}` | `attrs`: `category`, `ob_class`, `confidence`, `threat`, `sightings`, `age_s`, `stale`, `duplicates`, `duplicate_count`, `unit`, `theater`, `outside_ao`, `out_of_theater`, `unlocated` |
| unit | `unit:{category}:{min member id}` | same-category contacts within the element radius, after duplicate collapse |
| equipment | `ob:{ob_class}` | the order-of-battle class |
| report | `rpt:{report_id}` | INTREP or THREATREP |
| theater | `thr:{theater_id}` | only the active theater in `theater` scope |
| POI | `poi:{theater_id}:{name}` | |
| site | `sit:{theater_id}:{osm_type}/{osm_id}` | A mapped OpenStreetMap feature around the active theater (WG v2 §3.2), context only. The 60 most salient; `group` is the category's sector. Label: the OSM name (bidi-stripped, ≤ 80 characters) or "unnamed {category}"; subtitle "{Category word}  Mapped, not verified". `attrs`: `category`, `subtype`, `osm {type, id}`, `bounds [s,w,n,e]` (null for a point), `tags` (≤ 6 whitelisted, never `name`, values ≤ 60 characters; may be absent after budget trimming), `tags_total`, `protected` (only when true: medical), `source:"osm"`, `register:"mapped"`, `fetched_at_ms`. Status is always `ok` and salience at most 0.5. |
| alarm | `alarm:{seq}` | the newest 50 |
| feed | `feed:{name}` | bridge feeds plus `sim`, `real_data` and `theater` |

Edge kinds: `flying` (vehicle → mission, only while the phase is planning, executing or rtb),
`tracking` (vehicle → track), `operating_in` (vehicle → theater), `target` and `observes`
(mission → track), `member_of` (track → unit), `is_a` (track → equipment), `in_theater`
(track, POI or site → theater), `near` (track → POI within 250 m), `reports_on` (report → track),
`about` (alarm → vehicle, track or mission). No edge ever points at a site.

**Sites in the graph.** `meta.sites` is `{total, in_graph, omitted, degraded, reason,
fetched_at_ms, attribution, caveat}`, plus `tags_trimmed` and `trimmed_for_budget` when the graph
had to shed site detail to stay near its 150 KB budget (tags first, then the least salient sites,
never below 12; a caveat says so). The caveat "Sites are mapped OpenStreetMap data (ODbL), not an
order of battle." is added whenever sites are drawn, and "Map data feed down (…). Sites may be
missing, not absent." when the fetch degraded for any reason other than map data being off. A site
set whose box does not overlap the theater's (the moment between a switch and the new area's sites)
is served as no sites, with a reason. `GET /intel/entity/sit:…` answers for every fetched site, also
those past the 60-node cap: full tags, `name`, `name_en`, a "near" list (contacts within 1 km and the
theater's POIs) and no action, control or damage field. `intel_search` type `site` (and `place` or
`places`, meaning theater, POI and site) finds them.

**`GET /intel/overlay`** (WG v2 §3.3) is what the map draws:

```jsonc
{"type": "FeatureCollection", "rev": "1:1727500000000:0:0", "theater": {"id": "…", "epoch": 1},
 "attribution": ["© OpenStreetMap contributors, ODbL"], "counts": {"site": 41}, "omitted": {},
 "sites": {"total": 41, "served": 41, "degraded": false, "reason": null, "fetched_at_ms": 0},
 "features": [{"type": "Feature", "id": "sit:…", "geometry": {"type": "Point", "coordinates": [lon, lat]},
   "properties": {"kind": "site", "id": "sit:…", "label": "…", "category": "airfield",
                  "protected": false, "register": "mapped", "salience": 0.5, "labelled": true,
                  "simulated": false, "truth": false}}]}
```

`rev` is `{theater_epoch}:{sites fetched_at_ms or 0}:{engine revision, 0 in Phase A}:{truth}`.
Features come most salient first: up to 300 are served (the rest counted in `omitted.site`), and
the top 40 are `labelled`. `site` is the only kind in Phase A; the map draws an unknown kind as a
grey point labelled "Unrecognised map item". Without an in-process server the body is an empty
collection whose `sites.reason` says so.

Vehicle status: `critical` for a latched BINGO, fuel at or below BINGO, a declared lost link
(`loal`), a telemetry error or telemetry older than 5 s; `warn` for a margin under 10 points, a
degraded or `pending` link, or a degraded datum; `unknown` without telemetry.

## Chat stream (SSE)

The stream starts with `retry: 3000`. Each event has `id: <seq>` (except `session`), `event: <name>`
and `data: <json>`. A comment heartbeat (`: ping <unix seconds>`) is sent every 15 s. The last 500
events per session are kept for replay.

| Event | Data |
|---|---|
| `session` | `{session_id, model, available, provider:{id,label}, last_seq, history_truncated}`; always first, with no `id:` line |
| `provider_changed` | `{from:{id,label,model}, to:{id,label,model}, memory:"kept"\|"cleared", at_ms}`; just before `turn_start`, when the provider or its settings changed since this session's last turn |
| `turn_start` | `{turn_id, text}` |
| `text_delta` | `{turn_id, text}` |
| `thinking` | `{turn_id, text}` (summarized thinking) |
| `tool_call` | `{turn_id, call_id, tool, title, class, args, summary}`; `tool` is the bare name, `args` are shrunk for display |
| `approval_request` | `{approval_id, call_id, tool, class, title, summary, args, consequences:[str], allow_session, expires_at_ms, vehicle, dry_runnable, grant_scope, acknowledge_required, dry_run?, theater_preview?, time_scale_preview?}`; `theater_preview` is always present for `sim_set_theater` and `time_scale_preview` for `sim_set_time_scale` |
| `approval_resolved` | `{approval_id, call_id, decision:"approved"\|"denied"\|"expired"\|"cancelled", tool, scope:"once"\|"session", note?}` |
| `tool_result` | `{call_id, ok, outcome:"ok"\|"rejected"\|"error"\|"busy"\|"not_run", rejected?, error?, busy_with?:{task_id?, mission_id?, tool?}, summary, bytes, truncated, entities:[graph id]}` |
| `ui` | `{action:"focus", ids, note?}`, `{action:"track", vehicle, reason}`, `{action:"orb"}`, `{action:"inspect", id}`, `{action:"theater", id:"thr:…", label?}` or `{action:"map", ids:[1–50 graph ids], reason}` |
| `usage` | `{turn_id, cost_usd?, session_cost_usd?, cost_basis?, input_tokens?, output_tokens?, rate_limit?:{status, resets_at, type}}`; the end-of-turn event carries `cost_basis` (`anthropic_list` or `unreliable`; with `unreliable` the two cost fields are left out), and an event sent when the CLI reports a rate limit carries only `turn_id` and `rate_limit` |
| `turn_end` | `{turn_id, stop:"end"\|"interrupted"\|"error"\|"max_turns", error?}` |
| `error` | `{message, hint?, retryable, code, provider:{id,label}}`; `code` is `auth`, `billing`, `model`, `rate_limit`, `invalid_request`, `server`, `network`, `config` or `unknown` |

Details that matter to a client:

- `dry_run` is attached when the session has a dry run for the same mission kind and vehicle:
  `{ok, gate:{ok, required_pct, available_pct, plan_fuel_pct, return_fuel_pct, reserve_pct,
  bingo_latched, bingo_fuel_pct, envelope_violations (≤ 8), warnings (≤ 8), est_time_s,
  est_distance_m, envelope?:{ceiling_m_agl, min_agl_m, max_speed_mps, geofence:"enforced"|"none"}},
  at_ms, tool, eta_s?, fuel_pct_after?, waypoints?, matches_args}`. `matches_args` compares the
  effective plans (each code path's own defaults filled in), not the raw arguments.
- `outcome` is `not_run` for a call the operator denied, let expire or interrupted; `busy` when the
  server answered `{status:"busy"}`; `rejected` for a gate rejection, `refused:true` or
  `accepted:false`.
- `input_tokens` includes cache reads and writes. `cost_usd` is per turn (the CLI reports a running
  total per process; the service takes the difference).
- `rate_limit` appears when the CLI reports one (a Claude subscription login does).
- `cost_basis: "unreliable"` is every provider except the Claude kinds (login, API key, Bedrock,
  Vertex, Foundry): the CLI prices a model it doesn't know from its own table, so the dollars would be
  invented. The console says the provider bills you directly.
- Errors from a provider other than the Claude login are worded per `code` and name the provider
  ("OpenRouter rejected the key." with the hint "Open analyst settings to replace the key."); the
  Claude login keeps its sign-in wording and hint. The HTTP status the CLI reports decides the code
  first, then the CLI's error label, then the error text.
- `provider_changed.memory` is `kept` when only the model or key changed and `cleared` when the
  provider, endpoint or account changed: the conversation does not resume across providers. It is
  sent only once the session has run a turn, and a running turn is never interrupted by a change.
- Every event is redacted before it is stored or sent: a known key, or any run of 12 of its
  characters, becomes `[redacted key]`. While a key is known, streamed `text_delta` and `thinking` hold back
  their last 11 characters until the next delta or the end of the block, so a key split across two
  deltas is still caught. If redaction fired during a turn, the CLI is stopped and that session's
  transcript is rewritten without the key before the next turn resumes from it.
- The stop reason is `max_turns` after 40 model turns in one message.
- Entity references in assistant text use `[[type:id|label]]` or `[[type:id]]` with the eleven graph
  prefixes (`veh msn trk unit ob rpt thr poi sit alarm feed`). The console renders them as chips.
- `acknowledge_required` is true for the `safety_override` class (`analyst_policy.Decision.acknowledge`);
  the slip then needs its acknowledgement checkbox before Approve works.
- `theater_preview` and `time_scale_preview` come from `theater_tools.approval_preview(server, tool,
  args)`, which reads caches only and never raises. `{}` (no server, or any error) makes the slip
  Deny-only. `theater_preview` is `{label, place, query, geocoder, center, bbox, half_extent_m,
  area_km2, clamped_from_km, home{lat, lon, name, source, distance_m}, ground_msl_m, ground_source,
  airframe{from, to, label, reach_m}, previous, now_after[{row, now, after}], resets, keeps,
  sites{total, degraded}, caveats, checks[{text, ok}]}`; when the call's arguments no longer match
  the proposal, the "Proposal still valid" check is false and `mismatch` names the fields.
  `time_scale_preview` is `{from, to, checks, caveats}`. See
  [Runtime theaters and sim speed](#runtime-theaters-and-sim-speed).
- `ui {action:"theater"}` is not a tool: the service emits it itself after an **approved**
  `sim_set_theater` whose result is `status:"accepted"`, never for `unchanged`, a `duplicate`
  replay, a refusal or a denied call. Its `label` is bidi-stripped and at most 80 characters.
  `ui {action:"map"}` comes from the analyst's `ui_show_map`. Any other `ui` action is refused
  inside the service before it reaches the stream.

## Approval classes

`analyst_policy.classify(tool, args)` is pure and fails closed: a tool it does not know is a
`command` with no session grant, and an exception inside it also returns `command`.

| Class | Asks the operator | Session grant | Tools |
|---|---|---|---|
| `read` | no | – | `uav_get_telemetry`, `uav_list_vehicles`, `uav_task_status`, `mission_status`, `uav_los_check`, `uav_target_report`, `uav_identify_target`, `uav_assess_threat`, `uav_list_ob_classes`, `uav_real_data_status`, `uav_deconflict_airspace`, `uav_list_tracks`, `geo_lookup`, `geo_sites`; the curated `intel_overview`, `intel_search`, `intel_entity`, `read_intel_resource`, `ui_focus`, `ui_track`, `ui_show_orb`, `ui_inspect`, `ui_show_map` |
| `plan` | no | – | `mission_dry_run`, and `dry_run: true` on a tool that honours it: `uav_mission`, `uav_orbit_poi`, `mission_grid_search`, `mission_recon_route`, `mission_track_target`, `mission_identify_target`, `mission_threat_assessment`, `mission_handoff_track`; `theater_propose` (always, whatever its arguments; it changes nothing) |
| `sensor` | yes | per tool | `uav_get_detections`, `uav_scan_targets`, `uav_capture_image`, `uav_set_gimbal`, `uav_set_fov` |
| `command` | yes, every call | no | `uav_takeoff`, `uav_land`, `uav_return_to_home`, `uav_goto_gps`, `uav_fly_route`, `uav_hover`, `uav_orbit_poi`, `uav_mission`, `mission_grid_search`, `mission_recon_route`, `mission_track_target`, `mission_identify_target`, `mission_threat_assessment`, `mission_handoff_track`, `uav_handoff_target`, `mission_cancel`, `uav_abort`; any unknown tool |
| `sim` | yes, every call | no | `sim_set_time`, `sim_set_weather`, `sim_spawn_target`, `sim_move_target`, `sim_set_gps_degradation`, `sim_hydrate_real_data`, `sim_spawn_order_of_battle`, `sim_set_environment`, `sim_set_theater`, `sim_set_time_scale` |
| `safety_override` | yes, every call | no | `sim_set_fuel` (clears the BINGO latch), `sim_set_link_state` (can trigger an autonomous return), `sim_reset` (drops in-flight tasks) |

Argument rules:

- `dry_run: true` makes a call a `plan` only for the tools listed in the `plan` row. The server ignores
  undeclared arguments, so `mission_cancel(dry_run=true)` is still a real cancel.
- Any call carrying a `lost_link_plan` (top level or inside `params`, including `params` given as a
  JSON string) is a `command`, even as a dry run: the server applies it to the vehicle's live plan
  before it looks at `dry_run`.
- Each decision carries a `title`, a one-line `summary` and `consequences`. Movement and mission
  consequences say the aircraft takes off first if it is on the ground; abort and cancel say what
  happens next. Bidi control characters are stripped from all three.
- Only the `read` tools go into the SDK's `allowed_tools` (which skips the permission callback).
  Plan calls reach `can_use_tool` and are allowed there when `classify(...).auto` is true.
- An unanswered approval expires after 10 minutes and is denied. The model is told the call was not
  run and not to retry unless the operator asks.
- `uav_list_tracks`, `sim_set_environment` and `uav_handoff_target` are classified but not exposed to
  the analyst (see the toolbelt).
- `theater_propose` is a plan whatever its arguments. The lost-link rule above still applies to
  `mission_dry_run` and to the dry-runnable tools, but not to the five theater tools: none of them
  declares `dry_run`, `params` or `lost_link_plan`, and the server drops undeclared arguments.
- `Decision.acknowledge` is true for `safety_override`; it becomes the event's
  `acknowledge_required`.
- The theater tools' consequences come from the call's arguments only. For `sim_set_theater`, for
  example: "Moves the simulation to Bengaluru centre: a 5.0 × 5.0 km area around 12.97160,
  77.59460.", that every drone is parked, landed, at the new home, that fuel and the BINGO latch
  are kept unless the airframe changes, when the switch is refused, and what is kept and cleared.
  For `sim_set_time_scale`: "Runs the fake simulator 10× faster (physics, fuel, sun). Link-loss
  timers stay in wall-clock seconds." (at ×1 it says "at normal speed").
- The console fails safe on a class it does not know: `classKey()` maps it to `unknown`, and the
  slip is Deny-only (no Approve element exists) with the note "The console can't approve the
  "{class}" class."

## Session grants

- Only `sensor` calls can be granted, one tool at a time (`grant_scope` names the tool).
  `approve_session` on any other class returns 422.
- A granted tool runs without a slip for the rest of that chat session. The call still appears as a
  `tool_call` and `tool_result`.
- Grants live in memory with the session. Closing the session, "New session" in the UI, or a host
  restart ends them. `DELETE /chat/sessions/{sid}/grants/{tool}` revokes one; the composer footer
  shows "Standing approval: …" and a Revoke control.

## Order-slip rules

Every `approval_request` renders as an order slip inline in the transcript
(`gods-eye-view/src/console/chat/slip.js`, `validate.js`).

- **Content**, top to bottom: the class band and expiry countdown; the title; the summary; "What
  happens" (the server's `consequences`, verbatim, plus one "Right now" line from the graph); "Based
  on" (the dry-run block, rendering only the fields present); how to stop or undo it; "Show exact
  request"; an optional note for the analyst.
- **Buttons**: Deny is on the left and first in tab order; Approve is on the right and names the
  action ("Approve launch", "Approve flight", "Approve scan", "Approve override", …). There is no
  keyboard shortcut for approval. ⌘Enter in the note field denies and sends the note.
- **Arming**: Approve is disabled for 800 ms after the slip appears, and re-arms when the slip moves or
  resizes by more than 4 px, is scrolled into view by Review, a checkbox changes, the validation state
  changes, or the page becomes visible again. Under reduced motion the label reads "Ready in 1 s".
- **What counts as a press**: Enter or Space on the focused, armed button without key repeat acts at
  once. A pointer click must go down and up on the armed button with `detail === 1`, and it takes
  effect 500 ms later; a second click, a double-click, Deny or closing the slip in that time cancels
  it. A double-click never approves.
- **Variants**:
  - gate failed on a current dry run: "Deny and re-plan" is the only primary; there is no Approve;
  - a dry-runnable call with no dry run: "Ask for a dry run" is the primary, "Approve without a dry
    run" is secondary;
  - stale: "Ask for a fresh dry run" is the primary, "Approve anyway" is secondary.
- **Stale** means one of these changed since the matching dry run came back:
  - `matches_args` is false;
  - fuel is 2 or more points away from what the vehicle's own burn predicts ((fuel − BINGO) / time
    to BINGO, over the time between the two readings); with no burn rate, or on the ground, 2 points
    of raw change;
  - the vehicle landed or took off, or moved more than 50 m;
  - the BINGO latch or the link state changed;
  - `sim_set_weather`, `sim_set_time`, `sim_set_fuel` or `sim_set_theater` was approved;
  - another command for the same vehicle was approved.

  Validation runs on every render, on every graph change, and again at the press; a press on a slip
  that just went stale is swallowed and the slip re-arms. A dry run from before this page load is
  shown as "can't compare", without demoting Approve.
- **Sensor slips** show a checkbox above the buttons, "Allow <tool> for this session" (for example
  "Allow Scan for targets for this session"), captioned that flying and simulation changes still ask
  every time. Checking it re-arms the slip and turns Approve into "Approve and allow for session".
- **Override slips** need an acknowledgement checkbox before Approve can be pressed, for example "I
  understand this clears Drone1's BINGO latch." for `sim_set_fuel`. The box also appears whenever
  the request says `acknowledge_required`.
- **Deny-only slips.** Approve is not rendered at all (only Deny and a reason line) when:
  - the class is one the console does not know (above);
  - a theater or sim-speed slip's preview is missing, or lacks a required key (`checks`, `center`,
    `bbox`, `home`, `airframe`, `ground_msl_m` for `theater_preview`; `checks`, `from`, `to` for
    `time_scale_preview`): "The console couldn't build this preview, so it can't be approved.";
  - the slip is blocked: any preview check is false, or the console's own reading of the graph
    (`chat/validateTheater.assessTheater`) finds a drone airborne, busy, BINGO-latched or with its
    link lost or pending, a theater epoch that moved since the request arrived, or a switch in
    progress. The line reads "This can't be approved now: {reason}." (adding "Land {vehicle} first,
    then ask again." for an airborne drone). It is re-checked at render, on every graph change and
    at the press; a press on a slip that just became blocked is swallowed with "Conditions changed.
    Review the slip again."
- **Theater slip** (`sim_set_theater`, `chat/slipTheater.js`): a sim band ("Changes the
  simulation"); the place and how it was found ("geocoded by Photon (OpenStreetMap)"); "What
  happens" (the consequences) and a "Right now" line; a 72 px footprint drawn from numbers only
  (area square, home dot, dashed reach circle, captioned "Not a map. Shows the area against
  Drone1's reach.", in warn with "Drone1 can't reach the far corners and return." when the reach is
  shorter than the half-diagonal); centre, area (with the clamp line when the geocoded box was cut
  to fit the airframe), home, ground and airframe rows; a Now | After table; what resets and what
  stays; the checks; the undo line; and "Approve theater change", with "Theater changes are
  approved one at a time." Every row hides when its field is absent.
- **Sim speed slip** (`sim_set_time_scale`): title "Set sim speed", summary "×N", the consequences
  followed by `time_scale_preview.caveats`, and "Approve change".
- **Queue**: several slips are ordered oldest first ("1 of 2"); two waiting slips for the same vehicle
  both warn that the later one will be refused as busy.
- After a decision the slip is filed as a one-line record (approved, denied with the note, expired,
  or cancelled) that can be re-opened read-only.

## Tracking handoff

- **Who can start tracking**: the operator (Track on the rail, inspector or search), the analyst
  (`ui_track`, which emits `ui {action:"track"}`), or the service itself when an approved `command`
  that puts an aircraft in flight returns `ok` (`ui {action:"track", reason:"mission launched"}`). A
  dry run, a threat assessment without `survey`, a `busy`, `rejected` or `error` result never
  auto-tracks.
- **Operator clicks act at once.** Analyst and launch requests show a 3 s notice with "Stay in
  console" (Esc cancels), and only when the composer is empty and unfocused, the stage had no input
  in the last 3 s, no slip is pending and the console is not already tracking another vehicle.
  Otherwise the notice is static with a Track button. A second vehicle's launch never switches the
  view. An analyst `ui_show_orb` while tracking uses the same 3 s notice.
- **Entering**: the console calls the tracking port (`gods-eye-view/src/app/trackingPort.js`):
  `whenReady()`, `setMapVisible(true)`, then `enter(vehicle)`, which enables the UAV layer, tracks the
  drone and enters GEV's cockpit. `enter` resolves true once `body.cockpit-mode` is set, and false after
  35 s or if superseded. It first waits for GEV's boot-time UAV start (up to 20 s) and, if it switched
  the layer on itself, for layer announcements to stop for 150 ms (up to 2 s). After 20 s the notice
  says the map is still starting.
- **Leaving**: "Back to console", the dock, GEV's own Esc, `c` or map-view switch all return to the
  orb; `exit()` leaves the cockpit without re-tracking. Tracking entered from the map overview
  returns to the map on Esc or GEV's own cockpit exit; the dock's "Back to console" always returns
  to the orb.
- While the map is hidden, the port swallows GEV's bare-letter shortcuts and the cockpit `c` key,
  and the map is not rendered.
- **Operator Abort** is not the analyst: the rail, inspector and tracking dock call the bridge's
  `POST /control/command {tool:"uav_abort", vehicle}` after a one-step confirm.

## Map overview and mapped sites

The map overview shows an area rather than following a drone (WG v2 §4.2.6, §4.2.7).

- **Ways in.** Map in the Orb | List | Map toggle and `M` in the orb (both open the active
  theater), Show on map on the rail, an inspector, the theater notice or a directive line, and the
  analyst's `ui_show_map` (`ui {action:"map"}`). The console resolves the ids to a box: a theater
  uses its `bbox`; other ids their positions plus 15 % padding, at least 1 km across. An analyst
  request follows the same gate as tracking: a 3 s notice ("The analyst suggests showing {label} on
  the map: "{reason}". Opening the map in 3 s.", with Stay in console) only when the composer is
  empty and unfocused, the stage had no input for 3 s, no slip is pending and the console is not
  tracking; otherwise a static toast with Show on map. An operator's click opens the map at once.
- **Port.** The tracking port gains `supports(name)`, `showArea({bbox:[s,w,n,e]} |
  {center:[lat,lon], radiusM}, {animate}) → Promise<bool>` (the only place a `[s,w,n,e]` box
  becomes a Cesium rectangle), `enterOverview()` / `exitOverview()` (which toggle
  `body.gev-console-overview`: GEV's title bar, docks, panels, HUDs and first-run launcher are
  hidden; `#cesium-credits` and the UAV alarm stack stay), `setOverlayVisibility({sites})` and
  `onPick(cb)`. Without `showArea` the Map button says "The map can't show areas in this build."
- **Entering and leaving.** The camera is set while the map is still hidden, then the map is shown
  and an iris opens from the target's node on the orb (the theater's, when it is among the ids);
  Esc or "Back to console" closes it again.
  The screen reader hears "Map of {label}. Press Escape to return to the console." From the map,
  Track enters the cockpit with a short crossfade.
- **Dock.** "Map  {label}  {W × H} km", Back to console, Track, a Sites toggle with its count, a
  Key, "{n} more sites not drawn." and "Map data: © OpenStreetMap contributors, ODbL." whenever
  sites are drawn. Picking a site or a drone on the map opens the inspector as a sheet.
- **Context overlay** (`layers/uav/contextOverlay.js`, its own Cesium data source). While the
  overview is on, the map polls `GET /intel/overlay` with its last `rev` every 3 s, waits 5 s after
  an error, and on a theater change (id or `/snapshot.theater.epoch`) drops what it drew and
  refetches without a `rev`. It draws at most 150 site billboards, labels only features marked `labelled`
  (bidi-stripped, at most 80 characters), and adds "Protected" under medical sites.
- **Trails across a switch.** A fix more than 2 km (`JUMP_GUARD_M`) from the last trail vertex
  starts a new trail, and an epoch change clears every trail and refetches the mission and context
  overlays, so a theater switch never draws a line across the globe.

**Sites on the orb and in the panels.** Sites sit in their own band at +42° latitude, one row, with
the caption "Sites {n} ({m} more on the map)" and, when the fetch degraded, "Map data feed down.
Sites may be missing, not absent." They are always drawn in the neutral colour with a category
glyph, never a status colour. The site inspector shows "Mapped, not verified" (plus "Protected"
for medical sites), the category with its OSM tag, name, coordinates, tags, the source line and the
fixed caveat, and nearby contacts and POIs. Its actions are Ask about this, Focus, Show on map and
Plan recce over this, which only fills the composer; **there is never an engagement action**.
Search's Places filter covers the theater, POIs and sites, with category chips. The UI never calls
Photon, Nominatim or Overpass itself.

**Theater changes in the UI.** After an approved switch the transcript shows "Theater set to
{chip}." with Show on map (it never moves the view by itself). When the graph's theater id or epoch
changes, the orb swaps the theater at its pole, and a caption "Theater changed to {label}. {n} items
arrived." stays up to 20 s. The rail's theater block shows how it was set ("Set from chat at
{time}, approved by you", "Set by an MCP client at {time}", "Set from chat before the last
restart" or "Preset theater"), the area and home, "Geocoded by {geocoder}" (or "Placed from
coordinates, not geocoded"), "Sim running at ×{n} speed" when the speed is not 1, and "Switching to
{label}…" while a switch runs. An unknown node type anywhere is drawn as a lilac "Unrecognised" item
that is never green.

## The analyst's toolbelt

`analyst_toolbelt.build_toolbelt` builds an in-process SDK MCP server named `godseye` per session:

- One tool per real server tool (same name, description and input schema), whose handler calls
  `server.mcp.call_tool` on the host's loop. No HTTP, and no token on the CLI's command line.
- Not exposed: `uav_list_tracks` (uncapped; the intel tools replace it), `sim_set_environment`
  (legacy; it zeroes the wind) and `uav_handoff_target` (the GEV panel's alias of
  `mission_handoff_track`). Resources are reachable only through `read_intel_resource`, which admits
  `uav://mission/{id}`, `uav://reports/{id|latest}`, `uav://pattern-of-life/{poi|all}`,
  `uav://safety/geofence` and `uav://{vehicle}/telemetry`. Sim ground truth (`uav://targets`) and
  camera images are not readable.
- Curated tools: `intel_overview`, `intel_search`, `intel_entity`, `read_intel_resource`, `ui_focus`,
  `ui_track`, `ui_show_orb`, `ui_inspect`, `ui_show_map`. That is 57 tools in all (48 proxied,
  9 curated), counted against the Phase A server's 51.
- `ui_show_map{ids, reason}` (both required) frames 1–50 graph ids on the map and emits
  `ui {action:"map", ids, reason}`. Ids are checked against the chip grammar only (not against the
  graph) and de-duplicated in order; a bad list gets the error `invalid_ids`, which never echoes the
  model's input. `reason` is at most 200 characters.
- The proxy marks every call it makes as coming from the console
  (`theater_tools.CALL_VIA = "console"` around `server.mcp.call_tool`, reset afterwards), so a
  switch approved in the console records `set_via: "console"`, and a direct `/mcp` call `"mcp"`.
- `intel_search` takes `site` as a type, and `place`/`places` for theater, POI and site together.
- Results are compact JSON capped at 24,000 characters (`intel_entity` at 20,000, `intel_overview`
  at 6,000 bytes from the service). Tools that take `detail` or `top_n` get `detail="summary"` and
  `top_n=10` when the model leaves them unset. Anything cut is marked: lists end in
  `{"_truncated":true,"_omitted":N,"_note":…}` and the result carries a top-level `_truncated`.
- The CLI runs with no built-in tools (`tools=[]`), no settings files (`setting_sources=[]`), only
  this MCP server (`strict_mcp_config`), `verbatim_prompts=True` (so `@path` and `/command` in chat
  text are plain text), permission mode `default`, and `cwd=<store>/analyst`. Only the model,
  thinking, effort and the child environment depend on the provider (`chat.provider_options`):
  thinking is adaptive with summarized display unless the provider's setting or its full check turned
  it off.
- The system prompt is `mcp/godseye_uav/analyst_prompt.md` (package data), distilled from the
  `godseye-uav` skill: ISR-only identity, task → plan → dry-run → execute → monitor → report, server
  gates win, measured versus assumed, SALUTE and INTREP, and the console's approval and chip rules.
  Its "Theaters: working anywhere" section teaches the theater flow (`geo_lookup`, then
  `theater_propose`, then `sim_set_theater` with `set_args` exactly), when to pick the group-3
  airframe (areas wider than about 6 km), sim speed and its caveats, and that mapped sites are
  context cited as `[[sit:…|name]]`. The identity is still "ISR only".
- A session's CLI (about 190 MB resident) is disconnected after 10 minutes without a turn; the next
  message reconnects and resumes the conversation. At most 8 sessions are kept.

## Runtime theaters and sim speed

WG v2 Phase A lets the operator put the simulated AO over any real place, from chat, and run the
fake simulator faster. The five server tools are specified in `TOOL_CONTRACT.md` §4.5; this section
is the console's side and the rules around them. The upstreams, caches and attribution are in
`REAL_DATA_INTEGRATION.md`.

**The flow.**

1. `geo_lookup{query}` (read, no slip): coordinates, then a theater-table name, then Photon, then
   Nominatim. With map data off (`--geodata off`) only coordinates work. The operator may also just
   give coordinates.
2. `theater_propose{lat, lon | place_id | theater_id, …}` (plan, no slip): sizes the AO for the
   airframe, picks a home, samples the ground and fetches the area's sites. It changes nothing and
   returns `set_args`. A proposal lives 30 minutes, at most 16 are kept, and it runs in a worker
   thread under a 60 s limit.
3. `sim_set_theater(**set_args)` (sim, a slip every time, never grantable for the session). The
   handler checks, in order: an idempotency replay (`status:"duplicate"`); the proposal exists
   (`proposal_expired`); every argument matches it to 1e-7° and 0.01 m, the AO vertex by vertex
   (`proposal_mismatch`, with `fields`); the same theater row (AO, home, ground, label, POIs) and
   airframe already run (`{ok, status:"unchanged"}`; a corrected home or ground under the same id
   switches). Then the switch runs on the tasking loop, shielded: a cancelled caller never stops
   it half-way.
4. On `status:"accepted"` the chat service emits `ui {action:"theater"}` (see the SSE section).

An `/mcp` caller may call `sim_set_theater` directly, with the same refusals; the theater then
records `set_via:"mcp"`.

**Sizing the AO** (`theater_plan.py`).

| | Quad, small electric (`quad_suas_electric`) | Fixed-wing, group 3 (`group3_fixed_wing`) |
|---|---|---|
| Reach (`safety.reach_radius_m`) | 7,350 m | 352,800 m |
| Largest half-extent, `clamp(0.4 × reach, 1,500, 25,000)` | 2,940 m | 25,000 m |
| Half-extent when none is given or geocoded | 2,500 m | 15,000 m |

- The half-extent is the requested one, else half the shorter side of the geocoded box, else the
  default above, clamped between 1,500 m and the largest. A clamp adds a caveat and `clamped_from_km`
  (the geocoded box's full width and height).
- Home: the operator's point, which must lie inside the AO (`home_outside_ao`). Otherwise the
  nearest named park, recreation ground, pitch, grass or meadow within 0.4 × the half-extent of the
  centre that keeps the demo box plus 100 m inside the AO. Otherwise the AO centre, with "Home is
  the AO centre; the ground there is at sea level (over water?)." when the ground there is 1 m or
  less. With map data off, the centre.
- Ground, sampled at the home point: Re:Earth converted to EGM96 once, else Open-Meteo used as sea
  level (with its caveat), and the operator's `ground_msl_m` overrides both. A table theater keeps
  its table value. No answer: `ground_unknown`, "No ground
  elevation could be measured here; give ground_msl_m."
- Up to three POIs: named sites first, then open-ground names.
- Caveats: the widest air-defence ring that would cover the whole AO and the shorter ones that also
  would (for example "A long-range air-defence ring (75 km) would cover all of this 5.0 km AO."),
  "Terrain masking is on." or "Terrain masking is off.", the mapped-data caveat and, for the
  group-3 profile, "Fuel model only; the fake still flies multirotor kinematics (hover, 20 m/s
  cap)."
- Refusals: `bad_args`, `unknown_airframe`, `unknown_theater`, `home_outside_ao`, `ground_unknown`,
  `theater_invalid` (with `problems` when the theater fails validation), and the tool's own
  `invalid_parameter`, `rate_limited` and `proposal_timeout`.

**The switch** (`theater_switch.switch`). It is refused (`error:"switch_refused"`, with one sentence
per reason in `reasons`, joined in `message`) when:

- "a theater switch is already running";
- "restart recovery still running";
- "a safety tick did not finish; try again";
- "real AirSim: the origin is fixed by settings.json; runtime theater change is not supported";
- "runtime theater change needs the app host" (no theater listener: the legacy launcher);
- "vehicle roster unavailable";
- "{vehicle}: telemetry unavailable, cannot prove it is landed";
- "{vehicle}: airborne (landed_state={n})";
- "{vehicle}: busy ({tool} {state})";
- "{vehicle}: BINGO latched; refuel with sim_set_fuel first";
- "{vehicle}: link lost" (also while the link is pending);
- "a forced RTB is flying";
- the theater's validation problems, verbatim;
- "the EGM96 geoid is unavailable".

What the safety monitor's caches already prove (a drone's last sample airborne, a task running, a
BINGO latch, a lost link, a forced RTB, no fake, no app host, recovery running) is refused BEFORE
the switch raises its flag, so a caller retrying a doomed switch never pauses the safety ticks or
the operator's commands; the RPC-proven checks then run under the flag as the authoritative gate.
Each refusal after the "already running" check is audited as `theater_switch_refused`. Otherwise,
with no await in between, the
switch converts the new home's MSL ground to HAE once (T1), moves the fake simulator's, the MCP
backend's and the bridge adapter's origin, and cross-checks all three and the simulator's own
`getHomeGeoPoint` (to 1e-9° and 1 mm). A Stop or a closed chat never interrupts this: once the
origin starts to move the switch runs to its end and only then reports the cancellation. Any
failure there (or the tasking loop itself shutting down mid-move) is
a `theater_integrity` refusal: every origin copy is put back at the old origin (the drones are
re-parked at the old home, landed), the server keeps `theater_integrity_error`, writes a
`theater_integrity` audit row, suspends the safety monitor's enforcement (the first suppressed
action per drone is audited as `enforcement_suspended`), and refuses every later command but
`uav_land` and `uav_hover` with `theater_integrity` until a restart. Then it moves the geofence and home (in
place), the fuel models (a new airframe gives every drone a full tank, a fuel row with
`reason:"airframe_changed"` and an `airframe_changed` audit row; otherwise only their home moves),
clears alarms in progress, bumps `theater_epoch`, records `set_at_ms`, `previous` and `set_via`,
defines the new POIs (old pattern-of-life baselines are kept), refits the terrain grid, stops the
old area's real data without waiting, replaces the sites, persists `theater.json`, writes a
`theater_changed` audit row, restarts hydration in the background when it is on, and calls the
theater listeners (a failing listener is audited as `theater_listener_failed` and never fails the
switch).

The result is `{ok, status:"accepted", theater:{id, label, epoch, dynamic}, previous,
home:{lat, lon, alt_msl_m, alt_hae_m, undulation_m, datum_source}, airframe:{id, changed},
fuel:"kept"|"full tank (airframe changed)", reset, kept, sites_loaded, real_data}`. `reset` and
`kept` are the fixed lists the slip also shows:

| Resets | Keeps |
|---|---|
| Vehicle positions (every drone is parked at the new home, landed) | Fuel level and BINGO latch (same airframe) |
| Geofence, home and every fuel model's home | Contacts and tracks |
| Alarms in progress | Reports and pattern of life |
| Per-tick caches and real data for the old area | Mission history and the audit trail |
| Mapped sites (replaced by the new area's) | Lost-link plan and sim speed |
| | Scene objects in the old area |

**Around a switch.** While it runs, `tick_once` returns `{"skipped": "theater switch"}` and every
command is refused with `theater_changed`: "A theater switch is running; plan again when it
finishes." Every gate result carries `theater_epoch`; a command whose gate was checked at an
earlier epoch is refused with `theater_changed`: "The theater changed after this plan was checked;
plan again." The chat service keeps its dry runs per epoch, so a dry run from the old theater is
never attached to a slip in the new one.

**Persistence** (`<store>/theater.json`, written atomically after every switch and at every boot):

```jsonc
{"schema": "godseye.theater-state/v1", "theater_id": "dyn-bengaluru-centre-1a2b3c",
 "theater": { /* Theater.as_dict() without its real_data block; null for a table theater */ },
 "airframe": "quad_suas_electric", "epoch": 1, "set_at_ms": 0,
 "set_via": "console|mcp|boot", "previous": {"id": "default", "label": "Redmond (AirSim default)"}}
```

A table theater is stored by id only, so the table wins on reload. Sim speed is not stored: a
restart runs at ×1.

**Boot** (`host.build_host`). The persisted file is read (and a chat theater registered) before the
boot theater is resolved:

- `--theater` wins, and is re-persisted; otherwise the persisted theater; otherwise `default`.
- The epoch and the airframe are kept only when the persisted theater is the one booted. An explicit
  `--airframe` wins even then.
- A file that cannot be used (unreadable, another schema, no theater, an invalid chat-theater row,
  an unknown theater or airframe, a bad epoch) boots `default`, prints a WARNING line, writes a
  `theater_restore_failed` audit row, and is rewritten. It never blocks boot.
- Under a real AirSim (`--real`) a persisted theater other than `default` is never restored, because
  `settings.json` fixes the origin; that is audited the same way.
- The restored epoch is set before the safety monitor starts, and the boot persists the running
  theater with `set_via:"boot"`. `/app/config.theater` then carries the restored `epoch`.

**Restart recovery** (A6b):

- Task journal rows carry `theater` and `theater_epoch`; fuel journal rows carry `airframe`,
  `theater` and `theater_epoch`.
- A task that would resume is re-gated first. Planned in another theater: it is aborted with the
  reason "planned in {X}, booted in {Y}", a forced RTB and a `restart_resume_regate_failed` audit
  row; the same happens when its route fails the gate. Otherwise it is resubmitted with the gate's
  epoch and audited as `restart_resume_regated`. Takeoff, land and hover resume without a route
  gate. A legacy row without `theater` is re-gated but never aborted for the theater alone.
- A fuel level is restored only when its row's airframe is the one booted. Otherwise the drone
  starts on a full tank, audited as `fuel_restore_airframe_mismatch`; rows without `airframe` are
  legacy and are restored as before.
- A theater switch is refused until recovery has finished ("restart recovery still running").
- The safety monitor re-lists the sim's vehicles every 20 passes, so a drone created after boot is
  monitored (`vehicle_roster_refreshed`).

**Sim speed** (`sim_set_time_scale{scale}`, 1 to 10, a sim slip every time). The fake simulator
only: under a real AirSim it is refused with `time_scale_refused`. Physics, fuel burn and the sun
run `scale` times faster; physics is sub-stepped so no step exceeds 0.1 s. The safety monitor's
pass interval is divided by the scale (never below 0.05 s). The change is audited as
`time_scale_changed`, and `environment()` and fuel records carry `time_scale`. The returned
caveats, which the slip also shows, are:

1. "Link-loss timers stay in wall-clock seconds."
2. "Detections and scans run in real time."
3. "The analyst's clock is wall time."
4. "Safety checks run every 0.5 sim-seconds." (the formula gives 0.5 at every allowed speed)
5. above ×3 only: "Camera captures are rate-limited in real time; coverage gaps are likely above
   ×3."

**Audit rows** added in Phase A: `theater_changed`, `theater_switch_refused`, `theater_integrity`,
`theater_listener_failed`, `enforcement_suspended`, `airframe_changed`, `theater_restore_failed`, `time_scale_changed`,
`submit_refused` (a command refused as `theater_changed` or `theater_integrity`),
`real_data_stale_dropped` (a hydration that finished after a switch), `restart_resume_regated`,
`restart_resume_regate_failed`, `fuel_restore_airframe_mismatch` and `vehicle_roster_refreshed`. No
safety violation kind or alarm kind was added.

## Analyst sign-in and Anthropic's policy

The analyst needs the `app` extra (`pip install -e './godseye[app]'`), which installs
`claude-agent-sdk`. It runs the Claude Code CLI that the SDK bundles (0.2.160 bundles CLI 2.1.283),
or `Contents/Helpers/claude` inside the desktop app. Without the SDK, `/chat/status` says
`sdk_missing`; with `--no-chat`, `disabled`; without a CLI, `cli_missing`.

Which model provider the analyst uses is chosen in the console: **Analyst settings…** in the
analyst's menu, or ⌘, (Ctrl+, off macOS). The default is the Claude login. The CLI never picks up a
credential from the app's environment by itself: at launch, right after `app.sanitize_env`,
`app.capture_llm_env` takes every provider, credential and model variable out of the process
environment, and each CLI is started with an environment built for the active provider (see
[Analyst providers and keys](#analyst-providers-and-keys)).

- **Your own Claude login, on your own machine.** Sign in once with `claude` then `/login` (Claude
  Code on your PATH, or the SDK's bundled binary at `claude_agent_sdk/_bundled/claude`); on macOS the
  CLI stores the login in the keychain. This is how the analyst was run and tested here. It uses
  your plan's usage limits, and the console shows the limit warnings the CLI reports. The settings
  sheet's note says it is for your own local use only.
- **An Anthropic API key** ("Anthropic API key" in settings) is the supported way to run the analyst
  with your own Anthropic account. Paste it in settings, or set `ANTHROPIC_API_KEY` when you launch
  the app. A launch key selects that provider unless another one was put in use in settings, and it
  can't be changed or removed in settings.
- **Anything you give to other people must use API-key authentication.** Anthropic's Agent SDK
  documentation says that, unless previously approved, third-party developers may not offer
  claude.ai login or its rate limits for products built on the SDK, and should use API-key
  authentication instead. A build distributed to anyone else must have its user supply their own
  Anthropic API key (or a cloud provider), not a Claude login. The SDK's own package metadata also
  states that its use is governed by Anthropic's Commercial Terms of Service (see
  `THIRD_PARTY_NOTICES.md`).
- **`ANTHROPIC_BASE_URL` at launch** counts only together with `ANTHROPIC_AUTH_TOKEN` (Bearer) or
  `ANTHROPIC_API_KEY` (x-api-key): the pair becomes the custom endpoint's URL and key, read-only in
  settings, and selects it unless another provider was put in use in settings. A base URL without a
  key is ignored with a warning, because the CLI would send the Claude login's token to that host.
  When the app is launched from inside a Claude Code session, `sanitize_env` drops that session's
  base URL first.
- A Finder launch of the `.app` does not see variables from your shell profile. Keys entered in
  settings don't need the environment; to pass a launch variable to the packaged app, start
  `"dist/Eye in the Sky.app/Contents/MacOS/EyeInTheSky"` from a terminal.
- On the Claude login, a sign-in failure surfaces as an `error` event ("The analyst could not sign in
  to Claude.") with `code:"auth"` and the hint "Sign in with the claude CLI (`claude` then /login)
  or set ANTHROPIC_API_KEY". Other providers get the wording in
  [Chat stream (SSE)](#chat-stream-sse).

## Analyst providers and keys

The provider catalog is `mcp/godseye_uav/llm_providers.py`: data only, each fact with its source, and
anything not confirmed by the provider's documentation or this project's offline runs marked
"Unverified:" in the notes the settings sheet shows. The repository `README.md` ("Analyst
providers") lists the 15 providers and what each needs. `llm_settings.py` holds the mechanics:

- **Kinds.** `anthropic_login` (the default) and `anthropic_key` (Claude from Anthropic); `bedrock`,
  `vertex` and `foundry` (Claude on your cloud, through the CLI's own switches);
  `anthropic_compatible` (a provider's Anthropic Messages endpoint: OpenRouter, MiniMax, DeepSeek,
  Moonshot, Z.ai, Zhipu, Alibaba Model Studio, Ollama, LM Studio); and `custom` (any
  Anthropic-compatible URL). The Claude kinds take `effort` and are priced
  (`cost_basis:"anthropic_list"`); the other two are `unreliable`.
- **The CLI's environment** is built in one place, `llm_settings.build_child_env`. The SDK can set a
  child variable but not unset one, so every build first writes `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN` and every `CLAUDE_CODE_USE_*` provider switch the bundled CLI reads as
  blank. The Claude login adds nothing else (only a launch `CLAUDE_CODE_OAUTH_TOKEN` or
  `CLAUDE_CONFIG_DIR`, if one was set). Every other kind adds its credential, base URL or cloud
  switch and fields, and the model pins, then a hardening set
  (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1`,
  `CLAUDE_CODE_DISABLE_FAST_MODE=1`, `CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL=1`,
  `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1`, `CLAUDE_CODE_ATTRIBUTION_HEADER=0`,
  `CLAUDE_CODE_MAX_RETRIES=2`) and its own `CLAUDE_CONFIG_DIR=<store>/analyst/claude-home`, so it
  never sees the Claude login. A kind that needs a key is not started without one
  (`provider_key_missing`): given a base URL and no key, the CLI sends the Claude login's token to
  that URL. The key reaches the CLI through its environment only, never its command line.
- **Where the data goes.** The analyst's messages, the intel picture it reads and every tool result
  go to the active provider's host: `provider.host` in `/chat/status`, "Requests go to …" in the
  sheet. In this project's offline tests, the CLI of a non-login provider, behind a deny-all proxy,
  made no connection other than to its endpoint, including over a 200-second idle.
- **Extended thinking** is adaptive (summarized) unless it is set to Off, the catalog turns it off
  (Alibaba Model Studio), or, for providers marked `auto`, a full check saw the provider reject it
  for that model; the check then turns it off and says so.
- **Checks.** "Test connection" runs the quick check where the provider has one: one direct HTTP
  request (the models list for an Anthropic key, the key-info route for OpenRouter, otherwise a
  one-token message, which the sheet marks as billable), 20 s timeout, redirects refused. The Claude
  login and the cloud kinds have no quick check. "Run a full check" runs one tiny turn through the
  analyst's own engine (the bundled CLI with the analyst's option builder, a check prompt and one
  dummy tool, in `<store>/analyst/claude-check`, 45 s). For a URL provider it first sends one POST to
  `{base}/v1/messages` that never follows a redirect: a 3xx fails the check with code `redirect`,
  because the CLI would follow it and re-send an `x-api-key` header to the new host. The same probe
  runs before the analyst starts a CLI for a URL provider, and a redirect refuses the turn with
  `code:"config"`. A passing full check returns a one-use `check_token`, valid for 10 minutes and
  bound to that config and key.
- **Use and Save.** "Use {provider}" puts a provider in use. It needs a ready config, a `check_token`
  for exactly that config and key (except the Claude login) and, for a model that isn't Claude, the
  acknowledgement "I understand; use it anyway". Save stores edits without switching providers,
  except that a change to the provider in use (endpoint, model, family or key) needs the same check
  and acknowledgement (the sheet runs the check for you; the host answers 409 without it). The
  acknowledgement is recorded only with a model that needs it, so one sent while a Claude model was
  selected doesn't cover a later non-Claude one. A provider in use whose stored acknowledgement
  doesn't cover its current model family is not started.
- **Switching** applies from the next message; a running turn finishes on the provider it started
  with. A new provider, endpoint or account field starts a fresh CLI conversation
  (`provider_changed` with `memory:"cleared"`); a new model or key keeps it (`memory:"kept"`).
- **Models that aren't Claude.** Anthropic doesn't support routing Claude Code to non-Claude models
  through any gateway, and the analyst is built and tested with Claude. The sheet says so on every
  such provider. The approval policy doesn't depend on the model: every command, sensor tasking and
  sim change still waits for the operator's order slip.
- **Keys are write-only.** No route returns a key; the sheet shows "Saved key ending in" and the
  last four characters. Where a key is kept:
  - on macOS, your default (login) Keychain: a generic password with service `eye-in-the-sky.llm`,
    account `<scope>:<provider id>` (a random scope per settings file) and label "Eye in the Sky LLM
    key", written through `security -i` with the key hex-encoded on stdin, never on a command line,
    and read back to confirm;
  - otherwise, or if the Keychain write fails, or with `GODSEYE_LLM_SECRET_STORE=file`, or for a
    store under a temporary directory: `llm-secrets.json` beside the store, mode 0600;
  - with `GODSEYE_LLM_SECRET_STORE=memory`: in memory, for that run only.

  A save deletes the provider's key from every other store, and "Remove key" deletes it from all of
  them (500 `key_store_failed` if a copy can't be removed). A key from the launch environment is
  used for that run only and never stored. Every settings, status and chat answer, every SSE event
  and the `godseye_uav` and `claude_agent_sdk` log lines pass through a redactor that knows every
  key the process holds.

## Settings routes

`llm_settings.llm_settings_router`, mounted before `/api/*` and the static UI, wrapped in
`SettingsGuardMiddleware`. Every answer is `Cache-Control: no-store` and passes through the key
redactor.

- **Auth**: `Authorization: Bearer <token>` only. A `token` query parameter is refused (400
  `token_in_url`), because a URL ends up in logs and history.
- **Same origin only**: an `Origin` other than `http://127.0.0.1:<port>`, `http://localhost:<port>`
  or `http://[::1]:<port>` (the app's own port), or a `Sec-Fetch-Site` other than `same-origin` or
  `none`, gets 403 `cross_origin`. No `Access-Control-*` header is ever sent, so a cross-origin
  preflight fails. The console served by vite's dev server is cross-origin and can't use the sheet.
- **Bodies**: `POST` and `PUT` need `Content-Type: application/json` (415 `json_required`) and at
  most 64 KiB (413 `too_large`).
- **Writes** need `If-Match: <rev>`, the `rev` from the last read (428 `if_match_required` without
  it, 409 `settings_conflict` with the current `rev` if it changed). A settings file written by a
  newer version is read-only (`read_only:true`, 409 `read_only` on a write).

| Route | Body | Answer |
|---|---|---|
| `GET /settings/llm` | – | `{schema:"eye-in-the-sky.llm-settings/1", rev, active, read_only, notice, locks:{provider, provider_env, model, model_source}, key_store:{kind, label, path}, providers:[…]}` |
| `PUT /settings/llm` | `{provider, model?, small_model?, base_url?, fields?, auth_scheme?, thinking?:"auto"\|"adaptive"\|"off", model_family? (custom only), allow_insecure_http?, key?:{action:"keep"\|"set"\|"clear", value?}, activate?, check_token?, acknowledge_non_claude?}` | The `GET` view. Everything is validated before anything is stored. |
| `DELETE /settings/llm/providers/{id}/key` | – | The `GET` view; the key is deleted from every store and the provider's test record is cleared. |
| `POST /settings/llm/test` | `{provider, depth:"quick"\|"full", key?, …the PUT config fields}` | `{ok, depth, host, status, code, message, hint, retryable, latency_ms, checked_at_ms, thinking, thinking_detected, check_token, model}`. Never writes settings. |

Each entry in `providers` carries: `id, label, kind, group, model_family, claude_prefixes,
base_url:{value, default, editable, required, presets, env}` (URL kinds only), `host, model,
small_model, small_model_effective, thinking, thinking_effective, models:{default, small_default,
suggestions, placeholder}, fields, values, auth:{scheme, schemes, key_label, key_optional,
key_needed}, auth_scheme, allow_insecure_http, ack_non_claude_at_ms, needs_ack,
key:{configured, source, masked, env}, status, ready, reason, tested:{depth, ok, at_ms,
thinking_detected, code?, current}, notes, docs_url, docs, quick_check, quick_check_billable,
cost_basis, effort, locked:{model, base_url, key}`. `key.masked` is "…" plus the last four
characters, never more. `status` is `active` (in use and ready), `ready`, `key_saved`,
`needs_check`, `not_configured` or `from_environment`.

**Base URL rules** (`validate_base_url`): https anywhere; http only for a loopback address, or for a
private network address (RFC 1918 or ULA) with `allow_insecure_http:true`; no user name, password,
query or fragment; no link-local or unspecified address; never the app's own address; the URL is
the API root, and the analyst adds `/v1/messages`. A provider with a fixed endpoint accepts only its
listed endpoints.

**Test results**: `code` is one of the `error.code` values, or `redirect`, `malformed` (the endpoint
didn't answer like an Anthropic Messages API) or `endpoint` (nothing at `{base}/v1/messages`). One
test runs at a time and full checks are at least 10 s apart (429 `test_busy`, with `retry_after_s`
for the cooldown).

Other refusals: 422 `invalid_settings` `{field, message}`; 404 `unknown_provider` or `not_found`;
409 `locked_by_environment` `{field, env}` for a provider, model or key set at launch; 409
`needs_check` or `needs_ack` (above); 500 `key_store_failed`; 503 `settings_unavailable`.

## Environment variables

| Variable | Read by | Meaning |
|---|---|---|
| `GODSEYE_TOKEN` | `app.py` | API and MCP bearer token. `--token` wins; otherwise a random token per launch. Removed from the process environment before anything is spawned, and never printed. `start.sh` passes its `TOKEN` this way. |
| `GODSEYE_CHAT_MODEL` | `chat.py`, `llm_settings.py` | Analyst model for the Claude login and an Anthropic API key. `--model` wins; default `claude-opus-5`. Either one locks that model in settings; other providers use the model chosen there. |
| `GODSEYE_CHAT_EFFORT` | `chat.py` | `low`, `medium`, `high`, `xhigh` or `max`. `--effort` wins; an unknown value is ignored with a warning; unset means the model's default. Passed only to the Claude kinds (login, API key, Bedrock, Vertex, Foundry). |
| `GODSEYE_LLM_PROVIDER` | `llm_settings.py` | A provider id from the catalog. Puts that provider in use for this launch and locks the choice in settings. An unknown id is ignored with a warning. |
| `GODSEYE_LLM_SECRET_STORE` | `llm_settings.py` | Where keys entered in settings are kept: `keychain`, `file` (`llm-secrets.json`, 0600) or `memory` (this run only). Unset: the Keychain on macOS, the file elsewhere, and the file for a store under a temporary directory (`/tmp`, `/var/folders`, `$TMPDIR`) unless this says `keychain`. Dev and test runs should use `file` so they never touch the login Keychain. |
| `GODSEYE_UI_DIR` | `host.py` | Built UI directory. `--ui-dir` wins; default `gods-eye-view/dist`, or `ui/` inside the frozen app. |
| `GODSEYE_AIRSIM_PYTHONCLIENT` | `app.py`, scripts | Location of the AirSim PythonClient; otherwise `../airsim/PythonClient`, then `godseye/.godseye/vendor/airsim/PythonClient`. |
| `GODSEYE_BRIDGE_CORS_ORIGINS` | `bridge.py` | Comma- or space-separated CORS origins, or `*`. Default: ports 4173, 5173 and 5199 on localhost and 127.0.0.1. The token pages never get CORS headers. |
| `GODSEYE_REAL_DATA` | `server.py` | Real-data layer (safety-loop hydration) switch when `--real-data` is not given: `1/true/yes/on/enable(d)` (GEV's proxies), `direct` (Re:Earth and Open-Meteo, no GEV), or `0/false/no/off/disable(d)`; unset is off. Any other value is an error at startup. An explicit `--real-data` wins, including `off`. |
| `GODSEYE_GEODATA` | `server.py` | Map-data switch (`on`/`off` and the same spellings) for a server built without an explicit value: the legacy `launch.py` stack and in-process servers. Unset is off. The app always passes `--geodata` (default `on`), so it does not read this. |
| `GODSEYE_GEO_CONTACT` | `geo_http.py` | Contact (an email or URL) put in the map-data User-Agent, `EyeInTheSky/<version> (+godseye; contact: …)`; "unset" otherwise. Nominatim's and Overpass's usage policies ask for one. |
| `GODSEYE_NO_EGRESS` | `geo_http.py` | `1` (or true, yes, on) makes every direct map-data and direct hydration request refuse before opening a socket ("egress disabled"). The test suite sets it for the whole session. |
| `GODSEYE_GEV_ORIGIN` | `realdata.py` | Where `--real-data gev` finds GEV's `/api` providers; default `http://localhost:5199`. GEV's vite dev server listens on 4173 by default (and `start.sh` uses `UI_PORT`, 4173), so set `GODSEYE_GEV_ORIGIN=http://localhost:4173` to use it. Not used by `direct` or by map data. |
| `GODSEYE_AIRFRAME` | `safety.py` | Fuel-model airframe profile when neither `--airframe` nor a restored theater names one. An unknown id is an error. |
| Provider, credential and model variables | `app.py`, `llm_settings.py` | Taken out of the process environment at launch (`app.LLM_ENV_VARS`, the same list as `llm_settings.CAPTURED_VARS`), so the CLI never inherits them: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`, `ANTHROPIC_MODEL` and the `ANTHROPIC_DEFAULT_*_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` pins, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR`, every `CLAUDE_CODE_USE_*` switch, the Bedrock, Vertex, Foundry, AWS-hosted and Google Cloud base URLs and keys, `OPENROUTER_API_KEY`, `MINIMAX_API_KEY`, `DEEPSEEK_API_KEY`, `MOONSHOT_API_KEY`, `ZAI_API_KEY`, `ZHIPU_API_KEY`, `DASHSCOPE_API_KEY`, `OLLAMA_API_KEY` and `GODSEYE_LLM_PROVIDER`. The settings read them once: a provider's key variable supplies that provider's key for the run (shown as coming from the environment, not editable); `ANTHROPIC_API_KEY` alone selects the Anthropic API key provider, `CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY=1` the cloud one, and `ANTHROPIC_BASE_URL` with a key the custom endpoint, unless another provider was put in use in settings. The log prints their names only. |
| `AWS_*`, `GOOGLE_*`, `CLOUD_ML_REGION`, proxies, `NODE_EXTRA_CA_CERTS` | the CLI | Left in the environment: the cloud credential chains the Bedrock and Vertex providers use. They do nothing without a `CLAUDE_CODE_USE_*` switch, which only the settings set. |
| `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` | `app.py` | When either is present (launched from inside Claude Code), `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SSE_PORT`, `CLAUDE_EFFORT`, `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_OAUTH_*`, `CLAUDE_CODE_SDK_*`, `CLAUDE_CODE_MESSAGING_*` are removed so the analyst's CLI does not attach to the parent session. |
| `CI`, `SSH_CONNECTION`, `SSH_TTY`, `DISPLAY`, `WAYLAND_DISPLAY` | `app.py` | Decide whether a window can open; without one the default mode is `--browser`. |

`GODSEYE_MCP_URL` and `GODSEYE_MCP_TOKEN` apply only to the legacy `launch.py` stack; the app host
passes the MCP URL and token to the bridge directly. `GODSEYE_LIVE_NET=1` is read only by the test
suite: it runs the tests marked `live_net` and lifts the egress guard for those tests alone.

The app flags these interact with (`python -m godseye_uav.app --help`): `--theater <id>` (default:
the theater persisted in the store, else `default`), `--geodata on|off` (default `on`),
`--real-data off|direct|gev` (default: `GODSEYE_REAL_DATA`, else off) and `--airframe
quad_suas_electric|group3_fixed_wing` (default: the restored theater's, else `GODSEYE_AIRFRAME`,
else `quad_suas_electric`). The banner reports the restore, a failed restore, and a `map data`
line.

## Where things are written

| What | Where |
|---|---|
| Store (tracks, missions, audit, fuel journal) | `--store`, else `~/Library/Application Support/EyeInTheSky/store` on macOS, `$XDG_DATA_HOME/eye-in-the-sky/store` (default `~/.local/share/…`) elsewhere, `%LOCALAPPDATA%\EyeInTheSky\store` on Windows. `start.sh` uses `godseye/.godseye/store`. |
| Harness config (MCP URL + token, mode 0600) | `<store>/../mcp.json`; usable as a Claude Code `--mcp-config` file. |
| Running theater | `<store>/theater.json` (atomic writes): the theater, airframe, epoch and who set it; read at boot. See [Runtime theaters and sim speed](#runtime-theaters-and-sim-speed). |
| Map-data cache | `<store>/geodata-cache/`: place lookups (30 days), mapped sites and open-ground searches (24 hours), as JSON files. Memory only for an in-memory store. |
| Analyst working directory | `<store>/analyst` |
| Analyst transcripts | The Claude CLI writes every conversation to `<config dir>/projects/<encoded cwd>/<session>.jsonl`, whatever the options say. The encoded cwd is the absolute path of `<store>/analyst` with every character other than a letter or digit replaced by `-`. On the **Claude login** the config dir is `~/.claude` (or a `CLAUDE_CONFIG_DIR` set at launch): for the desktop app's default store that is `~/.claude/projects/-Users-<you>-Library-Application-Support-EyeInTheSky-store-analyst/`. On **every other provider** it is `<store>/analyst/claude-home` (mode 0700), so those transcripts are under `<store>/analyst/claude-home/projects/`. These files hold the full conversation, tool arguments and results. A full check runs its CLI with `<store>/analyst/claude-check` as the config dir (the Claude login's check keeps `~/.claude`) and `<store>/analyst/check-cwd` as the cwd. |
| Analyst settings | `<store>/../llm-settings.json` (mode 0600, atomic writes): the provider in use and each provider's model, endpoint, fields, test record and acknowledgement. Never a key. |
| Analyst keys | The macOS Keychain (service `eye-in-the-sky.llm`), or `<store>/../llm-secrets.json` (mode 0600); see [Analyst providers and keys](#analyst-providers-and-keys). |
| Frozen app log | `<store>/../logs/eye-in-the-sky.log` (mode 0600, rotated at 5 MB), only when the `.app` is launched without a terminal. |
| Window storage | `<store>/../webview` is passed to pywebview as its storage path. For the packaged app, WebKit keeps page data (including `localStorage`) in `~/Library/WebKit/io.eyeinthesky.console` and `~/Library/Caches/io.eyeinthesky.console`. |

## Observed cost

Measured on this machine with `claude-opus-5` and the owner's Claude login (no API key):

- A turn that dry-ran an orbit and then requested it (denied): 3 model calls, 81,234 input tokens
  including cache, $0.317.
- An end-to-end run of four turns: $0.293 (a briefing), $0.294 (plan and launch an orbit), $0.230
  (point the camera, then scan) and $1.125 (plan and launch a second orbit), $1.94 in total. The
  three turns with a recorded token count each had 238,000–250,000 input tokens including cache, so
  the spread most likely comes from how much of that input was served from the prompt cache. Every
  usage event in that run reported the account's seven-day limit as `allowed_warning`.
- Tool definitions are roughly half of every model call: the 52 tools measured before
  `uav_handoff_target` was hidden came to 45,322 characters (about 11,000–14,000 tokens), and the
  system prompt adds about 3,000 tokens. WG v2 Phase A brought the analyst's list to 57 tools and
  lengthened the prompt; that cost has not been re-measured.
- The smallest turn, after the provider settings were added: "Reply with the single word OK." with
  effort `low` took 5.5 s, 24,921 input tokens including cache and 4 output tokens, $0.250.

Figures on an API key follow Anthropic's API pricing for the chosen model. Other providers bill you
directly at their own prices; the console shows no dollar figure for them
(`cost_basis:"unreliable"`), because the CLI would price their models from Anthropic's table.

## Data-honesty rules

The orb, search, inspector and analyst all read the same graph (`intel_graph.build_graph`, pure), so
these rules hold everywhere:

- **Duplicates are collapsed.** The track store persists across runs. Tracks with the same equipment
  name and OB class within 25 m (`DUPLICATE_RADIUS_M`) from different origin runs become one node,
  the freshest, with `attrs.duplicates` (up to 5 ids) and `attrs.duplicate_count`;
  `meta.duplicates_collapsed` counts them. Two tracks from the same run are never merged. Unit sizes
  and counts use the collapsed picture.
- **The default scope is the active theater.** `scope=theater` keeps contacts inside the active AO,
  or within 10 km of it (`AO_MARGIN_M`, flagged `outside_ao`), plus contacts with no position. The
  rest are counted in `meta.out_of_theater` (tracks) and `meta.out_of_theater_contacts`, and
  `scope=all` shows them. An unknown active theater is never guessed: nothing is scoped and a caveat
  says so. In the app host the in-process server's theater always wins over the bridge's (which can
  lag a switch by one poll); without one, the bridge's is used. The table's default is never
  assumed.
- **Mapped sites are context, not intelligence.** A site node says "Mapped, not verified", carries
  no action, control or damage field, and no edge points at it. Its presence is not a finding and
  its absence is not a negative one: the caveats say "Sites are mapped OpenStreetMap data (ODbL),
  not an order of battle." and, when the fetch degraded, "Sites may be missing, not absent." Site
  names and tags are untrusted text: bidi- and control-stripped on the server and rendered as text
  only in the browser (never as HTML or a URL).
- **No assessment is not "none".** A contact without a threat level has `status:"unknown"` and
  `attrs.threat:"not assessed"`; `none` appears only when the model said so.
- **No invented timestamps.** A mission node has `ts_ms` only from its recorded start
  (`uav://mission/{id}.started`), otherwise `null`; reports without `as_of` get `null`.
- **Staleness.** A contact not re-fixed for 900 s (`CUSTODY_LAPSE_S`) is "custody lapsed": its
  position is the last known one and its salience drops. A vehicle whose telemetry is more than 5 s
  old is `critical`. A link that is down but not yet declared lost (`pending`) is `warn`, with
  `link_lost_since_ms`. In the UI, a failed poll after a good one turns the picture `stale`: the orb
  desaturates and the rail says "Last picture HH:MM:SSZ, not live." and "Sim host not responding";
  no graph at all is `offline`, and a 401 is `unauthorized`. The console polls every 2 s while
  visible, backs off to 5 s on error, and times a request out after 8 s.
- **Caps are counted, not hidden.** At most 400 track nodes (the least salient are counted in
  `meta.tracks_omitted`), the newest 50 alarm nodes, 100 edges per report or mission, 80 related
  entities per inspector read, 14 caveats. Entity reads are cut to 60,000 bytes and the overview to
  6,000 with `_truncated` markers saying what was removed.
- **Caveats travel with the data** (`meta.caveats`): the real-data layer being off (AGL is height
  above the launch datum, LOS is geometric, no live air traffic), unknown theater, feeds down ("an
  absent entity there is not a negative finding"), stale custody, the scope and dedupe arithmetic,
  and that threat levels are model outputs for sensor posture only.
- Display text from the server is stripped of bidi control characters (U+202A–202E, U+2066–2069) in
  the graph, in the approval text and again in the browser.

## Known limits

- **GEV's node-only providers are not in the app host.** GEV's live layers (aircraft, vessels, CCTV,
  terrain, Overpass and others under `gods-eye-view/server/providers/`) run inside its vite dev
  server. The app host answers `/api/*` with 404 `not_available_in_app_host`, so those layers are
  empty there. Run the vite dev server (`godseye/start.sh`) to use them.
- **The real-data layer is off by default.** With it off, AGL is height above the launch datum, LOS
  is geometric and there is no live air traffic; the graph says so. `--real-data direct` turns on
  terrain and weather without GEV (no mapped installations, no live traffic). `--real-data gev`
  (`GODSEYE_REAL_DATA=1`) needs GEV's `/api` providers at `GODSEYE_GEV_ORIGIN`, which the app host
  does not serve.
- **Map data leaves the machine.** With `--geodata on` (the app default), the text of a place
  search goes to Photon (and Nominatim on a fallback), a theater's box to Overpass, and single
  points to Re:Earth and Open-Meteo. Coordinates and theater-table names are answered locally.
  `--geodata off` sends none of it.
- **Runtime theaters need the fake simulator and the app host.** Under a real AirSim the origin is
  fixed by `settings.json`: switches and sim speed are refused, and a persisted chat theater is not
  restored. The legacy `launch.py` stack refuses switches ("needs the app host").
- **A large AO outruns the certified geo-registration.** Positions are certified to about 900 m
  from the origin (godseye `README.md`, "Known limits"); a group-3 AO reaches 25 km, so positions
  near its edge carry the growing horizontal error. The group-3 profile changes the fuel model
  only: the fake still flies multirotor kinematics.
- **Sim speed** is not persisted, and at any speed link-loss timers, detections, scans and the
  analyst's clock stay on wall time; camera captures leave gaps above ×3.
- **The console's theater checks read the graph**, which is polled every 2 s. The server re-checks
  everything at the switch, so a slip the console showed as ready can still be refused.
- **Map imagery keys** (`GOOGLE_MAPS_API_KEY`, `CESIUM_ION_TOKEN`) are read when the UI is built;
  `scripts/build_desktop.sh` leaves them out unless `--bake-keys` is passed.
- **Fonts load from Google Fonts** (Atkinson Hyperlegible Next and Mono for the console, plus GEV's
  own faces and Material Symbols); offline, the browser falls back to other fonts.
- **Chat sessions live in memory.** A host restart loses sessions and grants; the transcripts stay
  under `~/.claude/projects/` (Claude login) or `<store>/analyst/claude-home/projects/` (every other
  provider).
- **Providers other than the Claude login were tested offline only.** No third-party provider, and
  no Anthropic API key, was called while building the settings: the checks ran the real bundled CLI
  against a local stub behind a deny-all proxy, with fake keys. Provider facts that could not be
  confirmed are marked "Unverified:" in the catalog and in the sheet. The analyst is built and
  tested with Claude; other models may misuse tools or the doctrine (the approval slips still
  apply).
- **A redirect that starts mid-session.** The redirect probe runs at the full check and before each
  CLI start. A CLI that is already running would follow a redirect its endpoint starts answering
  later, and the CLI re-sends an `x-api-key` header to the new host (it drops `Authorization`).
- **Streamed text can lag by up to 11 characters** while a provider key is known: the redactor holds
  that much back to catch a key split across deltas.
- **The settings sheet needs the app host's own page.** From vite's dev server it is cross-origin
  and shows a message instead. Whether ⌘, reaches the page inside the desktop window has not been
  checked; "Analyst settings…" in the analyst's menu works everywhere.
- **A dev run with a store outside a temporary directory uses the macOS Keychain.** Set
  `GODSEYE_LLM_SECRET_STORE=file` for dev and test runs.
- The CLI sends `role:"system"` messages inside the message list; a provider that rejects them
  fails the full check. Vertex models without a global endpoint need a regional override the sheet
  doesn't offer yet.
- `provider_changed` is sent only once a session has run a turn: a change before the first message
  shows no divider.
- `tool_result.busy_with` is covered by unit tests, not yet seen live.
- A pointer approval takes effect 500 ms after the click, so scripts that click Approve must wait for
  it.
- GEV's voice input needs its Node dev server, so it does not work in the app host or the packaged
  app.
- "Ask about this" still shows in the inspector when the analyst is off.
- After a BINGO that fires once a mission has completed, the mission row reads "Complete 100% …
  incomplete - fuel" (`MISSION_INCOMPLETE_FUEL` in `safety.py`).
- Once a thread that built a PROJ transformer (`geo._grid_undulation`) has exited, a subprocess
  started by fork (which includes the SDK's asyncio launch of the CLI) can crash in the child with
  SIGSEGV. No current code path builds a transformer on a short-lived thread, and the analyst works
  today; the fix belongs in `geo.py`.
- The desktop app has its own limits (ad-hoc signature, arm64, macOS version, no bundled licence
  files); see "Desktop app (macOS)" in the repository `README.md` and `THIRD_PARTY_NOTICES.md`.
