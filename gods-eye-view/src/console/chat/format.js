/**
 * Pure helpers shared by the analyst chat modules: Zulu time, durations,
 * server-string segments, class metadata, tool vocabulary and approve verbs.
 *
 * No DOM, no globals. Every string here that reaches the screen is sentence
 * case; glyph names reach the DOM only through the frozen ICON map.
 */

/** Material Symbols glyph names used by the chat (UX spec §11.5). */
export const ICON = Object.freeze({
  forum: 'forum',
  more: 'more_horiz',
  send: 'send',
  stop: 'stop',
  check: 'check',
  block: 'block',
  schedule: 'schedule',
  read: 'manage_search',
  plan: 'route',
  sensor: 'sensors',
  command: 'flight_takeoff',
  sim: 'tune',
  override: 'gpp_maybe',
  expand: 'expand_more',
  refresh: 'refresh',
  login: 'login',
  abort: 'pan_tool',
  track: 'my_location',
  close: 'close',
  warning: 'warning',
  error: 'error',
  info: 'info',
  offline: 'cloud_off',
});

/** Approval classes (contract §5.2) with their phrase and glyph (spec §2.3). */
export const CLASS_META = Object.freeze({
  read: Object.freeze({ phrase: 'Reads data', icon: ICON.read, slip: false }),
  plan: Object.freeze({
    phrase: 'Plans only, nothing flies',
    icon: ICON.plan,
    slip: false,
  }),
  sensor: Object.freeze({
    phrase: 'Tasks a sensor',
    icon: ICON.sensor,
    slip: true,
  }),
  command: Object.freeze({
    phrase: 'Commands an aircraft',
    icon: ICON.command,
    slip: true,
  }),
  sim: Object.freeze({
    phrase: 'Changes the simulation',
    icon: ICON.sim,
    slip: true,
  }),
  safety_override: Object.freeze({
    phrase: 'Safety override',
    icon: ICON.override,
    slip: true,
  }),
});

/** Unknown classes fail closed to `command`, like the server policy. */
export function classKey(klass) {
  return Object.hasOwn(CLASS_META, klass) ? klass : 'command';
}

export function classMeta(klass) {
  return CLASS_META[classKey(klass)];
}

/** Human titles per bare tool name (mirrors analyst_policy._TITLES). */
export const TOOL_TITLES = Object.freeze({
  uav_get_telemetry: 'Read telemetry',
  uav_list_vehicles: 'List vehicles',
  uav_task_status: 'Check task status',
  mission_status: 'Check mission status',
  uav_los_check: 'Check line of sight',
  uav_target_report: 'Build intelligence report',
  uav_identify_target: 'Read contact report',
  uav_assess_threat: 'Assess threat',
  uav_list_ob_classes: 'List equipment classes',
  uav_real_data_status: 'Check real-data status',
  uav_deconflict_airspace: 'Check airspace',
  intel_overview: 'Read situation overview',
  intel_search: 'Search intel',
  intel_entity: 'Read entity',
  read_intel_resource: 'Read server resource',
  ui_focus: 'Highlight in orb',
  ui_track: 'Show drone on map',
  ui_show_orb: 'Return to orb',
  ui_inspect: 'Open inspector',
  mission_dry_run: 'Dry run',
  uav_get_detections: 'Collect detections',
  uav_scan_targets: 'Scan for targets',
  uav_capture_image: 'Capture image',
  uav_set_gimbal: 'Point camera',
  uav_set_fov: 'Set field of view',
  uav_takeoff: 'Take off',
  uav_land: 'Land',
  uav_return_to_home: 'Return to home',
  uav_goto_gps: 'Fly to point',
  uav_fly_route: 'Fly route',
  uav_hover: 'Hover',
  uav_orbit_poi: 'Orbit point',
  uav_mission: 'Mission',
  mission_grid_search: 'Grid search',
  mission_recon_route: 'Route recon',
  mission_track_target: 'Track contact',
  mission_identify_target: 'Identify contact',
  mission_threat_assessment: 'Threat assessment',
  mission_handoff_track: 'Hand off track',
  uav_handoff_target: 'Hand off track',
  mission_cancel: 'Cancel mission',
  uav_abort: 'Abort',
  sim_set_time: 'Set sim time',
  sim_set_weather: 'Set sim weather',
  sim_spawn_target: 'Spawn sim target',
  sim_move_target: 'Move sim target',
  sim_set_gps_degradation: 'Degrade GPS',
  sim_hydrate_real_data: 'Load real-world data',
  sim_spawn_order_of_battle: 'Spawn order of battle',
  sim_set_fuel: 'Refuel',
  sim_set_link_state: 'Set link state',
  sim_reset: 'Reset simulation',
});

export function toolTitle(tool) {
  return (Object.hasOwn(TOOL_TITLES, tool) && TOOL_TITLES[tool]) || tool || '';
}

/** The five sensor tools a v1 class-wide sensor grant covers (spec §6.6). */
export const SENSOR_TOOLS = Object.freeze([
  'uav_get_detections',
  'uav_scan_targets',
  'uav_capture_image',
  'uav_set_gimbal',
  'uav_set_fov',
]);

/**
 * Tools whose server implementation honours `dry_run` (analyst_policy
 * DRY_RUN_TOOLS). Used until `approval_request.dry_runnable` exists.
 */
export const DRY_RUNNABLE_TOOLS = Object.freeze([
  'uav_mission',
  'uav_orbit_poi',
  'mission_grid_search',
  'mission_recon_route',
  'mission_track_target',
  'mission_identify_target',
  'mission_threat_assessment',
  'mission_handoff_track',
]);

/** Sim changes after which an earlier dry run is out of date (spec §6.5). */
export const STALING_TOOLS = Object.freeze([
  'sim_set_weather',
  'sim_set_time',
  'sim_set_fuel',
]);

const MISSION_KIND = Object.freeze({
  grid_search: 'grid_search',
  recon_route: 'recon_route',
  track_target: 'track_target',
  track: 'track_target',
  identify: 'identify_target',
  identify_target: 'identify_target',
  orbit_poi: 'orbit_poi',
  threat_assessment: 'threat_assessment',
  assess: 'assess',
});

const TOOL_KIND = Object.freeze({
  mission_grid_search: 'grid_search',
  mission_recon_route: 'recon_route',
  mission_track_target: 'track_target',
  mission_identify_target: 'identify_target',
  mission_threat_assessment: 'threat_assessment',
  uav_orbit_poi: 'orbit_poi',
  mission_handoff_track: 'handoff',
  uav_handoff_target: 'handoff',
});

/** Mission kinds as words used mid-sentence ("a grid search"). */
export const KIND_LABEL = Object.freeze({
  grid_search: 'grid search',
  recon_route: 'route recon',
  track_target: 'contact track',
  identify_target: 'contact identification',
  orbit_poi: 'orbit',
  threat_assessment: 'threat assessment',
  handoff: 'track handoff',
  assess: 'point assessment',
});

/** Canonical mission kind for a call (port of analyst_policy.mission_kind). */
export function missionKind(tool, args) {
  const a = args && typeof args === 'object' ? args : {};
  if (tool === 'uav_mission' || tool === 'mission_dry_run') {
    const kind = a.kind ?? (tool === 'mission_dry_run' ? 'grid_search' : null);
    if (kind == null) return null;
    return MISSION_KIND[String(kind).toLowerCase()] ?? null;
  }
  return TOOL_KIND[tool] ?? null;
}

export function kindLabel(kind) {
  return (kind && KIND_LABEL[kind]) || 'mission';
}

/** Words with their indefinite article: "an orbit", "a grid search". */
export function withArticle(words) {
  const w = String(words ?? '').trim();
  if (!w) return '';
  return `${/^[aeiou]/i.test(w) ? 'an' : 'a'} ${w}`;
}

/** Words for a server mission kind as it appears on graph nodes (`track`, `grid_search`). */
export function kindWords(kind) {
  if (!kind) return 'mission';
  const key = String(kind).toLowerCase();
  return (
    KIND_LABEL[key] || KIND_LABEL[MISSION_KIND[key]] || key.replace(/_/g, ' ')
  );
}

/** The aircraft a call commits (the receiver on a handoff). */
export function callVehicle(tool, args) {
  const a = args && typeof args === 'object' ? args : {};
  const v =
    tool === 'mission_handoff_track' || tool === 'uav_handoff_target'
      ? a.to_vehicle
      : a.vehicle;
  return typeof v === 'string' && v ? v : null;
}

/** `approval.vehicle` (contract v1.1) falling back to the args. */
export function approvalVehicle(approval) {
  if (!approval) return null;
  if (typeof approval.vehicle === 'string' && approval.vehicle)
    return approval.vehicle;
  return callVehicle(approval.tool, approval.args);
}

/** `approval.dry_runnable` (v1.1) falling back to the server's list. */
export function isDryRunnable(approval) {
  if (!approval) return false;
  if (typeof approval.dry_runnable === 'boolean') return approval.dry_runnable;
  if (approval.args?.dry_run === true) return false;
  return DRY_RUNNABLE_TOOLS.includes(approval.tool);
}

/** Approve verb per tool and class (spec §6.5 item 9). */
export function approveVerb(tool, klass) {
  const k = classKey(klass);
  if (k === 'sensor')
    return tool === 'uav_scan_targets'
      ? 'Approve scan'
      : 'Approve sensor tasking';
  if (k === 'sim') return 'Approve change';
  if (k === 'safety_override') return 'Approve override';
  if (tool === 'uav_takeoff') return 'Approve takeoff';
  if (tool === 'uav_land') return 'Approve landing';
  if (tool === 'uav_return_to_home') return 'Approve return';
  if (tool === 'mission_cancel') return 'Approve cancel';
  if (tool === 'uav_abort') return 'Approve abort';
  if (tool === 'uav_handoff_target') return 'Approve handoff';
  if (
    tool === 'uav_goto_gps' ||
    tool === 'uav_fly_route' ||
    tool === 'uav_orbit_poi' ||
    tool === 'uav_hover'
  )
    return 'Approve flight';
  if (tool === 'uav_mission' || String(tool || '').startsWith('mission_'))
    return 'Approve launch';
  return 'Approve command';
}

/** Policy line under the buttons (spec §6.5 item 10). */
export function policyLine(klass) {
  const k = classKey(klass);
  if (k === 'command') return 'Commands are approved one at a time.';
  if (k === 'sim')
    return 'Simulation changes are approved one at a time in this version.';
  if (k === 'safety_override') return 'Overrides are approved one at a time.';
  return '';
}

/** Graph node type for an entity id prefix (contract §4). */
const PREFIX_TYPE = Object.freeze({
  veh: 'vehicle',
  msn: 'mission',
  trk: 'track',
  unit: 'unit',
  ob: 'equipment',
  rpt: 'report',
  thr: 'theater',
  poi: 'poi',
  alarm: 'alarm',
  feed: 'feed',
});

export function nodeTypeOf(id) {
  const prefix = String(id || '').split(':')[0];
  return PREFIX_TYPE[prefix] ?? 'unknown';
}

/** The bare name behind an id: `veh:Drone1` -> `Drone1`. */
export function bareId(id) {
  const text = String(id || '');
  const at = text.indexOf(':');
  return at >= 0 ? text.slice(at + 1) : text;
}

// ---- time ------------------------------------------------------------------

const pad2 = (n) => String(n).padStart(2, '0');

/** Zulu wall time: `14:02Z`, or `14:02:51Z` with seconds. Empty when unknown. */
export function zulu(ms, { seconds = false } = {}) {
  if (!Number.isFinite(ms)) return '';
  const d = new Date(ms);
  const hm = `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
  return seconds ? `${hm}:${pad2(d.getUTCSeconds())}Z` : `${hm}Z`;
}

/** Elapsed time for rows and footers: `0.4 s`, `6.2 s`, `12 s`, `2 min 5 s`. */
export function duration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)} s`;
  const total = Math.round(ms / 1000);
  if (total < 60) return `${total} s`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  if (m < 60) return s ? `${m} min ${s} s` : `${m} min`;
  const hrs = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `${hrs} h ${mm} min` : `${hrs} h`;
}

/** Whole seconds, for "Running, 12 s" style live counters. */
export function wholeSeconds(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  const total = Math.floor(ms / 1000);
  if (total < 60) return `${total} s`;
  return duration(total * 1000);
}

/** Seconds as `14 min 20 s` (ETAs). */
export function spanText(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '';
  return duration(Math.round(seconds) * 1000);
}

/** Countdown as `9:48`. */
export function countdown(ms) {
  const total = Math.max(0, Math.ceil((Number(ms) || 0) / 1000));
  const m = Math.floor(total / 60);
  return `${m}:${pad2(total % 60)}`;
}

/**
 * Relative age: `12 s ago`, `1 min ago`, `3 h ago`. Whole units counted
 * down, as the rail and inspector count them (4 min 50 s is "4 min ago").
 */
export function ago(atMs, nowMs) {
  if (!Number.isFinite(atMs) || !Number.isFinite(nowMs)) return '';
  const s = Math.max(0, Math.round((nowMs - atMs) / 1000));
  if (s < 60) return `${s} s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  return `${Math.floor(m / 60)} h ago`;
}

/** Normalize a rate-limit `resets_at` (unix seconds or ms) to ms. */
export function epochMs(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n < 1e12 ? n * 1000 : n;
}

// ---- numbers -----------------------------------------------------------------

export function pct(value, digits = 1) {
  return Number.isFinite(value) ? `${Number(value).toFixed(digits)}%` : '';
}

export function grouped(n) {
  return Number.isFinite(n) ? Math.round(n).toLocaleString('en-US') : '';
}

/** Token counts: `410`, `3.1k`, `1.2M`. */
export function tokens(n) {
  if (!Number.isFinite(n)) return '';
  if (n < 1000) return String(Math.round(n));
  if (n < 1e6) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`;
  return `${(n / 1e6).toFixed(1).replace(/\.0$/, '')}M`;
}

export function usd(n) {
  return Number.isFinite(n) ? `$${Number(n).toFixed(2)}` : '';
}

/** Bytes as `3.2 KB`. */
export function bytes(n) {
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Coordinates with a real minus sign, 5 decimals. */
export function coord(value) {
  if (!Number.isFinite(value)) return '';
  const text = Math.abs(value).toFixed(5);
  return value < 0 ? `−${text}` : text;
}

// ---- text ------------------------------------------------------------------

/**
 * Split a server string on the " · " separator (spec §11.2). The console never
 * writes that separator itself; it renders the parts as spaced segments.
 */
export function segments(text) {
  if (text == null) return [];
  return String(text)
    .split(' · ')
    .map((part) => part.trim())
    .filter(Boolean);
}

/** Capitalize the first letter only. */
export function capitalize(text) {
  const s = String(text ?? '');
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** Truncate to `max` characters with an ellipsis. */
export function truncate(text, max) {
  const s = String(text ?? '');
  return s.length > max ? `${s.slice(0, Math.max(0, max - 1)).trimEnd()}…` : s;
}

/** Join words as "a, b and c". */
export function listWords(words) {
  const list = words.filter(Boolean);
  if (list.length <= 1) return list.join('');
  return `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`;
}

/** The last complete sentence of a streaming text, for "Thinking: …". */
export function latestSentence(text, max = 90) {
  const s = String(text ?? '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!s) return '';
  const parts = s.split(/(?<=[.!?])\s+/).filter(Boolean);
  const last = parts[parts.length - 1] || s;
  return truncate(last, max);
}

/** Link-state words (safety LinkState values). */
export function linkWord(link) {
  if (link === 'up') return 'Link up';
  if (link === 'degraded') return 'Link degraded';
  if (link === 'pending') return 'Link down';
  if (link === 'loal' || link === 'lost') return 'Link lost';
  return '';
}

/** Warnings: strip a leading `code:` prefix and capitalize (spec §6.5). */
export function warningText(text) {
  const s = String(text ?? '').trim();
  const stripped = s.replace(/^[a-z0-9_]+:\s*/, '');
  return capitalize(stripped);
}

// ---- bidi ------------------------------------------------------------------

/**
 * Unicode bidi embedding, override and isolate controls (U+202A–U+202E,
 * U+2066–U+2069). Contact names come from third-party map tags and analyst
 * args are interpolated into server copy, so a U+202E can make "YLDNEIRF"
 * read "FRIENDLY" or reverse the console's own words after it. The console
 * never renders them.
 */
const BIDI_CONTROLS = /[\u202A-\u202E\u2066-\u2069]/g;

/** Remove bidi controls from a string; anything else passes through. */
export function stripBidi(value) {
  return typeof value === 'string' ? value.replace(BIDI_CONTROLS, '') : value;
}

/**
 * Show bidi controls as JSON escapes (`\u202E`), for "Show exact request":
 * the operator sees that the control is there, and the text is still the
 * same JSON value.
 */
export function escapeBidi(text) {
  return String(text ?? '').replace(
    BIDI_CONTROLS,
    (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`,
  );
}

/** Attributes whose words reach the screen (tooltips, names, hints). */
const TEXT_ATTRS = new Set(['title', 'aria-label', 'placeholder', 'alt']);

/**
 * Wrap an element factory `h(tag, attrs, ...kids)` so every string child and
 * text attribute has its bidi controls removed. The chat's DOM modules build
 * all of their text through the wrapped factory.
 */
export function bidiSafe(h) {
  return (tag, attrs = {}, ...kids) => {
    let clean = attrs;
    if (attrs && typeof attrs === 'object') {
      for (const key of Object.keys(attrs)) {
        if (TEXT_ATTRS.has(key) && typeof attrs[key] === 'string') {
          if (clean === attrs) clean = { ...attrs };
          clean[key] = stripBidi(attrs[key]);
        }
      }
    }
    return h(tag, clean, ...kids.map(stripBidi));
  };
}
