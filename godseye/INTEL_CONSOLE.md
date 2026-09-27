# Intelligence console and in-app analyst

This is the reference for the Eye in the Sky app: the single-process host, its HTTP and SSE contract,
the analyst's approval policy, and the data-honesty rules the console follows. Everything here was
checked against the code: `mcp/godseye_uav/{host,app,chat,analyst_policy,analyst_toolbelt,intel_graph}.py`
and `gods-eye-view/src/console/`. Where this file and the code disagree, the code wins.

**ISR-only.** The analyst observes, classifies and reports. Nothing it can call is kinetic, and threat
output is sensor-posture advice only. Every action that moves an aircraft, tasks a sensor or changes
the sim waits for the operator.

## Contents

- [What the operator sees](#what-the-operator-sees)
- [Architecture: one process, one origin](#architecture-one-process-one-origin)
- [HTTP contract](#http-contract)
- [Chat stream (SSE)](#chat-stream-sse)
- [Approval classes](#approval-classes)
- [Session grants](#session-grants)
- [Order-slip rules](#order-slip-rules)
- [Tracking handoff](#tracking-handoff)
- [The analyst's toolbelt](#the-analysts-toolbelt)
- [Analyst sign-in and Anthropic's policy](#analyst-sign-in-and-anthropics-policy)
- [Environment variables](#environment-variables)
- [Where things are written](#where-things-are-written)
- [Observed cost](#observed-cost)
- [Data-honesty rules](#data-honesty-rules)
- [Known limits](#known-limits)

## What the operator sees

The app opens on an intelligence console, not a map:

- an **orb**: a 3D sphere of every intel entity (vehicles, missions, contacts, units, equipment
  classes, reports, theater and POIs, alarms, feeds), drawn with Canvas2D;
- a **search and ask bar** (⌘K, Ctrl+K or `/`) that ranks graph nodes and ends with an
  "Ask the analyst" row;
- the **analyst chat**, which answers from the data and runs missions through the godseye tools;
- a **situation rail**: theater, sim status, fleet fuel against BINGO, running missions, alarms and
  data caveats;
- an **entity inspector** with measured and assumed values marked, related entities and actions
  (Ask about this, Focus, Track, Abort).

The Cesium map from God's Eye View (GEV) appears only in **tracking mode**, following one drone in the
cockpit view. `?console=off` on the page URL skips the console and loads the plain GEV map
application, first-run launcher included (`gods-eye-view/src/main.js`).

## Architecture: one process, one origin

```
 python -m godseye_uav.app            (./eye-in-the-sky, godseye-app, the .app, start.sh --headless)
 ┌───────────────────────────────── one FastAPI app, one asyncio loop ─────────────────────────────┐
 │ 127.0.0.1:8780  (start.sh: 127.0.0.1:8790, and the same app again on :8791)                      │
 │                                                                                                   │
 │  /                 built console UI (gods-eye-view/dist), window.__GODSEYE__ injected            │
 │  /app/config       /intel/*   /chat/*          host routers, added outside create_app            │
 │  /mcp              GodseyeUavServer, Streamable HTTP + bearer       <── external MCP harnesses   │
 │  /health /snapshot /mission-overlay /theaters /events /tracks /control/* /camera/*   bridge      │
 │  /api/*            404 {"error":"not_available_in_app_host"}                                      │
 │                                                                                                   │
 │  IntelService ── reads app.state.godseye (bridge caches) + the server object, never over HTTP    │
 │  ChatService  ── one actor task per chat session ── ClaudeSDKClient ──┐                          │
 │                  in-process SDK MCP server "godseye" (the toolbelt)   │ stdin/stdout              │
 │                  └─ server.mcp.call_tool(...) on the same loop        │                          │
 │  bridge mission feed ── polls /mcp over loopback HTTP (same port)     │                          │
 └────────────┬──────────────────────────────────────────────────────────┼──────────────────────────┘
              │ msgpack-rpc, loopback only                                ▼
      FakeAirSim (in-process threads)                        claude CLI child process ──► Anthropic API
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
- If `intel_graph` or `chat` fails to load or start, the host still boots and answers that module's
  routes with 503 (see below).

## HTTP contract

Auth is `Authorization: Bearer <token>` unless noted. SSE routes also accept `?token=` because
`EventSource` cannot set headers. Error bodies are top-level `{"error": ..., ...}`. A request body
that fails validation gets FastAPI's standard 422 (`{"detail": [...]}`).

| Route | Auth | Response |
|---|---|---|
| `GET /` and `GET /index.html` | none | The built UI with `<script>window.__GODSEYE__={"bridgeUrl":"","token":"…"}</script>` inserted before the first module script. Sent with `no-store`, `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'` and `Cross-Origin-Resource-Policy: same-origin`; CORS headers are stripped. If the UI is not built, a page that says how to build it. |
| `GET /app/config` | none | `{app:"eye-in-the-sky", version, theater:{id,label}, chat:{available, model, reason?}, mcp_path:"/mcp", ui:"built"\|"missing"}`. Never the token. |
| `GET /intel/graph?scope=theater\|all` | bearer | The intel graph (below). Bad scope: 422 `{error:"invalid_scope", scope, allowed}`. |
| `GET /intel/entity/{id}` | bearer | `{id, type, label, subtitle, status, requested_id?, fields, provenance, related:[{id,type,label,kind,dir}], related_omitted?, caveats, raw?}`, at most 60,000 bytes. Accepts a collapsed duplicate's id or a bare id. Unknown: 404 `{error:"unknown_entity", id}`. |
| `GET /intel/events/recent?limit=50` | bearer | `{events:[alarm payload + seq]}`, limit clamped to 1–100. |
| `GET /chat/status` | bearer | `{available, model, reason?:"disabled"\|"sdk_missing"\|"cli_missing", hint?, effort?}` |
| `POST /chat/sessions` | bearer | `{session_id}`. The CLI is not started until the first message. With 8 sessions open, the least recently active idle one is closed; if none is idle, 409 `{error:"busy", message}`. |
| `POST /chat/sessions/{sid}/messages` | bearer | Body `{text (1–8000 chars), context?:{focused_ids:[graph id]}}` → 202 `{turn_id}`. 404 `{error:"unknown_session", session_id}`; 409 `{error:"busy", turn_id}` while a turn runs; 503 `{error:"unavailable", reason, hint?}`; 422 `{error:"invalid", message}`. Up to 20 valid `focused_ids` are prefixed to the prompt as `[[id]]` references. |
| `GET /chat/sessions/{sid}/stream` | bearer or `?token=` | `text/event-stream` (next section). Resume with the `Last-Event-ID` header or `?last_event_id=`. 404 for an unknown session. |
| `POST /chat/sessions/{sid}/approvals/{approval_id}` | bearer | Body `{decision:"approve"\|"deny"\|"approve_session", note? (≤ 2000)}` → `{ok:true}`. 404 `{error:"unknown_approval", approval_id}` (unknown, expired or already decided); 422 `{error:"not_allowed", message}` for `approve_session` on a class that cannot be granted. |
| `GET /chat/sessions/{sid}/grants` | bearer | `{grants:[{tool, since_ms}]}`. 404 `{error:"unknown_session", session_id}`. |
| `DELETE /chat/sessions/{sid}/grants/{tool}` | bearer | `{ok:true}`; idempotent for a known session, 404 `unknown_session` otherwise. |
| `POST /chat/sessions/{sid}/interrupt` | bearer | `{ok:true}`. Pending approvals resolve as `cancelled`. 404 for an unknown session. |
| `DELETE /chat/sessions/{sid}` | bearer | `{ok:true}`. Stops the session's CLI. 404 for an unknown session. |
| `/mcp` | bearer | The godseye MCP server (46 tools, 8 resources; see `TOOL_CONTRACT.md`). A 401 names `http://127.0.0.1:<port>/.well-known/oauth-protected-resource` as its resource metadata; that address is not served (404). |
| `/api/{path}` | none | 404 `{error:"not_available_in_app_host"}`: GEV's node-only providers exist only under its vite dev server. |

When a module could not be loaded: `/intel/*` answers 503 `{error:"intel_unavailable"}`;
`GET /chat/status` answers `{available:false, reason:"sdk_missing"|"error", hint, model}` and the
other `/chat/*` routes 503 `{error:"chat_unavailable"}`. The bridge routes are unchanged; see
`BRIDGE_CONTRACT.md`. The chat never uses `/control/*`.

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
           "caveats": ["…"], "feeds": {"<name>": {"ok": true, "status": "ok", "error": "…", "at_ms": 0}}}
}
```

When the active theater is unknown, `theater` is `{id:null, label:null, place:null, known:false, reason}`.

| Node type | Id | Notes |
|---|---|---|
| vehicle | `veh:{name}` | `attrs`: `fuel_pct`, `bingo_fuel_pct`, `margin_pct`, `eta_to_bingo_s`, `bingo_latched`, `landed`, `agl_m`, `agl_is_real`, `link`, `link_lost_since_ms`, `mission`, `track_id`, `stale_ms`, `datum_degraded`, `lost_link {behaviour, declare_after_s, escalate_to_rtb_after_s, climb_to_m, source}` |
| mission | `msn:{mission_id}` | `attrs`: `kind`, `phase`, `vehicle`, `progress_pct`, `eta_s`, `waypoint {index?, of?}`, `coverage_pct`, `geofence`, `incomplete_reason`, `ts_basis` |
| track (contact) | `trk:{track_id}` | `attrs`: `category`, `ob_class`, `confidence`, `threat`, `sightings`, `age_s`, `stale`, `duplicates`, `duplicate_count`, `unit`, `theater`, `outside_ao`, `out_of_theater`, `unlocated` |
| unit | `unit:{category}:{min member id}` | same-category contacts within the element radius, after duplicate collapse |
| equipment | `ob:{ob_class}` | the order-of-battle class |
| report | `rpt:{report_id}` | INTREP or THREATREP |
| theater | `thr:{theater_id}` | only the active theater in `theater` scope |
| POI | `poi:{theater_id}:{name}` | |
| alarm | `alarm:{seq}` | the newest 50 |
| feed | `feed:{name}` | bridge feeds plus `sim`, `real_data` and `theater` |

Edge kinds: `flying` (vehicle → mission, only while the phase is planning, executing or rtb),
`tracking` (vehicle → track), `operating_in` (vehicle → theater), `target` and `observes`
(mission → track), `member_of` (track → unit), `is_a` (track → equipment), `in_theater`
(track or POI → theater), `near` (track → POI within 250 m), `reports_on` (report → track), `about`
(alarm → vehicle, track or mission).

Vehicle status: `critical` for a latched BINGO, fuel at or below BINGO, a declared lost link
(`loal`), a telemetry error or telemetry older than 5 s; `warn` for a margin under 10 points, a
degraded or `pending` link, or a degraded datum; `unknown` without telemetry.

## Chat stream (SSE)

The stream starts with `retry: 3000`. Each event has `id: <seq>` (except `session`), `event: <name>`
and `data: <json>`. A comment heartbeat (`: ping <unix seconds>`) is sent every 15 s. The last 500
events per session are kept for replay.

| Event | Data |
|---|---|
| `session` | `{session_id, model, available, last_seq, history_truncated}`; always first, with no `id:` line |
| `turn_start` | `{turn_id, text}` |
| `text_delta` | `{turn_id, text}` |
| `thinking` | `{turn_id, text}` (summarized thinking) |
| `tool_call` | `{turn_id, call_id, tool, title, class, args, summary}`; `tool` is the bare name, `args` are shrunk for display |
| `approval_request` | `{approval_id, call_id, tool, class, title, summary, args, consequences:[str], allow_session, expires_at_ms, vehicle, dry_runnable, grant_scope, dry_run?}` |
| `approval_resolved` | `{approval_id, call_id, decision:"approved"\|"denied"\|"expired"\|"cancelled", tool, scope:"once"\|"session", note?}` |
| `tool_result` | `{call_id, ok, outcome:"ok"\|"rejected"\|"error"\|"busy"\|"not_run", rejected?, error?, busy_with?:{task_id?, mission_id?, tool?}, summary, bytes, truncated, entities:[graph id]}` |
| `ui` | `{action:"focus", ids, note?}`, `{action:"track", vehicle, reason}`, `{action:"orb"}` or `{action:"inspect", id}` |
| `usage` | `{turn_id, cost_usd?, session_cost_usd?, input_tokens?, output_tokens?, rate_limit?:{status, resets_at, type}}` |
| `turn_end` | `{turn_id, stop:"end"\|"interrupted"\|"error"\|"max_turns", error?}` |
| `error` | `{message, hint?, retryable}` |

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
- The stop reason is `max_turns` after 40 model turns in one message.
- Entity references in assistant text use `[[type:id|label]]` or `[[type:id]]` with the ten graph
  prefixes (`veh msn trk unit ob rpt thr poi alarm feed`). The console renders them as chips.

## Approval classes

`analyst_policy.classify(tool, args)` is pure and fails closed: a tool it does not know is a
`command` with no session grant, and an exception inside it also returns `command`.

| Class | Asks the operator | Session grant | Tools |
|---|---|---|---|
| `read` | no | – | `uav_get_telemetry`, `uav_list_vehicles`, `uav_task_status`, `mission_status`, `uav_los_check`, `uav_target_report`, `uav_identify_target`, `uav_assess_threat`, `uav_list_ob_classes`, `uav_real_data_status`, `uav_deconflict_airspace`, `uav_list_tracks`; the curated `intel_overview`, `intel_search`, `intel_entity`, `read_intel_resource`, `ui_focus`, `ui_track`, `ui_show_orb`, `ui_inspect` |
| `plan` | no | – | `mission_dry_run`, and `dry_run: true` on a tool that honours it: `uav_mission`, `uav_orbit_poi`, `mission_grid_search`, `mission_recon_route`, `mission_track_target`, `mission_identify_target`, `mission_threat_assessment`, `mission_handoff_track` |
| `sensor` | yes | per tool | `uav_get_detections`, `uav_scan_targets`, `uav_capture_image`, `uav_set_gimbal`, `uav_set_fov` |
| `command` | yes, every call | no | `uav_takeoff`, `uav_land`, `uav_return_to_home`, `uav_goto_gps`, `uav_fly_route`, `uav_hover`, `uav_orbit_poi`, `uav_mission`, `mission_grid_search`, `mission_recon_route`, `mission_track_target`, `mission_identify_target`, `mission_threat_assessment`, `mission_handoff_track`, `uav_handoff_target`, `mission_cancel`, `uav_abort`; any unknown tool |
| `sim` | yes, every call | no | `sim_set_time`, `sim_set_weather`, `sim_spawn_target`, `sim_move_target`, `sim_set_gps_degradation`, `sim_hydrate_real_data`, `sim_spawn_order_of_battle`, `sim_set_environment` |
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
  - `sim_set_weather`, `sim_set_time` or `sim_set_fuel` was approved;
  - another command for the same vehicle was approved.

  Validation runs on every render, on every graph change, and again at the press; a press on a slip
  that just went stale is swallowed and the slip re-arms. A dry run from before this page load is
  shown as "can't compare", without demoting Approve.
- **Sensor slips** show a checkbox above the buttons, "Allow <tool> for this session" (for example
  "Allow Scan for targets for this session"), captioned that flying and simulation changes still ask
  every time. Checking it re-arms the slip and turns Approve into "Approve and allow for session".
- **Override slips** need an acknowledgement checkbox before Approve can be pressed, for example "I
  understand this clears Drone1's BINGO latch." for `sim_set_fuel`.
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
  orb; `exit()` leaves the cockpit without re-tracking.
- While the map is hidden, the port swallows GEV's bare-letter shortcuts and the cockpit `c` key,
  and the map is not rendered.
- **Operator Abort** is not the analyst: the rail, inspector and tracking dock call the bridge's
  `POST /control/command {tool:"uav_abort", vehicle}` after a one-step confirm.

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
  `ui_track`, `ui_show_orb`, `ui_inspect`. That is 51 tools in all (43 proxied, 8 curated).
- Results are compact JSON capped at 24,000 characters (`intel_entity` at 20,000, `intel_overview`
  at 6,000 bytes from the service). Tools that take `detail` or `top_n` get `detail="summary"` and
  `top_n=10` when the model leaves them unset. Anything cut is marked: lists end in
  `{"_truncated":true,"_omitted":N,"_note":…}` and the result carries a top-level `_truncated`.
- The CLI runs with no built-in tools (`tools=[]`), no settings files (`setting_sources=[]`), only
  this MCP server (`strict_mcp_config`), `verbatim_prompts=True` (so `@path` and `/command` in chat
  text are plain text), permission mode `default`, adaptive thinking with summarized display, and
  `cwd=<store>/analyst`.
- The system prompt is `mcp/godseye_uav/analyst_prompt.md` (package data), distilled from the
  `godseye-uav` skill: ISR-only identity, task → plan → dry-run → execute → monitor → report, server
  gates win, measured versus assumed, SALUTE and INTREP, and the console's approval and chip rules.
- A session's CLI (about 190 MB resident) is disconnected after 10 minutes without a turn; the next
  message reconnects and resumes the conversation. At most 8 sessions are kept.

## Analyst sign-in and Anthropic's policy

The analyst needs the `app` extra (`pip install -e './godseye[app]'`), which installs
`claude-agent-sdk`. It runs the Claude Code CLI that the SDK bundles (0.2.160 bundles CLI 2.1.283),
or `Contents/Helpers/claude` inside the desktop app. Without the SDK, `/chat/status` says
`sdk_missing`; with `--no-chat`, `disabled`; without a CLI, `cli_missing`. The app does not pass
credentials to the CLI; the CLI inherits the app's environment after `app.sanitize_env`:

- **Your own Claude login, on your own machine.** Sign in once with `claude` then `/login` (Claude
  Code on your PATH, or the SDK's bundled binary at `claude_agent_sdk/_bundled/claude`); on macOS the
  CLI stores the login in the keychain. This is how the analyst was run and tested here, with
  no API key. It uses your plan's usage limits, and the console shows the limit warnings the CLI
  reports.
- **`ANTHROPIC_API_KEY`.** If it is set in the app's environment, the CLI uses it; Anthropic's
  authentication docs rank an API key above the `/login` subscription.
- **Anything you give to other people must use API-key authentication.** Anthropic's Agent SDK
  documentation says that, unless previously approved, third-party developers may not offer
  claude.ai login or its rate limits for products built on the SDK, and should use API-key
  authentication instead. A build distributed to anyone else must have its user supply their own
  `ANTHROPIC_API_KEY` (or a cloud provider the CLI supports), not a Claude login. The SDK's own
  package metadata also states that its use is governed by Anthropic's Commercial Terms of Service
  (see `THIRD_PARTY_NOTICES.md`).
- There is no in-app key entry today. A key comes from the environment only. A Finder launch of the
  `.app` does not see variables from your shell profile; to pass one, start the binary from a terminal
  (`"dist/Eye in the Sky.app/Contents/MacOS/EyeInTheSky"`).
- `ANTHROPIC_BASE_URL` is kept unless the app was launched from inside a Claude Code session. Do not
  set it without a key for that endpoint: this project's provider research observed the CLI sending
  the Claude login's token to a base URL that had no key configured.
- Sign-in failures surface as an `error` event with the hint "Sign in with the claude CLI (`claude`
  then /login) or set ANTHROPIC_API_KEY".

## Environment variables

| Variable | Read by | Meaning |
|---|---|---|
| `GODSEYE_TOKEN` | `app.py` | API and MCP bearer token. `--token` wins; otherwise a random token per launch. Removed from the process environment before anything is spawned, and never printed. `start.sh` passes its `TOKEN` this way. |
| `GODSEYE_CHAT_MODEL` | `chat.py` | Analyst model. `--model` wins; default `claude-opus-5`. |
| `GODSEYE_CHAT_EFFORT` | `chat.py` | `low`, `medium`, `high`, `xhigh` or `max`. `--effort` wins; an unknown value is ignored with a warning; unset means the model's default. |
| `GODSEYE_UI_DIR` | `host.py` | Built UI directory. `--ui-dir` wins; default `gods-eye-view/dist`, or `ui/` inside the frozen app. |
| `GODSEYE_AIRSIM_PYTHONCLIENT` | `app.py`, scripts | Location of the AirSim PythonClient; otherwise `../airsim/PythonClient`, then `godseye/.godseye/vendor/airsim/PythonClient`. |
| `GODSEYE_BRIDGE_CORS_ORIGINS` | `bridge.py` | Comma- or space-separated CORS origins, or `*`. Default: ports 4173, 5173 and 5199 on localhost and 127.0.0.1. The token pages never get CORS headers. |
| `GODSEYE_REAL_DATA` | `server.py` | Real-data layer switch (`1/true/yes/on/enable(d)` or `0/false/no/off/disable(d)`); unset is off. Any other value is an error at startup. |
| `GODSEYE_GEV_ORIGIN` | `realdata.py` | Where the real-data layer finds GEV's `/api` providers; default `http://localhost:5199`. GEV's vite dev server listens on 4173 by default (and `start.sh` uses `UI_PORT`, 4173), so set `GODSEYE_GEV_ORIGIN=http://localhost:4173` to use it. |
| `GODSEYE_AIRFRAME` | `safety.py` | Fuel-model airframe profile. |
| `ANTHROPIC_API_KEY` | the CLI | Used by the analyst when set (above). |
| `CLAUDE_CODE_USE_BEDROCK`, `…_VERTEX`, `…_FOUNDRY`, `AWS_*`, `GOOGLE_*` | the CLI | Kept by `sanitize_env` for provider selection; not exercised by this project's tests. |
| `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` | `app.py` | When either is present (launched from inside Claude Code), `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SSE_PORT`, `CLAUDE_EFFORT`, `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_OAUTH_*`, `CLAUDE_CODE_SDK_*`, `CLAUDE_CODE_MESSAGING_*` are removed so the analyst's CLI does not attach to the parent session. |
| `CI`, `SSH_CONNECTION`, `SSH_TTY`, `DISPLAY`, `WAYLAND_DISPLAY` | `app.py` | Decide whether a window can open; without one the default mode is `--browser`. |

`GODSEYE_MCP_URL` and `GODSEYE_MCP_TOKEN` apply only to the legacy `launch.py` stack; the app host
passes the MCP URL and token to the bridge directly.

## Where things are written

| What | Where |
|---|---|
| Store (tracks, missions, audit, fuel journal) | `--store`, else `~/Library/Application Support/EyeInTheSky/store` on macOS, `$XDG_DATA_HOME/eye-in-the-sky/store` (default `~/.local/share/…`) elsewhere, `%LOCALAPPDATA%\EyeInTheSky\store` on Windows. `start.sh` uses `godseye/.godseye/store`. |
| Harness config (MCP URL + token, mode 0600) | `<store>/../mcp.json`; usable as a Claude Code `--mcp-config` file. |
| Analyst working directory | `<store>/analyst` |
| Analyst transcripts | The Claude CLI writes every conversation to `~/.claude/projects/<encoded cwd>/<session>.jsonl`, whatever the options say. The encoded cwd is the absolute path of `<store>/analyst` with every character other than a letter or digit replaced by `-`; for the desktop app's default store that is `~/.claude/projects/-Users-<you>-Library-Application-Support-EyeInTheSky-store-analyst/`. These files hold the full conversation, tool arguments and results. |
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
  system prompt adds about 3,000 tokens.

Figures on an API key follow Anthropic's API pricing for the chosen model.

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
  says so. When the bridge has not yet learned the theater, the in-process server is asked; the
  table's default is never used.
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
  is geometric and there is no live air traffic; the graph says so. Turning it on
  (`GODSEYE_REAL_DATA=1`) needs GEV's `/api` providers at `GODSEYE_GEV_ORIGIN`, which the app host
  does not serve.
- **Map imagery keys** (`GOOGLE_MAPS_API_KEY`, `CESIUM_ION_TOKEN`) are read when the UI is built;
  `scripts/build_desktop.sh` leaves them out unless `--bake-keys` is passed.
- **Fonts load from Google Fonts** (Atkinson Hyperlegible Next and Mono for the console, plus GEV's
  own faces and Material Symbols); offline, the browser falls back to other fonts.
- **Chat sessions live in memory.** A host restart loses sessions and grants; the transcripts stay
  under `~/.claude/projects/`.
- **No in-app provider or key settings.** A bring-your-own-key settings design has been written but
  not built; keys come from the environment only.
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
