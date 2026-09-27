/**
 * Situation rail (UX spec §7.2), its compact 64 px strip and drawer (§3), and
 * the operator Abort confirm shared by the rail, the inspector and the
 * tracking dock (§6.9).
 *
 * This module also hosts the small formatting kit the other two panels
 * (search.js, inspector.js) import: Zulu time, reading registers, status
 * words, `" · "` segment splitting and the fuel-versus-BINGO math. Keeping it
 * here means every panel states a reading the same way.
 *
 * DOM is built only through ../ui/uavDom.js helpers so the module runs under
 * GEV's stub document in node tests. Nothing touches a browser global at
 * import time.
 */
import { clamp, h, replaceKids, setClass, setHidden } from '../ui/uavDom.js';
import { glyphSvg } from './orb/glyphs.js';
import {
  ALARM_KIND_LABEL,
  alarmNodeLabel,
  feedLabel as orbFeedLabel,
  feedState as orbFeedWord,
} from './orb/text.js';
import { kindWords } from './chat/format.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Material Symbols glyph names. Glyph names reach the DOM only through this
 * frozen map (the materialSymbolsSubset test pins every lowercase literal a
 * `textContent` statement names). All of these are in UX spec §11.5.
 */
export const ICON = Object.freeze({
  abort: 'pan_tool',
  ask: 'forum',
  back: 'arrow_back',
  close: 'close',
  copy: 'content_copy',
  critical: 'error',
  focus: 'center_focus_weak',
  info: 'info',
  open: 'open_in_full',
  retry: 'refresh',
  search: 'search',
  track: 'my_location',
  warning: 'warning',
});

/** Node type -> the word the console uses for it. */
export const TYPE_WORD = Object.freeze({
  vehicle: 'Vehicle',
  mission: 'Mission',
  track: 'Contact',
  unit: 'Unit',
  equipment: 'Equipment class',
  report: 'Report',
  theater: 'Theater',
  poi: 'Place',
  alarm: 'Alarm',
  feed: 'Feed',
});

/** Alarm kind -> label (UX spec §7.2), shared with the orb (orb/text.js).
 *  Unknown kinds are humanized. */
export { ALARM_KIND_LABEL };

/** Alarm kinds that raise the critical banner (§7.3). */
export const CRITICAL_ALARM_KINDS = Object.freeze([
  'bingo',
  'geofence_breach',
  'lost_link',
]);

export const SEVERITY_WORD = Object.freeze({
  critical: 'Critical',
  warning: 'Warning',
  info: 'Info',
});

const SEVERITY_ICON = Object.freeze({
  critical: ICON.critical,
  warning: ICON.warning,
  info: ICON.info,
});

/** Mission phase -> word (§7.1). */
export const PHASE_WORD = Object.freeze({
  planning: 'Planning',
  executing: 'Executing',
  rtb: 'Returning home',
  complete: 'Complete',
  aborted: 'Aborted',
});

const RUNNING_PHASES = new Set(['planning', 'executing', 'rtb']);

const THREAT_WORD = Object.freeze({
  critical: 'Critical',
  high: 'High',
  moderate: 'Moderate',
  low: 'Low',
  none: 'None',
});

const NODE_TYPES = new Set(Object.keys(TYPE_WORD));
const NODE_STATUSES = new Set(['ok', 'warn', 'critical', 'stale', 'unknown']);

export const NOT_IN_PICTURE =
  'Not in the current picture (outside this theater or aged out).';
const NOT_ASSESSED_NOTE = 'Not the same as no threat.';

/** Stale telemetry threshold (ms): the graph marks a vehicle critical past it. */
const TELEMETRY_STALE_MS = 5000;
/** "Updated N s ago" turns warn after this long (§7.2). */
const UPDATED_WARN_MS = 10_000;

// ---------------------------------------------------------------------------
// Formatting kit (pure; exported for search.js and inspector.js)
// ---------------------------------------------------------------------------

const pad2 = (n) => String(n).padStart(2, '0');

/** A finite number or null — never a silent zero. */
export function num(value) {
  if (value == null || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** Epoch ms as Zulu time: "14:02Z", or "14:02:51Z" with seconds. */
export function zulu(ms, { seconds = false } = {}) {
  const n = num(ms);
  if (n == null) return '';
  const d = new Date(n);
  const hm = `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
  return seconds ? `${hm}:${pad2(d.getUTCSeconds())}Z` : `${hm}Z`;
}

/** Elapsed time as "2 s", "6 min", "3 h", "2 d". Empty when unknown. */
export function ago(ms, now = Date.now()) {
  const n = num(ms);
  if (n == null) return '';
  const s = Math.max(0, Math.round((now - n) / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const hr = Math.floor(m / 60);
  if (hr < 48) return `${hr} h`;
  return `${Math.floor(hr / 24)} d`;
}

/** Seconds as "14 min 20 s", "50 s", "1 h 5 min". Empty when unknown. */
export function duration(seconds) {
  const n = num(seconds);
  if (n == null || n < 0) return '';
  const total = Math.round(n);
  const hr = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (hr) return m ? `${hr} h ${m} min` : `${hr} h`;
  if (m) return s ? `${m} min ${s} s` : `${m} min`;
  return `${s} s`;
}

/**
 * Words that read as acronyms ("SAM", never "Sam"); the same set as the
 * host's intel_graph.ACRONYMS, so a category reads alike in every panel.
 */
const ACRONYMS = new Set([
  'sam',
  'aaa',
  'ew',
  'c2',
  'mlrs',
  'ifv',
  'mbt',
  'apc',
  'manpads',
  'uav',
  'isr',
  'gps',
  'poi',
  'ao',
  'los',
]);

/** "grid_search" -> "Grid search"; "sam_site" -> "SAM site". */
export function humanize(value) {
  const s = String(value ?? '')
    .replace(/_/g, ' ')
    .trim()
    .split(/\s+/)
    .map((w) => (ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase() : w))
    .join(' ');
  return s ? s[0].toUpperCase() + s.slice(1) : '';
}

/**
 * A server mission kind as a title word ("Orbit", "Grid search", "Contact
 * track"): the chat's kind words, so the rail, dock, inspector and launch
 * copy never show raw snake case such as "Orbit poi".
 */
export function missionKindTitle(kind) {
  if (!kind) return '';
  return humanize(kindWords(kind));
}

/** Split a server string on `" · "` into its segments (the console never writes `·`). */
export function splitSegments(text) {
  return String(text ?? '')
    .split(' · ')
    .map((part) => part.trim())
    .filter(Boolean);
}

/**
 * Server string -> spaced segment spans with visually hidden ", " separators,
 * so a screen reader hears a list and the eye sees columns.
 */
export function segments(text, cls = '') {
  const kids = [];
  splitSegments(text).forEach((part, i) => {
    if (i) kids.push(h('span', { class: 'ic-kit-vh' }, ', '));
    kids.push(h('span', { class: 'ic-kit-part' }, part));
  });
  return h('span', { class: `ic-kit-parts ${cls}`.trim() }, ...kids);
}

/** A node by id from an intelStore state, whether `byId` is a Map or an object. */
export function nodeOf(state, id) {
  if (!id) return null;
  const by = state?.byId;
  if (by && typeof by.get === 'function') {
    const hit = by.get(id);
    if (hit) return hit;
  } else if (by && Object.prototype.hasOwnProperty.call(by, id)) {
    return by[id];
  }
  const nodes = state?.graph?.nodes;
  return Array.isArray(nodes)
    ? (nodes.find((n) => n?.id === id) ?? null)
    : null;
}

/** Graph nodes of one type, in server order. */
export function nodesOfType(state, type) {
  const nodes = state?.graph?.nodes;
  return Array.isArray(nodes) ? nodes.filter((n) => n?.type === type) : [];
}

/** The id after the type prefix: "trk:T-3fa9c1" -> "T-3fa9c1". */
export function bareId(id) {
  const s = String(id ?? '');
  const i = s.indexOf(':');
  return i >= 0 ? s.slice(i + 1) : s;
}

/** Whether a contact/unit threat word is an assessment (not missing). */
export function isAssessed(threat) {
  const t = String(threat ?? '')
    .trim()
    .toLowerCase();
  return Boolean(t) && t !== 'not assessed' && t !== 'unknown';
}

/**
 * The status word paired with a node's colour (§2.2, §4.3). Contacts and
 * units read their threat word; a missing assessment is "Not assessed",
 * never "none" and never "Low".
 */
export function statusWord(node) {
  const type = node?.type;
  const status = node?.status;
  const attrs = node?.attrs || {};
  if (type === 'track' || type === 'unit') {
    if (status === 'stale') return 'Stale';
    if (status === 'unknown' || !isAssessed(attrs.threat))
      return 'Not assessed';
    const t = String(attrs.threat).toLowerCase();
    return THREAT_WORD[t] || humanize(t);
  }
  if (type === 'alarm') return SEVERITY_WORD[attrs.severity] || '';
  if (type === 'feed') {
    return (
      { ok: 'Up', warn: 'Degraded', critical: 'Down', stale: 'Stale' }[
        status
      ] || ''
    );
  }
  if (type === 'equipment') return '';
  return (
    {
      warn: 'Warning',
      critical: 'Critical',
      stale: 'Stale',
      unknown: 'Unknown',
    }[status] || ''
  );
}

/**
 * The colour tone for a node (§4.3): contacts and units that are `ok` are an
 * assessed-low film white, never green; equipment classes and places are
 * neutral Pencil. Lets CSS colour by one attribute.
 */
export function toneOf(type, status) {
  const s = NODE_STATUSES.has(status) ? status : 'unknown';
  if (type === 'equipment' || type === 'poi') return 'neutral';
  // An info-level alarm is Pencil grey, as the orb draws it, never green.
  if (type === 'alarm' && s === 'ok') return 'neutral';
  if (s === 'ok' && (type === 'track' || type === 'unit')) return 'low';
  return s;
}

/** Store statuses in which the picture is the last one fetched, not live. */
const PICTURE_NOT_LIVE = new Set(['stale', 'offline', 'unauthorized']);

/** Alarm id as the store keys it: `alarm.id`, else `alarm:{seq}`. */
export function alarmId(alarm) {
  if (!alarm) return null;
  if (alarm.id != null) return String(alarm.id);
  const seq = num(alarm.seq);
  return seq == null ? null : `alarm:${seq}`;
}

/** Alarm kind label, or a humanized kind. */
export function alarmLabel(kind) {
  return ALARM_KIND_LABEL[kind] || humanize(kind) || 'Alarm';
}

/** Link state word from a string or a `{state}` block, lowercased. */
export function linkState(link) {
  const raw = link && typeof link === 'object' ? link.state : link;
  const s = String(raw ?? '')
    .trim()
    .toLowerCase();
  return s || null;
}

// ---- fuel versus BINGO ----------------------------------------------------

/**
 * Fuel versus BINGO for one vehicle (graph `attrs` shape: fuel_pct,
 * bingo_fuel_pct, bingo_latched). Pure.
 *
 * state: 'unknown' (no fuel reading) · 'latched' (BINGO latched, returning) ·
 * 'no-bingo' (BINGO line not computed yet) · 'below' (at or under BINGO) ·
 * 'near' (under 10 points above) · 'above'.
 */
export function fuelState(attrs = {}) {
  const fuel = num(attrs.fuel_pct);
  const bingo = num(attrs.bingo_fuel_pct);
  const latched = attrs.bingo_latched === true;
  // A latched BINGO outlives the return: once down, the aircraft is landed,
  // not returning home.
  const landed = attrs.landed === true;
  const margin = fuel != null && bingo != null ? fuel - bingo : null;
  let state;
  if (fuel == null) state = 'unknown';
  else if (latched) state = 'latched';
  else if (bingo == null) state = 'no-bingo';
  else if (margin <= 0) state = 'below';
  else if (margin < 10) state = 'near';
  else state = 'above';
  return {
    fuel,
    bingo,
    margin,
    latched,
    landed,
    state,
    fillPct: clamp(fuel, 0, 100, 0),
    tickPct: bingo == null ? null : clamp(bingo, 0, 100),
  };
}

/**
 * The colour of a fuel reading, from the fuel margin alone (§7.2): critical
 * at or below BINGO (or latched), warn under 10 points above, ok above that.
 * A vehicle that is critical for another reason (link lost, stale telemetry)
 * does not turn its fuel red. 'unknown' when there is no fuel reading,
 * 'neutral' when the BINGO line is not computed yet.
 * @returns {'ok'|'warn'|'critical'|'unknown'|'neutral'}
 */
export function fuelTone(fs) {
  switch (fs?.state) {
    case 'latched':
    case 'below':
      return 'critical';
    case 'near':
      return 'warn';
    case 'above':
      return 'ok';
    case 'no-bingo':
      return 'neutral';
    default:
      return 'unknown';
  }
}

/** Points above BINGO: "42 points", "4.6 points", "1 point". */
export function formatPoints(margin, decimals = null) {
  const n = num(margin);
  if (n == null) return '';
  const digits = decimals ?? (Math.abs(n) >= 10 ? 0 : 1);
  const text = n.toFixed(digits);
  return `${text} ${Number(text) === 1 ? 'point' : 'points'}`;
}

/** Fuel percentage: "64%" (rail) or "64.0%" (inspector). */
export function formatFuel(value, decimals = 0) {
  const n = num(value);
  return n == null ? '' : `${n.toFixed(decimals)}%`;
}

/**
 * The fuel-versus-BINGO sentence. Only a latched BINGO claims the aircraft is
 * returning; a reading below the line without a latch says only that.
 */
export function fuelSentence(fs, { decimals = null, short = false } = {}) {
  switch (fs.state) {
    case 'unknown':
      return 'No fuel reading yet';
    case 'no-bingo':
      return 'BINGO not computed yet';
    case 'latched':
      if (fs.landed === true)
        return fs.margin != null && fs.margin > 0
          ? 'Landed; BINGO latched.'
          : 'Below BINGO. Landed; BINGO latched.';
      return fs.margin != null && fs.margin > 0
        ? "BINGO latched. Returning home; that can't be cancelled."
        : "Below BINGO. Returning home; that can't be cancelled.";
    case 'below':
      return 'Below BINGO.';
    default:
      return short
        ? `${formatPoints(fs.margin, decimals)} above`
        : `${formatPoints(fs.margin, decimals)} above BINGO`;
  }
}

/** Vehicle state word (§7.2): Landed, Airborne, Returning home on BINGO, Link lost. */
export function vehicleStateWord(attrs = {}) {
  // BINGO stays latched after the return lands; a landed aircraft is Landed.
  if (attrs.bingo_latched === true && attrs.landed !== true)
    return 'Returning home on BINGO';
  const link = linkState(attrs.link);
  if (link === 'loal' || link === 'lost') return 'Link lost';
  if (attrs.landed === true) return 'Landed';
  if (attrs.landed === false) return 'Airborne';
  return 'State unknown';
}

/**
 * Link and telemetry lines for a vehicle: [{text, status}]. Stale telemetry
 * is stated first because every other reading depends on it.
 */
export function linkLines(attrs = {}) {
  const lines = [];
  const staleMs = num(attrs.stale_ms);
  if (staleMs != null && staleMs > TELEMETRY_STALE_MS) {
    lines.push({
      text: `Telemetry stale, last ${ago(0, staleMs)} ago`,
      status: 'stale',
    });
  }
  const link = linkState(attrs.link);
  if (link === 'up') lines.push({ text: 'Link up', status: 'ok' });
  else if (link === 'degraded')
    lines.push({ text: 'Link degraded', status: 'warn' });
  else if (link === 'pending')
    lines.push({ text: 'Link down, not declared lost yet', status: 'warn' });
  else if (link === 'loal' || link === 'lost') {
    // `link_lost_since_ms` (graph v1.1) dates the loss; without it the line
    // makes no claim about when.
    const since = num(attrs.link_lost_since_ms);
    lines.push({
      text:
        since != null
          ? `Link lost since ${zulu(since, { seconds: true })}`
          : 'Link lost',
      status: 'critical',
    });
  } else lines.push({ text: 'Link: no reading', status: 'unknown' });
  return lines;
}

/** Abort is offered for airborne vehicles, vehicles with a task, and unknown state. */
export function canAbort(attrs = {}) {
  return attrs.landed !== true || Boolean(attrs.mission);
}

// ---------------------------------------------------------------------------
// DOM kit (built only from uavDom helpers)
// ---------------------------------------------------------------------------

/** A Material Symbols icon span. `name` must come from ICON. */
export function icon(name) {
  return h(
    'span',
    { class: 'material-symbols-outlined ic-kit-sym', 'aria-hidden': 'true' },
    name,
  );
}

/**
 * An orb glyph (constant SVG from orb/glyphs.js) in its status colour. Type and
 * status are whitelisted before they reach glyphSvg, so the markup set as
 * innerHTML is derived only from constants.
 */
export function glyph(type, status, size = 16, phase) {
  const safeType = NODE_TYPES.has(type) ? type : 'track';
  const safeStatus = NODE_STATUSES.has(status) ? status : 'unknown';
  const el = h('span', {
    class: 'ic-kit-glyph',
    'aria-hidden': 'true',
    'data-type': safeType,
    'data-status': safeStatus,
  });
  let svg = '';
  try {
    svg = glyphSvg(safeType, {
      status: safeStatus,
      size,
      phase: typeof phase === 'string' ? phase : undefined,
    });
  } catch {
    svg = '';
  }
  if (typeof svg === 'string' && svg) el.innerHTML = svg;
  return el;
}

/** A button with an optional icon; `key` restores focus across re-renders. */
export function button(
  text,
  { icon: name, cls = '', key, label, onClick } = {},
) {
  const el = h(
    'button',
    {
      type: 'button',
      class: `ic-kit-btn ${cls}`.trim(),
      'data-key': key,
      'aria-label': label,
    },
    name ? icon(name) : null,
    h('span', { class: 'ic-kit-btn__text' }, text),
  );
  if (onClick) el.addEventListener('click', onClick);
  return el;
}

/** A reading-register tag: "Estimated", "Assumed", "Measured", "Requested". */
export function registerTag(register, title) {
  const word = {
    estimated: 'Estimated',
    assumed: 'Assumed',
    measured: 'Measured',
    requested: 'Requested',
  }[register];
  return h(
    'span',
    { class: 'ic-kit-reg', 'data-register': register, title },
    word || humanize(register),
  );
}

/** The "No reading" dashed box with its reason — never a zero, never a blank. */
export function noReading(reason) {
  return h(
    'span',
    { class: 'ic-kit-noreading' },
    h('span', { class: 'ic-kit-noreading__title' }, 'No reading'),
    reason ? h('span', { class: 'ic-kit-noreading__reason' }, reason) : null,
  );
}

/** Italic lilac "Not assessed" with its tooltip. */
export function notAssessed() {
  return h(
    'span',
    { class: 'ic-kit-notassessed', title: NOT_ASSESSED_NOTE },
    'Not assessed',
  );
}

/** Set a CSS custom property without assuming a `style` object (stub DOM). */
export function setVar(el, name, value) {
  if (el?.style?.setProperty) el.style.setProperty(name, value);
  else el?.setAttribute?.(`data-${name.replace(/^--/, '')}`, value);
}

/**
 * Fuel bar from 0 to 100 % with a BINGO tick. Horizontal (rail, inspector) or
 * vertical (compact strip). Fill colour follows the vehicle node's status.
 */
export function fuelBar(fs, { status = 'unknown', vertical = false } = {}) {
  const parts = [];
  parts.push(
    fs.fuel == null ? 'Fuel: no reading' : `Fuel ${formatFuel(fs.fuel)}`,
  );
  if (fs.bingo != null) parts.push(`BINGO ${formatFuel(fs.bingo)}`);
  else if (fs.fuel != null) parts.push('BINGO not computed yet');
  const bar = h('span', {
    class: 'ic-kit-fuel',
    role: 'img',
    'aria-label': parts.join(', '),
    'data-orient': vertical ? 'vertical' : 'horizontal',
    'data-status': status,
    'data-state': fs.state,
  });
  const orient = vertical ? 'vertical' : 'horizontal';
  const fill = h('span', { class: 'ic-kit-fuel__fill', 'data-orient': orient });
  setVar(fill, '--ic-fill', `${fs.fillPct}%`);
  bar.append(fill);
  if (fs.tickPct != null) {
    const tick = h('span', {
      class: 'ic-kit-fuel__tick',
      'data-orient': orient,
    });
    setVar(tick, '--ic-tick', `${fs.tickPct}%`);
    bar.append(tick);
  }
  return bar;
}

/** Magenta mission progress bar. */
export function progressBar(pct, label) {
  const n = num(pct);
  const bar = h('span', {
    class: 'ic-kit-progress',
    role: 'img',
    'aria-label':
      n == null
        ? `${label}: progress not reported`
        : `${label} ${Math.round(n)}%`,
  });
  const fill = h('span', { class: 'ic-kit-progress__fill' });
  setVar(fill, '--ic-fill', `${clamp(n, 0, 100, 0)}%`);
  bar.append(fill);
  return bar;
}

/** Plain text of a stub or real element (tests and aria summaries). */
function walk(el, visit) {
  if (!el || typeof el !== 'object') return null;
  if (visit(el)) return el;
  for (const kid of el.children || []) {
    const hit = walk(kid, visit);
    if (hit) return hit;
  }
  return null;
}

/** Every element under `root` carrying `data-key` = key, in document order. */
function allByKey(root, key) {
  const out = [];
  if (!root || !key) return out;
  if (typeof root.querySelectorAll === 'function') {
    for (const el of root.querySelectorAll('[data-key]'))
      if (el.getAttribute?.('data-key') === key) out.push(el);
    return out;
  }
  walk(root, (el) => {
    if (el.getAttribute?.('data-key') === key) out.push(el);
    return false;
  });
  return out;
}

/**
 * Remember which keyed control had focus, so a re-render can restore it.
 * The same entity can appear twice (a field chip and a Related chip share a
 * key), so the position among same-key controls is kept too.
 * @returns {{key:string, nth:number}|null}
 */
export function focusedKey(root) {
  const active = globalThis.document?.activeElement;
  if (!active || active === root) return null;
  if (typeof root?.contains === 'function' && !root.contains(active))
    return null;
  const key = active.getAttribute?.('data-key') || null;
  if (!key) return null;
  return { key, nth: Math.max(0, allByKey(root, key).indexOf(active)) };
}

/** Re-focus the control `focusedKey` remembered, after a re-render. */
export function restoreFocus(root, mem) {
  if (!mem) return;
  const key = typeof mem === 'string' ? mem : mem.key;
  const hits = allByKey(root, key);
  if (!hits.length) return;
  const nth = typeof mem === 'string' ? 0 : Number(mem.nth) || 0;
  hits[Math.min(nth, hits.length - 1)]?.focus?.();
}

/** Whether an element is still in the document (stub elements count as in). */
function isConnected(el) {
  return Boolean(el) && el.isConnected !== false;
}

/**
 * Whether focus can land on `el`: connected, focusable, and not inside a
 * hidden or inert panel (at narrow the rail's tab panel is both while the
 * Orb tab shows, and focus() on it silently does nothing).
 */
function canTakeFocus(el) {
  if (!isConnected(el) || typeof el.focus !== 'function') return false;
  try {
    if (el.closest?.('[hidden], [inert]')) return false;
    if (typeof el.getClientRects === 'function' && !el.getClientRects().length)
      return false;
  } catch {
    return true;
  }
  return true;
}

/**
 * Remember the control that opened something (a confirm, tracking, a
 * sheet), so focus can go back to it. Panels rebuild their buttons on every
 * poll, so the element alone goes stale: its `data-key` and nearest panel
 * scope let `recallFocus` find the re-rendered twin.
 * @param {object|null} el the opener, usually document.activeElement
 * @returns {{el:object, key:string|null, scope:object|null}|null}
 */
export function rememberFocus(el) {
  if (!el || typeof el !== 'object') return null;
  const doc = globalThis.document;
  if (el === doc?.body || el === doc?.documentElement) return null;
  let scope = null;
  try {
    scope = el.closest?.('.ic-inspector, .ic-rail, .ic-dock, .ic-root') || null;
  } catch {
    scope = null;
  }
  return { el, key: el.getAttribute?.('data-key') || null, scope };
}

/**
 * The remembered control, or its re-rendered twin (same `data-key`, looked up
 * in its old scope, then the document), or the first `fallbackKeys` match.
 * @returns {object|null}
 */
export function recallFocus(mem, { doc, fallbackKeys = [] } = {}) {
  if (!mem) return null;
  if (canTakeFocus(mem.el)) return mem.el;
  const d = doc ?? globalThis.document;
  const roots = [];
  if (mem.scope && isConnected(mem.scope)) roots.push(mem.scope);
  const top = d?.body || d?.documentElement || null;
  if (top) roots.push(top);
  for (const key of [mem.key, ...fallbackKeys]) {
    if (!key) continue;
    for (const r of roots) {
      const hit = allByKey(r, key).find(canTakeFocus);
      if (hit) return hit;
    }
  }
  return null;
}

/** Focus what `rememberFocus` kept (see recallFocus). Returns the target. */
export function focusBack(mem, opts = {}) {
  const target = recallFocus(mem, opts);
  if (!target) return null;
  try {
    target.focus({ preventScroll: true });
  } catch {
    return null;
  }
  return target;
}

/**
 * Where to put a popover next to the control that opened it: under it,
 * flipped above when it would run off the bottom, clamped to the viewport
 * gutters. Pure.
 * @param {{left:number, top:number, bottom:number}} anchor client rect
 * @param {{width:number, height:number}} box the popover's size
 * @param {{width:number, height:number}} viewport
 * @returns {{left:number, top:number, flipped:boolean}}
 */
export function anchorPlacement(anchor, box, viewport, gutter = 16, gap = 8) {
  const vw = Number(viewport?.width) || 0;
  const vh = Number(viewport?.height) || 0;
  const w = Number(box?.width) || 0;
  const bh = Number(box?.height) || 0;
  let top = anchor.bottom + gap;
  let flipped = false;
  if (top + bh > vh - gutter) {
    const above = anchor.top - gap - bh;
    if (above >= gutter) {
      top = above;
      flipped = true;
    } else top = Math.max(gutter, vh - gutter - bh);
  }
  const maxLeft = Math.max(gutter, vw - gutter - w);
  const left = Math.min(Math.max(gutter, anchor.left), maxLeft);
  return { left: Math.round(left), top: Math.round(top), flipped };
}

// ---------------------------------------------------------------------------
// Operator Abort confirm (§6.9)
// ---------------------------------------------------------------------------

let confirmSeq = 0;
/** One confirm at a time, per page. */
let openConfirm = null;

function errorKind(err) {
  const name = err?.name;
  if (name && name !== 'Error') return name;
  return err?.constructor?.name || 'Error';
}

/** A message without trailing punctuation, so copy can end the sentence. */
export function bareMessage(value, fallback = 'no error detail') {
  const text = String(value ?? '')
    .trim()
    .replace(/[.!\s]+$/, '');
  return text || fallback;
}

function errorText(err) {
  return bareMessage(err?.message || err);
}

/**
 * Classify a `/control/command {tool:"uav_abort"}` answer (the bridge unwraps
 * the MCP envelope): success, refused/rejected by the server, busy, a tool
 * error, or an answer that doesn't confirm the abort. Pure.
 * @returns {{outcome:'aborted'|'refused'|'busy'|'error'|'unconfirmed', text:string, retry:boolean}}
 */
export function abortOutcome(result, vehicle, nowMs = Date.now()) {
  const res = result && typeof result === 'object' ? result : {};
  if (res.refused === true || res.rejected === true) {
    const reason = String(res.reason || res.error || 'no reason given').trim();
    return {
      outcome: 'refused',
      text: `Abort refused: ${reason}`,
      retry: false,
    };
  }
  if (res.status === 'busy') {
    const current =
      res.current?.tool || res.current?.mission_id || res.current?.task_id;
    return {
      outcome: 'busy',
      text: current
        ? `Abort wasn't run: ${vehicle} is busy with ${current}.`
        : `Abort wasn't run: ${vehicle} is busy with another task.`,
      retry: true,
    };
  }
  if (res.error || res.isError) {
    return {
      outcome: 'error',
      text: `Abort failed on ${vehicle}: ${bareMessage(res.error || res.text, 'tool error')}.`,
      retry: true,
    };
  }
  if (res.aborted === true) {
    return {
      outcome: 'aborted',
      text: `${vehicle} aborted at ${zulu(nowMs, { seconds: true })}. Holding position.`,
      retry: false,
    };
  }
  return {
    outcome: 'unconfirmed',
    text: `The server didn't confirm the abort. Check ${vehicle}'s state before trying again.`,
    retry: true,
  };
}

/** A thrown transport error -> honest copy. A timeout may still have landed. */
export function abortFailure(err, vehicle) {
  const kind = errorKind(err);
  if (kind === 'AuthError') {
    return {
      outcome: 'error',
      text: `Abort didn't reach ${vehicle}: the console's access token was rejected.`,
      retry: false,
    };
  }
  if (kind === 'TimeoutError') {
    return {
      outcome: 'error',
      text: `No answer about the abort in time. It may still have reached ${vehicle}; check its state before retrying.`,
      retry: true,
    };
  }
  return {
    outcome: 'error',
    text: `Abort didn't reach ${vehicle}: ${errorText(err)}.`,
    retry: true,
  };
}

/**
 * One-step Abort confirm (no arming delay: an e-stop must be fast). On confirm
 * it calls `ctx.api.abortVehicle(vehicle)` — the existing
 * `POST /control/command {tool:"uav_abort"}`, never the analyst — and shows
 * the honest result.
 *
 * Resolves when the dialog closes: `true` only if the server confirmed the
 * abort, `false` for Keep flying, a refusal, busy or an error.
 * @param {object} ctx console ctx ({api, root, bus})
 * @param {string} vehicle vehicle name, e.g. "Drone1"
 * @param {{now?:()=>number, host?:object}} [opts]
 * @returns {Promise<boolean>}
 */
export function confirmAbort(ctx, vehicle, opts = {}) {
  const name = String(vehicle || '').trim();
  if (!name) return Promise.resolve(false);
  if (openConfirm && openConfirm.vehicle === name) {
    openConfirm.focusSafe();
    return openConfirm.promise;
  }
  openConfirm?.close();

  const now = opts.now || (() => Date.now());
  const host = opts.host || ctx?.root || globalThis.document?.body;
  // The Abort button that opened it (a click in WebKit does not focus it),
  // else whatever had focus. Kept by key: the rail re-renders every poll.
  const anchor = opts.anchor || null;
  const opener = rememberFocus(anchor || globalThis.document?.activeElement);
  const n = ++confirmSeq;
  const titleId = `ic-abort-title-${n}`;
  const bodyId = `ic-abort-body-${n}`;

  let settle;
  const promise = new Promise((resolve) => {
    settle = resolve;
  });
  let aborted = false;
  let closed = false;
  let sending = false;

  const title = h(
    'h2',
    { class: 'ic-abort-confirm__title', id: titleId },
    `Abort ${name}?`,
  );
  const body = h(
    'p',
    { class: 'ic-abort-confirm__body', id: bodyId },
    `It cancels ${name}'s current task, clears its queue and holds position. A BINGO return can't be cancelled.`,
  );
  const result = h('p', { class: 'ic-abort-confirm__result', hidden: true });
  const actions = h('div', { class: 'ic-abort-confirm__actions' });
  const el = h(
    'div',
    {
      class: 'ic-abort-confirm',
      role: 'alertdialog',
      'aria-modal': 'false',
      'aria-labelledby': titleId,
      'aria-describedby': bodyId,
      'data-state': 'confirm',
      'data-vehicle': name,
    },
    title,
    body,
    result,
    actions,
  );

  const keep = button('Keep flying', {
    cls: 'ic-kit-btn--quiet',
    key: 'abort:keep',
    onClick: () => close(),
  });
  const go = button(`Abort ${name}`, {
    icon: ICON.abort,
    cls: 'ic-kit-btn--danger',
    key: 'abort:go',
    onClick: () => send(),
  });

  function setState(state) {
    el.setAttribute('data-state', state);
  }

  function showConfirm() {
    setState('confirm');
    setHidden(result, true);
    replaceKids(actions, [keep, go]);
  }

  function showResult(r) {
    setState(r.outcome === 'aborted' ? 'done' : 'failed');
    el.setAttribute('data-outcome', r.outcome);
    result.setAttribute('role', r.outcome === 'aborted' ? 'status' : 'alert');
    replaceKids(result, [r.text]);
    setHidden(result, false);
    const kids = [];
    if (r.retry) {
      kids.push(
        button('Retry', {
          icon: ICON.retry,
          cls: 'ic-kit-btn--danger',
          key: 'abort:retry',
          label: `Retry abort ${name}`,
          onClick: () => send(),
        }),
      );
    }
    const closeBtn = button('Close', {
      cls: 'ic-kit-btn--quiet',
      key: 'abort:close',
      onClick: () => close(),
    });
    kids.push(closeBtn);
    replaceKids(actions, kids);
    (r.retry ? kids[0] : closeBtn).focus?.();
  }

  async function send() {
    if (sending || closed) return;
    sending = true;
    setState('sending');
    setHidden(result, false);
    result.setAttribute('role', 'status');
    replaceKids(result, [`Sending abort to ${name}…`]);
    replaceKids(actions, []);
    let outcome;
    try {
      if (typeof ctx?.api?.abortVehicle !== 'function') {
        throw new Error('the console has no command path');
      }
      const res = await ctx.api.abortVehicle(name);
      outcome = abortOutcome(res, name, now());
    } catch (err) {
      outcome = abortFailure(err, name);
    }
    sending = false;
    if (closed) return;
    if (outcome.outcome === 'aborted') aborted = true;
    showResult(outcome);
  }

  function close() {
    if (closed) return;
    closed = true;
    el.remove?.();
    if (openConfirm?.el === el) openConfirm = null;
    focusBack(opener);
    settle(aborted);
  }

  el.addEventListener('keydown', (event) => {
    if (event?.key !== 'Escape') return;
    event.preventDefault?.();
    event.stopPropagation?.();
    if (!sending) close();
  });

  showConfirm();
  host?.append?.(el);
  placeNear(el, anchor);
  keep.focus?.();
  openConfirm = {
    vehicle: name,
    el,
    promise,
    close,
    focusSafe: () => (sending ? null : keep.focus?.()),
    confirm: () => send(),
  };
  return promise;
}

/**
 * Put the confirm next to the button that opened it, so the operator's eye
 * and pointer stay put. Without an anchor (a shortcut, the analyst) it keeps
 * the fixed, centred position from the stylesheet.
 */
function placeNear(el, anchor) {
  const rect = anchor?.getBoundingClientRect?.();
  if (!rect || !(rect.width > 0 || rect.height > 0) || !el?.style) return;
  const box = el.getBoundingClientRect?.() || {};
  const win = globalThis.window || globalThis;
  const vw =
    win.innerWidth || globalThis.document?.documentElement?.clientWidth;
  const vh =
    win.innerHeight || globalThis.document?.documentElement?.clientHeight;
  if (!vw || !vh) return;
  const at = anchorPlacement(rect, box, { width: vw, height: vh });
  el.style.left = `${at.left}px`;
  el.style.top = `${at.top}px`;
  el.setAttribute('data-anchored', at.flipped ? 'above' : 'below');
}

/** Test hook: the open confirm, if any. */
export function _openConfirm() {
  return openConfirm;
}

// ---------------------------------------------------------------------------
// Situation rail
// ---------------------------------------------------------------------------

function textLine(text, cls = 'ic-rail__line', attrs = {}) {
  return h('p', { class: cls, ...attrs }, text);
}

function sectionHead(title, aside) {
  return h(
    'div',
    { class: 'ic-rail__head' },
    h('h3', { class: 'ic-rail__title' }, title),
    aside ? h('span', { class: 'ic-rail__aside' }, aside) : null,
  );
}

function severityCounts(alarms) {
  const counts = { critical: 0, warning: 0, info: 0 };
  for (const a of alarms) {
    if (a?.severity in counts) counts[a.severity] += 1;
  }
  const parts = [];
  if (counts.critical) parts.push(`${counts.critical} critical`);
  if (counts.warning)
    parts.push(
      `${counts.warning} ${counts.warning === 1 ? 'warning' : 'warnings'}`,
    );
  if (counts.info) parts.push(`${counts.info} info`);
  return { counts, text: parts.join(', ') };
}

/** Alarms for the rail: the store's lane, else the graph's alarm nodes. */
export function railAlarms(state) {
  const lane = Array.isArray(state?.alarms) ? state.alarms.filter(Boolean) : [];
  if (lane.length) return lane;
  return nodesOfType(state, 'alarm').map((n) => ({
    id: n.id,
    seq: n.attrs?.seq,
    kind: n.attrs?.kind,
    severity: n.attrs?.severity,
    message: n.subtitle,
    atMs: n.ts_ms,
    vehicle: n.attrs?.vehicle,
  }));
}

/**
 * The last five alarms: critical pinned first (unviewed before viewed), then
 * newest first. Pure.
 */
export function pickAlarms(alarms, unviewedIds = new Set(), limit = 5) {
  const rank = (a) =>
    a?.severity === 'critical' || a?.critical === true
      ? unviewedIds.has(alarmId(a))
        ? 0
        : 1
      : 2;
  return [...alarms]
    .sort(
      (a, b) => rank(a) - rank(b) || (num(b?.atMs) ?? 0) - (num(a?.atMs) ?? 0),
    )
    .slice(0, limit);
}

/**
 * The name the console shows for a node: a feed gets the orb's plain name
 * ("Contacts feed", not the server's "Contacts"), anything else its server
 * label, else the bare id. One naming everywhere: orb, rail, search,
 * inspector.
 */
export function displayLabel(node) {
  if (node?.type === 'feed' || String(node?.id ?? '').startsWith('feed:'))
    return orbFeedLabel(node);
  // An alarm reads by its spec kind label ("BINGO fuel"), as on the orb, the
  // rail and the banner; the server labels it with the humanized kind.
  const alarm = alarmNodeLabel(node);
  if (alarm) return alarm;
  return String(node?.label || bareId(node?.id) || '');
}

/**
 * One feed in the console's words, worded exactly as the orb words it (its
 * name and short state come from orb/text.js), plus the rail's sentence and
 * a status colour. Reads the feed's graph node (status, subtitle) and/or its
 * `meta.feeds` row. Server plumbing ("mcp:uav_list_tracks", URLs) never
 * appears. Pure.
 * @param {string} name feed name, e.g. "contacts" (a "feed:" prefix is fine)
 * @param {{feed?:object, node?:object, now?:number}} [src]
 * @returns {{name:string, label:string, word:string, text:string,
 *   status:'ok'|'warn'|'critical'|'stale'|'unknown', at:number|null}}
 */
export function feedState(
  name,
  { feed = null, node = null, now = Date.now() } = {},
) {
  const key = String(name ?? '').replace(/^feed:/, '');
  const f = feed && typeof feed === 'object' ? feed : {};
  const a = node?.attrs || {};
  const at = num(f.at_ms ?? f.atMs ?? node?.ts_ms);
  const ok = (f.ok ?? a.ok) === true;
  const status = NODE_STATUSES.has(node?.status)
    ? node.status
    : NODE_STATUSES.has(f.status)
      ? f.status
      : ok
        ? 'ok'
        : 'critical';
  const shaped = {
    id: `feed:${key}`,
    type: 'feed',
    label: node?.label,
    status,
    subtitle: String(node?.subtitle ?? f.detail ?? ''),
  };
  const label = orbFeedLabel(shaped);
  const down = status === 'critical';
  const word = String(
    orbFeedWord(shaped, { downSince: down ? at : null }) || '',
  );
  let text;
  if (word === 'Up') {
    text =
      at != null && key !== 'sim'
        ? `${label} up, ${ago(at, now)} ago`
        : `${label} up`;
  } else if (down) {
    text =
      (at != null
        ? `${label} down since ${zulu(at, { seconds: true })}.`
        : `${label} down.`) + ' No reading, not a clear picture.';
  } else {
    text = `${label} ${word ? word[0].toLowerCase() + word.slice(1) : 'state unknown'}`;
  }
  // Substituted readings (real data off or not loaded) are a warning, not
  // an outage.
  const tone = key === 'real_data' && word !== 'Up' ? 'warn' : status;
  return { name: key, label, word, text, status: tone, at };
}

/**
 * The short state of a feed node for a search row or the inspector header,
 * the orb's word ("Up", "Off", "Down since 14:00Z") with the age of an up
 * feed. Never the server plumbing.
 */
export function feedSummary(node, now = Date.now()) {
  const fs = feedState(node?.id, { node, now });
  if (fs.word === 'Up' && fs.at != null && fs.name !== 'sim')
    return `Up, ${ago(fs.at, now)} ago`;
  return fs.word;
}

/**
 * Feed lines for "What's assumed": [{text, status}], one per feed in
 * `meta.feeds` (the theater feed is the rail's heading), worded by feedState.
 * Pass the graph so the lines can read each feed node's subtitle. Pure.
 */
export function feedLines(meta, now = Date.now(), graph = null) {
  const feeds = meta?.feeds && typeof meta.feeds === 'object' ? meta.feeds : {};
  const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  const lines = [];
  for (const name of Object.keys(feeds).sort()) {
    if (name === 'theater') continue;
    const node = nodes.find((n) => n?.id === `feed:${name}`) || null;
    const fs = feedState(name, { feed: feeds[name], node, now });
    lines.push({ text: fs.text, status: fs.status });
  }
  return lines;
}

/** Count lines for "What's assumed" from graph meta. Pure. */
export function honestyLines(meta) {
  const lines = [];
  const assessed = num(meta?.threat_assessed);
  const unassessed = num(meta?.threat_unassessed);
  if (assessed != null && unassessed != null && assessed + unassessed > 0) {
    lines.push(
      `Threat assessed for ${assessed} of ${assessed + unassessed} contacts.` +
        (unassessed ? ` ${unassessed} not assessed.` : ''),
    );
  }
  const dups = num(meta?.duplicates_collapsed);
  if (dups)
    lines.push(
      `${dups} duplicate ${dups === 1 ? 'sighting' : 'sightings'} merged.`,
    );
  const outside = num(meta?.out_of_theater_contacts ?? meta?.out_of_theater);
  if (outside) {
    lines.push(
      `${outside} ${outside === 1 ? 'contact' : 'contacts'} outside this theater ${outside === 1 ? 'is' : 'are'} hidden.`,
    );
  }
  return lines;
}

/**
 * Mount the situation rail.
 * @param {object} host element the rail renders into
 * @param {object} ctx console ctx ({store, bus, api, root, mode, orb})
 * @param {{now?:()=>number, layout?:string, tickMs?:number}} [opts]
 * @returns {{setLayout(layout:string):void, destroy():void, openDrawer():void, closeDrawer():void, render():void, element:object}}
 */
export function createSituation(host, ctx, opts = {}) {
  const now = opts.now || (() => Date.now());
  const bus = ctx?.bus;
  const store = ctx?.store;
  let layout = opts.layout || ctx?.layout || 'wide';
  let drawerOpen = false;
  let destroyed = false;
  let missionsSeen = false;
  const offs = [];

  const root = h('aside', {
    class: 'ic-rail',
    'aria-label': 'Situation',
    'data-layout': layout,
  });
  const body = h('div', { class: 'ic-rail__body' });
  const strip = h('button', {
    type: 'button',
    class: 'ic-rail-strip',
    'aria-haspopup': 'dialog',
    'aria-expanded': 'false',
    'data-key': 'strip',
  });
  const drawerClose = button('Close situation', {
    icon: ICON.close,
    cls: 'ic-kit-btn--quiet ic-rail-drawer__close',
    key: 'drawer:close',
    onClick: () => closeDrawer(),
  });
  const drawer = h(
    'div',
    {
      class: 'ic-rail-drawer',
      role: 'dialog',
      'aria-label': 'Situation',
      hidden: true,
    },
    drawerClose,
  );
  let updatedEl = null;

  strip.addEventListener('click', () => openDrawer());
  drawer.addEventListener('keydown', (event) => {
    if (event?.key !== 'Escape') return;
    event.preventDefault?.();
    event.stopPropagation?.();
    closeDrawer();
  });

  const state = () => store?.get?.() || {};
  const emit = (event, payload) => bus?.emit?.(event, payload);

  function inspect(id) {
    if (!id) return;
    emit('inspect', { id });
  }

  function track(vehicle) {
    if (!vehicle) return;
    emit('track:request', { vehicle, source: 'operator' });
  }

  function abort(vehicle, anchor) {
    return confirmAbort(ctx, vehicle, { now, anchor });
  }

  function viewAlarm(alarm) {
    const id = alarmId(alarm);
    if (id) {
      // The store emits `alarm:viewed` (and a change) itself.
      if (typeof store?.markAlarmViewed === 'function')
        store.markAlarmViewed(id);
      else emit('alarm:viewed', { id });
    }
    if (id && nodeOf(state(), id)) inspect(id);
    render();
  }

  // ---- sections -----------------------------------------------------------

  /**
   * What a section says before the first graph: loading while the service is
   * being asked, and "no reading" (never "no missions" or "no alarms") once it
   * has failed to answer.
   */
  function noGraphLine(st) {
    if (st.status === 'unauthorized')
      return textLine("No reading: the console's access token was rejected.");
    if (st.status === 'offline' || st.status === 'stale')
      return textLine('No reading until the local service answers.');
    return textLine('Loading the intel picture…');
  }

  function updatedLine(st) {
    const status = st.status;
    const g = st.graph;
    const last = num(st.lastLiveAt) ?? num(g?.generated_at_ms);
    if (status === 'unauthorized') {
      return {
        text: "The console's access token was rejected. Quit and reopen the app to get a new one.",
        status: 'critical',
      };
    }
    if (!g && (status === 'offline' || status === 'stale'))
      return {
        text: "No picture yet: the local service isn't answering.",
        status: 'critical',
      };
    if (!g) return { text: 'Loading the intel picture…', status: 'unknown' };
    if (status === 'offline' || status === 'stale') {
      return {
        text:
          last != null
            ? `Last picture ${zulu(last, { seconds: true })}, not live.`
            : 'Not live.',
        status: 'stale',
      };
    }
    if (last == null)
      return { text: 'Update time not reported', status: 'unknown' };
    return {
      text: `Updated ${ago(last, now())} ago`,
      status: now() - last > UPDATED_WARN_MS ? 'warn' : 'ok',
    };
  }

  function theaterSection(st) {
    const g = st.graph;
    const t = g?.theater || {};
    const known = Boolean(t.label) && t.known !== false;
    const kids = [
      h(
        'h2',
        { class: 'ic-rail__theater' },
        known ? t.label : g ? 'No active theater' : 'Theater not known yet',
      ),
    ];
    if (known && t.place) kids.push(textLine(t.place, 'ic-rail__place'));
    if (!known && t.reason) kids.push(textLine(t.reason, 'ic-rail__place'));
    const sim = nodeOf(st, 'feed:sim');
    if (sim) {
      // A picture that is no longer live (the host stopped answering) must
      // not keep saying "Sim running" from the last good poll.
      const up =
        !PICTURE_NOT_LIVE.has(st.status) &&
        (sim.status === 'ok' || sim.status === 'warn');
      kids.push(
        textLine(
          up ? 'Sim running' : 'Sim host not responding',
          'ic-rail__line',
          {
            'data-status': up ? 'ok' : 'critical',
          },
        ),
      );
    }
    const upd = updatedLine(st);
    updatedEl = textLine(upd.text, 'ic-rail__updated', {
      'data-status': upd.status,
    });
    kids.push(updatedEl);
    if (typeof store?.setScope === 'function' && g) {
      const scope = g.scope === 'all' ? 'all' : 'theater';
      const more = num(
        g.meta?.out_of_theater_contacts ?? g.meta?.out_of_theater,
      );
      const allText = more
        ? `All theaters (${more} more ${more === 1 ? 'contact' : 'contacts'})`
        : 'All theaters';
      const mk = (value, text) => {
        const b = h(
          'button',
          {
            type: 'button',
            class: 'ic-rail__scope-btn',
            'aria-pressed': scope === value ? 'true' : 'false',
            'data-key': `scope:${value}`,
          },
          text,
        );
        b.addEventListener('click', () => store.setScope(value));
        return b;
      };
      kids.push(
        h(
          'div',
          { class: 'ic-rail__scope', role: 'group', 'aria-label': 'Scope' },
          mk('theater', 'This theater'),
          mk('all', allText),
        ),
      );
    }
    return h(
      'section',
      { class: 'ic-rail__section', 'data-section': 'theater' },
      ...kids,
    );
  }

  function vehicleRow(node) {
    const a = node.attrs || {};
    const name = node.label || bareId(node.id);
    const vehicle = bareId(node.id);
    const fs = fuelState(a);
    const nameBtn = h(
      'button',
      {
        type: 'button',
        class: 'ic-rail__name',
        'data-key': `veh:${vehicle}`,
        'aria-label': `Inspect ${name}`,
      },
      name,
    );
    nameBtn.addEventListener('click', () => inspect(node.id));
    const head = h(
      'div',
      { class: 'ic-rail__rowhead' },
      nameBtn,
      h(
        'span',
        { class: 'ic-rail__state', 'data-status': node.status },
        vehicleStateWord(a),
      ),
    );
    const kids = [head];
    if (fs.state === 'unknown') {
      kids.push(noReading('No fuel telemetry yet.'));
    } else {
      // Fuel is coloured by its margin to BINGO alone: a vehicle that is
      // critical for another reason (link lost) keeps an honest fuel colour.
      const tone = fuelTone(fs);
      kids.push(fuelBar(fs, { status: tone }));
      const readout = h(
        'p',
        { class: 'ic-rail__readout', 'data-state': fs.state },
        h('span', { class: 'ic-kit-num' }, formatFuel(fs.fuel)),
        fs.bingo != null
          ? h(
              'span',
              { class: 'ic-rail__bingo' },
              `BINGO ${formatFuel(fs.bingo)}`,
            )
          : null,
      );
      kids.push(readout);
      kids.push(
        textLine(fuelSentence(fs), 'ic-rail__margin', {
          'data-state': fs.state,
          'data-status': tone,
        }),
      );
    }
    for (const line of linkLines(a)) {
      kids.push(
        textLine(line.text, 'ic-rail__link', { 'data-status': line.status }),
      );
    }
    const actions = [
      button('Track', {
        icon: ICON.track,
        key: `track:${vehicle}`,
        label: `Track ${name}`,
        onClick: () => track(vehicle),
      }),
    ];
    if (canAbort(a)) {
      actions.push(
        button('Abort', {
          icon: ICON.abort,
          cls: 'ic-kit-btn--danger',
          key: `abort:${vehicle}`,
          label: `Abort ${name}`,
          onClick: (event) => abort(vehicle, event?.currentTarget),
        }),
      );
    }
    kids.push(h('div', { class: 'ic-rail__actions' }, ...actions));
    return h(
      'div',
      {
        class: 'ic-rail__vehicle',
        'data-status': node.status,
        'data-vehicle': vehicle,
      },
      ...kids,
    );
  }

  function fleetSection(st) {
    const vehicles = nodesOfType(st, 'vehicle');
    const kids = [sectionHead('Fleet')];
    if (!st.graph) kids.push(noGraphLine(st));
    else if (!vehicles.length) {
      kids.push(
        textLine('No aircraft reported. The sim may still be starting.'),
      );
    } else kids.push(...vehicles.map(vehicleRow));
    return h(
      'section',
      { class: 'ic-rail__section', 'data-section': 'fleet' },
      ...kids,
    );
  }

  function missionRow(node) {
    const a = node.attrs || {};
    const mid = a.mission_id || bareId(node.id);
    const phase = String(a.phase || '').toLowerCase();
    const running = RUNNING_PHASES.has(phase);
    const kind = missionKindTitle(a.kind) || 'Mission';
    const nameBtn = h(
      'button',
      {
        type: 'button',
        class: 'ic-rail__name',
        'data-key': `msn:${mid}`,
        'aria-label': `Inspect ${kind} ${mid}`,
      },
      kind,
    );
    nameBtn.addEventListener('click', () => inspect(node.id));
    const kids = [
      h(
        'div',
        { class: 'ic-rail__rowhead' },
        nameBtn,
        h('span', { class: 'ic-kit-mono ic-rail__id' }, mid),
        h(
          'span',
          { class: 'ic-rail__state', 'data-phase': phase },
          PHASE_WORD[phase] || humanize(phase) || 'Phase unknown',
        ),
      ),
    ];
    const pct = num(a.progress_pct);
    if (pct != null) {
      kids.push(
        h(
          'div',
          { class: 'ic-rail__progress' },
          progressBar(pct, kind),
          h('span', { class: 'ic-kit-pct' }, `${Math.round(pct)}%`),
        ),
      );
    }
    const wp = a.waypoint && typeof a.waypoint === 'object' ? a.waypoint : null;
    const wpIndex = num(wp?.index ?? a.waypoint_index);
    const wpOf = num(wp?.of ?? a.waypoint_count);
    const detail = [];
    if (wpIndex != null && wpOf != null)
      detail.push(`Waypoint ${wpIndex} of ${wpOf}`);
    const eta = duration(a.eta_s);
    if (eta && running) detail.push(`≈ ${eta} left`);
    if (detail.length) kids.push(textLine(detail.join('   '), 'ic-rail__line'));
    if (a.incomplete_reason)
      kids.push(
        textLine(a.incomplete_reason, 'ic-rail__line', {
          'data-status': 'warn',
        }),
      );
    if (running && a.vehicle) {
      kids.push(
        h(
          'div',
          { class: 'ic-rail__actions' },
          button('Track', {
            icon: ICON.track,
            key: `track-msn:${mid}`,
            label: `Track ${a.vehicle}`,
            onClick: () => track(a.vehicle),
          }),
        ),
      );
    }
    return h(
      'div',
      { class: 'ic-rail__mission', 'data-running': running ? 'true' : 'false' },
      ...kids,
    );
  }

  function missionsSection(st) {
    const missions = nodesOfType(st, 'mission');
    // A mission-phase alarm is evidence one ran, even after a reload.
    if (
      missions.length ||
      railAlarms(st).some((a) => a?.kind === 'mission_phase')
    )
      missionsSeen = true;
    const running = missions.filter((m) =>
      RUNNING_PHASES.has(String(m.attrs?.phase || '').toLowerCase()),
    );
    const done = missions.filter((m) => !running.includes(m));
    const kids = [
      sectionHead(
        'Missions',
        running.length ? `${running.length} running` : null,
      ),
    ];
    if (!st.graph) kids.push(noGraphLine(st));
    else if (!missions.length) {
      // Finished missions leave the picture when the server drops them; once
      // one has been seen, "yet" would deny that it flew.
      kids.push(
        textLine(
          missionsSeen
            ? 'No missions running. Finished missions leave this list when the server drops them.'
            : 'No missions yet. Ask the analyst to plan one; it dry-runs first and asks before anything flies.',
        ),
      );
    } else {
      kids.push(...running.map(missionRow));
      kids.push(...done.slice(0, 3).map(missionRow));
      if (done.length > 3) {
        kids.push(
          textLine(
            `${done.length - 3} earlier missions not shown.`,
            'ic-rail__line ic-rail__muted',
          ),
        );
      }
    }
    return h(
      'section',
      { class: 'ic-rail__section', 'data-section': 'missions' },
      ...kids,
    );
  }

  function alarmRow(alarm, unviewed) {
    const id = alarmId(alarm);
    const sev = SEVERITY_WORD[alarm.severity] ? alarm.severity : 'info';
    const critical = sev === 'critical' || alarm.critical === true;
    const isNew = critical && unviewed.has(id);
    const at = num(alarm.atMs ?? alarm.at_ms);
    const row = h(
      'button',
      {
        type: 'button',
        class: 'ic-rail-alarm',
        'data-severity': sev,
        'data-viewed': isNew ? 'false' : 'true',
        'data-key': `alarm:${id ?? alarm.kind}`,
        title:
          at != null
            ? `${zulu(at, { seconds: true })}, ${ago(at, now())} ago`
            : undefined,
      },
      icon(SEVERITY_ICON[sev]),
      // The severity word leads the kind in one cell, so a narrow rail wraps
      // the words, never the grid. Info is the quiet default: its word is for
      // screen readers only (the glyph shape still says it).
      h(
        'span',
        { class: 'ic-rail-alarm__head' },
        h(
          'span',
          {
            class:
              sev === 'info'
                ? 'ic-rail-alarm__sev ic-kit-vh'
                : 'ic-rail-alarm__sev',
          },
          sev === 'info' ? `${SEVERITY_WORD[sev]}: ` : SEVERITY_WORD[sev],
        ),
        h('span', { class: 'ic-rail-alarm__kind' }, alarmLabel(alarm.kind)),
      ),
      at != null
        ? h('span', { class: 'ic-rail-alarm__time ic-kit-mono' }, zulu(at))
        : null,
      isNew
        ? h(
            'span',
            { class: 'ic-kit-reg ic-rail-alarm__new', 'data-register': 'new' },
            'New',
          )
        : null,
      alarm.message
        ? h('span', { class: 'ic-rail-alarm__msg' }, String(alarm.message))
        : null,
    );
    row.addEventListener('click', () => viewAlarm(alarm));
    return row;
  }

  function alarmsSection(st) {
    const alarms = railAlarms(st);
    const unviewed = new Set(
      (store?.unviewedCritical?.() || [])
        .map((a) => alarmId(a))
        .filter(Boolean),
    );
    const { text } = severityCounts(alarms);
    const kids = [sectionHead('Alarms', text || null)];
    if (!alarms.length)
      kids.push(
        st.graph ? textLine('No alarms this session.') : noGraphLine(st),
      );
    else {
      kids.push(
        ...pickAlarms(alarms, unviewed).map((a) => alarmRow(a, unviewed)),
      );
      if (alarms.length > 5) {
        const ids = nodesOfType(st, 'alarm').map((n) => n.id);
        const more = button(`Show all ${alarms.length}`, {
          cls: 'ic-kit-btn--link',
          key: 'alarms:all',
          onClick: () => {
            const set = new Set(ids);
            ctx?.orb?.filter?.(
              ids.length
                ? (n) => set.has(typeof n === 'string' ? n : n?.id)
                : null,
            );
            emit('search:filter', {
              ids,
              query: 'Alarms',
              source: 'situation',
            });
          },
        });
        kids.push(more);
      }
    }
    return h(
      'section',
      { class: 'ic-rail__section', 'data-section': 'alarms' },
      ...kids,
    );
  }

  function assumedSection(st) {
    const meta = st.graph?.meta || {};
    const kids = [sectionHead("What's assumed")];
    const caveats = Array.isArray(meta.caveats)
      ? meta.caveats.filter(Boolean)
      : [];
    if (caveats.length) {
      kids.push(
        h(
          'ul',
          { class: 'ic-rail__caveats' },
          ...caveats.map((c) =>
            h('li', { class: 'ic-rail__caveat' }, String(c)),
          ),
        ),
      );
    }
    // A picture that is not live cannot vouch for a feed being up: those
    // lines take the stale tone (§4.3 "every node takes the stale styling").
    const notLive = PICTURE_NOT_LIVE.has(st.status);
    for (const line of feedLines(meta, now(), st.graph)) {
      const status = notLive && line.status === 'ok' ? 'stale' : line.status;
      kids.push(
        textLine(line.text, 'ic-rail__line', { 'data-status': status }),
      );
    }
    for (const line of honestyLines(meta)) kids.push(textLine(line));
    if (!st.graph) kids.push(noGraphLine(st));
    kids.push(
      h(
        'p',
        { class: 'ic-rail__legend' },
        h('span', { class: 'ic-kit-legend' }, '≈ estimated'),
        h(
          'span',
          { class: 'ic-kit-legend ic-kit-assumed' },
          'dotted underline: assumed',
        ),
      ),
    );
    if (st.graph)
      kids.push(
        button('Ask to load real data', {
          cls: 'ic-kit-btn--link',
          key: 'ask:real-data',
          onClick: () =>
            emit('ask', {
              text: 'Load real-world data for this theater.',
              draft: true,
            }),
        }),
      );
    return h(
      'section',
      { class: 'ic-rail__section', 'data-section': 'assumed' },
      ...kids,
    );
  }

  // ---- compact strip --------------------------------------------------------

  function renderStrip(st) {
    const vehicles = nodesOfType(st, 'vehicle');
    const missions = nodesOfType(st, 'mission').filter((m) =>
      RUNNING_PHASES.has(String(m.attrs?.phase || '').toLowerCase()),
    );
    const alarms = railAlarms(st);
    const { counts } = severityCounts(alarms);
    const top = counts.critical
      ? 'critical'
      : counts.warning
        ? 'warning'
        : 'info';
    const caveats = Array.isArray(st.graph?.meta?.caveats)
      ? st.graph.meta.caveats.length
      : 0;
    const summary = [];
    const kids = [];
    for (const v of vehicles) {
      const a = v.attrs || {};
      const fs = fuelState(a);
      const label = v.label || bareId(v.id);
      summary.push(
        fs.fuel == null
          ? `${label} fuel no reading`
          : `${label} ${formatFuel(fs.fuel)}${fs.bingo != null ? `, BINGO ${formatFuel(fs.bingo)}` : ''}`,
      );
      kids.push(
        h(
          'span',
          { class: 'ic-rail-strip__veh', 'data-status': v.status },
          fuelBar(fs, { status: fuelTone(fs), vertical: true }),
          h(
            'span',
            { class: 'ic-rail-strip__initial', 'aria-hidden': 'true' },
            label.slice(0, 1),
          ),
        ),
      );
    }
    kids.push(
      h(
        'span',
        { class: 'ic-rail-strip__count' },
        h('span', { class: 'ic-rail-strip__num' }, String(missions.length)),
        h(
          'span',
          { class: 'ic-rail-strip__cap' },
          missions.length === 1 ? 'Mission' : 'Missions',
        ),
      ),
    );
    summary.push(
      `${missions.length} ${missions.length === 1 ? 'mission' : 'missions'} running`,
    );
    kids.push(
      h(
        'span',
        {
          class: 'ic-rail-strip__count ic-rail-strip__alarms',
          'data-severity': alarms.length ? top : 'none',
        },
        icon(SEVERITY_ICON[top]),
        h('span', { class: 'ic-rail-strip__num' }, String(alarms.length)),
        h('span', { class: 'ic-rail-strip__cap' }, 'Alarms'),
      ),
    );
    summary.push(
      counts.critical
        ? `${counts.critical} critical ${counts.critical === 1 ? 'alarm' : 'alarms'}`
        : `${alarms.length} ${alarms.length === 1 ? 'alarm' : 'alarms'}`,
    );
    if (caveats) {
      kids.push(
        h(
          'span',
          {
            class: 'ic-rail-strip__assumed',
            title: 'Some readings are assumed',
          },
          icon(ICON.info),
        ),
      );
      summary.push('some readings assumed');
    }
    strip.setAttribute('aria-label', `Open situation. ${summary.join('. ')}.`);
    replaceKids(strip, kids);
  }

  // ---- render ----------------------------------------------------------------

  function render() {
    if (destroyed) return;
    const key = focusedKey(root);
    const st = state();
    replaceKids(body, [
      theaterSection(st),
      fleetSection(st),
      missionsSection(st),
      alarmsSection(st),
      assumedSection(st),
    ]);
    if (layout === 'compact') {
      renderStrip(st);
      replaceKids(drawer, [drawerClose, body]);
      setHidden(drawer, !drawerOpen);
      strip.setAttribute('aria-expanded', drawerOpen ? 'true' : 'false');
      replaceKids(root, [strip, drawer]);
    } else {
      replaceKids(root, [body]);
    }
    restoreFocus(root, key);
  }

  function tick() {
    if (!updatedEl || destroyed) return;
    const upd = updatedLine(state());
    replaceKids(updatedEl, [upd.text]);
    updatedEl.setAttribute('data-status', upd.status);
  }

  function setLayout(next) {
    const value = ['wide', 'compact', 'narrow'].includes(next) ? next : 'wide';
    if (value === layout) return;
    layout = value;
    if (layout !== 'compact') drawerOpen = false;
    root.setAttribute('data-layout', layout);
    render();
  }

  function openDrawer() {
    if (layout !== 'compact' || drawerOpen) return;
    drawerOpen = true;
    setHidden(drawer, false);
    strip.setAttribute('aria-expanded', 'true');
    setClass(root, 'is-drawer-open', true);
    drawerClose.focus?.();
  }

  function closeDrawer() {
    if (!drawerOpen) return;
    drawerOpen = false;
    setHidden(drawer, true);
    strip.setAttribute('aria-expanded', 'false');
    setClass(root, 'is-drawer-open', false);
    strip.focus?.();
  }

  if (store?.on) {
    const off = store.on('change', () => render());
    if (typeof off === 'function') offs.push(off);
  }
  if (bus?.on) {
    for (const [event, fn] of [
      ['layout', (p) => setLayout(p?.layout)],
      ['alarm:viewed', () => render()],
    ]) {
      const off = bus.on(event, fn);
      if (typeof off === 'function') offs.push(off);
    }
  }
  const tickMs = opts.tickMs ?? 1000;
  let timer = null;
  if (tickMs > 0 && typeof globalThis.setInterval === 'function') {
    timer = globalThis.setInterval(tick, tickMs);
  }

  host?.append?.(root);
  render();

  return {
    element: root,
    render,
    setLayout,
    openDrawer,
    closeDrawer,
    isDrawerOpen: () => drawerOpen,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      if (timer != null) globalThis.clearInterval?.(timer);
      for (const off of offs.splice(0)) off();
      root.remove?.();
    },
  };
}
